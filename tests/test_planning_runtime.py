from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import unittest
from unittest import mock

import loop_manager
import maintenance_state
from planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from planning_runtime import (
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
from runtime_state import PlanningSnapshot


def planning_snapshot(**overrides):
    values = {
        "repo": "/repo",
        "phase": None,
        "processed_sha": None,
        "specification_fingerprint": None,
        "planning_fingerprint": None,
        "planning_attempt_fingerprint": None,
        "planning_workspace_owned": 0,
        "planning_retry_count": 0,
        "planner_session_id": None,
        "planning_validation_error": None,
        "planning_result_pending_validation": False,
        "planning_result_completed": False,
        "planning_result": None,
        "planning_error": None,
        "planning_error_fingerprint": None,
        "active_sha": None,
        "worktree": None,
        "last_error": None,
        "agent_role": None,
        "agent_snapshot": None,
        "agent_result_snapshot": None,
    }
    values.update(overrides)
    return PlanningSnapshot(**values)


def maintenance_cycle(**overrides):
    values = {
        "repo": "/repo",
        "last_success_commit": None,
        "last_success_merged_count": None,
        "phase": maintenance_state.MaintenancePhase.IDLE,
        "cycle_base_sha": None,
        "cycle_start_merged_count": None,
        "prerequisite_plan_ids": (),
        "refactoring_plan_ids": (),
        "revalidation_plan_ids": (),
        "publication_stage": None,
        "publication_base_sha": None,
        "publication_commit_sha": None,
        "publication_remote_oid": None,
        "publication_refactoring_plan_ids": (),
        "publication_revalidation_plan_ids": (),
        "include_general_planning": False,
        "revision": 0,
    }
    values.update(overrides)
    return maintenance_state.MaintenanceCycle(**values)


class FakeStatePort:
    def __init__(self, snapshot, cycle=None):
        self.snapshot = snapshot
        self.cycle = cycle or maintenance_cycle()
        self.updates = []
        self.clear_calls = []
        self.activate_result = None
        self.recovered_cycle = self.cycle
        self.recover_calls = []
        self.validation_cleared = False
        self.general_intent = None
        self.general_intent_record_count = 0
        self.general_intent_error_call = None
        self.schedule_result = SimpleNamespace(
            active=None,
            has_ready=False,
            has_waiting=False,
            runnable_ready=None,
            project_blocked=False,
        )

    def planning(self, _repo):
        return self.snapshot

    def maintenance_cycle(self, _repo):
        return self.cycle

    def update(self, _repo, **values):
        self.updates.append(values)
        snapshot_values = {
            key: value
            for key, value in values.items()
            if key in self.snapshot.__dataclass_fields__
        }
        if snapshot_values:
            self.snapshot = replace(self.snapshot, **snapshot_values)

    def transaction(self):
        return nullcontext()

    def advance_maintenance_cycle(self, _repo, _base_sha):
        return self.cycle

    def schedule(self, _repo):
        return self.schedule_result

    def maintenance_due(self, _repo, _interval):
        return SimpleNamespace(required=False)

    def begin_maintenance_assessment(self, *_args, **_kwargs):
        return self.cycle, False

    def restart_stale_maintenance(self, _repo, _expected, base_sha):
        self.cycle = replace(
            self.cycle,
            cycle_base_sha=base_sha,
            revision=self.cycle.revision + 1,
        )
        return self.cycle

    def activate_ready(self, _repo, _base_sha):
        if self.activate_result is not None:
            self.snapshot = replace(self.snapshot, phase="WORKING")
        return self.activate_result

    def preserve_diagnostics(self, _repo, _snapshot):
        pass

    def clear_active(
        self,
        _repo,
        *,
        processed_sha=None,
        specification_fingerprint=None,
        planning_fingerprint=None,
    ):
        self.clear_calls.append((processed_sha, planning_fingerprint))
        self.snapshot = replace(
            self.snapshot,
            phase=None,
            active_sha=None,
            worktree=None,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )

    def clear_active_in_transaction(self, repo, **values):
        self.clear_active(repo, **values)

    def clear_validation(self, _repo):
        self.validation_cleared = True

    def compact_catalog(self, _repo):
        return "[]"

    def execplan_rows(self, _repo):
        return []

    def transition_maintenance(self, *_args, **_kwargs):
        raise AssertionError("unexpected maintenance transition")

    def advance_success_baseline(self, *_args, **_kwargs):
        raise AssertionError("unexpected maintenance baseline")

    def recover_maintenance_batch(self, repo, worktree, base):
        self.recover_calls.append((repo, worktree, base))
        return self.recovered_cycle

    def record_maintenance_intent(self, *_args, **_kwargs):
        raise AssertionError("unexpected maintenance publication intent")

    def publish_maintenance_batch(self, *_args, **_kwargs):
        raise AssertionError("unexpected maintenance publication")

    def register_execplans(self, *_args, **_kwargs):
        raise AssertionError("base race must happen before catalog registration")

    def general_publication(self, _repo):
        return self.general_intent

    def recover_general_batch(self, *_args):
        self.clear_active("/repo")
        return "restart"

    def record_general_intent(self, *_args):
        self.general_intent_record_count += 1
        if self.general_intent_record_count == self.general_intent_error_call:
            raise RuntimeError("mutable READY changed before push")
        self.general_intent = object()

    def publish_general_batch(
        self,
        _repo,
        _worktree,
        _base,
        _result,
        plan_commit,
        specification_fingerprint,
        planning_fingerprint,
    ):
        self.general_intent = None
        self.clear_active(
            "/repo",
            processed_sha=plan_commit,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )


class FakeGitPort:
    def __init__(self):
        self.head = "base"
        self.remote = "base"
        self.fingerprint = "fingerprint"
        self.specification_fingerprints = {}
        self.dirty_files = []
        self.ancestor = True
        self.calls = []
        self.ownership_checks = []
        self.workspace_error = None

    def run(self, repo, *arguments, check=True):
        self.calls.append((str(repo), arguments, check))
        if arguments[:1] == ("commit",):
            self.head = "plan-commit"
        if arguments[:2] == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout=f"{self.head}\n", returncode=0)
        if arguments[:1] == ("rev-parse",):
            return SimpleNamespace(stdout=f"{self.remote}\n", returncode=0)
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return SimpleNamespace(
                stdout="", returncode=0 if self.ancestor else 1
            )
        return SimpleNamespace(stdout="", returncode=0)

    def input_fingerprint(self, _repo_path, _sha):
        return self.fingerprint

    def specification_fingerprint(self, _repo_path, sha):
        return self.specification_fingerprints.get(sha, "specification")

    def requirement_archive_only(self, *_args):
        return False

    def validate_workspace_identity(self, *_args):
        pass

    def validate_worktree_ownership(self, repo, fingerprint, worktree):
        self.ownership_checks.append((repo, fingerprint, worktree))

    def make_planning_worktree(self, *_args, **_kwargs):
        if self.workspace_error is not None:
            raise self.workspace_error

    def changed_files(self, _worktree, _since=None):
        return list(self.dirty_files)

    def validate_managed_worktree(self, _worktree):
        pass


