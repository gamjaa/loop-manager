"""Durable state and bounded Git context for maintenance review cycles."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Mapping, Sequence
from uuid import uuid4

from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, verify_remote_revision


class MaintenanceStateError(RuntimeError):
    """Raised when persisted maintenance state cannot be used safely."""


class MaintenancePhase(str, Enum):
    IDLE = "idle"
    ASSESSMENT = "assessment"
    PREREQUISITE_WAIT = "prerequisite_wait"
    REFACTORING = "refactoring"
    POST_REFACTOR_REVALIDATION = "post_refactor_revalidation"


MAINTENANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_cycles (
    repo TEXT PRIMARY KEY,
    last_success_commit TEXT,
    last_success_merged_count INTEGER,
    file_assessment_start_merged_count INTEGER,
    last_success_file_assessment_merged_count INTEGER,
    phase TEXT NOT NULL DEFAULT 'idle',
    cycle_base_sha TEXT,
    cycle_start_merged_count INTEGER,
    prerequisite_plan_ids TEXT NOT NULL DEFAULT '[]',
    refactoring_plan_ids TEXT NOT NULL DEFAULT '[]',
    revalidation_plan_ids TEXT NOT NULL DEFAULT '[]',
    publication_stage TEXT,
    publication_base_sha TEXT,
    publication_commit_sha TEXT,
    publication_remote_oid TEXT,
    publication_refactoring_plan_ids TEXT NOT NULL DEFAULT '[]',
    publication_revalidation_plan_ids TEXT NOT NULL DEFAULT '[]',
    publication_id_allocations TEXT NOT NULL DEFAULT '[]',
    include_general_planning INTEGER NOT NULL DEFAULT 0,
    full_assessment_completed INTEGER NOT NULL DEFAULT 0,
    full_assessment_source_sha TEXT,
    full_assessment_result TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

DOCUMENT_DRAFTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_drafts (
    draft_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    finding_ids TEXT NOT NULL,
    target_path TEXT NOT NULL,
    assessment_revision TEXT NOT NULL,
    original_revision TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    original_content BLOB NOT NULL,
    proposed_hash TEXT NOT NULL,
    proposed_content BLOB NOT NULL,
    complete_diff TEXT NOT NULL,
    source_relationships TEXT NOT NULL,
    meaning_evidence TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'rejected', 'stale')),
    requested_decision TEXT,
    decision_revision TEXT,
    decision_reason TEXT,
    plan_id TEXT,
    publication_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


@dataclass(frozen=True)
class MaintenanceCycle:
    repo: str
    last_success_commit: str | None
    last_success_merged_count: int | None
    file_assessment_start_merged_count: int | None
    last_success_file_assessment_merged_count: int | None
    phase: MaintenancePhase
    cycle_base_sha: str | None
    cycle_start_merged_count: int | None
    prerequisite_plan_ids: tuple[str, ...]
    refactoring_plan_ids: tuple[str, ...]
    revalidation_plan_ids: tuple[str, ...]
    publication_stage: str | None
    publication_base_sha: str | None
    publication_commit_sha: str | None
    publication_remote_oid: str | None
    publication_refactoring_plan_ids: tuple[str, ...]
    publication_revalidation_plan_ids: tuple[str, ...]
    include_general_planning: bool
    full_assessment_completed: bool
    full_assessment_source_sha: str | None
    full_assessment_result: str | None
    revision: int
    last_cycle_commit: str | None = None
    last_cycle_merged_count: int | None = None
    cycle_request_id: str | None = None
    consumed_request_id: str | None = None
    assessment_plan_ids: tuple[str, ...] = ()
    cycle_id: str | None = None
    legacy_release_identity: str | None = None
    publication_id_allocations: tuple[tuple[str, str, str, str], ...] = ()


@dataclass(frozen=True)
class ReviewDue:
    required: bool
    reason: str
    merged_since_baseline: int
    active_phase: MaintenancePhase


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the additive maintenance table without rewriting audit data."""

    connection.execute(MAINTENANCE_SCHEMA)
    connection.execute(DOCUMENT_DRAFTS_SCHEMA)
    maintenance_file_ledger.ensure_schema(connection)
    maintenance_file_publication.ensure_schema(connection)
    maintenance_file_review.ensure_schema(connection)
    maintenance_reconciliation.ensure_schema(connection)
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(maintenance_cycles)")
    }
    migrations = {
        "legacy_release_identity": "TEXT",
        "last_cycle_commit": "TEXT",
        "last_cycle_merged_count": "INTEGER",
        "cycle_request_id": "TEXT",
        "consumed_request_id": "TEXT",
        "assessment_plan_ids": "TEXT NOT NULL DEFAULT '[]'",
        "cycle_id": "TEXT",
        "include_general_planning": "INTEGER NOT NULL DEFAULT 0",
        "publication_refactoring_plan_ids": "TEXT NOT NULL DEFAULT '[]'",
        "publication_revalidation_plan_ids": "TEXT NOT NULL DEFAULT '[]'",
        "publication_id_allocations": "TEXT NOT NULL DEFAULT '[]'",
        "full_assessment_completed": "INTEGER NOT NULL DEFAULT 0",
        "full_assessment_source_sha": "TEXT",
        "full_assessment_result": "TEXT",
        "file_assessment_start_merged_count": "INTEGER",
        "last_success_file_assessment_merged_count": "INTEGER",
    }
    pending_columns = [(column, definition) for column, definition in migrations.items() if column not in columns]
    if pending_columns:
        with _transaction(connection):
            for column, definition in pending_columns:
                connection.execute(
                    f"ALTER TABLE maintenance_cycles ADD COLUMN {column} {definition}"
                )
            if "last_cycle_merged_count" not in columns:
                # Backfill once with its schema migration; initialized reads
                # must not acquire a write lock or leave an implicit transaction.
                connection.execute(
                    "UPDATE maintenance_cycles SET last_cycle_commit = last_success_commit, "
                    "last_cycle_merged_count = last_success_merged_count WHERE phase = 'idle' "
                    "AND publication_stage IS NULL AND last_cycle_merged_count IS NULL "
                    "AND last_success_commit IS NOT NULL AND last_success_merged_count >= 0"
                )
    draft_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(document_drafts)")
    }
    for column in ("plan_id", "publication_error"):
        if column not in draft_columns:
            connection.execute(f"ALTER TABLE document_drafts ADD COLUMN {column} TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS document_drafts_plan "
        "ON document_drafts(repo, plan_id) WHERE plan_id IS NOT NULL"
    )


