from tests.support.fixed_samples import use_fixed_guidance
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.maintenance import code_index, tiered_review
from loop_manager_internal.maintenance.maintenance_file_inventory import (
    SourceInventory, ClassifiedPath, TrackedSource, tracked_source_inventory,
)
from loop_manager_internal.maintenance.maintenance_file_review import RESULT_PREFIX as FILE_REVIEW_PREFIX
from loop_manager_internal.storage import maintenance_file_ledger
from tests.support.maintenance import inventory, root_results_for
from tests.support.maintenance_planning_case import MaintenancePlanningCase, git


class TieredReviewTest(unittest.TestCase):
    def test_assessment_reads_only_fixed_guidance_and_reuses_receipt(self):
        from loop_manager_internal.agents import common_guidance
        from tests.support.fixed_samples import PROJECT_FILES

        read = common_guidance._read
        def read_sample(path):
            self.assertTrue(path.is_relative_to(PROJECT_FILES), path)
            return read(path)
        with mock.patch.object(common_guidance, '_read', side_effect=read_sample) as reads:
            selected = self.select()
            self.complete(selected)
            self.assertFalse(self.select().required)
            self.assertEqual({call.args[0] for call in reads.call_args_list},
                             {PROJECT_FILES / path for path in common_guidance.GUIDANCE_PATHS})

    def setUp(self):
        use_fixed_guidance(self)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.repository = tiered_review.TieredReviewRepository(self.connection)
        self.sources = {"pkg/a.py": b"def calculate(value):\n    return value + 1\n",
                        "pkg/b.py": b"from pkg import a\ndef read():\n    return a.calculate(2)\n",
                        "tests/test_a.py": b"from pkg import a\ndef test_value():\n    assert a.calculate(1) == 2\n",
                        "tests/test_other.py": b"def test_unrelated():\n    assert True\n"}

    def select(self, merged=0, revision="sha", interval=20, **kwargs):
        inv = inventory(self.sources, revision)
        actual = SourceInventory(revision, tuple(TrackedSource(source.path, source.content_hash, source.mode, revision, "source")
            for source in inv.sources), tuple(ClassifiedPath(path, "source", "source") for path in self.sources))
        with mock.patch.object(tiered_review, "cached_source_inventory", return_value=actual), \
                mock.patch.object(tiered_review, "root_placement_inventory", return_value=()), \
                mock.patch.object(code_index, "git", side_effect=lambda _repo, _command, value: self.sources[value.split(":", 1)[1]]):
            return self.repository.select("repo", Path("/repo"), "main", revision, interval, merged, **kwargs)

    def test_explicit_request_can_select_full_review_with_zero_interval(self):
        self.assertFalse(self.select(interval=0).required)
        self.connection.execute("INSERT INTO tiered_maintenance_requests VALUES ('repo', 'manual')")
        selected = self.select(interval=0, force_full=True)
        self.assertEqual(selected.mode, 'full')
        self.assertEqual(selected.request_id, 'manual')

    def complete(self, selection, cycle=1, findings=None):
        assessment = self.repository.prepare("repo", cycle, selection)
        for unit in self.repository.units(assessment):
            value = {"digest": unit["digest"], "reviews": [{"module": part["module"],
                "summary": "Concrete module responsibility and relation were examined.",
                "citations": [{"path": next(iter(part["records"])),
                    "excerpt": self.sources[next(iter(part["records"]))].decode().splitlines()[0]}]} for part in unit["parts"]],
                "findings": findings or []}
            self.repository.complete("repo", unit, tiered_review.RESULT_PREFIX + json.dumps(value), self.sources.__getitem__)
        evidence = self.repository.evidence("repo", cycle, selection.index.revision)
        self.repository.promote("repo", evidence)
        self.connection.commit()
        return evidence

    def test_normalized_result_binds_identity_preserves_raw_and_rejects_missing_judgments(self):
        assessment = self.repository.prepare("repo", 1, self.select())
        unit = self.repository.pending("repo", assessment)
        value = {"reviews": [{"summary": "Actual responsibility and source relationships were reviewed.",
            "citations": [{"path": next(iter(part["records"])),
                          "excerpt": self.sources[next(iter(part["records"]))].decode().splitlines()[0]}]}
            for part in unit["parts"]], "findings": []}
        output = " **MODULE_REVIEW_RESULT** =\n```json\n" + json.dumps(value, indent=2) + "\n```"
        self.repository.complete("repo", unit, output, self.sources.__getitem__)
        restarted = tiered_review.TieredReviewRepository(self.connection)
        self.assertIsNone(restarted.pending("repo", assessment))
        receipt = restarted.evidence("repo", 1, "sha")["receipts"][0]
        self.assertEqual(receipt["digest"], unit["digest"])
        self.assertEqual([r["module"] for r in receipt["reviews"]], [p["module"] for p in unit["parts"]])
        audit = self.connection.execute("SELECT raw_result, normalized_result, source_revision, protocol "
            "FROM tiered_maintenance_result_audit").fetchone()
        self.assertEqual(audit[0], output)
        self.assertEqual(json.loads(audit[1]), receipt)
        self.assertEqual((audit[2], audit[3]), ("sha", tiered_review.RESULT_PROTOCOL))
        for change in ({"digest": "other"}, {"findings": None}, {"reviews": []}):
            with self.subTest(change=change), self.assertRaises(tiered_review.TieredReviewError):
                self.repository.complete("repo", unit, tiered_review.RESULT_PREFIX + json.dumps(value | change), self.sources.__getitem__)
        for field in ("summary", "citations"):
            missing = json.loads(json.dumps(value))
            del missing["reviews"][0][field]
            with self.subTest(field=field), self.assertRaises(tiered_review.TieredReviewError):
                self.repository.complete("repo", unit, tiered_review.RESULT_PREFIX + json.dumps(missing), self.sources.__getitem__)
        wrong_module = json.loads(json.dumps(value))
        wrong_module["reviews"][0]["module"] = "another/module"
        with self.assertRaises(tiered_review.TieredReviewError):
            self.repository.complete("repo", unit, tiered_review.RESULT_PREFIX + json.dumps(wrong_module), self.sources.__getitem__)

    def test_analysis_upgrade_rebinds_facts_only_for_identical_sources_and_keeps_scope(self):
        selection = self.select()
        assessment = self.repository.prepare("repo", 1, selection)
        legacy = json.loads(json.dumps(assessment))
        legacy.pop("analysis_version")
        legacy["paths"] = ["pkg/a.py"]
        for record in legacy["records"].values():
            record["facts"].pop("whitespace_only")
        self.connection.execute("UPDATE tiered_maintenance_assessments SET assessment_json = ?",
                                (json.dumps(legacy),))
        rebound = self.repository.prepare("repo", 1, selection)
        self.assertEqual(rebound["paths"], ["pkg/a.py"])
        self.assertEqual(rebound["prior_analysis_assessment"], legacy)
        self.assertEqual(rebound["analysis_version"], code_index.VERSION)
        self.assertEqual(self.repository.prepare("repo", 1, selection), rebound)
        forged = json.loads(json.dumps(legacy))
        forged["records"]["pkg/a.py"]["content_hash"] = "wrong"
        self.connection.execute("UPDATE tiered_maintenance_assessments SET assessment_json = ?",
                                (json.dumps(forged),))
        with self.assertRaises(tiered_review.TieredReviewError):
            self.repository.prepare("repo", 1, selection)

    def test_restart_refuses_result_without_matching_raw_audit(self):
        self.complete(self.select())
        self.connection.execute("UPDATE tiered_maintenance_units SET result_json = '{}' ")
        with self.assertRaises(tiered_review.TieredReviewError):
            tiered_review.TieredReviewRepository(self.connection).evidence("repo", 1, "sha")

    def test_resume_rejects_missing_judgment_even_if_raw_and_normalized_are_equal(self):
        self.complete(self.select())
        row = self.connection.execute('SELECT digest,result_json FROM tiered_maintenance_units LIMIT 1').fetchone()
        for field, replacement in (('reviews', []), ('findings', None)):
            invalid = json.loads(row[1])
            invalid[field] = replacement
            encoded = json.dumps(invalid)
            self.connection.execute('UPDATE tiered_maintenance_units SET result_json = ? WHERE digest = ?', (encoded, row[0]))
            self.connection.execute('UPDATE tiered_maintenance_result_audit SET raw_result = ?, normalized_result = ? WHERE digest = ?',
                (tiered_review.RESULT_PREFIX + encoded, encoded, row[0]))
            with self.subTest(field=field), self.assertRaises(tiered_review.TieredReviewError):
                self.repository.evidence('repo', 1, 'sha')

    def test_blank_facts_skip_only_blank_modules_and_keep_uncertain_sources(self):
        self.sources = {"blank/a.py": b"", "blank/b.py": b" \n\t", "pkg/empty.py": b" \n",
            "pkg/meaningful.py": b"def calculate(): return 1\n", "unknown/file.js": b" \n",
            "dynamic/a.py": b"__import__('anything')\n", "broken/a.py": b"def (\n"}
        assessment = self.repository.prepare("repo", 1, self.select())
        units = self.repository.units(assessment)
        parts = [part for unit in units for part in unit["parts"]]
        self.assertNotIn("blank", [part["module"] for part in parts])
        self.assertTrue({"pkg", "unknown", "dynamic", "broken"} <= {part["module"] for part in parts})
        pkg = next(part for part in parts if part["module"] == "pkg")
        self.assertNotIn("pkg/empty.py", pkg["records"])
        self.assertTrue(assessment["records"]["pkg/empty.py"]["facts"]["whitespace_only"])
        self.assertIn("blank/a.py", assessment["records"])

    def test_first_full_then_zero_model_fast_and_separate_global_clock(self):
        first = self.select(7)
        self.assertEqual((first.mode, first.reasons), ("full", ("initial_full",)))
        self.complete(first)
        self.assertFalse(self.select(7).required)
        self.sources["pkg/a.py"] = b"def calculate(value):\n    return value + 2\n"
        fast = self.select(10, "new")
        self.assertEqual(fast.mode, "fast")
        self.assertEqual(self.repository.units(self.repository.prepare("repo", 2, fast)), ())
        self.complete(fast, 2)
        self.assertEqual(self.repository.success("repo")["full_merged_count"], 7)
        self.assertFalse(self.select(26, "new").required)
        periodic = self.select(27, "new")
        self.assertEqual((periodic.mode, periodic.reasons), ("full", ("periodic_full",)))
        self.assertIsNotNone(self.repository.pending("repo", self.repository.prepare("repo", 3, periodic)))

    def test_contract_change_includes_consumers_and_only_related_tests(self):
        self.complete(self.select())
        self.sources["pkg/a.py"] = b"def calculate(value, offset=1):\n    return value + offset\n"
        selection = self.select(1, "new")
        self.assertEqual(selection.mode, "targeted")
        self.assertIn("public_contract_changed", selection.reasons)
        self.assertIn("pkg/b.py", selection.paths)
        self.assertIn("tests/test_a.py", selection.paths)
        self.assertNotIn("tests/test_other.py", selection.paths)
        self.complete(selection, 2)
        self.assertEqual(self.repository.success("repo")["full_merged_count"], 0)

    def test_dynamic_parse_failure_and_new_cycle_escalate(self):
        self.complete(self.select())
        self.sources["pkg/a.py"] = b"from pkg import b\ndef calculate(value):\n    return value + 1\n"
        selection = self.select(1, "cycle")
        self.assertIn("new_dependency_cycle", selection.reasons)
        self.sources["pkg/a.py"] = b"def calculate(value):\n    return __import__(value)\n"
        self.assertIn("uncertain_relations", self.select(1, "dynamic").reasons)
        self.sources["pkg/a.py"] = b"def calculate(\n"
        self.assertEqual(self.select(1, "broken").mode, "targeted")

    def test_state_body_change_and_deletion_escalate(self):
        self.sources["pkg/state.py"] = b"def transition():\n    return 1\n"
        self.complete(self.select())
        self.sources["pkg/state.py"] = b"def transition():\n    return 2\n"
        self.assertIn("state_storage_recovery_or_permission_changed", self.select(1, "change").reasons)
        del self.sources["pkg/state.py"]
        self.assertEqual(self.select(1, "delete").mode, "targeted")

    def test_partial_failure_preserves_session_and_does_not_record_success(self):
        assessment = self.repository.prepare("repo", 1, self.select())
        unit = self.repository.pending("repo", assessment)
        self.repository.session("repo", unit, "saved-session")
        with self.assertRaises(tiered_review.TieredReviewError):
            self.repository.complete("repo", unit, tiered_review.RESULT_PREFIX + json.dumps(
                {"digest": unit["digest"], "reviews": [], "findings": []}), self.sources.__getitem__)
        self.repository.fail("repo", unit, "missing modules")
        self.assertEqual(self.repository.pending("repo", assessment)["session_id"], "saved-session")
        self.assertIsNone(self.repository.success("repo"))
        with self.assertRaises(tiered_review.TieredReviewError):
            self.repository.evidence("repo", 1, "sha")

    def test_forged_citation_and_duplicate_json_keys_rejected(self):
        assessment = self.repository.prepare("repo", 1, self.select())
        unit = self.repository.pending("repo", assessment)
        for output in (tiered_review.RESULT_PREFIX + '{"digest":"a","digest":"b"}',
            tiered_review.RESULT_PREFIX + json.dumps({"digest": unit["digest"], "reviews": [
                {"module": part["module"], "summary": "Specific responsibility was examined.",
                 "citations": [{"path": next(iter(part["records"])), "excerpt": "fabricated source citation"}]}
                for part in unit["parts"]], "findings": []})):
            with self.assertRaises(tiered_review.TieredReviewError):
                self.repository.complete("repo", unit, output, self.sources.__getitem__)

    def test_request_is_consumed_only_after_success_and_matching_request(self):
        self.complete(self.select())
        self.connection.execute("INSERT INTO tiered_maintenance_requests VALUES ('repo', 'request-1')")
        selection = self.select()
        self.assertEqual(selection.reasons, ("requested_full",))
        evidence = self.complete(selection, 2)
        self.assertIsNone(self.connection.execute("SELECT 1 FROM tiered_maintenance_requests").fetchone())
        self.connection.execute("INSERT INTO tiered_maintenance_requests VALUES ('repo', 'request-2')")
        self.repository.promote("repo", evidence)
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM tiered_maintenance_requests").fetchone())

    def test_non_string_citation_path_is_validation_error(self):
        assessment = self.repository.prepare("repo", 1, self.select())
        unit = self.repository.pending("repo", assessment)
        result = {"digest": unit["digest"], "reviews": [{"module": part["module"],
            "summary": "Specific module responsibility was examined.",
            "citations": [{"path": [], "excerpt": "invalid source path"}]} for part in unit["parts"]], "findings": []}
        with self.assertRaises(tiered_review.TieredReviewError):
            self.repository.complete("repo", unit, tiered_review.RESULT_PREFIX + json.dumps(result), self.sources.__getitem__)

    def test_unchanged_module_receipt_reused_after_unrelated_remote_change(self):
        first = self.select()
        self.complete(first)
        # Add a new independent module: unchanged module batches retain their exact identities.
        self.sources["other/c.py"] = b"def separate():\n    return 3\n"
        selection = self.select(1, "advanced")
        self.assertEqual(selection.mode, "targeted")
        assessment = self.repository.prepare("repo", 2, selection)
        self.assertEqual(assessment["paths"], ["other/c.py"])

    def test_common_hash_binds_reusable_module_receipts_and_ongoing_assessment(self):
        from loop_manager_internal.agents import common_guidance
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='a' * 64):
            selection = self.select()
            first = self.complete(selection)
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='b' * 64):
            resumed = self.repository.prepare('repo', 1, selection)
            self.assertEqual(resumed, {key: value for key, value in first.items()
                                       if key not in {'receipts', 'root_plan_links'}})
            self.assertIsNone(first['root_plan_links'])
            self.assertIsNone(self.repository.pending('repo', resumed))
            new = self.repository.prepare('repo', 2, selection)
            self.assertEqual(new['guidance_hash'], 'b' * 64)
            self.assertIsNotNone(self.repository.pending('repo', new))
            previous_ids = {unit['digest'] for unit in self.repository.units(first)}
            self.assertFalse(previous_ids & {unit['digest'] for unit in self.repository.units(new)})
        legacy = dict(new)
        legacy.pop('guidance_hash')
        self.connection.execute('UPDATE tiered_maintenance_assessments SET assessment_json = ? WHERE repo = ?',
                                (json.dumps(legacy), 'repo'))
        with mock.patch.object(common_guidance, 'runtime_content_hash', return_value='c' * 64):
            migrated = self.repository.prepare('repo', 2, selection)
        self.assertEqual(migrated['guidance_hash'], 'c' * 64)
        self.assertEqual(migrated['prior_guidance_assessment'], legacy)
        self.assertIsNotNone(self.repository.pending('repo', migrated))

    def test_root_plan_links_are_idempotent_after_repository_restart(self):
        root = {"path": "legacy", "category": "internal_implementation",
            "destination": "src/legacy", "reason": "Move the internal legacy tree safely."}
        with tempfile.TemporaryDirectory() as directory:
            repository_root = Path(directory)
            plan_path = "docs/plans/[G-003] Move Legacy.md"
            target = repository_root / plan_path
            target.parent.mkdir(parents=True)
            contents = b"the final validated preservation plan\n"
            target.write_bytes(contents)
            link = {"path": "legacy", "destination": "src/legacy",
                "root_review_hash": maintenance_file_ledger.root_review_hash(root),
                "status": "new_plan", "plan_id": "G-003", "plan_path": plan_path,
                "plan_hash": tiered_review.plan_content_hash(contents),
                "reason": "The plan preserves the investigated root tree and its references."}
            evidence = {"root_reviews": [root], "digest": "e" * 64}
            with mock.patch.object(self.repository, "evidence", return_value=evidence):
                first = self.repository.record_root_plan_links(
                    "repo", 2, "sha", [link], plan_repository=repository_root)

            restarted = tiered_review.TieredReviewRepository(self.connection)
            with mock.patch.object(restarted, "evidence", return_value=evidence):
                second = restarted.record_root_plan_links(
                    "repo", 2, "sha", [link], plan_repository=repository_root)

        self.assertEqual(first, second)
        saved = self.connection.execute(
            "SELECT assessment_digest, links_json FROM tiered_maintenance_root_plan_links"
        ).fetchone()
        self.assertEqual(saved[0], evidence["digest"])
        self.assertEqual(json.loads(saved[1]), list(first))

    def test_success_schema_migrates_existing_rows_with_no_guidance_hash(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tiered_maintenance_success ("
            "repo TEXT PRIMARY KEY, records_json TEXT NOT NULL, roots_json TEXT NOT NULL, "
            "source_revision TEXT NOT NULL, full_merged_count INTEGER, full_source_revision TEXT, "
            "assessment_digest TEXT NOT NULL, root_reviews_json TEXT)")
        connection.execute("INSERT INTO tiered_maintenance_success VALUES "
                           "('repo', '{}', '[]', 'sha', 0, 'sha', 'digest', '[]')")

        saved = tiered_review.TieredReviewRepository(connection).success("repo")

        self.assertEqual(saved["root_reviews"], [])
        self.assertIsNone(saved["guidance_hash"])

    def test_prior_root_success_requires_the_same_guidance_hash(self):
        from loop_manager_internal.agents import common_guidance

        with mock.patch.object(common_guidance, "effective_planning_hash", return_value="old-guidance"):
            self.complete(self.select())
        self.assertEqual(self.repository.success("repo")["guidance_hash"], "old-guidance")
        saved_root = {"path": "docs", "category": "documentation"}
        self.connection.execute(
            "UPDATE tiered_maintenance_success SET roots_json = ?, root_reviews_json = ? WHERE repo = ?",
            (code_index.encode([saved_root]), code_index.encode([{"path": "docs"}]), "repo"),
        )

        inv = inventory(self.sources, "sha")
        actual = SourceInventory("sha", tuple(
            TrackedSource(source.path, source.content_hash, source.mode, "sha", "source")
            for source in inv.sources), tuple(
            ClassifiedPath(path, "source", "source") for path in self.sources))
        placement = SimpleNamespace(path="docs", category="documentation", reason="Tracked docs root")
        source = TrackedSource(".", "d" * 64, "040000", "sha", "root review source")
        with mock.patch.object(tiered_review, "cached_source_inventory", return_value=actual), \
                mock.patch.object(tiered_review, "root_placement_inventory", return_value=(placement,)), \
                mock.patch.object(code_index, "git", side_effect=lambda _repo, _command, value:
                    self.sources[value.split(":", 1)[1]]), \
                mock.patch.object(common_guidance, "effective_planning_hash", return_value="new-guidance"), \
                mock.patch.object(maintenance_file_ledger, "root_review_source", return_value=source), \
                mock.patch.object(maintenance_file_ledger.FileReviewRepository,
                                  "reusable_root_reviews", return_value=None), \
                mock.patch.object(maintenance_file_ledger, "validate_root_reviews",
                                  side_effect=AssertionError("stale guidance must not reuse tiered roots")):
            selection = self.repository.select("repo", Path("/repo"), "main", "sha", 20, 0)
            self.connection.execute("UPDATE tiered_maintenance_success SET root_reviews_json = NULL, "
                                    "guidance_hash = NULL WHERE repo = ?", ("repo",))
            legacy = self.repository.select("repo", Path("/repo"), "main", "sha", 20, 0)

        self.assertTrue(selection.required)
        self.assertEqual(selection.mode, "targeted")
        self.assertEqual(selection.reasons, ("root_review_required",))
        self.assertEqual(selection.paths, ())
        self.assertIsNone(selection.root_reviews)
        self.assertEqual((legacy.mode, legacy.reasons, legacy.paths),
                         ("targeted", ("root_review_required",), ()))
        assessment = self.repository.prepare("repo", 2, selection)
        units = self.repository.units(assessment)
        self.assertEqual([unit["kind"] for unit in units], ["root_placement"])

    def test_low_risk_module_change_with_missing_root_evidence_is_root_only_targeted(self):
        from loop_manager_internal.agents import common_guidance

        self.complete(self.select())
        self.connection.execute("UPDATE tiered_maintenance_success SET roots_json = ?, root_reviews_json = ? "
                                "WHERE repo = ?", (code_index.encode([{"path": "docs", "category": "documentation"}]),
                                code_index.encode([{"path": "docs"}]), "repo"))
        self.sources["pkg/a.py"] = b"def calculate(value):\n    return value + 2\n"
        inv = inventory(self.sources, "new-sha")
        actual = SourceInventory("new-sha", tuple(
            TrackedSource(source.path, source.content_hash, source.mode, "new-sha", "source")
            for source in inv.sources), tuple(
            ClassifiedPath(path, "source", "source") for path in self.sources))
        placement = SimpleNamespace(path="docs", category="documentation", reason="Tracked documentation root")
        source = TrackedSource(".", "d" * 64, "040000", "new-sha", "root review source")
        with mock.patch.object(tiered_review, "cached_source_inventory", return_value=actual), \
                mock.patch.object(tiered_review, "root_placement_inventory", return_value=(placement,)), \
                mock.patch.object(code_index, "git", side_effect=lambda _repo, _command, value:
                    self.sources[value.split(":", 1)[1]]), \
                mock.patch.object(common_guidance, "effective_planning_hash", return_value="current-guidance"), \
                mock.patch.object(maintenance_file_ledger, "root_review_source", return_value=source), \
                mock.patch.object(maintenance_file_ledger.FileReviewRepository,
                                  "reusable_root_reviews", return_value=None):
            selection = self.repository.select("repo", Path("/repo"), "main", "new-sha", 20, 0)
            assessment = self.repository.prepare("repo", 2, selection)

        self.assertEqual(selection.mode, "targeted")
        self.assertEqual(selection.reasons, ("root_review_required",))
        self.assertEqual(selection.paths, ())
        units = self.repository.units(assessment)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["kind"], "root_placement")

    def test_fast_assessment_rejects_unreviewed_root_unit_instead_of_calling_model(self):
        selection = self.select()
        selection = replace(selection, mode="fast", root_placements=({"path": "docs",
            "category": "documentation", "reason": "Tracked documentation root"},),
            root_reviews=None, root_source={"path": ".", "content_hash": "d" * 64,
                "mode": "040000", "revision": "sha", "reason": "root review source"}, base="main")
        assessment = self.repository.prepare("repo", 1, selection)
        self.assertEqual(assessment["mode"], "targeted")
        self.assertEqual([unit["kind"] for unit in self.repository.units(assessment)], ["root_placement"])

    def test_prepare_rechecks_root_evidence_guidance_before_binding_it(self):
        from loop_manager_internal.agents import common_guidance

        root = {"path": "docs", "category": "documentation"}
        selection = replace(self.select(), mode="fast", roots=(root,), base="main",
            root_source={"path": ".", "content_hash": "d" * 64, "mode": "040000",
                "revision": "sha", "reason": "root review source"},
            root_placements=({**root, "reason": "Tracked documentation root"},),
            root_reviews=(root,), root_guidance_hash="selected-guidance")
        with mock.patch.object(common_guidance, "effective_planning_hash", return_value="current-guidance"):
            assessment = self.repository.prepare("repo", 1, selection)

        self.assertEqual(assessment["mode"], "targeted")
        self.assertIsNone(assessment["root_reviews"])
        self.assertEqual(assessment["root_guidance_hash"], "current-guidance")
        self.assertEqual([unit["kind"] for unit in self.repository.units(assessment)], ["root_placement"])


