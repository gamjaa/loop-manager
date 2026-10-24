"""Claim, confirm and retry persisted Telegram message pieces."""
from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events
from collections.abc import Callable
from contextvars import ContextVar
from contextlib import contextmanager
import json
import sqlite3
from .repository import adopt_legacy_block_notices, ensure_schema
from .incidents import _current_block_key, _materialize_resumption
from .rendering import _message, _pieces, _draft_entities, MESSAGE_LIMIT
from .transport import send_telegram
from .policy import is_current, cancel_event

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
) -> tuple[str, int, int, list[dict]] | None:
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
        try:
            payload = json.loads(str(row[2]))
        except (TypeError, ValueError):
            cancel_event(connection, event_key)
            return None
        if not isinstance(payload, dict):
            cancel_event(connection, event_key)
            return None
        if row[1] == "persistent_execution_failure":
            # Recheck the exact target and cause before retiring the legacy
            # event. A matching current block owns the notice; a missing or
            # different block cannot make this failure actionable.
            from .events import suppress_persistent_failure_for_block
            subject = str(payload.get("execplan_id") or "__project__")
            suppress_persistent_failure_for_block(
                connection, str(row[0]), subject,
            )
            # Persistent-failure events are never sent separately. Preserve
            # their prior pieces and errors while preventing another claim.
            connection.execute(
                "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
                "delivery_started_at = NULL WHERE event_key = ? "
                "AND sent_at IS NULL AND canceled_at IS NULL",
                (event_key,),
            )
            return None
        if not is_current(connection, str(row[0]), str(row[1]), payload):
            cancel_event(connection, event_key)
            return None
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
        entities = (_draft_entities(payload, text, cursor * MESSAGE_LIMIT,
                                    (cursor + 1) * MESSAGE_LIMIT)
                    if row[1] == "draft_decisions" else [])
        return (pieces[cursor], cursor, len(pieces), entities) if claimed == 1 else None


def dispatch_pending(
    connection: sqlite3.Connection,
    token: str,
    chat_id: str,
    *,
    adapter: Callable[[str, str, str], None] = send_telegram,
    exclude_pr: bool = False,
) -> int:
    """Deliver pending rows; delivery errors remain secret-free and isolated."""

    if not token or not chat_id:
        return 0
    ensure_schema(connection)
    adopt_legacy_block_notices(connection)
    query = (
        "SELECT event_key, event_type, payload, repo FROM notification_outbox "
        "WHERE sent_at IS NULL AND canceled_at IS NULL "
        + ("AND event_type != 'pr_created' " if exclude_pr else "")
        + "ORDER BY created_at, event_key"
    )
    rows = connection.execute(query).fetchall()
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
        "SELECT event_key, event_type, payload, repo FROM notification_outbox "
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
            piece, cursor, total, entities = claimed
            first_piece = False
            try:
                if adapter is send_telegram:
                    adapter(token, chat_id, piece, disable_notification=bool(
                        json.loads(str(row[2])).get("silent", False)),
                        **({"entities": entities} if entities else {}))
                else:
                    adapter(token, chat_id, piece)
            except Exception as error:
                failure = f"Telegram delivery failed ({type(error).__name__})"
                events.emit("Telegram 전송 실패: 미전송 조각은 다음 실행에서 재시도", level="WARN",
                            project=row[3], plan=json.loads(str(row[2])).get("execplan_id"),
                            stage="NOTIFICATION", event_key=row[0], event_type=row[1], piece=cursor + 1, total_pieces=total,
                            error_type=type(error).__name__)
                with connection:
                    connection.execute(
                        "UPDATE notification_outbox SET delivery_started_at = NULL, "
                        "last_error = CASE WHEN canceled_at IS NULL THEN ? "
                        "ELSE last_error END WHERE event_key = ? AND sent_at IS NULL "
                        "AND confirmed_pieces = ?",
                        (failure, row[0], cursor),
                    )
                break
            with connection:
                connection.execute(
                    "UPDATE notification_outbox SET confirmed_pieces = ?, "
                    "delivery_started_at = CASE WHEN ? = ? "
                    "THEN delivery_started_at ELSE NULL END, "
                    "last_error = CASE WHEN canceled_at IS NULL THEN NULL ELSE last_error END, "
                    "sent_at = CASE WHEN ? = ? THEN CURRENT_TIMESTAMP "
                    "ELSE NULL END WHERE event_key = ? AND sent_at IS NULL "
                    "AND confirmed_pieces = ?",
                    (cursor + 1, cursor + 1, total, cursor + 1, total,
                     row[0], cursor),
                )
                if row[1] in {"blocked", "pr_created"}:
                    # Only explicitly continued versions inherit this exact
                    # frozen body's late confirmation; changed actions do not.
                    connection.execute(
                        "WITH RECURSIVE continued(event_key) AS ("
                        "SELECT event_key FROM notification_outbox WHERE json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.continued_from')=? "
                        "AND rendered_text IS (SELECT rendered_text FROM notification_outbox WHERE event_key=?) "
                        "UNION SELECT o.event_key FROM notification_outbox o JOIN continued c "
                        "ON json_extract(CASE WHEN json_valid(o.payload) THEN o.payload ELSE '{}' END, '$.continued_from')=c.event_key "
                        "WHERE o.rendered_text IS (SELECT rendered_text FROM notification_outbox WHERE event_key=?)) "
                        "UPDATE notification_outbox SET confirmed_pieces=MAX(confirmed_pieces, ?), "
                        "sent_at=CASE WHEN ?=? THEN COALESCE(sent_at, CURRENT_TIMESTAMP) ELSE sent_at END "
                        "WHERE event_key IN (SELECT event_key FROM continued)",
                        (row[0], row[0], row[0], cursor + 1, cursor + 1, total))
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
                events.emit("Telegram 전송 완료", project=row[3], plan=json.loads(str(row[2])).get("execplan_id"),
                            stage="NOTIFICATION", event_key=row[0], event_type=row[1], pieces=total)
                break
    return sent


