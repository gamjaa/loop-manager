"""Immutable Git scope observations and the scope failure contract."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode


@dataclass(frozen=True)
class GitSnapshot:
    """Content-free Git state captured across one Agent invocation."""

    head: str
    branch: str | None
    index: dict[str, tuple[str, ...]]
    dirty: dict[str, tuple[tuple[str, ...], str]]
    refs: dict[str, tuple[str, str]]
    worktrees: dict[str, tuple[str, ...]]
    config: dict[str, tuple[str, ...]]
    operations: dict[str, str]
    shared_metadata_complete: bool = True
    scope_id: str | None = field(default=None, compare=False)
    scope_start_digest: str | None = field(default=None, compare=False)

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": 5 if self.scope_id else 4,
                **({"scope_id": self.scope_id, "scope_start_digest": self.scope_start_digest}
                   if self.scope_id else {}),
                "head": self.head,
                "branch": self.branch,
                "index": {path: list(entries) for path, entries in self.index.items()},
                "dirty": {
                    path: [list(kinds), signature]
                    for path, (kinds, signature) in self.dirty.items()
                },
                "refs": {name: list(state) for name, state in self.refs.items()},
                "worktrees": {
                    path: list(state) for path, state in self.worktrees.items()
                },
                "config": {
                    key: list(values) for key, values in self.config.items()
                },
                "operations": self.operations,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def deserialize(cls, value: str) -> GitSnapshot:
        parsed = json.loads(value)
        version = parsed.get("version")
        if version not in {1, 2, 3, 4, 5}:
            raise RuntimeError("저장된 Agent Git snapshot 형식을 지원하지 않습니다.")
        return cls(
            head=str(parsed["head"]),
            branch=(str(parsed["branch"]) if parsed["branch"] is not None else None),
            index={
                str(path): tuple(str(entry) for entry in entries)
                for path, entries in parsed["index"].items()
            },
            dirty={
                str(path): (
                    tuple(str(kind) for kind in state[0]),
                    str(state[1]),
                )
                for path, state in parsed["dirty"].items()
            },
            refs={
                str(name): (str(state[0]), str(state[1]))
                for name, state in parsed.get("refs", {}).items()
            },
            worktrees={
                str(path): tuple(str(entry) for entry in state)
                for path, state in parsed.get("worktrees", {}).items()
            },
            config={
                str(key): tuple(str(value) for value in values)
                for key, values in parsed.get("config", {}).items()
            },
            operations={
                str(name): str(signature)
                for name, signature in parsed.get("operations", {}).items()
            },
            shared_metadata_complete=version in {4, 5},
            scope_id=parsed.get("scope_id") if version == 5 else None,
            scope_start_digest=parsed.get("scope_start_digest") if version == 5 else None,
        )


@dataclass(frozen=True)
class ManagerGitCheckpoint:
    """Validated Agent state before Manager-owned Git mutations."""

    snapshot: GitSnapshot
    cleanup_worktrees: frozenset[str]

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "snapshot": self.snapshot.serialize(),
                "cleanup_worktrees": sorted(self.cleanup_worktrees),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def deserialize(cls, value: str) -> ManagerGitCheckpoint:
        parsed = json.loads(value)
        if parsed.get("version") != 1:
            raise RuntimeError("저장된 Manager Git checkpoint 형식을 지원하지 않습니다.")
        return cls(
            snapshot=GitSnapshot.deserialize(str(parsed["snapshot"])),
            cleanup_worktrees=frozenset(
                str(path) for path in parsed.get("cleanup_worktrees", [])
            ),
        )


@dataclass(frozen=True)
class AgentChange:
    path: str
    kinds: tuple[str, ...]


class AgentScopeError(RuntimeError):
    """An Agent changed Git state outside its role boundary."""

    def __init__(
        self,
        message: str,
        result_snapshot: GitSnapshot | None = None,
        reason_code: BlockReasonCode = (
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
        ),
    ) -> None:
        super().__init__(message)
        self.result_snapshot = result_snapshot
        self.reason_code = reason_code
