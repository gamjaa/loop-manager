"""시간·수집·결과·프로세스 경계만 작은 fake로 검증한다."""
import ctypes
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.config.managed_config import load_managed_config, ManagedConfigError
from loop_manager_internal.execution import verify_process, windows_job
from loop_manager_internal.verification import guard, manifest, runner, manager_adapter
from loop_manager_internal.verification.coordination import execution_settings
from loop_manager_internal.execution.validation_feedback import MAX_DIAGNOSTIC, format_verify_failure
from loop_manager_internal.verification.timing import EventWriter, TimedResult, comparison, fixture_timing, summarize


class TimingAndManifestTests(unittest.TestCase):
    def test_comparison_rejects_changed_scope_environment_and_incomplete_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps([{"ids": ["case"]}]))
            summary = {"metadata": {"revision": "first", "cap": 1, "python": "fake", "git": "fake",
                       "environment": {"host": "same", "platform": "fake", "limits": {
                           "cpus": 4, "affinity_cpus": 4, "applied_quota": None, "source": "fake"}}},
                       "phases": [{"name": "verify", "duration": 20}], "result": {"passed": True},
                       "active": [], "counts": {"success": 1}}
            other = json.loads(json.dumps(summary))
            other["metadata"].update(revision="second", cap="auto")
            other["phases"][0]["duration"] = 10
            report = comparison([root, root], [summary, other])
            self.assertTrue(report["comparable"])
            self.assertEqual([run["seconds"] for run in report["runs"]], [20, 10])
            other["metadata"]["environment"]["limits"]["cpus"] = 2
            self.assertFalse(comparison([root, root], [summary, other])["comparable"])
            other["metadata"]["environment"]["limits"]["cpus"] = 4
            other["active"] = [{"id": "unfinished"}]
            self.assertFalse(comparison([root, root], [summary, other])["comparable"])
            changed = root / "changed"
            changed.mkdir()
            (changed / "manifest.json").write_text(json.dumps([{"ids": ["another"]}]))
            self.assertFalse(comparison([root, changed], [summary, summary])["scope_matches"])

    def run_suite(self, suite, root, clock=lambda: 1.0):
        writer = EventWriter(root / "events.jsonl", clock=clock)
        try:
            with writer.activate(), fixture_timing():
                result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=lambda *args, **kwargs:
                    TimedResult(*args, writer=writer, **kwargs)).run(suite)
        finally:
            writer.close()
        return result, summarize(root, now=20)

    def test_test_and_class_fixture_durations_include_cleanup(self):
        clock = [0.0]
        def advance(value):
            clock[0] += value
        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                advance(7)
                cls.addClassCleanup(advance, 5)
            def setUp(self):
                advance(1)
                self.addCleanup(advance, 4)
            def test_case(self):
                advance(2)
            def tearDown(self):
                advance(3)
        with tempfile.TemporaryDirectory() as directory:
            with fixture_timing():
                result, summary = self.run_suite(
                    unittest.TestSuite([Case("test_case")]), Path(directory), lambda: clock[0],
                )
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(summary["tests"][0]["duration"], 10)
        class_phases = [event for event in summary["phases"] if "Class" in event["name"]]
        self.assertEqual(sorted(event["duration"] for event in class_phases), [5, 7])

    def test_all_result_kinds_subtest_and_cleanup_errors_preserve_failure(self):
        class Cases(unittest.TestCase):
            def test_success(self):
                pass
            def test_failure(self):
                self.fail("expected fake failure")
            def test_error(self):
                self.addCleanup(lambda: 1 / 0)
            @unittest.skip("fake skip")
            def test_skip(self):
                pass
            @unittest.expectedFailure
            def test_expected(self):
                self.fail("expected")
            @unittest.expectedFailure
            def test_unexpected(self):
                pass
            def test_subtest(self):
                with self.subTest(value=1):
                    self.fail("fake subtest")
        with tempfile.TemporaryDirectory() as directory:
            result, summary = self.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Cases), Path(directory))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(summary["counts"], {"success": 1, "failure": 2, "error": 1, "skip": 1,
                                           "expected_failure": 1, "unexpected_success": 1})

    def test_truncated_event_preserves_active_test_without_false_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = EventWriter(root / "events.jsonl", clock=lambda: 1)
            writer.emit("test_start", id="unfinished")
            writer.close()
            with (root / "events.jsonl").open("a") as stream:
                stream.write('{"kind":"test_end"')
            summary = summarize(root, now=10)
        self.assertEqual(summary["counts"], {})
        self.assertEqual(summary["active"][0]["elapsed"], 9)

    def test_stopped_run_preserves_interruption_elapsed_when_report_is_read_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = [1]
            writer = EventWriter(root / "events.jsonl", clock=lambda: clock[0])
            writer.emit("test_start", id="unfinished")
            clock[0] = 4
            writer.emit("phase_end", name="verify_process", duration=3, result="complete")
            writer.close()
            summary = summarize(root, now=100)
        self.assertEqual(summary["active"][0]["elapsed"], 3)
        self.assertFalse(summary["tests"])

    def test_custom_load_tests_structure_and_manifest_validation(self):
        module = ModuleType("verify_fake_module")
        class Case(unittest.TestCase):
            def test_case(self):
                pass
        Case.__module__, Case.__qualname__ = module.__name__, "Case"
        calls = []
        class Suite(unittest.TestSuite):
            pass
        def load_tests(loader, tests, pattern):
            calls.append(pattern)
            return Suite([Case("test_case")])
        module.load_tests = load_tests
        loader = manifest.ManifestLoader()
        loader.loadTestsFromModule(module, pattern="test*.py")
        definition = loader.modules[0]
        with mock.patch.dict(sys.modules, {module.__name__: module}):
            self.assertIsInstance(manifest.load_module(definition), Suite)
        self.assertEqual(calls, ["test*.py", "test*.py"])
        valid = {"module": definition.name, "passed": True, "completed": list(definition.ids)}
        self.assertTrue(manifest.validate_report(definition, valid))
        for changed in ({"completed": []}, {"completed": [*definition.ids, *definition.ids]}, {"module": "other"}):
            with self.assertRaises(RuntimeError):
                manifest.validate_report(definition, {**valid, **changed})

    def test_fixture_skip_is_not_fabricated_test_completion(self):
        module = ModuleType("verify_fake_skip")
        module.setUpModule = mock.Mock(side_effect=unittest.SkipTest("fake fixture skip"))
        class Case(unittest.TestCase):
            def test_case(self):
                self.fail("must not run")
        Case.__module__, Case.__qualname__ = module.__name__, "Case"
        case = Case("test_case")
        definition = manifest.Module(module.__name__, (case.id(),), ((case.id(), module.__name__, module.__name__ + ".Case"),))
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {module.__name__: module}):
            result, summary = self.run_suite(unittest.TestSuite([case]), Path(directory))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(summary["tests"], [])
        report = {"module": module.__name__, "passed": True, "completed": [], "fixture_skipped": [case.id()],
                  "fixture_skip_sources": [f"setUpModule ({module.__name__})"]}
        self.assertTrue(manifest.validate_report(definition, report))
        report["fixture_skip_sources"] = []
        with self.assertRaises(RuntimeError):
            manifest.validate_report(definition, report)


