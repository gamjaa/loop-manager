import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import worker_result
from tests.support.publication import publishing_branch_pr_lookup
from tests.support.worker import dummy_worker_sessions


class WorkerPublicationValidationTest(unittest.TestCase):
    plan = {"id": "G-008", "title": "Descriptive Worker Commits"}

    def test_dirty_base_worktree_sync_preserves_file_index_and_head(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            head_before = git_command(repo, "rev-parse", "HEAD")
            index_before = git_command(repo, "diff", "--cached", "--", "src/app.py")

            result = loop_manager.synchronize_base_worktree(repo, "main")

            self.assertEqual(result.status, "equal")
            self.assertEqual(
                (repo / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 99\n",
            )
            self.assertEqual(
                git_command(repo, "diff", "--cached", "--", "src/app.py"),
                index_before,
            )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head_before)

    def test_equal_base_worktree_sync_records_noop(self):
        result = loop_manager.BaseWorktreeSyncResult(
            "equal", "이미 원격 base와 같음", "example-sha"
        )
        with mock.patch("loop_manager.operation_log") as operation_log:
            loop_manager.log_base_worktree_sync(Path("/source"), result)
        operation_log.assert_called_once_with(
            f"기본 worktree 동기화 equal ({Path('/source')}, remote=example-sha): 이미 원격 base와 같음",
            error=False,
        )

    @dummy_worker_sessions
    def test_verify_and_review_retries_keep_latest_worker_metadata_out_of_reviewer_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            git_command(repo, "add", str(plan.relative_to(repo)))
            git_command(repo, "commit", "-m", "Add ExecPlan G-001: Worker Boundary")
            git_command(repo, "push", "origin", "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            base_file = (repo / "src/app.py").read_text(encoding="utf-8")
            base_index = git_command(repo, "diff", "--cached")
            base_head = git_command(repo, "rev-parse", "HEAD")
            connection = sqlite3.connect(":memory:")
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch, worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', 'loop-manager/G-001', ?, ?)""",
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()
            worker_messages = iter(
                (
                    worker_result("Record initial context", "Initial summary", "test: PASS"),
                    worker_result("Refresh verified context", "Verified summary", "test: PASS"),
                    worker_result("Publish reviewed context", "Final summary", "test: PASS"),
                )
            )
            versions = iter((2, 3, 4))
            review_messages = iter(("ISSUE_ID: src/app.py::value::incorrect\nVERDICT: FAIL", "VERDICT: PASS"))
            reviewer_prompts = []
            logs = []

            def agent(worktree, _state, _config, role_name, prompt, **_kwargs):
                if role_name == "worker":
                    version = next(versions)
                    (worktree / "src/app.py").write_text(
                        f"VALUE = {version}\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(0, next(worker_messages), "worker-session")
                reviewer_prompts.append(prompt)
                return 0, next(review_messages)

            gh_commands = []
            original_command = loop_manager.command

            def command_stub(arguments, **kwargs):
                if arguments[-3:] == ["remote", "get-url", "origin"]:
                    return subprocess.CompletedProcess(arguments, 0, stdout="git@github.com:octo/loop-manager.git\n")
                if arguments[:3] == ["gh", "pr", "create"]:
                    gh_commands.append(arguments)
                    return subprocess.CompletedProcess(arguments, 0, stdout="https://example.test/pr/1\n")
                return original_command(arguments, **kwargs)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch", return_value=None,
            ), mock.patch("loop_manager.run_codex", side_effect=agent), mock.patch(
                "loop_manager.verify",
                side_effect=[(False, "verify failed"), (True, "verify: PASS"), (True, "verify: PASS")],
            ), mock.patch("loop_manager.command", side_effect=command_stub), mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                row = connection.execute(
                    "SELECT worker_result, status FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-001'", (str(repo),)
                ).fetchone()

                latest_title = git_command(worktree, "log", "-1", "--pretty=%s")
                latest_body = loop_manager.build_pr_body(
                    loop_manager.parse_worker_result(row["worker_result"], self.plan),
                    {**self.plan, "sources": ["docs/requirements/Active.md"]},
                    "docs/plans/[G-001] Worker Boundary.md",
                    "## Purpose\n\nPreserve immutable context.\n",
                    "git@github.com:octo/loop-manager.git",
                    "a" * 40,
                )

                base_file_after = (repo / "src/app.py").read_text(encoding="utf-8")
                base_index_after = git_command(repo, "diff", "--cached")
                base_head_after = git_command(repo, "rev-parse", "HEAD")
        self.assertEqual(latest_title, "Publish reviewed context")
        self.assertEqual(
            row["worker_result"],
            worker_result("Publish reviewed context", "Final summary", "test: PASS"),
        )
        self.assertNotIn("Publish reviewed context", reviewer_prompts[-1])
        self.assertNotIn("Final summary", reviewer_prompts[-1])
        self.assertNotIn("Initial summary", reviewer_prompts[-1])
        self.assertNotIn("test: PASS", reviewer_prompts[-1])
        self.assertNotIn("verify: PASS", reviewer_prompts[-1])
        self.assertIn("Final summary", latest_body)
        self.assertEqual(row["status"], "PR_OPEN")
        self.assertNotEqual(worktree, repo)
        self.assertEqual(base_file_after, base_file)
        self.assertEqual(base_index_after, base_index)
        self.assertEqual(base_head_after, base_head)
        self.assertEqual(len(gh_commands), 1)
        pr_title = gh_commands[0][gh_commands[0].index("--title") + 1]
        self.assertEqual(pr_title, "Implement G-001: Worker Boundary")
        self.assertNotEqual(pr_title, "Publish reviewed context")
        self.assertIn(("Reviewer 시작: G-001 Worker Boundary", {}), logs)
        self.assertIn(("Worker 반환: G-001 Worker Boundary (Review 실패)", {"error": True}), logs)

    @dummy_worker_sessions
    def test_invalid_worker_result_resumes_same_session_without_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            commit_and_push(repo, "Add worker plan")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch, worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', 'loop-manager/G-001', ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()
            sessions = []
            prompts = []

            def worker(agent_worktree, _state, _config, role_name, prompt, **kwargs):
                if role_name == "reviewer":
                    return loop_manager.CodexResult(
                        0, "VERDICT: PASS", "reviewer-session"
                    )
                sessions.append(kwargs["session_id"])
                prompts.append(prompt)
                if len(sessions) == 1:
                    result = worker_result(
                        "Worker Boundary", "Bad title", "test: PASS"
                    )
                else:
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    )
                    result = worker_result(
                        "Validate worker metadata", "Good title", "test: PASS"
                    )
                return loop_manager.CodexResult(0, result, "worker-session")

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch",
                side_effect=publishing_branch_pr_lookup(
                    connection, "https://example.test/pr/1"
                ),
            ), mock.patch("loop_manager.run_codex", side_effect=worker), mock.patch(
                "loop_manager.verify",
                side_effect=lambda *_args: (
                    self.assertEqual(len(sessions), 2) or True,
                    "verify: PASS",
                ),
            ) as verify:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                row = connection.execute(
                    "SELECT phase, last_error FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(row["phase"], "WORKING")
                self.assertIn("Worker 결과 검증 실패", row["last_error"])
                verify.assert_not_called()

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )

            self.assertEqual(sessions, [None, "worker-session"])
            self.assertIn("Worker 결과 검증 실패", prompts[1])
            verify.assert_called_once()
