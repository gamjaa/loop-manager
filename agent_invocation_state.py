"""Durable append-by-attempt ledger for Agent CLI observations."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

from agent_invocation import InvocationContext, ResolvedProfile, Usage


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_invocations (
    invocation_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    plan_id TEXT,
    definition_revision TEXT,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    session_id TEXT,
    transition TEXT NOT NULL,
    transition_cause TEXT,
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    result_kind TEXT,
    result_code INTEGER,
    collection_status TEXT NOT NULL DEFAULT 'pending',
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    cache_write_input_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    connection.execute("""CREATE INDEX IF NOT EXISTS agent_invocations_analysis
        ON agent_invocations(repo, role, provider, model, effort, transition, started_at)""")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvocationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def start(self, context: InvocationContext, role: str, provider: str,
              profile: ResolvedProfile, session_id: str | None) -> str:
        identity = uuid4().hex
        with self.connection:
            self.connection.execute("""INSERT INTO agent_invocations
                (invocation_id, repo, plan_id, definition_revision, role, provider,
                 model, effort, session_id, transition, transition_cause, attempt, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identity, context.repo, context.plan_id, context.definition_revision,
                 role, provider, profile.model, profile.effort, session_id,
                 context.transition or ("resume" if session_id else "start"),
                 context.transition_cause, context.attempt, utc_now()))
        return identity

    def observe_session(self, identity: str, session_id: str) -> None:
        with self.connection:
            self.connection.execute("""UPDATE agent_invocations SET session_id = ?
                WHERE invocation_id = ? AND finished_at IS NULL""", (session_id, identity))

    def finish(self, identity: str, kind: str, code: int | None,
               duration_ms: int, session_id: str | None, usage: Usage,
               collection_status: str | None = None) -> None:
        with self.connection:
            self.connection.execute("""UPDATE agent_invocations SET
                finished_at = ?, duration_ms = ?, result_kind = ?, result_code = ?,
                session_id = COALESCE(?, session_id), collection_status = ?,
                input_tokens = ?, cached_input_tokens = ?, cache_write_input_tokens = ?,
                output_tokens = ?, reasoning_output_tokens = ?
                WHERE invocation_id = ? AND finished_at IS NULL""",
                (utc_now(), duration_ms, kind, code, session_id,
                 collection_status or usage.status,
                 usage.input_tokens, usage.cached_input_tokens,
                 usage.cache_write_input_tokens, usage.output_tokens,
                 usage.reasoning_output_tokens, identity))

    def reconcile_interrupted(self) -> int:
        with self.connection:
            cursor = self.connection.execute("""UPDATE agent_invocations SET
                finished_at = ?, result_kind = 'interrupted', collection_status = 'unknown'
                WHERE finished_at IS NULL""", (utc_now(),))
        return cursor.rowcount

    def summary(self, repo: str) -> list[sqlite3.Row]:
        """Return comparable counters; unknown usage stays NULL."""
        return self.connection.execute("""SELECT role, provider, model, effort, transition,
            COUNT(*) AS calls,
            SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END) AS retries,
            SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens,
            SUM(cache_write_input_tokens) AS cache_write_input_tokens,
            SUM(output_tokens) AS output_tokens, SUM(reasoning_output_tokens) AS reasoning_output_tokens,
            SUM(CASE WHEN provider = 'claude' THEN input_tokens
                     WHEN input_tokens IS NOT NULL AND cached_input_tokens IS NOT NULL
                     THEN input_tokens - cached_input_tokens END) AS uncached_input_tokens,
            CASE WHEN SUM(CASE WHEN cached_input_tokens IS NOT NULL
                                    AND input_tokens IS NOT NULL
                                    AND (provider != 'claude' OR cache_write_input_tokens IS NOT NULL)
                               THEN 1 ELSE 0 END) = 0
                 THEN NULL
                 WHEN provider = 'claude' THEN
                     1.0 * SUM(CASE WHEN input_tokens IS NOT NULL AND cached_input_tokens IS NOT NULL
                                         AND cache_write_input_tokens IS NOT NULL
                                    THEN cached_input_tokens END) /
                     NULLIF(SUM(input_tokens + cached_input_tokens + cache_write_input_tokens), 0)
                 ELSE 1.0 * SUM(CASE WHEN input_tokens IS NOT NULL AND cached_input_tokens IS NOT NULL
                                     THEN cached_input_tokens END) /
                      NULLIF(SUM(CASE WHEN cached_input_tokens IS NOT NULL
                                       THEN input_tokens END), 0)
            END AS cache_ratio,
            SUM(duration_ms) AS duration_ms
            FROM agent_invocations WHERE repo = ?
            GROUP BY role, provider, model, effort, transition""", (repo,)).fetchall()
