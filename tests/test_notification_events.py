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
from loop_manager_internal.notifications.events import _BlockedPlanProjection
from loop_manager_internal.notifications import rendering
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import RecoveryRepository

from tests.support.notification import NotificationFixture


class NotificationRenderingTest(unittest.TestCase):
    def test_pr_notice_names_work_link_and_merge_wait(self):
        payload = {
            "project": "alpha", "execplan_id": "G-018",
            "execplan_title": "Preserve recovery evidence",
            "pr_url": "https://example.test/pr/18",
        }
        message = rendering._message("pr_created", payload)
        self.assertTrue(message.startswith("[alpha] G-018 PR 생성\n"))
        positions = [message.index(value) for value in (
            payload["execplan_title"], payload["pr_url"], "PR 검토·Merge 대기",
        )]
        self.assertEqual(positions, sorted(positions))

    def test_block_notice_prioritizes_confirmed_decision_and_keeps_evidence(self):
        payload = {
            "project": "alpha", "scope": "plan", "execplan_id": "G-018",
            "execplan_title": "Retain history", "reason_code": "user_decision_required",
            "cause": "Retention policy is missing", "paths": ["docs/PRODUCT.md"],
            "worker_decision": {"question": "How long should history be retained?"},
            "recovery": {
                "recovery_disposition": "user_decision", "unresolved": "retention criterion",
                "recovery_attempts": {"git_lock": 2}, "last_stable_revision": "base-sha",
            },
            "checkpoint": {
                "result": "published", "branch": "loop-manager/G-018",
                "branch_url": "https://github.com/org/repo/tree/saved-sha",
                "commit_oid": "saved-sha",
            },
        }
        original = json.dumps(payload, sort_keys=True)
        message = rendering._message("blocked", payload)
        positions = [message.index(value) for value in (
            "사용자 조치:", "작업: plan: G-018: Retain history",
            "원인: Retention policy is missing", "확인 대상:", "Question:",
            "Recovery status:", "Checkpoint:",
        )]
        self.assertEqual(positions, sorted(positions))
        for value in ("retention criterion", "git_lock: 2", "base-sha", "saved-sha"):
            self.assertIn(value, message)
        self.assertEqual(message.count("docs/PRODUCT.md"), 1)
        self.assertEqual(json.dumps(payload, sort_keys=True), original)

    def test_unconfirmed_block_does_not_turn_paths_or_retries_into_edit_instructions(self):
        payload = {
            "project": "alpha", "scope": "project", "stage": "planning",
            "reason_code": "git_lock", "cause": "lock busy",
            "paths": ["docs/plans/plan.md"],
            "recovery": {"recovery_attempts": {"git_lock": 3}},
            "checkpoint": {"result": "local_only", "reason": "remote unavailable",
                           "worktree": "/state/planning"},
        }
        message = rendering._message("blocked", payload)
        self.assertNotIn("사용자 조치:", message)
        self.assertNotIn("Files to edit", message)
        for value in ("lock busy", "git_lock: 3", "remote unavailable", "/state/planning"):
            self.assertIn(value, message)

    def test_guidance_targets_and_closed_pr_each_supply_only_the_confirmed_action(self):
        cases = (
            ({"guidance_locations": [{"user_decision": "docs/plans/plan.md:22"}]},
             "지침 제안과 User Decision"),
            ({"recovery": {"user_targets": ["docs/plans/plan.md:22"]}},
             "확인 대상의 보고된 원인"),
            ({"reason_code": "pull_request_closed"}, "같은 PR을 다시 열어"),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                payload = {
                    "project": "alpha", "scope": "plan", "execplan_id": "G-018",
                    "reason_code": "guidance_approval_required", "cause": "saved cause",
                    "paths": ["docs/plans/plan.md:22"], **fields,
                }
                message = rendering._message("blocked", payload)
                self.assertIn(expected, message)
                self.assertLess(message.index("사용자 조치:"), message.index("작업:"))
                self.assertEqual(message.count("docs/plans/plan.md:22"), 1)

    def test_persistent_failure_keeps_recorded_action_before_diagnostics(self):
        payload = {
            "project": "alpha", "stage": "PUBLISHING", "execplan_id": "G-018",
            "required_action": "Check the reported remote permission",
            "reason_code": "push_failed", "cause": "permission denied",
            "consecutive_external_runs": 3, "recovery_attempts": {"push_failed": 2},
        }
        message = rendering._message("persistent_execution_failure", payload)
        self.assertLess(message.index(payload["required_action"]), message.index(payload["cause"]))
        self.assertIn("Consecutive external runs: 3", message)
        self.assertIn("push_failed: 2", message)

    def test_wait_notice_orders_actions_stably_without_losing_other_waits_or_links(self):
        payload = {
            "project": "alpha",
            "plans": [
                {"id": "G-001", "title": "Dependency", "status": "READY",
                 "reason": "미충족 dependency G-003 (PR_OPEN)"},
                {"id": "G-002", "title": "Merge", "status": "PR_OPEN",
                 "reason": "PR Merge 대기", "pr_url": "https://example.test/pr/2"},
                {"id": "G-003", "title": "Decision", "status": "DRAFT",
                 "reason": "ExecPlan 사용자 판단 대기", "question": "Question: Choose A or B?",
                 "path": "docs/plans/draft.md", "revision": "draft-sha",
                 "url": "https://github.com/org/repo/blob/draft-sha/docs/plans/draft.md"},
                {"id": "G-004", "title": "Catalog", "status": "CATALOG_HELD",
                 "reason": "ExecPlan catalog 오류의 교정 대기",
                 "reason_code": "execplan_catalog_invalid", "evidence": "duplicate ID"},
                {"id": "G-005", "title": "Blocked choice", "status": "PLAN_BLOCKED",
                 "reason_code": "user_decision_required", "reason": "Choose retention"},
            ],
            "requirements": [
                {"id": "R-001", "reason": "Manager 게시 대기"},
                {"id": "R-002", "path": "docs/requirements/criterion.md",
                 "reason": "전체 충족 판단이 불확실하여 사용자 결정을 기다립니다: missing criterion"},
            ],
            "project_block": {"reason": "ownership uncertain"},
            "planning_error": "planning validation failed",
        }
        original = json.dumps(payload, sort_keys=True)
        message = rendering._message("work_waiting", payload)
        positions = [message.index(value) for value in (
            "사용자 확인 대기:", "- G-002", "- G-003", "- G-004", "- G-005", "- R-002",
            "기타 대기:", "- G-001", "- R-001",
        )]
        self.assertEqual(positions, sorted(positions))
        for value in (
            "https://example.test/pr/2", "Choose A or B?", "draft-sha",
            "docs/plans/draft.md", "duplicate ID", "ownership uncertain",
            "planning validation failed", "미충족 dependency G-003 (PR_OPEN)",
        ):
            self.assertIn(value, message)
        self.assertEqual(json.dumps(payload, sort_keys=True), original)

    def test_project_decision_precedes_passive_waits_without_relabeling_planning_error(self):
        payload = {
            "project": "alpha", "plans": [],
            "requirements": [{"id": "R-001", "reason": "Manager 게시 대기"}],
            "project_block": {"code": "user_decision_required", "reason": "Confirm policy"},
            "planning_error": "git lock busy; automatic retry pending",
        }
        message = rendering._message("work_waiting", payload)
        self.assertLess(message.index("PROJECT_BLOCKED"), message.index("기타 대기:"))
        self.assertLess(message.index("기타 대기:"), message.index("planning_error"))
        self.assertIn("automatic retry pending", message)
        self.assertNotIn("사용자 조치:", message)

    def test_long_block_keeps_diagnostics_and_local_checkpoint_across_pieces(self):
        payload = {
            "project": "alpha", "scope": "plan", "execplan_id": "G-018",
            "reason_code": "managed_worktree_conflict", "cause": "충돌 근거🙂\n" * 1500,
            "paths": ["src/work.py"],
            "checkpoint": {
                "result": "local_only", "reason": "unresolved rebase", "diagnostic": True,
                "original_branch": "loop-manager/G-018", "worktree": "/state/G-018",
                "complete": False, "omitted": ["unsafe-symlink"],
                "original_unpublished_reason": "remote changed",
            },
        }
        message = rendering._message("blocked", payload)
        pieces = rendering._pieces(message)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= rendering.MESSAGE_LIMIT for piece in pieces))
        self.assertEqual("".join(pieces), message)
        for value in ("unresolved rebase", "/state/G-018", "loop-manager/G-018",
                      "unsafe-symlink", "remote changed"):
            self.assertIn(value, pieces[-1])

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
    def test_targeted_worker_failure_retains_recovery_history_without_notifications(self):
        from tests.support.worker import worker_result
        connection = self.connection()
        repo = '/projects/alpha'
        with self.assertRaises(loop_manager.WorkerResultError) as raised:
            loop_manager.parse_worker_result(worker_result('Fix targeted regression',
                'Repair the current implementation.', 'test: FAIL'), {'id': 'G-018', 'title': 'Reliable notices'})
        cause = 'Worker 결과 검증 실패:\n' + str(raised.exception)
        repository = RecoveryRepository(connection)
        failure = RecoveryObservation(RecoveryReason.WORKER_CORRECTION, repo, 'G-018', 'WORKING',
            'plan-sha', 'base-sha', 'targeted-failure', 'head', cause)
        for _ in range(4):
            before = repository.external_run_snapshot(repo)
            repository.observe(failure)
            repository.observe_external_run(repo, before)
            notifications.enqueue_persistent_execution_failure_events(connection, repo)
            notifications.enqueue_worker_failure(connection, repo, 'G-018', cause, status='다음 실행에서 교정')
        self.assertEqual(self.events(connection), [])
        diagnostic = connection.execute('SELECT consecutive_runs, notification_eligible, notification_queued, detail '
            'FROM persistent_execution_failures WHERE repo=? AND subject=?', (repo, 'G-018')).fetchone()
        self.assertEqual(tuple(diagnostic[:3]), (4, 1, 0))
        self.assertEqual(diagnostic[3], cause)
        transport = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=transport), 0)
        transport.assert_not_called()

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
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            cleanup.callback(connection.close)
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
            cleanup.callback(restarted.close)
            restarted.row_factory = sqlite3.Row
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
        self.assertEqual(delivered.call_count, 1)

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
                "occurred_at": payloads["pr_created"]["occurred_at"],
                "status": "PR Merge 대기", "silent": False,
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


