"""Physical Agent invocation observations and tolerant provider usage parsing."""

import sqlite3
from pathlib import Path
import tempfile
import io
import json
import threading
from unittest import TestCase, mock, skipUnless

import agent_invocation as invocation
import agent_write_isolation as isolation
import agent_codex_sandbox as envelope
import agent_sandbox_exec as gate
import agent_invocation_state as state
import agent_provider_usage as provider_usage
import loop_manager as manager


class WriteIsolationTests(TestCase):
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
                alias.symlink_to(selected, target_is_directory=True)
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
        args = isolation.codex_arguments(False)
        self.assertIn('approvals_reviewer="auto_review"', args)
        self.assertTrue(any('sandbox_approval=false' in item and
                            'request_permissions=false' in item for item in args))
        self.assertIn('sandbox_workspace_write.exclude_slash_tmp=true', args)
        self.assertIn('sandbox_workspace_write.exclude_tmpdir_env_var=true', args)
        self.assertIn('mcp_servers={}', args)
        self.assertNotIn('--approve-for-me', args)
        self.assertEqual(args[args.index('--sandbox') + 1], 'workspace-write')
        self.assertEqual(isolation.codex_arguments(True)[1], 'read-only')

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
                                '[plugins.external]\nenabled = true\n',
                                'notify = ["external-command"]\n'):
                    config.write_text(setting, encoding='utf-8')
                    with self.assertRaisesRegex(isolation.IsolationError, 'MCP'):
                        isolation._assert_no_codex_extensions(root)

    def test_selected_codex_home_does_not_inherit_config_above_git_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            personal = root / '.codex'
            personal.mkdir()
            (personal / 'config.toml').write_text('[plugins.personal]\nenabled = true\n')
            selected = root / 'automation-codex'
            selected.mkdir()
            (selected / 'config.toml').write_text('model = "test"\n')
            repository = root / 'repo'
            repository.mkdir()
            (repository / '.git').write_text('gitdir: /registered/gitdir\n')
            nested = repository / 'nested'
            nested.mkdir()
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(selected)}):
                paths = isolation._codex_config_files(nested)
                self.assertIn(selected / 'config.toml', paths)
                self.assertIn(repository / '.codex/config.toml', paths)
                self.assertNotIn(personal / 'config.toml', paths)
                isolation._assert_no_codex_extensions(nested)
                project = repository / '.codex'
                project.mkdir()
                (project / 'config.toml').write_text('[mcp_servers.external]\ncommand = "tool"\n')
                with self.assertRaisesRegex(isolation.IsolationError, 'MCP'):
                    isolation._assert_no_codex_extensions(nested)

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
                    'codex', 'codex version', 'Linux:x:y', 'worker', str(root),
                    'repository', 'policy', 'verified', snapshot_digest='snapshot',
                    configuration_digest=digest)
                with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                     mock.patch.object(isolation.subprocess, 'run', return_value=
                                       manager.subprocess.CompletedProcess(
                                           ['codex', '--version'], 0, 'codex version\n', '')):
                    isolation.assert_current(evidence, root, root, root / 'config',
                                             str(root / 'source'), False,
                                             lambda: 'snapshot')
                    config.write_text('model = "second"\n', encoding='utf-8')
                    with self.assertRaisesRegex(isolation.IsolationError, '변경됐습니다'):
                        isolation.assert_current(evidence, root, root, root / 'config',
                                                 str(root / 'source'), False,
                                                 lambda: 'snapshot')

    def test_full_cli_probe_binds_new_configuration_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_home = root / 'codex-home'
            provider_home.mkdir()
            config = provider_home / 'config.toml'
            config.write_text('model = "first"\n', encoding='utf-8')
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repository')
            records = []
            launch = mock.Mock(prefix=('native', 'envelope'), arguments=('inner',))
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation, 'prepare_launch', return_value=launch), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation.subprocess, 'run', return_value=
                                   manager.subprocess.CompletedProcess(
                                       ['codex', '--version'], 0, 'codex version\n', '')):
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
                                       ['codex', '--version'], 0, 'codex version\n', '')), \
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

    def test_resume_rechecks_envelope_and_records_rejection_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repo')
            records = []
            launch = mock.Mock(prefix=('native',))
            launch.probe.side_effect = RuntimeError('sandbox unavailable')
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation, 'prepare_launch', return_value=launch), \
                 mock.patch.object(isolation.subprocess, 'run', return_value=
                                   manager.subprocess.CompletedProcess([], 0, 'version', '')):
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
            records = []
            with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'):
                with self.assertRaisesRegex(isolation.IsolationError, 'claude'):
                    isolation.prepare_isolation('claude', 'worker', root, root,
                                                False, records.append, lambda: 'snapshot',
                                                expected_repository=str(root / 'source'))
            self.assertEqual(records[0].status, 'rejected')
            self.assertEqual(records[0].repository_identity, 'repo')

    def test_native_windows_records_rejection_before_codex_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repo')
            records = []
            with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation.platform, 'system', return_value='Windows'), \
                 mock.patch.object(isolation, 'prepare_launch') as probe:
                with self.assertRaisesRegex(isolation.IsolationError, 'Windows'):
                    isolation.prepare_isolation(
                        'codex', 'worker', root, root, False, records.append,
                        lambda: 'snapshot', expected_repository=str(root / 'source'))
            probe.assert_not_called()
            self.assertEqual(records[0].status, 'rejected')

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
        identity = ledger.start(invocation.InvocationContext('repo', 'G-088'),
                                'worker', 'codex',
                                invocation.ResolvedProfile('model', 'high'),
                                'saved-session')
        evidence = isolation.IsolationEvidence(
            'codex', 'version', 'Linux:x:y', 'worker', '/worktree', 'repo',
            'policy', 'verified', snapshot_digest='snapshot')
        ledger.record_isolation(identity, evidence)
        self.assertEqual(ledger.reconcile_interrupted(), 1)
        row = connection.execute('''SELECT status, reason FROM agent_isolation_evidence
            WHERE invocation_id = ?''', (identity,)).fetchone()
        self.assertEqual(row, ('invalidated', 'agent_attempt_interrupted'))
        self.assertFalse(ledger.verified_invocation(identity, session_id='saved-session',
                                                    expected=evidence))


