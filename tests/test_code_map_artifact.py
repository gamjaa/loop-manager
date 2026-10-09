"""Source-bound context snapshots and publication provenance, without a Git CLI."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from tests.support.workspace import symlink_or_skip

from loop_manager_internal.maintenance import code_map_artifact as artifact
from loop_manager_internal.maintenance import maintenance_reconciliation as reconciliation
from loop_manager_internal.maintenance import maintenance_validation as validation
from loop_manager_internal.maintenance import maintenance_publication as publication
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, SourceInventoryError, TrackedSource
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanMode, MaintenancePlanningContext
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.planning.planning_runtime import PlanningLifecycleCoordinator, PlanningOutcomeKind
from loop_manager_internal.storage import maintenance_state
from tests.support.planning_case import PlanningCase, FakePorts, planning_snapshot, maintenance_cycle
from loop_manager_internal.catalog import catalog_service
from loop_manager_internal.agents.scope_contracts import AgentChange, AgentScopeError, GitSnapshot
from loop_manager_internal.agents.scope_validation import validate_agent_scope
from loop_manager_internal.storage import maintenance_file_ledger
from tests.support.maintenance import result_for


class CodeMapArtifactTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        self.connection.execute("INSERT INTO projects VALUES ('repo')")
        self.contents = {"src/a.py": b"def calculate(): return 1\n", "src/b.py": b"def calculate(): return 2\n"}
        self.sources = tuple(TrackedSource(path, hashlib.sha256(content).hexdigest(), "100644", "a" * 40, "source")
                             for path, content in self.contents.items())
        self.inventory = SourceInventory("a" * 40, self.sources, ())
        self.maps = {source.path: result_for(source, self.contents[source.path].decode().strip(),
                                            self.contents[source.path])["code_map"] for source in self.sources}
        self.context = MaintenancePlanningContext("repo", MaintenancePlanMode.BARRIER, "a" * 40, 4, "assessment", ())
        self.evidence = SimpleNamespace(inventory=self.inventory, tiered=None, code_maps=tuple(
            (path, json.dumps(value)) for path, value in self.maps.items()))

    def git_reader(self, data, mode=b"100644"):
        def read(repo, *args):
            self.assertEqual(repo, self.root)
            if args[0] == "ls-tree":
                self.assertEqual(args[2], self.inventory.revision)
                return mode + b" blob " + b"b" * 40 + b"\t" + artifact.ARTIFACT_PATH.encode() + b"\0"
            if args[:2] == ("cat-file", "-s"):
                return str(len(data)).encode()
            if args[:2] == ("cat-file", "blob"):
                return data
            if args[0] == "show":
                revision, path = args[1].split(":", 1)
                self.assertEqual(revision, self.inventory.revision)
                return self.contents[path]
            raise AssertionError(args)
        return read

    def jsonl(self, records):
        return b"".join(json.dumps(record).encode() + b"\n" for record in records)

    def read(self, data, **kwargs):
        with mock.patch.object(artifact, "_git", side_effect=self.git_reader(data)):
            return artifact.read_maps(self.root, self.inventory, **kwargs)

    def test_deterministic_snapshot_contains_no_execution_state(self):
        raw = artifact.serialize(self.inventory, dict(reversed(tuple(self.maps.items()))))
        self.assertEqual(raw, artifact.serialize(self.inventory, self.maps))
        lines = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(lines[0], {"version": 1})
        for item in lines[1:]:
            self.assertEqual(set(item), {"path", "content_hash", "mode", "code_map"})
        self.assertEqual(self.read(raw), self.maps)

    def test_stale_content_modes_and_deleted_paths_are_not_reused(self):
        payload = [json.loads(line) for line in artifact.serialize(self.inventory, self.maps).splitlines()]
        for key, value in (("content_hash", "b" * 64), ("mode", "100755"), ("path", "src/deleted.py")):
            candidate = json.loads(json.dumps(payload))
            candidate[1][key] = value
            with self.subTest(key=key):
                self.assertEqual(set(self.read(self.jsonl(candidate))), {"src/b.py"})

    def test_fabricated_symbols_references_and_extra_fields_are_not_trusted(self):
        payload = [json.loads(line) for line in artifact.serialize(self.inventory, self.maps).splitlines()]
        for changes in ({"public_symbols": ["missing"]}, {"responsibility": "short"},
                        {"direct_references": [{"target_path": "../../escape", "symbol": "calculate", "evidence": "calculate"}]},
                        {"direct_references": [{"target_path": "src/b.py", "symbol": "missing", "evidence": "missing()"}]},
                        {"session": "secret-state"}):
            candidate = json.loads(json.dumps(payload))
            candidate[1]["code_map"].update(changes)
            with self.subTest(changes=changes):
                self.assertNotIn("src/a.py", self.read(self.jsonl(candidate)))

    def test_duplicate_version_non_finite_and_symlink_blobs_are_ignored(self):
        raw = artifact.serialize(self.inventory, self.maps)
        value = [json.loads(line) for line in raw.splitlines()]
        invalid = [b"{", b'{"version":1,"version":1}\n', b'{"version":NaN}\n',
                   self.jsonl([{ "version": True}, *value[1:]]),
                   self.jsonl([*value, value[1]]), raw + b"{\n"]
        for data in invalid:
            with self.subTest(data=data[:35]):
                self.assertEqual(self.read(data), {})
        with mock.patch.object(artifact, "_git", side_effect=self.git_reader(raw, b"120000")):
            self.assertEqual(artifact.read_maps(self.root, self.inventory), {})

    def test_known_sqlite_context_is_not_replaced_or_revalidated(self):
        with mock.patch.object(artifact, "_git", side_effect=self.git_reader(
                artifact.serialize(self.inventory, self.maps))) as git:
            self.assertEqual(artifact.read_maps(self.root, self.inventory, self.maps), {})
        git.assert_not_called()

    def test_failed_git_read_is_not_treated_as_malformed_context(self):
        raw = artifact.serialize(self.inventory, self.maps)
        reader = self.git_reader(raw)

        def read(repo, *args):
            if args[0] == "show":
                raise SourceInventoryError("unreadable source")
            return reader(repo, *args)

        with mock.patch.object(artifact, "_git", side_effect=read):
            with self.assertRaises(SourceInventoryError):
                artifact.read_maps(self.root, self.inventory)

    def test_prepare_merges_retained_maps_and_records_receipt_before_write(self):
        evidence = SimpleNamespace(inventory=self.inventory,
                                   tiered=None,
                                   code_maps=(("src/b.py", json.dumps(self.maps["src/b.py"])),))
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value={"src/a.py": self.maps["src/a.py"]}), \
                mock.patch.object(artifact, "_git", return_value=b""):
            prepared = artifact.prepare(self.connection, "repo", self.context, self.root, evidence, None)
        target = self.root / artifact.ARTIFACT_PATH
        self.assertEqual(target.read_bytes(), artifact.serialize(self.inventory, self.maps))
        self.assertEqual(prepared.plans, ())
        self.assertEqual(prepared.publication_paths, (artifact.ARTIFACT_PATH,))
        self.assertEqual(prepared.maintenance_code_map_hash, hashlib.sha256(target.read_bytes()).hexdigest())
        # A new connection/phase validation can attach an artifact-only batch.
        self.assertEqual(artifact.attach_candidate(self.connection, self.context, self.root, None), prepared)
        target.write_text("tampered")
        with self.assertRaisesRegex(artifact.CodeMapArtifactError, "기록과 다릅니다"):
            artifact.attach_candidate(self.connection, self.context, self.root, None)

    def test_receipt_is_required_and_bound_to_cycle_and_source(self):
        target = self.root / artifact.ARTIFACT_PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(artifact.serialize(self.inventory, self.maps))
        with self.assertRaisesRegex(artifact.CodeMapArtifactError, "기록이 없습니다"):
            artifact.attach_candidate(self.connection, self.context, self.root, None)
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value={}), \
                mock.patch.object(artifact, "_git", return_value=b""):
            artifact.prepare(self.connection, "repo", self.context, self.root, self.evidence, None)
        for context in (replace(self.context, cycle_revision=5), replace(self.context, active_sha="b" * 40)):
            with self.assertRaises(artifact.CodeMapArtifactError):
                artifact.attach_candidate(self.connection, context, self.root, None)

    def test_identical_committed_snapshot_does_not_create_commit_or_receipt(self):
        target = self.root / artifact.ARTIFACT_PATH
        target.parent.mkdir(parents=True)
        raw = artifact.serialize(self.inventory, self.maps)
        target.write_bytes(raw)
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value=self.maps), \
                mock.patch.object(artifact, "_git", side_effect=[b"tracked-entry", raw]):
            self.assertIsNone(artifact.prepare(self.connection, "repo", self.context, self.root, self.evidence, None))

    def test_stale_owned_candidate_is_rebuilt_without_becoming_current_evidence(self):
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value={}), \
                mock.patch.object(artifact, "_git", return_value=b""):
            artifact.prepare(self.connection, "repo", self.context, self.root, self.evidence, None)
        context = replace(self.context, active_sha="c" * 40, cycle_revision=5)
        effects = SimpleNamespace(
            _git_tree_entries=lambda *_: {}, changed_files=lambda *_: [],
            _assert_maintenance_context_current=lambda *_: None,
        )
        self.assertIsNone(validation.validate_maintenance_planner_result(
            effects, self.connection, context, self.root, [artifact.ARTIFACT_PATH]))
        maps = {"src/a.py": self.maps["src/a.py"]}
        evidence = SimpleNamespace(inventory=replace(self.inventory, revision=context.active_sha),
                                   tiered=None,
                                   code_maps=(("src/a.py", json.dumps(maps["src/a.py"])),))
        # The current remote already contains the freshly computed context.
        raw = artifact.serialize(evidence.inventory, maps)
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value={}), \
                mock.patch.object(artifact, "_git", side_effect=[b"tracked-entry", raw]):
            self.assertIsNone(artifact.prepare(self.connection, "repo", context, self.root, evidence, None))
        self.assertEqual((self.root / artifact.ARTIFACT_PATH).read_bytes(), raw)
        # Stale output is never accepted for phases that cannot rebuild maps.
        with self.assertRaises(artifact.CodeMapArtifactError):
            artifact.attach_candidate(self.connection, replace(context, cycle_revision=6, cycle_phase="revalidation"),
                                      self.root, None, allow_stale=True)

    def test_conflict_restore_requires_exact_manager_receipt_and_regular_blob(self):
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value={}), \
                mock.patch.object(artifact, "_git", return_value=b""):
            artifact.prepare(self.connection, "repo", self.context, self.root, self.evidence, None)
        raw = (self.root / artifact.ARTIFACT_PATH).read_bytes()
        target = self.root / artifact.ARTIFACT_PATH
        for stash in ("", "b" * 40):
            for valid, mode in ((True, b"100644"), (False, b"100644"), (True, b"120000")):
                with self.subTest(stash=bool(stash), valid=valid, mode=mode):
                    target.write_bytes(b"conflict markers")
                    content = raw if valid else b"unknown Agent bytes"
                    metadata = mode + b" " + b"c" * 40 + b" 3" if not stash else mode + b" blob " + b"c" * 40
                    entry = metadata + b"\t" + artifact.ARTIFACT_PATH.encode() + b"\0"
                    with mock.patch.object(artifact, "_git", side_effect=[entry, str(len(content)).encode(), content]):
                        if valid and mode == b"100644":
                            artifact.restore_conflict(self.connection, "repo", self.root, stash)
                            self.assertEqual(target.read_bytes(), raw)
                        else:
                            with self.assertRaises(artifact.CodeMapArtifactError):
                                artifact.restore_conflict(self.connection, "repo", self.root, stash)
                            self.assertEqual(target.read_bytes(), b"conflict markers")

    def test_symlink_parent_or_target_is_never_followed(self):
        (self.root / "outside").mkdir()
        symlink_or_skip(self.root / "docs", self.root / "outside", target_is_directory=True)
        with self.assertRaises(artifact.CodeMapArtifactError):
            artifact.prepare(self.connection, "repo", self.context, self.root, self.evidence, None)
        self.assertEqual(list((self.root / "outside").iterdir()), [])

    def test_git_context_cache_does_not_mark_files_reviewed_or_commit_outer_transaction(self):
        repository = reconciliation.ReconciliationRepository(self.connection)
        self.connection.execute("CREATE TABLE atomic_probe (value INTEGER)")
        self.connection.commit()
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO atomic_probe VALUES (1)")
        with mock.patch.object(artifact, "read_maps", return_value=self.maps):
            self.assertEqual(repository.valid_maps("repo", self.inventory), self.maps)
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT count(*) FROM atomic_probe").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM successful_code_maps").fetchone()[0], 0)

    def test_output_path_is_not_a_planning_input_and_only_exact_path_is_excluded(self):
        effects = catalog_service.IsPlanningInputEffects(lambda _: False)
        self.assertFalse(catalog_service._is_planning_input(effects, artifact.ARTIFACT_PATH.encode()))
        lines = (b"100644 blob " + b"a" * 40 + b"\t" + artifact.ARTIFACT_PATH.encode(),
                 b"100644 blob " + b"b" * 40 + b"\tdocs/other.json")
        with mock.patch.object(maintenance_file_ledger, "_git", return_value=b"\n".join(lines)):
            self.assertEqual(maintenance_file_ledger.assessment_input_tree(self.root, "revision"), (lines[1],))

    def test_planner_scope_still_rejects_agent_code_map_writes(self):
        snapshot = GitSnapshot("base", None, {}, {}, {}, {}, {}, {})
        effects = SimpleNamespace(
            _scope_snapshot_with_start_ignore_rules=lambda _tree, _before, after: after,
            agent_snapshot_changes=lambda *_: [AgentChange(artifact.ARTIFACT_PATH, ("untracked",))],
        )
        for role in ("planner", "maintenance_planner"):
            with self.subTest(role=role), self.assertRaises(AgentScopeError):
                validate_agent_scope(role, self.root, snapshot, snapshot, effects=effects)

    def test_maintenance_validation_recovers_only_an_exact_manager_candidate(self):
        effects = SimpleNamespace(
            _git_tree_entries=lambda *_: {}, changed_files=lambda *_: [],
            _assert_maintenance_context_current=lambda *_: None,
        )
        with self.assertRaises(PlannerValidationError):
            validation.validate_maintenance_planner_result(effects, self.connection, self.context,
                                                         self.root, [artifact.ARTIFACT_PATH])
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value={}), \
                mock.patch.object(artifact, "_git", return_value=b""):
            prepared = artifact.prepare(self.connection, "repo", self.context, self.root, self.evidence, None)
        result = validation.validate_maintenance_planner_result(
            effects, self.connection, self.context, self.root, [artifact.ARTIFACT_PATH])
        self.assertEqual(result, prepared)
        (self.root / artifact.ARTIFACT_PATH).write_bytes(b"changed by Agent")
        with self.assertRaises(PlannerValidationError):
            validation.validate_maintenance_planner_result(effects, self.connection, self.context,
                                                         self.root, [artifact.ARTIFACT_PATH])

    def test_publication_commit_rechecks_actual_bytes_and_requires_manager_hash(self):
        with mock.patch.object(reconciliation.ReconciliationRepository, "valid_maps", return_value={}), \
                mock.patch.object(artifact, "_git", return_value=b""):
            result = artifact.prepare(self.connection, "repo", self.context, self.root, self.evidence, None)
        commit = "c" * 40

        def git(repo, *args):
            values = {("rev-parse", "HEAD"): commit,
                      ("rev-list", "--parents", "-n", "1", commit): f"{commit} {self.context.active_sha}",
                      ("diff", "--name-only", self.context.active_sha, commit): artifact.ARTIFACT_PATH}
            return SimpleNamespace(stdout=values[args])

        contents = (self.root / artifact.ARTIFACT_PATH).read_bytes()
        effects = SimpleNamespace(git=git, git_bytes=mock.Mock(return_value=contents),
                                  load_execplan_catalog=mock.Mock(), _parse_execplan=mock.Mock())
        publication._validate_maintenance_planning_commit(effects, self.root, result, commit)
        effects._parse_execplan.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "Manager"):
            publication._validate_maintenance_planning_commit(
                effects, self.root, replace(result, maintenance_code_map_hash=None), commit)
        effects.git_bytes.return_value = b"changed commit bytes"
        with self.assertRaisesRegex(RuntimeError, "변경됐습니다"):
            publication._validate_maintenance_planning_commit(effects, self.root, result, commit)


class CodeMapPublicationTest(PlanningCase):
    def prepared_run(self):
        cycle = maintenance_cycle(phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                                  cycle_base_sha="base", revision=4, file_assessment_start_merged_count=0)
        ports = FakePorts(planning_snapshot(phase="PLANNING", active_sha="base",
                                           worktree="/planning", planning_attempt_fingerprint="attempt"), cycle)
        context = MaintenancePlanningContext(self.context.repo, MaintenancePlanMode.BARRIER, "base", 4, "assessment", ())
        prepared = ValidatedPlanningResult((), (artifact.ARTIFACT_PATH,),
            publication_hashes=((artifact.ARTIFACT_PATH, "e" * 64),),
            maintenance_context=context, maintenance_code_map_hash="e" * 64)
        ports.state.prepare_file_assessment_artifact = mock.Mock(return_value=prepared)
        ports.state.record_maintenance_intent = mock.Mock(return_value=replace(cycle, publication_stage="prepared"))
        ports.state.publish_maintenance_batch = mock.Mock(return_value=cycle)
        ports.git.advance_remote_on_push = True
        ports.artifact_dirty = True
        original_git = ports.git.run

        def git(repo, *args, **kwargs):
            if args == ("status", "--porcelain", "--", artifact.ARTIFACT_PATH):
                ports.git.calls.append((str(repo), args, True))
                return SimpleNamespace(stdout="?? " + artifact.ARTIFACT_PATH if ports.artifact_dirty else "", returncode=0)
            return original_git(repo, *args, **kwargs)

        ports.git.run = git
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.maintenance_context = context
        run.current_fingerprint = "attempt"
        run.validated_result = None
        return ports, run, coordinator._publication

    def test_no_change_assessment_commits_snapshot_once_and_confirms_before_success(self):
        ports, run, service = self.prepared_run()
        self.assertIsNone(service._publish_planner_result(run))
        commands = [args for _repo, args, _check in ports.git.calls]
        self.assertEqual([args for args in commands if args[0] == "commit"],
                         [("commit", "-m", "Update periodic maintenance planning batch")])
        self.assertIn(("add", "--", artifact.ARTIFACT_PATH), commands)
        self.assertIn(("push", "origin", "HEAD:refs/heads/main"), commands)
        ports.state.record_maintenance_intent.assert_called_once()
        ports.state.publish_maintenance_batch.assert_called_once()

    def test_push_failure_preserves_candidate_and_does_not_promote(self):
        ports, run, service = self.prepared_run()
        original = ports.git.run

        def git(repo, *args, **kwargs):
            if args[0] == "push":
                return SimpleNamespace(stdout="rejected", returncode=1)
            return original(repo, *args, **kwargs)

        ports.git.run = git
        outcome = service._publish_planner_result(run)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        ports.state.record_maintenance_intent.assert_called_once()
        ports.state.publish_maintenance_batch.assert_not_called()
        self.assertFalse(any(args[:2] == ("worktree", "remove") for _repo, args, _check in ports.git.calls))

    def test_interruption_after_commit_reuses_single_commit_without_recommitting(self):
        ports, run, service = self.prepared_run()
        ports.git.head = "plan-commit"
        ports.artifact_dirty = False
        self.assertIsNone(service._publish_planner_result(run))
        self.assertFalse(any(args[0] == "commit" for _repo, args, _check in ports.git.calls))
        ports.state.record_maintenance_intent.assert_called_once()
        ports.state.publish_maintenance_batch.assert_called_once()

    def test_identical_remote_context_clears_stale_index_without_commit(self):
        ports, run, service = self.prepared_run()
        ports.state.prepare_file_assessment_artifact.return_value = None
        ports.state.file_assessment_evidence = mock.Mock(return_value="completed current evidence")
        ports.state.complete_file_assessment = mock.Mock()
        ports.state.advance_success_baseline = mock.Mock()
        ports.artifact_dirty = False
        run.dirty_files = [artifact.ARTIFACT_PATH]
        self.assertIsNone(service._publish_planner_result(run))
        self.assertEqual(run.dirty_files, [])
        self.assertIn((str(run.planning_worktree), ("add", "--", artifact.ARTIFACT_PATH), True), ports.git.calls)
        self.assertFalse(any(args[0] in {"commit", "push"} for _repo, args, _check in ports.git.calls))
        ports.state.record_maintenance_intent.assert_not_called()
        ports.state.complete_file_assessment.assert_called_once()

    def test_snapshot_failure_or_remote_race_does_not_create_commit(self):
        for error in (artifact.CodeMapArtifactError("snapshot rejected"),):
            ports, run, service = self.prepared_run()
            ports.state.prepare_file_assessment_artifact.side_effect = error
            self.assertEqual(service._publish_planner_result(run).kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertFalse(any(args[0] == "commit" for _repo, args, _check in ports.git.calls))
            ports.state.publish_maintenance_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
