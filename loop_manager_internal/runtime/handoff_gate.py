"""runtime handoff gate boundaries and named composition effects."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass, field
from collections.abc import Callable
from functools import wraps
from .contracts import RuntimeRefresh
import subprocess
import threading
import time

_pause_check_active = ContextVar('runtime_pause_check_active', default=False)


class SelfMergeRuntimeHandoff(BaseException):
    """A verified remote code change requires a drained runtime replacement."""

    def __init__(self, request: RuntimeRefresh | None = None):
        self.request = request
        super().__init__()


@dataclass
class RuntimeHandoffGate:
    """Serialize a handoff decision with the instant a subprocess starts."""

    event: threading.Event
    launch_lock: threading.Lock
    observer: Callable[[], None] | None = None
    request: RuntimeRefresh | None = None
    observation_lock: threading.Lock = field(default_factory=threading.Lock)
    observed_at: float | None = None
    pause_check: Callable[[], None] | None = None
    before_observe: Callable[[], None] | None = None

    def check(self) -> None:
        if self.pause_check is not None and not _pause_check_active.get():
            token = _pause_check_active.set(True)
            try:
                self.pause_check()
            finally:
                _pause_check_active.reset(token)
        if self.event.is_set():
            raise SelfMergeRuntimeHandoff(self.request)

    def stop(self, request: RuntimeRefresh | None = None) -> None:
        with self.launch_lock:
            if not self.event.is_set():
                self.request = request
            self.event.set()

    def observe(self) -> None:
        self.check()
        if self.observer is None:
            return
        if self.before_observe is not None:
            self.before_observe()
        with self.observation_lock:
            self.check()
            if self.observed_at is None or time.monotonic() - self.observed_at >= 30:
                self.observer()
                self.observed_at = time.monotonic()
        self.check()

    def popen(self, *args: object, **kwargs: object) -> subprocess.Popen:
        with self.launch_lock:
            self.check()
            return subprocess.Popen(*args, **kwargs)


def _check_runtime_handoff() -> None:
    gate = _runtime_handoff_signal.get()
    if gate is not None:
        gate.check()


def observe_runtime() -> None:
    """External observation belongs at stage boundaries, outside state/launch locks."""
    gate = _runtime_handoff_signal.get()
    if gate is not None:
        gate.observe()


def runtime_boundary(function):
    @wraps(function)
    def call(*args, **kwargs):
        observe_runtime()
        return function(*args, **kwargs)
    return call


def _handoff_popen(*args: object, **kwargs: object) -> subprocess.Popen:
    gate = _runtime_handoff_signal.get()
    return (
        gate.popen(*args, **kwargs) if gate is not None
        else subprocess.Popen(*args, **kwargs)
    )


def _handoff_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
    """Keep the spawn atomic with handoff without holding the lock while waiting."""

    gate = _runtime_handoff_signal.get()
    if gate is None:
        return subprocess.run(*args, **kwargs)
    options = dict(kwargs)
    input_data = options.pop("input", None)
    timeout = options.pop("timeout", None)
    if input_data is not None:
        options["stdin"] = subprocess.PIPE
    with gate.popen(*args, **options) as process:
        try:
            stdout, stderr = process.communicate(input_data, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(error.cmd, error.timeout, stdout, stderr) from error
        except BaseException:
            # Match subprocess.run: stop the child before the context manager
            # closes its pipes and waits, including on KeyboardInterrupt.
            process.kill()
            raise
    return subprocess.CompletedProcess(args[0], process.returncode, stdout, stderr)


_runtime_handoff_signal: ContextVar[RuntimeHandoffGate | None] = ContextVar(
    "runtime_handoff_signal", default=None,
)
