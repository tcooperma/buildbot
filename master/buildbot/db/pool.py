# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import time
import traceback
import shutil
import os
import sqlalchemy as sa
import twisted
from twisted.internet import reactor, threads, defer, task
import tempfile
from twisted.python import threadpool, failure, versions, log

# set this to True for *very* verbose query debugging output; this can
# be monkey-patched from master.cfg, too:
#     from buildbot.db import pool
#     pool.debug = True
debug = False

# Hack for bug #1992.  In as-yet-unknown circumstances, select() fails to
# notice that a selfpipe has been written to, thus causing callFromThread, as
# used in deferToThreadPool, to hang indefinitely.  The workaround is to wake
# up the select loop every second by ensuring that there is an event occuring
# every second, with this busy loop:
def bug1992hack(f):
    def w(*args, **kwargs):
        busyloop = task.LoopingCall(lambda : None)
        busyloop.start(1)
        d = f(*args, **kwargs)
        def stop_loop(r):
            busyloop.stop()
            return r
        d.addBoth(stop_loop)
        return d
    w.__name__ = f.__name__
    w.__doc__ = f.__doc__
    return w



def timed_do_fn(f):
    """Decorate a do function to log before, after, and elapsed time,
    with the name of the calling function.  This is not speedy!"""
    def wrap(*args, **kwargs):
        # get a description of the function that called us
        st = traceback.extract_stack(limit=2)
        file, line, name, _ = st[0]
        descr = "%s ('%s' line %d)" % (name, file, line)

        start_time = time.time()
        log.msg("%s - before" % (descr,))
        d = f(*args, **kwargs)
        def after(x):
            end_time = time.time()
            elapsed = (end_time - start_time) * 1000
            log.msg("%s - after (%0.2f ms elapsed)" % (descr, elapsed))
            return x
        d.addBoth(after)
        return d
    wrap.__name__ = f.__name__
    wrap.__doc__ = f.__doc__
    return wrap

