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


def _value(value) -> str:
    text = redact_secrets(str(value if value is not None else "-"))
    return json.dumps(text, ensure_ascii=False) if not text or any(
        char.isspace() or char in '"[]=;' for char in text
    ) else text


def emit(message: str, *, level="INFO", **fields) -> None:
    identity = {key: fields.pop(key) for key in ("run", "project", "plan", "stage") if key in fields}
    with log_context(**identity):
        operation_log(OperationLogEffects(local_now), message, level=level, **fields)


@contextmanager
def progress(label: str, *, describe=None, interval=None):
    """관측만 복사한 스레드에서 상태를 출력하며 SQLite·실행 상태는 건드리지 않는다."""
    if _context.get().get("progress_active"):
        yield
        return
    stopped, started = threading.Event(), time.monotonic()
    interval = PROGRESS_INTERVAL_SECONDS if interval is None else interval
    def report():
        while not stopped.wait(interval):
            try:
                try:
                    fields = describe() if describe else {}
                except Exception:
                    fields = {"progress_observation": "미확인"}
                if stopped.is_set():
                    break
                emit(f"{label} 진행 중", elapsed_seconds=round(time.monotonic() - started, 1),
                     **fields)
            except Exception:
                pass  # 선택적 관측 실패로 실행 결과를 바꾸지 않는다.
    token = _context.set({**_context.get(), "progress_active": True})
    thread = threading.Thread(target=copy_context().run, args=(report,), daemon=True)
    try:
        thread.start()
    except (OSError, RuntimeError):
        _context.reset(token)
        yield
        return
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
        _context.reset(token)


@contextmanager
def activity(label: str, *, heartbeat=False, **fields):
    """처리 경계의 시작·결과를 기록한다. 성공 의미는 호출자가 관측 결과로 채운다."""
    from contextlib import nullcontext
    started, result = time.monotonic(), {}
    emit(f"{label} 시작", **fields)
    try:
        with progress(label) if heartbeat else nullcontext():
            yield result
    except BaseException as error:
        handoff = isinstance(error, SelfMergeRuntimeHandoff)
        emit(f"{label} 종료", level="INFO" if handoff else "WARN",
             result="runtime_handoff" if handoff else "미확인" if not isinstance(error, Exception) else "실패",
             error_type=type(error).__name__, elapsed_seconds=round(time.monotonic() - started, 1))
        raise
    else:
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
        identity = " ".join(f"{key}={_value(context.get(key))}" for key in ("run", "project", "plan", "stage"))
        text = redact_secrets(str(message)).replace("\\", "\\\\").replace("\r", r"\r").replace("\n", r"\n").replace("\t", r"\t").replace("\x1b", r"\x1b")
        details = redact_secrets("".join(f"; {key}={_value(value)}" for key, value in fields.items()))
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
    effects.operation_log(separator.join(str(value) for value in values),
                          error=output is sys.stderr or bool(kwargs.get("error")))
