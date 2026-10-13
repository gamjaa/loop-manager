"""Assemble named effects without depending on the CLI module."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from loop_manager_internal.application.compatibility import bind_effects


class Composition:
    """Assembly-only references; services receive their own typed effect contracts."""

    def __init__(self, values: Mapping[str, object], *,
                 override_lookup: Callable[[str, object], object] | None = None) -> None:
        self._values = dict(values)
        self._override_lookup = override_lookup
        self._factories: dict[str, Callable[[], object]] = {}
        self._dependencies: set[str] = set()
        self._callbacks: set[str] = set()
        self._registered: set[str] = set()
        self._sealed = False

    @property
    def exports(self) -> dict[str, object]:
        return dict(self._values)

    def value(self, name: str) -> object:
        self._dependencies.add(name)
        try:
            value = self._values[name]
        except KeyError as error:
            raise ValueError(f"등록되지 않은 의존성: {name}") from error
        if self._override_lookup is not None:
            return self._override_lookup(name, value)
        return value

    def callback(self, name: str) -> Callable[..., object]:
        self._dependencies.add(name)
        self._callbacks.add(name)

        def invoke(*args, **kwargs):
            return self.value(name)(*args, **kwargs)

        return invoke

    def add(self, name: str, value: object) -> None:
        if self._sealed:
            raise ValueError("완료된 composition에는 등록할 수 없습니다.")
        if name in self._registered:
            raise ValueError(f"중복 의존성 등록: {name}")
        self._registered.add(name)
        self._values[name] = value

    def bind(self, name: str, implementation: Callable, effects: Callable[[], object]) -> None:
        self.add(name, bind_effects(implementation, effects))
        self._factories[name] = effects

    def factory(self, name: str, effects: Callable[[], object]) -> None:
        # These two legacy factories were module-level lambdas in loop_manager.
        effects.__module__ = "loop_manager"
        self.add(name, effects)
        self._factories[name] = effects

    def override(self, values: Mapping[str, object]) -> None:
        if self._sealed:
            raise ValueError("완료된 composition에는 override할 수 없습니다.")
        unknown = values.keys() - self._values.keys()
        if unknown:
            raise ValueError(f"등록되지 않은 override: {', '.join(sorted(unknown))}")
        self._values.update(values)

    def validate(self) -> None:
        # Factories only construct effect contracts. Callbacks remain lazy, so
        # registration order and reciprocal service references cause no effects.
        for factory in self._factories.values():
            factory()
        missing = self._dependencies - self._values.keys()
        if missing:
            raise ValueError(f"등록되지 않은 의존성: {', '.join(sorted(missing))}")
        invalid = {name for name in self._callbacks if not callable(self.value(name))}
        if invalid:
            raise ValueError(f"호출할 수 없는 의존성: {', '.join(sorted(invalid))}")
        self._sealed = True


def create_application(
    root: Path, *, overrides: Mapping[str, object] | None = None,
    override_lookup: Callable[[str, object], object] | None = None,
    registrars: Sequence[Callable[[Composition], None]] | None = None,
) -> Composition:
    """Build an independent graph; imports and assembly open no resources."""
    from loop_manager_internal.application.legacy_exports import VALUES
    from loop_manager_internal.agents.bindings import register as agents
    from loop_manager_internal.application.bindings import register as application
    from loop_manager_internal.catalog.bindings import register as catalog
    from loop_manager_internal.documents.bindings import register as documents
    from loop_manager_internal.execution.bindings import register as execution
    from loop_manager_internal.git.bindings import register as git
    from loop_manager_internal.maintenance.bindings import register as maintenance
    from loop_manager_internal.planning.bindings import register as planning
    from loop_manager_internal.recovery.bindings import register as recovery
    from loop_manager_internal.runtime.bindings import register as runtime
    from loop_manager_internal.storage.bindings import register as storage

    assembly = Composition({**VALUES, "ROOT": root}, override_lookup=override_lookup)
    for register in (agents, application, catalog, documents, execution, git,
                     maintenance, planning, recovery, runtime, storage):
        register(assembly)
    for register in registrars or ():
        register(assembly)
    if overrides:
        assembly.override(overrides)
    assembly.validate()
    return assembly
