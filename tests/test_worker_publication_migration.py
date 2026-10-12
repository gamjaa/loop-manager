from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, isolated_git_test
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class WorkerPublicationMigrationTest(unittest.TestCase):
    plan = {"id": "G-008", "title": "Descriptive Worker Commits"}

    def test_existing_active_database_backfills_plan_sha_or_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            git_command(repo, "add", str(plan.relative_to(repo)))
            git_command(repo, "commit", "-m", "Add ExecPlan G-001: Worker Boundary")
            plan_sha = git_command(repo, "rev-parse", "HEAD")
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA.replace("    plan_sha TEXT,\n", ""))
            database.execute(
                "INSERT INTO projects (repo, base_branch, active_sha, phase, plan_path) "
                "VALUES (?, 'main', 'rebased', 'WORKING', ?)",
                (str(repo), plan.relative_to(repo).as_posix()),
            )
            database.commit()
            database.close()

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
                loop_manager.backfill_plan_shas(connection, target_repo=str(repo))
                row = connection.execute(
                    "SELECT plan_sha, phase FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                loop_manager.clear_active(connection, str(repo))
                cleared = connection.execute(
                    "SELECT plan_sha FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()

        self.assertEqual(row["plan_sha"], plan_sha)
        self.assertEqual(row["phase"], "WORKING")
        self.assertIsNone(cleared["plan_sha"])

    @dummy_worker_sessions
    def test_unrecoverable_plan_sha_migration_stays_blocked_without_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA.replace("    plan_sha TEXT,\n", ""))
            database.execute(
                "INSERT INTO projects (repo, base_branch, active_sha, phase, plan_path) "
                "VALUES (?, 'main', 'rebased', 'WORKING', ?)",
                (str(repo), plan.relative_to(repo).as_posix()),
            )
            database.commit()
            database.close()

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
                loop_manager.backfill_plan_shas(connection, target_repo=str(repo))
                blocked = connection.execute(
                    "SELECT phase, last_error FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                    "loop_manager.run_codex"
                ) as run:
                    self.assertEqual(
                        loop_manager.run_project(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        ),
                        1,
                    )
                retained = connection.execute(
                    "SELECT phase, last_error FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()

        self.assertEqual(blocked["phase"], "PROJECT_BLOCKED")
        self.assertEqual(blocked["last_error"], loop_manager.PLAN_SHA_BACKFILL_ERROR)
        self.assertEqual(retained["phase"], "PROJECT_BLOCKED")
        self.assertEqual(retained["last_error"], loop_manager.PLAN_SHA_BACKFILL_ERROR)
        run.assert_not_called()

    def test_plan_sha_migration_and_clear_active(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA.replace("    plan_sha TEXT,\n", ""))
            database.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
            database.commit()
            database.close()

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
                loop_manager.update_project(connection, "repo", active_sha="current", plan_sha="planned")
                loop_manager.update_project(connection, "repo", active_sha="rebased")
                preserved = connection.execute(
                    "SELECT active_sha, plan_sha FROM projects WHERE repo = 'repo'"
                ).fetchone()
                self.assertEqual(tuple(preserved), ("rebased", "planned"))
                loop_manager.clear_active(connection, "repo")
                row = connection.execute(
                    "SELECT active_sha, plan_sha FROM projects WHERE repo = 'repo'"
                ).fetchone()

        self.assertIn("plan_sha", columns)
        self.assertEqual(tuple(row), (None, None))
