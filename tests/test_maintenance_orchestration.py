import json
import sqlite3
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.maintenance.maintenance_orchestration import (
    MaintenanceOrchestrationError,
    MaintenanceOutcome,
    maintenance_planning_fingerprint,
    parse_maintenance_planner_decision,
    validate_prerequisite_plan_ids,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanMode, MaintenancePlanningContext
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.planning import planning_runtime
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError
from tests.support.git import commit_and_push, git_command, make_remote_repository
from tests.support.worker import dummy_worker_context
from tests.support.catalog import plan_text


from tests.support.maintenance_cycle_case import MaintenanceCycleCase

class MaintenanceOrchestrationTest(MaintenanceCycleCase):
    def test_assessment_activation_preserves_worker_session_in_plan_and_projection(self):
        from loop_manager_internal.execution.worker_session import SessionBinding, SessionTransition

        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_plan(connection, "/one", "G-058")
        binding = SessionBinding("codex", "existing-model", "high").serialize()
        transition = SessionTransition(
            "existing-transition", "switch", "model_changed", "existing-session",
            SessionBinding("codex", "existing-model", "high"),
            SessionBinding("codex", "next-model", "high"),
        ).serialize()
        with tempfile.TemporaryDirectory() as directory:
            worktree = str(Path(directory) / "G-058")
            connection.execute(
                "UPDATE exec_plans SET ever_started = 1, branch = 'owned-G-058', "
                "worktree = ?, workspace_owned = 1, worker_session_id = 'existing-session', "
                "worker_session_binding = ?, worker_session_transition = ? "
                "WHERE repo = '/one' AND plan_id = 'G-058'",
                (worktree, binding, transition),
            )
            cycle, _ = self._legacy_cycle(connection, "/one", "a" * 40)
            connection.commit()
            activated = loop_manager.activate_ready_execplan(
                connection, "/one", Path(directory), "a" * 40,
            )
        project = connection.execute("SELECT * FROM projects WHERE repo = '/one'").fetchone()
        self.assertEqual(activated["status"], "IMPLEMENTING")
        self.assertEqual(project["phase"], "WORKING")
        for row in (activated, project):
            self.assertEqual(row["worker_session_id"], "existing-session")
            self.assertEqual(row["worker_session_binding"], binding)
            self.assertEqual(row["worker_session_transition"], transition)
            self.assertEqual(row["branch"], "owned-G-058")
            self.assertEqual(row["worktree"], worktree)
        self.assertEqual(activated["workspace_owned"], 1)
        self.assertEqual(activated["ever_started"], 1)
        self.assertEqual(activated["depends_on"], "[]")
        self.assertEqual(maintenance_state.MaintenanceRepository(connection).get("/one"), cycle)

    def test_unowned_ready_does_not_bypass_unpublished_assessment_barrier(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_plan(connection, "/one", "G-115", priority="critical")
        cycle, _ = self._legacy_cycle(connection, "/one", "a" * 40)
        decision = loop_manager.catalog_schedule_decision(connection, "/one")
        self.assertTrue(decision.has_ready)
        self.assertIsNone(decision.runnable_ready)
        self.assertEqual(maintenance_state.MaintenanceRepository(connection).get("/one"), cycle)

    def test_unpublished_assessment_resumes_owned_ready_without_relaxing_barriers(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_plan(connection, "/one", "G-058")
        self._insert_plan(connection, "/one", "G-115", priority="critical")
        connection.execute(
            "UPDATE exec_plans SET ever_started = 1, branch = 'owned', "
            "worktree = '/managed', workspace_owned = 1, worker_session_id = 'session' "
            "WHERE repo = '/one' AND plan_id = 'G-058'"
        )
        cycle, _ = self._legacy_cycle(connection, "/one", "a" * 40)
        loop_manager._ensure_execplans_schema(connection)
        before = dict(connection.execute(
            "SELECT * FROM exec_plans WHERE repo = '/one' AND plan_id = 'G-058'"
        ).fetchone())
        for _restart in range(2):
            decision = loop_manager.catalog_schedule_decision(connection, "/one")
            self.assertEqual(decision.runnable_ready["plan_id"], "G-058")
            self.assertEqual(maintenance_state.MaintenanceRepository(connection).get("/one"), cycle)
        connection.execute(
            "UPDATE maintenance_cycles SET publication_stage = 'prepared' WHERE repo = '/one'"
        )
        self.assertIsNone(loop_manager.catalog_schedule_decision(connection, "/one").runnable_ready)
        connection.execute(
            "UPDATE maintenance_cycles SET publication_stage = NULL, phase = 'post_refactor_revalidation' "
            "WHERE repo = '/one'"
        )
        self.assertIsNone(loop_manager.catalog_schedule_decision(connection, "/one").runnable_ready)
        self.assertEqual(dict(connection.execute(
            "SELECT * FROM exec_plans WHERE repo = '/one' AND plan_id = 'G-058'"
        ).fetchone()), before)

    def test_prerequisite_rows_follow_only_required_merged_closure(self):
        connection = self._connection()
        self._insert_plan(connection, "/one", "G-054", status="MERGED")
        self._archive_plan(connection, "/one", "G-054")
        self._insert_plan(
            connection, "/one", "G-060", status="MERGED", depends_on=("G-054",)
        )
        self._archive_plan(connection, "/one", "G-060")
        self._insert_plan(
            connection, "/one", "G-070", depends_on=("G-060", "G-054")
        )
        self._insert_plan(connection, "/one", "G-071", depends_on=("G-054",))
        self._insert_plan(connection, "/one", "G-099", status="MERGED")
        self._insert_plan(connection, "/two", "G-080", status="MERGED")

        rows = loop_manager.prerequisite_validation_rows(connection, "/one")
        self.assertEqual(
            {row["plan_id"] for row in rows}, {"G-054", "G-060", "G-070", "G-071"}
        )
        self.assertEqual(validate_prerequisite_plan_ids(rows, ("G-070",)), ("G-070",))
        compact = json.loads(loop_manager.compact_execplan_catalog(connection, "/one"))
        self.assertEqual([entry["id"] for entry in compact], ["G-070", "G-071"])

    def test_prerequisite_rows_reject_unverified_archived_dependency(self):
        connection = self._connection()
        self._insert_plan(connection, "/one", "G-054", status="MERGED")
        self._archive_plan(connection, "/one", "G-054")
        self._insert_plan(connection, "/one", "G-070", depends_on=("G-054",))

        for field, value, reason in (
            ("identity_verified", 0, "identity"),
            ("observed_path", "docs/plans/archive/[G-999] wrong.md", "identity"),
            ("merge_confirmed", 0, "Merge"),
            ("proof_revision", None, "Merge"),
        ):
            with self.subTest(field=field):
                original = connection.execute(
                    f"SELECT {field} FROM execplan_catalog_history WHERE repo = '/one'"
                ).fetchone()[0]
                connection.execute(
                    f"UPDATE execplan_catalog_history SET {field} = ? WHERE repo = '/one'",
                    (value,),
                )
                with self.assertRaisesRegex(
                    MaintenanceOrchestrationError, f"G-070 -> G-054.*{reason}"
                ):
                    loop_manager.prerequisite_validation_rows(connection, "/one")
                connection.execute(
                    f"UPDATE execplan_catalog_history SET {field} = ? WHERE repo = '/one'",
                    (original,),
                )

        connection.execute(
            "UPDATE exec_plans SET content_hash = 'mismatched' "
            "WHERE repo = '/one' AND plan_id = 'G-054'"
        )
        with self.assertRaisesRegex(MaintenanceOrchestrationError, "G-070 -> G-054.*identity"):
            loop_manager.prerequisite_validation_rows(connection, "/one")
        connection.execute(
            "UPDATE exec_plans SET content_hash = 'hash-G-054' "
            "WHERE repo = '/one' AND plan_id = 'G-054'"
        )

        connection.execute(
            "UPDATE exec_plans SET status = 'READY' "
            "WHERE repo = '/one' AND plan_id = 'G-054'"
        )
        with self.assertRaisesRegex(MaintenanceOrchestrationError, "G-070 -> G-054.*MERGED"):
            loop_manager.prerequisite_validation_rows(connection, "/one")
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = '/one' AND plan_id = 'G-054'"
        )

        connection.execute(
            "DELETE FROM execplan_catalog_history WHERE repo = '/one'"
        )
        with self.assertRaisesRegex(MaintenanceOrchestrationError, "G-070 -> G-054.*identity"):
            loop_manager.prerequisite_validation_rows(connection, "/one")

    def test_prerequisite_rows_reject_missing_or_cross_project_dependency(self):
        connection = self._connection()
        self._insert_plan(connection, "/two", "G-054", status="MERGED")
        self._insert_plan(connection, "/one", "G-070", depends_on=("G-054",))
        with self.assertRaisesRegex(MaintenanceOrchestrationError, "G-070 -> G-054"):
            loop_manager.prerequisite_validation_rows(connection, "/one")

        connection.execute(
            "UPDATE exec_plans SET depends_on = '[\"G-999\"]' "
            "WHERE repo = '/one' AND plan_id = 'G-070'"
        )
        with self.assertRaisesRegex(MaintenanceOrchestrationError, "G-070 -> G-999"):
            loop_manager.prerequisite_validation_rows(connection, "/one")

    def test_protocol_is_phase_specific_and_requires_exact_fields(self):
        assessment = maintenance_state.MaintenancePhase.ASSESSMENT
        revalidation = maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION

        decision = parse_maintenance_planner_decision(
            'note\nMAINTENANCE_RESULT: {"outcome":"refactoring",'
            '"plan_ids":["G-010","G-002"]}',
            assessment,
        )
        self.assertEqual(decision.outcome, MaintenanceOutcome.REFACTORING)
        self.assertEqual(decision.plan_ids, ("G-002", "G-010"))
        unsafe = parse_maintenance_planner_decision(
            'MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"goal drift"}',
            revalidation,
        )
        self.assertEqual(unsafe.reason, "goal drift")

        invalid = (
            ('MAINTENANCE_RESULT: {"outcome":"revalidated"}', assessment),
            (
                'MAINTENANCE_RESULT: {"outcome":"no_change","plan_ids":[]}',
                assessment,
            ),
            (
                'MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"x"}\n'
                'MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"x"}',
                revalidation,
            ),
        )
        for output, phase in invalid:
            with self.subTest(output=output), self.assertRaises(
                MaintenanceOrchestrationError
            ):
                parse_maintenance_planner_decision(output, phase)