_immediate_delivery = ContextVar("notification_immediate_delivery", default=None)


@contextmanager
def immediate_delivery(connection, token, chat_id, repo):
    binding = _immediate_delivery.set((connection, token, chat_id, repo))
    try:
        yield
    finally:
        _immediate_delivery.reset(binding)


def flush_project_events(connection, repo):
    binding = _immediate_delivery.get()
    if binding is None or binding[0] is not connection or binding[3] != repo or connection.in_transaction:
        return 0
    _, token, chat_id, _ = binding
    if not token or not chat_id:
        return 0
    try:
        rows = connection.execute(
            "SELECT event_key, event_type, payload, repo FROM notification_outbox "
            "WHERE repo = ? AND event_type != 'pr_created' "
            "AND sent_at IS NULL AND canceled_at IS NULL AND attempts = 0 "
            "ORDER BY rowid", (repo,),
        ).fetchall()
        return _deliver_rows(connection, token, chat_id, rows, send_telegram, first_attempt_only=True)
    except Exception as error:
        events.emit("Telegram 즉시 전송 처리 실패: 다음 실행에서 재시도", level="WARN",
                    error_type=type(error).__name__)
        return 0


def flush_new_pr_events(connection, repo):
    """Attempt newly published PR events at their already-observed publication boundary."""
    binding = _immediate_delivery.get()
    if binding is None or binding[0] is not connection or binding[3] != repo or connection.in_transaction:
        return 0
    _, token, chat_id, _ = binding
    if not token or not chat_id:
        return 0
    try:
        rows = connection.execute(
            "SELECT event_key, event_type, payload, repo FROM notification_outbox "
            "WHERE repo = ? AND event_type = 'pr_created' "
            "AND sent_at IS NULL AND canceled_at IS NULL AND attempts = 0 "
            "ORDER BY created_at, event_key", (repo,),
        ).fetchall()
        return _deliver_rows(connection, token, chat_id, rows, send_telegram,
                             first_attempt_only=True)
    except Exception as error:
        events.emit("새 PR 알림 전송 실패: 다음 실행에서 재확인", level="WARN",
                    error_type=type(error).__name__)
        return 0


def flush_observed_pr_events(connection, repo):
    """Retry this project's PR notices only after its remote PR observation succeeds."""
    binding = _immediate_delivery.get()
    if binding is None or binding[0] is not connection or binding[3] != repo or connection.in_transaction:
        return 0
    _, token, chat_id, _ = binding
    if not token or not chat_id:
        return 0
    try:
        rows = connection.execute(
            "SELECT event_key, event_type, payload, repo FROM notification_outbox "
            "WHERE repo = ? AND event_type = 'pr_created' "
            "AND sent_at IS NULL AND canceled_at IS NULL "
            "ORDER BY created_at, event_key", (repo,),
        ).fetchall()
        return _deliver_rows(connection, token, chat_id, rows, send_telegram)
    except Exception as error:
        events.emit("PR 알림 재확인 전송 실패: 다음 실행에서 다시 확인", level="WARN",
                    error_type=type(error).__name__)
        return 0
