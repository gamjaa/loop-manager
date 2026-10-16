"""Unit regressions for import purity, fixture lifetime and scoped compatibility."""
import ast
import json
from contextlib import ExitStack, nullcontext
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.execution.worker_session import SessionBinding
from loop_manager_internal.git import manager_git_journal
from tests.support.execution import ExecutionFixture, execution_fixture
from tests.support import database as database_fixture
from tests.support import notification as notification_fixture
from tests.support import recovery as recovery_fixture
from tests.support import review_escalation as escalation_fixture
from tests.support import workspace as workspace_fixture
from tests.support import worker as worker_fixture
from tests.support.worker import dummy_worker_context, dummy_worker_sessions, legacy_planning_ports
from tests.support.origin import OriginScopeFixture


class SupportFixtureTests(unittest.TestCase):
    def test_fixed_guidance_covers_digest_and_baseline_until_cleanup(self):
        from tests.support.fixed_samples import PROJECT_FILES, use_fixed_guidance
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage import planning_input_state

        connection = database_fixture.runtime_connection()
        self.addCleanup(connection.close)
        read = common_guidance._read
        def read_sample(path):
            self.assertTrue(path.is_relative_to(PROJECT_FILES), path)
            return read(path)
        with (mock.patch.object(common_guidance, 'RUNTIME_ROOT', Path('/missing-runtime')),
              mock.patch.object(common_guidance, '_read', side_effect=read_sample)):
            with ExitStack() as cleanup:
                use_fixed_guidance(SimpleNamespace(addCleanup=cleanup.callback))
                before = loop_manager._planning_input_digest([(b'app.py', b'blob', b'one')])
                planning_input_state.record_general(connection, 'repo', 'base', {})
                self.assertFalse(planning_input_state.guidance_changed(connection, 'repo'))
                self.assertEqual(before, loop_manager._planning_input_digest([(b'app.py', b'blob', b'one')]))
            self.assertEqual(common_guidance.RUNTIME_ROOT, Path('/missing-runtime'))

    def test_worker_decorator_keeps_planning_baseline_stable_across_nested_context(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage import planning_input_state
        from tests.support.fixed_samples import PROJECT_FILES

        connection = database_fixture.runtime_connection()
        self.addCleanup(connection.close)
        read = common_guidance._read
        def read_sample(path):
            self.assertTrue(path.is_relative_to(PROJECT_FILES), path)
            return read(path)

        @dummy_worker_sessions
        def scenario():
            planning_input_state.record_general(connection, 'repo', 'base', [])
            baseline = connection.execute(
                'SELECT guidance_hash FROM planning_input_baselines').fetchone()[0]
            with dummy_worker_context(roles=('worker', 'planner_check')):
                self.assertEqual(common_guidance.runtime_content_hash(), baseline)
            self.assertEqual(common_guidance.runtime_content_hash(), baseline)

        with (mock.patch.object(common_guidance, 'RUNTIME_ROOT', Path('/unavailable-runtime')),
              mock.patch.object(common_guidance, '_read', side_effect=read_sample)):
            scenario()
            self.assertEqual(common_guidance.RUNTIME_ROOT, Path('/unavailable-runtime'))

    def test_pr_recovery_pins_fixed_guidance_for_later_hash_consumers(self):
        from tests.support import pr_recovery
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage import runtime_state

        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            repo = Path(directory) / 'repo'
            (repo / 'docs').mkdir(parents=True)
            def make_worktree(_repo, worktree, *_args):
                (worktree / 'src').mkdir(parents=True)
            def register(connection, repo_key, *_args):
                database_fixture.database._ensure_execplans_schema(connection)
                database_fixture.AutomationCatalogFixture()._insert_plan(
                    connection, 'G-026', 'READY', repo=repo_key)
                runtime_state.RuntimeStateRepository(connection).activate(
                    repo_key, 'G-026', activation_sha='a' * 40,
                    branch='branch', worktree='/candidate')
            for target, options in {
                'tests.support.pr_recovery.make_remote_repository': {'return_value': repo},
                'tests.support.pr_recovery.git_command': {'return_value': 'a' * 40},
                'tests.support.pr_recovery.commit_and_push': {},
                'loop_manager.make_worktree': {'side_effect': make_worktree},
                'loop_manager.register_execplans': {'side_effect': register},
                'loop_manager._parse_execplan': {'return_value': {}},
                'loop_manager.specification_input_fingerprint': {'return_value': 'spec'},
                'loop_manager.planning_input_fingerprint': {'return_value': 'planning'},
                'loop_manager.plan_declarations': {'return_value': []},
                'tests.support.publication.git_command': {'return_value': 'a' * 40},
                'loop_manager._revision_execplan_body_hash': {'return_value': 'body'},
                'loop_manager._revision_execplan_source_fingerprint': {'return_value': 'source'},
                'loop_manager_internal.agents.common_guidance.RUNTIME_ROOT': {'new': Path('/unavailable-runtime')},
            }.items():
                patches.enter_context(mock.patch(target, **options))
            fixture = pr_recovery.PrRecoveryFixture()
            fixture.addCleanup = patches.callback
            result = fixture._pr_open_state(directory)
            connection = result[2]
            patches.callback(connection.close)
            with mock.patch.object(common_guidance, 'runtime_content_hash',
                                   side_effect=AssertionError('runtime fallback')):
                saved_hash = common_guidance.plan_content_hash(connection, str(repo), 'G-026')
            snapshot = connection.execute('SELECT definition_snapshot FROM exec_plans').fetchone()[0]
            self.assertEqual(json.loads(snapshot)['guidance_hash'], saved_hash)

    def test_publication_hash_and_worker_followup_use_fixed_guidance(self):
        from loop_manager_internal.agents import common_guidance
        from tests.support.fixed_samples import PROJECT_FILES
        from tests.support.publication import seed_validated_candidate

        fixture = execution_fixture(self)
        read = common_guidance._read
        def read_sample(path):
            self.assertTrue(path.is_relative_to(PROJECT_FILES), path)
            return read(path)

        with (mock.patch.object(common_guidance, 'RUNTIME_ROOT', Path('/unavailable-runtime')),
              mock.patch.object(common_guidance, '_read', side_effect=read_sample),
              mock.patch('tests.support.publication.git_command', side_effect=['base', 'candidate']),
              mock.patch.object(loop_manager, '_revision_execplan_body_hash', return_value='body'),
              mock.patch.object(loop_manager, '_revision_execplan_source_fingerprint', return_value='source')):
            snapshot = seed_validated_candidate(fixture.connection, Path('repo'), Path('/candidate'), 'G-047')
            @dummy_worker_sessions
            def followup():
                return common_guidance.plan_content_hash(fixture.connection, 'repo', 'G-047')
            self.assertEqual(followup(), snapshot.guidance_hash)
            self.assertEqual(common_guidance.RUNTIME_ROOT, Path('/unavailable-runtime'))
            row = fixture.connection.execute(
                'SELECT verify_result, verify_token, review_token FROM exec_plans').fetchone()
            self.assertEqual(row['verify_token'], snapshot.verify_token('base', 'candidate', row['verify_result']))
            self.assertEqual(row['review_token'], snapshot.review_token('base', 'candidate'))

    def test_execution_review_hash_uses_fixed_guidance_and_restores_runtime_root(self):
        from loop_manager_internal.agents import common_guidance
        from tests.support.fixed_samples import PROJECT_FILES

        original_root = common_guidance.RUNTIME_ROOT
        read = common_guidance._read
        def read_sample(path):
            self.assertTrue(path.is_relative_to(PROJECT_FILES), path)
            return read(path)

        with mock.patch.object(common_guidance, '_read', side_effect=read_sample) as reads:
            with ExitStack() as cleanup:
                owner = SimpleNamespace(addCleanup=cleanup.callback,
                                        assertEqual=self.assertEqual, fail=self.fail)
                fixture = execution_fixture(owner)
                context = fixture.review_context()
                self.assertEqual(fixture.handler._definition_snapshot.guidance_hash,
                                 common_guidance.runtime_content_hash())
                outcome = fixture.handler.review(context)
                self.assertEqual(outcome.context.phase.value, 'PUBLISHING')
                self.assertEqual({call.args[0] for call in reads.call_args_list},
                                 {PROJECT_FILES / path for path in common_guidance.GUIDANCE_PATHS})
            self.assertEqual(common_guidance.RUNTIME_ROOT, original_root)

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
        self.assertEqual((row['repo'], row['phase'], row['agent_role']), (str(source), 'PLANNING', 'planner'))
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

    def test_dummy_profile_can_cover_requested_planner_roles_without_changing_default(self):
        original = mock.Mock(return_value='real profile')
        with mock.patch.object(loop_manager, '_agent_profile', original):
            roles = ('worker', 'planner', 'maintenance_planner', 'planner_check')
            with dummy_worker_context(roles=roles):
                for role in roles:
                    with self.subTest(role=role):
                        self.assertEqual(loop_manager._agent_profile(
                            Path('unused.ini'), 'codex', role,
                            1 if role == 'worker' else None, Path('/managed')),
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
        with mock.patch.object(loop_manager, '_ExecutionLifecyclePorts', ports), \
             mock.patch.object(workspace_fixture, 'pin_common_guidance') as pin:
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
            pin.assert_not_called()

    def test_pr_body_observation_uses_pushed_remote_head_without_git_execution(self):
        from tests.support import pr_recovery
        from loop_manager_internal.documents.pr_description import refresh_common_guidance_pr
        pr = {'url': 'https://example.test/pr/34', 'state': 'OPEN',
              'headRefName': 'loop-manager/G-034', 'headRefOid': 'old-checkpoint', 'body': ''}
        repo, worktree = Path('/repo'), Path('/worktree')
        branch_ref = 'refs/heads/loop-manager/G-034'
        with mock.patch.object(pr_recovery, 'git_command',
                side_effect=[f'{head}\t{branch_ref}' for head in
                             ('first-push', 'final-push', 'final-push')]) as git:
            for head in ('first-push', 'final-push'):
                command = mock.Mock(side_effect=lambda *_a, **_k: SimpleNamespace(
                    returncode=0, stdout=json.dumps(pr_recovery.observe_live_pull_request(repo, pr))))
                refresh_common_guidance_pr(command, worktree, Path('/state'),
                    {**pr, 'headRefOid': head}, '')
                command.assert_called_once_with(['gh', 'pr', 'view', pr['url'], '--json',
                                                'body,headRefOid,state'], cwd=worktree)
            command.reset_mock()
            with self.assertRaisesRegex(RuntimeError, '현재 HEAD'):
                refresh_common_guidance_pr(command, worktree, Path('/state'), pr, '')
            command.assert_called_once_with(['gh', 'pr', 'view', pr['url'], '--json',
                                            'body,headRefOid,state'], cwd=worktree)
            self.assertEqual(git.call_args_list, [mock.call(repo, 'ls-remote', 'origin', branch_ref)] * 3)
        self.assertEqual(pr['headRefOid'], 'old-checkpoint')

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
             mock.patch('loop_manager_internal.application.scheduler.VerifyCoordinator',
                        side_effect=lambda: nullcontext()), \
             mock.patch('loop_manager_internal.application.scheduler.TaskScheduler'), \
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

    def test_database_fixture_closes_owned_connection_after_initialization_failure(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError('schema setup failed')
        with mock.patch.object(database_fixture.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'schema setup failed'):
                database_fixture.runtime_connection()
        connection.close.assert_called_once_with()

    def test_notification_fixture_closes_owned_connection_after_initialization_failure(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError('schema setup failed')
        owner = SimpleNamespace(addCleanup=self.addCleanup)
        with mock.patch.object(notification_fixture.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'schema setup failed'):
                notification_fixture.NotificationFixture.connection(owner)
        connection.close.assert_called_once_with()

    def test_notification_fixture_leaves_file_connection_cleanup_to_its_caller(self):
        owner = mock.Mock()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            returned = notification_fixture.NotificationFixture.connection(
                owner, database=Path(directory) / 'loop-manager.db'
            )
            cleanup.callback(returned.close)
            owner.addCleanup.assert_not_called()
        with self.assertRaises(sqlite3.ProgrammingError):
            returned.execute('SELECT 1')

    def test_recovery_fixture_closes_its_owned_connection_after_initialization_failure(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError('schema setup failed')
        owner = SimpleNamespace(addCleanup=self.addCleanup)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(recovery_fixture.sqlite3, 'connect', return_value=connection):
                with self.assertRaisesRegex(RuntimeError, 'schema setup failed'):
                    recovery_fixture.BoundedRecoveryFixture._failure_state(owner, directory)
        connection.close.assert_called_once_with()

    def test_recovery_fixture_does_not_close_a_borrowed_connection(self):
        owner = SimpleNamespace(addCleanup=mock.Mock())
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            _root, returned = recovery_fixture.BoundedRecoveryFixture._failure_state(
                owner, directory, connection=connection
            )
        self.assertIs(returned, connection)
        owner.addCleanup.assert_not_called()
        self.assertEqual(connection.execute('SELECT 1').fetchone()[0], 1)

    def test_active_worker_fixture_variants_preserve_preparation_contracts(self):
        result = (Path('/repo'), Path('/state'), mock.Mock())
        owner = SimpleNamespace()
        variants = (
            (
                worker_fixture,
                worker_fixture.ActiveWorkerFixture._make_active_worker_project,
                {'plan_id': 'G-001', 'title': 'Worker Boundary',
                 'record_general_input': True, 'commit_message': 'Add worker plan'},
            ),
            (
                recovery_fixture,
                recovery_fixture.BoundedRecoveryFixture._active_worker_project,
                {'plan_id': 'G-001', 'title': 'Worker Boundary',
                 'record_general_input': True, 'commit_message': 'Add worker plan'},
            ),
            (
                escalation_fixture,
                escalation_fixture.WorkerEscalationFixture._active_worker_project,
                {'plan_id': 'G-019', 'title': 'Review Escalation',
                 'record_general_input': False, 'commit_message': 'Add escalation plan'},
            ),
        )
        for fixture_module, prepare, expected in variants:
            with self.subTest(plan_id=expected['plan_id']), mock.patch.object(
                fixture_module, 'make_active_worker_project', return_value=result
            ) as shared_prepare:
                returned = prepare(owner, '/fixture')
            self.assertIs(returned, result)
            shared_prepare.assert_called_once_with(owner, '/fixture', **expected)

    def test_active_worker_project_registers_owned_state_and_input_choice(self):
        from loop_manager_internal.agents import common_guidance
        from tests.support.fixed_samples import PROJECT_FILES
        with tempfile.TemporaryDirectory() as directory:
            for record_general_input in (False, True):
                with self.subTest(record_general_input=record_general_input):
                    scenario = Path(directory) / f'scenario-{record_general_input}'
                    scenario.mkdir()
                    repo = scenario / 'repo'
                    repo.mkdir()
                    (repo / 'docs').mkdir()
                    worktree = scenario / 'worktree'
                    with mock.patch.object(
                        worker_fixture, 'make_remote_repository', return_value=repo
                    ), mock.patch.object(
                        worker_fixture, 'commit_and_push'
                    ), mock.patch.object(
                        worker_fixture, 'git_command', return_value='plan-sha'
                    ), mock.patch.object(
                        worker_fixture.loop_manager,
                        'expected_execplan_workspace',
                        return_value=('branch', worktree),
                    ), mock.patch.object(
                        worker_fixture.loop_manager, 'make_worktree'
                    ), mock.patch.object(
                        worker_fixture.planning_input_state, 'record_general'
                    ) as record_input, mock.patch.object(
                        worker_fixture.loop_manager, 'plan_declarations', return_value=[]
                    ):
                        prepared = worker_fixture.make_active_worker_project(
                            self,
                            str(scenario),
                            plan_id='G-001',
                            title='Worker Boundary',
                            record_general_input=record_general_input,
                            commit_message='Add worker plan',
                        )
                    self.assertEqual(len(prepared), 3)
                    self.assertEqual(common_guidance.RUNTIME_ROOT, PROJECT_FILES)
                    self.assertEqual(prepared[0], repo)
                    self.assertEqual(prepared[2].execute(
                        'SELECT phase FROM projects'
                    ).fetchone()[0], 'WORKING')
                    if record_general_input:
                        record_input.assert_called_once()
                    else:
                        record_input.assert_not_called()

    def test_active_worker_fixture_closes_connection_after_initialization_failure(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError('schema setup failed')
        cleanup_callbacks = []
        owner = SimpleNamespace(addCleanup=cleanup_callbacks.append)
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            (repo / 'docs').mkdir()
            with mock.patch.object(
                worker_fixture, 'make_remote_repository', return_value=repo
            ), mock.patch.object(
                worker_fixture, 'commit_and_push'
            ), mock.patch.object(
                worker_fixture, 'git_command', return_value='plan-sha'
            ), mock.patch.object(
                worker_fixture.loop_manager,
                'expected_execplan_workspace',
                return_value=('branch', Path(directory) / 'worktree'),
            ), mock.patch.object(
                worker_fixture.loop_manager, 'make_worktree'
            ), mock.patch.object(
                worker_fixture.sqlite3, 'connect', return_value=connection
            ):
                with self.assertRaisesRegex(RuntimeError, 'schema setup failed'):
                    worker_fixture.make_active_worker_project(
                        owner,
                        directory,
                        plan_id='G-001',
                        title='Worker Boundary',
                        record_general_input=True,
                        commit_message='Add worker plan',
                    )
        connection.close.assert_called_once_with()
        cleanup_callbacks[0]()
        connection.close.assert_called_once_with()

    def test_workspace_fixture_closes_owned_connection_after_initialization_failure(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError('schema setup failed')
        owner = SimpleNamespace(addCleanup=self.addCleanup)
        with mock.patch.object(workspace_fixture.sqlite3, 'connect', return_value=connection):
            with self.assertRaisesRegex(RuntimeError, 'schema setup failed'):
                workspace_fixture.CleanupFixture._memory_connection(owner, Path('/repo'))
        connection.close.assert_called_once_with()

    def test_catalog_fixture_registers_cleanup_before_initialization(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError('schema setup failed')
        with ExitStack() as cleanup:
            owner = SimpleNamespace(addCleanup=cleanup.callback)
            with mock.patch.object(database_fixture.sqlite3, 'connect', return_value=connection):
                with self.assertRaisesRegex(RuntimeError, 'schema setup failed'):
                    database_fixture.AutomationCatalogFixture._connection(owner)
        self.assertEqual(connection.close.call_count, 1)

    def test_git_factory_uses_explicit_repository_and_preserves_seed_files(self):
        from tests.support import git as fixture_git
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(fixture_git.subprocess, 'run') as process, \
                 mock.patch.object(fixture_git, 'git_command') as git:
                repo = fixture_git._initialize_remote_repository(directory)
            process.assert_called_once()
            self.assertEqual(git.call_count, 7)
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
            self.assertEqual((repo / 'docs/requirements/Active.md').read_text(), '# [R-001] Active\n')
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
