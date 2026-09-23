"""Durable recovery attempts, separate from projected execution state."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3

from recovery_runtime import (
    RecoveryAction, RecoveryDecision, RecoveryObservation, decide,
)
from scoped_blocking import BlockReasonCode


SCHEMA = """
CREATE TABLE IF NOT EXISTS recovery_records (
    repo TEXT NOT NULL,
    subject TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    identity TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    disposition TEXT NOT NULL,
    pending_action TEXT NOT NULL,
    phase TEXT NOT NULL,
    plan_revision TEXT,
    base_revision TEXT,
    fingerprint TEXT NOT NULL,
    progress_marker TEXT NOT NULL,
    occurrence INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    recovery_counts TEXT NOT NULL DEFAULT '{}',
    occurrence_index TEXT NOT NULL DEFAULT '{}',
    detail TEXT NOT NULL,
    final_evidence TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, subject)
)
"""


def block_user_targets(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    reason: BlockReasonCode,
    plan_path: str,
) -> tuple[str, ...]:
    """Return edit locations only when a final cause identifies exact lines."""

    if reason is not BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED:
        return ()
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'guidance_approvals'"
    ).fetchone()
    if exists is None:
        return ()
    rows = connection.execute(
        "SELECT proposal_line, decision_line FROM guidance_approvals "
        "WHERE repo = ? AND plan_id = ? AND applied = 0 "
        "ORDER BY sequence, proposal_id",
        (repo, plan_id),
    ).fetchall()
    return tuple(
        f"{plan_path}:{line}"
        for row in rows
        for line in (row[0], row[1])
    )


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(recovery_records)")}
    if "recovery_counts" not in columns:
        connection.execute(
            "ALTER TABLE recovery_records ADD COLUMN recovery_counts TEXT NOT NULL DEFAULT '{}'"
        )
    if "occurrence_index" not in columns:
        connection.execute(
            "ALTER TABLE recovery_records ADD COLUMN occurrence_index TEXT NOT NULL DEFAULT '{}'"
        )


class RecoveryStateConflict(RuntimeError):
    """Another process changed the selected recovery record."""


@dataclass(frozen=True)
class RecoveryRecord:
    generation: int
    decision: RecoveryDecision
    final_evidence: dict | None


class RecoveryRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        if not getattr(connection, "execplans_schema_ready", False):
            ensure_schema(connection)

    @staticmethod
    def _subject(plan_id: str | None) -> str:
        return plan_id if plan_id is not None else "__project__"

    def row(self, repo: str, plan_id: str | None) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM recovery_records WHERE repo = ? AND subject = ?",
            (repo, self._subject(plan_id)),
        ).fetchone()

    def observe(
        self,
        observation: RecoveryObservation,
        *,
        attempts: int = 0,
        correction_limit: int | None = None,
        same_run_retry_limit: int = 3,
        expected_generation: int | None = None,
        final_block_reason: BlockReasonCode | None = None,
    ) -> RecoveryRecord:
        """CAS an observation without touching workspace or validation fields."""

        previous = self.row(observation.repo, observation.plan_id)
        generation = int(previous["generation"]) if previous is not None else None
        if expected_generation is not None and generation != expected_generation:
            raise RecoveryStateConflict("recovery generation changed")
        occurrence_index = (
            json.loads(str(previous["occurrence_index"]))
            if previous is not None else {}
        )
        occurrence_key = json.dumps(
            [observation.reason.value, observation.fingerprint,
             observation.progress_marker], separators=(",", ":"),
        )
        occurrence = occurrence_index.get(occurrence_key, 0) + 1
        occurrence_index[occurrence_key] = occurrence
        counts = (
            json.loads(str(previous["recovery_counts"]))
            if previous is not None else {}
        )
        decision = decide(
            observation, occurrence=occurrence, attempts=attempts,
            correction_limit=correction_limit,
            same_run_retry_limit=same_run_retry_limit,
            final_block_reason=final_block_reason,
        )
        if decision.action is not RecoveryAction.FINAL_BLOCK:
            counts[observation.reason.value] = counts.get(observation.reason.value, 0) + 1
        evidence = None
        if decision.action is RecoveryAction.FINAL_BLOCK:
            last_stable_revision = (
                observation.base_revision
                or (str(previous["base_revision"] or "") if previous is not None else None)
            )
            evidence = {
                "reason_code": observation.reason.value,
                "block_reason_code": decision.block_reason.value,
                "recovery_disposition": decision.disposition.value,
                "occurrence": occurrence,
                "attempts": attempts,
                "recovery_attempts": counts,
                "last_stable_revision": last_stable_revision,
                "unresolved": decision.unresolved,
                "user_targets": list(observation.user_targets),
            }
        values = (
            observation.identity, observation.reason.value,
            decision.disposition.value, decision.action.value,
            observation.phase, observation.plan_revision,
            observation.base_revision, observation.fingerprint,
            observation.progress_marker, occurrence, attempts,
            json.dumps(counts, sort_keys=True, separators=(",", ":")),
            json.dumps(occurrence_index, sort_keys=True, separators=(",", ":")),
            observation.detail,
            json.dumps(evidence, sort_keys=True, ensure_ascii=False)
            if evidence is not None else None,
        )
        if previous is None:
            try:
                self.connection.execute(
                    "INSERT INTO recovery_records (repo, subject, identity, reason_code, "
                    "disposition, pending_action, phase, plan_revision, base_revision, "
                    "fingerprint, progress_marker, occurrence, attempts, recovery_counts, occurrence_index, "
                    "detail, final_evidence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (observation.repo, self._subject(observation.plan_id), *values),
                )
            except sqlite3.IntegrityError as error:
                raise RecoveryStateConflict("recovery record appeared") from error
            next_generation = 1
        else:
            cursor = self.connection.execute(
                "UPDATE recovery_records SET generation = generation + 1, "
                "identity = ?, reason_code = ?, disposition = ?, pending_action = ?, "
                "phase = ?, plan_revision = ?, base_revision = ?, fingerprint = ?, "
                "progress_marker = ?, occurrence = ?, attempts = ?, recovery_counts = ?, occurrence_index = ?, detail = ?, "
                "final_evidence = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND subject = ? AND generation = ?",
                (*values, observation.repo, self._subject(observation.plan_id), generation),
            )
            if cursor.rowcount != 1:
                raise RecoveryStateConflict("recovery generation changed")
            next_generation = generation + 1
        return RecoveryRecord(next_generation, decision, evidence)

    def clear(self, repo: str, plan_id: str | None) -> None:
        self.connection.execute(
            "DELETE FROM recovery_records WHERE repo = ? AND subject = ?",
            (repo, self._subject(plan_id)),
        )

    def mark_paused(self, repo: str, subject: str, identity: str,
                    generation: int, attempts: int) -> None:
        """Stop one run after its bounded retries without blocking the plan."""

        changed = self.connection.execute(
            "UPDATE recovery_records SET pending_action = 'pause', "
            "attempts = ?, generation = generation + 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND subject = ? "
            "AND identity = ? AND generation = ? "
            "AND disposition IN ('transient_retry', 'state_reconcile')",
            (attempts, repo, subject, identity, generation),
        )
        if changed.rowcount != 1:
            raise RecoveryStateConflict("recovery pause target changed")
