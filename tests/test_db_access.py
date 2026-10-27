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
from unittest import mock

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

    def test_planning_pr_observation_releases_access_before_waiting(self):
        from types import SimpleNamespace
        from loop_manager_internal.planning import planner_validation as validation
        from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.execute('CREATE TABLE exec_plans(repo TEXT, plan_id TEXT, branch TEXT, catalog_generation INTEGER)')
            self.connection.execute("INSERT INTO exec_plans VALUES ('repo', 'G-001', 'topic', 7)")
        snapshot = MutableReadySnapshot('repo', 'base', (SimpleNamespace(plan_id='G-001'),), 'token')
        entered, updated = threading.Event(), threading.Event()
        errors = []
        def other_request():
            try:
                self.assertTrue(entered.wait(5))
                with closing(open_connection(self.path)) as other, other:
                    other.execute('UPDATE sample SET value=9 WHERE id=1')
            except BaseException as error:
                errors.append(error)
            finally:
                updated.set()
        def observe(repo, branch):
            self.assertEqual((repo, branch), (Path('repo'), 'topic'))
            self.assertFalse(self.connection.in_transaction)
            self.assertFalse(self.connection._held)
            entered.set()
            self.assertTrue(updated.wait(5))
        effects = validation.AssertMutableReadySnapshotCurrentEffects(mock.Mock(), mock.Mock(), observe)
        worker = threading.Thread(target=other_request)
        worker.start()
        try:
            prepared = validation._observe_mutable_ready_prs(effects, self.connection, snapshot)
            self.assertEqual(prepared.branches, (('G-001', 'topic', 7),))
            self.assertEqual(self.connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 9)
        finally:
            entered.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_maintenance_observation_releases_access_before_waiting(self):
        from loop_manager_internal.storage import maintenance_file_ledger as ledger
        from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory
        from tests.support.fixed_samples import use_fixed_guidance
        use_fixed_guidance(self)
        repo = str(self.path.parent)
        self.connection.execute('CREATE TABLE projects(repo TEXT PRIMARY KEY, base_branch TEXT, generation INTEGER)')
        self.connection.execute("INSERT INTO projects VALUES (?, 'main', 0)", (repo,))
        repository = ledger.FileReviewRepository(self.connection)
        inventory = SourceInventory('a' * 40, (), ())
        entered, updated = threading.Event(), threading.Event()
        errors = []

        def other_request():
            try:
                self.assertTrue(entered.wait(5))
                with closing(open_connection(self.path)) as other, other:
                    other.execute('UPDATE sample SET value=9 WHERE id=1')
                updated.set()
            except BaseException as error:
                errors.append(error)
                updated.set()

        def observe(*args):
            self.assertFalse(self.connection.in_transaction)
            self.assertFalse(self.connection._held)
            entered.set()
            self.assertTrue(updated.wait(5))
            return inventory

        worker = threading.Thread(target=other_request)
        worker.start()
        try:
            with mock.patch.object(ledger, 'tracked_source_inventory', side_effect=observe) as observer, \
                    mock.patch.object(ledger, 'assessment_input_tree', return_value=()), \
                    mock.patch.object(ledger, 'verify_remote_revision') as remote:
                prepared = repository._prepare_evidence(repo, repo, 'main', inventory, (), root_reviews=())
                observer.assert_called_once()
                remote.assert_called_once()
            with mock.patch.object(ledger, '_git', side_effect=AssertionError('Git during apply')), \
                    mock.patch.object(ledger.common_guidance, 'effective_planning_hash',
                                      side_effect=AssertionError('guidance observation during apply')):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'rollback'), self.connection:
                        repository.apply_success(prepared)
                        raise RuntimeError('rollback')
                    self.assertIsNone(repository.last_success(repo))
                repository.apply_success(prepared)
            self.assertIsNotNone(repository.last_success(repo))
            self.assertEqual(self.connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 9)
        finally:
            entered.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_maintenance_prepared_success_rejects_db_races_and_nested_observation(self):
        from loop_manager_internal.storage import maintenance_file_ledger as ledger
        from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory
        from tests.support.fixed_samples import use_fixed_guidance
        use_fixed_guidance(self)
        repo = str(self.path.parent)
        with self.connection:
            self.connection.execute('CREATE TABLE projects(repo TEXT PRIMARY KEY, base_branch TEXT, owner TEXT, generation INTEGER)')
            self.connection.execute("INSERT INTO projects VALUES (?, 'main', 'owner', 0)", (repo,))
            self.connection.execute('CREATE TABLE maintenance_cycles(repo TEXT, revision INTEGER, scope TEXT)')
            self.connection.execute("INSERT INTO maintenance_cycles VALUES (?, 1, 'full')", (repo,))
            self.connection.execute('CREATE TABLE file_assessment_publications(repo TEXT, evidence_json TEXT)')
            self.connection.execute("INSERT INTO file_assessment_publications VALUES (?, 'original intent')", (repo,))
        repository = ledger.FileReviewRepository(self.connection)
        inventory = SourceInventory('a' * 40, (), ())
        mutations = (
            "UPDATE projects SET owner='competitor'",
            "UPDATE projects SET generation=generation+1",
            "UPDATE maintenance_cycles SET revision=revision+1",
            "UPDATE maintenance_cycles SET scope='targeted'",
            "UPDATE file_assessment_publications SET evidence_json='competing intent'",
            "INSERT INTO file_assessment_successes(repo,source_revision,reviewed_at) SELECT repo,'prior','prior' FROM projects",
        )
        with mock.patch.object(ledger, 'tracked_source_inventory', return_value=inventory) as observer, \
                mock.patch.object(ledger, 'assessment_input_tree', return_value=()), \
                mock.patch.object(ledger, 'verify_remote_revision'):
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    prepared = repository._prepare_evidence(repo, repo, 'main', inventory, (), root_reviews=())
                    with self.connection:
                        self.connection.execute(mutation)
                    before = repository._evidence_identity(repo)
                    with self.assertRaises(ledger.FileReviewLedgerError):
                        repository.apply_success(prepared)
                    self.assertEqual(repository._evidence_identity(repo), before)
                    self.assertEqual(repository.reviews(repo), ())
            observer.reset_mock()
            with self.connection, self.assertRaisesRegex(ledger.FileReviewLedgerError, 'transaction'):
                repository.record_success(repo, repo, 'main', inventory, (), root_reviews=())
            observer.assert_not_called()

    def test_existing_only_writer_cannot_create_missing_database(self):
        missing = self.path.parent / 'missing.db'
        with self.assertRaises(sqlite3.OperationalError):
            open_connection(missing, create=False)
        self.assertFalse(missing.exists())
        with closing(open_connection(self.path, create=False)) as connection, connection:
            connection.execute('UPDATE sample SET value=7 WHERE id=1')
        self.assertEqual(self.connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 7)

    def test_open_iteration_does_not_retain_a_read_lock(self):
        cursor = self.connection.execute('SELECT * FROM sample ORDER BY id')
        self.assertEqual(cursor.fetchone(), (1, 0))
        with closing(open_connection(self.path)) as other, other:
            other.execute('UPDATE sample SET value=4 WHERE id=2')
        with self.connection:
            self.connection.execute('UPDATE sample SET value=7 WHERE id=1')
        self.assertEqual(cursor.fetchall(), [(2, 0), (3, 0)])
        self.assertEqual(self.connection.execute('SELECT value FROM sample ORDER BY id').fetchall(), [(7,), (4,), (0,)])

    def test_nested_helpers_and_scripts_do_not_commit_the_outer_transaction(self):
        from loop_manager_internal.storage.runtime_state import _transaction
        with self.assertRaisesRegex(ValueError, 'outer'), _transaction(self.connection):
            self.connection.execute('UPDATE sample SET value=1 WHERE id=1')
            with _transaction(self.connection), self.connection:
                self.connection.executescript("UPDATE sample SET value=2 WHERE id=2; -- semicolon ;\n"
                                              "UPDATE sample SET value=3 WHERE id=3;")
                self.connection.commit()
            raise ValueError('outer')
        self.assertEqual(self.connection.execute('SELECT value FROM sample').fetchall(), [(0,), (0,), (0,)])
        self.assertFalse(self.connection._held)

    def test_caught_inner_error_rolls_back_only_its_savepoint(self):
        with self.connection:
            self.connection.execute('UPDATE sample SET value=1 WHERE id=1')
            with self.assertRaises(ValueError), self.connection:
                self.connection.execute('UPDATE sample SET value=2 WHERE id=2')
                raise ValueError('inner')
            self.connection.execute('UPDATE sample SET value=3 WHERE id=3')
        self.assertEqual(self.connection.execute('SELECT value FROM sample').fetchall(), [(1,), (0,), (3,)])

    def test_outer_context_owns_a_preexisting_implicit_transaction(self):
        self.connection.execute('SAVEPOINT prior_request')
        self.connection.execute('RELEASE prior_request')
        self.connection.execute('UPDATE sample SET value=5 WHERE id=1')
        with self.connection:
            self.connection.execute('UPDATE sample SET value=6 WHERE id=2')
        self.assertFalse(self.connection.in_transaction)
        with closing(open_connection(self.path, read_only=True)) as other:
            self.assertEqual(other.execute('SELECT value FROM sample').fetchall(), [(5,), (6,), (0,)])

    def test_explicit_transaction_survives_inner_helper_and_trigger_script(self):
        self.connection.execute('BEGIN IMMEDIATE')
        with self.connection:
            self.connection.executescript('''CREATE TRIGGER sample_change AFTER UPDATE ON sample
                WHEN NEW.id=1 BEGIN UPDATE sample SET value=8 WHERE id=2; END;
                UPDATE sample SET value=4 WHERE id=1;''')
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(self.connection.execute('SELECT value FROM sample').fetchall(), [(0,), (0,), (0,)])
        self.assertIsNone(self.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchone())

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
        with closing(open_connection(self.path, timeout=.03)) as blocked:
            with self.assertRaises(DatabaseWaitTimeout):
                blocked.execute('SELECT * FROM sample')
            self.assertFalse(blocked._held)
            self.assertIsNone(blocked._gate.owner)
        with closing(open_connection(self.path)) as blocked, \
                mock.patch('loop_manager_internal.storage.db_access._pause', side_effect=RuntimeError('cancelled')):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                blocked.execute('SELECT * FROM sample')
            self.assertFalse(blocked._held)
            self.assertIsNone(blocked._gate.owner)
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
            with mock.patch('loop_manager_internal.storage.db_access.time.monotonic', side_effect=[0.0, 0.0, 30.0, 30.0]):
                with _waiting(), _waiting():
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

    def test_request_retries_the_whole_transaction_with_current_generation(self):
        from loop_manager_internal.storage import db_access
        from loop_manager_internal.storage.runtime_state import _transaction
        auxiliary = self.path.with_name('auxiliary.db')
        with closing(sqlite3.connect(auxiliary)) as writer:
            writer.execute('CREATE TABLE sample(value)')
            writer.execute('INSERT INTO sample VALUES (0)')
            writer.commit()
            for generation in (0, 1):
                with self.subTest(generation=generation):
                    with self.connection:
                        self.connection.execute('UPDATE sample SET value=0')
                    writer.execute('UPDATE sample SET value=20')
                    retry, resume = threading.Event(), threading.Event()
                    attempts, results, errors = [], [], []
                    effect = mock.Mock(return_value='external-result')
                    effect_result = effect()
                    pause = db_access._pause
                    def wait_after_rollback(deadline, interval=.01):
                        retry.set()
                        if not resume.wait(5):
                            raise AssertionError('retry was not resumed')
                        pause(deadline, interval)
                    def apply(connection):
                        values = connection.execute('SELECT value FROM sample ORDER BY id').fetchall()
                        attempts.append((values[0][0], values[1][0]))
                        if values[1][0] != 0:
                            return False
                        self.assertEqual(effect_result, 'external-result')
                        with _transaction(connection):
                            connection.execute('UPDATE sample SET value=? WHERE id=1', (values[0][0]+1,))
                            if len(attempts) == 1:
                                # Attach after BEGIN so the contention occurs in
                                # the body, after a write, rather than at intake.
                                connection.execute('ATTACH DATABASE ? AS auxiliary', (str(auxiliary),))
                            # This real lock occurs after the main DB was changed.
                            # Even a helper catching the wait cannot commit a partial request.
                            try:
                                connection.execute('UPDATE auxiliary.sample SET value=value+1')
                            except DatabaseWaitTimeout:
                                if generation:
                                    raise
                        return True
                    def run():
                        try:
                            with closing(open_connection(self.path)) as connection:
                                results.append(connection.run_request(apply))
                                self.assertFalse(connection.in_transaction)
                                self.assertFalse(connection._held)
                        except BaseException as error:
                            errors.append(error)
                    with mock.patch.object(db_access, '_pause', side_effect=wait_after_rollback):
                        thread = threading.Thread(target=run)
                        thread.start()
                        try:
                            self.assertTrue(retry.wait(5))
                            with closing(sqlite3.connect(self.path)) as competitor:
                                self.assertEqual(competitor.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 0)
                                competitor.execute('UPDATE sample SET value=10 WHERE id=1')
                                competitor.execute('UPDATE sample SET value=? WHERE id=2', (generation,))
                                competitor.commit()
                            writer.commit()
                        finally:
                            resume.set()
                            thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(attempts, [(0, 0), (10, generation)])
                    self.assertEqual(results, [not generation])
                    self.assertEqual(self.connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0],
                                     10 if generation else 11)
                    self.assertEqual(writer.execute('SELECT value FROM sample').fetchone()[0], 20 if generation else 21)
                    effect.assert_called_once_with()

    def test_commit_retries_share_one_deadline_and_timeout_releases_the_turn(self):
        with closing(sqlite3.connect(self.path)) as reader, closing(open_connection(self.path, timeout=.05)) as connection:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM sample').fetchall()
            attempts, deadlines = [], []
            acquire = connection._gate.acquire
            def record(owner, deadline):
                deadlines.append(deadline)
                return acquire(owner, deadline)
            def change(current):
                attempts.append(current.execute('SELECT value FROM sample WHERE id=1').fetchone()[0])
                current.execute('UPDATE sample SET value=value+1 WHERE id=1')
            with mock.patch.object(connection._gate, 'acquire', side_effect=record):
                with self.assertRaises(DatabaseWaitTimeout):
                    connection.run_request(change)
            self.assertGreater(len(attempts), 1)
            self.assertEqual(set(attempts), {0})
            self.assertEqual(len(set(deadlines)), 1)
            self.assertFalse(connection.in_transaction)
            self.assertFalse(connection._held)
            reader.rollback()
            connection.run_request(change)
            self.assertEqual(connection.execute('SELECT value FROM sample WHERE id=1').fetchone()[0], 1)

    def test_commit_cancellation_and_retry_cancellation_roll_back(self):
        from loop_manager_internal.storage import db_access
        for retrying in (False, True):
            with self.subTest(retrying=retrying), closing(sqlite3.connect(self.path)) as reader, \
                    closing(open_connection(self.path)) as connection:
                reader.execute('BEGIN')
                reader.execute('SELECT * FROM sample').fetchall()
                cancelled = [False]
                def check():
                    if cancelled[0]:
                        raise RuntimeError('cancelled')
                def stop_retry(deadline, interval=.01):
                    self.assertFalse(connection._held)
                    cancelled[0] = True
                    db_access._check_wait(deadline)
                with mock.patch('loop_manager_internal.runtime.handoff_gate._check_runtime_handoff', side_effect=check):
                    if retrying:
                        with mock.patch.object(db_access, '_pause', side_effect=stop_retry), \
                                self.assertRaisesRegex(RuntimeError, 'cancelled'):
                            connection.run_request(lambda current: current.execute('UPDATE sample SET value=3'))
                    else:
                        connection.execute('UPDATE sample SET value=3')
                        cancelled[0] = True
                        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                            connection.commit()
                self.assertFalse(connection.in_transaction)
                self.assertFalse(connection._held)
                reader.rollback()
                self.assertEqual(connection.execute('SELECT value FROM sample').fetchall(), [(0,), (0,), (0,)])

    def test_explicit_commit_disables_native_wait_and_times_out_without_retaining_transaction(self):
        with closing(sqlite3.connect(self.path)) as reader, closing(open_connection(self.path, timeout=.03)) as connection:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM sample').fetchall()
            connection.execute('UPDATE sample SET value=4')
            connection.execute('PRAGMA busy_timeout=2000')
            with self.assertRaises(DatabaseWaitTimeout):
                connection.commit()
            self.assertFalse(connection.in_transaction)
            self.assertFalse(connection._held)
            self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 0)
            reader.rollback()
            self.assertEqual(connection.execute('SELECT value FROM sample').fetchall(), [(0,), (0,), (0,)])

    def test_request_does_not_retry_sql_errors_or_take_over_a_caller_transaction(self):
        calls = []
        def invalid(connection):
            calls.append(1)
            connection.execute('UPDATE sample SET value=7')
            connection.execute('UPDATE missing_table SET value=8')
        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.connection.run_request(invalid)
        self.assertNotIsInstance(raised.exception, DatabaseWaitTimeout)
        self.assertEqual(calls, [1])
        self.assertFalse(self.connection._held)
        self.assertEqual(self.connection.execute('SELECT value FROM sample').fetchall(), [(0,), (0,), (0,)])
        with self.connection:
            self.connection.execute('UPDATE sample SET value=2')
            with self.assertRaisesRegex(RuntimeError, 'transaction 밖'):
                self.connection.run_request(invalid)
        self.assertEqual(calls, [1])

    def test_request_handoff_before_commit_rolls_back_and_returns_access(self):
        from loop_manager_internal.runtime.handoff_gate import (
            RuntimeHandoffGate, SelfMergeRuntimeHandoff, _runtime_handoff_signal,
        )
        gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
        token = _runtime_handoff_signal.set(gate)
        try:
            def change(current):
                current.execute('UPDATE sample SET value=8')
                gate.stop()
            with self.assertRaises(SelfMergeRuntimeHandoff):
                self.connection.run_request(change)
            self.assertFalse(self.connection._held)
            self.assertFalse(self.connection.in_transaction)
            # Immediate completion observation remains possible after handoff.
            self.assertEqual(self.connection.execute('SELECT value FROM sample').fetchall(), [(0,), (0,), (0,)])
        finally:
            _runtime_handoff_signal.reset(token)

    def test_process_fifo_survives_repeated_requests_and_terminated_waiters(self):
        code = '''from pathlib import Path
from loop_manager_internal.storage.db_access import open_connection, _Gate
import sys
join = _Gate._join_process_queue
def queued(self, deadline):
 join(self, deadline)
 print('QUEUED', flush=True)
_Gate._join_process_queue = queued
connection = open_connection(Path(sys.argv[1]))
connection.run_request(lambda current: current.execute('UPDATE sample SET value=value+1 WHERE id=1'))
connection.close()
'''
        for terminated in (False, True):
            with self.subTest(terminated=terminated):
                with self.connection:
                    self.connection.execute('UPDATE sample SET value=0')
                    process = subprocess.Popen([sys.executable, '-c', code, str(self.path)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.addCleanup(lambda process=process: process.kill() if process.poll() is None else None)
                    self.assertEqual(process.stdout.readline().strip(), 'QUEUED')
                    if terminated:
                        process.kill()
                        process.wait(5)
                observed = []
                def repeat(current):
                    observed.append(current.execute('SELECT value FROM sample WHERE id=1').fetchone()[0])
                    current.execute('UPDATE sample SET value=value+10 WHERE id=1')
                for _ in range(3):
                    self.connection.run_request(repeat)
                output, error = process.communicate(timeout=5)
                self.assertEqual(observed, [0, 10, 20] if terminated else [1, 11, 21], error)
                if not terminated:
                    self.assertEqual(process.returncode, 0, error)

    def test_backup_accounts_only_confirmed_busy_sleep_and_keeps_process_time(self):
        from loop_manager_internal.verification.execution_clock import ExecutionClock
        for active in (0, 1):
            with self.subTest(active_processes=active), closing(sqlite3.connect(self.path)) as external, \
                    closing(open_connection(self.path, timeout=.03)) as source, \
                    closing(sqlite3.connect(':memory:')) as target:
                external.execute('BEGIN EXCLUSIVE')
                now, statuses = [0.0], []
                with ExecutionClock(clock=lambda: now[0]) as budget:
                    budget.active_processes = active
                    def sleep(seconds):
                        now[0] += seconds
                    with mock.patch('loop_manager_internal.storage.db_access.time.monotonic', side_effect=lambda: now[0]), \
                            mock.patch('loop_manager_internal.storage.db_access.time.sleep', side_effect=sleep):
                        with self.assertRaises(DatabaseWaitTimeout):
                            source.backup(target, progress=lambda status, *_: statuses.append(status), sleep=10)
                    self.assertTrue(statuses)
                    self.assertEqual(set(statuses), {sqlite3.SQLITE_BUSY})
                    self.assertAlmostEqual(budget.elapsed(), .03 if active else 0)
                self.assertFalse(source._held)
                external.rollback()
                source.backup(target)
                self.assertEqual(target.execute('SELECT count(*) FROM sample').fetchone()[0], 3)

    def test_backup_cancellation_releases_access_for_the_next_request(self):
        with closing(sqlite3.connect(self.path)) as external, closing(sqlite3.connect(':memory:')) as target:
            external.execute('BEGIN EXCLUSIVE')
            busy = [False]
            def check():
                if busy[0]:
                    raise RuntimeError('cancelled')
            def progress(status, *_):
                busy[0] = status == sqlite3.SQLITE_BUSY
            with mock.patch('loop_manager_internal.runtime.handoff_gate._check_runtime_handoff', side_effect=check), \
                    self.assertRaisesRegex(RuntimeError, 'cancelled'):
                self.connection.backup(target, progress=progress)
            self.assertFalse(self.connection._held)
            external.rollback()
            self.connection.backup(target)
            self.assertEqual(target.execute('SELECT count(*) FROM sample').fetchone()[0], 3)
