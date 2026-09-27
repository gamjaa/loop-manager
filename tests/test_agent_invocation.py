"""Physical Agent invocation observations and tolerant provider usage parsing."""

import sqlite3
from pathlib import Path
import tempfile
import io
import json
from unittest import TestCase, mock

import agent_invocation as invocation
import agent_write_isolation as isolation
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

    def test_each_codex_attempt_gets_fresh_verified_policy_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_home = root / 'codex-home'
            provider_home.mkdir()
            config = provider_home / 'config.toml'
            config.write_text('model = "first"\n', encoding='utf-8')
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repository')
            records = []
            with mock.patch.dict(isolation.os.environ, {'CODEX_HOME': str(provider_home)}), \
                 mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation, '_codex_probe') as probe, \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation.subprocess, 'run', return_value=
                                   manager.subprocess.CompletedProcess(
                                       ['codex', '--version'], 0, 'codex version\n', '')):
                first_args = isolation.prepare_isolation(
                    'codex', 'worker', root, root, False, records.append,
                    lambda: 'snapshot', expected_repository=str(root / 'source'))
                config.write_text('model = "second"\n', encoding='utf-8')
                second_args = isolation.prepare_isolation(
                    'codex', 'worker', root, root, False, records.append,
                    lambda: 'snapshot', expected_repository=str(root / 'source'))
            self.assertEqual(first_args, second_args)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual([record.status for record in records], ['verified', 'verified'])
            self.assertNotEqual(records[0].policy_digest, records[1].policy_digest)
            self.assertEqual(records[0].repository_identity, 'repository')

    def test_codex_probe_requires_allowed_selected_and_denied_external_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / 'selected'
            source = root / 'source'
            other = root / 'other'
            git_dir = root / 'git-worktree'
            common = root / 'common'
            for path in (selected, source, other, git_dir, common):
                path.mkdir()
            for path in (source / 'file', other / 'file', git_dir / 'HEAD',
                         common / 'HEAD', selected / 'file', selected / '.git'):
                path.write_text('unchanged', encoding='utf-8')
            boundary = isolation.WorktreeBoundary(
                selected, source, (other,), git_dir, common, 'repository')
            observed = []

            def probe(command, **_kwargs):
                paths = json.loads(command[-1])
                observed.append(paths)
                result = {name: 'denied' if name != 'selected' else 'allowed'
                          for name in paths}
                return manager.subprocess.CompletedProcess(
                    command, 0, json.dumps(result), '')

            with mock.patch.object(isolation, '_existing_writable_file',
                                   side_effect=lambda path: path / 'file'), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=probe):
                isolation._codex_probe(boundary, False, ())
            self.assertIn('source', observed[0])
            self.assertIn('other_0', observed[0])
            self.assertIn('shared_git', observed[0])
            self.assertIn('worktree_pointer', observed[0])
            self.assertIn('slash_tmp', observed[0])
            self.assertEqual((source / 'file').read_text(encoding='utf-8'), 'unchanged')

            def readonly(command, **_kwargs):
                paths = json.loads(command[-1])
                observed.append(paths)
                return manager.subprocess.CompletedProcess(
                    command, 0, json.dumps({name: 'denied' for name in paths}), '')

            with mock.patch.object(isolation, '_existing_writable_file',
                                   side_effect=lambda path: path / 'file'), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=readonly):
                isolation._codex_probe(boundary, True, ())
            self.assertEqual(observed[-1]['selected'], str(selected / 'file'))

            def escaped(command, **_kwargs):
                paths = json.loads(command[-1])
                result = {name: 'denied' if name != 'selected' else 'allowed'
                          for name in paths}
                result['shared_git'] = 'allowed'
                return manager.subprocess.CompletedProcess(
                    command, 0, json.dumps(result), '')

            with mock.patch.object(isolation, '_existing_writable_file',
                                   side_effect=lambda path: path / 'file'), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=escaped):
                with self.assertRaisesRegex(isolation.IsolationError, '경계가 일치'):
                    isolation._codex_probe(boundary, False, ())

            (selected / '.git').unlink()
            (selected / '.git').mkdir()
            with mock.patch.object(isolation, '_existing_writable_file',
                                   side_effect=lambda path: path / 'file'), \
                 mock.patch.object(isolation.subprocess, 'run', side_effect=readonly):
                isolation._codex_probe(boundary, True, ())
            self.assertNotIn('worktree_pointer', observed[-1])

    def test_resume_with_uninspectable_session_grants_records_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary = isolation.WorktreeBoundary(root, root / 'source', (),
                                                   root / 'git', root / 'common', 'repo')
            records = []
            with mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                 mock.patch.object(isolation.platform, 'system', return_value='Linux'), \
                 mock.patch.object(isolation, '_codex_probe') as probe:
                with self.assertRaisesRegex(isolation.IsolationError, '재개 session'):
                    isolation.prepare_isolation(
                        'codex', 'worker', root, root, False, records.append,
                        lambda: 'snapshot', expected_repository=str(root / 'source'),
                        session_id='prior-session')
            probe.assert_not_called()
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
                 mock.patch.object(isolation, '_codex_probe') as probe:
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
