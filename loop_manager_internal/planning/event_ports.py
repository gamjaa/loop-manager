"""planning event ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class PlanningEventPortsEffects:
    """Named effects for _PlanningEventPorts; no entry-point/module access."""
    operation_log: Callable[..., object]
    operation_print: Callable[..., object]


class _PlanningEventPorts:
    def log(self, message: str) -> None:
        self._effects.operation_log(message)

    def info(self, message: str) -> None:
        self._effects.operation_print(message)

    def error(self, message: str) -> None:
        self._effects.operation_print(message, file=sys.stderr)

    def __init__(self, effects: PlanningEventPortsEffects):
        self._effects = effects
