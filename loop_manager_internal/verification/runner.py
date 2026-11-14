"""같은 manifest를 직렬/부하 적응형 worker 프로세스에서 실행한다."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import math
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
from loop_manager_internal.verification.coordination import CoordinatorClient, VerifyCoordinator
from loop_manager_internal.verification.guard import MARKER, install_guard, _local
from loop_manager_internal.verification.unittest_adapter import worker_main, start_worker, stop_worker, failure_output
from loop_manager_internal.verification.manifest import validate_report
from loop_manager_internal.verification.contract import VERSION, ADAPTERS, ContractError, load, fields
from loop_manager_internal.verification import unittest_adapter, perl_adapter
from loop_manager_internal.verification.core import execute_command
from loop_manager_internal.verification.execution_clock import ExecutionClock, BUDGET_DIRECTORY, VerifyBudgetError
from loop_manager_internal.verification.resources import DEFAULT_WORKER_BYTES, ResourceSampler, parse_parallelism, process_memory
from loop_manager_internal.verification.timing import (
    EventWriter, atomic_json, bytecode_cache_changes,
    bytecode_cache_snapshot, bytecode_policy, comparison, diagnostic,
    phase, summarize,
)


IMPLEMENTATIONS = {"unittest": unittest_adapter, "perl-tap": perl_adapter}


def execution_queue(modules, directory, metadata, manifest, stage_id):
    """Use the latest diagnostic only when its whole execution scope is comparable."""
    original = list(modules)
    if not original:
        return original
    try:
        manifest = json.loads(json.dumps(manifest))
        stage_index, stage = next((index, stage) for index, stage in enumerate(manifest["stages"])
                                  if stage["id"] == stage_id)
        if stage["units"][0]["execution"] != "independent":
            return original
        candidates = [path for path in directory.parent.glob("*/summary.json")
                      if path.parent != directory]
        if not candidates or not isinstance(metadata.get("execution_context"), dict):
            return original
        history = max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))
        previous = json.loads(history.read_text(encoding="utf-8"))
        prior_manifest = json.loads((history.parent / "manifest.json").read_text(encoding="utf-8"))
        if (type(prior_manifest.get("version")) is not int or prior_manifest["version"] != VERSION
                or prior_manifest.get("run_id") != previous["metadata"]["run_id"]
                or previous["metadata"].get("mode") != "full"):
            return original
        prior_index = next(index for index, item in enumerate(prior_manifest["stages"])
                           if item["id"] == stage_id)
        def scope(value, index):
            return {**value, "stages": value["stages"][:index + 1]}
        current = {"metadata": metadata, "phases": [], "counts": {}, "active": [], "result": None}
        compared = comparison([directory, history.parent], [current, previous],
            manifests=[scope(manifest, stage_index), scope(prior_manifest, prior_index)])
        if (metadata.get("execution_context") != previous["metadata"].get("execution_context")
                or not compared["scope_matches"] or not compared["observed_environment_matches"]
                or previous["active"] or previous["result"]["passed"] is not True):
            return original
        uncertain = history.parent / "uncertain-reservations.json"
        if uncertain.exists() and json.loads(uncertain.read_text(encoding="utf-8")):
            return original
        selected = [unit for unit in stage["units"] if unit.get("status") != "NOT_SELECTED"]
        if [unit["payload"] for unit in selected] != json.loads(json.dumps([asdict(unit) for unit in original])):
            return original
        reports = {}
        for path in (history.parent / f"stage-{prior_index}").glob("module-*.json"):
            report = json.loads(path.read_text(encoding="utf-8"))
            name = report["module"]
            if name in reports:
                return original
            reports[name] = report
        if set(reports) != {unit.name for unit in original}:
            return original
        events = [event for event in previous["phases"]
                  if event.get("stage_id") == stage_id and event.get("name", "").startswith("module:")]
        if (len(events) != len(original) or {event["name"] for event in events} !=
                {f"module:{unit.name}" for unit in original}):
            return original
        module_events = {event["name"]: event for event in events}
        durations = {}
        for unit in original:
            report = reports[unit.name]
            if (type(report.get("version")) is not int or report["version"] != VERSION
                    or report.get("run_id") != previous["metadata"]["run_id"]
                    or report.get("stage_id") != stage_id or report.get("unit_id") != unit.name
                    or report.get("runner_error") or not validate_report(unit, report)):
                return original
            event = module_events[f"module:{unit.name}"]
            value = event.get("duration")
            if (event.get("result") != "complete" or event.get("unit_id") != unit.name
                    or event.get("run_id") != previous["metadata"]["run_id"]
                    or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                return original
            durations[unit.name] = value
        return sorted(original, key=lambda unit: -durations[unit.name])
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, StopIteration, RuntimeError):
        return original


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
    integration_stage = any(stage['kind'] == 'runner-integration' for stage in connection.stages)
    if integration_stage:
        from loop_manager_internal.verification.integration import _require_orchestrator
        _require_orchestrator()
        if os.name == 'nt' :
            raise ContractError('Loop Manager full integration requires WSL; use scripts/verify.ps1')
    observation = json.loads(os.environ["LOOP_MANAGER_VERIFY_OBSERVER"]) if "LOOP_MANAGER_VERIFY_OBSERVER" in os.environ else None
    observer = None
    if "LOOP_MANAGER_VERIFY_OBSERVER" in os.environ:
        fields(observation, ("endpoint", "session", "secret"))
        endpoint = observation["endpoint"]
        if (not isinstance(endpoint, list) or len(endpoint) != 2 or not isinstance(endpoint[0], str)
                or not _local(endpoint[0]) or type(endpoint[1]) is not int or not 0 < endpoint[1] < 65536
                or any(not isinstance(observation[key], str) or not observation[key] for key in ("session", "secret"))):
            raise ContractError("invalid standalone runner observation context")
        if any(key in os.environ for key in ("LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SESSION", "LOOP_MANAGER_VERIFY_SECRET")):
            raise ContractError("standalone observation and resource coordination cannot be combined")
        observer = CoordinatorClient(observation["endpoint"], observation["session"], observation["secret"])
    run_id = observation["session"] if observer else os.environ.get("LOOP_MANAGER_VERIFY_SESSION", uuid.uuid4().hex)
    entrypoint = Path(__file__).with_name("entrypoint.py").resolve()
    started_at = time.monotonic()
    budget = ExecutionClock()
    previous_budget = os.environ.get(BUDGET_DIRECTORY)
    os.environ[BUDGET_DIRECTORY] = str(budget.directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["LOOP_MANAGER_VERIFY_PROJECT_ROOT"] = str(connection.root)
    os.environ[MARKER] = "1"
    if not integration_stage:
        install_guard()
    parent_run_id = os.environ.get("LOOP_MANAGER_VERIFY_PARENT_RUN_ID")
    writer_identity = {"run_id": run_id}
    if parent_run_id:
        writer_identity["parent_run_id"] = parent_run_id
    writer = EventWriter(directory / "runner.jsonl", identity=writer_identity)
    coordinator = None
    session = None
    preserve_budget = False
    termination_uncertain = False
    try:
        if observer:
            while not observer.request("observer_state", waiting=False)["ready"]:
                remaining = budget.remaining(timeout)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("verify observer startup timeout")
                time.sleep(0.05)
        if "LOOP_MANAGER_VERIFY_ENDPOINT" in os.environ:
            client = CoordinatorClient(json.loads(os.environ["LOOP_MANAGER_VERIFY_ENDPOINT"]),
                                       os.environ["LOOP_MANAGER_VERIFY_SESSION"], os.environ["LOOP_MANAGER_VERIFY_SECRET"])
            coordinated = True
        else:
            coordinator = VerifyCoordinator()
            session, secret = coordinator.open(cap)
            coordinator.track_waiting(session, directory / "local-budget" if observer else budget.directory)
            class LocalClient:
                def request(self, operation, **data):
                    if operation == "open_child" and coordinator.server is None:
                        coordinator.start()
                    reply = coordinator.request(session, secret, operation, **data)
                    if observer:
                        observer.request("observer_state", waiting=coordinator.sessions[session]["waiting"])
                    return reply
            client, coordinated = LocalClient(), False
        def git_value(*arguments):
            return subprocess.run(["git", *arguments], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        metadata = writer.emit("run_start", version=VERSION, run_id=run_id, mode="full", adapter=connection.adapter, adapter_support=ADAPTERS[connection.adapter], revision=git_value("rev-parse", "HEAD"), git=git_value("--version"),
                    python=sys.version, command=sys.argv, cap=cap, coordinated=coordinated,
                    bytecode_policy=bytecode_policy(),
                    execution_context={"python": str(connection.python), "stages": list(connection.stages),
                        "parallelism": cap, "coordinated": coordinated, "instrumentation": 1,
                        "temporary_directory": tempfile.gettempdir(), "pythonpath": os.environ.get("PYTHONPATH")},
                    environment={"host": platform.node(), "platform": sys.platform,
                                 "limits": asdict(ResourceSampler().sample())},
                    estimate={"worker_bytes": DEFAULT_WORKER_BYTES, "measured": False})
        with writer.activate(), phase("verify"):
            with phase("bytecode_cache_snapshot_before", writer=writer):
                bytecode_before = bytecode_cache_snapshot(Path.cwd())
            passed = True
            manifest_stages = []
            for index, stage in enumerate(connection.stages):
                remaining = budget.remaining(timeout)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("verify total timeout")
                stage_directory = directory / f"stage-{index}"
                stage_directory.mkdir()
                writer.identity["stage_id"] = stage["id"]
                with phase(f"stage:{stage['id']}"):
                    if stage["kind"] == "tests":
                        modules, project_python = adapter.collect_project(connection, stage, stage_directory,
                            run_id, entrypoint, remaining, execute_command, writer, client)
                        chosen = modules
                        units = [{"id": module.name, "ids": list(module.ids),
                            "argv": [str(connection.python) if connection.adapter == "unittest" else sys.executable, "-u", str(entrypoint), "--worker" if connection.adapter == "unittest" else "--perl-worker"],
                            "cwd": str(connection.root / stage["cwd"]), "execution": stage["execution"],
                            "payload": asdict(module)} for module in modules]
                        manifest_stages.append({"id": stage["id"], "kind": "tests", "python" if connection.adapter == "unittest" else "tool": project_python, "units": units})
                        atomic_json(directory / "manifest.json", {"version": VERSION, "run_id": run_id,
                            "adapter": connection.adapter, "adapter_version": ADAPTERS[connection.adapter]["version"], "stages": manifest_stages})
                        queue = execution_queue(chosen, directory, metadata,
                            {"version": VERSION, "adapter": connection.adapter,
                             "adapter_version": ADAPTERS[connection.adapter]["version"], "stages": manifest_stages}, stage["id"]) if connection.adapter == "unittest" else chosen
                        writer.emit("execution_queue", units=[unit.name for unit in queue])
                        passed = adapter.execute(connection, stage, queue, stage_directory, writer, client, run_id, entrypoint)
                    elif stage['kind'] == 'runner-integration':
                        from loop_manager_internal.verification import integration
                        units = [{'id': identity, 'ids': [identity], 'execution': 'serial'} for identity in integration.case_ids()]
                        manifest_stages.append({'id': stage['id'], 'kind': stage['kind'], 'suite': stage['suite'], 'units': units,
                                               'result_path': f'stage-{index}/integration-result.json'})
                        atomic_json(directory / 'manifest.json', {'version': VERSION, 'run_id': run_id,
                            'adapter': connection.adapter, 'adapter_version': ADAPTERS[connection.adapter]['version'], 'stages': manifest_stages})
                        result = integration.execute(connection.root, stage_directory, client, budget, timeout,
                                                     run_id, stage['id'], metadata['revision'])
                        saved = json.loads((stage_directory / 'integration-result.json').read_text(encoding='utf-8'))
                        if saved != result:
                            raise ContractError('integration result receipt mismatch')
                        writer.emit('integration_result', result=result,
                                    diagnostic=str(stage_directory / 'integration-result.json'))
                        passed = integration.validate_stage(result, run_id, stage['id'], metadata['revision'])
                        if git_value('rev-parse', 'HEAD') != metadata['revision']:
                            raise ContractError('integration candidate revision changed')
                    else:
                        command = perl_adapter.command_argv(connection, stage["argv"]) if connection.adapter == "perl-tap" else stage["argv"]
                        argv = [{"{python}": str(connection.python), "{runner}": str(entrypoint)}.get(item, item)
                                for item in command]
                        manifest_stages.append({"id": stage["id"], "kind": "command", "argv": argv, "cwd": stage["cwd"]})
                        atomic_json(directory / "manifest.json", {"version": VERSION, "run_id": run_id,
                            "adapter": connection.adapter, "adapter_version": ADAPTERS[connection.adapter]["version"], "stages": manifest_stages})
                        passed = execute_command(argv, connection.root / stage["cwd"], remaining,
                                                 writer, client, run_id, stage["id"])
                if not passed:
                    break
            with phase("bytecode_cache_snapshot_after", writer=writer):
                bytecode_after = bytecode_cache_snapshot(Path.cwd())
            remaining = budget.remaining(timeout)
            if remaining is not None and remaining <= 0:
                raise TimeoutError("verify total timeout")
            if passed  and [stage['id'] for stage in manifest_stages] != [stage['id'] for stage in connection.stages]:
                raise ContractError('missing mandatory verify stage')
            writer.emit("run_end", version=VERSION, run_id=run_id, passed=passed, memory=process_memory(),
                        budget=budget.snapshot(timeout),
                        bytecode_cache=bytecode_cache_changes(bytecode_before, bytecode_after))
            return 0 if passed else 1
    except BaseException as error:
        if isinstance(error, VerifyTerminationUncertain):
            preserve_budget = True
            termination_uncertain = True
        if isinstance(error, VerifyBudgetError):
            preserve_budget = True
            # session 종료가 누계 파일을 갱신하기 전에 실패한 원본을 남긴다.
            if error.diagnostic_directory is not None:
                shutil.copytree(error.diagnostic_directory, directory / "budget-error", symlinks=True)
        raise
    finally:
        try:
            writer.close()
            if coordinator and session:
                if coordinator.reservations:
                    atomic_json(directory / "uncertain-reservations.json", coordinator.reservations)
                try:
                    if observer:
                        observer.request("observer_state", waiting=False)
                finally:
                    try:
                        coordinator.finish(session, stopped=not coordinator.reservations and not termination_uncertain)
                    finally:
                        coordinator.close()
            print(diagnostic(directory), flush=True)
        finally:
            try:
                if not preserve_budget and not isinstance(sys.exc_info()[1], VerifyTerminationUncertain):
                    budget.close()
            finally:
                if previous_budget is None:
                    os.environ.pop(BUDGET_DIRECTORY, None)
                else:
                    os.environ[BUDGET_DIRECTORY] = previous_budget


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--perl-worker", action="store_true")
    parser.add_argument("--perl-collect", type=Path)
    parser.add_argument("--protocol-version", type=int, default=VERSION)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--collect", type=Path)
    parser.add_argument("--compile", nargs="+", type=Path)
    parser.add_argument("--project-python", type=Path)
    parser.add_argument("--timeout", type=float, default=os.environ.get("LOOP_MANAGER_VERIFY_TIMEOUT"))
    parser.add_argument("--report", nargs="+", type=Path)
    parser.add_argument("--parallelism", default=os.environ.get("LOOP_MANAGER_VERIFY_PARALLELISM", "auto"))
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.protocol_version != VERSION:
        raise ContractError("unsupported runner protocol version")
    if arguments.version:
        print(json.dumps({"version": VERSION, "adapters": ADAPTERS}))
        return 0
    if arguments.perl_collect:
        perl_adapter.collect(json.loads(arguments.perl_collect.read_text(encoding="utf-8")))
        return 0
    if arguments.perl_worker:
        return perl_adapter.worker_main()
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
    if arguments.timeout is not None and (not math.isfinite(arguments.timeout) or arguments.timeout <= 0):
        parser.error("--timeout must be finite and positive")
    cap = parse_parallelism(arguments.parallelism)
    directory = Path(os.environ.get("LOOP_MANAGER_VERIFY_LOG_DIR", str(Path.cwd() / ".cache/verify" / uuid.uuid4().hex)))
    coordinated = "LOOP_MANAGER_VERIFY_SESSION" in os.environ
    if coordinated and not all(os.environ.get(name) for name in (
            "LOOP_MANAGER_VERIFY_SESSION", "LOOP_MANAGER_VERIFY_ENDPOINT", "LOOP_MANAGER_VERIFY_SECRET")):
        raise ContractError("incomplete Manager coordination context")
    if arguments.execute or coordinated:
        # Older coordinated Managers own the outer deadline but do not transmit it.
        timeout = arguments.timeout if arguments.timeout is not None else (
            None if coordinated or "LOOP_MANAGER_VERIFY_OBSERVER" in os.environ else 600)
        return run(directory, cap, timeout, arguments.project_python)
    # 단독 스크립트도 기존 전체 verify timeout과 자식 종료 확인 경계를 사용한다.
    from loop_manager_internal.execution.verify_process import run_verify_process
    environment = dict(os.environ, LOOP_MANAGER_VERIFY_LOG_DIR=str(directory), PYTHONIOENCODING="utf-8")
    timeout = arguments.timeout if arguments.timeout is not None else 600
    config = Path("config/config.ini")
    if config.is_file():
        from loop_manager_internal.config.settings import Settings
        managed = Settings(config)
        if arguments.timeout is None:
            timeout = managed.verify_timeout
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
