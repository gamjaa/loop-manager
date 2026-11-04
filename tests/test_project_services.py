"""Unit checks for CLI composition, named ports and project service boundaries."""
import ast
from inspect import getclosurevars, signature
import importlib
from dataclasses import fields
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.application import commands, project_service
from loop_manager_internal.planning import git_ports, state_ports
from tests.support.database import runtime_connection
from tests.support.database import AutomationCatalogFixture
from loop_manager_internal.recovery import waiting_pr_service, scoped_blocking
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository


class WaitingPrConflictResultTests(AutomationCatalogFixture, unittest.TestCase):
    def test_stale_preflight_diagnosis_does_not_replace_new_workspace_inputs(self):
        from loop_manager_internal.recovery.pull_request_recovery import _wait_for_conflict_inputs
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'PR_OPEN')
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.hydrate('repo', 'G-001')
        previous = runtime_state.project_row(connection, 'repo')
        runtime.update_plan('repo', 'G-001', plan_values={'worktree': '/new-owned', 'last_error': 'new cause'})
        with self.assertRaises(RecoveryFailure) as rejected:
            _wait_for_conflict_inputs(connection, 'repo', previous, 'old cause', 'pr_conflict_preflight')
        self.assertEqual(rejected.exception.reason, RecoveryReason.REMOTE_SNAPSHOT_RACE)
        self.assertEqual(runtime.get('repo', 'G-001').last_error, 'new cause')
        self.assertIsNone(RecoveryRepository(connection).row('repo', 'G-001'))

    def test_local_preflight_wait_allows_independent_projection_but_old_wait_does_not_hide_an_error(self):
        from loop_manager_internal.recovery.pull_request_recovery import _wait_for_conflict_inputs
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'PR_OPEN')
        self._insert_plan(connection, 'G-002', 'VERIFYING')
        connection.execute("UPDATE exec_plans SET branch='loop/G-001', pr_url='https://example/pr/1' "
                           "WHERE plan_id='G-001'")
        connection.execute('UPDATE projects SET max_tasks_per_project=2')
        connection.commit()
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.hydrate('repo', 'G-002')
        before = dict(connection.execute('SELECT * FROM projects').fetchone())
        plan = connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        def wait(_connection, _root, _state, repo, project, _pr):
            return _wait_for_conflict_inputs(connection, repo, project,
                                            'preserved local changes', 'pr_conflict_preflight')
        reconcile = mock.Mock(side_effect=wait)
        effects = waiting_pr_service.SelectWaitingPullRequestConflictEffects(
            loop_manager.active_execplan, loop_manager.hydrate_execplan_project, reconcile)
        observation = loop_manager.WaitingPullRequestObservation(((plan, {}),), False)
        result = waiting_pr_service.select_waiting_pull_request_conflict(
            effects, connection, Path('/repo'), Path('/state'), 'repo', observation)
        self.assertIsNone(result.step_result)
        reconcile.assert_called_once()
        self.assertFalse(result.conflict_started)
        self.assertEqual(dict(connection.execute('SELECT * FROM projects').fetchone()), before)
        self.assertEqual(runtime.get('repo', 'G-001').status, 'PR_OPEN')
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM execplan_blocks').fetchone()[0], 0)
        effects = waiting_pr_service.SelectWaitingPullRequestConflictEffects(
            loop_manager.active_execplan, loop_manager.hydrate_execplan_project,
            mock.Mock(return_value=loop_manager.PROJECT_STEP_TRANSIENT_PAUSE))
        result = waiting_pr_service.select_waiting_pull_request_conflict(
            effects, connection, Path('/repo'), Path('/state'), 'repo', observation)
        self.assertEqual(result.step_result, loop_manager.PROJECT_STEP_TRANSIENT_PAUSE)

    def test_plan_conflict_result_preserves_independent_projection_and_pending_rebase(self):
        for project_safety_block in (False, True):
            with self.subTest(project_safety_block=project_safety_block):
                connection = self._connection()
                self._insert_plan(connection, 'G-001', 'PR_OPEN')
                self._insert_plan(connection, 'G-002', 'VERIFYING')
                connection.execute("UPDATE exec_plans SET branch='loop/G-001', worktree='/state/G-001', "
                                   "pr_url='https://example/pr/1', pr_head_oid='head', "
                                   "worker_session_id='saved-worker' WHERE plan_id='G-001'")
                connection.commit()
                runtime = runtime_state.RuntimeStateRepository(connection)
                runtime.hydrate('repo', 'G-002')
                checkpoints = ExecutionCheckpointRepository(connection)
                pending = checkpoints.record('repo', 'G-001', expected_generation=0,
                    operation='rebase', expected_head='head', target_base_oid='base',
                    observed_remote_oid='head', snapshot={'stash_oid': 'saved-stash'})
                before = dict(connection.execute('SELECT * FROM projects').fetchone())
                independent = runtime.get('repo', 'G-002')
                plan = connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()

                def stop(*_args, **_kwargs):
                    self.assertEqual(runtime_state.execution_selection.get(), ('repo', 'G-001'))
                    runtime.block_plan('repo', 'G-001', reason_code='managed_worktree_conflict',
                        evidence='preserved conflict', release_fingerprint='input')
                    if project_safety_block:
                        scoped_blocking.record_project_block(connection, 'repo',
                            scoped_blocking.BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                            'shared ownership uncertain', 'shared-input',
                            plan_id='G-001', previous_phase='PR_OPEN')
                    return 1

                effects = waiting_pr_service.SelectWaitingPullRequestConflictEffects(
                    loop_manager.active_execplan, loop_manager.hydrate_execplan_project, stop)
                result = waiting_pr_service.select_waiting_pull_request_conflict(
                    effects, connection, Path('/repo'), Path('/state'), 'repo',
                    loop_manager.WaitingPullRequestObservation(((plan, {}),), True))
                self.assertEqual(result.step_result, 1 if project_safety_block else None)
                self.assertFalse(result.conflict_started)
                self.assertTrue(result.merged)
                self.assertEqual(runtime.get('repo', 'G-002'), independent)
                self.assertEqual(checkpoints.get('repo', 'G-001'), pending)
                self.assertEqual(runtime.get('repo', 'G-001').status, 'PLAN_BLOCKED')
                self.assertEqual(runtime.get('repo', 'G-001').worker_session_id, 'saved-worker')
                self.assertIsNone(runtime_state.execution_selection.get())
                if not project_safety_block:
                    self.assertEqual(dict(connection.execute('SELECT * FROM projects').fetchone()), before)

    def test_conflict_error_without_a_durable_plan_block_is_not_swallowed(self):
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'PR_OPEN')
        plan = connection.execute('SELECT * FROM exec_plans').fetchone()
        effects = waiting_pr_service.SelectWaitingPullRequestConflictEffects(
            loop_manager.active_execplan, loop_manager.hydrate_execplan_project,
            mock.Mock(return_value=1))
        result = waiting_pr_service.select_waiting_pull_request_conflict(
            effects, connection, Path('/repo'), Path('/state'), 'repo',
            loop_manager.WaitingPullRequestObservation(((plan, {}),), False))
        self.assertEqual(result.step_result, 1)
        self.assertFalse(result.conflict_started)

    def test_blocked_conflict_observes_remote_terminal_state_without_restarting_open_pr(self):
        for state in ('OPEN', 'CLOSED', 'MERGED'):
            with self.subTest(state=state):
                connection = self._connection()
                self._insert_plan(connection, 'G-001', 'PR_OPEN')
                connection.execute("UPDATE exec_plans SET branch='loop/G-001', "
                    "pr_url='https://example/pr/1', pr_head_oid='head'")
                connection.commit()
                runtime = runtime_state.RuntimeStateRepository(connection)
                runtime.block_plan('repo', 'G-001', reason_code='managed_worktree_conflict',
                                   evidence='preserved conflict', release_fingerprint='input')
                original = runtime.get('repo', 'G-001')
                effects = waiting_pr_service.ObserveWaitingPullRequestsEffects(
                    _block_closed_pull_request=mock.Mock(), _recorded_base_fetch=mock.Mock(),
                    _resume_reopened_pull_request=mock.Mock(),
                    classify_pull_request_mergeability=mock.Mock(),
                    complete_merged_execplan_worktree=mock.Mock(return_value=SimpleNamespace(status='safe')),
                    operation_log=mock.Mock(), operation_print=mock.Mock(),
                    pr_for_branch=mock.Mock(return_value={'url': original.pr_url,
                        'headRefName': original.branch, 'headRefOid': 'head',
                        'baseRefName': 'main', 'state': state}))
                result = waiting_pr_service.observe_waiting_pull_requests(
                    effects, connection, Path('/repo'), Path('/state'), 'repo')
                effects.pr_for_branch.assert_called_once_with(Path('/repo'), original.branch, original.pr_url)
                self.assertEqual(result.merged, state == 'MERGED')
                self.assertEqual(result.conflicts, ())
                self.assertEqual(effects._block_closed_pull_request.call_count, int(state == 'CLOSED'))
                self.assertEqual(effects.complete_merged_execplan_worktree.call_count, int(state == 'MERGED'))
                self.assertEqual(effects._recorded_base_fetch.call_count, int(state == 'MERGED'))
                effects.classify_pull_request_mergeability.assert_not_called()
                effects._resume_reopened_pull_request.assert_not_called()
                self.assertEqual(runtime.get('repo', 'G-001'), original)