@skipUnless(isolation.platform.system() == "Linux", "native mount proof requires Linux")
class NativeWriteIsolationTests(TestCase):
    def boundary(self, root):
        state_root = root / 'state'
        selected = state_root / 'worktrees' / 'project' / 'G-088'
        source = root / 'source'
        other = root / 'other'
        common = source / '.git'
        git_dir = common / 'worktrees' / 'G-088'
        home = root / 'user-codex'
        for path in (selected, other, git_dir, home):
            path.mkdir(parents=True)
        (selected / '.git').write_text('gitdir: ' + str(git_dir))
        for path in (common / 'HEAD', git_dir / 'HEAD'):
            path.write_text('ref: refs/heads/main\n')
        (home / 'auth.json').write_text('private-auth-placeholder')
        (home / 'config.toml').write_text('model="test"\n')
        return isolation.WorktreeBoundary(selected, source, (other,), git_dir, common,
                                         'repository'), state_root, home

    def test_native_envelope_keeps_auth_read_only_and_limits_runtime_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home = self.boundary(Path(directory))
            environment = {'CODEX_HOME': str(home), 'PATH': '/bin'}
            launch = envelope.prepare_launch(boundary, state_root, 'worker', False,
                                             isolation.codex_arguments(False), environment)
            self.assertEqual(environment['CODEX_HOME'], str(home))
            scoped_home = Path(launch.environment['CODEX_HOME'])
            self.assertTrue((scoped_home / 'auth.json').is_symlink())
            self.assertEqual((scoped_home / 'auth.json').resolve(), home / 'auth.json')
            policy = json.loads(launch.specification)
            self.assertEqual(set(policy['writable']),
                             {str(scoped_home), str(launch.attempt), str(boundary.worktree)})
            self.assertIn(str(home), policy['protected'])
            self.assertIn(str(boundary.common_dir), policy['protected'])
            self.assertIn(str(boundary.other_worktrees[0]), policy['protected'])
            self.assertIn(str(boundary.worktree / '.git'), policy['protected'])
            self.assertIn('sandbox_workspace_write.network_access=true', launch.prefix)
            self.assertIn('sandbox_workspace_write.network_access=false', launch.arguments)
            second = envelope.prepare_launch(boundary, state_root, 'worker', False,
                                              launch.arguments, environment)
            self.assertEqual(launch.runtime, second.runtime)
            self.assertNotEqual(launch.attempt, second.attempt)
            reviewer = envelope.prepare_launch(boundary, state_root, 'reviewer', True,
                                                isolation.codex_arguments(True), environment)
            self.assertNotEqual(launch.runtime, reviewer.runtime)
            self.assertNotIn(str(boundary.worktree), json.loads(reviewer.specification)['writable'])
            self.assertIn(str(boundary.worktree), json.loads(reviewer.specification)['protected'])

    def test_runtime_alias_and_replaced_auth_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary, state_root, home = self.boundary(root)
            environment = {'CODEX_HOME': str(home)}
            launch = envelope.prepare_launch(boundary, state_root, 'worker', False, (), environment)
            auth = Path(launch.environment['CODEX_HOME']) / 'auth.json'
            auth.unlink()
            auth.write_text('replaced')
            with self.assertRaisesRegex(RuntimeError, '읽기 전용 연결'):
                envelope.prepare_launch(boundary, state_root, 'worker', False, (), environment)
            alias = root / 'alias'
            alias.symlink_to(launch.runtime, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, '소유권'):
                envelope._owned_directory(alias)

    def test_runtime_overlap_is_rejected_before_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, _state_root, home = self.boundary(Path(directory))
            before = set(boundary.source.iterdir())
            with self.assertRaisesRegex(RuntimeError, '보호 경로가 겹칩니다'):
                envelope.prepare_launch(boundary, boundary.source, 'worker', False, (),
                                        {'CODEX_HOME': str(home)})
            self.assertEqual(set(boundary.source.iterdir()), before)
            self.assertFalse((boundary.source / 'agent-runtime').exists())

    def test_failed_proof_and_unknown_probe_output_do_not_verify(self):
        launch = envelope.CodexLaunch((), ('codex', 'sandbox'), {}, Path('/runtime'),
                                     Path('/attempt'), '{}')
        for code, output in ((1, ''), (0, '{}'), (0, 'agent-write-boundary-v1 extra')):
            with self.subTest(code=code, output=output), \
                 mock.patch.object(envelope.subprocess, 'run', return_value=
                                   manager.subprocess.CompletedProcess([], code, output, '')):
                with self.assertRaisesRegex(RuntimeError, '확인할 수 없습니다'):
                    launch.probe()

    def test_mount_proof_rejects_writable_root_submount_and_git_pointer(self):
        writable = [Path('/work'), Path('/runtime')]
        protected = [Path('/source'), Path('/common'), Path('/work/.git')]
        mounts = [(Path('/'), True, 'ext4'), (Path('/work'), False, 'ext4'),
                  (Path('/runtime'), False, 'ext4'), (Path('/work/.git'), True, 'ext4')]
        gate.check_mounts(mounts, writable, protected)
        for invalid in (
            [(Path('/'), False, 'ext4'), *mounts[1:]],
            [*mounts, (Path('/source/subdir'), False, 'ext4')],
            [*mounts[:-1], (Path('/work/.git'), False, 'ext4')],
            [*mounts, (Path('/tmp'), False, 'tmpfs')],
            [*mounts, (Path('/unexpected'), False, 'ext4')],
        ):
            with self.subTest(mounts=invalid), self.assertRaises(gate.BoundaryError):
                gate.check_mounts(invalid, writable, protected)

    def test_mount_parser_preserves_escaped_paths_and_per_mount_readonly(self):
        mounts = gate.mount_table(
            '1 0 8:1 / / ro,relatime - ext4 /dev/root rw\n'
            '2 1 8:1 /work /with\\040space rw,relatime - ext4 /dev/root rw\n')
        self.assertEqual(mounts, [(Path('/'), True, 'ext4'),
                                  (Path('/with space'), False, 'ext4')])

    def test_same_gate_must_pass_before_provider_exec(self):
        specification = json.dumps({'worktree': '/selected'})
        with mock.patch.object(gate.sys, 'argv', ['gate', specification, 'codex', 'exec']), \
             mock.patch.object(gate, 'check_boundary') as check, \
             mock.patch.object(gate.os, 'chdir') as chdir, \
             mock.patch.object(gate.os, 'execvpe') as execute:
            gate.main()
            check.assert_called_once_with({'worktree': '/selected'})
            chdir.assert_called_once_with('/selected')
            execute.assert_called_once_with('codex', ['codex', 'exec'], gate.os.environ)
            execute.reset_mock()
            check.side_effect = gate.BoundaryError('unproven')
            with mock.patch.object(gate.sys, 'stderr', io.StringIO()):
                self.assertEqual(gate.main(), 125)
            execute.assert_not_called()

    def test_source_readonly_failure_never_attempts_mutation(self):
        specification = {'writable': [], 'protected': ['/source'], 'files': ['/source/file']}
        with mock.patch.object(gate.Path, 'read_text', side_effect=[
                '1 0 8:1 / / ro - ext4 root rw\n',
                'CapEff:\t00000000\nNoNewPrivs:\t1\n']), \
             mock.patch.object(gate.os, 'readlink', return_value='mnt:[1]'), \
             mock.patch.object(gate.Path, 'exists', return_value=True), \
             mock.patch.object(gate.os, 'statvfs', return_value=mock.Mock(f_flag=0)), \
             mock.patch.object(gate.os, 'open') as write_open, \
             mock.patch.object(gate.tempfile, 'TemporaryDirectory') as scratch:
            with self.assertRaisesRegex(gate.BoundaryError, 'not read-only'):
                gate.check_boundary(specification)
            write_open.assert_not_called()
            scratch.assert_not_called()

    def test_writable_probe_creates_updates_renames_and_removes_only_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            selected = root / 'selected'
            source.mkdir()
            selected.mkdir()
            preserved = source / 'local-only'
            preserved.write_bytes(b'unchanged')
            specification = {'writable': [str(selected)], 'protected': [str(source)], 'files': []}
            with mock.patch.object(gate.Path, 'read_text', side_effect=[
                    '1 0 8:1 / / ro - ext4 root rw\n'
                    f'2 1 8:1 / {selected} rw - ext4 root rw\n',
                    'CapEff:\t0\nNoNewPrivs:\t1\n']), \
                 mock.patch.object(gate.os, 'readlink', return_value='mnt:[1]'), \
                 mock.patch.object(gate.os, 'statvfs', return_value=mock.Mock(f_flag=gate.os.ST_RDONLY)), \
                 mock.patch.object(gate.os, 'walk', side_effect=PermissionError('tree inspection unavailable')) as walk:
                gate.check_boundary(specification)
            walk.assert_not_called()
            self.assertEqual(list(selected.iterdir()), [])
            self.assertEqual(preserved.read_bytes(), b'unchanged')
            self.assertEqual(list(source.iterdir()), [preserved])

    def test_actual_adapter_wraps_roles_and_resume_and_persists_evidence(self):
        for role, read_only, session in (('planner', False, None), ('worker', False, 'saved'),
                                        ('reviewer', True, None), ('maintenance', False, None)):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                boundary, state_root, home = self.boundary(Path(directory))
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                state.ensure_schema(connection)
                commands = []

                def run(command, **kwargs):
                    if command == ['codex', '--version']:
                        return manager.subprocess.CompletedProcess(command, 0, 'version', '')
                    self.assertEqual(command[:2], ('codex', 'sandbox') if isinstance(command, tuple)
                                     else ['codex', 'sandbox'])
                    commands.append(command)
                    if 'exec' not in command:
                        return manager.subprocess.CompletedProcess(command, 0, 'agent-write-boundary-v1\n', '')
                    self.assertIn('approvals_reviewer="auto_review"', command)
                    self.assertIn('sandbox_workspace_write.network_access=false', command)
                    if session:
                        self.assertIn('resume', command)
                        self.assertIn(session, command)
                    target = Path(command[command.index('-o') + 1])
                    target.write_text('completed')
                    kwargs['stdout'].write('provider log\n')
                    return manager.subprocess.CompletedProcess(command, 0)

                with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                     mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                     mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                     mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                       mock.Mock(serialize=lambda: 'snapshot')), \
                     mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                     mock.patch.object(envelope.CodexLaunch, 'run', side_effect=run):
                    result = manager.run_agent(
                        'codex', boundary.worktree, state_root, state_root / 'config', role, 'prompt',
                        read_only=read_only, session_id=session, connection=connection,
                        resolved_profile=invocation.ResolvedProfile('model', 'high'),
                        invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
                self.assertEqual(result.code, 0)
                self.assertEqual(result.message, 'completed')
                self.assertEqual(len(commands), 2)
                self.assertEqual(list(commands[0]), list(commands[1][:len(commands[0])]))
                row = connection.execute('''SELECT proof.status, call.result_kind, call.session_id
                    FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
                self.assertEqual(row, ('verified', 'success', session))

    def test_handoff_after_preflight_blocks_native_spawn_and_invalidates_evidence(self):
        cases = (('reviewer', True, False, None),
                 ('worker', False, True, 'saved-session'),
                 ('worker', False, True, None))
        for role, read_only, persist_session, session in cases:
            with self.subTest(role=role, session=session), tempfile.TemporaryDirectory() as directory:
                boundary, state_root, home = self.boundary(Path(directory))
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                state.ensure_schema(connection)
                logs = state_root / 'logs' / manager.hashlib.sha256(
                    str(boundary.worktree).encode()).hexdigest()[:16]
                logs.mkdir(parents=True)
                (logs / f'{role}.log').write_text('old log')
                (logs / f'{role}-last.txt').write_text('old result')
                handoff = manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
                token = manager._runtime_handoff_signal.set(handoff)
                try:
                    with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                         mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                         mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                         mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                           mock.Mock(serialize=lambda: 'snapshot')), \
                         mock.patch.object(isolation.subprocess, 'run', return_value=
                                           manager.subprocess.CompletedProcess([], 0, 'version', '')), \
                         mock.patch.object(envelope.CodexLaunch, 'probe', side_effect=handoff.stop) as probe, \
                         mock.patch.object(manager.subprocess, 'Popen') as spawn, \
                         mock.patch.object(envelope.os, 'killpg') as terminate:
                        with self.assertRaises(manager.SelfMergeRuntimeHandoff):
                            manager.run_agent(
                                'codex', boundary.worktree, state_root, state_root / 'config',
                                role, 'prompt', read_only=read_only, persist_session=persist_session,
                                session_id=session, connection=connection,
                                resolved_profile=invocation.ResolvedProfile('model', 'high'),
                                invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
                        probe.assert_called_once()
                        spawn.assert_not_called()
                        terminate.assert_not_called()
                finally:
                    manager._runtime_handoff_signal.reset(token)
                proof = connection.execute('''SELECT proof.status, call.session_id, call.finished_at
                    FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
                self.assertEqual(proof[:2], ('invalidated', session))
                self.assertIsNotNone(proof[2])
                self.assertEqual((logs / f'{role}.log').read_text(), 'old log')
                self.assertEqual((logs / f'{role}-last.txt').read_text(), 'old result')

    def test_native_spawn_holds_handoff_lock_only_until_process_starts(self):
        for role, read_only, session in (('reviewer', True, None),
                                        ('worker', False, 'saved-session')):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                boundary, state_root, home = self.boundary(Path(directory))
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                state.ensure_schema(connection)
                handoff = manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
                process = mock.Mock(pid=4321, returncode=0)

                def start(command, **kwargs):
                    self.assertTrue(handoff.launch_lock.locked())
                    self.assertEqual(command[:2], ['codex', 'sandbox'])
                    self.assertTrue(kwargs['start_new_session'])
                    if session:
                        self.assertIn('resume', command)
                        self.assertIn(session, command)

                    def communicate(prompt, *, timeout):
                        self.assertEqual(prompt, 'prompt')
                        self.assertEqual(timeout, manager.AGENT_CLI_TIMEOUT_SECONDS)
                        self.assertTrue(handoff.launch_lock.acquire(blocking=False))
                        handoff.launch_lock.release()
                        handoff.stop()
                        Path(command[command.index('-o') + 1]).write_text('completed')
                        kwargs['stdout'].write('provider log\n')

                    process.communicate.side_effect = communicate
                    return process

                token = manager._runtime_handoff_signal.set(handoff)
                try:
                    with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                         mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                         mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                         mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                           mock.Mock(serialize=lambda: 'snapshot')), \
                         mock.patch.object(isolation.subprocess, 'run', return_value=
                                           manager.subprocess.CompletedProcess([], 0, 'version', '')), \
                         mock.patch.object(envelope.CodexLaunch, 'probe'), \
                         mock.patch.object(manager.subprocess, 'Popen', side_effect=start) as spawn, \
                         mock.patch.object(envelope.os, 'killpg') as terminate:
                        result = manager.run_agent(
                            'codex', boundary.worktree, state_root, state_root / 'config', role, 'prompt',
                            read_only=read_only, persist_session=session is not None, session_id=session,
                            connection=connection,
                            resolved_profile=invocation.ResolvedProfile('model', 'high'),
                            invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
                        spawn.assert_called_once()
                        terminate.assert_called_once_with(4321, envelope.signal.SIGKILL)
                finally:
                    manager._runtime_handoff_signal.reset(token)
                process.communicate.assert_called_once()
                process.wait.assert_called_once()
                self.assertEqual((result.code, result.message, result.session_id), (0, 'completed', session))
                proof = connection.execute('''SELECT proof.status, call.result_kind, call.session_id
                    FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
                self.assertEqual(proof, ('verified', 'success', session))

    def test_envelope_timeout_kills_process_group_and_waits(self):
        launch = envelope.CodexLaunch((), ('native',), {}, Path('/runtime'), Path('/attempt'), '{}')
        process = mock.Mock(pid=4321)
        process.communicate.side_effect = manager.subprocess.TimeoutExpired(['native'], 1)
        with mock.patch.object(envelope.subprocess, 'Popen', return_value=process) as start, \
             mock.patch.object(envelope.os, 'killpg') as terminate:
            with self.assertRaises(manager.subprocess.TimeoutExpired):
                launch.run(['native'], launch_process=manager._handoff_popen,
                           input='prompt', timeout=1, text=True)
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        terminate.assert_called_once_with(4321, envelope.signal.SIGKILL)
        process.wait.assert_called_once()

    def test_streamed_new_session_uses_envelope_and_persists_session(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home = self.boundary(Path(directory))
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            state.ensure_schema(connection)
            process = mock.Mock(pid=4321)
            process.stdin = io.StringIO()
            process.stdout = io.StringIO('{"type":"thread.started","thread_id":"new-session"}\n')
            process.wait.return_value = 0
            commands = []

            def run(command, **kwargs):
                if command == ['codex', '--version']:
                    return manager.subprocess.CompletedProcess(command, 0, 'version', '')
                return manager.subprocess.CompletedProcess(command, 0, 'agent-write-boundary-v1\n', '')

            def start(command, **kwargs):
                commands.append(command)
                self.assertTrue(kwargs['start_new_session'])
                Path(command[command.index('-o') + 1]).write_text('streamed result')
                return process

            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                   mock.Mock(serialize=lambda: 'snapshot')), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                 mock.patch.object(manager.subprocess, 'Popen', side_effect=start), \
                 mock.patch.object(envelope.os, 'killpg') as terminate:
                result = manager.run_agent(
                    'codex', boundary.worktree, state_root, state_root / 'config', 'worker', 'prompt',
                    read_only=False, persist_session=True, connection=connection,
                    resolved_profile=invocation.ResolvedProfile('model', 'high'),
                    invocation=invocation.InvocationContext(str(boundary.source), 'G-088'))
            self.assertEqual(commands[0][:2], ['codex', 'sandbox'])
            self.assertEqual((result.code, result.session_id), (0, 'new-session'))
            self.assertEqual(connection.execute('SELECT session_id FROM agent_invocations').fetchone()[0],
                             'new-session')
            terminate.assert_called_once_with(4321, envelope.signal.SIGKILL)

    def test_actual_gate_failure_invalidates_evidence_and_preserves_old_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home = self.boundary(Path(directory))
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            state.ensure_schema(connection)
            logs = state_root / 'logs' / manager.hashlib.sha256(str(boundary.worktree).encode()).hexdigest()[:16]
            logs.mkdir(parents=True)
            (logs / 'planner.log').write_text('old log')
            (logs / 'planner-last.txt').write_text('old result')

            def run(command, **kwargs):
                if command == ['codex', '--version']:
                    return manager.subprocess.CompletedProcess(command, 0, 'version', '')
                if 'exec' not in command:
                    return manager.subprocess.CompletedProcess(command, 0, 'agent-write-boundary-v1\n', '')
                kwargs['stdout'].write('agent write boundary verification failed')
                return manager.subprocess.CompletedProcess(command, 125)

            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(manager, 'capture_git_snapshot', return_value=
                                   mock.Mock(serialize=lambda: 'snapshot')), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                     mock.patch.object(envelope.CodexLaunch, 'run', side_effect=run):
                result = manager.run_agent(
                    'codex', boundary.worktree, state_root, state_root / 'config', 'planner', 'prompt',
                    read_only=False, connection=connection,
                    resolved_profile=invocation.ResolvedProfile('model', 'high'),
                    invocation=invocation.InvocationContext(str(boundary.source), None))
            self.assertEqual(result.code, 125)
            self.assertEqual(connection.execute('SELECT status FROM agent_isolation_evidence').fetchone()[0],
                             'invalidated')
            self.assertEqual((logs / 'planner.log').read_text(), 'old log')
            self.assertEqual((logs / 'planner-last.txt').read_text(), 'old result')


@skipUnless(isolation.platform.system() == 'Linux', 'native runtime envelope requires Linux')
class RuntimeReadOnlyIsolationTests(TestCase):
    def fixture(self, root):
        boundary, state_root, home = NativeWriteIsolationTests.boundary(self, root)
        head = 'a' * 40
        runtime = manager.expected_runtime_worktree(state_root, boundary.source, head)
        runtime.parent.mkdir(parents=True)
        boundary.worktree.rename(runtime)
        observed = {'selected': runtime, 'head': head, 'branch': 'HEAD', 'dirty': '',
                    'registered': True}

        def git(path, *args):
            if args == ('rev-parse', '--show-toplevel'):
                return str(observed['selected'])
            if args == ('rev-parse', '--path-format=absolute', '--git-common-dir'):
                return str(boundary.common_dir)
            if args == ('rev-parse', '--path-format=absolute', '--absolute-git-dir'):
                return str(boundary.git_dir)
            if args == ('worktree', 'list', '--porcelain'):
                selected = (f"worktree {observed['selected']}\nHEAD {observed['head']}\ndetached\n"
                            if observed['registered'] else '')
                return f'worktree {boundary.source}\nHEAD base\n\n' + selected
            if args == ('rev-parse', 'HEAD'):
                return observed['head']
            if args == ('rev-parse', '--abbrev-ref', 'HEAD'):
                return observed['branch']
            if args == ('status', '--porcelain=v1', '--untracked-files=all'):
                return observed['dirty']
            if args == ('remote', 'get-url', 'origin'):
                return 'https://example.invalid/owner/repo.git'
            raise AssertionError(args)

        return boundary, state_root, home, observed, git

    def test_runtime_is_allowed_only_for_read_only_and_remains_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home, observed, git = self.fixture(Path(directory))
            runtime = observed['selected']
            with mock.patch.object(isolation, '_git', side_effect=git):
                inspected = isolation.inspect_worktree(runtime, state_root, read_only=True)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(runtime, state_root, read_only=False)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(boundary.source, state_root, read_only=True)
                alias = runtime.parent / 'alias'
                alias.symlink_to(runtime, target_is_directory=True)
                with self.assertRaises(isolation.IsolationError):
                    isolation.inspect_worktree(alias, state_root, read_only=True)
            launch = envelope.prepare_launch(inspected, state_root, 'planner', True,
                                             isolation.codex_arguments(True), {'CODEX_HOME': str(home)})
            specification = json.loads(launch.specification)
            self.assertIn(str(runtime), specification['protected'])
            self.assertNotIn(str(runtime), specification['writable'])
            self.assertEqual(inspected.source, boundary.source)

    def test_runtime_rejects_changed_head_attached_dirty_and_unregistered_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            _boundary, state_root, _home, observed, git = self.fixture(Path(directory))
            runtime = observed['selected']
            with mock.patch.object(isolation, '_git', side_effect=git):
                for changes in ({'head': 'b' * 40}, {'branch': 'main'},
                                {'dirty': ' M code.py'}, {'registered': False}):
                    with self.subTest(changes=changes), mock.patch.dict(observed, changes):
                        with self.assertRaises(isolation.IsolationError):
                            isolation.inspect_worktree(runtime, state_root, read_only=True)
                invalid = state_root / 'runtimes' / 'wrong-repository' / observed['head']
                invalid.parent.mkdir()
                runtime.rename(invalid)
                observed['selected'] = invalid
                with self.assertRaisesRegex(isolation.IsolationError, 'exact SHA'):
                    isolation.inspect_worktree(invalid, state_root, read_only=True)

    def test_document_generate_reaches_isolated_agent_from_runtime_root(self):
        import document_drafts

        with tempfile.TemporaryDirectory() as directory:
            boundary, state_root, home, observed, git = self.fixture(Path(directory))
            runtime = observed['selected']
            connection = sqlite3.connect(':memory:')
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            state.ensure_schema(connection)
            connection.execute('CREATE TABLE projects (repo TEXT, base_branch TEXT, provider TEXT, registered INT)')
            connection.execute('INSERT INTO projects VALUES (?, ?, ?, 1)',
                               (str(boundary.source), 'main', 'codex'))
            connection.commit()
            drafts = mock.Mock()
            drafts.generation_prompt.return_value = ('draft prompt', 'document identity')
            launched = []

            def run(command, **kwargs):
                if command == ['codex', '--version']:
                    return manager.subprocess.CompletedProcess(command, 0, 'version', '')
                return manager.subprocess.CompletedProcess(command, 0, 'agent-write-boundary-v1\n', '')

            def execute(command, **kwargs):
                launched.append(command)
                helper = str(Path(envelope.__file__).with_name('agent_sandbox_exec.py'))
                specification = json.loads(command[command.index(helper) + 1])
                self.assertEqual(specification['worktree'], str(runtime))
                self.assertIn(str(runtime), specification['protected'])
                self.assertNotIn(str(runtime), specification['writable'])
                self.assertIn('--sandbox', command)
                self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
                Path(command[command.index('-o') + 1]).write_text('draft response')
                return manager.subprocess.CompletedProcess(command, 0)

            args = manager.argparse.Namespace(repository=str(boundary.source),
                                               draft_command='generate', target='docs/PRODUCT.md')
            with mock.patch.object(manager, 'ROOT', runtime), \
                 mock.patch.object(manager, 'is_loop_manager_repository', return_value=True), \
                 mock.patch.object(manager, 'connect_database', return_value=connection), \
                 mock.patch.object(manager, '_recorded_base_fetch'), \
                 mock.patch.object(document_drafts, 'DocumentDraftRepository', return_value=drafts), \
                 mock.patch.object(document_drafts, 'parse_generated_draft', return_value=None) as parse, \
                 mock.patch.object(manager, 'operation_print'), \
                 mock.patch.object(manager, '_agent_profile', return_value=invocation.ResolvedProfile('model', 'high')), \
                 mock.patch.object(manager, 'capture_git_snapshot', return_value=mock.Mock(serialize=lambda: 'snapshot')), \
                 mock.patch.object(isolation, '_git', side_effect=git), \
                 mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(home)}), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=run), \
                 mock.patch.object(envelope.CodexLaunch, 'run', side_effect=execute):
                result = manager._document_drafts_command(args, state_root, state_root / 'config')
            self.assertEqual(result, 0)
            self.assertEqual(len(launched), 1)
            drafts.generation_prompt.assert_called_once_with(str(boundary.source), boundary.source,
                                                             'main', 'docs/PRODUCT.md')
            parse.assert_called_once_with('draft response')
            row = connection.execute('''SELECT proof.status, proof.canonical_worktree, call.result_kind
                FROM agent_isolation_evidence proof JOIN agent_invocations call USING(invocation_id)''').fetchone()
            self.assertEqual(tuple(row), ('verified', str(runtime), 'success'))


class UsageTests(TestCase):
    def test_codex_final_usage_preserves_nullable_categories(self):
        usage = provider_usage.codex_log_usage(
            '{"type":"turn.completed","usage":{"input_tokens":100,'
            '"cached_input_tokens":60,"output_tokens":15}}\n'
        )
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.cache_write_input_tokens, usage.output_tokens,
                          usage.reasoning_output_tokens, usage.status),
                         (100, 60, None, 15, None, "known"))
        self.assertEqual(provider_usage.codex_log_usage("not json").status, "invalid")

    def test_invalid_counters_do_not_replace_other_provider_values(self):
        usage = provider_usage.claude_usage({"type": "result", "usage": {
            "input_tokens": 10, "cache_read_input_tokens": -1,
            "cache_creation_input_tokens": "5", "output_tokens": 3}})
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.cache_write_input_tokens, usage.output_tokens,
                          usage.reasoning_output_tokens, usage.status),
                         (10, None, None, 3, None, "invalid"))
        self.assertEqual(provider_usage.claude_usage({"type": "result"}).status, "unknown")

    def test_codex_nested_cached_and_reasoning_counters_are_independent(self):
        usage = provider_usage.codex_usage({"type": "turn.completed", "usage": {
            "input_tokens": 50, "output_tokens": 8,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 4}}})
        self.assertEqual((usage.cached_input_tokens, usage.reasoning_output_tokens),
                         (30, 4))

    def test_codex_inconsistent_breakdowns_are_invalid_without_losing_totals(self):
        usage = provider_usage.codex_usage({"type": "turn.completed", "usage": {
            "input_tokens": 5, "cached_input_tokens": 6,
            "output_tokens": 2, "reasoning_output_tokens": 3}})
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens, usage.reasoning_output_tokens,
                          usage.status), (5, None, 2, None, "invalid"))

    def test_claude_complete_usage_keeps_cache_reads_and_writes_separate(self):
        usage = provider_usage.claude_usage({"type": "result", "usage": {
            "input_tokens": 20, "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 5, "output_tokens": 4}})
        self.assertEqual((usage.input_tokens, usage.cached_input_tokens,
                          usage.cache_write_input_tokens, usage.output_tokens,
                          usage.reasoning_output_tokens, usage.status),
                         (20, 40, 5, 4, None, "known"))

    def test_claude_reported_thinking_breakdown_is_preserved(self):
        usage = provider_usage.claude_usage({"type": "result", "usage": {
            "output_tokens": 8, "output_tokens_details": {"thinking_tokens": 3}}})
        self.assertEqual((usage.output_tokens, usage.reasoning_output_tokens,
                          usage.status), (8, 3, "known"))

    def test_claude_malformed_json_keeps_agent_failure_and_invalid_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(manager.subprocess, "run", return_value=mock.Mock(
                returncode=1, stdout="{not-json")):
                result = manager.run_claude(
                    root, root, root / "config", "reviewer", "prompt",
                    read_only=True, persist_session=True, session_id="session",
                    resolved_profile=invocation.ResolvedProfile("model", "high"))
        self.assertEqual(result.code, 1)
        self.assertEqual(result.usage.status, "invalid")

    def test_codex_scalar_json_event_does_not_block_session_or_usage(self):
        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(
                    '42\n{"type":"thread.started","thread_id":"session"}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":10,'
                    '"cached_input_tokens":4,"output_tokens":2}}\n'
                )

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(manager.subprocess, "Popen", return_value=Process()):
                result = manager.run_codex(
                    root, root, root / "config", "planner", "prompt",
                    read_only=False, persist_session=True,
                    resolved_profile=invocation.ResolvedProfile("model", "high"))
        self.assertEqual((result.code, result.session_id,
                          result.usage.cached_input_tokens), (0, "session", 4))

    def test_codex_usage_log_read_failure_does_not_retry_successful_call(self):
        class Process:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(
                    '{"type":"thread.started","thread_id":"session"}\n'
                )

            def wait(self):
                return 0

        original_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == "planner.log":
                raise OSError("usage log unavailable")
            return original_read_text(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(manager.subprocess, "Popen", side_effect=lambda *a, **k: Process()) as launch, \
                 mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text):
                result = manager.run_agent_with_retries(
                    lambda _session, _fallback: manager.run_codex(
                        root, root, root / "config", "planner", "prompt",
                        read_only=False, persist_session=True,
                        resolved_profile=invocation.ResolvedProfile("model", "high")),
                    session_id=None,
                )
        self.assertEqual((result.code, result.session_id, result.usage.status),
                         (0, "session", "invalid"))
        self.assertEqual(launch.call_count, 1)


class RunnerTests(TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        state.ensure_schema(self.connection)

    def test_execution_agent_port_supplies_ledger_connection(self):
        with mock.patch.object(manager, "run_agent", return_value=manager.AgentResult(0, "done")) as run:
            result = manager._run_agent_with_connection(self.connection, "codex", Path("/worktree"))
        self.assertEqual(result.code, 0)
        self.assertIs(run.call_args.kwargs["connection"], self.connection)

    def test_pre_resolved_worker_profile_is_shared_by_cli_and_ledger(self):
        selected = invocation.ResolvedProfile('actual-model', 'high')
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.profile, selected)
                return manager.AgentResult(0, 'done', 'worker-session')
        self_outer = self
        with mock.patch.object(manager, '_agent_profile') as resolve, \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), Path('/state'), Path('/config'),
                'worker', 'prompt', read_only=False, level=5,
                connection=self.connection,
                invocation=invocation.InvocationContext('repo', 'G-041'),
                resolved_profile=selected,
            )
        self.assertEqual(result.code, 0)
        resolve.assert_not_called()
        row = self.connection.execute('SELECT model, effort FROM agent_invocations').fetchone()
        self.assertEqual(tuple(row), ('actual-model', 'high'))

    def test_each_fallback_process_has_own_resolved_profile_and_session(self):
        seen = []

        class Provider:
            def run(self, request):
                seen.append(request.profile)
                if request.session_id:
                    return manager.AgentResult(1, "session not found", request.session_id)
                request.on_session_started("new-session")
                return manager.AgentResult(0, "done", "new-session",
                                           usage=invocation.Usage(20, 10, None, 2))

        attempts = invocation.AttemptSequence("repo", "G-039", "revision")
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "worker", "secret prompt", read_only=False, level=3,
                persist_session=True, session_id=session, connection=self.connection,
                invocation=context), attempts)
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("real-model", "high")) as resolve_profile, \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent_with_retries(call, session_id="old-session")
        self.assertEqual(result.code, 0)
        self.assertEqual(resolve_profile.call_count, 2)
        self.assertEqual(seen, [invocation.ResolvedProfile("real-model", "high")] * 2)
        rows = self.connection.execute("SELECT * FROM agent_invocations ORDER BY attempt").fetchall()
        self.assertEqual([(row["attempt"], row["transition"], row["transition_cause"],
                           row["session_id"], row["result_kind"])
                          for row in rows],
                         [(1, "resume", None, "old-session", "permanent_configuration"),
                          (2, "fallback", "session_unavailable", "new-session", "success")])
        self.assertEqual(rows[1]["model"], "real-model")
        self.assertEqual(rows[1]["cached_input_tokens"], 10)
        self.assertNotIn("secret prompt", repr(dict(rows[1])))

    def test_planner_worker_reviewer_rows_keep_role_and_plan_identity(self):
        class Provider:
            def run(self, request):
                request.on_session_started(f"{request.role_name}-session")
                return manager.AgentResult(0, "done", f"{request.role_name}-session")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "medium")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            for role, plan in (("planner", None), ("worker", "G-039"),
                               ("reviewer", "G-039")):
                result = manager.run_agent(
                    "codex", Path("/worktree"), Path("/state"), Path("/config"),
                    role, "secret prompt", read_only=role == "reviewer",
                    level=3 if role == "worker" else None,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", plan,
                                                             "revision" if plan else None))
                self.assertEqual(result.code, 0)
        rows = self.connection.execute(
            "SELECT role, plan_id, definition_revision, session_id FROM agent_invocations ORDER BY rowid"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("planner", None, None, "planner-session"),
            ("worker", "G-039", "revision", "worker-session"),
            ("reviewer", "G-039", "revision", "reviewer-session"),
        ])

    def test_ledger_failure_does_not_change_agent_success(self):
        self.connection.execute("DROP TABLE agent_invocations")

        class Provider:
            def run(self, request):
                return manager.AgentResult(0, "done", "session")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual(result.code, 0)
        self.assertEqual(result.collection_status, "error")

    def test_profile_observation_failure_does_not_replace_provider_result(self):
        class Provider:
            def run(self, request):
                if request.profile is not None:
                    raise AssertionError("unresolved profile must stay absent")
                return manager.AgentResult(0, "done", "session")

        with mock.patch.object(manager, "_agent_profile", side_effect=RuntimeError("missing config")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual((result.code, result.collection_status), (0, "error"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM agent_invocations").fetchone()[0], 0)

    def test_transient_retry_and_timeout_have_distinct_rows(self):
        count = 0

        class Provider:
            def run(self, request):
                nonlocal count
                count += 1
                if count == 1:
                    raise manager.subprocess.TimeoutExpired("agent", 1)
                return manager.AgentResult(0, "done", "session")

        attempts = invocation.AttemptSequence("repo", None)
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, session_id=session,
                connection=self.connection, invocation=context), attempts)
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent_with_retries(call, session_id=None)
        self.assertEqual(result.code, 0)
        rows = self.connection.execute("SELECT result_kind, transition_cause FROM agent_invocations ORDER BY attempt").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("timeout", None), ("success", "timeout")])

    def test_replacement_context_marks_first_process_and_retry_cause(self):
        attempts = invocation.AttemptSequence(
            "repo", "G-039", "revision",
            initial_transition="replace", initial_cause="profile_changed")
        first = attempts.next(None, False)
        attempts.previous_failure = "usage_limit"
        retry = attempts.next(None, False)
        self.assertEqual(
            [(item.transition, item.transition_cause, item.attempt)
             for item in (first, retry)],
            [("replace", "profile_changed", 1), ("start", "usage_limit", 2)],
        )

    def test_retry_after_session_fallback_records_actual_transition(self):
        attempts = invocation.AttemptSequence("repo", "G-039")
        first = attempts.next("old-session", False)
        attempts.previous_failure = "permanent_configuration"
        fallback = attempts.next(None, True)
        attempts.previous_failure = "transient_failure"
        retry = attempts.next("new-session", True)
        self.assertEqual(
            [(item.transition, item.transition_cause) for item in (first, fallback, retry)],
            [("resume", None), ("fallback", "session_unavailable"),
             ("resume", "transient_failure")],
        )

    def test_session_observation_storage_error_keeps_agent_result(self):
        class Provider:
            def run(self, request):
                request.on_session_started("observed")
                return manager.AgentResult(0, "done", "observed")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()), \
             mock.patch.object(state.InvocationRepository, "observe_session",
                               side_effect=sqlite3.OperationalError("unavailable")):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual((result.code, result.collection_status), (0, "error"))
        row = self.connection.execute("SELECT session_id, collection_status FROM agent_invocations").fetchone()
        self.assertEqual(tuple(row), ("observed", "error"))

    def test_interrupted_process_preserves_observed_session(self):
        class Provider:
            def run(self, request):
                request.on_session_started("observed")
                raise KeyboardInterrupt()

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            with self.assertRaises(KeyboardInterrupt):
                manager.run_agent(
                    "codex", Path("/worktree"), Path("/state"), Path("/config"),
                    "worker", "secret", read_only=False, level=1,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", "G-039"))
        row = self.connection.execute(
            "SELECT session_id, result_kind, finished_at FROM agent_invocations"
        ).fetchone()
        self.assertEqual((row["session_id"], row["result_kind"]),
                         ("observed", "interrupted"))
        self.assertIsNotNone(row["finished_at"])
