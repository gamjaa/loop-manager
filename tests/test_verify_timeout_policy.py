"""Focused state transition test for a verified insufficient timeout."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import loop_manager
import runtime_state
from scoped_blocking import BlockReasonCode
from tests import test_runtime_state


class VerifyTimeoutPolicyTests(unittest.TestCase):
    def test_worker_progress_evidence_blocks_only_its_plan(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
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
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
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

    def test_higher_config_limit_resumes_same_workspace_at_verify(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
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
