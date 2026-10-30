from dataclasses import replace
import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import blocked_remote_observation
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode


from tests.support.blocked_remote import PlanRemoteRecoveryFixture
from tests.support.git import git_command


class PlanRecoveryGateTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def test_new_remote_input_keeps_original_failure_after_candidate_results_are_invalidated(self):
        self.connection.execute("UPDATE exec_plans SET worker_result='Original failure' WHERE plan_id='G-106'")
        self.connection.commit()
        def incorporate(_plan, context):
            context['candidate'] = self.signature
            with self.connection:
                self.connection.execute("UPDATE exec_plans SET worker_result=NULL WHERE plan_id='G-106'")
        with (mock.patch.object(self.coordinator, '_incorporate', side_effect=incorporate),
              mock.patch.object(self.coordinator, '_publish')):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            for revision, change_cause, expected in (
                    ('c' * 40, False, 'Original failure'), ('d' * 40, True, None)):
                with self.subTest(revision=revision):
                    if change_cause:
                        self.connection.execute("UPDATE execplan_blocks SET evidence='Different failure' WHERE plan_id='G-106'")
                        self.connection.commit()
                    self.base = revision
                    self.record_observation()
                    blocked_remote_observation.observe_refs.return_value = (
                        blocked_remote_observation.RefObservation('present', self.base),
                        blocked_remote_observation.RefObservation('absent'))
                    self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
                    saved = self.connection.execute(
                        "SELECT context FROM blocked_plan_reconciliations WHERE plan_id='G-106'").fetchone()[0]
                    self.assertEqual(json.loads(saved)['artifacts']['worker_result'], expected)

    def test_cycle_targets_can_recover_without_releasing_unrelated_plans(self):
        from loop_manager_internal.application.commands import local_recovery_request_ready
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository, MaintenancePhase
        store = MaintenanceRepository(self.connection)
        cycle = store.get(self.repo)
        request = SimpleNamespace(repo=self.repo, plan_id='G-106')
        for phase, publication, roots, allowed in (
                (MaintenancePhase.IDLE, None, (), True),
                (MaintenancePhase.REFACTORING, 'remote_confirmed', ('G-106',), True),
                (MaintenancePhase.PREREQUISITE_WAIT, None, ('G-106',), True),
                (MaintenancePhase.POST_REFACTOR_REVALIDATION, None, ('G-106',), True),
                (MaintenancePhase.REFACTORING, 'remote_confirmed', ('G-107',), False),
                (MaintenancePhase.REFACTORING, 'remote_confirmed', ('G-999',), False),
                (MaintenancePhase.REFACTORING, 'prepared', ('G-106',), False),
                (MaintenancePhase.ASSESSMENT, None, ('G-106',), False)):
            with self.subTest(phase=phase, publication=publication, roots=roots), mock.patch.object(
                    MaintenanceRepository, 'get', return_value=replace(cycle, phase=phase,
                        publication_stage=publication, refactoring_plan_ids=roots)):
                self.assertEqual(blocked_plan_reconciliation.slot_available(
                    self.connection, self.repo, 'G-106'), allowed)
                self.assertEqual(local_recovery_request_ready(self.connection, request), allowed)
        self.connection.execute("UPDATE exec_plans SET depends_on='[\"G-106\"]' WHERE plan_id='G-107'")
        with mock.patch.object(MaintenanceRepository, 'get', return_value=replace(cycle,
                phase=MaintenancePhase.REFACTORING, publication_stage='remote_confirmed',
                refactoring_plan_ids=('G-107',))):
            self.assertTrue(blocked_plan_reconciliation.slot_available(self.connection, self.repo, 'G-106'))
            self.assertTrue(local_recovery_request_ready(self.connection, request))
            self.connection.execute("UPDATE exec_plans SET depends_on='[\"G-107\"]' WHERE plan_id='G-106'")
            self.assertFalse(blocked_plan_reconciliation.slot_available(self.connection, self.repo, 'G-106'))
            self.assertFalse(local_recovery_request_ready(self.connection, request))

    def test_recovery_slot_checks_target_resources_instead_of_sibling_projection(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.update_plan(self.repo, 'G-107', plan_values={'status': 'VERIFYING'})
        runtime.hydrate(self.repo, 'G-107')
        before = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        from loop_manager_internal.config.managed_config import ManagedConfig, fast_mode_scope
        with fast_mode_scope(ManagedConfig(path=Path('/unused'), max_workers=2,
                                          telegram_token='', telegram_chat_id='', max_tasks_per_project=3)):
            self.assertTrue(blocked_plan_reconciliation.slot_available(self.connection, self.repo, 'G-106'))
            runtime.update_plan(self.repo, 'G-106', plan_values={'worktree': self.plan('G-107')['worktree']})
            self.assertFalse(blocked_plan_reconciliation.slot_available(self.connection, self.repo, 'G-106'))
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), before)

    def test_fetch_lease_contention_keeps_same_remote_observation_retryable(self):
        with mock.patch.object(self.coordinator, '_incorporate',
                side_effect=loop_manager.ProcessLockBusyError('git:fetch is occupied')) as incorporate:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        incorporate.assert_called_once()
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        saved = self.connection.execute(
            "SELECT stage FROM blocked_plan_reconciliations WHERE repo=? AND plan_id='G-106'", (self.repo,)).fetchone()
        self.assertNotEqual(saved['stage'], 'refused')

    def test_other_plans_independent_pr_recovery_does_not_take_the_target_slot(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.update_plan(self.repo, 'G-107', plan_values={'pr_conflict_recovery': 1})
        runtime.hydrate(self.repo, 'G-107')
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        plan, observation = dict(self.plan()), dict(self.row())
        with mock.patch.object(self.coordinator, '_reconcile', return_value='blocked') as recheck:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        recheck.assert_called_once()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertEqual(dict(self.plan()), plan)
        self.assertEqual(dict(self.row()), observation)
        self.ports.git.assert_not_called()

    def test_same_plans_pr_recovery_still_defers_for_planning_or_maintenance(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.update_plan(self.repo, 'G-106', plan_values={'pr_conflict_recovery': 1})
        runtime.hydrate(self.repo, 'G-106')
        self.assertTrue(blocked_plan_reconciliation.slot_available(self.connection, self.repo, 'G-106'))
        for update, reset in (
                ("UPDATE projects SET planning_error = 'pending planning'", "UPDATE projects SET planning_error = NULL"),
                ("INSERT INTO maintenance_cycles (repo, phase) VALUES ('repo', 'assessment')", "DELETE FROM maintenance_cycles")):
            with self.subTest(update=update):
                self.connection.execute(update)
                self.connection.commit()
                before = dict(self.row())
                self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred')
                self.assertEqual(dict(self.row()), before)
                self.connection.execute(reset)
                self.connection.commit()
        self.ports.git.assert_not_called()

    def test_completed_base_pending_adoption_uses_current_result_boundary(self):
        original = self.published_successor()
        plan = self.plan()
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        current = store.get(self.repo, 'G-106')
        request = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106')
        store.record(self.repo, 'G-106', expected_generation=current.generation, operation=None,
            expected_head=self.head, target_base_oid=self.base, observed_remote_oid=original.head,
            snapshot={'base_rebase_restart': {'phase': 'complete', 'remote': original.head},
                      'remote_adoption_intent': {'head': original.head, 'branch': plan['branch'],
                          'worktree': plan['worktree'], 'request_identity': request.request_identity,
                          'snapshot': original.serialize(), 'agent_snapshot': plan['agent_snapshot'],
                          'agent_result_snapshot': plan['agent_result_snapshot']}})
        self.ports.validate_blocked_agent_scope.side_effect = AssertionError('stale Agent comparison')
        def integrate(effects, connection, repo_path, state_dir, repo, plan, remote, snapshot):
            effects.git(Path(plan['worktree']), 'merge', '--ff-only', remote)
            return effects.capture_git_snapshot(Path(plan['worktree']))
        # The coordinator's Git-effect boundary is fake; the adoption handler
        # still performs its current lease, identity and transactional checks.
        with (mock.patch('loop_manager_internal.recovery.work_branch_integration.integrate',
                         side_effect=integrate) as integration,
              mock.patch.object(self.coordinator, '_incorporate', side_effect=KeyboardInterrupt)):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        integration.assert_called_once()
        self.ports._reconcile_published_blocked_checkpoint.assert_called_once()
        self.ports.validate_execplan_workspace_identity.assert_called()
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())

    def test_push_after_observation_is_preserved_until_refresh_pins_new_lease(self):
        original = self.published_successor()
        successor = self.row()['branch_oid']
        self.connection.execute(
            'UPDATE blocked_remote_observations SET branch_oid = ?, confirmed_branch_oid = ? '
            'WHERE repo = ? AND plan_id = ?',
            (original.head, original.head, self.repo, 'G-106'))
        self.connection.commit()
        with (mock.patch.object(loop_manager, '_worktree_state_signature', return_value=self.signature),
              mock.patch.object(self.coordinator, '_incorporate', side_effect=KeyboardInterrupt) as incorporate):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.ports._reconcile_published_blocked_checkpoint.assert_not_called()
            incorporate.assert_not_called()
            self.assertEqual(self.current_snapshot, original)
            self.assertEqual(blocked_checkpoint_state.BlockedCheckpointRepository(
                self.connection).get(self.repo, 'G-106').remote_oid, original.head)
            loop_manager.observe_blocked_plan_remotes(
                self.connection, Path('/repo'), self.repo,
                self.connection.execute('SELECT * FROM projects').fetchone(), plan_id='G-106')
            self.assertEqual(self.row()['branch_oid'], successor)
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        self.assertEqual(self.current_snapshot.head, successor)
        self.assertEqual(self.ports._reconcile_published_blocked_checkpoint.call_args.kwargs[
            'expected_remote_oid'], successor)
        self.assertEqual(execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection).get(self.repo, 'G-106').observed_remote_oid, successor)
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        self.assertEqual(self.plan()['agent_result_snapshot'], original.serialize())
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'],
                         'original verify failure')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))

    def test_published_successor_does_not_update_another_repositorys_plan(self):
        other = dict(self.plan())
        other['repo'] = 'other-repo'
        columns = ', '.join(other)
        placeholders = ', '.join('?' for _ in other)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, origin) VALUES (?, 'main', 'other/repo')",
            (other['repo'],),
        )
        self.connection.execute(
            f"INSERT INTO exec_plans ({columns}) VALUES ({placeholders})",
            tuple(other.values()),
        )
        self.connection.commit()

        self.published_successor()

        persisted = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-106'",
            (other['repo'],),
        ).fetchone()
        self.assertEqual(dict(persisted), other)

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

    def test_remote_snapshot_race_from_scope_validation_keeps_same_input_retryable(self):
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection).get(self.repo, 'G-106')
        self.assertIsNone(checkpoint.operation)
        request = self.connection.execute(
            "SELECT phase FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
            (self.repo, 'G-106'),
        ).fetchone()
        self.assertNotEqual(request['phase'], 'retry_pending')
        self.ports.validate_blocked_agent_scope.side_effect = [
            recovery_runtime.RecoveryFailure(
                recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE,
                'tracking ref moved during remote observation',
            ),
            None,
            None,
        ]

        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        saved = self.connection.execute(
            "SELECT stage FROM blocked_plan_reconciliations WHERE repo = ? AND plan_id = ?",
            (self.repo, 'G-106'),
        ).fetchone()
        self.assertEqual(saved['stage'], 'observed')

        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports.validate_blocked_agent_scope.assert_called()
        self.assertEqual(self.ports.validate_blocked_agent_scope.call_count, 3)
        self.assertFalse(blocked_remote_observation.pending_input(self.row()))

    def test_code_only_commit_marks_blocked_candidate_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            git_command(worktree, 'init', '--initial-branch=main')
            git_command(worktree, 'config', 'user.email', 'loop-manager@example.com')
            git_command(worktree, 'config', 'user.name', 'Loop Manager Test')
            source = worktree / 'src/app.py'
            source.parent.mkdir()
            source.write_text('VALUE = 1\n', encoding='utf-8')
            git_command(worktree, 'add', 'src/app.py')
            git_command(worktree, 'commit', '-m', 'baseline')
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            source.write_text('VALUE = 2\n', encoding='utf-8')
            git_command(worktree, 'add', 'src/app.py')
            git_command(worktree, 'commit', '-m', 'user code correction')
            self.ports.capture_git_snapshot.side_effect = loop_manager.capture_git_snapshot
            self.ports._worktree_execplan_input_fingerprint.return_value = 'same-plan-input'
            self.ports._revision_execplan_input_fingerprint.return_value = 'same-plan-input'
            self.connection.execute(
                'UPDATE exec_plans SET worktree = ?, blocked_fingerprint = ?, '
                'blocked_worktree_fingerprint = ?, agent_result_snapshot = ? '
                'WHERE repo = ? AND plan_id = ?',
                (str(worktree), 'same-plan-input', 'same-plan-input',
                 blocked_result.serialize(), self.repo, 'G-106'),
            )
            self.connection.commit()
            plan = self.plan()

            self.assertEqual(
                self.ports._worktree_execplan_input_fingerprint(worktree, plan),
                plan['blocked_worktree_fingerprint'],
            )
            self.assertTrue(self.coordinator._candidate_changed(
                plan, {'base_oid': self.base},
            ))


class ExistingPrPlanRecoveryTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def _resume_general_pr_candidate(self):
        original = self.published_successor()
        successor = self.row()['branch_oid']
        self.connection.execute(
            "UPDATE exec_plans SET pr_url = 'saved-pr', pr_head_oid = ?, publication_remote_oid = ?, "
            "worker_input_token = 'old-worker', verify_token = 'old-verify', review_token = 'old-review', "
            "publication_token = 'old-publication' WHERE plan_id = 'G-106'",
            (original.head, original.head))
        self.connection.execute("UPDATE blocked_checkpoints SET pr_url = 'saved-pr' WHERE plan_id = 'G-106'")
        self.connection.commit()
        loop_manager._ensure_execplans_schema(self.connection)
        self.pull_request = {'url': 'saved-pr', 'state': 'OPEN',
            'headRefName': self.plan()['branch'], 'headRefOid': successor, 'baseRefName': 'main'}
        self.ports.pr_for_branch.side_effect = lambda *_: dict(self.pull_request)
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, 'user fix resolves cause', 'verify')
        patch = mock.patch.object(loop_manager, 'pr_for_branch', side_effect=self.ports.pr_for_branch)
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(self.coordinator.reconcile('G-106'), 'resumed', self.plan()['last_error'])
        self.ports._journaled_direct_rebase.assert_not_called()
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
        self.assertIsNone(checkpoint.operation)
        self.assertIsNone(checkpoint.rebase_publication_intent())
        self.assertNotIn('current_rebase_acceptance', checkpoint.snapshot)
        self.assertEqual(self.plan()['pr_head_oid'], successor)
        self.assertEqual(self.plan()['publication_remote_oid'], successor)
        self.assertEqual(self.plan()['pr_conflict_recovery'], 0)
        receipt = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106')
        self.assertEqual(receipt.remote_oid, original.head)
        self.assertEqual(self.connection.execute(
            "SELECT accepted_remote_oid FROM blocked_checkpoints WHERE plan_id = 'G-106'"
        ).fetchone()[0], successor)
        return receipt

    def _enter_general_pr_candidate(self, execution=None, *, observe_waiting=False):
        from loop_manager_internal.application.project_service import ProjectService
        from loop_manager_internal.execution.execution_runtime import (
            ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind, ExecutionPhase, ExecutionPlanMetadata)
        from loop_manager_internal.planning.planning_runtime import PlanningOutcome, PlanningOutcomeKind
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        supplied_execution = execution
        if execution is None:
            execution = mock.Mock(spec=['prepare', 'recover', 'guard', 'run', 'verify', 'review', 'publish'])
        def context():
            plan = runtime.projected(self.repo)
            return ExecutionContext(repo=self.repo, repo_path=Path('/repo'), state_dir=Path('/state'),
                base_branch='main', base_sha=self.base, provider='codex', config_path=Path('/config'),
                plan=ExecutionPlanMetadata(plan.plan_id, self.plan()['title'], plan.path, plan.plan_sha),
                phase=ExecutionPhase(plan.status), branch=plan.branch, worktree=Path(plan.worktree),
                active_sha=plan.active_sha, validated_base_sha=plan.validated_base_sha,
                worker_session_id=plan.worker_session_id, pr_url=plan.pr_url,
                worker_input_token=plan.worker_input_token, verify_token=plan.verify_token,
                review_token=plan.review_token, publication_token=plan.publication_token)
        if supplied_execution is None:
            execution.prepare.side_effect = context
            execution.recover.side_effect = execution.guard.side_effect = lambda current: current
            for stage in ('run', 'verify', 'review', 'publish'):
                getattr(execution, stage).return_value = ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)
        def prepare(service):
            self.connection.execute('UPDATE projects SET planning_attempt_fingerprint = NULL')
            service.state.repo, service.state.repo_path, service.state.base = self.repo, Path('/repo'), 'main'
            service.state.row = self.connection.execute('SELECT * FROM projects WHERE repo = ?', (self.repo,)).fetchone()
            service.state.recovered_planning_outcome = PlanningOutcome(
                PlanningOutcomeKind.EXECUTION_READY if runtime.projected(self.repo) is not None
                else PlanningOutcomeKind.WAITING)
        def synchronize(service):
            if observe_waiting:
                loop_manager.observe_waiting_pull_requests(
                    self.connection, Path('/repo'), Path('/state'), self.repo)
                self.waiting_resume_status = self.plan()['status']
                prepare(service)
        # Repository/catalog effects and Agent processes are fake unit ports;
        # project PR recovery and execution phase dispatch remain production code.
        with (mock.patch.object(ProjectService, '_prepare_repository', prepare),
              mock.patch.object(ProjectService, '_reconcile_catalog_and_cleanup', return_value=None),
              mock.patch.object(ProjectService, '_synchronize_pull_requests', autospec=True, side_effect=synchronize),
              mock.patch.object(ProjectService, '_refresh_catalog', return_value=self.base),
              mock.patch.object(loop_manager, '_recorded_base_fetch', return_value=self.base),
              mock.patch.object(loop_manager, '_check_runtime_handoff'),
              mock.patch.object(loop_manager, '_ExecutionLifecyclePorts', return_value=execution),
              mock.patch.object(loop_manager, '_fetch_pr_conflict_recovery_refs'),
              mock.patch.object(loop_manager, 'remote_branch_oid',
                                side_effect=lambda *_: self.pull_request['headRefOid']),
              mock.patch.object(loop_manager, 'make_worktree'),
              mock.patch.object(loop_manager, 'operation_print')):
            result = loop_manager._run_project_step(self.connection, Path('/state'), self.repo,
                Path('/config'), None, False)
        return result, execution

    def _adopted_candidate_execution(self):
        from tests.support.execution import execution_fixture, recovered_rebase_publication
        from loop_manager_internal.execution.execution_handlers import ExecutionHandlers
        from loop_manager_internal.execution.execution_state import ExecutionStatePort
        from loop_manager_internal.execution.worker_session import SessionBinding
        fixture = execution_fixture(self)
        state = ExecutionStatePort(self.connection,
            loop_manager.runtime_state.RuntimeStateRepository(self.connection), self.repo)
        state.update_project(self.repo, active_sha=self.base,
            worker_session_binding=SessionBinding('codex', 'model', 'high').serialize())
        original, tree = 'b' * 40, 'e' * 40
        self.connection.execute(
            "UPDATE blocked_checkpoints SET published_oid = ?, commit_identity = 'owned-checkpoint', "
            "tree_oid = ? WHERE plan_id = 'G-106'", (original, tree))
        self.connection.commit()
        self.candidate_dirty = False
        self.pushes = []
        self.ancestry = True
        def git(path, *args, **kwargs):
            output, code = '', 0
            if args[:2] == ('remote', 'get-url'):
                output = 'https://github.com/example/repo'
            elif args[0] == 'rev-parse':
                output = (tree if args[-1] == f'{original}^{{tree}}' else
                    'f' * 40 if args[-1] == f"{'d' * 40}^{{tree}}" else
                    self.base if args[-1] in {'origin/main', 'FETCH_HEAD^{commit}'} else self.head)
            elif args[0] == 'show':
                output = (('c' * 40 if '--format=%P' in args else 'Concrete change')
                    if args[-1] == 'd' * 40 else
                    '[G-106] BLOCKED checkpoint\nScope: plan\nCheckpoint-Identity: owned-checkpoint\n')
            elif args[0] == 'write-tree':
                output = 'f' * 40
            elif args[0] == 'symbolic-ref':
                output = self.plan()['branch']
            elif args[0] in {'ls-remote', 'show-ref'}:
                output = self.pull_request['headRefOid']
                if args[0] == 'ls-remote':
                    output += '\t' + f"refs/heads/{self.plan()['branch']}\n"
            elif args == ('merge-base', self.base, 'HEAD'):
                output = self.base
            elif args == ('diff', '--no-ext-diff', '--name-status', '-z', '--find-renames', self.base):
                output = ''
            elif args[0] == 'merge-base':
                code = 0 if self.ancestry or args[2] == self.base else 1
            elif args[0] == 'status' and self.candidate_dirty:
                output = ' M app.py'
            elif args[0] == 'diff' and '--quiet' in args:
                code = int(self.candidate_dirty)
            elif args[0] == 'commit':
                self.head, self.candidate_dirty = 'd' * 40, False
            elif args[0] == 'push':
                self.pushes.append(args)
                self.pull_request['headRefOid'] = self.head
            return SimpleNamespace(stdout=output, returncode=code)
        git_port = mock.Mock(side_effect=git)
        patch = mock.patch.object(loop_manager, 'git', git_port)
        patch.start()
        self.addCleanup(patch.stop)
        workspace = replace(fixture.effects.workspace, git=git_port,
            _plan_id_from_path=loop_manager._plan_id_from_path,
            git_operation_entries=None,
            live_remote_branch_oid=lambda *_: self.pull_request['headRefOid'],
            remote_branch_oid=lambda *_: self.pull_request['headRefOid'])
        workspace._rebase_in_progress_for_branch.return_value = False
        # Production publication and ownership policies use the actual SQLite
        # source rows and checkpoint receipt. Only external effects are faked.
        publication = recovered_rebase_publication(fixture.effects.publication, state)
        publication.pr_for_branch.side_effect = lambda *_: dict(self.pull_request)
        publication.reconcile_existing_pull_request.side_effect = state.bind(
            loop_manager.reconcile_existing_pull_request, connected=True, mutates=True,
            name='reconcile_existing_pull_request')
        plan = {'id': 'G-106', 'title': self.plan()['title'], 'sources': []}
        fixture.effects.agent.validate_worker_workspace_scope.return_value = plan
        fixture.effects.validation.validate_active_execplan_metadata.return_value = plan
        fixture.effects.revision.reconcile_catalog.side_effect = lambda *_: SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha=self.base)
        fixture.effects.validation.ensure_worker_level_state.return_value = 6
        fixture.effects.agent.capture_git_snapshot.side_effect = lambda *_: replace(
            self.current_snapshot, head=self.head, branch=self.plan()['branch'],
            dirty={'app.py': (' M', 'worker patch')} if self.candidate_dirty else {})
        def agent(*args, **kwargs):
            result = fixture.agent(*args, **kwargs)
            if args[4] == 'worker':
                self.candidate_dirty = True
            return result
        fixture.effects.agent.run_agent.side_effect = agent
        fixture.effects.publication.command.side_effect = lambda arguments, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps({'body': '', **self.pull_request}))
        effects = replace(fixture.effects, state=state, workspace=workspace, publication=publication,
            checkpoint=execution_checkpoint_state.ExecutionCheckpointRepository(self.connection))
        handler = ExecutionHandlers(effects, repo=self.repo, repo_path=Path('/repo'),
            state_dir=Path('/state'), base='main', base_sha=self.base, provider='codex',
            config_path=Path('/config'), newly_activated=False)
        return handler, fixture

    def test_general_adoption_runs_real_worker_and_publishes_its_validated_successor(self):
        self._resume_general_pr_candidate()
        adopted = self.head
        handler, fixture = self._adopted_candidate_execution()
        receipt = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106')
        result, _ = self._enter_general_pr_candidate(handler)
        self.assertEqual(result, 0, self.plan()['last_error'])
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertEqual([call.args[4] for call in fixture.effects.agent.run_agent.call_args_list],
                         ['worker', 'reviewer'])
        self.assertEqual(fixture.effects.agent.run_agent.call_args_list[0].kwargs['session_id'], 'session')
        fixture.effects.validation.verify.assert_called_once()
        fixture.effects.publication.command.assert_called_once()
        self.assertEqual(fixture.effects.publication.command.call_args.args[0][:3], ['gh', 'pr', 'view'])
        self.assertEqual(self.pushes, [('push',
            f"--force-with-lease=refs/heads/{self.plan()['branch']}:{adopted}",
            'origin', f"{self.head}:refs/heads/{self.plan()['branch']}")])
        self.assertNotEqual(self.head, adopted)
        self.assertEqual(self.plan()['pr_url'], 'saved-pr')
        self.assertEqual(self.plan()['publication_remote_oid'], self.head)
        self.assertEqual(self.plan()['pr_head_oid'], self.head)
        self.assertEqual(self.plan()['worker_session_id'], 'session')
        checkpoint = handler.effects.checkpoint.get(self.repo, 'G-106')
        self.assertIsNone(checkpoint.operation)
        self.assertEqual(checkpoint.snapshot['pushed_checkpoint_head'], self.head)
        self.assertEqual(blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106'), receipt)

    def test_general_candidate_push_interruption_recovers_at_project_entry(self):
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        # Interrupt after the physical push, leaving its durable intent.
        push = handler.effects.publication.push_owned_pr_conflict_recovery
        saved = push.side_effect
        def interrupted(*args):
            push.side_effect = saved
            saved(*args)
            raise KeyboardInterrupt
        push.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self._enter_general_pr_candidate(handler)
        self.assertEqual(self.plan()['status'], 'PUBLISHING')
        self.assertEqual(len(self.pushes), 1)
        result, _ = self._enter_general_pr_candidate(handler)
        self.assertEqual(result, 0, self.plan()['last_error'])
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.plan()['publication_remote_oid'], self.head)
        self.assertIsNone(handler.effects.checkpoint.get(self.repo, 'G-106').operation)

    def test_general_candidate_before_push_restart_preserves_approval_and_publishes(self):
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        push = handler.effects.publication.push_owned_pr_conflict_recovery
        saved = push.side_effect
        push.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._enter_general_pr_candidate(handler)
        self.assertEqual(self.plan()['status'], 'PUBLISHING')
        self.assertNotEqual(self.pull_request['headRefOid'], self.head)
        self.assertIsNotNone(self.plan()['publication_token'])
        self.assertEqual(self.pushes, [])
        push.side_effect = saved
        result, _ = self._enter_general_pr_candidate(handler)
        self.assertEqual(result, 0, self.plan()['last_error'])
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual([call.args[4] for call in fixture.effects.agent.run_agent.call_args_list],
                         ['worker', 'reviewer'])
        fixture.effects.validation.verify.assert_called_once()

    def test_general_final_push_closed_and_reopened_confirms_receipt_before_merge_wait(self):
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        push = handler.effects.publication.push_owned_pr_conflict_recovery
        saved = push.side_effect
        def interrupted(*args):
            saved(*args)
            raise KeyboardInterrupt
        push.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self._enter_general_pr_candidate(handler)
        approval = self.plan()['publication_token']
        self.pull_request['state'] = 'CLOSED'
        result, _ = self._enter_general_pr_candidate(handler)
        self.assertEqual(result, 1)
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertEqual(self.plan()['blocked_reason'], 'pull_request_closed')
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.pull_request['state'] = 'OPEN'
        result, _ = self._enter_general_pr_candidate(handler, observe_waiting=True)
        self.assertEqual(result, 0, self.plan()['last_error'])
        self.assertEqual(self.waiting_resume_status, 'PUBLISHING')
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertEqual(self.plan()['publication_token'], approval)
        push.side_effect = saved
        result, _ = self._enter_general_pr_candidate(handler)
        self.assertEqual(result, 0, self.plan()['last_error'])
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertIsNone(handler.effects.checkpoint.get(self.repo, 'G-106').operation)
        self.assertEqual(self.plan()['publication_remote_oid'], self.head)
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual([call.args[4] for call in fixture.effects.agent.run_agent.call_args_list],
                         ['worker', 'reviewer'])
        fixture.effects.validation.verify.assert_called_once()

    def test_general_push_receipt_survives_post_push_candidate_invalidation(self):
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        saved_guard = handler._post_effect_guard
        def invalidate(context):
            if self.pushes:
                handler.effects.state.update_project(self.repo, phase='WORKING',
                    validated_base_sha=None, worker_input_token=None, archive_token=None,
                    verify_token=None, review_token=None, publication_token=None)
                raise KeyboardInterrupt
            return saved_guard(context)
        with mock.patch.object(handler, '_post_effect_guard', side_effect=invalidate):
            with self.assertRaises(KeyboardInterrupt):
                self._enter_general_pr_candidate(handler)
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertEqual(self.plan()['publication_remote_oid'], self.head)
        self.assertEqual(self.plan()['pr_head_oid'], self.head)
        checkpoint = handler.effects.checkpoint.get(self.repo, 'G-106')
        self.assertIsNone(checkpoint.operation)
        self.assertEqual(checkpoint.snapshot['pushed_checkpoint_head'], self.head)
        fixture.effects.agent.run_agent.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._enter_general_pr_candidate(handler)
        self.assertEqual(fixture.effects.agent.run_agent.call_args.args[4], 'worker')
        self.assertEqual(fixture.effects.agent.run_agent.call_args.kwargs['session_id'], 'session')
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertEqual(len(self.pushes), 1)

    def test_general_push_receipt_rolls_back_head_and_intent_together(self):
        self._resume_general_pr_candidate()
        adopted = self.head
        handler, fixture = self._adopted_candidate_execution()
        checkpoints = handler.effects.checkpoint
        saved_record = checkpoints.record
        def interrupted(*args, **kwargs):
            completing = (kwargs['operation'] is None and kwargs['expected_head'] == 'd' * 40
                and checkpoints.get(self.repo, 'G-106').operation == 'push')
            result = saved_record(*args, **kwargs)
            if completing:
                raise KeyboardInterrupt
            return result
        with mock.patch.object(checkpoints, 'record', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self._enter_general_pr_candidate(handler)
        self.assertEqual(self.plan()['status'], 'PUBLISHING')
        self.assertEqual(self.plan()['publication_remote_oid'], adopted)
        self.assertEqual(self.plan()['pr_head_oid'], adopted)
        self.assertEqual(checkpoints.get(self.repo, 'G-106').operation, 'push')
        self.assertEqual(handler.effects.state.check().publication_remote_oid, adopted)
        self.assertEqual(self.pull_request['headRefOid'], self.head)
        result, _ = self._enter_general_pr_candidate(handler)
        self.assertEqual(result, 0, self.plan()['last_error'])
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertEqual(self.plan()['publication_remote_oid'], self.head)
        self.assertIsNone(checkpoints.get(self.repo, 'G-106').operation)
        self.assertEqual(len(self.pushes), 1)
        fixture.effects.publication.command.assert_not_called()

    def test_general_final_push_confirmation_refuses_changed_projection_owner(self):
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        push = handler.effects.publication.push_owned_pr_conflict_recovery
        saved = push.side_effect
        def interrupted(*args):
            saved(*args)
            raise KeyboardInterrupt
        push.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self._enter_general_pr_candidate(handler)
        plan, other = dict(self.plan()), dict(self.plan('G-107'))
        checkpoint = handler.effects.checkpoint.get(self.repo, 'G-106')
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        with (mock.patch.object(loop_manager, '_fetch_pr_conflict_recovery_refs',
                                side_effect=lambda *_: runtime.hydrate(self.repo, 'G-107')),
              mock.patch.object(loop_manager, 'remote_branch_oid', return_value=self.head)):
            with self.assertRaises(loop_manager.PublicationSafetyError):
                loop_manager.recover_confirmed_pr_conflict_push(self.connection, Path('/repo'),
                    Path('/state'), self.repo, project, self.pull_request)
        self.assertEqual(dict(self.plan()), plan)
        self.assertEqual(dict(self.plan('G-107')), other)
        self.assertEqual(runtime.projected(self.repo).plan_id, 'G-107')
        self.assertEqual(handler.effects.checkpoint.get(self.repo, 'G-106'), checkpoint)
        self.assertEqual(len(self.pushes), 1)

    def test_current_resource_ownership_ignores_old_receipts_and_rechecks_live_pr(self):
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        context = handler.prepare()
        checkpoint = handler.effects.checkpoint.get(self.repo, 'G-106')
        for damage in ('receipt', 'tree', 'accepted', 'scope', 'last-adopted', 'pr', 'rewrite'):
            with self.subTest(damage=damage):
                snapshot, pr = dict(checkpoint.snapshot), dict(self.pull_request)
                original = dict(self.connection.execute('SELECT * FROM blocked_checkpoints').fetchone())
                if damage in {'receipt', 'tree', 'accepted'}:
                    field = {'receipt': 'commit_identity', 'tree': 'tree_oid',
                             'accepted': 'accepted_remote_oid'}[damage]
                    self.connection.execute(f"UPDATE blocked_checkpoints SET {field} = 'foreign' WHERE plan_id = 'G-106'")
                elif damage == 'scope':
                    snapshot['remote_adoption_scope'] = {**snapshot['remote_adoption_scope'], 'branch': 'foreign'}
                elif damage == 'last-adopted':
                    snapshot['last_adopted_remote_oid'] = 'foreign'
                elif damage == 'pr':
                    pr['url'] = 'foreign'
                else:
                    self.ancestry = False
                self.assertEqual(handler._owned_checkpoint_remote(context, handler._row(), self.head,
                    replace(checkpoint, snapshot=snapshot), pr), damage != 'pr')
                self.ancestry = True
                self.connection.execute(
                    "UPDATE blocked_checkpoints SET commit_identity = ?, tree_oid = ?, accepted_remote_oid = ? WHERE plan_id = 'G-106'",
                    (original['commit_identity'], original['tree_oid'], original['accepted_remote_oid']))
        git_port = handler.effects.workspace.git
        physical_git = git_port.side_effect
        for code, output in ((1, 'temporary query failure'), (0, 'malformed response')):
            with self.subTest(remote_response=output):
                def unavailable(path, *args, **kwargs):
                    if args[0] == 'ls-remote':
                        return SimpleNamespace(returncode=code, stdout=output)
                    return physical_git(path, *args, **kwargs)
                git_port.side_effect = unavailable
                try:
                    with self.assertRaises(RecoveryFailure) as caught:
                        handler._owned_checkpoint_remote(context, handler._row(), self.head,
                            checkpoint, self.pull_request)
                finally:
                    git_port.side_effect = physical_git
                self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        previous_push, next_head = 'd' * 40, '1' * 40
        handler.effects.state.update_project(self.repo,
            publication_remote_oid=previous_push, pr_head_oid=previous_push)
        self.head = self.pull_request['headRefOid'] = next_head
        pending = replace(checkpoint, operation='push', expected_head=next_head,
            snapshot={**checkpoint.snapshot, 'pushed_checkpoint_head': previous_push,
                      'remote_before': previous_push})
        self.assertTrue(handler._owned_checkpoint_remote(context, handler._row(), next_head,
            pending, self.pull_request))
        self.ancestry = False
        self.assertTrue(handler._owned_checkpoint_remote(context, handler._row(), next_head,
            pending, self.pull_request))
        fixture.effects.agent.run_agent.assert_not_called()
        self.assertEqual(self.pushes, [])

    def test_general_adoption_commit_interruption_keeps_scope_through_real_recovery(self):
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        checkpoints = handler.effects.checkpoint
        saved_record = checkpoints.record
        def interrupted(*args, **kwargs):
            if (kwargs['operation'] is None and kwargs['expected_head'] == 'd' * 40
                    and checkpoints.get(self.repo, 'G-106').operation == 'commit'):
                raise KeyboardInterrupt
            return saved_record(*args, **kwargs)
        with mock.patch.object(checkpoints, 'record', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self._enter_general_pr_candidate(handler)
        current = checkpoints.get(self.repo, 'G-106')
        self.assertEqual(current.operation, 'commit')
        context = handler.prepare()
        self.assertTrue(handler._owned_checkpoint_remote(context, handler._row(),
            self.pull_request['headRefOid'], current, self.pull_request))
        recovered = handler.recover(context)
        self.assertEqual(recovered.phase.value, 'VERIFYING')
        current = checkpoints.get(self.repo, 'G-106')
        self.assertIsNone(current.operation)
        self.assertIn('remote_adoption_scope', current.snapshot)
        result, _ = self._enter_general_pr_candidate(handler)
        self.assertEqual(result, 0, self.plan()['last_error'])
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertEqual(len(self.pushes), 1)
        fixture.effects.publication.command.assert_called_once()
        self.assertEqual(fixture.effects.publication.command.call_args.args[0][:3], ['gh', 'pr', 'view'])

    def test_general_pr_push_refuses_rewrites_and_changed_remote_or_pr(self):
        self._resume_general_pr_candidate()
        handler, fixture = self._adopted_candidate_execution()
        handler.effects.state.update_project(self.repo, phase='PUBLISHING', validated_base_sha=self.base)
        self.head = 'd' * 40
        original = dict(self.pull_request)
        for damage in ('rewrite', 'remote', 'closed', 'url', 'branch', 'base'):
            with self.subTest(damage=damage):
                def race(*_):
                    if damage == 'remote':
                        self.pull_request['headRefOid'] = 'f' * 40
                    elif damage != 'rewrite':
                        field, value = {'closed': ('state', 'CLOSED'), 'url': ('url', 'foreign'),
                            'branch': ('headRefName', 'foreign'), 'base': ('baseRefName', 'foreign')}[damage]
                        self.pull_request[field] = value
                self.ancestry = damage != 'rewrite'
                with (mock.patch.object(loop_manager, '_fetch_pr_conflict_recovery_refs', side_effect=race),
                      mock.patch.object(loop_manager, 'remote_branch_oid',
                                        side_effect=lambda *_: self.pull_request['headRefOid'])):
                    with self.assertRaises((loop_manager.PublicationSafetyError, recovery_runtime.RecoveryFailure)):
                        loop_manager.push_owned_pr_conflict_recovery(self.connection,
                            Path('/repo'), Path('/state'), self.repo, self.plan()['branch'],
                            Path(self.plan()['worktree']), original, self.head)
                self.pull_request = dict(original)
                self.ancestry = True
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.plan()['status'], 'PUBLISHING')
        self.assertEqual(self.plan()['publication_remote_oid'], original['headRefOid'])
        fixture.effects.publication.command.assert_not_called()

    def test_user_push_without_base_rebase_reenters_worker_before_merge_wait(self):
        receipt = self._resume_general_pr_candidate()
        other = dict(self.plan('G-107'))
        for _ in range(2):
            result, execution = self._enter_general_pr_candidate()
            self.assertEqual(result, 0)
            execution.run.assert_called_once()
            context = execution.run.call_args.args[0]
            self.assertEqual(context.phase.value, 'IMPLEMENTING')
            self.assertEqual(context.worker_session_id, 'session')
            for token in ('worker_input_token', 'verify_token', 'review_token', 'publication_token'):
                self.assertIsNone(getattr(context, token))
            execution.verify.assert_not_called()
            execution.review.assert_not_called()
            execution.publish.assert_not_called()
            self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
            self.assertEqual(self.plan()['pr_url'], 'saved-pr')
            self.assertEqual(blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106'), receipt)
            self.assertEqual(dict(self.plan('G-107')), other)

    def test_general_checkpoint_preserves_each_validation_stage_until_final_approval(self):
        self._resume_general_pr_candidate()
        for phase, stage in (('VERIFYING', 'verify'), ('REVIEWING', 'review'), ('PUBLISHING', 'publish')):
            with self.subTest(phase=phase):
                loop_manager.update_project(self.connection, self.repo, phase=phase)
                result, execution = self._enter_general_pr_candidate()
                self.assertEqual(result, 0)
                getattr(execution, stage).assert_called_once()
                self.assertEqual(self.plan()['status'], phase)
                self.assertIsNone(self.plan()['publication_token'])

    def test_general_checkpoint_pr_head_race_retries_before_worker(self):
        receipt = self._resume_general_pr_candidate()
        self.pull_request['headRefOid'] = 'unexpected-head'
        with mock.patch('loop_manager_internal.git.manager_git_journal.live_remote_branch_oid', return_value=self.head):
            result, execution = self._enter_general_pr_candidate()
        self.assertEqual(result, 2)
        execution.prepare.assert_not_called()
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.plan()['pr_head_oid'], self.head)
        self.assertEqual(blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106'), receipt)

    def test_general_checkpoint_with_final_publication_approval_enters_merge_wait(self):
        self._resume_general_pr_candidate()
        loop_manager.update_project(self.connection, self.repo,
            phase='PUBLISHING', publication_token='current-final-approval')
        result, execution = self._enter_general_pr_candidate()
        self.assertEqual(result, 0)
        execution.prepare.assert_not_called()
        self.assertEqual(self.plan()['status'], 'PR_OPEN')
        self.assertEqual(self.plan()['pr_url'], 'saved-pr')

    def _approved_final_push(self):
        from tests.support.publication import seed_validated_candidate
        self._resume_general_pr_candidate()
        before = self.head
        self.head = self.pull_request['headRefOid'] = 'd' * 40
        plan = self.plan()
        with (mock.patch('tests.support.publication.git_command', side_effect=[self.base, self.head]),
              mock.patch.object(loop_manager, '_revision_execplan_body_hash', return_value='body'),
              mock.patch.object(loop_manager, '_revision_execplan_source_fingerprint', return_value='sources')):
            approval = seed_validated_candidate(self.connection, Path(self.repo), Path(plan['worktree']), 'G-106')
        loop_manager.update_project(self.connection, self.repo,
            phase='PUBLISHING', active_sha=self.base, validated_base_sha=self.base,
            pr_conflict_recovery=1, pr_conflict_head_oid=before, pr_conflict_base_oid=self.base,
            publication_token=approval.publication_token(self.base, self.head))

    def test_approved_final_push_recovers_at_project_entry_without_repush(self):
        self._approved_final_push()
        for _ in range(2):
            result, execution = self._enter_general_pr_candidate()
            self.assertEqual(result, 0)
            execution.prepare.assert_not_called()
            plan = self.plan()
            self.assertEqual(plan['status'], 'PR_OPEN')
            self.assertEqual(plan['publication_remote_oid'], self.head)
            self.assertEqual(plan['pr_head_oid'], self.head)
            self.assertEqual(plan['pr_conflict_recovery'], 0)
            self.assertIsNone(plan['pr_conflict_head_oid'])
            self.assertIsNone(plan['pr_conflict_base_oid'])
        self.assertFalse(any(call.args[1:2] == ('push',) for call in loop_manager.git.call_args_list))

    def test_final_push_ownership_failure_preserves_publishing_and_recovery(self):
        self._approved_final_push()
        before = dict(self.plan())
        for port, damage in (('validate_execplan_workspace_identity', 'missing-worktree'),
                             ('validate_managed_worktree', 'wrong-branch')):
            with self.connection:
                scoped_blocking.clear_project_block(self.connection, self.repo)
            loop_manager.update_project(self.connection, self.repo, phase='PUBLISHING')
            with self.subTest(damage=damage), mock.patch.object(loop_manager, port,
                    side_effect=loop_manager.WorkspaceOwnershipError(damage)) as validate:
                result, execution = self._enter_general_pr_candidate()
                self.assertEqual(result, 1)
                validate.assert_called_once()
                execution.prepare.assert_not_called()
                plan = self.plan()
                self.assertEqual(plan['status'], 'PUBLISHING')
                self.assertIn('PR 충돌 복구 안전 검증 실패', plan['last_error'])
                for field in ('pr_url', 'pr_head_oid', 'publication_remote_oid', 'pr_conflict_recovery',
                              'pr_conflict_head_oid', 'pr_conflict_base_oid', 'publication_token'):
                    self.assertEqual(plan[field], before[field])
        self.assertFalse(any(call.args[1:2] == ('push',) for call in loop_manager.git.call_args_list))

    def test_final_push_confirmation_rejects_missing_mismatched_or_pending_approval(self):
        from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
        self._approved_final_push()
        approval = self.plan()['publication_token']
        definition = DefinitionSnapshot.deserialize(self.plan()['definition_snapshot'])
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        for condition in ('missing', 'mismatched', 'stale', 'rebase', 'rebase_publish'):
            with self.subTest(condition=condition):
                loop_manager.update_project(self.connection, self.repo,
                    publication_token=None if condition == 'missing' else approval)
                if condition == 'mismatched':
                    self.connection.execute("UPDATE projects SET publication_token = 'other-approval'")
                    self.connection.commit()
                elif condition == 'stale':
                    loop_manager.update_project(self.connection, self.repo,
                        publication_token=definition.publication_token(self.base, 'previous-head'))
                current = checkpoints.get(self.repo, 'G-106')
                checkpoints.record(self.repo, 'G-106', expected_generation=current.generation,
                    operation=condition if condition in {'rebase', 'rebase_publish'} else None,
                    expected_head=current.expected_head, target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid, snapshot=current.snapshot)
                plan, checkpoint = dict(self.plan()), checkpoints.get(self.repo, 'G-106')
                project = self.connection.execute('SELECT * FROM projects').fetchone()
                with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity') as workspace:
                    self.assertFalse(loop_manager.recover_confirmed_pr_conflict_push(
                        self.connection, Path('/repo'), Path('/state'), self.repo, project, self.pull_request))
                    workspace.assert_not_called()
                self.assertEqual(dict(self.plan()), plan)
                self.assertEqual(checkpoints.get(self.repo, 'G-106'), checkpoint)

    def test_user_push_rebase_publication_and_cause_recheck_resume_same_pr_recovery(self):
        from loop_manager_internal.git import managed_rebase_adoption, managed_rebase_validation
        from tests.support.rebase_observations import expectation, observation, scope_for
        original = self.published_successor()
        successor, rebased = self.row()['branch_oid'], 'd' * 40
        self.remote = successor
        self.connection.execute(
            "UPDATE exec_plans SET pr_url = 'saved-pr', pr_head_oid = ?, publication_remote_oid = ?, "
            "pr_conflict_recovery = 1, pr_conflict_head_oid = ?, pr_conflict_base_oid = ?, "
            "verify_token = 'old-verify', review_token = 'old-review' WHERE plan_id = 'G-106'",
            (original.head, original.head, original.head, self.base))
        self.connection.execute("UPDATE blocked_checkpoints SET pr_url = 'saved-pr' WHERE plan_id = 'G-106'")
        self.connection.commit()
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        self.ports.hydrate_execplan_project.side_effect = loop_manager.hydrate_execplan_project
        self.ports._complete_direct_rebase_checkpoint.side_effect = loop_manager._complete_direct_rebase_checkpoint
        self.ports.pr_for_branch.side_effect = lambda *_: {
            'url': 'saved-pr', 'state': 'OPEN', 'headRefName': self.plan()['branch'],
            'headRefOid': self.remote, 'baseRefName': 'main'}
        original_git = self.ports.git.side_effect
        events, pushes = [], []
        def git(path, *args, **kwargs):
            if args == ('merge-base', '--is-ancestor', self.base, 'HEAD'):
                return SimpleNamespace(returncode=1, stdout='')
            if args[:1] == ('ls-remote',):
                return SimpleNamespace(returncode=0, stdout=f'{self.remote}\trefs/heads/{self.plan()["branch"]}\n')
            if args[:1] == ('push',):
                events.append('push')
                pushes.append(args)
                self.remote = self.head
                return SimpleNamespace(returncode=0, stdout='pushed')
            return original_git(path, *args, **kwargs)
        def rebase(connection, repo_path, repo, plan_id, worktree, branch, target, **options):
            events.append('rebase')
            adopted = self.plan()
            self.assertEqual(adopted['pr_head_oid'], successor)
            self.assertEqual(adopted['pr_conflict_head_oid'], successor)
            current = checkpoints.get(repo, plan_id)
            checkpoints.record(repo, plan_id, expected_generation=current.generation,
                operation='rebase', expected_head=self.head, target_base_oid=target,
                observed_remote_oid=self.remote, snapshot=current.snapshot)
            self.head = rebased
            self.current_snapshot = replace(self.current_snapshot, head=rebased,
                refs={**self.current_snapshot.refs, f'refs/heads/{branch}': (rebased, '')},
                worktrees={str(worktree): (f'HEAD={rebased}', f'branch=refs/heads/{branch}')})
            return SimpleNamespace(returncode=0, stdout='rebased')
        def current_service(*_args, **options):
            current, plan = checkpoints.get(self.repo, 'G-106'), self.plan()
            remote_oid = options.get('remote_oid', plan['publication_remote_oid'] or current.observed_remote_oid)
            expected = expectation(repo=self.repo, repository_identity='example/repo', plan_id='G-106',
                plan_path=plan['path'], worktree=plan['worktree'], branch=plan['branch'],
                target_base_oid=current.target_base_oid or self.base, generation=current.generation,
                before_head=current.expected_head or original.head,
                remote=managed_rebase_validation.Remote('example/repo', plan['branch'], remote_oid,
                    managed_rebase_validation.PullRequest('saved-pr', 'example/repo', plan['branch'], remote_oid, 'OPEN')))
            return managed_rebase_adoption.ManagedRebaseService(expected, mock.Mock())
        def observe(expected, _effects):
            actual = observation(expected, head=self.head, branch_ref=(self.head, ''),
                registration=(f'HEAD={self.head}', f'branch=refs/heads/{expected.branch}'),
                remote=replace(expected.remote, oid=self.remote,
                    pull_request=replace(expected.remote.pull_request, head=self.remote)))
            return actual, scope_for(expected, actual)
        def evaluate(*_args):
            events.append('cause-recheck')
            projected = runtime.projected(self.repo)
            self.assertEqual(projected.plan_id, 'G-106')
            self.assertEqual(projected.pr_conflict_recovery, 1)
            self.assertEqual(projected.pr_head_oid, rebased)
            receipt = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106')
            self.assertEqual(receipt.remote_oid, rebased)
            self.assertEqual(receipt.local_oid, rebased)
            return True, 'current candidate resolves cause', 'verify'
        self.ports.git.side_effect = git
        self.ports._journaled_direct_rebase.side_effect = rebase
        self.ports._current_rebase_service.side_effect = current_service
        self.ports._evaluate_remote_checkpoint_cause.side_effect = evaluate
        other = dict(self.plan('G-107'))
        for patch in (
                mock.patch.object(loop_manager, 'git', side_effect=git),
                mock.patch.object(loop_manager, 'pr_for_branch', side_effect=self.ports.pr_for_branch),
                mock.patch.object(loop_manager, '_current_rebase_service', side_effect=current_service),
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
                mock.patch.object(checkpoint_ownership, 'owned_remote_oid', side_effect=lambda *args, **_kwargs: args[6]),
                mock.patch.object(managed_rebase_adoption, 'observe_current_rebase', side_effect=observe),
                mock.patch('loop_manager_internal.git.manager_git_journal.live_remote_branch_oid', side_effect=lambda *_: self.remote),
                mock.patch.object(blocked_remote_observation, 'observe_refs', side_effect=lambda *_: (
                    blocked_remote_observation.RefObservation('present', self.base),
                    blocked_remote_observation.RefObservation('present', self.remote)))):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(self.coordinator.reconcile('G-106'), 'resumed', self.plan()['last_error'])
        self.assertEqual(events, ['rebase', 'push', 'cause-recheck'])
        self.assertEqual(pushes, [('push', f'--force-with-lease=refs/heads/{self.plan()["branch"]}:{successor}',
                                 'origin', f'{rebased}:refs/heads/{self.plan()["branch"]}')])
        resumed = runtime.projected(self.repo)
        self.assertEqual(resumed.status, 'IMPLEMENTING')
        self.assertEqual(resumed.pr_url, 'saved-pr')
        self.assertEqual(resumed.pr_head_oid, rebased)
        self.assertEqual(resumed.worker_session_id, 'session')
        self.assertIsNone(resumed.verify_token)
        self.assertIsNone(resumed.review_token)
        self.assertIsNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
        self.assertIsNone(checkpoints.get(self.repo, 'G-106').operation)
        self.assertEqual(dict(self.plan('G-107')), other)
        self.assertEqual(self.connection.execute('SELECT stage FROM blocked_plan_reconciliations').fetchone()[0], 'complete')


class PendingIntegrationRemoteRefreshTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def _context(self, phase, *, operation=None, owned=True, base_pending=False):
        plan = self.plan()
        context = dict(self.row())
        context.update(plan_id='G-106', branch_oid='previous-user-input',
                       identity={field: plan[field] for field in blocked_plan_reconciliation.IDENTITY_FIELDS})
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        current = store.get(self.repo, 'G-106')
        store.record(self.repo, 'G-106', expected_generation=current.generation,
            operation=operation, expected_head=self.head, target_base_oid='previous-user-input',
            observed_remote_oid='previous-user-input', snapshot={
                'base_rebase_restart': {'phase': 'complete', 'remote': 'previous-user-input'},
                'remote_adoption_intent': {'head': 'previous-user-input',
                    'branch': plan['branch'] if owned else 'other', 'worktree': plan['worktree']}}
                if base_pending else {'work_branch_integration': {
                'phase': phase, 'remote': 'previous-user-input',
                'branch': plan['branch'] if owned else 'other', 'worktree': plan['worktree']}}
                if phase is not None else {})
        return context

    def _new_remote(self, *, ancestry=0, fetch=0):
        successor, base = 'successor', 'new-base'
        probe = mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
            blocked_remote_observation.RefObservation('present', base),
            blocked_remote_observation.RefObservation('present', successor)))
        probe.start()
        self.addCleanup(probe.stop)
        def git(root, *args, **kwargs):
            if args[0] == 'fetch':
                return SimpleNamespace(returncode=fetch, stdout='fetch failed' if fetch else '')
            if args == ('rev-parse', 'FETCH_HEAD^{commit}'):
                return SimpleNamespace(returncode=0, stdout=successor)
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(returncode=ancestry, stdout='')
            return self.git(root, *args, **kwargs)
        self.ports.git.side_effect = git

    def test_pending_preservation_and_rebase_start_reobserve_successor_and_base(self):
        for phase in ('preserving', 'rebasing'):
            with self.subTest(phase=phase):
                context = self._context(phase)
                self._new_remote()
                self.coordinator._remote(context)
                self.assertEqual((context['branch_oid'], context['base_oid']), ('successor', 'new-base'))
                self.ports.git.assert_any_call(Path('/repo'), 'merge-base', '--is-ancestor',
                                              'previous-user-input', 'successor', check=False)

    def test_completed_base_pending_adoption_reobserves_successor_and_new_base(self):
        context = self._context(None, base_pending=True)
        self._new_remote()
        self.coordinator._remote(context)
        self.assertEqual((context['branch_oid'], context['base_oid']), ('successor', 'new-base'))

    def test_completed_base_pending_adoption_rejects_rewrite_foreign_owner_and_other_operation(self):
        for options, ancestry in (({}, 1), ({'owned': False}, 0), ({'operation': 'commit'}, 0)):
            with self.subTest(options=options, ancestry=ancestry):
                context = self._context(None, base_pending=True, **options)
                self._new_remote(ancestry=ancestry)
                with self.assertRaises(RuntimeError):
                    self.coordinator._remote(context)
                self.assertEqual(context['branch_oid'], 'previous-user-input')

    def test_pending_refresh_rejects_rewrite_foreign_intent_and_other_operations(self):
        for options, ancestry, fetch in (({}, 1, 0), ({'owned': False}, 0, 0),
                                        ({'operation': 'commit'}, 0, 0), ({}, 0, 1)):
            with self.subTest(options=options, ancestry=ancestry, fetch=fetch):
                context = self._context('preserving', **options)
                self._new_remote(ancestry=ancestry, fetch=fetch)
                with self.assertRaises(RuntimeError):
                    self.coordinator._remote(context)
                self.assertEqual(context['branch_oid'], 'previous-user-input')

    def test_base_conflict_without_integration_reobserves_successor_and_retains_unchanged_lease_target(self):
        context = self._context(None, operation='rebase')
        self._new_remote()
        self.coordinator._remote(context)
        self.assertEqual((context['branch_oid'], context['base_oid']), ('successor', 'new-base'))
        context = self._context(None, operation='rebase')
        with mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
                blocked_remote_observation.RefObservation('present', 'new-base'),
                blocked_remote_observation.RefObservation('present', 'previous-user-input'))):
            with self.assertRaises(blocked_plan_reconciliation.InputChanged):
                self.coordinator._remote(context)
        self.assertEqual(context['base_oid'], self.base)


class PlanCheckpointPublicationRaceTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def test_transient_pr_lookup_keeps_blocked_input_pending(self):
        self.ports.pr_for_branch.side_effect = RuntimeError('temporary API failure')
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.connection.execute(
            "SELECT stage FROM blocked_plan_reconciliations WHERE plan_id = 'G-106'").fetchone()[0], 'observed')
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        self.assertEqual(scoped_blocking.plan_block(
            self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')

    def test_final_plan_release_rejects_changed_cause_identity(self):
        plan = self.plan()
        block = dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
        context = {
            'plan_id': 'G-106', 'identity': {
                field: plan[field] for field in blocked_plan_reconciliation.IDENTITY_FIELDS},
            'block': block, 'base_oid': self.base, 'candidate': self.signature,
            'evaluation': {'detail': 'cause resolved'}, 'input_identity': 'candidate',
        }
        self.connection.execute(
            "UPDATE execplan_blocks SET created_at = 'later' WHERE plan_id = 'G-106'")
        self.connection.commit()
        with mock.patch.object(self.coordinator, '_candidate_unchanged'):
            with self.assertRaises(RuntimeError):
                self.coordinator._resume(plan, context)
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertIsNotNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))

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
            SimpleNamespace(head=self.head), SimpleNamespace(status=status, acceptance=None))
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
        self.ports._revision_execplan_input_fingerprint.return_value = 'pending local proposal'
        self.assertFalse(self.coordinator._evaluate(self.plan(), context)['resolved'])
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, '현재 명세가 질문을 해소했습니다.', None)
        self.assertTrue(self.coordinator._evaluate(self.plan(), context)['resolved'])
        self.assertEqual(self.ports._evaluate_remote_checkpoint_cause.call_count, 3)


    def test_uncertain_verify_process_creates_safety_block_without_publication(self):
        self.ports._evaluate_remote_checkpoint_cause.side_effect = loop_manager.VerifyTerminationUncertain('verify child is still alive')
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        publish.assert_not_called()
        self.assertEqual(scoped_blocking.project_block(self.connection, self.repo)['reason_code'], 'repository_state_unsafe')
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')


