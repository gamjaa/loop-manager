"""The Manager leaves project CLI guidance to the Agent."""
from pathlib import Path
import tempfile
from unittest import TestCase

import loop_manager as lm
from tests.support.execution import ExecutionFixture
from tests.support.scope import AgentScopeFixture


class ExecutionGuidanceTests(ExecutionFixture, TestCase):
    def test_stale_historical_approval_is_preserved_without_blocking_worker(self):
        self.connection.execute(
            "INSERT INTO guidance_approvals (repo, plan_id, proposal_id, proposal_hash, "
            "target_path, scope, proposal_patch, proposal_patch_hash, proposal_line, "
            "decision_line, batch_id, sequence, baseline_content, baseline_mode, "
            "decision, expected_content_hash, invalidated_decision_hash) "
            "VALUES ('repo', 'G-047', 'old', 'old-hash', 'AGENTS.md', 'project', "
            "'invalid historical patch', 'patch-hash', 1, 2, 'old-batch', 0, "
            "x'ff', 420, 'approve', 'stale-hash', 'invalidated')"
        )
        self.connection.commit()
        original = tuple(self.connection.execute('SELECT * FROM guidance_approvals').fetchone())
        result = self.handler.run(self.context())
        self.effects.agent.run_agent.assert_called_once()
        self.assertEqual(self.state.row()["phase"], "VERIFYING")
        self.assertNotEqual(result.kind.value, 'final_block')
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM guidance_approvals').fetchone()), original)

    def test_prompts_use_task_documents_without_cli_guidance_payloads(self):
        plan_path = 'docs/plans/[G-047] Runtime.md'
        prompts = (
            lm.planner_prompt('', ''),
            lm.worker_prompt(plan_path, None, session_recovery=True),
            lm.reviewer_prompt('main'),
        )
        for prompt in prompts:
            self.assertNotIn('AGENTS.md', prompt)
            self.assertNotIn('Agent Guidance Proposal', prompt)
        self.assertIn(plan_path, prompts[1])
        self.assertIn('선택된 최신 ExecPlan과 현재 worktree', prompts[1])


class WorkerPromptFocusTests(TestCase):
    def test_retry_puts_unresolved_feedback_before_protocol_and_ignores_previous_result(self):
        feedback = 'verify:digest occurrence=2 revision=1\nFix the failing assertion'
        prompt = lm.worker_prompt(
            'docs/plans/plan.md', 'Worker 결과 검증 실패: missing title',
            previous_result='already successful work', validation_feedback=feedback,
        )
        self.assertEqual(prompt.count(feedback), 1)
        self.assertLess(prompt.index(feedback), prompt.index('COMMIT_TITLE:'))
        self.assertLess(prompt.index('missing title'), prompt.index('COMMIT_TITLE:'))
        self.assertIn('이미 성공한 작업을 불필요하게 반복', prompt)
        self.assertNotIn('already successful work', prompt)
        self.assertNotIn('실제 diff를', prompt)
        for marker in ('WORKER_OUTCOME:', 'COMMIT_TITLE:', 'PR_SUMMARY:',
                       'TARGETED_TESTS:', 'FAILED_COMMAND:', 'RERUN:'):
            self.assertIn(marker, prompt)
        self.assertIn('전체 verify는 Worker 종료 뒤 Loop Manager가 실행', prompt)

    def test_recovery_reconciles_plan_diff_and_feedback_without_claiming_verification(self):
        feedback = 'review:src/work.py::save::retention occurrence=1 revision=1'
        prompt = lm.worker_prompt(
            'docs/plans/plan.md', None, previous_result='Saved partial implementation',
            validation_feedback=feedback, session_recovery=True,
        )
        self.assertIn('선택된 최신 ExecPlan과 현재 worktree', prompt)
        self.assertIn('계획 목표와 실제 diff를', prompt)
        self.assertIn('이미 수행한 변경과 남은 작업', prompt)
        self.assertIn('다음 필요한 작업부터', prompt)
        self.assertIn('이전 결과만으로 검증 성공을 주장하지 마라', prompt)
        self.assertEqual(prompt.count(feedback), 1)
        self.assertIn('Saved partial implementation', prompt)

    def test_worker_prompt_allows_related_requirement_edits_but_defers_retirement(self):
        prompt = lm.worker_prompt('docs/plans/plan.md', None)
        self.assertNotIn('docs/requirements/ 아래 파일을 수정하거나 이동하지 마라', prompt)
        self.assertIn('Requirement 완료 판단이나 archive 이동 권한을 포함하지 않는다', prompt)
        self.assertIn('User Decision은 사용자를 대신해', prompt)
        self.assertIn('docs/PLANS.md는 이번 Worker 변경 범위가 아니다', prompt)

    def test_normal_turn_has_no_recovery_summary_or_correction_instruction(self):
        prompt = lm.worker_prompt('docs/plans/plan.md', None)
        self.assertNotIn('새 세션에서 이어간다', prompt)
        self.assertNotIn('실제 diff를', prompt)
        self.assertNotIn('미해결 검증 실패와 아래 오류', prompt)


class WorkerCliGuidanceScopeTests(AgentScopeFixture, TestCase):
    def test_worker_can_change_cli_guidance_within_ordinary_worktree_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = lm.capture_git_snapshot(repo)
            (repo / 'AGENTS.md').write_bytes(b'project instructions\xff\n')
            after = lm.capture_git_snapshot(repo)
            lm.validate_agent_scope('worker', repo, before, after, selected_plan_path=plan_path)
            with self.assertRaises(lm.AgentScopeError):
                lm.validate_agent_scope('reviewer', repo, before, after)
