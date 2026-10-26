"""Transactional ExecPlan runtime state and compatibility projection boundary."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
import sqlite3
from typing import Callable, Iterator, Mapping
from uuid import uuid4

from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import blocked_checkpoint_state


ACTIVE_STATUSES = frozenset(
    {"IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING"}
)

execution_selection: ContextVar[tuple[str, str] | None] = ContextVar("execution_selection", default=None)
include_blocked_projection = ContextVar("include_blocked_projection", default=False)
admitting_execution: ContextVar[bool] = ContextVar("admitting_execution", default=False)


@contextmanager
def selected_execution(repo: str, plan_id: str, *, include_blocked=False):
    token = execution_selection.set((repo, plan_id))
    blocked_token = include_blocked_projection.set(include_blocked)
    try:
        yield
    finally:
        include_blocked_projection.reset(blocked_token)
        execution_selection.reset(token)


def project_row(connection: sqlite3.Connection, repo: str):
    """프로젝트 등록 정보와 현재 호출이 소유한 작업의 실행 상태를 읽는다."""
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    selected = execution_selection.get()
    if row is None or selected is None or selected[0] != repo:
        return row
    snapshot = RuntimeStateRepository(connection).get(*selected)
    return {**dict(row), **snapshot.project_values()}

COMPATIBILITY_PHASE_BY_STATUS = {
    "IMPLEMENTING": "WORKING",
}
COMPATIBILITY_STATUS_BY_PHASE = {
    phase: status for status, phase in COMPATIBILITY_PHASE_BY_STATUS.items()
}

# These fields have the same stored representation in the ExecPlan source row and
# the legacy projects compatibility projection.
MIRRORED_FIELDS = (
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
    "worker_session_binding",
    "worker_session_transition",
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
    "definition_snapshot",
    "worker_input_token",
    "archive_token",
    "verify_token",
    "review_token",
    "publication_token",
    "revision_resume_status",
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

# These values belong only to project registration or planning. They may be
# changed beside a projection clear, but are never copied into exec_plans.
PROJECT_ONLY_FIELDS = frozenset(
    {
        "base_branch",
        "provider",
        "processed_sha",
        "specification_fingerprint",
        "planning_fingerprint",
        "planning_attempt_fingerprint",
        "planning_workspace_owned",
        "planning_retry_count",
        "planner_session_id",
        "planning_validation_error",
        "planning_result_pending_validation",
        "planning_result_completed",
        "planning_result",
        "planning_preserved_state",
        "planning_resume_plan_id",
        "planning_error",
        "planning_error_fingerprint",
        "planning_error_incident_id",
        "planning_error_origin",
        "planning_correction_error",
        "planning_correction_incident_id",
        "planning_correction_sha",
        "planning_correction_context",
    }
)

PROJECT_UPDATE_FIELDS = frozenset(MIRRORED_FIELDS) | PROJECT_ONLY_FIELDS | {
    "phase",
    "plan_path",
}

EXECPLAN_RUNTIME_FIELDS = frozenset(MIRRORED_FIELDS) | {
    "archive_resume_status",
    "status",
    "blocked_fingerprint",
    "blocked_worktree_fingerprint",
    "blocked_review_fingerprint",
    "retry_count",
    "verify_failure_history",
    "review_failure_history",
    "initial_worker_level",
    "effective_worker_level",
    "review_level_history",
    "review_correction_state",
    "blocked_reason",
    "guidance_plan_hash",
    "workspace_owned",
    "ever_started",
}

PLANNING_CLEAR_VALUES: Mapping[str, object] = {
    "planning_attempt_fingerprint": None,
    "planning_workspace_owned": 0,
    "planning_retry_count": 0,
    "planner_session_id": None,
    "planning_validation_error": None,
    "planning_result_pending_validation": 0,
    "planning_result_completed": 0,
    "planning_result": None,
    "planning_error": None,
    "planning_error_fingerprint": None,
    "planning_error_incident_id": None,
    "planning_error_origin": None,
    "planning_correction_error": None,
    "planning_correction_incident_id": None,
    "planning_correction_sha": None,
    "planning_correction_context": None,
}

EXECUTION_CLEAR_VALUES: Mapping[str, object] = {
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
    "worker_session_id": None,
    "worker_session_binding": None,
    "worker_session_transition": None,
    "reviewer_session_id": None,
    "worker_result": None,
    "verify_result": None,
    "review_result": None,
    "definition_snapshot": None,
    "worker_input_token": None,
    "archive_token": None,
    "verify_token": None,
    "review_token": None,
    "publication_token": None,
    "revision_resume_status": None,
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


class RuntimeStateError(RuntimeError):
    """Raised when an expected runtime row or state no longer matches."""


class TaskLimitReached(RuntimeStateError):
    """새 관리 배정 전에 유효 상한이 줄거나 자리가 사용됐다."""


def project_phase_for_status(status: str) -> str:
    """Translate the source status to its stored compatibility phase."""

    return COMPATIBILITY_PHASE_BY_STATUS.get(status, status)


def execplan_status_for_phase(phase: object) -> str | None:
    """Translate a compatibility phase without projecting planning state."""

    if phase is None or phase == "PLANNING":
        return None
    normalized = str(phase)
    return COMPATIBILITY_STATUS_BY_PHASE.get(normalized, normalized)


def _preserved_planning_values(raw: object) -> dict[str, object]:
    try:
        values = json.loads(str(raw))
    except (TypeError, ValueError) as error:
        raise RuntimeStateError("보존된 planning 상태가 유효하지 않습니다.") from error
    targets = values.pop("unconfirmed_plan_ids", None) if isinstance(values, dict) else None
    if (targets is not None and (not isinstance(targets, list)
            or any(not isinstance(value, str) or not re.fullmatch(r"G-\d{3,}", value) for value in targets)
            or len(targets) != len(set(targets)))):
        raise RuntimeStateError("보존된 planning 대상이 유효하지 않습니다.")
    if (not isinstance(values, dict)
            or set(values) != set(EXECUTION_CLEAR_VALUES) | set(PLANNING_CLEAR_VALUES)
            or not values["planning_error"] or values["phase"] not in (None, "PLANNING")):
        raise RuntimeStateError("보존된 planning 상태가 유효하지 않습니다.")
    return values


def planning_owns_execution_slot(project: sqlite3.Row) -> bool:
    """Only a verified suspension separates preserved errors from slot ownership."""
    if 'planning_resume_plan_id' in project.keys() and project['planning_resume_plan_id']:
        return True

    if "planning_preserved_state" in project.keys() and project["planning_preserved_state"]:
        try:
            _preserved_planning_values(project["planning_preserved_state"])
        except RuntimeStateError:
            return True
        return project["phase"] == "PLANNING"
    return bool(
        project["phase"] == "PLANNING"
        or project["planning_error"]
        or project["planning_error_fingerprint"]
        or project["planning_attempt_fingerprint"]
        or project["planning_workspace_owned"]
        or project["planning_retry_count"]
        or project["planner_session_id"]
        or project["planning_validation_error"]
        or project["planning_result_pending_validation"]
        or project["planning_result_completed"]
        or project["planning_result"]
    )


@dataclass(frozen=True)
class PlanningSnapshot:
    """Typed view of the project-owned planning lifecycle fields."""

    repo: str
    phase: str | None
    processed_sha: str | None
    specification_fingerprint: str | None
    planning_fingerprint: str | None
    planning_attempt_fingerprint: str | None
    planning_workspace_owned: int
    planning_retry_count: int
    planner_session_id: str | None
    planning_validation_error: str | None
    planning_result_pending_validation: bool
    planning_result_completed: bool
    planning_result: str | None
    planning_error: str | None
    planning_error_fingerprint: str | None
    planning_error_incident_id: str | None
    planning_error_origin: str | None
    planning_correction_error: str | None
    planning_correction_incident_id: str | None
    planning_correction_sha: str | None
    planning_correction_context: str | None
    active_sha: str | None
    worktree: str | None
    last_error: str | None
    agent_role: str | None
    agent_snapshot: str | None
    agent_result_snapshot: str | None
    planning_preserved_state: str | None = None
    planning_resume_plan_id: str | None = None

    @property
    def preserved_worktree(self) -> str | None:
        if self.planning_preserved_state:
            return _preserved_planning_values(self.planning_preserved_state)["worktree"]
        return self.worktree

    @property
    def preserved_unconfirmed_plan_ids(self) -> tuple[str, ...] | None:
        if not self.planning_preserved_state:
            return None
        _preserved_planning_values(self.planning_preserved_state)
        targets = json.loads(self.planning_preserved_state).get("unconfirmed_plan_ids")
        return tuple(targets) if targets is not None else None

    def __getitem__(self, field: str) -> object:
        """Support the narrow compatibility helpers during coordinator extraction."""

        if field not in self.__dataclass_fields__:
            raise KeyError(field)
        return getattr(self, field)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PlanningSnapshot":
        if row["planning_preserved_state"]:
            _preserved_planning_values(row["planning_preserved_state"])
        return cls(
            planning_preserved_state=_text(row, "planning_preserved_state"),
            planning_resume_plan_id=_text(row, "planning_resume_plan_id"),
            repo=str(row["repo"]),
            phase=_text(row, "phase"),
            processed_sha=_text(row, "processed_sha"),
            specification_fingerprint=_text(
                row, "specification_fingerprint"
            ),
            planning_fingerprint=_text(row, "planning_fingerprint"),
            planning_attempt_fingerprint=_text(
                row, "planning_attempt_fingerprint"
            ),
            planning_workspace_owned=int(row["planning_workspace_owned"]),
            planning_retry_count=int(row["planning_retry_count"]),
            planner_session_id=_text(row, "planner_session_id"),
            planning_validation_error=_text(row, "planning_validation_error"),
            planning_result_pending_validation=bool(
                row["planning_result_pending_validation"]
            ),
            planning_result_completed=bool(row["planning_result_completed"]),
            planning_result=_text(row, "planning_result"),
            planning_error=_text(row, "planning_error"),
            planning_error_fingerprint=_text(
                row, "planning_error_fingerprint"
            ),
            planning_error_incident_id=_text(row, "planning_error_incident_id"),
            planning_error_origin=_text(row, "planning_error_origin"),
            planning_correction_error=_text(row, "planning_correction_error"),
            planning_correction_incident_id=_text(row, "planning_correction_incident_id"),
            planning_correction_sha=_text(row, "planning_correction_sha"),
            planning_correction_context=_text(row, "planning_correction_context"),
            active_sha=_text(row, "active_sha"),
            worktree=_text(row, "worktree"),
            last_error=_text(row, "last_error"),
            agent_role=_text(row, "agent_role"),
            agent_snapshot=_text(row, "agent_snapshot"),
            agent_result_snapshot=_text(row, "agent_result_snapshot"),
        )


def clear_project_values(
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> dict[str, object]:
    """Return the legacy full clear while keeping field ownership explicit."""

    values = {**EXECUTION_CLEAR_VALUES, **PLANNING_CLEAR_VALUES}
    if processed_sha is not None:
        values["processed_sha"] = processed_sha
    if specification_fingerprint is not None:
        values["specification_fingerprint"] = specification_fingerprint
    if planning_fingerprint is not None:
        values["planning_fingerprint"] = planning_fingerprint
    return values


def _text(row: sqlite3.Row, field: str) -> str | None:
    value = row[field]
    return str(value) if value is not None else None


@dataclass(frozen=True)
class ExecutionSnapshot:
    """Normalized immutable view of one durable ExecPlan execution row."""

    repo: str
    plan_id: str
    path: str
    status: str
    catalog_generation: int
    current_revision_sha: str | None
    plan_sha: str | None
    active_sha: str | None
    validated_base_sha: str | None
    publication_remote_oid: str | None
    pr_conflict_recovery: int
    pr_conflict_head_oid: str | None
    pr_conflict_base_oid: str | None
    branch: str | None
    worktree: str | None
    pr_url: str | None
    pr_head_oid: str | None
    last_error: str | None
    worker_session_id: str | None
    worker_session_binding: str | None
    worker_session_transition: str | None
    reviewer_session_id: str | None
    worker_result: str | None
    verify_result: str | None
    review_result: str | None
    definition_snapshot: str | None
    worker_input_token: str | None
    archive_token: str | None
    verify_token: str | None
    review_token: str | None
    publication_token: str | None
    revision_resume_status: str | None
    workspace_owned: int
    ever_started: bool
    agent_role: str | None
    agent_snapshot: str | None
    agent_input_fingerprint: str | None
    agent_baseline: str | None
    agent_recovery: str | None
    agent_input_plan: str | None
    agent_result_plan: str | None
    agent_result_snapshot: str | None
    agent_manager_snapshot: str | None

    @property
    def phase(self) -> str:
        return project_phase_for_status(self.status)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ExecutionSnapshot":
        return cls(
            repo=str(row["repo"]),
            plan_id=str(row["plan_id"]),
            path=str(row["path"]),
            status=str(row["status"]),
            catalog_generation=int(row["catalog_generation"]),
            current_revision_sha=_text(row, "current_revision_sha"),
            plan_sha=_text(row, "plan_sha"),
            active_sha=_text(row, "active_sha"),
            validated_base_sha=_text(row, "validated_base_sha"),
            publication_remote_oid=_text(row, "publication_remote_oid"),
            pr_conflict_recovery=int(row["pr_conflict_recovery"]),
            pr_conflict_head_oid=_text(row, "pr_conflict_head_oid"),
            pr_conflict_base_oid=_text(row, "pr_conflict_base_oid"),
            branch=_text(row, "branch"),
            worktree=_text(row, "worktree"),
            pr_url=_text(row, "pr_url"),
            pr_head_oid=_text(row, "pr_head_oid"),
            last_error=_text(row, "last_error"),
            worker_session_id=_text(row, "worker_session_id"),
            worker_session_binding=_text(row, "worker_session_binding"),
            worker_session_transition=_text(row, "worker_session_transition"),
            reviewer_session_id=_text(row, "reviewer_session_id"),
            worker_result=_text(row, "worker_result"),
            verify_result=_text(row, "verify_result"),
            review_result=_text(row, "review_result"),
            definition_snapshot=_text(row, "definition_snapshot"),
            worker_input_token=_text(row, "worker_input_token"),
            archive_token=_text(row, "archive_token"),
            verify_token=_text(row, "verify_token"),
            review_token=_text(row, "review_token"),
            publication_token=_text(row, "publication_token"),
            revision_resume_status=_text(row, "revision_resume_status"),
            workspace_owned=int(row["workspace_owned"]),
            ever_started=bool(row["ever_started"]),
            agent_role=_text(row, "agent_role"),
            agent_snapshot=_text(row, "agent_snapshot"),
            agent_input_fingerprint=_text(row, "agent_input_fingerprint"),
            agent_baseline=_text(row, "agent_baseline"),
            agent_recovery=_text(row, "agent_recovery"),
            agent_input_plan=_text(row, "agent_input_plan"),
            agent_result_plan=_text(row, "agent_result_plan"),
            agent_result_snapshot=_text(row, "agent_result_snapshot"),
            agent_manager_snapshot=_text(row, "agent_manager_snapshot"),
        )

    def project_values(self) -> dict[str, object]:
        return {
            "active_sha": self.active_sha,
            "validated_base_sha": self.validated_base_sha,
            "publication_remote_oid": self.publication_remote_oid,
            "pr_conflict_recovery": self.pr_conflict_recovery,
            "pr_conflict_head_oid": self.pr_conflict_head_oid,
            "pr_conflict_base_oid": self.pr_conflict_base_oid,
            "plan_sha": self.plan_sha,
            "phase": self.phase,
            "branch": self.branch,
            "worktree": self.worktree,
            "plan_path": self.path,
            "pr_url": self.pr_url,
            "pr_head_oid": self.pr_head_oid,
            "last_error": self.last_error,
            "worker_session_id": self.worker_session_id,
            "worker_session_binding": self.worker_session_binding,
            "worker_session_transition": self.worker_session_transition,
            "reviewer_session_id": self.reviewer_session_id,
            "worker_result": self.worker_result,
            "verify_result": self.verify_result,
            "review_result": self.review_result,
            "definition_snapshot": self.definition_snapshot,
            "worker_input_token": self.worker_input_token,
            "archive_token": self.archive_token,
            "verify_token": self.verify_token,
            "review_token": self.review_token,
            "publication_token": self.publication_token,
            "revision_resume_status": self.revision_resume_status,
            "agent_role": self.agent_role,
            "agent_snapshot": self.agent_snapshot,
            "agent_input_fingerprint": self.agent_input_fingerprint,
            "agent_baseline": self.agent_baseline,
            "agent_recovery": self.agent_recovery,
            "agent_input_plan": self.agent_input_plan,
            "agent_result_plan": self.agent_result_plan,
            "agent_result_snapshot": self.agent_result_snapshot,
            "agent_manager_snapshot": self.agent_manager_snapshot,
        }


FaultHook = Callable[[str], None]


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Nest safely inside caller transactions without an intermediate commit."""

    from .db_access import CoordinatedConnection
    if isinstance(connection, CoordinatedConnection):
        with connection:
            yield
        return
    if not connection.in_transaction:
        # Reserve the writer before reading state. Two project workers using
        # deferred read transactions cannot both upgrade to a write lock;
        # SQLite rejects one immediately even with a busy timeout configured.
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return

    savepoint = f"runtime_state_{uuid4().hex}"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {savepoint}")
        connection.execute(f"RELEASE {savepoint}")
        raise
    else:
        connection.execute(f"RELEASE {savepoint}")