class TimelyProgressEventTest(NotificationFixture, unittest.TestCase):
    def test_progress_cause_redaction_preserves_identity_and_payload(self):
        from loop_manager_internal.execution.validation_feedback import redact_secrets
        connection = self.connection()
        cause = 'request rejected\n{"password": "first-private\\nsecond-private"}\nBearer bearer-private\n' + 'sk-' + 'a' * 24
        for identity in ('first-error', 'first-error', 'second-error'):
            with connection:
                notifications.enqueue_progress(connection, '/projects/alpha', 'maintenance_started', identity,
                    execplan_id='G-018', operation='Planner', cause=cause,
                    status='자동 교정 대기', silent=True)
        events = self.events(connection)
        self.assertEqual(len(events), 2)
        for row in events:
            payload = json.loads(row['payload'])
            self.assertEqual(payload['cause'], redact_secrets(cause))
            self.assertEqual(payload['execplan_id'], 'G-018')
            self.assertEqual(payload['status'], '자동 교정 대기')
            self.assertTrue(payload['silent'])
            text = notifications._message('maintenance_started', payload)
            self.assertIn('request rejected', text)
            self.assertIn(payload['occurred_at'], text)
            for secret in ('first-private', 'second-private', 'bearer-private', 'sk-' + 'a' * 24):
                self.assertNotIn(secret, row['payload'])
                self.assertNotIn(secret, text)

    def test_review_failure_projection_leaves_nonprotocol_errors_to_recovery(self):
        connection = self.connection()
        loop_manager.update_project(connection, '/projects/alpha',
                                    phase='REVIEWING', last_error='Reviewer CLI execution failed')
        self.assertEqual(self.events(connection), [])

    def test_review_failure_preserves_state_without_notification(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with connection:
                connection.execute("UPDATE projects SET phase = 'REVIEWING', last_error = 'protocol error' WHERE repo = ?", (repo,))
                notifications.enqueue_current_project_events(connection, repo)
                raise RuntimeError('interrupted')
        self.assertIsNone(connection.execute('SELECT last_error FROM projects WHERE repo = ?', (repo,)).fetchone()[0])
        for cause in ('protocol error', 'different protocol error'):
            loop_manager.update_project(connection, repo, phase='REVIEWING', last_error='Reviewer 결과 프로토콜 오류: ' + cause)
            self.assertIn(cause, connection.execute('SELECT last_error FROM projects WHERE repo = ?', (repo,)).fetchone()[0])
        self.assertEqual(self.events(connection), [])

    def test_review_protocol_failure_and_candidate_changes_do_not_notify(self):
        connection = self.connection()
        repo = '/projects/alpha'
        cause = 'Reviewer 결과 프로토콜 오류: missing ISSUE_ID'
        for token in ('candidate-1', 'candidate-1', 'candidate-2'):
            with connection:
                connection.execute('UPDATE exec_plans SET verify_token = ? WHERE repo = ?', (token, repo))
            loop_manager.update_project(connection, repo, phase='REVIEWING', last_error=cause)
        self.assertEqual(self.events(connection), [])

    def test_selection_is_silent_and_actual_start_is_durable_and_deduplicated(self):
        connection = self.connection()
        repo = '/projects/alpha'
        loop_manager.update_project(connection, repo, phase='IMPLEMENTING')
        self.assertEqual(self.events(connection), [])
        with connection:
            notifications.record_process_start(connection, repo, 'worker', 'process-1', plan_id='G-018')
            notifications.record_process_start(connection, repo, 'worker', 'process-1', plan_id='G-018')
        event = self.events(connection)[0]
        payload = json.loads(event['payload'])
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(payload['execplan_title'], 'Reliable notices')
        self.assertTrue(payload['silent'])
        self.assertEqual(payload['status'], '작업 중')
        self.assertIn(payload['occurred_at'], notifications._message('work_started', payload))
        with connection:
            notifications.record_process_start(connection, repo, 'worker', 'process-2', plan_id='G-018', resumed=True)
        self.assertEqual(len(self.events(connection)), 1)
        with connection:
            notifications.record_process_start(connection, repo, 'worker', 'process-3', plan_id='G-018')
        self.assertEqual(len(self.events(connection)), 1)

    def test_publication_and_draft_share_one_decision_notice_with_waiting(self):
        from loop_manager_internal.storage import execplan_draft_state
        connection = self.connection()
        repo = '/projects/alpha'
        plan = {'id': 'G-018', 'title': 'Reliable notices', 'sources': ['docs/PRODUCT.md'],
                'draft': True, 'draft_scope': 'scope', 'draft_question':
                'Question: Choose A or B?\nOptions: A, B\nImpact: A retains history',
                'draft_question_identity': 'question-1'}
        with connection:
            execplan_draft_state.observe(connection, repo, 'docs/plans/choice.md', plan, 'hash', 'remote-sha',
                                        remote_url='https://github.com/org/repo')
            notifications.record_planning_publication(connection, repo, [('docs/plans/choice.md', plan)], 'remote-sha')
            notifications.enqueue_current_project_events(connection, repo)
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle', active_requirements=())
        self.assertIsNotNone(snapshot)
        notifications.enqueue_waiting_reason(connection, repo, snapshot)
        events = self.events(connection)
        self.assertEqual([row['event_type'] for row in events], ['draft_decisions', 'work_waiting'])
        self.assertFalse(json.loads(events[1]['payload'])['silent'])
        self.assertEqual(json.loads(events[1]['payload'])['plans'][0]['status'], 'DRAFT')
        payload = json.loads(events[0]['payload'])
        self.assertFalse(payload['silent'])
        self.assertIn('/blob/remote-sha/', payload['plans'][0]['url'])
        self.assertEqual(payload['plans'][0]['id'], 'G-018')
        self.assertNotIn('Impact:', notifications._message('draft_decisions', payload))
        self.assertIn('사용자 판단 대기', payload['status'])
        with connection:
            execplan_draft_state.cancel(connection, repo, 'G-018', 'deleted-revision')
        notifications.mark_work_available(connection, repo)
        delivered = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=delivered), 0)
        delivered.assert_not_called()
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])

    def test_draft_notice_tracks_question_revision_and_preserves_sent_audit(self):
        from loop_manager_internal.storage import execplan_draft_state
        connection = self.connection()
        repo = '/projects/alpha'
        plan = {'id': 'G-018', 'title': 'Reliable notices', 'sources': ['docs/PRODUCT.md'],
                'draft': True, 'draft_scope': 'scope', 'draft_question':
                'Question: Choose A or B?\nFacts: known\nOptions: A, B\nImpact: history',
                'draft_question_identity': 'question-1'}
        with connection:
            execplan_draft_state.observe(connection, repo, 'docs/plans/choice.md', plan, 'hash', 'remote-sha',
                                        remote_url='https://github.com/org/repo')
            notifications.enqueue_current_project_events(connection, repo)
        first = self.events(connection)[0]
        saved = execplan_draft_state.evidence(connection, repo, 'G-018')
        saved['question'] = saved['question'].replace('A or B', 'C or D')
        saved['revision'] = 'new-decision'
        saved['identity'] = 'question-2'
        saved['url'] = saved['url'].replace('remote-sha', 'new-sha')
        with connection:
            connection.execute('UPDATE execplan_drafts SET evidence=? WHERE repo=? AND plan_id=?',
                               (json.dumps(saved), repo, 'G-018'))
        delivered = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=delivered), 0)
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])
        saved['revision'] = 'new-decision'
        with connection:
            connection.execute('UPDATE execplan_drafts SET evidence=? WHERE repo=? AND plan_id=?',
                               (json.dumps(saved), repo, 'G-018'))
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=delivered), 1)
        with connection:
            execplan_draft_state.cancel(connection, repo, 'G-018', 'resolved')
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=delivered)
        sent = [row for row in self.events(connection) if row['sent_at']]
        self.assertEqual(len(sent), 1)
        self.assertIsNone(sent[0]['canceled_at'])
        self.assertNotEqual(sent[0]['event_key'], first['event_key'])

    def test_automatic_failure_deduplicates_retry_but_separates_progress(self):
        connection = self.connection()
        repo = '/projects/alpha'
        repository = RecoveryRepository(connection)
        for marker in ('head-a', 'head-a', 'head-b'):
            with connection:
                repository.observe(RecoveryObservation(
                    RecoveryReason.FETCH_FAILED, repo, 'G-018', 'PUBLISHING',
                    'plan-sha', 'base-sha', 'fetch-failure', marker, 'fetch failed'))
        events = [row for row in self.events(connection) if row['event_type'] == 'stage_failed']
        self.assertEqual(events, [])
        self.assertIsNotNone(connection.execute('SELECT detail FROM recovery_records').fetchone())

    def test_failed_uncommitted_publication_never_leaves_success_event(self):
        connection = self.connection()
        with self.assertRaises(RuntimeError):
            with connection:
                notifications.record_planning_publication(connection, '/projects/alpha',
                    [('plan.md', {'id': 'G-018', 'title': 'Reliable notices'})], 'sha')
                raise RuntimeError('catalog apply failed')
        self.assertEqual(self.events(connection), [])

    def test_pr_registration_reports_merge_wait_with_original_time(self):
        connection = self.connection()
        loop_manager.update_project(connection, '/projects/alpha', phase='PR_OPEN', pr_url='https://example.test/pr/18')
        row = next(row for row in self.events(connection) if row['event_type'] == 'pr_created')
        payload = json.loads(row['payload'])
        self.assertFalse(payload['silent'])
        self.assertEqual(payload['status'], 'PR Merge 대기')
        self.assertIn(payload['occurred_at'], notifications._message('pr_created', payload))

    def test_planning_result_only_notifies_after_catalog_publication(self):
        from types import SimpleNamespace
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        connection = self.connection()
        state = object.__new__(_PlanningStatePorts)
        state._connection = connection
        plans = [('plan.md', {'id': 'G-018', 'title': 'Reliable notices'})]
        for result in (None, SimpleNamespace(plans=()), SimpleNamespace(plans=plans)):
            state.record_planning_result('/projects/alpha', 'input-1', result)
        self.assertEqual(self.events(connection), [])
        with connection:
            notifications.record_planning_publication(connection, '/projects/alpha', [], 'no-new-plans')
            notifications.record_planning_publication(connection, '/projects/alpha', plans, 'remote-sha')
            notifications.record_planning_publication(connection, '/projects/alpha', plans, 'remote-sha')
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(self.events(connection)[0]['event_type'], 'plans_published')

    def test_same_failure_after_resolution_is_a_separate_incident(self):
        connection = self.connection()
        repository = RecoveryRepository(connection)
        observation = RecoveryObservation(RecoveryReason.FETCH_FAILED, '/projects/alpha', 'G-018',
                                          'PUBLISHING', 'plan', 'base', 'failure', 'head', 'fetch failed')
        for _ in range(2):
            with connection:
                repository.observe(observation)
                repository.clear('/projects/alpha', 'G-018')
        self.assertEqual(self.events(connection), [])

    def test_planning_failure_owns_transaction_and_does_not_claim_publication(self):
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
        connection = self.connection()
        repository = RuntimeStateRepository(connection)
        with self.assertRaises(RuntimeError):
            with connection:
                connection.execute("BEGIN")
                repository.update_project('/projects/alpha', {
                    'phase': 'PLANNING', 'planning_attempt_fingerprint': 'input',
                    'planning_validation_error': 'invalid result',
                })
                raise RuntimeError('rollback')
        self.assertEqual(self.events(connection), [])
        loop_manager.update_project(connection, '/projects/alpha', phase='PLANNING',
                                    planning_attempt_fingerprint='input', planning_validation_error='invalid result')
        self.assertEqual(self.events(connection), [])
        self.assertEqual(connection.execute('SELECT planning_validation_error FROM projects').fetchone()[0], 'invalid result')

    def test_planner_and_reviewer_process_boundaries_do_not_notify(self):
        connection = self.connection()
        for role in ('planner', 'planner_check', 'maintenance_planner', 'reviewer'):
            with self.subTest(role=role), connection:
                notifications.record_process_start(connection, '/projects/alpha', role, role)
                notifications.record_process_start(connection, '/projects/alpha', role, role + '-resume', resumed=True)
        self.assertEqual(self.events(connection), [])


