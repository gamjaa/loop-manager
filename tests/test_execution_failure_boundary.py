import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.database import AutomationCatalogFixture
from tests.support.worker import dummy_worker_sessions


class PersistentExecutionFailureBoundaryTest(AutomationCatalogFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_successful_run_resolves_its_observed_retry(self):
        repo = str(Path("resolved-failure-repo").resolve())
        connection = self._connection(repo)
        with connection:
            connection.execute(
                "UPDATE projects SET phase = 'WORKING' WHERE repo = ?", (repo,)
            )
        loop_manager.notifications.configure_session(connection, enabled=True)
        loop_manager.notifications.ensure_schema(connection)
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
            outcomes = [
                loop_manager.run_project_result(
                    connection, Path("/unused-state"), repo, Path("/unused.ini"),
                    "main", False,
                )
                for _ in range(3)
            ]
        self.assertEqual(
            [outcome.kind for outcome in outcomes],
            [loop_manager.ProjectRunKind.WAITING] * 3,
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
    def test_scheduler_invocations_keep_persistent_failure_as_diagnostic_only(self):
        repo = str(Path("persistent-failure-repo").resolve())
        connection = self._connection(repo)
        with connection:
            connection.execute(
                "UPDATE projects SET phase = 'WORKING' WHERE repo = ?", (repo,)
            )
        loop_manager.notifications.configure_session(connection, enabled=True)
        loop_manager.notifications.ensure_schema(connection)
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
            for _ in range(3):
                outcome = loop_manager.run_project_result(
                    connection, Path("/unused-state"), repo, Path("/unused.ini"),
                    "main", False,
                )
                self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
                events = connection.execute(
                    "SELECT event_type FROM notification_outbox WHERE repo = ?",
                    (repo,),
                ).fetchall()
                self.assertEqual(events, [])
        diagnostic = connection.execute(
            "SELECT active, consecutive_runs, notification_eligible, notification_queued "
            "FROM persistent_execution_failures WHERE repo = ? AND subject = '__project__'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(diagnostic), (1, 3, 1, 0))
