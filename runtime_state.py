"""Transactional ExecPlan runtime state and compatibility projection boundary."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
import sqlite3
from typing import Callable, Iterator, Mapping
from uuid import uuid4

import notifications


ACTIVE_STATUSES = frozenset(
    {"IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING"}
)

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
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
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
        "planning_fingerprint",
        "planning_attempt_fingerprint",
        "planning_workspace_owned",
        "planning_retry_count",
        "planner_session_id",
        "planning_validation_error",
        "planning_result_pending_validation",
        "planning_result_completed",
        "planning_result",
        "planning_error",
        "planning_error_fingerprint",
    }
)

PROJECT_UPDATE_FIELDS = frozenset(MIRRORED_FIELDS) | PROJECT_ONLY_FIELDS | {
    "phase",
    "plan_path",
}

EXECPLAN_RUNTIME_FIELDS = frozenset(MIRRORED_FIELDS) | {
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
    "blocked_reason",
    "guidance_plan_hash",
    "workspace_owned",
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
    "reviewer_session_id": None,
    "worker_result": None,
    "verify_result": None,
    "review_result": None,
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


def project_phase_for_status(status: str) -> str:
    """Translate the source status to its stored compatibility phase."""

    return COMPATIBILITY_PHASE_BY_STATUS.get(status, status)


def execplan_status_for_phase(phase: object) -> str | None:
    """Translate a compatibility phase without projecting planning state."""

    if phase is None or phase == "PLANNING":
        return None
    normalized = str(phase)
    return COMPATIBILITY_STATUS_BY_PHASE.get(normalized, normalized)


def clear_project_values(
    *,
    processed_sha: str | None = None,
    planning_fingerprint: str | None = None,
) -> dict[str, object]:
    """Return the legacy full clear while keeping field ownership explicit."""

    values = {**EXECUTION_CLEAR_VALUES, **PLANNING_CLEAR_VALUES}
    if processed_sha is not None:
        values["processed_sha"] = processed_sha
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
    reviewer_session_id: str | None
    worker_result: str | None
    verify_result: str | None
    review_result: str | None
    workspace_owned: int
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
            reviewer_session_id=_text(row, "reviewer_session_id"),
            worker_result=_text(row, "worker_result"),
            verify_result=_text(row, "verify_result"),
            review_result=_text(row, "review_result"),
            workspace_owned=int(row["workspace_owned"]),
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
            "reviewer_session_id": self.reviewer_session_id,
            "worker_result": self.worker_result,
            "verify_result": self.verify_result,
            "review_result": self.review_result,
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

    def active_row(self, repo: str) -> sqlite3.Row | None:
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        rows = self.connection.execute(
            f"SELECT * FROM exec_plans WHERE repo = ? "
            f"AND status IN ({placeholders}) ORDER BY plan_id",
            (repo, *sorted(ACTIVE_STATUSES)),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeStateError("활성 ExecPlan runtime row가 둘 이상입니다.")
        return rows[0] if rows else None

    def active(self, repo: str) -> ExecutionSnapshot | None:
        row = self.active_row(repo)
        return ExecutionSnapshot.from_row(row) if row is not None else None

    def projected_row(self, repo: str) -> sqlite3.Row | None:
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
                assignments = ", ".join(f"{field} = ?" for field in translated)
                self.connection.execute(
                    f"UPDATE exec_plans SET {assignments} "
                    "WHERE repo = ? AND plan_id = ?",
                    (*translated.values(), repo, plan_id),
                )
                self._fault("after_execplan_update")
            self._update_project_row(repo, values)
            self._fault("after_project_update")
            if enqueue:
                self._enqueue(repo)

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
            assignments = ", ".join(f"{field} = ?" for field in translated)
            self.connection.execute(
                f"UPDATE exec_plans SET {assignments} "
                "WHERE repo = ? AND plan_id = ?",
                (*translated.values(), repo, match.group(1)),
            )
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
            self._update_plan_row(
                repo, plan_id, plan_values, expected_status=expected_status
            )
            self._fault("after_execplan_update")
            self._update_project_row(repo, project_values)
            self._fault("after_project_update")
            if enqueue:
                self._enqueue(repo)
            return self.get(repo, plan_id)

    def activate(
        self,
        repo: str,
        plan_id: str,
        *,
        activation_sha: str,
        branch: str,
        worktree: str,
    ) -> ExecutionSnapshot:
        selected = self.get(repo, plan_id)
        return self.transition(
            repo,
            plan_id,
            expected_status="READY",
            plan_values={
                "status": "IMPLEMENTING",
                "active_sha": activation_sha,
                "branch": branch,
                "worktree": worktree,
                "last_error": None,
            },
            project_values={
                "active_sha": activation_sha,
                "validated_base_sha": None,
                "publication_remote_oid": None,
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
                "worker_session_id": None,
                "reviewer_session_id": None,
                "worker_result": None,
                "verify_result": None,
                "review_result": None,
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
        with _transaction(self.connection):
            self._update_project_row(repo, snapshot.project_values())
            self._fault("after_project_update")
        return snapshot

    def clear(
        self,
        repo: str,
        *,
        processed_sha: str | None = None,
        planning_fingerprint: str | None = None,
        enqueue: bool = False,
    ) -> None:
        values = clear_project_values(
            processed_sha=processed_sha,
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
        pr_url: str | None = None,
        pr_head_oid: str | None = None,
        publication_remote_oid: str | None = None,
        pr_conflict_recovery: int | None = None,
        pr_conflict_head_oid: str | None = None,
        pr_conflict_base_oid: str | None = None,
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
        return self.transition_and_clear(
            repo,
            snapshot.plan_id,
            plan_values=values,
        )

    def mark_merged(
        self,
        repo: str,
        plan_id: str,
        *,
        project_values: Mapping[str, object] | None = None,
        enqueue: bool = False,
    ) -> ExecutionSnapshot:
        values: dict[str, object] = {
            "status": "MERGED",
            "last_error": None,
            "pr_conflict_recovery": 0,
            "pr_conflict_head_oid": None,
            "pr_conflict_base_oid": None,
        }
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
            self._update_plan_row(
                repo, plan_id, values, expected_status=expected_status
            )
            self._fault("after_execplan_update")
            self.connection.execute(
                """
                INSERT INTO execplan_blocks (
                    repo, plan_id, scope, reason_code, evidence, release_fingerprint
                ) VALUES (?, ?, 'plan', ?, ?, ?)
                ON CONFLICT(repo, plan_id) DO UPDATE SET
                    scope = excluded.scope,
                    reason_code = excluded.reason_code,
                    evidence = excluded.evidence,
                    release_fingerprint = excluded.release_fingerprint,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, plan_id, reason_code, evidence, release_fingerprint),
            )
            projection = (
                clear_project_values() if clear_projection else project_values
            )
            if projection is not None:
                self._update_project_row(repo, projection)
                self._fault("after_project_update")
            if enqueue:
                self._enqueue(repo)
            return self.get(repo, plan_id)
