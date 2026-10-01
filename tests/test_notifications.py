import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
import blocked_checkpoint_state
from managed_config import ManagedConfigError, load_managed_config
import notifications
import work_exhaustion
import project_registry
import scoped_blocking
from recovery_runtime import RecoveryObservation, RecoveryReason
from recovery_state import RecoveryRepository


class NotificationFixture:
    def connection(
        self,
        *,
        enabled: bool = True,
        database: str | Path = ":memory:",
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(database)
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
        return connection

    @staticmethod
    def events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM notification_outbox ORDER BY event_type"
        ).fetchall()


class ProjectCheckpointNotificationUnitTests(NotificationFixture, unittest.TestCase):
    def setUp(self):
        self.db = self.connection()
        self.addCleanup(self.db.close)
        self.repo = '/projects/alpha'
        self.db.execute("UPDATE projects SET origin = 'example/repo', phase = 'PROJECT_BLOCKED'")
        self.db.execute("INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence) "
                        "VALUES (?, 'project', 'publication_ownership_uncertain', 'G-018', 'original cause')", (self.repo,))
        self.db.commit()
        self.repository = blocked_checkpoint_state.DiagnosticPublicationRepository(self.db)
        self.cause = scoped_blocking.project_cause_identity(scoped_blocking.project_block(self.db, self.repo))

    def test_diagnostic_notice_contains_confirmed_ref_oid_and_original_branch_once(self):
        payload = dict(result='published', diagnostic=True, branch='loop-manager/diagnostics/G-018/content',
                       commit_oid='a' * 40, original_branch='loop-manager/G-018', base_oid='b' * 40)
        self.repository.result(self.repo, 'G-018', self.cause, payload)
        self.repository.result(self.repo, 'G-018', self.cause, payload)
        notices = self.events(self.db)
        self.assertEqual(len(notices), 1)
        decoded = json.loads(notices[0]['payload'])
        text = notifications._message('blocked', decoded)
        self.assertIn('loop-manager/diagnostics/G-018/content', text)
        self.assertIn('Original branch: loop-manager/G-018', text)
        self.assertIn('a' * 40, text)
        self.assertEqual(decoded['cause'], 'original cause')
        self.assertEqual(notifications._current_block_key(self.db, self.repo, decoded),
                         self.db.execute('SELECT block_key FROM notified_block_incidents').fetchone()[0])

    def test_local_only_notice_reports_actual_reason_and_ignores_stale_cause_result(self):
        self.repository.result(self.repo, 'G-018', self.cause,
                               dict(result='local_only', reason='unresolved rebase conflict', worktree='/state/G-018'))
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
            self.assertEqual(notifications.record_work_start(
                connection, repo, "Planner",
            ), 0)
        started = connection.execute(
            "SELECT started_operation, resumed_at FROM notified_block_incidents "
            "WHERE id = ?", (first_id,),
        ).fetchone()
        self.assertEqual(started["started_operation"], "Planner")
        self.assertIsNone(started["resumed_at"])
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
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
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
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "notices.db"
            connection = self.connection(database=database)
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

    def test_specification_deadlock_notification_names_documents_and_question(self):
        connection = self.connection()
        repo = "/projects/alpha"
        evidence = {
            "kind": "missing",
            "locations": [{"path": "docs/PRODUCT.md", "section": "Review policy"}],
            "problem": "No retention criterion is specified",
            "implementation_choice": "Whether to retain review history",
            "question": "How long should review history be retained?",
        }
        output = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"
        with connection:
            scoped_blocking.record_plan_block(
                connection, repo, "G-018",
                scoped_blocking.BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
                output, "input-fingerprint",
            )
            checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            checkpoint.finish_unpublished(
                checkpoint.get(repo, "G-018"), "local only", retryable=False,
            )
            notifications.enqueue_current_project_events(connection, repo)
            notifications.enqueue_current_project_events(connection, repo)
        events = self.events(connection)
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["paths"], ["docs/PRODUCT.md"])
        self.assertEqual(payload["specification_deadlock"], evidence)
        message = notifications._message("blocked", payload)
        self.assertIn("docs/PRODUCT.md", message)
        self.assertIn(evidence["question"], message)

    def test_guidance_block_reports_exact_decision_lines_and_recovery_attempts(self):
        connection = self.connection()
        repo = "/projects/alpha"
        path = "docs/plans/[G-018] Reliable notices.md"
        with connection:
            connection.execute(
                "INSERT INTO guidance_approvals (repo, plan_id, proposal_id, "
                "proposal_hash, target_path, scope, proposal_patch, "
                "proposal_patch_hash, proposal_line, decision_line, batch_id, "
                "sequence, baseline_content, baseline_mode) "
                "VALUES (?, 'G-018', 'guidance-a', 'hash', 'AGENTS.md', "
                "'common', 'patch', 'patch-hash', 15, 22, 'batch', 0, ?, 420)",
                (repo, b""),
            )
            RecoveryRepository(connection).observe(RecoveryObservation(
                RecoveryReason.GIT_LOCK, repo, "G-018", "IMPLEMENTING",
                "plan-sha", "base-sha", "lock", "worker:head", "lock busy",
            ))
            scoped_blocking.record_plan_block(
                connection, repo, "G-018",
                scoped_blocking.BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
                "User Decision pending", "guidance-fingerprint",
            )
            checkpoint_repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(repo, "G-018"),
                "local-only", retryable=False,
            )
            notifications.enqueue_current_project_events(connection, repo)
        blocked = next(
            json.loads(row["payload"]) for row in self.events(connection)
            if row["event_type"] == "blocked"
        )
        expected = [f"{path}:15", f"{path}:22"]
        self.assertEqual(blocked["recovery"]["user_targets"], expected)
        self.assertEqual(blocked["recovery"]["recovery_attempts"], {"git_lock": 1})
        self.assertEqual(blocked["paths"], expected)
        rendered = notifications._message("blocked", blocked)
        self.assertIn("Recovery attempts: git_lock: 1", rendered)
        self.assertEqual(rendered.count(expected[0]), 1)
        self.assertEqual(rendered.count(expected[1]), 1)

    def test_disabled_and_partial_configuration_do_not_create_events(self):
        connection = self.connection(enabled=False)
        loop_manager.update_project(
            connection,
            "/projects/alpha",
            phase="PR_OPEN",
            plan_path="docs/plans/[G-018] Reliable notices.md",
            pr_url="https://example.test/pr/18",
        )
        self.assertEqual(self.events(connection), [])

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.ini"
            secret = "telegram-secret-token"
            config.write_text(
                f"[telegram]\ntoken = {secret}\nchat_id =\n",
                encoding="utf-8",
            )
            with self.assertRaises(ManagedConfigError) as raised:
                load_managed_config(config)
            self.assertNotIn(secret, str(raised.exception))

    def test_persistent_failure_queues_once_and_keeps_disabled_diagnostics(self):
        connection = self.connection(enabled=False)
        repo = "/projects/alpha"
        repository = RecoveryRepository(connection)
        failure = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, repo, "G-018", "PUBLISHING",
            "plan-sha", "base-sha", "fetch-failure", "head-a", "fetch failed",
        )
        for _ in range(3):
            before = repository.external_run_snapshot(repo)
            repository.observe(failure)
            repository.observe_external_run(repo, before)
        notifications.enqueue_persistent_execution_failure_events(connection, repo)
        self.assertEqual(self.events(connection), [])
        diagnostic = connection.execute(
            "SELECT consecutive_runs, notification_eligible, notification_queued "
            "FROM persistent_execution_failures WHERE repo = ? AND subject = 'G-018'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(diagnostic), (3, 1, 0))

        notifications.configure_session(connection, enabled=True)
        with connection:
            notifications.enqueue_persistent_execution_failure_events(connection, repo)
            notifications.enqueue_persistent_execution_failure_events(connection, repo)
        events = self.events(connection)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "persistent_execution_failure")
        payload = json.loads(events[0]["payload"])
        self.assertEqual(payload["consecutive_external_runs"], 3)
        self.assertEqual(payload["execplan_id"], "G-018")
        self.assertIn("fetch failed", notifications._message(events[0]["event_type"], payload))

        failed = mock.Mock(side_effect=RuntimeError("network unavailable"))
        self.assertEqual(
            notifications.dispatch_pending(connection, "token", "chat", adapter=failed),
            0,
        )
        delivered = mock.Mock()
        self.assertEqual(
            notifications.dispatch_pending(connection, "token", "chat", adapter=delivered),
            1,
        )
        delivered.assert_called_once()

    def test_failed_persistent_delivery_remains_retryable_after_resolution(self):
        connection = self.connection()
        repo = "/projects/alpha"
        repository = RecoveryRepository(connection)
        failure = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, repo, "G-018", "PUBLISHING",
            "plan-sha", "base-sha", "fetch-failure", "head-a", "fetch failed",
        )
        for _ in range(3):
            before = repository.external_run_snapshot(repo)
            repository.observe(failure)
            repository.observe_external_run(repo, before)
        with connection:
            notifications.enqueue_persistent_execution_failure_events(connection, repo)

        failed = mock.Mock(side_effect=RuntimeError("network unavailable"))
        for _ in range(2):
            self.assertEqual(
                notifications.dispatch_pending(
                    connection, "token", "chat", adapter=failed,
                ),
                0,
            )
        with connection:
            repository.finish_external_run(repo, None, resolved=True)
        event = connection.execute(
            "SELECT attempts, sent_at FROM notification_outbox "
            "WHERE repo = ? AND event_type = 'persistent_execution_failure'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(event), (2, None))

        delivered = mock.Mock()
        self.assertEqual(
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=delivered,
            ),
            1,
        )
        delivered.assert_called_once()

    def test_pr_and_blocked_payloads_are_complete_and_deduplicated(self):
        connection = self.connection()
        repo = "/projects/alpha"
        path = "docs/plans/[G-018] Reliable notices.md"
        loop_manager.update_project(
            connection,
            repo,
            phase="PR_OPEN",
            plan_path=path,
            pr_url="https://example.test/pr/18",
        )
        loop_manager.update_project(connection, repo, phase="PR_OPEN")
        with connection:
            scoped_blocking.record_plan_block(
                connection,
                repo,
                "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "User decision required",
                "decision-fingerprint",
            )
            checkpoint_repository = (
                blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(repo, "G-018"),
                "workspace가 아직 생성되지 않았습니다.",
                retryable=False,
            )
            notifications.enqueue_current_project_events(connection, repo)
            notifications.enqueue_current_project_events(connection, repo)

        events = self.events(connection)
        self.assertEqual(len(events), 2)
        payloads = {
            row["event_type"]: json.loads(row["payload"]) for row in events
        }
        self.assertEqual(
            payloads["pr_created"],
            {
                "project": "alpha",
                "execplan_id": "G-018",
                "execplan_title": "Reliable notices",
                "pr_url": "https://example.test/pr/18",
            },
        )
        blocked = payloads["blocked"]
        self.assertEqual(blocked["stage"], "ExecPlan")
        self.assertEqual(blocked["scope"], "plan")
        self.assertEqual(blocked["reason_code"], "user_decision_required")
        self.assertEqual(blocked["cause"], "User decision required")
        self.assertEqual(
            blocked["paths"],
            [],
        )
        self.assertEqual(blocked["recovery"]["block_reason_code"], "user_decision_required")
        self.assertEqual(
            blocked["checkpoint"],
            {
                "result": "local_only",
                "reason": "workspace가 아직 생성되지 않았습니다.",
            },
        )

    def test_blocked_error_update_keeps_one_identity_event(self):
        connection = self.connection()
        repo = "/projects/alpha"
        path = "docs/plans/[G-018] Reliable notices.md"
        with connection:
            scoped_blocking.record_plan_block(
                connection,
                repo,
                "G-018",
                scoped_blocking.BlockReasonCode.PLAN_INPUT_INVALID,
                "Initial user action",
                "input-fingerprint",
            )
            checkpoint_repository = (
                blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(repo, "G-018"),
                "local-only",
                retryable=False,
            )
            notifications.enqueue_current_project_events(connection, repo)

        loop_manager._update_blocked_error(
            connection,
            repo,
            "G-018",
            "Different recovery action",
        )
        loop_manager._update_blocked_error(
            connection,
            repo,
            "G-018",
            "Different recovery action",
        )

        causes = {
            json.loads(row["payload"])["cause"]
            for row in self.events(connection)
            if row["event_type"] == "blocked"
        }
        self.assertEqual(
            causes,
            {"Initial user action"},
        )

    def test_blocked_event_waits_for_checkpoint_and_includes_published_commit(self):
        connection = self.connection()
        repo = "/projects/alpha"
        connection.execute(
            "UPDATE projects SET origin = ? WHERE repo = ?",
            ("example/alpha", repo),
        )
        connection.execute(
            "UPDATE exec_plans SET branch = ?, worktree = ?, workspace_owned = 1 "
            "WHERE repo = ? AND plan_id = 'G-018'",
            ("loop-manager/G-018", "/state/G-018", repo),
        )
        with connection:
            scoped_blocking.record_plan_block(
                connection,
                repo,
                "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "User decision required",
                "decision-fingerprint",
            )
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(self.events(connection), [])

        repository = blocked_checkpoint_state.BlockedCheckpointRepository(
            connection
        )
        oid = "a" * 40
        repository.record_local(repository.get(repo, "G-018"), oid)
        for _ in range(2):
            # Recreate the repository as on a later dispatch. Neither the
            # retry transition nor an unrelated outbox scan may finalize it.
            repository = blocked_checkpoint_state.BlockedCheckpointRepository(
                connection
            )
            with mock.patch.object(
                notifications, "enqueue_current_project_events"
            ) as enqueue:
                repository.finish_unpublished(
                    repository.get(repo, "G-018"),
                    "temporary network outage",
                    retryable=True,
                )
            enqueue.assert_not_called()
            pending = repository.pending(repo)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].phase.value, "retry_pending")
            self.assertEqual(pending[0].local_oid, oid)
            with connection:
                notifications.enqueue_current_project_events(connection, repo)
            self.assertEqual(self.events(connection), [])
        repository.finish_published(repository.get(repo, "G-018"), oid)
        repository.finish_published(repository.get(repo, "G-018"), oid)
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(repository.pending(repo), ())
        payload = json.loads(self.events(connection)[0]["payload"])
        self.assertEqual(payload["checkpoint"]["result"], "published")
        self.assertEqual(payload["checkpoint"]["commit_oid"], oid)
        self.assertEqual(
            payload["checkpoint"]["branch_url"],
            f"https://github.com/example/alpha/tree/{oid}",
        )


    def test_planning_blocked_payload_lists_basis_documents(self):
        connection = self.connection()
        loop_manager.update_project(
            connection,
            "/projects/alpha",
            phase="PLANNING",
            plan_path=None,
            planning_error="Planner output remained invalid",
        )
        row = self.events(connection)[0]
        payload = json.loads(row["payload"])
        self.assertEqual(row["event_type"], "blocked")
        self.assertEqual(payload["stage"], "planning")
        self.assertIn("docs/PRODUCT.md", payload["paths"])
        self.assertIn("docs/requirements/*.md", payload["paths"])
        self.assertIn("docs/ARCHITECTURE.md", payload["paths"])

    def test_failed_delivery_stays_pending_retries_once_and_hides_secrets(self):
        connection = self.connection()
        loop_manager.update_project(
            connection,
            "/projects/alpha",
            phase="PR_OPEN",
            plan_path="docs/plans/[G-018] Reliable notices.md",
            pr_url="https://example.test/pr/18",
        )
        token = "telegram-secret-token"
        chat_id = "secret-chat"
        failed = mock.Mock(
            side_effect=RuntimeError(f"https://api.telegram.org/bot{token}/failed")
        )
        self.assertEqual(
            notifications.dispatch_pending(
                connection, token, chat_id, adapter=failed
            ),
            0,
        )
        pending = self.events(connection)[0]
        self.assertIsNone(pending["sent_at"])
        self.assertIsNone(pending["delivery_started_at"])
        self.assertEqual(pending["attempts"], 1)
        self.assertNotIn(token, pending["last_error"])
        self.assertNotIn(chat_id, pending["last_error"])

        delivered = mock.Mock()
        self.assertEqual(
            notifications.dispatch_pending(
                connection, token, chat_id, adapter=delivered
            ),
            1,
        )
        self.assertEqual(
            notifications.dispatch_pending(
                connection, token, chat_id, adapter=delivered
            ),
            0,
        )
        delivered.assert_called_once()
        self.assertNotIn(token, delivered.call_args.args[2])
        self.assertNotIn(chat_id, delivered.call_args.args[2])

    def test_new_pr_delivery_is_scoped_and_fallback_keeps_pending_failure(self):
        connection = self.connection()
        repo = "/projects/alpha"
        other_repo = "/projects/beta"
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (other_repo,),
        )
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, pr_url
            ) VALUES (?, 'G-019', 'docs/plans/other.md', 'Other PR',
                      '[]', '[]', 'normal', 'hash', 'PR_OPEN', ?)
            """,
            (other_repo, "https://example.test/pr/19"),
        )
        with connection:
            loop_manager.update_project(
                connection,
                repo,
                phase="PR_OPEN",
                plan_path="docs/plans/[G-018] Reliable notices.md",
                pr_url="https://example.test/pr/18",
            )
            notifications.enqueue_current_project_events(connection, other_repo)

        failed = mock.Mock(side_effect=RuntimeError("network unavailable"))
        self.assertEqual(
            notifications.dispatch_new_pr_notifications(
                connection, "token", "chat", repo, adapter=failed
            ),
            0,
        )
        failed.assert_called_once()
        # A later project step does not immediately retry the failed event.
        self.assertEqual(
            notifications.dispatch_new_pr_notifications(
                connection, "token", "chat", repo, adapter=mock.Mock()
            ),
            0,
        )
        pending = connection.execute(
            "SELECT attempts, sent_at FROM notification_outbox WHERE repo = ?",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(pending), (1, None))
        other = connection.execute(
            "SELECT attempts FROM notification_outbox WHERE repo = ?",
            (other_repo,),
        ).fetchone()
        self.assertEqual(other[0], 0)

        delivered = mock.Mock()
        self.assertEqual(
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=delivered
            ),
            2,
        )
        self.assertEqual(delivered.call_count, 2)
        self.assertEqual(
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=delivered
            ),
            0,
        )

    def test_pr_delivery_precedes_next_project_step(self):
        connection = self.connection()
        repo = "/projects/alpha"
        delivered = mock.Mock()
        steps = []

        def run_step(*_args, **_kwargs):
            steps.append(len(steps) + 1)
            if len(steps) == 1:
                loop_manager.update_project(
                    connection,
                    repo,
                    phase="PR_OPEN",
                    plan_path="docs/plans/[G-018] Reliable notices.md",
                    pr_url="https://example.test/pr/18",
                )
                self.assertIsNone(self.events(connection)[0]["sent_at"])
                return loop_manager.PROJECT_STEP_RETRY
            self.assertIsNotNone(self.events(connection)[0]["sent_at"])
            delivered.assert_called_once()
            return 0

        def deliver():
            notifications.dispatch_new_pr_notifications(
                connection, "token", "chat", repo, adapter=delivered
            )

        with mock.patch(
            "loop_manager._checkpoint_dispatch_ready", return_value=False
        ), mock.patch("loop_manager._run_project_step", side_effect=run_step):
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                repo,
                Path("unused.ini"),
                None,
                False,
                on_step_completed=deliver,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(steps, [1, 2])
        self.assertEqual(self.events(connection)[0]["attempts"], 1)
        self.assertEqual(
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=delivered
            ),
            0,
        )
        delivered.assert_called_once()

    def test_registered_project_wires_immediate_delivery_after_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            connection = self.connection(database=state_dir / "loop-manager.db")
            repo = "/projects/alpha"
            project = project_registry.RegisteredProject(
                repo, "main", "codex", None, False
            )
            delivered = mock.Mock()
            original_dispatch = notifications.dispatch_new_pr_notifications

            def send_now(worker_connection, token, chat_id, selected_repo):
                return original_dispatch(
                    worker_connection,
                    token,
                    chat_id,
                    selected_repo,
                    adapter=delivered,
                )

            def run_one(worker_connection, *_args, on_step_completed, **_kwargs):
                with worker_connection:
                    worker_connection.execute(
                        "UPDATE exec_plans SET status = 'PR_OPEN', pr_url = ? "
                        "WHERE repo = ? AND plan_id = 'G-018'",
                        ("https://example.test/pr/18", repo),
                    )
                    notifications.enqueue_current_project_events(
                        worker_connection, repo
                    )
                self.assertIsNone(
                    worker_connection.execute(
                        "SELECT sent_at FROM notification_outbox"
                    ).fetchone()[0]
                )
                on_step_completed()
                self.assertIsNotNone(
                    worker_connection.execute(
                        "SELECT sent_at FROM notification_outbox"
                    ).fetchone()[0]
                )
                return loop_manager.ProjectRunResult(
                    loop_manager.ProjectRunKind.WAITING, 0
                )

            with mock.patch(
                "loop_manager.run_project_result", side_effect=run_one
            ), mock.patch(
                "loop_manager.notifications.dispatch_new_pr_notifications",
                side_effect=send_now,
            ):
                result = loop_manager._run_registered_project(
                    project,
                    state_dir,
                    Path("unused.ini"),
                    "token",
                    "chat",
                    mock.Mock(),
                    5,
                )
            self.assertEqual(result.result.kind, loop_manager.ProjectRunKind.WAITING)
            delivered.assert_called_once()
            self.assertEqual(
                notifications.dispatch_pending(
                    connection, "token", "chat", adapter=delivered
                ),
                0,
            )
            connection.close()

    def test_ambiguous_interruption_stays_pending_and_retries_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            loop_manager.update_project(
                connection,
                "/projects/alpha",
                phase="PR_OPEN",
                plan_path="docs/plans/[G-018] Reliable notices.md",
                pr_url="https://example.test/pr/18",
            )
            delivered = []

            def deliver_then_interrupt(_token, _chat_id, text):
                delivered.append(text)
                raise KeyboardInterrupt("process interrupted after delivery")

            with self.assertRaisesRegex(KeyboardInterrupt, "process interrupted"):
                notifications.dispatch_pending(
                    connection,
                    "token",
                    "chat",
                    adapter=deliver_then_interrupt,
                )
            connection.close()

            restarted = sqlite3.connect(database)
            restarted.row_factory = sqlite3.Row
            notifications.ensure_schema(restarted)
            claimed = self.events(restarted)[0]
            self.assertEqual(claimed["attempts"], 1)
            self.assertIsNotNone(claimed["delivery_started_at"])
            self.assertIsNone(claimed["sent_at"])

            restarted_adapter = mock.Mock()
            self.assertEqual(
                notifications.dispatch_pending(
                    restarted,
                    "token",
                    "chat",
                    adapter=restarted_adapter,
                ),
                1,
            )
            restarted_adapter.assert_called_once()
            retried = self.events(restarted)[0]
            self.assertEqual(retried["attempts"], 2)
            self.assertIsNotNone(retried["sent_at"])
            restarted.close()
        self.assertEqual(len(delivered), 1)

    def test_scheduler_retries_pending_when_project_processing_is_skipped(self):
        connection = self.connection()
        loop_manager.update_project(
            connection,
            "/projects/alpha",
            phase="PR_OPEN",
            plan_path="docs/plans/[G-018] Reliable notices.md",
            pr_url="https://example.test/pr/18",
        )
        config = mock.Mock(
            telegram_token="token",
            telegram_chat_id="chat",
            max_workers=1,
        )
        delivered = mock.Mock()
        original_dispatch = notifications.dispatch_pending

        def dispatch_pending(connection, token, chat_id):
            return original_dispatch(
                connection, token, chat_id, adapter=delivered
            )

        with mock.patch(
            "loop_manager.notifications.dispatch_pending",
            side_effect=dispatch_pending,
        ) as dispatch, mock.patch(
            "loop_manager.schedule_registered_projects", return_value=()
        ):
            self.assertEqual(
                loop_manager.run_registered_projects(
                    connection, Path("/state"), config
                ),
                0,
            )
        self.assertEqual(dispatch.call_count, 2)
        delivered.assert_called_once()
        self.assertIsNotNone(self.events(connection)[0]["sent_at"])


class SegmentedDeliveryTest(NotificationFixture, unittest.TestCase):
    def add_event(self, connection, kind="pr_created", body=""):
        payload = {
            "project": "alpha", "execplan_id": "G-018",
            "execplan_title": body, "pr_url": "https://example.test/pr/18",
        }
        notifications._insert(connection, "test-event", "/projects/alpha", kind, payload)
        connection.commit()
        return notifications._message(kind, payload)

    def test_exact_boundaries_and_unicode_round_trip(self):
        for length in (4096, 4097, 8193):
            with self.subTest(length=length):
                connection = self.connection()
                header = "[alpha] PR created\nG-018: "
                suffix = "\nhttps://example.test/pr/18"
                title_length = length - len(header) - len(suffix)
                title = ("한글🙂\n" * (title_length // 4 + 1))[:title_length]
                body = self.add_event(connection, body=title)
                self.assertEqual(len(body), length)
                calls = []
                self.assertEqual(notifications.dispatch_pending(
                    connection, "token", "chat",
                    adapter=lambda _token, _chat, text: calls.append(text),
                ), 1)
                self.assertEqual([len(piece) for piece in calls],
                                 [4096] * (length // 4096) +
                                 ([length % 4096] if length % 4096 else []))
                self.assertEqual("".join(calls), body)
                row = self.events(connection)[0]
                self.assertEqual(row["confirmed_pieces"], len(calls))
                self.assertIsNotNone(row["sent_at"])
                connection.close()

    def test_later_failure_resumes_frozen_text_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            original = self.add_event(connection, body="가🙂\n" * 3000)
            delivered = []

            def fail_second(_token, _chat, text):
                if delivered:
                    raise RuntimeError("body-secret token-secret chat-secret")
                delivered.append(text)

            self.assertEqual(notifications.dispatch_pending(
                connection, "token-secret", "chat-secret", adapter=fail_second,
            ), 0)
            row = self.events(connection)[0]
            self.assertEqual(row["confirmed_pieces"], 1)
            self.assertEqual(row["attempts"], 2)
            self.assertIsNone(row["sent_at"])
            self.assertEqual(row["rendered_text"], original)
            for secret in ("body-secret", "token-secret", "chat-secret", original):
                self.assertNotIn(secret, row["last_error"])
            connection.execute(
                "UPDATE notification_outbox SET payload = ? WHERE event_key = ?",
                ('{"project":"changed"}', "test-event"),
            )
            connection.commit()
            connection.close()

            restarted = sqlite3.connect(database)
            restarted.row_factory = sqlite3.Row
            notifications.ensure_schema(restarted)
            remaining = []
            self.assertEqual(notifications.dispatch_pending(
                restarted, "token", "chat",
                adapter=lambda _token, _chat, text: remaining.append(text),
            ), 1)
            self.assertEqual("".join(delivered + remaining), original)
            self.assertEqual(self.events(restarted)[0]["confirmed_pieces"], 3)
            self.assertEqual(notifications.dispatch_pending(
                restarted, "token", "chat", adapter=mock.Mock(),
            ), 0)
            restarted.close()

    def test_interrupted_later_piece_retries_only_uncertain_piece(self):
        connection = self.connection()
        original = self.add_event(connection, body="x" * 8200)
        delivered = []

        def interrupt(_token, _chat, text):
            delivered.append(text)
            if len(delivered) == 2:
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=interrupt,
            )
        row = self.events(connection)[0]
        self.assertEqual(row["confirmed_pieces"], 1)
        self.assertEqual(row["attempts"], 2)
        self.assertIsNotNone(row["delivery_started_at"])
        retried = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, text: retried.append(text),
        ), 1)
        self.assertEqual(retried[0], delivered[1])
        self.assertEqual("".join([delivered[0], *retried]), original)
        self.assertEqual(self.events(connection)[0]["attempts"], 4)

    def test_immediate_pr_dispatch_uses_same_piece_cursor(self):
        connection = self.connection()
        original = self.add_event(connection, body="x" * 5000)
        calls = []
        self.assertEqual(notifications.dispatch_new_pr_notifications(
            connection, "token", "chat", "/projects/alpha",
            adapter=lambda _token, _chat, text: calls.append(text),
        ), 1)
        self.assertEqual("".join(calls), original)
        self.assertEqual(self.events(connection)[0]["attempts"], 2)
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        ), 0)

    def test_cleared_block_cancels_remaining_pieces_and_keeps_progress(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            scoped_blocking.record_plan_block(
                connection, repo, "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "x" * 9000, "fingerprint",
            )
            checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
                connection
            )
            checkpoint.finish_unpublished(
                checkpoint.get(repo, "G-018"), "local only", retryable=False,
            )
            notifications.enqueue_current_project_events(connection, repo)
        calls = []

        def clear_after_first(_token, _chat, text):
            calls.append(text)
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            connection.commit()

        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=clear_after_first,
        ), 0)
        row = self.events(connection)[0]
        self.assertEqual(len(calls), 1)
        self.assertEqual(row["confirmed_pieces"], 1)
        self.assertIsNotNone(row["canceled_at"])
        self.assertIsNone(row["sent_at"])
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        ), 0)

    def test_published_block_with_remote_inspection_is_delivered(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            scoped_blocking.record_plan_block(
                connection, repo, "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "Decision still required", "fingerprint",
            )
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        request = checkpoint.get(repo, "G-018")
        checkpoint.finish_published(request, "a" * 40)
        original_key = self.events(connection)[0]["event_key"]
        checkpoint.record_remote_inspection(
            request, "b" * 40, "input", "still blocked",
        )
        self.assertEqual(len(self.events(connection)), 2)
        current_key = next(
            row["event_key"] for row in self.events(connection)
            if row["event_key"] != original_key
        )

        delivered = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=delivered,
        ), 1)
        delivered.assert_called_once()
        rows = {row["event_key"]: row for row in self.events(connection)}
        self.assertIsNotNone(rows[original_key]["canceled_at"])
        self.assertIsNone(rows[current_key]["canceled_at"])
        self.assertIsNotNone(rows[current_key]["sent_at"])

    def test_cleared_planning_error_cancels_only_its_unsent_notice(self):
        connection = self.connection()
        repo = "/projects/alpha"
        loop_manager.update_project(
            connection, repo, phase="PLANNING", planning_error="Old failure",
        )
        pr_message = self.add_event(connection, body="Unrelated PR")
        connection.execute(
            "UPDATE projects SET planning_error = NULL WHERE repo = ?", (repo,),
        )
        connection.commit()
        calls = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, text: calls.append(text),
        ), 1)
        self.assertEqual(calls, [pr_message])
        rows = {row["event_type"]: row for row in self.events(connection)}
        self.assertIsNotNone(rows["blocked"]["canceled_at"])
        self.assertEqual(rows["blocked"]["confirmed_pieces"], 0)
        self.assertIsNone(rows["blocked"]["sent_at"])
        self.assertIsNotNone(rows["pr_created"]["sent_at"])

    def test_legacy_pending_row_gains_piece_progress(self):
        connection = self.connection()
        connection.execute("DROP TABLE notification_outbox")
        connection.execute(
            "CREATE TABLE notification_outbox (event_key TEXT PRIMARY KEY, "
            "repo TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "delivery_started_at TEXT, sent_at TEXT)"
        )
        original = self.add_event(connection, body="legacy")
        calls = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, text: calls.append(text),
        ), 1)
        self.assertEqual(calls, [original])
        self.assertEqual(self.events(connection)[0]["confirmed_pieces"], 1)
        self.assertIsNotNone(self.events(connection)[0]["delivery_started_at"])


class CompletionCycleTest(NotificationFixture, unittest.TestCase):
    @staticmethod
    def _snapshot(cycle: str) -> work_exhaustion.WorkReasonSnapshot:
        return work_exhaustion.WorkReasonSnapshot(cycle, (), (), None, None)

    def _complete_project(
        self,
        connection: sqlite3.Connection,
        fingerprint: str,
    ) -> None:
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.execute(
            "UPDATE projects SET phase = NULL, planning_error = NULL, "
            "planning_fingerprint = ? WHERE repo = ?",
            (fingerprint, "/projects/alpha"),
        )
        notifications.record_merged_plan(
            connection, "/projects/alpha", "G-018", "Reliable notices"
        )
        connection.commit()

    def test_completion_payload_requires_all_gates_and_has_last_work(self):
        connection = self.connection()
        connection.execute(
            "UPDATE projects SET planning_fingerprint = 'cycle-one' WHERE repo = ?",
            ("/projects/alpha",),
        )
        self.assertFalse(
            notifications.enqueue_completion_if_ready(
                connection, "/projects/alpha", self._snapshot("cycle-one")
            )
        )
        self._complete_project(connection, "cycle-one")
        self.assertTrue(
            notifications.enqueue_completion_if_ready(
                connection, "/projects/alpha", self._snapshot("cycle-one")
            )
        )
        event = self.events(connection)[0]
        payload = json.loads(event["payload"])
        self.assertEqual(event["event_type"], "project_completed")
        self.assertEqual(payload["project"], "alpha")
        self.assertEqual(payload["last_completed_plan_id"], "G-018")
        self.assertEqual(payload["last_completed_title"], "Reliable notices")
        self.assertIn("활성 Requirement", payload["reason"])

    def test_completion_while_disabled_cancels_wait_before_reenabled_dispatch(self):
        for fail_delivery in (False, True):
            with self.subTest(fail_delivery=fail_delivery), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "state.db"
                connection = self.connection(database=database)
                repo = "/projects/alpha"
                waiting = work_exhaustion.WorkReasonSnapshot(
                    "cycle-one", (),
                    ({"id": "[R-031]", "path": "docs/requirements/[R-031] x.md",
                      "reason": "archive 확인 대기"},), None, None,
                )
                self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, waiting))
                if fail_delivery:
                    self.assertEqual(notifications.dispatch_pending(
                        connection, "token", "chat",
                        adapter=mock.Mock(side_effect=RuntimeError("network unavailable")),
                    ), 0)
                notifications.configure_session(connection, enabled=False)
                self._complete_project(connection, "cycle-one")
                self.assertFalse(notifications.enqueue_completion_if_ready(
                    connection, repo, self._snapshot("cycle-one"),
                ))
                events = self.events(connection)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "work_waiting")
                self.assertIsNotNone(events[0]["canceled_at"])
                self.assertIsNone(events[0]["sent_at"])
                self.assertEqual(events[0]["attempts"], int(fail_delivery))
                connection.close()

                restarted = sqlite3.connect(database)
                self.addCleanup(restarted.close)
                restarted.row_factory = sqlite3.Row
                notifications.configure_session(restarted, enabled=True)
                delivered = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(
                    restarted, "token", "chat", adapter=delivered,
                ), 0)
                delivered.assert_not_called()
                for _ in range(2):
                    self.assertTrue(notifications.enqueue_completion_if_ready(
                        restarted, repo, self._snapshot("cycle-one"),
                    ))
                self.assertEqual(notifications.dispatch_pending(
                    restarted, "token", "chat", adapter=delivered,
                ), 1)
                delivered.assert_called_once()
                self.assertEqual(
                    [row["event_type"] for row in self.events(restarted)
                     if row["sent_at"] is not None],
                    ["project_completed"],
                )

    def test_same_and_archive_only_cycle_deduplicate_but_new_input_reopens(self):
        connection = self.connection()
        self._complete_project(connection, "cycle-one")
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-one"))
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-one"))

        # Archive-only handling retains the prior planning fingerprint.
        connection.execute(
            "UPDATE projects SET processed_sha = 'archive-only-sha' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-one"))
        self.assertEqual(len(self.events(connection)), 1)

        connection.execute(
            "UPDATE projects SET planning_fingerprint = 'cycle-two' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-two"))
        self.assertEqual(len(self.events(connection)), 2)

    def test_planning_error_blocks_completion(self):
        connection = self.connection()
        self._complete_project(connection, "cycle-one")
        connection.execute(
            "UPDATE projects SET planning_error = 'still blocked' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        self.assertFalse(
            notifications.enqueue_completion_if_ready(
                connection, "/projects/alpha", self._snapshot("cycle-one")
            )
        )
        self.assertEqual(self.events(connection), [])

    def test_active_requirement_and_project_block_reject_completion(self):
        connection = self.connection()
        self._complete_project(connection, "cycle-one")
        requirement = work_exhaustion.WorkReasonSnapshot(
            "cycle-one", (), ({"id": "[R-031]", "path": "docs/requirements/[R-031] x.md",
                               "reason": "archive 확인 대기"},), None, None,
        )
        self.assertFalse(notifications.enqueue_completion_if_ready(
            connection, "/projects/alpha", requirement,
        ))
        connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
            "VALUES (?, 'project', 'repository_state_unsafe', 'unsafe')",
            ("/projects/alpha",),
        )
        self.assertFalse(notifications.enqueue_completion_if_ready(
            connection, "/projects/alpha", self._snapshot("cycle-one"),
        ))
        self.assertEqual(self.events(connection), [])


class WorkReasonOutboxTest(NotificationFixture, unittest.TestCase):
    def test_waiting_snapshot_lists_confirmed_causes_and_deduplicates_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            repo = "/projects/alpha"
            connection.execute(
                "UPDATE exec_plans SET depends_on = '[\"G-017\"]' "
                "WHERE repo = ? AND plan_id = 'G-018'", (repo,),
            )
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "(?, 'G-017', 'docs/plans/[G-017] Base.md', 'Base', '[]', "
                "'[]', 'normal', 'hash', 'PR_OPEN')", (repo,),
            )
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "(?, 'G-019', 'docs/plans/[G-019] Choice.md', 'Choice', '[]', "
                "'[]', 'normal', 'hash', 'PLAN_BLOCKED')", (repo,),
            )
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-019', 'plan', 'user_decision_required', 'Choose A or B')",
                (repo,),
            )
            paths = ("docs/requirements/[R-031] Waiting.md",)
            snapshot = work_exhaustion.collect(
                connection, repo, cycle="cycle-one", active_requirements=paths,
            )
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertFalse(snapshot.completed)
            self.assertIn("G-017 (PR_OPEN)", snapshot.describe())
            self.assertIn("user_decision_required", snapshot.describe())
            self.assertIn("[R-031]", snapshot.describe())
            self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
            first = self.events(connection)[0]
            message = notifications._message("work_waiting", json.loads(first["payload"]))
            self.assertIn("G-017 Base: PR Merge 대기", message)
            self.assertIn("G-019 Choice", message)
            connection.close()

            restarted = sqlite3.connect(database)
            restarted.row_factory = sqlite3.Row
            notifications.configure_session(restarted, enabled=True)
            notifications.enqueue_waiting_reason(restarted, repo, snapshot)
            self.assertEqual(len(self.events(restarted)), 1)
            changed = work_exhaustion.WorkReasonSnapshot(
                "cycle-one", snapshot.plans,
                ({"id": "[R-031]", "path": paths[0], "reason": "새 archive 원인"},),
                None, None,
            )
            notifications.enqueue_waiting_reason(restarted, repo, changed)
            self.assertEqual(len(self.events(restarted)), 2)
            self.assertIsNotNone(self.events(restarted)[0]["canceled_at"])
            notifications.mark_work_available(restarted, repo)
            notifications.enqueue_waiting_reason(restarted, repo, snapshot)
            self.assertEqual(len(self.events(restarted)), 3)
            restarted.close()

    def test_disabled_wait_transition_cancels_stale_notice_before_restart(self):
        for remaining_requirement in (False, True):
            for failed_delivery in (False, True):
                with self.subTest(requirement=remaining_requirement, failed=failed_delivery), tempfile.TemporaryDirectory() as directory:
                    database = Path(directory) / "state.db"
                    connection = self.connection(database=database)
                    repo = "/projects/alpha"
                    connection.execute(
                        "UPDATE exec_plans SET status = 'PR_OPEN' WHERE repo = ?", (repo,),
                    )
                    waiting = work_exhaustion.collect(
                        connection, repo, cycle="cycle-one", active_requirements=(),
                    )
                    assert waiting is not None
                    self.assertIn("PR Merge 대기", waiting.describe())
                    self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, waiting))
                    if failed_delivery:
                        self.assertEqual(notifications.dispatch_pending(
                            connection, "token", "chat",
                            adapter=mock.Mock(side_effect=RuntimeError("network unavailable")),
                        ), 0)
                    notifications.configure_session(connection, enabled=False)
                    # An unchanged wait still owns its pending delivery.
                    self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, waiting))
                    self.assertIsNone(self.events(connection)[0]["canceled_at"])
                    connection.execute(
                        "UPDATE exec_plans SET status = 'PLAN_BLOCKED' WHERE repo = ?", (repo,),
                    )
                    connection.execute(
                        "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                        "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'PR closed without merge')",
                        (repo,),
                    )
                    changed = work_exhaustion.collect(
                        connection, repo, cycle="cycle-one",
                        active_requirements=("docs/requirements/[R-031] x.md",)
                        if remaining_requirement else (),
                    )
                    assert changed is not None
                    self.assertEqual(changed.has_distinct_wait, remaining_requirement)
                    self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, changed))
                    events = self.events(connection)
                    self.assertEqual(len(events), 1)
                    self.assertIsNotNone(events[0]["canceled_at"])
                    self.assertIsNone(events[0]["sent_at"])
                    self.assertEqual(events[0]["attempts"], int(failed_delivery))
                    connection.close()

                    restarted = sqlite3.connect(database)
                    self.addCleanup(restarted.close)
                    restarted.row_factory = sqlite3.Row
                    notifications.configure_session(restarted, enabled=True)
                    delivered = mock.Mock()
                    self.assertEqual(notifications.dispatch_pending(
                        restarted, "token", "chat", adapter=delivered,
                    ), 0)
                    delivered.assert_not_called()
                    for _ in range(2):
                        self.assertEqual(notifications.enqueue_waiting_reason(
                            restarted, repo, changed,
                        ), remaining_requirement)
                    self.assertEqual(notifications.dispatch_pending(
                        restarted, "token", "chat", adapter=delivered,
                    ), int(remaining_requirement))
                    self.assertEqual(len(self.events(restarted)), 1 + int(remaining_requirement))

    def test_block_only_suppresses_wait_and_disabled_delivery_keeps_reason(self):
        connection = self.connection(enabled=False)
        repo = "/projects/alpha"
        connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED' WHERE repo = ?", (repo,),
        )
        connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'Choose A')",
            (repo,),
        )
        snapshot = work_exhaustion.collect(
            connection, repo, cycle="cycle-one", active_requirements=(),
        )
        assert snapshot is not None
        self.assertIn("Choose A", snapshot.describe())
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        notifications.configure_session(connection, enabled=True)
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertEqual(self.events(connection), [])

        requirement_path = "docs/requirements/[R-031] x.md"
        connection.execute(
            "UPDATE exec_plans SET sources = ? WHERE repo = ? AND plan_id = 'G-018'",
            (json.dumps([requirement_path]), repo),
        )
        related = work_exhaustion.collect(
            connection, repo, cycle="cycle-one",
            active_requirements=(requirement_path,),
        )
        assert related is not None
        self.assertIn("[R-031]", related.describe())
        self.assertFalse(related.has_distinct_wait)
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, related))
        self.assertEqual(self.events(connection), [])

        connection.execute(
            "UPDATE exec_plans SET sources = '[]' "
            "WHERE repo = ? AND plan_id = 'G-018'", (repo,),
        )
        with_requirement = work_exhaustion.collect(
            connection, repo, cycle="cycle-one",
            active_requirements=(requirement_path,),
        )
        assert with_requirement is not None
        self.assertTrue(with_requirement.has_distinct_wait)
        notifications.enqueue_waiting_reason(connection, repo, with_requirement)
        failed = mock.Mock(side_effect=RuntimeError("network unavailable"))
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=failed,
        ), 0)
        event = self.events(connection)[0]
        self.assertEqual(event["attempts"], 1)
        self.assertIsNone(event["sent_at"])
        delivered = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=delivered,
        ), 1)
        self.assertIsNotNone(self.events(connection)[0]["sent_at"])


if __name__ == "__main__":
    unittest.main()
