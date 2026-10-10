from tests.support.git import isolated_git_test
from tests.support.workspace import WorkspaceFixture
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class WorkspaceCleanupOwnershipTest(WorkspaceFixture, unittest.TestCase):

    @dummy_worker_sessions
    def test_merge_cleanup_preserves_unowned_deterministic_worktree(self):
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
            planning_baseline = connection.execute(
                "SELECT planning_fingerprint, processed_sha FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Unowned implementation")
            pr_head = git_command(worktree, "rev-parse", "HEAD")
            pr_url = "https://example.test/pr/12"
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'PR_OPEN', active_sha = ?, branch = ?, worktree = ?,
                    pr_url = ?
                WHERE repo = ? AND plan_id = 'G-012'
                """,
                (plan_sha, branch, str(worktree), pr_url, str(repo)),
            )
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'PR_OPEN',
                    branch = ?, worktree = ?, plan_path = ?, pr_url = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                    pr_url,
                    str(repo),
                ),
            )
            connection.commit()
            merged_pr = {
                "url": pr_url,
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "headRefOid": pr_head,
            }

            def planner(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                self.assertEqual(role_name, "planner")
                return loop_manager.CodexResult(0, "", "planner-session")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged_pr
            ), mock.patch(
                "loop_manager.run_codex", side_effect=planner
            ) as run_agent:
                merge_result = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                after_merge = connection.execute(
                    "SELECT phase, planning_fingerprint, processed_sha "
                    "FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                run_agent.assert_not_called()
                completion_result = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            pending = connection.execute(
                "SELECT workspace_owned, reason FROM cleanup_pending WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(merge_result.kind, loop_manager.ProjectRunKind.BLOCKED)
            self.assertEqual(merge_result.exit_code, 1)
            self.assertEqual(
                completion_result.kind,
                loop_manager.ProjectRunKind.BLOCKED,
            )
            self.assertEqual(completion_result.exit_code, 1)
            self.assertEqual(
                tuple(after_merge),
                ("PROJECT_BLOCKED", *tuple(planning_baseline)),
            )
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "소유권이 확인되지 않았습니다",
                project["last_error"],
            )
            self.assertEqual(tuple(plan_status), ("MERGED", 0))
            self.assertEqual(pending["workspace_owned"], 0)
            self.assertIn(
                "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                pending["reason"],
            )
            self.assertTrue(worktree.is_dir())
            self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), pr_head)
            run_agent.assert_not_called()

    @dummy_worker_sessions
    def test_merge_cleanup_preserves_unowned_saved_path_as_nonblocking_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                repo,
                state_dir,
                connection,
                plan,
                plan_sha,
                branch,
                _worktree,
            ) = self._ready_execplan(directory)
            victim = root / "user-merge-worktree"
            victim_branch = "user/merge"
            git_command(
                repo,
                "worktree",
                "add",
                "-b",
                victim_branch,
                str(victim),
                "origin/main",
            )
            marker = victim / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            git_command(victim, "add", "keep.txt")
            git_command(victim, "commit", "-m", "Keep user worktree")
            victim_head = git_command(victim, "rev-parse", "HEAD")
            pr_url = "https://example.test/pr/12"
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'PR_OPEN', active_sha = ?, branch = ?, worktree = ?,
                    pr_url = ?
                WHERE repo = ? AND plan_id = 'G-012'
                """,
                (plan_sha, branch, str(victim), pr_url, str(repo)),
            )
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'PR_OPEN',
                    branch = ?, worktree = ?, plan_path = ?, pr_url = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    branch,
                    str(victim),
                    plan.relative_to(repo).as_posix(),
                    pr_url,
                    str(repo),
                ),
            )
            connection.commit()
            merged_pr = {
                "url": pr_url,
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "headRefOid": victim_head,
            }

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged_pr
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

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["status"]
            pending = connection.execute(
                "SELECT workspace_owned, reason FROM cleanup_pending WHERE repo = ?",
                (str(repo),),
            ).fetchone()

            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "소유권이 확인되지 않았습니다",
                project["last_error"],
            )
            self.assertEqual(plan_status, "MERGED")
            self.assertEqual(pending["workspace_owned"], 0)
            self.assertIn(
                "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                pending["reason"],
            )
            self.assertTrue(marker.is_file())
            self.assertEqual(git_command(victim, "rev-parse", "HEAD"), victim_head)
            loop_manager.validate_managed_worktree(repo, victim, victim_branch)
            run_agent.assert_not_called()
