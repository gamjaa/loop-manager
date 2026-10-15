from tests.support.git import isolated_git_test
from tests.support.workspace import WorkspaceFixture
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push
from tests.support.worker import worker_result
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class WorkspaceMissingRecoveryTest(WorkspaceFixture, unittest.TestCase):
    def test_missing_worktree_is_recovered_from_owned_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _state_dir, branch, worktree = self._workspace(directory)
            original_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(repo, "worktree", "remove", str(worktree))

            outcome = loop_manager.make_worktree(
                repo,
                worktree,
                branch,
                "main",
                allow_create_branch=False,
            )

            self.assertEqual(outcome, "recovered")
            self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), original_head)
            loop_manager.validate_managed_worktree(repo, worktree, branch)

    @dummy_worker_sessions
    def test_interrupted_worker_recovers_missing_owned_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                plan,
                plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            loop_manager.make_worktree(repo, worktree, branch, "main")
            before = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = 'worker-session',
                        agent_role = 'worker', agent_snapshot = ?
                    WHERE repo = ? AND plan_id = 'G-012'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        before.serialize(),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'WORKING',
                        branch = ?, worktree = ?, plan_path = ?,
                        worker_session_id = 'worker-session',
                        agent_role = 'worker', agent_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan.relative_to(repo).as_posix(),
                        before.serialize(),
                        str(repo),
                    ),
                )

            git_command(repo, "worktree", "remove", str(worktree))
            sessions = []

            def resumed_worker(
                _worktree, _state, _config, role_name, _prompt, **kwargs
            ):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                return loop_manager.CodexResult(
                    1, "stop after recovery", "worker-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resumed_worker
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["status"]

            self.assertEqual(result, 1)
            self.assertEqual(sessions, ["worker-session"])
            self.assertTrue(worktree.is_dir())
            loop_manager.validate_managed_worktree(repo, worktree, branch)
            self.assertEqual(project["phase"], "WORKING", dict(project))
            self.assertEqual(project["last_error"], "stop after recovery")
            self.assertIsNone(project["agent_snapshot"])
            self.assertEqual(plan_status, "IMPLEMENTING")

    @dummy_worker_sessions
    def test_rebase_conflict_resumes_same_worker_session(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                plan,
                plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            loop_manager.make_worktree(repo, worktree, branch, "main")
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'VERIFYING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = 'worker-session',
                        worker_result = ?
                    WHERE repo = ? AND plan_id = 'G-012'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        worker_result(),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'VERIFYING',
                        branch = ?, worktree = ?, plan_path = ?,
                        worker_session_id = 'worker-session',
                        worker_result = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan.relative_to(repo).as_posix(),
                        worker_result(),
                        str(repo),
                    ),
                )

            (worktree / "src/app.py").write_text(
                "VALUE = 'worker'\n",
                encoding="utf-8",
            )
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Advance base with conflicting source")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify before rebase")
            ), mock.patch(
                "loop_manager.run_codex"
            ) as unexpected_agent:
                first_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            conflicted = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(first_result, 1)
            self.assertEqual(conflicted["phase"], "WORKING")
            self.assertTrue(
                conflicted["last_error"].startswith(
                    loop_manager.REBASE_CONFLICT_PREFIX
                )
            )
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(git_command(worktree, "branch", "--show-current"), "")
            unexpected_agent.assert_not_called()

            sessions = []

            def resolve_conflict(
                agent_worktree, _state, _config, role_name, _prompt, **kwargs
            ):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                if len(sessions) <= 2:
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 'resolved'\n",
                        encoding="utf-8",
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(),
                        "worker-session",
                    )
                return loop_manager.CodexResult(
                    1,
                    "stop after same-run verify recovery",
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify",
                return_value=(False, "stop after rebase resolution"),
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resolve_conflict
            ):
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            checkpoint = connection.execute(
                "SELECT operation, snapshot FROM execution_checkpoints "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            recovery = connection.execute(
                "SELECT subject, disposition, detail FROM recovery_records "
                "WHERE repo = ? ORDER BY updated_at DESC LIMIT 1",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                resumed_result, 1,
                (sessions, resumed["phase"], resumed["last_error"],
                 dict(checkpoint) if checkpoint else None,
                 dict(recovery) if recovery else None),
            )
            self.assertEqual(
                sessions, ["worker-session", "worker-session", "worker-session"]
            )
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(
                resumed["last_error"],
                "stop after same-run verify recovery",
            )
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(
                git_command(worktree, "branch", "--show-current"),
                branch,
            )
            self.assertEqual(
                (worktree / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 'resolved'\n",
            )

    @dummy_worker_sessions
    def test_missing_worktree_and_branch_blocks_without_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, branch, worktree = self._workspace(directory)
            git_command(repo, "worktree", "remove", str(worktree))
            git_command(repo, "branch", "-D", branch)

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "모두 복구할 수 없습니다",
            ):
                loop_manager.make_worktree(
                    repo,
                    worktree,
                    branch,
                    "main",
                    allow_create_branch=False,
                )

            self.assertFalse(worktree.exists())
            self.assertNotEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/heads/{branch}",
                    ]
                ).returncode,
                0,
            )

            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-012", "Recovery Safety")
            commit_and_push(repo, "Add recovery safety plan")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'notification_outbox'"
                ).fetchone()
            )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as run_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()

            plan_status = connection.execute(
                "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["status"]

            self.assertEqual(result, 1)
            self.assertEqual(blocked["phase"], "PROJECT_BLOCKED")
            self.assertIn("모두 복구할 수 없습니다", blocked["last_error"])
            self.assertEqual(plan_status, "IMPLEMENTING")
            run_agent.assert_not_called()
