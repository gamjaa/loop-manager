import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import RecoveryRepository, begin_external_run

from tests.support.notification import NotificationFixture


class IncidentPayloadEncodingTest(NotificationFixture, unittest.TestCase):
    def test_incident_and_outbox_share_unicode_payload_encoding(self):
        connection = self.connection()
        self.addCleanup(connection.close)
        with connection:
            self.decision_block(connection, problem="원인 \U0001f642")
        incident = connection.execute(
            "SELECT payload FROM notified_block_incidents WHERE subject = 'plan:G-018'"
        ).fetchone()[0]
        outbox = connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0]
        self.assertEqual(incident, outbox)
        self.assertIn("원인 \U0001f642", json.loads(outbox)["cause"])
        self.assertIn("원인", outbox)


class ProjectCheckpointNotificationUnitTests(NotificationFixture, unittest.TestCase):
    def setUp(self):
        self.db = self.connection()
        self.addCleanup(self.db.close)
        self.repo = '/projects/alpha'
        self.branch = 'loop-manager/G-018'
        self.worktree = '/state/G-018'
        self.db.execute("UPDATE projects SET origin = 'example/repo', phase = 'PROJECT_BLOCKED'")
        self.db.execute("UPDATE exec_plans SET branch = ?, worktree = ? WHERE repo = ? AND plan_id = 'G-018'",
                        (self.branch, self.worktree, self.repo))
        self.db.execute("INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence) "
                        "VALUES (?, 'project', 'publication_ownership_uncertain', 'G-018', 'original cause')", (self.repo,))
        self.db.commit()
        self.repository = blocked_checkpoint_state.DiagnosticPublicationRepository(self.db)
        self.cause = scoped_blocking.project_cause_identity(scoped_blocking.project_block(self.db, self.repo))

    def test_diagnostic_notice_contains_confirmed_ref_oid_and_original_branch_once(self):
        request = blocked_checkpoint_state.BlockedCheckpointRepository(self.db).request(
            repo=self.repo, plan_id='G-018', reason_code='publication_ownership_uncertain',
            evidence='original cause', branch=self.branch, worktree=self.worktree,
            expected_remote_oid=None, rebase_recovery=False)
        intent = self.repository.intent(request, base_oid='b' * 40, head_oid='a' * 40,
                                        tree_oid='c' * 40, cause='original cause')
        payload = dict(result='published', diagnostic=True, branch=intent['ref'],
                       commit_oid='a' * 40, original_branch=self.branch, base_oid='b' * 40)
        self.repository.result(self.repo, 'G-018', self.cause, payload, intent=intent)
        self.repository.result(self.repo, 'G-018', self.cause, payload, intent=intent)
        self.assertEqual(self.events(self.db), [])
        decoded = notifications._project_checkpoint(self.db, self.repo, 'G-018')
        self.assertEqual(decoded['branch'], intent['ref'])
        self.assertEqual(decoded['original_branch'], self.branch)
        self.assertEqual(decoded['commit_oid'], 'a' * 40)
        self.assertIn('a' * 40, decoded['branch_url'])

    def test_local_only_notice_reports_actual_reason_and_ignores_stale_cause_result(self):
        self.repository.result(self.repo, 'G-018', self.cause,
                               dict(result='local_only', reason='unresolved rebase conflict',
                                    original_branch=self.branch, worktree=self.worktree))
        self.assertEqual(self.events(self.db), [])
        decoded = notifications._project_checkpoint(self.db, self.repo, 'G-018')
        self.assertEqual(decoded['reason'], 'unresolved rebase conflict')
        self.assertEqual(decoded['worktree'], '/state/G-018')
        self.db.execute("UPDATE project_blocks SET evidence = 'new independent cause'")
        self.assertIsNone(notifications._project_checkpoint(self.db, self.repo, 'G-018'))


