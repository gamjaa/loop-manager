from contextlib import closing, ExitStack
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.profiles import profile_config
from tests.support.catalog import write_execplan
from tests.support.git import make_remote_repository, isolated_git_test
from tests.support.worker import dummy_worker_sessions
from loop_manager_internal.agents import provider_guidance
from tests.support.workspace import native_developer_texts


@isolated_git_test
class PlannerInterruptionRecoveryTest(unittest.TestCase):
    @dummy_worker_sessions
    def test_planner_commit_failure_preserves_valid_result_without_retry_penalty(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        original_git = loop_manager.git

        def reject_planning_commit(worktree, *arguments, **kwargs):
            if arguments[:1] == ("commit",) and arguments[-1].startswith("Add ExecPlan"):
                raise RuntimeError("commit hook rejected")
            return original_git(worktree, *arguments, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                with mock.patch("loop_manager.git", side_effect=reject_planning_commit):
                    with self.assertRaisesRegex(RuntimeError, "commit hook rejected"):
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                failed_commit = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(failed_commit["planning_retry_count"], 0)
                self.assertEqual(failed_commit["planning_result_pending_validation"], 1)
                self.assertIsNone(failed_commit["planning_validation_error"])
                self.assertIsNone(failed_commit["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_validation_error"])
            self.assertIsNone(resumed["planning_error"])

    @dummy_worker_sessions
    def test_interrupted_successful_planner_result_is_validated_before_resume(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        original_changed_files = loop_manager.changed_files
        changed_files_calls = 0

        def interrupt_after_result_is_persisted(*args, **kwargs):
            nonlocal changed_files_calls
            changed_files_calls += 1
            if changed_files_calls == 2:
                raise RuntimeError("process interrupted after Planner result")
            return original_changed_files(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            connection = loop_manager.connect_database(state_dir, create=True)
            cleanup.callback(connection.close)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                with mock.patch(
                    "loop_manager.changed_files",
                    side_effect=interrupt_after_result_is_persisted,
                ):
                    with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planner_session_id"], "planner-session")
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertIsNone(interrupted["planning_validation_error"])
                saved_result = interrupted["planning_result"]
                connection.close()
                connection = loop_manager.connect_database(state_dir)
                cleanup.callback(connection.close)
                self.assertEqual(
                    connection.execute(
                        "SELECT planning_result FROM projects WHERE repo = ?", (str(repo),)
                    ).fetchone()["planning_result"],
                    saved_result,
                )

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_validation_error"])
            self.assertIsNone(resumed["planning_error"])

    def test_interrupted_planner_persists_started_session_and_resumes(self):
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        thread_event = json.dumps(
            {"type": "thread.started", "thread_id": session_id}
        ) + "\n"

        class InterruptedOutput:
            def __iter__(self):
                yield thread_event
                raise RuntimeError("Planner process interrupted")

            def close(self):
                pass

        class InterruptedProcess:
            def __init__(self):
                self.stdin = mock.Mock()
                self.stdout = InterruptedOutput()
                self.killed = False

            def wait(self):
                return -9

            def kill(self):
                self.killed = True

        process = InterruptedProcess()
        real_popen = subprocess.Popen

        def start_process(arguments, *args, **kwargs):
            if arguments[0] == "codex":
                return process
            return real_popen(arguments, *args, **kwargs)

        resumed_sessions = []

        def verified_preflight(_provider, _role, worktree, _state_dir,
                               read_only, observe, *_args, **_kwargs):
            observe(loop_manager.agent_write_isolation.IsolationEvidence(
                'codex', 'fixture-version', 'Linux:fixture', 'planner',
                str(worktree), 'fixture-repository', 'fixture-policy', 'verified',
            ))
            return loop_manager.agent_write_isolation.codex_arguments(read_only)

        def stopped_resume(_worktree, _state, _config, role_name, _prompt, **kwargs):
            self.assertEqual(role_name, "planner")
            resumed_sessions.append(kwargs["session_id"])
            return loop_manager.CodexResult(
                1, "stop after persisted session check", kwargs["session_id"]
            )

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            config = profile_config(self)
            connection = loop_manager.connect_database(state_dir, create=True)

            with mock.patch.object(provider_guidance, '_probe', return_value='codex-cli 0.159.3'), mock.patch.object(
                provider_guidance, '_developer_texts', side_effect=native_developer_texts), mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"test-strong": frozenset({"xhigh"})},
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=start_process
            ), mock.patch(
                "loop_manager.agent_write_isolation.prepare_isolation",
                side_effect=verified_preflight,
            ), mock.patch(
                "loop_manager.agent_write_isolation.assert_current",
            ), mock.patch(
                "loop_manager.validate_provider_runtime",
            ), mock.patch(
                "loop_manager_internal.agents.agent_codex_sandbox.CodexLaunch.stop",
                side_effect=lambda child: child.kill(),
            ):
                with self.assertRaisesRegex(RuntimeError, "Planner process interrupted"):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        config,
                        "main",
                        False,
                    )
            connection.close()

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["phase"], "PLANNING")
                self.assertEqual(interrupted["planner_session_id"], session_id)
                self.assertTrue(Path(interrupted["worktree"]).is_dir())
                self.assertTrue(process.killed)

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.remote_base", return_value="main"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.run_codex", side_effect=stopped_resume
                ):
                    self.assertEqual(
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            config,
                            "main",
                            False,
                        ),
                        1,
                    )

        self.assertEqual(resumed_sessions, [session_id])
