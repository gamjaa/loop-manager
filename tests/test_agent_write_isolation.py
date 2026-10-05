"""Provider write policy, configuration isolation and invocation evidence."""

import sqlite3
from pathlib import Path
import tempfile
from unittest import TestCase, mock
from tests.support.workspace import symlink_or_skip

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.agents import agent_write_isolation as isolation
from loop_manager_internal.storage import agent_invocation_state as state
import loop_manager as manager


class WriteIsolationTests(TestCase):
    def setUp(self):
        # 임시 worktree의 상위 사용자 설정이 테스트로 유입되지 않게 한다.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / '.git').mkdir()
        temporary_root = mock.patch.object(tempfile, 'tempdir', str(root))
        temporary_root.start()
        self.addCleanup(temporary_root.stop)
        home = mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(root / 'codex-home')})
        home.start()
        self.addCleanup(home.stop)

    def test_worktree_inspection_rejects_alias_and_unregistered_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_root = root / 'state'
            selected = state_root / 'worktrees' / 'project' / 'G-088'
            source = root / 'source'
            common = source / '.git'
            git_dir = common / 'worktrees' / 'G-088'
            for path in (selected, git_dir):
                path.mkdir(parents=True)
            listed = f'worktree {source}\nHEAD abc\n\nworktree {selected}\nHEAD def\n'

            def git(path, *args):
                if args[-1] == '--show-toplevel':
                    return str(selected)
                if args[-1] == '--git-common-dir':
                    return str(common)
                if args[-1] == '--absolute-git-dir':
                    return str(git_dir)
                if args[-1] == '--porcelain':
                    return listed
                if args[-2:] == ('get-url', 'origin'):
                    return 'https://example.invalid/owner/repo.git'
                raise AssertionError(args)

            with mock.patch.object(isolation, '_git', side_effect=git):
                boundary = isolation.inspect_worktree(selected, state_root)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(source, state_root, read_only=True)
                self.assertEqual((boundary.worktree, boundary.source,
                                  boundary.common_dir), (selected, source, common))
                alias = selected.parent / 'alias'
                symlink_or_skip(alias, selected, target_is_directory=True)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(alias, state_root)
                unrelated = root / 'unrelated'
                unrelated.mkdir()
                with mock.patch.object(isolation, '_git', side_effect=lambda path, *args:
                                       listed.replace(f'worktree {selected}',
                                                      f'worktree {unrelated}')
                                       if args[-1] == '--porcelain' else git(path, *args)):
                    with self.assertRaisesRegex(isolation.IsolationError, '등록되지'):
                        isolation.inspect_worktree(selected, state_root)

    def test_codex_policy_preserves_auto_review_and_denies_broad_writes(self):
        import tomllib

        args = isolation.codex_arguments(False)
        self.assertIn('approvals_reviewer="auto_review"', args)
        self.assertTrue(any('sandbox_approval=false' in item and
                            'request_permissions=false' in item for item in args))
        self.assertIn('sandbox_workspace_write.exclude_slash_tmp=true', args)
        self.assertIn('sandbox_workspace_write.exclude_tmpdir_env_var=true', args)
        self.assertIn('mcp_servers={}', args)
        self.assertIn('features.plugins=false', args)
        self.assertIn('features.apps=false', args)
        self.assertNotIn('--approve-for-me', args)
        self.assertIn('--ignore-rules', args)
        self.assertEqual(args[args.index('--sandbox') + 1], 'workspace-write')
        self.assertEqual(isolation.codex_arguments(True)[1], 'read-only')
        for read_only in (False, True):
            policy = next(arg for arg in isolation.codex_arguments(read_only)
                          if arg.startswith('approval_policy='))
            self.assertEqual(tomllib.loads(policy)['approval_policy'], {'granular': {
                'sandbox_approval': False, 'request_permissions': False,
                'rules': True, 'mcp_elicitations': True, 'skill_approval': True,
            }})

    def test_unsupported_codex_version_rejects_before_native_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repo')
            for version in ('codex-cli 0.159.2', 'codex-cli 0.158.0', 'unknown'):
                for session in (None, 'saved-session'):
                    with self.subTest(version=version, session=session):
                        records = []
                        with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                             mock.patch.object(isolation, '_configuration_digest', return_value='config'), \
                             mock.patch.object(isolation, '_assert_no_codex_execpolicy'), \
                             mock.patch.object(isolation, '_assert_no_codex_extensions'), \
                             mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                             mock.patch.object(isolation.subprocess, 'run', return_value=
                                               manager.subprocess.CompletedProcess([], 0, version, '')), \
                             mock.patch.object(isolation, 'prepare_launch') as launch:
                            with self.assertRaisesRegex(isolation.IsolationError, '자동 검토 버전'):
                                isolation.prepare_isolation(
                                    'codex', 'worker', root, root, False, records.append,
                                    lambda: 'snapshot', expected_repository=str(boundary.source),
                                    session_id=session, environment_variables={})
                        launch.assert_not_called()
                        self.assertEqual(records[0].status, 'rejected')
                        self.assertEqual(records[0].cli_version, version)
                        self.assertEqual(records[0].snapshot_digest, 'snapshot')

    def test_execpolicy_rule_blocks_unproven_sandbox_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_home = root / 'codex-home'
            rules = provider_home / 'rules'
            rules.mkdir(parents=True)
            (rules / 'default.rules').write_text(
                'prefix_rule(pattern = ["git"], decision = "allow")\n',
                encoding='utf-8')
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}):
                with self.assertRaisesRegex(isolation.IsolationError, 'exec-policy'):
                    isolation._assert_no_codex_execpolicy(root)

    def test_inherited_codex_extensions_block_unproven_tool_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_home = root / 'codex-home'
            provider_home.mkdir()
            config = provider_home / 'config.toml'
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}):
                config.write_text('model = "test"\n', encoding='utf-8')
                isolation._assert_no_codex_extensions(root)
                for setting in ('[mcp_servers.external]\ncommand = "tool"\n',
                                'notify = ["external-command"]\n',
                                'default_permissions = "broad"\n',
                                '[permissions.broad.filesystem]\n"/" = "write"\n'):
                    config.write_text(setting, encoding='utf-8')
                    with self.assertRaisesRegex(isolation.IsolationError, 'MCP'):
                        isolation._assert_no_codex_extensions(root)

    def test_personal_plugin_registration_is_disabled_without_config_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            content = '[plugins."github@openai-curated"]\nenabled = true\n'
            config.write_text(content)
            with mock.patch.object(isolation, '_codex_config_files', return_value=(config,)):
                isolation._assert_no_codex_extensions(root)
            self.assertEqual(config.read_text(), content)
            for read_only in (True, False):
                args = isolation.codex_arguments(read_only)
                self.assertIn('features.plugins=false', args)
                self.assertIn('features.apps=false', args)

    def test_managed_requirements_cannot_force_extensions_on(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'requirements.toml'
            for feature in ('plugins', 'apps'):
                with self.subTest(feature=feature):
                    config.write_text('[features]\n' + feature + '=true\n')
                    with mock.patch.object(isolation, '_codex_config_files', return_value=(config,)):
                        with self.assertRaisesRegex(isolation.IsolationError, '활성화'):
                            isolation._assert_no_codex_extensions(root)

    def test_selected_codex_home_does_not_inherit_config_above_git_root(self):
        for linked in (False, True):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                personal = root / '.codex'
                personal.mkdir()
                personal_config = personal / 'config.toml'
                # A leaked MCP config must reject, unlike disabled plugin registrations.
                personal_config.write_text('[mcp_servers.personal]\ncommand = "tool"\n')
                selected = root / 'automation-codex'
                selected.mkdir()
                selected_config = selected / 'config.toml'
                selected_config.write_text('model = "test"\n')
                repository = root / 'repo'
                repository.mkdir()
                if linked:
                    (repository / '.git').write_text('gitdir: /registered/gitdir\n')
                else:
                    (repository / '.git').mkdir()
                nested = repository / 'nested'
                nested.mkdir()
                project_configs = (nested / '.codex/config.toml',
                                   repository / '.codex/config.toml')
                for config in project_configs:
                    config.parent.mkdir()
                    config.write_text('model = "test"\n')
                with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(selected)}), \
                     mock.patch.object(isolation.Path, 'home', return_value=root):
                    paths = isolation._codex_config_files(nested)
                    self.assertEqual(paths[:3], (selected_config, *project_configs))
                    self.assertNotIn(personal_config, paths)
                    isolation._assert_no_codex_extensions(nested)
                    digest = isolation._configuration_digest('codex', nested, None)
                    personal_config.write_text('invalid TOML [')
                    isolation._assert_no_codex_extensions(nested)
                    self.assertEqual(isolation._configuration_digest('codex', nested, None),
                                     digest)
                    for config in (selected_config, *project_configs):
                        with self.subTest(config=config):
                            config.write_text('[mcp_servers.external]\ncommand = "tool"\n')
                            with self.assertRaisesRegex(isolation.IsolationError, 'MCP'):
                                isolation._assert_no_codex_extensions(nested)
                            self.assertNotEqual(
                                isolation._configuration_digest('codex', nested, None), digest)
                            config.write_text('model = "test"\n')
                    self.assertEqual(personal_config.read_text(), 'invalid TOML [')

    def test_configuration_revision_changes_attempt_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_home = root / 'codex-home'
            provider_home.mkdir()
            config = provider_home / 'config.toml'
            config.write_text('model = "first"\n', encoding='utf-8')
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}):
                first = isolation._configuration_digest('codex', root, None)
                config.write_text('model = "second"\n', encoding='utf-8')
                second = isolation._configuration_digest('codex', root, None)
            self.assertNotEqual(first, second)

    def test_configuration_drift_refuses_launch_after_successful_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_home = root / 'codex-home'
            provider_home.mkdir()
            config = provider_home / 'config.toml'
            config.write_text('model = "first"\n', encoding='utf-8')
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repository')
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}):
                digest = isolation._configuration_digest('codex', root, root / 'config')
                evidence = isolation.IsolationEvidence(
                    'codex', 'codex-cli 0.159.3', 'Linux:x:y', 'worker', str(root),
                    'repository', 'policy', 'verified', snapshot_digest='snapshot',
                    configuration_digest=digest)
                with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                     mock.patch.object(isolation.subprocess, 'run', return_value=
                                       manager.subprocess.CompletedProcess(
                                           ['codex', '--version'], 0, 'codex-cli 0.159.3\n', '')):
                    isolation.assert_current(evidence, root, root, root / 'config',
                                             str(root / 'source'), False,
                                             lambda: 'snapshot')
                    config.write_text('model = "second"\n', encoding='utf-8')
                    with self.assertRaisesRegex(isolation.IsolationError, '변경됐습니다'):
                        isolation.assert_current(evidence, root, root, root / 'config',
                                                 str(root / 'source'), False,
                                                 lambda: 'snapshot')

    def test_native_tool_probe_binds_new_configuration_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_home = root / 'codex-home'
            provider_home.mkdir()
            config = provider_home / 'config.toml'
            config.write_text('model = "first"\n', encoding='utf-8')
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repository')
            records = []
            launch = mock.Mock(probe_arguments=('standalone',), arguments=('inner',))
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation, 'prepare_launch', return_value=launch), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation.subprocess, 'run', return_value=
                                   manager.subprocess.CompletedProcess(
                                       ['codex', '--version'], 0, 'codex-cli 0.159.3\n', '')):
                for session in (None, 'resume-session'):
                    result = isolation.prepare_isolation(
                        'codex', 'worker', root, root, False, records.append,
                        lambda: 'snapshot', expected_repository=str(root / 'source'),
                        session_id=session, environment_variables={})
                    self.assertIs(result, launch)
                    config.write_text('model = "second"\n', encoding='utf-8')
            self.assertEqual(launch.probe.call_count, 2)
            self.assertEqual([record.status for record in records], ['verified', 'verified'])
            self.assertNotEqual(records[0].policy_digest, records[1].policy_digest)
            self.assertEqual(records[0].repository_identity, 'repository')

    def test_unverified_codex_file_tools_stop_before_process_and_log_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            state.ensure_schema(connection)
            provider_home = root / 'codex-home'
            provider_home.mkdir()
            boundary = isolation.WorktreeBoundary(
                root, root / 'source', (), root / 'git', root / 'common', 'repository')
            log_dir = root / 'logs' / manager.hashlib.sha256(str(root).encode()).hexdigest()[:16]
            log_dir.mkdir(parents=True)
            prior = log_dir / 'planner-last.txt'
            prior.write_text('previous result', encoding='utf-8')
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation, 'prepare_launch', side_effect=RuntimeError('파일 도구 경계 미확인')) as probe, \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                   mock.Mock(serialize=lambda: 'snapshot')), \
                 mock.patch.object(isolation.subprocess, 'run', return_value=
                                   manager.subprocess.CompletedProcess(
                                       ['codex', '--version'], 0, 'codex-cli 0.159.3\n', '')), \
                 mock.patch.object(manager.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(isolation.IsolationError, '파일 도구'):
                    manager.run_agent(
                        'codex',
                        root, root, root / 'config', 'planner', 'prompt',
                        read_only=False,
                        resolved_profile=invocation.ResolvedProfile('model', 'high'),
                        connection=connection,
                        invocation=invocation.InvocationContext(str(root / 'source'), None))
            probe.assert_called_once()
            launch.assert_not_called()
            proof = connection.execute('''SELECT proof.status, proof.reason, call.result_kind
                FROM agent_isolation_evidence AS proof JOIN agent_invocations AS call
                USING (invocation_id)''').fetchone()
            self.assertEqual(proof[0], 'rejected')
            self.assertIn('파일 도구', proof[1])
            self.assertEqual(proof[2], 'exception')
            self.assertEqual(prior.read_text(encoding='utf-8'), 'previous result')

    def test_resume_rechecks_native_policy_and_records_rejection_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repo')
            records = []
            launch = mock.Mock(probe_arguments=('standalone',))
            launch.probe.side_effect = RuntimeError('sandbox unavailable')
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation, 'prepare_launch', return_value=launch), \
                 mock.patch.object(isolation.subprocess, 'run', return_value=
                                   manager.subprocess.CompletedProcess([], 0, 'codex-cli 0.159.3', '')):
                with self.assertRaisesRegex(isolation.IsolationError, 'sandbox unavailable'):
                    isolation.prepare_isolation(
                        'codex', 'worker', root, root, False, records.append,
                        lambda: 'snapshot', expected_repository=str(root / 'source'),
                        session_id='prior-session', environment_variables={})
            launch.probe.assert_called_once()
            self.assertEqual(records[0].status, 'rejected')

    def test_unsupported_provider_records_rejection_without_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repo')
            with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation.subprocess, 'run') as run:
                for role in ('planner', 'worker', 'maintenance', 'reviewer'):
                    for session in (None, 'saved-session'):
                        with self.subTest(role=role, session=session):
                            records = []
                            with self.assertRaisesRegex(isolation.IsolationError, 'claude'):
                                isolation.prepare_isolation(
                                    'claude', role, root, root, role == 'reviewer',
                                    records.append, lambda: 'snapshot',
                                    expected_repository=str(root / 'source'), session_id=session)
                            self.assertEqual(records[0].status, 'rejected')
                            self.assertEqual(records[0].repository_identity, 'repo')
                            self.assertEqual(records[0].role, role)
                run.assert_not_called()

    def test_unsupported_os_records_rejection_before_provider_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repo')
            with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation.platform, 'system', return_value='UnsupportedOS'), \
                 mock.patch.object(isolation, 'prepare_launch') as probe:
                for provider in ('codex', 'claude'):
                    with self.subTest(provider=provider):
                        records = []
                        with self.assertRaisesRegex(isolation.IsolationError, 'UnsupportedOS'):
                            isolation.prepare_isolation(
                                provider, 'worker', root, root, False, records.append,
                                lambda: 'snapshot', expected_repository=str(root / 'source'))
                        self.assertEqual(records[0].status, 'rejected')
            probe.assert_not_called()

    def test_rejected_preflight_stops_adapter_and_persists_attempt(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        profile = invocation.ResolvedProfile('model', 'high')

        def reject(*args, **_kwargs):
            args[-2](isolation.IsolationEvidence(
                'codex', 'version', 'Linux:x:y', 'worker', '/worktree',
                'repository', 'digest', 'rejected', 'sandbox unavailable'))
            raise isolation.IsolationError('sandbox unavailable')

        with mock.patch.object(isolation, 'prepare_isolation', side_effect=reject), \
             mock.patch.object(manager.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(isolation.IsolationError, 'sandbox unavailable'):
                manager.run_agent('codex', Path('/worktree'), Path('/state'),
                                  Path('/config'), 'worker', 'secret prompt',
                                  read_only=False, level=1, connection=connection,
                                  invocation=invocation.InvocationContext('repo', 'G-088'),
                                  resolved_profile=profile)
        launch.assert_not_called()
        row = connection.execute('''SELECT proof.status, proof.reason, call.result_kind
            FROM agent_isolation_evidence AS proof JOIN agent_invocations AS call
            USING (invocation_id)''').fetchone()
        self.assertEqual(row, ('rejected', 'sandbox unavailable', 'exception'))
        self.assertNotIn('secret prompt', repr(row))

    def test_claude_preflight_rejection_precedes_log_and_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []

            def reject(*args, **_kwargs):
                evidence = isolation.IsolationEvidence(
                    'claude', 'unknown', 'Linux:x:y', 'reviewer', str(root),
                    'repository', 'policy', 'rejected', 'file tool unverified')
                args[-2](evidence)
                raise isolation.IsolationError('file tool unverified')

            with mock.patch.object(isolation, 'prepare_isolation', side_effect=reject), \
                 mock.patch.object(manager.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(isolation.IsolationError, 'file tool unverified'):
                    manager.run_claude(
                        root, root, root / 'config', 'reviewer', 'prompt',
                        read_only=True,
                        resolved_profile=invocation.ResolvedProfile('model', 'high'),
                        on_isolation_checked=records.append,
                        isolation_snapshot_digest=lambda: 'snapshot',
                        expected_repository=str(root))
            launch.assert_not_called()
            self.assertEqual(records[0].status, 'rejected')
            self.assertFalse((root / 'logs').exists())

    def test_missing_launch_evidence_preserves_prior_provider_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / 'logs' / manager.hashlib.sha256(str(root).encode()).hexdigest()[:16]
            log_dir.mkdir(parents=True)
            prior = log_dir / 'planner-last.txt'
            prior.write_text('previous result', encoding='utf-8')
            with self.assertRaisesRegex(isolation.IsolationError, '증거가 없습니다'):
                manager.run_codex(
                    root, root, root / 'config', 'planner', 'prompt',
                    read_only=False,
                    resolved_profile=invocation.ResolvedProfile('model', 'high'),
                    isolation_arguments=isolation.codex_arguments(False))
            self.assertEqual(prior.read_text(encoding='utf-8'), 'previous result')

    def test_verified_evidence_is_bound_to_successful_attempt_and_session(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        profile = invocation.ResolvedProfile('model', 'high')
        evidence = isolation.IsolationEvidence(
            'codex', 'version', 'Linux:x:y', 'worker', '/worktree',
            'repository', 'policy', 'verified', snapshot_digest='snapshot',
            configuration_digest='configuration')

        class Provider:
            def run(self, request):
                request.on_isolation_checked(evidence)
                request.on_session_started('session-one')
                return manager.AgentResult(0, 'done', 'session-one')

        with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), Path('/state'), Path('/config'),
                'worker', 'prompt', read_only=False, level=1,
                connection=connection,
                invocation=invocation.InvocationContext('repo', 'G-088'),
                resolved_profile=profile)
        ledger = state.InvocationRepository(connection)
        self.assertTrue(ledger.verified_invocation(result.isolation_invocation_id,
                                                    session_id='session-one',
                                                    expected=evidence))
        self.assertFalse(ledger.verified_invocation(result.isolation_invocation_id,
                                                     session_id='another-session',
                                                     expected=evidence))
        self.assertFalse(ledger.verified_invocation(result.isolation_invocation_id,
                                                     session_id='session-one',
                                                     expected=isolation.IsolationEvidence(
                                                         'codex', 'version', 'Linux:x:y',
                                                         'worker', '/worktree', 'repository',
                                                         'policy', 'verified',
                                                         snapshot_digest='changed',
                                                         configuration_digest='configuration')))
        self.assertFalse(ledger.verified_invocation(result.isolation_invocation_id,
                                                     session_id='session-one',
                                                     expected=isolation.IsolationEvidence(
                                                         'codex', 'version', 'Linux:x:y',
                                                         'worker', '/worktree', 'repository',
                                                         'policy', 'verified',
                                                         snapshot_digest='snapshot',
                                                         configuration_digest='changed')))
        self.assertFalse(ledger.verified_invocation('unrecorded', session_id=None,
                                                    expected=evidence))
        self.assertFalse(ledger.verified_invocation(
            result.isolation_invocation_id, session_id='session-one',
            expected=isolation.IsolationEvidence(
                'codex', 'version', 'Linux:x:y', 'worker', '/worktree',
                'repository', 'policy', 'rejected', snapshot_digest='snapshot',
                configuration_digest='configuration')))
        connection.execute('UPDATE agent_invocations SET result_kind = ? WHERE invocation_id = ?',
                           ('interrupted', result.isolation_invocation_id))
        self.assertFalse(ledger.verified_invocation(result.isolation_invocation_id,
                                                    session_id='session-one',
                                                    expected=evidence))

    def test_interrupted_pending_attempt_invalidates_preflight_evidence(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        ledger = state.InvocationRepository(connection)
        for status in ('verified', 'prepared'):
            with self.subTest(status=status):
                identity = ledger.start(invocation.InvocationContext('repo', 'G-088'),
                                        'worker', 'codex',
                                        invocation.ResolvedProfile('model', 'high'),
                                        'saved-session')
                evidence = isolation.IsolationEvidence(
                    'codex', 'version', 'Linux:x:y', 'worker', '/worktree', 'repo',
                    'policy', status, snapshot_digest='snapshot')
                ledger.record_isolation(identity, evidence)
                self.assertEqual(ledger.reconcile_interrupted(), 1)
                row = connection.execute('''SELECT status, reason FROM agent_isolation_evidence
                    WHERE invocation_id = ?''', (identity,)).fetchone()
                self.assertEqual(row, ('invalidated', 'agent_attempt_interrupted'))
                self.assertFalse(ledger.verified_invocation(identity, session_id='saved-session',
                                                            expected=evidence))
                session = connection.execute('''SELECT session_id FROM agent_invocations
                    WHERE invocation_id = ?''', (identity,)).fetchone()[0]
                self.assertEqual(session, 'saved-session')

    def test_failed_legacy_preparation_invalidates_only_its_own_evidence(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        ledger = state.InvocationRepository(connection)
        identities = []
        for session in ('saved-session', 'other-session'):
            identity = ledger.start(invocation.InvocationContext('repo', 'G-097'),
                                    'worker', 'codex',
                                    invocation.ResolvedProfile('model', 'high'), session)
            identities.append(identity)
            ledger.record_isolation(identity, isolation.IsolationEvidence(
                'codex', 'version', 'Linux:x:y', 'worker', '/worktree', 'repo',
                'policy', 'prepared', snapshot_digest='snapshot'))
        ledger.finish(identities[0], 'exception', None, 1, None, invocation.Usage())
        rows = connection.execute('''SELECT call.session_id, proof.status, proof.reason
            FROM agent_invocations call JOIN agent_isolation_evidence proof USING(invocation_id)
            ORDER BY call.started_at''').fetchall()
        self.assertEqual(rows, [('saved-session', 'invalidated', 'agent_attempt_not_successful'),
                                ('other-session', 'prepared', '')])
