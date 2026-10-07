"""Durable append-by-attempt ledger for Agent CLI observations."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from uuid import uuid4

from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.storage import common_guidance_state


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
    scope_id TEXT,
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
    common_guidance_state.ensure_schema(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_invocations)")}
    for field in ("transition_id", "previous_provider", "previous_model",
                  "previous_effort", "previous_session_id", "scope_id", "guidance_task_id",
                  "guidance_content_hash"):
        if field not in columns:
            connection.execute(f"ALTER TABLE agent_invocations ADD COLUMN {field} TEXT")
    connection.execute("""CREATE INDEX IF NOT EXISTS agent_invocations_analysis
        ON agent_invocations(repo, role, provider, model, effort, transition, started_at)""")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InvocationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def invocation_scope_id(self, repo: str, role: str, worktree: str) -> str | None:
        """Bind an attempt only to the matching persisted role/workspace scope."""
        scope = RuntimeStateRepository(self.connection).scope_owner(repo)
        if (scope is not None and (scope['agent_role'] == role
                or (scope['agent_role'] == 'maintenance_planner' and role == 'planner'))
                and scope['worktree'] == worktree and scope['agent_snapshot']):
            return GitSnapshot.deserialize(str(scope['agent_snapshot'])).scope_id
        return None

    def latest_scope_session(self, scope_id: str) -> str | None:
        row = self.connection.execute(
            'SELECT session_id FROM agent_invocations WHERE scope_id = ? ORDER BY rowid DESC LIMIT 1',
            (scope_id,),
        ).fetchone()
        return row[0] if row else None

    def start(self, context: InvocationContext, role: str, provider: str,
              profile: ResolvedProfile, session_id: str | None, *,
              scope_id: str | None = None) -> str:
        identity = uuid4().hex
        with self.connection:
            self.connection.execute("""INSERT INTO agent_invocations
                (invocation_id, repo, plan_id, definition_revision, role, provider,
                 model, effort, session_id, transition, transition_cause,
                 transition_id, previous_provider, previous_model, previous_effort,
                 previous_session_id, attempt, started_at, scope_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identity, context.repo, context.plan_id, context.definition_revision,
                 role, provider, profile.model, profile.effort, session_id,
                 context.transition or ("resume" if session_id else "start"),
                 context.transition_cause, context.transition_id,
                 context.previous_provider, context.previous_model,
                 context.previous_effort, context.previous_session_id,
                 context.attempt, utc_now(), scope_id))
        return identity

    def verified_scope(self, scope_id: str, *, repo: str, plan_id: str | None,
                       role: str, provider: str, session_id: str | None,
                       worktree: str, repository_identity: str, start_digest: str) -> bool:
        """Every physical attempt in a saved scope must carry matching proof."""
        if not scope_id or not session_id:
            return False
        rows = self.connection.execute("""SELECT call.*, proof.status AS proof_status,
            proof.provider AS proof_provider, proof.role AS proof_role,
            proof.cli_version, proof.environment, proof.canonical_worktree,
            proof.repository_identity, proof.policy_digest, proof.snapshot_digest,
            proof.configuration_digest
            FROM agent_invocations AS call LEFT JOIN agent_isolation_evidence AS proof
            USING (invocation_id) WHERE call.scope_id = ? ORDER BY call.rowid""",
            (scope_id,)).fetchall()
        if not rows or rows[0]['snapshot_digest'] != start_digest:
            return False
        for row in rows:
            if (row['repo'] != repo or row['plan_id'] != plan_id
                    or row['role'] != role or row['provider'] != provider
                    or row['proof_provider'] != provider or row['proof_role'] != role
                    or row['proof_status'] != 'verified' or not row['finished_at']
                    or row['result_kind'] != 'success' or row['result_code'] != 0
                    or not row['session_id'] or not row['cli_version'] or not row['environment']
                    or row['canonical_worktree'] != worktree
                    or row['repository_identity'] != repository_identity
                    or any(not isinstance(row[key], str) or len(row[key]) != 64
                           or any(char not in '0123456789abcdef' for char in row[key])
                           for key in ('policy_digest', 'snapshot_digest', 'configuration_digest'))):
                return False
        return rows[-1]['session_id'] == session_id

    def observe_session(self, identity: str, session_id: str) -> None:
        with self.connection:
            self.connection.execute("""UPDATE agent_invocations SET session_id = ?
                WHERE invocation_id = ? AND finished_at IS NULL""", (session_id, identity))

    def bind_guidance(self, identity: str, task_id: str, content_hash: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE agent_invocations SET guidance_task_id = ?, guidance_content_hash = ? "
                "WHERE invocation_id = ? AND finished_at IS NULL", (task_id, content_hash, identity),
            )

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

    def finish(self, identity: str, kind: str, code: int | None,
               duration_ms: int, session_id: str | None, usage: Usage,
               collection_status: str | None = None, *,
               replace_session: bool = False) -> None:
        with self.connection:
            session_update = "session_id = ?" if replace_session else "session_id = COALESCE(?, session_id)"
            cursor = self.connection.execute(f"""UPDATE agent_invocations SET
                finished_at = ?, duration_ms = ?, result_kind = ?, result_code = ?,
                {session_update}, collection_status = ?,
                input_tokens = ?, cached_input_tokens = ?, cache_write_input_tokens = ?,
                output_tokens = ?, reasoning_output_tokens = ?
                WHERE invocation_id = ? AND finished_at IS NULL""",
                (utc_now(), duration_ms, kind, code, session_id,
                 collection_status or usage.status,
                 usage.input_tokens, usage.cached_input_tokens,
                 usage.cache_write_input_tokens, usage.output_tokens,
                 usage.reasoning_output_tokens, identity))
            if cursor.rowcount and kind != "success":
                self.connection.execute("""UPDATE agent_isolation_evidence
                    SET status = 'invalidated', reason = 'agent_attempt_not_successful'
                    WHERE invocation_id = ? AND status IN ('verified', 'prepared')""", (identity,))

    def reconcile_interrupted(self, *, state_dir=None) -> int:
        from .runtime_state import _transaction
        from .work_claims import live_manual_tasks
        count = 0
        with _transaction(self.connection):
            protected = live_manual_tasks(self.connection, state_dir) if state_dir is not None else frozenset()
            pending = self.connection.execute(
                'SELECT invocation_id,guidance_task_id FROM agent_invocations WHERE finished_at IS NULL').fetchall()
            for identity, task in pending:
                if task in protected:
                    continue
                self.connection.execute("""UPDATE agent_isolation_evidence
                    SET status = 'invalidated', reason = 'agent_attempt_interrupted'
                    WHERE status IN ('verified', 'prepared') AND invocation_id = ?""", (identity,))
                count += self.connection.execute("""UPDATE agent_invocations SET
                    finished_at = ?, result_kind = 'interrupted', collection_status = 'unknown'
                    WHERE invocation_id = ? AND finished_at IS NULL""", (utc_now(),identity)).rowcount
        return count

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
