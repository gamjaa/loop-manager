"""The Manager leaves project CLI guidance to the Agent."""
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import tempfile
from unittest import TestCase, mock

import loop_manager as lm
from tests.support.execution import ExecutionFixture
from tests.support.scope import AgentScopeFixture, snapshot


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
    def test_manual_correction_context_omits_raw_results_and_does_not_repeat_documents(self):
        from loop_manager_internal.execution.manual_candidate import ManualCandidate
        from loop_manager_internal.storage.manual_work_state import ensure_schema
        from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository, ensure_schema as feedback_schema
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        ensure_schema(connection)
        feedback_schema(connection)
        manual = ManualCandidate(connection, 'repo', 'G-001', mock.Mock(), mock.Mock())
        group = {'group_id': 'group', 'pending_source_head': None, 'source_update_conflict': False,
                 'documents': json.dumps([{'path': 'G-001.md', 'text': 'first G'},
                                          {'path': 'G-002.md', 'text': 'second G'}])}
        def stage(name, result, raw, revision):
            connection.execute('INSERT INTO manual_work_results VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('repo', 'group', 'G-001', name, 'head', 'base', revision,
                 result, raw, 'request', revision))
            connection.commit()
        stage('verify', 'PASS', 'PRIVATE VERIFY PASS LOG', '1')
        connection.execute(
            "INSERT INTO manual_work_requests (request_id,repo,group_id,source_head,base_sha,"
            "input_token,skip_verify,skip_review,reason,created_at) "
            "VALUES ('request','repo','group','head','base','token',0,1,?, 'now')",
            ('user requested omission',))
        stage('review', 'SKIPPED_BY_USER', 'skip; user requested omission', '2')
        feedback = ValidationFeedbackRepository(connection)
        with mock.patch.object(manual, 'enabled', return_value=True), mock.patch.object(manual, 'group', return_value=group):
            context = manual.worker_context()
            first = feedback.prepare('repo', 'G-001', 'session', context=context)
            text = '\n'.join(first.context.values())
            self.assertIn('first G', text)
            self.assertIn('second G', text)
            self.assertIn('SKIPPED_BY_USER', text)
            self.assertIn('제공 사유: user requested omission', text)
            self.assertNotIn('skip; user requested omission', text)
            self.assertIn('외부 targeted test 결과는 확인되지 않았으며', text)
            self.assertNotIn('PRIVATE VERIFY PASS LOG', text)
            feedback.delivered(first.attempt_id, 'session')
            stage('verify', 'FAIL', 'PRIVATE VERIFY FAILURE LOG', '3')
            delta = feedback.prepare('repo', 'G-001', 'session', context=manual.worker_context())
            self.assertEqual(set(delta.context), {'manual_stage:G-001:verify'})
            self.assertIn('FAIL', '\n'.join(delta.context.values()))
            self.assertNotIn('PRIVATE VERIFY FAILURE LOG', '\n'.join(delta.context.values()))
            replacement = feedback.prepare('repo', 'G-001', None, recovery=True, context=manual.worker_context())
            self.assertIn('manual_documents', replacement.context)

    def test_retry_puts_unresolved_feedback_before_protocol_and_ignores_previous_result(self):
        feedback = 'verify:digest occurrence=2 revision=1\nFix the failing assertion'
        prompt = lm.worker_prompt(
            'docs/plans/plan.md', 'Worker 결과 검증 실패: missing title',
            previous_result='already successful work', validation_feedback=feedback,
        )
        self.assertEqual(prompt.count(feedback), 1)
        self.assertLess(prompt.index(feedback), prompt.index('COMMIT_TITLE:'))
        self.assertLess(prompt.index('missing title'), prompt.index('COMMIT_TITLE:'))
        self.assertIn('미해결 원인에 관련된 변경', prompt)
        self.assertNotIn('already successful work', prompt)
        self.assertNotIn('실제 diff를', prompt)
        for marker in ('WORKER_OUTCOME:', 'COMMIT_TITLE:', 'PR_SUMMARY:',
                       'TARGETED_TESTS:'):
            self.assertIn(marker, prompt)
        self.assertNotIn('전체 verify는 Worker 종료 뒤 Loop Manager가 실행', prompt)
        self.assertNotIn('FAILED_COMMAND:', prompt)

    def test_recovery_reconciles_plan_diff_and_feedback_without_claiming_verification(self):
        feedback = 'review:src/work.py::save::retention occurrence=1 revision=1'
        prompt = lm.worker_prompt(
            'docs/plans/plan.md', None, previous_result='Saved partial implementation',
            validation_feedback=feedback, session_recovery=True,
        )
        self.assertIn('선택된 최신 ExecPlan과 현재 worktree', prompt)
        self.assertIn('현재 worktree의 실제 diff', prompt)
        self.assertIn('이미 수행한 변경과 남은 작업', prompt)
        self.assertIn('다음 필요한 작업부터', prompt)
        self.assertNotIn('이전 결과', prompt)
        self.assertEqual(prompt.count(feedback), 1)
        self.assertNotIn('Saved partial implementation', prompt)

    def test_worker_user_message_keeps_task_input_separate_from_static_instruction(self):
        prompt = lm.worker_prompt('docs/plans/plan.md', None)
        for phrase in ('frontmatter', 'sources 문자열', 'scripts/verify', 'User Decision',
                       'Requirement 완료 판단', '당신은 Loop Manager의 Worker'):
            self.assertNotIn(phrase, prompt)
        self.assertIn('docs/plans/plan.md', prompt)
        self.assertIn('WORKER_OUTCOME:', prompt)


    def test_normal_turn_has_no_recovery_summary_or_correction_instruction(self):
        prompt = lm.worker_prompt('docs/plans/plan.md', None)
        self.assertNotIn('새 세션에서 이어간다', prompt)
        self.assertNotIn('실제 diff를', prompt)
        self.assertNotIn('미해결 검증 실패와 아래 오류', prompt)


class WorkerCliGuidanceScopeTests(AgentScopeFixture, TestCase):
    def test_worker_can_change_cli_guidance_within_ordinary_worktree_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            plan_path = 'docs/plans/[G-047] Runtime.md'
            before = snapshot()
            after = replace(before, dirty={'AGENTS.md': (('untracked',), 'binary-guidance')})
            (repo / 'AGENTS.md').write_bytes(b'project instructions\xff\n')
            with (mock.patch.object(lm, 'capture_git_snapshot',
                                    side_effect=(before, after)) as observe,
                  mock.patch('subprocess.Popen',
                             side_effect=AssertionError('예상 밖 Git process 실행'))):
                self.assertEqual(lm.capture_git_snapshot(repo), before)
                self.assertEqual(lm.capture_git_snapshot(repo), after)
                lm.validate_agent_scope('worker', repo, before, after,
                                        selected_plan_path=plan_path)
                self.assertEqual(observe.call_count, 2)
