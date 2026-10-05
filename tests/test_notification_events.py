import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.config.managed_config import ManagedConfigError, load_managed_config
from loop_manager_internal.notifications import notifications
from loop_manager_internal.notifications.events import _BlockedPlanProjection
from loop_manager_internal.notifications import rendering
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import RecoveryRepository

from support.notification import NotificationFixture


class NotificationRenderingTest(unittest.TestCase):
    def test_checkpoint_links_accept_only_supported_github_origins(self):
        oid = "a" * 40
        expected = f"https://github.com/org/repo/tree/{oid}"
        for origin in (
            "org/repo", "https://github.com/org/repo.git",
            "git@github.com:org/repo.git", "ssh://git@github.com/org/repo.git",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(rendering._checkpoint_link(origin, oid), expected)
        self.assertIsNone(rendering._checkpoint_link("https://other.test/org/repo", oid))


class NotificationEventTest(NotificationFixture, unittest.TestCase):
    def test_blocked_projection_reads_aliases_in_any_column_order(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        fields = {
            "plan_id": "G-018", "plan_title": "Named projection",
            "plan_path": "docs/plans/plan.md", "plan_sources": "[]",
            "reason_code": "user_decision_required", "evidence": "cause",
            "checkpoint_result": "local_only", "checkpoint_remote_oid": None,
            "checkpoint_unpublished_reason": "unpublished",
            "checkpoint_branch": "branch", "origin": "owner/repo",
            "checkpoint_inspected_remote_oid": "inspected",
            "checkpoint_inspection_reason": "inspection reason",
        }
        expected = None
        for names in (list(fields), list(reversed(fields))):
            row = connection.execute(
                "SELECT " + ", ".join(f"? AS {name}" for name in names),
                [fields[name] for name in names],
            ).fetchone()
            projection = _BlockedPlanProjection.from_row(row)
            if expected is None:
                expected = projection
            self.assertEqual(projection, expected)
        self.assertEqual(expected.checkpoint_unpublished_reason, "unpublished")
        self.assertEqual(expected.checkpoint_inspected_remote_oid, "inspected")
        self.assertIsNone(expected.checkpoint_remote_oid)

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

    def test_blocked_plan_projection_keeps_guidance_and_checkpoint_fields_named(self):
        connection = self.connection()
        repo = "/projects/alpha"
        path = "docs/plans/[G-018] Named projection.md"
        with connection:
            connection.execute(
                "UPDATE exec_plans SET path = ?, title = ?, sources = ? "
                "WHERE repo = ? AND plan_id = 'G-018'",
                (path, "Named projection", '["docs/PRODUCT.md"]', repo),
            )
            connection.execute(
                "UPDATE projects SET origin = ? WHERE repo = ?",
                ("example/alpha", repo),
            )
            connection.execute(
                "INSERT INTO guidance_approvals (repo, plan_id, proposal_id, "
                "proposal_hash, target_path, scope, proposal_patch, "
                "proposal_patch_hash, proposal_line, decision_line, batch_id, "
                "sequence, baseline_content, baseline_mode) "
                "VALUES (?, 'G-018', 'guidance-a', 'hash', 'AGENTS.md', "
                "'common', 'patch', 'patch-hash', 15, 22, 'batch', 0, ?, 420)",
                (repo, b""),
            )
            scoped_blocking.record_plan_block(
                connection,
                repo,
                "G-018",
                scoped_blocking.BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
                "User Decision pending",
                "guidance-fingerprint",
            )
            checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(
                connection
            )
            request = checkpoints.request(
                repo=repo,
                plan_id="G-018",
                reason_code="guidance_approval_required",
                evidence="User Decision pending",
                branch="loop-manager/G-018",
                worktree="/state/G-018",
                expected_remote_oid=None,
                rebase_recovery=False,
            )
            checkpoints.finish_published(request, "a" * 40)

        event = next(
            row for row in self.events(connection) if row["event_type"] == "blocked"
        )
        payload = json.loads(event["payload"])
        self.assertEqual(payload["execplan_id"], "G-018")
        self.assertEqual(payload["execplan_title"], "Named projection")
        self.assertEqual(payload["cause"], "User Decision pending")
        self.assertEqual(
            payload["guidance_locations"],
            [{
                "proposal_id": "guidance-a",
                "proposal": f"{path}:15",
                "user_decision": f"{path}:22",
            }],
        )
        self.assertEqual(
            payload["checkpoint"],
            {
                "result": "published",
                "branch": "loop-manager/G-018",
                "branch_url": f"https://github.com/example/alpha/tree/{'a' * 40}",
                "commit_oid": "a" * 40,
            },
        )

    def test_merged_completion_outbox_replays_once_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            repo = "/projects/alpha"
            snapshot = work_exhaustion.WorkReasonSnapshot("merge-cycle", (), (), None, None)
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,),
                )
                connection.execute(
                    "UPDATE projects SET phase = NULL, planning_error = NULL, "
                    "planning_fingerprint = ? WHERE repo = ?",
                    ("merge-cycle", repo),
                )
                notifications.record_merged_plan(
                    connection, repo, "G-018", "Reliable notices",
                )
                self.assertTrue(notifications.enqueue_completion_if_ready(
                    connection, repo, snapshot,
                ))
            connection.close()

            restarted = sqlite3.connect(database)
            restarted.row_factory = sqlite3.Row
            self.addCleanup(restarted.close)
            notifications.configure_session(restarted, enabled=True)
            self.assertTrue(notifications.enqueue_completion_if_ready(
                restarted, repo, snapshot,
            ))
            delivered = mock.Mock()
            self.assertEqual(notifications.dispatch_pending(
                restarted, "token", "chat", adapter=delivered,
            ), 1)
            delivered.assert_called_once()
            self.assertEqual(restarted.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE repo = ? AND event_type = 'project_completed'", (repo,),
            ).fetchone()[0], 1)
            self.assertEqual(notifications.dispatch_pending(
                restarted, "token", "chat", adapter=delivered,
            ), 0)

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
