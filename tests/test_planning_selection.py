from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.planning.requirement_retirement import FinalInput
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import maintenance_state, planning_input_state
from loop_manager_internal.planning.input_fingerprints import (
    IsRequirementArchiveOnlyChangeEffects, _is_requirement_archive_only_change,
)
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance.maintenance_file_inventory import RootPlacement, TrackedSource
from tests.support.maintenance import result_for
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from loop_manager_internal.planning.planning_workspace import (
    planning_workspace_path, validate_planning_workspace_identity,
    ValidatePlanningWorkspaceIdentityEffects,
)
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
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
from loop_manager_internal.planning.planning_contracts import snapshot_run


from tests.support.planning_case import (
    PlanningCase, FakePorts, FakeStatePort, FakeGitPort, FakeAgentPort,
    FakeValidationPort, FakeEventPort, planning_snapshot, maintenance_cycle,
)

class PlanningSelectionTest(PlanningCase):
    def test_outstanding_requirement_work_adds_no_retirement_model_call(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (), audit_restorations=lambda *_args: (),
            audit_pending=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(ports.agent.run_count, 0)
        self.assertIn(outcome.kind, {
            PlanningOutcomeKind.FINGERPRINT_SKIPPED, PlanningOutcomeKind.NO_CHANGE,
        })

    def test_final_requirement_check_uses_one_general_turn_not_quick_check(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        item = FinalInput(
            path="docs/requirements/R.md", token="pinned", source_oid="source-blob", criteria=("Works",),
            related_ids=("G-001",),
        )
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (item,),
            audit_restorations=lambda *_args: (),
            audit_pending=lambda *_args: (),
            validate_result=lambda *_args: (),
            record=lambda *_args: None,
            record_audit_result=lambda *_args: None,
            stage_restorations=lambda *_args: (),
            stage_moves=lambda *_args, **_kwargs: (),
            receipted_paths=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(ports.agent.run_count, 1)
        self.assertFalse(ports.agent.roles[0][1])
        self.assertIn("REQUIREMENT_JUDGMENTS", ports.agent.prompts[0])
        self.assertNotEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)

    def test_historical_audit_uses_general_planner_once(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        item = SimpleNamespace(
            path="docs/requirements/R.md", token="audit-pinned",
            criteria=("Works",), related_ids=("G-001",),
        )
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (), audit_restorations=lambda *_args: (),
            audit_pending=lambda *_args: (item,),
            validate_result=lambda *_args: (), record=lambda *_args: None,
            record_audit_result=lambda *_args: None,
            stage_restorations=lambda *_args: (), stage_moves=lambda *_args, **_kwargs: (),
            receipted_paths=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(ports.agent.run_count, 1)
        self.assertFalse(ports.agent.roles[0][1])
        self.assertIn("REQUIREMENT_AUDIT", ports.agent.prompts[0])
        self.assertNotEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)

    def test_unfulfilled_final_judgment_requires_source_linked_followup_plan(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        source = "docs/requirements/R.md"
        item = FinalInput(
            path=source, token="pinned", source_oid="source-blob", criteria=("Works",),
            related_ids=("G-001",),
        )
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (item,), audit_restorations=lambda *_args: (),
            audit_pending=lambda *_args: (),
            validate_result=lambda *_args: (SimpleNamespace(
                source_path=source, outcome="unfulfilled",
            ),),
            record=lambda *_args: None, record_audit_result=lambda *_args: None,
            stage_restorations=lambda *_args: (), stage_moves=lambda *_args, **_kwargs: (),
            receipted_paths=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.agent.run_count, 1)

    def test_planning_workspace_identity_uses_selected_path(self):
        state_dir = Path("/state")
        selected = planning_workspace_path(state_dir, "/repo", "fingerprint")
        validate = mock.Mock()
        validate_planning_workspace_identity(
            ValidatePlanningWorkspaceIdentityEffects(validate),
            state_dir, "/repo", "fingerprint", selected,
        )
        validate.assert_called_once_with(
            state_dir, selected, selected, "planning worktree"
        )

    def test_dual_archive_skips_only_with_verified_unchanged_declarations(self):
        requirement = b"docs/requirements/R.md"
        plan = b"docs/plans/[G-001] Done.md"
        ready = b"docs/plans/[G-002] Ready.md"
        previous = {
            requirement: (b"blob", b"requirement"),
            plan: (b"blob", b"done"),
            ready: (b"blob", b"ready"),
        }
        for change in ("verified", "unverified", "plan-edit", "code", "archive-edit"):
            for has_ready in (False, True):
                with self.subTest(change=change, has_ready=has_ready):
                    current = {
                        b"docs/requirements/archive/R.md": previous[requirement],
                        b"docs/plans/archive/[G-001] Done.md": previous[plan],
                        ready: previous[ready],
                    }
                    if change == "plan-edit":
                        current[ready] = (b"blob", b"edited")
                    elif change == "code":
                        current[b"source.py"] = (b"blob", b"new-code")
                    elif change == "archive-edit":
                        current[b"docs/plans/archive/[G-001] Done.md"] = (b"blob", b"edited")
                    effects = IsRequirementArchiveOnlyChangeEffects(
                        _git_tree_entries=lambda _repo, sha: previous if sha == "old-base" else current,
                        _is_planning_input=loop_manager._is_planning_input,
                    )
                    ports = FakePorts(planning_snapshot(
                        processed_sha="old-base", planning_fingerprint="old-full",
                        specification_fingerprint="old-specification",
                    ))
                    ports.git.fingerprints = {"old-base": "old-full", "base": "archive-full"}
                    ports.git.specification_fingerprints["old-base"] = "old-specification"
                    ports.git.requirement_archive_only = lambda repo, old, new, **kwargs: (
                        _is_requirement_archive_only_change(effects, repo, old, new, **kwargs)
                    )
                    declarations = lambda tree: {
                        path.decode(): tuple(part.decode() for part in value)
                        for path, value in tree.items()
                        if loop_manager.execplan_catalog.is_active_execplan_path(path)
                    }
                    connection = sqlite3.connect(":memory:")
                    self.addCleanup(connection.close)
                    connection.row_factory = sqlite3.Row
                    planning_input_state.record_general(connection, "/repo", "old-base", declarations(previous))
                    if change != "unverified":
                        planning_input_state.advance_verified_delta(
                            connection, "/repo", "base", {plan.decode(): (("blob", "done"), None)},
                        )
                    ports.git.plan_declarations = lambda *_args: declarations(current)
                    ports.state.plan_declarations_match = lambda repo, entries: (
                        planning_input_state.matches(connection, repo, entries)
                    )
                    ports.state.schedule_result = SimpleNamespace(
                        active=None, has_ready=has_ready, has_waiting=False,
                        runnable_ready=object() if has_ready else None, project_blocked=False,
                    )
                    ports.state.activate_result = object() if has_ready else None
                    coordinator = PlanningLifecycleCoordinator(ports)
                    if change == "verified":
                        for _ in range(2):
                            outcome = coordinator.run(self.context)
                            self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY
                                             if has_ready else PlanningOutcomeKind.FINGERPRINT_SKIPPED)
                            ports.state.snapshot = replace(ports.state.snapshot, phase=None)
                        self.assertEqual(ports.agent.run_count, 0)
                        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")
                    else:
                        run = coordinator._new_run(self.context)
                        self.assertIsNone(coordinator._selection._select_planning_work(run))
                        self.assertIn(run.selection.kind, {
                            PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
                            PlanningDecisionKind.RUN_FALLBACK_PLANNING,
                        })

    def test_changed_plan_declaration_preempts_runnable_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        ports.state.plan_declarations_match_result = False
        ports.git.requirement_archive_only = mock.Mock(return_value=True)
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_FALLBACK_PLANNING)
        self.assertFalse(run.ready_activated_before_planning)
        ports.git.requirement_archive_only.assert_not_called()

    def test_legacy_processed_sha_cannot_prove_plan_review_before_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="legacy-v2",
            specification_fingerprint="specification",
        ))
        ports.state.plan_declarations_match_result = False
        ports.git.nonplan_fingerprints["base"] = "legacy-v2"
        ports.git.fingerprints["base"] = "current-v3"
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_FALLBACK_PLANNING)
        self.assertFalse(run.ready_activated_before_planning)

    def test_selection_step_returns_a_new_immutable_snapshot(self):
        ports = FakePorts(planning_snapshot())
        coordinator = PlanningLifecycleCoordinator(ports)
        initial = snapshot_run(coordinator._new_run(self.context))

        result = coordinator._selection.step("_select_planning_work", initial)

        self.assertIsNone(result.outcome)
        self.assertIsNone(initial.general_fingerprint)
        self.assertEqual(result.snapshot.general_fingerprint, "fingerprint")
        self.assertEqual(
            result.snapshot.selection.kind,
            PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
        )
        with self.assertRaises(FrozenInstanceError):
            result.snapshot.general_fingerprint = "changed"

    def test_new_document_approval_forces_planning_with_unchanged_fingerprints(self):
        ports = FakePorts(planning_snapshot(
            planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        ports.state.document_approval_pending = True
        ports.state.maintenance_due_required = True
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(replace(
            self.context, registered_only=True, self_repository=True,
        ))

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(
            run.selection.kind,
            PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
        )
        self.assertFalse(run.ready_activated_before_planning)
        self.assertEqual(ports.state.maintenance_begin_count, 0)

    def test_real_planner_port_records_resumption_at_agent_start(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        notifications.configure_session(connection, enabled=True)
        repo = "/projects/alpha"
        connection.execute(
            "INSERT INTO projects (repo, base_branch, planning_error) "
            "VALUES (?, 'main', 'bad plan')", (repo,),
        )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        loop_manager.update_project(connection, repo, planning_error=None)
        context = SimpleNamespace(
            repo=repo, provider="codex", state_dir=Path("/state"),
            config_path=Path("/config.ini"),
        )
        port = loop_manager._PlanningAgentPorts(connection, context, None)
        result = SimpleNamespace(
            code=0, message="", session_id=None,
            result_kind=loop_manager.AgentResultKind.SUCCESS,
        )
        with mock.patch.object(loop_manager, "run_agent_with_retries", return_value=result):
            port.run_planner(Path("/worktree"), "prompt", None, lambda _: None)
        resumed = connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchall()
        self.assertEqual(len(resumed), 1)
        self.assertEqual(json.loads(resumed[0][0])["operation"], "Planner")


    def test_registered_self_flag_only_reaches_maintenance_due_for_self_context(self):
        for self_repository in (False, True):
            ports = FakePorts(planning_snapshot())
            run = PlanningLifecycleCoordinator(ports)._new_run(
                replace(self.context, registered_only=True,
                        self_repository=self_repository)
            )
            PlanningLifecycleCoordinator(ports)._selection._select_planning_work(run)
            self.assertEqual(ports.state.maintenance_due_self_repository,
                             self_repository)

    def test_failed_general_input_without_worktree_survives_repeated_runs(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="fingerprint",
            planning_retry_count=3,
            planner_session_id="saved-session",
        ))
        before = ports.state.snapshot
        for attempt, base_sha in enumerate(
            ("base", "advanced-base", "advanced-base", "advanced-base")
        ):
            ports.state.schedule_result = SimpleNamespace(
                active=None,
                has_ready=attempt % 2 == 0,
                has_waiting=attempt % 2 == 1,
                runnable_ready=object() if attempt % 2 == 0 else None,
                project_blocked=False,
            )
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha=base_sha)
            )
            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot, before)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.updates, [])

    def test_failed_maintenance_input_ignores_base_only_advance(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base",
            revision=4,
        )
        fingerprint = maintenance_planning_fingerprint(cycle, "fingerprint")
        before = planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint=fingerprint,
            planning_error="saved maintenance failure",
            planning_error_fingerprint=fingerprint,
            planning_workspace_owned=1,
            planner_session_id="saved-session",
        )
        ports = FakePorts(before, cycle)
        ports.git.remote = "advanced-base"
        for attempt in range(4):
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha="advanced-base")
            )
            self.assertEqual(outcome.kind, (PlanningOutcomeKind.OPERATIONAL_ERROR
                if attempt == 0 else PlanningOutcomeKind.REMOTE_BASE_RESTART
                if attempt == 1 else PlanningOutcomeKind.WAITING))
            saved = (planning_snapshot(**{key: value for key, value in json.loads(ports.state.snapshot.planning_preserved_state).items()
                                          if key in PlanningSnapshot.__dataclass_fields__})
                     if ports.state.snapshot.planning_preserved_state else ports.state.snapshot)
            self.assertEqual(saved.active_sha, "advanced-base")
            self.assertEqual(saved.worktree, before.worktree)
            self.assertEqual(ports.state.snapshot.planning_error, before.planning_error)
            self.assertEqual(saved.planner_session_id, "saved-session")
            self.assertEqual(saved.planning_attempt_fingerprint, fingerprint)
            self.assertEqual(ports.state.cycle.cycle_base_sha, "advanced-base")
            self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.git.ownership_checks,
                         [("/repo", fingerprint, Path("/planning"))] * 2)

    def test_maintenance_mode_error_survives_base_and_source_advance(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base", revision=4,
        )
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint=maintenance_planning_fingerprint(cycle, "old-input"),
            planning_workspace_owned=1, planning_error="mode requires user action",
            planning_error_fingerprint="maintenance-cycle:assessment:4",
            planner_session_id="saved-session",
        ), cycle)
        ports.git.remote = "advanced-base"
        ports.git.fingerprints = {"base": "old-input", "advanced-base": "new-input"}

        for attempt in range(3):
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha="advanced-base"),
            )
            self.assertEqual(outcome.kind, (PlanningOutcomeKind.OPERATIONAL_ERROR
                if attempt == 0 else PlanningOutcomeKind.REMOTE_BASE_RESTART
                if attempt == 1 else PlanningOutcomeKind.WAITING))
            saved = (planning_snapshot(**{key: value for key, value in json.loads(ports.state.snapshot.planning_preserved_state).items()
                                          if key in PlanningSnapshot.__dataclass_fields__})
                     if ports.state.snapshot.planning_preserved_state else ports.state.snapshot)
            self.assertEqual(saved.active_sha, "advanced-base")
            self.assertEqual(ports.state.snapshot.planning_error, "mode requires user action")
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.clear_calls, [])

    def test_failed_maintenance_input_restarts_after_related_source_change(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base",
            revision=4,
        )
        fingerprint = maintenance_planning_fingerprint(cycle, "old-input")
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint=fingerprint,
            planning_workspace_owned=1,
            planning_error="saved maintenance failure",
            planning_error_fingerprint=fingerprint,
            planner_session_id="saved-session",
        ), cycle)
        ports.git.fingerprints = {"base": "old-input", "advanced-base": "new-input"}
        ports.git.remote = "advanced-base"
        source = "docs/requirements/[R-052] Relevant.md"
        ports.state.execplan_rows = lambda _repo: [{"plan_id": "G-066", "status": "READY"}]
        ports.git.relevant_document_changes = mock.Mock(return_value=(source,))
        ports.validation.prepare_maintenance_context = mock.Mock(return_value=object())
        ports.validation.maintenance_change_context = mock.Mock(return_value="source changed")
        ports.validation.maintenance_prompt = mock.Mock(return_value="correct the saved failure")
        ports.agent.result = PlanningAgentResult(
            1, "same failure", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="advanced-base")
        )

        self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(ports.git.relevant_document_changes.call_args.args[-1], ("G-066",))
        self.assertFalse(ports.git.relevant_document_changes.call_args.kwargs[
            "include_active_requirements"
        ])
        self.assertEqual(ports.git.ownership_checks,
                         [("/repo", fingerprint, Path("/planning"))])
        self.assertEqual(ports.state.snapshot.planning_error, "saved maintenance failure")
        self.assertEqual(ports.state.snapshot.active_sha, "advanced-base")
        self.assertEqual(ports.state.snapshot.worktree, "/planning")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.state.cycle.cycle_base_sha, "advanced-base")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        repeated = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="advanced-base"),
        )
        self.assertEqual(repeated.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.agent.run_count, 1)

    def test_specification_change_plans_once_before_runnable_ready(self):
        ports = FakePorts(
            planning_snapshot(
                processed_sha="old-base",
                specification_fingerprint="old-specification",
                planning_fingerprint="old-full",
            )
        )
        ports.git.fingerprint = "new-full"
        ports.git.specification_fingerprints["old-base"] = "old-specification"
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "specification",
        )
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")

        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        second = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)

    def _code_check_ports(self):
        ports = FakePorts(
            planning_snapshot(
                processed_sha="old-base",
                specification_fingerprint="specification",
                planning_fingerprint="old-full",
            )
        )
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.state.has_general_baseline = True
        plan = ReadyPlanSnapshot(
            plan_id="G-001", path="docs/plans/[G-001] Ready.md", title="Ready",
            sources=("docs/PRODUCT.md",), depends_on=(), complexity="low",
            risk="low", priority="normal", content_hash="plan-hash",
            plan_sha="base", text="plan", current_complexity="low",
            current_risk="low", current_metadata_recorded=True,
        )
        ports.validation.snapshot = MutableReadySnapshot(
            repo="/repo", active_sha="base", plans=(plan,),
            token=mutable_snapshot_token("/repo", "base", (plan,)),
        )
        ports.agent.result = PlanningAgentResult(
            0, 'PLANNER_CHECK_RESULT: {"outcome":"no_change","evidence":"code paths reviewed","reason":"plans remain valid","plan_ids":[]}',
            "check-session", PlanningAgentResultKind.SUCCESS,
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()
        return ports

    def test_source_change_with_ready_runs_planner_once_before_activation(self):
        ports = self._code_check_ports()
        ports.git.diff_name_status = "M\tsrc/app.py\nM\tdocs/plans/[G-002] Auto.md\n"

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.roles, [("planner_check", True)])
        self.assertIn("src/app.py", ports.agent.prompts[0])
        self.assertNotIn("docs/plans/[G-002] Auto.md", ports.agent.prompts[0])
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")
        self.assertEqual(ports.state.check_record["stage"], "accepted")

        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.agent.run_count, 1)

    def test_accepted_check_survives_content_identical_revision_advance(self):
        ports = self._code_check_ports()
        PlanningLifecycleCoordinator(ports).run(self.context)
        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        ports.git.remote = "same-content-base"
        ports.git.head = "same-content-base"
        ports.git.fingerprints["same-content-base"] = "new-full"

        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="same-content-base"),
        )

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.check_record["stage"], "accepted")

    def test_remote_code_change_during_check_reselects_new_input(self):
        ports = self._code_check_ports()
        original_run = ports.agent.run_planner

        def advance_remote(*args, **kwargs):
            result = original_run(*args, **kwargs)
            ports.git.remote = "new-base"
            ports.git.fingerprints["new-base"] = "newer-full"
            return result

        ports.agent.run_planner = advance_remote
        first = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.check_record["stage"], "result")
        ports.agent.run_planner = original_run

        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base"),
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.roles, [("planner_check", True)] * 2)
        self.assertEqual(ports.state.check_record["input_fingerprint"], "newer-full")

    def test_remote_document_change_during_check_hands_off_latest_input(self):
        ports = self._code_check_ports()
        original_run = ports.agent.run_planner

        def advance_remote(*args, **kwargs):
            result = original_run(*args, **kwargs)
            ports.git.remote = "document-base"
            ports.git.fingerprints["document-base"] = "document-full"
            ports.git.specification_fingerprints["document-base"] = "changed-specification"
            return result

        ports.agent.run_planner = advance_remote
        first = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        ports.agent.run_planner = original_run
        ports.agent.result = PlanningAgentResult(
            0, "general planner result", "general-session",
            PlanningAgentResultKind.SUCCESS,
        )

        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="document-base"),
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.roles, [("planner_check", True), (None, False)])
        self.assertEqual(ports.state.check_record["stage"], "handoff")

    def test_code_change_with_dependency_waiting_ready_checks_then_waits(self):
        ports = self._code_check_ports()
        ports.state.schedule_result.runnable_ready = None

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.roles, [("planner_check", True)])
        self.assertEqual(ports.state.check_record["stage"], "accepted")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_active_execution_is_not_preempted_by_check_snapshot(self):
        ports = self._code_check_ports()
        ports.state.schedule_result.active = object()
        ports.validation.prepare_general_context = mock.Mock(
            side_effect=AssertionError("active work must be resumed first"),
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(run.selection.kind, PlanningDecisionKind.RESUME_ACTIVE)
        ports.validation.prepare_general_context.assert_not_called()

    def test_code_change_without_mutable_ready_uses_general_planner(self):
        ports = self._code_check_ports()
        ports.validation.snapshot = MutableReadySnapshot(
            "/repo", "base", (), mutable_snapshot_token("/repo", "base", ()),
        )
        ports.agent.result = PlanningAgentResult(
            0, "general planner result", "general-session",
            PlanningAgentResultKind.SUCCESS,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.roles, [(None, False)])
        self.assertIsNone(ports.state.check_record)

    def test_guidance_and_code_change_uses_general_planner(self):
        ports = self._code_check_ports()
        ports.git.guidance_changed = True
        ports.agent.result = PlanningAgentResult(
            0, "general planner result", "general-session",
            PlanningAgentResultKind.SUCCESS,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.roles, [(None, False)])
        self.assertIsNone(ports.state.check_record)

    def test_accepted_check_does_not_claim_later_general_planning(self):
        ports = self._code_check_ports()
        first = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(first.kind, PlanningOutcomeKind.EXECUTION_READY)
        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        ports.validation.snapshot = MutableReadySnapshot(
            "/repo", "base", (), mutable_snapshot_token("/repo", "base", ()),
        )
        ports.agent.result = PlanningAgentResult(
            0, "general planner result", "general-session",
            PlanningAgentResultKind.SUCCESS,
        )

        second = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.roles, [("planner_check", True), (None, False)])
        self.assertEqual(ports.state.check_record["stage"], "superseded")

    def test_mutable_ready_owner_change_during_check_blocks_acceptance(self):
        ports = self._code_check_ports()
        original = ports.validation.snapshot
        empty = MutableReadySnapshot(
            "/repo", "base", (), mutable_snapshot_token("/repo", "base", ()),
        )
        ports.validation.prepare_general_context = mock.Mock(
            side_effect=[original, original, empty],
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.agent.roles, [("planner_check", True)])
        self.assertEqual(ports.state.check_record["stage"], "result")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

        ports.validation.prepare_general_context = mock.Mock(return_value=original)
        resumed = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(resumed.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.check_record["stage"], "accepted")

    def test_mutable_ready_owner_change_blocks_stale_handoff(self):
        ports = self._code_check_ports()
        ports.agent.result = PlanningAgentResult(
            0, 'PLANNER_CHECK_RESULT: {"outcome":"needs_revision","evidence":"changed API","reason":"update plan","plan_ids":["G-001"]}',
            "check-session", PlanningAgentResultKind.SUCCESS,
        )
        original = ports.validation.snapshot
        empty = MutableReadySnapshot(
            "/repo", "base", (), mutable_snapshot_token("/repo", "base", ()),
        )
        ports.validation.prepare_general_context = mock.Mock(
            side_effect=[original, original, empty],
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.agent.roles, [("planner_check", True)])
        self.assertEqual(ports.state.check_record["stage"], "result")

    def test_mutable_ready_owner_change_after_acceptance_blocks_activation(self):
        ports = self._code_check_ports()
        original = ports.validation.snapshot
        empty = MutableReadySnapshot(
            "/repo", "base", (), mutable_snapshot_token("/repo", "base", ()),
        )
        ports.validation.prepare_general_context = mock.Mock(
            side_effect=[original, original, original, empty],
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.check_record["stage"], "accepted")
        self.assertIsNone(ports.state.snapshot.phase)
        self.assertEqual(ports.agent.run_count, 1)

    def test_invalid_check_result_keeps_pending_session(self):
        ports = self._code_check_ports()
        ports.agent.result = PlanningAgentResult(
            0, "PLANNER_CHECK_RESULT: invalid", "check-session",
            PlanningAgentResultKind.SUCCESS,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.state.check_record["stage"], "pending")
        self.assertEqual(ports.state.check_record["session_id"], "check-session")
        self.assertEqual(ports.state.check_record["retry_count"], 1)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_check_result_with_secret_is_rejected_without_storing_value(self):
        ports = self._code_check_ports()
        ports.agent.result = PlanningAgentResult(
            0, 'PLANNER_CHECK_RESULT: {"outcome":"no_change","evidence":"Bearer exampletoken","reason":"plans remain valid","plan_ids":[]}',
            "check-session", PlanningAgentResultKind.SUCCESS,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.state.check_record["stage"], "pending")
        self.assertNotIn("exampletoken", str(ports.state.check_record))

    def test_check_scope_failure_cannot_advance_baseline(self):
        ports = self._code_check_ports()
        ports.agent.finish_scope = mock.Mock(return_value=PlanningScopeResult(
            error="read-only scope changed", result_snapshot="scope-evidence",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.check_record["stage"], "pending")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")
        self.assertEqual(ports.state.snapshot.planning_error, "read-only scope changed")
        self.assertEqual(ports.state.snapshot.agent_result_snapshot, "scope-evidence")

    def test_check_usage_limit_preserves_pending_session_and_baseline(self):
        ports = self._code_check_ports()
        ports.agent.result = PlanningAgentResult(
            1, "usage limit reached", "check-session",
            PlanningAgentResultKind.USAGE_LIMIT,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.USAGE_LIMIT_PAUSE)
        self.assertEqual(ports.state.check_record["stage"], "pending")
        self.assertEqual(ports.state.check_record["session_id"], "check-session")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_check_revision_hands_off_once_to_general_planner(self):
        for decision, plan_ids in (("needs_revision", ["G-001"]), ("uncertain", [])):
            with self.subTest(decision=decision):
                ports = self._code_check_ports()
                ports.agent.results = [
                    PlanningAgentResult(
                        0, "PLANNER_CHECK_RESULT: " + json.dumps({
                            "outcome": decision, "evidence": "changed API",
                            "reason": "G-001 validation needs review", "plan_ids": plan_ids,
                        }),
                        "check-session", PlanningAgentResultKind.SUCCESS,
                    ),
                    PlanningAgentResult(
                        0, "general planner result", "general-session",
                        PlanningAgentResultKind.SUCCESS,
                    ),
                ]

                outcome = PlanningLifecycleCoordinator(ports).run(self.context)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
                self.assertEqual(ports.agent.roles, [("planner_check", True), (None, False)])
                self.assertEqual(ports.state.check_record["stage"], "handoff")
                self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")

    def test_interrupted_handoff_resumes_general_without_second_check(self):
        ports = self._code_check_ports()
        ports.state.check_record = {
            "stage": "handoff", "error": "needs_revision: validation changed",
            "base_sha": "base", "session_id": "check-session",
        }
        ports.state.snapshot = replace(
            ports.state.snapshot, phase="PLANNING", active_sha="base",
            planning_attempt_fingerprint="new-full", planning_workspace_owned=1,
            worktree="/state/worktrees/planning",
        )
        ports.agent.result = PlanningAgentResult(
            0, "general planner result", "general-session",
            PlanningAgentResultKind.SUCCESS,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.roles, [(None, False)])
        self.assertEqual(ports.state.check_record["stage"], "handoff")

    def test_interrupted_general_result_after_handoff_is_not_recalled(self):
        ports = self._code_check_ports()
        ports.state.check_record = {
            "stage": "handoff", "error": "needs_revision: validation changed",
            "base_sha": "base", "session_id": "check-session",
        }
        ports.state.snapshot = replace(
            ports.state.snapshot, phase="PLANNING", active_sha="base",
            planning_attempt_fingerprint="new-full", planning_workspace_owned=1,
            worktree="/state/worktrees/planning", planning_result_completed=True,
            planning_result="general result", planner_session_id="general-session",
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")

    def test_interrupted_accepted_cleanup_resumes_without_second_check(self):
        ports = self._interrupted_accepted_ports()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def _interrupted_accepted_ports(self):
        ports = self._code_check_ports()
        from loop_manager_internal.storage.planner_check_state import target_fingerprint
        ports.state.check_record = {
            "stage": "accepted", "input_fingerprint": "new-full",
            "target_fingerprint": target_fingerprint(ports.validation.snapshot),
            "specification_fingerprint": "specification", "base_sha": "base",
            "comparison_sha": "old-base",
        }
        ports.state.snapshot = replace(
            ports.state.snapshot, phase="PLANNING", active_sha="base",
            planning_attempt_fingerprint="new-full", planning_workspace_owned=1,
            worktree="/state/worktrees/planning",
        )
        return ports

    def test_interrupted_accepted_reselects_changed_remote_input_or_target(self):
        for change in ("code", "document", "plan", "guidance", "owner"):
            with self.subTest(change=change):
                ports = self._interrupted_accepted_ports()
                ports.git.remote = "new-base"
                ports.git.fingerprints["new-base"] = "new-full"
                if change == "code":
                    ports.git.fingerprints["new-base"] = "newer-full"
                elif change == "document":
                    ports.git.specification_fingerprints["new-base"] = "changed-specification"
                elif change == "plan":
                    ports.state.plan_declarations_match_result = False
                elif change == "guidance":
                    ports.git.guidance_changed = True
                else:
                    ports.validation.snapshot = MutableReadySnapshot(
                        "/repo", "new-base", (),
                        mutable_snapshot_token("/repo", "new-base", ()),
                    )

                outcome = PlanningLifecycleCoordinator(ports).run(self.context)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
                self.assertIsNone(ports.state.snapshot.phase)
                self.assertEqual(ports.agent.run_count, 0)
                self.assertEqual(ports.state.check_record["stage"], "accepted")
                if change == "code":
                    resumed = PlanningLifecycleCoordinator(ports).run(
                        replace(self.context, base_sha="new-base"),
                    )
                    self.assertEqual(resumed.kind, PlanningOutcomeKind.EXECUTION_READY)
                    self.assertEqual(ports.agent.roles, [("planner_check", True)])
                    self.assertEqual(
                        ports.state.check_record["input_fingerprint"], "newer-full",
                    )

    def test_interrupted_accepted_reuses_identical_remote_revision(self):
        ports = self._interrupted_accepted_ports()
        ports.git.remote = "same-content-base"
        ports.git.fingerprints["same-content-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="same-content-base"),
        )

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_accepted_input_mismatch_at_activation_restarts_before_worker(self):
        ports = self._interrupted_accepted_ports()
        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.current_fingerprint = "newer-full"
        run.specification_fingerprint = "specification"
        run.fingerprint_skipped = True

        outcome = coordinator._selection._activate_next_execplan(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertIsNone(ports.state.snapshot.phase)

    def test_no_change_planner_result_advances_full_baseline_before_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.validation.result = None
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")
        self.assertEqual(ports.state.snapshot.processed_sha, "base")

    def test_source_change_at_direct_activation_restarts_before_worker(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base",
            specification_fingerprint="specification",
            planning_fingerprint="fingerprint",
        ))
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.git.remote = "new-base"
        ports.git.fingerprints["new-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertIsNone(ports.state.snapshot.phase)

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )
        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)

    def test_new_specification_at_direct_activation_restarts_before_worker(self):
        ports = FakePorts(
            planning_snapshot(
                specification_fingerprint="specification",
                planning_fingerprint="fingerprint",
            )
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.validation.result = None
        ports.git.remote = "new-base"
        ports.git.specification_fingerprints["new-base"] = "new-specification"
        ports.git.fingerprints["new-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertIsNone(ports.state.snapshot.phase)
        self.assertEqual(ports.agent.run_count, 0)

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "new-specification",
        )

    def test_new_specification_after_planner_success_restarts_before_worker(self):
        ports = FakePorts(
            planning_snapshot(
                specification_fingerprint="old-specification",
                planning_fingerprint="old-full",
            )
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.validation.result = None
        ports.git.specification_fingerprints["new-base"] = "new-specification"
        ports.git.fingerprints["new-base"] = "new-full"

        ports.git.relevant_document_changes = mock.Mock(return_value=("docs/PRODUCT.md",))
        original_planner = ports.agent.run_planner

        def advance_after_planner(*args):
            result = original_planner(*args)
            ports.git.remote = "new-base"
            return result

        ports.agent.run_planner = advance_after_planner
        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
        self.assertEqual(ports.state.snapshot.phase, "PLANNING")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint, "old-specification"
        )
        self.assertEqual(ports.state.snapshot.planner_session_id, "session-1")
        self.assertTrue(ports.state.snapshot.planning_result_completed)
        self.assertEqual(ports.state.clear_calls, [])

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 2)
        self.assertEqual(ports.agent.resumed_session, "session-1")
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "new-specification",
        )

    def test_remote_input_identity_controls_completed_result_reuse_after_pause(self):
        for input_changed in (False, True):
            with self.subTest(input_changed=input_changed):
                ports = FakePorts(planning_snapshot(planning_fingerprint="old-input"))
                ports.validation.result = None
                ports.git.fingerprints["new-base"] = (
                    "new-input" if input_changed else ports.git.fingerprint
                )
                original_planner = ports.agent.run_planner

                def advance_after_planner(*args):
                    result = original_planner(*args)
                    ports.git.remote = "new-base"
                    return result

                ports.agent.run_planner = advance_after_planner
                coordinator = PlanningLifecycleCoordinator(ports)
                first = coordinator.run(self.context)
                preserved = ports.state.snapshot

                self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
                self.assertEqual(preserved.phase, "PLANNING")
                self.assertTrue(preserved.planning_result_completed)
                self.assertEqual(ports.state.clear_calls, [])
                self.assertEqual(preserved.planning_fingerprint, "old-input")
                self.assertEqual(ports.agent.run_count, 1)
                second = coordinator.run(replace(self.context, base_sha="new-base"))

                self.assertEqual(second.kind, PlanningOutcomeKind.NO_CHANGE)
                self.assertEqual(ports.agent.run_count, 2 if input_changed else 1)
                if input_changed:
                    self.assertEqual(ports.agent.resumed_session, "session-1")
                self.assertEqual(ports.state.snapshot.processed_sha, "new-base")
                self.assertEqual(ports.state.snapshot.planning_fingerprint,
                                 ports.git.fingerprints["new-base"])

    def test_failed_input_correction_resumes_after_base_adoption_before_agent(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="old-input", planning_workspace_owned=1,
            planning_error="saved failure", planning_error_fingerprint="old-input",
            planner_session_id="saved-session", planning_retry_count=3,
            planning_validation_error="saved failure",
        ))
        ports.git.fingerprints = {"base": "old-input", "new-base": "new-input"}
        ports.git.remote = "new-base"
        ports.agent.result = PlanningAgentResult(
            1, "same failure", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE,
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        prepared = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))

        self.assertIsNone(prepared)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.active_sha, "new-base")
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint, "new-input")
        self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 0)
        resumed = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base"),
        )

        self.assertEqual(resumed.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.clear_calls, [])

    def test_related_document_correction_survives_interruption_before_planner(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="old-input", planning_workspace_owned=1,
            planning_error="saved failure", planning_error_fingerprint="old-input",
            planner_session_id="saved-session", planning_retry_count=3,
            planning_validation_error="saved failure", planning_result_completed=True,
            planning_result="saved result",
        ))
        ports.git.fingerprints = {"base": "old-input", "new-base": "new-input"}
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(return_value=("docs/PRODUCT.md",))
        ports.agent.result = PlanningAgentResult(
            1, "same failure", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE,
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        self.assertIsNone(coordinator._workspace._prepare_workspace(coordinator._new_run(self.context)))
        preserved = ports.state.snapshot

        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(preserved.active_sha, "new-base")
        self.assertIsNone(preserved.planning_result)
        self.assertEqual(ports.state.diagnostics[-1].planning_result, "saved result")
        self.assertEqual(ports.state.diagnostics[-1].planning_error, "saved failure")
        self.assertEqual(ports.state.diagnostics[-1].planning_validation_error, "saved failure")
        self.assertIn("docs/PRODUCT.md", preserved.planning_validation_error)
        self.assertTrue(preserved.planning_result_pending_validation)
        self.assertFalse(preserved.planning_result_completed)
        resumed = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base"),
        )

        self.assertEqual(resumed.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_error, preserved.planning_error)
        self.assertEqual(ports.state.clear_calls, [])

    def test_no_runnable_fallback_plans_once_then_stably_waits(self):
        ports = FakePorts(
            planning_snapshot(
                specification_fingerprint="specification",
                planning_fingerprint="old-full",
            )
        )
        ports.git.fingerprint = "new-full"
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=None,
            project_blocked=False,
        )

        first = PlanningLifecycleCoordinator(ports).run(self.context)
        second = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(second.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.run_count, 1)

    def test_archive_only_change_keeps_ready_and_success_baseline(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="old-specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "archive-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.git.requirement_archive_only = mock.Mock(return_value=True)
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_plan_only_remote_change_reselects_ready_from_latest_catalog(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base",
            specification_fingerprint="specification",
            planning_fingerprint="fingerprint",
        ))
        old_ready = SimpleNamespace(plan_id="G-001", priority="critical")
        new_ready = SimpleNamespace(plan_id="G-002", priority="critical")
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=old_ready, project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.git.remote = "new-base"
        ports.git.fingerprints["new-base"] = "fingerprint"
        activated = []
        original_activate = ports.state.activate_ready

        def activate_selected(repo, base_sha):
            activated.append(ports.state.schedule_result.runnable_ready.plan_id)
            return original_activate(repo, base_sha)

        ports.state.activate_ready = activate_selected

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(activated, [])
        self.assertIsNone(ports.state.snapshot.phase)
        self.assertEqual(ports.agent.run_count, 0)

        ports.git.head = "new-base"
        ports.state.schedule_result.runnable_ready = new_ready
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(activated, ["G-002"])
        self.assertEqual(ports.agent.run_count, 0)


