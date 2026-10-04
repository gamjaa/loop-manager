import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.maintenance import maintenance_findings as stream
from loop_manager_internal.maintenance import maintenance_reconciliation as review
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, TrackedSource


class FindingsStreamTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.repository = stream.FindingsRepository(self.connection)
        self.content = b"def calculate(): return 1\n"
        source = TrackedSource("src/calc.py", hashlib.sha256(self.content).hexdigest(), "100644", "a" * 40, "source")
        self.source = source
        self.inventory = SourceInventory(source.revision, (source,), ())
        self.progress = review.ReconciliationProgress((source,), (), (), None)
        self.summary = "All identified calculation boundaries have grounded plan comparisons."

    def data(self, finding_count=1, plan_count=3):
        plans = {f"G-{index:04d}": "READY" for index in range(plan_count)}
        texts = {plan: "Extract calculation from the interface module into a focused unit." for plan in plans}
        findings = []
        for index in range(finding_count):
            boundary = f"Extract calculation boundary number {index} from its interface module."
            findings.append({"issue_key": f"issue-{index:04d}", "targets": [self.source.path],
                "evidence": [{"path": self.source.path, "content_hash": self.source.content_hash,
                              "mode": self.source.mode, "excerpt": "def calculate()"}],
                "relation_keys": [], "boundary": boundary,
                "context_reduction": "Interface changes avoid unrelated calculation details.",
                "integration_cost": "One explicit import and focused unit tests preserve the boundary.",
                "status": "existing_plan", "plan_id": "G-0000",
                "plan_comparisons": [{"plan_id": plan, "plan_excerpt": text,
                    "finding_excerpt": boundary, "same_issue": plan == "G-0000",
                    "reason": "The compared plan scope addresses this boundary." if plan == "G-0000"
                              else "This plan covers a different responsibility from the finding."}
                    for plan, text in texts.items()]})
        return plans, texts, findings

    def prepare(self, plans, texts, **overrides):
        return self.repository.prepare("repo", overrides.get("cycle", 1), overrides.get("revision", self.source.revision),
                                       overrides.get("synthesis", "b" * 64), plans, texts)

    def validate(self, plans, texts):
        def check(payload):
            review.parse_findings(payload, self.progress, self.inventory, {self.source.path: self.content},
                                  plans, (), texts, synthesis_digest="b" * 64)
        return check

    def parts(self, findings):
        for finding in findings:
            body = {key: value for key, value in finding.items() if key != "plan_comparisons"}
            records = [("finding", None, body), *(("comparison", item["plan_id"], item) for item in finding["plan_comparisons"])]
            for kind, plan, record in records:
                text = stream.encode(record)
                fragments = [text[index:index + 2000] for index in range(0, len(text), 2000)]
                for index, fragment in enumerate(fragments):
                    yield {"kind": kind, "issue_key": finding["issue_key"], "plan_id": plan,
                           "part": index, "text": fragment, "last": index == len(fragments) - 1}

    def envelope(self, unit, parts, complete=False, counts=None):
        return {"manifest_id": unit.manifest_id, "index": unit.index, "parts": parts,
                "complete": complete, "counts": counts, "summary": self.summary if complete else None}

    def send(self, unit, value, plans, texts):
        output = stream.UNIT_PREFIX + stream.encode(value)
        self.assertLessEqual(len(output.encode()), stream.MAX_UNIT_BYTES)
        self.repository.complete("repo", unit, output, plans, self.validate(plans, texts))

    def collect(self, plans, texts, findings, restart_at=None):
        pending = list(self.parts(findings))
        units = 0
        while pending:
            unit = self.prepare(plans, texts)
            if unit.index == restart_at:
                self.repository.session("repo", unit, "saved-unit")
                self.repository.fail("repo", unit, "Resume the uncompleted record without repeating stored parts.")
                self.repository = stream.FindingsRepository(self.connection)
                unit = self.prepare(plans, texts)
                self.assertEqual(unit.session_id, "saved-unit")
                self.assertIn("Resume", unit.error)
            batch = []
            while pending:
                candidate = self.envelope(unit, [*batch, pending[0]])
                if len((stream.UNIT_PREFIX + stream.encode(candidate)).encode()) > stream.MAX_UNIT_BYTES - 2200:
                    break
                batch.append(pending.pop(0))
            self.assertTrue(batch)
            complete = not pending
            counts = {"finding_count": len(findings), "comparison_count": len(findings) * len(plans)} if complete else None
            self.send(unit, self.envelope(unit, batch, complete, counts), plans, texts)
            units += 1
        return self.prepare(plans, texts), units

    def test_small_nonempty_result_finishes_in_one_unit(self):
        plans, texts, findings = self.data()
        unit, count = self.collect(plans, texts, findings)
        self.assertEqual(count, 1)
        output = stream.RESULT_PREFIX + stream.encode(unit.receipt)
        self.assertLess(len(output.encode()), 600)
        self.assertNotIn('"findings":', output)
        self.assertEqual(self.repository.resolve("repo", unit, output, plans)["findings"], findings)

    def test_export_repairs_interrupted_catalog_and_plan_generation(self):
        plans, texts, findings = self.data()
        unit, _ = self.collect(plans, texts, findings)
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            manifest = self.repository.export("repo", unit, plans, texts, evidence)
            original_catalog = (manifest.parent / "plans.jsonl").read_bytes()
            original_plan = (manifest.parent / "plan-00000000.txt").read_bytes()
            (manifest.parent / "plan-00000000.txt").write_bytes(original_plan[:8])
            (manifest.parent / "plans.jsonl").write_bytes(original_catalog[:10])
            publish = stream._publish_file

            def interrupt(path, content):
                if path.name == "plans.jsonl":
                    raise OSError("interrupted before catalog publication")
                publish(path, content)
            with mock.patch.object(stream, "_publish_file", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    self.repository.export("repo", unit, plans, texts, evidence)
            self.assertFalse(manifest.exists())
            self.assertEqual((manifest.parent / "plan-00000000.txt").read_bytes(), original_plan)
            self.assertEqual(self.repository.export("repo", unit, plans, texts, evidence), manifest)
            self.assertEqual((manifest.parent / "plans.jsonl").read_bytes(), original_catalog)
            with mock.patch.object(stream, "_publish_file") as write:
                self.repository.export("repo", unit, plans, texts, evidence)
                write.assert_not_called()
            missing = manifest.parent / "plan-00000001.txt"
            missing.unlink()
            self.repository.export("repo", unit, plans, texts, evidence)
            self.assertEqual(missing.read_text(), texts["G-0001"])
            outside = Path(directory) / "outside.txt"
            outside.write_text("outside remains untouched")
            linked = manifest.parent / "plan-00000002.txt"
            linked.unlink()
            linked.symlink_to(outside)
            self.repository.export("repo", unit, plans, texts, evidence)
            self.assertFalse(linked.is_symlink())
            self.assertEqual(linked.read_text(), texts["G-0002"])
            self.assertEqual(outside.read_text(), "outside remains untouched")
            saved_unit = manifest.parent / "unit-00000000.json"
            original_unit = saved_unit.read_bytes()
            saved_unit.write_bytes(original_unit[:12])
            self.repository.export("repo", unit, plans, texts, evidence)
            self.assertEqual(saved_unit.read_bytes(), original_unit)

    def test_many_findings_and_plans_preserve_every_record_with_resume(self):
        plans, texts, findings = self.data(30, 120)
        self.assertGreater(len(stream.encode(findings).encode()), stream.MAX_UNIT_BYTES * 30)
        unit, count = self.collect(plans, texts, findings, restart_at=2)
        self.assertGreater(count, 30)
        self.assertEqual(unit.receipt["finding_count"], 30)
        self.assertEqual(unit.receipt["comparison_count"], 3600)
        output = stream.RESULT_PREFIX + stream.encode(unit.receipt)
        self.assertLess(len(output.encode()), 600)
        self.assertEqual(self.repository.resolve("repo", unit, output, plans)["findings"], findings)
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.repository.export("repo", unit, plans, texts, Path(directory) / "evidence.json")
            self.assertEqual(json.loads(manifest.read_text())["receipt"], unit.receipt)
            self.assertEqual(len(list(manifest.parent.glob("unit-*.json"))), count)
        self.connection.execute("DELETE FROM provisional_findings_units WHERE unit_index=2")
        with self.assertRaises(review.ReconciliationError):
            self.repository.resolve("repo", unit, output, plans)

    def test_single_finding_comparison_matrix_alone_exceeds_limit(self):
        plans, texts, findings = self.data(1, 100)
        self.assertGreater(len(stream.encode(findings[0]["plan_comparisons"]).encode()), stream.MAX_UNIT_BYTES)
        unit, count = self.collect(plans, texts, findings)
        self.assertGreater(count, 1)
        self.assertEqual(self.repository.resolve("repo", unit, stream.RESULT_PREFIX + stream.encode(unit.receipt), plans)["findings"], findings)

    def test_missing_duplicate_stale_and_oversized_units_are_rejected(self):
        plans, texts, findings = self.data()
        unit = self.prepare(plans, texts)
        parts = list(self.parts(findings))
        counts = {"finding_count": 1, "comparison_count": 3}
        for broken in (parts[:-1], [parts[0], *parts]):
            with self.assertRaises(review.ReconciliationError):
                self.send(unit, self.envelope(unit, broken, True, counts), plans, texts)
            self.assertEqual(self.prepare(plans, texts).index, 0)
        with self.assertRaises(review.ReconciliationError):
            self.repository.complete("repo", unit, "x" * (stream.MAX_UNIT_BYTES + 1), plans, self.validate(plans, texts))
        self.send(unit, self.envelope(unit, parts, True, counts), plans, texts)
        with self.assertRaises(review.ReconciliationError):
            self.send(unit, self.envelope(unit, parts, True, counts), plans, texts)
        sealed = self.prepare(plans, texts)
        wrong = {**sealed.receipt, "finding_count": 2}
        with self.assertRaises(review.ReconciliationError):
            self.repository.resolve("repo", sealed, stream.RESULT_PREFIX + stream.encode(wrong), plans)
        for overrides, changed_plans, changed_texts in (
            ({"revision": "c" * 40}, plans, texts), ({"cycle": 2}, plans, texts),
            ({"synthesis": "d" * 64}, plans, texts), ({}, {**plans, "G-0000": "IMPLEMENTING"}, texts),
            ({}, plans, {**texts, "G-0000": texts["G-0000"] + " updated scope"}),
        ):
            fresh = self.prepare(changed_plans, changed_texts, **overrides)
            self.assertNotEqual(fresh.manifest_id, sealed.manifest_id)
            self.assertEqual(fresh.index, 0)
            with self.assertRaises(review.ReconciliationError):
                self.repository.resolve("repo", sealed, stream.RESULT_PREFIX + stream.encode(sealed.receipt), plans)
            with self.assertRaises(review.ReconciliationError):
                self.repository.resolve("repo", fresh, stream.RESULT_PREFIX + stream.encode(sealed.receipt), changed_plans)

    def test_record_fragments_resume_and_restart_cannot_reuse_old_parts(self):
        plans, texts, findings = self.data(1, 1)
        parts = list(self.parts(findings))
        original = parts[0]
        first = {**original, "text": original["text"][:100], "last": False}
        last = {**original, "text": original["text"][100:], "part": 1}
        unit = self.prepare(plans, texts)
        self.send(unit, self.envelope(unit, [first]), plans, texts)
        next_unit = self.prepare(plans, texts)
        with self.assertRaises(review.ReconciliationError):
            self.send(next_unit, self.envelope(next_unit, [{**last, "part": 2}]), plans, texts)
        self.send(next_unit, self.envelope(next_unit, [last, *parts[1:]], True,
                  {"finding_count": 1, "comparison_count": 1}), plans, texts)
        old = self.prepare(plans, texts)
        changed = self.prepare(plans, texts, cycle=2)
        self.send(changed, self.envelope(changed, [first]), plans, texts)
        changed = self.prepare(plans, texts, cycle=2)
        self.repository.complete("repo", changed, "FINDINGS_RESTART: " + stream.encode({
            "manifest_id": changed.manifest_id, "index": changed.index,
            "reason": "The earlier proposed boundary must be corrected before plan comparisons."}), plans, self.validate(plans, texts))
        restarted = self.prepare(plans, texts, cycle=2)
        self.assertNotEqual(restarted.manifest_id, changed.manifest_id)
        self.assertEqual(restarted.index, 0)
        with self.assertRaises(review.ReconciliationError):
            self.repository.resolve("repo", restarted, stream.RESULT_PREFIX + stream.encode(old.receipt), plans)


if __name__ == "__main__":
    unittest.main()
