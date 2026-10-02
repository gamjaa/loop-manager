"""Worker Phase execution policy regression tests."""

from tests.support.current_rebase import current_rebase_case
from tests.support.execution import ExecutionFixture
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.execution.validation_feedback import Feedback
from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcomeKind
from loop_manager_internal.execution.worker_session import SessionBinding, SessionTransition
from tests.support.execution import VALID_WORKER_RESULT


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionWorkerPhaseTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_rebase_completion_session_handles_optional_snapshot(self):
        context = SimpleNamespace(worker_session_id='current-session')
        cases = (
            ({}, 'current-session'),
            ({'snapshot': None}, 'current-session'),
            ({'snapshot': {}}, 'current-session'),
            ({'snapshot': {'rebase_start_session_id': 'start-session'}},
             'current-session'),
            ({'snapshot': {'stash_oid': 'stash', 'pending': 'stage',
                           'rebase_start_session_id': 'start-session'}},
             'current-session'),
            ({'snapshot': {'stash_oid': 'stash', 'pending': 'continue',
                           'rebase_start_session_id': 'start-session'}},
             'start-session'),
            ({'snapshot': {'stash_oid': 'stash', 'pending': 'continue',
                           'rebase_start_session_id': None}}, None),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.handler._rebase_completion_session(
                    context, SimpleNamespace(**fields),
                ), expected)


    def test_worker_prompt_delivers_validation_once_and_keeps_other_errors(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'verify', False,
            'private raw prelude\nfirst failure',
            (Feedback('verify', 'verify:fingerprint', 'first failure'),),
        )
        self.state.update_project(
            'repo', last_error='전체 검증 실패:\nprivate raw prelude\nfirst failure',
            verify_result='private raw prelude\nfirst failure',
            worker_result='previous result', worker_session_id='same-session',
        )
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, VALID_WORKER_RESULT, session_id='same-session',
        )
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
            first_prompt = self.effects.agent.run_agent.call_args.args[5]
            self.handler.run(self.context())
            second_prompt = self.effects.agent.run_agent.call_args.args[5]
        self.assertIn('verify:fingerprint occurrence=1 revision=1', first_prompt)
        self.assertIn('WORKER_OUTCOME:', first_prompt)
        self.assertIn('needs_user_decision', second_prompt)
        self.assertEqual(first_prompt.count('first failure'), 1)
        self.assertNotIn('private raw prelude', first_prompt)
        self.assertNotIn('first failure', second_prompt)
        self.assertNotIn('verify:fingerprint', second_prompt)
        self.state.update_project('repo', last_error='Worker 결과 검증 실패:\nbad format')
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertIn('bad format', self.effects.agent.run_agent.call_args.args[5])


    def test_timeout_prompt_requests_cause_check_and_progress_evidence(self):
        prompt = lm.worker_prompt(
            'docs/plans/[G-047] Runtime.md', None,
            validation_feedback='전체 verify 시간 초과: verify_timeout_seconds=2',
        )
        self.assertIn('무한 반복·교착·중복 검증', prompt)
        self.assertIn('PR_SUMMARY 안에 VERIFY_TIMEOUT_LIMIT_INSUFFICIENT:', prompt)


    def test_session_unavailable_rebuilds_compact_unresolved_feedback(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'review', False,
            'raw Review detail omitted from prompt',
            (Feedback('review', 'review:a.py::f::bug', 'Fix the assertion'),),
        )
        self.state.update_project(
            'repo', worker_session_id='missing-session',
            worker_result='previous result', review_result='raw Review detail omitted from prompt',
            last_error='Review 실패:\nraw Review detail omitted from prompt',
        )
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        prompts = []

        def invoke(*args, **kwargs):
            prompts.append(args[5])
            if kwargs['session_id']:
                return lm.AgentResult(1, 'unknown session', session_id='missing-session')
            kwargs['on_session_started']('replacement')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='replacement')

        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(len(prompts), 2)
        self.assertIn('AGENTS.md, 선택된 최신 ExecPlan과 현재 worktree', prompts[1])
        self.assertIn('previous result', prompts[1])
        self.assertIn('review:a.py::f::bug occurrence=1 revision=1', prompts[1])
        self.assertIn('Fix the assertion', prompts[1])
        self.assertNotIn('raw Review detail omitted from prompt', prompts[1])
        self.assertEqual(feedback.prepare('repo', 'G-047', 'replacement').rows, ())


    def test_model_replacement_records_intent_before_agent_and_binds_event(self):
        old = SessionBinding('codex', 'old-model', 'low')
        self.state.update_project(
            'repo', worker_session_id='old-session',
            worker_session_binding=old.serialize(), worker_result='saved result',
            verify_result='raw verify', publication_token='publication',
        )
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'new-model', 'high',
        )
        observed = []
        def invoke(*args, **kwargs):
            row = self.state.row()
            observed.append((kwargs['session_id'], row['worker_session_id'],
                             SessionTransition.deserialize(row['worker_session_transition']),
                             kwargs['resolved_profile'], kwargs['invocation']))
            kwargs['on_session_started']('new-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='new-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        session, stored, intent, profile, invocation = observed[0]
        self.assertIsNone(session)
        self.assertIsNone(stored)
        self.assertEqual((intent.cause, intent.previous_session),
                         ('model_changed', 'old-session'))
        self.assertEqual((profile.model, profile.effort), ('new-model', 'high'))
        self.assertEqual((invocation.transition, invocation.transition_id,
                          invocation.previous_model),
                         ('replacement', intent.identity, 'old-model'))
        row = self.state.row()
        self.assertEqual((row['worker_session_id'], row['worker_session_binding']),
                         ('new-session', intent.target.serialize()))
        self.assertIsNone(row['worker_session_transition'])
        self.assertEqual((row['worker_result'], row['verify_result'],
                          row['publication_token']),
                         ('saved result', 'raw verify', 'publication'))


    def test_legacy_unbound_session_starts_recovery_without_resuming(self):
        self.state.update_project('repo', worker_session_id='legacy-session',
                                  worker_session_binding=None)
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        def invoke(*args, **kwargs):
            self.assertIsNone(kwargs['session_id'])
            self.assertEqual(kwargs['invocation'].transition_cause,
                             'unbound_session_recovery')
            self.assertIn('AGENTS.md, 선택된 최신 ExecPlan과 현재 worktree', args[5])
            kwargs['on_session_started']('bound-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='bound-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_id'], 'bound-session')


    def test_provider_change_launches_target_provider_without_old_session(self):
        self.state.update_project('repo', worker_session_id='codex-session')
        self.handler.provider = 'claude'
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'model', 'high',
        )
        def invoke(*args, **kwargs):
            self.assertEqual(args[0], 'claude')
            self.assertIsNone(kwargs['session_id'])
            self.assertEqual(kwargs['invocation'].transition_cause,
                             'provider_changed')
            kwargs['on_session_started']('claude-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='claude-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_binding'],
                         SessionBinding('claude', 'model', 'high').serialize())


    def test_level_and_alias_change_with_same_resolved_model_resumes_session(self):
        self.state.update_project('repo', worker_session_id='same-session')
        self.handler.level = 7
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'model', 'low',
        )
        def invoke(*args, **kwargs):
            self.assertEqual(kwargs['session_id'], 'same-session')
            self.assertEqual(kwargs['resolved_profile'].effort, 'low')
            self.assertEqual(kwargs['invocation'].transition, 'effort_resume')
            self.assertIsNone(self.state.row()['worker_session_transition'])
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='same-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_binding'],
                         SessionBinding('codex', 'model', 'low').serialize())


    def test_timeout_after_session_event_retries_observed_session(self):
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        attempts = []
        def invoke(*args, **kwargs):
            attempts.append(kwargs['session_id'])
            if len(attempts) == 1:
                kwargs['on_session_started']('observed-session')
                raise lm.subprocess.TimeoutExpired('agent', 1)
            return lm.AgentResult(0, VALID_WORKER_RESULT,
                                  session_id='observed-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(attempts, [None, 'observed-session'])
        self.assertEqual(self.state.row()['worker_session_id'], 'observed-session')


    def test_verify_audit_keeps_full_raw_output_and_success_clears_feedback(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        raw = 'beginning of raw output\n' + 'x' * 9000 + '\nFAIL'
        self.effects.validation.verify.side_effect = [
            (False, lm.VerifyOutput(raw)), (True, lm.VerifyOutput('SUCCESS output')),
        ]
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='fingerprint',
        )

        self.handler._verify_once(self.context('VERIFYING'))
        self.assertEqual(feedback.prepare('repo', 'G-047', 'session').rows[0]['occurrence'], 1)
        self.handler._verify_once(self.context('VERIFYING'))
        audit = self.connection.execute(
            "SELECT classification,raw_output FROM validation_results ORDER BY id"
        ).fetchall()
        self.assertEqual([(item['classification'], item['raw_output']) for item in audit],
                         [('failure', raw), ('success', 'SUCCESS output')])
        self.assertEqual(feedback.prepare('repo', 'G-047', 'session').rows, ())


    def test_verify_feedback_extracts_full_failure_source_not_persisted_tail(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        raw = (
            'FAILED tests/test_focus.py::test_message - AssertionError: expected message\n'
            'E       assert actual == expected\n'
            '================= 1 failed, 99 passed =================\n'
            + 'PASS unrelated completion\n' * 800
        )
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (
            False, lm.VerifyOutput(raw, exit_code=1),
        )
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='fingerprint',
        )

        self.handler._verify_once(self.context('VERIFYING'))

        row, = feedback.prepare('repo', 'G-047', 'session').rows
        self.assertIn('tests/test_focus.py::test_message', row['diagnostic'])
        self.assertIn('assert actual == expected', row['diagnostic'])
        self.assertIn('verify exit code: 1', row['diagnostic'])
        self.assertNotIn('unrelated completion', row['diagnostic'])
        audit = self.connection.execute(
            'SELECT raw_output FROM validation_results'
        ).fetchone()
        self.assertEqual(audit['raw_output'], raw)


    def test_invalid_worker_result_keeps_feedback_pending(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'verify', False, 'raw',
            (Feedback('verify', 'verify:fingerprint', 'fix this'),),
        )
        self.state.update_project('repo', worker_session_id='same-session')
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, 'invalid format', session_id='same-session',
        )
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError('format')

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(feedback.prepare('repo', 'G-047', 'same-session').rows[0]['revision'], 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_delivery_attempts WHERE status='delivered'"
        ).fetchone()[0], 0)


    def test_failed_fallback_start_reuses_recovery_prompt_on_next_turn(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'verify', False, 'raw full failure',
            (Feedback('verify', 'verify:fingerprint', 'fix this'),),
        )
        self.state.update_project(
            'repo', worker_session_id='missing-session', worker_result='previous result',
            verify_result='raw full failure', last_error='전체 검증 실패:\nraw full failure',
        )
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        prompts = []

        def failed_start(*args, **kwargs):
            prompts.append(args[5])
            if kwargs['session_id']:
                return lm.AgentResult(1, 'unknown session', session_id='missing-session')
            kwargs['on_session_started']('replacement')
            return lm.AgentResult(1, 'provider rejected', session_id='replacement')

        self.effects.agent.run_agent.side_effect = failed_start
        self.assertEqual(self.handler.run(self.context()).kind,
                         ExecutionOutcomeKind.OPERATIONAL_ERROR)
        self.effects.agent.run_agent.side_effect = lambda *args, **kwargs: (
            prompts.append(args[5]) or lm.AgentResult(
                0, VALID_WORKER_RESULT, session_id='replacement',
            )
        )
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertIn('AGENTS.md, 선택된 최신 ExecPlan과 현재 worktree', prompts[-1])
        self.assertIn('previous result', prompts[-1])
        self.assertIn('verify:fingerprint occurrence=1 revision=1', prompts[-1])
        self.assertNotIn('raw full failure', prompts[-1])


    def test_single_abnormal_rebase_worker_result_preserves_session_without_block(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'abnormal exit', session_id='same-session',
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['worker_session_id'], 'same-session')
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.effects.blocking.block_active_execplan.assert_not_called()
        correction = self.handler.rebase_corrections.get('repo', 'G-047')
        self.assertEqual(correction.occurrence, 1)
        self.assertIsNone(correction.worker_result)
        self.assertEqual(correction.audit[-1]['worker_result'], 'abnormal exit')


    def test_rebase_worker_result_format_error_stops_run_for_same_session_retry(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='worker-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError(
            'missing final fields'
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.WORKER_CORRECTION)
        self.assertEqual(lm._execution_outcome_step_result(result), 1)
        self.assertEqual(self.state.row()['worker_session_id'], 'session')
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')


    def test_autostash_published_completion_keeps_start_session_after_worker_replacement(self):
        from loop_manager_internal.git.managed_rebase_validation import FileChange
        case = current_rebase_case(self)
        case.expected = replace(case.expected, autostash_oid='stash')
        case.state.update_project('repo', worker_session_id='replacement-session')
        case.observed = replace(case.observed, index_clean=False,
            staged=(FileChange('app.py', '100644', '100644', 'old', 'approved', 'M'),))
        original = case.checkpoint.get('repo', 'G-047')
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            case.handler._complete_rebase_intent(case.fixture.context())
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
        case.observed = replace(case.observed, index_clean=True, staged=())
        case.handler._complete_rebase_intent(case.fixture.context())
        self.assertIsNone(case.checkpoint.get('repo', 'G-047').operation)
        self.assertEqual(case.state.row()['worker_session_id'], 'replacement-session')
        self.assertIsNone(case.state.row()['worker_result'])


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


    def test_conflict_profile_complexity_change_resumes_same_worker(self):
        self._assert_conflict_profile_resume(('low', 'low'), ('high', 'low'), 1)


    def test_conflict_profile_risk_change_resumes_same_worker(self):
        self._assert_conflict_profile_resume(('low', 'low'), ('low', 'critical'), 1)


    def test_conflict_profile_downgrade_preserves_escalated_level(self):
        self._assert_conflict_profile_resume(('high', 'critical'), ('low', 'low'), 8)


    def test_protocol_correction_reuses_session_before_publication(self):
        self.effects.validation.parse_reviewer_result.side_effect = [
            lm.ReviewerProtocolError('malformed'), ('PASS', ())]
        result = self.handler.review(self.review_context())
        self.assertEqual(result.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(
            [call for call in self.calls if call == 'reviewer'],
            ['reviewer', 'reviewer'],
        )
        self.assertEqual(self.effects.agent.run_agent.call_args.kwargs['session_id'], 'session')


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
