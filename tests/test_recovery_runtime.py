import unittest
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.git import rebase_block_reconciliation as rebase_blocks
from loop_manager_internal.storage.recovery_state import RecoveryRepository
from loop_manager_internal.planning.planning_runtime import PlanningOutcome, PlanningOutcomeKind

from loop_manager_internal.recovery.recovery_runtime import (
    RecoveryAction, RecoveryDisposition, RecoveryFailure, RecoveryObservation,
    RecoveryReason, classify_exception, decide, reason_from_block, require_final,
)
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode, BlockScope, reason_scope


def observation(reason, detail="same RuntimeError"):
    return RecoveryObservation(
        reason, "repo", "G-042", "VERIFYING", "plan-sha", "base-sha",
        "fingerprint", "worker-session:head", detail,
    )


class RecoveryPolicyTests(unittest.TestCase):
    def test_unproven_git_history_waits_without_user_action_or_timer_retry(self):
        decision = decide(observation(RecoveryReason.GIT_INPUT_UNCERTAIN), attempts=4001)
        self.assertEqual(decision.disposition, RecoveryDisposition.CONDITION_WAIT)
        self.assertEqual(decision.action, RecoveryAction.PAUSE)
        self.assertIsNone(decision.block_reason)

    def test_verified_rebase_can_resolve_final_record_without_erasing_failure_history(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = RecoveryRepository(connection)
        detail = rebase_blocks.LEGACY_COMPLETION_DIAGNOSTIC
        saved = RecoveryObservation(
            RecoveryReason.STATUS_FAILED, 'repo', 'G-101', 'VERIFYING',
            'plan', 'base', 'old-fingerprint', 'base|worker|', detail,
        )
        repository.observe(saved)
        repository.observe(
            replace(saved, reason=RecoveryReason.OWNERSHIP_UNCERTAIN),
            final_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
        )
        repository.resolve_rebase_evidence_failure(
            'repo', 'G-101', base_revision='base', worker_session='worker',
            released_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
        )
        resolved = repository.row('repo', 'G-101')
        self.assertEqual(resolved['pending_action'], 'reconcile')
        self.assertEqual(resolved['reason_code'], 'ownership_uncertain')
        self.assertEqual(len(json.loads(resolved['occurrence_index'])), 2)

    def test_rebase_block_selector_requires_linked_cause_but_not_historical_records(self):
        recovery = {
            "reason_code": RecoveryReason.STATUS_FAILED.value,
            "detail": rebase_blocks.LEGACY_COMPLETION_DIAGNOSTIC,
            "pending_action": "pause",
        }
        checkpoint = SimpleNamespace(
            operation="rebase", expected_head="before", target_base_oid="base",
        )
        block = {
            "reason_code": BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
            "evidence": rebase_blocks.LEGACY_COMPLETION_DIAGNOSTIC,
        }
        self.assertTrue(rebase_blocks.could_be_rebase_evidence_block(block))
        self.assertTrue(rebase_blocks.block_matches(block, None, checkpoint))
        self.assertTrue(rebase_blocks.block_matches(block, recovery, checkpoint))
        self.assertTrue(rebase_blocks.block_matches(
            block, {**recovery, "reason_code": RecoveryReason.REVIEW_EXHAUSTED.value},
            checkpoint,
        ))
        self.assertTrue(rebase_blocks.block_matches(
            {**block, "reason_code": BlockReasonCode.DEFAULT_WORKTREE_MUTATION.value},
            recovery, checkpoint,
        ))
        self.assertFalse(rebase_blocks.block_matches(
            {**block, "reason_code": BlockReasonCode.DEFAULT_WORKTREE_MUTATION.value},
            recovery, SimpleNamespace(operation=None, expected_head='before', target_base_oid='base'),
        ))
        self.assertFalse(rebase_blocks.block_matches(
            {**block, "evidence": "another safety failure"}, recovery, checkpoint,
        ))
        self.assertFalse(rebase_blocks.block_matches(
            block, recovery, SimpleNamespace(
                operation=None, expected_head="before", target_base_oid="base",
            ),
        ))
        final = {
            **recovery,
            "reason_code": RecoveryReason.OWNERSHIP_UNCERTAIN.value,
            "pending_action": "final_block",
            "final_evidence": json.dumps({
                "block_reason_code": block["reason_code"],
            }),
            "progress_marker": "base|worker|",
            "occurrence_index": json.dumps({
                json.dumps(["status_failed", "old", "base|worker|"]): 1,
            }),
        }
        self.assertTrue(rebase_blocks.block_matches(
            block, final, checkpoint,
        ))
        self.assertTrue(rebase_blocks.block_matches(
            block, {**final, "occurrence_index": "{}"}, checkpoint,
        ))

    def test_review_feedback_occurrence_never_blocks_and_budget_pauses(self):
        correction = decide(
            observation(RecoveryReason.REVIEW_FEEDBACK_CORRECTION),
            occurrence=99,
        )
        budget = decide(
            observation(RecoveryReason.REVIEW_CORRECTION_BUDGET), attempts=3,
        )
        self.assertEqual(correction.action, RecoveryAction.CORRECT)
        self.assertIsNone(correction.block_reason)
        self.assertEqual(budget.action, RecoveryAction.PAUSE)
        self.assertIsNone(budget.block_reason)

    def test_each_disposition_and_action_are_independent_of_block_scope(self):
        cases = (
            (RecoveryReason.FETCH_FAILED, 1, 0, RecoveryDisposition.TRANSIENT_RETRY, RecoveryAction.RETRY),
            (RecoveryReason.FETCH_FAILED, 1, 3, RecoveryDisposition.TRANSIENT_RETRY, RecoveryAction.PAUSE),
            (RecoveryReason.CATALOG_CACHE_STALE, 1, 0, RecoveryDisposition.STATE_RECONCILE, RecoveryAction.RECONCILE),
            (RecoveryReason.ARCHIVE_MOVE_MISMATCH, 2, 0, RecoveryDisposition.AGENT_CORRECTION, RecoveryAction.CORRECT),
            (RecoveryReason.ARCHIVE_MOVE_MISMATCH, 3, 0, RecoveryDisposition.USER_ACTION_REQUIRED, RecoveryAction.FINAL_BLOCK),
            (RecoveryReason.REBASE_CONFLICT, 3, 0, RecoveryDisposition.AGENT_CORRECTION, RecoveryAction.CORRECT),
            (RecoveryReason.REBASE_CONFLICT, 4, 0, RecoveryDisposition.CONDITION_WAIT, RecoveryAction.PAUSE),
            (RecoveryReason.GUIDANCE_APPROVAL_REQUIRED, 1, 0, RecoveryDisposition.USER_ACTION_REQUIRED, RecoveryAction.FINAL_BLOCK),
            (RecoveryReason.REMOTE_OID_UNCERTAIN, 1, 0, RecoveryDisposition.USER_ACTION_REQUIRED, RecoveryAction.FINAL_BLOCK),
        )
        for reason, occurrence, attempts, disposition, action in cases:
            with self.subTest(reason=reason, occurrence=occurrence, attempts=attempts):
                decision = decide(observation(reason), occurrence=occurrence, attempts=attempts)
                self.assertEqual((decision.disposition, decision.action), (disposition, action))
                self.assertEqual(decision.scope is None, action is not RecoveryAction.FINAL_BLOCK)
        self.assertEqual(decide(observation(RecoveryReason.GUIDANCE_APPROVAL_REQUIRED)).scope, BlockScope.PLAN)
        self.assertEqual(decide(observation(RecoveryReason.REMOTE_OID_UNCERTAIN)).scope, BlockScope.PROJECT)

    def test_same_runtime_error_container_uses_operation_code_not_message(self):
        error = RuntimeError("identical diagnostic")
        reasons = (RecoveryReason.FETCH_FAILED, RecoveryReason.OWNERSHIP_UNCERTAIN)
        actions = [decide(observation(classify_exception(error, operation=reason))).action
                   for reason in reasons]
        self.assertEqual(actions, [RecoveryAction.RETRY, RecoveryAction.FINAL_BLOCK])
        typed = RecoveryFailure(RecoveryReason.REBASE_CONTINUE_FAILED, "identical diagnostic")
        self.assertEqual(classify_exception(typed, operation=RecoveryReason.OWNERSHIP_UNCERTAIN),
                         RecoveryReason.REBASE_CONTINUE_FAILED)

    def test_rebase_correction_budget_cannot_authorize_a_final_conflict_block(self):
        for limit in (1, 4, 20):
            for occurrence in (limit, limit + 1, 4001):
                with self.subTest(limit=limit, occurrence=occurrence):
                    decision = decide(
                        observation(RecoveryReason.REBASE_CONFLICT),
                        occurrence=occurrence, attempts=4001,
                        correction_limit=limit,
                        final_block_reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
                    )
                    self.assertEqual(decision.action, RecoveryAction.PAUSE)
                    self.assertEqual(decision.disposition, RecoveryDisposition.CONDITION_WAIT)
                    self.assertIsNone(decision.block_reason)
                    self.assertIsNone(decision.scope)
                    self.assertIsNone(decision.unresolved)
                    with self.assertRaises(ValueError):
                        require_final(decision)

    def test_only_final_decision_passes_blocking_gate(self):
        with self.assertRaises(ValueError):
            require_final(decide(observation(RecoveryReason.GIT_LOCK)))
        final = decide(observation(RecoveryReason.NO_CHANGE))
        self.assertIs(require_final(final), final)
        waiting = decide(observation(RecoveryReason.COMMON_DEFECT))
        self.assertEqual(waiting.disposition, RecoveryDisposition.CONDITION_WAIT)
        self.assertEqual(waiting.block_reason, BlockReasonCode.COMMON_DEFECT_PENDING)
        self.assertIs(require_final(waiting), waiting)
        with self.assertRaises(ValueError):
            require_final(replace(waiting, block_reason=BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED))

    def test_every_legacy_block_reason_keeps_its_cause_and_scope(self):
        for block_reason in BlockReasonCode:
            with self.subTest(block_reason=block_reason):
                reason = reason_from_block(block_reason)
                decision = decide(observation(reason))
                if block_reason is BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL:
                    # A current isolation proof selects this new code explicitly;
                    # the shared typed cause must retain its legacy project gate.
                    self.assertEqual(decision.block_reason, BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN)
                    self.assertEqual(decision.scope, BlockScope.PROJECT)
                    decision = decide(observation(reason), final_block_reason=block_reason)
                self.assertEqual(decision.action, RecoveryAction.FINAL_BLOCK)
                self.assertEqual(decision.block_reason, block_reason)
                self.assertEqual(decision.scope, reason_scope(block_reason))

    def test_recovery_identity_deduplicates_same_cause_across_phase(self):
        first = observation(RecoveryReason.NO_CHANGE)
        self.assertEqual(first.identity, replace(first, phase="PUBLISHING").identity)
        self.assertNotEqual(
            first.identity,
            replace(first, base_revision="new-base-sha").identity,
        )

    def test_git_adapter_keeps_operation_cause_in_identical_runtime_error(self):
        with mock.patch("loop_manager.git", side_effect=RuntimeError("same failure")):
            for command, expected in (
                ("fetch", RecoveryReason.FETCH_FAILED),
                ("status", RecoveryReason.STATUS_FAILED),
                ("add", RecoveryReason.STAGE_FAILED),
            ):
                with self.subTest(command=command):
                    with self.assertRaises(RecoveryFailure) as raised:
                        loop_manager._typed_execution_git(Path("/repo"), command)
                    self.assertEqual(raised.exception.reason, expected)

    def test_deferred_planning_remote_race_is_nonblocking_pause(self):
        outcome = PlanningOutcome(
            PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
            detail="base moved",
        )
        self.assertEqual(
            loop_manager._planning_outcome_step_result(outcome),
            loop_manager.PROJECT_STEP_TRANSIENT_PAUSE,
        )

    def test_nonzero_git_operation_result_carries_lock_code(self):
        result = SimpleNamespace(
            returncode=1,
            stdout="fatal: Unable to create '/repo/.git/index.lock': File exists",
        )
        with mock.patch("loop_manager.git", return_value=result):
            with self.assertRaises(RecoveryFailure) as raised:
                loop_manager._typed_execution_git(
                    Path("/repo"), "rebase", "--continue", check=False,
                )
        self.assertEqual(raised.exception.reason, RecoveryReason.GIT_LOCK)


if __name__ == "__main__":
    unittest.main()
