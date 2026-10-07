"""차단 checkpoint의 자원 범위와 실행 격리 증거를 검증한다."""
from dataclasses import fields, replace
from pathlib import Path
import hashlib
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

import loop_manager as manager
from loop_manager_internal.agents import agent_scope_decision, scope_validation
from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence
from loop_manager_internal.application import project_service
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.recovery import blocked_checkpoint, checkpoint_adapter, project_block_service
from loop_manager_internal.storage import agent_invocation_state, blocked_checkpoint_state
from loop_manager_internal.documents.guidance_approval import GuidanceDocument
from tests.support.database import runtime_connection


class CheckpointScopeTests(TestCase):
    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.worktree = Path('/managed').resolve()
        self.branch = 'loop-manager/G-047'
        self.own_ref = f'refs/heads/{self.branch}'
        self.before = GitSnapshot('head', self.branch, {}, {},
            {self.own_ref: ('head', ''), 'refs/heads/B': ('old', '')},
            {str(self.worktree): (f'branch={self.own_ref}',), '/B': ('HEAD=old',)},
            {}, {}, scope_id='scope', scope_start_digest='digest')
        self.current = self.before
        self.resources = agent_scope_decision.used_git_resources(
            role='worker', phase='PLAN_BLOCKED', base_branch='main',
            branch=self.branch, worktree_key=str(self.worktree))
        self.connection.execute(
            "UPDATE projects SET phase='PLAN_BLOCKED', origin='owner/repo', "
            "plan_path='docs/plans/[G-047] Runtime.md', worktree=?, branch=?, "
            "agent_role='worker', agent_snapshot=?, worker_session_id='session'",
            (str(self.worktree), self.branch, self.before.serialize()))
        self.connection.execute(
            "UPDATE exec_plans SET status='PLAN_BLOCKED', branch=?, worktree=?, "
            "agent_role='worker', agent_snapshot=?, agent_result_snapshot=?, "
            "worker_session_id='session', guidance_plan_hash='document'",
            (self.branch, str(self.worktree), self.before.serialize(), self.before.serialize()))
        self.connection.commit()
        self.store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        self.request = self.store.request(repo='repo', plan_id='G-047',
            reason_code=manager.GUIDANCE_BLOCK_REASON, evidence='결정 대기',
            branch=self.branch, worktree=str(self.worktree), expected_remote_oid=None,
            rebase_recovery=False)
        self.connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES ('repo', 'G-047', 'plan', ?, ?)",
            (self.request.reason_code, self.request.evidence))
        self.connection.commit()
        self.effects = replace(manager._checkpoint_safety_effects(),
            git=lambda *_args, **_kwargs: SimpleNamespace(stdout='https://github.com/owner/repo', returncode=0),
            github_repository=lambda _: 'owner/repo',
            _proven_manager_base_refs=lambda *_: frozenset(),
            _proven_merged_cleanup_paths=lambda *_: frozenset(),
            _owned_checkpoint_remote_oid=lambda *_: None,
            _published_plan_allows_common_plans=lambda *_: False)
        self.adapter = checkpoint_adapter.BlockedCheckpointAdapter(
            self.connection, Path('repo'), Path('/state'), 'repo',
            safety_effects=lambda: self.effects,
            publication_effects=lambda: replace(manager._checkpoint_publication_effects(),
                capture_git_snapshot=lambda _: self.current))

    def plan(self):
        return self.connection.execute('SELECT * FROM exec_plans').fetchone()

    def test_stale_checkpoint_cause_refresh_cancel_and_competing_request(self):
        original = self.request
        self.connection.execute("UPDATE execplan_blocks SET reason_code='worker_failed', evidence='new cause'")
        self.connection.commit()
        updated = self.store.reconcile_cause(original)
        self.assertNotEqual(updated.request_identity, original.request_identity)
        self.assertEqual(updated.evidence, 'new cause')
        self.assertEqual(updated.expected_remote_oid, original.expected_remote_oid)
        self.assertEqual(updated.branch, original.branch)
        self.assertIsNone(self.store.reconcile_cause(original))
        row = self.connection.execute('SELECT publication_history FROM blocked_checkpoints').fetchone()
        self.assertIn(original.request_identity, row[0])
        self.connection.execute('DELETE FROM execplan_blocks')
        self.connection.commit()
        self.assertIsNone(self.store.reconcile_cause(updated))
        self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'unpublished')
        self.assertEqual(self.connection.execute('SELECT retry_count FROM exec_plans').fetchone()[0], 0)

    def test_checkpoint_accepts_manager_base_derived_head_and_rejects_target_change(self):
        for base in ('main', 'develop'):
            ref, head = 'refs/remotes/origin/' + base, 'refs/remotes/origin/HEAD'
            before = replace(self.before, refs={**self.before.refs, ref: ('a', ''), head: ('a', ref)})
            current = replace(before, refs={**before.refs, ref: ('b', ''), head: ('b', ref)})
            self.connection.execute('UPDATE exec_plans SET agent_snapshot=?', (before.serialize(),))
            self.effects = replace(self.effects, _proven_manager_base_refs=lambda *_: frozenset({ref}))
            with mock.patch.object(manager, '_agent_git_resources', return_value=None):
                self.assertIsNone(self.adapter._shared_safety_error(self.request, self.plan(), current))
                for altered in (('b', 'refs/heads/foreign'), ('foreign', ref)):
                    changed = replace(current, refs={**current.refs, head: altered})
                    self.assertEqual(self.adapter._shared_safety_error(self.request, self.plan(), changed).reason_code,
                                     'shared_ref_mutation')

    def test_dispatcher_rechecks_cause_before_safety_and_publication_completion(self):
        safety, publication, results = mock.Mock(), mock.Mock(), mock.Mock()
        safety.assess.return_value = blocked_checkpoint.SafetyVerdict(
            False, blocked_checkpoint.SafetyScope.PLAN, 'git_operation_in_progress', 'waiting', None, ())
        dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(self.store, safety, publication, results)
        self.connection.execute("UPDATE execplan_blocks SET evidence='current cause'")
        self.connection.commit()
        self.assertTrue(dispatcher._dispatch_one(self.request))
        inspected = safety.assess.call_args.args[0]
        self.assertEqual(inspected.evidence, 'current cause')
        self.assertNotEqual(inspected.request_identity, self.request.request_identity)
        results.promote_project_block.assert_not_called()
        publication.prepare.assert_not_called()
        self.connection.execute("UPDATE execplan_blocks SET evidence='changed after push'")
        self.connection.commit()
        dispatcher._finish_published(inspected, 'candidate')
        current = self.store.get('repo', 'G-047')
        self.assertEqual(current.phase.value, 'requested')
        self.assertEqual(current.evidence, 'changed after push')
        self.connection.execute('DELETE FROM execplan_blocks')
        self.connection.commit()
        safety.reset_mock()
        self.assertTrue(dispatcher._dispatch_one(current))
        safety.assess.assert_not_called()
        self.assertEqual(self.connection.execute('SELECT retry_count FROM exec_plans').fetchone()[0], 0)

    def test_cause_change_after_push_reuses_archived_proof_on_next_dispatch(self):
        from loop_manager_internal.recovery import checkpoint_service
        remote_ref = 'refs/remotes/origin/' + self.branch
        self.connection.execute("UPDATE blocked_checkpoints SET expected_remote_oid='old', remote_observed=1")
        original = self.store.get('repo', 'G-047')
        prepared = blocked_checkpoint.PreparedCheckpoint('head', 'tree', 'owned-identity', 'message', ())
        self.store.record_prepared(original, prepared)
        self.store.record_local(original, 'published')
        self.connection.execute("UPDATE execplan_blocks SET evidence='cause changed after push'")
        self.connection.commit()
        dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(self.store, mock.Mock(), mock.Mock(), mock.Mock())
        dispatcher._finish_published(self.store.get('repo', 'G-047'), 'published')
        refreshed = self.store.get('repo', 'G-047')
        self.assertIsNone(refreshed.local_oid)
        self.assertIsNone(refreshed.commit_identity)
        worker_result = replace(self.before, dirty={'src/app.py': (('staged',), 'worker-content')},
            index={'src/app.py': ('H 100644 worker-blob 0',)})
        self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (worker_result.serialize(),))
        self.connection.commit()
        current = replace(self.before, head='published',
            refs={**self.before.refs, self.own_ref: ('published', ''), remote_ref: ('published', '')})
        observed = {'tree': 'tree', 'message': 'Checkpoint-Identity: owned-identity', 'remote': 'published'}
        def git(_repo, *args, **kwargs):
            value = ('https://github.com/owner/repo' if args[0] == 'remote' else
                observed['tree'] if args[0] == 'rev-parse' else observed['message']
                if args[0] == 'show' else observed['remote'] + '\trefs/heads/' + self.branch)
            return SimpleNamespace(stdout=value, returncode=0)
        owned_effects = checkpoint_service.OwnedCheckpointRemoteOidEffects(git=git)
        owned = lambda connection, repo, request, plan, snapshot: checkpoint_service._owned_checkpoint_remote_oid(
            owned_effects, connection, repo, request, plan, snapshot)
        self.effects = replace(self.effects, _owned_checkpoint_remote_oid=owned, git=git,
            capture_git_snapshot=lambda _: current,
            validate_execplan_workspace_identity=lambda *_: None,
            validate_managed_worktree=lambda *_args, **_kwargs: None,
            _rebase_in_progress_for_branch=lambda *_: False)
        with mock.patch.object(manager, '_agent_git_resources', return_value=None):
            for key, wrong in (('tree', 'wrong'), ('message', 'forged'), ('remote', 'foreign')):
                previous = observed[key]
                observed[key] = wrong
                self.assertEqual(self.adapter._shared_safety_error(refreshed, self.plan(), current).reason_code,
                    'shared_ref_mutation')
                self.assertEqual(self.store.get('repo', 'G-047').expected_remote_oid, 'old')
                observed[key] = previous
            self.assertIsNone(self.adapter._shared_safety_error(refreshed, self.plan(), current))
        self.assertEqual(self.store.get('repo', 'G-047').expected_remote_oid, 'published')
        self.connection.execute("UPDATE blocked_checkpoints SET expected_remote_oid='competing'")
        competing = self.store.get('repo', 'G-047')
        self.store.accept_superseded_publication(competing, 'published')
        self.assertEqual(self.store.get('repo', 'G-047').expected_remote_oid, 'competing')
        self.connection.execute("UPDATE blocked_checkpoints SET expected_remote_oid='old'")
        self.connection.execute("UPDATE exec_plans SET worktree='/competing'")
        self.connection.commit()
        self.store.accept_superseded_publication(refreshed, 'published')
        self.assertEqual(self.store.get('repo', 'G-047').expected_remote_oid, 'old')
        self.connection.execute('UPDATE exec_plans SET worktree=?', (str(self.worktree),))
        # Replay the restart from the old lease, so dispatch must load the
        # lease accepted during its actual safety check before pushing.
        self.connection.execute("UPDATE blocked_checkpoints SET expected_remote_oid='old'")
        self.connection.commit()
        publication, results = mock.Mock(), mock.Mock()
        publication.existing_pull_request.return_value = None
        publication.prepare.return_value = blocked_checkpoint.PreparedCheckpoint(
            'published', 'new-tree', 'new-identity', 'new cause', ('src/app.py',))
        publication.ensure_local_commit.return_value = 'new-candidate'
        publication.observe_remote.side_effect = ['published', 'new-candidate']
        publication.is_fast_forward.return_value = True
        dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(self.store, self.adapter, publication, results)
        with (mock.patch.object(manager, '_agent_git_resources', return_value=None),
              mock.patch.object(manager, '_scope_snapshot_with_start_ignore_rules', side_effect=lambda _w, _b, a: a)):
            self.assertTrue(self.adapter.assess(refreshed).safe)
            archived = self.connection.execute('SELECT publication_history FROM blocked_checkpoints').fetchone()[0]
            self.connection.execute("UPDATE blocked_checkpoints SET publication_history='[]'")
            self.assertEqual(self.adapter.assess(refreshed).reason_code, 'shared_ref_mutation')
            self.assertFalse(self.adapter._validated_worker_checkpoint(refreshed, self.plan(), current))
            self.connection.execute('UPDATE blocked_checkpoints SET publication_history=?', (archived,))
            invalid_result = replace(worker_result, refs={**worker_result.refs, 'refs/heads/foreign': ('foreign', '')})
            self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (invalid_result.serialize(),))
            self.assertEqual(self.adapter.assess(refreshed).reason_code, 'agent_scope_recovery_pending')
            self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (worker_result.serialize(),))
            for key, value in (('tree', 'wrong'), ('message', 'forged')):
                original_value = observed[key]
                observed[key] = value
                self.assertFalse(self.adapter._validated_worker_checkpoint(refreshed, self.plan(), current))
                observed[key] = original_value
            for altered in (replace(current, dirty={'src/app.py': (('unstaged',), 'after-commit')}),
                            replace(current, head='foreign')):
                self.assertFalse(self.adapter._validated_worker_checkpoint(refreshed, self.plan(), altered))
            self.assertTrue(dispatcher._dispatch_one(refreshed))
        push = publication.push.call_args.args
        self.assertEqual(push[0].expected_remote_oid, 'published')
        self.assertEqual(push[2].mode, blocked_checkpoint.PushMode.FAST_FORWARD)
        self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'published')
        results.promote_project_block.assert_not_called()
        self.assertEqual(self.connection.execute('SELECT retry_count FROM exec_plans').fetchone()[0], 0)

    def test_checkpoint_ignores_unrelated_resources_only_with_invocation_proof(self):
        changes = (
            {'refs': {**self.before.refs, 'refs/heads/B': ('new', ''),
                      'refs/remotes/origin/B': ('new', '')}},
            {'worktrees': {**self.before.worktrees, '/B': ('HEAD=resolved',)}},
            {'config': {'local:branch.B.remote': ('origin',)}},
        )
        for values in changes:
            current = replace(self.before, **values)
            with self.subTest(values=values):
                with mock.patch.object(manager, '_agent_git_resources', return_value=self.resources):
                    self.assertIsNone(self.adapter._shared_safety_error(self.request, self.plan(), current))
                with mock.patch.object(manager, '_agent_git_resources', return_value=None):
                    self.assertIsNotNone(self.adapter._shared_safety_error(self.request, self.plan(), current))

    def test_used_remote_and_shared_config_still_refused(self):
        changes = (
            {'refs': {**self.before.refs, f'refs/remotes/origin/{self.branch}': ('foreign', '')}},
            {'refs': {**self.before.refs, 'refs/remotes/origin/main': ('foreign', '')}},
            {'config': {'local:core.hooksPath': ('foreign',)}},
        )
        with mock.patch.object(manager, '_agent_git_resources', return_value=self.resources):
            for values in changes:
                with self.subTest(values=values):
                    self.assertIsNotNone(self.adapter._shared_safety_error(
                        self.request, self.plan(), replace(self.before, **values)))

    def test_actual_selected_workspace_violations_still_stop_publication(self):
        self.effects = replace(self.effects, capture_git_snapshot=lambda _: self.current,
            validate_execplan_workspace_identity=lambda *_: None,
            validate_managed_worktree=lambda *_args, **_kwargs: None,
            _rebase_in_progress_for_branch=lambda *_: False)
        changes = (
            {'refs': {**self.before.refs, self.own_ref: ('foreign', '')}},
            {'worktrees': {str(self.worktree): ('branch=refs/heads/foreign',)}},
            {'operations': {'MERGE_HEAD': 'unresolved'}},
            {'branch': 'foreign'},
        )
        with (mock.patch.object(manager, '_agent_git_resources', return_value=self.resources),
              mock.patch.object(manager, '_scope_snapshot_with_start_ignore_rules', side_effect=lambda _w, _b, a: a)):
            self.assertTrue(self.adapter.assess(self.request).safe)
            for values in changes:
                with self.subTest(values=values):
                    self.current = replace(self.before, **values)
                    self.assertFalse(self.adapter.assess(self.request).safe)

    def test_worker_result_validation_uses_saved_invocation_context(self):
        result = replace(self.before, refs={**self.before.refs, 'refs/heads/B': ('new', '')})
        self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (result.serialize(),))
        with (mock.patch.object(manager, '_agent_git_resources', return_value=self.resources),
              mock.patch.object(manager, '_scope_snapshot_with_start_ignore_rules', side_effect=lambda _w, _b, a: a)):
            self.assertTrue(self.adapter._validated_worker_checkpoint(self.request, self.plan(), result))

    def test_manager_checkpoint_keeps_invocation_identity_for_restart(self):
        self.current = replace(self.before, head='checkpoint', scope_id=None, scope_start_digest=None)
        self.adapter._record_guidance_checkpoint_scope(self.request)
        stored = GitSnapshot.deserialize(self.plan()['agent_snapshot'])
        self.assertEqual(stored.head, 'checkpoint')
        self.assertEqual((stored.scope_id, stored.scope_start_digest), ('scope', 'digest'))
        self.assertEqual(self.plan()['worker_session_id'], 'session')
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')

    def test_saved_scope_rechecks_identity_including_when_another_plan_is_projected(self):
        before = replace(self.before, scope_start_digest='a' * 64)
        self.connection.execute('UPDATE exec_plans SET agent_snapshot=?', (before.serialize(),))
        self.connection.execute("UPDATE projects SET agent_snapshot=NULL, plan_path='other', worktree='/B'")
        ledger = agent_invocation_state.InvocationRepository(self.connection)
        identity = ledger.start(InvocationContext('repo', 'G-047'), 'worker', 'codex',
            ResolvedProfile('model', 'high'), 'session', scope_id=before.scope_id)
        common = Path('/common')
        repository_identity = hashlib.sha256((str(common) + '\0origin').encode()).hexdigest()
        ledger.record_isolation(identity, IsolationEvidence('codex', 'test', 'test', 'worker',
            str(self.worktree), repository_identity, 'b' * 64, 'verified',
            snapshot_digest='a' * 64, configuration_digest='c' * 64))
        ledger.finish(identity, 'success', 0, 1, 'session', Usage())
        managed = mock.Mock()
        effects = replace(manager._scope_validation_effects(),
            _git_common_directory=lambda _: common,
            git=lambda *_: SimpleNamespace(stdout='origin'),
            validate_managed_worktree=managed, _rebase_in_progress_for_branch=lambda *_: False)
        def resources():
            return scope_validation._agent_git_resources(self.connection, Path('repo'),
                self.worktree, before, 'worker', selected_plan_path=self.plan()['path'], effects=effects)
        self.assertEqual(resources(), self.resources)
        self.connection.execute("UPDATE exec_plans SET worker_session_id='changed-session'")
        self.assertIsNone(resources())
        self.connection.execute("UPDATE exec_plans SET worker_session_id='session'")
        managed.side_effect = manager.AgentScopeError('현재 A 소유권 위반')
        with self.assertRaises(manager.AgentScopeError):
            resources()

    def test_guidance_push_failure_restarts_pending_without_losing_scope(self):
        self.effects = replace(self.effects, capture_git_snapshot=lambda _: self.current,
            validate_execplan_workspace_identity=lambda *_: None,
            validate_managed_worktree=lambda *_args, **_kwargs: None,
            _rebase_in_progress_for_branch=lambda *_: False)
        prepared = blocked_checkpoint.PreparedCheckpoint('head', 'tree', 'identity', 'message', ())
        def commit(*_):
            self.current = replace(self.current, head='checkpoint',
                refs={**self.current.refs, self.own_ref: ('checkpoint', '')},
                scope_id=None, scope_start_digest=None)
            self.adapter._record_guidance_checkpoint_scope(self.request)
            return 'checkpoint'
        remote = [None]
        publication = mock.Mock()
        publication.existing_pull_request.return_value = None
        publication.prepare.return_value = prepared
        publication.ensure_local_commit.side_effect = commit
        publication.observe_remote.side_effect = lambda *_: remote[0]
        publication.push.side_effect = blocked_checkpoint.CheckpointPushError('일시적 push 실패', retryable=True)
        results = checkpoint_adapter._BlockedCheckpointResults(self.connection)
        def dispatch():
            return blocked_checkpoint.BlockedCheckpointDispatcher(
                self.store, self.adapter, publication, results).dispatch('repo')
        with (mock.patch.object(manager, '_agent_git_resources', return_value=self.resources),
              mock.patch.object(manager, '_scope_snapshot_with_start_ignore_rules', side_effect=lambda _w, _b, a: a)):
            self.assertEqual(dispatch(), 1)
            self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'retry_pending')
            self.assertEqual(publication.push.call_count, 3)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'restart.sqlite'
                with sqlite3.connect(path) as persisted:
                    self.connection.backup(persisted)
                persisted.close()
                self.connection.close()
                self.connection = sqlite3.connect(path)
                self.connection.row_factory = sqlite3.Row
                try:
                    self.adapter.connection = self.connection
                    self.store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
                    results = checkpoint_adapter._BlockedCheckpointResults(self.connection)
                    self.current = replace(self.current, refs={**self.current.refs, 'refs/heads/B': ('later', '')})
                    publication.push.side_effect = lambda *_: remote.__setitem__(0, 'checkpoint')
                    self.assertEqual(dispatch(), 1)
                    self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'published')
                    self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
                    self.assertIsNone(self.connection.execute('SELECT * FROM project_blocks').fetchone())
                    self.assertEqual(self.plan()['worker_session_id'], 'session')
                finally:
                    self.connection.close()


    def test_local_source_sync_failure_keeps_remote_catalog_available(self):
        for status in ('skipped', 'failed'):
            with self.subTest(status=status):
                sync = manager.BaseWorktreeSyncResult(status, '사용자 로컬 변경 보존', 'remote-sha')
                effects = SimpleNamespace(_recorded_base_fetch=mock.Mock(),
                    ensure_safe_base_worktree=mock.Mock(return_value=sync), log_base_worktree_sync=mock.Mock())
                service = SimpleNamespace(effects=effects,
                    identity=SimpleNamespace(connection=self.connection, state_dir=Path('/state')),
                    state=SimpleNamespace(repo='repo', repo_path=Path('repo'), base='main', base_sha=None),
                    _refresh_catalog=mock.Mock(return_value='remote-sha'), _archive_housekeeping=mock.Mock())
                project_service.ProjectService._synchronize_source_after_pr_reconciliation(service)
                self.assertEqual(service.state.base_sha, 'remote-sha')
                effects.log_base_worktree_sync.assert_called_once_with(Path('repo'), sync)
                self.assertIsNone(self.connection.execute('SELECT * FROM project_blocks').fetchone())
