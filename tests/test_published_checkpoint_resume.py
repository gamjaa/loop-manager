"""게시 OID가 남은 재개의 실제 소유권·저장 판정; Git/PR 관측만 대체한다."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest import TestCase, mock

import loop_manager as lm
from loop_manager_internal.execution.execution_handlers import ExecutionHandlers
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.recovery import checkpoint_ownership, scoped_blocking, scope_reconciliation
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.storage import blocked_checkpoint_state, runtime_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.execution import ExecutionFixture


class PublishedCheckpointResumeTests(ExecutionFixture, TestCase):
    oid = 'a' * 40
    tree = 'b' * 40

    def setUp(self):
        super().setUp()
        self.checkpoints = ExecutionCheckpointRepository(self.connection)
        self.store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        self.request = self.store.request(repo=self.repo, plan_id='G-047',
            reason_code='verify_retry_exhausted', evidence='targeted unit test: FAIL',
            branch='loop-manager/G-047', worktree=str(Path('/state/G-047')),
            expected_remote_oid=None, rebase_recovery=False)
        self.connection.execute("UPDATE blocked_checkpoints SET phase='published', result='published', "
            "published_oid=?, accepted_remote_oid=?, local_oid=?, commit_identity='identity', tree_oid=?",
            (self.oid, self.oid, self.oid, self.tree))
        self.state.update_project(self.repo, publication_remote_oid=self.oid,
            worker_result='targeted unit test: FAIL', worker_session_id='saved-worker')
        self.checkpoints.record(self.repo, 'G-047', expected_generation=0,
            operation=None, expected_head=self.oid, target_base_oid=None,
            observed_remote_oid=self.oid, snapshot={})
        self.effects = replace(self.effects, checkpoint=self.checkpoints)
        self.handler.effects = self.effects
        self.local = self.oid
        self.live = self.oid
        self.dirty = False
        self.effects.workspace.git.side_effect = self.observe_git
        self.effects.workspace.remote_branch_oid.return_value = self.oid
        self.effects.publication.pr_for_branch.return_value = None
        self.connection.commit()

    def observe_git(self, path, *args, **kwargs):
        if args == ('rev-parse', 'HEAD') or args == ('rev-parse', 'refs/heads/loop-manager/G-047'):
            return SimpleNamespace(returncode=0, stdout=self.local + '\n')
        if args == ('rev-parse', self.oid + '^{tree}'):
            return SimpleNamespace(returncode=0, stdout=self.tree + '\n')
        if args == ('show', '-s', '--format=%B', self.oid):
            return SimpleNamespace(returncode=0,
                stdout='[G-047] BLOCKED checkpoint\nScope: plan\nCheckpoint-Identity: identity\n')
        if args[0] == 'ls-remote':
            return SimpleNamespace(returncode=0,
                stdout=f'{self.live}\trefs/heads/loop-manager/G-047\n' if self.live else '')
        return self.observed_git(path, *args, **kwargs)

    def owns(self):
        return self.handler._owned_checkpoint_remote(self.context(), self.state.row(), self.oid,
            self.checkpoints.get(self.repo, 'G-047'))

    def receipt(self):
        return dict(self.connection.execute('SELECT * FROM blocked_checkpoints').fetchone())

    def restart(self):
        self.connection.commit()
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.connection.backup(connection)
        self.addCleanup(connection.close)
        self.connection = connection
        self.repository = runtime_state.RuntimeStateRepository(connection)
        self.state = ExecutionStatePort(connection, self.repository, self.repo)
        self.checkpoints = ExecutionCheckpointRepository(connection)
        self.store = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        self.effects = replace(self.effects, state=self.state, checkpoint=self.checkpoints)
        self.handler = ExecutionHandlers(self.effects, repo=self.repo, repo_path=Path('/repo'),
            state_dir=Path('/state'), base='main', base_sha='base', provider='codex',
            config_path=Path('/config'), newly_activated=False)
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row(self.repo)

    def replace_request(self):
        request = self.store.request(repo=self.repo, plan_id='G-047',
            reason_code='verify_retry_exhausted', evidence='later failure',
            branch='loop-manager/G-047', worktree=str(Path('/state/G-047')),
            expected_remote_oid=self.oid, rebase_recovery=True)
        self.store.finish_unpublished(request, 'local-only preservation', retryable=False)
        return request

    def test_markerless_published_oid_revalidates_and_survives_restart(self):
        self.assertTrue(self.owns())
        self.assertTrue(any(call.args[1] == 'ls-remote'
                            for call in self.effects.workspace.git.call_args_list))
        self.handler.newly_activated = True
        self.assertEqual(self.handler.recover(self.context()).phase.value, 'IMPLEMENTING')
        saved = self.checkpoints.get(self.repo, 'G-047')
        self.assertEqual(saved.snapshot['published_review_resume']['owner_oid'], self.oid)
        self.assertNotIn('blocked_resume', saved.snapshot)
        self.assertNotIn('current_rebase_acceptance', saved.snapshot)
        self.restart()
        self.assertEqual(self.handler.recover(self.context()).phase.value, 'IMPLEMENTING')
        self.assertTrue(self.owns())
        self.assertEqual(self.state.row()['worker_result'], 'targeted unit test: FAIL')
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')
        self.effects.agent.run_agent.assert_not_called()
        self.effects.publication.command.assert_not_called()
        self.assertFalse({'commit', 'push'} & {call.args[1]
                         for call in self.effects.workspace.git.call_args_list})

    def test_historical_publication_is_read_without_rewriting_current_receipt(self):
        request = self.replace_request()
        original = self.receipt()
        self.assertEqual(original['phase'], 'unpublished')
        self.assertTrue(self.owns())
        self.assertEqual(self.handler.recover(self.context()).phase.value, 'IMPLEMENTING')
        self.assertEqual(self.receipt(), original)
        self.assertEqual(self.checkpoints.get(self.repo, 'G-047').snapshot[
            'published_review_resume']['request_identity'], request.request_identity)
        self.restart()
        self.assertTrue(self.owns())

    def test_legacy_flat_history_requires_the_recorded_lease(self):
        original = self.receipt()
        history = [{key: original[key] for key in ('published_oid', 'accepted_remote_oid',
            'commit_identity', 'tree_oid', 'identity_reason_code', 'identity_evidence')}]
        self.replace_request()
        self.connection.execute('UPDATE blocked_checkpoints SET publication_history=?',
            (json.dumps(history),))
        self.assertTrue(self.owns())
        self.connection.execute("UPDATE blocked_checkpoints SET expected_remote_oid='unrelated'")
        self.assertFalse(self.owns())

    def test_refresh_published_request_keeps_its_original_owner(self):
        self.store.refresh(self.request, expected_remote_oid=self.oid)
        original = self.receipt()
        self.assertEqual(original['phase'], 'requested')
        self.assertIsNone(original['result'])
        self.assertTrue(self.owns())
        self.assertEqual(self.receipt(), original)

    def test_history_for_another_repository_plan_or_workspace_is_refused(self):
        self.replace_request()
        history = json.loads(self.receipt()['publication_history'])
        for key in ('repo', 'plan_id', 'branch', 'worktree', 'pr_url'):
            with self.subTest(key=key):
                entry = {**history[0]['superseded_request'], key: 'unrelated'}
                self.connection.execute('UPDATE blocked_checkpoints SET publication_history=?',
                    (json.dumps([{'superseded_request': entry}]),))
                self.assertFalse(self.owns())

    def test_reblock_discards_old_resume_but_keeps_publication_for_reactivation(self):
        self.handler.recover(self.context())
        self.repository.block_plan(self.repo, 'G-047', reason_code='verify_retry_exhausted',
            evidence='new failure', release_fingerprint='new-input', clear_projection=True)
        self.assertNotIn('published_review_resume', self.checkpoints.get(self.repo, 'G-047').snapshot)
        self.assertEqual(json.loads(self.receipt()['publication_history'])[-1][
            'superseded_request']['accepted_remote_oid'], self.oid)
        # 원인 해제는 기존 정책의 입력이며 소유권 확인이 실패를 자동 해제하지 않는다.
        self.repository.update_plan(self.repo, 'G-047', plan_values={'status': 'READY'})
        self.repository.activate(self.repo, 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree=str(Path('/state/G-047')))
        self.restart()
        self.handler.newly_activated = True
        self.handler.catalog_plan = self.repository.active_row(self.repo)
        self.assertEqual(self.handler.recover(self.context()).phase.value, 'IMPLEMENTING')
        self.assertTrue(self.owns())

    def test_remote_change_deletion_and_unrecorded_local_commit_are_refused(self):
        for local, live in ((self.oid, 'c' * 40), (self.oid, None), ('c' * 40, self.oid)):
            with self.subTest(local=local, live=live):
                self.local, self.live = local, live
                self.assertFalse(self.owns())
        self.assertEqual(self.state.row()['publication_remote_oid'], self.oid)

    def test_identity_tree_lease_and_workspace_mismatch_are_refused(self):
        original = self.receipt()
        for column in ('branch', 'worktree', 'tree_oid', 'commit_identity', 'accepted_remote_oid',
                       'published_oid', 'pending_remote_oid'):
            with self.subTest(column=column):
                self.connection.execute(f'UPDATE blocked_checkpoints SET {column}=?', ('invalid',))
                self.assertFalse(self.owns())
                self.connection.execute(f'UPDATE blocked_checkpoints SET {column}=?', (original[column],))
        self.effects.workspace.validate_execplan_workspace_identity.side_effect = lm.WorkspaceOwnershipError('owner')
        self.assertFalse(self.owns())

    def test_stale_marker_and_unexpected_pr_are_refused(self):
        marker = {'request_identity': 'stale', 'owner_oid': self.oid, 'head': self.oid}
        current = self.checkpoints.get(self.repo, 'G-047')
        current = self.checkpoints.record(self.repo, 'G-047', expected_generation=current.generation,
            operation=None, expected_head=self.oid, target_base_oid=None,
            observed_remote_oid=self.oid, snapshot={'published_review_resume': marker})
        self.assertFalse(self.owns())
        marker['request_identity'] = self.request.request_identity
        for state in ('OPEN', 'CLOSED', 'MERGED'):
            with self.subTest(state=state):
                self.assertFalse(self.handler._owned_checkpoint_remote(self.context(), self.state.row(),
                    self.oid, replace(current, snapshot={'published_review_resume': marker}),
                    {'state': state, 'headRefOid': self.oid}))

    def test_observation_failure_retries_without_owner_acceptance(self):
        observe = self.observe_git
        def fail_remote(path, *args, **kwargs):
            if args[0] == 'ls-remote':
                raise RuntimeError('remote unavailable')
            return observe(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = fail_remote
        with self.assertRaises(RecoveryFailure) as failure:
            self.owns()
        self.assertIs(failure.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        result = self.handler.recover(self.context())
        self.assertIs(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertNotIn('published_review_resume', self.checkpoints.get(self.repo, 'G-047').snapshot)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_publication_change_during_observation_cannot_store_acceptance(self):
        observe = self.observe_git
        def race(path, *args, **kwargs):
            result = observe(path, *args, **kwargs)
            if args[0] == 'ls-remote':
                self.connection.execute("UPDATE blocked_checkpoints SET request_identity='changed'")
            return result
        self.effects.workspace.git.side_effect = race
        result = self.handler.recover(self.context())
        self.assertIs(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertNotIn('published_review_resume', self.checkpoints.get(self.repo, 'G-047').snapshot)

    def test_missing_or_corrupt_history_never_becomes_owned(self):
        self.replace_request()
        self.connection.execute("UPDATE blocked_checkpoints SET publication_history='[]'")
        self.assertFalse(self.owns())
        self.connection.execute('UPDATE blocked_checkpoints SET publication_history=?',
            (json.dumps([{'accepted_remote_oid': self.oid}]),))
        self.assertFalse(self.owns())
        for history in ('broken JSON', '{}', '[null]'):
            with self.subTest(history=history):
                self.connection.execute('UPDATE blocked_checkpoints SET publication_history=?', (history,))
                with self.assertRaises(RecoveryFailure) as failure:
                    self.owns()
                self.assertIs(failure.exception.reason, RecoveryReason.PUBLICATION_RETRY)

    def test_pr_observation_failure_does_not_mean_no_pr(self):
        self.effects.publication.pr_for_branch.side_effect = RuntimeError('PR unavailable')
        result = self.handler.recover(self.context())
        self.assertIs(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertNotIn('published_review_resume', self.checkpoints.get(self.repo, 'G-047').snapshot)

    def project_block(self, *, independent=False):
        if independent:
            self.repository.block_plan(self.repo, 'G-047', reason_code='verify_retry_exhausted',
                evidence='unresolved unit test', release_fingerprint='same-input')
        previous = 'PLAN_BLOCKED' if independent else 'WORKING'
        scoped_blocking.record_project_block(self.connection, self.repo,
            scoped_blocking.BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN, 'owner uncertain', None,
            plan_id='G-047', previous_phase=previous)
        self.connection.execute("UPDATE projects SET phase='PROJECT_BLOCKED'")
        self.connection.commit()

    def resume_project(self):
        branch = 'loop-manager/G-047'
        current = lm.GitSnapshot(self.local, branch, {}, {},
            {f'refs/heads/{branch}': (self.local, ''),
             f'refs/remotes/origin/{branch}': (self.live, '')}, {}, {}, {})
        effects = scope_reconciliation.ResumeOwnedCheckpointProjectBlockEffects(
            _clear_active_in_current_transaction=lm._clear_active_in_current_transaction,
            _direct_rebase_history_proven=lambda *_: False,
            _published_review_commit_intent_matches=lambda *_: False,
            capture_git_snapshot=lambda *_: current, git=self.observe_git,
            pr_for_branch=lambda *_: None, remote_branch_oid=lambda *_: self.live,
            validate_execplan_workspace_identity=self.effects.workspace.validate_execplan_workspace_identity,
            validate_managed_worktree=self.effects.workspace.validate_managed_worktree,
            validate_blocked_agent_scope=lm.validate_blocked_agent_scope,
            unmet_execplan_dependencies=lm.unmet_execplan_dependencies)
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        return scope_reconciliation._resume_owned_checkpoint_project_block(effects,
            self.connection, Path('/repo'), Path('/state'), self.repo,
            scoped_blocking.project_block(self.connection, self.repo), project, plan)

    def test_project_false_block_recovers_historical_owner_without_rebase(self):
        self.replace_request()
        original = self.receipt()
        self.project_block()
        self.assertTrue(self.resume_project())
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.receipt(), original)
        self.assertNotIn('current_rebase_acceptance', self.checkpoints.get(self.repo, 'G-047').snapshot)
        self.restart()
        self.assertTrue(self.owns())

    def test_project_release_preserves_independent_plan_failure_and_feedback(self):
        self.project_block(independent=True)
        original = dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-047'))
        self.assertTrue(self.resume_project())
        self.assertEqual(self.repository.get(self.repo, 'G-047').status, 'PLAN_BLOCKED')
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-047')), original)
        self.assertEqual(self.repository.get(self.repo, 'G-047').worker_result, 'targeted unit test: FAIL')

    def test_release_interruption_rolls_back_proof_and_cause_together(self):
        self.project_block()
        before = self.checkpoints.get(self.repo, 'G-047')
        clear = scoped_blocking.clear_project_block
        def interrupted(*args):
            clear(*args)
            raise KeyboardInterrupt('after cause release')
        with mock.patch.object(scoped_blocking, 'clear_project_block', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.resume_project()
        self.assertEqual(self.checkpoints.get(self.repo, 'G-047'), before)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        self.restart()
        self.assertTrue(self.resume_project())

    def test_project_generation_cause_and_publication_races_preserve_the_new_state(self):
        self.project_block()
        observe = self.observe_git
        for kind in ('generation', 'cause', 'publication', 'remote'):
            with self.subTest(kind=kind):
                changed = []
                def race(path, *args, **kwargs):
                    result = observe(path, *args, **kwargs)
                    if args[0] == 'ls-remote' and not changed:
                        changed.append(True)
                        if kind == 'generation':
                            current = self.checkpoints.get(self.repo, 'G-047')
                            self.checkpoints.record(self.repo, 'G-047',
                                expected_generation=current.generation, operation=None,
                                expected_head=current.expected_head, target_base_oid=None,
                                observed_remote_oid=current.observed_remote_oid, snapshot={'race': True})
                        elif kind == 'cause':
                            self.connection.execute("UPDATE project_blocks SET evidence='independent cause'")
                        elif kind == 'publication':
                            self.connection.execute("UPDATE blocked_checkpoints SET request_identity='changed'")
                        else:
                            self.live = 'c' * 40
                    return result
                self.observe_git = race
                self.assertFalse(self.resume_project())
                self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
                self.assertNotIn('published_review_resume', self.checkpoints.get(self.repo, 'G-047').snapshot)
                self.observe_git = observe
                self.live = self.oid

    def test_actual_saved_agent_violation_cannot_release_publication_block(self):
        self.project_block()
        before = lm.GitSnapshot(self.oid, 'loop-manager/G-047', {}, {}, {}, {}, {}, {})
        self.connection.execute("UPDATE exec_plans SET agent_role='reviewer', agent_snapshot=?, "
            "agent_result_snapshot=?, agent_recovery=NULL", (before.serialize(),
            replace(before, dirty={'app.py': (('unstaged',), 'forbidden write')}).serialize()))
        # 실물 범위 판정으로 Reviewer 쓰기를 거부하며 I/O snapshot만 제공한다.
        with (mock.patch.object(lm, 'capture_git_snapshot', return_value=before),
              mock.patch.object(lm, 'git', side_effect=self.observe_git),
              mock.patch.object(lm, 'validate_agent_scope', wraps=lm.validate_agent_scope) as scope,
              mock.patch.object(lm, 'validate_blocked_agent_scope',
                                wraps=lm.validate_blocked_agent_scope) as validation):
            self.assertFalse(self.resume_project())
        validation.assert_called_once()
        self.assertGreater(scope.call_count, 0)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