class NotificationOutboxTest(NotificationFixture, unittest.TestCase):
    def test_final_block_replaces_partial_legacy_failure_notice_and_keeps_its_audit(self):
        connection = self.connection()
        repo = "/projects/alpha"
        connection.execute(
            "UPDATE exec_plans SET current_revision_sha = 'plan-revision', "
            "active_sha = 'base-revision' WHERE repo = ? AND plan_id = 'G-018'",
            (repo,),
        )
        begin_external_run(connection, "final-block-run")
        RecoveryRepository(connection).observe(RecoveryObservation(
            RecoveryReason.USER_DECISION, repo, "G-018", "READY",
            "plan-revision", "base-revision", "decision-fingerprint",
            "base-revision", "User decision required",
        ))
        failure = connection.execute(
            "SELECT episode, phase, reason_code, fingerprint, consecutive_runs "
            "FROM persistent_execution_failures WHERE repo = ? AND subject = 'G-018'",
            (repo,),
        ).fetchone()
        identity = "\0".join((
            "G-018", str(failure[0]), str(failure[1]), str(failure[2]),
            str(failure[3]),
        ))
        legacy_key = notifications._key("persistent_failure", repo, identity)
        legacy_payload = {
            "project": "alpha", "stage": str(failure[1]), "scope": "execution",
            "execplan_id": "G-018", "reason_code": str(failure[2]),
            "cause": "User decision required", "consecutive_external_runs": 1,
        }
        notifications._insert(
            connection, legacy_key, repo, "persistent_execution_failure",
            legacy_payload,
        )
        legacy_payload_before = connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_key = ?",
            (legacy_key,),
        ).fetchone()[0]
        frozen_text = "already confirmed legacy text"
        connection.execute(
            "UPDATE notification_outbox SET attempts = 2, last_error = ?, "
            "rendered_text = ?, confirmed_pieces = 1, "
            "delivery_started_at = CURRENT_TIMESTAMP WHERE event_key = ?",
            ("previous send error", frozen_text, legacy_key),
        )

        with connection:
            scoped_blocking.record_plan_block(
                connection, repo, "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "User decision required", "decision-fingerprint",
            )
        self.confirm_worker_question(connection)
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        checkpoint.finish_unpublished(
            checkpoint.get(repo, "G-018"), "local only", retryable=False,
        )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)

        old = connection.execute(
            "SELECT attempts, last_error, rendered_text, confirmed_pieces, "
            "canceled_at, delivery_started_at, sent_at, payload "
            "FROM notification_outbox WHERE event_key = ?", (legacy_key,),
        ).fetchone()
        self.assertEqual(old[0:4], (2, "previous send error", frozen_text, 1))
        self.assertIsNotNone(old[4])
        self.assertIsNone(old[5])
        self.assertIsNone(old[6])
        self.assertEqual(old[7], legacy_payload_before)
        self.assertEqual(
            [row["event_type"] for row in self.events(connection)],
            ["blocked", "persistent_execution_failure"],
        )
        failure = connection.execute(
            "SELECT active, consecutive_runs, notification_eligible, "
            "notification_queued, suppressed_by_block FROM "
            "persistent_execution_failures WHERE repo = ? AND subject = 'G-018'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(failure), (0, 1, 1, 0, 1))

        delivered = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, text: delivered.append(text),
        ), 1)
        self.assertEqual(len(delivered), 1)
        self.assertIn("[alpha] BLOCKED", delivered[0])
        self.assertIn("사용자 조치 대기", delivered[0])


    def test_existing_delivered_plan_notice_is_adopted_without_duplicate_delivery(self):
        connection = self.connection()
        repo = "/projects/alpha"
        reason = "user_decision_required"
        key = notifications._key("blocked", repo, f"plan\0{reason}\0G-018")
        payload = {
            "project": "alpha", "stage": "ExecPlan", "scope": "plan",
            "reason_code": reason, "execplan_id": "G-018",
            "cause": "old notified cause", "paths": [],
        }
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', ?, 'later mutable error')",
                (repo, reason),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, sent_at) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP)",
                (key, repo, json.dumps(payload)),
            )
        with connection:
            notifications.ensure_schema(connection)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM notified_block_incidents WHERE repo = ?", (repo,),
            ).fetchone())
        with self.assertRaisesRegex(RuntimeError, "interrupted adoption"):
            with connection:
                notifications.adopt_legacy_block_notices(connection)
                raise RuntimeError("interrupted adoption")
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents"
        ).fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM block_notice_events"
        ).fetchone()[0], 0)
        with connection:
            notifications.adopt_legacy_block_notices(connection)
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0], 1)
        incident = connection.execute(
            "SELECT event_key FROM notified_block_incidents WHERE repo = ?",
            (repo,),
        ).fetchone()
        self.assertEqual(incident[0], key)
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 1)
        resumed = connection.execute(
            "SELECT payload FROM notified_block_incidents ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(json.loads(resumed[0])["cause"], "old notified cause")

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_legacy_planning_notice_is_adopted_before_release_without_reenqueue(self):
        connection = self.connection()
        repo = "/projects/alpha"
        key = notifications._key("blocked", repo, "project\0planning_error\0")
        payload = {
            "project": "alpha", "stage": "planning", "scope": "project",
            "reason_code": "planning_error", "execplan_id": None,
            "cause": "old planning error", "paths": [],
        }
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "UPDATE projects SET planning_error = 'old planning error' "
                "WHERE repo = ?", (repo,),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, sent_at) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP)",
                (key, repo, json.dumps(payload)),
            )
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0], 1)


    def test_legacy_partial_project_notice_cancels_remaining_piece_on_release(self):
        connection = self.connection()
        repo = "/projects/alpha"
        reason = "repository_state_unsafe"
        key = notifications._key("blocked", repo, f"project\0{reason}\0")
        payload = {
            "project": "alpha", "stage": "project", "scope": "project",
            "reason_code": reason, "execplan_id": None,
            "cause": "old project cause", "paths": [],
        }
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
                "VALUES (?, 'project', ?, 'later mutable error')",
                (repo, reason),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, rendered_text, "
                "confirmed_pieces) VALUES (?, ?, 'blocked', ?, ?, 1)",
                (key, repo, json.dumps(payload), "x" * 5000),
            )
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        old = connection.execute(
            "SELECT canceled_at, confirmed_pieces FROM notification_outbox "
            "WHERE event_key = ?", (key,),
        ).fetchone()
        self.assertIsNotNone(old[0])
        self.assertEqual(old[1], 1)
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()[0], 0)

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_legacy_changed_checkpoint_notices_share_one_active_incident(self):
        connection = self.connection()
        repo = "/projects/alpha"
        reason = "user_decision_required"
        current_key = notifications._key("blocked", repo, f"plan\0{reason}\0G-018")
        old_key = notifications._key(
            "blocked", repo, f"plan\0{reason}\0G-018\0old-checkpoint",
        )
        old_payload = {
            "project": "alpha", "stage": "ExecPlan", "scope": "plan",
            "reason_code": reason, "execplan_id": "G-018",
            "cause": "first notified cause", "paths": [],
        }
        new_payload = {**old_payload, "cause": "later block detail"}
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', ?, 'later block detail')",
                (repo, reason),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, rendered_text, "
                "confirmed_pieces) VALUES (?, ?, 'blocked', ?, ?, 1)",
                (old_key, repo, json.dumps(old_payload), "x" * 5000),
            )
            connection.execute(
                "INSERT INTO notification_outbox (event_key, repo, event_type, payload) "
                "VALUES (?, ?, 'blocked', ?)",
                (current_key, repo, json.dumps(new_payload)),
            )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents WHERE repo = ?",
            (repo,),
        ).fetchone()[0], 1)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0], 2)
        self.assertIsNotNone(connection.execute(
            "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
            (old_key,),
        ).fetchone()[0])
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 1)
        resumed = connection.execute(
            "SELECT payload FROM notified_block_incidents ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(json.loads(resumed[0])["cause"], "first notified cause")

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_older_legacy_notice_does_not_notify_new_undelivered_same_reason_block(self):
        connection = self.connection()
        repo = "/projects/alpha"
        reason = "user_decision_required"
        old_key = notifications._key("blocked", repo, f"plan\0{reason}\0G-018")
        payload = {
            "project": "alpha", "stage": "ExecPlan", "scope": "plan",
            "reason_code": reason, "execplan_id": "G-018",
            "cause": "prior incident", "paths": [],
        }
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', ?, 'new incident')",
                (repo, reason),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, sent_at, created_at) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP, '2000-01-01 00:00:00')",
                (old_key, repo, json.dumps(payload)),
            )
        with connection:
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0], 2)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()[0], 0)

    def test_notified_plan_resumes_only_at_its_worker_start_and_new_block_is_distinct(self):
        connection = self.connection()
        repo = "/projects/alpha"

        def block():
            with connection:
                connection.execute(
                    "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                    "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'Decision pending')",
                    (repo,),
                )
                self.confirm_worker_question(connection)
                notifications.enqueue_current_project_events(connection, repo)

        block()
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        ), 1)
        first_key = next(row["event_key"] for row in self.events(connection)
                         if row["event_type"] == "blocked")
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-019",
            ), 0)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 1)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
        resumed = [row for row in self.events(connection)
                   if row["event_type"] == "resumed"]
        self.assertEqual(len(resumed), 0)
        self.assertEqual(json.loads(connection.execute("SELECT payload FROM notified_block_incidents").fetchone()[0])["cause"], "Decision pending")
        block()
        second_key = next(row["event_key"] for row in self.events(connection)
                          if row["event_type"] == "blocked"
                          and row["event_key"] != first_key)
        self.assertNotEqual(first_key, second_key)

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_partial_block_release_cancels_stale_piece_and_retries_resumption(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', ?)",
                (repo, "x" * 9000),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        sent = []

        def fail_second(_token, _chat, piece):
            sent.append(piece)
            if len(sent) == 2:
                raise RuntimeError("offline")

        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=fail_second,
        ), 0)
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 1)
        blocked = next(row for row in self.events(connection)
                       if row["event_type"] == "blocked")
        self.assertEqual(blocked["confirmed_pieces"], 1)
        self.assertIsNotNone(blocked["canceled_at"])
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=lambda *_: (_ for _ in ()).throw(
                RuntimeError("offline")
            ),
        ), 0)
        delivered = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=lambda _t, _c, text: delivered.append(text),
        ), 0)
        self.assertEqual(delivered, [])
        self.assertEqual(json.loads(connection.execute('SELECT payload FROM notified_block_incidents').fetchone()[0])['cause'], 'x' * 9000)

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_project_and_planning_release_wait_for_matching_work(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
                "VALUES (?, 'project', 'repository_state_unsafe', 'unsafe worktree')",
                (repo,),
            )
            self.legacy_project_notice(connection)
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        with connection:
            self.legacy_planning_notice(connection, "invalid plan", confirmed=1, sent=True)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        payloads = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM notified_block_incidents WHERE started_operation IS NOT NULL ORDER BY id")]
        self.assertEqual([item["reason_code"] for item in payloads],
                         ["repository_state_unsafe", "planning_error"])

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_disabled_telegram_never_creates_resumption(self):
        connection = self.connection(enabled=False)
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'decision')",
                (repo,),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
        self.assertEqual(self.events(connection), [])

    def test_disabling_telegram_after_notice_suppresses_late_resumption(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'decision')",
                (repo,),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
        notifications.configure_session(connection, enabled=False)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 1)
        incident = connection.execute('SELECT started_operation, resumed_at FROM notified_block_incidents').fetchone()
        self.assertEqual(incident[0], 'Worker')
        self.assertIsNotNone(incident[1])
        notifications.configure_session(connection, enabled=True)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
        self.assertFalse(any(row["event_type"] == "resumed"
                             for row in self.events(connection)))

    def test_restart_preserves_confirmed_incident_and_unconfirmed_block_stays_silent(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        database = Path(directory) / "notices.db"
        connection = self.connection(database=database)
        self.addCleanup(connection.close)
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'original cause')",
                (repo,),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        )
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
        connection.close()

        restarted = sqlite3.connect(database)
        self.addCleanup(restarted.close)
        restarted.row_factory = sqlite3.Row
        notifications.configure_session(restarted, enabled=True)
        with restarted:
            self.assertEqual(notifications.record_work_start(
                restarted, repo, "Worker", plan_id="G-018",
            ), 1)
            restarted.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'new cause')",
                (repo,),
            )
            self.confirm_worker_question(restarted)
            notifications.enqueue_current_project_events(restarted, repo)
            scoped_blocking.clear_plan_block(restarted, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                restarted, repo, "Worker", plan_id="G-018",
            ), 0)
        self.assertEqual(len([row for row in self.events(restarted)
                              if row["event_type"] == "resumed"]), 0)

        self.assertEqual(restarted.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_reopened_pr_records_resumption_in_transition(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
                "blocked_reason = 'pull_request_closed' WHERE repo = ?", (repo,),
            )
            connection.execute(
                "UPDATE projects SET phase = 'PLAN_BLOCKED' WHERE repo = ?", (repo,),
            )
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'pull_request_closed', 'PR closed')",
                (repo,),
            )
            connection.execute("UPDATE exec_plans SET pr_url='https://example.test/pr/18'")
            scoped_blocking.record_plan_block(connection, repo, 'G-018', scoped_blocking.BlockReasonCode.PULL_REQUEST_CLOSED, 'PR closed', 'closed-head')
            checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            checkpoints.finish_unpublished(checkpoints.get(repo, 'G-018'), 'local-only', retryable=False)
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-018'",
            (repo,),
        ).fetchone()
        loop_manager._resume_reopened_pull_request(
            connection, repo, plan, "https://example.test/pr/18", "head",
            recovery=False,
        )
        payload = connection.execute(
            "SELECT payload FROM notified_block_incidents ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(payload)
        incident = connection.execute('SELECT started_operation, resumed_at FROM notified_block_incidents').fetchone()
        self.assertEqual(incident[0], 'PR handling')
        self.assertIsNotNone(incident[1])
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'PR_OPEN')

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_inflight_block_piece_confirmed_after_release_still_resumes(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'decision')",
                (repo,),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)

        def release_during_send(_token, _chat, _text):
            with connection:
                scoped_blocking.clear_plan_block(connection, repo, "G-018")
                self.assertEqual(notifications.record_work_start(
                    connection, repo, "Worker", plan_id="G-018",
                ), 0)

        notifications.dispatch_pending(
            connection, "token", "chat", adapter=release_during_send,
        )
        resumed = [row for row in self.events(connection)
                   if row["event_type"] == "resumed"]
        self.assertEqual(len(resumed), 0)

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_updated_notice_during_one_block_has_one_resumption_incident(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'first cause')",
                (repo,),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        with connection:
            connection.execute(
                "UPDATE execplan_blocks SET reason_code = 'guidance_approval_required', "
                "evidence = 'updated cause' WHERE repo = ? AND plan_id = 'G-018'",
                (repo,),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 1)
        incidents = connection.execute(
            "SELECT * FROM notified_block_incidents WHERE repo = ?", (repo,),
        ).fetchall()
        self.assertEqual(len(incidents), 1)
        resumed = connection.execute('SELECT * FROM notified_block_incidents ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(json.loads(resumed["payload"])["cause"], "first cause")

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_inflight_earlier_notice_keeps_first_confirmed_cause_after_update(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'first cause')",
                (repo,),
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)

        def update_during_send(_token, _chat, _text):
            with connection:
                connection.execute(
                    "UPDATE execplan_blocks SET reason_code = 'guidance_approval_required', "
                    "evidence = 'later cause' WHERE repo = ? AND plan_id = 'G-018'",
                    (repo,),
                )
                self.confirm_worker_question(connection)
                notifications.enqueue_current_project_events(connection, repo)

        notifications.dispatch_pending(
            connection, "token", "chat", adapter=update_during_send,
        )
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 1)
        payload = connection.execute(
            "SELECT payload FROM notified_block_incidents ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(json.loads(payload[0])["cause"], "first cause")

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)


