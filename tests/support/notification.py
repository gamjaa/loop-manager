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
                       proposal='Grant the documented permission or choose a supported alternative.'):
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
        detail = f'{problem}\n{proposal}\n{question}'
        fingerprint = hashlib.sha256(json.dumps(declaration, sort_keys=True).encode()).hexdigest()
        RecoveryRepository(connection).observe(RecoveryObservation(
            RecoveryReason.USER_DECISION, repo, plan_id, str(plan['status']),
            str(plan['current_revision_sha'] or plan['plan_sha'] or ''), str(plan['active_sha'] or ''),
            fingerprint, 'worker-question', detail, worker_decision=outcome.evidence()))
        RuntimeStateRepository(connection).block_plan(repo, plan_id,
            reason_code='user_decision_required', evidence=detail,
            release_fingerprint=fingerprint, enqueue=False)
        checkpoints = BlockedCheckpointRepository(connection)
        checkpoints.finish_unpublished(checkpoints.get(repo, plan_id), 'local-only test artifact', retryable=False)
        notifications.enqueue_current_project_events(connection, repo)
        return detail

    @staticmethod
    def events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM notification_outbox ORDER BY event_type"
        ).fetchall()
