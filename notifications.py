"""Durable notification outbox and Telegram delivery boundary."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import reviewer_specification
from work_exhaustion import WorkReasonSnapshot
from urllib.parse import quote
from urllib.request import Request, urlopen


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
    _adopt_legacy_block_notices(connection)


def _adopt_legacy_block_notices(connection: sqlite3.Connection) -> None:
    """Attach notices from each still-active pre-incident block."""

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
        if current_key is None:
            continue
        if subject == "planning":
            current_error = connection.execute(
                "SELECT planning_error FROM projects WHERE repo = ?", (repo,),
            ).fetchone()
            # Legacy planning notices reused a constant key across incidents.
            # A different saved cause proves this notice belongs to an earlier
            # error; a later Planner invocation proves the old error cleared,
            # even if the next error has identical text.
            if (event_key != current_key or current_error is None
                    or payload.get("cause") != current_error[0]
                    or _planner_started_after_notice(connection, repo, str(row[5]))):
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


def configure_session(connection: sqlite3.Connection, *, enabled: bool) -> None:
    """Store only a connection-local enabled bit, never credentials."""

    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS notification_session "
        "(enabled INTEGER NOT NULL)"
    )
    connection.execute("DELETE FROM notification_session")
    connection.execute(
        "INSERT INTO notification_session (enabled) VALUES (?)",
        (int(enabled),),
    )


def _enabled(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT enabled FROM notification_session LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0])


def _key(kind: str, repo: str, identity: str) -> str:
    digest = hashlib.sha256(f"{repo}\0{identity}".encode()).hexdigest()
    return f"{kind}:{digest}"


def _remote_inspection_suffix(result: object, oid: object, reason: object) -> str:
    if result == "published" and reason:
        return f"\0remote\0{oid or 'deleted'}\0{reason}"
    return ""


def _insert(
    connection: sqlite3.Connection,
    key: str,
    repo: str,
    kind: str,
    payload: dict[str, object],
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO notification_outbox "
        "(event_key, repo, event_type, payload) VALUES (?, ?, ?, ?)",
        (key, repo, kind, json.dumps(payload, ensure_ascii=False)),
    )


def _queue_block(
    connection: sqlite3.Connection, repo: str, subject: str,
    block_key: str, payload: dict[str, object],
) -> None:
    """Give each separately notified block its own durable delivery identity."""

    active = connection.execute(
        "SELECT id, block_key, event_key, confirmed_before, version, payload FROM "
        "notified_block_incidents "
        "WHERE repo = ? AND subject = ? AND released_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (repo, subject),
    ).fetchone()
    if active is not None:
        if active[1] == block_key:
            if connection.execute(
                "SELECT 1 FROM notification_outbox WHERE event_key = ?",
                (active[2],),
            ).fetchone() is None:
                _insert(connection, str(active[2]), repo, "blocked",
                        json.loads(str(active[5])))
            return
        previous = connection.execute(
            "SELECT confirmed_pieces FROM notification_outbox "
            "WHERE event_key = ?", (active[2],),
        ).fetchone()
        confirmed = max(int(active[3]), int(previous[0]) if previous else 0)
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
            "delivery_started_at = NULL WHERE event_key = ? "
            "AND sent_at IS NULL AND canceled_at IS NULL", (active[2],),
        )
        version = int(active[4]) + 1
        event_key = f"{block_key}:{active[0]}:{version}"
        connection.execute(
            "UPDATE notified_block_incidents SET block_key = ?, event_key = ?, "
            "confirmed_before = ?, version = ?, "
            "payload = CASE WHEN ? > 0 THEN payload ELSE ? END "
            "WHERE id = ?",
            (block_key, event_key, confirmed, version, confirmed,
             json.dumps(payload, ensure_ascii=False), active[0]),
        )
        _insert(connection, event_key, repo, "blocked", payload)
        connection.execute(
            "INSERT INTO block_notice_events (event_key, incident_id) "
            "VALUES (?, ?)", (event_key, active[0]),
        )
        return
    cursor = connection.execute(
        "INSERT INTO notified_block_incidents "
        "(repo, subject, block_key, event_key, payload) VALUES (?, ?, ?, ?, ?)",
        (repo, subject, block_key, f"pending:{repo}:{subject}",
         json.dumps(payload, ensure_ascii=False)),
    )
    incident_id = cursor.lastrowid
    event_key = f"{block_key}:{incident_id}:0"
    connection.execute(
        "UPDATE notified_block_incidents SET event_key = ? WHERE id = ?",
        (event_key, incident_id),
    )
    _insert(connection, event_key, repo, "blocked", payload)
    connection.execute(
        "INSERT INTO block_notice_events (event_key, incident_id) "
        "VALUES (?, ?)", (event_key, incident_id),
    )


def release_block(connection: sqlite3.Connection, repo: str, subject: str) -> None:
    """Release an incident and discard undelivered BLOCKED pieces atomically."""

    ensure_schema(connection)
    rows = connection.execute(
        "SELECT id, event_key FROM notified_block_incidents "
        "WHERE repo = ? AND subject = ? AND released_at IS NULL",
        (repo, subject),
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE notified_block_incidents SET released_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (row[0],),
        )
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
            "delivery_started_at = NULL WHERE event_key = ? "
            "AND sent_at IS NULL AND canceled_at IS NULL", (row[1],),
        )


def record_work_start(
    connection: sqlite3.Connection, repo: str, operation: str,
    *, plan_id: str | None = None,
) -> int:
    """Enqueue resumption for released, confirmed incidents at a real start."""

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'project_blocks', 'execplan_blocks', "
            "'notified_block_incidents')"
        )
    }
    if tables != {
        "projects", "project_blocks", "execplan_blocks",
        "notified_block_incidents",
    }:
        return 0
    ensure_schema(connection)
    if connection.execute(
        "SELECT 1 FROM project_blocks WHERE repo = ?", (repo,),
    ).fetchone() is not None:
        return 0
    project = connection.execute(
        "SELECT planning_error FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if project is None or project[0]:
        return 0
    subjects = ["project"]
    if operation == "Planner":
        subjects.append("planning")
    elif plan_id is not None and operation in {"Worker", "PR handling"}:
        subjects.append(f"plan:{plan_id}")
    queued = 0
    for subject in subjects:
        if subject.startswith("plan:") and connection.execute(
            "SELECT 1 FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone() is not None:
            continue
        rows = connection.execute(
            "SELECT id FROM notified_block_incidents "
            "WHERE repo = ? AND subject = ? AND released_at IS NOT NULL "
            "AND resumed_at IS NULL ORDER BY id", (repo, subject),
        ).fetchall()
        for row in rows:
            if not _enabled(connection):
                connection.execute(
                    "UPDATE notified_block_incidents SET resumed_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND resumed_at IS NULL", (row[0],),
                )
                continue
            connection.execute(
                "UPDATE notified_block_incidents SET started_operation = ? "
                "WHERE id = ? AND started_operation IS NULL",
                (operation, row[0]),
            )
            queued += _materialize_resumption(connection, int(row[0]))
    return queued


def _materialize_resumption(connection: sqlite3.Connection, incident_id: int) -> int:
    row = connection.execute(
        "SELECT i.repo, i.payload, i.started_operation, o.confirmed_pieces, "
        "i.confirmed_before "
        "FROM notified_block_incidents i JOIN notification_outbox o "
        "ON o.event_key = i.event_key WHERE i.id = ? "
        "AND i.released_at IS NOT NULL AND i.resumed_at IS NULL",
        (incident_id,),
    ).fetchone()
    if row is None or not row[2] or max(int(row[3]), int(row[4])) == 0:
        return 0
    if not _enabled(connection):
        connection.execute(
            "UPDATE notified_block_incidents SET resumed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND resumed_at IS NULL", (incident_id,),
        )
        return 0
    repo = str(row[0])
    old = json.loads(str(row[1]))
    payload = {
        "project": old["project"], "scope": old["scope"],
        "execplan_id": old.get("execplan_id"),
        "reason_code": old["reason_code"], "cause": old["cause"],
        "operation": str(row[2]),
    }
    _insert(connection, _key("resumed", repo, str(incident_id)), repo,
            "resumed", payload)
    connection.execute(
        "UPDATE notified_block_incidents SET resumed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND resumed_at IS NULL", (incident_id,),
    )
    return 1


def _checkpoint_link(origin: object, oid: object) -> str | None:
    if not origin or not oid:
        return None
    value = str(origin)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return "https://github.com/" + value + "/tree/" + str(oid)
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:")
    elif value.startswith("ssh://git@github.com/"):
        value = "https://github.com/" + value.removeprefix(
            "ssh://git@github.com/"
        )
    if not value.startswith("https://github.com/"):
        return None
    return value.removesuffix(".git") + "/tree/" + str(oid)


def enqueue_current_project_events(connection: sqlite3.Connection, repo: str) -> None:
    """Enqueue all plan/project events after their owning update and before commit."""

    if not _enabled(connection):
        return
    ensure_schema(connection)
    project = connection.execute(
        "SELECT phase, plan_path, pr_url, last_error, planning_error "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        return
    name = Path(repo).name or repo
    recovery_available = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_records'"
    ).fetchone() is not None

    def final_recovery(subject: str) -> tuple[str | None, dict | None]:
        if not recovery_available:
            return None, None
        row = connection.execute(
            "SELECT identity, final_evidence FROM recovery_records "
            "WHERE repo = ? AND subject = ?", (repo, subject),
        ).fetchone()
        if row is None or row[1] is None:
            return None, None
        return str(row[0]), json.loads(str(row[1]))

    for plan in connection.execute(
        "SELECT plan_id, title, pr_url FROM exec_plans "
        "WHERE repo = ? AND status = 'PR_OPEN' AND pr_url IS NOT NULL",
        (repo,),
    ).fetchall():
        _insert(
            connection,
            _key("pr", repo, str(plan[2])),
            repo,
            "pr_created",
            {
                "project": name,
                "execplan_id": str(plan[0]),
                "execplan_title": str(plan[1]),
                "pr_url": str(plan[2]),
            },
        )

    plan_blocks = connection.execute(
        "SELECT p.plan_id, p.title, p.path, p.sources, b.reason_code, b.evidence, "
        "c.result, c.remote_oid, c.unpublished_reason, c.branch, r.origin, "
        "c.inspected_remote_oid, c.inspection_reason "
        "FROM execplan_blocks AS b JOIN exec_plans AS p USING (repo, plan_id) "
        "JOIN projects AS r ON r.repo = b.repo "
        "LEFT JOIN blocked_checkpoints AS c USING (repo, plan_id) "
        "WHERE b.repo = ? AND (c.repo IS NULL OR c.phase IN "
        "('published', 'unpublished')) ORDER BY p.plan_id",
        (repo,),
    ).fetchall()
    for plan in plan_blocks:
        try:
            sources = json.loads(str(plan[3]))
        except (TypeError, ValueError):
            sources = []
        stage = "ExecPlan"
        plan_id = str(plan[0])
        plan_title = str(plan[1])
        recovery_identity, recovery_evidence = final_recovery(plan_id)
        paths = (
            list(recovery_evidence["user_targets"])
            if recovery_evidence is not None
            else [str(plan[2]), *(str(item) for item in sources)]
        )
        deadlock = None
        if plan[4] == "review_specification_deadlock":
            try:
                deadlock = reviewer_specification.parse_evidence(str(plan[5]))
            except ValueError:
                pass
            if deadlock is not None:
                paths = list(deadlock.paths)
        checkpoint = None
        if plan[6] == "published":
            checkpoint = {
                "result": "published",
                "branch": plan[9],
                "branch_url": _checkpoint_link(plan[10], plan[7]),
                "commit_oid": plan[7],
            }
        elif plan[6] == "local_only":
            checkpoint = {
                "result": "local_only",
                "reason": str(plan[8] or "checkpoint를 게시하지 않았습니다."),
            }
        guidance_locations = []
        if plan[4] == "guidance_approval_required":
            rows = connection.execute(
                "SELECT proposal_id, proposal_line, decision_line "
                "FROM guidance_approvals WHERE repo = ? AND plan_id = ? "
                "AND applied = 0 ORDER BY sequence, proposal_id",
                (repo, plan_id),
            ).fetchall()
            guidance_locations = [
                {
                    "proposal_id": str(row[0]),
                    "proposal": f"{plan[2]}:{row[1]}",
                    "user_decision": f"{plan[2]}:{row[2]}",
                }
                for row in rows
            ]
            paths.extend(
                location
                for item in guidance_locations
                for location in (item["proposal"], item["user_decision"])
                if location not in paths
            )
        remote_inspection = _remote_inspection_suffix(
            plan[6], plan[11], plan[12]
        )
        payload = {
            "project": name,
            "stage": stage,
            "scope": "plan",
            "reason_code": str(plan[4]),
            "execplan_id": plan_id,
            "execplan_title": plan_title,
            "cause": str(plan[5]),
            "paths": paths,
            "guidance_locations": guidance_locations,
            "checkpoint": checkpoint,
            "recovery": recovery_evidence,
        }
        if deadlock is not None:
            payload["specification_deadlock"] = {
                "kind": deadlock.kind,
                "locations": [
                    {"path": path, "section": section}
                    for path, section in deadlock.locations
                ],
                "problem": deadlock.problem,
                "implementation_choice": deadlock.implementation_choice,
                "question": deadlock.question,
            }
        suppress_persistent_failure_for_block(connection, repo, plan_id)
        _queue_block(
            connection,
            repo,
            f"plan:{plan_id}",
            _key("blocked", repo, f"plan\0{plan[4]}\0{plan_id}" +
                 (f"\0{recovery_identity}" if recovery_identity else "") +
                 remote_inspection),
            payload,
        )

    project_block = connection.execute(
        "SELECT b.reason_code, b.plan_id, b.evidence, p.title, p.path, p.sources "
        "FROM project_blocks AS b LEFT JOIN exec_plans AS p "
        "ON p.repo = b.repo AND p.plan_id = b.plan_id WHERE b.repo = ?",
        (repo,),
    ).fetchone()
    if project_block is not None:
        recovery_identity, recovery_evidence = final_recovery(
            str(project_block[1] or "__project__")
        )
        try:
            project_sources = json.loads(str(project_block[5])) if project_block[5] else []
        except (TypeError, ValueError):
            project_sources = []
        project_paths = (
            [
                str(project_block[4]),
                *(str(item) for item in project_sources),
            ]
            if project_block[4]
            else []
        )
        if recovery_evidence is not None:
            project_paths = list(recovery_evidence["user_targets"])
        suppress_persistent_failure_for_block(
            connection, repo, str(project_block[1] or "__project__"),
        )
        _queue_block(
            connection,
            repo,
            "project",
            _key(
                "blocked",
                repo,
                f"project\0{project_block[0]}\0{project_block[1] or ''}" +
                (f"\0{recovery_identity}" if recovery_identity else ""),
            ),
            {
                "project": name,
                "stage": "project",
                "scope": "project",
                "reason_code": str(project_block[0]),
                "execplan_id": project_block[1],
                "execplan_title": project_block[3],
                "cause": str(project_block[2]),
                "paths": project_paths,
                "guidance_locations": [],
                "recovery": recovery_evidence,
            },
        )

    if project[4] and project_block is None:
        _queue_block(
            connection,
            repo,
            "planning",
            _key("blocked", repo, "project\0planning_error\0"),
            {
                "project": name,
                "stage": "planning",
                "scope": "project",
                "reason_code": "planning_error",
                "execplan_id": None,
                "execplan_title": None,
                "cause": str(project[4]),
                "paths": [
                    "docs/PRODUCT.md",
                    "docs/requirements/*.md",
                    "docs/ARCHITECTURE.md",
                ],
                "guidance_locations": [],
            },
        )


def enqueue_persistent_execution_failure_events(
    connection: sqlite3.Connection, repo: str,
) -> None:
    """Queue one actionable notice for each eligible external-run streak.

    The recovery projection is retained even when Telegram is disabled.  The
    outbox is intentionally populated only for an enabled notification session,
    matching the established behaviour for all other project events.
    """

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'persistent_execution_failures'"
    ).fetchone()
    if exists is None or not _enabled(connection):
        return
    ensure_schema(connection)
    project = connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        return
    rows = connection.execute(
        "SELECT * FROM persistent_execution_failures WHERE repo = ? "
        "AND active = 1 AND notification_eligible = 1 "
        "AND notification_queued = 0 AND suppressed_by_block = 0 "
        "ORDER BY subject",
        (repo,),
    ).fetchall()
    for row in rows:
        subject = str(row["subject"])
        plan_id = None if subject == "__project__" else subject
        identity = "\0".join((subject, str(row["episode"]), str(row["phase"]),
                              str(row["reason_code"]), str(row["fingerprint"])))
        payload = {
            "project": Path(repo).name or repo,
            "stage": str(row["phase"]),
            "scope": "execution",
            "execplan_id": plan_id,
            "reason_code": str(row["reason_code"]),
            "cause": str(row["detail"]),
            "consecutive_external_runs": int(row["consecutive_runs"]),
            "recovery_attempts": json.loads(str(row["recovery_attempts"])),
            "required_action": (
                "Review the structured failure and fix the reported "
                "configuration, permission, or repository condition."
            ),
        }
        _insert(
            connection, _key("persistent_failure", repo, identity), repo,
            "persistent_execution_failure", payload,
        )
        connection.execute(
            "UPDATE persistent_execution_failures SET notification_queued = 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND subject = ? "
            "AND episode = ? AND notification_eligible = 1",
            (repo, subject, int(row["episode"])),
        )


def cancel_pending_persistent_execution_failure_event(
    connection: sqlite3.Connection, repo: str, subject: str, episode: int,
    phase: str, reason_code: str, fingerprint: str,
) -> None:
    """Withdraw a notice only before its first delivery attempt."""

    identity = "\0".join((subject, str(episode), phase, reason_code, fingerprint))
    connection.execute(
        "DELETE FROM notification_outbox WHERE event_key = ? "
        "AND sent_at IS NULL AND attempts = 0",
        (_key("persistent_failure", repo, identity),),
    )


def suppress_persistent_failure_for_block(
    connection: sqlite3.Connection, repo: str, subject: str,
) -> None:
    """Make a final BLOCKED notice own its pending failure incident."""

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'persistent_execution_failures'"
    ).fetchone() is None:
        return
    row = connection.execute(
        "SELECT episode, phase, reason_code, fingerprint "
        "FROM persistent_execution_failures WHERE repo = ? AND subject = ? "
        "AND active = 1", (repo, subject),
    ).fetchone()
    if row is None:
        return
    cancel_pending_persistent_execution_failure_event(
        connection, repo, subject, int(row[0]), str(row[1]), str(row[2]),
        str(row[3]),
    )
    connection.execute(
        "UPDATE persistent_execution_failures SET active = 0, "
        "consecutive_runs = 0, notification_eligible = 0, "
        "notification_queued = 0, suppressed_by_block = 1, "
        "updated_at = CURRENT_TIMESTAMP, resolved_at = CURRENT_TIMESTAMP "
        "WHERE repo = ? AND subject = ?", (repo, subject),
    )


def record_merged_plan(
    connection: sqlite3.Connection, repo: str, plan_id: str, title: str
) -> None:
    ensure_schema(connection)
    connection.execute(
        """
        INSERT INTO project_completion_cycles
            (repo, last_completed_plan_id, last_completed_title)
        VALUES (?, ?, ?)
        ON CONFLICT(repo) DO UPDATE SET
            last_completed_plan_id = excluded.last_completed_plan_id,
            last_completed_title = excluded.last_completed_title
        """,
        (repo, plan_id, title),
    )


def enqueue_completion_if_ready(
    connection: sqlite3.Connection, repo: str,
    snapshot: WorkReasonSnapshot | None = None,
) -> bool:
    """Record completion only for an already verified stable selection."""

    if snapshot is None or not snapshot.completed:
        return False
    ensure_schema(connection)
    project = connection.execute(
        "SELECT phase, planning_fingerprint, planning_error "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    has_historical_catalog = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'execplan_catalog_history'"
    ).fetchone() is not None
    unfinished_query = (
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id) LIMIT 1"
        if has_historical_catalog else
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' LIMIT 1"
    )
    if (
        project is None
        or project[0] is not None
        or not project[1]
        or str(project[1]) != snapshot.cycle
        or project[2]
        or connection.execute(unfinished_query, (repo,)).fetchone() is not None
        or connection.execute(
            "SELECT 1 FROM project_blocks WHERE repo = ?", (repo,),
        ).fetchone() is not None
    ):
        return False

    cycle_key = str(project[1])
    completion = connection.execute(
        "SELECT last_completed_plan_id, last_completed_title "
        "FROM project_completion_cycles WHERE repo = ?",
        (repo,),
    ).fetchone()
    last_id = str(completion[0]) if completion and completion[0] else None
    last_title = str(completion[1]) if completion and completion[1] else None
    if last_id is None:
        merged = connection.execute(
            "SELECT plan_id, title FROM exec_plans "
            "WHERE repo = ? AND status = 'MERGED'",
            (repo,),
        ).fetchall()
        if merged:
            def plan_order(row: sqlite3.Row) -> tuple[int, str]:
                value = str(row[0])
                try:
                    return int(value.removeprefix("G-")), value
                except ValueError:
                    return -1, value

            latest = max(merged, key=plan_order)
            last_id, last_title = str(latest[0]), str(latest[1])
    last_title = last_title or "Planner confirmed no additional work"
    with connection:
        _cancel_pending_waiting(connection, repo)
        if not _enabled(connection):
            return False
        connection.execute(
            """
            INSERT INTO project_completion_cycles (repo, cycle_key, completed_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo) DO UPDATE SET
                cycle_key = excluded.cycle_key,
                completed_at = excluded.completed_at
            """,
            (repo, cycle_key),
        )
        _insert(
            connection,
            _key("completed", repo, cycle_key),
            repo,
            "project_completed",
            {
                "project": Path(repo).name or repo,
                "reason": snapshot.describe(),
                "last_completed_plan_id": last_id,
                "last_completed_title": last_title,
            },
        )
    return True


def _cancel_pending_waiting(connection: sqlite3.Connection, repo: str) -> None:
    connection.execute(
        "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP "
        "WHERE repo = ? AND event_type = 'work_waiting' "
        "AND sent_at IS NULL AND canceled_at IS NULL",
        (repo,),
    )


def mark_work_available(connection: sqlite3.Connection, repo: str) -> None:
    """End a waiting incident when selection can proceed again."""

    ensure_schema(connection)
    with connection:
        connection.execute(
            "UPDATE work_reason_episodes SET signature = '__work_available__' "
            "WHERE repo = ? AND signature != '__work_available__'", (repo,),
        )
        _cancel_pending_waiting(connection, repo)


def enqueue_waiting_reason(
    connection: sqlite3.Connection, repo: str, snapshot: WorkReasonSnapshot,
) -> bool:
    """Create one waiting incident per stable cause and cause transition."""

    if snapshot.completed:
        return False
    ensure_schema(connection)
    payload = snapshot.payload(Path(repo).name or repo)
    signature = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode()
    ).hexdigest()
    with connection:
        previous = connection.execute(
            "SELECT signature, generation FROM work_reason_episodes WHERE repo = ?",
            (repo,),
        ).fetchone()
        generation = (
            int(previous[1]) if previous and previous[0] == signature
            else int(previous[1]) + 1 if previous else 1
        )
        connection.execute(
            "INSERT INTO work_reason_episodes (repo, signature, generation) "
            "VALUES (?, ?, ?) ON CONFLICT(repo) DO UPDATE SET "
            "signature = excluded.signature, generation = excluded.generation",
            (repo, signature, generation),
        )
        if not snapshot.has_distinct_wait:
            _cancel_pending_waiting(connection, repo)
            return False
        event_key = _key("waiting", repo, f"{generation}\0{signature}")
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND event_type = 'work_waiting' "
            "AND event_key != ? AND sent_at IS NULL AND canceled_at IS NULL",
            (repo, event_key),
        )
        if not _enabled(connection):
            return False
        _insert(connection, event_key, repo, "work_waiting", payload)
    return True


def _message(kind: str, payload: dict[str, object]) -> str:
    if kind == "resumed":
        subject = payload.get("execplan_id") or "project"
        return (
            f"[{payload['project']}] 작업 재개 ({payload['scope']}: {subject})\n"
            f"이전 차단 원인: {payload['reason_code']}\n{payload['cause']}\n"
            f"시작한 작업: {payload['operation']}"
        )
    if kind == "work_waiting":
        lines = [f"[{payload['project']}] 다음 작업 대기"]
        for plan in payload["plans"]:
            lines.append(f"- {plan['id']} {plan['title']}: {plan['reason']}")
            if plan.get("action_targets"):
                lines.append("  사용자 확인 대상: " + ", ".join(plan["action_targets"]))
        for requirement in payload["requirements"]:
            lines.append(f"- {requirement['id']}: {requirement['reason']}")
        if payload.get("project_block"):
            lines.append("PROJECT_BLOCKED: " + payload["project_block"]["reason"])
        if payload.get("planning_error"):
            lines.append("planning_error: " + str(payload["planning_error"]))
        return "\n".join(lines)
    if kind == "pr_created":
        return (
            f"[{payload['project']}] PR created\n"
            f"{payload['execplan_id']}: {payload['execplan_title']}\n"
            f"{payload['pr_url']}"
        )
    if kind == "blocked":
        subject = payload.get("execplan_id") or payload["stage"]
        paths = "\n".join(f"- {path}" for path in payload["paths"])
        recovery = payload.get("recovery")
        recovery_text = ""
        if isinstance(recovery, dict):
            counts = recovery.get("recovery_attempts")
            if isinstance(counts, dict) and counts:
                attempts = ", ".join(
                    f"{reason}: {count}" for reason, count in sorted(counts.items())
                )
                recovery_text = f"\nRecovery attempts: {attempts}"
            stable_revision = recovery.get("last_stable_revision")
            if stable_revision:
                recovery_text += f"\nLast stable revision: {stable_revision}"
        checkpoint = payload.get("checkpoint")
        checkpoint_text = ""
        if isinstance(checkpoint, dict):
            if checkpoint.get("result") == "published":
                checkpoint_text = (
                    "\nCheckpoint: "
                    + str(checkpoint.get("branch_url") or checkpoint.get("branch"))
                    + "\nCommit: "
                    + str(checkpoint.get("commit_oid"))
                )
            else:
                checkpoint_text = "\nCheckpoint: local only (" + str(
                    checkpoint.get("reason")
                ) + ")"
        return (
            f"[{payload['project']}] BLOCKED ({payload.get('scope')}: {subject})\n"
            f"Reason: {payload.get('reason_code')}\n{payload['cause']}"
            + recovery_text
            + (f"\nFiles to edit:\n{paths}" if paths else "")
            + (f"\nQuestion: {payload['specification_deadlock']['question']}"
               if payload.get('specification_deadlock') else "")
            + checkpoint_text
        )
    if kind == "persistent_execution_failure":
        subject = payload.get("execplan_id") or "project"
        attempts = payload.get("recovery_attempts")
        attempt_text = ""
        if isinstance(attempts, dict) and attempts:
            attempt_text = "\nRecovery attempts: " + ", ".join(
                f"{reason}: {count}" for reason, count in sorted(attempts.items())
            )
        return (
            f"[{payload['project']}] Persistent execution failure "
            f"({payload['stage']}: {subject})\n"
            f"Reason: {payload['reason_code']}\n{payload['cause']}"
            f"\nConsecutive external runs: {payload['consecutive_external_runs']}"
            + attempt_text
            + f"\nAction: {payload['required_action']}"
        )
    return (
        f"[{payload['project']}] Project complete\n"
        f"{payload.get('reason', 'Planner confirmed no additional work.')}\n"
        "Last completed work: "
        f"{payload.get('last_completed_plan_id') or '-'} "
        f"{payload['last_completed_title']}"
    )


def send_telegram(token: str, chat_id: str, text: str) -> None:
    endpoint = f"https://api.telegram.org/bot{quote(token, safe=':')}/sendMessage"
    request = Request(
        endpoint,
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("Telegram returned a non-success status")


MESSAGE_LIMIT = 4096


def _pieces(text: str) -> list[str]:
    return [text[start:start + MESSAGE_LIMIT]
            for start in range(0, len(text), MESSAGE_LIMIT)]


def _current_block_key(
    connection: sqlite3.Connection, repo: str, payload: dict[str, object],
) -> str | None:
    """Rebuild the identity of the block currently owning this notice."""

    subject = payload.get("execplan_id")
    reason = payload.get("reason_code")
    if payload.get("stage") == "planning":
        project = connection.execute(
            "SELECT planning_error FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        has_block = connection.execute(
            "SELECT 1 FROM project_blocks WHERE repo = ?", (repo,),
        ).fetchone()
        return (_key("blocked", repo, "project\0planning_error\0")
                if project and project[0] and not has_block else None)
    if payload.get("scope") == "plan":
        block = connection.execute(
            "SELECT b.reason_code, c.result, c.inspected_remote_oid, "
            "c.inspection_reason FROM execplan_blocks b "
            "LEFT JOIN blocked_checkpoints c USING (repo, plan_id) "
            "WHERE b.repo = ? AND b.plan_id = ?", (repo, subject),
        ).fetchone()
        if block is None or block[0] != reason:
            return None
        recovery_subject = str(subject)
        identity = f"plan\0{reason}\0{subject}"
        remote = _remote_inspection_suffix(block[1], block[2], block[3])
    elif payload.get("scope") == "project":
        block = connection.execute(
            "SELECT reason_code, plan_id FROM project_blocks WHERE repo = ?",
            (repo,),
        ).fetchone()
        if block is None or block[0] != reason or block[1] != subject:
            return None
        recovery_subject = str(subject or "__project__")
        identity = f"project\0{reason}\0{subject or ''}"
        remote = ""
    else:
        return None
    recovery_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'recovery_records'"
    ).fetchone()
    if recovery_table:
        recovery = connection.execute(
            "SELECT identity FROM recovery_records WHERE repo = ? "
            "AND subject = ? AND final_evidence IS NOT NULL",
            (repo, recovery_subject),
        ).fetchone()
        if recovery:
            identity += f"\0{recovery[0]}"
    return _key("blocked", repo, identity + remote)


def _cancel_stale_block(
    connection: sqlite3.Connection, event_key: str, repo: str,
    payload: dict[str, object],
) -> bool:
    if event_key == _current_block_key(connection, repo, payload):
        return False
    connection.execute(
        "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
        "delivery_started_at = NULL WHERE event_key = ? "
        "AND sent_at IS NULL AND canceled_at IS NULL",
        (event_key,),
    )
    return True


def _claim_piece(
    connection: sqlite3.Connection, event_key: str, *, first_attempt_only: bool,
) -> tuple[str, int, int] | None:
    with connection:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT repo, event_type, payload, rendered_text, confirmed_pieces, "
            "attempts FROM notification_outbox WHERE event_key = ? "
            "AND sent_at IS NULL AND canceled_at IS NULL",
            (event_key,),
        ).fetchone()
        if row is None or (first_attempt_only and row[5] != 0):
            return None
        payload = json.loads(str(row[2]))
        if row[1] == "blocked":
            incident = connection.execute(
                "SELECT released_at, block_key FROM notified_block_incidents "
                "WHERE event_key = ?", (event_key,),
            ).fetchone()
            if incident is not None and incident[0] is not None:
                connection.execute(
                    "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
                    "delivery_started_at = NULL WHERE event_key = ? "
                    "AND sent_at IS NULL AND canceled_at IS NULL",
                    (event_key,),
                )
                return None
            if (incident is None or incident[1] != _current_block_key(
                connection, str(row[0]), payload
            )) and _cancel_stale_block(connection, event_key, str(row[0]), payload):
                return None
        text = str(row[3]) if row[3] is not None else _message(str(row[1]), payload)
        pieces = _pieces(text)
        cursor = int(row[4])
        if cursor >= len(pieces):
            return None
        claimed = connection.execute(
            "UPDATE notification_outbox SET rendered_text = ?, "
            "attempts = attempts + 1, last_error = NULL, "
            "delivery_started_at = CURRENT_TIMESTAMP "
            "WHERE event_key = ? AND sent_at IS NULL AND canceled_at IS NULL "
            "AND confirmed_pieces = ?"
            + (" AND attempts = 0" if first_attempt_only else ""),
            (text, event_key, cursor),
        ).rowcount
        return (pieces[cursor], cursor, len(pieces)) if claimed == 1 else None


def dispatch_pending(
    connection: sqlite3.Connection,
    token: str,
    chat_id: str,
    *,
    adapter: Callable[[str, str, str], None] = send_telegram,
) -> int:
    """Deliver pending rows; delivery errors remain secret-free and isolated."""

    if not token or not chat_id:
        return 0
    ensure_schema(connection)
    rows = connection.execute(
        "SELECT event_key, event_type, payload FROM notification_outbox "
        "WHERE sent_at IS NULL AND canceled_at IS NULL "
        "ORDER BY created_at, event_key"
    ).fetchall()
    return _deliver_rows(connection, token, chat_id, rows, adapter)


def dispatch_new_pr_notifications(
    connection: sqlite3.Connection,
    token: str,
    chat_id: str,
    repo: str,
    *,
    adapter: Callable[[str, str, str], None] = send_telegram,
) -> int:
    """Deliver newly committed PR events before this project's next step."""

    if not token or not chat_id:
        return 0
    if connection.in_transaction:
        # Never publish an event before its owning state change commits.
        return 0
    rows = connection.execute(
        "SELECT event_key, event_type, payload FROM notification_outbox "
        "WHERE repo = ? AND event_type = 'pr_created' "
        "AND sent_at IS NULL AND canceled_at IS NULL AND attempts = 0 "
        "ORDER BY created_at, event_key",
        (repo,),
    ).fetchall()
    return _deliver_rows(
        connection, token, chat_id, rows, adapter, first_attempt_only=True
    )


