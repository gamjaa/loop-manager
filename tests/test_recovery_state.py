import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
import runtime_state
from recovery_runtime import RecoveryAction, RecoveryObservation, RecoveryReason
from recovery_state import RecoveryRepository, RecoveryStateConflict
from recovery_dispatcher import RecoveryDispatcher
from tests import test_runtime_state


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

    def test_validation_success_resets_only_its_correction_streak(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository = RecoveryRepository(connection)

        def correction(fingerprint):
            return RecoveryObservation(
                RecoveryReason.WORKER_CORRECTION, 'repo', 'G-047',
                'VERIFYING', 'plan-sha', 'base', fingerprint,
                'worker-session:head', 'same validation failure',
            )

        verify = correction('verify-fingerprint')
        review = correction('review-fingerprint')
        rebase = RecoveryObservation(
            RecoveryReason.REBASE_CONFLICT, 'repo', 'G-047', 'IMPLEMENTING',
            'plan-sha', 'base', 'verify-fingerprint',
            'worker-session:head', 'conflict remains',
        )
        repository.observe(verify)
        repository.observe(verify)
        repository.observe(review)
        repository.observe(rebase)
        connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, "
            "review_failure_history = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            (json.dumps({'verify-fingerprint': {'occurrences': 2, 'sample': 'failure'}}),
             json.dumps({'review-fingerprint': {'occurrences': 1, 'sample': 'failure'}})),
        )
        connection.commit()

        loop_manager.clear_validation_failure_history(connection, 'repo', 'verify')

        plan = connection.execute(
            "SELECT verify_failure_history, review_failure_history FROM exec_plans "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(json.loads(plan['verify_failure_history']), {})
        self.assertIn('review-fingerprint', json.loads(plan['review_failure_history']))
        self.assertEqual(repository.observe(verify).decision.occurrence, 1)
        self.assertEqual(repository.observe(review).decision.occurrence, 2)
        self.assertEqual(repository.observe(rebase).decision.occurrence, 2)

        loop_manager.clear_validation_failure_history(connection, 'repo', 'review')

        self.assertEqual(repository.observe(review).decision.occurrence, 1)
        self.assertEqual(repository.observe(verify).decision.occurrence, 2)

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
