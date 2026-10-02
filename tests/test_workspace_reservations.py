from tests.support.workspace import WorkspaceFixture
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import dummy_worker_sessions


class WorkspaceReservationsTest(WorkspaceFixture, unittest.TestCase):
    def test_planning_git_port_only_translates_workspace_ownership_errors(self):
        connection = sqlite3.connect(":memory:")
        port = loop_manager._PlanningGitPorts(
            connection,
            Path("/repo"),
            Path("/state"),
        )
        arguments = (
            Path("/repo"),
            Path("/planning"),
            "main",
        )
        keywords = {
            "allow_create": True,
            "allow_existing": False,
            "ownership_reserved": False,
            "expected_head": "a" * 40,
            "before_create": lambda: None,
        }

        with mock.patch(
            "loop_manager.make_planning_worktree",
            side_effect=loop_manager.GitLockError("index.lock exists"),
        ), self.assertRaises(loop_manager.GitLockError):
            port.make_planning_worktree(*arguments, **keywords)

        with mock.patch(
            "loop_manager.make_planning_worktree",
            side_effect=loop_manager.WorkspaceOwnershipError("wrong owner"),
        ), self.assertRaisesRegex(
            loop_manager.PlanningWorkspaceOwnershipError,
            "wrong owner",
        ):
            port.make_planning_worktree(*arguments, **keywords)


    @dummy_worker_sessions
    def test_execplan_reservation_rejects_branch_inserted_before_create(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            real_git = loop_manager.git

            def interrupt_before_worktree_add(target, *arguments, **kwargs):
                if arguments[:2] == ("worktree", "add") and "-b" in arguments:
                    raise KeyboardInterrupt(
                        "interrupt before ExecPlan worktree creation"
                    )
                return real_git(target, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.git", side_effect=interrupt_before_worktree_add
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "before ExecPlan worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            reserved = connection.execute(
                "SELECT workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                reserved["workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertFalse(worktree.exists())
            self.assertEqual(
                loop_manager.git(
                    repo,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode,
                1,
            )
            interrupted_agent.assert_not_called()

            git_command(
                repo,
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                "origin/main",
            )
            (worktree / "external.txt").write_text(
                "not owned by Loop Manager\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "external.txt")
            git_command(worktree, "commit", "-m", "Create external branch state")
            external_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(repo, "worktree", "remove", str(worktree))
            self.assertFalse(worktree.exists())
            self.assertNotEqual(external_head, plan_sha)

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
            ) as resumed_agent:
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
            blocked = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "예약 ExecPlan branch가 저장된 기준 commit과 다릅니다",
                project["last_error"],
            )
            self.assertEqual(
                tuple(blocked),
                ("IMPLEMENTING", loop_manager.WORKSPACE_RESERVED),
            )
            self.assertFalse(worktree.exists())
            self.assertEqual(
                git_command(repo, "rev-parse", f"refs/heads/{branch}"),
                external_head,
            )
            self.assertEqual(
                git_command(repo, "show", f"{branch}:external.txt"),
                "not owned by Loop Manager",
            )
            resumed_agent.assert_not_called()

    @dummy_worker_sessions
    def test_planning_reservation_preserves_clean_external_path_on_input_change(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            real_git = loop_manager.git

            def interrupt_before_worktree_add(target, *arguments, **kwargs):
                if arguments[:3] == ("worktree", "add", "--detach"):
                    raise KeyboardInterrupt(
                        "interrupt before planning worktree creation"
                    )
                return real_git(target, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.git", side_effect=interrupt_before_worktree_add
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "before planning worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            reserved = connection.execute(
                "SELECT active_sha, phase, worktree, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            planning_worktree = Path(reserved["worktree"])
            self.assertEqual(reserved["phase"], "PLANNING")
            self.assertEqual(
                reserved["planning_workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertFalse(planning_worktree.exists())
            interrupted_agent.assert_not_called()

            git_command(
                repo,
                "worktree",
                "add",
                "--detach",
                str(planning_worktree),
                reserved["active_sha"],
            )
            self.assertEqual(
                git_command(planning_worktree, "status", "--porcelain"),
                "",
            )
            (repo / "docs/PRODUCT.md").write_text(
                "# changed after planning reservation\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change input after planning reservation")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex"
            ) as resumed_agent:
                first_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                second_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            preserved = connection.execute(
                "SELECT phase, planning_error, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual((first_result, second_result), (1, 1))
            self.assertEqual(preserved["phase"], "PLANNING")
            self.assertIn(
                "planning workspace cleanup 소유권 검증 실패",
                preserved["planning_error"],
            )
            self.assertIn(
                "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                preserved["planning_error"],
            )
            self.assertEqual(
                preserved["planning_workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertTrue(planning_worktree.is_dir())
            self.assertEqual(
                git_command(planning_worktree, "rev-parse", "HEAD"),
                reserved["active_sha"],
            )
            self.assertEqual(
                git_command(planning_worktree, "status", "--porcelain"),
                "",
            )
            resumed_agent.assert_not_called()

    @dummy_worker_sessions
    def test_planning_workspace_reservation_survives_post_create_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            real_make_planning_worktree = loop_manager.make_planning_worktree

            def interrupt_after_create(*args, **kwargs):
                real_make_planning_worktree(*args, **kwargs)
                raise KeyboardInterrupt("interrupt after planning worktree creation")

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.make_planning_worktree",
                side_effect=interrupt_after_create,
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "planning worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            interrupted = connection.execute(
                "SELECT phase, worktree, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(interrupted["phase"], "PLANNING")
            self.assertEqual(
                interrupted["planning_workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertTrue(Path(interrupted["worktree"]).is_dir())
            interrupted_agent.assert_not_called()

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(
                    1,
                    "stop after planning reservation recovery",
                    "planner-session",
                ),
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed = connection.execute(
                "SELECT phase, planning_error, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(resumed["phase"], "PLANNING")
            self.assertIsNone(resumed["planning_error"])
            self.assertEqual(
                resumed["planning_workspace_owned"],
                loop_manager.WORKSPACE_OWNED,
            )
            resumed_agent.assert_called_once()
            self.assertEqual(resumed_agent.call_args.args[3], "planner")

    @dummy_worker_sessions
    def test_execplan_workspace_reservation_survives_post_create_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _plan_sha,
                _branch,
                worktree,
            ) = self._ready_execplan(directory)
            real_make_worktree = loop_manager.make_worktree

            def interrupt_after_create(*args, **kwargs):
                real_make_worktree(*args, **kwargs)
                raise KeyboardInterrupt("interrupt after ExecPlan worktree creation")

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.make_worktree",
                side_effect=interrupt_after_create,
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "ExecPlan worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            interrupted = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            interrupted_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(interrupted["phase"], "WORKING")
            self.assertEqual(
                tuple(interrupted_plan),
                ("IMPLEMENTING", loop_manager.WORKSPACE_RESERVED),
            )
            self.assertTrue(worktree.is_dir())
            interrupted_agent.assert_not_called()

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                return_value=None,
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(
                    1,
                    "stop after ExecPlan reservation recovery",
                    "worker-session",
                ),
            ) as resumed_agent:
                result = loop_manager.run_project(
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
            resumed_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(
                resumed["last_error"],
                "stop after ExecPlan reservation recovery",
            )
            self.assertEqual(
                tuple(resumed_plan),
                ("IMPLEMENTING", loop_manager.WORKSPACE_OWNED),
            )
            resumed_agent.assert_called_once()
            self.assertEqual(resumed_agent.call_args.args[3], "worker")

    def test_existing_catalog_does_not_infer_workspace_ownership(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE exec_plans (
                repo TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                status TEXT NOT NULL,
                branch TEXT,
                worktree TEXT,
                blocked_fingerprint TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO exec_plans "
            "(repo, plan_id, status, branch, worktree, blocked_fingerprint) "
            "VALUES ('repo', 'G-012', 'BLOCKED', 'loop-manager/G-012', "
            "'managed', 'fingerprint')"
        )

        loop_manager._ensure_execplans_schema(connection)

        row = connection.execute(
            "SELECT workspace_owned, blocked_worktree_fingerprint "
            "FROM exec_plans WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(tuple(row), (0, None))
