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
    concurrent: bool = False


@dataclass(frozen=True)
class RuntimeMutation:
    source: Path
    path: Path
    commit_sha: str
    mutation_id: str | None
    legacy: bool = False
    registration_lock_reason: str | None = None
    concurrent: bool = False


class GitLockError(RuntimeError):
    """A concurrent Git process owns a repository lock."""


class WorkspaceOwnershipError(RuntimeError):
    """A managed worktree cannot be proven to belong to its saved task."""


class ProcessLockBusyError(RuntimeError):
    """An automatic invocation or concrete resource lease is already held."""


class PublicationSafetyError(RuntimeError):
    """Publication cannot prove the selected branch and workspace ownership."""


class ManagedWorktreeConflictError(PublicationSafetyError):
    """A managed Git update would overwrite preserved local artifacts."""


PROJECT_STEP_RETRY = 2
PROJECT_STEP_PLANNING_PENDING = 5
WORKSPACE_UNOWNED = 0
WORKSPACE_OWNED = 1
WORKSPACE_RESERVED = 2
REBASE_IN_PROGRESS_ERROR = "Manager rebase 진행 중"
REBASE_CONFLICT_PREFIX = "rebase 충돌 해결 필요:\n"
PR_CONFLICT_PREFLIGHT_BLOCK_REASON = "pr-conflict-preflight"
PR_CONFLICT_PREFLIGHT_ERROR_PREFIX = "PR_OPEN 충돌 복구 소유권 검증 실패:"
