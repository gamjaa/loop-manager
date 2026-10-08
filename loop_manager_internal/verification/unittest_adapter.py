"""unittest 수집, fixture, reporter와 프로젝트 interpreter 경계."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import importlib
import json
import os
from pathlib import Path
import py_compile
import sys
import traceback
import unittest

from loop_manager_internal.verification.guard import MARKER, install_guard, worker_environment
from loop_manager_internal.verification.git_timing import git_command_timing
from loop_manager_internal.verification.manifest import Module, discover, load_module, test_cases, validate_report
from loop_manager_internal.verification.resources import process_memory
from loop_manager_internal.verification.core import stop_worker, failure_output
from loop_manager_internal.verification import timing as instrumentation
from loop_manager_internal.verification.timing import EventWriter, TimedResult, atomic_json, fixture_timing, phase


def project_imports():
    """Test imports must resolve the candidate, never the Manager's runtime tree."""
    root = Path.cwd().resolve()
    runtime = Path(__file__).resolve().parents[2]
    if runtime != root:
        for name in list(sys.modules):
            if name == "loop_manager" or name == "loop_manager_internal" or name.startswith("loop_manager_internal."):
                del sys.modules[name]
        sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != runtime]
    sys.path.insert(0, str(root))
    if (root / "loop_manager_internal/verification/timing.py").is_file():
        # Bind before discovery/load_tests can import helpers with copied ContextVars.
        importlib.import_module("loop_manager_internal.verification.timing")
        bind_instrumentation()


def bind_instrumentation():
    # Candidate helpers use the same context as the runtime's unittest hooks.
    candidate = sys.modules.get("loop_manager_internal.verification.timing")
    if candidate is not None and candidate is not instrumentation:
        for name in ("_current_writer", "_fixture_timing_active", "_phase_names"):
            setattr(candidate, name, getattr(instrumentation, name))


def worker_main():
    os.environ[MARKER] = "1"
    install_guard()
    if sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "worker PR_SET_CHILD_SUBREAPER")
    sys.path.insert(0, os.environ.get("LOOP_MANAGER_VERIFY_TEST_DIRECTORY", str(Path.cwd() / "tests")))
    project_imports()
    writer = None
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if writer is None:
                writer = EventWriter(Path(request["directory"]) / f"worker-{request['worker']}.jsonl", identity={key: request[key] for key in ("run_id", "stage_id") if key in request})
            writer.identity.update({key: request[key] for key in ("run_id", "stage_id", "unit_id") if key in request})
            module = Module(request["module"], tuple(request["ids"]), tuple(tuple(owner) for owner in request["owners"]), request["pattern"])
            try:
                with writer.activate(), phase(f"import:{module.name}"):
                    suite = load_module(module)
                    owners = {test.id(): (test.__class__.__module__,
                              f"{test.__class__.__module__}.{test.__class__.__qualname__}") for test in test_cases(suite)}
                with writer.activate(), phase(f"module:{module.name}"), fixture_timing(), git_command_timing():
                    result = unittest.TextTestRunner(
                        resultclass=lambda *args, **kwargs: TimedResult(*args, writer=writer, **kwargs),
                        stream=sys.stderr,
                    ).run(suite)
                if sys.platform.startswith("linux"):
                    with writer.activate(), phase(f"descendant_cleanup:{module.name}"):
                        while True:
                            try:
                                os.waitpid(-1, 0)
                            except ChildProcessError:
                                break
                report = {"module": module.name, "passed": result.wasSuccessful(),
                          "completed": result.completed, "memory": process_memory()}
                fixture_skips = {test.id() for test, _reason in result.skipped if getattr(test, "test_case", test).id() not in result.completed}
                report["fixture_skip_sources"] = sorted(fixture_skips)
                report["fixture_skipped"] = [identity for identity, (owner, klass) in owners.items()
                    if identity not in result.completed and
                    (f"setUpModule ({owner})" in fixture_skips or f"setUpClass ({klass})" in fixture_skips)]
            except Exception:
                traceback.print_exc()
                report = {"module": module.name, "passed": False, "completed": [], "runner_error": True}
            if "run_id" in request:
                report.update(version=1, run_id=request["run_id"], unit_id=request["unit_id"], stage_id=request["stage_id"])
            sys.stdout.flush()
            sys.stderr.flush()
            atomic_json(Path(request["result"]), report)
    finally:
        if writer:
            writer.close()
    return 0


@dataclass
class Worker:
    process: object
    token: str
    number: int
    output: object
    job: object = None
    module: Module | None = None
    report: Path | None = None
    log_offset: int = 0
    identity: dict | None = None

    def assign(self, module, report, directory):
        self.output.flush()
        self.log_offset = self.output.tell()
        self.module, self.report = module, report
        self.process.stdin.write(json.dumps({"module": module.name, "ids": module.ids, "owners": module.owners, "pattern": module.pattern,
                                 "result": str(report), "directory": str(directory), "worker": self.number, **(self.identity or {}), **({"unit_id": module.name} if self.identity else {})}) + "\n")
        self.process.stdin.flush()


