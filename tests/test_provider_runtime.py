from contextlib import closing
import json
import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.worker import worker_result


class ProviderNeutralAgentRuntimeTest(unittest.TestCase):
    def test_policy_failure_stops_retry_and_session_fallback(self):
        failures = (
            "auto_review classifier timeout",
            "auto_review classifier timed out",
            "auto_review classifier timeout: cannot resume",
            "auto_review classifier unavailable: session not found; try again later",
            "guardian review failed: usage limit reached; cannot resume",
            "sandbox initialization failed: connection reset; thread not found",
            "request_permissions denied: unknown session",
            "approval policy invalid: service unavailable; cannot resume",
            "automatic approval review disabled: session expired",
            "guardian thread not found",
            "invalid value auto for --permission-mode: cannot resume",
        )
        for message in failures:
            for kind in (None, loop_manager.AgentResultKind.TRANSIENT_FAILURE):
                with self.subTest(message=message, kind=kind):
                    invoke = mock.Mock(return_value=loop_manager.AgentResult(
                        1, message, "saved-session", kind))
                    self.assertEqual(loop_manager.classify_agent_result(1, message),
                                     loop_manager.AgentResultKind.PERMANENT_CONFIGURATION)
                    self.assertFalse(loop_manager.session_resume_unavailable(message))
                    result = loop_manager.run_agent_with_retries(invoke, session_id="saved-session")
                    invoke.assert_called_once_with("saved-session", False)
                    self.assertEqual(result.session_id, "saved-session")
                    self.assertEqual(result.result_kind,
                                     loop_manager.AgentResultKind.PERMANENT_CONFIGURATION)

    def test_policy_banner_preserves_normal_transport_and_session_recovery(self):
        banner = "approvals_reviewer: auto_review\nsandbox: workspace-write\n"
        messages = (("service unavailable", loop_manager.AgentResultKind.TRANSIENT_FAILURE),
                    ("request timeout", loop_manager.AgentResultKind.TRANSIENT_FAILURE),
                    ("usage limit reached", loop_manager.AgentResultKind.USAGE_LIMIT),
                    ("session not found", loop_manager.AgentResultKind.PERMANENT_CONFIGURATION))
        for message, kind in messages:
            with self.subTest(message=message):
                self.assertEqual(loop_manager.classify_agent_result(1, banner + message), kind)
        self.assertTrue(loop_manager.session_resume_unavailable(banner + "session not found"))
        self.assertEqual(loop_manager.classify_agent_result(0, "classifier unavailable"),
                         loop_manager.AgentResultKind.SUCCESS)

    def test_legacy_project_migration_defaults_provider_to_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = state_dir / "loop-manager.db"
            legacy = sqlite3.connect(database)
            legacy.execute(
                "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
            )
            legacy.commit()
            legacy.close()

            with closing(loop_manager.connect_database(state_dir)) as migrated, migrated:
                columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(projects)")
                }
                row = migrated.execute(
                    "SELECT provider FROM projects WHERE repo = 'repo'"
                ).fetchone()

        self.assertIn("provider", columns)
        self.assertEqual(row["provider"], "codex")

    def test_codex_and_claude_profiles_validate_every_role_and_level(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        all_efforts = frozenset(
            {"low", "medium", "high", "xhigh", "max", "ultra"}
        )
        codex_models = {
            "gpt-6-astra": all_efforts,
            "gpt-5.6-sol": all_efforts,
            "gpt-5.6-terra": all_efforts,
            "gpt-5.6-luna": all_efforts,
        }

        loop_manager.validate_provider_profiles(config, "codex", codex_models)
        supported = subprocess.CompletedProcess(
            ["claude", "--init-only"], 0, stdout=""
        )
        with mock.patch(
            "loop_manager.command", return_value=supported
        ) as capability_command:
            claude_models = loop_manager.claude_model_efforts(config)
        loop_manager.validate_provider_profiles(config, "claude", claude_models)
        self.assertTrue(
            any(
                "--init-only" in call.args[0]
                for call in capability_command.call_args_list
            )
        )

        self.assertEqual(
            loop_manager.provider_worker_profile(
                config, "codex", 7, codex_models
            ),
            ("gpt-5.6-sol", "xhigh"),
        )
        self.assertEqual(
            loop_manager.provider_worker_profile(
                config, "claude", 1, claude_models
            ),
            ("claude-haiku-4-5-20251001", "medium"),
        )
        self.assertEqual(
            loop_manager.provider_profile(
                config,
                "claude",
                "claude.roles",
                "reviewer",
                "Claude reviewer",
                claude_models,
            ),
            ("claude-opus-5", "high"),
        )

    def test_invalid_claude_profile_stops_before_cli_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[claude.models]\nfast = claude-haiku-4-5-20251001\n"
                "[claude.worker]\nlevel1 = fast,minimal\n",
                encoding="utf-8",
            )
            with mock.patch("loop_manager.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                    loop_manager.run_claude(
                        root / "worktree",
                        root / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=1,
                    )

        run.assert_not_called()

    def test_claude_catalog_rejects_cli_unsupported_model_and_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[claude.models]\nstrong = claude-opus-999\n"
                "[claude.roles]\nplanner = strong,high\n",
                encoding="utf-8",
            )

            def reject_model(arguments, **_kwargs):
                return subprocess.CompletedProcess(
                    arguments,
                    1 if "claude-opus-999" in arguments else 0,
                    stdout="Unknown model",
                )

            with mock.patch(
                "loop_manager.command", side_effect=reject_model
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Model.*claude-opus-999"
                ):
                    loop_manager.claude_model_efforts(config, root)

            config.write_text(
                "[claude.models]\nstrong = claude-opus-5\n"
                "[claude.roles]\nplanner = strong,max\n",
                encoding="utf-8",
            )

            def reject_effort(arguments, **_kwargs):
                return subprocess.CompletedProcess(
                    arguments,
                    1 if "--effort" in arguments else 0,
                    stdout="Effort max is unavailable for this model",
                )

            with mock.patch(
                "loop_manager.command", side_effect=reject_effort
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Model/Effort.*claude-opus-5,max"
                ):
                    loop_manager.claude_model_efforts(config, root)

    def test_runtime_validation_rejects_missing_and_unauthenticated_cli(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with mock.patch("loop_manager.shutil.which", return_value=None), mock.patch(
            "loop_manager.command"
        ) as command:
            with self.assertRaisesRegex(RuntimeError, "설치되어 있지 않습니다"):
                loop_manager.validate_provider_runtime(config, "claude")
        command.assert_not_called()

        unauthenticated = subprocess.CompletedProcess(
            ["claude", "auth", "status"], 1, stdout="not logged in"
        )
        with mock.patch(
            "loop_manager.shutil.which", return_value="/bin/claude"
        ), mock.patch(
            "loop_manager.command", return_value=unauthenticated
        ), mock.patch(
            "loop_manager.claude_model_efforts"
        ) as model_efforts:
            with self.assertRaisesRegex(RuntimeError, "인증되어 있지 않습니다"):
                loop_manager.validate_provider_runtime(config, "claude")
        model_efforts.assert_not_called()
        authenticated = subprocess.CompletedProcess(
            ["claude", "auth", "status"], 0, stdout='{"loggedIn":true}'
        )
        with mock.patch(
            "loop_manager.shutil.which", return_value="/bin/claude"
        ), mock.patch(
            "loop_manager.command", return_value=authenticated
        ) as auth_command:
            loop_manager.validate_provider_runtime(config, "claude")
        self.assertEqual(
            auth_command.call_args_list[0].args[0],
            ["claude", "auth", "status"],
        )
        self.assertTrue(
            any(
                "--init-only" in call.args[0]
                for call in auth_command.call_args_list[1:]
            )
        )

    def test_claude_starts_resumes_and_applies_role_permissions(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        session_id = "claude-session"
        calls: list[tuple[list[str], dict[str, object]]] = []
        stream = (
            json.dumps({"type": "system", "session_id": session_id})
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "session_id": session_id,
                    "result": worker_result("Implement Claude adapter"),
                    "is_error": False,
                }
            )
            + "\n"
        )

        class StartedOutput:
            def __iter__(self):
                return iter(stream.splitlines(keepends=True))

            def close(self):
                pass

        class StartedProcess:
            def __init__(self, arguments, **kwargs):
                calls.append((arguments, kwargs))
                self.stdin = mock.Mock()
                self.stdout = StartedOutput()

            def wait(self):
                return 0

            def kill(self):
                raise AssertionError("completed Claude process must not be killed")

        resumed_payload = json.dumps(
            {
                "type": "result",
                "session_id": session_id,
                "result": "VERDICT: PASS",
                "is_error": False,
            }
        )

        def resumed(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return subprocess.CompletedProcess(arguments, 0, stdout=resumed_payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            state_dir = root / "state"
            started_sessions: list[str] = []
            with mock.patch(
                "loop_manager.claude_worker_profile",
                return_value=("claude-sonnet-5", "high"),
            ), mock.patch(
                "loop_manager.claude_role",
                return_value=("claude-opus-5", "high"),
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=StartedProcess
            ), mock.patch(
                "loop_manager.subprocess.run", side_effect=resumed
            ):
                started = loop_manager.run_claude(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "worker prompt",
                    read_only=False,
                    level=5,
                    persist_session=True,
                    on_session_started=started_sessions.append,
                )
                resumed_result = loop_manager.run_claude(
                    worktree,
                    state_dir,
                    config,
                    "reviewer",
                    "review prompt",
                    read_only=True,
                    persist_session=True,
                    session_id=session_id,
                )

        self.assertEqual(started.session_id, session_id)
        self.assertIn("COMMIT_TITLE:", started.message)
        self.assertEqual(started_sessions, [session_id])
        self.assertEqual(resumed_result.message, "VERDICT: PASS")
        start_arguments, start_options = calls[0]
        resume_arguments, resume_options = calls[1]
        self.assertIn("stream-json", start_arguments)
        self.assertEqual(
            start_arguments[
                start_arguments.index("--permission-mode") + 1
            ],
            "auto",
        )
        self.assertNotIn("worker prompt", start_arguments)
        self.assertEqual(start_options["cwd"], worktree)
        self.assertIn("--resume", resume_arguments)
        self.assertIn(session_id, resume_arguments)
        self.assertEqual(
            resume_arguments[
                resume_arguments.index("--permission-mode") + 1
            ],
            "auto",
        )
        self.assertIn("--disallowedTools", resume_arguments)
        self.assertEqual(resume_options["cwd"], worktree)
        for arguments in (start_arguments, resume_arguments):
            settings = json.loads(arguments[arguments.index("--settings") + 1])
            self.assertEqual(settings["permissions"]["defaultMode"], "auto")
            self.assertEqual(settings["sandbox"], {
                "enabled": True, "failIfUnavailable": True,
                "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False,
                "excludedCommands": [],
            })

    def test_claude_planner_processes_do_not_force_bytecode_isolation(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        session_id = "claude-planner-session"
        stream = (
            json.dumps({"type": "system", "session_id": session_id}) + "\n"
            + json.dumps({"type": "result", "session_id": session_id, "result": "planned"})
            + "\n"
        )

        def started_process(*_arguments, **_kwargs):
            process = mock.Mock()
            process.stdin = mock.Mock()
            process.stdout = io.StringIO(stream)
            process.wait.return_value = 0
            return process

        def resumed_process(arguments, **_kwargs):
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=json.dumps({"type": "result", "session_id": session_id, "result": "repaired"}),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            with mock.patch.dict(
                os.environ, {"PYTHONDONTWRITEBYTECODE": "0"}, clear=False
            ), mock.patch(
                "loop_manager.claude_role", return_value=("claude-opus-5", "high")
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=started_process
            ) as popen, mock.patch(
                "loop_manager.subprocess.run", side_effect=resumed_process
            ) as run:
                started = loop_manager.run_claude(
                    worktree, root / "state", config, "planner", "plan",
                    read_only=False, persist_session=True,
                )
                resumed = loop_manager.run_claude(
                    worktree, root / "state", config, "planner", "repair",
                    read_only=False, persist_session=True, session_id=started.session_id,
                )
                self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "0")

        self.assertEqual(started.session_id, session_id)
        self.assertEqual(resumed.session_id, session_id)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", popen.call_args.kwargs["env"])
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", run.call_args.kwargs["env"])

    def test_session_fallback_and_provider_errors_use_common_contract(self):
        sessions: list[tuple[str | None, bool]] = []

        def invoke(
            session_id: str | None, fallback: bool
        ) -> loop_manager.AgentResult:
            sessions.append((session_id, fallback))
            if session_id:
                return loop_manager.AgentResult(
                    1, "session was not found", session_id
                )
            return loop_manager.AgentResult(0, "fresh", "fresh-session")

        result = loop_manager.run_agent_with_retries(
            invoke, session_id="expired-session"
        )
        self.assertEqual(
            sessions,
            [("expired-session", False), (None, True)],
        )
        self.assertEqual(result.session_id, "fresh-session")
        self.assertEqual(
            loop_manager.classify_agent_result(1, "You've hit your limit"),
            loop_manager.AgentResultKind.USAGE_LIMIT,
        )
        self.assertEqual(
            loop_manager.classify_agent_result(1, "service unavailable"),
            loop_manager.AgentResultKind.TRANSIENT_FAILURE,
        )

        attempts = 0

        def timeout(
            _session_id: str | None, _fallback: bool
        ) -> loop_manager.AgentResult:
            nonlocal attempts
            attempts += 1
            raise subprocess.TimeoutExpired(["claude"], 30)

        timed_out = loop_manager.run_agent_with_retries(timeout, session_id=None)
        self.assertEqual(attempts, loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1)
        self.assertEqual(
            timed_out.result_kind,
            loop_manager.AgentResultKind.TRANSIENT_FAILURE,
        )

    def test_claude_stream_errors_preserve_output_for_classification_and_retry(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        class StreamOutput:
            def __init__(self, raw_output):
                self.raw_output = raw_output

            def __iter__(self):
                return iter(self.raw_output.splitlines(keepends=True))

            def close(self):
                pass

        class StreamProcess:
            def __init__(self, raw_output, returncode):
                self.stdin = mock.Mock()
                self.stdout = StreamOutput(raw_output)
                self.returncode = returncode

            def wait(self):
                return self.returncode

            def kill(self):
                raise AssertionError("completed Claude process must not be killed")

        success_stream = (
            json.dumps({"type": "system", "session_id": "fresh-session"})
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "session_id": "fresh-session",
                    "result": worker_result("Recovered after transient error"),
                    "is_error": False,
                }
            )
            + "\n"
        )
        process_results = iter(
            [
                ("service unavailable\n", 1),
                (success_stream, 0),
            ]
        )

        def start_process(*_args, **_kwargs):
            raw_output, returncode = next(process_results)
            return StreamProcess(raw_output, returncode)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            state_dir = root / "state"
            with mock.patch(
                "loop_manager.claude_worker_profile",
                return_value=("claude-sonnet-5", "high"),
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=start_process
            ) as popen:

                def invoke(_session_id, _fallback):
                    return loop_manager.run_claude(
                        worktree,
                        state_dir,
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=5,
                        persist_session=True,
                    )

                recovered = loop_manager.run_agent_with_retries(
                    invoke, session_id=None
                )

                malformed = '{"type": BROKEN "usage limit reached"\n'
                with mock.patch(
                    "loop_manager.subprocess.Popen",
                    return_value=StreamProcess(malformed, 1),
                ):
                    limited = loop_manager.run_claude(
                        worktree,
                        state_dir,
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=5,
                        persist_session=True,
                    )

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(recovered.session_id, "fresh-session")
        self.assertEqual(
            limited.message, '{"type": BROKEN "usage limit reached"'
        )
        self.assertEqual(
            limited.result_kind,
            loop_manager.AgentResultKind.USAGE_LIMIT,
        )

    def test_claude_zero_exit_stream_error_remains_failure(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        cases = (
            ({"type": "error", "error": {"message": "usage limit reached"}},
             "usage limit reached", loop_manager.AgentResultKind.USAGE_LIMIT, 1),
            ({"type": "error", "message": "service unavailable"},
             "service unavailable", loop_manager.AgentResultKind.TRANSIENT_FAILURE,
             loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1),
            ({"type": "error"}, '"type": "error"',
             loop_manager.AgentResultKind.PERMANENT_CONFIGURATION, 1),
            ({"type": "result", "is_error": True, "result": "usage limit reached"},
             "usage limit reached", loop_manager.AgentResultKind.USAGE_LIMIT, 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for session_id in (None, "stream-session"):
                for trailing_result in (False, True):
                    for error, expected_message, kind, attempts in cases:
                        with self.subTest(
                            session_id=session_id, trailing_result=trailing_result,
                            error=error,
                        ):
                            events = []
                            if session_id:
                                events.append({"type": "system", "session_id": session_id})
                            events.append(error)
                            if trailing_result:
                                events.append({"type": "result", "result": "done"})
                            stream = "\n".join(json.dumps(event) for event in events)

                            def start(*_args, **_kwargs):
                                process = mock.Mock()
                                process.stdout = io.StringIO(stream)
                                process.wait.return_value = 0
                                return process

                            with mock.patch(
                                "loop_manager.claude_worker_profile",
                                return_value=("claude-sonnet-5", "high"),
                            ), mock.patch(
                                "loop_manager.subprocess.Popen", side_effect=start
                            ):
                                result = loop_manager.run_claude(
                                    root, root / "state", config, "worker", "prompt",
                                    read_only=False, level=5, persist_session=True,
                                )

                            self.assertEqual(result.code, 1)
                            self.assertEqual(result.session_id, session_id)
                            self.assertIn(expected_message, result.message)
                            self.assertNotIn("세션 ID를 확인", result.message)
                            self.assertEqual(result.result_kind, kind)
                            invoke = mock.Mock(return_value=result)
                            retried = loop_manager.run_agent_with_retries(
                                invoke, session_id=None
                            )
                            self.assertEqual(invoke.call_count, attempts)
                            self.assertEqual(retried.result_kind, kind)
                            if session_id and attempts > 1:
                                self.assertEqual(
                                    invoke.call_args_list[1],
                                    mock.call(session_id, False),
                                )

    def test_provider_dispatch_and_environment_strip_manager_secrets(self):
        result = loop_manager.AgentResult(0, "ok", "session")
        with mock.patch(
            "loop_manager.run_codex", return_value=result
        ) as codex, mock.patch(
            "loop_manager.run_claude", return_value=result
        ) as claude:
            loop_manager.run_agent(
                "codex", Path("worktree"), Path("state"), Path("config"), "planner", "p",
                read_only=False,
            )
            loop_manager.run_agent(
                "claude", Path("worktree"), Path("state"), Path("config"), "reviewer", "p",
                read_only=True,
            )
        codex.assert_called_once()
        claude.assert_called_once()

        environment = {
            "HOME": "/home/test",
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "provider-secret",
            "TELEGRAM_TOKEN": "manager-secret",
            "LOOP_MANAGER_SECRET": "manager-secret",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            claude_environment = loop_manager.agent_environment("claude")
            codex_environment = loop_manager.agent_environment("codex")

        self.assertEqual(
            claude_environment["ANTHROPIC_API_KEY"], "provider-secret"
        )
        self.assertNotIn("TELEGRAM_TOKEN", claude_environment)
        self.assertNotIn("LOOP_MANAGER_SECRET", claude_environment)
        self.assertNotIn("ANTHROPIC_API_KEY", codex_environment)

    def test_cli_accepts_provider_only_as_registration_choice(self):
        args = loop_manager.build_parser().parse_args(
            ["config", "add", "repo", "--provider", "claude"]
        )
        self.assertEqual(args.provider, "claude")
        run = loop_manager.build_parser().parse_args(["run"])
        self.assertFalse(hasattr(run, "provider"))
        with redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ):
            loop_manager.build_parser().parse_args(
                ["run", "--provider", "claude"]
            )
