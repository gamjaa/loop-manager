#!/usr/bin/env python3
"""AI Agent가 Loop Manager의 다음 PR을 만들게 하는 부트스트랩 실행기."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
import configparser
import contextlib
import difflib
import errno
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Protocol
from urllib.parse import quote

from guidance_approval import (
    GuidanceDocument,
    GuidanceFormatError,
    GuidanceProposal,
    apply_patch_to_content,
    contains_guidance_sections,
    content_hash as guidance_content_hash,
    finalized_patch,
    guidance_patch_hash,
    parse_guidance_document,
)
from managed_config import ManagedConfig, ManagedConfigError, load_managed_config
import notifications
import project_registry


ROOT = Path(__file__).resolve().parent
MAX_PLANNER_VALIDATION_RETRIES = 3
MAX_VALIDATION_FINGERPRINT_REPEATS = 3
MAX_TRANSIENT_AGENT_RETRIES = 3
AGENT_CLI_TIMEOUT_SECONDS = 3600
PROJECT_STEP_RETRY = 2
PROJECT_STEP_TRANSIENT_PAUSE = 3
PROJECT_STEP_USAGE_PAUSE = 4
PROJECT_STEP_PLANNING_PENDING = 5
TRANSIENT_AGENT_ERROR_PREFIX = "일시적 Agent 실패 (상태 보존):"
USAGE_LIMIT_ERROR_PREFIX = "AI 사용 한도 소진 (상태 보존):"
AGENT_PROVIDERS = frozenset({"codex", "claude"})
CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
PLAN_SHA_BACKFILL_ERROR = "계획 commit SHA를 기존 작업에서 안전하게 복구할 수 없습니다."
WORKSPACE_UNOWNED = 0
WORKSPACE_OWNED = 1
WORKSPACE_RESERVED = 2
REBASE_IN_PROGRESS_ERROR = "Manager rebase 진행 중"
REBASE_CONFLICT_PREFIX = "rebase 충돌 해결 필요:\n"
PR_CLOSED_BLOCK_REASON = "pull-request-closed"
PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}
ACTIVE_EXECPLAN_STATUSES = frozenset(
    {"IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING", "PR_OPEN", "BLOCKED"}
)
KNOWN_EXECPLAN_STATUSES = ACTIVE_EXECPLAN_STATUSES | {"READY", "MERGED"}
FIELDS = {
    "base_branch",
    "provider",
    "processed_sha",
    "planning_fingerprint",
    "planning_attempt_fingerprint",
    "planning_workspace_owned",
    "planning_retry_count",
    "planner_session_id",
    "worker_session_id",
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
    "planning_validation_error",
    "planning_result_pending_validation",
    "planning_result_completed",
    "planning_error",
    "planning_error_fingerprint",
    "active_sha",
    "validated_base_sha",
    "publication_remote_oid",
    "pr_conflict_recovery",
    "pr_conflict_head_oid",
    "pr_conflict_base_oid",
    "plan_sha",
    "phase",
    "branch",
    "worktree",
    "plan_path",
    "pr_url",
    "pr_head_oid",
    "last_error",
    "agent_role",
    "agent_snapshot",
    "agent_input_fingerprint",
    "agent_baseline",
    "agent_recovery",
    "agent_input_plan",
    "agent_result_plan",
    "agent_result_snapshot",
    "agent_manager_snapshot",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    repo TEXT PRIMARY KEY,
    base_branch TEXT NOT NULL,
    processed_sha TEXT,
    provider TEXT NOT NULL DEFAULT 'codex',
    origin TEXT,
    registered INTEGER NOT NULL DEFAULT 1,
    agents_hash TEXT,
    agents_approved INTEGER NOT NULL DEFAULT 0,
    planning_fingerprint TEXT,
    planning_attempt_fingerprint TEXT,
    planning_workspace_owned INTEGER NOT NULL DEFAULT 0,
    planning_retry_count INTEGER NOT NULL DEFAULT 0,
    planner_session_id TEXT,
    worker_session_id TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    planning_validation_error TEXT,
    planning_result_pending_validation INTEGER NOT NULL DEFAULT 0,
    planning_result_completed INTEGER NOT NULL DEFAULT 0,
    planning_error TEXT,
    planning_error_fingerprint TEXT,
    active_sha TEXT,
    validated_base_sha TEXT,
    publication_remote_oid TEXT,
    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,
    pr_conflict_head_oid TEXT,
    pr_conflict_base_oid TEXT,
    plan_sha TEXT,
    phase TEXT,
    branch TEXT,
    worktree TEXT,
    plan_path TEXT,
    pr_url TEXT,
    pr_head_oid TEXT,
    last_error TEXT,
    agent_role TEXT,
    agent_snapshot TEXT,
    agent_input_fingerprint TEXT,
    agent_baseline TEXT,
    agent_recovery TEXT,
    agent_input_plan TEXT,
    agent_result_plan TEXT,
    agent_result_snapshot TEXT,
    agent_manager_snapshot TEXT
)
"""

CLEANUP_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup_pending (
    repo TEXT NOT NULL,
    worktree TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    pr_head_oid TEXT,
    reason TEXT NOT NULL,
    workspace_owned INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, worktree),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

EXEC_PLANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_plans (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    sources TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    priority TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_sha TEXT,
    active_sha TEXT,
    validated_base_sha TEXT,
    publication_remote_oid TEXT,
    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,
    pr_conflict_head_oid TEXT,
    pr_conflict_base_oid TEXT,
    branch TEXT,
    worktree TEXT,
    pr_url TEXT,
    pr_head_oid TEXT,
    last_error TEXT,
    blocked_fingerprint TEXT,
    blocked_worktree_fingerprint TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    worker_session_id TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    verify_failure_history TEXT NOT NULL DEFAULT '{}',
    review_failure_history TEXT NOT NULL DEFAULT '{}',
    initial_worker_level INTEGER,
    effective_worker_level INTEGER,
    review_level_history TEXT NOT NULL DEFAULT '{}',
    blocked_reason TEXT,
    guidance_plan_hash TEXT,
    workspace_owned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, plan_id),
    UNIQUE (repo, path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

GUIDANCE_APPROVALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS guidance_approvals (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    target_path TEXT NOT NULL,
    scope TEXT NOT NULL,
    proposal_patch TEXT NOT NULL,
    proposal_patch_hash TEXT NOT NULL,
    proposal_line INTEGER NOT NULL,
    decision_line INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    decision_revision TEXT NOT NULL DEFAULT '',
    decision_hash TEXT,
    approved_patch TEXT,
    approved_patch_hash TEXT,
    baseline_content BLOB NOT NULL,
    baseline_mode INTEGER NOT NULL,
    expected_content_hash TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    invalidated_decision_hash TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id, proposal_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""

EXEC_PLANS_ACTIVE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execplan_per_project
ON exec_plans(repo)
WHERE status IN ('IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING', 'PR_OPEN', 'BLOCKED')
"""


class AgentResultKind(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient_failure"
    USAGE_LIMIT = "usage_limit"
    PERMANENT_CONFIGURATION = "permanent_configuration"


class ProjectRunKind(str, Enum):
    COMPLETED = "completed"
    WAITING = "waiting"
    BLOCKED = "blocked"
    TRANSIENT_PAUSE = "transient_pause"
    USAGE_LIMIT_PAUSE = "usage_limit_pause"
    CONFIGURATION_ERROR = "configuration_error"
    OPERATIONAL_ERROR = "operational_error"


@dataclass(frozen=True)
class ProjectRunResult:
    kind: ProjectRunKind
    exit_code: int
    detail: str | None = None


@dataclass(frozen=True)
class ScheduledProjectResult:
    repo: str
    result: ProjectRunResult


@dataclass(frozen=True)
class CatalogScheduleDecision:
    active: sqlite3.Row | None
    has_ready: bool
    runnable_ready: sqlite3.Row | None


@dataclass(frozen=True)
class AgentResult:
    code: int
    message: str
    session_id: str | None = None
    kind: AgentResultKind | None = None

    def __iter__(self):
        # Keep two-value unpacking compatible with callers that do not manage sessions.
        return iter((self.code, self.message))

    @property
    def result_kind(self) -> AgentResultKind:
        return self.kind or classify_agent_result(self.code, self.message)


@dataclass(frozen=True)
class AgentProfile:
    model: str
    effort: str


@dataclass(frozen=True)
class ProjectRegistrationInspection:
    root: Path
    base_branch: str
    provider: str
    agents_hash: str
    agents_approved: bool
    origin: str | None = None


@dataclass(frozen=True)
class BaseWorktreeSyncResult:
    """A best-effort primary-worktree update that never gates remote work."""

    status: str
    reason: str
    remote_sha: str


@dataclass(frozen=True)
class RuntimeWorktree:
    """The immutable Loop Manager revision selected by the source bootstrap."""

    path: Path
    commit_sha: str


class GitLockError(RuntimeError):
    """A concurrent Git process owns a repository lock."""


@dataclass(frozen=True)
class AgentRequest:
    worktree: Path
    state_dir: Path
    config_path: Path
    role_name: str
    prompt: str
    read_only: bool
    level: int | None = None
    persist_session: bool = False
    session_id: str | None = None
    on_session_started: Callable[[str], None] | None = None


# Public compatibility name for integrations written before provider-neutral
# execution was introduced.
CodexResult = AgentResult


class AgentProvider(Protocol):
    """Provider boundary consumed by orchestration state transitions."""

    name: str

    def run(self, request: AgentRequest) -> tuple[int, str] | AgentResult:
        ...

@dataclass(frozen=True)
class ValidationFailureDecision:
    stage: str
    fingerprint: str
    occurrences: int
    blocked: bool


@dataclass(frozen=True)
class WorkerLevelDecision:
    previous_level: int
    effective_level: int
    review_history: dict[str, dict[str, int]]

    @property
    def escalated(self) -> bool:
        return self.effective_level > self.previous_level


@dataclass(frozen=True)
class CleanupAssessment:
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class WorkerPublication:
    """The Worker-provided metadata used for its commit and pull request."""

    commit_title: str
    summary: str
    test_evidence: str


class RequirementArchiveError(RuntimeError):
    """Requirement archive eligibility or movement cannot be proven safe."""


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

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": 4,
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
        if version not in {1, 2, 3, 4}:
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
            shared_metadata_complete=version == 4,
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
    ) -> None:
        super().__init__(message)
        self.result_snapshot = result_snapshot


class WorkspaceOwnershipError(RuntimeError):
    """A managed worktree cannot be proven to belong to its saved task."""


class WorkerResultError(RuntimeError):
    """A Worker final response cannot safely be published."""


class ReviewerProtocolError(RuntimeError):
    """A failed Reviewer response does not identify actionable issues."""


class PublicationSafetyError(RuntimeError):
    """A remote publication branch cannot be proven safe to update."""


class ProcessLockBusyError(RuntimeError):
    """The single Loop Manager process lock is already held."""


def classify_agent_result(code: int, message: str) -> AgentResultKind:
    """Classify an Agent CLI result without another Agent call."""

    if code == 0:
        return AgentResultKind.SUCCESS
    normalized = " ".join(message.casefold().split())
    if any(
        phrase in normalized
        for phrase in (
            "usage limit",
            "usage cap",
            "quota exceeded",
            "insufficient_quota",
            "credit balance",
            "usage limit reached",
            "hit your limit",
            "out of extra usage",
        )
    ):
        return AgentResultKind.USAGE_LIMIT
    if any(
        phrase in normalized
        for phrase in (
            "timed out",
            "timeout",
            "overloaded",
            "rate limit exceeded",
            "temporarily unavailable",
            "service unavailable",
            "connection reset",
            "connection refused",
            "network error",
            "dns error",
            "gateway timeout",
            "internal server error",
            "try again later",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "too many requests",
        )
    ):
        return AgentResultKind.TRANSIENT_FAILURE
    return AgentResultKind.PERMANENT_CONFIGURATION


def session_resume_unavailable(message: str) -> bool:
    normalized = " ".join(message.casefold().split())
    return bool(
        re.search(
            r"(?:session|thread|rollout).{0,80}(?:not found|does not exist|expired)",
            normalized,
        )
    ) or any(
        phrase in normalized
        for phrase in (
            "unknown session",
            "unknown thread",
            "cannot resume",
            "could not resume",
            "unable to resume",
        )
    )


def _coerce_agent_result(
    result: tuple[int, str] | AgentResult,
    session_id: str | None,
) -> AgentResult:
    if isinstance(result, AgentResult):
        return result
    code, message = result
    return AgentResult(code, message, session_id)


def run_agent_with_retries(
    invoke: Callable[[str | None, bool], tuple[int, str] | AgentResult],
    *,
    session_id: str | None,
) -> AgentResult:
    """Retry transient CLI failures and replace an unavailable saved session once."""

    active_session_id = session_id
    transient_retries = 0
    session_fallback = False
    while True:
        try:
            raw_result = invoke(active_session_id, session_fallback)
            result = _coerce_agent_result(raw_result, active_session_id)
        except subprocess.TimeoutExpired as error:
            result = AgentResult(
                1,
                f"Agent CLI timeout: {error}",
                active_session_id,
                AgentResultKind.TRANSIENT_FAILURE,
            )
        except OSError as error:
            result = AgentResult(
                1,
                f"Agent CLI 실행 실패: {error}",
                active_session_id,
                AgentResultKind.TRANSIENT_FAILURE,
            )

        if result.session_id:
            active_session_id = result.session_id
        if result.result_kind is AgentResultKind.SUCCESS:
            return result
        if (
            result.result_kind is not AgentResultKind.USAGE_LIMIT
            and active_session_id is not None
            and not session_fallback
            and session_resume_unavailable(result.message)
        ):
            active_session_id = None
            session_fallback = True
            continue
        if (
            result.result_kind is AgentResultKind.TRANSIENT_FAILURE
            and transient_retries < MAX_TRANSIENT_AGENT_RETRIES
        ):
            transient_retries += 1
            continue
        return result


def local_now() -> datetime:
    """Return the local clock through a patchable boundary for operation logs."""

    return datetime.now()


def operation_log(message: str, *, error: bool = False) -> None:
    """Write timestamped operational output without adding persistent state."""

    prefix = local_now().strftime("%Y-%m-%d %H:%M:%S")
    output = sys.stderr if error else sys.stdout
    for line in str(message).splitlines() or [""]:
        __import__("builtins").print(f"{prefix} {line}", file=output)


def operation_print(*values: object, **kwargs: object) -> None:
    """Route legacy operational output through the shared timestamped boundary."""

    output = kwargs.get("file", sys.stdout)
    separator = str(kwargs.get("sep", " "))
    operation_log(separator.join(str(value) for value in values), error=output is sys.stderr)

def state_directory(value: str | None = None) -> Path:

    if value:
        return Path(value).expanduser().resolve()
    if configured := os.environ.get("LOOP_MANAGER_STATE_DIR"):
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        parent = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return parent / "loop-manager"


def is_lock_contention(error: OSError) -> bool:
    """Return whether a non-blocking lock failure means another process holds it."""

    return error.errno in {errno.EACCES, errno.EAGAIN} or (
        os.name == "nt" and error.winerror in {32, 33}
    )


@contextlib.contextmanager
def process_lock(state_dir: Path):
    # ponytail: 부트스트랩은 전역 직렬화한다. 실제 병렬 처리 도입 시 프로젝트별 잠금으로 교체한다.
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "loop-manager.lock"
    handle = lock_path.open("a+b")
    handle.seek(0)
    if handle.read(1) == b"":
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        if is_lock_contention(error):
            raise ProcessLockBusyError("다른 Loop Manager 프로세스가 실행 중입니다.") from error
        raise
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def runtime_repository_id(source: Path) -> str:
    return hashlib.sha256(str(_canonical_path(source)).encode()).hexdigest()[:16]


def expected_runtime_worktree(
    state_dir: Path,
    source: Path,
    commit_sha: str,
) -> Path:
    return state_dir / "runtimes" / runtime_repository_id(source) / commit_sha


def validate_runtime_worktree(
    source: Path,
    state_dir: Path,
    runtime: Path,
    commit_sha: str,
) -> None:
    """Prove that an existing runtime is the clean detached worktree we own."""

    expected = expected_runtime_worktree(state_dir, source, commit_sha)
    _validate_managed_path_boundary(state_dir, runtime, expected, "runtime worktree")
    validate_managed_worktree(source, runtime, None)
    head = git(runtime, "rev-parse", "HEAD").stdout.strip()
    if head != commit_sha:
        raise WorkspaceOwnershipError(
            f"runtime HEAD가 선택한 origin/main과 다릅니다: {head} "
            f"(expected {commit_sha})"
        )
    status = git(
        runtime,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout
    if status.strip():
        raise WorkspaceOwnershipError(
            f"기존 runtime worktree가 clean 상태가 아닙니다: {runtime}"
        )
    operations = _git_operation_entries(runtime)
    if operations:
        raise WorkspaceOwnershipError(
            "기존 runtime worktree에 Git operation이 진행 중입니다: "
            + ", ".join(sorted(operations))
        )


def prepare_runtime_worktree(source: Path, state_dir: Path) -> RuntimeWorktree:
    """Fetch origin/main and select an immutable linked runtime worktree."""

    source = _canonical_path(source)
    root = _canonical_path(
        Path(git(source, "rev-parse", "--show-toplevel").stdout.strip())
    )
    if root != source:
        raise RuntimeError(f"Loop Manager source 저장소 루트가 아닙니다: {source}")
    github_repository(git(source, "remote", "get-url", "origin").stdout)
    git(source, "fetch", "origin")
    commit_sha = git(
        source,
        "rev-parse",
        "--verify",
        "refs/remotes/origin/main^{commit}",
    ).stdout.strip()
    runtime = expected_runtime_worktree(state_dir, source, commit_sha)
    _validate_managed_path_boundary(
        state_dir,
        runtime,
        runtime,
        "runtime worktree",
    )
    try:
        _canonical_path(runtime).relative_to(source)
    except ValueError:
        pass
    else:
        raise WorkspaceOwnershipError(
            f"runtime 경로가 source checkout 내부에 있습니다: {runtime}"
        )
    if runtime.exists() or runtime.is_symlink():
        if not runtime.is_dir():
            raise WorkspaceOwnershipError(
                f"runtime 경로가 디렉터리가 아닙니다: {runtime}"
            )
        validate_runtime_worktree(source, state_dir, runtime, commit_sha)
        return RuntimeWorktree(runtime, commit_sha)

    expected_path = _canonical_path(runtime)
    registered = next(
        (
            record
            for record in _worktree_catalog(source)
            if _canonical_path(Path(str(record.get("worktree", ""))))
            == expected_path
        ),
        None,
    )
    if registered is not None:
        raise WorkspaceOwnershipError(
            f"runtime 경로가 없지만 Git worktree 등록이 남아 있습니다: {runtime}"
        )
    runtime.parent.mkdir(parents=True, exist_ok=True)
    git(source, "worktree", "add", "--detach", str(runtime), commit_sha)
    validate_runtime_worktree(source, state_dir, runtime, commit_sha)
    return RuntimeWorktree(runtime, commit_sha)


def source_config_path(source: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = source / candidate
    return candidate.resolve()


def runtime_child_arguments(
    arguments: list[str],
    *,
    source: Path,
    state_dir: Path,
    config_path: Path,
    runtime: RuntimeWorktree,
) -> list[str]:
    """Preserve user CLI semantics while pinning external path inputs."""

    stripped: list[str] = []
    skip_value = False
    value_options = {
        "--state-dir",
        "--config",
        "--runtime-source",
        "--runtime-sha",
    }
    for argument in arguments:
        if skip_value:
            skip_value = False
            continue
        if argument in value_options:
            skip_value = True
            continue
        if any(argument.startswith(option + "=") for option in value_options):
            continue
        if argument == "--runtime-child":
            continue
        stripped.append(argument)
    if skip_value:
        raise RuntimeError("경로 옵션 값이 없습니다.")
    return [
        "--runtime-child",
        "--runtime-source",
        str(source),
        "--runtime-sha",
        runtime.commit_sha,
        "--state-dir",
        str(state_dir),
        "--config",
        str(config_path),
        *stripped,
    ]


def run_runtime_child(
    source: Path,
    state_dir: Path,
    config_path: Path,
    runtime: RuntimeWorktree,
    arguments: list[str],
) -> int:
    child_arguments = runtime_child_arguments(
        arguments,
        source=source,
        state_dir=state_dir,
        config_path=config_path,
        runtime=runtime,
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-u",
            str(runtime.path / "loop_manager.py"),
            *child_arguments,
        ],
        cwd=runtime.path,
        check=False,
    )
    return completed.returncode


def validate_runtime_child(
    source: Path,
    state_dir: Path,
    commit_sha: str,
) -> None:
    validate_runtime_worktree(source, state_dir, ROOT, commit_sha)

def _ensure_execplans_schema(connection: sqlite3.Connection) -> None:
    connection.execute(EXEC_PLANS_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(exec_plans)")}
    if "blocked_fingerprint" not in columns:
        connection.execute("ALTER TABLE exec_plans ADD COLUMN blocked_fingerprint TEXT")
    if "blocked_worktree_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN blocked_worktree_fingerprint TEXT"
        )
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 0"
        )
    migrations = {
        "reviewer_session_id": "TEXT",
        "review_result": "TEXT",
        "validated_base_sha": "TEXT",
        "publication_remote_oid": "TEXT",
        "pr_conflict_recovery": "INTEGER NOT NULL DEFAULT 0",
        "pr_conflict_head_oid": "TEXT",
        "pr_conflict_base_oid": "TEXT",
        "pr_head_oid": "TEXT",
        "verify_failure_history": "TEXT NOT NULL DEFAULT '{}'",
        "review_failure_history": "TEXT NOT NULL DEFAULT '{}'",
        "initial_worker_level": "INTEGER",
        "effective_worker_level": "INTEGER",
        "review_level_history": "TEXT NOT NULL DEFAULT '{}'",
        "blocked_reason": "TEXT",
        "guidance_plan_hash": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE exec_plans ADD COLUMN {column} {definition}"
            )
    connection.execute(GUIDANCE_APPROVALS_SCHEMA)
    connection.execute(EXEC_PLANS_ACTIVE_INDEX)


def connect_database(state_dir: Path, *, create: bool = False) -> sqlite3.Connection:
    database = state_dir / "loop-manager.db"
    if not create and not database.is_file():
        raise RuntimeError(
            "상태 DB가 없습니다. 먼저 `loop_manager.py config init`을 실행하세요."
        )
    connection = sqlite3.connect(database, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if create:
        connection.execute(SCHEMA)
        project_registry.ensure_registry_schema(connection)
        _ensure_execplans_schema(connection)
        _ensure_cleanup_pending_schema(connection)
        notifications.ensure_schema(connection)
        connection.commit()
    else:
        try:
            connection.execute("SELECT 1 FROM projects LIMIT 1")
        except sqlite3.DatabaseError as error:
            connection.close()
            raise RuntimeError("상태 DB를 읽을 수 없습니다. 백업을 복원하거나 다시 초기화하세요.") from error
    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    migrations = {
        "provider": "TEXT NOT NULL DEFAULT 'codex'",
        "plan_path": "TEXT",
        "planning_fingerprint": "TEXT",
        "planning_attempt_fingerprint": "TEXT",
        "planning_workspace_owned": "INTEGER NOT NULL DEFAULT 0",
        "planning_retry_count": "INTEGER NOT NULL DEFAULT 0",
        "planner_session_id": "TEXT",
        "plan_sha": "TEXT",
        "worker_session_id": "TEXT",
        "reviewer_session_id": "TEXT",
        "worker_result": "TEXT",
        "verify_result": "TEXT",
        "review_result": "TEXT",
        "planning_validation_error": "TEXT",
        "planning_result_pending_validation": "INTEGER NOT NULL DEFAULT 0",
        "planning_result_completed": "INTEGER NOT NULL DEFAULT 0",
        "planning_error": "TEXT",
        "planning_error_fingerprint": "TEXT",
        "validated_base_sha": "TEXT",
        "publication_remote_oid": "TEXT",
        "pr_conflict_recovery": "INTEGER NOT NULL DEFAULT 0",
        "pr_conflict_head_oid": "TEXT",
        "pr_conflict_base_oid": "TEXT",
        "pr_head_oid": "TEXT",
        "agent_role": "TEXT",
        "agent_snapshot": "TEXT",
        "agent_input_fingerprint": "TEXT",
        "agent_baseline": "TEXT",
        "agent_recovery": "TEXT",
        "agent_input_plan": "TEXT",
        "agent_result_plan": "TEXT",
        "agent_result_snapshot": "TEXT",
        "agent_manager_snapshot": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {definition}")
    if not migrations.keys() <= columns:
        connection.commit()
    project_registry.ensure_registry_schema(connection)
    _ensure_execplans_schema(connection)
    _ensure_cleanup_pending_schema(connection)
    notifications.ensure_schema(connection)
    connection.commit()
    # Repository-aware migrations belong in run_project, after persisted Agent
    # config has been checked against its pre-invocation snapshot.
    return connection


def update_project(connection: sqlite3.Connection, repo: str, **values: object) -> None:
    if not values or not set(values).issubset(FIELDS):
        raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
    current = connection.execute(
        "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    assignments = ", ".join(f"{name} = ?" for name in values)
    connection.execute(
        f"UPDATE projects SET {assignments} WHERE repo = ?",
        [*values.values(), repo],
    )
    _mirror_project_update_to_execplan(connection, repo, current, values)
    notifications.enqueue_current_project_events(connection, repo)
    connection.commit()


def command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        raise RuntimeError(f"명령 실패 ({' '.join(arguments[:3])}):\n{output}")
    return result


def is_git_lock_failure(output: str) -> bool:
    normalized = " ".join(output.casefold().split())
    return any(
        marker in normalized
        for marker in (
            "another git process",
            "index.lock': file exists",
            "index.lock: file exists",
            "cannot lock ref",
            "could not lock",
            "failed to lock",
            "unable to create",
        )
    ) and ("lock" in normalized or ".lock" in normalized)


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = command(["git", "-C", str(repo), *arguments], check=False)
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        message = f"명령 실패 (git -C {repo}):\n{output}"
        if is_git_lock_failure(output):
            raise GitLockError(message)
        raise RuntimeError(message)
    return result


def github_repository(origin_url: str) -> str:
    match = re.fullmatch(
        r"(?:git@|ssh://git@|https://)(?:www\.)?github\.com[:/](?P<owner>[^/:\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?",
        origin_url.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("origin이 GitHub 저장소가 아닙니다.")
    return f"{match.group('owner')}/{match.group('repo')}".lower()


def backfill_plan_shas(
    connection: sqlite3.Connection,
    *,
    target_repo: str | None = None,
) -> None:
    rows = connection.execute(
        """
        SELECT repo, plan_path
        FROM projects
        WHERE plan_sha IS NULL
          AND plan_path IS NOT NULL
          AND phase IN ('WORKING', 'VERIFYING', 'REVIEWING', 'PUBLISHING')
        """
    ).fetchall()
    if target_repo is not None:
        rows = [row for row in rows if row["repo"] == target_repo]
    for row in rows:
        result = git(
            Path(row["repo"]),
            "log",
            "--all",
            "--diff-filter=A",
            "--format=%H",
            "--",
            row["plan_path"],
            check=False,
        )
        commits = [
            value
            for value in result.stdout.splitlines()
            if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
        ]
        if len(commits) == 1:
            connection.execute(
                "UPDATE projects SET plan_sha = ? WHERE repo = ?",
                (commits[0], row["repo"]),
            )
        else:
            connection.execute(
                "UPDATE projects SET phase = 'BLOCKED', last_error = ? WHERE repo = ?",
                (
                    PLAN_SHA_BACKFILL_ERROR,
                    row["repo"],
                ),
            )
            notifications.enqueue_current_project_events(connection, str(row["repo"]))
    connection.commit()


def git_bytes(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        output = result.stdout[-6000:].decode("utf-8", errors="replace").strip()
        message = f"명령 실패 (git -C {repo}):\n{output}"
        if is_git_lock_failure(output):
            raise GitLockError(message)
        raise RuntimeError(message)
    return result.stdout


def _git_tree_entries(repo: Path, revision: str) -> dict[bytes, tuple[bytes, bytes]]:
    tree = git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("Git tree 항목 형식이 올바르지 않습니다.")
        _mode, object_type, object_id = parts
        if object_type not in {b"blob", b"commit"}:
            raise RuntimeError("지원하지 않는 Git tree 항목 형식입니다.")
        entries[path] = (object_type, object_id)
    return entries


def _is_active_requirement(path: bytes) -> bool:
    return re.fullmatch(rb"docs/requirements/[^/]*\.md", path) is not None


def _is_planning_input(path: bytes) -> bool:
    if path in {b"AGENTS.md", b"docs/PRODUCT.md", b"docs/ARCHITECTURE.md"}:
        return True
    if _is_active_requirement(path):
        return True
    return not path.startswith(b"docs/")


def _planning_input_digest(entries: list[tuple[bytes, bytes, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-planning-input-v2\0")
    for path, object_type, contents in sorted(entries, key=lambda entry: entry[0]):
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(object_type).to_bytes(8, "big"))
        digest.update(object_type)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _planning_input_fingerprint(repo: Path, revision: str) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in _git_tree_entries(repo, revision).items():
        if _is_planning_input(path):
            if object_type == b"blob":
                contents = git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            else:
                contents = object_id
            entries.append((path, object_type, contents))
    return _planning_input_digest(entries)


def planning_input_fingerprint(repo: Path, base: str) -> str:
    return _planning_input_fingerprint(repo, f"origin/{base}")


def _is_requirement_archive_only_change(
    repo: Path,
    previous_revision: str,
    current_revision: str,
) -> bool:
    previous_tree = _git_tree_entries(repo, previous_revision)
    current_tree = _git_tree_entries(repo, current_revision)
    previous_inputs = {
        path: object_id for path, object_id in previous_tree.items() if _is_planning_input(path)
    }
    current_inputs = {
        path: object_id for path, object_id in current_tree.items() if _is_planning_input(path)
    }

    removed = previous_inputs.keys() - current_inputs.keys()
    added = current_inputs.keys() - previous_inputs.keys()
    changed = {
        path
        for path in previous_inputs.keys() & current_inputs.keys()
        if previous_inputs[path] != current_inputs[path]
    }
    if not removed or added or changed:
        return False

    requirement_prefix = b"docs/requirements/"
    archive_prefix = b"docs/requirements/archive/"
    for path in removed:
        if not _is_active_requirement(path):
            return False
        archived_path = archive_prefix + path.removeprefix(requirement_prefix)
        if archived_path in previous_tree or current_tree.get(archived_path) != previous_inputs[path]:
            return False
    return True


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def requirement_archive_path(source: str) -> str:
    match = re.fullmatch(r"docs/requirements/([^/\\]+\.md)", source)
    if match is None:
        raise ValueError(f"활성 Requirement 경로가 아닙니다: {source}")
    return f"docs/requirements/archive/{match.group(1)}"


def _execplan_source_exists(
    worktree: Path,
    source: str,
    *,
    allow_archive_fallback: bool,
) -> bool:
    if (worktree / source).is_file():
        return True
    return bool(
        allow_archive_fallback
        and source != "docs/PRODUCT.md"
        and (worktree / requirement_archive_path(source)).is_file()
    )


def parse_frontmatter(text: str) -> dict[str, object]:
    lines = text.replace("\r\n", "\n").splitlines()
    if not lines or lines[0] != "---":
        raise RuntimeError("ExecPlan frontmatter가 없습니다.")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise RuntimeError("ExecPlan frontmatter 종료 구분자가 없습니다.") from error
    values: dict[str, object] = {}
    current_list: str | None = None
    for line in lines[1:end]:
        if line.startswith("  - ") and current_list:
            assert isinstance(values[current_list], list)
            values[current_list].append(line[4:].strip())
            continue
        if ":" not in line:
            raise RuntimeError(f"유효하지 않은 frontmatter 줄입니다: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in values:
            raise RuntimeError(f"중복 frontmatter key입니다: {key}")
        if value == "":
            values[key] = []
            current_list = key
        elif value == "[]":
            values[key] = []
            current_list = None
        else:
            values[key] = value
            current_list = None
    return values


def _parse_execplan(
    worktree: Path,
    path: Path,
    *,
    require_priority: bool = False,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, object]:
    legacy = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    allowed = legacy | {"priority"}
    values = parse_frontmatter(path.read_text(encoding="utf-8"))
    if set(values) not in {frozenset(legacy), frozenset(allowed)}:
        raise RuntimeError("ExecPlan frontmatter key가 docs/PLANS.md 형식과 다릅니다.")
    if "priority" not in values:
        if require_priority:
            raise RuntimeError("새 ExecPlan에는 priority가 필요합니다.")
        values["priority"] = "normal"
    plan_id = values["id"]
    title = values["title"]
    if not isinstance(plan_id, str) or not re.fullmatch(r"G-\d{3,}", plan_id):
        raise RuntimeError("ExecPlan id는 G-000 형식이어야 합니다.")
    if not isinstance(title, str) or path.name != f"[{plan_id}] {title}.md":
        raise RuntimeError("ExecPlan 파일명은 `[id] title.md`와 일치해야 합니다.")
    sources = values["sources"]
    dependencies = values["depends_on"]
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("ExecPlan sources가 하나 이상 필요합니다.")
    if not isinstance(dependencies, list):
        raise RuntimeError("ExecPlan depends_on이 유효하지 않습니다.")
    for source in sources:
        if not isinstance(source, str) or not (
            source == "docs/PRODUCT.md"
            or re.fullmatch(r"docs/requirements/[^/\\]+\.md", source)
        ):
            raise RuntimeError(f"허용되지 않는 ExecPlan source입니다: {source}")
        if validate_sources and not _execplan_source_exists(
            worktree,
            source,
            allow_archive_fallback=allow_archive_fallback,
        ):
            raise RuntimeError(f"존재하지 않는 ExecPlan source입니다: {source}")
    for dependency in dependencies:
        if not isinstance(dependency, str) or not re.fullmatch(r"G-\d{3,}", dependency):
            raise RuntimeError(f"ExecPlan depends_on ID 형식이 유효하지 않습니다: {dependency}")
    if values["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("ExecPlan complexity가 유효하지 않습니다.")
    if values["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("ExecPlan risk가 유효하지 않습니다.")
    if values["priority"] not in PRIORITY_ORDER:
        raise RuntimeError("ExecPlan priority가 유효하지 않습니다.")
    return values


def _validate_dependency_graph(catalog: dict[str, dict[str, object]]) -> None:
    for plan_id, plan in catalog.items():
        dependencies = plan["depends_on"]
        assert isinstance(dependencies, list)
        missing = sorted(dependency for dependency in dependencies if dependency not in catalog)
        if missing:
            raise RuntimeError(
                f"ExecPlan {plan_id}의 존재하지 않는 depends_on ID입니다: "
                + ", ".join(missing)
            )

    states: dict[str, str] = {}
    stack: list[str] = []

    def visit(plan_id: str) -> None:
        states[plan_id] = "visiting"
        stack.append(plan_id)
        dependencies = catalog[plan_id]["depends_on"]
        assert isinstance(dependencies, list)
        for dependency in sorted(dependencies):
            if states.get(dependency) == "visiting":
                cycle_start = stack.index(dependency)
                cycle = [*stack[cycle_start:], dependency]
                raise RuntimeError("순환 ExecPlan 의존성입니다: " + " -> ".join(cycle))
            if states.get(dependency) is None:
                visit(dependency)
        stack.pop()
        states[plan_id] = "visited"

    for plan_id in catalog:
        if states.get(plan_id) is None:
            visit(plan_id)


def load_execplan_catalog(
    worktree: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, dict[str, object]]:
    entries: list[tuple[str, dict[str, object]]] = []
    plans_directory = worktree / "docs/plans"
    plan_paths = sorted(
        (path for path in plans_directory.glob("*.md") if path.is_file()),
        key=lambda path: path.name,
    )
    for path in plan_paths:
        plan = _parse_execplan(
            worktree,
            path,
            validate_sources=validate_sources,
            allow_archive_fallback=allow_archive_fallback,
        )
        plan_id = plan["id"]
        assert isinstance(plan_id, str)
        entries.append((plan_id, plan))

    catalog: dict[str, dict[str, object]] = {}
    for plan_id, plan in sorted(entries, key=lambda entry: entry[0]):
        if plan_id in catalog:
            raise RuntimeError(f"중복 ExecPlan id입니다: {plan_id}")
        catalog[plan_id] = plan

    _validate_dependency_graph(catalog)
    return catalog


def validate_execplan(worktree: Path, relative_path: str) -> dict[str, object]:
    catalog = load_execplan_catalog(worktree)
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", relative_path)
    if match:
        plan = catalog.get(match.group(1))
        if plan is not None:
            expected_path = f"docs/plans/[{plan['id']}] {plan['title']}.md"
            if relative_path == expected_path:
                return plan
    raise RuntimeError(f"검증된 ExecPlan 카탈로그에 계획이 없습니다: {relative_path}")


class PlannerValidationError(RuntimeError):
    """A Planner output error that is eligible for a correction attempt."""


@dataclass(frozen=True)
class ValidatedPlanningResult:
    """Validated files to publish, separated from executable plans."""

    plans: tuple[tuple[str, dict[str, object]], ...]
    publication_paths: tuple[str, ...]


_FUTURE_IDEA_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


def _idea_tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[0-9a-zA-Z가-힣]+", text.casefold()))


def _future_idea_items_from_text(text: str) -> tuple[tuple[str, ...], ...]:
    items: list[tuple[str, ...]] = []
    for line in text.splitlines():
        match = _FUTURE_IDEA_ITEM.fullmatch(line)
        if match is None:
            continue
        item = re.sub(r"^\[[ xX]\]\s*", "", match.group(1)).strip()
        tokens = _idea_tokens(item)
        if tokens:
            items.append(tokens)
    return tuple(items)


def _future_idea_items(
    worktree: Path,
    active_sha: str,
) -> tuple[tuple[str, ...], ...]:
    path = worktree / "docs/FUTURE_IDEAS.md"
    current_text = ""
    if path.is_file():
        try:
            current_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError("Planner FUTURE_IDEAS를 읽을 수 없습니다.") from error

    previous = git(
        worktree,
        "show",
        f"{active_sha}:docs/FUTURE_IDEAS.md",
        check=False,
    )
    previous_text = previous.stdout if previous.returncode == 0 else ""
    return tuple(
        dict.fromkeys(
            _future_idea_items_from_text(previous_text)
            + _future_idea_items_from_text(current_text)
        )
    )


def _contains_token_sequence(
    tokens: tuple[str, ...],
    candidate: tuple[str, ...],
) -> bool:
    width = len(candidate)
    return any(
        tokens[offset : offset + width] == candidate
        for offset in range(len(tokens) - width + 1)
    )


def _validate_future_idea_separation(
    worktree: Path,
    active_sha: str,
    plan_paths: list[str],
) -> None:
    future_ideas = _future_idea_items(worktree, active_sha)
    if not future_ideas:
        return
    for plan_path in plan_paths:
        try:
            plan_text = (worktree / plan_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"Planner ExecPlan을 읽을 수 없습니다: {plan_path}") from error
        plan_tokens = _idea_tokens(plan_text)
        if any(
            _contains_token_sequence(plan_tokens, idea)
            for idea in future_ideas
        ):
            raise RuntimeError(
                "docs/FUTURE_IDEAS.md 항목을 같은 계획 주기의 새 ExecPlan에 "
                f"포함할 수 없습니다: {plan_path}"
            )


def validate_planner_result(
    worktree: Path,
    active_sha: str,
    dirty_files: list[str],
) -> ValidatedPlanningResult | None:
    """Validate new ExecPlans and the optional future-idea document.

    Scope and ExecPlan catalog errors are deliberately translated to a
    dedicated exception. Operational Git failures must retain their normal
    recovery boundary and never consume the Planner validation retry budget.
    """
    planned_files = sorted(set(dirty_files) | set(changed_files(worktree, active_sha)))
    if not planned_files:
        return None
    plan_paths: list[str] = []
    for plan_path in planned_files:
        if plan_path == "docs/FUTURE_IDEAS.md":
            if not (worktree / plan_path).is_file():
                raise PlannerValidationError(
                    "Planner는 docs/FUTURE_IDEAS.md를 삭제할 수 없습니다."
                )
            continue
        if not re.fullmatch(r"docs/plans/[^/]+\.md", plan_path):
            raise PlannerValidationError(
                "Planner는 docs/plans의 새 ExecPlan과 docs/FUTURE_IDEAS.md만 "
                "작성할 수 있습니다."
            )
        if not (worktree / plan_path).is_file():
            raise PlannerValidationError("Planner는 기존 ExecPlan을 삭제할 수 없습니다.")
        try:
            planner_text = (worktree / plan_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PlannerValidationError(f"Planner ExecPlan을 읽을 수 없습니다: {plan_path}") from error
        if contains_guidance_sections(planner_text):
            raise PlannerValidationError("Planner는 AGENTS.md guidance를 제안할 수 없습니다.")
        existed = git(
            worktree,
            "cat-file",
            "-e",
            f"{active_sha}:{plan_path}",
            check=False,
        ).returncode == 0
        if existed:
            raise PlannerValidationError(f"Planner는 기존 ExecPlan을 수정할 수 없습니다: {plan_path}")
        plan_paths.append(plan_path)

    try:
        catalog = load_execplan_catalog(worktree, validate_sources=False)
        plans: list[tuple[str, dict[str, object]]] = []
        for plan_path in plan_paths:
            parsed = _parse_execplan(
                worktree,
                worktree / plan_path,
                require_priority=True,
                allow_archive_fallback=False,
            )
            plans.append((plan_path, parsed))
        _validate_future_idea_separation(worktree, active_sha, plan_paths)
        new_ids = {str(plan["id"]) for _, plan in plans}
        existing_ids = [int(plan_id[2:]) for plan_id in catalog if plan_id not in new_ids]
        first_id = max(existing_ids, default=0) + 1
        expected = list(range(first_id, first_id + len(plans)))
        actual = sorted(int(str(plan["id"])[2:]) for _, plan in plans)
        if actual != expected:
            raise RuntimeError("새 ExecPlan ID는 기존 최대 번호 다음부터 연속적이어야 합니다.")
        return ValidatedPlanningResult(
            plans=tuple(
                sorted(plans, key=lambda item: int(str(item[1]["id"])[2:]))
            ),
            publication_paths=tuple(planned_files),
        )
    except RuntimeError as error:
        raise PlannerValidationError(str(error)) from error


def _plan_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", path)
    return match.group(1) if match else None


def _execplan_metadata(plan: dict[str, object]) -> tuple[str, str, str]:
    return (
        json.dumps(plan["sources"], ensure_ascii=False, separators=(",", ":")),
        json.dumps(plan["depends_on"], ensure_ascii=False, separators=(",", ":")),
        str(plan["priority"]),
    )


def register_execplans(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    plan_commit: str,
    planning_fingerprint: str,
) -> None:
    """Register one published Planner batch atomically and idempotently."""

    with connection:
        _ensure_execplans_schema(connection)
        for path, plan in plans:
            plan_id = str(plan["id"])
            sources, dependencies, priority = _execplan_metadata(plan)
            content_hash = file_hash(worktree / path)
            initial_level = worker_level(plan)
            existing = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND (plan_id = ? OR path = ?)",
                (repo, plan_id, path),
            ).fetchone()
            if existing:
                immutable = (
                    existing["plan_id"],
                    existing["path"],
                    existing["title"],
                    existing["sources"],
                    existing["depends_on"],
                    existing["priority"],
                    existing["content_hash"],
                    existing["plan_sha"],
                )
                expected = (
                    plan_id,
                    path,
                    str(plan["title"]),
                    sources,
                    dependencies,
                    priority,
                    content_hash,
                    plan_commit,
                )
                if immutable != expected:
                    raise RuntimeError(f"게시된 ExecPlan 등록 정보가 충돌합니다: {plan_id}")
                if existing["initial_worker_level"] is None:
                    connection.execute(
                        "UPDATE exec_plans SET initial_worker_level = ?, "
                        "effective_worker_level = ? WHERE repo = ? AND plan_id = ?",
                        (initial_level, initial_level, repo, plan_id),
                    )
                continue
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, plan_sha, initial_worker_level,
                    effective_worker_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?, ?)
                """,
                (
                    repo,
                    plan_id,
                    path,
                    str(plan["title"]),
                    sources,
                    dependencies,
                    priority,
                    content_hash,
                    plan_commit,
                    initial_level,
                    initial_level,
                ),
            )
        values = _clear_active_values(
            processed_sha=plan_commit,
            planning_fingerprint=planning_fingerprint,
        )
        assignments = ", ".join(f"{name} = ?" for name in values)
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?", (*values.values(), repo)
        )


def _legacy_plan_sha(root: Path, revision: str, relative_path: str) -> str:
    result = git(
        root,
        "log",
        revision,
        "--diff-filter=A",
        "--format=%H",
        "--",
        relative_path,
        check=False,
    )
    commits = [
        value
        for value in result.stdout.splitlines()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
    ]
    if len(commits) != 1:
        raise RuntimeError(
            f"Legacy ExecPlan의 게시 commit을 안전하게 확인할 수 없습니다: {relative_path}"
        )
    return commits[0]


def migrate_legacy_execplans(
    connection: sqlite3.Connection,
    *,
    reconcile_history: bool = False,
    target_repo: str | None = None,
) -> None:
    """Conservatively move legacy project-owned execution state into the catalog."""

    _ensure_execplans_schema(connection)
    phase_status = {
        "WORKING": "IMPLEMENTING",
        "VERIFYING": "VERIFYING",
        "REVIEWING": "REVIEWING",
        "PUBLISHING": "PUBLISHING",
        "PR_OPEN": "PR_OPEN",
        "BLOCKED": "BLOCKED",
    }
    if target_repo is None:
        projects = connection.execute("SELECT * FROM projects ORDER BY repo").fetchall()
    else:
        projects = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (target_repo,)
        ).fetchall()
    with connection:
        for project in projects:
            if project["phase"] == "PLANNING":
                continue
            root = Path(project["repo"])
            if reconcile_history:
                git(root, "fetch", "origin")
            active_path = str(project["plan_path"] or "")
            documents: dict[str, str] = {}

            revision = f"refs/remotes/origin/{project['base_branch']}"
            listed = git(
                root,
                "ls-tree",
                "-r",
                "--name-only",
                revision,
                "--",
                "docs/plans",
                check=False,
            )
            if listed.returncode == 0:
                for relative_path in listed.stdout.splitlines():
                    if not re.fullmatch(r"docs/plans/[^/]+\.md", relative_path):
                        continue
                    shown = git(root, "show", f"{revision}:{relative_path}", check=False)
                    if shown.returncode == 0:
                        documents[relative_path] = shown.stdout

            if active_path and active_path not in documents and project["plan_sha"]:
                shown = git(
                    root,
                    "show",
                    f"{project['plan_sha']}:{active_path}",
                    check=False,
                )
                if shown.returncode == 0:
                    documents[active_path] = shown.stdout

            for relative_path, plan_text in sorted(documents.items()):
                try:
                    plan = published_execplan(relative_path, plan_text)
                except RuntimeError:
                    continue
                plan_id = str(plan["id"])
                if connection.execute(
                    "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = ?",
                    (project["repo"], plan_id),
                ).fetchone():
                    continue
                is_active = relative_path == active_path and project["phase"] in phase_status
                if not is_active and not reconcile_history:
                    continue
                historical_pr = None
                if is_active:
                    status = phase_status[str(project["phase"])]
                    plan_sha = project["plan_sha"]
                else:
                    historical_pr = pr_for_branch(root, f"loop-manager/{plan_id}")
                    if historical_pr is None:
                        status = "READY"
                    elif historical_pr["state"] == "MERGED" or historical_pr.get("mergedAt"):
                        status = "MERGED"
                    else:
                        raise RuntimeError(
                            f"Legacy ExecPlan의 PR 상태를 자동 이관할 수 없습니다: "
                            f"{plan_id} ({historical_pr['state']})"
                        )
                    plan_sha = _legacy_plan_sha(root, revision, relative_path)
                sources, dependencies, priority = _execplan_metadata(plan)
                initial_level = worker_level(plan)
                connection.execute(
                    """
                    INSERT INTO exec_plans (
                        repo, plan_id, path, title, sources, depends_on, priority,
                        content_hash, status, plan_sha, active_sha, branch, worktree,
                        pr_url, last_error, worker_session_id, worker_result, verify_result,
                        initial_worker_level, effective_worker_level, workspace_owned
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?)
                    """,
                    (
                        project["repo"],
                        plan_id,
                        relative_path,
                        str(plan["title"]),
                        sources,
                        dependencies,
                        priority,
                        hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
                        status,
                        plan_sha,
                        project["active_sha"] if is_active else None,
                        project["branch"] if is_active else None,
                        project["worktree"] if is_active else None,
                        project["pr_url"] if is_active else (
                            str(historical_pr["url"]) if historical_pr else None
                        ),
                        project["last_error"] if is_active else None,
                        project["worker_session_id"] if is_active else None,
                        project["worker_result"] if is_active else None,
                        project["verify_result"] if is_active else None,
                        initial_level,
                        initial_level,
                        1 if is_active else 0,
                    ),
                )


def active_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    _ensure_execplans_schema(connection)
    placeholders = ", ".join("?" for _ in ACTIVE_EXECPLAN_STATUSES)
    return connection.execute(
        f"SELECT * FROM exec_plans WHERE repo = ? AND status IN ({placeholders})",
        (repo, *sorted(ACTIVE_EXECPLAN_STATUSES)),
    ).fetchone()


def mark_execplan_workspace_owned(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        changed = connection.execute(
            "UPDATE exec_plans SET workspace_owned = 1 "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        )
        if changed.rowcount != 1:
            raise RuntimeError("workspace 소유권을 기록할 ExecPlan을 찾을 수 없습니다.")


def reserve_execplan_workspace(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        changed = connection.execute(
            "UPDATE exec_plans SET workspace_owned = ? "
            "WHERE repo = ? AND plan_id = ? AND workspace_owned IN (?, ?)",
            (
                WORKSPACE_RESERVED,
                repo,
                plan_id,
                WORKSPACE_UNOWNED,
                WORKSPACE_RESERVED,
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("workspace 생성을 예약할 ExecPlan을 찾을 수 없습니다.")


def compact_execplan_catalog(connection: sqlite3.Connection, repo: str) -> str:
    """Serialize only non-terminal plan metadata, never plan bodies."""

    _ensure_execplans_schema(connection)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED'",
        (repo,),
    ).fetchall()
    rows = sorted(rows, key=lambda row: int(str(row["plan_id"])[2:]))
    catalog = [
        {
            "id": row["plan_id"],
            "title": row["title"],
            "status": row["status"],
            "sources": json.loads(row["sources"]),
            "depends_on": json.loads(row["depends_on"]),
            "priority": row["priority"],
        }
        for row in rows
    ]
    return json.dumps(catalog, ensure_ascii=False, indent=2)


def catalog_schedule_decision(
    connection: sqlite3.Connection,
    repo: str,
) -> CatalogScheduleDecision:
    """Separate active work, READY presence, and dependency-gated selection."""

    _ensure_execplans_schema(connection)
    active = active_execplan(connection, repo)
    statuses = {
        row["plan_id"]: row["status"]
        for row in connection.execute(
            "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
            (repo,),
        )
    }
    ready = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY'",
        (repo,),
    ).fetchall()
    candidates = []
    for row in ready:
        dependencies = json.loads(row["depends_on"])
        if all(statuses.get(dependency) == "MERGED" for dependency in dependencies):
            candidates.append(row)
    selected = None
    if active is None and candidates:
        selected = min(
            candidates,
            key=lambda row: (
                PRIORITY_ORDER[str(row["priority"])],
                int(str(row["plan_id"])[2:]),
            ),
        )
    return CatalogScheduleDecision(
        active=active,
        has_ready=bool(ready),
        runnable_ready=selected,
    )


def select_ready_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Apply the dependency gate before deterministic priority/ID ordering."""

    return catalog_schedule_decision(connection, repo).runnable_ready


def activate_ready_execplan(
    connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    base_sha: str,
) -> sqlite3.Row | None:
    selected = select_ready_execplan(connection, repo)
    if selected is None:
        return None
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    branch = str(selected["branch"] or f"loop-manager/{selected['plan_id']}")
    worktree = (
        Path(str(selected["worktree"]))
        if selected["worktree"]
        else state_dir / "worktrees" / project_id / str(selected["plan_id"])
    )
    activation_sha = (
        git(worktree, "rev-parse", "HEAD").stdout.strip()
        if worktree.is_dir()
        else base_sha
    )
    with connection:
        changed = connection.execute(
            """
            UPDATE exec_plans
            SET status = 'IMPLEMENTING', active_sha = ?, branch = ?, worktree = ?,
                last_error = NULL
            WHERE repo = ? AND plan_id = ? AND status = 'READY'
            """,
            (activation_sha, branch, str(worktree), repo, selected["plan_id"]),
        )
        if changed.rowcount != 1:
            raise RuntimeError("READY ExecPlan 선택 상태가 변경되었습니다.")
        connection.execute(
            """
            UPDATE projects
            SET active_sha = ?, validated_base_sha = NULL,
                publication_remote_oid = NULL, pr_conflict_recovery = 0,
                pr_conflict_head_oid = NULL, pr_conflict_base_oid = NULL,
                plan_sha = ?, phase = 'WORKING',
                branch = ?, worktree = ?, plan_path = ?, pr_url = NULL,
                pr_head_oid = NULL, last_error = NULL,
                worker_session_id = NULL, reviewer_session_id = NULL,
                worker_result = NULL, verify_result = NULL, review_result = NULL
            WHERE repo = ?
            """,
            (
                activation_sha,
                selected["plan_sha"],
                branch,
                str(worktree),
                selected["path"],
                repo,
            ),
        )
    return connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, selected["plan_id"]),
    ).fetchone()


def hydrate_active_project(connection: sqlite3.Connection, repo: str) -> None:
    """Refresh the legacy execution mirror from the plan-state source of truth."""

    plan = active_execplan(connection, repo)
    if plan is None:
        return
    phase = "WORKING" if plan["status"] == "IMPLEMENTING" else plan["status"]
    connection.execute(
        """
        UPDATE projects
        SET active_sha = ?, validated_base_sha = ?, publication_remote_oid = ?,
            pr_conflict_recovery = ?, pr_conflict_head_oid = ?,
            pr_conflict_base_oid = ?, plan_sha = ?, phase = ?, branch = ?,
            worktree = ?, plan_path = ?,
            pr_url = ?, pr_head_oid = ?, last_error = ?, worker_session_id = ?,
            reviewer_session_id = ?, worker_result = ?, verify_result = ?, review_result = ?
        WHERE repo = ?
        """,
        (
            plan["active_sha"],
            plan["validated_base_sha"],
            plan["publication_remote_oid"],
            plan["pr_conflict_recovery"],
            plan["pr_conflict_head_oid"],
            plan["pr_conflict_base_oid"],
            plan["plan_sha"],
            phase,
            plan["branch"],
            plan["worktree"],
            plan["path"],
            plan["pr_url"],
            plan["pr_head_oid"],
            plan["last_error"],
            plan["worker_session_id"],
            plan["reviewer_session_id"],
            plan["worker_result"],
            plan["verify_result"],
            plan["review_result"],
            repo,
        ),
    )
    connection.commit()


def _mirror_project_update_to_execplan(
    connection: sqlite3.Connection,
    repo: str,
    current: sqlite3.Row | None,
    values: dict[str, object],
) -> None:
    if current is None or not current["plan_path"]:
        return
    if "phase" in values and values["phase"] is None:
        return
    plan_id = _plan_id_from_path(str(current["plan_path"]))
    if plan_id is None:
        return
    _ensure_execplans_schema(connection)
    translated: dict[str, object] = {}
    for field in (
        "active_sha",
        "validated_base_sha",
        "publication_remote_oid",
        "pr_conflict_recovery",
        "pr_conflict_head_oid",
        "pr_conflict_base_oid",
        "plan_sha",
        "branch",
        "worktree",
        "pr_url",
        "pr_head_oid",
        "last_error",
        "worker_session_id",
        "reviewer_session_id",
        "worker_result",
        "verify_result",
        "review_result",
    ):
        if field in values:
            translated[field] = values[field]
    if "phase" in values:
        status = "IMPLEMENTING" if values["phase"] == "WORKING" else values["phase"]
        if status != "PLANNING":
            translated["status"] = status
    if not translated:
        return
    assignments = ", ".join(f"{name} = ?" for name in translated)
    connection.execute(
        f"UPDATE exec_plans SET {assignments} WHERE repo = ? AND plan_id = ?",
        (*translated.values(), repo, plan_id),
    )


def validate_runtime_prerequisites(
    path: Path,
    *,
    provider: str,
    config_path: Path | None,
) -> None:
    """Validate executable, authentication, and profile prerequisites only."""

    if not shutil.which("git") or not shutil.which("gh"):
        raise RuntimeError("git과 gh CLI가 PATH에 있어야 합니다.")
    if provider not in AGENT_PROVIDERS:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    if config_path is not None:
        validate_provider_runtime(config_path, provider, path)
    elif not shutil.which(provider):
        raise RuntimeError(
            f"선택한 {provider.capitalize()} CLI가 PATH에 설치되어 있지 않습니다."
        )


def repository_origin(root: Path) -> str:
    return github_repository(git(root, "remote", "get-url", "origin").stdout)


def validate_repository(
    path: Path,
    *,
    allow_agents_mismatch: bool,
    provider: str = "codex",
    config_path: Path | None = None,
    registration_checks: bool = True,
    expected_origin: str | None = None,
    origin_result: list[str] | None = None,
) -> Path:
    """Validate runtime prerequisites and stable repository identity only."""

    del allow_agents_mismatch, registration_checks
    path = path.expanduser().resolve()
    validate_runtime_prerequisites(
        path,
        provider=provider,
        config_path=config_path,
    )
    root = Path(git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != path:
        raise RuntimeError(f"저장소 루트 경로를 지정하세요: {root}")
    current_origin = repository_origin(root)
    if origin_result is not None:
        origin_result.append(current_origin)
    if expected_origin is not None and current_origin != expected_origin:
        raise RuntimeError(
            "등록된 GitHub origin과 현재 origin이 다릅니다: "
            f"{current_origin} (expected {expected_origin})"
        )
    return root


def remote_base(repo: Path, requested: str | None) -> str:
    git(repo, "fetch", "origin")
    if requested:
        base = requested
    else:
        reference = git(
            repo,
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ).stdout.strip()
        base = reference.removeprefix("origin/")
    git(repo, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}")
    return base


def _revision_blob(
    root: Path,
    entries: Mapping[bytes, tuple[bytes, bytes]],
    relative_path: str,
) -> bytes | None:
    entry = entries.get(os.fsencode(relative_path))
    if entry is None:
        return None
    object_type, object_id = entry
    if object_type != b"blob":
        raise RuntimeError(f"등록 파일이 blob이 아닙니다: {relative_path}")
    return git_bytes(root, "cat-file", "blob", object_id.decode("ascii"))


def inspect_registration_files(
    root: Path,
    revision: str,
    *,
    approve_agents_mismatch: bool,
) -> tuple[str, bool]:
    """Validate registration documents from one immutable commit tree."""

    entries = _git_tree_entries(root, revision)
    required_paths = ("AGENTS.md", "docs/PLANS.md", "docs/ARCHITECTURE.md")
    blobs = {
        relative_path: _revision_blob(root, entries, relative_path)
        for relative_path in required_paths
    }
    missing = [path for path, contents in blobs.items() if contents is None]
    product = _revision_blob(root, entries, "docs/PRODUCT.md")
    active_requirements = tuple(
        path for path in entries if _is_active_requirement(path)
    )
    if product is None and not active_requirements:
        missing.append("docs/PRODUCT.md 또는 docs/requirements/*.md")
    if missing:
        raise RuntimeError("필수 파일이 없습니다: " + ", ".join(missing))

    plans = blobs["docs/PLANS.md"]
    architecture = blobs["docs/ARCHITECTURE.md"]
    agents = blobs["AGENTS.md"]
    assert plans is not None and architecture is not None and agents is not None
    if hashlib.sha256(plans).hexdigest() != file_hash(ROOT / "docs/PLANS.md"):
        raise RuntimeError("docs/PLANS.md가 Loop Manager 기준 파일과 다릅니다.")
    if hashlib.sha256(architecture).hexdigest() == file_hash(
        ROOT / "docs/ARCHITECTURE.template.md"
    ):
        raise RuntimeError(
            "docs/ARCHITECTURE.md가 변경되지 않은 템플릿이라 등록할 수 없습니다."
        )
    if product is not None and hashlib.sha256(product).hexdigest() == file_hash(
        ROOT / "docs/PRODUCT.template.md"
    ):
        raise RuntimeError(
            "docs/PRODUCT.md가 변경되지 않은 템플릿이라 등록할 수 없습니다."
        )

    try:
        target_agents = agents.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise RuntimeError("origin의 AGENTS.md가 UTF-8이 아닙니다.") from error
    common_agents = normalized_text(ROOT / "AGENTS.md")
    agents_hash = hashlib.sha256(agents).hexdigest()
    mismatch = common_agents not in target_agents
    if mismatch and not approve_agents_mismatch:
        raise RuntimeError(
            "AGENTS.md에 공통 규칙이 없습니다. 내용을 확인한 뒤 "
            "`config add --approve-agents-mismatch`로 승인하세요."
        )
    return agents_hash, mismatch


def inspect_project_registration(
    path: Path,
    *,
    config_path: Path,
    provider: str,
    requested_base: str | None,
    approve_agents_mismatch: bool,
) -> ProjectRegistrationInspection:
    """Validate one project from its fetched origin revision before registry writes."""

    origins: list[str] = []
    root = validate_repository(
        path,
        allow_agents_mismatch=approve_agents_mismatch,
        provider=provider,
        config_path=config_path,
        registration_checks=False,
        origin_result=origins,
    )
    if not origins:
        raise RuntimeError("검증한 GitHub origin을 확인할 수 없습니다.")
    origin = origins[0]
    base_branch = remote_base(root, requested_base)
    revision = git(
        root,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    agents_hash, agents_approved = inspect_registration_files(
        root,
        revision,
        approve_agents_mismatch=approve_agents_mismatch,
    )
    return ProjectRegistrationInspection(
        root=root,
        base_branch=base_branch,
        provider=provider,
        agents_hash=agents_hash,
        agents_approved=agents_approved,
        origin=origin,
    )


def add_registered_project(
    connection: sqlite3.Connection,
    path: Path,
    *,
    config_path: Path,
    provider: str,
    requested_base: str | None,
    approve_agents_mismatch: bool,
) -> project_registry.RegisteredProject:
    inspection = inspect_project_registration(
        path,
        config_path=config_path,
        provider=provider,
        requested_base=requested_base,
        approve_agents_mismatch=approve_agents_mismatch,
    )
    project = project_registry.RegisteredProject(
        repo=str(inspection.root),
        base_branch=inspection.base_branch,
        provider=inspection.provider,
        agents_hash=inspection.agents_hash,
        agents_approved=inspection.agents_approved,
        origin=inspection.origin,
    )
    project_registry.add_project(connection, project)
    return project


def _base_worktree_status(repo: Path) -> str:
    return git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout


def _ignored_worktree_paths(repo: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            _nul_git_paths(
                repo,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            )
        )
    )


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = tuple(part for part in left.replace("\\", "/").split("/") if part)
    right_parts = tuple(part for part in right.replace("\\", "/").split("/") if part)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def _base_worktree_snapshot(
    repo: Path,
) -> tuple[str | None, str, str, tuple[tuple[str, str], ...], tuple[str, ...]]:
    branch_result = git(
        repo,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    return (
        branch,
        head,
        _base_worktree_status(repo),
        tuple(sorted(_git_operation_entries(repo).items())),
        _ignored_worktree_paths(repo),
    )


def synchronize_base_worktree(
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
) -> BaseWorktreeSyncResult:
    """Best-effort fast-forward of an eligible primary checkout."""

    try:
        selected_remote = remote_sha or git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base}^{{commit}}",
        ).stdout.strip()
        before = _base_worktree_snapshot(repo)
        branch, local_sha, status, operations, ignored = before
        if branch is None:
            return BaseWorktreeSyncResult("skipped", "detached HEAD", selected_remote)
        if branch != base:
            return BaseWorktreeSyncResult(
                "skipped",
                f"다른 branch checkout: {branch}",
                selected_remote,
            )
        if operations:
            return BaseWorktreeSyncResult(
                "skipped",
                "진행 중 Git operation: " + ", ".join(name for name, _ in operations),
                selected_remote,
            )
        if status:
            return BaseWorktreeSyncResult(
                "skipped",
                "tracked, staged 또는 non-ignored untracked 변경",
                selected_remote,
            )
        if local_sha == selected_remote:
            return BaseWorktreeSyncResult("equal", "이미 원격 base와 같음", selected_remote)

        behind = git(
            repo,
            "merge-base",
            "--is-ancestor",
            local_sha,
            selected_remote,
            check=False,
        )
        if behind.returncode not in {0, 1}:
            return BaseWorktreeSyncResult(
                "failed",
                "local/remote ancestry 확인 실패: " + behind.stdout[-2000:].strip(),
                selected_remote,
            )
        if behind.returncode == 1:
            ahead = git(
                repo,
                "merge-base",
                "--is-ancestor",
                selected_remote,
                local_sha,
                check=False,
            )
            if ahead.returncode not in {0, 1}:
                return BaseWorktreeSyncResult(
                    "failed",
                    "local/remote ancestry 확인 실패: "
                    + ahead.stdout[-2000:].strip(),
                    selected_remote,
                )
            reason = "ahead" if ahead.returncode == 0 else "diverged"
            return BaseWorktreeSyncResult("skipped", reason, selected_remote)

        changed_paths = tuple(
            _nul_git_paths(
                repo,
                "diff",
                "--name-only",
                "-z",
                local_sha,
                selected_remote,
            )
        )
        overlaps = sorted(
            ignored_path
            for ignored_path in ignored
            if any(
                _paths_overlap(ignored_path, changed_path)
                for changed_path in changed_paths
            )
        )
        if overlaps:
            return BaseWorktreeSyncResult(
                "skipped",
                "원격 변경과 겹치는 ignored 경로: " + ", ".join(overlaps[:8]),
                selected_remote,
            )

        rechecked = _base_worktree_snapshot(repo)
        if rechecked != before:
            return BaseWorktreeSyncResult(
                "skipped",
                "eligibility 확인 뒤 기본 worktree 상태가 변경됨",
                selected_remote,
            )
        updated = git(repo, "merge", "--ff-only", selected_remote, check=False)
        if updated.returncode:
            output = updated.stdout[-2000:].strip()
            reason = "Git lock 충돌" if is_git_lock_failure(output) else "fast-forward 실패"
            if output:
                reason += f": {output}"
            return BaseWorktreeSyncResult("failed", reason, selected_remote)
        final_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        if final_head != selected_remote:
            return BaseWorktreeSyncResult(
                "failed",
                f"fast-forward 결과 HEAD 불일치: {final_head}",
                selected_remote,
            )
        return BaseWorktreeSyncResult("updated", "원격 base로 fast-forward", selected_remote)
    except (GitLockError, RuntimeError, OSError) as error:
        return BaseWorktreeSyncResult("failed", str(error), remote_sha or "")


def ensure_safe_base_worktree(
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
) -> BaseWorktreeSyncResult:
    """Compatibility entry point for the non-blocking worktree synchronizer."""

    return synchronize_base_worktree(repo, base, remote_sha=remote_sha)


def log_base_worktree_sync(repo: Path, result: BaseWorktreeSyncResult) -> None:
    if result.status == "equal":
        return
    operation_log(
        f"기본 worktree 동기화 {result.status} ({repo}): {result.reason}",
        error=result.status in {"skipped", "failed"},
    )

def worker_level(plan: dict[str, object]) -> int:
    complexity_scores = {"low": 1, "medium": 3, "high": 5}
    risk_scores = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    try:
        complexity = complexity_scores[plan["complexity"]]
        risk = risk_scores[plan["risk"]]
    except (KeyError, TypeError) as error:
        raise ValueError("검증된 ExecPlan complexity와 risk가 필요합니다.") from error
    return min(complexity + risk, 7)


def _deserialize_review_level_history(
    value: object,
) -> dict[str, dict[str, int]]:
    if value in {None, ""}:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
    history: dict[str, dict[str, int]] = {}
    for level, records in parsed.items():
        if (
            not isinstance(level, str)
            or not level.isdigit()
            or not 1 <= int(level) <= 8
            or not isinstance(records, dict)
        ):
            raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
        normalized_records: dict[str, int] = {}
        for fingerprint, occurrences in records.items():
            if (
                not isinstance(fingerprint, str)
                or not fingerprint
                or type(occurrences) is not int
                or occurrences < 1
            ):
                raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
            normalized_records[fingerprint] = occurrences
        history[level] = normalized_records
    return history


def evaluate_review_worker_level(
    effective_level: int,
    review_history: dict[str, dict[str, int]],
    fingerprints: tuple[str, ...],
) -> WorkerLevelDecision:
    """Return the monotonic level transition for one valid Reviewer FAIL."""

    if type(effective_level) is not int or not 1 <= effective_level <= 8:
        raise ValueError(f"effective Worker level이 유효하지 않습니다: {effective_level}")
    if not fingerprints or any(not fingerprint for fingerprint in fingerprints):
        raise ValueError("Review 승격 평가에는 실패 지문이 하나 이상 필요합니다.")

    history = {
        level: dict(records)
        for level, records in review_history.items()
    }
    current = history.setdefault(str(effective_level), {})
    for fingerprint in sorted(set(fingerprints)):
        current[fingerprint] = current.get(fingerprint, 0) + 1

    should_escalate = (
        any(occurrences >= 2 for occurrences in current.values())
        or len(current) >= 3
    )
    next_level = (
        effective_level + 1
        if should_escalate and effective_level < 8
        else effective_level
    )
    return WorkerLevelDecision(effective_level, next_level, history)


def ensure_worker_level_state(
    connection: sqlite3.Connection,
    repo: str,
    plan: dict[str, object],
) -> int:
    """Backfill and validate the persisted initial/effective Worker levels."""

    catalog_plan = active_execplan(connection, repo)
    if catalog_plan is None:
        raise RuntimeError("Worker level을 저장할 활성 ExecPlan을 찾을 수 없습니다.")
    calculated_initial = worker_level(plan)
    initial = catalog_plan["initial_worker_level"]
    effective = catalog_plan["effective_worker_level"]
    if initial is None and effective is None:
        with connection:
            connection.execute(
                """
                UPDATE exec_plans
                SET initial_worker_level = ?, effective_worker_level = ?,
                    review_level_history = '{}'
                WHERE repo = ? AND plan_id = ?
                """,
                (
                    calculated_initial,
                    calculated_initial,
                    repo,
                    catalog_plan["plan_id"],
                ),
            )
        return calculated_initial
    if initial is None or effective is None:
        raise RuntimeError("저장된 Worker level 상태가 완전하지 않습니다.")
    if (
        type(initial) is not int
        or initial != calculated_initial
        or not 1 <= initial <= 7
        or type(effective) is not int
        or not initial <= effective <= 8
    ):
        raise RuntimeError("저장된 Worker level 상태가 ExecPlan과 일치하지 않습니다.")
    review_history = _deserialize_review_level_history(
        catalog_plan["review_level_history"]
    )
    if any(not initial <= int(level) <= effective for level in review_history):
        raise RuntimeError("저장된 Review 승격 이력이 Worker level과 일치하지 않습니다.")

    return effective


def codex_model_efforts(worktree: Path | None = None) -> dict[str, frozenset[str]]:
    try:
        catalog = json.loads(
            command(
                ["codex", "debug", "models"],
                cwd=worktree,
                env=agent_environment(),
            ).stdout
        )
        models = catalog["models"]
        return {
            model["slug"]: frozenset(level["effort"] for level in model["supported_reasoning_levels"])
            for model in models
        }
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Codex CLI model catalog 형식이 올바르지 않습니다.") from error


def provider_profile(
    config_path: Path,
    provider: str,
    section: str,
    name: str,
    label: str,
    model_efforts: Mapping[str, frozenset[str]],
) -> tuple[str, str]:
    """Resolve and validate one provider profile without invoking its CLI."""

    provider_labels = {"codex": "Codex", "claude": "Claude"}
    provider_label = provider_labels.get(provider)
    if provider_label is None or not section.startswith(f"{provider}."):
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    parser = configparser.ConfigParser()
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except configparser.Error as error:
        raise RuntimeError(
            f"{provider_label} 설정 파일 형식이 올바르지 않습니다: {config_path}"
        ) from error
    if not loaded:
        raise RuntimeError(f"설정 파일이 없습니다: {config_path}")
    if section not in parser:
        raise RuntimeError(f"{label} 설정 section [{section}]이 없습니다.")
    if name not in parser[section]:
        raise RuntimeError(f"{label} 설정이 없습니다: [{section}] {name}")

    try:
        value = parser[section][name]
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: [{section}] {name}"
        ) from error
    if not value.strip():
        raise RuntimeError(f"{label} Model alias/Effort 설정이 비어 있습니다.")
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise RuntimeError(f"{label} 설정은 `Model alias,Effort` 형식이어야 합니다.")
    alias, effort = parts
    if not alias:
        raise RuntimeError(f"{label} Model alias 설정이 비어 있습니다.")
    if not effort:
        raise RuntimeError(f"{label} Effort 설정이 비어 있습니다.")
    models_section = f"{provider}.models"
    if models_section not in parser:
        raise RuntimeError(
            f"{label} Model alias section [{models_section}]가 없습니다."
        )
    if alias not in parser[models_section]:
        raise RuntimeError(
            f"{label} Model alias가 [{models_section}]에 없습니다: {alias}"
        )
    try:
        model = parser[models_section][alias].strip()
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: "
            f"[{models_section}] {alias}"
        ) from error
    if not model:
        raise RuntimeError(
            f"{label} Model 설정이 비어 있습니다: [{models_section}] {alias}"
        )

    if section == f"{provider}.worker" and name == "level8":
        expected_alias, expected_effort = {
            "codex": ("frontier", "low"),
            "claude": ("strong", "max"),
        }[provider]
        if (alias, effort) != (expected_alias, expected_effort):
            raise RuntimeError(
                f"{label}은 {expected_alias},{expected_effort} 설정이어야 합니다."
            )
        if provider == "codex" and model != "gpt-6-astra":
            raise RuntimeError(f"{label} Model은 gpt-6-astra여야 합니다: {model}")
        if provider == "claude" and not re.fullmatch(
            r"claude-opus-5(?:[-.].*)?", model
        ):
            raise RuntimeError(
                f"{label} Model은 claude-opus-5 계열이어야 합니다: {model}"
            )

    supported = model_efforts.get(model)
    if supported is None:
        raise RuntimeError(
            f"{label} Model을 {provider_label} CLI가 지원하지 않습니다: {model}"
        )
    if effort not in supported:
        raise RuntimeError(
            f"{label} Model/Effort 조합을 {provider_label} CLI가 지원하지 않습니다: "
            f"{model},{effort}"
        )
    return model, effort


def provider_worker_profile(
    config_path: Path,
    provider: str,
    level: int,
    model_efforts: Mapping[str, frozenset[str]],
) -> tuple[str, str]:
    if type(level) is not int or not 1 <= level <= 8:
        raise RuntimeError(f"{provider.capitalize()} Worker level이 유효하지 않습니다: {level}")
    name = f"level{level}"
    return provider_profile(
        config_path,
        provider,
        f"{provider}.worker",
        name,
        f"{provider.capitalize()} Worker {name}",
        model_efforts,
    )


def codex_profile(
    config_path: Path,
    section: str,
    name: str,
    label: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return provider_profile(
        config_path,
        "codex",
        section,
        name,
        label,
        codex_model_efforts(worktree),
    )


def role(config_path: Path, name: str, worktree: Path | None = None) -> tuple[str, str]:
    return codex_profile(config_path, "codex.roles", name, f"Codex {name}", worktree)


def worker_profile(
    config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    if type(level) is not int or not 1 <= level <= 8:
        raise RuntimeError(f"Codex Worker level이 유효하지 않습니다: {level}")
    return provider_worker_profile(
        config_path,
        "codex",
        level,
        codex_model_efforts(worktree),
    )


def claude_model_efforts(
    config_path: Path,
    worktree: Path | None = None,
) -> dict[str, frozenset[str]]:
    """Build a catalog from configured profiles that the Claude CLI accepts."""

    parser = configparser.ConfigParser()
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except configparser.Error as error:
        raise RuntimeError(
            f"Claude 설정 파일 형식이 올바르지 않습니다: {config_path}"
        ) from error
    if not loaded:
        raise RuntimeError(f"설정 파일이 없습니다: {config_path}")
    if "claude.models" not in parser:
        raise RuntimeError("Claude Model alias section [claude.models]가 없습니다.")

    aliases: dict[str, str] = {}
    try:
        configured_models = parser["claude.models"].items()
        for alias, model in configured_models:
            resolved = model.strip()
            if not re.fullmatch(
                r"claude-(?:opus|sonnet|haiku)-[a-z0-9.-]+", resolved
            ):
                raise RuntimeError(
                    "Claude CLI가 지원하는 Model 이름 형식이 아닙니다: "
                    f"{resolved or '<empty>'}"
                )
            aliases[alias] = resolved
    except configparser.InterpolationError as error:
        raise RuntimeError("Claude Model alias 설정 값 형식이 올바르지 않습니다.") from error

    configured_pairs: set[tuple[str, str]] = set()
    for section in ("claude.roles", "claude.worker"):
        if section not in parser:
            continue
        for name in parser[section]:
            try:
                parts = [
                    part.strip() for part in parser[section][name].split(",")
                ]
            except configparser.InterpolationError as error:
                raise RuntimeError(
                    f"Claude 설정 값 형식이 올바르지 않습니다: [{section}] {name}"
                ) from error
            if len(parts) != 2:
                continue
            alias, effort = parts
            model = aliases.get(alias)
            if model is None or not effort:
                continue
            if effort not in CLAUDE_EFFORTS:
                raise RuntimeError(
                    "Claude Model/Effort 조합을 Claude CLI가 지원하지 않습니다: "
                    f"{model},{effort}"
                )
            configured_pairs.add((model, effort))

    environment = agent_environment("claude")
    for model in sorted(set(aliases.values())):
        result = command(
            ["claude", "--bare", "--init-only", "--model", model],
            cwd=worktree,
            check=False,
            env=environment,
        )
        if result.returncode:
            raise RuntimeError(f"Claude CLI가 Model을 지원하지 않습니다: {model}")

    catalog: dict[str, set[str]] = {
        model: set() for model in set(aliases.values())
    }
    for model, effort in sorted(configured_pairs):
        result = command(
            [
                "claude",
                "--bare",
                "--init-only",
                "--model",
                model,
                "--effort",
                effort,
            ],
            cwd=worktree,
            check=False,
            env=environment,
        )
        if result.returncode:
            raise RuntimeError(
                "Claude CLI가 Model/Effort 조합을 지원하지 않습니다: "
                f"{model},{effort}"
            )
        catalog[model].add(effort)

    return {
        model: frozenset(efforts) for model, efforts in catalog.items()
    }


def claude_role(
    config_path: Path,
    name: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return provider_profile(
        config_path,
        "claude",
        "claude.roles",
        name,
        f"Claude {name}",
        claude_model_efforts(config_path, worktree),
    )


def claude_worker_profile(
    config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return provider_worker_profile(
        config_path,
        "claude",
        level,
        claude_model_efforts(config_path, worktree),
    )


def agent_environment(
    provider: str = "codex", role_name: str | None = None
) -> dict[str, str]:
    """Return the filtered environment for one Agent subprocess role."""
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SSH_AUTH_SOCK",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    provider_allowed = {
        "codex": {
            "CODEX_ACCESS_TOKEN",
            "CODEX_CA_CERTIFICATE",
            "CODEX_HOME",
            "OPENAI_API_KEY",
        },
        "claude": {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR",
        },
    }
    if provider not in provider_allowed:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    allowed.update(provider_allowed[provider])

    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    if role_name == "planner":
        # A Planner often imports project modules while inspecting a planning
        # worktree. The scope snapshot deliberately includes ignored files,
        # so prevent Python's automatic cache writes at this subprocess
        # boundary rather than weakening that validation.
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def codex_sandbox_arguments(read_only: bool) -> list[str]:
    return ["--sandbox", "read-only"] if read_only else ["--approve-for-me"]


def run_codex(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
) -> tuple[int, str] | CodexResult:
    if role_name == "worker":
        if level is None:
            raise RuntimeError("Codex Worker level이 없습니다.")
        model, effort = worker_profile(config_path, level, worktree)
    else:
        if level is not None:
            raise RuntimeError(f"Codex {role_name}에 Worker level을 지정할 수 없습니다.")
        model, effort = role(config_path, role_name, worktree)
    project_dir = state_dir / "logs" / hashlib.sha256(str(worktree).encode()).hexdigest()[:16]
    project_dir.mkdir(parents=True, exist_ok=True)
    last_message = project_dir / f"{role_name}-last.txt"
    log = project_dir / f"{role_name}.log"
    last_message.unlink(missing_ok=True)
    if session_id:
        arguments = [
            "codex",
            "exec",
            *codex_sandbox_arguments(read_only),
            "-C",
            str(worktree),
            "resume",
            "--json",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-o",
            str(last_message),
            session_id,
            "-",
        ]
    else:
        arguments = [
            "codex",
            "exec",
            *(["--json"] if persist_session else ["--ephemeral"]),
            *codex_sandbox_arguments(read_only),
            "-C",
            str(worktree),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-o",
            str(last_message),
            "-",
        ]
    resolved_session_id = session_id
    timeout_triggered = threading.Event()
    if persist_session and not session_id:
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=agent_environment("codex", role_name),
                cwd=worktree,
            )
            assert process.stdin is not None
            assert process.stdout is not None

            def terminate_on_timeout() -> None:
                timeout_triggered.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timeout = threading.Timer(
                AGENT_CLI_TIMEOUT_SECONDS,
                terminate_on_timeout,
            )
            timeout.daemon = True
            timeout.start()
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    output.write(line)
                    output.flush()
                    if resolved_session_id:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "thread.started" and isinstance(
                        event.get("thread_id"), str
                    ):
                        resolved_session_id = event["thread_id"]
                        if on_session_started:
                            on_session_started(resolved_session_id)
                returncode = process.wait()
            except BaseException:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise
            finally:
                timeout.cancel()
                process.stdout.close()
    else:
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(
                arguments,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=output,
                stderr=subprocess.STDOUT,
                env=agent_environment("codex", role_name),
                cwd=worktree,
                timeout=AGENT_CLI_TIMEOUT_SECONDS,
            )
        returncode = result.returncode
    message = last_message.read_text(encoding="utf-8") if last_message.is_file() else ""
    if timeout_triggered.is_set():
        message = f"Codex {role_name} 실행 timeout ({AGENT_CLI_TIMEOUT_SECONDS}초)"
    if returncode and not message:
        message = log.read_text(encoding="utf-8")[-6000:]
    if persist_session and not resolved_session_id:
        if returncode == 0:
            return CodexResult(
                1,
                f"Codex {role_name} 실행 결과에서 세션 ID를 확인할 수 없습니다.",
            )
    if persist_session or session_id:
        return CodexResult(
            returncode,
            message.strip(),
            resolved_session_id,
            (
                AgentResultKind.TRANSIENT_FAILURE
                if timeout_triggered.is_set()
                else None
            ),
        )
    return returncode, message.strip()


def _agent_profile(
    config_path: Path,
    provider: str,
    role_name: str,
    level: int | None,
    worktree: Path,
) -> AgentProfile:
    if role_name == "worker":
        if level is None:
            raise RuntimeError(f"{provider.capitalize()} Worker level이 없습니다.")
        if provider == "codex":
            return AgentProfile(*worker_profile(config_path, level, worktree))
        if provider == "claude":
            return AgentProfile(
                *claude_worker_profile(config_path, level, worktree)
            )
    elif level is not None:
        raise RuntimeError(
            f"{provider.capitalize()} {role_name}에 Worker level을 지정할 수 없습니다."
        )
    elif provider == "codex":
        return AgentProfile(*role(config_path, role_name, worktree))
    elif provider == "claude":
        return AgentProfile(*claude_role(config_path, role_name, worktree))
    raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")


def validate_provider_profiles(
    config_path: Path,
    provider: str,
    model_efforts: Mapping[str, frozenset[str]],
) -> None:
    """Validate every configured role before an Agent state transition."""

    for role_name in ("planner", "reviewer"):
        provider_profile(
            config_path,
            provider,
            f"{provider}.roles",
            role_name,
            f"{provider.capitalize()} {role_name}",
            model_efforts,
        )
    for level in range(1, 9):
        provider_worker_profile(config_path, provider, level, model_efforts)


def validate_provider_runtime(
    config_path: Path,
    provider: str,
    worktree: Path | None = None,
) -> None:
    """Fail fast when the selected CLI, auth, or profiles are unavailable."""

    if provider not in AGENT_PROVIDERS:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    executable = "codex" if provider == "codex" else "claude"
    label = "Codex" if provider == "codex" else "Claude"
    if not shutil.which(executable):
        raise RuntimeError(f"선택한 {label} CLI가 PATH에 설치되어 있지 않습니다.")

    auth_arguments = (
        ["codex", "login", "status"]
        if provider == "codex"
        else ["claude", "auth", "status"]
    )
    auth = command(
        auth_arguments,
        cwd=worktree,
        check=False,
        env=agent_environment(provider),
    )
    if auth.returncode:
        login = "codex login" if provider == "codex" else "claude auth login"
        raise RuntimeError(
            f"선택한 {label} CLI가 인증되어 있지 않습니다. {login}을 실행하세요."
        )

    try:
        model_efforts = (
            codex_model_efforts(worktree)
            if provider == "codex"
            else claude_model_efforts(config_path, worktree)
        )
        validate_provider_profiles(config_path, provider, model_efforts)
    except RuntimeError as error:
        raise RuntimeError(f"{label} profile 설정 오류: {error}") from error


def claude_permission_arguments(read_only: bool) -> list[str]:
    if read_only:
        return [
            "--permission-mode",
            "plan",
            "--disallowedTools",
            "Edit",
            "Write",
            "NotebookEdit",
        ]
    return ["--permission-mode", "acceptEdits"]


def _claude_result_message(event: Mapping[str, object]) -> str:
    result = event.get("result")
    if isinstance(result, str):
        return result
    message = event.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, list):
            return "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
            )
    error = event.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        return str(error["message"])
    return ""


def run_claude(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
) -> tuple[int, str] | AgentResult:
    profile = _agent_profile(
        config_path, "claude", role_name, level, worktree
    )
    model, effort = profile.model, profile.effort
    project_dir = (
        state_dir
        / "logs"
        / hashlib.sha256(str(worktree).encode()).hexdigest()[:16]
    )
    project_dir.mkdir(parents=True, exist_ok=True)
    log = project_dir / f"claude-{role_name}.log"
    last_message = project_dir / f"claude-{role_name}-last.txt"
    last_message.unlink(missing_ok=True)

    streaming = persist_session and session_id is None
    arguments = [
        "claude",
        "--print",
        "--output-format",
        "stream-json" if streaming else "json",
        *(["--verbose"] if streaming else []),
        "--model",
        model,
        "--effort",
        effort,
        *claude_permission_arguments(read_only),
        *(["--resume", session_id] if session_id else []),
    ]
    resolved_session_id = session_id
    message = ""
    returncode = 0
    timeout_triggered = threading.Event()

    if streaming:
        final_event: Mapping[str, object] = {}
        stream_error_seen = False
        stream_error_message = ""
        diagnostic_tail = ""
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=agent_environment("claude", role_name),
                cwd=worktree,
            )
            assert process.stdin is not None
            assert process.stdout is not None

            def terminate_on_timeout() -> None:
                timeout_triggered.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timeout = threading.Timer(
                AGENT_CLI_TIMEOUT_SECONDS,
                terminate_on_timeout,
            )
            timeout.daemon = True
            timeout.start()
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    output.write(line)
                    output.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        diagnostic_tail = (diagnostic_tail + line)[-6000:]
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("is_error") or event.get("type") == "error":
                        stream_error_seen = True
                        candidate = _claude_result_message(event)
                        stream_error_message = candidate or line.strip()[-6000:]
                    if event.get("is_error") or event.get("type") in {
                        "error",
                        "result",
                    }:
                        diagnostic_tail = (diagnostic_tail + line)[-6000:]
                    event_session = event.get("session_id")
                    if (
                        resolved_session_id is None
                        and isinstance(event_session, str)
                        and event_session
                    ):
                        resolved_session_id = event_session
                        if on_session_started:
                            on_session_started(resolved_session_id)
                    if event.get("type") == "result":
                        final_event = event
                returncode = process.wait()
            except BaseException:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise
            finally:
                timeout.cancel()
                process.stdout.close()
        message = stream_error_message or _claude_result_message(final_event)
        if stream_error_seen and returncode == 0:
            returncode = 1
        if returncode and not message:
            message = diagnostic_tail
    else:
        result = subprocess.run(
            arguments,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=agent_environment("claude", role_name),
            cwd=worktree,
            timeout=AGENT_CLI_TIMEOUT_SECONDS,
        )
        returncode = result.returncode
        raw_output = result.stdout or ""
        log.write_text(raw_output, encoding="utf-8")
        try:
            event = json.loads(raw_output)
        except json.JSONDecodeError:
            event = {}
        if isinstance(event, dict):
            message = _claude_result_message(event)
            event_session = event.get("session_id")
            if isinstance(event_session, str) and event_session:
                resolved_session_id = event_session
            if event.get("is_error") and returncode == 0:
                returncode = 1
        if not message and returncode:
            message = raw_output[-6000:]

    if timeout_triggered.is_set():
        message = f"Claude {role_name} 실행 timeout ({AGENT_CLI_TIMEOUT_SECONDS}초)"
    if message:
        last_message.write_text(message, encoding="utf-8")
    if persist_session and not resolved_session_id and returncode == 0:
        return AgentResult(
            1,
            f"Claude {role_name} 실행 결과에서 세션 ID를 확인할 수 없습니다.",
        )
    if persist_session or session_id:
        return AgentResult(
            returncode,
            message.strip(),
            resolved_session_id,
            (
                AgentResultKind.TRANSIENT_FAILURE
                if timeout_triggered.is_set()
                else None
            ),
        )
    return returncode, message.strip()


class CodexAgentAdapter:
    name = "codex"

    def run(self, request: AgentRequest) -> tuple[int, str] | AgentResult:
        return run_codex(
            request.worktree,
            request.state_dir,
            request.config_path,
            request.role_name,
            request.prompt,
            read_only=request.read_only,
            level=request.level,
            persist_session=request.persist_session,
            session_id=request.session_id,
            on_session_started=request.on_session_started,
        )


class ClaudeAgentAdapter:
    name = "claude"

    def run(self, request: AgentRequest) -> tuple[int, str] | AgentResult:
        return run_claude(
            request.worktree,
            request.state_dir,
            request.config_path,
            request.role_name,
            request.prompt,
            read_only=request.read_only,
            level=request.level,
            persist_session=request.persist_session,
            session_id=request.session_id,
            on_session_started=request.on_session_started,
        )


_AGENT_ADAPTERS: dict[str, AgentProvider] = {
    "codex": CodexAgentAdapter(),
    "claude": ClaudeAgentAdapter(),
}


def agent_provider(name: str) -> AgentProvider:
    try:
        return _AGENT_ADAPTERS[name]
    except KeyError as error:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {name}") from error


def run_agent(
    provider: str,
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
) -> tuple[int, str] | AgentResult:
    """Execute one provider-neutral Agent request."""

    request = AgentRequest(
        worktree=worktree,
        state_dir=state_dir,
        config_path=config_path,
        role_name=role_name,
        prompt=prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=on_session_started,
    )
    return agent_provider(provider).run(request)


class VerifyOutput(str):
    """Bounded persisted feedback that retains full output for fingerprinting."""

    fingerprint_source: str

    def __new__(cls, output: str) -> VerifyOutput:
        fingerprint_source = output.strip()
        instance = super().__new__(cls, fingerprint_source[-8000:])
        instance.fingerprint_source = fingerprint_source
        return instance


def verify(worktree: Path) -> tuple[bool, str]:
    if os.name == "nt":
        script = worktree / "scripts/verify.ps1"
        executable = shutil.which("pwsh") or shutil.which("powershell")
        arguments = [executable or "pwsh", "-NoProfile", "-File", str(script)]
    else:
        script = worktree / "scripts/verify.sh"
        arguments = ["bash", str(script)]
    if not script.is_file():
        return False, f"전체 검증 스크립트가 없습니다: {script.relative_to(worktree)}"
    result = command(arguments, cwd=worktree, check=False)
    return result.returncode == 0, VerifyOutput(result.stdout)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP_NOISE = re.compile(
    r"(?ix)"
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
)
_DURATION_NOISE = re.compile(
    r"(?i)\b(?:in|after|elapsed[:=]?)\s+\d+(?:[.,]\d+)?\s*(?:ms|s|sec|secs|seconds?)\b"
)
_TEMP_PATH_NOISE = re.compile(
    r"(?i)(?<![\w])"
    r"(?P<root>[a-z]:[\\/][^\r\n]*?[\\/]temp|/(?:private/)?(?:var/)?tmp)"
    r"(?P<tail>[\\/][^\s:]+)"
)
_TEMP_DIRECTORY_CONTEXT = re.compile(
    r"(?i)(?:\b(?:temporary\s+directory|directory|workspace|worktree)\b"
    r"|디렉터리|작업\s*공간)"
)


_REVIEW_ISSUE_ID = re.compile(
    r"(?mi)^[ \t]*ISSUE_ID:[ \t]*(?P<identifier>[^\r\n]+?)[ \t]*$"
)
_REVIEW_ISSUE_DECLARATION = re.compile(r"(?i)^[ \t]*ISSUE_ID\b")
_REVIEW_ISSUE_IDENTIFIER = re.compile(
    r"(?i)^(?!/)(?!.*(?:^|/)\.\.(?:/|$))"
    r"[a-z0-9_.\-/]+::[a-z0-9_.<>/-]+::[a-z0-9][a-z0-9-]*$"
)
_REVIEW_VERDICT_DECLARATION = re.compile(r"(?i)^[ \t]*VERDICT\b")
_REVIEW_VERDICT = re.compile(r"(?i)^[ \t]*VERDICT:[ \t]*(PASS|FAIL)[ \t]*$")


def _normalize_review_issue_ids(output: str) -> tuple[str, ...]:
    output = output.replace("\r\n", "\n").replace("\r", "\n")
    declarations = [
        line
        for line in output.splitlines()
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    if not declarations:
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과에 유효한 ISSUE_ID가 없습니다."
        )
    matches = list(_REVIEW_ISSUE_ID.finditer(output))
    if len(matches) != len(declarations):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    raw_identifiers = [
        " ".join(match.group("identifier").split()).casefold()
        for match in matches
    ]
    if any(
        _REVIEW_ISSUE_IDENTIFIER.fullmatch(identifier) is None
        for identifier in raw_identifiers
    ):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    identifiers = set(raw_identifiers)
    return tuple(f"ISSUE_ID:{identifier}" for identifier in sorted(identifiers))


def parse_reviewer_result(output: str) -> tuple[str, tuple[str, ...]]:
    """Validate the Reviewer response contract before any state transition."""

    lines = output.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    meaningful_lines = [
        (index, line)
        for index, line in enumerate(lines)
        if line.strip()
    ]
    verdict_lines = [
        (index, line)
        for index, line in meaningful_lines
        if _REVIEW_VERDICT_DECLARATION.match(line)
    ]
    if len(verdict_lines) != 1:
        raise ReviewerProtocolError(
            "Reviewer 결과는 정확히 하나의 VERDICT를 포함해야 합니다."
        )
    verdict_index, verdict_line = verdict_lines[0]
    verdict_match = _REVIEW_VERDICT.fullmatch(verdict_line)
    if verdict_match is None:
        raise ReviewerProtocolError(
            "Reviewer VERDICT는 PASS 또는 FAIL이어야 합니다."
        )
    if verdict_index != meaningful_lines[-1][0]:
        raise ReviewerProtocolError(
            "Reviewer VERDICT는 마지막 비어 있지 않은 줄이어야 합니다."
        )

    verdict = verdict_match.group(1).upper()
    issue_declarations = [
        line
        for _index, line in meaningful_lines
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    if verdict == "PASS":
        if issue_declarations:
            raise ReviewerProtocolError(
                "Reviewer PASS 결과에는 ISSUE_ID가 없어야 합니다."
            )
        return verdict, ()
    return verdict, _normalize_review_issue_ids(output)


def _normalize_temp_path(match: re.Match[str]) -> str:
    tail = match.group("tail").strip("\\/")
    components = [part for part in re.split(r"[\\/]", tail) if part]
    leaf = components[-1]
    line_start = match.string.rfind("\n", 0, match.start()) + 1
    prefix = match.string[line_start:match.start()]
    if len(components) == 1 and _TEMP_DIRECTORY_CONTEXT.search(prefix):
        return "<TEMP_PATH>"
    return f"<TEMP_PATH>/{leaf}"


def normalize_validation_failure(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> str:
    """Remove unstable execution noise and make failure line ordering irrelevant."""

    if stage not in {"verify", "review"}:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    fingerprint_source = (
        output.fingerprint_source if isinstance(output, VerifyOutput) else output
    )
    normalized = fingerprint_source.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ANSI_ESCAPE.sub("", normalized)
    if worktree is not None:
        path_values = {
            str(worktree),
            str(worktree.resolve()),
            str(worktree).replace("\\", "/"),
            str(worktree).replace("/", "\\"),
        }
        for path_value in sorted(path_values, key=len, reverse=True):
            if path_value:
                normalized = re.sub(
                    re.escape(path_value),
                    "<WORKTREE>",
                    normalized,
                    flags=re.IGNORECASE,
                )
    normalized = _TEMP_PATH_NOISE.sub(_normalize_temp_path, normalized)
    normalized = _TIMESTAMP_NOISE.sub("<TIMESTAMP>", normalized)
    normalized = _DURATION_NOISE.sub("<DURATION>", normalized)
    if stage == "review":
        return "\n".join(_normalize_review_issue_ids(normalized))
    lines = []
    for line in normalized.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        lines.append(line)
    return "\n".join(sorted(lines))


def validation_failure_fingerprints(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    normalized = normalize_validation_failure(stage, output, worktree)
    failures = normalized.splitlines() if stage == "review" else [normalized]
    fingerprints = []
    for failure in failures:
        digest = hashlib.sha256()
        digest.update(b"loop-manager-validation-failure-v1\0")
        digest.update(stage.encode("utf-8"))
        digest.update(b"\0")
        digest.update(failure.encode("utf-8"))
        fingerprints.append((digest.hexdigest(), failure))
    return tuple(fingerprints)


def validation_failure_fingerprint(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    fingerprints = validation_failure_fingerprints(stage, output, worktree)
    if len(fingerprints) != 1:
        raise ValueError(
            "복수 Review issue는 validation_failure_fingerprints()로 처리해야 합니다."
        )
    return fingerprints[0]


def _deserialize_failure_history(value: object) -> dict[str, dict[str, object]]:
    if value in {None, ""}:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
    history: dict[str, dict[str, object]] = {}
    for fingerprint, record in parsed.items():
        if not isinstance(fingerprint, str) or not isinstance(record, dict):
            raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
        occurrences = record.get("occurrences")
        if type(occurrences) is not int or occurrences < 1:
            raise RuntimeError("저장된 검증 실패 반복 횟수가 유효하지 않습니다.")
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": str(record.get("sample", "")),
        }
    return history


def clear_validation_failure_history(
    connection: sqlite3.Connection,
    repo: str,
    stage: str,
) -> None:
    column = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }.get(stage)
    if column is None:
        raise ValueError(f"검증 성공 단계가 유효하지 않습니다: {stage}")
    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("지문 이력을 초기화할 활성 ExecPlan을 찾을 수 없습니다.")
    with connection:
        connection.execute(
            f"UPDATE exec_plans SET {column} = '{{}}' "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan["plan_id"]),
        )


def pr_for_branch(repo: Path, branch: str, url: str | None = None) -> dict[str, object] | None:
    fields = "url,state,mergedAt,headRefOid,headRefName,mergeable"
    if url:
        result = command(["gh", "pr", "view", url, "--json", fields], cwd=repo)
        return json.loads(result.stdout)
    result = command(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", fields],
        cwd=repo,
    )
    rows = json.loads(result.stdout)
    return rows[0] if rows else None


def classify_pull_request_mergeability(
    pull_request: Mapping[str, object],
) -> str:
    """Normalize GitHub's asynchronous mergeability result."""

    value = str(pull_request.get("mergeable") or "UNKNOWN").upper()
    if value == "CONFLICTING":
        return "conflicting"
    if value == "MERGEABLE":
        return "mergeable"
    return "unknown"


def _fetch_pr_conflict_recovery_refs(
    repo_path: Path,
    base: str,
    branch: str,
) -> None:
    fetched = git(
        repo_path,
        "fetch",
        "origin",
        f"+refs/heads/{base}:refs/remotes/origin/{base}",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
        check=False,
    )
    if fetched.returncode:
        raise RuntimeError(
            "PR 충돌 복구에 필요한 원격 base/head ref를 가져올 수 없습니다:\n"
            + fetched.stdout[-8000:]
        )


def _validate_pr_conflict_recovery_ownership(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> tuple[sqlite3.Row, Path, str, str, str]:
    """Prove the saved URL, branch, workspace, and observed remote head."""

    plan = active_execplan(connection, repo)
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    url = str(pull_request.get("url") or "")
    head_name = str(pull_request.get("headRefName") or "")
    head_oid = str(pull_request.get("headRefOid") or "")
    stored_url = str(project["pr_url"] or "")
    stored_head = str(project["pr_head_oid"] or "")
    if (
        plan is None
        or plan["status"] != "PR_OPEN"
        or plan_id is None
        or str(plan["plan_id"]) != plan_id
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "PR 충돌 복구 workspace의 SQLite 소유권을 확인할 수 없습니다."
        )
    if not stored_url or url != stored_url:
        raise PublicationSafetyError(
            "PR 충돌 복구 URL이 저장된 소유 PR과 일치하지 않습니다."
        )
    if not branch or head_name != branch:
        raise PublicationSafetyError(
            "PR 충돌 복구 head branch가 관리 branch와 일치하지 않습니다."
        )
    if (
        not head_oid
        or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head_oid) is None
        or not stored_head
        or head_oid != stored_head
    ):
        raise PublicationSafetyError(
            "PR 충돌 복구 head OID가 저장된 PR head와 일치하지 않습니다."
        )
    validate_execplan_workspace_identity(
        state_dir, repo, plan_id, branch, worktree
    )
    make_worktree(
        repo_path,
        worktree,
        branch,
        str(project["base_branch"]),
        allow_create_branch=False,
        allow_recovery=True,
        allow_existing=True,
    )
    snapshot = capture_git_snapshot(worktree)
    if snapshot.head != head_oid or snapshot.dirty or snapshot.operations:
        raise PublicationSafetyError(
            "PR 충돌 복구 시작 전 관리 worktree가 관측한 원격 head의 "
            "clean 상태와 일치하지 않습니다."
        )
    remote_oid = remote_branch_oid(repo_path, branch)
    if remote_oid != head_oid:
        raise PublicationSafetyError(
            "PR 충돌 복구 시작 전 원격 branch가 관측한 PR head와 "
            "일치하지 않습니다."
        )
    base_oid = git(
        repo_path,
        "rev-parse",
        f"refs/remotes/origin/{project['base_branch']}^{{commit}}",
    ).stdout.strip()
    return plan, worktree, branch, head_oid, base_oid


def _validate_active_pr_conflict_recovery(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> None:
    """Revalidate races without resetting an in-progress recovery stage."""

    plan = active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    observed_head = str(project["pr_conflict_head_oid"] or "")
    if (
        plan is None
        or not int(plan["pr_conflict_recovery"])
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "진행 중인 PR 충돌 복구의 SQLite 소유권을 확인할 수 없습니다."
        )
    if (
        str(pull_request.get("url") or "") != str(project["pr_url"] or "")
        or str(pull_request.get("headRefName") or "") != branch
        or not observed_head
        or str(pull_request.get("headRefOid") or "") != observed_head
    ):
        raise PublicationSafetyError(
            "진행 중인 PR의 URL, head branch 또는 head OID가 복구 시작 시 "
            "관측값과 달라졌습니다."
        )
    _fetch_pr_conflict_recovery_refs(
        repo_path, str(project["base_branch"]), branch
    )
    if remote_branch_oid(repo_path, branch) != observed_head:
        raise PublicationSafetyError(
            "진행 중인 PR의 원격 head가 복구 시작 시 관측값과 달라졌습니다."
        )
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    make_worktree(
        repo_path,
        worktree,
        branch,
        str(project["base_branch"]),
        allow_create_branch=False,
        allow_recovery=True,
        allow_existing=True,
        allow_rebase=_rebase_in_progress_for_branch(worktree, branch),

    )

def remote_branch_oid(repo: Path, branch: str) -> str | None:
    """Return the fetched remote branch OID without consulting local branch names."""

    result = git(
        repo,
        "show-ref",
        "--hash",
        f"refs/remotes/origin/{branch}",
        check=False,
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        raise RuntimeError(
            f"원격 publication branch 확인 실패 (git 종료 코드 {result.returncode})."
        )
    oid = result.stdout.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid) is None:
        raise RuntimeError("원격 publication branch OID 형식이 유효하지 않습니다.")
    return oid


def _owned_publication_plan(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
) -> sqlite3.Row:
    plan = active_execplan(connection, repo)
    if (
        plan is None
        or plan["status"] != "PUBLISHING"
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "publication branch의 Loop Manager 소유권을 SQLite에서 확인할 수 없습니다."
        )
    validate_execplan_workspace_identity(
        state_dir,
        repo,
        str(plan["plan_id"]),
        branch,
        worktree,
    )
    validate_managed_worktree(repo_path, worktree, branch)
    return plan


def record_remote_publication_branch(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    remote_oid: str | None,
) -> None:
    """Remember only a remote branch proven to be this managed branch."""

    if remote_oid is None:
        return
    plan = _owned_publication_plan(
        connection, repo_path, state_dir, repo, branch, worktree
    )
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    recorded_oid = str(plan["publication_remote_oid"] or "")
    if remote_oid == local_oid:
        if recorded_oid != remote_oid:
            update_project(
                connection,
                repo,
                publication_remote_oid=remote_oid,
            )
        return
    if recorded_oid == remote_oid:
        return
    raise PublicationSafetyError(
        "PR이 없는 원격 branch가 현재 관리 HEAD 또는 저장된 게시 OID와 "
        f"일치하지 않아 갱신을 거부합니다: {branch}"
    )


def push_managed_publication_branch(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
) -> str:
    """Push idempotently, using an explicit lease only for owned PR-less recovery."""

    plan = _owned_publication_plan(
        connection, repo_path, state_dir, repo, branch, worktree
    )
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    remote_oid = remote_branch_oid(repo_path, branch)
    record_remote_publication_branch(
        connection,
        repo_path,
        state_dir,
        repo,
        branch,
        worktree,
        remote_oid,
    )
    if remote_oid == local_oid:
        return local_oid

    push_arguments = [
        "push",
        "-u",
        "origin",
        f"HEAD:refs/heads/{branch}",
    ]
    if remote_oid is not None:
        recorded_oid = str(plan["publication_remote_oid"] or "")
        if recorded_oid != remote_oid:
            refreshed = active_execplan(connection, repo)
            recorded_oid = str(
                refreshed["publication_remote_oid"] if refreshed else ""
            )
        if recorded_oid != remote_oid:
            raise PublicationSafetyError(
                "저장된 원격 branch OID와 fetch 결과가 달라 강제 갱신을 거부합니다."
            )
        push_arguments.insert(
            1,
            f"--force-with-lease=refs/heads/{branch}:{remote_oid}",
        )

    pushed = git(worktree, *push_arguments, check=False)
    if pushed.returncode:
        raise RuntimeError(f"publication branch push 실패:\n{pushed.stdout[-8000:]}")

    git(
        repo_path,
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    )
    confirmed_oid = remote_branch_oid(repo_path, branch)
    if confirmed_oid != local_oid:
        raise RuntimeError(
            "publication branch push 결과를 원격에서 확인할 수 없습니다."
        )
    update_project(
        connection,
        repo,
        publication_remote_oid=local_oid,
    )
    return local_oid


def push_owned_pr_conflict_recovery(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    pull_request: Mapping[str, object],
) -> str:
    """Rewrite only the proven owned PR head with its observed explicit lease."""

    plan = active_execplan(connection, repo)
    project = connection.execute(
        "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if (
        project is None
        or plan is None
        or plan["status"] != "PUBLISHING"
        or not int(plan["pr_conflict_recovery"])
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "기존 PR 충돌 복구 게시 소유권을 SQLite에서 확인할 수 없습니다."
        )
    url = str(plan["pr_url"] or "")
    observed_head = str(plan["pr_conflict_head_oid"] or "")
    target_base = str(plan["pr_conflict_base_oid"] or "")
    if (
        str(pull_request.get("state") or "").upper() != "OPEN"
        or str(pull_request.get("url") or "") != url
        or str(pull_request.get("headRefName") or "") != branch
        or str(pull_request.get("headRefOid") or "") != observed_head
    ):
        raise PublicationSafetyError(
            "게시 직전 PR URL, state, head branch 또는 head OID가 복구 "
            "시작 시 관측값과 다릅니다."
        )
    if (
        not observed_head
        or not target_base
        or str(plan["active_sha"] or "") != target_base
        or str(plan["validated_base_sha"] or "") != target_base
    ):
        raise PublicationSafetyError(
            "기존 PR 충돌 복구의 lease OID와 검증 base가 완전하지 않습니다."
        )
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    validate_managed_worktree(repo_path, worktree, branch)
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    contains_base = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        local_oid,
        check=False,
    )
    if contains_base.returncode != 0:
        raise PublicationSafetyError(
            "검증된 base OID가 게시할 관리 HEAD의 조상이 아닙니다."
        )

    _fetch_pr_conflict_recovery_refs(
        repo_path,
        str(project["base_branch"]),
        branch,
    )
    remote_oid = remote_branch_oid(repo_path, branch)
    if remote_oid != observed_head:
        raise PublicationSafetyError(
            "게시 직전 원격 PR head가 복구 시작 시 관측값과 달라졌습니다."
        )
    lease = f"--force-with-lease=refs/heads/{branch}:{observed_head}"
    pushed = git(
        worktree,
        "push",
        lease,
        "-u",
        "origin",
        f"HEAD:refs/heads/{branch}",
        check=False,
    )
    if pushed.returncode:
        normalized = pushed.stdout.casefold()
        if any(
            marker in normalized
            for marker in ("stale info", "fetch first", "rejected", "lease")
        ):
            raise PublicationSafetyError(
                "기존 PR branch lease가 거부되어 원격 변경을 덮어쓰지 않았습니다:\n"
                + pushed.stdout[-8000:]
            )
        raise RuntimeError(
            f"기존 PR 충돌 복구 push 실패:\n{pushed.stdout[-8000:]}"
        )

    git(
        repo_path,
        "fetch",
        "origin",
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    )
    if remote_branch_oid(repo_path, branch) != local_oid:
        raise RuntimeError(
            "기존 PR 충돌 복구 push 결과를 원격에서 확인할 수 없습니다."
        )
    return local_oid

def recover_confirmed_pr_conflict_push(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> bool:
    """Recover only a previously pushed local HEAD, never an arbitrary remote head."""

    if (
        project["phase"] != "PUBLISHING"
        or not int(project["pr_conflict_recovery"])
    ):
        return False
    plan = active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    remote_head = str(pull_request.get("headRefOid") or "")
    observed_head = str(project["pr_conflict_head_oid"] or "")
    target_base = str(project["pr_conflict_base_oid"] or "")
    if (
        plan is None
        or plan["status"] != "PUBLISHING"
        or not int(plan["pr_conflict_recovery"])
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(pull_request.get("state") or "").upper() != "OPEN"
        or str(pull_request.get("url") or "") != str(project["pr_url"] or "")
        or str(pull_request.get("headRefName") or "") != branch
        or not remote_head
        or remote_head == observed_head
        or not target_base
        or str(project["active_sha"] or "") != target_base
        or str(project["validated_base_sha"] or "") != target_base
    ):
        return False
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    validate_managed_worktree(repo_path, worktree, branch)
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if remote_head != local_oid:
        return False
    contains_base = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        local_oid,
        check=False,
    )
    if contains_base.returncode != 0:
        return False
    _fetch_pr_conflict_recovery_refs(
        repo_path, str(project["base_branch"]), branch
    )
    if remote_branch_oid(repo_path, branch) != local_oid:
        return False
    update_project(
        connection,
        repo,
        phase="PR_OPEN",
        publication_remote_oid=local_oid,
        pr_head_oid=local_oid,
        pr_conflict_recovery=0,
        pr_conflict_head_oid=None,
        pr_conflict_base_oid=None,
        last_error=None,
    )
    return True

def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)




def _validate_managed_path_boundary(
    state_dir: Path,
    worktree: Path,
    expected: Path,
    label: str,
) -> None:
    lexical_worktree = os.path.normcase(
        os.path.abspath(worktree.expanduser())
    )
    lexical_expected = os.path.normcase(
        os.path.abspath(expected.expanduser())
    )
    if lexical_worktree != lexical_expected:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 결정적 소유 경계와 다릅니다: "
            f"{worktree} (expected {expected})"
        )
    state_root = _canonical_path(state_dir)
    resolved = _canonical_path(worktree)
    try:
        resolved.relative_to(state_root)
    except ValueError as error:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 Loop Manager 상태 디렉터리 밖을 가리킵니다: "
            f"{worktree}"
        ) from error

def _worktree_catalog(repo: Path) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    if current:
        records.append(current)
    return records


def _rebase_head_name(worktree: Path) -> str | None:
    for directory in ("rebase-merge", "rebase-apply"):
        result = git(worktree, "rev-parse", "--git-path", f"{directory}/head-name", check=False)
        if result.returncode:
            continue
        git_path = Path(result.stdout.strip())
        if not git_path.is_absolute():
            git_path = worktree / git_path
        if git_path.is_file():
            return git_path.read_text(encoding="utf-8").strip()
    return None


def _rebase_in_progress_for_branch(worktree: Path, branch: str) -> bool:
    return _rebase_head_name(worktree) == f"refs/heads/{branch}"


def validate_managed_worktree(
    repo: Path,
    worktree: Path,
    branch: str | None,
    *,
    allow_rebase: bool = False,
) -> None:
    """Prove that a path is registered to this repository and expected branch."""

    if not worktree.is_dir():
        raise WorkspaceOwnershipError(f"관리 worktree 경로가 없습니다: {worktree}")
    expected_path = _canonical_path(worktree)
    record = next(
        (
            item
            for item in _worktree_catalog(repo)
            if _canonical_path(Path(str(item.get("worktree", "")))) == expected_path
        ),
        None,
    )
    if record is None:
        raise WorkspaceOwnershipError(
            f"Git에 등록되지 않은 관리 worktree입니다: {worktree}"
        )
    actual_branch = record.get("branch")
    if branch is None:
        if "detached" not in record:
            raise WorkspaceOwnershipError(
                f"planning worktree가 detached HEAD가 아닙니다: {worktree}"
            )
    elif actual_branch != f"refs/heads/{branch}":
        if (
            allow_rebase
            and "detached" in record
            and _rebase_in_progress_for_branch(worktree, branch)
        ):
            return
        rendered = actual_branch or "detached"
        raise WorkspaceOwnershipError(
            f"관리 worktree branch 불일치: {worktree} "
            f"(expected refs/heads/{branch}, actual {rendered})"
        )

def validate_reserved_worktree(
    repo: Path,
    worktree: Path,
    branch: str | None,
    expected_head: str,
) -> None:
    """Prove that a reserved path is the clean worktree Manager intended."""

    if not expected_head:
        raise WorkspaceOwnershipError(
            "예약 workspace의 기준 commit SHA가 저장되지 않았습니다."
        )
    validate_managed_worktree(repo, worktree, branch)
    snapshot = capture_git_snapshot(worktree)
    violations: list[str] = []
    if snapshot.head != expected_head:
        violations.append(
            f"HEAD {snapshot.head} (expected {expected_head})"
        )
    if snapshot.dirty:
        paths = ", ".join(sorted(snapshot.dirty)[:8])
        violations.append(f"변경 파일 {paths}")
    if snapshot.operations:
        operations = ", ".join(sorted(snapshot.operations))
        violations.append(f"Git operation {operations}")
    if violations:
        raise WorkspaceOwnershipError(
            "예약 workspace가 저장된 생성 기준과 다릅니다: "
            + "; ".join(violations)
        )



def make_worktree(
    repo: Path,
    worktree: Path,
    branch: str,
    base: str,
    *,
    allow_recovery: bool = True,
    allow_existing: bool = True,
    allow_rebase: bool = False,
    allow_create_branch: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
    before_create: Callable[[], None] | None = None,
) -> str:
    """Create, recover, or validate one owned ExecPlan worktree."""

    if ownership_reserved and not expected_head:
        raise WorkspaceOwnershipError(
            "예약 ExecPlan workspace의 기준 commit SHA가 없습니다."
        )
    if worktree.exists() and not worktree.is_dir():
        raise WorkspaceOwnershipError(
            f"관리 worktree 경로가 디렉터리가 아닙니다: {worktree}"
        )
    if worktree.is_dir():
        if not allow_existing:
            raise WorkspaceOwnershipError(
                f"새 ExecPlan의 관리 worktree가 이미 존재해 소유권을 증명할 수 없습니다: "
                f"{worktree}"
            )
        if ownership_reserved:
            validate_reserved_worktree(repo, worktree, branch, str(expected_head))
        else:
            validate_managed_worktree(
                repo, worktree, branch, allow_rebase=allow_rebase
            )
        return "existing"

    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "prune", "--expire", "now")
    branch_exists = (
        git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode
        == 0
    )
    if branch_exists:
        if (allow_create_branch and not ownership_reserved) or not allow_recovery:
            raise WorkspaceOwnershipError(
                f"새 ExecPlan의 관리 branch가 이미 존재해 소유권을 증명할 수 없습니다: "
                f"{branch}"
            )
        for record in _worktree_catalog(repo):
            if record.get("branch") == f"refs/heads/{branch}":
                owner = Path(str(record["worktree"]))
                raise WorkspaceOwnershipError(
                    f"관리 branch가 다른 worktree에 연결되어 있습니다: "
                    f"{branch} ({owner})"
                )
        if ownership_reserved:
            branch_head = git(
                repo,
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}",
            ).stdout.strip()
            if branch_head != expected_head:
                raise WorkspaceOwnershipError(
                    "예약 ExecPlan branch가 저장된 기준 commit과 다릅니다: "
                    f"{branch} ({branch_head}, expected {expected_head})"
                )
        git(repo, "worktree", "add", str(worktree), branch)
        outcome = "recovered"
    elif allow_create_branch or ownership_reserved:
        if before_create is not None:
            before_create()
        start_point = expected_head or f"origin/{base}"
        git(repo, "worktree", "add", "-b", branch, str(worktree), start_point)
        outcome = "created"
    else:
        raise WorkspaceOwnershipError(
            f"저장된 worktree와 관리 branch를 모두 복구할 수 없습니다: "
            f"{worktree} ({branch})"
        )
    if ownership_reserved:
        validate_reserved_worktree(repo, worktree, branch, str(expected_head))
    else:
        validate_managed_worktree(repo, worktree, branch, allow_rebase=allow_rebase)
    return outcome


def make_planning_worktree(
    repo: Path,
    worktree: Path,
    base: str,
    *,
    allow_create: bool = True,
    before_create: Callable[[], None] | None = None,
    allow_existing: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
) -> str:
    if ownership_reserved and not expected_head:
        raise WorkspaceOwnershipError(
            "예약 planning workspace의 기준 commit SHA가 없습니다."
        )
    if worktree.exists() and not worktree.is_dir():
        raise WorkspaceOwnershipError(
            f"planning worktree 경로가 디렉터리가 아닙니다: {worktree}"
        )
    if worktree.is_dir():
        if not allow_existing:
            raise WorkspaceOwnershipError(
                "새 planning worktree 경로가 이미 존재해 소유권을 증명할 수 "
                f"없습니다: {worktree}"
            )
        if ownership_reserved:
            validate_reserved_worktree(repo, worktree, None, str(expected_head))
        else:
            validate_managed_worktree(repo, worktree, None)
        return "existing"
    if not allow_create:
        raise WorkspaceOwnershipError(
            f"보존된 planning worktree를 복구할 branch가 없습니다: "
            f"{worktree}"
        )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if before_create is not None:
        before_create()
    start_point = expected_head or f"origin/{base}"
    git(repo, "worktree", "add", "--detach", str(worktree), start_point)
    if ownership_reserved:
        validate_reserved_worktree(repo, worktree, None, str(expected_head))
    else:
        validate_managed_worktree(repo, worktree, None)
    return "created"


def expected_execplan_workspace(
    state_dir: Path,
    repo: str,
    plan_id: str,
) -> tuple[str, Path]:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return (
        f"loop-manager/{plan_id}",
        state_dir / "worktrees" / project_id / plan_id,
    )


def validate_execplan_workspace_identity(
    state_dir: Path,
    repo: str,
    plan_id: str,
    branch: str,
    worktree: Path,
) -> None:
    expected_branch, expected_worktree = expected_execplan_workspace(
        state_dir, repo, plan_id
    )
    if branch != expected_branch:
        raise WorkspaceOwnershipError(
            f"ExecPlan 관리 branch가 결정적 소유 경계와 다릅니다: "
            f"{branch} (expected {expected_branch})"
        )
    _validate_managed_path_boundary(
        state_dir,
        worktree,
        expected_worktree,
        "ExecPlan worktree",
    )


def _nul_git_paths(repo: Path, *arguments: str) -> list[str]:
    return [
        os.fsdecode(record)
        for record in git_bytes(repo, *arguments).split(b"\0")
        if record
    ]


def _index_entries(worktree: Path) -> dict[str, tuple[str, ...]]:
    entries: dict[str, list[str]] = {}
    for record in git_bytes(
        worktree, "ls-files", "--stage", "-v", "-z"
    ).split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise RuntimeError("Git index snapshot을 해석할 수 없습니다.")
        entries.setdefault(os.fsdecode(raw_path), []).append(
            metadata.decode("ascii", errors="replace")
        )
    return {
        path: tuple(sorted(metadata))
        for path, metadata in entries.items()
    }


def _non_default_index_flags(entries: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            entry.split(" ", 1)[0]
            for entry in entries
            if not entry.startswith("H ")
        )
    )


def _shared_ref_entries(worktree: Path) -> dict[str, tuple[str, str]]:
    refs: dict[str, tuple[str, str]] = {}
    output = git(
        worktree,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(symref)",
    ).stdout
    for line in output.splitlines():
        fields = line.split("\0")
        if len(fields) != 3 or not fields[0]:
            raise RuntimeError("Git ref snapshot을 해석할 수 없습니다.")
        refname, objectname, symref = fields
        if refname in refs:
            raise RuntimeError(f"Git ref snapshot에 중복 ref가 있습니다: {refname}")
        refs[refname] = (objectname, symref)
    return refs


def _registered_worktree_entries(
    worktree: Path,
) -> dict[str, tuple[str, ...]]:
    entries: dict[str, tuple[str, ...]] = {}
    active_path = _canonical_path(worktree)
    for record in _worktree_catalog(worktree):
        raw_path = record.get("worktree")
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeError("Git worktree snapshot에 경로가 없습니다.")
        path = str(_canonical_path(Path(raw_path)))
        if path in entries:
            raise RuntimeError(
                f"Git worktree snapshot에 중복 경로가 있습니다: {path}"
            )
        state = [
            f"{key}={value}" for key, value in record.items() if key != "worktree"
        ]
        if _canonical_path(Path(raw_path)) != active_path:
            state.append(f"state={_worktree_state_signature(Path(raw_path))}")
        entries[path] = tuple(sorted(state))
    return entries


def _normalized_config_origin(worktree: Path, raw_origin: bytes) -> bytes:
    if not raw_origin.startswith(b"file:"):
        return raw_origin
    origin_path = Path(os.fsdecode(raw_origin.removeprefix(b"file:")))
    if not origin_path.is_absolute():
        origin_path = worktree / origin_path
    return b"file:" + os.fsencode(_canonical_path(origin_path))


def _config_entries(worktree: Path) -> dict[str, tuple[str, ...]]:
    records = git_bytes(
        worktree,
        "config",
        "--null",
        "--show-origin",
        "--show-scope",
        "--list",
        "--includes",
    ).split(b"\0")
    if records and not records[-1]:
        records.pop()
    if len(records) % 3:
        raise RuntimeError("Git config snapshot을 해석할 수 없습니다.")

    entries: dict[str, list[str]] = {}
    for offset in range(0, len(records), 3):
        raw_scope, raw_origin, definition = records[offset : offset + 3]
        raw_name, separator, raw_value = definition.partition(b"\n")
        if not raw_scope or not raw_origin or not raw_name or not separator:
            raise RuntimeError("Git config snapshot을 해석할 수 없습니다.")
        scope = raw_scope.decode("ascii", errors="replace")
        name = raw_name.decode("utf-8", errors="replace")
        origin = _normalized_config_origin(worktree, raw_origin)
        digest = hashlib.sha256(origin + b"\0" + raw_value).hexdigest()
        entries.setdefault(f"{scope}:{name}", []).append(digest)
    return {
        key: tuple(sorted(values))
        for key, values in entries.items()
    }


# AUTO_MERGE is deliberately excluded. The ort strategy may leave this
# auxiliary tree ref behind after a conflicted operation has completed, so it
# does not prove that a Git operation is still active. Controlling markers such
# as MERGE_HEAD and the rebase directories remain protected below.
_GIT_OPERATION_PATHS = (
    "MERGE_HEAD",
    "MERGE_MODE",
    "MERGE_MSG",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "BISECT_HEAD",
    "BISECT_LOG",
    "BISECT_START",
    "BISECT_TERMS",
)


def _git_operation_path_signature(path: Path) -> str:
    digest = hashlib.sha256()

    def include(candidate: Path, relative: str) -> None:
        stat = candidate.lstat()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_mode).encode("ascii"))
        digest.update(b"\0")
        if candidate.is_symlink():
            digest.update(os.fsencode(os.readlink(candidate)))
        elif candidate.is_file():
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)

    include(path, ".")
    if path.is_dir() and not path.is_symlink():
        for candidate in sorted(
            path.rglob("*"),
            key=lambda entry: entry.relative_to(path).as_posix(),
        ):
            include(candidate, candidate.relative_to(path).as_posix())
    return digest.hexdigest()


def _git_operation_entries(worktree: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for name in _GIT_OPERATION_PATHS:
        resolved = git(
            worktree,
            "rev-parse",
            "--git-path",
            name,
        ).stdout.strip()
        path = Path(resolved)
        if not path.is_absolute():
            path = worktree / path
        if path.exists() or path.is_symlink():
            entries[name] = _git_operation_path_signature(path)
    return entries


def _metadata_changes_after_block(
    before: Mapping[str, object],
    blocked: Mapping[str, object],
    current: Mapping[str, object],
    candidates: frozenset[str],
) -> frozenset[str]:
    return frozenset(
        key
        for key in candidates
        if before.get(key) == blocked.get(key)
        and blocked.get(key) != current.get(key)
    )


def _path_signature(worktree: Path, relative_path: str) -> str:
    path = worktree / relative_path
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    digest = hashlib.sha256()
    digest.update(str(stat.st_mode).encode("ascii"))
    digest.update(b"\0")
    try:
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.is_dir():
            head = git(path, "rev-parse", "--verify", "HEAD", check=False)
            status = git(
                path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                check=False,
            )
            digest.update(head.stdout.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(status.stdout.encode("utf-8", errors="replace"))
        else:
            digest.update(f"special:{stat.st_size}".encode("ascii"))
    except OSError as error:
        digest.update(f"unreadable:{type(error).__name__}".encode("ascii"))
    return digest.hexdigest()


def _capture_local_worktree_state(
    worktree: Path,
) -> tuple[
    str,
    str | None,
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], str]],
]:
    """Capture one worktree's HEAD, branch, index and file state."""

    head = git(worktree, "rev-parse", "--verify", "HEAD").stdout.strip()
    branch_result = git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    index = _index_entries(worktree)
    path_kinds: dict[str, set[str]] = {}
    path_groups = (
        (
            "unstaged",
            _nul_git_paths(
                worktree,
                "diff",
                "--name-only",
                "-z",
                "--ignore-submodules=none",
            ),
        ),
        (
            "staged",
            _nul_git_paths(
                worktree,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--ignore-submodules=none",
            ),
        ),
        (
            "untracked",
            _nul_git_paths(
                worktree,
                "ls-files",
                "--others",
                "-z",
            ),
        ),
    )
    for kind, paths in path_groups:
        for relative_path in paths:
            path_kinds.setdefault(relative_path, set()).add(kind)
    for relative_path, entries in index.items():
        if _non_default_index_flags(entries):
            path_kinds.setdefault(relative_path, set()).add("index-hidden")
    dirty = {
        path: (tuple(sorted(kinds)), _path_signature(worktree, path))
        for path, kinds in sorted(path_kinds.items())
    }
    return head, branch, index, dirty


def _worktree_state_signature(worktree: Path) -> str:
    if not worktree.is_dir():
        return "missing"
    head, branch, index, dirty = _capture_local_worktree_state(worktree)
    encoded = json.dumps(
        {
            "head": head,
            "branch": branch,
            "index": index,
            "dirty": dirty,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_git_snapshot(worktree: Path) -> GitSnapshot:
    """Capture local and repository-wide Git state without staging content."""

    head, branch, index, dirty = _capture_local_worktree_state(worktree)
    return GitSnapshot(
        head=head,
        branch=branch,
        index=index,
        dirty=dirty,
        refs=_shared_ref_entries(worktree),
        worktrees=_registered_worktree_entries(worktree),
        config=_config_entries(worktree),
        operations=_git_operation_entries(worktree),
    )


def agent_snapshot_changes(
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
) -> list[AgentChange]:
    changes: dict[str, set[str]] = {}
    for path in set(before.index) | set(after.index):
        before_entries = before.index.get(path, ())
        after_entries = after.index.get(path, ())
        if before_entries != after_entries:
            changes.setdefault(path, set()).add("index")
            before_flags = _non_default_index_flags(before_entries)
            after_flags = _non_default_index_flags(after_entries)
            if before_flags != after_flags:
                changes[path].add("index flags")
    for path in set(before.dirty) | set(after.dirty):
        if before.dirty.get(path) != after.dirty.get(path):
            before_kinds = before.dirty.get(path, ((), ""))[0]
            after_kinds = after.dirty.get(path, ((), ""))[0]
            changes.setdefault(path, set()).update(before_kinds)
            changes[path].update(after_kinds)
            changes[path].add("worktree")
    if before.head != after.head:
        for path in _nul_git_paths(
            worktree,
            "diff",
            "--name-only",
            "-z",
            before.head,
            after.head,
        ):
            changes.setdefault(path, set()).add("HEAD")
    return [
        AgentChange(path, tuple(sorted(kinds)))
        for path, kinds in sorted(changes.items())
    ]


def _worker_path_allowed(
    path: str,
    selected_plan_path: str,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
) -> bool:
    normalized = path.replace("\\", "/")
    lowered = normalized.casefold()
    if normalized == selected_plan_path:
        return True
    if normalized in allowed_guidance_targets:
        return True
    if lowered in {"docs/product.md", "docs/future_ideas.md"}:
        return True
    if Path(normalized).name.casefold() == "agents.md":
        return False
    if lowered == "docs/plans.md":
        return allow_common_plans
    if lowered == "docs/architecture.md":
        return False
    if lowered.startswith("docs/requirements/"):
        allowed_paths = {
            candidate
            for source in allowed_requirement_archives
            for candidate in (source, requirement_archive_path(source))
        }
        return normalized in allowed_paths
    if re.fullmatch(r"docs/plans/[^/]+\.md", normalized):
        return False
    return True


def validate_agent_scope(
    role_name: str,
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    selected_plan_path: str | None = None,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
    allowed_ref_changes: frozenset[str] = frozenset(),
    allowed_worktree_changes: frozenset[str] = frozenset(),
    external_worktree: Path | None = None,
    base_branch: str | None = None,
) -> None:
    """Validate the net Git mutation made by one Agent role."""

    changes = agent_snapshot_changes(worktree, before, after)
    violations: list[str] = []
    if before.head != after.head:
        violations.append(f"HEAD 변경 ({before.head} -> {after.head})")
    if before.branch != after.branch:
        violations.append(
            f"branch 전환 ({before.branch or 'detached'} -> "
            f"{after.branch or 'detached'})"
        )

    if not before.shared_metadata_complete or not after.shared_metadata_complete:
        violations.append("공유 Git metadata 기준 snapshot 누락")
    else:
        # Codex owns these ephemeral refs and may create or prune them while an
        # Agent is running. Filter both sides during comparison so snapshots
        # persisted by older versions remain compatible with this boundary.
        compared_before_refs = {
            key: value
            for key, value in before.refs.items()
            if not key.startswith("refs/codex/turn-diffs/")
        }
        compared_after_refs = {
            key: value
            for key, value in after.refs.items()
            if not key.startswith("refs/codex/turn-diffs/")
        }
        external_ref_changes = frozenset()
        external_worktree_changes = frozenset()
        if external_worktree is not None:
            external_worktree_changes = frozenset(
                (str(_canonical_path(external_worktree)),)
            )
            if base_branch:
                external_ref_changes = frozenset(
                    (
                        f"refs/heads/{base_branch}",
                        f"refs/remotes/origin/{base_branch}",
                        "refs/remotes/origin/HEAD",
                    )
                )
        metadata_states = (
            (
                "공유 Git refs",
                compared_before_refs,
                compared_after_refs,
                allowed_ref_changes | external_ref_changes,
            ),
            (
                "Git worktree 등록·HEAD·index·파일 상태",
                before.worktrees,
                after.worktrees,
                allowed_worktree_changes | external_worktree_changes,
            ),
            (
                "Git config",
                before.config,
                after.config,
                frozenset(),
            ),
            (
                "Git operation 상태",
                before.operations,
                after.operations,
                frozenset(),
            ),
        )
        for label, before_state, after_state, allowed_changes in metadata_states:
            changed_keys = sorted(
                key
                for key in set(before_state) | set(after_state)
                if before_state.get(key) != after_state.get(key)
                and key not in allowed_changes
            )
            if changed_keys:
                displayed = ", ".join(changed_keys[:8])
                remainder = len(changed_keys) - 8
                if remainder > 0:
                    displayed += f", 외 {remainder}개"
                violations.append(f"{label} 변경 ({displayed})")

    for change in changes:
        if "index flags" in change.kinds:
            violations.append(
                f"{change.path} ({', '.join(change.kinds)})"
            )
            continue
        allowed = False
        if role_name == "planner":
            allowed = bool(
                change.path == "docs/FUTURE_IDEAS.md"
                or (
                    re.fullmatch(r"docs/plans/[^/]+\.md", change.path)
                    and change.path not in before.index
                )
            )
        elif role_name == "worker":
            if selected_plan_path is None:
                raise ValueError("Worker scope 검증에 선택된 ExecPlan 경로가 없습니다.")
            allowed = _worker_path_allowed(
                change.path,
                selected_plan_path,
                allowed_requirement_archives,
                allowed_guidance_targets,
                allow_common_plans,
            )
        elif role_name == "reviewer":
            allowed = False
        else:
            raise ValueError(f"알 수 없는 Agent 역할입니다: {role_name}")
        if not allowed:
            violations.append(
                f"{change.path} ({', '.join(change.kinds) or 'Git 상태'})"
            )

    if violations:
        raise AgentScopeError(
            f"{role_name} Git 범위 위반: " + "; ".join(violations),
            result_snapshot=after,
        )


def _same_local_snapshot(left: GitSnapshot, right: GitSnapshot) -> bool:
    return (
        left.head == right.head
        and left.branch == right.branch
        and left.index == right.index
        and left.dirty == right.dirty
        and left.operations == right.operations
    )


def _git_with_index(
    worktree: Path,
    index_path: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index_path)
    return command(
        ["git", "-C", str(worktree), *arguments],
        env=environment,
    )


def _hash_git_blob(worktree: Path, contents: bytes) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), "hash-object", "-w", "--stdin"],
        input=contents,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        output = result.stdout[-6000:].decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Agent 시작 파일 보존 실패:\n{output}")
    return result.stdout.decode("ascii").strip()


def _agent_worktree_tree(
    worktree: Path,
    snapshot: GitSnapshot,
    index_tree: str,
) -> str:
    """Write the exact dirty worktree state through an isolated index."""

    with tempfile.TemporaryDirectory(prefix="loop-manager-index-") as directory:
        temporary_index = Path(directory) / "index"
        _git_with_index(worktree, temporary_index, "read-tree", index_tree)
        for relative_path in sorted(snapshot.dirty):
            target = worktree / relative_path
            try:
                target_stat = target.lstat()
            except FileNotFoundError:
                _git_with_index(
                    worktree,
                    temporary_index,
                    "update-index",
                    "--force-remove",
                    "--",
                    relative_path,
                )
                continue

            if stat.S_ISREG(target_stat.st_mode):
                mode = "100755" if target_stat.st_mode & 0o111 else "100644"
                object_id = _hash_git_blob(worktree, target.read_bytes())
            elif stat.S_ISLNK(target_stat.st_mode):
                mode = "120000"
                object_id = _hash_git_blob(
                    worktree,
                    os.fsencode(os.readlink(target)),
                )
            elif stat.S_ISDIR(target_stat.st_mode):
                nested_head = git(
                    target,
                    "rev-parse",
                    "--verify",
                    "HEAD",
                    check=False,
                )
                if nested_head.returncode:
                    raise RuntimeError(
                        "Agent 시작 시점의 directory 변경을 안전하게 "
                        f"보존할 수 없습니다: {relative_path}"
                    )
                mode = "160000"
                object_id = nested_head.stdout.strip()
            else:
                raise RuntimeError(
                    "Agent 시작 시점의 특수 파일을 안전하게 보존할 수 "
                    f"없습니다: {relative_path}"
                )
            _git_with_index(
                worktree,
                temporary_index,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                object_id,
                relative_path,
            )
        return _git_with_index(
            worktree,
            temporary_index,
            "write-tree",
        ).stdout.strip()


def _commit_tree(
    worktree: Path,
    tree: str,
    parent: str,
    message: str,
) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Loop Manager",
            "GIT_AUTHOR_EMAIL": "loop-manager@localhost",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
            "GIT_COMMITTER_NAME": "Loop Manager",
            "GIT_COMMITTER_EMAIL": "loop-manager@localhost",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
        }
    )
    return command(
        [
            "git",
            "-C",
            str(worktree),
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            message,
        ],
        env=environment,
    ).stdout.strip()


def _capture_agent_baseline(
    repo: str,
    worktree: Path,
    snapshot: GitSnapshot,
) -> str | None:
    if not snapshot.dirty:
        return None
    if snapshot.operations:
        # Rebase-conflict resolution intentionally starts inside a Git
        # operation. Its scope is still validated, but it cannot use the
        # ordinary HEAD/index tree recovery representation.
        return None
    index_tree = git(worktree, "write-tree").stdout.strip()
    worktree_tree = _agent_worktree_tree(worktree, snapshot, index_tree)
    baseline_id = hashlib.sha256(
        f"{snapshot.head}\0{index_tree}\0{worktree_tree}".encode("ascii")
    ).hexdigest()
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    refname = f"refs/loop-manager/baselines/{project_id}/{baseline_id}"
    index_commit = _commit_tree(
        worktree,
        index_tree,
        snapshot.head,
        f"Loop Manager baseline index {baseline_id}",
    )
    worktree_commit = _commit_tree(
        worktree,
        worktree_tree,
        index_commit,
        f"Loop Manager baseline worktree {baseline_id}",
    )
    existing = git(
        worktree,
        "rev-parse",
        "--verify",
        refname,
        check=False,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != worktree_commit:
            raise RuntimeError(f"Agent 시작 상태 보존 ref 충돌: {refname}")
    else:
        git(worktree, "update-ref", refname, worktree_commit)
    return json.dumps(
        {
            "version": 1,
            "ref": refname,
            "ref_target": worktree_commit,
            "index_tree": index_tree,
            "worktree_tree": worktree_tree,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def begin_agent_scope(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    role_name: str,
    *,
    input_fingerprint: str | None = None,
) -> GitSnapshot:
    row = connection.execute(
        "SELECT agent_role, agent_snapshot, plan_path "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Agent scope를 저장할 프로젝트가 없습니다.")
    if row["agent_snapshot"]:
        if row["agent_role"] != role_name:
            raise RuntimeError(
                f"저장된 Agent 역할이 현재 역할과 다릅니다: "
                f"{row['agent_role']} -> {role_name}"
            )
        return GitSnapshot.deserialize(str(row["agent_snapshot"]))
    preliminary_snapshot = capture_git_snapshot(worktree)
    agent_baseline = _capture_agent_baseline(
        repo,
        worktree,
        preliminary_snapshot,
    )
    snapshot = (
        capture_git_snapshot(worktree)
        if agent_baseline is not None
        else preliminary_snapshot
    )
    if not _same_local_snapshot(preliminary_snapshot, snapshot):
        raise RuntimeError("Agent 시작 상태를 보존하는 동안 worktree가 변경됐습니다.")
    plan_path = str(row["plan_path"] or "")
    plan_file = worktree / plan_path
    agent_input_plan = (
        plan_file.read_text(encoding="utf-8")
        if role_name == "reviewer" and plan_path and plan_file.is_file()
        else None
    )
    update_project(
        connection,
        repo,
        agent_role=role_name,
        agent_snapshot=snapshot.serialize(),
        agent_input_fingerprint=input_fingerprint,
        agent_baseline=agent_baseline,
        agent_recovery=None,
        agent_input_plan=agent_input_plan,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )
    return snapshot


def clear_agent_scope(connection: sqlite3.Connection, repo: str) -> None:
    update_project(
        connection,
        repo,
        agent_role=None,
        agent_snapshot=None,
        agent_baseline=None,
        agent_recovery=None,
        agent_input_plan=None,
        agent_result_plan=None,
        agent_input_fingerprint=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def validate_planning_workspace_identity(
    state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    expected = (
        state_dir / "worktrees" / project_id / "planning" / fingerprint
    )
    _validate_managed_path_boundary(
        state_dir,
        worktree,
        expected,
        "planning worktree",
    )


def validate_planning_worktree_ownership(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    """Require confirmed Manager ownership before removing a planning worktree."""

    project = connection.execute(
        "SELECT worktree, planning_workspace_owned FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if worktree.exists() and (
        project is None
        or int(project["planning_workspace_owned"]) != WORKSPACE_OWNED
        or Path(str(project["worktree"] or "")) != worktree
    ):
        raise WorkspaceOwnershipError(
            "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}"
        )

    if not fingerprint:
        raise WorkspaceOwnershipError(
            "planning worktree의 입력 fingerprint가 저장되지 않았습니다."
        )
    validate_planning_workspace_identity(
        state_dir,
        repo,
        fingerprint,
        worktree,
    )
    if worktree.exists():
        validate_managed_worktree(repo_path, worktree, None)


def changed_files(worktree: Path, since: str | None = None) -> list[str]:
    if since:
        output = git(worktree, "diff", "--name-only", f"{since}..HEAD").stdout
        return sorted(line for line in output.splitlines() if line)
    unstaged = git(worktree, "diff", "--name-only").stdout.splitlines()
    staged = git(worktree, "diff", "--cached", "--name-only").stdout.splitlines()
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted(set(unstaged + staged + untracked))


def branch_has_changes(worktree: Path, base: str) -> bool:
    result = git(
        worktree,
        "diff",
        "--quiet",
        f"origin/{base}...HEAD",
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            f"관리 branch 변경 확인에 실패했습니다 (git 종료 코드 {result.returncode})."
        )
    return result.returncode == 1


def assess_cleanup(worktree: Path, pr_head_oid: str | None) -> CleanupAssessment:
    """Classify a managed worktree without changing it or shared Git state."""
    if not worktree.exists():
        return CleanupAssessment("missing")
    try:
        status_lines = git(
            worktree, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout.splitlines()
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 상태 확인 실패: {error}")

    dirty_kinds: list[str] = []
    if any(line.startswith("??") for line in status_lines):
        dirty_kinds.append("untracked 파일")
    if any(len(line) >= 2 and line[0] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("staged 변경")
    if any(len(line) >= 2 and line[1] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("tracked 파일 수정")
    if dirty_kinds:
        return CleanupAssessment("dirty", ", ".join(dirty_kinds))

    if not pr_head_oid:
        return CleanupAssessment("unverifiable", "PR head commit 정보가 없습니다.")
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", pr_head_oid) is None:
        return CleanupAssessment("unverifiable", "PR head commit OID 형식이 유효하지 않습니다.")
    try:
        local_head = git(worktree, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        head_object = git(
            worktree, "cat-file", "-e", f"{pr_head_oid}^{{commit}}", check=False
        )
        if head_object.returncode:
            return CleanupAssessment(
                "unverifiable", f"PR head commit object를 찾을 수 없습니다: {pr_head_oid}"
            )
        relation = git(
            worktree,
            "merge-base",
            "--is-ancestor",
            local_head,
            pr_head_oid,
            check=False,
        )
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"commit 관계 확인 실패: {error}")
    if relation.returncode == 0:
        return CleanupAssessment("safe")
    if relation.returncode == 1:
        return CleanupAssessment(
            "local-only commits", "로컬 HEAD에 PR head에 포함되지 않은 commit이 있습니다."
        )
    return CleanupAssessment(
        "unverifiable", f"commit 관계 확인 실패 (git 종료 코드 {relation.returncode})"
    )


def validate_pending_cleanup_worktree(
    repo_path: Path,
    state_dir: Path,
    repo: str,
    worktree: Path,
) -> None:
    """Prove a pending cleanup path belongs to its deterministic ExecPlan."""

    if not worktree.exists():
        return
    branch_result = git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    if branch_result.returncode:
        raise WorkspaceOwnershipError(
            "보존된 cleanup worktree가 branch에 연결되어 있지 않습니다."
        )
    branch = branch_result.stdout.strip()
    match = re.fullmatch(r"loop-manager/(G-\d+)", branch)
    if match is None:
        raise WorkspaceOwnershipError(
            f"보존된 cleanup worktree branch의 소유권을 증명할 수 없습니다: {branch}"
        )
    plan_id = match.group(1)
    validate_execplan_workspace_identity(
        state_dir,
        repo,
        plan_id,
        branch,
        worktree,
    )
    validate_managed_worktree(repo_path, worktree, branch)


def remove_cleanup_worktree(
    repo: Path, worktree: Path, assessment: CleanupAssessment
) -> CleanupAssessment:
    if assessment.status != "safe":
        return assessment
    try:
        git(repo, "worktree", "remove", str(worktree))
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 제거 실패: {error}")
    return assessment


def _ensure_cleanup_pending_schema(connection: sqlite3.Connection) -> None:
    connection.execute(CLEANUP_PENDING_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cleanup_pending)")
    }
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE cleanup_pending ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 1"
        )


def _store_cleanup_pending(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
    reason: str,
    *,
    workspace_owned: int = WORKSPACE_OWNED,
) -> None:
    connection.execute(
        """
        INSERT INTO cleanup_pending (
            repo, worktree, pr_url, pr_head_oid, reason, workspace_owned
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, worktree) DO UPDATE SET
            pr_url = excluded.pr_url,
            pr_head_oid = excluded.pr_head_oid,
            reason = excluded.reason,
            workspace_owned = excluded.workspace_owned,
            updated_at = CURRENT_TIMESTAMP
        """,
        (repo, str(worktree), pr_url, pr_head_oid, reason, workspace_owned),
    )


def recheck_cleanup_pending(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
) -> list[sqlite3.Row]:
    _ensure_cleanup_pending_schema(connection)
    pending = connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()
    for item in pending:
        worktree = Path(item["worktree"])
        if int(item["workspace_owned"]) != WORKSPACE_OWNED:
            if not worktree.exists() and not worktree.is_symlink():
                connection.execute(
                    "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                    (repo, str(worktree)),
                )
            continue
        try:
            validate_pending_cleanup_worktree(
                repo_path,
                state_dir,
                repo,
                worktree,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            assessment = CleanupAssessment(
                "unverifiable",
                f"cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, item["pr_head_oid"])
            assessment = remove_cleanup_worktree(repo_path, worktree, assessment)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            connection.execute(
                """
                UPDATE cleanup_pending
                SET reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND worktree = ?
                """,
                (assessment.reason or assessment.status, repo, str(worktree)),
            )
    connection.commit()
    return connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()


def planning_diagnostics_path(
    state_dir: Path,
    repo: str,
    fingerprint: str,
) -> Path:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return state_dir / "logs" / project_id / "planning" / f"{fingerprint}.txt"


def preserve_planning_diagnostics(
    state_dir: Path,
    repo: str,
    row: sqlite3.Row,
) -> Path:
    fingerprint = row["planning_attempt_fingerprint"]
    if not fingerprint:
        fingerprint = row["planning_error_fingerprint"] or "unknown"
    worktree = Path(row["worktree"]) if row["worktree"] else None
    error = (
        row["planning_error"]
        or row["planning_validation_error"]
        or row["last_error"]
        or ""
    )
    diff = ""
    if worktree and worktree.is_dir():
        try:
            base_revision = row["active_sha"] or "HEAD"
            diff = git(worktree, "diff", "--binary", base_revision).stdout
            for relative_path in _nul_git_paths(
                worktree, "ls-files", "--others", "-z"
            ):
                untracked = git(
                    worktree,
                    "diff",
                    "--binary",
                    "--no-index",
                    "--",
                    os.devnull,
                    relative_path,
                    check=False,
                )
                if untracked.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"untracked diff 생성 실패 ({relative_path}): "
                        f"git 종료 코드 {untracked.returncode}"
                    )
                diff += untracked.stdout
        except RuntimeError as diff_error:
            diff = f"diff 보존 실패: {diff_error}"
    diagnostic = planning_diagnostics_path(state_dir, repo, fingerprint)
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    diagnostic.write_text(
        "\n".join(
            [
                f"input_fingerprint: {fingerprint}",
                f"planning_worktree: {worktree or ''}",
                f"planner_session_id: {row['planner_session_id'] or ''}",
                f"validation_retries: {row['planning_retry_count']}",
                "",
                "last_error:",
                str(error),
                "",
                "diff:",
                diff,
            ]
        ),
        encoding="utf-8",
    )
    return diagnostic


def clear_planning_validation_state(
    connection: sqlite3.Connection,
    repo: str,
) -> None:
    update_project(
        connection,
        repo,
        planning_attempt_fingerprint=None,
        planning_retry_count=0,
        planner_session_id=None,
        worker_session_id=None,
        reviewer_session_id=None,
        worker_result=None,
        verify_result=None,
        review_result=None,
        planning_validation_error=None,
        planning_result_pending_validation=0,
        planning_result_completed=0,
        planning_error=None,
        planning_error_fingerprint=None,
        last_error=None,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_baseline=None,
        agent_recovery=None,
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def _clear_active_values(
    *,
    processed_sha: str | None = None,
    planning_fingerprint: str | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "active_sha": None,
        "validated_base_sha": None,
        "publication_remote_oid": None,
        "pr_conflict_recovery": 0,
        "pr_conflict_head_oid": None,
        "pr_conflict_base_oid": None,
        "plan_sha": None,
        "phase": None,
        "branch": None,
        "worktree": None,
        "plan_path": None,
        "pr_url": None,
        "pr_head_oid": None,
        "last_error": None,
        "planning_attempt_fingerprint": None,
        "planning_workspace_owned": 0,
        "planning_retry_count": 0,
        "planner_session_id": None,
        "worker_session_id": None,
        "reviewer_session_id": None,
        "worker_result": None,
        "verify_result": None,
        "review_result": None,
        "planning_validation_error": None,
        "planning_result_pending_validation": 0,
        "planning_result_completed": 0,
        "planning_error": None,
        "planning_error_fingerprint": None,
        "agent_role": None,
        "agent_snapshot": None,
        "agent_baseline": None,
        "agent_recovery": None,
        "agent_input_plan": None,
        "agent_result_plan": None,
        "agent_input_fingerprint": None,
        "agent_result_snapshot": None,
        "agent_manager_snapshot": None,
    }
    if processed_sha is not None:
        values["processed_sha"] = processed_sha
    if planning_fingerprint is not None:
        values["planning_fingerprint"] = planning_fingerprint
    return values


def clear_active(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    update_project(
        connection,
        repo,
        **_clear_active_values(
            processed_sha=processed_sha,
            planning_fingerprint=planning_fingerprint,
        ),
    )


def _execplan_input_paths(plan: sqlite3.Row) -> list[str]:
    sources = json.loads(plan["sources"])
    return [str(plan["path"]), *(str(source) for source in sources)]


def _execplan_input_fingerprint(entries: list[tuple[str, str | None]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-blocked-input-v1\0")
    for relative_path, contents in entries:
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        if contents is None:
            digest.update(b"\0")
            continue
        normalized = contents.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        digest.update(b"\1")
        digest.update(len(normalized).to_bytes(8, "big"))
        digest.update(normalized)
    return digest.hexdigest()


def _worktree_execplan_input_fingerprint(worktree: Path, plan: sqlite3.Row) -> str:
    entries = []
    for relative_path in _execplan_input_paths(plan):
        path = worktree / relative_path
        contents = path.read_text(encoding="utf-8") if path.is_file() else None
        entries.append((relative_path, contents))
    return _execplan_input_fingerprint(entries)


def _revision_execplan_input_fingerprint(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    entries = []
    for relative_path in _execplan_input_paths(plan):
        shown = git(repo_path, "show", f"{revision}:{relative_path}", check=False)
        entries.append((relative_path, shown.stdout if shown.returncode == 0 else None))
    return _execplan_input_fingerprint(entries)


def _execplan_metadata_identity_error(
    repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
    subject: str,
) -> str | None:
    path = str(plan["path"])
    plan_sha = str(plan["plan_sha"] or "")
    if plan_sha:
        shown = git(repo_path, "show", f"{plan_sha}:{path}", check=False)
        if shown.returncode:
            return f"{subject}의 게시 메타데이터를 확인할 수 없습니다."
        try:
            published = published_execplan(path, shown.stdout)
        except RuntimeError as error:
            return f"{subject}의 게시 메타데이터가 유효하지 않습니다: {error}"
        metadata_fields = (
            "id",
            "title",
            "sources",
            "depends_on",
            "complexity",
            "risk",
            "priority",
        )
        if any(current[field] != published[field] for field in metadata_fields):
            return f"{subject}의 고정 메타데이터는 변경할 수 없습니다."
        return None

    sources, dependencies, priority = _execplan_metadata(current)
    if (
        current["id"] != plan["plan_id"]
        or current["title"] != plan["title"]
        or sources != plan["sources"]
        or dependencies != plan["depends_on"]
        or priority != plan["priority"]
    ):
        return f"{subject}의 SQLite 메타데이터와 현재 파일이 일치하지 않습니다."
    return None


def _blocked_execplan_metadata_identity_error(
    repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
) -> str | None:
    return _execplan_metadata_identity_error(
        repo_path,
        current,
        plan,
        "BLOCKED ExecPlan",
    )


def validate_active_execplan_metadata(
    repo_path: Path,
    worktree: Path,
    relative_path: str,
    plan: sqlite3.Row,
) -> dict[str, object]:
    current = validate_execplan(worktree, relative_path)
    error = _execplan_metadata_identity_error(
        repo_path,
        current,
        plan,
        "활성 ExecPlan",
    )
    if error is not None:
        raise RuntimeError(error)
    return current


def validate_worker_workspace_scope(
    repo_path: Path,
    worktree: Path,
    branch: str,
    base_branch: str,
    selected_plan_path: str,
    catalog_plan: sqlite3.Row,
    before: GitSnapshot,
    *,
    allow_rebase: bool = False,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
) -> dict[str, object]:
    """Validate one Worker-controlled operation before Manager Git writes."""

    after = capture_git_snapshot(worktree)
    validate_agent_scope(
        "worker",
        worktree,
        before,
        after,
        selected_plan_path=selected_plan_path,
        allowed_requirement_archives=allowed_requirement_archives,
        allowed_guidance_targets=allowed_guidance_targets,
        allow_common_plans=allow_common_plans,
        external_worktree=repo_path,
        base_branch=base_branch,
    )
    plan = validate_active_execplan_metadata(
        repo_path,
        worktree,
        selected_plan_path,
        catalog_plan,
    )
    validate_managed_worktree(
        repo_path,
        worktree,
        branch,
        allow_rebase=allow_rebase,
    )
    return plan


GUIDANCE_BLOCK_REASON = "guidance_approval"


def _guidance_document(worktree: Path, plan_path: str) -> GuidanceDocument | None:
    try:
        contents = (worktree / plan_path).read_text(encoding="utf-8")
        return parse_guidance_document(contents)
    except (OSError, GuidanceFormatError) as error:
        raise AgentScopeError(f"Worker guidance 제안 형식 위반: {error}") from error


def _git_common_directory(repository: Path) -> Path:
    value = git(
        repository,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ).stdout.strip()
    path = Path(value)
    return _canonical_path(path if path.is_absolute() else repository / path)


def is_loop_manager_repository(repository: Path) -> bool:
    """Identify the registered Loop Manager source across its managed worktrees."""

    try:
        return _git_common_directory(repository) == _git_common_directory(ROOT)
    except RuntimeError:
        return _canonical_path(repository) == _canonical_path(ROOT)


def _validate_guidance_proposal_scope(
    proposals: tuple[GuidanceProposal, ...],
    *,
    common_allowed: bool,
) -> None:
    for proposal in proposals:
        if common_allowed and proposal.scope == "project":
            raise AgentScopeError(
                "Loop Manager 루트 AGENTS.md는 공통 원본이므로 "
                "Scope common으로 명시적으로 승인해야 합니다."
            )
        if proposal.scope == "common" and not common_allowed:
            raise AgentScopeError(
                "공통 AGENTS.md guidance는 Loop Manager 자체 저장소의 "
                f"Worker만 제안할 수 있습니다: {proposal.proposal_id}"
            )


def _guidance_rows(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[sqlite3.Row, ...]:
    _ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM guidance_approvals "
            "WHERE repo = ? AND plan_id = ? ORDER BY batch_id, sequence, proposal_id",
            (repo, plan_id),
        ).fetchall()
    )


def _guidance_worker_targets(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT DISTINCT target_path FROM guidance_approvals "
        "WHERE repo = ? AND plan_id = ? AND applied = 0 "
        "AND decision != 'pending' ORDER BY target_path",
        (repo, plan_id),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _published_plan_allows_common_plans(
    repo_path: Path,
    catalog_plan: sqlite3.Row,
) -> bool:
    if not is_loop_manager_repository(repo_path):
        return False
    plan_sha = str(catalog_plan["plan_sha"] or "")
    if not plan_sha:
        return False
    shown = git(
        repo_path,
        "show",
        f"{plan_sha}:{catalog_plan['path']}",
        check=False,
    )
    if shown.returncode:
        return False
    acceptance = re.search(
        r"(?ms)^## Acceptance Criteria\s*\n+(.*?)(?=^## |\Z)",
        shown.stdout,
    )
    return bool(
        acceptance
        and re.search(
            r"(?im)^-\s+(?:공통\s+)?`docs/PLANS\.md`(?:에|를|을|의|\s)"
            r".*(?:변경|문서화|갱신|수정|change|update|document)",
            acceptance.group(1),
        )
    )


def _guidance_target_state(
    worktree: Path,
    target: str,
) -> tuple[bytes, int]:
    path = worktree / target
    try:
        target_stat = path.lstat()
    except FileNotFoundError as error:
        raise GuidanceFormatError(f"guidance 대상 파일이 없습니다: {target}") from error
    if path.is_symlink() or not path.is_file():
        raise GuidanceFormatError(
            f"guidance 대상은 일반 파일이어야 합니다: {target}"
        )
    # Git records executable permission, not local umask/group write bits.
    git_mode = 0o755 if target_stat.st_mode & stat.S_IXUSR else 0o644
    return path.read_bytes(), git_mode


def _guidance_block_message(
    plan: sqlite3.Row,
    proposals: tuple[GuidanceProposal, ...],
    detail: str,
) -> str:
    locations = []
    for proposal in proposals:
        locations.append(
            f"제안 {proposal.proposal_id}: {plan['path']}:{proposal.line}; "
            f"User Decision: {plan['path']}:{proposal.decision.line}"
        )
    return (
        f"AGENTS.md guidance 사용자 결정 대기 ({plan['plan_id']}).\n"
        f"ExecPlan: {plan['path']}\n{detail}\n" + "\n".join(locations)
    )


def _guidance_batch_id(
    repo: str,
    plan_id: str,
    document: GuidanceDocument,
    proposals: tuple[GuidanceProposal, ...],
) -> str:
    encoded = json.dumps(
        {
            "repo": repo,
            "plan_id": plan_id,
            "structure": document.structure_hash,
            "proposals": [proposal.identity for proposal in proposals],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _block_for_new_guidance(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    document: GuidanceDocument,
    new_proposals: tuple[GuidanceProposal, ...],
    worker_result: str,
) -> None:
    if not new_proposals:
        raise RuntimeError("차단할 새 guidance 제안이 없습니다.")
    _validate_guidance_proposal_scope(
        new_proposals,
        common_allowed=is_loop_manager_repository(repo_path),
    )
    for proposal in new_proposals:
        if proposal.decision.decision != "pending":
            raise AgentScopeError(
                "Worker가 새 guidance 제안의 User Decision을 미리 확정할 수 없습니다: "
                f"{proposal.proposal_id}"
            )

    batch_id = _guidance_batch_id(
        repo,
        str(plan["plan_id"]),
        document,
        new_proposals,
    )
    baselines: dict[str, tuple[bytes, int]] = {}
    for proposal in new_proposals:
        if proposal.target not in baselines:
            baselines[proposal.target] = _guidance_target_state(
                worktree,
                proposal.target,
            )
    for proposal in new_proposals:
        baseline, _baseline_mode = baselines[proposal.target]
        apply_patch_to_content(
            baseline,
            proposal.target,
            proposal.patch,
        )
    message = _guidance_block_message(
        plan,
        new_proposals,
        "각 User Decision의 Decision과 Revision을 사용자가 직접 확정해야 합니다.",
    )
    result_snapshot = capture_git_snapshot(worktree).serialize()
    with connection:
        for sequence, proposal in enumerate(new_proposals):
            baseline, baseline_mode = baselines[proposal.target]
            connection.execute(
                """
                INSERT INTO guidance_approvals (
                    repo, plan_id, proposal_id, proposal_hash, target_path,
                    scope, proposal_patch, proposal_patch_hash,
                    proposal_line, decision_line, batch_id, sequence,
                    baseline_content, baseline_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    plan["plan_id"],
                    proposal.proposal_id,
                    proposal.identity,
                    proposal.target,
                    proposal.scope,
                    proposal.patch,
                    guidance_patch_hash(proposal.target, proposal.patch),
                    proposal.line,
                    proposal.decision.line,
                    batch_id,
                    sequence,
                    baseline,
                    baseline_mode,
                ),
            )
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'BLOCKED', blocked_reason = ?,
                blocked_fingerprint = NULL,
                blocked_worktree_fingerprint = NULL,
                guidance_plan_hash = ?, last_error = ?, worker_result = ?
            WHERE repo = ? AND plan_id = ?
            """,
            (
                GUIDANCE_BLOCK_REASON,
                document.structure_hash,
                message,
                worker_result,
                repo,
                plan["plan_id"],
            ),
        )
        connection.execute(
            """
            UPDATE projects
            SET phase = 'BLOCKED', last_error = ?, worker_result = ?,
                agent_result_snapshot = ?, agent_manager_snapshot = NULL
            WHERE repo = ?
            """,
            (message, worker_result, result_snapshot, repo),
        )
        notifications.enqueue_current_project_events(connection, repo)


def _guidance_document_matches_rows(
    document: GuidanceDocument,
    rows: tuple[sqlite3.Row, ...],
    *,
    require_decisions: bool,
) -> tuple[dict[str, GuidanceProposal], tuple[GuidanceProposal, ...]]:
    proposals = {proposal.proposal_id: proposal for proposal in document.proposals}
    stored = {str(row["proposal_id"]): row for row in rows}
    missing = sorted(set(stored) - set(proposals))
    if missing:
        raise AgentScopeError(
            "기존 guidance 제안은 삭제할 수 없습니다: " + ", ".join(missing)
        )
    for proposal_id, row in stored.items():
        proposal = proposals[proposal_id]
        if proposal.identity != str(row["proposal_hash"]):
            raise AgentScopeError(
                f"기존 guidance 제안은 수정할 수 없습니다: {proposal_id}"
            )
        decision_is_immutable = require_decisions or bool(row["applied"])
        if decision_is_immutable and row["decision_hash"]:
            if proposal.decision.identity != str(row["decision_hash"]):
                raise AgentScopeError(
                    "Worker는 확정된 guidance User Decision을 수정할 수 없습니다: "
                    f"{proposal_id}"
                )
    new = tuple(
        proposal
        for proposal in document.proposals
        if proposal.proposal_id not in stored
    )
    return proposals, new


def _guidance_final_expected(
    rows: tuple[sqlite3.Row, ...],
    proposals: Mapping[str, GuidanceProposal],
) -> tuple[
    dict[tuple[str, str], tuple[bytes, int]],
    dict[str, str],
]:
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row["batch_id"]), str(row["target_path"])),
            [],
        ).append(row)
    expected: dict[tuple[str, str], tuple[bytes, int]] = {}
    final_hashes: dict[str, str] = {}
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda row: int(row["sequence"]))
        contents = bytes(ordered[0]["baseline_content"])
        mode = int(ordered[0]["baseline_mode"])
        for row in ordered:
            proposal = proposals[str(row["proposal_id"])]
            patch = finalized_patch(proposal)
            if patch is not None:
                contents = apply_patch_to_content(
                    contents,
                    proposal.target,
                    patch,
                )
        expected[key] = (contents, mode)
        final_hash = guidance_content_hash(contents)
        for row in ordered:
            final_hashes[str(row["proposal_id"])] = final_hash
    return expected, final_hashes


def resume_guidance_approval_if_decided(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
) -> bool:
    plan = active_execplan(connection, repo)
    if (
        plan is None
        or plan["status"] != "BLOCKED"
        or plan["blocked_reason"] != GUIDANCE_BLOCK_REASON
    ):
        return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    resuming_rebase = _rebase_in_progress_for_branch(worktree, branch)
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
            allow_rebase=resuming_rebase,
        )
        validate_blocked_agent_scope(
            connection,
            repo_path,
            repo,
            base,
            worktree,
            str(plan["path"]),
        )
        document = _guidance_document(worktree, str(plan["path"]))
        if document is None:
            raise AgentScopeError("저장된 guidance 제안이 ExecPlan에서 사라졌습니다.")
        if document.structure_hash != str(plan["guidance_plan_hash"] or ""):
            raise AgentScopeError(
                "User Decision 이외의 ExecPlan 변경으로 guidance 승인을 해제할 수 없습니다."
            )
        rows = _guidance_rows(connection, repo, str(plan["plan_id"]))
        proposals, new = _guidance_document_matches_rows(
            document,
            rows,
            require_decisions=False,
        )
        if new:
            raise AgentScopeError(
                "사용자 결정 대기 중 새 guidance 제안을 추가할 수 없습니다."
            )
        _validate_guidance_proposal_scope(
            document.proposals,
            common_allowed=is_loop_manager_repository(repo_path),
        )
        pending_rows = tuple(row for row in rows if not int(row["applied"]))
        for row in pending_rows:
            proposal = proposals[str(row["proposal_id"])]
            if proposal.decision.decision == "pending":
                return False
            invalidated = str(row["invalidated_decision_hash"] or "")
            if invalidated and proposal.decision.identity == invalidated:
                _update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    _guidance_block_message(
                        plan,
                        (proposal,),
                        "무효화된 결정과 다른 Revision 또는 대체 patch가 필요합니다.",
                    ),
                )
                return False
        _expected, final_hashes = _guidance_final_expected(
            pending_rows,
            proposals,
        )
        checked_targets: set[str] = set()
        for row in pending_rows:
            target = str(row["target_path"])
            if target in checked_targets:
                continue
            checked_targets.add(target)
            try:
                current_contents, current_mode = _guidance_target_state(worktree, target)
            except GuidanceFormatError:
                if row["invalidated_decision_hash"]:
                    continue
                raise
            if (
                not row["invalidated_decision_hash"]
                and (
                    current_contents != bytes(row["baseline_content"])
                    or current_mode != int(row["baseline_mode"])
                )
            ):
                raise AgentScopeError(
                    f"승인 전 guidance 대상이 변경됐습니다: {target}"
                )
        with connection:
            for row in pending_rows:
                proposal = proposals[str(row["proposal_id"])]
                patch = finalized_patch(proposal)
                connection.execute(
                    """
                    UPDATE guidance_approvals
                    SET decision = ?, decision_revision = ?,
                        decision_hash = ?, approved_patch = ?,
                        approved_patch_hash = ?, expected_content_hash = ?,
                        invalidated_decision_hash = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE repo = ? AND plan_id = ? AND proposal_id = ?
                    """,
                    (
                        proposal.decision.decision,
                        proposal.decision.revision,
                        proposal.decision.identity,
                        patch,
                        guidance_patch_hash(proposal.target, patch or ""),
                        final_hashes[proposal.proposal_id],
                        repo,
                        plan["plan_id"],
                        proposal.proposal_id,
                    ),
                )
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'IMPLEMENTING', blocked_reason = NULL,
                    blocked_fingerprint = NULL,
                    blocked_worktree_fingerprint = NULL, last_error = NULL
                WHERE repo = ? AND plan_id = ?
                """,
                (repo, plan["plan_id"]),
            )
            connection.execute(
                "UPDATE projects SET phase = 'WORKING', last_error = NULL "
                "WHERE repo = ?",
                (repo,),
            )
        if resuming_rebase:
            update_project(
                connection, repo, last_error=REBASE_IN_PROGRESS_ERROR,
                validated_base_sha=None,
            )
        clear_agent_scope(connection, repo)
        return True
    except (AgentScopeError, GuidanceFormatError, RuntimeError) as error:
        _update_blocked_error(
            connection,
            repo,
            str(plan["plan_id"]),
            f"guidance 승인 해제 실패:\n{error}",
        )
        return False


def _invalidate_guidance_approval(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    document: GuidanceDocument,
    proposals: tuple[GuidanceProposal, ...],
    worker_result: str,
    detail: str,
) -> None:
    message = _guidance_block_message(plan, proposals, detail)
    result_snapshot = capture_git_snapshot(worktree).serialize()
    proposal_ids = tuple(proposal.proposal_id for proposal in proposals)
    placeholders = ", ".join("?" for _ in proposal_ids)
    with connection:
        connection.execute(
            f"""
            UPDATE guidance_approvals
            SET invalidated_decision_hash = decision_hash,
                applied = 0, updated_at = CURRENT_TIMESTAMP
            WHERE repo = ? AND plan_id = ?
              AND proposal_id IN ({placeholders})
            """,
            (repo, plan["plan_id"], *proposal_ids),
        )
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'BLOCKED', blocked_reason = ?,
                blocked_fingerprint = NULL,
                blocked_worktree_fingerprint = NULL,
                guidance_plan_hash = ?, last_error = ?, worker_result = ?
            WHERE repo = ? AND plan_id = ?
            """,
            (
                GUIDANCE_BLOCK_REASON,
                document.structure_hash,
                message,
                worker_result,
                repo,
                plan["plan_id"],
            ),
        )
        connection.execute(
            """
            UPDATE projects
            SET phase = 'BLOCKED', last_error = ?, worker_result = ?,
                agent_result_snapshot = ?, agent_manager_snapshot = NULL
            WHERE repo = ?
            """,
            (message, worker_result, result_snapshot, repo),
        )
        # Manager rebase/verify may already have cleared the Agent scope.
        # Freeze the diagnosed state so only the User Decision can change.
        connection.execute(
            "UPDATE projects SET agent_role = 'worker', agent_snapshot = ?, "
            "agent_baseline = NULL, agent_recovery = NULL, "
            "agent_input_plan = NULL, agent_result_plan = NULL, "
            "agent_input_fingerprint = NULL WHERE repo = ?",
            (result_snapshot, repo),
        )
        notifications.enqueue_current_project_events(connection, repo)


def inspect_worker_guidance_result(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    worker_result: str,
) -> bool:
    """Validate approved targets, then block on any newly proposed guidance."""

    document = _guidance_document(worktree, str(plan["path"]))
    rows = _guidance_rows(connection, repo, str(plan["plan_id"]))
    if document is None:
        if rows:
            raise AgentScopeError("기존 guidance 제안이 ExecPlan에서 사라졌습니다.")
        return False
    _validate_guidance_proposal_scope(
        document.proposals,
        common_allowed=is_loop_manager_repository(repo_path),
    )
    proposals, new = _guidance_document_matches_rows(
        document,
        rows,
        require_decisions=True,
    )
    unapplied = tuple(row for row in rows if not int(row["applied"]))
    if unapplied:
        expected, _final_hashes = _guidance_final_expected(
            unapplied,
            proposals,
        )
        mismatched: list[GuidanceProposal] = []
        for (_batch_id, target), (expected_contents, expected_mode) in expected.items():
            try:
                actual_contents, actual_mode = _guidance_target_state(worktree, target)
                exact = actual_contents == expected_contents and actual_mode == expected_mode
            except GuidanceFormatError:
                exact = False
            if not exact:
                mismatched.extend(
                    proposals[str(row["proposal_id"])]
                    for row in unapplied
                    if str(row["target_path"]) == target
                )
        if mismatched:
            _invalidate_guidance_approval(
                connection,
                repo,
                worktree,
                plan,
                document,
                tuple(mismatched),
                worker_result,
                "실제 AGENTS.md 결과가 승인된 patch 결과와 다릅니다. "
                "User Decision Revision을 갱신해 다시 결정해야 합니다.",
            )
            return True
        with connection:
            connection.executemany(
                """
                UPDATE guidance_approvals
                SET applied = 1, updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND plan_id = ? AND proposal_id = ?
                """,
                (
                    (repo, plan["plan_id"], str(row["proposal_id"]))
                    for row in unapplied
                ),
            )
    if new:
        _block_for_new_guidance(
            connection,
            repo_path,
            repo,
            worktree,
            plan,
            document,
            new,
            worker_result,
        )
        return True
    with connection:
        connection.execute(
            "UPDATE exec_plans SET guidance_plan_hash = ? "
            "WHERE repo = ? AND plan_id = ?",
            (document.structure_hash, repo, plan["plan_id"]),
        )
    return False


def validate_guidance_publication(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    worker_result: str,
    *,
    previous_base: str | None = None,
    latest_base: str | None = None,
) -> bool:
    """Revalidate the latest approval per target, including after Manager rebase."""

    rows = tuple(connection.execute(
        "SELECT * FROM guidance_approvals WHERE repo = ? AND plan_id = ? "
        "ORDER BY rowid",
        (repo, plan["plan_id"]),
    ).fetchall())
    if not rows:
        return False
    document = _guidance_document(worktree, str(plan["path"]))
    if document is None:
        raise AgentScopeError("게시할 ExecPlan에 guidance 결정이 없습니다.")
    proposals, new = _guidance_document_matches_rows(
        document, rows, require_decisions=True
    )
    if new:
        raise AgentScopeError("게시 전에 승인되지 않은 guidance 제안이 있습니다.")
    latest_batches = {str(row["target_path"]): str(row["batch_id"]) for row in rows}
    selected = tuple(
        row for row in rows
        if str(row["batch_id"]) == latest_batches[str(row["target_path"])]
    )
    mismatched = []
    new_baselines: dict[str, tuple[bytes, int]] = {}
    for target in latest_batches:
        target_rows = tuple(row for row in selected if row["target_path"] == target)
        last = target_rows[-1]
        try:
            contents, mode = _guidance_target_state(worktree, target)
            exact = (
                all(int(row["applied"]) for row in target_rows)
                and guidance_content_hash(contents) == last["expected_content_hash"]
                and mode == int(last["baseline_mode"])
            )
        except GuidanceFormatError:
            exact = False
        if previous_base and latest_base:
            old = git(worktree, "rev-parse", f"{previous_base}:{target}", check=False)
            new_blob = git(worktree, "rev-parse", f"{latest_base}:{target}", check=False)
            old_mode = git(worktree, "ls-tree", previous_base, "--", target).stdout
            new_mode = git(worktree, "ls-tree", latest_base, "--", target).stdout
            if old.returncode or new_blob.returncode or old_mode != new_mode:
                # Reapproval during a paused rebase already binds the decision
                # to this base. Do not invalidate it again after --continue.
                base_approved = False
                shown = subprocess.run(
                    ["git", "show", f"{latest_base}:{target}"],
                    cwd=worktree, check=False, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if shown.returncode == 0 and new_mode.startswith(("100644 ", "100755 ")):
                    new_baselines[target] = (
                        shown.stdout,
                        0o755 if new_mode.startswith("100755 ") else 0o644,
                    )
                    base_approved = all(
                        bytes(item["baseline_content"]) == shown.stdout
                        and int(item["baseline_mode"]) == new_baselines[target][1]
                        for item in target_rows
                    )
                exact = exact and base_approved
        if not exact:
            mismatched.extend(proposals[str(row["proposal_id"])] for row in target_rows)
    if not mismatched:
        return False
    _invalidate_guidance_approval(
        connection, repo, worktree, plan, document, tuple(mismatched),
        worker_result,
        "최신 base 또는 게시할 AGENTS.md가 승인 기준과 다릅니다. "
        "새 기준에 적용할 정확한 대체 patch와 User Decision Revision이 필요합니다.",
    )
    with connection:
        for target, (contents, mode) in new_baselines.items():
            connection.execute(
                "UPDATE guidance_approvals SET baseline_content = ?, baseline_mode = ? "
                "WHERE repo = ? AND plan_id = ? AND target_path = ? AND batch_id = ?",
                (contents, mode, repo, plan["plan_id"], target, latest_batches[target]),
            )
    return True


def guidance_resume_context(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> str:
    rows = tuple(
        row
        for row in _guidance_rows(connection, repo, plan_id)
        if not int(row["applied"])
    )
    if not rows:
        return ""
    payload = [
        {
            "proposal_id": str(row["proposal_id"]),
            "target": str(row["target_path"]),
            "scope": str(row["scope"]),
            "decision": str(row["decision"]),
            "revision": str(row["decision_revision"]),
            "patch": str(row["approved_patch"] or ""),
            "patch_hash": str(row["approved_patch_hash"] or ""),
            "expected_content_hash": str(row["expected_content_hash"] or ""),
        }
        for row in rows
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)



def _blocked_execplan_metadata_error(
    repo_path: Path,
    worktree: Path,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    try:
        current = _parse_execplan(worktree, worktree / path)
    except (OSError, RuntimeError) as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"

    return _blocked_execplan_metadata_identity_error(repo_path, current, plan)


def _blocked_execplan_revision_metadata_error(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    shown = git(repo_path, "show", f"{revision}:{path}", check=False)
    if shown.returncode:
        return "BLOCKED ExecPlan을 remote 기준으로 다시 검증할 수 없습니다."
    try:
        current = published_execplan(path, shown.stdout)
    except RuntimeError as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"
    return _blocked_execplan_metadata_identity_error(repo_path, current, plan)


def _update_blocked_error(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    error: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE exec_plans SET last_error = ? WHERE repo = ? AND plan_id = ?",
            (error, repo, plan_id),
        )
        connection.execute(
            "UPDATE projects SET last_error = ? WHERE repo = ?",
            (error, repo),
        )
        notifications.enqueue_current_project_events(connection, repo)


def block_active_execplan(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    error: str,
    verify_result: str,
    *,
    preserved_agent_result_snapshot: str | None = None,
) -> None:
    project = connection.execute(
        "SELECT plan_path, agent_role, agent_snapshot, agent_input_fingerprint, "
        "agent_input_plan "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    plan_id = _plan_id_from_path(str(project["plan_path"] or "")) if project else None
    plan = (
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if plan_id
        else None
    )
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    blocked_worktree_fingerprint = _worktree_execplan_input_fingerprint(
        worktree, plan
    )
    fingerprint = str(
        project["agent_input_fingerprint"] or blocked_worktree_fingerprint
    )
    agent_result_snapshot = preserved_agent_result_snapshot
    if agent_result_snapshot is None and project["agent_snapshot"]:
        try:
            agent_result_snapshot = capture_git_snapshot(worktree).serialize()
        except RuntimeError:
            pass
    agent_result_plan = None
    if (
        project["agent_role"] == "reviewer"
        and project["agent_input_plan"] is not None
    ):
        plan_file = worktree / str(plan["path"])
        if plan_file.is_file():
            agent_result_plan = plan_file.read_text(encoding="utf-8")
    with connection:
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'BLOCKED', blocked_fingerprint = ?,
                blocked_reason = CASE WHEN blocked_reason = 'pull-request-closed'
                    THEN NULL ELSE blocked_reason END,
                blocked_worktree_fingerprint = ?, last_error = ?, verify_result = ?
            WHERE repo = ? AND plan_id = ?
            """,
            (
                fingerprint,
                blocked_worktree_fingerprint,
                error,
                verify_result,
                repo,
                plan_id,
            ),
        )
        connection.execute(
            """
            UPDATE projects
            SET phase = 'BLOCKED', last_error = ?, verify_result = ?,
                agent_result_snapshot = ?, agent_result_plan = ?,
                agent_manager_snapshot = NULL
            WHERE repo = ?
            """,
            (error, verify_result, agent_result_snapshot, agent_result_plan, repo),
        )
        notifications.enqueue_current_project_events(connection, repo)


def transition_validation_failure(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    stage: str,
    output: str,
) -> ValidationFailureDecision:
    """Persist stage-local failures and own their retry/BLOCKED transition."""

    columns = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }
    labels = {"verify": "전체 verify", "review": "Review"}
    history_column = columns.get(stage)
    if history_column is None:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("검증 실패를 기록할 활성 ExecPlan을 찾을 수 없습니다.")
    failures = validation_failure_fingerprints(stage, output, worktree)
    history = _deserialize_failure_history(plan[history_column])
    recorded_failures = []
    for fingerprint, normalized in failures:
        previous = history.get(fingerprint, {"occurrences": 0})
        occurrences = int(previous["occurrences"]) + 1
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": normalized[-2000:],
        }
        recorded_failures.append((fingerprint, normalized, occurrences))
    blocking_failure = next(
        (
            failure
            for failure in recorded_failures
            if failure[2] > MAX_VALIDATION_FINGERPRINT_REPEATS
        ),
        None,
    )
    fingerprint, normalized, occurrences = blocking_failure or max(
        recorded_failures,
        key=lambda failure: failure[2],
    )
    serialized_history = json.dumps(
        history,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    level_decision: WorkerLevelDecision | None = None
    serialized_level_history: str | None = None
    if stage == "review" and plan["effective_worker_level"] is not None:
        level_decision = evaluate_review_worker_level(
            plan["effective_worker_level"],
            _deserialize_review_level_history(plan["review_level_history"]),
            tuple(failure[0] for failure in recorded_failures),
        )
        serialized_level_history = json.dumps(
            level_decision.review_history,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    blocked = blocking_failure is not None
    if blocked:
        error = (
            f"{labels[stage]} 동일 실패가 Worker 수정 후 "
            f"{MAX_VALIDATION_FINGERPRINT_REPEATS}회 다시 발생했습니다.\n"
            f"실패 지문: {fingerprint}\n{output[-8000:]}"
        )
        blocked_input = _worktree_execplan_input_fingerprint(worktree, plan)
    else:
        prefix = "전체 검증 실패" if stage == "verify" else "Review 실패"
        error = f"{prefix}:\n{output[-8000:]}"
        blocked_input = None

    execplan_assignments = [
        f"{history_column} = ?",
        "validated_base_sha = NULL",
        "status = ?",
        "last_error = ?",
    ]
    execplan_values: list[object] = [
        serialized_history,
        "BLOCKED" if blocked else "IMPLEMENTING",
        error,
    ]
    project_assignments = [
        "validated_base_sha = NULL", "phase = ?", "last_error = ?"
    ]
    project_values: list[object] = ["BLOCKED" if blocked else "WORKING", error]
    if stage == "verify":
        execplan_assignments.append("verify_result = ?")
        execplan_values.append(output)
        project_assignments.append("verify_result = ?")
        project_values.append(output)
    else:
        execplan_assignments.append("review_result = ?")
        execplan_values.append(output)
        project_assignments.append("review_result = ?")
        project_values.append(output)
        if level_decision is not None:
            execplan_assignments.extend(
                ["effective_worker_level = ?", "review_level_history = ?"]
            )
            execplan_values.extend(
                [level_decision.effective_level, serialized_level_history]
            )
    if blocked:
        execplan_assignments.extend(
            ["blocked_fingerprint = ?", "blocked_worktree_fingerprint = ?"]
        )
        execplan_values.extend([blocked_input, blocked_input])
        project_assignments.append("agent_manager_snapshot = NULL")

    with connection:
        connection.execute(
            f"UPDATE exec_plans SET {', '.join(execplan_assignments)} "
            "WHERE repo = ? AND plan_id = ?",
            (*execplan_values, repo, plan["plan_id"]),
        )
        connection.execute(
            f"UPDATE projects SET {', '.join(project_assignments)} WHERE repo = ?",
            (*project_values, repo),
        )
        if blocked:
            notifications.enqueue_current_project_events(connection, repo)
    return ValidationFailureDecision(
        stage,
        fingerprint,
        occurrences,
        blocked,
    )


def block_active_execplan_from_revision(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    error: str,
    verify_result: str = "",
    *,
    preserved_agent_result_snapshot: str | None = None,
) -> None:
    """Block an active plan when its saved worktree is not safe to read."""

    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    project = connection.execute(
        "SELECT agent_input_fingerprint FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    fingerprint = str(
        (project["agent_input_fingerprint"] if project else None)
        or _revision_execplan_input_fingerprint(repo_path, revision, plan)
    )
    with connection:
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'BLOCKED', blocked_fingerprint = ?,
                blocked_reason = CASE WHEN blocked_reason = 'pull-request-closed'
                    THEN NULL ELSE blocked_reason END,
                blocked_worktree_fingerprint = NULL, last_error = ?, verify_result = ?
            WHERE repo = ? AND plan_id = ?
            """,
            (fingerprint, error, verify_result, repo, plan["plan_id"]),
        )
        connection.execute(
            """
            UPDATE projects
            SET phase = 'BLOCKED', last_error = ?, verify_result = ?,
                agent_result_snapshot = ?, agent_manager_snapshot = NULL
            WHERE repo = ?
            """,
            (error, verify_result, preserved_agent_result_snapshot, repo),
        )

        notifications.enqueue_current_project_events(connection, repo)

def block_persisted_agent_scope_violation(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    row: sqlite3.Row,
    detail: str,
    result_snapshot: GitSnapshot | None = None,
) -> str:
    """Persist a pre-resume scope violation without discarding Agent state."""

    message = f"Agent 범위 선검증 실패:\n{detail}"
    if result_snapshot is not None:
        preserved_snapshot = result_snapshot.serialize()
    elif row["agent_result_snapshot"]:
        preserved_snapshot = str(row["agent_result_snapshot"])
    else:
        preserved_snapshot = str(row["agent_snapshot"])
    plan = active_execplan(connection, repo)
    if plan is None:
        if row["phase"] == "PLANNING":
            update_project(
                connection,
                repo,
                planning_validation_error=detail,
                last_error=message,
                agent_result_snapshot=preserved_snapshot,
            )
        else:
            update_project(
                connection,
                repo,
                phase="BLOCKED",
                last_error=message,
                agent_result_snapshot=preserved_snapshot,
            )
        return message

    saved_worktree = Path(str(row["worktree"] or ""))
    if saved_worktree.is_dir():
        block_active_execplan(
            connection,
            repo,
            saved_worktree,
            message,
            str(row["verify_result"] or ""),
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    else:
        revision = str(
            plan["plan_sha"]
            or row["active_sha"]
            or f"refs/remotes/origin/{row['base_branch']}"
        )
        block_active_execplan_from_revision(
            connection,
            repo_path,
            repo,
            revision,
            message,
            str(row["verify_result"] or ""),
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    return message


def recover_persisted_agent_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
) -> None:
    """Recover a missing Agent worktree only from its proven owned branch."""

    worktree = Path(str(project["worktree"] or ""))
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    plan = active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    if (
        project["phase"] == "PLANNING"
        or plan_id is None
        or plan is None
        or plan["plan_id"] != plan_id
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or not branch
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "저장된 Agent worktree의 Loop Manager 소유권을 증명할 수 없습니다: "
            f"{worktree}"
        )
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            plan_id,
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"저장된 Agent worktree 복구 실패: {error}"
        ) from error


def _blocked_agent_local_baseline(
    before: GitSnapshot,
    result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    *,
    allowed_dirty_paths: frozenset[str] = frozenset(),
) -> GitSnapshot:
    """Accept a diagnosed result plus explicit user-edited input files."""

    if (
        not before.shared_metadata_complete
        or not result.shared_metadata_complete
        or not current.shared_metadata_complete
    ):
        raise AgentScopeError("저장된 BLOCKED Agent Git metadata 기준 snapshot이 없습니다.")

    new_local_changes: set[str] = set()
    for state_name, before_state, result_state, current_state in (
        ("index", before.index, result.index, current.index),
        ("dirty", before.dirty, result.dirty, current.dirty),
    ):
        for path in set(before_state) | set(result_state) | set(current_state):
            value = current_state.get(path)
            if (
                value not in (before_state.get(path), result_state.get(path))
                and not (state_name == "dirty" and path in allowed_dirty_paths)
            ):
                new_local_changes.add(path)
    if new_local_changes:
        displayed = ", ".join(sorted(new_local_changes)[:8])
        remainder = len(new_local_changes) - 8
        if remainder > 0:
            displayed += f", 외 {remainder}개"
        raise AgentScopeError(
            "BLOCKED 이후 새 파일 또는 index 변경을 자동 보존할 수 없습니다: "
            + displayed
        )

    worktree_key = str(_canonical_path(worktree))
    if current.worktrees.get(worktree_key) is None:
        raise AgentScopeError("BLOCKED Agent 결과에 관리 worktree 등록이 없습니다.")
    return GitSnapshot(
        head=before.head,
        branch=before.branch,
        index=current.index,
        dirty=current.dirty,
        refs=before.refs,
        worktrees=before.worktrees,
        config=before.config,
        operations=before.operations,
    )


def _diagnosed_blocked_commit_allowances(
    before: GitSnapshot,
    blocked_result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    scope_before: GitSnapshot,
) -> tuple[GitSnapshot, frozenset[str], frozenset[str]]:
    """Allow only the exact diagnosed commit while it awaits isolation."""

    if blocked_result.head == before.head:
        return scope_before, frozenset(), frozenset()
    if (
        before.branch is None
        or blocked_result.branch != before.branch
        or current.branch != before.branch
        or current.head != blocked_result.head
    ):
        return scope_before, frozenset(), frozenset()

    branch_ref = f"refs/heads/{before.branch}"
    result_branch_ref = blocked_result.refs.get(branch_ref)
    if (
        result_branch_ref is None
        or result_branch_ref[0] != blocked_result.head
        or current.refs.get(branch_ref) != result_branch_ref
    ):
        return scope_before, frozenset(), frozenset()

    worktree_key = str(_canonical_path(worktree))
    result_registration = blocked_result.worktrees.get(worktree_key)
    if (
        result_registration is None
        or current.worktrees.get(worktree_key) != result_registration
    ):
        return scope_before, frozenset(), frozenset()

    return (
        GitSnapshot(
            head=current.head,
            branch=scope_before.branch,
            index=scope_before.index,
            dirty=scope_before.dirty,
            refs=scope_before.refs,
            worktrees=scope_before.worktrees,
            config=scope_before.config,
            operations=scope_before.operations,
        ),
        frozenset((branch_ref,)),
        frozenset((worktree_key,)),
    )


def validate_persisted_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    allow_planner_worktree_correction: bool = False,
    manager_cleanup_worktrees: frozenset[str] = frozenset(),
) -> GitSnapshot | None:
    """Validate persisted Agent output before repository-aware Git work."""

    project = connection.execute(
        "SELECT phase, base_branch, branch, worktree, agent_role, agent_snapshot, "
        "agent_result_snapshot, agent_manager_snapshot, plan_path FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_snapshot"]:
        return None
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"저장된 Agent Git snapshot이 유효하지 않습니다: {error}"
        ) from error
    if not before.shared_metadata_complete:
        raise AgentScopeError("저장된 Agent Git metadata 기준 snapshot이 없습니다.")

    saved_worktree = Path(str(project["worktree"] or ""))
    config_context = saved_worktree if saved_worktree.is_dir() else repo_path
    current_config = _config_entries(config_context)
    changed_keys = sorted(
        key
        for key in set(before.config) | set(current_config)
        if before.config.get(key) != current_config.get(key)
    )
    if changed_keys:
        displayed = ", ".join(changed_keys[:8])
        remainder = len(changed_keys) - 8
        if remainder > 0:
            displayed += f", 외 {remainder}개"
        raise AgentScopeError(f"저장된 Agent Git config 변경 ({displayed})")
    role_name = str(project["agent_role"] or "")
    if role_name not in {"planner", "worker", "reviewer"}:
        raise AgentScopeError(
            f"저장된 Agent 역할이 유효하지 않습니다: {role_name or '없음'}"
        )
    selected_plan_path = str(project["plan_path"] or "")
    if role_name == "worker" and not selected_plan_path:
        raise AgentScopeError("저장된 Worker ExecPlan 경로가 없습니다.")
    guidance_targets: tuple[str, ...] = ()
    allow_common_plans = False
    if role_name == "worker":
        active_plan = active_execplan(connection, repo)
        if active_plan is None:
            raise AgentScopeError("저장된 Worker ExecPlan 상태가 없습니다.")
        guidance_targets = _guidance_worker_targets(
            connection, repo, str(active_plan["plan_id"])
        )
        allow_common_plans = _published_plan_allows_common_plans(
            repo_path, active_plan
        )
    if not saved_worktree.is_dir():
        recover_persisted_agent_worktree(
            connection, repo_path, state_dir, repo, project
        )

    current = capture_git_snapshot(saved_worktree)
    if allow_planner_worktree_correction:
        # The existing invalid commit/file diff is handled by the Planner
        # correction path below. Accept that already-diagnosed HEAD, index and
        # worktree state here while still rejecting branch switches and every
        # shared Git metadata mutation before Manager Git commands can run.
        planner_result: GitSnapshot | None = None
        correction_worktrees = dict(before.worktrees)
        saved_worktree_key = str(_canonical_path(saved_worktree))
        before_registration = before.worktrees.get(saved_worktree_key)
        current_registration = current.worktrees.get(saved_worktree_key)
        if (
            before_registration is not None
            and current_registration is not None
            and tuple(
                field
                for field in before_registration
                if not field.startswith("HEAD=")
            )
            == tuple(
                field
                for field in current_registration
                if not field.startswith("HEAD=")
            )
        ):
            correction_worktrees[saved_worktree_key] = current_registration
        allowed_ref_changes: frozenset[str] = frozenset()
        allowed_worktree_changes: frozenset[str] = frozenset()
        if project["agent_result_snapshot"]:
            try:
                planner_result = GitSnapshot.deserialize(
                    str(project["agent_result_snapshot"])
                )
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                raise AgentScopeError(
                    f"저장된 Planner 결과 snapshot이 유효하지 않습니다: {error}"
                ) from error
            if planner_result.shared_metadata_complete:
                base = str(project["base_branch"])
                allowed_ref_changes = _metadata_changes_after_block(
                    before.refs,
                    planner_result.refs,
                    current.refs,
                    frozenset(
                        (
                            f"refs/heads/{base}",
                            f"refs/remotes/origin/{base}",
                        )
                    ),
                )
                allowed_worktree_changes = _metadata_changes_after_block(
                    before.worktrees,
                    planner_result.worktrees,
                    current.worktrees,
                    frozenset((str(_canonical_path(repo_path)),)),
                )
        if (
            planner_result is not None
            and planner_result.worktrees.get(saved_worktree_key) != current_registration
        ):
            correction_worktrees[saved_worktree_key] = before_registration
        correction_result = planner_result or current
        correction_before = GitSnapshot(
            head=correction_result.head,
            branch=before.branch,
            index=correction_result.index,
            dirty=correction_result.dirty,
            refs=before.refs,
            worktrees=correction_worktrees,
            config=before.config,
            operations=before.operations,
        )
        validate_agent_scope(
            "planner",
            saved_worktree,
            correction_before,
            current,
            allowed_ref_changes=allowed_ref_changes,
            allowed_worktree_changes=allowed_worktree_changes,
            external_worktree=repo_path,
            base_branch=str(project["base_branch"]),
        )
        return current

    if project["agent_manager_snapshot"]:
        try:
            checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
        if project["phase"] != "BLOCKED":
            validate_agent_scope(
                role_name,
                saved_worktree,
                before,
                checkpoint.snapshot,
                selected_plan_path=(
                    selected_plan_path if role_name == "worker" else None
                ),
                allowed_guidance_targets=guidance_targets,
                allow_common_plans=allow_common_plans,
                external_worktree=repo_path,
                base_branch=str(project["base_branch"]),
            )
        checkpoint_scope_before = checkpoint.snapshot
        if project["phase"] == "BLOCKED" and selected_plan_path:
            checkpoint_scope_before = _blocked_agent_local_baseline(
                checkpoint.snapshot,
                checkpoint.snapshot,
                current,
                saved_worktree,
                allowed_dirty_paths=frozenset((selected_plan_path,)),
            )
        manager_refs = frozenset(
            refname
            for refname in set(checkpoint.snapshot.refs) | set(current.refs)
            if checkpoint.snapshot.refs.get(refname) != current.refs.get(refname)
            and (
                refname.startswith("refs/remotes/origin/")
                or refname.startswith("refs/tags/")
                or (
                    project["phase"] == "BLOCKED"
                    and refname == f"refs/heads/{project['base_branch']}"
                )
            )
        )
        allowed_cleanup = checkpoint.cleanup_worktrees | manager_cleanup_worktrees
        if project["phase"] == "BLOCKED":
            allowed_cleanup |= frozenset((str(_canonical_path(repo_path)),))
        try:
            validate_agent_scope(
                "reviewer",
                saved_worktree,
                checkpoint_scope_before,
                current,
                allowed_ref_changes=manager_refs,
                allowed_worktree_changes=allowed_cleanup,
                external_worktree=repo_path,
                base_branch=str(project["base_branch"]),
            )
        except AgentScopeError as error:
            raise AgentScopeError(
                f"중단된 Manager Git 작업 후 Agent scope 검증 실패: {error}"
            ) from error
        return current

    scope_before = before
    allowed_ref_changes: frozenset[str] = frozenset()
    allowed_worktree_changes: frozenset[str] = frozenset()
    if project["phase"] == "BLOCKED" and project["agent_result_snapshot"]:
        try:
            blocked_result = GitSnapshot.deserialize(
                str(project["agent_result_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Agent 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error
        if blocked_result.shared_metadata_complete:
            scope_before = _blocked_agent_local_baseline(
                before,
                blocked_result,
                current,
                saved_worktree,
                allowed_dirty_paths=(
                    frozenset((selected_plan_path,))
                    if selected_plan_path
                    else frozenset()
                ),
            )
            (
                scope_before,
                commit_ref_changes,
                commit_worktree_changes,
            ) = _diagnosed_blocked_commit_allowances(
                before,
                blocked_result,
                current,
                saved_worktree,
                scope_before,
            )
            base = str(project["base_branch"])
            manager_ref_candidates = frozenset(
                {f"refs/heads/{base}"}
                | {
                    refname
                    for refname in set(before.refs) | set(current.refs)
                    if refname.startswith("refs/remotes/origin/")
                    or refname.startswith("refs/tags/")
                }
            )
            allowed_ref_changes = _metadata_changes_after_block(
                before.refs,
                blocked_result.refs,
                current.refs,
                manager_ref_candidates,
            )
            allowed_worktree_changes = _metadata_changes_after_block(
                before.worktrees,
                blocked_result.worktrees,
                current.worktrees,
                frozenset((str(_canonical_path(repo_path)),)),
            )
            allowed_ref_changes |= commit_ref_changes
            allowed_worktree_changes |= commit_worktree_changes

    validate_agent_scope(
        role_name,
        saved_worktree,
        scope_before,
        current,
        selected_plan_path=(
            selected_plan_path if role_name == "worker" else None
        ),
        allowed_guidance_targets=guidance_targets,
        allow_common_plans=allow_common_plans,
        allowed_ref_changes=allowed_ref_changes,
        allowed_worktree_changes=allowed_worktree_changes,
        external_worktree=repo_path,
        base_branch=str(project["base_branch"]),
    )

    return current


def refresh_persisted_agent_scope_after_manager_git(
    connection: sqlite3.Connection,
    repo: str,
    validated: GitSnapshot,
    manager_worktree_changes: frozenset[str],
) -> GitSnapshot:
    """Merge trusted Manager refs into an interrupted Agent's start snapshot."""

    project = connection.execute(
        "SELECT base_branch, worktree, agent_snapshot FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_snapshot"]:
        raise AgentScopeError("갱신할 저장된 Agent Git snapshot이 없습니다.")
    stored = GitSnapshot.deserialize(str(project["agent_snapshot"]))
    worktree = Path(str(project["worktree"] or ""))
    if not worktree.is_dir():
        raise AgentScopeError(
            f"저장된 Agent worktree를 확인할 수 없습니다: {worktree}"
        )

    current = capture_git_snapshot(worktree)
    manager_refs = frozenset(
        refname
        for refname in set(validated.refs) | set(current.refs)
        if validated.refs.get(refname) != current.refs.get(refname)
        and (
            refname.startswith("refs/remotes/origin/")
            or refname.startswith("refs/tags/")
        )
    )
    try:
        validate_agent_scope(
            "reviewer",
            worktree,
            validated,
            current,
            allowed_ref_changes=manager_refs,
            allowed_worktree_changes=manager_worktree_changes,
            external_worktree=Path(repo),
            base_branch=str(project["base_branch"]),
        )
    except AgentScopeError as error:
        raise AgentScopeError(
            f"Manager Git 작업 후 Agent scope 갱신 실패: {error}"
        ) from error
    refreshed_worktrees = dict(stored.worktrees)
    for path in manager_worktree_changes:
        registration = current.worktrees.get(path)
        if registration is None:
            refreshed_worktrees.pop(path, None)
        else:
            refreshed_worktrees[path] = registration
    refreshed = GitSnapshot(
        head=stored.head,
        branch=stored.branch,
        index=stored.index,
        dirty=stored.dirty,
        refs=current.refs,
        worktrees=refreshed_worktrees,
        config=stored.config,
        operations=stored.operations,
    )
    update_project(
        connection,
        repo,
        agent_snapshot=refreshed.serialize(),
        agent_manager_snapshot=None,
    )
    return refreshed


def validate_blocked_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    worktree: Path,
    selected_plan_path: str,
) -> None:
    """Revalidate preserved Agent output before a BLOCKED plan can resume."""

    project = connection.execute(
        "SELECT agent_role, agent_snapshot, agent_result_snapshot, agent_manager_snapshot "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        raise AgentScopeError("BLOCKED Agent 범위를 확인할 프로젝트가 없습니다.")
    serialized = project["agent_snapshot"]
    if not serialized:
        return
    role_name = str(project["agent_role"] or "")
    if role_name not in {"planner", "worker", "reviewer"}:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent 역할이 유효하지 않습니다: {role_name or '없음'}"
        )
    try:
        before = GitSnapshot.deserialize(str(serialized))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent Git snapshot이 유효하지 않습니다: {error}"
        ) from error
    current = capture_git_snapshot(worktree)
    scope_before = before
    allowed_ref_changes: frozenset[str] = frozenset()
    allowed_worktree_changes: frozenset[str] = frozenset()
    if project["agent_manager_snapshot"]:
        try:
            manager_checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
        allowed_worktree_changes |= manager_checkpoint.cleanup_worktrees
    result_serialized = project["agent_result_snapshot"]
    if result_serialized:
        try:
            blocked_result = GitSnapshot.deserialize(str(result_serialized))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Agent 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error

        if (
            before.shared_metadata_complete
            and blocked_result.shared_metadata_complete
            and current.shared_metadata_complete
        ):
            scope_before = _blocked_agent_local_baseline(
                before,
                blocked_result,
                current,
                worktree,
                allowed_dirty_paths=frozenset((selected_plan_path,)),
            )
            (
                scope_before,
                commit_ref_changes,
                commit_worktree_changes,
            ) = _diagnosed_blocked_commit_allowances(
                before,
                blocked_result,
                current,
                worktree,
                scope_before,
            )
            manager_ref_candidates = frozenset(
                {f"refs/heads/{base}"}
                | {
                    refname
                    for refname in set(before.refs) | set(current.refs)
                    if refname.startswith("refs/remotes/origin/")
                    or refname.startswith("refs/tags/")
                }
            )
            allowed_ref_changes = _metadata_changes_after_block(
                before.refs,
                blocked_result.refs,
                current.refs,
                manager_ref_candidates,
            )
            primary_worktree = str(_canonical_path(repo_path))
            allowed_worktree_changes |= _metadata_changes_after_block(
                before.worktrees,
                blocked_result.worktrees,
                current.worktrees,
                frozenset((primary_worktree,)),
            )
            allowed_ref_changes |= commit_ref_changes
            allowed_worktree_changes |= commit_worktree_changes

    validate_agent_scope(
        role_name,
        worktree,
        scope_before,
        current,
        selected_plan_path=(
            selected_plan_path if role_name == "worker" else None
        ),
        allowed_ref_changes=allowed_ref_changes,
        allowed_worktree_changes=allowed_worktree_changes,
        external_worktree=repo_path,
        base_branch=base,
    )


def _reconcile_blocked_reviewer_plan(
    relative_path: str,
    before_text: str,
    result_text: str,
    current_text: str,
) -> str:
    """Remove Reviewer edits while retaining separable user edits."""

    if before_text == result_text:
        return current_text

    before_lines = before_text.splitlines(keepends=True)
    result_lines = result_text.splitlines(keepends=True)
    current_lines = current_text.splitlines(keepends=True)
    user_equal_blocks = [
        (result_start, result_end, current_start, current_end)
        for tag, result_start, result_end, current_start, current_end in (
            difflib.SequenceMatcher(
                None,
                result_lines,
                current_lines,
                autojunk=False,
            ).get_opcodes()
        )
        if tag == "equal"
    ]
    reverse_edits: list[tuple[int, int, list[str]]] = []
    agent_edits = difflib.SequenceMatcher(
        None,
        result_lines,
        before_lines,
        autojunk=False,
    ).get_opcodes()
    for tag, result_start, result_end, before_start, before_end in agent_edits:
        if tag == "equal":
            continue
        if result_start == result_end:
            positions = {
                current_start + result_start - equal_start
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start <= equal_end
            }
            if len(positions) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start = current_end = positions.pop()
        else:
            ranges = {
                (
                    current_start + result_start - equal_start,
                    current_start + result_end - equal_start,
                )
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start and result_end <= equal_end
            }
            if len(ranges) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start, current_end = ranges.pop()
        reverse_edits.append(
            (current_start, current_end, before_lines[before_start:before_end])
        )

    for current_start, current_end, replacement in reversed(reverse_edits):
        current_lines[current_start:current_end] = replacement
    return "".join(current_lines)


def _deserialize_blocked_agent_recovery(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent 복구 상태가 유효하지 않습니다: {error}"
        ) from error
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or parsed.get("state") not in {"prepared", "stashed", "isolated"}
        or not isinstance(parsed.get("source_snapshot"), str)
        or not isinstance(parsed.get("original_head"), str)
        or not isinstance(parsed.get("violation_head"), str)
        or not isinstance(parsed.get("branch"), str)
        or not isinstance(parsed.get("stash_marker"), str)
        or not isinstance(parsed.get("violation_ref"), str)
        or (
            parsed.get("plan_temp_path") is not None
            and not isinstance(parsed.get("plan_temp_path"), str)
        )
        or (
            parsed.get("plan_mode") is not None
            and not isinstance(parsed.get("plan_mode"), int)
        )
        or (
            parsed.get("baseline_snapshot") is not None
            and not isinstance(parsed.get("baseline_snapshot"), str)
        )
        or (
            parsed.get("plan_path") is not None
            and not isinstance(parsed.get("plan_path"), str)
        )
        or (
            parsed.get("plan_contents") is not None
            and not isinstance(parsed.get("plan_contents"), str)
        )
    ):
        raise AgentScopeError("저장된 BLOCKED Agent 복구 상태 형식을 지원하지 않습니다.")
    return parsed


def _deserialize_agent_baseline(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise AgentScopeError(
            f"저장된 Agent 시작 상태가 유효하지 않습니다: {error}"
        ) from error
    required = ("ref", "ref_target", "index_tree", "worktree_tree")
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or any(not isinstance(parsed.get(key), str) for key in required)
    ):
        raise AgentScopeError("저장된 Agent 시작 상태 형식을 지원하지 않습니다.")
    return {key: str(parsed[key]) for key in required}


def _restore_agent_index_flags(
    worktree: Path,
    entries: Mapping[str, tuple[str, ...]],
) -> None:
    for relative_path, states in sorted(entries.items()):
        tags = {state.split(" ", 1)[0] for state in states}
        if any(tag.lower() == "s" for tag in tags):
            git(worktree, "update-index", "--skip-worktree", "--", relative_path)
        if any(tag.islower() for tag in tags):
            git(worktree, "update-index", "--assume-unchanged", "--", relative_path)


def _restore_agent_staged_entries(
    worktree: Path,
    before: GitSnapshot,
) -> None:
    """Restore the pre-Agent stage-0 entries after rebase autostash."""

    for relative_path, (kinds, _signature) in sorted(before.dirty.items()):
        if "staged" not in kinds:
            continue
        stage_zero: tuple[str, str] | None = None
        for entry in before.index.get(relative_path, ()):
            fields = entry.split()
            if len(fields) != 4:
                raise AgentScopeError(
                    f"저장된 Agent index 항목이 유효하지 않습니다: {relative_path}"
                )
            _tag, mode, object_id, stage = fields
            if stage == "0":
                stage_zero = (mode, object_id)
        if stage_zero is None:
            git(worktree, "update-index", "--force-remove", "--", relative_path)
        else:
            mode, object_id = stage_zero
            git(
                worktree,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                object_id,
                relative_path,
            )
    _restore_agent_index_flags(worktree, before.index)


def _restore_agent_baseline(
    worktree: Path,
    before: GitSnapshot,
    serialized: str | None,
) -> None:
    if not before.dirty:
        return
    if serialized is None:
        raise AgentScopeError(
            "dirty Agent 시작 상태를 복원할 보존 tree가 없습니다."
        )
    baseline = _deserialize_agent_baseline(serialized)
    ref_target = git(
        worktree,
        "rev-parse",
        "--verify",
        baseline["ref"],
        check=False,
    )
    if ref_target.returncode or ref_target.stdout.strip() != baseline["ref_target"]:
        raise AgentScopeError("Agent 시작 상태 보존 ref가 변경되거나 사라졌습니다.")
    worktree_tree = git(
        worktree,
        "rev-parse",
        baseline["ref_target"] + "^{tree}",
    ).stdout.strip()
    index_tree = git(
        worktree,
        "rev-parse",
        baseline["ref_target"] + "^1^{tree}",
    ).stdout.strip()
    if (
        worktree_tree != baseline["worktree_tree"]
        or index_tree != baseline["index_tree"]
    ):
        raise AgentScopeError("Agent 시작 상태 보존 tree가 저장된 값과 다릅니다.")

    git(worktree, "read-tree", "--reset", "-u", worktree_tree)
    git(worktree, "read-tree", index_tree)
    _restore_agent_index_flags(worktree, before.index)
    restored = capture_git_snapshot(worktree)
    if not _same_local_snapshot(before, restored):
        raise AgentScopeError("Agent 시작 시점의 기존 변경을 정확히 복원하지 못했습니다.")


def _worktree_relative_target(worktree: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AgentScopeError(f"복구 경로가 worktree 밖을 가리킵니다: {relative_path}")
    target = worktree / candidate
    try:
        target.parent.resolve().relative_to(worktree.resolve())
    except ValueError as error:
        raise AgentScopeError(
            f"복구 경로의 상위 디렉터리가 worktree 밖을 가리킵니다: {relative_path}"
        ) from error
    return target


def _remove_interrupted_recovery_temp(
    worktree: Path,
    relative_path: str | None,
) -> None:
    if relative_path is None:
        return
    target = _worktree_relative_target(worktree, relative_path)
    if target.is_dir() and not target.is_symlink():
        raise AgentScopeError(f"복구 임시 경로가 디렉터리입니다: {relative_path}")
    if target.exists() or target.is_symlink():
        target.unlink()


def _atomic_restore_file(
    worktree: Path,
    relative_path: str,
    temp_relative_path: str,
    contents: str,
    mode: int,
) -> None:
    target = _worktree_relative_target(worktree, relative_path)
    temporary = _worktree_relative_target(worktree, temp_relative_path)
    _remove_interrupted_recovery_temp(worktree, temp_relative_path)
    try:
        with temporary.open("xb") as handle:
            handle.write(contents.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 원자적 복원 실패: {error}"
        ) from error


def _serialize_blocked_agent_recovery(recovery: Mapping[str, object]) -> str:
    return json.dumps(
        recovery,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _blocked_recovery_stash_oid(worktree: Path, marker: str) -> str | None:
    listed = git(
        worktree,
        "stash",
        "list",
        "--format=%H%x00%gs",
        check=False,
    )
    if listed.returncode:
        raise AgentScopeError(
            f"BLOCKED Agent stash 확인 실패:\n{listed.stdout[-8000:]}"
        )
    for line in listed.stdout.splitlines():
        oid, separator, subject = line.partition("\0")
        if separator and subject.endswith(marker):
            return oid
    return None


def _restore_blocked_agent_recovery_plan(
    worktree: Path,
    plan_path: str | None,
    plan_contents: str | None,
    plan_temp_path: str | None,
    plan_mode: int | None,
) -> None:
    if plan_path is None:
        return
    target = _worktree_relative_target(worktree, plan_path)
    try:
        if plan_contents is None:
            if target.exists() or target.is_symlink():
                target.unlink()
        else:
            if plan_temp_path is None or plan_mode is None:
                raise AgentScopeError(
                    "BLOCKED ExecPlan 원자적 복원 정보가 없습니다."
                )
            _atomic_restore_file(
                worktree,
                plan_path,
                plan_temp_path,
                plan_contents,
                plan_mode,
            )
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 복원 실패: {error}"
        ) from error


def _recovered_agent_local_state_matches(
    worktree: Path,
    before: GitSnapshot,
    current: GitSnapshot,
    plan_path: str | None,
    plan_contents: str | None,
    plan_mode: int | None,
) -> bool:
    if (
        current.head != before.head
        or current.branch != before.branch
        or current.index != before.index
        or current.operations != before.operations
    ):
        return False
    current_dirty = dict(current.dirty)
    before_dirty = dict(before.dirty)
    if plan_path is None:
        return current_dirty == before_dirty
    current_dirty.pop(plan_path, None)
    before_dirty.pop(plan_path, None)
    if current_dirty != before_dirty:
        return False
    target = _worktree_relative_target(worktree, plan_path)
    if plan_contents is None:
        return not target.exists() and not target.is_symlink()
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(target_stat.st_mode)
        and stat.S_IMODE(target_stat.st_mode) == plan_mode
        and target.read_text(encoding="utf-8") == plan_contents
    )


def _mark_blocked_agent_isolated(
    connection: sqlite3.Connection,
    repo: str,
    recovery: dict[str, object],
    before: GitSnapshot,
) -> None:
    recovery["state"] = "isolated"
    recovery["baseline_snapshot"] = before.serialize()
    update_project(
        connection,
        repo,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_recovery=_serialize_blocked_agent_recovery(recovery),
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def _complete_blocked_agent_recovery(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
) -> None:
    project = connection.execute(
        "SELECT agent_snapshot, agent_baseline, agent_recovery "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return
    if not project["agent_snapshot"]:
        raise AgentScopeError("BLOCKED Agent 복구에 시작 snapshot이 없습니다.")

    recovery = _deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        source = GitSnapshot.deserialize(str(recovery["source_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 snapshot이 유효하지 않습니다: {error}"
        ) from error

    current_config = _config_entries(worktree)
    if current_config != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    marker = str(recovery["stash_marker"])
    stash_oid = _blocked_recovery_stash_oid(worktree, marker)
    if recovery["state"] == "prepared":
        if stash_oid is None:
            current = capture_git_snapshot(worktree)
            if current.serialize() != source.serialize():
                raise AgentScopeError(
                    "BLOCKED Agent 복구 준비 후 worktree 상태가 추가로 변경됐습니다."
                )
            stashed = git(
                worktree,
                "stash",
                "push",
                "--all",
                "-m",
                marker,
                check=False,
            )
            if stashed.returncode:
                raise AgentScopeError(
                    f"BLOCKED Agent 변경 보존 실패:\n{stashed.stdout[-8000:]}"
                )
            stash_oid = _blocked_recovery_stash_oid(worktree, marker)
        recovery["state"] = "stashed"
        recovery["stash_oid"] = stash_oid
        update_project(
            connection,
            repo,
            agent_recovery=_serialize_blocked_agent_recovery(recovery),
        )

    branch = str(recovery["branch"])
    original_head = str(recovery["original_head"])
    violation_head = str(recovery["violation_head"])
    plan_path_value = recovery.get("plan_path")
    plan_path = str(plan_path_value) if plan_path_value is not None else None
    plan_contents_value = recovery.get("plan_contents")
    plan_contents = (
        str(plan_contents_value) if plan_contents_value is not None else None
    )
    plan_temp_value = recovery.get("plan_temp_path")
    plan_temp_path = (
        str(plan_temp_value) if plan_temp_value is not None else None
    )
    plan_mode_value = recovery.get("plan_mode")
    plan_mode = int(plan_mode_value) if plan_mode_value is not None else None
    _remove_interrupted_recovery_temp(worktree, plan_temp_path)
    current = capture_git_snapshot(worktree)
    if current.branch != branch:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree branch가 저장된 branch와 다릅니다."
        )
    if current.operations:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree에 Git operation이 남아 있습니다."
        )
    if before.operations:
        raise AgentScopeError(
            "Agent 실행 전 Git operation이 있는 worktree는 자동 복구할 수 없습니다."
        )

    if _recovered_agent_local_state_matches(
        worktree,
        before,
        current,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        _mark_blocked_agent_isolated(connection, repo, recovery, before)
        return

    if current.head == violation_head and violation_head != original_head:
        if current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent stash 후 worktree에 새 변경이 생겼습니다."
            )
        violation_ref = str(recovery["violation_ref"])
        existing_ref = git(
            worktree,
            "rev-parse",
            "--verify",
            violation_ref,
            check=False,
        )
        if existing_ref.returncode == 0:
            if existing_ref.stdout.strip() != violation_head:
                raise AgentScopeError(
                    f"BLOCKED Agent 진단 ref 충돌: {violation_ref}"
                )
        else:
            git(worktree, "update-ref", violation_ref, violation_head)
        git(worktree, "reset", "--hard", original_head)
        current = capture_git_snapshot(worktree)
    elif current.head != original_head:
        raise AgentScopeError(
            "BLOCKED Agent 복구 HEAD가 저장된 Agent 결과 또는 시작 commit과 다릅니다."
        )

    if not _same_local_snapshot(before, current):
        if current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent 복구 중 예상하지 못한 새 worktree 변경이 생겼습니다."
            )
        _restore_agent_baseline(
            worktree,
            before,
            (
                str(project["agent_baseline"])
                if project["agent_baseline"] is not None
                else None
            ),
        )

    _restore_blocked_agent_recovery_plan(
        worktree,
        plan_path,
        plan_contents,
        plan_temp_path,
        plan_mode,
    )

    restored = capture_git_snapshot(worktree)
    if not _recovered_agent_local_state_matches(
        worktree,
        before,
        restored,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        raise AgentScopeError("BLOCKED Agent 복구 후 저장된 변경 상태와 일치하지 않습니다.")
    _mark_blocked_agent_isolated(connection, repo, recovery, before)


def preserve_blocked_agent_changes(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan_id: str,
    *,
    preserved_input_path: str | None = None,
) -> GitSnapshot | None:
    """Persist and isolate a diagnosed scope violation crash-safely."""

    project = connection.execute(
        "SELECT agent_role, agent_snapshot, agent_result_snapshot, "
        "agent_recovery, agent_input_plan, agent_result_plan "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        raise AgentScopeError("보존할 BLOCKED Agent 프로젝트가 없습니다.")
    if project["agent_recovery"]:
        recovery = _deserialize_blocked_agent_recovery(
            str(project["agent_recovery"])
        )
        if recovery["state"] == "isolated":
            baseline_snapshot = recovery.get("baseline_snapshot")
            if not isinstance(baseline_snapshot, str):
                raise AgentScopeError("격리된 Agent 시작 snapshot이 없습니다.")
            try:
                return GitSnapshot.deserialize(baseline_snapshot)
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                raise AgentScopeError(
                    f"격리된 Agent 시작 snapshot이 유효하지 않습니다: {error}"
                ) from error
        if not project["agent_snapshot"]:
            raise AgentScopeError("복구 중인 BLOCKED Agent 시작 snapshot이 없습니다.")
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        _complete_blocked_agent_recovery(connection, repo, worktree)
        return before
    if not project["agent_snapshot"] or not project["agent_result_snapshot"]:
        return None
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"보존할 BLOCKED Agent snapshot이 유효하지 않습니다: {error}"
        ) from error
    if before.operations:
        raise AgentScopeError(
            "Agent 실행 전 Git operation이 있는 worktree는 자동 격리할 수 없습니다."
        )
    current = capture_git_snapshot(worktree)
    if current.branch != before.branch or current.head not in {
        before.head,
        result.head,
    }:
        raise AgentScopeError(
            "BLOCKED 이후 HEAD 또는 branch가 추가로 변경되어 자동 보존할 수 없습니다."
        )
    allowed_dirty_paths = (
        frozenset((preserved_input_path,))
        if preserved_input_path is not None
        else frozenset()
    )
    _blocked_agent_local_baseline(
        before,
        result,
        current,
        worktree,
        allowed_dirty_paths=allowed_dirty_paths,
    )
    preserved_paths = frozenset(
        path
        for path in allowed_dirty_paths
        if current.dirty.get(path)
        not in (before.dirty.get(path), result.dirty.get(path))
    )
    plan_path: str | None = None
    plan_contents: str | None = None
    plan_mode: int | None = None
    if preserved_paths:
        if len(preserved_paths) != 1:
            raise AgentScopeError(
                "사용자가 수정한 BLOCKED 입력 경로를 하나로 결정할 수 없습니다."
            )
        plan_path = next(iter(preserved_paths))
        plan_file = worktree / plan_path
        if plan_file.is_symlink() or (plan_file.exists() and not plan_file.is_file()):
            raise AgentScopeError(
                f"사용자가 수정한 BLOCKED ExecPlan 형식이 안전하지 않습니다: {plan_path}"
            )
        plan_contents = (
            plan_file.read_text(encoding="utf-8")
            if plan_file.is_file()
            else None
        )
        plan_mode = (
            stat.S_IMODE(plan_file.lstat().st_mode)
            if plan_file.is_file()
            else None
        )
        if project["agent_role"] == "reviewer":
            before_text = project["agent_input_plan"]
            result_text = project["agent_result_plan"]
            agent_changed_path = (
                before.dirty.get(plan_path) != result.dirty.get(plan_path)
            )
            if before_text is None or result_text is None:
                if agent_changed_path:
                    raise AgentScopeError(
                        "BLOCKED ExecPlan의 Reviewer 변경을 분리할 "
                        f"본문 기준이 없습니다: {plan_path}"
                    )
            elif str(before_text) != str(result_text):
                if plan_contents is None:
                    raise AgentScopeError(
                        f"사용자가 수정한 BLOCKED ExecPlan을 읽을 수 없습니다: {plan_path}"
                    )
                plan_contents = _reconcile_blocked_reviewer_plan(
                    plan_path,
                    str(before_text),
                    str(result_text),
                    plan_contents,
                )

    source_serialized = current.serialize()
    recovery_id = hashlib.sha256(
        json.dumps(
            {
                "repo": repo,
                "plan_id": plan_id,
                "source": source_serialized,
                "plan_path": plan_path,
                "plan_contents": plan_contents,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    plan_temp_path: str | None = None
    if plan_path is not None and plan_contents is not None:
        plan_relative = Path(plan_path)
        plan_temp_path = str(
            plan_relative.parent
            / f".{plan_relative.name}.loop-manager-recovery-{recovery_id}.tmp"
        )
    recovery: dict[str, object] = {
        "version": 1,
        "state": "prepared",
        "source_snapshot": source_serialized,
        "original_head": before.head,
        "violation_head": result.head,
        "branch": str(before.branch or ""),
        "stash_marker": f"loop-manager scope violation {plan_id} {recovery_id}",
        "violation_ref": (
            f"refs/loop-manager/violations/{project_id}/{plan_id}/{recovery_id}"
        ),
        "plan_path": plan_path,
        "plan_contents": plan_contents,
        "plan_temp_path": plan_temp_path,
        "plan_mode": plan_mode,
        "stash_oid": None,
    }
    update_project(
        connection,
        repo,
        agent_recovery=_serialize_blocked_agent_recovery(recovery),
    )
    _complete_blocked_agent_recovery(connection, repo, worktree)
    return before


def resume_interrupted_blocked_agent_recovery(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    """Finish a persisted violation isolation before normal repository work."""

    project = connection.execute(
        "SELECT phase, base_branch, branch, worktree, agent_snapshot, "
        "agent_recovery FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return False
    recovery = _deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    if project["phase"] != "BLOCKED":
        raise AgentScopeError(
            "BLOCKED Agent 복구 상태가 활성 BLOCKED 작업과 일치하지 않습니다."
        )
    serialized_before = project["agent_snapshot"]
    if recovery["state"] == "isolated":
        serialized_before = recovery.get("baseline_snapshot")
    if not isinstance(serialized_before, str):
        raise AgentScopeError("BLOCKED Agent 복구 시작 snapshot이 없습니다.")
    try:
        before = GitSnapshot.deserialize(serialized_before)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 시작 snapshot이 유효하지 않습니다: {error}"
        ) from error

    worktree = Path(str(project["worktree"] or ""))
    branch = str(project["branch"] or "")
    config_context = worktree if worktree.is_dir() else repo_path
    if _config_entries(config_context) != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    plan = active_execplan(connection, repo)
    if (
        plan is None
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "BLOCKED Agent 복구 workspace의 Loop Manager 소유권을 증명할 수 없습니다."
        )
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
            allow_rebase=(
                recovery["state"] == "isolated"
                and _rebase_in_progress_for_branch(worktree, branch)
            ),
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 workspace 검증 실패: {error}"
        ) from error

    if recovery["state"] != "isolated":
        _complete_blocked_agent_recovery(connection, repo, worktree)
    return True


def _requeue_blocked_execplan(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is not None and int(plan["pr_conflict_recovery"]):
        with connection:
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'IMPLEMENTING', blocked_fingerprint = NULL,
                    blocked_worktree_fingerprint = NULL, retry_count = 0,
                    blocked_reason = NULL, guidance_plan_hash = NULL,
                    validated_base_sha = NULL, verify_result = NULL,
                    review_result = NULL, last_error = ?,
                    verify_failure_history = '{}',
                    review_failure_history = '{}',
                    effective_worker_level = initial_worker_level,
                    review_level_history = '{}'
                WHERE repo = ? AND plan_id = ?
                """,
                (REBASE_IN_PROGRESS_ERROR, repo, plan_id),
            )
            connection.execute(
                """
                UPDATE projects
                SET phase = 'WORKING', validated_base_sha = NULL,
                    verify_result = NULL, review_result = NULL, last_error = ?,
                    agent_role = NULL, agent_snapshot = NULL,
                    agent_input_fingerprint = NULL, agent_baseline = NULL,
                    agent_recovery = NULL, agent_input_plan = NULL,
                    agent_result_plan = NULL, agent_result_snapshot = NULL,
                    agent_manager_snapshot = NULL
                WHERE repo = ?
                """,
                (REBASE_IN_PROGRESS_ERROR, repo),
            )
        return
    values = _clear_active_values()
    assignments = ", ".join(f"{name} = ?" for name in values)
    with connection:
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'READY', blocked_fingerprint = NULL,
                blocked_worktree_fingerprint = NULL, retry_count = 0,
                blocked_reason = NULL, guidance_plan_hash = NULL,
                last_error = NULL, worker_session_id = NULL,
                reviewer_session_id = NULL, worker_result = NULL,
                verify_result = NULL, review_result = NULL,
                validated_base_sha = NULL, publication_remote_oid = NULL,
                pr_url = NULL, pr_head_oid = NULL,
                verify_failure_history = '{}', review_failure_history = '{}',
                effective_worker_level = initial_worker_level,
                review_level_history = '{}'
            WHERE repo = ? AND plan_id = ?
            """,
            (repo, plan_id),
        )
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?",
            (*values.values(), repo),
        )


def resume_blocked_execplan_if_inputs_changed(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
) -> bool:
    plan = active_execplan(connection, repo)
    if plan is None or plan["status"] != "BLOCKED" or not plan["blocked_fingerprint"]:
        return False
    stored = str(plan["blocked_fingerprint"])
    worktree = Path(str(plan["worktree"]))
    branch = str(plan["branch"] or "")
    revision = f"refs/remotes/origin/{base}"
    remote_input_changed = (
        _revision_execplan_input_fingerprint(repo_path, revision, plan) != stored
    )
    if int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        if not remote_input_changed:
            return False
        try:
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                str(plan["plan_id"]),
                branch,
                worktree,
            )
        except WorkspaceOwnershipError as error:
            message = f"BLOCKED workspace 충돌 해제 검증 실패:\n{error}"
            _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        branch_state = git(
            repo_path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        conflicts = []
        if worktree.exists() or worktree.is_symlink():
            conflicts.append(f"path {worktree}")
        if branch_state.returncode == 0:
            conflicts.append(f"branch {branch}")
        elif branch_state.returncode != 1:
            conflicts.append(f"branch 상태 확인 실패 {branch}")
        if conflicts:
            message = (
                "BLOCKED workspace 충돌이 해소되지 않았습니다: "
                + ", ".join(conflicts)
            )
            _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        metadata_error = _blocked_execplan_revision_metadata_error(
            repo_path,
            revision,
            plan,
        )
        if metadata_error:
            _update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False
        _requeue_blocked_execplan(connection, repo, str(plan["plan_id"]))
        return True

    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except (RuntimeError, WorkspaceOwnershipError) as error:
        message = f"BLOCKED worktree 복구 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    try:
        validate_blocked_agent_scope(
            connection,
            repo_path,
            repo,
            base,
            worktree,
            str(plan["path"]),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 범위 재검증 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    current_worktree_fingerprint = (
        _worktree_execplan_input_fingerprint(worktree, plan)
        if worktree.is_dir()
        else None
    )
    blocked_worktree_fingerprint = plan["blocked_worktree_fingerprint"]
    worktree_changed = bool(
        current_worktree_fingerprint != stored
        and current_worktree_fingerprint != blocked_worktree_fingerprint
    )
    if not worktree_changed and not remote_input_changed:
        return False
    try:
        preserved_before = preserve_blocked_agent_changes(
            connection,
            repo,
            worktree,
            str(plan["plan_id"]),
            preserved_input_path=(str(plan["path"]) if worktree_changed else None),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 변경 보존 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    base_ancestry = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        revision,
        "HEAD",
        check=False,
    )
    if base_ancestry.returncode not in (0, 1):
        error = (
            "BLOCKED 해제 전 원격 base ancestry 확인 실패:\n"
            f"{base_ancestry.stdout[-8000:]}"
        )
        _update_blocked_error(connection, repo, str(plan["plan_id"]), error)
        return False
    if base_ancestry.returncode == 1:
        rebased = git(
            worktree,
            "rebase",
            "--autostash",
            f"origin/{base}",
            check=False,
        )
        if rebased.returncode:
            error = f"BLOCKED 해제 전 rebase 실패:\n{rebased.stdout[-8000:]}"
            _update_blocked_error(connection, repo, str(plan["plan_id"]), error)
            return False
        if preserved_before is not None:
            try:
                _restore_agent_staged_entries(worktree, preserved_before)
            except (AgentScopeError, RuntimeError) as error:
                restore_error = f"BLOCKED 해제 후 index 복원 실패:\n{error}"
                _update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    restore_error,
                )
                return False
    if worktree.is_dir():
        metadata_error = _blocked_execplan_metadata_error(repo_path, worktree, plan)
        if metadata_error:
            _update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False

    clear_agent_scope(connection, repo)
    _requeue_blocked_execplan(connection, repo, str(plan["plan_id"]))
    return True


def complete_merged_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    project = connection.execute(
        "SELECT plan_path, branch FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        raise WorkspaceOwnershipError("Merge cleanup 프로젝트를 찾을 수 없습니다.")
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    if plan_id is None:
        raise WorkspaceOwnershipError("Merge cleanup ExecPlan ID를 확인할 수 없습니다.")
    branch = str(project["branch"] or "")
    _ensure_execplans_schema(connection)
    plan = connection.execute(
        "SELECT branch, worktree, workspace_owned, title FROM exec_plans "
        "WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is None:
        raise WorkspaceOwnershipError(
            f"Merge cleanup ExecPlan을 찾을 수 없습니다: {plan_id}"
        )
    workspace_owned = WORKSPACE_OWNED
    worktree_present = worktree.exists() or worktree.is_symlink()
    if not worktree_present:
        assessment = CleanupAssessment("missing")
    elif (
        int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        workspace_owned = 0
        assessment = CleanupAssessment(
            "unverifiable",
            "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}",
        )
    else:
        try:
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                plan_id,
                branch,
                worktree,
            )
            validate_managed_worktree(repo_path, worktree, branch)
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_owned = 0
            assessment = CleanupAssessment(
                "unverifiable",
                f"Merge cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, pr_head_oid)
            assessment = remove_cleanup_worktree(repo_path, worktree, assessment)
    values = _clear_active_values()
    with connection:
        _ensure_execplans_schema(connection)
        _ensure_cleanup_pending_schema(connection)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            _store_cleanup_pending(
                connection,
                repo,
                worktree,
                pr_url,
                pr_head_oid,
                assessment.reason or assessment.status,
                workspace_owned=workspace_owned,
            )
        if plan_id:
            connection.execute(
                "UPDATE exec_plans SET status = 'MERGED', last_error = NULL, "
                "pr_conflict_recovery = 0, pr_conflict_head_oid = NULL, "
                "pr_conflict_base_oid = NULL WHERE repo = ? AND plan_id = ?",
                (repo, plan_id),
            )
            notifications.record_merged_plan(connection, repo, plan_id, str(plan["title"]))
        unfinished = connection.execute(
            "SELECT 1 FROM exec_plans "
            "WHERE repo = ? AND status != 'MERGED' LIMIT 1",
            (repo,),
        ).fetchone()
        if unfinished is None:
            values["processed_sha"] = None
            values["planning_fingerprint"] = None
        assignments = ", ".join(f"{name} = ?" for name in values)
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?",
            [*values.values(), repo],
        )
    return assessment


def _block_closed_pull_request(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
) -> None:
    error = "PR이 Merge 없이 닫혔습니다."
    with connection:
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'BLOCKED', blocked_reason = ?,
                pr_url = ?, pr_head_oid = ?, last_error = ?
            WHERE repo = ? AND plan_id = ?
            """,
            (
                PR_CLOSED_BLOCK_REASON,
                url,
                head_oid,
                error,
                repo,
                plan["plan_id"],
            ),
        )
        connection.execute(
            """
            UPDATE projects
            SET phase = 'BLOCKED', pr_url = ?, pr_head_oid = ?,
                last_error = ?
            WHERE repo = ?
            """,
            (url, head_oid, error, repo),
        )
        notifications.enqueue_current_project_events(connection, repo)


def _resume_reopened_pull_request(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
    *,
    recovery: bool,
) -> None:
    phase = "WORKING" if recovery else "PR_OPEN"
    status = "IMPLEMENTING" if recovery else "PR_OPEN"
    last_error = REBASE_IN_PROGRESS_ERROR if recovery else None
    execplan_values: dict[str, object] = {
        "status": status,
        "blocked_reason": None,
        "blocked_fingerprint": None,
        "blocked_worktree_fingerprint": None,
        "pr_url": url,
        "pr_head_oid": head_oid,
        "last_error": last_error,
    }
    project_values: dict[str, object] = {
        "phase": phase,
        "pr_url": url,
        "pr_head_oid": head_oid,
        "last_error": last_error,
    }
    if recovery:
        for values in (execplan_values, project_values):
            values.update(
                validated_base_sha=None,
                verify_result=None,
                review_result=None,
            )
    execplan_assignments = ", ".join(
        f"{name} = ?" for name in execplan_values
    )
    project_assignments = ", ".join(
        f"{name} = ?" for name in project_values
    )
    with connection:
        connection.execute(
            f"UPDATE exec_plans SET {execplan_assignments} "
            "WHERE repo = ? AND plan_id = ?",
            (*execplan_values.values(), repo, plan["plan_id"]),
        )
        connection.execute(
            f"UPDATE projects SET {project_assignments} WHERE repo = ?",
            (*project_values.values(), repo),
        )
        notifications.enqueue_current_project_events(connection, repo)

def reconcile_existing_pull_request(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> int | None:
    """Attach one existing head-branch PR and apply its durable state."""

    branch = str(project["branch"] or "")
    url = str(pull_request.get("url") or project["pr_url"] or "")
    if not url:
        raise RuntimeError("기존 PR URL을 확인할 수 없습니다.")
    head_name = pull_request.get("headRefName")
    if head_name and str(head_name) != branch:
        raise PublicationSafetyError(
            f"기존 PR head branch가 관리 branch와 다릅니다: {head_name}"
        )
    head_value = pull_request.get("headRefOid")
    head_oid = str(head_value) if head_value else project["pr_head_oid"]
    state = str(pull_request.get("state") or "OPEN").upper()
    blocked_plan = (
        active_execplan(connection, repo)
        if project["phase"] == "BLOCKED"
        else None
    )
    reopening_closed_pr = bool(
        state == "OPEN"
        and blocked_plan is not None
        and blocked_plan["blocked_reason"] == PR_CLOSED_BLOCK_REASON
    )
    if (
        state in {"OPEN", "CLOSED"}
        and project["phase"] == "BLOCKED"
        and blocked_plan is not None
        and blocked_plan["blocked_reason"] != PR_CLOSED_BLOCK_REASON
    ):
        operation_print(
            "PR 충돌 복구 차단 상태를 유지합니다: "
            + str(project["last_error"] or "사용자 조치가 필요합니다."),
            file=sys.stderr,
        )
        return 1
    if state != "OPEN" or (
        project["phase"] != "PR_OPEN"
        and not int(project["pr_conflict_recovery"])
    ):
        update_project(
            connection,
            repo,
            pr_url=url,
            pr_head_oid=head_oid,
        )

    if state == "OPEN":
        if int(project["pr_conflict_recovery"]):
            try:
                if recover_confirmed_pr_conflict_push(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    project,
                    pull_request,
                ):
                    operation_print(
                        f"기존 PR 충돌 복구 push 확인 완료: {url}"
                    )
                    return 0
                _validate_active_pr_conflict_recovery(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    project,
                    pull_request,
                )
            except (
                PublicationSafetyError,
                WorkspaceOwnershipError,
            ) as error:
                recovery_error = f"PR 충돌 복구 안전 검증 실패: {error}"
                block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{project['base_branch']}",
                    recovery_error,
                    str(project["verify_result"] or ""),
                )
                operation_print(recovery_error, file=sys.stderr)
                return 1
            if project["phase"] in {"PR_OPEN", "BLOCKED"}:
                if reopening_closed_pr:
                    assert blocked_plan is not None
                    _resume_reopened_pull_request(
                        connection,
                        repo,
                        blocked_plan,
                        url,
                        head_oid,
                        recovery=True,
                    )
                else:
                    update_project(
                        connection,
                        repo,
                        phase="WORKING",
                        validated_base_sha=None,
                        verify_result=None,
                        review_result=None,
                        last_error=REBASE_IN_PROGRESS_ERROR,
                    )
            return None

        if project["phase"] == "PR_OPEN":
            try:
                _fetch_pr_conflict_recovery_refs(
                    repo_path, str(project["base_branch"]), branch
                )
                (
                    _plan,
                    _worktree,
                    _branch,
                    observed_head,
                    base_oid,
                ) = _validate_pr_conflict_recovery_ownership(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    project,
                    pull_request,
                )
            except (
                PublicationSafetyError,
                WorkspaceOwnershipError,
            ) as error:
                recovery_error = f"PR_OPEN 충돌 복구 소유권 검증 실패: {error}"
                block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{project['base_branch']}",
                    recovery_error,
                    str(project["verify_result"] or ""),
                )
                operation_print(recovery_error, file=sys.stderr)
                return 1

            mergeability = classify_pull_request_mergeability(pull_request)
            if mergeability != "conflicting":
                if mergeability == "unknown":
                    operation_print(
                        f"PR mergeability 계산 대기 중: {url}"
                    )
                else:
                    operation_print(f"Merge 대기 중: {url}")
                return 0

            update_project(
                connection,
                repo,
                phase="WORKING",
                validated_base_sha=None,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=observed_head,
                pr_conflict_base_oid=base_oid,
                verify_result=None,
                review_result=None,
                last_error=REBASE_IN_PROGRESS_ERROR,
            )
            operation_print(f"PR 충돌 자동 복구 시작: {url}")
            return None
        plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
        worktree = Path(str(project["worktree"] or ""))
        try:
            if plan_id is None:
                raise WorkspaceOwnershipError(
                    "PR_OPEN ExecPlan ID를 확인할 수 없습니다."
                )
            plan = active_execplan(connection, repo)
            if (
                plan is None
                or plan["plan_id"] != plan_id
                or int(plan["workspace_owned"]) != WORKSPACE_OWNED
            ):
                raise WorkspaceOwnershipError(
                    "PR_OPEN workspace의 Loop Manager 소유권이 확인되지 않았습니다."
                )
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                plan_id,
                branch,
                worktree,
            )
            make_worktree(
                repo_path,
                worktree,
                branch,
                str(project["base_branch"]),
                allow_create_branch=False,
                allow_recovery=True,
                allow_existing=True,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_error = (
                f"PR_OPEN workspace 소유권/복구 검증 실패: {error}"
            )
            block_active_execplan_from_revision(
                connection,
                repo_path,
                repo,
                f"refs/remotes/origin/{project['base_branch']}",
                workspace_error,
            )
            operation_print(workspace_error, file=sys.stderr)
            return 1
        if reopening_closed_pr:
            assert blocked_plan is not None
            _resume_reopened_pull_request(
                connection,
                repo,
                blocked_plan,
                url,
                head_oid,
                recovery=False,
            )
        else:
            update_project(
                connection,
                repo,
                phase="PR_OPEN",
                pr_url=url,
                pr_head_oid=head_oid,
                last_error=None,
            )
        operation_print(f"Merge 대기 중: {url}")
        return 0

    if state == "MERGED" or pull_request.get("mergedAt"):
        git(repo_path, "fetch", "origin")
        persisted = connection.execute(
            "SELECT pr_head_oid FROM projects WHERE repo = ?",
            (repo,),
        ).fetchone()
        stored_head_oid = (
            str(persisted["pr_head_oid"])
            if persisted and persisted["pr_head_oid"]
            else None
        )
        worktree = Path(str(project["worktree"] or ""))
        try:
            assessment = complete_merged_worktree(
                connection,
                repo_path,
                repo,
                state_dir,
                worktree,
                url,
                stored_head_oid,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_error = (
                f"Merge cleanup workspace 소유권 검증 실패: {error}"
            )
            block_active_execplan_from_revision(
                connection,
                repo_path,
                repo,
                f"refs/remotes/origin/{project['base_branch']}",
                workspace_error,
            )
            operation_print(workspace_error, file=sys.stderr)
            return 1
        if assessment.status in {"safe", "missing"}:
            operation_print(f"Merge 확인 및 worktree 정리 완료: {url}")
        else:
            operation_print(
                f"Merge 확인 완료; worktree 보존: {worktree} "
                f"({assessment.reason or assessment.status})",
                file=sys.stderr,
            )
        return PROJECT_STEP_PLANNING_PENDING

    if state == "CLOSED":
        plan = active_execplan(connection, repo)
        if plan is None:
            raise RuntimeError("닫힌 PR과 연결된 활성 ExecPlan을 찾을 수 없습니다.")
        _block_closed_pull_request(connection, repo, plan, url, head_oid)
        operation_print(f"닫힌 PR을 다시 열어야 합니다: {url}", file=sys.stderr)
        return 1

    raise RuntimeError(f"지원하지 않는 GitHub PR 상태입니다: {state}")


def resume_pr_conflict_rebase(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    project: sqlite3.Row,
    worktree: Path,
    branch: str,
) -> int | None:
    """Resume the Manager-owned rebase only after its intent is durable."""

    if (
        not int(project["pr_conflict_recovery"])
        or project["phase"] != "WORKING"
        or project["last_error"] != REBASE_IN_PROGRESS_ERROR
        or _rebase_in_progress_for_branch(worktree, branch)
    ):
        return None
    plan = active_execplan(connection, repo)
    target_base = str(project["pr_conflict_base_oid"] or "")
    observed_head = str(project["pr_conflict_head_oid"] or "")
    if (
        plan is None
        or not int(plan["pr_conflict_recovery"])
        or not target_base
        or not observed_head
    ):
        error = "PR 충돌 복구 rebase marker 또는 관측 OID가 유효하지 않습니다."
        block_active_execplan(
            connection,
            repo,
            worktree,
            error,
            str(project["verify_result"] or ""),
        )
        operation_print(error, file=sys.stderr)
        return 1

    latest_base = git(
        repo_path,
        "rev-parse",
        f"refs/remotes/origin/{project['base_branch']}^{{commit}}",
    ).stdout.strip()
    if latest_base != target_base:
        target_base = latest_base
        update_project(
            connection,
            repo,
            pr_conflict_base_oid=target_base,
            validated_base_sha=None,
            verify_result=None,
            review_result=None,
            last_error=REBASE_IN_PROGRESS_ERROR,
        )

    ancestry = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        "HEAD",
        check=False,
    )
    if ancestry.returncode == 0:
        update_project(
            connection,
            repo,
            active_sha=target_base,
            phase="VERIFYING",
            last_error=None,
        )
        return 0
    if ancestry.returncode != 1:
        error = (
            "PR 충돌 복구 base ancestry 확인 실패:\n"
            + ancestry.stdout[-8000:]
        )
        block_active_execplan(
            connection,
            repo,
            worktree,
            error,
            str(project["verify_result"] or ""),
        )
        operation_print(error, file=sys.stderr)
        return 1

    rebased = git(worktree, "rebase", target_base, check=False)
    if rebased.returncode and is_git_lock_failure(rebased.stdout):
        raise GitLockError(rebased.stdout[-8000:])
    if rebased.returncode == 0:
        update_project(
            connection,
            repo,
            active_sha=target_base,
            phase="VERIFYING",
            last_error=None,
        )
        return 0
    if _rebase_in_progress_for_branch(worktree, branch):
        update_project(
            connection,
            repo,
            phase="WORKING",
            validated_base_sha=None,
            last_error=REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:],
        )
        return 0

    error = f"PR 충돌 복구 rebase 실패:\n{rebased.stdout[-8000:]}"
    block_active_execplan(
        connection,
        repo,
        worktree,
        error,
        str(project["verify_result"] or ""),
    )
    operation_print(error, file=sys.stderr)
    return 1

def planner_prompt(error: str | None, compact_catalog: str = "[]") -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    return f"""
당신은 Loop Manager의 Planner다.
AGENTS.md, 활성 PRODUCT와 Requirement, docs/ARCHITECTURE.md, docs/PLANS.md 및 현재 코드·테스트를 읽어라.
아래 compact catalog는 SQLite의 비종료 ExecPlan만 코드로 직렬화한 것이다. MERGED 계획은 제외됐다.
{compact_catalog}

현재 입력에서 식별 가능한 미충족 작업 전체를 독립적으로 검증·Merge 가능한 ExecPlan들로 나누어 한 번에 작성하라.
catalog가 이미 충족하는 작업이나 코드·테스트에 이미 구현된 작업은 중복 계획하지 마라.
catalog에서 현재 입력과 겹치는 비종료 계획만 필요할 때 전문을 읽고, 기존 docs/plans 전체를 기본 입력으로 읽지 마라.
새 ExecPlan마다 priority를 기록하고, 기존 ExecPlan을 수정·삭제하지 말며 docs/plans 바로 아래에 새 파일만 작성하라.
현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어는 같은 계획 주기의 ExecPlan 범위나 미충족 작업의 근거로 사용하지 말고 docs/FUTURE_IDEAS.md에만 분리해 기록하라.
docs/FUTURE_IDEAS.md의 목록 항목과 같은 내용을 새 ExecPlan의 제목이나 본문에 반복하지 마라.
docs/FUTURE_IDEAS.md는 새 ExecPlan과 함께 생성·수정할 수 있지만, 그 밖의 기존 파일은 수정하지 마라.
구현, 테스트, commit, push와 PR 생성은 하지 마라. 추가 계획이 필요 없으면 파일을 변경하지 마라.
{failure}
""".strip()


def worker_prompt(
    plan_path: str,
    error: str | None,
    previous_result: str | None = None,
    validation_feedback: str | None = None,
    *,
    archive_candidates: tuple[str, ...] = (),
    session_recovery: bool = False,
    guidance_common_allowed: bool = False,
    guidance_context: str = "",
    allow_common_plans: bool = False,
) -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    saved_feedback = (
        "\nSQLite에 보존된 전체 verify 또는 Review 실패 피드백:\n"
        f"{validation_feedback[-6000:]}\n"
        if validation_feedback
        else ""
    )
    if archive_candidates:
        archive_instruction = (
            "\n다음 Requirement만 내용 변경 없이 같은 파일명으로 "
            "docs/requirements/archive/에 이동하라:\n"
            f"{json.dumps(archive_candidates, ensure_ascii=False)}\n"
        )
    else:
        archive_instruction = (
            "\n계산된 Requirement archive 후보가 없다. "
            "docs/requirements/ 아래 파일을 수정하거나 이동하지 마라.\n"
        )
    recovery = ""
    if session_recovery:
        recovery = (
            "\n저장된 Worker 세션을 재개할 수 없어 새 세션에서 이어간다. "
            "현재 worktree와 아래 이전 Worker 결과 및 실패 내용을 기준으로 계속하라:\n"
            f"{(previous_result or '이전 Worker 결과 없음')[-6000:]}\n"
        )
    common_scope = (
        "이 저장소의 루트 AGENTS.md는 공통 원본이므로 Scope는 반드시 `common`이어야 한다."
        if guidance_common_allowed
        else "이 저장소에서는 Scope `project`만 허용되며 `common` 제안은 범위 위반이다."
    )
    plans_scope = (
        "게시된 ExecPlan이 공통 docs/PLANS.md 변경을 명시했으므로 그 파일 변경도 허용된다."
        if allow_common_plans
        else "docs/PLANS.md는 이번 Worker 변경 범위가 아니다."
    )
    if guidance_context:
        guidance_instruction = (
            "\nSQLite에 사용자 결정과 결합되어 승인된 guidance가 있다. "
            "아래 approve/replace patch만 해당 Target에 적용하고 reject 대상은 원래 baseline으로 유지하라. "
            "이전 불일치가 남아 있으면 expected_content_hash가 되도록 복원하라. "
            "User Decision은 수정하지 말며, 최종 대상 내용은 expected_content_hash와 일치해야 한다:\n"
            f"{guidance_context}\n"
        )
    else:
        guidance_instruction = f"""
현재 ExecPlan 달성에 꼭 필요한 AGENTS.md 규칙 변경을 발견하면 AGENTS.md를 직접 수정하지 마라.
대신 {plan_path}의 ## Decision Log 끝에 아래 형식을 추가하고 Worker 실행을 끝내 사용자 결정을 기다려라.
proposal ID는 이 ExecPlan 안에서 고유한 소문자·숫자·하이픈 값이어야 하고 Target은 저장소 루트의 `AGENTS.md`만 허용된다.
{common_scope}
Worker는 User Decision을 반드시 pending/빈 Revision/빈 Patch로 작성해야 하며 기존 제안·결정을 수정하거나 삭제하지 마라.

### Agent Guidance Proposal: guidance-example
Target: `AGENTS.md`
Scope: `{"common" if guidance_common_allowed else "project"}`
Reason: 현재 ExecPlan에 이 규칙이 필요한 구체적인 이유
Impact: 적용하지 않으면 현재 구현에 생기는 구체적인 영향
Patch:
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1,1 +1,2 @@
 기존 줄
+추가할 정확한 줄
```

### Agent Guidance User Decision: guidance-example
Decision: `pending`
Revision: ``
Patch:
```diff
```
"""

    return f"""
당신은 Loop Manager의 Worker다.
AGENTS.md와 {plan_path}를 읽고 해당 ExecPlan 하나만 구현하라.
관련 targeted test를 작성하되 commit, push, PR 생성과 scripts/verify 실행은 하지 마라.
docs/plans에 새 ExecPlan을 만들지 말고 불필요한 후속 기능도 구현하지 마라.
{plans_scope}
{guidance_instruction}
구현과 관련 unit test를 작성하거나 갱신한 뒤, 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 targeted unit test만 실행하라. 개별 test case, test class, test module 또는 이에 준하는 좁은 단위만 허용된다.
targeted unit test가 실패하면 같은 세션에서 원인을 수정하고 같은 targeted test를 다시 실행하라. 문서·설정 전용 변경이거나 실행 가능한 관련 unit test가 없으면 테스트 범위를 확대하지 말고, 실행하지 못한 구체적인 사유를 최종 결과에 남겨라.
실패 이력을 보고할 때는 실패한 명령만 적은 `FAILED_COMMAND:` 줄과 수정 후 동일 명령만 적은 `RERUN:` 줄을 각각 두고, 재실행의 실패가 없는 최종 결과를 그 뒤에 명시하라. 두 명령은 공백을 정규화한 뒤 정확히 같아야 하며 하나의 실행에서 성공·실패가 섞인 요약은 통과로 보고하지 마라.
전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check 및 scripts/verify 실행은 절대 실행하지 마라. 전체 verify는 Worker 종료 뒤 Loop Manager가 실행하며, 실패한 경우에만 이 세션으로 진단을 전달한다.
작업이 완료되면 최종 응답 마지막에 아래 형식을 정확히 포함하라. 제목과 Summary는 최종 diff 전체를 설명해야 하며 ExecPlan 제목이나 일반적인 변경 문구를 쓰지 마라:
COMMIT_TITLE: 한 줄의 구체적인 변경 제목
PR_SUMMARY: 실제 변경을 간결히 설명하는 Summary
TARGETED_TESTS:
실행한 최소 범위 targeted unit test와 결과, 또는 실행하지 못한 구체적인 사유
{archive_instruction}
{recovery}
{saved_feedback}
{failure}
""".strip()



_TARGETED_TEST_FAILURE = re.compile(
    r"(?i)(?<![\w])(?:FAIL(?:ED|URES?)?|ERRORS?|실패)(?![\w])"
)
_TARGETED_TEST_EXCEPTION_FAILURE = re.compile(
    r"(?m)(?<![\w])[A-Z][A-Za-z0-9_]*(?:Error|Exception)"
    r"(?=[ \t]*(?::|발생|occurred\b|(?:was[ \t]+)?raised\b|$))"
)
_TARGETED_TEST_SUCCESS = re.compile(
    r"(?i)(?<![\w])(?:PASS(?:ED)?|SUCCESS|OK|성공|통과)(?![\w])"
)
_TARGETED_TEST_COMMAND = re.compile(
    r"(?mi)^[ \t]*(?:[-*][ \t]+)?"
    r"(?P<label>FAILED_COMMAND|RERUN):[ \t]*(?P<command>\S.*?)?[ \t]*$"
)


def _targeted_test_failure_matches(evidence: str) -> list[re.Match[str]]:
    failures = []
    command_spans = [
        (match.start(), match.end())
        for match in _TARGETED_TEST_COMMAND.finditer(evidence)
    ]
    candidates = sorted(
        (
            *_TARGETED_TEST_FAILURE.finditer(evidence),
            *_TARGETED_TEST_EXCEPTION_FAILURE.finditer(evidence),
        ),
        key=lambda match: (match.start(), match.end()),
    )
    for match in candidates:
        if any(start <= match.start() < end for start, end in command_spans):
            continue
        prefix = evidence[max(0, match.start() - 32):match.start()]
        count = re.search(
            r"(?i)(?<![\w])(\d+)(?:\s+(?:tests?|cases?))?\s*$",
            prefix,
        )
        if count is not None and int(count.group(1)) == 0:
            continue
        if re.search(r"(?i)\b(?:no|without)\s+$", prefix):
            continue
        failures.append(match)
    return failures


def _has_unresolved_targeted_test_failure(evidence: str) -> bool:
    commands = list(_TARGETED_TEST_COMMAND.finditer(evidence))
    failures = _targeted_test_failure_matches(evidence)
    if not failures:
        return bool(commands)
    recovery_rerun = None

    for index, failure in enumerate(failures):
        prior_commands = [
            match
            for match in commands
            if match.end() <= failure.start()
        ]
        if not prior_commands:
            return True
        failed_run = prior_commands[-1]
        if (
            index == 0
            and failed_run.group("label").upper() != "FAILED_COMMAND"
        ):
            return True
        failed_command = " ".join(
            (failed_run.group("command") or "").split()
        )
        if not failed_command:
            return True

        subsequent_commands = [
            match
            for match in commands
            if match.start() >= failure.end()
        ]
        if not subsequent_commands:
            return True
        rerun = subsequent_commands[0]
        rerun_command = " ".join((rerun.group("command") or "").split())
        if (
            rerun.group("label").upper() != "RERUN"
            or rerun_command != failed_command
        ):
            return True
        recovery_rerun = rerun

    if recovery_rerun is None:
        return True
    if commands[-1].start() != recovery_rerun.start():
        return True
    return (
        _TARGETED_TEST_SUCCESS.search(evidence, recovery_rerun.end())
        is None
    )


def parse_worker_result(message: str, plan: dict[str, object]) -> WorkerPublication:
    """Parse and validate the Worker final-result contract before publishing."""
    match = re.search(
        r"(?ms)^COMMIT_TITLE:[ \t]*(?P<title>[^\r\n]*)\r?\nPR_SUMMARY:[ \t]*(?P<summary>.*?)\r?\nTARGETED_TESTS:\s*(?P<tests>.+)\Z",
        message.strip(),
    )
    if not match:
        title_line = re.search(r"(?m)^COMMIT_TITLE:.*$", message)
        if title_line and not re.match(r"\r?\nPR_SUMMARY:", message[title_line.end():]):
            raise WorkerResultError("Worker commit 제목은 한 줄이어야 합니다.")
        raise WorkerResultError(
            "Worker 결과에는 COMMIT_TITLE, PR_SUMMARY, TARGETED_TESTS 형식이 모두 필요합니다."
        )

    title = match.group("title").strip()
    summary = match.group("summary").strip()
    test_evidence = match.group("tests").strip()
    if not title:
        raise WorkerResultError("Worker commit 제목이 비어 있습니다.")
    if not summary:
        raise WorkerResultError("Worker PR Summary가 비어 있습니다.")
    if not test_evidence:
        raise WorkerResultError("Worker targeted unit test 증거가 비어 있습니다.")
    if _has_unresolved_targeted_test_failure(test_evidence):
        raise WorkerResultError(
            "Worker가 해결되지 않은 targeted unit test 실패를 보고했습니다."
        )

    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    normalized = " ".join(title.split()).casefold().rstrip(" .,:;!?…")
    generic_title_words = {
        "implement", "implementation", "implementations", "change", "changes", "code",
        "update", "updates", "fix", "fixes", "the", "work", "feature", "features",
        "변경", "사항", "구현",
    }
    prohibited = {
        " ".join(plan_title.split()).casefold().rstrip(" .,:;!?…"),
        f"implement {plan_id}".casefold(),
        f"implement {plan_id}: {plan_title}".casefold(),
        f"implement {plan_title}".casefold(),
    }
    if (
        (bool(normalized.split()) and set(normalized.split()) <= generic_title_words)
        or normalized in prohibited
        or re.match(rf"implement\s+(?:execplan\s+)?{re.escape(plan_id.casefold())}\b", normalized)
    ):
        raise WorkerResultError(
            "Worker commit 제목은 ExecPlan 제목이나 일반적인 변경 문구가 아니라 실제 변경을 설명해야 합니다."
        )
    return WorkerPublication(title, summary, test_evidence)

def immutable_github_blob_url(origin_url: str, commit_sha: str, relative_path: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
        raise RuntimeError("계획 commit SHA가 유효하지 않습니다.")
    repository = github_repository(origin_url)
    encoded_path = quote(relative_path, safe="/")
    return f"https://github.com/{repository}/blob/{commit_sha}/{encoded_path}"


def execplan_purpose(plan_text: str) -> str:
    body = re.sub(r"(?s)\A---.*?---\s*", "", plan_text, count=1)
    match = re.search(r"(?ms)^## Purpose\s*\n+(.*?)(?=^## |\Z)", body)
    if not match or not match.group(1).strip():
        raise RuntimeError("ExecPlan Purpose를 찾을 수 없습니다.")
    return " ".join(match.group(1).split())


def published_execplan(plan_path: str, plan_text: str) -> dict[str, object]:
    """Read metadata from the immutable ExecPlan commit without live source checks."""
    plan = parse_frontmatter(plan_text)
    required = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    if set(plan) not in {frozenset(required), frozenset(required | {"priority"})}:
        raise RuntimeError("게시된 ExecPlan frontmatter가 올바르지 않습니다.")
    if "priority" not in plan:
        plan["priority"] = "normal"
    if plan["priority"] not in PRIORITY_ORDER:
        raise RuntimeError("게시된 ExecPlan priority가 유효하지 않습니다.")
    plan_id = plan["id"]
    title = plan["title"]
    if not isinstance(plan_id, str) or not isinstance(title, str):
        raise RuntimeError("게시된 ExecPlan ID 또는 제목이 유효하지 않습니다.")
    if Path(plan_path).name != f"[{plan_id}] {title}.md":
        raise RuntimeError("게시된 ExecPlan 경로가 메타데이터와 일치하지 않습니다.")
    sources = plan["sources"]
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("게시된 ExecPlan source가 하나 이상 필요합니다.")
    for source in sources:
        if not isinstance(source, str) or not (
            source == "docs/PRODUCT.md"
            or re.fullmatch(r"docs/requirements/[^/\\]+\.md", source)
        ):
            raise RuntimeError(f"게시된 ExecPlan source가 유효하지 않습니다: {source}")
    dependencies = plan["depends_on"]
    if not isinstance(dependencies, list):
        raise RuntimeError("게시된 ExecPlan depends_on이 유효하지 않습니다.")
    if any(
        not isinstance(dependency, str) or not re.fullmatch(r"G-\d{3,}", dependency)
        for dependency in dependencies
    ):
        raise RuntimeError("게시된 ExecPlan depends_on ID가 유효하지 않습니다.")
    if plan["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("게시된 ExecPlan complexity가 유효하지 않습니다.")
    if plan["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("게시된 ExecPlan risk가 유효하지 않습니다.")
    return plan


def _requirement_archive_tree(
    repo_path: Path,
    revision: str,
) -> dict[bytes, tuple[bytes, bytes]]:
    try:
        return _git_tree_entries(repo_path, revision)
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision의 Requirement/ExecPlan 이력을 확인할 수 없습니다: "
            f"{revision} ({error})"
        ) from error


def _stored_requirement_sources(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    sources: set[str] = set()
    for row in rows:
        plan_id = str(row["plan_id"])
        try:
            stored_sources = json.loads(str(row["sources"]))
        except json.JSONDecodeError as error:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 SQLite source를 해석할 수 없습니다."
            ) from error
        if not isinstance(stored_sources, list) or any(
            not isinstance(source, str) for source in stored_sources
        ):
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 SQLite source를 해석할 수 없습니다."
            )
        for source in stored_sources:
            if source == "docs/PRODUCT.md":
                continue
            if re.fullmatch(r"docs/requirements/[^/\\]+\.md", source) is None:
                raise RequirementArchiveError(
                    f"ExecPlan {plan_id}의 SQLite source가 유효하지 않습니다: {source}"
                )
            sources.add(source)
    return tuple(sorted(sources))


def requirement_archive_sources(
    connection: sqlite3.Connection,
    repo: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT plan_id, sources FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    return _stored_requirement_sources(rows)


def _later_published_execplan(
    repo_path: Path,
    revision: str,
    row: sqlite3.Row,
) -> dict[str, object] | None:
    plan_id = str(row["plan_id"])
    plan_sha = str(row["plan_sha"] or "")
    if not plan_sha:
        return None
    ancestry = git(
        repo_path,
        "merge-base",
        "--is-ancestor",
        revision,
        plan_sha,
        check=False,
    )
    if ancestry.returncode == 1:
        return None
    if ancestry.returncode != 0:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: "
            f"{ancestry.stdout[-2000:]}"
        )
    path = str(row["path"])
    try:
        plan_text = git_bytes(
            repo_path,
            "show",
            f"{plan_sha}:{path}",
        ).decode("utf-8")
        plan = published_execplan(path, plan_text)
    except (UnicodeDecodeError, RuntimeError) as error:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: {error}"
        ) from error
    if plan["id"] != plan_id:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: ID 불일치"
        )
    return plan


def _revision_execplan_catalog(
    repo_path: Path,
    revision: str,
) -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    tree_entries = sorted(_requirement_archive_tree(repo_path, revision).items())
    for path, (object_type, object_id) in tree_entries:
        if re.fullmatch(rb"docs/plans/[^/]+\.md", path) is None:
            continue
        if object_type != b"blob":
            raise RequirementArchiveError(
                f"ExecPlan source 관계를 확정할 수 없습니다: {path.decode('utf-8', errors='replace')}"
            )
        try:
            relative_path = path.decode("utf-8")
            plan_text = git_bytes(
                repo_path,
                "cat-file",
                "blob",
                object_id.decode("ascii"),
            ).decode("utf-8")
            plan = published_execplan(relative_path, plan_text)
        except (UnicodeDecodeError, RuntimeError) as error:
            raise RequirementArchiveError(
                f"ExecPlan source 관계를 확정할 수 없습니다: "
                f"{path.decode('utf-8', errors='replace')} ({error})"
            ) from error
        plan_id = str(plan["id"])
        if plan_id in catalog:
            raise RequirementArchiveError(
                f"ExecPlan source 관계를 확정할 수 없습니다: 중복 ID {plan_id}"
            )
        catalog[plan_id] = plan
    try:
        _validate_dependency_graph(catalog)
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"ExecPlan source 관계를 확정할 수 없습니다: {error}"
        ) from error
    return catalog


def requirement_archive_candidates(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    current_plan_id: str,
) -> tuple[str, ...]:
    """Return active Requirements safe to move in the current implementation PR."""

    rows = connection.execute(
        "SELECT plan_id, path, sources, status, plan_sha "
        "FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    states = {str(row["plan_id"]): row for row in rows}
    if current_plan_id not in states:
        raise RequirementArchiveError(
            f"현재 ExecPlan의 SQLite 상태 없음: {current_plan_id}"
        )
    for plan_id, row in states.items():
        if str(row["status"]) not in KNOWN_EXECPLAN_STATUSES:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )
    if not _stored_requirement_sources(rows):
        return ()

    catalog = _revision_execplan_catalog(repo_path, revision)
    state_ids = set(states)
    for plan_id in sorted(state_ids - set(catalog)):
        later_plan = _later_published_execplan(
            repo_path,
            revision,
            states[plan_id],
        )
        if later_plan is not None:
            catalog[plan_id] = later_plan
    try:
        _validate_dependency_graph(catalog)
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"ExecPlan source 관계를 확정할 수 없습니다: {error}"
        ) from error

    catalog_ids = set(catalog)
    if catalog_ids != state_ids:
        missing_states = sorted(catalog_ids - state_ids)
        missing_plans = sorted(state_ids - catalog_ids)
        details = []
        if missing_states:
            details.append("SQLite 상태 없음: " + ", ".join(missing_states))
        if missing_plans:
            details.append("Git ExecPlan 없음: " + ", ".join(missing_plans))
        raise RequirementArchiveError(
            "ExecPlan source 관계 또는 상태를 확정할 수 없습니다 ("
            + "; ".join(details)
            + ")."
        )

    for plan_id, plan in catalog.items():
        row = states[plan_id]
        try:
            stored_sources = json.loads(str(row["sources"]))
        except json.JSONDecodeError as error:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 SQLite source를 해석할 수 없습니다."
            ) from error
        if (
            str(row["path"]) != f"docs/plans/[{plan_id}] {plan['title']}.md"
            or stored_sources != plan["sources"]
        ):
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )

    tree = _requirement_archive_tree(repo_path, revision)
    candidates: list[str] = []
    for path in sorted(tree):
        if not _is_active_requirement(path):
            continue
        source = path.decode("utf-8")
        related = [
            plan_id
            for plan_id, plan in catalog.items()
            if source in plan["sources"]
        ]
        if not related:
            continue
        if any(
            plan_id != current_plan_id
            and str(states[plan_id]["status"]) != "MERGED"
            for plan_id in related
        ):
            continue
        archived = requirement_archive_path(source).encode("utf-8")
        if archived in tree:
            raise RequirementArchiveError(
                f"Requirement archive 대상이 이미 존재합니다: "
                f"{requirement_archive_path(source)}"
            )
        candidates.append(source)
    return tuple(candidates)

def requirement_archive_touched_sources(
    repo_path: Path,
    revision: str,
    worktree: Path,
) -> tuple[str, ...]:
    """Find active Requirement paths whose move pair differs from the base tree."""

    tree = _requirement_archive_tree(repo_path, revision)
    touched = []
    for path in sorted(tree):
        if not _is_active_requirement(path):
            continue
        source = path.decode("utf-8")
        archived = requirement_archive_path(source)
        if archived.encode("utf-8") in tree:
            continue
        if not (worktree / source).is_file() or (worktree / archived).exists():
            touched.append(source)
    return tuple(touched)


def _requirement_archive_blob(
    repo: Path,
    revision: str,
    archived: str,
) -> bytes:
    try:
        output = git_bytes(
            repo,
            "ls-tree",
            "-z",
            revision,
            "--",
            archived,
        )
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Requirement archive의 Git tree 상태를 확인할 수 없습니다: "
            f"{archived} ({error})"
        ) from error
    records = [record for record in output.split(b"\0") if record]
    expected_path = archived.encode("utf-8")
    if len(records) == 1:
        metadata, separator, actual_path = records[0].partition(b"\t")
        parts = metadata.split()
    else:
        separator = b""
        actual_path = b""
        parts = []
    if (
        not separator
        or actual_path != expected_path
        or len(parts) != 3
        or parts[0] not in {b"100644", b"100755"}
        or parts[1] != b"blob"
    ):
        raise RequirementArchiveError(
            f"Requirement archive가 {revision}에 일반 blob으로 추적되지 않았습니다: "
            f"{archived}"
        )
    return parts[2]


def requirement_archive_move_set(
    repo_path: Path,
    revision: str,
    worktree: Path,
    *,
    tracked_revision: str | None = None,
) -> tuple[str, ...]:
    """Return content-preserving Requirement moves relative to one base revision."""

    tree = _requirement_archive_tree(repo_path, revision)
    moved: list[str] = []
    for path, (object_type, object_id) in sorted(tree.items()):
        if not _is_active_requirement(path):
            continue
        source = path.decode("utf-8")
        archived = requirement_archive_path(source)
        source_path = worktree / source
        archived_path = worktree / archived
        archived_in_base = archived.encode("utf-8") in tree
        if source_path.is_file():
            if not archived_in_base and archived_path.exists():
                raise RequirementArchiveError(
                    f"Requirement archive 대상이 이미 존재합니다: {archived}"
                )
            continue
        if archived_in_base:
            raise RequirementArchiveError(
                f"활성 Requirement와 기존 archive의 관계를 확정할 수 없습니다: {source}"
            )
        try:
            archived_mode = archived_path.lstat().st_mode
        except FileNotFoundError as error:
            raise RequirementArchiveError(
                f"Requirement 원본과 archive 대상이 모두 없습니다: {source}"
            ) from error
        if not stat.S_ISREG(archived_mode):
            raise RequirementArchiveError(
                f"Requirement archive 대상이 일반 파일이 아닙니다: {archived}"
            )
        if object_type != b"blob":
            raise RequirementArchiveError(
                f"Requirement 원본 내용을 확인할 수 없습니다: {source}"
            )
        original = git_bytes(
            repo_path,
            "cat-file",
            "blob",
            object_id.decode("ascii"),
        )
        if archived_path.read_bytes() != original:
            raise RequirementArchiveError(
                f"Requirement archive 이동 중 내용이 변경됐습니다: {source}"
            )
        if tracked_revision is None:
            ignored = git(
                worktree,
                "check-ignore",
                "--quiet",
                "--",
                archived,
                check=False,
            )
            if ignored.returncode == 0:
                raise RequirementArchiveError(
                    f"Requirement archive 대상이 Git ignore 규칙으로 제외됩니다: "
                    f"{archived}"
                )
            if ignored.returncode != 1:
                raise RequirementArchiveError(
                    f"Requirement archive 대상의 Git 추적 가능 여부를 확인할 수 "
                    f"없습니다: {archived} ({ignored.stdout[-2000:]})"
                )
        else:
            archived_object_id = _requirement_archive_blob(
                worktree,
                tracked_revision,
                archived,
            )
            tracked_contents = git_bytes(
                worktree,
                "cat-file",
                "blob",
                archived_object_id.decode("ascii"),
            )
            if tracked_contents != original:
                raise RequirementArchiveError(
                    f"Requirement archive의 게시 내용이 원본과 다릅니다: {source}"
                )
        moved.append(source)
    return tuple(moved)

def requirement_archive_reconciliation_error(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> str | None:
    if expected == actual:
        return None
    return (
        "Requirement archive 이동 집합이 최신 eligibility와 다릅니다.\n"
        f"기대: {json.dumps(expected, ensure_ascii=False)}\n"
        f"현재: {json.dumps(actual, ensure_ascii=False)}\n"
        "기대 집합만 내용 변경 없이 docs/requirements/archive/로 이동하라."
    )



def build_pr_body(
    worker: WorkerPublication,
    plan: dict[str, object],
    plan_path: str,
    plan_text: str,
    origin_url: str,
    plan_sha: str,
) -> str:
    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    plan_link = immutable_github_blob_url(origin_url, plan_sha, plan_path)
    sources = plan["sources"]
    assert isinstance(sources, list)
    source_lines = [
        f"- [{source}]({immutable_github_blob_url(origin_url, plan_sha, str(source))})"
        for source in sources
    ]
    return "\n".join(
        [
            "## Summary",
            worker.summary,
            "",
            "## ExecPlan",
            f"- [{plan_id}: {plan_title}]({plan_link})",
            f"- Purpose: {execplan_purpose(plan_text)}",
            "",
            "## Sources",
            *source_lines,
        ]
    )

def reviewer_prompt(
    base: str,
    worker_result: str | None = None,
    verify_result: str | None = None,
    review_result: str | None = None,
    *,
    session_recovery: bool = False,
) -> str:
    worker_evidence = (worker_result or "Worker가 targeted unit test 결과 또는 미실행 사유를 보고하지 않았습니다.")[-6000:]
    verify_evidence = (verify_result or "Loop Manager 전체 verify가 성공했습니다(출력 없음).")[-6000:]
    recovery = ""
    if session_recovery:
        recovery = (
            "\n저장된 Reviewer 세션을 재개할 수 없어 새 세션에서 이어간다. "
            "아래 이전 Review 결과를 포함해 현재 diff를 다시 검토하라:\n"
            f"{(review_result or '이전 Review 결과 없음')[-6000:]}\n"
        )
    return f"""
origin/{base}와 현재 HEAD의 차이를 읽기 전용으로 검토하라.
AGENTS.md, 근거 PRODUCT/Requirement, ARCHITECTURE와 ExecPlan을 기준으로 correctness, 회귀, 테스트 누락, verify 약화를 확인하라.
Worker가 보고한 targeted unit test 결과 또는 미실행 사유:
{worker_evidence}

Loop Manager가 실행한 전체 verify 결과:
{verify_evidence}
{recovery}
테스트 command와 scripts/verify를 실행하지 말고, 코드·테스트·verify 스크립트의 diff 및 위 결과만으로 판단하라.
blocking issue마다 설명 직전에 `ISSUE_ID: relative/path::stable-symbol-or-test::category-slug`를 한 줄로 기록하라.
ISSUE_ID에는 줄 번호나 자연어 설명을 넣지 말고, 같은 issue가 재발하면 위치·표현이 바뀌어도 이전과 정확히 같은 ID를 사용하라.
`VERDICT: FAIL`은 하나 이상의 ISSUE_ID가 있어야 하며, 서로 다른 issue는 서로 다른 ID를 사용하라.
blocking issue가 있으면 간결히 설명하고 마지막 줄을 `VERDICT: FAIL`로 끝내라.
blocking issue가 없으면 마지막 줄을 `VERDICT: PASS`로 끝내라. 파일은 수정하지 마라.
""".strip()


def _run_project_step(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
) -> int:
    # Resolve the persisted project key without consulting repository config.
    # An interrupted Agent may have changed origin or hooksPath, so the saved
    # snapshot must be checked before normal repository validation reads it.
    repo_path = Path(repo_arg).expanduser().resolve()
    repo = str(repo_path)
    project_registry.ensure_registry_schema(connection)
    _ensure_execplans_schema(connection)
    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if row is not None and int(row["registered"]) != 1:
        raise RuntimeError(f"등록되지 않은 프로젝트입니다: {repo}")
    if row is None and registered_only:
        raise RuntimeError(f"등록되지 않은 프로젝트입니다: {repo}")
    provider = str(row["provider"]) if row else (requested_provider or "codex")
    if provider not in AGENT_PROVIDERS:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    if (
        row is not None
        and requested_provider is not None
        and requested_provider != provider
    ):
        raise RuntimeError(
            f"등록된 provider는 {provider}이며 {requested_provider}로 변경할 수 없습니다."
        )
    _ensure_cleanup_pending_schema(connection)
    cleanup_candidates = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in connection.execute(
            "SELECT worktree FROM cleanup_pending "
            "WHERE repo = ? AND workspace_owned = ?",
            (repo, WORKSPACE_OWNED),
        ).fetchall()
    )
    checkpoint_cleanup_candidates = cleanup_candidates
    if row and row["agent_recovery"]:
        try:
            resume_interrupted_blocked_agent_recovery(
                connection,
                repo_path,
                state_dir,
                repo,
            )
        except (AgentScopeError, RuntimeError) as error:
            recovery_error = f"BLOCKED Agent 중단 복구 실패:\n{error}"
            plan = active_execplan(connection, repo)
            if plan is not None:
                _update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    recovery_error,
                )
            else:
                update_project(connection, repo, last_error=recovery_error)
            operation_print(recovery_error, file=sys.stderr)
            return 1
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?",
            (repo,),
        ).fetchone()
    validated_agent_scope: GitSnapshot | None = None
    if row and row["agent_snapshot"]:
        planner_correction = bool(
            row["phase"] == "PLANNING" and row["planning_validation_error"]
        )
        try:
            validated_agent_scope = validate_persisted_agent_scope(
                connection,
                repo_path,
                state_dir,
                repo,
                allow_planner_worktree_correction=planner_correction,
                manager_cleanup_worktrees=cleanup_candidates,
            )
        except AgentScopeError as error:
            detail = str(error)
            scope_result_snapshot = error.result_snapshot
        except RuntimeError:
            detail = "저장된 Agent Git 상태를 안전하게 읽을 수 없습니다."
            scope_result_snapshot = None
        else:
            detail = None
            scope_result_snapshot = None
        if detail is not None:
            message = block_persisted_agent_scope_violation(
                connection,
                repo_path,
                repo,
                row,
                detail,
                result_snapshot=scope_result_snapshot,
            )
            operation_print(message, file=sys.stderr)
            return 1
        if validated_agent_scope is not None:
            if row["agent_manager_snapshot"]:
                checkpoint_cleanup_candidates |= ManagerGitCheckpoint.deserialize(
                    str(row["agent_manager_snapshot"])
                ).cleanup_worktrees
            update_project(
                connection,
                repo,
                agent_manager_snapshot=ManagerGitCheckpoint(
                    validated_agent_scope,
                    checkpoint_cleanup_candidates,
                ).serialize(),
            )
    validated_origins: list[str] = []
    if row is not None:
        repo_path = validate_repository(
            repo_path,
            allow_agents_mismatch=False,
            provider=provider,
            config_path=config_path,
            registration_checks=False,
            expected_origin=(str(row["origin"]) if row["origin"] else None),
            origin_result=validated_origins,
        )
        current_origin = (
            str(row["origin"])
            if row["origin"]
            else (validated_origins[0] if validated_origins else None)
        )
        if row["origin"] is None and current_origin is not None:
            with connection:
                connection.execute(
                    "UPDATE projects SET origin = ? WHERE repo = ?",
                    (current_origin, repo),
                )
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
    else:
        repo_path = validate_repository(
            repo_path,
            allow_agents_mismatch=allow_agents_mismatch,
            provider=provider,
            config_path=config_path,
            origin_result=validated_origins,
        )
        current_origin = validated_origins[0] if validated_origins else None

    base = remote_base(
        repo_path,
        requested_base or (row["base_branch"] if row else None),
    )
    base_sha = git(
        repo_path,
        "rev-parse",
        f"refs/remotes/origin/{base}^{{commit}}",
    ).stdout.strip()
    if row is not None and (
        row["phase"] == "PR_OPEN" or int(row["pr_conflict_recovery"])
    ):
        sync_result = BaseWorktreeSyncResult(
            "skipped",
            "PR 충돌 감지/복구 중 source checkout 보존",
            base_sha,
        )
    else:
        sync_result = ensure_safe_base_worktree(
            repo_path,
            base,
            remote_sha=base_sha,
        )
    if isinstance(sync_result, BaseWorktreeSyncResult):
        log_base_worktree_sync(repo_path, sync_result)

    backfill_plan_shas(connection, target_repo=repo)
    migrate_legacy_execplans(
        connection,
        reconcile_history=True,
        target_repo=repo,
    )
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row:
        hydrate_active_project(connection, repo)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    _ensure_cleanup_pending_schema(connection)
    cleanup_candidates = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in connection.execute(
            "SELECT worktree FROM cleanup_pending "
            "WHERE repo = ? AND workspace_owned = ?",
            (repo, WORKSPACE_OWNED),
        ).fetchall()
    )
    cleanup_pending = recheck_cleanup_pending(
        connection, repo_path, repo, state_dir
    )
    remaining_cleanup = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in cleanup_pending
    )
    manager_worktree_changes = checkpoint_cleanup_candidates - remaining_cleanup
    for pending in cleanup_pending:
        operation_print(
            f"보존된 worktree: {pending['worktree']} ({pending['reason']})",
            file=sys.stderr,
        )

    if row and row["branch"]:
        existing_pr = pr_for_branch(repo_path, row["branch"], row["pr_url"])
        if existing_pr:
            try:
                reconciliation = reconcile_existing_pull_request(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    row,
                    existing_pr,
                )
                if reconciliation is not None:
                    return reconciliation
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (repo,)
                ).fetchone()
            except PublicationSafetyError as error:
                publication_error = f"기존 PR 복구 거부: {error}"
                block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{row['base_branch']}",
                    publication_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(publication_error, file=sys.stderr)
                return 1

    if row and row["phase"] == "BLOCKED":
        blocked_plan = active_execplan(connection, repo)
        resume_blocked = (
            resume_guidance_approval_if_decided
            if blocked_plan and blocked_plan["blocked_reason"] == GUIDANCE_BLOCK_REASON
            else resume_blocked_execplan_if_inputs_changed
        )
        if resume_blocked(
            connection,
            repo_path,
            repo,
            str(row["base_branch"]),
            state_dir,
        ):
            validated_agent_scope = None
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            operation_print(
                f"작업이 차단되어 있습니다: {row['last_error'] or '사용자 조치가 필요합니다.'}",
                file=sys.stderr,
            )
            return 1

    if validated_agent_scope is not None:
        try:
            validated_agent_scope = refresh_persisted_agent_scope_after_manager_git(
                connection,
                repo,
                validated_agent_scope,
                manager_worktree_changes,
            )
        except (AgentScopeError, RuntimeError) as error:
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            message = block_persisted_agent_scope_violation(
                connection,
                repo_path,
                repo,
                row,
                str(error),
            )
            operation_print(message, file=sys.stderr)
            return 1

    ready_activated_before_planning = False
    catalog_decision = catalog_schedule_decision(connection, repo)
    if row is not None and not row["phase"] and catalog_decision.has_ready:
        if catalog_decision.runnable_ready is None:
            operation_print(
                "READY ExecPlan이 남아 있지만 dependency가 충족되지 않아 대기합니다."
            )
            return 0
        git(repo_path, "fetch", "origin")
        base_sha = git(
            repo_path,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base}^{{commit}}",
        ).stdout.strip()
        activate_ready_execplan(connection, repo, state_dir, base_sha)
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        ready_activated_before_planning = True

    current_fingerprint = (
        _planning_input_fingerprint(repo_path, base_sha)
        if row is None or not row["phase"] or row["phase"] == "PLANNING"
        else None
    )

    # A fixed planning error must remain reportable even when the user's base
    # worktree later becomes unsafe. The fingerprint reads the selected SHA,
    # so this check neither reads nor alters the user's local changes.
    if row and row["planning_error"] and row["planning_error_fingerprint"] == current_fingerprint:
        operation_print(
            f"계획 오류가 해결되지 않았습니다: {row['planning_error']}\n"
            f"보존된 planning worktree: {row['worktree']}",
            file=sys.stderr,
        )
        return 1

    if not row:
        connection.execute(
            "INSERT INTO projects (repo, base_branch, provider, origin) "
            "VALUES (?, ?, ?, ?)",
            (repo, base, provider, current_origin),
        )
        connection.commit()
        migrate_legacy_execplans(
            connection,
            reconcile_history=True,
            target_repo=repo,
        )
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    elif not row["phase"]:
        update_project(connection, repo, base_branch=base)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if row["planning_error"]:
        stale_worktree = Path(row["worktree"]) if row["worktree"] else None
        stored_fingerprint = str(
            row["planning_attempt_fingerprint"]
            or row["planning_error_fingerprint"]
            or ""
        )
        try:
            if stale_worktree is None:
                raise WorkspaceOwnershipError(
                    "planning worktree 경로가 저장되지 않았습니다."
                )
            validate_planning_worktree_ownership(
                connection,
                repo_path,
                state_dir,
                repo,
                stored_fingerprint,
                stale_worktree,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            cleanup_error = (
                f"planning workspace cleanup 소유권 검증 실패: {error}"
            )
            update_project(
                connection,
                repo,
                planning_error=cleanup_error,
                planning_error_fingerprint=current_fingerprint,
                last_error=cleanup_error,
            )
            operation_print(cleanup_error, file=sys.stderr)
            return 1
        preserve_planning_diagnostics(state_dir, repo, row)
        if stale_worktree.exists():
            git(repo_path, "worktree", "remove", "--force", str(stale_worktree))
        clear_active(connection, repo)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if row["phase"] == "PLANNING":
        started_fingerprint = row["planning_attempt_fingerprint"]
        if not started_fingerprint:
            started_fingerprint = _planning_input_fingerprint(repo_path, row["active_sha"])
        if started_fingerprint != current_fingerprint:
            stale_worktree = Path(row["worktree"]) if row["worktree"] else None
            try:
                if stale_worktree is None:
                    raise WorkspaceOwnershipError(
                        "planning worktree 경로가 저장되지 않았습니다."
                    )
                validate_planning_worktree_ownership(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    str(started_fingerprint),
                    stale_worktree,
                )
            except (RuntimeError, WorkspaceOwnershipError) as error:
                cleanup_error = (
                    f"planning workspace cleanup 소유권 검증 실패: {error}"
                )
                update_project(
                    connection,
                    repo,
                    planning_error=cleanup_error,
                    planning_error_fingerprint=current_fingerprint,
                    last_error=cleanup_error,
                )
                operation_print(cleanup_error, file=sys.stderr)
                return 1
            if row["planning_validation_error"]:
                preserve_planning_diagnostics(state_dir, repo, row)
            if stale_worktree.exists():
                git(repo_path, "worktree", "remove", "--force", str(stale_worktree))
            clear_active(connection, repo)
            row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    planning_required = not row["phase"]

    if not row["phase"] and row["planning_fingerprint"] == current_fingerprint:
        update_project(connection, repo, processed_sha=base_sha)
        operation_print("계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다.")
        planning_required = False

    if planning_required and row["planning_fingerprint"] and row["processed_sha"]:
        processed_fingerprint = _planning_input_fingerprint(repo_path, row["processed_sha"])
        if processed_fingerprint == current_fingerprint:
            update_project(connection, repo, processed_sha=base_sha)
            operation_print("계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다.")
            planning_required = False
        elif _is_requirement_archive_only_change(repo_path, row["processed_sha"], base_sha):
            update_project(connection, repo, processed_sha=base_sha)
            operation_print("Requirement archive 이동 외의 계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다.")
            planning_required = False

    if planning_required:
        assert current_fingerprint is not None
        project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
        worktree = state_dir / "worktrees" / project_id / "planning" / current_fingerprint
        update_project(
            connection,
            repo,
            active_sha=base_sha,
            plan_sha=None,
            planning_attempt_fingerprint=current_fingerprint,
            planning_workspace_owned=0,
            planning_retry_count=0,
            planner_session_id=None,
            worker_session_id=None,
            worker_result=None,
            verify_result=None,
            planning_validation_error=None,
            planning_result_pending_validation=0,
            planning_result_completed=0,
            planning_error=None,
            planning_error_fingerprint=None,
            phase="PLANNING",
            branch=None,
            worktree=str(worktree),
            plan_path=None,
            pr_url=None,
            last_error=None,
        )
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    if row["phase"] == "PLANNING":
        planning_worktree = Path(row["worktree"])
        try:
            validate_planning_workspace_identity(
                state_dir,
                repo,
                str(row["planning_attempt_fingerprint"]),
                planning_worktree,
            )
            planning_started = bool(
                row["planner_session_id"]
                or row["agent_snapshot"]
                or row["planning_result_pending_validation"]
                or row["planning_result_completed"]
                or row["planning_validation_error"]
                or row["planning_retry_count"]
                or row["last_error"]
            )
            planning_workspace_owned = (
                row["planning_workspace_owned"] == WORKSPACE_OWNED
            )
            planning_workspace_reserved = (
                row["planning_workspace_owned"] == WORKSPACE_RESERVED
            )
            make_planning_worktree(
                repo_path,
                planning_worktree,
                base,
                allow_create=(
                    not planning_started
                    and (
                        not planning_workspace_owned
                        or planning_workspace_reserved
                    )
                ),
                allow_existing=(
                    planning_workspace_owned or planning_workspace_reserved
                ),
                ownership_reserved=planning_workspace_reserved,
                expected_head=str(row["active_sha"] or ""),
                before_create=lambda: update_project(
                    connection,
                    repo,
                    planning_workspace_owned=WORKSPACE_RESERVED,
                ),
            )
            update_project(
                connection,
                repo,
                planning_workspace_owned=WORKSPACE_OWNED,
            )
        except WorkspaceOwnershipError as error:
            validation_error = f"planning workspace 소유권 검증 실패: {error}"
            update_project(
                connection,
                repo,
                planning_error=validation_error,
                planning_error_fingerprint=current_fingerprint,
                last_error=validation_error,
            )
            operation_print(validation_error, file=sys.stderr)
            return 1
        head = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
        dirty_files = changed_files(planning_worktree)
        if row["planning_validation_error"] and head != row["active_sha"]:
            # The Planner is not allowed to commit. Turn an invalid Planner
            # commit back into a worktree change so the persisted validation
            # error can be repaired in the same session on the next run.
            git(planning_worktree, "reset", "--mixed", str(row["active_sha"]))
            head = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
            dirty_files = changed_files(planning_worktree)
        planner_result_in_flight = bool(
            row["planning_result_pending_validation"]
            and not row["planning_result_completed"]
        )
        planner_scope_error: PlannerValidationError | None = None
        if planner_result_in_flight or (
            head == row["active_sha"]
            and (
                not dirty_files
                or row["planning_validation_error"]
                or row["last_error"]
            )
            and not row["planning_result_pending_validation"]
            and not (
                row["planning_validation_error"]
                and row["planning_retry_count"] >= MAX_PLANNER_VALIDATION_RETRIES
            )
        ):
            operation_log("Planner 시작: 다음 ExecPlan 계획을 생성합니다.")
            # A Planner can modify the worktree before its CLI invocation
            # returns. Persist this boundary first so an interrupted process
            # resumes that same session before validating its partial output.
            # A completed result is the only pending result that may be
            # validated after an interruption.
            if not planner_result_in_flight:
                update_project(
                    connection,
                    repo,
                    last_error=None,
                    planning_result_pending_validation=1,
                    planning_result_completed=0,
                )
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (repo,)
                ).fetchone()
            planner_scope_before = begin_agent_scope(
                connection,
                repo,
                planning_worktree,
                "planner",
            )
            planner_guard = planner_mutex or contextlib.nullcontext()
            with planner_guard:
                result = run_agent_with_retries(
                    lambda current_session_id, _session_recovery: run_agent(
                        provider,
                        planning_worktree,
                        state_dir,
                        config_path,
                        "planner",
                        planner_prompt(
                            row["planning_validation_error"] or row["last_error"],
                            compact_execplan_catalog(connection, repo),
                        ),
                        read_only=False,
                        persist_session=True,
                        session_id=current_session_id,
                        on_session_started=lambda started_session_id: update_project(
                            connection,
                            repo,
                            planner_session_id=started_session_id,
                        ),
                    ),
                    session_id=row["planner_session_id"],
                )
            code, message = result
            result_session_id = getattr(result, "session_id", None)
            if result_session_id:
                update_project(
                    connection,
                    repo,
                    planner_session_id=result_session_id,
                )
            planner_scope_after: GitSnapshot | None = None
            try:
                planner_scope_after = capture_git_snapshot(planning_worktree)
                validate_agent_scope(
                    "planner",
                    planning_worktree,
                    planner_scope_before,
                    planner_scope_after,
                    external_worktree=repo_path,
                    base_branch=base,
                )
                validate_managed_worktree(repo_path, planning_worktree, None)
            except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
                planner_scope_error = PlannerValidationError(str(error))
                if planner_scope_after is not None:
                    update_project(
                        connection,
                        repo,
                        agent_result_snapshot=planner_scope_after.serialize(),
                    )
            else:
                clear_agent_scope(connection, repo)

            if code:
                agent_error = message or f"Planner 종료 코드 {code}"
                if result.result_kind is AgentResultKind.TRANSIENT_FAILURE:
                    agent_error = f"{TRANSIENT_AGENT_ERROR_PREFIX} {agent_error}"
                    step_result = PROJECT_STEP_TRANSIENT_PAUSE
                elif result.result_kind is AgentResultKind.USAGE_LIMIT:
                    agent_error = f"{USAGE_LIMIT_ERROR_PREFIX} {agent_error}"
                    step_result = PROJECT_STEP_USAGE_PAUSE
                else:
                    step_result = 1
                update_project(
                    connection,
                    repo,
                    last_error=agent_error,
                    planning_result_pending_validation=0,
                    planning_result_completed=0,
                )
                operation_print(agent_error, file=sys.stderr)
                return step_result
            update_project(
                connection,
                repo,
                last_error=None,
                planning_result_completed=1,
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            dirty_files = changed_files(planning_worktree)

        if planner_scope_error is None:
            scope_row = connection.execute(
                "SELECT agent_role, agent_snapshot FROM projects WHERE repo = ?",
                (repo,),
            ).fetchone()
            if scope_row["agent_snapshot"]:
                persisted_after: GitSnapshot | None = None
                try:
                    if scope_row["agent_role"] != "planner":
                        raise AgentScopeError(
                            "저장된 Agent 역할이 Planner와 다릅니다."
                        )
                    persisted_before = GitSnapshot.deserialize(
                        str(scope_row["agent_snapshot"])
                    )
                    persisted_after = capture_git_snapshot(planning_worktree)
                    validate_agent_scope(
                        "planner",
                        planning_worktree,
                        persisted_before,
                        persisted_after,
                        external_worktree=repo_path,
                        base_branch=base,
                    )
                    validate_managed_worktree(repo_path, planning_worktree, None)
                except (
                    AgentScopeError,
                    WorkspaceOwnershipError,
                    RuntimeError,
                ) as error:
                    planner_scope_error = PlannerValidationError(str(error))
                    if persisted_after is not None:
                        update_project(
                            connection,
                            repo,
                            agent_result_snapshot=persisted_after.serialize(),
                        )
                else:
                    clear_agent_scope(connection, repo)


        try:
            if planner_scope_error is not None:
                raise planner_scope_error
            validated_result = validate_planner_result(
                planning_worktree, str(row["active_sha"]), dirty_files
            )
        except PlannerValidationError as error:
            validation_error = str(error)
            retry_count = row["planning_retry_count"]
            if row["planning_result_pending_validation"] and row["planning_validation_error"]:
                retry_count += 1
            values = {
                "planning_validation_error": validation_error,
                "planning_result_pending_validation": 0,
                "planning_result_completed": 0,
                "last_error": None,
                "planning_retry_count": retry_count,
            }
            if retry_count >= MAX_PLANNER_VALIDATION_RETRIES:
                values.update(
                    planning_error=validation_error,
                    planning_error_fingerprint=current_fingerprint,
                )
            update_project(connection, repo, **values)
            operation_print(str(error), file=sys.stderr)
            return 1

        if validated_result is None:
            completed_fingerprint = _planning_input_fingerprint(repo_path, row["active_sha"])
            validate_planning_worktree_ownership(
                connection,
                repo_path,
                state_dir,
                repo,
                current_fingerprint,
                planning_worktree,
            )
            git(repo_path, "worktree", "remove", str(planning_worktree))
            clear_active(
                connection,
                repo,
                processed_sha=row["active_sha"],
                planning_fingerprint=completed_fingerprint,
            )
            operation_print("추가로 계획할 작업이 없습니다.")
            row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        else:
            plans = list(validated_result.plans)
            if dirty_files:
                # Publishing failures are operational failures, not Planner
                # validation failures. Leave the validated result for retry.
                git(
                    planning_worktree,
                    "add",
                    "--",
                    *validated_result.publication_paths,
                )
                if not plans:
                    message = "Update future ideas from planning"
                else:
                    first = plans[0][1]
                    last = plans[-1][1]
                    if len(plans) == 1:
                        message = f"Add ExecPlan {first['id']}: {first['title']}"
                    else:
                        message = f"Add ExecPlans {first['id']}-{last['id']}"
                git(planning_worktree, "commit", "-m", message)

            clear_planning_validation_state(connection, repo)
            plan_commit = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
            git(repo_path, "fetch", "origin")
            latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
            if latest_sha not in {row["active_sha"], plan_commit}:
                published = git(
                    planning_worktree,
                    "merge-base",
                    "--is-ancestor",
                    plan_commit,
                    f"origin/{base}",
                    check=False,
                ).returncode == 0
                if not published:
                    validate_planning_worktree_ownership(
                        connection,
                        repo_path,
                        state_dir,
                        repo,
                        current_fingerprint,
                        planning_worktree,
                    )
                    git(repo_path, "worktree", "remove", str(planning_worktree))
                    clear_active(connection, repo)
                    operation_print(
                        "계획 중 base가 변경되어 최신 기준으로 다시 계획합니다.",
                        file=sys.stderr,
                    )
                    return 1
            if latest_sha != plan_commit and latest_sha == row["active_sha"]:
                pushed = git(
                    planning_worktree,
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{base}",
                    check=False,
                )
                if pushed.returncode:
                    update_project(
                        connection,
                        repo,
                        last_error=f"ExecPlan push 실패:\n{pushed.stdout[-8000:]}",
                    )
                    operation_print(pushed.stdout, file=sys.stderr)
                    return 1
                git(repo_path, "fetch", "origin")
                latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
                if latest_sha != plan_commit:
                    update_project(
                        connection,
                        repo,
                        last_error="ExecPlan push 결과를 origin/base에서 확인할 수 없습니다.",
                    )
                    operation_print("ExecPlan push 결과 확인에 실패했습니다.", file=sys.stderr)
                    return 1

            validate_planning_worktree_ownership(
                connection,
                repo_path,
                state_dir,
                repo,
                current_fingerprint,
                planning_worktree,
            )
            register_execplans(
                connection,
                repo,
                planning_worktree,
                plans,
                plan_commit,
                current_fingerprint,
            )
            git(repo_path, "worktree", "remove", str(planning_worktree))
            base_sha = latest_sha
            row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    newly_activated = ready_activated_before_planning
    if not row["phase"]:
        git(repo_path, "fetch", "origin")
        latest_selection_sha = git(
            repo_path,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base}^{{commit}}",
        ).stdout.strip()
        catalog_decision = catalog_schedule_decision(connection, repo)
        if not catalog_decision.has_ready:
            latest_selection_fingerprint = _planning_input_fingerprint(
                repo_path, latest_selection_sha
            )
            if latest_selection_fingerprint != current_fingerprint:
                operation_print(
                    "계획 중 입력이 변경되어 최신 기준으로 다시 계획합니다.",
                    file=sys.stderr,
                )
                return 1
        elif catalog_decision.runnable_ready is None:
            operation_print(
                "READY ExecPlan이 남아 있지만 dependency가 충족되지 않아 대기합니다."
            )
            return 0
        base_sha = latest_selection_sha
        selected = activate_ready_execplan(connection, repo, state_dir, base_sha)
        if selected is None:
            operation_print("실행 가능한 READY ExecPlan이 없습니다.")
            return 0
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        newly_activated = True
    branch = str(row["branch"] or "")
    worktree = Path(str(row["worktree"] or ""))
    plan_id = _plan_id_from_path(str(row["plan_path"] or ""))
    catalog_plan = active_execplan(connection, repo)
    workspace_ownership = int(catalog_plan["workspace_owned"] if catalog_plan else 0)
    workspace_owned = workspace_ownership == WORKSPACE_OWNED
    workspace_reserved = workspace_ownership == WORKSPACE_RESERVED
    rebase_error = str(row["last_error"] or "")
    rebase_recovery = bool(
        row["phase"] == "WORKING"
        and (
            rebase_error == REBASE_IN_PROGRESS_ERROR
            or rebase_error.startswith(REBASE_CONFLICT_PREFIX)
        )
    )
    resolving_rebase = bool(
        rebase_recovery
        and worktree.is_dir()
        and _rebase_in_progress_for_branch(worktree, branch)
    )
    try:
        if plan_id is None:
            raise WorkspaceOwnershipError(
                "활성 ExecPlan 경로에서 ID를 확인할 수 없습니다."
            )
        if catalog_plan is None or catalog_plan["plan_id"] != plan_id:
            raise WorkspaceOwnershipError(
                "활성 ExecPlan의 catalog 상태를 확인할 수 없습니다."
            )
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            plan_id,
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            allow_create_branch=(
                newly_activated
                and not workspace_owned
                and not workspace_reserved
            ),
            allow_recovery=workspace_owned or workspace_reserved,
            allow_existing=workspace_owned or workspace_reserved,
            ownership_reserved=workspace_reserved,
            expected_head=str(row["active_sha"] or ""),
            before_create=lambda: reserve_execplan_workspace(
                connection,
                repo,
                plan_id,
            ),
            allow_rebase=resolving_rebase,
        )
        mark_execplan_workspace_owned(connection, repo, plan_id)
    except (RuntimeError, WorkspaceOwnershipError) as error:
        workspace_error = f"관리 workspace 소유권/복구 검증 실패: {error}"
        block_active_execplan_from_revision(
            connection,
            repo_path,
            repo,
            f"refs/remotes/origin/{base}",
            workspace_error,
            str(row["verify_result"] or ""),
        )
        operation_print(workspace_error, file=sys.stderr)
        return 1

    if int(row["pr_conflict_recovery"]):
        recovery_rebase = resume_pr_conflict_rebase(
            connection,
            repo_path,
            repo,
            row,
            worktree,
            branch,
        )
        if recovery_rebase == 1:
            return 1
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        rebase_error = str(row["last_error"] or "")
        rebase_recovery = bool(
            row["phase"] == "WORKING"
            and (
                rebase_error == REBASE_IN_PROGRESS_ERROR
                or rebase_error.startswith(REBASE_CONFLICT_PREFIX)
            )
        )
        resolving_rebase = bool(
            rebase_recovery
            and _rebase_in_progress_for_branch(worktree, branch)
        )

    if rebase_recovery and not resolving_rebase and not int(row["pr_conflict_recovery"]):
        update_project(
            connection, repo, phase="VERIFYING", last_error=None
        )
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
    assert catalog_plan is not None
    try:
        plan = validate_active_execplan_metadata(
            repo_path,
            worktree,
            str(row["plan_path"]),
            catalog_plan,
        )
        level = ensure_worker_level_state(connection, repo, plan)
    except RuntimeError as error:
        metadata_error = str(error)
        block_active_execplan(
            connection,
            repo,
            worktree,
            metadata_error,
            str(row["verify_result"] or ""),
        )
        operation_print(metadata_error, file=sys.stderr)
        return 1
    try:
        archive_sources = requirement_archive_sources(connection, repo)
    except RequirementArchiveError as error:
        archive_error = f"Requirement archive source 확인 실패: {error}"
        block_active_execplan(
            connection,
            repo,
            worktree,
            archive_error,
            str(row["verify_result"] or ""),
        )
        operation_print(archive_error, file=sys.stderr)
        return 1
    archive_candidates: tuple[str, ...] = ()
    archive_scope_sources: tuple[str, ...] = ()
    if row["phase"] == "WORKING" and archive_sources:
        archive_revision = str(row["active_sha"] or "")
        try:
            archive_candidates = requirement_archive_candidates(
                connection,
                repo_path,
                repo,
                archive_revision,
                str(plan["id"]),
            )
            archive_scope_sources = tuple(
                sorted(
                    set(archive_candidates)
                    | set(
                        requirement_archive_touched_sources(
                            repo_path,
                            archive_revision,
                            worktree,
                        )
                    )
                )
            )
        except RequirementArchiveError as error:
            archive_error = f"Requirement archive eligibility 확인 실패: {error}"
            block_active_execplan(
                connection,
                repo,
                worktree,
                archive_error,
                str(row["verify_result"] or ""),
            )
            operation_print(archive_error, file=sys.stderr)
            return 1

    if row["phase"] == "WORKING":
        operation_log(f"Worker 시작: {plan['id']} {plan['title']}")
        worker_scope_before = begin_agent_scope(
            connection,
            repo,
            worktree,
            "worker",
            input_fingerprint=_worktree_execplan_input_fingerprint(
                worktree, catalog_plan
            ),
        )
        persisted_validation_feedback = []
        if _deserialize_failure_history(
            catalog_plan["verify_failure_history"]
        ):
            persisted_validation_feedback.append(str(row["verify_result"] or ""))
        if _deserialize_failure_history(
            catalog_plan["review_failure_history"]
        ):
            persisted_validation_feedback.append(str(row["review_result"] or ""))
        validation_feedback = "\n\n".join(
            feedback for feedback in persisted_validation_feedback if feedback
        )
        guidance_targets = _guidance_worker_targets(
            connection, repo, str(catalog_plan["plan_id"])
        )
        guidance_context = guidance_resume_context(
            connection, repo, str(catalog_plan["plan_id"])
        )
        allow_common_plans = _published_plan_allows_common_plans(
            repo_path, catalog_plan
        )
        result = run_agent_with_retries(
            lambda current_session_id, session_recovery: run_agent(
                provider,
                worktree,
                state_dir,
                config_path,
                "worker",
                worker_prompt(
                    row["plan_path"],
                    row["last_error"],
                    row["worker_result"],
                    validation_feedback,
                    archive_candidates=archive_candidates,
                    session_recovery=session_recovery,
                    guidance_common_allowed=is_loop_manager_repository(repo_path),
                    guidance_context=guidance_context,
                    allow_common_plans=allow_common_plans,
                ),
                read_only=False,
                level=level,
                persist_session=True,
                session_id=current_session_id,
                on_session_started=lambda started_session_id: update_project(
                    connection,
                    repo,
                    worker_session_id=started_session_id,
                ),
            ),
            session_id=row["worker_session_id"],
        )
        code, message = result
        result_session_id = getattr(result, "session_id", None)
        if result_session_id:
            update_project(connection, repo, worker_session_id=result_session_id)

        try:
            plan = validate_worker_workspace_scope(
                repo_path,
                worktree,
                branch,
                base,
                str(row["plan_path"]),
                catalog_plan,
                worker_scope_before,
                allow_rebase=resolving_rebase,
                allowed_requirement_archives=archive_scope_sources,
                allowed_guidance_targets=guidance_targets,
                allow_common_plans=allow_common_plans,
            )
            guidance_blocked = inspect_worker_guidance_result(
                connection,
                repo_path,
                repo,
                worktree,
                catalog_plan,
                message,
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                scope_error,
                str(row["verify_result"] or ""),
            )
            operation_print(scope_error, file=sys.stderr)
            return 1
        if guidance_blocked:
            blocked = active_execplan(connection, repo)
            operation_print(str(blocked["last_error"]), file=sys.stderr)
            return 1
        clear_agent_scope(connection, repo)

        if code:
            worker_error = message or f"Agent 종료 코드 {code}"
            result_kind = result.result_kind
            if result_kind is AgentResultKind.TRANSIENT_FAILURE:
                worker_error = f"{TRANSIENT_AGENT_ERROR_PREFIX} {worker_error}"
                step_result = PROJECT_STEP_TRANSIENT_PAUSE
            elif result_kind is AgentResultKind.USAGE_LIMIT:
                worker_error = f"{USAGE_LIMIT_ERROR_PREFIX} {worker_error}"
                step_result = PROJECT_STEP_USAGE_PAUSE
            elif resolving_rebase:
                worker_error = REBASE_CONFLICT_PREFIX + worker_error
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    worker_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(worker_error, file=sys.stderr)
                return 1
            else:
                step_result = 1
            if resolving_rebase:
                worker_error = REBASE_CONFLICT_PREFIX + worker_error
            update_project(
                connection, repo, phase="WORKING", last_error=worker_error
            )
            operation_print(worker_error, file=sys.stderr)
            return step_result
        try:
            parse_worker_result(message, plan)
        except WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            if resolving_rebase:
                validation_error = REBASE_CONFLICT_PREFIX + validation_error
            update_project(
                connection,
                repo,
                phase="WORKING",
                last_error=validation_error,
            )
            operation_print(validation_error, file=sys.stderr)
            return 1
        if resolving_rebase:
            git(worktree, "add", "-A")
            staged = git(
                worktree,
                "diff",
                "--cached",
                "--quiet",
                check=False,
            )
            if staged.returncode not in {0, 1}:
                staging_error = (
                    "rebase 충돌 해결 결과 확인 실패 "
                    f"(git 종료 코드 {staged.returncode})."
                )
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    staging_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(staging_error, file=sys.stderr)
                return 1
            if staged.returncode == 0:
                continued = git(
                    worktree,
                    "rebase",
                    "--skip",
                    check=False,
                )
            else:
                continued = git(
                    worktree,
                    "-c",
                    "core.editor=true",
                    "rebase",
                    "--continue",
                    check=False,
                )
            if continued.returncode:
                continuation_error = (
                    REBASE_CONFLICT_PREFIX + continued.stdout[-8000:]
                )
                if not _rebase_in_progress_for_branch(worktree, branch):
                    block_active_execplan(
                        connection,
                        repo,
                        worktree,
                        continuation_error,
                        str(row["verify_result"] or ""),
                    )
                else:
                    update_project(
                        connection,
                        repo,
                        phase="WORKING",
                        last_error=continuation_error,
                    )
                operation_print(continuation_error, file=sys.stderr)
                return 1
            if not branch_has_changes(worktree, base):
                no_change_error = (
                    "rebase 충돌 해결 후 Worker 변경이 남지 않아 PR을 "
                    "생성할 수 없습니다."
                )
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    no_change_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(no_change_error, file=sys.stderr)
                return 1
            latest_sha = (
                str(row["pr_conflict_base_oid"])
                if int(row["pr_conflict_recovery"])
                else git(
                    worktree, "rev-parse", f"origin/{base}"
                ).stdout.strip()
            )
            update_project(connection, repo, active_sha=latest_sha)
            if validate_guidance_publication(
                connection, repo, worktree, catalog_plan, message,
                previous_base=str(row["active_sha"]), latest_base=latest_sha,
            ):
                return 1
        if archive_sources:
            post_worker_revision = (
                latest_sha if resolving_rebase else str(row["active_sha"] or "")
            )
            try:
                expected_archives = requirement_archive_candidates(
                    connection,
                    repo_path,
                    repo,
                    post_worker_revision,
                    str(plan["id"]),
                )
                actual_archives = requirement_archive_move_set(
                    repo_path,
                    post_worker_revision,
                    worktree,
                )
            except RequirementArchiveError as error:
                archive_error = f"Requirement archive 이동 검증 실패: {error}"
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    archive_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(archive_error, file=sys.stderr)
                return 1
            archive_error = requirement_archive_reconciliation_error(
                expected_archives,
                actual_archives,
            )
            if archive_error is not None:
                update_project(
                    connection,
                    repo,
                    phase="WORKING",
                    validated_base_sha=None,
                    worker_result=message,
                    verify_result=None,
                    review_result=None,
                    last_error=archive_error,
                )
                operation_print(archive_error, file=sys.stderr)
                return PROJECT_STEP_RETRY
        update_project(
            connection,
            repo,
            phase="VERIFYING",
            validated_base_sha=None,
            worker_result=message,
            verify_result=None,
            last_error=None,
        )

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "VERIFYING":
        if validate_guidance_publication(
            connection, repo, worktree, catalog_plan, str(row["worker_result"] or "")
        ):
            return 1
        operation_log(f"전체 verify 시작: {plan['id']} {plan['title']}")
        try:
            worker_publication = parse_worker_result(str(row["worker_result"] or ""), plan)
        except WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            update_project(connection, repo, phase="WORKING", last_error=validation_error)
            operation_print(validation_error, file=sys.stderr)
            return 1

        verify_scope_before = begin_agent_scope(
            connection,
            repo,
            worktree,
            "worker",
            input_fingerprint=_worktree_execplan_input_fingerprint(
                worktree, catalog_plan
            ),
        )
        passed, output = verify(worktree)
        try:
            plan = validate_worker_workspace_scope(
                repo_path,
                worktree,
                branch,
                base,
                str(row["plan_path"]),
                catalog_plan,
                verify_scope_before,
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                scope_error,
                output,
            )
            operation_print(scope_error, file=sys.stderr)
            return 1
        clear_agent_scope(connection, repo)
        if not passed:
            operation_log(f"Worker 반환: {plan['id']} {plan['title']} (전체 verify 실패)", error=True)
            decision = transition_validation_failure(
                connection,
                repo,
                worktree,
                "verify",
                output,
            )
            operation_print(output, file=sys.stderr)
            return 1 if decision.blocked else PROJECT_STEP_RETRY

        # Every successful verify is a progress boundary. Clear its stage-local
        # history before checking for a newer base or running a second verify
        # after rebase, so later failures cannot inherit pre-success repeats.
        clear_validation_failure_history(connection, repo, "verify")

        dirty = bool(git(worktree, "status", "--porcelain").stdout.strip())
        if not dirty and not branch_has_changes(worktree, base):
            no_change_error = (
                "Worker가 변경을 만들지 않아 PR을 생성할 수 없습니다. "
                "ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다."
            )
            block_active_execplan(
                connection,
                repo,
                worktree,
                no_change_error,
                output,
            )
            operation_print(no_change_error, file=sys.stderr)
            return 1
        if dirty:
            git(worktree, "add", "-A")
            plan = validate_active_execplan_metadata(
                repo_path,
                worktree,
                str(row["plan_path"]),
                catalog_plan,
            )
            git(worktree, "commit", "-m", worker_publication.commit_title)

        git(repo_path, "fetch", "origin")
        latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
        if latest_sha != row["active_sha"]:
            update_project(
                connection,
                repo,
                phase="WORKING",
                validated_base_sha=None,
                pr_conflict_base_oid=(
                    latest_sha if int(row["pr_conflict_recovery"])
                    else row["pr_conflict_base_oid"]
                ),
                last_error=REBASE_IN_PROGRESS_ERROR,
            )
            rebased = git(worktree, "rebase", latest_sha, check=False)
            if rebased.returncode:
                if _rebase_in_progress_for_branch(worktree, branch):
                    if validate_guidance_publication(
                        connection, repo, worktree, catalog_plan,
                        str(row["worker_result"] or ""),
                        previous_base=str(row["active_sha"]), latest_base=latest_sha,
                    ):
                        return 1
                    rebase_failure = REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:]
                    update_project(
                        connection,
                        repo,
                        phase="WORKING",
                        validated_base_sha=None,
                        last_error=rebase_failure,
                    )
                else:
                    rebase_failure = f"rebase 실패:\n{rebased.stdout[-8000:]}"
                    block_active_execplan(
                        connection,
                        repo,
                        worktree,
                        rebase_failure,
                        output,
                    )
                operation_print(rebased.stdout, file=sys.stderr)
                return 1
            # Persist the new base now so recovery does not repeat that rebase.
            update_project(connection, repo, active_sha=latest_sha)
            if validate_guidance_publication(
                connection, repo, worktree, catalog_plan,
                str(row["worker_result"] or ""),
                previous_base=str(row["active_sha"]), latest_base=latest_sha,
            ):
                return 1
            operation_log(f"전체 verify 시작: {plan['id']} {plan['title']} (rebase 후)")
            verify_scope_before = begin_agent_scope(
                connection,
                repo,
                worktree,
                "worker",
                input_fingerprint=_worktree_execplan_input_fingerprint(
                    worktree, catalog_plan
                ),
            )
            passed, output = verify(worktree)
            try:
                plan = validate_worker_workspace_scope(
                    repo_path,
                    worktree,
                    branch,
                    base,
                    str(row["plan_path"]),
                    catalog_plan,
                    verify_scope_before,
                )
            except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
                scope_error = str(error)
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    scope_error,
                    output,
                )
                operation_print(scope_error, file=sys.stderr)
                return 1
            clear_agent_scope(connection, repo)
            if not passed:
                operation_log(f"Worker 반환: {plan['id']} {plan['title']} (rebase 후 전체 verify 실패)", error=True)
                decision = transition_validation_failure(
                    connection,
                    repo,
                    worktree,
                    "verify",
                    output,
                )
                operation_print(output, file=sys.stderr)
                return 1 if decision.blocked else PROJECT_STEP_RETRY
            clear_validation_failure_history(connection, repo, "verify")
            if git(worktree, "status", "--porcelain").stdout.strip():
                git(worktree, "add", "-A")
                plan = validate_active_execplan_metadata(
                    repo_path,
                    worktree,
                    str(row["plan_path"]),
                    catalog_plan,
                )
                git(worktree, "commit", "-m", worker_publication.commit_title)
            update_project(
                connection,
                repo,
                active_sha=latest_sha,
                validated_base_sha=latest_sha,
                verify_result=output,
            )
        else:
            update_project(
                connection,
                repo,
                validated_base_sha=str(row["active_sha"] or ""),
                verify_result=output,
            )
        try:
            plan = validate_active_execplan_metadata(
                repo_path,
                worktree,
                str(row["plan_path"]),
                catalog_plan,
            )
        except RuntimeError as error:
            metadata_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                metadata_error,
                output,
            )
            operation_print(metadata_error, file=sys.stderr)
            return 1

        if not branch_has_changes(worktree, base):
            no_change_error = (
                "rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다. "
                "ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다."
            )
            block_active_execplan(
                connection,
                repo,
                worktree,
                no_change_error,
                output,
            )
            operation_print(no_change_error, file=sys.stderr)
            return 1
        update_project(connection, repo, phase="REVIEWING", last_error=None)

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "REVIEWING":
        operation_log(f"Reviewer 시작: {plan['id']} {plan['title']}")
        reviewer_scope_before = begin_agent_scope(
            connection,
            repo,
            worktree,
            "reviewer",
            input_fingerprint=_worktree_execplan_input_fingerprint(
                worktree, catalog_plan
            ),
        )

        review_result = run_agent_with_retries(
            lambda current_session_id, session_recovery: run_agent(
                provider,
                worktree,
                state_dir,
                config_path,
                "reviewer",
                reviewer_prompt(
                    base,
                    row["worker_result"],
                    row["verify_result"],
                    row["review_result"],
                    session_recovery=session_recovery,
                ),
                read_only=True,
                persist_session=True,
                session_id=current_session_id,
                on_session_started=lambda started_session_id: update_project(
                    connection,
                    repo,
                    reviewer_session_id=started_session_id,
                ),
            ),
            session_id=row["reviewer_session_id"],
        )
        code, review = review_result
        if review_result.session_id:
            update_project(
                connection,
                repo,
                reviewer_session_id=review_result.session_id,
            )
        try:
            reviewer_scope_after = capture_git_snapshot(worktree)
            validate_agent_scope(
                "reviewer",
                worktree,
                reviewer_scope_before,
                reviewer_scope_after,
                external_worktree=repo_path,
                base_branch=base,
            )
            validate_managed_worktree(repo_path, worktree, branch)
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                scope_error,
                str(row["verify_result"] or ""),
            )
            operation_print(scope_error, file=sys.stderr)
            return 1
        clear_agent_scope(connection, repo)

        if code:
            reviewer_error = review or f"Reviewer 종료 코드 {code}"
            if review_result.result_kind is AgentResultKind.TRANSIENT_FAILURE:
                reviewer_error = f"{TRANSIENT_AGENT_ERROR_PREFIX} {reviewer_error}"
                step_result = PROJECT_STEP_TRANSIENT_PAUSE
            elif review_result.result_kind is AgentResultKind.USAGE_LIMIT:
                reviewer_error = f"{USAGE_LIMIT_ERROR_PREFIX} {reviewer_error}"
                step_result = PROJECT_STEP_USAGE_PAUSE
            else:
                step_result = 1
            update_project(
                connection,
                repo,
                phase="REVIEWING",
                last_error=reviewer_error,
                review_result=review,
            )
            operation_print(reviewer_error, file=sys.stderr)
            return step_result

        try:
            verdict, _issue_ids = parse_reviewer_result(review)
        except ReviewerProtocolError as error:
            reviewer_error = f"Reviewer 결과 프로토콜 오류: {error}"
            update_project(
                connection,
                repo,
                phase="REVIEWING",
                last_error=reviewer_error,
                review_result=review,
            )
            operation_print(reviewer_error, file=sys.stderr)
            return 1

        if verdict == "FAIL":
            decision = transition_validation_failure(
                connection,
                repo,
                worktree,
                "review",
                review,
            )
            operation_log(f"Worker 반환: {plan['id']} {plan['title']} (Review 실패)", error=True)
            operation_print(review or "Reviewer 결과가 유효하지 않습니다.", file=sys.stderr)
            return 1 if decision.blocked else PROJECT_STEP_RETRY
        clear_validation_failure_history(connection, repo, "review")
        update_project(
            connection,
            repo,
            phase="PUBLISHING",
            last_error=None,
            review_result=review,
        )

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "PUBLISHING":
        existing_pr = pr_for_branch(
            repo_path,
            branch,
            str(row["pr_url"]) if int(row["pr_conflict_recovery"]) else None,
        )
        if existing_pr:
            try:
                reconciliation = reconcile_existing_pull_request(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    row,
                    existing_pr,
                )
                if reconciliation is not None:
                    return reconciliation
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (repo,)
                ).fetchone()
            except PublicationSafetyError as error:
                publication_error = f"기존 PR 복구 거부: {error}"
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    publication_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(publication_error, file=sys.stderr)
                return 1

        git(repo_path, "fetch", "origin")
        latest_sha = git(
            repo_path, "rev-parse", f"origin/{base}"
        ).stdout.strip()
        remote_oid = (
            None
            if int(row["pr_conflict_recovery"])
            else remote_branch_oid(repo_path, branch)
        )
        try:
            if not int(row["pr_conflict_recovery"]):
                record_remote_publication_branch(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    branch,
                    worktree,
                    remote_oid,
                )
        except PublicationSafetyError as error:
            publication_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                publication_error,
                str(row["verify_result"] or ""),
            )
            operation_print(publication_error, file=sys.stderr)
            return 1

        if latest_sha != row["validated_base_sha"]:
            if latest_sha != row["active_sha"]:
                update_project(
                    connection,
                    repo,
                    phase="WORKING",
                    validated_base_sha=None,
                    pr_conflict_base_oid=(
                        latest_sha if int(row["pr_conflict_recovery"])
                        else row["pr_conflict_base_oid"]
                    ),
                    last_error=REBASE_IN_PROGRESS_ERROR,
                )
                rebased = git(
                    worktree, "rebase", latest_sha, check=False
                )
                if rebased.returncode:
                    if _rebase_in_progress_for_branch(worktree, branch):
                        if validate_guidance_publication(
                            connection, repo, worktree, catalog_plan,
                            str(row["worker_result"] or ""),
                            previous_base=str(row["active_sha"]), latest_base=latest_sha,
                        ):
                            return 1
                        rebase_failure = (
                            REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:]
                        )
                        update_project(
                            connection,
                            repo,
                            phase="WORKING",
                            validated_base_sha=None,
                            last_error=rebase_failure,
                        )
                    else:
                        rebase_failure = (
                            f"게시 직전 rebase 실패:\n{rebased.stdout[-8000:]}"
                        )
                        block_active_execplan(
                            connection,
                            repo,
                            worktree,
                            rebase_failure,
                            str(row["verify_result"] or ""),
                        )
                    operation_print(rebase_failure, file=sys.stderr)
                    return 1
                update_project(connection, repo, active_sha=latest_sha)
                if validate_guidance_publication(
                    connection, repo, worktree, catalog_plan,
                    str(row["worker_result"] or ""),
                    previous_base=str(row["active_sha"]), latest_base=latest_sha,
                ):
                    return 1
                if not branch_has_changes(worktree, base):
                    no_change_error = (
                        "게시 직전 rebase 후 Worker 변경이 남지 않아 PR을 "
                        "생성할 수 없습니다."
                    )
                    block_active_execplan(
                        connection,
                        repo,
                        worktree,
                        no_change_error,
                        str(row["verify_result"] or ""),
                    )
                    operation_print(no_change_error, file=sys.stderr)
                    return 1
                update_project(
                    connection,
                    repo,
                    active_sha=latest_sha,
                    validated_base_sha=None,
                    phase="VERIFYING",
                    verify_result=None,
                    review_result=None,
                    last_error=None,
                )
            else:
                update_project(
                    connection,
                    repo,
                    validated_base_sha=None,
                    phase="VERIFYING",
                    verify_result=None,
                    review_result=None,
                    last_error=None,
                )
            operation_print(
                "게시 직전 base가 변경되어 전체 verify와 Review를 다시 수행합니다.",
                file=sys.stderr,
            )
            return PROJECT_STEP_RETRY

        if row["active_sha"] != latest_sha:
            publication_error = (
                "검증 base와 관리 branch의 반영 base가 일치하지 않습니다."
            )
            block_active_execplan(
                connection,
                repo,
                worktree,
                publication_error,
                str(row["verify_result"] or ""),
            )
            operation_print(publication_error, file=sys.stderr)
            return 1
        if archive_sources:
            try:
                final_archive_candidates = requirement_archive_candidates(
                    connection,
                    repo_path,
                    repo,
                    latest_sha,
                    str(plan["id"]),
                )
                final_archive_moves = requirement_archive_move_set(
                    repo_path,
                    latest_sha,
                    worktree,
                    tracked_revision="HEAD",
                )
            except RequirementArchiveError as error:
                archive_error = f"게시 직전 Requirement archive 확인 실패: {error}"
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    archive_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(archive_error, file=sys.stderr)
                return 1
            archive_error = requirement_archive_reconciliation_error(
                final_archive_candidates,
                final_archive_moves,
            )
            if archive_error is not None:
                update_project(
                    connection,
                    repo,
                    phase="WORKING",
                    validated_base_sha=None,
                    verify_result=None,
                    review_result=None,
                    last_error=archive_error,
                )
                operation_print(archive_error, file=sys.stderr)
                return PROJECT_STEP_RETRY
        if validate_guidance_publication(
            connection, repo, worktree, catalog_plan, str(row["worker_result"] or "")
        ):
            return 1
        if not branch_has_changes(worktree, base):
            no_change_error = (
                "게시할 Worker 변경이 남지 않아 PR을 생성할 수 없습니다."
            )
            block_active_execplan(
                connection,
                repo,
                worktree,
                no_change_error,
                str(row["verify_result"] or ""),
            )
            operation_print(no_change_error, file=sys.stderr)
            return 1

        if int(row["pr_conflict_recovery"]):
            current_pr = pr_for_branch(
                repo_path, branch, str(row["pr_url"] or "")
            )
            if current_pr is None:
                publication_error = (
                    "게시 직전 저장된 기존 PR을 다시 조회할 수 없습니다."
                )
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    publication_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(publication_error, file=sys.stderr)
                return 1
            reconciliation = reconcile_existing_pull_request(
                connection,
                repo_path,
                state_dir,
                repo,
                row,
                current_pr,
            )
            if reconciliation is not None:
                return reconciliation
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            try:
                head_oid = push_owned_pr_conflict_recovery(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    branch,
                    worktree,
                    current_pr,
                )
            except PublicationSafetyError as error:
                publication_error = str(error)
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    publication_error,
                    str(row["verify_result"] or ""),
                )
                operation_print(publication_error, file=sys.stderr)
                return 1
            except RuntimeError as error:
                publication_error = str(error)
                update_project(
                    connection,
                    repo,
                    phase="PUBLISHING",
                    last_error=publication_error,
                )
                operation_print(publication_error, file=sys.stderr)
                return 1
            update_project(
                connection,
                repo,
                phase="PR_OPEN",
                publication_remote_oid=head_oid,
                pr_head_oid=head_oid,
                pr_conflict_recovery=0,
                pr_conflict_head_oid=None,
                pr_conflict_base_oid=None,
                last_error=None,
            )
            operation_print(f"기존 PR 충돌 복구 게시 완료: {row['pr_url']}")
            return 0
        plan_sha = str(row["plan_sha"] or "")
        if not plan_sha:
            plan_error = "계획 commit SHA가 없습니다."
            block_active_execplan(
                connection,
                repo,
                worktree,
                plan_error,
                str(row["verify_result"] or ""),
            )
            operation_print(plan_error, file=sys.stderr)
            return 1
        plan_text = git(
            worktree, "show", f"{plan_sha}:{row['plan_path']}"
        ).stdout
        plan = published_execplan(str(row["plan_path"]), plan_text)
        try:
            worker_publication = parse_worker_result(
                str(row["worker_result"] or ""), plan
            )
        except WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            update_project(
                connection,
                repo,
                phase="WORKING",
                validated_base_sha=None,
                last_error=validation_error,
            )
            operation_print(validation_error, file=sys.stderr)
            return 1
        origin_url = git(
            worktree, "remote", "get-url", "origin"
        ).stdout.strip()
        body = build_pr_body(
            worker_publication,
            plan,
            str(row["plan_path"]),
            plan_text,
            origin_url,
            plan_sha,
        )
        try:
            head_oid = push_managed_publication_branch(
                connection,
                repo_path,
                state_dir,
                repo,
                branch,
                worktree,
            )
        except PublicationSafetyError as error:
            publication_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                publication_error,
                str(row["verify_result"] or ""),
            )
            operation_print(publication_error, file=sys.stderr)
            return 1
        except RuntimeError as error:
            publication_error = str(error)
            update_project(
                connection,
                repo,
                phase="PUBLISHING",
                last_error=publication_error,
            )
            operation_print(publication_error, file=sys.stderr)
            return 1

        title = f"Implement {plan['id']}: {plan['title']}"
        created = command(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=worktree,
        )
        url = created.stdout.strip().splitlines()[-1]
        update_project(
            connection,
            repo,
            phase="PR_OPEN",
            pr_url=url,
            pr_head_oid=head_oid,
            last_error=None,
        )
        operation_print(f"PR 생성 완료: {url}")
    return 0


def run_project_result(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
) -> ProjectRunResult:
    """Run retryable validation progress to a stable project-level outcome."""

    while True:
        try:
            step_result = _run_project_step(
                connection,
                state_dir,
                repo_arg,
                config_path,
                requested_base,
                allow_agents_mismatch,
                requested_provider,
                registered_only,
                planner_mutex,
            )
        except GitLockError as error:
            operation_log(
                f"일시적 Git lock 충돌 ({repo_arg}): {error}",
                error=True,
            )
            return ProjectRunResult(
                ProjectRunKind.TRANSIENT_PAUSE,
                0,
                str(error),
            )
        if step_result != PROJECT_STEP_RETRY:
            break

    repo = str(Path(repo_arg).expanduser().resolve())
    row = connection.execute(
        "SELECT phase, last_error FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    detail = str(row["last_error"] or "") if row else None
    if step_result == PROJECT_STEP_TRANSIENT_PAUSE:
        return ProjectRunResult(ProjectRunKind.TRANSIENT_PAUSE, 0, detail)
    if step_result == PROJECT_STEP_USAGE_PAUSE:
        return ProjectRunResult(ProjectRunKind.USAGE_LIMIT_PAUSE, 0, detail)
    if step_result == PROJECT_STEP_PLANNING_PENDING:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    if row is not None and row["phase"] == "BLOCKED":
        return ProjectRunResult(ProjectRunKind.BLOCKED, 1, detail)
    if step_result != 0:
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, detail)
    if row is not None and row["phase"] is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    unfinished = connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' LIMIT 1",
        (repo,),
    ).fetchone()
    if unfinished is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    notifications.enqueue_completion_if_ready(connection, repo)
    return ProjectRunResult(ProjectRunKind.COMPLETED, 0, detail)


def run_project(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
) -> int:
    """CLI-compatible adapter for the reusable structured project result."""

    return run_project_result(
        connection,
        state_dir,
        repo_arg,
        config_path,
        requested_base,
        allow_agents_mismatch,
        requested_provider,
        registered_only=registered_only,
        planner_mutex=planner_mutex,
    ).exit_code


def _run_registered_project(
    project: project_registry.RegisteredProject,
    state_dir: Path,
    config_path: Path,
    notifications_enabled: bool,
    planner_mutex: threading.Lock,
) -> ScheduledProjectResult:
    """Run one registry row with state isolated to this worker connection."""

    try:
        with connect_database(state_dir) as connection:
            notifications.configure_session(
                connection, enabled=notifications_enabled
            )
            result = run_project_result(
                connection,
                state_dir,
                project.repo,
                config_path,
                None,
                False,
                registered_only=True,
                planner_mutex=planner_mutex,
            )
    except (ManagedConfigError, project_registry.ProjectRegistryError) as error:
        result = ProjectRunResult(
            ProjectRunKind.CONFIGURATION_ERROR,
            1,
            str(error),
        )
        operation_log(
            f"프로젝트 설정 실패 ({project.repo}): {error}",
            error=True,
        )
    except Exception as error:
        result = ProjectRunResult(
            ProjectRunKind.OPERATIONAL_ERROR,
            1,
            str(error),
        )
        operation_log(
            f"프로젝트 처리 실패 ({project.repo}): {error}",
            error=True,
        )
    return ScheduledProjectResult(project.repo, result)


def schedule_registered_projects(
    connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
) -> tuple[ScheduledProjectResult, ...]:
    """Run each registered project once with bounded cross-project concurrency."""

    projects = project_registry.list_projects(connection)
    if not projects:
        return ()

    planner_mutex = threading.Lock()
    worker_count = min(config.max_workers, len(projects))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="loop-project",
    ) as pool:
        futures = tuple(
            pool.submit(
                _run_registered_project,
                project,
                state_dir,
                config.path,
                config.telegram_enabled,
                planner_mutex,
            )
            for project in projects
        )
        return tuple(future.result() for future in futures)


def run_registered_projects(
    connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
) -> int:
    """Run all registered projects and aggregate outcomes after every worker exits."""

    try:
        notifications.dispatch_pending(
            connection, config.telegram_token, config.telegram_chat_id
        )
    except Exception as error:
        operation_log(
            f"알림 재시도 실패 ({type(error).__name__})", error=True
        )
    results = schedule_registered_projects(connection, state_dir, config)
    try:
        notifications.dispatch_pending(
            connection, config.telegram_token, config.telegram_chat_id
        )
    except Exception as error:
        operation_log(
            f"알림 재시도 실패 ({type(error).__name__})", error=True
        )
    return 1 if any(item.result.exit_code for item in results) else 0


def configure_registry(
    args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
) -> int:
    if args.config_command == "init":
        load_managed_config(config_path)
        with connect_database(state_dir, create=True):
            pass
        operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0

    with connect_database(state_dir) as connection:
        if args.config_command == "list":
            projects = project_registry.list_projects(connection)
            if not projects:
                operation_print("등록된 프로젝트가 없습니다.")
            for project in projects:
                approval = "approved" if project.agents_approved else "common"
                operation_print(
                    f"{project.repo}\t{project.base_branch}\t"
                    f"{project.provider}\tAGENTS:{approval}"
                )
            return 0
        if args.config_command == "add":
            config = load_managed_config(config_path)
            project = add_registered_project(
                connection,
                Path(args.repository),
                config_path=config.path,
                provider=args.provider,
                requested_base=args.base,
                approve_agents_mismatch=args.approve_agents_mismatch,
            )
            approval = " (AGENTS.md 불일치 승인 저장)" if project.agents_approved else ""
            operation_print(
                f"프로젝트 등록 완료: {project.repo} "
                f"({project.base_branch}, {project.provider}){approval}"
            )
            return 0
        repo = str(Path(args.repository).expanduser().resolve())
        project_registry.remove_project(connection, repo)
        operation_print(f"프로젝트 등록 해제 완료: {repo}")
        return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="기본 운영체제 상태 디렉터리 대신 사용할 경로")
    parser.add_argument("--config", default=str(ROOT / "config/config.ini"), help="config.ini 경로")
    parser.add_argument(
        "--runtime-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--runtime-source", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-sha", help=argparse.SUPPRESS)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help=argparse.SUPPRESS)

    configuration = subcommands.add_parser(
        "config", help="상태 DB와 프로젝트 registry를 명시적으로 관리"
    )
    config_commands = configuration.add_subparsers(
        dest="config_command", required=True
    )
    config_commands.add_parser("init", help="상태 DB 초기화")
    config_commands.add_parser("list", help="등록 프로젝트 목록 조회")
    add = config_commands.add_parser("add", help="프로젝트 검증 및 등록")
    add.add_argument("repository", help="대상 Git 저장소 루트")
    add.add_argument("--base", help="기준 branch. 생략하면 origin/HEAD 사용")
    add.add_argument(
        "--provider",
        choices=sorted(AGENT_PROVIDERS),
        default="codex",
        help="프로젝트 Agent provider (기본값: codex)",
    )
    add.add_argument(
        "--approve-agents-mismatch",
        action="store_true",
        help="현재 AGENTS.md 해시에 결합해 공통 규칙 불일치를 승인",
    )
    remove = config_commands.add_parser("remove", help="프로젝트 등록 해제")
    remove.add_argument("repository", help="등록 해제할 저장소 루트")

    run = subcommands.add_parser("run", help="등록 프로젝트의 다음 작업을 처리")
    run.add_argument(
        "--skip-if-running",
        action="store_true",
        help="다른 Loop Manager가 실행 중이면 스케줄러용으로 조용히 성공 종료",
    )
    return parser


def execute_runtime_command(
    args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
) -> int:
    if args.command == "init":
        with connect_database(state_dir, create=True):
            pass
        operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0
    if args.command == "config":
        return configure_registry(args, state_dir, config_path)
    config = load_managed_config(config_path)
    with connect_database(state_dir) as connection:
        return run_registered_projects(connection, state_dir, config)


def main() -> int:
    raw_arguments = sys.argv[1:]
    args = build_parser().parse_args(raw_arguments)
    state_dir = state_directory(args.state_dir)
    config_path = source_config_path(ROOT, args.config)
    try:
        if args.runtime_child:
            if not args.runtime_source or not args.runtime_sha:
                raise RuntimeError("runtime 자식 소유권 정보가 없습니다.")
            source = Path(args.runtime_source).expanduser().resolve()
            validate_runtime_child(source, state_dir, str(args.runtime_sha))
            return execute_runtime_command(args, state_dir, config_path)

        source = ROOT
        with process_lock(state_dir):
            runtime = prepare_runtime_worktree(source, state_dir)
            return run_runtime_child(
                source,
                state_dir,
                config_path,
                runtime,
                raw_arguments,
            )
    except ProcessLockBusyError as error:
        if args.command == "run" and getattr(args, "skip_if_running", False):
            return 0
        operation_log(f"오류: {error}", error=True)
        return 1
    except (
        ManagedConfigError,
        project_registry.ProjectRegistryError,
        RuntimeError,
        OSError,
        sqlite3.DatabaseError,
        json.JSONDecodeError,
    ) as error:
        operation_log(f"오류: {error}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
