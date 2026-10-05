import json
import hashlib
import sqlite3
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import loop_manager
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents import scope_recovery, agent_scope_decision
from loop_manager_internal.agents import scope_baseline
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.git.managed_rebase_validation import FileChange
from loop_manager_internal.recovery import work_branch_integration as integration
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository


class WorkBranchIntegrationUnitTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.store = ExecutionCheckpointRepository(self.connection)
        self.branch = 'loop-manager/G-166'
        self.plan = dict(plan_id='G-166', branch=self.branch,
                         worktree='/state/G-166', worker_session_id='same-session')
        self.before = GitSnapshot('local', self.branch,
            {'file': ('H 100644 staged-blob 0',)},
            {'file': (('staged', 'unstaged'), 'working-bytes'),
             'new.bin': (('untracked',), 'binary-bytes')},
            {f'refs/heads/{self.branch}': ('local', '')},
            {'/state/G-166': ('HEAD=local', f'branch=refs/heads/{self.branch}')}, {}, {})
        self.now = self.before
        self.tree = 'index-tree'
        self.baseline = json.dumps(dict(ref='preserved-ref', ref_target='preserved-commit',
                                      index_tree='index-tree', worktree_tree='working-tree'))
        self.accepted = SimpleNamespace(status=SimpleNamespace(value='complete'))
        self.service = mock.Mock()
        self.service.require_owned.side_effect = lambda **kw: (
            SimpleNamespace(head=self.now.head), self.accepted)
        self.service.observe.side_effect = lambda: (None, self.accepted)
        self.effects = SimpleNamespace(
            _capture_agent_baseline=mock.Mock(return_value=self.baseline),
            capture_git_snapshot=mock.Mock(side_effect=lambda _: self.now),
            git=mock.Mock(side_effect=self.git),
            _journaled_direct_rebase=mock.Mock(side_effect=self.rebase),
            _current_rebase_service=mock.Mock(return_value=self.service))

    def git(self, _, *args, **kwargs):
        output = ''
        if args[-2:] == ('rebase', '--abort'):
            self.now = replace(self.before, head='preservation', dirty={}, index={}, operations={})
            self.tree = 'working-tree'
        if args[:1] == ('-c',):
            args = args[2:]
        if args[0] == 'commit-tree':
            output = 'preservation'
        elif args[0] == 'update-ref':
            self.now = replace(self.now, head='preservation')
        elif args[0] == 'write-tree':
            output = self.tree
        elif args[0] == 'read-tree':
            self.tree = args[1]
            self.now = replace(self.now, index={}, dirty={})
        return subprocess.CompletedProcess([], 0, output)

    def rebase(self, *args, **kwargs):
        current = self.store.get('repo', 'G-166')
        self.store.record('repo', 'G-166', expected_generation=current.generation,
            operation='rebase', expected_head=self.now.head, target_base_oid='remote',
            observed_remote_oid='remote', snapshot=current.snapshot)
        self.now = replace(self.now, head='combined')
        return subprocess.CompletedProcess([], 0, 'OK')

    def run_integration(self, remote='remote'):
        return integration.integrate(self.effects, self.connection, Path('/repo'),
            Path('/state'), 'repo', self.plan, remote,
            self.before if remote == 'remote' else self.now)

    def test_dirty_input_preserves_index_worktree_untracked_and_exact_remote_lease(self):
        result = self.run_integration()
        current = self.store.get('repo', 'G-166')
        saved = current.snapshot['work_branch_integration']
        self.assertEqual(json.loads(saved['baseline'])['index_tree'], 'index-tree')
        self.assertEqual(saved['local_tree'], 'working-tree')
        self.assertEqual(GitSnapshot.deserialize(saved['before']).dirty, self.before.dirty)
        self.assertEqual(result.head, 'combined')
        self.assertEqual(current.observed_remote_oid, 'remote')
        self.assertIsNone(current.operation)
        self.assertEqual(self.effects._journaled_direct_rebase.call_args.kwargs,
                         dict(observed_remote=('remote',), agent_session_id='same-session'))
        self.assertFalse(any(call.args[1] in {'reset', 'clean', 'push'}
                             for call in self.effects.git.call_args_list))

    def test_pinned_index_stat_refresh_precedes_clean_capture_and_rebase(self):
        git = self.git
        def stat_stale_index(root, *args, **kwargs):
            result = git(root, *args, **kwargs)
            if args[0] == 'read-tree':
                self.now = replace(self.now, dirty={'new.bin': (('unstaged',), 'binary-bytes')})
            elif args == ('update-index', '--refresh'):
                self.now = replace(self.now, dirty={})
            return result
        self.effects.git.side_effect = stat_stale_index
        self.assertFalse(self.run_integration().dirty)
        self.effects._journaled_direct_rebase.assert_called_once()

    def test_working_change_during_index_refresh_keeps_preservation_and_stops_rebase(self):
        git = self.git
        def changed_file(root, *args, **kwargs):
            if args == ('update-index', '--refresh'):
                raise RuntimeError('needs update')
            return git(root, *args, **kwargs)
        self.effects.git.side_effect = changed_file
        with self.assertRaisesRegex(RuntimeError, 'needs update'):
            self.run_integration()
        saved = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.assertEqual(saved['baseline'], self.baseline)
        self.assertEqual(saved['phase'], 'preserving')
        self.effects._journaled_direct_rebase.assert_not_called()

    def test_complete_restart_does_not_repeat_preservation_or_rebase(self):
        self.run_integration()
        self.run_integration()
        self.effects._capture_agent_baseline.assert_called_once()
        self.effects._journaled_direct_rebase.assert_called_once()

    def test_interruption_after_rebase_adopts_manual_completion_without_replaying(self):
        self.service.observe.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration()
        self.assertEqual(self.store.get('repo', 'G-166').operation, 'rebase')
        self.service.observe.side_effect = lambda: (None, self.accepted)
        self.run_integration()
        self.effects._journaled_direct_rebase.assert_called_once()
        self.assertIsNone(self.store.get('repo', 'G-166').operation)

    def test_pending_conflict_never_records_success(self):
        self.accepted.status.value = 'waiting_for_resolution'
        with self.assertRaisesRegex(RuntimeError, '수동 해결'):
            self.run_integration()
        saved = self.store.get('repo', 'G-166')
        self.assertEqual(saved.operation, 'rebase')
        self.assertEqual(saved.snapshot['work_branch_integration']['phase'], 'rebasing')

    def test_changed_working_bytes_refused_before_index_or_head_mutation(self):
        self.now = replace(self.before, dirty={'file': (('unstaged',), 'new-bytes')})
        with self.assertRaisesRegex(RuntimeError, '수집 중'):
            self.run_integration()
        self.effects.git.assert_not_called()
        self.effects._journaled_direct_rebase.assert_not_called()

    def test_interruption_after_ref_update_restores_only_pinned_index(self):
        git = self.effects.git.side_effect
        def interrupted(*args, **kwargs):
            value = git(*args, **kwargs)
            if 'update-ref' in args[1:]:
                raise KeyboardInterrupt
            return value
        self.effects.git.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration()
        self.effects.git.side_effect = git
        self.run_integration()
        self.assertEqual(self.now.head, 'combined')
        self.effects._capture_agent_baseline.assert_called_once()

    def test_new_user_push_preserves_conflict_before_restarting_owned_operation(self):
        self.accepted.status.value = 'waiting_for_resolution'
        with self.assertRaises(RuntimeError):
            self.run_integration()
        self.now = replace(self.now, branch=None,
            index={'conflict.bin': ('M 100644 original 1', 'M 100644 ours 2', 'M 100644 theirs 3')},
            dirty={'conflict.bin': (('staged', 'unstaged'), 'manual-binary')},
            operations={'rebase-merge': 'owned-metadata'})
        self.effects._capture_agent_baseline.side_effect = lambda repo, root, snapshot, **kw: (
            self.baseline if snapshot.dirty else None)
        self.accepted.status.value = 'complete'
        self.run_integration('new-user-remote')
        saved = self.store.get('repo', 'G-166').snapshot
        old = saved['work_branch_integration_history'][0]
        self.assertEqual(old['restart']['baseline'], self.baseline)
        self.assertEqual(GitSnapshot.deserialize(old['restart']['snapshot']).index['conflict.bin'][2],
                         'M 100644 theirs 3')
        self.effects._capture_agent_baseline.assert_any_call(
            'repo', Path('/state/G-166'), GitSnapshot.deserialize(old['restart']['snapshot']),
            preserve_conflicts=True)
        self.assertEqual(saved['work_branch_integration']['remote'], 'new-user-remote')
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 2)


