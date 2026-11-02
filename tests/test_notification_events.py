from tests.support.git import isolated_git_test
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote
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
from loop_manager_internal.storage.recovery_state import RecoveryRepository, begin_external_run

from tests.support.notification import NotificationFixture


class NotificationRenderingTest(unittest.TestCase):
    def test_pr_notice_names_work_link_and_merge_wait(self):
        payload = {
            "project": "alpha", "execplan_id": "G-018",
            "execplan_title": "Preserve recovery evidence",
            "pr_url": "https://example.test/pr/18",
        }
        message = rendering._message("pr_created", payload)
        self.assertTrue(message.startswith("[alpha] G-018 PR 검토 후보 게시\n"))
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
            "- G-003", "- G-004", "- G-005", "- PROJECT_BLOCKED",
            "- G-001", "- G-002", "- R-001", "- R-002", "- planning_error",
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
        self.assertLess(message.index("PROJECT_BLOCKED"), message.index("R-001"))
        self.assertLess(message.index("R-001"), message.index("planning_error"))
        self.assertIn("automatic retry pending", message)
        self.assertNotIn("사용자 조치:", message)
        self.assertNotIn("사용자 확인 대기:", message)
        self.assertIn("user_decision_required", message)

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

    def test_merged_completion_state_survives_restart_without_notice(self):
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
                self.confirm_final_review(connection, repo, 'merge-cycle')
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
            self.assertEqual(notifications.dispatch_pending(restarted, 'token', 'chat', adapter=delivered), 0)
            delivered.assert_not_called()
            completion = restarted.execute('SELECT * FROM project_completion_cycles WHERE repo = ?', (repo,)).fetchone()
            self.assertEqual(completion['cycle_key'], 'merge-cycle')
            self.assertEqual(completion['last_completed_plan_id'], 'G-018')
            self.assertEqual(completion['last_completed_title'], 'Reliable notices')
            self.assertIsNotNone(completion['completed_at'])
            self.assertEqual(self.events(restarted), [])

    def test_disabled_and_partial_configuration_do_not_create_events(self):
        connection = self.connection(enabled=False)
        self.prepare_pr_candidate(connection)
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

    def test_persistent_failure_stays_diagnostic_without_separate_alert(self):
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
        self.assertEqual(self.events(connection), [])
        diagnostic = connection.execute(
            "SELECT consecutive_runs, notification_eligible, notification_queued "
            "FROM persistent_execution_failures WHERE repo = ? AND subject = 'G-018'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(diagnostic), (3, 1, 0))

        notifications.configure_session(connection, enabled=True)
        self.assertEqual(self.events(connection), [])
        diagnostic = connection.execute(
            "SELECT consecutive_runs, notification_eligible, notification_queued "
            "FROM persistent_execution_failures WHERE repo = ? AND subject = 'G-018'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(diagnostic), (3, 1, 0))

    def test_same_plan_different_block_cause_does_not_suppress_failure_record(self):
        connection = self.connection()
        repo = "/projects/alpha"
        connection.execute(
            "UPDATE exec_plans SET current_revision_sha = 'plan-revision', "
            "active_sha = 'base-revision' WHERE repo = ? AND plan_id = 'G-018'",
            (repo,),
        )
        begin_external_run(connection, "unrelated-block-run")
        RecoveryRepository(connection).observe(RecoveryObservation(
            RecoveryReason.USER_DECISION, repo, "G-018", "READY",
            "plan-revision", "base-revision", "decision-fingerprint",
            "base-revision", "Original failure cause",
        ))
        scoped_blocking.ensure_schema(connection)
        connection.execute(
            "INSERT INTO execplan_blocks "
            "(repo, plan_id, scope, reason_code, evidence) "
            "VALUES (?, 'G-018', 'plan', 'user_decision_required', ?)",
            (repo, "Different block cause"),
        )

        notifications.suppress_persistent_failure_for_block(
            connection, repo, "G-018",
        )
        failure = connection.execute(
            "SELECT active, consecutive_runs, notification_eligible, "
            "suppressed_by_block FROM persistent_execution_failures "
            "WHERE repo = ? AND subject = 'G-018'", (repo,),
        ).fetchone()
        self.assertEqual(tuple(failure), (1, 1, 1, 0))

    def test_pr_and_blocked_payloads_are_complete_and_deduplicated(self):
        connection = self.connection()
        repo = "/projects/alpha"
        candidate = self.prepare_pr_candidate(connection)
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
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
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)

        events = self.events(connection)
        self.assertEqual(len(events), 2)
        payloads = {
            row["event_type"]: json.loads(row["payload"]) for row in events
        }
        self.assertEqual(
            {key: payloads["pr_created"][key] for key in (
                "project", "execplan_id", "execplan_title", "pr_url",
                "candidate_identity", "candidate_head_oid",
                "candidate_publication_token", "user_action",
                "status", "silent",
            )},
            {
                "project": "alpha",
                "execplan_id": "G-018",
                "execplan_title": "Reliable notices",
                **candidate,
                "status": "PR 검토·Merge 대기", "silent": False,
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
            set(),
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
        self.assertEqual(self.events(connection), [])
        with connection:
            scoped_blocking.record_plan_block(
                connection,
                repo,
                "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "User decision required",
                "decision-fingerprint",
            )
            self.confirm_worker_question(connection)
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
                self.confirm_worker_question(connection)
                notifications.enqueue_current_project_events(connection, repo)
            self.assertEqual(self.events(connection), [])
        repository.finish_published(repository.get(repo, "G-018"), oid)
        repository.finish_published(repository.get(repo, "G-018"), oid)
        with connection:
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(self.events(connection)[0]["event_type"], "blocked")
        self.assertEqual(repository.pending(repo), ())
        payload = json.loads(self.events(connection)[0]["payload"])
        self.assertEqual(payload["checkpoint"]["result"], "published")
        self.assertEqual(payload["checkpoint"]["commit_oid"], oid)
        self.assertEqual(
            payload["checkpoint"]["branch_url"],
            f"https://github.com/example/alpha/tree/{oid}",
        )
        failure = connection.execute(
            "SELECT active, consecutive_runs, notification_eligible, "
            "notification_queued, suppressed_by_block FROM "
            "persistent_execution_failures WHERE repo = ? AND subject = 'G-018'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(failure), (0, 1, 1, 0, 1))


    def test_planning_blocked_payload_lists_basis_documents(self):
        connection = self.connection()
        loop_manager.update_project(
            connection,
            "/projects/alpha",
            phase="PLANNING",
            plan_path=None,
            planning_error="Planner output remained invalid",
        )
        self.assertEqual(self.events(connection), [])
        self.assertEqual(connection.execute('SELECT planning_error FROM projects').fetchone()[0], 'Planner output remained invalid')


@isolated_git_test
class TimelyProgressEventTest(NotificationFixture, unittest.TestCase):
    def test_progress_kinds_never_create_even_silent_outbox_rows(self):
        connection = self.connection()
        for kind in ('planning_result', 'work_started', 'resumed',
                     'stage_failed', 'maintenance_started', 'maintenance_completed',
                     'project_completed', 'work_waiting'):
            for silent in (False, True):
                with self.subTest(kind=kind, silent=silent), connection:
                    notifications.enqueue_progress(connection, '/projects/alpha', kind,
                        kind + str(silent), execplan_id='G-018', silent=silent)
        self.assertEqual(self.events(connection), [])
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'READY')

    def test_progress_cause_redaction_preserves_identity_and_payload(self):
        from loop_manager_internal.execution.validation_feedback import redact_secrets
        connection = self.connection()
        from loop_manager_internal.storage import execplan_draft_state
        execplan_draft_state.ensure_schema(connection)
        path = 'docs/plans/[G-018] Reliable notices.md'
        url = 'https://github.com/org/repo/blob/draft-sha/' + quote(path, safe='/')
        connection.execute("UPDATE projects SET origin='https://github.com/org/repo' WHERE repo='/projects/alpha'")
        connection.execute("UPDATE exec_plans SET path=?, current_revision_sha='draft-sha' "
                           "WHERE repo='/projects/alpha' AND plan_id='G-018'", (path,))
        connection.execute("INSERT INTO execplan_drafts VALUES (?, 'G-018', 1, ?)",
            ('/projects/alpha', json.dumps({'pending': True, 'question': 'Which retention policy?',
                'identity': 'retention-question', 'revision': 'question-revision', 'path': path, 'url': url})))
        cause = 'request rejected\n{"password": "first-private\\nsecond-private"}\nBearer bearer-private\n' + 'sk-' + 'a' * 24
        for identity in ('first-error', 'first-error', 'second-error'):
            with connection:
                notifications.enqueue_progress(connection, '/projects/alpha', 'draft_decision', identity,
                    execplan_id='G-018', question='Which retention policy?', url=url,
                    decision_identity='retention-question', decision_revision='question-revision',
                    source_path=path, operation='Planner', cause=cause, status='자동 교정 대기', silent=True)
        events = self.events(connection)
        self.assertEqual(len(events), 2)
        for row in events:
            payload = json.loads(row['payload'])
            self.assertEqual(payload['cause'], redact_secrets(cause))
            self.assertEqual(payload['execplan_id'], 'G-018')
            self.assertEqual(payload['status'], '자동 교정 대기')
            self.assertTrue(payload['silent'])
            text = notifications._message('draft_decision', payload)
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
        self.assertEqual(self.events(connection), [])
        with connection:
            notifications.record_process_start(connection, repo, 'worker', 'process-2', plan_id='G-018', resumed=True)
            notifications.record_process_start(connection, repo, 'worker', 'process-3', plan_id='G-018')
        self.assertEqual(self.events(connection), [])
        self.assertEqual(connection.execute('SELECT phase FROM projects').fetchone()[0], 'IMPLEMENTING')

    def test_publication_and_draft_share_one_decision_notice_with_waiting(self):
        from loop_manager_internal.storage import execplan_draft_state
        connection = self.connection()
        repo = '/projects/alpha'
        plan = {'id': 'G-018', 'title': 'Reliable notices', 'sources': ['docs/PRODUCT.md'],
                'draft': True, 'draft_scope': 'scope', 'draft_question':
                'Question: Choose A or B?\nOptions: A, B\nImpact: A retains history',
                'draft_question_identity': 'question-1'}
        plan_path = 'docs/plans/[G-018] Reliable notices.md'
        with connection:
            connection.execute("UPDATE projects SET origin='https://github.com/org/repo' WHERE repo=?", (repo,))
            connection.execute("UPDATE exec_plans SET path=?, title=?, current_revision_sha='remote-sha' "
                               "WHERE repo=? AND plan_id='G-018'", (plan_path, plan['title'], repo))
            execplan_draft_state.observe(connection, repo, plan_path, plan, 'hash', 'remote-sha',
                                        remote_url='https://github.com/org/repo')
            notifications.record_planning_publication(connection, repo, [(plan_path, plan)], 'remote-sha')
            notifications.enqueue_current_project_events(connection, repo)
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle', active_requirements=())
        self.assertIsNotNone(snapshot)
        notifications.enqueue_waiting_reason(connection, repo, snapshot)
        events = self.events(connection)
        self.assertEqual([row['event_type'] for row in events], ['draft_decisions'])
        self.assertEqual(connection.execute('SELECT generation FROM work_reason_episodes').fetchone()[0], 1)
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
        plan_path = 'docs/plans/[G-018] Reliable notices.md'
        with connection:
            connection.execute("UPDATE projects SET origin='https://github.com/org/repo' WHERE repo=?", (repo,))
            connection.execute("UPDATE exec_plans SET path=?, title=?, current_revision_sha='remote-sha' "
                               "WHERE repo=? AND plan_id='G-018'", (plan_path, plan['title'], repo))
            execplan_draft_state.observe(connection, repo, plan_path, plan, 'hash', 'remote-sha',
                                        remote_url='https://github.com/org/repo')
            notifications.enqueue_current_project_events(connection, repo)
        first = self.events(connection)[0]
        saved = execplan_draft_state.evidence(connection, repo, 'G-018')
        saved['question'] = saved['question'].replace('A or B', 'C or D')
        saved['revision'] = 'new-decision'
        saved['identity'] = 'question-2'
        saved['url'] = saved['url'].replace('remote-sha', 'new-sha')
        with connection:
            connection.execute("UPDATE exec_plans SET current_revision_sha='new-sha' "
                               "WHERE repo=? AND plan_id='G-018'", (repo,))
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
        self.prepare_pr_candidate(connection)
        with connection:
            notifications.enqueue_current_project_events(connection, '/projects/alpha')
        row = next(row for row in self.events(connection) if row['event_type'] == 'pr_created')
        payload = json.loads(row['payload'])
        self.assertFalse(payload['silent'])
        self.assertEqual(payload['status'], 'PR 검토·Merge 대기')
        self.assertIn(payload['occurred_at'], notifications._message('pr_created', payload))

    def test_pr_candidate_event_reuses_identity_after_rebase_tokens_are_renewed(self):
        connection = self.connection()
        repo = '/projects/alpha'
        first = self.prepare_pr_candidate(connection)
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
            notifications.enqueue_current_project_events(connection, repo)
        events = [row for row in self.events(connection) if row['event_type'] == 'pr_created']
        self.assertEqual(len(events), 1)
        original = dict(events[0])
        first_key = original['event_key']
        self.assertEqual(json.loads(original['payload'])['candidate_identity'], first['candidate_identity'])

        with connection:
            connection.execute(
                "UPDATE projects SET pr_head_oid=? WHERE repo=?",
                ('c' * 40, repo),
            )
            connection.execute(
                "UPDATE exec_plans SET pr_head_oid=? WHERE repo=? AND plan_id='G-018'",
                ('c' * 40, repo),
            )
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(len([row for row in self.events(connection) if row['event_type'] == 'pr_created']), 1)
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])
        canceled = dict(self.events(connection)[0])

        second = self.prepare_pr_candidate(connection, head='c' * 40)
        self.assertNotEqual(first['candidate_publication_token'], second['candidate_publication_token'])
        self.assertEqual(first['candidate_identity'], second['candidate_identity'])
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
            notifications.enqueue_current_project_events(connection, repo)
        events = {row['event_key']: row for row in self.events(connection)
                  if row['event_type'] == 'pr_created'}
        self.assertEqual(len(events), 2)
        self.assertEqual(dict(events[first_key]), canceled)
        current = next(row for key, row in events.items() if key != first_key)
        self.assertEqual(current['event_key'], f'{first_key}:v1')
        self.assertIsNone(current['canceled_at'])
        self.assertEqual(json.loads(current['payload'])['candidate_identity'], second['candidate_identity'])
        self.assertEqual(json.loads(current['payload'])['candidate_head_oid'], second['candidate_head_oid'])
        self.assertEqual(json.loads(current['payload'])['candidate_publication_token'],
                         second['candidate_publication_token'])
        self.assertEqual(json.loads(current['payload'])['occurred_at'],
                         json.loads(original['payload'])['occurred_at'])
        self.assertEqual(json.loads(current['payload'])['continued_from'], first_key)

    def test_real_rebase_keeps_review_patch_identity_and_does_not_repeat_pr_notice(self):
        from types import SimpleNamespace
        from loop_manager_internal.execution.execution_review_handler import ReviewHandler
        from loop_manager_internal.execution.execution_revision import DefinitionSnapshot

        project_repo = '/projects/alpha'
        with tempfile.TemporaryDirectory() as directory:
            repo_path = Path(directory) / 'repo'
            repo_path.mkdir()

            def git(*args, input=None):
                if args and isinstance(args[0], Path):
                    path, *args = args
                else:
                    path = repo_path
                result = subprocess.run(
                    ['git', '-C', str(path), *args], capture_output=True,
                    text=True, input=input, check=False,
                )
                if result.returncode:
                    raise RuntimeError(result.stderr)
                return result

            for args in (('init', '-b', 'main'), ('config', 'user.email', 'tests@example.test'),
                         ('config', 'user.name', 'Tests')):
                subprocess.run(['git', '-C', str(repo_path), *args], check=True,
                               capture_output=True, text=True)
            source = repo_path / 'source.txt'
            source.write_text('zero\ntarget\nend\n', encoding='utf-8')
            snippet = repo_path / 'snippet.py'
            snippet.write_text('value = "base"\n', encoding='utf-8')
            branches = repo_path / 'branches.py'
            branches.write_text(
                'def f():\n    before()\n    value = 0\n    after()\n\n'
                'def g():\n    before()\n    value = 0\n    after()\n', encoding='utf-8',
            )
            latin = repo_path / 'latin.py'
            latin.write_bytes(b'value = "base"\n')
            git('add', 'source.txt', 'snippet.py', 'branches.py', 'latin.py')
            git('commit', '-m', 'base')
            dependency = repo_path / 'dependency'
            dependency.mkdir()
            dependency_oid = git('rev-parse', 'HEAD').stdout.strip()
            git('update-index', '--add', '--cacheinfo', f'160000,{dependency_oid},dependency')
            git('commit', '-m', 'base gitlink')
            base_before = git('rev-parse', 'HEAD').stdout.strip()
            git('checkout', '-b', 'feature')
            source.write_text('zero\nreviewed change\nend\n', encoding='utf-8')
            git('commit', '-am', 'reviewed change')
            head_before = git('rev-parse', 'HEAD').stdout.strip()

            definition = DefinitionSnapshot(
                revision_sha='definition-revision',
                path='docs/plans/[G-018] Reliable notices.md',
                title='Reliable notices', sources=('docs/PRODUCT.md',),
                depends_on=(), complexity='medium', risk='medium', priority='high',
                body_hash='body-hash', source_fingerprint='source-fingerprint',
                archive_candidates=(),
            )
            handler = object.__new__(ReviewHandler)
            handler.ports = SimpleNamespace(
                snapshot=lambda: SimpleNamespace(_definition_snapshot=definition),
                git=git,
                git_bytes=lambda path, *args: subprocess.run(
                    ['git', '-C', str(path), '-c', 'diff.autoRefreshIndex=false', *args],
                    capture_output=True, check=True,
                ).stdout,
                identity=SimpleNamespace(repo=project_repo),
            )
            context = SimpleNamespace(
                worktree=repo_path, plan=SimpleNamespace(plan_id='G-018'),
            )
            before = handler._pr_review_candidate_proof(
                context, {'validated_base_sha': base_before}, head_before,
            )

            git('checkout', 'main')
            source.write_text('upstream\nzero\ntarget\nend\n', encoding='utf-8')
            git('commit', '-am', 'base advances')
            base_after = git('rev-parse', 'HEAD').stdout.strip()
            git('checkout', 'feature')
            git('rebase', 'main')
            head_after = git('rev-parse', 'HEAD').stdout.strip()
            after = handler._pr_review_candidate_proof(
                context, {'validated_base_sha': base_after}, head_after,
            )

            self.assertNotEqual(before[0], after[0])
            self.assertEqual(before[1], after[1])

            def candidate_with_content(branch, path, content):
                git('checkout', 'main')
                git('checkout', '-b', branch)
                target = repo_path / path
                if path == 'dependency':
                    git('update-index', '--cacheinfo', f'160000,{content},dependency')
                elif isinstance(content, bytes):
                    target.write_bytes(content)
                else:
                    target.write_text(content, encoding='utf-8')
                git('commit', '-m' if path == 'dependency' else '-am', branch)
                head = git('rev-parse', 'HEAD').stdout.strip()
                proof = handler._pr_review_candidate_proof(
                    context, {'validated_base_sha': base_after}, head,
                )
                return proof, head

            spaced, spaced_head = candidate_with_content(
                'candidate-spaced', 'snippet.py', 'value = "a b"\n',
            )
            compact, compact_head = candidate_with_content(
                'candidate-compact', 'snippet.py', 'value = "ab"\n',
            )
            self.assertNotEqual(spaced[1], compact[1])
            one_space_indent, _ = candidate_with_content(
                'candidate-one-space-indent', 'snippet.py', 'def value():\n return "x"\n',
            )
            two_space_indent, _ = candidate_with_content(
                'candidate-two-space-indent', 'snippet.py', 'def value():\n  return "x"\n',
            )
            self.assertNotEqual(one_space_indent[1], two_space_indent[1])
            if_branch, if_branch_head = candidate_with_content(
                'candidate-first-function', 'branches.py',
                'def f():\n    before()\n    value = 1\n    after()\n\n'
                'def g():\n    before()\n    value = 0\n    after()\n',
            )
            else_branch, else_branch_head = candidate_with_content(
                'candidate-second-function', 'branches.py',
                'def f():\n    before()\n    value = 0\n    after()\n\n'
                'def g():\n    before()\n    value = 1\n    after()\n',
            )
            self.assertNotEqual(if_branch[1], else_branch[1])
            latin_one, latin_one_head = candidate_with_content(
                'candidate-latin-one', 'latin.py', b'value = "\xe9"\n',
            )
            latin_two, latin_two_head = candidate_with_content(
                'candidate-latin-two', 'latin.py', b'value = "\xea"\n',
            )
            self.assertNotEqual(latin_one[1], latin_two[1])

            gitlink_one, gitlink_one_head = candidate_with_content(
                'candidate-gitlink-one', 'dependency', head_before,
            )
            gitlink_two, gitlink_two_head = candidate_with_content(
                'candidate-gitlink-two', 'dependency', head_after,
            )
            self.assertIsNotNone(gitlink_one)
            self.assertIsNotNone(gitlink_two)
            self.assertNotEqual(gitlink_one[1], gitlink_two[1])

            # A repeated block inserted before g must not rename g's candidate.
            git('checkout', 'main')
            branches.write_text(
                'def h():\n    before()\n    value = 0\n    after()\n\n'
                + branches.read_text(encoding='utf-8'), encoding='utf-8',
            )
            git('commit', '-am', 'upstream adds identical context before g')
            repeated_base = git('rev-parse', 'HEAD').stdout.strip()
            git('checkout', 'candidate-second-function')
            git('rebase', 'main')
            repeated_head = git('rev-parse', 'HEAD').stdout.strip()
            repeated_proof = handler._pr_review_candidate_proof(
                context, {'validated_base_sha': repeated_base}, repeated_head,
            )
            self.assertNotEqual(else_branch[0], repeated_proof[0])
            self.assertEqual(else_branch[1], repeated_proof[1])

            git('checkout', 'candidate-gitlink-two')
            git('rebase', 'main')
            rebased_gitlink = handler._pr_review_candidate_proof(
                context, {'validated_base_sha': repeated_base},
                git('rev-parse', 'HEAD').stdout.strip(),
            )
            self.assertEqual(gitlink_two[1], rebased_gitlink[1])

            connection = self.connection()
            first = self.prepare_pr_candidate(
                connection, repo=project_repo, base=base_before, head=head_before,
                review_input_identity=before[1],
            )
            self.assertEqual(first['candidate_publication_token'], before[0])
            with connection:
                notifications.enqueue_current_project_events(connection, project_repo)
            sent = mock.Mock()
            self.assertEqual(notifications.dispatch_pending(
                connection, 'token', 'chat', adapter=sent,
            ), 1)
            rebased_candidate = self.prepare_pr_candidate(
                connection, repo=project_repo, base=base_after, head=head_after,
                review_input_identity=after[1],
            )
            self.assertNotEqual(first['candidate_publication_token'],
                                rebased_candidate['candidate_publication_token'])
            self.assertEqual(rebased_candidate['candidate_publication_token'], after[0])
            self.assertEqual(rebased_candidate['candidate_identity'], before[1])
            with connection:
                notifications.enqueue_current_project_events(connection, project_repo)
            self.assertEqual(notifications.dispatch_pending(
                connection, 'token', 'chat', adapter=sent,
            ), 0)
            self.assertEqual(len([row for row in self.events(connection)
                                  if row['event_type'] == 'pr_created']), 1)
            sent.assert_called_once()

            spaced_candidate = self.prepare_pr_candidate(
                connection, repo=project_repo, base=base_after, head=spaced_head,
                review_input_identity=spaced[1],
            )
            self.assertEqual(spaced_candidate['candidate_publication_token'], spaced[0])
            with connection:
                notifications.enqueue_current_project_events(connection, project_repo)
            self.assertEqual(notifications.dispatch_pending(
                connection, 'token', 'chat', adapter=sent,
            ), 1)

            compact_candidate = self.prepare_pr_candidate(
                connection, repo=project_repo, base=base_after, head=compact_head,
                review_input_identity=compact[1],
            )
            self.assertEqual(compact_candidate['candidate_publication_token'], compact[0])
            with connection:
                notifications.enqueue_current_project_events(connection, project_repo)
            self.assertEqual(notifications.dispatch_pending(
                connection, 'token', 'chat', adapter=sent,
            ), 1)
            self.assertEqual(len([row for row in self.events(connection)
                                  if row['event_type'] == 'pr_created']), 3)
            self.assertEqual(sent.call_count, 3)

            for branch, proof, head in (
                ('first repeated context', if_branch, if_branch_head),
                ('second repeated context', else_branch, else_branch_head),
                ('Latin-1 byte one', latin_one, latin_one_head),
                ('Latin-1 byte two', latin_two, latin_two_head),
                ('gitlink one', gitlink_one, gitlink_one_head),
                ('gitlink two', gitlink_two, gitlink_two_head),
            ):
                with self.subTest(candidate=branch):
                    candidate = self.prepare_pr_candidate(
                        connection, repo=project_repo, base=base_after, head=head,
                        review_input_identity=proof[1],
                    )
                    self.assertEqual(candidate['candidate_identity'], proof[1])
                    with connection:
                        notifications.enqueue_current_project_events(connection, project_repo)
                    self.assertEqual(notifications.dispatch_pending(
                        connection, 'token', 'chat', adapter=sent,
                    ), 1)
            self.assertEqual(len([row for row in self.events(connection)
                                  if row['event_type'] == 'pr_created']), 9)
            self.assertEqual(sent.call_count, 9)
            self.prepare_pr_candidate(
                connection, repo=project_repo, base=repeated_base, head=repeated_head,
                review_input_identity=repeated_proof[1],
            )
            with connection:
                notifications.enqueue_current_project_events(connection, project_repo)
            self.assertEqual(notifications.dispatch_pending(
                connection, 'token', 'chat', adapter=sent,
            ), 0)
            self.assertEqual(sent.call_count, 9)


    def test_unregistered_planning_results_do_not_create_publication_notices(self):
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
        self.assertEqual(self.events(connection), [])

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
            sent.assert_not_called()
            self.assertIsNotNone(cycle.cycle_id)
            again, created = ports.begin_maintenance_assessment(repo, 20, 'a' * 40, include_general_planning=False)
            self.assertFalse(created)
            self.assertEqual(again, cycle)
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                with connection:
                    connection.execute('BEGIN')
                    repository.advance_success_baseline(repo, expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit='a' * 40)
                    raise RuntimeError('interrupted')
            self.assertEqual(repository.get(repo), cycle)
            self.assertEqual(self.events(connection), [])
            done = repository.advance_success_baseline(repo, expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit='a' * 40)
            ports.flush_notifications(repo)
            self.assertEqual(done.phase, maintenance_state.MaintenancePhase.IDLE)
            sent.assert_not_called()
            self.assertEqual(done.last_cycle_commit, 'a' * 40)
            with self.assertRaises(maintenance_state.MaintenanceStateError):
                repository.advance_success_baseline(repo, expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit='a' * 40)
            self.assertEqual(self.events(connection), [])

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
        self.assertEqual(self.events(connection), [])
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
        self.assertEqual(done.last_cycle_commit, 'b' * 40)
        self.assertEqual(repository.get(repo), done)
        self.assertEqual(self.events(connection), [])
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.finish_cycle(repo, expected=cycle, source_sha='b' * 40)
        self.assertEqual(self.events(connection), [])
