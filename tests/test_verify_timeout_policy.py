"""Focused state transition test for a verified insufficient timeout."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from tests.support.database import runtime_connection


class VerifyTimeoutPolicyTests(unittest.TestCase):
    def test_worker_progress_evidence_blocks_only_its_plan(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        with mock.patch.object(
            loop_manager, '_worktree_execplan_input_fingerprint', return_value='input'
        ), mock.patch.object(loop_manager, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'snapshot'
            detail = loop_manager.block_verify_timeout_limit(
                connection, 'repo', Path('/state/G-047'),
                'saved Worker result', '9 of 10 tests completed',
                'whole verify timeout\n전체 verify 시간 초과: verify_timeout_seconds=2',
            )
        plan = connection.execute(
            "SELECT status,blocked_reason,blocked_fingerprint,worker_result "
            "FROM exec_plans WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone()
        self.assertEqual(plan['status'], 'PLAN_BLOCKED')
        self.assertEqual(
            plan['blocked_reason'],
            BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value,
        )
        self.assertEqual(plan['blocked_fingerprint'], '2')
        self.assertEqual(plan['worker_result'], 'saved Worker result')
        block = connection.execute(
            "SELECT release_fingerprint FROM execplan_blocks "
            "WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone()
        self.assertEqual(block['release_fingerprint'], '2')
        self.assertIn('9 of 10 tests completed', detail)

    def test_repeated_unknown_timeout_uses_verify_retry_limit(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        decisions = []
        with mock.patch.object(
            loop_manager, '_blocked_validation_input_fingerprints',
            return_value=('input', 'worktree'),
        ), mock.patch.object(loop_manager, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'snapshot'
            for progress in ('one', 'two', 'three', 'four'):
                decisions.append(loop_manager.transition_validation_failure(
                    connection, 'repo', Path('/state/G-047'), 'verify',
                    loop_manager.VerifyOutput(
                        f'{progress}\n전체 verify 시간 초과:', timed_out=True,
                    ),
                ))
        self.assertEqual([item.blocked for item in decisions], [False, False, False, True])
        self.assertEqual(len({item.fingerprint for item in decisions}), 1)

    def test_validation_transition_redacts_persisted_failure_state(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        secret = 'verify-transition-secret'
        loop_manager.transition_validation_failure(
            connection, 'repo', Path('/state/G-047'), 'verify',
            f'FAILED tests/test_config.py::test_loads\npassword: {secret}',
        )

        rows = connection.execute(
            "SELECT last_error,verify_result,verify_failure_history FROM exec_plans "
            "WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone()
        project = connection.execute(
            "SELECT last_error,verify_result FROM projects WHERE repo='repo'"
        ).fetchone()
        for value in (*rows, *project):
            self.assertNotIn(secret, str(value))

    def test_verify_fingerprint_keeps_previous_raw_identity(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        raw = 'FAILED tests/test_config.py::test_loads\npassword: same-secret'
        expected, _ = loop_manager.validation_failure_fingerprint(
            'verify', raw, Path('/state/G-047'),
        )
        first = loop_manager.transition_validation_failure(
            connection, 'repo', Path('/state/G-047'), 'verify', raw,
        )
        output = loop_manager.VerifyOutput(raw)
        decision = loop_manager.transition_validation_failure(
            connection, 'repo', Path('/state/G-047'), 'verify', output,
        )
        self.assertEqual(decision.fingerprint, expected)
        self.assertEqual(first.fingerprint, expected)
        self.assertEqual(decision.occurrences, 2)
        row = connection.execute(
            "SELECT verify_failure_history,verify_result FROM exec_plans "
            "WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone()
        self.assertNotIn('same-secret', str(tuple(row)))

    def test_higher_config_limit_resumes_same_workspace_at_verify(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository.block_plan(
            'repo', 'G-047',
            reason_code=BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value,
            evidence='9 of 10 tests completed', release_fingerprint='2',
            plan_values={
                'blocked_fingerprint': '2', 'worker_result': 'saved Worker result',
                'verify_result': 'whole verify timeout',
            },
            clear_projection=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.ini'
            config.write_text('[loop]\nverify_timeout_seconds = 2\n', encoding='utf-8')
            with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'), mock.patch.object(
                loop_manager, 'validate_blocked_agent_scope'
            ):
                self.assertFalse(loop_manager.resume_plan_blocks(
                    connection, Path('repo'), Path('/state'), 'repo', 'main', config,
                ))
                config.write_text('[loop]\nverify_timeout_seconds = 4\n', encoding='utf-8')
                self.assertTrue(loop_manager.resume_plan_blocks(
                    connection, Path('repo'), Path('/state'), 'repo', 'main', config,
                ))
        plan = connection.execute(
            "SELECT status,worktree,worker_result,verify_result FROM exec_plans "
            "WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone()
        self.assertEqual(plan['status'], 'VERIFYING')
        self.assertEqual(plan['worktree'], '/state/G-047')
        self.assertEqual(plan['worker_result'], 'saved Worker result')
        self.assertEqual(plan['verify_result'], 'whole verify timeout')

    def test_manual_recheck_runs_timeout_verify_without_clearing_failure_history(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        history = '{"old-failure":{"occurrences":3,"sample":"timeout"}}'
        repository.update_plan(
            'repo', 'G-047', plan_values={
                'retry_count': 3, 'verify_failure_history': history,
                'effective_worker_level': 4, 'review_level_history': '{"4":2}',
            },
        )
        repository.block_plan(
            'repo', 'G-047',
            reason_code=BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value,
            evidence='9 of 10 tests completed', release_fingerprint='2',
            plan_values={
                'blocked_fingerprint': '2', 'worker_result': 'saved Worker result',
                'verify_result': 'whole verify timeout',
            },
            clear_projection=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.ini'
            config.write_text('[loop]\nverify_timeout_seconds = 2\n', encoding='utf-8')
            with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'), mock.patch.object(
                loop_manager, 'validate_blocked_agent_scope'
            ):
                self.assertTrue(loop_manager.resume_plan_blocks(
                    connection, Path('repo'), Path('/state'), 'repo', 'main', config,
                    plan_id='G-047', force_timeout_recheck=True,
                ))
        plan = connection.execute(
            "SELECT status,retry_count,verify_failure_history,effective_worker_level,review_level_history "
            "FROM exec_plans WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone()
        self.assertEqual(plan['status'], 'VERIFYING')
        self.assertEqual(plan['retry_count'], 3)
        self.assertEqual(plan['verify_failure_history'], history)
        self.assertEqual(plan['effective_worker_level'], 4)
        self.assertEqual(plan['review_level_history'], '{"4":2}')
