"""Verify Phase execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage import verify_receipts
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcome, ExecutionOutcomeKind
from tests.support.worker import decision as worker_decision
from tests.support.execution import READY_WORKER_RESULT, VALID_WORKER_RESULT


from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionVerifyPhaseTests(ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_successful_verify_saves_candidate_proof_before_scope_block(self):
        self.review_context()
        self.dirty = False
        self.state.update_project('repo', phase='VERIFYING')
        context = self.handler._context()
        definition = self.handler._snapshot()._definition_snapshot
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (True, 'completed verify output')
        final = ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK, detail='scope blocked', error=True)

        def reject_scope(*_args, **_kwargs):
            row = self.state.row()
            self.assertEqual(row['validated_base_sha'], context.active_sha)
            self.assertEqual(row['verify_result'], 'completed verify output')
            self.assertEqual(row['verify_token'], definition.verify_token(
                context.active_sha, 'base', 'completed verify output',
            ))
            raise lm.AgentScopeError('scope blocked after verify')

        self.effects.agent.validate_worker_workspace_scope.side_effect = reject_scope
        with mock.patch.object(self.handler, '_block_final'), \
                mock.patch.object(self.handler, '_final', return_value=final):
            outcome, output = self.handler._verify_once(context, candidate_head='base')

        self.assertIs(outcome, final)
        self.assertEqual(output, 'completed verify output')

    def test_successful_verify_with_dirty_candidate_and_scope_block_has_no_proof(self):
        for dirty_status in (' M app.py', 'M  app.py', '?? new.py'):
            with self.subTest(status=dirty_status):
                self.review_context()
                self.state.update_project('repo', phase='VERIFYING', verify_token=None,
                                          validated_base_sha=None)
                context = self.handler._context()
                self.effects.validation.verify.side_effect = None
                self.effects.validation.verify.return_value = (True, 'completed verify output')
                self.effects.agent.validate_worker_workspace_scope.side_effect = lm.AgentScopeError(
                    'ignored artifact scope block')
                original_git = self.git
                def git(worktree, *args, **kwargs):
                    if args[:2] == ('status', '--porcelain'):
                        return SimpleNamespace(stdout=dirty_status)
                    return original_git(worktree, *args, **kwargs)
                self.effects.workspace.git.side_effect = git
                with mock.patch.object(self.handler, '_block_final'), \
                        mock.patch.object(self.handler, '_final'):
                    self.handler._verify_once(context, candidate_head='base')
                self.assertIsNone(self.state.row()['verify_token'])
                self.assertIsNone(self.state.row()['validated_base_sha'])

    def test_failed_verify_scope_block_cannot_reuse_prior_success_token(self):
        self.review_context()
        definition = self.handler._snapshot()._definition_snapshot
        self.state.update_project(
            'repo', phase='VERIFYING', validated_base_sha='base',
            verify_result='prior successful output',
            verify_token=definition.verify_token('base', 'base', 'prior successful output'),
        )
        context = self.handler._context()
        self.dirty = False
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (False, 'failed output from resumed verify')
        self.effects.agent.validate_worker_workspace_scope.side_effect = lm.AgentScopeError(
            'scope block after failed verify',
        )
        final = ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK, detail='scope blocked', error=True)

        def save_block_output(repo, _worktree, _detail, verify_result, **_kwargs):
            self.state.update_project(repo, verify_result=verify_result)
            return final

        with (mock.patch.object(self.handler, '_fetch_latest_base', return_value='base'),
              mock.patch.object(self.handler, '_post_effect_guard', return_value=context),
              mock.patch.object(self.handler, '_block_final', side_effect=save_block_output),
              mock.patch.object(self.handler, '_final', return_value=final)):
            outcome = self.handler.verify(context, SimpleNamespace(
                reconcile=mock.Mock(return_value=None),
            ))

        self.assertIs(outcome, final)
        row = self.state.row()
        self.assertEqual(row['verify_result'], 'failed output from resumed verify')
        self.assertIsNone(row['validated_base_sha'])
        self.assertIsNone(row['verify_token'])
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertFalse(lm.project_block_service._saved_verify_matches_candidate(plan, 'base'))

    def test_timeout_verify_uses_stable_fingerprint_and_returns_to_worker(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (
            False, lm.VerifyOutput(
                'progress\n전체 verify 시간 초과: verify_timeout_seconds=2',
                timed_out=True,
            ),
        )
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='timeout',
        )
        outcome, output = self.handler._verify_once(self.context('VERIFYING'))
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIn('전체 verify 시간 초과:', output)
        first = lm.validation_failure_fingerprints('verify', output)[0][0]
        second = lm.validation_failure_fingerprints(
            'verify', lm.VerifyOutput('other progress', timed_out=True),
        )[0][0]
        self.assertEqual(first, second)
        self.effects.validation.transition_validation_failure.assert_called_once()

    def test_verify_failure_redacts_transition_and_operation_output(self):
        secret = 'verify-handler-secret'
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (
            False, f'FAILED tests/test_config.py::test_loads\npassword: {secret}',
        )
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='failure',
        )

        _outcome, output = self.handler._verify_once(self.context('VERIFYING'))

        self.assertNotIn(secret, output)
        self.assertNotIn(
            secret,
            self.effects.validation.transition_validation_failure.call_args.args[3],
        )
        self.assertTrue(all(
            secret not in str(call.args)
            for call in self.effects.events.operation_print.call_args_list
        ))


    def test_unconfirmed_verify_termination_blocks_project(self):
        self.effects.validation.verify.side_effect = lm.VerifyTerminationUncertain(
            'verify process group 잔존 (PGID 123)'
        )
        with mock.patch.object(self.handler, '_block_final') as blocked:
            outcome, _ = self.handler._verify_once(self.context('VERIFYING'))
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            blocked.call_args.kwargs['reason'],
            lm.BlockReasonCode.REPOSITORY_STATE_UNSAFE,
        )


    def test_worker_evidence_blocks_insufficient_verify_limit(self):
        self.state.update_project(
            'repo', phase='WORKING',
            verify_result='progress\n전체 verify 시간 초과: verify_timeout_seconds=2',
        )
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0,
            'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
            'COMMIT_TITLE: Fix verify\nPR_SUMMARY: progress\n'
            'TARGETED_TESTS: none\n'
            'VERIFY_TIMEOUT_LIMIT_INSUFFICIENT: 9 of 10 required tests completed',
            session_id='session',
        )
        self.effects.validation.parse_worker_result.return_value = SimpleNamespace(
            commit_title='Fix verify'
        )
        self.effects.validation.block_verify_timeout_limit.return_value = 'limit too short'
        with mock.patch.object(self.handler, '_finish_worker') as finish:
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.validation.block_verify_timeout_limit.assert_called_once()
        finish.assert_not_called()


    def test_saved_verifying_decision_blocks_before_commit_and_verify(self):
        self._decision_workspace()
        message = worker_decision()
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result=message,
            agent_role=None, agent_snapshot=None, agent_input_fingerprint=None,
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler.verify(self.context(), None)
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.assertEqual(self.connection.execute(
            "SELECT blocked_fingerprint FROM exec_plans WHERE repo = 'repo' "
            "AND plan_id = 'G-047'"
        ).fetchone()[0], 'remote-input')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.assertNotIn('add', self.calls)


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
        self.assertEqual(first.context.phase, ExecutionPhase.IMPLEMENTING)
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['stage_evidence'], {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'app.py': 'accepted-content'},
        })

        first = self.handler.run(first.context)
        self.assertEqual(first.context.phase, ExecutionPhase.VERIFYING)
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


    def test_worker_and_reviewer_keep_phase_outcome_after_telemetry_error(self):
        def reported_agent(*args, **kwargs):
            self.calls.append(args[4])
            kwargs['on_session_started']('session')
            return lm.AgentResult(0, READY_WORKER_RESULT, session_id='session',
                                  collection_status='error')

        self.effects.agent.run_agent.side_effect = reported_agent
        worker = self.handler.run(self.handler.guard(self.context()))
        verified = self.handler.verify(worker.context, self.handler)
        reviewed = self.handler.review(verified.context)

        self.assertEqual(reviewed.context.phase, ExecutionPhase.PUBLISHING)
        calls = self.effects.agent.run_agent.call_args_list
        self.assertEqual([call.args[4] for call in calls], ['worker', 'reviewer'])
        for call in calls:
            request = call.kwargs['invocation']
            self.assertEqual((request.repo, request.plan_id, request.attempt),
                             ('repo', 'G-047', 1))
            self.assertNotIn('connection', call.kwargs)


    def test_latest_base_uses_same_intent_and_rebase_for_both_checkpoints(self):
        for phase in ('VERIFYING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base',
                                          worker_result=VALID_WORKER_RESULT)
                self.remote = 'new-base'
                self.calls.clear()
                context = self.context()
                result = (self.handler.verify(context, self.handler) if phase == 'VERIFYING'
                          else self.handler.publish(context, self.handler))
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                self.assertEqual(self.state.row()['phase'], 'WORKING')
                self.assertIsNone(self.state.row()['worker_result'])
                self.assertIsNone(self.state.row()['review_result'])
                self.assertEqual(self.calls.count('rebase'), 1)
                self.assertEqual(self.calls.count('verify'), 0)
                self.assertEqual(self.state.row()['active_sha'], 'new-base')
                confirmed = self.handler.run(self.handler._context())
                self.assertEqual(confirmed.context.phase, ExecutionPhase.VERIFYING)
                verified = self.handler.verify(confirmed.context, self.handler)
                self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING)
                self.assertEqual(self.calls.count('verify'), 1)
                self.assertLess(self.calls.index('rebase'), self.calls.index('worker'))
                self.assertLess(self.calls.index('worker'), self.calls.index('verify'))


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
        self.effects.validation.verify.assert_not_called()
        self.effects.workspace.git.side_effect = original
        self.handler.verify(self.context(), self.handler)
        self.assertEqual(self.calls.count('commit'), 1)
        self.assertEqual(self.calls.count('verify'), 1)


    def test_candidate_head_change_after_verify_requires_new_validation(self):
        self.handler.guard(self.context())
        worker = self.handler.run(self.handler._context())
        verified = self.handler.verify(worker.context, self.handler)
        self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING)
        previous_token = self.state.row()['verify_token']
        self.assertIsNotNone(previous_token)
        original = self.git

        def replaced_head(path, *args, **kwargs):
            if path == Path('/state/G-047') and args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='replacement-head\n', returncode=0)
            return original(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = replaced_head
        outcome = self.handler.guard(self.handler._context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertIsNone(self.state.row()['verify_token'])
        self.effects.agent.run_agent.assert_called_once()


    def test_review_entry_rechecks_verified_candidate_before_agent(self):
        self.handler.guard(self.context())
        worker = self.handler.run(self.handler._context())
        verified = self.handler.verify(worker.context, self.handler)
        original = self.git

        def replaced_head(path, *args, **kwargs):
            if path == Path('/state/G-047') and args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='replacement-head\n', returncode=0)
            return original(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = replaced_head
        outcome = self.handler.review(verified.context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.effects.agent.run_agent.assert_called_once()


    def test_review_entry_returns_invalid_worker_result_to_correction(self):
        self.handler.guard(self.context())
        worker = self.handler.run(self.handler._context())
        verified = self.handler.verify(worker.context, self.handler)
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError(
            'TARGETED_TESTS 형식이 유효하지 않습니다.',
        )

        outcome = self.handler.review(verified.context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.effects.agent.run_agent.assert_called_once()


    def test_review_entry_accepts_empty_successful_verify_output(self):
        self.effects.agent.reviewer_prompt.side_effect = lm.reviewer_prompt
        context = self.review_context(verify_output='')

        outcome = self.handler.review(context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.agent.run_agent.assert_called_once()
        prompt = self.effects.agent.run_agent.call_args.args[5]
        self.assertIn('Manager 확인: 전체 verify PASS', prompt)
        self.assertIn('기록된 출력 없음', prompt)
        self.assertIn('candidate HEAD=base', prompt)

    def test_review_entry_rejects_missing_current_candidate_receipt(self):
        self.review_context()
        selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
        self.connection.execute(
            "UPDATE verify_receipts SET status='failure' WHERE id=?",
            (selection['receipt_id'],),
        )

        outcome = self.handler.review(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertIsNone(self.state.row()['verify_token'])
        self.effects.agent.run_agent.assert_not_called()


    def test_head_change_during_verify_does_not_save_success(self):
        original = self.git
        changed = False

        def changing_git(path, *args, **kwargs):
            if path == Path('/state/G-047') and args == ('rev-parse', 'HEAD') and changed:
                return SimpleNamespace(stdout='new-head\n', returncode=0)
            return original(path, *args, **kwargs)

        def verify(_worktree):
            nonlocal changed
            changed = True
            return True, 'OK'

        self.effects.workspace.git.side_effect = changing_git
        self.effects.validation.verify.side_effect = verify
        outcome = self.handler.verify(self.context('VERIFYING'), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['verify_result'])
        self.assertIsNone(self.state.row()['verify_token'])
        self.effects.validation.verify.assert_called_once()


    def test_verify_generated_allowed_change_is_committed_and_reverified(self):
        attempts = 0

        def verify(_worktree):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.dirty = True
            return True, 'OK'

        self.effects.validation.verify.side_effect = verify
        first = self.handler.verify(self.context('VERIFYING'), self.handler)
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.calls.count('commit'), 2)
        self.assertEqual(attempts, 1)
        self.assertIsNone(self.state.row()['verify_result'])
        self.effects.validation.clear_validation_failure_history.assert_not_called()
        second = self.handler.verify(self.handler._context(), self.handler)
        self.assertEqual(second.context.phase, ExecutionPhase.REVIEWING)
        self.assertEqual(attempts, 2)
        self.assertEqual(self.calls.count('commit'), 2)
        self.effects.validation.clear_validation_failure_history.assert_called_once_with(
            'repo', 'verify',
        )


    def test_unchanged_worker_result_blocks_before_full_verify(self):
        self.dirty = False
        self.effects.workspace.branch_has_changes.return_value = False
        outcome = self.handler.verify(self.context('VERIFYING'), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.validation.verify.assert_not_called()
        self.assertNotIn('commit', self.calls)


    def test_rebase_discards_prior_candidate_evidence_before_verify(self):
        self.remote = 'new-base'
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='worker result',
            verify_result='old verify', review_result='old review',
            validated_base_sha='base', verify_token='old token',
            review_token='old review token',
        )
        outcome = self.handler.verify(self.handler._context(), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertIsNone(row['verify_token'])
        self.assertIsNone(row['review_token'])
        self.effects.validation.verify.assert_not_called()


    def test_validation_retry_and_final_block_remain_distinct(self):
        for stage in ('verify', 'review'):
            for blocked in (False, True):
                with self.subTest(stage=stage, blocked=blocked):
                    self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(blocked=blocked, fingerprint='diagnostics')
                    self.effects.validation.verify.side_effect = lambda _: (False, 'diagnostics')
                    self.effects.validation.parse_reviewer_result.return_value = ('FAIL', ('issue',))
                    phase = 'VERIFYING' if stage == 'verify' else 'REVIEWING'
                    context = (
                        self.context(phase) if stage == 'verify'
                        else self.review_context()
                    )
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
                self.effects.validation.verify.assert_not_called()
