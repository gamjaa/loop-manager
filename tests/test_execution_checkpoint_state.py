"""Generation and runtime correction CAS tests."""

import sqlite3
import unittest

from execution_checkpoint_state import (
    CheckpointRace, ExecutionCheckpointRepository,
)
import runtime_state
from tests import test_runtime_state
from tests.test_managed_rebase_validation import decision_for, expectation, observation


class CheckpointRepositoryTests(unittest.TestCase):
    def test_current_acceptance_is_generation_bound_and_preserves_legacy_audit(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        history = {'rebase_completion': {'id': 'old-journal'},
                   'completed_rebase': {'generation': 0, 'journal_id': 'legacy'},
                   'stage_evidence': {'tree': 'old-tree'}, 'failure': 'old-failure'}
        repository.record(
            '/repo', 'G-108', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid='remote', snapshot=history,
        )
        result = decision_for().acceptance
        saved = repository.accept_current_rebase('/repo', 'G-108', acceptance=result)
        self.assertEqual(saved.current_rebase_acceptance(), result)
        for key, value in history.items():
            self.assertEqual(saved.snapshot[key], value)
        self.assertEqual(saved.completed_rebase(), history['completed_rebase'])
        self.assertEqual(repository.accept_current_rebase('/repo', 'G-108',
                                                        acceptance=result), saved)
        with self.assertRaises(CheckpointRace):
            repository.accept_current_rebase('/repo', 'G-108', acceptance={**result, 'head': 'forged'})
        repository.record(
            '/repo', 'G-108', expected_generation=saved.generation, operation=None,
            expected_head='other', target_base_oid='base',
            observed_remote_oid='remote', snapshot=saved.snapshot,
        )
        self.assertIsNone(repository.get('/repo', 'G-108').current_rebase_acceptance())
        with self.assertRaises(CheckpointRace):
            repository.accept_current_rebase('/repo', 'G-108', acceptance=result)

    def test_current_acceptance_supports_no_history_and_retains_prior_acceptances(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        first = decision_for(expectation(generation=0)).acceptance
        saved = repository.accept_current_rebase('/repo', 'G-108', acceptance=first)
        self.assertEqual(saved.generation, 1)
        self.assertIsNone(saved.completed_rebase())
        expected = expectation(generation=saved.generation)
        second = decision_for(expected, observation(expected, repository_state_fingerprint='new')).acceptance
        final = repository.accept_current_rebase('/repo', 'G-108', acceptance=second)
        self.assertEqual(final.snapshot['current_rebase_acceptance_history'], [first])
        self.assertEqual(final.current_rebase_acceptance(), second)

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

    def test_rebase_completion_rolls_back_checkpoint_when_projection_fails(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.activate('repo', 'G-047', activation_sha='old-base',
                         branch='loop/G-047', worktree='/owned')
        checkpoint = ExecutionCheckpointRepository(connection)
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='old-head', target_base_oid='new-base',
            observed_remote_oid=None, snapshot={'rebase_run_id': 'run-1'},
        )
        before = checkpoint.get('repo', 'G-047')
        original = runtime.projected('repo')
        def fail(stage):
            if stage == 'after_execplan_update':
                raise RuntimeError('projection interrupted')
        failing = runtime_state.RuntimeStateRepository(connection, fault_hook=fail)
        with self.assertRaisesRegex(RuntimeError, 'projection interrupted'):
            failing.complete_rebase(
                original, checkpoint, completion={
                    'id': 'result-1', 'run_id': 'run-1',
                    'intent_generation': 1, 'before_head': 'old-head',
                    'target_base_oid': 'new-base', 'remote_oid': None,
                    'head': 'new-head',
                },
                project_values={'phase': 'VERIFYING', 'active_sha': 'new-base'},
            )
        self.assertEqual(checkpoint.get('repo', 'G-047'), before)
        self.assertEqual(runtime.projected('repo'), original)

    def test_rebase_completion_updates_checkpoint_and_projection_once(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.activate('repo', 'G-047', activation_sha='old-base',
                         branch='loop/G-047', worktree='/owned')
        checkpoint = ExecutionCheckpointRepository(connection)
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='old-head', target_base_oid='new-base',
            observed_remote_oid='remote', snapshot={
                'rebase_run_id': 'run-1', 'pending': 'continue',
                'worker_result': 'approved', 'stage_evidence': {'tree': 'tree'},
                'stash_oid': 'saved-stash',
            },
        )
        completion = {
            'id': 'result-1', 'run_id': 'run-1', 'intent_generation': 1,
            'before_head': 'old-head', 'target_base_oid': 'new-base',
            'remote_oid': 'remote', 'head': 'new-head',
        }
        values = {
            'phase': 'VERIFYING', 'active_sha': 'new-base',
            'validated_base_sha': None, 'verify_result': None,
            'review_result': None,
        }
        published_proof = {
            'generation': 1, 'journal_id': 'result-1',
            'before_head': 'old-head', 'after_head': 'new-head',
            'remote_oid': 'remote',
        }
        original = runtime.projected('repo')
        completed = runtime.complete_rebase(
            original, checkpoint, completion=completion, project_values=values,
            published_proof=published_proof,
        )
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['rebase_completion'], completion)
        self.assertEqual(saved.completed_rebase(), published_proof)
        self.assertNotIn('pending', saved.snapshot)
        self.assertNotIn('stash_oid', saved.snapshot)
        self.assertEqual(completed.status, 'VERIFYING')
        self.assertEqual(connection.execute(
            "SELECT active_sha FROM projects WHERE repo = 'repo'"
        ).fetchone()[0], 'new-base')
        self.assertEqual(runtime.complete_rebase(
            completed, checkpoint, completion=completion, project_values=values,
            published_proof=published_proof,
        ), completed)
        self.assertEqual(checkpoint.get('repo', 'G-047').generation, saved.generation)
        with self.assertRaises(runtime_state.RuntimeStateError):
            runtime.complete_rebase(
                completed, checkpoint, completion=completion, project_values=values,
                published_proof={**published_proof, 'journal_id': 'other'},
            )
        with self.assertRaises(runtime_state.RuntimeStateError):
            runtime.complete_rebase(
                completed, checkpoint, completion={**completion, 'id': 'other'},
                project_values=values,
            )

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
