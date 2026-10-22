"""Durable block incidents, identities, release and resumption."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from .repository import (adopt_legacy_block_notices, ensure_schema,
                         _insert, _encode_payload, _event_payload, _planner_started_after_notice)
from .rendering import _checkpoint_link
from .policy import block_action

def _key(kind: str, repo: str, identity: str) -> str:
    digest = hashlib.sha256(f"{repo}\0{identity}".encode()).hexdigest()
    return f"{kind}:{digest}"


def _remote_inspection_suffix(result: object, oid: object, reason: object) -> str:
    if result == "published" and reason:
        return f"\0remote\0{oid or 'deleted'}\0{reason}"
    return ""


def _plan_block_key(
    repo: str, reason: object, plan_id: object,
    recovery_identity: object = None, remote_suffix: str = "",
) -> str:
    identity = f"plan\0{reason}\0{plan_id}"
    if recovery_identity:
        identity += f"\0{recovery_identity}"
    return _key("blocked", repo, identity + remote_suffix)


def _project_block_key(
    repo: str, reason: object, plan_id: object,
    recovery_identity: object = None, checkpoint_suffix: str = "",
) -> str:
    identity = f"project\0{reason}\0{plan_id or ''}"
    if recovery_identity:
        identity += f"\0{recovery_identity}"
    return _key("blocked", repo, identity + checkpoint_suffix)


def _carry_planning_delivery(
    connection: sqlite3.Connection, repo: str, incident_id: int,
    block_key: str, cause: object,
) -> bool:
    """Preserve the first confirmed notice in the current planning episode."""

    rows = connection.execute(
        "SELECT i.id, i.block_key, i.event_key, i.payload, i.resumed_at, "
        "i.confirmed_before, o.confirmed_pieces, o.sent_at, o.created_at, "
        "i.started_operation "
        "FROM notified_block_incidents i LEFT JOIN notification_outbox o "
        "ON o.event_key = i.event_key "
        "WHERE i.repo = ? AND i.subject = 'planning' AND i.id <= ? "
        "ORDER BY i.id DESC", (repo, incident_id),
    ).fetchall()
    confirmed_payload = None
    confirmed_from_older = False
    for row in rows:
        try:
            same_cause = (row[1] == block_key
                          and json.loads(str(row[3])).get("cause") == cause)
        except (TypeError, ValueError, AttributeError):
            same_cause = False
        if (not same_cause or row[4] is not None or row[9] is not None
                or (row[8] is not None and _planner_started_after_notice(
                    connection, repo, str(row[8]),
                ))):
            break
        if (int(row[5]) > 0 or int(row[6] or 0) > 0 or row[7] is not None):
            confirmed_payload = row[3]
            confirmed_from_older = int(row[0]) != incident_id
    if confirmed_payload is None:
        return False
    connection.execute(
        "UPDATE notified_block_incidents SET confirmed_before = 1, "
        "payload = ? WHERE id = ?",
        (confirmed_payload, incident_id),
    )
    if confirmed_from_older:
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
            "delivery_started_at = NULL WHERE event_key = ? "
            "AND sent_at IS NULL AND canceled_at IS NULL",
            (rows[0][2],),
        )
    return True


def _queue_block(
    connection: sqlite3.Connection, repo: str, subject: str,
    block_key: str, payload: dict[str, object],
) -> None:
    """Give each separately notified block its own durable delivery identity."""

    action = block_action(connection, repo, payload)
    if action is None:
        return
    payload = {**payload, "action": action}
    if action['kind'] == 'unrecoverable':
        payload['status'] = '자동 복구 불가'
    payload = _event_payload("blocked", payload)

    active = connection.execute(
        "SELECT id, block_key, event_key, confirmed_before, version, "
        "COALESCE((SELECT payload FROM notification_outbox o WHERE "
        "o.event_key=notified_block_incidents.event_key), payload) FROM "
        "notified_block_incidents "
        "WHERE repo = ? AND subject = ? AND released_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (repo, subject),
    ).fetchone()
    if active is not None:
        try:
            saved = json.loads(str(active[5]))
        except (TypeError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        if active[1] == block_key and (
            saved.get('action') == action or block_action(connection, repo, saved) == action
        ):
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
             _encode_payload(payload), active[0]),
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
         _encode_payload(payload)),
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
    adopt_legacy_block_notices(connection)
    if subject == "planning":
        current = connection.execute(
            "SELECT id, block_key, payload FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning' AND released_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (repo,),
        ).fetchone()
        if current is not None:
            _carry_planning_delivery(
                connection, repo, int(current[0]), str(current[1]),
                json.loads(str(current[2])).get("cause"),
            )
    rows = connection.execute(
        "SELECT i.id, i.event_key, i.payload, i.confirmed_before, "
        "COALESCE(o.confirmed_pieces, 0) FROM notified_block_incidents i "
        "LEFT JOIN notification_outbox o ON o.event_key = i.event_key "
        "WHERE i.repo = ? AND i.subject = ? AND i.released_at IS NULL "
        "ORDER BY i.id",
        (repo, subject),
    ).fetchall()
    if subject == "planning" and rows:
        # A former release bug could leave several active rows for one error.
        # Carry their confirmed delivery onto the current row so one real
        # Planner start creates one resumption without erasing older audit rows.
        confirmed = next(
            (row for row in rows if max(int(row[3]), int(row[4])) > 0), None,
        )
        if confirmed is not None:
            connection.execute(
                "UPDATE notified_block_incidents SET confirmed_before = 1, "
                "payload = ? WHERE id = ?",
                (confirmed[2], rows[-1][0]),
            )
    for row in rows:
        connection.execute(
            "UPDATE notified_block_incidents SET released_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (row[0],),
        )
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP "
            "WHERE event_key = ? "
            "AND sent_at IS NULL AND canceled_at IS NULL", (row[1],),
        )


def record_work_start(
    connection: sqlite3.Connection, repo: str, operation: str,
    *, plan_id: str | None = None, start_identity: str | None = None,
    resumed: bool = False,
) -> int:
    """Record starts without Telegram; count newly finalized confirmed incidents."""

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
    adopt_legacy_block_notices(connection)
    if connection.execute(
        "SELECT 1 FROM project_blocks WHERE repo = ?", (repo,),
    ).fetchone() is not None:
        return 0
    project = connection.execute(
        "SELECT planning_error FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if project is None:
        return 0
    subjects = [] if project[0] else ["project"]
    if operation == "Planner" and not project[0]:
        subjects.append("planning")
    elif plan_id is not None and operation in {"Worker", "PR handling"}:
        subjects.append(f"plan:{plan_id}")
    started = 0
    for subject in subjects:
        if subject.startswith("plan:") and connection.execute(
            "SELECT 1 FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone() is not None:
            continue
        if subject == "planning":
            # Historical duplicate incidents may all have been released by
            # one clear. Its latest row carries confirmed delivery from them.
            rows = connection.execute(
                "SELECT i.id FROM notified_block_incidents i "
                "WHERE i.repo = ? AND i.subject = ? "
                "AND i.released_at IS NOT NULL AND i.resumed_at IS NULL "
                "AND i.id = (SELECT j.id FROM notified_block_incidents j "
                "WHERE j.repo = ? AND j.subject = ? "
                "AND j.released_at IS NOT NULL "
                "ORDER BY j.id DESC LIMIT 1)",
                (repo, subject, repo, subject),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id FROM notified_block_incidents "
                "WHERE repo = ? AND subject = ? AND released_at IS NOT NULL "
                "AND resumed_at IS NULL ORDER BY id", (repo, subject),
            ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE notified_block_incidents SET started_operation = ?, start_identity = ?, started_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "payload = json_set(CASE WHEN ? IS NULL THEN payload "
                "ELSE json_set(payload, '$.started_execplan_id', ?) END, '$.started_as_resume', ?) "
                "WHERE id = ? AND started_operation IS NULL",
                (operation, start_identity, plan_id, plan_id, int(resumed), row[0]),
            )
            started += _materialize_resumption(connection, int(row[0]))
    return started


def recover_interrupted_starts(connection):
    """Reconcile abandoned sends at startup, before project workers exist."""
    ensure_schema(connection)
    rows = connection.execute(
        "SELECT id FROM notified_block_incidents WHERE released_at IS NOT NULL "
        "AND resumed_at IS NULL AND start_identity IS NOT NULL",
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE notification_outbox SET delivery_started_at = NULL "
            "WHERE canceled_at IS NOT NULL AND event_key IN "
            "(SELECT event_key FROM block_notice_events WHERE incident_id = ?)",
            (row[0],),
        )
        _materialize_resumption(connection, int(row[0]))


def _materialize_resumption(connection: sqlite3.Connection, incident_id: int) -> int:
    """Finalize a confirmed incident's actual start without another message."""
    row = connection.execute(
        "SELECT i.confirmed_before, o.confirmed_pieces FROM notified_block_incidents i "
        "JOIN notification_outbox o ON o.event_key = i.event_key "
        "WHERE i.id = ? AND i.released_at IS NOT NULL AND i.resumed_at IS NULL "
        "AND i.started_operation IS NOT NULL", (incident_id,),
    ).fetchone()
    if row is None or max(int(row[0]), int(row[1])) == 0:
        return 0
    return connection.execute(
        "UPDATE notified_block_incidents SET resumed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND resumed_at IS NULL", (incident_id,),
    ).rowcount


