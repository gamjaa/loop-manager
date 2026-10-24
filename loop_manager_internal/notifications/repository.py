"""Notification schema, outbox persistence, and session flag."""
from __future__ import annotations
import json
import sqlite3

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_outbox (
    event_key TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivery_started_at TEXT,
    sent_at TEXT,
    rendered_text TEXT,
    confirmed_pieces INTEGER NOT NULL DEFAULT 0,
    canceled_at TEXT,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""
COMPLETION_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_completion_cycles (
    repo TEXT PRIMARY KEY,
    cycle_key TEXT,
    last_completed_plan_id TEXT,
    last_completed_title TEXT,
    completed_at TEXT,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""
WORK_REASON_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_reason_episodes (
    repo TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    generation INTEGER NOT NULL,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""
PR_REVIEW_CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pr_review_candidates (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    publication_token TEXT NOT NULL,
    review_input_identity TEXT NOT NULL,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""
BLOCK_INCIDENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS notified_block_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    subject TEXT NOT NULL,
    block_key TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    released_at TEXT,
    resumed_at TEXT,
    started_operation TEXT,
    started_at TEXT,
    start_identity TEXT,
    confirmed_before INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""
BLOCK_NOTICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS block_notice_events (
    event_key TEXT PRIMARY KEY,
    incident_id INTEGER NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES notified_block_incidents(id)
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(OUTBOX_SCHEMA)
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(notification_outbox)")
    }
    if "delivery_started_at" not in columns:
        connection.execute(
            "ALTER TABLE notification_outbox ADD COLUMN delivery_started_at TEXT"
        )
    if "rendered_text" not in columns:
        connection.execute(
            "ALTER TABLE notification_outbox ADD COLUMN rendered_text TEXT"
        )
    if "confirmed_pieces" not in columns:
        connection.execute(
            "ALTER TABLE notification_outbox ADD COLUMN confirmed_pieces "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "canceled_at" not in columns:
        connection.execute(
            "ALTER TABLE notification_outbox ADD COLUMN canceled_at TEXT"
        )
    connection.execute(COMPLETION_SCHEMA)
    connection.execute(WORK_REASON_SCHEMA)
    connection.execute(PR_REVIEW_CANDIDATE_SCHEMA)
    connection.execute(BLOCK_INCIDENT_SCHEMA)
    connection.execute(BLOCK_NOTICE_SCHEMA)
    incident_columns = {
        str(row[1]) for row in connection.execute(
            "PRAGMA table_info(notified_block_incidents)"
        )
    }
    if "started_operation" not in incident_columns:
        connection.execute(
            "ALTER TABLE notified_block_incidents ADD COLUMN started_operation TEXT"
        )
    if "start_identity" not in incident_columns:
        connection.execute("ALTER TABLE notified_block_incidents ADD COLUMN start_identity TEXT")
    if "started_at" not in incident_columns:
        connection.execute("ALTER TABLE notified_block_incidents ADD COLUMN started_at TEXT")
    if "confirmed_before" not in incident_columns:
        connection.execute(
            "ALTER TABLE notified_block_incidents ADD COLUMN confirmed_before "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "version" not in incident_columns:
        connection.execute(
            "ALTER TABLE notified_block_incidents ADD COLUMN version "
            "INTEGER NOT NULL DEFAULT 0"
        )


def adopt_legacy_block_notices(connection: sqlite3.Connection) -> None:
    """Attach pre-incident notices to the blocks they still represent.

    This is a current-state incident migration, rather than schema setup.  The
    event projection owns when it runs so preparing an SQLite connection cannot
    alter recovery or delivery state by itself.
    """

    required = {
        "projects", "project_blocks", "execplan_blocks", "blocked_checkpoints",
    }
    available = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'project_blocks', 'execplan_blocks', "
            "'blocked_checkpoints')"
        )
    }
    if available != required:
        return
    from .incidents import _current_block_key

    rows = connection.execute(
        "SELECT o.event_key, o.repo, o.payload, o.sent_at, o.confirmed_pieces, "
        "o.created_at "
        "FROM notification_outbox AS o WHERE o.event_type = 'blocked' "
        "AND o.canceled_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM block_notice_events AS n "
        "WHERE n.event_key = o.event_key) ORDER BY o.rowid"
    ).fetchall()
    groups: dict[tuple[str, str], list[tuple[sqlite3.Row, dict[str, object]]]] = {}
    for row in rows:
        try:
            payload = json.loads(str(row[2]))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("stage") == "planning":
            subject = "planning"
        elif payload.get("scope") == "plan" and payload.get("execplan_id"):
            subject = f"plan:{payload['execplan_id']}"
        elif payload.get("scope") == "project":
            subject = "project"
        else:
            continue
        repo = str(row[1])
        event_key = str(row[0])
        current_key = _current_block_key(connection, repo, payload)
        if subject == "planning":
            current_error = connection.execute(
                "SELECT planning_error FROM projects WHERE repo = ?", (repo,),
            ).fetchone()
            # Legacy planning notices reused a constant key across incidents.
            # A different saved cause proves this notice belongs to an earlier
            # error; a later Planner invocation proves the old error cleared,
            # even if the next error has identical text.
            if (current_key is None or event_key != current_key
                    or current_error is None
                    or payload.get("cause") != current_error[0]
                    or _planner_started_after_notice(connection, repo, str(row[5]))):
                # The legacy key still matches a new planning error, so its
                # unsent pieces must be canceled before dispatch sees it.
                connection.execute(
                    "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
                    "delivery_started_at = NULL WHERE event_key = ? "
                    "AND sent_at IS NULL AND canceled_at IS NULL", (event_key,),
                )
                continue
        elif current_key is None:
            continue
        elif subject.startswith("plan:"):
            block = connection.execute(
                "SELECT reason_code, created_at FROM execplan_blocks "
                "WHERE repo = ? AND plan_id = ?",
                (repo, payload["execplan_id"]),
            ).fetchone()
            if (block is None or block[0] != payload.get("reason_code")
                    or str(row[5]) < str(block[1])):
                continue
        else:
            block = connection.execute(
                "SELECT reason_code, plan_id, created_at FROM project_blocks "
                "WHERE repo = ?", (repo,),
            ).fetchone()
            if (block is None or block[0] != payload.get("reason_code")
                    or block[1] != payload.get("execplan_id")
                    or str(row[5]) < str(block[2])):
                continue
        groups.setdefault((repo, subject), []).append((row, payload))

    for (repo, subject), notices in groups.items():
        active = connection.execute(
            "SELECT 1 FROM notified_block_incidents WHERE repo = ? "
            "AND subject = ? AND released_at IS NULL",
            (repo, subject),
        ).fetchone()
        if active is not None:
            continue
        current_key = _current_block_key(connection, repo, notices[-1][1])
        current = next(
            (item for item in notices if str(item[0][0]) == current_key),
            notices[-1],
        )
        confirmed = next(
            (item for item in notices if item[0][3] or int(item[0][4]) > 0),
            None,
        )
        event_key = str(current[0][0])
        cursor = connection.execute(
            "INSERT INTO notified_block_incidents "
            "(repo, subject, block_key, event_key, payload, confirmed_before) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo, subject, event_key, event_key,
             str((confirmed or current)[0][2]), int(confirmed is not None)),
        )
        for row, _payload in notices:
            connection.execute(
                "INSERT INTO block_notice_events (event_key, incident_id) "
                "VALUES (?, ?)", (row[0], cursor.lastrowid),
            )
            if str(row[0]) != event_key:
                connection.execute(
                    "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
                    "delivery_started_at = NULL WHERE event_key = ? "
                    "AND sent_at IS NULL AND canceled_at IS NULL", (row[0],),
                )


def _planner_started_after_notice(
    connection: sqlite3.Connection, repo: str, notice_created_at: str,
) -> bool:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'agent_invocations'"
    ).fetchone() is None:
        return False
    # Outbox timestamps have second precision. Treat same-second starts as
    # ambiguous instead of mistaking the invocation that caused the notice
    # for a later episode.
    return connection.execute(
        "SELECT 1 FROM agent_invocations WHERE repo = ? "
        "AND role IN ('planner', 'maintenance_planner') "
        "AND julianday(started_at) >= julianday(?) + 1.0 / 86400 LIMIT 1",
        (repo, notice_created_at),
    ).fetchone() is not None


def configure_session(connection: sqlite3.Connection, *, enabled: bool,
                      verify_timeout_seconds: int | None = None) -> None:
    """Bind non-secret notification settings to this connection, never credentials."""

    if type(verify_timeout_seconds) is not int or verify_timeout_seconds <= 0:
        verify_timeout_seconds = None
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS notification_session "
        "(enabled INTEGER NOT NULL, verify_timeout_seconds INTEGER)"
    )
    columns = {str(row[1]) for row in connection.execute('PRAGMA table_info(notification_session)')}
    if 'verify_timeout_seconds' not in columns:
        connection.execute('ALTER TABLE notification_session ADD COLUMN verify_timeout_seconds INTEGER')
    connection.execute("DELETE FROM notification_session")
    connection.execute(
        "INSERT INTO notification_session (enabled, verify_timeout_seconds) VALUES (?, ?)",
        (int(enabled), verify_timeout_seconds),
    )


def _enabled(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT enabled FROM notification_session LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0])