class GuardAndConfigurationTests(unittest.TestCase):
    def test_cli_guard_allows_git_and_fake_boundary_but_rejects_real_agent_commands(self):
        for command in (["codex", "exec"], ["C:\\bin\\claude.exe", "-p"], ["bash", "-c", "codex exec prompt"]):
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                guard.check_command(command, enabled=True)
        guard.check_command(["git", "push"], enabled=True)
        guard.check_command([sys.executable, "-c", "print('codex')"], enabled=True)

    def test_audit_guard_blocks_api_and_persists_for_explicit_child_environment(self):
        hooks = []
        guard.install_guard.cache_clear()
        self.addCleanup(guard.install_guard.cache_clear)
        with mock.patch.object(sys, "addaudithook", side_effect=hooks.append):
            guard.install_guard()
        audit = hooks[0]
        with self.assertRaises(RuntimeError):
            audit("subprocess.Popen", (b"codex", ["git", "status"], None, {}))
        with self.assertRaises(RuntimeError):
            audit("socket.getaddrinfo", ("api.openai.com", 443))
        with self.assertRaises(RuntimeError):
            audit("socket.connect", (None, ("1.2.3.4", 443)))
        audit("socket.connect", (None, ("127.0.0.1", 1234)))
        environment = {"PATH": "fake", "LOOP_MANAGER_VERIFY_SECRET": "must not propagate"}
        audit("subprocess.Popen", (sys.executable, [sys.executable, "-c", "pass"], None, environment))
        self.assertEqual(environment[guard.MARKER], "1")
        self.assertIn("child_guard", environment["PYTHONPATH"])
        self.assertNotIn("LOOP_MANAGER_VERIFY_SECRET", guard.worker_environment(environment))
        with self.assertRaises(RuntimeError):
            audit("subprocess.Popen", (None, 'cmd /c "codex exec"', None, {}))
        windows_environment = {}
        audit("subprocess.Popen", (None, 'git rev-parse HEAD', None, windows_environment))
        self.assertEqual(windows_environment[guard.MARKER], "1")
        self.assertIn("child_guard", windows_environment["PYTHONPATH"])

    def test_managed_parallelism_defaults_caps_and_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            path.write_text("[loop]\n", encoding="utf-8")
            self.assertEqual(load_managed_config(path).verify_parallelism, "auto")
            for value in ("auto", "1", "4"):
                path.write_text(f"[loop]\nverify_parallelism = {value}\n", encoding="utf-8")
                self.assertEqual(str(load_managed_config(path).verify_parallelism), value)
            for value in ("0", "no", "2.5"):
                path.write_text(f"[loop]\nverify_parallelism = {value}\n", encoding="utf-8")
                with self.assertRaisesRegex(ManagedConfigError, "verify_parallelism"):
                    load_managed_config(path)