class PlanCauseRevalidationTests(PlanRemoteRecoveryFixture, unittest.TestCase):
    def test_question_assessment_checkpoint_race_keeps_the_input_retryable(self):
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan(self.repo, 'G-106', reason_code='user_decision_required', evidence='original question',
            release_fingerprint='input', clear_projection=False, plan_values={'active_sha': 'before-docs'})
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        from loop_manager_internal.storage import planning_review_state as reviews
        with self.connection:
            reviews.begin(self.connection, self.repo, purpose='delta', revision=self.base,
                input_fingerprint='new', specification_fingerprint='spec', work_cycle='cycle', comparison_revision=None)
            reviews.validate(self.connection, self.repo, self.base, 'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(
                revision=self.base, plans=[dict(plan_id='G-106', requirements=[dict(
                    source='docs/requirements/[R-002] Policy.md', reason='recheck question', quote='unchanged source')])])), 'reviewed')
            reviews.confirm(self.connection, self.repo, self.base, self.base)
        def assess(*_args, **_kwargs):
            current = store.get(self.repo, 'G-106')
            with self.connection:
                store.record(self.repo, 'G-106', expected_generation=current.generation,
                    operation=current.operation, expected_head=current.expected_head,
                    target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                    snapshot={**(current.snapshot or {}), 'concurrent_audit': 'preserved'})
            return 0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(dict(resolved=False, detail='still needed', evidence=[]))
        effects = SimpleNamespace(git=lambda _root, *args, **_kw: SimpleNamespace(
                returncode=0, stdout=self.base if args[0] == 'rev-parse' else
                'prior source' if args[0] == 'show' and args[1].startswith('before-docs:') else 'unchanged source'),
            run_agent=mock.Mock(side_effect=assess), capture_git_snapshot=mock.Mock(), validate_agent_scope=mock.Mock())
        with self.assertRaises(recovery_runtime.RecoveryFailure) as caught:
            _assess_user_decision_cause(effects, self.connection, self.coordinator.repo_path, self.coordinator.state_dir,
                                        self.repo, self.plan('G-106'), None)
        self.assertIs(caught.exception.reason, recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE)
        self.assertEqual(effects.run_agent.call_count, 1, self.plan()['last_error'])
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        self.assertEqual(store.get(self.repo, 'G-106').snapshot['concurrent_audit'], 'preserved')
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original question')

    def test_revised_correction_owner_resumes_worker_without_its_own_merge(self):
        from loop_manager_internal.storage import common_defect_state, runtime_state
        runtime_state.RuntimeStateRepository(self.connection).block_plan(self.repo, 'G-106',
            reason_code='common_defect_pending', evidence='shared correction', release_fingerprint='input', clear_projection=False)
        correction = dict(ready=False, owner_execution=True,
                          corrections=[dict(plan_id='G-106', merge=None, confirmed_revision=self.base)])
        independent = dict(self.plan('G-107'))
        with mock.patch.object(common_defect_state, 'reassessment', return_value=correction), \
             mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'resumed')
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertEqual(self.plan('G-106')['status'], 'IMPLEMENTING')
        self.assertEqual(dict(self.plan('G-107')), independent)
        context = dict(block=dict(reason_code='user_decision_required'), common_defect_input=correction, artifacts={})
        self.assertFalse(self.coordinator._evaluate(self.plan('G-106'), context)['resolved'])
        context.update(block=dict(reason_code='common_defect_pending'), base_oid=self.base)
        with mock.patch.object(self.ports, 'git', return_value=SimpleNamespace(returncode=1)):
            self.assertFalse(self.coordinator._evaluate(self.plan('G-106'), context)['resolved'])

    def test_verified_common_correction_rechecks_only_affected_processed_task(self):
        from loop_manager_internal.storage import common_defect_state, runtime_state
        runtime_state.RuntimeStateRepository(self.connection).block_plan(self.repo, 'G-106',
            reason_code='common_defect_pending', evidence='shared correction', release_fingerprint='input', clear_projection=False)
        correction = [dict(ready=False, corrections=[dict(plan_id='G-108', merge=None)])]
        independent = dict(self.plan('G-107'))
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, 'actual cause resolved', None)
        with mock.patch.object(common_defect_state, 'reassessment', side_effect=lambda *_: correction[0]), \
             mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
            with self.connection:
                blocked_remote_observation.mark_processed(self.connection, self.repo, 'G-106',
                                                          blocked_remote_observation.input_identity(self.row()))
            correction[0] = dict(ready=True, corrections=[dict(plan_id='G-108', merge={'id': 1})])
            self.assertEqual(self.coordinator.reconcile('G-106'), 'resumed')
            self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
        self.assertEqual(self.plan('G-106')['status'], 'IMPLEMENTING')
        self.assertEqual(dict(self.plan('G-107')), independent)

    def test_common_correction_change_during_probe_keeps_original_block(self):
        from loop_manager_internal.storage import common_defect_state, runtime_state
        runtime_state.RuntimeStateRepository(self.connection).block_plan(self.repo, 'G-106',
            reason_code='common_defect_pending', evidence='shared correction', release_fingerprint='input', clear_projection=False)
        correction = [dict(ready=True, corrections=[dict(plan_id='G-108', merge={'id': 1})])]
        def changed(*_):
            correction[0] = dict(ready=False, corrections=[dict(plan_id='G-108', merge=None)])
            return True, 'stale assessment', None
        self.ports._evaluate_remote_checkpoint_cause.side_effect = changed
        with mock.patch.object(common_defect_state, 'reassessment', side_effect=lambda *_: correction[0]), \
             mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.plan('G-106')['status'], 'PLAN_BLOCKED')
        self.assertEqual(self.plan('G-106')['blocked_reason'], 'common_defect_pending')

    def test_new_planning_input_after_interruption_finishes_owned_rebase_before_new_target(self):
        from loop_manager_internal.storage import planning_review_state as reviews
        from loop_manager_internal.catalog.execplan_catalog import plan_content_hash
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        old_target = 'c' * 40
        independent = dict(self.plan('G-107'))
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET active_sha='before-docs' WHERE plan_id='G-106'")
            store.record(self.repo, 'G-106', expected_generation=0, operation='rebase',
                expected_head=self.head, target_base_oid=old_target, observed_remote_oid=None, snapshot={'audit': 'kept'})
        with mock.patch.object(self.ports, '_complete_direct_rebase_checkpoint', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        saved = self.connection.execute("SELECT * FROM blocked_plan_reconciliations WHERE plan_id='G-106'").fetchone()
        self.assertEqual(saved['stage'], 'adopted')
        revised = (b'---\nid: G-106\ntitle: Work\nsources:\n  - docs/PRODUCT.md\ndepends_on: []\n'
                   b'complexity: low\nrisk: low\npriority: critical\n---\n\nRevised validation contract.\n')
        revised_hash = plan_content_hash(revised)
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET content_hash=?, sources=?, current_revision_sha=? "
                                    "WHERE plan_id='G-106'", (revised_hash, '["docs/PRODUCT.md"]', self.base))
            reviews.begin(self.connection, self.repo, purpose='delta', revision=self.base,
                input_fingerprint='new', specification_fingerprint='spec', work_cycle='cycle', comparison_revision=None)
            reviews.validate(self.connection, self.repo, self.base, 'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(
                revision=self.base, plans=[dict(plan_id='G-106', requirements=[dict(
                    source='docs/requirements/[R-002] Policy.md', reason='new validation policy', quote='Current policy.')])])), 'reviewed')
            reviews.confirm(self.connection, self.repo, self.base, self.base, revised_plan_ids=('G-106',))
        self.assertEqual(store.get(self.repo, 'G-106').target_base_oid, old_target)
        def document(*_args):
            self.assertFalse(self.connection.in_transaction)
            return revised
        self.ports.git_bytes.side_effect = document
        for case in ('mismatched_blob', 'mismatched_dependency', 'changed_branch', 'checkpoint_race'):
            with self.subTest(case=case):
                def lookup(*_args):
                    self.assertFalse(self.connection.in_transaction)
                    if case == 'checkpoint_race':
                        current = store.get(self.repo, 'G-106')
                        with self.connection:
                            store.record(self.repo, 'G-106', expected_generation=current.generation,
                                operation=current.operation, expected_head=current.expected_head,
                                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                                snapshot={**current.snapshot, 'concurrent_audit': 'kept'})
                    return revised + b'unconfirmed edit' if case == 'mismatched_blob' else revised
                self.ports.git_bytes.side_effect = lookup
                with self.connection:
                    if case == 'mismatched_dependency':
                        self.connection.execute("UPDATE exec_plans SET depends_on='[\"G-107\"]' WHERE plan_id='G-106'")
                    elif case == 'changed_branch':
                        self.connection.execute("UPDATE exec_plans SET branch='unowned-branch' WHERE plan_id='G-106'")
                self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred' if case == 'checkpoint_race' else 'blocked')
                self.assertEqual(store.get(self.repo, 'G-106').operation, 'rebase')
                self.assertEqual(store.get(self.repo, 'G-106').target_base_oid, old_target)
                current_context = self.connection.execute("SELECT context FROM blocked_plan_reconciliations WHERE plan_id='G-106'").fetchone()[0]
                self.assertEqual(json.loads(current_context)['identity'], json.loads(saved['context'])['identity'])
                with self.connection:
                    self.connection.execute("UPDATE exec_plans SET depends_on='[]', branch='loop-manager/G-106' WHERE plan_id='G-106'")
        self.ports.git_bytes.side_effect = document
        steps = []
        def finish(*_args, **_kwargs):
            current = store.get(self.repo, 'G-106')
            steps.append(('complete', current.target_base_oid))
            with self.connection:
                store.record(self.repo, 'G-106', expected_generation=current.generation, operation=None,
                    expected_head=self.head, target_base_oid=current.target_base_oid,
                    observed_remote_oid=None, snapshot=current.snapshot)
        def start(*args, **_kwargs):
            steps.append(('start', args[6]))
            current = store.get(self.repo, 'G-106')
            with self.connection:
                store.record(self.repo, 'G-106', expected_generation=current.generation, operation='rebase',
                    expected_head=self.head, target_base_oid=args[6], observed_remote_oid=None, snapshot=current.snapshot)
            return SimpleNamespace(returncode=0, stdout='rebased')
        self.ports._complete_direct_rebase_checkpoint.side_effect = finish
        self.ports._journaled_direct_rebase.side_effect = start
        self.ports.git.side_effect = lambda path, *args, **kwargs: (
            SimpleNamespace(returncode=0 if args[-1] == self.base else 1, stdout='')
            if args[:1] == ('merge-base',) else self.git(path, *args, **kwargs))
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(steps, [('complete', old_target), ('start', self.base), ('complete', self.base)])
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
        self.assertIsNone(store.get(self.repo, 'G-106').operation)
        self.assertEqual(store.get(self.repo, 'G-106').snapshot['audit'], 'kept')
        self.assertEqual(store.get(self.repo, 'G-106').snapshot['concurrent_audit'], 'kept')
        self.assertEqual(dict(self.plan('G-107')), independent)
        final_context = json.loads(self.connection.execute(
            "SELECT context FROM blocked_plan_reconciliations WHERE plan_id='G-106'").fetchone()[0])
        self.assertEqual(final_context['identity']['content_hash'], revised_hash)
        self.assertEqual(final_context['identity']['sources'], '["docs/PRODUCT.md"]')
        self.assertEqual(final_context['comparison_revision'], 'before-docs')

    def test_question_assessment_uses_pre_rebase_revision_after_active_sha_advances(self):
        from loop_manager_internal.storage import planning_review_state as reviews, runtime_state
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan(self.repo, 'G-106', reason_code='user_decision_required', evidence='original question',
            release_fingerprint='input', clear_projection=False, plan_values={'active_sha': 'before-docs'})
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        source = 'docs/requirements/[R-002] Policy.md'
        policy = 'Ordinary calls and mock checks are sufficient.'
        with self.connection:
            store.record(self.repo, 'G-106', expected_generation=0, operation='rebase', expected_head=self.head,
                target_base_oid=self.base, observed_remote_oid=None, snapshot={})
            reviews.begin(self.connection, self.repo, purpose='delta', revision=self.base,
                input_fingerprint='new', specification_fingerprint='spec', work_cycle='cycle', comparison_revision=None)
            reviews.validate(self.connection, self.repo, self.base, 'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(
                revision=self.base, plans=[dict(plan_id='G-106', requirements=[dict(
                    source=source, reason='resolves validation choice', quote=policy)])])), 'reviewed')
            reviews.confirm(self.connection, self.repo, self.base, self.base)
        def finish(*_args, **_kwargs):
            current = store.get(self.repo, 'G-106')
            with self.connection:
                store.record(self.repo, 'G-106', expected_generation=current.generation, operation=None,
                    expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot=current.snapshot)
                runtime.update_plan(self.repo, 'G-106', plan_values={'active_sha': self.base})
        self.ports._complete_direct_rebase_checkpoint.side_effect = finish
        def git(_root, *args, **_kwargs):
            if args[0] == 'rev-parse':
                return SimpleNamespace(returncode=0, stdout=self.base)
            if args[0] in {'merge-base', 'diff'}:
                return SimpleNamespace(returncode=0, stdout='')
            if args[0] == 'show':
                revision, path = args[1].split(':', 1)
                text = (policy if revision == self.base else 'Choose a validation strategy.') if path == source else 'Unchanged document.'
                return SimpleNamespace(returncode=0, stdout=text)
            raise AssertionError(args)
        agent = mock.Mock(return_value=(0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(dict(
            resolved=True, detail='Current policy resolves the choice.', evidence=[dict(source=source, quote=policy)]))))
        effects = SimpleNamespace(git=git, run_agent=agent, capture_git_snapshot=mock.Mock(), validate_agent_scope=mock.Mock())
        self.ports._evaluate_remote_checkpoint_cause.side_effect = lambda db, root, state, repo, plan, *_args: (
            _assess_user_decision_cause(effects, db, root, state, repo, plan, None))
        self.assertEqual(self.coordinator.reconcile('G-106'), 'resumed')
        agent.assert_called_once()
        payload = json.loads(agent.call_args.args[5].splitlines()[-1])
        self.assertEqual(payload['prior_revision'], 'before-docs')
        self.assertIn('+Ordinary calls', payload['changes'][source])
        self.assertEqual(self.plan()['active_sha'], self.base)
        self.assertEqual(store.get(self.repo, 'G-106').snapshot['user_decision_assessment']['prior_revision'], 'before-docs')

    def test_confirmed_planning_rechecks_processed_target_and_preserves_independent_state(self):
        from loop_manager_internal.storage import planning_review_state as reviews
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        with self.connection:
            blocked_remote_observation.mark_processed(self.connection, self.repo, 'G-106',
                                                       blocked_remote_observation.input_identity(self.row()))
        self.assertFalse(blocked_remote_observation.pending_input(self.row()))
        independent = dict(self.plan('G-107'))
        inputs = 'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(revision=self.base, plans=[
            dict(plan_id='G-106', requirements=[dict(source='docs/requirements/[R-002] Policy.md',
                reason='changes the failed validation contract', quote='Current policy.')])]))
        with self.connection:
            reviews.begin(self.connection, self.repo, purpose='delta', revision=self.base,
                input_fingerprint='input', specification_fingerprint='spec', work_cycle='cycle', comparison_revision=None)
            reviews.validate(self.connection, self.repo, self.base, inputs, 'reviewed')
        self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred')
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
        with self.connection:
            reviews.confirm(self.connection, self.repo, self.base, self.base)
        with (mock.patch.object(self.coordinator, '_publish', side_effect=KeyboardInterrupt),
              mock.patch.object(self.coordinator, '_incorporate', wraps=self.coordinator._incorporate) as incorporate):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
            incorporate.assert_called_once()
        self.assertEqual(self.ports._evaluate_remote_checkpoint_cause.call_count, 2)
        with (mock.patch.object(self.coordinator, '_publish'),
              mock.patch.object(self.coordinator, '_incorporate') as incorporate):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            incorporate.assert_not_called()
        self.assertEqual(self.ports._evaluate_remote_checkpoint_cause.call_count, 2)
        self.assertEqual(dict(self.plan('G-107')), independent)
        self.assertEqual(reviews.reassessment(self.connection, self.repo, 'G-107'), {})
        self.assertEqual(self.plan()['verify_failure_history'], '{"failure":3}')
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'],
                         'original verify failure')

    def test_adopted_base_conflict_reobserves_successor_before_incorporation_after_restart(self):
        self.published_successor()
        store = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        def conflict(plan, context):
            prior = store.get(self.repo, 'G-106')
            store.record(self.repo, 'G-106', expected_generation=prior.generation, operation='rebase',
                expected_head=self.head, target_base_oid=self.base,
                observed_remote_oid=context['branch_oid'], snapshot=prior.snapshot)
            raise RuntimeError('base conflict')
        with mock.patch.object(self.coordinator, '_incorporate', side_effect=conflict):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.connection.execute(
            'SELECT stage FROM blocked_plan_reconciliations WHERE plan_id = ?', ('G-106',)).fetchone()[0], 'adopted')
        successor = 'e' * 40
        self.connection.execute("UPDATE blocked_remote_observations SET branch_oid = ? WHERE plan_id = 'G-106'",
                                (successor,))
        self.connection.commit()
        git = self.ports.git.side_effect
        def successor_git(root, *args, **kwargs):
            if args == ('rev-parse', 'FETCH_HEAD^{commit}'):
                return SimpleNamespace(returncode=0, stdout=successor)
            return git(root, *args, **kwargs)
        self.ports.git.side_effect = successor_git
        restarted = blocked_plan_reconciliation.BlockedPlanReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')
        steps = []
        def adopt(plan, context):
            steps.append(('adopt', context['branch_oid']))
        def incorporate(plan, context):
            steps.append(('incorporate', context['branch_oid']))
            raise RuntimeError('base conflict remains')
        with (mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
                  blocked_remote_observation.RefObservation('present', self.base),
                  blocked_remote_observation.RefObservation('present', successor))),
              mock.patch.object(restarted, '_adopt', side_effect=adopt),
              mock.patch.object(restarted, '_incorporate', side_effect=incorporate)):
            self.assertEqual(restarted.reconcile('G-106'), 'blocked')
        self.assertEqual(steps, [('adopt', successor), ('incorporate', successor)])
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')

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
                with self.subTest():
                    self.coordinator._validate(self.plan())


    def test_guidance_tracking_requires_exact_live_base_and_durable_branch_identity(self):
        original, proof = self.guidance_tracking_updates()
        current = self.current_snapshot
        self.current_snapshot = replace(current, refs={**current.refs, 'refs/remotes/origin/main': ('unproven', '')})
        with self.subTest():
            self.coordinator._validate(self.plan())
        self.current_snapshot = current
        proof.return_value = None
        with self.subTest():
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
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
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
        completed = False
        self.ports._blocked_execplan_metadata_error.side_effect = (
            lambda *_: None if completed else 'old sources differ from catalog')
        def rebase(*args, **kwargs):
            store.record(self.repo, 'G-106', expected_generation=0, operation='rebase',
                         expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
            return SimpleNamespace(returncode=0, stdout='rebased')
        def complete(*args, **kwargs):
            nonlocal completed
            current = store.get(self.repo, 'G-106')
            store.record(self.repo, 'G-106', expected_generation=current.generation, operation=None,
                         expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None, snapshot={})
            completed = True
        self.ports._journaled_direct_rebase.side_effect = rebase
        self.ports._complete_direct_rebase_checkpoint.side_effect = complete
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._journaled_direct_rebase.assert_called_once()
        self.ports._complete_direct_rebase_checkpoint.assert_called_once()
        self.assertEqual(self.ports._journaled_direct_rebase.call_args.kwargs['observed_remote'], (None,))
        self.ports._recorded_base_fetch.assert_called_once_with(
            Path('/repo'), Path('/state'), 'main', owner='plan:G-106')
        self.assertEqual(self.plan()['blocked_fingerprint'], 'input')

    def test_final_catalog_mismatch_still_blocks_cause_evaluation(self):
        self.ports._blocked_execplan_metadata_error.return_value = 'final sources differ from catalog'
        with mock.patch.object(self.coordinator, '_publish') as publish:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.ports._current_rebase_service.return_value.require_owned.assert_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        publish.assert_not_called()


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
        self.ports._evaluate_remote_checkpoint_cause.assert_called_once()
        self.assertEqual(self.ports._evaluate_remote_checkpoint_cause.call_args.args[4]['blocked_reason'],
                         'user_decision_required')


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
        proof.assert_not_called()
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
            with self.subTest():
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
                with self.subTest():
                    self.persisted_adoption_scope()
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def test_interruption_after_release_preserves_audit_and_uses_adopted_baseline(self):
        original = self.published_successor()
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        request = repository.get(self.repo, 'G-106')
        tree = 'd' * 40
        message = '[G-106] BLOCKED checkpoint\nScope: plan\nCheckpoint-Identity: checkpoint\n'
        repository.record_prepared(request, blocked_checkpoint.PreparedCheckpoint(
            original.head, tree, 'checkpoint', message, ()))
        repository.finish_published(request, original.head)
        git = self.ports.git.side_effect
        def ownership_git(path, *args, **kwargs):
            if args == ('show', '-s', '--format=%B', original.head):
                return SimpleNamespace(returncode=0, stdout=message)
            if args == ('rev-parse', f'{original.head}^{{tree}}'):
                return SimpleNamespace(returncode=0, stdout=tree)
            if args[:1] == ('ls-remote',):
                return SimpleNamespace(returncode=0, stdout=(
                    f"{self.row()['branch_oid']}\trefs/heads/{original.branch}\n"))
            return git(path, *args, **kwargs)
        self.ports.git.side_effect = ownership_git
        self.ports.owned_remote_oid.side_effect = checkpoint_ownership.owned_remote_oid
        manager = loop_manager.ManagerGitCheckpoint(original, frozenset()).serialize()
        self.connection.execute(
            "UPDATE exec_plans SET agent_manager_snapshot = ?, agent_baseline = 'old' "
            "WHERE repo = ? AND plan_id = 'G-106'", (manager, self.repo))
        self.connection.commit()
        self.ports._evaluate_remote_checkpoint_cause.return_value = (True, 'resolved', 'verify')
        effects = replace(loop_manager._scope_reconciliation_effects_resume_plan_blocks(),
                          plan_recovery_effects=lambda: self.ports)
        resume = blocked_plan_reconciliation.BlockedPlanReconciler._resume
        def interrupt(instance, *args, **kwargs):
            resume(instance, *args, **kwargs)
            raise KeyboardInterrupt()
        with (mock.patch.object(loop_manager, '_scope_reconciliation_effects_resume_plan_blocks',
                                return_value=effects),
              mock.patch.object(blocked_plan_reconciliation.BlockedPlanReconciler, '_resume', new=interrupt),
              mock.patch.object(loop_manager, '_resume_remote_corrected_execplan',
                                side_effect=AssertionError('unobserved legacy recovery'))):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, Path('/repo'), Path('/state'), self.repo, 'main')
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        self.assertEqual(self.plan()['agent_result_snapshot'], original.serialize())
        self.assertEqual(self.plan()['agent_manager_snapshot'], manager)
        self.assertEqual(self.plan()['agent_baseline'], 'old')
        self.assertIsNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
        self.assertEqual(self.coordinator.reconcile('G-106'), 'unhandled')
        loop_manager.hydrate_active_project(self.connection, self.repo)
        projected = self.connection.execute('SELECT * FROM projects WHERE repo = ?', (self.repo,)).fetchone()
        self.assertEqual(projected['agent_snapshot'], original.serialize())
        self.assertEqual(projected['agent_manager_snapshot'], manager)
        before = loop_manager.begin_agent_scope(
            self.connection, self.repo, Path(self.plan()['worktree']), 'worker')
        self.assertEqual(before.head, self.head)
        self.assertNotEqual(before.head, original.head)
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def test_agent_scope_violation_before_adoption_is_not_absorbed(self):
        original = self.published_successor()
        self.current_snapshot = replace(original, refs={**original.refs, 'refs/heads/unowned': ('foreign', '')})
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports._reconcile_published_blocked_checkpoint.assert_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_called()
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
