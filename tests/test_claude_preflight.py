"""Managed startup sources and CLI diagnostics; no actual Agent invocation."""
import copy
import json
import os
from pathlib import Path
import plistlib
import subprocess
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
        elif arguments[1:] == ['--help']:
            output = '--restricted --setting-sources --settings --permission-mode --permission-prompts --tools --disallowedTools --strict-mcp-config'
        elif arguments[-2:] == ['auth', 'status']:
            self.assertIn('--restricted', arguments)
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

    def test_ignored_personal_project_settings_and_auth_values_do_not_enter_source_identity(self):
        (self.home / 'settings.json').write_text('{"sandbox":{"enabled":false}}')
        project = self.workspace / '.claude'
        project.mkdir()
        (project / 'settings.json').write_text('local secret content')
        _, fingerprint, observations = preflight.configuration(self.environment, None)
        _, changed, _ = preflight.configuration(dict(self.environment, ANTHROPIC_API_KEY='another-secret'), None)
        self.assertEqual(fingerprint, changed)
        self.assertNotIn('not-a-real-key', repr(observations))
        self.assertNotIn('local secret content', repr(observations))
        self.assertFalse(any(str(item.get('path', '')).endswith('/settings.json') for item in observations))

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
            self.assertEqual(launch.environment['TMPDIR'], str(launch.attempt))
            self.assertEqual(launch.environment['CLAUDE_CODE_TMPDIR'], str(launch.attempt))
            self.assertEqual(launch.environment['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'], str(launch.attempt))
            self.assertEqual(launch.attempt.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(launch.reason)['runtime_enforcement'], 'not_observed')
            self.assertEqual(json.loads(launch.reason)['authentication_home'], str(self.home))
            self.assertEqual(json.loads(launch.reason)['auto_mode_support'], 'requires_invocation_init')
            self.assertNotIn('not-a-real-key', launch.specification + launch.reason)
            self.assertNotIn('not-a-real-key', repr(launch))
            self.assertIn('--permission-prompts', launch.arguments)
        self.assertEqual(native.call_count, 8)

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
                if failure == 'missing-options' and arguments[1:] == ['--help']:
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