def calculate_review_due(
    last_success_merged_count: int | None,
    current_merged_count: int,
    interval: int,
    phase: MaintenancePhase = MaintenancePhase.IDLE,
) -> ReviewDue:
    """Decide review eligibility solely from durable audit values."""

    if interval < 0:
        raise ValueError("refactoring review interval must be nonnegative")
    if current_merged_count < 0:
        raise MaintenanceStateError("MERGED ExecPlan 수가 음수입니다.")
    if phase is not MaintenancePhase.IDLE:
        return ReviewDue(False, "active_cycle", 0, phase)
    if last_success_merged_count is None:
        return ReviewDue(False, "baseline_pending", 0, phase)
    if last_success_merged_count < 0 or current_merged_count < last_success_merged_count:
        raise MaintenanceStateError("MERGED ExecPlan 감사 기준값이 현재 이력보다 큽니다.")
    merged_since_baseline = current_merged_count - last_success_merged_count
    if interval == 0:
        return ReviewDue(False, "disabled", merged_since_baseline, phase)
    required = merged_since_baseline >= interval
    return ReviewDue(
        required,
        "interval_reached" if required else "interval_pending",
        merged_since_baseline,
        phase,
    )


def calculate_file_assessment_due(
    start_merged_count: int | None,
    last_success_merged_count: int | None,
    current_merged_count: int,
    interval: int,
    phase: MaintenancePhase = MaintenancePhase.IDLE,
    *,
    completion_candidate: bool = False,
) -> ReviewDue:
    """Schedule a file assessment independently of legacy review history."""

    if interval < 0:
        raise ValueError("refactoring review interval must be nonnegative")
    if current_merged_count < 0:
        raise MaintenanceStateError("MERGED ExecPlan 수가 음수입니다.")
    if start_merged_count is not None and (
        start_merged_count < 0 or current_merged_count < start_merged_count
    ):
        raise MaintenanceStateError("파일별 평가 시작 기준값이 현재 이력보다 큽니다.")
    if last_success_merged_count is not None and (
        start_merged_count is None
        or last_success_merged_count < start_merged_count
        or current_merged_count < last_success_merged_count
    ):
        raise MaintenanceStateError("파일별 평가 성공 기준값이 현재 이력과 맞지 않습니다.")
    if phase is not MaintenancePhase.IDLE:
        return ReviewDue(False, "active_cycle", 0, phase)
    if start_merged_count is None:
        return ReviewDue(False, "baseline_pending", 0, phase)
    baseline = (
        last_success_merged_count
        if last_success_merged_count is not None
        else start_merged_count
    )
    merged_since_baseline = current_merged_count - baseline
    if interval == 0:
        return ReviewDue(False, "disabled", merged_since_baseline, phase)
    if merged_since_baseline >= interval:
        return ReviewDue(True, "interval_reached", merged_since_baseline, phase)
    if (
        completion_candidate
        and last_success_merged_count is not None
        and merged_since_baseline >= 10
    ):
        return ReviewDue(True, "stabilization_due", merged_since_baseline, phase)
    return ReviewDue(False, "interval_pending", merged_since_baseline, phase)


def _plan_sort_key(plan_id: str) -> tuple[int, int | str, str]:
    match = re.fullmatch(r"G-(\d+)", plan_id)
    if match:
        return (0, int(match.group(1)), plan_id)
    return (1, plan_id, plan_id)


def _decode_plan_ids(raw: object, field: str) -> tuple[str, ...]:
    try:
        values = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as error:
        raise MaintenanceStateError(
            f"{field} 저장값이 올바른 JSON 배열이 아닙니다."
        ) from error
    if not isinstance(values, list) or any(
        not isinstance(item, str) for item in values
    ):
        raise MaintenanceStateError(
            f"{field}에는 문자열 plan ID 배열만 저장할 수 있습니다."
        )
    if len(values) != len(set(values)):
        raise MaintenanceStateError(f"{field}에 중복 plan ID가 있습니다.")
    return tuple(sorted(values, key=_plan_sort_key))


def _encode_publication_plan_ids(values: Sequence[str], field: str) -> str:
    if isinstance(values, (str, bytes)) or any(
        not isinstance(item, str) or re.fullmatch(r"G-\d{3,}", item) is None
        for item in values
    ):
        raise MaintenanceStateError(
            f"{field}에는 G-000 형식 문자열 plan ID만 사용할 수 있습니다."
        )
    if len(values) != len(set(values)):
        raise MaintenanceStateError(f"{field}에 중복 plan ID가 있습니다.")
    return json.dumps(
        sorted(values, key=_plan_sort_key), separators=(",", ":")
    )


def _encode_publication_id_allocations(
    values: Sequence[tuple[str, str, str, str]],
) -> str:
    if isinstance(values, (str, bytes)):
        raise MaintenanceStateError("publication ID 배정 mapping 형식이 유효하지 않습니다.")
    allocations = []
    seen_sources: set[str] = set()
    seen_source_ids: set[str] = set()
    seen_targets: set[str] = set()
    seen_target_ids: set[str] = set()
    for item in values:
        if not isinstance(item, (tuple, list)) or len(item) != 4:
            raise MaintenanceStateError("publication ID 배정 mapping 항목이 유효하지 않습니다.")
        source_path, source_id, target_path, target_id = item
        if any(not isinstance(value, str) for value in item):
            raise MaintenanceStateError("publication ID 배정 mapping identity가 유효하지 않습니다.")
        source_match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] ([^/]+)\.md", source_path)
        target_match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] ([^/]+)\.md", target_path)
        if (
            not isinstance(source_id, str)
            or not isinstance(target_id, str)
            or source_match is None
            or target_match is None
            or source_match.group(1) != source_id
            or target_match.group(1) != target_id
            or source_match.group(2) != target_match.group(2)
            or source_path in seen_sources
            or source_id in seen_source_ids
            or target_path in seen_targets
            or target_id in seen_target_ids
        ):
            raise MaintenanceStateError("publication ID 배정 mapping identity가 유효하지 않습니다.")
        seen_sources.add(source_path)
        seen_source_ids.add(source_id)
        seen_targets.add(target_path)
        seen_target_ids.add(target_id)
        allocations.append((source_path, source_id, target_path, target_id))
    allocations.sort(key=lambda item: _plan_sort_key(item[3]))
    return json.dumps(allocations, separators=(",", ":"))