def start_worker(token, number, directory, *, command=None, cwd=None, environment=None, identity=None):
    from loop_manager_internal.verification.core import start_process
    command = command or [sys.executable, "-u", "-m", "loop_manager_internal.verification.runner", "--worker"]
    process, output, job = start_process(command, number, directory, cwd=cwd,
        environment=worker_environment(os.environ if environment is None else environment))
    return Worker(process, token, number, output, job, identity=identity)


def compile_project(root):
    for path in [root / "loop_manager.py", *sorted((root / "loop_manager_internal").rglob("*.py"))]:
        py_compile.compile(str(path), doraise=True)



def collect(request):
    """Run only under the project's interpreter and dependency environment."""
    if not (3, 11) <= sys.version_info[:2] <= (3, 14):
        raise RuntimeError("unittest adapter requires Python 3.11–3.14")
    os.environ[MARKER] = "1"
    install_guard()
    directory = Path(request["collection"]["directory"])
    project_imports()
    modules = discover(directory, pattern=request["collection"]["pattern"])
    atomic_json(Path(request["result"]), {"version": 1, "run_id": request["run_id"],
        "stage_id": request["stage_id"], "python": sys.version,
        "modules": [asdict(module) for module in modules]})


def collect_project(connection, stage, directory, run_id, entrypoint, timeout, execute_command, writer, client):
    request = {"version": 1, "run_id": run_id, "stage_id": stage["id"],
        "collection": {**stage["collection"], "directory": str(connection.root / stage["collection"]["directory"])},
        "result": str(directory / "collection.json")}
    path = directory / "collection-request.json"
    atomic_json(path, request)
    passed = execute_command([str(connection.python), "-u", str(entrypoint), "--collect", str(path)],
        connection.root / stage["cwd"], timeout, writer, client, run_id, stage["id"] + ":collect")
    if not passed:
        raise RuntimeError("unittest project collection failed")
    report = json.loads(Path(request["result"]).read_text(encoding="utf-8"))
    if type(report.get("version")) is not int or report.get("version") != 1 or report.get("run_id") != run_id or report.get("stage_id") != stage["id"]:
        raise RuntimeError("unittest collection identity mismatch")
    modules = [Module(item["name"], tuple(item["ids"]), tuple(tuple(owner) for owner in item["owners"]), item["pattern"])
               for item in report["modules"]]
    from loop_manager_internal.verification.contract import validate_manifest
    validate_manifest(modules)
    return modules, report["python"]


def execute(connection, stage, modules, directory, writer, client, run_id, entrypoint):
    from loop_manager_internal.verification.core import execute_manifest
    environment = dict(os.environ, LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(connection.root), LOOP_MANAGER_VERIFY_TEST_DIRECTORY=str(connection.root / stage["collection"]["directory"]))
    identity = {"version": 1, "run_id": run_id, "stage_id": stage["id"]}
    def validate(unit, report):
        if type(report.get("version")) is not int or any(report.get(key) != value for key, value in {**identity, "unit_id": unit.name}.items()):
            raise RuntimeError("unittest result identity/version mismatch")
        return validate_report(unit, report)
    def spawn(token, number, logs):
        return start_worker(token, number, logs,
            command=[str(connection.python), "-u", str(entrypoint), "--worker"],
            cwd=connection.root / stage["cwd"], environment=environment, identity=identity)
    if stage["execution"] == "serial":
        # Keep one reusable process, so module fixtures and dependency order remain intact.
        parent = client
        class SerialClient:
            def request(self, operation, **data):
                if operation == "acquire" and "token" not in data and active[0]:
                    return {"granted": False, "reason": "serial stage"}
                reply = parent.request(operation, **data)
                if operation == "acquire" and reply["granted"]:
                    active[0] = True
                return reply
        active = [False]
        client = SerialClient()
    return execute_manifest(modules, directory, writer, client, spawn=spawn, validate_report=validate,
        stop_worker=stop_worker, failure_output=failure_output,
        coverage=lambda unit, report: report["completed"] + report.get("fixture_skipped", []))


def compile_paths(root, targets):
    from loop_manager_internal.verification.contract import inside
    for target in targets:
        path = inside(root, str(target))
        if not path.exists():
            raise RuntimeError("compile input is missing")
        for source in ([path] if path.is_file() else sorted(path.rglob("*.py"))):
            inside(root, str(source.relative_to(root)))
            py_compile.compile(str(source), doraise=True)
