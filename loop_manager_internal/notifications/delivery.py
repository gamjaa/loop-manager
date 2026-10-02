"""Claim, confirm and retry persisted Telegram message pieces."""
from __future__ import annotations
from collections.abc import Callable
import json
import sqlite3
from .repository import ensure_schema
from .incidents import _current_block_key, _materialize_resumption
from .rendering import _message, _pieces
from .transport import send_telegram

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
