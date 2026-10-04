"""stdout과 분리해 즉시 flush하는 검증 이벤트와 진단 집계."""
from __future__ import annotations
from collections import Counter
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock

_current_writer: ContextVar[EventWriter | None] = ContextVar("verify_events", default=None)
_fixture_timing_active: ContextVar[bool] = ContextVar("fixture_timing_active", default=False)


class EventWriter:
    def __init__(self, path: Path, *, clock=time.monotonic):
        self.path, self.clock = path, clock
        self.stream = path.open("a", encoding="utf-8")

    def emit(self, kind: str, **values):
        event = {"kind": kind, "at": self.clock(), "utc": datetime.now(timezone.utc).isoformat(),
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
    start = writer.emit("phase_start", name=name)
    try:
        yield
    except BaseException:
        writer.emit("phase_end", name=name, start=start["at"],
                    duration=writer.clock() - start["at"], result="error")
        raise
    else:
        writer.emit("phase_end", name=name, start=start["at"],
                    duration=writer.clock() - start["at"], result="complete")


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, writer, **kwargs):
        super().__init__(*args, **kwargs)
        self.writer, self.started, self.outcomes, self.completed = writer, {}, {}, []

    def startTest(self, test):
        super().startTest(test)
        self.started[test.id()] = self.writer.emit("test_start", id=test.id())["at"]

    def _outcome(self, test, outcome):
        if self.outcomes.get(test.id()) not in {"failure", "error", "unexpected_success"}:
            self.outcomes[test.id()] = outcome
        if test.id() not in self.started:
            self.writer.emit("fixture_result", id=test.id(), result=outcome)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._outcome(test, "success")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._outcome(test, "failure")

    def addError(self, test, err):
        super().addError(test, err)
        self._outcome(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._outcome(test, "skip")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._outcome(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._outcome(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        outcome = "success"
        if err is not None:
            outcome = "failure" if issubclass(err[0], test.failureException) else "error"
            self._outcome(test, outcome)
        self.writer.emit("subtest_result", id=test.id(), result=outcome)

    def stopTest(self, test):
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
    names = ("_handleClassSetUp", "_tearDownPreviousClass", "_handleModuleFixture", "_handleModuleTearDown")
    try:
        with ExitStack() as patches:
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
        _fixture_timing_active.reset(token)


def atomic_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def summarize(directory: Path, *, now=None):
    now = time.monotonic() if now is None else now
    finished_at = None
    completed, active, phases, decisions, metadata, fixtures, result = [], {}, [], [], {}, [], None
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            if not line.endswith("\n"):
                break
            event = json.loads(line)
            kind = event["kind"]
            identity = event.get("id", event.get("name", ""))
            key = (event["pid"], kind.split("_")[0], identity)
            if kind in {"test_start", "phase_start"}:
                active[key] = event
            elif kind in {"test_end", "phase_end"}:
                active.pop(key, None)
                (completed if kind == "test_end" else phases).append(event)
                if kind == "phase_end" and event["name"] in {"verify_process", "verify"}:
                    finished_at = max(finished_at or event["at"], event["at"])
            elif kind == "allocation":
                decisions.append(event)
            elif kind == "run_start":
                metadata = event
            elif kind == "run_end":
                result = event
            elif kind == "fixture_result":
                fixtures.append(event)
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
    summary = {"metadata": metadata, "counts": dict(Counter(item["result"] for item in completed)),
               "tests": sorted(completed, key=lambda item: item["duration"], reverse=True),
               "phases": sorted(phases, key=lambda item: item["duration"], reverse=True),
               "active": [{**event, "elapsed": max(0, (finished_at if finished_at is not None else now) - event["at"])} for event in active.values()],
               "allocations": decisions, "git_snapshots": snapshots, "fixture_results": fixtures, "result": result}
    atomic_json(directory / "summary.json", summary)
    return summary


def comparison(directories, summaries):
    """범위와 관측된 환경을 대조하고 실측 시간만 나란히 제공한다."""
    scopes, contexts, runs = [], [], []
    for directory, summary in zip(directories, summaries):
        path = directory / "manifest.json"
        modules = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        scopes.append(tuple(identity for module in modules for identity in module["ids"]))
        metadata = summary["metadata"]
        environment = metadata.get("environment", {})
        limits = environment.get("limits", {})
        contexts.append((metadata.get("python"), metadata.get("git"), environment.get("host"),
                         environment.get("platform"), limits.get("cpus"), limits.get("affinity_cpus"),
                         limits.get("applied_quota"), limits.get("source")))
        def duration(name):
            return next((event["duration"] for event in summary["phases"] if event["name"] == name), None)
        runs.append({"directory": str(directory), "revision": metadata.get("revision"), "cap": metadata.get("cap"),
                     "seconds": duration("verify_process") if duration("verify_process") is not None else duration("verify"),
                     "git_fixture_seconds": sum(event["duration"] for event in summary["phases"]
                                                if event["name"].startswith("git_fixture:")),
                     "git_snapshot_count": sum(cost["count"] for cost in summary.get("git_snapshots", {}).values()),
                     "git_snapshot_commands": sum(command["count"] for cost in summary.get("git_snapshots", {}).values()
                                                  for command in cost["commands"].values()),
                     "passed": (summary.get("result") or {}).get("passed"), "counts": summary["counts"]})
    scope_matches = bool(scopes and scopes[0]) and all(scope == scopes[0] for scope in scopes)
    environment_matches = bool(contexts and all(value is not None for value in contexts[0][:6])) and all(
        context == contexts[0] for context in contexts)
    complete = all(run["passed"] is True and not summary["active"] for run, summary in zip(runs, summaries))
    return {"scope_matches": scope_matches, "observed_environment_matches": environment_matches,
            "comparable": len(runs) > 1 and scope_matches and environment_matches and complete,
            "runs": runs, "note": "관측된 환경 일치만 확인합니다. 부하·메모리 제한도 원본 기록과 대조하세요. 포함 관계인 단계 시간은 합산하지 않습니다."}


def diagnostic(directory: Path):
    summary = summarize(directory)
    lines = [f"verify 시간 진단: {directory / 'summary.json'}", f"완료 결과: {summary['counts']}",
             f"fixture 결과: {dict(Counter(event['result'] for event in summary['fixture_results']))}"]
    for event in summary["tests"][:5]:
        lines.append(f"느린 테스트 {event['duration']:.3f}s: {event['id']}")
    for event in summary["active"][:5]:
        lines.append(f"미완료 {event['elapsed']:.3f}s: {event.get('id', event.get('name'))}")
    return "\n".join(lines)
