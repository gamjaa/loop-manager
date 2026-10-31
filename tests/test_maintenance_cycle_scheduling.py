import json
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOrchestrationError, maintenance_planning_fingerprint, validate_prerequisite_plan_ids
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.planning import planning_runtime
from tests.support.git import git_command, isolated_git_test
from tests.support.worker import dummy_worker_sessions


from tests.support.maintenance_cycle_case import MaintenanceCycleCase

@isolated_git_test
class MaintenanceCycleTest(MaintenanceCycleCase):
    def test_empty_file_selection_finishes_without_assessment_agent(self):
        source = "a" * 40
        context = planning_runtime.PlanningContext(
            repo="/repo", repo_path=Path("/repo"), state_dir=Path("/state"),
            base_branch="main", base_sha=source, provider="codex",
            config_path=Path("/config.ini"), registered_only=True,
            refactoring_review_interval=20,
        )
        row = SimpleNamespace(
            phase="PLANNING", active_sha=source, planning_validation_error=None,
            planning_result_pending_validation=0, planning_result_completed=0,
            planning_retry_count=0, last_error=None,
        )
        cycle = SimpleNamespace(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            file_assessment_start_merged_count=0,
            full_assessment_completed=True, include_general_planning=False,
            publication_stage=None, revision=1,
        )
        run = planning_runtime._PlanningRun(
            context, row, cycle, source, True,
        )
        run.planning_worktree = Path("/worktree")
        ports = mock.Mock()
        del ports.tiered
        ports.git.run.return_value.stdout = source
        ports.git.changed_files.return_value = []
        ports.file_review.progress.return_value.selected = ()
        ports.file_review.progress.return_value.next_unit = None
        ports.file_review.progress.return_value.root_reviews = ()
        coordinator = planning_runtime.PlanningLifecycleCoordinator(ports)
        outcome = planning_runtime.PlanningOutcome(
            planning_runtime.PlanningOutcomeKind.NO_CHANGE,
        )
        with mock.patch.object(
            coordinator._assessment, "_complete_empty_file_assessment", return_value=outcome,
        ) as complete:
            self.assertEqual(coordinator._assessment._run_planner_turn(run), outcome)
        complete.assert_called_once_with(run, Path("/worktree"))
        ports.agent.run_planner.assert_not_called()
        # A source-less tree with an uninvestigated root must run its review unit.
        ports.file_review.progress.return_value.next_unit = object()
        review_outcome = planning_runtime.PlanningOutcome(planning_runtime.PlanningOutcomeKind.WAITING)
        with mock.patch.object(coordinator._assessment, "_complete_empty_file_assessment") as complete, mock.patch.object(
            coordinator._assessment, "_review_one_file", return_value=review_outcome,
        ) as review:
            self.assertEqual(coordinator._assessment._run_planner_turn(run), review_outcome)
        complete.assert_not_called()
        review.assert_called_once_with(run, Path("/worktree"))
        # A completed investigation with a confirmed destination must reach planning.
        ports.file_review.progress.return_value.next_unit = None
        ports.file_review.progress.return_value.root_reviews = ({
            "path": "notes.md", "category": "internal_implementation", "destination": "docs/notes.md",
        },)
        with mock.patch.object(coordinator._assessment, "_complete_empty_file_assessment") as complete, mock.patch.object(
            coordinator._assessment, "_review_one_file", return_value=None,
        ), mock.patch.object(coordinator._assessment, "_reconcile_one_relation", return_value=review_outcome) as reconcile:
            self.assertEqual(coordinator._assessment._run_planner_turn(run), review_outcome)
        complete.assert_not_called()
        reconcile.assert_called_once_with(run, Path("/worktree"))


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


    def test_cycle_selection_finishes_prerequisites_and_refactoring_without_revalidation(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_plan(connection, "/one", "G-010", priority="critical")
        self._insert_plan(connection, "/one", "G-002", priority="low")
        self._insert_plan(connection, "/one", "G-099", priority="critical")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = self._legacy_cycle(connection, "/one", "a" * 40)
        repository.transition("/one", expected_phase=cycle.phase, expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT, prerequisite_plan_ids=("G-010", "G-002"))
        self.assertEqual(loop_manager.catalog_schedule_decision(connection, "/one").runnable_ready["plan_id"], "G-010")
        connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE plan_id IN ('G-010', 'G-002')")
        connection.execute("INSERT INTO execplan_catalog_revisions VALUES ('/one', ?, 'verified', CURRENT_TIMESTAMP)", ("b" * 40,))
        finished = loop_manager._advance_maintenance_cycle_after_merges(connection, "/one", "b" * 40)
        self.assertEqual(finished.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(finished.cycle_base_sha, "a" * 40)
        self.assertEqual(finished.last_cycle_merged_count, 2)
        self.assertEqual(loop_manager.catalog_schedule_decision(connection, "/one").runnable_ready["plan_id"], "G-099")


    @dummy_worker_sessions
    def test_prerequisite_merge_returns_to_outside_ready_without_reassessment(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (
                    ("G-001", "Required First", "READY", (), "normal"),
                    ("G-002", "Outside Cycle", "READY", (), "critical"),
                ),
            )
            planner_calls = 0
            roles: list[str] = []
            selected: list[str | None] = []

            def agent(
                _worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                nonlocal planner_calls
                roles.append(role_name)
                if role_name == "planner":
                    planner_calls += 1
                    outcome = (
                        '{"outcome":"prerequisites","plan_ids":["G-001"]}'
                        if planner_calls == 1
                        else '{"outcome":"no_change"}'
                    )
                    return loop_manager.CodexResult(
                        0, f"MAINTENANCE_RESULT: {outcome}",
                        f"maintenance-session-{planner_calls}",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker",
                    "worker-session-" + str(self._active_plan_id(connection, repo)))

            try:
                first = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                waiting = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                self.assertEqual(
                    waiting.phase,
                    maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                )
                self.assertEqual(waiting.prerequisite_plan_ids, ("G-001",))
                self.assertEqual(selected, ["G-001"])

                self._mark_active_merged(
                    connection, repo_path, repo, "G-001"
                )
                latest = git_command(repo_path, "rev-parse", "origin/main")
                second = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                completed = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                archived_latest = git_command(repo_path, "rev-parse", "origin/main")
                self.assertTrue(loop_manager._is_requirement_archive_only_change(
                    repo_path, latest, archived_latest,
                    verified_plan_declarations=True, allow_execplan_only=True,
                ))
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(
            roles, ["planner", "worker", "worker"]
        )
        self.assertEqual(selected, ["G-001", "G-002"])
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_cycle_commit, archived_latest)
        self.assertEqual(completed.last_cycle_merged_count, 1)


    @dummy_worker_sessions
    def test_refactoring_batch_last_merge_releases_ready_without_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-001", "Ready Feature", "READY", (), "critical"),),
            )
            roles: list[str] = []
            prompts: list[str] = []
            selected: list[str | None] = []

            def agent(
                worktree, _state, _config, role_name, prompt, **_kwargs
            ):
                roles.append(role_name)
                prompts.append(prompt)
                if role_name == "planner":
                    if "post-refactor revalidation" in prompt:
                        return loop_manager.CodexResult(
                            0,
                            'MAINTENANCE_RESULT: {"outcome":"revalidated"}',
                            "revalidation-session",
                        )
                    self._write_refactoring_batch(worktree)
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"refactoring",'
                        '"plan_ids":["G-002"]}',
                        "assessment-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker",
                    "worker-session-" + str(self._active_plan_id(connection, repo)))

            try:
                first = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                refactoring = maintenance_state.MaintenanceRepository(
                    connection
                ).get(repo)
                barrier = connection.execute(
                    "SELECT depends_on FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (repo,),
                ).fetchone()
                self.assertEqual(
                    refactoring.phase,
                    maintenance_state.MaintenancePhase.REFACTORING,
                )
                self.assertEqual(refactoring.refactoring_plan_ids, ("G-002",))
                self.assertEqual(json.loads(barrier["depends_on"]), ["G-002"])
                self.assertEqual(selected, ["G-002"])

                self._mark_active_merged(
                    connection, repo_path, repo, "G-002"
                )
                latest = git_command(repo_path, "rev-parse", "origin/main")
                second = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                completed = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(
            roles, ["planner", "worker", "worker"]
        )
        self.assertFalse(any("post-refactor revalidation" in prompt for prompt in prompts))
        self.assertEqual(selected, ["G-002", "G-001"])
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_cycle_commit, latest)


    def test_preserved_unsafe_post_revalidation_error_blocks_new_model_and_ready(self):
        from tests.support.planning_case import FakePorts, maintenance_cycle, planning_snapshot
        row = planning_snapshot(phase="PLANNING", active_sha="base", planning_error="goal preservation is uncertain",
            planning_error_fingerprint="saved-error", planning_result_completed=True,
            planning_result='MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"goal preservation is uncertain"}')
        cycle = maintenance_cycle(phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha="base", refactoring_plan_ids=("G-001",))
        from dataclasses import replace
        row = replace(row, planning_error_fingerprint=maintenance_planning_fingerprint(cycle, "saved-error"))
        ports = FakePorts(row, cycle)
        ports.git.input_fingerprint = lambda *_args: "saved-error"
        coordinator = planning_runtime.PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(planning_runtime.PlanningContext(repo="/repo", repo_path=Path("/repo"), state_dir=Path("/state"),
            base_branch="main", base_sha="base", provider="codex", config_path=Path("/config.ini"),
            registered_only=True, refactoring_review_interval=20))
        ports.state.suspend_planning = mock.Mock(side_effect=RuntimeError("preserve unsafe result"))
        outcome = coordinator._selection._gate_preserved_error(run)
        self.assertIsNotNone(outcome)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_result, row.planning_result)
        self.assertEqual(ports.state.cycle, cycle)


    def test_legacy_post_cycle_does_not_close_unconfirmed_merge(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_plan(connection, "/one", "G-001", status="PR_OPEN")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = self._legacy_cycle(connection, "/one", "a" * 40)
        refactoring = repository.transition("/one", expected_phase=cycle.phase, expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING, refactoring_plan_ids=("G-001",))
        post = repository.transition("/one", expected_phase=refactoring.phase, expected_revision=refactoring.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION)
        self.assertEqual(loop_manager._advance_maintenance_cycle_after_merges(connection, "/one", "b" * 40), post)
        self.assertIsNone(repository.get("/one").last_cycle_merged_count)
