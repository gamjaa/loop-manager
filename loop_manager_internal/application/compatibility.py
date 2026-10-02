"""Preserve public signatures while binding only named, lazy effects."""
from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import signature
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


def bind_effects(implementation: Callable[..., R], effects: Callable[[], object]) -> Callable[P, R]:
    """Build a compatibility call without passing or importing the root module."""
    @wraps(implementation)
    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        return implementation(effects(), *args, **kwargs)

    original = signature(implementation)
    call.__signature__ = original.replace(parameters=tuple(original.parameters.values())[1:])
    injected_name = next(iter(original.parameters))
    call.__annotations__ = {name: annotation for name, annotation in call.__annotations__.items()
                            if name != injected_name}
    call.__module__ = "loop_manager"
    return call