class VerifyProcessBoundaryTests(unittest.TestCase):
    def test_linux_freeze_failure_still_kills_every_observed_child(self):
        def signal(pid, _birth, action):
            if pid == 101 and action == 9:
                raise OSError("secondary failure")
        with mock.patch.object(verify_process.signal, "SIGSTOP", 19, create=True), mock.patch.object(
            verify_process.signal, "SIGKILL", 9, create=True), mock.patch.object(
            verify_process.os, "killpg", create=True), mock.patch.object(
            verify_process, "_linux_process_info", return_value=(0, 0, "T", 1)), mock.patch.object(
            verify_process, "_wait_linux_process_stopped"), mock.patch.object(
            verify_process, "_linux_descendants", side_effect=[{101: 1, 102: 2}, RuntimeError("primary failure")]), mock.patch.object(
            verify_process, "_signal_linux_process", side_effect=signal) as signals:
            with self.assertRaisesRegex(verify_process.VerifyTerminationUncertain, "primary failure.*secondary failure"):
                verify_process._stop_linux_tree(123)
        self.assertEqual([call.args[0] for call in signals.call_args_list if call.args[2] == 9], [101, 102])

    def test_windows_job_is_assigned_before_start_and_waits_for_descendants(self):
        root = Path.cwd()
        process, job, events = mock.Mock(), mock.Mock(), []
        process.wait.return_value, process.returncode = 0, 0
        job.assign.side_effect = lambda _process: events.append("assign")
        process.stdin.write.side_effect = lambda _data: events.append("start")
        with mock.patch.object(verify_process.os, "name", "nt"), mock.patch.object(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), mock.patch.object(
            windows_job, "WindowsJob", return_value=job):
            result = verify_process.run_verify_process(["verify"], root, 10,
                launch_process=mock.Mock(return_value=process), job_scope=True,
                on_started=lambda *_args: events.append("bind"))
        self.assertEqual(events, ["assign", "bind", "start"])
        self.assertEqual(result[:2], (True, False))
        job.wait.assert_called_once()
        job.close.assert_called_once()

    def test_windows_parent_exit_cannot_hide_active_children(self):
        root = Path.cwd()
        process, job = mock.Mock(), mock.Mock()
        process.wait.return_value, process.returncode = 0, 0
        job.wait.side_effect = [TimeoutError("child active"), None]
        with mock.patch.object(verify_process.os, "name", "nt"), mock.patch.object(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), mock.patch.object(
            windows_job, "WindowsJob", return_value=job):
            result = verify_process.run_verify_process(["verify"], root, 10,
                launch_process=mock.Mock(return_value=process), job_scope=True)
        self.assertEqual(result[:2], (False, True))
        job.terminate.assert_called_once()

    def test_linux_cleanup_attempts_every_known_child_after_signal_failure(self):
        process = mock.Mock(pid=123)
        def signal(pid, *_args):
            if pid == 101:
                raise OSError("fake signal failure")
        with mock.patch.object(verify_process.os, "name", "posix"), mock.patch.object(
            verify_process.sys, "platform", "linux"), mock.patch.object(
            verify_process.signal, "SIGKILL", 9, create=True), mock.patch.object(
            verify_process, "_stop_linux_tree", return_value={101: 1, 102: 2}), mock.patch.object(
            verify_process.os, "killpg", create=True), mock.patch.object(
            verify_process, "_signal_linux_process", side_effect=signal) as signals, mock.patch.object(
            verify_process, "_confirm_posix_group_stopped"), mock.patch.object(
            verify_process, "_confirm_linux_descendants_stopped"):
            with self.assertRaises(verify_process.VerifyTerminationUncertain):
                verify_process.terminate_verify_process(process)
        self.assertEqual([call.args[0] for call in signals.call_args_list], [101, 102])

    def test_windows_job_limit_and_handle_lifetime_with_fake_kernel(self):
        kernel = mock.Mock()
        kernel.CreateJobObjectW.return_value = 99
        kernel.SetInformationJobObject.return_value = 1
        kernel.AssignProcessToJobObject.return_value = 1
        def accounting(_handle, _kind, pointer, *_args):
            ctypes.cast(pointer, ctypes.POINTER(windows_job.Accounting)).contents.active = 0
            return 1
        kernel.QueryInformationJobObject.side_effect = accounting
        job = windows_job.WindowsJob(kernel=kernel)
        job.assign(SimpleNamespace(_handle=100))
        self.assertEqual(job.active(), 0)
        job.close()
        job.close()
        kernel.CloseHandle.assert_called_once_with(99)


