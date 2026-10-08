import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.config.managed_config import ManagedConfigError, load_managed_config
from loop_manager_internal.notifications import notifications
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import RecoveryRepository

from tests.support.notification import NotificationFixture


class IncidentPayloadEncodingTest(NotificationFixture, unittest.TestCase):
    def test_incident_and_outbox_share_unicode_payload_encoding(self):
        connection = self.connection()
        self.addCleanup(connection.close)
        loop_manager.update_project(connection, "/projects/alpha", planning_error="원인 \U0001f642")
        incident = connection.execute(
            "SELECT payload FROM notified_block_incidents WHERE subject = 'planning'"
        ).fetchone()[0]
        outbox = connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0]
        self.assertEqual(incident, outbox)
        self.assertEqual(json.loads(outbox)["cause"], "원인 \U0001f642")
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
        notices = self.events(self.db)
        self.assertEqual(len(notices), 1)
        decoded = json.loads(notices[0]['payload'])
        text = notifications._message('blocked', decoded)
        self.assertIn(intent['ref'], text)
        self.assertIn('Original branch: loop-manager/G-018', text)
        self.assertIn('a' * 40, text)
        self.assertEqual(decoded['cause'], 'original cause')
        self.assertEqual(notifications._current_block_key(self.db, self.repo, decoded),
                         self.db.execute('SELECT block_key FROM notified_block_incidents').fetchone()[0])

    def test_local_only_notice_reports_actual_reason_and_ignores_stale_cause_result(self):
        self.repository.result(self.repo, 'G-018', self.cause,
                               dict(result='local_only', reason='unresolved rebase conflict',
                                    original_branch=self.branch, worktree=self.worktree))
        decoded = json.loads(self.events(self.db)[0]['payload'])
        self.assertIn('unresolved rebase conflict', notifications._message('blocked', decoded))
        self.assertIn('/state/G-018', notifications._message('blocked', decoded))
        self.db.execute("UPDATE project_blocks SET evidence = 'new independent cause'")
        self.assertIsNone(notifications._project_checkpoint(self.db, self.repo, 'G-018'))


