from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance.maintenance_file_inventory import RootPlacement, TrackedSource
from tests.support.maintenance import result_for
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from loop_manager_internal.maintenance.maintenance_orchestration import maintenance_planning_fingerprint
from loop_manager_internal.planning.planning_runtime import (
    PlanningAgentResult,
    PlanningAgentResultKind,
    PlanningContext,
    PlanningLifecycleCoordinator,
    PlanningOutcomeKind,
    PlanningResultInvalid,
    PlanningScopeResult,
    PlanningWorkspaceOwnershipError,
    PlanningDecisionKind,
    choose_planning_decision,
)
from loop_manager_internal.storage.runtime_state import PlanningSnapshot
from loop_manager_internal.maintenance import maintenance_file_inventory


from tests.support.planning_case import (
    PlanningCase, FakePorts, FakeStatePort, FakeGitPort, FakeAgentPort,
    FakeValidationPort, FakeEventPort, planning_snapshot, maintenance_cycle,
)

class PlanningAssessmentTest(PlanningCase):
    def test_corrupt_saved_assessment_stops_before_agent_invocation(self):
        revision = "a" * 40
        ports = FakePorts(
            planning_snapshot(phase="PLANNING", active_sha=revision),
            maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                cycle_base_sha=revision, revision=4,
                full_assessment_result='{"source_revision":"tampered"}',
            ),
        )
        ports.validation.maintenance_prompt = mock.Mock(return_value="legacy prompt")
        ports.git.head = revision
        ports.assessment = mock.Mock()
        ports.assessment.inventory.return_value = {"module.py": "b" * 64}
        ports.assessment.prompt.side_effect = maintenance_file_inventory.AssessmentError(
            "저장된 보고서가 현재 source와 다릅니다."
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(replace(self.context, self_repository=True))
        run.planning_worktree = Path("/worktree")
        run.maintenance_context = object()

        outcome = coordinator._assessment._run_planner_turn(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 1)
        self.assertEqual(ports.state.snapshot.planning_result_pending_validation, 0)

    def test_assessment_port_rejects_invalid_report_without_completing_cycle(self):
        revision = "a" * 40
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha=revision, revision=1,
        )
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha=revision,
            planning_result='MAINTENANCE_RESULT: {"outcome":"no_change"}',
            planning_result_pending_validation=True,
        ), cycle)
        ports.validation.validate_maintenance_result = mock.Mock(return_value=None)
        ports.assessment = mock.Mock()
        ports.assessment.inventory.return_value = {"docs/PRODUCT.md": "hash"}
        ports.assessment.validate.side_effect = maintenance_file_inventory.AssessmentError(
            "invalid report"
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(replace(self.context, self_repository=True))
        run.planning_worktree = Path("/planning")
        run.maintenance_context = object()

        outcome = coordinator._assessment._validate_planner_result(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        ports.assessment.inventory.assert_called_once_with(Path("/repo"), revision)
        ports.assessment.validate.assert_called_once()
        self.assertIn("invalid report", ports.state.snapshot.planning_validation_error)

    def test_assessment_resume_restarts_selection_if_remote_base_changes(self):
        ports = FakePorts(planning_snapshot(), maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT, cycle_base_sha="base",
        ))
        ports.state.schedule_result.runnable_ready = {"plan_id": "G-058"}
        ports.state.activate_ready = mock.Mock()
        ports.git.remote = "new-base"
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._selection._select_planning_work(coordinator._new_run(self.context))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        ports.state.activate_ready.assert_not_called()
        self.assertEqual(ports.state.updates, [])

    def test_due_and_unpublished_assessment_yield_without_advancing_success(self):
        for phase in (maintenance_state.MaintenancePhase.IDLE,
                      maintenance_state.MaintenancePhase.ASSESSMENT):
            with self.subTest(phase=phase):
                cycle = maintenance_cycle(phase=phase, cycle_base_sha="base",
                    last_success_merged_count=5, last_success_file_assessment_merged_count=5)
                ports = FakePorts(planning_snapshot(), cycle)
                ports.state.maintenance_due_required = True
                ports.state.schedule_result.has_ready = True
                ports.state.schedule_result.runnable_ready = {"plan_id": "G-058"}
                ports.state.activate_result = {"plan_id": "G-058"}
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(replace(self.context, registered_only=True))
                run.base_sha = "base"
                outcome = coordinator._selection._select_planning_work(run)
                self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
                self.assertEqual(ports.state.cycle.last_success_merged_count, 5)
                self.assertEqual(ports.state.cycle.last_success_file_assessment_merged_count, 5)
                self.assertEqual(ports.state.updates, [])
                self.assertEqual(ports.state.snapshot.phase, "WORKING")

    def test_assessment_reconciles_one_relation_before_final_planner(self):
        left_content = b"def calculate(): return helper()\n"
        right_content = b"def helper(): return 1\n"
        left = TrackedSource("src/a.py", hashlib.sha256(left_content).hexdigest(),
                             "100644", "a" * 40, "source code")
        right = TrackedSource("src/b.py", hashlib.sha256(right_content).hexdigest(),
                              "100644", "a" * 40, "source code")
        relation = maintenance_reconciliation.Relation("relation-key", left, right, ())
        ports = FakePorts(
            planning_snapshot(phase="PLANNING", active_sha="a" * 40),
            maintenance_cycle(phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                              cycle_base_sha="a" * 40, revision=4,
                              file_assessment_start_merged_count=0),
        )
        ports.file_review = SimpleNamespace(progress=mock.Mock(return_value=
            maintenance_file_review.FileReviewProgress((left,), ({"path": left.path},), None)))
        ports.reconciliation = SimpleNamespace(
            progress=mock.Mock(return_value=maintenance_reconciliation.ReconciliationProgress(
                (left,), (), (relation,), relation)),
            content=mock.Mock(side_effect=[left_content, right_content,
                                           left_content, right_content]),
            start=mock.Mock(), unit=mock.Mock(return_value=("saved-relation-session", None)),
            session=mock.Mock(), complete=mock.Mock(), fail=mock.Mock(),
        )
        ports.agent.result = PlanningAgentResult(
            0, maintenance_reconciliation.RELATION_PREFIX + json.dumps({
                "key": relation.key, "status": "confirmed",
                "left_excerpt": "helper()", "right_excerpt": "def helper()",
                "reason": "The caller resolves to this helper's implementation.",
            }), "saved-relation-session", PlanningAgentResultKind.SUCCESS,
        )
        ports.git.head = "a" * 40
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/worktree")

        outcome = coordinator._assessment._run_planner_turn(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.resumed_session, "saved-relation-session")
        self.assertEqual(ports.reconciliation.complete.call_count, 1)
        self.assertEqual(ports.reconciliation.fail.call_count, 0)

    def test_assessment_review_unit_precedes_planner_and_preserves_session(self):
        content = b"def calculate(): return 1\n"
        source = TrackedSource(
            "src/calc.py", hashlib.sha256(content).hexdigest(), "100644",
            "a" * 40, "source code",
        )
        ports = FakePorts(
            planning_snapshot(phase="PLANNING", active_sha="a" * 40),
            maintenance_cycle(phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                              cycle_base_sha="a" * 40, revision=4,
                              file_assessment_start_merged_count=0),
        )
        unit = maintenance_file_review.ReviewUnit(source, "saved-session", None)
        root_placement = RootPlacement(
            "engine.py", "internal_implementation", "root source candidate",
        )
        ports.file_review = SimpleNamespace(
            progress=mock.Mock(return_value=maintenance_file_review.FileReviewProgress(
                (source,), (), unit, (root_placement,),
            )),
            content=mock.Mock(return_value=content),
            start=mock.Mock(), session=mock.Mock(), complete=mock.Mock(),
            fail=mock.Mock(),
        )
        ports.agent.result = PlanningAgentResult(
            0,
            maintenance_file_review.RESULT_PREFIX + json.dumps(
                result_for(source, "def calculate(): return 1", content)
            ),
            "saved-session", PlanningAgentResultKind.SUCCESS,
        )
        ports.git.head = "a" * 40
        ports.agent.run_planner = mock.Mock(wraps=ports.agent.run_planner)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/worktree")

        outcome = coordinator._assessment._run_planner_turn(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertEqual(ports.agent.run_count, 1)
        prompt = ports.agent.run_planner.call_args.args[1]
        for phrase in (
            "기존 구현·공유 helper·관련 테스트와 표준 도구",
            "현재 목표에 필요하지 않은 추상화·설정·파일 추가",
            "오류 처리·보안·접근성·회귀 검증",
            "결정·예외·실패 흐름과 Acceptance Criteria의 의미",
            "파일이나 문서를 수정하지 마라",
            "FILE_REVIEW_RESULT:",
            "revision-bound root placement inventory:",
            '"path": "engine.py"',
        ):
            self.assertIn(phrase, prompt)
        self.assertEqual(ports.file_review.complete.call_args.args[2], source)
        self.assertEqual(ports.file_review.fail.call_count, 0)

    def test_final_decision_waits_for_one_resolution_without_rerunning_planner(self):
        output = 'MAINTENANCE_RESULT: {"outcome":"no_change"}'
        ports = FakePorts(
            planning_snapshot(phase="PLANNING", active_sha="a" * 40,
                              planning_result=output, planning_result_completed=1,
                              planning_result_pending_validation=1),
            maintenance_cycle(phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                              cycle_base_sha="a" * 40, revision=4,
                              file_assessment_start_merged_count=0),
        )
        unit = maintenance_reconciliation.ResolutionUnit("d" * 64, "e" * 64, 7,
            {"key": "page-7", "assessment": "This page has one cohesive responsibility.", "has_issue": False},
            frozenset(), Path("/evidence/decision.json"), "saved-unit-session")
        ports.reconciliation = SimpleNamespace(validate=mock.Mock(return_value=unit),
            resolution_session=mock.Mock(), resolution_complete=mock.Mock(), resolution_fail=mock.Mock())
        ports.agent.result = PlanningAgentResult(0,
            maintenance_reconciliation.RESOLUTION_PREFIX + json.dumps({"key": "page-7",
                "excerpt": unit.assessment["assessment"], "issue_keys": [], "disposition": "no_issue",
                "reason": "The existing boundary needs no structural change."}),
            "saved-unit-session", PlanningAgentResultKind.SUCCESS)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/worktree")
        run.maintenance_context = object()
        self.assertEqual(coordinator._assessment._validate_planner_result(run).kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-unit-session")
        ports.reconciliation.resolution_complete.assert_called_once()
        self.assertEqual(ports.state.planning(self.context.repo).planning_result, output)
        self.assertTrue(ports.state.planning(self.context.repo).planning_result_pending_validation)

    def test_pending_reverse_scan_yields_before_relation_or_synthesis_agent(self):
        ports = FakePorts(
            planning_snapshot(phase="PLANNING", active_sha="a" * 40),
            maintenance_cycle(phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                              cycle_base_sha="a" * 40, revision=4),
        )
        ports.reconciliation = mock.Mock()
        ports.reconciliation.progress.return_value = maintenance_reconciliation.ReconciliationProgress(
            (), (), (), None, pending_scan=True)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        outcome = coordinator._assessment._reconcile_one_relation(run, Path("/worktree"))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.run_count, 0)
        ports.reconciliation.synthesis.assert_not_called()

    def test_final_assessment_uses_independent_pages_and_durable_evidence(self):
        import sqlite3
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES (?)", (self.context.repo,))
        repository = maintenance_reconciliation.SynthesisRepository(connection)
        findings = maintenance_findings.FindingsRepository(connection)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        context = replace(self.context, state_dir=Path(directory.name))
        ports = FakePorts(
            planning_snapshot(phase="PLANNING", active_sha="a" * 40),
            maintenance_cycle(phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                              cycle_base_sha="a" * 40, revision=4,
                              file_assessment_start_merged_count=0),
        )
        ports.git.head = "a" * 40
        reviews = tuple({"path": f"src/file_{index}.py", "evidence": {
            "chunk_reviews": ["large review evidence " * 2000],
            "responsibility_and_integration": f"unique responsibility issue {index}"}}
            for index in range(13))
        progress = maintenance_reconciliation.ReconciliationProgress((), (), (), None, file_reviews=reviews)
        ports.file_review = mock.Mock()
        def findings_unit(repo, path, base, cycle, revision, plans, directory):
            page = repository.prepare(repo, cycle, revision, progress)
            unit = findings.prepare(repo, cycle, revision, page.digest, plans, {})
            return unit, findings.export(repo, unit, plans, {}, repository.export(repo, page, progress, directory))

        def findings_complete(repo, path, base, cycle, revision, plans, unit, output):
            findings.complete(repo, unit, output, plans, lambda value: None)

        ports.reconciliation = SimpleNamespace(
            findings_unit=findings_unit, findings_session=findings.session,
            findings_complete=findings_complete, findings_fail=findings.fail,
            progress=mock.Mock(return_value=progress), synthesis=repository.prepare,
            synthesis_session=repository.session, synthesis_complete=repository.complete,
            synthesis_export=repository.export)
        ports.validation.maintenance_prompt = mock.Mock(return_value="assessment prompt")
        prompts, sessions = [], []

        def consume(_worktree, prompt, session_id, callback):
            prompts.append(prompt)
            sessions.append(session_id)
            callback("session-one")
            page = repository.prepare(self.context.repo, 4, "a" * 40, progress)
            if page.index < page.total:
                message = maintenance_reconciliation.SYNTHESIS_PREFIX + json.dumps({
                    "key": page.key, "excerpt": page.text.partition("\n")[2][:20], "has_issue": True,
                    "assessment": "Retain every local issue and compare across the reviewed pages.",
                })
            elif "FINDINGS_UNIT:" in prompt:
                unit = findings.prepare(context.repo, 4, "a" * 40, page.digest, {}, {})
                message = maintenance_findings.UNIT_PREFIX + json.dumps({
                    "manifest_id": unit.manifest_id, "index": unit.index, "parts": [],
                    "complete": True, "counts": {"finding_count": 0, "comparison_count": 0},
                    "summary": "All stored assessments remain available for final resolution."})
            else:
                # This final call has never seen prior sessions. All validated
                # observations must still be recoverable via the supplied bundle.
                manifest = next(path for path in Path(directory.name).rglob("manifest.json")
                                if (path.parent / "page-00000000.json").exists())
                self.assertIn(str(next(Path(directory.name).rglob("findings/*/manifest.json"))), prompt)
                saved = json.loads((manifest.parent / "page-00000000.json").read_text())
                self.assertIn("Retain every local issue", saved["result"]["assessment"])
                message = "planner result"
            return PlanningAgentResult(0, message, "session-one", PlanningAgentResultKind.SUCCESS)

        ports.agent.run_planner = mock.Mock(side_effect=consume)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(context)
        run.planning_worktree = Path("/worktree")
        run.maintenance_context = object()
        with mock.patch.object(coordinator._assessment, "_review_one_file", return_value=None):
            for _ in maintenance_reconciliation.synthesis_pages(progress):
                result = coordinator._assessment._run_planner_turn(run)
                self.assertEqual(result.kind, PlanningOutcomeKind.WAITING)
            self.assertEqual(coordinator._assessment._run_planner_turn(run).kind, PlanningOutcomeKind.WAITING)
            self.assertIsNone(coordinator._assessment._run_planner_turn(run))
        self.assertIsNone(sessions[0])
        self.assertTrue(all(session is None for session in sessions))
        self.assertTrue(all(len(prompt.encode()) < 50_000 for prompt in prompts))
        self.assertIn("unique responsibility issue 12", "".join(prompts[:-1]))
        self.assertNotIn("chunk_reviews", prompts[-1])
        self.assertNotIn("SYNTHESIS_RESOLUTIONS:", prompts[-1])
        self.assertIn("manifest_id", prompts[-1])
        self.assertIn("finding_count", prompts[-1])

    def test_active_legacy_assessment_skips_new_file_review_stage(self):
        ports = FakePorts(
            planning_snapshot(phase="PLANNING", active_sha="base"),
            maintenance_cycle(phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                              cycle_base_sha="base", revision=4),
        )
        ports.file_review = mock.Mock()
        ports.validation.maintenance_prompt = mock.Mock(return_value="legacy prompt")
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/worktree")
        run.maintenance_context = object()

        self.assertIsNone(coordinator._assessment._run_planner_turn(run))

        ports.file_review.progress.assert_not_called()
        ports.file_review.completed_summary.assert_not_called()
        self.assertEqual(ports.agent.run_count, 1)

    def test_new_periodic_review_starts_file_assessment_before_ready_worker(self):
        ports = FakePorts(planning_snapshot())
        ports.state.maintenance_due_required = True
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        # The real scheduler exposes only resumable owned READY during assessment.
        # This fixture is an unowned READY, runnable only while the cycle is idle.
        def schedule(_repo):
            decision = SimpleNamespace(**vars(ports.state.schedule_result))
            if ports.state.cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
                decision.runnable_ready = None
            return decision

        ports.state.schedule = mock.Mock(side_effect=schedule)
        ports.state.activate_ready = mock.Mock()
        for index, reason in enumerate(("no_successful_assessment", "interval_reached"), 1):
            ports.state.maintenance_due_reason = reason
            run = PlanningLifecycleCoordinator(ports)._new_run(
                replace(self.context, registered_only=True)
            )
            outcome = PlanningLifecycleCoordinator(ports)._selection._select_planning_work(run)
            self.assertIsNone(outcome)
            self.assertEqual(ports.state.maintenance_begin_count, index)
            self.assertTrue(run.maintenance_planning)
            ports.state.cycle = maintenance_cycle()

        ports.state.maintenance_due_reason = "one_time_full_assessment"
        ports.state.schedule_result.has_ready = False
        ports.state.schedule_result.runnable_ready = None
        run = PlanningLifecycleCoordinator(ports)._new_run(
            replace(self.context, registered_only=True, self_repository=True)
        )
        PlanningLifecycleCoordinator(ports)._selection._select_planning_work(run)
        self.assertEqual(ports.state.maintenance_begin_count, 3)
        ports.state.activate_ready.assert_not_called()

    def test_missing_full_assessment_evidence_uses_bounded_planner_retry(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="a" * 40, revision=1)
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="a" * 40,
            planning_result='MAINTENANCE_RESULT: {"outcome":"no_change"}',
            planning_result_pending_validation=True,
            planning_validation_error="earlier invalid evidence",
            planning_retry_count=1,
        ), cycle)
        ports.validation.validate_maintenance_result = mock.Mock(return_value=None)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(replace(self.context, self_repository=True))
        run.planning_worktree = Path("/planning")
        run.maintenance_context = object()
        with mock.patch("loop_manager_internal.planning.assessment_port.maintenance_file_inventory.source_inventory",
                        return_value={"docs/PRODUCT.md": "hash"}):
            outcome = coordinator._assessment._validate_planner_result(run)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 2)
        self.assertIn("전수 평가 결과", ports.state.snapshot.planning_validation_error)

    def test_malformed_full_assessment_finding_uses_bounded_planner_retry(self):
        revision = "a" * 40
        report = {
            "source_revision": revision,
            "coverage": {key: "Reviewed responsibility boundaries."
                         for key in ("code", "tests", "documents")},
            "findings": [{"id": "agent-scope", "kind": ["code"],
                          "path": "module.py", "source_hash": "b" * 64,
                          "evidence": "G-051 already separated Agent scope responsibilities.",
                          "status": "resolved", "plan_ids": []}],
        }
        result = ('MAINTENANCE_RESULT: {"outcome":"no_change"}\n'
                  'FULL_ASSESSMENT_RESULT: ' + json.dumps(report))
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha=revision, revision=1)
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha=revision, planning_result=result,
            planning_result_pending_validation=True,
            planning_validation_error="earlier invalid evidence",
            planning_retry_count=1,
        ), cycle)
        ports.validation.validate_maintenance_result = mock.Mock(return_value=None)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(replace(self.context, self_repository=True))
        run.planning_worktree = Path("/planning")
        run.maintenance_context = object()
        with mock.patch("loop_manager_internal.planning.assessment_port.maintenance_file_inventory.source_inventory",
                        return_value={"module.py": "b" * 64}):
            outcome = coordinator._assessment._validate_planner_result(run)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 2)
        self.assertIn("발견 항목", ports.state.snapshot.planning_validation_error)

    def test_planner_agent_port_passes_planless_invocation_and_ignores_telemetry_error(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        port = loop_manager._PlanningAgentPorts(connection, self.context, None)
        reported = loop_manager.AgentResult(
            0, "planned", "session", collection_status="error"
        )

        with mock.patch.object(loop_manager, "run_agent", return_value=reported) as run_agent:
            result = port.run_planner(Path("/planning"), "prompt", None, lambda _: None)

        self.assertEqual(result.kind, PlanningAgentResultKind.SUCCESS)
        request = run_agent.call_args.kwargs["invocation"]
        self.assertEqual((request.repo, request.plan_id, request.transition,
                          request.attempt), ("/repo", None, "start", 1))
        self.assertIs(run_agent.call_args.kwargs["connection"], connection)
