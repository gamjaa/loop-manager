"""Shared SQLite fixture for notification unit tests."""
import sqlite3
from pathlib import Path
import loop_manager
from loop_manager_internal.notifications import notifications

class NotificationFixture:
    def connection(
        self,
        *,
        enabled: bool = True,
        database: str | Path = ":memory:",
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(database)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            notifications.ensure_schema(connection)
            notifications.configure_session(connection, enabled=enabled)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                ("/projects/alpha",),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status
                ) VALUES (?, 'G-018', ?, 'Reliable notices', ?, '[]', 'high', 'hash', 'READY')
                """,
                (
                    "/projects/alpha",
                    "docs/plans/[G-018] Reliable notices.md",
                    '["docs/PRODUCT.md"]',
                ),
            )
            connection.execute(
                "UPDATE projects SET plan_path = ? WHERE repo = ?",
                ("docs/plans/[G-018] Reliable notices.md", "/projects/alpha"),
            )
            connection.commit()
        except BaseException:
            connection.close()
            raise
        if database == ":memory:":
            self.addCleanup(connection.close)
        return connection

    def decision_block(self, connection, *, question='Which permission should be granted?',
                       problem='The required repository permission is unavailable.',
                       proposal='Grant the documented permission or choose a supported alternative.',
                       detail=None, fingerprint=None, record_block=True):
        """Persist a parsed Worker question through the real recovery/block repositories."""
        import hashlib
        import json
        from loop_manager_internal.execution.worker_outcome import parse_worker_outcome
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
        from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
        repo, plan_id = '/projects/alpha', 'G-018'
        plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
        declaration = {'kind': 'needs_user_decision', 'problem': problem, 'proposal': proposal,
                       'question': question, 'locations': [{'path': plan['path'], 'section': 'Scope'}]}
        outcome = parse_worker_outcome('WORKER_OUTCOME: ' + json.dumps(declaration),
            plan_path=plan['path'], sources=tuple(json.loads(plan['sources'])),
            documents={plan['path']: '## Scope\nPermission decision.'})
        detail = detail if detail is not None else f'{problem}\n{proposal}\n{question}'
        fingerprint = fingerprint or hashlib.sha256(json.dumps(declaration, sort_keys=True).encode()).hexdigest()
        previous = RecoveryRepository(connection).row(repo, plan_id)
        RecoveryRepository(connection).observe(RecoveryObservation(
            RecoveryReason.USER_DECISION, repo, plan_id, str(previous['phase'] if previous else plan['status']),
            str(plan['current_revision_sha'] or plan['plan_sha'] or ''), str(plan['active_sha'] or ''),
            fingerprint, str(previous['progress_marker'] if previous else 'worker-question'), detail, worker_decision=outcome.evidence()))
        if not record_block:
            return detail
        RuntimeStateRepository(connection).block_plan(repo, plan_id,
            reason_code='user_decision_required', evidence=detail,
            release_fingerprint=fingerprint, enqueue=False)
        checkpoints = BlockedCheckpointRepository(connection)
        checkpoints.finish_unpublished(checkpoints.get(repo, plan_id), 'local-only test artifact', retryable=False)
        notifications.enqueue_current_project_events(connection, repo)
        return detail

    def confirm_worker_question(self, connection):
        """Supply an explicit parsed question for a fixture's current plan block."""
        block = connection.execute("SELECT evidence, release_fingerprint FROM execplan_blocks WHERE repo='/projects/alpha' AND plan_id='G-018' AND reason_code='user_decision_required'").fetchone()
        if block is not None:
            self.decision_block(connection, detail=block[0], fingerprint=block[1], record_block=False)

    def legacy_planning_notice(self, connection, cause='old planning error', *, confirmed=0, sent=False):
        """Seed a persisted old-policy notice, rather than generate a current event."""
        repo = '/projects/alpha'
        connection.execute('UPDATE projects SET planning_error=? WHERE repo=?', (cause, repo))
        key = notifications._key('blocked', repo, 'project\0planning_error\0')
        notifications._insert(connection, key, repo, 'blocked',
            {'project': 'alpha', 'stage': 'planning', 'scope': 'project',
             'reason_code': 'planning_error', 'execplan_id': None, 'cause': cause, 'paths': []})
        connection.execute('UPDATE notification_outbox SET confirmed_pieces=?, sent_at=CASE WHEN ? THEN CURRENT_TIMESTAMP END WHERE event_key=?', (confirmed, int(sent), key))
        notifications.adopt_legacy_block_notices(connection)
        return key

    def legacy_project_notice(self, connection):
        """Seed an already confirmed safety notice from the former policy."""
        repo = '/projects/alpha'
        block = connection.execute('SELECT * FROM project_blocks WHERE repo=?', (repo,)).fetchone()
        assert block is not None
        from loop_manager_internal.notifications.incidents import _project_block_key
        key = _project_block_key(repo, block['reason_code'], block['plan_id'])
        notifications._insert(connection, key, repo, 'blocked',
            {'project': 'alpha', 'stage': 'project', 'scope': 'project',
             'execplan_id': block['plan_id'], 'reason_code': block['reason_code'],
             'cause': block['evidence'], 'paths': []})
        connection.execute('UPDATE notification_outbox SET confirmed_pieces=1, sent_at=CURRENT_TIMESTAMP WHERE event_key=?', (key,))
        notifications.adopt_legacy_block_notices(connection)

    @staticmethod
    def events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM notification_outbox ORDER BY event_type"
        ).fetchall()
