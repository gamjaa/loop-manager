"""Generation and runtime correction CAS tests."""

import sqlite3
import unittest

from execution_checkpoint_state import (
    CheckpointRace, ExecutionCheckpointRepository,
)
import runtime_state
from tests import test_runtime_state


class CheckpointRepositoryTests(unittest.TestCase):
    def test_completed_rebase_requires_later_generation_and_cleared_operation(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        proof = {'generation': 1, 'journal_id': 'sealed'}
        intent = repository.record(
            'repo', 'G-102', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'completed_rebase': proof},
        )
        self.assertIsNone(intent.completed_rebase())
        completed = repository.record(
            'repo', 'G-102', expected_generation=1, operation=None,
            expected_head='after', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'completed_rebase': proof},
        )
        self.assertEqual(completed.completed_rebase(), proof)
        self.assertIsNone(repository.record(
            'repo', 'G-102', expected_generation=2, operation=None,
            expected_head='after', target_base_oid='base',
            observed_remote_oid='remote',
            snapshot={'completed_rebase': {'generation': 3}},
        ).completed_rebase())

    def test_intent_and_completion_reject_stale_generation(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        intent = repository.record(
            'repo', 'G-044', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={'staged_paths': ['a.py']},
        )
        self.assertEqual(intent.generation, 1)
        self.assertEqual(repository.get('repo', 'G-044').snapshot['staged_paths'], ['a.py'])
        with self.assertRaises(CheckpointRace):
            repository.record(
                'repo', 'G-044', expected_generation=0, operation=None,
                expected_head='after', target_base_oid='base',
                observed_remote_oid=None, snapshot=None,
            )
        completed = repository.record(
            'repo', 'G-044', expected_generation=1, operation=None,
            expected_head='after', target_base_oid='base',
            observed_remote_oid=None, snapshot={'after_head': 'after'},
        )
        self.assertEqual(completed.generation, 2)
        self.assertIsNone(completed.operation)

    def test_runtime_correction_preserves_sessions_and_rejects_stale_row(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate('repo', 'G-047', activation_sha='old-base',
                            branch='loop/G-047', worktree='/owned')
        expected = repository.projected('repo')
        assert expected is not None
        repository.transition_from_snapshot(
            expected, plan_values={
                'status': 'PUBLISHING', 'worker_session_id': 'worker',
                'verify_result': 'PASS', 'review_result': 'PASS',
            }, project_values={
                'phase': 'PUBLISHING', 'worker_session_id': 'worker',
                'verify_result': 'PASS', 'review_result': 'PASS',
            },
        )
        current = repository.projected('repo')
        assert current is not None
        corrected = repository.correct_checkpoint(
            current, phase='VERIFYING', active_sha='new-base',
            validated_base_sha=None, invalidate_validation=True,
        )
        self.assertEqual(corrected.status, 'VERIFYING')
        self.assertEqual(corrected.worker_session_id, 'worker')
        self.assertIsNone(corrected.verify_result)
        self.assertIsNone(corrected.review_result)
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.correct_checkpoint(
                current, phase='VERIFYING', active_sha='older',
                validated_base_sha=None, invalidate_validation=True,
            )

    def test_catalog_generation_change_rejects_old_checkpoint_correction(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate('repo', 'G-047', activation_sha='base',
                            branch='loop/G-047', worktree='/owned')
        expected = repository.projected('repo')
        assert expected is not None
        connection.execute(
            "UPDATE exec_plans SET catalog_generation = catalog_generation + 1 "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.correct_checkpoint(
                expected, phase='VERIFYING', active_sha='base',
                validated_base_sha=None, invalidate_validation=True,
            )
        refreshed = repository.projected('repo')
        assert refreshed is not None
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.correct_checkpoint(
                refreshed, phase='VERIFYING', active_sha='base',
                validated_base_sha=None, invalidate_validation=True,
                expected_catalog=(expected.catalog_generation,
                                  expected.current_revision_sha),
            )
