import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from maintenance_file_inventory import SourceInventory, TrackedSource
from maintenance_file_review import FileReviewProgress
import maintenance_reconciliation as reconciliation


REVISION = "a" * 40


def source(path, content):
    return TrackedSource(path, hashlib.sha256(content).hexdigest(), "100644", REVISION, "source")


def review(item, *, reference=None, similar=None):
    references = ([{"target_path": reference, "symbol": "helper", "evidence": "helper()"}]
                  if reference else [])
    return {
        "path": item.path, "content_hash": item.content_hash, "mode": item.mode,
        "source_revision": item.revision,
        "code_map": {"responsibility": "Calculate values for callers.",
                     "entry_points": [], "public_symbols": ["calculate"],
                     "direct_references": references, "related_tests": []},
        "pending_references": references,
        "similar_responsibilities": ([{
            "target_path": similar, "responsibility": "Calculate values",
            "reason": "Both paths calculate the same returned value.",
        }] if similar else []),
    }


class ReconciliationUnitTest(unittest.TestCase):
    def setUp(self):
        self.contents = {
            "src/a.py": b"def calculate(): return helper()\n",
            "src/b.py": b"def helper(): return calculate()\n",
            "src/c.py": b"def unrelated(): return calculate()\n",
        }
        self.sources = {path: source(path, content) for path, content in self.contents.items()}
        self.inventory = SourceInventory(REVISION, tuple(self.sources.values()), ())
        self.review_a = review(self.sources["src/a.py"], reference="src/b.py")
        self.review_b = review(self.sources["src/b.py"], similar="src/a.py")
        self.review_b["code_map"]["public_symbols"] = ["helper"]
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        self.connection.execute("INSERT INTO projects VALUES ('repo')")
        self.repository = reconciliation.ReconciliationRepository(self.connection)

    def progress(self, selected, completed):
        return FileReviewProgress(tuple(selected), tuple(completed), None)

    def test_forward_reverse_and_unmapped_context_are_order_independent(self):
        first = self.progress((self.sources["src/a.py"], self.sources["src/b.py"]),
                              (self.review_a, self.review_b))
        reverse = self.progress(tuple(reversed(first.selected)), tuple(reversed(first.completed)))
        for progress in (first, reverse):
            relations = reconciliation.relation_candidates(
                self.inventory, progress, {}, {"src/c.py": self.contents["src/c.py"].decode()},
            )
            self.assertEqual({(item.left.path, item.right.path) for item in relations},
                             {("src/a.py", "src/b.py"), ("src/a.py", "src/c.py")})
            if progress is first:
                expected = relations
            else:
                self.assertEqual(relations, expected)

    def test_valid_old_map_contributes_reverse_edge_and_stale_map_is_ignored(self):
        selected = self.progress((self.sources["src/a.py"],),
                                 (review(self.sources["src/a.py"]),))
        old_map = review(self.sources["src/b.py"], reference="src/a.py")["code_map"]
        self.connection.execute(
            "INSERT INTO successful_code_maps VALUES (?, ?, ?, ?, ?)",
            ("repo", "src/b.py", self.sources["src/b.py"].content_hash,
             "100644", json.dumps(old_map)),
        )
        maps = self.repository.valid_maps("repo", self.inventory)
        self.assertEqual(len(reconciliation.relation_candidates(self.inventory, selected, maps, {})), 1)
        self.connection.execute("UPDATE successful_code_maps SET mode = '100755'")
        self.assertEqual(self.repository.valid_maps("repo", self.inventory), {})

    def test_symbol_only_candidate_matches_current_code_map(self):
        selected_review = review(self.sources["src/a.py"])
        selected_review["pending_references"] = [{
            "target_path": None, "symbol": "helper", "evidence": "helper()",
        }]
        old_map = review(self.sources["src/b.py"])["code_map"]
        old_map["public_symbols"] = ["helper"]
        progress = self.progress((self.sources["src/a.py"],), (selected_review,))
        relations = reconciliation.relation_candidates(
            self.inventory, progress, {"src/b.py": old_map}, {},
        )
        self.assertEqual(len(relations), 1)
        self.assertIn("pending_symbol", {item["kind"] for item in relations[0].signals})

    def test_relation_resume_and_source_change_invalidate_only_related_result(self):
        selected = self.progress((self.sources["src/a.py"],), (self.review_a,))
        with (mock.patch.object(reconciliation, "tracked_source_inventory", return_value=self.inventory),
              mock.patch.object(reconciliation, "source_bytes",
                                side_effect=lambda _repo, _base, item: self.contents[item.path])):
            progress = self.repository.progress("repo", Path("repo"), "main", 1,
                                                self.inventory, selected)
            relation = next(item for item in progress.relations if item.right.path == "src/b.py")
            self.repository.start("repo", 1, REVISION, relation)
            self.repository.session("repo", 1, relation, "session-1")
            result = reconciliation.parse_relation(
                reconciliation.RELATION_PREFIX + json.dumps({
                    "key": relation.key, "status": "confirmed",
                    "left_excerpt": "helper()", "right_excerpt": "def helper()",
                    "reason": "The call resolves to this shared helper implementation.",
                }), relation, self.contents[relation.left.path], self.contents[relation.right.path],
            )
            self.repository.complete("repo", 1, relation, result)
            resumed = self.repository.progress("repo", Path("repo"), "main", 1,
                                               self.inventory, selected)
            self.assertIn(result, resumed.completed)
            self.assertEqual(self.repository.unit("repo", relation), (None, None))
            changed = self.progress(selected.selected, (review(self.sources["src/a.py"]),))
            restarted = self.repository.progress("repo", Path("repo"), "main", 1,
                                                 self.inventory, changed)
            self.assertEqual(restarted.completed, ())
            self.assertIsNotNone(restarted.next_relation)
            self.assertEqual(restarted.next_relation.signals[0]["kind"], "unmapped_reverse")

    def test_relation_excerpts_must_be_in_current_blobs(self):
        relation = reconciliation.relation_candidates(
            self.inventory, self.progress((self.sources["src/a.py"],), (self.review_a,)),
            {}, {},
        )[0]
        payload = {"key": relation.key, "status": "excluded",
                   "left_excerpt": "imaginary", "right_excerpt": "def helper()",
                   "reason": "The source calls a different helper implementation."}
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "현재 원본"):
            reconciliation.parse_relation(
                reconciliation.RELATION_PREFIX + json.dumps(payload), relation,
                self.contents[relation.left.path], self.contents[relation.right.path],
            )

    def test_unresolved_relation_blocks_even_a_no_issue_result(self):
        relation = reconciliation.relation_candidates(
            self.inventory, self.progress((self.sources["src/a.py"],), (self.review_a,)),
            {}, {},
        )[0]
        progress = reconciliation.ReconciliationProgress(
            (self.sources["src/a.py"],), (), (relation,), relation,
        )
        output = reconciliation.FINDINGS_PREFIX + json.dumps({
            "source_revision": REVISION, "findings": [],
            "no_issue_reason": "No duplicate responsibility was found after this review.",
        })
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "미확인"):
            reconciliation.parse_findings(output, progress, self.inventory,
                                          self.contents, {}, ())


if __name__ == "__main__":
    unittest.main()
