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
from loop_manager_internal.storage import planning_input_state
from loop_manager_internal.planning import planning_runtime
from loop_manager_internal.catalog.execplan_catalog import plan_content_hash
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError
from tests.support.git import advance_remote_file, commit_and_push, git_command, make_remote_repository
from tests.support.worker import dummy_worker_context
from tests.support.catalog import plan_text


from tests.support.maintenance_cycle_case import MaintenanceCycleCase

class MaintenanceCycleTest(MaintenanceCycleCase):
    def test_first_and_interval_due_are_project_scoped(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        self.assertTrue(repository.due("/one", 2).required)
        self.assertTrue(repository.due("/two", 2).required)

        cycle, created = repository.begin_assessment_if_due(
            "/one", 2, "a" * 40, self_repository=True,
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

    def test_empty_stabilization_selection_settles_or_restarts_without_an_agent(self):
        source = "a" * 40
        context = planning_runtime.PlanningContext(
            repo="/repo", repo_path=Path("/repo"), state_dir=Path("/state"),
            base_branch="main", base_sha=source, provider="codex",
            config_path=Path("/config.ini"), registered_only=True,
            refactoring_review_interval=20,
        )
        row = SimpleNamespace(
            phase=None, planning_error=None, worktree=None,
            planning_fingerprint="planning", specification_fingerprint="specification",
            processed_sha=None,
        )
        cycle = SimpleNamespace(
            phase=maintenance_state.MaintenancePhase.IDLE,
            publication_stage=None,
        )
        decision = SimpleNamespace(
            active=None, has_ready=False, has_waiting=False,
            runnable_ready=None, project_blocked=False,
        )
        ports = mock.Mock()
        del ports.tiered
        ports.state.advance_maintenance_cycle.return_value = cycle
        ports.state.schedule.return_value = decision
        ports.state.execplan_rows.return_value = []
        ports.state.maintenance_due.return_value = maintenance_state.ReviewDue(
            True, "stabilization_due", 10, maintenance_state.MaintenancePhase.IDLE,
        )
        ports.state.settle_empty_stabilization_assessment.return_value = cycle
        ports.git.input_fingerprint.return_value = "planning"
        ports.git.specification_fingerprint.return_value = "specification"
        empty_inventory = object()
        ports.file_review.empty_selection_inventory.return_value = empty_inventory
        coordinator = planning_runtime.PlanningLifecycleCoordinator(ports)
        run = planning_runtime._PlanningRun(context, row, cycle, source, False)

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        ports.state.settle_empty_stabilization_assessment.assert_called_once_with(
            "/repo", 20, source_sha=source, repository_path="/repo",
            base_branch="main", inventory=empty_inventory,
        )
        ports.state.begin_maintenance_assessment.assert_not_called()
        ports.agent.run_planner.assert_not_called()

        ports.file_review.empty_selection_inventory.side_effect = SourceInventoryError(
            "unclassified source"
        )
        failed_run = planning_runtime._PlanningRun(context, row, cycle, source, False)
        failed = coordinator._selection._select_planning_work(failed_run)
        self.assertEqual(
            failed.kind, planning_runtime.PlanningOutcomeKind.REMOTE_BASE_RESTART,
        )
        self.assertEqual(failed.detail, "unclassified source")
        self.assertEqual(ports.state.settle_empty_stabilization_assessment.call_count, 1)

    def test_prepared_batch_persists_new_plan_sets_for_restart_recovery(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = self._legacy_cycle(
            connection, "/one", "a" * 40, include_general_planning=True,
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

        restarted = repository.restart_prepared_batch(
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
        cycle, _ = self._legacy_cycle(connection, "/one", "a" * 40)
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
        maintenance_only, _ = self._legacy_cycle(connection, "/one", "a" * 40)
        combined, _ = self._legacy_cycle(
            connection, "/two", "a" * 40, include_general_planning=True,
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

    def test_maintenance_planner_prompt_shares_quality_guidance_in_each_mode(self):
        for mode in MaintenancePlanMode:
            context = MaintenancePlanningContext(
                repo="/quality-guidance",
                mode=mode,
                active_sha="a" * 40,
                cycle_revision=1,
                cycle_phase="ASSESSMENT",
                eligible_plans=(),
            )
            prompt = loop_manager.maintenance_planner_prompt(
                context,
                "[]",
                "{}",
                include_general_planning=mode is MaintenancePlanMode.BARRIER,
            )
            for signal in (
                "기존 구현·공유 helper·관련 테스트와 표준 도구",
                "현재 목표에 필요하지 않은 추상화·설정·파일 추가",
                "오류 처리·보안·접근성·회귀 검증",
                "결정·예외·실패 흐름과 Acceptance Criteria의 의미",
                "원본을 실제로 읽는 파일별 단위",
                "성공 ledger가 아니다",
            ):
                with self.subTest(mode=mode, signal=signal):
                    self.assertIn(signal, prompt)

    def test_active_legacy_cycle_combines_general_gate_in_one_planner_call(self):
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
            self._legacy_cycle(
                connection, repo, git_command(repo_path, "rev-parse", "origin/main"),
                include_general_planning=True,
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
                with mock.patch.object(
                    loop_manager.notifications, "enqueue_completion_if_ready"
                ) as enqueue:
                    result = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
                project = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertIn("docs/requirements/Active.md", result.detail)
        self.assertEqual(len(prompts), 1)
        self.assertIn("현재 미충족 일반 PRODUCT/Requirement 작업도", prompts[0])
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(cycle.last_success_merged_count, 0)
        self.assertIsNone(project["phase"])
        self.assertIsNotNone(project["planning_fingerprint"])
        enqueue.assert_not_called()

    def test_active_legacy_assessment_runs_before_existing_ready_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-001", "Ready Feature", "READY", (), "critical"),),
            )
            roles: list[str] = []
            prompts: list[str] = []
            selected: list[str | None] = []

            def agent(
                _worktree, _state, _config, role_name, prompt, **_kwargs
            ):
                roles.append(role_name)
                prompts.append(prompt)
                if role_name == "planner":
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"no_change"}',
                        "maintenance-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

            try:
                result = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
                plan = connection.execute(
                    "SELECT status FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(roles, ["planner", "worker"])
        self.assertEqual(selected, ["G-001"])
        self.assertNotIn("현재 미충족 일반 PRODUCT/Requirement 작업도", prompts[0])
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertIsNotNone(cycle.last_success_commit)
        self.assertEqual(plan["status"], "IMPLEMENTING")

    def test_prerequisite_merge_reassesses_before_outside_ready_worker(self):
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
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

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
                latest = self._advance_remote(repo_path, "prerequisite")
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
        # The merged worker changes source code, so full planning checks that
        # input after the maintenance cycle before selecting the next READY.
        self.assertEqual(
            roles, ["planner", "worker", "planner", "planner", "worker"]
        )
        self.assertEqual(selected, ["G-001", "G-002"])
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_success_commit, latest)

    def test_refactoring_batch_selects_immediately_then_revalidates_before_ready(self):
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
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

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
                latest = self._advance_remote(repo_path, "refactoring")
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
        # The remote implementation commit is still a general planning input.
        self.assertEqual(
            roles, ["planner", "worker", "planner", "planner", "worker"]
        )
        self.assertIn("post-refactor revalidation", prompts[2])
        self.assertEqual(selected, ["G-002", "G-001"])
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_success_commit, latest)

    def test_unsafe_revalidation_preserves_cycle_and_blocks_ready_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (
                    ("G-000", "Merged Foundation", "MERGED", (), "normal"),
                    (
                        "G-001",
                        "Ready Feature",
                        "READY",
                        ("G-000",),
                        "critical",
                    ),
                ),
            )
            base_sha = git_command(repo_path, "rev-parse", "origin/main")
            self._enter_revalidation(connection, repo, base_sha)
            roles: list[str] = []

            def agent(
                _worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                roles.append(role_name)
                return loop_manager.CodexResult(
                    0,
                    'MAINTENANCE_RESULT: {"outcome":"unsafe",'
                    '"reason":"goal preservation is uncertain"}',
                    "unsafe-session",
                )

            try:
                result = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
                project = connection.execute(
                    "SELECT phase, planning_error FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                plan = connection.execute(
                    "SELECT status FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(roles, ["planner"])
        self.assertEqual(
            cycle.phase,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        self.assertEqual(project["phase"], "PLANNING")
        self.assertIn("goal preservation is uncertain", project["planning_error"])
        self.assertEqual(plan["status"], "READY")

    def test_remote_target_edit_recovers_unsafe_in_same_session_and_completes_cycle(self):
        # Real Git, catalog, validation and SQLite; only the external Agent is scripted.
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-000", "Merged Foundation", "MERGED", (), "normal"),
                 ("G-001", "Ready Feature", "READY", ("G-000",), "critical")),
            )
            base = git_command(repo_path, "rev-parse", "origin/main")
            self._enter_revalidation(connection, repo, base)
            calls = []
            original = 'MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"old target evidence"}'

            def agent(worktree, _state, _config, role, prompt, **kwargs):
                if role != "planner":
                    return loop_manager.CodexResult(1, "stop worker", "worker-session")
                calls.append((worktree, kwargs.get("session_id"), prompt))
                self.assertLessEqual(len(calls), 2)
                return loop_manager.CodexResult(
                    0, original if len(calls) == 1 else
                    'MAINTENANCE_RESULT: {"outcome":"revalidated"}', "unsafe-session",
                )

            try:
                first = self._run_registered(connection, state_dir, repo_path, agent)
                self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
                saved = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (repo,),
                ).fetchone()
                self.assertEqual(saved["planning_result"], original)
                self.assertTrue(saved["planning_result_completed"])
                self.assertIn("old target evidence", saved["planning_error"])
                self.assertEqual(git_command(Path(saved["worktree"]), "status", "--porcelain"), "")
                # An unchanged retry must retain the completed unsafe result.
                self._run_registered(connection, state_dir, repo_path, agent)
                self.assertEqual(len(calls), 1)
                target = "docs/plans/[G-001] Ready Feature.md"
                updated = (repo_path / target).read_text(encoding="utf-8") + "\nUpdated target evidence.\n"
                latest = advance_remote_file(directory, repo_path, target, updated, "Revise target evidence")
                real_git = loop_manager.git
                pushes = []

                def record_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] == "push":
                        pushes.append(arguments)
                    return real_git(path, *arguments, **kwargs)

                with mock.patch("loop_manager.git", side_effect=record_git):
                    self._run_registered(connection, state_dir, repo_path, agent)
                completed = maintenance_state.MaintenanceRepository(connection).get(repo)
                project = connection.execute(
                    "SELECT planning_error FROM projects WHERE repo = ?", (repo,),
                ).fetchone()
                plan = connection.execute(
                    "SELECT content_hash FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (repo,),
                ).fetchone()
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][0], calls[0][0])
                self.assertEqual(calls[1][1], "unsafe-session")
                self.assertIn(target, calls[1][2])
                self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
                self.assertEqual(completed.last_success_commit, latest)
                self.assertIsNone(project["planning_error"])
                self.assertEqual(plan["content_hash"], plan_content_hash(updated))
                self.assertEqual(pushes, [])
                diagnostics = [p.read_text(encoding="utf-8") for p in
                               (state_dir / "logs").rglob("*.txt")]
                self.assertTrue(any(original in text and saved["planning_error"] in text
                                    and "unsafe-session" in text for text in diagnostics))
            finally:
                connection.close()

    def test_stale_revalidation_snapshot_rejects_result_before_ready_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (
                    ("G-000", "Merged Foundation", "MERGED", (), "normal"),
                    (
                        "G-001",
                        "Ready Feature",
                        "READY",
                        ("G-000",),
                        "critical",
                    ),
                ),
            )
            base_sha = git_command(repo_path, "rev-parse", "origin/main")
            self._enter_revalidation(connection, repo, base_sha)
            roles: list[str] = []

            def agent(
                _worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                roles.append(role_name)
                connection.execute(
                    "UPDATE exec_plans SET workspace_owned = 2 "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (repo,),
                )
                connection.commit()
                return loop_manager.CodexResult(
                    0,
                    'MAINTENANCE_RESULT: {"outcome":"revalidated"}',
                    "stale-session",
                )

            try:
                result = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
                project = connection.execute(
                    "SELECT phase, planning_validation_error "
                    "FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(roles, ["planner"])
        self.assertEqual(
            cycle.phase,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        self.assertEqual(project["phase"], "PLANNING")
        self.assertIn("READY snapshot", project["planning_validation_error"])

    def test_failed_maintenance_push_resumes_saved_result_without_replanning(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-001", "Ready Feature", "READY", (), "critical"),),
            )
            planner_calls = 0
            selected: list[str | None] = []

            def agent(
                worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                nonlocal planner_calls
                if role_name == "planner":
                    planner_calls += 1
                    self.assertEqual(planner_calls, 1)
                    self._write_refactoring_batch(worktree)
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"refactoring",'
                        '"plan_ids":["G-002"]}',
                        "assessment-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

            real_git = loop_manager.git
            failed_pushes = 0

            def fail_first_maintenance_push(path, *arguments, **kwargs):
                nonlocal failed_pushes
                if arguments[:2] == ("push", "origin") and arguments[2:] == (
                    "HEAD:refs/heads/main",
                ):
                    failed_pushes += 1
                    return subprocess.CompletedProcess(
                        ["git"], 1, "injected maintenance push failure"
                    )
                return real_git(path, *arguments, **kwargs)

            try:
                with mock.patch(
                    "loop_manager.git", side_effect=fail_first_maintenance_push
                ):
                    first = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                prepared = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT phase, last_error, planning_result "
                    "FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                self.assertEqual(prepared.publication_stage, "prepared")
                self.assertEqual(project["phase"], "PLANNING")
                self.assertIn("push 실패", project["last_error"])
                self.assertIn("refactoring", project["planning_result"])

                second = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                recovered = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(failed_pushes, 1)
        self.assertEqual(planner_calls, 1)
        self.assertEqual(selected, ["G-002"])
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )

    def test_post_push_sqlite_failure_recovers_prepared_intent_without_repush(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-001", "Ready Feature", "READY", (), "critical"),),
            )
            planner_calls = 0
            selected: list[str | None] = []

            def agent(
                worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                nonlocal planner_calls
                if role_name == "planner":
                    planner_calls += 1
                    self.assertEqual(planner_calls, 1)
                    self._write_refactoring_batch(worktree)
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"refactoring",'
                        '"plan_ids":["G-002"]}',
                        "assessment-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

            real_transition = maintenance_state.MaintenanceRepository.transition

            def fail_refactoring_transition(
                repository, repo_arg, *arguments, **kwargs
            ):
                if (
                    kwargs.get("next_phase")
                    is maintenance_state.MaintenancePhase.REFACTORING
                ):
                    raise maintenance_state.MaintenanceStateError(
                        "injected post-push SQLite failure"
                    )
                return real_transition(
                    repository, repo_arg, *arguments, **kwargs
                )

            try:
                with mock.patch.object(
                    maintenance_state.MaintenanceRepository,
                    "transition",
                    autospec=True,
                    side_effect=fail_refactoring_transition,
                ):
                    first = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                prepared = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT phase, planning_error FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                missing = connection.execute(
                    "SELECT 1 FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (repo,),
                ).fetchone()
                self.assertEqual(prepared.publication_stage, "prepared")
                self.assertEqual(project["phase"], "PLANNING")
                self.assertIn("injected post-push", project["planning_error"])
                self.assertIsNone(missing)

                real_git = loop_manager.git
                recovery_pushes: list[tuple[str, ...]] = []

                def record_recovery_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] == "push":
                        recovery_pushes.append(arguments)
                    return real_git(path, *arguments, **kwargs)

                with mock.patch(
                    "loop_manager.git", side_effect=record_recovery_git
                ):
                    second = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                recovered = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT planning_error FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(recovery_pushes, [])
        self.assertEqual(planner_calls, 1)
        self.assertEqual(selected, ["G-002"])
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertIsNone(project["planning_error"])

    def test_published_combined_batch_recovers_before_changed_fingerprint_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-000", "Merged Foundation", "MERGED", (), "normal"),),
            )
            planner_calls = 0
            selected: list[str | None] = []

            def agent(
                worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                nonlocal planner_calls
                if role_name == "planner":
                    planner_calls += 1
                    self.assertEqual(planner_calls, 1)
                    plans_dir = worktree / "docs/plans"
                    (plans_dir / "[G-001] Extract Boundary.md").write_text(
                        plan_text(
                            "G-001",
                            "Extract Boundary",
                            depends_on=("G-000",),
                        ),
                        encoding="utf-8",
                    )
                    (plans_dir / "[G-002] Pending Capability.md").write_text(
                        plan_text(
                            "G-002",
                            "Pending Capability",
                            depends_on=("G-001",),
                            purpose=(
                                "Deliver a pending product capability after the "
                                "structural boundary is available."
                            ),
                            acceptance_criteria=(
                                "Preserve the public API and expose the queued "
                                "product behavior."
                            ),
                            implementation=(
                                "Implement the product behavior on the extracted "
                                "module boundary."
                            ),
                        ),
                        encoding="utf-8",
                    )
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"refactoring",'
                        '"plan_ids":["G-001"]}',
                        "combined-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

            real_transition = maintenance_state.MaintenanceRepository.transition

            def fail_refactoring_transition(
                repository, repo_arg, *arguments, **kwargs
            ):
                if (
                    kwargs.get("next_phase")
                    is maintenance_state.MaintenancePhase.REFACTORING
                ):
                    raise maintenance_state.MaintenanceStateError(
                        "injected combined SQLite interruption"
                    )
                return real_transition(
                    repository, repo_arg, *arguments, **kwargs
                )

            try:
                with mock.patch.object(
                    maintenance_state.MaintenanceRepository,
                    "transition",
                    autospec=True,
                    side_effect=fail_refactoring_transition,
                ), mock.patch(
                    "loop_manager_internal.planning.tiered_assessment.enabled",
                    return_value=False,
                ):
                    first = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                prepared = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT worktree, planning_attempt_fingerprint "
                    "FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                prepared_worktree = Path(project["worktree"])
                plan_commit = str(prepared.publication_commit_sha)
                self.assertTrue(prepared.include_general_planning)
                self.assertEqual(prepared.publication_stage, "prepared")
                self.assertTrue(prepared_worktree.is_dir())

                git_command(repo_path, "fetch", "origin")
                git_command(repo_path, "merge", "--ff-only", "origin/main")
                product = repo_path / "docs/PRODUCT.md"
                product.write_text(
                    product.read_text(encoding="utf-8")
                    + "\nNew product input after publication.\n",
                    encoding="utf-8",
                )
                commit_and_push(repo_path, "Advance combined planning input")
                latest = git_command(repo_path, "rev-parse", "origin/main")
                self.assertNotEqual(
                    loop_manager._planning_input_fingerprint(repo_path, plan_commit),
                    loop_manager._planning_input_fingerprint(repo_path, latest),
                )

                real_git = loop_manager.git
                recovery_pushes: list[tuple[str, ...]] = []

                def record_recovery_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] == "push":
                        recovery_pushes.append(arguments)
                    return real_git(path, *arguments, **kwargs)

                with mock.patch(
                    "loop_manager.git", side_effect=record_recovery_git
                ), mock.patch(
                    "loop_manager_internal.planning.tiered_assessment.enabled",
                    return_value=False,
                ):
                    second = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                recovered = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT processed_sha, planning_fingerprint "
                    "FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                general = connection.execute(
                    "SELECT depends_on FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(planner_calls, 1)
        self.assertEqual(recovery_pushes, [])
        self.assertFalse(prepared_worktree.exists())
        self.assertEqual(selected, ["G-001"])
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(recovered.refactoring_plan_ids, ("G-001",))
        self.assertEqual(recovered.revalidation_plan_ids, ("G-002",))
        self.assertEqual(project["processed_sha"], plan_commit)
        self.assertIsNotNone(project["planning_fingerprint"])
        self.assertEqual(json.loads(general["depends_on"]), ["G-001"])

    def test_active_or_due_cycle_blocks_project_completion_notification(self):
        connection = self._connection()
        connection.execute(
            "UPDATE projects SET planning_fingerprint = 'cycle-one', "
            "specification_fingerprint = 'spec-one', processed_sha = ? WHERE repo = '/one'",
            ("a" * 40,),
        )
        planning_input_state.record_general(connection, "/one", "a" * 40, {})
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = self._legacy_cycle(connection, "/one", "a" * 40)
        with mock.patch.object(loop_manager, "_run_project_step", return_value=0), mock.patch.object(
            loop_manager.notifications, "enqueue_completion_if_ready"
        ) as enqueue, mock.patch.object(
            loop_manager, "_planning_input_fingerprint", return_value="cycle-one"
        ), mock.patch.object(
            loop_manager, "_specification_input_fingerprint", return_value="spec-one"
        ), mock.patch.object(
            loop_manager, "_git_tree_entries", return_value={}
        ), mock.patch.object(loop_manager, "git") as git:
            git.return_value.stdout = "a" * 40
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
            enqueue.assert_called_once_with(connection, "/one", mock.ANY)
