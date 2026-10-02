from tests.support.workspace import WorkspaceFixture
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import dummy_worker_sessions


class WorkspaceCollisionsTest(WorkspaceFixture, unittest.TestCase):

    def test_new_workspace_rejects_preexisting_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _state_dir, _connection, _plan, _plan_sha, branch, worktree = (
                self._ready_execplan(directory)
            )
            worktree.mkdir(parents=True)
            marker = worktree / "unrelated.txt"
            marker.write_text("preserve me\n", encoding="utf-8")

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "worktree가 이미 존재",
            ):
                loop_manager.make_worktree(
                    repo,
                    worktree,
                    branch,
                    "main",
                    allow_create_branch=True,
                    allow_recovery=False,
                    allow_existing=False,
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me\n")

    @dummy_worker_sessions
    def test_branch_collision_blocks_then_resumes_after_conflict_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            git_command(repo, "branch", branch, "origin/main")
            branch_head = git_command(repo, "rev-parse", branch)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
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

            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn("이미 존재해 소유권을 증명할 수 없습니다", project["last_error"])
            self.assertEqual(plan_status, "IMPLEMENTING")
            self.assertEqual(git_command(repo, "rev-parse", branch), branch_head)
            self.assertFalse(worktree.exists())
            run_agent.assert_not_called()

            ownership = connection.execute(
                "SELECT workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["workspace_owned"]
            self.assertEqual(ownership, 0)

            (repo / "docs/PRODUCT.md").write_text(
                "# Resolve blocked input\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change blocked plan input")
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as resumed_agent:
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed_project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed_result, 1)
            self.assertEqual(resumed_project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "이미 존재해 소유권을 증명할 수 없습니다",
                resumed_project["last_error"],
            )
            self.assertEqual(tuple(resumed_plan), ("IMPLEMENTING", 0))
            self.assertEqual(git_command(repo, "rev-parse", branch), branch_head)
            self.assertFalse(worktree.exists())
            resumed_agent.assert_not_called()

            git_command(repo, "branch", "-D", branch)
            planner_result = loop_manager.CodexResult(0, "", "planner-session")
            worker_stop = loop_manager.CodexResult(
                1, "stop after ownership recovery", "worker-session"
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
                "loop_manager.run_codex", side_effect=[planner_result, worker_stop]
            ) as recovered_agent:
                recovered_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            recovered_project = connection.execute(
                "SELECT phase, branch, worktree, last_error "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            recovered_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(recovered_result, 1)
            self.assertEqual(recovered_project["phase"], "WORKING")
            self.assertEqual(recovered_project["branch"], branch)
            self.assertEqual(
                recovered_project["last_error"],
                "stop after ownership recovery",
            )
            self.assertEqual(tuple(recovered_plan), ("IMPLEMENTING", 1))
            self.assertTrue(worktree.exists())
            self.assertEqual(
                loop_manager.git(
                    repo,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(
                [call.args[3] for call in recovered_agent.call_args_list],
                ["planner", "worker"],
            )

    @dummy_worker_sessions
    def test_new_planning_rejects_preexisting_detached_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            project_id = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
            planning_worktree = (
                state_dir
                / "worktrees"
                / project_id
                / "planning"
                / fingerprint
            )
            planning_worktree.parent.mkdir(parents=True)
            git_command(
                repo,
                "worktree",
                "add",
                "--detach",
                str(planning_worktree),
                "origin/main",
            )
            marker = planning_worktree / "keep.txt"
            marker.write_text("preserve me\n", encoding="utf-8")
            original_head = git_command(planning_worktree, "rev-parse", "HEAD")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            connection.commit()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
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
                "SELECT phase, planning_error, planning_error_fingerprint, "
                "planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PLANNING")
            self.assertIn(
                "새 planning worktree 경로가 이미 존재",
                project["planning_error"],
            )
            self.assertEqual(project["planning_workspace_owned"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual(
                git_command(planning_worktree, "rev-parse", "HEAD"),
                original_head,
            )
            run_agent.assert_not_called()

            (repo / "docs/PRODUCT.md").write_text(
                "# Changed planning input\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change planning input")
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex"
            ) as resumed_agent:
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed = connection.execute(
                "SELECT planning_error, planning_error_fingerprint, "
                "planning_workspace_owned, last_error "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed_result, 1)
            self.assertEqual(resumed["planning_error"], project["planning_error"])
            self.assertEqual(
                resumed["planning_error_fingerprint"],
                project["planning_error_fingerprint"],
            )
            self.assertIn(
                "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                resumed["last_error"],
            )
            self.assertEqual(resumed["planning_workspace_owned"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual(
                git_command(planning_worktree, "rev-parse", "HEAD"),
                original_head,
            )
            resumed_agent.assert_not_called()

    def test_started_planning_worktree_is_not_recreated_from_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            missing = root / "state/worktrees/project/planning/fingerprint"

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "복구할 branch가 없습니다",
            ):
                loop_manager.make_planning_worktree(
                    repo,
                    missing,
                    "main",
                    allow_create=False,
                )

            self.assertFalse(missing.exists())

    @dummy_worker_sessions
    def test_changed_planning_input_preserves_unowned_saved_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            victim = root / "user-worktree"
            victim_branch = "user/planning"
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
            victim_head = git_command(victim, "rev-parse", "HEAD")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, phase, worktree,
                    planning_attempt_fingerprint, planning_validation_error
                ) VALUES (?, 'main', ?, 'PLANNING', ?, 'stale-input', 'invalid')
                """,
                (str(repo), plan_sha, str(victim)),
            )
            connection.commit()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
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
                "SELECT phase, planning_error, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PLANNING")
            self.assertIn(
                "planning workspace cleanup 소유권 검증 실패",
                project["planning_error"],
            )
            self.assertEqual(project["last_error"], project["planning_error"])
            self.assertTrue(marker.is_file())
            self.assertEqual(git_command(victim, "rev-parse", "HEAD"), victim_head)
            loop_manager.validate_managed_worktree(repo, victim, victim_branch)
            run_agent.assert_not_called()

    def test_wrong_branch_and_wrong_deterministic_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-012"
            )
            git_command(
                repo,
                "worktree",
                "add",
                "-b",
                "loop-manager/other",
                str(worktree),
                "origin/main",
            )

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "branch 불일치",
            ):
                loop_manager.make_worktree(repo, worktree, branch, "main")
            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "결정적 소유 경계",
            ):
                loop_manager.validate_execplan_workspace_identity(
                    state_dir,
                    str(repo),
                    "G-012",
                    branch,
                    root / "elsewhere",
                )
