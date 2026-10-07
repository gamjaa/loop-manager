from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from loop_manager_internal.maintenance import code_index, tiered_review
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, ClassifiedPath, TrackedSource
from tests.support.maintenance import inventory


class TieredReviewTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.repository = tiered_review.TieredReviewRepository(self.connection)
        self.sources = {"pkg/a.py": b"def calculate(value):\n    return value + 1\n",
                        "pkg/b.py": b"from pkg import a\ndef read():\n    return a.calculate(2)\n",
                        "tests/test_a.py": b"from pkg import a\ndef test_value():\n    assert a.calculate(1) == 2\n",
                        "tests/test_other.py": b"def test_unrelated():\n    assert True\n"}

    def select(self, merged=0, revision="sha", **kwargs):
        inv = inventory(self.sources, revision)
        actual = SourceInventory(revision, tuple(TrackedSource(source.path, source.content_hash, source.mode, revision, "source")
            for source in inv.sources), tuple(ClassifiedPath(path, "source", "source") for path in self.sources))
        with mock.patch.object(tiered_review, "cached_source_inventory", return_value=actual), \
                mock.patch.object(code_index, "git", side_effect=lambda _repo, _command, value: self.sources[value.split(":", 1)[1]]):
            return self.repository.select("repo", Path("/repo"), "main", revision, 20, merged, **kwargs)

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
            self.assertEqual(resumed, {key: value for key, value in first.items() if key != 'receipts'})
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


if __name__ == "__main__":
    unittest.main()