class FakeAgentPort:
    def __init__(self):
        self.run_count = 0
        self.resumed_session = None
        self.result = PlanningAgentResult(
            0,
            "planner result",
            "session-1",
            PlanningAgentResultKind.SUCCESS,
        )

    def begin_scope(self, _role, _worktree):
        return object()

    def run_planner(
        self,
        _worktree,
        _prompt,
        session_id,
        _on_session_started,
    ):
        self.run_count += 1
        self.resumed_session = session_id
        return self.result

    def finish_scope(self, _role, _worktree, _before):
        return PlanningScopeResult()

    def validate_saved_scope(self, _role, _worktree, _snapshot):
        return PlanningScopeResult()

    def clear_scope(self, _repo):
        pass


class FakeValidationPort:
    def __init__(self):
        self.general_error = None
        snapshot = MutableReadySnapshot(
            repo="/repo",
            active_sha="base",
            plans=(),
            token=mutable_snapshot_token("/repo", "base", ()),
        )
        self.snapshot = snapshot
        self.result = SimpleNamespace(
            plans=(("G-001", {"id": "G-001", "title": "Plan"}),),
            publication_paths=("docs/plans/[G-001] Plan.md",),
            revisions=(),
            deletions=(),
            mutable_snapshot=snapshot,
        )

    def maintenance_context_at_revision(self, *_args):
        raise AssertionError("unexpected maintenance context")

    def prepare_maintenance_context(self, *_args):
        raise AssertionError("unexpected maintenance context")

    def maintenance_change_context(self, *_args):
        raise AssertionError("unexpected maintenance context")

    def prepare_general_context(self, *_args):
        return self.snapshot

    def planner_prompt(self, error, catalog, _mutable_snapshot):
        return f"{error or ''}:{catalog}"

    def maintenance_prompt(self, *_args, **_kwargs):
        raise AssertionError("unexpected maintenance prompt")

    def validate_general_result(self, *_args):
        if self.general_error is not None:
            raise PlanningResultInvalid(self.general_error)
        return self.result

    def validate_maintenance_result(self, *_args, **_kwargs):
        raise AssertionError("unexpected maintenance validation")

    def validate_publication_base(self, *_args):
        pass


