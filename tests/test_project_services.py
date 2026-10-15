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


class ProjectServiceCompositionTests(unittest.TestCase):
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

    def test_execution_planning_waits_only_for_controlling_git_operations(self):
        effects = git_ports.PlanningGitPortsEffects(**{field.name: mock.Mock()
            for field in fields(git_ports.PlanningGitPortsEffects)})
        port = git_ports._PlanningGitPorts(effects, None, Path('/repo'), Path('/state'))
        with tempfile.TemporaryDirectory() as directory:
            for markers, pending in (({}, False), ({'MERGE_MSG': 'old'}, False),
                                     ({'rebase-merge': 'active'}, True), ({'MERGE_HEAD': 'active'}, True)):
                with self.subTest(markers=markers):
                    effects._git_operation_entries.return_value = markers
                    self.assertEqual(port.execution_operation_pending(Path(directory)), pending)
            self.assertTrue(port.execution_operation_pending(Path(directory) / 'missing'))

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
        # Keep the existing strict failure-report contract through the relocated export.
        with self.assertRaisesRegex(loop_manager.WorkerResultError, '해결되지 않은 targeted'):
            loop_manager.parse_worker_result(message + '\n이번 재검증에서 실패 없음.', plan)

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