class ConflictBaselineUnitTests(unittest.TestCase):
    def test_all_conflict_stages_are_pinned_without_writing_selected_index(self):
        snapshot = GitSnapshot('head', None,
            {'binary': ('M 100644 base 1', 'M 100644 ours 2', 'M 100644 theirs 3'),
             'other': ('H 100755 staged 0',)},
            {'binary': (('staged', 'unstaged'), 'bytes')}, {}, {}, {}, {'rebase-merge': 'metadata'})
        def git(_, *args, **kwargs):
            value = 'stages' if args[0] == 'ls-files' else 'head-tree' if args[0] == 'rev-parse' else ''
            return subprocess.CompletedProcess([], 1 if '--verify' in args else 0, value)
        effects = SimpleNamespace(git=mock.Mock(side_effect=git))
        index_calls = []
        def index(root, path, *args, **kwargs):
            index_calls.append(args)
            return subprocess.CompletedProcess([], 0, 'tree-' + path.name if args[0] == 'write-tree' else '')
        with (mock.patch.object(scope_baseline, '_git_with_index', side_effect=index),
              mock.patch.object(scope_baseline, '_agent_worktree_tree', return_value='working-tree'),
              mock.patch.object(scope_baseline, '_commit_tree', return_value='pinned') as commits):
            result = json.loads(scope_baseline._capture_agent_baseline(
                'repo', Path('/work'), snapshot, effects=effects, preserve_conflicts=True))
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['stage_trees'], {str(stage): 'tree-' + str(stage) for stage in range(4)})
        for mode, blob, path in [('100644', 'base', 'binary'), ('100644', 'ours', 'binary'),
                                 ('100644', 'theirs', 'binary'), ('100755', 'staged', 'other')]:
            self.assertIn(('update-index', '--add', '--cacheinfo', mode, blob, path), index_calls)
        self.assertEqual(commits.call_count, 6)
        self.assertFalse(any('read-tree' in call.args for call in effects.git.call_args_list))


