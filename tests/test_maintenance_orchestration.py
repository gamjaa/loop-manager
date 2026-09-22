import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from maintenance_orchestration import (
    MaintenanceOrchestrationError,
    MaintenanceOutcome,
    maintenance_planning_fingerprint,
    parse_maintenance_planner_decision,
    validate_prerequisite_plan_ids,
)
from maintenance_planning import MaintenancePlanMode, MaintenancePlanningContext
import maintenance_state
from tests.test_loop_manager import make_remote_repository


class MaintenanceOrchestrationTest(unittest.TestCase):
    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.executemany(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (("/one",), ("/two",)),
        )
        connection.commit()
        return connection

    @staticmethod
    def _insert_plan(
        connection: sqlite3.Connection,
        repo: str,
        plan_id: str,
        *,
        status: str = "READY",
        depends_on: tuple[str, ...] = (),
        priority: str = "normal",
    ) -> None:
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on,
                priority, content_hash, status, plan_sha
            ) VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
            """,
            (
                repo,
                plan_id,
                f"docs/plans/[{plan_id}] {plan_id}.md",
                plan_id,
                json.dumps(depends_on),
                priority,
                f"hash-{plan_id}",
                status,
                "a" * 40,
            ),
        )

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

    def test_first_and_interval_due_are_project_scoped(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        self.assertTrue(repository.due("/one", 2).required)
        self.assertTrue(repository.due("/two", 2).required)

        cycle, created = repository.begin_assessment_if_due(
            "/one", 2, "a" * 40
        )
        self.assertTrue(created)
        completed = repository.advance_success_baseline(
            "/one",
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            remote_commit="a" * 40,
        )
        self.assertEqual(completed.last_success_merged_count, 0)
        self._insert_plan(connection, "/one", "G-001", status="MERGED")
        self.assertFalse(repository.due("/one", 2).required)
        self._insert_plan(connection, "/one", "G-002", status="MERGED")
        self.assertTrue(repository.due("/one", 2).required)
        self.assertTrue(repository.due("/two", 2).required)

    def test_prepared_batch_persists_new_plan_sets_for_restart_recovery(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due(
            "/one", 5, "a" * 40, include_general_planning=True
        )
        prepared = repository.record_publication_intent(
            "/one",
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            base_sha="a" * 40,
            commit_sha="b" * 40,
            refactoring_plan_ids=("G-100",),
            revalidation_plan_ids=("G-099", "G-101"),
        )
        self.assertEqual(prepared.publication_refactoring_plan_ids, ("G-100",))
        self.assertEqual(
            prepared.publication_revalidation_plan_ids,
            ("G-099", "G-101"),
        )
        self.assertTrue(prepared.include_general_planning)

        restarted = repository.restart_unpublished_batch(
            "/one", expected=prepared, new_base_sha="c" * 40
        )
        self.assertIsNone(restarted.publication_stage)
        self.assertEqual(restarted.publication_refactoring_plan_ids, ())
        self.assertEqual(restarted.publication_revalidation_plan_ids, ())
        self.assertTrue(restarted.include_general_planning)

    def test_prerequisites_require_unowned_dependency_closed_acyclic_set(self):
        connection = self._connection()
        self._insert_plan(connection, "/one", "G-001", status="MERGED")
        self._insert_plan(
            connection, "/one", "G-002", depends_on=("G-001",)
        )
        self._insert_plan(
            connection, "/one", "G-003", depends_on=("G-002",)
        )
        rows = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = '/one'"
        ).fetchall()
        self.assertEqual(
            validate_prerequisite_plan_ids(rows, ("G-003", "G-002")),
            ("G-002", "G-003"),
        )
        with self.assertRaisesRegex(
            MaintenanceOrchestrationError, "닫혀 있지 않습니다"
        ):
            validate_prerequisite_plan_ids(rows, ("G-003",))

        connection.execute(
            "UPDATE exec_plans SET branch = 'owned' "
            "WHERE repo = '/one' AND plan_id = 'G-002'"
        )
        owned_rows = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = '/one'"
        ).fetchall()
        with self.assertRaisesRegex(MaintenanceOrchestrationError, "무소유 READY"):
            validate_prerequisite_plan_ids(owned_rows, ("G-002", "G-003"))

        connection.execute(
            'UPDATE exec_plans SET branch = NULL, depends_on = \'["G-003"]\' '
            "WHERE repo = '/one' AND plan_id = 'G-002'"
        )
        cyclic_rows = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = '/one'"
        ).fetchall()
        with self.assertRaisesRegex(MaintenanceOrchestrationError, "cycle"):
            validate_prerequisite_plan_ids(cyclic_rows, ("G-002", "G-003"))

    def test_cycle_selection_overrides_general_ready_and_gates_revalidation(self):
        connection = self._connection()
        self._insert_plan(connection, "/one", "G-001", status="MERGED")
        self._insert_plan(
            connection,
            "/one",
            "G-010",
            depends_on=("G-001",),
            priority="critical",
        )
        self._insert_plan(connection, "/one", "G-002", priority="low")
        self._insert_plan(connection, "/one", "G-099", priority="critical")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due("/one", 5, "a" * 40)
        waiting = repository.transition(
            "/one",
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            prerequisite_plan_ids=("G-010", "G-002"),
        )
        decision = loop_manager.catalog_schedule_decision(connection, "/one")
        self.assertEqual(decision.runnable_ready["plan_id"], "G-010")

        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = '/one' AND plan_id IN ('G-010', 'G-002')"
        )
        assessed = loop_manager._advance_maintenance_cycle_after_merges(
            connection, "/one", "b" * 40
        )
        self.assertEqual(assessed.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        self.assertEqual(assessed.cycle_base_sha, "b" * 40)

        self._insert_plan(connection, "/one", "G-020", priority="critical")
        self._insert_plan(connection, "/one", "G-003", priority="low")
        refactoring = repository.transition(
            "/one",
            expected_phase=assessed.phase,
            expected_revision=assessed.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-020", "G-003"),
            revalidation_plan_ids=("G-099",),
        )
        decision = loop_manager.catalog_schedule_decision(connection, "/one")
        self.assertEqual(decision.runnable_ready["plan_id"], "G-003")
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = '/one' AND plan_id IN ('G-020', 'G-003')"
        )
        revalidation = loop_manager._advance_maintenance_cycle_after_merges(
            connection, "/one", "c" * 40
        )
        self.assertEqual(
            revalidation.phase,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        decision = loop_manager.catalog_schedule_decision(connection, "/one")
        self.assertTrue(decision.has_ready)
        self.assertIsNone(decision.runnable_ready)
        self.assertEqual(refactoring.refactoring_plan_ids, ("G-003", "G-020"))

    def test_prompt_and_fingerprint_distinguish_combined_maintenance_call(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        maintenance_only, _ = repository.begin_assessment_if_due(
            "/one", 5, "a" * 40
        )
        combined, _ = repository.begin_assessment_if_due(
            "/two", 5, "a" * 40, include_general_planning=True
        )
        self.assertNotEqual(
            maintenance_planning_fingerprint(maintenance_only, "same"),
            maintenance_planning_fingerprint(combined, "same"),
        )
        context = MaintenancePlanningContext(
            repo="/two",
            mode=MaintenancePlanMode.BARRIER,
            active_sha="a" * 40,
            cycle_revision=combined.revision,
            cycle_phase=combined.phase.value,
            eligible_plans=(),
        )
        prompt = loop_manager.maintenance_planner_prompt(
            context, "[]", "{}", include_general_planning=True
        )
        for signal in (
            "책임·상태 전이·외부 연동의 혼합",
            "반복 변경 집중",
            "독립 테스트",
            "오류 복구 경계",
            "후속 변경 충돌",
            "현재 미충족 일반 PRODUCT/Requirement 작업도",
        ):
            with self.subTest(signal=signal):
                self.assertIn(signal, prompt)

    def test_registered_initial_cycle_combines_general_gate_in_one_planner_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_path = make_remote_repository(str(root / "project"))
            state_dir = root / "state"
            state_dir.mkdir()
            connection = loop_manager.connect_database(state_dir, create=True)
            repo = str(repo_path)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (repo,),
            )
            connection.commit()
            prompts: list[str] = []

            def agent(
                _worktree,
                _state,
                _config,
                role_name,
                prompt,
                **_kwargs,
            ):
                self.assertEqual(role_name, "planner")
                prompts.append(prompt)
                return loop_manager.CodexResult(
                    0,
                    'MAINTENANCE_RESULT: {"outcome":"no_change"}',
                    "maintenance-session",
                )

            try:
                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo_path
                ), mock.patch(
                    "loop_manager.validate_provider_runtime"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.run_codex", side_effect=agent
                ), mock.patch.object(
                    loop_manager.notifications, "enqueue_completion_if_ready"
                ) as enqueue:
                    result = loop_manager.run_project_result(
                        connection,
                        state_dir,
                        repo,
                        Path("unused.ini"),
                        "main",
                        False,
                        registered_only=True,
                    )
                cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
                project = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.COMPLETED)
        self.assertEqual(len(prompts), 1)
        self.assertIn("현재 미충족 일반 PRODUCT/Requirement 작업도", prompts[0])
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(cycle.last_success_merged_count, 0)
        self.assertIsNone(project["phase"])
        self.assertIsNotNone(project["planning_fingerprint"])
        enqueue.assert_called_once_with(mock.ANY, repo)

    def test_active_or_due_cycle_blocks_project_completion_notification(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due("/one", 5, "a" * 40)
        with mock.patch.object(loop_manager, "_run_project_step", return_value=0), mock.patch.object(
            loop_manager.notifications, "enqueue_completion_if_ready"
        ) as enqueue:
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                "/one",
                Path("/config.ini"),
                None,
                False,
            )
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
            enqueue.assert_not_called()

            repository.advance_success_baseline(
                "/one",
                expected_phase=cycle.phase,
                expected_revision=cycle.revision,
                remote_commit="a" * 40,
            )
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                "/one",
                Path("/config.ini"),
                None,
                False,
            )
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.COMPLETED)
            enqueue.assert_called_once_with(connection, "/one")


if __name__ == "__main__":
    unittest.main()
