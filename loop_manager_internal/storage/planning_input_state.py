"""Durable, scoped comparison baseline for active ExecPlan declarations."""
from __future__ import annotations

import json
import sqlite3
from typing import Mapping
from loop_manager_internal.storage import planner_check_state
from loop_manager_internal.agents import common_guidance


FORMAT = "active-plan-content-sha256-v1"
FULL_FORMAT = "planning-input-v3"
SCOPE = "general"
SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_input_baselines (
    repo TEXT NOT NULL,
    scope TEXT NOT NULL,
    reference_revision TEXT NOT NULL,
    last_verified_revision TEXT NOT NULL,
    fingerprint_format TEXT NOT NULL,
    full_input_format TEXT NOT NULL,
    entries TEXT NOT NULL,
    PRIMARY KEY (repo, scope)
)
"""

PlanEntries = Mapping[str, tuple[str, str]]


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    columns = {row[1] for row in connection.execute('PRAGMA table_info(planning_input_baselines)')}
    if 'guidance_hash' not in columns:
        connection.execute('ALTER TABLE planning_input_baselines ADD COLUMN guidance_hash TEXT')


def guidance_changed(connection: sqlite3.Connection, repo: str) -> bool:
    ensure_schema(connection)
    row = connection.execute('SELECT guidance_hash FROM planning_input_baselines WHERE repo = ? AND scope = ?',
                             (repo, SCOPE)).fetchone()
    return row is None or row[0] != common_guidance.runtime_content_hash()


def _encoded(entries: PlanEntries) -> str:
    return json.dumps(entries, sort_keys=True, separators=(",", ":"))


def _saved(connection: sqlite3.Connection, repo: str) -> dict[str, tuple[str, str]] | None:
    ensure_schema(connection)
    row = connection.execute(
        "SELECT fingerprint_format, full_input_format, entries FROM planning_input_baselines "
        "WHERE repo = ? AND scope = ?", (repo, SCOPE),
    ).fetchone()
    if (row is None or row["fingerprint_format"] != FORMAT
            or row["full_input_format"] != FULL_FORMAT):
        return None
    try:
        raw = json.loads(row["entries"])
        if not isinstance(raw, dict) or any(
            not isinstance(path, str) or not isinstance(value, list)
            or len(value) != 2 or not all(isinstance(part, str) for part in value)
            for path, value in raw.items()
        ):
            return None
        return {path: (value[0], value[1]) for path, value in raw.items()}
    except (TypeError, ValueError):
        return None


def matches(connection: sqlite3.Connection, repo: str, current: PlanEntries) -> bool:
    saved = _saved(connection, repo)
    return saved is not None and saved == current


def has_baseline(connection: sqlite3.Connection, repo: str) -> bool:
    ensure_schema(connection)
    return connection.execute(
        "SELECT 1 FROM planning_input_baselines WHERE repo = ? AND scope = ?",
        (repo, SCOPE),
    ).fetchone() is not None


def record_general(
    connection: sqlite3.Connection, repo: str, revision: str, entries: PlanEntries,
) -> None:
    """Call in the same transaction as a successful full planning result."""
    ensure_schema(connection)
    connection.execute(
        "INSERT INTO planning_input_baselines "
        "(repo, scope, reference_revision, last_verified_revision, fingerprint_format, full_input_format, entries, guidance_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(repo, scope) DO UPDATE SET "
        "reference_revision = excluded.reference_revision, "
        "last_verified_revision = excluded.last_verified_revision, "
        "fingerprint_format = excluded.fingerprint_format, "
        "full_input_format = excluded.full_input_format, entries = excluded.entries, guidance_hash = excluded.guidance_hash",
        (repo, SCOPE, revision, revision, FORMAT, FULL_FORMAT, _encoded(entries),
         common_guidance.planning_content_hash(connection, repo) or common_guidance.runtime_content_hash()),
    )
    check = planner_check_state.get(connection, repo)
    if check is not None and check['stage'] != 'accepted':
        # A full validated planning result resolves its preceding check/handoff.
        # Keep the decision and session audit; move the comparison revision to
        # the same successfully published baseline in this transaction.
        if not planner_check_state.advance(
            connection, repo, str(check['stage']), 'accepted',
            expected_input=str(check['input_fingerprint']),
            expected_target=str(check['target_fingerprint']), base_sha=revision,
        ):
            raise RuntimeError('일반 planning 확정 전 점검 상태가 변경됐습니다.')


def advance_verified_delta(
    connection: sqlite3.Connection, repo: str, revision: str,
    changes: Mapping[str, tuple[tuple[str, str] | None, tuple[str, str] | None]],
) -> bool:
    """Consume only a verified Manager plan delta, preserving unrelated input."""
    if not changes:
        return False
    saved = _saved(connection, repo)
    if saved is None:
        return False
    applied = False
    for path, (before, after) in changes.items():
        if saved.get(path) != before:
            # An unobserved user change at this path remains a Planner input.
            continue
        if after is None:
            saved.pop(path, None)
        else:
            saved[path] = after
        applied = True
    if applied:
        connection.execute(
            "UPDATE planning_input_baselines SET entries = ?, last_verified_revision = ? "
            "WHERE repo = ? AND scope = ? AND fingerprint_format = ? "
            "AND full_input_format = ?",
            (_encoded(saved), revision, repo, SCOPE, FORMAT, FULL_FORMAT),
        )
    return applied


def consume_owned_records(connection: sqlite3.Connection, repo: str, revision: str, current: PlanEntries) -> bool:
    import hashlib
    from loop_manager_internal.execution.execution_revision import DefinitionSnapshot, record_only_definition
    from loop_manager_internal.storage import runtime_state
    if _saved(connection, repo) is None or connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone() is None:
        return False
    changes = {}
    for row in connection.execute('SELECT snapshot FROM execution_checkpoints WHERE repo = ?', (repo,)):
        try:
            record = json.loads(row[0] or '{}').get('document_record')
            if not isinstance(record, dict) or record.get('applied') is not True or not isinstance(record.get('text'), str):
                continue
            previous = DefinitionSnapshot.deserialize(record.get('planning_previous') or record.get('previous'))
            if previous is None or previous.body_text is None or record_only_definition(previous, record['text']) == previous:
                continue
            before = ('blob', hashlib.sha256(previous.body_text.encode('utf-8')).hexdigest())
            after = ('blob', hashlib.sha256(record['text'].encode('utf-8')).hexdigest())
            if current.get(previous.path) == after:
                changes[previous.path] = (before, after)
        except (ValueError, TypeError, AttributeError):
            continue
    with runtime_state._transaction(connection):
        return advance_verified_delta(connection, repo, revision, changes)


def delta(before: PlanEntries, after: PlanEntries) -> dict[
    str, tuple[tuple[str, str] | None, tuple[str, str] | None]
]:
    return {
        path: (before.get(path), after.get(path))
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    }