class RunnerPoolTests(unittest.TestCase):
    def test_compile_error_is_not_converted_to_success_or_resource_fallback(self):
        with mock.patch.object(runner.py_compile, "compile", side_effect=runner.py_compile.PyCompileError(
            SyntaxError, SyntaxError("fake syntax failure"), "loop_manager.py")):
            with self.assertRaises(runner.py_compile.PyCompileError):
                runner.compile_project(Path.cwd())

    def test_linux_bootstrap_preserves_worker_input_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            process = mock.Mock()
            with mock.patch.object(sys, "platform", "linux"), mock.patch.object(runner, "os", SimpleNamespace(name="posix", environ=os.environ)), mock.patch.object(
                runner.subprocess, "Popen", return_value=process) as launch:
                worker = runner.start_worker("token", 0, Path(directory))
            try:
                bootstrap = launch.call_args.args[0][2]
                self.assertIn("stdin=sys.stdin", bootstrap)
                self.assertIn("os.waitpid(-1, 0)", bootstrap)
                self.assertTrue(launch.call_args.kwargs["start_new_session"])
            finally:
                worker.output.close()

    def test_worker_protocol_preserves_fixture_failure_and_flushes_report(self):
        module = ModuleType("verify_fake_failure")
        class Case(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("fixture failed")
            def test_case(self):
                pass
        Case.__module__, Case.__qualname__ = module.__name__, "Case"
        module.Case = Case
        definition = manifest.ManifestLoader()
        definition.loadTestsFromModule(module, pattern="test*.py")
        expected = definition.modules[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {"module": expected.name, "ids": expected.ids, "owners": expected.owners,
                       "pattern": expected.pattern, "directory": directory, "worker": 0, "result": str(root / "result.json")}
            stderr = io.StringIO()
            with mock.patch.dict(sys.modules, {module.__name__: module}), mock.patch.dict(os.environ), mock.patch.object(
                runner, "install_guard"), mock.patch.object(runner, "process_memory", return_value=None), mock.patch.object(
                sys, "platform", "win32"), mock.patch.object(sys, "path", list(sys.path)), mock.patch.object(
                sys, "stdin", io.StringIO(json.dumps(request) + "\n")), mock.patch.object(sys, "stderr", stderr):
                self.assertEqual(runner.worker_main(), 0)
            report = json.loads((root / "result.json").read_text())
            self.assertFalse(manifest.validate_report(expected, report))
            self.assertEqual(report["completed"], [])
            self.assertIn("fixture failed", stderr.getvalue())
            self.assertEqual(summarize(root)["fixture_results"][0]["result"], "error")

    def test_failed_module_output_omits_previous_success_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = b"previous successful module output\n"
            (root / "worker-0.log").write_bytes(prefix + b"ERROR: failing_case\nsecret=private-value\n")
            worker = SimpleNamespace(number=0, log_offset=len(prefix), module=manifest.Module("failed", ()))
            with mock.patch.object(sys, "stdout", io.StringIO()) as output:
                runner.failure_output(worker, root)
            value = output.getvalue()
            self.assertIn("ERROR: failing_case", value)
            self.assertNotIn("previous successful", value)
            self.assertNotIn("private-value", value)

    def test_serial_pool_reuses_worker_and_releases_only_after_stop(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            writer = EventWriter(root / "events.jsonl")
            cleanup.callback(writer.close)
            requests, stopped = [], []
            class Client:
                def request(self, operation, **values):
                    requests.append((operation, values))
                    if operation == "release":
                        if not stopped:
                            raise AssertionError("release before child stop")
                    return {"granted": True, "token": "owned", "reason": "fake"}
            worker = SimpleNamespace(process=mock.Mock(pid=100), token="owned", number=0, module=None, report=None, job=None)
            worker.process.poll.return_value = None
            def assign(module, path, _directory):
                worker.module, worker.report = module, path
                path.write_text(json.dumps({"module": module.name, "passed": True, "completed": list(module.ids)}))
            worker.assign = assign
            spawn = mock.Mock(return_value=worker)
            with mock.patch.object(runner, "stop_worker", side_effect=lambda _worker: stopped.append(True)):
                self.assertTrue(runner.execute_manifest([manifest.Module("first", ("a",)), manifest.Module("second", ("b",))],
                                                       root, writer, Client(), spawn=spawn, sleep=lambda _seconds: None))
            spawn.assert_called_once()
            self.assertEqual([operation for operation, _values in requests].count("release"), 1)

    def test_start_failure_reclaims_only_confirmed_unstarted_reservation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            writer = EventWriter(root / "events.jsonl")
            cleanup.callback(writer.close)
            client = mock.Mock()
            client.request.return_value = {"granted": True, "token": "owned", "reason": "fake"}
            with self.assertRaisesRegex(OSError, "launch failed"):
                runner.execute_manifest([manifest.Module("first", ("a",))], root, writer, client,
                                        spawn=mock.Mock(side_effect=OSError("launch failed")))
            client.request.assert_any_call("not_started", token="owned")


class ManagerAdapterTests(unittest.TestCase):
    def test_context_cap_and_diagnostic_path_survive_bounded_failure_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = mock.Mock(endpoint=("127.0.0.1", 123))
            coordinator.open.return_value = ("session", "private")
            effects = mock.Mock()
            effects.run_verify_process.return_value = (False, False, "ERROR: failed_case\nAssertionError: failed\n")
            with execution_settings(coordinator, 2, root):
                passed, _timeout, output = manager_adapter.run(effects, ["fake"], root, 20)
            self.assertFalse(passed)
            coordinator.open.assert_called_once_with(2, require_guard=True)
            coordinator.finish.assert_called_once_with("session", stopped=True)
            coordinator.close.assert_not_called()
            environment = effects.run_verify_process.call_args.kwargs["environment"]
            self.assertEqual(environment["LOOP_MANAGER_VERIFY_PARALLELISM"], "2")
            diagnostic = format_verify_failure(str(output), exit_code=1)
            self.assertIn("FAILED: failed_case", diagnostic)
            self.assertIn("[VERIFY_DIAGNOSTIC]", diagnostic)
            self.assertLessEqual(len(diagnostic), MAX_DIAGNOSTIC)
            self.assertNotIn("private", diagnostic)
            names = [f"module.case_{number:02d}_" + "x" * 120 for number in range(20)]
            large = "\n".join(f"ERROR: {name}\nAssertionError: failed" for name in names)
            limited = format_verify_failure(large + "\n[VERIFY_DIAGNOSTIC] " + str(root / "summary.json"), exit_code=1)
            self.assertLessEqual(len(limited), MAX_DIAGNOSTIC)
            self.assertTrue(all(line[8:] in names for line in limited.splitlines() if line.startswith("FAILED: ")))
            self.assertIn("[VERIFY_DIAGNOSTIC]", limited)

    def test_uncertain_termination_keeps_reservation_and_writes_separate_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = mock.Mock(endpoint=("127.0.0.1", 123))
            coordinator.open.return_value = ("session", "private")
            coordinator.lock = __import__("threading").Lock()
            coordinator.reservations = {"owned": {"session": "session", "uncertain": True},
                                        "other": {"session": "other"}}
            effects = mock.Mock()
            effects.run_verify_process.side_effect = verify_process.VerifyTerminationUncertain("fake uncertainty")
            with execution_settings(coordinator, "auto", root), self.assertRaises(verify_process.VerifyTerminationUncertain):
                manager_adapter.run(effects, ["fake"], root, 20)
            coordinator.finish.assert_called_once_with("session", stopped=False)
            journal = json.loads((root / "logs/verify/session/uncertain-reservations.json").read_text())
            self.assertEqual(set(journal), {"owned"})
