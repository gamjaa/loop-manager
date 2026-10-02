"""Read-only remote observations for blocked ExecPlans.

Processed inputs are deliberately separate: later recovery stages advance them
only after their own work has completed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
import subprocess
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS blocked_remote_observations (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    registered_origin TEXT,
    origin_url TEXT,
    base_ref TEXT,
    branch_ref TEXT,
    base_result TEXT NOT NULL,
    branch_result TEXT NOT NULL,
    base_error TEXT,
    branch_error TEXT,
    base_oid TEXT,
    branch_oid TEXT,
    confirmed_base_oid TEXT,
    confirmed_branch_oid TEXT,
    blocked_reason TEXT,
    blocked_fingerprint TEXT,
    project_blocked_reason TEXT,
    project_blocked_fingerprint TEXT,
    worktree_fingerprint TEXT,
    observed_at TEXT NOT NULL,
    processed_input TEXT,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""

OID = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


@dataclass(frozen=True)
class RefObservation:
    result: str  # present, absent, or error
    oid: str | None = None
    error: str | None = None


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(blocked_remote_observations)")}
    for name in (
        "registered_origin",
        "base_error", "branch_error", "project_blocked_reason",
        "project_blocked_fingerprint",
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE blocked_remote_observations ADD COLUMN {name} TEXT")


def _git_failure_code(returncode: int, stderr: str) -> str:
    """Store a stable cause without persisting remote diagnostics or credentials."""
    message = stderr.lower()
    if any(part in message for part in (
        "authentication failed", "permission denied", "could not read username",
        "terminal prompts disabled", "http basic: access denied",
        "returned error: 401", "returned error: 403",
    )):
        return "authentication"
    if any(part in message for part in (
        "could not resolve", "connection refused",
        "network is unreachable", "connection timed out", "failed to connect",
    )):
        return "network"
    return f"git-exit-{returncode}"


def observe_refs(
    repo: Path, origin_url: str, refs: tuple[str, str], *,
    unavailable_reason: str = "origin-unavailable",
) -> tuple[RefObservation, RefObservation]:
    """Query exact remote heads without updating local refs, index, or worktrees."""
    valid = tuple(ref.startswith("refs/heads/") and ref != "refs/heads/" for ref in refs)
    if not origin_url:
        error = RefObservation("error", error=unavailable_reason)
        return (error, error)
    selected = tuple(ref for ref, is_valid in zip(refs, valid) if is_valid)
    if not selected:
        error = RefObservation("error", error="invalid-ref")
        return (error, error)
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--heads", origin_url, *selected],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        error = RefObservation("error", error="timeout")
        return (error, error)
    except OSError as failure:
        error = RefObservation("error", error=f"os-error-{failure.errno or 'unknown'}")
        return (error, error)
    if result.returncode:
        error = RefObservation("error", error=_git_failure_code(result.returncode, result.stderr))
        return (error, error)
    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[1] not in selected or not OID.fullmatch(parts[0]) or parts[1] in found:
            error = RefObservation("error", error="invalid-response")
            return (error, error)
        found[parts[1]] = parts[0].lower()
    return tuple(
        RefObservation("error", error="invalid-ref") if not is_valid else
        RefObservation("present", found[ref]) if ref in found else RefObservation("absent")
        for ref, is_valid in zip(refs, valid)
    )  # type: ignore[return-value]


def input_identity(row: sqlite3.Row | dict[str, object]) -> str:
    """Stable recovery input; observation timestamp is intentionally excluded."""
    keys = (
        "registered_origin", "origin_url", "base_ref", "branch_ref",
        "base_result", "branch_result",
        "base_error", "branch_error", "base_oid", "branch_oid",
        "blocked_reason", "blocked_fingerprint",
        "project_blocked_reason", "project_blocked_fingerprint",
        "worktree_fingerprint",
    )
    return json.dumps([row[key] for key in keys], ensure_ascii=False, separators=(",", ":"))


def pending_input(row: sqlite3.Row) -> bool:
    """Whether a later recovery stage has not processed this exact input."""
    return row["processed_input"] != input_identity(row)


def record(
    connection: sqlite3.Connection, *, repo: str, plan_id: str,
    registered_origin: str | None, origin_url: str | None,
    base_ref: str, branch_ref: str,
    base: RefObservation, branch: RefObservation,
    blocked_reason: str | None, blocked_fingerprint: str | None,
    project_blocked_reason: str | None, project_blocked_fingerprint: str | None,
    worktree_fingerprint: str | None,
) -> bool:
    """Save this observation and return whether its recovery input changed."""
    previous = connection.execute(
        "SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    values = dict(
        registered_origin=registered_origin, origin_url=origin_url,
        base_ref=base_ref, branch_ref=branch_ref,
        base_result=base.result, branch_result=branch.result,
        base_error=base.error, branch_error=branch.error,
        base_oid=base.oid, branch_oid=branch.oid,
        blocked_reason=blocked_reason, blocked_fingerprint=blocked_fingerprint,
        project_blocked_reason=project_blocked_reason,
        project_blocked_fingerprint=project_blocked_fingerprint,
        worktree_fingerprint=worktree_fingerprint,
    )
    changed = previous is None or input_identity(previous) != input_identity(values)
    connection.execute(
        """INSERT INTO blocked_remote_observations (
            repo, plan_id, registered_origin, origin_url, base_ref, branch_ref,
            base_result, branch_result, base_error, branch_error, base_oid, branch_oid,
            confirmed_base_oid, confirmed_branch_oid, blocked_reason,
            blocked_fingerprint, project_blocked_reason, project_blocked_fingerprint,
            worktree_fingerprint, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, plan_id) DO UPDATE SET
            registered_origin = excluded.registered_origin,
            origin_url = excluded.origin_url, base_ref = excluded.base_ref,
            branch_ref = excluded.branch_ref, base_result = excluded.base_result,
            branch_result = excluded.branch_result,
            base_error = excluded.base_error, branch_error = excluded.branch_error,
            base_oid = excluded.base_oid,
            branch_oid = excluded.branch_oid,
            confirmed_base_oid = COALESCE(excluded.confirmed_base_oid, blocked_remote_observations.confirmed_base_oid),
            confirmed_branch_oid = COALESCE(excluded.confirmed_branch_oid, blocked_remote_observations.confirmed_branch_oid),
            blocked_reason = excluded.blocked_reason,
            blocked_fingerprint = excluded.blocked_fingerprint,
            project_blocked_reason = excluded.project_blocked_reason,
            project_blocked_fingerprint = excluded.project_blocked_fingerprint,
            worktree_fingerprint = excluded.worktree_fingerprint,
            observed_at = excluded.observed_at""",
        (
            repo, plan_id, registered_origin, origin_url, base_ref, branch_ref,
            base.result, branch.result, base.error, branch.error, base.oid, branch.oid,
            base.oid if base.result == "present" else None,
            branch.oid if branch.result == "present" else None,
            blocked_reason, blocked_fingerprint,
            project_blocked_reason, project_blocked_fingerprint,
            worktree_fingerprint,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return changed


def mark_processed(connection: sqlite3.Connection, repo: str, plan_id: str, expected_input: str) -> bool:
    """Future recovery stages can acknowledge only the observation they used."""
    row = connection.execute(
        "SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if row is None or input_identity(row) != expected_input:
        return False
    connection.execute(
        "UPDATE blocked_remote_observations SET processed_input = ? WHERE repo = ? AND plan_id = ?",
        (expected_input, repo, plan_id),
    )
    return True
