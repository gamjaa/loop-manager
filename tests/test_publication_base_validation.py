from tests.support.publication import PublicationFixture
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, commit_and_push, isolated_git_test
from tests.support.worker import worker_result
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class PublicationBaseValidationTest(PublicationFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_publication_revalidates_after_verify_and_review_base_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _branch,
                _worktree,
            ) = self._publication_state(directory)
            (repo / "docs/notes.md").write_text(
                "# Base two\n", encoding="utf-8"
            )
            commit_and_push(repo, "Advance base before publication")
            reviews = []
            created = []
            review_base_advanced = False

            def reviewer(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                if role_name == "worker":
                    return loop_manager.CodexResult(0,
                        worker_result("Confirm current rebase candidate", "Checks the latest candidate and inputs.",
                                      "current rebase candidate: PASS"), "worker-session")
                self.assertEqual(role_name, "reviewer")
                reviews.append(role_name)
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-session"
                )

            def pr_lookup(*_args, **_kwargs):
                nonlocal review_base_advanced
                phase = connection.execute(
                    "SELECT phase FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["phase"]
                if (
                    reviews == ["reviewer"]
                    and phase == "PUBLISHING"
                    and not review_base_advanced
                ):
                    review_base_advanced = True
                    (repo / "README.md").write_text(
                        "# Base three\n", encoding="utf-8"
                    )
                    commit_and_push(repo, "Advance base during review")
                return None
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", side_effect=pr_lookup
            ), mock.patch(
                "loop_manager.run_codex", side_effect=reviewer
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify, mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command",
                side_effect=self._command_stub(created),
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, active_sha, validated_base_sha, pr_head_oid "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, active_sha, validated_base_sha, pr_head_oid "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            latest_base = git_command(repo, "rev-parse", "origin/main")

        self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["active_sha"], latest_base)
        self.assertEqual(plan["validated_base_sha"], latest_base)
        self.assertIsNotNone(plan["pr_head_oid"])
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(reviews, ["reviewer", "reviewer"])
        self.assertEqual(len(created), 1)

    @dummy_worker_sessions
    def test_publication_rebase_that_removes_change_blocks_before_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _branch,
                _worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            commit_and_push(repo, "Apply implementation upstream")

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
            ) as agent, mock.patch("loop_manager.verify") as verify:
                outcome = loop_manager.run_project(
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
            plan = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()

        self.assertEqual(outcome, 0)
        self.assertIsNone(blocked["phase"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIn("rebase 후 Worker 변경이 남지 않아", plan["last_error"])
        agent.assert_not_called()
        verify.assert_not_called()
