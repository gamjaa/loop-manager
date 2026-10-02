"""Documents execution policy regression tests."""

from tests.support.execution import ExecutionFixture
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionDocumentsTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_document_execution_guard_preserves_crlf_approved_diff(self):
        original, proposed = b"# Product\r\nOld.\r\n", b"# Product\r\nNew.\r\n"
        draft = SimpleNamespace(
            draft_id="a" * 32, state="approved", target_path="docs/PRODUCT.md",
            decision_revision="b" * 32, original_revision="c" * 40,
            original_hash=hashlib.sha256(original).hexdigest(),
            proposed_hash=hashlib.sha256(proposed).hexdigest(),
            complete_diff=handlers.document_drafts._complete_diff(
                "docs/PRODUCT.md", original, proposed,
            ),
        )
        self.handler.plan = {
            **self.plan,
            "sources": [handlers.document_publication.R027_SOURCE],
        }
        plan_text = handlers.document_publication.planning_instruction(draft)
        repository = mock.Mock()
        repository.assert_current = mock.Mock()
        with (
            mock.patch.object(handlers.document_drafts, "DocumentDraftRepository",
                              return_value=repository),
            mock.patch.object(Path, "read_bytes", return_value=plan_text.encode("utf-8")),
            mock.patch.object(handlers.document_publication, "validate_exact_worktree"),
        ):
            self.handler._document_guard(self.context(), draft)
        repository.assert_current.assert_called_once()


    def test_document_plan_applies_program_bytes_without_worker_agent(self):
        draft = SimpleNamespace(
            draft_id="a" * 32, target_path="docs/PRODUCT.md",
        )
        repository = mock.Mock()
        repository.assert_current = mock.Mock()
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(handlers.document_drafts, 'DocumentDraftRepository',
                              return_value=repository),
            mock.patch.object(handlers.document_publication, 'read_plan_text',
                              return_value='approved plan'),
            mock.patch.object(handlers.document_publication, 'validate_document_plan'),
            mock.patch.object(handlers.document_publication, 'apply_approved_bytes') as apply,
        ):
            outcome = self.handler.run(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        apply.assert_called_once()
        self.effects.agent.run_agent.assert_not_called()
        self.assertIn('실행 가능한 관련 unit test가 없습니다',
                      self.state.row()['worker_result'])


    def test_document_review_requires_explicit_meaning_and_diff_verdicts(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.effects.agent.reviewer_prompt.return_value = 'review prompt'
        self.effects.agent.run_agent.side_effect = lambda *args, **kwargs: lm.AgentResult(
            0, 'VERDICT: PASS', session_id=None,
        )
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard'),
            mock.patch.object(self.handler, '_document_block',
                              return_value=ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK)) as block,
            mock.patch.object(handlers.document_drafts, 'review_artifact', return_value='artifact'),
        ):
            outcome = self.handler.review(self.review_context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIn('명시적으로 확정', block.call_args.args[2])
        self.effects.publication.command.assert_not_called()


    def test_document_review_passes_with_explicit_meaning_and_diff_verdicts(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.effects.agent.reviewer_prompt.return_value = 'review prompt'
        self.effects.agent.run_agent.side_effect = lambda *args, **kwargs: lm.AgentResult(
            0, 'DOCUMENT_MEANING: PASS\nDOCUMENT_DIFF: PASS\nVERDICT: PASS',
            session_id=None,
        )
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard'),
            mock.patch.object(handlers.document_drafts, 'review_artifact', return_value='artifact'),
        ):
            outcome = self.handler.review(self.review_context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')


    def test_document_publication_stops_before_pr_when_original_is_stale(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard', side_effect=
                              handlers.document_drafts.DocumentDraftError('original changed')),
            mock.patch.object(self.handler, '_document_block',
                              return_value=ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK)) as block,
            mock.patch.object(self.handler, '_fetch_latest_base', return_value='base'),
        ):
            outcome = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIn('original changed', str(block.call_args.args[2]))
        self.effects.publication.command.assert_not_called()


    def test_document_uncertainty_persists_audit_and_blocks_plan(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        repository = mock.Mock()
        repository.record_publication_error = mock.Mock()
        with mock.patch.object(
            handlers.document_drafts, 'DocumentDraftRepository',
            return_value=repository,
        ):
            outcome = self.handler._document_block(
                self.context('REVIEWING'), draft, 'meaning uncertain',
            )
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.assertIn('meaning uncertain', repository.record_publication_error.call_args.args[1])


    def test_document_pr_resume_reuses_existing_pr_after_interruption(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.command.side_effect = RuntimeError('interrupted response')
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard') as guard,
        ):
            paused = self.handler.publish(self.context('PUBLISHING'), self.handler)
            self.assertEqual(paused.kind, ExecutionOutcomeKind.RETRY)
            self.effects.publication.pr_for_branch.return_value = {
                'url': 'created', 'headRefOid': 'base',
            }
            self.effects.publication.reconcile_existing_pull_request.return_value = 0
            resumed = self.handler.publish(self.context(), self.handler)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertGreaterEqual(guard.call_count, 2)
        self.assertEqual(self.calls.count('push'), 1)
        self.assertEqual(self.effects.publication.command.call_count, 1)


    def test_document_plan_without_durable_approval_mapping_blocks_before_agent(self):
        self.effects.agent.is_loop_manager_repository.return_value = True
        with mock.patch.object(
            handlers.document_publication, 'read_plan_text',
            return_value='DOCUMENT_DRAFT_ID: ' + 'a' * 32,
        ):
            outcome = self.handler.guard(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.effects.agent.run_agent.assert_not_called()


    def test_document_plan_keeps_approved_pr_free_of_requirement_archive(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.effects.archive.requirement_archive_sources.side_effect = AssertionError(
            'document-only PR must not add an archive move',
        )
        with mock.patch.object(self.handler, '_document_draft', return_value=draft):
            outcome = self.handler.guard(self.context())
        self.assertNotIsInstance(outcome, ExecutionOutcome)
        self.assertEqual(self.handler.archive_sources, ())
        self.effects.archive.requirement_archive_sources.assert_not_called()
