"""모델·외부 명령 없이 공통 운영 로그와 진행 관측을 검증한다."""
import io
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase, mock

from loop_manager_internal.runtime import operation_events as events
from loop_manager_internal.verification.manager_adapter import progress_observation
from loop_manager_internal.agents.agent_invocation import AgentResult, run_agent_with_retries


class OperationObservabilityTests(TestCase):
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

    def test_progress_copies_context_stops_and_suppresses_nested_reporters(self):
        seen, observed = [], threading.Event()
        def record(message, **fields):
            seen.append((message, events.current_context()))
            observed.set()
        with mock.patch.object(events, "emit", side_effect=record):
            with events.log_context(run="run-2", project="repo"):
                with events.progress("부모", interval=.001), events.progress("자식", interval=.001):
                    self.assertTrue(observed.wait(2))
                self.assertFalse(events.current_context().get("progress_active"))
        self.assertTrue(all(message == "부모 진행 중" and context["run"] == "run-2" for message, context in seen))

    def test_exception_is_not_reported_as_completion(self):
        with mock.patch.object(events, "emit") as output:
            with self.assertRaises(KeyboardInterrupt), events.activity("검증"):
                raise KeyboardInterrupt()
        self.assertEqual(output.call_args.kwargs["result"], "미확인")

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
