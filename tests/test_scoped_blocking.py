import json
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
import blocked_checkpoint_state
import maintenance_state
import notifications
import project_registry
import scoped_blocking
from scoped_blocking import BlockReasonCode, BlockScope


class ScopedBlockingTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)
        notifications.ensure_schema(self.connection)
        self.repo = "/projects/scoped"
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (self.repo,),
        )
        self.connection.commit()

    def plan(
        self,
        plan_id,
        status="READY",
        *,
        priority="normal",
        dependencies=(),
        branch=None,
        pr_url=None,
    ):
        path = f"docs/plans/[{plan_id}] Work.md"
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, branch, pr_url, workspace_owned
            ) VALUES (?, ?, ?, ?, '[]', ?, ?, 'hash', ?, ?, ?, 1)
            """,
            (
                self.repo,
                plan_id,
                path,
                f"Work {plan_id}",
                json.dumps(list(dependencies)),
                priority,
                status,
                branch,
                pr_url,
            ),
        )
        self.connection.commit()
        return self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (self.repo, plan_id),
        ).fetchone()

    def test_initialized_worker_queries_do_not_reenter_schema_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            parent = loop_manager.connect_database(state, create=True)
            self.addCleanup(parent.close)
            parent.execute("BEGIN IMMEDIATE")

            def query_worker(repo):
                connection = loop_manager.connect_database(state, run_migrations=False)
                try:
                    connection.execute("PRAGMA busy_timeout = 100")
                    statements = []
                    connection.set_trace_callback(statements.append)
                    self.assertIsNone(loop_manager.active_execplan(connection, repo))
                    self.assertEqual(loop_manager.plan_blocked_execplans(connection, repo), ())
                    self.assertEqual(loop_manager.merge_wait_execplans(connection, repo), ())
                    self.assertEqual(loop_manager.compact_execplan_catalog(connection, repo), "[]")
                    self.assertIsNone(loop_manager.catalog_schedule_decision(connection, repo).active)
                    self.assertFalse(any(
                        sql.lstrip().upper().startswith(("CREATE", "DROP", "ALTER"))
                        and any(name in sql.lower() for name in (
                            "exec_plans", "one_active_execplan_per_project",
                            "execplan_blocks", "project_blocks", "guidance_approvals",
                        ))
                        for sql in statements
                    ), statements)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(query_worker, ("repo-a", "repo-b")))
            parent.rollback()

    def test_reopened_conflict_precedes_independent_ready(self):
        plan = self.plan("G-001", "PR_OPEN", branch="loop-manager/G-001")
        self.plan("G-002", priority="critical")
        loop_manager._block_closed_pull_request(self.connection, self.repo, plan, "https://pr/1", "a" * 40)
        opened = {"state": "OPEN", "url": "https://pr/1", "headRefOid": "a" * 40, "mergeable": "CONFLICTING"}

        def start_recovery(connection, _root, _state, repo, project, _pr):
            self.assertIn("[G-001]", project["plan_path"])
            self.assertEqual(loop_manager.project_execplan(connection, repo)["status"], "PR_OPEN")
            loop_manager.update_project(connection, repo, phase="WORKING", pr_conflict_recovery=1)

        with mock.patch("loop_manager.pr_for_branch", return_value=opened), mock.patch(
            "loop_manager.reconcile_existing_pull_request", side_effect=start_recovery
        ) as recover:
            result = loop_manager.synchronize_waiting_pull_requests(self.connection, Path(self.repo), Path("/state"), self.repo)
        recover.assert_called_once()
        self.assertTrue(result.conflict_started)
        decision = loop_manager.catalog_schedule_decision(self.connection, self.repo)
        self.assertEqual(decision.active["plan_id"], "G-001")
        self.assertIsNone(decision.runnable_ready)

    def test_cleanup_project_block_stops_later_merge_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            for plan_id in ("G-001", "G-002"):
                self.plan(plan_id, "PR_OPEN", branch=f"loop-manager/{plan_id}")
            self.connection.execute("UPDATE exec_plans SET workspace_owned = 0, worktree = ?", (directory,))
            self.connection.commit()
            merged = {"state": "MERGED", "url": "https://pr/1", "headRefOid": "a" * 40}
            with mock.patch("loop_manager.pr_for_branch", return_value=merged) as read, mock.patch("loop_manager.git") as git, mock.patch(
                "loop_manager.complete_merged_execplan_worktree", wraps=loop_manager.complete_merged_execplan_worktree
            ) as cleanup:
                loop_manager.synchronize_waiting_pull_requests(self.connection, Path(self.repo), Path(directory), self.repo)
            self.assertEqual(read.call_count, 2)
            cleanup.assert_called_once()
            git.assert_called_once_with(Path(self.repo), "fetch", "origin")
            self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
            statuses = dict(self.connection.execute("SELECT plan_id, status FROM exec_plans"))
            self.assertEqual(statuses, {"G-001": "MERGED", "G-002": "PR_OPEN"})

    def test_reason_mapping_and_atomic_plan_project_records(self):
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
                "same verify fingerprint",
                "verify-fingerprint",
            )

        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        block = self.connection.execute(
            "SELECT * FROM execplan_blocks WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(
            (block["scope"], block["reason_code"], block["release_fingerprint"]),
            ("plan", "verify_retry_exhausted", "verify-fingerprint"),
        )

        with self.assertRaises(ValueError):
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.PULL_REQUEST_CLOSED,
                "wrong scope",
                None,
                plan_id="G-001",
                previous_phase=None,
            )

        self.plan("G-002", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.SHARED_REF_MUTATION,
                "refs/heads/main changed",
                "repository-fingerprint",
                plan_id="G-002",
                previous_phase="WORKING",
            )
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        active = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
        self.assertEqual(active["status"], "IMPLEMENTING")
        scoped_blocking.validate_records(self.connection, self.repo)

        expected_plan_reasons = {
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
            BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
            BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
            BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
            BlockReasonCode.USER_DECISION_REQUIRED,
            BlockReasonCode.PULL_REQUEST_CLOSED,
            BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
            BlockReasonCode.PLAN_INPUT_INVALID,
            BlockReasonCode.WORKER_OUTPUT_INVALID,
        }
        self.assertEqual(
            {
                reason
                for reason in BlockReasonCode
                if scoped_blocking.reason_scope(reason) is BlockScope.PLAN
            },
            expected_plan_reasons,
        )

    def test_waiting_rows_do_not_occupy_the_single_mutation_slot(self):
        self.plan("G-001", "PR_OPEN", branch="loop-manager/G-001")
        self.plan("G-002", "PR_OPEN", branch="loop-manager/G-002")
        self.plan("G-003", "IMPLEMENTING")
        with self.assertRaises(sqlite3.IntegrityError):
            self.plan("G-004", "VERIFYING")

        self.connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED' "
            "WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        )
        self.connection.execute(
            "INSERT INTO execplan_blocks "
            "(repo, plan_id, scope, reason_code, evidence) "
            "VALUES (?, 'G-003', 'plan', 'plan_input_invalid', 'decision')",
            (self.repo,),
        )
        self.plan("G-005", "REVIEWING")
        self.assertEqual(
            loop_manager.active_execplan(self.connection, self.repo)["plan_id"],
            "G-005",
        )

    def test_dependency_gate_selects_independent_ready_while_others_wait(self):
        blocked = self.plan("G-001", "IMPLEMENTING", priority="critical")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.USER_DECISION_REQUIRED,
                "decision",
                "decision-fingerprint",
            )
        self.plan("G-002", dependencies=("G-001",), priority="critical")
        self.plan("G-003", priority="normal")
        self.plan(
            "G-004",
            "PR_OPEN",
            priority="high",
            branch="loop-manager/G-004",
        )
        self.plan("G-005", priority="high")

        decision = loop_manager.catalog_schedule_decision(
            self.connection, self.repo
        )
        self.assertIsNone(decision.active)
        self.assertTrue(decision.has_plan_blocked)
        self.assertTrue(decision.has_pr_open)
        self.assertEqual(decision.runnable_ready["plan_id"], "G-005")
        self.assertNotEqual(blocked["plan_id"], decision.runnable_ready["plan_id"])

        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                "cannot determine safe repository state",
                None,
                plan_id="G-001",
                previous_phase=None,
            )
        blocked_decision = loop_manager.catalog_schedule_decision(
            self.connection, self.repo
        )
        self.assertTrue(blocked_decision.project_blocked)
        self.assertIsNone(blocked_decision.runnable_ready)

    def test_agent_scope_ignores_codex_turn_refs_but_structures_real_boundaries(self):
        baseline = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="loop-manager/G-001",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        turn_ref = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {"refs/codex/turn-diffs/1": ("b" * 40, "")},
            }
        )
        loop_manager.validate_agent_scope(
            "reviewer", Path(self.repo), baseline, turn_ref
        )

        shared_ref = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {"refs/tags/agent-created": ("b" * 40, "")},
            }
        )
        with self.assertRaises(loop_manager.AgentScopeError) as raised:
            loop_manager.validate_agent_scope(
                "reviewer", Path(self.repo), baseline, shared_ref
            )
        self.assertEqual(
            raised.exception.reason_code,
            BlockReasonCode.SHARED_REF_MUTATION,
        )
        self.assertIs(
            scoped_blocking.reason_scope(raised.exception.reason_code),
            BlockScope.PROJECT,
        )

        local_change = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "dirty": {"module.py": (("unstaged",), "changed")},
            }
        )
        with self.assertRaises(loop_manager.AgentScopeError) as raised:
            loop_manager.validate_agent_scope(
                "reviewer", Path(self.repo), baseline, local_change
            )
        self.assertEqual(
            raised.exception.reason_code,
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
        )

    def test_existing_turn_diff_project_block_releases_automatically(self):
        baseline = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="loop-manager/G-001",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        current = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {
                    "refs/codex/turn-diffs/captures/1/session/base": (
                        "b" * 40,
                        "",
                    )
                },
            }
        )
        self.plan(
            "G-001",
            "IMPLEMENTING",
            branch="loop-manager/G-001",
        )
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/state/G-001', "
                "agent_role = 'worker', agent_snapshot = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (baseline.serialize(), self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'WORKING', "
                "plan_path = 'docs/plans/[G-001] Work.md' WHERE repo = ?",
                (self.repo,),
            )
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.TURN_DIFF_REF_MUTATION,
                "worker Git 범위 위반: Codex turn ref changed",
                "repository-fingerprint",
                plan_id="G-001",
                previous_phase="WORKING",
            )

        with mock.patch(
            "loop_manager.validate_execplan_workspace_identity"
        ), mock.patch("loop_manager.validate_managed_worktree"), mock.patch(
            "loop_manager.capture_git_snapshot", return_value=current
        ):
            released = loop_manager.resume_project_block_if_safe(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )

        self.assertTrue(released)
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertEqual(project["phase"], "WORKING")

    def test_all_open_prs_are_read_before_one_conflict_is_selected(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            priority="normal",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        self.plan(
            "G-002",
            "PR_OPEN",
            priority="high",
            branch="loop-manager/G-002",
            pr_url="https://pr/2",
        )
        pulls = {
            "loop-manager/G-001": {
                "url": "https://pr/1",
                "state": "OPEN",
                "headRefName": "loop-manager/G-001",
                "headRefOid": "1" * 40,
                "mergeable": "UNKNOWN",
            },
            "loop-manager/G-002": {
                "url": "https://pr/2",
                "state": "OPEN",
                "headRefName": "loop-manager/G-002",
                "headRefOid": "2" * 40,
                "mergeable": "CONFLICTING",
            },
        }
        observed = []

        def lookup(_repo, branch, _url=None):
            observed.append(branch)
            return pulls[branch]

        with mock.patch("loop_manager.pr_for_branch", side_effect=lookup), mock.patch(
            "loop_manager.reconcile_existing_pull_request", return_value=None
        ) as reconcile:
            result = loop_manager.synchronize_waiting_pull_requests(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )

        self.assertEqual(
            observed,
            ["loop-manager/G-001", "loop-manager/G-002"],
        )
        self.assertTrue(result.conflict_started)
        self.assertEqual(reconcile.call_count, 1)
        mirrored = self.connection.execute(
            "SELECT plan_path FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertIn("[G-002]", mirrored["plan_path"])

    def test_closed_and_reopened_pr_only_updates_its_plan(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        self.plan(
            "G-002",
            "PR_OPEN",
            branch="loop-manager/G-002",
            pr_url="https://pr/2",
        )

        def pulls(state):
            return {
                "loop-manager/G-001": {
                    "url": "https://pr/1",
                    "state": state,
                    "headRefOid": "1" * 40,
                    "mergeable": "UNKNOWN",
                },
                "loop-manager/G-002": {
                    "url": "https://pr/2",
                    "state": "OPEN",
                    "headRefOid": "2" * 40,
                    "mergeable": "UNKNOWN",
                },
            }

        closed = pulls("CLOSED")
        with mock.patch(
            "loop_manager.pr_for_branch",
            side_effect=lambda _repo, branch, _url=None: closed[branch],
        ):
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        statuses = {
            row["plan_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                (self.repo,),
            )
        }
        self.assertEqual(statuses, {"G-001": "PLAN_BLOCKED", "G-002": "PR_OPEN"})

        reopened = pulls("OPEN")
        with mock.patch(
            "loop_manager.pr_for_branch",
            side_effect=lambda _repo, branch, _url=None: reopened[branch],
        ):
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        statuses = {
            row["plan_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                (self.repo,),
            )
        }
        self.assertEqual(statuses, {"G-001": "PR_OPEN", "G-002": "PR_OPEN"})

    def test_project_block_allows_pr_read_but_skips_merge_mutation(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                "default worktree changed",
                "git-snapshot",
                plan_id="G-001",
                previous_phase=None,
            )
        merged = {
            "url": "https://pr/1",
            "state": "MERGED",
            "mergedAt": "2026-01-01T00:00:00Z",
            "headRefOid": "1" * 40,
        }
        with mock.patch("loop_manager.pr_for_branch", return_value=merged) as read_pr, mock.patch(
            "loop_manager.git"
        ) as git, mock.patch(
            "loop_manager.complete_merged_execplan_worktree"
        ) as complete:
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        read_pr.assert_called_once()
        git.assert_not_called()
        complete.assert_not_called()
        status = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()[0]
        self.assertEqual(status, "PR_OPEN")

    def test_selected_pr_can_escalate_without_occupying_active_status(self):
        plan = self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        loop_manager.hydrate_execplan_project(self.connection, self.repo, plan)
        with mock.patch(
            "loop_manager._revision_execplan_input_fingerprint",
            return_value="repository-fingerprint",
        ):
            loop_manager.block_active_execplan_from_revision(
                self.connection,
                Path(self.repo),
                self.repo,
                "refs/remotes/origin/main",
                "shared refs changed",
                reason=BlockReasonCode.SHARED_REF_MUTATION,
            )
        stored = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        project_block = scoped_blocking.project_block(self.connection, self.repo)
        self.assertEqual(stored["status"], "PR_OPEN")
        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
        self.assertEqual(project_block["plan_id"], "G-001")
        self.assertEqual(project_block["reason_code"], "shared_ref_mutation")

        with mock.patch(
            "loop_manager.validate_execplan_workspace_identity"
        ), mock.patch("loop_manager.validate_managed_worktree"):
            self.assertTrue(
                loop_manager.resume_project_block_if_safe(
                    self.connection,
                    Path(self.repo),
                    Path("/state"),
                    self.repo,
                )
            )
        released_project = self.connection.execute(
            "SELECT phase, plan_path FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertEqual(tuple(released_project), (None, None))

    def test_project_release_restores_phase_and_preserves_plan_block(self):
        snapshot = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="main",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        blocked = self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.USER_DECISION_REQUIRED,
                "plan decision",
                "plan-fingerprint",
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PLANNING', worktree = ?, "
                "agent_role = 'planner', agent_snapshot = ? WHERE repo = ?",
                (self.repo, snapshot.serialize(), self.repo),
            )
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                "default worktree changed",
                "repository-fingerprint",
                plan_id=None,
                previous_phase="PLANNING",
            )
        with mock.patch("loop_manager.capture_git_snapshot", return_value=snapshot):
            released = loop_manager.resume_project_block_if_safe(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        retained = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (self.repo, blocked["plan_id"]),
        ).fetchone()
        self.assertTrue(released)
        self.assertEqual(project["phase"], "PLANNING")
        self.assertEqual(retained["status"], "PLAN_BLOCKED")
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))

    def test_block_notifications_distinguish_new_fingerprint_and_scope(self):
        notifications.configure_session(self.connection, enabled=True)
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "first evidence",
                "one",
            )
            checkpoint_repository = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    self.connection
                )
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(self.repo, "G-001"),
                "first local-only result",
                retryable=False,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "updated evidence",
                "two",
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(self.repo, "G-001"),
                "updated local-only result",
                retryable=False,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
        rows = self.connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(
            (payload["scope"], payload["reason_code"], payload["execplan_id"]),
            ("plan", "review_retry_exhausted", "G-001"),
        )

        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
                "shared config changed",
                "config-fingerprint",
                plan_id="G-001",
                previous_phase=None,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
        project_payload = next(
            parsed
            for parsed in (
                json.loads(row[0])
                for row in self.connection.execute(
                    "SELECT payload FROM notification_outbox "
                    "WHERE event_type = 'blocked'"
                )
            )
            if parsed["scope"] == "project"
        )
        self.assertEqual(
            (
                project_payload["scope"],
                project_payload["reason_code"],
                project_payload["execplan_id"],
                project_payload["execplan_title"],
            ),
            ("project", "shared_git_config_mutation", "G-001", "Work G-001"),
        )
        self.assertEqual(
            project_payload["paths"],
            [],
        )

    def test_plan_block_is_waiting_and_registry_removal_is_rejected(self):
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.PLAN_INPUT_INVALID,
                "invalid plan input",
                "input-fingerprint",
            )
        with mock.patch("loop_manager._run_project_step", return_value=1):
            result = loop_manager.run_project_result(
                self.connection,
                Path("/state"),
                self.repo,
                Path("config.ini"),
                None,
                False,
            )
        self.assertEqual(
            (result.kind, result.exit_code, result.detail),
            (loop_manager.ProjectRunKind.WAITING, 0, "invalid plan input"),
        )
        with self.assertRaises(project_registry.ProjectRegistryError):
            project_registry.remove_project(self.connection, self.repo)

    def test_checkpoint_retry_does_not_occupy_independent_ready_slot(self):
        self.plan("G-001", "IMPLEMENTING", priority="critical")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "review failure",
                "review-fingerprint",
            )
        self.plan("G-002", "READY", priority="normal")
        steps = []

        def project_step(*_args, **_kwargs):
            steps.append(len(steps) + 1)
            if len(steps) == 2:
                self.connection.execute(
                    "UPDATE exec_plans SET status = 'MERGED' "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (self.repo,),
                )
                self.connection.commit()
                return 0
            return 1

        with mock.patch(
            "loop_manager.dispatch_blocked_checkpoints", return_value=0
        ) as checkpoint, mock.patch(
            "loop_manager._run_project_step", side_effect=project_step
        ):
            result = loop_manager.run_project_result(
                self.connection,
                Path("/state"),
                self.repo,
                Path("config.ini"),
                None,
                False,
            )

        self.assertEqual(steps, [1, 2])
        checkpoint.assert_called_once()
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        blocked = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(blocked["status"], "PLAN_BLOCKED")

    def test_external_remote_checkpoint_change_prevents_plan_resume(self):
        self.plan(
            "G-001",
            "IMPLEMENTING",
            branch="loop-manager/G-001",
        )
        self.connection.execute(
            "UPDATE exec_plans SET worktree = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("/state/G-001", self.repo),
        )
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.PLAN_INPUT_INVALID,
                "input changed",
                "old-input",
            )
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        )
        repository.finish_published(
            repository.get(self.repo, "G-001"), "a" * 40
        )

        with mock.patch.object(
            loop_manager._BlockedCheckpointAdapter,
            "observe_remote",
            return_value="b" * 40,
        ):
            resumed = loop_manager.resume_plan_blocks(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
                "main",
            )

        self.assertFalse(resumed)
        plan = self.connection.execute(
            "SELECT status, last_error FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIn("외부에서 변경", plan["last_error"])

    def test_legacy_unscoped_block_is_conservatively_project_blocked(self):
        self.plan("G-001", "READY")
        self.connection.execute(
            "UPDATE exec_plans SET status = 'BLOCKED', last_error = 'legacy text' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        )
        self.connection.execute(
            "UPDATE projects SET phase = 'BLOCKED', plan_path = ? WHERE repo = ?",
            ("docs/plans/[G-001] Work.md", self.repo),
        )
        self.connection.commit()

        loop_manager._ensure_execplans_schema(self.connection)

        project_block = scoped_blocking.project_block(
            self.connection, self.repo
        )
        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(
            project_block["reason_code"], "legacy_unscoped_block"
        )


if __name__ == "__main__":
    unittest.main()
