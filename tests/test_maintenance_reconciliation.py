import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from dataclasses import replace
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

    def test_valid_map_does_not_hide_reverse_reference_in_current_source(self):
        selected_review = review(self.sources["src/a.py"])
        selected_review["code_map"]["responsibility"] = "Compute values for the public API."
        selected = self.progress((self.sources["src/a.py"],), (selected_review,))
        old_map = review(self.sources["src/b.py"])["code_map"]
        old_map["responsibility"] = "Handle requests through the adapter."
        old_map["public_symbols"] = ["helper"]
        self.connection.execute("INSERT INTO successful_code_maps VALUES (?, ?, ?, ?, ?)",
                                ("repo", "src/b.py", self.sources["src/b.py"].content_hash,
                                 "100644", json.dumps(old_map)))
        with (mock.patch.object(reconciliation, "cached_source_inventory", return_value=self.inventory),
              mock.patch.object(reconciliation, "source_bytes",
                                side_effect=lambda _repo, _base, item: self.contents[item.path]) as read):
            progress = self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
        relation = next(item for item in progress.relations if item.right.path == "src/b.py")
        self.assertIn("source_reverse", {item["kind"] for item in relation.signals})
        self.assertIn(self.sources["src/b.py"], [call.args[2] for call in read.call_args_list])
        self.assertIsNotNone(progress.next_relation)
        self.assertEqual(progress.selected, selected.selected)

    def test_cached_relation_receipt_avoids_rereads_but_revalidates_changed_result(self):
        selected = self.progress((self.sources["src/a.py"],), (self.review_a,))
        with (mock.patch.object(reconciliation, "cached_source_inventory", return_value=self.inventory),
              mock.patch.object(reconciliation, "source_bytes", side_effect=lambda _repo, _base, item: self.contents[item.path]) as read):
            progress = self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
            relation = next(item for item in progress.relations if item.right.path == "src/b.py")
            self.repository.start("repo", 1, REVISION, relation)
            result = {"key": relation.key, "status": "confirmed", "left_excerpt": "helper()",
                      "right_excerpt": "def helper()", "reason": "The reviewed caller uses this helper implementation."}
            self.repository.complete("repo", 1, relation, result)
            self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
            read.reset_mock()
            self.repository = reconciliation.ReconciliationRepository(self.connection)
            self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
            read.assert_not_called()
            result["left_excerpt"] = "fabricated source excerpt"
            self.connection.execute("UPDATE provisional_relations SET result_json = ?", (json.dumps(result),))
            progress = self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
            self.assertEqual(progress.completed, ())
            self.assertEqual(read.call_count, 2)

    def test_interrupted_scan_keeps_completed_source_receipts(self):
        selected = self.progress((self.sources["src/a.py"],), (self.review_a,))
        def interrupted(_repository, _base, source):
            if source.path == "src/c.py":
                raise reconciliation.FileReviewError("interrupted source read")
            return self.contents[source.path]
        with (mock.patch.object(reconciliation, "cached_source_inventory", return_value=self.inventory),
              mock.patch.object(reconciliation, "source_bytes", side_effect=interrupted)):
            with self.assertRaises(reconciliation.FileReviewError):
                self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
        self.repository = reconciliation.ReconciliationRepository(self.connection)
        with (mock.patch.object(reconciliation, "cached_source_inventory", return_value=self.inventory),
              mock.patch.object(reconciliation, "source_bytes", return_value=self.contents["src/c.py"]) as read):
            progress = self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
        read.assert_called_once_with(Path("repo"), "main", self.sources["src/c.py"])
        self.assertFalse(progress.pending_scan)

    def test_only_changed_context_identity_is_rescanned(self):
        selected = self.progress((self.sources["src/a.py"],), (self.review_a,))
        with (mock.patch.object(reconciliation, "cached_source_inventory", return_value=self.inventory) as inventory,
              mock.patch.object(reconciliation, "source_bytes", side_effect=lambda _repo, _base, item: self.contents[item.path]) as read):
            self.repository.progress("repo", Path("repo"), "main", 1, self.inventory, selected)
            changed = replace(self.sources["src/b.py"], mode="100755")
            inventory.return_value = replace(self.inventory, sources=(self.sources["src/a.py"], changed, self.sources["src/c.py"]))
            read.reset_mock()
            self.repository.progress("repo", Path("repo"), "main", 1, inventory.return_value, selected)
            read.assert_called_once_with(Path("repo"), "main", changed)

    def test_short_public_symbols_missing_from_valid_map_still_form_candidates(self):
        for symbol in ("foo", "id", "x"):
            with self.subTest(symbol=symbol):
                selected_review = review(self.sources["src/a.py"])
                selected_review["code_map"]["public_symbols"] = [symbol]
                old_map = review(self.sources["src/b.py"])["code_map"]
                old_map["responsibility"] = "A different responsibility with an incomplete reference map."
                progress = self.progress((self.sources["src/a.py"],), (selected_review,))
                relations = reconciliation.relation_candidates(self.inventory, progress,
                    {"src/b.py": old_map}, {"src/b.py": f"result = {symbol}()"})
                self.assertEqual(len(relations), 1)
                self.assertIn({"origin": "src/b.py", "target": "src/a.py",
                               "kind": "source_reverse", "evidence": symbol}, relations[0].signals)
                near_miss = reconciliation.relation_candidates(self.inventory, progress,
                    {"src/b.py": old_map}, {"src/b.py": f"result = prefix{symbol}suffix()"})
                self.assertEqual(near_miss, ())

    def test_context_scan_is_bounded_durable_and_invalidates_changed_queries(self):
        selected = self.progress((self.sources["src/a.py"],), (self.review_a,))
        context = tuple(source(f"src/context_{index}.py", b"calculate()") for index in range(35))
        inventory = SourceInventory(REVISION, (self.sources["src/a.py"], *context), ())
        with (mock.patch.object(reconciliation, "cached_source_inventory", return_value=inventory),
              mock.patch.object(reconciliation, "source_bytes", return_value=b"calculate()") as read):
            first = self.repository.progress("repo", Path("repo"), "main", 1, inventory, selected)
            self.assertTrue(first.pending_scan)
            self.assertEqual(read.call_count, 16)
            with self.assertRaises(reconciliation.ReconciliationError):
                reconciliation.synthesis_pages(first)
            self.repository = reconciliation.ReconciliationRepository(self.connection)
            second = self.repository.progress("repo", Path("repo"), "main", 1, inventory, selected)
            self.assertTrue(second.pending_scan)
            self.assertEqual(read.call_count, 32)
            self.repository.progress("repo", Path("repo"), "main", 1, inventory, selected)
            read.reset_mock()
            self.repository.progress("repo", Path("repo"), "main", 1, inventory, selected)
            read.assert_not_called()
            changed = self.progress(selected.selected, ({**self.review_a, "pending_references": []},))
            self.assertTrue(self.repository.progress("repo", Path("repo"), "main", 1, inventory, changed).pending_scan)
            self.assertEqual(read.call_count, 16)

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
        with (mock.patch.object(reconciliation, "cached_source_inventory", return_value=self.inventory),
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

    def test_no_issue_requires_each_completed_relation_reason(self):
        result = {"key": "relation-key", "status": "excluded",
                  "reason": "The helpers implement distinct responsibilities for their callers."}
        progress = reconciliation.ReconciliationProgress((), (result,), (), None)
        value = {"files": [], "relations": []}
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "누락"):
            reconciliation._validate_no_issue(value, progress)
        value["relations"] = [{"id": result["key"], "excerpt": result["reason"],
                               "reason": "Distinct responsibilities do not justify merging these helpers."}]
        reconciliation._validate_no_issue(value, progress)
        value["relations"][0]["excerpt"] = "Fabricated relationship review evidence."
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "검증된 검토"):
            reconciliation._validate_no_issue(value, progress)

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


class SynthesisPagingTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        self.connection.execute("INSERT INTO projects VALUES ('repo')")
        self.repository = reconciliation.SynthesisRepository(self.connection)
        self.reviews = tuple({"path": f"src/{index}.py", "evidence": {
            "chunk_reviews": ["large evidence " * 3000], "issue": f"unique-issue-{index}"}}
            for index in range(13))
        self.progress = reconciliation.ReconciliationProgress((), (), (), None, file_reviews=self.reviews)

    def finish(self, page):
        self.repository.session("repo", page, "session-one")
        self.repository.complete("repo", page, reconciliation.SYNTHESIS_PREFIX + json.dumps({
            "key": page.key, "excerpt": page.text.partition("\n")[2][:20], "has_issue": True,
            "assessment": "Keep this page's structural issue for the final reconciliation.",
        }))

    def test_large_records_are_lossless_bounded_and_resume_after_restart(self):
        pages = reconciliation.synthesis_pages(self.progress)
        self.assertGreater(len(pages), 13)
        expected = "".join(json.dumps({"kind": "file_review", "value": value},
                                     ensure_ascii=False, sort_keys=True) for value in self.reviews)
        self.assertEqual("".join(page.partition("\n")[2] for page in pages), expected)
        first = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.finish(first)
        self.repository = reconciliation.SynthesisRepository(self.connection)
        page = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.assertEqual((page.index, page.session_id), (1, None))
        while page.index < page.total:
            self.assertLess(len(reconciliation.synthesis_prompt(page, Path("/evidence/manifest.json")).encode()), 50_000)
            self.finish(page)
            page = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.assertEqual(page.index, len(pages))
        self.assertEqual(len(json.loads(self.connection.execute(
            "SELECT results_json FROM provisional_synthesis").fetchone()[0])), len(pages))
        stale = self.repository.prepare("repo", 1, "b" * 40, self.progress)
        self.assertEqual((stale.index, stale.session_id), (0, None))

    def test_short_tail_and_changed_evidence_or_cycle(self):
        for size in range(11920, 12030):
            progress = reconciliation.ReconciliationProgress((), (), (), None,
                file_reviews=({"path": "a.py", "evidence": "x" * size},))
            pages = reconciliation.synthesis_pages(progress)
            self.assertTrue(all(12 <= len(page) <= reconciliation.SYNTHESIS_PAGE_CHARS for page in pages))
        first = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.finish(first)
        changed = reconciliation.ReconciliationProgress((), (), (), None,
            file_reviews=(*self.reviews, {"path": "new.py", "evidence": "newly validated issue"}))
        page = self.repository.prepare("repo", 1, REVISION, changed)
        self.assertEqual((page.index, page.session_id), (0, None))
        self.assertNotEqual(page.digest, first.digest)
        self.finish(page)
        next_cycle = self.repository.prepare("repo", 2, REVISION, changed)
        self.assertEqual((next_cycle.index, next_cycle.session_id), (0, None))

    def test_invalid_receipt_and_replaced_session_cannot_skip_pages(self):
        first = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.repository.session("repo", first, "session-one")
        with self.assertRaises(reconciliation.ReconciliationError):
            self.repository.complete("repo", first, reconciliation.SYNTHESIS_PREFIX + json.dumps({
                "key": first.key, "excerpt": "nonexistent page evidence", "has_issue": True, "assessment": "A detailed but unsupported assessment."}))
        self.assertEqual(self.repository.prepare("repo", 1, REVISION, self.progress).index, 0)
        self.finish(first)
        second = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.repository.session("repo", second, "replacement")
        replay = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.assertEqual((replay.index, replay.session_id), (1, "replacement"))
        self.finish(replay)
        self.assertEqual(self.repository.prepare("repo", 1, REVISION, self.progress).index, 2)

    def test_fresh_final_session_can_retrieve_all_receipts_and_cannot_omit_any(self):
        page = self.repository.prepare("repo", 1, REVISION, self.progress)
        while page.index < page.total:
            self.finish(page)
            page = self.repository.prepare("repo", 1, REVISION, self.progress)
        self.repository = reconciliation.SynthesisRepository(self.connection)
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.repository.export("repo", page, self.progress, Path(directory))
            metadata = json.loads(manifest.read_text())
            self.assertEqual(metadata["completed"], len(reconciliation.synthesis_pages(self.progress)))
            resolutions = []
            for index in range(metadata["total"]):
                record = json.loads((manifest.parent / f"page-{index:08d}.json").read_text())
                self.assertIn("assessment", record["result"])
                resolutions.append({"key": record["key"], "excerpt": record["result"]["assessment"],
                    "issue_keys": ["found-issue"], "disposition": "linked", "reason": "This saved candidate is addressed by the linked finding."})
            payload = {"digest": page.digest, "pages": resolutions}
            findings = {"findings": [{"issue_key": "found-issue"}]}
            def validate(value):
                self.repository.validate_resolutions("repo", page,
                    "SYNTHESIS_RESOLUTIONS: " + json.dumps(value), findings)
            validate(payload)
            for changed in ({**payload, "pages": resolutions[1:]},
                            {**payload, "digest": "stale"},
                            {**payload, "pages": [*resolutions, resolutions[0]]},
                            {**payload, "pages": [{**resolutions[0], "issue_keys": [], "disposition": "no_issue"}, *resolutions[1:]]},
                            {**payload, "pages": [{**resolutions[0], "excerpt": "invented saved assessment"}, *resolutions[1:]]}):
                with self.assertRaises(reconciliation.ReconciliationError):
                    validate(changed)
            excluded = [{**item, "issue_keys": [], "disposition": "excluded",
                         "reason": "The original candidate is excluded after comparing the verified responsibilities."}
                        for item in resolutions]
            self.repository.validate_resolutions("repo", page,
                "SYNTHESIS_RESOLUTIONS: " + json.dumps({**payload, "pages": excluded}), {"findings": []})




if __name__ == "__main__":
    unittest.main()
