"""Immutable bootstrap and workspace identities shared by runtime adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BaseWorktreeSyncResult:
    """A best-effort primary-worktree update that never gates remote work."""

    status: str
    reason: str
    remote_sha: str


@dataclass(frozen=True)
class BaseWorktreeSnapshot:
    branch: str | None
    head: str
    index: tuple[tuple[str, tuple[str, ...]], ...]
    changes: tuple[tuple[str, str, str], ...]
    operations: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RuntimeWorktree:
    """The immutable Loop Manager revision selected by the source bootstrap."""

    path: Path
    commit_sha: str
    mutation_id: str | None = None


@dataclass(frozen=True)
class BootstrapRefreshHandoff:
    base_branch: str
    commit_sha: str


@dataclass(frozen=True)
class RuntimeMutation:
    source: Path
    path: Path
    commit_sha: str
    mutation_id: str | None
    legacy: bool = False
    registration_lock_reason: str | None = None


class GitLockError(RuntimeError):
    """A concurrent Git process owns a repository lock."""


class WorkspaceOwnershipError(RuntimeError):
    """A managed worktree cannot be proven to belong to its saved task."""


class ProcessLockBusyError(RuntimeError):
    """The single Loop Manager process lock is already held."""
