"""모델·외부 명령 없이 공통 운영 로그와 진행 관측을 검증한다."""
import io
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase, mock

from loop_manager_internal.runtime import operation_events as events
from loop_manager_internal.application.contracts import ProjectRunKind, ProjectRunResult
from loop_manager_internal.verification.manager_adapter import progress_observation
from loop_manager_internal.verification.timing import EventWriter, phase
from loop_manager_internal.agents.agent_invocation import AgentResult, run_agent_with_retries


class OperationObservabilityTests(TestCase):
    def test_log_shortens_identifiers_and_both_path_styles_only_for_display(self):
        run = "82961ed208bbf88263b9bacf52d72b32"
        runtime_sha = "fe06cca9d9bb4d57c9e67693caf0846a0adb7059"
        for project in ("/home/jeong/loop-manager/", "D:\\repo\\loop-manager\\"):
            with self.subTest(project=project):
                output = io.StringIO()
                with mock.patch("sys.stdout", output), events.log_context(run=run, project=project):
                    events.emit("runtime 준비 종료", runtime_sha=runtime_sha)
                    self.assertEqual(events.current_context()["run"], run)
                    self.assertEqual(events.current_context()["project"], project)
                self.assertIn("[run=82961ed project=loop-manager plan=- stage=-]", output.getvalue())
                self.assertIn("runtime_sha=fe06cca", output.getvalue())
                for full_value in (run, runtime_sha, project):
                    self.assertNotIn(full_value, output.getvalue())

    def test_context_isolated_in_parallel_and_values_cannot_inject_log_lines(self):
        output, barrier = io.StringIO(), threading.Barrier(2)
        def run(project):
            with events.log_context(run="run-1", project=project, plan="G-1", stage="VERIFYING"):
                barrier.wait(timeout=3)
                events.emit("진행\npassword=hidden", password="another-secret")
        with mock.patch("sys.stdout", output), ThreadPoolExecutor(2) as pool:
            list(pool.map(run, ("첫 프로젝트", "둘째 프로젝트")))
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for project in ('project="첫 프로젝트"', 'project="둘째 프로젝트"'):
            self.assertEqual(sum(project in line for line in lines), 1)
        self.assertNotIn("hidden", output.getvalue())
        self.assertNotIn("another-secret", output.getvalue())
        self.assertEqual(events.current_context(), {})

    def test_machine_result_stdout_is_reserved_and_warning_uses_stderr(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            with events.log_context(result_output=True):
                events.emit("원문 조회")
            events.emit("자동 재시도", level="WARN")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("INFO", stderr.getvalue())
        self.assertIn("WARN", stderr.getvalue())

    def test_operation_print_preserves_an_explicit_severity(self):
        operation_log = mock.Mock()
        events.operation_print(events.OperationPrintEffects(operation_log), "retry later", level="WARN")
        operation_log.assert_called_once_with("retry later", error=False, level="WARN")

    def test_output_failure_does_not_change_successful_operation(self):
        output = mock.Mock()
        output.write.side_effect = RuntimeError("출력 불가")
        with mock.patch("sys.stdout", output), events.activity("검증") as report:
            report.update(result="PASS")
        self.assertEqual(report["result"], "PASS")

    def test_short_activity_only_reports_completion_with_context(self):
        with mock.patch.object(events, "emit") as output, \
             mock.patch.object(events.time, "monotonic", return_value=10):
            with events.activity("프로젝트 준비", heartbeat=True, target="저장소") as report:
                report.update(result="다음 경계로 진행")
        output.assert_called_once_with("프로젝트 준비 종료", level="INFO", elapsed_seconds=0,
                                       target="저장소", result="다음 경계로 진행")

    def test_long_activity_reports_progress_and_completion_with_context(self):
        observed, records = threading.Event(), []
        def record(message, **fields):
            records.append((message, fields))
            if message == "Agent 호출 처리 진행 중":
                observed.set()
        with mock.patch.object(events, "emit", side_effect=record), \
             mock.patch.object(events, "PROGRESS_INTERVAL_SECONDS", .001):
            with events.activity("Agent 호출 처리", heartbeat=True, provider="codex") as report:
                self.assertTrue(observed.wait(2))
                report.update(result="실행 결과 수신")
        self.assertTrue(all(message == "Agent 호출 처리 진행 중" for message, _ in records[:-1]))
        self.assertEqual(records[-1][0], "Agent 호출 처리 종료")
        self.assertTrue(all(fields["provider"] == "codex" for _, fields in records))

    def test_nested_progress_reports_child_and_resumes_parent(self):
        seen, parent_before, child_seen, parent_after = [], threading.Event(), threading.Event(), threading.Event()
        child_finished = threading.Event()
        def record(message, **fields):
            seen.append((message, events.current_context()))
            if message == "부모 진행 중":
                if child_finished.is_set():
                    parent_after.set()
                else:
                    parent_before.set()
            elif message == "자식 진행 중":
                child_seen.set()
        with mock.patch.object(events, "emit", side_effect=record):
            with events.log_context(run="run-2", project="repo"):
                with events.progress("부모", interval=.001):
                    self.assertTrue(parent_before.wait(2))
                    with events.progress("자식", interval=.001):
                        self.assertTrue(child_seen.wait(2))
                    child_finished.set()
                    self.assertTrue(parent_after.wait(2))
                self.assertFalse(events.current_context().get("progress_active"))
        self.assertIn("자식 진행 중", [message for message, _ in seen])
        self.assertTrue(all(context["run"] == "run-2" for _, context in seen))

    def test_exception_is_not_reported_as_completion(self):
        with mock.patch.object(events, "emit") as output:
            with self.assertRaises(KeyboardInterrupt), events.activity("검증"):
                raise KeyboardInterrupt()
        self.assertEqual(output.call_args.kwargs["result"], "미확인")

    def test_activity_can_report_a_command_failure_as_error(self):
        with mock.patch.object(events, "emit") as output:
            with self.assertRaisesRegex(ValueError, "invalid"):
                with events.activity("명령 처리", error_level="ERROR"):
                    raise ValueError("invalid")
        self.assertEqual(output.call_args.kwargs["level"], "ERROR")

    def test_project_result_log_level_uses_result_kind_not_exit_code(self):
        cases = (
            (ProjectRunResult(ProjectRunKind.COMPLETED, 0), "INFO"),
            (ProjectRunResult(ProjectRunKind.WAITING, 0), "INFO"),
            (ProjectRunResult(ProjectRunKind.USAGE_LIMIT_PAUSE, 1), "INFO"),
            (ProjectRunResult(ProjectRunKind.RUNTIME_HANDOFF, 0), "INFO"),
            (ProjectRunResult(ProjectRunKind.TRANSIENT_PAUSE, 1), "WARN"),
            (ProjectRunResult(ProjectRunKind.BLOCKED, 1), "ERROR"),
            (ProjectRunResult(ProjectRunKind.CONFIGURATION_ERROR, 0), "ERROR"),
            (ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1), "ERROR"),
        )
        for result, level in cases:
            with self.subTest(kind=result.kind, exit_code=result.exit_code):
                self.assertEqual(result.log_level, level)

    def test_runtime_handoff_is_a_normal_pause(self):
        from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
        with mock.patch.object(events, "emit") as output:
            with self.assertRaises(SelfMergeRuntimeHandoff), events.activity("프로젝트"):
                raise SelfMergeRuntimeHandoff()
        self.assertEqual(output.call_args.kwargs["result"], "runtime_handoff")
        self.assertEqual(output.call_args.kwargs["level"], "INFO")

    def test_reporter_start_failure_preserves_operation_and_context(self):
        with mock.patch("threading.Thread.start", side_effect=RuntimeError("스레드 부족")):
            with events.progress("관측"):
                self.assertFalse(events.current_context().get("progress_active"))
        self.assertEqual(events.current_context(), {})

    def test_observation_failure_still_reports_unknown_progress(self):
        observed = threading.Event()
        fields = []
        def record(_message, **values):
            fields.append(values)
            observed.set()
        with mock.patch.object(events, "emit", side_effect=record):
            with events.progress("검증", describe=mock.Mock(side_effect=OSError()), interval=.001):
                self.assertTrue(observed.wait(2))
        self.assertTrue(all(item["progress_observation"] == "미확인" for item in fields))

    def test_retries_and_session_fallback_are_explained_without_raw_response(self):
        invoke = mock.Mock(side_effect=[AgentResult(1, "session not found"),
                                       AgentResult(1, "network error password=secret"), AgentResult(0, "done")])
        with mock.patch.object(events, "emit") as output:
            result = run_agent_with_retries(invoke, session_id="saved")
        self.assertEqual(result.code, 0)
        self.assertIn("세션 재개 불가", output.call_args_list[0].args[0])
        self.assertEqual(output.call_args_list[1].kwargs["retry"], 1)
        self.assertNotIn("secret", str(output.call_args_list))

    def test_verify_progress_reads_only_complete_new_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "worker.jsonl"
            records = [{"kind": "test_start", "id": "첫 테스트"},
                       {"kind": "allocation", "decision": {"granted": False, "reason": "메모리 대기"}}]
            path.write_text("".join(json.dumps(row) + "\n" for row in records) + '{"kind":"test_end",', encoding="utf-8")
            positions, state = {}, {}
            first = progress_observation(directory, positions, state)
            self.assertEqual((first["active_tests"], first["resource_wait"]), (1, "메모리 대기"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write('"id":"첫 테스트"}\n')
            second = progress_observation(directory, positions, state)
            self.assertEqual((second["active_tests"], second["observed_completed_tests"]), (0, 1))
            self.assertEqual(progress_observation(directory, positions, state), second)

    def test_verify_progress_keeps_run_identity_and_clears_stage_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage-0"
            stage.mkdir()
            worker_log = stage / "worker-1.jsonl"
            runner_log = root / "runner.jsonl"
            worker_events = [
                {"kind": "test_start", "run_id": "run-a", "clock_domain": "linux",
                 "stage_id": "unittest", "unit_id": "module.test", "worker_id": 1,
                 "pid": 11, "id": "tests.sample.Case.test_active", "at": 4},
                {"kind": "test_start", "run_id": "run-other", "clock_domain": "linux",
                 "stage_id": "unittest", "unit_id": "module.test", "worker_id": 1,
                 "pid": 11, "id": "tests.sample.Case.test_foreign", "at": 5},
            ]
            runner_events = [
                {"kind": "unit_start", "run_id": "run-a", "clock_domain": "linux",
                 "stage_id": "unittest", "unit_id": "module.test", "worker_id": 1,
                 "pid": 11, "at": 1},
                {"kind": "phase_start", "run_id": "run-a", "clock_domain": "linux",
                 "stage_id": "unittest", "pid": 10, "name": "resource_wait", "at": 2},
                {"kind": "allocation", "run_id": "run-a", "clock_domain": "linux",
                 "stage_id": "unittest", "pid": 10,
                 "decision": {"granted": False, "reason": "memory pressure"}, "at": 3},
                {"kind": "job_child_wait", "run_id": "run-a", "clock_domain": "linux",
                 "stage_id": "unittest", "unit_id": "module.test", "worker_id": 1,
                 "worker_pid": 11, "pid": 10, "active_processes": 2, "at": 6},
            ]
            worker_log.write_text("".join(json.dumps(item) + "\n" for item in worker_events), encoding="utf-8")
            runner_log.write_text("".join(json.dumps(item) + "\n" for item in runner_events), encoding="utf-8")
            positions, state = {}, {}

            observed = progress_observation(root, positions, state, "run-a")

            self.assertEqual(observed["active_tests"], 1)
            self.assertIn("module.test", observed["current_unit"])
            self.assertIn("tests.sample.Case.test_active", observed["current_test"])
            self.assertIn("memory pressure", observed["resource_wait"])
            self.assertIn("active=2", observed["job_child_wait"])
            self.assertEqual(observed["active_units"], 1)

            with runner_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": "phase_end", "run_id": "run-a",
                    "clock_domain": "linux", "stage_id": "unittest", "pid": 10,
                    "name": "stage:unittest", "start": 0, "duration": 7,
                    "result": "complete", "at": 7}) + "\n")
            observed = progress_observation(root, positions, state, "run-a")

            self.assertEqual(observed["active_tests"], 0)
            self.assertEqual(observed["active_units"], 0)
            self.assertEqual(observed["resource_wait"], "-")
            self.assertEqual(observed["job_child_wait"], "-")
            self.assertEqual(observed["current_test"], "-")

    def test_terminal_stage_or_run_event_dominates_worker_starts_in_same_observation(self):
        for terminal_kind in ("stage_end", "run_end"):
            with self.subTest(terminal=terminal_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / "stage-0"
                stage.mkdir()
                run_id = f"run-{terminal_kind}"
                terminal = {"kind": "phase_end", "run_id": run_id, "clock_domain": "linux",
                    "stage_id": "unittest", "pid": 10, "name": "stage:unittest",
                    "start": 0, "duration": 5, "result": "complete", "at": 5}
                if terminal_kind == "run_end":
                    terminal = {"kind": "run_end", "run_id": run_id, "clock_domain": "linux",
                                "stage_id": "unittest", "pid": 10, "at": 6}
                (root / "runner.jsonl").write_text(json.dumps(terminal) + "\n", encoding="utf-8")
                (stage / "worker-0.jsonl").write_text(json.dumps({
                    "kind": "test_start", "run_id": run_id, "clock_domain": "linux",
                    "stage_id": "unittest", "unit_id": "module.test", "worker_id": 0,
                    "pid": 11, "id": "tests.sample.Case.test_active", "at": 1}) + "\n",
                    encoding="utf-8")

                observed = progress_observation(root, {}, {}, run_id)

                self.assertEqual(observed["active_tests"], 0)
                self.assertEqual(observed["active_units"], 0)
                self.assertEqual(observed["current_test"], "-")

    def test_verify_progress_closes_phase_after_stage_identity_changes_without_run_end(self):
        for raises in (False, True):
            with self.subTest(raises=raises), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_id = "run-stage-drift"
                writer = EventWriter(root / "runner.jsonl", identity={"run_id": run_id})
                try:
                    with writer.activate():
                        try:
                            with phase("verify"):
                                writer.identity["stage_id"] = "unittest"
                                if raises:
                                    raise RuntimeError("verify failed")
                        except RuntimeError:
                            pass
                finally:
                    writer.close()

                observed = progress_observation(root, {}, {}, run_id)

                self.assertEqual(observed["active_phases"], "-")
                records = [json.loads(line) for line in (root / "runner.jsonl").read_text().splitlines()]
                self.assertNotEqual(records[0].get("stage_id"), records[1].get("stage_id"))
                self.assertEqual(records[0]["phase_id"], records[1]["phase_id"])
                self.assertEqual(records[1]["result"], "error" if raises else "complete")
                self.assertNotIn("run_end", [record["kind"] for record in records])

    def test_verify_progress_follows_windows_parent_snapshot_and_os_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "parent-run"
            root.mkdir()
            bundle = Path(temporary) / "loop-manager-verify-bundle"
            wsl_logs = bundle / "logs"
            wsl_logs.mkdir(parents=True)
            windows_logs = Path(str(root) + "-windows")
            windows_stage = windows_logs / "stage-0"
            windows_stage.mkdir(parents=True)
            (root / "snapshot.json").write_text(json.dumps({
                "run_id": "parent-run", "revision": "a" * 40, "bundle": str(bundle),
                "snapshot_sha256": "b" * 64}), encoding="utf-8")
            (root / "result.json").write_text(json.dumps({
                "run_id": "parent-run", "wsl": "PASS", "windows": "NOT_RUN", "wsl_run_id": "linux-run",
                "windows_run_id": "parent-run"}), encoding="utf-8")
            (wsl_logs / "worker-0.jsonl").write_text(json.dumps({
                "kind": "test_end", "run_id": "linux-run", "parent_run_id": "parent-run",
                "clock_domain": "linux", "stage_id": "unittest", "unit_id": "module.wsl",
                "worker_id": 0, "pid": 20, "id": "tests.wsl.Case.test_done", "at": 3,
                "duration": 1, "result": "success"}) + "\n"
                + json.dumps({"kind": "test_end", "run_id": "linux-run", "parent_run_id": "other-parent",
                    "clock_domain": "linux", "stage_id": "unittest", "unit_id": "module.old",
                    "worker_id": 0, "pid": 20, "id": "tests.wsl.Case.test_foreign", "at": 4,
                    "duration": 1, "result": "success"}) + "\n", encoding="utf-8")
            (windows_stage / "worker-0.jsonl").write_text(json.dumps({
                "kind": "test_start", "run_id": "parent-run", "clock_domain": "win32",
                "stage_id": "windows-core", "unit_id": "module.windows", "worker_id": 0,
                "pid": 20, "id": "tests.windows.Case.test_active", "at": 1}) + "\n",
                encoding="utf-8")
            (root / "launcher.jsonl").write_text(json.dumps({
                "kind": "phase_start", "run_id": "parent-run", "clock_domain": "win32",
                "pid": 10, "name": "windows-core", "at": 2}) + "\n", encoding="utf-8")

            observed = progress_observation(root, {}, {}, "parent-run")

            self.assertEqual(observed["observed_completed_tests"], 1)
            self.assertEqual(observed["active_tests"], 1)
            self.assertEqual(observed["current_stage"], "windows-core")
            self.assertEqual(observed["current_unit"], "module.windows")
            self.assertEqual(observed["windows_wsl"], "PASS")
            self.assertEqual(observed["windows_native"], "RUNNING")
            self.assertEqual(observed["snapshot_revision"], "a" * 40)
