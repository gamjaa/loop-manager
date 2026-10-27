from tests.support.fixed_samples import use_fixed_guidance
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
from loop_manager_internal.storage import maintenance_state, planning_review_state
from loop_manager_internal.planning.planning_workspace import legacy_release_identity
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
from loop_manager_internal.planning.assessment_port import MaintenanceAssessmentPort


def confirm_final_review(connection, repo, revision, input_fingerprint, specification_fingerprint):
    identity = {
        "revision": revision, "input_fingerprint": input_fingerprint,
        "specification_fingerprint": specification_fingerprint,
        "work_cycle": planning_review_state.work_cycle(connection, repo),
    }
    planning_review_state.begin(connection, repo, purpose="final", comparison_revision=None, **identity)
    result = "FINAL_SPECIFICATION_REVIEW: " + json.dumps({
        **identity, "outcome": "fulfilled", "evidence": ["docs/PRODUCT.md", "src/app.py"],
        "reason": "Fixture models a previously confirmed full specification review.",
    })
    planning_review_state.validate(connection, repo, revision, result, "fulfilled")
    planning_review_state.confirm(connection, repo, revision, revision)


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
        "planning_error_incident_id": None,
        "planning_error_origin": None,
        "planning_correction_error": None,
        "planning_correction_incident_id": None,
        "planning_correction_sha": None,
        "planning_correction_context": None,
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
        "file_assessment_start_merged_count": None,
        "last_success_file_assessment_merged_count": None,
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
        "full_assessment_completed": False,
        "full_assessment_source_sha": None,
        "full_assessment_result": None,
        "revision": 0,
    }
    values.update(overrides)
    return maintenance_state.MaintenanceCycle(**values)


