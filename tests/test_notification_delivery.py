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
from loop_manager_internal.notifications import transport
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import RecoveryRepository

from support.notification import NotificationFixture


class TelegramTransportTest(unittest.TestCase):
    def test_send_encodes_payload_and_checks_response(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(transport, "urlopen", return_value=response) as open_url:
            transport.send_telegram("token:part", "chat", "한글 \U0001f642")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bottoken:part/sendMessage")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "chat_id": "chat", "text": "한글 \U0001f642", "disable_notification": False,
        })
        self.assertEqual(open_url.call_args.kwargs["timeout"], 15)

        response.__enter__.return_value.status = 503
        with mock.patch.object(transport, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "non-success status") as raised:
                transport.send_telegram("secret-token", "secret-chat", "message")
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("secret-chat", str(raised.exception))


class NotificationDeliveryTest(NotificationFixture, unittest.TestCase):
    def test_pending_targeted_failure_notices_are_canceled_without_losing_sent_piece_audit(self):
        for kind in ('persistent_execution_failure', 'stage_failed'):
            for attempts, pieces in ((0, 0), (1, 0), (1, 1)):
                with self.subTest(kind=kind, attempts=attempts, pieces=pieces):
                    connection = self.connection()
                    payload = {'project': 'alpha', 'execplan_id': 'G-018', 'reason_code': 'worker_correction',
                        'operation': 'Worker', 'cause': 'Worker가 해결되지 않은 targeted unit test 실패를 보고했습니다.',
                        'status': '다음 실행에서 교정', 'stage': 'WORKING', 'scope': 'execution',
                        'consecutive_external_runs': 3}
                    notifications._insert(connection, 'legacy-targeted', '/projects/alpha', kind, payload)
                    connection.execute('UPDATE notification_outbox SET attempts=?, confirmed_pieces=?, rendered_text=?',
                        (attempts, pieces, 'already confirmed piece\n' + 'pending content\n' * 700))
                    connection.commit()
                    transport = mock.Mock()
                    notifications.dispatch_pending(connection, 'token', 'chat', adapter=transport)
                    transport.assert_not_called()
                    row = connection.execute('SELECT canceled_at, attempts, confirmed_pieces, payload FROM notification_outbox').fetchone()
                    self.assertIsNotNone(row[0])
                    self.assertEqual((row[1], row[2]), (attempts, pieces))
                    self.assertEqual(json.loads(row[3])['cause'], payload['cause'])

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
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            state_dir = Path(directory)
            connection = self.connection(database=state_dir / "loop-manager.db")
            cleanup.callback(connection.close)
            repo = "/projects/alpha"
            project = project_registry.RegisteredProject(
                repo, "main", "codex", None, False
            )
            delivered = mock.Mock()
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
                "loop_manager_internal.notifications.delivery.send_telegram",
                side_effect=delivered,
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
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            cleanup.callback(connection.close)
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
            cleanup.callback(restarted.close)
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
            "occurred_at": "2000-01-01T00:00:00Z", "status": "PR Merge 대기", "silent": False,
        }
        notifications._insert(connection, "test-event", "/projects/alpha", kind, payload)
        connection.commit()
        return notifications._message(kind, payload)

    def test_exact_boundaries_and_unicode_round_trip(self):
        for length in (4096, 4097, 8193):
            with self.subTest(length=length):
                connection = self.connection()
                header = "[alpha] G-018 PR 생성\n"
                suffix = "\nhttps://example.test/pr/18\n상태: PR 검토·Merge 대기\n발생 시각: 2000-01-01T00:00:00Z\n처리 상태: PR Merge 대기"
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
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            cleanup.callback(connection.close)
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
            cleanup.callback(restarted.close)
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


