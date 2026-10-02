"""runtime operation events boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import sys


def local_now() -> datetime:
    """Return the local clock through a patchable boundary for operation logs."""

    return datetime.now()


@dataclass(frozen=True)
class OperationLogEffects:
    """Named effects for operation_log; no entry-point/module access."""
    local_now: Callable[..., object]


def operation_log(effects: OperationLogEffects, message: str, *, error: bool = False) -> None:
    """Write timestamped operational output without adding persistent state."""

    prefix = effects.local_now().strftime("%Y-%m-%d %H:%M:%S")
    output = sys.stderr if error else sys.stdout
    for line in str(message).splitlines() or [""]:
        __import__("builtins").print(f"{prefix} {line}", file=output)


@dataclass(frozen=True)
class OperationPrintEffects:
    """Named effects for operation_print; no entry-point/module access."""
    operation_log: Callable[..., object]


def operation_print(effects: OperationPrintEffects, *values: object, **kwargs: object) -> None:
    """Route legacy operational output through the shared timestamped boundary."""

    output = kwargs.get("file", sys.stdout)
    separator = str(kwargs.get("sep", " "))
    effects.operation_log(separator.join(str(value) for value in values), error=output is sys.stderr)
