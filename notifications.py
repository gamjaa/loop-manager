"""Durable notification outbox and Telegram delivery boundary."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import reviewer_specification
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
        remote_inspection = (
            f"\0remote\0{plan[11] or 'deleted'}\0{plan[12]}"
            if plan[6] == "published" and plan[12] else ""
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
        _insert(
            connection,
            _key("blocked", repo, f"plan\0{plan[4]}\0{plan_id}" +
                 (f"\0{recovery_identity}" if recovery_identity else "") +
                 remote_inspection),
            repo,
            "blocked",
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
        _insert(
            connection,
            _key(
                "blocked",
                repo,
                f"project\0{project_block[0]}\0{project_block[1] or ''}" +
                (f"\0{recovery_identity}" if recovery_identity else ""),
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
                "recovery": recovery_evidence,
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
    has_historical_catalog = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'execplan_catalog_history'"
    ).fetchone()
    unfinished_query = (
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id) LIMIT 1"
        if has_historical_catalog
        else "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' LIMIT 1"
    )
    unfinished = connection.execute(unfinished_query, (repo,)).fetchone()
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
        "AND sent_at IS NULL AND attempts = 0 "
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
        with connection:
            claimed = connection.execute(
                "UPDATE notification_outbox "
                "SET attempts = attempts + 1, last_error = NULL, "
                "delivery_started_at = CURRENT_TIMESTAMP "
                "WHERE event_key = ? AND sent_at IS NULL"
                + (" AND attempts = 0" if first_attempt_only else ""),
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
