"""Durable notification outbox and Telegram delivery boundary."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
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


def enqueue_current_project_events(connection: sqlite3.Connection, repo: str) -> None:
    """Enqueue PR/BLOCKED state after its owning update and before commit."""

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

    if project[0] == "PR_OPEN" and project[2]:
        plan = connection.execute(
            "SELECT plan_id, title FROM exec_plans WHERE repo = ? AND path = ?",
            (repo, project[1]),
        ).fetchone()
        if plan:
            _insert(
                connection,
                _key("pr", repo, str(project[2])),
                repo,
                "pr_created",
                {
                    "project": name,
                    "execplan_id": str(plan[0]),
                    "execplan_title": str(plan[1]),
                    "pr_url": str(project[2]),
                },
            )

    cause = project[4] or (project[3] if project[0] == "BLOCKED" else None)
    if not cause:
        return
    plan = connection.execute(
        "SELECT plan_id, title, path, sources FROM exec_plans "
        "WHERE repo = ? AND path = ?",
        (repo, project[1]),
    ).fetchone()
    if plan:
        try:
            sources = json.loads(str(plan[3]))
        except (TypeError, ValueError):
            sources = []
        stage = "ExecPlan"
        plan_id = str(plan[0])
        plan_title = str(plan[1])
        paths = [str(plan[2]), *(str(item) for item in sources)]
    else:
        stage = "planning"
        plan_id = plan_title = None
        paths = [
            "docs/PRODUCT.md",
            "docs/requirements/*.md",
            "docs/ARCHITECTURE.md",
        ]
    _insert(
        connection,
        _key("blocked", repo, f"{stage}\0{plan_id or ''}\0{cause}"),
        repo,
        "blocked",
        {
            "project": name,
            "stage": stage,
            "execplan_id": plan_id,
            "execplan_title": plan_title,
            "cause": str(cause),
            "paths": paths,
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
        return (
            f"[{payload['project']}] BLOCKED ({subject})\n"
            f"{payload['cause']}\nFiles to edit:\n{paths}"
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
        "WHERE sent_at IS NULL ORDER BY created_at, event_key"
    ).fetchall()
    sent = 0
    for row in rows:
        try:
            adapter(token, chat_id, _message(str(row[1]), json.loads(str(row[2]))))
        except Exception as error:
            failure = f"Telegram delivery failed ({type(error).__name__})"
            with connection:
                connection.execute(
                    "UPDATE notification_outbox SET attempts = attempts + 1, "
                    "last_error = ? WHERE event_key = ? AND sent_at IS NULL",
                    (failure, row[0]),
                )
            continue
        with connection:
            connection.execute(
                "UPDATE notification_outbox SET attempts = attempts + 1, "
                "last_error = NULL, sent_at = CURRENT_TIMESTAMP "
                "WHERE event_key = ? AND sent_at IS NULL",
                (row[0],),
            )
        sent += 1
    return sent

