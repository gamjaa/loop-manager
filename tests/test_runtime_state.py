import dataclasses
from pathlib import Path
import sqlite3
import tempfile
import unittest

import loop_manager
import notifications
import runtime_state
from recovery_runtime import RecoveryObservation, RecoveryReason, decide
from scoped_blocking import BlockReasonCode


class RuntimeStateRepositoryTest(unittest.TestCase):
    def test_nonfinal_recovery_decision_cannot_enter_block_transaction(self):
        connection = self._connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        connection.execute(
            "UPDATE projects SET agent_input_fingerprint = 'saved-input' "
            "WHERE repo = 'repo'"
        )
        connection.commit()
        retry = decide(RecoveryObservation(
            RecoveryReason.GIT_LOCK, 'repo', 'G-047', 'IMPLEMENTING',
            'plan-sha', 'base', 'lock', 'worker-session', 'git busy',
        ))

        with self.assertRaises(ValueError):
            loop_manager.block_active_execplan_from_revision(
                connection, Path('/repo'), 'repo', 'base', 'git busy',
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_decision=retry,
            )
        self.assertEqual(repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.assertIsNone(connection.execute(
            "SELECT * FROM recovery_records WHERE repo = 'repo'"
        ).fetchone())
        self.assertIsNone(connection.execute(
            "SELECT * FROM execplan_blocks WHERE repo = 'repo'"
        ).fetchone())

    def test_transaction_reserves_writer_before_read_and_preserves_nesting(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE state (value INTEGER)")
        statements = []
        connection.set_trace_callback(statements.append)
        with runtime_state._transaction(connection):
            connection.execute("SELECT * FROM state").fetchall()
            connection.execute("INSERT INTO state VALUES (1)")
        self.assertEqual(statements[0], "BEGIN IMMEDIATE")
        self.assertFalse(connection.in_transaction)
        with self.assertRaises(KeyboardInterrupt):
            with runtime_state._transaction(connection):
                connection.execute("INSERT INTO state VALUES (2)")
                raise KeyboardInterrupt()
        self.assertFalse(connection.in_transaction)
        connection.execute("BEGIN")
        connection.execute("INSERT INTO state VALUES (3)")
        with self.assertRaises(ValueError):
            with runtime_state._transaction(connection):
                connection.execute("INSERT INTO state VALUES (4)")
                raise ValueError("rollback nested write")
        with runtime_state._transaction(connection):
            connection.execute("INSERT INTO state VALUES (5)")
        self.assertTrue(connection.in_transaction)
        self.assertEqual(connection.execute("SELECT value FROM state").fetchall(), [(1,), (3,), (5,)])
        connection.rollback()
        self.assertEqual(connection.execute("SELECT value FROM state").fetchall(), [(1,)])

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        notifications.ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO projects (
                repo, base_branch, provider, planning_fingerprint,
                planning_attempt_fingerprint, phase
            ) VALUES ('repo', 'main', 'codex', 'planning-kept',
                      'planning-cleared', NULL)
            """
        )
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha, active_sha, branch, worktree,
                workspace_owned
            ) VALUES (
                'repo', 'G-047', 'docs/plans/[G-047] Runtime.md', 'Runtime',
                '["docs/PRODUCT.md"]', '[]', 'normal', 'hash', 'READY',
                'plan-sha', NULL, NULL, NULL, 1
            )
            """
        )
        connection.commit()
        return connection

    def test_phase_mapping_and_field_ownership_are_declared_once(self):
        self.assertEqual(
            runtime_state.project_phase_for_status("IMPLEMENTING"), "WORKING"
        )
        for value in (
            "VERIFYING",
            "REVIEWING",
            "PUBLISHING",
            "PR_OPEN",
            "PLAN_BLOCKED",
        ):
            with self.subTest(value=value):
                self.assertEqual(runtime_state.project_phase_for_status(value), value)
                self.assertEqual(runtime_state.execplan_status_for_phase(value), value)
        self.assertEqual(
            runtime_state.execplan_status_for_phase("WORKING"), "IMPLEMENTING"
        )
        self.assertEqual(
            runtime_state.COMPATIBILITY_PHASE_BY_STATUS,
            {"IMPLEMENTING": "WORKING"},
        )
        self.assertIsNone(runtime_state.execplan_status_for_phase("PLANNING"))
        self.assertIsNone(runtime_state.execplan_status_for_phase(None))
        self.assertTrue(
            set(runtime_state.MIRRORED_FIELDS).isdisjoint(
                runtime_state.PROJECT_ONLY_FIELDS
            )
        )
        self.assertEqual(loop_manager.FIELDS, runtime_state.PROJECT_UPDATE_FIELDS)

    def test_planning_snapshot_is_typed_and_keeps_project_only_state(self):
        connection = self._connection()
        repository = runtime_state.RuntimeStateRepository(connection)

        snapshot = repository.planning("repo")

        self.assertEqual(snapshot.repo, "repo")
        self.assertEqual(snapshot.planning_fingerprint, "planning-kept")
        self.assertEqual(
            snapshot.planning_attempt_fingerprint, "planning-cleared"
        )
        self.assertIsNone(snapshot.phase)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.phase = "PLANNING"

    def test_activation_update_hydrate_and_clear_round_trip_runtime_fields(self):
        connection = self._connection()
        repository = runtime_state.RuntimeStateRepository(connection)

        activated = repository.activate(
            "repo",
            "G-047",
            activation_sha="active-sha",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )
        self.assertEqual(activated.status, "IMPLEMENTING")
        self.assertEqual(activated.phase, "WORKING")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            activated.status = "VERIFYING"

        project = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(
            (
                project["phase"],
                project["plan_path"],
                project["branch"],
                project["worktree"],
            ),
            (
                "WORKING",
                "docs/plans/[G-047] Runtime.md",
                "loop-manager/G-047",
                "/state/G-047",
            ),
        )

        repository.update_project(
            "repo",
            {
                "phase": "REVIEWING",
                "worker_session_id": "worker-session",
                "reviewer_session_id": "reviewer-session",
                "worker_result": "worker-result",
                "verify_result": "verify-result",
                "review_result": "review-result",
                "validated_base_sha": "base-sha",
                "publication_remote_oid": "remote-sha",
                "pr_url": "https://example.test/pr/47",
                "pr_head_oid": "head-sha",
                "agent_role": "reviewer",
                "agent_snapshot": "snapshot",
                "agent_input_fingerprint": "fingerprint",
                "planning_fingerprint": "project-only",
            },
        )
        snapshot = repository.get("repo", "G-047")
        self.assertEqual(snapshot.status, "REVIEWING")
        self.assertEqual(snapshot.worker_session_id, "worker-session")
        self.assertEqual(snapshot.reviewer_session_id, "reviewer-session")
        self.assertEqual(snapshot.verify_result, "verify-result")
        self.assertEqual(snapshot.publication_remote_oid, "remote-sha")
        self.assertEqual(snapshot.agent_role, "reviewer")
        self.assertNotIn(
            "planning_fingerprint",
            {row[1] for row in connection.execute("PRAGMA table_info(exec_plans)")},
        )

        connection.execute(
            "UPDATE projects SET phase = NULL, worker_session_id = NULL, "
            "reviewer_session_id = NULL, plan_path = NULL WHERE repo = 'repo'"
        )
        connection.commit()
        repository.hydrate("repo", "G-047")
        hydrated = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(hydrated["phase"], "REVIEWING")
        self.assertEqual(hydrated["worker_session_id"], "worker-session")
        self.assertEqual(hydrated["reviewer_session_id"], "reviewer-session")
        self.assertEqual(hydrated["planning_fingerprint"], "project-only")

        repository.clear("repo")
        cleared = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertIsNone(cleared["phase"])
        self.assertIsNone(cleared["plan_path"])
        self.assertIsNone(cleared["worker_session_id"])
        self.assertIsNone(cleared["planning_attempt_fingerprint"])
        self.assertEqual(cleared["planning_fingerprint"], "project-only")
        self.assertEqual(repository.get("repo", "G-047").status, "REVIEWING")

    def test_compatibility_wrappers_preserve_public_update_and_hydrate_contract(self):
        connection = self._connection()
        loop_manager.activate_ready_execplan(
            connection,
            "repo",
            loop_manager.Path("/state"),
            "active-sha",
        )
        loop_manager.update_project(
            connection,
            "repo",
            phase="VERIFYING",
            worker_session_id="worker-session",
        )
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual((plan["status"], plan["worker_session_id"]), (
            "VERIFYING", "worker-session"
        ))
        connection.execute(
            "UPDATE projects SET phase = NULL, plan_path = NULL WHERE repo = 'repo'"
        )
        connection.commit()
        loop_manager.hydrate_execplan_project(connection, "repo", plan)
        project = connection.execute(
            "SELECT phase, plan_path FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(tuple(project), (
            "VERIFYING", "docs/plans/[G-047] Runtime.md"
        ))
        loop_manager.clear_active(connection, "repo")
        self.assertIsNone(
            connection.execute(
                "SELECT phase FROM projects WHERE repo = 'repo'"
            ).fetchone()[0]
        )
        with self.assertRaisesRegex(ValueError, "유효하지 않은 프로젝트 상태 필드"):
            loop_manager.update_project(connection, "repo", unknown="value")
        loop_manager._mirror_project_update_to_execplan(
            connection,
            "repo",
            connection.execute(
                "SELECT plan_path FROM projects WHERE repo = 'repo'"
            ).fetchone(),
            {"unknown": "ignored"},
        )
        loop_manager.update_project(connection, "missing", phase="WORKING")
        loop_manager.clear_active(connection, "missing")
        loop_manager.hydrate_execplan_project(connection, "missing", plan)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('other', 'main')"
        )
        loop_manager.hydrate_execplan_project(connection, "other", plan)
        self.assertEqual(
            connection.execute(
                "SELECT plan_path FROM projects WHERE repo = 'other'"
            ).fetchone()[0],
            "docs/plans/[G-047] Runtime.md",
        )

        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        legacy.row_factory = sqlite3.Row
        legacy.execute(loop_manager.SCHEMA)
        legacy.execute(
            "INSERT INTO projects (repo, base_branch, plan_path, phase) "
            "VALUES ('legacy', 'main', "
            "'docs/plans/[G-047] Runtime.md', 'WORKING')"
        )
        loop_manager.update_project(
            legacy, "legacy", worker_session_id="legacy-session"
        )
        self.assertIsNotNone(
            legacy.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'exec_plans'"
            ).fetchone()
        )

    def test_workspace_reservation_and_merge_are_repository_operations(self):
        connection = self._connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.update_plan(
            "repo",
            "G-047",
            plan_values={"workspace_owned": 0},
            enqueue=False,
        )

        def fail_after_update(stage: str) -> None:
            if stage == "after_execplan_update":
                raise RuntimeError(stage)

        with self.assertRaisesRegex(RuntimeError, "after_execplan_update"):
            runtime_state.RuntimeStateRepository(
                connection,
                fault_hook=fail_after_update,
            ).reserve_workspace(
                "repo",
                "G-047",
                reserved_value=-1,
                allowed_values=(0, -1),
            )
        self.assertEqual(repository.get("repo", "G-047").workspace_owned, 0)

        reserved = repository.reserve_workspace(
            "repo",
            "G-047",
            reserved_value=-1,
            allowed_values=(0, -1),
        )
        self.assertEqual(reserved.workspace_owned, -1)
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.reserve_workspace(
                "repo",
                "G-047",
                reserved_value=-1,
                allowed_values=(0,),
            )
        self.assertEqual(repository.get("repo", "G-047").workspace_owned, -1)

        repository.activate(
            "repo",
            "G-047",
            activation_sha="active-sha",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )
        merged = repository.mark_merged(
            "repo",
            "G-047",
            project_values=runtime_state.clear_project_values(
                processed_sha="merged-sha",
                planning_fingerprint="merged-cycle",
            ),
        )
        project = connection.execute(
            "SELECT phase, plan_path, processed_sha, planning_fingerprint "
            "FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(merged.status, "MERGED")
        self.assertEqual(
            tuple(project),
            (None, None, "merged-sha", "merged-cycle"),
        )

    def test_expected_status_mismatch_and_between_row_failure_roll_back(self):
        for stage in ("after_execplan_update", "after_project_update"):
            with self.subTest(stage=stage):
                connection = self._connection()

                def fail(current: str) -> None:
                    if current == stage:
                        raise RuntimeError(stage)

                repository = runtime_state.RuntimeStateRepository(
                    connection, fault_hook=fail
                )
                with self.assertRaisesRegex(RuntimeError, stage):
                    repository.activate(
                        "repo",
                        "G-047",
                        activation_sha="active-sha",
                        branch="loop-manager/G-047",
                        worktree="/state/G-047",
                    )
                plan = connection.execute(
                    "SELECT status, active_sha FROM exec_plans "
                    "WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                project = connection.execute(
                    "SELECT phase, active_sha, plan_path FROM projects "
                    "WHERE repo = 'repo'"
                ).fetchone()
                self.assertEqual(tuple(plan), ("READY", None))
                self.assertEqual(tuple(project), (None, None, None))

                activated = runtime_state.RuntimeStateRepository(
                    connection
                ).activate(
                    "repo",
                    "G-047",
                    activation_sha="active-sha",
                    branch="loop-manager/G-047",
                    worktree="/state/G-047",
                )
                self.assertEqual(activated.status, "IMPLEMENTING")

        connection = self._connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            "repo",
            "G-047",
            activation_sha="active-sha",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.activate(
                "repo",
                "G-047",
                activation_sha="different",
                branch="different",
                worktree="different",
            )
        self.assertEqual(repository.get("repo", "G-047").active_sha, "active-sha")

    def test_stage_revision_tokens_are_snapshot_bound_and_projected_atomically(self):
        connection = self._connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        original = repository.activate(
            "repo",
            "G-047",
            activation_sha="base",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )

        revised = repository.transition_from_snapshot(
            original,
            plan_values={
                "definition_snapshot": '{"revision":"new"}',
                "worker_input_token": "worker-token",
                "archive_token": "archive-token",
            },
            project_values={
                "definition_snapshot": '{"revision":"new"}',
                "worker_input_token": "worker-token",
                "archive_token": "archive-token",
            },
        )

        project = connection.execute(
            "SELECT definition_snapshot, worker_input_token, archive_token "
            "FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(
            tuple(project),
            ('{"revision":"new"}', "worker-token", "archive-token"),
        )
        self.assertEqual(revised.worker_input_token, "worker-token")
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.transition_from_snapshot(
                original,
                plan_values={"verify_token": "stale"},
                project_values={"verify_token": "stale"},
            )
        self.assertIsNone(repository.get("repo", "G-047").verify_token)

    def test_outbox_failures_roll_back_source_projection_and_event(self):
        for stage in ("before_outbox_enqueue", "after_outbox_enqueue"):
            with self.subTest(stage=stage):
                connection = self._connection()
                repository = runtime_state.RuntimeStateRepository(connection)
                repository.activate(
                    "repo",
                    "G-047",
                    activation_sha="active-sha",
                    branch="loop-manager/G-047",
                    worktree="/state/G-047",
                )
                repository.update_project("repo", {"phase": "PUBLISHING"})
                notifications.configure_session(connection, enabled=True)
                connection.commit()

                def fail(current: str) -> None:
                    if current == stage:
                        raise RuntimeError(stage)

                failing = runtime_state.RuntimeStateRepository(
                    connection, fault_hook=fail
                )
                with self.assertRaisesRegex(RuntimeError, stage):
                    failing.open_pull_request(
                        "repo",
                        pr_url="https://example.test/pr/47",
                        pr_head_oid="head-sha",
                    )
                plan = connection.execute(
                    "SELECT status, pr_url FROM exec_plans "
                    "WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                project = connection.execute(
                    "SELECT phase, plan_path, pr_url FROM projects WHERE repo = 'repo'"
                ).fetchone()
                outbox_count = connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox"
                ).fetchone()[0]
                self.assertEqual(tuple(plan), ("PUBLISHING", None))
                self.assertEqual(
                    tuple(project),
                    ("PUBLISHING", "docs/plans/[G-047] Runtime.md", None),
                )
                self.assertEqual(outbox_count, 0)

    def test_existing_database_schema_and_runtime_values_survive_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            connection = loop_manager.connect_database(state_dir, create=True)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, provider, active_sha, validated_base_sha,
                    publication_remote_oid, plan_sha, phase, branch, worktree,
                    plan_path, pr_url, pr_head_oid, worker_session_id,
                    reviewer_session_id, worker_result, verify_result,
                    review_result, agent_role, agent_snapshot,
                    agent_input_fingerprint
                ) VALUES (
                    'repo', 'main', 'codex', 'active-sha', 'base-sha',
                    'remote-sha', 'plan-sha', 'VERIFYING',
                    'loop-manager/G-047', '/state/G-047',
                    'docs/plans/[G-047] Runtime.md',
                    'https://example.test/pr/47', 'head-sha',
                    'worker-session', 'reviewer-session', 'worker-result',
                    'verify-result', 'review-result', 'worker', 'snapshot',
                    'fingerprint'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, plan_sha, active_sha,
                    validated_base_sha, publication_remote_oid, branch,
                    worktree, pr_url, pr_head_oid, worker_session_id,
                    reviewer_session_id, worker_result, verify_result,
                    review_result, workspace_owned, agent_role, agent_snapshot,
                    agent_input_fingerprint
                ) VALUES (
                    'repo', 'G-047', 'docs/plans/[G-047] Runtime.md',
                    'Runtime', '["docs/PRODUCT.md"]', '[]', 'normal',
                    'hash', 'VERIFYING', 'plan-sha', 'active-sha', 'base-sha',
                    'remote-sha', 'loop-manager/G-047', '/state/G-047',
                    'https://example.test/pr/47', 'head-sha',
                    'worker-session', 'reviewer-session', 'worker-result',
                    'verify-result', 'review-result', 1, 'worker', 'snapshot',
                    'fingerprint'
                )
                """
            )
            connection.commit()
            schema_before = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            )
            connection.close()

            resumed = loop_manager.connect_database(state_dir)
            self.addCleanup(resumed.close)
            schema_after = tuple(
                tuple(row)
                for row in resumed.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            )
            snapshot = runtime_state.RuntimeStateRepository(resumed).get(
                "repo", "G-047"
            )
            loop_manager.hydrate_active_project(resumed, "repo")
            project = resumed.execute(
                "SELECT phase, branch, worktree, pr_url, pr_head_oid, "
                "worker_session_id, reviewer_session_id, verify_result, "
                "publication_remote_oid, agent_role, agent_snapshot, "
                "agent_input_fingerprint FROM projects WHERE repo = 'repo'"
            ).fetchone()
            resumed.close()

        self.assertEqual(schema_after, schema_before)
        self.assertEqual(snapshot.status, "VERIFYING")
        self.assertEqual(snapshot.phase, "VERIFYING")
        self.assertEqual(snapshot.workspace_owned, 1)
        self.assertEqual(
            tuple(project),
            (
                "VERIFYING",
                "loop-manager/G-047",
                "/state/G-047",
                "https://example.test/pr/47",
                "head-sha",
                "worker-session",
                "reviewer-session",
                "verify-result",
                "remote-sha",
                "worker",
                "snapshot",
                "fingerprint",
            ),
        )
