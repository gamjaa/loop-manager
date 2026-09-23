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


class NotificationOutboxTest(NotificationFixture, unittest.TestCase):
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
