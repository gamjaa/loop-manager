"""Targeted test 실행 원본, 연속성, 중복과 Worker 최종 차단 경계."""
import io
from dataclasses import replace
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest import TestCase, mock

from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from loop_manager_internal.execution.result_validation import TargetedTestFailure
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason
from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository, ensure_schema
from loop_manager_internal.storage.agent_invocation_state import InvocationRepository, ensure_schema as invocation_schema
from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile
from loop_manager_internal.verification import targeted
from tests.support.execution import ExecutionFixture


def event(run, identity='tests.Sample.test_one', result='failure'):
    return {'run_id': run, 'id': identity, 'result': result}


class TargetedHistoryTests(TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        ensure_schema(self.connection)
        self.repository = TargetedTestRepository(self.connection)

    def test_only_three_distinct_runs_of_the_same_test_trigger_the_limit(self):
        for number in range(1, 4):
            self.repository.observe('repo', 'G-1', [event(str(number))])
            self.repository.observe('repo', 'G-1', [event(str(number))])
            self.assertEqual(bool(self.repository.repeated('repo', 'G-1')), number == 3)
        self.assertEqual(self.repository.sequence('repo', 'G-1'), 3)
        self.assertFalse(self.repository.repeated('repo', 'G-2'))

    def test_other_failures_are_separate_and_success_or_skip_breaks_the_streak(self):
        for reset in ('success', 'skip', 'expected_failure', 'unexpected_success'):
            with self.subTest(reset=reset):
                self.repository.observe('repo', reset, [event('a'), event('b', result=reset),
                    event('c'), event('d'), event('other', 'tests.Sample.test_other')])
                self.assertFalse(self.repository.repeated('repo', reset))
                self.repository.observe('repo', reset, [event('e', result='error')])
                self.assertEqual(self.repository.repeated('repo', reset), ('tests.Sample.test_one',))

    def test_reaching_three_is_preserved_until_block_even_if_the_turn_later_passes(self):
        self.repository.observe('repo', 'G-1', [event('a'), event('b'), event('c'), event('d', result='success')])
        self.assertTrue(self.repository.repeated('repo', 'G-1'))
        with self.connection:
            self.repository.acknowledge('repo', 'G-1')
        self.assertFalse(self.repository.repeated('repo', 'G-1'))
        self.repository.observe('repo', 'G-1', [event('e')])
        self.assertFalse(self.repository.repeated('repo', 'G-1'))

    def test_conflicting_duplicate_rolls_back_the_whole_observation(self):
        self.repository.observe('repo', 'G-1', [event('a')])
        with self.assertRaises(ValueError):
            self.repository.observe('repo', 'G-1', [event('b'), event('a', result='success')])
        self.assertEqual(self.repository.sequence('repo', 'G-1'), 1)

    def test_database_restart_preserves_individual_test_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            with closing(sqlite3.connect(path)) as first, first:
                ensure_schema(first)
                TargetedTestRepository(first).observe('repo', 'G-1', [event('a'), event('b')])
            with closing(sqlite3.connect(path)) as second, second:
                repository = TargetedTestRepository(second)
                repository.observe('repo', 'G-1', [event('b'), event('c')])
                self.assertTrue(repository.repeated('repo', 'G-1'))


class TargetedRecoveryTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'state.db'
        self.open()
        self.addCleanup(lambda: self.connection.close())
        self.proof = mock.patch('loop_manager_internal.agents.process_receipt.recover',
                               return_value={'finished_at': 'proof'})
        self.recover = self.proof.start()
        self.addCleanup(self.proof.stop)

    def open(self):
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        invocation_schema(self.connection)
        self.calls = InvocationRepository(self.connection)
        self.tests = TargetedTestRepository(self.connection)

    def bind(self, name='one', *, repo='repo', plan='G-1'):
        identity = self.calls.start(InvocationContext(repo, plan), 'worker', 'codex',
                                    ResolvedProfile('old-runtime-model', 'high'), None)
        directory = self.root / 'agent-runtime' / 'old-runtime' / name
        directory.mkdir(parents=True)
        receipt = directory.parent / (name + '.process.json')
        self.tests.bind(identity, directory, receipt, state_dir=self.root)
        self.calls.bind_process_receipt(identity, str(receipt))
        return identity, directory

    def write(self, directory, runs=('a',), *, suffix=''):
        for run in runs:
            record = dict(event(run), kind='test_end', version=1, utc='2026-10-15T00:00:00')
            (directory / f'targeted-{run}.jsonl').write_text(json.dumps(record) + '\n' + suffix, encoding='utf-8')

    def replay(self):
        self.calls.reconcile_interrupted(state_dir=self.root)

    def test_restart_without_finally_replays_once_and_does_not_restore_consumed_reach(self):
        identity, directory = self.bind()
        self.write(directory, ('a', 'b', 'c'), suffix='{"kind":')
        self.connection.close()
        self.open()
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 3)
        self.assertTrue(self.tests.repeated('repo', 'G-1'))
        self.assertEqual(self.connection.execute('SELECT result_kind FROM agent_invocations').fetchone()[0], 'interrupted')
        self.tests.acknowledge('repo', 'G-1')
        self.connection.commit()
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 3)
        self.assertFalse(self.tests.repeated('repo', 'G-1'))
        self.assertTrue(directory.is_dir())

    def test_live_missing_receipt_and_manual_writer_keep_unconsumed_files(self):
        _, directory = self.bind()
        self.write(directory)
        self.recover.return_value = None
        self.replay()
        self.assertTrue(self.tests.pending('repo', 'G-1'))
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 0)
        self.recover.return_value = {'finished_at': 'proof'}
        with mock.patch('loop_manager_internal.storage.work_claims.live_manual_tasks', return_value={None}):
            self.replay()
        self.assertTrue(self.tests.pending('repo', 'G-1'))
        self.assertTrue((directory / 'targeted-a.jsonl').is_file())
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 1)

    def test_database_failure_rolls_back_results_and_consumption_then_retries(self):
        _, directory = self.bind()
        self.write(directory, ('a', 'b'))
        self.connection.execute('''CREATE TRIGGER fail_consumption BEFORE UPDATE OF status
            ON targeted_test_collections WHEN NEW.status='collected'
            BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END''')
        self.connection.commit()
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 0)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM targeted_test_run_owners').fetchone()[0], 0)
        self.assertTrue(self.tests.pending('repo', 'G-1'))
        self.connection.execute('DROP TRIGGER fail_consumption')
        self.connection.commit()
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 2)

    def test_interruption_after_collection_before_storage_leaves_retry_binding(self):
        _, directory = self.bind()
        self.write(directory)
        with mock.patch.object(self.tests, 'observe', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tests.reconcile(state_dir=self.root)
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 0)
        self.assertTrue(self.tests.pending('repo', 'G-1'))
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 1)

    def test_malformed_earlier_call_waits_without_blocking_other_plan_or_repo(self):
        _, first = self.bind('one')
        self.write(first, suffix='{}\n')
        _, second = self.bind('two')
        self.write(second, ('b',))
        _, other = self.bind('other', plan='G-2')
        self.write(other, ('c',))
        _, other_repo = self.bind('other-repo', repo='other')
        self.write(other_repo, ('d',))
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 0)
        self.assertFalse(self.tests.pending('repo', 'G-2'))
        self.assertFalse(self.tests.pending('other', 'G-1'))
        self.write(first)
        self.replay()
        self.assertEqual(len(self.connection.execute('SELECT * FROM targeted_test_results WHERE repo=? AND plan_id=?', ('repo', 'G-1')).fetchall()), 2)

    def test_copied_run_from_another_invocation_is_not_reattributed(self):
        _, first = self.bind('one')
        self.write(first)
        self.replay()
        _, second = self.bind('two', repo='other', plan='G-2')
        self.write(second)
        self.replay()
        self.assertTrue(self.tests.pending('other', 'G-2'))
        self.assertEqual(self.tests.sequence('other', 'G-2'), 0)

    def test_replaced_directory_and_file_symlink_are_preserved_for_diagnosis(self):
        _, directory = self.bind()
        self.write(directory)
        saved = directory.with_name('saved')
        directory.rename(saved)
        directory.mkdir()
        self.write(directory)
        self.replay()
        self.assertTrue(self.tests.pending('repo', 'G-1'))
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 0)
        (directory / 'targeted-a.jsonl').unlink()
        directory.rmdir()
        saved.rename(directory)
        path = directory / 'targeted-a.jsonl'
        path.rename(directory / 'original')
        try:
            path.symlink_to(directory / 'original')
        except OSError:
            self.skipTest('이 플랫폼에서 symlink 생성 권한이 없습니다.')
        self.replay()
        self.assertTrue(self.tests.pending('repo', 'G-1'))
        path.unlink()
        (directory / 'original').rename(path)
        self.replay()
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 1)

    def test_missing_directory_and_read_error_do_not_become_empty_success(self):
        _, directory = self.bind()
        directory.rmdir()
        self.replay()
        self.assertTrue(self.tests.pending('repo', 'G-1'))
        self.assertEqual(self.tests.sequence('repo', 'G-1'), 0)
        # A separate call with a valid binding isolates a transient read failure.
        _, other = self.bind('other', plan='G-2')
        self.write(other)
        with mock.patch.object(targeted.os, 'open', side_effect=PermissionError):
            self.replay()
        self.assertTrue(self.tests.pending('repo', 'G-2'))
        self.replay()
        self.assertFalse(self.tests.pending('repo', 'G-2'))

    def test_directory_scan_error_preserves_unconsumed_results_until_retry(self):
        for error_type in (PermissionError, OSError):
            with self.subTest(error=error_type.__name__):
                plan = error_type.__name__
                identity, directory = self.bind(plan, plan=plan)
                self.write(directory, (plan + '-a', plan + '-b', plan + '-c'))
                originals = {path: path.read_bytes() for path in directory.iterdir()}
                with mock.patch.object(targeted.os, 'scandir', side_effect=error_type('scan unavailable')):
                    with self.assertRaises(error_type):
                        targeted.collect(directory)
                    self.replay()
                row = self.connection.execute('''SELECT status, error FROM targeted_test_collections
                    WHERE invocation_id=?''', (identity,)).fetchone()
                self.assertEqual(tuple(row), ('error', error_type.__name__))
                self.assertTrue(self.tests.pending('repo', plan))
                self.assertEqual(self.tests.sequence('repo', plan), 0)
                self.assertFalse(self.tests.repeated('repo', plan))
                self.assertEqual({path: path.read_bytes() for path in originals}, originals)
                self.connection.close()
                self.open()
                self.replay()
                self.assertFalse(self.tests.pending('repo', plan))
                self.assertEqual(len(self.connection.execute('''SELECT * FROM targeted_test_results
                    WHERE repo='repo' AND plan_id=?''', (plan,)).fetchall()), 3)
                self.assertEqual(self.tests.repeated('repo', plan), ('tests.Sample.test_one',))
                sequence = self.tests.sequence('repo', plan)
                self.replay()
                self.assertEqual(self.tests.sequence('repo', plan), sequence)


