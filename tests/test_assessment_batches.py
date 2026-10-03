import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.maintenance import maintenance_file_review as files
from loop_manager_internal.maintenance import maintenance_reconciliation as relations
from loop_manager_internal.maintenance.maintenance_file_inventory import TrackedSource, SourceInventoryError
from loop_manager_internal.maintenance.review_ports import _PlanningFileReviewPorts
from loop_manager_internal.planning import assessment_batches as batches, planning_runtime
from loop_manager_internal.planning.planning_contracts import (
    PlanningAgentResult, PlanningAgentResultKind, PlanningOutcome, PlanningOutcomeKind,
    PlanningScopeResult, PlanningStepResult,
)
from tests.support.maintenance import result_for


REVISION = "a" * 40


class AssessmentBatchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        self.connection.execute("INSERT INTO projects VALUES ('repo')")
        self.contents = {f"src/{name}.py": f"def calculate_{name}(): return 1\n".encode()
                         for name in ("a", "b", "c")}
        self.sources = tuple(TrackedSource(path, hashlib.sha256(content).hexdigest(),
                                           "100644", REVISION, "source")
                             for path, content in self.contents.items())
        self.port = _PlanningFileReviewPorts(self.connection)
        self.progress = files.FileReviewProgress(self.sources, (), files.ReviewUnit(self.sources[0], None, None))
        self.port.progress = mock.Mock(return_value=self.progress)
        self.port.content = mock.Mock(side_effect=lambda repo, base, source: self.contents[source.path])
        self.agent = mock.Mock()
        self.agent.finish_scope.return_value = PlanningScopeResult()
        self.ports = SimpleNamespace(file_review=self.port, agent=self.agent, events=mock.Mock())
        self.runner = batches.MaintenanceAssessmentBatches(self.ports)
        self.run = SimpleNamespace(
            context=SimpleNamespace(repo="repo", repo_path=Path("/repo"), base_branch="main",
                                    state_dir=Path(self.directory.name)),
            maintenance_cycle=SimpleNamespace(revision=1), row=SimpleNamespace(active_sha=REVISION))
        self.worktree = Path("/worktree")

    def answer(self, output, kind=PlanningAgentResultKind.SUCCESS):
        def invoke(worktree, prompt, session, callback):
            callback("batch-session")
            return PlanningAgentResult(0 if kind is PlanningAgentResultKind.SUCCESS else 1,
                                       output, "batch-session", kind)
        self.agent.run_planner.side_effect = invoke

    def file_output(self, sources=None):
        return "\n".join(files.RESULT_PREFIX + json.dumps(result_for(
            source, self.contents[source.path].decode().strip(), self.contents[source.path]))
            for source in (sources or self.sources))

    def saved(self):
        return [json.loads(row[0]) for row in self.connection.execute(
            "SELECT result_json FROM provisional_file_reviews WHERE result_json IS NOT NULL")]

    def test_multiple_files_use_one_call_and_individual_durable_receipts(self):
        self.answer(self.file_output())
        outcome = self.runner.review_files(self.run, self.worktree)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertEqual({item["path"] for item in self.saved()}, set(self.contents))
        self.assertEqual(self.agent.run_planner.call_count, 1)
        prompt = self.agent.run_planner.call_args.args[1]
        for source in self.sources:
            self.assertIn(source.content_hash, prompt)
        repository = files.ProvisionalReviewRepository(self.connection)
        resumed = files.FileReviewProgress(self.sources, tuple(self.saved()), None)
        self.assertEqual(repository.pending_units("repo", resumed), ())

    def test_multiline_legacy_receipts_remain_valid_in_a_batch(self):
        self.answer("\n".join(files.RESULT_PREFIX + "\n```json\n" + json.dumps(result_for(
            source, self.contents[source.path].decode().strip(), self.contents[source.path]), indent=2)
            + "\n```" for source in self.sources))
        self.assertEqual(self.runner.review_files(self.run, self.worktree).kind,
                         PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertEqual(len(self.saved()), 3)

    def test_missing_receipt_saves_valid_files_and_resumes_only_unfinished_unit(self):
        self.answer(self.file_output(self.sources[:2]))
        outcome = self.runner.review_files(self.run, self.worktree)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(len(self.saved()), 2)
        resumed = files.FileReviewProgress(self.sources, tuple(self.saved()),
                                           files.ReviewUnit(self.sources[2], "batch-session", "missing"))
        pending = files.ProvisionalReviewRepository(self.connection).pending_units("repo", resumed)
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0].source.path, pending[0].session_id), (self.sources[2].path, "batch-session"))
        self.assertIn("누락", pending[0].error)

    def test_invalid_source_evidence_does_not_discard_valid_receipts(self):
        lines = self.file_output().splitlines()
        value = json.loads(lines[1][len(files.RESULT_PREFIX):])
        value["evidence"]["chunk_reviews"][0]["chunk_hash"] = "b" * 64
        lines[1] = files.RESULT_PREFIX + json.dumps(value)
        self.answer("\n".join(lines))
        outcome = self.runner.review_files(self.run, self.worktree)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual({item["path"] for item in self.saved()}, {self.sources[0].path, self.sources[2].path})

    def test_duplicate_or_foreign_receipt_cannot_advance_coverage(self):
        for output in (self.file_output() + "\n" + self.file_output().splitlines()[0],
                       files.RESULT_PREFIX + json.dumps({"path": "outside.py"})):
            with self.subTest(output=output[:40]):
                self.answer(output)
                outcome = self.runner.review_files(self.run, self.worktree)
                self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
                self.assertEqual(self.saved(), [])

    def test_scope_violation_and_usage_limit_stop_without_success_receipts(self):
        self.answer(self.file_output())
        self.agent.finish_scope.return_value = PlanningScopeResult(error="unsafe scope")
        self.assertEqual(self.runner.review_files(self.run, self.worktree).kind,
                         PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(self.saved(), [])
        self.agent.finish_scope.return_value = PlanningScopeResult()
        self.answer("usage limit", PlanningAgentResultKind.USAGE_LIMIT)
        self.assertEqual(self.runner.review_files(self.run, self.worktree).kind,
                         PlanningOutcomeKind.USAGE_LIMIT_PAUSE)
        self.assertEqual(self.saved(), [])

    def test_remote_change_after_call_rejects_unverified_receipts(self):
        def invoke(*args):
            args[3]("batch-session")
            self.port.content.side_effect = SourceInventoryError("remote advanced")
            return PlanningAgentResult(0, self.file_output(), "batch-session", PlanningAgentResultKind.SUCCESS)
        self.agent.run_planner.side_effect = invoke
        self.assertEqual(self.runner.review_files(self.run, self.worktree).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(self.saved(), [])

    def test_input_budget_bounds_batch_without_losing_pending_files(self):
        self.answer(self.file_output(self.sources[:1]))
        with mock.patch.object(batches, "MAX_BATCH_BYTES", 1):
            outcome = self.runner.review_files(self.run, self.worktree)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertEqual(len(self.saved()), 1)
        self.assertNotIn(self.sources[1].content_hash, self.agent.run_planner.call_args.args[1])

    def test_relation_batch_checks_each_original_and_saves_each_receipt(self):
        repository = relations.ReconciliationRepository(self.connection)
        units = tuple(relations.Relation(f"key-{index}", self.sources[0], source, ())
                      for index, source in enumerate(self.sources[1:]))
        port = SimpleNamespace(unit=lambda *args: (None, None), content=self.port.content,
                               start=repository.start, session=repository.session,
                               complete=repository.complete, fail=repository.fail)
        self.ports.reconciliation = port
        output = "\n".join(relations.RELATION_PREFIX + json.dumps({
            "key": unit.key, "status": "confirmed",
            "left_excerpt": self.contents[unit.left.path].decode().strip(),
            "right_excerpt": self.contents[unit.right.path].decode().strip(),
            "reason": "Both current originals expose a related calculation boundary.",
        }) for unit in units)
        self.answer(output)
        progress = relations.ReconciliationProgress(self.sources, (), units, units[0])
        self.assertEqual(self.runner.review_relations(self.run, self.worktree, progress).kind,
                         PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertEqual(self.agent.run_planner.call_count, 1)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM provisional_relations WHERE result_json IS NOT NULL").fetchone()[0], 2)

    def synthesis_fixture(self):
        repository = relations.SynthesisRepository(self.connection)
        progress = relations.ReconciliationProgress((), (), (), None,
            file_reviews=tuple({"path": f"{index}.py", "evidence": "x" * 9000} for index in range(3)))
        page = repository.prepare("repo", 1, REVISION, progress)
        texts = relations.synthesis_pages(progress)
        candidates = tuple(relations.SynthesisPage(page.digest, index, page.total, text, None)
                           for index, text in enumerate(texts))
        self.ports.reconciliation = SimpleNamespace(
            synthesis_batch=lambda *args: candidates, synthesis_export=repository.export,
            synthesis_session=repository.session, synthesis_complete=repository.complete,
            progress=lambda *args: progress,
            synthesis=lambda *args: repository.prepare("repo", 1, REVISION, progress))
        return repository, progress, page, candidates

    def test_synthesis_batch_keeps_individual_cursor_validation(self):
        repository, progress, page, candidates = self.synthesis_fixture()
        self.answer("\n".join(relations.SYNTHESIS_PREFIX + json.dumps({
            "key": item.key, "excerpt": item.text.partition("\n")[2][:30], "has_issue": False,
            "assessment": "The original evidence has no actionable structural issue.",
        }) for item in candidates))
        self.assertEqual(self.runner.synthesize(self.run, self.worktree, progress, page).kind,
                         PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertEqual(self.agent.run_planner.call_count, 1)
        resumed = relations.SynthesisRepository(self.connection).prepare("repo", 1, REVISION, progress)
        self.assertEqual(resumed.index, resumed.total)

    def test_failed_synthesis_page_preserves_completed_prefix_after_restart(self):
        repository, progress, page, candidates = self.synthesis_fixture()
        self.answer("\n".join(relations.SYNTHESIS_PREFIX + json.dumps({
            "key": item.key, "excerpt": "not in evidence" if index == 1 else item.text.partition("\n")[2][:30],
            "has_issue": False, "assessment": "The original evidence has no actionable structural issue.",
        }) for index, item in enumerate(candidates)))
        self.assertEqual(self.runner.synthesize(self.run, self.worktree, progress, page).kind,
                         PlanningOutcomeKind.VALIDATION_RETRY)
        resumed = relations.SynthesisRepository(self.connection).prepare("repo", 1, REVISION, progress)
        self.assertEqual((resumed.index, resumed.session_id), (1, "batch-session"))

    def test_final_resolution_batch_preserves_all_page_coverage_checks(self):
        repository, progress, page, candidates = self.synthesis_fixture()
        for item in candidates:
            current = repository.prepare("repo", 1, REVISION, progress)
            repository.session("repo", current, "synthesis-session")
            repository.complete("repo", current, relations.SYNTHESIS_PREFIX + json.dumps({
                "key": item.key, "excerpt": item.text.partition("\n")[2][:30], "has_issue": False,
                "assessment": "The evidence contains no actionable structural issue.",
            }))
        complete = repository.prepare("repo", 1, REVISION, progress)
        findings = {"findings": [], "no_issue_reason": "No structural issue remains."}
        manifest = repository.export("repo", complete, progress, Path(self.directory.name))
        first = repository.next_resolution("repo", complete, findings, manifest)
        units = repository.resolution_batch("repo", first, batches.MAX_BATCH_UNITS)
        self.ports.reconciliation.resolution_batch = repository.resolution_batch
        self.ports.reconciliation.resolution_session = repository.resolution_session
        self.ports.reconciliation.resolution_complete = repository.resolution_complete
        self.ports.reconciliation.resolution_fail = repository.resolution_fail
        self.answer("\n".join(relations.RESOLUTION_PREFIX + json.dumps({
            "key": unit.assessment["key"], "excerpt": unit.assessment["assessment"],
            "issue_keys": [], "disposition": "no_issue", "reason": "No actionable candidate remains on this page.",
        }) for unit in units))
        self.assertEqual(self.runner.resolve(self.run, self.worktree, first).kind,
                         PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertEqual(self.agent.run_planner.call_count, 1)
        self.assertIsNone(repository.next_resolution("repo", complete, findings, manifest))
        repository.validate_resolutions("repo", complete, findings)

    def test_partial_final_resolution_keeps_valid_receipts_without_marking_success(self):
        repository, progress, page, candidates = self.synthesis_fixture()
        for item in candidates:
            current = repository.prepare("repo", 1, REVISION, progress)
            repository.session("repo", current, "synthesis-session")
            repository.complete("repo", current, relations.SYNTHESIS_PREFIX + json.dumps({
                "key": item.key, "excerpt": item.text.partition("\n")[2][:30], "has_issue": True,
                "assessment": "This evidence has a candidate that must be explicitly resolved.",
            }))
        complete = repository.prepare("repo", 1, REVISION, progress)
        findings = {"findings": [], "no_issue_reason": "No accepted structural issue remains."}
        manifest = repository.export("repo", complete, progress, Path(self.directory.name))
        first = repository.next_resolution("repo", complete, findings, manifest)
        units = repository.resolution_batch("repo", first, batches.MAX_BATCH_UNITS)
        self.ports.reconciliation.resolution_batch = repository.resolution_batch
        self.ports.reconciliation.resolution_session = repository.resolution_session
        self.ports.reconciliation.resolution_complete = repository.resolution_complete
        self.ports.reconciliation.resolution_fail = repository.resolution_fail
        self.answer("\n".join(relations.RESOLUTION_PREFIX + json.dumps({
            "key": unit.assessment["key"], "excerpt": unit.assessment["assessment"], "issue_keys": [],
            "disposition": "no_issue" if unit.index == 1 else "excluded",
            "reason": "The candidate duplicates an existing boundary and needs no new plan.",
        }) for unit in units))
        self.assertEqual(self.runner.resolve(self.run, self.worktree, first).kind,
                         PlanningOutcomeKind.VALIDATION_RETRY)
        with self.assertRaises(relations.ReconciliationError):
            repository.validate_resolutions("repo", complete, findings)
        resumed = relations.SynthesisRepository(self.connection).next_resolution("repo", complete, findings, manifest)
        self.assertEqual((resumed.index, resumed.session_id), (1, "batch-session"))
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM provisional_synthesis_resolutions WHERE result_json IS NOT NULL").fetchone()[0], 2)


class AssessmentContinuationTest(unittest.TestCase):
    def coordinator(self, outcomes):
        ports = mock.Mock()
        coordinator = planning_runtime.PlanningLifecycleCoordinator(ports)
        ports.git.fetch_base.return_value = REVISION
        coordinator._new_run = mock.Mock()
        gate, assessment = mock.Mock(), mock.Mock()
        gate.step.side_effect = lambda phase, snapshot: PlanningStepResult(snapshot, None)
        assessment.step.side_effect = lambda phase, snapshot: PlanningStepResult(snapshot, next(outcomes))
        coordinator._handlers = ((gate, "inputs"), (assessment, "assessment"))
        return coordinator, gate, assessment

    def test_successful_batches_recheck_inputs_before_continuing_to_final_outcome(self):
        coordinator, gate, assessment = self.coordinator(iter([
            PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS),
            PlanningOutcome(PlanningOutcomeKind.NO_CHANGE)]))
        with mock.patch.object(planning_runtime, "snapshot_run", side_effect=lambda run: run):
            outcome = coordinator.run(SimpleNamespace(base_sha=REVISION, repo_path=Path("/repo"), base_branch="main"))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(gate.step.call_count, 2)
        self.assertEqual(coordinator._new_run.call_count, 2)

    def test_remote_advance_stops_before_next_batch_and_preserves_restart_revision(self):
        coordinator, gate, assessment = self.coordinator(iter([
            PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS)]))
        coordinator._ports.git.fetch_base.return_value = "b" * 40
        with mock.patch.object(planning_runtime, "snapshot_run", side_effect=lambda run: run):
            outcome = coordinator.run(SimpleNamespace(base_sha=REVISION, repo_path=Path("/repo"), base_branch="main"))
        self.assertEqual((outcome.kind, outcome.base_sha),
                         (PlanningOutcomeKind.REMOTE_BASE_RESTART, "b" * 40))
        self.assertEqual(assessment.step.call_count, 1)

    def test_budget_yields_durably_and_errors_do_not_trigger_another_batch(self):
        coordinator, gate, assessment = self.coordinator(iter([
            PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS)]))
        with mock.patch.object(planning_runtime, "snapshot_run", side_effect=lambda run: run), \
             mock.patch.object(planning_runtime, "MAX_ASSESSMENT_BATCHES", 1):
            self.assertEqual(coordinator.run(mock.Mock()).kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(assessment.step.call_count, 1)
        for kind in (PlanningOutcomeKind.VALIDATION_RETRY, PlanningOutcomeKind.REMOTE_BASE_RESTART,
                     PlanningOutcomeKind.USAGE_LIMIT_PAUSE):
            coordinator, gate, assessment = self.coordinator(iter([PlanningOutcome(kind)]))
            with mock.patch.object(planning_runtime, "snapshot_run", side_effect=lambda run: run):
                self.assertEqual(coordinator.run(mock.Mock()).kind, kind)
            self.assertEqual(assessment.step.call_count, 1)


if __name__ == "__main__":
    unittest.main()
