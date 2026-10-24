"""stdout과 분리해 즉시 flush하는 검증 이벤트와 진단 집계."""
from __future__ import annotations
from collections import Counter
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
import unittest
import uuid
from unittest import mock

_current_writer: ContextVar[EventWriter | None] = ContextVar("verify_events", default=None)
_fixture_timing_active: ContextVar[bool] = ContextVar("fixture_timing_active", default=False)
_test_fixture_failures: ContextVar[dict[int, tuple[str, BaseException]] | None] = ContextVar("test_fixture_failures", default=None)
_phase_names: ContextVar[tuple[str, ...]] = ContextVar("verify_phase_names", default=())


class EventWriter:
    def __init__(self, path: Path, *, clock=time.monotonic, identity=None):
        self.path, self.clock = path, clock
        self.identity = identity or {}
        self.active_fixtures = {}
        self.stream = path.open("a", encoding="utf-8")

    def emit(self, kind: str, **values):
        event = {"version": 1, **self.identity, "kind": kind, "at": self.clock(),
                 "utc": datetime.now(timezone.utc).isoformat(), "clock_domain": sys.platform,
                 "pid": os.getpid(), **values}
        self.stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.stream.flush()
        return event

    def close(self):
        self.stream.close()

    @contextmanager
    def activate(self):
        token = _current_writer.set(self)
        try:
            yield self
        finally:
            _current_writer.reset(token)


