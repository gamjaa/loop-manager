"""Durable append-by-attempt ledger for Agent CLI observations."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

from agent_invocation import InvocationContext, ResolvedProfile, Usage
from agent_write_isolation import IsolationEvidence


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
    transition_id TEXT,
    previous_provider TEXT,
    previous_model TEXT,
    previous_effort TEXT,
    previous_session_id TEXT,
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

ISOLATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_isolation_evidence (
    invocation_id TEXT PRIMARY KEY REFERENCES agent_invocations(invocation_id),
    provider TEXT NOT NULL,
    cli_version TEXT NOT NULL,
    environment TEXT NOT NULL,
    role TEXT NOT NULL,
    canonical_worktree TEXT NOT NULL,
    repository_identity TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    configuration_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    checked_at TEXT NOT NULL
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    connection.execute(ISOLATION_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_invocations)")}
    for field in ("transition_id", "previous_provider", "previous_model",
                  "previous_effort", "previous_session_id"):
        if field not in columns:
            connection.execute(f"ALTER TABLE agent_invocations ADD COLUMN {field} TEXT")
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
                 model, effort, session_id, transition, transition_cause,
                 transition_id, previous_provider, previous_model, previous_effort,
                 previous_session_id, attempt, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identity, context.repo, context.plan_id, context.definition_revision,
                 role, provider, profile.model, profile.effort, session_id,
                 context.transition or ("resume" if session_id else "start"),
                 context.transition_cause, context.transition_id,
                 context.previous_provider, context.previous_model,
                 context.previous_effort, context.previous_session_id,
                 context.attempt, utc_now()))
        return identity

    def observe_session(self, identity: str, session_id: str) -> None:
        with self.connection:
            self.connection.execute("""UPDATE agent_invocations SET session_id = ?
                WHERE invocation_id = ? AND finished_at IS NULL""", (session_id, identity))

    def record_isolation(self, identity: str, evidence: IsolationEvidence) -> None:
        """Tie preflight to one invocation, never to a reusable session alone."""
        with self.connection:
            self.connection.execute("""INSERT INTO agent_isolation_evidence
                (invocation_id, provider, cli_version, environment, role,
                 canonical_worktree, repository_identity, policy_digest, snapshot_digest,
                 configuration_digest,
                 status, reason, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identity, evidence.provider, evidence.cli_version,
                 evidence.environment, evidence.role, evidence.worktree,
                 evidence.repository_identity, evidence.policy_digest,
                 evidence.snapshot_digest, evidence.configuration_digest,
                 evidence.status, evidence.reason, utc_now()))

    def verified_invocation(self, identity: str, *, session_id: str | None,
                            expected: IsolationEvidence) -> bool:
        """Legacy, pending, interrupted and rejected attempts are never proof."""
        if expected.status != "verified":
            return False
        row = self.connection.execute("""SELECT 1 FROM agent_invocations AS call
            JOIN agent_isolation_evidence AS proof USING (invocation_id)
            WHERE call.invocation_id = ? AND proof.status = 'verified'
              AND call.finished_at IS NOT NULL AND call.result_kind = 'success'
              AND proof.provider = ? AND proof.cli_version = ?
              AND proof.environment = ? AND proof.role = ?
              AND proof.canonical_worktree = ? AND proof.repository_identity = ?
              AND proof.policy_digest = ? AND proof.snapshot_digest = ?
              AND proof.configuration_digest = ?
              AND (? IS NULL OR call.session_id = ?)""",
            (identity, expected.provider, expected.cli_version,
             expected.environment, expected.role, expected.worktree,
             expected.repository_identity, expected.policy_digest,
             expected.snapshot_digest, expected.configuration_digest,
             session_id, session_id)).fetchone()
        return row is not None

    def update_isolation(self, identity: str, previous: IsolationEvidence,
                         evidence: IsolationEvidence) -> None:
        """Complete only this unfinished call's preparation; never reuse proof."""
        immutable = ("provider", "cli_version", "environment", "role", "worktree",
                     "repository_identity", "snapshot_digest", "configuration_digest")
        if (any(getattr(previous, name) != getattr(evidence, name) for name in immutable)
                or (previous.status, evidence.status) not in (
                    ("prepared", "verified"), ("prepared", "rejected"), ("verified", "rejected"))):
            raise sqlite3.IntegrityError("invalid isolation confirmation transition")
        with self.connection:
            cursor = self.connection.execute("""UPDATE agent_isolation_evidence SET
                policy_digest = ?, status = ?, reason = ?, checked_at = ?
                WHERE invocation_id = ? AND policy_digest = ? AND status = ?
                  AND provider = ? AND cli_version = ? AND environment = ? AND role = ?
                  AND canonical_worktree = ? AND repository_identity = ?
                  AND snapshot_digest = ? AND configuration_digest = ?
                  AND EXISTS (SELECT 1 FROM agent_invocations call
                              WHERE call.invocation_id = ? AND call.finished_at IS NULL)""",
                (evidence.policy_digest, evidence.status, evidence.reason, utc_now(), identity,
                 previous.policy_digest, previous.status, previous.provider, previous.cli_version,
                 previous.environment, previous.role, previous.worktree, previous.repository_identity,
                 previous.snapshot_digest, previous.configuration_digest, identity))
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("isolation preparation changed or attempt finished")

    def finish(self, identity: str, kind: str, code: int | None,
               duration_ms: int, session_id: str | None, usage: Usage,
               collection_status: str | None = None) -> None:
        with self.connection:
            if kind != "success":
                self.connection.execute("""UPDATE agent_isolation_evidence
                    SET status = 'invalidated', reason = 'agent_attempt_not_successful'
                    WHERE invocation_id = ? AND status IN ('verified', 'prepared')""", (identity,))
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
            self.connection.execute("""UPDATE agent_isolation_evidence
                SET status = 'invalidated', reason = 'agent_attempt_interrupted'
                WHERE status IN ('verified', 'prepared') AND invocation_id IN (
                    SELECT invocation_id FROM agent_invocations WHERE finished_at IS NULL
                )""")
            cursor = self.connection.execute("""UPDATE agent_invocations SET
                finished_at = ?, result_kind = 'interrupted', collection_status = 'unknown'
                WHERE finished_at IS NULL""", (utc_now(),))
        return cursor.rowcount

    def summary(self, repo: str) -> list[sqlite3.Row]:
        """Return comparable counters; unknown usage stays NULL."""
        return self.connection.execute("""WITH observations AS (
            SELECT *,
                CASE WHEN input_tokens IS NOT NULL AND cached_input_tokens IS NOT NULL
                     THEN input_tokens END AS paired_input_tokens,
                CASE WHEN input_tokens IS NOT NULL AND cached_input_tokens IS NOT NULL
                     THEN cached_input_tokens END AS paired_cached_input_tokens
            FROM agent_invocations WHERE repo = ?
        )
        SELECT role, provider, model, effort, transition,
            COUNT(*) AS calls,
            SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END) AS retries,
            SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens,
            SUM(cache_write_input_tokens) AS cache_write_input_tokens,
            SUM(output_tokens) AS output_tokens, SUM(reasoning_output_tokens) AS reasoning_output_tokens,
            SUM(CASE WHEN provider = 'claude' THEN input_tokens
                     WHEN input_tokens IS NOT NULL AND cached_input_tokens IS NOT NULL
                     THEN input_tokens - cached_input_tokens END) AS uncached_input_tokens,
            CASE WHEN provider = 'claude' THEN
                     1.0 * SUM(CASE WHEN cache_write_input_tokens IS NOT NULL
                                    THEN paired_cached_input_tokens END) /
                     NULLIF(SUM(paired_input_tokens + paired_cached_input_tokens
                                + cache_write_input_tokens), 0)
                 ELSE 1.0 * SUM(paired_cached_input_tokens) /
                      NULLIF(SUM(paired_input_tokens), 0)
            END AS cache_ratio,
            SUM(duration_ms) AS duration_ms
            FROM observations
            GROUP BY role, provider, model, effort, transition""", (repo,)).fetchall()
