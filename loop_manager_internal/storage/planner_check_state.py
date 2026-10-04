"""Durable input and target bound read-only Planner check decisions."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot


SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_checks (
    repo TEXT PRIMARY KEY,
    input_fingerprint TEXT NOT NULL,
    specification_fingerprint TEXT NOT NULL,
    target_fingerprint TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    comparison_sha TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('pending', 'result', 'accepted', 'handoff', 'superseded')),
    session_id TEXT,
    result TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_check_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    specification_fingerprint TEXT NOT NULL,
    target_fingerprint TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    comparison_sha TEXT NOT NULL,
    stage TEXT NOT NULL,
    session_id TEXT,
    result TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    connection.execute(HISTORY_SCHEMA)


def target_fingerprint(snapshot: MutableReadySnapshot) -> str:
    """Ignore a revision-only advance; include all mutable plan declarations."""
    plans = [
        (plan.plan_id, plan.path, plan.content_hash, plan.title,
         plan.sources, plan.depends_on, plan.complexity, plan.risk,
         plan.priority, plan.current_complexity, plan.current_risk)
        for plan in snapshot.plans
    ]
    return hashlib.sha256(json.dumps(
        plans, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def get(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    ensure_schema(connection)
    return connection.execute(
        "SELECT * FROM planner_checks WHERE repo = ?", (repo,),
    ).fetchone()


def begin(
    connection: sqlite3.Connection, repo: str, *, input_fingerprint: str,
    specification_fingerprint: str, target_fingerprint: str,
    base_sha: str, comparison_sha: str,
) -> None:
    ensure_schema(connection)
    previous = get(connection, repo)
    if previous is not None and (
        previous["input_fingerprint"] != input_fingerprint
        or previous["target_fingerprint"] != target_fingerprint
        or previous["stage"] != "pending"
    ):
        connection.execute(
            "INSERT INTO planner_check_history (repo, input_fingerprint, specification_fingerprint, "
            "target_fingerprint, base_sha, comparison_sha, stage, session_id, result, error, retry_count) "
            "SELECT repo, input_fingerprint, specification_fingerprint, target_fingerprint, "
            "base_sha, comparison_sha, stage, "
            "session_id, result, error, retry_count FROM planner_checks WHERE repo = ?",
            (repo,),
        )
    connection.execute(
        "INSERT INTO planner_checks (repo, input_fingerprint, specification_fingerprint, "
        "target_fingerprint, base_sha, comparison_sha, stage) VALUES (?, ?, ?, ?, ?, ?, 'pending') "
        "ON CONFLICT(repo) DO UPDATE SET input_fingerprint=excluded.input_fingerprint, "
        "specification_fingerprint=excluded.specification_fingerprint, "
        "target_fingerprint=excluded.target_fingerprint, base_sha=excluded.base_sha, "
        "comparison_sha=excluded.comparison_sha, stage='pending', session_id=NULL, "
        "result=NULL, error=NULL, retry_count=0",
        (repo, input_fingerprint, specification_fingerprint, target_fingerprint,
         base_sha, comparison_sha),
    )


def advance(
    connection: sqlite3.Connection, repo: str, expected_stage: str,
    next_stage: str, *, expected_input: str | None = None,
    expected_target: str | None = None, **values: object,
) -> bool:
    columns = ", ".join(f"{name} = ?" for name in values)
    set_clause = "stage = ?" + (", " + columns if columns else "")
    where = "repo = ? AND stage = ?"
    expected: list[object] = [repo, expected_stage]
    if expected_input is not None:
        where += " AND input_fingerprint = ?"
        expected.append(expected_input)
    if expected_target is not None:
        where += " AND target_fingerprint = ?"
        expected.append(expected_target)
    cursor = connection.execute(
        f"UPDATE planner_checks SET {set_clause} WHERE {where}",
        (next_stage, *values.values(), *expected),
    )
    return cursor.rowcount == 1
