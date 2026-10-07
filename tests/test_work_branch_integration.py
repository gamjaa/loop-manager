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
from loop_manager_internal.git.managed_rebase_validation import FileChange, RebaseDecision, RebaseStatus
from loop_manager_internal.git import managed_rebase_validation as rebase_policy
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
        self.service.require_owned.side_effect = self.owned
        self.service.observe.side_effect = lambda: (None, self.accepted)
        self.effects = SimpleNamespace(
            _capture_agent_baseline=mock.Mock(return_value=self.baseline),
            _git_with_index=mock.Mock(return_value=subprocess.CompletedProcess([], 0, 'working-tree')),
            _commit_tree=mock.Mock(return_value='preservation'),
            capture_git_snapshot=mock.Mock(side_effect=lambda _: self.now),
            git=mock.Mock(side_effect=self.git),
            _journaled_direct_rebase=mock.Mock(side_effect=self.rebase),
            _current_rebase_service=mock.Mock(return_value=self.service))

    def owned(self, **kwargs):
        current = self.store.get('repo', 'G-166')
        decision = self.accepted
        if self.now.operations:
            decision = SimpleNamespace(status=SimpleNamespace(value='waiting_for_resolution'))
        elif current.operation == 'rebase' and self.now.head == current.expected_head:
            decision = SimpleNamespace(status=SimpleNamespace(value='ready_to_start'))
        return SimpleNamespace(head=self.now.head), decision

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
            operation='rebase', expected_head=self.now.head, target_base_oid=args[-1],
            observed_remote_oid=kwargs['observed_remote'][0], snapshot=current.snapshot)
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

    def test_application_tree_is_normalized_while_raw_baseline_is_retained(self):
        self.effects._git_with_index.return_value.stdout = 'normalized-tree'
        self.run_integration()
        saved = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.assertEqual(json.loads(saved['baseline'])['worktree_tree'], 'working-tree')
        self.assertEqual(saved['local_tree'], 'normalized-tree')
        self.effects._commit_tree.assert_called_once_with(
            Path(self.plan['worktree']), 'normalized-tree', 'local', 'Preserve local input for G-166')
        calls = self.effects._git_with_index.call_args_list
        self.assertEqual(calls[1].args[2:], ('--literal-pathspecs', 'add', '--all', '--', 'file', 'new.bin'))
        self.assertEqual(calls[0].args[1], calls[1].args[1])
        self.assertEqual(calls[1].args[1], calls[2].args[1])
        self.assertNotEqual(calls[1].args[1], Path(self.plan['worktree']) / '.git/index')

    def test_filter_failure_stops_before_selected_head_or_index_moves(self):
        self.effects._git_with_index.side_effect = RuntimeError('working-tree-encoding conversion failed')
        with self.assertRaisesRegex(RuntimeError, 'conversion failed'):
            self.run_integration()
        self.effects._commit_tree.assert_not_called()
        self.effects.git.assert_not_called()
        self.effects._journaled_direct_rebase.assert_not_called()

    def test_preservation_commit_uses_manager_identity_without_user_config(self):
        def command(arguments, *, env):
            self.assertEqual(env['GIT_AUTHOR_NAME'], 'Loop Manager')
            self.assertEqual(env['GIT_COMMITTER_NAME'], 'Loop Manager')
            self.assertEqual(env['GIT_AUTHOR_EMAIL'], 'loop-manager@localhost')
            self.assertEqual(env['GIT_COMMITTER_EMAIL'], 'loop-manager@localhost')
            return subprocess.CompletedProcess(arguments, 0, 'preservation\n')
        effects = SimpleNamespace(command=mock.Mock(side_effect=command))
        self.effects._commit_tree.side_effect = lambda root, tree, parent, message: scope_baseline._commit_tree(
            root, tree, parent, message, effects=effects)
        self.assertEqual(self.run_integration().head, 'combined')
        self.assertIn('commit-tree', effects.command.call_args.args[0])

    def test_base_conflict_restart_preserves_stages_and_resumes_after_abort_interruption(self):
        conflict = replace(self.before, head='detached', branch=None,
            index={'file': ('M 100644 base 1', 'M 100644 ours 2', 'M 100644 theirs 3')},
            dirty={'file': (('staged', 'unstaged'), 'manual')}, operations={'rebase-merge': 'owned'})
        self.now = conflict
        self.store.record('repo', 'G-166', expected_generation=0, operation='rebase',
            expected_head='local', target_base_oid='base', observed_remote_oid='old-remote', snapshot={})
        git = self.git
        def abort(root, *args, **kwargs):
            if args[-2:] == ('rebase', '--abort'):
                self.now = replace(self.before, dirty={}, operations={})
                raise KeyboardInterrupt
            return git(root, *args, **kwargs)
        self.effects.git.side_effect = abort
        arguments = (self.effects, self.connection, Path('/repo'), Path('/state'),
                     'repo', self.plan, 'new-remote')
        with self.assertRaises(KeyboardInterrupt):
            integration.restart_base_rebase(*arguments, conflict)
        saved = self.store.get('repo', 'G-166')
        self.assertEqual(saved.operation, 'rebase')
        restart = saved.snapshot['base_rebase_restart']
        self.assertEqual(GitSnapshot.deserialize(restart['snapshot']).index, conflict.index)
        self.effects.git.reset_mock()
        completed = integration.restart_base_rebase(*arguments, self.now)
        self.assertIsNone(completed.operation)
        self.assertEqual(completed.expected_head, 'local')
        self.assertEqual(completed.target_base_oid, 'new-remote')
        self.assertEqual(completed.snapshot['base_rebase_restart']['phase'], 'complete')
        self.assertEqual(completed.snapshot['base_rebase_restart']['baseline'], self.baseline)
        self.effects.git.assert_not_called()
        self.effects._capture_agent_baseline.assert_called_once_with(
            'repo', Path(self.plan['worktree']), conflict, preserve_conflicts=True)

    def test_base_conflict_ownership_refusal_does_not_capture_or_abort(self):
        self.store.record('repo', 'G-166', expected_generation=0, operation='rebase',
            expected_head='local', target_base_oid='base', observed_remote_oid='old-remote', snapshot={})
        self.service.require_owned.side_effect = RuntimeError('origin or shared ownership changed')
        with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
            integration.restart_base_rebase(self.effects, self.connection, Path('/repo'), Path('/state'),
                                           'repo', self.plan, 'new-remote', self.now)
        self.effects._capture_agent_baseline.assert_not_called()
        self.effects.git.assert_not_called()
        self.assertEqual(self.store.get('repo', 'G-166').operation, 'rebase')

    def _base_completion_service(self):
        self.now = replace(self.now, head='base-completed', branch=self.branch, dirty={}, index={}, operations={})
        def service(*args, **kwargs):
            current = self.store.get('repo', 'G-166')
            acceptance = dict(kind=rebase_policy.ACCEPTANCE_KIND, policy_version=rebase_policy.POLICY_VERSION,
                repo='repo', repository_identity='repository', plan_id='G-166',
                plan_path='docs/plans/[G-166] Work.md', worktree=self.plan['worktree'], branch=self.branch,
                target_base_oid=current.target_base_oid, head=self.now.head, generation=current.generation,
                remote=dict(repository='repo', branch=self.branch, oid=kwargs['remote_oid'], pull_request=None),
                input_fingerprint='input', scope_fingerprint='scope', state_fingerprint='state', role='worker')
            acceptance['id'] = rebase_policy.fingerprint(acceptance)
            self.accepted = RebaseDecision(RebaseStatus.COMPLETE, acceptance=acceptance)
            return self.service
        self.effects._current_rebase_service.side_effect = service

    def _pending_base_completion(self):
        current = self.store.record('repo', 'G-166', expected_generation=0, operation='rebase',
            expected_head='local', target_base_oid='base', observed_remote_oid='old-remote',
            snapshot={'agent-audit': self.before.serialize(), 'worker-session': 'same-session',
                      'current_rebase_acceptance': {'old': 'audit'}})
        self._base_completion_service()
        return current

    def complete_base(self, remote='successor'):
        return integration.restart_base_rebase(self.effects, self.connection, Path('/repo'), Path('/state'),
                                               'repo', self.plan, remote, self.now)

    def test_manual_base_completion_is_accepted_before_successor_without_abort(self):
        original = self._pending_base_completion()
        completed = self.complete_base()
        self.assertIsNone(completed.operation)
        self.assertEqual(completed.expected_head, 'base-completed')
        self.assertEqual(completed.target_base_oid, 'base')
        self.assertEqual(completed.observed_remote_oid, 'successor')
        self.assertEqual(completed.current_rebase_acceptance(), self.accepted.acceptance)
        restart = completed.snapshot['base_rebase_restart']
        self.assertEqual((restart['original_head'], restart['remote_before']), ('local', 'old-remote'))
        self.assertEqual(restart['acceptance_id'], self.accepted.acceptance['id'])
        self.assertEqual(completed.snapshot['current_rebase_acceptance_history'], [{'old': 'audit'}])
        for key in ('agent-audit', 'worker-session'):
            self.assertEqual(completed.snapshot[key], original.snapshot[key])
        self.effects._capture_agent_baseline.assert_not_called()
        self.effects.git.assert_not_called()
        self.effects._journaled_direct_rebase.assert_not_called()
        self.assertEqual(self.complete_base(), completed)

    def test_manual_base_completion_keeps_saved_conflict_and_retargets_successor(self):
        original = self._pause_base_restart(after_abort=False)
        self._base_completion_service()
        completed = self.complete_base('third-input')
        self.assertEqual(completed.snapshot['base_rebase_restart_history'], [original])
        for key in ('baseline', 'snapshot', 'original_head'):
            self.assertEqual(completed.snapshot['base_rebase_restart'][key], original[key])
        self.assertEqual(completed.snapshot['base_rebase_restart']['remote'], 'third-input')
        self.assertEqual(completed.target_base_oid, 'base')
        self.assertEqual(completed.expected_head, 'base-completed')
        self.effects._capture_agent_baseline.assert_called_once()
        self.assertFalse(any(call.args[-2:] == ('rebase', '--abort')
                             for call in self.effects.git.call_args_list))

    def test_manual_base_completion_recheck_interruption_retries_same_result(self):
        original = self._pending_base_completion()
        self.service.observe.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.complete_base()
        self.assertEqual(self.store.get('repo', 'G-166'), original)
        self.service.observe.side_effect = lambda: (None, self.accepted)
        self.assertEqual(self.complete_base().expected_head, 'base-completed')
        self.effects.git.assert_not_called()

    def test_manual_base_completion_save_interruption_does_not_repeat_completion(self):
        self._pending_base_completion()
        complete = integration._complete_owned_rebase
        def interrupted(*args):
            complete(*args)
            raise KeyboardInterrupt
        with mock.patch.object(integration, '_complete_owned_rebase', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.complete_base()
        saved = self.store.get('repo', 'G-166')
        self.assertIsNone(saved.operation)
        self.assertEqual(self.complete_base(), saved)
        self.effects._capture_agent_baseline.assert_not_called()
        self.effects.git.assert_not_called()

    def test_changed_manual_base_completion_is_refused_before_abort(self):
        original = self._pending_base_completion()
        self.service.observe.side_effect = lambda: (None, RebaseDecision(RebaseStatus.WAITING))
        with self.assertRaisesRegex(RuntimeError, '완료 관측'):
            self.complete_base()
        self.assertEqual(self.store.get('repo', 'G-166'), original)
        self.effects._capture_agent_baseline.assert_not_called()
        self.effects.git.assert_not_called()

    def test_checkpoint_change_during_manual_base_completion_is_not_adopted(self):
        original = self._pending_base_completion()
        def changed():
            current = self.store.get('repo', 'G-166')
            self.store.record('repo', 'G-166', expected_generation=current.generation,
                operation=current.operation, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                snapshot={**current.snapshot, 'other-intent': True})
            return None, self.accepted
        self.service.observe.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, '완료 관측'):
            self.complete_base()
        self.assertEqual(self.store.get('repo', 'G-166'), original)
        self.effects.git.assert_not_called()

    def test_manual_base_completion_still_requires_current_ownership(self):
        original = self._pending_base_completion()
        self.service.require_owned.side_effect = RuntimeError('origin or Agent scope changed')
        with self.assertRaisesRegex(RuntimeError, 'origin or Agent scope'):
            self.complete_base()
        self.assertEqual(self.store.get('repo', 'G-166'), original)
        self.effects._capture_agent_baseline.assert_not_called()
        self.effects.git.assert_not_called()

    def test_manual_base_completion_rejects_rewritten_pending_successor(self):
        self._pause_base_restart(after_abort=False)
        original = self.store.get('repo', 'G-166')
        self._base_completion_service()
        self.effects.git.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess([], 1, '')
        with self.assertRaisesRegex(RuntimeError, '후속 commit'):
            self.complete_base('third-input')
        self.assertEqual(self.store.get('repo', 'G-166'), original)

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

    def _manually_complete_pending_rebase(self):
        self.accepted.status.value = 'waiting_for_resolution'
        with self.assertRaisesRegex(RuntimeError, '수동 해결'):
            self.run_integration()
        original = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.now = replace(self.now, head='manual-completion', dirty={}, index={}, operations={})
        self.accepted.status.value = 'complete'
        self.effects._capture_agent_baseline.return_value = None
        self.effects.git.reset_mock()
        return original

    def _assert_completed_successor(self, original):
        current = self.store.get('repo', 'G-166')
        self.assertIsNone(current.operation)
        self.assertEqual(current.expected_head, 'combined')
        completed = current.snapshot['work_branch_integration_history'][0]
        self.assertEqual(completed, {**original, 'phase': 'complete', 'head': 'manual-completion'})
        saved = current.snapshot['work_branch_integration']
        self.assertEqual(GitSnapshot.deserialize(saved['before']).head, 'manual-completion')
        self.assertEqual(saved['local_commit'], 'manual-completion')
        self.assertEqual(saved['phase'], 'complete')
        self.assertFalse(any(call.args[-2:] == ('rebase', '--abort')
                             for call in self.effects.git.call_args_list))
        self.effects._commit_tree.assert_called_once()
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 2)
        self.assertEqual(self.effects._journaled_direct_rebase.call_args.kwargs['agent_session_id'],
                         'same-session')

    def test_manual_completion_is_accepted_before_integrating_successor(self):
        original = self._manually_complete_pending_rebase()
        self.run_integration('successor')
        self._assert_completed_successor(original)
        self.assertEqual(self.store.get('repo', 'G-166').target_base_oid, 'successor')
        self.assertEqual(self.store.get('repo', 'G-166').observed_remote_oid, 'successor')
        self.run_integration('successor')
        self._assert_completed_successor(original)

    def test_push_after_completion_save_interruption_keeps_completed_target_and_head(self):
        original = self._manually_complete_pending_rebase()
        record = ExecutionCheckpointRepository.record
        def interrupted(store, *args, **kwargs):
            if kwargs['snapshot'].get('work_branch_integration') is None:
                raise KeyboardInterrupt
            return record(store, *args, **kwargs)
        with mock.patch.object(ExecutionCheckpointRepository, 'record', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_integration('successor')
        current = self.store.get('repo', 'G-166')
        self.assertIsNone(current.operation)
        self.assertEqual(current.expected_head, 'manual-completion')
        self.assertEqual(current.target_base_oid, 'remote')
        self.assertEqual(current.observed_remote_oid, 'successor')
        self.assertEqual(current.snapshot['work_branch_integration'],
                         {**original, 'phase': 'complete', 'head': 'manual-completion'})
        self.run_integration('third-input')
        self._assert_completed_successor(original)
        self.assertEqual(self.store.get('repo', 'G-166').observed_remote_oid, 'third-input')

    def test_interruption_during_manual_completion_recheck_retries_without_abort(self):
        original = self._manually_complete_pending_rebase()
        self.service.observe.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration('successor')
        self.assertEqual(self.store.get('repo', 'G-166').operation, 'rebase')
        self.assertEqual(self.store.get('repo', 'G-166').snapshot['work_branch_integration'], original)
        self.service.observe.side_effect = lambda: (None, self.accepted)
        self.run_integration('successor')
        self._assert_completed_successor(original)

    def test_changed_manual_completion_is_refused_before_abort_or_successor_rebase(self):
        original = self._manually_complete_pending_rebase()
        self.service.observe.return_value = (None, SimpleNamespace(status='changed'))
        self.service.observe.side_effect = None
        with self.assertRaisesRegex(RuntimeError, '완료 관측'):
            self.run_integration('successor')
        self.assertEqual(self.store.get('repo', 'G-166').snapshot['work_branch_integration'], original)
        self.assertEqual(self.now.head, 'manual-completion')
        self.effects.git.assert_not_called()
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 1)

    def test_checkpoint_change_during_completion_recheck_is_not_adopted(self):
        original = self._manually_complete_pending_rebase()
        def changed():
            current = self.store.get('repo', 'G-166')
            self.store.record('repo', 'G-166', expected_generation=current.generation,
                operation=current.operation, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                snapshot={**current.snapshot, 'other-intent': True})
            return None, self.accepted
        self.service.observe.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, '완료 관측'):
            self.run_integration('successor')
        self.assertEqual(self.store.get('repo', 'G-166').snapshot['work_branch_integration'], original)
        self.assertEqual(self.store.get('repo', 'G-166').operation, 'rebase')
        self.effects.git.assert_not_called()
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 1)

    def test_manual_completion_successor_still_requires_current_ownership(self):
        original = self._manually_complete_pending_rebase()
        self.service.require_owned.side_effect = RuntimeError('origin or Agent scope changed')
        with self.assertRaisesRegex(RuntimeError, 'origin or Agent scope'):
            self.run_integration('successor')
        self.assertEqual(self.store.get('repo', 'G-166').snapshot['work_branch_integration'], original)
        self.effects.git.assert_not_called()
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 1)

    def test_pending_rebase_start_refreshes_service_generation_before_completion(self):
        def interrupted(*args, **kwargs):
            current = self.store.get('repo', 'G-166')
            self.store.record('repo', 'G-166', expected_generation=current.generation,
                operation='rebase', expected_head=self.now.head, target_base_oid=args[-1],
                observed_remote_oid=kwargs['observed_remote'][0], snapshot=current.snapshot)
            raise KeyboardInterrupt
        self.effects._journaled_direct_rebase.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration()
        pending_generation = self.store.get('repo', 'G-166').generation
        generations = []
        def service(*args, **kwargs):
            generations.append(self.store.get('repo', 'G-166').generation)
            return self.service
        self.effects._current_rebase_service.side_effect = service
        self.effects._journaled_direct_rebase.side_effect = self.rebase
        self.run_integration()
        self.assertEqual(generations, [pending_generation, pending_generation + 1])
        self.assertIsNone(self.store.get('repo', 'G-166').operation)
        self.assertEqual(self.store.get('repo', 'G-166').expected_head, 'combined')
        self.effects._commit_tree.assert_called_once()

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

    def test_new_successor_after_ref_update_interruption_reuses_original_preservation(self):
        git = self.git
        def interrupted(root, *args, **kwargs):
            result = git(root, *args, **kwargs)
            if 'update-ref' in args:
                raise KeyboardInterrupt
            return result
        self.effects.git.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration()
        original = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.assertEqual(original['phase'], 'preserving')
        self.effects.git.side_effect = git
        self.run_integration('successor')
        current = self.store.get('repo', 'G-166')
        saved = current.snapshot['work_branch_integration']
        self.assertEqual(saved['phase'], 'complete')
        self.assertEqual(current.observed_remote_oid, 'successor')
        self.assertEqual(current.snapshot['work_branch_integration_history'], [original])
        for field in ('before', 'baseline', 'local_commit', 'local_tree'):
            self.assertEqual(saved[field], original[field])
        self.effects._capture_agent_baseline.assert_called_once()
        self.effects._journaled_direct_rebase.assert_called_once()
        self.assertEqual(self.effects._journaled_direct_rebase.call_args.args[-1], 'successor')

    def test_new_successor_before_rebase_start_rechecks_pinned_work_and_does_not_repeat_commit(self):
        self.effects._journaled_direct_rebase.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration()
        original = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.assertEqual(original['phase'], 'rebasing')
        self.assertIsNone(self.store.get('repo', 'G-166').operation)
        self.effects._journaled_direct_rebase.side_effect = self.rebase
        self.run_integration('successor')
        self.assertEqual(self.now.head, 'combined')
        saved = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.assertEqual(saved['baseline'], original['baseline'])
        self.assertEqual(saved['before'], original['before'])
        self.effects._capture_agent_baseline.assert_called_once()
        self.assertEqual(self.effects._journaled_direct_rebase.call_args.kwargs['observed_remote'],
                         ('successor',))
        self.assertEqual(sum('update-ref' in call.args for call in self.effects.git.call_args_list), 1)

    def test_pending_successor_rejects_changed_local_work_without_mutating_git(self):
        self.effects._journaled_direct_rebase.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration()
        self.now = replace(self.now, dirty={'file': (('unstaged',), 'later user bytes')})
        self.effects.git.reset_mock()
        with self.assertRaisesRegex(RuntimeError, '새 로컬 작업물'):
            self.run_integration('successor')
        self.assertFalse(any('read-tree' in call.args or 'update-ref' in call.args
                             for call in self.effects.git.call_args_list))
        self.assertEqual(self.now.dirty['file'][1], 'later user bytes')
        saved = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.assertEqual(saved['baseline'], self.baseline)

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

    def _pause_work_restart(self, *, after_abort):
        self.accepted.status.value = 'waiting_for_resolution'
        with self.assertRaises(RuntimeError):
            self.run_integration()
        self.now = replace(self.now, branch=None,
            index={'conflict.bin': ('M 100644 base 1', 'M 100644 ours 2', 'M 100644 theirs 3')},
            dirty={'conflict.bin': (('staged', 'unstaged'), 'manual binary bytes')},
            operations={'rebase-merge': 'owned'})
        self.effects._capture_agent_baseline.side_effect = lambda repo, root, snapshot, **kw: (
            self.baseline if snapshot.dirty else None)
        self.accepted.status.value = 'complete'
        def interrupted(root, *args, **kwargs):
            if args[-2:] == ('rebase', '--abort'):
                if after_abort:
                    self.git(root, *args, **kwargs)
                raise KeyboardInterrupt
            return self.git(root, *args, **kwargs)
        self.effects.git.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_integration('second-input')
        original = self.store.get('repo', 'G-166').snapshot['work_branch_integration']
        self.assertEqual(original['restart']['remote'], 'second-input')
        self.effects.git.side_effect = self.git
        self.effects.git.reset_mock()
        return original

    def _work_restart_successor(self, *, after_abort):
        original = self._pause_work_restart(after_abort=after_abort)
        self.run_integration('third-input')
        current = self.store.get('repo', 'G-166')
        history = current.snapshot['work_branch_integration_history']
        self.assertEqual(history[0], original)
        for field in ('baseline', 'snapshot', 'original_head'):
            self.assertEqual(history[1]['restart'][field], original['restart'][field])
        self.assertEqual(history[1]['restart']['remote'], 'third-input')
        self.assertEqual(current.snapshot['work_branch_integration']['phase'], 'complete')
        self.assertEqual(current.observed_remote_oid, 'third-input')
        self.assertIsNone(current.operation)
        self.assertEqual(sum(call.kwargs.get('preserve_conflicts', False)
                             for call in self.effects._capture_agent_baseline.call_args_list), 1)
        self.assertEqual(sum(call.args[-2:] == ('rebase', '--abort')
                             for call in self.effects.git.call_args_list), int(not after_abort))
        self.assertEqual(self.effects._commit_tree.call_count, 1)
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 2)
        self.effects.git.reset_mock()
        self.run_integration('third-input')
        self.assertFalse(any(call.args[-2:] == ('rebase', '--abort')
                             for call in self.effects.git.call_args_list))
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 2)

    def test_push_after_restart_save_reuses_conflict_and_restarts_once(self):
        self._work_restart_successor(after_abort=False)

    def test_push_after_abort_reuses_conflict_without_second_abort(self):
        self._work_restart_successor(after_abort=True)

    def test_restart_successor_rewrite_is_refused_without_losing_preservation(self):
        original = self._pause_work_restart(after_abort=False)
        def rewritten(root, *args, **kwargs):
            if args == ('merge-base', '--is-ancestor', 'second-input', 'third-input'):
                return subprocess.CompletedProcess([], 1, '')
            return self.git(root, *args, **kwargs)
        self.effects.git.side_effect = rewritten
        with self.assertRaisesRegex(RuntimeError, '후속 commit'):
            self.run_integration('third-input')
        self.assertEqual(self.store.get('repo', 'G-166').snapshot['work_branch_integration'], original)
        self.assertFalse(any(call.args[-2:] == ('rebase', '--abort')
                             for call in self.effects.git.call_args_list))
        self.assertEqual(self.effects._journaled_direct_rebase.call_count, 1)

    def test_restart_successor_still_requires_current_ownership(self):
        original = self._pause_work_restart(after_abort=True)
        self.service.require_owned.side_effect = RuntimeError('origin or ownership changed')
        with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
            self.run_integration('third-input')
        self.assertEqual(self.store.get('repo', 'G-166').snapshot['work_branch_integration'], original)
        self.effects.git.assert_not_called()

    def _pause_base_restart(self, *, after_abort):
        self.now = replace(self.before, head='detached', branch=None,
            index={'file': ('M 100644 base 1', 'M 100644 ours 2', 'M 100644 theirs 3')},
            dirty={'file': (('staged', 'unstaged'), 'manual base conflict')},
            operations={'rebase-merge': 'owned'})
        self.store.record('repo', 'G-166', expected_generation=0, operation='rebase',
            expected_head='local', target_base_oid='base', observed_remote_oid='remote', snapshot={})
        def restored(root, *args, **kwargs):
            if args[-2:] == ('rebase', '--abort'):
                self.now = replace(self.before, dirty={}, operations={})
                return subprocess.CompletedProcess([], 0, '')
            return self.git(root, *args, **kwargs)
        def interrupted(root, *args, **kwargs):
            if args[-2:] == ('rebase', '--abort'):
                if after_abort:
                    restored(root, *args, **kwargs)
                raise KeyboardInterrupt
            return self.git(root, *args, **kwargs)
        self.effects.git.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            integration.restart_base_rebase(self.effects, self.connection, Path('/repo'), Path('/state'),
                                           'repo', self.plan, 'second-input', self.now)
        original = self.store.get('repo', 'G-166').snapshot['base_rebase_restart']
        self.effects.git.side_effect = restored
        self.effects.git.reset_mock()
        return original

    def _base_restart_successor(self, *, after_abort):
        original = self._pause_base_restart(after_abort=after_abort)
        current = integration.restart_base_rebase(self.effects, self.connection, Path('/repo'), Path('/state'),
                                                 'repo', self.plan, 'third-input', self.now)
        self.assertEqual(current.snapshot['base_rebase_restart_history'], [original])
        for field in ('baseline', 'snapshot', 'original_head'):
            self.assertEqual(current.snapshot['base_rebase_restart'][field], original[field])
        self.assertEqual(current.snapshot['base_rebase_restart']['remote'], 'third-input')
        self.assertEqual(current.snapshot['base_rebase_restart']['phase'], 'complete')
        self.assertEqual(current.target_base_oid, 'third-input')
        self.assertIsNone(current.operation)
        self.effects._capture_agent_baseline.assert_called_once()
        self.assertEqual(sum(call.args[-2:] == ('rebase', '--abort')
                             for call in self.effects.git.call_args_list), int(not after_abort))

    def test_base_successor_after_restart_save_retains_conflict_snapshot(self):
        self._base_restart_successor(after_abort=False)

    def test_base_successor_after_abort_does_not_repeat_abort(self):
        self._base_restart_successor(after_abort=True)

    def test_base_restart_rewrite_preserves_original_intent(self):
        original = self._pause_base_restart(after_abort=False)
        self.effects.git.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess([], 1, '')
        with self.assertRaisesRegex(RuntimeError, '후속 commit'):
            integration.restart_base_rebase(self.effects, self.connection, Path('/repo'), Path('/state'),
                                           'repo', self.plan, 'third-input', self.now)
        self.assertEqual(self.store.get('repo', 'G-166').snapshot['base_rebase_restart'], original)
        self.assertFalse(any(call.args[-2:] == ('rebase', '--abort')
                             for call in self.effects.git.call_args_list))


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
    def test_base_restart_refs_require_exact_project_and_pinned_oid(self):
        ref = 'refs/loop-manager/baselines/' + hashlib.sha256(b'repo').hexdigest()[:16] + '/' + 'a' * 64
        saved = {'base_rebase_restart': {'baseline': json.dumps({'ref': ref, 'ref_target': 'pinned'})}}
        self.assertEqual(integration.preserved_refs('repo', saved, {ref: ('pinned', '')}), frozenset({ref}))
        self.assertFalse(integration.preserved_refs('repo', saved, {ref: ('changed', '')}))
        self.assertFalse(integration.preserved_refs('other', saved, {ref: ('pinned', '')}))

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
                       'refs/remotes/origin/HEAD', 'refs/remotes/origin/loop-manager/G-166'}))

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

    def test_legacy_or_missing_cause_keeps_commit_isolation_route(self):
        self.effects.capture_git_snapshot.return_value = self.result
        for reason in (None, 'legacy_unscoped_block'):
            with self.subTest(reason=reason):
                self.plan['blocked_reason'] = reason
                self.effects.validate_agent_scope.reset_mock()
                self.validate()
                self.assertEqual(self.effects.validate_agent_scope.call_count, 1)
                self.assertEqual(self.effects.validate_agent_scope.call_args.args[0], 'worker')
                self.assertEqual(self.effects.validate_agent_scope.call_args.args[2].dirty, self.result.dirty)
        self.effects.git.assert_not_called()

    def test_diagnosed_result_keeps_scope_correction_without_user_normalization(self):
        self.effects.capture_git_snapshot.return_value = self.result
        self.plan['blocked_reason'] = 'plan_input_invalid'
        self.effects.validate_agent_scope.side_effect = [AgentScopeError('diagnosed commit'), None]
        self.validate()
        checks = self.effects.validate_agent_scope.call_args_list
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(check.args[0] == 'worker' for check in checks))
        self.assertEqual(checks[-1].args[2].dirty, self.result.dirty)
        self.effects.git.assert_not_called()

    def test_actual_agent_violation_is_not_absorbed_as_user_input(self):
        self.effects.validate_agent_scope.side_effect = AgentScopeError('actual Agent violation')
        with self.assertRaisesRegex(AgentScopeError, 'actual Agent'):
            self.validate(require_safe_result=True)
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