class TieredRootReviewTest(MaintenancePlanningCase):
    def test_root_only_targeted_success_keeps_full_baseline_and_reuses_current_root_evidence(self):
        repository = tiered_review.TieredReviewRepository(self.connection)
        selection = repository.select(str(self.repo), self.repo, "main", self.base_sha, 20, 1, force_full=True)
        assessment = repository.prepare(str(self.repo), 1, selection)
        inventory = tracked_source_inventory(self.repo, "main", self.base_sha, include_uncertain=True)
        root_reviews = list(root_results_for(self.repo, inventory))

        def root_output(unit, reviews):
            source = unit["source"]
            return FILE_REVIEW_PREFIX + json.dumps({"path": source["path"],
                "content_hash": source["content_hash"], "mode": source["mode"],
                "source_revision": source["revision"], "root_reviews": reviews})

        for unit in repository.units(assessment):
            if unit.get("kind") == "root_placement":
                output = root_output(unit, root_reviews)
            else:
                reviews = []
                for part in unit["parts"]:
                    path = next(iter(part["records"]))
                    excerpt = (self.repo / path).read_text(encoding="utf-8").strip()
                    reviews.append({"module": part["module"],
                        "summary": "The module responsibility and source relation were reviewed.",
                        "citations": [{"path": path, "excerpt": excerpt}]})
                output = tiered_review.RESULT_PREFIX + json.dumps({"digest": unit["digest"],
                    "reviews": reviews, "findings": []})
            repository.complete(str(self.repo), unit, output, lambda path: (self.repo / path).read_bytes())

        full_evidence = repository.evidence(str(self.repo), 1, self.base_sha)
        repository.promote(str(self.repo), full_evidence)
        self.connection.commit()
        full_success = repository.success(str(self.repo))
        self.assertEqual(full_success["full_source_revision"], self.base_sha)

        fast = repository.select(str(self.repo), self.repo, "main", self.base_sha, 20, 1)
        fast_assessment = repository.prepare(str(self.repo), 2, fast)
        self.assertEqual((fast.required, fast.mode), (False, "fast"))
        self.assertEqual(repository.units(fast_assessment), ())

        product = self.repo / "docs/PRODUCT.md"
        product.write_text(product.read_text(encoding="utf-8") + "\nroot content changed\n",
                           encoding="utf-8", newline="\n")
        git(self.repo, "add", "docs/PRODUCT.md")
        git(self.repo, "commit", "-m", "Change root documentation")
        revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", "HEAD:main")
        selection = repository.select(str(self.repo), self.repo, "main", revision, 20, 1)
        targeted = repository.prepare(str(self.repo), 3, selection)
        units = repository.units(targeted)
        self.assertEqual((selection.mode, selection.reasons, selection.paths),
                         ("targeted", ("root_review_required",), ()))
        self.assertEqual([unit["kind"] for unit in units], ["root_placement"])
        current_inventory = tracked_source_inventory(self.repo, "main", revision, include_uncertain=True)
        current_reviews = list(root_results_for(self.repo, current_inventory))
        unit = units[0]
        repository.complete(str(self.repo), unit, root_output(unit, current_reviews),
                            lambda path: (self.repo / path).read_bytes())
        evidence = repository.evidence(str(self.repo), 3, revision)
        repository.promote(str(self.repo), evidence)

        success = repository.success(str(self.repo))
        self.assertEqual(success["source_revision"], revision)
        self.assertEqual(success["full_merged_count"], full_success["full_merged_count"])
        self.assertEqual(success["full_source_revision"], self.base_sha)

    def test_asset_only_assessment_requires_exact_complete_root_investigation(self):
        for path in ("legacy/module.py", "refactored/adapter.py"):
            (self.repo / path).unlink()
        (self.repo / "scratch.png").write_bytes(b"\x89PNG\x00fixture")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "Create source-free root inventory")
        revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", "HEAD:main")
        repository = tiered_review.TieredReviewRepository(self.connection)
        selection = repository.select(str(self.repo), self.repo, "main", revision, 20, 1, force_full=True)
        self.assertEqual(selection.index.records, {})
        assessment = repository.prepare(str(self.repo), 1, selection)
        units = repository.units(assessment)
        self.assertEqual(len(units), 1)
        unit = units[0]
        self.assertEqual(unit["kind"], "root_placement")
        inventory = tracked_source_inventory(self.repo, "main", revision, include_uncertain=True)
        reviews = list(root_results_for(self.repo, inventory))

        def output(root_reviews):
            source = unit["source"]
            return FILE_REVIEW_PREFIX + json.dumps({"path": source["path"], "content_hash": source["content_hash"],
                "mode": source["mode"], "source_revision": source["revision"], "root_reviews": root_reviews})

        duplicate_reference = dict(reviews[0], references=reviews[0]["references"] * 2)
        protected_move = [dict(item) for item in reviews]
        protected_move[0].update(category="internal_implementation", destination="archive/docs")
        for invalid in ([], [reviews[0], reviews[0]], [duplicate_reference, *reviews[1:]], protected_move,
                        [dict(reviews[0], references=[{"path": [], "excerpt": "untrusted reference"}]), *reviews[1:]],
                        [dict(reviews[0], references=[{"path": ".", "excerpt": "forged root citation"}]), *reviews[1:]]):
            with self.subTest(invalid=invalid), self.assertRaises(tiered_review.TieredReviewError):
                repository.complete(str(self.repo), unit, output(invalid), lambda _path: b"")
        repository.complete(str(self.repo), unit, output(reviews), lambda _path: b"")
        evidence = repository.evidence(str(self.repo), 1, revision)
        self.assertEqual(evidence["receipts"], [])
        self.assertEqual({item["path"] for item in evidence["root_reviews"]}, {"docs", "scratch.png"})
        self.assertIsNone(evidence["root_plan_links"])
        audit = self.connection.execute(
            "SELECT protocol, source_revision, scope_json FROM tiered_maintenance_result_audit WHERE digest = ?",
            (unit["digest"],),
        ).fetchone()
        self.assertEqual(audit[0], tiered_review.ROOT_RESULT_PROTOCOL)
        self.assertEqual(audit[1], revision)
        self.assertEqual(json.loads(audit[2]), unit)
        self.connection.execute("UPDATE tiered_maintenance_result_audit SET source_revision = 'stale' WHERE digest = ?",
                                (unit["digest"],))
        with self.assertRaises(tiered_review.TieredReviewError):
            repository.evidence(str(self.repo), 1, revision)

    def test_mixed_tree_investigates_sources_and_excluded_roots_with_launcher_evidence(self):
        (self.repo / "requirements-prod.txt").write_text("example==1.0\n", encoding="utf-8", newline="\n")
        (self.repo / "brand.svg").write_text("<svg><title>Brand</title></svg>\n", encoding="utf-8", newline="\n")
        (self.repo / "run.sh").write_text("#!/bin/sh\nexec python3 app.py\n", encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "Add root manifest, asset, and launcher")
        revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", "HEAD:main")

        repository = tiered_review.TieredReviewRepository(self.connection)
        assessment = repository.prepare(str(self.repo), 1, repository.select(
            str(self.repo), self.repo, "main", revision, 20, 1, force_full=True))
        units = repository.units(assessment)
        root_unit = next(unit for unit in units if unit.get("kind") == "root_placement")
        inventory = tracked_source_inventory(self.repo, "main", revision, include_uncertain=True)
        reviews = list(root_results_for(self.repo, inventory))
        launcher = next(item for item in reviews if item["path"] == "run.sh")
        launcher.update(category="execution_build_deploy", destination=None,
            references=[{"path": "run.sh", "excerpt": "#!/bin/sh\nexec python3 app.py"}],
            reason="The tracked executable launches the application and remains at its working path.")
        root_source = root_unit["source"]
        root_output = FILE_REVIEW_PREFIX + json.dumps({
            "path": root_source["path"], "content_hash": root_source["content_hash"],
            "mode": root_source["mode"], "source_revision": root_source["revision"],
            "root_reviews": reviews,
        })
        for unit in units:
            if unit is root_unit:
                output = root_output
            else:
                module_reviews = []
                for part in unit["parts"]:
                    path = next(iter(part["records"]))
                    excerpt = (self.repo / path).read_text(encoding="utf-8").rstrip("\n")
                    module_reviews.append({"module": part["module"],
                        "summary": "The source modules are small fixtures with no uncovered structural issue.",
                        "citations": [{"path": path, "excerpt": excerpt}]})
                value = {"digest": unit["digest"], "reviews": module_reviews, "findings": []}
                output = tiered_review.RESULT_PREFIX + json.dumps(value)
            repository.complete(str(self.repo), unit, output, lambda path: (self.repo / path).read_bytes())

        evidence = repository.evidence(str(self.repo), 1, revision)
        self.assertEqual({item["path"] for item in evidence["root_reviews"]},
                         {"docs", "legacy", "refactored", "requirements-prod.txt", "brand.svg", "run.sh"})
        self.assertEqual(next(item for item in evidence["root_reviews"] if item["path"] == "run.sh")["category"],
                         "execution_build_deploy")
        self.assertNotIn("run.sh", tiered_review._root_move_candidates(evidence["root_reviews"]))
        self.assertTrue(evidence["receipts"])


if __name__ == "__main__":
    unittest.main()
