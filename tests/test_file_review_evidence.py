import hashlib
import json
import unittest

from loop_manager_internal.maintenance import maintenance_file_review as review
from loop_manager_internal.maintenance.file_review_evidence import (
    review_cites_source,
    unverified_symbols,
)
from loop_manager_internal.maintenance.maintenance_file_inventory import TrackedSource
from tests.support.maintenance import result_for


PYTHON_SOURCE = b'''class Runner:
    @property
    def result(self):
        return 1

    async def run(self):
        return self.result

    class Nested:
        def probe(self):
            return True

class Other:
    def stop(self):
        pass
'''


class SourceSymbolEvidenceTest(unittest.TestCase):
    def test_qualified_definitions_include_properties_async_and_nested_classes(self):
        symbols = ("Runner.result", "Runner.run", "Runner.Nested.probe", "Other.stop")
        for path in ("src/runner.py", "src/runner.pyi"):
            with self.subTest(path=path):
                self.assertEqual(unverified_symbols(
                    symbols, path, PYTHON_SOURCE, PYTHON_SOURCE.decode(),
                ), ())

    def test_names_from_different_classes_do_not_establish_a_definition(self):
        symbols = ("Runner.stop", "Other.run", "module.Runner.run")
        self.assertEqual(unverified_symbols(
            symbols, "src/runner.py", PYTHON_SOURCE, PYTHON_SOURCE.decode(),
        ), symbols)

    def test_unsupported_or_unparseable_source_keeps_literal_validation(self):
        for path, content in (("runner.txt", PYTHON_SOURCE),
                              ("runner.py", b"class Runner:\n    def run(\n")):
            with self.subTest(path=path):
                self.assertEqual(unverified_symbols(
                    ("Runner", "Runner.run"), path, content, content.decode(),
                ), ("Runner.run",))


class SourceCitationEvidenceTest(unittest.TestCase):
    def test_partial_long_excerpt_establishes_source_citation(self):
        anchor = "Provider profile boundary: codex_model_efforts: Callable validates roles."
        note = "middle_excerpt의 codex_model_efforts: Callable 선언은 역할 해석 경계를 보여준다."
        self.assertTrue(review_cites_source(note, (anchor,)))

    def test_citation_ignores_line_wrapping_whitespace(self):
        self.assertTrue(review_cites_source(
            "The codex_model_efforts: Callable declaration injects role resolution.",
            ("codex_model_efforts:\n    Callable",),
        ))

    def test_field_labels_and_short_common_words_are_not_source_evidence(self):
        anchors = ("def calculate_value(): return result",)
        for note in ("opening_excerpt establishes the workflow boundary.",
                     "The result is returned to the caller.",
                     "An unrelated namespace supplies the validation boundary."):
            with self.subTest(note=note):
                self.assertFalse(review_cites_source(note, anchors))

    def test_short_source_requires_whole_excerpt_and_blank_source_is_preserved(self):
        self.assertTrue(review_cites_source("The x = 1 assignment stores the input.", ("x = 1",)))
        self.assertFalse(review_cites_source("Only x exists here.", ("x = 1",)))
        self.assertTrue(review_cites_source("A whitespace-only package marker. \n\t", (" \n\t",)))


class FileReviewEvidenceContractTest(unittest.TestCase):
    def payload(self):
        source = TrackedSource(
            "src/runner.py", hashlib.sha256(PYTHON_SOURCE).hexdigest(), "100644",
            "a" * 40, "source",
        )
        payload = result_for(source, "class Runner:", PYTHON_SOURCE)
        payload["code_map"]["entry_points"] = ["Runner.run"]
        payload["code_map"]["public_symbols"] = ["Runner.result", "Other.stop"]
        chunk = payload["evidence"]["chunk_reviews"][0]
        chunk["opening_excerpt"] = PYTHON_SOURCE[:80].decode()
        chunk["review"] = "The class Runner: definition owns the asynchronous workflow."
        return source, payload

    def parse(self, source, payload):
        return review.parse_review(
            review.RESULT_PREFIX + json.dumps(payload), source, PYTHON_SOURCE,
        )

    def test_parser_accepts_qualified_definitions_and_partial_verified_citation(self):
        source, payload = self.payload()
        parsed = self.parse(source, payload)
        self.assertEqual(parsed["code_map"]["entry_points"], ["Runner.run"])

    def test_symbol_error_identifies_the_invalid_field_and_symbol(self):
        source, payload = self.payload()
        payload["code_map"]["entry_points"] = ["Runner.stop"]
        with self.assertRaisesRegex(review.FileReviewError, r"code_map\.entry_points.*Runner.stop"):
            self.parse(source, payload)

    def test_citation_error_identifies_the_segment_and_recovery_format(self):
        source, payload = self.payload()
        payload["evidence"]["chunk_reviews"][0]["review"] = (
            "The opening_excerpt establishes ownership without citing any source."
        )
        with self.assertRaisesRegex(review.FileReviewError, r"chunk_reviews\[0\]\.review.*12"):
            self.parse(source, payload)

    def test_citation_change_preserves_hash_excerpt_and_coverage_checks(self):
        for forged in ("hash", "excerpt", "coverage", "identity"):
            with self.subTest(forged=forged):
                source, payload = self.payload()
                chunks = payload["evidence"]["chunk_reviews"]
                if forged == "hash":
                    chunks[0]["chunk_hash"] = "0" * 64
                elif forged == "excerpt":
                    chunks[0]["opening_excerpt"] = "An invented source fragment."
                elif forged == "coverage":
                    chunks.clear()
                else:
                    payload["source_revision"] = "b" * 40
                with self.assertRaises(review.FileReviewError):
                    self.parse(source, payload)


if __name__ == "__main__":
    unittest.main()
