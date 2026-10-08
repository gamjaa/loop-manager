import json
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.profiles import profile_config


class PlannerSessionsTest(unittest.TestCase):
    def test_planner_codex_persists_thread_id_and_resumes_exact_session(self):
        config = profile_config(self)
        calls = []
        call_options = []

        thread_event = (
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53",
                }
            )
            + "\n"
        )

        class StartedOutput:
            def __iter__(self):
                return iter((thread_event,))

            def close(self):
                pass

        class StartedProcess:
            def __init__(self, arguments, **kwargs):
                calls.append(arguments)
                call_options.append(kwargs)
                self.stdin = mock.Mock()
                self.stdout = StartedOutput()

            def wait(self):
                return 0

            def kill(self):
                raise AssertionError("completed Planner process must not be killed")

        def codex_run(arguments, **kwargs):
            calls.append(arguments)
            call_options.append(kwargs)
            return subprocess.CompletedProcess(arguments, 0)

        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            with mock.patch.dict(os.environ, {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": "/provider-cache",
            }), mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"test-strong": frozenset({"xhigh"})},
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=StartedProcess
            ), mock.patch("loop_manager.subprocess.run", side_effect=codex_run):
                started = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "planner",
                    "plan",
                    read_only=False,
                    persist_session=True,
                )
                resumed = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "planner",
                    "repair",
                    read_only=False,
                    persist_session=True,
                    session_id=started.session_id,
                )

        self.assertEqual(started.code, 0)
        self.assertEqual(
            started.session_id,
            "0199a213-81c0-7800-8aa1-bbab2a035a53",
        )
        self.assertEqual(resumed.session_id, started.session_id)
        self.assertEqual(calls[0][:3], ["codex", "exec", "--json"])
        self.assertNotIn("--ephemeral", calls[0])
        self.assertEqual(calls[1][:4], ["codex", "exec", "--sandbox", "workspace-write"])
        cwd_index = calls[1].index("-C")
        self.assertEqual(calls[1][cwd_index:cwd_index + 3], ["-C", str(worktree), "resume"])
        self.assertNotIn("--approve-for-me", calls[1])
        self.assertIn("--json", calls[1])
        self.assertIn(started.session_id, calls[1])
        self.assertEqual(call_options[0]["cwd"], worktree)
        self.assertEqual(call_options[1]["cwd"], worktree)
        for options in call_options:
            self.assertEqual(options["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(options["env"]["PYTHONPYCACHEPREFIX"], "/provider-cache")

    def test_planner_environment_does_not_override_parent_bytecode_policy(self):
        parent_environment = {
            "HOME": "/home/test",
            "PATH": "/bin",
            "PYTHONDONTWRITEBYTECODE": "0",
            "PYTHONPYCACHEPREFIX": "/parent-cache",
        }
        with mock.patch.dict(os.environ, parent_environment, clear=True):
            planner_codex = loop_manager.agent_environment("codex", "planner")
            planner_claude = loop_manager.agent_environment("claude", "planner")
            worker_codex = loop_manager.agent_environment("codex", "worker")
            self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "0")

        self.assertEqual(planner_codex["PYTHONDONTWRITEBYTECODE"], "0")
        self.assertEqual(planner_claude["PYTHONDONTWRITEBYTECODE"], "0")
        self.assertEqual(worker_codex["PYTHONDONTWRITEBYTECODE"], "0")
        self.assertEqual(planner_codex["PYTHONPYCACHEPREFIX"], "/parent-cache")

    def test_resumed_read_only_codex_keeps_managed_worktree_boundary(self):
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        observed = {}

        def resumed(arguments, **kwargs):
            observed["arguments"] = arguments
            observed["cwd"] = kwargs["cwd"]
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertNotIn("-o", arguments)
            (kwargs["cwd"] / "resume-output.txt").write_text("managed\n", encoding="utf-8")
            process = mock.Mock()
            process.stdin = io.StringIO()
            process.stdout = io.StringIO(
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": "VERDICT: PASS",
                }}) + "\n"
            )
            process.wait.return_value = 0
            return process

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            base.mkdir()
            worktree = root / "managed"
            worktree.mkdir()
            state_dir = root / "state"
            with mock.patch(
                "loop_manager.role", return_value=("test-strong", "high")
            ), mock.patch("loop_manager.subprocess.Popen", side_effect=resumed):
                result = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    root / "unused.ini",
                    "reviewer",
                    "review",
                    read_only=True,
                    session_id=session_id,
                )

            self.assertEqual(result.code, 0)
            self.assertEqual(result.message, "VERDICT: PASS")
            self.assertEqual(observed["cwd"], worktree)
            self.assertTrue((worktree / "resume-output.txt").is_file())
            self.assertFalse((base / "resume-output.txt").exists())

        arguments = observed["arguments"]
        self.assertEqual(arguments[:4], ["codex", "exec", "--sandbox", "read-only"])
        cwd_index = arguments.index("-C")
        self.assertEqual(arguments[cwd_index:cwd_index + 3], ["-C", str(worktree), "resume"])
        self.assertIn('approvals_reviewer="auto_review"', arguments)
        self.assertIn(session_id, arguments)
