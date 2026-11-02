"""Native configuration contracts, without executing an Agent or write probes."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.agents.agent_claude_policy import policy_arguments
from loop_manager_internal.agents.agent_write_isolation import IsolationError, WorktreeBoundary


@unittest.skipIf(os.name == 'nt', 'Claude native 정책의 POSIX 경로 계약')
class ClaudePolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / 'work tree'
        self.temporary = self.root / 'owned call'
        self.boundary = WorktreeBoundary(self.workspace, self.root / 'source',
            (self.root / 'other',), self.root / 'source/.git/worktrees/G-191',
            self.root / 'source/.git', 'repository')
        platform = mock.patch('loop_manager_internal.agents.agent_claude_policy.platform.system',
                              return_value='Linux')
        platform.start()
        self.addCleanup(platform.stop)

    def settings(self, read_only=False, **kwargs):
        arguments = policy_arguments(self.boundary, self.temporary, read_only, **kwargs)
        return arguments, json.loads(arguments[arguments.index('--settings') + 1])

    def test_file_and_command_policies_protect_git_source_and_other_worktrees(self):
        arguments, settings = self.settings()
        self.assertNotIn('--restricted', arguments)
        self.assertEqual(arguments[arguments.index('--setting-sources') + 1], 'user,project,local')
        self.assertTrue(settings['permissions']['blockReadsOutsideWorkingDirectories'])
        self.assertEqual(settings['permissions']['additionalDirectories'], [str(self.temporary)])
        for path in (self.boundary.source, *self.boundary.other_worktrees,
                     self.boundary.common_dir, self.boundary.git_dir, self.workspace / '.git'):
            with self.subTest(path=path):
                self.assertIn(str(path), settings['sandbox']['filesystem']['denyWrite'])
                self.assertIn(f'Edit(/{path})', settings['permissions']['deny'])
                self.assertIn(f'Edit(/{path}/**)', settings['permissions']['deny'])
        tools = arguments[arguments.index('--tools') + 1].split(',')
        self.assertIn('Bash', tools)
        self.assertFalse({'Edit', 'Write', 'NotebookEdit'} & set(tools))
        self.assertIn('Edit', settings['permissions']['deny'])
        self.assertNotIn('Agent', tools)
        self.assertNotIn(str(self.workspace), settings['sandbox']['filesystem']['denyWrite'])
        self.assertNotIn('allow', settings['permissions'])

    def test_file_writes_outside_workspace_never_reach_the_classifier(self):
        for read_only in (False, True):
            arguments, settings = self.settings(read_only)
            tools = arguments[arguments.index('--tools') + 1].split(',')
            denied_tools = arguments[arguments.index('--disallowedTools') + 1].split(',')
            for tool in ('Edit', 'Write', 'NotebookEdit'):
                with self.subTest(read_only=read_only, tool=tool):
                    self.assertNotIn(tool, tools)
                    self.assertIn(tool, denied_tools)
                    # A whole-tool rule covers every target, including an
                    # ordinary external file, a new path, and a symlink alias.
                    self.assertIn('Edit', settings['permissions']['deny'])
            self.assertIn('Bash', tools)
            self.assertEqual(arguments[arguments.index('--permission-mode') + 1], 'auto')
            self.assertTrue(settings['sandbox']['enabled'])
            self.assertTrue(settings['sandbox']['failIfUnavailable'])
            self.assertFalse(settings['sandbox']['allowUnsandboxedCommands'])
            self.assertEqual(str(self.workspace) in settings['sandbox']['filesystem']['denyWrite'],
                             read_only)

    def test_home_installed_targeted_runtime_is_readable_without_becoming_writable(self):
        package = Path('/home/manager/runtime/loop_manager_internal')
        _, settings = self.settings(targeted_runtime=package)
        filesystem = settings['sandbox']['filesystem']
        self.assertEqual(filesystem['allowRead'], [str(package)])
        self.assertIn(str(package), filesystem['denyWrite'])
        self.assertNotIn(str(package), filesystem['allowWrite'])
        self.assertNotIn(str(package), settings['permissions']['additionalDirectories'])
        self.assertNotIn(str(package.parent), filesystem['allowRead'])
        for root in (self.workspace, self.workspace.parent, self.temporary / 'package'):
            with self.subTest(root=root), self.assertRaises(IsolationError):
                self.settings(targeted_runtime=root)

    def test_reviewer_keeps_read_commands_but_denies_workspace_and_all_file_edits(self):
        arguments, settings = self.settings(read_only=True)
        self.assertIn('Edit', settings['permissions']['deny'])
        self.assertIn(str(self.workspace), settings['sandbox']['filesystem']['denyWrite'])
        tools = arguments[arguments.index('--tools') + 1].split(',')
        self.assertIn('Bash', tools)
        self.assertIn('Read', tools)
        self.assertFalse({'Edit', 'Write', 'NotebookEdit'} & set(tools))

    def test_auto_review_strict_sandbox_and_owned_temp_are_explicit(self):
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                arguments, settings = self.settings(read_only, fast_mode=True)
                sandbox = settings['sandbox']
                self.assertEqual(arguments[arguments.index('--permission-mode') + 1], 'auto')
                self.assertEqual(settings['autoMode'], {'classifyAllShell': True})
                self.assertTrue(settings['fastMode'])
                self.assertTrue(sandbox['enabled'])
                self.assertTrue(sandbox['failIfUnavailable'])
                self.assertFalse(sandbox['allowUnsandboxedCommands'])
                self.assertFalse(sandbox['autoAllowBashIfSandboxed'])
                self.assertEqual(sandbox['excludedCommands'], [])
                self.assertEqual(sandbox['network']['allowUnixSockets'], [])
                self.assertEqual(sandbox['filesystem']['allowWrite'], [str(self.temporary)])
                self.assertIn(str(Path('/tmp').resolve() / f'claude-{os.getuid()}'),
                              sandbox['filesystem']['denyWrite'])

    def test_inherited_permissions_and_empty_arrays_cannot_cancel_conflicts(self):
        conflicts = [
            {'permissions': {'allow': ['Bash(npm test)']}},
            {'permissions': {'additionalDirectories': [str(self.root)]}},
            {'permissions': {'defaultMode': 'acceptEdits'}},
            {'disableAutoMode': 'disable'},
            {'permissions': {'disableAutoMode': 'disable'}},
            {'autoMode': {'hard_deny': []}},
            {'autoMode': {'classifyAllShell': False}},
            {'sandbox': {'enabled': False}},
            {'sandbox': {'enabled': 1}},
            {'sandbox': {'failIfUnavailable': False}},
            {'sandbox': {'autoAllowBashIfSandboxed': True}},
            {'sandbox': {'allowUnsandboxedCommands': True}},
            {'sandbox': {'excludedCommands': ['python']}},
            {'sandbox': {'filesystem': {'disabled': True}}},
            {'sandbox': {'filesystem': {'allowWrite': [str(self.root)]}}},
            {'sandbox': {'network': {'allowAllUnixSockets': True}}},
            {'sandbox': {'allowNestedUserNamespaces': True}},
            {'sandbox': {'unknownFutureOverride': True}},
            {'env': {'CLAUDE_CODE_TMPDIR': str(self.root)}},
            {'hooks': {'PreToolUse': []}},
            {'policyHelper': '/external/helper'},
            {'apiKeyHelper': 'external-command'},
            {'awsAuthRefresh': 'external-command'},
            {'managedMcpServers': {'writer': {'command': 'writer'}}},
        ]
        for inherited in conflicts:
            with self.subTest(inherited=inherited), self.assertRaises(IsolationError):
                self.settings(managed_settings=inherited)

    def test_compatible_managed_settings_are_not_mutated_or_treated_as_replaced(self):
        inherited = {'permissions': {'allow': [], 'deny': ['Read(//private/**)']},
            'sandbox': {'enabled': True, 'filesystem': {'allowWrite': [str(self.temporary)],
                                                       'denyWrite': [str(self.root / 'private')]}}}
        original = copy.deepcopy(inherited)
        _, settings = self.settings(managed_settings=inherited)
        self.assertEqual(inherited, original)
        # Native merging retains the managed denies; the request never claims
        # to be the final merged policy or drops that source's original bytes.
        self.assertNotEqual(settings, inherited)
        self.assertIn('Read(//private/**)', inherited['permissions']['deny'])

    def test_invalid_policy_types_are_rejected_before_json_emission(self):
        for inherited in ([], {'permissions': []}, {'sandbox': []},
                {'sandbox': {'filesystem': []}}, {'sandbox': {'network': []}},
                {'autoMode': []}, {'permissions': {'deny': 'Edit'}},
                {'sandbox': {'filesystem': {'allowWrite': [1]}}}):
            with self.subTest(inherited=inherited), self.assertRaises(IsolationError):
                self.settings(managed_settings=inherited)

    def test_overlaps_and_ambiguous_glob_roots_are_rejected(self):
        for temporary in (self.boundary.source / 'tmp', self.boundary.common_dir / 'tmp',
                self.workspace / 'tmp', self.workspace.parent, self.root / 'glob[abc]',
                self.root / 'child/../alias'):
            with self.subTest(temporary=temporary), self.assertRaises(IsolationError):
                policy_arguments(self.boundary, temporary, False)

    def test_symlink_temporary_root_is_rejected(self):
        self.temporary.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(self.temporary, target_is_directory=True)
        with self.assertRaisesRegex(IsolationError, '모호한 경로'):
            policy_arguments(self.boundary, alias, False)


class ClaudeUnsupportedPlatformTests(unittest.TestCase):
    def test_native_windows_is_rejected_without_policy_or_attribute_error(self):
        with mock.patch('loop_manager_internal.agents.agent_claude_policy.platform.system',
                        return_value='Windows'), self.assertRaisesRegex(IsolationError, 'native sandbox'):
            policy_arguments(mock.Mock(), Path('/temporary'), False)
