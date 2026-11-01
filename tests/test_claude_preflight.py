"""Managed startup sources and CLI diagnostics; no actual Agent invocation."""
import copy
import json
import os
from pathlib import Path
import plistlib
import subprocess
import shutil
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.agents import agent_claude_preflight as preflight
from loop_manager_internal.agents.agent_write_isolation import IsolationError, WorktreeBoundary


class ManagedSourceResolutionTests(unittest.TestCase):
    def test_first_wins_ignores_shadowed_scalars_but_not_cross_source_settings(self):
        remote = {'model': 'claude-opus-5-5'}
        files = {'sandbox': {'enabled': False}}
        self.assertEqual(preflight.resolve_managed(remote, {}, files), remote)
        for lower in ({'env': {'CLAUDE_CODE_TMPDIR': '/outside'}},
                {'sandbox': {'filesystem': {'disabled': True}}},
                {'sandbox': {'filesystem': {'disabled': 0}}},
                {'sandbox': {'bwrapPath': '/external/wrapper'}},
                {'sandbox': {'filesystem': {'allowManagedReadPathsOnly': True}}}):
            with self.subTest(lower=lower), self.assertRaises(IsolationError):
                preflight.resolve_managed(remote, {}, lower)

    def test_merge_combines_lists_without_mutating_inputs_and_keeps_highest_scalars(self):
        remote = {'managedSourcesBehavior': 'merge', 'sandbox': {'enabled': True},
                  'permissions': {'deny': ['Read(//private/**)']}}
        files = {'sandbox': {'enabled': False}, 'permissions': {
            'defaultMode': 'acceptEdits', 'deny': ['Edit(//source/**)', 'Read(//private/**)']}}
        original = copy.deepcopy((remote, files))
        merged = preflight.resolve_managed(remote, {}, files)
        self.assertIs(merged['sandbox']['enabled'], True)
        self.assertNotIn('defaultMode', merged['permissions'])
        self.assertCountEqual(merged['permissions']['deny'], ['Read(//private/**)', 'Edit(//source/**)'])
        self.assertEqual((remote, files), original)

    def test_control_keys_do_not_shadow_a_real_policy_and_mdm_precedes_files(self):
        self.assertEqual(preflight.resolve_managed({'managedSourcesBehavior': 'merge'},
            {'model': 'mdm'}, {'model': 'file'}), {'model': 'mdm'})

    def test_control_only_merge_includes_lower_unsafe_sandbox_policy(self):
        remote = {'managedSourcesBehavior': 'merge'}
        mdm = {'model': 'mdm'}
        files = {'sandbox': {'enabled': False}}
        effective = preflight.resolve_managed(remote, mdm, files)
        self.assertIs(effective['sandbox']['enabled'], False)
        from loop_manager_internal.agents.agent_claude_policy import policy_arguments
        with mock.patch.object(preflight.platform, 'system', return_value='Linux'), self.assertRaises(IsolationError):
            policy_arguments(mock.Mock(), Path('/owned'), False, managed_settings=effective)
        # A higher policy document with no control retains first-wins.
        self.assertEqual(preflight.resolve_managed({'model': 'remote'}, remote, files), {'model': 'remote'})

    def test_malformed_nested_sources_raise_policy_errors(self):
        for source in ([], {'permissions': []}, {'sandbox': []},
                       {'autoMode': []}, {'env': []}, {'unknownSetting': True},
                       {'sandbox': {'network': []}}, {'sandbox': {'filesystem': []}},
                       {'managedSourcesBehavior': 'unknown', 'model': 'model'}):
            with self.subTest(source=source), self.assertRaises(IsolationError):
                preflight.resolve_managed(source, {}, {})


