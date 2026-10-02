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
import notifications
import maintenance_state
import maintenance_file_review
import maintenance_findings
import maintenance_reconciliation
from maintenance_file_inventory import RootPlacement, TrackedSource
from tests.test_maintenance_file_review import result_for
from planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from maintenance_orchestration import maintenance_planning_fingerprint
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
        self.general_recovery_result = "restart"
        self.general_intent_record_count = 0
        self.general_intent_error_call = None
        self.maintenance_due_self_repository = None
        self.maintenance_due_required = False
        self.maintenance_due_reason = "interval_reached"
        self.maintenance_begin_count = 0
        self.document_approval_pending = False
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

    def pending_document_approval(self, _repo, _repository, _base_branch):
        return self.document_approval_pending

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
        self.fingerprints = {}
        self.specification_fingerprints = {}
        self.dirty_files = []
        self.ancestor = True
        self.calls = []
        self.ownership_checks = []
        self.ownership_error = None
        self.workspace_error = None
        self.remove_error = None
        self.advance_remote_on_push = False

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
        return SimpleNamespace(stdout="", returncode=0)

    def input_fingerprint(self, _repo_path, _sha):
        return self.fingerprints.get(_sha, self.fingerprint)

    def specification_fingerprint(self, _repo_path, sha):
        return self.specification_fingerprints.get(sha, "specification")

    def requirement_archive_only(self, *_args):
        return False

    def validate_workspace_identity(self, *_args):
        pass

    def validate_worktree_ownership(self, repo, fingerprint, worktree):
        self.ownership_checks.append((repo, fingerprint, worktree))
        if self.ownership_error is not None:
            raise self.ownership_error

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
    def test_unpublished_planning_slot_yields_only_clean_owned_base_worktree(self):
        for guard in ("clean", "dirty", "committed", "ownership", "publication", "project_blocked", "active"):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory() as directory:
                snapshot = planning_snapshot(
                    phase="PLANNING", active_sha="base", worktree=directory,
                    planning_attempt_fingerprint="attempt", planning_workspace_owned=1,
                    processed_sha="old", planning_fingerprint="successful-input",
                    specification_fingerprint="successful-specification",
                )
                cycle = maintenance_cycle(
                    phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                    cycle_base_sha="base", last_success_merged_count=5,
                    last_success_file_assessment_merged_count=5,
                )
                ports = FakePorts(snapshot, cycle)
                ports.state.schedule_result.runnable_ready = {"plan_id": "G-058"}
                if guard == "dirty":
                    original = ports.git.run
                    ports.git.run = lambda path, *args: (
                        SimpleNamespace(stdout="?? user.txt", returncode=0)
                        if args == ("status", "--porcelain") else original(path, *args)
                    )
                elif guard == "committed":
                    ports.git.head = "new-commit"
                elif guard == "ownership":
                    ports.git.ownership_error = PlanningWorkspaceOwnershipError("foreign workspace")
                elif guard == "publication":
                    ports.state.general_intent = object()
                elif guard == "project_blocked":
                    ports.state.schedule_result.project_blocked = True
                elif guard == "active":
                    ports.state.schedule_result.active = {"plan_id": "G-059"}
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                if guard == "ownership":
                    with self.assertRaises(PlanningWorkspaceOwnershipError):
                        coordinator._select_planning_work(run)
                else:
                    outcome = coordinator._select_planning_work(run)
                    if guard == "clean":
                        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
                        self.assertIsNone(ports.state.snapshot.phase)
                        self.assertEqual(ports.state.snapshot.planning_fingerprint, "successful-input")
                        self.assertEqual(ports.state.snapshot.processed_sha, "old")
                    else:
                        self.assertIsNone(outcome)
                        self.assertEqual(ports.state.snapshot, snapshot)
                self.assertEqual(ports.state.cycle, cycle)

    def test_assessment_resume_restarts_selection_if_remote_base_changes(self):
        ports = FakePorts(planning_snapshot(), maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT, cycle_base_sha="base",
        ))
        ports.state.schedule_result.runnable_ready = {"plan_id": "G-058"}
        ports.state.activate_ready = mock.Mock()
        ports.git.remote = "new-base"
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._select_planning_work(coordinator._new_run(self.context))
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
                outcome = coordinator._select_planning_work(run)
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

        outcome = coordinator._run_planner_turn(run)

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

        outcome = coordinator._run_planner_turn(run)

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
        self.assertEqual(coordinator._validate_planner_result(run).kind, PlanningOutcomeKind.WAITING)
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
        outcome = coordinator._reconcile_one_relation(run, Path("/worktree"))
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
        with mock.patch.object(coordinator, "_review_one_file", return_value=None):
            for _ in maintenance_reconciliation.synthesis_pages(progress):
                result = coordinator._run_planner_turn(run)
                self.assertEqual(result.kind, PlanningOutcomeKind.WAITING)
            self.assertEqual(coordinator._run_planner_turn(run).kind, PlanningOutcomeKind.WAITING)
            self.assertIsNone(coordinator._run_planner_turn(run))
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

        self.assertIsNone(coordinator._run_planner_turn(run))

        ports.file_review.progress.assert_not_called()
        ports.file_review.completed_summary.assert_not_called()
        self.assertEqual(ports.agent.run_count, 1)

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

        self.assertIsNone(coordinator._select_planning_work(run))
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

    def test_registered_self_flag_only_reaches_maintenance_due_for_self_context(self):
        for self_repository in (False, True):
            ports = FakePorts(planning_snapshot())
            run = PlanningLifecycleCoordinator(ports)._new_run(
                replace(self.context, registered_only=True,
                        self_repository=self_repository)
            )
            PlanningLifecycleCoordinator(ports)._select_planning_work(run)
            self.assertEqual(ports.state.maintenance_due_self_repository,
                             self_repository)

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
            outcome = PlanningLifecycleCoordinator(ports)._select_planning_work(run)
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
        PlanningLifecycleCoordinator(ports)._select_planning_work(run)
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
        with mock.patch("planning_runtime.full_assessment.source_inventory",
                        return_value={"docs/PRODUCT.md": "hash"}):
            outcome = coordinator._validate_planner_result(run)
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
        with mock.patch("planning_runtime.full_assessment.source_inventory",
                        return_value={"module.py": "b" * 64}):
            outcome = coordinator._validate_planner_result(run)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 2)
        self.assertIn("발견 항목", ports.state.snapshot.planning_validation_error)

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

    def test_changed_failed_input_without_workspace_starts_new_planning(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.clear_calls[0], (None, None))
        self.assertIsNone(ports.state.snapshot.planning_error)

    def test_changed_input_without_saved_workspace_keeps_session_evidence(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
            planner_session_id="saved-session",
            planning_validation_error="saved diagnostics",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                         "old-input")
        self.assertEqual(ports.state.snapshot.planner_session_id,
                         "saved-session")
        self.assertEqual(ports.state.snapshot.planning_validation_error,
                         "saved diagnostics")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)

    def test_changed_input_without_workspace_preserves_phase_or_active_sha(self):
        for phase, active_sha in (("PLANNING", None), (None, "base")):
            with self.subTest(phase=phase, active_sha=active_sha):
                ports = FakePorts(planning_snapshot(
                    phase=phase,
                    active_sha=active_sha,
                    planning_error="saved failure",
                    planning_error_fingerprint="old-input",
                ))

                outcome = PlanningLifecycleCoordinator(ports).run(self.context)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(ports.state.snapshot.phase, phase)
                self.assertEqual(ports.state.snapshot.active_sha, active_sha)
                self.assertEqual(ports.state.snapshot.planning_error,
                                 "saved failure")
                self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                                 "old-input")
                self.assertEqual(ports.state.clear_calls, [])
                self.assertEqual(ports.agent.run_count, 0)

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
        for _ in range(4):
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha="advanced-base")
            )
            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot, before)
            self.assertEqual(ports.state.cycle, cycle)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.git.ownership_checks, [])

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
        ), cycle)
        ports.git.fingerprints = {"base": "old-input", "advanced-base": "new-input"}

        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="advanced-base")
        )

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.git.ownership_checks,
                         [("/repo", fingerprint, Path("/planning"))])
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertEqual(ports.state.cycle.cycle_base_sha, "advanced-base")
        self.assertEqual(ports.agent.run_count, 0)

    def test_changed_failed_input_cleans_owned_workspace_before_replanning(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="old-input",
            planning_workspace_owned=1,
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.git.ownership_checks[0],
                         ("/repo", "old-input", Path("/planning")))
        self.assertEqual(ports.state.clear_calls[0], (None, None))
        self.assertEqual(ports.agent.run_count, 1)

    def test_changed_failed_input_keeps_error_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="old-input",
                planning_workspace_owned=1,
                planning_error="saved failure",
                planning_error_fingerprint="old-input",
                planning_validation_error="saved diagnostics",
            ))
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "old-input")
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)

    def test_changed_failed_input_keeps_unowned_workspace_and_diagnostics(self):
        before = planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="old-input",
            planning_workspace_owned=0,
            planning_error="saved ownership failure",
            planning_error_fingerprint="old-input",
            planning_validation_error="saved diagnostics",
            planner_session_id="saved-session",
        )
        ports = FakePorts(before)
        ports.git.ownership_error = PlanningWorkspaceOwnershipError(
            "workspace owner is unknown"
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error,
                         "saved ownership failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                         "old-input")
        self.assertEqual(ports.state.snapshot.planning_validation_error,
                         "saved diagnostics")
        self.assertEqual(ports.state.snapshot.planner_session_id,
                         "saved-session")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)

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

    def test_general_publication_cleanup_failure_keeps_saved_planning_state(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved publication failure",
                planning_error_fingerprint="fingerprint",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            )
            ports = FakePorts(before)
            ports.state.general_intent = object()
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved publication failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)

    def test_verified_pending_general_publication_resumes_saved_result(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="fingerprint",
            planning_workspace_owned=1,
            planning_error="saved push failure",
            planning_error_fingerprint="fingerprint",
            planning_result="saved result",
            planning_result_completed=True,
        ))
        ports.state.general_intent = object()
        ports.state.general_recovery_result = "pending"
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.git.advance_remote_on_push = True

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertNotEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR,
                            ports.events.messages)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertTrue(any(
            arguments[:1] == ("push",)
            for _repo, arguments, _check in ports.git.calls
        ))

    def test_pending_general_publication_cleanup_failure_restores_saved_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved push failure",
                planning_error_fingerprint="fingerprint",
                planning_validation_error="saved diagnostics",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_pending_validation=True,
                planning_result_completed=True,
            )
            ports = FakePorts(before)
            ports.state.general_intent = object()
            ports.state.general_recovery_result = "pending"
            def apply_batch(*_args):
                ports.state.general_intent = None
                ports.state.clear_active("/repo")
                ports.state.update(
                    "/repo",
                    planning_validation_error=None,
                    planner_session_id=None,
                    planning_result=None,
                    planning_result_pending_validation=False,
                    planning_result_completed=False,
                )
            ports.state.publish_general_batch = mock.Mock(side_effect=apply_batch)
            ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
            ports.git.advance_remote_on_push = True
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.active_sha, "base")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_attempt_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planning_workspace_owned, 1)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved push failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertIsNotNone(ports.state.general_intent)
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)
            self.assertFalse(ports.state.validation_cleared)
            ports.state.publish_general_batch.assert_called_once()

    def test_remote_publication_recovery_cleanup_failure_keeps_saved_result(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result="saved result",
                planning_result_completed=True,
            ))
            ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
            ports.git.remote = "concurrent-base"
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertIsNotNone(ports.state.general_intent)
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)

    def test_pending_maintenance_publication_cleanup_failure_restores_state(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            cycle = maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                cycle_base_sha="base",
                publication_stage="prepared",
                publication_base_sha="base",
                revision=4,
            )
            fingerprint = maintenance_planning_fingerprint(cycle, "fingerprint")
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint=fingerprint,
                planning_workspace_owned=1,
                planning_error="saved publication failure",
                planning_error_fingerprint=fingerprint,
                planning_validation_error="saved diagnostics",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            )
            ports = FakePorts(before, cycle)
            ports.git.head = "plan-commit"
            ports.git.advance_remote_on_push = True
            ports.git.remove_error = RuntimeError("cleanup failed")
            def apply_batch(*_args):
                ports.state.clear_active("/repo")
                ports.state.update(
                    "/repo",
                    planning_validation_error=None,
                    planner_session_id=None,
                    planning_result=None,
                    planning_result_completed=False,
                )
            ports.state.publish_maintenance_batch = mock.Mock(side_effect=apply_batch)
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.current_fingerprint = fingerprint
            run.planning_worktree = worktree
            run.maintenance_context = object()
            run.validated_result = ports.validation.result

            outcome = coordinator._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.active_sha, "base")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_attempt_fingerprint,
                             fingerprint)
            self.assertEqual(ports.state.snapshot.planning_workspace_owned, 1)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved publication failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             fingerprint)
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertTrue(worktree.exists())
            ports.state.publish_maintenance_batch.assert_called_once()

    def test_no_change_cleanup_failure_keeps_saved_planning_result(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved validation failure",
                planning_error_fingerprint="fingerprint",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            ))
            ports.git.remove_error = RuntimeError("cleanup failed")
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.planning_worktree = worktree
            run.current_fingerprint = "fingerprint"

            outcome = coordinator._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved validation failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertTrue(worktree.exists())

    def test_prerequisite_cleanup_failure_preserves_planning_and_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            cycle = maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                cycle_base_sha="base",
                revision=4,
            )
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved validation failure",
                planning_error_fingerprint="fingerprint",
                planner_session_id="saved-session",
                planning_result=(
                    'MAINTENANCE_RESULT: '
                    '{"outcome":"prerequisites","plan_ids":["G-001"]}'
                ),
                planning_result_completed=True,
            ), cycle)
            ports.git.remove_error = RuntimeError("cleanup failed")
            ports.state.transition_maintenance = mock.Mock(
                side_effect=lambda *_args, **_kwargs: setattr(
                    ports.state, "cycle", replace(
                        ports.state.cycle,
                        phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                    )
                )
            )
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.planning_worktree = worktree
            run.current_fingerprint = "fingerprint"
            run.maintenance_context = object()

            with mock.patch(
                "planning_runtime.validate_prerequisite_plan_ids",
                return_value=("G-001",),
            ):
                outcome = coordinator._validate_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved validation failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertEqual(ports.state.cycle, cycle)
            self.assertTrue(worktree.exists())
            ports.state.transition_maintenance.assert_called_once()

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

    def test_source_change_with_ready_runs_planner_once_before_activation(self):
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
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")

        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.agent.run_count, 1)

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
        ports.git.remote = "new-base"
        ports.git.specification_fingerprints["new-base"] = "new-specification"
        ports.git.fingerprints["new-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertIsNone(ports.state.snapshot.phase)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint, "specification"
        )

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 2)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "new-specification",
        )

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

    def test_failed_planner_does_not_activate_stale_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.agent.result = PlanningAgentResult(
            1, "planner failed", None,
            PlanningAgentResultKind.PERMANENT_CONFIGURATION,
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertNotEqual(ports.state.snapshot.phase, "WORKING")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_unpublished_planner_does_not_activate_stale_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.general_intent_error_call = 1
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertNotEqual(ports.state.snapshot.phase, "WORKING")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")


class PlanningDecisionTest(unittest.TestCase):
    def test_active_work_keeps_precedence_over_changed_full_input(self):
        decision = choose_planning_decision(
            has_active=True,
            specification_changed=True,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(decision.kind, PlanningDecisionKind.RESUME_ACTIVE)

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

    def test_changed_full_input_precedes_runnable_ready(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=False,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(decision.kind, PlanningDecisionKind.RUN_FALLBACK_PLANNING)

    def test_changed_full_input_plans_without_runnable_ready(self):
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
