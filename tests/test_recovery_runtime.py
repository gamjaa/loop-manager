import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import loop_manager
from planning_runtime import PlanningOutcome, PlanningOutcomeKind

from recovery_runtime import (
    RecoveryAction, RecoveryDisposition, RecoveryFailure, RecoveryObservation,
    RecoveryReason, classify_exception, decide, reason_from_block, require_final,
)
from scoped_blocking import BlockReasonCode, BlockScope, reason_scope


def observation(reason, detail="same RuntimeError"):
    return RecoveryObservation(
        reason, "repo", "G-042", "VERIFYING", "plan-sha", "base-sha",
        "fingerprint", "worker-session:head", detail,
    )


class RecoveryPolicyTests(unittest.TestCase):
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
            (RecoveryReason.REBASE_CONFLICT, 4, 0, RecoveryDisposition.USER_ACTION_REQUIRED, RecoveryAction.FINAL_BLOCK),
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

    def test_only_final_decision_passes_blocking_gate(self):
        with self.assertRaises(ValueError):
            require_final(decide(observation(RecoveryReason.GIT_LOCK)))
        final = decide(observation(RecoveryReason.NO_CHANGE))
        self.assertIs(require_final(final), final)

    def test_every_legacy_block_reason_keeps_its_cause_and_scope(self):
        for block_reason in BlockReasonCode:
            with self.subTest(block_reason=block_reason):
                reason = reason_from_block(block_reason)
                decision = decide(observation(reason))
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
