"""Generation and runtime correction CAS tests."""

import sqlite3
import unittest

from loop_manager_internal.storage.execution_checkpoint_state import (
    CheckpointRace, ExecutionCheckpointRepository,
)
from loop_manager_internal.storage import runtime_state
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


class RebasePublicationRepositoryTests(unittest.TestCase):
    def setUp(self):
        from loop_manager_internal.execution.execution_state import ExecutionStatePort
        self.connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(self.connection.close)
        self.runtime = runtime_state.RuntimeStateRepository(self.connection)
        self.runtime.activate('repo', 'G-047', activation_sha='base',
                              branch='loop/G-047', worktree='/owned')
        self.state = ExecutionStatePort(self.connection, self.runtime, 'repo')
        self.state.update_project('repo', publication_remote_oid='remote',
                                  last_error='preserved error', verify_result='unrelated audit')
        self.checkpoints = ExecutionCheckpointRepository(self.connection)
        self.checkpoints.record('repo', 'G-047', expected_generation=0,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'failure': 'preserved failure'})
        self.expected = expectation(repo='repo', plan_id='G-047',
            plan_path='docs/plans/[G-047] Runtime.md', branch='loop/G-047')
        from dataclasses import replace
        self.expected = replace(self.expected, remote=replace(self.expected.remote, branch='loop/G-047'))
        self.acceptance = decision_for(self.expected, observation(self.expected)).acceptance
        self.accepted = self.checkpoints.accept_current_rebase('repo', 'G-047', acceptance=self.acceptance)

    def begin(self):
        return self.checkpoints.begin_rebase_publication('repo', 'G-047',
            expected_generation=self.accepted.generation,
            acceptance=self.acceptance, remote_before='remote')

    def test_unprojected_blocked_publication_preserves_block_and_execution_slot(self):
        from loop_manager_internal.execution.execution_state import DirectRebasePublicationStatePort
        self.runtime.transition_and_clear('repo', 'G-047', plan_values={
            'status': 'PLAN_BLOCKED', 'blocked_reason': 'verify_retry_exhausted'}, enqueue=False)
        state = DirectRebasePublicationStatePort(self.connection, self.runtime, 'repo', 'G-047')
        before = state.expected
        project = dict(self.connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone())
        pending = self.begin()
        acceptance = self.after_push()
        def finish():
            state.complete_rebase_publication(self.checkpoints,
                intent=pending.snapshot['rebase_publication_intent'], acceptance=acceptance,
                reobserve=lambda: ('after', acceptance))
        finish()
        from dataclasses import replace
        self.assertEqual(self.runtime.get('repo', 'G-047'), replace(before, publication_remote_oid='after'))
        self.assertEqual(dict(self.connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()), project)
        completed = self.checkpoints.get('repo', 'G-047')
        finish()
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), completed)
        self.assertIsNone(self.runtime.projected('repo'))

    def test_unprojected_publication_rejects_changed_plan_and_rolls_back_storage_failure(self):
        from loop_manager_internal.execution.execution_state import DirectRebasePublicationStatePort
        self.runtime.transition_and_clear('repo', 'G-047', plan_values={'status': 'PLAN_BLOCKED'}, enqueue=False)
        pending = self.begin()
        acceptance = self.after_push()
        def fail(stage):
            if stage == 'after_execplan_update':
                raise RuntimeError('storage interrupted')
        state = DirectRebasePublicationStatePort(self.connection,
            runtime_state.RuntimeStateRepository(self.connection, fault_hook=fail), 'repo', 'G-047')
        before = state.expected
        with self.assertRaisesRegex(RuntimeError, 'storage interrupted'):
            state.complete_rebase_publication(self.checkpoints,
                intent=pending.snapshot['rebase_publication_intent'], acceptance=acceptance,
                reobserve=lambda: ('after', acceptance))
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
        self.assertEqual(self.runtime.get('repo', 'G-047'), before)
        self.assertEqual(state.expected, before)
        self.runtime.update_plan('repo', 'G-047', plan_values={'branch': 'other-owner'}, enqueue=False)
        with self.assertRaises(runtime_state.RuntimeStateError):
            state.check()
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)

    def after_push(self):
        from dataclasses import replace
        expected = replace(self.expected, generation=self.checkpoints.get('repo', 'G-047').generation,
                           remote=replace(self.expected.remote, oid='after'))
        return decision_for(expected, observation(expected)).acceptance

    def test_publication_intent_rejects_stale_generation_head_or_lease(self):
        for changes in ({'expected_generation': 0}, {'remote_before': 'other'},
                        {'acceptance': {**self.acceptance, 'head': 'forged'}}):
            with self.subTest(changes=changes):
                arguments = dict(expected_generation=self.accepted.generation,
                                 acceptance=self.acceptance, remote_before='remote')
                arguments.update(changes)
                with self.assertRaises(CheckpointRace):
                    self.checkpoints.begin_rebase_publication('repo', 'G-047', **arguments)
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), self.accepted)

    def test_completion_is_idempotent_preserves_phase_failure_and_validation(self):
        pending = self.begin()
        intent = pending.snapshot['rebase_publication_intent']
        acceptance = self.after_push()
        before = dict(self.state.row())
        def finish():
            self.state.complete_rebase_publication(self.checkpoints, intent=intent,
                acceptance=acceptance, reobserve=lambda: ('after', acceptance))
        finish()
        completed = self.checkpoints.get('repo', 'G-047')
        projected = self.runtime.projected('repo')
        self.assertIsNone(completed.operation)
        self.assertEqual(completed.snapshot['failure'], 'preserved failure')
        for key in ('phase', 'last_error', 'verify_result', 'review_result', 'worker_result'):
            self.assertEqual(self.state.row()[key], before[key])
        self.assertEqual(self.state.row()['publication_remote_oid'], 'after')
        finish()
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), completed)
        self.assertEqual(self.runtime.projected('repo'), projected)

    def test_projection_failure_rolls_back_completion_and_keeps_retryable_intent(self):
        from loop_manager_internal.execution.execution_state import ExecutionStatePort
        pending = self.begin()
        intent = pending.snapshot['rebase_publication_intent']
        acceptance = self.after_push()
        def fail(stage):
            if stage == 'after_execplan_update':
                raise RuntimeError('projection interrupted')
        state = ExecutionStatePort(self.connection,
            runtime_state.RuntimeStateRepository(self.connection, fault_hook=fail), 'repo')
        before = state.expected
        with self.assertRaisesRegex(RuntimeError, 'projection interrupted'):
            state.complete_rebase_publication(self.checkpoints, intent=intent,
                acceptance=acceptance, reobserve=lambda: ('after', acceptance))
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
        self.assertEqual(self.runtime.projected('repo'), before)
        self.assertEqual(state.expected, before)
        self.state.complete_rebase_publication(self.checkpoints, intent=intent,
            acceptance=acceptance, reobserve=lambda: ('after', acceptance))
        self.assertEqual(self.state.row()['publication_remote_oid'], 'after')

    def test_changed_confirmation_never_advances_runtime_lease(self):
        pending = self.begin()
        acceptance = self.after_push()
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.state.complete_rebase_publication(self.checkpoints,
                intent=pending.snapshot['rebase_publication_intent'], acceptance=acceptance,
                reobserve=lambda: ('competitor', acceptance))
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
        self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')

    def test_pending_publication_cannot_be_replaced_by_another_rebase_or_lease(self):
        pending = self.begin()
        for changed in ({'operation': 'rebase'}, {'observed_remote_oid': 'competitor'},
                        {'expected_head': 'other-head'}):
            with self.subTest(changed=changed):
                values = dict(operation='rebase_publish', expected_head=pending.expected_head,
                              target_base_oid=pending.target_base_oid,
                              observed_remote_oid=pending.observed_remote_oid, snapshot=pending.snapshot)
                values.update(changed)
                with self.assertRaises(CheckpointRace):
                    self.checkpoints.record('repo', 'G-047', expected_generation=pending.generation, **values)
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