class FakeEventPort:
    def __init__(self):
        self.messages = []

    def log(self, message):
        self.messages.append(("log", message))

    def info(self, message):
        self.messages.append(("info", message))

    def error(self, message):
        self.messages.append(("error", message))


class FakePorts:
    def __init__(self, snapshot, cycle=None):
        self.state = FakeStatePort(snapshot, cycle)
        self.git = FakeGitPort()
        self.agent = FakeAgentPort()
        self.validation = FakeValidationPort()
        self.events = FakeEventPort()


class PlanningLifecycleCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.context = PlanningContext(
            repo="/repo",
            repo_path=Path("/repo"),
            state_dir=Path("/state"),
            base_branch="main",
            base_sha="base",
            provider="codex",
            config_path=Path("/config.ini"),
            registered_only=False,
            refactoring_review_interval=5,
        )

    def test_matching_fingerprint_skips_agent_and_completes_no_change(self):
        ports = FakePorts(
            planning_snapshot(planning_fingerprint="fingerprint")
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.FINGERPRINT_SKIPPED)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.processed_sha, "base")

    def test_validation_retry_resumes_session_and_persists_retry_transition(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planner_session_id="session-1",
                planning_validation_error="first invalid result",
                planning_retry_count=1,
            )
        )
        ports.validation.general_error = "second invalid result"

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.agent.resumed_session, "session-1")
        self.assertEqual(ports.state.snapshot.planning_retry_count, 2)
        self.assertEqual(
            ports.state.snapshot.planning_validation_error,
            "second invalid result",
        )
        self.assertFalse(
            ports.state.snapshot.planning_result_pending_validation
        )

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

    def test_transient_git_error_escapes_without_persisting_planning_error(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
            )
        )
        ports.git.workspace_error = RuntimeError("index.lock exists")

        with self.assertRaisesRegex(RuntimeError, "index.lock exists"):
            PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertFalse(
            any("planning_error" in update for update in ports.state.updates)
        )

    def test_workspace_ownership_error_is_persisted_for_same_fingerprint(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
            )
        )
        ports.git.workspace_error = PlanningWorkspaceOwnershipError(
            "workspace belongs to another task"
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(
            ports.state.snapshot.planning_error,
            "planning workspace 소유권 검증 실패: workspace belongs to another task",
        )

    def test_remote_base_race_discards_commit_before_push_or_registration(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.git.remote = "concurrent-base"
        ports.git.ancestor = False

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(
            outcome.kind, PlanningOutcomeKind.PUBLICATION_RECOVERY
        )
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertIn(("commit", "-m", "Add ExecPlan G-001: Plan"), commands)
        self.assertNotIn(
            ("push", "origin", "HEAD:refs/heads/main"), commands
        )
        self.assertEqual(len(ports.state.clear_calls), 1)

    def test_mutable_ready_race_after_fetch_stops_before_push(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.general_intent_error_call = 2

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(
            ports.state.snapshot.planning_error,
            "mutable READY changed before push",
        )
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertNotIn(
            ("push", "origin", "HEAD:refs/heads/main"), commands
        )

    def test_prepared_publication_recovers_before_agent_or_validation(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base",
            publication_stage="prepared",
            publication_base_sha="old-base",
            publication_commit_sha="plan-commit",
            revision=3,
        )
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="maintenance-fingerprint",
                planning_workspace_owned=1,
            ),
            cycle,
        )
        ports.git.remote = "published-base"
        ports.state.recovered_cycle = replace(
            cycle,
            publication_stage="remote_confirmed",
            publication_remote_oid="published-base",
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(
            outcome.kind, PlanningOutcomeKind.PUBLICATION_RECOVERY
        )
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(
            ports.state.recover_calls,
            [("/repo", Path("/planning"), "main")],
        )

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

    def test_unchanged_specification_activates_ready_before_changed_full_input(self):
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
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)

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


class PlanningDecisionTest(unittest.TestCase):
    def test_specification_change_precedes_runnable_ready(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=True,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(
            decision.kind,
            PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
        )

    def test_unchanged_specification_prefers_ready_before_fallback(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=False,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(decision.kind, PlanningDecisionKind.ACTIVATE_READY)

    def test_changed_full_input_falls_back_only_without_runnable_ready(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=False,
            runnable_ready=False,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(
            decision.kind,
            PlanningDecisionKind.RUN_FALLBACK_PLANNING,
        )


if __name__ == "__main__":
    unittest.main()
