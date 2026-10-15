"""Targeted test 실행 원본, 연속성, 중복과 Worker 최종 차단 경계."""
import io
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
            with sqlite3.connect(path) as first:
                ensure_schema(first)
                TargetedTestRepository(first).observe('repo', 'G-1', [event('a'), event('b')])
            with sqlite3.connect(path) as second:
                repository = TargetedTestRepository(second)
                repository.observe('repo', 'G-1', [event('b'), event('c')])
                self.assertTrue(repository.repeated('repo', 'G-1'))


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