@contextmanager
def phase(name: str, *, writer=None):
    writer = writer or _current_writer.get()
    if writer is None:
        yield
        return
    phase_id = uuid.uuid4().hex
    start = writer.emit("phase_start", name=name, phase_id=phase_id)
    token = _phase_names.set((*_phase_names.get(), name))
    try:
        yield
    except BaseException:
        writer.emit("phase_end", name=name, phase_id=phase_id, start=start["at"],
                    duration=writer.clock() - start["at"], result="error")
        raise
    else:
        writer.emit("phase_end", name=name, phase_id=phase_id, start=start["at"],
                    duration=writer.clock() - start["at"], result="complete")
    finally:
        _phase_names.reset(token)


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, writer, **kwargs):
        super().__init__(*args, **kwargs)
        self.writer, self.started, self.outcomes, self.completed = writer, {}, {}, []

    def startTest(self, test):
        super().startTest(test)
        self.writer.current_test = test.id()
        self.started[test.id()] = self.writer.emit("test_start", id=test.id())["at"]

    def _outcome(self, test, outcome):
        previous = self.outcomes.get(test.id())
        if (previous not in {"failure", "error", "unexpected_success"}
                and not (previous == "fixture_error"
                         and outcome in {"success", "skip", "expected_failure", "fixture_error"})):
            self.outcomes[test.id()] = outcome
        if test.id() not in self.started:
            self.writer.emit("fixture_result", id=test.id(), result=outcome)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._outcome(test, "success")

    def addFailure(self, test, err):
        fixture = self._fixture_scope(test, err)
        if fixture is not None:
            super().addError(test, err)
            self._record_fixture_error(test, fixture)
            return
        super().addFailure(test, err)
        failures = _test_fixture_failures.get()
        fixture = failures.pop(id(test), None) if failures is not None else None
        if fixture is None:
            self._outcome(test, "failure")
            return
        self.writer.emit("fixture_result", id=test.id(), result="failure", scope=fixture)
        self._outcome(test, "fixture_error")

    def addError(self, test, err):
        fixture = self._fixture_scope(test, err)
        super().addError(test, err)
        if fixture is None:
            self._outcome(test, "error")
            return
        self._record_fixture_error(test, fixture)

    def _fixture_scope(self, test, err):
        failures = _test_fixture_failures.get()
        fixture = failures.get(id(test)) if failures is not None else None
        if fixture is None or err[1] is not fixture[1]:
            return None
        del failures[id(test)]
        return fixture[0]

    def _record_fixture_error(self, test, fixture):
        self.writer.emit("fixture_result", id=test.id(), result="error", scope=fixture)
        self._outcome(test, "fixture_error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._outcome(getattr(test, "test_case", test), "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._outcome(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._outcome(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        fixture = self.writer.active_fixtures.get(id(test))
        outcome = "success"
        if err is not None:
            outcome = "failure" if issubclass(err[0], test.failureException) else "error"
            if fixture is None:
                self._outcome(test, outcome)
            else:
                self._record_fixture_error(test, fixture)
        event = {"id": test.id(), "result": outcome}
        if fixture is not None:
            event["scope"] = fixture
        self.writer.emit("subtest_result", **event)

    def stopTest(self, test):
        self.writer.current_test = None
        identity = test.id()
        started = self.started.pop(identity)
        if identity not in self.outcomes:
            self.writer.emit("test_interrupted", id=identity, elapsed=self.writer.clock() - started)
            super().stopTest(test)
            return
        self.writer.emit("test_end", id=identity, start=started,
                         duration=self.writer.clock() - started,
                         result=self.outcomes.get(identity, "error"))
        self.completed.append(identity)
        super().stopTest(test)


@contextmanager
def fixture_timing():
    """load_tests의 suite 구조를 유지하며 표준 fixture hook을 기록한다."""
    if _fixture_timing_active.get():
        yield
        return
    token = _fixture_timing_active.set(True)
    fixture_failures = _test_fixture_failures.set({})
    names = ("_handleClassSetUp", "_tearDownPreviousClass", "_handleModuleFixture", "_handleModuleTearDown")
    try:
        with ExitStack() as patches:
            subtest = unittest.TestCase.subTest
            variants = Counter()
            @contextmanager
            def timed_subtest(test, *args, **kwargs):
                variants[test.id()] += 1
                with phase(f'subtest:{test.id()}:{variants[test.id()]}'), subtest(test, *args, **kwargs):
                    yield
            patches.enter_context(mock.patch.object(unittest.TestCase, 'subTest', timed_subtest))
            test_case_types = [unittest.TestCase]
            isolated_async_case = getattr(unittest, "IsolatedAsyncioTestCase", None)
            if isolated_async_case is not None:
                test_case_types.append(isolated_async_case)
            for case_type in test_case_types:
                for name, scope in (("_callSetUp", "setUp"), ("_callTearDown", "tearDown")):
                    original = getattr(case_type, name)

                    def measured_test_fixture(test, *args, _original=original, _scope=scope, **kwargs):
                        writer = _current_writer.get()
                        active = writer.active_fixtures if writer is not None else None
                        key = id(test)
                        previous = active.get(key) if active is not None else None
                        if active is not None:
                            active[key] = _scope
                        try:
                            return _original(test, *args, **kwargs)
                        except unittest.SkipTest:
                            raise
                        except KeyboardInterrupt:
                            raise
                        except BaseException as error:
                            failures = _test_fixture_failures.get()
                            if failures is not None:
                                failures[id(test)] = (_scope, error)
                            raise
                        finally:
                            if active is not None:
                                if previous is None:
                                    active.pop(key, None)
                                else:
                                    active[key] = previous

                    patches.enter_context(mock.patch.object(case_type, name, measured_test_fixture))
            for name in names:
                original = getattr(unittest.TestSuite, name)

                def measured(suite, *args, _name=name, _original=original):
                    test = args[0] if len(args) == 2 else None
                    result = args[-1]
                    previous = getattr(result, "_previousTestClass", None)
                    current = test.__class__ if test is not None else None
                    if _name == "_handleClassSetUp":
                        changed, owner = current != previous, current
                    elif _name == "_tearDownPreviousClass":
                        changed, owner = previous is not None and current != previous, previous
                    elif _name == "_handleModuleFixture":
                        changed = previous is None or previous.__module__ != current.__module__
                        owner = current.__module__
                    else:
                        changed, owner = previous is not None, previous.__module__ if previous else None
                    if changed:
                        label = owner if isinstance(owner, str) else f"{owner.__module__}.{owner.__qualname__}"
                        with phase(f"fixture:{_name}:{label}"):
                            return _original(suite, *args)
                    return _original(suite, *args)

                patches.enter_context(mock.patch.object(unittest.TestSuite, name, measured))
            yield
    finally:
        _test_fixture_failures.reset(fixture_failures)
        _fixture_timing_active.reset(token)


def atomic_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def bytecode_policy():
    """Expose the effective interpreter cache choice without guessing outcomes."""
    return {
        "write_enabled": not sys.dont_write_bytecode,
        "cache_tag": sys.implementation.cache_tag,
        "cache_prefix": sys.pycache_prefix,
        "optimization": sys.flags.optimize,
    }


def bytecode_cache_snapshot(root: Path):
    """Observe this runner's Python sources and their current interpreter caches."""
    root = Path(root)
    sources = []
    entrypoint = root / "loop_manager.py"
    if entrypoint.is_file() and not entrypoint.is_symlink():
        sources.append(entrypoint)
    for base in (root / "loop_manager_internal", root / "tests"):
        if not base.is_dir() or base.is_symlink():
            continue
        for directory, dirs, filenames in os.walk(base, followlinks=False):
            parent = Path(directory)
            dirs[:] = sorted(name for name in dirs
                             if name != "__pycache__" and not (parent / name).is_symlink())
            sources.extend(parent / name for name in filenames if name.endswith(".py"))

    observed = {}
    for source in sources:
        try:
            cache = Path(importlib.util.cache_from_source(str(source)))
            metadata = cache.stat(follow_symlinks=False)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(metadata.st_mode):
            observed[source.relative_to(root).as_posix()] = (
                metadata.st_dev, metadata.st_ino, metadata.st_size,
                metadata.st_mtime_ns, metadata.st_ctime_ns,
            )
    return observed


def bytecode_cache_changes(before, after):
    """Count newly available, changed, retained and removed project caches."""
    common = before.keys() & after.keys()
    return {
        "existing": len(before),
        "present_after": len(after),
        "created": len(after.keys() - before.keys()),
        "changed": sum(before[path] != after[path] for path in common),
        "unchanged": sum(before[path] == after[path] for path in common),
        "removed": len(before.keys() - after.keys()),
    }


def summarize(directory: Path, *, now=None):
    now = time.monotonic() if now is None else now
    finished_at, executions = {}, {}
    ended_runs, ended_stages = set(), set()
    completed, active, phases, decisions, metadata, fixtures, result = [], {}, [], [], {}, [], None
    git_costs, git_incomplete = [], []
    assignments, module_results, child_events, queues, selections = [], [], [], [], []
    bytecode_cache = None
    for path in sorted(directory.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            if not line.endswith("\n"):
                break
            event = json.loads(line)
            kind = event["kind"]
            identity = event.get("id", event.get("name", ""))
            run_key = (event.get("clock_domain", "legacy"), event.get("run_id"),
                       event.get("parent_run_id"))
            base_key = (*run_key, event.get("stage_id"), event.get("unit_id"), event["pid"])
            if kind in {"phase_start", "phase_end"}:
                phase_identity = event.get("phase_id")
                if phase_identity is None:
                    phase_identity = (event.get("name"), event.get("start", event.get("at")))
                key = (*run_key, event["pid"], "phase", phase_identity)
            else:
                key = (*base_key, kind.split("_")[0], identity)
            if kind in {"test_start", "phase_start"}:
                active[key] = event
            elif kind in {"test_end", "phase_end"}:
                active.pop(key, None)
                (completed if kind == "test_end" else phases).append(event)
                if kind == "phase_end" and event.get("name", "").startswith("stage:"):
                    stage_key = base_key[:4]
                    ended_stages.add(stage_key)
                    for active_key in list(active):
                        if active_key[:4] == stage_key:
                            active.pop(active_key, None)
                if kind == "phase_end" and event.get("name") in {"verify_process", "verify"}:
                    finished_at[run_key] = max(finished_at.get(run_key, event["at"]), event["at"])
            elif kind == "run_start":
                metadata = event
                executions.setdefault(run_key, {})["start"] = event
            elif kind == "run_end":
                result = event
                bytecode_cache = event.get("bytecode_cache")
                executions.setdefault(run_key, {})["end"] = event
                finished_at[run_key] = event.get("at", finished_at.get(run_key))
                ended_runs.add(run_key)
            elif kind == "allocation":
                decisions.append(event)
            elif kind == "unit_start":
                assignments.append(event)
            elif kind == "module_result":
                module_results.append(event)
            elif kind in {"result_file_observed", "job_child_wait", "job_child_wait_observation",
                          "job_child_exit_confirmed", "job_child_exit_unconfirmed"}:
                child_events.append(event)
            elif kind == "execution_queue":
                queues.append(event)
            elif kind == "partial_selection":
                selections.append(event)
            elif kind == "fixture_result":
                fixtures.append(event)
            elif kind == 'git_cost':
                git_costs.append(event)
            elif kind == 'git_cost_incomplete':
                git_incomplete.append(event)
            if kind == "run_end":
                execution_key = run_key[:3]
                for active_key in list(active):
                    if active_key[:3] == execution_key:
                        active.pop(active_key, None)
    active = {
        key: event for key, event in active.items()
        if ((event.get("clock_domain", "legacy"), event.get("run_id"),
             event.get("parent_run_id")) not in ended_runs
            and (event.get("clock_domain", "legacy"), event.get("run_id"),
                 event.get("parent_run_id"), event.get("stage_id")) not in ended_stages)
    }
    snapshots = {}
    for event in phases:
        if event.get("snapshot") != "full":
            continue
        cost = snapshots.setdefault(event["name"], {"count": 0, "seconds": 0.0, "commands": {}})
        cost["count"] += 1
        cost["seconds"] += event["duration"]
        for command, values in event["commands"].items():
            total = cost["commands"].setdefault(command, {"count": 0, "seconds": 0.0})
            total["count"] += values["count"]
            total["seconds"] += values["seconds"]
    execution_runs = []
    for (clock_domain, run_id, parent_run_id), events in executions.items():
        start, end = events.get("start"), events.get("end")
        started_at = start.get("at") if isinstance(start, dict) else None
        ended_at = end.get("at") if isinstance(end, dict) else None
        elapsed = None
        if isinstance(started_at, (int, float)) and isinstance(ended_at, (int, float)):
            elapsed = max(0.0, ended_at - started_at)
        elif (isinstance(started_at, (int, float)) and clock_domain == sys.platform):
            elapsed = max(0.0, now - started_at)
        execution_runs.append({
            "run_id": run_id, "parent_run_id": parent_run_id,
            "clock_domain": clock_domain,
            "mode": start.get("mode") if isinstance(start, dict) else None,
            "started_at": started_at, "ended_at": ended_at,
            "elapsed_seconds": elapsed,
            "result": end,
        })
    primary_run = next((item for item in execution_runs
                        if item.get("run_id") == metadata.get("run_id")), None)
    for item in active.values():
        domain = item.get("clock_domain", "legacy")
        run_key = (domain, item.get("run_id"), item.get("parent_run_id"))
        end_at = finished_at.get(run_key)
        if end_at is None and domain == sys.platform:
            end_at = now
        item["elapsed"] = max(0.0, end_at - item["at"]) if end_at is not None else None
    partial_result = _read_summary_object(directory / "partial-result.json")
    windows_pipeline = _windows_pipeline_summary(directory, metadata.get("run_id"))
    summary = {"metadata": metadata, "counts": dict(Counter(item["result"] for item in completed)),
               "tests": sorted(completed, key=lambda item: item["duration"], reverse=True),
               "phases": sorted(phases, key=lambda item: item["duration"], reverse=True),
               "active": list(active.values()),
               "allocations": decisions, "git_snapshots": snapshots, "git_commands": git_costs,
               "git_commands_incomplete": git_incomplete, "fixture_results": fixtures,
               "bytecode_cache": bytecode_cache, "result": result,
               "assignments": assignments, "module_results": module_results,
               "child_lifecycle": child_events, "execution_queues": queues,
               "partial_selections": selections, "partial_result": partial_result,
               "execution_runs": execution_runs,
               "elapsed_seconds": primary_run.get("elapsed_seconds") if primary_run else None,
               "windows_pipeline": windows_pipeline}
    atomic_json(directory / "summary.json", summary)
    return summary


def _read_summary_object(path):
    try:
        if path.is_file() and not path.is_symlink():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def _windows_pipeline_summary(directory, parent_run_id=None):
    result = _read_summary_object(directory / "result.json")
    snapshot = _read_summary_object(directory / "snapshot.json")
    if parent_run_id is not None:
        if result is not None and result.get("run_id") != parent_run_id:
            result = None
        if snapshot is not None and snapshot.get("run_id") != parent_run_id:
            snapshot = None
    if result is None and snapshot is None:
        return None
    children = {}
    for name in ("wsl", "windows"):
        child_directory = Path(str(directory) + f"-{name}")
        child = _read_summary_object(child_directory / "summary.json")
        metadata = child.get("metadata", {}) if child else {}
        if not isinstance(metadata, dict):
            child, metadata = None, {}
        if child is not None and parent_run_id is not None:
            reported_run = (result or {}).get(f"{name}_run_id")
            linked = (metadata.get("parent_run_id") == parent_run_id
                      or metadata.get("run_id") == parent_run_id
                      or reported_run is not None and metadata.get("run_id") == reported_run)
            if not linked:
                child, metadata = None, {}
        child_result = child.get("result") if child else None
        reported_path = (result or {}).get(f"{name}_diagnostic")
        summary_path = str(child_directory / "summary.json") if child else reported_path
        children[name] = {
            "directory": str(child_directory),
            "summary_path": summary_path,
            "summary_available": child is not None,
            "run_id": metadata.get("run_id") or (result or {}).get(f"{name}_run_id"),
            "parent_run_id": metadata.get("parent_run_id"),
            "clock_domain": metadata.get("clock_domain"),
            "mode": metadata.get("mode"),
            "result": child_result,
            "elapsed_seconds": child.get("elapsed_seconds") if child else None,
            "completed_tests": child.get("counts", {}) if child else {},
            "phase_count": len(child.get("phases", [])) if child else None,
            "assignment_count": len(child.get("assignments", [])) if child else None,
            "allocation_count": len(child.get("allocations", [])) if child else None,
            "git_command_count": sum(item.get("count", 0) for item in child.get("git_commands", [])) if child else None,
            "git_command_seconds": sum(item.get("seconds", 0.0) for item in child.get("git_commands", [])) if child else None,
        }
    return {"result": result, "snapshot": snapshot, "children": children}


def comparison(directories, summaries, *, manifests=None):
    """범위와 관측된 환경을 대조하고 실측 시간만 나란히 제공한다."""
    scopes, contexts, bytecode_policies, runs, contracts = [], [], [], [], []
    for index, (directory, summary) in enumerate(zip(directories, summaries)):
        path = directory / "manifest.json"
        modules = manifests[index] if manifests is not None else (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else [])
        contracts.append(None if isinstance(modules, list) else (
            modules.get("version"), modules.get("adapter"), modules.get("adapter_version"),
            [(stage["id"], stage.get("kind"), stage.get("python"), stage.get("argv"), stage.get("cwd"),
              [(unit.get("id"), unit.get("argv"), unit.get("cwd"), unit.get("execution"),
                unit.get("payload"), unit.get("status")) for unit in stage.get("units", [])])
             for stage in modules.get("stages", [])]))
        if isinstance(modules, dict):
            modules = [unit for stage in modules.get("stages", []) for unit in stage.get("units", [])]
        scopes.append(tuple(identity for module in modules
                            if module.get("status") != "NOT_SELECTED" for identity in module["ids"]))
        metadata = summary["metadata"]
        environment = metadata.get("environment", {})
        limits = environment.get("limits", {})
        execution_context = metadata.get("execution_context")
        if isinstance(execution_context, dict):
            execution_context = {key: value for key, value in execution_context.items()
                                 if key != "parallelism"}
        contexts.append((metadata.get("python"), metadata.get("git"), environment.get("host"),
                         environment.get("platform"), limits.get("cpus"), limits.get("affinity_cpus"),
                         limits.get("applied_quota"), limits.get("source"), execution_context))
        bytecode_policies.append(metadata.get("bytecode_policy"))
        def duration(name):
            return next((event["duration"] for event in summary["phases"] if event["name"] == name), None)
        fixture_phases = [event for event in summary["phases"]
                          if isinstance(event.get("name"), str) and event["name"].startswith("git_fixture:")]
        fixture_durations = []
        fixture_intervals = []
        for index, event in enumerate(fixture_phases):
            value = event.get("duration")
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                continue
            fixture_durations.append((index, value))
            start, pid = event.get("start"), event.get("pid")
            if (isinstance(start, (int, float)) and not isinstance(start, bool)
                    and math.isfinite(start) and isinstance(pid, int) and not isinstance(pid, bool)):
                fixture_intervals.append((index, pid, start, start + value))
        nested_fixture_phases = set()
        # ponytail: O(n²) over a small fixture event set; group and sort by PID if it grows.
        for child_index, child_pid, child_start, child_end in fixture_intervals:
            for parent_index, parent_pid, parent_start, parent_end in fixture_intervals:
                if (child_index != parent_index and child_pid == parent_pid
                        and parent_start <= child_start and child_end <= parent_end
                        and (parent_start < child_start or child_end < parent_end)):
                    nested_fixture_phases.add(child_index)
                    break
        runs.append({"directory": str(directory), "revision": metadata.get("revision"), "cap": metadata.get("cap"),
                     "mode": metadata.get("mode", "full"),
                     "seconds": duration("verify_process") if duration("verify_process") is not None else duration("verify"),
                     "git_fixture_seconds": sum(value for index, value in fixture_durations
                                                 if index not in nested_fixture_phases),
                     "git_snapshot_count": sum(cost["count"] for cost in summary.get("git_snapshots", {}).values()),
                     "git_snapshot_commands": sum(command["count"] for cost in summary.get("git_snapshots", {}).values()
                                                  for command in cost["commands"].values()),
                     "git_commands": sum(cost['count'] for cost in summary.get('git_commands', [])),
                     "git_command_seconds": sum(cost['seconds'] for cost in summary.get('git_commands', [])),
                     "bytecode_policy": metadata.get("bytecode_policy"),
                     "bytecode_cache": summary.get("bytecode_cache"),
                     "passed": (summary.get("result") or {}).get("passed"), "counts": summary["counts"]})
    scope_matches = bool(scopes and scopes[0]) and all(scope == scopes[0] for scope in scopes) and all(value == contracts[0] for value in contracts)
    mode_matches = bool(summaries) and all(summary["metadata"].get("mode", "full") ==
        summaries[0]["metadata"].get("mode", "full") for summary in summaries)
    scope_matches = scope_matches and mode_matches
    bytecode_policy_matches = bool(bytecode_policies and isinstance(bytecode_policies[0], dict)) and all(
        policy == bytecode_policies[0] for policy in bytecode_policies)
    environment_matches = bool(contexts and all(value is not None for value in contexts[0][:6])) and all(
        context == contexts[0] for context in contexts)
    environment_matches = environment_matches and bytecode_policy_matches
    complete = all(run["passed"] is True and not summary["active"] for run, summary in zip(runs, summaries))
    return {"scope_matches": scope_matches, "observed_environment_matches": environment_matches,
            "mode_matches": mode_matches,
            "bytecode_policy_matches": bytecode_policy_matches,
            "comparable": len(runs) > 1 and scope_matches and environment_matches and complete,
            "runs": runs, "note": "관측된 환경·bytecode 설정과 실제 캐시 파일 변화를 기록합니다. 속도 개선을 합성하지 않습니다. 부하·메모리 제한도 원본 기록과 대조하고, 포함 관계인 단계 시간은 합산하지 않습니다."}


def diagnostic(directory: Path):
    summary = summarize(directory)
    lines = [f"verify 시간 진단: {directory / 'summary.json'}", f"완료 결과: {summary['counts']}",
             f"fixture 결과: {dict(Counter(event['result'] for event in summary['fixture_results']))}"]
    if summary.get("elapsed_seconds") is not None:
        lines.append(f"전체 실제 경과: {summary['elapsed_seconds']:.3f}s")
    if summary.get("assignments") or summary.get("module_results"):
        lines.append("배정/결과: "
                     f"assignments={len(summary['assignments'])}, "
                     f"result_files_observed={sum(event['kind'] == 'result_file_observed' for event in summary['child_lifecycle'])}, "
                     f"modules_completed={len(summary['module_results'])}, "
                     f"Job_child_waits={sum(event['kind'] == 'job_child_wait' for event in summary['child_lifecycle'])}")
    partial = summary.get("partial_result")
    if isinstance(partial, dict):
        lines.append(f"부분 verify: {partial.get('status', '상태 미기록')} (전체 verify 아님)")
    pipeline = summary.get("windows_pipeline")
    if isinstance(pipeline, dict):
        result = pipeline.get("result") or {}
        lines.append(f"Windows 단계: WSL={result.get('wsl', '미확인')}, native={result.get('windows', '미확인')}")
        for name, child in pipeline.get("children", {}).items():
            if child.get("run_id"):
                status = "summary" if child.get("summary_available") else "summary 미확인"
                lines.append(f"{name} {status}: {child.get('summary_path') or '-'} (run={child['run_id']}, "
                             f"clock={child.get('clock_domain') or 'legacy'})")
    policy = summary["metadata"].get("bytecode_policy")
    cache = summary.get("bytecode_cache")
    if isinstance(policy, dict) and isinstance(cache, dict):
        mode = "enabled" if policy.get("write_enabled") else "disabled"
        lines.append(
            "Python bytecode: "
            f"{mode}, tag={policy.get('cache_tag')}, prefix={policy.get('cache_prefix')}, "
            f"existing={cache.get('existing')}, created={cache.get('created')}, "
            f"changed={cache.get('changed')}, unchanged={cache.get('unchanged')}, "
            f"removed={cache.get('removed')}"
        )
    for event in summary["tests"][:5]:
        lines.append(f"느린 테스트 {event['duration']:.3f}s: {event['id']}")
    for event in summary["active"][:5]:
        lines.append(f"미완료 {event['elapsed']:.3f}s: {event.get('id', event.get('name'))}")
    return "\n".join(lines)
