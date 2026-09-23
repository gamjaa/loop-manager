"""Run the real phase policies with in-memory state and fake external effects."""
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import execution_handlers as handlers
from execution_revision import DefinitionSnapshot
import loop_manager as lm
import runtime_state
from execution_checkpoint_state import CheckpointRecord, ExecutionCheckpointRepository
from requirement_archive import ArchiveSnapshot
from execution_state import ExecutionStatePort
from execution_runtime import (
    ExecutionPhase,
    ExecutionPhaseCoordinator,
    ExecutionOutcome,
    ExecutionOutcomeKind,
)
from tests import test_runtime_state


class ExecutionHandlerTests(TestCase):
    def setUp(self):
        self.connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(self.connection.close)
        self.repository = runtime_state.RuntimeStateRepository(self.connection)
        self.repository.activate('repo', 'G-047', activation_sha='base',
                                 branch='loop-manager/G-047', worktree='/state/G-047')
        self.state = ExecutionStatePort(self.connection, self.repository, 'repo')
        self.plan = {'id': 'G-047', 'title': 'Runtime', 'sources': ['docs/PRODUCT.md']}
        self.calls = []
        self.remote = 'base'
        self.dirty = True

        def bundle(cls):
            return cls(**{f.name: mock.Mock(name=f.name, return_value=None)
                          for f in fields(cls)})

        self.effects = handlers.ExecutionEffects(
            state=self.state, contract=lm,
            workspace=bundle(handlers.WorkspaceEffects),
            agent=bundle(handlers.AgentEffects),
            validation=bundle(handlers.ValidationEffects),
            archive=bundle(handlers.ArchiveEffects),
            blocking=bundle(handlers.BlockingEffects),
            publication=bundle(handlers.PublicationEffects),
            events=bundle(handlers.EventsEffects),
            revision=bundle(handlers.RevisionEffects),
        )
        self.effects.workspace.is_directory.return_value = False
        self.effects.workspace.branch_has_changes.return_value = True
        self.effects.workspace.git.side_effect = self.git
        self.effects.agent.validate_worker_workspace_scope.return_value = self.plan
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('staged',), 'accepted-content')},
        )
        self.effects.validation.validate_active_execplan_metadata.return_value = self.plan
        self.effects.validation._deserialize_failure_history.return_value = {}
        self.effects.validation.parse_worker_result.return_value = SimpleNamespace(commit_title='Concrete change')
        self.effects.validation.verify.side_effect = lambda _: (self.calls.append('verify') or True, 'OK')
        self.effects.validation.parse_reviewer_result.return_value = ('PASS', ())
        self.effects.agent.run_agent_with_retries.side_effect = lambda call, session_id: call(session_id, False)
        self.effects.agent.run_agent.side_effect = self.agent
        self.effects.publication.published_execplan.return_value = self.plan
        self.effects.publication.push_managed_publication_branch.side_effect = lambda *a: self.calls.append('push') or 'head'
        self.effects.publication.command.side_effect = lambda *a, **k: self.calls.append('pr') or SimpleNamespace(stdout='https://example/pr/1')
        self.effects.revision.reconcile_catalog.side_effect = lambda *_: SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha=self.remote
        )
        self.effects.revision.body_hash.return_value = 'body'
        self.effects.revision.source_fingerprint.return_value = 'source'
        self.effects.revision.changed_paths.return_value = ()
        self.effects.revision.unmet_dependencies.return_value = ()
        self.effects.archive.requirement_archive_sources.return_value = ()
        self.handler = handlers.ExecutionHandlers(
            self.effects, repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')

    def git(self, path, *args, **kwargs):
        self.calls.append(args[0])
        if args[0] == 'rebase' and '--skip' not in args:
            self.assertEqual(self.state.row()['last_error'], lm.REBASE_IN_PROGRESS_ERROR)
        if 'rebase' in args and ('--continue' in args or '--skip' in args):
            # The fake command succeeds, so its operation metadata must be
            # cleared just as a completed real rebase would be.
            self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        if args[0] == 'commit':
            self.dirty = False
        output = self.remote if args[0] == 'rev-parse' else ''
        if args[0] == 'status' and self.dirty:
            output = ' M app.py'
        return SimpleNamespace(stdout=output, returncode=0)

    def agent(self, *args, **kwargs):
        self.calls.append(args[4])
        kwargs['on_session_started']('session')
        return lm.AgentResult(0, 'result', session_id='session')

    def context(self, phase=None):
        if phase is not None:
            self.state.update_project('repo', phase=phase)
        return self.handler._context()

    def observed_git(self, path, *args, **kwargs):
        if args[0] == 'symbolic-ref':
            return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
        if args[0] == 'ls-files':
            return SimpleNamespace(stdout='', returncode=0)
        if args[:2] == ('diff', '--cached'):
            return SimpleNamespace(stdout='', returncode=0)
        if args[0] == 'merge-base':
            return SimpleNamespace(stdout='', returncode=0)
        if args == ('rev-parse', 'HEAD'):
            return SimpleNamespace(stdout='owned-head\n', returncode=0)
        return self.git(path, *args, **kwargs)

    def _use_runtime_contract(self):
        runtime = lm._ExecutionLifecyclePorts(
            self.connection, self.repository,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.effects = replace(self.effects, contract=runtime.effects.contract)
        self.handler.effects = self.effects

    def test_zero_exit_rebase_with_unmerged_index_keeps_conflict_correction(self):
        checkpoint = mock.Mock()
        checkpoint.get.return_value = CheckpointRecord(
            generation=1, operation='rebase', expected_head='before',
            target_base_oid='new-base', snapshot={'staged_paths': ['app.py']},
        )
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        original_git = self.git

        def conflicted_git(path, *args, **kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(
                    stdout='100644 ' + 'a' * 40 + ' 1\tapp.py\0',
                    returncode=0,
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = conflicted_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        context = self.context('VERIFYING')
        before = self.state.row()['active_sha']

        result = self.handler._post_rebase_observation(
            context, 'new-base', 'Created autostash: deadbee\nconflicts',
        )

        self.assertEqual(result.recovery.observation.reason.value, 'rebase_conflict')
        self.assertEqual(self.state.row()['active_sha'], before)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        saved = checkpoint.record.call_args.kwargs['snapshot']
        self.assertIn('100644 ' + 'a' * 40, saved['unmerged_index'])

    def test_publication_intent_records_expected_head_and_completes(self):
        checkpoint = mock.Mock()
        checkpoint.get.side_effect = [
            CheckpointRecord(generation=1),
            CheckpointRecord(generation=2, operation='push',
                             expected_head='base', target_base_oid='base',
                             observed_remote_oid='old'),
        ]
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        context = self.context('PUBLISHING')

        self.handler._publication_intent(context, 'push', 'old')
        self.assertEqual(checkpoint.record.call_args_list[0].kwargs['operation'], 'push')
        self.assertEqual(checkpoint.record.call_args_list[0].kwargs['expected_head'], 'base')
        self.handler._complete_publication_intent(context)
        self.assertIsNone(checkpoint.record.call_args_list[1].kwargs['operation'])

    def test_preparation_recovery_corrects_owned_stale_markers_before_verify(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='old-base',
            validated_base_sha='old-base', worker_session_id='worker-session',
            verify_result='PASS', review_result='PASS',
        )
        self.remote = 'new-base'

        def observed_git(path, *args, **kwargs):
            if args[0] == 'symbolic-ref':
                return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='', returncode=0)
            if args[:2] == ('diff', '--cached'):
                return SimpleNamespace(stdout='', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='owned-head\n', returncode=0)
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        context = self.context()
        recovered = self.handler.recover(context)

        self.assertEqual(recovered.phase, ExecutionPhase.VERIFYING)
        row = self.state.row()
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.effects.validation.verify.assert_not_called()

    def test_preparation_leaves_new_rebase_to_verify_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='saved worker output',
            worker_session_id='worker-session',
        )
        self.remote = 'new-base'

        def stale_base_git(path, *args, **kwargs):
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = stale_base_git
        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_result'], 'saved worker output')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.assertNotIn('rebase', [call.args[1] for call in self.effects.workspace.git.call_args_list])
        self.effects.publication.pr_for_branch.assert_not_called()

    def test_preparation_preserves_existing_rebase_correction_diagnostic(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        message = lm.REBASE_CONFLICT_PREFIX + 'Worker 결과 검증 실패: actionable output'
        self.state.update_project(
            'repo', phase='WORKING', last_error=message,
            worker_session_id='worker-session',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertTrue(recovered.resolving_rebase)
        self.assertEqual(self.state.row()['last_error'], message)
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')

    def test_review_failure_checkpoint_resumes_worker_without_pr_lookup(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='base',
            validated_base_sha=None, worker_session_id='worker-session',
            worker_result='previous worker result',
            review_result='VERDICT: FAIL', last_error='Review 실패: fix issue',
        )
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.state.row()['last_error'], 'Review 실패: fix issue')
        self.assertEqual(self.state.row()['worker_result'], 'previous worker result')
        self.effects.publication.pr_for_branch.assert_not_called()

    def test_new_activation_drops_old_execution_head_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.handler.newly_activated = True
        checkpoint.record(
            'repo', 'G-047', expected_generation=0,
            operation=None, expected_head='old-execution-head',
            target_base_oid='old-base', observed_remote_oid=None,
            snapshot={'old_phase': 'PUBLISHING'},
        )
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertEqual(saved.snapshot['reactivated_from_head'],
                         'old-execution-head')

    def test_external_remote_checkpoint_uses_publication_ownership_cause(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'external-head'

        result = self.handler.recover(self.context('PUBLISHING'))

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.REMOTE_OID_UNCERTAIN)
        self.assertIn('갱신을 거부', result.recovery.observation.detail)

    def test_preparation_resumes_durable_rebase_continue_without_worker(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='old-base',
            validated_base_sha=None, worker_result='saved correction',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='old-head', target_base_oid='new-base',
            observed_remote_oid=None,
            snapshot={'pending': 'continue', 'worker_result': 'saved correction'},
        )
        self.remote = 'new-base'
        operation = {'rebase': True}

        def observed_git(path, *args, **kwargs):
            if args[0] == 'symbolic-ref':
                return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='', returncode=0)
            if args[:2] == ('diff', '--cached'):
                return SimpleNamespace(stdout='', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='owned-head\n', returncode=0)
            if args[:2] == ('rebase', '--skip'):
                operation['rebase'] = False
                return SimpleNamespace(stdout='completed', returncode=0)
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace._rebase_in_progress_for_branch.side_effect = (
            lambda *_: operation['rebase']
        )
        context = self.context()

        recovered = self.handler.recover(context)

        self.assertEqual(recovered.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_result'], 'saved correction')
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.effects.agent.run_agent.assert_not_called()

    def test_resolved_rebase_then_verify_failure_returns_to_same_worker(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='base',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session', worker_result='old result',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before-head', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git

        first = self.handler.run(self.context())
        self.assertEqual(first.context.phase, ExecutionPhase.VERIFYING)
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['stage_evidence'], {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'app.py': 'accepted-content'},
        })

        self.effects.validation.verify.side_effect = lambda _: (False, 'verify failure')

        def fail_verify(_repo, _worktree, _stage, output):
            self.state.update_project(
                'repo', phase='WORKING', validated_base_sha=None,
                verify_result=output, last_error='전체 검증 실패: ' + output,
            )
            return SimpleNamespace(blocked=False, fingerprint='verify-failure')

        self.effects.validation.transition_validation_failure.side_effect = fail_verify
        verified = self.handler.verify(first.context, self.handler)
        self.assertEqual(verified.kind, ExecutionOutcomeKind.RETRY)
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'stop after same-run verify recovery', session_id='worker-session',
        )

        recovered = self.handler.recover(self.handler._context())
        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        second = self.handler.run(recovered)

        self.assertEqual(second.kind, ExecutionOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(self.state.row()['last_error'],
                         'stop after same-run verify recovery')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')

    def test_staged_rebase_resume_reuses_saved_worker_result(self):
        self._assert_staged_rebase_resume(True)

    def test_staged_autostash_resume_reuses_saved_worker_result(self):
        self._assert_staged_rebase_resume(False)

    def test_staged_rebase_resume_rejects_added_files(self):
        self._assert_staged_rebase_resume(True, changed=True)

    def _assert_staged_rebase_resume(self, rebase_metadata, *, changed=False):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before-head', target_base_oid='base',
            observed_remote_oid=None,
            snapshot={'pending': 'stage', 'worker_result': 'saved correction',
                      'stage_evidence': {'head': 'owned-head', 'branch': 'loop-manager/G-047',
                                         'files': {'app.py': 'accepted-content'}}},
        )
        if changed:
            self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
                head='owned-head', branch='loop-manager/G-047',
                dirty={'app.py': (('staged',), 'accepted-content'),
                       'extra.py': (('untracked',), 'unexpected-content')},
            )
        def staged_git(path, *args, **kwargs):
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            return self.observed_git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = staged_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = rebase_metadata
        result = self.handler.recover(self.context())
        self.effects.agent.run_agent.assert_not_called()
        self.assertFalse(any(call.args[1:3] == ('add', '-A')
                             for call in self.effects.workspace.git.call_args_list))
        if changed:
            self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
            self.assertEqual(checkpoint.get('repo', 'G-047').operation, 'rebase')
            self.assertFalse(any('--continue' in call.args
                                 for call in self.effects.workspace.git.call_args_list))
            return
        self.assertEqual(result.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_result'], 'saved correction')
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.effects.agent.run_agent.assert_not_called()
        continuations = [call for call in self.effects.workspace.git.call_args_list
                         if '--continue' in call.args]
        self.assertEqual(len(continuations), int(rebase_metadata))

    def test_autostash_without_identity_blocks_after_command_and_on_resume(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before-head', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        def conflicted_git(path, *args, **kwargs):
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='100644 blob 1\tapp.py\0', returncode=0)
            return self.observed_git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = conflicted_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        context = self.context('VERIFYING')
        for result in (
            self.handler._post_rebase_observation(context, 'base', 'completed'),
            self.handler.recover(context),
        ):
            self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
            self.assertEqual(result.detail, 'autostash_identity_unavailable')
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.assertEqual(checkpoint.get('repo', 'G-047').operation, 'rebase')
        self.assertEqual(self.effects.blocking.block_active_execplan.call_count, 2)
        for call in self.effects.blocking.block_active_execplan.call_args_list:
            self.assertEqual(call.kwargs['reason'], lm.BlockReasonCode.REPOSITORY_STATE_UNSAFE)

    def test_old_pr_head_after_rebase_cannot_attach_or_push(self):
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'existing', 'headRefOid': 'old',
            'headRefName': 'loop-manager/G-047', 'state': 'OPEN',
        }
        self.effects.workspace.remote_branch_oid.return_value = 'old'
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.reconcile_existing_pull_request.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()

    def test_post_push_pr_with_different_head_is_not_attached(self):
        self._assert_post_push_pr_attachment('other-head', False)

    def test_post_push_pr_with_verified_head_is_attached(self):
        self._assert_post_push_pr_attachment('base', True)

    def _assert_post_push_pr_attachment(self, pr_head, accepted):
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.push_managed_publication_branch.side_effect = (
            lambda *_: self.calls.append('push') or 'base'
        )
        self.effects.workspace.remote_branch_oid.side_effect = (
            lambda *_: 'base' if 'push' in self.calls else None
        )
        self.effects.publication.pr_for_branch.side_effect = lambda *_: (
            {'url': 'existing', 'state': 'OPEN', 'headRefOid': pr_head,
             'headRefName': 'loop-manager/G-047'} if 'push' in self.calls else None
        )
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS if accepted
                         else ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.effects.publication.reconcile_existing_pull_request.call_count,
                         int(accepted))
        self.effects.publication.push_managed_publication_branch.assert_called_once()
        self.effects.publication.command.assert_not_called()

    def test_checkpoint_fetch_lock_preserves_intent_and_worker_session(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project('repo', worker_session_id='worker-session')
        self.effects.workspace.git.side_effect = lm.GitLockError('index.lock')

        result = self.handler.recover(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'git_lock')
        self.assertTrue(checkpoint.get('repo', 'G-047').snapshot['fetch_in_progress'])
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_worktree_recovery_failure_keeps_durable_pending_boundary(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary worktree failure')
        self.state.update_project('repo', worker_session_id='worker-session')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = checkpoint.get('repo', 'G-047').snapshot
        self.assertTrue(saved['worktree_recovery_pending'])
        self.assertEqual(saved['branch'], 'loop-manager/G-047')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_commit_command_failure_preserves_expected_head_intent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects

        def failing_git(path, *args, **kwargs):
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='before-head\n', returncode=0)
            if args[0] == 'commit':
                raise lm.GitLockError('index.lock')
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = failing_git
        with self.assertRaises(lm.GitLockError):
            self.handler._commit_owned(self.context('VERIFYING'), 'change')
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.operation, 'commit')
        self.assertEqual(saved.expected_head, 'before-head')
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')

    def test_final_block_uses_current_verify_output_before_project_checkpoint(self):
        result = self.handler._block_final(
            'repo', Path('/state/G-047'), 'no publishable change',
            'verify: PASS', reason=lm.BlockReasonCode.PLAN_INPUT_INVALID,
            recovery_reason=handlers.RecoveryReason.NO_CHANGE,
        )
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.args[3],
            'verify: PASS',
        )

    def test_real_handlers_complete_each_phase_and_reject_replay(self):
        context = self.context()
        for method in ('run', 'verify', 'review', 'publish'):
            old = context
            call = getattr(self.handler, method)
            args = (old, self.handler) if method in {'verify', 'publish'} else (old,)
            result = call(*args)
            context = result.context
            before = list(self.calls)
            with self.assertRaises(runtime_state.RuntimeStateError):
                call(*args)
            self.assertEqual(self.calls, before)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PR_OPEN')
        order = [c for c in self.calls if c in {'worker', 'verify', 'commit', 'reviewer', 'push', 'pr'}]
        self.assertEqual(order, ['worker', 'verify', 'commit', 'reviewer', 'push', 'pr'])

    def test_revision_guard_tokens_follow_complete_phase_lifecycle(self):
        context = self.handler.guard(self.context())
        self.assertNotIsInstance(context, handlers.ExecutionOutcome)

        for method in ('run', 'verify', 'review', 'publish'):
            call = getattr(self.handler, method)
            args = (context, self.handler) if method in {'verify', 'publish'} else (context,)
            result = call(*args)
            context = result.context

        completed = self.repository.get('repo', 'G-047')
        self.assertEqual(completed.status, 'PR_OPEN')
        self.assertIsNotNone(completed.definition_snapshot)
        self.assertIsNotNone(completed.worker_input_token)
        self.assertIsNotNone(completed.archive_token)
        self.assertIsNotNone(completed.verify_token)
        self.assertIsNotNone(completed.review_token)
        self.assertIsNotNone(completed.publication_token)

    def test_latest_base_uses_same_intent_and_rebase_for_both_checkpoints(self):
        for phase in ('VERIFYING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base')
                self.remote = 'new-base'
                self.calls.clear()
                context = self.context()
                if phase == 'VERIFYING':
                    result = self.handler.verify(context, self.handler)
                    self.assertEqual(result.context.phase, ExecutionPhase.REVIEWING)
                    self.assertEqual(self.calls.count('verify'), 2)
                else:
                    result = self.handler.publish(context, self.handler)
                    self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                    self.assertEqual(self.calls.count('verify'), 0)
                    self.assertIsNone(self.state.row()['review_result'])
                self.assertEqual(self.calls.count('rebase'), 1)
                self.assertEqual(self.state.row()['active_sha'], 'new-base')

    def test_stale_callback_cannot_update_same_phase_new_snapshot(self):
        context = self.context()
        self.repository.update_project('repo', {'worker_result': 'newer observation'})
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.state.update_project('repo', worker_session_id='stale')
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.handler.run(context)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.repository.get('repo', 'G-047').worker_session_id)

    def test_source_revision_invalidates_artifacts_but_preserves_sessions_and_workspace(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        snapshot = DefinitionSnapshot.deserialize(
            self.state.row()['definition_snapshot']
        )
        assert snapshot is not None
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_session_id='worker-session',
            reviewer_session_id='reviewer-session',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            worker_input_token=snapshot.worker_token,
            archive_token=snapshot.archive_token,
            verify_token=snapshot.verify_token('base'),
            review_token=snapshot.review_token('base'),
            publication_token=snapshot.publication_token('base'),
        )
        self.effects.revision.source_fingerprint.return_value = 'changed-source'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['reviewer_session_id'], 'reviewer-session')
        self.assertEqual(row['branch'], 'loop-manager/G-047')
        self.assertEqual(row['worktree'], '/state/G-047')

    def test_working_body_change_clears_retry_artifacts_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            worker_session_id='worker-session',
            worker_result='previous worker result',
            verify_result='failed verify result',
            review_result='previous review result',
            validated_base_sha='base',
        )
        self.effects.revision.body_hash.return_value = 'revised-body'

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, ExecutionOutcome)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertIsNone(row['validated_base_sha'])
        self.assertEqual(row['worker_session_id'], 'worker-session')

    def test_working_source_change_clears_retry_artifacts_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            worker_result='previous worker result',
            verify_result='failed verify result',
            worker_input_token='stale-worker-token',
        )
        self.effects.revision.source_fingerprint.return_value = 'revised-source'

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, ExecutionOutcome)
        row = self.state.row()
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['worker_input_token'])

    def _conflict_revision_setup(self):
        self.handler.guard(self.context())
        workspace_plan = self.handler._cached_plan(self.handler.catalog_plan)
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.validate_worker_workspace_scope
        )
        def resume_agent(*args, **kwargs):
            self.calls.append(args[4])
            session = kwargs['session_id'] or 'session'
            kwargs['on_session_started'](session)
            return lm.AgentResult(0, 'result', session_id=session)
        self.effects.agent.run_agent.side_effect = resume_agent
        return workspace_plan

    def _dispatch_conflict_revision(self, workspace_plan):
        # Exercise the real workspace metadata comparison, faking only Git and
        # file parsing. A constant validator result would hide stale metadata.
        with mock.patch('loop_manager.capture_git_snapshot'), mock.patch(
            'loop_manager.validate_agent_scope'
        ), mock.patch('loop_manager.validate_managed_worktree'), mock.patch(
            'loop_manager.validate_execplan', return_value=workspace_plan
        ):
            return ExecutionPhaseCoordinator(self.handler).dispatch(
                self.handler._context()
            )

    def test_conflict_revision_dependency_merge_resumes_worker(self):
        workspace_plan = self._conflict_revision_setup()
        applied = self.state.row()['definition_snapshot']
        for plan_id, dependencies in (('G-048', '["G-050"]'), ('G-050', '[]')):
            self.connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "('repo', ?, ?, 'Dependency', '[\"docs/PRODUCT.md\"]', ?, "
                "'normal', 'hash', 'READY')",
                (plan_id, f'docs/plans/[{plan_id}] Dependency.md', dependencies),
            )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on = '[\"G-048\"]' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        self.remote = 'new-base'
        self.effects.revision.unmet_dependencies.side_effect = (
            lambda repo, plan_id: lm.unmet_execplan_dependencies(
                self.connection, repo, plan_id
            )
        )

        outcome = ExecutionPhaseCoordinator(self.handler).dispatch(
            self.handler._context()
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.STABLE_WAIT)
        self.assertIn('G-048', outcome.detail)
        self.assertIn('G-050', outcome.detail)
        self.effects.agent.run_agent.assert_not_called()
        self.assertNotIn('rebase', self.calls)
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'previous worker result')
        self.assertTrue(row['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = 'repo' AND plan_id IN ('G-048', 'G-050')"
        )
        self.connection.commit()
        resumed = self._dispatch_conflict_revision(workspace_plan)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertEqual(
            self.effects.agent.run_agent.call_args.kwargs['session_id'],
            'worker-session',
        )
        self.assertIsNone(self.effects.agent.worker_prompt.call_args.args[2])
        self.assertEqual(self.state.row()['definition_snapshot'], applied)
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertEqual(self.state.row()['branch'], 'loop-manager/G-047')
        self.assertEqual(self.state.row()['worktree'], '/state/G-047')
        self.effects.validation.verify.assert_not_called()
        latest_plan = self.handler._cached_plan(self.handler.catalog_plan)
        rerun = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(rerun.context.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        verified = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING)
        reviewed = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(reviewed.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(
            [call for call in self.calls if call in {'worker', 'verify', 'reviewer'}],
            ['worker', 'worker', 'verify', 'reviewer'],
        )

    def test_conflict_revision_priority_change_allows_old_workspace_metadata(self):
        workspace_plan = self._conflict_revision_setup()
        self.connection.execute(
            "UPDATE exec_plans SET priority = 'high' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        self.remote = 'new-base'

        outcome = self._dispatch_conflict_revision(workspace_plan)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertEqual(self.handler.plan['priority'], 'high')
        self.assertEqual(
            self.effects.agent.worker_prompt.call_args.args[2],
            'previous worker result',
        )
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], 'result')

    def _assert_conflict_profile_resume(self, old_profile, new_profile, effective):
        self.connection.execute(
            "UPDATE exec_plans SET current_complexity = ?, current_risk = ? "
            "WHERE repo = 'repo' AND plan_id = 'G-047'", old_profile,
        )
        self.connection.commit()
        workspace_plan = self._conflict_revision_setup()
        current_plan = dict(workspace_plan, complexity=new_profile[0], risk=new_profile[1])
        initial = lm.worker_level(current_plan)
        selected = max(initial, effective)
        history = '{"1":{"prior-review":2}}'
        # G-036 publishes the new profile and monotonic level in one update.
        self.connection.execute(
            "UPDATE exec_plans SET current_complexity = ?, current_risk = ?, "
            "initial_worker_level = ?, effective_worker_level = ?, "
            "review_level_history = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            (*new_profile, initial, selected, history),
        )
        self.connection.commit()
        self.remote = 'new-base'
        self.effects.validation.ensure_worker_level_state.side_effect = (
            lambda repo, plan: lm.ensure_worker_level_state(self.connection, repo, plan)
        )
        with mock.patch('loop_manager.validate_execplan', return_value=workspace_plan):
            with self.assertRaisesRegex(RuntimeError, 'metadata'):
                lm.validate_active_execplan_metadata(
                    Path('/repo'), Path('/state/G-047'),
                    self.state.row()['plan_path'], self.repository.active_row('repo'),
                )

        outcome = self._dispatch_conflict_revision(workspace_plan)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        call = self.effects.agent.run_agent.call_args
        self.assertEqual(call.kwargs['session_id'], 'worker-session')
        self.assertEqual(call.kwargs['level'], selected)
        self.assertEqual(self.handler.plan['complexity'], new_profile[0])
        self.assertEqual(self.handler.plan['risk'], new_profile[1])
        self.assertEqual(self.effects.agent.worker_prompt.call_args.args[2],
                         'previous worker result')
        row = self.repository.active_row('repo')
        self.assertEqual(row['initial_worker_level'], initial)
        self.assertEqual(row['effective_worker_level'], selected)
        self.assertEqual(row['review_level_history'], history)
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        self.handler.guard(self.handler._context())
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], 'result')

    def test_conflict_profile_complexity_change_resumes_same_worker(self):
        self._assert_conflict_profile_resume(('low', 'low'), ('high', 'low'), 1)

    def test_conflict_profile_risk_change_resumes_same_worker(self):
        self._assert_conflict_profile_resume(('low', 'low'), ('low', 'critical'), 1)

    def test_conflict_profile_downgrade_preserves_escalated_level(self):
        self._assert_conflict_profile_resume(('high', 'critical'), ('low', 'low'), 8)

    def test_conflict_revision_scope_rejects_unapproved_metadata(self):
        workspace_plan = self._conflict_revision_setup()
        baseline = DefinitionSnapshot.deserialize(self.state.row()['definition_snapshot'])
        self.connection.execute(
            "UPDATE exec_plans SET priority = 'high', depends_on = '[\"G-048\"]', "
            "current_complexity = 'medium', current_risk = 'medium' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        catalog = self.repository.active_row('repo')
        for field, value in (
            ('priority', 'critical'), ('depends_on', ['G-099']),
            ('complexity', 'high'), ('risk', 'high'),
            ('title', 'Unauthorized'), ('sources', ['docs/OTHER.md']),
        ):
            with self.subTest(field=field), mock.patch(
                'loop_manager.capture_git_snapshot'
            ), mock.patch('loop_manager.validate_agent_scope'), mock.patch(
                'loop_manager.validate_managed_worktree'
            ), mock.patch('loop_manager.validate_execplan', return_value={
                **workspace_plan, field: value,
            }):
                with self.assertRaisesRegex(lm.ExecPlanMetadataError, 'metadata'):
                    lm.validate_worker_workspace_scope(
                        Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                        'main', catalog['path'], catalog, mock.Mock(),
                        allow_rebase=True, rebase_definition=baseline,
                    )
        with mock.patch('loop_manager.capture_git_snapshot'), mock.patch(
            'loop_manager.validate_agent_scope'
        ), mock.patch('loop_manager.validate_managed_worktree'), mock.patch(
            'loop_manager.validate_execplan', return_value=workspace_plan
        ) as parsed:
            with self.assertRaisesRegex(lm.ExecPlanMetadataError, 'metadata'):
                lm.validate_worker_workspace_scope(
                    Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                    'main', catalog['path'], catalog, mock.Mock(),
                )
            parsed.return_value = {
                **workspace_plan, 'priority': 'high', 'depends_on': ['G-048'],
                'complexity': 'medium', 'risk': 'medium',
            }
            current = lm.validate_worker_workspace_scope(
                Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                'main', catalog['path'], catalog, mock.Mock(),
                allow_rebase=True, rebase_definition=baseline,
            )
            self.assertEqual(current['priority'], 'high')
            self.assertEqual(current['depends_on'], ['G-048'])
            self.assertEqual(current['complexity'], 'medium')
            self.assertEqual(current['risk'], 'medium')
        with self.assertRaisesRegex(RuntimeError, '충돌 복구'):
            lm.validate_worker_workspace_scope(
                Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                'main', catalog['path'], catalog, mock.Mock(),
                rebase_definition=baseline,
            )

    def test_conflicted_rebase_blocks_new_worker_input_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.revision.body_hash.return_value = 'revised-body'

        outcome = ExecutionPhaseCoordinator(self.handler).dispatch(
            self.handler._context()
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
        )

    def test_conflict_resolution_rechecks_new_remote_base_before_verify(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.remote = 'new-base'
        guarded = self.handler.guard(self.handler._context())
        self.assertNotIsInstance(guarded, ExecutionOutcome)

        resolved = self.handler._finish_worker(
            guarded, self.state.row(), 'resolved worker result'
        )

        self.assertEqual(resolved.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertEqual(self.calls.count('rebase'), 2)

    def test_revision_guard_adopts_legacy_phase_artifacts_without_worker_replay(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        row = self.state.row()
        snapshot = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        assert snapshot is not None
        self.assertEqual(row['phase'], 'PUBLISHING')
        self.assertEqual(row['worker_result'], 'worker-result')
        self.assertEqual(row['verify_result'], 'verify-result')
        self.assertEqual(row['review_result'], 'review-result')
        self.assertEqual(row['worker_input_token'], snapshot.worker_token)
        self.assertEqual(row['archive_token'], snapshot.archive_token)
        self.assertEqual(row['verify_token'], snapshot.verify_token('base'))
        self.assertEqual(row['review_token'], snapshot.review_token('base'))

    def test_revision_guard_retries_when_base_and_catalog_shas_disagree(self):
        self.effects.revision.reconcile_catalog.side_effect = None
        self.effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha='other-base'
        )

        outcome = self.handler.guard(self.context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['definition_snapshot'])
        self.effects.revision.body_hash.assert_not_called()
        self.effects.revision.source_fingerprint.assert_not_called()

    def test_revision_guard_rejects_legacy_artifacts_after_source_change(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )
        self.remote = 'new-base'
        self.effects.revision.changed_paths.return_value = (
            'docs/PRODUCT.md',
        )

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])

    def test_successful_revision_refresh_clears_rebase_recovery_marker(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        self.remote = 'new-base'

        outcome = self.handler.guard(guarded)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertIsNone(row['revision_resume_status'])
        self.assertIsNone(row['last_error'])

    def test_revision_refresh_checkpoints_dirty_verified_worker_patch_before_rebase(self):
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='worker-result'
        )
        self.remote = 'new-base'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertLess(self.calls.index('commit'), self.calls.index('rebase'))
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertEqual(self.state.row()['worker_result'], 'worker-result')

    def test_dependency_gate_reports_unmerged_direct_and_indirect_plans(self):
        for plan_id, dependencies in (
            ('G-035', '[]'),
            ('G-036', '["G-035"]'),
        ):
            self.connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "('repo', ?, ?, ?, '[\"docs/PRODUCT.md\"]', ?, 'normal', "
                "'hash', 'READY')",
                (plan_id, f'docs/plans/[{plan_id}] Plan.md', 'Plan', dependencies),
            )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on = '[\"G-036\"]' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()

        self.assertEqual(
            lm.unmet_execplan_dependencies(
                self.connection, 'repo', 'G-047'
            ),
            ('G-035', 'G-036'),
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = 'repo' AND plan_id IN ('G-035', 'G-036')"
        )
        self.assertEqual(
            lm.unmet_execplan_dependencies(
                self.connection, 'repo', 'G-047'
            ),
            (),
        )

    def test_verify_interruption_resumes_without_duplicate_commit(self):
        context = self.context('VERIFYING')
        original = self.git
        def interrupted(path, *args, **kwargs):
            if args[0] == 'fetch':
                raise RuntimeError('interruption')
            return original(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = interrupted
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.handler.verify(context, self.handler)
        self.effects.workspace.git.side_effect = original
        self.handler.verify(self.context(), self.handler)
        self.assertEqual(self.calls.count('commit'), 1)

    def test_protocol_correction_reuses_session_before_publication(self):
        self.effects.validation.parse_reviewer_result.side_effect = [
            lm.ReviewerProtocolError('malformed'), ('PASS', ())]
        result = self.handler.review(self.context('REVIEWING'))
        self.assertEqual(result.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(self.calls, ['reviewer', 'reviewer'])
        self.assertEqual(self.effects.agent.run_agent.call_args.kwargs['session_id'], 'session')

    def test_archive_change_returns_to_worker_before_push(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.archive.requirement_archive_reconciliation_error.return_value = 'archive changed'
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertNotIn('push', self.calls)

    def test_guard_pins_archive_target_without_resetting_worker_session(self):
        source = 'docs/requirements/source.md'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        snapshot = ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        self.effects.archive.requirement_archive_snapshot.return_value = snapshot
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.state.update_project('repo', worker_session_id='saved-session')

        result = self.handler.guard(self.context())

        self.assertEqual(result.archive_snapshot_token, snapshot.token)
        target = self.connection.execute(
            'SELECT snapshot_token, stage FROM requirement_archive_targets '
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(target['snapshot_token'], snapshot.token)
        self.assertEqual(target['stage'], 'WORKTREE_UPDATED')
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_guard_remote_race_preserves_worker_session_without_block(self):
        source = 'docs/requirements/source.md'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git
        fetches = 0

        def git(path, *args, **kwargs):
            nonlocal fetches
            if args[0] == 'fetch':
                fetches += 1
                if fetches == 2:
                    self.remote = 'base-2'
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.state.update_project('repo', worker_session_id='saved-session')

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.BASE_BEHIND)
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_guard_persists_archive_target_before_latest_base_refresh(self):
        source = 'docs/requirements/source.md'
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        snapshot = ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        self.effects.archive.requirement_archive_snapshot.return_value = snapshot
        self.handler._refresh_revision_base = mock.Mock(
            return_value=ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.handler._refresh_revision_base.assert_called_once()
        target = self.connection.execute(
            'SELECT base_oid, snapshot_token, stage '
            "FROM requirement_archive_targets WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(tuple(target), ('base-2', snapshot.token, 'OBSERVED'))
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_guard_invalid_remote_catalog_uses_final_recovery_gate(self):
        path = 'docs/plans/[G-002] Invalid.md'
        self.effects.revision.reconcile_catalog.side_effect = None
        self.effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='invalid-catalog'),
            revision_sha='base',
            diagnostic=SimpleNamespace(message='invalid source', path=path,
                                       source='docs/requirements/missing.md'),
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.CATALOG_INVALID)
        self.assertEqual(result.recovery.observation.user_targets,
                         (path, 'docs/requirements/missing.md'))
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.effects.archive.requirement_archive_snapshot.assert_not_called()

    def test_guard_refuses_archive_rebase_when_head_lacks_plan_ancestry(self):
        source = 'docs/requirements/source.md'
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=1 if path == Path('/state/G-047') else 0
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.OWNERSHIP_UNCERTAIN)
        self.assertNotIn('rebase', self.calls)
        self.assertIsNone(self.connection.execute(
            "SELECT snapshot_token FROM requirement_archive_targets WHERE repo = 'repo'"
        ).fetchone())

    def test_guard_archive_rebase_checks_managed_head_not_base_checkout(self):
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (
            'docs/requirements/source.md',
        )
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=0 if path == Path('/state/G-047') else 1
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.handler._refresh_revision_base = mock.Mock(
            return_value=ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.handler._refresh_revision_base.assert_called_once()
        ancestry_paths = [
            call.args[0] for call in self.effects.workspace.git.call_args_list
            if call.args[1:3] == ('merge-base', '--is-ancestor')
        ]
        self.assertEqual(ancestry_paths, [Path('/state/G-047')])
        self.effects.workspace.validate_managed_worktree.assert_called_with(
            Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047'
        )

    def test_guard_archive_snapshot_ignores_base_checkout_head(self):
        self.effects.archive.requirement_archive_sources.return_value = (
            'docs/requirements/source.md',
        )
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=0 if path == Path('/state/G-047') else 1
                )
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git

        result = self.handler.guard(self.context())

        self.assertNotIsInstance(result, ExecutionOutcome)
        ancestry_paths = [
            call.args[0] for call in self.effects.workspace.git.call_args_list
            if call.args[1:3] == ('merge-base', '--is-ancestor')
        ]
        self.assertEqual(ancestry_paths, [Path('/state/G-047')])
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_existing_pr_recovery_does_not_push_or_create(self):
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'existing', 'headRefOid': 'base',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.calls, ['rev-parse'])

    def test_worker_format_scope_and_guidance_stop_before_verify(self):
        scenarios = ('format', 'scope', 'guidance')
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.effects.validation.parse_worker_result.side_effect = None
                self.effects.agent.validate_worker_workspace_scope.side_effect = None
                self.effects.agent.inspect_worker_guidance_result.return_value = False
                self.state.update_project('repo', phase='WORKING')
                if scenario == 'format':
                    self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError('format')
                elif scenario == 'scope':
                    self.effects.agent.validate_worker_workspace_scope.side_effect = lm.AgentScopeError('scope')
                else:
                    self.effects.agent.inspect_worker_guidance_result.return_value = True
                result = self.handler.run(self.context())
                self.assertEqual(
                    result.kind,
                    ExecutionOutcomeKind.RETRY if scenario == 'format'
                    else ExecutionOutcomeKind.FINAL_BLOCK,
                )
                self.assertNotIn('verify', self.calls)

    def test_worker_metadata_mismatch_corrects_then_final_blocks(self):
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )

        outcomes = [self.handler.run(self.handler._context()) for _ in range(3)]

        self.assertEqual([outcome.kind for outcome in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
            ExecutionOutcomeKind.FINAL_BLOCK,
        ])
        self.assertEqual([outcome.recovery.occurrence for outcome in outcomes], [1, 2, 3])
        self.assertEqual(outcomes[-1].recovery.block_reason, lm.BlockReasonCode.PLAN_INPUT_INVALID)
        self.assertEqual(self.state.row()['worker_session_id'], 'session')
        self.assertNotIn('verify', self.calls)
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_rebase_metadata_correction_preserves_resume_context(self):
        self.state.update_project(
            'repo', last_error=lm.REBASE_IN_PROGRESS_ERROR,
            worker_session_id='worker-session', worker_result='saved result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, 'result', session_id='worker-session',
        )
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )

        for occurrence in (1, 2):
            context = self.handler._context()
            self.assertTrue(context.resolving_rebase)
            outcome = self.handler.run(context)
            self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
            self.assertEqual(outcome.recovery.occurrence, occurrence)
            self.assertEqual(outcome.recovery.observation.reason.value, 'worker_correction')
            self.assertTrue(self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
            self.assertIn('catalog metadata mismatch', self.state.row()['last_error'])
            self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
            self.assertEqual(self.state.row()['worker_result'], 'saved result')
            self.assertTrue(
                self.effects.agent.validate_worker_workspace_scope.call_args.kwargs['allow_rebase']
            )
            self.assertEqual(
                self.effects.agent.run_agent.call_args.kwargs['session_id'], 'worker-session',
            )

        self.assertTrue(self.handler._context().resolving_rebase)
        self.assertIn(
            'catalog metadata mismatch', self.effects.agent.worker_prompt.call_args.args[1],
        )
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertNotIn('verify', self.calls)

    def test_verify_metadata_mismatch_returns_to_worker_with_output(self):
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )
        self.state.update_project('repo', phase='VERIFYING', worker_result='saved worker result')

        outcome, output = self.handler._verify_once(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(outcome.recovery.observation.reason.value, 'worker_correction')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['worker_result'], 'saved worker result')
        self.assertEqual(self.state.row()['verify_result'], output)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_repeated_worker_result_format_uses_same_session_before_final_gate(self):
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError('bad format')
        outcomes = [self.handler.run(self.context()) for _ in range(3)]

        self.assertEqual([item.kind for item in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
            ExecutionOutcomeKind.FINAL_BLOCK,
        ])
        self.assertEqual([item.recovery.occurrence for item in outcomes], [1, 2, 3])
        self.assertEqual(self.state.row()['worker_session_id'], 'session')
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_rebase_worker_retry_prompt_uses_latest_failure(self):
        prior_error = lm.REBASE_CONFLICT_PREFIX + 'Worker 결과 검증 실패:\ninvalid result'
        self.state.update_project('repo', last_error=prior_error,
                                  pr_conflict_recovery=1, worker_session_id='worker-session')
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'still conflicted', session_id='worker-session')

        outcomes = [self.handler.run(self.handler._context()) for _ in range(2)]

        self.assertEqual([outcome.kind for outcome in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
        ])
        prompts = [call.args[1] for call in self.effects.agent.worker_prompt.call_args_list]
        self.assertEqual(prompts, [
            prior_error, lm.REBASE_CONFLICT_PREFIX + 'still conflicted',
        ])
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_validation_retry_and_final_block_remain_distinct(self):
        for stage in ('verify', 'review'):
            for blocked in (False, True):
                with self.subTest(stage=stage, blocked=blocked):
                    self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(blocked=blocked, fingerprint='diagnostics')
                    self.effects.validation.verify.side_effect = lambda _: (False, 'diagnostics')
                    self.effects.validation.parse_reviewer_result.return_value = ('FAIL', ('issue',))
                    phase = 'VERIFYING' if stage == 'verify' else 'REVIEWING'
                    context = self.context(phase)
                    result = (self.handler.verify(context, self.handler) if stage == 'verify'
                              else self.handler.review(context))
                    self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK if blocked else ExecutionOutcomeKind.RETRY)

    def test_rebase_conflict_preserves_intent_for_both_checkpoints(self):
        self.remote = 'changed'
        original = self.git
        def conflict(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if args[0] == 'rebase':
                result.returncode = 1
                result.stdout = 'conflict details'
            return result
        self.effects.workspace.git.side_effect = conflict
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        for phase in ('VERIFYING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base')
                context = self.context()
                result = (self.handler.verify(context, self.handler) if phase == 'VERIFYING'
                          else self.handler.publish(context, self.handler))
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                self.assertEqual(result.recovery.disposition.value, 'agent_correction')
                self.effects.blocking.block_active_execplan.assert_not_called()
                self.assertEqual(self.state.row()['phase'], 'WORKING')
                self.assertTrue(self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
                self.assertIsNone(self.state.row()['validated_base_sha'])

    def test_workspace_runtime_failure_retries_without_blocking_or_losing_context(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary git failure')
        self.state.update_project('repo', worker_session_id='worker-session',
                                  worker_result='saved result')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'worktree_recovery_failed')
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'saved result')
        self.assertEqual(row['worktree'], '/state/G-047')
        self.effects.blocking.block_active_execplan_from_revision.assert_not_called()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM blocked_checkpoints WHERE repo = 'repo'"
        ).fetchone()[0], 0)

    def test_exhausted_transient_agent_retries_pause_with_saved_session(self):
        def transient_agent(*_args, **kwargs):
            kwargs['on_session_started']('worker-session')
            return lm.AgentResult(
                1, 'service unavailable', session_id='worker-session',
                kind=lm.AgentResultKind.TRANSIENT_FAILURE,
            )

        self.effects.agent.run_agent.side_effect = transient_agent
        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(result.recovery.observation.reason.value, 'service_unavailable')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_transient_worker_pause_preserves_rebase_resume_marker(self):
        self.state.update_project(
            'repo', pr_conflict_recovery=1,
            last_error=lm.REBASE_IN_PROGRESS_ERROR,
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'temporary provider failure', 'worker-session',
            lm.AgentResultKind.TRANSIENT_FAILURE,
        )

        result = self.handler.run(self.handler._context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertTrue(
            self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX)
        )
        self.assertTrue(self.handler._context().resolving_rebase)

    def test_pr_rebase_resume_operation_failure_retries_without_checkpoint(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.resume_pr_conflict_rebase.return_value = lm.PROJECT_STEP_RETRY
        self.state.update_project(
            'repo', pr_conflict_recovery=1,
            last_error=lm.REBASE_IN_PROGRESS_ERROR,
            worker_session_id='worker-session',
        )

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'rebase_continue_failed')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_publication_checkpoint_sha_lag_reconciles_from_owned_head(self):
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='stale',
            validated_base_sha='base', worker_session_id='worker-session',
            verify_result='saved verify', review_result='saved review',
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'checkpoint_stale')
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertIsNone(self.state.row()['verify_result'])
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_repeated_archive_mismatch_reaches_final_gate_only_at_limit(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.return_value = ('source.md',)
        self.effects.archive.requirement_archive_move_set.return_value = ()
        self.effects.archive.requirement_archive_reconciliation_error.return_value = 'archive changed'

        first = self.handler.publish(self.context(), self.handler)
        self.assertEqual(first.recovery.occurrence, 1)
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.effects.blocking.block_active_execplan.assert_not_called()
        second = self.handler._recover(first.recovery.observation.reason, 'archive changed',
                                       fingerprint=first.recovery.observation.fingerprint)
        self.assertEqual(second.recovery.occurrence, 2)
        self.effects.blocking.block_active_execplan.assert_not_called()
        third = self.handler._recover(first.recovery.observation.reason, 'archive changed',
                                      fingerprint=first.recovery.observation.fingerprint)
        self.assertEqual(third.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_archive_move_validation_error_returns_to_worker_without_block(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project(
            'repo', phase='PUBLISHING', validated_base_sha='base',
            worker_session_id='worker-session', verify_result='saved verify',
            review_result='saved review',
        )
        self.effects.archive.requirement_archive_candidates.return_value = ('source.md',)
        self.effects.archive.requirement_archive_move_set.side_effect = lm.RequirementArchiveError('move invalid')

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'archive_move_mismatch')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['verify_result'], 'saved verify')
        self.assertEqual(self.state.row()['review_result'], 'saved review')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_archive_destination_collision_is_final_with_actual_path(self):
        self._use_runtime_contract()
        path = 'docs/requirements/archive/source.md'
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.side_effect = (
            lm.RequirementArchiveCollisionError(path)
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.user_targets, (path,))
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_archive_git_lock_retries_without_blocking(self):
        self._use_runtime_contract()
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.side_effect = (
            lm.GitLockError('locked')
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.GIT_LOCK)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_publication_interruption_recovers_existing_pr_without_repush(self):
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.command.side_effect = RuntimeError('interrupted response')
        paused = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(paused.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(paused.recovery.observation.reason.value, 'publication_retry')
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'created', 'headRefOid': 'base',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        resumed = self.handler.publish(self.context(), self.handler)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.calls.count('push'), 1)
        self.assertEqual(self.effects.publication.command.call_count, 1)

    def test_pr_conflict_lease_rejection_blocks_without_opening_pr(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='base',
            validated_base_sha='base', publication_remote_oid='old-head',
            pr_conflict_recovery=1, pr_conflict_head_oid='old-head',
            pr_conflict_base_oid='base', pr_head_oid='old-head',
            pr_url='https://example/pr/1',
        )
        self.effects.workspace.remote_branch_oid.return_value = 'old-head'
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'https://example/pr/1', 'state': 'OPEN',
            'headRefName': 'loop-manager/G-047', 'headRefOid': 'old-head',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = None
        self.effects.publication.push_owned_pr_conflict_recovery.side_effect = (
            lm.PublicationSafetyError('lease rejected')
        )

        prepared = self.handler.recover(self.context())
        self.assertEqual(prepared.phase, ExecutionPhase.PUBLISHING)
        result = self.handler.publish(prepared, self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
        )
        self.effects.publication.push_owned_pr_conflict_recovery.assert_called_once()
        self.effects.state.check()
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')

    def test_publication_uses_immutable_plan_link_with_reconciled_metadata(self):
        self.connection.execute(
            "UPDATE exec_plans SET title = 'Current Runtime', "
            "publication_path = 'docs/plans/[G-047] Original Runtime.md', "
            "publication_content_hash = 'original-hash', "
            "publication_sha = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            ("a" * 40,),
        )
        self.connection.commit()
        self.plan = {
            'id': 'G-047', 'title': 'Current Runtime',
            'sources': ['docs/PRODUCT.md'], 'depends_on': [],
            'complexity': 'low', 'risk': 'low', 'priority': 'normal',
        }
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')
        self.effects.publication.published_execplan.return_value = {
            **self.plan, 'title': 'Original Runtime',
        }
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        show = [
            call for call in self.effects.workspace.git.call_args_list
            if len(call.args) >= 3 and call.args[1] == 'show'
        ]
        self.assertEqual(
            show[-1].args[2],
            f"{'a' * 40}:docs/plans/[G-047] Original Runtime.md",
        )
        build = self.effects.publication.build_pr_body.call_args.args
        self.assertEqual(build[1]['title'], 'Current Runtime')
        self.assertEqual(
            build[2], 'docs/plans/[G-047] Original Runtime.md'
        )
        self.assertEqual(build[5], 'a' * 40)

    def test_workspace_violation_blocks_before_agent(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.validate_execplan_workspace_identity.side_effect = lm.WorkspaceOwnershipError('ownership')
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan_from_revision.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_guidance_mismatch_reads_blocked_plan_after_projection_release(self):
        self.connection.execute(lm.GUIDANCE_APPROVALS_SCHEMA)
        proposal = SimpleNamespace(
            proposal_id='guidance-change', line=10,
            decision=SimpleNamespace(line=20),
        )
        document = SimpleNamespace(proposals=(proposal,), structure_hash='structure')
        approval = {'applied': 0, 'proposal_id': proposal.proposal_id,
                    'target_path': 'AGENTS.md'}
        self.effects.agent.inspect_worker_guidance_result.side_effect = self.state.bind(
            lm.inspect_worker_guidance_result, connected=True, mutates=True,
        )
        context = self.context()
        with (
            mock.patch.object(lm, '_guidance_document', return_value=document),
            mock.patch.object(lm, '_guidance_rows', return_value=(approval,)),
            mock.patch.object(lm, '_validate_guidance_proposal_scope'),
            mock.patch.object(lm, 'is_loop_manager_repository', return_value=True),
            mock.patch.object(lm, '_guidance_document_matches_rows',
                              return_value=({proposal.proposal_id: proposal}, ())),
            mock.patch.object(lm, '_guidance_final_expected',
                              return_value=({('batch', 'AGENTS.md'): (b'approved', 0o100644)}, {})),
            mock.patch.object(lm, '_guidance_target_state',
                              return_value=(b'mismatched', 0o100644)),
            mock.patch.object(lm, 'capture_git_snapshot') as snapshot,
        ):
            snapshot.return_value.serialize.return_value = '{}'
            result = self.handler.run(context)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIsNone(self.repository.projected('repo'))
        self.assertIsNone(self.state.expected)
        blocked = self.repository.get('repo', 'G-047')
        self.assertEqual(blocked.status, 'PLAN_BLOCKED')
        self.assertIn('실제 AGENTS.md 결과가 승인된 patch 결과와 다릅니다.', blocked.last_error)
        self.assertEqual(self.state.blocked()['last_error'], blocked.last_error)
        self.assertEqual(
            self.effects.events.operation_print.call_args.args[0], blocked.last_error,
        )
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.handler.run(context)