class UserBlobProvenanceUnitTests(unittest.TestCase):
    def test_only_exact_owned_preservation_ref_can_advance_comparison(self):
        prefix = 'refs/loop-manager/baselines/' + hashlib.sha256(b'repo').hexdigest()[:16] + '/'
        owned = prefix + 'a' * 64
        similar = prefix + 'b' * 64
        saved = {'work_branch_integration': {'baseline': json.dumps(
            {'ref': owned, 'ref_target': 'pinned'})}}
        self.assertEqual(integration.preserved_refs('repo', saved,
            {owned: ('pinned', ''), similar: ('other', '')}), frozenset({owned}))
        self.assertEqual(integration.preserved_refs('repo', saved,
            {owned: ('tampered', '')}), frozenset())
        self.assertEqual(integration.preserved_refs('different-repo', saved,
            {owned: ('pinned', '')}), frozenset())
    def test_exact_user_document_blob_is_allowed_and_later_agent_blob_is_rejected(self):
        path = 'AGENTS.md'
        change = FileChange(path, '100644', '100644', 'old', 'user', 'M')
        state = SimpleNamespace(committed=(change,), staged=(), files=(), index=())
        blobs = {path: {('100644', 'user')}}
        self.assertEqual(integration.matching_user_paths(state, blobs), {path})
        state.staged = (replace(change, new_oid='agent'),)
        self.assertEqual(integration.matching_user_paths(state, blobs), set())

    def test_unconfirmed_working_bytes_and_conflict_stages_do_not_borrow_user_permission(self):
        state = SimpleNamespace(committed=(), staged=(), files=(SimpleNamespace(path='AGENTS.md'),), index=())
        blobs = {'AGENTS.md': {('100644', 'user')}}
        self.assertEqual(integration.matching_user_paths(state, blobs), set())
        state.files = ()
        state.index = (SimpleNamespace(path='AGENTS.md', stage=2),)
        self.assertEqual(integration.matching_user_paths(state, blobs), set())

    def test_user_blob_diff_preserves_delete_and_binary_identity(self):
        git = mock.Mock(return_value=subprocess.CompletedProcess([], 0,
            ':100644 000000 old 000000 D\0deleted\0:100644 100644 old binary M\0file.bin\0'))
        self.assertEqual(integration.user_changes(git, Path('/work'), 'base', ('remote',)),
            {'deleted': {('000000', '000000')}, 'file.bin': {('100644', 'binary')}})


class BlockedUserInputScopeUnitTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.before = GitSnapshot('agent-start', 'loop-manager/G-166', {}, {},
            {'refs/heads/loop-manager/G-166': ('agent-start', '')},
            {'/state/G-166': ('HEAD=agent-start', 'branch=refs/heads/loop-manager/G-166')}, {}, {},
            scope_id='finished-scope', scope_start_digest='start-digest')
        self.result = replace(self.before, dirty={'code.py': (('untracked',), 'agent-bytes')})
        self.current = replace(self.result, head='user-head',
            dirty={'AGENTS.md': (('staged', 'unstaged'), 'user-bytes')},
            refs={**self.before.refs, 'refs/heads/loop-manager/G-166': ('user-head', ''),
                  'refs/heads/main': ('user-base', ''), 'refs/heads/loop-manager/user': ('user-ref', '')},
            worktrees={**self.before.worktrees, '/source': ('HEAD=user-base',)})
        self.plan = dict(agent_snapshot=self.before.serialize(), agent_role='worker',
            agent_result_snapshot=self.result.serialize(), agent_recovery=None,
            agent_manager_snapshot=None, branch=self.before.branch, worktree='/state/G-166',
            blocked_reason='verify_retry_exhausted', current_revision_sha='base', plan_sha='base')
        resources = agent_scope_decision.used_git_resources(
            role='worker', phase='PLAN_BLOCKED', base_branch='main',
            branch=self.before.branch, worktree_key='/state/G-166')
        self.effects = SimpleNamespace(
            repository=mock.Mock(return_value=SimpleNamespace(scope_owner=lambda *args: self.plan,
                                                               scope_catalog_available=lambda: False)),
            capture_git_snapshot=mock.Mock(return_value=self.current),
            _plan_id_from_path=mock.Mock(return_value='G-166'),
            _owned_rebase_state_dir=mock.Mock(return_value=None),
            _proven_merged_cleanup_paths=mock.Mock(return_value=frozenset()),
            _proven_manager_base_refs=mock.Mock(return_value=frozenset()),
            requirement_archive_candidates=mock.Mock(return_value=()),
            _guidance_worker_targets=mock.Mock(return_value=()),
            _published_plan_allows_common_plans=mock.Mock(return_value=False),
            _agent_git_resources=mock.Mock(return_value=resources),
            validate_agent_scope=mock.Mock(),
            git=mock.Mock(return_value=subprocess.CompletedProcess([], 0, '')),
            github_repository=mock.Mock())

    def validate(self, **options):
        scope_recovery.validate_blocked_agent_scope(
            self.connection, Path('/source'), 'repo', 'main', Path('/state/G-166'),
            'docs/plans/[G-166] Work.md', saved_plan=self.plan, effects=self.effects, **options)

    def test_completed_isolated_agent_result_is_checked_before_local_user_changes(self):
        self.validate()
        checks = self.effects.validate_agent_scope.call_args_list
        self.assertEqual(checks[0].args[0], 'worker')
        self.assertEqual(checks[0].args[3], self.result)
        self.assertEqual(checks[-1].args[0], 'reviewer')
        self.assertEqual(checks[-1].args[2].dirty, self.current.dirty)
        self.assertEqual(checks[-1].kwargs['resources'].refs,
            frozenset({'refs/heads/loop-manager/G-166', 'refs/remotes/origin/main',
                       'refs/remotes/origin/loop-manager/G-166'}))

    def test_same_head_input_needs_no_ancestry_command(self):
        self.effects.capture_git_snapshot.return_value = replace(self.current, head=self.result.head)
        self.validate()
        self.effects.git.assert_not_called()

    def test_scope_correction_keeps_saved_result_and_checks_current_state(self):
        self.plan['blocked_reason'] = 'managed_worktree_scope_violation'
        self.effects.capture_git_snapshot.return_value = self.result
        self.validate()
        self.effects.git.assert_not_called()
        self.assertEqual(self.effects.validate_agent_scope.call_args.args[3], self.result)
        self.assertEqual(self.plan['agent_result_snapshot'], self.result.serialize())

    def test_actual_agent_violation_is_not_absorbed_as_user_input(self):
        self.effects.validate_agent_scope.side_effect = AgentScopeError('actual Agent violation')
        with self.assertRaisesRegex(AgentScopeError, 'actual Agent'):
            self.validate()
        self.effects.validate_agent_scope.assert_called_once()

    def test_unfinished_or_unproven_scope_does_not_absorb_new_user_bytes(self):
        self.effects._agent_git_resources.return_value = None
        with self.assertRaisesRegex(AgentScopeError, '새 파일 또는 index 변경'):
            self.validate()

    def test_shared_config_change_remains_visible_to_read_only_scope_check(self):
        changed = replace(self.current, config={'local:remote.origin.url': ('foreign',)})
        self.effects.capture_git_snapshot.return_value = changed
        def check(role, root, before, after, **kwargs):
            if before.config != after.config:
                raise AgentScopeError('origin changed')
        self.effects.validate_agent_scope.side_effect = check
        with self.assertRaisesRegex(AgentScopeError, 'origin changed'):
            self.validate()

    def test_legacy_safe_result_ignores_later_default_checkout_and_unowned_branch_metadata(self):
        self.effects._agent_git_resources.return_value = None
        self.plan['workspace_owned'] = 1
        self.current = replace(self.result,
            refs={**self.result.refs, 'refs/heads/main': ('user-base', ''),
                  'refs/heads/loop-manager/user': ('user-ref', '')},
            worktrees={**self.result.worktrees, '/source': ('HEAD=user-base',)})
        self.effects.capture_git_snapshot.return_value = self.current
        self.validate(require_safe_result=True)
        checked = self.effects.validate_agent_scope.call_args_list
        self.assertEqual(checked[0].args[3], self.result)
        self.assertIn('refs/remotes/origin/main', checked[-1].kwargs['resources'].refs)
        self.assertNotIn('refs/heads/main', checked[-1].kwargs['resources'].refs)


if __name__ == '__main__':
    unittest.main()
