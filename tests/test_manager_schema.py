import sqlite3
import tempfile
import unittest
from pathlib import Path
import loop_manager


class ManagerSchemaTest(unittest.TestCase):
    def test_schema_and_project_update(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")

        loop_manager.update_project(
            connection,
            "repo",
            phase="WORKING",
            active_sha="abc",
            planning_fingerprint="fingerprint",
            planning_retry_count=2,
            planner_session_id="session-1",
        )

        row = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertEqual(
            (
                row["phase"],
                row["active_sha"],
                row["planning_fingerprint"],
                row["planning_retry_count"],
                row["planner_session_id"],
            ),
            ("WORKING", "abc", "fingerprint", 2, "session-1"),
        )

    def test_existing_database_adds_planning_fingerprint_without_losing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                """
                CREATE TABLE projects (
                    repo TEXT PRIMARY KEY,
                    base_branch TEXT NOT NULL,
                    processed_sha TEXT,
                    active_sha TEXT,
                    phase TEXT,
                    branch TEXT,
                    worktree TEXT,
                    pr_url TEXT,
                    last_error TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'WORKING')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(projects)")
                }
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = 'repo'"
                ).fetchone()

        self.assertIn("plan_path", columns)
        self.assertIn("specification_fingerprint", columns)
        self.assertIn("planning_fingerprint", columns)
        self.assertIn("planning_attempt_fingerprint", columns)
        self.assertIn("planning_retry_count", columns)
        self.assertIn("planner_session_id", columns)
        self.assertIn("planning_validation_error", columns)
        self.assertIn("planning_error", columns)
        self.assertIn("planning_result_pending_validation", columns)
        self.assertIn("planning_result_completed", columns)
        self.assertIn("planning_error_fingerprint", columns)
        self.assertTrue({
            "planning_error_incident_id", "planning_error_origin",
            "planning_correction_error", "planning_correction_incident_id",
            "planning_correction_sha", "planning_correction_context",
        } <= columns)
        self.assertEqual((row["base_branch"], row["phase"]), ("main", "WORKING"))
        self.assertIsNone(row["specification_fingerprint"])
        self.assertIsNone(row["planning_fingerprint"])
        self.assertEqual(row["planning_retry_count"], 0)
        self.assertEqual(row["planning_result_pending_validation"], 0)
        self.assertIsNone(row["planning_error"])
        self.assertIsNone(row["planning_correction_incident_id"])

    def test_existing_database_adds_worker_session_and_result_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                """
                CREATE TABLE projects (
                    repo TEXT PRIMARY KEY,
                    base_branch TEXT NOT NULL,
                    phase TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'WORKING')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
                row = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()

        self.assertTrue({"worker_session_id", "worker_result", "verify_result"} <= columns)
        self.assertIsNone(row["worker_session_id"])
        self.assertIsNone(row["worker_result"])
        self.assertIsNone(row["verify_result"])
