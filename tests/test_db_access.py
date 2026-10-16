"""실제 SQLite 파일·스레드·별도 프로세스의 공통 접근 경계를 확인한다."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from loop_manager_internal.storage.db_access import open_connection, DatabaseWaitTimeout


class DatabaseAccessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'state.db'
        self.connection = open_connection(self.path)
        self.addCleanup(self.connection.close)
        with self.connection:
            self.connection.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY, value INTEGER)')
            self.connection.executemany('INSERT INTO sample VALUES (?,0)', [(1,), (2,), (3,)])

    def test_open_iteration_does_not_retain_a_read_lock(self):
        cursor = self.connection.execute('SELECT * FROM sample ORDER BY id')
        self.assertEqual(cursor.fetchone(), (1, 0))
        with closing(open_connection(self.path)) as other, other:
            other.execute('UPDATE sample SET value=4 WHERE id=2')
        with self.connection:
            self.connection.execute('UPDATE sample SET value=7 WHERE id=1')
        self.assertEqual(cursor.fetchall(), [(2, 0), (3, 0)])
        self.assertEqual(self.connection.execute('SELECT value FROM sample ORDER BY id').fetchall(), [(7,), (4,), (0,)])

    def test_threads_queue_and_keep_read_modify_write_atomic(self):
        errors = []
        started, finished = threading.Event(), threading.Event()
        def change():
            try:
                with closing(open_connection(self.path)) as connection:
                    started.set()
                    for _ in range(8):
                        with connection:
                            value = connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0]
                            connection.execute('UPDATE sample SET value=? WHERE id=1', (value+1,))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        with self.connection:
            threads = [threading.Thread(target=change) for _ in range(3)]
            for thread in threads: thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(.05))
        for thread in threads: thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(self.connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 24)

    def test_same_thread_nested_connection_is_rejected_without_deadlock(self):
        with closing(open_connection(self.path)) as other:
            with self.connection:
                other.commit()
                other.rollback()
                with self.assertRaisesRegex(RuntimeError, '다른 연결'):
                    other.execute('SELECT * FROM sample')
                with self.connection:
                    self.connection.execute('UPDATE sample SET value=3 WHERE id=1')
        self.assertEqual(self.connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 3)

    def test_rollback_and_close_release_the_transaction(self):
        with self.assertRaises(ValueError), self.connection:
            self.connection.execute('UPDATE sample SET value=3 WHERE id=1')
            raise ValueError('abort')
        self.connection.execute('UPDATE sample SET value=9 WHERE id=1')
        self.connection.close()
        with closing(open_connection(self.path)) as other:
            self.assertEqual(other.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 0)

    def test_read_only_keeps_database_bytes_and_never_creates_a_missing_database(self):
        before = self.path.read_bytes()
        with closing(open_connection(self.path, read_only=True)) as reader, reader:
            self.assertEqual(reader.execute('SELECT count(*) FROM sample').fetchone()[0], 3)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute('UPDATE sample SET value=2')
        self.assertEqual(self.path.read_bytes(), before)
        missing = self.path.with_name('missing.db')
        with self.assertRaises(sqlite3.OperationalError): open_connection(missing, read_only=True)
        self.assertFalse(missing.exists())

    def test_external_writer_timeout_preserves_state_and_next_request_runs(self):
        with closing(sqlite3.connect(self.path)) as external, closing(open_connection(self.path, timeout=.08)) as owned:
            external.execute('UPDATE sample SET value=5 WHERE id=2')
            with self.assertRaises(DatabaseWaitTimeout):
                owned.execute('UPDATE sample SET value=6 WHERE id=1')
            self.assertFalse(owned.in_transaction)
            external.rollback()
            with owned: owned.execute('UPDATE sample SET value=7 WHERE id=1')
        self.assertEqual(self.connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 7)

    def test_cancelled_queue_request_does_not_retain_its_turn(self):
        from unittest import mock
        errors = []
        def read():
            try:
                with closing(open_connection(self.path)) as other:
                    other.execute('SELECT * FROM sample')
            except RuntimeError as error:
                errors.append(str(error))
        with self.connection, mock.patch(
                'loop_manager_internal.runtime.handoff_gate._check_runtime_handoff',
                side_effect=RuntimeError('cancelled')):
            thread = threading.Thread(target=read)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['cancelled'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM sample').fetchone()[0], 3)

    def test_independent_database_is_not_blocked(self):
        with self.connection, closing(open_connection(self.path.with_name('other.db'))) as other, other:
            other.execute('CREATE TABLE other(value)')

    def test_process_waits_for_commit_then_reads_current_state(self):
        code = """from pathlib import Path
from loop_manager_internal.storage.db_access import open_connection
import sys
with open_connection(Path(sys.argv[1])) as connection:
 connection.execute('UPDATE sample SET value=11 WHERE id=1')
 print('READY', flush=True)
 sys.stdin.readline()
connection.close()
"""
        process = subprocess.Popen([sys.executable, '-c', code, str(self.path)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertEqual(process.stdout.readline().strip(), 'READY')
        result, errors = [], []
        started, done = threading.Event(), threading.Event()
        def read():
            try:
                with closing(open_connection(self.path, read_only=True)) as connection:
                    started.set()
                    result.append(connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0])
            except BaseException as error: errors.append(error)
            finally: done.set()
        thread = threading.Thread(target=read); thread.start()
        self.assertTrue(started.wait(2))
        self.assertFalse(done.wait(.08))
        output, error = process.communicate('continue\n', timeout=5)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual((process.returncode, errors, result), (0, [], [11]), error)

    def test_db_wait_is_excluded_only_before_actual_process_execution(self):
        from unittest import mock
        from loop_manager_internal.storage.db_access import _waiting
        from loop_manager_internal.verification.execution_clock import ExecutionClock
        now = [0.0]
        with ExecutionClock(clock=lambda: now[0]) as budget:
            with mock.patch('loop_manager_internal.storage.db_access.time.monotonic', side_effect=[0.0, 30.0]):
                with _waiting():
                    pass
            now[0] = 32.0
            self.assertEqual(budget.elapsed(), 2.0)
            self.assertEqual(budget.remaining(10), 8.0)
            budget.active_processes = 1
            with mock.patch('loop_manager_internal.storage.db_access.time.monotonic', return_value=32.0):
                with _waiting():
                    now[0] = 34.0
            self.assertEqual(budget.elapsed(), 4.0)

    def test_alias_path_uses_the_same_transaction_boundary(self):
        alias = self.path.parent / 'uncreated' / '..' / self.path.name
        with closing(open_connection(alias)) as other, self.connection:
            with self.assertRaisesRegex(RuntimeError, '다른 연결'):
                other.execute('SELECT * FROM sample')

    def test_closed_connection_does_not_capture_the_queue(self):
        self.connection.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.connection.commit()
        with closing(open_connection(self.path)) as other, other:
            other.execute('UPDATE sample SET value=2')
