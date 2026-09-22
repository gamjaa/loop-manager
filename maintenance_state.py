"""Durable state and bounded Git context for maintenance review cycles."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import json
import re
import sqlite3
from typing import Iterator, Sequence
from uuid import uuid4


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
    include_general_planning INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


@dataclass(frozen=True)
class MaintenanceCycle:
    repo: str
    last_success_commit: str | None
    last_success_merged_count: int | None
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
    revision: int


@dataclass(frozen=True)
class ReviewDue:
    required: bool
    reason: str
    merged_since_baseline: int
    active_phase: MaintenancePhase


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the additive maintenance table without rewriting audit data."""

    connection.execute(MAINTENANCE_SCHEMA)
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(maintenance_cycles)")
    }
    migrations = {
        "include_general_planning": "INTEGER NOT NULL DEFAULT 0",
        "publication_refactoring_plan_ids": "TEXT NOT NULL DEFAULT '[]'",
        "publication_revalidation_plan_ids": "TEXT NOT NULL DEFAULT '[]'",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE maintenance_cycles ADD COLUMN {column} {definition}"
            )


def calculate_review_due(
    last_success_merged_count: int | None,
    current_merged_count: int,
    interval: int,
    phase: MaintenancePhase = MaintenancePhase.IDLE,
) -> ReviewDue:
    """Decide review eligibility solely from durable audit values."""

    if interval <= 0:
        raise ValueError("refactoring review interval must be positive")
    if current_merged_count < 0:
        raise MaintenanceStateError("MERGED ExecPlan 수가 음수입니다.")
    if phase is not MaintenancePhase.IDLE:
        return ReviewDue(False, "active_cycle", 0, phase)
    if last_success_merged_count is None:
        return ReviewDue(True, "no_successful_assessment", 0, phase)
    if last_success_merged_count < 0 or current_merged_count < last_success_merged_count:
        raise MaintenanceStateError("MERGED ExecPlan 감사 기준값이 현재 이력보다 큽니다.")
    merged_since_baseline = current_merged_count - last_success_merged_count
    required = merged_since_baseline >= interval
    return ReviewDue(
        required,
        "interval_reached" if required else "interval_pending",
        merged_since_baseline,
        phase,
    )


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


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
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

    def _merged_count(self, repo: str) -> int:
        history_exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'execplan_catalog_history'"
        ).fetchone()
        history_gate = ""
        if history_exists is not None:
            history_gate = (
                " AND NOT EXISTS ("
                "SELECT 1 FROM execplan_catalog_history h "
                "WHERE h.repo = exec_plans.repo "
                "AND h.currently_archived = 1 "
                "AND h.plan_id = exec_plans.plan_id "
                "AND h.merge_confirmed = 0)"
            )
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM exec_plans "
                "WHERE repo = ? AND status = 'MERGED'" + history_gate,
                (repo,),
            ).fetchone()[0]
        )

    @staticmethod
    def _empty(repo: str) -> MaintenanceCycle:
        return MaintenanceCycle(
            repo=repo,
            last_success_commit=None,
            last_success_merged_count=None,
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
            revision=0,
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
            revision=int(row["revision"]),
        )

    def due(self, repo: str, interval: int) -> ReviewDue:
        state = self.get(repo)
        return calculate_review_due(
            state.last_success_merged_count,
            self._merged_count(repo),
            interval,
            state.phase,
        )

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
    ) -> tuple[MaintenanceCycle, bool]:
        """Create one assessment cycle, or return the already-active cycle."""

        with _transaction(self.connection):
            state = self.get(repo)
            if state.phase is not MaintenancePhase.IDLE:
                return state, False
            merged_count = self._merged_count(repo)
            decision = calculate_review_due(
                state.last_success_merged_count,
                merged_count,
                interval,
                state.phase,
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
                    include_general_planning = excluded.include_general_planning,
                    revision = maintenance_cycles.revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, base_sha, merged_count, int(include_general_planning)),
            )
            return self.get(repo), True

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

    def restart_unpublished_batch(
        self,
        repo: str,
        *,
        expected: MaintenanceCycle,
        new_base_sha: str,
    ) -> MaintenanceCycle:
        """Invalidate one unpushed intent while retaining its maintenance phase.

        The caller must first prove from Git that the remote advanced without
        publishing this batch. Revision comparison invalidates old contexts.
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
            raise MaintenanceStateError("재시작할 미게시 publication intent가 유효하지 않습니다.")
        with _transaction(self.connection):
            if self.get(repo) != expected:
                raise MaintenanceStateError("유지보수 주기 단계 또는 revision이 변경됐습니다.")
            self.connection.execute(
                """
                UPDATE maintenance_cycles
                SET cycle_base_sha = ?, publication_stage = NULL,
                    publication_base_sha = NULL, publication_commit_sha = NULL,
                    publication_remote_oid = NULL,
                    publication_refactoring_plan_ids = '[]',
                    publication_revalidation_plan_ids = '[]',
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                """,
                (new_base_sha, repo, expected.phase.value, expected.revision),
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
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                  AND publication_stage IS NULL
                  AND publication_base_sha IS NULL
                  AND publication_commit_sha IS NULL
                  AND publication_remote_oid IS NULL
                """,
                (new_base_sha, repo, expected.phase.value, expected.revision),
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
        with _transaction(self.connection):
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
            merged_count = self._merged_count(repo)
            cursor = self.connection.execute(
                """
                UPDATE maintenance_cycles
                SET last_success_commit = ?, last_success_merged_count = ?,
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
                    include_general_planning = 0,
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND phase = ? AND revision = ?
                """,
                (
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
            return self.get(repo)
