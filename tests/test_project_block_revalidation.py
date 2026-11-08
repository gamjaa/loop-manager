import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import blocked_remote_observation


from tests.support.blocked_remote import ProjectRemoteRecoveryFixture
from tests.support.rebase_observations import observation
from loop_manager_internal.git.managed_rebase_validation import Remote


class ProjectRecoveryGateTests(ProjectRemoteRecoveryFixture, unittest.TestCase):
    def test_merged_owner_preserves_project_block_without_catalog_or_git_recovery(self):
        before = dict(scoped_blocking.project_block(self.connection, self.repo))
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-106'")
        self.connection.commit()
        self.assertFalse(self.recovery.project_slot_available(self.connection, self.repo, 'G-106'))
        self.assertFalse(self.coordinator.refresh_catalog('G-106'))
        self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred')
        plan = dict(self.plan())
        other = dict(self.plan('G-107'))
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo,'G-106')
        snapshot = loop_manager.GitSnapshot(self.head,plan['branch'],{},{},{},{},{},{})
        with mock.patch('loop_manager.capture_git_snapshot',return_value=snapshot), \
             mock.patch('loop_manager.validate_execplan_workspace_identity',
                        side_effect=loop_manager.WorkspaceOwnershipError('ownership unresolved')) as identity, \
             mock.patch('loop_manager.validate_managed_worktree'), \
             mock.patch('loop_manager._resume_owned_checkpoint_project_block') as execution_recovery, \
             mock.patch('loop_manager._reconcile_rebase_evidence_block') as rebase_recovery, \
             mock.patch('loop_manager.run_agent') as agent:
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection,Path('/repo'),Path('/state'),self.repo))
            self.assertEqual(dict(scoped_blocking.project_block(self.connection,self.repo)),before)
            identity.side_effect=None
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection,Path('/repo'),Path('/state'),self.repo))
            self.assertIsNone(scoped_blocking.project_block(self.connection,self.repo))
            execution_recovery.assert_not_called()
            rebase_recovery.assert_not_called()
            agent.assert_not_called()
        self.assertEqual(dict(self.plan()),plan)
        self.assertEqual(dict(self.plan('G-107')),other)
        self.assertEqual(execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection).get(self.repo,'G-106'),checkpoint)
        self.ports._recorded_base_fetch.assert_not_called()
        self.ports.reconcile_execplan_catalog.assert_not_called()

    def test_cycle_project_owner_can_recover_without_waiting_for_cycle_completion(self):
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository, MaintenancePhase
        cycle = MaintenanceRepository(self.connection).get(self.repo)
        self.connection.execute("UPDATE projects SET plan_path='docs/plans/[G-106] Work.md'")
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE plan_id='G-106'")
        with mock.patch.object(MaintenanceRepository, 'get', return_value=replace(cycle,
                phase=MaintenancePhase.REFACTORING, publication_stage='remote_confirmed',
                refactoring_plan_ids=('G-106',))):
            self.assertTrue(self.recovery.project_slot_available(self.connection, self.repo, 'G-106'))

    def test_fetch_lease_contention_keeps_same_remote_observation_retryable(self):
        with mock.patch.object(self.coordinator, '_incorporate',
                side_effect=loop_manager.ProcessLockBusyError('git:fetch is occupied')) as incorporate:
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        incorporate.assert_called_once()
        self.assertTrue(blocked_remote_observation.pending_input(self.row()))
        saved = self.connection.execute(
            "SELECT stage FROM project_blocked_reconciliations WHERE repo=? AND plan_id='G-106'", (self.repo,)).fetchone()
        self.assertNotEqual(saved['stage'], 'refused')

    def test_other_active_or_projected_plan_and_planning_defer_all_git_writes(self):
        for update, reset in (
                ("UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE plan_id = 'G-107'",
                 "UPDATE exec_plans SET status = 'PLAN_BLOCKED' WHERE plan_id = 'G-107'"),
                ("UPDATE projects SET plan_path = 'docs/plans/[G-107] Work.md'", "UPDATE projects SET plan_path = NULL"),
                ("UPDATE projects SET planning_error = 'pending planning'", "UPDATE projects SET planning_error = NULL")):
            with self.subTest(update=update):
                self.connection.execute(update)
                self.connection.commit()
                self.assertEqual(self.coordinator.reconcile('G-106'), 'deferred')
                self.ports.git.assert_not_called()
                self.assertTrue(blocked_remote_observation.pending_input(self.row()))
                self.connection.execute(reset)
                self.connection.commit()


    def test_missing_legacy_plan_preserves_project_block_before_dependency_check(self):
        block = dict(scoped_blocking.project_block(self.connection, self.repo))
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        self.connection.execute("DELETE FROM exec_plans WHERE plan_id = 'G-106'")
        self.connection.commit()
        self.ports.unmet_execplan_dependencies.side_effect = loop_manager.unmet_execplan_dependencies
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.ports.unmet_execplan_dependencies.assert_not_called()
        self.ports.git.assert_not_called()
        self.assertEqual(dict(scoped_blocking.project_block(self.connection, self.repo)), block)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)


    def test_active_blocked_owner_can_keep_its_slot_without_hydration_or_agent(self):
        self.connection.execute("UPDATE projects SET plan_path = 'docs/plans/[G-106] Work.md'")
        from loop_manager_internal.recovery.plan_recovery import target_available
        for status in ('VERIFYING', 'REVIEWING', 'PUBLISHING', 'IMPLEMENTING'):
            with self.subTest(status=status):
                self.connection.execute("UPDATE exec_plans SET status=? WHERE plan_id='G-106'", (status,))
                self.assertTrue(self.recovery.project_slot_available(self.connection, self.repo, 'G-106'))
                self.assertFalse(target_available(self.connection, self.repo, 'G-106'))
        self.connection.commit()
        self.assertTrue(self.recovery.project_slot_available(self.connection, self.repo, 'G-106'))
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        with (mock.patch.object(self.coordinator, '_lineage_known', return_value=True),
              mock.patch.object(self.coordinator, '_adopt') as adopt,
              mock.patch.object(self.coordinator, '_incorporate', side_effect=lambda plan, context: context.update(candidate=self.signature)),
              mock.patch.object(self.coordinator, '_publish') as publish):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        adopt.assert_called_once()
        publish.assert_called_once()
        self.ports.hydrate_execplan_project.assert_not_called()
        self.ports._evaluate_remote_checkpoint_cause.assert_not_called()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertEqual(self.plan()['worker_session_id'], 'session')


    def test_scope_refusal_preserves_original_cause_session_and_other_plan(self):
        other = dict(self.plan('G-107'))
        with (mock.patch.object(self.coordinator, '_lineage_known', return_value=False),
              mock.patch.object(self.coordinator, '_incorporate', side_effect=RuntimeError('unresolved rebase conflict')),
              mock.patch.object(self.coordinator, '_publish') as publish):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        publish.assert_not_called()
        block = scoped_blocking.project_block(self.connection, self.repo)
        self.assertEqual(block['evidence'], 'original project cause')
        self.assertEqual(self.connection.execute('SELECT last_error FROM projects').fetchone()[0], 'original project cause')
        self.assertEqual(dict(self.plan('G-107')), other)
        self.assertEqual(self.plan()['worker_session_id'], 'session')
        payload = json.loads(self.connection.execute('SELECT payload FROM project_checkpoint_results').fetchone()[0])
        self.assertEqual(payload['reason'], 'unresolved rebase conflict')


    def test_project_adoption_rejects_changed_cause_or_execution_slot(self):
        original = self.published_project_successor()
        context = self.complete_context()
        context['branch_oid'] = 'c' * 40
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        for update, reset in (
                ("UPDATE project_blocks SET evidence = 'another safety cause'",
                 "UPDATE project_blocks SET evidence = 'original project cause'"),
                ("UPDATE projects SET plan_path = 'docs/plans/[G-107] Work.md'",
                 "UPDATE projects SET plan_path = NULL")):
            with self.subTest(update=update):
                self.connection.execute(update)
                self.connection.commit()
                row = self.connection.execute("SELECT * FROM blocked_checkpoints WHERE plan_id = 'G-106'").fetchone()
                self.assertEqual(loop_manager._reconcile_published_blocked_checkpoint(
                    self.connection, Path('/repo'), Path('/state'), self.repo, self.plan(), row,
                    repository.get(self.repo, 'G-106'), repository, adopt_only=True,
                    expected_remote_oid=context['branch_oid'], project_recovery_context=context), 'blocked')
                self.assertEqual(self.head, original.head)
                self.connection.execute(reset)
                self.connection.commit()
        self.assertFalse(any('merge' in call.args for call in self.ports.git.call_args_list))


