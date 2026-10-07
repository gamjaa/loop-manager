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
            _guidance_rows=lambda *_: [{'invalidated_decision_hash': None}],
            _guidance_document=lambda *_: GuidanceDocument((), 'document'),
            _guidance_document_matches_rows=lambda *_args, **_kwargs: None,
            _published_plan_allows_common_plans=lambda *_: False)
        self.adapter = checkpoint_adapter.BlockedCheckpointAdapter(
            self.connection, Path('repo'), Path('/state'), 'repo',
            safety_effects=lambda: self.effects,
            publication_effects=lambda: replace(manager._checkpoint_publication_effects(),
                capture_git_snapshot=lambda _: self.current))

    def plan(self):
        return self.connection.execute('SELECT * FROM exec_plans').fetchone()

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

    def test_guidance_result_validation_uses_saved_invocation_context(self):
        self.effects = replace(self.effects,
            _guidance_document=lambda *_: GuidanceDocument((), 'new-format', ('document',)))
        result = replace(self.before, refs={**self.before.refs, 'refs/heads/B': ('new', '')})
        self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (result.serialize(),))
        with (mock.patch.object(manager, '_agent_git_resources', return_value=self.resources),
              mock.patch.object(manager, '_scope_snapshot_with_start_ignore_rules', side_effect=lambda _w, _b, a: a)):
            self.assertTrue(self.adapter._validated_guidance_checkpoint(self.request, self.plan(), result))

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

    def test_guidance_double_block_recovery_uses_the_same_resource_scope(self):
        self.connection.execute(
            "UPDATE exec_plans SET publication_remote_oid='head', blocked_reason=?",
            (manager.GUIDANCE_BLOCK_REASON,))
        self.connection.execute("UPDATE blocked_checkpoints SET phase='published', published_oid='head'")
        self.connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, evidence, plan_id, previous_phase) "
            "VALUES ('repo', 'project', 'shared_ref_mutation', 'B changed', 'G-047', 'PLAN_BLOCKED')")
        current = replace(self.before, refs={**self.before.refs, 'refs/heads/B': ('new', '')})
        effects = {field.name: mock.Mock() for field in fields(project_block_service.ReconcileGuidanceDoubleBlockEffects)}
        effects.update(_canonical_path=lambda path: path.resolve(),
            _guidance_checkpoint_commit_evidence=lambda *_: '결정 대기',
            _guidance_document=lambda *_: GuidanceDocument((), 'new-format', ('document',)),
            _guidance_rows=lambda *_: [{'applied': 0, 'invalidated_decision_hash': None,
                'decision_hash': None, 'target_path': 'AGENTS.md', 'baseline_content': b'rules', 'baseline_mode': 0o100644}],
            _guidance_document_matches_rows=lambda *_args, **_kwargs: ({}, ()),
            _guidance_target_state=lambda *_: (b'rules', 0o100644),
            _owned_checkpoint_remote_oid=lambda *_: 'head',
            _proven_manager_base_refs=lambda *_: frozenset(),
            _proven_merged_cleanup_paths=lambda *_: frozenset(),
            capture_git_snapshot=lambda _: current,
            git=lambda _repo, *args: SimpleNamespace(stdout='Checkpoint-Identity: identity' if args[0] == 'show' else 'tree'))
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        block = self.connection.execute('SELECT * FROM project_blocks').fetchone()
        # 실행 격리 없는 과거 snapshot은 같은 예외를 받을 수 없다.
        effects['_agent_git_resources'] = lambda *_args, **_kwargs: None
        def recover():
            return project_block_service._reconcile_guidance_double_block(
                project_block_service.ReconcileGuidanceDoubleBlockEffects(**effects),
                self.connection, Path('repo'), Path('/state'), 'repo', block, project, self.plan())
        self.assertFalse(recover())
        effects['_agent_git_resources'] = lambda *_args, **_kwargs: self.resources
        self.assertTrue(recover())
        self.assertIsNone(self.connection.execute('SELECT * FROM project_blocks').fetchone())
        self.assertEqual(self.plan()['blocked_reason'], manager.GUIDANCE_BLOCK_REASON)

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