class NotificationOutboxTest(NotificationFixture, unittest.TestCase):
    def test_inflight_planning_piece_confirmed_after_start_still_resumes(self):
        connection = self.connection()
        repo = "/projects/alpha"
        loop_manager.update_project(connection, repo, planning_error="x" * 9000)
        incident_id = connection.execute(
            "SELECT id FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()[0]

        def release_during_first_piece(_token, _chat, _text):
            loop_manager.update_project(connection, repo, planning_error=None)
            with connection:
                self.assertEqual(notifications.record_work_start(
                    connection, repo, "Planner",
                ), 0)
            incident = connection.execute(
                "SELECT started_operation, resumed_at "
                "FROM notified_block_incidents WHERE id = ?", (incident_id,),
            ).fetchone()
            self.assertEqual(incident["started_operation"], "Planner")
            self.assertIsNone(incident["resumed_at"])

        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=release_during_first_piece,
        ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE repo = ? AND event_type = 'resumed'", (repo,),
        ).fetchone()[0], 1)
        self.assertIsNotNone(connection.execute(
            "SELECT resumed_at FROM notified_block_incidents WHERE id = ?",
            (incident_id,),
        ).fetchone()[0])

    def test_released_partial_planning_notice_retries_original_remaining_piece(self):
        connection = self.connection()
        repo = "/projects/alpha"
        loop_manager.update_project(connection, repo, planning_error="owner mismatch")
        first = connection.execute(
            "SELECT block_key, event_key, payload FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()
        latest_key = first["event_key"] + ":unsent"
        with connection:
            connection.execute(
                "UPDATE notified_block_incidents SET released_at = CURRENT_TIMESTAMP "
                "WHERE event_key = ?", (first["event_key"],),
            )
            connection.execute(
                "UPDATE notification_outbox SET rendered_text = ?, "
                "confirmed_pieces = 1, canceled_at = CURRENT_TIMESTAMP "
                "WHERE event_key = ?", ("x" * 5000, first["event_key"]),
            )
            connection.execute(
                "INSERT INTO notified_block_incidents "
                "(repo, subject, block_key, event_key, payload, released_at) "
                "VALUES (?, 'planning', ?, ?, ?, CURRENT_TIMESTAMP)",
                (repo, first["block_key"], latest_key, first["payload"]),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, canceled_at) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP)",
                (latest_key, repo, first["payload"]),
            )

        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        original = connection.execute(
            "SELECT canceled_at, confirmed_pieces FROM notification_outbox "
            "WHERE event_key = ?", (first["event_key"],),
        ).fetchone()
        self.assertIsNone(original["canceled_at"])
        self.assertEqual(original["confirmed_pieces"], 1)
        self.assertIsNotNone(connection.execute(
            "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
            (latest_key,),
        ).fetchone()[0])
        delivered = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _t, _c, piece: delivered.append(piece),
        ), 1)
        self.assertEqual(delivered, ["x" * 904])

        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)

    def test_same_second_planner_start_closes_unsent_planning_episode(self):
        connection = self.connection()
        repo = "/projects/alpha"
        loop_manager.update_project(connection, repo, planning_error="same cause")
        first_id = connection.execute(
            "SELECT id FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()[0]
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            notifications.record_process_start(
                connection, repo, "planner", "same-second-planner",
            )
        started = connection.execute(
            "SELECT started_operation, resumed_at FROM notified_block_incidents "
            "WHERE id = ?", (first_id,),
        ).fetchone()
        self.assertEqual(started["started_operation"], "Planner")
        self.assertIsNone(started["resumed_at"])
        self.assertEqual(len([row for row in self.events(connection)
                              if row["event_type"] == "work_started"]), 0)
        notice_created_at = connection.execute(
            "SELECT o.created_at FROM notification_outbox o "
            "JOIN notified_block_incidents i ON i.event_key = o.event_key "
            "WHERE i.id = ?", (first_id,),
        ).fetchone()[0]
        with connection:
            connection.execute(
                "INSERT INTO agent_invocations "
                "(invocation_id, repo, role, provider, model, effort, "
                "transition, attempt, started_at) "
                "VALUES ('same-second-planner', ?, 'planner', 'codex', "
                "'model', 'high', 'start', 1, ?)",
                (repo, notice_created_at),
            )

        loop_manager.update_project(connection, repo, planning_error="same cause")
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()[0], 2)
        self.assertIsNotNone(connection.execute(
            "SELECT released_at FROM notified_block_incidents WHERE id = ?",
            (first_id,),
        ).fetchone()[0])

    def test_released_planning_duplicates_keep_delivered_notice_not_unsent_latest(self):
        connection = self.connection()
        repo = "/projects/alpha"
        loop_manager.update_project(connection, repo, planning_error="owner mismatch")
        first = connection.execute(
            "SELECT block_key, event_key, payload FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()
        latest_key = first["event_key"] + ":unsent"
        with connection:
            connection.execute(
                "UPDATE notification_outbox SET sent_at = CURRENT_TIMESTAMP, "
                "confirmed_pieces = 1 WHERE event_key = ?",
                (first["event_key"],),
            )
            connection.execute(
                "UPDATE notified_block_incidents SET released_at = CURRENT_TIMESTAMP "
                "WHERE event_key = ?", (first["event_key"],),
            )
            connection.execute(
                "INSERT INTO notified_block_incidents "
                "(repo, subject, block_key, event_key, payload, released_at) "
                "VALUES (?, 'planning', ?, ?, ?, CURRENT_TIMESTAMP)",
                (repo, first["block_key"], latest_key, first["payload"]),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, canceled_at) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP)",
                (latest_key, repo, first["payload"]),
            )

        for _ in range(3):
            with connection:
                notifications.enqueue_current_project_events(connection, repo)
            self.assertIsNotNone(connection.execute(
                "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
                (latest_key,),
            ).fetchone()[0])
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()[0], 2)

        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE repo = ? AND event_type = 'resumed'", (repo,),
        ).fetchone()[0], 1)

    def test_release_of_duplicate_active_planning_incidents_resumes_once(self):
        connection = self.connection()
        repo = "/projects/alpha"
        loop_manager.update_project(connection, repo, planning_error="owner mismatch")
        first = connection.execute(
            "SELECT block_key, event_key, payload FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()
        with connection:
            connection.execute(
                "UPDATE notification_outbox SET confirmed_pieces = 1 "
                "WHERE event_key = ?", (first["event_key"],),
            )
            connection.execute(
                "INSERT INTO notified_block_incidents "
                "(repo, subject, block_key, event_key, payload) "
                "VALUES (?, 'planning', ?, ?, ?)",
                (repo, first["block_key"], first["event_key"] + ":duplicate",
                 first["payload"]),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload) VALUES (?, ?, 'blocked', ?)",
                (first["event_key"] + ":duplicate", repo, first["payload"]),
            )
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE repo = ? AND event_type = 'resumed'", (repo,),
        ).fetchone()[0], 1)

    def test_planning_incident_survives_database_reopen_and_repeated_observation(self):
        repo = "/projects/alpha"
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            cleanup.callback(connection.close)
            with connection:
                connection.execute(
                    "UPDATE projects SET planning_error = 'owner mismatch' "
                    "WHERE repo = ?", (repo,),
                )
                notifications.enqueue_current_project_events(connection, repo)
            incident_id = connection.execute(
                "SELECT id FROM notified_block_incidents "
                "WHERE repo = ? AND subject = 'planning'", (repo,),
            ).fetchone()[0]
            connection.close()

            connection = sqlite3.connect(database)
            cleanup.callback(connection.close)
            connection.row_factory = sqlite3.Row
            for _ in range(3):
                with connection:
                    notifications.enqueue_current_project_events(connection, repo)
            self.assertEqual(connection.execute(
                "SELECT id FROM notified_block_incidents "
                "WHERE repo = ? AND subject = 'planning'", (repo,),
            ).fetchall()[0][0], incident_id)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE repo = ? AND event_type = 'blocked'", (repo,),
            ).fetchone()[0], 1)
            connection.close()

    def test_historical_planning_duplicates_keep_one_incident_until_real_restart(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "UPDATE projects SET planning_error = 'owner mismatch' WHERE repo = ?",
                (repo,),
            )
            notifications.enqueue_current_project_events(connection, repo)
            first = connection.execute(
                "SELECT id, block_key, event_key, payload FROM "
                "notified_block_incidents WHERE subject = 'planning'",
            ).fetchone()
            connection.execute(
                "UPDATE notification_outbox SET sent_at = CURRENT_TIMESTAMP, "
                "confirmed_pieces = 1 "
                "WHERE event_key = ?", (first["event_key"],),
            )
            duplicate_key = first["event_key"] + ":historical"
            connection.execute(
                "INSERT INTO notified_block_incidents "
                "(repo, subject, block_key, event_key, payload, released_at) "
                "VALUES (?, 'planning', ?, ?, ?, CURRENT_TIMESTAMP)",
                (repo, first["block_key"], duplicate_key, first["payload"]),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, sent_at, confirmed_pieces) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP, 1)",
                (duplicate_key, repo, first["payload"]),
            )
            # Simulate the old false release of the latest incident.
            connection.execute(
                "UPDATE notified_block_incidents SET released_at = CURRENT_TIMESTAMP "
                "WHERE id = ?", (first["id"],),
            )

        for _ in range(3):
            with connection:
                notifications.enqueue_current_project_events(connection, repo)
                self.assertEqual(notifications.record_work_start(
                    connection, repo, "Worker", plan_id="G-018",
                ), 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM notified_block_incidents "
                "WHERE repo = ? AND subject = 'planning'", (repo,),
            ).fetchone()[0], 2)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning' AND released_at IS NULL",
            (repo,),
        ).fetchone()[0], 1)

        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 0)
        loop_manager.update_project(connection, repo, planning_error="owner mismatch")
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents "
            "WHERE repo = ? AND subject = 'planning'", (repo,),
        ).fetchone()[0], 3)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE repo = ? AND event_type = 'resumed'", (repo,),
        ).fetchone()[0], 1)

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
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()
        self.assertEqual(json.loads(resumed[0])["cause"], "old notified cause")

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

    def test_prior_planning_notice_does_not_notify_new_error_with_reused_key(self):
        connection = self.connection()
        repo = "/projects/alpha"
        old_key = notifications._key("blocked", repo, "project\0planning_error\0")
        old_payload = {
            "project": "alpha", "stage": "planning", "scope": "project",
            "reason_code": "planning_error", "execplan_id": None,
            "cause": "previous resolved error", "paths": [],
        }
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "UPDATE projects SET planning_error = 'new undelivered error' "
                "WHERE repo = ?", (repo,),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, sent_at) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP)",
                (old_key, repo, json.dumps(old_payload)),
            )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0], 2)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents WHERE repo = ?",
            (repo,),
        ).fetchone()[0], 1)
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()[0], 0)

    def test_unsent_legacy_planning_notice_is_canceled_before_new_error_dispatch(self):
        connection = self.connection()
        repo = "/projects/alpha"
        old_key = notifications._key("blocked", repo, "project\0planning_error\0")
        old_payload = {
            "project": "alpha", "stage": "planning", "scope": "project",
            "reason_code": "planning_error", "execplan_id": None,
            "cause": "resolved error", "paths": [],
        }
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "UPDATE projects SET planning_error = 'resolved error' WHERE repo = ?",
                (repo,),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload) VALUES (?, ?, 'blocked', ?)",
                (old_key, repo, json.dumps(old_payload)),
            )
            # The earlier error clears without dispatch before another error.
            connection.execute(
                "UPDATE projects SET planning_error = NULL WHERE repo = ?", (repo,),
            )
            connection.execute(
                "UPDATE projects SET planning_error = 'new error' WHERE repo = ?",
                (repo,),
            )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        delivered = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, message: delivered.append(message),
        ), 1)
        self.assertEqual(len(delivered), 1)
        self.assertIn("new error", delivered[0])
        self.assertNotIn("resolved error", delivered[0])
        old_notice = connection.execute(
            "SELECT canceled_at, sent_at FROM notification_outbox WHERE event_key = ?",
            (old_key,),
        ).fetchone()
        self.assertIsNotNone(old_notice[0])
        self.assertIsNone(old_notice[1])
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        resumed = connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'",
        ).fetchone()
        self.assertEqual(json.loads(resumed[0])["cause"], "new error")

    def test_identical_planning_error_after_later_planner_start_is_new_incident(self):
        connection = self.connection()
        repo = "/projects/alpha"
        key = notifications._key("blocked", repo, "project\0planning_error\0")
        payload = {
            "project": "alpha", "stage": "planning", "scope": "project",
            "reason_code": "planning_error", "execplan_id": None,
            "cause": "repeated error", "paths": [],
        }
        with connection:
            connection.execute("DROP TABLE block_notice_events")
            connection.execute("DROP TABLE notified_block_incidents")
            connection.execute(
                "UPDATE projects SET planning_error = 'repeated error' WHERE repo = ?",
                (repo,),
            )
            connection.execute(
                "INSERT INTO notification_outbox "
                "(event_key, repo, event_type, payload, sent_at, created_at) "
                "VALUES (?, ?, 'blocked', ?, CURRENT_TIMESTAMP, '2000-01-01 00:00:00')",
                (key, repo, json.dumps(payload)),
            )
            connection.execute(
                "INSERT INTO agent_invocations "
                "(invocation_id, repo, role, provider, model, effort, transition, "
                "attempt, started_at) VALUES "
                "('later-planner', ?, 'planner', 'codex', 'model', 'high', "
                "'start', 1, '2000-01-02T00:00:00+00:00')",
                (repo,),
            )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()[0], 2)
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 0)

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
        ), 1)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()[0], 1)

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
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()
        self.assertEqual(json.loads(resumed[0])["cause"], "first notified cause")

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
        self.assertEqual(len(resumed), 1)
        self.assertEqual(json.loads(resumed[0]["payload"])["cause"], "Decision pending")
        block()
        second_key = next(row["event_key"] for row in self.events(connection)
                          if row["event_type"] == "blocked"
                          and row["event_key"] != first_key)
        self.assertNotEqual(first_key, second_key)

    def test_partial_block_release_cancels_stale_piece_and_retries_resumption(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', ?)",
                (repo, "x" * 9000),
            )
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
        ), 1)
        self.assertEqual(len(delivered), 3)
        self.assertIn("작업 재개", delivered[0])

    def test_project_and_planning_release_wait_for_matching_work(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
                "VALUES (?, 'project', 'repository_state_unsafe', 'unsafe worktree')",
                (repo,),
            )
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        loop_manager.update_project(connection, repo, planning_error="invalid plan")
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 1)
        payloads = [json.loads(row["payload"]) for row in self.events(connection)
                    if row["event_type"] == "resumed"]
        self.assertEqual([item["reason_code"] for item in payloads],
                         ["repository_state_unsafe", "planning_error"])

    def test_disabled_telegram_never_creates_resumption(self):
        connection = self.connection(enabled=False)
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'decision')",
                (repo,),
            )
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
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
        notifications.configure_session(connection, enabled=False)
        with connection:
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Worker", plan_id="G-018",
            ), 0)
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
            notifications.enqueue_current_project_events(restarted, repo)
            scoped_blocking.clear_plan_block(restarted, repo, "G-018")
            self.assertEqual(notifications.record_work_start(
                restarted, repo, "Worker", plan_id="G-018",
            ), 0)
        self.assertEqual(len([row for row in self.events(restarted)
                              if row["event_type"] == "resumed"]), 1)

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
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()
        self.assertIsNotNone(payload)
        self.assertEqual(json.loads(payload[0])["operation"], "PR handling")

    def test_inflight_block_piece_confirmed_after_release_still_resumes(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'decision')",
                (repo,),
            )
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
        self.assertEqual(len(resumed), 1)

    def test_updated_notice_during_one_block_has_one_resumption_incident(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'first cause')",
                (repo,),
            )
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        with connection:
            connection.execute(
                "UPDATE execplan_blocks SET reason_code = 'guidance_approval_required', "
                "evidence = 'updated cause' WHERE repo = ? AND plan_id = 'G-018'",
                (repo,),
            )
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
        resumed = next(row for row in self.events(connection)
                       if row["event_type"] == "resumed")
        self.assertEqual(json.loads(resumed["payload"])["cause"], "first cause")

    def test_inflight_earlier_notice_keeps_first_confirmed_cause_after_update(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'first cause')",
                (repo,),
            )
            notifications.enqueue_current_project_events(connection, repo)

        def update_during_send(_token, _chat, _text):
            with connection:
                connection.execute(
                    "UPDATE execplan_blocks SET reason_code = 'guidance_approval_required', "
                    "evidence = 'later cause' WHERE repo = ? AND plan_id = 'G-018'",
                    (repo,),
                )
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
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone()
        self.assertEqual(json.loads(payload[0])["cause"], "first cause")


