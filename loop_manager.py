#!/usr/bin/env python3
"""AI Agent가 Loop Manager의 다음 PR을 만들게 하는 부트스트랩 실행기."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
import configparser
import contextlib
from contextvars import ContextVar
import difflib
import errno
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
from execution_runtime import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionOutcomeKind,
    ExecutionPhase,
    ExecutionPhaseCoordinator,
    ExecutionPlanMetadata,
    LatestBaseCheckpoint,
    LatestBasePort,
)
from managed_config import ManagedConfig, ManagedConfigError, load_managed_config
import notifications
import work_exhaustion
import reviewer_specification
import blocked_checkpoint
import blocked_checkpoint_state
import execplan_archive
import execplan_archive_state
import requirement_archive
import requirement_archive_state
import execplan_catalog
import execplan_catalog_state
from planning_catalog import (
    MutableReadySnapshot,
    PlanDeletion,
    mutable_snapshot_token,
    snapshot_from_dict,
    snapshot_to_dict,
)
from planning_runtime import (
    MAX_PLANNER_VALIDATION_RETRIES,
    PlanningAgentResult,
    PlanningAgentResultKind,
    PlanningContext,
    PlanningLifecycleCoordinator,
    PlanningOutcome,
    PlanningOutcomeKind,
    PlanningResultInvalid,
    PlanningScopeResult,
    PlanningWorkspaceOwnershipError,
)
import project_registry
import runtime_state
import recovery_state
import recovery_runtime
import execution_handlers
import execution_revision
import execution_checkpoint_state
import checkpoint_ownership
from agent_invocation import (AgentProfile, AgentRequest, AgentResult, AgentResultKind,
                              AttemptSequence, InvocationContext, ResolvedProfile,
                              TrackedAgentCall, Usage, classify_agent_result)
from agent_provider_usage import claude_usage, codex_log_usage
import agent_invocation_state
import validation_feedback_state
from validation_feedback import normalize_feedback, redact_secrets
from worker_outcome import worker_outcome_instruction
from verify_process import run_verify_process, VerifyTerminationUncertain
from execution_state import ExecutionStatePort as _ExecutionStatePort
from types import SimpleNamespace
from maintenance_planning import (
    MaintenancePlanMode,
    MaintenancePlanningContext,
    MaintenancePlanningError,
    MaintenancePlanningRestart,
    PlanRevision,
    ReadyPlanSnapshot,
    terminal_new_plan_ids,
    validate_barrier_revision,
    validate_refactoring_plan,
    validate_revalidation_revision,
)
from maintenance_orchestration import (
    MaintenanceOrchestrationError,
    MaintenanceOutcome,
    MaintenancePlannerDecision,
    all_cycle_plans_merged,
    cycle_plan_status_error,
    maintenance_planning_fingerprint,
    parse_maintenance_planner_decision,
    select_cycle_ready_execplan,
    validate_prerequisite_plan_ids,
)
import maintenance_git
import maintenance_state
import maintenance_file_review
import maintenance_reconciliation
import maintenance_file_publication
import maintenance_file_ledger
import maintenance_findings
import maintenance_file_inventory
import document_publication
import scoped_blocking
import agent_scope_decision
import rebase_file_evidence
import managed_rebase_evidence
import manager_git_journal
from scoped_blocking import BlockReasonCode, BlockScope


ROOT = Path(__file__).resolve().parent
MAX_VALIDATION_FINGERPRINT_REPEATS = 3
MAX_TRANSIENT_AGENT_RETRIES = 3
MAX_REVIEWER_PROTOCOL_ATTEMPTS = 3
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
PR_CLOSED_BLOCK_REASON = BlockReasonCode.PULL_REQUEST_CLOSED.value
PR_CONFLICT_PREFLIGHT_BLOCK_REASON = "pr-conflict-preflight"
PR_CONFLICT_PREFLIGHT_ERROR_PREFIX = "PR_OPEN 충돌 복구 소유권 검증 실패:"
PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}
ACTIVE_EXECPLAN_STATUSES = runtime_state.ACTIVE_STATUSES
WAITING_EXECPLAN_STATUSES = frozenset({"PR_OPEN", "PLAN_BLOCKED"})
KNOWN_EXECPLAN_STATUSES = ACTIVE_EXECPLAN_STATUSES | WAITING_EXECPLAN_STATUSES | {
    "READY",
    "MERGED",
}
FIELDS = runtime_state.PROJECT_UPDATE_FIELDS

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
    specification_fingerprint TEXT,
    planning_fingerprint TEXT,
    planning_attempt_fingerprint TEXT,
    planning_workspace_owned INTEGER NOT NULL DEFAULT 0,
    planning_retry_count INTEGER NOT NULL DEFAULT 0,
    planner_session_id TEXT,
    worker_session_id TEXT,
    worker_session_binding TEXT,
    worker_session_transition TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    definition_snapshot TEXT,
    worker_input_token TEXT,
    archive_token TEXT,
    verify_token TEXT,
    review_token TEXT,
    publication_token TEXT,
    revision_resume_status TEXT,
    planning_validation_error TEXT,
    planning_result_pending_validation INTEGER NOT NULL DEFAULT 0,
    planning_result_completed INTEGER NOT NULL DEFAULT 0,
    planning_result TEXT,
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

MERGED_WORKTREE_CLEANUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS merged_worktree_cleanups (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    worktree TEXT NOT NULL,
    branch TEXT NOT NULL,
    registration TEXT NOT NULL,
    pr_head_oid TEXT NOT NULL,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""

MANAGER_BASE_FETCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS manager_base_fetches (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    refname TEXT NOT NULL,
    before_ref TEXT,
    target_oid TEXT NOT NULL,
    action_token TEXT,
    phase TEXT NOT NULL
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
    current_complexity TEXT,
    current_risk TEXT,
    current_revision_sha TEXT,
    catalog_generation INTEGER NOT NULL DEFAULT 0,
    publication_path TEXT,
    publication_content_hash TEXT,
    publication_sha TEXT,
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
    blocked_review_fingerprint TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    worker_session_id TEXT,
    worker_session_binding TEXT,
    worker_session_transition TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    definition_snapshot TEXT,
    worker_input_token TEXT,
    archive_token TEXT,
    verify_token TEXT,
    review_token TEXT,
    publication_token TEXT,
    revision_resume_status TEXT,
    verify_failure_history TEXT NOT NULL DEFAULT '{}',
    review_failure_history TEXT NOT NULL DEFAULT '{}',
    initial_worker_level INTEGER,
    effective_worker_level INTEGER,
    review_level_history TEXT NOT NULL DEFAULT '{}',
    blocked_reason TEXT,
    guidance_plan_hash TEXT,
    workspace_owned INTEGER NOT NULL DEFAULT 0,
    ever_started INTEGER NOT NULL DEFAULT 0,
    agent_role TEXT,
    agent_snapshot TEXT,
    agent_input_fingerprint TEXT,
    agent_baseline TEXT,
    agent_recovery TEXT,
    agent_input_plan TEXT,
    agent_result_plan TEXT,
    agent_result_snapshot TEXT,
    agent_manager_snapshot TEXT,
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
WHERE status IN ('IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING')
"""

EXECPLAN_CANCELLATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_cancellations (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    sources TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    complexity TEXT NOT NULL,
    risk TEXT NOT NULL,
    priority TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    plan_sha TEXT NOT NULL,
    deletion_commit TEXT NOT NULL,
    cancelled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

GENERAL_PLANNING_PUBLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS general_planning_publications (
    repo TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    snapshot_token TEXT NOT NULL,
    specification_fingerprint TEXT NOT NULL,
    planning_fingerprint TEXT NOT NULL,
    manifest TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


class ProjectRunKind(str, Enum):
    COMPLETED = "completed"
    WAITING = "waiting"
    BLOCKED = "blocked"
    TRANSIENT_PAUSE = "transient_pause"
    USAGE_LIMIT_PAUSE = "usage_limit_pause"
    CONFIGURATION_ERROR = "configuration_error"
    OPERATIONAL_ERROR = "operational_error"
    RUNTIME_HANDOFF = "runtime_handoff"


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
    has_plan_blocked: bool = False
    has_pr_open: bool = False
    project_blocked: bool = False

    @property
    def has_waiting(self) -> bool:
        return self.has_plan_blocked or self.has_pr_open or self.project_blocked


@dataclass(frozen=True)
class ProjectRegistrationInspection:
    root: Path
    base_branch: str
    provider: str
    agents_hash: str
    agents_approved: bool
    origin: str | None = None
    source_revision: str | None = None


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
    recovery: recovery_runtime.RecoveryDecision | None = None


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


class RequirementArchiveCollisionError(RequirementArchiveError):
    """A Requirement archive destination is already occupied."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Requirement archive 대상이 이미 존재합니다: {path}")


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
        reason_code: BlockReasonCode = (
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
        ),
    ) -> None:
        super().__init__(message)
        self.result_snapshot = result_snapshot
        self.reason_code = reason_code


class WorkspaceOwnershipError(RuntimeError):
    """A managed worktree cannot be proven to belong to its saved task."""


class WorkerResultError(RuntimeError):
    """A Worker final response cannot safely be published."""


class ExecPlanMetadataError(RuntimeError):
    """The selected ExecPlan declaration disagrees with the catalog."""


class ReviewerProtocolError(RuntimeError):
    """A failed Reviewer response does not identify actionable issues."""


class PublicationSafetyError(RuntimeError):
    """A remote publication branch cannot be proven safe to update."""


class ManagedWorktreeConflictError(PublicationSafetyError):
    """A managed worktree has plan-local state that prevents publication."""


class ProcessLockBusyError(RuntimeError):
    """The single Loop Manager process lock is already held."""


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
        if isinstance(invoke, TrackedAgentCall):
            invoke.attempts.previous_failure = result.ledger_kind
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
def process_lock(state_dir: Path, *, identity: str = "0"):
    # ponytail: 부트스트랩은 전역 직렬화한다. 실제 병렬 처리 도입 시 프로젝트별 잠금으로 교체한다.
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "loop-manager.lock"
    handle = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
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
        _write_process_lock_identity(handle, identity)
        yield handle
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_process_lock_identity(handle, identity: str) -> None:
    handle.seek(0)
    handle.write(identity.encode("ascii"))
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def _held_process_lock_identity(state_dir: Path) -> str:
    """Read the parent lease only after proving another process still holds it."""
    with (state_dir / "loop-manager.lock").open("r+b") as handle:
        identity = handle.read(128).decode("ascii", errors="replace")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if is_lock_contention(error):
                return identity
            raise
        else:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    raise RuntimeError("runtime 부모 process lock이 유지되지 않습니다.")


def _runtime_intent_path(state_dir: Path, mutation_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", mutation_id):
        raise RuntimeError("runtime mutation ID 형식이 유효하지 않습니다.")
    return state_dir / "runtime-intents" / f"{mutation_id}.json"


def _runtime_intent_fields(source: Path, runtime: Path, sha: str) -> dict[str, str]:
    return {
        "source": str(_canonical_path(source)),
        "path": str(_canonical_path(runtime)),
        "sha": sha,
    }


def _write_runtime_intent(
    state_dir: Path, mutation_id: str, source: Path, runtime: Path, sha: str,
    *, created: bool, lock_reason: str | None,
) -> None:
    path = _runtime_intent_path(state_dir, mutation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        directory = os.open(state_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    payload = {
        **_runtime_intent_fields(source, runtime, sha),
        "created": created,
        "lock_reason": lock_reason,
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _read_runtime_intent(state_dir, mutation_id, source, runtime, sha) == (
            created, lock_reason,
        ):
            return
        raise RuntimeError("runtime mutation intent가 기존 기록과 다릅니다.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _read_runtime_intent(
    state_dir: Path, mutation_id: str, source: Path, runtime: Path, sha: str,
) -> tuple[bool, str | None]:
    try:
        payload = json.loads(_runtime_intent_path(state_dir, mutation_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("runtime mutation intent를 확인할 수 없습니다.") from error
    if not isinstance(payload, dict) or any(
        payload.get(key) != value
        for key, value in _runtime_intent_fields(source, runtime, sha).items()
    ) or type(payload.get("created")) is not bool or (
        payload.get("lock_reason") is not None
        and not isinstance(payload.get("lock_reason"), str)
    ):
        raise RuntimeError("runtime mutation intent의 repository·경로·SHA가 다릅니다.")
    return payload["created"], payload.get("lock_reason")


def _legacy_runtime_adoption_path(state_dir: Path, source: Path, sha: str) -> Path:
    return state_dir / "runtime-intents" / "legacy" / runtime_repository_id(source) / f"{sha}.json"


def _validate_legacy_runtime_adoption(
    state_dir: Path, source: Path, runtime: Path, sha: str,
) -> None:
    try:
        payload = json.loads(
            _legacy_runtime_adoption_path(state_dir, source, sha).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise WorkspaceOwnershipError(
            f"runtime의 구버전 전환 소유 증거가 없습니다: {runtime}"
        ) from error
    if payload != {**_runtime_intent_fields(source, runtime, sha), "legacy": True}:
        raise WorkspaceOwnershipError(
            f"runtime의 구버전 전환 소유 증거가 다릅니다: {runtime}"
        )


def _record_legacy_runtime_adoption(
    state_dir: Path, source: Path, runtime: Path, sha: str,
) -> None:
    path = _legacy_runtime_adoption_path(state_dir, source, sha)
    if path.exists():
        _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**_runtime_intent_fields(source, runtime, sha), "legacy": True}
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


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


def _runtime_catalog_record(source: Path, runtime: Path) -> dict[str, str | bool]:
    expected = _canonical_path(runtime)
    for record in _worktree_catalog(source):
        if _canonical_path(Path(str(record.get("worktree", "")))) == expected:
            return record
    raise WorkspaceOwnershipError(f"runtime Git worktree 등록이 없습니다: {runtime}")


def _runtime_transition_state(source: Path, runtime: Path) -> dict[str, object]:
    expected = _canonical_path(runtime)
    record = next((item for item in _worktree_catalog(source)
                   if _canonical_path(Path(str(item.get("worktree", "")))) == expected),
                  None)
    return {
        "exists": runtime.exists() or runtime.is_symlink(),
        "registration": (
            {"head": record.get("HEAD"), "detached": bool(record.get("detached")),
             "locked": record.get("locked")}
            if record is not None else None
        ),
    }


def _managed_worktree_transition_state(repo: Path, worktree: Path) -> dict[str, object]:
    """Observe the registration and index tree of one ExecPlan workspace."""
    record = next((item for item in _worktree_catalog(repo)
                   if _canonical_path(Path(str(item.get("worktree", ""))))
                   == _canonical_path(worktree)), None)
    exists = worktree.exists() or worktree.is_symlink()
    return {
        "exists": exists,
        "registration": ({"head": record.get("HEAD"),
                          "branch": record.get("branch"),
                          "locked": record.get("locked")}
                         if record is not None else None),
        "index": (git(worktree, "write-tree").stdout.strip()
                  if exists and record is not None else None),
    }


def _managed_worktree_receipt(
    state_dir: Path, intent: Mapping[str, object], kind: str,
) -> Path:
    return state_dir / "managed-worktree-intents" / f"{intent['id']}.{kind}.json"


def _managed_worktree_marker(
    state_dir: Path, intent: Mapping[str, object], kind: str,
) -> bool:
    receipt = _managed_worktree_receipt(state_dir, intent, kind)
    return receipt.is_file() and manager_git_journal._read(receipt) == {
        "id": intent["id"], "repo": intent["repo"],
        "target": intent["target"], "before": intent["before"],
        "expected": intent["expected"],
    }


def _write_managed_worktree_receipt(
    state_dir: Path, token: str, repo: Path, worktree: Path,
    before: Mapping[str, object], expected: Mapping[str, object], kind: str,
) -> None:
    root = state_dir / "managed-worktree-intents"
    root.mkdir(parents=True, exist_ok=True)
    manager_git_journal._sync_directory(state_dir)
    manager_git_journal._write_new(root / f"{token}.{kind}.json", {
        "id": token, "repo": str(_canonical_path(repo)),
        "target": str(_canonical_path(worktree)), "before": dict(before),
        "expected": dict(expected),
    })


def _recover_managed_worktree_removal(
    state_dir: Path, repo: Path, worktree: Path,
) -> bool:
    return manager_git_journal.recover_active(
        state_dir, repo=str(_canonical_path(repo)),
        kind="managed_worktree_remove", target=str(_canonical_path(worktree)),
        observe=lambda: _managed_worktree_transition_state(repo, worktree),
        marker=lambda intent: _managed_worktree_marker(
            state_dir, intent, "remove",
        ),
    )


def _assess_missing_managed_cleanup(
    state_dir: Path, repo: Path, worktree: Path,
) -> CleanupAssessment:
    try:
        _recover_managed_worktree_removal(state_dir, repo, worktree)
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 정리 기록 복구 실패: {error}")
    return CleanupAssessment("missing")


def _runtime_transition_marker(
    state_dir: Path, source: Path, runtime: Path, sha: str,
    intent: Mapping[str, object],
) -> bool:
    expected = intent["expected"]
    lock_reason = expected["registration"]["locked"]
    if not isinstance(lock_reason, str) or not lock_reason.startswith("loop-manager-runtime:"):
        return False
    mutation_id = lock_reason.removeprefix("loop-manager-runtime:")
    try:
        return _read_runtime_intent(
            state_dir, mutation_id, source, runtime, sha,
        ) == (True, lock_reason)
    except RuntimeError:
        return False


def _validate_runtime_lock_origin(
    state_dir: Path, source: Path, runtime: Path, sha: str, lock_reason: object,
) -> None:
    prefix = "loop-manager-runtime:"
    if not isinstance(lock_reason, str) or not lock_reason.startswith(prefix):
        raise RuntimeError("runtime 잠금 소유권을 확인할 수 없습니다.")
    prior_created, prior_lock = _read_runtime_intent(
        state_dir, lock_reason.removeprefix(prefix), source, runtime, sha,
    )
    if not prior_created or prior_lock != lock_reason:
        raise RuntimeError("runtime 등록 증거가 유효하지 않습니다.")


def _validated_runtime_registration_marker(
    source: Path,
    state_dir: Path,
    runtime: Path,
    sha: str,
    mutation_id: str | None,
    *,
    legacy: bool,
    record_legacy_adoption: bool = False,
) -> str | None:
    """Verify the catalog marker against durable intent without changing Git."""
    lock_reason = _runtime_catalog_record(source, runtime).get("locked")
    if legacy:
        if lock_reason is None:
            if record_legacy_adoption:
                _record_legacy_runtime_adoption(state_dir, source, runtime, sha)
            else:
                _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
            return None
        _validate_runtime_lock_origin(state_dir, source, runtime, sha, lock_reason)
        return lock_reason

    if mutation_id is None:
        raise RuntimeError("runtime mutation ID가 없습니다.")
    created, expected_lock_reason = _read_runtime_intent(
        state_dir, mutation_id, source, runtime, sha,
    )
    if lock_reason != expected_lock_reason:
        raise RuntimeError("runtime 등록의 durable mutation 표식이 다릅니다.")
    if created and expected_lock_reason != f"loop-manager-runtime:{mutation_id}":
        raise RuntimeError("runtime 등록의 durable mutation ID가 다릅니다.")
    if not created and expected_lock_reason is None:
        _validate_legacy_runtime_adoption(state_dir, source, runtime, sha)
    elif not created:
        _validate_runtime_lock_origin(
            state_dir, source, runtime, sha, expected_lock_reason,
        )
    return lock_reason


def prepare_runtime_worktree(
    source: Path, state_dir: Path, *, mutation_id: str | None = None,
    commit_sha: str | None = None,
) -> RuntimeWorktree:
    """Select an immutable linked runtime worktree at a fetched commit."""

    source = _canonical_path(source)
    if commit_sha is None:
        _, commit_sha = fetch_bootstrap_revision(source, state_dir)
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
    removal_slot = manager_git_journal._slot(
        state_dir, str(source), "runtime_remove", str(runtime),
    )
    if (removal_slot / "active.json").exists():
        remove_runtime_worktree(
            source, state_dir, RuntimeWorktree(runtime, commit_sha, mutation_id),
        )
    if mutation_id is not None:
        manager_git_journal.recover_active(
            state_dir, repo=str(source), kind="runtime_create", target=str(runtime),
            observe=lambda: _runtime_transition_state(source, runtime),
            marker=lambda intent: _runtime_transition_marker(
                state_dir, source, runtime, commit_sha, intent,
            ),
        )
    if runtime.exists() or runtime.is_symlink():
        if not runtime.is_dir():
            raise WorkspaceOwnershipError(
                f"runtime 경로가 디렉터리가 아닙니다: {runtime}"
            )
        validate_runtime_worktree(source, state_dir, runtime, commit_sha)
        if mutation_id is not None:
            record = _runtime_catalog_record(source, runtime)
            lock_reason = record.get("locked")
            if lock_reason:
                prefix = "loop-manager-runtime:"
                if not isinstance(lock_reason, str) or not lock_reason.startswith(prefix):
                    raise WorkspaceOwnershipError(f"runtime 잠금 소유권을 확인할 수 없습니다: {runtime}")
                prior_id = lock_reason.removeprefix(prefix)
                prior_created, prior_lock = _read_runtime_intent(
                    state_dir, prior_id, source, runtime, commit_sha,
                )
                if not prior_created or prior_lock != lock_reason:
                    raise WorkspaceOwnershipError(f"runtime 등록 증거가 유효하지 않습니다: {runtime}")
            else:
                _validate_legacy_runtime_adoption(state_dir, source, runtime, commit_sha)
            _write_runtime_intent(
                state_dir, mutation_id, source, runtime, commit_sha,
                created=False, lock_reason=lock_reason if isinstance(lock_reason, str) else None,
            )
        return RuntimeWorktree(runtime, commit_sha, mutation_id)

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
    if mutation_id is not None:
        lock_reason = f"loop-manager-runtime:{mutation_id}"
        before_state = _runtime_transition_state(source, runtime)
        if before_state != {"exists": False, "registration": None}:
            raise WorkspaceOwnershipError("runtime 생성 직전 worktree 상태가 변경됐습니다.")
        expected_state = {
            "exists": True,
            "registration": {"head": commit_sha, "detached": True,
                             "locked": lock_reason},
        }

        def apply(_token: str) -> None:
            _write_runtime_intent(
                state_dir, mutation_id, source, runtime, commit_sha,
                created=True, lock_reason=lock_reason,
            )
            git(source, "worktree", "add", "--detach", "--lock",
                f"--reason={lock_reason}", str(runtime), commit_sha)
            validate_runtime_worktree(source, state_dir, runtime, commit_sha)

        manager_git_journal.execute(
            state_dir, owner="bootstrap", kind="runtime_create",
            repo=str(source), target=str(runtime), before=before_state,
            expected=expected_state, observe=lambda: _runtime_transition_state(source, runtime),
            apply=apply, marker=lambda intent: _runtime_transition_marker(
                state_dir, source, runtime, commit_sha, intent,
            ),
        )
    else:
        git(source, "worktree", "add", "--detach", str(runtime), commit_sha)
    validate_runtime_worktree(source, state_dir, runtime, commit_sha)
    return RuntimeWorktree(runtime, commit_sha, mutation_id)


def remove_runtime_worktree(
    source: Path, state_dir: Path, runtime: RuntimeWorktree,
) -> None:
    """Remove an inactive owned runtime under the caller's process lock.

    A missing directory is never proof of our removal: recovery requires the
    durable receipt written only after a successful non-forced Git removal.
    """
    source = _canonical_path(source)
    path = runtime.path
    sha = runtime.commit_sha
    _validate_managed_path_boundary(
        state_dir, path, expected_runtime_worktree(state_dir, source, sha),
        "runtime worktree",
    )
    receipts = state_dir / "runtime-removals"

    def marker(intent: Mapping[str, object]) -> bool:
        receipt = receipts / f"{intent['id']}.json"
        return receipt.is_file() and manager_git_journal._read(receipt) == {
            "id": intent["id"], "repo": str(source), "target": str(path),
            "before": intent["before"],
        }

    if manager_git_journal.recover_active(
        state_dir, repo=str(source), kind="runtime_remove", target=str(path),
        observe=lambda: _runtime_transition_state(source, path), marker=marker,
    ):
        return
    validate_runtime_worktree(source, state_dir, path, sha)
    before = _runtime_transition_state(source, path)
    lock_reason = before["registration"]["locked"]
    _validate_runtime_lock_origin(state_dir, source, path, sha, lock_reason)
    # Do not let Git remove ignored user data as part of an otherwise clean tree.
    if _ignored_worktree_paths(path):
        raise WorkspaceOwnershipError("runtime ignored 파일을 보존하기 위해 정리를 중단합니다.")
    receipts.mkdir(parents=True, exist_ok=True)
    manager_git_journal._sync_directory(state_dir)

    def apply(token: str) -> None:
        validate_runtime_worktree(source, state_dir, path, sha)
        if _runtime_transition_state(source, path) != before or _ignored_worktree_paths(path):
            raise WorkspaceOwnershipError("runtime 정리 직전 상태가 변경됐습니다.")
        git(source, "worktree", "unlock", str(path))
        # An interruption after unlock or before the receipt deliberately blocks;
        # neither absence nor a stale creation marker can authorize recovery.
        git(source, "worktree", "remove", str(path))
        manager_git_journal._write_new(receipts / f"{token}.json", {
            "id": token, "repo": str(source), "target": str(path), "before": before,
        })

    manager_git_journal.execute(
        state_dir, owner="bootstrap", kind="runtime_remove", repo=str(source),
        target=str(path), before=before,
        expected={"exists": False, "registration": None},
        observe=lambda: _runtime_transition_state(source, path),
        apply=apply, marker=marker,
    )


def bootstrap_base_branch(source: Path, state_dir: Path, origin: str) -> str:
    """Read a registered self base without changing SQLite before the handoff."""

    database = state_dir / "loop-manager.db"
    if not database.is_file():
        return "main"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
        if not {"repo", "base_branch"} <= columns:
            raise RuntimeError("상태 DB의 프로젝트 등록 정보를 읽을 수 없습니다.")
        origin_column = "origin" if "origin" in columns else "NULL AS origin"
        registered_filter = "WHERE registered = 1" if "registered" in columns else ""
        rows = connection.execute(
            f"SELECT repo, base_branch, {origin_column} FROM projects {registered_filter}"
        ).fetchall()
    # A registered alias or another worktree must not select the bootstrap base.
    matches = [row for row in rows if row[0] == str(source)]
    if len(matches) > 1:
        raise RuntimeError("등록된 self source identity가 중복됩니다.")
    if not matches:
        return "main"
    _, base, saved_origin = matches[0]
    if saved_origin is not None and str(saved_origin) != origin:
        raise RuntimeError("등록된 self origin과 현재 source origin이 다릅니다.")
    if not base:
        raise RuntimeError("등록된 self base branch가 비어 있습니다.")
    return str(base)


def _tracking_ref_oid(repo: Path, refname: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", f"{refname}^{{commit}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _recorded_base_fetch(
    repo: Path, state_dir: Path, base: str, *, owner: str,
) -> str:
    """Fetch one base object and move its tracking ref with an owned CAS."""
    refname = f"refs/remotes/origin/{base}"
    remote_ref = f"refs/heads/{base}"
    remote = git(repo, "ls-remote", "--heads", "origin", remote_ref, check=False)
    rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if remote.returncode or len(rows) != 1 or rows[0][1:] != [remote_ref]:
        raise RuntimeError(f"origin/{base}의 원격 OID를 확정할 수 없습니다.")
    selected = rows[0][0]
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", selected) is None:
        raise RuntimeError(f"origin/{base}의 원격 OID가 유효하지 않습니다.")
    before = {"oid": _tracking_ref_oid(repo, refname)}

    def apply(token: str) -> None:
        git(repo, "fetch", "--no-tags", "--refmap=", "origin", remote_ref)
        fetched = git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
        if fetched != selected:
            raise RuntimeError(f"origin/{base}가 fetch 중 변경됐습니다.")
        git(repo, "update-ref", "--create-reflog", "-m",
            f"loop-manager-base-fetch:{token}", refname, selected,
            before["oid"] or "0" * len(selected))

    def marker(intent: Mapping[str, object]) -> bool:
        token = intent["id"]
        result = git(repo, "reflog", "show", "-1", "--format=%H%x00%gs", refname,
                     check=False)
        return (
            result.returncode == 0
            and f"{intent['expected']['oid']}\x00loop-manager-base-fetch:{token}"
            in result.stdout.splitlines()
        )

    manager_git_journal.execute(
        state_dir, owner=owner, kind=("bootstrap_fetch" if owner == "bootstrap"
                                      else "project_fetch"),
        repo=str(_canonical_path(repo)), target=refname,
        before=before, expected={"oid": selected},
        observe=lambda: {"oid": _tracking_ref_oid(repo, refname)},
        apply=apply, marker=marker,
    )
    return selected


def fetch_bootstrap_revision(source: Path, state_dir: Path) -> tuple[str, str]:
    """Resolve the registered base and fetch that branch under the process lock."""

    source = _canonical_path(source)
    root = _canonical_path(Path(git(source, "rev-parse", "--show-toplevel").stdout.strip()))
    if root != source:
        raise RuntimeError(f"Loop Manager source 저장소 루트가 아닙니다: {source}")
    origin = github_repository(git(source, "remote", "get-url", "origin").stdout)
    base = bootstrap_base_branch(source, state_dir, origin)
    sha = _recorded_base_fetch(source, state_dir, base, owner="bootstrap")
    return base, sha


def validate_bootstrap_revision(source: Path, base: str, sha: str) -> None:
    """Reject a source or remote race before starting work at the selected SHA."""

    current = git(source, "rev-parse", "HEAD").stdout.strip()
    branch_result = git(source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    remote = git(
        source, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
    ).stdout.strip()
    if current != sha or remote != sha or branch != base:
        raise RuntimeError(
            f"bootstrap revision 변경: source={current} branch={branch} "
            f"origin/{base}={remote} expected={sha}"
        )
    operations = _git_operation_entries(source)
    if operations:
        raise RuntimeError("진행 중 Git operation: " + ", ".join(sorted(operations)))


def prepare_bootstrap_runtime(
    source: Path, state_dir: Path, mutation_id: str,
) -> RuntimeWorktree | BootstrapRefreshHandoff:
    """Refresh an old source and hand control to the next external invocation."""

    base, sha = fetch_bootstrap_revision(source, state_dir)
    result = synchronize_base_worktree(
        source, base, remote_sha=sha, state_dir=state_dir, owner="bootstrap",
    )
    if result.status == "updated":
        operation_log(f"bootstrap source 갱신 완료: origin/{base} {sha}; 다음 실행에 인계")
        return BootstrapRefreshHandoff(base, sha)
    if result.status != "equal":
        raise RuntimeError(
            f"bootstrap source 갱신 불가 (origin/{base} {sha}): "
            f"{result.status}: {result.reason}"
        )
    validate_bootstrap_revision(source, base, sha)
    runtime = prepare_runtime_worktree(
        source, state_dir, mutation_id=mutation_id, commit_sha=sha,
    )
    validate_bootstrap_revision(source, base, sha)
    return runtime


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
        "--runtime-mutation-id",
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
    child_arguments = [
        "--runtime-child",
        "--runtime-source",
        str(source),
        "--runtime-sha",
        runtime.commit_sha,
    ]
    if runtime.mutation_id is not None:
        child_arguments.extend(("--runtime-mutation-id", runtime.mutation_id))
    return [
        *child_arguments,
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
            "-B",  # Keep immutable runtime imports from creating ignored bytecode.
            "-u",
            str(runtime.path / "loop_manager.py"),
            *child_arguments,
        ],
        cwd=runtime.path,
        check=False,
    )
    if runtime.mutation_id is not None:
        try:
            remove_runtime_worktree(source, state_dir, runtime)
        except (RuntimeError, OSError) as error:
            operation_log(f"runtime 정리 보류: {error}", error=True)
    return completed.returncode


def validate_runtime_child(
    source: Path,
    state_dir: Path,
    commit_sha: str,
) -> None:
    validate_runtime_worktree(source, state_dir, ROOT, commit_sha)


def validate_runtime_mutation(
    source: Path, state_dir: Path, sha: str, mutation_id: str | None,
) -> RuntimeMutation:
    """Bind a child invocation to the held parent lock and actual Git runtime."""
    runtime = expected_runtime_worktree(state_dir, source, sha)
    if _canonical_path(ROOT) != _canonical_path(runtime):
        raise RuntimeError(f"runtime 경로가 선택한 SHA와 다릅니다: {ROOT} (expected {runtime})")
    identity = _held_process_lock_identity(state_dir)
    if mutation_id is None:
        if identity != "0":
            raise RuntimeError("mutation ID가 없는 새 형식 runtime 호출은 허용되지 않습니다.")
        legacy = True
    else:
        if identity != f"runtime:{mutation_id}":
            raise RuntimeError("runtime mutation ID와 부모 process lock이 다릅니다.")
        legacy = False
    origin = repository_origin(source)
    base = bootstrap_base_branch(source, state_dir, origin)
    origin_sha = git(
        source, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
    ).stdout.strip()
    if origin_sha != sha:
        raise RuntimeError(
            f"runtime SHA가 현재 origin/{base}와 다릅니다: "
            f"{sha} (origin/{base} {origin_sha})"
        )
    validate_runtime_worktree(source, state_dir, runtime, sha)
    lock_reason = _validated_runtime_registration_marker(
        source, state_dir, runtime, sha, mutation_id,
        legacy=legacy, record_legacy_adoption=True,
    )
    operation_log(
        f"runtime mutation 검증: path={runtime} sha={sha} "
        f"mode={'legacy' if legacy else 'owned'}"
    )
    return RuntimeMutation(source, runtime, sha, mutation_id, legacy, lock_reason)

class StateConnection(sqlite3.Connection):
    """Keep completed schema initialization local to one database connection."""

    execplans_schema_ready = False


def _ensure_execplans_schema(connection: sqlite3.Connection) -> None:
    if isinstance(connection, StateConnection) and connection.execplans_schema_ready:
        return
    connection.execute(EXEC_PLANS_SCHEMA)
    execplan_catalog_state.ensure_schema(connection)
    execplan_archive_state.ensure_schema(connection)
    requirement_archive_state.ensure_schema(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(exec_plans)")}
    if "blocked_fingerprint" not in columns:
        connection.execute("ALTER TABLE exec_plans ADD COLUMN blocked_fingerprint TEXT")
    if "blocked_worktree_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN blocked_worktree_fingerprint TEXT"
        )
    if "blocked_review_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN blocked_review_fingerprint TEXT"
        )
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 0"
        )
    migrations = {
        "archive_resume_status": "TEXT",
        "reviewer_session_id": "TEXT",
        "worker_session_binding": "TEXT",
        "worker_session_transition": "TEXT",
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
        "agent_role": "TEXT",
        "agent_snapshot": "TEXT",
        "agent_input_fingerprint": "TEXT",
        "agent_baseline": "TEXT",
        "agent_recovery": "TEXT",
        "agent_input_plan": "TEXT",
        "agent_result_plan": "TEXT",
        "agent_result_snapshot": "TEXT",
        "agent_manager_snapshot": "TEXT",
        "current_complexity": "TEXT",
        "current_risk": "TEXT",
        "current_revision_sha": "TEXT",
        "catalog_generation": "INTEGER NOT NULL DEFAULT 0",
        "publication_path": "TEXT",
        "publication_content_hash": "TEXT",
        "publication_sha": "TEXT",
        "definition_snapshot": "TEXT",
        "worker_input_token": "TEXT",
        "archive_token": "TEXT",
        "verify_token": "TEXT",
        "review_token": "TEXT",
        "publication_token": "TEXT",
        "revision_resume_status": "TEXT",
        "ever_started": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE exec_plans ADD COLUMN {column} {definition}"
            )
    if {
        "path",
        "content_hash",
        "plan_sha",
    }.issubset(columns):
        # RELEASE commits a standalone backfill, but preserves an existing
        # caller transaction. Re-run after interrupted column additions too.
        connection.execute("SAVEPOINT publication_backfill")
        try:
            connection.execute(
                """
                UPDATE exec_plans
                SET publication_path = COALESCE(publication_path, path),
                    publication_content_hash = COALESCE(
                        publication_content_hash, content_hash
                    ),
                    publication_sha = COALESCE(publication_sha, plan_sha),
                    current_revision_sha = COALESCE(current_revision_sha, plan_sha)
                WHERE publication_path IS NULL
                   OR publication_content_hash IS NULL
                   OR (publication_sha IS NULL AND plan_sha IS NOT NULL)
                   OR (current_revision_sha IS NULL AND plan_sha IS NOT NULL)
                """
            )
        except BaseException:
            connection.execute("ROLLBACK TO publication_backfill")
            connection.execute("RELEASE publication_backfill")
            raise
        else:
            connection.execute("RELEASE publication_backfill")
    # A prior process may have committed the column before backfilling rows.
    # Repeat the monotonic evidence check on every new connection.
    migrated_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(exec_plans)")
    }
    evidence = []
    if "status" in migrated_columns:
        evidence.append("status != 'READY'")
    for field in (
        "active_sha",
        "validated_base_sha",
        "publication_remote_oid",
        "pr_conflict_head_oid",
        "pr_conflict_base_oid",
        "branch",
        "worktree",
        "pr_url",
        "pr_head_oid",
        "last_error",
        "blocked_fingerprint",
        "blocked_worktree_fingerprint",
        "blocked_review_fingerprint",
        "worker_session_id",
        "reviewer_session_id",
        "worker_result",
        "verify_result",
        "review_result",
        "blocked_reason",
        "guidance_plan_hash",
        "agent_role",
        "agent_snapshot",
        "agent_input_fingerprint",
        "agent_baseline",
        "agent_recovery",
        "agent_input_plan",
        "agent_result_plan",
        "agent_result_snapshot",
        "agent_manager_snapshot",
    ):
        if field in migrated_columns:
            evidence.append(f"{field} IS NOT NULL")
    for field in (
        "pr_conflict_recovery",
        "retry_count",
        "workspace_owned",
    ):
        if field in migrated_columns:
            evidence.append(f"{field} != 0")
    for field in (
        "verify_failure_history",
        "review_failure_history",
        "review_level_history",
    ):
        if field in migrated_columns:
            evidence.append(f"{field} != '{{}}'")
    if evidence:
        # RELEASE commits a standalone repair without committing a caller's
        # existing transaction, matching the publication backfill above.
        connection.execute("SAVEPOINT ever_started_backfill")
        try:
            connection.execute(
                "UPDATE exec_plans SET ever_started = 1 WHERE ever_started = 0 "
                "AND (" + " OR ".join(evidence) + ")"
            )
        except BaseException:
            connection.execute("ROLLBACK TO ever_started_backfill")
            connection.execute("RELEASE ever_started_backfill")
            raise
        else:
            connection.execute("RELEASE ever_started_backfill")
    connection.execute(GUIDANCE_APPROVALS_SCHEMA)
    connection.execute(EXECPLAN_CANCELLATIONS_SCHEMA)
    connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
    agent_invocation_state.ensure_schema(connection)
    validation_feedback_state.ensure_schema(connection)
    validation_feedback_state.ValidationFeedbackRepository(connection).migrate_legacy()
    scoped_blocking.ensure_schema(connection)
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    blocked_checkpoint_state.ensure_schema(connection)
    connection.execute("DROP INDEX IF EXISTS one_active_execplan_per_project")
    connection.execute(EXEC_PLANS_ACTIVE_INDEX)
    scoped_blocking.migrate_legacy_blocks(connection)
    if isinstance(connection, StateConnection):
        connection.execplans_schema_ready = True


def connect_database(
    state_dir: Path,
    *,
    create: bool = False,
    run_migrations: bool = True,
) -> sqlite3.Connection:
    database = state_dir / "loop-manager.db"
    if not create and not database.is_file():
        raise RuntimeError(
            "상태 DB가 없습니다. 먼저 `loop_manager.py config init`을 실행하세요."
        )
    connection = sqlite3.connect(database, timeout=30.0, factory=StateConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if create:
        connection.execute(SCHEMA)
        project_registry.ensure_registry_schema(connection)
        _ensure_execplans_schema(connection)
        _ensure_cleanup_pending_schema(connection)
        maintenance_state.ensure_schema(connection)
        notifications.ensure_schema(connection)
        blocked_checkpoint_state.ensure_schema(connection)
        connection.commit()
    else:
        try:
            connection.execute("SELECT 1 FROM projects LIMIT 1")
        except sqlite3.DatabaseError as error:
            connection.close()
            raise RuntimeError("상태 DB를 읽을 수 없습니다. 백업을 복원하거나 다시 초기화하세요.") from error
    if not run_migrations:
        # Scheduler workers inherit the schema initialized by the parent.
        connection.execplans_schema_ready = True
        return connection
    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    migrations = {
        "provider": "TEXT NOT NULL DEFAULT 'codex'",
        "specification_fingerprint": "TEXT",
        "plan_path": "TEXT",
        "planning_fingerprint": "TEXT",
        "planning_attempt_fingerprint": "TEXT",
        "planning_workspace_owned": "INTEGER NOT NULL DEFAULT 0",
        "planning_retry_count": "INTEGER NOT NULL DEFAULT 0",
        "planner_session_id": "TEXT",
        "plan_sha": "TEXT",
        "worker_session_id": "TEXT",
        "worker_session_binding": "TEXT",
        "worker_session_transition": "TEXT",
        "reviewer_session_id": "TEXT",
        "worker_result": "TEXT",
        "verify_result": "TEXT",
        "review_result": "TEXT",
        "planning_validation_error": "TEXT",
        "planning_result_pending_validation": "INTEGER NOT NULL DEFAULT 0",
        "planning_result_completed": "INTEGER NOT NULL DEFAULT 0",
        "planning_result": "TEXT",
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
        "definition_snapshot": "TEXT",
        "worker_input_token": "TEXT",
        "archive_token": "TEXT",
        "verify_token": "TEXT",
        "review_token": "TEXT",
        "publication_token": "TEXT",
        "revision_resume_status": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {definition}")
    if not migrations.keys() <= columns:
        connection.commit()
    project_registry.ensure_registry_schema(connection)
    _ensure_execplans_schema(connection)
    _ensure_cleanup_pending_schema(connection)
    maintenance_state.ensure_schema(connection)
    notifications.ensure_schema(connection)
    blocked_checkpoint_state.ensure_schema(connection)
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
    if current is None:
        notifications.enqueue_current_project_events(connection, repo)
        connection.commit()
        return
    if (
        current["plan_path"]
        and not ("phase" in values and values["phase"] is None)
    ):
        _ensure_execplans_schema(connection)
    runtime_state.RuntimeStateRepository(connection).update_project(repo, values)
    connection.commit()


def command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = _handoff_run(
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


def git(
    repo: Path, *arguments: str, check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_index = 0
    while command_index < len(arguments) and arguments[command_index] == "-c":
        command_index += 2
    if arguments[command_index : command_index + 1] == ("rebase",):
        rebase_arguments = arguments[command_index + 1:]
        overlap_error = None
        if rebase_arguments and rebase_arguments[-1] in {
            "--continue", "--skip", "--abort"
        }:
            overlap_error = _rebase_resume_ignored_overlap(repo, rebase_arguments[-1])
        elif rebase_arguments and not rebase_arguments[-1].startswith("-"):
            overlaps = _ignored_paths_overlapping_git_update(
                repo, rebase_arguments[-1]
            )
            if overlaps:
                overlap_error = (
                    "rebase 대상 tracked 경로와 겹치는 ignored 파일: "
                    + ", ".join(overlaps[:8])
                )
        if overlap_error:
            if check:
                raise ManagedWorktreeConflictError(overlap_error)
            return subprocess.CompletedProcess(
                ["git", "-C", str(repo), *arguments], 1, overlap_error
            )
    result = command(
        ["git", "-C", str(repo), *arguments], check=False,
        **({"env": env} if env is not None else {}),
    )
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
            project = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?", (row["repo"],)
            ).fetchone()
            scoped_blocking.record_project_block(
                connection,
                str(row["repo"]),
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                PLAN_SHA_BACKFILL_ERROR,
                None,
                plan_id=_plan_id_from_path(str(row["plan_path"] or "")),
                previous_phase=(str(project["phase"] or "") or None),
            )
            notifications.enqueue_current_project_events(connection, str(row["repo"]))
    connection.commit()


def git_bytes(repo: Path, *arguments: str) -> bytes:
    result = _handoff_run(
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


def revision_execplan_snapshot(
    repo: Path,
    revision: str,
    *,
    archive_conflict_plan_ids: frozenset[str] = frozenset(),
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    """Read and fully validate active/archive plans from one immutable tree."""

    revision_sha = git(
        repo, "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).stdout.strip()
    tree = git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision_sha)
    documents: dict[str, bytes] = {}
    regular_paths: set[str] = set()
    invalid_plan_entries: dict[str, str] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("Git tree 항목 형식이 올바르지 않습니다.")
        mode, object_type, object_id = parts
        try:
            relative_path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            if raw_path.startswith(b"docs/plans/"):
                raise execplan_catalog.ExecPlanCatalogError(
                    "ExecPlan Git 경로가 UTF-8이 아닙니다.",
                    code="invalid-path-encoding",
                ) from error
            continue
        regular = object_type == b"blob" and mode in {b"100644", b"100755"}
        if regular:
            regular_paths.add(relative_path)
        plan_candidate = bool(
            re.fullmatch(r"docs/plans/[^/]+\.md", relative_path)
            or re.fullmatch(
                r"docs/plans/archive/[^/]+\.md", relative_path
            )
        )
        if not plan_candidate:
            continue
        if not regular:
            invalid_plan_entries[relative_path] = (
                "symlink" if mode == b"120000" else mode.decode("ascii")
            )
            continue
        documents[relative_path] = git_bytes(
            repo, "cat-file", "blob", object_id.decode("ascii")
        )
    return execplan_catalog.snapshot_from_documents(
        documents,
        validate_sources=True,
        available_regular_paths=frozenset(regular_paths),
        invalid_plan_entries=invalid_plan_entries,
        revision_sha=revision_sha,
        archive_conflict_plan_ids=archive_conflict_plan_ids,
    )


def _merged_execplan_ids(
    connection: sqlite3.Connection, repo: str
) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ? AND status = 'MERGED'",
            (repo,),
        )
    )


class _GitCatalogDefinitionPort:
    """Pin catalog reads to a fetched remote ref without consulting worktrees."""

    def __init__(
        self,
        repo: Path,
        remote_ref: str,
        archive_conflict_plan_ids: Callable[[], frozenset[str]],
        pinned_revision: str | None = None,
    ):
        self.repo = repo
        self.remote_ref = remote_ref
        self.archive_conflict_plan_ids = archive_conflict_plan_ids
        self.pinned_revision = pinned_revision

    def current_revision(self) -> str:
        return git(
            self.repo,
            "rev-parse",
            "--verify",
            f"{self.remote_ref}^{{commit}}",
        ).stdout.strip()

    def load(self) -> execplan_catalog_state.PinnedCatalog:
        revision_sha = self.pinned_revision or self.current_revision()
        try:
            snapshot = revision_execplan_snapshot(
                self.repo,
                revision_sha,
                archive_conflict_plan_ids=self.archive_conflict_plan_ids(),
            )
        except execplan_catalog.ExecPlanCatalogError as error:
            tree = _git_tree_entries(self.repo, revision_sha)
            evidence = []
            for path in error.related_paths:
                entry = tree.get(path.encode("utf-8"))
                if entry is not None:
                    evidence.append(
                        f"{path}:{entry[0].decode('ascii')}:"
                        f"{entry[1].decode('ascii')}"
                    )
                else:
                    evidence.append(f"{path}:missing")
            diagnostic = execplan_catalog_state.ReconciliationDiagnostic(
                revision_sha=revision_sha,
                code=error.code,
                message=str(error),
                path=error.path,
                source=error.source,
                related_paths=error.related_paths,
                fingerprint=execplan_catalog_state.diagnostic_fingerprint(
                    revision_sha,
                    error.code,
                    str(error),
                    tuple(evidence),
                ),
            )
            raise execplan_catalog_state.InvalidPinnedCatalog(
                diagnostic
            ) from error
        tree = git_bytes(
            self.repo, "ls-tree", "-r", "-z", "--full-tree", revision_sha
        )
        regular_paths: set[str] = set()
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, _separator, path = record.partition(b"\t")
            if metadata.split()[:2] not in (
                [b"100644", b"blob"], [b"100755", b"blob"]
            ):
                continue
            try:
                regular_paths.add(path.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        return execplan_catalog_state.PinnedCatalog(
            revision_sha, snapshot, frozenset(regular_paths)
        )


def reconcile_execplan_catalog(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base_branch: str,
    *,
    record_invalid: bool = True,
    remote_sha: str | None = None,
) -> execplan_catalog_state.CatalogReconciliationResult:
    """Converge declaration cache or persist a recoverable project diagnostic."""

    remote_ref = f"refs/remotes/origin/{base_branch}"
    if remote_sha is not None:
        current_sha = git(
            repo_path, "rev-parse", "--verify", f"{remote_ref}^{{commit}}"
        ).stdout.strip()
        if current_sha != remote_sha:
            return execplan_catalog_state.CatalogReconciliationResult(
                execplan_catalog_state.ReconciliationKind.STALE,
                revision_sha=remote_sha,
            )
    port = _GitCatalogDefinitionPort(
        repo_path,
        remote_ref,
        lambda: _merged_execplan_ids(connection, repo),
        pinned_revision=remote_sha,
    )
    result = execplan_catalog_state.CatalogReconciliationService(
        execplan_catalog_state.DefinitionStateRepository(connection),
        port,
    ).reconcile(repo)
    catalog_reason = BlockReasonCode.EXECPLAN_CATALOG_INVALID.value
    existing_block = scoped_blocking.project_block(connection, repo)
    if result.kind is execplan_catalog_state.ReconciliationKind.INVALID_CATALOG:
        if not record_invalid:
            return result
        diagnostic = result.diagnostic
        assert diagnostic is not None
        location = diagnostic.path or "(catalog)"
        source = f"; source={diagnostic.source}" if diagnostic.source else ""
        evidence = (
            "원격 ExecPlan catalog가 유효하지 않습니다: "
            f"revision={diagnostic.revision_sha}; path={location}{source}; "
            f"code={diagnostic.code}; {diagnostic.message}"
        )
        project = connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        plan_id = _plan_id_from_path(diagnostic.path or "")
        with connection:
            scoped_blocking.record_project_block(
                connection,
                repo,
                BlockReasonCode.EXECPLAN_CATALOG_INVALID,
                evidence,
                diagnostic.fingerprint,
                plan_id=plan_id,
                previous_phase=(
                    str(project["phase"])
                    if project is not None and project["phase"]
                    else None
                ),
            )
            notifications.enqueue_current_project_events(connection, repo)
        return result
    if (
        record_invalid
        and result.kind is execplan_catalog_state.ReconciliationKind.RECONCILED
        and existing_block is not None
        and existing_block["reason_code"] == catalog_reason
    ):
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
    return result


def reconcile_execution_execplan_catalog(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base_branch: str,
) -> execplan_catalog_state.CatalogReconciliationResult:
    """Let the execution recovery gate persist its own final decision."""

    return reconcile_execplan_catalog(
        connection, repo_path, repo, base_branch, record_invalid=False
    )


def _is_active_requirement(path: bytes) -> bool:
    return re.fullmatch(rb"docs/requirements/[^/]*\.md", path) is not None


def _is_planning_input(path: bytes) -> bool:
    if path in {b"AGENTS.md", b"docs/PRODUCT.md", b"docs/ARCHITECTURE.md"}:
        return True
    if _is_active_requirement(path):
        return True
    return not path.startswith(b"docs/")


def _is_specification_input(path: bytes) -> bool:
    return path in {b"docs/PRODUCT.md", b"docs/ARCHITECTURE.md"} or (
        _is_active_requirement(path)
    )


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


def _specification_input_digest(
    entries: list[tuple[bytes, bytes, bytes]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-specification-input-v1\0")
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


def _specification_input_fingerprint(repo: Path, revision: str) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in _git_tree_entries(repo, revision).items():
        if not _is_specification_input(path):
            continue
        contents = (
            git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            if object_type == b"blob"
            else object_id
        )
        entries.append((path, object_type, contents))
    return _specification_input_digest(entries)


def specification_input_fingerprint(repo: Path, base: str) -> str:
    return _specification_input_fingerprint(repo, f"origin/{base}")


def _is_requirement_archive_only_change(
    repo: Path,
    previous_revision: str,
    current_revision: str,
) -> bool:
    previous_tree = _git_tree_entries(repo, previous_revision)
    current_tree = _git_tree_entries(repo, current_revision)
    archive_prefix = b"docs/requirements/archive/"

    def relevant(path: bytes) -> bool:
        return _is_planning_input(path) or path.startswith(archive_prefix)

    previous = {
        path.decode("utf-8"): value
        for path, value in previous_tree.items()
        if relevant(path)
    }
    current = {
        path.decode("utf-8"): value
        for path, value in current_tree.items()
        if relevant(path)
    }
    return execplan_catalog.classify_archive_only_tree_change(
        previous, current
    ).requirement_only


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def requirement_archive_path(source: str) -> str:
    return execplan_catalog.requirement_archive_path(source)


def parse_frontmatter(text: str) -> dict[str, object]:
    return execplan_catalog.parse_frontmatter(text)


def _parse_execplan(
    worktree: Path,
    path: Path,
    *,
    require_priority: bool = False,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, object]:
    return execplan_catalog.parse_execplan_file(
        worktree,
        path,
        require_priority=require_priority,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    )



def _validate_dependency_graph(catalog: dict[str, dict[str, object]]) -> None:
    return execplan_catalog.validate_dependency_graph(catalog)


def load_execplan_snapshot(
    worktree: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    return execplan_catalog.load_execplan_snapshot(
        worktree,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    )


def load_execplan_catalog(
    worktree: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, dict[str, object]]:
    return load_execplan_snapshot(
        worktree,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    ).active_metadata()



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


class PlannerValidationError(PlanningResultInvalid):
    """A Planner output error that is eligible for a correction attempt."""


@dataclass(frozen=True)
class ValidatedPlanningResult:
    """Validated files to publish, separated from executable plans."""

    plans: tuple[tuple[str, dict[str, object]], ...]
    publication_paths: tuple[str, ...]
    publication_hashes: tuple[tuple[str, str], ...] = ()
    revisions: tuple[PlanRevision, ...] = ()
    deletions: tuple[PlanDeletion, ...] = ()
    mutable_snapshot: MutableReadySnapshot | None = None
    maintenance_context: MaintenancePlanningContext | None = None
    refactoring_plan_ids: tuple[str, ...] = ()
    revalidation_plan_ids: tuple[str, ...] = ()


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
    *,
    connection: sqlite3.Connection | None = None,
    repo: str | None = None,
    mutable_snapshot: MutableReadySnapshot | None = None,
) -> ValidatedPlanningResult | None:
    """Validate one create/revise/rename/delete planning batch.

    Scope and ExecPlan catalog errors are deliberately translated to a
    dedicated exception. Operational Git failures must retain their normal
    recovery boundary and never consume the Planner validation retry budget.
    """
    planned_files = sorted(set(dirty_files) | set(changed_files(worktree, active_sha)))
    if not planned_files:
        return None
    snapshot = mutable_snapshot or MutableReadySnapshot(
        repo=repo or "",
        active_sha=active_sha,
        plans=(),
        token=mutable_snapshot_token(repo or "", active_sha, ()),
    )
    if snapshot.active_sha != active_sha or (repo is not None and snapshot.repo != repo):
        raise PlannerValidationError("mutable READY snapshot이 planning 기준과 다릅니다.")

    plan_paths: list[str] = []
    base_paths: set[str] = set()
    current_plans: dict[str, dict[str, object]] = {}
    for changed_path in planned_files:
        if changed_path == "docs/FUTURE_IDEAS.md":
            if not (worktree / changed_path).is_file():
                raise PlannerValidationError(
                    "Planner는 docs/FUTURE_IDEAS.md를 삭제할 수 없습니다."
                )
            continue
        if not re.fullmatch(r"docs/plans/[^/]+\.md", changed_path):
            raise PlannerValidationError(
                "Planner는 docs/plans의 새 계획, mutable READY와 "
                "docs/FUTURE_IDEAS.md만 변경할 수 있습니다."
            )
        existed = git(
            worktree,
            "cat-file",
            "-e",
            f"{active_sha}:{changed_path}",
            check=False,
        ).returncode == 0
        if existed:
            if changed_path not in snapshot.by_path:
                if mutable_snapshot is None:
                    raise PlannerValidationError(
                        "Planner는 기존 ExecPlan을 수정·삭제할 수 없습니다: "
                        f"{changed_path}"
                    )
                raise PlannerValidationError(
                    "Planner는 snapshot 밖 ExecPlan을 변경할 수 없습니다: "
                    f"{changed_path}"
                )
            base_paths.add(changed_path)
        if not (worktree / changed_path).is_file():
            plan_paths.append(changed_path)
            continue
        try:
            planner_text = (worktree / changed_path).read_text(encoding="utf-8")
            parsed = _parse_execplan(
                worktree,
                worktree / changed_path,
                require_priority=not existed,
                allow_archive_fallback=False,
            )
        except (OSError, UnicodeError, RuntimeError) as error:
            raise PlannerValidationError(
                f"Planner ExecPlan을 읽을 수 없습니다: {changed_path} ({error})"
            ) from error
        if contains_guidance_sections(planner_text):
            raise PlannerValidationError("Planner는 AGENTS.md guidance를 제안할 수 없습니다.")
        current_plans[changed_path] = parsed
        plan_paths.append(changed_path)

    try:
        catalog = load_execplan_catalog(worktree)
        plans: list[tuple[str, dict[str, object]]] = []
        revisions: list[PlanRevision] = []
        deletions: list[PlanDeletion] = []
        renamed_ids: set[str] = set()
        displaced_paths: set[str] = set()
        for path in base_paths:
            original = snapshot.by_path.get(path)
            if original is None:
                raise RuntimeError(
                    f"Planner는 snapshot 밖 ExecPlan을 변경할 수 없습니다: {path}"
                )
            current = current_plans.get(path)
            if current is None or str(current["id"]) != original.plan_id:
                displaced_paths.add(path)

        for path, parsed in current_plans.items():
            plan_id = str(parsed["id"])
            path_identity = snapshot.by_path.get(path)
            if path_identity is not None and plan_id == path_identity.plan_id:
                revisions.append(PlanRevision(path, parsed, path_identity))
                continue
            original = snapshot.by_id.get(plan_id)
            if original is not None:
                if original.path not in displaced_paths or plan_id in renamed_ids:
                    raise RuntimeError(
                        f"ExecPlan rename identity가 모호합니다: {plan_id}"
                    )
                revisions.append(PlanRevision(path, parsed, original))
                renamed_ids.add(plan_id)
            else:
                plans.append((path, parsed))

        for path in sorted(displaced_paths):
            original = snapshot.by_path[path]
            if original.plan_id not in renamed_ids:
                deletions.append(PlanDeletion(original))

        if connection is not None and repo is not None and deletions:
            deleted_ids = {item.snapshot.plan_id for item in deletions}
            revised_ids = {item.snapshot.plan_id for item in revisions}
            for row in connection.execute(
                "SELECT plan_id, depends_on FROM exec_plans WHERE repo = ?",
                (repo,),
            ):
                plan_id = str(row["plan_id"])
                if plan_id in deleted_ids or plan_id in revised_ids:
                    continue
                dependencies = set(
                    _stored_string_list(row["depends_on"], "depends_on")
                )
                blocked = sorted(dependencies & deleted_ids)
                if blocked:
                    raise RuntimeError(
                        "삭제 계획을 snapshot 밖 실행 catalog가 참조합니다: "
                        f"{plan_id} -> {', '.join(blocked)}"
                    )

        new_plan_paths = [path for path, _plan in plans]
        _validate_future_idea_separation(worktree, active_sha, new_plan_paths)
        new_ids = {str(plan["id"]) for _, plan in plans}
        reserved_ids = set(catalog) - new_ids
        reserved_ids.update(plan.plan_id for plan in snapshot.plans)
        if connection is not None and repo is not None:
            reserved_ids.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT plan_id FROM exec_plans WHERE repo = ? "
                    "UNION SELECT plan_id FROM execplan_cancellations WHERE repo = ?",
                    (repo, repo),
                )
            )
        archived_directory = worktree / "docs/plans/archive"
        if archived_directory.is_dir():
            for archived in archived_directory.glob("*.md"):
                match = re.match(r"\[(G-\d{3,})\]", archived.name)
                if match is not None:
                    reserved_ids.add(match.group(1))
        if new_ids & reserved_ids:
            raise RuntimeError(
                "새 ExecPlan ID가 기존 또는 취소된 ID를 재사용합니다: "
                + ", ".join(sorted(new_ids & reserved_ids))
            )
        existing_ids = [int(plan_id[2:]) for plan_id in reserved_ids]
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
            publication_hashes=tuple(
                (path, file_hash(worktree / path))
                for path in planned_files
                if (worktree / path).is_file()
            ),
            revisions=tuple(
                sorted(revisions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            deletions=tuple(
                sorted(deletions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            mutable_snapshot=snapshot,
        )
    except RuntimeError as error:
        raise PlannerValidationError(str(error)) from error


def _stored_string_list(value: object, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise MaintenancePlanningError(
            f"{field} catalog 값이 올바른 JSON 배열이 아닙니다."
        ) from error
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise MaintenancePlanningError(
            f"{field} catalog 값에는 문자열 배열이 필요합니다."
        )
    return tuple(parsed)


def _ready_plan_snapshot_from_contents(
    row: sqlite3.Row,
    contents: bytes,
) -> ReadyPlanSnapshot:
    plan_id = str(row["plan_id"])
    path = str(row["path"])
    ownership = {
        "branch": row["branch"],
        "worktree": row["worktree"],
        "workspace reservation": int(row["workspace_owned"] or 0),
        "Worker session": row["worker_session_id"],
        "Reviewer session": row["reviewer_session_id"],
        "PR URL": row["pr_url"],
        "PR head": row["pr_head_oid"],
        "PR conflict recovery": int(row["pr_conflict_recovery"] or 0),
        "PR conflict head": row["pr_conflict_head_oid"],
        "PR conflict base": row["pr_conflict_base_oid"],
    }
    owned = [
        name
        for name, value in ownership.items()
        if value is not None and value != 0
    ]
    if str(row["status"]) != "READY" or owned:
        detail = ", ".join(owned) if owned else str(row["status"])
        raise MaintenancePlanningError(
            f"유지보수 개정 대상은 무소유 READY 계획이어야 합니다: {plan_id} ({detail})"
        )
    plan_sha = str(row["current_revision_sha"] or row["plan_sha"] or "")
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", plan_sha) is None:
        raise MaintenancePlanningError(
            f"계획 revision SHA를 확인할 수 없습니다: {plan_id}"
        )
    content_hash = hashlib.sha256(contents).hexdigest()
    if content_hash != str(row["content_hash"]):
        raise MaintenancePlanningError(
            f"READY ExecPlan content hash가 catalog와 다릅니다: {path}"
        )
    try:
        text = contents.decode("utf-8")
        plan = published_execplan(path, text)
    except (RuntimeError, UnicodeError) as error:
        raise MaintenancePlanningError(str(error)) from error
    sources = _stored_string_list(row["sources"], "sources")
    dependencies = _stored_string_list(row["depends_on"], "depends_on")
    if (
        re.fullmatch(r"G-\d{3,}", str(plan["id"])) is None
        or str(plan["id"]) != plan_id
        or str(plan["title"]) != str(row["title"])
        or tuple(str(item) for item in plan["sources"]) != sources
        or tuple(str(item) for item in plan["depends_on"]) != dependencies
        or str(plan["priority"]) != str(row["priority"])
    ):
        raise MaintenancePlanningError(
            f"READY ExecPlan metadata가 catalog와 다릅니다: {path}"
        )
    return ReadyPlanSnapshot(
        plan_id=plan_id,
        path=path,
        title=str(plan["title"]),
        sources=sources,
        depends_on=dependencies,
        complexity=str(plan["complexity"]),
        risk=str(plan["risk"]),
        priority=str(plan["priority"]),
        content_hash=content_hash,
        plan_sha=plan_sha,
        text=text,
        current_complexity=row["current_complexity"],
        current_risk=row["current_risk"],
        current_metadata_recorded=True,
    )


def _assert_ready_plan_revision(
    repository: Path,
    active_revision: str,
    snapshot: ReadyPlanSnapshot,
    contents: bytes,
) -> None:
    """Bind a READY row to the Git revision that published its exact body."""

    try:
        is_ancestor = _git_commit_is_ancestor(
            repository, snapshot.plan_sha, active_revision
        )
        revision_contents = git_bytes(
            repository, "show", f"{snapshot.plan_sha}:{snapshot.path}"
        )
    except RuntimeError as error:
        raise MaintenancePlanningError(
            "READY ExecPlan plan revision SHA를 확인할 수 없습니다: "
            f"{snapshot.plan_id}"
        ) from error
    if not is_ancestor or revision_contents != contents:
        raise MaintenancePlanningError(
            "READY ExecPlan plan revision SHA가 현재 catalog 본문과 "
            f"일치하지 않습니다: {snapshot.plan_id}"
        )


def _git_path_matches_revision(
    worktree: Path,
    revision: str,
    path: str,
    *,
    cached: bool,
) -> bool:
    """Compare a Git path through its worktree or index checkout filters."""

    arguments = ["diff", "--quiet", "--no-ext-diff"]
    if cached:
        arguments.append("--cached")
    arguments.extend((revision, "--", path))
    result = git(worktree, *arguments, check=False)
    if result.returncode == 1:
        return False
    if result.returncode:
        raise RuntimeError(result.stdout[-6000:].strip())
    return True


def _git_path_has_standard_index_flags(worktree: Path, path: str) -> bool:
    """Require a normally tracked index entry before trusting Git diff output."""

    entries = git_bytes(worktree, "ls-files", "-v", "-z", "--", path).split(
        b"\0"
    )
    records = [entry for entry in entries if entry]
    return records == [b"H " + os.fsencode(path)]


def _ready_plan_snapshot(
    worktree: Path,
    active_sha: str,
    row: sqlite3.Row,
) -> ReadyPlanSnapshot:
    path = str(row["path"])
    target = worktree / path
    if not target.is_file():
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일을 확인할 수 없습니다: {path}"
        )
    try:
        base_contents = git_bytes(worktree, "show", f"{active_sha}:{path}")
        standard_index_flags = _git_path_has_standard_index_flags(worktree, path)
        checkout_matches = _git_path_matches_revision(
            worktree, active_sha, path, cached=False
        )
    except RuntimeError as error:
        raise MaintenancePlanningError(
            f"평가 기준 revision에서 READY ExecPlan을 확인할 수 없습니다: {path}"
        ) from error
    if not standard_index_flags or not checkout_matches:
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일이 평가 기준 revision과 다릅니다: {path}"
        )
    snapshot = _ready_plan_snapshot_from_contents(row, base_contents)
    _assert_ready_plan_revision(worktree, active_sha, snapshot, base_contents)
    try:
        index_matches = _git_path_matches_revision(
            worktree, active_sha, path, cached=True
        )
    except RuntimeError as error:
        raise MaintenancePlanningError(
            f"평가 기준 revision에서 READY ExecPlan을 확인할 수 없습니다: {path}"
        ) from error
    if not index_matches:
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일이 평가 기준 revision과 다릅니다: {path}"
        )
    return snapshot


def _ready_plan_snapshot_at_revision(
    repository: Path,
    revision: str,
    row: sqlite3.Row,
) -> ReadyPlanSnapshot:
    path = str(row["path"])
    try:
        contents = git_bytes(repository, "show", f"{revision}:{path}")
    except RuntimeError as error:
        raise MaintenancePlanningError(
            f"보존된 publication 기준에서 READY ExecPlan을 확인할 수 없습니다: {path}"
        ) from error
    snapshot = _ready_plan_snapshot_from_contents(row, contents)
    _assert_ready_plan_revision(repository, revision, snapshot, contents)
    return snapshot


_MUTABLE_READY_EVIDENCE_FIELDS = (
    "active_sha",
    "validated_base_sha",
    "publication_remote_oid",
    "branch",
    "worktree",
    "pr_url",
    "pr_head_oid",
    "last_error",
    "blocked_fingerprint",
    "blocked_worktree_fingerprint",
    "blocked_review_fingerprint",
    "worker_session_id",
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
    "blocked_reason",
    "guidance_plan_hash",
    "agent_role",
    "agent_snapshot",
    "agent_input_fingerprint",
    "agent_baseline",
    "agent_recovery",
    "agent_input_plan",
    "agent_result_plan",
    "agent_result_snapshot",
    "agent_manager_snapshot",
)


def _row_is_mutable_ready(row: sqlite3.Row) -> bool:
    """Keep the general Planner boundary conservative and evidence based."""

    if str(row["status"]) != "READY" or bool(row["ever_started"]):
        return False
    if any(row[field] is not None for field in _MUTABLE_READY_EVIDENCE_FIELDS):
        return False
    if any(
        int(row[field] or 0) != 0
        for field in (
            "workspace_owned",
            "retry_count",
            "pr_conflict_recovery",
        )
    ):
        return False
    return all(
        str(row[field] or "{}") == "{}"
        for field in (
            "verify_failure_history",
            "review_failure_history",
            "review_level_history",
        )
    )


def mutable_ready_snapshot(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    active_sha: str,
) -> MutableReadySnapshot:
    """Bind every never-started READY declaration to Git and SQLite bytes."""

    _ensure_execplans_schema(connection)
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        raise PlannerValidationError(
            "활성 maintenance cycle 중에는 일반 mutable READY snapshot을 만들 수 없습니다."
        )
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY' "
        "ORDER BY plan_id",
        (repo,),
    ).fetchall()
    plans: list[ReadyPlanSnapshot] = []
    for row in rows:
        if not _row_is_mutable_ready(row):
            continue
        try:
            plans.append(
                _ready_plan_snapshot_at_revision(
                    worktree, active_sha, row
                )
            )
        except MaintenancePlanningError as error:
            raise PlannerValidationError(str(error)) from error
    ordered = tuple(sorted(plans, key=lambda item: int(item.plan_id[2:])))
    return MutableReadySnapshot(
        repo=repo,
        active_sha=active_sha,
        plans=ordered,
        token=mutable_snapshot_token(repo, active_sha, ordered),
    )


def _assert_mutable_ready_snapshot_current(
    connection: sqlite3.Connection,
    snapshot: MutableReadySnapshot,
) -> None:
    """Compare-and-snapshot guard used immediately before publication."""

    if maintenance_state.MaintenanceRepository(connection).get(
        snapshot.repo
    ).phase is not maintenance_state.MaintenancePhase.IDLE:
        raise PlannerValidationError(
            "mutable READY snapshot 이후 maintenance cycle이 시작됐습니다."
        )
    for plan in snapshot.plans:
        if not plan.current_metadata_recorded:
            raise PlannerValidationError(
                f"mutable READY snapshot의 SQLite metadata 근거가 없습니다: {plan.plan_id}"
            )
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (snapshot.repo, plan.plan_id),
        ).fetchone()
        if row is None or not _row_is_mutable_ready(row):
            raise PlannerValidationError(
                f"mutable READY snapshot 이후 실행 상태가 변경됐습니다: {plan.plan_id}"
            )
        sources, dependencies, priority = _execplan_metadata(
            {
                "sources": list(plan.sources),
                "depends_on": list(plan.depends_on),
                "priority": plan.priority,
            }
        )
        current = (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            row["current_complexity"],
            row["current_risk"],
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
        )
        expected = (
            plan.path,
            plan.title,
            sources,
            dependencies,
            priority,
            plan.current_complexity,
            plan.current_risk,
            plan.content_hash,
            plan.plan_sha,
        )
        if current != expected:
            raise PlannerValidationError(
                f"mutable READY snapshot metadata가 변경됐습니다: {plan.plan_id}"
            )


def _maintenance_mode_and_rows(
    connection: sqlite3.Connection,
    repo: str,
    cycle: maintenance_state.MaintenanceCycle,
    active_sha: str,
) -> tuple[MaintenancePlanMode, list[sqlite3.Row]]:
    if cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
        if cycle.cycle_base_sha != active_sha:
            raise MaintenancePlanningError(
                "유지보수 assessment 기준 SHA가 planning worktree 기준과 "
                "일치하지 않습니다."
            )
        rows = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
        return MaintenancePlanMode.BARRIER, list(rows)
    if cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
        rows = []
        for plan_id in cycle.revalidation_plan_ids:
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
                "WHERE h.repo = exec_plans.repo "
                "AND h.currently_archived = 1 "
                "AND h.plan_id = exec_plans.plan_id)",
                (repo, plan_id),
            ).fetchone()
            if row is None:
                raise MaintenancePlanningError(
                    f"재검증 대상 계획이 catalog에 없습니다: {plan_id}"
                )
            rows.append(row)
        return MaintenancePlanMode.REVALIDATION, rows
    raise MaintenancePlanningError(
        "활성 유지보수 assessment 또는 post-refactor revalidation 단계가 "
        "없어 기존 ExecPlan 수정 권한을 열 수 없습니다."
    )


def _maintenance_context(
    repo: str,
    active_sha: str,
    cycle: maintenance_state.MaintenanceCycle,
    mode: MaintenancePlanMode,
    snapshots: tuple[ReadyPlanSnapshot, ...],
) -> MaintenancePlanningContext:
    return MaintenancePlanningContext(
        repo=repo,
        mode=mode,
        active_sha=active_sha,
        cycle_revision=cycle.revision,
        cycle_phase=cycle.phase.value,
        eligible_plans=snapshots,
    )


def prepare_maintenance_planning_context(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    active_sha: str,
) -> MaintenancePlanningContext:
    """Bind guarded Planner authority to one active SQLite cycle revision."""

    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    mode, rows = _maintenance_mode_and_rows(
        connection, repo, cycle, active_sha
    )
    snapshots = tuple(
        _ready_plan_snapshot(worktree, active_sha, row) for row in rows
    )
    return _maintenance_context(repo, active_sha, cycle, mode, snapshots)


def _maintenance_context_at_revision(
    connection: sqlite3.Connection,
    repo: str,
    repository: Path,
    active_sha: str,
    cycle: maintenance_state.MaintenanceCycle,
) -> MaintenancePlanningContext:
    mode, rows = _maintenance_mode_and_rows(
        connection, repo, cycle, active_sha
    )
    snapshots = tuple(
        _ready_plan_snapshot_at_revision(repository, active_sha, row)
        for row in rows
    )
    return _maintenance_context(repo, active_sha, cycle, mode, snapshots)


def _assert_maintenance_context_current(
    connection: sqlite3.Connection,
    context: MaintenancePlanningContext,
) -> None:
    cycle = maintenance_state.MaintenanceRepository(connection).get(context.repo)
    if cycle.phase.value != context.cycle_phase or cycle.revision != context.cycle_revision:
        raise MaintenancePlanningError(
            "유지보수 주기 단계 또는 revision이 Planner 실행 뒤 변경됐습니다."
        )
    if cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
        expected_mode = MaintenancePlanMode.BARRIER
        expected_plan_ids = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT plan_id FROM exec_plans "
                "WHERE repo = ? AND status = 'READY' "
                "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
                "WHERE h.repo = exec_plans.repo "
                "AND h.currently_archived = 1 "
                "AND h.plan_id = exec_plans.plan_id) ORDER BY plan_id",
                (context.repo,),
            )
        )
    elif cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
        expected_mode = MaintenancePlanMode.REVALIDATION
        expected_plan_ids = cycle.revalidation_plan_ids
    else:
        raise MaintenancePlanningError(
            "현재 유지보수 단계에는 기존 ExecPlan 수정 권한이 없습니다."
        )
    context_plan_ids = tuple(plan.plan_id for plan in context.eligible_plans)
    if context.mode is not expected_mode or context_plan_ids != expected_plan_ids:
        raise MaintenancePlanningError(
            "maintenance mode 또는 eligible READY snapshot이 SQLite 단계와 "
            "일치하지 않습니다."
        )
    for snapshot in context.eligible_plans:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (context.repo, snapshot.plan_id),
        ).fetchone()
        if row is None:
            raise MaintenancePlanningError(
                f"READY snapshot 계획이 catalog에서 사라졌습니다: {snapshot.plan_id}"
            )
        ownership_values = (
            row["branch"],
            row["worktree"],
            int(row["workspace_owned"] or 0),
            row["worker_session_id"],
            row["reviewer_session_id"],
            row["pr_url"],
            row["pr_head_oid"],
            int(row["pr_conflict_recovery"] or 0),
            row["pr_conflict_head_oid"],
            row["pr_conflict_base_oid"],
        )
        current = (
            str(row["status"]),
            str(row["path"]),
            str(row["title"]),
            _stored_string_list(row["sources"], "sources"),
            _stored_string_list(row["depends_on"], "depends_on"),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            ownership_values,
        )
        expected = (
            "READY",
            snapshot.path,
            snapshot.title,
            snapshot.sources,
            snapshot.depends_on,
            snapshot.priority,
            snapshot.content_hash,
            snapshot.plan_sha,
            (None, None, 0, None, None, None, None, 0, None, None),
        )
        if current != expected:
            raise MaintenancePlanningError(
                "READY snapshot의 상태, metadata 또는 소유권이 변경됐습니다: "
                f"{snapshot.plan_id}"
            )


def validate_maintenance_planner_result(
    connection: sqlite3.Connection,
    context: MaintenancePlanningContext,
    worktree: Path,
    dirty_files: list[str],
    *,
    refactoring_plan_ids: tuple[str, ...] | None = None,
    allow_general_plans: bool = False,
) -> ValidatedPlanningResult | None:
    """Validate output without consuming correction retries on Git failures."""

    # Read Git outside the output-validation boundary. A tree query distinguishes
    # absent paths from an unreadable repository without interpreting cat-file's
    # nonzero status as proof that a path is new.
    planned_files = sorted(
        set(dirty_files) | set(changed_files(worktree, context.active_sha))
    )
    base_paths = _git_tree_entries(worktree, context.active_sha)
    path_moves_by_sha: dict[str, dict[str, str]] = {}
    if context.mode is MaintenancePlanMode.REVALIDATION:
        for snapshot in context.eligible_plans:
            if snapshot.path not in planned_files:
                continue
            if snapshot.plan_sha not in path_moves_by_sha:
                tree = _git_tree_entries(worktree, snapshot.plan_sha)
                # Only unique, byte-identical deleted -> added files establish
                # a move. Similarity, copies and ambiguous replacements cannot
                # prove preservation of the original acceptance criterion.
                old_blobs: dict[bytes, list[bytes]] = {}
                new_blobs: dict[bytes, list[bytes]] = {}
                for entries, grouped in ((tree, old_blobs), (base_paths, new_blobs)):
                    for name, (kind, oid) in entries.items():
                        if kind == b"blob":
                            grouped.setdefault(oid, []).append(name)
                moves = {}
                for oid, old_names in old_blobs.items():
                    new_names = new_blobs.get(oid, [])
                    if len(old_names) == len(new_names) == 1:
                        old, new = old_names[0], new_names[0]
                        if old not in base_paths and new not in tree:
                            moves[old.decode("utf-8")] = new.decode("utf-8")
                path_moves_by_sha[snapshot.plan_sha] = moves
    try:
        _assert_maintenance_context_current(connection, context)
        if not planned_files:
            return None
        eligible = context.eligible_by_path
        planned_contents: dict[str, tuple[bytes, str]] = {}
        new_paths: list[str] = []
        revised_paths: list[str] = []
        for path in planned_files:
            if path == "docs/FUTURE_IDEAS.md" and allow_general_plans:
                if not (worktree / path).is_file():
                    raise MaintenancePlanningError(
                        "Planner는 docs/FUTURE_IDEAS.md를 삭제할 수 없습니다."
                    )
                planned_contents[path] = ((worktree / path).read_bytes(), "")
                continue
            if not re.fullmatch(r"docs/plans/[^/]+\.md", path):
                raise MaintenancePlanningError(
                    "유지보수 Planner는 검증된 docs/plans batch만 변경할 수 있습니다."
                )
            if not (worktree / path).is_file():
                raise MaintenancePlanningError(
                    f"유지보수 Planner는 ExecPlan을 삭제·rename할 수 없습니다: {path}"
                )
            contents = (worktree / path).read_bytes()
            text = contents.decode("utf-8")
            planned_contents[path] = (contents, text)
            if contains_guidance_sections(text):
                raise MaintenancePlanningError(
                    "유지보수 Planner는 AGENTS.md guidance를 제안할 수 없습니다."
                )
            existed = path.encode("utf-8") in base_paths
            if existed:
                if path not in eligible:
                    raise MaintenancePlanningError(
                        f"SQLite snapshot에 없는 기존 ExecPlan은 수정할 수 없습니다: {path}"
                    )
                revised_paths.append(path)
            else:
                if context.mode is not MaintenancePlanMode.BARRIER:
                    raise MaintenancePlanningError(
                        "post-refactor revalidation은 새 ExecPlan을 만들 수 없습니다."
                    )
                new_paths.append(path)

        try:
            catalog_snapshot = load_execplan_snapshot(
                worktree, validate_sources=False
            )
        except RuntimeError as error:
            raise MaintenancePlanningError(str(error)) from error
        catalog = catalog_snapshot.active_metadata()
        new_plans: list[tuple[str, dict[str, object]]] = []
        for path in new_paths:
            try:
                plan = _parse_execplan(
                    worktree,
                    worktree / path,
                    require_priority=True,
                    allow_archive_fallback=False,
                )
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            try:
                validated_plan = published_execplan(path, planned_contents[path][1])
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            if validated_plan != plan:
                raise MaintenancePlanningError(
                    f"검증 중 maintenance ExecPlan이 변경됐습니다: {path}"
                )
            new_plans.append((path, validated_plan))
        new_ids = {str(plan["id"]) for _, plan in new_plans}
        declared_refactoring = (
            new_ids
            if refactoring_plan_ids is None
            else set(refactoring_plan_ids)
        )
        if not declared_refactoring <= new_ids:
            unknown = sorted(declared_refactoring - new_ids)
            raise MaintenancePlanningError(
                "MAINTENANCE_RESULT가 생성되지 않은 refactoring plan을 참조합니다: "
                + ", ".join(unknown)
            )
        general_ids = new_ids - declared_refactoring
        if general_ids and not allow_general_plans:
            raise MaintenancePlanningError(
                "maintenance-only 호출은 일반 기능 ExecPlan을 만들 수 없습니다: "
                + ", ".join(sorted(general_ids))
            )
        for path, plan in new_plans:
            if str(plan["id"]) in declared_refactoring:
                validate_refactoring_plan(path, plan, planned_contents[path][1])
        if allow_general_plans:
            _validate_future_idea_separation(worktree, context.active_sha, new_paths)

        existing_ids = [
            int(plan_id[2:])
            for plan_id in catalog_snapshot.all
            if plan_id not in new_ids
        ]
        first_id = max(existing_ids, default=0) + 1
        expected_ids = list(range(first_id, first_id + len(new_plans)))
        actual_ids = sorted(int(str(plan["id"])[2:]) for _, plan in new_plans)
        if actual_ids != expected_ids:
            raise MaintenancePlanningError(
                "새 리팩터링 ExecPlan ID는 기존 최대 번호 다음부터 연속적이어야 합니다."
            )

        refactoring_plans = [
            item for item in new_plans
            if str(item[1]["id"]) in declared_refactoring
        ]
        terminals = terminal_new_plan_ids(refactoring_plans)
        if context.mode is MaintenancePlanMode.BARRIER:
            if refactoring_plans:
                if set(revised_paths) != set(eligible):
                    missing = sorted(set(eligible) - set(revised_paths))
                    raise MaintenancePlanningError(
                        "barrier mode는 모든 eligible READY 계획에 말단 dependency를 "
                        f"추가해야 합니다: {', '.join(missing)}"
                    )
                for _path, plan in new_plans:
                    if str(plan["id"]) in general_ids and not set(terminals) <= set(plan["depends_on"]):
                        raise MaintenancePlanningError(
                            "combined 일반 계획은 리팩터링 DAG 말단 뒤에 있어야 합니다: "
                            + str(plan["id"])
                        )
                statuses = {
                    str(row["plan_id"]): str(row["status"])
                    for row in connection.execute(
                        "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                        (context.repo,),
                    )
                }
                for _path, plan in refactoring_plans:
                    for dependency in plan["depends_on"]:
                        if dependency not in declared_refactoring and statuses.get(str(dependency)) != "MERGED":
                            raise MaintenancePlanningError(
                                "리팩터링 DAG의 외부 dependency는 이미 MERGED여야 합니다: "
                                f"{plan['id']} -> {dependency}"
                            )
            elif revised_paths:
                raise MaintenancePlanningError(
                    "리팩터링 계획이 없는 assessment는 READY 장벽을 수정할 수 없습니다."
                )

        revisions: list[PlanRevision] = []
        for path in revised_paths:
            snapshot = eligible[path]
            plan = catalog[snapshot.plan_id]
            text = planned_contents[path][1]
            try:
                validated_plan = published_execplan(path, text)
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            if validated_plan != plan:
                raise MaintenancePlanningError(
                    f"검증 중 maintenance ExecPlan이 변경됐습니다: {path}"
                )
            if context.mode is MaintenancePlanMode.BARRIER:
                validate_barrier_revision(
                    snapshot, path, validated_plan, text, terminals
                )
            else:
                validate_revalidation_revision(
                    snapshot, path, validated_plan, text,
                    path_moves=path_moves_by_sha[snapshot.plan_sha],
                )
            revisions.append(PlanRevision(path, validated_plan, snapshot))

        return ValidatedPlanningResult(
            plans=tuple(
                sorted(new_plans, key=lambda item: int(str(item[1]["id"])[2:]))
            ),
            publication_paths=tuple(planned_files),
            publication_hashes=tuple(
                (path, hashlib.sha256(planned_contents[path][0]).hexdigest())
                for path in planned_files
            ),
            revisions=tuple(
                sorted(revisions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            maintenance_context=context,
            refactoring_plan_ids=tuple(
                sorted(declared_refactoring, key=lambda value: int(value[2:]))
            ),
            revalidation_plan_ids=(
                tuple(
                    sorted(
                        {revision.snapshot.plan_id for revision in revisions}
                        | general_ids,
                        key=lambda value: int(value[2:]),
                    )
                ) if refactoring_plans else ()
            ),
        )
    except MaintenancePlanningError as error:
        raise PlannerValidationError(str(error)) from error
    except (OSError, UnicodeError) as error:
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


def _register_execplan_rows(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    plan_commit: str,
) -> None:
    """Apply the new-row part shared by normal and maintenance batches."""

    _ensure_execplans_schema(connection)
    for path, plan in plans:
        plan_id = str(plan["id"])
        sources, dependencies, priority = _execplan_metadata(plan)
        content_hash = file_hash(worktree / path)
        initial_level = worker_level(plan)
        historical = connection.execute(
            "SELECT plan_id FROM execplan_catalog_history "
            "WHERE repo = ? AND (plan_id = ? OR original_path = ?)",
            (repo, plan_id, path),
        ).fetchone()
        if historical is not None:
            raise RuntimeError(
                "게시된 ExecPlan이 archived historical identity와 충돌합니다: "
                f"{plan_id}"
            )
        existing = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND (plan_id = ? OR path = ?)",
            (repo, plan_id, path),
        ).fetchone()
        if existing:
            immutable = (
                existing["plan_id"], existing["path"], existing["title"],
                existing["sources"], existing["depends_on"], existing["priority"],
                existing["content_hash"], existing["plan_sha"],
            )
            expected = (
                plan_id, path, str(plan["title"]), sources, dependencies,
                priority, content_hash, plan_commit,
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
        execplan_catalog_state.insert_published_plan_row(
            connection, repo, path, plan, content_hash, plan_commit,
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
        _register_execplan_rows(connection, repo, worktree, plans, plan_commit)
        runtime_state.RuntimeStateRepository(connection).clear(
            repo,
            processed_sha=plan_commit,
            planning_fingerprint=planning_fingerprint,
            enqueue=False,
        )


@dataclass(frozen=True)
class GeneralPlanningPublication:
    repo: str
    stage: str
    base_sha: str
    commit_sha: str
    snapshot_token: str
    specification_fingerprint: str
    planning_fingerprint: str
    manifest: str
    manifest_hash: str


def _planning_result_manifest(result: ValidatedPlanningResult) -> str:
    snapshot = result.mutable_snapshot
    if snapshot is None:
        raise PlannerValidationError(
            "일반 planning batch에 mutable READY snapshot이 없습니다."
        )
    payload = {
        "version": 1,
        "publication_paths": list(result.publication_paths),
        "publication_hashes": dict(result.publication_hashes),
        "plans": [[path, plan] for path, plan in result.plans],
        "revisions": [
            {
                "path": revision.path,
                "plan": dict(revision.plan),
                "snapshot": snapshot_to_dict(revision.snapshot),
            }
            for revision in result.revisions
        ],
        "deletions": [
            snapshot_to_dict(deletion.snapshot)
            for deletion in result.deletions
        ],
        "mutable_snapshot": {
            "repo": snapshot.repo,
            "active_sha": snapshot.active_sha,
            "token": snapshot.token,
            "plans": [snapshot_to_dict(plan) for plan in snapshot.plans],
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _planning_result_from_manifest(manifest: str) -> ValidatedPlanningResult:
    try:
        payload = json.loads(manifest)
        if payload.get("version") != 1:
            raise ValueError("unsupported version")
        snapshot_value = payload["mutable_snapshot"]
        snapshot_plans = tuple(
            snapshot_from_dict(item) for item in snapshot_value["plans"]
        )
        snapshot = MutableReadySnapshot(
            repo=str(snapshot_value["repo"]),
            active_sha=str(snapshot_value["active_sha"]),
            plans=snapshot_plans,
            token=str(snapshot_value["token"]),
        )
        expected_token = mutable_snapshot_token(
            snapshot.repo, snapshot.active_sha, snapshot.plans
        )
        if snapshot.token != expected_token:
            raise ValueError("snapshot token mismatch")
        plans = tuple(
            (str(item[0]), dict(item[1])) for item in payload["plans"]
        )
        revisions = tuple(
            PlanRevision(
                str(item["path"]),
                dict(item["plan"]),
                snapshot_from_dict(item["snapshot"]),
            )
            for item in payload["revisions"]
        )
        deletions = tuple(
            PlanDeletion(snapshot_from_dict(item))
            for item in payload["deletions"]
        )
        publication_hashes = tuple(
            sorted(
                (str(path), str(value))
                for path, value in payload["publication_hashes"].items()
            )
        )
        return ValidatedPlanningResult(
            plans=plans,
            publication_paths=tuple(
                str(path) for path in payload["publication_paths"]
            ),
            publication_hashes=publication_hashes,
            revisions=revisions,
            deletions=deletions,
            mutable_snapshot=snapshot,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PlannerValidationError(
            "보존된 general planning publication manifest가 유효하지 않습니다."
        ) from error


def general_planning_publication(
    connection: sqlite3.Connection, repo: str
) -> GeneralPlanningPublication | None:
    row = connection.execute(
        "SELECT * FROM general_planning_publications WHERE repo = ?", (repo,)
    ).fetchone()
    if row is None:
        return None
    publication = GeneralPlanningPublication(
        repo=str(row["repo"]),
        stage=str(row["stage"]),
        base_sha=str(row["base_sha"]),
        commit_sha=str(row["commit_sha"]),
        snapshot_token=str(row["snapshot_token"]),
        specification_fingerprint=str(row["specification_fingerprint"]),
        planning_fingerprint=str(row["planning_fingerprint"]),
        manifest=str(row["manifest"]),
        manifest_hash=str(row["manifest_hash"]),
    )
    if hashlib.sha256(publication.manifest.encode("utf-8")).hexdigest() != (
        publication.manifest_hash
    ):
        raise PlannerValidationError(
            "보존된 general planning publication manifest hash가 다릅니다."
        )
    return publication


def _validate_general_planning_commit(
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> None:
    snapshot = result.mutable_snapshot
    if snapshot is None:
        raise PlannerValidationError("일반 planning snapshot이 없습니다.")
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if head != plan_commit:
        raise PlannerValidationError(
            "general planning worktree HEAD가 보존할 commit과 다릅니다."
        )
    ancestry = git(
        worktree, "rev-list", "--parents", "-n", "1", plan_commit
    ).stdout.split()
    if len(ancestry) != 2 or ancestry[1] != snapshot.active_sha:
        raise PlannerValidationError(
            "general planning batch는 시작 기준 바로 다음 단일 commit이어야 합니다."
        )
    committed_paths = tuple(
        sorted(
            path
            for path in git(
                worktree,
                "diff",
                "--no-renames",
                "--name-only",
                snapshot.active_sha,
                plan_commit,
            ).stdout.splitlines()
            if path
        )
    )
    if committed_paths != tuple(sorted(result.publication_paths)):
        raise PlannerValidationError(
            "general planning commit에 검증 batch 밖의 변경이 있습니다."
        )
    hashes = dict(result.publication_hashes)
    if len(hashes) != len(result.publication_hashes):
        raise PlannerValidationError("general planning file hash가 중복됐습니다.")
    expected_existing = {
        path
        for path in result.publication_paths
        if git(
            worktree,
            "cat-file",
            "-e",
            f"{plan_commit}:{path}",
            check=False,
        ).returncode
        == 0
    }
    if set(hashes) != expected_existing:
        raise PlannerValidationError(
            "general planning 결과의 file hash 또는 삭제 manifest가 불완전합니다."
        )
    for path, expected_hash in hashes.items():
        contents = git_bytes(worktree, "show", f"{plan_commit}:{path}")
        if hashlib.sha256(contents).hexdigest() != expected_hash:
            raise PlannerValidationError(
                f"general planning 파일이 검증 후 변경됐습니다: {path}"
            )
    for path in set(result.publication_paths) - expected_existing:
        if git(
            worktree, "cat-file", "-e", f"{plan_commit}:{path}", check=False
        ).returncode == 0:
            raise PlannerValidationError(
                f"삭제 manifest 경로가 planning commit에 남아 있습니다: {path}"
            )


def record_general_planning_publication_intent(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> GeneralPlanningPublication:
    snapshot = result.mutable_snapshot
    if snapshot is None or snapshot.repo != repo:
        raise PlannerValidationError(
            "SQLite-bound mutable READY snapshot이 publication 대상과 다릅니다."
        )
    _assert_mutable_ready_snapshot_current(connection, snapshot)
    _validate_general_planning_commit(worktree, result, plan_commit)
    if is_loop_manager_repository(Path(repo)):
        import document_drafts
        project = connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if project is None:
            raise PlannerValidationError("문서 계획의 등록 프로젝트가 없습니다.")
        drafts = document_drafts.DocumentDraftRepository(connection)
        try:
            for path, plan in result.plans:
                plan_text = document_publication.read_plan_text(worktree / path)
                claim = document_publication.plan_claim(plan_text)
                if claim is not None:
                    try:
                        draft = drafts.get(claim)
                        document_publication.validate_document_plan(plan_text, plan, draft)
                        drafts.assert_current(draft, Path(repo), str(project["base_branch"]))
                    except document_drafts.DocumentDraftError as error:
                        drafts.record_publication_error(claim, str(error))
                        raise
        except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
            raise PlannerValidationError(str(error)) from error
    manifest = _planning_result_manifest(result)
    manifest_hash = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    expected = (
        "prepared",
        snapshot.active_sha,
        plan_commit,
        snapshot.token,
        specification_fingerprint,
        planning_fingerprint,
        manifest_hash,
    )
    with connection:
        current = general_planning_publication(connection, repo)
        if current is not None:
            actual = (
                current.stage,
                current.base_sha,
                current.commit_sha,
                current.snapshot_token,
                current.specification_fingerprint,
                current.planning_fingerprint,
                current.manifest_hash,
            )
            if actual != expected:
                raise PlannerValidationError(
                    "다른 general planning publication intent가 이미 있습니다."
                )
            return current
        connection.execute(
            """
            INSERT INTO general_planning_publications (
                repo, stage, base_sha, commit_sha, snapshot_token,
                specification_fingerprint, planning_fingerprint,
                manifest, manifest_hash
            ) VALUES (?, 'prepared', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo,
                snapshot.active_sha,
                plan_commit,
                snapshot.token,
                specification_fingerprint,
                planning_fingerprint,
                manifest,
                manifest_hash,
            ),
        )
    publication = general_planning_publication(connection, repo)
    assert publication is not None
    return publication


def _mutable_ready_cas_where() -> str:
    null_fields = " AND ".join(
        f"{field} IS NULL" for field in _MUTABLE_READY_EVIDENCE_FIELDS
    )
    return (
        "status = 'READY' AND ever_started = 0 "
        "AND workspace_owned = 0 AND retry_count = 0 "
        "AND pr_conflict_recovery = 0 "
        "AND verify_failure_history = '{}' "
        "AND review_failure_history = '{}' "
        "AND review_level_history = '{}' AND "
        + null_fields
    )


def _general_planning_batch_already_applied(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> bool:
    project = connection.execute(
        "SELECT processed_sha, specification_fingerprint, planning_fingerprint "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or tuple(project) != (
        plan_commit,
        specification_fingerprint,
        planning_fingerprint,
    ):
        return False
    for path, plan in result.plans:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, str(plan["id"])),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(plan)
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
        ) != (
            path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            file_hash(worktree / path),
            plan_commit,
        ):
            return False
    for revision in result.revisions:
        plan = dict(revision.plan)
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, revision.snapshot.plan_id),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(plan)
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
            bool(row["ever_started"]),
        ) != (
            revision.path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            file_hash(worktree / revision.path),
            plan_commit,
            "READY",
            False,
        ):
            return False
    for deletion in result.deletions:
        original = deletion.snapshot
        row = connection.execute(
            "SELECT * FROM execplan_cancellations "
            "WHERE repo = ? AND plan_id = ?",
            (repo, original.plan_id),
        ).fetchone()
        if row is None or (
            str(row["path"]),
            str(row["content_hash"]),
            str(row["plan_sha"]),
            str(row["deletion_commit"]),
        ) != (
            original.path,
            original.content_hash,
            original.plan_sha,
            plan_commit,
        ):
            return False
        if connection.execute(
            "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, original.plan_id),
        ).fetchone() is not None:
            return False
    return True


def _apply_general_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> None:
    snapshot = result.mutable_snapshot
    if snapshot is None or snapshot.repo != repo:
        raise PlannerValidationError("general planning snapshot 대상이 다릅니다.")
    with runtime_state._transaction(connection):
        intent = general_planning_publication(connection, repo)
        if intent is None:
            if _general_planning_batch_already_applied(
                connection,
                repo,
                worktree,
                result,
                plan_commit,
                specification_fingerprint,
                planning_fingerprint,
            ):
                return
            raise PlannerValidationError(
                "보존된 general planning publication intent가 없습니다."
            )
        if (
            intent.stage,
            intent.base_sha,
            intent.commit_sha,
            intent.snapshot_token,
            intent.specification_fingerprint,
            intent.planning_fingerprint,
        ) != (
            "prepared",
            snapshot.active_sha,
            plan_commit,
            snapshot.token,
            specification_fingerprint,
            planning_fingerprint,
        ):
            raise PlannerValidationError(
                "보존된 general planning publication intent가 batch와 다릅니다."
            )
        _assert_mutable_ready_snapshot_current(connection, snapshot)
        cas = _mutable_ready_cas_where()
        for deletion in result.deletions:
            original = deletion.snapshot
            changed = connection.execute(
                f"""
                DELETE FROM exec_plans
                WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
                  AND sources = ? AND depends_on = ? AND priority = ?
                  AND current_complexity IS ? AND current_risk IS ?
                  AND content_hash = ?
                  AND COALESCE(current_revision_sha, plan_sha) = ? AND {cas}
                """,
                (
                    repo,
                    original.plan_id,
                    original.path,
                    original.title,
                    json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                    original.priority,
                    original.current_complexity,
                    original.current_risk,
                    original.content_hash,
                    original.plan_sha,
                ),
            )
            if changed.rowcount != 1:
                raise PlannerValidationError(
                    "publication 직전 mutable READY deletion이 변경됐습니다: "
                    f"{original.plan_id}"
                )
            connection.execute(
                """
                INSERT INTO execplan_cancellations (
                    repo, plan_id, path, title, sources, depends_on,
                    complexity, risk, priority, content_hash, plan_sha,
                    deletion_commit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    original.plan_id,
                    original.path,
                    original.title,
                    json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                    original.complexity,
                    original.risk,
                    original.priority,
                    original.content_hash,
                    original.plan_sha,
                    plan_commit,
                ),
            )

        staged_paths: dict[str, str] = {}
        for revision in result.revisions:
            original = revision.snapshot
            if revision.path == original.path:
                continue
            staged = (
                f"docs/plans/.planning-{snapshot.token[:12]}-"
                f"{original.plan_id}.md"
            )
            changed = connection.execute(
                f"""
                UPDATE exec_plans SET path = ?
                WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
                  AND sources = ? AND depends_on = ? AND priority = ?
                  AND current_complexity IS ? AND current_risk IS ?
                  AND content_hash = ?
                  AND COALESCE(current_revision_sha, plan_sha) = ? AND {cas}
                """,
                (
                    staged,
                    repo,
                    original.plan_id,
                    original.path,
                    original.title,
                    json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                    original.priority,
                    original.current_complexity,
                    original.current_risk,
                    original.content_hash,
                    original.plan_sha,
                ),
            )
            if changed.rowcount != 1:
                raise PlannerValidationError(
                    "publication 직전 mutable READY rename이 변경됐습니다: "
                    f"{original.plan_id}"
                )
            staged_paths[original.plan_id] = staged

        for revision in result.revisions:
            original = revision.snapshot
            plan = dict(revision.plan)
            sources, dependencies, priority = _execplan_metadata(plan)
            initial_level = worker_level(plan)
            expected_path = staged_paths.get(original.plan_id, original.path)
            changed = connection.execute(
                f"""
                UPDATE exec_plans
                SET path = ?, title = ?, sources = ?, depends_on = ?,
                    priority = ?, content_hash = ?, current_revision_sha = ?,
                    current_complexity = ?, current_risk = ?,
                    initial_worker_level = ?, effective_worker_level = ?
                WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
                  AND sources = ? AND depends_on = ? AND priority = ?
                  AND current_complexity IS ? AND current_risk IS ?
                  AND content_hash = ?
                  AND COALESCE(current_revision_sha, plan_sha) = ? AND {cas}
                """,
                (
                    revision.path,
                    str(plan["title"]),
                    sources,
                    dependencies,
                    priority,
                    file_hash(worktree / revision.path),
                    plan_commit,
                    str(plan["complexity"]),
                    str(plan["risk"]),
                    initial_level,
                    initial_level,
                    repo,
                    original.plan_id,
                    expected_path,
                    original.title,
                    json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                    original.priority,
                    original.current_complexity,
                    original.current_risk,
                    original.content_hash,
                    original.plan_sha,
                ),
            )
            if changed.rowcount != 1:
                raise PlannerValidationError(
                    "publication 직전 mutable READY revision이 변경됐습니다: "
                    f"{original.plan_id}"
                )

        _register_execplan_rows(
            connection, repo, worktree, list(result.plans), plan_commit
        )
        if is_loop_manager_repository(Path(repo)):
            import document_drafts
            drafts = document_drafts.DocumentDraftRepository(connection)
            try:
                for path, plan in result.plans:
                    plan_text = document_publication.read_plan_text(worktree / path)
                    claim = document_publication.plan_claim(plan_text)
                    if claim is not None:
                        draft = drafts.get(claim)
                        document_publication.validate_document_plan(plan_text, plan, draft)
                        drafts.bind_plan(repo, claim, str(plan["id"]))
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                raise PlannerValidationError(str(error)) from error
        _clear_active_in_current_transaction(
            connection,
            repo,
            processed_sha=plan_commit,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )
        connection.execute(
            "DELETE FROM general_planning_publications WHERE repo = ?", (repo,)
        )


def publish_general_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> None:
    remote = git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if remote != plan_commit:
        raise PlannerValidationError(
            "general planning commit의 fast-forward push를 origin/base에서 확인할 수 없습니다."
        )
    _validate_general_planning_commit(worktree, result, plan_commit)
    _apply_general_planning_batch(
        connection,
        repo,
        worktree,
        result,
        plan_commit,
        specification_fingerprint,
        planning_fingerprint,
    )


def recover_general_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> str:
    """Return pending, applied, or restart for one durable general intent."""

    intent = general_planning_publication(connection, repo)
    if intent is None:
        raise PlannerValidationError(
            "복구할 general planning publication intent가 없습니다."
        )
    result = _planning_result_from_manifest(intent.manifest)
    if result.mutable_snapshot is None or (
        result.mutable_snapshot.token != intent.snapshot_token
    ):
        raise PlannerValidationError(
            "publication intent와 mutable READY snapshot token이 다릅니다."
        )
    _validate_general_planning_commit(worktree, result, intent.commit_sha)
    remote = git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if remote == intent.base_sha:
        return "pending"
    if _git_commit_is_ancestor(worktree, intent.commit_sha, remote):
        if remote != intent.commit_sha:
            _assert_remote_preserves_maintenance_batch(
                worktree,
                intent.base_sha,
                intent.commit_sha,
                remote,
                reject_catalog_drift=True,
            )
        _apply_general_planning_batch(
            connection,
            repo,
            worktree,
            result,
            intent.commit_sha,
            intent.specification_fingerprint,
            intent.planning_fingerprint,
        )
        return "applied"
    if not _git_commit_is_ancestor(worktree, intent.base_sha, remote):
        raise PlannerValidationError(
            "general planning publication base가 origin/base 이력에 없습니다."
        )
    with runtime_state._transaction(connection):
        connection.execute(
            "DELETE FROM general_planning_publications WHERE repo = ?", (repo,)
        )
        _clear_active_in_current_transaction(connection, repo)
    return "restart"


def validate_maintenance_publication_base(
    connection: sqlite3.Connection,
    repo: str,
    repository: Path,
    base_branch: str,
    context: MaintenancePlanningContext,
) -> None:
    """Invalidate and restart a stale maintenance result before publication."""

    if context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )

    current = git(
        repository,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if current == context.active_sha:
        return

    cycle_repository = maintenance_state.MaintenanceRepository(connection)
    expected_cycle = cycle_repository.get(repo)
    _assert_maintenance_context_current(connection, context)
    with connection:
        restarted = cycle_repository.restart_stale_planning_context(
            repo,
            expected=expected_cycle,
            new_base_sha=current,
        )
        _clear_active_in_current_transaction(connection, repo)
    raise MaintenancePlanningRestart(current, restarted.revision)


def _validate_maintenance_planning_commit(
    repository: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> None:
    context = result.maintenance_context
    if context is None:
        raise MaintenancePlanningError(
            "일반 Planner 결과를 maintenance publication API에 전달할 수 없습니다."
        )
    head = git(repository, "rev-parse", "HEAD").stdout.strip()
    if head != plan_commit:
        raise MaintenancePlanningError(
            "maintenance planning worktree HEAD가 보존할 commit과 다릅니다."
        )
    ancestry = git(
        repository,
        "rev-list",
        "--parents",
        "-n",
        "1",
        plan_commit,
    ).stdout.split()
    if len(ancestry) != 2 or ancestry[1] != context.active_sha:
        raise MaintenancePlanningError(
            "maintenance batch는 평가 기준 바로 다음의 단일 planning commit이어야 합니다."
        )
    committed_paths = tuple(
        sorted(
            line
            for line in git(
                repository,
                "diff",
                "--name-only",
                context.active_sha,
                plan_commit,
            ).stdout.splitlines()
            if line
        )
    )
    if committed_paths != tuple(sorted(result.publication_paths)):
        raise MaintenancePlanningError(
            "maintenance planning commit에 검증 batch 밖의 변경이 있습니다."
        )
    validated_hashes = dict(result.publication_hashes)
    if (
        len(validated_hashes) != len(result.publication_hashes)
        or set(validated_hashes) != set(result.publication_paths)
    ):
        raise MaintenancePlanningError(
            "maintenance planning 결과의 검증된 file hash가 불완전합니다."
        )
    expected = {
        path: plan for path, plan in result.plans
    }
    expected.update({revision.path: revision.plan for revision in result.revisions})
    load_execplan_catalog(repository, validate_sources=False)
    for path in result.publication_paths:
        local_contents = (repository / path).read_bytes()
        remote_contents = git_bytes(repository, "show", f"{plan_commit}:{path}")
        expected_hash = validated_hashes[path]
        if (
            hashlib.sha256(local_contents).hexdigest() != expected_hash
            or hashlib.sha256(remote_contents).hexdigest() != expected_hash
        ):
            raise MaintenancePlanningError(
                "maintenance ExecPlan이 Planner 결과 검증 후 "
                f"변경됐습니다: {path}"
            )
        if path == "docs/FUTURE_IDEAS.md":
            continue
        parsed = _parse_execplan(repository, repository / path, validate_sources=False)
        if parsed != expected[path]:
            raise MaintenancePlanningError(
                f"검증 뒤 maintenance ExecPlan metadata가 변경됐습니다: {path}"
            )


def _validate_maintenance_publication_commit(
    repository: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    *,
    require_remote_confirmation: bool = True,
) -> None:
    context = result.maintenance_context
    if context is None:
        raise MaintenancePlanningError(
            "일반 Planner 결과를 maintenance publication API에 전달할 수 없습니다."
        )
    remote = git(
        repository,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    expected_remote = (
        plan_commit if require_remote_confirmation else context.active_sha
    )
    if remote != expected_remote:
        detail = "fast-forward push" if require_remote_confirmation else "평가 기준"
        raise MaintenancePlanningError(
            f"maintenance planning commit의 {detail}을 origin/base에서 "
            "확인할 수 없습니다."
        )
    _validate_maintenance_planning_commit(repository, result, plan_commit)


def _git_commit_is_ancestor(
    repository: Path,
    older: str,
    newer: str,
) -> bool:
    result = git(
        repository, "merge-base", "--is-ancestor", older, newer, check=False
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError("maintenance publication Git ancestry 확인 실패.")
    return result.returncode == 0


def _assert_remote_preserves_maintenance_batch(
    repository: Path,
    base_sha: str,
    plan_commit: str,
    remote_sha: str,
    *,
    reject_catalog_drift: bool = False,
) -> None:
    """Confirm a published batch remains byte-identical at a newer remote tip."""

    if not _git_commit_is_ancestor(repository, plan_commit, remote_sha):
        raise MaintenancePlanningError(
            "보존된 maintenance planning commit이 origin/base 이력에 없습니다."
        )
    base_tree = _git_tree_entries(repository, base_sha)
    plan_tree = _git_tree_entries(repository, plan_commit)
    remote_tree = _git_tree_entries(repository, remote_sha)
    batch_paths = {
        path
        for path in base_tree.keys() | plan_tree.keys()
        if base_tree.get(path) != plan_tree.get(path)
    }
    changed_paths = sorted(
        path
        for path in batch_paths
        if plan_tree.get(path) != remote_tree.get(path)
    )
    if changed_paths:
        rendered = ", ".join(
            path.decode("utf-8", errors="replace") for path in changed_paths
        )
        raise MaintenancePlanningError(
            "게시된 maintenance batch 경로가 후속 원격 commit에서 변경됐습니다: "
            f"{rendered}"
        )

    if not reject_catalog_drift:
        return

    plans_prefix = b"docs/plans/"
    catalog_changes = sorted(
        path
        for path in plan_tree.keys() | remote_tree.keys()
        if path.startswith(plans_prefix)
        and path.endswith(b".md")
        and b"/" not in path[len(plans_prefix) :]
        and plan_tree.get(path) != remote_tree.get(path)
    )
    if catalog_changes:
        rendered = ", ".join(
            path.decode("utf-8", errors="replace") for path in catalog_changes
        )
        raise MaintenancePlanningError(
            "게시 뒤 원격 ExecPlan catalog가 후속 commit에서 변경됐습니다: "
            f"{rendered}"
        )


def record_maintenance_publication_intent(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> maintenance_state.MaintenanceCycle:
    """Persist a validated local batch before attempting its remote push."""

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    _assert_maintenance_context_current(connection, context)
    _validate_maintenance_publication_commit(
        worktree,
        base_branch,
        result,
        plan_commit,
        require_remote_confirmation=False,
    )
    expected_phase = (
        maintenance_state.MaintenancePhase.ASSESSMENT
        if context.mode is MaintenancePlanMode.BARRIER
        else maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
    )
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    evidence = None
    if (expected_phase is maintenance_state.MaintenancePhase.ASSESSMENT
        and cycle.file_assessment_start_merged_count is not None
        and (not is_loop_manager_repository(Path(repo))
             or cycle.full_assessment_completed)):
        evidence = maintenance_file_publication.collect(
            connection, repo, worktree, base_branch,
            context.cycle_revision, context.active_sha,
        )
    with runtime_state._transaction(connection):
        intent = maintenance_state.MaintenanceRepository(
            connection
        ).record_publication_intent(
            repo,
            expected_phase=expected_phase,
            expected_revision=context.cycle_revision,
            base_sha=context.active_sha,
            commit_sha=plan_commit,
            refactoring_plan_ids=result.refactoring_plan_ids,
            revalidation_plan_ids=result.revalidation_plan_ids,
        )
        if evidence is not None:
            maintenance_file_publication.save_intent(
                connection, repo, context.cycle_revision, context.active_sha,
                plan_commit, evidence,
            )
        return intent


def _maintenance_rows_match_published_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> bool:
    for path, plan in result.plans:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, str(plan["id"])),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(plan)
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
        ) != (
            path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            file_hash(worktree / path),
            plan_commit,
            "READY",
        ):
            return False
    for revision in result.revisions:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, revision.snapshot.plan_id),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(dict(revision.plan))
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
        ) != (
            revision.path,
            revision.snapshot.title,
            sources,
            dependencies,
            priority,
            file_hash(worktree / revision.path),
            plan_commit,
            "READY",
        ):
            return False
    return True


def _assert_maintenance_publication_intent(
    connection: sqlite3.Connection,
    repo: str,
    context: MaintenancePlanningContext,
    plan_commit: str,
    result: ValidatedPlanningResult,
) -> None:
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    current = (
        cycle.publication_stage,
        cycle.publication_base_sha,
        cycle.publication_commit_sha,
        cycle.publication_remote_oid,
        cycle.publication_refactoring_plan_ids,
        cycle.publication_revalidation_plan_ids,
    )
    expected = (
        "prepared", context.active_sha, plan_commit, None,
        result.refactoring_plan_ids, result.revalidation_plan_ids,
    )
    if current != expected:
        raise MaintenancePlanningError(
            "원격 push 전에 보존한 maintenance publication intent가 "
            "현재 batch와 일치하지 않습니다."
        )


def _maintenance_publication_already_applied(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> bool:
    context = result.maintenance_context
    assert context is not None
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if context.mode is MaintenancePlanMode.BARRIER and result.refactoring_plan_ids:
        cycle_matches = (
            cycle.phase is maintenance_state.MaintenancePhase.REFACTORING
            and cycle.revision == context.cycle_revision + 1
            and cycle.refactoring_plan_ids == result.refactoring_plan_ids
            and cycle.revalidation_plan_ids == result.revalidation_plan_ids
            and cycle.publication_base_sha == context.active_sha
            and cycle.publication_commit_sha == plan_commit
            and cycle.publication_remote_oid == plan_commit
        )
    else:
        cycle_matches = (
            cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and cycle.revision == context.cycle_revision + 1
            and cycle.last_success_commit == plan_commit
        )
    if (cycle_matches and context.mode is MaintenancePlanMode.BARRIER
        and cycle.file_assessment_start_merged_count is not None
        and (not is_loop_manager_repository(Path(repo))
             or cycle.full_assessment_completed)):
        evidence = maintenance_file_publication.saved_intent(
            connection, repo, context.cycle_revision, context.active_sha,
            plan_commit,
        )
        success = maintenance_file_ledger.FileReviewRepository(connection).last_success(repo)
        if (success is None or success.source_revision != evidence.inventory.revision
            or cycle.last_success_file_assessment_merged_count is None):
            return False
    return cycle_matches and _maintenance_rows_match_published_batch(
        connection, repo, worktree, result, plan_commit
    )


def publish_maintenance_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> maintenance_state.MaintenanceCycle:
    """Atomically publish catalog revisions after a confirmed remote push.

    Calling this again after a process interruption is idempotent when the
    remote commit, catalog rows, and persisted cycle describe the same batch.
    """

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    _validate_maintenance_publication_commit(
        worktree, base_branch, result, plan_commit
    )
    return _apply_maintenance_planning_batch(
        connection, repo, worktree, base_branch, result, plan_commit
    )


def _apply_maintenance_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    *, current_remote: str | None = None,
) -> maintenance_state.MaintenanceCycle:
    """Apply one already-confirmed maintenance batch in a SQLite transaction."""

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    with runtime_state._transaction(connection):
        repository = maintenance_state.MaintenanceRepository(connection)
        if _maintenance_publication_already_applied(
            connection, repo, worktree, result, plan_commit
        ):
            return repository.get(repo)
        include_general_planning = repository.get(repo).include_general_planning
        _assert_maintenance_publication_intent(
            connection, repo, context, plan_commit, result
        )
        _assert_maintenance_context_current(connection, context)
        _register_execplan_rows(
            connection, repo, worktree, list(result.plans), plan_commit
        )
        for revision in result.revisions:
            snapshot = revision.snapshot
            _sources, dependencies, _priority = _execplan_metadata(
                dict(revision.plan)
            )
            cursor = connection.execute(
                """
                UPDATE exec_plans
                SET depends_on = ?, content_hash = ?, plan_sha = ?,
                    current_revision_sha = ?, current_complexity = ?, current_risk = ?,
                    catalog_generation = catalog_generation + 1
                WHERE repo = ? AND plan_id = ? AND status = 'READY'
                  AND path = ? AND title = ? AND sources = ?
                  AND depends_on = ? AND priority = ?
                  AND content_hash = ?
                  AND COALESCE(current_revision_sha, plan_sha) = ?
                  AND branch IS NULL AND worktree IS NULL
                  AND workspace_owned = 0
                  AND worker_session_id IS NULL
                  AND reviewer_session_id IS NULL AND pr_url IS NULL
                  AND pr_head_oid IS NULL
                  AND pr_conflict_recovery = 0
                  AND pr_conflict_head_oid IS NULL
                  AND pr_conflict_base_oid IS NULL
                """,
                (
                    dependencies,
                    file_hash(worktree / revision.path),
                    plan_commit,
                    plan_commit,
                    str(revision.plan["complexity"]),
                    str(revision.plan["risk"]),
                    repo,
                    snapshot.plan_id,
                    snapshot.path,
                    snapshot.title,
                    json.dumps(
                        snapshot.sources,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        snapshot.depends_on,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    snapshot.priority,
                    snapshot.content_hash,
                    snapshot.plan_sha,
                ),
            )
            if cursor.rowcount != 1:
                raise MaintenancePlanningError(
                    "publication 직전 READY snapshot이 변경됐습니다: "
                    f"{snapshot.plan_id}"
                )

        cycle = repository.get(repo)
        if (context.mode is MaintenancePlanMode.BARRIER
            and cycle.file_assessment_start_merged_count is not None
            and (not is_loop_manager_repository(Path(repo))
                 or cycle.full_assessment_completed)):
            evidence = maintenance_file_publication.saved_intent(
                connection, repo, context.cycle_revision, context.active_sha,
                plan_commit,
            )
            maintenance_file_publication.promote(
                connection, repo, Path(repo), base_branch,
                current_remote or plan_commit, evidence,
            )
            repository.advance_file_assessment_success_baseline(
                repo, expected_revision=context.cycle_revision,
                source_sha=context.active_sha,
                expected_merged_count=evidence.merged_count,
                bump_revision=False,
            )

        if (
            context.mode is MaintenancePlanMode.BARRIER
            and result.refactoring_plan_ids
        ):
            next_cycle = repository.transition(
                repo,
                expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                expected_revision=context.cycle_revision,
                next_phase=maintenance_state.MaintenancePhase.REFACTORING,
                refactoring_plan_ids=result.refactoring_plan_ids,
                revalidation_plan_ids=result.revalidation_plan_ids,
                publication_stage="remote_confirmed",
                publication_base_sha=context.active_sha,
                publication_commit_sha=plan_commit,
                publication_remote_oid=plan_commit,
                include_general_planning=False,
            )
        else:
            next_cycle = repository.advance_success_baseline(
                repo,
                expected_phase=(
                    maintenance_state.MaintenancePhase.ASSESSMENT
                    if context.mode is MaintenancePlanMode.BARRIER
                    else maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
                ),
                expected_revision=context.cycle_revision,
                remote_commit=plan_commit,
            )
        _clear_active_in_current_transaction(
            connection,
            repo,
            processed_sha=plan_commit if include_general_planning else None,
            specification_fingerprint=(
                _specification_input_fingerprint(worktree, plan_commit)
                if include_general_planning
                else None
            ),
            planning_fingerprint=(
                _planning_input_fingerprint(worktree, plan_commit)
                if include_general_planning
                else None
            ),
        )
        return next_cycle


def restart_stale_maintenance_publication(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> maintenance_state.MaintenanceCycle:
    """Restart a batch proven absent from the newly fetched remote base.

    Git files and refs remain intact. The caller creates a fresh planning
    worktree at cycle_base_sha and obtains a new context after this returns.
    """
    repository = maintenance_state.MaintenanceRepository(connection)
    cycle = repository.get(repo)
    if (
        cycle.publication_stage != "prepared"
        or not cycle.publication_base_sha
        or not cycle.publication_commit_sha
        or cycle.publication_remote_oid is not None
    ):
        raise MaintenancePlanningError("재시작할 prepared publication intent가 없습니다.")
    remote = git(
        worktree, "rev-parse", "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    base = cycle.publication_base_sha
    commit = cycle.publication_commit_sha
    if remote in {base, commit}:
        raise MaintenancePlanningError("게시 기준이 이동하지 않았거나 batch가 이미 게시됐습니다.")

    if (
        not _git_commit_is_ancestor(worktree, base, remote)
        or _git_commit_is_ancestor(worktree, commit, remote)
    ):
        raise MaintenancePlanningError("미게시 batch임을 확정할 수 없어 재시작할 수 없습니다.")
    base_tree = _git_tree_entries(worktree, base)
    commit_tree = _git_tree_entries(worktree, commit)
    remote_tree = _git_tree_entries(worktree, remote)
    batch_paths = {
        path for path in base_tree.keys() | commit_tree.keys()
        if base_tree.get(path) != commit_tree.get(path)
    }
    if any(base_tree.get(path) != remote_tree.get(path) for path in batch_paths):
        raise MaintenancePlanningError("원격 파일이 보존된 batch와 겹쳐 재시작할 수 없습니다.")
    with runtime_state._transaction(connection):
        restarted = repository.restart_prepared_batch(
            repo, expected=cycle, new_base_sha=remote
        )
        _clear_active_in_current_transaction(connection, repo)
        return restarted


def recover_maintenance_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> maintenance_state.MaintenanceCycle:
    """Recover a pushed batch from its SQLite intent and remote commit."""

    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.publication_stage == "remote_confirmed":
        remote_oid = cycle.publication_remote_oid
        current_remote = git(
            worktree,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base_branch}^{{commit}}",
        ).stdout.strip()
        if remote_oid and current_remote == remote_oid:
            return cycle
        if (
            not remote_oid
            or not cycle.publication_base_sha
            or cycle.publication_commit_sha != remote_oid
        ):
            raise MaintenancePlanningError(
                "적용된 maintenance publication 기록이 완전하지 않습니다."
            )
        _assert_remote_preserves_maintenance_batch(
            worktree,
            cycle.publication_base_sha,
            remote_oid,
            current_remote,
        )
        return cycle
    if (
        cycle.publication_stage != "prepared"
        or cycle.publication_base_sha is None
        or cycle.publication_commit_sha is None
        or cycle.publication_remote_oid is not None
    ):
        raise MaintenancePlanningError(
            "복구할 maintenance publication intent가 없습니다."
        )
    base_sha = cycle.publication_base_sha
    plan_commit = cycle.publication_commit_sha
    current_remote = git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    remote_advanced_after_publication = False
    if current_remote != plan_commit:
        if current_remote == base_sha:
            raise MaintenancePlanningError(
                "보존된 maintenance planning commit의 push가 아직 확인되지 않았습니다."
            )
        if _git_commit_is_ancestor(worktree, plan_commit, current_remote):
            _assert_remote_preserves_maintenance_batch(
                worktree, base_sha, plan_commit, current_remote,
                reject_catalog_drift=True,
            )
            remote_advanced_after_publication = True
        else:
            return restart_stale_maintenance_publication(
                connection, repo, worktree, base_branch
            )
    if (
        remote_advanced_after_publication
        and cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
        and cycle.file_assessment_start_merged_count is not None
        and (not is_loop_manager_repository(Path(repo))
             or cycle.full_assessment_completed)
    ):
        evidence = maintenance_file_publication.saved_intent(
            connection, repo, cycle.revision, base_sha, plan_commit,
        )
        if not maintenance_file_publication.sources_match_remote(
            Path(repo), base_branch, current_remote, evidence,
        ):
            # The plans remain published. A later catalog reconciliation imports
            # them, while this assessment must inspect the new source revision.
            with runtime_state._transaction(connection):
                restarted = maintenance_state.MaintenanceRepository(
                    connection
                ).restart_prepared_batch(
                    repo, expected=cycle, new_base_sha=current_remote,
                )
                _clear_active_in_current_transaction(connection, repo)
                return restarted
    context = _maintenance_context_at_revision(
        connection, repo, worktree, base_sha, cycle
    )
    try:
        result = validate_maintenance_planner_result(
            connection,
            context,
            worktree,
            changed_files(worktree),
            refactoring_plan_ids=(cycle.publication_refactoring_plan_ids if context.mode is MaintenancePlanMode.BARRIER else ()),
            allow_general_plans=cycle.include_general_planning,
        )
    except PlannerValidationError as error:
        raise MaintenancePlanningError(
            f"보존된 maintenance publication batch가 유효하지 않습니다: {error}"
        ) from error
    if result is None:
        raise MaintenancePlanningError(
            "보존된 maintenance publication commit에 검증할 batch가 없습니다."
        )
    if remote_advanced_after_publication:
        _validate_maintenance_planning_commit(worktree, result, plan_commit)
        return _apply_maintenance_planning_batch(
            connection, repo, worktree, base_branch, result, plan_commit,
            current_remote=current_remote,
        )
    return publish_maintenance_planning_batch(
        connection,
        repo,
        worktree,
        base_branch,
        result,
        plan_commit,
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


def _current_execplan_sha(
    root: Path,
    revision: str,
    entry: execplan_catalog.ExecPlanCatalogEntry,
) -> str:
    """Find the oldest revision that contains the current active plan bytes."""

    result = git(
        root,
        "log",
        revision,
        "--format=%H",
        "--",
        entry.active_path,
        check=False,
    )
    for commit in reversed(result.stdout.splitlines()):
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
            continue
        try:
            content = git_bytes(
                root, "show", f"{commit}:{entry.active_path}"
            )
        except RuntimeError:
            continue
        if hashlib.sha256(content).hexdigest() == entry.content_hash:
            return commit
    raise RuntimeError(
        "현재 active ExecPlan 본문의 게시 commit을 안전하게 확인할 수 없습니다: "
        f"{entry.active_path}"
    )


class _LegacyCatalogMergeProof:
    """GitHub/Git adapter kept outside the parser and catalog repository."""

    def __init__(
        self,
        root: Path,
        revision: str,
        stored: Mapping[str, execplan_catalog_state.StoredPlanIdentity],
    ):
        self.root = root
        self.revision = revision
        self.stored = stored

    def _stored_git_proof(
        self, entry: execplan_catalog.ExecPlanCatalogEntry
    ) -> execplan_catalog_state.MergeProof:
        identity = self.stored.get(entry.plan_id)
        if (
            identity is None
            or identity.status != "MERGED"
            or not identity.plan_sha
        ):
            return execplan_catalog_state.MergeProof(False)
        try:
            revision_sha = git(
                self.root, "rev-parse", f"{self.revision}^{{commit}}"
            ).stdout.strip()
            published = git_bytes(
                self.root,
                "show",
                f"{identity.plan_sha}:{identity.path}",
            )
            ancestor = _git_commit_is_ancestor(
                self.root, identity.plan_sha, revision_sha
            )
        except RuntimeError as error:
            return execplan_catalog_state.MergeProof(False, detail=str(error))
        published_hash = hashlib.sha256(published).hexdigest()
        if not ancestor or published_hash != identity.content_hash:
            return execplan_catalog_state.MergeProof(False)
        return execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.STORED_AND_GIT,
            plan_sha=identity.plan_sha,
            publication_content_hash=published_hash,
            revision=revision_sha,
        )

    def proof_for(
        self, entry: execplan_catalog.ExecPlanCatalogEntry
    ) -> execplan_catalog_state.MergeProof:
        pull_request = pr_for_branch(
            self.root, f"loop-manager/{entry.plan_id}"
        )
        if pull_request is None:
            return self._stored_git_proof(entry)
        merged = bool(
            pull_request["state"] == "MERGED" or pull_request.get("mergedAt")
        )
        if not merged:
            return execplan_catalog_state.MergeProof(
                False,
                pr_url=str(pull_request.get("url") or "") or None,
                detail=str(pull_request.get("state") or "unknown"),
            )
        identity = self.stored.get(entry.plan_id)
        if identity is not None and identity.plan_sha:
            plan_sha = identity.plan_sha
        else:
            # Use the latest active publication, including maintenance revisions.
            # Matching archive bytes against any older revision would accept
            # an archive reverted to an obsolete publication.
            plan_sha = git(
                self.root, "log", self.revision, "--no-renames",
                "--diff-filter=AM", "--format=%H", "-1", "--",
                entry.active_path,
            ).stdout.strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", plan_sha):
                raise RuntimeError("archived ExecPlan의 게시 revision이 없습니다.")
        published_hash = hashlib.sha256(
            git_bytes(self.root, "show", f"{plan_sha}:{entry.active_path}")
        ).hexdigest()
        return execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
            plan_sha=plan_sha,
            publication_content_hash=published_hash,
            revision=(
                str(
                    (pull_request.get("mergeCommit") or {}).get("oid")
                    or ""
                )
                or str(pull_request.get("mergedAt") or "")
                or None
            ),
            pr_url=str(pull_request.get("url") or "") or None,
        )


def _insert_recovered_execplan(
    connection: sqlite3.Connection,
    repo: str,
    entry: execplan_catalog.ExecPlanCatalogEntry,
    *,
    path: str,
    content_hash: str,
    status: str,
    plan_sha: str | None,
    pr_url: str | None = None,
    project: sqlite3.Row | None = None,
) -> None:
    existing = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, entry.plan_id),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["publication_path"] or existing["path"]) != path
            or str(
                existing["publication_content_hash"] or existing["content_hash"]
            ) != content_hash
            or (
                (existing["publication_sha"] or existing["plan_sha"])
                and plan_sha
                and str(existing["publication_sha"] or existing["plan_sha"])
                != plan_sha
            )
        ):
            raise RuntimeError(
                f"복구된 ExecPlan identity가 기존 catalog와 충돌합니다: {entry.plan_id}"
            )
        if status == "MERGED" and str(existing["status"]) != "MERGED":
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                entry.plan_id,
                plan_values={"status": "MERGED"},
                enqueue=False,
            )
        return

    plan = entry.metadata()
    sources, dependencies, priority = _execplan_metadata(plan)
    initial_level = worker_level(plan)
    is_active = project is not None
    connection.execute(
        """
        INSERT INTO exec_plans (
            repo, plan_id, path, title, sources, depends_on, priority,
            content_hash, current_complexity, current_risk,
            current_revision_sha, publication_path,
            publication_content_hash, publication_sha,
            status, plan_sha, active_sha, branch, worktree,
            pr_url, last_error, worker_session_id, worker_result, verify_result,
            initial_worker_level, effective_worker_level, workspace_owned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo,
            entry.plan_id,
            path,
            entry.title,
            sources,
            dependencies,
            priority,
            content_hash,
            entry.complexity,
            entry.risk,
            plan_sha,
            path,
            content_hash,
            plan_sha,
            status,
            plan_sha,
            project["active_sha"] if is_active else None,
            project["branch"] if is_active else None,
            project["worktree"] if is_active else None,
            project["pr_url"] if is_active else pr_url,
            project["last_error"] if is_active else None,
            project["worker_session_id"] if is_active else None,
            project["worker_result"] if is_active else None,
            project["verify_result"] if is_active else None,
            initial_level,
            initial_level,
            1 if is_active else 0,
        ),
    )


def _quarantine_unverified_historical_runtime(
    connection: sqlite3.Connection,
    repo: str,
    recovery: execplan_catalog_state.CatalogRecoveryResult,
) -> None:
    """Keep an archived diagnostic from resuming an old active runtime row."""

    diagnostic_ids = {item.plan_id for item in recovery.diagnostics}
    if not diagnostic_ids:
        return
    repository = runtime_state.RuntimeStateRepository(connection)
    project = connection.execute(
        "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    projected_path = str(project["plan_path"] or "") if project else ""
    for plan_id in sorted(diagnostic_ids):
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None or str(row["status"]) not in ACTIVE_EXECPLAN_STATUSES:
            continue
        plan_values = {
            "status": "PLAN_BLOCKED",
            "archive_resume_status": str(row["status"]),
        }
        if projected_path == str(row["path"]):
            repository.transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values=runtime_state.clear_project_values(),
                expected_status=str(row["status"]),
                enqueue=False,
            )
            projected_path = ""
        else:
            repository.update_plan(
                repo,
                plan_id,
                plan_values=plan_values,
                expected_status=str(row["status"]),
                enqueue=False,
            )


def _migrate_execplan_catalog(
    connection: sqlite3.Connection,
    *,
    reconcile_history: bool,
    target_repo: str | None,
    manage_transaction: bool = True,
) -> None:
    phase_status = {
        "WORKING": "IMPLEMENTING",
        "VERIFYING": "VERIFYING",
        "REVIEWING": "REVIEWING",
        "PUBLISHING": "PUBLISHING",
        "PR_OPEN": "PR_OPEN",
        "BLOCKED": "PLAN_BLOCKED",
        "PLAN_BLOCKED": "PLAN_BLOCKED",
    }
    query = "SELECT * FROM projects ORDER BY repo"
    parameters: tuple[str, ...] = ()
    if target_repo is not None:
        query = "SELECT * FROM projects WHERE repo = ?"
        parameters = (target_repo,)
    projects = connection.execute(query, parameters).fetchall()

    with connection if manage_transaction else contextlib.nullcontext():
        for project in projects:
            if project["phase"] == "PLANNING":
                continue
            root = Path(str(project["repo"]))
            revision = f"refs/remotes/origin/{project['base_branch']}"
            snapshot = revision_execplan_snapshot(
                root,
                revision,
                archive_conflict_plan_ids=_merged_execplan_ids(
                    connection, str(project["repo"])
                ),
            )
            active_path = str(project["plan_path"] or "")
            rows = connection.execute(
                "SELECT plan_id, path, content_hash, plan_sha, status, "
                "publication_path, publication_content_hash, publication_sha "
                "FROM exec_plans WHERE repo = ?",
                (project["repo"],),
            ).fetchall()
            stored = {
                str(row["plan_id"]): execplan_catalog_state.StoredPlanIdentity(
                    plan_id=str(row["plan_id"]),
                    path=str(row["publication_path"] or row["path"]),
                    content_hash=str(
                        row["publication_content_hash"] or row["content_hash"]
                    ),
                    plan_sha=(
                        str(row["publication_sha"] or row["plan_sha"])
                        if row["publication_sha"] or row["plan_sha"]
                        else None
                    ),
                    status=str(row["status"]),
                    current_content_hash=str(row["content_hash"]),
                )
                for row in rows
            }
            historical_ids = {
                str(row["plan_id"])
                for row in connection.execute(
                    "SELECT plan_id FROM execplan_catalog_history WHERE repo = ?",
                    (project["repo"],),
                )
            }
            catalog_initialized = connection.execute(
                "SELECT 1 FROM execplan_catalog_revisions WHERE repo = ?",
                (project["repo"],),
            ).fetchone() is not None
            missing_stored_ids = set(stored) - set(snapshot.all)
            legacy_backfill_allowed = (
                not catalog_initialized and not missing_stored_ids
            )
            unknown_active_ids = set(snapshot.active) - set(stored) - historical_ids
            unknown_archived_ids = (
                set(snapshot.archived) - set(stored) - historical_ids
            )
            unknown_ids = sorted(unknown_active_ids | unknown_archived_ids)
            if missing_stored_ids:
                missing_id = sorted(missing_stored_ids)[0]
                if unknown_ids:
                    replacement_id = unknown_ids[0]
                    raise execplan_catalog.ExecPlanCatalogError(
                        "원격 ExecPlan ID 변경을 기존 실행 이력과 하나로 "
                        f"대응할 수 없습니다: {missing_id} -> {replacement_id}",
                        code="ambiguous-id-change",
                        path=snapshot.all[replacement_id].path,
                        related_paths=(stored[missing_id].path,),
                    )
                raise execplan_catalog.ExecPlanCatalogError(
                    "SQLite ExecPlan에 대응하는 원격 파일이 없습니다: "
                    f"{missing_id}",
                    code="missing-plan",
                    path=stored[missing_id].path,
                )
            if catalog_initialized and unknown_ids:
                unknown_id = unknown_ids[0]
                raise execplan_catalog.ExecPlanCatalogError(
                    "원격 ExecPlan ID를 SQLite 실행 이력과 대응할 수 없습니다: "
                    f"{unknown_id}",
                    code="unknown-plan-id",
                    path=snapshot.all[unknown_id].path,
                )
            catalog_repository = execplan_catalog_state.CatalogStateRepository(
                connection, ensure=False
            )
            runtime_repository = runtime_state.RuntimeStateRepository(connection)
            for entry in snapshot.active.values():
                row = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                    (project["repo"], entry.plan_id),
                ).fetchone()
                if (
                    row is not None
                    and row["status"] == "PLAN_BLOCKED"
                    and row["archive_resume_status"] in ACTIVE_EXECPLAN_STATUSES
                    and row["path"] == entry.active_path
                    and row["content_hash"] == entry.content_hash
                    and project["phase"] is None
                    and runtime_repository.active_row(str(project["repo"])) is None
                ):
                    runtime_repository.update_plan(
                        str(project["repo"]), entry.plan_id,
                        plan_values={
                            "status": row["archive_resume_status"],
                            "archive_resume_status": None,
                        },
                        expected_status="PLAN_BLOCKED", enqueue=False,
                    )
                    runtime_repository.hydrate(str(project["repo"]), entry.plan_id)
            catalog_repository.mark_active(str(project["repo"]), snapshot.active)

            for entry in snapshot.active.values():
                is_active = (
                    entry.path == active_path
                    and str(project["phase"] or "") in phase_status
                )
                if not is_active and not reconcile_history:
                    continue
                identity = stored.get(entry.plan_id)
                if identity is not None:
                    continue
                if (
                    not legacy_backfill_allowed
                    and entry.plan_id not in historical_ids
                ):
                    # A newly observed ID is a reconciliation diagnostic, not
                    # legacy state that may be registered ahead of validation.
                    continue
                pull_request = None
                if is_active:
                    status = phase_status[str(project["phase"])]
                    plan_sha = str(project["plan_sha"] or "") or None
                else:
                    pull_request = pr_for_branch(
                        root, f"loop-manager/{entry.plan_id}"
                    )
                    if pull_request is None:
                        status = "READY"
                    elif pull_request["state"] == "MERGED" or pull_request.get("mergedAt"):
                        status = "MERGED"
                    else:
                        raise RuntimeError(
                            "Legacy ExecPlan의 PR 상태를 자동 이관할 수 없습니다: "
                            f"{entry.plan_id} ({pull_request['state']})"
                        )
                    plan_sha = _current_execplan_sha(
                        root, revision, entry
                    )
                _insert_recovered_execplan(
                    connection,
                    str(project["repo"]),
                    entry,
                    path=entry.active_path,
                    content_hash=entry.content_hash,
                    status=status,
                    plan_sha=plan_sha,
                    pr_url=(str(pull_request["url"]) if pull_request else None),
                    project=(project if is_active else None),
                )

            if not reconcile_history:
                continue
            recovery_snapshot = execplan_catalog.ExecPlanCatalogSnapshot(
                active=snapshot.active,
                archived={
                    plan_id: entry
                    for plan_id, entry in snapshot.archived.items()
                    if legacy_backfill_allowed
                    or plan_id in stored
                    or plan_id in historical_ids
                },
                revision_sha=snapshot.revision_sha,
            )
            recovery = execplan_catalog_state.CatalogRecoveryService(
                catalog_repository,
                _LegacyCatalogMergeProof(root, revision, stored),
            ).reconcile(str(project["repo"]), recovery_snapshot, stored)
            _quarantine_unverified_historical_runtime(
                connection, str(project["repo"]), recovery
            )
            for recovered in recovery.merged:
                _insert_recovered_execplan(
                    connection,
                    str(project["repo"]),
                    recovered.entry,
                    path=recovered.original_path,
                    content_hash=recovered.original_content_hash,
                    status="MERGED",
                    plan_sha=recovered.plan_sha,
                    pr_url=recovered.proof.pr_url,
                )


def migrate_legacy_execplans(
    connection: sqlite3.Connection,
    *,
    reconcile_history: bool = False,
    target_repo: str | None = None,
    ensure_schema: bool = True,
    manage_transaction: bool = True,
) -> None:
    """Conservatively move legacy project-owned execution state into the catalog."""

    if ensure_schema:
        _ensure_execplans_schema(connection)
    _migrate_execplan_catalog(
        connection,
        reconcile_history=reconcile_history,
        target_repo=target_repo,
        manage_transaction=manage_transaction,
    )
    with connection if manage_transaction else contextlib.nullcontext():
        scoped_blocking.migrate_legacy_blocks(connection)
    return



def active_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Return the only plan currently occupying the repository mutation slot."""

    _ensure_execplans_schema(connection)
    return runtime_state.RuntimeStateRepository(connection).active_row(repo)


def project_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Return the plan explicitly mirrored by ``projects``, regardless of status."""

    return runtime_state.RuntimeStateRepository(connection).projected_row(repo)


def plan_blocked_execplans(
    connection: sqlite3.Connection, repo: str
) -> tuple[sqlite3.Row, ...]:
    _ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'PLAN_BLOCKED' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
    )


def merge_wait_execplans(
    connection: sqlite3.Connection, repo: str
) -> tuple[sqlite3.Row, ...]:
    _ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'PR_OPEN' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
    )


def mark_execplan_workspace_owned(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        try:
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                plan_id,
                plan_values={"workspace_owned": WORKSPACE_OWNED},
                enqueue=False,
            )
        except runtime_state.RuntimeStateError as error:
            raise RuntimeError(
                "workspace 소유권을 기록할 ExecPlan을 찾을 수 없습니다."
            ) from error


def reserve_execplan_workspace(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        try:
            runtime_state.RuntimeStateRepository(connection).reserve_workspace(
                repo,
                plan_id,
                reserved_value=WORKSPACE_RESERVED,
                allowed_values=(WORKSPACE_UNOWNED, WORKSPACE_RESERVED),
            )
        except runtime_state.RuntimeStateError as error:
            raise RuntimeError(
                "workspace 생성을 예약할 ExecPlan을 찾을 수 없습니다."
            ) from error


def compact_execplan_catalog(connection: sqlite3.Connection, repo: str) -> str:
    """Serialize only non-terminal plan metadata, never plan bodies."""

    _ensure_execplans_schema(connection)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id)",
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


def prerequisite_validation_rows(
    connection: sqlite3.Connection, repo: str
) -> list[sqlite3.Row]:
    """Load active plans and only their verified, merged dependency closure."""

    _ensure_execplans_schema(connection)
    active = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo AND h.plan_id = exec_plans.plan_id "
        "AND h.currently_archived = 1)",
        (repo,),
    ).fetchall()
    included = {str(row["plan_id"]): row for row in active}
    pending = list(active)
    while pending:
        plan = pending.pop()
        plan_id = str(plan["plan_id"])
        try:
            dependencies = json.loads(str(plan["depends_on"]))
        except json.JSONDecodeError as error:
            raise MaintenanceOrchestrationError(
                f"선행 후보 dependency를 읽을 수 없습니다: {plan_id}"
            ) from error
        if not isinstance(dependencies, list) or any(
            not isinstance(dependency, str) for dependency in dependencies
        ):
            raise MaintenanceOrchestrationError(
                f"선행 후보 dependency 형식이 유효하지 않습니다: {plan_id}"
            )
        for dependency in dependencies:
            if dependency in included:
                continue
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, dependency),
            ).fetchone()
            if row is None:
                raise MaintenanceOrchestrationError(
                    f"선행 후보의 dependency가 catalog에 없습니다: {plan_id} -> {dependency}"
                )
            history = connection.execute(
                "SELECT * FROM execplan_catalog_history "
                "WHERE repo = ? AND plan_id = ? AND currently_archived = 1",
                (repo, dependency),
            ).fetchone()
            if history is None and str(row["path"]).startswith(
                "docs/plans/archive/"
            ):
                raise MaintenanceOrchestrationError(
                    f"archived dependency 검증 실패: {plan_id} -> {dependency}: "
                    "archive identity와 Merge 증명이 없습니다"
                )
            if history is not None:
                identity_ok = (
                    bool(history["identity_verified"])
                    and re.fullmatch(
                        rf"docs/plans/\[{re.escape(dependency)}\] [^/]+\.md",
                        str(history["original_path"]),
                    ) is not None
                    and re.fullmatch(
                        rf"docs/plans/archive/\[{re.escape(dependency)}\] [^/]+\.md",
                        str(history["observed_path"]),
                    ) is not None
                    and bool(history["original_content_hash"])
                    and bool(history["final_content_hash"])
                    and bool(history["plan_sha"])
                    and row["path"] == history["observed_path"]
                    and row["content_hash"] == history["final_content_hash"]
                )
                proof_ok = (
                    bool(history["merge_confirmed"])
                    and history["proof_kind"] in {
                        kind.value for kind in execplan_catalog_state.MergeProofKind
                        if kind is not execplan_catalog_state.MergeProofKind.NONE
                    }
                    and bool(history["proof_revision"])
                    and not history["diagnostic_code"]
                )
                if row["status"] != "MERGED" or not identity_ok or not proof_ok:
                    reason = (
                        "SQLite 상태가 MERGED가 아닙니다" if row["status"] != "MERGED"
                        else "archive identity가 확인되지 않았습니다" if not identity_ok
                        else "Merge 증명이 확인되지 않았습니다"
                    )
                    raise MaintenanceOrchestrationError(
                        f"archived dependency 검증 실패: {plan_id} -> {dependency}: {reason}"
                    )
            included[dependency] = row
            pending.append(row)
    return list(included.values())


def catalog_schedule_decision(
    connection: sqlite3.Connection,
    repo: str,
) -> CatalogScheduleDecision:
    """Separate active work, READY presence, and dependency-gated selection."""

    _ensure_execplans_schema(connection)
    active = active_execplan(connection, repo)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
    ).fetchall()
    historical_confirmation = {
        str(row["plan_id"]): bool(row["merge_confirmed"])
        for row in connection.execute(
            """
            SELECT plan_id, merge_confirmed
            FROM execplan_catalog_history
            WHERE repo = ?
              AND currently_archived = 1
            """,
            (repo,),
        ).fetchall()
    }
    historical_ids = set(historical_confirmation)
    project_is_blocked = scoped_blocking.project_block(connection, repo) is not None
    statuses = {
        row["plan_id"]: (
            row["status"]
            if str(row["plan_id"]) not in historical_confirmation
            or historical_confirmation[str(row["plan_id"])]
            else "HISTORICAL_UNVERIFIED"
        )
        for row in rows
    }
    ready = [
        row
        for row in rows
        if row["status"] == "READY" and str(row["plan_id"]) not in historical_ids
    ]
    execution_rows = [
        row for row in rows
        if str(row["plan_id"]) not in historical_ids
        or (
            historical_confirmation[str(row["plan_id"])]
            and row["status"] == "MERGED"
        )
    ]
    candidates = []
    for row in ready:
        dependencies = json.loads(row["depends_on"])
        if all(statuses.get(dependency) == "MERGED" for dependency in dependencies):
            candidates.append(row)
    selected = None
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if active is None and not project_is_blocked and cycle.phase in {
        maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
        maintenance_state.MaintenancePhase.REFACTORING,
    }:
        selected = select_cycle_ready_execplan(execution_rows, cycle)
    elif (
        active is None
        and not project_is_blocked
        and cycle.phase is maintenance_state.MaintenancePhase.IDLE
        and candidates
    ):
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
        has_plan_blocked=any(
            row["status"] == "PLAN_BLOCKED"
            and str(row["plan_id"]) not in historical_ids for row in rows
        ),
        has_pr_open=any(
            row["status"] == "PR_OPEN"
            and str(row["plan_id"]) not in historical_ids for row in rows
        ),
        project_blocked=project_is_blocked,
    )


def select_ready_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Apply the dependency gate before deterministic priority/ID ordering."""

    return catalog_schedule_decision(connection, repo).runnable_ready


def _general_planning_required(
    row: sqlite3.Row,
    repo_path: Path,
    base_sha: str,
    current_fingerprint: str,
) -> bool:
    if row["planning_fingerprint"] == current_fingerprint:
        return False
    if row["planning_fingerprint"] and row["processed_sha"]:
        processed_fingerprint = _planning_input_fingerprint(
            repo_path, str(row["processed_sha"])
        )
        if processed_fingerprint == current_fingerprint:
            return False
        if _is_requirement_archive_only_change(
            repo_path, str(row["processed_sha"]), base_sha
        ):
            return False
    return True


def _maintenance_change_context_text(
    repo_path: Path,
    base_branch: str,
    cycle: maintenance_state.MaintenanceCycle,
) -> str:
    current = str(cycle.cycle_base_sha or "")
    baseline = str(cycle.last_success_commit or current)
    context = maintenance_git.bounded_remote_change_context(
        repo_path, base_branch, baseline, current
    )
    if not context.ok:
        raise MaintenancePlanningError(
            context.error or "bounded 유지보수 변경 문맥을 만들 수 없습니다."
        )
    return json.dumps(
        {
            "baseline_commit": context.baseline_commit,
            "current_commit": context.current_commit,
            "truncated": context.truncated,
            "commits": [
                {
                    "sha": commit.sha,
                    "subject": commit.subject,
                    "paths": list(commit.paths),
                }
                for commit in context.commits
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _advance_maintenance_cycle_after_merges(
    connection: sqlite3.Connection,
    repo: str,
    base_sha: str,
) -> maintenance_state.MaintenanceCycle:
    repository = maintenance_state.MaintenanceRepository(connection)
    cycle = repository.get(repo)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id "
        "AND h.merge_confirmed = 0)",
        (repo,),
    ).fetchall()
    error = cycle_plan_status_error(rows, cycle)
    if error is not None:
        raise MaintenancePlanningError(error)
    if (
        cycle.phase is maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
        and all_cycle_plans_merged(rows, cycle.prerequisite_plan_ids)
    ):
        return repository.transition(
            repo,
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha=base_sha,
            include_general_planning=False,
        )
    if (
        cycle.phase is maintenance_state.MaintenancePhase.REFACTORING
        and all_cycle_plans_merged(rows, cycle.refactoring_plan_ids)
    ):
        return repository.transition(
            repo,
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha=base_sha,
            include_general_planning=False,
        )
    return cycle


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
    try:
        runtime_state.RuntimeStateRepository(connection).activate(
            repo,
            str(selected["plan_id"]),
            activation_sha=activation_sha,
            branch=branch,
            worktree=str(worktree),
        )
    except runtime_state.RuntimeStateError as error:
        raise RuntimeError(
            "READY ExecPlan 선택 상태가 변경되었습니다."
        ) from error
    connection.commit()
    return connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, selected["plan_id"]),
    ).fetchone()


def hydrate_active_project(connection: sqlite3.Connection, repo: str) -> None:
    """Refresh the legacy execution mirror from the plan-state source of truth."""

    plan = active_execplan(connection, repo)
    if plan is None:
        return
    hydrate_execplan_project(connection, repo, plan)


def hydrate_execplan_project(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
) -> None:
    """Hydrate one explicitly selected plan into the compatibility mirror."""

    if connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is None:
        connection.commit()
        return
    snapshot = runtime_state.ExecutionSnapshot.from_row(plan)
    if snapshot.repo != repo:
        snapshot = replace(snapshot, repo=repo)
    runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(
        repo, snapshot
    )
    connection.commit()


def _mirror_project_update_to_execplan(
    connection: sqlite3.Connection,
    repo: str,
    current: sqlite3.Row | None,
    values: dict[str, object],
) -> None:
    plan_path = (
        str(current["plan_path"])
        if current is not None and current["plan_path"]
        else None
    )
    compatible_values = {
        field: value for field, value in values.items() if field in FIELDS
    }
    if not compatible_values:
        return
    if plan_path and not ("phase" in values and values["phase"] is None):
        _ensure_execplans_schema(connection)
    runtime_state.RuntimeStateRepository(connection).mirror_project_values(
        repo, plan_path, compatible_values
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


def remote_base(
    repo: Path, requested: str | None, *, state_dir: Path | None = None,
) -> str:
    if state_dir is None:
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
    if state_dir is not None:
        heads = git(repo, "ls-remote", "--heads", "origin").stdout.splitlines()
        branches = []
        for line in heads:
            fields = line.split()
            if len(fields) != 2 or not fields[1].startswith("refs/heads/"):
                raise RuntimeError("project fetch 원격 branch 목록이 유효하지 않습니다.")
            branches.append(fields[1].removeprefix("refs/heads/"))
        if base not in branches:
            raise RuntimeError(f"origin/{base} 원격 branch를 찾을 수 없습니다.")
        for branch in sorted(branches):
            _recorded_base_fetch(repo, state_dir, branch, owner="project")
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
    state_dir: Path | None = None,
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
    base_branch = remote_base(root, requested_base, state_dir=state_dir)
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
        source_revision=revision,
    )


def add_registered_project(
    connection: sqlite3.Connection,
    path: Path,
    *,
    config_path: Path,
    provider: str,
    requested_base: str | None,
    approve_agents_mismatch: bool,
    state_dir: Path | None = None,
) -> project_registry.RegisteredProject:
    inspection = inspect_project_registration(
        path,
        config_path=config_path,
        provider=provider,
        requested_base=requested_base,
        approve_agents_mismatch=approve_agents_mismatch,
        state_dir=state_dir,
    )
    project = project_registry.RegisteredProject(
        repo=str(inspection.root),
        base_branch=inspection.base_branch,
        provider=inspection.provider,
        agents_hash=inspection.agents_hash,
        agents_approved=inspection.agents_approved,
        origin=inspection.origin,
    )
    # Schema migration is independent of registration and may commit. Finish it
    # before the registration transaction so a stale remote revision can roll
    # back the registry row together with recovered catalog and baseline data.
    project_registry.ensure_registry_schema(connection)
    _ensure_execplans_schema(connection)
    maintenance_state.ensure_schema(connection)
    with runtime_state._transaction(connection):
        project_registry.add_project(connection, project, manage_transaction=False)
        if inspection.root.is_dir():
            current_sha = git(
                inspection.root, "rev-parse", "--verify",
                f"refs/remotes/origin/{project.base_branch}^{{commit}}",
            ).stdout.strip()
            source_sha = inspection.source_revision or current_sha
            if current_sha != source_sha:
                raise RuntimeError("등록 중 원격 source revision이 변경됐습니다.")
            migrate_legacy_execplans(
                connection,
                reconcile_history=True,
                target_repo=project.repo,
                ensure_schema=False,
                manage_transaction=False,
            )
            snapshot = revision_execplan_snapshot(
                inspection.root, source_sha,
                archive_conflict_plan_ids=_merged_execplan_ids(connection, project.repo),
            )
            current_sha = git(
                inspection.root, "rev-parse", "--verify",
                f"refs/remotes/origin/{project.base_branch}^{{commit}}",
            ).stdout.strip()
            if current_sha != source_sha:
                raise RuntimeError("등록 중 원격 ExecPlan catalog revision이 변경됐습니다.")
            connection.execute(
                "INSERT INTO execplan_catalog_revisions "
                "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET "
                "revision_sha = excluded.revision_sha, "
                "catalog_fingerprint = excluded.catalog_fingerprint",
                (project.repo, source_sha,
                 execplan_catalog_state._catalog_fingerprint(snapshot)),
            )
            repository = maintenance_state.MaintenanceRepository(connection)
            repository.initialize_file_assessment_start(
                project.repo,
                source_sha=source_sha,
                expected_merged_count=repository.merged_count(project.repo),
            )
    return project


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


def _ignored_paths_overlapping_git_update(worktree: Path, target: str) -> list[str]:
    ignored = _ignored_worktree_paths(worktree)
    if not ignored:
        return []
    changed = _nul_git_paths(
        worktree, "diff", "--name-only", "-z", "HEAD", target
    )
    fork = git(worktree, "merge-base", "HEAD", target).stdout.strip()
    for commit in git(worktree, "rev-list", "--reverse", f"{fork}..HEAD").stdout.splitlines():
        changed.extend(_nul_git_paths(
            worktree, "diff-tree", "-m", "--root", "--no-commit-id",
            "--name-only", "-r", "-z", commit,
        ))
    return sorted(
        path for path in ignored
        if any(_paths_overlap(path, candidate) for candidate in changed)
    )


def _remaining_rebase_commits(
    worktree: Path,
    metadata_directory: Path,
    backend: str,
    original_head: str,
    onto: str,
    action: str,
) -> list[str] | None:
    if backend == "rebase-merge":
        todo = metadata_directory / "git-rebase-todo"
        if not todo.is_file():
            return None
        commits = []
        for line in todo.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=2)
            if (
                not fields
                or fields[0].startswith("#")
                or fields[0] in {"drop", "d"}
            ):
                continue
            if fields[0] not in {
                "pick", "p", "reword", "r", "edit", "e",
                "squash", "s", "fixup", "f",
            } or len(fields) < 2:
                return None
            commits.append(
                git_bytes(
                    worktree, "rev-parse", "--verify", f"{fields[1]}^{{commit}}"
                ).decode("ascii").strip()
            )
        return commits

    next_path = metadata_directory / "next"
    last_path = metadata_directory / "last"
    if not next_path.is_file() or not last_path.is_file():
        return None
    commits = git_bytes(
        worktree, "rev-list", "--reverse", original_head, f"^{onto}"
    ).decode("ascii").splitlines()
    try:
        next_index = int(next_path.read_text(encoding="ascii").strip())
        last_index = int(last_path.read_text(encoding="ascii").strip())
    except ValueError:
        return None
    if len(commits) != last_index or not 1 <= next_index <= last_index + 1:
        return None
    return commits[next_index - (1 if action == "--continue" else 0):]


def _rebase_resume_ignored_overlap(worktree: Path, action: str) -> str | None:
    ignored = _ignored_worktree_paths(worktree)
    if not ignored:
        return None
    metadata: tuple[str, str, Path, str] | None = None
    for directory in ("rebase-merge", "rebase-apply"):
        raw_directory = os.fsdecode(
            git_bytes(worktree, "rev-parse", "--git-path", directory).strip()
        )
        metadata_directory = Path(raw_directory)
        if not metadata_directory.is_absolute():
            metadata_directory = worktree / metadata_directory
        values = []
        for name in ("orig-head", "onto"):
            path = metadata_directory / name
            if not path.is_file():
                break
            values.append(path.read_text(encoding="ascii").strip())
        if len(values) == 2:
            metadata = values[0], values[1], metadata_directory, directory
            break
    if metadata is None:
        return "rebase 재개 전 남은 tracked 경로를 확인할 수 없습니다."
    original_head, onto, metadata_directory, backend = metadata
    if action == "--abort":
        changed = _nul_git_paths(
            worktree, "diff", "--name-only", "-z", "HEAD", original_head
        )
    else:
        commits = _remaining_rebase_commits(
            worktree, metadata_directory, backend, original_head, onto, action
        )
        if commits is None:
            return "rebase 재개 전 남은 tracked 경로를 확인할 수 없습니다."
        changed = [
            path
            for commit in commits
            for path in _nul_git_paths(
                worktree, "diff-tree", "-m", "--root", "--no-commit-id",
                "--name-only", "-r", "-z", commit,
            )
        ]
    overlaps = sorted(
        path for path in ignored
        if any(_paths_overlap(path, candidate) for candidate in changed)
    )
    if overlaps:
        return (
            "rebase 재개 tracked 경로와 겹치는 ignored 파일: "
            + ", ".join(overlaps[:8])
        )
    return None


def _base_worktree_snapshot(
    repo: Path,
) -> BaseWorktreeSnapshot:
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
    index = _index_entries(repo)
    operations = tuple(sorted(_git_operation_entries(repo).items()))
    preliminary = BaseWorktreeSnapshot(
        branch=branch,
        head=head,
        index=tuple(sorted(index.items())),
        changes=(),
        operations=operations,
    )
    if operations or _base_worktree_index_error(preliminary):
        return preliminary
    changes = []
    for kind, paths in (
        ("staged", _nul_git_paths(
            repo, "diff", "--cached", "--no-renames", "--name-only", "-z",
            "--ignore-submodules=none",
        )),
        ("unstaged", _nul_git_paths(
            repo, "diff", "--no-renames", "--name-only", "-z",
            "--ignore-submodules=none",
        )),
        ("untracked", _nul_git_paths(
            repo, "ls-files", "--others", "--exclude-standard", "-z",
        )),
        ("ignored", _ignored_worktree_paths(repo)),
    ):
        changes.extend((kind, path, _base_local_path_signature(repo, path)) for path in paths)
    return BaseWorktreeSnapshot(
        branch=branch,
        head=head,
        index=preliminary.index,
        changes=tuple(sorted(changes)),
        operations=operations,
    )


def _base_local_path_signature(repo: Path, relative_path: str) -> str:
    path = repo / relative_path
    if not path.exists() and not path.is_symlink():
        return "missing"
    return _git_operation_path_signature(path)


def _base_worktree_index_error(snapshot: BaseWorktreeSnapshot) -> str | None:
    for path, entries in snapshot.index:
        if len(entries) != 1:
            return path
        fields = entries[0].split()
        if (
            len(fields) != 4
            or fields[0] != "H"
            or fields[3] != "0"
            or not fields[2].strip("0")
        ):
            return path
    return None


def _base_paths_overlap(left: str, right: str) -> bool:
    # A remote checkout can be case insensitive even when the current host is not.
    return _paths_overlap(left.casefold(), right.casefold())


def _source_transition_state(repo: Path) -> dict[str, object]:
    snapshot = _base_worktree_snapshot(repo)
    return {
        "branch": snapshot.branch, "head": snapshot.head,
        "index": {path: list(entries) for path, entries in snapshot.index},
        "changes": [list(entry) for entry in snapshot.changes],
        "operations": [list(entry) for entry in snapshot.operations],
    }


def _source_transition_marker(repo: Path, base: str, intent: Mapping[str, object]) -> bool:
    token = f"loop-manager-source:{intent['id']}: "
    for refname in (f"refs/heads/{base}", "HEAD"):
        result = git(repo, "reflog", "show", "-1", "--format=%H%x00%gs",
                     refname, check=False)
        if result.returncode or not result.stdout.startswith(
            f"{intent['expected']['head']}\x00{token}"
        ):
            return False
    return True


def _source_expected_index(
    repo: Path, selected_remote: str, before_index: Mapping[str, object],
    changed_paths: tuple[str, ...],
) -> dict[str, object]:
    """Keep local entries verbatim and replace only the remote tree delta."""
    expected = dict(before_index)
    remote_entries = {}
    for record in git_bytes(repo, "ls-tree", "-r", "-z", selected_remote).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, _kind, oid = metadata.decode("ascii").split()
            remote_entries[os.fsdecode(path)] = [f"H {mode} {oid} 0"]
    for path in changed_paths:
        expected.pop(path, None)
        if path in remote_entries:
            expected[path] = remote_entries[path]
    return expected


def synchronize_base_worktree(
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
    owner: str = "project",
) -> BaseWorktreeSyncResult:
    """Best-effort fast-forward of an eligible primary checkout."""

    selected_remote = remote_sha or ""
    try:
        if state_dir is not None:
            manager_git_journal.recover_active(
                state_dir, repo=str(_canonical_path(repo)),
                kind="source_fast_forward", target=str(_canonical_path(repo)),
                observe=lambda: _source_transition_state(repo),
                marker=lambda intent: _source_transition_marker(repo, base, intent),
            )
        observed_remote = git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base}^{{commit}}",
        ).stdout.strip()
        selected_remote = selected_remote or observed_remote
        if selected_remote != observed_remote:
            return BaseWorktreeSyncResult(
                "skipped", "선택한 원격 SHA와 현재 origin ref가 다름", selected_remote
            )
        # Equal HEAD needs no file or index scan: nothing will be checked out.
        if git(repo, "rev-parse", "HEAD").stdout.strip() == selected_remote:
            current_branch = git(
                repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
            )
            if current_branch.returncode:
                return BaseWorktreeSyncResult("skipped", "detached HEAD", selected_remote)
            branch = current_branch.stdout.strip()
            if branch != base:
                return BaseWorktreeSyncResult(
                    "skipped", f"다른 branch checkout: {branch}", selected_remote
                )
            operations = _git_operation_entries(repo)
            if operations:
                return BaseWorktreeSyncResult(
                    "skipped",
                    "진행 중 Git operation: " + ", ".join(sorted(operations)),
                    selected_remote,
                )
            return BaseWorktreeSyncResult("equal", "이미 원격 base와 같음", selected_remote)
        before = _base_worktree_snapshot(repo)
        if before.branch is None:
            return BaseWorktreeSyncResult("skipped", "detached HEAD", selected_remote)
        if before.branch != base:
            return BaseWorktreeSyncResult(
                "skipped",
                f"다른 branch checkout: {before.branch}",
                selected_remote,
            )
        if before.operations:
            return BaseWorktreeSyncResult(
                "skipped",
                "진행 중 Git operation: " + ", ".join(name for name, _ in before.operations),
                selected_remote,
            )
        if before.head == selected_remote:
            return BaseWorktreeSyncResult("equal", "이미 원격 base와 같음", selected_remote)
        index_error = _base_worktree_index_error(before)
        if index_error:
            return BaseWorktreeSyncResult(
                "skipped",
                f"비정상 index entry: {index_error}",
                selected_remote,
            )

        behind = git(
            repo,
            "merge-base",
            "--is-ancestor",
            before.head,
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
                before.head,
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
                "--no-renames",
                "--name-only",
                "-z",
                before.head,
                selected_remote,
            )
        )
        overlaps = sorted({
            (kind, path)
            for kind, path, _ in before.changes
            if any(_base_paths_overlap(path, changed) for changed in changed_paths)
        })
        if overlaps:
            return BaseWorktreeSyncResult(
                "skipped",
                "원격 변경과 겹치는 로컬 경로: "
                + ", ".join(f"{kind} {path}" for kind, path in overlaps[:8]),
                selected_remote,
            )

        rechecked = _base_worktree_snapshot(repo)
        if rechecked != before:
            return BaseWorktreeSyncResult(
                "skipped",
                "eligibility 확인 뒤 기본 worktree 상태가 변경됨",
                selected_remote,
            )
        current_remote = git(
            repo, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
        ).stdout.strip()
        if current_remote != selected_remote:
            return BaseWorktreeSyncResult(
                "skipped", "eligibility 확인 뒤 origin ref가 변경됨", selected_remote
            )
        if state_dir is None:
            updated = git(
                repo, "merge", "--ff-only", "--no-autostash", selected_remote,
                check=False,
            )
            if updated.returncode:
                output = updated.stdout[-2000:].strip()
                reason = "Git lock 충돌" if is_git_lock_failure(output) else "fast-forward 실패"
                if output:
                    reason += f": {output}"
                return BaseWorktreeSyncResult("failed", reason, selected_remote)
        else:
            before_state = _source_transition_state(repo)
            if before_state["head"] != before.head or before_state["branch"] != base:
                raise manager_git_journal.MutationEvidenceError(
                    "source fast-forward 직전 HEAD 또는 branch가 변경됐습니다."
                )
            if before_state != {
                "branch": before.branch, "head": before.head,
                "index": {path: list(entries) for path, entries in before.index},
                "changes": [list(entry) for entry in before.changes],
                "operations": [list(entry) for entry in before.operations],
            }:
                raise manager_git_journal.MutationEvidenceError(
                    "source fast-forward 직전 로컬 상태가 변경됐습니다."
                )
            expected_state = {
                **before_state, "head": selected_remote,
                "index": _source_expected_index(
                    repo, selected_remote, before_state["index"], changed_paths,
                ),
            }

            def apply(token: str) -> None:
                environment = {**os.environ,
                               "GIT_REFLOG_ACTION": f"loop-manager-source:{token}"}
                updated = git(
                    repo, "merge", "--ff-only", "--no-autostash", selected_remote,
                    check=False, env=environment,
                )
                if updated.returncode:
                    raise RuntimeError("fast-forward 실패: " + updated.stdout[-2000:].strip())

            manager_git_journal.execute(
                state_dir, owner=owner, kind="source_fast_forward",
                repo=str(_canonical_path(repo)), target=str(_canonical_path(repo)),
                before=before_state, expected=expected_state,
                observe=lambda: _source_transition_state(repo), apply=apply,
                marker=lambda intent: _source_transition_marker(repo, base, intent),
            )
        final_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        if final_head != selected_remote:
            return BaseWorktreeSyncResult(
                "failed",
                f"fast-forward 결과 HEAD 불일치: {final_head}",
                selected_remote,
            )
        return BaseWorktreeSyncResult("updated", "원격 base로 fast-forward", selected_remote)
    except (GitLockError, RuntimeError, OSError) as error:
        return BaseWorktreeSyncResult("failed", str(error), selected_remote)


def ensure_safe_base_worktree(
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
) -> BaseWorktreeSyncResult:
    """Compatibility entry point for the non-blocking worktree synchronizer."""

    return synchronize_base_worktree(
        repo, base, remote_sha=remote_sha, state_dir=state_dir,
    )


def log_base_worktree_sync(repo: Path, result: BaseWorktreeSyncResult) -> None:
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
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                str(catalog_plan["plan_id"]),
                plan_values={
                    "initial_worker_level": calculated_initial,
                    "effective_worker_level": calculated_initial,
                    "review_level_history": "{}",
                },
                enqueue=False,
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
    if any(not 1 <= int(level) <= effective for level in review_history):
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
    resolved_profile: ResolvedProfile | None = None,
) -> tuple[int, str] | CodexResult:
    if resolved_profile is not None:
        model, effort = resolved_profile.model, resolved_profile.effort
    elif role_name == "worker":
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
            process = _handoff_popen(
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
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(event, dict):
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
            result = _handoff_run(
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
    try:
        usage = codex_log_usage(log.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        # Usage is observational; a failed log read cannot retry a completed CLI call.
        usage = Usage(status="invalid")
    if persist_session and not resolved_session_id:
        if returncode == 0:
            return CodexResult(
                1,
                f"Codex {role_name} 실행 결과에서 세션 ID를 확인할 수 없습니다.",
                usage=usage,
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
            usage,
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
    resolved_profile: ResolvedProfile | None = None,
) -> tuple[int, str] | AgentResult:
    profile = resolved_profile or _agent_profile(
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
    usage = Usage()

    if streaming:
        final_event: Mapping[str, object] = {}
        stream_error_seen = False
        stream_error_message = ""
        diagnostic_tail = ""
        with log.open("w", encoding="utf-8") as output:
            process = _handoff_popen(
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
                    except (ValueError, RecursionError):
                        diagnostic_tail = (diagnostic_tail + line)[-6000:]
                        if usage.status == "unknown":
                            usage = Usage(status="invalid")
                        continue
                    if not isinstance(event, dict):
                        if usage.status == "unknown":
                            usage = Usage(status="invalid")
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
                        parsed_usage = claude_usage(event)
                        if parsed_usage.status != "unknown" or usage.status != "invalid":
                            usage = parsed_usage
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
        result = _handoff_run(
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
        except (ValueError, RecursionError):
            event = {}
            usage = Usage(status="invalid")
        if isinstance(event, dict):
            message = _claude_result_message(event)
            parsed_usage = claude_usage(event)
            if parsed_usage.status != "unknown" or usage.status != "invalid":
                usage = parsed_usage
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
            usage=usage,
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
            usage,
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
            resolved_profile=request.profile,
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
            resolved_profile=request.profile,
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
    connection: sqlite3.Connection | None = None,
    invocation: InvocationContext | None = None,
    resolved_profile: ResolvedProfile | None = None,
) -> tuple[int, str] | AgentResult:
    """Execute one provider-neutral Agent request."""

    _check_runtime_handoff()
    resolved: ResolvedProfile | None = resolved_profile
    collection_status = "unknown"
    if invocation is not None and resolved is None:
        try:
            resolved = _agent_profile(config_path, provider, role_name, level, worktree)
        except (RuntimeError, OSError):
            # Profile observation must not alter the provider's own result.
            # A real adapter still resolves and validates before launching CLI.
            collection_status = "error"
    ledger = agent_invocation_state.InvocationRepository(connection) if connection is not None else None
    identity: str | None = None
    if ledger is not None and invocation is not None and resolved is not None:
        try:
            gate = _runtime_handoff_signal.get()
            if gate is None:
                identity = ledger.start(invocation, role_name, provider, resolved, session_id)
            else:
                with gate.launch_lock:
                    gate.check()
                    identity = ledger.start(
                        invocation, role_name, provider, resolved, session_id,
                    )
            collection_status = "pending"
        except sqlite3.Error:
            collection_status = "error"

    def observe_session(started_session_id: str) -> None:
        nonlocal collection_status
        if ledger is not None and identity is not None:
            try:
                ledger.observe_session(identity, started_session_id)
            except sqlite3.Error:
                collection_status = "error"
        if on_session_started is not None:
            on_session_started(started_session_id)

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
        on_session_started=observe_session,
        profile=resolved,
    )
    started = time.monotonic()
    try:
        raw = agent_provider(provider).run(request)
    except BaseException as error:
        if ledger is not None and identity is not None:
            kind = ("interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else
                    "timeout" if isinstance(error, subprocess.TimeoutExpired) else
                    "transient_failure" if isinstance(error, OSError) else "exception")
            try:
                ledger.finish(identity, kind, None,
                              max(0, int((time.monotonic() - started) * 1000)),
                              None, Usage())
            except sqlite3.Error:
                pass
        raise
    result = _coerce_agent_result(raw, session_id)
    if ledger is not None and identity is not None:
        try:
            ledger.finish(identity, result.ledger_kind, result.code,
                          max(0, int((time.monotonic() - started) * 1000)),
                          result.session_id, result.usage,
                          "error" if collection_status == "error" else None)
            if collection_status != "error":
                collection_status = result.usage.status
        except sqlite3.Error:
            collection_status = "error"
    if isinstance(raw, AgentResult):
        return replace(raw, collection_status=collection_status)
    return raw


def _run_agent_with_connection(
    connection: sqlite3.Connection, *args: object, **kwargs: object
) -> tuple[int, str] | AgentResult:
    """Keep execution coordinator unaware of the ledger connection."""
    return run_agent(*args, connection=connection, **kwargs)


class VerifyOutput(str):
    """Bounded persisted feedback that retains full output for fingerprinting."""

    fingerprint_source: str
    timed_out: bool
    exit_code: int | None

    def __new__(cls, output: str, *, timed_out: bool = False,
                exit_code: int | None = None) -> VerifyOutput:
        shown = redact_secrets(output) if timed_out else output
        instance = super().__new__(cls, shown.strip()[-8000:])
        instance.fingerprint_source = output
        instance.timed_out = timed_out
        instance.exit_code = exit_code
        return instance


def verify(worktree: Path, timeout_seconds: int = 600) -> tuple[bool, str]:
    _check_runtime_handoff()
    if os.name == "nt":
        script = worktree / "scripts/verify.ps1"
        executable = shutil.which("pwsh") or shutil.which("powershell")
        arguments = [executable or "pwsh", "-NoProfile", "-File", str(script)]
    else:
        script = worktree / "scripts/verify.sh"
        arguments = ["bash", str(script)]
    if not script.is_file():
        return False, f"전체 검증 스크립트가 없습니다: {script.relative_to(worktree)}"
    gate = _runtime_handoff_signal.get()
    passed, timed_out, output = run_verify_process(
        arguments, worktree, timeout_seconds,
        **({"launch_process": _handoff_popen} if gate is not None else {}),
    )
    exit_code = getattr(output, "exit_code", 0 if passed else None)
    if timed_out:
        output += (
            f"\n전체 verify 시간 초과: verify_timeout_seconds={timeout_seconds}. "
            "verify와 자식 프로세스 종료를 확인했습니다."
        )
    return passed, VerifyOutput(output, timed_out=timed_out, exit_code=exit_code)


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
_REVIEW_ISSUE_SYMBOL = re.compile(r"(?i)^[a-z0-9_.<>/-]+$")
_REVIEW_ISSUE_CATEGORY = re.compile(r"(?i)^[a-z0-9][a-z0-9-]*$")
_REVIEW_VERDICT_DECLARATION = re.compile(r"(?i)^[ \t]*VERDICT\b")
_REVIEW_VERDICT = re.compile(r"(?i)^[ \t]*VERDICT:[ \t]*(PASS|FAIL|SPEC_BLOCKED)[ \t]*$")


def _valid_review_issue_path(path: str) -> bool:
    if (
        not path
        or path != path.strip()
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or not path.isprintable()
    ):
        return False
    parts = path.split("/")
    return all(
        part and part == part.strip() and part not in {".", ".."}
        for part in parts
    )


def _valid_review_issue_identifier(identifier: str) -> bool:
    components = identifier.split("::")
    if len(components) != 3:
        return False
    path, symbol, category = components
    return (
        _valid_review_issue_path(path)
        and _REVIEW_ISSUE_SYMBOL.fullmatch(symbol) is not None
        and _REVIEW_ISSUE_CATEGORY.fullmatch(category) is not None
    )


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
        match.group("identifier").strip().casefold()
        for match in matches
    ]
    if any(
        not _valid_review_issue_identifier(identifier)
        for identifier in raw_identifiers
    ):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    identifiers = set(raw_identifiers)
    return tuple(f"ISSUE_ID:{identifier}" for identifier in sorted(identifiers))


def parse_reviewer_result(
    output: str,
) -> tuple[str, tuple[str, ...] | reviewer_specification.SpecificationDeadlock]:
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
            "Reviewer VERDICT는 PASS, FAIL 또는 SPEC_BLOCKED이어야 합니다."
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
    specification_declarations = [
        line for _index, line in meaningful_lines
        if line.lstrip().upper().startswith("SPEC_DEADLOCK:")
    ]
    if verdict == "SPEC_BLOCKED":
        if issue_declarations:
            raise ReviewerProtocolError("SPEC_BLOCKED 결과에는 ISSUE_ID가 없어야 합니다.")
        try:
            return verdict, reviewer_specification.parse_evidence(output)
        except ValueError as error:
            raise ReviewerProtocolError(str(error)) from error
    if specification_declarations:
        raise ReviewerProtocolError("PASS·FAIL 결과에는 SPEC_DEADLOCK이 없어야 합니다.")
    if verdict == "PASS":
        if issue_declarations:
            raise ReviewerProtocolError(
                "Reviewer PASS 결과에는 ISSUE_ID가 없어야 합니다."
            )
        return verdict, ()
    return verdict, _normalize_review_issue_ids(output)


def _review_issue_paths(output: object) -> list[str]:
    if not isinstance(output, str) or not output.strip():
        return []
    try:
        verdict, details = parse_reviewer_result(output)
    except ReviewerProtocolError:
        return []
    if verdict == "SPEC_BLOCKED":
        assert isinstance(details, reviewer_specification.SpecificationDeadlock)
        return sorted(details.paths)
    if verdict != "FAIL":
        return []
    return sorted(
        {
            match.group("identifier").strip().split("::", 1)[0]
            for match in _REVIEW_ISSUE_ID.finditer(output)
        }
    )


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
    normalized = (
        "전체 verify 시간 초과"
        if stage == "verify" and getattr(output, "timed_out", False)
        else normalize_validation_failure(stage, output, worktree)
    )
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
    fingerprints = tuple(_deserialize_failure_history(plan[column]))
    with connection:
        runtime_state.RuntimeStateRepository(connection).update_plan(
            repo,
            str(plan["plan_id"]),
            plan_values={column: "{}"},
            enqueue=False,
        )
        recovery_state.RecoveryRepository(connection).clear_correction_occurrences(
            repo, str(plan["plan_id"]), fingerprints,
        )


def pr_for_branch(repo: Path, branch: str, url: str | None = None) -> dict[str, object] | None:
    fields = "url,state,mergedAt,headRefOid,headRefName,baseRefName,mergeable"
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
    state_dir: Path,
    base: str,
    branch: str,
) -> None:
    for ref in dict.fromkeys((base, branch)):
        _recorded_base_fetch(repo_path, state_dir, ref, owner="project")


def _validate_pr_conflict_recovery_identity(
    connection: sqlite3.Connection,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
    *,
    expected_status: str,
) -> tuple[sqlite3.Row, Path, str, str]:
    plan = project_execplan(connection, repo)
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
        or plan["status"] != expected_status
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
    return plan, worktree, branch, head_oid


def _validate_pr_conflict_recovery_ownership(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
    *,
    expected_status: str = "PR_OPEN",
) -> tuple[sqlite3.Row, Path, str, str, str]:
    """Prove the saved URL, branch, workspace, and observed remote head."""

    plan, worktree, branch, head_oid = (
        _validate_pr_conflict_recovery_identity(
            connection,
            repo,
            project,
            pull_request,
            expected_status=expected_status,
        )
    )
    plan_id = str(plan["plan_id"])
    validate_execplan_workspace_identity(
        state_dir, repo, plan_id, branch, worktree
    )
    make_worktree(
        repo_path,
        worktree,
        branch,
        str(project["base_branch"]),
        journal_state_dir=state_dir,
        journal_owner=f"plan:{plan_id}",
        allow_create_branch=False,
        allow_recovery=True,
        allow_existing=True,
    )
    snapshot = capture_git_snapshot(worktree)
    ignored_paths = frozenset(_ignored_worktree_paths(worktree))
    publishable_dirty = {
        path: state
        for path, state in snapshot.dirty.items()
        if path not in ignored_paths
    }
    flagged_paths = sorted(
        path for path, entries in snapshot.index.items()
        if _non_default_index_flags(entries)
    )
    if (
        snapshot.branch != branch or snapshot.head != head_oid
        or publishable_dirty or flagged_paths or snapshot.operations
    ):
        details = []
        if snapshot.branch != branch:
            details.append(f"branch {snapshot.branch} (expected {branch})")
        if snapshot.head != head_oid:
            details.append(f"HEAD {snapshot.head} (expected {head_oid})")
        if publishable_dirty:
            details.append(
                "게시 가능 변경 " + ", ".join(sorted(publishable_dirty)[:8])
            )
        if flagged_paths:
            details.append("index flags " + ", ".join(flagged_paths[:8]))
        if snapshot.operations:
            details.append(
                "Git operation " + ", ".join(sorted(snapshot.operations))
            )
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 시작 전 관리 worktree가 관측한 원격 head의 "
            "clean 상태와 일치하지 않습니다: " + "; ".join(details)
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
    overlaps = _ignored_paths_overlapping_git_update(worktree, base_oid)
    if overlaps:
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 tracked 경로와 겹치는 ignored 파일: "
            + ", ".join(overlaps[:8])
        )
    return plan, worktree, branch, head_oid, base_oid


def _validate_blocked_pr_without_conflict(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> None:
    """Read-only proof for releasing an old conflict preflight false positive."""

    plan, worktree, branch, head_oid = _validate_pr_conflict_recovery_identity(
        connection, repo, project, pull_request, expected_status="PLAN_BLOCKED"
    )
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    if not worktree.is_dir():
        raise WorkspaceOwnershipError("PR 충돌 복구 worktree가 존재하지 않습니다.")
    snapshot = capture_git_snapshot(worktree)
    ignored = frozenset(_ignored_worktree_paths(worktree))
    dirty = sorted(path for path in snapshot.dirty if path not in ignored)
    flags = sorted(
        path for path, entries in snapshot.index.items()
        if _non_default_index_flags(entries)
    )
    if snapshot.branch != branch or snapshot.head != head_oid or dirty or flags or snapshot.operations:
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 차단 해제 전 관리 worktree 상태 불일치: "
            + "; ".join(filter(None, (
                f"branch {snapshot.branch} (expected {branch})" if snapshot.branch != branch else "",
                f"HEAD {snapshot.head} (expected {head_oid})" if snapshot.head != head_oid else "",
                "게시 가능 변경 " + ", ".join(dirty[:8]) if dirty else "",
                "index flags " + ", ".join(flags[:8]) if flags else "",
                "Git operation " + ", ".join(sorted(snapshot.operations)) if snapshot.operations else "",
            )))
        )
    remote = git(
        repo_path, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
    ).stdout.splitlines()
    if len(remote) != 1 or remote[0].split("\t", 1)[0] != head_oid:
        raise PublicationSafetyError(
            "PR 충돌 복구 차단 해제 전 원격 branch가 관측한 PR head와 일치하지 않습니다."
        )


def _is_pr_conflict_preflight_block(
    project: sqlite3.Row,
    plan: sqlite3.Row | None,
) -> bool:
    if plan is None:
        return False
    return bool(
        plan["blocked_reason"] == PR_CONFLICT_PREFLIGHT_BLOCK_REASON
        or str(project["last_error"] or "").startswith(
            PR_CONFLICT_PREFLIGHT_ERROR_PREFIX
        )
    )


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
        repo_path, state_dir, str(project["base_branch"]), branch
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
        journal_state_dir=state_dir,
        journal_owner=f"plan:{plan['plan_id']}",
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
    if remote_oid is None and plan['publication_remote_oid']:
        raise PublicationSafetyError(
            '저장된 게시 branch가 원격에서 삭제돼 새 branch 생성으로 복구할 수 없습니다.'
        )
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
        live = git(repo_path, 'ls-remote', '--heads', 'origin',
                   f'refs/heads/{branch}', check=False)
        if (live.returncode or
                [line.split() for line in live.stdout.splitlines() if line.strip()]
                != [[remote_oid, f'refs/heads/{branch}']]):
            raise PublicationSafetyError(
                'push 직전 원격 branch가 저장된 lease OID와 다릅니다.'
            )
        if plan['pr_url'] or pr_for_branch(repo_path, branch) is not None:
            raise PublicationSafetyError(
                'PR이 있는 branch를 checkpoint 복구 강제 push할 수 없습니다.'
            )
        push_arguments.insert(
            1,
            f"--force-with-lease=refs/heads/{branch}:{remote_oid}",
        )
    else:
        live = git(repo_path, 'ls-remote', '--heads', 'origin',
                   f'refs/heads/{branch}', check=False)
        if live.returncode or live.stdout.strip():
            raise PublicationSafetyError(
                '새 branch push 직전 원격 ref가 생성되거나 조회에 실패했습니다.'
            )

    pushed = git(worktree, *push_arguments, check=False)
    if pushed.returncode:
        raise RuntimeError(f"publication branch push 실패:\n{pushed.stdout[-8000:]}")

    _recorded_base_fetch(repo_path, state_dir, branch, owner="project")
    confirmed_oid = remote_branch_oid(repo_path, branch)
    if confirmed_oid != local_oid:
        raise RuntimeError(
            "publication branch push 결과를 원격에서 확인할 수 없습니다."
        )
    confirmed = git(repo_path, 'ls-remote', '--heads', 'origin',
                    f'refs/heads/{branch}', check=False)
    if (confirmed.returncode or
            [line.split() for line in confirmed.stdout.splitlines() if line.strip()]
            != [[local_oid, f'refs/heads/{branch}']]):
        raise RuntimeError('publication branch push의 새 OID를 원격에서 확인할 수 없습니다.')
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
        state_dir,
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

    _recorded_base_fetch(repo_path, state_dir, branch, owner="project")
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
        repo_path, state_dir, str(project["base_branch"]), branch
    )
    if remote_branch_oid(repo_path, branch) != local_oid:
        return False
    runtime_state.RuntimeStateRepository(connection).open_pull_request(
        repo,
        publication_remote_oid=local_oid,
        pr_head_oid=local_oid,
        pr_conflict_recovery=0,
        pr_conflict_head_oid=None,
        pr_conflict_base_oid=None,
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


def _owned_rebase_state_dir(repo: str, plan_id: str, worktree: Path) -> Path | None:
    """Legacy recovery calls can be journaled only for a deterministic plan path."""
    if not re.fullmatch(r"G-\d+", plan_id) or len(worktree.parents) < 3:
        return None
    state_dir = worktree.parents[2]
    _branch, expected = expected_execplan_workspace(state_dir, repo, plan_id)
    return state_dir if _canonical_path(expected) == _canonical_path(worktree) else None


def _owned_rebase_metadata(worktree: Path) -> dict[str, str] | None:
    for directory in ("rebase-merge", "rebase-apply"):
        values = {}
        for name in ("orig-head", "head-name", "onto"):
            result = git(worktree, "rev-parse", "--git-path", f"{directory}/{name}",
                         check=False)
            if result.returncode:
                break
            path = Path(result.stdout.strip())
            if not path.is_absolute():
                path = worktree / path
            if not path.is_file():
                break
            values[name] = path.read_text(encoding="utf-8").strip()
        if len(values) == 3:
            return values
    return None


def _owned_rebase_journal_state(worktree: Path, branch: str) -> dict[str, object]:
    snapshot = capture_git_snapshot(worktree)
    metadata = _owned_rebase_metadata(worktree)
    rebase_head = None
    if metadata is not None:
        result = git(worktree, 'rev-parse', '--verify', 'REBASE_HEAD', check=False)
        if result.returncode == 0:
            rebase_head = result.stdout.strip()
    return json.loads(json.dumps({
        "head": snapshot.head, "branch": snapshot.branch,
        "ref": snapshot.refs.get(f"refs/heads/{branch}"),
        "index": snapshot.index, "dirty": snapshot.dirty,
        "file_evidence": rebase_file_evidence.capture(worktree, snapshot.head, snapshot.dirty),
        "registration": snapshot.worktrees.get(str(_canonical_path(worktree))),
        "operations": snapshot.operations,
        "metadata": metadata, "rebase_head": rebase_head,
        "unmerged": bool(git(worktree, "ls-files", "-u", "-z").stdout),
        "index_clean": git(worktree, "diff", "--cached", "--quiet",
                           check=False).returncode == 0,
    }, sort_keys=True))


def _finish_owned_direct_rebase(
    state_dir: Path, connection: sqlite3.Connection, repo_path: Path,
    repo: str, plan_id: str, worktree: Path, branch: str,
    *, allow_equal: bool = False, agent_session_id: str | None = None,
) -> bool:
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, plan_id,
    )

    def replay_proof(intent, observed):
        return manager_git_journal.rebase_replay_proof(
            git, state_dir, intent, observed,
        )

    def validate(intent: Mapping[str, object], observed: Mapping[str, object]):
        expected = intent["expected"]
        current_pr_head = None
        if expected.get('evidence_version') == 2:
            try:
                row = connection.execute(
                    'SELECT pr_head_oid, pr_conflict_head_oid FROM projects WHERE repo = ?',
                    (repo,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is not None:
                current_pr_head = str(row['pr_head_oid'] or row['pr_conflict_head_oid'] or '') or None
        if (
            intent["owner"] != f"plan:{plan_id}"
            or expected["branch"] != branch
            or expected.get("agent_session_id") != agent_session_id
            or checkpoint.operation != "rebase"
            or checkpoint.expected_head != expected["before_head"]
            or checkpoint.target_base_oid != expected["target_base_oid"]
            or checkpoint.observed_remote_oid != expected["remote_oid"]
            or (expected.get('evidence_version') == 2 and (
                (checkpoint.snapshot or {}).get('rebase_run_id') != expected.get('run_id')
                or (checkpoint.snapshot or {}).get('rebase_source_commits') != expected.get('source_commits')
                or (checkpoint.snapshot or {}).get('rebase_remote_oid') != expected.get('remote_oid')
                or (checkpoint.snapshot or {}).get('rebase_pr_head_oid') != expected.get('pr_head_oid')
                or current_pr_head != expected.get('pr_head_oid')
                or checkpoint.generation < expected.get('checkpoint_generation', -1)
            ))
        ):
            raise manager_git_journal.MutationEvidenceError(
                "Manager rebase checkpoint 또는 원격 OID가 다릅니다."
            )
        if observed["metadata"] is None:
            marker = manager_git_journal.rebase_completion_matches(
                git, repo_path, branch=branch, head=observed["head"],
                identity=intent["id"], target_base_oid=expected["target_base_oid"],
            )
            if (not marker and expected.get('evidence_version') == 2
                    and observed == intent['before']):
                marker = manager_git_journal.successful_rebase_invocation(
                    state_dir, intent['repo'], intent['target'], intent['id'],
                )
            patches = (False if expected.get('evidence_version') == 2 else
                       checkpoint_ownership.rebase_preserves_commits(
                           repo_path, expected["before_head"], observed["head"],
                           expected["target_base_oid"], git,
                       ))
        else:
            marker = manager_git_journal.rebase_conflict_matches(
                git, worktree, head=observed["head"], identity=intent["id"],
            )
            patches = False
        ancestor = git(worktree, "merge-base", "--is-ancestor",
                       expected["target_base_oid"], observed["head"], check=False)
        replay_verified = False
        if expected.get('evidence_version') == 2:
            replay_proof(intent, observed)
            replay_verified = True
        return manager_git_journal.verified_rebase_result(
            intent, observed, current_remote_oid=(
                manager_git_journal.live_remote_branch_oid(git, repo_path, branch)
                if expected.get('evidence_version') == 2 else remote_branch_oid(repo_path, branch)
            ),
            target_ancestor=ancestor.returncode == 0,
            patches_preserved=patches, reflog_marker=marker,
            autostash_owned=bool((checkpoint.snapshot or {}).get("stash_oid")),
            replay_verified=replay_verified,
        )

    return manager_git_journal.complete_rebase(
        state_dir, repo=str(_canonical_path(repo_path)),
        target=str(_canonical_path(worktree)),
        observe=lambda: _owned_rebase_journal_state(worktree, branch),
        validate=validate,
        allow_equal=allow_equal,
        replay_proof=lambda intent, observed: (
            replay_proof(intent, observed)
            if intent['expected'].get('evidence_version') == 2 else None
        ),
    )


def _journaled_direct_rebase(
    connection: sqlite3.Connection, repo_path: Path, repo: str, plan_id: str,
    worktree: Path, branch: str, target_oid: str, *, autostash: bool = False,
    agent_session_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    state_dir = _owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None:
        return git(worktree, "rebase", *(("--autostash",) if autostash else ()),
                   target_oid, check=False)
    before = _owned_rebase_journal_state(worktree, branch)
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    remote_oid = remote_branch_oid(repo_path, branch)
    if current.observed_remote_oid not in (None, remote_oid):
        raise manager_git_journal.MutationEvidenceError(
            "Manager rebase 전 게시 원격 OID가 checkpoint와 다릅니다."
        )
    if manager_git_journal.live_remote_branch_oid(git, repo_path, branch) != remote_oid:
        raise manager_git_journal.MutationEvidenceError(
            'Manager rebase start: live remote differs from fetched OID'
        )
    if current.operation == "rebase":
        if (current.expected_head != before["head"]
                or current.target_base_oid != target_oid
                or current.observed_remote_oid != remote_oid):
            raise manager_git_journal.MutationEvidenceError(
                "Manager rebase 기존 checkpoint가 현재 작업과 다릅니다."
            )
    else:
        try:
            row = connection.execute(
                'SELECT pr_head_oid, pr_conflict_head_oid FROM projects WHERE repo = ?',
                (repo,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        pr_head_oid = (str(row['pr_head_oid'] or row['pr_conflict_head_oid'] or '')
                       or None) if row is not None else None
        if pr_head_oid and pr_head_oid != remote_oid:
            raise manager_git_journal.MutationEvidenceError(
                'Manager rebase start: PR head differs from remote OID'
            )
        source_base, source_commits = managed_rebase_evidence.source_commits(
            git, worktree, before['head'], target_oid,
        )
        run_id = secrets.token_hex(16)
        checkpoint.record(
            repo, plan_id, expected_generation=current.generation,
            operation="rebase", expected_head=before["head"],
            target_base_oid=target_oid, observed_remote_oid=remote_oid,
            snapshot={**(current.snapshot or {}),
                      'rebase_evidence_version': managed_rebase_evidence.EVIDENCE_VERSION,
                      'rebase_run_id': run_id,
                      'rebase_source_base': source_base,
                      'rebase_source_commits': source_commits,
                      'rebase_pr_head_oid': pr_head_oid,
                      'rebase_remote_oid': remote_oid},
        )
    current = checkpoint.get(repo, plan_id)
    snapshot = current.snapshot or {}
    token = manager_git_journal.begin_rebase(
        state_dir, owner=f"plan:{plan_id}", repo=str(_canonical_path(repo_path)),
        target=str(_canonical_path(worktree)), before=before,
        expected={"branch": branch, "before_head": before["head"],
                  "target_base_oid": target_oid, "remote_oid": remote_oid,
                  "affected_paths": manager_git_journal.rebase_affected_paths(
                      git, worktree, before["head"], target_oid,
                  ),
                  "agent_session_id": agent_session_id,
                  **({"evidence_version": managed_rebase_evidence.EVIDENCE_VERSION,
                      "run_id": snapshot['rebase_run_id'],
                      "source_base": snapshot['rebase_source_base'],
                      "source_commits": snapshot['rebase_source_commits'],
                      "pr_head_oid": snapshot.get('rebase_pr_head_oid'),
                      "checkpoint_generation": current.generation}
                     if snapshot.get('rebase_evidence_version') ==
                     managed_rebase_evidence.EVIDENCE_VERSION else {})},
    )
    environment = dict(os.environ)
    environment["GIT_REFLOG_ACTION"] = f"loop-manager-rebase:{token}"
    result = git(worktree, "rebase", *(("--autostash",) if autostash else ()),
                 target_oid, check=False, env=environment)
    if result.returncode == 0:
        manager_git_journal.record_rebase_invocation(
            state_dir, str(_canonical_path(repo_path)),
            str(_canonical_path(worktree)), token, returncode=result.returncode,
        )
    _finish_owned_direct_rebase(
        state_dir, connection, repo_path, repo, plan_id, worktree, branch,
        allow_equal=True, agent_session_id=agent_session_id,
    )
    return result


def _direct_rebase_history_proven(
    connection: sqlite3.Connection, repo_path: Path, repo: str, plan_id: str,
    worktree: Path, branch: str, before_head: str, after_head: str,
    target_base: str, agent_session_id: str | None,
) -> bool:
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, plan_id,
    )
    if (checkpoint.snapshot or {}).get('rebase_evidence_version') != 2:
        return checkpoint_ownership.rebase_preserves_commits(
            repo_path, before_head, after_head, target_base, git,
        )
    state_dir = _owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None:
        return False
    transitions = manager_git_journal.verified_managed_transitions(
        state_dir, str(_canonical_path(repo_path)), f'plan:{plan_id}',
        agent_session_id=agent_session_id, git=git,
    )
    return any(
        item['intent']['kind'] == 'managed_rebase'
        and item['intent']['expected'].get('branch') == branch
        and item['intent']['expected'].get('before_head') == before_head
        and item['intent']['expected'].get('target_base_oid') == target_base
        and item['result']['observed'].get('head') == after_head
        and item['result']['observed'].get('metadata') is None
        for item in transitions
    )


def _complete_direct_rebase_checkpoint(
    connection: sqlite3.Connection, repo: str, plan_id: str,
    worktree: Path, target_base_oid: str,
) -> None:
    repository = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = repository.get(repo, plan_id)
    if current.operation == "rebase":
        repository.record(
            repo, plan_id, expected_generation=current.generation,
            operation=None,
            expected_head=git(worktree, "rev-parse", "HEAD").stdout.strip(),
            target_base_oid=target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot=current.snapshot,
        )


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
    journal_state_dir: Path | None = None,
    journal_owner: str | None = None,
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
    if journal_state_dir is not None:
        if not journal_owner:
            raise WorkspaceOwnershipError("관리 worktree journal의 계획 identity가 없습니다.")
        manager_git_journal.recover_active(
            journal_state_dir, repo=str(_canonical_path(repo)),
            kind="managed_worktree_create", target=str(_canonical_path(worktree)),
            observe=lambda: _managed_worktree_transition_state(repo, worktree),
            marker=lambda intent: _managed_worktree_marker(
                journal_state_dir, intent, "create",
            ),
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
    if journal_state_dir is None:
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
        arguments = ("worktree", "add", str(worktree), branch)
        outcome = "recovered"
    elif allow_create_branch or ownership_reserved:
        if before_create is not None:
            before_create()
        start_point = expected_head or f"origin/{base}"
        arguments = ("worktree", "add", "-b", branch, str(worktree), start_point)
        outcome = "created"
    else:
        raise WorkspaceOwnershipError(
            f"저장된 worktree와 관리 branch를 모두 복구할 수 없습니다: "
            f"{worktree} ({branch})"
        )
    if journal_state_dir is None:
        git(repo, *arguments)
    else:
        before = _managed_worktree_transition_state(repo, worktree)
        if before != {"exists": False, "registration": None, "index": None}:
            raise WorkspaceOwnershipError("관리 worktree 생성 직전 등록 상태가 변경됐습니다.")
        head = git(
            repo, "rev-parse", "--verify",
            start_point if outcome == "created" else branch,
        ).stdout.strip()
        tree = git(repo, "rev-parse", "--verify", f"{head}^{{tree}}").stdout.strip()
        mutation_id = secrets.token_hex(16)
        lock_reason = f"loop-manager-worktree:{mutation_id}"
        expected = {
            "exists": True,
            "registration": {"head": head, "branch": f"refs/heads/{branch}",
                             "locked": lock_reason},
            "index": tree,
        }

        def apply(token: str) -> None:
            _write_managed_worktree_receipt(
                journal_state_dir, token, repo, worktree, before, expected,
                "create",
            )
            git(repo, "worktree", "add", "--lock", f"--reason={lock_reason}",
                *arguments[2:])

        manager_git_journal.execute(
            journal_state_dir, owner=journal_owner,
            kind="managed_worktree_create", repo=str(_canonical_path(repo)),
            target=str(_canonical_path(worktree)), before=before,
            expected=expected,
            observe=lambda: _managed_worktree_transition_state(repo, worktree),
            apply=apply,
            marker=lambda intent: _managed_worktree_marker(
                journal_state_dir, intent, "create",
            ),
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
    snapshot = {
        key: tuple(sorted(values))
        for key, values in entries.items()
    }
    repository_exclude = Path(
        os.fsdecode(
            git_bytes(worktree, "rev-parse", "--git-path", "info/exclude").strip()
        )
    )
    if not repository_exclude.is_absolute():
        repository_exclude = worktree / repository_exclude
    configured_global = subprocess.run(
        ["git", "-C", str(worktree), "config", "--path", "--get", "core.excludesFile"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if configured_global.returncode not in {0, 1}:
        raise RuntimeError("Git global exclude 경로를 확인할 수 없습니다.")
    if configured_global.returncode == 0:
        global_exclude = Path(os.fsdecode(configured_global.stdout.strip()))
    else:
        global_exclude = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git/ignore"
    if not global_exclude.is_absolute():
        global_exclude = worktree / global_exclude
    for name, path in (
        ("repository-exclude", repository_exclude),
        ("global-exclude", global_exclude),
    ):
        canonical = _canonical_path(path)
        signature = _git_operation_path_signature(canonical) if canonical.exists() else "missing"
        snapshot[f"ignore-source:{name}"] = (str(canonical), signature)
    return snapshot


def _with_proven_legacy_ignore_config(
    saved: GitSnapshot, current_config: dict[str, tuple[str, ...]]
) -> GitSnapshot:
    """Fill old snapshot keys only when external ignore sources have no rules."""

    keys = tuple(
        sorted(key for key in current_config if key.startswith("ignore-source:"))
    )
    if any(key.startswith("ignore-source:") for key in saved.config):
        return saved
    if len(keys) != 2:
        return saved
    for key in keys:
        path_text, signature = current_config[key]
        path = Path(path_text)
        if signature == "missing":
            if path.exists() or path.is_symlink():
                return saved
            continue
        try:
            if not path.is_file() or _git_operation_path_signature(path) != signature:
                return saved
            if any(
                line and not line.startswith(b"#")
                for line in path.read_bytes().splitlines()
            ):
                return saved
        except OSError:
            return saved
    return replace(
        saved,
        config={**saved.config, **{key: current_config[key] for key in keys}},
    )


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
    """Compatibility surface for callers of the persisted-scope policy."""
    return agent_scope_decision.metadata_changes_after_block(
        before, blocked, current, candidates
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
                "--exclude-standard",
                "-z",
            ),
        ),
    )
    for kind, paths in path_groups:
        for relative_path in paths:
            path_kinds.setdefault(relative_path, set()).add(kind)
    # A changed ignore rule cannot make a new file disappear from the scope
    # snapshot. Git's current exclude result is trustworthy only when the
    # matching tracked rule is still the version at HEAD.
    for relative_path in _nul_git_paths(
        worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    ):
        source = _ignore_rule_source(worktree, relative_path)
        if source is None:
            raise RuntimeError(f"ignored 경로의 Git 규칙을 확인할 수 없습니다: {relative_path}")
        if source == ".gitignore" or source.endswith("/.gitignore"):
            if source in path_kinds or source not in index:
                path_kinds.setdefault(relative_path, set()).add("untracked")
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


def _ignore_rule_source(worktree: Path, path: str) -> str | None:
    result = _handoff_run(
        [
            "git", "-C", str(worktree), "check-ignore",
            "--no-index", "-v", "-z", "--stdin",
        ],
        input=os.fsencode(path) + b"\0",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 1:
        return None
    fields = result.stdout.split(b"\0")
    if (
        result.returncode != 0
        or len(fields) != 5
        or fields[-1]
        or fields[3] != os.fsencode(path)
    ):
        raise RuntimeError(f"ignored 경로의 Git 규칙을 확인할 수 없습니다: {path}")
    return os.fsdecode(fields[0])


def _normalize_legacy_ignored_snapshot(
    worktree: Path, saved: GitSnapshot, current: GitSnapshot
) -> GitSnapshot:
    """Discard old untracked entries only when their starting ignore rule is proven."""

    dirty = dict(saved.dirty)
    for path, (kinds, _signature) in saved.dirty.items():
        if kinds != ("untracked",) or path in current.dirty:
            continue
        if path in saved.index or path in current.index:
            continue
        source = _ignore_rule_source(worktree, path)
        if source is None:
            continue
        if not (source == ".gitignore" or source.endswith("/.gitignore")):
            continue
        if source not in saved.index or saved.index.get(source) != current.index.get(source):
            continue
        if source in saved.dirty or source in current.dirty:
            continue
        old_rule = git(worktree, "rev-parse", f"{saved.head}:{source}", check=False)
        new_rule = git(worktree, "rev-parse", f"{current.head}:{source}", check=False)
        if old_rule.returncode or new_rule.returncode or old_rule.stdout != new_rule.stdout:
            continue
        dirty.pop(path)
    return replace(saved, dirty=dirty) if dirty != saved.dirty else saved


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


def _scope_snapshot_with_start_ignore_rules(
    worktree: Path, before: GitSnapshot, after: GitSnapshot
) -> GitSnapshot:
    """Keep paths hidden by a changed tracked ignore rule in scope."""

    if before.head == after.head:
        return after
    dirty = dict(after.dirty)
    for path in _nul_git_paths(
        worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    ):
        source = _ignore_rule_source(worktree, path)
        if source is None:
            raise AgentScopeError(f"ignored 경로의 Git 규칙을 확인할 수 없습니다: {path}")
        if source == ".gitignore" or source.endswith("/.gitignore"):
            old_rule = git(worktree, "rev-parse", f"{before.head}:{source}", check=False)
            new_rule = git(worktree, "rev-parse", f"{after.head}:{source}", check=False)
            if old_rule.returncode or new_rule.returncode or old_rule.stdout != new_rule.stdout:
                dirty[path] = (("untracked",), _path_signature(worktree, path))
    return replace(after, dirty=dirty) if dirty != after.dirty else after


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

    after = _scope_snapshot_with_start_ignore_rules(worktree, before, after)
    changes = agent_snapshot_changes(worktree, before, after)
    violations: list[str] = []
    violation_reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
    external_key = (
        str(_canonical_path(external_worktree))
        if external_worktree is not None
        else None
    )
    external_registration_preserved = bool(
        external_key is not None
        and external_key in before.worktrees
        and external_key in after.worktrees
    )
    external_base_ref = f"refs/heads/{base_branch}" if base_branch else None
    external_base_ref_owned = bool(
        external_registration_preserved
        and external_key is not None
        and external_base_ref is not None
        and any(
            f"branch={external_base_ref}" in snapshot.worktrees[external_key]
            for snapshot in (before, after)
        )
    )

    def add_violation(message: str, reason: BlockReasonCode) -> None:
        nonlocal violation_reason
        violations.append(message)
        if scoped_blocking.reason_scope(reason) is BlockScope.PROJECT:
            violation_reason = reason

    if before.head != after.head:
        add_violation(
            f"HEAD 변경 ({before.head} -> {after.head})",
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
        )
    if before.branch != after.branch:
        add_violation(
            f"branch 전환 ({before.branch or 'detached'} -> "
            f"{after.branch or 'detached'})",
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
        )

    if not before.shared_metadata_complete or not after.shared_metadata_complete:
        add_violation(
            "공유 Git metadata 기준 snapshot 누락",
            BlockReasonCode.REPOSITORY_STATE_UNSAFE,
        )
    else:
        # Codex owns this ephemeral namespace for turn capture/checkpoint
        # bookkeeping. It never identifies a published branch or a managed
        # workspace, so normalize both sides for current and persisted scope
        # snapshots while keeping every other repository ref protected.
        before_scope_refs = {
            key: value
            for key, value in before.refs.items()
            if not key.startswith("refs/codex/turn-diffs/")
        }
        after_scope_refs = {
            key: value
            for key, value in after.refs.items()
            if not key.startswith("refs/codex/turn-diffs/")
        }
        metadata_states = (
            (
                "공유 Git refs",
                before_scope_refs,
                after_scope_refs,
                allowed_ref_changes,
                BlockReasonCode.SHARED_REF_MUTATION,
            ),
            (
                "Git worktree 등록·HEAD·index·파일 상태",
                before.worktrees,
                after.worktrees,
                allowed_worktree_changes,
                BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION,
            ),
            (
                "Git config",
                before.config,
                after.config,
                frozenset(),
                BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
            ),
            (
                "Git operation 상태",
                before.operations,
                after.operations,
                frozenset(),
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
            ),
        )
        for label, before_state, after_state, allowed_changes, reason in metadata_states:
            effective_allowed_changes = allowed_changes
            if (
                label == "Git worktree 등록·HEAD·index·파일 상태"
                and external_registration_preserved
            ):
                # The registered project's primary checkout is not owned by the
                # managed Agent. Its local HEAD, branch, index and files are
                # evaluated by the source freshness/safe-fast-forward gates.
                # Requiring the entry on both sides still protects worktree
                # registration additions and removals.
                effective_allowed_changes |= frozenset((external_key,))
            elif (
                label == "공유 Git refs"
                and external_base_ref_owned
            ):
                # Moving the primary checkout on its configured base branch
                # necessarily moves this local ref. Other local branches,
                # remote refs and tags remain repository-wide protected state.
                effective_allowed_changes |= frozenset(
                    (external_base_ref,)
                )
            changed_keys = sorted(
                key
                for key in set(before_state) | set(after_state)
                if before_state.get(key) != after_state.get(key)
                and key not in effective_allowed_changes
            )
            if changed_keys:
                if label == "공유 Git refs":
                    owned_branch_ref = (
                        f"refs/heads/{before.branch}" if before.branch else None
                    )
                    unowned_changes = [
                        key for key in changed_keys if key != owned_branch_ref
                    ]
                    if unowned_changes:
                        reason = BlockReasonCode.SHARED_REF_MUTATION
                    else:
                        reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
                elif label == "Git worktree 등록·HEAD·index·파일 상태":
                    managed_worktree = str(_canonical_path(worktree))
                    unowned_changes = [
                        key for key in changed_keys if key != managed_worktree
                    ]
                    if external_key is not None and external_key in unowned_changes:
                        reason = BlockReasonCode.DEFAULT_WORKTREE_MUTATION
                    elif unowned_changes:
                        reason = BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION
                    else:
                        reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
                displayed = ", ".join(changed_keys[:8])
                remainder = len(changed_keys) - 8
                if remainder > 0:
                    displayed += f", 외 {remainder}개"
                add_violation(f"{label} 변경 ({displayed})", reason)

    for change in changes:
        if "index flags" in change.kinds:
            add_violation(
                f"{change.path} ({', '.join(change.kinds)})",
                BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
            )
            continue
        allowed = False
        if role_name == "planner":
            allowed = bool(
                change.path == "docs/FUTURE_IDEAS.md"
                or re.fullmatch(r"docs/plans/[^/]+\.md", change.path)
            )
        elif role_name == "maintenance_planner":
            allowed = bool(
                change.path == "docs/FUTURE_IDEAS.md"
                or re.fullmatch(r"docs/plans/[^/]+\.md", change.path)
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
            add_violation(
                f"{change.path} ({', '.join(change.kinds) or 'Git 상태'})",
                BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
            )

    if violations:
        raise AgentScopeError(
            f"{role_name} Git 범위 위반: " + "; ".join(violations),
            result_snapshot=after,
            reason_code=violation_reason,
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
    result = _handoff_run(
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
    if git(worktree, "ls-files", "-u", "-z").stdout:
        # Autostash can leave an unmerged index after rebase metadata vanishes.
        # Its stage OIDs are captured by the execution checkpoint, while this
        # ordinary baseline cannot represent them with write-tree.
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
        "SELECT agent_role, agent_snapshot, agent_input_fingerprint, plan_path "
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
        if (
            role_name == "worker" and input_fingerprint is not None
            and row["agent_input_fingerprint"] != input_fingerprint
        ):
            # A resumed pre-contract Worker scope may have saved its local
            # plan fingerprint. The new turn's remote input is authoritative.
            update_project(
                connection, repo, agent_input_fingerprint=input_fingerprint,
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
    try:
        _head, _branch, _index, dirty = _capture_local_worktree_state(worktree)
        operations = _git_operation_entries(worktree)
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 상태 확인 실패: {error}")
    if dirty or operations:
        return CleanupAssessment(
            "dirty", "index flag 또는 Git operation 변경: "
            + ", ".join(sorted((*dirty, *operations))),
        )

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
    repo: Path, worktree: Path, assessment: CleanupAssessment,
    *, state_dir: Path | None = None, owner: str | None = None,
) -> CleanupAssessment:
    if assessment.status != "safe":
        return assessment
    try:
        _head, _branch, _index, dirty = _capture_local_worktree_state(worktree)
        if dirty or _git_operation_entries(worktree):
            return CleanupAssessment("dirty", "worktree 제거 직전 권위 있는 변경이 있습니다.")
        arguments = ("--force",) if _ignored_worktree_paths(worktree) else ()
        if state_dir is None:
            git(repo, "worktree", "remove", *arguments, str(worktree))
        else:
            if not owner:
                raise WorkspaceOwnershipError("관리 worktree 정리 journal의 계획 identity가 없습니다.")
            before = _managed_worktree_transition_state(repo, worktree)
            head = before["registration"]["head"] if before["registration"] else None
            tree = git(repo, "rev-parse", "--verify", f"{head}^{{tree}}").stdout.strip()
            if (not before["exists"] or before["index"] != tree):
                raise WorkspaceOwnershipError("관리 worktree 정리 직전 index가 HEAD와 다릅니다.")
            expected = {"exists": False, "registration": None, "index": None}

            def marker(intent: Mapping[str, object]) -> bool:
                return _managed_worktree_marker(state_dir, intent, "remove")

            def apply(token: str) -> None:
                if _managed_worktree_transition_state(repo, worktree) != before:
                    raise WorkspaceOwnershipError("관리 worktree 정리 직전 상태가 변경됐습니다.")
                if before["registration"]["locked"]:
                    git(repo, "worktree", "unlock", str(worktree))
                git(repo, "worktree", "remove", *arguments, str(worktree))
                _write_managed_worktree_receipt(
                    state_dir, token, repo, worktree, before, expected, "remove",
                )

            manager_git_journal.execute(
                state_dir, owner=owner, kind="managed_worktree_remove",
                repo=str(_canonical_path(repo)),
                target=str(_canonical_path(worktree)), before=before,
                expected=expected,
                observe=lambda: _managed_worktree_transition_state(repo, worktree),
                apply=apply, marker=marker,
            )
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 제거 실패: {error}")
    return assessment


def _ensure_cleanup_pending_schema(connection: sqlite3.Connection) -> None:
    connection.execute(CLEANUP_PENDING_SCHEMA)
    connection.execute(MERGED_WORKTREE_CLEANUP_SCHEMA)
    connection.execute(MANAGER_BASE_FETCH_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cleanup_pending)")
    }
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE cleanup_pending ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 1"
        )
    fetch_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(manager_base_fetches)")
    }
    if "action_token" not in fetch_columns:
        connection.execute(
            "ALTER TABLE manager_base_fetches ADD COLUMN action_token TEXT"
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
            _recover_managed_worktree_removal(state_dir, repo_path, worktree)
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
            assessment = remove_cleanup_worktree(
                repo_path, worktree, assessment, state_dir=state_dir,
                owner=f"plan:{worktree.name}",
            )
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
        worker_session_binding=None,
        worker_session_transition=None,
        reviewer_session_id=None,
        worker_result=None,
        verify_result=None,
        review_result=None,
        planning_validation_error=None,
        planning_result_pending_validation=0,
        planning_result_completed=0,
        planning_result=None,
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
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> dict[str, object]:
    return runtime_state.clear_project_values(
        processed_sha=processed_sha,
        specification_fingerprint=specification_fingerprint,
        planning_fingerprint=planning_fingerprint,
    )


def _clear_active_in_current_transaction(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    try:
        runtime_state.RuntimeStateRepository(connection).clear(
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
            enqueue=False,
        )
    except runtime_state.RuntimeStateError as error:
        raise RuntimeError(
            "project planning 상태를 원자적으로 정리할 수 없습니다."
        ) from error


def clear_active(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    if connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is None:
        connection.commit()
        return
    runtime_state.RuntimeStateRepository(connection).clear(
        repo,
        processed_sha=processed_sha,
        specification_fingerprint=specification_fingerprint,
        planning_fingerprint=planning_fingerprint,
        enqueue=True,
    )
    connection.commit()


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


def _revision_execplan_source_fingerprint(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    """Fingerprint only current source documents; the plan body is classified separately."""

    entries = []
    for relative_path in json.loads(str(plan["sources"])):
        shown = git(
            repo_path,
            "show",
            f"{revision}:{relative_path}",
            check=False,
        )
        if shown.returncode and relative_path != "docs/PRODUCT.md":
            archive_path = execplan_catalog.requirement_archive_path(
                str(relative_path)
            )
            shown = git(
                repo_path,
                "show",
                f"{revision}:{archive_path}",
                check=False,
            )
        entries.append(
            (str(relative_path), shown.stdout if shown.returncode == 0 else None)
        )
    return _execplan_input_fingerprint(entries)


def _revision_execplan_body_hash(
    repo_path: Path,
    revision: str,
    path: str,
) -> str:
    shown = git(repo_path, "show", f"{revision}:{path}", check=False)
    if shown.returncode:
        raise RuntimeError(f"최신 ExecPlan 본문을 읽을 수 없습니다: {path}")
    return execution_revision.execplan_body_hash(shown.stdout)


def _revision_changed_paths(
    repo_path: Path,
    previous_revision: str,
    current_revision: str,
) -> tuple[str, ...]:
    if not previous_revision or previous_revision == current_revision:
        return ()
    changed = git(
        repo_path,
        "diff",
        "--name-only",
        "--find-renames",
        previous_revision,
        current_revision,
        "--",
        check=False,
    )
    if changed.returncode:
        raise RuntimeError("실행 base revision 차이를 확인할 수 없습니다.")
    return tuple(
        sorted(
            {
                line.strip()
                for line in changed.stdout.splitlines()
                if line.strip()
            }
        )
    )


def unmet_execplan_dependencies(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[str, ...]:
    """Return every direct or indirect dependency not confirmed MERGED."""

    rows = connection.execute(
        "SELECT plan_id, depends_on, status FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    graph = {
        str(row["plan_id"]): (
            tuple(str(item) for item in json.loads(str(row["depends_on"]))),
            str(row["status"]),
        )
        for row in rows
    }
    if plan_id not in graph:
        raise RuntimeError(f"dependency를 확인할 ExecPlan이 없습니다: {plan_id}")
    unmet: set[str] = set()
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current in visited:
            return
        visited.add(current)
        dependencies, _status = graph[current]
        for dependency in dependencies:
            stored = graph.get(dependency)
            if stored is None or stored[1] != "MERGED":
                unmet.add(dependency)
            if stored is not None:
                visit(dependency)

    visit(plan_id)
    return tuple(sorted(unmet))


def _revision_review_issue_fingerprint(
    repo_path: Path,
    revision: str,
    review_result: object,
) -> str | None:
    paths = _review_issue_paths(review_result)
    if not paths:
        return None
    entries = []
    for relative_path in paths:
        shown = git(repo_path, "show", f"{revision}:{relative_path}", check=False)
        entries.append(
            (relative_path, shown.stdout if shown.returncode == 0 else None)
        )
    return _execplan_input_fingerprint(entries)


def _blocked_validation_base_revision(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
) -> str:
    project = connection.execute(
        "SELECT active_sha FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    return str(
        (project["active_sha"] if project else None)
        or plan["active_sha"]
        or plan["current_revision_sha"]
        or plan["plan_sha"]
        or ""
    )


def _blocked_validation_input_fingerprints(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
) -> tuple[str, str]:
    """Capture comparable base and worktree inputs at the blocking boundary."""

    worktree_fingerprint = _worktree_execplan_input_fingerprint(worktree, plan)
    revision = _blocked_validation_base_revision(connection, repo, plan)
    if not revision:
        return worktree_fingerprint, worktree_fingerprint
    resolved = git(
        repo_path,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        check=False,
    )
    if resolved.returncode:
        return worktree_fingerprint, worktree_fingerprint
    return (
        _revision_execplan_input_fingerprint(repo_path, revision, plan),
        worktree_fingerprint,
    )


def _execplan_metadata_identity_error(
    repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
    subject: str,
) -> str | None:
    del repo_path
    if current["id"] != plan["plan_id"]:
        return f"{subject}의 ID가 reconciled catalog identity와 다릅니다."
    reconciled = _current_execplan_metadata(plan, current)
    metadata_fields = (
        "title",
        "sources",
        "depends_on",
        "complexity",
        "risk",
        "priority",
    )
    if any(current[field] != reconciled[field] for field in metadata_fields):
        return f"{subject} metadata가 reconciled catalog와 다릅니다."
    return None


def _current_execplan_metadata(
    plan: sqlite3.Row,
    fallback: Mapping[str, object],
) -> dict[str, object]:
    """Expose the reconciled declaration cache without publication-audit fields."""

    complexity = plan["current_complexity"] or fallback.get("complexity")
    risk = plan["current_risk"] or fallback.get("risk")
    return {
        "id": str(plan["plan_id"]),
        "title": str(plan["title"]),
        "sources": list(json.loads(str(plan["sources"]))),
        "depends_on": list(json.loads(str(plan["depends_on"]))),
        "complexity": str(complexity),
        "risk": str(risk),
        "priority": str(plan["priority"]),
    }


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
    *,
    rebase_definition: execution_revision.DefinitionSnapshot | None = None,
) -> dict[str, object]:
    try:
        current = validate_execplan(worktree, relative_path)
    except RuntimeError as error:
        raise ExecPlanMetadataError(str(error)) from error
    comparison_plan = plan
    if rebase_definition is not None:
        # A paused rebase still contains its applied declaration. Scheduling,
        # profile and dependency fields may lag during conflict resolution;
        # all other identity fields and all normal validation remain strict.
        if rebase_definition.path != relative_path:
            raise ExecPlanMetadataError("rebase 기준 ExecPlan 경로가 현재 작업 경로와 다릅니다.")
        comparison_plan = dict(plan)
        for field, column in (
            ('priority', 'priority'),
            ('complexity', 'current_complexity'),
            ('risk', 'current_risk'),
        ):
            applied = getattr(rebase_definition, field)
            if current[field] == applied:
                comparison_plan[column] = applied
        if tuple(current['depends_on']) == rebase_definition.depends_on:
            comparison_plan['depends_on'] = json.dumps(rebase_definition.depends_on)
    error = _execplan_metadata_identity_error(
        repo_path,
        current,
        comparison_plan,
        "활성 ExecPlan",
    )
    if error is not None:
        raise ExecPlanMetadataError(error)
    if relative_path != str(plan["path"]):
        raise ExecPlanMetadataError("활성 ExecPlan 경로가 reconciled catalog와 다릅니다.")
    return _current_execplan_metadata(plan, current)


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
    rebase_definition: execution_revision.DefinitionSnapshot | None = None,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
) -> dict[str, object]:
    """Validate one Worker-controlled operation before Manager Git writes."""

    if rebase_definition is not None and not allow_rebase:
        raise ExecPlanMetadataError("rebase 기준 선언은 충돌 복구 중에만 사용할 수 있습니다.")
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
        **({'rebase_definition': rebase_definition} if allow_rebase else {}),
    )
    validate_managed_worktree(
        repo_path,
        worktree,
        branch,
        allow_rebase=allow_rebase,
    )
    return plan


GUIDANCE_BLOCK_REASON = BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED.value


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
    catalog_plan: Mapping[str, object],
) -> bool:
    if not is_loop_manager_repository(repo_path):
        return False
    current_revision_sha = (
        catalog_plan["current_revision_sha"]
        if "current_revision_sha" in catalog_plan.keys()
        else None
    )
    plan_sha = str(
        current_revision_sha or catalog_plan["plan_sha"] or ""
    )
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
            r"(?im)^-\s+(?:공통\s+)?`docs/PLANS\.md`(?:에|를|을|의|와|과|\s)"
            r".*(?:변경|문서화|갱신|수정|설명|change|update|document|describe)",
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
        _final_block_recovery_decision(
            connection, repo, plan, "WORKING",
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, message,
            document.structure_hash, None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=GUIDANCE_BLOCK_REASON,
            evidence=message,
            release_fingerprint=document.structure_hash,
            plan_values={
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "guidance_plan_hash": document.structure_hash,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
            },
            project_values={
                "phase": "PLAN_BLOCKED",
                "last_error": message,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
            },
        )


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


def _plan_block_selected_for_release(
    connection: sqlite3.Connection,
    repo: str,
    *,
    blocked_reason: str | None = None,
    require_fingerprint: bool = False,
) -> sqlite3.Row | None:
    """Return the mirrored block, or hydrate one uniquely matching block."""

    def matches(candidate: sqlite3.Row | None) -> bool:
        return bool(
            candidate is not None
            and candidate["status"] == "PLAN_BLOCKED"
            and (blocked_reason is None or candidate["blocked_reason"] == blocked_reason)
            and (not require_fingerprint or candidate["blocked_fingerprint"])
        )

    plan = project_execplan(connection, repo)
    if matches(plan):
        return plan
    clauses = ["repo = ?", "status = 'PLAN_BLOCKED'"]
    parameters: list[object] = [repo]
    if blocked_reason is not None:
        clauses.append("blocked_reason = ?")
        parameters.append(blocked_reason)
    if require_fingerprint:
        clauses.append("blocked_fingerprint IS NOT NULL")
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE " + " AND ".join(clauses) + " ORDER BY plan_id",
        parameters,
    ).fetchall()
    if len(rows) != 1:
        return None
    hydrate_execplan_project(connection, repo, rows[0])
    return rows[0]


def resume_guidance_approval_if_decided(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
) -> bool:
    plan = _plan_block_selected_for_release(
        connection,
        repo,
        blocked_reason=GUIDANCE_BLOCK_REASON,
    )
    if plan is None:
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
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
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
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                str(plan["plan_id"]),
                plan_values={
                    "status": "IMPLEMENTING",
                    "blocked_reason": None,
                    "blocked_fingerprint": None,
                    "blocked_worktree_fingerprint": None,
                    "last_error": None,
                },
                project_values={
                    "phase": "WORKING",
                    "last_error": None,
                },
                enqueue=False,
            )
            scoped_blocking.clear_plan_block(
                connection, repo, str(plan["plan_id"])
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
        # Manager rebase/verify may already have cleared the Agent scope.
        # Freeze the diagnosed state so only the User Decision can change.
        _final_block_recovery_decision(
            connection, repo, plan, "WORKING",
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, message,
            document.structure_hash, None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=GUIDANCE_BLOCK_REASON,
            evidence=message,
            release_fingerprint=document.structure_hash,
            plan_values={
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "guidance_plan_hash": document.structure_hash,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
                "agent_role": "worker",
                "agent_snapshot": result_snapshot,
                "agent_baseline": None,
                "agent_recovery": None,
                "agent_input_plan": None,
                "agent_result_plan": None,
                "agent_input_fingerprint": None,
            },
            clear_projection=True,
        )


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
    runtime_state.RuntimeStateRepository(connection).update_plan(
        repo,
        str(plan["plan_id"]),
        plan_values={"guidance_plan_hash": document.structure_hash},
        enqueue=False,
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
        runtime_state.RuntimeStateRepository(connection).transition(
            repo,
            plan_id,
            plan_values={"last_error": error},
            project_values={"last_error": error},
            enqueue=False,
        )
        connection.execute(
            "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND plan_id = ?",
            (error, repo, plan_id),
        )
        notifications.enqueue_current_project_events(connection, repo)


def block_active_execplan(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    error: str,
    verify_result: str,
    *,
    reason: BlockReasonCode = BlockReasonCode.PLAN_INPUT_INVALID,
    preserved_agent_result_snapshot: str | None = None,
    recovery_decision: recovery_runtime.RecoveryDecision | None = None,
    release_fingerprint: str | None = None,
    worker_result: str | None = None,
) -> None:
    project = connection.execute(
        "SELECT plan_path, phase, agent_role, agent_snapshot, agent_input_fingerprint, "
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
        plan = project_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    blocked_worktree_fingerprint = _worktree_execplan_input_fingerprint(
        worktree, plan
    )
    fingerprint = str(
        project["agent_input_fingerprint"] or blocked_worktree_fingerprint
    )
    block_fingerprint = release_fingerprint or fingerprint
    blocked_review_fingerprint = None
    if reason is BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK:
        revision = str(plan["active_sha"] or plan["current_revision_sha"] or "")
        block_fingerprint = _revision_execplan_input_fingerprint(
            Path(repo), revision, plan
        )
        blocked_review_fingerprint = _revision_review_issue_fingerprint(
            Path(repo), revision, error
        )
    agent_result_snapshot = preserved_agent_result_snapshot
    if agent_result_snapshot is None:
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
    plan_values = {
        "blocked_fingerprint": block_fingerprint,
        "blocked_worktree_fingerprint": blocked_worktree_fingerprint,
        "blocked_review_fingerprint": blocked_review_fingerprint,
        "last_error": error,
        "verify_result": verify_result,
        "agent_result_snapshot": agent_result_snapshot,
        "agent_result_plan": agent_result_plan,
        "agent_manager_snapshot": None,
    }
    if worker_result is not None:
        plan_values["worker_result"] = worker_result
    plan_id = str(plan["plan_id"])
    with runtime_state._transaction(connection):
        decision = _final_block_recovery_decision(
            connection, repo, plan, str(project["phase"] or "") if project else "",
            reason, error, fingerprint, recovery_decision,
        )
        if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                plan_id,
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=block_fingerprint,
                plan_values=plan_values,
                clear_projection=True,
            )
        else:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values={
                    "verify_result": verify_result,
                    "agent_result_snapshot": agent_result_snapshot,
                    "agent_result_plan": agent_result_plan,
                    "agent_manager_snapshot": None,
                },
                enqueue=False,
            )
            scoped_blocking.record_project_block(
                connection,
                repo,
                reason,
                error,
                fingerprint,
                plan_id=plan_id,
                previous_phase=str(project["phase"] or "") or None,
                recovery_decision=decision,
            )
            notifications.enqueue_current_project_events(connection, repo)


def block_verify_timeout_limit(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    worker_result: str,
    evidence: str,
    verify_result: str,
) -> str:
    observed = re.search(
        r"전체 verify 시간 초과: verify_timeout_seconds=([1-9][0-9]*)",
        verify_result,
    )
    if observed is None:
        raise RuntimeError("verify 시간 초과 상한 증거가 없습니다.")
    timeout = int(observed.group(1))
    detail = (
        f"전체 verify에 필요한 검증이 진행 중이었으나 현재 상한 "
        f"verify_timeout_seconds={timeout}초가 부족합니다. "
        f"[loop] verify_timeout_seconds를 늘리세요.\n"
        f"진행 근거: {redact_secrets(evidence)[-1800:]}"
    )
    block_active_execplan(
        connection, repo, worktree, detail, verify_result,
        reason=BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
        release_fingerprint=str(timeout), worker_result=worker_result,
    )
    return detail


def _final_block_recovery_decision(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    phase: str,
    reason: BlockReasonCode,
    error: str,
    fingerprint: str,
    decision: recovery_runtime.RecoveryDecision | None,
) -> recovery_runtime.RecoveryDecision:
    """Accept only a final typed decision in the blocking transaction."""

    if decision is None:
        observation = recovery_runtime.RecoveryObservation(
            reason=recovery_runtime.reason_from_block(reason),
            repo=repo, plan_id=str(plan["plan_id"]), phase=phase,
            plan_revision=str(plan["current_revision_sha"] or plan["plan_sha"] or ""),
            base_revision=str(plan["active_sha"] or ""),
            fingerprint=fingerprint,
            progress_marker=str(plan["active_sha"] or ""), detail=error,
            user_targets=recovery_state.block_user_targets(
                connection, repo, str(plan["plan_id"]), reason,
                str(plan["path"]),
            ),
        )
        decision = recovery_state.RecoveryRepository(connection).observe(
            observation, final_block_reason=reason,
        ).decision
    recovery_runtime.require_final(decision)
    if decision.block_reason != reason or decision.observation.repo != repo or decision.observation.plan_id != str(plan["plan_id"]):
        raise ValueError("recovery decision and block target disagree")
    return decision


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
            if stage == "verify" and failure[2] > MAX_VALIDATION_FINGERPRINT_REPEATS
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
        blocked_input, blocked_worktree_input = (
            _blocked_validation_input_fingerprints(
                connection,
                Path(repo),
                repo,
                worktree,
                plan,
            )
        )
        blocked_review_input = None
        if stage == "review":
            blocked_revision = _blocked_validation_base_revision(
                connection, repo, plan
            )
            if blocked_revision:
                blocked_review_input = _revision_review_issue_fingerprint(
                    Path(repo), blocked_revision, output
                )
    else:
        prefix = "전체 검증 실패" if stage == "verify" else "Review 실패"
        error = f"{prefix}:\n{output[-8000:]}"
        blocked_input = None
        blocked_worktree_input = None
        blocked_review_input = None

    plan_values: dict[str, object] = {
        history_column: serialized_history,
        "validated_base_sha": None,
        "status": "PLAN_BLOCKED" if blocked else "IMPLEMENTING",
        "last_error": error,
    }
    project_values: dict[str, object] = {
        "validated_base_sha": None,
        "phase": "WORKING",
        "last_error": error,
    }
    if stage == "verify":
        plan_values["verify_result"] = output
        project_values["verify_result"] = output
    else:
        plan_values["review_result"] = output
        project_values["review_result"] = output
        if level_decision is not None:
            plan_values["effective_worker_level"] = level_decision.effective_level
            plan_values["review_level_history"] = serialized_level_history
    if blocked:
        plan_values.update(
            blocked_fingerprint=blocked_input,
            blocked_worktree_fingerprint=blocked_worktree_input,
            blocked_review_fingerprint=blocked_review_input,
        )
        try:
            plan_values["agent_result_snapshot"] = capture_git_snapshot(worktree).serialize()
        except (OSError, RuntimeError):
            # Missing evidence must fail closed in the checkpoint assessor.
            plan_values["agent_result_snapshot"] = None
        reason = (
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED
            if stage == "verify"
            else BlockReasonCode.REVIEW_RETRY_EXHAUSTED
        )
        with runtime_state._transaction(connection):
            recovery_decision = _final_block_recovery_decision(
                connection, repo, plan, stage.upper(), reason, error,
                fingerprint, None,
            )
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                str(plan["plan_id"]),
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=blocked_input,
                plan_values=plan_values,
                clear_projection=True,
            )
    else:
        runtime_state.RuntimeStateRepository(connection).transition(
            repo,
            str(plan["plan_id"]),
            plan_values=plan_values,
            project_values=project_values,
            enqueue=False,
        )
    return ValidationFailureDecision(
        stage,
        fingerprint,
        occurrences,
        blocked,
        recovery_decision if blocked else None,
    )


def block_active_execplan_from_revision(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    error: str,
    verify_result: str = "",
    *,
    reason: BlockReasonCode = BlockReasonCode.PLAN_INPUT_INVALID,
    preserved_agent_result_snapshot: str | None = None,
    blocked_reason: str | None = None,
    recovery_decision: recovery_runtime.RecoveryDecision | None = None,
) -> None:
    """Block an active plan when its saved worktree is not safe to read."""

    plan = active_execplan(connection, repo) or project_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    project = connection.execute(
        "SELECT phase, agent_input_fingerprint FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    fingerprint = str(
        (project["agent_input_fingerprint"] if project else None)
        or _revision_execplan_input_fingerprint(repo_path, revision, plan)
    )
    plan_values: dict[str, object] = {
        "blocked_fingerprint": fingerprint,
        "blocked_worktree_fingerprint": None,
        "blocked_review_fingerprint": None,
        "last_error": error,
        "verify_result": verify_result,
        "agent_result_snapshot": preserved_agent_result_snapshot,
        "agent_manager_snapshot": None,
    }
    if blocked_reason is not None:
        plan_values["blocked_reason"] = blocked_reason
    plan_id = str(plan["plan_id"])
    with runtime_state._transaction(connection):
        decision = _final_block_recovery_decision(
            connection, repo, plan,
            str(project["phase"] or "") if project else "", reason, error,
            fingerprint, recovery_decision,
        )
        if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                plan_id,
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=fingerprint,
                plan_values=plan_values,
                clear_projection=True,
            )
        else:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values={
                    "verify_result": verify_result,
                    "agent_result_snapshot": preserved_agent_result_snapshot,
                    "agent_manager_snapshot": None,
                },
                enqueue=False,
            )
            scoped_blocking.record_project_block(
                connection,
                repo,
                reason,
                error,
                fingerprint,
                plan_id=plan_id,
                previous_phase=(
                    str(project["phase"] or "") or None if project else None
                ),
                recovery_decision=decision,
            )
            notifications.enqueue_current_project_events(connection, repo)

def block_persisted_agent_scope_violation(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    row: sqlite3.Row,
    detail: str,
    result_snapshot: GitSnapshot | None = None,
    reason: BlockReasonCode = BlockReasonCode.REPOSITORY_STATE_UNSAFE,
) -> str:
    """Persist a pre-resume scope violation without discarding Agent state."""

    message = f"Agent 범위 선검증 실패:\n{detail}"
    if result_snapshot is not None:
        preserved_snapshot = result_snapshot.serialize()
    elif row["agent_result_snapshot"]:
        preserved_snapshot = str(row["agent_result_snapshot"])
    else:
        preserved_snapshot = str(row["agent_snapshot"])
    plan = active_execplan(connection, repo) or project_execplan(connection, repo)
    if plan is None:
        if (
            row["phase"] == "PLANNING"
            and scoped_blocking.reason_scope(reason) is BlockScope.PLAN
        ):
            update_project(
                connection,
                repo,
                planning_validation_error=detail,
                last_error=message,
                agent_result_snapshot=preserved_snapshot,
            )
        else:
            if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
                reason = BlockReasonCode.REPOSITORY_STATE_UNSAFE
            with connection:
                scoped_blocking.record_project_block(
                    connection,
                    repo,
                    reason,
                    message,
                    None,
                    plan_id=None,
                    previous_phase=str(row["phase"] or "") or None,
                )
                runtime_state.RuntimeStateRepository(connection).update_project(
                    repo,
                    {"agent_result_snapshot": preserved_snapshot},
                    enqueue=False,
                )
                notifications.enqueue_current_project_events(connection, repo)
        return message

    saved_worktree = Path(str(row["worktree"] or ""))
    if saved_worktree.is_dir():
        block_active_execplan(
            connection,
            repo,
            saved_worktree,
            message,
            str(row["verify_result"] or ""),
            reason=reason,
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    else:
        revision = str(
            plan["current_revision_sha"]
            or plan["plan_sha"]
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
            reason=reason,
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    return message


def recover_persisted_agent_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
) -> str:
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
        return make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan_id}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"저장된 Agent worktree 복구 실패: {error}"
        ) from error


def _proven_recovered_worktree_baseline(
    before: GitSnapshot, current: GitSnapshot, state_dir: Path,
    repo_path: Path, plan_id: str, worktree: Path,
) -> GitSnapshot:
    """Account for only this run's journaled lock on a recreated worktree."""
    key = str(_canonical_path(worktree))
    old = before.worktrees.get(key)
    new = current.worktrees.get(key)
    if (
        old is None or new is None or before.head != current.head
        or before.branch != current.branch or before.index != current.index
        or before.dirty != current.dirty
        or f"HEAD={before.head}" not in old
        or f"branch=refs/heads/{before.branch}" not in old
    ):
        raise AgentScopeError("복구한 Agent worktree의 기존 파일·HEAD 상태가 다릅니다.")
    transitions = manager_git_journal.verified_managed_transitions(
        state_dir, str(_canonical_path(repo_path)), f"plan:{plan_id}",
        git=git,
    )
    owned = any(
        item["intent"]["kind"] == "managed_worktree_create"
        and item["intent"]["target"] == key
        and item["intent"]["before"] == {
            "exists": False, "registration": None, "index": None,
        }
        and item["result"]["observed"]["registration"] == {
            "head": current.head,
            "branch": f"refs/heads/{current.branch}",
            "locked": item["intent"]["expected"]["registration"]["locked"],
        }
        and set(new) == set(old) | {
            "locked=" + item["intent"]["expected"]["registration"]["locked"]
        }
        for item in transitions
    )
    if not owned:
        raise AgentScopeError("복구한 Agent worktree의 Manager journal 소유권이 없습니다.")
    return replace(before, worktrees={**before.worktrees, key: new})


def _blocked_agent_local_baseline(
    before: GitSnapshot,
    result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    *,
    allowed_dirty_paths: frozenset[str] = frozenset(),
) -> GitSnapshot:
    """Compatibility surface for the pure BLOCKED snapshot decision."""
    try:
        return agent_scope_decision.blocked_agent_local_baseline(
            before, result, current, str(_canonical_path(worktree)),
            allowed_dirty_paths=allowed_dirty_paths,
        )
    except agent_scope_decision.ScopeDecisionError as error:
        raise AgentScopeError(str(error)) from error


def _diagnosed_blocked_commit_allowances(
    before: GitSnapshot,
    blocked_result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    scope_before: GitSnapshot,
) -> tuple[GitSnapshot, frozenset[str], frozenset[str]]:
    """Compatibility surface for the pure diagnosed-commit decision."""
    return agent_scope_decision.diagnosed_blocked_commit_allowances(
        before, blocked_result, current, str(_canonical_path(worktree)), scope_before
    )


def validate_persisted_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    allow_planner_worktree_correction: bool = False,
    manager_cleanup_worktrees: frozenset[str] = frozenset(),
    runtime_mutation: RuntimeMutation | None = None,
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
    before = _with_proven_legacy_ignore_config(before, current_config)
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
        raise AgentScopeError(
            f"저장된 Agent Git config 변경 ({displayed})",
            reason_code=BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
        )
    role_name = str(project["agent_role"] or "")
    if role_name not in {"planner", "maintenance_planner", "worker", "reviewer"}:
        raise AgentScopeError(
            f"저장된 Agent 역할이 유효하지 않습니다: {role_name or '없음'}"
        )
    selected_plan_path = str(project["plan_path"] or "")
    if role_name == "worker" and not selected_plan_path:
        raise AgentScopeError("저장된 Worker ExecPlan 경로가 없습니다.")
    guidance_targets: tuple[str, ...] = ()
    allow_common_plans = False
    if role_name == "worker":
        active_plan = active_execplan(connection, repo) or project_execplan(
            connection, repo
        )
        if active_plan is None:
            raise AgentScopeError("저장된 Worker ExecPlan 상태가 없습니다.")
        guidance_targets = _guidance_worker_targets(
            connection, repo, str(active_plan["plan_id"])
        )
        allow_common_plans = _published_plan_allows_common_plans(
            repo_path, active_plan
        )
    recovered_worktree = False
    if not saved_worktree.is_dir():
        recovered_worktree = recover_persisted_agent_worktree(
            connection, repo_path, state_dir, repo, project
        ) == "recovered"

    current = capture_git_snapshot(saved_worktree)
    if recovered_worktree:
        plan_id = _plan_id_from_path(selected_plan_path)
        if not plan_id:
            raise AgentScopeError("복구한 Agent worktree의 ExecPlan identity가 없습니다.")
        previous_registration = before.worktrees.get(
            str(_canonical_path(saved_worktree))
        )
        before = _proven_recovered_worktree_baseline(
            before, current, state_dir, repo_path, plan_id, saved_worktree,
        )
        recovered_baseline = before
    else:
        recovered_baseline = None
    proven_cleanups = _proven_merged_cleanup_paths(
        connection, repo, before, current,
    )
    manager_cleanup_worktrees |= proven_cleanups
    phase = str(project["phase"] or "")
    guidance_ref_allowances: frozenset[str] | None = None
    if phase == "PLAN_BLOCKED" and role_name == "worker":
        plan_id = _plan_id_from_path(selected_plan_path)
        plan_row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone() if plan_id else None
        if plan_row is not None and plan_row["blocked_reason"] == GUIDANCE_BLOCK_REASON:
            owned = set(_proven_manager_base_refs(
                connection, repo_path, repo, plan_id, before, current,
            ))
            checkpoint_row = connection.execute(
                "SELECT 1 FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
                (repo, plan_id),
            ).fetchone()
            if checkpoint_row is not None:
                request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
                    repo, plan_id,
                )
                if _owned_checkpoint_remote_oid(
                    connection, repo_path, request, plan_row, current,
                ) is not None:
                    owned.add(f"refs/remotes/origin/{plan_row['branch']}")
                    owned.add(f"refs/heads/{plan_row['branch']}")
            guidance_ref_allowances = frozenset(owned)
    result: GitSnapshot | None = None
    checkpoint: ManagerGitCheckpoint | None = None
    if allow_planner_worktree_correction and project["agent_result_snapshot"]:
        try:
            result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 Planner 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error
    elif project["agent_manager_snapshot"]:
        try:
            checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
    elif phase == "PLAN_BLOCKED" and project["agent_result_snapshot"]:
        try:
            result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Agent 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error
    recovered_result_baseline = None
    if recovered_worktree and result is not None:
        key = str(_canonical_path(saved_worktree))
        if result.worktrees.get(key) != previous_registration:
            raise AgentScopeError(
                "복구한 Agent worktree의 저장된 결과 등록 상태가 다릅니다."
            )
        result = replace(
            result, worktrees={**result.worktrees, key: current.worktrees[key]},
        )
        recovered_result_baseline = result
    recovered_manager_checkpoint = None
    if recovered_worktree and checkpoint is not None:
        key = str(_canonical_path(saved_worktree))
        if checkpoint.snapshot.worktrees.get(key) != previous_registration:
            raise AgentScopeError(
                "복구한 Agent worktree의 Manager checkpoint 등록 상태가 다릅니다."
            )
        checkpoint = replace(checkpoint, snapshot=replace(
            checkpoint.snapshot,
            worktrees={**checkpoint.snapshot.worktrees,
                       key: current.worktrees[key]},
        ))
        recovered_manager_checkpoint = checkpoint

    before = _normalize_legacy_ignored_snapshot(saved_worktree, before, current)
    if result is not None:
        result = _with_proven_legacy_ignore_config(result, current_config)
        result = _normalize_legacy_ignored_snapshot(saved_worktree, result, current)
    if checkpoint is not None:
        checkpoint = replace(
            checkpoint,
            snapshot=_normalize_legacy_ignored_snapshot(
                saved_worktree,
                _with_proven_legacy_ignore_config(checkpoint.snapshot, current_config),
                current,
            ),
        )

    manager_runtime: tuple[str, str] | None = None
    if runtime_mutation is not None and _canonical_path(repo_path) == _canonical_path(runtime_mutation.source):
        try:
            held_identity = _held_process_lock_identity(state_dir)
            expected_identity = (
                "0" if runtime_mutation.legacy
                else f"runtime:{runtime_mutation.mutation_id}"
            )
            if held_identity != expected_identity:
                raise WorkspaceOwnershipError(
                    "runtime scope 검증 중 부모 process lock identity가 변경됐습니다."
                )
            validate_runtime_worktree(
                runtime_mutation.source, state_dir,
                runtime_mutation.path, runtime_mutation.commit_sha,
            )
            current_lock_reason = _validated_runtime_registration_marker(
                runtime_mutation.source, state_dir,
                runtime_mutation.path, runtime_mutation.commit_sha,
                runtime_mutation.mutation_id, legacy=runtime_mutation.legacy,
            )
            if current_lock_reason != runtime_mutation.registration_lock_reason:
                raise WorkspaceOwnershipError(
                    "runtime 등록의 잠금 표식이 자식 시작 이후 변경됐습니다."
                )
        except RuntimeError as error:
            raise AgentScopeError(f"runtime mutation 검증 실패: {error}") from error
        manager_runtime = (
            str(_canonical_path(runtime_mutation.path)), runtime_mutation.commit_sha,
        )

    try:
        decision = agent_scope_decision.decide_persisted_scope(
            role=role_name,
            phase=phase,
            base_branch=str(project["base_branch"]),
            selected_plan_path=selected_plan_path,
            before=before,
            current=current,
            result=result,
            checkpoint=checkpoint,
            worktree_key=str(_canonical_path(saved_worktree)),
            repo_key=str(_canonical_path(repo_path)),
            planner_correction=allow_planner_worktree_correction,
            manager_cleanup_worktrees=manager_cleanup_worktrees,
            manager_runtime=manager_runtime,
        )
    except agent_scope_decision.ScopeDecisionError as error:
        raise AgentScopeError(str(error)) from error
    if manager_runtime is not None:
        operation_log(
            "runtime scope 기준선 검증: "
            f"before={hashlib.sha256(before.serialize().encode()).hexdigest()[:16]} "
            f"current={hashlib.sha256(current.serialize().encode()).hexdigest()[:16]} "
            f"path={manager_runtime[0]} sha={manager_runtime[1]}"
        )
    for check in decision.checks:
        try:
            check_refs = check.allowed_refs
            check_worktrees = check.allowed_worktrees
            if guidance_ref_allowances is not None:
                check_refs &= guidance_ref_allowances
                check_worktrees = (
                    check_worktrees & (proven_cleanups | frozenset((str(_canonical_path(saved_worktree)),)))
                ) | proven_cleanups
            validate_agent_scope(
                check.role,
                saved_worktree,
                check.before,
                check.after,
                selected_plan_path=(
                    selected_plan_path if check.role == "worker" else None
                ),
                allowed_guidance_targets=(
                    guidance_targets if check.role == "worker" else ()
                ),
                allow_common_plans=(
                    allow_common_plans if check.role == "worker" else False
                ),
                allowed_ref_changes=check_refs,
                allowed_worktree_changes=check_worktrees,
                external_worktree=repo_path,
                base_branch=str(project["base_branch"]),
            )
        except AgentScopeError as error:
            if check.checkpoint_result:
                raise AgentScopeError(
                    f"중단된 Manager Git 작업 후 Agent scope 검증 실패: {error}"
                ) from error
            raise
    if recovered_baseline is not None:
        recovered_values = {"agent_snapshot": recovered_baseline.serialize()}
        if recovered_result_baseline is not None:
            recovered_values["agent_result_snapshot"] = (
                recovered_result_baseline.serialize()
            )
        if recovered_manager_checkpoint is not None:
            recovered_values["agent_manager_snapshot"] = (
                recovered_manager_checkpoint.serialize()
            )
        update_project(connection, repo, **recovered_values)
    if decision.reconciled_before is not None and decision.reconciled_before != before:
        update_project(
            connection, repo,
            agent_snapshot=decision.reconciled_before.serialize(),
        )
        operation_log(
            f"runtime scope 기준선 반영: path={manager_runtime[0]} sha={manager_runtime[1]}"
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
    allowed_worktree_changes |= _proven_merged_cleanup_paths(
        connection, repo, before, current,
    )
    plan_id = _plan_id_from_path(selected_plan_path)
    has_execplans = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exec_plans'"
    ).fetchone() is not None
    guidance_plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone() if plan_id and has_execplans else None
    if plan_id is not None:
        allowed_ref_changes |= _proven_manager_base_refs(
            connection, repo_path, repo, plan_id, before, current,
        )
    if guidance_plan is not None and guidance_plan["blocked_reason"] == GUIDANCE_BLOCK_REASON:
        checkpoint_row = connection.execute(
            "SELECT 1 FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if checkpoint_row is not None:
            request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
                repo, plan_id,
            )
            if _owned_checkpoint_remote_oid(
                connection, repo_path, request, guidance_plan, current,
            ) is not None:
                allowed_ref_changes |= frozenset((
                    f"refs/remotes/origin/{guidance_plan['branch']}",
                ))
    if project["agent_manager_snapshot"]:
        try:
            manager_checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
        if guidance_plan is None or guidance_plan["blocked_reason"] != GUIDANCE_BLOCK_REASON:
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
            manager_ref_candidates = (
                frozenset()
                if guidance_plan is not None and guidance_plan["blocked_reason"] == GUIDANCE_BLOCK_REASON
                else frozenset(
                    {f"refs/heads/{base}"}
                    | {
                        refname
                        for refname in set(before.refs) | set(current.refs)
                        if refname.startswith("refs/remotes/origin/")
                        or refname.startswith("refs/tags/")
                    }
                )
            )
            allowed_ref_changes |= _metadata_changes_after_block(
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
    if project["phase"] != "PLAN_BLOCKED":
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

    plan = project_execplan(connection, repo)
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
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
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
    *, publication_remote_oid: str | None = None,
) -> None:
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is not None and int(plan["pr_conflict_recovery"]):
        with connection:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values={
                    "status": "IMPLEMENTING",
                    "blocked_fingerprint": None,
                    "blocked_worktree_fingerprint": None,
                    "blocked_review_fingerprint": None,
                    "retry_count": 0,
                    "blocked_reason": None,
                    "guidance_plan_hash": None,
                    "validated_base_sha": None,
                    "verify_result": None,
                    "review_result": None,
                    "last_error": REBASE_IN_PROGRESS_ERROR,
                    "verify_failure_history": "{}",
                    "review_failure_history": "{}",
                    "effective_worker_level": plan["initial_worker_level"],
                    "review_level_history": "{}",
                    "worker_session_transition": None,
                    "worker_session_binding": (
                        plan["worker_session_binding"]
                        if plan["worker_session_id"] is not None else None
                    ),
                },
                project_values={
                    "phase": "WORKING",
                    "validated_base_sha": None,
                    "verify_result": None,
                    "review_result": None,
                    "last_error": REBASE_IN_PROGRESS_ERROR,
                    "agent_role": None,
                    "agent_snapshot": None,
                    "agent_input_fingerprint": None,
                    "agent_baseline": None,
                    "agent_recovery": None,
                    "agent_input_plan": None,
                    "agent_result_plan": None,
                    "agent_result_snapshot": None,
                    "agent_manager_snapshot": None,
                    "worker_session_id": plan["worker_session_id"],
                    "worker_session_transition": None,
                    "worker_session_binding": (
                        plan["worker_session_binding"]
                        if plan["worker_session_id"] is not None else None
                    ),
                },
                enqueue=False,
            )
            scoped_blocking.clear_plan_block(connection, repo, plan_id)
        return
    with connection:
        runtime_state.RuntimeStateRepository(connection).transition(
            repo,
            plan_id,
            plan_values={
                "status": "READY",
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None,
                "retry_count": 0,
                "blocked_reason": None,
                "guidance_plan_hash": None,
                "last_error": None,
                "worker_session_id": None,
                "worker_session_binding": None,
                "worker_session_transition": None,
                "reviewer_session_id": None,
                "worker_result": None,
                "verify_result": None,
                "review_result": None,
                "validated_base_sha": None,
                "publication_remote_oid": publication_remote_oid,
                "pr_url": None,
                "pr_head_oid": None,
                "verify_failure_history": "{}",
                "review_failure_history": "{}",
                "effective_worker_level": plan["initial_worker_level"],
                "review_level_history": "{}",
            },
            project_values=_clear_active_values(),
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)


def _record_blocked_resume_head(
    connection: sqlite3.Connection, repo: str, plan_id: str,
    owner_oid: str, before_head: str, target_base_oid: str,
    after_head: str | None, before_snapshot: str | None = None,
) -> None:
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    checkpoint.record(
        repo, plan_id, expected_generation=current.generation,
        operation=None, expected_head=after_head or before_head,
        target_base_oid=target_base_oid, observed_remote_oid=owner_oid,
        snapshot={'blocked_resume': {
            'owner_oid': owner_oid, 'before_head': before_head,
            'target_base_oid': target_base_oid, 'after_head': after_head,
            'before_snapshot': (before_snapshot or
                (current.snapshot or {}).get('blocked_resume', {}).get('before_snapshot')),
        }},
    )


def _blocked_resume_worktree_matches(
    worktree: Path, marker: dict, current: GitSnapshot,
) -> bool:
    """Match preserved files and staging, allowing only autostash unstaging."""
    serialized = marker.get('before_snapshot')
    if not serialized:
        return False
    before = GitSnapshot.deserialize(serialized)
    if before.head != marker.get('before_head') or before.branch != current.branch:
        return False
    if before.config != current.config or not current.shared_metadata_complete:
        return False
    # Rebase may change blobs, but cannot legitimately introduce index flags.
    for path, entries in current.index.items():
        tags = tuple(entry.split()[0] for entry in entries)
        old_tags = tuple(entry.split()[0] for entry in before.index.get(path, ()))
        if tags != (old_tags if path in before.index else ('H',)):
            return False
    if set(before.dirty) != set(current.dirty):
        return False
    unstaged_by_rebase = False
    for path, (kinds, signature) in before.dirty.items():
        now_kinds, now_signature = current.dirty[path]
        if signature != now_signature:
            return False
        if kinds == now_kinds:
            if 'staged' in kinds and before.index.get(path) != current.index.get(path):
                return False
        elif (not marker.get('after_head') and 'staged' in kinds
              and now_kinds == ('unstaged',)):
            unstaged_by_rebase = True
        else:
            return False
    if unstaged_by_rebase:
        # Only a clean index can be attributed to rebase --autostash.
        if git(worktree, 'diff', '--cached', '--quiet', check=False).returncode:
            return False
        _restore_agent_staged_entries(worktree, before)
        restored = capture_git_snapshot(worktree)
        return restored.dirty == before.dirty and all(
            restored.index.get(path) == before.index.get(path)
            for path, (kinds, _) in before.dirty.items() if 'staged' in kinds
        )
    return True


def _resume_interrupted_blocked_rebase(
    connection: sqlite3.Connection, repo_path: Path,
    repo: str, base: str, plan: sqlite3.Row, worktree: Path, branch: str,
) -> bool | None:
    """Complete a saved release only after its Git effect is independently proven."""
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(plan['plan_id']),
    )
    marker = (checkpoint.snapshot or {}).get('blocked_resume')
    if not marker:
        return None
    published = connection.execute(
        'SELECT phase, accepted_remote_oid FROM blocked_checkpoints '
        'WHERE repo = ? AND plan_id = ?',
        (repo, plan['plan_id']),
    ).fetchone()
    if (published is None or published['phase'] != 'published'
            or marker.get('owner_oid') != published['accepted_remote_oid']):
        # A later block may publish a new checkpoint while retaining the old
        # execution snapshot. It must start a fresh release proof.
        return None
    current_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    before_head = str(marker.get('before_head') or '')
    after_head = str(marker.get('after_head') or '')
    if not after_head and current_head == before_head:
        return None
    try:
        target_base = git(repo_path, 'rev-parse',
                          f'refs/remotes/origin/{base}').stdout.strip()
        snapshot = capture_git_snapshot(worktree)
        remote_oid = remote_branch_oid(repo_path, branch)
        owned_oid = checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, str(plan['plan_id']), branch,
            worktree, remote_oid, git, before_head=before_head,
            recorded_oid=str(plan['publication_remote_oid'] or '') or None,
        ) if not plan['pr_url'] else None
        safe = (
            owned_oid is not None
            and marker.get('owner_oid') == owned_oid
            and marker.get('target_base_oid') == target_base
            and checkpoint.observed_remote_oid == owned_oid
            and snapshot.head == current_head and snapshot.branch == branch
            and not snapshot.operations
            and (not after_head or after_head == current_head)
            and _blocked_execplan_metadata_error(repo_path, worktree, plan) is None
        )
        if safe:
            state_dir = _owned_rebase_state_dir(repo, str(plan['plan_id']), worktree)
            if state_dir is not None:
                _finish_owned_direct_rebase(
                    state_dir, connection, repo_path, repo,
                    str(plan['plan_id']), worktree, branch,
                    agent_session_id=plan['worker_session_id'],
                )
            safe = (
                _direct_rebase_history_proven(
                    connection, repo_path, repo, str(plan['plan_id']), worktree,
                    branch, before_head, current_head, target_base,
                    plan['worker_session_id'],
                )
                and _blocked_resume_worktree_matches(worktree, marker, snapshot)
            )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        safe = False
    if not safe:
        _update_blocked_error(connection, repo, str(plan['plan_id']),
                              '중단된 차단 해제 rebase의 게시 소유권과 HEAD를 증명할 수 없습니다.')
        return False
    if not after_head:
        _record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base, current_head,
        )
    clear_agent_scope(connection, repo)
    _requeue_blocked_execplan(
        connection, repo, str(plan['plan_id']), publication_remote_oid=owned_oid,
    )
    return True


def resume_blocked_execplan_if_inputs_changed(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
) -> bool:
    plan = _plan_block_selected_for_release(
        connection,
        repo,
        require_fingerprint=True,
    )
    if plan is None:
        return False
    stored = str(plan["blocked_fingerprint"])
    worktree = Path(str(plan["worktree"]))
    branch = str(plan["branch"] or "")
    revision = f"refs/remotes/origin/{base}"
    execplan_input_changed = (
        _revision_execplan_input_fingerprint(repo_path, revision, plan) != stored
    )
    review_input_changed = False
    if plan["blocked_reason"] != BlockReasonCode.USER_DECISION_REQUIRED.value:
        current_review_fingerprint = _revision_review_issue_fingerprint(
            repo_path, revision, plan["review_result"]
        )
        stored_review_fingerprint = plan["blocked_review_fingerprint"]
        if current_review_fingerprint is None:
            review_input_changed = False
        elif stored_review_fingerprint:
            review_input_changed = current_review_fingerprint != str(
                stored_review_fingerprint
            )
        else:
            blocked_revision = str(
                plan["active_sha"]
                or plan["current_revision_sha"]
                or plan["plan_sha"]
                or ""
            )
            blocked_review_fingerprint = (
                _revision_review_issue_fingerprint(
                    repo_path, blocked_revision, plan["review_result"]
                )
                if blocked_revision
                else None
            )
            review_input_changed = (
                blocked_review_fingerprint is not None
                and current_review_fingerprint != blocked_review_fingerprint
            )
    remote_input_changed = execplan_input_changed or review_input_changed
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
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except (RuntimeError, WorkspaceOwnershipError) as error:
        message = f"BLOCKED worktree 복구 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    interrupted = _resume_interrupted_blocked_rebase(
        connection, repo_path, repo, base, plan, worktree, branch,
    )
    if interrupted is not None:
        return interrupted
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
    owned_oid = None
    checkpoint_row = connection.execute(
        "SELECT phase FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan['plan_id']),
    ).fetchone()
    if checkpoint_row is not None and checkpoint_row['phase'] == 'published':
        remote_oid = remote_branch_oid(repo_path, branch)
        owned_oid = checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, str(plan['plan_id']), branch,
            worktree, remote_oid, git,
            before_head=git(worktree, 'rev-parse', 'HEAD').stdout.strip(),
            recorded_oid=str(plan['publication_remote_oid'] or '') or None,
        ) if not plan['pr_url'] else None
        if owned_oid is None:
            _update_blocked_error(
                connection, repo, str(plan['plan_id']),
                '게시 checkpoint의 원격 OID와 계획 소유권을 증명할 수 없습니다.',
            )
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
    target_base_oid = git(repo_path, 'rev-parse', revision).stdout.strip()
    if owned_oid is not None:
        before_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        if git(repo_path, 'merge-base', '--is-ancestor', owned_oid,
               before_head, check=False).returncode != 0:
            _update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '게시 checkpoint 이후 로컬 HEAD 계보가 다릅니다.')
            return False
        _record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base_oid, None,
            capture_git_snapshot(worktree).serialize(),
        )
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
        rebased = _journaled_direct_rebase(
            connection, repo_path, repo, str(plan['plan_id']), worktree,
            branch, target_base_oid, autostash=True,
            agent_session_id=plan['worker_session_id'],
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

    if owned_oid is not None:
        after_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        if not _direct_rebase_history_proven(
            connection, repo_path, repo, str(plan['plan_id']), worktree,
            branch, before_head, after_head, target_base_oid,
            plan['worker_session_id'],
        ):
            _update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '차단 해제 rebase의 commit 계보를 증명할 수 없습니다.')
            return False
        pending = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, str(plan['plan_id']),
        )
        if not _blocked_resume_worktree_matches(
            worktree, pending.snapshot['blocked_resume'], capture_git_snapshot(worktree),
        ):
            _update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '차단 해제 rebase 이후 보존한 작업 상태가 다릅니다.')
            return False
        _record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base_oid, after_head,
        )
    elif base_ancestry.returncode == 1:
        _complete_direct_rebase_checkpoint(
            connection, repo, str(plan['plan_id']), worktree, target_base_oid,
        )

    clear_agent_scope(connection, repo)
    _requeue_blocked_execplan(connection, repo, str(plan["plan_id"]),
                              publication_remote_oid=owned_oid)
    return True


def _record_merged_worktree_cleanup(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    worktree: Path,
    branch: str,
    pr_head_oid: str | None,
    assessment: CleanupAssessment,
    registration: tuple[str, ...] | None,
) -> None:
    """Save only a completed removal of a validated Merge workspace."""
    if assessment.status != "safe" or not registration or not pr_head_oid:
        return
    key = str(_canonical_path(worktree))
    if (
        f"branch=refs/heads/{branch}" not in registration
        or capture_git_snapshot(repo_path).worktrees.get(key) is not None
        or worktree.exists() or worktree.is_symlink()
    ):
        raise WorkspaceOwnershipError("Merge cleanup 뒤 worktree 제거를 확인할 수 없습니다.")
    _ensure_cleanup_pending_schema(connection)
    connection.execute(
        "INSERT OR REPLACE INTO merged_worktree_cleanups "
        "(repo, plan_id, worktree, branch, registration, pr_head_oid) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (repo, plan_id, key, branch, json.dumps(registration), pr_head_oid),
    )


def _proven_merged_cleanup_paths(
    connection: sqlite3.Connection,
    repo: str,
    before: GitSnapshot,
    current: GitSnapshot,
) -> frozenset[str]:
    """Allow only the exact registration removed by a recorded Merge cleanup."""
    _ensure_cleanup_pending_schema(connection)
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exec_plans'"
    ).fetchone() is None:
        return frozenset()
    proven: set[str] = set()
    for row in connection.execute(
        "SELECT c.*, p.status, p.branch AS plan_branch, p.worktree AS plan_worktree, "
        "p.workspace_owned, p.pr_head_oid AS plan_pr_head_oid "
        "FROM merged_worktree_cleanups c JOIN exec_plans p "
        "ON p.repo = c.repo AND p.plan_id = c.plan_id WHERE c.repo = ?",
        (repo,),
    ):
        path = str(row["worktree"])
        try:
            registration = tuple(json.loads(str(row["registration"])))
        except (TypeError, ValueError):
            continue
        if (
            row["status"] == "MERGED"
            and int(row["workspace_owned"] or 0) == WORKSPACE_OWNED
            and str(row["plan_branch"] or "") == row["branch"]
            and str(_canonical_path(Path(str(row["plan_worktree"] or "")))) == path
            and row["pr_head_oid"] == row["plan_pr_head_oid"]
            and before.worktrees.get(path) == registration
            and current.worktrees.get(path) is None
            and not Path(path).exists()
            and not Path(path).is_symlink()
        ):
            proven.add(path)
    return frozenset(proven)


def _record_manager_base_fetch_intent(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    base: str,
    before: GitSnapshot,
) -> int:
    """Persist the exact base ref we intend to fetch before changing Git refs."""
    refname = f"refs/remotes/origin/{base}"
    remote = git(
        repo_path, "ls-remote", "--heads", "origin", f"refs/heads/{base}",
        check=False,
    )
    rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if remote.returncode or len(rows) != 1 or rows[0][1:] != [f"refs/heads/{base}"]:
        raise RuntimeError("Manager base fetch의 원격 OID를 확정할 수 없습니다.")
    target = rows[0][0]
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", target) is None:
        raise RuntimeError("Manager base fetch 원격 OID가 유효하지 않습니다.")
    _ensure_cleanup_pending_schema(connection)
    existing = connection.execute(
        "SELECT id, before_ref, target_oid, action_token FROM manager_base_fetches "
        "WHERE repo = ? AND plan_id = ? AND refname = ? AND phase = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (repo, plan_id, refname),
    ).fetchone()
    before_ref = json.dumps(before.refs.get(refname))
    if existing is not None:
        if existing["before_ref"] == before_ref and existing["target_oid"] == target:
            if not existing["action_token"]:
                raise RuntimeError("이전 Manager base fetch intent의 소유 표식이 없습니다.")
            return int(existing["id"])
        try:
            saved = json.loads(str(existing["before_ref"]))
            cursor = tuple(saved) if saved is not None else None
            successors = connection.execute(
                "SELECT * FROM manager_base_fetches WHERE repo = ? AND plan_id = ? "
                "AND refname = ? AND id >= ? ORDER BY id",
                (repo, plan_id, refname, int(existing["id"])),
            ).fetchall()
            for prior in successors:
                previous = json.loads(str(prior["before_ref"]))
                previous = tuple(previous) if previous is not None else None
                if previous != cursor or (
                    prior["phase"] != "complete"
                    and not _manager_base_fetch_applied(repo_path, prior, successors)
                ):
                    raise RuntimeError("이전 Manager base fetch 체인을 증명할 수 없습니다.")
                cursor = (str(prior["target_oid"]), "")
        except (TypeError, ValueError) as error:
            raise RuntimeError("이전 Manager base fetch intent가 유효하지 않습니다.") from error
        if cursor != before.refs.get(refname):
            raise RuntimeError("이전 Manager base fetch intent를 안전하게 재개할 수 없습니다.")
    with connection:
        cursor = connection.execute(
            "INSERT INTO manager_base_fetches "
            "(repo, plan_id, refname, before_ref, target_oid, action_token, phase) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (repo, plan_id, refname, before_ref,
             target, secrets.token_hex(16)),
        )
    return int(cursor.lastrowid)


def _manager_base_fetch_applied(
    repo_path: Path, row: sqlite3.Row,
    successors: list[sqlite3.Row] | None = None,
) -> bool:
    """Require this action at the tip or an uninterrupted owned reflog suffix."""
    token = str(row["action_token"] or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return False
    reflog = git(
        repo_path, "reflog", "show", "--format=%H%x00%gs",
        str(row["refname"]), check=False,
    )
    if reflog.returncode:
        return False
    lines = reflog.stdout.splitlines()
    action = f"{row['target_oid']}\x00loop-manager-base-fetch:{token}"
    if lines.count(action) != 1:
        return False
    position = lines.index(action)
    cursor = str(row["target_oid"])
    remaining = [item for item in (successors or [])
                 if item["refname"] == row["refname"] and item["id"] > row["id"]]
    for line in reversed(lines[:position]):
        matched = None
        for candidate in remaining:
            try:
                previous = json.loads(str(candidate["before_ref"]))
            except (TypeError, ValueError):
                return False
            if (previous == [cursor, ""] and line ==
                    f"{candidate['target_oid']}\x00loop-manager-base-fetch:{candidate['action_token']}"):
                matched = candidate
                break
        if matched is None:
            return False
        cursor = str(matched["target_oid"])
        remaining = [item for item in remaining if item["id"] > matched["id"]]
    return _tracking_ref_oid(repo_path, str(row["refname"])) == cursor


def _perform_manager_base_fetch(
    connection: sqlite3.Connection,
    repo_path: Path,
    intent_id: int,
    base: str,
) -> str:
    """Fetch the object, then update only the intended tracking ref with CAS."""
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ? AND phase = 'pending'",
        (intent_id,),
    ).fetchone()
    if row is None or row["refname"] != f"refs/remotes/origin/{base}":
        raise RuntimeError("Manager base fetch intent가 현재 base와 다릅니다.")
    token = str(row["action_token"] or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise RuntimeError("Manager base fetch action token이 유효하지 않습니다.")
    try:
        previous = json.loads(str(row["before_ref"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.") from error
    if previous is not None and (
        not isinstance(previous, list) or len(previous) != 2
        or previous[1] != ""
        or not isinstance(previous[0], str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", previous[0])
    ):
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.")
    target = str(row["target_oid"])
    git(repo_path, "fetch", "--no-tags", "--refmap=", "origin",
        f"refs/heads/{base}")
    fetched = git(repo_path, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
    if fetched != target:
        raise RuntimeError("Manager base fetch 원격 OID가 저장된 intent와 다릅니다.")
    git(
        repo_path, "update-ref", "--create-reflog", "-m",
        f"loop-manager-base-fetch:{token}", str(row["refname"]), target,
        previous[0] if previous is not None else "0" * len(target),
    )
    return base


def _finish_manager_base_fetch(
    connection: sqlite3.Connection,
    repo_path: Path,
    intent_id: int,
) -> None:
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ?",
        (intent_id,),
    ).fetchone()
    if row is None or git(repo_path, "rev-parse", str(row["refname"])).stdout.strip() != row["target_oid"]:
        raise RuntimeError("Manager base fetch 결과가 저장된 원격 OID와 다릅니다.")
    try:
        previous = json.loads(str(row["before_ref"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.") from error
    if previous != [row["target_oid"], ""] and not _manager_base_fetch_applied(repo_path, row):
        raise RuntimeError("Manager base fetch의 ref 갱신 증거가 없습니다.")
    with connection:
        connection.execute(
            "UPDATE manager_base_fetches SET phase = 'complete' WHERE id = ?",
            (intent_id,),
        )


def _journaled_manager_base_fetch(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    intent_id: int, base: str, plan_id: str,
) -> str:
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ? AND phase = 'pending'",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Manager base fetch intent를 찾을 수 없습니다.")
    previous = json.loads(str(row["before_ref"]))
    before_oid = previous[0] if previous is not None else None
    refname = str(row["refname"])
    target = str(row["target_oid"])

    def marker(_intent: Mapping[str, object]) -> bool:
        return before_oid == target or _manager_base_fetch_applied(repo_path, row)

    manager_git_journal.recover_active(
        state_dir, repo=str(_canonical_path(repo_path)),
        kind="project_fetch", target=refname,
        observe=lambda: {"oid": _tracking_ref_oid(repo_path, refname)},
        marker=marker,
    )
    current = _tracking_ref_oid(repo_path, refname)
    if current != target:
        manager_git_journal.execute(
            state_dir, owner=f"guidance:{plan_id}", kind="project_fetch",
            repo=str(_canonical_path(repo_path)), target=refname,
            before={"oid": before_oid}, expected={"oid": target},
            observe=lambda: {"oid": _tracking_ref_oid(repo_path, refname)},
            apply=lambda _token: _perform_manager_base_fetch(
                connection, repo_path, intent_id, base,
            ),
            marker=marker,
        )
    elif not marker({}):
        raise RuntimeError("Manager base fetch의 소유 ref 갱신 증거가 없습니다.")
    _finish_manager_base_fetch(connection, repo_path, intent_id)
    return base


def _proven_manager_base_refs(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    before: GitSnapshot,
    current: GitSnapshot,
) -> frozenset[str]:
    """Follow completed fetches or an interrupted fetch with its Git action."""
    _ensure_cleanup_pending_schema(connection)
    proven: set[str] = set()
    rows = connection.execute(
        "SELECT id, refname, before_ref, target_oid, action_token, phase "
        "FROM manager_base_fetches "
        "WHERE repo = ? AND plan_id = ? AND phase IN ('pending', 'complete') "
        "ORDER BY id",
        (repo, plan_id),
    ).fetchall()
    for refname in {str(row["refname"]) for row in rows}:
        cursor = before.refs.get(refname)
        for row in rows:
            if row["refname"] != refname:
                continue
            try:
                previous = json.loads(str(row["before_ref"]))
            except (TypeError, ValueError):
                continue
            previous = tuple(previous) if previous is not None else None
            if previous == cursor and (
                row["phase"] == "complete"
                or _manager_base_fetch_applied(repo_path, row, rows)
            ):
                cursor = (str(row["target_oid"]), "")
        if cursor is not None and cursor == current.refs.get(refname):
            proven.add(refname)
    return frozenset(proven)


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
    cleanup_registration: tuple[str, ...] | None = None
    worktree_present = worktree.exists() or worktree.is_symlink()
    if not worktree_present:
        assessment = _assess_missing_managed_cleanup(state_dir, repo_path, worktree)
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
            if assessment.status == "safe":
                cleanup_registration = capture_git_snapshot(repo_path).worktrees.get(
                    str(_canonical_path(worktree))
                )
            assessment = remove_cleanup_worktree(
                repo_path, worktree, assessment, state_dir=state_dir,
                owner=f"plan:{plan_id}",
            )
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
        project_values = _clear_active_values()
        runtime_state.RuntimeStateRepository(connection).mark_merged(
            repo,
            plan_id,
            project_values=project_values,
            enqueue=False,
        )
        _record_merged_worktree_cleanup(
            connection, repo_path, repo, plan_id, worktree, branch,
            pr_head_oid, assessment, cleanup_registration,
        )
        notifications.record_merged_plan(connection, repo, plan_id, str(plan["title"]))
    return assessment


def complete_merged_execplan_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
    plan: sqlite3.Row,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    """Complete exactly one merge-wait plan without hydrating another plan."""

    _ensure_cleanup_pending_schema(connection)
    branch = str(plan["branch"] or "")
    worktree = Path(str(plan["worktree"] or ""))
    workspace_owned = WORKSPACE_OWNED
    cleanup_registration: tuple[str, ...] | None = None
    # Merge recording is plan-scoped; managed worktree cleanup can wait until
    # the planning attempt releases the shared repository mutation slot.
    planning_owns_slot = not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo)
    if planning_owns_slot:
        assessment = CleanupAssessment("deferred", "planning 실행 자리 보존")
    elif not (worktree.exists() or worktree.is_symlink()):
        assessment = _assess_missing_managed_cleanup(state_dir, repo_path, worktree)
    elif int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        workspace_owned = WORKSPACE_UNOWNED
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
                str(plan["plan_id"]),
                branch,
                worktree,
            )
            validate_managed_worktree(repo_path, worktree, branch)
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_owned = WORKSPACE_UNOWNED
            assessment = CleanupAssessment(
                "unverifiable",
                f"Merge cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, pr_head_oid)
            if assessment.status == "safe":
                cleanup_registration = capture_git_snapshot(repo_path).worktrees.get(
                    str(_canonical_path(worktree))
                )
            assessment = remove_cleanup_worktree(
                repo_path,
                worktree,
                assessment,
                state_dir=state_dir,
                owner=f"plan:{plan['plan_id']}",
            )

    with connection:
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
        plan_id = str(plan["plan_id"])
        mirrored = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        project_values: dict[str, object] = {}
        if (mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
                and mirrored["plan_path"] == plan["path"]):
            project_values.update(_clear_active_values())
        runtime_state.RuntimeStateRepository(connection).mark_merged(
            repo,
            plan_id,
            project_values=project_values,
            enqueue=False,
        )
        _record_merged_worktree_cleanup(
            connection, repo_path, repo, plan_id, worktree, branch,
            pr_head_oid, assessment, cleanup_registration,
        )
        scoped_blocking.clear_plan_block(connection, repo, str(plan["plan_id"]))
        notifications.record_merged_plan(
            connection, repo, str(plan["plan_id"]), str(plan["title"])
        )
        if assessment.status == "unverifiable":
            scoped_blocking.record_project_block(
                connection,
                repo,
                BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
                str(assessment.reason or assessment.status),
                None,
                plan_id=str(plan["plan_id"]),
                previous_phase=None,
            )
        notifications.enqueue_current_project_events(connection, repo)
    return assessment


@dataclass(frozen=True)
class PullRequestSyncResult:
    step_result: int | None = None
    conflict_started: bool = False
    merged: bool = False


@dataclass(frozen=True)
class WaitingPullRequestObservation:
    conflicts: tuple[tuple[sqlite3.Row, Mapping[str, object]], ...]
    merged: bool
    merged_pr_urls: tuple[str, ...] = ()


def observe_waiting_pull_requests(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    require_base_identity: bool = False,
) -> WaitingPullRequestObservation:
    """Commit every merge-wait observation before source or conflict work."""

    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND "
        "(status = 'PR_OPEN' OR (status = 'PLAN_BLOCKED' AND blocked_reason = ?)) "
        "ORDER BY plan_id",
        (repo, PR_CLOSED_BLOCK_REASON),
    ).fetchall()
    registered_base = ""
    if rows:
        project = connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if project is None or not project["base_branch"]:
            raise RuntimeError("Merge 대기 PR의 등록 base branch를 확인할 수 없습니다.")
        registered_base = str(project["base_branch"])
    conflicts: list[tuple[sqlite3.Row, Mapping[str, object]]] = []
    merged = False
    merged_pr_urls: list[str] = []
    for stored in rows:
        branch = str(stored["branch"] or "")
        if not branch:
            raise RuntimeError(
                f"저장된 Merge 대기 PR의 관리 branch를 확인할 수 없습니다: {stored['plan_id']}"
            )
        pull_request = pr_for_branch(
            repo_path,
            branch,
            str(stored["pr_url"] or "") or None,
        )
        if pull_request is None:
            raise RuntimeError(
                f"저장된 Merge 대기 PR의 GitHub 상태를 확인할 수 없습니다: {branch}"
            )
        observed_url = str(pull_request.get("url") or "")
        stored_url = str(stored["pr_url"] or "")
        if stored_url and observed_url and observed_url != stored_url:
            raise PublicationSafetyError(
                f"저장된 PR URL과 GitHub PR identity가 다릅니다: {branch}"
            )
        head_name = pull_request.get("headRefName")
        if head_name and str(head_name) != branch:
            raise PublicationSafetyError(
                f"저장된 PR head branch와 GitHub PR identity가 다릅니다: {branch}"
            )
        observed_base = pull_request.get("baseRefName")
        if observed_base is not None and str(observed_base) != registered_base:
            raise PublicationSafetyError(
                f"GitHub PR base branch가 등록 base와 다릅니다: {branch}"
            )
        if require_base_identity and not observed_base:
            raise PublicationSafetyError(
                f"self PR base branch identity를 확인할 수 없습니다: {branch}"
            )
        url = str(pull_request.get("url") or stored["pr_url"] or "")
        if not url:
            raise RuntimeError(f"저장된 Merge 대기 PR URL을 확인할 수 없습니다: {branch}")
        head_value = pull_request.get("headRefOid")
        head_oid = str(head_value) if head_value else stored["pr_head_oid"]
        state = str(pull_request.get("state") or "OPEN").upper()
        if state == "MERGED" or pull_request.get("mergedAt"):
            if scoped_blocking.project_block(connection, repo) is not None:
                continue
            assessment = complete_merged_execplan_worktree(
                connection,
                repo_path,
                repo,
                state_dir,
                stored,
                url,
                str(head_oid) if head_oid else None,
            )
            merged = True
            merged_pr_urls.append(url)
            if assessment.status in {"safe", "missing"}:
                operation_print(f"Merge 확인 및 worktree 정리 완료: {url}")
            else:
                operation_print(
                    f"Merge 확인 완료; worktree 보존: {stored['worktree']} "
                    f"({assessment.reason or assessment.status})",
                    file=sys.stderr,
                )
            continue
        if state == "CLOSED":
            if stored["status"] == "PR_OPEN":
                _block_closed_pull_request(connection, repo, stored, url, head_oid)
            continue
        if state != "OPEN":
            raise RuntimeError(f"지원하지 않는 GitHub PR 상태입니다: {state}")
        if (
            stored["status"] == "PLAN_BLOCKED"
            and stored["blocked_reason"] == PR_CLOSED_BLOCK_REASON
        ):
            expected_head = str(stored["pr_head_oid"] or "")
            if expected_head and head_oid and str(head_oid) != expected_head:
                if not runtime_state.RuntimeStateRepository(
                    connection
                ).recovery_slot_available(repo):
                    # Keep the closed-PR block until its head can be checked
                    # without replacing an unrelated planning projection.
                    continue
                error = (
                    "다시 열린 PR head OID가 저장된 관리 head와 다릅니다: "
                    f"expected {expected_head}, actual {head_oid}"
                )
                with connection:
                    scoped_blocking.record_project_block(
                        connection,
                        repo,
                        BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                        error,
                        expected_head,
                        plan_id=str(stored["plan_id"]),
                        previous_phase=None,
                    )
                    notifications.enqueue_current_project_events(connection, repo)
                continue
            _resume_reopened_pull_request(
                connection,
                repo,
                stored,
                url,
                head_oid,
                recovery=False,
            )
            stored = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, stored["plan_id"]),
            ).fetchone()
        with connection:
            resumed = notifications.record_work_start(
                connection, repo, "PR handling", plan_id=str(stored["plan_id"]),
            )
        if resumed:
            operation_log(f"차단 해제 후 PR 처리 재개: {stored['plan_id']}")
        mergeability = classify_pull_request_mergeability(pull_request)
        with connection:
            plan_values: dict[str, object] = {"pr_url": url}
            # Conflict recovery must compare GitHub's head with the stored
            # ownership OID before adopting a changed head.
            if mergeability != "conflicting":
                plan_values["pr_head_oid"] = head_oid
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                str(stored["plan_id"]),
                plan_values=plan_values,
                enqueue=False,
            )
            notifications.enqueue_current_project_events(connection, repo)
        if mergeability == "conflicting":
            conflicts.append((stored, pull_request))

    # Each completed transition is committed before a remote fetch can fail.
    # A later run fetches again and resumes source synchronization from SQLite.
    if merged:
        project = connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if project is None or not project["base_branch"]:
            raise RuntimeError("Merge 뒤 fetch할 프로젝트 base branch를 확인할 수 없습니다.")
        base = str(project["base_branch"])
        _recorded_base_fetch(repo_path, state_dir, base, owner="project")
    return WaitingPullRequestObservation(
        tuple(conflicts), merged, tuple(merged_pr_urls)
    )


def select_waiting_pull_request_conflict(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    observation: WaitingPullRequestObservation,
) -> PullRequestSyncResult:
    """Select one still-eligible conflict after all observations are durable."""

    if active_execplan(connection, repo) is not None:
        return PullRequestSyncResult(merged=observation.merged)
    if not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo):
        return PullRequestSyncResult(merged=observation.merged)
    if scoped_blocking.project_block(connection, repo) is not None:
        return PullRequestSyncResult(merged=observation.merged)
    current_rows = {
        str(row["plan_id"]): row
        for row in connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
        )
    }
    statuses = {
        plan_id: str(row["status"]) for plan_id, row in current_rows.items()
    }
    eligible: list[tuple[sqlite3.Row, Mapping[str, object]]] = []
    for stored, pull_request in observation.conflicts:
        current = current_rows.get(str(stored["plan_id"]))
        if current is None or current["status"] != "PR_OPEN":
            continue
        if all(
            statuses.get(str(dependency)) == "MERGED"
            for dependency in json.loads(str(current["depends_on"]))
        ):
            eligible.append((current, pull_request))
    if not eligible:
        return PullRequestSyncResult(merged=observation.merged)
    plan, pull_request = min(
        eligible,
        key=lambda item: (
            PRIORITY_ORDER[str(item[0]["priority"])],
            int(str(item[0]["plan_id"])[2:]),
        ),
    )
    hydrate_execplan_project(connection, repo, plan)
    project = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    assert project is not None
    result = reconcile_existing_pull_request(
        connection,
        repo_path,
        state_dir,
        repo,
        project,
        pull_request,
    )
    return PullRequestSyncResult(result, result is None, observation.merged)


def synchronize_waiting_pull_requests(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    after_reconciliation: Callable[[], None] | None = None,
    after_observation: Callable[[WaitingPullRequestObservation], None] | None = None,
    require_base_identity: bool = False,
) -> PullRequestSyncResult:
    observation = observe_waiting_pull_requests(
        connection, repo_path, state_dir, repo,
        require_base_identity=require_base_identity,
    )
    if after_observation is not None:
        after_observation(observation)
    if after_reconciliation is not None:
        after_reconciliation()
    return select_waiting_pull_request_conflict(
        connection, repo_path, state_dir, repo, observation
    )


def _reconcile_guidance_double_block(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    block: sqlite3.Row,
    project: sqlite3.Row,
    plan: sqlite3.Row,
) -> bool:
    """Restore a published guidance wait only from independent Git and DB proof."""
    false_positive_reasons = {
        BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION.value,
        BlockReasonCode.SHARED_REF_MUTATION.value,
        BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION.value,
        BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
    }
    if (
        block["reason_code"] not in false_positive_reasons
        or plan["status"] != "PLAN_BLOCKED"
        or plan["blocked_reason"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}
        or plan["agent_role"] != "worker"
        or not plan["guidance_plan_hash"]
        or not plan["publication_remote_oid"]
        or not plan["worker_session_id"]
        or project["plan_path"] != plan["path"]
        or project["branch"] != plan["branch"]
        or project["worktree"] != plan["worktree"]
        or project["agent_role"] != "worker"
        or project["agent_snapshot"] != plan["agent_snapshot"]
        or project["worker_session_id"] != plan["worker_session_id"]
        or int(plan["workspace_owned"] or 0) != WORKSPACE_OWNED
    ):
        return False
    checkpoint = connection.execute(
        "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan["plan_id"]),
    ).fetchone()
    recorded_block = connection.execute(
        "SELECT * FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
        (repo, plan["plan_id"]),
    ).fetchone()
    if checkpoint is None or recorded_block is None:
        return False
    branch = str(plan["branch"])
    worktree = Path(str(plan["worktree"]))
    oid = str(plan["publication_remote_oid"])
    try:
        validate_execplan_workspace_identity(
            state_dir, repo, str(plan["plan_id"]), branch, worktree,
        )
        validate_managed_worktree(repo_path, worktree, branch)
        before = GitSnapshot.deserialize(str(plan["agent_snapshot"]))
        current = capture_git_snapshot(worktree)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
            repo, str(plan["plan_id"]),
        )
        if _owned_checkpoint_remote_oid(connection, repo_path, request, plan, current) != oid:
            raise ValueError("게시 OID와 원격 head가 일치하지 않습니다.")
        evidence = _guidance_checkpoint_commit_evidence(
            repo_path, str(plan["plan_id"]), oid,
        )
        if evidence is None:
            raise ValueError("guidance checkpoint commit identity 또는 tree가 유효하지 않습니다.")
        document = _guidance_document(worktree, str(plan["path"]))
        rows = _guidance_rows(connection, repo, str(plan["plan_id"]))
        if (
            document is None or document.structure_hash != plan["guidance_plan_hash"]
            or not rows or any(row["applied"] or row["invalidated_decision_hash"] for row in rows)
        ):
            raise ValueError("guidance 제안 또는 승인 대기 상태가 변경됐습니다.")
        proposals, new = _guidance_document_matches_rows(
            document, rows, require_decisions=False,
        )
        if new or any(
            row["decision_hash"]
            and proposals[str(row["proposal_id"])].decision.identity != row["decision_hash"]
            for row in rows
        ):
            raise ValueError("guidance User Decision과 저장된 증거가 다릅니다.")
        for target in {str(row["target_path"]) for row in rows}:
            row = next(row for row in rows if row["target_path"] == target)
            if _guidance_target_state(worktree, target) != (
                bytes(row["baseline_content"]), int(row["baseline_mode"]),
            ):
                raise ValueError(f"승인 전 AGENTS.md 내용이 변경됐습니다: {target}")
        own_path = str(_canonical_path(worktree))
        cleaned = _proven_merged_cleanup_paths(connection, repo, before, current)
        fetched_refs = _proven_manager_base_refs(
            connection, repo_path, repo, str(plan["plan_id"]), before, current,
        )
        remote_ref = f"refs/remotes/origin/{branch}"
        own_ref = f"refs/heads/{branch}"
        if (
            not before.shared_metadata_complete or not current.shared_metadata_complete
            or before.head != oid or current.head != oid
            or before.branch != branch or current.branch != branch
            or before.index != current.index
            or before.config != current.config
            or before.operations != current.operations
            or any(path != str(plan["path"]) for path in current.dirty)
            or any(
                before.refs.get(name) != current.refs.get(name)
                for name in before.refs.keys() | current.refs.keys()
                if name not in {remote_ref, own_ref} | fetched_refs
                and not name.startswith("refs/codex/turn-diffs/")
            )
            or any(
                before.worktrees.get(path) != current.worktrees.get(path)
                for path in before.worktrees.keys() | current.worktrees.keys()
                if path not in cleaned | {own_path}
            )
        ):
            raise ValueError("저장된 Worker snapshot 밖의 Git 변경이 있습니다.")
        if before.worktrees.get(own_path) != current.worktrees.get(own_path):
            raise ValueError("guidance worktree 등록이 변경됐습니다.")
        if checkpoint["phase"] == "published" and checkpoint["published_oid"] != oid:
            raise ValueError("저장된 checkpoint 게시 OID가 다릅니다.")
        if checkpoint["phase"] == "published" and checkpoint["reason_code"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}:
            raise ValueError("저장된 checkpoint 차단 원인이 guidance와 무관합니다.")
        if checkpoint["phase"] != "published" and checkpoint["reason_code"] not in false_positive_reasons:
            raise ValueError("교체된 checkpoint 원인의 소유권을 확인할 수 없습니다.")
        if recorded_block["reason_code"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}:
            raise ValueError("계획 차단 원인이 guidance와 무관합니다.")
        commit_message = git(repo_path, "show", "-s", "--format=%B", oid).stdout
        commit_fields = dict(line.split(": ", 1) for line in commit_message.splitlines() if ": " in line)
        tree = git(repo_path, "rev-parse", f"{oid}^{{tree}}").stdout.strip()
    except (AgentScopeError, GuidanceFormatError, RuntimeError, ValueError, KeyError, TypeError, WorkspaceOwnershipError) as error:
        operation_log(f"guidance 이중 차단 복구 보류 ({plan['plan_id']}): {error}", error=True)
        return False
    identity = blocked_checkpoint_state.request_identity(
        repo, str(plan["plan_id"]), GUIDANCE_BLOCK_REASON, evidence,
        branch, str(worktree), str(plan["guidance_plan_hash"]),
    )
    with runtime_state._transaction(connection):
        connection.execute(
            "UPDATE exec_plans SET blocked_reason = ?, blocked_fingerprint = NULL, "
            "blocked_worktree_fingerprint = NULL, last_error = ? "
            "WHERE repo = ? AND plan_id = ?",
            (GUIDANCE_BLOCK_REASON, evidence, repo, plan["plan_id"]),
        )
        connection.execute(
            "UPDATE execplan_blocks SET reason_code = ?, evidence = ?, "
            "release_fingerprint = ? WHERE repo = ? AND plan_id = ?",
            (GUIDANCE_BLOCK_REASON, evidence, plan["guidance_plan_hash"],
             repo, plan["plan_id"]),
        )
        connection.execute(
            "UPDATE blocked_checkpoints SET request_identity = ?, phase = 'published', "
            "reason_code = ?, evidence = ?, branch = ?, worktree = ?, "
            "commit_identity = ?, tree_oid = ?, local_oid = ?, remote_oid = ?, "
            "published_oid = ?, accepted_remote_oid = ?, result = 'published', "
            "unpublished_reason = NULL, retryable = 0 WHERE repo = ? AND plan_id = ?",
            (identity, GUIDANCE_BLOCK_REASON, evidence, branch, str(worktree),
             commit_fields["Checkpoint-Identity"], tree, oid, oid, oid, oid,
             repo, plan["plan_id"]),
        )
        scoped_blocking.clear_project_block(connection, repo)
        connection.execute(
            "UPDATE projects SET phase = 'PLAN_BLOCKED', last_error = ? WHERE repo = ?",
            (evidence, repo),
        )
    return True


def _published_review_commit_intent_matches(
    repo_path: Path, checkpoint: execution_checkpoint_state.CheckpointRecord,
    head: str,
) -> bool:
    """Prove an unfinished Manager commit intent produced the current HEAD."""
    saved = checkpoint.snapshot or {}
    if (checkpoint.operation != 'commit' or not checkpoint.expected_head
            or checkpoint.expected_head == head
            or saved.get('before_head') != checkpoint.expected_head
            or not saved.get('commit_message')):
        return False
    return (
        git(repo_path, 'show', '-s', '--format=%P', head).stdout.strip()
        == checkpoint.expected_head
        and git(repo_path, 'show', '-s', '--format=%B', head).stdout.strip()
        == str(saved['commit_message']).strip()
    )


def _resume_owned_checkpoint_project_block(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, block: sqlite3.Row, project: sqlite3.Row, plan: sqlite3.Row,
) -> bool:
    """Restore only a proven published checkpoint mistaken for external work."""
    plan_id = str(plan['plan_id'])
    branch = str(plan['branch'] or '')
    worktree = Path(str(plan['worktree'] or ''))
    original_phase = ('WORKING' if plan['status'] == 'IMPLEMENTING'
                      else str(plan['status']))
    blocked_projection = plan['status'] == 'PLAN_BLOCKED'
    projection_matches = (
        project['plan_path'] in (None, plan['path'])
        and project['branch'] in (None, branch)
        and project['worktree'] in (None, str(worktree))
    ) if blocked_projection else (
        project['plan_path'] == plan['path']
        and project['branch'] == branch
        and project['worktree'] == str(worktree)
    )
    if (
        block['plan_id'] != plan_id
        or project['phase'] != 'PROJECT_BLOCKED'
        or block['previous_phase'] != original_phase
        or plan['status'] not in ACTIVE_EXECPLAN_STATUSES | {'PLAN_BLOCKED'}
        or not projection_matches
        or plan['pr_url'] or project['pr_url']
        or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
    ):
        return False
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, plan_id,
    )
    snapshot = checkpoint.snapshot or {}
    marker = snapshot.get('blocked_resume')
    review_marker = snapshot.get('published_review_resume')
    if review_marker is not None and not isinstance(review_marker, dict):
        return False
    try:
        validate_execplan_workspace_identity(state_dir, repo, plan_id, branch, worktree)
        validate_managed_worktree(repo_path, worktree, branch)
        current = capture_git_snapshot(worktree)
        if (current.operations or current.dirty or current.branch != branch
                or (not marker and any(_non_default_index_flags(entries)
                                       for entries in current.index.values()))
                or not current.head):
            return False
        remote_oid = remote_branch_oid(repo_path, branch)
        if pr_for_branch(repo_path, branch) is not None:
            return False
        if marker:
            before_head = str(marker.get('before_head') or '')
            after_head = str(marker.get('after_head') or '')
            target_base = str(marker.get('target_base_oid') or '')
            if (not before_head or not after_head or not target_base
                    or marker.get('owner_oid') != remote_oid
                    or after_head != current.head):
                return False
        else:
            before_head = str(
                (review_marker.get('head') if isinstance(review_marker, dict) else None)
                or snapshot.get('reactivated_from_head') or ''
            )
            after_head = current.head
            target_base = str(snapshot.get('base_oid') or checkpoint.target_base_oid or '')
            if not before_head:
                return False
        pending_review_commit = (
            not marker and review_marker is not None
            and _published_review_commit_intent_matches(
                repo_path, checkpoint, current.head,
            )
        )
        direct_review = (
            not marker and before_head == remote_oid
            and (checkpoint.expected_head == current.head or pending_review_commit)
            and checkpoint.observed_remote_oid == remote_oid
            and (not plan['publication_remote_oid']
                 or plan['publication_remote_oid'] == remote_oid)
        )
        if direct_review:
            published = connection.execute(
                'SELECT request_identity, phase, reason_code, branch, worktree, '
                'accepted_remote_oid FROM blocked_checkpoints '
                'WHERE repo = ? AND plan_id = ?', (repo, plan_id),
            ).fetchone()
            if (published is None or published['phase'] != 'published'
                    or published['reason_code'] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value
                    or published['branch'] != branch
                    or published['worktree'] != str(worktree)
                    or published['accepted_remote_oid'] != remote_oid
                    or not published['request_identity']
                    or (review_marker and (
                        review_marker.get('request_identity') != published['request_identity']
                        or review_marker.get('owner_oid') != remote_oid
                        or review_marker.get('head') != remote_oid
                    ))
                    or not current.shared_metadata_complete
                    or current.refs.get(f'refs/heads/{branch}') != (current.head, '')
                    or current.refs.get(f'refs/remotes/origin/{branch}') != (remote_oid, '')):
                return False
        elif not target_base:
            return False
        if (
            (checkpoint.expected_head != current.head and not pending_review_commit)
            or checkpoint.observed_remote_oid != remote_oid
            or (not direct_review and not _direct_rebase_history_proven(
                connection, repo_path, repo, plan_id, worktree, branch,
                before_head, after_head, target_base,
                plan['worker_session_id'],
            ))
        ):
            return False
        owned_oid = checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, plan_id, branch, worktree,
            remote_oid, git,
            before_head=current.head if direct_review else before_head,
            recorded_oid=str(plan['publication_remote_oid'] or '') or None,
        )
        if owned_oid is None:
            return False
    except (OSError, RuntimeError, WorkspaceOwnershipError, ValueError):
        return False
    with runtime_state._transaction(connection):
        if direct_review and review_marker is None:
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid,
                observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**snapshot, 'published_review_resume': {
                    'request_identity': str(published['request_identity']),
                    'owner_oid': owned_oid, 'head': current.head,
                }},
            )
        elif not marker and not direct_review:
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid,
                observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**snapshot, 'blocked_resume': {
                    'owner_oid': owned_oid, 'before_head': before_head,
                    'after_head': after_head, 'target_base_oid': target_base,
                }},
            )
        connection.execute(
            'UPDATE exec_plans SET publication_remote_oid = ? '
            'WHERE repo = ? AND plan_id = ?', (owned_oid, repo, plan_id),
        )
        scoped_blocking.clear_project_block(connection, repo)
        if plan['status'] in ACTIVE_EXECPLAN_STATUSES:
            refreshed = connection.execute(
                'SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                (repo, plan_id),
            ).fetchone()
            runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(
                repo, runtime_state.ExecutionSnapshot.from_row(refreshed),
            )
        else:
            _clear_active_in_current_transaction(connection, repo)
    return True


def _clear_project_block_without_losing_planning_error(
    connection: sqlite3.Connection, repo: str, project: sqlite3.Row,
) -> None:
    scoped_blocking.clear_project_block(connection, repo)
    if (
        runtime_state.planning_owns_execution_slot(project)
        and project["planning_error"]
    ):
        connection.execute(
            "UPDATE projects SET last_error = ? WHERE repo = ?",
            (project["planning_error"], repo),
        )


def resume_project_block_if_safe(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    """Release only a project block whose original safety boundary is restored."""

    block = scoped_blocking.project_block(connection, repo)
    if block is None:
        return False
    if block["reason_code"] == BlockReasonCode.LEGACY_UNSCOPED_BLOCK.value:
        return False
    plan_id = str(block["plan_id"] or "")
    project = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        return False
    planning_owns_slot = runtime_state.planning_owns_execution_slot(project)
    if not plan_id:
        if block['reason_code'] == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value:
            return False
        try:
            if block["reason_code"] == BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value:
                worktree = Path(str(project["worktree"] or ""))
                fingerprint = str(project["planning_attempt_fingerprint"] or "")
                if not fingerprint:
                    return False
                validate_planning_worktree_ownership(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    fingerprint,
                    worktree,
                )
            elif project["agent_snapshot"]:
                before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
                worktree = Path(str(project["worktree"] or repo_path))
                after = capture_git_snapshot(worktree)
                validate_agent_scope(
                    str(project["agent_role"] or "planner"),
                    worktree,
                    before,
                    after,
                    external_worktree=repo_path,
                    base_branch=str(project["base_branch"]),
                )
            else:
                return False
        except (AgentScopeError, RuntimeError, WorkspaceOwnershipError, ValueError):
            return False
        with connection:
            _clear_project_block_without_losing_planning_error(connection, repo, project)
        return True
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is None:
        return False
    if planning_owns_slot and str(plan["status"]) in ACTIVE_EXECPLAN_STATUSES:
        return False
    if (
        planning_owns_slot
        and block['reason_code']
        == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value
    ):
        return False
    if not planning_owns_slot and _reconcile_guidance_double_block(
        connection, repo_path, state_dir, repo, block, project, plan,
    ):
        return True
    if block['reason_code'] == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value:
        return _resume_owned_checkpoint_project_block(
            connection, repo_path, state_dir, repo, block, project, plan,
        )
    if (
        block["reason_code"]
        == BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value
        and int(plan["workspace_owned"])
        in {WORKSPACE_UNOWNED, WORKSPACE_RESERVED}
    ):
        worktree = Path(str(plan["worktree"] or ""))
        branch = str(plan["branch"] or "")
        try:
            validate_execplan_workspace_identity(
                state_dir, repo, plan_id, branch, worktree
            )
        except WorkspaceOwnershipError:
            return False
        branch_state = git(
            repo_path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        if (
            worktree.exists()
            or worktree.is_symlink()
            or branch_state.returncode != 1
        ):
            return False
        status = str(plan["status"])
        with connection:
            _clear_project_block_without_losing_planning_error(connection, repo, project)
        if status in ACTIVE_EXECPLAN_STATUSES:
            _requeue_blocked_execplan(connection, repo, plan_id)
        else:
            if not planning_owns_slot:
                with connection:
                    _clear_active_in_current_transaction(connection, repo)
        return True
    if int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    try:
        validate_execplan_workspace_identity(
            state_dir, repo, plan_id, branch, worktree
        )
        validate_managed_worktree(
            repo_path,
            worktree,
            branch,
            allow_rebase=_rebase_in_progress_for_branch(worktree, branch),
        )
        if plan["agent_snapshot"]:
            before = GitSnapshot.deserialize(str(plan["agent_snapshot"]))
            after = capture_git_snapshot(worktree)
            role_name = str(plan["agent_role"] or "worker")
            validate_agent_scope(
                role_name,
                worktree,
                before,
                after,
                selected_plan_path=(
                    str(plan["path"]) if role_name == "worker" else None
                ),
                external_worktree=repo_path,
                base_branch=str(project["base_branch"]),
            )
    except (AgentScopeError, RuntimeError, WorkspaceOwnershipError, ValueError):
        return False
    with connection:
        _clear_project_block_without_losing_planning_error(connection, repo, project)
        if str(plan["status"]) in ACTIVE_EXECPLAN_STATUSES:
            hydrate_execplan_project(connection, repo, plan)
        elif not planning_owns_slot:
            _clear_active_in_current_transaction(connection, repo)
    return True


def _remote_checkpoint_input_identity(
    blocked: sqlite3.Row, snapshot: GitSnapshot | None,
    ignored_paths: tuple[str, ...] = (),
) -> str:
    payload = (
        str(blocked["blocked_reason"] or ""),
        str(blocked["blocked_fingerprint"] or ""),
        str(blocked["blocked_worktree_fingerprint"] or ""),
        snapshot.serialize() if snapshot is not None else "unavailable",
        ignored_paths,
    )
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _remote_checkpoint_refusal(
    connection: sqlite3.Connection,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    row: sqlite3.Row,
    oid: str | None,
    input_identity: str,
    reason: str,
    *, retryable: bool = False,
) -> None:
    if (
        row["inspected_remote_oid"] == oid
        and row["inspected_input"] == input_identity
        and row["inspection_reason"] == reason
    ):
        return
    with runtime_state._transaction(connection):
        repository.record_remote_refusal(
            request, oid, input_identity, reason, commit=False,
            retryable=retryable,
        )
        runtime_state.RuntimeStateRepository(connection).transition(
            request.repo, request.plan_id,
            plan_values={"last_error": reason},
            project_values={"last_error": reason},
            enqueue=False,
        )
        connection.execute(
            "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND plan_id = ?",
            (reason, request.repo, request.plan_id),
        )
        notifications.enqueue_current_project_events(connection, request.repo)


def _resume_remote_corrected_execplan(
    connection: sqlite3.Connection, repo: str, plan: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    candidate_oid: str, input_identity: str, reason: str,
    *, new_failure_stage: str | None = None, assessment: str | None = None,
) -> None:
    """Treat a user's committed correction as unverified Worker input."""
    plan_id = str(plan["plan_id"])
    # Hydration commits; finish it before the atomic release boundary.
    hydrate_execplan_project(connection, repo, plan)
    with runtime_state._transaction(connection):
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, plan_id,
            plan_values={
                "status": "IMPLEMENTING", "blocked_reason": None,
                "blocked_fingerprint": None, "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None, "last_error": None,
                "retry_count": 0, "worker_result": None,
                "verify_result": assessment if new_failure_stage == "verify" else None,
                "review_result": assessment if new_failure_stage == "review" else None,
                "validated_base_sha": None, "reviewer_session_id": None,
                "worker_input_token": None, "archive_token": None,
                "verify_token": None, "review_token": None,
                "publication_token": None, "publication_remote_oid": None,
                "verify_failure_history": plan["verify_failure_history"] if new_failure_stage else "{}",
                "review_failure_history": plan["review_failure_history"] if new_failure_stage else "{}",
                "effective_worker_level": plan["initial_worker_level"],
                "review_level_history": "{}",
                "agent_role": None, "agent_snapshot": None,
                "agent_baseline": None, "agent_recovery": None,
                "agent_input_plan": None, "agent_result_plan": None,
                "agent_input_fingerprint": None,
                "agent_result_snapshot": None,
                "agent_manager_snapshot": None,
            },
            project_values={
                "phase": "WORKING", "last_error": None,
                "worker_result": None,
                "verify_result": assessment if new_failure_stage == "verify" else None,
                "review_result": assessment if new_failure_stage == "review" else None, "validated_base_sha": None,
                "worker_session_id": plan["worker_session_id"],
                "agent_role": None, "agent_snapshot": None,
                "agent_baseline": None, "agent_recovery": None,
                "agent_input_plan": None, "agent_result_plan": None,
                "agent_input_fingerprint": None,
                "agent_result_snapshot": None,
                "agent_manager_snapshot": None,
            },
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)
        repository.record_remote_inspection(
            request, candidate_oid, input_identity, reason, commit=False,
        )


def _remote_checkpoint_has_new_failure(
    connection: sqlite3.Connection, repo: str, blocked: sqlite3.Row,
    stage: str, output: str, worktree: Path,
) -> bool:
    """Use the normal validation fingerprint contract to recognize progress."""
    history = _deserialize_failure_history(blocked[f"{stage}_failure_history"])
    previous = blocked[f"{stage}_result"]
    known = set(history)
    if previous:
        known.update(key for key, _ in validation_failure_fingerprints(stage, previous, worktree))
    if not known:
        request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
            repo, str(blocked["plan_id"]),
        )
        if request and request.evidence:
            try:
                known.update(key for key, _ in validation_failure_fingerprints(
                    stage, request.evidence, worktree,
                ))
            except ReviewerProtocolError:
                return False
    return bool(known) and any(
        key not in known for key, _ in validation_failure_fingerprints(stage, output, worktree)
    )


def _remote_checkpoint_review_revision(
    repo_path: Path, worktree: Path, base_branch: str,
    state_dir: Path,
) -> tuple[str, str] | None:
    """Read the latest base and a candidate HEAD that contains that base."""
    base = _recorded_base_fetch(
        repo_path, state_dir, base_branch, owner="project",
    )
    head = git(worktree, "rev-parse", "HEAD^{commit}").stdout.strip()
    if not base or not head:
        raise RuntimeError("원격 checkpoint 검증 revision을 확인할 수 없습니다.")
    ancestry = git(
        repo_path, "merge-base", "--is-ancestor", base, head, check=False,
    ).returncode
    if ancestry not in {0, 1}:
        raise RuntimeError("원격 checkpoint 후보의 base ancestry를 확인할 수 없습니다.")
    return (base, head) if ancestry == 0 else None


def _evaluate_remote_checkpoint_cause(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, blocked: sqlite3.Row, config_path: Path | None,
    timeout_seconds: int | None,
) -> tuple[bool, str, str | None]:
    """Inspect the candidate while retaining the block and all execution evidence."""
    worktree = Path(str(blocked["worktree"]))
    config = config_path or ROOT / "config/config.ini"
    timeout = (timeout_seconds if timeout_seconds is not None
               else load_managed_config(config).verify_timeout_seconds)
    project = connection.execute(
        "SELECT provider, base_branch FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    revision = _remote_checkpoint_review_revision(
        repo_path, worktree, str(project["base_branch"]), state_dir,
    )
    if revision is None:
        return False, "원격 checkpoint 후보가 최신 base를 포함하지 않습니다.", "stale_base"
    before = capture_git_snapshot(worktree)
    passed, output = verify(worktree, timeout)
    validate_agent_scope(
        "reviewer", worktree, before, capture_git_snapshot(worktree),
        external_worktree=repo_path,
    )
    validation_feedback_state.ValidationFeedbackRepository(connection).record_result(
        repo, str(blocked["plan_id"]), revision[0], "verify",
        "success" if passed else "failure",
        str(getattr(output, "fingerprint_source", output)),
    )
    if not passed:
        return _remote_checkpoint_has_new_failure(
            connection, repo, blocked, "verify", output, worktree,
        ), str(output), "verify"
    if blocked["blocked_reason"] == BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value:
        return True, str(output), None
    try:
        worker_publication = parse_worker_result(
            str(blocked["worker_result"] or ""),
            {"id": str(blocked["plan_id"]), "title": str(blocked["title"])},
        )
    except WorkerResultError as error:
        return False, f"원격 checkpoint Worker 결과 검증 실패: {error}", "worker"
    targeted_test_reason = reviewer_targeted_test_reason(
        worker_publication.test_evidence,
    )
    previous_review = blocked["review_result"]
    protocol_error = None
    for attempt in range(1, MAX_REVIEWER_PROTOCOL_ATTEMPTS + 1):
        if _remote_checkpoint_review_revision(
            repo_path, worktree, str(project["base_branch"]), state_dir,
        ) != revision:
            return False, "원격 checkpoint verify 뒤 base 또는 HEAD가 변경됐습니다.", "revision_drift"
        review_before = capture_git_snapshot(worktree)
        code, review = run_agent(
            str(project["provider"]), worktree, state_dir, config, "reviewer",
            reviewer_prompt(
                str(project["base_branch"]), targeted_test_reason, previous_review,
                session_recovery=True,
                protocol_error=protocol_error,
            ),
            read_only=True,
        )
        validate_agent_scope(
            "reviewer", worktree, review_before, capture_git_snapshot(worktree),
            external_worktree=repo_path,
        )
        if code:
            raise RuntimeError(f"원격 checkpoint Reviewer 실행 미완료: {review}")
        try:
            verdict, details = parse_reviewer_result(review)
            if verdict == "SPEC_BLOCKED":
                assert isinstance(details, reviewer_specification.SpecificationDeadlock)
                try:
                    reviewer_specification.validate_locations(
                        details, str(blocked["path"]),
                        tuple(str(source) for source in json.loads(str(blocked["sources"]))),
                    )
                except ValueError as error:
                    raise ReviewerProtocolError(str(error)) from error
        except ReviewerProtocolError as error:
            protocol_error = str(error)
            previous_review = review
            if attempt == MAX_REVIEWER_PROTOCOL_ATTEMPTS:
                evidence = (
                    f"Reviewer 결과 프로토콜 오류: {protocol_error} "
                    f"(프로토콜 교정 {MAX_REVIEWER_PROTOCOL_ATTEMPTS}회 실패)\n"
                    f"직전 결과:\n{review}"
                )
                return False, evidence, "protocol_exhausted"
            continue
        if verdict == "SPEC_BLOCKED":
            return False, review, "specification"
        validation_feedback_state.ValidationFeedbackRepository(connection).observe(
            repo, str(blocked["plan_id"]), revision[0], "review", verdict == "PASS", review,
            normalize_feedback("review", review) if verdict == "FAIL" else (),
        )
        return verdict == "PASS" or _remote_checkpoint_has_new_failure(
            connection, repo, blocked, "review", review, worktree,
        ), review, "review" if verdict != "PASS" else None
    raise AssertionError("Reviewer 프로토콜 교정 횟수가 소진되지 않았습니다.")


def _reclassify_remote_specification_deadlock(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    blocked: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    accepted_oid: str,
    worktree: Path,
    review: str,
) -> None:
    """Persist a newly discovered specification block beside its published checkpoint."""

    verdict, details = parse_reviewer_result(review)
    if verdict != "SPEC_BLOCKED" or not isinstance(
        details, reviewer_specification.SpecificationDeadlock
    ):
        raise ReviewerProtocolError("원격 checkpoint 명세 교착 증거가 유효하지 않습니다.")
    _reclassify_remote_checkpoint_block(
        connection, repo_path, repo, blocked, repository, request,
        accepted_oid, worktree,
        reason=BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
        evidence=review, review_fingerprint_source=review,
        user_targets=details.paths,
    )


def _reclassify_remote_checkpoint_block(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    blocked: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    accepted_oid: str,
    worktree: Path,
    *,
    reason: BlockReasonCode,
    evidence: str,
    review_fingerprint_source: str | None,
    user_targets: tuple[str, ...],
) -> None:
    """Replace one final cause without resetting its published checkpoint."""

    base_revision = str(
        blocked["active_sha"] or blocked["current_revision_sha"] or blocked["plan_sha"] or ""
    )
    release_fingerprint = _revision_execplan_input_fingerprint(
        repo_path, base_revision, blocked,
    )
    review_fingerprint = (
        _revision_review_issue_fingerprint(
            repo_path, base_revision, review_fingerprint_source,
        )
        if review_fingerprint_source is not None else None
    )
    worktree_fingerprint = _worktree_execplan_input_fingerprint(worktree, blocked)
    observation = recovery_runtime.RecoveryObservation(
        reason=recovery_runtime.reason_from_block(reason),
        repo=repo, plan_id=str(blocked["plan_id"]), phase="PLAN_BLOCKED",
        plan_revision=str(blocked["current_revision_sha"] or blocked["plan_sha"] or ""),
        base_revision=base_revision,
        fingerprint=hashlib.sha256(
            f"{release_fingerprint}|{review_fingerprint or evidence}".encode()
        ).hexdigest(),
        progress_marker=accepted_oid, detail=evidence,
        user_targets=user_targets,
    )
    with runtime_state._transaction(connection):
        decision = recovery_state.RecoveryRepository(connection).observe(
            observation, final_block_reason=reason,
        ).decision
        recovery_runtime.require_final(decision)
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, str(blocked["plan_id"]), expected_status="PLAN_BLOCKED",
            plan_values={
                "blocked_reason": reason.value,
                "blocked_fingerprint": release_fingerprint,
                "blocked_worktree_fingerprint": worktree_fingerprint,
                "blocked_review_fingerprint": review_fingerprint,
                "review_result": evidence,
                "last_error": evidence,
                "review_failure_history": "{}",
                "review_level_history": "{}",
                "retry_count": 0,
            },
            project_values={"last_error": evidence},
            enqueue=False,
        )
        changed = connection.execute(
            "UPDATE execplan_blocks SET reason_code = ?, evidence = ?, "
            "release_fingerprint = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND plan_id = ? AND reason_code = ?",
            (
                reason.value, evidence, release_fingerprint, repo,
                str(blocked["plan_id"]), str(blocked["blocked_reason"]),
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("원격 checkpoint 차단 원인이 변경됐습니다.")
        repository.reclassify_published(
            request, reason_code=reason.value, evidence=evidence,
            release_fingerprint=release_fingerprint,
            accepted_oid=accepted_oid,
        )
        notifications.enqueue_current_project_events(connection, repo)


def _reconcile_published_blocked_checkpoint(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    blocked: sqlite3.Row,
    row: sqlite3.Row,
    request: blocked_checkpoint.CheckpointRequest,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    config_path: Path | None = None,
    verify_timeout_seconds: int | None = None,
) -> str:
    """Inspect one exact remote ref without adopting unproven or unsafe work."""
    adapter = _BlockedCheckpointAdapter(connection, repo_path, state_dir, repo)
    snapshot = None
    worktree = Path(str(blocked["worktree"] or ""))
    branch = str(blocked["branch"] or "")
    try:
        observed = adapter.observe_remote(request)
    except (OSError, RuntimeError) as error:
        observed = None
        observation_error = f"원격 checkpoint를 재검증할 수 없습니다: {error}"
    else:
        observation_error = None
    accepted = str(row["accepted_remote_oid"] or row["published_oid"] or row["remote_oid"])
    unchanged = observed == accepted and not row["pending_remote_oid"]
    try:
        validate_execplan_workspace_identity(
            state_dir, repo, str(blocked["plan_id"]), branch, worktree,
        )
        validate_managed_worktree(repo_path, worktree, branch)
        snapshot = capture_git_snapshot(worktree)
        ignored_paths = tuple(_nul_git_paths(
            worktree, "ls-files", "--others", "--ignored",
            "--exclude-standard", "-z",
        ))
    except (OSError, RuntimeError, WorkspaceOwnershipError) as error:
        safety_error = f"관리 branch/worktree 소유권을 증명할 수 없습니다: {error}"
        ignored_paths = ()
    else:
        safety_error = None
    input_identity = _remote_checkpoint_input_identity(
        blocked, snapshot, ignored_paths,
    )

    def refuse(message: str) -> str:
        _remote_checkpoint_refusal(
            connection, repository, request, row, observed, input_identity,
            message,
        )
        return "blocked"

    def retry(message: str) -> str:
        _remote_checkpoint_refusal(
            connection, repository, request, row, observed, input_identity,
            message, retryable=True,
        )
        return "blocked"

    if (
        not row["pending_remote_oid"]
        and row["inspected_remote_oid"] == observed
        and row["inspected_input"] == input_identity
        and row["inspection_reason"]
        and not row["inspection_retryable"]
        and blocked["blocked_reason"]
        != BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value
    ):
        return "blocked"
    if unchanged:
        return "unchanged"
    if observation_error:
        return retry(observation_error)
    current_block = connection.execute(
        "SELECT reason_code FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
        (repo, blocked["plan_id"]),
    ).fetchone()
    if (
        current_block is None
        or current_block["reason_code"] != request.reason_code
        or blocked["blocked_reason"] != request.reason_code
        or request.plan_id != blocked["plan_id"]
    ):
        return refuse("checkpoint와 현재 PLAN_BLOCKED 원인/계획 identity가 다릅니다.")
    if observed is None:
        return refuse("게시된 원격 checkpoint branch가 삭제됐습니다.")
    if not row["published_oid"]:
        return refuse("게시 OID가 증명되지 않은 legacy checkpoint의 원격 변경은 채택할 수 없습니다.")
    if row["pending_remote_oid"] and row["pending_remote_oid"] != observed:
        return refuse("미완료 원격 checkpoint 채택 중 branch OID가 다시 변경됐습니다.")
    if blocked["pr_url"] or int(blocked["workspace_owned"] or 0) != WORKSPACE_OWNED:
        return refuse("PR 또는 관리 workspace 소유권 때문에 원격 checkpoint를 채택할 수 없습니다.")
    if request.branch != branch or request.worktree != str(worktree):
        return refuse("저장된 checkpoint branch/worktree가 ExecPlan 소유권과 다릅니다.")
    if safety_error:
        return refuse(safety_error)
    assert snapshot is not None
    if snapshot.operations:
        return refuse("미해결 Git operation: " + ", ".join(sorted(snapshot.operations)))
    if snapshot.branch != branch or not snapshot.head:
        return refuse("관리 worktree의 branch 또는 HEAD가 저장된 ExecPlan과 다릅니다.")
    own_ref = f"refs/heads/{branch}"
    own_worktree = snapshot.worktrees.get(str(_canonical_path(worktree)), ())
    if (
        not snapshot.shared_metadata_complete
        or snapshot.refs.get(own_ref, (None, None))[0] != snapshot.head
        or f"branch={own_ref}" not in own_worktree
    ):
        return refuse("관리 branch ref와 worktree 등록 상태의 소유권을 증명할 수 없습니다.")
    if snapshot.dirty:
        return refuse("로컬 tracked/index/untracked 변경을 보존하기 위해 원격 fast-forward를 중단합니다.")
    if any(_non_default_index_flags(entries) for entries in snapshot.index.values()):
        return refuse("관리 worktree index flag가 안전하지 않습니다.")
    project = connection.execute(
        "SELECT origin FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    expected_origin = str(project["origin"] or "") if project else ""
    try:
        if not expected_origin:
            raise RuntimeError("등록 origin이 없습니다.")
        for context in (repo_path, worktree):
            for args in (("remote", "get-url", "--all", "origin"),
                         ("remote", "get-url", "--push", "--all", "origin")):
                urls = git(context, *args).stdout.splitlines()
                if len(urls) != 1 or github_repository(urls[0]) != expected_origin:
                    raise RuntimeError("등록 origin과 fetch/push 대상이 다릅니다.")
    except RuntimeError as error:
        return refuse(f"원격 저장소 소유권을 증명할 수 없습니다: {error}")
    fetched = git(
        repo_path, "fetch", "--no-tags", "origin",
        f"refs/heads/{branch}", check=False,
    )
    if fetched.returncode:
        return retry("원격 checkpoint branch fetch 실패: " + fetched.stdout[-2000:])
    fetched_result = git(
        repo_path, "rev-parse", "FETCH_HEAD^{commit}", check=False,
    )
    if fetched_result.returncode:
        return retry("fetch된 원격 checkpoint OID를 확인할 수 없습니다.")
    fetched_oid = fetched_result.stdout.strip()
    if fetched_oid != observed:
        return retry("원격 checkpoint branch가 조회와 fetch 사이에 변경됐습니다.")
    local_history = git(
        repo_path, "merge-base", "--is-ancestor", accepted, snapshot.head,
        check=False,
    )
    if local_history.returncode not in {0, 1}:
        return retry("로컬 checkpoint ancestry를 확인할 수 없습니다.")
    if local_history.returncode == 1:
        return refuse("로컬 branch HEAD가 마지막 확인 원격 head의 후속이 아닙니다.")
    for ancestor, label in ((accepted, "마지막 확인 head"),
                            (str(row["published_oid"]), "게시 checkpoint"),
                            (snapshot.head, "로컬 HEAD")):
        relation = git(
            repo_path, "merge-base", "--is-ancestor", ancestor, observed,
            check=False,
        )
        if relation.returncode not in {0, 1}:
            return retry(f"원격 branch의 {label} ancestry를 확인할 수 없습니다.")
        if relation.returncode == 1:
            return refuse(f"원격 branch가 {label}의 후속이 아닙니다 (삭제/재작성/분기 또는 검증 실패).")
    try:
        changed_paths = _nul_git_paths(
            repo_path, "diff", "--no-renames", "--name-only", "-z",
            accepted, observed, "--",
        )
    except RuntimeError as error:
        return retry(f"원격 checkpoint 변경 경로를 확인할 수 없습니다: {error}")
    try:
        sources = tuple(str(source) for source in json.loads(str(blocked["sources"])))
    except (TypeError, ValueError):
        return refuse("ExecPlan source 목록을 검증할 수 없습니다.")
    archives = tuple(source for source in sources if source.startswith("docs/requirements/")
                     and "/archive/" not in source)
    guidance = adapter._approved_guidance_targets(request)
    common_plans = _published_plan_allows_common_plans(repo_path, blocked)
    for path in changed_paths:
        if not _worker_path_allowed(path, str(blocked["path"]), archives,
                                    guidance, common_plans):
            return refuse(f"원격 checkpoint 변경 범위 위반: {path}")
        entry = git(repo_path, "ls-tree", observed, "--", path).stdout
        if entry and not entry.startswith("100644 ") and not entry.startswith("100755 "):
            return refuse(f"원격 checkpoint에 symlink 또는 특수 파일이 있습니다: {path}")
    for local_path in ignored_paths:
        if any(
            local_path == remote_path
            or local_path.startswith(remote_path + "/")
            or remote_path.startswith(local_path + "/")
            for remote_path in changed_paths
        ):
            return refuse(f"ignored 로컬 경로와 원격 변경이 겹칩니다: {local_path}")
    metadata_error = _blocked_execplan_revision_metadata_error(
        repo_path, observed, blocked,
    )
    if metadata_error:
        return refuse(metadata_error)
    repository.start_remote_adoption(request, observed)
    if snapshot.head != observed:
        advanced = git(
            worktree, "-c", "core.hooksPath=/dev/null",
            "merge", "--ff-only", "--no-edit", observed, check=False,
        )
        if advanced.returncode:
            return retry("로컬 checkpoint fast-forward 실패: " + advanced.stdout[-2000:])
    if git(worktree, "rev-parse", "HEAD^{commit}").stdout.strip() != observed:
        return refuse("fast-forward 뒤 관리 worktree HEAD가 원격 후보와 다릅니다.")
    adopted_snapshot = capture_git_snapshot(worktree)
    input_identity = _remote_checkpoint_input_identity(
        blocked, adopted_snapshot, ignored_paths,
    )
    failure_evidence = "\n".join(
        str(value) for value in (
            request.evidence,
            blocked["verify_result"]
            if blocked["blocked_reason"] == BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value
            else blocked["review_result"],
        ) if value
    )
    corrected_paths = execution_revision.remote_failure_correction_paths(
        str(blocked["blocked_reason"]), changed_paths, failure_evidence,
    )
    if corrected_paths:
        try:
            resolved, assessment, new_failure_stage = _evaluate_remote_checkpoint_cause(
                connection, repo_path, state_dir, repo, blocked, config_path,
                verify_timeout_seconds,
            )
        except VerifyTerminationUncertain as error:
            with connection:
                scoped_blocking.record_project_block(
                    connection, repo, BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                    str(error), request.request_identity,
                    plan_id=request.plan_id, previous_phase=None,
                )
                notifications.enqueue_current_project_events(connection, repo)
            return "project-blocked"
        except (OSError, RuntimeError, ValueError) as error:
            return retry(f"원격 checkpoint 원인 재평가 미완료: {error}")
        if new_failure_stage == "specification":
            try:
                _reclassify_remote_specification_deadlock(
                    connection, repo_path, repo, blocked, repository, request,
                    observed, worktree, assessment,
                )
            except (OSError, RuntimeError, ValueError) as error:
                return retry(f"원격 checkpoint 명세 교착 기록 미완료: {error}")
            return "blocked"
        if new_failure_stage == "protocol_exhausted":
            try:
                _reclassify_remote_checkpoint_block(
                    connection, repo_path, repo, blocked, repository, request,
                    observed, worktree,
                    reason=BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
                    evidence=assessment, review_fingerprint_source=None,
                    user_targets=(
                        str(blocked["path"]),
                        *(str(source) for source in json.loads(str(blocked["sources"]))),
                    ),
                )
            except (OSError, RuntimeError, ValueError) as error:
                return retry(f"원격 checkpoint Reviewer 프로토콜 차단 기록 미완료: {error}")
            return "blocked"
        if new_failure_stage == "revision_drift":
            return retry(assessment)
        if not resolved:
            detail = "원격 checkpoint 원인 유지:\n" + assessment
            with connection:
                connection.execute(
                    "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE repo = ? AND plan_id = ?",
                    (detail, repo, request.plan_id),
                )
                connection.execute(
                    "UPDATE exec_plans SET last_error = ? WHERE repo = ? AND plan_id = ?",
                    (detail, repo, request.plan_id),
                )
                repository.record_remote_inspection(
                    request, observed, input_identity, detail, commit=False,
                )
                notifications.enqueue_current_project_events(connection, repo)
            return "blocked"
        _resume_remote_corrected_execplan(
            connection, repo, blocked, repository, request, observed,
            input_identity, "원격 checkpoint 진행 확인 후 Worker부터 재검증: " + assessment,
            new_failure_stage=new_failure_stage, assessment=assessment,
        )
        return "resumed"
    if blocked["blocked_reason"] == BlockReasonCode.PLAN_INPUT_INVALID.value and (
        blocked["blocked_fingerprint"]
        and _revision_execplan_input_fingerprint(repo_path, observed, blocked)
        != str(blocked["blocked_fingerprint"])
    ):
        _resume_remote_corrected_execplan(
            connection, repo, blocked, repository, request, observed,
            input_identity, "변경된 ExecPlan 입력을 Worker부터 재검증",
        )
        return "resumed"
    reason = str(blocked["last_error"] or "저장된 차단 원인의 해제 조건이 아직 충족되지 않았습니다.")
    repository.record_remote_inspection(request, observed, input_identity, reason)
    return "adopted"


def _resume_legacy_review_count_block(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, plan: sqlite3.Row,
) -> bool:
    """Release only a Review occurrence block with proven workspace ownership."""

    plan_id = str(plan["plan_id"])
    block = scoped_blocking.plan_block(connection, repo, plan_id)
    if (
        block is None
        or block["reason_code"] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value
        or scoped_blocking.project_block(connection, repo) is not None
        or plan["status"] != "PLAN_BLOCKED"
        or plan["blocked_reason"] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value
        or plan["pr_url"] or plan["pr_head_oid"]
        or plan["agent_recovery"] or int(plan["pr_conflict_recovery"] or 0)
        or not str(plan["review_result"] or "").strip().endswith("VERDICT: FAIL")
        or not plan["agent_result_snapshot"]
        or int(plan["workspace_owned"] or 0) != WORKSPACE_OWNED
    ):
        return False
    record = recovery_state.RecoveryRepository(connection).row(repo, plan_id)
    if record is not None and record["final_evidence"]:
        try:
            evidence = json.loads(str(record["final_evidence"]))
        except (TypeError, ValueError):
            return False
        if evidence.get("block_reason_code") != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value:
            return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    if not branch or not worktree.is_dir():
        return False
    checkpoint = connection.execute(
        "SELECT phase, reason_code, published_oid, local_oid, "
        "tree_oid, commit_identity FROM blocked_checkpoints "
        "WHERE repo = ? AND plan_id = ?", (repo, plan_id),
    ).fetchone()
    if checkpoint is not None and checkpoint["reason_code"] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value:
        return False
    if checkpoint is not None and checkpoint["phase"] not in {"published", "unpublished"}:
        return False
    published_resume = None
    try:
        validate_execplan_workspace_identity(state_dir, repo, plan_id, branch, worktree)
        validate_managed_worktree(repo_path, worktree, branch)
        if _rebase_in_progress_for_branch(worktree, branch):
            return False
        snapshot = capture_git_snapshot(worktree)
        if snapshot.branch != branch or snapshot.operations or not snapshot.head:
            return False
        blocked_snapshot = GitSnapshot.deserialize(str(plan["agent_result_snapshot"]))
        if (not snapshot.shared_metadata_complete
                or not blocked_snapshot.shared_metadata_complete):
            return False
        if checkpoint is None or (
            checkpoint["phase"] == "unpublished" and not checkpoint["local_oid"]
        ):
            if (
                snapshot.head != blocked_snapshot.head
                or snapshot.branch != blocked_snapshot.branch
                or snapshot.index != blocked_snapshot.index
                or snapshot.dirty != blocked_snapshot.dirty
            ):
                return False
        elif checkpoint["phase"] == "unpublished":
            local_oid = str(checkpoint["local_oid"])
            commit_identity = str(checkpoint["commit_identity"] or "")
            tree_oid = str(checkpoint["tree_oid"] or "")
            if (
                snapshot.head != local_oid or not commit_identity or not tree_oid
                or git(repo_path, "rev-parse", f"{local_oid}^{{tree}}").stdout.strip() != tree_oid
                or f"Checkpoint-Identity: {commit_identity}" not in git(
                    repo_path, "show", "-s", "--format=%B", local_oid,
                ).stdout
            ):
                return False
        validate_blocked_agent_scope(
            connection, repo_path, repo,
            str(connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
            ).fetchone()[0]),
            worktree, str(plan["path"]),
        )
        remote_oid = remote_branch_oid(repo_path, branch)
        observed_remote = git(
            repo_path, "ls-remote", "--heads", "origin",
            f"refs/heads/{branch}", check=False,
        )
        remote_lines = [
            line.split() for line in observed_remote.stdout.splitlines() if line.strip()
        ]
        expected_lines = ([[remote_oid, f"refs/heads/{branch}"]]
                          if remote_oid is not None else [])
        if observed_remote.returncode or remote_lines != expected_lines:
            return False
        if pr_for_branch(repo_path, branch) is not None:
            return False
        if checkpoint is not None and checkpoint["phase"] == "published":
            published = str(checkpoint["published_oid"] or "")
            request_row = connection.execute(
                "SELECT request_identity, reason_code, branch, worktree, "
                "accepted_remote_oid FROM blocked_checkpoints "
                "WHERE repo = ? AND plan_id = ?", (repo, plan_id),
            ).fetchone()
            if (not published or remote_oid != published
                    or snapshot.head != published or snapshot.dirty
                    or any(_non_default_index_flags(entries)
                           for entries in snapshot.index.values())
                    or request_row is None
                    or request_row["reason_code"] != block["reason_code"]
                    or request_row["branch"] != branch
                    or request_row["worktree"] != str(worktree)
                    or request_row["accepted_remote_oid"] != published
                    or request_row["request_identity"] !=
                    blocked_checkpoint_state.request_identity(
                        repo, plan_id, str(block["reason_code"]),
                        str(block["evidence"]), branch, str(worktree),
                        str(block["release_fingerprint"] or "") or None,
                    )
                    or checkpoint_ownership.owned_remote_oid(
                        connection, repo_path, repo, plan_id, branch,
                        worktree, remote_oid, git, before_head=snapshot.head,
                        recorded_oid=str(plan["publication_remote_oid"] or "") or None,
                    ) != remote_oid):
                return False
            published_resume = {
                "request_identity": str(request_row["request_identity"]),
                "owner_oid": published,
                "head": snapshot.head,
            }
        elif remote_oid is not None and remote_oid != plan["publication_remote_oid"]:
            return False
        if remote_oid is not None and git(
            repo_path, "merge-base", "--is-ancestor", remote_oid,
            snapshot.head, check=False,
        ).returncode != 0:
            return False
    except (AgentScopeError, OSError, RuntimeError, WorkspaceOwnershipError,
            KeyError, TypeError, ValueError):
        return False
    with runtime_state._transaction(connection):
        if published_resume is not None:
            execution = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
            current = execution.get(repo, plan_id)
            execution.record(
                repo, plan_id, expected_generation=current.generation,
                operation=None, expected_head=snapshot.head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=remote_oid,
                snapshot={**(current.snapshot or {}),
                          "published_review_resume": published_resume},
            )
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, plan_id,
            plan_values={
                "status": "IMPLEMENTING", "blocked_reason": None,
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None,
                "validated_base_sha": None,
                **({"publication_remote_oid": remote_oid}
                   if published_resume is not None else {}),
                "last_error": f"Review 실패:\n{str(plan['review_result'] or '')[-8000:]}",
            },
            project_values={
                "phase": "WORKING", "validated_base_sha": None,
                **({"publication_remote_oid": remote_oid}
                   if published_resume is not None else {}),
                "last_error": f"Review 실패:\n{str(plan['review_result'] or '')[-8000:]}",
            },
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)
    return True


def resume_plan_blocks(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    base: str,
    config_path: Path | None = None,
    verify_timeout_seconds: int | None = None,
) -> bool:
    """Reevaluate each plan block without letting one overwrite another."""

    if scoped_blocking.project_block(connection, repo) is not None:
        return False
    if active_execplan(connection, repo) is not None:
        return False
    if not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo):
        return False
    for blocked in plan_blocked_execplans(connection, repo):
        if blocked["blocked_reason"] == PR_CLOSED_BLOCK_REASON:
            continue
        checkpoint_row = connection.execute(
            "SELECT * FROM blocked_checkpoints "
            "WHERE repo = ? AND plan_id = ?",
            (repo, blocked["plan_id"]),
        ).fetchone()
        if (
            checkpoint_row is not None
            and checkpoint_row["phase"] == "published"
            and checkpoint_row["remote_oid"]
        ):
            checkpoint_repository = (
                blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            )
            checkpoint_request = checkpoint_repository.get(
                repo, str(blocked["plan_id"])
            )
            remote_result = _reconcile_published_blocked_checkpoint(
                connection, repo_path, state_dir, repo, blocked,
                checkpoint_row, checkpoint_request, checkpoint_repository,
                config_path, verify_timeout_seconds,
            )
            if remote_result == "project-blocked":
                return False
            if remote_result == "blocked":
                continue
            if remote_result == "resumed":
                return True
        else:
            remote_result = None
        if blocked["blocked_reason"] == BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value:
            # A changed published checkpoint needs the existing remote cause
            # and ownership review. Only an unchanged (or absent) checkpoint
            # can use the count-only legacy release path.
            if remote_result in {None, "unchanged"}:
                hydrate_execplan_project(connection, repo, blocked)
                if _resume_legacy_review_count_block(
                    connection, repo_path, state_dir, repo, blocked,
                ):
                    return True
            with connection:
                _clear_active_in_current_transaction(connection, repo)
            continue
        if blocked["blocked_reason"] == BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value:
            old_timeout = int(blocked["blocked_fingerprint"])
            new_timeout = (
                verify_timeout_seconds
                if verify_timeout_seconds is not None
                else load_managed_config(config_path or ROOT / "config/config.ini").verify_timeout_seconds
            )
            if new_timeout <= old_timeout:
                continue
            hydrate_execplan_project(connection, repo, blocked)
            worktree = Path(str(blocked["worktree"]))
            try:
                validate_execplan_workspace_identity(
                    state_dir, repo, str(blocked["plan_id"]),
                    str(blocked["branch"]), worktree,
                )
                validate_blocked_agent_scope(
                    connection, repo_path, repo, base, worktree,
                    str(blocked["path"]),
                )
            except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
                _update_blocked_error(connection, repo, str(blocked["plan_id"]), str(error))
                with connection:
                    _clear_active_in_current_transaction(connection, repo)
                continue
            with connection:
                runtime_state.RuntimeStateRepository(connection).transition(
                    repo, str(blocked["plan_id"]),
                    plan_values={
                        "status": "VERIFYING", "blocked_reason": None,
                        "blocked_fingerprint": None,
                        "blocked_worktree_fingerprint": None,
                        "blocked_review_fingerprint": None,
                        "verify_failure_history": "{}",
                        "retry_count": 0,
                        "last_error": None,
                    },
                    project_values={"phase": "VERIFYING", "last_error": None},
                    enqueue=False,
                )
                scoped_blocking.clear_plan_block(connection, repo, str(blocked["plan_id"]))
            return True
        hydrate_execplan_project(connection, repo, blocked)
        resume = (
            resume_guidance_approval_if_decided
            if blocked["blocked_reason"] == GUIDANCE_BLOCK_REASON
            else resume_blocked_execplan_if_inputs_changed
        )
        resumed = resume(connection, repo_path, repo, base, state_dir)
        refreshed = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, blocked["plan_id"]),
        ).fetchone()
        if resumed and refreshed and refreshed["status"] in ACTIVE_EXECPLAN_STATUSES:
            hydrate_execplan_project(connection, repo, refreshed)
            return True
        if active_execplan(connection, repo) is None:
            with connection:
                _clear_active_in_current_transaction(connection, repo)
    return False


def _block_closed_pull_request(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
) -> None:
    error = "PR이 Merge 없이 닫혔습니다."
    mirrored = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    clear_projection = bool(
        mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
        and mirrored["plan_path"] == plan["path"]
    )
    with runtime_state._transaction(connection):
        _final_block_recovery_decision(
            connection, repo, plan, "PR_OPEN",
            BlockReasonCode.PULL_REQUEST_CLOSED, error,
            str(head_oid or ""), None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=BlockReasonCode.PULL_REQUEST_CLOSED.value,
            evidence=error,
            release_fingerprint=str(head_oid or "") or None,
            plan_values={
                "pr_url": url,
                "pr_head_oid": head_oid,
            },
            clear_projection=clear_projection,
        )


def _resume_reopened_pull_request(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
    *,
    recovery: bool,
    conflict_oids: tuple[str, str] | None = None,
) -> None:
    if conflict_oids is not None and not recovery:
        raise ValueError("PR 충돌 복구 OID는 복구 전이에만 기록할 수 있습니다.")
    phase = "WORKING" if recovery else "PR_OPEN"
    status = "IMPLEMENTING" if recovery else "PR_OPEN"
    last_error = REBASE_IN_PROGRESS_ERROR if recovery else None
    execplan_values: dict[str, object] = {
        "status": status,
        "blocked_reason": None,
        "blocked_fingerprint": None,
        "blocked_worktree_fingerprint": None,
        "blocked_review_fingerprint": None,
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
            if conflict_oids is not None:
                values.update(
                    pr_conflict_recovery=1,
                    pr_conflict_head_oid=conflict_oids[0],
                    pr_conflict_base_oid=conflict_oids[1],
                )
    with runtime_state._transaction(connection):
        mirrored = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        repository = runtime_state.RuntimeStateRepository(connection)
        if (mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
                and mirrored["plan_path"] == plan["path"]):
            projection = project_values if recovery else _clear_active_values()
            repository.transition(
                repo,
                str(plan["plan_id"]),
                plan_values=execplan_values,
                project_values=projection,
                enqueue=False,
            )
        else:
            repository.update_plan(
                repo,
                str(plan["plan_id"]),
                plan_values=execplan_values,
                enqueue=False,
            )
        scoped_blocking.clear_plan_block(
            connection, repo, str(plan["plan_id"])
        )
        resumed = notifications.record_work_start(
            connection, repo, "PR handling", plan_id=str(plan["plan_id"]),
        )
        notifications.enqueue_current_project_events(connection, repo)
    if resumed:
        operation_log(f"차단 해제 후 PR 처리 재개: {plan['plan_id']}")

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
    blocked_plan = project_execplan(connection, repo)
    if blocked_plan is not None and blocked_plan["status"] != "PLAN_BLOCKED":
        blocked_plan = None
    reopening_closed_pr = bool(
        state == "OPEN"
        and blocked_plan is not None
        and blocked_plan["blocked_reason"] == PR_CLOSED_BLOCK_REASON
    )
    recovering_preflight_block = bool(
        state == "OPEN"
        and project["phase"] == "PLAN_BLOCKED"
        and _is_pr_conflict_preflight_block(project, blocked_plan)
    )
    if (
        state in {"OPEN", "CLOSED"}
        and project["phase"] == "PLAN_BLOCKED"
        and blocked_plan is not None
        and blocked_plan["blocked_reason"] != PR_CLOSED_BLOCK_REASON
        and not recovering_preflight_block
    ):
        operation_print(
            "PR 충돌 복구 차단 상태를 유지합니다: "
            + str(project["last_error"] or "사용자 조치가 필요합니다."),
            file=sys.stderr,
        )
        return 1
    if state != "OPEN" or (
        project["phase"] not in {"PR_OPEN", "PLAN_BLOCKED"}
        and not int(project["pr_conflict_recovery"])
    ):
        update_project(
            connection,
            repo,
            pr_url=url,
            pr_head_oid=head_oid,
        )

    if state == "OPEN":
        if recovering_preflight_block:
            assert blocked_plan is not None
            try:
                mergeability = classify_pull_request_mergeability(pull_request)
                if mergeability == "conflicting":
                    _validate_pr_conflict_recovery_identity(
                        connection, repo, project, pull_request,
                        expected_status="PLAN_BLOCKED",
                    )
                    _fetch_pr_conflict_recovery_refs(
                        repo_path, state_dir, str(project["base_branch"]), branch
                    )
                    (_plan, _worktree, _branch, observed_head, base_oid) = (
                        _validate_pr_conflict_recovery_ownership(
                            connection, repo_path, state_dir, repo, project,
                            pull_request, expected_status="PLAN_BLOCKED",
                        )
                    )
                else:
                    _validate_blocked_pr_without_conflict(
                        connection, repo_path, state_dir, repo, project,
                        pull_request,
                    )
            except (
                PublicationSafetyError,
                WorkspaceOwnershipError,
            ) as error:
                recovery_error = (
                    f"{PR_CONFLICT_PREFLIGHT_ERROR_PREFIX} {error}"
                )
                _update_blocked_error(
                    connection,
                    repo,
                    str(blocked_plan["plan_id"]),
                    recovery_error,
                )
                operation_print(recovery_error, file=sys.stderr)
                return 1
            _resume_reopened_pull_request(
                connection,
                repo,
                blocked_plan,
                url,
                head_oid,
                recovery=mergeability == "conflicting",
                conflict_oids=(observed_head, base_oid)
                if mergeability == "conflicting" else None,
            )
            if mergeability == "conflicting":
                operation_print(f"PR 충돌 자동 복구 재개: {url}")
                return None
            operation_print(f"PR_OPEN 충돌 복구 false positive 자동 해제: {url}")
            return 0

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
                if reopening_closed_pr:
                    _validate_pr_conflict_recovery_ownership(
                        connection,
                        repo_path,
                        state_dir,
                        repo,
                        project,
                        pull_request,
                        expected_status="PLAN_BLOCKED",
                    )
                else:
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
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                operation_print(recovery_error, file=sys.stderr)
                return 1
            if project["phase"] in {"PR_OPEN", "PLAN_BLOCKED"}:
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
                _validate_pr_conflict_recovery_identity(
                    connection,
                    repo,
                    project,
                    pull_request,
                    expected_status="PR_OPEN",
                )
            except PublicationSafetyError as error:
                recovery_error = (
                    f"{PR_CONFLICT_PREFLIGHT_ERROR_PREFIX} {error}"
                )
                block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{project['base_branch']}",
                    recovery_error,
                    str(project["verify_result"] or ""),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                    blocked_reason=PR_CONFLICT_PREFLIGHT_BLOCK_REASON,
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
            try:
                _fetch_pr_conflict_recovery_refs(
                    repo_path, state_dir, str(project["base_branch"]), branch
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
                reason = (
                    BlockReasonCode.MANAGED_WORKTREE_CONFLICT
                    if isinstance(error, ManagedWorktreeConflictError)
                    else BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN
                )
                block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{project['base_branch']}",
                    recovery_error,
                    str(project["verify_result"] or ""),
                    reason=reason,
                    blocked_reason=PR_CONFLICT_PREFLIGHT_BLOCK_REASON,
                )
                operation_print(recovery_error, file=sys.stderr)
                return 1

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
            plan = project_execplan(connection, repo)
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
                journal_state_dir=state_dir,
                journal_owner=f"plan:{plan_id}",
                allow_create_branch=False,
                allow_recovery=True,
                allow_existing=True,
            )
        except WorkspaceOwnershipError as error:
            workspace_error = (
                f"PR_OPEN workspace 소유권/복구 검증 실패: {error}"
            )
            block_active_execplan_from_revision(
                connection,
                repo_path,
                repo,
                f"refs/remotes/origin/{project['base_branch']}",
                workspace_error,
                reason=BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
            )
            operation_print(workspace_error, file=sys.stderr)
            return 1
        except RuntimeError as error:
            detail = f"PR_OPEN workspace 복구 재시도: {error}"
            _record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.WORKTREE_RECOVERY_FAILED,
                detail,
            )
            operation_print(detail, file=sys.stderr)
            return PROJECT_STEP_RETRY
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
            runtime_state.RuntimeStateRepository(connection).open_pull_request(
                repo,
                pr_url=url,
                pr_head_oid=head_oid,
            )
        operation_print(f"Merge 대기 중: {url}")
        return 0

    if state == "MERGED" or pull_request.get("mergedAt"):
        remote_base(
            repo_path, str(project["base_branch"]), state_dir=state_dir,
        )
        persisted = connection.execute(
            "SELECT pr_head_oid FROM projects WHERE repo = ?",
            (repo,),
        ).fetchone()
        stored_head_oid = (
            str(persisted["pr_head_oid"])
            if persisted and persisted["pr_head_oid"]
            else None
        )
        plan = project_execplan(connection, repo)
        if plan is None:
            raise RuntimeError("병합된 PR과 연결된 ExecPlan을 찾을 수 없습니다.")
        worktree = Path(str(plan["worktree"] or ""))
        try:
            assessment = complete_merged_execplan_worktree(
                connection,
                repo_path,
                repo,
                state_dir,
                plan,
                url,
                stored_head_oid,
            )
        except WorkspaceOwnershipError as error:
            workspace_error = (
                f"Merge cleanup workspace 소유권 검증 실패: {error}"
            )
            block_active_execplan_from_revision(
                connection,
                repo_path,
                repo,
                f"refs/remotes/origin/{project['base_branch']}",
                workspace_error,
                reason=BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
            )
            operation_print(workspace_error, file=sys.stderr)
            return 1
        except RuntimeError as error:
            detail = f"Merge cleanup workspace 조회 재시도: {error}"
            _record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.WORKTREE_RECOVERY_FAILED,
                detail,
            )
            operation_print(detail, file=sys.stderr)
            return PROJECT_STEP_RETRY
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
        plan = project_execplan(connection, repo)
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
    if git(worktree, "ls-files", "-u", "-z").stdout:
        update_project(
            connection, repo, phase="WORKING", validated_base_sha=None,
            last_error=REBASE_CONFLICT_PREFIX + "unmerged index after rebase/autostash",
        )
        return 0
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
            reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
        )
        operation_print(error, file=sys.stderr)
        return 1

    state_dir = _owned_rebase_state_dir(repo, str(plan['plan_id']), worktree)
    if state_dir is not None:
        _finish_owned_direct_rebase(
            state_dir, connection, repo_path, repo,
            str(plan['plan_id']), worktree, branch,
            agent_session_id=plan['worker_session_id'],
        )

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
        operation_print(error, file=sys.stderr)
        return PROJECT_STEP_RETRY

    rebased = _journaled_direct_rebase(
        connection, repo_path, repo, str(plan['plan_id']), worktree,
        branch, target_base, agent_session_id=plan['worker_session_id'],
    )
    if rebased.returncode and is_git_lock_failure(rebased.stdout):
        raise GitLockError(rebased.stdout[-8000:])
    if git(worktree, "ls-files", "-u", "-z").stdout:
        update_project(
            connection, repo, phase="WORKING", validated_base_sha=None,
            last_error=REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:],
        )
        return 0
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
    operation_print(error, file=sys.stderr)
    return PROJECT_STEP_RETRY

def planner_prompt(
    error: str | None,
    compact_catalog: str = "[]",
    mutable_snapshot: MutableReadySnapshot | None = None,
) -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    snapshot = mutable_snapshot or MutableReadySnapshot(
        repo="",
        active_sha="",
        plans=(),
        token=mutable_snapshot_token("", "", ()),
    )
    return f"""
당신은 Loop Manager의 Planner다.
AGENTS.md, 활성 PRODUCT와 Requirement, docs/ARCHITECTURE.md, docs/PLANS.md 및 현재 코드·테스트를 읽어라.
아래 compact catalog는 SQLite의 비종료 ExecPlan만 코드로 직렬화한 것이다. MERGED 계획은 제외됐다.
{compact_catalog}

아래 mutable READY snapshot은 SQLite와 planning 기준 commit에 결합돼 있다. token과 각 ID/path/revision을 변경 권한의 경계로 사용하라.
{snapshot.prompt_payload()}

현재 입력에서 식별 가능한 미충족 작업 전체를 독립적으로 검증·Merge 가능한 ExecPlan들로 나누어 한 번에 작성하라.
catalog가 이미 충족하는 작업이나 코드·테스트에 이미 구현된 작업은 중복 계획하지 마라.
catalog에서 현재 입력과 겹치는 비종료 계획만 필요할 때 전문을 읽고, 기존 docs/plans 전체를 기본 입력으로 읽지 마라.
새 ExecPlan마다 priority를 기록하라. mutable READY snapshot의 계획은 같은 ID를 보존한 채 필요하면 본문·metadata를 수정하거나 title에 맞게 rename하거나 삭제할 수 있다.
snapshot 밖의 기존 ExecPlan, 실행된 계획, MERGED·archive 계획은 수정·rename·삭제하지 마라. 삭제한 ID를 새 계획에 재사용하지 마라.
현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어는 같은 계획 주기의 ExecPlan 범위나 미충족 작업의 근거로 사용하지 말고 docs/FUTURE_IDEAS.md에만 분리해 기록하라.
docs/FUTURE_IDEAS.md의 목록 항목과 같은 내용을 새 ExecPlan의 제목이나 본문에 반복하지 마라.
docs/FUTURE_IDEAS.md는 planning batch와 함께 생성·수정할 수 있지만, 위에서 허용한 mutable READY 외의 기존 파일은 수정하지 마라.
구현, 테스트, commit, push와 PR 생성은 하지 마라. 추가 계획이 필요 없으면 파일을 변경하지 마라.
{failure}
""".strip()


def maintenance_planner_prompt(
    context: MaintenancePlanningContext,
    compact_catalog: str,
    change_context: str,
    error: str | None = None,
    *,
    include_general_planning: bool = False,
) -> str:
    """Return the active, mode-specific maintenance Planner contract."""

    failure = (
        "\n이전 실행에서 다음 검증 문제가 발생했다. 같은 허용 범위 안에서 "
        f"원인을 해결하라:\n{error[-6000:]}\n"
        if error
        else ""
    )
    targets = json.dumps(
        [plan.plan_id for plan in context.eligible_plans],
        ensure_ascii=False,
    )
    general_contract = (
        "현재 미충족 일반 PRODUCT/Requirement 작업도 같은 호출에서 모두 계획하라. "
        "일반 계획은 새 파일로만 작성하고, 리팩터링 계획도 만들면 모든 일반 계획을 "
        "리팩터링 DAG 말단 뒤에 depends_on으로 배치하라. docs/FUTURE_IDEAS.md는 일반 "
        "Planner 규칙 안에서만 변경할 수 있다."
        if include_general_planning
        else "이 호출에서는 새 일반 기능 계획이나 docs/FUTURE_IDEAS.md를 만들거나 수정하지 마라."
    )
    if context.mode is MaintenancePlanMode.BARRIER:
        mode_contract = f"""
현재 mode는 maintenance barrier다. 구체적인 구조 문제와 대상 책임 경계를 코드에서 확인한 경우에만 새 리팩터링 ExecPlan을 작성하라.
각 새 계획은 사용자 동작·공개 계약·저장 형식 보존 조건, targeted unit test와 전체 verify 회귀 조건을 명시하고 독립적으로 검증·Merge 가능한 DAG로 나눠라.
기본 priority는 normal이며 데이터 손실·보안·자동 실행 차단·반복 수동 복구의 구체적 근거가 본문에 있을 때만 상향하라.
새 DAG의 모든 말단 ID를 다음 eligible READY 계획의 기존 depends_on 뒤에 추가하라: {targets}
eligible 계획은 ID, 파일 경로, title, sources, complexity, risk, priority, 본문과 기존 depends_on을 그대로 보존하고 dependency 추가 외에는 수정하지 마라.
현재 코드보다 먼저 필요한 기존 READY 계획이 있으면 새 파일을 만들지 말고 최소 dependency-closed 선행 plan ID 집합을 반환하라.
기존 비종료 계획이 같은 구조 문제를 해결하면 중복 리팩터링 계획을 만들지 마라. 파일 길이·함수 길이·변경 횟수 하나만으로 계획을 만들지 마라.
{general_contract}
최종 응답에는 아래 protocol 중 하나를 정확히 한 줄로 기록하라. JSON 밖의 결과 표시는 판단에 사용되지 않는다.
MAINTENANCE_RESULT: {{"outcome":"no_change"}}
MAINTENANCE_RESULT: {{"outcome":"prerequisites","plan_ids":["G-000"]}}
MAINTENANCE_RESULT: {{"outcome":"refactoring","plan_ids":["G-000"]}}
"""
    else:
        mode_contract = f"""
현재 mode는 post-refactor revalidation이다. 다음 eligible READY 계획만 최신 구조에 맞게 재검증하라: {targets}
새 ExecPlan을 만들지 마라. 기존 ID, 파일 경로, title, sources, complexity, risk, priority와 Purpose의 원래 제품 목표를 보존하라.
Acceptance Criteria의 경로 표현, Implementation, Validation, Progress, Decision Log와 depends_on만 최신 구조에 필요한 범위에서 개정할 수 있다.
같은 제품 목표와 범위를 보존했다고 확정할 수 없으면 파일을 변경하지 마라.
최종 응답에는 아래 protocol 중 하나를 정확히 한 줄로 기록하라.
MAINTENANCE_RESULT: {{"outcome":"revalidated"}}
MAINTENANCE_RESULT: {{"outcome":"unsafe","reason":"구체적인 진단"}}
"""
    return f"""
당신은 Loop Manager의 유지보수 Planner다.
AGENTS.md, 활성 PRODUCT와 Requirement, docs/ARCHITECTURE.md, docs/PLANS.md, 현재 코드·테스트 및 아래 compact catalog와 bounded 변경 문맥을 읽어라.
책임·상태 전이·외부 연동의 혼합, 핵심 경로의 반복 변경 집중, 독립 테스트가 어려운 경계, 이름·제어 흐름·결합도, 오류 복구 경계, 중복 구현·불필요한 전역 가변 상태와 후속 변경 충돌을 함께 검토하라.
assessment에서는 선정 파일의 원본을 실제로 읽는 파일별 단위와 동시에 책임·진입점·공개 심벌·직접 참조·관련 테스트 경로의 짧은 코드맵을 만든다. 아래 잠정 기록은 관계 확인 후보이지 성공 ledger가 아니다. 변경되지 않은 파일은 필요한 문맥으로만 읽고 검토 완료 대상으로 세지 마라. 인용과 source revision을 확인하고, 줄 수나 토큰 수만으로 분리를 강제하지 마라.

compact catalog:
{compact_catalog}

bounded 변경 문맥:
{change_context}

{mode_contract}
기존 파일을 삭제·rename하지 말고 mode와 combined scope가 허용하지 않은 파일을 변경하지 마라.
구현, 테스트, commit, push와 PR 생성은 하지 마라. 이 권한은 활성 SQLite 유지보수 단계와 위 target snapshot에만 한정된다.
사용자가 BLOCKED 해제를 위해 ExecPlan을 직접 편집하는 별도 흐름과 이 권한을 혼동하지 말고, 이 호출로 사용자 편집을 대행하거나 권한 범위를 넓히지 마라.
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
    failure = (
        f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n"
        f"{redact_secrets(error)[-6000:]}\n"
        if error else ""
    )
    saved_feedback = (
        "\n미해결 검증 피드백 delta (identity, occurrence, revision):\n"
        f"{validation_feedback}\n"
        if validation_feedback
        else ""
    )
    if "전체 verify 시간 초과:" in (error or "") or "전체 verify 시간 초과:" in (validation_feedback or ""):
        failure += (
            "검증 출력과 진행 위치, 실행된 테스트 및 verify 스크립트를 확인해 "
            "무한 반복·교착·중복 검증을 먼저 점검하라. 필요한 검증을 삭제하거나 "
            "시간 상한을 임의로 올리지 마라. 필요한 검증이 정상 진행했고 현재 "
            "상한만 부족하다는 구체적 근거가 확인되면 PR_SUMMARY 안에 "
            "VERIFY_TIMEOUT_LIMIT_INSUFFICIENT: <진행 근거와 필요한 설정 조정> "
            "한 줄을 적어라.\n"
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
            "AGENTS.md, 선택된 최신 ExecPlan과 현재 worktree를 다시 확인하라. "
            "아래 이전 Worker 결과와 미해결 검증 피드백을 기준으로 계속하라:\n"
            f"{redact_secrets(previous_result or '이전 Worker 결과 없음')[-6000:]}\n"
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
{worker_outcome_instruction()}
{archive_instruction}
{recovery}
{saved_feedback}
{failure}
""".strip()



_TARGETED_TEST_FAILURE = re.compile(
    r"(?i)(?<![\w])(?:FAIL(?:ED|URES?)?|ERRORS?|실패"
    r"|(?:did|does|do|has|have|had|was|were|is|are)\s+not\s+pass(?:ed)?"
    r"|(?:not|never|didn't|doesn't|don't)\s+pass(?:ed)?)(?![\w])"
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
    no_test_reason = reviewer_targeted_test_reason(test_evidence)
    if _NO_RUNNABLE_TARGETED_TEST.search(test_evidence) and not no_test_reason:
        raise WorkerResultError(
            "Worker targeted unit test 미실행 사유에서 성공 내역을 제외하고 구체적인 이유를 보고해야 합니다."
        )
    if not (_TARGETED_TEST_SUCCESS.search(test_evidence) or no_test_reason):
        raise WorkerResultError(
            "Worker targeted unit test 통과 결과 또는 실행하지 못한 구체적인 사유가 필요합니다."
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


_NO_RUNNABLE_TARGETED_TEST = re.compile(
    r"(?:"
    r"(?:(?:실행 가능한|적용할|관련)\s+)*(?:targeted\s+)?unit\s+tests?(?:가|를|는)?\s+"
    r"(?:없(?:어|어서|었습니다|습니다|다)"
    r"|(?:실행|수행)(?:할\s+수\s+없|하지\s+(?:않았|못했))(?:습니다|다))"
    r"|no runnable targeted (?:unit )?tests?"
    r")",
    re.IGNORECASE,
)
_TARGETED_TEST_METADATA = re.compile(
    r"(?i)(?<![\w])(?:[\w./-]+\s*:\s*)?"
    r"(?:\d+\s+tests?\s+)?"
    r"(?:PASS(?:ED)?|SUCCESS|OK|성공|통과)(?![\w])(?:\s*\([^)]*\))?"
    r"|(?i:(?<![\w])(?:FAILED_COMMAND|RERUN):)"
    r"|(?i:(?<![\w])(?:python\d*(?:\.\d+)?\s+-m\s+unittest|pytest|npm\s+test)\b)"
)
_INLINE_TEST_COMMAND = re.compile(
    r"(?i)(?<![\w])(?:python\d*(?:\.\d+)?\s+-m\s+unittest|pytest|npm\s+test)\b"
)


def reviewer_targeted_test_reason(test_evidence: str) -> str | None:
    """Return only an explicit exceptional no-test disposition for review.

    Successful test prose is deliberately not interpreted: the validated Worker
    result and verify checkpoint are the program's evidence for entering Review.
    """
    for line in test_evidence.splitlines():
        for clause in re.split(r"[;|]", line):
            normalized = " ".join(clause.split())
            match = _NO_RUNNABLE_TARGETED_TEST.search(normalized)
            if match is None:
                continue
            if any(command.start() < match.start()
                   for command in _INLINE_TEST_COMMAND.finditer(normalized)):
                continue
            metadata = list(_TARGETED_TEST_METADATA.finditer(normalized))
            start = max(
                (item.end() for item in metadata if item.end() <= match.start()),
                default=0,
            )
            end = min(
                (item.start() for item in metadata if item.start() >= match.end()),
                default=len(normalized),
            )
            reason = normalized[start:end].lstrip(" :;|,.-").rstrip(" :;|,")
            reason_match = _NO_RUNNABLE_TARGETED_TEST.search(reason)
            if reason_match and (
                reason[:reason_match.start()].strip(" :;|,.-")
                or reason[reason_match.end():].strip(" :;|,.-")
            ):
                return reason[:600]
    return None

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
    except GitLockError:
        raise
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision의 Requirement/ExecPlan 이력을 확인할 수 없습니다: "
            f"{revision} ({error})"
        ) from error


def _stored_requirement_sources(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    try:
        return requirement_archive.stored_requirement_sources(rows)
    except requirement_archive.ArchiveSnapshotError as error:
        raise RequirementArchiveError(str(error)) from error


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
    plan_sha = str(row["publication_sha"] or row["plan_sha"] or "")
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
    path = str(row["publication_path"] or row["path"])
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
    *,
    archive_conflict_plan_ids: frozenset[str] = frozenset(),
) -> dict[str, dict[str, object]]:
    try:
        snapshot = revision_execplan_snapshot(
            repo_path,
            revision,
            archive_conflict_plan_ids=archive_conflict_plan_ids,
        )
    except GitLockError:
        raise
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision의 ExecPlan source 관계를 확정할 수 없습니다: {error}"
        ) from error
    return {
        plan_id: {
            **entry.metadata(),
            "path": entry.path,
            "content_hash": entry.content_hash,
        }
        for plan_id, entry in snapshot.all.items()
    }



def requirement_archive_snapshot(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    current_plan_id: str,
    *,
    allow_future_publication: bool = False,
) -> requirement_archive.ArchiveSnapshot:
    """Read candidate scope from one immutable Git and SQLite view."""

    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
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
    catalog = _revision_execplan_catalog(
        repo_path,
        revision,
        archive_conflict_plan_ids=frozenset(
            plan_id for plan_id, row in states.items()
            if str(row["status"]) == "MERGED"
        ),
    )
    historical_rows = connection.execute(
        "SELECT plan_id, merge_confirmed FROM execplan_catalog_history "
        "WHERE repo = ? AND currently_archived = 1",
        (repo,),
    ).fetchall()
    unverified_historical_ids = {
        str(row["plan_id"])
        for row in historical_rows if not int(row["merge_confirmed"])
    }
    state_ids = set(states)
    if allow_future_publication:
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
    missing_states = catalog_ids - state_ids
    unexpected_missing_states = missing_states - unverified_historical_ids
    if unexpected_missing_states or state_ids - catalog_ids:
        missing_plans = sorted(state_ids - catalog_ids)
        details = []
        if unexpected_missing_states:
            details.append(
                "SQLite 상태 없음: "
                + ", ".join(sorted(unexpected_missing_states))
            )
        if missing_plans:
            details.append("Git ExecPlan 없음: " + ", ".join(missing_plans))
        raise RequirementArchiveError(
            "ExecPlan source 관계 또는 상태를 확정할 수 없습니다 ("
            + "; ".join(details)
            + ")."
        )

    tree = _requirement_archive_tree(repo_path, revision)
    try:
        snapshot = requirement_archive.calculate(
            base_oid=revision,
            catalog=catalog,
            states={plan_id: str(row["status"]) for plan_id, row in states.items()},
            active_requirements=tuple(
                path.decode("utf-8") for path in tree if _is_active_requirement(path)
            ),
            archived_requirements=frozenset(
                path.decode("utf-8") for path in tree
                if path.startswith(b"docs/requirements/archive/")
            ),
            current_plan_id=current_plan_id,
            unverified_historical_ids=frozenset(unverified_historical_ids),
            known_statuses=frozenset(KNOWN_EXECPLAN_STATUSES),
            cache_versions={
                plan_id: tuple(
                    row[field] if field in row.keys() else None
                    for field in (
                        "path", "title", "sources", "depends_on",
                        "current_complexity", "current_risk", "priority",
                        "content_hash", "current_revision_sha",
                    )
                )
                for plan_id, row in states.items()
            },
        )
    except requirement_archive.ArchiveCollisionError as error:
        raise RequirementArchiveCollisionError(error.path) from error
    except requirement_archive.ArchiveSnapshotError as error:
        raise RequirementArchiveError(str(error)) from error
    return snapshot


def requirement_archive_candidates(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    current_plan_id: str,
) -> tuple[str, ...]:
    """Compatibility projection of a revision-pinned archive snapshot."""

    states = {
        str(row["plan_id"]): str(row["status"])
        for row in connection.execute(
            "SELECT plan_id, status FROM exec_plans WHERE repo = ?", (repo,)
        )
    }
    if current_plan_id not in states:
        raise RequirementArchiveError(
            f"현재 ExecPlan의 SQLite 상태 없음: {current_plan_id}"
        )
    for plan_id, status in states.items():
        if status not in KNOWN_EXECPLAN_STATUSES:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )
    if not requirement_archive_sources(connection, repo):
        return ()
    return requirement_archive_snapshot(
        connection, repo_path, repo, revision, current_plan_id,
        allow_future_publication=True,
    ).candidates

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
    def ignore_status(path: str) -> tuple[int, str]:
        result = git(
            worktree, "check-ignore", "--quiet", "--", path, check=False
        )
        return result.returncode, result.stdout

    def tracked_contents(path: str) -> bytes:
        object_id = _requirement_archive_blob(worktree, tracked_revision, path)
        return git_bytes(worktree, "cat-file", "blob", object_id.decode("ascii"))

    try:
        return requirement_archive.validated_moves(
            tree, worktree,
            blob_contents=lambda object_id: git_bytes(
                repo_path, "cat-file", "blob", object_id.decode("ascii")
            ),
            tracked_contents=tracked_contents if tracked_revision is not None else None,
            ignore_status=ignore_status if tracked_revision is None else None,
        )
    except requirement_archive.ArchiveCollisionError as error:
        raise RequirementArchiveCollisionError(error.path) from error
    except requirement_archive.ArchiveMoveError as error:
        raise RequirementArchiveError(str(error)) from error

def requirement_archive_reconciliation_error(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
    *,
    revision: str | None = None,
) -> str | None:
    if expected == actual:
        return None
    return (
        "Requirement archive 이동 집합이 최신 eligibility와 다릅니다.\n"
        + (f"기준 revision: {revision}\n" if revision else "")
        + f"기대: {json.dumps(expected, ensure_ascii=False)}\n"
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
    targeted_test_reason: str | None = None,
    review_result: str | None = None,
    *,
    session_recovery: bool = False,
    protocol_error: str | None = None,
) -> str:
    targeted_test_context = ""
    if targeted_test_reason:
        targeted_test_context = (
            "\n관련 targeted unit test를 실행하지 못한 사유:\n"
            f"{targeted_test_reason}\n"
        )
    recovery = ""
    if session_recovery:
        recovery = (
            "\n저장된 Reviewer 세션을 재개할 수 없어 새 세션에서 이어간다. "
            "아래 이전 Review 결과를 포함해 현재 diff를 다시 검토하라:\n"
            f"{(review_result or '이전 Review 결과 없음')[-6000:]}\n"
        )
    correction = ""
    if protocol_error:
        correction = (
            "\n직전 Reviewer 결과가 아래 프로토콜 검증에 실패했다:\n"
            f"{protocol_error}\n"
            "직전 결과:\n"
            f"{(review_result or '직전 Review 결과 없음')[-6000:]}\n"
            "검토 판단은 유지하되 아래 출력 계약에 맞춘 전체 결과만 다시 응답하라.\n"
        )
    return f"""
origin/{base}와 현재 HEAD의 차이를 읽기 전용으로 검토하라.
AGENTS.md, 근거 PRODUCT/Requirement, ARCHITECTURE와 ExecPlan을 기준으로 correctness, 회귀, 테스트 누락, verify 약화를 확인하라.
{targeted_test_context}
{recovery}
{correction}
테스트 command와 scripts/verify를 실행하지 말고, 코드·테스트·verify 스크립트의 diff와 필요한 미실행 사유만으로 판단하라.
blocking issue마다 설명 직전에 `ISSUE_ID: relative/path::stable-symbol-or-test::category-slug`를 한 줄로 기록하라.
relative/path는 `/`로 구분한 실제 저장소 상대 경로이며 공백과 대괄호를 허용하지만, 절대 경로·빈 구성요소·`.`·`..`·역슬래시·colon·제어문자는 허용하지 않는다.
ISSUE_ID에는 줄 번호나 자연어 설명을 넣지 말고, 같은 issue가 재발하면 위치·표현이 바뀌어도 이전과 정확히 같은 ID를 사용하라.
`VERDICT: FAIL`은 하나 이상의 ISSUE_ID가 있어야 하며, 서로 다른 issue는 서로 다른 ID를 사용하라.
근거 PRODUCT/Requirement/ARCHITECTURE 또는 ExecPlan의 누락·모순 때문에 구현 정답을 판정할 수 없으면 `SPEC_DEADLOCK: {{"kind":"conflict","locations":[{{"path":"저장소 상대 문서 경로","section":"절 이름"}}],"problem":"상충 내용 또는 빠진 기준","implementation_choice":"판단할 수 없는 구현 선택","question":"사용자가 문서에 보강할 결정"}}`를 한 줄로 기록하고 마지막 줄을 `VERDICT: SPEC_BLOCKED`로 끝내라. 누락이면 kind를 `missing`으로 쓴다.
locations는 현재 ExecPlan, 그 sources, docs/PRODUCT.md 또는 docs/ARCHITECTURE.md의 실제 경로와 절이어야 한다. conflict는 서로 다른 두 위치 이상, missing은 한 위치 이상 필요하다. ISSUE_ID와 함께 쓰지 마라. 현재 문서로 판단 가능한 코드·테스트 결함은 기존 FAIL로 보고하라.
blocking issue가 있으면 간결히 설명하고 마지막 줄을 `VERDICT: FAIL`로 끝내라.
blocking issue가 없으면 마지막 줄을 `VERDICT: PASS`로 끝내라. 파일은 수정하지 마라.
""".strip()


class _PlanningStatePorts:
    """Bind coordinator state transitions to the G-047 repositories."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        runtime_repository: runtime_state.RuntimeStateRepository,
        maintenance_repository: maintenance_state.MaintenanceRepository,
        state_dir: Path,
    ):
        self._connection = connection
        self._runtime_repository = runtime_repository
        self._maintenance_repository = maintenance_repository
        self._state_dir = state_dir

    def planning(self, repo: str) -> runtime_state.PlanningSnapshot:
        return self._runtime_repository.planning(repo)

    def maintenance_cycle(
        self, repo: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.get(repo)

    def update(self, repo: str, **values: object) -> None:
        update_project(self._connection, repo, **values)

    def transaction(self):
        return runtime_state._transaction(self._connection)

    def advance_maintenance_cycle(
        self, repo: str, base_sha: str
    ) -> maintenance_state.MaintenanceCycle:
        return _advance_maintenance_cycle_after_merges(
            self._connection, repo, base_sha
        )

    def schedule(self, repo: str) -> CatalogScheduleDecision:
        return catalog_schedule_decision(self._connection, repo)

    def pending_document_approval(
        self, repo: str, repository: Path, base_branch: str,
    ) -> bool:
        if not is_loop_manager_repository(repository):
            return False
        import document_drafts
        return bool(document_drafts.DocumentDraftRepository(
            self._connection,
        ).unplanned_approved(repo, repository, base_branch))

    def maintenance_due(
        self, repo: str, interval: int, *, self_repository: bool = False,
        completion_candidate: bool = False,
    ) -> maintenance_state.ReviewDue:
        return self._maintenance_repository.file_assessment_due(
            repo, interval, self_repository=self_repository,
            completion_candidate=completion_candidate,
        )

    def begin_maintenance_assessment(
        self,
        repo: str,
        interval: int,
        base_sha: str,
        *,
        include_general_planning: bool,
        self_repository: bool = False,
        completion_candidate: bool = False,
    ) -> tuple[maintenance_state.MaintenanceCycle, bool]:
        return self._maintenance_repository.begin_assessment_if_due(
            repo,
            interval,
            base_sha,
            include_general_planning=include_general_planning,
            self_repository=self_repository,
            completion_candidate=completion_candidate,
        )

    def settle_empty_stabilization_assessment(
        self,
        repo: str,
        interval: int,
        *,
        source_sha: str,
        repository_path: str,
        base_branch: str,
        inventory: maintenance_file_inventory.SourceInventory,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.settle_empty_stabilization_assessment(
            repo,
            interval,
            source_sha=source_sha,
            repository_path=repository_path,
            base_branch=base_branch,
            inventory=inventory,
        )

    def record_full_assessment(
        self, repo: str, *, expected_revision: int, source_sha: str, result: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.record_full_assessment(
            repo, expected_revision=expected_revision, source_sha=source_sha,
            result=result,
        )

    def restart_stale_maintenance(
        self,
        repo: str,
        expected: maintenance_state.MaintenanceCycle,
        base_sha: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.restart_stale_planning_context(
            repo,
            expected=expected,
            new_base_sha=base_sha,
        )

    def activate_ready(self, repo: str, base_sha: str) -> sqlite3.Row | None:
        return activate_ready_execplan(
            self._connection, repo, self._state_dir, base_sha
        )

    def preserve_diagnostics(
        self, repo: str, snapshot: runtime_state.PlanningSnapshot
    ) -> None:
        preserve_planning_diagnostics(self._state_dir, repo, snapshot)

    def clear_active(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None:
        clear_active(
            self._connection,
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )

    def clear_active_in_transaction(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None:
        _clear_active_in_current_transaction(
            self._connection,
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )

    def clear_validation(self, repo: str) -> None:
        clear_planning_validation_state(self._connection, repo)

    def compact_catalog(self, repo: str) -> str:
        return compact_execplan_catalog(self._connection, repo)

    def execplan_rows(self, repo: str) -> list[sqlite3.Row]:
        return prerequisite_validation_rows(self._connection, repo)

    def transition_maintenance(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        next_phase: maintenance_state.MaintenancePhase,
        prerequisite_plan_ids: tuple[str, ...],
        include_general_planning: bool,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.transition(
            repo,
            expected_phase=expected_phase,
            expected_revision=expected_revision,
            next_phase=next_phase,
            prerequisite_plan_ids=prerequisite_plan_ids,
            include_general_planning=include_general_planning,
        )

    def advance_success_baseline(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        remote_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.advance_success_baseline(
            repo,
            expected_phase=expected_phase,
            expected_revision=expected_revision,
            remote_commit=remote_commit,
        )

    def file_assessment_evidence(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
    ) -> maintenance_file_publication.AssessmentEvidence:
        return maintenance_file_publication.collect(
            self._connection, repo, repository, base_branch,
            cycle_revision, source_sha,
        )

    def complete_file_assessment(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
        evidence: maintenance_file_publication.AssessmentEvidence,
    ) -> None:
        with runtime_state._transaction(self._connection):
            maintenance_file_publication.promote(
                self._connection, repo, repository, base_branch, source_sha,
                evidence,
            )
            self._maintenance_repository.advance_file_assessment_success_baseline(
                repo, expected_revision=cycle_revision, source_sha=source_sha,
                expected_merged_count=evidence.merged_count,
                bump_revision=False,
            )

    def recover_maintenance_batch(
        self, repo: str, worktree: Path, base: str
    ) -> maintenance_state.MaintenanceCycle:
        return recover_maintenance_planning_batch(
            self._connection, repo, worktree, base
        )

    def record_maintenance_intent(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return record_maintenance_publication_intent(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
        )

    def publish_maintenance_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return publish_maintenance_planning_batch(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
        )

    def register_execplans(
        self,
        repo: str,
        worktree: Path,
        plans: list[tuple[str, dict[str, object]]],
        plan_commit: str,
        fingerprint: str,
    ) -> None:
        register_execplans(
            self._connection,
            repo,
            worktree,
            plans,
            plan_commit,
            fingerprint,
        )

    def general_publication(
        self, repo: str
    ) -> GeneralPlanningPublication | None:
        return general_planning_publication(self._connection, repo)

    def recover_general_batch(
        self, repo: str, worktree: Path, base: str
    ) -> str:
        return recover_general_planning_batch(
            self._connection, repo, worktree, base
        )

    def record_general_intent(
        self,
        repo: str,
        worktree: Path,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> GeneralPlanningPublication:
        return record_general_planning_publication_intent(
            self._connection,
            repo,
            worktree,
            validated_result,
            plan_commit,
            specification_fingerprint,
            planning_fingerprint,
        )

    def publish_general_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> None:
        publish_general_planning_batch(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
            specification_fingerprint,
            planning_fingerprint,
        )


class _PlanningGitPorts:
    """Bind coordinator Git operations to the managed-worktree helpers."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        repo_path: Path,
        state_dir: Path,
    ):
        self._connection = connection
        self._repo_path = repo_path
        self._state_dir = state_dir

    def run(
        self, repo: Path, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if len(arguments) == 3 and arguments[:2] == ("worktree", "remove"):
            workspace = Path(arguments[2])
            validate_managed_worktree(repo, workspace, None)
            _head, _branch, _index, dirty = _capture_local_worktree_state(workspace)
            if dirty or _git_operation_entries(workspace):
                raise WorkspaceOwnershipError(
                    f"planning worktree에 권위 있는 변경이 남았습니다: {workspace}"
                )
            if _ignored_worktree_paths(workspace):
                arguments = ("worktree", "remove", "--force", str(workspace))
        return git(repo, *arguments, check=check)

    def input_fingerprint(self, repo_path: Path, sha: str) -> str:
        return _planning_input_fingerprint(repo_path, sha)

    def specification_fingerprint(self, repo_path: Path, sha: str) -> str:
        return _specification_input_fingerprint(repo_path, sha)

    def requirement_archive_only(
        self, repo_path: Path, old_sha: str, new_sha: str
    ) -> bool:
        return _is_requirement_archive_only_change(repo_path, old_sha, new_sha)

    def validate_workspace_identity(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None:
        try:
            validate_planning_workspace_identity(
                self._state_dir, repo, fingerprint, worktree
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def validate_worktree_ownership(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None:
        try:
            validate_planning_worktree_ownership(
                self._connection,
                self._repo_path,
                self._state_dir,
                repo,
                fingerprint,
                worktree,
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def make_planning_worktree(
        self,
        repo_path: Path,
        worktree: Path,
        base: str,
        *,
        allow_create: bool,
        allow_existing: bool,
        ownership_reserved: bool,
        expected_head: str,
        before_create: Callable[[], None],
    ) -> None:
        try:
            make_planning_worktree(
                repo_path,
                worktree,
                base,
                allow_create=allow_create,
                allow_existing=allow_existing,
                ownership_reserved=ownership_reserved,
                expected_head=expected_head,
                before_create=before_create,
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def changed_files(
        self, worktree: Path, since: str | None = None
    ) -> list[str]:
        return changed_files(worktree, since)

    def validate_managed_worktree(self, worktree: Path) -> None:
        validate_managed_worktree(self._repo_path, worktree, None)


class _ExecPlanArchiveGitPort:
    """Concrete detached-worktree Git adapter for archive housekeeping."""

    def __init__(self, repo_path: Path, state_dir: Path, repo: str):
        self.repo_path = repo_path
        self.state_dir = state_dir
        self.repo = repo

    def fetch_remote_base(self, base_branch: str) -> str:
        return _recorded_base_fetch(
            self.repo_path, self.state_dir, base_branch, owner="project",
        )

    @staticmethod
    def _parse_tree_record(record: bytes) -> tuple[str, str, str, str]:
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("ExecPlan archive Git tree 항목 형식이 올바르지 않습니다.")
        mode, object_type, object_id = parts
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("ExecPlan archive Git 경로가 UTF-8이 아닙니다.") from error
        return (
            mode.decode("ascii"),
            object_type.decode("ascii"),
            object_id.decode("ascii"),
            path,
        )

    def _manifest(self, revision: str) -> dict[str, tuple[str, str, str]]:
        raw = git_bytes(
            self.repo_path,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            revision,
        )
        manifest: dict[str, tuple[str, str, str]] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            mode, object_type, object_id, path = self._parse_tree_record(record)
            manifest[path] = (mode, object_type, object_id)
        return manifest

    def _entry(
        self, revision: str, path: str
    ) -> execplan_archive.ArchiveTreeEntry | None:
        raw = git_bytes(
            self.repo_path,
            "ls-tree",
            "-z",
            revision,
            "--",
            f":(literal){path}",
        )
        records = [record for record in raw.split(b"\0") if record]
        exact = []
        for record in records:
            mode, object_type, object_id, observed = self._parse_tree_record(record)
            if observed == path:
                exact.append((mode, object_type, object_id))
        if not exact:
            return None
        if len(exact) != 1:
            raise RuntimeError(f"ExecPlan archive 경로가 중복 관측됐습니다: {path}")
        mode, object_type, object_id = exact[0]
        contents = (
            git_bytes(self.repo_path, "cat-file", "blob", object_id)
            if object_type == "blob"
            else b""
        )
        return execplan_archive.ArchiveTreeEntry(
            mode,
            object_type,
            object_id,
            execplan_catalog.plan_content_hash(contents),
        )

    def definitions_current(
        self,
        base_sha: str,
        candidates: Sequence[execplan_archive_state.MergedArchiveCandidate],
    ) -> bool:
        for candidate in candidates:
            source = self._entry(base_sha, candidate.active_path)
            destination = self._entry(base_sha, candidate.archive_path)
            observed = source if source is not None else destination
            if observed is None or not observed.regular:
                return False
            if observed.content_hash != candidate.content_hash:
                return False
        return True

    def observe(
        self,
        revision: str,
        candidates: Sequence[execplan_archive_state.MergedArchiveCandidate],
    ) -> tuple[execplan_archive.ArchiveCandidateObservation, ...]:
        conflicts = execplan_catalog.conflicting_archived_plan_paths(
            self._manifest(revision)
        )
        return tuple(
            execplan_archive.ArchiveCandidateObservation(
                candidate,
                self._entry(revision, candidate.active_path),
                self._entry(revision, candidate.archive_path),
                next(
                    (
                        path for path in conflicts.get(candidate.plan_id, ())
                        if path != candidate.archive_path
                    ),
                    None,
                ),
            )
            for candidate in candidates
        )

    def _workspace(self, operation_id: str) -> Path:
        project_id = hashlib.sha256(self.repo.encode("utf-8")).hexdigest()[:16]
        workspace = (
            self.state_dir
            / "worktrees"
            / project_id
            / "execplan-archive"
            / operation_id
        )
        _validate_managed_path_boundary(
            self.state_dir,
            workspace,
            workspace,
            "ExecPlan archive worktree",
        )
        return workspace

    @staticmethod
    def _expected_moves(
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((move.source, move.destination) for move in moves))

    def _validate_archive_tree(
        self,
        base_sha: str,
        revision: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> str:
        before = self._manifest(base_sha)
        after = self._manifest(revision)
        classification = execplan_catalog.classify_archive_only_tree_change(
            before, after
        )
        actual = tuple(
            sorted((move.source, move.destination) for move in classification.moves)
        )
        if not classification.execplan_only or actual != self._expected_moves(moves):
            raise RuntimeError(
                "archive commit diff가 검증된 ExecPlan rename-only 집합과 다릅니다."
            )
        return git(
            self.repo_path, "rev-parse", f"{revision}^{{tree}}"
        ).stdout.strip()

    def _prepared_from_commit(
        self,
        base_sha: str,
        operation_id: str,
        commit_oid: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> execplan_archive.PreparedArchive:
        parents = git(
            self.repo_path,
            "rev-list",
            "--parents",
            "-n",
            "1",
            commit_oid,
        ).stdout.split()
        if len(parents) != 2 or parents[1] != base_sha:
            raise RuntimeError("archive commit은 원격 base의 단일 자식이어야 합니다.")
        final_hash = self._validate_archive_tree(base_sha, commit_oid, moves)
        return execplan_archive.PreparedArchive(
            operation_id, base_sha, final_hash, commit_oid
        )

    def _remove_clean_workspace(self, workspace: Path) -> None:
        if not (workspace.exists() or workspace.is_symlink()):
            return
        validate_managed_worktree(self.repo_path, workspace, None)
        status = git(workspace, "status", "--porcelain=v1", "-z").stdout
        if status:
            raise RuntimeError(
                f"archive 임시 worktree에 예상하지 못한 변경이 남았습니다: {workspace}"
            )
        removed = git(
            self.repo_path,
            "worktree",
            "remove",
            str(workspace),
            check=False,
        )
        if removed.returncode:
            raise RuntimeError(
                "archive 임시 worktree 제거 실패:\n" + removed.stdout[-6000:]
            )

    def _discard_staged_workspace(
        self,
        workspace: Path,
        base_sha: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> None:
        """Remove only a proven Manager-owned archive staging workspace."""

        validate_managed_worktree(self.repo_path, workspace, None)
        head = git(workspace, "rev-parse", "HEAD").stdout.strip()
        if head != base_sha:
            raise RuntimeError(
                "archive base-race worktree HEAD가 시작 base와 다릅니다."
            )
        staged_tree = git(workspace, "write-tree").stdout.strip()
        self._validate_archive_tree(base_sha, staged_tree, moves)
        unstaged = git(
            workspace, "diff", "--name-only", "-z", "--"
        ).stdout
        untracked = git(
            workspace,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout
        if unstaged or untracked:
            raise RuntimeError(
                "archive base-race worktree에 검증되지 않은 파일 변경이 있습니다."
            )
        for move in reversed(moves):
            git(workspace, "mv", "--", move.destination, move.source)
        restored_tree = git(workspace, "write-tree").stdout.strip()
        base_tree = git(
            self.repo_path, "rev-parse", f"{base_sha}^{{tree}}"
        ).stdout.strip()
        if restored_tree != base_tree:
            raise RuntimeError(
                "archive base-race worktree를 원래 tree로 복원하지 못했습니다."
            )
        self._remove_clean_workspace(workspace)

    @staticmethod
    def _stage_archive_moves(
        workspace: Path,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> None:
        """Stage recovered renames without requiring deleted paths to exist."""

        for move in moves:
            git(
                workspace,
                "update-index",
                "--force-remove",
                "--",
                move.source,
            )
        destinations = [
            f":(literal){move.destination}" for move in moves
        ]
        git(workspace, "add", "--", *destinations)

    @staticmethod
    def _commit_archive(workspace: Path, message: str) -> None:
        """Create a Manager-owned commit without ambient identity or hooks."""

        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Loop Manager",
                "GIT_AUTHOR_EMAIL": "loop-manager@localhost",
                "GIT_COMMITTER_NAME": "Loop Manager",
                "GIT_COMMITTER_EMAIL": "loop-manager@localhost",
            }
        )
        with tempfile.TemporaryDirectory(
            prefix="loop-manager-execplan-archive-hooks-"
        ) as hooks_directory:
            committed = command(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={hooks_directory}",
                    "-c",
                    "commit.gpgSign=false",
                    "-C",
                    str(workspace),
                    "commit",
                    "-m",
                    message,
                ],
                check=False,
                env=environment,
            )
        if committed.returncode:
            output = committed.stdout[-6000:].strip()
            detail = f"archive commit 실패:\n{output}"
            if is_git_lock_failure(output):
                raise GitLockError(detail)
            raise RuntimeError(detail)

    def prepare_commit(
        self,
        base_sha: str,
        base_branch: str,
        operation_id: str,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> execplan_archive.PreparedArchive:
        workspace = self._workspace(operation_id)
        if workspace.exists() or workspace.is_symlink():
            validate_managed_worktree(self.repo_path, workspace, None)
            head = git(workspace, "rev-parse", "HEAD").stdout.strip()
            if head != base_sha:
                return self._prepared_from_commit(
                    base_sha, operation_id, head, moves
                )
        else:
            workspace.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo_path, "worktree", "prune", "--expire", "now")
            git(
                self.repo_path,
                "worktree",
                "add",
                "--detach",
                str(workspace),
                base_sha,
            )
            validate_managed_worktree(self.repo_path, workspace, None)

        for move in moves:
            source = workspace / move.source
            destination = workspace / move.destination
            source_present = source.exists() or source.is_symlink()
            destination_present = destination.exists() or destination.is_symlink()
            if source_present and not destination_present:
                destination.parent.mkdir(parents=True, exist_ok=True)
                git(workspace, "mv", "--", move.source, move.destination)
            elif not source_present and destination_present:
                continue
            else:
                raise RuntimeError(
                    "archive 임시 worktree의 rename 상태가 불확실합니다: "
                    f"{move.source} -> {move.destination}"
                )

        self._stage_archive_moves(workspace, moves)
        staged_tree = git(workspace, "write-tree").stdout.strip()
        self._validate_archive_tree(base_sha, staged_tree, moves)
        if self.fetch_remote_base(base_branch) != base_sha:
            self._discard_staged_workspace(workspace, base_sha, moves)
            raise execplan_archive.ArchiveRemoteBaseChanged(
                "archive commit 직전 원격 base가 변경됐습니다."
            )
        plan_ids = ", ".join(sorted(move.plan_id for move in moves))
        self._commit_archive(
            workspace, f"Archive merged ExecPlans: {plan_ids}"
        )
        commit_oid = git(workspace, "rev-parse", "HEAD").stdout.strip()
        prepared = self._prepared_from_commit(
            base_sha, operation_id, commit_oid, moves
        )
        return prepared

    def commit_matches(
        self,
        prepared: execplan_archive.PreparedArchive,
        moves: Sequence[execplan_archive.ArchiveMove],
    ) -> bool:
        exists = git(
            self.repo_path,
            "cat-file",
            "-e",
            f"{prepared.commit_oid}^{{commit}}",
            check=False,
        )
        if exists.returncode:
            return False
        actual = self._prepared_from_commit(
            prepared.base_sha,
            prepared.operation_id,
            prepared.commit_oid,
            moves,
        )
        if actual.final_hash != prepared.final_hash:
            return False
        return True

    def cleanup_prepared(
        self, prepared: execplan_archive.PreparedArchive
    ) -> None:
        workspace = self._workspace(prepared.operation_id)
        if not (workspace.exists() or workspace.is_symlink()):
            return
        validate_managed_worktree(self.repo_path, workspace, None)
        head = git(workspace, "rev-parse", "HEAD").stdout.strip()
        if head != prepared.commit_oid:
            raise RuntimeError(
                "archive 임시 worktree HEAD가 저장된 commit과 다릅니다: "
                f"{head} != {prepared.commit_oid}"
            )
        self._remove_clean_workspace(workspace)

    def push_fast_forward(
        self,
        prepared: execplan_archive.PreparedArchive,
        base_branch: str,
    ) -> execplan_archive.ArchivePushResult:
        pushed = git(
            self.repo_path,
            "push",
            "origin",
            f"{prepared.commit_oid}:refs/heads/{base_branch}",
            check=False,
        )
        try:
            remote_oid = self.fetch_remote_base(base_branch)
        except Exception as error:
            return execplan_archive.ArchivePushResult(
                False,
                error=f"push 결과 원격 확인 실패: {error}",
            )
        if remote_oid == prepared.commit_oid:
            return execplan_archive.ArchivePushResult(True, remote_oid)
        if remote_oid != prepared.base_sha:
            return execplan_archive.ArchivePushResult(
                False,
                remote_oid=remote_oid,
                error="push 도중 원격 base가 변경됐습니다.",
                base_changed=True,
            )
        return execplan_archive.ArchivePushResult(
            False,
            remote_oid=remote_oid,
            error=pushed.stdout[-6000:] or "fast-forward push가 거부됐습니다.",
        )


def dispatch_execplan_archive_housekeeping(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    base_branch: str,
) -> execplan_archive.ArchiveDispatchOutcome:
    """Assemble the archive service and map its result to operator logging."""

    def refresh_candidates(base_sha: str) -> bool:
        result = reconcile_execplan_catalog(
            connection, repo_path, repo, base_branch
        )
        if result.kind is not execplan_catalog_state.ReconciliationKind.RECONCILED:
            return False
        current = git(
            repo_path, "rev-parse",
            f"refs/remotes/origin/{base_branch}^{{commit}}",
        ).stdout.strip()
        return result.revision_sha == base_sha and current == base_sha

    outcome = execplan_archive.ExecPlanArchiveDispatcher(
        execplan_archive_state.ExecPlanArchiveRepository(connection),
        _ExecPlanArchiveGitPort(repo_path, state_dir, repo),
        refresh_candidates=refresh_candidates,
    ).dispatch(repo, base_branch)
    if outcome.commit_oid:
        operation_print(
            "MERGED ExecPlan archive 게시 완료: "
            + ", ".join(outcome.archived_plan_ids)
        )
    for plan_id, reason in outcome.pending:
        operation_print(
            f"ExecPlan archive 대기 ({plan_id}): {reason}",
            file=sys.stderr,
        )
    return outcome


def _execplan_archive_housekeeping_safe(
    connection: sqlite3.Connection,
    repo: str,
    validated_agent_scope: object | None,
    *,
    maintenance_assessment_enabled: bool,
    refactoring_review_interval: int,
) -> bool:
    """Allow base mutation only between active project lifecycles."""

    if scoped_blocking.project_block(connection, repo) is not None:
        return False
    current = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if (
        current is None
        or current["phase"] is not None
        or runtime_state.planning_owns_execution_slot(current)
        or active_execplan(connection, repo) is not None
        or validated_agent_scope is not None
    ):
        return False
    maintenance_repository = maintenance_state.MaintenanceRepository(
        connection
    )
    maintenance_cycle = maintenance_repository.get(repo)
    if maintenance_cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        return False
    if maintenance_assessment_enabled:
        decision = catalog_schedule_decision(connection, repo)
        if (
            not decision.has_waiting
            and maintenance_repository.file_assessment_due(
                repo, refactoring_review_interval,
                self_repository=is_loop_manager_repository(Path(repo)),
            ).required
        ):
            return False
    return True


class _PlanningAgentPorts:
    """Bind Planner CLI and Git scope validation to the coordinator."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        context: PlanningContext,
        planner_mutex: threading.Lock | None,
    ):
        self._connection = connection
        self._context = context
        self._planner_mutex = planner_mutex

    def begin_scope(self, role: str, worktree: Path) -> GitSnapshot:
        return begin_agent_scope(
            self._connection, self._context.repo, worktree, role
        )

    def run_planner(
        self,
        worktree: Path,
        prompt: str,
        session_id: str | None,
        on_session_started: Callable[[str], None],
    ) -> PlanningAgentResult:
        context = self._context
        guard = self._planner_mutex or contextlib.nullcontext()
        with guard:
            with self._connection:
                resumed = notifications.record_work_start(
                    self._connection, context.repo, "Planner",
                )
            if resumed:
                operation_log(f"차단 해제 후 Planner 작업 재개: {context.repo}")
            result = run_agent_with_retries(
                TrackedAgentCall(lambda current_session_id, _session_recovery, invocation: run_agent(
                    context.provider,
                    worktree,
                    context.state_dir,
                    context.config_path,
                    "planner",
                    prompt,
                    read_only=False,
                    persist_session=True,
                    session_id=current_session_id,
                    on_session_started=on_session_started,
                    connection=self._connection,
                    invocation=invocation,
                ), AttemptSequence(context.repo, None)),
                session_id=session_id,
            )
        kind = {
            AgentResultKind.SUCCESS: PlanningAgentResultKind.SUCCESS,
            AgentResultKind.TRANSIENT_FAILURE: (
                PlanningAgentResultKind.TRANSIENT_FAILURE
            ),
            AgentResultKind.USAGE_LIMIT: PlanningAgentResultKind.USAGE_LIMIT,
            AgentResultKind.PERMANENT_CONFIGURATION: (
                PlanningAgentResultKind.PERMANENT_CONFIGURATION
            ),
        }[result.result_kind]
        message = result.message
        if result.code and kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
            message = f"{TRANSIENT_AGENT_ERROR_PREFIX} {message}"
        elif result.code and kind is PlanningAgentResultKind.USAGE_LIMIT:
            message = f"{USAGE_LIMIT_ERROR_PREFIX} {message}"
        return PlanningAgentResult(
            result.code,
            message,
            result.session_id,
            kind,
        )

    def finish_scope(
        self, role: str, worktree: Path, before: GitSnapshot
    ) -> PlanningScopeResult:
        after: GitSnapshot | None = None
        try:
            after = capture_git_snapshot(worktree)
            validate_agent_scope(
                role,
                worktree,
                before,
                after,
                external_worktree=self._context.repo_path,
                base_branch=self._context.base_branch,
            )
            validate_managed_worktree(
                self._context.repo_path, worktree, None
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            return PlanningScopeResult(
                str(error), after.serialize() if after is not None else None
            )
        return PlanningScopeResult()

    def validate_saved_scope(
        self, role: str, worktree: Path, snapshot: str
    ) -> PlanningScopeResult:
        after: GitSnapshot | None = None
        try:
            before = GitSnapshot.deserialize(snapshot)
            after = capture_git_snapshot(worktree)
            validate_agent_scope(
                role,
                worktree,
                before,
                after,
                external_worktree=self._context.repo_path,
                base_branch=self._context.base_branch,
            )
            validate_managed_worktree(
                self._context.repo_path, worktree, None
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            return PlanningScopeResult(
                str(error), after.serialize() if after is not None else None
            )
        return PlanningScopeResult()

    def clear_scope(self, repo: str) -> None:
        clear_agent_scope(self._connection, repo)


class _PlanningValidationPorts:
    """Bind prompt and validation helpers without lifecycle decisions."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def maintenance_context_at_revision(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> MaintenancePlanningContext:
        return _maintenance_context_at_revision(
            self._connection, repo, worktree, active_sha, cycle
        )

    def prepare_maintenance_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MaintenancePlanningContext:
        return prepare_maintenance_planning_context(
            self._connection, repo, worktree, active_sha
        )

    def maintenance_change_context(
        self,
        repo_path: Path,
        base: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> str:
        return _maintenance_change_context_text(repo_path, base, cycle)

    def prepare_general_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MutableReadySnapshot:
        return mutable_ready_snapshot(
            self._connection, repo, worktree, active_sha
        )

    def planner_prompt(
        self,
        error: str | None,
        catalog: str,
        mutable_snapshot: MutableReadySnapshot,
    ) -> str:
        prompt = planner_prompt(error, catalog, mutable_snapshot)
        import document_drafts
        repository = Path(mutable_snapshot.repo)
        if is_loop_manager_repository(repository):
            project = self._connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?",
                (mutable_snapshot.repo,),
            ).fetchone()
            if project is not None:
                candidate = document_drafts.DocumentDraftRepository(
                    self._connection,
                ).planning_candidate(
                    mutable_snapshot.repo, repository, str(project["base_branch"]),
                )
                if candidate is not None:
                    prompt += document_publication.planning_instruction(candidate)
        return prompt

    def maintenance_prompt(
        self,
        context: MaintenancePlanningContext,
        catalog: str,
        change_context: str,
        error: str | None,
        *,
        include_general_planning: bool,
    ) -> str:
        return maintenance_planner_prompt(
            context,
            catalog,
            change_context,
            error,
            include_general_planning=include_general_planning,
        )

    def validate_general_result(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        dirty_files: list[str],
        mutable_snapshot: MutableReadySnapshot,
    ) -> ValidatedPlanningResult | None:
        result = validate_planner_result(
            worktree,
            active_sha,
            dirty_files,
            connection=self._connection,
            repo=repo,
            mutable_snapshot=mutable_snapshot,
        )
        import document_drafts
        repository = Path(repo)
        if not is_loop_manager_repository(repository):
            return result
        project = self._connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if project is None:
            return result
        drafts = document_drafts.DocumentDraftRepository(self._connection)
        candidate = drafts.planning_candidate(
            repo, repository, str(project["base_branch"]),
        )
        claims = []
        for path, plan in result.plans if result is not None else ():
            text = document_publication.read_plan_text(worktree / path)
            try:
                claim = document_publication.plan_claim(text)
            except document_drafts.DocumentDraftError as error:
                raise PlannerValidationError(str(error)) from error
            if claim is not None:
                claims.append((text, plan, claim))
        if candidate is not None:
            if (result is None or len(result.plans) != 1 or result.revisions
                or result.deletions or result.publication_paths != (result.plans[0][0],)
                or len(claims) != 1 or claims[0][2] != candidate.draft_id):
                raise PlannerValidationError(
                    "승인 문서 수정본에는 별도 새 ExecPlan 하나만 필요합니다."
                )
            try:
                drafts.assert_current(candidate, repository, str(project["base_branch"]))
                document_publication.validate_document_plan(
                    claims[0][0], claims[0][1], candidate,
                )
            except document_drafts.DocumentDraftError as error:
                drafts.record_publication_error(candidate.draft_id, str(error))
                raise PlannerValidationError(str(error)) from error
        elif claims:
            raise PlannerValidationError("계획할 승인 문서 수정본이 없습니다.")
        return result

    def validate_maintenance_result(
        self,
        context: MaintenancePlanningContext,
        worktree: Path,
        dirty_files: list[str],
        *,
        refactoring_plan_ids: tuple[str, ...],
        allow_general_plans: bool,
    ) -> ValidatedPlanningResult | None:
        return validate_maintenance_planner_result(
            self._connection,
            context,
            worktree,
            dirty_files,
            refactoring_plan_ids=refactoring_plan_ids,
            allow_general_plans=allow_general_plans,
        )

    def validate_publication_base(
        self,
        repo: str,
        repo_path: Path,
        base: str,
        context: MaintenancePlanningContext,
    ) -> None:
        validate_maintenance_publication_base(
            self._connection, repo, repo_path, base, context
        )


class _PlanningEventPorts:
    def log(self, message: str) -> None:
        operation_log(message)

    def info(self, message: str) -> None:
        operation_print(message)

    def error(self, message: str) -> None:
        operation_print(message, file=sys.stderr)


class _PlanningFileReviewPorts:
    """Keep provisional file units outside the success ledger and Planner row."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._repository = maintenance_file_review.ProvisionalReviewRepository(connection)

    def empty_selection_inventory(
        self, repo: str, repository: Path, base: str, source_revision: str,
    ) -> maintenance_file_inventory.SourceInventory | None:
        inventory = maintenance_file_inventory.cached_source_inventory(
            self._connection, repository, base, source_revision,
        )
        selected = maintenance_file_ledger.FileReviewRepository(
            self._connection,
        ).selected_sources(repo, inventory)
        return None if selected else inventory

    def progress(
        self, repo: str, repository: Path, base: str, cycle_revision: int,
        source_revision: str,
    ) -> maintenance_file_review.FileReviewProgress:
        inventory = maintenance_file_inventory.cached_source_inventory(
            self._connection, repository, base, source_revision,
        )
        return self._repository.progress(
            repo, repository, base, cycle_revision, inventory,
        )

    def content(self, repository: Path, base: str, source: maintenance_file_inventory.TrackedSource) -> bytes:
        return maintenance_file_review.source_bytes(repository, base, source)

    def start(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource) -> None:
        self._repository.start(repo, cycle_revision, source)

    def session(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, session_id: str) -> None:
        self._repository.session(repo, cycle_revision, source, session_id)

    def complete(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, result: dict) -> None:
        self._repository.complete(repo, cycle_revision, source, result)

    def fail(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, error: str) -> None:
        self._repository.fail(repo, cycle_revision, source, error)

    def completed_summary(self, repo: str) -> dict:
        return self._repository.completed_summary(repo)


class _PlanningReconciliationPorts:
    """Keep relation evidence provisional until G-086 publication succeeds."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._repository = maintenance_reconciliation.ReconciliationRepository(connection)
        self._synthesis = maintenance_reconciliation.SynthesisRepository(connection)
        self._findings = maintenance_findings.FindingsRepository(connection)
        self._reviews = maintenance_file_review.ProvisionalReviewRepository(connection)

    def progress(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 source_revision: str) -> maintenance_reconciliation.ReconciliationProgress:
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, source_revision)
        reviews = self._reviews.progress(repo, repository, base, cycle_revision, inventory)
        return self._repository.progress(repo, repository, base, cycle_revision, inventory, reviews)

    def synthesis(self, repo: str, cycle: int, revision: str,
                  progress: maintenance_reconciliation.ReconciliationProgress):
        return self._synthesis.prepare(repo, cycle, revision, progress)

    def synthesis_export(self, repo: str, page, progress, directory: Path) -> Path:
        return self._synthesis.export(repo, page, progress, directory)

    def synthesis_session(self, repo: str, page, session_id: str) -> None:
        self._synthesis.session(repo, page, session_id)

    def synthesis_complete(self, repo: str, page, output: str) -> None:
        self._synthesis.complete(repo, page, output)

    def content(self, repository: Path, base: str, source: maintenance_file_inventory.TrackedSource) -> bytes:
        return maintenance_file_review.source_bytes(repository, base, source)

    def start(self, repo: str, cycle_revision: int, revision: str,
              relation: maintenance_reconciliation.Relation) -> None:
        self._repository.start(repo, cycle_revision, revision, relation)

    def session(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
                session_id: str) -> None:
        self._repository.session(repo, cycle_revision, relation, session_id)

    def complete(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
                 result: dict) -> None:
        self._repository.complete(repo, cycle_revision, relation, result)

    def fail(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
             error: str) -> None:
        self._repository.fail(repo, cycle_revision, relation, error)

    def unit(self, repo: str, relation: maintenance_reconciliation.Relation) -> tuple[str | None, str | None]:
        return self._repository.unit(repo, relation)

    def resolution_session(self, repo, unit, session_id):
        self._synthesis.resolution_session(repo, unit, session_id)

    def resolution_complete(self, repo, unit, output):
        self._synthesis.resolution_complete(repo, unit, output)

    def resolution_fail(self, repo, unit, error):
        self._synthesis.resolution_fail(repo, unit, error)

    def _plan_texts(self, repository: Path, revision: str, existing_plans: dict[str, str]) -> dict[str, str]:
        texts = {}
        for raw_path in _git_tree_entries(repository, revision):
            if not re.fullmatch(rb"docs/plans/[^/]+\.md", raw_path):
                continue
            path = raw_path.decode("utf-8")
            text = git_bytes(repository, "show", f"{revision}:{path}").decode("utf-8")
            plan_id = str(parse_frontmatter(text).get("id", ""))
            if plan_id in existing_plans:
                texts[plan_id] = text
        if set(texts) != set(existing_plans):
            raise maintenance_reconciliation.ReconciliationError("고정 비종료 계획의 원문이 누락됐습니다.")
        return texts

    def findings_unit(self, repo, repository, base, cycle, revision, existing_plans, directory):
        progress = self.progress(repo, repository, base, cycle, revision)
        synthesis = self.synthesis(repo, cycle, revision, progress)
        if synthesis.index != synthesis.total:
            raise maintenance_reconciliation.ReconciliationError("발견 수집 전 모든 증거 페이지가 필요합니다.")
        texts = self._plan_texts(repository, revision, existing_plans)
        unit = self._findings.prepare(repo, cycle, revision, synthesis.digest, existing_plans, texts)
        evidence = self.synthesis_export(repo, synthesis, progress, directory)
        manifest = self._findings.export(repo, unit, existing_plans, texts, evidence)
        return unit, manifest

    def findings_session(self, repo, unit, session_id):
        self._findings.session(repo, unit, session_id)

    def findings_fail(self, repo, unit, error):
        self._findings.fail(repo, unit, error)

    def findings_complete(self, repo, repository, base, cycle, revision, existing_plans, unit, output):
        progress = self.progress(repo, repository, base, cycle, revision)
        synthesis = self.synthesis(repo, cycle, revision, progress)
        texts = self._plan_texts(repository, revision, existing_plans)
        fresh = self._findings.prepare(repo, cycle, revision, synthesis.digest, existing_plans, texts)
        if (fresh.manifest_id, fresh.index) != (unit.manifest_id, unit.index):
            raise maintenance_reconciliation.ReconciliationError("발견 수집 중 source·계획 집합이 바뀌었습니다.")
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, revision)
        sources = {item.path: item for item in inventory.sources}
        class Contents(dict):
            def __missing__(self, path):
                self[path] = maintenance_file_review.source_bytes(repository, base, sources[path])
                return self[path]
        def validate(value):
            new_ids = tuple(sorted({item.get("plan_id", "") for item in value["findings"] if item.get("status") == "new_plan"}))
            maintenance_reconciliation.parse_findings(value, progress, inventory, Contents(),
                existing_plans, new_ids, texts, synthesis_digest=synthesis.digest)
        self._findings.complete(repo, unit, output, existing_plans, validate)

    def validate(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 revision: str, output: str, existing_plans: dict[str, str],
                 new_plan_ids: tuple[str, ...], *, resolution_directory: Path | None = None
                 ) -> dict | maintenance_reconciliation.ResolutionUnit:
        progress = self.progress(repo, repository, base, cycle_revision, revision)
        synthesis = self.synthesis(repo, cycle_revision, revision, progress)
        if synthesis.index != synthesis.total:
            raise maintenance_reconciliation.ReconciliationError("종합 증거 페이지 검토가 완료되지 않았습니다.")
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, revision)
        sources = {source.path: source for source in inventory.sources}

        class CurrentContents(dict):
            def __missing__(self, path: str) -> bytes:
                content = maintenance_file_review.source_bytes(repository, base, sources[path])
                self[path] = content
                return content

        plan_texts = self._plan_texts(repository, revision, existing_plans)
        collection = self._findings.prepare(repo, cycle_revision, revision, synthesis.digest, existing_plans, plan_texts)
        payload = self._findings.resolve(repo, collection, output, existing_plans)

        findings = maintenance_reconciliation.parse_findings(
            payload, progress, inventory, CurrentContents(), existing_plans, new_plan_ids, plan_texts,
            synthesis_digest=synthesis.digest,
        )
        if resolution_directory is not None and synthesis.total:
            manifest = self.synthesis_export(repo, synthesis, progress, resolution_directory)
            unit = self._synthesis.next_resolution(repo, synthesis, findings, manifest)
            if unit is not None:
                return unit
        self._synthesis.validate_resolutions(repo, synthesis, findings)
        return findings


class _PlanningLifecyclePorts:
    """Concrete bootstrap bindings for the independent coordinator."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        runtime_repository: runtime_state.RuntimeStateRepository,
        maintenance_repository: maintenance_state.MaintenanceRepository,
        context: PlanningContext,
        planner_mutex: threading.Lock | None,
    ):
        self.state = _PlanningStatePorts(
            connection, runtime_repository, maintenance_repository, context.state_dir
        )
        self.git = _PlanningGitPorts(
            connection, context.repo_path, context.state_dir
        )
        self.agent = _PlanningAgentPorts(connection, context, planner_mutex)
        self.validation = _PlanningValidationPorts(connection)
        self.file_review = _PlanningFileReviewPorts(connection)
        self.reconciliation = _PlanningReconciliationPorts(connection)
        self.events = _PlanningEventPorts()


def _planning_outcome_step_result(outcome: PlanningOutcome) -> int | None:
    """Translate a typed coordinator result at the process adapter boundary."""

    if outcome.kind in {
        PlanningOutcomeKind.EXECUTION_READY,
        PlanningOutcomeKind.REFACTORING,
    }:
        return None
    if outcome.kind in {
        PlanningOutcomeKind.FINGERPRINT_SKIPPED,
        PlanningOutcomeKind.NO_CHANGE,
        PlanningOutcomeKind.WAITING,
    }:
        return 0
    if outcome.kind in {
        PlanningOutcomeKind.PREREQUISITE_WAIT,
        PlanningOutcomeKind.REMOTE_BASE_RESTART,
        PlanningOutcomeKind.PUBLICATION_RECOVERY,
    }:
        return PROJECT_STEP_RETRY
    if outcome.kind is PlanningOutcomeKind.TRANSIENT_PAUSE:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is PlanningOutcomeKind.USAGE_LIMIT_PAUSE:
        return PROJECT_STEP_USAGE_PAUSE
    return 1


def reconcile_false_positive_runtime_block(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    block: sqlite3.Row,
    runtime_mutation: RuntimeMutation,
) -> bool:
    """Reconcile a saved prevalidation result only when its entire delta is ours."""
    if (
        _canonical_path(repo_path) != _canonical_path(runtime_mutation.source)
        or not str(block["evidence"] or "").startswith("Agent 범위 선검증 실패:")
        or not project["agent_snapshot"]
        or not project["agent_result_snapshot"]
    ):
        return False
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
        saved_worktree = Path(str(project["worktree"] or repo))
        current = capture_git_snapshot(saved_worktree)
        runtime_path = str(_canonical_path(runtime_mutation.path))
        expected = agent_scope_decision.reconcile_runtime_snapshot(
            before, before, current, runtime_path,
        )
        if result != current:
            operation_log(
                f"runtime 차단 재조정 거부: 결과 snapshot 불일치 "
                f"path={runtime_path} sha={runtime_mutation.commit_sha}",
                error=True,
            )
            return False
        if expected != current:
            deltas = []
            for label, older, newer in (
                ("refs", expected.refs, current.refs),
                ("worktrees", expected.worktrees, current.worktrees),
                ("config", expected.config, current.config),
                ("operations", expected.operations, current.operations),
            ):
                changed = sorted(
                    key for key in set(older) | set(newer)
                    if older.get(key) != newer.get(key)
                )
                if changed:
                    deltas.append(f"{label}={','.join(changed[:8])}")
            if any(getattr(expected, key) != getattr(current, key)
                   for key in ("head", "branch", "index", "dirty")):
                deltas.append("local-state")
            operation_log(
                f"runtime 차단 재조정 거부: path={runtime_path} "
                f"sha={runtime_mutation.commit_sha} delta={'; '.join(deltas)}",
                error=True,
            )
            return False
        validate_persisted_agent_scope(
            connection, repo_path, state_dir, repo,
            runtime_mutation=runtime_mutation,
        )
    except (AgentScopeError, RuntimeError, ValueError, KeyError):
        return False
    return True


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
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    _check_runtime_handoff()
    # Resolve the persisted project key without consulting repository config.
    # An interrupted Agent may have changed origin or hooksPath, so the saved
    # snapshot must be checked before normal repository validation reads it.
    repo_path = Path(repo_arg).expanduser().resolve()
    repo = str(repo_path)
    if not registered_only:
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
    require_self_pr_base = _self_runtime_project(
        connection, repo, state_dir, runtime_mutation,
    ) is not None
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
    initial_project_block = (
        scoped_blocking.project_block(connection, repo) if row is not None else None
    )
    if row is not None and initial_project_block is not None and runtime_mutation is not None:
        reconcile_false_positive_runtime_block(
            connection, repo_path, state_dir, repo, row,
            initial_project_block, runtime_mutation,
        )
    if (
        row is not None
        and initial_project_block is not None
        and initial_project_block["reason_code"]
        != BlockReasonCode.EXECPLAN_CATALOG_INVALID.value
    ):
        synchronize_waiting_pull_requests(
            connection, repo_path, state_dir, repo,
            require_base_identity=require_self_pr_base,
            after_observation=lambda observation: _handoff_observed_self_merge(
                connection, repo, state_dir, runtime_mutation, observation,
            ),
        )
        resume_project_block_if_safe(connection, repo_path, state_dir, repo)
        project_block = scoped_blocking.project_block(connection, repo)
        if project_block is not None:
            operation_print(
                "프로젝트 안전 차단 상태입니다: "
                + str(project_block["evidence"]),
                file=sys.stderr,
            )
            return 1
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
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
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
        except AgentScopeError as error:
            detail = str(error)
            scope_result_snapshot = error.result_snapshot
            scope_reason = error.reason_code
        except RuntimeError:
            detail = "저장된 Agent Git 상태를 안전하게 읽을 수 없습니다."
            scope_result_snapshot = None
            scope_reason = BlockReasonCode.REPOSITORY_STATE_UNSAFE
        else:
            detail = None
            scope_result_snapshot = None
            scope_reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
        if detail is not None:
            message = block_persisted_agent_scope_violation(
                connection,
                repo_path,
                repo,
                row,
                detail,
                result_snapshot=scope_result_snapshot,
                reason=scope_reason,
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

    selected_base = requested_base or (row["base_branch"] if row else None)
    fetch_intent: int | None = None
    if (
        row is not None and validated_agent_scope is not None
        and row["phase"] == "PLAN_BLOCKED"
        and selected_base == row["base_branch"] and row["plan_path"]
    ):
        plan_id = _plan_id_from_path(str(row["plan_path"]))
        if plan_id:
            waiting_plan = connection.execute(
                "SELECT blocked_reason FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, plan_id),
            ).fetchone()
            if waiting_plan and waiting_plan["blocked_reason"] == GUIDANCE_BLOCK_REASON:
                fetch_intent = _record_manager_base_fetch_intent(
                    connection, repo_path, repo, plan_id,
                    str(selected_base), validated_agent_scope,
                )
    if fetch_intent is not None:
        base = _journaled_manager_base_fetch(
            connection, repo_path, state_dir, fetch_intent, str(selected_base),
            str(plan_id),
        )
    else:
        base = remote_base(repo_path, selected_base, state_dir=state_dir)
    backfill_plan_shas(connection, target_repo=repo)
    try:
        migrate_legacy_execplans(
            connection,
            reconcile_history=True,
            target_repo=repo,
            ensure_schema=not registered_only,
        )
    except execplan_catalog.ExecPlanCatalogError:
        # The typed reconciliation boundary below records invalid remote input
        # after PR synchronization without mutating declaration cache rows.
        pass
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row:
        hydrate_active_project(connection, repo)
        if (
            active_execplan(connection, repo) is None
            and row["phase"] in {"PR_OPEN", "PLAN_BLOCKED", "BLOCKED"}
            and not runtime_state.planning_owns_execution_slot(row)
            and scoped_blocking.project_block(connection, repo) is None
            and validated_agent_scope is None
        ):
            with connection:
                _clear_active_in_current_transaction(connection, repo)
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
    if row is not None and runtime_state.planning_owns_execution_slot(row):
        cleanup_pending = connection.execute(
            "SELECT * FROM cleanup_pending WHERE repo = ?", (repo,)
        ).fetchall()
    else:
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

    def archive_housekeeping() -> None:
        if not _execplan_archive_housekeeping_safe(
            connection,
            repo,
            validated_agent_scope,
            maintenance_assessment_enabled=registered_only,
            refactoring_review_interval=refactoring_review_interval,
        ):
            return
        try:
            dispatch_execplan_archive_housekeeping(
                connection,
                repo_path,
                state_dir,
                repo,
                base,
            )
        except Exception as error:
            # Archive maintenance is deliberately non-gating: MERGED state,
            # READY selection, planning, and completion continue unchanged.
            operation_print(
                f"ExecPlan archive housekeeping 실패 (다음 실행에서 재시도): {error}",
                file=sys.stderr,
            )

    base_sha = ""

    def synchronize_source_after_pr_reconciliation() -> None:
        nonlocal base_sha
        # Read the durable project state after all PR transitions and cleanup.
        current_project = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if current_project is not None and str(current_project["base_branch"]) != base:
            raise RuntimeError("PR 상태 동기화 중 프로젝트 base branch가 변경됐습니다.")
        archive_housekeeping()
        # Archive publication may fast-forward origin. Pin both source sync
        # and catalog reconciliation to the resulting remote revision.
        base_sha = git(
            repo_path,
            "rev-parse",
            f"refs/remotes/origin/{base}^{{commit}}",
        ).stdout.strip()
        if scoped_blocking.project_block(connection, repo) is None:
            waiting_open = connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'PR_OPEN' LIMIT 1",
                (repo,),
            ).fetchone() is not None
            confirmed_merge = connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'MERGED' LIMIT 1",
                (repo,),
            ).fetchone() is not None
            conflict_recovery = bool(
                current_project is not None
                and int(current_project["pr_conflict_recovery"])
            )
            if (waiting_open or conflict_recovery) and not confirmed_merge:
                sync_result = BaseWorktreeSyncResult(
                    "skipped",
                    "PR 충돌 감지/복구 중 source checkout 보존",
                    base_sha,
                )
            else:
                sync_result = ensure_safe_base_worktree(
                    repo_path, base, remote_sha=base_sha, state_dir=state_dir,
                )
            if isinstance(sync_result, BaseWorktreeSyncResult):
                log_base_worktree_sync(repo_path, sync_result)

    pr_sync = synchronize_waiting_pull_requests(
        connection,
        repo_path,
        state_dir,
        repo,
        after_reconciliation=synchronize_source_after_pr_reconciliation,
        require_base_identity=require_self_pr_base,
        after_observation=lambda observation: _handoff_observed_self_merge(
            connection, repo, state_dir, runtime_mutation, observation,
        ),
    )
    if pr_sync.step_result not in {None, 0}:
        return pr_sync.step_result
    if not base_sha:
        base_sha = git(
            repo_path,
            "rev-parse",
            f"refs/remotes/origin/{base}^{{commit}}",
        ).stdout.strip()
    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if row is not None and row["phase"] != "PLANNING":
        catalog_result = reconcile_execplan_catalog(
            connection, repo_path, repo, base, remote_sha=base_sha
        )
        if (
            catalog_result.kind
            is execplan_catalog_state.ReconciliationKind.STALE
        ):
            _record_project_recovery(
                connection, repo,
                recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE,
                "ExecPlan catalog snapshot이 적용 전에 변경되어 다시 조회합니다: "
                + str(catalog_result.revision_sha or ""),
            )
            return PROJECT_STEP_RETRY
        if (
            catalog_result.kind
            is execplan_catalog_state.ReconciliationKind.INVALID_CATALOG
        ):
            assert catalog_result.diagnostic is not None
            operation_print(
                catalog_result.diagnostic.message,
                file=sys.stderr,
            )
            return 1
        base_sha = str(catalog_result.revision_sha or base_sha)
        migrate_legacy_execplans(
            connection,
            reconcile_history=True,
            target_repo=repo,
            ensure_schema=False,
        )
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
    project_block = scoped_blocking.project_block(connection, repo)
    if project_block is not None:
        resume_project_block_if_safe(
            connection, repo_path, state_dir, repo
        )
        project_block = scoped_blocking.project_block(connection, repo)
    if project_block is not None:
        operation_print(
            "프로젝트 안전 차단 상태입니다: "
            + str(project_block["evidence"]),
            file=sys.stderr,
        )
        return 1
    if (
        row is not None
        and int(row["registered"]) == 1
        and row["phase"] is None
        and active_execplan(connection, repo) is None
    ):
        repository = maintenance_state.MaintenanceRepository(connection)
        state = repository.get(repo)
        if (
            state.phase is maintenance_state.MaintenancePhase.IDLE
            and state.file_assessment_start_merged_count is None
        ):
            current_sha = git(
                repo_path, "rev-parse", "--verify",
                f"refs/remotes/origin/{base}^{{commit}}",
            ).stdout.strip()
            if current_sha != base_sha:
                return PROJECT_STEP_RETRY
            repository.initialize_file_assessment_start(
                repo, source_sha=base_sha,
                expected_merged_count=repository.merged_count(repo),
            )
    if pr_sync.step_result not in {None, 0}:
        return pr_sync.step_result
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
                reason=getattr(
                    error,
                    "reason_code",
                    BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                ),
            )
            operation_print(message, file=sys.stderr)
            return 1
    resume_plan_blocks(
        connection, repo_path, state_dir, repo, base, config_path,
        verify_timeout_seconds,
    )
    if scoped_blocking.project_block(connection, repo) is not None:
        return 1
    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()

    if row and row["branch"] and not runtime_state.planning_owns_execution_slot(row):
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
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                operation_print(publication_error, file=sys.stderr)
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
            ensure_schema=not registered_only,
        )
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
    elif not row["phase"]:
        update_project(connection, repo, base_branch=base)
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()

    assert row is not None
    runtime_repository = runtime_state.RuntimeStateRepository(connection)
    maintenance_repository = maintenance_state.MaintenanceRepository(connection)
    planning_context = PlanningContext(
        repo=repo,
        repo_path=repo_path,
        state_dir=state_dir,
        base_branch=base,
        base_sha=base_sha,
        provider=provider,
        config_path=config_path,
        registered_only=registered_only,
        refactoring_review_interval=refactoring_review_interval,
        self_repository=registered_only and is_loop_manager_repository(repo_path),
    )
    _check_runtime_handoff()
    planning_outcome = PlanningLifecycleCoordinator(
        _PlanningLifecyclePorts(
            connection,
            runtime_repository,
            maintenance_repository,
            planning_context,
            planner_mutex,
        )
    ).run(planning_context)
    if planning_outcome.kind in {
        PlanningOutcomeKind.REMOTE_BASE_RESTART,
        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
        PlanningOutcomeKind.TRANSIENT_PAUSE,
    }:
        reason = (
            recovery_runtime.RecoveryReason.SERVICE_UNAVAILABLE
            if planning_outcome.kind is PlanningOutcomeKind.TRANSIENT_PAUSE
            else recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE
            if planning_outcome.kind is PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED
            else recovery_runtime.RecoveryReason.BASE_BEHIND
        )
        _record_project_recovery(
            connection, repo, reason,
            planning_outcome.detail or planning_outcome.kind.value,
            attempts=(
                3 if planning_outcome.kind in {
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                    PlanningOutcomeKind.TRANSIENT_PAUSE,
                } else 0
            ),
        )
    planning_step_result = _planning_outcome_step_result(planning_outcome)
    if planning_step_result is not None:
        return planning_step_result
    base_sha = planning_outcome.base_sha or base_sha
    newly_activated = planning_outcome.newly_activated
    execution_ports = _ExecutionLifecyclePorts(
        connection,
        runtime_repository,
        repo=repo,
        repo_path=repo_path,
        state_dir=state_dir,
        base=base,
        base_sha=base_sha,
        provider=provider,
        config_path=config_path,
        verify_timeout_seconds=verify_timeout_seconds,
        newly_activated=newly_activated,
    )
    execution = ExecutionPhaseCoordinator(execution_ports)
    _check_runtime_handoff()
    prepared = execution.prepare()
    if isinstance(prepared, ExecutionOutcome):
        return _execution_outcome_step_result(prepared)
    execution_context = prepared
    while True:
        _check_runtime_handoff()
        execution_outcome = execution.dispatch(execution_context)
        if (
            execution_outcome.kind is ExecutionOutcomeKind.PHASE_ADVANCED
            and execution_outcome.context is not None
        ):
            execution_context = execution_outcome.context
            continue
        return _execution_outcome_step_result(execution_outcome)

def _checkpoint_dispatch_ready(
    connection: sqlite3.Connection,
    repo: str,
) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'blocked_checkpoints')"
        )
    }
    if tables != {"projects", "blocked_checkpoints"}:
        return False
    return connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is not None


def _record_project_recovery(
    connection: sqlite3.Connection,
    repo: str,
    reason: recovery_runtime.RecoveryReason,
    detail: str,
    *,
    attempts: int = 0,
) -> recovery_runtime.RecoveryDecision:
    """Persist a project/PR operation failure without changing execution state."""

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'exec_plans')"
        )
    }
    project = (
        connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        if "projects" in tables else None
    )
    plan = (
        project_execplan(connection, repo)
        if project is not None and "exec_plans" in tables else None
    )
    phase = str(project["phase"] or "") if project is not None else ""
    active_sha = str(project["active_sha"] or "") if project is not None else ""
    branch = str(project["branch"] or "") if project is not None else ""
    marker = f"{active_sha}|{branch}|{project['worker_session_id'] if project is not None else ''}"
    fingerprint = hashlib.sha256(
        f"{reason.value}|{phase}|{active_sha}|{branch}".encode()
    ).hexdigest()
    observation = recovery_runtime.RecoveryObservation(
        reason=reason, repo=repo,
        plan_id=str(plan["plan_id"]) if plan is not None else None,
        phase=phase,
        plan_revision=(str(plan["current_revision_sha"] or plan["plan_sha"] or "")
                       if plan is not None else None),
        base_revision=active_sha, fingerprint=fingerprint,
        progress_marker=marker, detail=detail,
    )
    with runtime_state._transaction(connection):
        return recovery_state.RecoveryRepository(connection).observe(
            observation, attempts=attempts,
        ).decision


def _blocked_recovery_subjects(
    connection: sqlite3.Connection, repo: str,
) -> frozenset[str]:
    """Return subjects whose final BLOCKED event owns this failure instead."""

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('execplan_blocks', 'project_blocks')"
        )
    }
    subjects: set[str] = set()
    if "execplan_blocks" in tables:
        subjects.update(
            str(row[0]) for row in connection.execute(
                "SELECT plan_id FROM execplan_blocks WHERE repo = ?", (repo,)
            )
        )
    if "project_blocks" in tables:
        subjects.update(
            str(row[0]) if row[0] is not None else "__project__"
            for row in connection.execute(
                "SELECT plan_id FROM project_blocks WHERE repo = ?", (repo,)
            )
        )
    return frozenset(subjects)


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
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    on_step_completed: Callable[[], None] | None = None,
    runtime_mutation: RuntimeMutation | None = None,
    external_run_id: str | None = None,
) -> ProjectRunResult:
    """Run one scheduler invocation and project its external failure outcome."""

    repo = str(Path(repo_arg).expanduser().resolve())
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    recovery_repository = recovery_state.RecoveryRepository(connection)
    run_id = recovery_state.begin_external_run(connection, external_run_id)
    # The TEMP run marker must not hold the transaction that later makes each
    # recovery observation durable on its own bounded state transition.
    connection.commit()
    outcome: ProjectRunResult | None = None
    try:
        outcome = _run_project_result(
            connection, state_dir, repo_arg, config_path, requested_base,
            allow_agents_mismatch, requested_provider,
            registered_only=registered_only, planner_mutex=planner_mutex,
            refactoring_review_interval=refactoring_review_interval,
            verify_timeout_seconds=verify_timeout_seconds,
            on_step_completed=on_step_completed, runtime_mutation=runtime_mutation,
        )
        return outcome
    except SelfMergeRuntimeHandoff:
        outcome = ProjectRunResult(ProjectRunKind.RUNTIME_HANDOFF, 0)
        raise
    finally:
        # Failure observations are saved in their recovery transaction.  This
        # boundary only resolves an execution that reached a stable outcome.
        with connection:
            recovery_repository.finish_external_run(
                repo, run_id,
                resolved=(
                    outcome is not None
                    and outcome.kind not in {
                        ProjectRunKind.TRANSIENT_PAUSE,
                        ProjectRunKind.USAGE_LIMIT_PAUSE,
                        ProjectRunKind.OPERATIONAL_ERROR,
                        ProjectRunKind.BLOCKED,
                        ProjectRunKind.RUNTIME_HANDOFF,
                    }
                ),
                blocked_subjects=(
                    frozenset() if outcome is not None
                    and outcome.kind is ProjectRunKind.RUNTIME_HANDOFF
                    else _blocked_recovery_subjects(connection, repo)
                ),
            )
            if outcome is None or outcome.kind is not ProjectRunKind.RUNTIME_HANDOFF:
                notifications.enqueue_persistent_execution_failure_events(connection, repo)


def _stable_work_reason(
    connection: sqlite3.Connection, repo: str, *,
    refactoring_review_interval: int,
) -> work_exhaustion.WorkReasonSnapshot | None:
    """Confirm that the latest remote selection has no runnable work."""

    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if row is None or row["phase"] not in {None, "PR_OPEN", "PLAN_BLOCKED"}:
        return None
    if row["pr_conflict_recovery"] or row["planning_error"]:
        return None
    if not row["planning_fingerprint"]:
        return None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'general_planning_publications'"
    ).fetchone() is not None and connection.execute(
        "SELECT 1 FROM general_planning_publications WHERE repo = ?", (repo,),
    ).fetchone() is not None:
        return None
    schedule = catalog_schedule_decision(connection, repo)
    if schedule.active is not None or schedule.runnable_ready is not None:
        return None
    maintenance = maintenance_state.MaintenanceRepository(connection)
    cycle = maintenance.get(repo)
    if (
        cycle.phase is not maintenance_state.MaintenancePhase.IDLE
        or cycle.publication_stage is not None
        or maintenance.file_assessment_due(
            repo, refactoring_review_interval,
            self_repository=is_loop_manager_repository(Path(repo)),
        ).required
    ):
        return None
    repo_path = Path(repo)
    revision = f"refs/remotes/origin/{row['base_branch']}"
    base_sha = git(
        repo_path, "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).stdout.strip()
    current_input = _planning_input_fingerprint(repo_path, base_sha)
    current_specification = _specification_input_fingerprint(repo_path, base_sha)
    archive_only = False
    if current_input != row["planning_fingerprint"] and row["processed_sha"]:
        archive_only = _is_requirement_archive_only_change(
            repo_path, str(row["processed_sha"]), base_sha,
        )
    if _general_planning_required(row, repo_path, base_sha, current_input):
        return None
    if row["specification_fingerprint"] is not None:
        if (
            current_specification != row["specification_fingerprint"]
            and not archive_only
            and not (
                row["processed_sha"]
                and _specification_input_fingerprint(
                    repo_path, str(row["processed_sha"])
                ) == current_specification
            )
        ):
            return None
    active_requirements = tuple(
        path.decode("utf-8") for path in _git_tree_entries(repo_path, base_sha)
        if _is_active_requirement(path)
    )
    snapshot = work_exhaustion.collect(
        connection, repo, cycle=str(row["planning_fingerprint"]),
        active_requirements=active_requirements,
    )
    return None if snapshot is not None and snapshot.completed and row["phase"] else snapshot


def _run_project_result(
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
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    on_step_completed: Callable[[], None] | None = None,
    runtime_mutation: RuntimeMutation | None = None,
) -> ProjectRunResult:
    """Run retryable validation progress to a stable project-level outcome."""

    repo = str(Path(repo_arg).expanduser().resolve())
    checkpoint_attempts: set[str] = set()
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    recovery_attempts: dict[str, int] = {}
    pause_detail: str | None = None
    while True:
        if _checkpoint_dispatch_ready(connection, repo):
            project_before = connection.execute(
                "SELECT phase, pr_conflict_recovery FROM projects WHERE repo = ?",
                (repo,),
            ).fetchone()
            schedule_before = catalog_schedule_decision(connection, repo)
            maintenance_before = maintenance_state.MaintenanceRepository(
                connection
            ).get(repo)
            if (
                schedule_before.active is not None
                or schedule_before.runnable_ready is not None
                or project_before["phase"] not in {
                    None, "PR_OPEN", "PLAN_BLOCKED", "PROJECT_BLOCKED",
                }
                or project_before["pr_conflict_recovery"]
                or maintenance_before.phase is not maintenance_state.MaintenancePhase.IDLE
            ):
                notifications.mark_work_available(connection, repo)
            pending_before = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    connection
                ).pending(repo)
            )
            if any(
                request.request_identity not in checkpoint_attempts
                for request in pending_before
            ):
                dispatch_blocked_checkpoints(
                    connection,
                    Path(repo),
                    state_dir,
                    repo,
                    skip_identities=frozenset(checkpoint_attempts),
                )
                checkpoint_attempts.update(
                    request.request_identity for request in pending_before
                )
        try:
            recovery_before = {
                str(record["subject"]): int(record["generation"])
                for record in connection.execute(
                    "SELECT subject, generation FROM recovery_records WHERE repo = ?",
                    (repo,),
                )
            }
            step_arguments = (
                connection,
                state_dir,
                repo_arg,
                config_path,
                requested_base,
                allow_agents_mismatch,
                requested_provider,
                registered_only,
                planner_mutex,
                refactoring_review_interval,
                verify_timeout_seconds,
            )
            step_result = _run_project_step(
                *step_arguments,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
        except GitLockError as error:
            _record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.GIT_LOCK,
                str(error),
                attempts=3,
            )
            operation_log(
                f"일시적 Git lock 충돌 ({repo_arg}): {error}",
                error=True,
            )
            return ProjectRunResult(
                ProjectRunKind.TRANSIENT_PAUSE,
                0,
                str(error),
            )
        if on_step_completed is not None:
            on_step_completed()
        if step_result == PROJECT_STEP_RETRY:
            for record in connection.execute(
                "SELECT subject, generation, identity, disposition, detail "
                "FROM recovery_records WHERE repo = ?", (repo,),
            ):
                if int(record["generation"]) <= recovery_before.get(str(record["subject"]), 0):
                    continue
                if record["disposition"] not in {"transient_retry", "state_reconcile"}:
                    continue
                identity = str(record["identity"])
                run_key = f"{record['subject']}|{record['disposition']}"
                recovery_attempts[run_key] = recovery_attempts.get(run_key, 0) + 1
                if recovery_attempts[run_key] >= 3:
                    with connection:
                        recovery_state.RecoveryRepository(connection).mark_paused(
                            repo, str(record["subject"]), identity,
                            int(record["generation"]), recovery_attempts[run_key],
                        )
                    step_result = PROJECT_STEP_TRANSIENT_PAUSE
                    pause_detail = str(record["detail"])
                    break
        if _checkpoint_dispatch_ready(connection, repo):
            pending_after = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    connection
                ).pending(repo)
            )
            if any(
                request.request_identity not in checkpoint_attempts
                for request in pending_after
            ):
                dispatch_blocked_checkpoints(
                    connection,
                    Path(repo),
                    state_dir,
                    repo,
                    skip_identities=frozenset(checkpoint_attempts),
                )
                checkpoint_attempts.update(
                    request.request_identity for request in pending_after
                )
            schedule = catalog_schedule_decision(connection, repo)
            if (
                step_result not in {
                    PROJECT_STEP_TRANSIENT_PAUSE,
                    PROJECT_STEP_USAGE_PAUSE,
                    PROJECT_STEP_PLANNING_PENDING,
                }
                and schedule.active is None
                and schedule.runnable_ready is not None
                and not schedule.project_blocked
            ):
                project_after = connection.execute(
                    "SELECT phase, planning_error FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                if project_after is not None and (
                    project_after["phase"] != "PLANNING"
                    and not project_after["planning_error"]
                ):
                    step_result = PROJECT_STEP_RETRY
        if step_result != PROJECT_STEP_RETRY:
            break

    row = connection.execute(
        "SELECT phase, last_error, planning_error FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    detail = str(row["last_error"] or "") if row else None
    if step_result == PROJECT_STEP_TRANSIENT_PAUSE:
        return ProjectRunResult(ProjectRunKind.TRANSIENT_PAUSE, 0, pause_detail or detail)
    if step_result == PROJECT_STEP_USAGE_PAUSE:
        return ProjectRunResult(ProjectRunKind.USAGE_LIMIT_PAUSE, 0, detail)
    if step_result == PROJECT_STEP_PLANNING_PENDING:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    if row is not None and row["phase"] == "PROJECT_BLOCKED":
        project_block = scoped_blocking.project_block(connection, repo)
        reason = (
            f"PROJECT_BLOCKED {project_block['reason_code']}: {project_block['evidence']}"
            if project_block is not None else detail
        )
        operation_log(str(reason), error=True)
        return ProjectRunResult(ProjectRunKind.BLOCKED, 1, reason)
    if row is not None and row["planning_error"]:
        reason = f"planning_error: {row['planning_error']}"
        operation_log(reason, error=True)
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, reason)
    snapshot = None
    if step_result == 0 or (step_result == 1 and connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'PLAN_BLOCKED' "
        "LIMIT 1", (repo,),
    ).fetchone() is not None):
        snapshot = _stable_work_reason(
            connection, repo, refactoring_review_interval=refactoring_review_interval,
        )
    if snapshot is not None:
        reason = snapshot.describe()
        operation_log(f"작업 선택 결과 ({repo}): {reason}")
        if snapshot.completed and step_result == 0:
            notifications.enqueue_completion_if_ready(connection, repo, snapshot)
            return ProjectRunResult(ProjectRunKind.COMPLETED, 0, reason)
        if not snapshot.completed:
            notifications.enqueue_waiting_reason(connection, repo, snapshot)
            return ProjectRunResult(ProjectRunKind.WAITING, 0, reason)
    plan_block = connection.execute(
        "SELECT evidence FROM execplan_blocks WHERE repo = ? "
        "ORDER BY updated_at DESC, plan_id LIMIT 1",
        (repo,),
    ).fetchone()
    if step_result != 0 and plan_block is not None:
        return ProjectRunResult(
            ProjectRunKind.WAITING,
            0,
            str(plan_block["evidence"]),
        )
    if step_result != 0:
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, detail)
    if row is not None and row["phase"] is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    maintenance_repository = maintenance_state.MaintenanceRepository(connection)
    maintenance_cycle = maintenance_repository.get(repo)
    if (
        maintenance_cycle.phase is not maintenance_state.MaintenancePhase.IDLE
        or maintenance_repository.file_assessment_due(
            repo, refactoring_review_interval,
            self_repository=is_loop_manager_repository(Path(repo)),
        ).required
    ):
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    unfinished = connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id) LIMIT 1",
        (repo,),
    ).fetchone()
    if unfinished is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    return ProjectRunResult(ProjectRunKind.WAITING, 0, detail or "Planner 확인 대기")


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
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
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
        refactoring_review_interval=refactoring_review_interval,
        verify_timeout_seconds=verify_timeout_seconds,
    ).exit_code


def _run_registered_project(
    project: project_registry.RegisteredProject,
    state_dir: Path,
    config_path: Path,
    telegram_token: str,
    telegram_chat_id: str,
    planner_mutex: threading.Lock,
    refactoring_review_interval: int,
    verify_timeout_seconds: int = 600,
    runtime_mutation: RuntimeMutation | None = None,
    external_run_id: str | None = None,
    handoff_gate: RuntimeHandoffGate | None = None,
) -> ScheduledProjectResult:
    """Run one registry row with state isolated to this worker connection."""

    handoff_token = _runtime_handoff_signal.set(handoff_gate)
    try:
        _check_runtime_handoff()
        # The scheduler connection completed migrations before starting any
        # project threads. Repeating DDL here races with another project's
        # state transaction and can fail with SQLITE_BUSY.
        with connect_database(state_dir, run_migrations=False) as connection:
            notifications.configure_session(
                connection, enabled=bool(telegram_token and telegram_chat_id)
            )
            # TEMP session setup opens a SQLite transaction. Close it before
            # runtime-state repository operations so each lifecycle change
            # owns and durably commits its intended transaction boundary.
            connection.commit()

            def deliver_new_pr_notifications() -> None:
                try:
                    notifications.dispatch_new_pr_notifications(
                        connection,
                        telegram_token,
                        telegram_chat_id,
                        project.repo,
                    )
                except Exception as error:
                    operation_log(
                        f"즉시 PR 알림 전송 실패 ({type(error).__name__})",
                        error=True,
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
                refactoring_review_interval=refactoring_review_interval,
                verify_timeout_seconds=verify_timeout_seconds,
                on_step_completed=deliver_new_pr_notifications,
                external_run_id=external_run_id,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
    except SelfMergeRuntimeHandoff:
        if handoff_gate is not None:
            handoff_gate.stop()
        result = ProjectRunResult(ProjectRunKind.RUNTIME_HANDOFF, 0)
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
    finally:
        _runtime_handoff_signal.reset(handoff_token)
    return ScheduledProjectResult(project.repo, result)


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


_runtime_handoff_signal: ContextVar[RuntimeHandoffGate | None] = ContextVar(
    "runtime_handoff_signal", default=None,
)


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


def _registered_self_runtime_project(
    project: project_registry.RegisteredProject,
    runtime: RuntimeMutation,
    state_dir: Path,
) -> bool:
    """Match the parent's source checkout, not an alias or another clone."""

    source = _canonical_path(runtime.source)
    if _canonical_path(runtime.path) != _canonical_path(ROOT):
        return False
    if project.repo != str(source) or Path(project.repo).is_symlink():
        return False
    repository = Path(project.repo)
    top_level = _canonical_path(Path(git(
        repository, "rev-parse", "--show-toplevel"
    ).stdout.strip()))
    if top_level != source or _git_common_directory(repository) != _git_common_directory(source):
        raise RuntimeError("등록된 self source repository identity가 다릅니다.")
    if _git_common_directory(runtime.path) != _git_common_directory(source):
        raise RuntimeError("runtime과 self source repository identity가 다릅니다.")
    source_origin = repository_origin(source)
    if repository_origin(repository) != source_origin or repository_origin(runtime.path) != source_origin:
        raise RuntimeError("runtime과 등록된 self source origin이 다릅니다.")
    if project.origin is not None and project.origin != source_origin:
        raise RuntimeError("등록된 self origin이 현재 origin과 다릅니다.")
    if not project.base_branch:
        raise RuntimeError("등록된 self base branch가 비어 있습니다.")
    if bootstrap_base_branch(source, state_dir, source_origin) != project.base_branch:
        raise RuntimeError("runtime과 등록된 self base branch가 다릅니다.")
    if git(runtime.path, "rev-parse", "HEAD").stdout.strip() != runtime.commit_sha:
        raise RuntimeError("현재 runtime SHA가 선택된 SHA와 다릅니다.")
    return True


def _self_runtime_project(
    connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    runtime: RuntimeMutation | None,
) -> project_registry.RegisteredProject | None:
    if runtime is None:
        return None
    for project in project_registry.list_projects(connection):
        if project.repo == repo:
            return project if _registered_self_runtime_project(
                project, runtime, state_dir,
            ) else None
    return None


def _finish_self_merge_handoff(
    connection: sqlite3.Connection,
    project: project_registry.RegisteredProject,
    state_dir: Path,
    runtime: RuntimeMutation,
    observation: WaitingPullRequestObservation,
) -> None:
    """Use the durable merge observation to decide a normal runtime exit."""

    if not observation.merged:
        return
    if not _registered_self_runtime_project(project, runtime, state_dir):
        raise RuntimeError("self Merge 뒤 source identity가 변경됐습니다.")
    repo_path = Path(project.repo)
    base = project.base_branch
    remote_sha = git(
        repo_path, "rev-parse", "--verify",
        f"refs/remotes/origin/{base}^{{commit}}",
    ).stdout.strip()
    if scoped_blocking.project_block(connection, project.repo) is None:
        sync = ensure_safe_base_worktree(
            repo_path, base, remote_sha=remote_sha, state_dir=state_dir,
        )
    else:
        sync = BaseWorktreeSyncResult(
            "skipped", "프로젝트 안전 차단 중 source checkout 보존", remote_sha,
        )
    log_base_worktree_sync(repo_path, sync)
    if git(
        repo_path, "rev-parse", "--verify",
        f"refs/remotes/origin/{base}^{{commit}}",
    ).stdout.strip() != remote_sha:
        raise RuntimeError("self Merge 뒤 origin base ref가 변경됐습니다.")
    if not _registered_self_runtime_project(project, runtime, state_dir):
        raise RuntimeError("self Merge 뒤 source identity가 변경됐습니다.")
    if remote_sha == runtime.commit_sha:
        return
    ancestor = git(
        repo_path, "merge-base", "--is-ancestor",
        runtime.commit_sha, remote_sha, check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError(
            "self Merge 뒤 runtime과 원격 base의 fast-forward 관계를 "
            "확인할 수 없습니다."
        )
    gate = _runtime_handoff_signal.get()
    if gate is not None:
        gate.stop()
    operation_log(
        f"self Merge runtime handoff: repository={project.repo} "
        f"base={base} runtime={runtime.commit_sha} remote={remote_sha} "
        f"prs={','.join(observation.merged_pr_urls)} "
        f"source_sync={sync.status} ({sync.reason})"
    )
    raise SelfMergeRuntimeHandoff()


def _preflight_self_merge_handoff(
    connection: sqlite3.Connection,
    projects: tuple[project_registry.RegisteredProject, ...],
    state_dir: Path,
    runtime: RuntimeMutation | None,
) -> None:
    """Reconcile self PRs before any project thread can start new work."""

    if runtime is None:
        return
    for project in projects:
        if not _registered_self_runtime_project(project, runtime, state_dir):
            continue
        observation = observe_waiting_pull_requests(
            connection, Path(project.repo), state_dir, project.repo,
            require_base_identity=True,
        )
        _finish_self_merge_handoff(
            connection, project, state_dir, runtime, observation,
        )
        return


def _handoff_observed_self_merge(
    connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    runtime: RuntimeMutation | None,
    observation: WaitingPullRequestObservation,
) -> None:
    """Guard a merge newly observed after the scheduler preflight."""

    if runtime is None or not observation.merged:
        return
    project = _self_runtime_project(connection, repo, state_dir, runtime)
    if project is not None:
        _finish_self_merge_handoff(
            connection, project, state_dir, runtime, observation,
        )


def schedule_registered_projects(
    connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
    runtime_mutation: RuntimeMutation | None = None,
) -> tuple[ScheduledProjectResult, ...]:
    """Run each registered project once with bounded cross-project concurrency."""

    projects = project_registry.list_projects(connection)
    if not projects:
        return ()

    if runtime_mutation is not None:
        notifications.configure_session(
            connection, enabled=bool(config.telegram_token and config.telegram_chat_id),
        )
        connection.commit()
    _preflight_self_merge_handoff(connection, projects, state_dir, runtime_mutation)

    planner_mutex = threading.Lock()
    handoff_gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
    external_run_id = secrets.token_hex(16)
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
                config.telegram_token,
                config.telegram_chat_id,
                planner_mutex,
                config.refactoring_review_interval,
                config.verify_timeout_seconds,
                external_run_id=external_run_id,
                handoff_gate=handoff_gate,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
            for project in projects
        )
        return tuple(future.result() for future in futures)


def run_registered_projects(
    connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
    runtime_mutation: RuntimeMutation | None = None,
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
    try:
        results = schedule_registered_projects(
            connection, state_dir, config,
            **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
        )
    except SelfMergeRuntimeHandoff:
        return 0
    if any(item.result.kind is ProjectRunKind.RUNTIME_HANDOFF for item in results):
        return 0
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
                state_dir=state_dir,
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



def _document_drafts_command(
    args: argparse.Namespace, state_dir: Path, config_path: Path,
) -> int:
    import document_drafts

    repository = Path(args.repository).expanduser().resolve()
    if not is_loop_manager_repository(repository):
        raise document_drafts.DocumentDraftError(
            "일회성 문서 수정본은 Loop Manager 저장소 자체에서만 사용할 수 있습니다."
        )
    repo = str(repository)
    with connect_database(state_dir) as connection:
        project = connection.execute(
            "SELECT base_branch, provider FROM projects WHERE repo = ? AND registered = 1",
            (repo,),
        ).fetchone()
        if project is None:
            raise document_drafts.DocumentDraftError("등록된 Loop Manager 저장소가 아닙니다.")
        base_branch = str(project["base_branch"])
        drafts = document_drafts.DocumentDraftRepository(connection)

        def display_review(draft: document_drafts.DocumentDraft) -> None:
            sys.stdout.flush()
            output = sys.stdout.buffer
            output.write(document_drafts.review_artifact_bytes(draft))
            output.flush()

        if args.draft_command == "list":
            for path, findings in drafts.candidates(repo).items():
                operation_print(f"candidate\t{path}\t{','.join(findings)}")
            for draft in drafts.list(repo):
                operation_print(
                    f"{draft.state}\t{draft.draft_id}\t{draft.proposed_hash}\t"
                    f"{draft.target_path}"
                )
            return 0
        if args.draft_command == "show":
            draft = drafts.get(args.draft_id)
            if draft.repo != repo:
                raise document_drafts.DocumentDraftError("다른 저장소의 수정본입니다.")
            display_review(draft)
            return 0
        _recorded_base_fetch(
            repository, state_dir, base_branch, owner="project",
        )
        if args.draft_command == "generate":
            prompt, identity = drafts.generation_prompt(
                repo, repository, base_branch, args.target,
            )
            result = _coerce_agent_result(
                run_agent(
                    str(project["provider"]), ROOT, state_dir, config_path,
                    "planner", prompt, read_only=True,
                ),
                None,
            )
            if result.code:
                raise document_drafts.DocumentDraftError(
                    "문서 수정본 Agent가 성공하지 못했습니다. 원본과 결정은 그대로입니다."
                )
            generated = document_drafts.parse_generated_draft(result.message)
            if generated is None:
                operation_print("의미 보존을 확정하지 못해 수정본을 만들지 않았습니다.")
                return 0
            draft = drafts.create(
                repo, repository, base_branch, args.target, *generated,
                expected_identity=identity,
            )
            connection.commit()
            display_review(draft)
        elif args.draft_command == "create":
            content_file = Path(args.content_file).expanduser().resolve()
            if content_file.is_relative_to(repository):
                raise document_drafts.DocumentDraftError(
                    "수정본 입력 파일은 source checkout 밖에 두어야 합니다."
                )
            draft = drafts.create(
                repo, repository, base_branch, args.target,
                content_file.read_bytes(), args.meaning_evidence,
            )
            connection.commit()
            display_review(draft)
        elif args.draft_command == "decide":
            draft = drafts.get(args.draft_id)
            if draft.repo != repo:
                raise document_drafts.DocumentDraftError("다른 저장소의 수정본입니다.")
            draft = drafts.decide(
                args.draft_id, args.proposed_hash, args.decision,
                repository, base_branch,
                uncertain_reason=args.uncertain_reason,
            )
            connection.commit()
            operation_print(
                f"{draft.state}\t{draft.draft_id}\t{draft.proposed_hash}\t"
                f"{draft.decision_revision}"
            )
            if draft.decision_reason:
                operation_print(draft.decision_reason)
        else:
            for draft in drafts.approved(repo, repository, base_branch):
                operation_print(
                    f"approved\t{draft.draft_id}\t{draft.proposed_hash}\t"
                    f"{draft.target_path}"
                )
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
    parser.add_argument("--runtime-mutation-id", help=argparse.SUPPRESS)
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

    drafts = subcommands.add_parser(
        "document-drafts", help="Loop Manager 전수 평가 문서 수정본 검토와 결정"
    )
    drafts.add_argument("repository", help="등록된 Loop Manager 저장소 루트")
    draft_commands = drafts.add_subparsers(dest="draft_command", required=True)
    draft_commands.add_parser("list", help="문서 후보와 저장된 수정본 조회")
    generate_draft = draft_commands.add_parser("generate", help="평가 발견에서 읽기 전용 수정본 생성")
    generate_draft.add_argument("target", help="평가 결과의 활성 문서 경로")
    create_draft = draft_commands.add_parser("create", help="외부 파일의 수정본 저장")
    create_draft.add_argument("target", help="평가 결과의 활성 문서 경로")
    create_draft.add_argument("content_file", help="원본 checkout 밖의 UTF-8 수정본 파일")
    create_draft.add_argument("--meaning-evidence", required=True,
                              help="기존 의미가 보존된 구체적인 근거")
    show_draft = draft_commands.add_parser("show", help="전체 원본·수정본·diff 조회")
    show_draft.add_argument("draft_id")
    decide_draft = draft_commands.add_parser("decide", help="정확한 수정본 승인 또는 거절")
    decide_draft.add_argument("draft_id")
    decide_draft.add_argument("proposed_hash", help="show의 Proposed SHA256")
    decide_draft.add_argument("decision", choices=("approve", "reject"))
    decide_draft.add_argument("--uncertain-reason", default="",
                              help="의미 보존이 불확실하면 승인 대신 새 검토 요구")
    draft_commands.add_parser("approved", help="현재 원본에 유효한 승인 조회")

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
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    config = load_managed_config(config_path)
    if args.command == "init":
        with connect_database(state_dir, create=True):
            pass
        operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0
    if args.command == "config":
        return configure_registry(args, state_dir, config_path)
    if args.command == "document-drafts":
        return _document_drafts_command(args, state_dir, config_path)
    with connect_database(state_dir) as connection:
        agent_invocation_state.InvocationRepository(connection).reconcile_interrupted()
        return run_registered_projects(
            connection, state_dir, config,
            **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
        )


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
            runtime_mutation = validate_runtime_mutation(
                source, state_dir, str(args.runtime_sha), args.runtime_mutation_id,
            )
            if runtime_mutation is not None and not runtime_mutation.legacy:
                base = bootstrap_base_branch(source, state_dir, repository_origin(source))
                validate_bootstrap_revision(source, base, str(args.runtime_sha))
            return execute_runtime_command(
                args, state_dir, config_path, runtime_mutation,
            )

        source = ROOT
        mutation_id = secrets.token_hex(16)
        with process_lock(state_dir, identity=f"bootstrap:{mutation_id}") as lock_handle:
            runtime = prepare_bootstrap_runtime(source, state_dir, mutation_id)
            if isinstance(runtime, BootstrapRefreshHandoff):
                if args.command != "run":
                    command_name = (
                        f"config {args.config_command}"
                        if args.command == "config" else args.command
                    )
                    raise RuntimeError(
                        f"bootstrap source를 origin/{runtime.base_branch} "
                        f"{runtime.commit_sha}로 갱신해 {command_name} 명령은 "
                        "실행하지 않았습니다. 같은 명령을 다시 실행하세요."
                    )
                return 0
            _write_process_lock_identity(lock_handle, f"runtime:{mutation_id}")
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


def _owned_checkpoint_remote_oid(
    connection: sqlite3.Connection,
    repo_path: Path,
    request: blocked_checkpoint.CheckpointRequest,
    plan: sqlite3.Row,
    current: GitSnapshot,
) -> str | None:
    """Prove a changed tracking ref against durable publication and live origin."""
    if not request.branch or current.branch != request.branch:
        return None
    remote_ref = f"refs/remotes/origin/{request.branch}"
    local_ref = f"refs/heads/{request.branch}"
    tracked = current.refs.get(remote_ref)
    if tracked is None or tracked[1] or current.refs.get(local_ref) != (current.head, ""):
        return None
    candidate = tracked[0]
    published = str(plan["publication_remote_oid"] or "")
    durable = candidate == published and bool(published)
    if not durable and request.local_oid == candidate and request.tree_oid and request.commit_identity:
        try:
            durable = (
                current.head == candidate
                and git(repo_path, "rev-parse", f"{candidate}^{{tree}}").stdout.strip()
                == request.tree_oid
                and f"Checkpoint-Identity: {request.commit_identity}" in
                git(repo_path, "show", "-s", "--format=%B", candidate).stdout
            )
        except RuntimeError:
            durable = False
    if not durable:
        return None
    try:
        observed = git(
            repo_path, "ls-remote", "--heads", "origin",
            f"refs/heads/{request.branch}", check=False,
        )
    except RuntimeError:
        return None
    rows = [line.split() for line in observed.stdout.splitlines() if line.strip()]
    if observed.returncode or rows != [[candidate, f"refs/heads/{request.branch}"]]:
        return None
    return candidate


def _guidance_checkpoint_commit_evidence(
    repo_path: Path, plan_id: str, oid: str,
) -> str | None:
    """Return the original guidance evidence only for an intact Manager commit."""
    try:
        message = git(repo_path, "show", "-s", "--format=%B", oid).stdout
        parent_line = git(repo_path, "show", "-s", "--format=%P", oid).stdout.strip()
        parents = parent_line.split()
        tree = git(repo_path, "rev-parse", f"{oid}^{{tree}}").stdout.strip()
        changed = tuple(sorted(_nul_git_paths(
            repo_path, "diff", "--name-only", "-z", parents[0], oid,
        ))) if len(parents) == 1 else ()
        author = git(repo_path, "show", "-s", "--format=%an <%ae>%n%cn <%ce>", oid).stdout.splitlines()
        fields = dict(line.split(": ", 1) for line in message.splitlines() if ": " in line)
        evidence = json.loads(fields["Evidence"])
        paths = tuple(sorted(
            line[2:] for line in message.split("Paths:\n", 1)[1].splitlines()
            if line.startswith("- ") and line != "- (none)"
        ))
        if (
            len(parents) != 1
            or author != ["Loop Manager <loop-manager@localhost>"] * 2
            or not isinstance(evidence, str)
            or fields.get("Reason") != GUIDANCE_BLOCK_REASON
            or fields.get("Scope") != "plan"
            or fields.get("Evidence-SHA256") != hashlib.sha256(evidence.encode()).hexdigest()
            or paths != changed
            or message.splitlines()[0] != f"[{plan_id}] BLOCKED checkpoint"
            or fields.get("Checkpoint-Identity") != blocked_checkpoint.checkpoint_identity(
                plan_id=plan_id, reason_code=GUIDANCE_BLOCK_REASON,
                evidence=evidence, parent_oid=parents[0], tree_oid=tree,
                included_paths=paths,
            )
        ):
            return None
        return evidence
    except (KeyError, ValueError, IndexError, RuntimeError, json.JSONDecodeError):
        return None


class _BlockedCheckpointAdapter:
    """Git/GitHub safety and publication adapter for one registered project."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        repo_path: Path,
        state_dir: Path,
        repo: str,
    ):
        self.connection = connection
        self.repo_path = repo_path
        self.state_dir = state_dir
        self.repo = repo

    def _plan(self, request: blocked_checkpoint.CheckpointRequest) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT p.*, b.reason_code AS checkpoint_reason, "
            "b.evidence AS checkpoint_evidence "
            "FROM exec_plans AS p JOIN execplan_blocks AS b "
            "USING (repo, plan_id) WHERE p.repo = ? AND p.plan_id = ?",
            (request.repo, request.plan_id),
        ).fetchone()
        if (
            row is None
            or row["status"] != "PLAN_BLOCKED"
            or row["checkpoint_reason"] != request.reason_code
            or row["checkpoint_evidence"] != request.evidence
        ):
            raise RuntimeError("checkpoint 대상 PLAN_BLOCKED identity가 변경됐습니다.")
        return row

    def _approved_guidance_targets(self, request: blocked_checkpoint.CheckpointRequest) -> tuple[str, ...]:
        targets = []
        for row in self.connection.execute(
            "SELECT target_path, expected_content_hash FROM guidance_approvals "
            "WHERE repo = ? AND plan_id = ? AND applied = 1 "
            "AND expected_content_hash IS NOT NULL ORDER BY target_path",
            (request.repo, request.plan_id),
        ):
            target = str(row["target_path"])
            path = Path(str(request.worktree or "")) / target
            try:
                matches = (
                    path.is_file()
                    and not path.is_symlink()
                    and guidance_content_hash(path.read_bytes())
                    == str(row["expected_content_hash"])
                )
            except OSError:
                matches = False
            if matches:
                targets.append(target)
        return tuple(targets)

    def assess(
        self, request: blocked_checkpoint.CheckpointRequest
    ) -> blocked_checkpoint.SafetyVerdict:
        try:
            plan = self._plan(request)
        except RuntimeError as error:
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "checkpoint_identity_changed",
                str(error),
                None,
                (),
            )
        if plan["pr_url"]:
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "existing_pull_request",
                "기존 PR에 연결된 계획은 checkpoint 대상이 아닙니다.",
                None,
                (),
            )
        if not request.branch or not request.worktree:
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "managed_workspace_not_created",
                "관리 branch 또는 worktree 생성 전에 차단되어 local-only로 보존합니다.",
                None,
                (),
            )
        project_blocked = scoped_blocking.project_block(
            self.connection, request.repo
        ) is not None
        branch = str(plan["branch"] or "")
        worktree = Path(str(plan["worktree"] or ""))
        identity_matches = bool(
            request.branch
            and request.worktree
            and request.branch == branch
            and Path(request.worktree) == worktree
        )
        snapshot = None
        try:
            validate_execplan_workspace_identity(
                self.state_dir,
                request.repo,
                request.plan_id,
                branch,
                worktree,
            )
            validate_managed_worktree(
                self.repo_path,
                worktree,
                branch,
                allow_rebase=_rebase_in_progress_for_branch(worktree, branch),
            )
            snapshot = capture_git_snapshot(worktree)
        except (OSError, RuntimeError, WorkspaceOwnershipError):
            identity_matches = False

        if (
            snapshot is not None
            and identity_matches
            and not project_blocked
            and snapshot.operations
        ):
            operations = tuple(sorted(snapshot.operations))
            return blocked_checkpoint.SafetyVerdict(
                False,
                blocked_checkpoint.SafetyScope.PLAN,
                "git_operation_in_progress",
                "미해결 Git operation: " + ", ".join(operations),
                snapshot.head,
                (),
            )

        dirty_paths: list[blocked_checkpoint.DirtyPath] = []
        shared_metadata_owned = False
        head_oid = snapshot.head if snapshot is not None else None
        actual_branch = snapshot.branch if snapshot is not None else None
        operations = tuple(sorted(snapshot.operations)) if snapshot is not None else ()
        if snapshot is not None:
            metadata_error = self._shared_safety_error(request, plan, snapshot)
            if metadata_error is not None:
                return metadata_error
            active_key = str(_canonical_path(worktree))
            worktree_entry = snapshot.worktrees.get(active_key, ())
            branch_ref = f"refs/heads/{branch}"
            shared_metadata_owned = bool(
                snapshot.shared_metadata_complete
                and f"branch={branch_ref}" in worktree_entry
                and snapshot.refs.get(branch_ref, (None, None))[0] == snapshot.head
            )
            try:
                sources = tuple(str(item) for item in json.loads(str(plan["sources"])))
            except (TypeError, ValueError):
                sources = ()
            archive_sources = tuple(
                source
                for source in sources
                if source.startswith("docs/requirements/")
                and "/archive/" not in source
            )
            guidance_targets = self._approved_guidance_targets(request)
            allow_common_plans = _published_plan_allows_common_plans(
                self.repo_path, plan
            )
            for path, (kinds, _) in sorted(snapshot.dirty.items()):
                target = worktree / path
                try:
                    tree_entry = git(worktree, "ls-tree", "HEAD", "--", path).stdout
                    symlink = (
                        target.is_symlink()
                        or tree_entry.startswith("120000 ")
                        or any(
                            entry.split()[1:2] == ["120000"]
                            for entry in snapshot.index.get(path, ())
                        )
                    )
                    external_worktree = bool(
                        target.exists()
                        and not target.is_file()
                        and not symlink
                    )
                except OSError:
                    symlink = True
                    external_worktree = True
                ignored_result = git(
                    worktree,
                    "check-ignore",
                    "--no-index",
                    "--quiet",
                    "--",
                    path,
                    check=False,
                )
                ignored = ignored_result.returncode == 0
                if ignored_result.returncode not in {0, 1}:
                    ignored = True
                entries = snapshot.index.get(path, ())
                dirty_paths.append(
                    blocked_checkpoint.DirtyPath(
                        path=path,
                        kinds=kinds,
                        allowed=_worker_path_allowed(
                            path,
                            str(plan["path"]),
                            archive_sources,
                            guidance_targets,
                            allow_common_plans,
                        ),
                        symlink=symlink,
                        ignored=ignored,
                        index_flags=_non_default_index_flags(entries),
                        external_worktree=external_worktree,
                    )
                )
        observation = blocked_checkpoint.SafetyObservation(
            workspace_identity_matches=identity_matches,
            workspace_owned=int(plan["workspace_owned"] or 0) == WORKSPACE_OWNED,
            head_oid=head_oid,
            actual_branch=actual_branch,
            expected_branch=branch,
            operations=operations,
            dirty_paths=tuple(dirty_paths),
            shared_metadata_owned=shared_metadata_owned,
            project_blocked=project_blocked,
            legacy_scope_uncertain=(
                request.reason_code
                == BlockReasonCode.LEGACY_UNSCOPED_BLOCK.value
            ),
            agent_scope_recovery_pending=bool(
                plan["agent_recovery"]
                or (
                    plan["agent_snapshot"]
                    and not self._validated_guidance_checkpoint(request, plan, snapshot)
                )
            ),
        )
        return blocked_checkpoint.assess_safety(observation)

    def _validated_guidance_checkpoint(
        self, request: blocked_checkpoint.CheckpointRequest,
        plan: sqlite3.Row, current: GitSnapshot | None,
    ) -> bool:
        if (
            request.reason_code != GUIDANCE_BLOCK_REASON
            or plan["agent_role"] != "worker"
            or current is None
            or not plan["agent_result_snapshot"]
        ):
            return False
        rows = _guidance_rows(self.connection, request.repo, request.plan_id)
        if not rows or any(row["invalidated_decision_hash"] for row in rows):
            return False
        worktree = Path(str(request.worktree))
        try:
            document = _guidance_document(worktree, str(plan["path"]))
            if document is None or document.structure_hash != plan["guidance_plan_hash"]:
                return False
            _guidance_document_matches_rows(document, rows, require_decisions=False)
            before = GitSnapshot.deserialize(str(plan["agent_snapshot"]))
            result = GitSnapshot.deserialize(str(plan["agent_result_snapshot"]))
            validate_agent_scope(
                "worker", worktree, before, result,
                selected_plan_path=str(plan["path"]),
                allowed_guidance_targets=self._approved_guidance_targets(request),
                allow_common_plans=_published_plan_allows_common_plans(self.repo_path, plan),
            )
            if _same_local_snapshot(result, current):
                return True
            # A crash can occur after commit but before recording the Manager
            # snapshot. Accept only the durable prepared tree and identity.
            return bool(
                request.commit_identity and request.tree_oid
                and not current.dirty
                and git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip() == request.tree_oid
                and f"Checkpoint-Identity: {request.commit_identity}" in
                git(worktree, "show", "-s", "--format=%B", "HEAD").stdout
            )
        except (RuntimeError, ValueError, TypeError, KeyError):
            return False

    def _record_guidance_checkpoint_scope(
        self, request: blocked_checkpoint.CheckpointRequest,
    ) -> None:
        if request.reason_code != GUIDANCE_BLOCK_REASON:
            return
        snapshot = capture_git_snapshot(Path(str(request.worktree))).serialize()
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET agent_snapshot = ?, agent_result_snapshot = ? "
                "WHERE repo = ? AND plan_id = ?",
                (snapshot, snapshot, request.repo, request.plan_id),
            )
            self.connection.execute(
                "UPDATE projects SET agent_snapshot = ?, agent_result_snapshot = ? "
                "WHERE repo = ? AND plan_path = "
                "(SELECT path FROM exec_plans WHERE repo = ? AND plan_id = ?)",
                (snapshot, snapshot, request.repo, request.repo, request.plan_id),
            )

    def _shared_safety_error(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        plan: sqlite3.Row,
        current: GitSnapshot,
    ) -> blocked_checkpoint.SafetyVerdict | None:
        """Check persisted ownership before any GitHub or remote operation."""
        def reject(reason, detail):
            return blocked_checkpoint.SafetyVerdict(
                False, blocked_checkpoint.SafetyScope.PROJECT,
                reason, detail, current.head, (),
            )

        project = self.connection.execute(
            "SELECT origin FROM projects WHERE repo = ?", (request.repo,)
        ).fetchone()
        try:
            expected = str(project["origin"] or "") if project else ""
            if not expected:
                return blocked_checkpoint.SafetyVerdict(
                    False, blocked_checkpoint.SafetyScope.PLAN,
                    "registered_origin_missing",
                    "등록 origin이 없어 checkpoint를 local-only로 보존합니다.",
                    current.head, (),
                )
            for context in (self.repo_path, Path(request.worktree)):
                for arguments in (("remote", "get-url", "--all", "origin"),
                                  ("remote", "get-url", "--push", "--all", "origin")):
                    urls = git(context, *arguments).stdout.splitlines()
                    if len(urls) != 1 or github_repository(urls[0]) != expected:
                        raise RuntimeError("등록 origin과 fetch/push 대상이 다릅니다.")
        except RuntimeError as error:
            return reject("shared_git_config_mutation", str(error))

        serialized = plan["agent_snapshot"] or plan["agent_result_snapshot"]
        try:
            if not serialized:
                raise ValueError("persisted Git snapshot이 없습니다.")
            before = GitSnapshot.deserialize(str(serialized))
            if not before.shared_metadata_complete:
                raise ValueError("persisted shared metadata가 없습니다.")
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            return reject("workspace_ownership_uncertain", str(error))
        if before.config != current.config:
            return reject("shared_git_config_mutation", "checkpoint Git config가 저장된 snapshot과 다릅니다.")
        own_ref = f"refs/heads/{request.branch}"
        remote_ref = f"refs/remotes/origin/{request.branch}"
        owned_remote_oid = (
            _owned_checkpoint_remote_oid(
                self.connection, self.repo_path, request, plan, current,
            )
            if before.refs.get(remote_ref) != current.refs.get(remote_ref)
            else None
        )
        fetched_refs = _proven_manager_base_refs(
            self.connection, self.repo_path, request.repo, request.plan_id,
            before, current,
        )
        for name in before.refs.keys() | current.refs.keys():
            if name == own_ref or name in fetched_refs or name.startswith("refs/codex/turn-diffs/"):
                continue
            if before.refs.get(name) == current.refs.get(name):
                continue
            if name == remote_ref and current.refs.get(name) == (owned_remote_oid, ""):
                continue
            return reject("shared_ref_mutation", f"checkpoint 공유 ref 변경: {name}")
        own_path = str(_canonical_path(Path(request.worktree)))
        cleaned = _proven_merged_cleanup_paths(
            self.connection, request.repo, before, current,
        )
        for path in before.worktrees.keys() | current.worktrees.keys():
            if (path != own_path and path not in cleaned
                    and before.worktrees.get(path) != current.worktrees.get(path)):
                return reject("outside_managed_worktree_mutation", f"checkpoint 외부 worktree 변경: {path}")
        return None

    def existing_pull_request(
        self, request: blocked_checkpoint.CheckpointRequest
    ) -> object | None:
        if not request.branch:
            return None
        try:
            return pr_for_branch(self.repo_path, request.branch)
        except RuntimeError as error:
            raise blocked_checkpoint.CheckpointPushError(
                f"checkpoint PR 조회 실패: {error}", retryable=True
            ) from error

    def _tree_for_paths(
        self, worktree: Path, paths: tuple[str, ...]
    ) -> str:
        with tempfile.TemporaryDirectory(
            prefix="loop-manager-checkpoint-index-"
        ) as directory:
            temporary_index = Path(directory) / "index"
            _git_with_index(worktree, temporary_index, "read-tree", "HEAD")
            if paths:
                _git_with_index(
                    worktree,
                    temporary_index,
                    "add",
                    "--all",
                    "--",
                    *paths,
                )
            return _git_with_index(
                worktree, temporary_index, "write-tree"
            ).stdout.strip()

    def prepare(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        verdict: blocked_checkpoint.SafetyVerdict,
    ) -> blocked_checkpoint.PreparedCheckpoint:
        if not verdict.safe or verdict.head_oid is None or request.worktree is None:
            raise RuntimeError("안전하지 않은 checkpoint를 준비할 수 없습니다.")
        worktree = Path(request.worktree)
        tree_oid = self._tree_for_paths(worktree, verdict.included_paths)
        identity = blocked_checkpoint.checkpoint_identity(
            plan_id=request.plan_id,
            reason_code=request.reason_code,
            evidence=request.evidence,
            parent_oid=verdict.head_oid,
            tree_oid=tree_oid,
            included_paths=verdict.included_paths,
        )
        return blocked_checkpoint.PreparedCheckpoint(
            parent_oid=verdict.head_oid,
            tree_oid=tree_oid,
            commit_identity=identity,
            message=blocked_checkpoint.checkpoint_message(
                plan_id=request.plan_id,
                reason_code=request.reason_code,
                evidence=request.evidence,
                identity=identity,
                included_paths=verdict.included_paths,
            ),
            included_paths=verdict.included_paths,
        )

    def ensure_local_commit(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        prepared: blocked_checkpoint.PreparedCheckpoint,
    ) -> str:
        if request.worktree is None:
            raise RuntimeError("checkpoint worktree가 저장되지 않았습니다.")
        worktree = Path(request.worktree)
        current_head = git(worktree, "rev-parse", "HEAD^{commit}").stdout.strip()
        if current_head != prepared.parent_oid:
            current_tree = git(
                worktree, "rev-parse", "HEAD^{tree}"
            ).stdout.strip()
            current_message = git(
                worktree, "show", "-s", "--format=%B", "HEAD"
            ).stdout
            if (
                current_tree == prepared.tree_oid
                and f"Checkpoint-Identity: {prepared.commit_identity}"
                in current_message
            ):
                self._record_guidance_checkpoint_scope(request)
                return current_head
            raise RuntimeError(
                "checkpoint 준비 뒤 관리 branch HEAD가 예상하지 못하게 변경됐습니다."
            )
        if not prepared.included_paths:
            head_tree = git(
                worktree, "rev-parse", "HEAD^{tree}"
            ).stdout.strip()
            if head_tree != prepared.tree_oid:
                raise RuntimeError("clean checkpoint tree가 현재 HEAD와 다릅니다.")
            self._record_guidance_checkpoint_scope(request)
            return current_head

        git(
            worktree,
            "add",
            "--all",
            "--",
            *prepared.included_paths,
        )
        actual_tree = git(worktree, "write-tree").stdout.strip()
        if actual_tree != prepared.tree_oid:
            raise RuntimeError(
                "checkpoint stage 결과가 사전 검증한 tree와 다릅니다."
            )
        with tempfile.TemporaryDirectory(
            prefix="loop-manager-checkpoint-message-"
        ) as directory:
            message_path = Path(directory) / "message"
            message_path.write_text(prepared.message, encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "GIT_AUTHOR_NAME": "Loop Manager",
                    "GIT_AUTHOR_EMAIL": "loop-manager@localhost",
                    "GIT_COMMITTER_NAME": "Loop Manager",
                    "GIT_COMMITTER_EMAIL": "loop-manager@localhost",
                }
            )
            committed = command(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={directory}",
                    "-c",
                    "commit.gpgSign=false",
                    "-C",
                    str(worktree),
                    "commit",
                    "--file",
                    str(message_path),
                ],
                check=False,
                env=environment,
            )
        if committed.returncode:
            raise RuntimeError(
                "checkpoint commit 실패:\n" + committed.stdout[-8000:]
            )
        local_oid = git(
            worktree, "rev-parse", "HEAD^{commit}"
        ).stdout.strip()
        if git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip() != prepared.tree_oid:
            raise RuntimeError("checkpoint commit tree 검증에 실패했습니다.")
        self._record_guidance_checkpoint_scope(request)
        return local_oid

    def observe_remote(
        self, request: blocked_checkpoint.CheckpointRequest
    ) -> str | None:
        if not request.branch:
            raise RuntimeError("checkpoint branch가 저장되지 않았습니다.")
        result = git(
            self.repo_path,
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{request.branch}",
            check=False,
        )
        if result.returncode:
            raise blocked_checkpoint.CheckpointPushError(
                "checkpoint 원격 branch 조회 실패:\n" + result.stdout[-8000:],
                retryable=True,
            )
        rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 2:
            raise RuntimeError("checkpoint 원격 branch 응답이 유일하지 않습니다.")
        oid = rows[0][0]
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid) is None:
            raise RuntimeError("checkpoint 원격 branch OID가 유효하지 않습니다.")
        return oid

    def is_fast_forward(self, remote_oid: str, local_oid: str) -> bool:
        result = git(
            self.repo_path,
            "merge-base",
            "--is-ancestor",
            remote_oid,
            local_oid,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError("checkpoint fast-forward 관계를 확인할 수 없습니다.")
        return result.returncode == 0

    def push(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        local_oid: str,
        decision: blocked_checkpoint.PushDecision,
    ) -> None:
        if not request.branch or not request.worktree:
            raise blocked_checkpoint.CheckpointPushError(
                "checkpoint branch/worktree가 저장되지 않았습니다.",
                retryable=False,
            )
        arguments = [
            "push",
            "origin",
            f"{local_oid}:refs/heads/{request.branch}",
        ]
        if decision.mode is blocked_checkpoint.PushMode.REBASE_LEASE:
            if not request.expected_remote_oid:
                raise blocked_checkpoint.CheckpointPushError(
                    "rebase checkpoint lease OID가 없습니다.", retryable=False
                )
            arguments.insert(
                1,
                "--force-with-lease="
                f"refs/heads/{request.branch}:{request.expected_remote_oid}",
            )
        pushed = git(Path(request.worktree), *arguments, check=False)
        if pushed.returncode:
            normalized = " ".join(pushed.stdout.casefold().split())
            permanent = any(
                marker in normalized
                for marker in (
                    "protected branch",
                    "permission denied",
                    "not permitted",
                    "authentication failed",
                    "authorization failed",
                    "http 403",
                    "stale info",
                    "fetch first",
                    "non-fast-forward",
                )
            )
            raise blocked_checkpoint.CheckpointPushError(
                "checkpoint push 실패:\n" + pushed.stdout[-8000:],
                retryable=not permanent,
            )


class _BlockedCheckpointResults:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def promote_project_block(
        self,
        request: blocked_checkpoint.CheckpointRequest,
        verdict: blocked_checkpoint.SafetyVerdict,
    ) -> None:
        reason = {
            "shared_ref_mutation": BlockReasonCode.SHARED_REF_MUTATION,
            "shared_git_config_mutation": BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
            "outside_managed_worktree_mutation": BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION,
            "workspace_ownership_uncertain": (
                BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN
            ),
        }.get(
            str(verdict.reason_code),
            BlockReasonCode.REPOSITORY_STATE_UNSAFE,
        )
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                request.repo,
                reason,
                verdict.detail or "checkpoint project safety violation",
                request.request_identity,
                plan_id=request.plan_id,
                previous_phase=None,
            )

    def finalized(self, request: blocked_checkpoint.CheckpointRequest) -> None:
        # The repository commits the result and outbox together. There must be
        # no separate, crash-sensitive notification transaction here.
        pass


def dispatch_blocked_checkpoints(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    skip_identities: frozenset[str] = frozenset(),
) -> int:
    """Commit prior block state, then process its network-independent request."""

    connection.commit()
    repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
    adapter = _BlockedCheckpointAdapter(
        connection, repo_path, state_dir, repo
    )
    results = _BlockedCheckpointResults(connection)
    dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(
        repository,
        adapter,
        adapter,
        results,
        max_push_attempts=MAX_TRANSIENT_AGENT_RETRIES,
    )
    try:
        return dispatcher.dispatch(repo, skip_identities=skip_identities)
    except blocked_checkpoint.CheckpointPushError as error:
        for request in repository.pending(repo):
            if request.request_identity in skip_identities:
                continue
            repository.finish_unpublished(
                request, str(error), retryable=error.retryable
            )
            results.finalized(request)
        return 0
    except (OSError, RuntimeError) as error:
        for request in repository.pending(repo):
            if request.request_identity in skip_identities:
                continue
            repository.finish_unpublished(
                request,
                f"checkpoint 처리 실패: {error}",
                retryable=True,
            )
            results.finalized(request)
        return 0



def _execution_outcome_step_result(outcome: ExecutionOutcome) -> int:
    """Translate a typed execution result at the process adapter boundary."""

    if (outcome.recovery is not None
            and outcome.recovery.action is recovery_runtime.RecoveryAction.CORRECT
            and outcome.context is not None):
        reason = outcome.recovery.observation.reason
        if (
            reason is recovery_runtime.RecoveryReason.WORKER_CORRECTION
            and outcome.context.phase is ExecutionPhase.IMPLEMENTING
        ) or (
            reason is recovery_runtime.RecoveryReason.REBASE_CONFLICT
            and outcome.context.phase is not ExecutionPhase.IMPLEMENTING
        ):
            # Stop after an initial Manager rebase conflict or a Worker output
            # protocol error; the saved session receives that feedback next run.
            return 1
    if outcome.kind in {
        ExecutionOutcomeKind.SUCCESS,
        ExecutionOutcomeKind.STABLE_WAIT,
    }:
        return 0
    if outcome.kind in {
        ExecutionOutcomeKind.PHASE_ADVANCED,
        ExecutionOutcomeKind.RETRY,
    }:
        return PROJECT_STEP_RETRY
    if outcome.kind is ExecutionOutcomeKind.TRANSIENT_PAUSE:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is ExecutionOutcomeKind.USAGE_PAUSE:
        return PROJECT_STEP_USAGE_PAUSE
    if outcome.kind is ExecutionOutcomeKind.PLANNING_PENDING:
        return PROJECT_STEP_PLANNING_PENDING
    return 1


def _typed_execution_git(path: Path, *arguments: str, **options: object):
    """Keep Git operation identity when a shared subprocess helper fails."""

    command = arguments[0] if arguments else ""
    reason = {
        "fetch": recovery_runtime.RecoveryReason.FETCH_FAILED,
        "status": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "diff": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "rev-parse": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "merge-base": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "add": recovery_runtime.RecoveryReason.STAGE_FAILED,
        "commit": recovery_runtime.RecoveryReason.STAGE_FAILED,
        "rebase": recovery_runtime.RecoveryReason.REBASE_CONTINUE_FAILED,
        "-c": recovery_runtime.RecoveryReason.REBASE_CONTINUE_FAILED,
    }.get(command, recovery_runtime.RecoveryReason.WORKTREE_RECOVERY_FAILED)
    try:
        result = git(path, *arguments, **options)
        if result.returncode and is_git_lock_failure(result.stdout):
            raise recovery_runtime.RecoveryFailure(
                recovery_runtime.RecoveryReason.GIT_LOCK,
                result.stdout[-8000:],
            )
        return result
    except recovery_runtime.RecoveryFailure:
        raise
    except GitLockError as error:
        raise recovery_runtime.RecoveryFailure(
            recovery_runtime.RecoveryReason.GIT_LOCK, str(error)
        ) from error
    except (RuntimeError, OSError) as error:
        raise recovery_runtime.RecoveryFailure(reason, str(error)) from error


def _typed_execution_command(*arguments: object, **options: object):
    try:
        return command(*arguments, **options)
    except (RuntimeError, OSError) as error:
        raise recovery_runtime.RecoveryFailure(
            recovery_runtime.RecoveryReason.PUBLICATION_RETRY, str(error)
        ) from error


def _ExecutionLifecyclePorts(connection, runtime_repository, **identity):
    """Bind named effects to legacy helpers, preserving their patch points."""
    verify_timeout_seconds = identity.pop("verify_timeout_seconds", 600)
    state = _ExecutionStatePort(connection, runtime_repository, identity["repo"])
    effects = execution_handlers.ExecutionEffects(
        state=state,
        checkpoint=execution_checkpoint_state.ExecutionCheckpointRepository(connection),
        contract=SimpleNamespace(**{name: globals()[name] for name in (
            'AgentResultKind', 'AgentScopeError', 'ExecPlanMetadataError',
            'GitLockError', 'MAX_REVIEWER_PROTOCOL_ATTEMPTS',
            'MAX_VALIDATION_FINGERPRINT_REPEATS', 'PROJECT_STEP_RETRY',
            'PROJECT_STEP_TRANSIENT_PAUSE', 'PROJECT_STEP_USAGE_PAUSE',
            'PROJECT_STEP_PLANNING_PENDING', 'PublicationSafetyError',
            'REBASE_CONFLICT_PREFIX', 'REBASE_IN_PROGRESS_ERROR',
            'RequirementArchiveCollisionError', 'RequirementArchiveError',
            'ReviewerProtocolError', 'TRANSIENT_AGENT_ERROR_PREFIX',
            'USAGE_LIMIT_ERROR_PREFIX', 'WORKSPACE_OWNED', 'WORKSPACE_RESERVED',
            'WorkerResultError', 'WorkspaceOwnershipError',
            'reviewer_targeted_test_reason',
            'VerifyTerminationUncertain',
        )}),
        workspace=execution_handlers.WorkspaceEffects(
            is_directory=lambda path: path.is_dir(),
            git=state.bind(_typed_execution_git, name="git", connected=False, mutates=False),
            make_worktree=state.bind(make_worktree, name="make_worktree", connected=False, mutates=False),
            branch_has_changes=state.bind(branch_has_changes, name="branch_has_changes", connected=False, mutates=False),
            remote_branch_oid=state.bind(remote_branch_oid, name="remote_branch_oid", connected=False, mutates=False),
            _rebase_in_progress_for_branch=state.bind(_rebase_in_progress_for_branch, name="_rebase_in_progress_for_branch", connected=False, mutates=False),
            validate_execplan_workspace_identity=state.bind(validate_execplan_workspace_identity, name="validate_execplan_workspace_identity", connected=False, mutates=False),
            validate_managed_worktree=state.bind(validate_managed_worktree, name="validate_managed_worktree", connected=False, mutates=False),
            _plan_id_from_path=state.bind(_plan_id_from_path, name="_plan_id_from_path", connected=False, mutates=False),
            reserve_execplan_workspace=state.bind(reserve_execplan_workspace, name="reserve_execplan_workspace", connected=True, mutates=True),
            mark_execplan_workspace_owned=state.bind(mark_execplan_workspace_owned, name="mark_execplan_workspace_owned", connected=True, mutates=True),
            resume_pr_conflict_rebase=state.bind(resume_pr_conflict_rebase, name="resume_pr_conflict_rebase", connected=True, mutates=True),
            git_operation_entries=state.bind(_git_operation_entries, name="git_operation_entries", connected=False, mutates=False),
        ),
        agent=execution_handlers.AgentEffects(
            begin_agent_scope=state.bind(begin_agent_scope, name="begin_agent_scope", connected=True, mutates=True),
            capture_git_snapshot=state.bind(capture_git_snapshot, name="capture_git_snapshot", connected=False, mutates=False),
            path_signature=state.bind(_path_signature, name="path_signature", connected=False, mutates=False),
            clear_agent_scope=state.bind(clear_agent_scope, name="clear_agent_scope", connected=True, mutates=True),
            validate_agent_scope=state.bind(validate_agent_scope, name="validate_agent_scope", connected=False, mutates=False),
            validate_worker_workspace_scope=state.bind(validate_worker_workspace_scope, name="validate_worker_workspace_scope", connected=False, mutates=False),
            _worktree_execplan_input_fingerprint=state.bind(_worktree_execplan_input_fingerprint, name="_worktree_execplan_input_fingerprint", connected=False, mutates=False),
            _revision_execplan_input_fingerprint=state.bind(_revision_execplan_input_fingerprint, name="_revision_execplan_input_fingerprint", connected=False, mutates=False),
            _guidance_worker_targets=state.bind(_guidance_worker_targets, name="_guidance_worker_targets", connected=True, mutates=False),
            guidance_resume_context=state.bind(guidance_resume_context, name="guidance_resume_context", connected=True, mutates=False),
            _published_plan_allows_common_plans=state.bind(_published_plan_allows_common_plans, name="_published_plan_allows_common_plans", connected=False, mutates=False),
            is_loop_manager_repository=state.bind(is_loop_manager_repository, name="is_loop_manager_repository", connected=False, mutates=False),
            inspect_worker_guidance_result=state.bind(inspect_worker_guidance_result, name="inspect_worker_guidance_result", connected=True, mutates=True),
            validate_guidance_publication=state.bind(validate_guidance_publication, name="validate_guidance_publication", connected=True, mutates=True),
            run_agent=state.bind(_run_agent_with_connection, name="run_agent", connected=True, mutates=False),
            run_agent_with_retries=state.bind(run_agent_with_retries, name="run_agent_with_retries", connected=False, mutates=False),
            worker_prompt=state.bind(worker_prompt, name="worker_prompt", connected=False, mutates=False),
            reviewer_prompt=state.bind(reviewer_prompt, name="reviewer_prompt", connected=False, mutates=False),
            resolve_worker_profile=lambda config_path, provider, level, worktree: _agent_profile(
                config_path, provider, "worker", level, worktree,
            ),
        ),
        validation=execution_handlers.ValidationEffects(
            verify=state.bind(
                lambda worktree: verify(
                    worktree, verify_timeout_seconds,
                ),
                name="verify", connected=False, mutates=False,
            ),
            block_verify_timeout_limit=state.bind(
                block_verify_timeout_limit, name="block_verify_timeout_limit",
                connected=True, mutates=True,
            ),
            parse_worker_result=state.bind(parse_worker_result, name="parse_worker_result", connected=False, mutates=False),
            parse_reviewer_result=state.bind(parse_reviewer_result, name="parse_reviewer_result", connected=False, mutates=False),
            validate_active_execplan_metadata=state.bind(validate_active_execplan_metadata, name="validate_active_execplan_metadata", connected=False, mutates=False),
            ensure_worker_level_state=state.bind(ensure_worker_level_state, name="ensure_worker_level_state", connected=True, mutates=True),
            clear_validation_failure_history=state.bind(clear_validation_failure_history, name="clear_validation_failure_history", connected=True, mutates=True),
            transition_validation_failure=state.bind(transition_validation_failure, name="transition_validation_failure", connected=True, mutates=True),
        ),
        feedback=execution_handlers.FeedbackEffects(
            repository=validation_feedback_state.ValidationFeedbackRepository(connection),
            fingerprints=validation_failure_fingerprints,
        ),
        archive=execution_handlers.ArchiveEffects(
            requirement_archive_sources=state.bind(requirement_archive_sources, name="requirement_archive_sources", connected=True, mutates=False),
            requirement_archive_snapshot=state.bind(requirement_archive_snapshot, name="requirement_archive_snapshot", connected=True, mutates=False),
            requirement_archive_candidates=state.bind(requirement_archive_candidates, name="requirement_archive_candidates", connected=True, mutates=False),
            requirement_archive_move_set=state.bind(requirement_archive_move_set, name="requirement_archive_move_set", connected=False, mutates=False),
            requirement_archive_reconciliation_error=state.bind(requirement_archive_reconciliation_error, name="requirement_archive_reconciliation_error", connected=False, mutates=False),
            requirement_archive_touched_sources=state.bind(requirement_archive_touched_sources, name="requirement_archive_touched_sources", connected=False, mutates=False),
        ),
        publication=execution_handlers.PublicationEffects(
            pr_for_branch=state.bind(pr_for_branch, name="pr_for_branch", connected=False, mutates=False),
            reconcile_existing_pull_request=state.bind(reconcile_existing_pull_request, name="reconcile_existing_pull_request", connected=True, mutates=True),
            record_remote_publication_branch=state.bind(record_remote_publication_branch, name="record_remote_publication_branch", connected=True, mutates=True),
            push_managed_publication_branch=state.bind(push_managed_publication_branch, name="push_managed_publication_branch", connected=True, mutates=True),
            push_owned_pr_conflict_recovery=state.bind(push_owned_pr_conflict_recovery, name="push_owned_pr_conflict_recovery", connected=True, mutates=True),
            published_execplan=state.bind(published_execplan, name="published_execplan", connected=False, mutates=False),
            build_pr_body=state.bind(build_pr_body, name="build_pr_body", connected=False, mutates=False),
            command=state.bind(_typed_execution_command, name="command", connected=False, mutates=False),
        ),
        blocking=execution_handlers.BlockingEffects(
            block_active_execplan=state.bind(block_active_execplan, name="block_active_execplan", connected=True, mutates=True),
            block_active_execplan_from_revision=state.bind(block_active_execplan_from_revision, name="block_active_execplan_from_revision", connected=True, mutates=True),
        ),
        events=execution_handlers.EventsEffects(
            operation_log=state.bind(operation_log, name="operation_log", connected=False, mutates=False),
            operation_print=state.bind(operation_print, name="operation_print", connected=False, mutates=False),
        ),
        revision=execution_handlers.RevisionEffects(
            reconcile_catalog=state.bind(reconcile_execution_execplan_catalog, name="reconcile_execplan_catalog", connected=True, mutates=True),
            source_fingerprint=state.bind(_revision_execplan_source_fingerprint, name="_revision_execplan_source_fingerprint", connected=False, mutates=False),
            body_hash=state.bind(_revision_execplan_body_hash, name="_revision_execplan_body_hash", connected=False, mutates=False),
            changed_paths=state.bind(_revision_changed_paths, name="_revision_changed_paths", connected=False, mutates=False),
            unmet_dependencies=state.bind(unmet_execplan_dependencies, name="unmet_execplan_dependencies", connected=True, mutates=False),
        ),
    )
    return execution_handlers.ExecutionHandlers(effects, **identity)


if __name__ == "__main__":
    raise SystemExit(main())
