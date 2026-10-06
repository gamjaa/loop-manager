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
from loop_manager_internal.storage import planning_review_state
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

    def test_completion_candidate_does_not_create_an_empty_stabilization_cycle(self):
        from tests.support.planning_case import FakePorts, maintenance_cycle, planning_snapshot
        ports = FakePorts(planning_snapshot(processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification"), maintenance_cycle(last_cycle_merged_count=0))
        ports.state.maintenance_due_required = False
        ports.state.maintenance_due_reason = "interval_pending"
        ports.file_review = mock.Mock()
        context = planning_runtime.PlanningContext(repo="/repo", repo_path=Path("/repo"), state_dir=Path("/state"),
            base_branch="main", base_sha="base", provider="codex", config_path=Path("/config.ini"), registered_only=True,
            refactoring_review_interval=20)
        coordinator = planning_runtime.PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(context)
        coordinator._selection._select_planning_work(run)
        self.assertEqual(ports.state.maintenance_begin_count, 0)
        ports.file_review.empty_selection_inventory.assert_not_called()
        self.assertFalse(run.maintenance_planning)
        self.assertEqual(ports.agent.run_count, 0)

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
        self.assertEqual(selected, ["G-001", "G-002"])
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_cycle_commit, latest)

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
                        encoding="utf-8", newline="\n",
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
                        encoding="utf-8", newline="\n",
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
        repo = str(Path("/one").resolve())
        connection.execute("UPDATE projects SET repo = ? WHERE repo = '/one'", (repo,))
        connection.execute(
            "UPDATE projects SET planning_fingerprint = 'cycle-one', "
            "specification_fingerprint = 'spec-one', processed_sha = ? WHERE repo = ?",
            ("a" * 40, repo),
        )
        planning_input_state.record_general(connection, repo, "a" * 40, {})
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = self._legacy_cycle(connection, repo, "a" * 40)

        def record_final_review():
            # The mocked project step skips Planner; arrange its confirmed receipt.
            revision = "a" * 40
            work_cycle = planning_review_state.work_cycle(connection, repo)
            planning_review_state.begin(
                connection, repo, purpose="final", revision=revision,
                input_fingerprint="cycle-one", specification_fingerprint="spec-one",
                work_cycle=work_cycle, comparison_revision=None,
            )
            result = "FINAL_SPECIFICATION_REVIEW: " + json.dumps({
                "revision": revision, "input_fingerprint": "cycle-one",
                "specification_fingerprint": "spec-one", "work_cycle": work_cycle,
                "outcome": "fulfilled", "evidence": ["docs/PRODUCT.md", "src/app.py"],
                "reason": "Full specifications and implementation reviewed",
            })
            planning_review_state.validate(connection, repo, revision, result, "fulfilled")
            planning_review_state.confirm(connection, repo, revision, revision)

        record_final_review()
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
                repo,
                Path("/config.ini"),
                None,
                False,
            )
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
            enqueue.assert_not_called()

            repository.advance_success_baseline(
                repo,
                expected_phase=cycle.phase,
                expected_revision=cycle.revision,
                remote_commit="a" * 40,
            )
            # Maintenance closes independently and invalidates the earlier receipt.
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                repo,
                Path("/config.ini"),
                None,
                False,
            )
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
            enqueue.assert_not_called()

            record_final_review()
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                repo,
                Path("/config.ini"),
                None,
                False,
            )
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.COMPLETED)
            enqueue.assert_called_once_with(connection, repo, mock.ANY)


