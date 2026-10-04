import sqlite3
import unittest
from pathlib import Path
from unittest import mock
from loop_manager_internal.notifications import notifications

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery import checkpoint_service
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import blocked_checkpoint_state
import loop_manager
from loop_manager_internal.storage import runtime_state


class BlockedCheckpointRepositoryTest(unittest.TestCase):
    def test_diagnostic_intent_reuse_does_not_promote_original_ownership(self):
        request = self.request()
        cause = self.diagnostic_cause('original failure')
        repository = blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection)
        original = dict(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        intent = repository.intent(request, base_oid='a' * 40, head_oid='b' * 40,
                                   tree_oid='c' * 40, cause='original failure')
        self.connection.execute(
            "UPDATE project_blocks SET evidence = 'later observation' WHERE repo = 'repo'")
        current_cause = scoped_blocking.project_cause_identity(
            scoped_blocking.project_block(self.connection, 'repo'))
        self.assertNotEqual(cause, current_cause)
        reused = repository.intent(request, base_oid='a' * 40, head_oid='b' * 40,
                                   tree_oid='c' * 40, cause='later observation')
        self.assertEqual(dict(intent), dict(reused))
        payload = dict(result='published', diagnostic=True, branch=intent['ref'], commit_oid='b' * 40,
                       original_branch=request.branch, base_oid='a' * 40)
        with self.assertRaises(RuntimeError):
            repository.result('repo', 'G-034', cause, payload, intent=reused)
        self.assertIsNone(self.connection.execute('SELECT * FROM project_checkpoint_results').fetchone())
        repository.result('repo', 'G-034', current_cause, payload, intent=reused)
        repository.result('repo', 'G-034', current_cause, payload, intent=reused)
        self.assertEqual(self.connection.execute(
            'SELECT cause_identity FROM project_checkpoint_results').fetchone()[0], current_cause)
        self.assertEqual(self.connection.execute(
            'SELECT evidence FROM project_blocks').fetchone()[0], 'later observation')
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), original)
        self.assertIsNone(self.connection.execute('SELECT accepted_remote_oid FROM blocked_checkpoints').fetchone()[0])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM blocked_diagnostic_publications').fetchone()[0], 1)
        self.assertEqual(self.connection.execute('SELECT confirmed_oid FROM blocked_diagnostic_publications').fetchone()[0], 'b' * 40)
        with self.assertRaises(RuntimeError):
            repository.result('repo', 'G-034', current_cause, {**payload, 'commit_oid': 'd' * 40}, intent=reused)

    def test_diagnostic_result_and_notification_rollback_together(self):
        request = self.request()
        cause = self.diagnostic_cause()
        repository = blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection)
        intent = repository.intent(request, base_oid='a' * 40, head_oid='b' * 40, tree_oid='c' * 40, cause='failure')
        with mock.patch.object(notifications, 'enqueue_current_project_events', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                repository.result('repo', 'G-034', cause,
                                  dict(result='published', diagnostic=True, branch=intent['ref'],
                                       commit_oid='b' * 40, original_branch=request.branch,
                                       base_oid='a' * 40), intent=intent)
        self.assertIsNone(self.connection.execute('SELECT confirmed_oid FROM blocked_diagnostic_publications').fetchone()[0])
        self.assertIsNone(self.connection.execute('SELECT * FROM project_checkpoint_results').fetchone())

    def test_diagnostic_result_rejects_stale_cause_and_missing_intent_row(self):
        request = self.request()
        cause = self.diagnostic_cause()
        repository = blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection)
        intent = repository.intent(request, base_oid='a' * 40, head_oid='b' * 40,
                                   tree_oid='c' * 40, cause='failure')
        payload = dict(result='published', diagnostic=True, branch=intent['ref'], commit_oid='b' * 40,
                       original_branch=request.branch, base_oid='a' * 40)
        with self.assertRaises(RuntimeError):
            repository.result('repo', 'G-034', 'stale-cause', payload, intent=intent)
        self.connection.execute('DELETE FROM blocked_diagnostic_publications')
        with self.assertRaises(RuntimeError):
            repository.result('repo', 'G-034', cause, payload, intent=intent)
        self.assertIsNone(self.connection.execute('SELECT * FROM project_checkpoint_results').fetchone())

    def test_transient_checkpoint_observation_keeps_unattempted_request_pending(self):
        first = self.request()
        self.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, "
            "content_hash, status, branch, worktree, workspace_owned) "
            "VALUES ('repo', 'G-035', 'docs/plans/[G-035] Other.md', 'Other', '[]', '[]', "
            "'critical', 'hash', 'PLAN_BLOCKED', 'loop-manager/G-035', '/state/G-035', 1)"
        )
        second = self.repository.request(
            repo='repo', plan_id='G-035', reason_code='verify_retry_exhausted',
            evidence='other failure', branch='loop-manager/G-035', worktree='/state/G-035',
            expected_remote_oid=None, rebase_recovery=False,
        )
        effects = checkpoint_service.DispatchBlockedCheckpointsEffects(
            _BlockedCheckpointAdapter=lambda *_args: mock.Mock())
        with mock.patch.object(
                blocked_checkpoint.BlockedCheckpointDispatcher, '_dispatch_one',
                side_effect=RuntimeError('PR observation unavailable')) as attempted:
            self.assertEqual(checkpoint_service.dispatch_blocked_checkpoints(
                effects, self.connection, Path('/repo'), Path('/state'), 'repo'), 0)
        attempted.assert_called_once_with(first)
        self.assertEqual(self.repository.get('repo', first.plan_id).phase.value, 'retry_pending')
        self.assertEqual(self.repository.get('repo', second.plan_id).phase.value, 'requested')

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, branch, worktree, workspace_owned
            ) VALUES ('repo', 'G-034', 'docs/plans/[G-034] Checkpoint.md',
                      'Checkpoint', '["docs/PRODUCT.md"]', '[]', 'critical',
                      'hash', 'PLAN_BLOCKED', 'loop-manager/G-034',
                      '/state/G-034', 1)
            """
        )
        self.repository = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        )

    def tearDown(self):
        self.connection.close()

    def test_terminal_result_and_outbox_rollback_and_retry_together(self):
        notifications.configure_session(self.connection, enabled=True)
        notifications.ensure_schema(self.connection)
        for published in (True, False):
            with self.subTest(published=published):
                request = self.request(evidence=str(published))
                self.connection.execute(
                    "INSERT OR REPLACE INTO execplan_blocks "
                    "(repo, plan_id, scope, reason_code, evidence) "
                    "VALUES ('repo', 'G-034', 'plan', 'verify_retry_exhausted', ?)",
                    (str(published),),
                )
                self.connection.execute("DELETE FROM notification_outbox")
                self.connection.commit()
                def finish():
                    if published:
                        self.repository.finish_published(request, "a" * 40)
                    else:
                        self.repository.finish_unpublished(request, "local only", retryable=False)
                enqueue = notifications.enqueue_current_project_events
                def interrupted(connection, repo):
                    enqueue(connection, repo)
                    raise KeyboardInterrupt()
                with mock.patch.object(notifications, "enqueue_current_project_events", side_effect=interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        finish()
                self.assertEqual(len(self.repository.pending("repo")), 1)
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
                finish()
                self.assertEqual(self.repository.pending("repo"), ())
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
                finish()
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)

    def request(self, evidence="failure", release_fingerprint=None):
        return self.repository.request(
            repo="repo",
            plan_id="G-034",
            reason_code="verify_retry_exhausted",
            evidence=evidence,
            branch="loop-manager/G-034",
            worktree="/state/G-034",
            expected_remote_oid=None,
            rebase_recovery=False,
            release_fingerprint=release_fingerprint,
        )

    def diagnostic_cause(self, evidence='failure'):
        self.connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence) "
            "VALUES ('repo', 'project', 'repository_state_unsafe', 'G-034', ?)",
            (evidence,),
        )
        return scoped_blocking.project_cause_identity(
            scoped_blocking.project_block(self.connection, 'repo'))

    def test_request_and_phase_progress_are_durable_and_idempotent(self):
        request = self.request()
        prepared = blocked_checkpoint.PreparedCheckpoint(
            parent_oid="a" * 40,
            tree_oid="b" * 40,
            commit_identity="identity",
            message="message",
            included_paths=("code.py",),
        )
        self.repository.record_prepared(request, prepared)
        self.repository.record_local(request, "c" * 40)
        observed = self.repository.record_remote_observation(request, None)
        self.assertTrue(observed.remote_observed)
        self.repository.finish_unpublished(
            observed, "network unavailable", retryable=True
        )

        resumed = self.repository.pending("repo")
        self.assertEqual(len(resumed), 1)
        self.assertIs(resumed[0].phase, blocked_checkpoint.CheckpointPhase.RETRY_PENDING)
        self.assertEqual(resumed[0].local_oid, "c" * 40)
        same = self.request()
        self.assertIs(same.phase, blocked_checkpoint.CheckpointPhase.RETRY_PENDING)
        changed = self.request(evidence="new failure")
        self.assertIs(changed.phase, blocked_checkpoint.CheckpointPhase.REQUESTED)
        self.assertNotEqual(changed.request_identity, same.request_identity)
        changed_fingerprint = self.request(
            evidence="new failure", release_fingerprint="new-input"
        )
        self.assertNotEqual(
            changed_fingerprint.request_identity,
            changed.request_identity,
        )

    def test_published_result_records_remote_oid_on_execplan_without_unblocking(self):
        request = self.request()
        oid = "d" * 40
        self.repository.finish_published(request, oid)

        row = self.connection.execute(
            "SELECT status, publication_remote_oid FROM exec_plans"
        ).fetchone()
        self.assertEqual(tuple(row), ("PLAN_BLOCKED", oid))
        self.assertEqual(self.repository.pending("repo"), ())
        checkpoint = self.connection.execute(
            "SELECT phase, result, remote_oid, published_oid, accepted_remote_oid "
            "FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(checkpoint),
                         ("published", "published", oid, oid, oid))

    def test_remote_inspection_keeps_original_publication_oid(self):
        request = self.request()
        published = "a" * 40
        candidate = "b" * 40
        self.repository.finish_published(request, published)
        self.repository.start_remote_adoption(request, candidate)
        self.repository.record_remote_inspection(
            request, candidate, "input", "still blocked",
        )
        row = self.connection.execute("SELECT * FROM blocked_checkpoints").fetchone()
        self.assertEqual(row["published_oid"], published)
        self.assertEqual(row["remote_oid"], published)
        self.assertEqual(row["accepted_remote_oid"], candidate)
        self.assertEqual(row["inspected_remote_oid"], candidate)
        self.assertEqual(row["inspection_reason"], "still blocked")
        self.assertEqual(row["inspection_retryable"], 0)
        self.assertIsNone(row["pending_remote_oid"])

    def test_existing_checkpoint_table_gains_remote_inspection_columns(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE blocked_checkpoints (repo TEXT, plan_id TEXT, "
                "remote_oid TEXT, PRIMARY KEY (repo, plan_id))"
            )
            blocked_checkpoint_state.ensure_schema(connection)
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(blocked_checkpoints)"
                )
            }
            self.assertTrue({
                "published_oid", "accepted_remote_oid", "pending_remote_oid",
                "inspected_remote_oid", "inspected_input", "inspection_reason",
                "inspection_retryable",
            }.issubset(columns))
        finally:
            connection.close()

    def test_plan_block_and_checkpoint_request_share_one_transaction(self):
        self.connection.execute(
            "DELETE FROM blocked_checkpoints WHERE repo = 'repo'"
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE repo = 'repo'"
        )
        self.connection.execute(
            "UPDATE projects SET phase = 'WORKING', plan_path = ? "
            "WHERE repo = 'repo'",
            ("docs/plans/[G-034] Checkpoint.md",),
        )
        self.connection.commit()

        def fail(stage):
            if stage == "after_project_update":
                raise RuntimeError(stage)

        repository = runtime_state.RuntimeStateRepository(
            self.connection, fault_hook=fail
        )
        with self.assertRaisesRegex(RuntimeError, "after_project_update"):
            repository.block_plan(
                "repo",
                "G-034",
                reason_code="review_retry_exhausted",
                evidence="review failure",
                release_fingerprint="fingerprint",
                clear_projection=True,
            )
        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(plan["status"], "IMPLEMENTING")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM execplan_blocks WHERE repo = 'repo'"
            ).fetchone()
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM blocked_checkpoints WHERE repo = 'repo'"
            ).fetchone()
        )

        with self.connection:
            runtime_state.RuntimeStateRepository(self.connection).block_plan(
                "repo",
                "G-034",
                reason_code="review_retry_exhausted",
                evidence="review failure",
                release_fingerprint="fingerprint",
                clear_projection=True,
            )
        stored = self.connection.execute(
            "SELECT p.status, b.reason_code, c.phase "
            "FROM exec_plans AS p JOIN execplan_blocks AS b USING (repo, plan_id) "
            "JOIN blocked_checkpoints AS c USING (repo, plan_id)"
        ).fetchone()
        self.assertEqual(
            tuple(stored),
            ("PLAN_BLOCKED", "review_retry_exhausted", "requested"),
        )
