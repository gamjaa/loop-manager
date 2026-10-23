"""Generation and runtime correction CAS tests."""

import sqlite3
import unittest
import json
from unittest import mock

from loop_manager_internal.storage.execution_checkpoint_state import (
    CheckpointRace, ExecutionCheckpointRepository, SCHEMA,
)
from loop_manager_internal.storage import runtime_state
from tests.support.database import runtime_connection
from tests.support.rebase_observations import decision_for, expectation, observation


class CheckpointRepositoryTests(unittest.TestCase):
    def test_legacy_workspace_hold_backfill_is_atomic_and_preserves_checkpoints(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(SCHEMA.replace('    workspace_ownership_held INTEGER NOT NULL DEFAULT 0,\n', ''))
        for plan_id, snapshot in (('G-001', {'workspace_ownership_hold': {'block': 'held'}, 'audit': 'preserved'}),
                                  ('G-002', {'workspace_ownership_hold': {}}), ('G-003', None)):
            connection.execute('INSERT INTO execution_checkpoints(repo,plan_id,generation,snapshot) VALUES (?,?,7,?)',
                               ('repo', plan_id, json.dumps(snapshot)))
        connection.commit()
        before = [tuple(row) for row in connection.execute('SELECT * FROM execution_checkpoints ORDER BY plan_id')]
        with mock.patch('loop_manager_internal.storage.execution_checkpoint_state.json.loads', side_effect=ValueError('bad checkpoint')):
            with self.assertRaisesRegex(ValueError, 'bad checkpoint'):
                ExecutionCheckpointRepository(connection)
        self.assertNotIn('workspace_ownership_held', {row[1] for row in connection.execute('PRAGMA table_info(execution_checkpoints)')})
        ExecutionCheckpointRepository(connection)
        after = [tuple(row) for row in connection.execute('SELECT * FROM execution_checkpoints ORDER BY plan_id')]
        self.assertEqual([row[:-1] for row in after], before)
        self.assertEqual([row[-1] for row in after], [1, 0, 0])
        with mock.patch('loop_manager_internal.storage.execution_checkpoint_state.json.loads', side_effect=AssertionError('repeat backfill')):
            ExecutionCheckpointRepository(connection)

    def test_workspace_hold_projection_tracks_generation_and_outer_rollback(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        def save(generation, snapshot):
            return repository.record('repo', 'G-001', expected_generation=generation, operation=None,
                expected_head=None, target_base_oid=None, observed_remote_oid=None, snapshot=snapshot)
        def held():
            return connection.execute('SELECT workspace_ownership_held FROM execution_checkpoints').fetchone()[0]
        save(0, {'workspace_ownership_hold': {'block': 'held'}})
        connection.commit()
        with self.assertRaises(CheckpointRace):
            save(0, {})
        self.assertEqual(held(), 1)
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with connection:
                connection.execute('BEGIN')
                save(1, {})
                self.assertEqual(held(), 0)
                raise RuntimeError('rollback')
        self.assertEqual(held(), 1)
        save(1, None)
        self.assertEqual(held(), 0)

    def test_workspace_owner_selection_never_reads_checkpoint_body(self):
        from loop_manager_internal.recovery import scoped_blocking
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        repository.record('repo', 'G-047', expected_generation=0, operation=None,
            expected_head=None, target_base_oid=None, observed_remote_oid=None,
            snapshot={'workspace_ownership_hold': {'block': 'held'}, 'audit': 'x' * 1_000_000})
        def authorize(operation, table, column, *_):
            if operation == sqlite3.SQLITE_READ and table == 'execution_checkpoints' and column == 'snapshot':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        self.addCleanup(connection.set_authorizer, None)
        self.assertEqual([row['plan_id'] for row in scoped_blocking.workspace_held_resources(connection, 'repo')], ['G-047'])
        self.assertEqual(scoped_blocking.workspace_held_resources(connection, 'other'), ())

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
        connection = runtime_connection()
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
        connection = runtime_connection()
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
        connection = runtime_connection()
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
        connection = runtime_connection()
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
        self.connection = runtime_connection()
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


class CancelledCheckpointTests(unittest.TestCase):
    def setUp(self):
        import json
        from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        # This READY fixture has no execution ownership; checkpoint receipts
        # below model pending requests and older confirmed publication audit.
        self.connection.execute("UPDATE exec_plans SET workspace_owned=0 WHERE repo='repo'")
        self.connection.commit()
        self.execution = ExecutionCheckpointRepository(self.connection)
        self.blocked = BlockedCheckpointRepository(self.connection)
        self.execution.record(
            'repo', 'G-047', expected_generation=0, operation=None,
            expected_head='head', target_base_oid='base', observed_remote_oid='remote',
            snapshot={'worker_result': 'saved', 'pushed_checkpoint_head': 'published'},
        )
        self.request = self.blocked.request(
            repo='repo', plan_id='G-047', reason_code='failure', evidence='saved failure',
            branch=None, worktree=None, expected_remote_oid='remote', rebase_recovery=False,
        )
        self.connection.execute("UPDATE blocked_checkpoints SET publication_history=?",
                                (json.dumps([{'published_oid': 'confirmed'}]),))
        self.connection.commit()
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.original = ReadyPlanSnapshot(
            row['plan_id'], row['path'], row['title'], ('docs/PRODUCT.md',), (),
            'low', 'low', row['priority'], row['content_hash'], row['plan_sha'],
            '---\nid: G-047\n---\nSaved plan',
            current_metadata_recorded=True,
        )
        self.receipts = {table: dict(self.connection.execute(f'SELECT * FROM {table}').fetchone())
                         for table in ('execution_checkpoints', 'blocked_checkpoints')}

    def cancel(self):
        from loop_manager_internal.planning.planning_catalog import PlanDeletion
        from loop_manager_internal.storage.planning_publication_state import apply_mutable_catalog_changes
        from loop_manager_internal.storage.runtime_state import _transaction
        with _transaction(self.connection):
            apply_mutable_catalog_changes(
                self.connection, 'repo', deletions=(PlanDeletion(self.original),), revisions=(),
                snapshot_token='snapshot', plan_commit='deletion', content_hash=lambda _: '',
                agent=False,
            )

    def test_cancellation_moves_requests_and_confirmed_receipts_to_audit(self):
        import json
        self.cancel()
        audit = json.loads(self.connection.execute(
            'SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.assertEqual(audit['checkpoints'], self.receipts)
        self.assertIsNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        for table in self.receipts:
            self.assertIsNone(self.connection.execute(f'SELECT * FROM {table}').fetchone())
        self.assertEqual(self.execution.get('repo', 'G-047').generation, 0)
        self.assertEqual(self.blocked.pending('repo'), ())
        self.assertIsNone(self.blocked.reconcile_cause(self.request))
        with self.assertRaises(CheckpointRace):
            self.execution.record(
                'repo', 'G-047', expected_generation=0, operation='checkpoint',
                expected_head='new', target_base_oid='base', observed_remote_oid=None, snapshot={},
            )
        with self.assertRaisesRegex(RuntimeError, '취소된 작업'):
            self.blocked.request(
                repo='repo', plan_id='G-047', reason_code='failure', evidence='stale',
                branch=None, worktree=None, expected_remote_oid=None, rebase_recovery=False,
            )
        with self.assertRaisesRegex(RuntimeError, '취소된 작업'):
            self.blocked.record_local(self.request, 'stale-head')
        # An unrelated identity continues to accept independent Git intent.
        other = self.execution.record(
            'repo', 'G-999', expected_generation=0, operation='checkpoint',
            expected_head='other', target_base_oid='base', observed_remote_oid=None, snapshot={},
        )
        self.assertEqual(other.generation, 1)
        self.assertEqual(json.loads(self.connection.execute(
            'SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0]), audit)

    def test_cancellation_failure_restores_requests_with_execution_row(self):
        self.connection.execute("CREATE TRIGGER reject_cancel BEFORE INSERT ON execplan_cancellations "
                                "BEGIN SELECT RAISE(ABORT, 'interrupted cancellation'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'interrupted cancellation'):
            self.cancel()
        self.assertIsNotNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        self.assertIsNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
        for table, receipt in self.receipts.items():
            self.assertEqual(dict(self.connection.execute(f'SELECT * FROM {table}').fetchone()), receipt)
        self.connection.execute('DROP TRIGGER reject_cancel')
        self.cancel()
        self.assertEqual(self.blocked.pending('repo'), ())

    def test_legacy_orphan_requests_cannot_resume_after_restart(self):
        self.cancel()
        # Older runtimes could leave rows behind. Reopening repositories must
        # honor the cancellation even while those old receipts remain intact.
        for table, receipt in self.receipts.items():
            self.connection.execute(
                f"INSERT INTO {table} ({', '.join(receipt)}) VALUES ({', '.join('?' for _ in receipt)})",
                tuple(receipt.values()),
            )
        self.connection.commit()
        from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
        execution = ExecutionCheckpointRepository(self.connection)
        blocked = BlockedCheckpointRepository(self.connection)
        self.assertIsNone(execution.get('repo', 'G-047').operation)
        self.assertEqual(blocked.pending('repo'), ())
        with self.assertRaisesRegex(RuntimeError, '취소된 작업'):
            blocked.get('repo', 'G-047')
        self.assertIsNone(blocked.reconcile_cause(self.request))
        for table, receipt in self.receipts.items():
            self.assertEqual(dict(self.connection.execute(f'SELECT * FROM {table}').fetchone()), receipt)

    def test_pending_call_reservation_preserves_plan_and_cannot_be_recreated_after_cancel(self):
        from pathlib import Path
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.planning.planning_catalog import PlannerValidationError
        task_calls.ensure_schema(self.connection)
        target = task_calls.reserve(self.connection, Path('/unused'), 'repo', 'G-047', 'verify', 'run')
        other = task_calls.reserve(self.connection, Path('/unused'), 'repo', 'G-999', 'agent', 'other')
        with self.assertRaisesRegex(PlannerValidationError, '호출 예약'):
            self.cancel()
        self.assertIsNotNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        for table, receipt in self.receipts.items():
            self.assertEqual(dict(self.connection.execute(f'SELECT * FROM {table}').fetchone()), receipt)
        task_calls.release(target)
        self.cancel()
        with self.assertRaises(task_calls.CallDeferred):
            task_calls.reserve(self.connection, Path('/unused'), 'repo', 'G-047', 'verify', 'late')
        self.assertEqual([row[0] for row in self.connection.execute('SELECT token FROM task_call_reservations')], [other.token])

    def test_collected_agent_result_requires_physical_finish_before_cancellation(self):
        from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile
        from loop_manager_internal.storage.task_calls import CallDeferred
        from loop_manager_internal.planning.planning_catalog import PlannerValidationError
        repository = InvocationRepository(self.connection)
        context = InvocationContext('repo', 'G-047')
        profile = ResolvedProfile('model', 'high')
        invocation = repository.start(context, 'worker', 'codex', profile, 'saved-session')
        repository.mark_execution_started(invocation)
        self.connection.execute("UPDATE agent_invocations SET finished_at='collected', result_kind='success', "
                                "result_code=0 WHERE invocation_id=?", (invocation,))
        self.connection.commit()
        with self.assertRaisesRegex(PlannerValidationError, 'writer 종료'):
            self.cancel()
        self.assertIsNotNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        self.connection.execute("UPDATE agent_invocations SET execution_finished_at='stopped', "
                                "execution_finish_evidence='receipt' WHERE invocation_id=?", (invocation,))
        self.connection.commit()
        audit = dict(self.connection.execute('SELECT * FROM agent_invocations').fetchone())
        self.cancel()
        with self.assertRaises(CallDeferred):
            repository.start(context, 'worker', 'codex', profile, 'saved-session')
        self.assertEqual(dict(self.connection.execute('SELECT * FROM agent_invocations').fetchone()), audit)

    def test_git_operation_defers_cancellation_without_consuming_requests(self):
        from loop_manager_internal.planning.planning_catalog import PlannerValidationError
        current = self.execution.get('repo', 'G-047')
        saved = self.execution.record(
            'repo', 'G-047', expected_generation=current.generation, operation='rebase',
            expected_head=current.expected_head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid, snapshot=current.snapshot,
        )
        self.connection.commit()
        with self.assertRaisesRegex(PlannerValidationError, 'Git operation'):
            self.cancel()
        self.assertEqual(self.execution.get('repo', 'G-047'), saved)
        self.assertEqual(self.blocked.get('repo', 'G-047'), self.request)
        self.execution.record(
            'repo', 'G-047', expected_generation=saved.generation, operation=None,
            expected_head=saved.expected_head, target_base_oid=saved.target_base_oid,
            observed_remote_oid=saved.observed_remote_oid, snapshot=saved.snapshot,
        )
        self.connection.commit()
        self.cancel()
        self.assertIsNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())

    def test_manual_preparation_preserves_group_and_execution_until_owner_releases(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from loop_manager_internal.planning.planning_catalog import PlannerValidationError
        store = ManualWorkRepository(self.connection)
        group = store.create('repo', 'start', 'manual', [], 'head', 'base', members=('G-047',))
        group = store.update(group, owner_id='G-047')
        self.connection.commit()
        with self.assertRaisesRegex(PlannerValidationError, '수동'):
            self.cancel()
        self.assertEqual(dict(store.get('repo', group['group_id'])), dict(group))
        self.assertIsNotNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        # A previously closed, unowned group remains historical audit. It is
        # not a manual stop/reset request and is not rewritten by cancellation.
        group = store.update(group, state='CLOSED')
        self.connection.commit()
        self.cancel()
        self.assertEqual(dict(store.get('repo', group['group_id'])), dict(group))
        with self.assertRaisesRegex(runtime_state.RuntimeStateError, '취소된 작업'):
            store.accept(group, head='new', base='base', input_token='token')
        self.assertIsNone(self.connection.execute('SELECT * FROM manual_work_requests').fetchone())

    def test_stale_manual_request_is_not_selected_after_cancellation(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        store = ManualWorkRepository(self.connection)
        self.connection.execute("INSERT INTO exec_plans (repo,plan_id,path,title,sources,depends_on,priority,content_hash,status) "
            "VALUES ('repo','G-999','other.md','Other','[]','[]','normal','other','READY')")
        group = store.create('repo', 'start', 'manual', [], 'head', 'base', members=('G-047',))
        group = store.update(group, state='CLOSED', owner_id='G-047')
        self.connection.commit()
        self.cancel()
        # Preserve an old runtime's request verbatim, including audit identity.
        self.connection.execute("INSERT INTO manual_work_requests "
            "(request_id,repo,group_id,source_head,base_sha,input_token,skip_verify,skip_review,reason,created_at) "
            "VALUES ('legacy','repo',?,'head','base','token',0,0,'old','earlier')", (group['group_id'],))
        group = store.update(group, state='AUTOMATIC', request_id='legacy')
        other = store.create('repo', 'start', 'independent', [], 'head', 'base', members=('G-999',))
        other = store.update(other, owner_id='G-999', state='AUTOMATIC')
        request = store.accept(other, head='head', base='base', input_token='token')
        self.connection.commit()
        self.assertEqual([row['group_id'] for row in store.pending('repo')], [other['group_id']])
        self.assertEqual(dict(store.get('repo', group['group_id'])), dict(group))
        self.assertEqual(store.request(group)['request_id'], 'legacy')
        self.assertEqual(dict(store.request(store.get('repo', other['group_id']))), dict(request))

    def test_shared_resource_owner_preserves_both_execution_rows(self):
        from loop_manager_internal.planning.planning_catalog import PlannerValidationError
        self.connection.execute("UPDATE exec_plans SET branch='shared', worktree='/shared'")
        self.connection.execute("INSERT INTO exec_plans (repo,plan_id,path,title,sources,depends_on,priority,"
            "content_hash,status,branch,worktree) VALUES ('repo','G-999','other.md','Other','[]','[]',"
            "'normal','other','VERIFYING','shared','/shared')")
        self.connection.commit()
        before = [dict(row) for row in self.connection.execute('SELECT * FROM exec_plans ORDER BY plan_id')]
        with self.assertRaisesRegex(PlannerValidationError, '공유 자원'):
            self.cancel()
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM exec_plans ORDER BY plan_id')], before)
        self.assertIsNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())

    def test_external_deletion_recheck_precedes_transaction_and_preserves_audit_on_race(self):
        from loop_manager_internal.catalog import execplan_catalog, remote_deletions
        from loop_manager_internal.storage.execplan_catalog_state import DefinitionStateRepository, PinnedCatalog
        repository = DefinitionStateRepository(self.connection)
        expected = repository.capture('repo')
        safe = [False]
        def recheck():
            self.assertFalse(self.connection.in_transaction)
            return safe[0]
        deletion = remote_deletions.RemoteDeletion(self.original, '{}', recheck)
        pinned = PinnedCatalog('revision', execplan_catalog.snapshot_from_documents({}), deletions=(deletion,))
        self.assertIsNone(repository.apply('repo', pinned, expected, revision_is_current=lambda: True))
        self.assertEqual(repository.capture('repo'), expected)
        for table, receipt in self.receipts.items():
            self.assertEqual(dict(self.connection.execute(f'SELECT * FROM {table}').fetchone()), receipt)
        safe[0] = True
        self.assertEqual(repository.apply('repo', pinned, expected, revision_is_current=lambda: True), ('G-047',))
        self.assertIsNone(self.connection.execute('SELECT * FROM exec_plans').fetchone())

    def test_external_recheck_never_runs_inside_callers_transaction(self):
        from unittest import mock
        from loop_manager_internal.catalog import execplan_catalog, remote_deletions
        from loop_manager_internal.storage.execplan_catalog_state import DefinitionStateRepository, PinnedCatalog
        repository = DefinitionStateRepository(self.connection)
        expected = repository.capture('repo')
        recheck = mock.Mock(side_effect=AssertionError('external I/O under SQLite writer'))
        pinned = PinnedCatalog('revision', execplan_catalog.snapshot_from_documents({}),
            deletions=(remote_deletions.RemoteDeletion(self.original, '{}', recheck),))
        self.connection.execute('BEGIN IMMEDIATE')
        self.assertIsNone(repository.apply('repo', pinned, expected, revision_is_current=lambda: True))
        recheck.assert_not_called()
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(repository.capture('repo'), expected)
        self.connection.rollback()
