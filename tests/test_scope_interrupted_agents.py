from contextlib import closing
from tests.support.scope import AgentScopeFixture
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import dummy_worker_sessions


class ScopeInterruptedAgentsTest(AgentScopeFixture, unittest.TestCase):
    def test_self_hosted_runtime_preserves_interrupted_worker_and_reviewer_identity(self):
        for role in ("worker", "reviewer"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                state_dir = Path(directory) / "state"
                self._persist_interrupted_worker(state_dir, repo, plan_path, before)
                connection = sqlite3.connect(state_dir / "loop-manager.db")
                connection.row_factory = sqlite3.Row
                connection.execute(
                    "UPDATE projects SET agent_role = ?, worker_session_id = 'worker-existing', "
                    "reviewer_session_id = 'reviewer-existing' WHERE repo = ?",
                    (role, str(repo)),
                )
                connection.commit()

                writer = Path(directory) / "writer"
                subprocess.run(
                    ["git", "clone", str(Path(directory) / "remote.git"), str(writer)],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                git_command(writer, "config", "user.email", "loop-manager@example.com")
                git_command(writer, "config", "user.name", "Loop Manager Test")
                (writer / "README.md").write_text("# Changed\n", encoding="utf-8")
                commit_and_push(writer, "Advance runtime")
                selected_sha = git_command(writer, "rev-parse", "HEAD")

                with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                    with loop_manager.process_lock(state_dir) as handle:
                        mutation_id = "d" * 32
                        runtime = loop_manager.prepare_runtime_worktree(
                            repo, state_dir, mutation_id=mutation_id,
                        )
                        loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                        with mock.patch.object(loop_manager, "ROOT", runtime.path):
                            identity = loop_manager.validate_runtime_mutation(
                                repo, state_dir, selected_sha, mutation_id,
                            )
                        loop_manager.validate_persisted_agent_scope(
                            connection, repo, state_dir, str(repo),
                            runtime_mutation=identity,
                        )
                project = connection.execute(
                    "SELECT worker_session_id, reviewer_session_id, agent_snapshot "
                    "FROM projects WHERE repo = ?", (str(repo),),
                ).fetchone()
                plan = connection.execute(
                    "SELECT status, retry_count FROM exec_plans WHERE repo = ? "
                    "AND plan_id = 'G-001'", (str(repo),),
                ).fetchone()
                self.assertEqual(project["worker_session_id"], "worker-existing")
                self.assertEqual(project["reviewer_session_id"], "reviewer-existing")
                self.assertEqual(plan["status"], "IMPLEMENTING")
                self.assertEqual(plan["retry_count"], 0)
                self.assertIn(str(runtime.path), loop_manager.GitSnapshot.deserialize(
                    project["agent_snapshot"]
                ).worktrees)
                connection.close()


    @dummy_worker_sessions
    def test_interrupted_planner_scope_violation_resumes_correction_from_result_snapshot(self):
        planner_sessions = []

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            if role_name != "planner":
                return loop_manager.CodexResult(
                    1,
                    "stop after corrected planning",
                    "worker-session",
                )
            planner_sessions.append(kwargs["session_id"])
            unexpected = worktree / "unexpected.txt"
            if len(planner_sessions) == 1:
                kwargs["on_session_started"]("planner-session")
                unexpected.write_text("invalid\n", encoding="utf-8")
                raise RuntimeError("process interrupted after invalid Planner edit")
            self.assertEqual(kwargs["session_id"], "planner-session")
            unexpected.unlink()
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Recovered")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

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
                side_effect=agent,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "process interrupted",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )
                blocked = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertIn(
                    "unexpected.txt",
                    blocked["planning_validation_error"],
                )
                self.assertEqual(planner_sessions, [None])
                start_snapshot = loop_manager.GitSnapshot.deserialize(
                    blocked["agent_snapshot"]
                )
                result_snapshot = loop_manager.GitSnapshot.deserialize(
                    blocked["agent_result_snapshot"]
                )
                self.assertNotEqual(start_snapshot, result_snapshot)
                self.assertIn("unexpected.txt", result_snapshot.dirty)

                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT phase, planning_retry_count, planning_error "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(planner_sessions, [None, "planner-session"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_retry_count"], 0)
            self.assertIsNone(resumed["planning_error"])

    def test_interrupted_config_change_stops_before_database_git_migrations(self):
        scenarios = (
            (
                "remote-url",
                ("remote", "set-url", "origin"),
                "https://secret@attacker.invalid/repo.git",
                "local:remote.origin.url",
            ),
            (
                "hooks-path",
                ("config", "core.hooksPath"),
                "agent-hooks",
                "local:core.hookspath",
            ),
        )
        for name, arguments, configured_value, expected_key in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                state_dir = root / "state"
                self._persist_interrupted_worker(state_dir, repo, plan_path, before)

                sensitive_value = (
                    str(root / configured_value)
                    if name == "hooks-path"
                    else configured_value
                )
                git_command(repo, *arguments, sensitive_value)

                with mock.patch.object(
                    loop_manager.sys,
                    "argv",
                    [
                        "loop_manager.py",
                        "--runtime-child",
                        "--runtime-source",
                        str(loop_manager.ROOT),
                        "--runtime-sha",
                        "a" * 40,
                        "--state-dir",
                        str(state_dir),
                        "--config",
                        str(loop_manager.ROOT / "config/config.ini.example"),
                        "run",
                    ],
                ), mock.patch(
                    "loop_manager.validate_runtime_child"
                ), mock.patch(
                    "loop_manager.validate_runtime_mutation", return_value=None
                ), mock.patch(
                    "loop_manager.git",
                    side_effect=AssertionError(
                        "unsafe Git command before config validation"
                    ),
                ) as unsafe_git, mock.patch(
                    "loop_manager.run_codex"
                ) as agent:
                    result = loop_manager.main()
                    database = sqlite3.connect(state_dir / "loop-manager.db")
                    database.row_factory = sqlite3.Row
                    project = database.execute(
                        "SELECT phase, last_error FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    plan = database.execute(
                        "SELECT status, blocked_fingerprint FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-001'",
                        (str(repo),),
                    ).fetchone()
                    database.close()

                self.assertEqual(result, 1)
                self.assertEqual(project["phase"], "PROJECT_BLOCKED")
                self.assertEqual(plan["status"], "IMPLEMENTING")
                self.assertIsNotNone(plan["blocked_fingerprint"])
                self.assertIn("범위 선검증 실패", project["last_error"])
                self.assertIn(expected_key, project["last_error"])
                self.assertNotIn(sensitive_value, project["last_error"])
                unsafe_git.assert_not_called()
                agent.assert_not_called()

    def test_interrupted_non_base_ref_change_blocks_before_migration_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            state_dir = root / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, before)
            changed_ref = git_command(repo, "rev-parse", "HEAD^")
            git_command(
                repo,
                "update-ref",
                "refs/heads/agent-created",
                changed_ref,
            )
            real_git = loop_manager.git
            git_calls = []

            def reject_fetch(repo_path, *arguments, **kwargs):
                git_calls.append(arguments)
                if arguments[:2] == ("fetch", "origin"):
                    raise AssertionError("migration fetch ran before scope validation")
                return real_git(repo_path, *arguments, **kwargs)

            with mock.patch.object(
                loop_manager.sys,
                "argv",
                [
                    "loop_manager.py",
                    "--runtime-child",
                    "--runtime-source",
                    str(loop_manager.ROOT),
                    "--runtime-sha",
                    "a" * 40,
                    "--state-dir",
                    str(state_dir),
                    "--config",
                    str(loop_manager.ROOT / "config/config.ini.example"),
                    "run",
                ],
            ), mock.patch(
                "loop_manager.validate_runtime_child"
            ), mock.patch(
                "loop_manager.validate_runtime_mutation", return_value=None
            ), mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.git",
                side_effect=reject_fetch,
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                result = loop_manager.main()

            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.row_factory = sqlite3.Row
            project = database.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = database.execute(
                "SELECT status, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            database.close()

            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertEqual(plan["status"], "IMPLEMENTING")
            self.assertIsNotNone(plan["blocked_fingerprint"])
            self.assertIn("공유 Git refs 변경", project["last_error"])
            self.assertIn("refs/heads/agent-created", project["last_error"])
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/heads/agent-created"),
                changed_ref,
            )
            self.assertFalse(
                any(arguments[:2] == ("fetch", "origin") for arguments in git_calls)
            )
            agent.assert_not_called()

    @dummy_worker_sessions
    def test_interrupted_worker_resume_accepts_manager_fetched_remote_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            cleanup_branch, cleanup_worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-099"
            )
            loop_manager.make_worktree(repo, cleanup_worktree, cleanup_branch, "main")

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            loop_manager._ensure_cleanup_pending_schema(connection)
            loop_manager._store_cleanup_pending(
                connection,
                str(repo),
                cleanup_worktree,
                "https://example.test/pr/99",
                plan_sha,
                "pending cleanup",
            )
            connection.commit()
            before = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = 'worker-session'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (plan_sha, branch, str(worktree), str(repo)),
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
                        plan_path,
                        before.serialize(),
                        str(repo),
                    ),
                )

            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )

            remote = root / "remote.git"
            git_command(
                remote,
                "update-ref",
                "refs/heads/manager-update",
                plan_sha,
            )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.refresh_persisted_agent_scope_after_manager_git",
                side_effect=KeyboardInterrupt("interrupt after Manager checkpoint"),
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "Manager checkpoint",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            interrupted_checkpoint = connection.execute(
                "SELECT agent_manager_snapshot FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()["agent_manager_snapshot"]
            self.assertIsNotNone(interrupted_checkpoint)
            self.assertFalse(cleanup_worktree.exists())
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/remotes/origin/manager-update"),
                plan_sha,
            )
            interrupted_agent.assert_not_called()

            sessions = []

            def resumed_worker(_worktree, _state, _config, role_name, _prompt, **kwargs):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                return loop_manager.CodexResult(
                    1, "stop after resumed worker", "worker-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
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
                "SELECT phase, last_error, agent_snapshot, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()["status"]

            self.assertEqual(result, 1)
            self.assertEqual(sessions, ["worker-session"])
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(plan_status, "IMPLEMENTING")
            self.assertEqual(project["last_error"], "stop after resumed worker")
            self.assertIsNone(project["agent_snapshot"])
            self.assertIsNone(project["agent_manager_snapshot"])
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/remotes/origin/manager-update"),
                plan_sha,
            )
            self.assertFalse(cleanup_worktree.exists())
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM cleanup_pending WHERE repo = ?",
                    (str(repo),),
                ).fetchone()[0],
                0,
            )

    def test_existing_database_adds_persistent_agent_scope_fields(self):
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
                "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
            )
            database.commit()
            database.close()

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(projects)")
                }
                row = connection.execute(
                    "SELECT agent_role, agent_snapshot, agent_input_fingerprint, agent_result_snapshot, "
                    "planning_workspace_owned "
                    "FROM projects "
                    "WHERE repo = 'repo'"
                ).fetchone()

            self.assertTrue(
                {
                    "agent_role",
                    "agent_snapshot",
                    "agent_input_fingerprint",
                    "agent_result_snapshot",
                    "planning_workspace_owned",
                }
                <= columns
            )
            self.assertEqual(tuple(row), (None, None, None, None, 0))