class TimelyProgressDeliveryTest(NotificationFixture, unittest.TestCase):
    def test_immediate_delivery_waits_for_commit_and_preserves_silent_pieces(self):
        from loop_manager_internal.notifications import delivery
        connection = self.connection()
        repo = '/projects/alpha'
        sent = []
        with notifications.immediate_delivery(connection, 'token', 'chat', repo), \
             mock.patch.object(delivery, 'send_telegram', side_effect=lambda *args, **kw: sent.append((args, kw))):
            with connection:
                notifications.enqueue_progress(connection, repo, 'maintenance_started', 'failure',
                    cause='x' * 6000, status='자동 교정 대기', silent=True)
                self.assertEqual(notifications.flush_project_events(connection, repo), 0)
            self.assertEqual(sent, [])
            self.assertEqual(notifications.flush_project_events(connection, repo), 1)
            self.assertEqual(len(sent), 2)
            self.assertTrue(all(kw['disable_notification'] for _args, kw in sent))
            self.assertEqual(notifications.flush_project_events(connection, repo), 0)

    def test_silent_transport_and_partial_restart_keep_original_payload(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(transport, 'urlopen', return_value=response) as open_url:
            transport.send_telegram('token', 'chat', 'notice', disable_notification=True)
        self.assertTrue(json.loads(open_url.call_args.args[0].data)['disable_notification'])
        directory = self.enterContext(tempfile.TemporaryDirectory())
        database = Path(directory) / 'state.db'
        connection = self.connection(database=database)
        with connection:
            notifications._insert(connection, 'legacy-failure', '/projects/alpha', 'stage_failed',
                {'project': 'alpha', 'cause': 'x' * 6000, 'status': '자동 교정 대기', 'silent': True})
        original = self.events(connection)[0]['payload']
        send = mock.Mock(side_effect=[None, RuntimeError('secret token')])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        self.assertEqual(self.events(connection)[0]['confirmed_pieces'], 1)
        connection.close()
        restarted = sqlite3.connect(database)
        self.addCleanup(restarted.close)
        restarted.row_factory = sqlite3.Row
        notifications.configure_session(restarted, enabled=True)
        with restarted:
            notifications.enqueue_progress(restarted, '/projects/alpha', 'stage_failed', 'failure',
                cause='changed observation', status='automatic', silent=True)
        self.assertEqual(self.events(restarted)[0]['payload'], original)
        delivered = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(restarted, 'token', 'chat', adapter=delivered), 1)
        delivered.assert_called_once()


class InterruptedBlockStartupTest(NotificationFixture, unittest.TestCase):
    def test_restart_recovers_canceled_inflight_start_before_worker_and_deduplicates(self):
        from types import SimpleNamespace
        from loop_manager_internal.notifications import delivery
        for confirmed in (0, 1):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / 'restart.db'
                connection = self.connection(database=database)
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', ?)", (repo, 'x' * 6000))
                    notifications.enqueue_current_project_events(connection, repo)
                    if confirmed:
                        connection.execute("UPDATE notification_outbox SET confirmed_pieces=1")
                key = self.events(connection)[0]['event_key']
                # Claim the next piece, then stop before confirmation or the
                # exception callback can observe its result.
                self.assertIsNotNone(delivery._claim_piece(connection, key, first_attempt_only=False))
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'saved-start', plan_id='G-018')
                started_at = connection.execute('SELECT started_at FROM notified_block_incidents').fetchone()[0]
                connection.close()
                restarted = sqlite3.connect(database)
                self.addCleanup(restarted.close)
                restarted.row_factory = sqlite3.Row
                config = SimpleNamespace(telegram_token='token', telegram_chat_id='chat')
                expected = 'resumed' if confirmed else 'work_started'
                sent = mock.Mock()
                dispatch = notifications.dispatch_pending
                def worker(*a, **kw):
                    self.assertEqual(restarted.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type=? AND sent_at IS NOT NULL", (expected,)).fetchone()[0], 1)
                    return ()
                with mock.patch('loop_manager.schedule_registered_projects', side_effect=worker), \
                        mock.patch.object(delivery, 'send_telegram', sent), \
                        mock.patch.object(notifications, 'dispatch_pending', side_effect=lambda conn, token, chat: dispatch(conn, token, chat, adapter=sent)):
                    self.assertEqual(loop_manager.run_registered_projects(restarted, Path(directory), config), 0)
                    self.assertEqual(loop_manager.run_registered_projects(restarted, Path(directory), config), 0)
                self.assertEqual(sent.call_count, 2 if confirmed else 1)
                self.assertTrue(all(call.kwargs['disable_notification'] for call in sent.call_args_list))
                event = restarted.execute("SELECT payload FROM notification_outbox WHERE event_type=?", (expected,)).fetchone()[0]
                self.assertEqual(json.loads(event)['occurred_at'], started_at)
                self.assertIsNotNone(restarted.execute('SELECT canceled_at FROM notification_outbox WHERE event_key=?', (key,)).fetchone()[0])
                with restarted:
                    notifications.record_process_start(restarted, repo, 'worker', 'saved-start', plan_id='G-018')
                    notifications.record_process_start(restarted, repo, 'worker', 'new-start', plan_id='G-018')
                self.assertEqual(restarted.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type='work_started'").fetchone()[0], 0 if confirmed else 1)
                restarted.close()

    def test_disabled_startup_preserves_abandoned_record_without_notice(self):
        from types import SimpleNamespace
        from loop_manager_internal.notifications import delivery
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
            notifications.enqueue_current_project_events(connection, repo)
        key = self.events(connection)[0]['event_key']
        delivery._claim_piece(connection, key, first_attempt_only=False)
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, 'G-018')
            notifications.record_process_start(connection, repo, 'worker', 'saved-start', plan_id='G-018')
        with mock.patch('loop_manager.schedule_registered_projects', return_value=()):
            loop_manager.run_registered_projects(connection, Path('/unused'), SimpleNamespace(telegram_token='', telegram_chat_id=''))
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(connection.execute('SELECT start_identity FROM notified_block_incidents').fetchone()[0], 'saved-start')
