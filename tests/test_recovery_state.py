import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
import runtime_state
from recovery_runtime import RecoveryAction, RecoveryObservation, RecoveryReason
from recovery_state import (
    RecoveryRepository, RecoveryStateConflict, begin_external_run,
    pending_review_correction, reserve_review_correction, start_review_correction,
)
from recovery_dispatcher import RecoveryDispatcher
from tests import test_runtime_state


class RecoveryRepositoryTests(unittest.TestCase):
    def test_registered_project_reentry_shares_budget_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.db'
            for result_id, expected in ((1, 1), (2, 2)):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                RecoveryRepository(connection)
                begin_external_run(connection, 'same-scheduler-run')
                self.assertEqual(
                    reserve_review_correction(
                        connection, 'repo', 'G-042', result_id,
                    ), (True, expected, 'same-scheduler-run'),
                )
                connection.close()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            begin_external_run(connection, 'next-scheduler-run')
            self.assertEqual(
                reserve_review_correction(connection, 'repo', 'G-042', 2),
                (True, 1, 'next-scheduler-run'),
            )
            connection.close()

    def test_review_budget_counts_distinct_review_results_once_per_external_run(self):
        self.connection.execute(
            "CREATE TABLE validation_results (id INTEGER PRIMARY KEY, repo TEXT, "
            "plan_id TEXT, stage TEXT, classification TEXT, raw_output TEXT)"
        )
        run = begin_external_run(self.connection)
        for result_id in range(1, 5):
            diagnostic = f"ISSUE_ID: a.py::issue-{result_id}::blocking"
            self.connection.execute(
                "INSERT INTO validation_results VALUES (?, 'repo', 'G-042', "
                "'review', 'failure', ?)", (result_id, diagnostic),
            )
            self.assertEqual(
                pending_review_correction(self.connection, 'repo', 'G-042', diagnostic),
                result_id,
            )
            allowed, used, identity = reserve_review_correction(
                self.connection, 'repo', 'G-042', result_id,
            )
            self.assertEqual((allowed, used, identity), (result_id <= 3, min(result_id, 3), run))
            if result_id <= 3:
                start_review_correction(
                    self.connection, 'repo', 'G-042', run, result_id,
                )
                start_review_correction(
                    self.connection, 'repo', 'G-042', run, result_id,
                )
                self.assertEqual(
                    reserve_review_correction(self.connection, 'repo', 'G-042', result_id),
                    (True, result_id, run),
                )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM review_correction_turns WHERE started_at IS NOT NULL"
        ).fetchone()[0], 3)
        self.connection.execute(
            "INSERT INTO validation_results VALUES (5, 'repo', 'G-042', "
            "'verify', 'failure', 'verify failure')"
        )
        self.assertIsNone(pending_review_correction(
            self.connection, 'repo', 'G-042', diagnostic,
        ))
        next_run = begin_external_run(self.connection)
        self.assertNotEqual(next_run, run)
        self.assertEqual(
            reserve_review_correction(self.connection, 'repo', 'G-042', 4),
            (True, 1, next_run),
        )

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

    def test_external_runs_count_once_and_reset_on_progress_or_success(self):
        def observe_run(observation, repeats=1):
            before = self.repository.external_run_snapshot("repo")
            for _ in range(repeats):
                self.repository.observe(observation)
            self.repository.observe_external_run("repo", before)

        failure = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
            "plan-sha", "base-sha", "fetch-failure", "head-a", "fetch failed",
        )
        observe_run(failure, repeats=3)
        first = self.connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(first), (1, 0))

        observe_run(failure)
        observe_run(failure)
        third = self.connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(third), (3, 1))

        observe_run(RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
            "plan-sha", "base-sha", "fetch-failure", "head-b", "fetch failed",
        ))
        progressed = self.connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(progressed), (1, 0))

        before_success = self.repository.external_run_snapshot("repo")
        self.repository.observe_external_run("repo", before_success)
        resolved = self.connection.execute(
            "SELECT active, consecutive_runs FROM persistent_execution_failures "
            "WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(resolved), (0, 0))

    def test_final_block_is_suppressed_from_persistent_notification(self):
        before = self.repository.external_run_snapshot("repo")
        self.repository.observe(RecoveryObservation(
            RecoveryReason.GUIDANCE_APPROVAL_REQUIRED, "repo", "G-042",
            "IMPLEMENTING", "plan-sha", "base-sha", "guidance", "head-a",
            "decision pending",
        ))
        self.repository.observe_external_run(
            "repo", before, blocked_subjects=frozenset({"G-042"}),
        )
        row = self.connection.execute(
            "SELECT notification_eligible, suppressed_by_block FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(row), (0, 1))

    def test_external_failure_streak_survives_interrupted_database_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            observation = RecoveryObservation(
                RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
                "plan-sha", "base-sha", "fetch-failure", "head-a", "fetch failed",
            )
            for _ in range(3):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                repository = RecoveryRepository(connection)
                begin_external_run(connection)
                repository.observe(observation)
                connection.commit()
                connection.close()

            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT consecutive_runs, notification_eligible FROM "
                "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
            ).fetchone()
            connection.close()
        self.assertEqual(tuple(row), (3, 1))

    def test_third_interrupted_observation_commits_outbox_before_run_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            repo = "repo"
            failure = RecoveryObservation(
                RecoveryReason.FETCH_FAILED, repo, None, "WORKING", None,
                "base-sha", "fetch-failure", "head-a", "fetch failed",
            )
            for _ in range(3):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)
                loop_manager._ensure_execplans_schema(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO projects (repo, base_branch) "
                    "VALUES (?, 'main')", (repo,),
                )
                loop_manager.notifications.configure_session(connection, enabled=True)
                begin_external_run(connection)
                connection.commit()
                with runtime_state._transaction(connection):
                    RecoveryRepository(connection).observe(failure)
                # Closing here models a process that never reaches finish_external_run.
                connection.close()

            connection = sqlite3.connect(database)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE repo = ? AND event_type = 'persistent_execution_failure'",
                (repo,),
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT consecutive_runs, notification_queued FROM "
                "persistent_execution_failures WHERE repo = ?", (repo,),
            ).fetchone(), (3, 1))
            connection.close()

    def test_final_block_withdraws_immediate_pending_failure_notice(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )
        loop_manager.notifications.configure_session(connection, enabled=True)
        run_id = begin_external_run(connection)
        repository = RecoveryRepository(connection)
        repository.observe(RecoveryObservation(
            RecoveryReason.GUIDANCE_APPROVAL_REQUIRED, "repo", "G-042",
            "IMPLEMENTING", "plan-sha", "base-sha", "guidance", "head-a",
            "decision pending",
        ))
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], 1)

        # BLOCKED notification creation may commit before run cleanup.
        loop_manager.notifications.suppress_persistent_failure_for_block(
            connection, "repo", "G-042",
        )
        connection.commit()
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], 0)
        repository.finish_external_run(
            "repo", run_id, resolved=False,
            blocked_subjects=frozenset({"G-042"}),
        )
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], 0)

    def test_external_failure_projection_supports_tuple_rows(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = RecoveryRepository(connection)
        before = repository.external_run_snapshot("repo")
        repository.observe(RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", None, "WORKING", None,
            "base-sha", "fetch-failure", "head-a", "fetch failed",
        ))

        repository.observe_external_run("repo", before)

        row = connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = '__project__'"
        ).fetchone()
        self.assertEqual(row, (1, 0))

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