class IndependentPlanResumptionTest(NotificationFixture, unittest.TestCase):
    def test_project_resume_keeps_started_plan_and_does_not_repeat_worker_start(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
                               "VALUES (?, 'project', 'repository_state_unsafe', 'unsafe')", (repo,))
            self.legacy_project_notice(connection)
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock())
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
            notifications.record_process_start(connection, repo, 'worker', 'resume', plan_id='G-018', resumed=True)
            notifications.record_process_start(connection, repo, 'worker', 'correction', plan_id='G-018')
        resumed = connection.execute('SELECT * FROM notified_block_incidents ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(json.loads(resumed['payload'])['started_execplan_id'], 'G-018')
        self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(connection)))

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_unsent_planner_start_keeps_new_episode_without_generic_notice(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            self.legacy_planning_notice(connection, 'same cause')
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            notifications.record_process_start(connection, repo, 'planner', 'planner-start')
            notifications.record_process_start(connection, repo, 'planner', 'planner-start')
        loop_manager.update_project(connection, repo, planning_error='same cause')
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents WHERE subject = 'planning'"
        ).fetchone()[0], 1)
        self.assertFalse(any(row['event_type'] in {'work_started', 'resumed'} for row in self.events(connection)))

    def test_unsent_released_block_does_not_suppress_physical_starts(self):
        for failed_send in (False, True):
            with self.subTest(failed_send=failed_send):
                connection = self.connection()
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                                       "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
                    self.confirm_worker_question(connection)
                    notifications.enqueue_current_project_events(connection, repo)
                if failed_send:
                    notifications.dispatch_pending(connection, 'token', 'chat',
                                                   adapter=mock.Mock(side_effect=OSError('offline')))
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                    for identity in ('first-start', 'first-start', 'second-start'):
                        notifications.record_process_start(connection, repo, 'worker', identity, plan_id='G-018')
                starts = [row for row in self.events(connection) if row['event_type'] == 'work_started']
                self.assertEqual(len(starts), 0)
                self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))
                incident = connection.execute('SELECT started_operation FROM notified_block_incidents').fetchone()
                self.assertEqual(incident[0], 'Worker')

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_failed_inflight_block_send_materializes_start_without_resume(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                               "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        def failed_send(_token, _chat, _text):
            with connection:
                scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                notifications.record_process_start(connection, repo, 'worker', 'first-start', plan_id='G-018')
            self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(connection)))
            raise OSError('offline')
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=failed_send)
        starts = [row for row in self.events(connection) if row['event_type'] == 'work_started']
        self.assertEqual(len(starts), 0)
        incident = connection.execute('SELECT started_at FROM notified_block_incidents').fetchone()
        self.assertIsNotNone(incident[0])
        with connection:
            notifications.record_process_start(connection, repo, 'worker', 'first-start', plan_id='G-018')
            notifications.record_process_start(connection, repo, 'worker', 'second-start', plan_id='G-018')
        self.assertEqual(len([row for row in self.events(connection) if row['event_type'] == 'work_started']), 0)
        self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_matching_worker_and_pr_resume_preserve_planning_incident_and_safety_gates(self):
        for operation in ('Worker', 'PR handling'):
            with self.subTest(operation=operation):
                connection = self.connection()
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                                       "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'original cause')", (repo,))
                    self.legacy_planning_notice(connection, 'independent planning error')
                    self.confirm_worker_question(connection)
                    notifications.enqueue_current_project_events(connection, repo)
                notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock())
                planning = dict(connection.execute("SELECT * FROM notified_block_incidents WHERE subject = 'planning'").fetchone())
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                    self.assertEqual(notifications.record_work_start(connection, repo, operation, plan_id='G-019'), 0)
                    connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
                                       "VALUES (?, 'project', 'repository_state_unsafe', 'unsafe')", (repo,))
                    self.assertEqual(notifications.record_work_start(connection, repo, operation, plan_id='G-018'), 0)
                    connection.execute('DELETE FROM project_blocks WHERE repo = ?', (repo,))
                    notifications.record_process_start(connection, repo, 'worker', 'actual-process', plan_id='G-018') if operation == 'Worker' else notifications.record_work_start(connection, repo, operation, plan_id='G-018')
                    self.assertEqual(notifications.record_work_start(connection, repo, operation, plan_id='G-018'), 0)
                resumes = [row for row in self.events(connection) if row['event_type'] == 'resumed']
                self.assertEqual(len(resumes), 0)
                started = connection.execute("SELECT payload, started_operation FROM notified_block_incidents WHERE subject='plan:G-018'").fetchone()
                payload = json.loads(started[0])
                self.assertEqual((payload['scope'], payload['execplan_id'], payload['cause'], started[1]),
                                 ('plan', 'G-018', 'original cause', operation))
                self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(connection)))
                self.assertEqual(dict(connection.execute("SELECT * FROM notified_block_incidents WHERE subject = 'planning'").fetchone()), planning)
                self.assertEqual(connection.execute('SELECT planning_error FROM projects').fetchone()[0], 'independent planning error')

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_partial_notice_confirmation_after_start_and_restart_keeps_start_time(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        database = Path(directory) / 'state.db'
        connection = self.connection(database=database)
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                               "VALUES (?, 'G-018', 'plan', 'user_decision_required', ?)", (repo, 'x' * 5000))
            connection.execute("UPDATE projects SET planning_error = 'preserved error' WHERE repo = ?", (repo,))
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        def start_during_send(_token, _chat, _text):
            if "BLOCKED" not in _text or "작업: plan: G-018" not in _text:
                return
            with connection:
                scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                notifications.record_process_start(connection, repo, 'worker', 'process-1', plan_id='G-018')
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=start_during_send)
        resume = connection.execute("SELECT payload FROM notified_block_incidents WHERE subject = 'plan:G-018'").fetchone()[0]
        incident = connection.execute("SELECT started_at FROM notified_block_incidents WHERE subject = 'plan:G-018'").fetchone()[0]
        self.assertIsNotNone(incident)
        connection.close()
        restarted = sqlite3.connect(database)
        self.addCleanup(restarted.close)
        restarted.row_factory = sqlite3.Row
        notifications.configure_session(restarted, enabled=True)
        with restarted:
            notifications.record_process_start(restarted, repo, 'worker', 'process-1', plan_id='G-018')
        self.assertEqual(len([row for row in self.events(restarted) if row['event_type'] == 'resumed']), 0)
        self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(restarted)))
        self.assertEqual(restarted.execute("SELECT started_at FROM notified_block_incidents WHERE subject = 'plan:G-018'").fetchone()[0], incident)
        self.assertEqual(restarted.execute("SELECT payload FROM notified_block_incidents WHERE subject = 'plan:G-018'").fetchone()[0], resume)

        self.assertEqual(restarted.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)