class TargetedRunnerTests(TestCase):
    def test_unittest_records_native_results_for_each_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, records = root / 'project', root / 'records'
            project.mkdir()
            records.mkdir()
            (project / 'case_sample.py').write_text('''import unittest
class Sample(unittest.TestCase):
    def test_failure(self): self.fail('actual assertion')
    def test_success(self): pass
''', encoding='utf-8')
            environment = dict(os.environ, LOOP_MANAGER_TARGETED_TEST_DIRECTORY=str(records),
                               LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(project))
            for _ in range(3):
                result = subprocess.run([sys.executable, str(Path(targeted.__file__).resolve()),
                    '--unittest', 'case_sample.Sample'], cwd=project, env=environment,
                    capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 1, result.stderr)
            results = targeted.collect(records)
            self.assertEqual(len(results), 6)
            self.assertEqual(len({item['run_id'] for item in results}), 3)
            self.assertEqual([item['result'] for item in results].count('failure'), 3)
            self.assertEqual([item['result'] for item in results].count('success'), 3)

    def test_collection_failure_does_not_invent_test_failures(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {'LOOP_MANAGER_TARGETED_TEST_DIRECTORY': directory}), \
             mock.patch.object(targeted, 'project_imports'), \
             mock.patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(targeted.main(['--unittest', 'module_that_does_not_exist']), 2)
            self.assertEqual(targeted.collect(directory), ())

    def test_junit_uses_a_fresh_report_and_preserves_parameter_identity(self):
        def execute(arguments, **_):
            Path(arguments[-1]).write_text('''<testsuite><testcase classname="suite" name="test_x[a]"><failure/></testcase>
                <testcase classname="suite" name="test_x[b]"/></testsuite>''', encoding='utf-8')
            return mock.Mock(returncode=1)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {'LOOP_MANAGER_TARGETED_TEST_DIRECTORY': directory}), \
             mock.patch.object(targeted.subprocess, 'run', side_effect=execute):
            self.assertEqual(targeted.main(['--junit', '--', 'test-tool', '{junit}']), 1)
            self.assertEqual({(item['id'], item['result']) for item in targeted.collect(directory)},
                             {('suite.test_x[a]', 'failure'), ('suite.test_x[b]', 'success')})

    def test_completed_events_survive_an_incomplete_final_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'targeted-run.jsonl'
            record = dict(event('run'), kind='test_end', version=1, utc='2026-10-15T00:00:00')
            path.write_text(json.dumps(record) + '\n{"kind":', encoding='utf-8')
            self.assertEqual(len(targeted.collect(directory)), 1)
            path.write_text(json.dumps(record) + '\n' + json.dumps(record) + '\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                targeted.collect(directory)


class WorkerTargetedFailureTests(ExecutionFixture, TestCase):
    def test_unresolved_report_with_live_writer_preserves_scope_and_pending_evidence(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.handler.state_dir = Path(temporary.name)
        self.effects.validation.parse_worker_result.side_effect = TargetedTestFailure('unresolved')
        def worker(*args, **kwargs):
            self.bound_records(('a',), state_dir=self.handler.state_dir)
            return self.agent(*args, **kwargs)
        self.effects.agent.run_agent.side_effect = worker
        with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value=None):
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.effects.agent.clear_agent_scope.assert_not_called()
        self.assertTrue(TargetedTestRepository(self.connection).pending('repo', 'G-047'))

    def test_recovery_at_cli_retry_blocks_without_another_physical_worker(self):
        from loop_manager_internal.agents.agent_invocation import AgentResult, AgentResultKind
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.handler.state_dir = Path(temporary.name)
        def worker(*args, **kwargs):
            self.bound_records(state_dir=self.handler.state_dir)
            return AgentResult(1, 'connection reset', kind=AgentResultKind.TRANSIENT_FAILURE)
        self.effects.agent.run_agent.side_effect = worker
        with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason, RecoveryReason.TARGETED_TEST_EXHAUSTED)
        self.assertEqual(self.effects.agent.run_agent.call_count, 1)
        self.effects.validation.verify.assert_not_called()

    def test_successful_report_waits_for_collection_without_becoming_provider_error(self):
        self.effects.agent.run_agent.side_effect = lambda *a, **kw: replace(
            self.agent(*a, **kw), targeted_collection_pending=True)
        result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(result.recovery.observation.reason, RecoveryReason.TARGETED_TEST_CORRECTION)
        self.effects.validation.verify.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()

    def bound_records(self, runs=('a', 'b', 'c'), *, state_dir=None):
        if state_dir is None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.handler.state_dir = Path(temporary.name)
        directory = self.handler.state_dir / 'agent-runtime' / 'previous-runtime' / 'call-one'
        directory.mkdir(parents=True)
        calls = InvocationRepository(self.connection)
        identity = calls.start(InvocationContext('repo', 'G-047'), 'worker', 'codex',
                               ResolvedProfile('old-model', 'low'), None)
        receipt = directory.parent / (directory.name + '.process.json')
        TargetedTestRepository(self.connection).bind(identity, directory, receipt, state_dir=self.handler.state_dir)
        calls.bind_process_receipt(identity, str(receipt))
        for run in runs:
            record = dict(event(run), kind='test_end', version=1, utc='2026-10-15T00:00:00')
            (directory / f'targeted-{run}.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')

    def test_recovered_third_failure_blocks_before_worker_and_verify(self):
        self.bound_records()
        with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason, RecoveryReason.TARGETED_TEST_EXHAUSTED)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        repository = TargetedTestRepository(self.connection)
        self.assertEqual(repository.sequence('repo', 'G-047'), 3)
        self.assertFalse(repository.repeated('repo', 'G-047'))
        with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
            repository.reconcile(state_dir=self.handler.state_dir)
        self.assertFalse(repository.repeated('repo', 'G-047'))

    def test_unknown_writer_pauses_only_its_work_without_worker_or_report_correction(self):
        self.bound_records(('a',))
        with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value=None):
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(result.recovery.observation.reason, RecoveryReason.TARGETED_TEST_CORRECTION)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_block_failure_rolls_back_reach_consumption_and_retries_existing_evidence(self):
        self.bound_records()
        # Raise after the existing block/ack transaction's work, before commit.
        self.connection.execute('''CREATE TRIGGER fail_ack BEFORE UPDATE OF repeated
            ON targeted_test_streaks WHEN OLD.repeated=1 AND NEW.repeated=0
            BEGIN SELECT RAISE(ABORT, 'injected ack failure'); END''')
        self.connection.commit()
        with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
            with self.assertRaises(sqlite3.IntegrityError):
                self.handler.run(self.context())
        self.assertTrue(TargetedTestRepository(self.connection).repeated('repo', 'G-047'))
        self.connection.execute('DROP TRIGGER fail_ack')
        self.connection.commit()
        with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertFalse(TargetedTestRepository(self.connection).repeated('repo', 'G-047'))
        self.effects.agent.run_agent.assert_not_called()

    def test_same_test_across_changed_candidates_blocks_on_the_third_execution(self):
        repository = TargetedTestRepository(self.connection)
        self.effects.validation.parse_worker_result.side_effect = TargetedTestFailure('unresolved test')
        for number in range(3):
            self.effects.agent.capture_git_snapshot.return_value.dirty = {'app.py': f'candidate-{number}'}
            def worker(*args, **kwargs):
                repository.observe('repo', 'G-047', [event(str(number))])
                return self.agent(*args, **kwargs)
            self.effects.agent.run_agent.side_effect = worker
            outcome = self.handler.run(self.context())
            self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK if number == 2
                             else ExecutionOutcomeKind.RETRY)

    def test_three_failures_inside_one_worker_turn_block_before_verify(self):
        repository = TargetedTestRepository(self.connection)
        def worker(*args, **kwargs):
            repository.observe('repo', 'G-047', [event('a'), event('b'), event('c')])
            return self.agent(*args, **kwargs)
        self.effects.agent.run_agent.side_effect = worker
        result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIn('연속 3회', result.detail)
        self.assertEqual(result.recovery.observation.reason, RecoveryReason.TARGETED_TEST_EXHAUSTED)
        self.effects.validation.verify.assert_not_called()

    def test_distinct_failures_across_turns_do_not_use_the_completion_report_limit(self):
        repository = TargetedTestRepository(self.connection)
        self.effects.validation.parse_worker_result.side_effect = TargetedTestFailure('unresolved test')
        for number in range(4):
            def worker(*args, **kwargs):
                repository.observe('repo', 'G-047', [event(str(number), f'tests.Sample.test_{number}')])
                return self.agent(*args, **kwargs)
            self.effects.agent.run_agent.side_effect = worker
            outcome = self.handler.run(self.context())
            self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
            self.assertEqual(outcome.recovery.observation.reason, RecoveryReason.TARGETED_TEST_CORRECTION)

    def test_unproven_failure_pauses_without_final_block(self):
        self.effects.validation.parse_worker_result.side_effect = TargetedTestFailure('unproven')
        outcome = self.handler.run(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertIn('LOOP_MANAGER_TARGETED_TEST_RUNNER', self.state.row()['last_error'])
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_restart_after_recording_third_failure_blocks_without_another_worker(self):
        TargetedTestRepository(self.connection).observe('repo', 'G-047', [event('a'), event('b'), event('c')])
        outcome = self.handler.run(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.run_agent.assert_not_called()
