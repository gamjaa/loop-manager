"""Durable recovery attempts, separate from projected execution state."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
import uuid

from recovery_runtime import (
    RecoveryAction, RecoveryDecision, RecoveryObservation, RecoveryReason, decide,
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

PERSISTENT_FAILURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS persistent_execution_failures (
    repo TEXT NOT NULL,
    subject TEXT NOT NULL,
    episode INTEGER NOT NULL DEFAULT 1,
    phase TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    progress_marker TEXT NOT NULL,
    consecutive_runs INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    notification_eligible INTEGER NOT NULL DEFAULT 0,
    notification_queued INTEGER NOT NULL DEFAULT 0,
    suppressed_by_block INTEGER NOT NULL DEFAULT 0,
    last_run_id TEXT,
    detail TEXT NOT NULL DEFAULT '',
    recovery_attempts TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
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
    connection.execute(PERSISTENT_FAILURE_SCHEMA)
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(recovery_records)")}
    if "recovery_counts" not in columns:
        connection.execute(
            "ALTER TABLE recovery_records ADD COLUMN recovery_counts TEXT NOT NULL DEFAULT '{}'"
        )
    if "occurrence_index" not in columns:
        connection.execute(
            "ALTER TABLE recovery_records ADD COLUMN occurrence_index TEXT NOT NULL DEFAULT '{}'"
        )
    failure_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(persistent_execution_failures)"
        )
    }
    if "last_run_id" not in failure_columns:
        connection.execute(
            "ALTER TABLE persistent_execution_failures ADD COLUMN last_run_id TEXT"
        )


def begin_external_run(connection: sqlite3.Connection) -> str:
    """Bind one scheduler invocation to this SQLite connection."""

    run_id = uuid.uuid4().hex
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS recovery_external_run "
        "(run_id TEXT NOT NULL)"
    )
    connection.execute("DELETE FROM recovery_external_run")
    connection.execute(
        "INSERT INTO recovery_external_run (run_id) VALUES (?)", (run_id,)
    )
    return run_id


def _external_run_id(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute(
            "SELECT run_id FROM recovery_external_run LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row is not None else None


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
        run_id = _external_run_id(self.connection)
        if run_id is not None:
            self._record_external_failure(
                observation.repo,
                self._subject(observation.plan_id),
                phase=observation.phase,
                reason_code=observation.reason.value,
                fingerprint=observation.fingerprint,
                progress_marker=observation.progress_marker,
                disposition=decision.disposition.value,
                detail=observation.detail,
                recovery_attempts=json.dumps(
                    counts, sort_keys=True, separators=(",", ":"),
                ),
                run_id=run_id,
            )
        return RecoveryRecord(next_generation, decision, evidence)

    def clear(self, repo: str, plan_id: str | None) -> None:
        self.connection.execute(
            "DELETE FROM recovery_records WHERE repo = ? AND subject = ?",
            (repo, self._subject(plan_id)),
        )

    def external_run_snapshot(self, repo: str) -> dict[str, int]:
        """Capture recovery generations before one scheduler invocation."""

        return {
            str(row[0]): int(row[1])
            for row in self.connection.execute(
                "SELECT subject, generation FROM recovery_records WHERE repo = ?",
                (repo,),
            )
        }

    def _record_external_failure(
        self,
        repo: str,
        subject: str,
        *,
        phase: str,
        reason_code: str,
        fingerprint: str,
        progress_marker: str,
        disposition: str,
        detail: str,
        recovery_attempts: str,
        run_id: str | None,
    ) -> None:
        """Durably project one observed failure, once per external run."""

        previous = self.connection.execute(
            "SELECT episode, phase, reason_code, fingerprint, progress_marker, "
            "consecutive_runs, active, notification_queued, last_run_id "
            "FROM persistent_execution_failures WHERE repo = ? AND subject = ?",
            (repo, subject),
        ).fetchone()
        same_cause = (
            previous is not None
            and bool(previous[6])
            and str(previous[1]) == phase
            and str(previous[2]) == reason_code
            and str(previous[3]) == fingerprint
            and str(previous[4]) == progress_marker
        )
        same_run = (
            same_cause and run_id is not None and previous[8] == run_id
        )
        episode = int(previous[0]) if same_cause else (
            int(previous[0]) + 1 if previous is not None else 1
        )
        consecutive_runs = (
            int(previous[5]) if same_run else
            int(previous[5]) + 1 if same_cause else 1
        )
        eligible = (
            consecutive_runs >= 3 or disposition == "user_action_required"
        )
        if previous is None:
            self.connection.execute(
                "INSERT INTO persistent_execution_failures "
                "(repo, subject, episode, phase, reason_code, fingerprint, "
                "progress_marker, consecutive_runs, active, "
                "notification_eligible, notification_queued, suppressed_by_block, "
                "last_run_id, detail, recovery_attempts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0, ?, ?, ?)",
                (
                    repo, subject, episode, phase, reason_code, fingerprint,
                    progress_marker, consecutive_runs, int(eligible), run_id,
                    detail, recovery_attempts,
                ),
            )
            return
        self.connection.execute(
            "UPDATE persistent_execution_failures SET "
            "episode = ?, phase = ?, reason_code = ?, fingerprint = ?, "
            "progress_marker = ?, consecutive_runs = ?, active = 1, "
            "notification_eligible = ?, notification_queued = CASE "
            "WHEN ? THEN notification_queued ELSE 0 END, "
            "suppressed_by_block = 0, last_run_id = ?, detail = ?, "
            "recovery_attempts = ?, updated_at = CURRENT_TIMESTAMP, resolved_at = NULL "
            "WHERE repo = ? AND subject = ?",
            (
                episode, phase, reason_code, fingerprint, progress_marker,
                consecutive_runs, int(eligible), int(same_cause), run_id,
                detail, recovery_attempts, repo, subject,
            ),
        )

    def observe_external_run(
        self,
        repo: str,
        before: dict[str, int],
        *,
        blocked_subjects: frozenset[str] = frozenset(),
    ) -> None:
        """Compatibility boundary for callers without a scheduler run context."""

        changed = [
            row for row in self.connection.execute(
                "SELECT subject, generation, phase, reason_code, fingerprint, "
                "progress_marker, disposition, recovery_counts, detail "
                "FROM recovery_records WHERE repo = ?", (repo,)
            )
            if int(row[1]) > before.get(str(row[0]), 0)
        ]
        for row in changed:
            self._record_external_failure(
                repo, str(row[0]), phase=str(row[2]), reason_code=str(row[3]),
                fingerprint=str(row[4]), progress_marker=str(row[5]),
                disposition=str(row[6]), detail=str(row[8]),
                recovery_attempts=str(row[7]), run_id=None,
            )
        self.finish_external_run(
            repo, None, resolved=not bool(changed), blocked_subjects=blocked_subjects,
        )

    def finish_external_run(
        self,
        repo: str,
        run_id: str | None,
        *,
        resolved: bool,
        blocked_subjects: frozenset[str] = frozenset(),
    ) -> None:
        """Resolve successful attempts or suppress final BLOCKED duplicates."""

        where = "repo = ?" + (" AND last_run_id = ?" if run_id is not None else "")
        parameters: tuple[object, ...] = (repo,) + ((run_id,) if run_id is not None else ())
        for subject in blocked_subjects:
            self.connection.execute(
                "UPDATE persistent_execution_failures SET active = 0, "
                "consecutive_runs = 0, notification_eligible = 0, "
                "notification_queued = 0, suppressed_by_block = 1, "
                "updated_at = CURRENT_TIMESTAMP, resolved_at = CURRENT_TIMESTAMP "
                f"WHERE {where} AND subject = ?",
                (*parameters, subject),
            )
        if resolved:
            placeholders = ", ".join("?" for _ in blocked_subjects)
            exclusion = f" AND subject NOT IN ({placeholders})" if placeholders else ""
            self.connection.execute(
                "UPDATE persistent_execution_failures SET active = 0, "
                "consecutive_runs = 0, notification_eligible = 0, "
                "notification_queued = 0, suppressed_by_block = 0, "
                "updated_at = CURRENT_TIMESTAMP, resolved_at = CURRENT_TIMESTAMP "
                f"WHERE repo = ?{exclusion}",
                (repo, *blocked_subjects),
            )
        if run_id is not None:
            self.connection.execute("DELETE FROM recovery_external_run")

    def clear_correction_occurrences(
        self, repo: str, plan_id: str, fingerprints: tuple[str, ...],
    ) -> None:
        """End successful validation streaks without discarding recovery evidence."""

        if not fingerprints:
            return
        previous = self.row(repo, plan_id)
        if previous is None:
            return
        selected = set(fingerprints)
        index = json.loads(str(previous["occurrence_index"]))
        remaining = {}
        for key, occurrence in index.items():
            reason, fingerprint, _progress = json.loads(key)
            if reason != RecoveryReason.WORKER_CORRECTION.value or fingerprint not in selected:
                remaining[key] = occurrence
        if len(remaining) == len(index):
            return
        changed = self.connection.execute(
            "UPDATE recovery_records SET occurrence_index = ?, "
            "generation = generation + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND subject = ? AND generation = ?",
            (json.dumps(remaining, sort_keys=True, separators=(",", ":")),
             repo, self._subject(plan_id), int(previous["generation"])),
        )
        if changed.rowcount != 1:
            raise RecoveryStateConflict("recovery generation changed")

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
