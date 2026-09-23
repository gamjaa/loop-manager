"""Durable target identity for bounded Requirement archive reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Callable

from requirement_archive import ArchiveSnapshot
import runtime_state


SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_archive_targets (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    base_oid TEXT NOT NULL,
    catalog_token TEXT NOT NULL,
    state_token TEXT NOT NULL,
    snapshot_token TEXT NOT NULL,
    candidates TEXT NOT NULL,
    move_set TEXT,
    stage TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id)
)
"""


class ArchiveTargetConflict(RuntimeError):
    """The remote base, execution, or target changed during reconciliation."""


@dataclass(frozen=True)
class ArchiveTarget:
    generation: int
    snapshot: ArchiveSnapshot
    snapshot_token: str
    stage: str
    attempts: int


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)


class ArchiveTargetRepository:
    """CAS a pending target beside the expected execution snapshot."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def get(self, repo: str, plan_id: str) -> ArchiveTarget | None:
        row = self.connection.execute(
            "SELECT * FROM requirement_archive_targets WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            return None
        snapshot = ArchiveSnapshot(
            base_oid=str(row["base_oid"]),
            catalog_token=str(row["catalog_token"]),
            state_token=str(row["state_token"]),
            current_plan_id=plan_id,
            candidates=tuple(json.loads(str(row["candidates"]))),
            related_status=(),
            move_set=(tuple(json.loads(str(row["move_set"])))
                      if row["move_set"] is not None else None),
        )
        return ArchiveTarget(int(row["generation"]), snapshot,
                             str(row["snapshot_token"]), str(row["stage"]),
                             int(row["attempts"]))

    def pause(
        self, repo: str, plan_id: str, observed_base: str,
        *, expected_execution: runtime_state.ExecutionSnapshot,
        attempts: int, expected_generation: int | None,
    ) -> None:
        """Persist a bounded remote race without accepting an eligibility set."""

        with runtime_state._transaction(self.connection):
            if runtime_state.RuntimeStateRepository(self.connection).get(
                repo, plan_id
            ) != expected_execution:
                raise ArchiveTargetConflict("archive pause 실행 snapshot이 변경됐습니다.")
            current = self.connection.execute(
                "SELECT generation FROM requirement_archive_targets "
                "WHERE repo = ? AND plan_id = ?", (repo, plan_id),
            ).fetchone()
            generation = int(current["generation"]) if current else None
            if generation != expected_generation:
                raise ArchiveTargetConflict("archive pause target이 변경됐습니다.")
            if current is None:
                self.connection.execute(
                    "INSERT INTO requirement_archive_targets "
                    "(repo, plan_id, generation, base_oid, catalog_token, "
                    "state_token, snapshot_token, candidates, move_set, stage, attempts) "
                    "VALUES (?, ?, 1, ?, '', '', '', '[]', NULL, 'PAUSED', ?)",
                    (repo, plan_id, observed_base, attempts),
                )
            else:
                self.connection.execute(
                    "UPDATE requirement_archive_targets SET stage = 'PAUSED', "
                    "attempts = ?, generation = generation + 1, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE repo = ? AND plan_id = ? AND generation = ?",
                    (attempts, repo, plan_id, generation),
                )

    def put(
        self,
        repo: str,
        snapshot: ArchiveSnapshot,
        *,
        expected_execution: runtime_state.ExecutionSnapshot,
        base_is_current: Callable[[str], bool],
        snapshot_is_current: Callable[[], bool],
        expected_generation: int | None = None,
        stage: str = "OBSERVED",
        attempts: int = 0,
    ) -> ArchiveTarget:
        if stage not in {"OBSERVED", "WORKTREE_UPDATED", "CORRECTION_PENDING",
                         "VALIDATED", "PAUSED"}:
            raise ValueError(f"유효하지 않은 archive target stage: {stage}")
        if expected_execution.repo != repo or expected_execution.plan_id != snapshot.current_plan_id:
            raise ArchiveTargetConflict("archive target 실행 identity가 다릅니다.")
        if not base_is_current(snapshot.base_oid) or not snapshot_is_current():
            raise ArchiveTargetConflict("archive target 원격 또는 SQLite snapshot이 변경됐습니다.")
        with runtime_state._transaction(self.connection):
            current = runtime_state.RuntimeStateRepository(self.connection).get(
                repo, snapshot.current_plan_id
            )
            if current != expected_execution:
                raise ArchiveTargetConflict("archive target 실행 snapshot이 변경됐습니다.")
            previous = self.connection.execute(
                "SELECT generation FROM requirement_archive_targets "
                "WHERE repo = ? AND plan_id = ?",
                (repo, snapshot.current_plan_id),
            ).fetchone()
            generation = int(previous["generation"]) if previous else None
            if generation != expected_generation:
                raise ArchiveTargetConflict("archive target generation이 변경됐습니다.")
            values = (
                snapshot.base_oid, snapshot.catalog_token, snapshot.state_token,
                snapshot.token, json.dumps(snapshot.candidates, ensure_ascii=False),
                json.dumps(snapshot.move_set, ensure_ascii=False)
                if snapshot.move_set is not None else None,
                stage, attempts,
            )
            if previous is None:
                self.connection.execute(
                    "INSERT INTO requirement_archive_targets "
                    "(repo, plan_id, generation, base_oid, catalog_token, state_token, "
                    "snapshot_token, candidates, move_set, stage, attempts) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, snapshot.current_plan_id, *values),
                )
                next_generation = 1
            else:
                changed = self.connection.execute(
                    "UPDATE requirement_archive_targets SET generation = generation + 1, "
                    "base_oid = ?, catalog_token = ?, state_token = ?, "
                    "snapshot_token = ?, candidates = ?, move_set = ?, stage = ?, "
                    "attempts = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE repo = ? AND plan_id = ? AND generation = ?",
                    (*values, repo, snapshot.current_plan_id, generation),
                )
                if changed.rowcount != 1:
                    raise ArchiveTargetConflict("archive target generation이 변경됐습니다.")
                next_generation = generation + 1
            if not base_is_current(snapshot.base_oid) or not snapshot_is_current():
                raise ArchiveTargetConflict("archive target 저장 중 원격 또는 SQLite snapshot이 변경됐습니다.")
        return ArchiveTarget(next_generation, snapshot, snapshot.token, stage, attempts)