def _insert(
    connection: sqlite3.Connection,
    key: str,
    repo: str,
    kind: str,
    payload: dict[str, object],
) -> None:
    payload = _event_payload(kind, payload)
    if kind != "pr_created":
        connection.execute(
            "INSERT OR IGNORE INTO notification_outbox "
            "(event_key, repo, event_type, payload) VALUES (?, ?, ?, ?)",
            (key, repo, kind, _encode_payload(payload)),
        )
        return

    versions = connection.execute(
        "SELECT event_key, payload, attempts, last_error, rendered_text, "
        "confirmed_pieces, canceled_at, sent_at FROM notification_outbox "
        "WHERE repo=? AND event_type=? AND "
        "(event_key=? OR event_key LIKE ?) ORDER BY rowid",
        (repo, kind, key, f"{key}:v%"),
    ).fetchall()
    if not versions:
        connection.execute(
            "INSERT OR IGNORE INTO notification_outbox "
            "(event_key, repo, event_type, payload) VALUES (?, ?, ?, ?)",
            (key, repo, kind, _encode_payload(payload)),
        )
        return
    if any(row[7] is not None or row[6] is None for row in versions):
        # A sent or currently pending version already represents this action.
        return

    previous = versions[-1]
    try:
        original = json.loads(str(previous[1]))
    except (TypeError, ValueError):
        original = {}
    if isinstance(original, dict) and original.get("occurred_at"):
        payload["occurred_at"] = original["occurred_at"]
    payload["continued_from"] = str(previous[0])
    connection.execute(
        "INSERT OR IGNORE INTO notification_outbox "
        "(event_key, repo, event_type, payload, attempts, last_error, "
        "rendered_text, confirmed_pieces) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"{key}:v{len(versions)}", repo, kind, _encode_payload(payload),
         previous[2], previous[3], previous[4], previous[5]),
    )


def _encode_payload(payload: dict[str, object]) -> str:
    """Use the same JSON representation for incidents and outbox rows."""
    return json.dumps(payload, ensure_ascii=False)


def _event_payload(kind: str, payload: dict[str, object]) -> dict[str, object]:
    from datetime import datetime, timezone
    payload = dict(payload)
    payload.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("silent", kind in {"resumed", "project_completed", "work_started", "planning_result"})
    payload.setdefault("status", {"pr_created": "PR 검토·Merge 대기", "blocked": "사용자 조치 대기",
                                  "resumed": "작업 중", "project_completed": "완료"}.get(kind, "대기"))
    return payload