class PreservedPlanningSelectionTest(PlanningCase):
    def failed_ports(self, **values):
        defaults = dict(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_workspace_owned=1, planning_attempt_fingerprint="fingerprint",
            planner_session_id="saved-session", planning_result="unpublished-result",
            planning_validation_error="failed validation", planning_retry_count=3,
            planning_error="saved failure", planning_error_fingerprint="fingerprint",
        )
        defaults.update(values)
        return FakePorts(planning_snapshot(**defaults))

    def test_same_input_releases_then_runs_independent_ready_without_planner(self):
        ports = self.failed_ports()
        original = ports.state.snapshot
        ports.state.schedule_result.runnable_ready = object()
        ports.state.schedule_result.has_ready = True
        ports.state.activate_result = object()
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        saved = ports.state.snapshot.planning_preserved_state
        self.assertEqual(json.loads(saved)["planner_session_id"], original.planner_session_id)
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertTrue(outcome.newly_activated)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_preserved_state, saved)
        self.assertEqual(ports.state.snapshot.planning_error, original.planning_error)
        self.assertFalse(any(args[:1] == ("push",) for _, args, _ in ports.git.calls))

    def test_no_independent_work_waits_without_completion_or_retirement(self):
        ports = self.failed_ports()
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        ports.retirement = SimpleNamespace(pending=mock.Mock())
        for _ in range(3):
            outcome = PlanningLifecycleCoordinator(ports).run(self.context)
            self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        ports.retirement.pending.assert_not_called()
        self.assertEqual(ports.agent.run_count, 0)

    def test_release_refuses_uncertain_ownership_operations_scope_and_publication(self):
        for reason in ("ownership", "operation", "agent", "scope", "intent", "maintenance-intent"):
            with self.subTest(reason=reason):
                ports = self.failed_ports()
                if reason == "ownership":
                    ports.git.ownership_error = PlanningWorkspaceOwnershipError("unknown owner")
                elif reason == "operation":
                    ports.git.validate_planning_release = mock.Mock(
                        side_effect=PlanningWorkspaceOwnershipError("unsealed Git operation"))
                elif reason == "agent":
                    ports.state.planning_release_pending = lambda _repo: True
                elif reason == "scope":
                    ports.state.snapshot = replace(ports.state.snapshot, agent_snapshot="scope")
                    ports.agent.validate_saved_scope = lambda *_args: PlanningScopeResult("shared ref changed")
                elif reason == "intent":
                    ports.state.general_intent = object()
                else:
                    ports.state.cycle = replace(ports.state.cycle, publication_stage="prepared")
                original = ports.state.snapshot
                run = PlanningLifecycleCoordinator(ports)._new_run(self.context)
                outcome = PlanningLifecycleCoordinator(ports)._selection._gate_preserved_error(run)
                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(ports.state.snapshot, original)
                self.assertEqual(ports.agent.run_count, 0)

    def test_active_pr_correction_keeps_slot_even_when_failed_input_changes(self):
        ports = self.failed_ports()
        PlanningLifecycleCoordinator(ports).run(self.context)
        saved = ports.state.snapshot.planning_preserved_state
        ports.state.snapshot = replace(ports.state.snapshot, phase="WORKING")
        ports.state.schedule_result.active = object()
        ports.git.fingerprint = "changed-input"
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertFalse(outcome.newly_activated)
        self.assertEqual(ports.state.snapshot.planning_preserved_state, saved)
        self.assertEqual(ports.agent.run_count, 0)

    def test_changed_input_restores_same_attempt_before_revalidation(self):
        ports = self.failed_ports()
        original = ports.state.snapshot
        PlanningLifecycleCoordinator(ports).run(self.context)
        ports.git.fingerprint = "changed-input"
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.snapshot, original)
        self.assertEqual(ports.agent.run_count, 0)

    def test_restored_validation_error_clears_only_after_saved_session_revalidation(self):
        ports = self.failed_ports(planning_error="failed validation", planning_error_origin="validation")
        PlanningLifecycleCoordinator(ports).run(self.context)
        ports.git.remote = "new-base"
        ports.git.fingerprints["new-base"] = "changed-input"
        ports.git.relevant_document_changes = mock.Mock(return_value=("docs/PRODUCT.md",))
        ports.validation.result = None
        updated = replace(self.context, base_sha="new-base")
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(updated).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.snapshot.planning_error, "failed validation")
        self.assertEqual(ports.agent.run_count, 0)
        outcome = PlanningLifecycleCoordinator(ports).run(updated)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertIsNone(ports.state.snapshot.planning_preserved_state)

    def test_remote_change_at_activation_keeps_preserved_result(self):
        ports = self.failed_ports()
        PlanningLifecycleCoordinator(ports).run(self.context)
        saved = ports.state.snapshot
        ports.state.schedule_result.runnable_ready = object()
        ports.git.remote = "new-base"
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.snapshot, saved)
        self.assertEqual(ports.agent.run_count, 0)


