import io
import errno
import os
import datetime
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.application import commands as commands_app
from loop_manager_internal.application import manual_work as manual_work_app
from loop_manager_internal.application import project_runner as project_runner_app
from loop_manager_internal.application import scheduler as scheduler_app
from loop_manager_internal.application.contracts import ProjectRunKind, ProjectRunResult, ScheduledProjectResult
from loop_manager_internal.documents import document_drafts
from loop_manager_internal.runtime import operation_events as events
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.runtime import process_lock as process_lock_module


class TextAndBytesCapture(io.StringIO):
    def __init__(self):
        super().__init__()
        self.buffer = io.BytesIO()


class ExecutionProgressLogTest(unittest.TestCase):
    def test_operation_log_uses_fixed_local_timestamp_for_both_streams(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        fixed = datetime.datetime(2026, 9, 19, 8, 7, 6)
        with mock.patch("loop_manager.local_now", return_value=fixed), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            loop_manager.operation_log("started")
            loop_manager.operation_log("failed", error=True)
        prefix = r"^2026-09-19 08:07:06[+-]\d{2}:\d{2} "
        self.assertRegex(stdout.getvalue(), prefix + r"INFO \[run=- project=- plan=- stage=-\] started\n$")
        self.assertRegex(stderr.getvalue(), prefix + r"ERROR \[run=- project=- plan=- stage=-\] failed\n$")

    def test_status_result_output_uses_parsed_options_not_repository_text(self):
        cases = (
            (["status", "--", "--json"], False),
            (["status", "--", "--raw"], False),
            (["status", "/tmp/project", "--json"], True),
        )
        for arguments, result_output in cases:
            with self.subTest(arguments=arguments):
                stdout, stderr = io.StringIO(), io.StringIO()
                def report(*_args, **_kwargs):
                    loop_manager.operation_log("status probe")
                    return 0
                with mock.patch("sys.argv", ["loop_manager.py", *arguments]), \
                     mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr), \
                     mock.patch("loop_manager.state_directory", return_value=Path("state")), \
                     mock.patch("loop_manager.configure_registry", side_effect=report):
                    self.assertEqual(loop_manager.main(), 0)
                selected = stderr.getvalue() if result_output else stdout.getvalue()
                other = stdout.getvalue() if result_output else stderr.getvalue()
                self.assertIn("status probe", selected)
                self.assertNotIn("status probe", other)

    def test_document_draft_artifact_bytes_are_unchanged_and_logs_use_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "manager"
            repository.mkdir()
            content_file = root / "draft.md"
            content_file.write_bytes(b"draft input")
            artifact = b"original document\n--- diff ---\nproposed document\n"
            draft = types.SimpleNamespace(repo=str(repository))
            original_emit = events.emit
            connection = mock.Mock()
            connection.execute.return_value.fetchone.return_value = {
                "base_branch": "main", "provider": "codex",
            }
            drafts = mock.MagicMock()
            drafts.get.return_value = draft
            drafts.create.return_value = draft
            drafts.generation_prompt.return_value = ("prompt", "identity")
            agent_progress = threading.Event()
            def run_agent(*_args, **_kwargs):
                with events.activity("Agent 호출 처리", heartbeat=True):
                    self.assertTrue(agent_progress.wait(2))
                return object()
            def observe_emit(message, **fields):
                if message == "Agent 호출 처리 진행 중":
                    agent_progress.set()
                original_emit(message, **fields)
            effects = commands_app.DocumentDraftsCommandEffects(
                ROOT=repository,
                _coerce_agent_result=lambda *_args: types.SimpleNamespace(code=0, message="generated"),
                _recorded_base_fetch=mock.Mock(),
                connect_database=lambda _state: connection,
                is_loop_manager_repository=lambda _repo: True,
                operation_print=mock.Mock(),
                run_agent=mock.Mock(side_effect=run_agent),
            )
            for command in ("show", "generate", "create"):
                with self.subTest(command=command):
                    stdout, stderr = TextAndBytesCapture(), io.StringIO()
                    values = {"repository": str(repository), "draft_command": command}
                    if command == "show":
                        values["draft_id"] = "D-1"
                    else:
                        values["target"] = "docs/PRODUCT.md"
                    if command == "create":
                        values.update(content_file=str(content_file), meaning_evidence="unchanged meaning")
                    args = types.SimpleNamespace(**values)
                    with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr), \
                         mock.patch.object(document_drafts, "DocumentDraftRepository", return_value=drafts), \
                         mock.patch.object(document_drafts, "review_artifact_bytes", return_value=artifact) as render, \
                         mock.patch.object(document_drafts, "parse_generated_draft", return_value=(b"draft", "meaning")), \
                         mock.patch.object(events, "emit", side_effect=observe_emit), \
                         mock.patch.object(events, "PROGRESS_INTERVAL_SECONDS", .001), \
                         events.log_context(result_output=True):
                        self.assertEqual(commands_app._document_drafts_command(
                            effects, args, root / "state", root / "config.ini",
                        ), 0)
                    self.assertEqual(stdout.buffer.getvalue(), artifact)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("문서 수정본 처리 종료", stderr.getvalue())
                    if command == "generate":
                        self.assertIn("Agent 호출 처리 진행 중", stderr.getvalue())
                    render.assert_called_once_with(draft)

    def test_bootstrap_parent_and_runtime_child_keep_result_artifact_separate(self):
        repository = "/tmp/manager--json"
        target = "docs/PRODUCT.md"
        artifact = b"source bytes\n---\ndiff bytes\n"
        stdout, stderr = TextAndBytesCapture(), io.StringIO()
        parent_runs = []

        @contextmanager
        def process_lock(*_args, **_kwargs):
            yield object()

        def run_child(source, state_dir, config_path, runtime, arguments):
            parent_context = events.current_context()
            parent_runs.append(parent_context["run"])
            self.assertTrue(parent_context["result_output"])
            self.assertEqual(parent_context["project"], str(Path(repository).resolve()))
            self.assertEqual(parent_context["stage"], "BOOTSTRAP")
            loop_manager.operation_log("parent operational log")
            child_argv = [
                "loop_manager.py", "--runtime-child", "--runtime-source", str(source),
                "--runtime-sha", "a" * 40, *arguments,
            ]
            with mock.patch("sys.argv", child_argv), mock.patch.dict(os.environ, {
                "LOOP_MANAGER_RUN_ID": parent_context["run"],
                "LOOP_MANAGER_RUNTIME_HANDOFF_TOKEN": "",
            }):
                return loop_manager.main()

        def execute_child(args, *_args):
            child_context = events.current_context()
            self.assertTrue(child_context["result_output"])
            self.assertEqual(child_context["project"], str(Path(repository).resolve()))
            self.assertEqual(child_context["run"], parent_runs[-1])
            loop_manager.operation_log("child operational log")
            sys.stdout.buffer.write(artifact)
            return 0

        with mock.patch("sys.argv", [
            "loop_manager.py", "document-drafts", repository, "generate", target,
        ]), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr), \
             mock.patch("loop_manager.state_directory", return_value=Path("/tmp/state")), \
             mock.patch("loop_manager.execution_path.apply_execution_path"), \
             mock.patch("loop_manager.process_lock", side_effect=process_lock), \
             mock.patch("loop_manager.prepare_bootstrap_runtime", return_value=types.SimpleNamespace(commit_sha="a" * 40)), \
             mock.patch("loop_manager._write_process_lock_identity"), \
             mock.patch("loop_manager.run_runtime_child", side_effect=run_child), \
             mock.patch("loop_manager.validate_runtime_child"), \
             mock.patch("loop_manager.validate_runtime_mutation", return_value=None), \
             mock.patch("loop_manager.execute_runtime_command", side_effect=execute_child):
            self.assertEqual(loop_manager.main(), 0)

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stdout.buffer.getvalue(), artifact)
        self.assertIn("runtime 준비 종료", stderr.getvalue())
        self.assertIn("parent operational log", stderr.getvalue())
        self.assertIn("child operational log", stderr.getvalue())
        self.assertEqual(events.current_context(), {})

    def test_manual_command_logs_its_own_repository_and_plan_scope(self):
        connection = mock.Mock()
        observed, nested_results = [], []
        nested_progress = threading.Event()
        service = mock.Mock()
        def validate_repository():
            observed.append(events.current_context())
            with events.progress("수동 하위 관측", interval=.001):
                nested_results.append(nested_progress.wait(2))
        service.validate_repository.side_effect = validate_repository
        service.start.return_value = 0
        effects = types.SimpleNamespace(
            connect_database=mock.Mock(return_value=connection),
            operation_print=mock.Mock(),
        )
        args = types.SimpleNamespace(
            repository="/tmp/manual-repo", manual_command="start", plan_id="G-283",
        )
        with mock.patch.object(manual_work_app, "ManualWorkService", return_value=service), \
             mock.patch.object(events, "emit", side_effect=lambda message, **_fields:
                               nested_progress.set() if message == "수동 하위 관측 진행 중" else None):
            self.assertEqual(manual_work_app.manual_command(
                effects, args, Path("state"), Path("config.ini"),
            ), 0)
        self.assertEqual(nested_results, [True])
        self.assertEqual(observed[0]["project"], str(Path(args.repository).resolve()))
        self.assertEqual(observed[0]["plan"], "G-283")
        self.assertEqual(observed[0]["stage"], "MANUAL_START")
        self.assertEqual(events.current_context(), {})

    def test_recovery_wait_keeps_request_scope_and_nonzero_exit_warn_level(self):
        request = types.SimpleNamespace(repo="/tmp/recovery-repo", plan_id="G-283",
                                        request_id="request-1", resumed=False)
        connection = mock.Mock()
        def query(sql, *_args):
            cursor = mock.Mock()
            cursor.fetchone.return_value = {"provider": "codex", "planning_error": None} \
                if sql.startswith("SELECT provider") else None
            cursor.fetchall.return_value = []
            return cursor
        connection.execute.side_effect = query
        outcome = ProjectRunResult(ProjectRunKind.TRANSIENT_PAUSE, 0, "retry later")
        observed, emitted, nested_results = [], [], []
        verify_progress = threading.Event()
        def run_project(*_args, **_kwargs):
            observed.append(events.current_context())
            with events.progress("verify", interval=.001):
                nested_results.append(verify_progress.wait(2))
            return outcome
        def record(message, **fields):
            emitted.append((message, fields))
            if message == "verify 진행 중":
                verify_progress.set()
        effects = commands_app.LocalRecoveryExecutionEffects(
            operation_print=mock.Mock(), run_project_result=run_project,
        )
        after = {
            "phase": None, "planning_error_sha256": None, "plans": [],
            "blocks": [], "current_failures": [],
        }
        with mock.patch.object(commands_app.local_recovery_state, "snapshot", return_value=after), \
             mock.patch.object(commands_app.local_recovery_state, "complete_request"), \
             mock.patch.object(events, "emit", side_effect=record):
            result = commands_app.execute_local_recovery_request(
                effects, connection, Path("state"), request, Path("config.ini"),
                planner_mutex=threading.Lock(),
                refactoring_review_interval=20, verify_timeout_seconds=600,
            )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.kind, ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(nested_results, [True])
        self.assertEqual(observed[0]["project"], request.repo)
        self.assertEqual(observed[0]["plan"], request.plan_id)
        self.assertEqual(observed[0]["stage"], "RECOVERY")
        effects.operation_print.assert_any_call("retry later", level="WARN")
        logged = next(fields for message, fields in emitted if message == "로컬 환경 재검사 처리 종료")
        self.assertEqual(logged["level"], "WARN")
        self.assertEqual(events.current_context(), {})

    def test_whole_run_level_uses_project_outcomes_not_exit_code(self):
        cases = (
            (ProjectRunKind.USAGE_LIMIT_PAUSE, "INFO"),
            (ProjectRunKind.TRANSIENT_PAUSE, "WARN"),
            (ProjectRunKind.BLOCKED, "ERROR"),
        )
        for kind, expected_level in cases:
            with self.subTest(kind=kind):
                connection = sqlite3.connect(":memory:")
                emitted = []
                outcome = ScheduledProjectResult(
                    "/tmp/project", ProjectRunResult(kind, 1, "observed"),
                )
                config = types.SimpleNamespace(
                    telegram_token="", telegram_chat_id="", verify_timeout_seconds=600,
                )
                effects = scheduler_app.RunRegisteredProjectsEffects(
                    operation_log=mock.Mock(),
                    schedule_registered_projects=mock.Mock(return_value=(outcome,)),
                )
                with mock.patch.object(scheduler_app.refresh, "paused", return_value=False), \
                     mock.patch.object(scheduler_app.notifications, "configure_session"), \
                     mock.patch.object(scheduler_app.notifications, "recover_interrupted_starts"), \
                     mock.patch.object(scheduler_app.notifications, "dispatch_pending"), \
                     mock.patch.object(scheduler_app.events, "emit", side_effect=lambda message, **fields: emitted.append((message, fields))):
                    self.assertEqual(scheduler_app.run_registered_projects(
                        effects, connection, Path("state"), config,
                    ), 1)
                connection.close()
                final = next(fields for message, fields in emitted if message == "전체 실행 종료")
                self.assertEqual(final["level"], expected_level)
                self.assertEqual(final["failed"], 1)

    def test_transient_git_lock_is_warn_while_preserving_success_exit(self):
        connection = mock.Mock()
        connection.execplans_schema_ready = True
        connection.execute.return_value = mock.MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        effects = project_runner_app.InternalRunProjectResultEffects(
            _checkpoint_dispatch_ready=mock.Mock(return_value=False),
            _record_project_recovery=mock.Mock(),
            _run_project_step=mock.Mock(side_effect=GitLockError("locked")),
            _stable_work_reason=mock.Mock(),
            catalog_schedule_decision=mock.Mock(),
            dispatch_blocked_checkpoints=mock.Mock(),
            is_loop_manager_repository=mock.Mock(),
            operation_log=mock.Mock(),
        )
        result = project_runner_app._run_project_result(
            effects, connection, Path("state"), "/tmp/project", Path("config.ini"),
            None, False,
        )
        self.assertEqual(result.kind, ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(result.exit_code, 0)
        effects.operation_log.assert_called_once_with(
            "일시적 Git lock 충돌 (/tmp/project): locked", level="WARN",
        )

    def test_skip_if_running_only_silences_lock_contention(self):
        state_dir = Path("state")
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run", "--skip-if-running"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock",
            side_effect=loop_manager.ProcessLockBusyError("busy"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 0)
        output.assert_not_called()
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock",
            side_effect=loop_manager.ProcessLockBusyError("busy"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 1)
        self.assertTrue(output.called)
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run", "--skip-if-running"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock", side_effect=RuntimeError("broken")
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 1)
        self.assertTrue(output.called)

    def test_process_lock_only_classifies_contention_errnos_as_busy(self):
        for platform, lock_module in (("posix", "fcntl"), ("nt", "msvcrt")):
            for error_number, expected in (
                (errno.EAGAIN, loop_manager.ProcessLockBusyError),
                (errno.ENOLCK, OSError),
            ):
                with self.subTest(platform=platform, errno=error_number):
                    error = OSError(error_number, "lock failed")
                    error.winerror = 0
                    lock = mock.Mock(side_effect=error)
                    handle = mock.Mock()
                    fake_module = (
                        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lock)
                        if lock_module == "fcntl"
                        else types.SimpleNamespace(LK_NBLCK=1, locking=lock)
                    )
                    with mock.patch.object(process_lock_module.os, "name", platform), mock.patch.object(
                        process_lock_module.os, "open", return_value=10
                    ), mock.patch.object(
                        process_lock_module.os, "fdopen", return_value=handle
                    ), mock.patch.dict(sys.modules, {lock_module: fake_module}):
                        with self.assertRaises(expected) as raised:
                            with process_lock_module.process_lock(mock.MagicMock()):
                                pass
                    if expected is OSError:
                        self.assertEqual(raised.exception.errno, errno.ENOLCK)
                    handle.close.assert_called_once()

    def test_process_lock_closes_handle_when_unlock_fails_on_each_platform(self):
        for platform, lock_module in (("posix", "fcntl"), ("nt", "msvcrt")):
            with self.subTest(platform=platform):
                state_dir = mock.MagicMock()
                handle = mock.Mock()
                handle.fileno.return_value = 10
                entered = mock.Mock()
                lock = mock.Mock(side_effect=[None, OSError("unlock failed")])
                fake_msvcrt = types.SimpleNamespace(
                    LK_NBLCK=1, LK_UNLCK=2, locking=lock,
                )
                fake_fcntl = types.SimpleNamespace(
                    LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=lock,
                )
                patches = [
                    mock.patch.object(process_lock_module.os, "name", platform),
                    mock.patch.object(process_lock_module.os, "open", return_value=10),
                    mock.patch.object(process_lock_module.os, "fdopen", return_value=handle),
                    mock.patch.dict(sys.modules, {lock_module: fake_fcntl if lock_module == "fcntl" else fake_msvcrt}),
                ]
                with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                    process_lock_module.os, "fsync"
                ) as fsync:
                    with self.assertRaisesRegex(OSError, "unlock failed"):
                        with process_lock_module.process_lock(state_dir) as acquired:
                            entered(acquired)
                entered.assert_called_once_with(handle)
                fsync.assert_called_once_with(10)
                handle.close.assert_called_once()
                self.assertEqual(lock.call_args_list, [
                    mock.call(10, 1, 1), mock.call(10, 2, 1),
                ] if platform == "nt" else [
                    mock.call(10, 3), mock.call(10, 4),
                ])

    @unittest.skipIf(os.name == "nt", "POSIX 셸 실행 fixture")
    def test_bash_launcher_skips_git_and_preserves_run_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run.sh"
            launcher.write_text((loop_manager.ROOT / "run.sh").read_text(encoding="utf-8"), encoding="utf-8")
            python = root / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/usr/bin/env bash\nprintf 'python:%s\\n' \"$*\"\n", encoding="utf-8")
            python.chmod(0o755)
            git = root / "bin/git"
            git.parent.mkdir()
            git.write_text("#!/usr/bin/env bash\nprintf 'git-called:%s\\n' \"$*\"\nexit 99\n", encoding="utf-8")
            git.chmod(0o755)
            env = {**os.environ, "PATH": f"{git.parent}:{os.environ['PATH']}"}
            completed = subprocess.run(["bash", str(launcher), "target-repo"], cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0)
            self.assertNotIn("git-called:", completed.stdout)
            self.assertIn("python:-u", completed.stdout)
            self.assertIn("run target-repo", completed.stdout)

    def test_launchers_have_no_timestamp_banner_or_git_execution(self):
        powershell = (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8")
        bash = (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("Get-Date -Format", powershell)
        self.assertNotIn("date ", bash)
        self.assertNotIn("git ", powershell.casefold())
        self.assertNotIn("git ", bash.casefold())
