import sqlite3
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.worker import dummy_worker_sessions


class PersistentExecutionFailureBoundaryTest(unittest.TestCase):
    @dummy_worker_sessions
    def test_successful_run_resolves_its_observed_retry(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repo = str(Path("resolved-failure-repo").resolve())
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,)
        )
        loop_manager.notifications.configure_session(connection, enabled=True)
        connection.commit()

        def recovered_run(*_args, **_kwargs):
            from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
            from loop_manager_internal.storage.recovery_state import RecoveryRepository

            RecoveryRepository(connection).observe(RecoveryObservation(
                RecoveryReason.FETCH_FAILED, repo, None, "WORKING", None,
                "base-sha", "fetch-failure", "head-a", "fetch failed",
            ))
            return loop_manager.ProjectRunResult(loop_manager.ProjectRunKind.WAITING, 0)

        with mock.patch("loop_manager._run_project_result", side_effect=recovered_run):
            for _ in range(3):
                loop_manager.run_project_result(
                    connection, Path("/unused-state"), repo, Path("/unused.ini"),
                    "main", False,
                )
        diagnostic = connection.execute(
            "SELECT active, consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = ? AND subject = '__project__'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(diagnostic), (0, 0, 0))
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE repo = ?", (repo,)
            ).fetchone()[0],
            0,
        )

    @dummy_worker_sessions
    def test_scheduler_invocations_enqueue_only_the_third_unchanged_failure(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repo = str(Path("persistent-failure-repo").resolve())
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, phase) VALUES (?, 'main', 'WORKING')",
            (repo,),
        )
        loop_manager.notifications.configure_session(connection, enabled=True)
        connection.commit()

        def failed_step(*_args, **_kwargs):
            from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
            from loop_manager_internal.storage.recovery_state import RecoveryRepository

            RecoveryRepository(connection).observe(RecoveryObservation(
                RecoveryReason.FETCH_FAILED, repo, None, "WORKING", None,
                "base-sha", "fetch-failure", "head-a", "fetch failed",
            ))
            return loop_manager.PROJECT_STEP_TRANSIENT_PAUSE

        with mock.patch("loop_manager._run_project_step", side_effect=failed_step):
            for expected_events in (0, 0, 1):
                outcome = loop_manager.run_project_result(
                    connection, Path("/unused-state"), repo, Path("/unused.ini"),
                    "main", False,
                )
                self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
                events = connection.execute(
                    "SELECT event_type FROM notification_outbox WHERE repo = ?",
                    (repo,),
                ).fetchall()
                self.assertEqual(len(events), expected_events)
