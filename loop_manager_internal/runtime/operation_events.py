"""runtime operation events boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import json
import sys
import threading
import time

from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff


_context = ContextVar("operation_context", default={})
_active_progress = ContextVar("operation_progress", default=None)
_output_lock = threading.Lock()
PROGRESS_INTERVAL_SECONDS = 30


def current_context() -> dict:
    return dict(_context.get())


def summary(value) -> str:
    return str(value).splitlines()[0][:300] if value else "-"


@contextmanager
def log_context(**values):
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def _value(value, key=None) -> str:
    text = redact_secrets(str(value if value is not None else "-"))
    if key in ("run", "runtime_sha"):
        text = text[:7]
    elif key == "project":
        text = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "-"
    return json.dumps(text, ensure_ascii=False) if not text or any(
        char.isspace() or char in '"[]=;' for char in text
    ) else text


def emit(message: str, *, level="INFO", **fields) -> None:
    identity = {key: fields.pop(key) for key in ("run", "project", "plan", "stage") if key in fields}
    with log_context(**identity):
        operation_log(OperationLogEffects(local_now), message, level=level, **fields)


class _ProgressReporter:
    def __init__(self):
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._paused = 0

    def pause(self):
        with self._lock:
            self._paused += 1

    def resume(self):
        with self._lock:
            self._paused -= 1

    def should_report(self):
        with self._lock:
            return not self._paused and not self.stopped.is_set()


@contextmanager
def progress(label: str, *, describe=None, interval=None):
    """중첩 관측이 부모 heartbeat를 잠시 이어받으며 실행 상태는 건드리지 않는다."""
    parent = _active_progress.get()
    if parent is not None:
        parent.pause()
    reporter = _ProgressReporter()
    progress_token = _active_progress.set(reporter)
    context_token = _context.set({**_context.get(), "progress_active": True})
    started = time.monotonic()
    interval = PROGRESS_INTERVAL_SECONDS if interval is None else interval
    def report():
        while not reporter.stopped.wait(interval):
            if not reporter.should_report():
                continue
            try:
                try:
                    fields = describe() if describe else {}
                except Exception:
                    fields = {"progress_observation": "미확인"}
                if reporter.stopped.is_set():
                    break
                if not reporter.should_report():
                    continue
                emit(f"{label} 진행 중", elapsed_seconds=round(time.monotonic() - started, 1),
                     **fields)
            except Exception:
                pass  # 선택적 관측 실패로 실행 결과를 바꾸지 않는다.
    thread = threading.Thread(target=copy_context().run, args=(report,), daemon=True)
    try:
        thread.start()
    except (OSError, RuntimeError):
        _active_progress.reset(progress_token)
        _context.reset(context_token)
        if parent is not None:
            parent.resume()
        yield
        return
    try:
        yield
    finally:
        reporter.stopped.set()
        thread.join(timeout=1)
        _active_progress.reset(progress_token)
        _context.reset(context_token)
        if parent is not None:
            parent.resume()


@contextmanager
def activity(label: str, *, heartbeat=False, error_level="WARN", **fields):
    """짧은 처리는 종료 한 줄, 긴 처리는 주기적 관측과 종료 결과를 기록한다."""
    from contextlib import nullcontext
    started, result = time.monotonic(), {}
    try:
        with progress(label, describe=lambda: fields) if heartbeat else nullcontext():
            yield result
    except BaseException as error:
        handoff = isinstance(error, SelfMergeRuntimeHandoff)
        level = "INFO" if handoff else error_level if isinstance(error, Exception) else "WARN"
        emit(f"{label} 종료", level=level,
             result="runtime_handoff" if handoff else "미확인" if not isinstance(error, Exception) else "실패",
             error_type=type(error).__name__, elapsed_seconds=round(time.monotonic() - started, 1), **fields)
        raise
    else:
        result = {**fields, **result}
        level = result.pop("level", "INFO")
        emit(f"{label} 종료", level=level, elapsed_seconds=round(time.monotonic() - started, 1), **result)


def local_now() -> datetime:
    """Return the local clock through a patchable boundary for operation logs."""

    return datetime.now()


@dataclass(frozen=True)
class OperationLogEffects:
    """Named effects for operation_log; no entry-point/module access."""
    local_now: Callable[..., object]


def operation_log(effects: OperationLogEffects, message: str, *, error: bool = False,
                  level: str | None = None, **fields) -> None:
    """공통 문맥·비밀값 제거·한 줄 출력을 적용하며 출력 실패는 격리한다."""
    try:
        context = _context.get()
        level = level or (context.get("error_level", "ERROR") if error else "INFO")
        prefix = effects.local_now().astimezone().isoformat(sep=" ", timespec="seconds")
        identity = " ".join(f"{key}={_value(context.get(key), key)}" for key in ("run", "project", "plan", "stage"))
        text = redact_secrets(str(message)).replace("\\", "\\\\").replace("\r", r"\r").replace("\n", r"\n").replace("\t", r"\t").replace("\x1b", r"\x1b")
        details = redact_secrets("".join(f"; {key}={_value(value, key)}" for key, value in fields.items()))
        output = sys.stderr if level != "INFO" or context.get("result_output") else sys.stdout
        with _output_lock:
            __import__("builtins").print(f"{prefix} {level} [{identity}] {text}{details}", file=output, flush=True)
    except Exception:
        pass


@dataclass(frozen=True)
class OperationPrintEffects:
    """Named effects for operation_print; no entry-point/module access."""
    operation_log: Callable[..., object]


def operation_print(effects: OperationPrintEffects, *values: object, **kwargs: object) -> None:
    """Route legacy operational output through the shared timestamped boundary."""

    output = kwargs.get("file", sys.stdout)
    separator = str(kwargs.get("sep", " "))
    level = kwargs.get("level")
    effects.operation_log(
        separator.join(str(value) for value in values),
        error=output is sys.stderr or bool(kwargs.get("error")),
        **({"level": level} if level is not None else {}),
    )
