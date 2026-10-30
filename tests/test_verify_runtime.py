"""시간·수집·결과·프로세스 경계만 작은 fake로 검증한다."""
import ctypes
from contextlib import ExitStack
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import py_compile
import runpy
import threading
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.config.managed_config import load_managed_config, ManagedConfigError
from loop_manager_internal.execution import verify_process, windows_job
from loop_manager_internal.verification import guard, manifest, runner, manager_adapter, unittest_adapter
from loop_manager_internal.verification.coordination import execution_settings, VerifyCoordinator
from loop_manager_internal.verification.resources import Snapshot, MIB
from loop_manager_internal.execution.validation_feedback import MAX_DIAGNOSTIC, format_verify_failure
from loop_manager_internal.verification.timing import (
    EventWriter, TimedResult, bytecode_cache_changes, bytecode_cache_snapshot,
    comparison, fixture_timing, phase, summarize,
)


class TimingAndManifestTests(unittest.TestCase):
    def test_summary_terminal_stage_or_run_event_dominates_worker_starts(self):
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

                summary = summarize(root, now=10)

                self.assertEqual(summary["active"], [])

    def test_summary_closes_phase_after_stage_identity_changes_without_run_end(self):
        for raises in (False, True):
            with self.subTest(raises=raises), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                writer = EventWriter(root / "runner.jsonl", identity={"run_id": "run-stage-drift"})
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

                records = [json.loads(line) for line in (root / "runner.jsonl").read_text().splitlines()]
                summary = summarize(root, now=10)

                self.assertNotEqual(records[0].get("stage_id"), records[1].get("stage_id"))
                self.assertEqual(records[0]["phase_id"], records[1]["phase_id"])
                self.assertEqual(records[1]["result"], "error" if raises else "complete")
                self.assertNotIn("run_end", [record["kind"] for record in records])
                self.assertEqual(summary["active"], [])

    def test_windows_summary_links_os_runs_without_combining_their_clocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            root.mkdir()
            clock = [100.0]
            writer = EventWriter(root / "launcher.jsonl", clock=lambda: clock[0],
                                 identity={"run_id": "parent-run"})
            with writer.activate():
                writer.emit("run_start", mode="windows+wsl")
                with phase("wsl-full"):
                    clock[0] += 10
                clock[0] += 10
                writer.emit("run_end", passed=True, wsl="PASS", windows="PASS")
            writer.close()
            (root / "snapshot.json").write_text(json.dumps({
                "run_id": "parent-run", "revision": "a" * 40,
                "bundle": "C:/Temp/loop-manager-verify-1",
                "snapshot_sha256": "b" * 64}), encoding="utf-8")
            (root / "result.json").write_text(json.dumps({
                "run_id": "parent-run", "passed": True, "wsl": "PASS", "windows": "PASS",
                "wsl_run_id": "linux-run", "windows_run_id": "parent-run"}), encoding="utf-8")
            for name, run_id, domain, elapsed in (
                    ("wsl", "linux-run", "linux", 8.0),
                    ("windows", "parent-run", "win32", 4.0)):
                child = Path(str(root) + f"-{name}")
                child.mkdir()
                (child / "summary.json").write_text(json.dumps({
                    "metadata": {"run_id": run_id, "parent_run_id": "parent-run",
                                 "clock_domain": domain, "mode": "full"},
                    "result": {"passed": True}, "elapsed_seconds": elapsed,
                    "counts": {"success": 3}, "phases": [], "assignments": [],
                    "allocations": [], "git_commands": []}), encoding="utf-8")

            summary = summarize(root, now=130.0)

            self.assertEqual(summary["elapsed_seconds"], 20.0)
            self.assertEqual(len(summary["execution_runs"]), 1)
            self.assertEqual(summary["counts"], {})
            pipeline = summary["windows_pipeline"]
            self.assertEqual(pipeline["snapshot"]["revision"], "a" * 40)
            self.assertEqual(pipeline["children"]["wsl"]["run_id"], "linux-run")
            self.assertEqual(pipeline["children"]["wsl"]["elapsed_seconds"], 8.0)
            self.assertEqual(pipeline["children"]["windows"]["clock_domain"], "win32")
            self.assertEqual(pipeline["children"]["windows"]["elapsed_seconds"], 4.0)

    def test_comparison_rejects_changed_scope_environment_and_incomplete_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps([{"ids": ["case"]}]))
            summary = {"metadata": {"revision": "first", "cap": 1, "python": "fake", "git": "fake",
                       "bytecode_policy": {"write_enabled": True, "cache_tag": "cpython-312",
                                           "cache_prefix": None, "optimization": 0},
                       "execution_context": {"python": "/project/python", "stages": ["unittest", "compile"],
                           "parallelism": 1, "coordinated": True, "instrumentation": 1,
                           "temporary_directory": "/tmp", "pythonpath": None},
                       "environment": {"host": "same", "platform": "fake", "limits": {
                           "cpus": 4, "affinity_cpus": 4, "applied_quota": None, "source": "fake"}}},
                       "phases": [{"name": "verify", "duration": 20}], "result": {"passed": True},
                       "active": [], "counts": {"success": 1}}
            other = json.loads(json.dumps(summary))
            other["metadata"].update(revision="second", cap="auto")
            other["metadata"]["execution_context"]["parallelism"] = "auto"
            other["phases"][0]["duration"] = 10
            report = comparison([root, root], [summary, other])
            self.assertTrue(report["comparable"])
            self.assertEqual([run["seconds"] for run in report["runs"]], [20, 10])
            other["metadata"]["environment"]["limits"]["cpus"] = 2
            self.assertFalse(comparison([root, root], [summary, other])["comparable"])
            other["metadata"]["environment"]["limits"]["cpus"] = 4
            other["metadata"]["execution_context"]["pythonpath"] = "/different"
            self.assertFalse(comparison([root, root], [summary, other])["comparable"])
            other["metadata"]["execution_context"]["pythonpath"] = None
            other["active"] = [{"id": "unfinished"}]
            self.assertFalse(comparison([root, root], [summary, other])["comparable"])
            other["active"] = []
            other["metadata"]["bytecode_policy"]["cache_prefix"] = "/different-cache"
            changed_policy = comparison([root, root], [summary, other])
            self.assertFalse(changed_policy["bytecode_policy_matches"])
            self.assertFalse(changed_policy["comparable"])
            changed = root / "changed"
            changed.mkdir()
            (changed / "manifest.json").write_text(json.dumps([{"ids": ["another"]}]))
            self.assertFalse(comparison([root, changed], [summary, summary])["scope_matches"])

    def test_comparison_counts_nested_fixture_time_once_and_preserves_independent_phases(self):
        clock = [0.0]

        def advance(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(json.dumps([{"ids": ["case"]}]))
            writer = EventWriter(root / "events.jsonl", clock=lambda: clock[0])
            try:
                with writer.activate():
                    with phase("git_fixture:boundary_save"):
                        advance(1)
                        with phase("git_fixture:boundary_confirmation"):
                            advance(2)
                        with phase("git_fixture:boundary_copy"):
                            advance(4)
                            with phase("git_fixture:boundary_confirmation"):
                                advance(3)
                    with phase("git_fixture:boundary_restore"):
                        advance(1)
                        with phase("git_fixture:boundary_copy"):
                            advance(3)
                        with phase("git_fixture:boundary_confirmation"):
                            advance(2)
                    with phase("git_fixture:boundary_cleanup"):
                        advance(2)
                    with phase("git_fixture:boundary_identity"):
                        advance(5)
                    # A second test's same-named phase is sequential and remains independent.
                    clock[0] = 100
                    with phase("git_fixture:boundary_copy"):
                        advance(5)
                    # Another process can have an overlapping clock interval without nesting.
                    writer.emit("phase_end", name="git_fixture:boundary_copy", pid=os.getpid() + 1,
                                start=2, duration=4, result="complete")
                    writer.emit("phase_end", name="git_fixture:legacy", duration=3, result="complete")
                    writer.emit("phase_end", name="git_fixture:unknown_process", pid=None, start=0, duration=8,
                                result="complete")
            finally:
                writer.close()
            summary = summarize(root)
            summary["phases"].append({"name": "git_fixture:missing_duration", "start": 0,
                                      "pid": os.getpid(), "result": "complete"})
            original_phases = json.loads(json.dumps(summary["phases"]))

            report = comparison([root], [summary])

        self.assertEqual(report["runs"][0]["git_fixture_seconds"], 43)
        self.assertEqual(summary["phases"], original_phases)

    def test_bytecode_cache_snapshot_identifies_created_and_unchanged_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "loop_manager.py"
            source.write_text("VALUE = 7\n", encoding="utf-8")
            before = bytecode_cache_snapshot(root)
            self.assertEqual(before, {})

            py_compile.compile(str(source), doraise=True)
            after = bytecode_cache_snapshot(root)
            created = bytecode_cache_changes(before, after)
            self.assertEqual((created["existing"], created["created"], created["unchanged"]), (0, 1, 0))

            retained = bytecode_cache_changes(after, bytecode_cache_snapshot(root))
            self.assertEqual((retained["existing"], retained["created"], retained["unchanged"]), (1, 0, 1))

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
                with self.subTest(value=0):
                    self.skipTest("fake subtest skip before failure")
                with self.subTest(value=1):
                    self.fail("fake subtest")
        with tempfile.TemporaryDirectory() as directory:
            result, summary = self.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Cases), Path(directory))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(summary["counts"], {"success": 1, "failure": 2, "error": 1, "skip": 1,
                                           "expected_failure": 1, "unexpected_success": 1})

    def test_skipped_subtest_completes_parent_without_fixture_skip(self):
        class Case(unittest.TestCase):
            def test_case(self):
                with self.subTest(kind="unsupported"):
                    self.skipTest("platform feature unavailable")
                with self.subTest(kind="supported"):
                    self.assertTrue(True)
        case = Case("test_case")
        with tempfile.TemporaryDirectory() as directory:
            result, summary = self.run_suite(unittest.TestSuite([case]), Path(directory))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.completed, [case.id()])
        self.assertEqual(summary["counts"], {"skip": 1})
        self.assertEqual(summary["fixture_results"], [])
        self.assertTrue(manifest.validate_report(
            manifest.Module(__name__, (case.id(),)),
            {"module": __name__, "passed": True, "completed": result.completed}))

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


