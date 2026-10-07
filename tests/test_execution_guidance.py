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