class UnsentBlockStartTest(NotificationFixture, unittest.TestCase):
    def test_failed_in_flight_block_send_does_not_notify_automatic_worker_resume(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        def resume_then_fail(*_args):
            with connection:
                scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                notifications.record_process_start(connection, repo, 'worker', 'resume', plan_id='G-018', resumed=True)
            raise OSError('offline')
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=resume_then_fail)
        with connection:
            notifications.recover_interrupted_starts(connection)
            notifications.record_process_start(connection, repo, 'worker', 'resume', plan_id='G-018', resumed=True)
        self.assertFalse(any(row['event_type'] in {'work_started', 'resumed'} for row in self.events(connection)))
        incident = connection.execute('SELECT payload FROM notified_block_incidents').fetchone()
        self.assertTrue(json.loads(incident[0])['started_as_resume'])

    def test_unsent_and_failed_block_do_not_suppress_later_worker_starts(self):
        for attempted in (False, True):
            with self.subTest(attempted=attempted):
                connection = self.connection()
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
                    self.confirm_worker_question(connection)
                    notifications.enqueue_current_project_events(connection, repo)
                if attempted:
                    notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock(side_effect=OSError('offline')))
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'first', plan_id='G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'first', plan_id='G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'second', plan_id='G-018')
                starts = [row for row in self.events(connection) if row['event_type'] == 'work_started']
                self.assertEqual(len(starts), 0)
                self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)

    def test_in_flight_failure_falls_back_to_saved_start_without_duplicate(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        def start_then_fail(*_args):
            with connection:
                scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                notifications.record_process_start(connection, repo, 'worker', 'first', plan_id='G-018')
            self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(connection)))
            raise OSError('offline')
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=start_then_fail)
        with connection:
            notifications.record_process_start(connection, repo, 'worker', 'first', plan_id='G-018')
        starts = [row for row in self.events(connection) if row['event_type'] == 'work_started']
        self.assertEqual(len(starts), 0)
        incident = connection.execute('SELECT started_at FROM notified_block_incidents').fetchone()
        self.assertIsNotNone(incident[0])
        self.assertEqual(connection.execute('SELECT start_identity FROM notified_block_incidents').fetchone()[0], 'first')
        self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)