class FakeStatePort:
    def prepare_file_assessment(self, repo, repository, base_branch, source_sha, evidence):
        return evidence

    def prepare_legacy_release(self, expected, cycle):
        if self.snapshot != expected or self.cycle != cycle:
            raise RuntimeError("release input changed")
        self.cycle = replace(cycle, legacy_release_identity=legacy_release_identity(expected, cycle))
        return self.cycle

    def complete_legacy_release(self, expected, cycle):
        with self.transaction():
            if self.snapshot != expected or self.cycle != cycle:
                raise RuntimeError("release input changed")
            self.clear_active_in_transaction(expected.repo)
            self.cycle = replace(cycle, legacy_release_identity=None)

    def __init__(self, snapshot, cycle=None):
        self.snapshot = snapshot
        self.cycle = cycle or maintenance_cycle()
        self.updates = []
        self.diagnostics = []
        self.remote_diagnostics = []
        self.local_recheck_results = set()
        self.clear_calls = []
        self.activate_result = None
        self.recovered_cycle = self.cycle
        self.recover_calls = []
        self.validation_cleared = False
        self.general_intent = None
        self.general_recovery_result = "restart"
        self.general_intent_record_count = 0
        self.general_intent_error_call = None
        self.maintenance_due_self_repository = None
        self.maintenance_due_required = False
        self.maintenance_due_reason = "interval_reached"
        self.maintenance_begin_count = 0
        self.plan_declarations_match_result = True
        self.check_record = None
        self.has_general_baseline = False
        self.schedule_result = SimpleNamespace(
            active=None,
            has_ready=False,
            has_waiting=False,
            runnable_ready=None,
            project_blocked=False,
        )

    def reconcile_catalog(self, *_args):
        pass

    def planning(self, _repo):
        return self.snapshot

    def plan_declarations_match(self, _repo, _entries):
        return self.plan_declarations_match_result

    def planning_release_pending(self, _repo):
        return False

    def pause_execution_for_planning(self, expected, plan_id):
        self.paused_execution = expected
        self.snapshot = replace(expected, phase=None, active_sha=None, worktree=None,
                                planning_resume_plan_id=plan_id)

    def resume_execution_after_planning(self, _repo):
        original = self.paused_execution
        self.snapshot = replace(self.snapshot, phase=original.phase, active_sha=original.active_sha,
                                worktree=original.worktree, planning_resume_plan_id=None)

    def suspend_planning(self, expected, *, unconfirmed_plan_ids=None):
        from loop_manager_internal.storage.runtime_state import PLANNING_CLEAR_VALUES, EXECUTION_CLEAR_VALUES
        saved = {key: getattr(expected, key, default)
                 for key, default in {**EXECUTION_CLEAR_VALUES, **PLANNING_CLEAR_VALUES}.items()}
        if unconfirmed_plan_ids is not None:
            saved['unconfirmed_plan_ids'] = list(unconfirmed_plan_ids)
        values = {key: value for key, value in PLANNING_CLEAR_VALUES.items()
                  if key in expected.__dataclass_fields__ and not key.startswith("planning_error")}
        self.snapshot = replace(expected, **values, phase=None, active_sha=None,
                                worktree=None, agent_role=None, agent_snapshot=None,
                                agent_result_snapshot=None, last_error=None,
                                planning_preserved_state=json.dumps(saved))

    def resume_planning(self, expected):
        values = {key: value for key, value in json.loads(expected.planning_preserved_state).items()
                  if key in expected.__dataclass_fields__}
        self.snapshot = replace(expected, **values, planning_preserved_state=None)

    def general_baseline(self, _repo):
        return self.has_general_baseline

    def planner_check(self, _repo):
        return self.check_record

    def begin_planner_check(self, _repo, **values):
        self.check_record = {**values, "stage": "pending", "session_id": None,
                             "result": None, "error": None, "retry_count": 0}

    def advance_planner_check(self, _repo, expected_stage, next_stage, *,
                              expected_input=None, expected_target=None, **values):
        if self.check_record is None or self.check_record["stage"] != expected_stage:
            return False
        if (expected_input is not None
                and self.check_record["input_fingerprint"] != expected_input):
            return False
        if (expected_target is not None
                and self.check_record["target_fingerprint"] != expected_target):
            return False
        self.check_record.update(values)
        self.check_record["stage"] = next_stage
        return True

    def record_general_plan_declarations(self, _repo, _revision, _entries):
        self.plan_declarations_match_result = True

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

    def local_recheck_consumed(self, request_id):
        return request_id in self.local_recheck_results

    def update_local_recheck_result(self, repo, request_id, **values):
        self.update(repo, **values)
        self.local_recheck_results.add(request_id)

    def release_planning_correction(self, _repo, *, error, active_sha, context):
        row = self.snapshot
        if not (row.planning_error == row.planning_correction_error == error
                and row.planning_error_incident_id is not None
                and row.planning_error_incident_id == row.planning_correction_incident_id
                and row.active_sha == row.planning_correction_sha == active_sha
                and row.planning_correction_context == context
                and row.planning_result_completed):
            return False
        self.update(_repo, planning_error=None, planning_error_fingerprint=None,
                    planning_error_incident_id=None,
                    planning_correction_error=None, planning_correction_incident_id=None,
                    planning_correction_sha=None,
                    planning_correction_context=None)
        return True

    def transaction(self):
        @contextmanager
        def restore_on_error():
            before = (self.snapshot, self.cycle, self.general_intent)
            try:
                yield
            except BaseException:
                self.snapshot, self.cycle, self.general_intent = before
                raise

        return restore_on_error()

    def advance_maintenance_cycle(self, _repo, _base_sha):
        return self.cycle

    def schedule(self, _repo):
        return self.schedule_result

    def maintenance_due(self, _repo, _interval, *, self_repository=False,
                        completion_candidate=False):
        self.maintenance_due_self_repository = self_repository
        return SimpleNamespace(required=self.maintenance_due_required,
                               reason=self.maintenance_due_reason)

    def begin_maintenance_assessment(self, *_args, **_kwargs):
        self.maintenance_begin_count += 1
        self.cycle = replace(
            self.cycle, phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha=_args[2], revision=self.cycle.revision + 1,
        )
        return self.cycle, True

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
        self.diagnostics.append(_snapshot)

    def retry_diagnostic_preservation(self, _repo, _snapshot):
        self.remote_diagnostics.append(_snapshot)

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
            planning_error=None,
            planning_error_fingerprint=None,
            planning_attempt_fingerprint=None,
            planning_workspace_owned=0,
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

    def record_maintenance_scope(self, _repo, _revision, plan_ids, prerequisite_plan_ids=()):
        self.cycle = replace(self.cycle, assessment_plan_ids=plan_ids, prerequisite_plan_ids=prerequisite_plan_ids)

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
        if self.general_recovery_result != "pending":
            self.clear_active("/repo")
        return self.general_recovery_result

    def supersede_unpublished_general_intent(self, _repo):
        self.general_intent = None

    def supersede_unpublished_maintenance_intent(self, _repo, cycle, _base):
        return cycle

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
    def execution_blocks_planning(self, _worktree):
        return False

    def __init__(self):
        self.head = "base"
        self.remote = "base"
        self.fingerprint = "fingerprint"
        self.fingerprints = {}
        self.nonplan_fingerprints = {}
        self.specification_fingerprints = {}
        self.dirty_files = []
        self.ancestor = True
        self.calls = []
        self.ownership_checks = []
        self.ownership_error = None
        self.workspace_error = None
        self.remove_error = None
        self.advance_remote_on_push = False
        self.guidance_changed = False
        self.diff_name_status = ""

    def runtime_guidance_changed(self, _repo_path):
        return False

    def fetch_base(self, repo_path, base):
        self.calls.append((str(repo_path), ("recorded_base_fetch", base), True))
        return self.remote

    def save_planning_correction(self, *_args):
        pass

    def planning_correction(self, *_args):
        return None

    def run(self, repo, *arguments, check=True):
        self.calls.append((str(repo), arguments, check))
        if arguments[:2] == ("worktree", "remove") and self.remove_error:
            raise self.remove_error
        if arguments[:1] == ("commit",):
            self.head = "plan-commit"
        if arguments[:1] == ("push",) and self.advance_remote_on_push:
            self.remote = self.head
        if arguments[:2] == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout=f"{self.head}\n", returncode=0)
        if arguments[:1] == ("rev-parse",):
            return SimpleNamespace(stdout=f"{self.remote}\n", returncode=0)
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return SimpleNamespace(
                stdout="", returncode=0 if self.ancestor else 1
            )
        if arguments[:2] == ("diff", "--name-only") and arguments[-1:] == ("AGENTS.md",):
            return SimpleNamespace(
                stdout="AGENTS.md\n" if self.guidance_changed else "", returncode=0,
            )
        if arguments[:2] == ("diff", "--name-status"):
            return SimpleNamespace(stdout=self.diff_name_status, returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    def input_fingerprint(self, _repo_path, _sha):
        return self.fingerprints.get(_sha, self.fingerprint)

    def nonplan_fingerprint(self, _repo_path, sha):
        return self.nonplan_fingerprints.get(sha, self.fingerprints.get(sha, self.fingerprint))

    def plan_declarations(self, _repo_path, _sha):
        return {}

    def specification_fingerprint(self, _repo_path, sha):
        return self.specification_fingerprints.get(sha, "specification")

    def requirement_archive_only(self, *_args, **_kwargs):
        return False

    def validate_workspace_identity(self, *_args):
        pass

    def validate_worktree_ownership(self, repo, fingerprint, worktree):
        self.ownership_checks.append((repo, fingerprint, worktree))
        if self.ownership_error is not None:
            raise self.ownership_error

    def validate_planning_release(self, _repo, _worktree):
        pass

    def make_planning_worktree(self, *_args, **_kwargs):
        if self.workspace_error is not None:
            raise self.workspace_error

    def changed_files(self, _worktree, _since=None):
        return list(self.dirty_files)

    def rebase_planning_workspace(self, _repo, _fingerprint, _worktree, _old, target):
        self.validate_worktree_ownership(_repo, _fingerprint, _worktree)
        self.head = target
        return SimpleNamespace(target=target, changed=True, conflict=False)

    def complete_planning_rebase(self, *_args):
        self.calls.append((None, ("complete_planning_rebase", *_args), True))

    def accept_resolved_stash_conflict(self, *_args):
        pass

    def relevant_document_changes(self, *_args, **_kwargs):
        return ()

    def validate_managed_worktree(self, _worktree):
        pass


class FakeAgentPort:
    def prompt_session(self, session_id):
        return session_id

    def __init__(self):
        self.run_count = 0
        self.resumed_session = None
        self.roles = []
        self.prompts = []
        self.invocation_metadata = []
        self.results = []
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
        *, profile_role=None, read_only=False, purpose=None, target=None,
    ):
        self.run_count += 1
        self.roles.append((profile_role, read_only))
        self.prompts.append(_prompt)
        self.invocation_metadata.append((purpose, target))
        self.resumed_session = session_id
        return self.results.pop(0) if self.results else self.result

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
            retirement_moves=(),
            restoration_moves=(),
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
        self.assessment = MaintenanceAssessmentPort()



class PlanningCase(unittest.TestCase):
    def setUp(self):
        use_fixed_guidance(self)
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