def _decode_publication_id_allocations(raw: object) -> tuple[tuple[str, str, str, str], ...]:
    try:
        values = json.loads(str(raw))
        if not isinstance(values, list):
            raise ValueError("not a list")
        normalized = _encode_publication_id_allocations(values)
        return tuple(tuple(item) for item in json.loads(normalized))
    except (TypeError, ValueError, json.JSONDecodeError, MaintenanceStateError) as error:
        raise MaintenanceStateError(
            "publication ID 배정 mapping 저장값이 유효하지 않습니다."
        ) from error


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if not connection.in_transaction:
        # Reserve the writer before reading cycle/baseline state. Concurrent
        # projects cannot safely upgrade two deferred read locks to writers.
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return
    savepoint = f"maintenance_{uuid4().hex}"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {savepoint}")
        connection.execute(f"RELEASE {savepoint}")
        raise
    else:
        connection.execute(f"RELEASE {savepoint}")


class MaintenanceRepository:
    """Transactional persistence boundary for per-project maintenance cycles."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def _notify_cycle(self, repo, cycle, kind, reason, plan_ids=()):
        from loop_manager_internal.notifications import notifications
        if not notifications._enabled(self.connection):
            return
        identity = cycle.cycle_id or f"{cycle.cycle_base_sha}:{cycle.cycle_start_merged_count}"
        titles = {row["plan_id"]: row["title"] for row in self.plan_rows(repo)}
        notifications.enqueue_progress(
            self.connection, repo, kind, identity,
            operation="유지보수", cause=reason,
            plans=[{"id": plan_id, "title": titles[plan_id]} for plan_id in plan_ids],
            status="유지보수 평가 중" if kind == "maintenance_started" else "일반 작업 선택 대기",
            silent=True,
        )

    def _merged_count(self, repo: str) -> int:
        from .execplan_catalog_state import verified_historical_merge_ids

        verified = verified_historical_merge_ids(self.connection, repo)
        archived = {
            str(row[0]) for row in self.connection.execute(
                "SELECT plan_id FROM execplan_catalog_history "
                "WHERE repo=? AND currently_archived=1", (repo,),
            )
        } if self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_catalog_history'"
        ).fetchone() else set()
        rows = self.connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo=? AND status='MERGED'", (repo,),
        ).fetchall()
        row_ids = {str(row[0]) for row in rows}
        return sum(1 for plan_id in row_ids if plan_id not in archived or plan_id in verified) + len(verified - row_ids)

    def merged_count(self, repo: str) -> int:
        """Return the existing catalog-backed MERGED count for a project."""

        return self._merged_count(repo)

    def plan_rows(self, repo: str) -> list[Mapping[str, object]]:
        """Use current rows plus proof-verified, rowless historical merges."""
        rows = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo AND h.plan_id = exec_plans.plan_id AND h.currently_archived = 1 "
            "AND (h.merge_confirmed = 0 OR h.identity_verified = 0 OR h.proof_revision IS NULL OR h.diagnostic_code IS NOT NULL))",
            (repo,),
        ).fetchall()
        from .execplan_catalog_state import verified_historical_merge_ids, verified_historical_merge_row

        row_ids = {str(row["plan_id"]) for row in rows}
        return [
            *rows,
            *(
                verified_historical_merge_row(plan_id)
                for plan_id in sorted(verified_historical_merge_ids(self.connection, repo) - row_ids)
            ),
        ]

    @staticmethod
    def _empty(repo: str) -> MaintenanceCycle:
        return MaintenanceCycle(
            repo=repo,
            last_success_commit=None,
            last_success_merged_count=None,
            file_assessment_start_merged_count=None,
            last_success_file_assessment_merged_count=None,
            phase=MaintenancePhase.IDLE,
            cycle_base_sha=None,
            cycle_start_merged_count=None,
            prerequisite_plan_ids=(),
            refactoring_plan_ids=(),
            revalidation_plan_ids=(),
            publication_stage=None,
            publication_base_sha=None,
            publication_commit_sha=None,
            publication_remote_oid=None,
            publication_refactoring_plan_ids=(),
            publication_revalidation_plan_ids=(),
            include_general_planning=False,
            full_assessment_completed=False,
            full_assessment_source_sha=None,
            full_assessment_result=None,
            revision=0,
            publication_id_allocations=(),
        )

    def get(self, repo: str) -> MaintenanceCycle:
        row = self.connection.execute(
            "SELECT * FROM maintenance_cycles WHERE repo = ?", (repo,)
        ).fetchone()
        if row is None:
            return self._empty(repo)
        try:
            phase = MaintenancePhase(str(row["phase"]))
        except ValueError as error:
            raise MaintenanceStateError(
                "알 수 없는 유지보수 주기 단계입니다."
            ) from error
        known_plan_ids = {
            str(item[0])
            for item in self.connection.execute(
                "SELECT plan_id FROM exec_plans WHERE repo = ?", (repo,)
            )
        }

        def stored_plan_ids(field: str) -> tuple[str, ...]:
            plan_ids = _decode_plan_ids(row[field], field)
            unknown = sorted(set(plan_ids) - known_plan_ids, key=_plan_sort_key)
            if unknown:
                raise MaintenanceStateError(
                    f"{field}에 알 수 없는 plan ID가 있습니다: {', '.join(unknown)}"
                )
            return plan_ids

        return MaintenanceCycle(
            repo=str(row["repo"]),
            last_success_commit=(
                str(row["last_success_commit"])
                if row["last_success_commit"] is not None
                else None
            ),
            last_success_merged_count=(
                int(row["last_success_merged_count"])
                if row["last_success_merged_count"] is not None
                else None
            ),
            file_assessment_start_merged_count=(
                int(row["file_assessment_start_merged_count"])
                if row["file_assessment_start_merged_count"] is not None
                else None
            ),
            last_success_file_assessment_merged_count=(
                int(row["last_success_file_assessment_merged_count"])
                if row["last_success_file_assessment_merged_count"] is not None
                else None
            ),
            phase=phase,
            cycle_base_sha=(
                str(row["cycle_base_sha"])
                if row["cycle_base_sha"] is not None
                else None
            ),
            cycle_start_merged_count=(
                int(row["cycle_start_merged_count"])
                if row["cycle_start_merged_count"] is not None
                else None
            ),
            prerequisite_plan_ids=stored_plan_ids("prerequisite_plan_ids"),
            refactoring_plan_ids=stored_plan_ids("refactoring_plan_ids"),
            revalidation_plan_ids=stored_plan_ids("revalidation_plan_ids"),
            publication_stage=(
                str(row["publication_stage"])
                if row["publication_stage"] is not None
                else None
            ),
            publication_base_sha=(
                str(row["publication_base_sha"])
                if row["publication_base_sha"] is not None
                else None
            ),
            publication_commit_sha=(
                str(row["publication_commit_sha"])
                if row["publication_commit_sha"] is not None
                else None
            ),
            publication_remote_oid=(
                str(row["publication_remote_oid"])
                if row["publication_remote_oid"] is not None
                else None
            ),
            publication_refactoring_plan_ids=_decode_plan_ids(
                row["publication_refactoring_plan_ids"],
                "publication_refactoring_plan_ids",
            ),
            publication_revalidation_plan_ids=_decode_plan_ids(
                row["publication_revalidation_plan_ids"],
                "publication_revalidation_plan_ids",
            ),
            include_general_planning=bool(row["include_general_planning"]),
            full_assessment_completed=bool(row["full_assessment_completed"]),
            full_assessment_source_sha=row["full_assessment_source_sha"],
            full_assessment_result=row["full_assessment_result"],
            revision=int(row["revision"]),
            last_cycle_commit=row["last_cycle_commit"],
            last_cycle_merged_count=row["last_cycle_merged_count"],
            cycle_request_id=row["cycle_request_id"],
            consumed_request_id=row["consumed_request_id"],
            assessment_plan_ids=stored_plan_ids("assessment_plan_ids"),
            cycle_id=row["cycle_id"],
            legacy_release_identity=row["legacy_release_identity"],
            publication_id_allocations=_decode_publication_id_allocations(
                row["publication_id_allocations"]
            ),
        )

    def due(self, repo: str, interval: int, *, self_repository: bool = False) -> ReviewDue:
        return self.file_assessment_due(repo, interval, self_repository=self_repository)

    def file_assessment_due(
        self, repo: str, interval: int, *, completion_candidate: bool = False,
        self_repository: bool = False,
    ) -> ReviewDue:
        state = self.get(repo)
        # Legacy idle success was written only after confirmed publication and
        # revalidation. Its count is a completion boundary, unlike a module receipt.
        baseline = state.last_cycle_merged_count
        if baseline is None and state.last_success_commit:
            baseline = state.last_success_merged_count
        merged_count = self._merged_count(repo)
        due = calculate_review_due(baseline, merged_count, interval, state.phase)
        if state.phase is not MaintenancePhase.IDLE:
            return due
        request = self.pending_request(repo)
        if request and request != state.consumed_request_id:
            return ReviewDue(True, "requested_full", due.merged_since_baseline, state.phase)
        if baseline is None:
            # A receipt alone does not prove the legacy cycle ended safely.
            if (state.last_success_merged_count is not None
                    or state.last_success_file_assessment_merged_count is not None
                    or state.full_assessment_completed):
                raise MaintenanceStateError("기존 유지보수 성공 기록의 종료 경계를 확인할 수 없습니다.")
            if self.connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'tiered_maintenance_success'").fetchone():
                if self.connection.execute("SELECT 1 FROM tiered_maintenance_success WHERE repo = ?", (repo,)).fetchone():
                    raise MaintenanceStateError("평가 근거는 있으나 유지보수 종료 경계를 확인할 수 없습니다.")
            return calculate_review_due(
                state.file_assessment_start_merged_count, merged_count, interval, state.phase,
            )
        return due

    def pending_request(self, repo: str) -> str | None:
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'tiered_maintenance_requests'").fetchone():
            return None
        row = self.connection.execute("SELECT request_id FROM tiered_maintenance_requests WHERE repo = ?", (repo,)).fetchone()
        return str(row[0]) if row else None

    def record_assessment_plans(self, repo: str, expected_revision: int, plan_ids: Sequence[str],
                                prerequisite_plan_ids: Sequence[str] = ()) -> None:
        """Save validated existing finding targets before publication, without changing its identity."""
        with _transaction(self.connection):
            encoded = self._serialize_plan_ids(repo, plan_ids, "assessment_plan_ids")
            prerequisites = self._serialize_plan_ids(repo, prerequisite_plan_ids, "prerequisite_plan_ids")
            cursor = self.connection.execute(
                "UPDATE maintenance_cycles SET assessment_plan_ids = ?, prerequisite_plan_ids = ? "
                "WHERE repo = ? AND phase = 'assessment' AND revision = ?",
                (encoded, prerequisites, repo, expected_revision),
            )
            if cursor.rowcount != 1:
                raise MaintenanceStateError("평가 범위를 기록할 cycle identity가 변경됐습니다.")

    def record_legacy_release(self, repo: str, *, expected: MaintenanceCycle, identity: str) -> MaintenanceCycle:
        """Bind a checked clean legacy workspace release before its Git removal."""
        with _transaction(self.connection):
            state = self.get(repo)
            if (state != expected or state.phase is not MaintenancePhase.POST_REFACTOR_REVALIDATION
                    or state.publication_stage == "prepared"
                    or re.fullmatch(r"[0-9a-f]{64}", identity) is None
                    or state.legacy_release_identity not in (None, identity)):
                raise MaintenanceStateError("기존 사후 단계의 workspace 해제 identity가 변경됐습니다.")
            self.connection.execute("UPDATE maintenance_cycles SET legacy_release_identity = ? WHERE repo = ?", (identity, repo))
            return self.get(repo)

    def clear_legacy_release(self, repo: str, *, expected: MaintenanceCycle) -> None:
        """Caller also clears the matching planning slot in the same transaction."""
        if self.get(repo) != expected or expected.legacy_release_identity is None:
            raise MaintenanceStateError("기존 사후 단계의 workspace 해제 intent가 변경됐습니다.")
        self.connection.execute("UPDATE maintenance_cycles SET legacy_release_identity = NULL WHERE repo = ?", (repo,))

    def finish_cycle(self, repo: str, *, expected: MaintenanceCycle, source_sha: str) -> MaintenanceCycle:
        """Close confirmed work atomically; never turn the merged source into a review receipt."""
        with _transaction(self.connection):
            state = self.get(repo)
            if state != expected or state.phase not in {
                MaintenancePhase.ASSESSMENT, MaintenancePhase.PREREQUISITE_WAIT, MaintenancePhase.REFACTORING,
                MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }:
                raise MaintenanceStateError("유지보수 종료 단계 또는 revision이 변경됐습니다.")
            if state.phase is MaintenancePhase.ASSESSMENT and (state.cycle_id is not None or not state.prerequisite_plan_ids):
                raise MaintenanceStateError("미확정 평가를 Merge 종료로 처리할 수 없습니다.")
            if state.legacy_release_identity is not None:
                raise MaintenanceStateError("미완료 workspace 해제를 유지보수 종료로 처리할 수 없습니다.")
            if state.publication_stage not in (None, "remote_confirmed"):
                raise MaintenanceStateError("미확정 유지보수 게시를 종료할 수 없습니다.")
            if state.publication_stage == "remote_confirmed" and (
                not state.publication_base_sha or not state.publication_commit_sha
                or state.publication_remote_oid != state.publication_commit_sha
            ):
                raise MaintenanceStateError("확정된 유지보수 게시 identity가 완전하지 않습니다.")
            from loop_manager_internal.maintenance.maintenance_orchestration import cycle_scope_plan_ids
            rows = self.plan_rows(repo)
            scope = cycle_scope_plan_ids(rows, state)
            from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
            if catalog_held_plan_ids(self.connection, repo).intersection(scope):
                raise MaintenanceStateError("유지보수 확정 범위의 catalog 보류가 남아 있습니다.")
            by_id = {row["plan_id"]: row["status"] for row in rows}
            if not scope or any(by_id[plan_id] != "MERGED" for plan_id in scope):
                raise MaintenanceStateError("유지보수 확정 범위의 실제 Merge가 완료되지 않았습니다.")
            count = self._verified_merged_count(repo, source_sha, self._merged_count(repo))
            self.connection.execute(
                "UPDATE maintenance_cycles SET phase = 'idle', last_cycle_commit = ?, last_cycle_merged_count = ?, publication_stage = NULL, "
                "include_general_planning = 0, revision = revision + 1, updated_at = CURRENT_TIMESTAMP WHERE repo = ?",
                (source_sha, count, repo),
            )
            self._notify_cycle(repo, state, "maintenance_completed", "확정 범위의 실제 Merge 완료", scope)
            return self.get(repo)

    def _verified_merged_count(
        self, repo: str, source_sha: str, expected_merged_count: int
    ) -> int:
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_sha):
            raise MaintenanceStateError("검증된 catalog source revision이 없습니다.")
        try:
            revision = None
            if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_catalog_views'"
            ).fetchone() is not None:
                revision = self.connection.execute(
                    "SELECT revision_sha FROM execplan_catalog_views WHERE repo = ?", (repo,),
                ).fetchone()
            if revision is None:
                revision = self.connection.execute(
                    "SELECT revision_sha FROM execplan_catalog_revisions WHERE repo = ?", (repo,),
                ).fetchone()
        except sqlite3.OperationalError as error:
            raise MaintenanceStateError(
                "검증된 catalog source revision이 없습니다."
            ) from error
        if revision is None or str(revision[0]) != source_sha:
            raise MaintenanceStateError("검증된 catalog source revision이 다릅니다.")
        count = self._merged_count(repo)
        if expected_merged_count < 0 or count != expected_merged_count:
            raise MaintenanceStateError("검증된 MERGED ExecPlan 수가 변경됐습니다.")
        return count

    def initialize_file_assessment_start(
        self, repo: str, *, source_sha: str, expected_merged_count: int
    ) -> MaintenanceCycle:
        """Set the first schedule baseline once at a verified, idle boundary."""

        with _transaction(self.connection):
            state = self.get(repo)
            if state.file_assessment_start_merged_count is not None:
                return state
            if state.phase is not MaintenancePhase.IDLE:
                return state
            count = self._verified_merged_count(repo, source_sha, expected_merged_count)
            if state.last_success_merged_count is not None and (
                state.last_success_merged_count < 0
                or count < state.last_success_merged_count
            ):
                raise MaintenanceStateError("MERGED ExecPlan 감사 기준값이 현재 이력보다 큽니다.")
            self.connection.execute(
                "INSERT INTO maintenance_cycles "
                "(repo, file_assessment_start_merged_count, revision) "
                "VALUES (?, ?, 1) ON CONFLICT(repo) DO UPDATE SET "
                "file_assessment_start_merged_count = excluded.file_assessment_start_merged_count, "
                "revision = maintenance_cycles.revision + 1, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE maintenance_cycles.file_assessment_start_merged_count IS NULL "
                "AND maintenance_cycles.phase = 'idle'",
                (repo, count),
            )
            return self.get(repo)

    def advance_file_assessment_success_baseline(
        self,
        repo: str,
        *,
        expected_revision: int,
        source_sha: str,
        expected_merged_count: int,
        bump_revision: bool = True,
    ) -> MaintenanceCycle:
        """Persist a verified file assessment success for the publication path."""

        with _transaction(self.connection):
            state = self.get(repo)
            if (
                state.phase not in {
                    MaintenancePhase.ASSESSMENT,
                    MaintenancePhase.POST_REFACTOR_REVALIDATION,
                }
                or state.revision != expected_revision
                or state.cycle_base_sha != source_sha
                or state.file_assessment_start_merged_count is None
            ):
                raise MaintenanceStateError("파일별 평가의 source 또는 단계가 변경됐습니다.")
            count = self._verified_merged_count(repo, source_sha, expected_merged_count)
            if count < state.file_assessment_start_merged_count or (
                state.last_success_file_assessment_merged_count is not None
                and count < state.last_success_file_assessment_merged_count
            ):
                raise MaintenanceStateError("파일별 평가 성공 기준값이 현재 이력보다 큽니다.")
            self.connection.execute(
                "UPDATE maintenance_cycles SET "
                "last_success_file_assessment_merged_count = ?, "
                "revision = revision + ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND revision = ?",
                (count, int(bump_revision), repo, expected_revision),
            )
            return self.get(repo)

    def settle_empty_stabilization_assessment(
        self,
        repo: str,
        interval: int,
        *,
        source_sha: str,
        repository_path: str,
        base_branch: str,
        inventory: SourceInventory,
    ) -> MaintenanceCycle:
        """Record a verified empty completion-candidate assessment once.

        Re-recording the empty success through the ledger retires deleted
        paths and rechecks the live remote revision immediately before the
        durable baseline advances.
        """

        with _transaction(self.connection):
            state = self.get(repo)
            if inventory.revision != source_sha:
                raise MaintenanceStateError("빈 안정화 평가 inventory source가 다릅니다.")
            # Compatibility for a saved legacy empty-selection settlement;
            # automatic selection no longer enters this stabilization path.
            decision = calculate_file_assessment_due(
                state.file_assessment_start_merged_count,
                state.last_success_file_assessment_merged_count,
                self._merged_count(repo), interval, state.phase,
                completion_candidate=True,
            )
            if (
                state.phase is not MaintenancePhase.IDLE
                or decision.reason != "stabilization_due"
            ):
                raise MaintenanceStateError(
                    "빈 안정화 평가는 현재 완료 후보에서만 확정할 수 있습니다."
                )
            maintenance_file_ledger.FileReviewRepository(
                self.connection
            ).record_success(
                repo, repository_path, base_branch, inventory, (),
            )
            # Keep the durable count tied to the exact revision whose empty
            # inventory just retired obsolete ledger entries.
            verify_remote_revision(Path(repository_path), base_branch, source_sha)
            count = self._verified_merged_count(
                repo, source_sha, decision.merged_since_baseline
                + int(state.last_success_file_assessment_merged_count or 0),
            )
            self.connection.execute(
                "UPDATE maintenance_cycles SET "
                "last_success_file_assessment_merged_count = ?, "
                "revision = revision + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND phase = 'idle' AND revision = ?",
                (count, repo, state.revision),
            )
            settled = self.get(repo)
            if settled.revision == state.revision:
                raise MaintenanceStateError("빈 안정화 평가 기준값을 확정하지 못했습니다.")
            return settled

    def _serialize_plan_ids(
        self, repo: str, values: Sequence[str], field: str
    ) -> str:
        if isinstance(values, (str, bytes)) or any(
            not isinstance(item, str) for item in values
        ):
            raise MaintenanceStateError(
                f"{field}에는 문자열 plan ID만 사용할 수 있습니다."
            )
        if len(values) != len(set(values)):
            raise MaintenanceStateError(f"{field}에 중복 plan ID가 있습니다.")
        known = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT plan_id FROM exec_plans WHERE repo = ?", (repo,)
            )
        }
        unknown = sorted(set(values) - known, key=_plan_sort_key)
        if unknown:
            raise MaintenanceStateError(
                f"{field}에 알 수 없는 plan ID가 있습니다: {', '.join(unknown)}"
            )
        return json.dumps(
            sorted(values, key=_plan_sort_key), separators=(",", ":")
        )

    def begin_assessment_if_due(
        self,
        repo: str,
        interval: int,
        base_sha: str,
        *,
        include_general_planning: bool = False,
        self_repository: bool = False,
        completion_candidate: bool = False,
        due_override: ReviewDue | None = None,
    ) -> tuple[MaintenanceCycle, bool]:
        """Create one assessment cycle, or return the already-active cycle."""

        with _transaction(self.connection):
            state = self.get(repo)
            if state.phase is not MaintenancePhase.IDLE:
                return state, False
            merged_count = self._merged_count(repo)
            decision = due_override or self.file_assessment_due(
                repo, interval, self_repository=self_repository,
                completion_candidate=completion_candidate,
            )
            if not decision.required:
                return state, False
            self.connection.execute(
                """
                INSERT INTO maintenance_cycles (
                    repo, phase, cycle_base_sha,
                    cycle_start_merged_count, include_general_planning, revision
                ) VALUES (?, 'assessment', ?, ?, ?, 1)
                ON CONFLICT(repo) DO UPDATE SET
                    phase = 'assessment',
                    cycle_base_sha = excluded.cycle_base_sha,
                    cycle_start_merged_count = excluded.cycle_start_merged_count,
                    prerequisite_plan_ids = '[]',
                    refactoring_plan_ids = '[]',
                    revalidation_plan_ids = '[]',
                    publication_stage = NULL,
                    publication_base_sha = NULL,
                    publication_commit_sha = NULL,
                    publication_remote_oid = NULL,
                    publication_refactoring_plan_ids = '[]',
                    publication_revalidation_plan_ids = '[]',
                    publication_id_allocations = '[]',
                    full_assessment_source_sha = CASE
                        WHEN full_assessment_completed = 0 THEN excluded.cycle_base_sha
                        ELSE full_assessment_source_sha END,
                    full_assessment_result = CASE
                        WHEN full_assessment_completed = 0 THEN NULL
                        ELSE full_assessment_result END,
                    include_general_planning = excluded.include_general_planning,
                    revision = maintenance_cycles.revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, base_sha, merged_count, int(include_general_planning)),
            )
            request_id = self.pending_request(repo)
            self.connection.execute(
                "UPDATE maintenance_cycles SET cycle_request_id = ?, consumed_request_id = COALESCE(?, consumed_request_id), "
                "assessment_plan_ids = '[]', cycle_id = ? WHERE repo = ?",
                (request_id, request_id, uuid4().hex, repo),
            )
            cycle = self.get(repo)
            self._notify_cycle(repo, cycle, "maintenance_started", {
                "initial_full": "최초 유지보수 평가",
                "interval_reached": "Merge 수에 따른 유지보수 주기 도래",
                "requested_full": "사용자의 명시적 유지보수 요청",
            }.get(decision.reason, decision.reason))
            return cycle, True

    def record_full_assessment(
        self, repo: str, *, expected_revision: int, source_sha: str,
        result: str,
    ) -> MaintenanceCycle:
        """Preserve a validated assessment before planning publication."""
        if len(result.encode("utf-8")) > 65536 or not result:
            raise MaintenanceStateError("전수 평가 기록이 비었거나 크기 제한을 넘었습니다.")
        with _transaction(self.connection):
            state = self.get(repo)
            if (state.phase is not MaintenancePhase.ASSESSMENT
                or state.revision != expected_revision
                or state.cycle_base_sha != source_sha
                or state.full_assessment_completed):
                raise MaintenanceStateError("전수 평가의 source 또는 단계가 변경됐습니다.")
            if state.full_assessment_result is not None:
                if state.full_assessment_result != result:
                    raise MaintenanceStateError("보존된 전수 평가 결과가 현재 결과와 다릅니다.")
                return state
            self.connection.execute(
                "UPDATE maintenance_cycles SET full_assessment_source_sha = ?, "
                "full_assessment_result = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND revision = ?",
                (source_sha, result, repo, expected_revision),
            )
            return self.get(repo)

    def record_publication_intent(
        self,
        repo: str,
        *,
        expected_phase: MaintenancePhase,
        expected_revision: int,
        base_sha: str,
        commit_sha: str,
        refactoring_plan_ids: Sequence[str] = (),
        revalidation_plan_ids: Sequence[str] = (),
        id_allocations: Sequence[tuple[str, str, str, str]] = (),
    ) -> MaintenanceCycle:
        """Persist a recoverable local batch before its remote push."""

        if (
            re.fullmatch(r"[0-9a-fA-F]{7,64}", base_sha) is None
            or re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha) is None
            or base_sha == commit_sha
        ):
            raise MaintenanceStateError(
                "유지보수 publication 기준과 commit SHA가 유효하지 않습니다."
            )
        serialized_allocations = _encode_publication_id_allocations(id_allocations)
        expected_allocations = _decode_publication_id_allocations(serialized_allocations)
        expected = ("prepared", base_sha, commit_sha, None)
        with _transaction(self.connection):
            state = self.get(repo)
            if (
                state.phase is not expected_phase
                or state.revision != expected_revision
            ):
                raise MaintenanceStateError(
                    "유지보수 주기 단계 또는 revision이 변경됐습니다."
                )
            current = (
                state.publication_stage,
                state.publication_base_sha,
                state.publication_commit_sha,
                state.publication_remote_oid,
            )
            if current == expected:
                expected_refactoring = tuple(
                    sorted(refactoring_plan_ids, key=_plan_sort_key)
                )
                expected_revalidation = tuple(
                    sorted(revalidation_plan_ids, key=_plan_sort_key)
                )
                if (
                    state.publication_refactoring_plan_ids != expected_refactoring
                    or state.publication_revalidation_plan_ids != expected_revalidation
                    or state.publication_id_allocations != expected_allocations
                ):
                    raise MaintenanceStateError(
                        "보존된 publication plan 집합이 현재 batch와 다릅니다."
                    )
                return state
            if any(value is not None for value in current):
                raise MaintenanceStateError(
                    "다른 유지보수 publication batch가 이미 보존돼 있습니다."
                )
            serialized_refactoring = _encode_publication_plan_ids(
                refactoring_plan_ids, "refactoring_plan_ids"
            )
            serialized_revalidation = _encode_publication_plan_ids(
                revalidation_plan_ids, "revalidation_plan_ids"
            )
            cursor = self.connection.execute(
                """
                UPDATE maintenance_cycles
                SET publication_stage = 'prepared',
                    publication_base_sha = ?,
                    publication_commit_sha = ?,
                    publication_remote_oid = NULL,
                    publication_refactoring_plan_ids = ?,
                    publication_revalidation_plan_ids = ?,
                    publication_id_allocations = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                  AND publication_stage IS NULL
                  AND publication_base_sha IS NULL
                  AND publication_commit_sha IS NULL
                  AND publication_remote_oid IS NULL
                """,
                (
                    base_sha,
                    commit_sha,
                    serialized_refactoring,
                    serialized_revalidation,
                    serialized_allocations,
                    repo,
                    expected_phase.value,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise MaintenanceStateError(
                    "유지보수 publication intent를 보존할 수 없습니다."
                )
            return self.get(repo)

    def restart_prepared_batch(
        self,
        repo: str,
        *,
        expected: MaintenanceCycle,
        new_base_sha: str,
    ) -> MaintenanceCycle:
        """Invalidate a prepared intent while retaining its maintenance phase.

        The caller proves either that the batch was not published or that its
        published plans survived but source drift requires a new assessment.
        Revision comparison invalidates old contexts.
        """
        if (
            expected.repo != repo
            or expected.phase not in {
                MaintenancePhase.ASSESSMENT,
                MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }
            or expected.publication_stage != "prepared"
            or not expected.publication_base_sha
            or not expected.publication_commit_sha
            or expected.publication_remote_oid is not None
            or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", new_base_sha) is None
            or new_base_sha in {
                expected.publication_base_sha, expected.publication_commit_sha
            }
        ):
            raise MaintenanceStateError("재시작할 prepared publication intent가 유효하지 않습니다.")
        with _transaction(self.connection):
            if self.get(repo) != expected:
                raise MaintenanceStateError("유지보수 주기 단계 또는 revision이 변경됐습니다.")
            self.connection.execute(
                """
                UPDATE maintenance_cycles
                SET cycle_base_sha = ?, publication_stage = NULL,
                    full_assessment_result = CASE
                        WHEN full_assessment_completed = 1
                          OR full_assessment_source_sha = ? THEN full_assessment_result
                        ELSE NULL END,
                    full_assessment_source_sha = CASE
                        WHEN full_assessment_completed = 1 THEN full_assessment_source_sha
                        ELSE ? END,
                    publication_base_sha = NULL, publication_commit_sha = NULL,
                    publication_remote_oid = NULL,
                    publication_refactoring_plan_ids = '[]',
                    publication_revalidation_plan_ids = '[]',
                    publication_id_allocations = '[]',
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                """,
                (new_base_sha, new_base_sha, new_base_sha, repo, expected.phase.value, expected.revision),
            )
            return self.get(repo)

    def restart_stale_planning_context(
        self,
        repo: str,
        *,
        expected: MaintenanceCycle,
        new_base_sha: str,
    ) -> MaintenanceCycle:
        """Move an intent-free planning stage to a newer remote base.

        Incrementing the revision invalidates every context created from the
        stale base. The active phase and its recorded plan sets are preserved
        so the caller can create a fresh planning worktree for the same stage.
        """

        publication = (
            expected.publication_stage,
            expected.publication_base_sha,
            expected.publication_commit_sha,
            expected.publication_remote_oid,
        )
        if (
            expected.repo != repo
            or expected.phase not in {
                MaintenancePhase.ASSESSMENT,
                MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }
            or any(value is not None for value in publication)
            or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", new_base_sha)
            is None
        ):
            raise MaintenanceStateError(
                "재시작할 intent-free 유지보수 planning context가 유효하지 않습니다."
            )
        with _transaction(self.connection):
            if self.get(repo) != expected:
                raise MaintenanceStateError(
                    "유지보수 주기 단계 또는 revision이 변경됐습니다."
                )
            cursor = self.connection.execute(
                """
                UPDATE maintenance_cycles
                SET cycle_base_sha = ?, revision = revision + 1,
                    full_assessment_result = CASE
                        WHEN full_assessment_completed = 1
                          OR full_assessment_source_sha = ? THEN full_assessment_result
                        ELSE NULL END,
                    full_assessment_source_sha = CASE
                        WHEN full_assessment_completed = 1 THEN full_assessment_source_sha
                        ELSE ? END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                  AND publication_stage IS NULL
                  AND publication_base_sha IS NULL
                  AND publication_commit_sha IS NULL
                  AND publication_remote_oid IS NULL
                """,
                (new_base_sha, new_base_sha, new_base_sha, repo, expected.phase.value, expected.revision),
            )
            if cursor.rowcount != 1:
                raise MaintenanceStateError(
                    "stale 유지보수 planning context를 재시작할 수 없습니다."
                )
            return self.get(repo)

    def transition(
        self,
        repo: str,
        *,
        expected_phase: MaintenancePhase,
        expected_revision: int,
        next_phase: MaintenancePhase,
        prerequisite_plan_ids: Sequence[str] | None = None,
        refactoring_plan_ids: Sequence[str] | None = None,
        revalidation_plan_ids: Sequence[str] | None = None,
        publication_stage: str | None = None,
        publication_base_sha: str | None = None,
        publication_commit_sha: str | None = None,
        publication_remote_oid: str | None = None,
        cycle_base_sha: str | None = None,
        include_general_planning: bool | None = None,
    ) -> MaintenanceCycle:
        allowed = {
            MaintenancePhase.ASSESSMENT: {
                MaintenancePhase.PREREQUISITE_WAIT,
                MaintenancePhase.REFACTORING,
            },
            MaintenancePhase.PREREQUISITE_WAIT: {
                MaintenancePhase.ASSESSMENT
            },
            MaintenancePhase.REFACTORING: {
                MaintenancePhase.POST_REFACTOR_REVALIDATION
            },
        }
        if next_phase not in allowed.get(expected_phase, set()):
            raise MaintenanceStateError(
                "허용되지 않는 유지보수 단계 전이입니다: "
                f"{expected_phase.value} -> {next_phase.value}"
            )
        updates: dict[str, object] = {
            "phase": next_phase.value,
            "publication_stage": publication_stage,
            "publication_base_sha": publication_base_sha,
            "publication_commit_sha": publication_commit_sha,
            "publication_remote_oid": publication_remote_oid,
        }
        if cycle_base_sha is not None:
            updates["cycle_base_sha"] = cycle_base_sha
        if include_general_planning is not None:
            updates["include_general_planning"] = int(include_general_planning)
        if publication_stage is None:
            updates["publication_refactoring_plan_ids"] = "[]"
            updates["publication_revalidation_plan_ids"] = "[]"
            updates["publication_id_allocations"] = "[]"
        with _transaction(self.connection):
            state = self.get(repo)
            if cycle_base_sha is not None and not state.full_assessment_completed:
                updates["full_assessment_result"] = (
                    state.full_assessment_result
                    if cycle_base_sha == state.full_assessment_source_sha else None
                )
                updates["full_assessment_source_sha"] = cycle_base_sha
            if (next_phase in {MaintenancePhase.REFACTORING, MaintenancePhase.PREREQUISITE_WAIT}
                and state.full_assessment_result is not None):
                updates["full_assessment_completed"] = 1
            if prerequisite_plan_ids is not None:
                updates["prerequisite_plan_ids"] = self._serialize_plan_ids(
                    repo, prerequisite_plan_ids, "prerequisite_plan_ids"
                )
            if refactoring_plan_ids is not None:
                updates["refactoring_plan_ids"] = self._serialize_plan_ids(
                    repo, refactoring_plan_ids, "refactoring_plan_ids"
                )
            if revalidation_plan_ids is not None:
                updates["revalidation_plan_ids"] = self._serialize_plan_ids(
                    repo, revalidation_plan_ids, "revalidation_plan_ids"
                )
            assignments = ", ".join(f"{name} = ?" for name in updates)
            cursor = self.connection.execute(
                f"""
                UPDATE maintenance_cycles
                SET {assignments}, revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                """,
                (*updates.values(), repo, expected_phase.value, expected_revision),
            )
            if cursor.rowcount != 1:
                raise MaintenanceStateError(
                    "유지보수 주기 단계 또는 revision이 변경됐습니다."
                )
            return self.get(repo)

    def advance_success_baseline(
        self,
        repo: str,
        *,
        expected_phase: MaintenancePhase,
        expected_revision: int,
        remote_commit: str,
    ) -> MaintenanceCycle:
        if expected_phase not in {
            MaintenancePhase.ASSESSMENT,
            MaintenancePhase.POST_REFACTOR_REVALIDATION,
        }:
            raise MaintenanceStateError(
                "이 단계에서는 성공 기준선을 전진시킬 수 없습니다."
            )
        with _transaction(self.connection):
            state = self.get(repo)
            if expected_phase is MaintenancePhase.POST_REFACTOR_REVALIDATION:
                from loop_manager_internal.maintenance.maintenance_orchestration import cycle_scope_plan_ids
                rows = self.plan_rows(repo)
                scope = cycle_scope_plan_ids(rows, state)
                statuses = {row["plan_id"]: row["status"] for row in rows}
                if not scope or any(statuses[plan_id] != "MERGED" for plan_id in scope):
                    raise MaintenanceStateError("기존 사후 단계의 확정 범위 Merge가 완료되지 않았습니다.")
            if expected_phase is MaintenancePhase.ASSESSMENT and (state.assessment_plan_ids or state.prerequisite_plan_ids):
                return self.transition(
                    repo, expected_phase=expected_phase, expected_revision=expected_revision,
                    next_phase=(MaintenancePhase.PREREQUISITE_WAIT if state.prerequisite_plan_ids
                                else MaintenancePhase.REFACTORING), include_general_planning=False,
                )
            merged_count = self._merged_count(repo)
            cursor = self.connection.execute(
                """
                UPDATE maintenance_cycles
                SET last_success_commit = CASE WHEN phase = 'assessment' THEN ? ELSE last_success_commit END,
                    last_success_merged_count = CASE WHEN phase = 'assessment' THEN ? ELSE last_success_merged_count END,
                    last_cycle_commit = ?, last_cycle_merged_count = ?,
                    full_assessment_completed = CASE
                        WHEN full_assessment_result IS NOT NULL THEN 1
                        ELSE full_assessment_completed END,
                    phase = 'idle', cycle_base_sha = NULL,
                    cycle_start_merged_count = NULL,
                    prerequisite_plan_ids = '[]',
                    refactoring_plan_ids = '[]',
                    revalidation_plan_ids = '[]',
                    publication_stage = NULL,
                    publication_base_sha = NULL,
                    publication_commit_sha = NULL,
                    publication_remote_oid = NULL,
                    publication_refactoring_plan_ids = '[]',
                    publication_revalidation_plan_ids = '[]',
                    publication_id_allocations = '[]',
                    include_general_planning = 0,
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                """,
                (
                    remote_commit,
                    merged_count,
                    remote_commit,
                    merged_count,
                    repo,
                    expected_phase.value,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise MaintenanceStateError(
                    "유지보수 주기 단계 또는 revision이 변경됐습니다."
                )
            self._notify_cycle(repo, state, "maintenance_completed",
                               "후속 계획 없이 평가·게시 완료" if expected_phase is MaintenancePhase.ASSESSMENT
                               else "확정 범위의 실제 Merge 완료",
                               () if expected_phase is MaintenancePhase.ASSESSMENT else scope)
            return self.get(repo)