class PlanningReleaseGitTest(unittest.TestCase):
    def test_release_checks_shared_operations_unmerged_index_and_workspace(self):
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / 'planning'
            worktree.mkdir()
            for failure in ('none', 'worktree-operation', 'shared-operation', 'unmerged', 'missing'):
                with self.subTest(failure=failure):
                    effects = SimpleNamespace(
                        git=mock.Mock(side_effect=lambda _repo, *args, **_kwargs: SimpleNamespace(
                            stdout='conflict' if args == ('ls-files', '--unmerged') and failure == 'unmerged' else '')),
                        _nul_git_paths=mock.Mock(),
                        _git_operation_entries=lambda path: {'operation': 'active'} if (
                            failure == 'worktree-operation' and path == worktree
                            or failure == 'shared-operation' and path == root) else {},
                    )
                    port = _PlanningGitPorts(effects, None, root, root / 'state')
                    target = root / 'missing' if failure == 'missing' else worktree
                    if failure == 'none':
                        port.validate_planning_release('/repo', target)
                    else:
                        with self.assertRaises(PlanningWorkspaceOwnershipError):
                            port.validate_planning_release('/repo', target)

    def test_release_pending_agent_is_read_without_modifying_invocations(self):
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.storage import agent_invocation_state
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        agent_invocation_state.ensure_schema(connection)
        connection.execute("""INSERT INTO agent_invocations (
            invocation_id, repo, role, provider, model, effort, transition, attempt, started_at
        ) VALUES ('call', '/repo', 'planner', 'codex', 'model', 'high', 'start', 1, 'time')""")
        port = _PlanningStatePorts(None, connection, None, None, Path('/state'))
        self.assertTrue(port.planning_release_pending('/repo'))
        self.assertFalse(port.planning_release_pending('/other'))
        connection.execute("UPDATE agent_invocations SET finished_at = 'time'")
        self.assertFalse(port.planning_release_pending('/repo'))
