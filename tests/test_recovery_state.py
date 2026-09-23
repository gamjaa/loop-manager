import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from recovery_runtime import RecoveryAction, RecoveryObservation, RecoveryReason
from recovery_state import RecoveryRepository, RecoveryStateConflict
from recovery_dispatcher import RecoveryDispatcher


class RecoveryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.repository = RecoveryRepository(self.connection)

    def observation(self, progress="worker:head", fingerprint="conflict"):
        return RecoveryObservation(
            RecoveryReason.REBASE_CONFLICT, "repo", "G-042", "IMPLEMENTING",
            "plan-sha", "base-sha", fingerprint, progress, "conflict remains",
        )

    def test_occurrence_progress_cas_and_final_evidence_survive_repository_restart(self):
        first = self.repository.observe(self.observation())
        self.assertEqual(first.decision.action, RecoveryAction.CORRECT)
        with self.assertRaises(RecoveryStateConflict):
            self.repository.observe(self.observation(), expected_generation=0)
        second = RecoveryRepository(self.connection).observe(
            self.observation(), expected_generation=first.generation)
        self.assertEqual(second.decision.occurrence, 2)
        self.repository.observe(RecoveryObservation(
            RecoveryReason.GIT_LOCK, "repo", "G-042", "IMPLEMENTING",
            "plan-sha", "base-sha", "lock", "worker:head", "lock busy",
        ))
        third = self.repository.observe(
            self.observation(), expected_generation=second.generation + 1)
        self.assertEqual(third.decision.occurrence, 3)
        self.assertEqual(third.decision.action, RecoveryAction.CORRECT)
        final = self.repository.observe(
            self.observation(), expected_generation=third.generation)
        self.assertEqual(final.decision.action, RecoveryAction.FINAL_BLOCK)
        self.assertEqual(final.final_evidence["occurrence"], 4)
        self.assertEqual(final.final_evidence["last_stable_revision"], "base-sha")
        self.assertEqual(final.final_evidence["recovery_attempts"]["git_lock"], 1)
        self.assertEqual(final.final_evidence["recovery_attempts"]["rebase_conflict"], 3)
        progressed = self.repository.observe(self.observation(progress="worker:new-head"))
        self.assertEqual(progressed.decision.occurrence, 1)
        self.assertEqual(progressed.decision.action, RecoveryAction.CORRECT)
        self.assertIsNone(progressed.final_evidence)

    def test_transient_pause_preserves_only_recovery_record(self):
        observation = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
            "plan-sha", "base-sha", "fetch", "worker:head", "service unavailable",
        )
        record = self.repository.observe(observation, attempts=3)
        self.assertEqual(record.decision.action, RecoveryAction.PAUSE)
        self.assertEqual(self.repository.row("repo", "G-042")["pending_action"], "pause")
        self.assertIsNone(record.final_evidence)

    def test_project_recovery_before_execplan_schema_is_durable(self):
        self.connection.execute(loop_manager.SCHEMA)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )

        decision = loop_manager._record_project_recovery(
            self.connection, "repo", RecoveryReason.GIT_LOCK,
            "index.lock exists", attempts=3,
        )

        self.assertEqual(decision.action, RecoveryAction.PAUSE)
        row = self.repository.row("repo", None)
        self.assertEqual((row["reason_code"], row["pending_action"]),
                         ("git_lock", "pause"))

    def test_process_run_pauses_after_three_identical_remote_races(self):
        repo = str(Path("repo").resolve())
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, phase) "
            "VALUES (?, 'main', 'WORKING')", (repo,)
        )
        self.connection.commit()
        observation = RecoveryObservation(
            RecoveryReason.REMOTE_SNAPSHOT_RACE, repo, "G-042", "VERIFYING",
            "plan-sha", "base-sha", "remote-race", "worker:head", "base moved",
        )
        calls = []

        def step(*_args):
            calls.append(1)
            RecoveryRepository(self.connection).observe(observation)
            return loop_manager.PROJECT_STEP_RETRY

        with mock.patch("loop_manager._run_project_step", side_effect=step):
            result = loop_manager.run_project_result(
                self.connection, Path("/unused-state"), repo, Path("/unused.ini"),
                "main", False,
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(result.exit_code, 0)
        row = self.repository.row(repo, "G-042")
        self.assertEqual((row["pending_action"], row["attempts"]), ("pause", 3))

    def test_process_run_bounds_remote_race_even_when_revision_changes(self):
        repo = str(Path("repo").resolve())
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, phase) VALUES (?, 'main', 'WORKING')",
            (repo,),
        )
        self.connection.commit()
        attempts = []

        def step(*_args):
            attempts.append(len(attempts) + 1)
            RecoveryRepository(self.connection).observe(RecoveryObservation(
                RecoveryReason.REMOTE_SNAPSHOT_RACE, repo, "G-042",
                "VERIFYING", "plan-sha", f"base-{attempts[-1]}",
                f"race-{attempts[-1]}", "worker:head", "base moved",
            ))
            return loop_manager.PROJECT_STEP_RETRY

        with mock.patch("loop_manager._run_project_step", side_effect=step):
            result = loop_manager.run_project_result(
                self.connection, Path("/unused-state"), repo, Path("/unused.ini"),
                "main", False,
            )
        self.assertEqual(len(attempts), 3)
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)

    def test_final_block_effect_failure_rolls_back_recovery_record(self):
        observation = RecoveryObservation(
            RecoveryReason.GUIDANCE_APPROVAL_REQUIRED, "repo", "G-042",
            "IMPLEMENTING", "plan-sha", "base-sha", "guidance", "worker:head",
            "decision pending",
        )
        calls = []

        def failing_block(*_args, **_kwargs):
            calls.append(1)
            raise RuntimeError("block transaction interrupted")

        dispatcher = RecoveryDispatcher(
            self.connection, failing_block, failing_block,
        )
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            dispatcher.apply(
                observation, worktree=Path("/worktree"), repo_path=Path("/repo"),
                revision="base-sha", verify_result="",
            )
        self.assertEqual(calls, [1])
        self.assertIsNone(self.repository.row("repo", "G-042"))

    def test_nonfinal_dispatch_never_calls_checkpoint_or_block_effect(self):
        observation = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042",
            "VERIFYING", "plan-sha", "base-sha", "fetch", "worker:head",
            "temporary fetch error",
        )
        blocked = mock.Mock(side_effect=AssertionError("block called"))
        dispatcher = RecoveryDispatcher(self.connection, blocked, blocked)
        decision = dispatcher.apply(
            observation, worktree=Path("/worktree"), repo_path=Path("/repo"),
            revision="base-sha", verify_result="saved verify",
        )
        self.assertEqual(decision.action, RecoveryAction.RETRY)
        blocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