class ProjectCauseRevalidationTests(ProjectRemoteRecoveryFixture, unittest.TestCase):
    def test_manager_project_gate_runs_only_linked_recovery_before_returning_blocked(self):
        self.connection.execute('UPDATE projects SET registered = 1')
        self.connection.commit()
        with (mock.patch.object(Path, 'resolve', return_value=Path(self.repo)),
              mock.patch.object(loop_manager, '_self_runtime_project', return_value=None),
              mock.patch.object(loop_manager, 'observe_blocked_plan_remotes') as observe,
              mock.patch.object(loop_manager, 'observe_waiting_pull_requests') as pr,
              mock.patch.object(loop_manager, 'resume_project_block_if_safe', return_value=False),
              mock.patch.object(self.recovery, 'ProjectBlockedReconciler') as recovery,
              mock.patch.object(loop_manager, '_recorded_base_fetch') as fetch,
              mock.patch.object(loop_manager, 'run_agent') as agent,
              mock.patch.object(loop_manager, 'operation_print')):
            self.assertEqual(loop_manager._run_project_step(
                self.connection, Path('/state'), self.repo, Path('/config'), None, False,
                registered_only=True), 1)
        observe.assert_called_once()
        pr.assert_called_once()
        self.assertTrue(pr.call_args.kwargs['completion_only'])
        recovery.return_value.refresh_catalog.assert_called_once_with('G-106')
        recovery.return_value.reconcile.assert_called_once_with('G-106')
        fetch.assert_not_called()
        agent.assert_not_called()
        self.assertEqual(scoped_blocking.project_block(self.connection, self.repo)['evidence'], 'original project cause')


    def test_unchanged_conflict_waits_and_remote_or_worktree_changes_retry(self):
        with (mock.patch.object(self.coordinator, '_lineage_known', return_value=False),
              mock.patch.object(self.coordinator, '_incorporate', side_effect=self.recovery.LocalWorkPending('conflict pending')) as incorporate):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            incorporate.assert_called_once()
            self.signature = 'manual conflict resolution'
            self.record_observation()
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(incorporate.call_count, 2)


    def test_project_only_adopts_successor_before_base_and_cause_revalidation(self):
        original = self.published_project_successor()
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        cause = dict(scoped_blocking.project_block(self.connection, self.repo))
        other = dict(self.plan('G-107'))
        steps = []
        incorporate = self.coordinator._incorporate
        def incorporate_adopted(plan, context):
            self.assertEqual(self.head, 'c' * 40)
            self.assertEqual(plan['publication_remote_oid'], self.head)
            self.assertIsNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
            steps.append('incorporate')
            return incorporate(plan, context)
        self.ports.resume_project_block_if_safe.side_effect = lambda *args: steps.append('revalidate') or False
        with (mock.patch.object(self.coordinator, '_incorporate', side_effect=incorporate_adopted),
              mock.patch.object(self.coordinator, '_publish', side_effect=lambda *args: steps.append('publish'))):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(steps, ['incorporate', 'publish', 'revalidate'])
        self.assertEqual(self.adoption_scope_targets, [(self.previous_base, original.head), (self.base, self.head)])
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
        self.assertEqual(checkpoint.snapshot['last_adopted_remote_oid'], self.head)
        self.assertEqual(checkpoint.current_rebase_acceptance()['target_base_oid'], self.base)
        self.assertEqual(checkpoint.current_rebase_acceptance()['remote']['oid'], self.head)
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        self.assertEqual(self.plan()['agent_result_snapshot'], original.serialize())
        self.assertEqual(self.plan()['worker_session_id'], 'session')
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertIsNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
        self.assertEqual(dict(scoped_blocking.project_block(self.connection, self.repo)), cause)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertEqual(dict(self.plan('G-107')), other)


    def test_project_successor_fetch_preserves_refs_with_unowned_mapping(self):
        original = self.published_project_successor()
        refs = {'refs/heads/unowned': original.head}
        configured_refspec = f"+refs/heads/{self.plan()['branch']}:refs/heads/unowned"
        git_effect = loop_manager.git.side_effect
        fetches = []

        def mapped_git(path, *args, **kwargs):
            if args[:1] == ('fetch',):
                fetches.append(args)
                if '--refmap=' not in args:
                    refs[configured_refspec.split(':')[1]] = 'c' * 40
            return git_effect(path, *args, **kwargs)

        context = self.complete_context()
        context['branch_oid'] = 'c' * 40
        with mock.patch.object(loop_manager, 'git', side_effect=mapped_git):
            self.coordinator._adopt(self.plan(), context)
        self.assertEqual(self.head, 'c' * 40)
        self.assertEqual(refs, {'refs/heads/unowned': original.head})
        self.assertEqual(fetches, [('fetch', '--no-tags', '--refmap=', 'origin',
                                   f"refs/heads/{self.plan()['branch']}")])


    def test_project_adoption_preserves_an_independent_plan_cause(self):
        original = self.published_project_successor(plan_block=True)
        self.connection.execute(
            "UPDATE execplan_blocks SET reason_code = ?, evidence = 'pending user decision' WHERE plan_id = 'G-106'",
            (loop_manager.GUIDANCE_BLOCK_REASON,))
        self.connection.execute("UPDATE exec_plans SET blocked_reason = ? WHERE plan_id = 'G-106'",
                                (loop_manager.GUIDANCE_BLOCK_REASON,))
        self.connection.commit()
        cause = dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))
        context = self.complete_context()
        context['branch_oid'] = 'c' * 40
        self.coordinator._adopt(self.plan(), context)
        self.assertEqual(self.head, 'c' * 40)
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')), cause)
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')


    def test_project_only_scope_refusal_preserves_local_head_and_project_cause(self):
        original = self.published_project_successor()
        cause = dict(scoped_blocking.project_block(self.connection, self.repo))
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        with (mock.patch.object(self.recovery, 'observe_current_rebase', return_value=(
                  observation(remote=None), SimpleNamespace(allowed=False))),
              mock.patch.object(self.coordinator, '_incorporate') as incorporate,
              mock.patch.object(self.coordinator, '_publish') as publish):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        incorporate.assert_not_called()
        publish.assert_not_called()
        self.assertFalse(any('merge' in call.args for call in self.ports.git.call_args_list))
        self.assertEqual(self.head, original.head)
        self.assertEqual(dict(scoped_blocking.project_block(self.connection, self.repo)), cause)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertIsNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-106'))


    def test_plan_adoption_still_requires_its_own_plan_block(self):
        original = self.published_project_successor()
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        coordinator = blocked_plan_reconciliation.BlockedPlanReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')
        with self.assertRaisesRegex(RuntimeError, 'PLAN_BLOCKED'):
            coordinator._adopt(self.plan(), {'branch_oid': 'c' * 40})
        self.assertEqual(self.head, original.head)
        self.assertFalse(any('merge' in call.args for call in self.ports.git.call_args_list))
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)


    def test_owned_original_oid_does_not_need_to_be_an_ancestor_of_rebased_head(self):
        self.connection.execute("UPDATE exec_plans SET publication_remote_oid = ? WHERE plan_id = 'G-106'", (self.base,))
        self.connection.commit()
        self.ports.owned_remote_oid.return_value = self.base
        self.assertTrue(self.coordinator._lineage_known(self.plan(), self.complete_context()))
        self.assertEqual(self.ports.owned_remote_oid.call_args.kwargs['before_head'], self.base)


    def test_pending_owned_target_is_finished_even_if_remote_base_advances(self):
        context = self.complete_context()
        checkpoints = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        checkpoints.record(self.repo, 'G-106', expected_generation=0, operation='rebase',
                           expected_head=self.head, target_base_oid=self.base, observed_remote_oid=None,
                           snapshot={'before_head': self.head})
        self.ports._current_rebase_service.side_effect = self.current_service
        service = self.current_service()
        with (mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
                  blocked_remote_observation.RefObservation('present', 'c' * 40),
                  blocked_remote_observation.RefObservation('absent'))),
              mock.patch.object(self.recovery, 'independent_scope', return_value=(service, service.require_owned()[0]))):
            self.coordinator._incorporate(self.plan(), context)
        self.assertFalse(any(call.args[1:2] == ('fetch',) for call in self.ports.git.call_args_list))
        self.assertEqual(checkpoints.get(self.repo, 'G-106').current_rebase_acceptance()['target_base_oid'], self.base)


    def test_current_scope_accepts_missing_historical_rebase_evidence(self):
        remote = Remote(self.repo, self.plan()['branch'], None)
        observed = observation(
            remote=remote, branch=self.plan()['branch'],
            branch_ref=(self.head, ''), head=self.head, branch_worktrees=(str(Path('/state/G-106').resolve()),),
            registration=(f'HEAD={self.head}', 'branch=refs/heads/loop-manager/G-106'), index=())
        self.ports._current_rebase_service.return_value.expected.remote = observed.remote
        for markers in ((), (('REBASE_HEAD', 'retained'), ('MERGE_MSG', 'retained'))):
            with self.subTest(markers=markers), mock.patch.object(self.recovery, 'observe_current_rebase',
                    return_value=(replace(observed, operations=markers), SimpleNamespace(allowed=True))):
                _, current = self.recovery.independent_scope(
                    self.ports, self.connection, Path('/repo'), Path('/state'), self.repo,
                    self.plan(), target=self.base, remote_oid=None)
                self.assertEqual(current.head, self.head)
        self.assertIsNone(loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection).get(self.repo, 'G-106').snapshot)


    def test_current_scope_rejects_pending_conflict_and_scope_mismatch(self):
        remote = Remote(self.repo, self.plan()['branch'], None)
        self.ports._current_rebase_service.return_value.expected.remote = remote
        for allowed, operations, error, detail in (
                (False, (), RuntimeError, '현재 작업 범위 또는 원격 기대값 불일치'),
                (True, (('rebase-merge', 'metadata'),), self.recovery.LocalWorkPending, '미해결 Git operation')):
            observed = observation(remote=remote, operations=operations)
            with mock.patch.object(self.recovery, 'observe_current_rebase', return_value=(observed, SimpleNamespace(allowed=allowed))):
                with self.assertRaisesRegex(error, detail):
                    self.recovery.independent_scope(self.ports, self.connection, Path('/repo'), Path('/state'),
                                                    self.repo, self.plan(), target=self.base, remote_oid=None)
        self.ports._current_rebase_service.return_value.require_owned.assert_called_once()