class SinglePassMaintenanceCycleTest(MaintenanceCycleCase):
    def test_legacy_prerequisite_hold_waits_without_reassessment_after_restart(self):
        from tests.support.planning_case import FakePorts, planning_snapshot
        for status in ("READY", "MERGED"):
            for held_id in ("G-001", "G-002"):
                with self.subTest(status=status, held_id=held_id):
                    self.connection.execute("DELETE FROM maintenance_cycles")
                    self.connection.execute("DELETE FROM exec_plans")
                    self.connection.execute("DELETE FROM execplan_catalog_holds")
                    dependency_status = "MERGED" if held_id == "G-002" else status
                    self._insert_plan(self.connection, "/one", "G-001", status=dependency_status)
                    self._insert_plan(self.connection, "/one", "G-002", status=status, depends_on=("G-001",))
                    self._insert_plan(self.connection, "/one", "G-099", priority="critical")
                    self._legacy_cycle(self.connection, "/one", self.source, include_general_planning=True)
                    self.connection.execute("UPDATE maintenance_cycles SET prerequisite_plan_ids = '[\"G-002\"]', "
                        "full_assessment_completed = 1, full_assessment_source_sha = ?, full_assessment_result = ?",
                        (self.source, '{"evidence":"saved"}'))
                    original = self.repository.get("/one")
                    self.assertIsNone(original.cycle_id)
                    self.connection.execute("INSERT INTO execplan_catalog_holds VALUES ('/one', ?, ?, '{}', NULL)",
                        (held_id, self.latest))
                    self.catalog()
                    self.connection.commit()
                    context = planning_runtime.PlanningContext(
                        repo="/one", repo_path=Path("/one"), state_dir=Path("/state"),
                        base_branch="main", base_sha=self.latest, provider="codex",
                        config_path=Path("/config.ini"), registered_only=True,
                        refactoring_review_interval=20)
                    connection = self.connection
                    for restart in (False, True):
                        if restart:
                            connection = sqlite3.connect(":memory:")
                            connection.row_factory = sqlite3.Row
                            self.addCleanup(connection.close)
                            self.connection.backup(connection)
                        repository = maintenance_state.MaintenanceRepository(connection)
                        ports = FakePorts(planning_snapshot(repo="/one"), repository.get("/one"))
                        ports.state.maintenance_cycle = repository.get
                        ports.state.advance_maintenance_cycle = lambda repo, sha: loop_manager._advance_maintenance_cycle_after_merges(connection, repo, sha)
                        ports.state.schedule = lambda repo: loop_manager.catalog_schedule_decision(connection, repo)
                        ports.state.execplan_rows = repository.plan_rows
                        ports.state.activate_ready = mock.Mock(side_effect=ports.state.activate_ready)
                        outcome = planning_runtime.PlanningLifecycleCoordinator(ports).run(context)
                        self.assertEqual(outcome.kind, planning_runtime.PlanningOutcomeKind.WAITING)
                        self.assertEqual(ports.agent.run_count, 0)
                        self.assertEqual(ports.state.maintenance_begin_count, 0)
                        self.assertEqual(ports.state.updates, [])
                        ports.state.activate_ready.assert_not_called()
                        waiting = repository.get("/one")
                        self.assertEqual(waiting.phase, maintenance_state.MaintenancePhase.PREREQUISITE_WAIT)
                        self.assertEqual(waiting.revision, original.revision + 1)
                        self.assertEqual(waiting.prerequisite_plan_ids, original.prerequisite_plan_ids)
                        self.assertEqual(waiting.full_assessment_result, original.full_assessment_result)
                        self.assertEqual(waiting.full_assessment_source_sha, original.full_assessment_source_sha)
                        self.assertEqual(waiting.last_success_commit, original.last_success_commit)
                        self.assertIsNone(waiting.last_cycle_commit)
                        self.assertIsNone(waiting.last_cycle_merged_count)
                        self.assertFalse(waiting.include_general_planning)
                        self.assertTrue(any("dependency" in message for _level, message in ports.events.messages))
                    connection.execute("DELETE FROM execplan_catalog_holds")
                    connection.commit()
                    resumed = loop_manager._advance_maintenance_cycle_after_merges(connection, "/one", self.latest)
                    selected = loop_manager.catalog_schedule_decision(connection, "/one").runnable_ready
                    if status == "READY":
                        self.assertEqual(resumed, waiting)
                        self.assertEqual(selected["plan_id"], "G-002" if dependency_status == "MERGED" else "G-001")
                    else:
                        self.assertEqual(resumed.phase, maintenance_state.MaintenancePhase.IDLE)
                        self.assertEqual(resumed.last_cycle_merged_count, 2)
                        self.assertEqual(selected["plan_id"], "G-099")

    def test_catalog_hold_keeps_merged_scope_waiting_until_catalog_is_resolved(self):
        self._insert_plan(self.connection, "/one", "G-001", status="MERGED")
        cycle = self.start(maintenance_state.MaintenancePhase.REFACTORING, ("G-001",))
        self.catalog()
        self.connection.execute("INSERT INTO execplan_catalog_holds VALUES ('/one', 'G-001', ?, '{}', NULL)", (self.latest,))
        self.connection.commit()
        self.assertEqual(self.advance(), cycle)
        with self.assertRaisesRegex(maintenance_state.MaintenanceStateError, "catalog 보류"):
            self.repository.finish_cycle("/one", expected=cycle, source_sha=self.latest)
        self.connection.execute("DELETE FROM execplan_catalog_holds")
        self.connection.commit()
        self.assertEqual(self.advance().phase, maintenance_state.MaintenancePhase.IDLE)

    def test_legacy_release_intent_survives_restart_and_rolls_back_with_planning_clear(self):
        from types import SimpleNamespace
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository, RuntimeStateError
        self._legacy_cycle(self.connection, "/one", self.source)
        self.connection.execute("UPDATE maintenance_cycles SET phase = 'post_refactor_revalidation'")
        self.connection.execute("UPDATE projects SET phase = 'PLANNING', active_sha = ?, worktree = '/managed/planning', "
            "planning_workspace_owned = 1, planning_attempt_fingerprint = 'saved', planner_session_id = 'saved-session' WHERE repo = '/one'", (self.source,))
        self.connection.commit()
        runtime = RuntimeStateRepository(self.connection)
        expected = runtime.planning("/one")
        effects = SimpleNamespace(_clear_active_in_current_transaction=loop_manager._clear_active_in_current_transaction)
        port = _PlanningStatePorts(effects, self.connection, runtime, self.repository, Path("/state"))
        pending = port.prepare_legacy_release(expected, self.repository.get("/one"))
        self.assertIsNotNone(pending.legacy_release_identity)
        self.assertFalse(self.connection.in_transaction)
        with self.assertRaisesRegex(maintenance_state.MaintenanceStateError, "workspace 해제"):
            self.repository.finish_cycle("/one", expected=pending, source_sha=self.latest)
        restarted = sqlite3.connect(":memory:")
        restarted.row_factory = sqlite3.Row
        self.addCleanup(restarted.close)
        self.connection.backup(restarted)
        repository = maintenance_state.MaintenanceRepository(restarted)
        runtime = RuntimeStateRepository(restarted)
        def interrupted_clear(*args, **kwargs):
            loop_manager._clear_active_in_current_transaction(*args, **kwargs)
            raise KeyboardInterrupt("before transaction commit")
        effects._clear_active_in_current_transaction = interrupted_clear
        port = _PlanningStatePorts(effects, restarted, runtime, repository, Path("/state"))
        with self.assertRaises(KeyboardInterrupt):
            port.complete_legacy_release(expected, pending)
        self.assertEqual(repository.get("/one"), pending)
        self.assertEqual(runtime.planning("/one"), expected)
        restarted.execute("UPDATE projects SET active_sha = ? WHERE repo = '/one'", (self.latest,))
        restarted.commit()
        with self.assertRaises(RuntimeStateError):
            port.complete_legacy_release(expected, pending)
        self.assertIsNotNone(repository.get("/one").legacy_release_identity)
        restarted.execute("UPDATE projects SET active_sha = ? WHERE repo = '/one'", (self.source,))
        restarted.execute("UPDATE maintenance_cycles SET revision = revision + 1")
        restarted.commit()
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            port.complete_legacy_release(expected, pending)
        self.assertEqual(runtime.planning("/one"), expected)
        restarted.execute("UPDATE maintenance_cycles SET revision = ?", (pending.revision,))
        restarted.commit()
        effects._clear_active_in_current_transaction = loop_manager._clear_active_in_current_transaction
        port.complete_legacy_release(expected, pending)
        self.assertIsNone(runtime.planning("/one").phase)
        self.assertIsNone(repository.get("/one").legacy_release_identity)
        self.assertIsNone(repository.get("/one").last_cycle_merged_count)

    def setUp(self):
        self.connection = self._connection()
        self.addCleanup(self.connection.close)
        self.repository = maintenance_state.MaintenanceRepository(self.connection)
        self.source = "a" * 40
        self.latest = "b" * 40

    def catalog(self, source=None):
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions (repo, revision_sha, catalog_fingerprint) "
            "VALUES ('/one', ?, 'verified') ON CONFLICT(repo) DO UPDATE SET revision_sha = excluded.revision_sha",
            (source or self.latest,),
        )

    def advance(self):
        return loop_manager._advance_maintenance_cycle_after_merges(self.connection, "/one", self.latest)

    def start(self, phase, ids):
        cycle, _ = self.repository.begin_assessment_if_due("/one", 20, self.source)
        arguments = {"prerequisite_plan_ids" if phase is maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
                     else "refactoring_plan_ids": ids}
        return self.repository.transition("/one", expected_phase=cycle.phase, expected_revision=cycle.revision,
                                          next_phase=phase, **arguments)

    def test_prerequisite_and_final_merge_close_without_new_assessment_or_receipt(self):
        for phase in (maintenance_state.MaintenancePhase.PREREQUISITE_WAIT, maintenance_state.MaintenancePhase.REFACTORING):
            with self.subTest(phase=phase):
                self.connection.execute("DELETE FROM maintenance_cycles")
                self.connection.execute("DELETE FROM exec_plans")
                self._insert_plan(self.connection, "/one", "G-001")
                started = self.start(phase, ("G-001",))
                self.assertEqual(self.advance(), started)
                self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
                self.catalog()
                finished = self.advance()
                self.assertEqual(finished.phase, maintenance_state.MaintenancePhase.IDLE)
                self.assertEqual(finished.last_cycle_merged_count, 1)
                self.assertEqual(finished.last_cycle_commit, self.latest)
                self.assertEqual(finished.cycle_base_sha, self.source)
                self.assertIsNone(finished.last_success_commit)
                self.assertFalse(self.repository.file_assessment_due("/one", 20).required)
                self.assertEqual(self.advance(), finished)

    def test_required_dependency_closure_runs_before_refactor_and_preserves_barrier(self):
        self._insert_plan(self.connection, "/one", "G-001")
        self._insert_plan(self.connection, "/one", "G-002", depends_on=("G-001",))
        self._insert_plan(self.connection, "/one", "G-099", priority="critical")
        cycle = self.start(maintenance_state.MaintenancePhase.REFACTORING, ("G-002",))
        self.assertEqual(loop_manager.catalog_schedule_decision(self.connection, "/one").runnable_ready["plan_id"], "G-001")
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE plan_id = 'G-001'")
        self.assertEqual(self.advance(), cycle)
        self.assertEqual(loop_manager.catalog_schedule_decision(self.connection, "/one").runnable_ready["plan_id"], "G-002")
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE plan_id = 'G-002'")
        self.catalog()
        self.advance()
        self.assertEqual(loop_manager.catalog_schedule_decision(self.connection, "/one").runnable_ready["plan_id"], "G-099")

    def test_legacy_post_phase_closes_only_after_merge_and_publication_confirmation(self):
        self._insert_plan(self.connection, "/one", "G-001", status="PLAN_BLOCKED")
        cycle = self.start(maintenance_state.MaintenancePhase.REFACTORING, ("G-001",))
        post = self.repository.transition("/one", expected_phase=cycle.phase, expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION)
        self.assertEqual(self.advance(), post)
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        self.connection.execute("UPDATE maintenance_cycles SET publication_stage = 'prepared'")
        pending = self.repository.get("/one")
        self.assertEqual(self.advance(), pending)
        self.assertIsNone(pending.last_cycle_merged_count)
        self.connection.execute("UPDATE maintenance_cycles SET publication_stage = NULL")
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            self.advance()  # A Merge row alone cannot replace verified remote catalog observation.
        self.catalog()
        finished = self.advance()
        self.assertEqual(finished.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(finished.refactoring_plan_ids, ("G-001",))

    def test_existing_finding_plan_waits_for_required_merges(self):
        self._insert_plan(self.connection, "/one", "G-001", status="PR_OPEN")
        cycle, _ = self.repository.begin_assessment_if_due("/one", 20, self.source)
        self.repository.record_assessment_plans("/one", cycle.revision, ("G-001",))
        executing = self.repository.advance_success_baseline("/one", expected_phase=cycle.phase,
            expected_revision=cycle.revision, remote_commit=self.source)
        self.assertEqual(executing.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(self.advance(), executing)
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        self.catalog()
        self.assertEqual(self.advance().last_cycle_merged_count, 1)

    def test_interval_uses_end_count_and_requests_are_bound_once_across_restart(self):
        from loop_manager_internal.maintenance import tiered_review
        tiered_review.ensure_schema(self.connection)
        self._insert_plan(self.connection, "/one", "G-001", status="MERGED")
        self.start(maintenance_state.MaintenancePhase.REFACTORING, ("G-001",))
        self.catalog()
        self.advance()
        for number in range(2, 21):
            self._insert_plan(self.connection, "/one", f"G-{number:03d}", status="MERGED")
        self.assertFalse(self.repository.file_assessment_due("/one", 20, completion_candidate=True).required)
        self._insert_plan(self.connection, "/one", "G-021", status="MERGED")
        self.assertTrue(self.repository.file_assessment_due("/one", 20).required)
        self.connection.execute("INSERT INTO tiered_maintenance_requests VALUES ('/one', 'request-1')")
        cycle, started = self.repository.begin_assessment_if_due("/one", 20, self.latest)
        self.assertTrue(started)
        self.assertEqual(cycle.cycle_request_id, "request-1")
        resumed, started = maintenance_state.MaintenanceRepository(self.connection).begin_assessment_if_due("/one", 20, self.latest)
        self.assertFalse(started)
        self.assertEqual(cycle, resumed)
        self.repository.advance_success_baseline("/one", expected_phase=cycle.phase, expected_revision=cycle.revision, remote_commit=self.latest)
        self.assertFalse(self.repository.file_assessment_due("/one", 20).required)
        self.connection.execute("UPDATE tiered_maintenance_requests SET request_id = 'request-2'")
        self.assertEqual(self.repository.file_assessment_due("/one", 20).reason, "requested_full")

    def test_legacy_idle_boundary_migrates_without_rewriting_assessment_source(self):
        self.connection.execute("ALTER TABLE maintenance_cycles DROP COLUMN last_cycle_merged_count")
        self.connection.execute("ALTER TABLE maintenance_cycles DROP COLUMN last_cycle_commit")
        self._insert_plan(self.connection, "/one", "G-001", status="MERGED")
        self.connection.execute("INSERT INTO maintenance_cycles (repo, last_success_commit, last_success_merged_count) VALUES ('/one', ?, 1)", (self.source,))
        self.connection.commit()
        restarted = maintenance_state.MaintenanceRepository(self.connection)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(restarted.get("/one").last_cycle_commit, self.source)
        self.assertFalse(restarted.file_assessment_due("/one", 20).required)
        self.assertEqual(restarted.get("/one").last_success_commit, self.source)

    def test_unverifiable_archived_merge_cannot_close_scope(self):
        self._insert_plan(self.connection, "/one", "G-001", status="MERGED")
        self.start(maintenance_state.MaintenancePhase.REFACTORING, ("G-001",))
        self._archive_plan(self.connection, "/one", "G-001")
        self.connection.execute("UPDATE execplan_catalog_history SET merge_confirmed = 0")
        self.catalog()
        from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
        with self.assertRaises(MaintenancePlanningError):
            self.advance()
        self.assertIsNone(self.repository.get("/one").last_cycle_merged_count)

    def test_closure_commit_rolls_back_on_interruption(self):
        self._insert_plan(self.connection, "/one", "G-001", status="MERGED")
        active = self.start(maintenance_state.MaintenancePhase.REFACTORING, ("G-001",))
        self.catalog()
        self.connection.execute("CREATE TRIGGER fail_close BEFORE UPDATE ON maintenance_cycles WHEN NEW.phase = 'idle' BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.advance()
        self.assertEqual(self.repository.get("/one"), active)
        self.connection.execute("DROP TRIGGER fail_close")
        self.assertEqual(self.advance().last_cycle_merged_count, 1)

    def test_last_merge_keeps_original_tiered_success_bytes_and_full_source(self):
        from loop_manager_internal.maintenance import tiered_review
        self._insert_plan(self.connection, "/one", "G-001", status="MERGED")
        self.start(maintenance_state.MaintenancePhase.REFACTORING, ("G-001",))
        tiered_review.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO tiered_maintenance_success VALUES ('/one', '{}', '[]', ?, 0, ?, 'original-receipt')", (self.source, self.source))
        before = tuple(self.connection.execute("SELECT * FROM tiered_maintenance_success").fetchone())
        self.catalog()
        self.advance()
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM tiered_maintenance_success").fetchone()), before)
        self.assertFalse(self.repository.file_assessment_due("/one", 20).required)

    def test_new_request_during_active_cycle_is_preserved_for_next_cycle(self):
        from loop_manager_internal.maintenance import tiered_review
        tiered_review.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO tiered_maintenance_requests VALUES ('/one', 'first-request')")
        cycle, _ = self.repository.begin_assessment_if_due("/one", 20, self.source)
        self.connection.execute("UPDATE tiered_maintenance_requests SET request_id = 'next-request'")
        resumed, created = self.repository.begin_assessment_if_due("/one", 20, self.latest)
        self.assertFalse(created)
        self.assertEqual(resumed, cycle)
        self.repository.advance_success_baseline("/one", expected_phase=cycle.phase,
            expected_revision=cycle.revision, remote_commit=self.source)
        self.assertEqual(self.repository.file_assessment_due("/one", 20).reason, "requested_full")
        next_cycle, created = self.repository.begin_assessment_if_due("/one", 20, self.latest)
        self.assertTrue(created)
        self.assertEqual(next_cycle.cycle_request_id, "next-request")
        self.assertNotEqual(next_cycle.cycle_id, cycle.cycle_id)

    def test_prerequisite_scope_survives_artifact_publication_and_waits_for_merge(self):
        from loop_manager_internal.maintenance.maintenance_publication import (
            _apply_maintenance_planning_batch, ApplyMaintenancePlanningBatchEffects,
            _maintenance_publication_already_applied, MaintenancePublicationAlreadyAppliedEffects,
        )
        from loop_manager_internal.application.contracts import ValidatedPlanningResult
        self._insert_plan(self.connection, "/one", "G-001")
        cycle, _ = self.repository.begin_assessment_if_due("/one", 20, self.source)
        self.repository.record_assessment_plans("/one", cycle.revision, (), ("G-001",))
        context = MaintenancePlanningContext("/one", MaintenancePlanMode.BARRIER, self.source, cycle.revision, "assessment", ())
        result = ValidatedPlanningResult((), ("docs/maintenance/code-map.jsonl",), maintenance_context=context)
        effects = ApplyMaintenancePlanningBatchEffects(**{name: mock.Mock() for name in ApplyMaintenancePlanningBatchEffects.__dataclass_fields__})
        effects._maintenance_publication_already_applied.return_value = False
        effects.plan_declarations.return_value = {}
        published = _apply_maintenance_planning_batch(effects, self.connection, "/one", Path("/one"), "main", result, self.latest)
        self.assertEqual(published.phase, maintenance_state.MaintenancePhase.PREREQUISITE_WAIT)
        self.assertEqual(published.prerequisite_plan_ids, ("G-001",))
        self.assertIsNone(published.last_cycle_merged_count)
        repeated = MaintenancePublicationAlreadyAppliedEffects(mock.Mock(return_value=True), mock.Mock(return_value=False))
        self.assertTrue(_maintenance_publication_already_applied(repeated, self.connection, "/one", Path("/one"), result, self.latest))
        self.assertEqual(self.advance(), published)
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        self.catalog()
        self.assertEqual(self.advance().last_cycle_merged_count, 1)

    def test_legacy_assessment_after_prerequisite_merge_does_not_restart_evaluation(self):
        self._insert_plan(self.connection, "/one", "G-001")
        cycle, _ = self._legacy_cycle(self.connection, "/one", self.source)
        self.repository.record_assessment_plans("/one", cycle.revision, (), ("G-001",))
        resumed = self.advance()
        self.assertEqual(resumed.phase, maintenance_state.MaintenancePhase.PREREQUISITE_WAIT)
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        self.catalog()
        self.assertEqual(self.advance().phase, maintenance_state.MaintenancePhase.IDLE)