class DBThreadPool(threadpool.ThreadPool):

    running = False

    # Some versions of SQLite incorrectly cache metadata about which tables are
    # and are not present on a per-connection basis.  This cache can be flushed
    # by querying the sqlite_master table.  We currently assume all versions of
    # SQLite have this bug, although it has only been observed in 3.4.2.  A
    # dynamic check for this bug would be more appropriate.  This is documented
    # in bug #1810.
    __broken_sqlite = False

    def __init__(self, engine):
        pool_size = 5

        # If the engine has an C{optimal_thread_pool_size} attribute, then the
        # maxthreads of the thread pool will be set to that value.  This is
        # most useful for SQLite in-memory connections, where exactly one
        # connection (and thus thread) should be used.
        if hasattr(engine, 'optimal_thread_pool_size'):
            pool_size = engine.optimal_thread_pool_size

        threadpool.ThreadPool.__init__(self,
                        minthreads=1,
                        maxthreads=pool_size,
                        name='DBThreadPool')
        self.engine = engine
        if engine.dialect.name == 'sqlite':
            vers = self.get_sqlite_version()
            log.msg("Using SQLite Version %s" % (vers,))
            if vers < (3,3,17):
                log.msg("NOTE: this old version of SQLite does not support "
                        "multiple simultaneous accesses to the database; "
                        "add the 'pool_size=1' argument to your db url")
            brkn = self.__broken_sqlite = self.detect_bug1810()
            if brkn:
                log.msg("Applying SQLite workaround from Buildbot bug #1810")
        self._start_evt = reactor.callWhenRunning(self._start)

        # patch the do methods to do verbose logging if necessary
        if debug:
            self.do = timed_do_fn(self.do)
            self.do_with_engine = timed_do_fn(self.do_with_engine)

    def _start(self):
        self._start_evt = None
        if not self.running:
            self.start()
            self._stop_evt = reactor.addSystemEventTrigger(
                    'during', 'shutdown', self._stop)
            self.running = True

    def _stop(self):
        self._stop_evt = None
        self.stop()
        self.engine.dispose()
        self.running = False

    def shutdown(self):
        """Manually stop the pool.  This is only necessary from tests, as the
        pool will stop itself when the reactor stops under normal
        circumstances."""
        if not self._stop_evt:
            return # pool is already stopped
        reactor.removeSystemEventTrigger(self._stop_evt)
        self._stop()

    @bug1992hack
    def do(self, callable, *args, **kwargs):
        def thd():
            conn = self.engine.contextual_connect()
            if self.__broken_sqlite: # see bug #1810
                conn.execute("select * from sqlite_master")
            try:
                rv = callable(conn, *args, **kwargs)
                assert not isinstance(rv, sa.engine.ResultProxy), \
                        "do not return ResultProxy objects!"
            finally:
                conn.close()
            return rv
        return threads.deferToThreadPool(reactor, self, thd)

    @bug1992hack
    def do_with_engine(self, callable, *args, **kwargs):
        def thd():
            if self.__broken_sqlite: # see bug #1810
                self.engine.execute("select * from sqlite_master")
            rv = callable(self.engine, *args, **kwargs)
            assert not isinstance(rv, sa.engine.ResultProxy), \
                    "do not return ResultProxy objects!"
            return rv
        return threads.deferToThreadPool(reactor, self, thd)

    # older implementations for twisted < 0.8.2, which does not have
    # deferToThreadPool; this basically re-implements it, although it gets some
    # of the synchronization wrong - the thread may still be "in use" when the
    # deferred fires in the parent, which can lead to database accesses hopping
    # between threads.  In practice, this should not cause any difficulty.
    @bug1992hack
    def do_081(self, callable, *args, **kwargs): # pragma: no cover
        d = defer.Deferred()
        def thd():
            try:
                conn = self.engine.contextual_connect()
                if self.__broken_sqlite: # see bug #1810
                    conn.execute("select * from sqlite_master")
                try:
                    rv = callable(conn, *args, **kwargs)
                    assert not isinstance(rv, sa.engine.ResultProxy), \
                            "do not return ResultProxy objects!"
                finally:
                    conn.close()
                reactor.callFromThread(d.callback, rv)
            except:
                reactor.callFromThread(d.errback, failure.Failure())
        self.callInThread(thd)
        return d

    @bug1992hack
    def do_with_engine_081(self, callable, *args, **kwargs): # pragma: no cover
        d = defer.Deferred()
        def thd():
            try:
                conn = self.engine
                if self.__broken_sqlite: # see bug #1810
                    conn.execute("select * from sqlite_master")
                rv = callable(conn, *args, **kwargs)
                assert not isinstance(rv, sa.engine.ResultProxy), \
                        "do not return ResultProxy objects!"
                reactor.callFromThread(d.callback, rv)
            except:
                reactor.callFromThread(d.errback, failure.Failure())
        self.callInThread(thd)
        return d

    # use the 0.8.1 versions on old Twisteds
    if twisted.version < versions.Version('twisted', 8, 2, 0):
        do = do_081
        do_with_engine = do_with_engine_081

    def detect_bug1810(self):
        # detect buggy SQLite implementations; call only for a known-sqlite
        # dialect
        try:
            import pysqlite2.dbapi2 as sqlite
            sqlite = sqlite
        except ImportError:
            import sqlite3 as sqlite

        tmpdir = tempfile.mkdtemp()
        dbfile = os.path.join(tmpdir, "detect_bug1810.db")
        def test(select_from_sqlite_master=False):
            conn1 = None
            conn2 = None
            try:
                conn1 = sqlite.connect(dbfile)
                curs1 = conn1.cursor()
                curs1.execute("PRAGMA table_info('foo')")

                conn2 = sqlite.connect(dbfile)
                curs2 = conn2.cursor()
                curs2.execute("CREATE TABLE foo ( a integer )")

                if select_from_sqlite_master:
                    curs1.execute("SELECT * from sqlite_master")
                curs1.execute("SELECT * from foo")
            finally:
                if conn1:
                    conn1.close()
                if conn2:
                    conn2.close()
                os.unlink(dbfile)

        try:
            test()
        except sqlite.OperationalError:
            # this is the expected error indicating it's broken
            shutil.rmtree(tmpdir)
            return True

        # but this version should not fail..
        test(select_from_sqlite_master=True)
        shutil.rmtree(tmpdir)
        return False # not broken - no workaround required

    def get_sqlite_version(self):
        engine = sa.create_engine('sqlite://')
        conn = engine.contextual_connect()

        try:
            r = conn.execute("SELECT sqlite_version()")
            vers_row = r.fetchone()
            r.close()
        except:
            return (0,)

        if vers_row:
            try:
                return tuple(map(int, vers_row[0].split('.')))
            except (TypeError, ValueError):
                return (0,)
        else:
            return (0,)
