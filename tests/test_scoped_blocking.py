import json
import sqlite3
import tempfile
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
import blocked_checkpoint_state
import execution_checkpoint_state
import maintenance_state
import notifications
import project_registry
import scoped_blocking
import rebase_block_reconciliation
import blocked_plan_reconciliation
import blocked_remote_observation
from scoped_blocking import BlockReasonCode, BlockScope


class BlockedPlanRemoteRefreshTests(unittest.TestCase):
    """Coordinator unit tests: every Git/verify/Agent effect is a fake port."""

    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)
        self.repo, self.base, self.head = 'repo', 'a' * 40, 'b' * 40
        self.connection.execute("INSERT INTO projects (repo, base_branch, origin) VALUES ('repo', 'main', 'example/repo')")
        for plan_id in ('G-106', 'G-107'):
            self.connection.execute(
                'INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, '
                'content_hash, status, branch, worktree, workspace_owned, worker_session_id, '
                'verify_failure_history, effective_worker_level, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (self.repo, plan_id, f'docs/plans/[{plan_id}] Work.md', 'Work', '[]', '[]', 'hash',
                 'IMPLEMENTING', f'loop-manager/{plan_id}', f'/state/{plan_id}', 1, 'session', '{"failure":3}', 6, 'critical'))
            runtime_state = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
            runtime_state.block_plan(self.repo, plan_id, reason_code='verify_retry_exhausted',
                                     evidence='original verify failure', release_fingerprint='input',
                                     clear_projection=True)
            self.connection.execute('UPDATE exec_plans SET blocked_fingerprint = ? WHERE plan_id = ?',
                                    ('input', plan_id))
            self.record_observation(plan_id)
        self.signature = 'before'
        self.ports = mock.Mock(spec=loop_manager)
        self.ports.GUIDANCE_BLOCK_REASON = loop_manager.GUIDANCE_BLOCK_REASON
        self.ports.PR_CLOSED_BLOCK_REASON = loop_manager.PR_CLOSED_BLOCK_REASON
        self.ports.unmet_execplan_dependencies.return_value = []
        self.ports.github_repository.return_value = 'example/repo'
        self.ports.pr_for_branch.return_value = None
        self.ports._blocked_execplan_metadata_error.return_value = None
        self.ports._worktree_execplan_input_fingerprint.return_value = 'input'
        self.ports._revision_execplan_input_fingerprint.return_value = 'input'
        self.ports._worktree_state_signature.side_effect = lambda _: self.signature
        self.ports.capture_git_snapshot.return_value = SimpleNamespace(
            head=self.head, dirty={}, index={}, operations={})
        self.ports.git.side_effect = self.git
        self.ports._update_blocked_error.side_effect = loop_manager._update_blocked_error
        self.ports._evaluate_remote_checkpoint_cause.return_value = (False, 'same failure', 'verify')
        self.ports._current_rebase_service.return_value.require_owned.return_value = (
            SimpleNamespace(head=self.head), SimpleNamespace(status=SimpleNamespace(value='complete')))
        self.ports.observe_blocked_plan_remotes.side_effect = self.observe
        self.ref_probe = mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
            blocked_remote_observation.RefObservation('present', self.base),
            blocked_remote_observation.RefObservation('absent')))
        self.ref_probe.start()
        self.addCleanup(self.ref_probe.stop)
        self.coordinator = blocked_plan_reconciliation.BlockedPlanReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main',
            timeout_seconds=600)

    def git(self, _, *args, **kwargs):
        output = ''
        if args[:2] == ('remote', 'get-url'):
            output = 'https://example.test/example/repo'
        elif args[:1] == ('rev-parse',):
            output = self.base if args[1] == 'FETCH_HEAD^{commit}' else self.head
        return SimpleNamespace(returncode=0, stdout=output)

    def record_observation(self, plan_id='G-106'):
        blocked_remote_observation.record(
            self.connection, repo=self.repo, plan_id=plan_id,
            registered_origin='example/repo', origin_url='https://example.test/example/repo',
            base_ref='refs/heads/main', branch_ref=f'refs/heads/loop-manager/{plan_id}',
            base=blocked_remote_observation.RefObservation('present', self.base),
            branch=blocked_remote_observation.RefObservation('absent'),
            blocked_reason='verify_retry_exhausted', blocked_fingerprint='input',
            project_blocked_reason=None, project_blocked_fingerprint=None,
            worktree_fingerprint=getattr(self, 'signature', 'before'))
        self.connection.commit()

    def observe(self, connection, repo_path, repo, project, *, plan_id):
        self.record_observation(plan_id)

    def plan(self, plan_id='G-106'):
        return self.connection.execute('SELECT * FROM exec_plans WHERE plan_id = ?', (plan_id,)).fetchone()

    def row(self):
        return self.connection.execute("SELECT * FROM blocked_remote_observations WHERE plan_id = 'G-106'").fetchone()

    def test_other_worker_defers_without_losing_observation_or_projection(self):
        self.connection.execute("UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE plan_id = 'G-107'")
        self.connection.commit()
        before = dict(self.row())
        self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred')
        self.assertEqual(dict(self.row()), before)
        self.ports.git.assert_not_called()

    def test_legacy_origin_not_yet_registered_keeps_existing_document_recovery(self):
        self.connection.execute(
            "UPDATE blocked_remote_observations SET registered_origin = NULL, "
            "base_result = 'error', branch_result = 'error', base_oid = NULL, "
            "base_error = 'registered-origin-missing', branch_error = 'registered-origin-missing' "
            "WHERE plan_id = 'G-106'")
        self.connection.commit()
        plan, block = dict(self.plan()), dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
        self.assertEqual(self.coordinator.reconcile('G-106'), 'unhandled')
        self.assertEqual(dict(self.plan()), plan)
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')), block)
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        self.ports.git.assert_not_called()
        self.ports._update_blocked_error.assert_not_called()

    def test_registered_origin_lookup_failure_does_not_take_legacy_release_path(self):
        failure = blocked_remote_observation.RefObservation('error', error='network')
        with mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(failure, failure)):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_not_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')

    def test_manager_keeps_legacy_input_recovery_when_origin_is_not_registered(self):
        self.connection.execute(
            "UPDATE blocked_remote_observations SET registered_origin = NULL WHERE plan_id = 'G-106'")
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.commit()
        other = dict(self.plan('G-107'))
        def resume(connection, repo_path, repo, base, state_dir, *, plan_id):
            self.assertEqual(plan_id, 'G-106')
            runtime = loop_manager.runtime_state.RuntimeStateRepository(connection)
            runtime.update_plan(repo, plan_id, plan_values={'status': 'IMPLEMENTING',
                                'blocked_reason': None, 'blocked_fingerprint': None}, enqueue=False)
            scoped_blocking.clear_plan_block(connection, repo, plan_id)
            return True
        with (mock.patch.object(loop_manager, '_resume_interrupted_blocked_rebase', return_value=None),
              mock.patch.object(loop_manager, 'resume_blocked_execplan_if_inputs_changed', side_effect=resume) as recover):
            self.assertTrue(loop_manager.resume_plan_blocks(
                self.connection, Path('/repo'), Path('/state'), self.repo, 'main'))
        recover.assert_called_once()
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertEqual(dict(self.plan('G-107')), other)
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))

    def test_unmerged_dependency_leaves_observed_input_unconsumed(self):
        self.ports.unmet_execplan_dependencies.return_value = ['G-105']
        self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        self.ports.git.assert_not_called()

    def test_planning_and_maintenance_keep_the_serial_slot(self):
        for update, reset in (
                ("UPDATE projects SET phase = 'PLANNING'", "UPDATE projects SET phase = NULL"),
                ("UPDATE projects SET planning_error = 'saved error'", "UPDATE projects SET planning_error = NULL"),
                ("UPDATE projects SET pr_conflict_recovery = 1", "UPDATE projects SET pr_conflict_recovery = 0"),
                ("INSERT INTO maintenance_cycles (repo, phase) VALUES ('repo', 'assessment')", "DELETE FROM maintenance_cycles")):
            with self.subTest(update=update):
                self.connection.execute(update)
                self.connection.commit()
                self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred')
                self.connection.execute(reset)
                self.connection.commit()
        self.ports.git.assert_not_called()
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))

    def test_order_and_unchanged_input_do_not_repeat_cause_check_or_publication(self):
        steps = []
        with (mock.patch.object(self.coordinator, '_adopt', side_effect=lambda *args: steps.append('adopt')),
              mock.patch.object(self.coordinator, '_incorporate', side_effect=lambda plan, context:
                                (steps.append('rebase-and-publish'), context.update(candidate=self.signature))),
              mock.patch.object(self.coordinator, '_publish', side_effect=lambda *args: steps.append('checkpoint'))):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(steps, ['adopt', 'rebase-and-publish', 'checkpoint'])
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
        self.assertFalse(blocked_remote_observation.pending_input(self.row()))
        self.assertTrue(blocked_remote_observation.pending_input(self.connection.execute(
            "SELECT * FROM blocked_remote_observations WHERE plan_id = 'G-107'").fetchone()))
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')

    def test_release_keeps_session_failure_audit_and_other_plan_and_invalidates_tokens(self):
        self.connection.execute("UPDATE exec_plans SET worker_result = 'old worker', verify_token = 'old verify', review_token = 'old review', publication_token = 'old publication' WHERE plan_id = 'G-106'")
        self.connection.commit()
        other = dict(self.plan('G-107'))
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, 'fixed', None)
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'resumed')
        publish.assert_not_called()
        plan = self.plan()
        self.assertEqual(plan['status'], 'IMPLEMENTING')
        self.assertEqual(plan['worker_session_id'], 'session')
        self.assertEqual(plan['verify_failure_history'], '{"failure":3}')
        self.assertEqual(plan['effective_worker_level'], 6)
        for field in ('worker_result', 'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(plan[field])
        self.assertEqual(dict(self.plan('G-107')), other)
        self.assertIsNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
        self.assertEqual(self.connection.execute('SELECT plan_path FROM projects').fetchone()[0], plan['path'])

    def test_refusal_keeps_original_cause_and_is_memoized(self):
        self.ports.pr_for_branch.return_value = {'state': 'OPEN'}
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.ports._update_blocked_error.assert_called_once()
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')

    def test_fetch_race_preserves_files_and_does_not_rebase_or_release(self):
        real_fake_git = self.git
        self.ports.git.side_effect = lambda path, *args, **kwargs: (
            SimpleNamespace(returncode=0, stdout='different') if args == ('rev-parse', 'FETCH_HEAD^{commit}')
            else real_fake_git(path, *args, **kwargs))
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_not_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertIn('OID 경쟁', self.plan()['last_error'])

    def test_base_only_change_rebases_even_with_unchanged_document_fingerprint(self):
        self.ports.git.side_effect = lambda path, *args, **kwargs: (
            SimpleNamespace(returncode=1, stdout='') if args[:1] == ('merge-base',)
            else self.git(path, *args, **kwargs))
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        def rebase(*args, **kwargs):
            store.record(self.repo, 'G-106', expected_generation=0, operation='rebase',
                         expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
            return SimpleNamespace(returncode=0, stdout='rebased')
        def complete(*args, **kwargs):
            current = store.get(self.repo, 'G-106')
            store.record(self.repo, 'G-106', expected_generation=current.generation, operation=None,
                         expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
        self.ports._journaled_direct_rebase.side_effect = rebase
        self.ports._complete_direct_rebase_checkpoint.side_effect = complete
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_called_once()
        self.ports._complete_direct_rebase_checkpoint.assert_called_once()
        self.assertEqual(self.plan()['blocked_fingerprint'], 'input')

    def test_interrupted_evaluated_stage_does_not_repeat_rebase_or_cause_check(self):
        with mock.patch.object(self.coordinator, '_publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        with (mock.patch.object(self.coordinator, '_incorporate') as incorporate,
              mock.patch.object(self.coordinator, '_publish') as publish):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        incorporate.assert_not_called()
        publish.assert_called_once()
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()

    def test_candidate_change_after_evaluation_does_not_release(self):
        with mock.patch.object(self.coordinator, '_publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        self.signature = 'external edit'
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertIn('후보', self.plan()['last_error'])

    def test_pending_rebase_reuses_current_state_without_execution_history(self):
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        store.record(self.repo, 'G-106', expected_generation=0, operation='rebase',
                     expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
        self.connection.commit()
        def finish(*args, **kwargs):
            current = store.get(self.repo, 'G-106')
            store.record(self.repo, 'G-106', expected_generation=current.generation,
                         operation=None, expected_head=self.head, target_base_oid=self.base,
                         observed_remote_oid=None, snapshot={})
        self.ports._complete_direct_rebase_checkpoint.side_effect = finish
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_not_called()
        self.ports._complete_direct_rebase_checkpoint.assert_called_once()

    def test_current_scope_mismatch_does_not_run_cause_check_or_publication(self):
        self.ports._current_rebase_service.return_value.require_owned.side_effect = RuntimeError('scope mismatch')
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        publish.assert_not_called()
        self.assertIn('scope mismatch', self.plan()['last_error'])

    def test_scope_is_rechecked_after_resolved_probe_before_release(self):
        def resolved(*args, **kwargs):
            self.ports._current_rebase_service.return_value.require_owned.side_effect = RuntimeError('current scope changed')
            return True, 'fixed', None
        self.ports._evaluate_remote_checkpoint_cause.side_effect = resolved
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertIsNotNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))

    def test_changed_slot_owner_keeps_other_plan_projection_and_diagnostic(self):
        projected = {}
        def switch_owner(*args, **kwargs):
            self.connection.execute(
                "UPDATE exec_plans SET status = 'IMPLEMENTING', last_error = 'other worker feedback' WHERE plan_id = 'G-107'")
            runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
            runtime.hydrate_snapshot(self.repo, runtime.get(self.repo, 'G-107'))
            projected.update(dict(self.connection.execute('SELECT * FROM projects').fetchone()))
            return True, 'fixed', None
        self.ports._evaluate_remote_checkpoint_cause.side_effect = switch_owner
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        publish.assert_not_called()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), projected)
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertEqual(self.plan('G-107')['last_error'], 'other worker feedback')

    def test_new_verify_failure_does_not_clear_a_saved_review_cause(self):
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, 'new verify failure', 'verify')
        context = {'block': {'reason_code': 'review_retry_exhausted', 'evidence': 'review issue'}, 'artifacts': {}}
        self.assertFalse(self.coordinator._evaluate(self.plan(), context)['resolved'])

    def test_saved_start_intent_runs_git_once_after_interruption_before_rebase(self):
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        store.record(self.repo, 'G-106', expected_generation=0, operation='rebase',
                     expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
        self.connection.commit()
        status = SimpleNamespace(value='ready_to_start')
        self.ports._current_rebase_service.return_value.require_owned.return_value = (
            SimpleNamespace(head=self.head), SimpleNamespace(status=status))
        def start(*args, **kwargs):
            status.value = 'complete'
            return SimpleNamespace(returncode=0, stdout='rebased')
        def finish(*args, **kwargs):
            current = store.get(self.repo, 'G-106')
            store.record(self.repo, 'G-106', expected_generation=current.generation, operation=None,
                         expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
        self.ports._journaled_direct_rebase.side_effect = start
        self.ports._complete_direct_rebase_checkpoint.side_effect = finish
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_called_once()
        self.assertIsNone(store.get(self.repo, 'G-106').operation)

    def test_incomplete_assessment_retries_only_the_cause_probe(self):
        self.ports._evaluate_remote_checkpoint_cause.side_effect = [RuntimeError('transport unavailable'),
                                                                   (False, 'same cause', None)]
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        with (mock.patch.object(self.coordinator, '_incorporate') as incorporate,
              mock.patch.object(self.coordinator, '_publish')):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        incorporate.assert_not_called()
        self.assertEqual(self.ports._evaluate_remote_checkpoint_cause.call_count, 2)

    def test_stale_base_probe_leaves_input_pending_for_a_new_target(self):
        self.ports._evaluate_remote_checkpoint_cause.return_value = (False, 'base advanced', 'stale_base')
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        publish.assert_not_called()
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')

    def test_saved_target_finishes_before_rebase_of_later_observed_base(self):
        old_target = 'c' * 40
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        store.record(self.repo, 'G-106', expected_generation=0, operation='rebase',
                     expected_head=self.head, target_base_oid=old_target, observed_remote_oid=None, snapshot={})
        self.connection.commit()
        steps = []
        def finish(*args, **kwargs):
            current = store.get(self.repo, 'G-106')
            steps.append(('complete', current.target_base_oid))
            store.record(self.repo, 'G-106', expected_generation=current.generation, operation=None,
                         expected_head=self.head, target_base_oid=current.target_base_oid, observed_remote_oid=None, snapshot={})
        def start(*args, **kwargs):
            steps.append(('start', args[6]))
            current = store.get(self.repo, 'G-106')
            store.record(self.repo, 'G-106', expected_generation=current.generation, operation='rebase',
                         expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
            return SimpleNamespace(returncode=0, stdout='rebased')
        self.ports._complete_direct_rebase_checkpoint.side_effect = finish
        self.ports._journaled_direct_rebase.side_effect = start
        self.ports.git.side_effect = lambda path, *args, **kwargs: (
            SimpleNamespace(returncode=1, stdout='') if args[:1] == ('merge-base',) else self.git(path, *args, **kwargs))
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(steps, [('complete', old_target), ('start', self.base), ('complete', self.base)])

    def test_explicit_decision_and_safety_causes_cannot_be_cleared_by_oid_change(self):
        for reason in (loop_manager.GUIDANCE_BLOCK_REASON, 'user_decision_required',
                       'workspace_ownership_uncertain', loop_manager.PR_CLOSED_BLOCK_REASON):
            with self.subTest(reason=reason):
                context = {'block': {'reason_code': reason, 'evidence': 'original'},
                           'artifacts': {}, 'base_oid': self.base}
                result = self.coordinator._evaluate(self.plan(), context)
                self.assertFalse(result['resolved'])
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()

    def test_specification_and_worker_output_causes_use_their_document_release_input(self):
        self.ports._worktree_execplan_input_fingerprint.return_value = 'changed specification'
        for reason in ('review_specification_deadlock', 'worker_output_invalid'):
            with self.subTest(reason=reason):
                context = {'block': {'reason_code': reason, 'evidence': 'original'},
                           'artifacts': {}, 'base_oid': self.base}
                self.assertTrue(self.coordinator._evaluate(self.plan(), context)['resolved'])
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()

    def test_preexisting_local_decision_document_is_not_a_new_release_input(self):
        self.connection.execute(
            "UPDATE exec_plans SET blocked_worktree_fingerprint = 'pending local proposal' WHERE plan_id = 'G-106'")
        self.ports._worktree_execplan_input_fingerprint.return_value = 'pending local proposal'
        context = {'block': {'reason_code': 'user_decision_required', 'evidence': 'pending decision'},
                   'artifacts': {}, 'base_oid': self.base}
        self.assertFalse(self.coordinator._evaluate(self.plan(), context)['resolved'])
        # Publishing that document to base is a new explicit input even when
        # its contents match the local proposal captured at the original block.
        self.ports._revision_execplan_input_fingerprint.return_value = 'pending local proposal'
        self.assertTrue(self.coordinator._evaluate(self.plan(), context)['resolved'])
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()

    def test_explicit_plan_selection_does_not_depend_on_another_mirror(self):
        self.connection.execute("UPDATE projects SET plan_path = 'docs/plans/[G-107] Work.md'")
        selected = loop_manager._plan_block_selected_for_release(self.connection, self.repo, plan_id='G-106')
        self.assertEqual(selected['plan_id'], 'G-106')
        self.assertEqual(self.connection.execute('SELECT plan_path FROM projects').fetchone()[0], self.plan('G-107')['path'])

    def test_manager_iterates_explicit_plan_ids_and_stops_after_selected_resume(self):
        with mock.patch.object(blocked_plan_reconciliation, 'BlockedPlanReconciler') as factory:
            factory.return_value.reconcile.side_effect = ['blocked', 'resumed']
            self.assertTrue(loop_manager.resume_plan_blocks(
                self.connection, Path('/repo'), Path('/state'), self.repo, 'main'))
        self.assertEqual(factory.return_value.reconcile.call_args_list, [mock.call('G-106'), mock.call('G-107')])

    def test_uncertain_verify_process_creates_safety_block_without_publication(self):
        self.ports._evaluate_remote_checkpoint_cause.side_effect = loop_manager.VerifyTerminationUncertain('verify child is still alive')
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        publish.assert_not_called()
        self.assertEqual(scoped_blocking.project_block(self.connection, self.repo)['reason_code'], 'repository_state_unsafe')
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')

    def test_manager_preserves_new_project_safety_block_and_stops_other_recovery(self):
        def block_project(plan_id):
            with self.connection:
                scoped_blocking.record_project_block(self.connection, self.repo, BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                                                     'uncertain child', 'input', plan_id=plan_id, previous_phase=None)
            return 'blocked'
        with mock.patch.object(blocked_plan_reconciliation, 'BlockedPlanReconciler') as factory:
            factory.return_value.reconcile.side_effect = block_project
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path('/repo'), Path('/state'), self.repo, 'main'))
        factory.return_value.reconcile.assert_called_once_with('G-106')
        self.assertEqual(self.connection.execute('SELECT phase FROM projects').fetchone()[0], 'PROJECT_BLOCKED')


class ScopedBlockingTest(unittest.TestCase):
    def test_incomplete_legacy_rebase_plan_block_stays_blocked(self):
        self.plan('G-001', 'PLAN_BLOCKED', branch='loop-manager/G-001')
        reason = rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET blocked_reason = 'managed_worktree_conflict' "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, "
                "evidence) VALUES (?, 'G-001', 'plan', "
                "'managed_worktree_conflict', ?)", (self.repo, reason),
            )
        with (
            mock.patch.object(loop_manager, '_reconcile_rebase_evidence_block',
                              return_value=False) as proof,
            mock.patch.object(loop_manager, 'hydrate_execplan_project') as generic,
        ):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'),
                self.repo, 'main',
            ))
        proof.assert_called_once()
        generic.assert_not_called()
        self.assertIsNotNone(scoped_blocking.plan_block(
            self.connection, self.repo, 'G-001',
        ))

    def test_rebase_false_block_cannot_take_generic_project_release(self):
        plan = self.plan('G-001', 'VERIFYING', branch='loop-manager/G-001')
        reason = rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/managed', active_sha = 'base', "
                "worker_session_id = 'worker' WHERE repo = ? AND plan_id = 'G-001'",
                (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = 'loop-manager/G-001', worktree = '/managed' WHERE repo = ?",
                (plan['path'], self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, "
                "evidence, previous_phase) VALUES (?, 'project', "
                "'repository_state_unsafe', 'G-001', ?, 'VERIFYING')",
                (self.repo, reason),
            )
            self.connection.execute(
                "INSERT INTO recovery_records (repo, subject, identity, reason_code, "
                "disposition, pending_action, phase, base_revision, fingerprint, progress_marker, "
                "occurrence, attempts, detail) VALUES (?, 'G-001', 'retry', "
                "'status_failed', 'transient_retry', 'pause', 'VERIFYING', 'base', "
                "'fingerprint', 'base|worker|', 1, 3, ?)",
                (self.repo, reason),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation='rebase',
                expected_head='before', target_base_oid='base',
                observed_remote_oid=None, snapshot={},
            )
        with (
            mock.patch.object(loop_manager, '_reconcile_rebase_evidence_block',
                              return_value=False) as proof,
            mock.patch.object(loop_manager, 'validate_managed_worktree') as generic,
        ):
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        proof.assert_called_once()
        generic.assert_not_called()
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager.manager_git_journal, 'live_remote_branch_oid',
                              return_value='external-head'),
            mock.patch.object(loop_manager, '_ExecutionLifecyclePorts') as construction,
        ):
            self.assertFalse(loop_manager._reconcile_rebase_evidence_block(
                self.connection, Path(self.repo), Path('/state'), self.repo,
                scoped_blocking.project_block(self.connection, self.repo),
                plan_block=False,
            ))
        construction.assert_not_called()
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager.manager_git_journal, 'live_remote_branch_oid', return_value=None),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, '_ExecutionLifecyclePorts',
                              side_effect=RuntimeError('injected evidence failure')),
        ):
            self.assertFalse(loop_manager._reconcile_rebase_evidence_block(
                self.connection, Path(self.repo), Path('/state'), self.repo,
                scoped_blocking.project_block(self.connection, self.repo),
                plan_block=False,
            ))
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()['phase'], 'PROJECT_BLOCKED')

    def test_project_owner_recovers_pushed_unrebased_review_commit(self):
        branch, published, corrected = 'loop-manager/G-001', 'a' * 40, 'b' * 40
        worktree = Path('/managed')
        plan = self.plan('G-001', 'VERIFYING', branch=branch)
        marker = {'request_identity': 'review-request',
                  'owner_oid': published, 'head': published}
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), corrected, self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'remote inspection failed', 'VERIFYING')",
                (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO blocked_checkpoints "
                "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                "branch, worktree, accepted_remote_oid) "
                "VALUES (?, 'G-001', 'review-request', 'published', "
                "'review_retry_exhausted', 'review', ?, ?, ?)",
                (self.repo, branch, str(worktree), published),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation='push',
                expected_head=corrected, target_base_oid='base',
                observed_remote_oid=published,
                snapshot={'published_review_resume': marker,
                          'remote_before': published},
            )
        current = SimpleNamespace(
            head=corrected, branch=branch, operations={}, dirty={}, index={},
            shared_metadata_complete=True, refs={
                f'refs/heads/{branch}': (corrected, ''),
                f'refs/remotes/origin/{branch}': (corrected, ''),
            },
        )
        live = {'oid': corrected}

        def git(_repo, *args, **_kwargs):
            self.assertEqual(args[0], 'ls-remote')
            return SimpleNamespace(
                returncode=0,
                stdout=f"{live['oid']}\trefs/heads/{branch}\n",
            )

        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value=corrected),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, 'git', side_effect=git),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=published) as owner,
        ):
            live['oid'] = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            live['oid'] = corrected
            owner.return_value = None
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            owner.return_value = published
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            self.assertFalse(owner.call_args.kwargs['require_live'])
            execution = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            )
            pending = execution.get(self.repo, 'G-001')
            self.assertNotIn('blocked_resume', pending.snapshot)
            execution.record(
                self.repo, 'G-001', expected_generation=pending.generation,
                operation=None, expected_head=corrected,
                target_base_oid='base', observed_remote_oid=corrected,
                snapshot={**pending.snapshot,
                          'pushed_checkpoint_head': corrected},
            )
            with self.connection:
                self.connection.execute(
                    "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?",
                    (self.repo,),
                )
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES (?, 'project', 'publication_ownership_uncertain', "
                    "'G-001', 'remote inspection failed again', 'VERIFYING')",
                    (self.repo,),
                )
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertEqual(saved.snapshot['published_review_resume'], marker)
        self.assertNotIn('blocked_resume', saved.snapshot)

    def test_legacy_rebased_project_owner_uses_rebase_proof(self):
        branch, published, rebased = 'loop-manager/G-001', 'a' * 40, 'b' * 40
        worktree = Path('/managed')
        plan = self.plan('G-001', 'IMPLEMENTING', branch=branch)
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = NULL "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'old rebase projection', 'WORKING')", (self.repo,),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation=None,
                expected_head=rebased, target_base_oid='base',
                observed_remote_oid=published,
                snapshot={'reactivated_from_head': published,
                          'base_oid': 'base'},
            )
        current = SimpleNamespace(
            head=rebased, branch=branch, operations={}, dirty={}, index={},
        )
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value=published),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, '_direct_rebase_history_proven',
                              return_value=True) as rebase_proof,
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=published),
        ):
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        rebase_proof.assert_called_once()
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertEqual(saved.snapshot['blocked_resume']['before_head'], published)
        self.assertEqual(saved.snapshot['blocked_resume']['after_head'], rebased)
        self.assertNotIn('published_review_resume', saved.snapshot)

    def test_project_owner_recovers_pending_unrebased_worker_commit(self):
        branch, published, corrected = 'loop-manager/G-001', 'a' * 40, 'b' * 40
        worktree = Path('/managed')
        plan = self.plan('G-001', 'VERIFYING', branch=branch)
        review_marker = {
            'request_identity': 'review-request', 'owner_oid': published,
            'head': published,
        }
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), published, self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'remote inspection failed', 'VERIFYING')",
                (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO blocked_checkpoints "
                "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                "branch, worktree, accepted_remote_oid) "
                "VALUES (?, 'G-001', 'review-request', 'published', "
                "'review_retry_exhausted', 'review', ?, ?, ?)",
                (self.repo, branch, str(worktree), published),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation='commit',
                expected_head=published, target_base_oid='base',
                observed_remote_oid=published,
                snapshot={'published_review_resume': review_marker,
                          'before_head': published,
                          'commit_message': 'Worker correction',
                          'commit_tree_oid': 'c' * 40},
            )
        current = SimpleNamespace(
            head=corrected, branch=branch, operations={}, dirty={}, index={},
            shared_metadata_complete=True, refs={
                f'refs/heads/{branch}': (corrected, ''),
                f'refs/remotes/origin/{branch}': (published, ''),
            },
        )
        commit = {'parent': published, 'tree': 'c' * 40}

        def git(_repo, *args, **_kwargs):
            if args[:3] == ('show', '-s', '--format=%P'):
                return SimpleNamespace(stdout=commit['parent'] + '\n')
            if args[:3] == ('show', '-s', '--format=%B'):
                return SimpleNamespace(stdout='Worker correction\n')
            if args[:1] == ('rev-parse',):
                return SimpleNamespace(stdout=commit['tree'] + '\n')
            raise AssertionError(args)

        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid',
                              return_value=published) as remote,
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, 'git', side_effect=git),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=published) as owner,
        ):
            commit['parent'] = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            commit['parent'] = published
            commit['tree'] = 'd' * 40
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            commit['tree'] = 'c' * 40
            remote.return_value = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            remote.return_value = published
            owner.return_value = None
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            owner.return_value = published
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            self.assertEqual(owner.call_args.kwargs['before_head'], corrected)
            execution = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            )
            pending = execution.get(self.repo, 'G-001')
            self.assertEqual(pending.operation, 'commit')
            self.assertNotIn('blocked_resume', pending.snapshot)
            execution.record(
                self.repo, 'G-001', expected_generation=pending.generation,
                operation=None, expected_head=corrected,
                target_base_oid='base', observed_remote_oid=published,
                snapshot=pending.snapshot,
            )
            with self.connection:
                self.connection.execute(
                    "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?",
                    (self.repo,),
                )
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES (?, 'project', 'publication_ownership_uncertain', "
                    "'G-001', 'remote inspection failed again', 'VERIFYING')",
                    (self.repo,),
                )
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['published_review_resume'], review_marker)
        self.assertNotIn('blocked_resume', saved.snapshot)

    def test_published_review_release_seals_owner_without_rebase_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            branch, oid = 'loop-manager/G-001', 'a' * 40
            snapshot = loop_manager.GitSnapshot(
                head=oid, branch=branch, index={}, dirty={}, refs={
                    f'refs/heads/{branch}': (oid, ''),
                    f'refs/remotes/origin/{branch}': (oid, ''),
                }, worktrees={}, config={}, operations={},
            )
            self.plan('G-001', 'IMPLEMENTING', branch=branch)
            with self.connection:
                self.connection.execute(
                    "UPDATE exec_plans SET worktree = ?, publication_remote_oid = NULL, "
                    "review_result = 'VERDICT: FAIL', agent_result_snapshot = ? "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (str(worktree), snapshot.serialize(), self.repo),
                )
                scoped_blocking.record_plan_block(
                    self.connection, self.repo, 'G-001',
                    BlockReasonCode.REVIEW_RETRY_EXHAUSTED, 'review failure', 'old',
                )
                self.connection.execute(
                    "UPDATE blocked_checkpoints SET request_identity = ?, "
                    "phase = 'published', "
                    "result = 'published', branch = ?, worktree = ?, "
                    "reason_code = 'review_retry_exhausted', published_oid = ?, "
                    "accepted_remote_oid = ? WHERE repo = ? AND plan_id = 'G-001'",
                    (blocked_checkpoint_state.request_identity(
                        self.repo, 'G-001', 'review_retry_exhausted',
                        'review failure', branch, str(worktree), 'old',
                    ), branch, str(worktree), oid, oid, self.repo),
                )
            plan = self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (self.repo,),
            ).fetchone()
            with (
                mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
                mock.patch.object(loop_manager, 'validate_managed_worktree'),
                mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch', return_value=False),
                mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
                mock.patch.object(loop_manager, 'remote_branch_oid', return_value=oid),
                mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
                mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                                  return_value=oid) as owner,
                mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(
                    stdout=f'{oid}\trefs/heads/{branch}\n', returncode=0,
                )),
            ):
                self.connection.execute(
                    "UPDATE blocked_checkpoints SET request_identity = 'wrong' "
                    "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
                )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                self.connection.execute(
                    "UPDATE blocked_checkpoints SET request_identity = ? "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (blocked_checkpoint_state.request_identity(
                        self.repo, 'G-001', 'review_retry_exhausted',
                        'review failure', branch, str(worktree), 'old',
                    ), self.repo),
                )
                owner.return_value = None
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                owner.return_value = oid
                self.assertTrue(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
            saved = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).get(self.repo, 'G-001')
            self.assertEqual(saved.snapshot['published_review_resume']['owner_oid'], oid)
            self.assertNotIn('blocked_resume', saved.snapshot)
            self.assertEqual(self.connection.execute(
                "SELECT publication_remote_oid FROM exec_plans WHERE repo = ? "
                "AND plan_id = 'G-001'", (self.repo,),
            ).fetchone()[0], oid)
            self.assertEqual(self.connection.execute(
                "SELECT publication_remote_oid FROM projects WHERE repo = ?",
                (self.repo,),
            ).fetchone()[0], oid)

    def test_project_ownership_block_recovers_unrebased_review_owner(self):
        branch, oid, worktree = 'loop-manager/G-001', 'a' * 40, Path('/managed')
        plan = self.plan('G-001', 'IMPLEMENTING', branch=branch)
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), oid, self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'uncertain', 'WORKING')", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO blocked_checkpoints "
                "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                "branch, worktree, accepted_remote_oid) "
                "VALUES (?, 'G-001', 'request', 'published', "
                "'review_retry_exhausted', 'review', ?, ?, ?)",
                (self.repo, branch, str(worktree), oid),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation=None,
                expected_head=oid, target_base_oid=None,
                observed_remote_oid=oid,
                snapshot={'reactivated_from_head': oid},
            )
        current = SimpleNamespace(
            head=oid, branch=branch, operations={}, dirty={}, index={},
            shared_metadata_complete=True, refs={
                f'refs/heads/{branch}': (oid, ''),
                f'refs/remotes/origin/{branch}': (oid, ''),
            },
        )
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value=oid) as remote,
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=oid),
        ):
            remote.return_value = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            remote.return_value = oid
            current.refs[f'refs/heads/{branch}'] = ('other-local', '')
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            current.refs[f'refs/heads/{branch}'] = (oid, '')
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            first = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).get(self.repo, 'G-001')
            with self.connection:
                self.connection.execute(
                    "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?",
                    (self.repo,),
                )
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES (?, 'project', 'publication_ownership_uncertain', "
                    "'G-001', 'uncertain again', 'WORKING')", (self.repo,),
                )
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertEqual(saved.generation, first.generation)
        self.assertNotIn('blocked_resume', saved.snapshot)
        self.assertEqual(saved.snapshot['published_review_resume'], {
            'request_identity': 'request', 'owner_oid': oid, 'head': oid,
        })

    def test_published_review_checkpoint_reconciles_before_legacy_count_release(self):
        self.plan('G-001', 'IMPLEMENTING', branch='loop-manager/G-001')
        loop_manager.runtime_state.RuntimeStateRepository(self.connection).block_plan(
            self.repo, 'G-001', reason_code='review_retry_exhausted',
            evidence='prior issue', release_fingerprint='prior-input',
            clear_projection=True,
        )
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection,
        )
        checkpoint.finish_published(checkpoint.get(self.repo, 'G-001'), 'published-oid')

        with (
            mock.patch.object(loop_manager, '_reconcile_published_blocked_checkpoint',
                              return_value='blocked') as reconcile,
            mock.patch.object(loop_manager, '_resume_legacy_review_count_block',
                              return_value=True) as release,
        ):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
            ))
            reconcile.assert_called_once()
            release.assert_not_called()
            reconcile.return_value = 'unchanged'
            self.assertTrue(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
            ))
            release.assert_called_once()

    def test_legacy_review_count_block_releases_only_owned_non_rebase_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            snapshot = loop_manager.GitSnapshot(
                head='owned-head', branch='loop-manager/G-001',
                index={}, dirty={}, refs={}, worktrees={}, config={}, operations={},
            )
            self.plan('G-001', 'IMPLEMENTING', branch='loop-manager/G-001')
            with self.connection:
                self.connection.execute(
                    "UPDATE exec_plans SET worktree = ?, worker_session_id = 'worker-session', "
                    "reviewer_session_id = 'review-session', review_result = 'VERDICT: FAIL', "
                    "agent_result_snapshot = ?, "
                    "review_failure_history = '{\"old\":{\"occurrences\":4}}' "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (str(worktree), snapshot.serialize(), self.repo),
                )
                scoped_blocking.record_plan_block(
                    self.connection, self.repo, 'G-001',
                    BlockReasonCode.REVIEW_RETRY_EXHAUSTED, 'old count limit', 'old',
                )
                checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
                    self.connection,
                )
                checkpoint.finish_unpublished(
                    checkpoint.get(self.repo, 'G-001'), 'local only', retryable=False,
                )
            plan = self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (self.repo,),
            ).fetchone()
            loop_manager.hydrate_execplan_project(self.connection, self.repo, plan)
            with self.connection:
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence) "
                    "VALUES (?, 'project', 'repository_state_unsafe', 'G-001', 'unsafe')",
                    (self.repo,),
                )
            with (
                mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
                mock.patch.object(loop_manager, 'validate_managed_worktree'),
                mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch', return_value=True) as rebase,
                mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot) as captured,
                mock.patch.object(loop_manager, 'remote_branch_oid', return_value=None),
                mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(
                    stdout='', returncode=0,
                )) as git_effect,
                mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            ):
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                with self.connection:
                    self.connection.execute(
                        "DELETE FROM project_blocks WHERE repo = ?", (self.repo,),
                    )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                rebase.return_value = False
                captured.return_value = loop_manager.GitSnapshot(
                    **{**snapshot.__dict__, 'dirty': {'new.py': (('unstaged',), 'changed')}}
                )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                captured.return_value = snapshot
                git_effect.return_value = SimpleNamespace(
                    stdout='other-head\trefs/heads/loop-manager/G-001\n',
                    returncode=0,
                )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                git_effect.return_value = SimpleNamespace(stdout='', returncode=0)
                self.assertTrue(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
            saved = self.connection.execute(
                "SELECT status, worker_session_id, reviewer_session_id, "
                "review_result, review_failure_history FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            ).fetchone()
            self.assertEqual(tuple(saved), (
                'IMPLEMENTING', 'worker-session', 'review-session',
                'VERDICT: FAIL', '{"old":{"occurrences":4}}',
            ))
            self.assertIsNone(scoped_blocking.plan_block(
                self.connection, self.repo, 'G-001',
            ))
            self.assertIsNotNone(self.connection.execute(
                "SELECT final_evidence FROM recovery_records "
                "WHERE repo = ? AND subject = 'G-001'", (self.repo,),
            ).fetchone()['final_evidence'])
            self.assertEqual(self.connection.execute(
                "SELECT phase FROM blocked_checkpoints "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            ).fetchone()['phase'], 'unpublished')

    def test_planning_attempt_keeps_slot_across_multiple_plan_blocks_and_pr_close(self):
        self.plan('G-001', 'PLAN_BLOCKED')
        self.plan('G-002', 'PLAN_BLOCKED')
        self.plan('G-003', 'PR_OPEN', branch='loop/G-003',
                  pr_url='https://example.test/pr/3')
        with self.connection:
            self.connection.execute(loop_manager.GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
            self.connection.execute(
                "INSERT INTO general_planning_publications "
                "(repo, stage, base_sha, commit_sha, snapshot_token, "
                "specification_fingerprint, planning_fingerprint, manifest, "
                "manifest_hash) VALUES (?, 'prepared', 'base', 'commit', "
                "'snapshot', 'spec', 'input', '{}', 'hash')", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO maintenance_cycles "
                "(repo, phase, cycle_base_sha, publication_stage, revision) "
                "VALUES (?, 'assessment', 'base', 'prepared', 3)", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PLANNING', active_sha = 'base', "
                "planning_attempt_fingerprint = 'attempt', "
                "planning_workspace_owned = 1, worktree = '/planning', "
                "planner_session_id = 'planner', planning_retry_count = 2, "
                "agent_role = 'planner', agent_snapshot = 'snapshot', "
                "planning_result_pending_validation = 1, "
                "planning_result = 'saved' WHERE repo = ?", (self.repo,),
            )
        before = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone())
        publication_before = dict(self.connection.execute(
            "SELECT * FROM general_planning_publications WHERE repo = ?",
            (self.repo,),
        ).fetchone())
        cycle_before = dict(self.connection.execute(
            "SELECT * FROM maintenance_cycles WHERE repo = ?", (self.repo,),
        ).fetchone())
        with mock.patch.object(loop_manager, 'hydrate_execplan_project') as hydrate:
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
            ))
        hydrate.assert_not_called()
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={
            'state': 'CLOSED', 'url': 'https://example.test/pr/3',
            'headRefName': 'loop/G-003', 'headRefOid': 'head',
        }):
            loop_manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM general_planning_publications WHERE repo = ?",
            (self.repo,),
        ).fetchone()), publication_before)
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM maintenance_cycles WHERE repo = ?", (self.repo,),
        ).fetchone()), cycle_before)
        statuses = dict(self.connection.execute(
            "SELECT plan_id, status FROM exec_plans WHERE repo = ?", (self.repo,),
        ).fetchall())
        self.assertEqual(statuses, {
            'G-001': 'PLAN_BLOCKED', 'G-002': 'PLAN_BLOCKED',
            'G-003': 'PLAN_BLOCKED',
        })
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={
            'state': 'OPEN', 'url': 'https://example.test/pr/3',
            'headRefName': 'loop/G-003', 'headRefOid': 'head',
        }):
            loop_manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        ).fetchone()[0], 'PR_OPEN')

    def test_plan_project_block_releases_only_after_validation_without_clearing_planning(self):
        plan = self.plan('G-001', 'PLAN_BLOCKED', branch='loop/G-001')
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/managed/G-001' "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', "
                "planning_error = 'planner failed', "
                "planning_attempt_fingerprint = 'attempt', "
                "worktree = '/planning', agent_snapshot = 'snapshot' "
                "WHERE repo = ?", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, "
                "evidence, previous_phase) VALUES (?, 'project', "
                "'workspace_ownership_uncertain', 'G-001', 'blocked', 'PLANNING')",
                (self.repo,),
            )
        before = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone())
        with mock.patch.object(
            loop_manager, 'validate_execplan_workspace_identity',
            side_effect=loop_manager.WorkspaceOwnershipError('not owned'),
        ):
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'), \
                mock.patch.object(loop_manager, 'validate_managed_worktree'), \
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch',
                                  return_value=False):
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        after = self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()
        self.assertEqual(after['phase'], 'PLANNING')
        self.assertEqual(after['planning_error'], 'planner failed')
        self.assertEqual(after['last_error'], 'planner failed')
        for field in ('planning_attempt_fingerprint', 'worktree', 'agent_snapshot'):
            self.assertEqual(after[field], before[field])
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(plan['plan_id'], 'G-001')

    def test_merged_pr_records_plan_and_defers_cleanup_during_planning(self):
        self.plan('G-003', 'PR_OPEN', branch='loop/G-003',
                  pr_url='https://example.test/pr/3')
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/managed/G-003' "
                "WHERE repo = ? AND plan_id = 'G-003'", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PLANNING', worktree = '/planning', "
                "planning_attempt_fingerprint = 'attempt', "
                "planning_result_completed = 1, "
                "agent_snapshot = 'planner-snapshot' WHERE repo = ?", (self.repo,),
            )
        before = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone())
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={
            'state': 'MERGED', 'url': 'https://example.test/pr/3',
            'headRefName': 'loop/G-003', 'headRefOid': 'head',
        }), mock.patch.object(loop_manager, 'remove_cleanup_worktree') as cleanup, \
                mock.patch.object(loop_manager, '_recorded_base_fetch'):
            observed = loop_manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertTrue(observed.merged)
        cleanup.assert_not_called()
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        ).fetchone()[0], 'MERGED')
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM cleanup_pending WHERE repo = ? AND worktree = '/managed/G-003'",
            (self.repo,),
        ).fetchone())

    def test_missing_merged_worktree_uses_managed_cleanup_evidence_without_planning(self):
        self.plan('G-003', 'PR_OPEN', branch='loop/G-003')
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / 'missing'
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ? "
                "WHERE repo = ? AND plan_id = 'G-003'",
                (str(worktree), self.repo),
            )
            plan = self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
                (self.repo,),
            ).fetchone()
            with mock.patch.object(
                loop_manager, '_assess_missing_managed_cleanup',
                return_value=loop_manager.CleanupAssessment('missing'),
            ) as assess:
                result = loop_manager.complete_merged_execplan_worktree(
                    self.connection, Path(self.repo), self.repo,
                    Path('/state'), plan, 'https://example.test/pr/3', 'head',
                )
        assess.assert_called_once_with(Path('/state'), Path(self.repo), worktree)
        self.assertEqual(result.status, 'missing')
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        ).fetchone()[0], 'MERGED')

    def test_conflicting_pr_waits_for_planning_slot(self):
        plan = self.plan('G-003', 'PR_OPEN', branch='loop/G-003')
        self.connection.execute(
            "UPDATE projects SET phase = 'PLANNING', "
            "planning_attempt_fingerprint = 'attempt' WHERE repo = ?",
            (self.repo,),
        )
        self.connection.commit()
        observation = loop_manager.WaitingPullRequestObservation(
            ((plan, {'state': 'OPEN', 'mergeable': 'CONFLICTING'}),), False,
        )
        with mock.patch.object(loop_manager, 'hydrate_execplan_project') as hydrate:
            result = loop_manager.select_waiting_pull_request_conflict(
                self.connection, Path(self.repo), Path('/state'),
                self.repo, observation,
            )
        hydrate.assert_not_called()
        self.assertFalse(result.conflict_started)
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()[0], 'PLANNING')

    def test_releasing_notified_plan_block_rolls_back_notice_cancellation(self):
        self.plan("G-001", "PLAN_BLOCKED")
        notifications.configure_session(self.connection, enabled=True)
        with self.connection:
            self.connection.execute(
                "INSERT INTO execplan_blocks "
                "(repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-001', 'plan', 'user_decision_required', 'decision')",
                (self.repo,),
            )
            notifications.enqueue_current_project_events(self.connection, self.repo)
        event = self.connection.execute(
            "SELECT event_key FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()
        self.assertIsNotNone(event)

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.connection:
                scoped_blocking.clear_plan_block(self.connection, self.repo, "G-001")
                raise RuntimeError("rollback")

        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM execplan_blocks WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone())
        self.assertIsNone(self.connection.execute(
            "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
            (event[0],),
        ).fetchone()[0])
        self.assertIsNone(self.connection.execute(
            "SELECT released_at FROM notified_block_incidents WHERE event_key = ?",
            (event[0],),
        ).fetchone()[0])
    def test_requeued_plan_keeps_only_verified_checkpoint_remote_oid(self):
        self.plan('G-081', 'PLAN_BLOCKED', branch='loop/G-081')
        loop_manager._requeue_blocked_execplan(
            self.connection, self.repo, 'G-081',
            publication_remote_oid='a' * 40,
        )
        row = self.connection.execute(
            "SELECT status, publication_remote_oid FROM exec_plans WHERE repo = ? AND plan_id = 'G-081'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(row['status'], 'READY')
        self.assertEqual(row['publication_remote_oid'], 'a' * 40)

    def test_project_publication_block_recovers_only_matching_owned_rebase(self):
        plan = self.plan('G-081', 'IMPLEMENTING', branch='loop/G-081')
        marker = {'owner_oid': 'a' * 40, 'before_head': 'before',
                  'after_head': 'after', 'target_base_oid': 'base'}
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        checkpoint.record(
            self.repo, 'G-081', expected_generation=0, operation=None,
            expected_head='after', target_base_oid='base',
            observed_remote_oid='a' * 40,
            snapshot={'blocked_resume': marker},
        )
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/managed', worker_session_id = 'session' "
                "WHERE repo = ? AND plan_id = 'G-081'", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = 'loop/G-081', worktree = '/managed' WHERE repo = ?",
                (plan['path'], self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', 'G-081', 'old error', 'WORKING')",
                (self.repo,),
            )
        current = SimpleNamespace(
            head='after', branch='loop/G-081', operations={}, dirty={},
        )
        with (
            mock.patch.object(loop_manager, '_direct_rebase_history_proven', return_value=True),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value='a' * 40),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value='a' * 40) as owner,
            mock.patch.object(loop_manager.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
        ):
            owner.return_value = None
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            self.assertIsNotNone(scoped_blocking.project_block(
                self.connection, self.repo,
            ))
            owner.reset_mock()
            owner.return_value = 'a' * 40
            recovered = loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertTrue(recovered)
        owner.assert_called_once()
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = self.connection.execute(
            "SELECT status, publication_remote_oid, worker_session_id FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-081'", (self.repo,),
        ).fetchone()
        self.assertEqual(tuple(saved), ('IMPLEMENTING', 'a' * 40, 'session'))

        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
                "blocked_reason = 'review_retry_exhausted' "
                "WHERE repo = ? AND plan_id = 'G-081'", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = NULL, "
                "branch = NULL, worktree = NULL WHERE repo = ?", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO execplan_blocks "
                "(repo, plan_id, scope, reason_code, evidence) VALUES "
                "(?, 'G-081', 'plan', 'review_retry_exhausted', 'separate block')",
                (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-081', 'old error', 'PLAN_BLOCKED')", (self.repo,),
            )
        with (
            mock.patch.object(loop_manager, '_direct_rebase_history_proven', return_value=True),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value='a' * 40),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value='a' * 40),
            mock.patch.object(loop_manager.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
        ):
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-081'",
            (self.repo,),
        ).fetchone()[0], 'PLAN_BLOCKED')
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM execplan_blocks WHERE repo = ? AND plan_id = 'G-081'",
            (self.repo,),
        ).fetchone())

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)
        notifications.ensure_schema(self.connection)
        self.repo = "/projects/scoped"
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (self.repo,),
        )
        self.connection.commit()

    def plan(
        self,
        plan_id,
        status="READY",
        *,
        priority="normal",
        dependencies=(),
        branch=None,
        pr_url=None,
    ):
        path = f"docs/plans/[{plan_id}] Work.md"
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, branch, pr_url, workspace_owned
            ) VALUES (?, ?, ?, ?, '[]', ?, ?, 'hash', ?, ?, ?, 1)
            """,
            (
                self.repo,
                plan_id,
                path,
                f"Work {plan_id}",
                json.dumps(list(dependencies)),
                priority,
                status,
                branch,
                pr_url,
            ),
        )
        self.connection.commit()
        return self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (self.repo, plan_id),
        ).fetchone()

    def test_initialized_worker_queries_do_not_reenter_schema_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            parent = loop_manager.connect_database(state, create=True)
            self.addCleanup(parent.close)
            parent.execute("BEGIN IMMEDIATE")

            def query_worker(repo):
                connection = loop_manager.connect_database(state, run_migrations=False)
                try:
                    connection.execute("PRAGMA busy_timeout = 100")
                    statements = []
                    connection.set_trace_callback(statements.append)
                    self.assertIsNone(loop_manager.active_execplan(connection, repo))
                    self.assertEqual(loop_manager.plan_blocked_execplans(connection, repo), ())
                    self.assertEqual(loop_manager.merge_wait_execplans(connection, repo), ())
                    self.assertEqual(loop_manager.compact_execplan_catalog(connection, repo), "[]")
                    self.assertIsNone(loop_manager.catalog_schedule_decision(connection, repo).active)
                    self.assertFalse(any(
                        sql.lstrip().upper().startswith(("CREATE", "DROP", "ALTER"))
                        and any(name in sql.lower() for name in (
                            "exec_plans", "one_active_execplan_per_project",
                            "execplan_blocks", "project_blocks", "guidance_approvals",
                        ))
                        for sql in statements
                    ), statements)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(query_worker, ("repo-a", "repo-b")))
            parent.rollback()

    def test_reopened_conflict_precedes_independent_ready(self):
        plan = self.plan("G-001", "PR_OPEN", branch="loop-manager/G-001")
        self.plan("G-002", priority="critical")
        loop_manager._block_closed_pull_request(self.connection, self.repo, plan, "https://pr/1", "a" * 40)
        opened = {"state": "OPEN", "url": "https://pr/1", "headRefOid": "a" * 40, "mergeable": "CONFLICTING"}

        def start_recovery(connection, _root, _state, repo, project, _pr):
            self.assertIn("[G-001]", project["plan_path"])
            self.assertEqual(loop_manager.project_execplan(connection, repo)["status"], "PR_OPEN")
            loop_manager.update_project(connection, repo, phase="WORKING", pr_conflict_recovery=1)

        with mock.patch("loop_manager.pr_for_branch", return_value=opened), mock.patch(
            "loop_manager.reconcile_existing_pull_request", side_effect=start_recovery
        ) as recover:
            result = loop_manager.synchronize_waiting_pull_requests(self.connection, Path(self.repo), Path("/state"), self.repo)
        recover.assert_called_once()
        self.assertTrue(result.conflict_started)
        decision = loop_manager.catalog_schedule_decision(self.connection, self.repo)
        self.assertEqual(decision.active["plan_id"], "G-001")
        self.assertIsNone(decision.runnable_ready)

    def test_cleanup_project_block_stops_later_merge_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            for plan_id in ("G-001", "G-002"):
                self.plan(plan_id, "PR_OPEN", branch=f"loop-manager/{plan_id}")
            self.connection.execute("UPDATE exec_plans SET workspace_owned = 0, worktree = ?", (directory,))
            self.connection.commit()
            merged = {"state": "MERGED", "url": "https://pr/1", "headRefOid": "a" * 40}
            with mock.patch("loop_manager.pr_for_branch", return_value=merged) as read, mock.patch("loop_manager._recorded_base_fetch", return_value="a" * 40) as fetched, mock.patch(
                "loop_manager.complete_merged_execplan_worktree", wraps=loop_manager.complete_merged_execplan_worktree
            ) as cleanup:
                loop_manager.synchronize_waiting_pull_requests(self.connection, Path(self.repo), Path(directory), self.repo)
            self.assertEqual(read.call_count, 2)
            cleanup.assert_called_once()
            fetched.assert_called_once_with(
                Path(self.repo), Path(directory), "main", owner="project",
            )
            self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
            statuses = dict(self.connection.execute("SELECT plan_id, status FROM exec_plans"))
            self.assertEqual(statuses, {"G-001": "MERGED", "G-002": "PR_OPEN"})

    def test_reason_mapping_and_atomic_plan_project_records(self):
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
                "same verify fingerprint",
                "verify-fingerprint",
            )

        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        block = self.connection.execute(
            "SELECT * FROM execplan_blocks WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(
            (block["scope"], block["reason_code"], block["release_fingerprint"]),
            ("plan", "verify_retry_exhausted", "verify-fingerprint"),
        )

        with self.assertRaises(ValueError):
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.PULL_REQUEST_CLOSED,
                "wrong scope",
                None,
                plan_id="G-001",
                previous_phase=None,
            )

        self.plan("G-002", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.SHARED_REF_MUTATION,
                "refs/heads/main changed",
                "repository-fingerprint",
                plan_id="G-002",
                previous_phase="WORKING",
            )
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        active = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
        self.assertEqual(active["status"], "IMPLEMENTING")
        scoped_blocking.validate_records(self.connection, self.repo)

        expected_plan_reasons = {
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
            BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
            BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
            BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
            BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
            BlockReasonCode.USER_DECISION_REQUIRED,
            BlockReasonCode.PULL_REQUEST_CLOSED,
            BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
            BlockReasonCode.PLAN_INPUT_INVALID,
            BlockReasonCode.WORKER_OUTPUT_INVALID,
        }
        self.assertEqual(
            {
                reason
                for reason in BlockReasonCode
                if scoped_blocking.reason_scope(reason) is BlockScope.PLAN
            },
            expected_plan_reasons,
        )

    def test_waiting_rows_do_not_occupy_the_single_mutation_slot(self):
        self.plan("G-001", "PR_OPEN", branch="loop-manager/G-001")
        self.plan("G-002", "PR_OPEN", branch="loop-manager/G-002")
        self.plan("G-003", "IMPLEMENTING")
        with self.assertRaises(sqlite3.IntegrityError):
            self.plan("G-004", "VERIFYING")

        self.connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED' "
            "WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        )
        self.connection.execute(
            "INSERT INTO execplan_blocks "
            "(repo, plan_id, scope, reason_code, evidence) "
            "VALUES (?, 'G-003', 'plan', 'plan_input_invalid', 'decision')",
            (self.repo,),
        )
        self.plan("G-005", "REVIEWING")
        self.assertEqual(
            loop_manager.active_execplan(self.connection, self.repo)["plan_id"],
            "G-005",
        )

    def test_dependency_gate_selects_independent_ready_while_others_wait(self):
        blocked = self.plan("G-001", "IMPLEMENTING", priority="critical")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.USER_DECISION_REQUIRED,
                "decision",
                "decision-fingerprint",
            )
        self.plan("G-002", dependencies=("G-001",), priority="critical")
        self.plan("G-003", priority="normal")
        self.plan(
            "G-004",
            "PR_OPEN",
            priority="high",
            branch="loop-manager/G-004",
        )
        self.plan("G-005", priority="high")

        decision = loop_manager.catalog_schedule_decision(
            self.connection, self.repo
        )
        self.assertIsNone(decision.active)
        self.assertTrue(decision.has_plan_blocked)
        self.assertTrue(decision.has_pr_open)
        self.assertEqual(decision.runnable_ready["plan_id"], "G-005")
        self.assertNotEqual(blocked["plan_id"], decision.runnable_ready["plan_id"])

        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                "cannot determine safe repository state",
                None,
                plan_id="G-001",
                previous_phase=None,
            )
        blocked_decision = loop_manager.catalog_schedule_decision(
            self.connection, self.repo
        )
        self.assertTrue(blocked_decision.project_blocked)
        self.assertIsNone(blocked_decision.runnable_ready)

    def test_agent_scope_ignores_codex_turn_refs_but_structures_real_boundaries(self):
        baseline = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="loop-manager/G-001",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        turn_ref = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {"refs/codex/turn-diffs/1": ("b" * 40, "")},
            }
        )
        loop_manager.validate_agent_scope(
            "reviewer", Path(self.repo), baseline, turn_ref
        )

        shared_ref = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {"refs/tags/agent-created": ("b" * 40, "")},
            }
        )
        with self.assertRaises(loop_manager.AgentScopeError) as raised:
            loop_manager.validate_agent_scope(
                "reviewer", Path(self.repo), baseline, shared_ref
            )
        self.assertEqual(
            raised.exception.reason_code,
            BlockReasonCode.SHARED_REF_MUTATION,
        )
        self.assertIs(
            scoped_blocking.reason_scope(raised.exception.reason_code),
            BlockScope.PROJECT,
        )

        local_change = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "dirty": {"module.py": (("unstaged",), "changed")},
            }
        )
        with self.assertRaises(loop_manager.AgentScopeError) as raised:
            loop_manager.validate_agent_scope(
                "reviewer", Path(self.repo), baseline, local_change
            )
        self.assertEqual(
            raised.exception.reason_code,
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
        )

    def test_existing_turn_diff_project_block_releases_automatically(self):
        baseline = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="loop-manager/G-001",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        current = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {
                    "refs/codex/turn-diffs/captures/1/session/base": (
                        "b" * 40,
                        "",
                    )
                },
            }
        )
        self.plan(
            "G-001",
            "IMPLEMENTING",
            branch="loop-manager/G-001",
        )
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/state/G-001', "
                "agent_role = 'worker', agent_snapshot = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (baseline.serialize(), self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'WORKING', "
                "plan_path = 'docs/plans/[G-001] Work.md' WHERE repo = ?",
                (self.repo,),
            )
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.TURN_DIFF_REF_MUTATION,
                "worker Git 범위 위반: Codex turn ref changed",
                "repository-fingerprint",
                plan_id="G-001",
                previous_phase="WORKING",
            )

        with mock.patch(
            "loop_manager.validate_execplan_workspace_identity"
        ), mock.patch("loop_manager.validate_managed_worktree"), mock.patch(
            "loop_manager.capture_git_snapshot", return_value=current
        ):
            released = loop_manager.resume_project_block_if_safe(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )

        self.assertTrue(released)
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertEqual(project["phase"], "WORKING")

    def test_all_open_prs_are_read_before_one_conflict_is_selected(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            priority="normal",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        self.plan(
            "G-002",
            "PR_OPEN",
            priority="high",
            branch="loop-manager/G-002",
            pr_url="https://pr/2",
        )
        pulls = {
            "loop-manager/G-001": {
                "url": "https://pr/1",
                "state": "OPEN",
                "headRefName": "loop-manager/G-001",
                "headRefOid": "1" * 40,
                "mergeable": "UNKNOWN",
            },
            "loop-manager/G-002": {
                "url": "https://pr/2",
                "state": "OPEN",
                "headRefName": "loop-manager/G-002",
                "headRefOid": "2" * 40,
                "mergeable": "CONFLICTING",
            },
        }
        observed = []

        def lookup(_repo, branch, _url=None):
            observed.append(branch)
            return pulls[branch]

        with mock.patch("loop_manager.pr_for_branch", side_effect=lookup), mock.patch(
            "loop_manager.reconcile_existing_pull_request", return_value=None
        ) as reconcile:
            result = loop_manager.synchronize_waiting_pull_requests(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )

        self.assertEqual(
            observed,
            ["loop-manager/G-001", "loop-manager/G-002"],
        )
        self.assertTrue(result.conflict_started)
        self.assertEqual(reconcile.call_count, 1)
        mirrored = self.connection.execute(
            "SELECT plan_path FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertIn("[G-002]", mirrored["plan_path"])

    def test_closed_and_reopened_pr_only_updates_its_plan(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        self.plan(
            "G-002",
            "PR_OPEN",
            branch="loop-manager/G-002",
            pr_url="https://pr/2",
        )

        def pulls(state):
            return {
                "loop-manager/G-001": {
                    "url": "https://pr/1",
                    "state": state,
                    "headRefOid": "1" * 40,
                    "mergeable": "UNKNOWN",
                },
                "loop-manager/G-002": {
                    "url": "https://pr/2",
                    "state": "OPEN",
                    "headRefOid": "2" * 40,
                    "mergeable": "UNKNOWN",
                },
            }

        closed = pulls("CLOSED")
        with mock.patch(
            "loop_manager.pr_for_branch",
            side_effect=lambda _repo, branch, _url=None: closed[branch],
        ):
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        statuses = {
            row["plan_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                (self.repo,),
            )
        }
        self.assertEqual(statuses, {"G-001": "PLAN_BLOCKED", "G-002": "PR_OPEN"})

        reopened = pulls("OPEN")
        with mock.patch(
            "loop_manager.pr_for_branch",
            side_effect=lambda _repo, branch, _url=None: reopened[branch],
        ):
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        statuses = {
            row["plan_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                (self.repo,),
            )
        }
        self.assertEqual(statuses, {"G-001": "PR_OPEN", "G-002": "PR_OPEN"})

    def test_project_block_allows_pr_read_but_skips_merge_mutation(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                "default worktree changed",
                "git-snapshot",
                plan_id="G-001",
                previous_phase=None,
            )
        merged = {
            "url": "https://pr/1",
            "state": "MERGED",
            "mergedAt": "2026-01-01T00:00:00Z",
            "headRefOid": "1" * 40,
        }
        with mock.patch("loop_manager.pr_for_branch", return_value=merged) as read_pr, mock.patch(
            "loop_manager.git"
        ) as git, mock.patch(
            "loop_manager.complete_merged_execplan_worktree"
        ) as complete:
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        read_pr.assert_called_once()
        git.assert_not_called()
        complete.assert_not_called()
        status = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()[0]
        self.assertEqual(status, "PR_OPEN")

    def test_selected_pr_can_escalate_without_occupying_active_status(self):
        plan = self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        loop_manager.hydrate_execplan_project(self.connection, self.repo, plan)
        with mock.patch(
            "loop_manager._revision_execplan_input_fingerprint",
            return_value="repository-fingerprint",
        ):
            loop_manager.block_active_execplan_from_revision(
                self.connection,
                Path(self.repo),
                self.repo,
                "refs/remotes/origin/main",
                "shared refs changed",
                reason=BlockReasonCode.SHARED_REF_MUTATION,
            )
        stored = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        project_block = scoped_blocking.project_block(self.connection, self.repo)
        self.assertEqual(stored["status"], "PR_OPEN")
        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
        self.assertEqual(project_block["plan_id"], "G-001")
        self.assertEqual(project_block["reason_code"], "shared_ref_mutation")

        with mock.patch(
            "loop_manager.validate_execplan_workspace_identity"
        ), mock.patch("loop_manager.validate_managed_worktree"):
            self.assertTrue(
                loop_manager.resume_project_block_if_safe(
                    self.connection,
                    Path(self.repo),
                    Path("/state"),
                    self.repo,
                )
            )
        released_project = self.connection.execute(
            "SELECT phase, plan_path FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertEqual(tuple(released_project), (None, None))

    def test_project_release_restores_phase_and_preserves_plan_block(self):
        snapshot = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="main",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        blocked = self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.USER_DECISION_REQUIRED,
                "plan decision",
                "plan-fingerprint",
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PLANNING', worktree = ?, "
                "agent_role = 'planner', agent_snapshot = ? WHERE repo = ?",
                (self.repo, snapshot.serialize(), self.repo),
            )
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                "default worktree changed",
                "repository-fingerprint",
                plan_id=None,
                previous_phase="PLANNING",
            )
        with mock.patch("loop_manager.capture_git_snapshot", return_value=snapshot):
            released = loop_manager.resume_project_block_if_safe(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        retained = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (self.repo, blocked["plan_id"]),
        ).fetchone()
        self.assertTrue(released)
        self.assertEqual(project["phase"], "PLANNING")
        self.assertEqual(retained["status"], "PLAN_BLOCKED")
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))

    def test_block_notifications_distinguish_new_fingerprint_and_scope(self):
        notifications.configure_session(self.connection, enabled=True)
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "first evidence",
                "one",
            )
            checkpoint_repository = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    self.connection
                )
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(self.repo, "G-001"),
                "first local-only result",
                retryable=False,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "updated evidence",
                "two",
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(self.repo, "G-001"),
                "updated local-only result",
                retryable=False,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
        rows = self.connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(
            (payload["scope"], payload["reason_code"], payload["execplan_id"]),
            ("plan", "review_retry_exhausted", "G-001"),
        )

        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
                "shared config changed",
                "config-fingerprint",
                plan_id="G-001",
                previous_phase=None,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
        project_payload = next(
            parsed
            for parsed in (
                json.loads(row[0])
                for row in self.connection.execute(
                    "SELECT payload FROM notification_outbox "
                    "WHERE event_type = 'blocked'"
                )
            )
            if parsed["scope"] == "project"
        )
        self.assertEqual(
            (
                project_payload["scope"],
                project_payload["reason_code"],
                project_payload["execplan_id"],
                project_payload["execplan_title"],
            ),
            ("project", "shared_git_config_mutation", "G-001", "Work G-001"),
        )
        self.assertEqual(
            project_payload["paths"],
            [],
        )

    def test_plan_block_is_waiting_and_registry_removal_is_rejected(self):
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.PLAN_INPUT_INVALID,
                "invalid plan input",
                "input-fingerprint",
            )
        with mock.patch("loop_manager._run_project_step", return_value=1):
            result = loop_manager.run_project_result(
                self.connection,
                Path("/state"),
                self.repo,
                Path("config.ini"),
                None,
                False,
            )
        self.assertEqual(
            (result.kind, result.exit_code, result.detail),
            (loop_manager.ProjectRunKind.WAITING, 0, "invalid plan input"),
        )
        with self.assertRaises(project_registry.ProjectRegistryError):
            project_registry.remove_project(self.connection, self.repo)

    def test_checkpoint_retry_does_not_occupy_independent_ready_slot(self):
        self.plan("G-001", "IMPLEMENTING", priority="critical")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "review failure",
                "review-fingerprint",
            )
        self.plan("G-002", "READY", priority="normal")
        steps = []

        def project_step(*_args, **_kwargs):
            steps.append(len(steps) + 1)
            if len(steps) == 2:
                self.connection.execute(
                    "UPDATE exec_plans SET status = 'MERGED' "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (self.repo,),
                )
                self.connection.commit()
                return 0
            return 1

        with mock.patch(
            "loop_manager.dispatch_blocked_checkpoints", return_value=0
        ) as checkpoint, mock.patch(
            "loop_manager._run_project_step", side_effect=project_step
        ):
            result = loop_manager.run_project_result(
                self.connection,
                Path("/state"),
                self.repo,
                Path("config.ini"),
                None,
                False,
            )

        self.assertEqual(steps, [1, 2])
        checkpoint.assert_called_once()
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        blocked = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(blocked["status"], "PLAN_BLOCKED")

    def test_legacy_remote_checkpoint_change_prevents_plan_resume(self):
        self.plan(
            "G-001",
            "IMPLEMENTING",
            branch="loop-manager/G-001",
        )
        self.plan("G-002", "READY")
        self.connection.execute(
            "UPDATE exec_plans SET worktree = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("/state/G-001", self.repo),
        )
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.PLAN_INPUT_INVALID,
                "input changed",
                "old-input",
            )
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        )
        repository.finish_published(
            repository.get(self.repo, "G-001"), "a" * 40
        )

        with mock.patch.object(
            loop_manager._BlockedCheckpointAdapter,
            "observe_remote",
            return_value="b" * 40,
        ):
            resumed = loop_manager.resume_plan_blocks(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
                "main",
            )

        self.assertFalse(resumed)
        plan = self.connection.execute(
            "SELECT status, last_error FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIn("소유권", plan["last_error"])
        self.assertEqual(
            loop_manager.catalog_schedule_decision(
                self.connection, self.repo,
            ).runnable_ready["plan_id"],
            "G-002",
        )

    def test_legacy_unscoped_block_is_conservatively_project_blocked(self):
        self.plan("G-001", "READY")
        self.connection.execute(
            "UPDATE exec_plans SET status = 'BLOCKED', last_error = 'legacy text' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        )
        self.connection.execute(
            "UPDATE projects SET phase = 'BLOCKED', plan_path = ? WHERE repo = ?",
            ("docs/plans/[G-001] Work.md", self.repo),
        )
        self.connection.commit()

        loop_manager._ensure_execplans_schema(self.connection)

        project_block = scoped_blocking.project_block(
            self.connection, self.repo
        )
        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(
            project_block["reason_code"], "legacy_unscoped_block"
        )


if __name__ == "__main__":
    unittest.main()


class EvidenceFreeRebaseBlockTests(unittest.TestCase):
    """Real current-state decisions and SQLite transitions; no Git lifecycle."""
    def setUp(self):
        from tests.test_execution_handlers import current_rebase_case
        self.case = current_rebase_case(self)
        self.connection = self.case.fixture.connection
        self.checkpoints = self.case.checkpoint
        self.repo, self.plan_id = 'repo', 'G-047'
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute("UPDATE projects SET planning_attempt_fingerprint = NULL")
        self.connection.execute("UPDATE exec_plans SET workspace_owned = 1")
        self.connection.commit()
        self.is_plan_block = True
        def patch(*args, **kwargs):
            patcher = mock.patch.object(*args, **kwargs)
            value = patcher.start()
            self.addCleanup(patcher.stop)
            return value
        self.factory = patch(loop_manager, '_current_rebase_service',
            side_effect=lambda *_a, **_k: self.case.factory(None))
        self.git_write = patch(loop_manager, 'git',
            side_effect=AssertionError('recovery must not write or invoke Git directly'))
        patch(loop_manager, 'operation_log')

    def block(self, *, project=False, separate=None, reason=None):
        self.is_plan_block = not project
        reason = reason or ('repository_state_unsafe' if project else 'managed_worktree_conflict')
        evidence = (rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC
                    if project else 'G-088 repeated conflict with changed plan inputs')
        with self.connection:
            if project:
                self.connection.execute("UPDATE projects SET phase = 'PROJECT_BLOCKED'")
                self.connection.execute("INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES ('repo', 'project', ?, 'G-047', ?, 'WORKING')", (reason, evidence))
            if not project or separate:
                cause = separate or reason
                self.connection.execute("UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
                    "blocked_reason = ?, last_error = 'original block text', "
                    "blocked_fingerprint = 'old-input'", (cause,))
                self.connection.execute("INSERT INTO execplan_blocks "
                    "(repo, plan_id, scope, reason_code, evidence) "
                    "VALUES ('repo', 'G-047', 'plan', ?, ?)",
                    (cause, 'independent original cause' if separate else evidence))
                if not project:
                    self.connection.execute("UPDATE projects SET phase = 'PLAN_BLOCKED'")
            subject = 'project' if project else 'plan:G-047'
            self.connection.execute("INSERT INTO notified_block_incidents "
                "(repo, subject, block_key, event_key, payload) VALUES ('repo', ?, 'block', 'event', '{}')",
                (subject,))
        return evidence

    def resume(self):
        if self.is_plan_block:
            return loop_manager.resume_plan_blocks(self.connection, Path('/repo'), Path('/state'), 'repo', 'main')
        return loop_manager.resume_project_block_if_safe(self.connection, Path('/repo'), Path('/state'), 'repo')

    def snapshot(self):
        return {table: [dict(row) for row in self.connection.execute('SELECT * FROM ' + table)]
                for table in ('projects', 'exec_plans', 'execplan_blocks', 'project_blocks',
                              'execution_checkpoints', 'recovery_records', 'notified_block_incidents')}

    def pending(self, *, staged=False):
        from dataclasses import replace
        from managed_rebase_validation import FileChange, IndexEntry
        self.case.observed = replace(self.case.observed, head='partial', branch=None,
            branch_ref=('before', ''), registration=('HEAD=partial', 'detached=True'),
            branch_worktrees=(), operations=(('rebase-merge', 'operation'),),
            metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                      ('orig-head', 'before'), ('onto', 'base')),
            index=(IndexEntry('app.py', '100644', 'manual', 0 if staged else 2,
                              'H' if staged else 'M', 0 if staged else 0x2000),),
            staged=(FileChange('app.py', '100644', '100644', 'old', 'manual', 'M'),) if staged else (),
            index_clean=False)

    def test_changed_inputs_and_session_resume_manual_completion_without_recovery_history(self):
        self.block()
        self.connection.execute("UPDATE exec_plans SET worker_session_id = 'replacement-session', "
                                "worker_input_token = 'obsolete', review_failure_history = '{\"issue\":2}'")
        self.connection.commit()
        before = self.checkpoints.get('repo', 'G-047')
        self.assertTrue(self.resume())
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['worker_session_id'], 'replacement-session')
        self.assertEqual(row['review_failure_history'], '{"issue":2}')
        for field in ('worker_result', 'worker_input_token', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[field], field)
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        adopted = self.checkpoints.get('repo', 'G-047')
        self.assertIsNone(adopted.operation)
        self.assertEqual(adopted.snapshot['failure'], before.snapshot['failure'])
        self.assertTrue(adopted.current_rebase_acceptance())
        self.assertEqual(adopted.snapshot['rebase_revalidation_audit'][-1]['released_rebase_block']['evidence'],
                         'G-088 repeated conflict with changed plan inputs')
        self.assertFalse(self.resume())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), adopted)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM notified_block_incidents').fetchone()[0], 1)
        self.git_write.assert_not_called()

    def test_owned_detached_conflict_resumes_same_operation(self):
        self.block()
        self.pending()
        original = self.case.observed
        self.assertTrue(self.resume())
        self.assertEqual(self.case.observed, original)
        self.assertEqual(self.checkpoints.get('repo', 'G-047').operation, 'rebase')
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['last_error'], loop_manager.REBASE_IN_PROGRESS_ERROR)
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        self.git_write.assert_not_called()

    def test_manual_stage_resumes_without_stage_approval_or_extra_git_step(self):
        self.block()
        self.pending(staged=True)
        self.assertTrue(self.resume())
        self.assertEqual(self.case.observed.staged[0].new_oid, 'manual')
        self.assertEqual(self.checkpoints.get('repo', 'G-047').operation, 'rebase')
        self.git_write.assert_not_called()

    def test_previously_adopted_result_rechecks_current_state_and_releases_once(self):
        self.case.handler._complete_rebase_intent(self.case.fixture.context())
        self.block()
        self.assertTrue(self.resume())
        adopted = self.checkpoints.get('repo', 'G-047')
        self.assertFalse(self.resume())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), adopted)
        self.git_write.assert_not_called()

    def test_same_pending_result_cannot_repeat_a_released_correction(self):
        detail = self.block()
        self.pending()
        self.assertTrue(self.resume())
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
                                    "blocked_reason = 'managed_worktree_conflict'")
            self.connection.execute("UPDATE projects SET phase = 'PLAN_BLOCKED'")
            self.connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES ('repo', 'G-047', 'plan', 'managed_worktree_conflict', ?)", (detail,))
        before = self.snapshot()
        self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        from dataclasses import replace
        self.case.expected = replace(self.case.expected, input_fingerprint='new-user-input')
        self.case.observed = replace(self.case.observed, input_fingerprint='new-user-input')
        self.assertTrue(self.resume())

    def exhausted_pending(self):
        from recovery_runtime import RecoveryObservation, RecoveryReason
        from recovery_state import RecoveryRepository
        detail = self.block()
        self.pending()
        RecoveryRepository(self.connection).observe(RecoveryObservation(
            RecoveryReason.REBASE_CONFLICT, 'repo', 'G-047', 'WORKING',
            'old-revision', 'old-base', 'conflict', 'old-progress', detail), correction_limit=1)
        snapshot = loop_manager.GitSnapshot('partial', None,
            {'app.py': ('M 100644 manual 2',)},
            {'app.py': (('unstaged',), 'unresolved-content')},
            {'refs/heads/loop-manager/G-047': ('before', '')},
            {'/state/G-047': ('HEAD=partial', 'detached=True')}, {}, {'rebase-merge': 'operation'})
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET blocked_fingerprint = 'same-input', "
                "blocked_worktree_fingerprint = 'same-input', agent_result_snapshot = ?",
                (snapshot.serialize(),))
        return snapshot

    def test_first_exhausted_conflict_stays_blocked_until_selected_git_state_changes(self):
        from dataclasses import replace
        snapshot = self.exhausted_pending()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot) as capture, \
             mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint', return_value='same-input'), \
             mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint', return_value='same-input'):
            before = self.snapshot()
            self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)
            self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)
            capture.return_value = replace(snapshot, dirty={'app.py': (('unstaged',), 'user-resolution')})
            self.assertTrue(self.resume())
            self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.git_write.assert_not_called()

    def test_changed_current_inputs_resume_exhausted_conflict_without_git_progress(self):
        snapshot = self.exhausted_pending()
        for changed in ('revision', 'worktree'):
            with self.subTest(changed=changed), \
                 mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot), \
                 mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                     return_value='new-input' if changed == 'revision' else 'same-input'), \
                 mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                     return_value='new-input' if changed == 'worktree' else 'same-input'), \
                 mock.patch.object(loop_manager.runtime_state.RuntimeStateRepository,
                     'resume_rebase_block') as release:
                self.assertTrue(self.resume())
                release.assert_called_once()
        self.git_write.assert_not_called()

    def test_missing_recovery_history_does_not_make_saved_snapshot_a_resume_gate(self):
        snapshot = self.exhausted_pending()
        self.connection.execute('DELETE FROM recovery_records')
        self.connection.commit()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot), \
             mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint', return_value='same-input'), \
             mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint', return_value='same-input'):
            self.assertTrue(self.resume())
        self.git_write.assert_not_called()

    def test_project_rebase_release_preserves_each_independent_plan_cause(self):
        for cause in ('review_retry_exhausted', 'guidance_approval_required', 'user_decision_required',
                      'pull_request_closed', 'managed_worktree_scope_violation'):
            with self.subTest(cause=cause):
                # One isolated fixture per cause, without inheriting unrelated tests.
                case = EvidenceFreeRebaseBlockTests()
                case.setUp()
                try:
                    case.block(project=True, separate=cause)
                    original = dict(scoped_blocking.plan_block(case.connection, 'repo', 'G-047'))
                    self.assertTrue(case.resume())
                    self.assertEqual(dict(scoped_blocking.plan_block(case.connection, 'repo', 'G-047')), original)
                    row = case.connection.execute('SELECT * FROM exec_plans').fetchone()
                    self.assertEqual(row['status'], 'PLAN_BLOCKED')
                    self.assertEqual(row['blocked_reason'], cause)
                    self.assertEqual(row['last_error'], 'original block text')
                    self.assertEqual(row['review_result'], 'old review')
                    self.assertEqual(row['worker_session_id'], 'saved-worker')
                    self.assertIsNone(row['review_token'])
                    self.assertIsNone(scoped_blocking.project_block(case.connection, 'repo'))
                finally:
                    case.doCleanups()

    def test_unrelated_project_cause_is_not_released_by_rebase(self):
        self.block(project=True, reason='shared_ref_mutation')
        before = self.snapshot()
        with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity',
                               side_effect=loop_manager.WorkspaceOwnershipError('foreign owner')):
            self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.factory.assert_not_called()

    def test_other_execution_and_planning_keep_projection_sessions_and_diagnostics(self):
        self.block()
        for values in ("planning_error = 'separate error'", "planning_attempt_fingerprint = 'pending'",
                       "planner_session_id = 'planner'"):
            with self.subTest(values=values), self.connection:
                self.connection.execute('UPDATE projects SET ' + values)
                before = self.snapshot()
                self.assertFalse(self.resume())
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("UPDATE projects SET planning_error = NULL, "
                    "planning_attempt_fingerprint = NULL, planner_session_id = NULL")
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES ('repo', 'G-048', "
            "'docs/plans/[G-048] Other.md', 'Other', '[]', '[]', 'normal', 'hash', 'IMPLEMENTING')")
        self.connection.commit()
        before = self.snapshot()
        self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.factory.assert_not_called()

    def test_dependency_gate_blocks_release_without_observation(self):
        self.block()
        self.connection.execute("UPDATE exec_plans SET depends_on = '[\"G-109\"]'")
        self.connection.commit()
        before = self.snapshot()
        self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.factory.assert_not_called()

    def test_new_project_rebase_diagnosis_needs_no_legacy_text_or_occurrence_history(self):
        from recovery_state import RecoveryRepository
        from recovery_runtime import RecoveryObservation, RecoveryReason
        self.block(project=True, reason='publication_ownership_uncertain')
        detail = 'current owned rebase completion was misclassified'
        self.connection.execute('UPDATE project_blocks SET evidence = ?', (detail,))
        RecoveryRepository(self.connection).observe(RecoveryObservation(
            RecoveryReason.REMOTE_OID_UNCERTAIN, 'repo', 'G-047', 'WORKING',
            'old-input', 'old-base', 'old-fingerprint', 'another-session', detail),
            final_block_reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
        self.connection.execute("UPDATE recovery_records SET occurrence_index = '{}'")
        self.connection.commit()
        self.assertTrue(self.resume())
        self.assertIsNone(scoped_blocking.project_block(self.connection, 'repo'))
        self.assertEqual(RecoveryRepository(self.connection).row('repo', 'G-047')['pending_action'], 'reconcile')

    def test_unlinked_pending_rebase_project_cause_cannot_take_generic_release(self):
        self.block(project=True)
        self.connection.execute("UPDATE project_blocks SET evidence = 'unknown unrelated safety cause'")
        self.connection.commit()
        before = self.snapshot()
        with mock.patch.object(loop_manager, 'validate_managed_worktree') as generic:
            self.assertFalse(self.resume())
        generic.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_remote_scope_index_and_operation_mismatches_preserve_block(self):
        from dataclasses import replace
        from managed_rebase_validation import FileChange, IndexEntry
        self.block()
        original = self.case.observed
        for observed in (replace(original, remote=replace(original.remote, oid='foreign')),
                         replace(original, base_is_ancestor=False),
                         replace(original, base_is_ancestor=False, head='before', branch_ref=('before', ''),
                                 registration=('HEAD=before', 'branch=refs/heads/loop-manager/G-047')),
                         replace(original, operations=(('MERGE_HEAD', 'foreign'),)),
                         replace(original, index=(IndexEntry('app.py', '100644', 'blob', 0, 'S', 0x40000000),)),
                         replace(original, committed=(FileChange('docs/ARCHITECTURE.md',
                             '100644', '100644', 'old', 'new', 'M'),))):
            with self.subTest(observed=observed):
                self.case.observed = observed
                before = self.snapshot()
                self.assertFalse(self.resume())
                self.assertEqual(self.snapshot(), before)

    def test_target_and_original_head_mismatch_reject_detached_recovery(self):
        from dataclasses import replace
        self.block()
        self.pending()
        original = self.case.observed
        for key in ('onto', 'orig-head', 'head-name'):
            self.case.observed = replace(original, metadata=tuple(
                (name, 'foreign' if name == key else value) for name, value in original.metadata))
            before = self.snapshot()
            self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)

    def test_reobservation_race_and_transaction_faults_roll_back_then_resume_once(self):
        from dataclasses import replace
        import runtime_state
        self.block()
        before = self.snapshot()
        original = self.case.observed
        observe = self.case.factory(None).require_owned
        calls = []
        def raced(**kwargs):
            calls.append(None)
            if len(calls) == 2:
                self.case.observed = replace(original, remote=replace(original.remote, oid='raced'))
            return observe(**kwargs)
        with mock.patch.object(loop_manager, '_current_rebase_service',
                               return_value=SimpleNamespace(require_owned=raced)):
            self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.case.observed = original
        for stage in ('after_rebase_acceptance', 'after_rebase_block_release'):
            def fault(actual):
                if actual == stage:
                    raise runtime_state.RuntimeStateError('interrupted ' + stage)
            with mock.patch.object(runtime_state.RuntimeStateRepository, '_fault', side_effect=fault):
                self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.resume())
        adopted = self.checkpoints.get('repo', 'G-047')
        self.assertFalse(self.resume())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), adopted)

    def test_direct_input_and_interrupted_entry_use_current_recovery_before_generic_branch_gate(self):
        self.block()
        self.pending(staged=True)
        with mock.patch.object(loop_manager, 'make_worktree', side_effect=AssertionError('generic branch gate')):
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                self.connection, Path('/repo'), 'repo', 'main', Path('/state')))
        self.git_write.assert_not_called()

    def test_interrupted_entry_routes_detached_state_to_same_service(self):
        self.block()
        self.pending()
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        with mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')):
            self.assertTrue(loop_manager._resume_interrupted_blocked_rebase(
                self.connection, Path('/repo'), 'repo', 'main', plan,
                Path('/state/G-047'), 'loop-manager/G-047'))
        self.git_write.assert_not_called()

    def test_direct_input_recovery_cannot_replace_a_different_selected_block(self):
        self.block()
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status, blocked_reason, blocked_fingerprint) "
            "VALUES ('repo', 'G-048', 'docs/plans/[G-048] Other.md', 'Other', '[]', '[]', "
            "'normal', 'hash', 'PLAN_BLOCKED', 'verify_retry_exhausted', 'other-input')")
        self.connection.execute("UPDATE projects SET plan_path = 'docs/plans/[G-048] Other.md'")
        self.connection.commit()
        other = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id = 'G-048'").fetchone()
        with mock.patch.object(loop_manager, '_plan_block_selected_for_release', return_value=None) as selected:
            self.assertFalse(loop_manager.resume_blocked_execplan_if_inputs_changed(
                self.connection, Path('/repo'), 'repo', 'main', Path('/state')))
        selected.assert_called_once()
        self.factory.assert_not_called()
        self.assertEqual(self.connection.execute("SELECT plan_path FROM projects").fetchone()[0], other['path'])
