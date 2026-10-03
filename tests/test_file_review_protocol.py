"""Pure unit regressions for the file-review response and prompt contract."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loop_manager_internal.maintenance import file_review_protocol as protocol
from loop_manager_internal.maintenance import maintenance_file_review as review
from loop_manager_internal.maintenance.maintenance_file_inventory import TrackedSource
from loop_manager_internal.storage import maintenance_file_ledger as ledger
from tests.support.maintenance import result_for


class FileReviewProtocolTest(unittest.TestCase):
    def setUp(self):
        self.content = protocol.EXAMPLE_CONTENT
        self.source = self.source_for(self.content)
        self.payload = protocol.result_example()

    def source_for(self, content):
        return TrackedSource("example.py", hashlib.sha256(content).hexdigest(),
                             "100644", "a" * 40, "source")

    def parse(self, payload):
        return review.parse_review(protocol.RESULT_PREFIX + json.dumps(payload),
                                   self.source, self.content)

    def test_whitespace_multiline_and_fences_preserve_evidence(self):
        compact = json.dumps(self.payload)
        pretty = json.dumps(self.payload, indent=2)
        for output in (
            protocol.RESULT_PREFIX + compact,
            protocol.RESULT_MARKER + compact,
            protocol.RESULT_MARKER + "\n" + compact,
            " \t" + protocol.RESULT_MARKER + "\r\n\t" + pretty,
            "```json\n" + protocol.RESULT_PREFIX + pretty + "\n```",
            protocol.RESULT_MARKER + "\n```json\n" + pretty + "\n```",
        ):
            with self.subTest(output=output[:70]):
                self.assertEqual(review.parse_review(output, self.source, self.content),
                                 self.payload)

    def test_missing_or_multiple_tagged_results_are_rejected(self):
        line = protocol.RESULT_PREFIX + json.dumps(self.payload)
        for output in ("", json.dumps(self.payload), line + "\n" + line,
                       protocol.RESULT_MARKER + "\n" + line):
            with self.subTest(output=output[:70]):
                with self.assertRaisesRegex(review.FileReviewError, "정확히 하나"):
                    review.parse_review(output, self.source, self.content)

    def test_literal_marker_in_json_string_is_not_an_extra_result(self):
        self.payload["evidence"]["change_context"] += "\nFILE_REVIEW_RESULT: is a literal."
        self.assertEqual(self.parse(self.payload), self.payload)

    def test_extra_json_and_trailing_explanations_are_not_silently_discarded(self):
        line = protocol.RESULT_PREFIX + json.dumps(self.payload)
        for suffix in (" {}", "\n{}", "\n설명을 더 붙입니다.", "\n```\n{}"):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(review.FileReviewError, "JSON 뒤"):
                    review.parse_review(line + suffix, self.source, self.content)

    def test_invalid_json_and_non_object_results_are_rejected(self):
        for value, message in (("{", "JSON이 유효하지"), ("{'path': 1}", "JSON이 유효하지"),
                               ("[]", "JSON object"), ("null", "JSON object"),
                               ('{"value":NaN}', "허용되지 않은 값")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(review.FileReviewError, message):
                    protocol.decode_result(protocol.RESULT_PREFIX + value)

    def test_duplicate_keys_at_any_depth_are_rejected(self):
        for value in ('{"path":"first", "path":"second"}',
                      '{"evidence":{"change_context":"first", "change_context":"second"}}'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(review.FileReviewError, "중복 key"):
                    protocol.decode_result(protocol.RESULT_PREFIX + value)

    def test_missing_and_extra_fields_are_named_at_their_object_boundary(self):
        for boundary in ("FILE_REVIEW_RESULT", "evidence", "code_map", "chunk"):
            with self.subTest(boundary=boundary):
                payload = copy.deepcopy(self.payload)
                if boundary == "FILE_REVIEW_RESULT":
                    target, missing, extra = payload, "pending_references", "root_placement_inventory"
                elif boundary == "chunk":
                    target, missing, extra = payload["evidence"]["chunk_reviews"][0], "review", "root_placement_decision"
                else:
                    target = payload[boundary]
                    missing = "change_context" if boundary == "evidence" else "responsibility"
                    extra = "root_placement_inventory"
                del target[missing]
                target[extra] = []
                with self.assertRaises(review.FileReviewError) as raised:
                    self.parse(payload)
                self.assertIn("누락 필드: " + missing, str(raised.exception))
                self.assertIn("추가 필드: " + extra, str(raised.exception))

    def test_field_length_errors_distinguish_type_minimum_and_maximum(self):
        for value, message in ((None, "현재 NoneType"), ("   ", "최소 12자"),
                               ("x" * 1985, "1985자, 최대 1200자")):
            with self.subTest(value_type=type(value).__name__):
                self.payload["evidence"]["change_context"] = value
                with self.assertRaisesRegex(review.FileReviewError, message) as raised:
                    self.parse(self.payload)
                self.assertIn("evidence.change_context", str(raised.exception))
        for size in (12, protocol.MAX_FIELD_CHARS):
            self.payload["evidence"]["change_context"] = "x" * size
            self.parse(self.payload)

    def test_list_bound_and_chunk_review_bound_are_preserved(self):
        self.payload["code_map"]["public_symbols"] = ["calculate"] * (protocol.MAX_LIST_ITEMS + 1)
        with self.assertRaisesRegex(review.FileReviewError, "33개, 허용 0~32개"):
            self.parse(self.payload)
        self.payload["code_map"]["public_symbols"] = []
        self.payload["evidence"]["chunk_reviews"][0]["review"] = "calculate"
        with self.assertRaisesRegex(review.FileReviewError, "최소 20자"):
            self.parse(self.payload)

    def test_format_tolerance_does_not_accept_forged_source_or_chunk_evidence(self):
        mutations = (
            lambda p: p.update(source_revision="c" * 40),
            lambda p: p.update(content_hash="c" * 64),
            lambda p: p["evidence"].update(excerpts=["not actual source"]),
            lambda p: p["evidence"].update(chunk_reviews=[]),
            lambda p: p["evidence"]["chunk_reviews"][0].update(chunk_hash="c" * 64),
            lambda p: p["evidence"]["chunk_reviews"][0].update(index=True),
            lambda p: p["evidence"]["chunk_reviews"][0].update(opening_excerpt="invented source"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                payload = copy.deepcopy(self.payload)
                mutate(payload)
                with self.assertRaises(review.FileReviewError):
                    review.parse_review(protocol.RESULT_MARKER + "\n" + json.dumps(payload, indent=2),
                                        self.source, self.content)

    def test_reference_candidate_shapes_and_actual_source_evidence_are_preserved(self):
        self.payload["pending_references"] = [{"target_path": None, "symbol": "calculate",
                                               "evidence": "calculate()"}]
        self.parse(self.payload)
        self.payload["pending_references"][0]["invented"] = True
        with self.assertRaisesRegex(review.FileReviewError, "추가 필드: invented"):
            self.parse(self.payload)
        del self.payload["pending_references"][0]["invented"]
        self.payload["pending_references"][0]["evidence"] = "missing()"
        with self.assertRaisesRegex(review.FileReviewError, "원본 인용"):
            self.parse(self.payload)

    def test_large_result_has_all_chunks_without_a_total_response_limit(self):
        content = b"def calculate(): return 1\n" * 330000
        source = self.source_for(content)
        payload = result_for(source, "def calculate(): return 1", content)
        raw = protocol.RESULT_PREFIX + json.dumps(payload)
        self.assertGreater(len(payload["evidence"]["chunk_reviews"]), protocol.MAX_LIST_ITEMS)
        self.assertGreater(len(raw), 48000)
        self.assertEqual(review.parse_review(raw, source, content), payload)

    def test_non_utf8_evidence_stays_bound_to_rendered_bytes(self):
        content = b"def calculate(): return '\xff'\n"
        source = self.source_for(content)
        payload = result_for(source, "def calculate(): return '\\xff'", content)
        self.assertEqual(review.parse_review(protocol.RESULT_PREFIX + json.dumps(payload),
                                            source, content), payload)

    def test_prompt_examples_are_parseable_and_root_contract_is_separate(self):
        for root in (False, True):
            with self.subTest(root=root):
                content = protocol.EXAMPLE_TREE if root else self.content
                example = protocol.result_example(root=root)
                source = TrackedSource(example["path"], example["content_hash"], example["mode"],
                                       example["source_revision"], ledger.ROOT_REVIEW_REASON if root else "source")
                prompt = review.review_prompt(source, content)
                sample = prompt.split("```text\n", 1)[1].split("\n```", 1)[0]
                parsed = review.parse_review(sample, source, content)
                self.assertEqual(parsed, example)
                self.assertIn("필드를 생략하거나 추가하지 마라", prompt)
                self.assertIn("1200자", prompt)
                if root:
                    self.assertNotIn("evidence", parsed)
                    with patch.object(ledger, "root_placement_inventory", return_value=(SimpleNamespace(path="example.py"),)), \
                         patch.object(ledger, "_git", return_value=content):
                        inventory = SimpleNamespace(revision=source.revision, classifications=())
                        self.assertEqual(ledger.validate_root_reviews(Path("/unused"), inventory,
                                                                     parsed["root_reviews"]),
                                         tuple(example["root_reviews"]))
                else:
                    self.assertNotIn("root_reviews", parsed)
                    self.assertIn("root inventory는 문맥", prompt)
                    self.assertIn("별도 root 조사 단위", prompt)
                    self.assertIn("최소 20자", prompt)
                    self.assertIn("최대 32개", prompt)

    def test_root_result_uses_same_decoder_without_bypassing_identity(self):
        payload = protocol.result_example(root=True)
        source = TrackedSource(".", payload["content_hash"], "040000", "a" * 40, ledger.ROOT_REVIEW_REASON)
        self.assertEqual(review.parse_review(protocol.RESULT_MARKER + "\n" + json.dumps(payload),
                                            source, protocol.EXAMPLE_TREE), payload)
        payload["root_placement_inventory"] = []
        with self.assertRaisesRegex(review.FileReviewError, "추가 필드: root_placement_inventory"):
            review.parse_review(protocol.RESULT_PREFIX + json.dumps(payload), source, protocol.EXAMPLE_TREE)
        del payload["root_placement_inventory"]
        payload["source_revision"] = "c" * 40
        with self.assertRaisesRegex(review.FileReviewError, "identity"):
            review.parse_review(protocol.RESULT_PREFIX + json.dumps(payload), source, protocol.EXAMPLE_TREE)

    def test_root_item_diagnostics_use_shared_fields_and_limits(self):
        example = protocol.result_example(root=True)
        inventory = SimpleNamespace(revision=example["source_revision"], classifications=())
        with patch.object(ledger, "root_placement_inventory", return_value=(SimpleNamespace(path="example.py"),)), \
             patch.object(ledger, "_git", return_value=protocol.EXAMPLE_TREE):
            for value, message in ((None, "현재 NoneType"), ("x", "최소 12자"),
                                   ("x" * 1201, "1201자, 최대 1200자")):
                with self.subTest(value_type=type(value).__name__):
                    results = copy.deepcopy(example["root_reviews"])
                    results[0]["ownership"] = value
                    with self.assertRaisesRegex(ledger.FileReviewLedgerError, message):
                        ledger.validate_root_reviews(Path("/unused"), inventory, results)
            results = copy.deepcopy(example["root_reviews"])
            results[0]["ownership"] = " " + "x" * 1200 + " "
            ledger.validate_root_reviews(Path("/unused"), inventory, results)
            del results[0]["ownership"]
            results[0]["invented"] = True
            with self.assertRaisesRegex(ledger.FileReviewLedgerError,
                                        "누락 필드: ownership; 추가 필드: invented"):
                ledger.validate_root_reviews(Path("/unused"), inventory, results)

    def test_root_reference_source_proof_and_field_contract_are_preserved(self):
        example = protocol.result_example(root=True)
        inventory = SimpleNamespace(revision=example["source_revision"], classifications=())
        with patch.object(ledger, "root_placement_inventory", return_value=(SimpleNamespace(path="example.py"),)), \
             patch.object(ledger, "_git", return_value=protocol.EXAMPLE_TREE):
            for mutation, message in (
                ({"excerpt": "invented tree entry"}, "원본과 다릅니다"),
                ({"extra_field": "invented"}, "추가 필드: extra_field"),
            ):
                with self.subTest(mutation=mutation):
                    results = copy.deepcopy(example["root_reviews"])
                    results[0]["references"][0].update(mutation)
                    with self.assertRaisesRegex(ledger.FileReviewLedgerError, message):
                        ledger.validate_root_reviews(Path("/unused"), inventory, results)