class IndependentPlanResumptionTest(NotificationFixture, unittest.TestCase):
    def test_project_resume_keeps_started_plan_and_does_not_repeat_worker_start(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
                               "VALUES (?, 'project', 'repository_state_unsafe', 'unsafe')", (repo,))
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock())
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
            notifications.record_process_start(connection, repo, 'worker', 'resume', plan_id='G-018', resumed=True)
            notifications.record_process_start(connection, repo, 'worker', 'correction', plan_id='G-018')
        resumed = next(row for row in self.events(connection) if row['event_type'] == 'resumed')
        self.assertEqual(json.loads(resumed['payload'])['execplan_id'], 'G-018')
        self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(connection)))

    def test_unsent_planner_start_keeps_new_episode_without_generic_notice(self):
        connection = self.connection()
        repo = '/projects/alpha'
        loop_manager.update_project(connection, repo, planning_error='same cause')
        loop_manager.update_project(connection, repo, planning_error=None)
        with connection:
            notifications.record_process_start(connection, repo, 'planner', 'planner-start')
            notifications.record_process_start(connection, repo, 'planner', 'planner-start')
        loop_manager.update_project(connection, repo, planning_error='same cause')
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notified_block_incidents WHERE subject = 'planning'"
        ).fetchone()[0], 2)
        self.assertFalse(any(row['event_type'] in {'work_started', 'resumed'} for row in self.events(connection)))

    def test_unsent_released_block_does_not_suppress_physical_starts(self):
        for failed_send in (False, True):
            with self.subTest(failed_send=failed_send):
                connection = self.connection()
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                                       "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
                    notifications.enqueue_current_project_events(connection, repo)
                if failed_send:
                    notifications.dispatch_pending(connection, 'token', 'chat',
                                                   adapter=mock.Mock(side_effect=OSError('offline')))
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                    for identity in ('first-start', 'first-start', 'second-start'):
                        notifications.record_process_start(connection, repo, 'worker', identity, plan_id='G-018')
                starts = [row for row in self.events(connection) if row['event_type'] == 'work_started']
                self.assertEqual(len(starts), 1)
                self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))
                incident = connection.execute('SELECT started_operation FROM notified_block_incidents').fetchone()
                self.assertEqual(incident[0], 'Worker')

    def test_failed_inflight_block_send_materializes_start_without_resume(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                               "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
            notifications.enqueue_current_project_events(connection, repo)
        def failed_send(_token, _chat, _text):
            with connection:
                scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                notifications.record_process_start(connection, repo, 'worker', 'first-start', plan_id='G-018')
            self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(connection)))
            raise OSError('offline')
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=failed_send)
        starts = [row for row in self.events(connection) if row['event_type'] == 'work_started']
        self.assertEqual(len(starts), 1)
        incident = connection.execute('SELECT started_at FROM notified_block_incidents').fetchone()
        self.assertEqual(json.loads(starts[0]['payload'])['occurred_at'], incident[0])
        with connection:
            notifications.record_process_start(connection, repo, 'worker', 'first-start', plan_id='G-018')
            notifications.record_process_start(connection, repo, 'worker', 'second-start', plan_id='G-018')
        self.assertEqual(len([row for row in self.events(connection) if row['event_type'] == 'work_started']), 1)
        self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))

    def test_matching_worker_and_pr_resume_preserve_planning_incident_and_safety_gates(self):
        for operation in ('Worker', 'PR handling'):
            with self.subTest(operation=operation):
                connection = self.connection()
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                                       "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'original cause')", (repo,))
                    connection.execute("UPDATE projects SET planning_error = 'independent planning error' WHERE repo = ?", (repo,))
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
                self.assertEqual(len(resumes), 1)
                payload = json.loads(resumes[0]['payload'])
                self.assertEqual((payload['scope'], payload['execplan_id'], payload['cause'], payload['operation']),
                                 ('plan', 'G-018', 'original cause', operation))
                self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(connection)))
                self.assertEqual(dict(connection.execute("SELECT * FROM notified_block_incidents WHERE subject = 'planning'").fetchone()), planning)
                self.assertEqual(connection.execute('SELECT planning_error FROM projects').fetchone()[0], 'independent planning error')

    def test_partial_notice_confirmation_after_start_and_restart_keeps_start_time(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        database = Path(directory) / 'state.db'
        connection = self.connection(database=database)
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                               "VALUES (?, 'G-018', 'plan', 'user_decision_required', ?)", (repo, 'x' * 5000))
            connection.execute("UPDATE projects SET planning_error = 'preserved error' WHERE repo = ?", (repo,))
            notifications.enqueue_current_project_events(connection, repo)
        def start_during_send(_token, _chat, _text):
            if "BLOCKED" not in _text or "작업: plan: G-018" not in _text:
                return
            with connection:
                scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                notifications.record_process_start(connection, repo, 'worker', 'process-1', plan_id='G-018')
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=start_during_send)
        resume = connection.execute("SELECT payload FROM notification_outbox WHERE event_type = 'resumed'").fetchone()[0]
        incident = connection.execute("SELECT started_at FROM notified_block_incidents WHERE subject = 'plan:G-018'").fetchone()[0]
        self.assertEqual(json.loads(resume)['occurred_at'], incident)
        connection.close()
        restarted = sqlite3.connect(database)
        self.addCleanup(restarted.close)
        restarted.row_factory = sqlite3.Row
        notifications.configure_session(restarted, enabled=True)
        with restarted:
            notifications.record_process_start(restarted, repo, 'worker', 'process-1', plan_id='G-018')
        self.assertEqual(len([row for row in self.events(restarted) if row['event_type'] == 'resumed']), 1)
        self.assertFalse(any(row['event_type'] == 'work_started' for row in self.events(restarted)))
        self.assertEqual(restarted.execute("SELECT payload FROM notification_outbox WHERE event_type = 'resumed'").fetchone()[0], resume)


class UnsentBlockStartTest(NotificationFixture, unittest.TestCase):
    def test_unsent_and_failed_block_do_not_suppress_later_worker_starts(self):
        for attempted in (False, True):
            with self.subTest(attempted=attempted):
                connection = self.connection()
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
                    notifications.enqueue_current_project_events(connection, repo)
                if attempted:
                    notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock(side_effect=OSError('offline')))
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'first', plan_id='G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'first', plan_id='G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'second', plan_id='G-018')
                starts = [row for row in self.events(connection) if row['event_type'] == 'work_started']
                self.assertEqual(len(starts), 1)
                self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))

    def test_in_flight_failure_falls_back_to_saved_start_without_duplicate(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
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
        self.assertEqual(len(starts), 1)
        payload = json.loads(starts[0]['payload'])
        incident = connection.execute('SELECT started_at FROM notified_block_incidents').fetchone()
        self.assertEqual(payload['occurred_at'], incident[0])
        self.assertTrue(payload['silent'])
        self.assertFalse(any(row['event_type'] == 'resumed' for row in self.events(connection)))
