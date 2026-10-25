"""Audited, bounded requests to recheck a registered project's local state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS local_environment_recovery_requests (
    request_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    plan_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed')),
    before_state TEXT NOT NULL,
    after_state TEXT,
    result TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS local_environment_recheck_consumption (
    request_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    cause TEXT NOT NULL,
    PRIMARY KEY (request_id, subject, cause)
);
DROP INDEX IF EXISTS one_pending_local_recovery_per_repo;
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_local_recovery_per_plan
ON local_environment_recovery_requests(repo, plan_id)
WHERE status = 'pending' AND plan_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_project_local_recovery
ON local_environment_recovery_requests(repo)
WHERE status = 'pending' AND plan_id IS NULL;
"""


class LocalRecoveryRequestError(RuntimeError):
    """A recovery request cannot be recorded or resumed safely."""


@dataclass(frozen=True)
class LocalRecoveryRequest:
    request_id: str
    repo: str
    plan_id: str | None
    status: str
    resumed: bool


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


def _digest(value: object) -> str:
    raw = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def snapshot(connection: sqlite3.Connection, repo: str, plan_id: str | None) -> dict:
    """Capture cause identities and retry/promotion history without storing secrets."""
    project = connection.execute(
        "SELECT phase, planning_error, pr_conflict_recovery FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    selected_plans = connection.execute(
        "SELECT plan_id, status, blocked_reason, blocked_fingerprint, retry_count, "
        "verify_failure_history, review_failure_history, initial_worker_level, "
        "effective_worker_level, review_level_history, current_revision_sha, plan_sha "
        "FROM exec_plans WHERE repo = ? AND (? IS NULL OR plan_id = ?) ORDER BY plan_id",
        (repo, plan_id, plan_id),
    ).fetchall()
    blocks: list[dict] = []
    for table, scope in (("execplan_blocks", "plan"), ("project_blocks", "project")):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,),
        ).fetchone()
        if exists is None:
            continue
        sql = f"SELECT plan_id, reason_code, evidence, release_fingerprint FROM {table} WHERE repo = ?"
        args: tuple[object, ...] = (repo,)
        if scope == "plan" and plan_id is not None:
            sql += " AND plan_id = ?"
            args += (plan_id,)
        blocks.extend({
            "scope": scope,
            "plan_id": str(row[0]) if row[0] is not None else None,
            "reason_code": str(row[1]),
            "evidence_sha256": _digest(row[2]),
            "release_fingerprint_sha256": _digest(row[3]) if row[3] else None,
        } for row in connection.execute(sql, args))
    recoveries: list[dict] = []
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'",
    ).fetchone()
    if exists is not None:
        rows = connection.execute(
            "SELECT subject, generation, reason_code, disposition, pending_action, phase, "
            "fingerprint, progress_marker, occurrence, attempts, recovery_counts, "
            "occurrence_index, detail FROM recovery_records WHERE repo = ? "
            "AND (? IS NULL OR subject IN (?, '__project__')) ORDER BY subject",
            (repo, plan_id, plan_id),
        )
        recoveries = [{
            "subject": str(row[0]), "generation": int(row[1]),
            "reason_code": str(row[2]), "disposition": str(row[3]),
            "pending_action": str(row[4]), "phase": str(row[5]),
            "fingerprint_sha256": _digest(row[6]),
            "progress_marker_sha256": _digest(row[7]),
            "occurrence": int(row[8]), "attempts": int(row[9]),
            "recovery_counts_sha256": _digest(row[10]),
            "occurrence_index_sha256": _digest(row[11]),
            "detail_sha256": _digest(row[12]),
        } for row in rows]
    plans = [{
        "plan_id": str(row[0]), "status": str(row[1]),
        "blocked_reason": str(row[2]) if row[2] else None,
        "blocked_fingerprint_sha256": _digest(row[3]) if row[3] else None,
        "retry_count": int(row[4]),
        "verify_failure_history_sha256": _digest(row[5]),
        "review_failure_history_sha256": _digest(row[6]),
        "initial_worker_level": int(row[7]) if row[7] is not None else None,
        "effective_worker_level": int(row[8]) if row[8] is not None else None,
        "review_level_history_sha256": _digest(row[9]),
        "revision_sha256": _digest(row[10] or row[11]),
    } for row in selected_plans]
    failures = []
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'persistent_execution_failures'").fetchone():
        failures = [str(row[0]) for row in connection.execute(
            "SELECT subject FROM persistent_execution_failures WHERE repo = ? AND active = 1 "
            "AND (? IS NULL OR subject IN (?, '__project__'))", (repo, plan_id, plan_id))]
    return {
        "current_failures": failures,
        "phase": str(project[0] or "") if project else "",
        "planning_error_sha256": _digest(project[1]) if project and project[1] else None,
        "pr_conflict_recovery": bool(project[2]) if project else False,
        "plans": plans,
        "blocks": sorted(blocks, key=lambda item: (item["scope"], item["plan_id"] or "")),
        "recoveries": recoveries,
    }


