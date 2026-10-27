import hashlib
import json
from pathlib import Path
import sqlite3
import os
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock, skipUnless

from loop_manager_internal.agents import process_receipt
from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.storage.agent_invocation_state import InvocationRepository, ensure_schema


class ProcessReceiptTest(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        runtime = self.state / 'agent-runtime' / ('a' * 64)
        runtime.mkdir(parents=True)
        self.launch = SimpleNamespace(runtime=runtime, attempt=runtime / 'call-one')
        self.path = process_receipt.receipt_path(self.launch)
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        ensure_schema(self.connection)
        self.repository = InvocationRepository(self.connection)
        self.identity = self.repository.start(InvocationContext('repo', None), 'planner', 'codex',
                                              ResolvedProfile('model', 'high'), 'session')
        self.repository.bind_process_receipt(self.identity, str(self.path))
        self.repository.mark_execution_started(self.identity)

    def row(self):
        return self.connection.execute('SELECT * FROM agent_invocations').fetchone()

    def started(self):
        with mock.patch.object(process_receipt.sys, 'platform', 'linux'), \
             mock.patch.object(process_receipt, '_linux_host', return_value='host'), \
             mock.patch.object(process_receipt, '_boot', return_value='boot'):
            process_receipt.started(self.launch, SimpleNamespace(pid=1234))

    def test_result_message_uses_only_the_matching_attempt_artifact(self):
        self.launch.attempt.mkdir()
        source = self.launch.attempt / 'last-message.txt'
        source.write_text('original response\n', encoding='utf-8')
        expected = hashlib.sha256(b'original response').hexdigest()
        self.assertEqual(process_receipt.result_message(str(self.path), expected), 'original response')

        shared = (self.state / 'logs' / hashlib.sha256(str(Path('/worktree')).encode()).hexdigest()[:16]
                  / 'reviewer-last.txt')
        shared.parent.mkdir(parents=True)
        shared.write_text('another invocation response', encoding='utf-8')
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            process_receipt.result_message(str(self.path), expected)

        source.write_text('replacement response', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '지문이 다릅니다'):
            process_receipt.result_message(str(self.path), expected)

    def test_result_message_rejects_symlinked_attempt_artifacts(self):
        self.launch.attempt.mkdir()
        source = self.launch.attempt / 'last-message.txt'
        target = self.state / 'response.txt'
        target.write_text('original response', encoding='utf-8')
        try:
            source.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'symlink creation unavailable: {type(error).__name__}')
        with self.assertRaisesRegex(ValueError, '경로가 변경되었습니다'):
            process_receipt.result_message(str(self.path), hashlib.sha256(b'original response').hexdigest())

        source.unlink()
        self.launch.attempt.rmdir()
        substituted = self.state / 'substituted-attempt'
        substituted.mkdir()
        (substituted / 'last-message.txt').write_text('original response', encoding='utf-8')
        try:
            self.launch.attempt.symlink_to(substituted, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'directory symlink creation unavailable: {type(error).__name__}')
        with self.assertRaisesRegex(ValueError, '경로가 변경되었습니다'):
            process_receipt.result_message(str(self.path), hashlib.sha256(b'original response').hexdigest())

    def recover(self, *, live=False, boot='boot'):
        with mock.patch.object(process_receipt.sys, 'platform', 'linux'), \
             mock.patch.object(process_receipt, '_linux_host', return_value='host'), \
             mock.patch.object(process_receipt, '_boot', return_value=boot), \
             mock.patch.object(process_receipt, '_confirm_posix_group_stopped',
                               side_effect=RuntimeError('live') if live else None):
            return self.repository.reconcile_interrupted(state_dir=self.state)

    def test_missing_final_result_is_recovered_without_fabricating_result_code(self):
        self.started()
        self.assertEqual(self.recover(), 1)
        first = dict(self.row())
        self.assertEqual((first['result_kind'], first['result_code'], first['collection_status']),
                         ('interrupted', None, 'unknown'))
        self.assertTrue(first['execution_finished_at'])
        self.assertIsNone(first['execution_duration_ms'])
        self.assertIn('process_group_absent_on_recovery', first['execution_finish_evidence'])
        self.assertEqual(self.recover(), 0)
        self.assertEqual(dict(self.row()), first)

    def test_process_receipt_updates_preserve_the_invocation_observation(self):
        facts = dict(started_at='2026-10-24T12:00:00+00:00', session_id='session')
        process_receipt.observe(self.path, self.identity, facts)
        process_receipt.started(self.launch, SimpleNamespace(pid=1234))
        self.assertEqual(process_receipt.observation(str(self.path), self.state, self.identity)['session_id'], 'session')
        process_receipt.finished(self.launch, SimpleNamespace(pid=1234))
        value = process_receipt.observation(str(self.path), self.state, self.identity)
        self.assertEqual(value, dict(version=1, identity=self.identity, **facts))
        self.assertTrue(process_receipt.recover(str(self.path), self.state)['finished_at'])

    def test_malformed_or_foreign_invocation_observation_preserves_pending_state_and_reservation(self):
        from loop_manager_internal.storage import task_calls
        task_calls.ensure_schema(self.connection)
        owner = task_calls.reserve(self.connection, self.state, 'repo', 'G-1', 'agent', 'run')
        with self.connection:
            self.connection.execute('UPDATE task_call_reservations SET invocation_id=?, receipt_path=?',
                                    (self.identity, str(self.path)))
        for mode in ('foreign', 'malformed', 'malformed_request_session'):
            with self.subTest(mode=mode):
                self.path.unlink(missing_ok=True)
                identity = 'other-invocation' if mode == 'foreign' else self.identity
                facts = ({} if mode == 'foreign' else {'request_session_id': 3}
                         if mode == 'malformed_request_session' else {'outcome': {'kind': 'success'}})
                process_receipt.observe(self.path, identity, facts)
                process_receipt.finished(self.launch, SimpleNamespace(pid=1234))
                self.assertEqual(self.repository.reconcile_interrupted(state_dir=self.state), 0)
                self.assertIsNone(self.row()['finished_at'])
                self.assertFalse(task_calls.confirmed(owner))
                self.assertEqual(len(task_calls.recover(self.connection, self.state)), 1)

    def test_prelaunch_rejection_is_recovered_as_non_execution_without_a_result(self):
        with self.connection:
            self.connection.execute("UPDATE agent_invocations SET execution_status='prepared', execution_started_at=NULL")
        process_receipt.not_started(self.path)
        self.repository.reconcile_interrupted(state_dir=self.state)
        first = dict(self.row())
        self.assertEqual((first['execution_status'], first['result_code']), ('not_executed', None))
        self.assertEqual(json.loads(first['execution_finish_evidence'])['evidence'], 'not_started')
        self.repository.reconcile_interrupted(state_dir=self.state)
        self.assertEqual(dict(self.row()), first)

    def test_non_execution_proof_cannot_replace_a_process_start_or_downgrade_the_ledger(self):
        from loop_manager_internal.storage import task_calls
        self.started()
        original = self.path.read_bytes()
        process_receipt.not_started(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.unlink()
        process_receipt.not_started(self.path)
        self.repository.reconcile_interrupted(state_dir=self.state)
        self.assertEqual(self.row()['execution_status'], 'executed')
        self.assertIsNone(self.row()['execution_finished_at'])
        task_calls.ensure_schema(self.connection)
        owner = task_calls.reserve(self.connection, self.state, 'repo', 'G-1', 'agent', 'run')
        with self.connection:
            self.connection.execute('UPDATE task_call_reservations SET invocation_id=?, receipt_path=?',
                                    (self.identity, str(self.path)))
        self.assertFalse(task_calls.confirmed(owner))
        self.assertEqual(len(task_calls.recover(self.connection, self.state)), 1)

    def test_live_group_and_missing_or_malformed_proof_stay_unknown(self):
        self.recover()
        self.assertIsNone(self.row()['execution_finished_at'])
        self.started()
        self.recover(live=True)
        self.assertIsNone(self.row()['execution_finished_at'])
        for malformed in ('{', '[]'):
            self.path.write_text(malformed, encoding='utf-8')
            self.recover()
            self.assertIsNone(self.row()['execution_finished_at'])

    def test_exception_after_process_finish_is_not_changed_to_success(self):
        self.repository.mark_execution_finished(self.identity, 10)
        self.repository.finish(self.identity, 'exception', None, 20, None, Usage())
        self.recover()
        row = self.row()
        self.assertEqual((row['result_kind'], row['result_code'], row['execution_duration_ms']),
                         ('exception', None, 10))
        self.assertTrue(row['execution_finished_at'])

    def test_finish_receipt_recovers_when_database_finish_observation_was_lost(self):
        self.started()
        with mock.patch.object(process_receipt.sys, 'platform', 'linux'), \
             mock.patch.object(process_receipt, '_linux_host', return_value='host'), \
             mock.patch.object(process_receipt, '_boot', return_value='boot'):
            process_receipt.finished(self.launch, SimpleNamespace(pid=1234))
        self.repository.finish(self.identity, 'exception', None, 20, None, Usage())
        self.recover(live=True)
        row = self.row()
        self.assertEqual((row['result_kind'], row['result_code']), ('exception', None))
        self.assertTrue(row['execution_finished_at'])

    def test_reboot_of_same_host_proves_old_group_stopped(self):
        self.started()
        self.recover(live=True, boot='new-boot')
        self.assertIn('host_rebooted', self.row()['execution_finish_evidence'])

    def test_durable_recovery_receipt_survives_database_write_loss_and_keeps_outer_transaction(self):
        self.started()
        self.recover()
        first = self.row()['execution_finished_at']
        self.connection.execute('UPDATE agent_invocations SET execution_finished_at=NULL')
        self.assertTrue(self.connection.in_transaction)
        self.recover(live=True)
        self.assertEqual(self.row()['execution_finished_at'], first)
        self.assertTrue(self.connection.in_transaction)

    def test_foreign_host_or_writable_attempt_path_cannot_supply_proof(self):
        self.started()
        value = json.loads(self.path.read_text())
        value['host'] = 'other-host'
        self.path.write_text(json.dumps(value))
        self.recover()
        self.assertIsNone(self.row()['execution_finished_at'])
        self.launch.attempt.mkdir()
        forged = self.launch.attempt / 'forged.process.json'
        forged.write_text(json.dumps({**value, 'finished_at': 'forged', 'evidence': 'process_group_stopped'}))
        self.assertIsNone(process_receipt.recover(str(forged), self.state))

    @skipUnless(sys.platform.startswith('linux'), 'Linux 프로세스 그룹 종료 관측')
    def test_kernel_group_observation_preserves_live_writer_then_recovers_stopped_writer(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   start_new_session=True)
        def cleanup():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        process_receipt.started(self.launch, process)
        self.repository.finish(self.identity, 'exception', None, 1, None, Usage())
        self.repository.reconcile_execution(state_dir=self.state)
        self.assertIsNone(self.row()['execution_finished_at'])
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        self.repository.reconcile_execution(state_dir=self.state)
        self.assertTrue(self.row()['execution_finished_at'])
        self.assertEqual((self.row()['result_kind'], self.row()['result_code']), ('exception', None))
