import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

import maintenance_file_inventory
import maintenance_file_review as review
import maintenance_file_ledger


def result_for(source, excerpt):
    return {
        "path": source.path,
        "content_hash": source.content_hash,
        "mode": source.mode,
        "source_revision": source.revision,
        "evidence": {
            "excerpts": [excerpt],
            "readability": "Names show the workflow clearly.",
            "responsibility_and_integration": "The function owns one workflow and no external calls.",
            "error_recovery": "Errors pass to the caller without hidden state.",
            "test_boundary": "The function can be tested through its return value.",
            "duplication": "No duplicate implementation was found in this file.",
            "change_context": "The narrow function keeps unrelated code out of later work.",
        },
        "code_map": {
            "responsibility": "Calculate the input value for callers.",
            "entry_points": ["calculate"],
            "public_symbols": ["calculate"],
            "direct_references": [],
            "related_tests": ["tests/test_calc.py"],
        },
        "pending_references": [],
        "similar_responsibilities": [],
    }


class ProvisionalFileReviewTest(unittest.TestCase):
    def test_structured_reference_candidates_are_normalized_and_source_checked(self):
        content = b"def calculate(): return helper()\n"
        source = maintenance_file_inventory.TrackedSource(
            "src/calc.py", hashlib.sha256(content).hexdigest(), "100644",
            "a" * 40, "source",
        )
        payload = result_for(source, "def calculate(): return helper()")
        payload["code_map"]["direct_references"] = [
            {"target_path": "src/helper.py", "symbol": "helper", "evidence": "helper()"},
        ]
        payload["pending_references"] = [
            {"target_path": "src/helper.py", "symbol": "helper", "evidence": "helper()"},
        ]
        payload["similar_responsibilities"] = [
            {"target_path": "src/other.py", "responsibility": "calculation",
             "reason": "Both modules calculate a returned value."},
        ]
        output = review.RESULT_PREFIX + json.dumps(payload)
        parsed = review.parse_review(output, source, content)
        self.assertEqual(parsed["pending_references"][0]["target_path"], "src/helper.py")
        self.assertEqual(parsed["similar_responsibilities"][0]["target_path"], "src/other.py")
        payload["pending_references"][0]["evidence"] = "imaginary()"
        with self.assertRaisesRegex(review.FileReviewError, "원본 인용"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)

    def test_large_source_prompt_uses_revision_bound_chunked_read(self):
        content = b"def calculate(): return 1\n" * 3000
        source = maintenance_file_inventory.TrackedSource(
            "src/calc.py", hashlib.sha256(content).hexdigest(), "100644",
            "a" * 40, "source",
        )
        prompt = review.review_prompt(source, content)
        self.assertIn("git show " + source.revision + ":" + source.path, prompt)
        self.assertIn("작은 연속 구간", prompt)
        self.assertNotIn("원본 시작", prompt)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(
            "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT)"
        )
        self.connection.execute(
            "INSERT INTO projects VALUES (?, 'main')", (str(self.repo),)
        )
        self.progress = review.ProvisionalReviewRepository(self.connection)
        self.ledger = maintenance_file_ledger.FileReviewRepository(self.connection)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def publish(self, files):
        for path, content in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "source")
        revision = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", revision)
        return maintenance_file_inventory.tracked_source_inventory(
            self.repo, "main", revision,
        )

    def state(self, inventory, cycle=1):
        return self.progress.progress(
            str(self.repo), self.repo, "main", cycle, inventory,
        )

    def finish(self, source):
        content = review.source_bytes(self.repo, "main", source)
        payload = result_for(source, content.decode().strip())
        parsed = review.parse_review(
            review.RESULT_PREFIX + json.dumps(payload), source, content,
        )
        self.progress.start(str(self.repo), 1, source)
        self.progress.complete(str(self.repo), 1, source, parsed)
        return parsed

    def test_multiple_units_resume_without_advancing_success_ledger(self):
        inventory = self.publish({
            "src/a.py": "def calculate(): return 1\n",
            "src/b.py": "def calculate(): return 2\n",
        })
        first = self.state(inventory)
        self.assertEqual(len(first.selected), 2)
        self.assertEqual(first.next_unit.source.path, "src/a.py")
        self.progress.start(str(self.repo), 1, first.next_unit.source)
        self.progress.session(str(self.repo), 1, first.next_unit.source, "resume-a")
        in_flight = self.state(inventory)
        self.assertEqual(in_flight.next_unit.session_id, "resume-a")
        a = self.finish(first.next_unit.source)
        resumed = review.ProvisionalReviewRepository(self.connection).progress(
            str(self.repo), self.repo, "main", 1, inventory,
        )
        self.assertEqual(resumed.completed, (a,))
        self.assertEqual(resumed.completed[0]["code_map"]["public_symbols"], ["calculate"])
        self.assertEqual(resumed.completed[0]["code_map"]["related_tests"],
                         ["tests/test_calc.py"])
        self.assertEqual(resumed.next_unit.source.path, "src/b.py")
        self.finish(resumed.next_unit.source)
        final = self.state(inventory)
        self.assertIsNone(final.next_unit)
        self.assertEqual(len(final.completed), 2)
        self.assertEqual(self.ledger.reviews(str(self.repo)), ())
        self.assertIsNone(self.ledger.last_success(str(self.repo)))

    def test_rejects_forged_evidence_empty_blob_and_duplicate_result(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n"})
        source = inventory.sources[0]
        content = review.source_bytes(self.repo, "main", source)
        payload = result_for(source, "not in source")
        with self.assertRaisesRegex(review.FileReviewError, "인용"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)
        payload["evidence"]["excerpts"] = ["def calculate(): return 1"]
        line = review.RESULT_PREFIX + json.dumps(payload)
        with self.assertRaisesRegex(review.FileReviewError, "정확히 하나"):
            review.parse_review(line + "\n" + line, source, content)
        with self.assertRaisesRegex(review.FileReviewError, "빈 원본"):
            review.parse_review(line, source, b"")
        payload["mode"] = "100755"
        with self.assertRaisesRegex(review.FileReviewError, "identity"):
            review.parse_review(review.RESULT_PREFIX + json.dumps(payload), source, content)

    def test_source_drift_and_cycle_revision_invalidate_only_stale_units(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n"})
        source = inventory.sources[0]
        self.finish(source)
        self.assertEqual(len(self.state(inventory).completed), 1)
        self.assertIsNotNone(self.state(inventory, cycle=2).next_unit)
        self.finish(source)
        changed = self.publish({"src/a.py": "def calculate(): return 2\n"})
        with self.assertRaises(maintenance_file_inventory.SourceInventoryError):
            self.state(inventory)
        fresh = self.state(changed)
        self.assertEqual(fresh.completed, ())
        self.assertEqual(fresh.next_unit.source.content_hash,
                         hashlib.sha256(b"def calculate(): return 2\n").hexdigest())

    def test_corrupt_saved_result_and_mode_change_require_new_review(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n"})
        source = inventory.sources[0]
        self.finish(source)
        self.connection.execute(
            "UPDATE provisional_file_reviews SET result_json = ? WHERE repo = ?",
            ('{"forged":true}', str(self.repo)),
        )
        self.assertEqual(self.state(inventory).completed, ())
        self.finish(source)
        (self.repo / source.path).chmod(0o755)
        changed = self.publish({})
        current = self.state(changed)
        self.assertEqual(current.completed, ())
        self.assertEqual(current.next_unit.source.mode, "100755")
        (self.repo / source.path).rename(self.repo / "src/renamed.py")
        renamed = self.publish({})
        moved = self.state(renamed)
        self.assertEqual(moved.completed, ())
        self.assertEqual(moved.next_unit.source.path, "src/renamed.py")

    def test_context_file_is_not_selected_or_code_mapped(self):
        inventory = self.publish({"src/a.py": "def calculate(): return 1\n",
                                  "src/b.py": "def calculate(): return 2\n"})
        selected = inventory.sources[0]
        context = inventory.sources[1]
        self.ledger.record_success(
            str(self.repo), str(self.repo), "main", inventory,
            tuple(maintenance_file_ledger.ReviewedSource(
                item.path, item.content_hash, item.mode, item.revision,
                "Reviewed implementation and test responsibilities.",
            ) for item in inventory.sources),
        )
        changed = self.publish({"src/a.py": "def calculate(): return 3\n"})
        state = self.state(changed)
        self.assertEqual(tuple(item.path for item in state.selected), (selected.path,))
        current_context = next(item for item in changed.sources if item.path == context.path)
        self.assertEqual(review.source_bytes(self.repo, "main", current_context),
                         b"def calculate(): return 2\n")
        self.assertEqual(self.progress.completed_summary(str(self.repo)),
                         {"preview": [], "more_than_preview": False})


if __name__ == "__main__":
    unittest.main()
