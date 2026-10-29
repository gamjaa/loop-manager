"""Durable append-by-attempt ledger for Agent CLI observations."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import json
import re
from uuid import uuid4

from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository, _transaction
from loop_manager_internal.storage import common_guidance_state


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_invocations (
    invocation_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    plan_id TEXT,
    task_id TEXT,
    purpose TEXT,
    target TEXT,
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
    execution_status TEXT NOT NULL DEFAULT 'unknown',
    execution_started_at TEXT,
    execution_finished_at TEXT,
    execution_duration_ms INTEGER,
    process_receipt_path TEXT,
    execution_finish_evidence TEXT,
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
    from .targeted_test_state import ensure_schema as targeted_schema
    targeted_schema(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_invocations)")}
    for field, definition in (("transition_id", "TEXT"), ("previous_provider", "TEXT"),
                  ("previous_model", "TEXT"), ("purpose", "TEXT"), ("target", "TEXT"),
                  ("task_id", "TEXT"), ("execution_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                  ("execution_started_at", "TEXT"), ("execution_finished_at", "TEXT"),
                  ("execution_duration_ms", "INTEGER"), ("previous_effort", "TEXT"),
                  ("process_receipt_path", "TEXT"), ("execution_finish_evidence", "TEXT"),
                  ("previous_session_id", "TEXT"), ("scope_id", "TEXT"),
                  ("guidance_task_id", "TEXT"), ("guidance_content_hash", "TEXT")):
        if field not in columns:
            connection.execute(f"ALTER TABLE agent_invocations ADD COLUMN {field} {definition}")
    if 'execution_identity' not in columns:
        connection.execute('ALTER TABLE agent_invocations ADD COLUMN execution_identity TEXT')
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
                (invocation_id, repo, plan_id, task_id, purpose, target, definition_revision, role, provider,
                 model, effort, session_id, transition, transition_cause,
                 transition_id, previous_provider, previous_model, previous_effort,
                 previous_session_id, attempt, started_at, scope_id, execution_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identity, context.repo, context.plan_id, context.task_id, context.purpose,
                 context.target, context.definition_revision,
                 role, provider, profile.model, profile.effort, session_id,
                 context.transition or ("resume" if session_id else "start"),
                 context.transition_cause, context.transition_id,
                 context.previous_provider, context.previous_model,
                 context.previous_effort, context.previous_session_id,
                 context.attempt, utc_now(), scope_id, "prepared"))
        return identity

    def mark_execution_started(self, identity: str, *, observed_at: str | None = None) -> None:
        """Record only the provider callback reached after successful process creation."""
        with _transaction(self.connection):
            self.connection.execute("""UPDATE agent_invocations SET
                execution_status = 'executed',
                execution_started_at = COALESCE(execution_started_at, ?)
                WHERE invocation_id = ? AND finished_at IS NULL""",
                (observed_at or utc_now(), identity))

    def mark_execution_finished(self, identity: str, duration_ms: int, *, observed_at: str | None = None) -> None:
        with _transaction(self.connection):
            self.connection.execute("""UPDATE agent_invocations SET
                execution_finished_at = ?, execution_duration_ms = ?,
                execution_finish_evidence = 'provider_process_finished'
                WHERE invocation_id = ? AND finished_at IS NULL AND execution_finished_at IS NULL
                  AND execution_status = 'executed'""",
                (observed_at or utc_now(), max(0, duration_ms), identity))

    def bind_process_receipt(self, identity: str, path: str) -> None:
        with self.connection:
            self.connection.execute('''UPDATE agent_invocations SET process_receipt_path=?
                WHERE invocation_id=? AND finished_at IS NULL''', (path, identity))

    def reconcile_execution(self, *, state_dir, repo: str | None = None) -> None:
        from loop_manager_internal.agents.process_receipt import recover
        from .runtime_state import _transaction
        rows = self.connection.execute('''SELECT invocation_id, process_receipt_path
            FROM agent_invocations WHERE finished_at IS NOT NULL AND execution_finished_at IS NULL
            AND process_receipt_path IS NOT NULL AND (? IS NULL OR repo=?)''', (repo, repo)).fetchall()
        for identity, path in rows:
            proof = recover(path, state_dir)
            if proof is not None:
                with _transaction(self.connection):
                    self.connection.execute('''UPDATE agent_invocations SET execution_finished_at=?,
                        execution_finish_evidence=?,
                        execution_status=CASE WHEN ?='not_started' THEN 'not_executed' ELSE execution_status END
                        WHERE invocation_id=? AND execution_finished_at IS NULL
                        AND (?!='not_started' OR execution_started_at IS NULL)''',
                        (proof['finished_at'], json.dumps(proof, sort_keys=True), proof.get('evidence'),
                         identity, proof.get('evidence', '')))

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
        with _transaction(self.connection):
            self.connection.execute("""UPDATE agent_invocations SET session_id = ?
                WHERE invocation_id = ? AND finished_at IS NULL""", (session_id, identity))

    def bind_guidance(self, identity: str, task_id: str, content_hash: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE agent_invocations SET guidance_task_id = ?, guidance_content_hash = ? "
                "WHERE invocation_id = ? AND finished_at IS NULL", (task_id, content_hash, identity),
            )

    def record_isolation(self, identity: str, evidence: IsolationEvidence) -> None:
        """Tie final preflight to one unstarted invocation, never to a reusable session."""
        with self.connection:
            result = self.connection.execute("""INSERT INTO agent_isolation_evidence
                (invocation_id, provider, cli_version, environment, role,
                 canonical_worktree, repository_identity, policy_digest, snapshot_digest,
                 configuration_digest,
                 status, reason, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    provider=excluded.provider, cli_version=excluded.cli_version,
                    environment=excluded.environment, role=excluded.role,
                    canonical_worktree=excluded.canonical_worktree,
                    repository_identity=excluded.repository_identity,
                    policy_digest=excluded.policy_digest,
                    snapshot_digest=excluded.snapshot_digest,
                    configuration_digest=excluded.configuration_digest,
                    status=excluded.status, reason=excluded.reason,
                    checked_at=excluded.checked_at
                WHERE EXISTS (SELECT 1 FROM agent_invocations call
                    WHERE call.invocation_id=excluded.invocation_id
                      AND call.provider=excluded.provider AND call.role=excluded.role
                      AND call.execution_started_at IS NULL AND call.finished_at IS NULL)
                  AND (agent_isolation_evidence.status='verified' OR
                       (agent_isolation_evidence.provider='claude' AND agent_isolation_evidence.status='prepared'))
                  AND agent_isolation_evidence.provider=excluded.provider
                  AND agent_isolation_evidence.cli_version=excluded.cli_version
                  AND agent_isolation_evidence.environment=excluded.environment
                  AND agent_isolation_evidence.role=excluded.role
                  AND agent_isolation_evidence.canonical_worktree=excluded.canonical_worktree
                  AND agent_isolation_evidence.repository_identity=excluded.repository_identity
                  AND agent_isolation_evidence.snapshot_digest=excluded.snapshot_digest""",
                (identity, evidence.provider, evidence.cli_version,
                 evidence.environment, evidence.role, evidence.worktree,
                 evidence.repository_identity, evidence.policy_digest,
                 evidence.snapshot_digest, evidence.configuration_digest,
                 evidence.status, evidence.reason, utc_now()))
            if result.rowcount != 1:
                raise sqlite3.IntegrityError('격리 증거 identity 또는 실행 전 갱신 경계가 유효하지 않습니다.')
            self.connection.execute("""UPDATE agent_invocations SET execution_identity=NULL
                WHERE invocation_id=? AND execution_started_at IS NULL AND finished_at IS NULL""",
                (identity,))

    def record_execution_identity(self, identity: str, value: dict[str, str]) -> None:
        required = {'provider', 'model', 'effort', 'instruction', 'environment', 'policy'}
        if set(value) != required or any(not isinstance(v, str) or not v for v in value.values()):
            raise ValueError('실제 Agent 실행 입력 identity 형식이 다릅니다.')
        if any(re.fullmatch(r'[0-9a-f]{64}', value[key]) is None
               for key in ('instruction', 'environment', 'policy')):
            raise ValueError('실제 Agent 실행 입력은 비밀값 없는 지문이어야 합니다.')
        with self.connection:
            result = self.connection.execute('''UPDATE agent_invocations SET execution_identity=?
                WHERE invocation_id=? AND finished_at IS NULL AND execution_identity IS NULL
                AND provider=? AND model=? AND effort=? AND EXISTS (
                    SELECT 1 FROM agent_isolation_evidence proof
                    WHERE proof.invocation_id=agent_invocations.invocation_id AND proof.status='verified')''',
                (json.dumps(value, sort_keys=True), identity, value['provider'], value['model'], value['effort']))
            if result.rowcount != 1:
                raise ValueError('실제 Agent 실행 입력과 현재 invocation·격리 증거가 다릅니다.')

    def execution_identity(self, identity: str, *, repo: str, plan_id: str | None,
                           role: str, worktree: str, provider: str) -> dict[str, str] | None:
        row = self.connection.execute('''SELECT call.execution_identity FROM agent_invocations call
            JOIN agent_isolation_evidence proof USING (invocation_id)
            WHERE call.invocation_id=? AND call.repo=? AND call.plan_id IS ? AND call.role=?
              AND call.provider=? AND proof.provider=call.provider AND proof.role=call.role
              AND proof.canonical_worktree=? AND proof.status='verified'
              AND call.execution_status='executed' AND call.execution_started_at IS NOT NULL
              AND call.execution_finished_at IS NOT NULL AND call.finished_at IS NOT NULL
              AND call.result_kind='success' AND call.result_code=0
              AND call.collection_status!='error' AND call.guidance_content_hash IS NOT NULL
              AND proof.snapshot_digest!='' AND proof.configuration_digest!=''
              AND call.execution_identity IS NOT NULL''',
            (identity, repo, plan_id, role, provider, worktree)).fetchone()
        return json.loads(row[0]) if row is not None else None

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
               replace_session: bool = False,
               execution_status: str | None = None,
               observed_at: str | None = None) -> None:
        with _transaction(self.connection):
            session_update = "session_id = ?" if replace_session else "session_id = COALESCE(?, session_id)"
            cursor = self.connection.execute(f"""UPDATE agent_invocations SET
                finished_at = ?, duration_ms = ?, result_kind = ?, result_code = ?,
                {session_update}, collection_status = ?,
                execution_status = COALESCE(?, execution_status),
                input_tokens = ?, cached_input_tokens = ?, cache_write_input_tokens = ?,
                output_tokens = ?, reasoning_output_tokens = ?
                WHERE invocation_id = ? AND finished_at IS NULL""",
                (observed_at or utc_now(), duration_ms, kind, code, session_id,
                 collection_status or usage.status, execution_status,
                 usage.input_tokens, usage.cached_input_tokens,
                 usage.cache_write_input_tokens, usage.output_tokens,
                 usage.reasoning_output_tokens, identity))
            if cursor.rowcount and kind != "success":
                self.connection.execute("""UPDATE agent_isolation_evidence
                    SET status = 'invalidated', reason = CASE WHEN provider='claude'
                        THEN reason ELSE 'agent_attempt_not_successful' END
                    WHERE invocation_id = ? AND status IN ('verified', 'prepared')""", (identity,))

    def apply_observation(self, identity: str, value: dict) -> None:
        """Apply already observed facts to this invocation; never execute a provider."""
        with _transaction(self.connection):
            if 'started_at' in value:
                self.mark_execution_started(identity, observed_at=value['started_at'])
            if 'finished_at' in value:
                self.mark_execution_finished(identity, value['duration_ms'], observed_at=value['finished_at'])
            if 'session_id' in value:
                self.observe_session(identity, value['session_id'])
            if 'outcome' in value:
                outcome = value['outcome']
                self.finish(identity, outcome['kind'], outcome['code'], outcome['duration_ms'],
                            outcome['session_id'], Usage(**outcome['usage']), outcome['collection_status'],
                            replace_session=True, observed_at=outcome['observed_at'],
                            execution_status='executed' if 'started_at' in value else 'not_executed')

    def reconcile_observations(self, *, state_dir, repo: str | None = None,
                               invocation_id: str | None = None) -> set[str]:
        """Recover receipt observations first; leave unreadable/pending records intact."""
        from loop_manager_internal.agents.process_receipt import observation
        from loop_manager_internal.storage.db_access import CoordinatedConnection
        from loop_manager_internal.runtime import operation_events as events
        pending = set()
        rows = self.connection.execute('''SELECT invocation_id, process_receipt_path FROM agent_invocations
            WHERE finished_at IS NULL AND process_receipt_path IS NOT NULL AND (? IS NULL OR repo=?)
            AND (? IS NULL OR invocation_id=?)''', (repo, repo, invocation_id, invocation_id)).fetchall()
        for identity, path in rows:
            try:
                value = observation(path, state_dir, identity)
                if value is None:
                    continue
                if isinstance(self.connection, CoordinatedConnection) and not self.connection.in_transaction:
                    self.connection.run_request(lambda current: self.apply_observation(identity, value))
                else:
                    self.apply_observation(identity, value)
            except (OSError, ValueError, TypeError, sqlite3.Error) as error:
                pending.add(identity)
                events.emit('Agent 호출 관측 저장 재개 대기', level='WARN', invocation_id=identity,
                            error_type=type(error).__name__)
        return pending

    def reconcile_interrupted(self, *, state_dir=None) -> int:
        from .runtime_state import _transaction
        from .work_claims import live_manual_tasks
        count = 0
        observations_pending = self.reconcile_observations(state_dir=state_dir) if state_dir is not None else set()
        with _transaction(self.connection):
            protected = live_manual_tasks(self.connection, state_dir) if state_dir is not None else frozenset()
            pending = self.connection.execute(
                'SELECT invocation_id,guidance_task_id FROM agent_invocations WHERE finished_at IS NULL').fetchall()
            for identity, task in pending:
                if task in protected or identity in observations_pending:
                    continue
                self.connection.execute("""UPDATE agent_isolation_evidence
                    SET status = 'invalidated', reason = CASE WHEN provider='claude'
                        THEN reason ELSE 'agent_attempt_interrupted' END
                    WHERE status IN ('verified', 'prepared') AND invocation_id = ?""", (identity,))
                count += self.connection.execute("""UPDATE agent_invocations SET
                    finished_at = ?, result_kind = 'interrupted', collection_status = 'unknown',
                    execution_status = CASE WHEN execution_status = 'prepared'
                                            THEN 'unknown' ELSE execution_status END
                    WHERE invocation_id = ? AND finished_at IS NULL""", (utc_now(),identity)).rowcount
        if state_dir is not None:
            self.reconcile_execution(state_dir=state_dir)
            from .targeted_test_state import TargetedTestRepository
            TargetedTestRepository(self.connection).reconcile(state_dir=state_dir)
        return count

    def summary(self, repo: str) -> list[sqlite3.Row]:
        """Count only proven process starts; legacy and pending execution stays unknown."""
        return self.connection.execute("""WITH observations AS (
            SELECT *,
                CASE WHEN execution_status = 'executed' THEN input_tokens END AS executed_input_tokens,
                CASE WHEN execution_status = 'executed' THEN cached_input_tokens END AS executed_cached_input_tokens,
                CASE WHEN execution_status = 'executed' THEN cache_write_input_tokens END AS executed_cache_write_input_tokens,
                CASE WHEN execution_status = 'executed' THEN output_tokens END AS executed_output_tokens,
                CASE WHEN execution_status = 'executed' THEN reasoning_output_tokens END AS executed_reasoning_output_tokens,
                CASE WHEN execution_status = 'executed' AND input_tokens IS NOT NULL
                          AND cached_input_tokens IS NOT NULL
                     THEN input_tokens END AS paired_input_tokens,
                CASE WHEN execution_status = 'executed' AND input_tokens IS NOT NULL
                          AND cached_input_tokens IS NOT NULL
                     THEN cached_input_tokens END AS paired_cached_input_tokens
            FROM agent_invocations WHERE repo = ?
        )
        SELECT role, purpose, plan_id, task_id, target, provider, model, effort, transition,
            SUM(CASE WHEN execution_status = 'executed' THEN 1 ELSE 0 END) AS calls,
            SUM(CASE WHEN execution_status = 'not_executed' THEN 1 ELSE 0 END) AS not_executed,
            SUM(CASE WHEN execution_status IN ('prepared', 'unknown') THEN 1 ELSE 0 END) AS execution_unknown,
            SUM(CASE WHEN execution_status = 'executed' AND attempt > 1 THEN 1 ELSE 0 END) AS retries,
            SUM(executed_input_tokens) AS input_tokens,
            SUM(executed_cached_input_tokens) AS cached_input_tokens,
            SUM(executed_cache_write_input_tokens) AS cache_write_input_tokens,
            SUM(executed_output_tokens) AS output_tokens,
            SUM(executed_reasoning_output_tokens) AS reasoning_output_tokens,
            SUM(CASE WHEN provider = 'claude' THEN executed_input_tokens
                     WHEN executed_input_tokens IS NOT NULL AND executed_cached_input_tokens IS NOT NULL
                     THEN executed_input_tokens - executed_cached_input_tokens END) AS uncached_input_tokens,
            CASE WHEN provider = 'claude' THEN
                     1.0 * SUM(CASE WHEN executed_cache_write_input_tokens IS NOT NULL
                                    THEN paired_cached_input_tokens END) /
                     NULLIF(SUM(paired_input_tokens + paired_cached_input_tokens
                                + executed_cache_write_input_tokens), 0)
                 ELSE 1.0 * SUM(paired_cached_input_tokens) /
                      NULLIF(SUM(paired_input_tokens), 0)
            END AS cache_ratio,
            SUM(execution_duration_ms) AS duration_ms
            FROM observations
            GROUP BY role, purpose, plan_id, task_id, target, provider, model, effort, transition""", (repo,)).fetchall()