class MaintenanceProgressEventTest(NotificationFixture, unittest.TestCase):
    def test_cycle_start_empty_finish_and_rollback_keep_durable_identity(self):
        from loop_manager_internal.storage import maintenance_state
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.notifications import delivery
        connection = self.connection()
        repo = '/projects/alpha'
        repository = maintenance_state.MaintenanceRepository(connection)
        from tests.support.maintenance import request_maintenance
        request_maintenance(connection, repo)
        connection.commit()
        ports = object.__new__(_PlanningStatePorts)
        ports._connection = connection
        ports._maintenance_repository = repository
        sent = mock.Mock()
        with notifications.immediate_delivery(connection, 'token', 'chat', repo), mock.patch.object(delivery, 'send_telegram', sent):
            cycle, created = ports.begin_maintenance_assessment(repo, 20, 'a' * 40, include_general_planning=False)
            self.assertTrue(created)
            sent.assert_called_once()
            self.assertTrue(sent.call_args.kwargs['disable_notification'])
            self.assertIn('사용자의 명시적 유지보수 요청', sent.call_args.args[2])
            again, created = ports.begin_maintenance_assessment(repo, 20, 'a' * 40, include_general_planning=False)
            self.assertFalse(created)
            self.assertEqual(again, cycle)
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                with connection:
                    connection.execute('BEGIN')
                    repository.advance_success_baseline(repo, expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit='a' * 40)
                    raise RuntimeError('interrupted')
            self.assertEqual(repository.get(repo), cycle)
            self.assertEqual(len(self.events(connection)), 1)
            done = repository.advance_success_baseline(repo, expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit='a' * 40)
            ports.flush_notifications(repo)
            self.assertEqual(done.phase, maintenance_state.MaintenancePhase.IDLE)
            self.assertEqual(sent.call_count, 2)
            self.assertIn('후속 계획 없이', sent.call_args.args[2])
            with self.assertRaises(maintenance_state.MaintenanceStateError):
                repository.advance_success_baseline(repo, expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit='a' * 40)
            self.assertEqual(len(self.events(connection)), 2)

    def test_cycle_with_followup_does_not_finish_until_confirmed_merge(self):
        from loop_manager_internal.storage import maintenance_state
        connection = self.connection()
        repo = '/projects/alpha'
        repository = maintenance_state.MaintenanceRepository(connection)
        from tests.support.maintenance import request_maintenance
        request_maintenance(connection, repo)
        cycle, _ = repository.begin_assessment_if_due(repo, 20, 'a' * 40)
        repository.record_assessment_plans(repo, cycle.revision, ('G-018',))
        cycle = repository.advance_success_baseline(repo, expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit='a' * 40)
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual([row['event_type'] for row in self.events(connection)], ['maintenance_started'])
        with connection:
            connection.execute("UPDATE exec_plans SET status = 'PR_OPEN' WHERE repo = ?", (repo,))
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.finish_cycle(repo, expected=cycle, source_sha='b' * 40)
        with connection:
            connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,))
            connection.execute("INSERT INTO execplan_catalog_revisions (repo, revision_sha, catalog_fingerprint) VALUES (?, ?, ?)", (repo, 'b' * 40, 'catalog'))
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.finish_cycle(repo, expected=cycle, source_sha='c' * 40)
        done = repository.finish_cycle(repo, expected=cycle, source_sha='b' * 40)
        self.assertEqual(done.phase, maintenance_state.MaintenancePhase.IDLE)
        completed = next(row for row in self.events(connection) if row['event_type'] == 'maintenance_completed')
        payload = json.loads(completed['payload'])
        self.assertEqual(payload['plans'], [{'id': 'G-018', 'title': 'Reliable notices'}])
        self.assertTrue(payload['silent'])
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.finish_cycle(repo, expected=cycle, source_sha='b' * 40)
        self.assertEqual(len(self.events(connection)), 2)
