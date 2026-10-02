"""runtime handoff gate boundaries and named composition effects."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
import subprocess
import threading


class SelfMergeRuntimeHandoff(BaseException):
    """A durable self PR merge requires the next external bootstrap revision."""


@dataclass(frozen=True)
class RuntimeHandoffGate:
    """Serialize a handoff decision with the instant a subprocess starts."""

    event: threading.Event
    launch_lock: threading.Lock

    def check(self) -> None:
        if self.event.is_set():
            raise SelfMergeRuntimeHandoff()

    def stop(self) -> None:
        with self.launch_lock:
            self.event.set()

    def popen(self, *args: object, **kwargs: object) -> subprocess.Popen:
        with self.launch_lock:
            self.check()
            return subprocess.Popen(*args, **kwargs)


def _check_runtime_handoff() -> None:
    gate = _runtime_handoff_signal.get()
    if gate is not None:
        gate.check()


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
