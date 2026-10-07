"""Guidance execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from types import SimpleNamespace
from unittest import TestCase, mock

import loop_manager as lm
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from tests.support.execution import READY_WORKER_RESULT
from pathlib import Path


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionGuidanceTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.state.update_project('repo', active_sha='base', worktree=str(Path('/state/G-047')))

    def test_preapproval_is_prepared_before_worker_scope_and_model_call(self):
        order = []
        self.effects.agent.prepare_guidance_preapprovals.side_effect = lambda *args: order.append('prepare') or False
        self.effects.agent.begin_agent_scope.side_effect = lambda *args, **kwargs: order.append('scope')
        agent = self.effects.agent.run_agent.side_effect
        self.effects.agent.run_agent.side_effect = lambda *args, **kwargs: order.append('agent') or agent(*args, **kwargs)
        self.handler.run(self.context())
        self.assertEqual(order[:3], ['prepare', 'scope', 'agent'])

    def test_stale_preapproval_stops_before_worker_scope_model_and_verify(self):
        self.effects.agent.prepare_guidance_preapprovals.return_value = True
        result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.begin_agent_scope.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()

    def test_invalid_preapproval_blocks_only_its_plan_before_agent_scope(self):
        self.effects.agent.prepare_guidance_preapprovals.side_effect = lm.AgentScopeError('사전 승인 불일치')
        result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        blocked = self.effects.blocking.block_active_execplan.call_args.kwargs['recovery_decision']
        self.assertEqual(blocked.block_reason.value, 'plan_input_invalid')
        self.assertEqual(blocked.scope.value, 'plan')
        self.effects.agent.begin_agent_scope.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()

    def test_preapproval_read_failure_uses_recovery_without_running_worker(self):
        self.effects.agent.prepare_guidance_preapprovals.side_effect = OSError('읽기 실패')
        result = self.handler.run(self.context())
        self.assertIn(result.kind, (ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.TRANSIENT_PAUSE))
        self.effects.agent.begin_agent_scope.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()

    def test_guidance_result_keeps_worker_scope_until_block_is_recorded(self):
        worktree, before, after = self._prepare_pending_guidance(
            implementation_changes=True,
        )
        with (
            mock.patch.object(lm, 'capture_git_snapshot', return_value=after),
            mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
        ):
            result = self.handler.run(self.context())
        self._assert_pending_guidance(result, worktree, before, after, 'session')
        self.assertEqual(self.repository.get('repo', 'G-047').worker_result,
                         READY_WORKER_RESULT)
        self.effects.agent.validate_worker_workspace_scope.assert_called_once()


    def test_saved_rebase_guidance_result_keeps_worker_scope(self):
        worktree, before, after = self._prepare_pending_guidance(
            implementation_changes=False,
        )
        saved = SimpleNamespace(
            scope_marker='accepted', worker_code=0, worker_result='proposal',
        )
        with (
            mock.patch.object(lm, 'capture_git_snapshot', return_value=after),
            mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
        ):
            result = self.handler._resume_saved_rebase_worker(
                self.context(), saved, SimpleNamespace(progress='accepted'),
            )
        self._assert_pending_guidance(result, worktree, before, after, 'saved-session')
        self.assertEqual(self.repository.get('repo', 'G-047').worker_result, 'proposal')
        self.effects.agent.run_agent.assert_not_called()


    def test_real_worker_guidance_plan_only_checkpoint_tree(self):
        self._run_real_worker_guidance_checkpoint(implementation_changes=False)


    def test_real_worker_guidance_with_code_and_test_checkpoint_tree(self):
        self._run_real_worker_guidance_checkpoint(implementation_changes=True)


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


class WorkerGuidanceInputTests(TestCase):
    def test_worker_request_example_is_complete_pending_and_parses_with_proposal_diff(self):
        from loop_manager_internal.documents import guidance_approval as ga
        prompt = lm.worker_prompt('docs/plans/test.md', None)
        start = prompt.index('### Agent Guidance Proposal: guidance-example')
        end = prompt.index(ga.END_USER_DECISION, start) + len(ga.END_USER_DECISION)
        form = prompt[start:end]
        document = ga.parse_guidance_document('## Decision Log\n' + form)
        proposal, = document.proposals
        self.assertEqual(proposal.decision.decision, 'pending')
        self.assertEqual(proposal.decision.revision, '')
        self.assertEqual(proposal.decision.patch, '')
        self.assertIn('Comment: ``', form)
        self.assertNotIn('Revision:', form)
        self.assertNotIn('Baseline:', form)
        self.assertEqual(form.count(proposal.patch), 2)
        self.assertIn('덮어쓰거나 같은 요청을 중복하지 마라', prompt)
        self.assertIn('자동 실행의 자기 승인은 금지', prompt)
        self.assertIn('사용자가 특정 결정과 범위를 명시', prompt)
        for decision in ('approve', 'reject', 'replace'):
            decided = ga.parse_guidance_document('## Decision Log\n' + form.replace('Decision: `pending`', f'Decision: `{decision}`'))
            self.assertEqual(decided.proposals[0].decision.decision, decision)