class LegacyPlanningPolicyTest(NotificationFixture, unittest.TestCase):
    def test_saved_notices_keep_audit_through_restart_and_changed_or_cleared_cause(self):
        for cursor, sent in ((0, False), (1, False), (1, True), (0, True)):
            for cause in ('old planning error', 'new planning error', None):
                with self.subTest(cursor=cursor, sent=sent, cause=cause), tempfile.TemporaryDirectory() as directory:
                    database = Path(directory) / 'state.db'
                    connection = self.connection(database=database)
                    try:
                        with connection:
                            self.legacy_planning_notice(connection, confirmed=cursor, sent=sent)
                            connection.execute("UPDATE notification_outbox SET rendered_text=?, attempts=2, last_error='saved error', delivery_started_at=CURRENT_TIMESTAMP", ('frozen text ' * 500,))
                        before = dict(self.events(connection)[0])
                        loop_manager.update_project(connection, '/projects/alpha', planning_error=cause)
                        with connection:
                            notifications.record_process_start(connection, '/projects/alpha', 'planner', 'physical-start')
                            notifications.recover_interrupted_starts(connection)
                    finally:
                        connection.close()
                    restarted = sqlite3.connect(database)
                    restarted.row_factory = sqlite3.Row
                    try:
                        notifications.configure_session(restarted, enabled=True)
                        for _ in range(2):
                            with restarted:
                                notifications.enqueue_current_project_events(restarted, '/projects/alpha')
                                notifications.recover_interrupted_starts(restarted)
                        adapter = mock.Mock()
                        self.assertEqual(notifications.dispatch_pending(restarted, 'token', 'chat', adapter=adapter), 0)
                        adapter.assert_not_called()
                        after = dict(self.events(restarted)[0])
                        self.assertEqual(len(self.events(restarted)), 1)
                        for field in before.keys() - {'canceled_at', 'delivery_started_at'}:
                            self.assertEqual(after[field], before[field], field)
                        if not sent:
                            self.assertIsNotNone(after['canceled_at'])
                        self.assertEqual(restarted.execute('SELECT planning_error FROM projects').fetchone()[0], cause)
                        incident = restarted.execute('SELECT * FROM notified_block_incidents').fetchone()
                        self.assertEqual(json.loads(incident['payload'])['cause'], 'old planning error')
                        if cause is None:
                            self.assertEqual(incident['start_identity'], 'physical-start')
                            self.assertIsNotNone(incident['started_at'])
                        else:
                            self.assertIsNone(incident['started_at'])
                    finally:
                        restarted.close()

    def test_duplicate_history_keeps_first_confirmation_and_never_reactivates_on_projection(self):
        for released in (False, True):
            connection = self.connection()
            repo = '/projects/alpha'
            with self.subTest(released=released), connection:
                key = self.legacy_planning_notice(connection, confirmed=1)
                first = connection.execute('SELECT * FROM notified_block_incidents').fetchone()
                duplicate = key + ':duplicate'
                connection.execute('INSERT INTO notification_outbox (event_key, repo, event_type, payload) VALUES (?, ?, \'blocked\', ?)',
                                   (duplicate, repo, first['payload']))
                cursor = connection.execute('INSERT INTO notified_block_incidents (repo, subject, block_key, event_key, payload) VALUES (?, \'planning\', ?, ?, ?)',
                                   (repo, first['block_key'], duplicate, first['payload']))
                connection.execute('INSERT INTO block_notice_events VALUES (?, ?)', (duplicate, cursor.lastrowid))
                connection.execute('UPDATE notification_outbox SET canceled_at=CURRENT_TIMESTAMP, rendered_text=?, last_error=\'prior error\', attempts=2', ('frozen ' * 900,))
                if released:
                    connection.execute('UPDATE notified_block_incidents SET released_at=CURRENT_TIMESTAMP')
                before = [dict(row) for row in self.events(connection)]
                prior = [dict(row) for row in connection.execute('SELECT * FROM notified_block_incidents ORDER BY id')]
                for _ in range(2):
                    notifications.enqueue_current_project_events(connection, repo)
                self.assertEqual([dict(row) for row in self.events(connection)], before)
                self.assertEqual([dict(row) for row in connection.execute('SELECT * FROM notified_block_incidents ORDER BY id')], prior)
            loop_manager.update_project(connection, repo, planning_error=None)
            with connection:
                notifications.record_work_start(connection, repo, 'Planner', start_identity='planner')
                notifications.recover_interrupted_starts(connection)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM notified_block_incidents WHERE released_at IS NULL').fetchone()[0], 0)
            self.assertEqual(len(self.events(connection)), 2)
            for old, row in zip(before, self.events(connection)):
                for field in old.keys() - {'delivery_started_at'}:
                    self.assertEqual(row[field], old[field], field)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed','work_started')").fetchone()[0], 0)
            confirmed = connection.execute('SELECT MAX(confirmed_before) FROM notified_block_incidents').fetchone()[0]
            self.assertEqual(confirmed, 1)