def begin_request(
    connection: sqlite3.Connection, repo: str, plan_id: str | None,
) -> LocalRecoveryRequest:
    ensure_schema(connection)
    # A short write transaction serializes intake even across independent CLI
    # processes. Different scopes queue; repeated scopes reuse the same identity.
    from .runtime_state import _transaction
    with _transaction(connection):
        pending = connection.execute(
            "SELECT request_id FROM local_environment_recovery_requests "
            "WHERE repo = ? AND plan_id IS ? AND status = 'pending'",
            (repo, plan_id),
        ).fetchone()
        if pending is not None:
            return LocalRecoveryRequest(str(pending[0]), repo, plan_id, "pending", True)
        before = json.dumps(snapshot(connection, repo, plan_id), sort_keys=True, separators=(",", ":"))
        request_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO local_environment_recovery_requests "
            "(request_id, repo, plan_id, status, before_state) VALUES (?, ?, ?, 'pending', ?)",
            (request_id, repo, plan_id, before),
        )
    return LocalRecoveryRequest(request_id, repo, plan_id, "pending", False)


def pending_requests(connection: sqlite3.Connection, repo: str) -> tuple[LocalRecoveryRequest, ...]:
    """Read a stable intake order without DDL in scheduler worker threads."""
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'local_environment_recovery_requests'",
    ).fetchone():
        return ()
    rows = connection.execute(
        "SELECT request_id, plan_id FROM local_environment_recovery_requests "
        "WHERE repo = ? AND status = 'pending' ORDER BY created_at, rowid", (repo,),
    )
    return tuple(LocalRecoveryRequest(str(row[0]), repo, row[1], "pending", True) for row in rows)


def pending_request(connection: sqlite3.Connection, repo: str) -> LocalRecoveryRequest | None:
    requests = pending_requests(connection, repo)
    return requests[0] if requests else None


def recheck_consumed(connection: sqlite3.Connection, request_id: str, subject: str, cause: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM local_environment_recheck_consumption WHERE request_id = ? AND subject = ? AND cause = ?",
        (request_id, subject, cause),
    ).fetchone() is not None


def consume_recheck(connection: sqlite3.Connection, request_id: str, subject: str, cause: str) -> bool:
    """Claim once in the caller's transaction with the resumed phase transition."""
    row = connection.execute(
        "SELECT 1 FROM local_environment_recovery_requests WHERE request_id = ? AND status = 'pending'",
        (request_id,),
    ).fetchone()
    if row is None:
        raise LocalRecoveryRequestError("유효한 재검사 요청이 없습니다.")
    changed = connection.execute(
        "INSERT OR IGNORE INTO local_environment_recheck_consumption (request_id, subject, cause) VALUES (?, ?, ?)",
        (request_id, subject, cause),
    )
    return changed.rowcount == 1


def _require_pending_request(connection, request: LocalRecoveryRequest) -> None:
    row = connection.execute(
        'SELECT repo,plan_id,status FROM local_environment_recovery_requests WHERE request_id=?',
        (request.request_id,),
    ).fetchone()
    if (row is None or row['repo'] != request.repo or row['plan_id'] != request.plan_id
            or row['status'] != 'pending'):
        raise LocalRecoveryRequestError("로컬 재검사 요청 대상 또는 상태가 변경됐습니다.")


def complete_request(
    connection: sqlite3.Connection, request: LocalRecoveryRequest,
    result: str, after: dict,
) -> dict:
    from .db_access import run_database_request

    def complete(db):
        _require_pending_request(db, request)
        current = snapshot(db, request.repo, request.plan_id)
        if current != after:
            raise LocalRecoveryRequestError("결과 판단 뒤 로컬 재검사 대상 상태가 변경됐습니다.")
        serialized = json.dumps(after, sort_keys=True, separators=(",", ":"))
        changed = db.execute(
            "UPDATE local_environment_recovery_requests SET status = 'completed', "
            "after_state = ?, result = ?, updated_at = CURRENT_TIMESTAMP, "
            "completed_at = CURRENT_TIMESTAMP WHERE request_id = ? AND repo = ? "
            "AND plan_id IS ? AND status = 'pending'",
            (serialized, result, request.request_id, request.repo, request.plan_id),
        )
        if changed.rowcount != 1:
            raise LocalRecoveryRequestError("로컬 재검사 요청 대상 또는 상태가 변경됐습니다.")
        return after

    return run_database_request(connection, complete)


def record_progress(
    connection: sqlite3.Connection, request: LocalRecoveryRequest,
    after: dict,
) -> None:
    """Keep an unfinished bounded request resumable without issuing a new identity."""
    from .db_access import run_database_request

    def record(db):
        _require_pending_request(db, request)
        current = snapshot(db, request.repo, request.plan_id)
        if current != after:
            raise LocalRecoveryRequestError("로컬 재검사 진행 관측 뒤 대상 상태가 변경됐습니다.")
        serialized = json.dumps(current, sort_keys=True, separators=(",", ":"))
        changed = db.execute(
            "UPDATE local_environment_recovery_requests SET after_state = ?, "
            "result = 'in_progress', updated_at = CURRENT_TIMESTAMP WHERE request_id = ? "
            "AND repo = ? AND plan_id IS ? AND status = 'pending'",
            (serialized, request.request_id, request.repo, request.plan_id),
        )
        if changed.rowcount != 1:
            raise LocalRecoveryRequestError("로컬 재검사 요청 대상 또는 상태가 변경됐습니다.")

    run_database_request(connection, record)