def _project_checkpoint(connection, repo, plan_id):
    """Read only the result bound to the current project cause."""
    from loop_manager_internal.recovery import scoped_blocking
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                          "AND name = 'project_checkpoint_results'").fetchone() is None:
        return None
    block = scoped_blocking.project_block(connection, repo)
    row = connection.execute(
        'SELECT cause_identity, payload FROM project_checkpoint_results WHERE repo = ? AND plan_id = ?',
        (repo, plan_id)).fetchone()
    if block is None or row is None or row[0] != scoped_blocking.project_cause_identity(block):
        return None
    result = json.loads(row[1])
    origin = connection.execute('SELECT origin FROM projects WHERE repo = ?', (repo,)).fetchone()
    if result.get('result') == 'published':
        result['branch_url'] = _checkpoint_link(origin[0] if origin else None, result.get('commit_oid'))
    return result


def _project_checkpoint_suffix(checkpoint):
    return ("\0checkpoint:" + hashlib.sha256(json.dumps(
        checkpoint, sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()) if checkpoint else ""


def _preservation_checkpoint(connection, repo, scope, owner):
    """Read current-cause artifacts without treating them as branch ownership."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_preservations'").fetchone() is None:
        return None
    if scope == 'planning':
        row = connection.execute('SELECT planning_error_incident_id, planning_error_fingerprint, '
                                 'planning_attempt_fingerprint FROM projects WHERE repo = ?', (repo,)).fetchone()
        cause = str(row[0] or row[1] or row[2] or '') if row else ''
    else:
        table = 'project_blocks' if scope == 'project' else 'execplan_blocks'
        row = connection.execute(f'SELECT * FROM {table} WHERE repo = ? AND plan_id = ?', (repo, owner)).fetchone()
        if row is None:
            return None
        cause = hashlib.sha256(json.dumps(dict(row), sort_keys=True).encode()).hexdigest()
    rows = connection.execute('SELECT payload FROM diagnostic_preservations WHERE repo = ? '
                              'AND scope = ? AND owner = ? ORDER BY observed_order DESC', (repo, scope, owner)).fetchall()
    for row in rows:
        payload = json.loads(row[0])
        if payload.get('cause_identity') == cause:
            origin = connection.execute('SELECT origin FROM projects WHERE repo = ?', (repo,)).fetchone()
            if payload.get('result') == 'published':
                payload['branch_url'] = _checkpoint_link(origin[0] if origin else None, payload.get('commit_oid'))
            return payload
    return None


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
        return (_key("blocked", repo, "project\0planning_error\0" + _project_checkpoint_suffix(
                    _preservation_checkpoint(connection, repo, 'planning', 'planning')))
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
        remote = _remote_inspection_suffix(block[1], block[2], block[3])
        remote += _project_checkpoint_suffix(_preservation_checkpoint(connection, repo, 'plan', str(subject)))
    elif payload.get("scope") == "project":
        block = connection.execute(
            "SELECT reason_code, plan_id FROM project_blocks WHERE repo = ?",
            (repo,),
        ).fetchone()
        if block is None or block[0] != reason or block[1] != subject:
            return None
        recovery_subject = str(subject or "__project__")
        remote = _project_checkpoint_suffix(_preservation_checkpoint(connection, repo, 'project', subject)
                                             or _project_checkpoint(connection, repo, subject))
    else:
        return None
    recovery_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'recovery_records'"
    ).fetchone()
    recovery_identity = None
    if recovery_table:
        recovery = connection.execute(
            "SELECT identity FROM recovery_records WHERE repo = ? "
            "AND subject = ? AND final_evidence IS NOT NULL",
            (repo, recovery_subject),
        ).fetchone()
        if recovery:
            recovery_identity = recovery[0]
    if payload.get("scope") == "plan":
        return _plan_block_key(repo, reason, subject, recovery_identity, remote)
    return _project_block_key(repo, reason, subject, recovery_identity, remote)