class ProjectCheckpointPublicationTests(ProjectRemoteRecoveryFixture, unittest.TestCase):
    def record_diagnostic_publication(self, context):
        _, request = self.coordinator._request(self.plan(), context)
        repository = blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection)
        intent = repository.intent(request, base_oid=self.base, head_oid=self.head,
                                   tree_oid='c' * 40, cause=context['project_block']['evidence'])
        repository.result(
            self.repo, 'G-106', context['cause_identity'],
            dict(result='published', diagnostic=True, branch=intent['ref'],
                 commit_oid=self.head, original_branch=request.branch, base_oid=self.base),
            intent=intent)

    def test_transient_diagnostic_push_retries_saved_intent_without_new_rebase(self):
        with (mock.patch.object(self.coordinator, '_lineage_known', return_value=False),
              mock.patch.object(self.coordinator, '_incorporate', side_effect=lambda plan, context: context.update(candidate=self.signature)) as incorporate,
              mock.patch.object(self.coordinator, '_publish', side_effect=[
                  loop_manager.blocked_checkpoint.CheckpointPushError('network unavailable', retryable=True), None]) as publish):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        incorporate.assert_called_once()
        self.assertEqual(publish.call_count, 2)


    def test_current_acceptance_keeps_project_projection_and_is_restart_idempotent(self):
        context = self.complete_context()
        project, other = dict(self.connection.execute('SELECT * FROM projects').fetchone()), dict(self.plan('G-107'))
        self.ports._current_rebase_service.side_effect = self.current_service
        service = self.current_service()
        with mock.patch.object(self.recovery, 'independent_scope', return_value=(service, service.require_owned()[0])):
            self.coordinator._incorporate(self.plan(), context)
            first = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
            self.coordinator._incorporate(self.plan(), context)
        second = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
        self.assertEqual(first, second)
        self.assertEqual(first.current_rebase_acceptance()['head'], self.head)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertEqual(dict(self.plan('G-107')), other)
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertEqual(self.plan()['worker_session_id'], 'session')
        self.ports._publish_direct_rebase_checkpoint.assert_not_called()


    def test_confirmed_original_publication_supplies_scope_lease_after_generation_advance(self):
        checkpoints, _ = self.confirmed_original_checkpoint()
        self.assertIsNone(checkpoints.get(self.repo, 'G-106').current_rebase_acceptance())
        request = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(self.repo, 'G-106')
        def inspect(*args, remote_oid, **kwargs):
            if remote_oid != self.head:
                raise RuntimeError('stale checkpoint lease')
        with mock.patch.object(self.recovery, 'independent_scope', side_effect=inspect) as scope:
            self.assertTrue(self.recovery.checkpoint_scope_verified(
                self.ports, self.connection, Path('/repo'), Path('/state'), request, self.plan()))
            scope.assert_called_once()
            current = checkpoints.get(self.repo, 'G-106')
            changed = json.loads(json.dumps(current.snapshot))
            changed['rebase_publication_result']['intent_id'] = 'unconfirmed'
            for checkpoint in (replace(current, snapshot=changed),
                               replace(current, operation='rebase_publish'),
                               replace(current, observed_remote_oid='unexpected')):
                with self.subTest(checkpoint=checkpoint.operation), mock.patch.object(
                        execution_checkpoint_state.ExecutionCheckpointRepository, 'get', return_value=checkpoint):
                    self.assertFalse(self.recovery.checkpoint_scope_verified(
                        self.ports, self.connection, Path('/repo'), Path('/state'), request, self.plan()))
            self.connection.execute("UPDATE exec_plans SET publication_remote_oid = 'unexpected' WHERE plan_id = 'G-106'")
            self.assertFalse(self.recovery.checkpoint_scope_verified(
                self.ports, self.connection, Path('/repo'), Path('/state'), request, self.plan()))


    def test_confirmed_original_publication_restart_keeps_acceptance_and_skips_fetch(self):
        checkpoints, original = self.confirmed_original_checkpoint()
        before = checkpoints.get(self.repo, 'G-106')
        context = self.complete_context()
        context.update(diagnostic=False, branch_oid=original)
        self.ports._current_rebase_service.side_effect = self.current_service
        service = self.current_service()
        with (mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
                  blocked_remote_observation.RefObservation('present', self.base),
                  blocked_remote_observation.RefObservation('present', self.head))),
              mock.patch.object(self.recovery, 'independent_scope', return_value=(service, service.require_owned()[0]))):
            self.coordinator._incorporate(self.plan(), context)
        self.assertEqual(checkpoints.get(self.repo, 'G-106'), before)
        self.assertFalse(any(call.args[1:2] == ('fetch',) for call in self.ports.git.call_args_list))
        self.ports._journaled_direct_rebase.assert_not_called()


    def test_prepared_local_commit_is_recovered_before_rebase_without_duplicate_commit(self):
        from loop_manager_internal.recovery import blocked_checkpoint
        context = self.complete_context()
        adapter = self.ports._BlockedCheckpointAdapter.return_value
        adapter.assess.return_value = SimpleNamespace(safe=True)
        prepared = blocked_checkpoint.PreparedCheckpoint(self.base, 'c' * 40, 'identity', 'message', ('code.py',))
        adapter.prepare.return_value = prepared
        adapter.ensure_local_commit.side_effect = [KeyboardInterrupt(), self.head]
        with self.assertRaises(KeyboardInterrupt):
            self.coordinator._local_checkpoint(self.plan(), context, adapter)
        saved = json.loads(self.connection.execute('SELECT context FROM project_blocked_reconciliations').fetchone()[0])
        self.coordinator._local_checkpoint(self.plan(), saved, adapter)
        adapter.prepare.assert_called_once()
        self.assertEqual(adapter.ensure_local_commit.call_args.args[1], prepared)
        self.assertEqual(self.connection.execute('SELECT local_oid FROM blocked_checkpoints WHERE plan_id = ?', ('G-106',)).fetchone()[0], self.head)


    def test_confirmed_original_publication_releases_only_its_project_cause(self):
        context = self.complete_context()
        context['diagnostic'] = False
        store, request = self.coordinator._request(self.plan(), context)
        store.finish_published(request, self.head)
        loop_manager.blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection).result(
            self.repo, 'G-106', context['cause_identity'],
            dict(result='published', branch=self.plan()['branch'], commit_oid=self.head))
        other = dict(self.plan('G-107'))
        with (mock.patch.object(self.coordinator, '_candidate_unchanged'),
              mock.patch.object(self.coordinator, '_lineage_known', return_value=True)):
            self.coordinator._revalidate_project_cause(self.plan(), context)
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-106')['evidence'], 'original verify failure')
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.assertEqual(dict(self.plan('G-107')), other)


    def test_diagnostic_success_never_releases_publication_ownership_cause(self):
        context = self.complete_context()
        self.record_diagnostic_publication(context)
        with mock.patch.object(self.coordinator, '_candidate_unchanged'):
            self.coordinator._revalidate_project_cause(self.plan(), context)
        self.assertEqual(scoped_blocking.project_block(self.connection, self.repo)['evidence'], 'original project cause')


    def test_later_cause_revalidation_error_keeps_the_confirmed_publication(self):
        def published(plan, context):
            self.record_diagnostic_publication(context)
        with (mock.patch.object(self.coordinator, '_lineage_known', return_value=False),
              mock.patch.object(self.coordinator, '_incorporate', side_effect=lambda plan, context: context.update(candidate=self.signature)),
              mock.patch.object(self.coordinator, '_publish', side_effect=published),
              mock.patch.object(self.coordinator, '_revalidate_project_cause', side_effect=blocked_plan_reconciliation.InputChanged('remote changed after publication'))):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        payload = json.loads(self.connection.execute('SELECT payload FROM project_checkpoint_results').fetchone()[0])
        self.assertEqual(payload['result'], 'published')
        self.assertEqual(payload['commit_oid'], self.head)


    def test_changed_candidate_restarts_worker_after_its_project_gate_is_released(self):
        self.connection.execute("DELETE FROM execplan_blocks WHERE plan_id = 'G-106'")
        self.connection.execute("UPDATE exec_plans SET status = 'REVIEWING', verify_token = 'old', review_token = 'old' WHERE plan_id = 'G-106'")
        self.connection.commit()
        context = self.complete_context()
        context['project_block']['reason_code'] = 'workspace_ownership_uncertain'
        self.connection.execute("UPDATE project_blocks SET reason_code = 'workspace_ownership_uncertain'")
        self.connection.commit()
        with mock.patch.object(self.coordinator, '_candidate_unchanged'):
            self.coordinator._revalidate_project_cause(self.plan(), context)
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertIsNone(self.plan()['verify_token'])
        self.assertIsNone(self.plan()['review_token'])
        self.assertEqual(self.plan()['worker_session_id'], 'session')
        self.assertEqual(self.connection.execute('SELECT phase FROM projects').fetchone()[0], 'WORKING')


    def test_push_before_sqlite_interruption_reuses_the_same_diagnostic_ref(self):
        from loop_manager_internal.storage import blocked_checkpoint_state
        context, remote = self.diagnostic_context(), [None]
        adapter = self.ports._BlockedCheckpointAdapter.return_value
        adapter.observe_remote.side_effect = lambda _: remote[0]
        self.ports.push_diagnostic_checkpoint.side_effect = lambda _, request, head: remote.__setitem__(0, head)
        with (mock.patch.object(self.coordinator, '_remote', return_value=None),
              mock.patch.object(self.coordinator, '_candidate_unchanged'),
              mock.patch.object(blocked_checkpoint_state.DiagnosticPublicationRepository, 'result', side_effect=KeyboardInterrupt)):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator._publish(self.plan(), context)
        with (mock.patch.object(self.coordinator, '_remote', return_value=None),
              mock.patch.object(self.coordinator, '_candidate_unchanged')):
            self.coordinator._publish(self.plan(), context)
            self.coordinator._publish(self.plan(), context)
        self.ports.push_diagnostic_checkpoint.assert_called_once()
        row = self.connection.execute('SELECT * FROM blocked_diagnostic_publications').fetchone()
        self.assertEqual(row['confirmed_oid'], self.head)
        self.assertEqual(row['original_branch'], 'loop-manager/G-106')
        self.assertIsNone(self.plan()['publication_remote_oid'])
        self.assertIsNone(self.plan()['pr_url'])
        self.assertEqual(scoped_blocking.project_block(self.connection, self.repo)['evidence'], 'original project cause')


    def test_retired_project_intent_is_not_recreated_or_reported_as_published(self):
        context = self.diagnostic_context()
        self.record_diagnostic_publication(context)
        row = self.connection.execute('SELECT * FROM blocked_diagnostic_publications').fetchone()
        uses = [dict(use) for use in self.connection.execute('SELECT * FROM diagnostic_ref_uses')]
        self.connection.execute('INSERT INTO diagnostic_cleanup_attempts VALUES (?, ?, ?, ?, ?, ?, ?)',
            (self.repo, row['ref'], 'basis', self.head, 'complete', 1,
             json.dumps(dict(state=dict(uses=uses), events=[]))))
        self.connection.commit()
        adapter = self.ports._BlockedCheckpointAdapter.return_value
        adapter.observe_remote.reset_mock()
        # The saved intent's tree must match the current candidate's tree.
        self.ports.git.side_effect = lambda path, *args, **kwargs: SimpleNamespace(returncode=0,
            stdout='c' * 40 if args[:2] == ('rev-parse', 'HEAD^{tree}') else self.head)
        with mock.patch.object(self.coordinator, '_remote', return_value=None):
            self.coordinator._publish(self.plan(), context)
        adapter.observe_remote.assert_not_called()
        self.ports.push_diagnostic_checkpoint.assert_not_called()
        payload = json.loads(self.connection.execute('SELECT payload FROM project_checkpoint_results').fetchone()[0])
        self.assertEqual(payload['result'], 'published')
        from loop_manager_internal.notifications.incidents import _project_checkpoint
        current = _project_checkpoint(self.connection, self.repo, 'G-106')
        self.assertEqual(current['result'], 'local_only')
        self.assertEqual(current['remote_state'], 'absent')
        self.assertTrue(current['historical_publication'])
        self.assertEqual(self.connection.execute('SELECT confirmed_oid FROM blocked_diagnostic_publications').fetchone()[0], self.head)

    def test_diagnostic_ref_collision_never_pushes_or_changes_original_ref(self):
        self.ports._BlockedCheckpointAdapter.return_value.observe_remote.return_value = 'c' * 40
        with mock.patch.object(self.coordinator, '_remote', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '다른 OID'):
                self.coordinator._publish(self.plan(), self.diagnostic_context())
        self.ports.push_diagnostic_checkpoint.assert_not_called()
        self.assertIsNone(self.plan()['publication_remote_oid'])
