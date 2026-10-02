from dataclasses import replace
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import blocked_remote_observation
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode


from tests.support.blocked_remote import PlanRemoteRecoveryFixture


class PlanRecoveryGateTests(PlanRemoteRecoveryFixture, unittest.TestCase):
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


class PlanCheckpointPublicationRaceTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def test_registered_origin_lookup_failure_does_not_take_legacy_release_path(self):
        failure = blocked_remote_observation.RefObservation('error', error='network')
        with mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(failure, failure)):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_not_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')


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


    def test_guidance_successor_scope_survives_restart_without_rewriting_agent_snapshots(self):
        original = self.published_successor()
        reason = loop_manager.GUIDANCE_BLOCK_REASON
        for table, field in (('exec_plans', 'blocked_reason'), ('execplan_blocks', 'reason_code'),
                             ('blocked_checkpoints', 'reason_code')):
            self.connection.execute(f"UPDATE {table} SET {field} = ? WHERE plan_id = 'G-106'", (reason,))
        self.connection.commit()
        save = self.coordinator._save
        def interrupt(plan_id, key, stage, context):
            if stage == 'adopted':
                raise KeyboardInterrupt()
            return save(plan_id, key, stage, context)
        with mock.patch.object(self.coordinator, '_save', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        restarted = blocked_plan_reconciliation.BlockedPlanReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')
        with mock.patch.object(restarted, '_publish'):
            self.assertEqual(restarted.reconcile('G-106'), 'blocked')
        saved = self.connection.execute("SELECT stage FROM blocked_plan_reconciliations WHERE plan_id = 'G-106'").fetchone()
        self.assertEqual(saved['stage'], 'complete', self.plan()['last_error'])
        loop_manager.hydrate_execplan_project(self.connection, self.repo, self.plan())
        loop_manager.validate_blocked_agent_scope(self.connection, Path('/repo'), self.repo, 'main',
                                                 Path(self.plan()['worktree']), self.plan()['path'])
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        self.assertEqual(self.plan()['blocked_reason'], reason)
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()


    def test_occupied_project_restart_validates_pending_adoption_before_reconcile(self):
        self._assert_occupied_project_restart_validates_pending_adoption_before_reconcile()


    def test_occupied_restart_pending_adoption_rejects_unowned_ref(self):
        self._assert_occupied_restart_pending_adoption_rejects_unowned_ref()


    def test_base_fetch_failure_retries_same_input_after_restart(self):
        calls = []
        def git(path, *args, **kwargs):
            if args[:1] == ('fetch',):
                calls.append(args)
                if len(calls) == 1:
                    return SimpleNamespace(returncode=1, stdout='network unavailable')
            return self.git(path, *args, **kwargs)
        self.ports.git.side_effect = git
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        restarted = blocked_plan_reconciliation.BlockedPlanReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')
        with mock.patch.object(restarted, '_publish'):
            self.assertEqual(restarted.reconcile('G-106'), 'blocked')
        self.assertEqual(len(calls), 2)
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
        self.assertFalse(blocked_remote_observation.pending_input(self.row()))


    def test_registered_observation_preserves_legacy_review_release(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = loop_manager.GitSnapshot(self.head, self.plan()['branch'], {}, {},
                                                refs={}, worktrees={}, config={}, operations={})
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, review_result = 'VERDICT: FAIL', "
                "agent_result_snapshot = ?, blocked_reason = 'review_retry_exhausted' WHERE plan_id = 'G-106'",
                (directory, snapshot.serialize()))
            self.connection.execute("UPDATE execplan_blocks SET reason_code = 'review_retry_exhausted' WHERE plan_id = 'G-106'")
            self.connection.execute("UPDATE blocked_checkpoints SET reason_code = 'review_retry_exhausted', phase = 'unpublished' WHERE plan_id = 'G-106'")
            self.connection.commit()
            loop_manager._ensure_execplans_schema(self.connection)
            other = dict(self.plan('G-107'))
            with (mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
                  mock.patch.object(loop_manager, 'validate_managed_worktree'),
                  mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
                  mock.patch.object(loop_manager, '_rebase_in_progress_for_branch', return_value=False),
                  mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
                  mock.patch.object(loop_manager, 'remote_branch_oid', return_value=None),
                  mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(returncode=0, stdout='')),
                  mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
                  mock.patch.object(blocked_plan_reconciliation, 'BlockedPlanReconciler') as remote):
                remote.return_value.reconcile.return_value = 'blocked'
                self.assertTrue(loop_manager.resume_plan_blocks(
                    self.connection, Path('/repo'), Path('/state'), self.repo, 'main'))
            remote.assert_not_called()
            self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
            self.assertEqual(self.plan()['worker_session_id'], 'session')
            self.assertEqual(self.plan()['review_result'], 'VERDICT: FAIL')
            self.assertEqual(dict(self.plan('G-107')), other)


    def test_fetch_race_preserves_files_and_does_not_rebase_or_release(self):
        real_fake_git = self.git
        self.ports.git.side_effect = lambda path, *args, **kwargs: (
            SimpleNamespace(returncode=0, stdout='different') if args == ('rev-parse', 'FETCH_HEAD^{commit}')
            else real_fake_git(path, *args, **kwargs))
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_not_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertIn('OID 경쟁', self.plan()['last_error'])


    def test_candidate_change_after_evaluation_does_not_release(self):
        with mock.patch.object(self.coordinator, '_publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        self.signature = 'external edit'
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertIn('후보', self.plan()['last_error'])


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


    def test_uncertain_verify_process_creates_safety_block_without_publication(self):
        self.ports._evaluate_remote_checkpoint_cause.side_effect = loop_manager.VerifyTerminationUncertain('verify child is still alive')
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        publish.assert_not_called()
        self.assertEqual(scoped_blocking.project_block(self.connection, self.repo)['reason_code'], 'repository_state_unsafe')
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')


class PlanCauseRevalidationTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def test_refusal_keeps_original_cause_and_is_memoized(self):
        self.ports.pr_for_branch.return_value = {'state': 'OPEN'}
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.ports._update_blocked_error.assert_called_once()
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')


    def test_guidance_tracking_allowance_does_not_excuse_actual_agent_ref_changes(self):
        original, _ = self.guidance_tracking_updates()
        for ref in ('refs/remotes/origin/main', f"refs/remotes/origin/{self.plan()['branch']}"):
            with self.subTest(ref=ref):
                result = replace(original, refs={**original.refs, ref: self.current_snapshot.refs[ref]})
                self.connection.execute("UPDATE exec_plans SET agent_result_snapshot = ? WHERE plan_id = 'G-106'",
                                        (result.serialize(),))
                with self.assertRaises(loop_manager.AgentScopeError):
                    self.coordinator._validate(self.plan())


    def test_guidance_tracking_requires_exact_live_base_and_durable_branch_identity(self):
        original, proof = self.guidance_tracking_updates()
        current = self.current_snapshot
        self.current_snapshot = replace(current, refs={**current.refs, 'refs/remotes/origin/main': ('unproven', '')})
        with self.assertRaises(loop_manager.AgentScopeError):
            self.coordinator._validate(self.plan())
        self.current_snapshot = current
        proof.return_value = None
        with self.assertRaises(loop_manager.AgentScopeError):
            self.coordinator._validate(self.plan())
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def test_fast_forward_effect_before_sqlite_result_resumes_without_repeating_merge(self):
        original = self.interrupt_after_fast_forward()
        self.ports.git.reset_mock()
        restarted = blocked_plan_reconciliation.BlockedPlanReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')
        with mock.patch.object(restarted, '_publish'):
            self.assertEqual(restarted.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
        self.assertFalse(any('merge' in call.args for call in self.ports.git.call_args_list))
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
        self.assertIsNone(checkpoint.snapshot['remote_adoption_intent'])
        self.assertEqual(checkpoint.snapshot['remote_adoption_scope']['head'], self.head)


    def test_interrupted_fast_forward_rejects_later_shared_mutation(self):
        original = self.interrupt_after_fast_forward()
        self.current_snapshot = replace(self.current_snapshot,
            refs={**self.current_snapshot.refs, 'refs/heads/unowned': ('foreign', '')})
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def test_interrupted_fast_forward_rejects_later_staged_edit(self):
        self.interrupt_after_fast_forward()
        self.current_snapshot = replace(self.current_snapshot,
            dirty={'code.py': (('staged',), 'changed')})
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()


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
        self.ports._recorded_base_fetch.assert_called_once_with(
            Path('/repo'), Path('/state'), 'main', owner='plan:G-106')
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


    def test_new_verify_failure_does_not_clear_a_saved_review_cause(self):
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, 'new verify failure', 'verify')
        context = {'block': {'reason_code': 'review_retry_exhausted', 'evidence': 'review issue'}, 'artifacts': {}}
        self.assertFalse(self.coordinator._evaluate(self.plan(), context)['resolved'])


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


class PlanRecoveryAdoptionTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def test_guidance_post_checkpoint_tracking_updates_allow_scope_before_successor_adoption(self):
        original, proof = self.guidance_tracking_updates()
        self.coordinator._validate(self.plan())
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        proof.assert_called_once()
        self.assertFalse(proof.call_args.kwargs['require_live'])
        self.assertEqual(proof.call_args.args[6], original.head)
        self.ports._reconcile_published_blocked_checkpoint.assert_not_called()


    def test_guidance_tracking_updates_continue_through_successor_scope_and_cause_check(self):
        original, proof = self.guidance_tracking_updates()
        for table in ('execplan_blocks', 'blocked_checkpoints'):
            self.connection.execute(f"UPDATE {table} SET reason_code = ? WHERE plan_id = 'G-106'",
                                    (loop_manager.GUIDANCE_BLOCK_REASON,))
        self.connection.commit()
        proof.side_effect = lambda *args, **kwargs: args[6]
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        stage = self.connection.execute("SELECT stage FROM blocked_plan_reconciliations WHERE plan_id = 'G-106'").fetchone()[0]
        self.assertEqual(stage, 'complete', self.plan()['last_error'])
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        self.assertEqual(self.plan()['blocked_reason'], loop_manager.GUIDANCE_BLOCK_REASON)
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        # The accepted rebase path still compares the original Agent output,
        # then validates post-Agent tracking refs against the same proofs.
        branch, worktree = self.plan()['branch'], self.plan()['worktree']
        self.current_snapshot = replace(self.current_snapshot, head='rebased',
            refs={**self.current_snapshot.refs, f'refs/heads/{branch}': ('rebased', '')},
            worktrees={worktree: ('HEAD=rebased', f'branch=refs/heads/{branch}')})
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        prior = checkpoints.get(self.repo, 'G-106')
        checkpoints.record(self.repo, 'G-106', expected_generation=prior.generation,
            operation=None, expected_head='rebased', target_base_oid=self.base,
            observed_remote_oid=prior.observed_remote_oid,
            snapshot={**prior.snapshot, 'current_rebase_acceptance': {'head': 'rebased'}})
        with mock.patch.object(loop_manager, 'requirement_archive_candidates', return_value=()):
            self.coordinator._validate(self.plan(), rebase=True)
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def test_successor_adoption_preserves_agent_audit_and_validates_new_baseline(self):
        original = self.published_successor()
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.ports._evaluate_remote_checkpoint_cause.call_count, 1, self.plan()['last_error'])
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        self.assertEqual(self.plan()['agent_result_snapshot'], original.serialize())
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
        self.assertEqual(checkpoint.snapshot['remote_adoption_scope']['head'], self.head)
        # Later shared metadata writes and local commits are not absorbed.
        adopted = self.current_snapshot
        for changed in (replace(adopted, head='unauthorized'),
                        replace(adopted, config={'unexpected': ('value',)}),
                        replace(adopted, refs={**adopted.refs, 'refs/heads/unowned': ('foreign', '')})):
            self.current_snapshot = changed
            with self.assertRaises(loop_manager.AgentScopeError):
                self.coordinator._validate(self.plan())


    def test_previous_rebase_does_not_shadow_resumed_worker_adoption(self):
        self.with_previous_rebase_acceptance(self.test_resumed_worker_uses_adopted_head_and_checks_its_output)


    def test_previous_rebase_does_not_shadow_project_entry_pending_adoption(self):
        self.with_previous_rebase_acceptance(self._assert_occupied_project_restart_validates_pending_adoption_before_reconcile)


    def test_previous_rebase_pending_adoption_still_rejects_unowned_ref(self):
        self.with_previous_rebase_acceptance(self._assert_occupied_restart_pending_adoption_rejects_unowned_ref)


    def test_later_rebase_has_priority_over_completed_adoption(self):
        checkpoint = SimpleNamespace(operation=None, expected_head='adopted', observed_remote_oid='adopted',
            snapshot={'remote_adoption_scope': {'head': 'adopted'},
                      'current_rebase_acceptance': {'head': 'previous'}})
        self.assertTrue(blocked_plan_reconciliation.remote_adoption_is_current(checkpoint))
        checkpoint.operation = 'rebase'
        self.assertFalse(blocked_plan_reconciliation.remote_adoption_is_current(checkpoint))
        checkpoint.operation = None
        checkpoint.expected_head = 'rebased'
        checkpoint.snapshot['current_rebase_acceptance'] = {'head': 'rebased'}
        self.assertFalse(blocked_plan_reconciliation.remote_adoption_is_current(checkpoint))
        checkpoint.expected_head = 'adopted'
        checkpoint.snapshot['current_rebase_acceptance'] = {'head': 'adopted'}
        self.assertFalse(blocked_plan_reconciliation.remote_adoption_is_current(checkpoint))


    def test_resumed_worker_uses_adopted_head_and_checks_its_output(self):
        original = self.published_successor()
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, 'resolved', 'verify')
        self.assertEqual(self.coordinator.reconcile('G-106'), 'resumed')
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertEqual(self.persisted_adoption_scope().head, self.head)
        before = loop_manager.begin_agent_scope(
            self.connection, self.repo, Path(self.plan()['worktree']), 'worker')
        self.assertEqual(before.head, self.head)
        loop_manager.validate_agent_scope('worker', Path(self.plan()['worktree']),
            before, self.current_snapshot, selected_plan_path=self.plan()['path'])
        loop_manager.update_project(self.connection, self.repo,
                                    agent_result_snapshot=self.current_snapshot.serialize())
        self.persisted_adoption_scope()
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        for changed in (
                replace(self.current_snapshot, head='unauthorized'),
                replace(self.current_snapshot, refs={**self.current_snapshot.refs,
                    'refs/heads/unowned': ('foreign', '')})):
            with self.subTest(changed=changed):
                loop_manager.update_project(self.connection, self.repo, agent_result_snapshot=changed.serialize())
                with self.assertRaises(loop_manager.AgentScopeError):
                    self.persisted_adoption_scope()
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def test_agent_scope_violation_before_adoption_is_not_absorbed(self):
        original = self.published_successor()
        self.current_snapshot = replace(original, refs={**original.refs, 'refs/heads/unowned': ('foreign', '')})
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._reconcile_published_blocked_checkpoint.assert_not_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def test_successor_fetch_failure_retries_without_consuming_observation(self):
        self.published_successor()
        self.fail_branch_fetch = True
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()


    def test_remote_lookup_failure_retries_same_input(self):
        failed = blocked_remote_observation.RefObservation('error', error='network')
        with mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(failed, failed)):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