def _deliver_rows(
    connection: sqlite3.Connection,
    token: str,
    chat_id: str,
    rows: list[sqlite3.Row],
    adapter: Callable[[str, str, str], None],
    *,
    first_attempt_only: bool = False,
) -> int:
    sent = 0
    for row in rows:
        first_piece = True
        while True:
            claimed = _claim_piece(
                connection, str(row[0]),
                first_attempt_only=first_attempt_only and first_piece,
            )
            if claimed is None:
                break
            piece, cursor, total = claimed
            first_piece = False
            try:
                adapter(token, chat_id, piece)
            except Exception as error:
                failure = f"Telegram delivery failed ({type(error).__name__})"
                with connection:
                    connection.execute(
                        "UPDATE notification_outbox SET delivery_started_at = NULL, "
                        "last_error = ? WHERE event_key = ? AND sent_at IS NULL "
                        "AND confirmed_pieces = ?",
                        (failure, row[0], cursor),
                    )
                break
            with connection:
                connection.execute(
                    "UPDATE notification_outbox SET confirmed_pieces = ?, "
                    "delivery_started_at = CASE WHEN ? = ? "
                    "THEN delivery_started_at ELSE NULL END, last_error = NULL, "
                    "sent_at = CASE WHEN ? = ? THEN CURRENT_TIMESTAMP "
                    "ELSE NULL END WHERE event_key = ? AND sent_at IS NULL "
                    "AND confirmed_pieces = ?",
                    (cursor + 1, cursor + 1, total, cursor + 1, total,
                     row[0], cursor),
                )
                if row[1] == "blocked":
                    incident = connection.execute(
                        "SELECT incident_id FROM block_notice_events "
                        "WHERE event_key = ?", (row[0],),
                    ).fetchone()
                    if incident is not None:
                        connection.execute(
                            "UPDATE notified_block_incidents SET "
                            "payload = CASE WHEN confirmed_before = 0 AND "
                            "NOT EXISTS (SELECT 1 FROM notification_outbox current "
                            "WHERE current.event_key = notified_block_incidents.event_key "
                            "AND current.confirmed_pieces > 0) "
                            "THEN ? ELSE payload END, "
                            "confirmed_before = MAX(confirmed_before, ?) "
                            "WHERE id = ? AND event_key != ?",
                            (row[2], cursor + 1, incident[0], row[0]),
                        )
                        _materialize_resumption(connection, int(incident[0]))
            if cursor + 1 == total:
                sent += 1
                break
    return sent
