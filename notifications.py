"""Durable notification outbox and Telegram delivery boundary."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import re
from pathlib import Path
import sqlite3
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
    connection.execute(COMPLETION_SCHEMA)


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
        "c.result, c.remote_oid, c.unpublished_reason, c.branch, r.origin "
        "FROM execplan_blocks AS b JOIN exec_plans AS p USING (repo, plan_id) "
        "JOIN projects AS r ON r.repo = b.repo "
        "LEFT JOIN blocked_checkpoints AS c USING (repo, plan_id) "
        "WHERE b.repo = ? AND (c.repo IS NULL OR c.phase IN "
        "('published', 'unpublished', 'retry_pending')) ORDER BY p.plan_id",
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
        paths = [str(plan[2]), *(str(item) for item in sources)]
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
            )
        _insert(
            connection,
            _key("blocked", repo, f"plan\0{plan[4]}\0{plan_id}"),
            repo,
            "blocked",
            {
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
            },
        )

    project_block = connection.execute(
        "SELECT b.reason_code, b.plan_id, b.evidence, p.title, p.path, p.sources "
        "FROM project_blocks AS b LEFT JOIN exec_plans AS p "
        "ON p.repo = b.repo AND p.plan_id = b.plan_id WHERE b.repo = ?",
        (repo,),
    ).fetchone()
    if project_block is not None:
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
        _insert(
            connection,
            _key(
                "blocked",
                repo,
                f"project\0{project_block[0]}\0{project_block[1] or ''}",
            ),
            repo,
            "blocked",
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
            },
        )

    if project[4] and project_block is None:
        _insert(
            connection,
            _key("blocked", repo, "project\0planning_error\0"),
            repo,
            "blocked",
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


def enqueue_completion_if_ready(connection: sqlite3.Connection, repo: str) -> bool:
    """Record one completion event for a Planner-confirmed input cycle."""

    if not _enabled(connection):
        return False
    ensure_schema(connection)
    project = connection.execute(
        "SELECT phase, planning_fingerprint, planning_error "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    unfinished = connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' LIMIT 1",
        (repo,),
    ).fetchone()
    if (
        project is None
        or project[0] is not None
        or not project[1]
        or project[2]
        or unfinished is not None
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
                "last_completed_plan_id": last_id,
                "last_completed_title": last_title,
            },
        )
    return True


def _message(kind: str, payload: dict[str, object]) -> str:
    if kind == "pr_created":
        return (
            f"[{payload['project']}] PR created\n"
            f"{payload['execplan_id']}: {payload['execplan_title']}\n"
            f"{payload['pr_url']}"
        )
    if kind == "blocked":
        subject = payload.get("execplan_id") or payload["stage"]
        paths = "\n".join(f"- {path}" for path in payload["paths"])
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
            + (f"\nFiles to edit:\n{paths}" if paths else "")
            + checkpoint_text
        )
    return (
        f"[{payload['project']}] Project complete\nLast completed work: "
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
        "WHERE sent_at IS NULL "
        "ORDER BY created_at, event_key"
    ).fetchall()
    sent = 0
    for row in rows:
        with connection:
            claimed = connection.execute(
                "UPDATE notification_outbox "
                "SET attempts = attempts + 1, last_error = NULL, "
                "delivery_started_at = CURRENT_TIMESTAMP "
                "WHERE event_key = ? AND sent_at IS NULL",
                (row[0],),
            ).rowcount
        if claimed != 1:
            continue
        try:
            adapter(token, chat_id, _message(str(row[1]), json.loads(str(row[2]))))
        except Exception as error:
            failure = f"Telegram delivery failed ({type(error).__name__})"
            with connection:
                connection.execute(
                    "UPDATE notification_outbox SET delivery_started_at = NULL, "
                    "last_error = ? WHERE event_key = ? AND sent_at IS NULL",
                    (failure, row[0]),
                )
            continue
        with connection:
            connection.execute(
                "UPDATE notification_outbox SET last_error = NULL, "
                "sent_at = CURRENT_TIMESTAMP WHERE event_key = ? "
                "AND sent_at IS NULL AND delivery_started_at IS NOT NULL",
                (row[0],),
            )
        sent += 1
    return sent