class GuardInstallationTests(unittest.TestCase):
    def setUp(self):
        self.hooks = []
        self.state = SimpleNamespace(lock=threading.RLock(), active=threading.local(), audit=None, wrapper=None)
        patches = (
            mock.patch.object(guard, "_state", self.state),
            mock.patch.object(sys, guard._STATE_KEY, self.state),
            mock.patch.object(sys, "addaudithook", side_effect=self.hooks.append),
            mock.patch.object(sys, "audit", side_effect=self.dispatch),
            mock.patch.object(subprocess.Popen, "__init__", self.fake_start),
            mock.patch.dict(os.environ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def dispatch(self, event, *arguments):
        for hook in self.hooks:
            hook(event, arguments)

    def fake_start(self, process, args, env=None):
        self.dispatch("subprocess.Popen", args[0], args, None, env)

    def test_runpy_import_and_reentry_share_hook_and_wrapper(self):
        loaded = runpy.run_path(guard.__file__)
        loaded["install_guard"]()
        wrapper = subprocess.Popen.__init__
        guard.install_guard()
        loaded["install_guard"]()
        self.assertEqual(len(self.hooks), 1)
        self.assertIs(subprocess.Popen.__init__, wrapper)
        # An arbitrary wrapper attribute is not installation evidence.
        def replacement(process, args, env=None):
            return self.fake_start(process, args, env)
        replacement._loop_manager_environment_guard = True
        subprocess.Popen.__init__ = replacement
        guard.install_guard()
        self.assertIsNot(subprocess.Popen.__init__, replacement)
        self.assertEqual(len(self.hooks), 1)

    def test_environment_prepared_once_per_boundary_and_commands_always_checked(self):
        guard.install_guard()
        environment = {"LOOP_MANAGER_VERIFY_SECRET": "private-test-value"}
        with mock.patch.object(guard, "worker_environment", wraps=guard.worker_environment) as prepare:
            subprocess.Popen.__init__(object(), ["git", "status"], env=environment)
            self.assertEqual(prepare.call_count, 1)
            with self.assertRaises(RuntimeError):
                subprocess.Popen.__init__(object(), ["codex", "exec"], env=environment)
            self.assertEqual(prepare.call_count, 2)
            self.dispatch("subprocess.Popen", "git", ["git", "status"], None, environment.copy())
            self.assertEqual(prepare.call_count, 3)
        self.assertEqual(environment, {"LOOP_MANAGER_VERIFY_SECRET": "private-test-value"})
        self.assertIsNone(self.state.active.environment)

    def test_windows_audit_command_line_keeps_argv_and_rejects_shell_or_executable_override(self):
        def start(process, args, env=None, shell=False, executable=None):
            self.assertIsInstance(args, tuple)
            self.dispatch('subprocess.Popen', executable or ('cmd.exe' if shell else None),
                          subprocess.list2cmdline(args), None, env)
        subprocess.Popen.__init__ = start
        guard.install_guard()
        subprocess.Popen.__init__(object(), [sys.executable, '-c', 'print("codex claude")'])
        for options in ({'shell': True}, {'executable': 'codex.exe'}):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                subprocess.Popen.__init__(object(),
                    (sys.executable, '-c', 'print("codex claude")'), **options)

    def test_changed_environment_is_prepared_again_and_start_failure_clears_scope(self):
        def start(process, args, env=None):
            env["LOOP_MANAGER_VERIFY_SECRET"] = "private-test-value"
            self.dispatch("subprocess.Popen", args[0], args, None, env)
            self.assertNotIn("LOOP_MANAGER_VERIFY_SECRET", env)
            raise OSError("not started")
        subprocess.Popen.__init__ = start
        guard.install_guard()
        with mock.patch.object(guard, "worker_environment", wraps=guard.worker_environment) as prepare:
            with self.assertRaises(OSError):
                subprocess.Popen.__init__(object(), ["git"])
            self.assertEqual(prepare.call_count, 2)
        self.assertIsNone(self.state.active.environment)
        with mock.patch.object(guard, "worker_environment", side_effect=ValueError("invalid environment")):
            with self.assertRaises(ValueError):
                subprocess.Popen.__init__(object(), ["git"])
        self.assertIsNone(self.state.active.environment)

    def test_partial_install_retries_wrapper_without_duplicate_hook(self):
        with mock.patch.object(guard.inspect, "signature", side_effect=ValueError("invalid signature")):
            with self.assertRaises(ValueError):
                guard.install_guard()
        self.assertEqual(len(self.hooks), 1)
        self.assertIsNone(self.state.wrapper)
        guard.install_guard()
        self.assertEqual(len(self.hooks), 1)
        self.assertIs(subprocess.Popen.__init__, self.state.wrapper)

    def test_interrupted_confirmation_retries_probe_without_duplicate_hook(self):
        with mock.patch.object(sys, "audit", side_effect=RuntimeError("probe interrupted")):
            with self.assertRaises(RuntimeError):
                guard.install_guard()
        self.assertIsNone(self.state.wrapper)
        guard.install_guard()
        self.assertEqual(len(self.hooks), 1)
        self.assertIs(subprocess.Popen.__init__, self.state.wrapper)

    def test_rejected_hook_is_not_cached_as_installed(self):
        with mock.patch.object(sys, "addaudithook"):
            with self.assertRaisesRegex(RuntimeError, "audit hook"):
                guard.install_guard()
        self.assertIsNone(self.state.audit)
        self.assertIsNone(self.state.wrapper)
        with mock.patch.object(sys, "addaudithook", side_effect=RuntimeError("installation failed")):
            with self.assertRaises(RuntimeError):
                guard.install_guard()
        guard.install_guard()
        self.assertEqual(len(self.hooks), 1)

    def test_sitecustomize_failure_stops_instead_of_running_unprotected(self):
        for write_error in (None, OSError("stderr unavailable")):
            with self.subTest(write_error=write_error), \
                    mock.patch.dict(os.environ, {guard.MARKER: "1"}), \
                    mock.patch.object(runpy, "run_path", side_effect=ValueError("private-test-value")), \
                    mock.patch.object(os, "write", side_effect=write_error) as write, \
                    mock.patch.object(os, "_exit", side_effect=SystemExit(1)) as exit_process:
                with self.assertRaises(SystemExit) as stopped:
                    exec(compile((Path(guard.__file__).parent / "child_guard/sitecustomize.py").read_text(),
                                 "sitecustomize.py", "exec"), {"__file__": guard.__file__})
                self.assertEqual(stopped.exception.code, 1)
                exit_process.assert_called_once_with(1)
                write.assert_called_once_with(2, b"verify Python guard installation failed\n")


class GuardAndConfigurationTests(unittest.TestCase):
    def test_worker_environment_preserves_explicit_bytecode_selection(self):
        environment = {
            "PATH": "fake",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "/user/cache",
        }
        with mock.patch.object(guard.sys, "dont_write_bytecode", False), \
                mock.patch.object(guard.sys, "pycache_prefix", None):
            child = guard.worker_environment(environment)
        self.assertEqual(child["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(child["PYTHONPYCACHEPREFIX"], "/user/cache")
        self.assertEqual(environment["PYTHONPYCACHEPREFIX"], "/user/cache")

        with mock.patch.object(guard.sys, "dont_write_bytecode", True), \
                mock.patch.object(guard.sys, "pycache_prefix", "/cli/cache"):
            child = guard.worker_environment(environment)
        self.assertEqual(child["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(child["PYTHONPYCACHEPREFIX"], "/cli/cache")

        managed_default = {
            "PATH": "fake",
            "LOOP_MANAGER_RUNTIME_DEFAULT_PYCACHEPREFIX": "1",
        }
        with mock.patch.object(guard.sys, "dont_write_bytecode", False), \
                mock.patch.object(guard.sys, "pycache_prefix", "/manager/default"):
            child = guard.worker_environment(managed_default)
        self.assertNotIn("PYTHONPYCACHEPREFIX", child)
        self.assertNotIn("LOOP_MANAGER_RUNTIME_DEFAULT_PYCACHEPREFIX", child)


    def test_managed_environment_aliases_keep_candidate_and_bytecode_policy(self):
        for windows in (False, True):
            environment = {
                b'loop_manager_verify_project_root' if windows else b'LOOP_MANAGER_VERIFY_PROJECT_ROOT': b'/candidate',
                b'pythonpath' if windows else b'PYTHONPATH': os.fsencode('/dependency'),
                b'pythondontwritebytecode' if windows else b'PYTHONDONTWRITEBYTECODE': b'1',
                b'pythonpycacheprefix' if windows else b'PYTHONPYCACHEPREFIX': b'/selected/cache',
            }
            original = dict(environment)
            platform = SimpleNamespace(name='nt' if windows else 'posix', environ=os.environ,
                                       fsdecode=os.fsdecode, fsencode=os.fsencode, pathsep=os.pathsep)
            with mock.patch.object(guard, 'os', platform), \
                    mock.patch.object(sys, 'dont_write_bytecode', False), \
                    mock.patch.object(sys, 'pycache_prefix', None):
                child = guard.worker_environment(environment)
            self.assertEqual(child['PYTHONPATH'].split(os.pathsep)[1:], ['/candidate', '/dependency'])
            self.assertEqual(child['PYTHONDONTWRITEBYTECODE'], '1')
            self.assertEqual(child['PYTHONPYCACHEPREFIX'], '/selected/cache')
            self.assertEqual(environment, original)

    def test_cli_guard_allows_git_and_fake_boundary_but_rejects_real_agent_commands(self):
        for command in (["codex", "exec"], ["C:\\bin\\claude.exe", "-p"], ["bash", "-c", "codex exec prompt"]):
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                guard.check_command(command, enabled=True)
        guard.check_command(["git", "push"], enabled=True)
        guard.check_command([sys.executable, "-c", "print('codex')"], enabled=True)

    def test_audit_guard_blocks_api_and_persists_for_explicit_child_environment(self):
        hooks = []
        state = SimpleNamespace(lock=threading.RLock(), active=threading.local(), audit=None, wrapper=None)
        def dispatch(event, *arguments):
            for hook in hooks:
                hook(event, arguments)
        with mock.patch.object(guard, "_state", state), \
                mock.patch.object(subprocess.Popen, "__init__"), \
                mock.patch.object(sys, "addaudithook", side_effect=hooks.append), \
                mock.patch.object(sys, "audit", side_effect=dispatch):
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
        self.assertNotIn("LOOP_MANAGER_VERIFY_SECRET", environment)
        with self.assertRaises(RuntimeError):
            audit("subprocess.Popen", (None, 'cmd /c "codex exec"', None, {}))
        windows_environment = {}
        audit("subprocess.Popen", (None, 'git rev-parse HEAD', None, windows_environment))
        self.assertEqual(windows_environment[guard.MARKER], "1")
        self.assertIn("child_guard", windows_environment["PYTHONPATH"])

    def test_subprocess_receives_sanitized_copy_for_inherited_and_explicit_environment(self):
        guard.install_guard()
        credential_names = ("LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SECRET",
                            "LOOP_MANAGER_VERIFY_SESSION")
        script = (
            "import json, os; names = " + repr(credential_names) + "; "
            "print(json.dumps({'credentials': [name for name in names if name in os.environ], "
            "'marker': os.environ.get('LOOP_MANAGER_TEST_NO_AGENTS'), "
            "'child_guard': any(path.endswith('child_guard') for path in "
            "os.environ.get('PYTHONPATH', '').split(os.pathsep)), "
            "'ordinary': os.environ.get('VERIFY_CHILD_TEST')}))"
        )
        command = [sys.executable, "-c", script]
        with mock.patch.dict(os.environ, {name: "private-test-value" for name in credential_names}, clear=False):
            os.environ["VERIFY_CHILD_TEST"] = "inherited-value"
            parent_environment = dict(os.environ)
            inherited = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(dict(os.environ), parent_environment)
        inherited_result = json.loads(inherited.stdout)
        self.assertEqual(inherited_result["credentials"], [])
        self.assertEqual(inherited_result["marker"], "1")
        self.assertTrue(inherited_result["child_guard"])
        self.assertEqual(inherited_result["ordinary"], "inherited-value")

        explicit_environment = {name: "private-test-value" for name in credential_names}
        explicit_environment.update(PATH=os.environ.get("PATH", ""), VERIFY_CHILD_TEST="explicit-value")
        original_environment = dict(explicit_environment)
        explicit = subprocess.run(command, env=explicit_environment, capture_output=True,
                                  text=True, check=True, timeout=10)
        self.assertEqual(explicit_environment, original_environment)
        explicit_result = json.loads(explicit.stdout)
        self.assertEqual(explicit_result["credentials"], [])
        self.assertEqual(explicit_result["marker"], "1")
        self.assertTrue(explicit_result["child_guard"])
        self.assertEqual(explicit_result["ordinary"], "explicit-value")

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess environments accept bytes keys")
    def test_subprocess_drops_bytes_aliases_of_verify_credentials(self):
        guard.install_guard()
        credential_names = ("LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SECRET",
                            "LOOP_MANAGER_VERIFY_SESSION")
        script = (
            "import json, os; names = " + repr(credential_names) + "; "
            "print(json.dumps({'credentials': [name for name in names if name in os.environ], "
            "'ordinary': os.environ.get('VERIFY_CHILD_TEST')}))"
        )
        environment = {os.fsencode(name): b"private-test-value" for name in credential_names}
        environment.update(PATH=os.environ.get("PATH", ""), VERIFY_CHILD_TEST="bytes-value")
        caller_environment = dict(environment)
        result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True,
                                text=True, check=True, timeout=10)
        self.assertEqual(environment, caller_environment)
        child = json.loads(result.stdout)
        self.assertEqual(child["credentials"], [])
        self.assertEqual(child["ordinary"], "bytes-value")

    def test_windows_environment_cleanup_ignores_credential_key_case(self):
        credential_names = ("LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SECRET",
                            "LOOP_MANAGER_VERIFY_SESSION")
        environment = {
            "loop_manager_verify_endpoint": "private-test-value",
            "LOOP_MANAGER_VERIFY_SECRET": "private-test-value",
            "Loop_Manager_Verify_Session": "private-test-value",
            b"lOoP_mAnAgEr_vErIfY_sEcReT": b"private-test-value",
            "VERIFY_CHILD_TEST": "preserved-value",
        }
        original_environment = dict(environment)
        windows_os = SimpleNamespace(name="nt", environ=os.environ, fsdecode=os.fsdecode,
                                     fsencode=os.fsencode, pathsep=os.pathsep)
        with mock.patch.object(guard, "os", windows_os):
            child_environment = guard.worker_environment(environment)
        self.assertEqual(environment, original_environment)
        self.assertEqual(child_environment["VERIFY_CHILD_TEST"], "preserved-value")
        normalized_names = {name.casefold() for name in credential_names}
        self.assertFalse(any(isinstance(key, (str, bytes)) and os.fsdecode(key).casefold() in normalized_names
                             for key in child_environment))

    @unittest.skipUnless(os.name == "nt", "requires Windows environment key lookup semantics")
    def test_windows_subprocess_drops_case_variant_verify_credentials(self):
        guard.install_guard()
        credential_names = ("LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SECRET",
                            "LOOP_MANAGER_VERIFY_SESSION")
        script = (
            "import json, os; names = " + repr(credential_names) + "; "
            "print(json.dumps({'credentials': [name for name in names if os.environ.get(name) is not None], "
            "'ordinary': os.environ.get('VERIFY_CHILD_TEST')}))"
        )
        environment = dict(os.environ)
        environment.update({name.casefold(): "private-test-value" for name in credential_names})
        environment["VERIFY_CHILD_TEST"] = "preserved-value"
        original_environment = dict(environment)
        result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True,
                                text=True, check=True, timeout=10)
        self.assertEqual(environment, original_environment)
        child = json.loads(result.stdout)
        self.assertEqual(child["credentials"], [])
        self.assertEqual(child["ordinary"], "preserved-value")

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
    def exercise_pool(self, *, immediate=False, cap=3, child=False, fail_stop=False, fail_start=False,
                      complete_during_burst=False):
        cleanup = ExitStack()
        self.addCleanup(cleanup.close)
        root = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        self.pool_root = root
        clock, identities, workers, sleeps, requests, allocations = [0.0], {}, [], [], [], []
        coordinator = VerifyCoordinator(clock=lambda: clock[0], identity=identities.get,
            sampler=SimpleNamespace(sample=lambda: Snapshot(clock[0], 8, 0, 4096 * MIB, 'fake')))
        session, secret = coordinator.open(cap)
        writer = EventWriter(root / 'events.jsonl', clock=lambda: clock[0])
        cleanup.callback(writer.close)
        class Client:
            def request(self, operation, **values):
                reply = coordinator.request(session, secret, operation, **values)
                requests.append((operation, len(sleeps), values, reply))
                return reply
        def report(worker):
            worker.report.write_text(json.dumps({'module': worker.module.name,
                'passed': True, 'completed': list(worker.module.ids)}))
        def spawn(token, number, directory):
            if fail_start and number == 1:
                raise OSError('launch failed')
            process = mock.Mock(pid=100 + number)
            process.poll.return_value = None
            identities[process.pid] = number + 1
            worker = SimpleNamespace(process=process, token=token, number=number,
                module=None, report=None, job=mock.Mock() if child else None)
            if child:
                worker.job.active.return_value = 2
            def assign(module, path, _directory):
                worker.module, worker.report = module, path
                allocations.append((module.name, len(sleeps)))
                if worker.job:
                    worker.job.active.return_value = 2
                if immediate:
                    report(worker)
                if complete_during_burst and len(allocations) == 2:
                    report(workers[0])
            worker.assign = assign
            workers.append(worker)
            return worker
        def sleep(seconds):
            self.assertEqual(seconds, 0.05)
            self.assertLess(len(sleeps), 10, 'busy polling or stalled pool')
            sleeps.append(seconds)
            clock[0] += seconds
            for worker in workers:
                if worker.module is not None:
                    report(worker)
                    if worker.job:
                        worker.job.active.return_value = 1
        def stop(worker):
            if fail_stop:
                raise verify_process.VerifyTerminationUncertain('child exit unknown')
            identities.pop(worker.process.pid)
        units = [manifest.Module(str(index), (f'case{index}',)) for index in range(3)]
        with mock.patch.object(runner, 'stop_worker', side_effect=stop):
            if fail_stop or fail_start:
                with self.assertRaisesRegex((RuntimeError, OSError), '자식 정리 실패|launch failed'):
                    runner.execute_manifest(units, root, writer, Client(), spawn=spawn, sleep=sleep)
            else:
                self.assertTrue(runner.execute_manifest(units, root, writer, Client(), spawn=spawn, sleep=sleep))
        return coordinator, requests, allocations, sleeps, workers

    def test_multiple_grants_have_no_intervening_sleep_and_protect_reservations(self):
        coordinator, requests, assigned, sleeps, workers = self.exercise_pool()
        self.assertEqual(assigned, [('0', 0), ('1', 0), ('2', 0)])
        grants = [reply for operation, _, _, reply in requests if operation == 'acquire']
        self.assertEqual([reply['running'] for reply in grants], [0, 1, 2])
        self.assertEqual([reply['pending_cpu'] for reply in grants], [0, 1, 2])
        self.assertEqual([reply['pending_memory'] for reply in grants], [0, 128 * MIB, 256 * MIB])
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(len(workers), 3)
        self.assertFalse(coordinator.reservations)

    def test_cap_rejection_waits_then_reuses_confirmed_idle_worker(self):
        coordinator, requests, assigned, sleeps, workers = self.exercise_pool(cap=2)
        self.assertEqual(assigned, [('0', 0), ('1', 0), ('2', 1)])
        replies = [reply for operation, _, _, reply in requests if operation == 'acquire']
        self.assertEqual([reply['granted'] for reply in replies], [True, True, False, True])
        self.assertEqual(len(workers), 2)
        self.assertFalse(coordinator.reservations)

    def test_immediate_results_have_no_sleep_and_do_not_create_extra_workers(self):
        coordinator, _, assigned, sleeps, workers = self.exercise_pool(immediate=True, cap=1)
        self.assertEqual(assigned, [('0', 0), ('1', 0), ('2', 0)])
        self.assertEqual(sleeps, [])
        self.assertEqual(len(workers), 1)
        self.assertFalse(coordinator.reservations)

    def test_result_ready_during_burst_is_collected_without_sleep_after_cap_rejection(self):
        coordinator, _, assigned, sleeps, _ = self.exercise_pool(cap=2, complete_during_burst=True)
        self.assertEqual(assigned, [('0', 0), ('1', 0), ('2', 0)])
        self.assertEqual(len(sleeps), 1)
        self.assertFalse(coordinator.reservations)

    def test_other_worker_result_is_collected_before_the_next_grant(self):
        coordinator, requests, assigned, sleeps, workers = self.exercise_pool(
            complete_during_burst=True)
        operations = [operation for operation, _, _, _ in requests]
        self.assertLess(operations.index('idle'), operations.index('acquire', 3))
        self.assertEqual(assigned, [('0', 0), ('1', 0), ('2', 0)])
        self.assertEqual(len(workers), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertFalse(coordinator.reservations)

    def test_report_with_live_descendant_waits_before_idle_or_reuse(self):
        _, requests, assigned, sleeps, _ = self.exercise_pool(immediate=True, cap=1, child=True)
        self.assertTrue(sleeps)
        self.assertEqual([at for operation, at, _, _ in requests if operation == 'idle'], [1, 2, 3])
        self.assertEqual(assigned, [('0', 0), ('1', 1), ('2', 2)])
        events = [json.loads(line) for line in (self.pool_root / "events.jsonl").read_text().splitlines()]
        for unit_id in ("0", "1", "2"):
            observed = next(i for i, event in enumerate(events)
                            if event["kind"] == "result_file_observed" and event["unit_id"] == unit_id)
            waiting = next(i for i, event in enumerate(events)
                           if event["kind"] == "job_child_wait" and event["unit_id"] == unit_id)
            confirmed = next(i for i, event in enumerate(events)
                             if event["kind"] == "job_child_exit_confirmed" and event["unit_id"] == unit_id)
            completed = next(i for i, event in enumerate(events)
                             if event["kind"] == "module_result" and event["unit_id"] == unit_id)
            self.assertLess(observed, waiting)
            self.assertLess(waiting, confirmed)
            self.assertLess(confirmed, completed)
        assignments = [event for event in events if event["kind"] == "unit_start"]
        self.assertEqual([event["previous_unit_id"] for event in assignments], [None, "0", "1"])
        self.assertTrue(all(event["seconds_since_previous_result"] is not None
                            for event in assignments[1:]))

    def test_continuous_start_failure_cleans_previous_worker_and_only_unstarted_token(self):
        coordinator, requests, _, _, _ = self.exercise_pool(fail_start=True)
        self.assertEqual([operation for operation, _, _, _ in requests],
            ['acquire', 'bind', 'acquire', 'not_started', 'release'])
        self.assertFalse(coordinator.reservations)

    def test_unknown_worker_termination_keeps_every_reservation(self):
        coordinator, requests, _, _, _ = self.exercise_pool(fail_stop=True)
        self.assertEqual(len(coordinator.reservations), 3)
        self.assertFalse(any(operation == 'release' for operation, _, _, _ in requests))

    def test_unchanged_rejections_keep_periodic_snapshot_and_reservation_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            clock = [0.0]
            writer = EventWriter(root / "events.jsonl", clock=lambda: clock[0])
            cleanup.callback(writer.close)
            attempts = iter(enumerate((0.0, 0.4, 1.1, 1.2)))
            class Client:
                def request(self, operation, **values):
                    if operation != "acquire":
                        return {}
                    index, clock[0] = next(attempts)
                    return {"granted": index == 3, "token": "owned", "reason": "same pressure",
                            "snapshot_age": 0, "pending_cpu": index, "running": 0,
                            "snapshot": {"busy": 1.5 + index / 10}}
            worker = SimpleNamespace(process=mock.Mock(pid=100), token="owned", number=0,
                                     module=None, report=None, job=None)
            worker.process.poll.return_value = None
            def assign(module, path, _directory):
                worker.module, worker.report = module, path
                path.write_text(json.dumps({"module": module.name, "passed": True, "completed": list(module.ids)}))
            worker.assign = assign
            with mock.patch.object(runner, "stop_worker"):
                self.assertTrue(runner.execute_manifest([manifest.Module("first", ("a",))], root, writer,
                    Client(), spawn=lambda *_args: worker, sleep=lambda _seconds: None))
            allocations = summarize(root)["allocations"]
            self.assertEqual([event["at"] for event in allocations], [0.0, 1.1, 1.2])
            self.assertEqual([event["decision"]["pending_cpu"] for event in allocations], [0, 2, 3])

    def test_compile_error_is_not_converted_to_success_or_resource_fallback(self):
        with mock.patch.object(runner.py_compile, "compile", side_effect=runner.py_compile.PyCompileError(
            SyntaxError, SyntaxError("fake syntax failure"), "loop_manager.py")):
            with self.assertRaises(runner.py_compile.PyCompileError):
                runner.compile_project(Path.cwd())

    def test_linux_bootstrap_preserves_worker_input_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            process = mock.Mock()
            with mock.patch.object(sys, "platform", "linux"), mock.patch("loop_manager_internal.verification.core.os", SimpleNamespace(name="posix", environ=os.environ)), mock.patch.object(
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
                unittest_adapter, "install_guard"), mock.patch.object(unittest_adapter, "process_memory", return_value=None), mock.patch.object(
                sys, "platform", "win32"), mock.patch.object(sys, "path", list(sys.path)), mock.patch.object(
                sys, "stdin", io.StringIO(json.dumps(request) + "\n")), mock.patch.object(sys, "stderr", stderr):
                self.assertEqual(runner.worker_main(), 0)
            report = json.loads((root / "result.json").read_text())
            self.assertFalse(manifest.validate_report(expected, report))
            self.assertEqual(report["completed"], [])
            self.assertIn("fixture failed", stderr.getvalue())
            self.assertEqual(summarize(root)["fixture_results"][0]["result"], "error")

    def test_reused_worker_events_update_module_identity_each_assignment(self):
        modules = {}
        requests = []
        for name in ("verify_reuse_first", "verify_reuse_second"):
            module = ModuleType(name)
            class Case(unittest.TestCase):
                def test_case(self):
                    pass
            Case.__module__ = name
            module.Case = Case
            modules[name] = module
            ids = list(manifest.test_ids(unittest.TestLoader().loadTestsFromModule(module)))
            requests.append({"module": name, "ids": ids, "owners": [],
                "pattern": "test*.py", "worker": 0, "worker_id": 0, "directory": None,
                "result": None, "run_id": "run-current", "parent_run_id": "parent-run",
                "stage_id": "unittest", "unit_id": name})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for request in requests:
                request["directory"] = directory
                request["result"] = str(root / f"{request['module']}.json")
            stdin = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
            stderr = io.StringIO()
            with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ), \
                 mock.patch.object(unittest_adapter, "install_guard"), \
                 mock.patch.object(unittest_adapter, "process_memory", return_value=None), \
                 mock.patch.object(sys, "platform", "win32"), mock.patch.object(sys, "path", list(sys.path)), \
                 mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stderr", stderr):
                self.assertEqual(unittest_adapter.worker_main(), 0)

            reports = [json.loads((root / f"{name}.json").read_text())
                       for name in ("verify_reuse_first", "verify_reuse_second")]
            self.assertTrue(all(report.get("passed") for report in reports),
                            {"reports": reports, "stderr": stderr.getvalue()})
            events = [json.loads(line) for line in (root / "worker-0.jsonl").read_text().splitlines()]
            starts = [event for event in events if event["kind"] == "test_start"]
            self.assertEqual([event["unit_id"] for event in starts],
                             ["verify_reuse_first", "verify_reuse_second"])
            self.assertTrue(all(event["run_id"] == "run-current" for event in starts))
            self.assertTrue(all(event["parent_run_id"] == "parent-run" for event in starts))
            self.assertTrue(all(event["worker_id"] == 0 for event in starts))

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

    def test_pool_failure_paths_stop_before_release_and_preserve_original_error(self):
        for failure in ('pipe', 'json', 'exit_before_report', 'exit_after_report', 'cancel'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
                root = Path(directory)
                writer = EventWriter(root / 'events.jsonl')
                cleanup.callback(writer.close)
                operations = []
                class Client:
                    def request(self, operation, **values):
                        operations.append(operation)
                        return {'granted': True, 'token': 'owned', 'reason': 'fake'}
                process = mock.Mock(pid=100, stdin=io.StringIO())
                process.poll.return_value = 1 if failure.startswith('exit_') else None
                worker = SimpleNamespace(process=process, token='owned', number=0,
                    module=None, report=None, job=None)
                def assign(unit, report, _directory):
                    worker.module, worker.report = unit, report
                    if failure == 'pipe':
                        raise BrokenPipeError('pipe')
                    if failure == 'cancel':
                        raise KeyboardInterrupt()
                    if failure in ('json', 'exit_after_report'):
                        report.write_text('invalid json')
                worker.assign = assign
                def stop(_worker):
                    operations.append('stop')
                expected = {'pipe': BrokenPipeError, 'json': json.JSONDecodeError,
                    'exit_before_report': RuntimeError, 'exit_after_report': RuntimeError,
                    'cancel': KeyboardInterrupt}[failure]
                with mock.patch.object(runner, 'stop_worker', side_effect=stop):
                    with self.assertRaises(expected):
                        runner.execute_manifest([manifest.Module('unit', ('test',))], root, writer,
                            Client(), spawn=lambda *_args: worker, sleep=lambda _: None)
                self.assertEqual(operations, ['acquire', 'bind', 'stop', 'release'])

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
            sleep = mock.Mock()
            with mock.patch.object(runner, "stop_worker", side_effect=lambda _worker: stopped.append(True)):
                self.assertTrue(runner.execute_manifest([manifest.Module("first", ("a",)), manifest.Module("second", ("b",))],
                                                       root, writer, Client(), spawn=spawn, sleep=sleep))
            spawn.assert_called_once()
            sleep.assert_not_called()
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


class ExecutionQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.current, self.history = self.root / 'current', self.root / 'history'
        self.current.mkdir()
        self.history.mkdir()
        (self.history / 'stage-0').mkdir()
        self.modules = [manifest.Module(name, (f'{name}.Case.a',),
            ((f'{name}.Case.a', name, f'{name}.Case'),), 'test*.py') for name in ('short', 'long', 'tie')]
        self.manifest = {'version': 1, 'adapter': 'unittest', 'adapter_version': 1, 'stages': [
            {'id': 'tests', 'kind': 'tests', 'python': 'project-python', 'units': [
                {'id': unit.name, 'ids': list(unit.ids), 'payload': json.loads(json.dumps(asdict(unit))),
                 'execution': 'independent', 'argv': ['python', 'worker'], 'cwd': '/project'}
                for unit in self.modules]}]}
        self.metadata = {'run_id': 'current', 'python': 'runtime-python', 'git': 'git', 'mode': 'full',
            'bytecode_policy': {'write_enabled': True},
            'execution_context': {'python': '/python', 'stages': ['tests', 'compile'], 'parallelism': 3},
            'environment': {'host': 'host', 'platform': 'linux', 'limits': {
                'cpus': 4, 'affinity_cpus': 4, 'source': 'fake'}}}
        self.previous = {'metadata': {**self.metadata, 'run_id': 'history'}, 'phases': [
            {'name': f'module:{unit.name}', 'duration': duration, 'result': 'complete',
             'stage_id': 'tests', 'unit_id': unit.name, 'run_id': 'history'}
            for unit, duration in zip(self.modules, (0, 5, 5))],
            'result': {'passed': True}, 'active': [], 'counts': {'success': 3}}
        self.prior_manifest = {**json.loads(json.dumps(self.manifest)), 'run_id': 'history'}
        # Completed history may include later mandatory commands; their declaration
        # remains part of execution_context while comparison projects this stage.
        self.prior_manifest['stages'].append({'id': 'compile', 'kind': 'command', 'argv': ['compile']})
        for index, unit in enumerate(self.modules):
            (self.history / f'stage-0/module-{index}.json').write_text(json.dumps({
                'version': 1, 'module': unit.name, 'unit_id': unit.name, 'stage_id': 'tests',
                'run_id': 'history', 'passed': True, 'completed': list(unit.ids)}))
        self.save_history()

    def save_history(self):
        (self.history / 'manifest.json').write_text(json.dumps(self.prior_manifest))
        (self.history / 'summary.json').write_text(json.dumps(self.previous))

    def queue(self, modules=None):
        return runner.execution_queue(self.modules if modules is None else modules, self.current,
            self.metadata, self.manifest, 'tests')

    def test_longest_first_with_stable_ties_preserves_manifest_and_fixture_identity(self):
        # Runner holds dataclass tuples in memory; history is decoded JSON lists.
        for unit, payload in zip(self.manifest['stages'][0]['units'], self.modules):
            unit['payload'] = asdict(payload)
        before = json.loads(json.dumps(self.manifest))
        self.assertEqual(self.queue(), [self.modules[1], self.modules[2], self.modules[0]])
        self.assertEqual(json.loads(json.dumps(self.manifest)), before)
        self.assertEqual(self.queue()[0].owners, self.modules[1].owners)
        self.assertEqual(self.queue()[0].pattern, 'test*.py')

    def test_missing_corrupt_or_incomplete_duration_keeps_the_entire_collection_order(self):
        saved = json.loads(json.dumps(self.previous))
        for alter in (
            lambda s: s['phases'].pop(),
            lambda s: s['phases'].append(dict(s['phases'][0])),
            lambda s: s['phases'][0].pop('duration'),
            lambda s: s['phases'][0].update(duration=None),
            lambda s: s['phases'][0].update(duration=True),
            lambda s: s['phases'][0].update(duration=-1),
            lambda s: s['phases'][0].update(duration=float('nan')),
            lambda s: s['phases'][0].update(duration=float('inf')),
            lambda s: s['phases'][0].update(result='error'),
            lambda s: s['phases'][0].update(run_id='foreign'),
            lambda s: s['active'].append({'id': 'running'}),
            lambda s: s['result'].update(passed=False),
            lambda s: s.update(metadata=[]),
            lambda s: s.update(phases=[None]),
        ):
            self.previous = json.loads(json.dumps(saved))
            alter(self.previous)
            self.save_history()
            with self.subTest(history=self.previous):
                self.assertEqual(self.queue(), self.modules)
        (self.history / 'summary.json').write_text('{corrupt')
        self.assertEqual(self.queue(), self.modules)
        (self.history / 'summary.json').unlink()
        self.assertEqual(self.queue(), self.modules)

    def test_changed_environment_contract_owners_or_serial_stage_keeps_collection_order(self):
        saved = json.loads(json.dumps(self.previous))
        for alter in (
            lambda m: m['environment'].update(host='other'),
            lambda m: m['environment']['limits'].update(cpus=2),
            lambda m: m['bytecode_policy'].update(write_enabled=False),
            lambda m: m['execution_context'].update(stages=['tests', 'other-command']),
            lambda m: m.pop('execution_context'),
            lambda m: m['execution_context'].update(parallelism=1),
        ):
            self.previous = json.loads(json.dumps(saved))
            alter(self.previous['metadata'])
            self.save_history()
            self.assertEqual(self.queue(), self.modules)
        self.previous = saved
        self.prior_manifest['run_id'] = 'foreign'
        self.save_history()
        self.assertEqual(self.queue(), self.modules)
        self.prior_manifest['run_id'] = 'history'
        self.prior_manifest['stages'][0]['units'][0]['payload']['owners'] = []
        self.save_history()
        self.assertEqual(self.queue(), self.modules)
        self.manifest['stages'][0]['units'][0]['execution'] = 'serial'
        self.assertEqual(self.queue(), self.modules)

    def test_missing_duplicate_partial_reports_and_unknown_reservations_reject_history(self):
        path = self.history / 'stage-0/module-0.json'
        report = json.loads(path.read_text())
        for change in ({'completed': []}, {'completed': [report['completed'][0]] * 2},
                       {'unit_id': 'foreign'}, {'version': True}):
            path.write_text(json.dumps({**report, **change}))
            self.assertEqual(self.queue(), self.modules)
        path.write_text(json.dumps(report))
        duplicate = path.with_name('module-3.json')
        duplicate.write_text(json.dumps(report))
        self.assertEqual(self.queue(), self.modules)
        duplicate.unlink()
        uncertain = self.history / 'uncertain-reservations.json'
        uncertain.write_text('{"owned": {}}')
        self.assertEqual(self.queue(), self.modules)
        uncertain.unlink()
        path.unlink()
        self.assertEqual(self.queue(), self.modules)

    def test_partial_full_and_different_selections_are_not_comparable(self):
        self.metadata['mode'] = 'partial'
        self.assertEqual(self.queue(), self.modules)
        self.previous['metadata']['mode'] = 'partial'
        for units in (self.manifest['stages'][0]['units'], self.prior_manifest['stages'][0]['units']):
            for index, unit in enumerate(units):
                unit['status'] = 'NOT_SELECTED' if index == 2 else 'SELECTED'
        (self.history / 'stage-0/module-2.json').unlink()
        self.previous['phases'].pop()
        self.previous['counts'] = {'success': 2}
        self.save_history()
        chosen = self.modules[:2]
        self.assertEqual(self.queue(chosen), list(reversed(chosen)))
        self.assertEqual(self.manifest['stages'][0]['units'][2]['status'], 'NOT_SELECTED')
        self.prior_manifest['stages'][0]['units'][1]['status'] = 'NOT_SELECTED'
        self.save_history()
        self.assertEqual(self.queue(chosen), chosen)
        self.assertEqual(self.queue([]), [])


class ManagerAdapterTests(unittest.TestCase):
    def test_progress_reads_stage_worker_events_once_and_tracks_interruption(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            stage = root / 'stage-0'
            stage.mkdir()
            writer = EventWriter(stage / 'worker-1.jsonl',
                                 identity={'run_id': 'run-current', 'stage_id': 'unittest',
                                           'unit_id': 'module.test_verify_runtime'})
            cleanup.callback(writer.close)
            writer.emit('test_start', id='test_done')
            writer.emit('test_end', id='test_done')
            writer.emit('test_start', id='test_running')
            positions, state = {}, {}

            observed = manager_adapter.progress_observation(root, positions, state, 'run-current')

            self.assertEqual(observed['observed_completed_tests'], 1)
            self.assertEqual(observed['active_tests'], 1)
            self.assertEqual(observed['current_test'], 'test_running')
            self.assertEqual(observed['current_stage'], 'unittest')
            self.assertEqual(observed['current_unit'], 'module.test_verify_runtime')
            self.assertEqual(manager_adapter.progress_observation(root, positions, state, 'run-current'), observed)
            writer.emit('test_interrupted', id='test_running')
            observed = manager_adapter.progress_observation(root, positions, state, 'run-current')
            self.assertEqual(observed['observed_completed_tests'], 1)
            self.assertEqual(observed['active_tests'], 0)

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
            self.assertEqual(environment["LOOP_MANAGER_VERIFY_VERSION"], "1")
            self.assertEqual(environment["LOOP_MANAGER_VERIFY_RUNTIME_PYTHON"], sys.executable)
            self.assertEqual(environment["LOOP_MANAGER_VERIFY_PROJECT_ROOT"], str(root.resolve()))
            self.assertTrue(Path(environment["LOOP_MANAGER_VERIFY_RUNNER"]).is_absolute())
            self.assertEqual(environment["LOOP_MANAGER_VERIFY_TIMEOUT"], "20")
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
