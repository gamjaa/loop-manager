"""Compact transport must retain source, coverage and durable receipt checks."""
import hashlib
import json
import sqlite3
import unittest

from loop_manager_internal.maintenance import compact_file_review as compact
from loop_manager_internal.maintenance import maintenance_file_review as review
from loop_manager_internal.maintenance import file_review_protocol as protocol
from loop_manager_internal.maintenance.maintenance_file_inventory import TrackedSource
from tests.support.maintenance import compact_result


class CompactFileReviewTest(unittest.TestCase):
    def setUp(self):
        self.content = b"from .helper import helper\nclass Runner:\n    def run(self): return helper()\n"
        self.source = self.source_for(self.content)
        self.paths = ("pkg/review.py", "pkg/helper.py", "pkg/__init__.py")
        self.payload = compact_result(self.source, self.content)

    def source_for(self, content, path="pkg/review.py"):
        return TrackedSource(path, hashlib.sha256(content).hexdigest(), "100644", "a" * 40, "source")

    def parse(self, payload=None, *, source=None, content=None):
        return review.parse_review(protocol.RESULT_PREFIX + json.dumps(payload or self.payload),
                                  source or self.source, self.content if content is None else content,
                                  source_paths=self.paths)

    def test_selected_static_candidates_expand_to_source_grounded_legacy_contract(self):
        self.payload.update(symbols=[1], entry_points=[1], references=[0], pending=[0],
                            tests=["tests/test_review.py"])
        parsed = self.parse()
        self.assertEqual(set(parsed), set(protocol.FILE_RESULT_FIELDS))
        self.assertEqual(parsed["code_map"]["public_symbols"], ["Runner.run"])
        self.assertEqual(parsed["code_map"]["direct_references"], [{
            "target_path": "pkg/helper.py", "symbol": "helper", "evidence": "from .helper import helper",
        }])
        self.assertEqual(parsed["pending_references"], parsed["code_map"]["direct_references"])
        self.assertEqual(parsed["evidence"]["chunk_reviews"][0]["chunk_hash"], self.source.content_hash)
        self.assertEqual(review.parse_review(protocol.RESULT_PREFIX + json.dumps(parsed),
                                            self.source, self.content), parsed)

    def test_candidates_are_not_selected_or_claimed_by_manager(self):
        parsed = self.parse()
        self.assertEqual(parsed["code_map"]["public_symbols"], [])
        self.assertEqual(parsed["code_map"]["direct_references"], [])
        self.assertEqual(parsed["pending_references"], [])

    def test_explicit_symbols_references_and_issue_notes_remain_supported(self):
        note = "Runner.run delegates recovery to helper without translating errors here."
        self.payload.update(symbols=["Runner.run"], issues={"error_recovery": note}, references=[{
            "target_path": "pkg/helper.py", "symbol": "helper", "evidence": "helper()",
        }])
        parsed = self.parse()
        self.assertEqual(parsed["evidence"]["error_recovery"], note)
        self.assertEqual(parsed["evidence"]["readability"], self.payload["summary"])
        self.assertEqual(parsed["code_map"]["public_symbols"], ["Runner.run"])

    def test_invalid_indexes_duplicates_and_fabricated_symbols_remain_rejected(self):
        for field, values in (("symbols", [-1]), ("symbols", [2]), ("symbols", [True]),
                              ("symbols", [1, "Runner.run"]), ("symbols", ["Runner.missing"]),
                              ("references", [1]), ("references", [False]),
                              ("references", [0, 0]), ("pending", [-1])):
            with self.subTest(field=field, values=values):
                payload = {**self.payload, field: values}
                with self.assertRaises(review.FileReviewError):
                    self.parse(payload)

    def test_all_chunks_required_and_each_note_must_cite_its_actual_span(self):
        content = b"# Opening segment describes calculation.\n" * 1500
        source = self.source_for(content)
        payload = compact_result(source, content)
        parsed = self.parse(payload, source=source, content=content)
        chunks = parsed["evidence"]["chunk_reviews"]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[-1]["end"], len(content))
        for index, item in enumerate(chunks):
            span = content[item["start"]:item["end"]]
            self.assertEqual(item["index"], index)
            self.assertEqual(item["chunk_hash"], hashlib.sha256(span).hexdigest())
        for notes in (payload["chunks"][:1], payload["chunks"] + [payload["chunks"][0]],
                      [payload["chunks"][0], "A fabricated statement without any original citation."]):
            with self.subTest(notes=len(notes)):
                with self.assertRaises(review.FileReviewError):
                    self.parse({**payload, "chunks": notes}, source=source, content=content)

    def test_missing_unknown_or_invalid_fields_cannot_be_accepted(self):
        for key in compact.REQUIRED_FIELDS:
            payload = dict(self.payload)
            del payload[key]
            with self.subTest(missing=key), self.assertRaises(review.FileReviewError):
                self.parse(payload)
        for changes in ({"version": True}, {"version": 3}, {"extra": []},
                        {"issues": {"extra": "A sufficiently long problem report."}},
                        {"issues": {"readability": "short"}}, {"issues": []},
                        {"summary": "x" * 1201}, {"symbols": [0] * 33},
                        {"chunks": ["x" * 1201]}, {"chunks": [False]},
                        {"path": "pkg/other.py"}, {"content_hash": "b" * 64},
                        {"source_revision": "b" * 40}, {"mode": "100755"}):
            with self.subTest(changes=changes), self.assertRaises(review.FileReviewError):
                self.parse({**self.payload, **changes})
        with self.assertRaisesRegex(review.FileReviewError, "원본 지문"):
            self.parse(content=self.content + b"# changed\n")

    def test_empty_whitespace_binary_rendering_and_field_boundary(self):
        for content in (b"", b" \n\t", b"\xff arbitrary non UTF8 source\n", b"x\n"):
            source = self.source_for(content)
            payload = compact_result(source, content)
            payload["summary"] = "x" * 1200
            parsed = self.parse(payload, source=source, content=content)
            self.assertEqual(len(parsed["evidence"]["chunk_reviews"]), bool(content))
            self.assertEqual(parsed["evidence"]["excerpts"], [] if not content.strip()
                             else [content.decode("utf-8", "backslashreplace").strip()])

    def test_relative_package_submodule_and_external_import_candidates(self):
        content = (b"from ..helper import calculate\nfrom . import sibling\n"
                   b"import pkg.helper as helper\nfrom external import resource\n")
        source = self.source_for(content, "pkg/nested/review.py")
        facts = compact.source_facts(source, content, (*self.paths, "pkg/nested/sibling.py"),
                                     chunk_bytes=review.MAX_EMBEDDED_SOURCE_BYTES)
        self.assertEqual([(item["target_path"], item["symbol"]) for item in facts["references"]], [
            ("pkg/helper.py", "calculate"), ("pkg/nested/sibling.py", "sibling"),
            ("pkg/helper.py", "pkg.helper"), (None, "resource"),
        ])
        for item in facts["references"]:
            self.assertIn(item["evidence"], content.decode())
        for path, code in (("module.txt", b"import helper\n"), ("broken.py", b"def !\n")):
            facts = compact.source_facts(self.source_for(code, path), code,
                                         chunk_bytes=review.MAX_EMBEDDED_SOURCE_BYTES)
            self.assertEqual(facts["symbols"], [])
            self.assertEqual(facts["references"], [])

    def test_prompt_shares_contract_and_supplies_manager_facts_without_skipping_source(self):
        first = review.review_prompt(self.source, self.content, compact=True, source_paths=self.paths)
        next_prompt = review.review_prompt(self.source, self.content, compact=True,
                                          source_paths=self.paths, include_contract=False)
        self.assertIn(self.content.decode(), first)
        self.assertIn("Runner.run", first)
        self.assertIn('"target_path":"pkg/helper.py"', first)
        self.assertIn("모두 평가", first)
        self.assertNotIn(compact.result_instructions(), next_prompt)
        self.assertIn(self.source.content_hash, next_prompt)
        self.assertIn(self.content.decode(), next_prompt)
        large = b"# source coverage\n" * 4000
        prompt = review.review_prompt(self.source_for(large), large, compact=True)
        self.assertIn(self.source.revision + ":" + self.source.path, prompt)
        self.assertNotIn("원본 시작", prompt)
        self.assertIn('"index":1', prompt)

    def test_compact_receipt_persists_as_legacy_and_reduces_generated_output(self):
        parsed = self.parse()
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('repo')")
        repository = review.ProvisionalReviewRepository(connection)
        repository.start("repo", 1, self.source)
        repository.complete("repo", 1, self.source, parsed)
        raw = connection.execute("SELECT result_json FROM provisional_file_reviews").fetchone()[0]
        self.assertNotIn("version", json.loads(raw))
        self.assertEqual(review.parse_review(protocol.RESULT_PREFIX + raw, self.source, self.content), parsed)
        self.assertLess(len(json.dumps(self.payload)), len(json.dumps(parsed)) / 2)


if __name__ == "__main__":
    unittest.main()
