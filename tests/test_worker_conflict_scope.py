import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager


class WorkerConflictScopeUnitTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.repo, self.worktree = Path('/repo').resolve(), Path('/managed').resolve()
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (str(self.repo),))
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status, branch, worktree, workspace_owned) VALUES "
            "(?, 'G-092', 'docs/plans/[G-092] Work.md', 'Work', '[]', '[]', 'normal', 'hash', "
            "'IMPLEMENTING', 'loop-manager/G-092', ?, 1)", (str(self.repo), str(self.worktree)))
        self.plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.before = loop_manager.GitSnapshot('partial', None,
            {'app.py': tuple(f'M 100644 old-{stage} {stage}' for stage in (1, 2, 3))}, {},
            {'refs/heads/loop-manager/G-092': ('before', '')},
            {str(self.worktree): ('HEAD=partial', 'detached=True')}, {}, {'rebase-merge': 'owned'})
        self.after = replace(self.before, index={'app.py': ('H 100644 resolved 0',)},
                             dirty={'app.py': (('staged',), 'resolved-file')})
        self.service = mock.Mock()
        self.service.require_owned.return_value = (None, SimpleNamespace(
            status=loop_manager.managed_rebase_validation.RebaseStatus.WAITING))
        for patcher in (
            mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.after),
            mock.patch.object(loop_manager, '_scope_snapshot_with_start_ignore_rules',
                              side_effect=lambda _worktree, _before, after: after),
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(loop_manager, '_current_rebase_service', return_value=self.service),
            mock.patch.object(loop_manager, 'validate_active_execplan_metadata', return_value={'id': 'G-092'}),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'git', side_effect=AssertionError('unit test has no Git writes')),
            mock.patch.object(loop_manager, 'git_bytes', return_value=b''),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def validate(self):
        return loop_manager.validate_worker_workspace_scope(
            self.repo, self.worktree, 'loop-manager/G-092', 'main', self.plan['path'],
            self.plan, self.before, allow_rebase=True, connection=self.connection)

    def test_pending_checkpoint_defers_final_declaration_checks_only_after_ownership(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        ExecutionCheckpointRepository(self.connection).record(
            str(self.repo), 'G-092', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base', observed_remote_oid='remote', snapshot={})
        self.service.require_owned.return_value = (SimpleNamespace(metadata={'onto': 'base'}), None)
        self.assertEqual(self.validate()['id'], 'G-092')
        loop_manager.validate_active_execplan_metadata.assert_not_called()
        self.service.require_owned.side_effect = RuntimeError('foreign remote')
        with self.assertRaises(loop_manager.WorkspaceOwnershipError):
            self.validate()

    def test_normal_stage_resolution_requires_and_uses_current_owned_rebase(self):
        self.assertEqual(self.validate(), {'id': 'G-092'})
        self.service.require_owned.assert_called_once()

    def test_worker_delta_preserves_manager_replay_documents_but_rejects_new_edits(self):
        from loop_manager_internal.agents.scope_contracts import AgentScopeError
        path = 'docs/plans/[G-265] Historical.md'
        self.before = replace(self.before,
            refs={**self.before.refs, 'refs/remotes/origin/main': ('latest-main', '')},
            index={**self.before.index, path: ('H 100644 historical 0',)},
            dirty={path: (('staged',), 'historical')})
        self.after = replace(self.after, refs=self.before.refs,
            index={**self.after.index, path: self.before.index[path]},
            dirty={**self.after.dirty, **self.before.dirty})
        self.assertEqual(self.validate(), {'id': 'G-092'})
        self.after = replace(self.after, dirty={**self.after.dirty, path: (('unstaged',), 'worker-edit')})
        with self.assertRaises(AgentScopeError):
            self.validate()

    def test_allow_rebase_alone_cannot_accept_uncertain_metadata_remote_or_scope(self):
        self.service.require_owned.side_effect = RuntimeError('current ownership or remote mismatch')
        with self.assertRaises(loop_manager.WorkspaceOwnershipError):
            self.validate()
        loop_manager.validate_active_execplan_metadata.assert_called_once()
        self.service.require_owned.assert_called_once()

    def test_flag_change_mixed_with_resolution_still_fails_after_owned_rebase(self):
        for tag in ('S', 'h'):
            self.after = replace(self.after, index={'app.py': (f'{tag} 100644 resolved 0',)})
            with self.subTest(tag=tag), self.assertRaises(loop_manager.WorkspaceOwnershipError):
                self.validate()

    def completed_rebase(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        ExecutionCheckpointRepository(self.connection).record(
            str(self.repo), 'G-092', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base', observed_remote_oid='remote', snapshot={})
        self.after = replace(self.after, head='completed', branch='loop-manager/G-092',
            dirty={}, operations={}, refs={'refs/heads/loop-manager/G-092': ('completed', '')},
            worktrees={str(self.worktree): ('HEAD=completed', 'branch=refs/heads/loop-manager/G-092')})
        self.completion_observation = SimpleNamespace(head='completed',
            repository_state_fingerprint=loop_manager.managed_rebase_validation.fingerprint({
                'refs': self.after.refs, 'worktrees': self.after.worktrees, 'config': self.after.config}))
        self.service.require_owned.return_value = (self.completion_observation, SimpleNamespace(
            status=loop_manager.managed_rebase_validation.RebaseStatus.COMPLETE))

    def test_worker_completed_owned_rebase_accepts_head_ref_index_and_operation_transition(self):
        self.completed_rebase()
        self.assertEqual(self.validate(), {'id': 'G-092'})
        self.service.require_owned.assert_called_once()

    def test_worker_completed_rebase_requires_current_ownership_and_remote_lease(self):
        self.completed_rebase()
        self.service.require_owned.side_effect = RuntimeError('remote or ownership differs')
        with self.assertRaises(loop_manager.WorkspaceOwnershipError):
            self.validate()

    def test_owned_completion_with_all_commits_skipped_accepts_unchanged_head(self):
        self.completed_rebase()
        self.before = replace(self.before, head='completed')
        self.assertEqual(self.validate(), {'id': 'G-092'})

    def test_completed_rebase_uses_real_current_result_policy(self):
        from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
        from loop_manager_internal.git.managed_rebase_validation import FileChange
        from tests.support.rebase_observations import expectation, observation, scope_for
        self.completed_rebase()
        expected = expectation(plan_id='G-092', plan_path=self.plan['path'],
            worktree=str(self.worktree), branch='loop-manager/G-092')
        expected = replace(expected, remote=replace(expected.remote, branch=expected.branch))
        observed = observation(expected, head=self.after.head,
            branch_ref=self.after.refs[f'refs/heads/{expected.branch}'],
            registration=self.after.worktrees[str(self.worktree)],
            repository_state_fingerprint=self.completion_observation.repository_state_fingerprint)
        service = ManagedRebaseService(expected, mock.Mock())
        with mock.patch.object(loop_manager, '_current_rebase_service', return_value=service), \
             mock.patch('loop_manager_internal.git.managed_rebase_adoption.observe_current_rebase',
                        return_value=(observed, scope_for(expected, observed))) as read:
            self.assertEqual(self.validate(), {'id': 'G-092'})
            for invalid in (
                replace(observed, base_is_ancestor=False),
                replace(observed, identity_verified=False),
                replace(observed, remote=replace(observed.remote, oid='foreign')),
                replace(observed, input_fingerprint='changed-input'),
                replace(observed, committed=(FileChange('docs/requirements/archive/Unexpected.md', '100644', '100644', 'old', 'new', 'M'),)),
                replace(observed, index_clean=False),
            ):
                with self.subTest(observed=invalid):
                    read.return_value = (invalid, scope_for(expected, invalid))
                    with self.assertRaises(loop_manager.WorkspaceOwnershipError):
                        self.validate()

    def test_saved_completion_is_accepted_after_rebase_adoption(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from tests.support.rebase_observations import expectation, observation, decision_for
        self.completed_rebase()
        expected = expectation(plan_id='G-092', plan_path=self.plan['path'],
            worktree=str(self.worktree), branch='loop-manager/G-092')
        expected = replace(expected, remote=replace(expected.remote, branch=expected.branch))
        observed = observation(expected, head=self.after.head, branch_ref=('completed', ''),
            registration=self.after.worktrees[str(self.worktree)])
        acceptance = decision_for(expected, observed).acceptance
        checkpoints = ExecutionCheckpointRepository(self.connection)
        current = checkpoints.get(str(self.repo), 'G-092')
        checkpoints.record(str(self.repo), 'G-092', expected_generation=current.generation,
            operation=None, expected_head='completed', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'current_rebase_acceptance': acceptance})
        before_serialized, after_serialized = self.before.serialize(), self.after.serialize()
        loop_manager.validate_agent_scope('worker', self.worktree,
            loop_manager.GitSnapshot.deserialize(before_serialized),
            loop_manager.GitSnapshot.deserialize(after_serialized),
            selected_plan_path=self.plan['path'], external_worktree=self.repo, connection=self.connection,
            owned_rebase=True)
        self.assertEqual(self.before.serialize(), before_serialized)
        self.assertEqual(self.after.serialize(), after_serialized)

    def test_blocked_scope_accepts_saved_worker_completion_without_erasing_snapshots(self):
        self.completed_rebase()
        before_serialized, after_serialized = self.before.serialize(), self.after.serialize()
        self.connection.execute("UPDATE exec_plans SET agent_role='worker', agent_snapshot=?, "
            "agent_result_snapshot=? WHERE plan_id='G-092'", (before_serialized, after_serialized))
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        with mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False), \
             mock.patch.object(loop_manager, 'requirement_archive_candidates', return_value=()), \
             mock.patch.object(loop_manager, '_proven_manager_base_refs', return_value=frozenset()), \
             mock.patch.object(loop_manager, '_proven_merged_cleanup_paths', return_value=frozenset()), \
             mock.patch.object(loop_manager, '_reconciled_manager_scope_metadata',
                               side_effect=lambda _c, _p, _s, _r, _id, value, *_args: value):
            loop_manager.validate_blocked_agent_scope(self.connection, self.repo, str(self.repo),
                'main', self.worktree, plan['path'], accepted_rebase=True, saved_plan=plan)
        saved = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(saved['agent_snapshot'], before_serialized)
        self.assertEqual(saved['agent_result_snapshot'], after_serialized)

    def test_worker_completed_rebase_cannot_accept_stale_saved_result_or_observation(self):
        self.completed_rebase()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=replace(self.after, head='later')):
            with self.subTest():
                loop_manager.validate_agent_scope('worker', self.worktree, self.before, self.after,
                    selected_plan_path=self.plan['path'], external_worktree=self.repo,
                    connection=self.connection)
        self.completion_observation.repository_state_fingerprint = 'older-state'
        self.validate()

    def test_completed_rebase_preserves_unrelated_metadata_checks(self):
        self.completed_rebase()
        completed = self.after
        for after in (
            replace(completed, refs={**completed.refs, 'refs/heads/foreign': ('foreign', '')}),
            replace(completed, worktrees={**completed.worktrees, '/other': ('HEAD=foreign',)}),
            replace(completed, config={'local:core.hooksPath': ('foreign',)}),
        ):
            with self.subTest(after=after):
                self.after = after
                self.completion_observation.repository_state_fingerprint = (
                    loop_manager.managed_rebase_validation.fingerprint({
                        'refs': after.refs, 'worktrees': after.worktrees, 'config': after.config}))
                with self.subTest():
                    self.validate()

    def test_completion_observation_race_preserves_head_violation(self):
        self.completed_rebase()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=(
                self.after, replace(self.after, index={'app.py': ('H 100644 raced 0',)}))):
            with self.subTest():
                loop_manager.validate_agent_scope('worker', self.worktree, self.before, self.after,
                    selected_plan_path=self.plan['path'], external_worktree=self.repo,
                    connection=self.connection)

    def test_completed_rebase_preserves_index_security_flag_changes(self):
        self.completed_rebase()
        original_before, original_after = self.before, self.after
        for before, after in (
            (original_before, replace(original_after, index={'app.py': ('S 100644 resolved 0',)})),
            (replace(original_before, index={'app.py': ('s 100644 old 0',)}), original_after),
        ):
            with self.subTest(before=before, after=after):
                self.before, self.after = before, after
                if loop_manager.agent_scope_decision._non_default_index_flags(after.index.get('app.py', ())):
                    with self.assertRaises(loop_manager.WorkspaceOwnershipError):
                        self.validate()
                else:
                    self.validate()

    def test_general_worker_and_reviewer_cannot_borrow_completed_rebase(self):
        self.completed_rebase()
        self.before = replace(self.before, branch='loop-manager/G-092', operations={})
        with self.subTest():
            self.validate()
        self.before = replace(self.before, branch=None, operations={'rebase-merge': 'owned'})
        with self.subTest():
            loop_manager.validate_agent_scope('reviewer', self.worktree, self.before, self.after,
                selected_plan_path=self.plan['path'], external_worktree=self.repo,
                connection=self.connection)