class ProjectServiceCompositionTests(unittest.TestCase):
    def test_resume_existing_execution_does_not_repeat_local_preflight_wait(self):
        from loop_manager_internal.recovery.pull_request_recovery import _wait_for_conflict_inputs
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE exec_plans SET status='PR_OPEN', branch='loop/G-047', pr_url='https://example/pr/1'")
        connection.commit()
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.hydrate('repo', 'G-047')
        _wait_for_conflict_inputs(connection, 'repo', runtime_state.project_row(connection, 'repo'),
                                  'preserved local changes', 'pr_conflict_preflight')
        effects = SimpleNamespace(pr_for_branch=mock.Mock(), reconcile_existing_pull_request=mock.Mock())
        service = project_service.ProjectService(effects, project_service.ProjectIdentity(connection,
            Path('/state'), 'repo', Path('/config.ini'), None, False, None, True, None, 20, 600, None))
        service.state.repo = 'repo'
        service.state.row = connection.execute('SELECT * FROM projects').fetchone()
        self.assertIsNone(service._resume_existing_execution())
        effects.pr_for_branch.assert_not_called()
        effects.reconcile_existing_pull_request.assert_not_called()

    def test_resume_existing_execution_leaves_local_conflict_to_cause_specific_recheck(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute('UPDATE projects SET planning_attempt_fingerprint=NULL')
        connection.commit()
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.activate('repo', 'G-047', activation_sha='base',
                         branch='loop/G-047', worktree='/state/G-047')
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_conflict',
                           evidence='preserved conflict', release_fingerprint='input')
        runtime.hydrate('repo', 'G-047')
        effects = SimpleNamespace(pr_for_branch=mock.Mock(),
                                  reconcile_existing_pull_request=mock.Mock())
        identity = project_service.ProjectIdentity(connection, Path('/state'), 'repo',
            Path('/config.ini'), None, False, None, True, None, 20, 600, None)
        service = project_service.ProjectService(effects, identity)
        service.state.repo = 'repo'
        service.state.row = connection.execute('SELECT * FROM projects').fetchone()
        before = runtime.get('repo', 'G-047')
        for _ in range(2):
            self.assertIsNone(service._resume_existing_execution())
        effects.pr_for_branch.assert_not_called()
        effects.reconcile_existing_pull_request.assert_not_called()
        self.assertEqual(runtime.get('repo', 'G-047'), before)

    def test_scheduled_planning_wait_still_dispatches_task_recovery(self):
        from loop_manager_internal.application.task_scheduling import current_scheduler, current_task
        for scheduled, nested, kind in (
            (True, False, project_service.PlanningOutcomeKind.WAITING),
            (False, False, project_service.PlanningOutcomeKind.WAITING),
            (True, True, project_service.PlanningOutcomeKind.WAITING),
            (True, False, project_service.PlanningOutcomeKind.TRANSIENT_PAUSE),
        ):
            with self.subTest(scheduled=scheduled, nested=nested, kind=kind):
                connection = runtime_connection()
                self.addCleanup(connection.close)
                effects = SimpleNamespace(_recorded_base_fetch=mock.Mock(),
                    _check_runtime_handoff=mock.Mock(), _PlanningLifecyclePorts=mock.Mock(),
                    _record_project_recovery=mock.Mock(),
                    is_loop_manager_repository=mock.Mock(return_value=False),
                    _planning_outcome_step_result=loop_manager._planning_outcome_step_result)
                identity = project_service.ProjectIdentity(connection, Path('/state'), 'repo',
                    Path('/config.ini'), None, False, None, True, None, 20, 600, None)
                service = project_service.ProjectService(effects, identity)
                service.state.repo = 'repo'
                service.state.base = 'main'
                service.state.row = connection.execute('SELECT * FROM projects').fetchone()
                scheduler_token = current_scheduler.set(SimpleNamespace(draining_projects=set()) if scheduled else None)
                task_token = current_task.set(object() if nested else None)
                try:
                    with mock.patch.object(service, '_refresh_catalog', return_value='base'), \
                         mock.patch.object(service, '_dispatch_execution', return_value=73) as dispatch, \
                         mock.patch.object(project_service, 'PlanningLifecycleCoordinator') as planning:
                        planning.return_value.run.return_value = project_service.PlanningOutcome(kind)
                        result = service._run_coordinators()
                    if scheduled and not nested and kind is project_service.PlanningOutcomeKind.WAITING:
                        self.assertEqual(result, 73)
                        dispatch.assert_called_once_with(mock.ANY, newly_activated=False, planning_context=mock.ANY)
                    else:
                        dispatch.assert_not_called()
                finally:
                    current_task.reset(task_token)
                    current_scheduler.reset(scheduler_token)

    def test_manual_revalidation_defers_only_open_pr_reconciliation(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        for state in ('OPEN','CLOSED','MERGED'):
            with self.subTest(state=state):
                connection=runtime_connection()
                self.addCleanup(connection.close)
                connection.execute('UPDATE projects SET planning_attempt_fingerprint=NULL')
                connection.commit()
                repository=loop_manager.runtime_state.RuntimeStateRepository(connection)
                repository.activate('repo','G-047',activation_sha='base',
                    branch='loop-manager/G-047',worktree='/state/G-047')
                repository.update_project('repo',{'phase':'VERIFYING','pr_url':'https://example/pull/1'})
                effects=SimpleNamespace(_project_pr_merge_confirmed=mock.Mock(return_value=False),
                    _plan_id_from_path=mock.Mock(return_value='G-047'),
                    pr_for_branch=mock.Mock(return_value={'url':'https://example/pull/1','state':state}),
                    reconcile_existing_pull_request=mock.Mock(return_value=73))
                identity=project_service.ProjectIdentity(connection,Path('/state'),'repo',
                    Path('/config.ini'),None,False,None,True,None,20,600,None)
                service=project_service.ProjectService(effects,identity)
                service.state.repo='repo'
                service.state.row=connection.execute('SELECT * FROM projects').fetchone()
                with mock.patch.object(service,'_recover_pending_pr_checkpoint',return_value=None), \
                     mock.patch.object(ManualWorkRepository,'for_plan',return_value={'state':'AUTOMATIC'}):
                    result=service._resume_existing_execution()
                if state=='OPEN':
                    self.assertIsNone(result)
                    effects.reconcile_existing_pull_request.assert_not_called()
                else:
                    self.assertEqual(result,73)
                    effects.reconcile_existing_pull_request.assert_called_once()

    def test_planning_updates_keep_nested_transaction_and_rollback(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                connection = runtime_connection()
                self.addCleanup(connection.close)
                port = loop_manager._PlanningStatePorts(connection,
                    loop_manager.runtime_state.RuntimeStateRepository(connection),
                    loop_manager.maintenance_state.MaintenanceRepository(connection), Path('/state'))
                try:
                    with port.transaction():
                        with port.transaction():
                            port.update('repo', planning_error='current failure')
                            self.assertTrue(connection.in_transaction)
                            self.assertEqual(port.planning('repo').planning_error, 'current failure')
                            if fail:
                                raise ValueError('interrupt paired planning writes')
                except ValueError:
                    if not fail:
                        raise
                self.assertFalse(connection.in_transaction)
                self.assertEqual(port.planning('repo').planning_error,
                    None if fail else 'current failure')
                port.update('repo', planning_error=None)
                self.assertFalse(connection.in_transaction)

    def test_project_recovery_routes_to_planning_while_execution_is_paused(self):
        for planning_owns_slot in (False, True):
            with self.subTest(planning_owns_slot=planning_owns_slot):
                connection = runtime_connection()
                self.addCleanup(connection.close)
                connection.execute('UPDATE projects SET planning_attempt_fingerprint = NULL')
                connection.commit()
                repository = loop_manager.runtime_state.RuntimeStateRepository(connection)
                repository.activate('repo', 'G-047', activation_sha='base',
                    branch='loop-manager/G-047', worktree='/state/G-047')
                if planning_owns_slot:
                    repository.pause_execution_for_planning(repository.planning('repo'), 'G-047')
                self.assertIsNotNone(repository.active_row('repo'))
                effects = SimpleNamespace(_recorded_base_fetch=mock.Mock(),
                    _check_runtime_handoff=mock.Mock(), _PlanningLifecyclePorts=mock.Mock(),
                    is_loop_manager_repository=mock.Mock(return_value=False),
                    _planning_outcome_step_result=mock.Mock(return_value=None))
                identity = project_service.ProjectIdentity(connection, Path('/state'), 'repo',
                    Path('/config.ini'), None, False, None, True, None, 20, 600, None,
                    recovery_request_id='request')
                service = project_service.ProjectService(effects, identity)
                service.state.repo = 'repo'
                service.state.base = 'main'
                service.state.row = connection.execute('SELECT * FROM projects').fetchone()
                with mock.patch.object(service, '_refresh_catalog', return_value='base'), \
                     mock.patch.object(service, '_dispatch_execution', return_value=0) as execution, \
                     mock.patch.object(project_service, 'PlanningLifecycleCoordinator') as planning:
                    planning.return_value.run.return_value = project_service.PlanningOutcome(
                        project_service.PlanningOutcomeKind.NO_CHANGE)

                    self.assertEqual(service._run_coordinators(), 0)

                if planning_owns_slot:
                    execution.assert_not_called()
                    context = planning.return_value.run.call_args.args[0]
                    self.assertTrue(context.local_environment_recheck)
                    self.assertEqual(context.recovery_request_id, 'request')
                else:
                    execution.assert_called_once_with(mock.ANY, newly_activated=False)
                    planning.assert_not_called()

    def test_execution_planning_preserves_rebase_and_waits_for_other_git_operations(self):
        effects = git_ports.PlanningGitPortsEffects(**{field.name: mock.Mock()
            for field in fields(git_ports.PlanningGitPortsEffects)})
        port = git_ports._PlanningGitPorts(effects, None, Path('/repo'), Path('/state'))
        with tempfile.TemporaryDirectory() as directory:
            for markers, pending in (({}, False), ({'MERGE_MSG': 'old'}, False),
                                     ({'rebase-merge': 'active'}, False), ({'rebase-apply': 'active'}, False),
                                     ({'rebase-merge': 'active', 'sequencer': 'active'}, True),
                                     ({'MERGE_HEAD': 'active'}, True)):
                with self.subTest(markers=markers):
                    effects._git_operation_entries.return_value = markers
                    self.assertEqual(port.execution_blocks_planning(Path(directory)), pending)
            self.assertTrue(port.execution_blocks_planning(Path(directory) / 'missing'))

    def test_publication_release_checks_remote_head_and_missing_worktree_registration(self):
        effects = git_ports.PlanningGitPortsEffects(**{field.name: mock.Mock()
            for field in fields(git_ports.PlanningGitPortsEffects)})
        port = git_ports._PlanningGitPorts(effects, None, Path('/repo'), Path('/state'))
        effects.git.return_value = SimpleNamespace(stdout='new-remote')
        port.confirm_publication_release(Path('/repo'), 'main', 'base', 'published')
        effects._assert_remote_preserves_maintenance_batch.assert_called_once_with(
            Path('/repo'), 'base', 'published', 'new-remote')
        effects._git_operation_entries.return_value = {}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch('loop_manager_internal.git.manager_git_journal.has_pending_project_mutation', return_value=False):
            worktree = Path(directory)
            effects.git.side_effect = lambda _repo, *args, **kwargs: SimpleNamespace(
                stdout='' if args[:1] == ('ls-files',) else 'changed-head')
            with self.assertRaisesRegex(loop_manager.PlanningWorkspaceOwnershipError, 'HEAD'):
                port.validate_publication_release('/repo', worktree, 'published')
            missing = worktree / 'missing'
            effects.git.side_effect = None
            effects.git.return_value = SimpleNamespace(stdout=f'worktree {missing}\0HEAD published\0\0')
            with self.assertRaisesRegex(loop_manager.PlanningWorkspaceOwnershipError, 'Git 등록'):
                port.validate_publication_release('/repo', missing, 'published')
            effects.git.return_value = SimpleNamespace(stdout='')
            port.validate_publication_release('/repo', missing, 'published')

    def test_relocated_workspace_boundary_accepts_owned_path_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state'
            expected = state / 'worktree'
            loop_manager._validate_managed_path_boundary(state, expected, expected, 'Worker')
            with self.assertRaisesRegex(loop_manager.WorkspaceOwnershipError, '결정적 소유 경계'):
                loop_manager._validate_managed_path_boundary(state, state / 'other', expected, 'Worker')

    def test_relocated_workspace_boundary_rejects_symlink_escape_without_changing_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state'
            state.mkdir()
            outside = Path(temporary) / 'outside'
            outside.mkdir()
            preserved = outside / 'user.txt'
            preserved.write_text('preserved', encoding='utf-8')
            worktree = state / 'worktree'
            try:
                worktree.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f'symlink creation is unavailable: {error}')
            with self.assertRaisesRegex(loop_manager.WorkspaceOwnershipError, '상태 디렉터리 밖'):
                loop_manager._validate_managed_path_boundary(state, worktree, worktree, 'Worker')
            self.assertTrue(worktree.is_symlink())
            self.assertEqual(preserved.read_text(encoding='utf-8'), 'preserved')

    def test_root_contains_only_cli_definitions_and_compatibility_installation(self):
        tree = ast.parse(Path(loop_manager.__file__).read_text(encoding='utf-8'))
        definitions = [node.name for node in tree.body
                       if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
        self.assertEqual(definitions, ['build_parser', 'main'])
        self.assertNotIn('globals()', ast.unparse(tree))
        self.assertNotIn('_bind_effects(', ast.unparse(tree))
        self.assertFalse(any(isinstance(node, ast.Lambda) for node in ast.walk(tree)))

    def test_catalog_class_export_keeps_method_patches_on_existing_instances(self):
        self.assertIsInstance(loop_manager._GitCatalogDefinitionPort, type)
        port = loop_manager._GitCatalogDefinitionPort(Path('/repo'), 'refs/remotes/origin/main', frozenset)
        self.assertIsInstance(port, loop_manager._GitCatalogDefinitionPort)
        with mock.patch.object(loop_manager._GitCatalogDefinitionPort, 'load', return_value='pinned') as load:
            self.assertEqual(port.load(), 'pinned')
            load.assert_called_once_with()

    def test_compatibility_exports_bind_the_matching_named_effect_contract(self):
        # A private/public helper pair must not shadow either helper's effect type.
        for name, value in vars(loop_manager).items():
            implementation = getattr(value, '__wrapped__', None)
            if implementation is None or getattr(value, '__module__', None) != 'loop_manager':
                continue
            with self.subTest(helper=name):
                binding = value.__init__ if isinstance(value, type) else value
                factory = getclosurevars(binding).nonlocals['effects']
                effects = factory()
                parameter = next(iter(signature(implementation).parameters.values()))
                contract = getattr(importlib.import_module(implementation.__module__), parameter.annotation)
                self.assertIs(type(effects), contract)
                self.assertNotIn(parameter.name, value.__annotations__)
                self.assertEqual(tuple(signature(value).parameters),
                                 tuple(signature(implementation).parameters)[1:])

    def test_planning_ports_resolve_named_callbacks_after_construction(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        state = loop_manager._PlanningStatePorts(
            connection, loop_manager.runtime_state.RuntimeStateRepository(connection),
            loop_manager.maintenance_state.MaintenanceRepository(connection), Path('/state'))
        git = loop_manager._PlanningGitPorts(connection, Path('/repo'), Path('/state'))
        with mock.patch.object(loop_manager, 'catalog_schedule_decision', return_value='decision') as schedule:
            self.assertEqual(state.schedule('repo'), 'decision')
            schedule.assert_called_once_with(connection, 'repo')
        with mock.patch.object(loop_manager, 'git', return_value='observed') as command:
            self.assertEqual(git.run(Path('/repo'), 'rev-parse', 'HEAD'), 'observed')
            command.assert_called_once_with(Path('/repo'), 'rev-parse', 'HEAD', check=True)
        for port, contract in ((state, state_ports.PlanningStatePortsEffects),
                               (git, git_ports.PlanningGitPortsEffects)):
            self.assertEqual({field.name for field in fields(port._effects)},
                             {field.name for field in fields(contract)})
            self.assertTrue(all(callable(getattr(port._effects, field.name)) for field in fields(contract)))

    def test_execution_ports_keep_project_identity_timeout_and_scope_signature(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute('UPDATE projects SET planning_attempt_fingerprint = NULL')
        connection.commit()
        repository = loop_manager.runtime_state.RuntimeStateRepository(connection)
        repository.activate('repo', 'G-047', activation_sha='a' * 40,
                            branch='loop-manager/G-047', worktree='/state/G-047')
        ports = loop_manager._ExecutionLifecyclePorts(
            connection, loop_manager.runtime_state.RuntimeStateRepository(connection),
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='a' * 40, provider='codex',
            config_path=Path('/source/config.ini'), newly_activated=False,
            verify_timeout_seconds=741)
        with mock.patch.object(loop_manager, 'verify', return_value=(True, 'passed')) as verify:
            self.assertEqual(ports.effects.validation.verify(Path('/state/G-047')), (True, 'passed'))
            verify.assert_called_once_with(Path('/state/G-047'), 741)
        self.assertEqual(ports.repo, 'repo')
        self.assertEqual(ports.config_path, Path('/source/config.ini'))
        self.assertIs(ports.effects.state.connection, connection)
        arguments = (Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                     'main', 'docs/plans/[G-047] Runtime.md', {}, object())
        with mock.patch.object(loop_manager, 'validate_worker_workspace_scope',
                               autospec=True, return_value={}) as validate:
            for phase in ('IMPLEMENTING', 'VERIFYING'):
                with self.subTest(phase=phase):
                    self.assertEqual(ports.effects.agent.validate_worker_workspace_scope(
                        *arguments, scope_phase=phase), {})
                    validate.assert_called_with(*arguments, scope_phase=phase,
                                                connection=connection, state_dir=Path('/state'))

    def test_cli_dispatch_reaches_command_handlers_without_project_work(self):
        config_path = Path('/source/config.ini')
        config = SimpleNamespace(path=config_path)
        for name, target in (('config', 'configure_registry'),
                             ('add', 'configure_registry'),
                             ('remove', 'configure_registry'),
                             ('document-drafts', '_document_drafts_command')):
            with self.subTest(command=name), mock.patch.object(loop_manager, 'load_managed_config', return_value=config), \
                 mock.patch.object(loop_manager, target, return_value=7) as handler, \
                 mock.patch.object(loop_manager, 'run_registered_projects') as scheduler:
                args = SimpleNamespace(command=name)
                self.assertEqual(loop_manager.execute_runtime_command(args, Path('/state'), config_path), 7)
                handler.assert_called_once_with(args, Path('/state'), config_path)
                scheduler.assert_not_called()
        self.assertEqual(loop_manager.execute_runtime_command.__wrapped__.__module__, commands.__name__)

    def test_worker_publication_export_preserves_targeted_evidence_validation(self):
        evidence = 'python3 -m unittest tests.test_project_services -v: PASS (9 tests)'
        message = ('COMMIT_TITLE: Compose project services through named callbacks\n'
                   'PR_SUMMARY: Move project processing into its owning services.\n'
                   'TARGETED_TESTS:\n' + evidence)
        plan = {'id': 'G-123', 'title': 'Reduce Root Entry Point and Compose Project Services'}
        publication = loop_manager.parse_worker_result(message, plan)
        self.assertEqual(publication.test_evidence, evidence)
        self.assertEqual(publication.commit_title, 'Compose project services through named callbacks')
        self.assertEqual(loop_manager.parse_worker_result.__wrapped__.__module__,
                         'loop_manager_internal.execution.result_validation')
        # Preserve both explicit zero-failure evidence and rejection of real failures.
        clean = message + '\n이번 재검증에서 실패 없음.'
        self.assertEqual(loop_manager.parse_worker_result(clean, plan).test_evidence,
                         evidence + '\n이번 재검증에서 실패 없음.')
        with self.assertRaisesRegex(loop_manager.WorkerResultError, '해결되지 않은 targeted'):
            loop_manager.parse_worker_result(clean + '\nFAILED (failures=1)', plan)

    def test_project_stages_stop_at_preserved_failure_boundary(self):
        effects = project_service.RunProjectStepEffects(**{
            field.name: mock.Mock(name=field.name) for field in fields(project_service.RunProjectStepEffects)})
        identity = project_service.ProjectIdentity(mock.Mock(), Path('/state'), '/repo',
            Path('/config.ini'), None, False, None, True, None, 20, 600, None)
        service = project_service.ProjectService(effects, identity)
        events = []
        service._prepare_repository = lambda: events.append('prepare')
        service._reconcile_catalog_and_cleanup = lambda: events.append('catalog')
        service._synchronize_pull_requests = lambda: events.append('PR') or 1
        service._resume_existing_execution = mock.Mock()
        service._run_coordinators = mock.Mock()
        self.assertEqual(service.run(), project_service.ProjectStepResult(1))
        self.assertEqual(events, ['prepare', 'catalog', 'PR'])
        service._resume_existing_execution.assert_not_called()
        service._run_coordinators.assert_not_called()

    def test_project_provider_mismatch_stops_before_repository_effects(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repo = str(Path('repo').resolve())
        connection.execute('UPDATE projects SET repo = ?', (repo,))
        connection.commit()
        with mock.patch.object(loop_manager, 'observe_blocked_plan_remotes') as observe, \
             mock.patch.object(loop_manager, 'remote_base') as fetch:
            with self.assertRaisesRegex(RuntimeError, '변경할 수 없습니다'):
                loop_manager._run_project_step(connection, Path('/state'), repo,
                    Path('/config.ini'), None, False, requested_provider='claude', registered_only=True)
        observe.assert_not_called()
        fetch.assert_not_called()
