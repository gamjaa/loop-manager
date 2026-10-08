"""같은 manifest를 직렬/부하 적응형 worker 프로세스에서 실행한다."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import os
import platform
from pathlib import Path
import py_compile
import subprocess
import sys
import time
import traceback
import uuid

from loop_manager_internal.verification.coordination import CoordinatorClient, VerifyCoordinator
from loop_manager_internal.verification.guard import MARKER, install_guard
from loop_manager_internal.verification.unittest_adapter import (worker_main, start_worker, stop_worker, failure_output, compile_project)
from loop_manager_internal.verification.manifest import validate_report
from loop_manager_internal.verification.contract import VERSION, ADAPTERS, ContractError, load
from loop_manager_internal.verification import unittest_adapter
from loop_manager_internal.verification.core import execute_command
from loop_manager_internal.verification.resources import DEFAULT_WORKER_BYTES, ResourceSampler, parse_parallelism, process_memory
from loop_manager_internal.verification.timing import (
    EventWriter, TimedResult, atomic_json, bytecode_cache_changes,
    bytecode_cache_snapshot, bytecode_policy, comparison, diagnostic,
    fixture_timing, phase, summarize,
)


IMPLEMENTATIONS = {"unittest": unittest_adapter}

def execute_manifest(modules, directory, writer, client, *, spawn=None, sleep=time.sleep):
    from loop_manager_internal.verification.core import execute_manifest as execute
    return execute(modules, directory, writer, client, spawn=spawn or start_worker,
                   validate_report=validate_report, stop_worker=stop_worker,
                   failure_output=failure_output, coverage=lambda unit, report: report["completed"] + report.get("fixture_skipped", []), sleep=sleep)


def run(directory, cap, timeout=600, project_python=None):
    connection = load(Path.cwd(), project_python=project_python or sys.executable)
    if connection is None:
        raise ContractError("scripts/verify.json is required for the common runner")
    adapter = IMPLEMENTATIONS[connection.adapter]
    run_id = os.environ.get("LOOP_MANAGER_VERIFY_SESSION", uuid.uuid4().hex)
    entrypoint = Path(__file__).with_name("entrypoint.py").resolve()
    started_at = time.monotonic()
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["LOOP_MANAGER_VERIFY_PROJECT_ROOT"] = str(connection.root)
    os.environ[MARKER] = "1"
    install_guard()
    writer = EventWriter(directory / "runner.jsonl", identity={"run_id": run_id})
    coordinator = None
    session = None
    try:
        if "LOOP_MANAGER_VERIFY_ENDPOINT" in os.environ:
            client = CoordinatorClient(json.loads(os.environ["LOOP_MANAGER_VERIFY_ENDPOINT"]),
                                       os.environ["LOOP_MANAGER_VERIFY_SESSION"], os.environ["LOOP_MANAGER_VERIFY_SECRET"])
            coordinated = True
        else:
            coordinator = VerifyCoordinator()
            session, secret = coordinator.open(cap)
            class LocalClient:
                def request(self, operation, **data):
                    return coordinator.request(session, secret, operation, **data)
            client, coordinated = LocalClient(), False
        def git_value(*arguments):
            return subprocess.run(["git", *arguments], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        writer.emit("run_start", version=VERSION, run_id=run_id, adapter=connection.adapter, adapter_support=ADAPTERS[connection.adapter], revision=git_value("rev-parse", "HEAD"), git=git_value("--version"),
                    python=sys.version, command=sys.argv, cap=cap, coordinated=coordinated,
                    bytecode_policy=bytecode_policy(),
                    environment={"host": platform.node(), "platform": sys.platform,
                                 "limits": asdict(ResourceSampler().sample())},
                    estimate={"worker_bytes": DEFAULT_WORKER_BYTES, "measured": False})
        with writer.activate(), phase("verify"):
            with phase("bytecode_cache_snapshot_before", writer=writer):
                bytecode_before = bytecode_cache_snapshot(Path.cwd())
            passed = True
            manifest_stages = []
            for index, stage in enumerate(connection.stages):
                remaining = timeout - (time.monotonic() - started_at)
                if remaining <= 0:
                    raise TimeoutError("verify total timeout")
                stage_directory = directory / f"stage-{index}"
                stage_directory.mkdir()
                writer.identity["stage_id"] = stage["id"]
                with phase(f"stage:{stage['id']}"):
                    if stage["kind"] == "tests":
                        modules, project_python = adapter.collect_project(connection, stage, stage_directory,
                            run_id, entrypoint, remaining, execute_command, writer, client)
                        units = [{"id": module.name, "ids": list(module.ids),
                            "argv": [str(connection.python), "-u", str(entrypoint), "--worker"],
                            "cwd": str(connection.root / stage["cwd"]), "execution": stage["execution"],
                            "payload": asdict(module)} for module in modules]
                        manifest_stages.append({"id": stage["id"], "kind": "tests", "python": project_python, "units": units})
                        atomic_json(directory / "manifest.json", {"version": VERSION, "run_id": run_id,
                            "adapter": connection.adapter, "adapter_version": ADAPTERS[connection.adapter]["version"], "stages": manifest_stages})
                        passed = adapter.execute(connection, stage, modules, stage_directory, writer, client, run_id, entrypoint)
                    else:
                        argv = [{"{python}": str(connection.python), "{runner}": str(entrypoint)}.get(item, item)
                                for item in stage["argv"]]
                        manifest_stages.append({"id": stage["id"], "kind": "command", "argv": argv, "cwd": stage["cwd"]})
                        atomic_json(directory / "manifest.json", {"version": VERSION, "run_id": run_id,
                            "adapter": connection.adapter, "adapter_version": ADAPTERS[connection.adapter]["version"], "stages": manifest_stages})
                        passed = execute_command(argv, connection.root / stage["cwd"], remaining,
                                                 writer, client, run_id, stage["id"])
                if not passed:
                    break
            with phase("bytecode_cache_snapshot_after", writer=writer):
                bytecode_after = bytecode_cache_snapshot(Path.cwd())
            writer.emit("run_end", version=VERSION, run_id=run_id, passed=passed, memory=process_memory(),
                        bytecode_cache=bytecode_cache_changes(bytecode_before, bytecode_after))
            return 0 if passed else 1
    finally:
        writer.close()
        if coordinator and session:
            if coordinator.reservations:
                atomic_json(directory / "uncertain-reservations.json", coordinator.reservations)
            coordinator.finish(session, stopped=not coordinator.reservations)
        print(diagnostic(directory), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--protocol-version", type=int, default=VERSION)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--collect", type=Path)
    parser.add_argument("--compile", nargs="+", type=Path)
    parser.add_argument("--project-python", type=Path)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--report", nargs="+", type=Path)
    parser.add_argument("--parallelism", default=os.environ.get("LOOP_MANAGER_VERIFY_PARALLELISM", "auto"))
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.protocol_version != VERSION:
        raise ContractError("unsupported runner protocol version")
    if arguments.version:
        print(json.dumps({"version": VERSION, "adapters": ADAPTERS}))
        return 0
    if arguments.collect:
        unittest_adapter.collect(json.loads(arguments.collect.read_text(encoding="utf-8")))
        return 0
    if arguments.compile:
        unittest_adapter.compile_paths(Path.cwd(), arguments.compile)
        return 0
    if arguments.worker:
        return worker_main()
    if arguments.report:
        summaries = [summarize(path) for path in arguments.report]
        print(json.dumps({"summaries": summaries, "comparison": comparison(arguments.report, summaries)},
                         ensure_ascii=False, indent=2))
        return 0
    cap = parse_parallelism(arguments.parallelism)
    directory = Path(os.environ.get("LOOP_MANAGER_VERIFY_LOG_DIR", str(Path.cwd() / ".cache/verify" / uuid.uuid4().hex)))
    if arguments.execute or "LOOP_MANAGER_VERIFY_SESSION" in os.environ:
        return run(directory, cap, arguments.timeout, arguments.project_python)
    # 단독 스크립트도 기존 전체 verify timeout과 자식 종료 확인 경계를 사용한다.
    from loop_manager_internal.execution.verify_process import run_verify_process
    environment = dict(os.environ, LOOP_MANAGER_VERIFY_LOG_DIR=str(directory), PYTHONIOENCODING="utf-8")
    timeout = arguments.timeout
    config = Path("config/config.ini")
    if config.is_file():
        from loop_manager_internal.config.managed_config import load_managed_config
        managed = load_managed_config(config)
        if "--timeout" not in sys.argv:
            timeout = managed.verify_timeout_seconds
        if "--parallelism" not in sys.argv and "LOOP_MANAGER_VERIFY_PARALLELISM" not in os.environ:
            cap = managed.verify_parallelism
    passed, timed_out, output = run_verify_process(
        [sys.executable, "-u", str(Path(__file__).with_name("entrypoint.py")), "--execute", "--parallelism", str(cap), "--timeout", str(timeout), "--project-python", str(arguments.project_python or sys.executable)],
        Path.cwd(), timeout, environment=environment, job_scope=True)
    print(output, end="")
    if timed_out:
        print(diagnostic(directory))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
