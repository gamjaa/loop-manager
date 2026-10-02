from tests.support.workspace import CleanupFixture
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import dummy_worker_sessions


class CleanupPendingStateTest(CleanupFixture, unittest.TestCase):

    def test_existing_database_adds_cleanup_pending_without_losing_project(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT NOT NULL, phase TEXT)"
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'PR_OPEN')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cleanup_pending'"
                ).fetchone()
                row = connection.execute(
                    "SELECT repo, base_branch, phase FROM projects WHERE repo = 'repo'"
                ).fetchone()

            self.assertIsNotNone(table)
            self.assertEqual(tuple(row), ("repo", "main", "PR_OPEN"))

    def test_existing_cleanup_records_default_to_owned_during_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT NOT NULL, phase TEXT)"
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'PR_OPEN')"
            )
            database.execute(
                """
                CREATE TABLE cleanup_pending (
                    repo TEXT NOT NULL,
                    worktree TEXT NOT NULL,
                    pr_url TEXT NOT NULL,
                    pr_head_oid TEXT,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (repo, worktree)
                )
                """
            )
            database.execute(
                """
                INSERT INTO cleanup_pending (
                    repo, worktree, pr_url, pr_head_oid, reason
                ) VALUES ('repo', '/tmp/managed', 'https://example.test/pr/1', NULL, 'dirty')
                """
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                pending = connection.execute(
                    "SELECT reason, workspace_owned FROM cleanup_pending WHERE repo = 'repo'"
                ).fetchone()

            self.assertEqual(tuple(pending), ("dirty", loop_manager.WORKSPACE_OWNED))

    def test_pending_recheck_removes_safe_worktree_and_missing_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Implement cleanup fixture")
            pr_head = git_command(worktree, "rev-parse", "HEAD")
            connection = self._memory_connection(repo)
            missing = root / "already-gone"
            for path, oid in ((worktree, pr_head), (missing, None)):
                loop_manager._store_cleanup_pending(
                    connection,
                    str(repo),
                    path,
                    "https://example.test/pr/1",
                    oid,
                    "preserved",
                )
            connection.commit()

            remaining = loop_manager.recheck_cleanup_pending(
                connection, repo, str(repo), state_dir
            )

            self.assertEqual(remaining, [])
            self.assertFalse(worktree.exists())

    def test_unowned_pending_is_not_inspected_and_clears_only_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            victim = root / "user-worktree"
            victim.mkdir()
            marker = victim / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            connection = self._memory_connection(repo)
            loop_manager._store_cleanup_pending(
                connection,
                str(repo),
                victim,
                "https://example.test/pr/1",
                None,
                "ownership unverified",
                workspace_owned=0,
            )
            connection.commit()

            with mock.patch(
                "loop_manager.validate_pending_cleanup_worktree"
            ) as validate, mock.patch(
                "loop_manager.remove_cleanup_worktree"
            ) as remove:
                remaining = loop_manager.recheck_cleanup_pending(
                    connection, repo, str(repo), state_dir
                )
                marker.unlink()
                victim.rmdir()
                cleared = loop_manager.recheck_cleanup_pending(
                    connection, repo, str(repo), state_dir
                )

            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["workspace_owned"], 0)
            self.assertEqual(cleared, [])
            validate.assert_not_called()
            remove.assert_not_called()

    @dummy_worker_sessions
    def test_merged_dirty_worktree_becomes_nonblocking_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            write_execplan(repo, "G-001", "Cleanup")
            commit_and_push(repo, "Add cleanup plan")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            state_dir = root / "state"
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Implement cleanup fixture")
            pr_head = git_command(worktree, "rev-parse", "HEAD")
            (worktree / "untracked.txt").write_text("keep\n", encoding="utf-8")
            connection = self._memory_connection(repo)
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'PR_OPEN', branch = ?, worktree = ?,
                    plan_path = 'docs/plans/[G-001] Cleanup.md',
                    pr_url = 'https://example.test/pr/1'
                WHERE repo = ?
                """,
                (pr_head, plan_sha, branch, str(worktree), str(repo)),
            )
            connection.commit()
            merged_pr = {
                "url": "https://example.test/pr/1",
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "headRefOid": pr_head,
            }

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged_pr
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, branch, worktree, pr_url, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            pending = connection.execute(
                "SELECT worktree, pr_head_oid, reason FROM cleanup_pending WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 0)
            self.assertEqual(tuple(project), (None, None, None, None, None))
            self.assertEqual(pending["worktree"], str(worktree))
            self.assertEqual(pending["pr_head_oid"], pr_head)
            self.assertIn("untracked 파일", pending["reason"])
            self.assertTrue(worktree.exists())
            planner.assert_called_once()
            self.assertEqual(planner.call_args.args[3], "planner")
