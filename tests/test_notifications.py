import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from managed_config import ManagedConfigError, load_managed_config
import notifications


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
        connection.commit()
        return connection

    @staticmethod
    def events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM notification_outbox ORDER BY event_type"
        ).fetchall()


class NotificationOutboxTest(NotificationFixture, unittest.TestCase):
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
        loop_manager.update_project(
            connection,
            repo,
            phase="BLOCKED",
            last_error="User decision required",
        )
        loop_manager.update_project(
            connection,
            repo,
            phase="BLOCKED",
            last_error="User decision required",
        )

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
        self.assertEqual(blocked["cause"], "User decision required")
        self.assertEqual(
            blocked["paths"],
            [path, "docs/PRODUCT.md"],
        )

    def test_blocked_error_update_enqueues_each_new_cause_once(self):
        connection = self.connection()
        repo = "/projects/alpha"
        path = "docs/plans/[G-018] Reliable notices.md"
        loop_manager.update_project(
            connection,
            repo,
            phase="BLOCKED",
            plan_path=path,
            last_error="Initial user action",
        )

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
            {"Initial user action", "Different recovery action"},
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


class CompletionCycleTest(NotificationFixture, unittest.TestCase):
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
                connection, "/projects/alpha"
            )
        )
        self._complete_project(connection, "cycle-one")
        self.assertTrue(
            notifications.enqueue_completion_if_ready(
                connection, "/projects/alpha"
            )
        )
        event = self.events(connection)[0]
        payload = json.loads(event["payload"])
        self.assertEqual(event["event_type"], "project_completed")
        self.assertEqual(payload["project"], "alpha")
        self.assertEqual(payload["last_completed_plan_id"], "G-018")
        self.assertEqual(payload["last_completed_title"], "Reliable notices")

    def test_same_and_archive_only_cycle_deduplicate_but_new_input_reopens(self):
        connection = self.connection()
        self._complete_project(connection, "cycle-one")
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha")
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha")

        # Archive-only handling retains the prior planning fingerprint.
        connection.execute(
            "UPDATE projects SET processed_sha = 'archive-only-sha' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha")
        self.assertEqual(len(self.events(connection)), 1)

        connection.execute(
            "UPDATE projects SET planning_fingerprint = 'cycle-two' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha")
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
                connection, "/projects/alpha"
            )
        )
        self.assertEqual(self.events(connection), [])


if __name__ == "__main__":
    unittest.main()