@unittest.skipIf(os.name == 'nt', 'POSIX Claude startup sources and owned directories')
class ClaudePreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.short_temp_requests = []
        make_directory = tempfile.mkdtemp
        def owned_temp(*args, **kwargs):
            if kwargs.get('prefix') == 'lm-':
                self.short_temp_requests.append(dict(kwargs))
                kwargs = dict(kwargs, dir=self.root)
            directory = make_directory(*args, **kwargs)
            if kwargs.get('prefix') == 'lm-':
                self.addCleanup(shutil.rmtree, directory)
            return directory
        self.enterContext(mock.patch.object(tempfile, 'mkdtemp', side_effect=owned_temp))
        self.state = self.root / 'state'
        self.state.mkdir()
        self.system = self.root / 'system'
        self.system.mkdir()
        self.home = self.root / 'claude-home'
        self.home.mkdir(mode=0o700)
        self.workspace = self.root / 'worktrees/G-191'
        self.workspace.mkdir(parents=True)
        self.boundary = WorktreeBoundary(self.workspace, self.root / 'source',
            (self.root / 'other',), self.root / 'source/.git/worktrees/G-191',
            self.root / 'source/.git', 'repo-identity')
        self.environment = {'HOME': str(self.root), 'CLAUDE_CONFIG_DIR': str(self.home),
                            'ANTHROPIC_API_KEY': 'not-a-real-key'}
        for patcher in (mock.patch.object(preflight, '_system_directory', return_value=self.system),
                mock.patch.object(preflight.platform, 'system', return_value='Linux'),
                mock.patch.object(preflight.platform, 'release', return_value='linux-test')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self, arguments, **kwargs):
        self.assertEqual(kwargs['env']['ANTHROPIC_API_KEY'], 'not-a-real-key')
        if arguments[1:] == ['--version']:
            output = '2.1.307 (Claude Code)'
        elif arguments[-1:] == ['--help']:
            output = '--restricted --setting-sources --settings --permission-mode --permission-prompts --tools --disallowedTools --strict-mcp-config'
        elif arguments[-2:] == ['auth', 'status']:
            self.assertNotIn('--restricted', arguments)
            self.assertEqual(arguments[arguments.index('--setting-sources') + 1], 'user,project,local')
            self.assertIn('--strict-mcp-config', arguments)
            output = json.dumps({'authMethod': 'api_key', 'configDirectory': str(self.home)})
        elif arguments[-1] == 'doctor':
            output = 'No settings errors'
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, output, '')

    def prepare(self, **kwargs):
        return preflight.prepare(self.boundary, self.state, 'worker', False,
                                  self.environment, None, **kwargs)

    def test_sorted_dropins_merge_arrays_and_remote_precedence_is_fingerprinted(self):
        (self.system / 'managed-settings.json').write_text(json.dumps({'model': 'base',
            'permissions': {'deny': ['Read(//base/**)']}}))
        dropins = self.system / 'managed-settings.d'
        dropins.mkdir()
        (dropins / '20-last.json').write_text(json.dumps({'model': 'last',
            'permissions': {'deny': ['Read(//last/**)']}}))
        (dropins / '10-first.json').write_text('{"model":"first"}')
        (dropins / '.ignored.json').write_text('not json')
        (dropins / 'ignored.txt').write_text('not json')
        settings, fingerprint, observations = preflight.configuration(self.environment, None)
        self.assertEqual(settings['model'], 'last')
        self.assertEqual(settings['permissions']['deny'], ['Read(//base/**)', 'Read(//last/**)'])
        self.assertNotIn('not json', repr(observations))
        (self.home / 'remote-settings.json').write_text('{"model":"remote"}')
        settings, changed, _ = preflight.configuration(self.environment, None)
        self.assertEqual(settings, {'model': 'remote'})
        self.assertNotEqual(fingerprint, changed)

    def test_personal_settings_and_auth_values_do_not_enter_source_identity(self):
        personal = self.root / '.claude'
        personal.mkdir()
        path = personal / 'settings.json'
        path.write_text('{"hooks":{"SessionStart":[{"command":"personal-hook"}]}}')
        _, fingerprint, observations = preflight.configuration(self.environment, None, self.workspace)
        path.write_text('invalid personal settings')
        (self.root / '.claude.json').write_text('invalid personal global settings')
        _, changed, _ = preflight.configuration(
            dict(self.environment, ANTHROPIC_API_KEY='another-secret'), None, self.workspace)
        self.assertEqual(fingerprint, changed)
        self.assertNotIn('not-a-real-key', repr(observations))
        self.assertNotIn(str(path), repr(observations))

    def test_consumed_settings_lifecycle_and_final_drift_preserve_original_files(self):
        project = self.workspace / '.claude'
        project.mkdir()
        self.boundary.source.mkdir()
        (self.boundary.source / '.claude').mkdir()
        # A home/ancestor repository must not replace the linked-worktree root.
        (self.root / '.git').mkdir()
        # Anchor repository discovery inside the fixture, independent of any
        # .git entry and ownership on the machine's temporary ancestors.
        self.boundary.git_dir.mkdir(parents=True)
        (self.boundary.git_dir / 'commondir').write_text('../..')
        (self.workspace / '.git').write_text('gitdir: ' + str(self.boundary.git_dir))
        for path in (self.home / 'settings.json', project / 'settings.json',
                     project / 'settings.local.json',
                     self.boundary.source / '.claude/settings.local.json'):
            with self.subTest(path=path), mock.patch.object(preflight.subprocess, 'run', side_effect=self.native):
                def fingerprint():
                    return preflight.configuration(self.environment, None, self.workspace,
                                                   self.boundary.source)[1]
                absent = self.prepare()
                absent_digest = fingerprint()
                path.write_text('{"model":"created"}')
                with self.assertRaisesRegex(IsolationError, 'source가 변경'):
                    preflight.assert_current(absent, self.boundary, 'worker', False, None)
                self.prepare()
                created = fingerprint()
                replacement = path.with_suffix('.new')
                replacement.write_text('{"model":"replaced"}')
                replacement.replace(path)
                self.prepare()
                replaced = fingerprint()
                path.unlink()
                self.prepare()
                deleted = fingerprint()
                path.write_text('{"model":"recreated"}')
                self.prepare()
                recreated = fingerprint()
                self.assertEqual(len({created, replaced, deleted, recreated}), 4)
                self.assertEqual(path.read_text(), '{"model":"recreated"}')
                self.assertEqual(deleted, absent_digest)
                path.unlink()

    def test_linked_local_root_and_settings_merge_match_discovery_and_managed_calls(self):
        source = self.boundary.source
        git_dir = source / '.git/worktrees/task'
        git_dir.mkdir(parents=True)
        (git_dir / 'commondir').write_text('../..')
        (self.workspace / '.git').write_text('gitdir: ' + str(git_dir))
        (source / '.claude').mkdir()
        (self.workspace / '.claude').mkdir()
        (source / '.claude/settings.local.json').write_text(
            '{"model":"root-local","permissions":{"deny":["Read(//root/**)"]}}')
        (self.workspace / '.claude/settings.json').write_text(
            '{"model":"project","permissions":{"deny":["Read(//project/**)"]}}')
        (self.home / 'settings.json').write_text(
            '{"model":"home","permissions":{"deny":["Read(//home/**)"]}}')
        settings, fingerprint, _ = preflight.configuration(self.environment, None, self.workspace)
        self.assertEqual(settings['model'], 'root-local')
        self.assertEqual(settings['permissions']['deny'],
                         ['Read(//home/**)', 'Read(//project/**)', 'Read(//root/**)'])
        self.assertEqual(fingerprint, preflight.configuration(
            self.environment, None, self.workspace, source)[1])
        # Native local-scope ownership/home exception keeps the cwd file.
        environment = dict(self.environment, HOME=str(self.workspace))
        settings, _, observations = preflight.configuration(environment, None, self.workspace, source)
        self.assertEqual(settings['model'], 'project')
        self.assertNotIn(str(source / '.claude/settings.local.json'), repr(observations))
        # Unresolved Git layout is diagnosed, never treated as absent settings.
        (git_dir / 'commondir').unlink()
        with self.assertRaisesRegex(IsolationError, 'local 설정 root'):
            preflight.configuration(self.environment, None, self.workspace)

    def test_consumed_conflicts_and_source_aliases_are_rejected_before_queries(self):
        path = self.home / 'settings.json'
        for contents in ('{"hooks":{"SessionStart":[]}}', '{"permissions":{"allow":["Edit"]}}',
                         '{"sandbox":{"filesystem":{"disabled":true}}}', 'invalid'):
            path.write_text(contents)
            with self.subTest(contents=contents), mock.patch.object(preflight.subprocess, 'run') as native:
                with self.assertRaises(IsolationError):
                    self.prepare()
                native.assert_not_called()
                self.assertEqual(path.read_text(), contents)
        path.unlink()
        personal = self.root / 'personal-settings.json'
        personal.write_text('{}')
        path.symlink_to(personal)
        with self.assertRaisesRegex(IsolationError, '소유권·경로'):
            self.prepare()

    def test_safe_source_change_during_authentication_is_refused(self):
        def mutate(arguments, **kwargs):
            if arguments[-2:] == ['auth', 'status']:
                (self.home / 'settings.json').write_text('{"model":"changed"}')
            return self.native(arguments, **kwargs)
        with mock.patch.object(preflight.subprocess, 'run', side_effect=mutate) as native:
            with self.assertRaisesRegex(IsolationError, '조회 중'):
                self.prepare()
        self.assertFalse(any(call.args[0][-1] == 'doctor' for call in native.call_args_list))
        self.assertEqual((self.home / 'settings.json').read_text(), '{"model":"changed"}')

    def test_broken_links_bad_json_nonobjects_and_dropin_read_errors_are_not_absence(self):
        path = self.system / 'managed-settings.json'
        for contents in ('not json', '[]', '{"settings":{"sandbox":{"enabled":false}}}'):
            path.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(IsolationError):
                preflight.configuration(self.environment, None)
        path.unlink()
        path.symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(IsolationError, '링크'):
            preflight.configuration(self.environment, None)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(IsolationError, '일반 파일'):
            preflight.configuration(self.environment, None)
        path.rmdir()
        with mock.patch.object(preflight.os, 'scandir', side_effect=PermissionError), self.assertRaises(IsolationError):
            preflight.configuration(self.environment, None)

    def test_each_preparation_owns_a_fresh_temp_and_records_documented_basis_only(self):
        with mock.patch.object(preflight.subprocess, 'run', side_effect=self.native) as native:
            first = self.prepare()
            second = self.prepare()
        self.assertNotEqual(first.attempt, second.attempt)
        for launch in (first, second):
            self.assertEqual(launch.environment['TMPDIR'], str(launch.bash_temporary))
            self.assertEqual(launch.environment['CLAUDE_CODE_TMPDIR'], str(launch.bash_temporary))
            self.assertEqual(launch.environment['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'], str(launch.attempt))
            self.assertEqual(launch.attempt.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(launch.reason)['runtime_enforcement'], 'not_observed')
            self.assertEqual(json.loads(launch.reason)['authentication_home'], str(self.home))
            self.assertEqual(json.loads(launch.reason)['auto_mode_support'], 'requires_invocation_init')
            self.assertNotIn('not-a-real-key', launch.specification + launch.reason)
            self.assertNotIn('not-a-real-key', repr(launch))
            self.assertIn('--permission-prompts', launch.arguments)
        self.assertEqual(native.call_count, 8)

    def test_long_state_path_uses_short_owned_native_bash_temp_without_shared_fallback(self):
        self.state = self.root / ('long-state-' + 'x' * 100)
        self.state.mkdir()
        with mock.patch.object(preflight.subprocess, 'run', side_effect=self.native):
            launch = self.prepare()
        self.assertGreater(len(str(launch.attempt)), 150)
        override = Path(launch.environment['CLAUDE_CODE_TMPDIR'])
        # Native Unix transformation appends claude-UID; a long override would
        # instead choose the shared system fallback denied by our policy.
        def native_bash_tmp(root):
            return ((Path('/tmp').resolve() if len(str(root).encode()) > 50 else root)
                    / f'claude-{os.getuid()}')
        # Filesystem allocation stays in the fixture; native CLI and the short
        # system allocation are mocked. Check the requested production path.
        short = self.short_temp_requests[-1]['dir'] / override.name
        actual = native_bash_tmp(short)
        shared = Path('/tmp').resolve() / f'claude-{os.getuid()}'
        self.assertEqual(actual.parent, short)
        self.assertEqual(native_bash_tmp(launch.attempt), shared)
        settings = json.loads(launch.arguments[launch.arguments.index('--settings') + 1])
        filesystem = settings['sandbox']['filesystem']
        self.assertIn(str(override), filesystem['allowWrite'])
        from loop_manager_internal.agents.agent_claude_policy import policy_arguments
        args = policy_arguments(self.boundary, launch.attempt, False, bash_temporary=short)
        filesystem = json.loads(args[args.index('--settings') + 1])['sandbox']['filesystem']
        self.assertIn(str(actual.parent), filesystem['allowWrite'])
        self.assertIn(str(shared), filesystem['denyWrite'])
        self.assertFalse(any(actual == Path(root) or actual.is_relative_to(Path(root))
                             for root in filesystem['denyWrite']))
        self.assertEqual(override.stat().st_mode & 0o777, 0o700)
        override.rmdir()
        with self.assertRaisesRegex(IsolationError, '다시 확인'):
            preflight.assert_current(launch, self.boundary, 'worker', False, None)
        override.mkdir(mode=0o700)  # cleanup owns this exact test directory

    def test_nonregular_source_closes_its_open_descriptor(self):
        descriptor = os.open(self.system, os.O_RDONLY)
        try:
            with mock.patch.object(preflight.os, 'open', return_value=descriptor), self.assertRaises(IsolationError):
                preflight._read(self.system, [])
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def test_conflicting_managed_policy_is_rejected_before_any_cli_lookup(self):
        (self.system / 'managed-settings.json').write_text('{"sandbox":{"enabled":false}}')
        with mock.patch.object(preflight.subprocess, 'run') as native, self.assertRaises(IsolationError):
            self.prepare()
        native.assert_not_called()

    def test_cli_version_features_and_authentication_failures_are_preserved(self):
        for failure in ('old-version', 'missing-options', 'auth'):
            def native(arguments, **kwargs):
                if failure == 'old-version' and arguments[1:] == ['--version']:
                    return subprocess.CompletedProcess(arguments, 0, '2.1.248 (Claude Code)', '')
                if failure == 'missing-options' and arguments[-1:] == ['--help']:
                    return subprocess.CompletedProcess(arguments, 0, '--settings', '')
                if failure == 'auth' and arguments[-2:] == ['auth', 'status']:
                    return subprocess.CompletedProcess(arguments, 1, '', 'authentication expired')
                return self.native(arguments, **kwargs)
            with self.subTest(failure=failure), mock.patch.object(preflight.subprocess, 'run', side_effect=native), self.assertRaises(IsolationError):
                self.prepare()

    def test_doctor_ignored_settings_and_new_remote_policy_are_rechecked(self):
        for failure in ('ignored-settings', 'new-remote-conflict'):
            def native(arguments, **kwargs):
                if arguments[-1] == 'doctor':
                    if failure == 'ignored-settings':
                        return subprocess.CompletedProcess(arguments, 0, 'Settings Warning: ignored sandbox.enabled', '')
                    (self.home / 'remote-settings.json').write_text('{"disableAutoMode":"disable"}')
                return self.native(arguments, **kwargs)
            with self.subTest(failure=failure), mock.patch.object(preflight.subprocess, 'run', side_effect=native), self.assertRaises(IsolationError):
                self.prepare()

    def test_role_and_runtime_overlap_are_rejected_before_storage_mutation(self):
        with self.assertRaises(IsolationError):
            preflight.prepare(self.boundary, self.state, 'reviewer', False, self.environment, None)
        with self.assertRaises(IsolationError):
            preflight.prepare(self.boundary, self.workspace, 'worker', False, self.environment, None)
        self.assertFalse((self.workspace / 'agent-runtime').exists())
        self.assertFalse((self.state / 'agent-runtime').exists())

    def test_recheck_accepts_other_worktree_changes_but_refuses_settings_or_cli_drift(self):
        with mock.patch.object(preflight.subprocess, 'run', side_effect=self.native):
            launch = self.prepare()
            changed = WorktreeBoundary(self.workspace, self.boundary.source, (self.root / 'new-other',),
                self.boundary.git_dir, self.boundary.common_dir, self.boundary.repository_identity)
            preflight.assert_current(launch, changed, 'worker', False, None)
            (self.home / 'remote-settings.json').write_text('{"sandbox":{"enabled":false}}')
            with self.assertRaisesRegex(IsolationError, 'source가 변경'):
                preflight.assert_current(launch, self.boundary, 'worker', False, None)
            (self.home / 'remote-settings.json').unlink()
        with mock.patch.object(preflight.subprocess, 'run', return_value=
                subprocess.CompletedProcess([], 0, '2.1.308 (Claude Code)', '')):
            with self.assertRaisesRegex(IsolationError, 'CLI 버전'):
                preflight.assert_current(launch, self.boundary, 'worker', False, None)

    def test_recheck_does_not_recreate_deleted_temp_or_accept_a_different_role(self):
        with mock.patch.object(preflight.subprocess, 'run', side_effect=self.native):
            launch = self.prepare()
        with self.assertRaisesRegex(IsolationError, 'identity'):
            preflight.assert_current(launch, self.boundary, 'reviewer', True, None)
        launch.attempt.rmdir()
        with self.assertRaisesRegex(IsolationError, '다시 확인'):
            preflight.assert_current(launch, self.boundary, 'worker', False, None)
        self.assertFalse(launch.attempt.exists())

    def test_auth_status_from_another_home_is_not_accepted(self):
        def native(arguments, **kwargs):
            if arguments[-2:] == ['auth', 'status']:
                return subprocess.CompletedProcess(arguments, 0,
                    '{"authMethod":"api_key","configDirectory":"/different"}', '')
            return self.native(arguments, **kwargs)
        with mock.patch.object(preflight.subprocess, 'run', side_effect=native), self.assertRaisesRegex(IsolationError, '인증 상태'):
            self.prepare()

    def test_mdm_managed_plist_precedence_and_conflicts_are_distinguished_from_absence(self):
        (self.system / 'managed-settings.json').write_text('{"model":"file"}')
        machine, user = self.root / 'machine.plist', self.root / 'user.plist'
        with mock.patch.object(preflight.platform, 'system', return_value='Darwin'), \
                mock.patch.object(preflight, '_mdm_paths', return_value=(machine, user)):
            effective, _, _ = preflight.configuration(self.environment, None)
            self.assertEqual(effective['model'], 'file')
            machine.write_bytes(plistlib.dumps({'model': 'mdm'}))
            effective, _, observations = preflight.configuration(self.environment, None)
            self.assertEqual(effective['model'], 'mdm')
            self.assertTrue(any(row.get('path') == str(machine) for row in observations))
            user.write_bytes(plistlib.dumps({'model': 'other-mdm'}))
            with self.assertRaisesRegex(IsolationError, 'MDM'):
                preflight.configuration(self.environment, None)
            user.write_bytes(plistlib.dumps({'model': 'mdm'}))
            self.assertEqual(preflight.configuration(self.environment, None)[0]['model'], 'mdm')
            user.write_text('not plist')
            with self.assertRaisesRegex(IsolationError, 'source 형식'):
                preflight.configuration(self.environment, None)

    def test_unknown_wsl_host_policy_inheritance_is_diagnosed_before_cli_lookup(self):
        with mock.patch.object(preflight.platform, 'release', return_value='microsoft-WSL2'), \
                mock.patch.object(preflight.subprocess, 'run') as native, \
                self.assertRaisesRegex(IsolationError, 'Windows managed'):
            self.prepare()
        native.assert_not_called()

    def test_success_exit_does_not_hide_a_stderr_policy_warning(self):
        with mock.patch.object(preflight.subprocess, 'run', return_value=
                subprocess.CompletedProcess([], 0, 'ok', 'Settings Warning: ignored sandbox.enabled')), \
                self.assertRaisesRegex(IsolationError, 'stderr'):
            preflight._command(['claude', 'doctor'], self.environment, self.workspace)