class RuntimeStateRepository:
    """Own ExecPlan runtime rows, their project projection, and outbox atomicity."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        fault_hook: FaultHook | None = None,
    ):
        self.connection = connection
        self._fault_hook = fault_hook

    def _fault(self, stage: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage)

    def scope_owner(self, repo: str, plan_id: str | None = None) -> Mapping[str, object] | sqlite3.Row | None:
        """Read saved Agent scope from its project or selected plan owner."""
        if plan_id is None:
            return project_row(self.connection, repo)
        return self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?", (repo, plan_id),
        ).fetchone()

    def scope_catalog_available(self) -> bool:
        """Keep pre-catalog scope recovery read-only until migration runs."""
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exec_plans'",
        ).fetchone() is not None

    @staticmethod
    def _validate_fields(values: Mapping[str, object], allowed: frozenset[str]) -> None:
        if not values or not set(values).issubset(allowed):
            raise ValueError("유효하지 않은 runtime 상태 필드입니다.")

    def _plan_row(self, repo: str, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            raise RuntimeStateError(f"ExecPlan runtime row를 찾을 수 없습니다: {plan_id}")
        return row

    def get(self, repo: str, plan_id: str) -> ExecutionSnapshot:
        return ExecutionSnapshot.from_row(self._plan_row(repo, plan_id))

    def planning(self, repo: str) -> PlanningSnapshot:
        """Read project planning state without exposing a mutable SQLite row."""

        row = self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if row is None:
            raise RuntimeStateError("planning 상태를 읽을 프로젝트가 없습니다.")
        return PlanningSnapshot.from_row(row)

    def suspend_planning(
        self, expected: PlanningSnapshot, *, unconfirmed_plan_ids: tuple[str, ...] | None = None,
    ) -> None:
        """Release only a checked failed attempt; retain its entire projection."""
        with _transaction(self.connection):
            if self.planning(expected.repo) != expected or not expected.planning_error:
                raise RuntimeStateError("planning 보존 입력이 변경됐습니다.")
            if (self.active_row(expected.repo) is not None and not expected.planning_resume_plan_id
                    or expected.phase not in (None, "PLANNING")):
                raise RuntimeStateError("다른 실행이 planning 슬롯을 사용하고 있습니다.")
            if expected.planning_preserved_state:
                raise RuntimeStateError("이미 보존된 planning 상태입니다.")
            row = self.connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (expected.repo,),
            ).fetchone()
            saved = {key: row[key] for key in (*EXECUTION_CLEAR_VALUES, *PLANNING_CLEAR_VALUES)}
            if unconfirmed_plan_ids is not None:
                saved["unconfirmed_plan_ids"] = list(unconfirmed_plan_ids)
            _preserved_planning_values(json.dumps(saved))
            values = {**EXECUTION_CLEAR_VALUES, **PLANNING_CLEAR_VALUES}
            # Errors remain visible to completion, notifications and registration.
            for key in PLANNING_CLEAR_VALUES:
                if key.startswith("planning_error"):
                    values.pop(key)
            values["planning_preserved_state"] = json.dumps(saved, sort_keys=True)
            self._update_project_row(expected.repo, values)
            self._fault("after_planning_suspend")

    def resume_planning(self, expected: PlanningSnapshot) -> None:
        """Restore the same attempt after the execution slot has become free."""
        with _transaction(self.connection):
            if (self.planning(expected.repo) != expected or not expected.planning_preserved_state
                    or (self.active_row(expected.repo) is not None and not expected.planning_resume_plan_id)
                    or expected.phase is not None):
                raise RuntimeStateError("planning 복구 슬롯 또는 입력이 변경됐습니다.")
            from loop_manager_internal.recovery.scoped_blocking import project_block
            if project_block(self.connection, expected.repo) is not None:
                raise RuntimeStateError("PROJECT_BLOCKED 중에는 planning을 복구할 수 없습니다.")
            values = _preserved_planning_values(expected.planning_preserved_state)
            values["planning_preserved_state"] = None
            self._update_project_row(expected.repo, values)
            self._fault("after_planning_resume")

    def recovery_slot_available(self, repo: str) -> bool:
        """Allow plan recovery to project or clear only after planning releases it."""

        row = self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        return row is not None and not row['planning_resume_plan_id'] and not planning_owns_execution_slot(row)

    def pause_execution_for_planning(self, expected: PlanningSnapshot, plan_id: str) -> None:
        with _transaction(self.connection):
            active = self.active(expected.repo)
            projected = self.projected(expected.repo)
            if (self.planning(expected.repo) != expected or active is None or projected != active
                    or active.plan_id != plan_id or active.pr_url or active.pr_conflict_recovery
                    or expected.planning_resume_plan_id):
                raise RuntimeStateError('planning 전환의 현재 작업 또는 소유권이 변경됐습니다.')
            # 실행 원본은 exec_plans에 그대로 두고 프로젝트 투영만 비운다.
            self._update_project_row(expected.repo, {
                **clear_project_values(), 'planning_resume_plan_id': plan_id,
            })
            self._fault('after_execution_planning_pause')

    def resume_execution_after_planning(self, repo: str) -> ExecutionSnapshot:
        with _transaction(self.connection):
            row = self.planning(repo)
            active = self.active(repo)
            independent = bool(active and row.planning_preserved_state
                and row.preserved_unconfirmed_plan_ids is not None
                and active.plan_id not in row.preserved_unconfirmed_plan_ids)
            if (row.phase is not None or ((row.planning_error or row.planning_preserved_state) and not independent)
                    or active is None or active.plan_id != row.planning_resume_plan_id):
                raise RuntimeStateError('planning 완료 또는 현재 작업의 identity를 확인할 수 없습니다.')
            self.hydrate_snapshot(repo, active)
            self._update_project_row(repo, {'planning_resume_plan_id': None})
            self._fault('after_execution_planning_resume')
            return active

    def active_row(self, repo: str) -> sqlite3.Row | None:
        selected = execution_selection.get()
        if selected is not None and selected[0] == repo:
            row = self._plan_row(*selected)
            return row if row["status"] in ACTIVE_STATUSES else None
        if admitting_execution.get():
            return None
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        rows = self.connection.execute(
            f"SELECT * FROM exec_plans WHERE repo = ? "
            f"AND status IN ({placeholders}) ORDER BY plan_id",
            (repo, *sorted(ACTIVE_STATUSES)),
        ).fetchall()
        return rows[0] if rows else None

    def active(self, repo: str) -> ExecutionSnapshot | None:
        row = self.active_row(repo)
        return ExecutionSnapshot.from_row(row) if row is not None else None

    def projected_row(self, repo: str) -> sqlite3.Row | None:
        selected = execution_selection.get()
        if selected is not None and selected[0] == repo:
            return self._plan_row(*selected) if include_blocked_projection.get() else self.active_row(repo)
        row = self.connection.execute(
            "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if row is None or not row["plan_path"]:
            return None
        return self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND path = ?",
            (repo, row["plan_path"]),
        ).fetchone()

    def projected(self, repo: str) -> ExecutionSnapshot | None:
        row = self.projected_row(repo)
        return ExecutionSnapshot.from_row(row) if row is not None else None

    def _update_project_row(self, repo: str, values: Mapping[str, object]) -> None:
        self._validate_fields(values, PROJECT_UPDATE_FIELDS)
        values = dict(values)
        selected = execution_selection.get()
        if selected is not None and selected[0] == repo:
            # 작업은 exec_plans만 바꾼다. 투영 해제로 독립 planning 기록도 지우지 않는다.
            return
        preserved = self.connection.execute(
            "SELECT planning_preserved_state FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if (preserved is not None and preserved[0]
                and "planning_preserved_state" not in values):
            # Execution projection cleanup cannot dispose of a suspended attempt.
            values = {key: value for key, value in values.items() if key not in PLANNING_CLEAR_VALUES}
        if not values:
            return
        if "planning_error" in values:
            if "planning_correction_error" not in values:
                values.update(
                    planning_correction_error=None,
                    planning_correction_incident_id=None,
                    planning_correction_sha=None,
                    planning_correction_context=None,
                )
            previous = self.connection.execute(
                "SELECT planning_error, planning_error_incident_id "
                "FROM projects WHERE repo = ?", (repo,),
            ).fetchone()
            if values["planning_error"] is None:
                values["planning_error_incident_id"] = None
                values["planning_error_origin"] = None
            elif (previous is None or previous["planning_error"] != values["planning_error"]
                  or not previous["planning_error_incident_id"]):
                values.setdefault("planning_error_incident_id", uuid4().hex)
                if previous is None or previous["planning_error"] != values["planning_error"]:
                    values.setdefault("planning_error_origin", None)
            if values["planning_error"] is None and previous is not None and previous[0]:
                notifications.release_block(self.connection, repo, "planning")
                self._fault("after_planning_block_release")
        assignments = ", ".join(f"{field} = ?" for field in values)
        changed = self.connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?",
            (*values.values(), repo),
        )
        if changed.rowcount != 1:
            raise RuntimeStateError("project runtime projection을 갱신할 수 없습니다.")

    def _update_plan_row(
        self,
        repo: str,
        plan_id: str,
        values: Mapping[str, object],
        *,
        expected_status: str | None = None,
    ) -> None:
        self._validate_fields(values, EXECPLAN_RUNTIME_FIELDS)
        if values.get("status") in ACTIVE_STATUSES:
            from .task_calls import management_available
            if not management_available(self.connection,repo,plan_id):
                raise TaskLimitReached("프로젝트 관리 대상 한도 때문에 작업을 재개할 수 없습니다.")
        if values.get("status") in ACTIVE_STATUSES or any(values.get(field) for field in (
                "branch", "worktree", "worker_session_id", "reviewer_session_id", "pr_url", "workspace_owned")):
            from .execplan_draft_state import pending_ids
            if plan_id in pending_ids(self.connection, repo):
                raise RuntimeStateError("사용자 판단 대기 draft ExecPlan을 실행할 수 없습니다.")
        assignments = ", ".join(f"{field} = ?" for field in values)
        expected = " AND status = ?" if expected_status is not None else ""
        parameters = [*values.values(), repo, plan_id]
        if expected_status is not None:
            parameters.append(expected_status)
        changed = self.connection.execute(
            f"UPDATE exec_plans SET {assignments} "
            f"WHERE repo = ? AND plan_id = ?{expected}",
            parameters,
        )
        if changed.rowcount != 1:
            raise RuntimeStateError("ExecPlan runtime 상태가 예상과 다릅니다.")

    def _enqueue(self, repo: str) -> None:
        self._fault("before_outbox_enqueue")
        notifications.enqueue_current_project_events(self.connection, repo)
        self._fault("after_outbox_enqueue")

    def update_project(
        self,
        repo: str,
        values: Mapping[str, object],
        *,
        enqueue: bool = True,
    ) -> None:
        """Update the compatibility row and mirror execution-owned fields."""

        if not values or not set(values).issubset(PROJECT_UPDATE_FIELDS):
            raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
        with _transaction(self.connection):
            project = self.connection.execute(
                "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            selected = execution_selection.get()
            if selected is not None and selected[0] == repo:
                project = {"plan_path": self.get(*selected).path}
            translated: dict[str, object] = {}
            if not ("phase" in values and values["phase"] is None):
                translated.update(
                    (field, values[field])
                    for field in MIRRORED_FIELDS
                    if field in values
                )
                if "phase" in values:
                    status = execplan_status_for_phase(values["phase"])
                    if status is not None:
                        translated["status"] = status
            plan_id = None
            if project is not None and project["plan_path"]:
                match = re.fullmatch(
                    r"docs/plans/\[(G-\d{3,})\] [^/]+\.md",
                    str(project["plan_path"]),
                )
                plan_id = match.group(1) if match else None
            if translated and plan_id is not None:
                if self.connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?',
                                           (repo, plan_id)).fetchone() is not None:
                    self._update_plan_row(repo, plan_id, translated)
                self._fault("after_execplan_update")
            self._update_project_row(repo, values)
            self._fault("after_project_update")
            if enqueue:
                self._enqueue(repo)

    def release_planning_correction(
        self, repo: str, *, error: str, active_sha: str, context: str,
    ) -> bool:
        """Release only the incident linked to this freshly validated result."""
        with _transaction(self.connection):
            row = self.connection.execute(
                "SELECT planning_error, planning_correction_error, "
                "planning_error_incident_id, planning_correction_incident_id, "
                "planning_correction_sha, planning_correction_context, "
                "active_sha, planning_result_completed FROM projects WHERE repo = ?",
                (repo,),
            ).fetchone()
            if row is None or not (
                row["planning_error"] == error
                and row["planning_correction_error"] == error
                and bool(row["planning_error_incident_id"])
                and row["planning_error_incident_id"] == row["planning_correction_incident_id"]
                and row["planning_correction_sha"] == active_sha
                and row["planning_correction_context"] == context
                and row["active_sha"] == active_sha
                and row["planning_result_completed"]
            ):
                return False
            self.update_project(repo, {
                "planning_error": None,
                "planning_error_fingerprint": None,
                "planning_correction_error": None,
                "planning_correction_incident_id": None,
                "planning_correction_sha": None,
                "planning_correction_context": None,
            })
            return True

    def complete_rebase(
        self, expected, checkpoint_repository, *,
        project_values: Mapping[str, object],
        completion: Mapping[str, object] | None = None,
        published_proof: Mapping[str, object] | None = None,
        acceptance: Mapping[str, object] | None = None,
        reobserve=None,
    ):
        """Atomically apply legacy proof or an explicitly rechecked current result.

        Existing callers retain their journal policy. Current-result acceptance
        requires a fresh decision and grants neither publication nor completion.
        """
        if acceptance is not None:
            if completion is not None or published_proof is not None:
                raise RuntimeStateError('current acceptance is not journal execution proof')
            return self._complete_current_rebase(
                expected, checkpoint_repository, acceptance=acceptance,
                project_values=project_values, reobserve=reobserve,
            )
        if completion is None:
            raise RuntimeStateError('rebase completion result is missing')
        repo, plan_id = expected.repo, expected.plan_id
        with _transaction(self.connection):
            current = checkpoint_repository.get(repo, plan_id)
            snapshot = current.snapshot or {}
            if current.operation is None:
                if (snapshot.get('rebase_completion') != dict(completion)
                        or (published_proof is not None
                            and snapshot.get('completed_rebase') != dict(published_proof))
                        or current.expected_head != completion['head']):
                    raise RuntimeStateError('rebase completion identity changed')
                return self.projected(repo)
            if (current.operation != 'rebase'
                    or current.expected_head != completion['before_head']
                    or current.target_base_oid != completion['target_base_oid']
                    or current.observed_remote_oid != completion['remote_oid']
                    or (completion.get('run_id') is not None and (
                        snapshot.get('rebase_run_id') != completion['run_id']
                        or current.generation < completion['intent_generation']
                    ))):
                raise RuntimeStateError('rebase checkpoint differs from sealed result')
            cleared = dict(snapshot)
            for key in ('pending', 'stage_evidence', 'worker_result',
                        'unmerged_index', 'after_head', 'stash_oid'):
                cleared.pop(key, None)
            cleared['rebase_completion'] = dict(completion)
            if published_proof is not None:
                cleared['completed_rebase'] = dict(published_proof)
            checkpoint_repository.record(
                repo, plan_id, expected_generation=current.generation,
                operation=None, expected_head=str(completion['head']),
                target_base_oid=current.target_base_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot=cleared,
            )
            plan_values = dict(project_values)
            if 'phase' in plan_values:
                plan_values['status'] = execplan_status_for_phase(plan_values.pop('phase'))
            return self.transition_from_snapshot(
                expected, plan_values=plan_values,
                project_values=project_values,
            )

    def _complete_current_rebase(
        self, expected, checkpoint_repository, *, acceptance, project_values, reobserve,
    ):
        from loop_manager_internal.git.managed_rebase_validation import RebaseDecision, RebaseStatus, valid_acceptance

        if (not valid_acceptance(acceptance) or not callable(reobserve)
                or checkpoint_repository.connection is not self.connection):
            raise RuntimeStateError('current rebase acceptance or recheck is missing')
        repo, plan_id = expected.repo, expected.plan_id
        if (acceptance['repo'] != repo or acceptance['plan_id'] != plan_id
                or acceptance['plan_path'] != expected.path
                or acceptance['branch'] != expected.branch
                or acceptance['worktree'] != expected.worktree
                or expected.workspace_owned != 1
                or (expected.publication_remote_oid is not None
                    and acceptance['remote'].get('oid') != expected.publication_remote_oid
                    and acceptance['remote'].get('oid') != expected.pr_conflict_head_oid)
                or acceptance['remote'].get('pull_request') != (
                    None if expected.pr_url is None else {
                        'identity': expected.pr_url,
                        'repository': acceptance['remote'].get('repository'),
                        'branch': expected.branch, 'head': expected.pr_head_oid,
                        'state': 'OPEN',
                    })):
            raise RuntimeStateError('current rebase execution ownership changed')
        values = dict(project_values)
        if (values.get('phase') not in {'WORKING', 'IMPLEMENTING'}
                and not (expected.status == 'PLAN_BLOCKED'
                         and values.get('phase') == expected.phase)):
            raise RuntimeStateError('current acceptance requires candidate revalidation')
        if ((expected.status == 'PLAN_BLOCKED' and values.get('phase') != expected.phase)
                or values.get('active_sha', acceptance['target_base_oid']) !=
                    acceptance['target_base_oid']):
            raise RuntimeStateError('current acceptance cannot release an execution block')
        invalidated = {
            'validated_base_sha', 'verify_result', 'review_result', 'verify_token',
            'review_token', 'publication_token', 'worker_result', 'worker_input_token',
            'archive_token',
        }
        if (not set(values) <= invalidated | {'phase', 'active_sha', 'last_error', 'revision_resume_status'}
                or any(values.get(key) is not None for key in invalidated | {
                    'last_error', 'revision_resume_status'})):
            raise RuntimeStateError('current acceptance cannot approve publication or clear blocks')
        if expected.status == 'PLAN_BLOCKED' and any(
                key in values for key in ('last_error', 'revision_resume_status')):
            raise RuntimeStateError('current acceptance cannot release an execution block')
        values.update({key: None for key in invalidated})
        values['active_sha'] = acceptance['target_base_oid']
        def check_owner():
            projection = project_row(self.connection, repo)
            fields = ('plan_path', 'branch', 'worktree', 'pr_url', 'pr_head_oid', 'phase')
            owner = (expected.path, expected.branch, expected.worktree,
                     expected.pr_url, expected.pr_head_oid, expected.phase)
            if (self.projected(repo) != expected or self.get(repo, plan_id) != expected
                    or projection is None or tuple(projection[key] for key in fields) != owner):
                raise RuntimeStateError('execution ownership or project projection changed')
        check_owner()
        current = checkpoint_repository.get(repo, plan_id)
        already_applied = current.current_rebase_acceptance() == dict(acceptance)
        if not already_applied and current.generation != acceptance['generation']:
            raise RuntimeStateError('current rebase acceptance generation is stale')
        decision = reobserve()
        if (not isinstance(decision, RebaseDecision)
                or decision.status is not RebaseStatus.COMPLETE
                or decision.acceptance != dict(acceptance)):
            raise RuntimeStateError('current rebase state or inputs changed after validation')
        with _transaction(self.connection):
            check_owner()
            if checkpoint_repository.get(repo, plan_id) != current:
                raise RuntimeStateError('checkpoint changed during rebase recheck')
            if already_applied:
                return expected
            checkpoint_repository.accept_current_rebase(
                repo, plan_id, acceptance=dict(acceptance), prior_artifacts={
                    key: getattr(expected, key) for key in (
                        'worker_result', 'worker_input_token', 'archive_token',
                        'verify_result', 'review_result', 'verify_token',
                        'review_token', 'publication_token', 'last_error',
                    )},
            )
            self._fault('after_rebase_acceptance')
            plan_values = dict(values)
            plan_values['status'] = execplan_status_for_phase(plan_values.pop('phase'))
            return self.transition_from_snapshot(
                expected, plan_values=plan_values, project_values=values,
            )

    def resume_rebase_block(self, expected_plan, expected_project, block, checkpoint_repository,
                            observation, *, expected_checkpoint, is_plan_block, reobserve, dependencies_ready,
                            pending_error):
        """Adopt current Git state and release one rebase cause atomically.

        No Git writes occur here. Other plan causes, sessions, failure history and
        planning ownership remain intact; every resumed candidate needs validation.
        """
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage import recovery_state
        from loop_manager_internal.git.managed_rebase_adoption import revalidation_values
        from loop_manager_internal.git.managed_rebase_validation import RebaseStatus, valid_acceptance

        repo, plan_id = expected_plan['repo'], expected_plan['plan_id']
        observed, decision = observation
        if decision.status not in {RebaseStatus.START, RebaseStatus.WAITING, RebaseStatus.COMPLETE}:
            raise RuntimeStateError('current rebase recovery is unsafe')
        if decision.status is RebaseStatus.COMPLETE and not valid_acceptance(decision.acceptance):
            raise RuntimeStateError('current rebase recovery acceptance is missing')
        current_checkpoint = checkpoint_repository.get(repo, plan_id)
        if current_checkpoint != expected_checkpoint:
            raise RuntimeStateError('rebase recovery checkpoint changed before release')
        saved = current_checkpoint.snapshot or {}
        prior_release = saved.get('rebase_block_release') or {}
        if (decision.status is RebaseStatus.WAITING
                and prior_release.get('reason_code') == block['reason_code']
                and saved.get('rebase_block_release_state') == observed.fingerprint):
            raise RuntimeStateError('unchanged pending rebase recovery was already resumed')
        def check_owner():
            project = project_row(self.connection, repo)
            current_block = (scoped_blocking.plan_block(self.connection, repo, plan_id)
                             if is_plan_block else scoped_blocking.project_block(self.connection, repo))
            active = self.active_row(repo)
            if (project is None or block['repo'] != repo or block['plan_id'] != plan_id
                    or dict(self._plan_row(repo, plan_id)) != dict(expected_plan)
                    or dict(project) != dict(expected_project)
                    or current_block is None or dict(current_block) != dict(block)
                    or planning_owns_execution_slot(project)
                    or (active is not None and active['plan_id'] != plan_id)
                    or (is_plan_block and scoped_blocking.project_block(self.connection, repo) is not None)
                    or not dependencies_ready()
                    or checkpoint_repository.get(repo, plan_id) != current_checkpoint):
                raise RuntimeStateError('rebase recovery owner, cause or execution slot changed')
        check_owner()
        if reobserve() != observation:
            raise RuntimeStateError('current rebase state changed during block release')
        with _transaction(self.connection):
            check_owner()
            separate_block = not is_plan_block and expected_plan['status'] == 'PLAN_BLOCKED'
            values = revalidation_values(current_checkpoint.target_base_oid)
            retired_scope = scoped_blocking.is_scope_baseline_block(block)
            if retired_scope:
                values['agent_recovery'] = None
            phase = values.pop('phase')
            values['status'] = 'PLAN_BLOCKED' if separate_block else 'IMPLEMENTING'
            if separate_block:
                values.pop('last_error')
                values.pop('revision_resume_status')
                # Retain outstanding feedback; its tokens no longer validate
                # this candidate. Adoption also stores previous artifacts.
                values.pop('review_result')
                values.pop('verify_result')
                phase = 'PLAN_BLOCKED'
            elif decision.status is not RebaseStatus.COMPLETE:
                values['last_error'] = pending_error
            if decision.status is RebaseStatus.COMPLETE:
                if current_checkpoint.generation != decision.acceptance['generation']:
                    raise RuntimeStateError('current rebase recovery generation changed')
                checkpoint_repository.accept_current_rebase(
                    repo, plan_id, acceptance=decision.acceptance,
                    prior_artifacts={key: expected_plan[key] for key in (
                        'worker_result', 'worker_input_token', 'verify_result', 'review_result',
                        'verify_token', 'review_token', 'publication_token', 'last_error')}
                        | {'released_rebase_block': dict(block),
                           'prior_agent_recovery': expected_plan['agent_recovery']})
                self._fault('after_rebase_acceptance')
            else:
                checkpoint_repository.record(
                    repo, plan_id, expected_generation=current_checkpoint.generation,
                    operation=current_checkpoint.operation, expected_head=current_checkpoint.expected_head,
                    target_base_oid=current_checkpoint.target_base_oid,
                    observed_remote_oid=current_checkpoint.observed_remote_oid,
                    snapshot={**(current_checkpoint.snapshot or {}),
                              'rebase_block_release': dict(block),
                              'rebase_block_release_state': observed.fingerprint,
                              'rebase_block_recovery_audit': [
                                  *(current_checkpoint.snapshot or {}).get('rebase_block_recovery_audit', ()),
                                  {'block': dict(block), 'prior_agent_recovery': expected_plan['agent_recovery'],
                                   'prior_artifacts': {key: expected_plan[key] for key in (
                                      'worker_result', 'verify_result', 'review_result', 'last_error')}}]})
            scoped_blocking.clear_revalidated_rebase_block(
                self.connection, block, is_plan_block=is_plan_block,
                acceptance=decision.acceptance if decision.status is RebaseStatus.COMPLETE else None,
                pending_work=bool(observed.files), pending_target=(
                    current_checkpoint.target_base_oid if decision.status is RebaseStatus.WAITING else None))
            self._fault('after_rebase_block_release')
            recovery_state.RecoveryRepository(self.connection).resolve_rebase_block(
                repo, plan_id, block)
            if retired_scope:
                recovery_state.RecoveryRepository(self.connection).resolve_git_scope_failure(repo, plan_id)
                recovery_state.RecoveryRepository(self.connection).resolve_git_scope_failure(repo, None)
            if is_plan_block:
                values.update(blocked_reason=None, blocked_fingerprint=None,
                              blocked_worktree_fingerprint=None, blocked_review_fingerprint=None)
            projection = ExecutionSnapshot.from_row(expected_plan).project_values()
            projection.update({key: value for key, value in values.items() if key in MIRRORED_FIELDS})
            projection['phase'] = phase
            return self.transition(repo, plan_id, plan_values=values,
                                   project_values=projection, enqueue=False)

    def mirror_project_values(
        self,
        repo: str,
        plan_path: str | None,
        values: Mapping[str, object],
    ) -> None:
        """Compatibility entry point for the former mirror-only helper."""

        if not values or not set(values).issubset(PROJECT_UPDATE_FIELDS):
            raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
        if not plan_path or ("phase" in values and values["phase"] is None):
            return
        match = re.fullmatch(
            r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", plan_path
        )
        if match is None:
            return
        translated = {
            field: values[field] for field in MIRRORED_FIELDS if field in values
        }
        if "phase" in values:
            status = execplan_status_for_phase(values["phase"])
            if status is not None:
                translated["status"] = status
        if not translated:
            return
        with _transaction(self.connection):
            if self.connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?',
                                       (repo, match.group(1))).fetchone() is not None:
                self._update_plan_row(repo, match.group(1), translated)
            self._fault("after_execplan_update")

    def update_plan(
        self,
        repo: str,
        plan_id: str,
        *,
        plan_values: Mapping[str, object],
        expected_status: str | None = None,
        enqueue: bool = True,
    ) -> ExecutionSnapshot:
        """Update source-only runtime fields for a non-projected plan."""

        with _transaction(self.connection):
            self._update_plan_row(
                repo, plan_id, plan_values, expected_status=expected_status
            )
            self._fault("after_execplan_update")
            if enqueue:
                self._enqueue(repo)
            return self.get(repo, plan_id)

    def reserve_workspace(
        self,
        repo: str,
        plan_id: str,
        *,
        reserved_value: int,
        allowed_values: tuple[int, ...],
    ) -> ExecutionSnapshot:
        """Atomically reserve a plan workspace from an allowed ownership state."""

        if not allowed_values:
            raise ValueError("workspace 예약 허용 상태가 비어 있습니다.")
        placeholders = ", ".join("?" for _ in allowed_values)
        with _transaction(self.connection):
            from .execplan_draft_state import pending_ids
            if plan_id in pending_ids(self.connection, repo):
                raise RuntimeStateError("사용자 판단 대기 draft ExecPlan의 workspace를 예약할 수 없습니다.")
            changed = self.connection.execute(
                "UPDATE exec_plans SET workspace_owned = ? "
                "WHERE repo = ? AND plan_id = ? "
                f"AND workspace_owned IN ({placeholders})",
                (reserved_value, repo, plan_id, *allowed_values),
            )
            if changed.rowcount != 1:
                raise RuntimeStateError("ExecPlan workspace를 예약할 수 없습니다.")
            self._fault("after_execplan_update")
            return self.get(repo, plan_id)

    def transition(
        self,
        repo: str,
        plan_id: str,
        *,
        plan_values: Mapping[str, object],
        project_values: Mapping[str, object],
        expected_status: str | None = None,
        enqueue: bool = True,
    ) -> ExecutionSnapshot:
        """Apply a decided source/projection transition in one savepoint."""

        with _transaction(self.connection):
            if plan_values.get("status") in ACTIVE_STATUSES:
                from .work_claims import publication_claims
                if plan_id in publication_claims(self.connection, repo)[0]:
                    raise RuntimeStateError(f"{plan_id}: Planner 게시가 계획을 소유 예약 중입니다.")
                from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
                if plan_id in catalog_held_plan_ids(self.connection, repo):
                    raise RuntimeStateError("catalog 오류로 보류된 ExecPlan을 실행할 수 없습니다.")
            self._update_plan_row(
                repo, plan_id, plan_values, expected_status=expected_status
            )
            self._fault("after_execplan_update")
            self._update_project_row(repo, project_values)
            self._fault("after_project_update")
            if enqueue:
                self._enqueue(repo)
            return self.get(repo, plan_id)

    def transition_from_snapshot(
        self,
        expected: ExecutionSnapshot,
        *,
        plan_values: Mapping[str, object],
        project_values: Mapping[str, object],
    ) -> ExecutionSnapshot:
        """Reject stale phase observations and switched project projections."""
        with _transaction(self.connection):
            if self.projected(expected.repo) != expected:
                raise RuntimeStateError("execution snapshot이 변경되었습니다.")
            return self.transition(
                expected.repo,
                expected.plan_id,
                expected_status=expected.status,
                plan_values=plan_values,
                project_values=project_values,
            )

    def observe_worker_session(
        self, expected: ExecutionSnapshot, transition_id: str | None,
        session_id: str, binding: str,
    ) -> ExecutionSnapshot:
        """Bind a provider observation to exactly the intended Worker transition."""
        from loop_manager_internal.execution.worker_session import SessionTransition

        pending = SessionTransition.deserialize(expected.worker_session_transition)
        if (pending.identity if pending else None) != transition_id:
            raise RuntimeStateError("Worker session transition identity가 변경되었습니다.")
        if pending is not None and pending.target.serialize() != binding:
            raise RuntimeStateError("Worker session 목표 profile이 변경되었습니다.")
        values = {
            "worker_session_id": session_id,
            "worker_session_binding": binding,
            "worker_session_transition": None,
        }
        return self.transition_from_snapshot(
            expected, plan_values=values, project_values=values,
        )

    def correct_checkpoint(
        self, expected: ExecutionSnapshot, *, phase: str,
        active_sha: str | None, validated_base_sha: str | None,
        invalidate_validation: bool,
        expected_catalog: tuple[int, str | None] | None = None,
    ) -> ExecutionSnapshot:
        """Apply an already assessed correction against the complete row snapshot."""
        if phase not in ACTIVE_STATUSES:
            raise ValueError("invalid checkpoint phase")
        if expected_catalog is not None and (
            expected.catalog_generation, expected.current_revision_sha
        ) != expected_catalog:
            raise RuntimeStateError("checkpoint catalog revision이 변경되었습니다.")
        values: dict[str, object] = {
            "phase": project_phase_for_status(phase),
            "active_sha": active_sha,
            "validated_base_sha": validated_base_sha,
            "last_error": None,
            "revision_resume_status": None,
        }
        if invalidate_validation:
            values.update(
                verify_result=None, review_result=None, verify_token=None,
                review_token=None, publication_token=None,
            )
        return self.transition_from_snapshot(
            expected,
            plan_values={
                **{key: value for key, value in values.items() if key != "phase"},
                "status": phase,
            },
            project_values=values,
        )

    def activate(self, repo: str, plan_id: str, *, activation_sha: str,
                 branch: str, worktree: str, preserve_worker_session: bool = False) -> ExecutionSnapshot:
        from loop_manager_internal.storage import verify_receipts
        with _transaction(self.connection):
            result = self._activate(repo, plan_id, activation_sha=activation_sha,
                                    branch=branch, worktree=worktree,
                                    preserve_worker_session=preserve_worker_session)
            if not preserve_worker_session or verify_receipts.selected(self.connection, repo, plan_id) is None:
                verify_receipts.start_cycle(self.connection, repo, plan_id)
        return result

    def _activate(
        self,
        repo: str,
        plan_id: str,
        *,
        activation_sha: str,
        branch: str,
        worktree: str,
        preserve_worker_session: bool = False,
    ) -> ExecutionSnapshot:
        from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
        if plan_id in catalog_held_plan_ids(self.connection, repo):
            raise RuntimeStateError("catalog 오류로 보류된 ExecPlan을 activate할 수 없습니다.")
        selected = self.get(repo, plan_id)
        from loop_manager_internal.config.managed_config import fast_mode_settings
        from loop_manager_internal.runtime.project_registry import task_limit
        from loop_manager_internal.storage.task_calls import managed_count
        config = fast_mode_settings.get()
        _, limit, _ = task_limit(self.connection, repo, config.max_tasks_per_project if config else 1)
        if managed_count(self.connection, repo) >= limit:
            raise TaskLimitReached("프로젝트 관리 대상 한도 때문에 새 작업을 배정할 수 없습니다.")
        # Maintenance deferral resumes an owned execution; ordinary activation
        # still discards stale session state from a newly queued declaration.
        worker_session_values = {
            field: getattr(selected, field) if preserve_worker_session else None
            for field in (
                "worker_session_id", "worker_session_binding", "worker_session_transition",
            )
        }
        return self.transition(
            repo,
            plan_id,
            expected_status="READY",
            plan_values={
                "status": "IMPLEMENTING",
                "ever_started": 1,
                "active_sha": activation_sha,
                "branch": branch,
                "worktree": worktree,
                "last_error": None,
                **worker_session_values,
                "definition_snapshot": None,
                "worker_input_token": None,
                "archive_token": None,
                "verify_token": None,
                "review_token": None,
                "publication_token": None,
                "revision_resume_status": None,
            },
            project_values={
                "active_sha": activation_sha,
                "validated_base_sha": None,
                "publication_remote_oid": selected.publication_remote_oid,
                "pr_conflict_recovery": 0,
                "pr_conflict_head_oid": None,
                "pr_conflict_base_oid": None,
                "plan_sha": selected.plan_sha,
                "phase": "WORKING",
                "branch": branch,
                "worktree": worktree,
                "plan_path": selected.path,
                "pr_url": None,
                "pr_head_oid": None,
                "last_error": None,
                **worker_session_values,
                "reviewer_session_id": None,
                "worker_result": None,
                "verify_result": None,
                "review_result": None,
                "definition_snapshot": None,
                "worker_input_token": None,
                "archive_token": None,
                "verify_token": None,
                "review_token": None,
                "publication_token": None,
                "revision_resume_status": None,
            },
            enqueue=False,
        )

    def hydrate(self, repo: str, plan_id: str) -> ExecutionSnapshot:
        snapshot = self.get(repo, plan_id)
        return self.hydrate_snapshot(repo, snapshot)

    def hydrate_snapshot(
        self, repo: str, snapshot: ExecutionSnapshot
    ) -> ExecutionSnapshot:
        if snapshot.repo != repo:
            raise RuntimeStateError("hydrate 대상 프로젝트와 snapshot이 다릅니다.")
        from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
        with _transaction(self.connection):
            if snapshot.plan_id in catalog_held_plan_ids(self.connection, repo):
                raise RuntimeStateError("catalog 오류로 보류된 ExecPlan을 hydrate할 수 없습니다.")
            self._update_project_row(repo, snapshot.project_values())
            self._fault("after_project_update")
        return snapshot

    def clear(
        self,
        repo: str,
        *,
        processed_sha: str | None = None,
        specification_fingerprint: str | None = None,
        planning_fingerprint: str | None = None,
        enqueue: bool = False,
    ) -> None:
        values = clear_project_values(
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )
        with _transaction(self.connection):
            self._update_project_row(repo, values)
            self._fault("after_project_update")
            if enqueue:
                self._enqueue(repo)

    def transition_and_clear(
        self,
        repo: str,
        plan_id: str,
        *,
        plan_values: Mapping[str, object],
        expected_status: str | None = None,
        enqueue: bool = True,
    ) -> ExecutionSnapshot:
        return self.transition(
            repo,
            plan_id,
            plan_values=plan_values,
            project_values=clear_project_values(),
            expected_status=expected_status,
            enqueue=enqueue,
        )

    def open_pull_request(
        self,
        repo: str,
        *,
        new_pr: bool = False,
        pr_url: str | None = None,
        pr_head_oid: str | None = None,
        publication_remote_oid: str | None = None,
        pr_conflict_recovery: int | None = None,
        pr_conflict_head_oid: str | None = None,
        pr_conflict_base_oid: str | None = None,
        manual_stage_tokens: Mapping[str, str] | None = None,
    ) -> ExecutionSnapshot:
        snapshot = self.projected(repo)
        if snapshot is None:
            raise RuntimeStateError("PR_OPEN으로 전이할 projection ExecPlan이 없습니다.")
        values: dict[str, object] = {
            "status": "PR_OPEN",
            "last_error": None,
        }
        if pr_url is not None:
            values["pr_url"] = pr_url
        if pr_head_oid is not None:
            values["pr_head_oid"] = pr_head_oid
        if publication_remote_oid is not None:
            values["publication_remote_oid"] = publication_remote_oid
        if pr_conflict_recovery is not None:
            values["pr_conflict_recovery"] = pr_conflict_recovery
            values["pr_conflict_head_oid"] = pr_conflict_head_oid
            values["pr_conflict_base_oid"] = pr_conflict_base_oid
        with _transaction(self.connection):
            if new_pr:
                from loop_manager_internal.notifications import notifications
                notifications.record_pr_creation(self.connection, repo, snapshot.plan_id, pr_url)
            result = self.transition_and_clear(repo, snapshot.plan_id, plan_values=values)
            from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
            ManualWorkRepository(self.connection).publish_group(repo,snapshot.plan_id,
                {key:getattr(result,key) for key in ('pr_url','pr_head_oid','publication_remote_oid')},
                stage_tokens=manual_stage_tokens)
            return result

    def mark_merged(
        self,
        repo: str,
        plan_id: str,
        *,
        project_values: Mapping[str, object] | None = None,
        enqueue: bool = False,
        pr_url: str | None = None,
        pr_head_oid: str | None = None,
    ) -> ExecutionSnapshot:
        values: dict[str, object] = {
            "status": "MERGED",
            "last_error": None,
            "pr_conflict_recovery": 0,
            "pr_conflict_head_oid": None,
            "pr_conflict_base_oid": None,
        }
        if pr_url is not None:
            values.update(pr_url=pr_url, pr_head_oid=pr_head_oid)
        if project_values:
            return self.transition(
                repo,
                plan_id,
                plan_values=values,
                project_values=project_values,
                enqueue=enqueue,
            )
        return self.update_plan(
            repo,
            plan_id,
            plan_values=values,
            enqueue=enqueue,
        )

    def block_plan(
        self,
        repo: str,
        plan_id: str,
        *,
        reason_code: str,
        evidence: str,
        release_fingerprint: str | None,
        plan_values: Mapping[str, object] | None = None,
        project_values: Mapping[str, object] | None = None,
        clear_projection: bool = False,
        expected_status: str | None = None,
        enqueue: bool = True,
    ) -> ExecutionSnapshot:
        values = dict(plan_values or {})
        values.update(
            status="PLAN_BLOCKED",
            blocked_reason=reason_code,
            last_error=evidence,
        )
        with _transaction(self.connection):
            prior_status = str(self._plan_row(repo, plan_id)['status'])
            self._update_plan_row(
                repo, plan_id, values, expected_status=expected_status
            )
            self._fault("after_execplan_update")
            # Keep the final proof with its plan block: a project recovery may
            # subsequently replace the shared per-plan recovery projection.
            recovery_snapshot = None
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'").fetchone():
                recovery = self.connection.execute(
                    "SELECT * FROM recovery_records WHERE repo=? AND subject=? AND pending_action='final_block' AND detail=?",
                    (repo, plan_id, evidence)).fetchone()
                if recovery is not None:
                    try:
                        final = json.loads(recovery['final_evidence'])
                    except (TypeError, ValueError):
                        final = None
                    if isinstance(final, dict) and final.get('block_reason_code') == reason_code:
                        recovery_snapshot = json.dumps({key: recovery[key] for key in (
                            'identity', 'reason_code', 'phase', 'plan_revision', 'base_revision',
                            'fingerprint', 'progress_marker', 'detail', 'pending_action', 'final_evidence')})
            self.connection.execute(
                """
                INSERT INTO execplan_blocks (
                    repo, plan_id, scope, reason_code, evidence, release_fingerprint, recovery_snapshot
                ) VALUES (?, ?, 'plan', ?, ?, ?, ?)
                ON CONFLICT(repo, plan_id) DO UPDATE SET
                    scope = excluded.scope,
                    reason_code = excluded.reason_code,
                    evidence = excluded.evidence,
                    release_fingerprint = excluded.release_fingerprint,
                    recovery_snapshot = excluded.recovery_snapshot,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, plan_id, reason_code, evidence, release_fingerprint, recovery_snapshot),
            )
            checkpoint_plan = self._plan_row(repo, plan_id)
            blocked_checkpoint_state.BlockedCheckpointRepository(
                self.connection
            ).request(
                repo=repo,
                plan_id=plan_id,
                reason_code=reason_code,
                evidence=evidence,
                branch=_text(checkpoint_plan, "branch"),
                worktree=_text(checkpoint_plan, "worktree"),
                expected_remote_oid=(_text(checkpoint_plan, "publication_remote_oid")
                                     or _text(checkpoint_plan, "pr_head_oid")),
                rebase_recovery=bool(
                    checkpoint_plan["publication_remote_oid"] or checkpoint_plan["pr_head_oid"]
                ),
                release_fingerprint=release_fingerprint,
            )
            # Resolve the checkpoint module at the transition boundary; its
            # recovery types also import runtime state during startup.
            from loop_manager_internal.storage import execution_checkpoint_state
            execution_checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection
            )
            current = execution_checkpoint.get(repo, plan_id)
            from loop_manager_internal.recovery.scoped_blocking import SCOPE_BASELINE_REASONS
            if reason_code in SCOPE_BASELINE_REASONS and prior_status in ACTIVE_STATUSES:
                current = execution_checkpoint.record(
                    repo, plan_id, expected_generation=current.generation,
                    operation=current.operation, expected_head=current.expected_head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot={**(current.snapshot or {}), 'scope_block_resume_status': prior_status},
                )
            if current.snapshot and any(key in current.snapshot for key in (
                    'blocked_resume', 'published_review_resume')):
                snapshot = dict(current.snapshot)
                snapshot.pop('blocked_resume', None)
                snapshot.pop('published_review_resume', None)
                execution_checkpoint.record(
                    repo, plan_id, expected_generation=current.generation,
                    operation=current.operation, expected_head=current.expected_head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot=snapshot,
                )
            projection = (
                clear_project_values() if clear_projection else project_values
            )
            if projection is not None:
                self._update_project_row(repo, projection)
                self._fault("after_project_update")
            # PLAN_BLOCKED notification is intentionally delayed until the
            # durable checkpoint dispatcher records published/local-only.
            return self.get(repo, plan_id)
