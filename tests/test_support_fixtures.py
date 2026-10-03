"""Unit regressions for import purity, fixture lifetime and scoped compatibility."""
import ast
import json
from contextlib import ExitStack
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.execution.worker_session import SessionBinding
from loop_manager_internal.git import manager_git_journal
from tests.support.execution import ExecutionFixture, execution_fixture
from tests.support.worker import dummy_worker_context, dummy_worker_sessions, legacy_planning_ports
from tests.support.origin import OriginScopeFixture


class SupportFixtureTests(unittest.TestCase):
    def test_origin_scope_fixture_preserves_project_snapshot_without_git(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        source = Path('/source')
        before = loop_manager.GitSnapshot('source-head', 'main', {}, {}, {}, {}, {}, {})
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=before) as capture:
            saved = OriginScopeFixture()._persist_planner_scope(connection, source)
        capture.assert_called_once_with(source)
        self.assertEqual(saved, before)
        row = connection.execute('SELECT * FROM projects').fetchone()
        self.assertEqual((row['repo'], row['phase'], row['agent_role']), ('/source', 'PLANNING', 'planner'))
        self.assertEqual(loop_manager.GitSnapshot.deserialize(row['agent_snapshot']), before)
        self.assertFalse(connection.in_transaction)

    def test_imports_create_no_resources_or_discoverable_cases(self):
        modules = sorted('tests.support.' + path.stem
                         for path in Path('tests/support').glob('*.py'))
        modules.remove('tests.support.__init__')
        script = '''
import importlib, json, pathlib, sqlite3, subprocess, tempfile, unittest
from unittest import mock
import loop_manager
modules = json.loads(__import__('sys').argv[1])
with (mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('DB at import')),
      mock.patch.object(tempfile, 'TemporaryDirectory', side_effect=AssertionError('temp at import')),
      mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('process at import')),
      mock.patch.object(pathlib.Path, 'mkdir', side_effect=AssertionError('mkdir at import')),
      mock.patch.object(pathlib.Path, 'write_text', side_effect=AssertionError('write at import')),
      mock.patch.object(pathlib.Path, 'write_bytes', side_effect=AssertionError('write at import')),
      mock.patch.object(mock._patch, 'start', side_effect=AssertionError('patch at import'))):
    for name in modules:
        module = importlib.import_module(name)
        assert unittest.TestLoader().loadTestsFromModule(module).countTestCases() == 0, name
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, json.dumps(modules)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tests_and_support_do_not_import_test_fixtures(self):
        for path in Path('tests').rglob('*.py'):
            with self.subTest(path=str(path)):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                imports = [node for node in ast.walk(tree)
                           if isinstance(node, (ast.Import, ast.ImportFrom))]
                for node in imports:
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse((node.module or '').startswith('tests.test_'))
                        if node.module == 'tests':
                            self.assertFalse(any(alias.name.startswith('test_') for alias in node.names))
                    else:
                        self.assertFalse(any(alias.name.startswith(('tests.test_', 'test_'))
                                             for alias in node.names))
                if path.parent.name == 'support':
                    self.assertFalse(any(isinstance(node, ast.FunctionDef)
                                         and node.name.startswith('test_') for node in ast.walk(tree)))
                self.assertFalse(any(isinstance(node, ast.FunctionDef)
                                     and node.name in ('setUpModule', 'tearDownModule')
                                     for node in tree.body))

    def test_dummy_profile_is_explicit_and_restored_after_exception(self):
        original = mock.Mock(return_value='real profile')
        with mock.patch.object(loop_manager, '_agent_profile', original):
            with self.assertRaisesRegex(RuntimeError, 'fixture interrupted'):
                with dummy_worker_context():
                    self.assertEqual(loop_manager._agent_profile(
                        Path('unused.ini'), 'codex', 'worker', 1, Path('/managed')),
                        loop_manager.AgentProfile('fixture-worker-model', 'medium'))
                    self.assertEqual(loop_manager._agent_profile(
                        Path('unused.ini'), 'codex', 'maintenance_planner', None, Path('/managed')),
                        'real profile')
                    self.assertEqual(loop_manager._agent_profile(
                        Path('configured.ini'), 'codex', 'worker', 1, Path('/managed')), 'real profile')
                    self.assertEqual(loop_manager._agent_profile(
                        Path('unused.ini'), 'codex', 'reviewer', None, Path('/managed')), 'real profile')
                    raise RuntimeError('fixture interrupted')
            self.assertIs(loop_manager._agent_profile, original)
            self.assertEqual(loop_manager._agent_profile(
                Path('unused.ini'), 'codex', 'worker', 1, Path('/managed')), 'real profile')
            self.assertEqual(original.call_count, 4)

    def test_dummy_profile_can_cover_legacy_planner_roles_without_changing_default(self):
        original = mock.Mock(return_value='real profile')
        with mock.patch.object(loop_manager, '_agent_profile', original):
            with dummy_worker_context(roles=('worker', 'planner', 'maintenance_planner')):
                for role in ('worker', 'planner', 'maintenance_planner'):
                    with self.subTest(role=role):
                        self.assertEqual(loop_manager._agent_profile(
                            Path('unused.ini'), 'codex', role, 1, Path('/managed')),
                            loop_manager.AgentProfile('fixture-worker-model', 'medium'))
                self.assertEqual(loop_manager._agent_profile(
                    Path('unused.ini'), 'codex', 'reviewer', 1, Path('/managed')), 'real profile')
            self.assertIs(loop_manager._agent_profile, original)
            original.assert_called_once()

    def test_legacy_planning_ports_hide_tiered_only_inside_fixture(self):
        original = mock.Mock(side_effect=lambda: SimpleNamespace(tiered=object()))
        with mock.patch.object(loop_manager, '_PlanningLifecyclePorts', original):
            with legacy_planning_ports():
                self.assertFalse(hasattr(loop_manager._PlanningLifecyclePorts(), 'tiered'))
            self.assertIs(loop_manager._PlanningLifecyclePorts, original)
            self.assertTrue(hasattr(loop_manager._PlanningLifecyclePorts(), 'tiered'))

    def test_dummy_session_binding_only_updates_opted_in_legacy_session(self):
        repository = mock.Mock()
        saved = SimpleNamespace(worker_session_id='session', worker_session_binding=None)
        repository.projected.return_value = saved
        ports = mock.Mock(return_value='real ports')
        identity = dict(config_path=Path('unused.ini'), repo='repo', provider='codex')
        with mock.patch.object(loop_manager, '_ExecutionLifecyclePorts', ports):
            with dummy_worker_context():
                self.assertEqual(loop_manager._ExecutionLifecyclePorts('db', repository, **identity), 'real ports')
                repository.update_project.assert_called_once_with('repo', {
                    'worker_session_binding': SessionBinding(
                        'codex', 'fixture-worker-model', 'medium').serialize(),
                }, enqueue=False)
                repository.update_project.reset_mock()
                for binding, session, config in (
                    ('already-bound', 'session', 'unused.ini'),
                    (None, None, 'unused.ini'),
                    (None, 'session', 'configured.ini'),
                ):
                    with self.subTest(binding=binding, session=session, config=config):
                        saved.worker_session_binding = binding
                        saved.worker_session_id = session
                        loop_manager._ExecutionLifecyclePorts('db', repository,
                            **{**identity, 'config_path': Path(config)})
                        repository.update_project.assert_not_called()
            self.assertIs(loop_manager._ExecutionLifecyclePorts, ports)
            loop_manager._ExecutionLifecyclePorts('db', repository, **identity)
            repository.update_project.assert_not_called()
            self.assertEqual(ports.call_count, 5)

    def test_decorated_case_does_not_change_following_profile_lookup(self):
        real_profile = mock.Mock(return_value='unpatched')
        @dummy_worker_sessions
        def legacy_case():
            return loop_manager._agent_profile(Path('unused.ini'), 'codex', 'worker', 1, Path('/managed'))
        with mock.patch.object(loop_manager, '_agent_profile', real_profile):
            self.assertEqual(legacy_case().model, 'fixture-worker-model')
            self.assertIs(loop_manager._agent_profile, real_profile)
            self.assertEqual(loop_manager._agent_profile(
                Path('unused.ini'), 'codex', 'worker', 1, Path('/managed')), 'unpatched')
            real_profile.assert_called_once()

    def test_dummy_profiles_cover_scheduler_threads_and_restore_after_join(self):
        projects = (SimpleNamespace(repo='blocked'), SimpleNamespace(repo='runnable'))
        config = loop_manager.ManagedConfig(Path('/fixture/unused.ini'), 2, '', '')
        configured = loop_manager.AgentProfile('configured-planner', 'high')

        def real_profile(config_path, provider, role_name, level, worktree):
            if role_name == 'worker':
                raise loop_manager.ManagedConfigError('missing worker config')
            return configured

        def run_project(project, state_dir, config_path, *_args, **_kwargs):
            role_name = 'planner' if project.repo == 'blocked' else 'worker'
            profile = loop_manager._agent_profile(
                config_path, 'codex', role_name, 1, Path(project.repo))
            return project.repo, profile, threading.get_ident()

        @dummy_worker_sessions
        def run_scheduler():
            return loop_manager.schedule_registered_projects(None, Path('/state'), config)

        with mock.patch.object(loop_manager, '_agent_profile', side_effect=real_profile) as profile, \
             mock.patch.object(loop_manager.project_registry, 'list_projects', return_value=projects), \
             mock.patch.object(loop_manager, '_run_registered_project', side_effect=run_project):
            results = run_scheduler()
            self.assertEqual([(repo, value) for repo, value, _thread in results], [
                ('blocked', configured),
                ('runnable', loop_manager.AgentProfile('fixture-worker-model', 'medium')),
            ])
            self.assertTrue(all(thread != threading.get_ident() for _repo, _value, thread in results))
            profile.assert_called_once_with(
                config.path, 'codex', 'planner', 1, Path('blocked'))
            self.assertIs(loop_manager._agent_profile, profile)
            with self.assertRaisesRegex(loop_manager.ManagedConfigError, 'missing worker config'):
                loop_manager._agent_profile(config.path, 'codex', 'worker', 1, Path('runnable'))

    def test_execution_fixtures_isolate_state_and_close_nested_resources(self):
        original = manager_git_journal.begin_rebase
        first = execution_fixture(self)
        first_patch = manager_git_journal.begin_rebase
        first.state.update_project('repo', worker_session_id='first-only')
        second = execution_fixture(self)
        self.assertIsNone(second.state.row()['worker_session_id'])
        self.assertEqual(first.state.row()['worker_session_id'], 'first-only')
        second.doCleanups()
        self.assertIs(manager_git_journal.begin_rebase, first_patch)
        with self.assertRaises(sqlite3.ProgrammingError):
            second.connection.execute('SELECT 1')
        first.doCleanups()
        self.assertIs(manager_git_journal.begin_rebase, original)
        with self.assertRaises(sqlite3.ProgrammingError):
            first.connection.execute('SELECT 1')

    def test_failed_setup_keeps_cleanup_registered_with_owner(self):
        original = manager_git_journal.begin_rebase
        connection = sqlite3.connect(':memory:')
        with ExitStack() as cleanup:
            owner = SimpleNamespace(addCleanup=cleanup.callback,
                                    assertEqual=self.assertEqual, fail=self.fail)
            def interrupted(fixture):
                fixture.addCleanup(connection.close)
                patch = mock.patch.object(manager_git_journal, 'begin_rebase')
                patch.start()
                fixture.addCleanup(patch.stop)
                raise RuntimeError('setup failed')
            with mock.patch.object(ExecutionFixture, 'setUp', interrupted):
                with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                    execution_fixture(owner)
            self.assertIsNot(manager_git_journal.begin_rebase, original)
        self.assertIs(manager_git_journal.begin_rebase, original)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')

    def test_git_factory_uses_explicit_repository_and_preserves_seed_files(self):
        import tempfile
        from tests.support import git as fixture_git
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(fixture_git.subprocess, 'run') as process, \
                 mock.patch.object(fixture_git, 'git_command') as git:
                repo = fixture_git.make_remote_repository(directory)
            process.assert_called_once()
            self.assertEqual(process.call_args.args[0], [
                'git', 'init', '--bare', '--initial-branch=main',
                str(Path(directory) / 'remote.git'),
            ])
            self.assertEqual(git.call_args_list[:4], [
                mock.call(repo, 'init', '--initial-branch=main'),
                mock.call(repo, 'config', 'user.email', 'loop-manager@example.com'),
                mock.call(repo, 'config', 'user.name', 'Loop Manager Test'),
                mock.call(repo, 'remote', 'add', 'origin', str(Path(directory) / 'remote.git')),
            ])
            self.assertEqual((repo / 'src/app.py').read_text(), 'VALUE = 1\n')
            self.assertEqual((repo / 'docs/requirements/Active.md').read_text(), '# Active\n')
            self.assertEqual(git.call_args_list[-2:], [
                mock.call(repo, 'commit', '-m', 'Initial'),
                mock.call(repo, 'push', '-u', 'origin', 'main'),
            ])

    def test_workspace_factory_uses_owned_destination_without_running_git(self):
        from tests.support import workspace
        root, repo = Path('/fixture'), Path('/fixture/repo')
        branch, worktree = loop_manager.expected_execplan_workspace(root / 'state', str(repo), 'G-012')
        with mock.patch.object(workspace, 'make_remote_repository', return_value=repo) as repository, \
             mock.patch.object(loop_manager, 'make_worktree') as create:
            result = workspace.WorkspaceFixture()._workspace(str(root))
        self.assertEqual(result, (repo, root / 'state', branch, worktree))
        repository.assert_called_once_with(str(root))
        create.assert_called_once_with(repo, worktree, branch, 'main')
