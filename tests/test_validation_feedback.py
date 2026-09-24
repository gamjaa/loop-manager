import unittest

import loop_manager as lm
from validation_feedback import (MAX_DIAGNOSTIC, format_verify_failure,
                                 is_validation_error, normalize_feedback,
                                 redact_secrets, render_delta)


class ValidationFeedbackTests(unittest.TestCase):
    def test_duplicate_review_ids_combine_diagnostics_deterministically(self):
        sections = ["ISSUE_ID: File.py::Func::Bug\nFix A.",
                    "ISSUE_ID: file.py::func::bug\nFix B.",
                    "ISSUE_ID: File.py::Func::Bug\nFix A."]
        raw = "\n".join(sections) + "\nVERDICT: FAIL"
        items = normalize_feedback("review", raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].identity, "review:file.py::func::bug")
        self.assertEqual(items[0].diagnostic, "Fix A.\n\nFix B.")
        self.assertEqual(items, normalize_feedback(
            "review", "\n".join(reversed(sections)) + "\nVERDICT: FAIL"))
        self.assertEqual(len(lm.validation_failure_fingerprints("review", raw)), 1)

    def test_review_issues_are_individually_bounded_and_sorted(self):
        raw = ("ISSUE_ID: z.py::thing::bug\nFix Z.\n"
               "ISSUE_ID: a.py::test::coverage\n" + "x" * 3000 +
               "\nVERDICT: FAIL")
        items = normalize_feedback("review", raw)
        self.assertEqual([item.identity for item in items], [
            "review:a.py::test::coverage", "review:z.py::thing::bug",
        ])
        self.assertLessEqual(len(items[0].diagnostic), MAX_DIAGNOSTIC)
        rendered = render_delta([
            {"stage": item.stage, "identity": item.identity, "occurrence": 1,
             "revision": 1, "diagnostic": item.diagnostic}
            for item in items
        ])
        self.assertNotIn("VERDICT: FAIL", rendered)
        self.assertIn("Fix Z.", rendered)

    def test_verify_uses_stable_fingerprint_and_bounded_diagnostic(self):
        item, = normalize_feedback("verify", "secret prelude\n" + "a" * 3000,
                                   (("fixed-digest", "normalized"),))
        self.assertEqual(item.identity, "verify:fixed-digest")
        self.assertLessEqual(len(item.diagnostic), MAX_DIAGNOSTIC)
        self.assertNotIn("secret prelude", item.diagnostic)

    def test_verify_formatter_extracts_early_failure_and_omits_success_log(self):
        raw = (
            "FAILED tests/test_widget.py::test_saves - AssertionError: saved value\n"
            "E       assert 'draft' == 'published'\n"
            "================= 1 failed, 42 passed in 1.2s =================\n"
            + "PASS completed check\n" * 800
        )
        item, = normalize_feedback("verify", raw, (("digest", "normalized"),),
                                   exit_code=1)
        self.assertIn("tests/test_widget.py::test_saves", item.diagnostic)
        self.assertIn("assert 'draft' == 'published'", item.diagnostic)
        self.assertIn("1 failed, 42 passed", item.diagnostic)
        self.assertNotIn("completed check", item.diagnostic)
        self.assertIn("verify exit code: 1", item.diagnostic)

    def test_verify_formatter_keeps_multiple_failures_and_marks_omission(self):
        raw = (
            "FAIL: test_alpha (tests.test_alpha.Alpha)\n"
            "AssertionError: " + "a" * 1000 + "\n"
            "FAIL: test_beta (tests.test_beta.Beta)\n"
            "AssertionError: " + "b" * 1000 + "\n"
            "FAILED (failures=2)\n"
        )
        diagnostic = format_verify_failure(raw, exit_code=1)
        self.assertIn("test_alpha", diagnostic)
        self.assertIn("test_beta", diagnostic)
        self.assertIn("[diagnostic truncated]", diagnostic)
        self.assertLessEqual(len(diagnostic), MAX_DIAGNOSTIC)

    def test_verify_formatter_falls_back_to_bounded_tail_without_guessing(self):
        raw = "unrecognized output\n" + "x" * 3000 + "\nAPI_KEY=private-value"
        diagnostic = format_verify_failure(raw, exit_code=17)
        self.assertIn("verify exit code: 17", diagnostic)
        self.assertIn("[diagnostic truncated]", diagnostic)
        self.assertNotIn("private-value", diagnostic)
        self.assertNotIn("FAILED:", diagnostic)

    def test_verify_timeout_only_reports_observed_timeout_information(self):
        diagnostic = format_verify_failure(
            "progress\n전체 verify 시간 초과: verify_timeout_seconds=2\nsecret=private-value",
            exit_code=-9, timed_out=True,
        )
        self.assertIn("전체 verify 시간 초과", diagnostic)
        self.assertIn("verify exit code: -9", diagnostic)
        self.assertNotIn("progress", diagnostic)
        self.assertNotIn("private-value", diagnostic)

    def test_validation_error_is_not_repeated_in_non_validation_channel(self):
        self.assertTrue(is_validation_error("전체 verify 실패:\nraw", "raw", None))
        self.assertTrue(is_validation_error("Review 실패:\nraw", None, "raw"))
        self.assertFalse(is_validation_error(
            "Worker 결과 검증 실패:\ninvalid format", "raw", None,
        ))

    def test_diagnostic_and_recovery_text_redact_common_credentials(self):
        raw = "API_KEY=private-value\nAuthorization: Bearer secret-bearer\nFix assertion"
        item, = normalize_feedback("verify", raw, (("digest", "normalized"),))
        self.assertNotIn("private-value", item.diagnostic)
        self.assertNotIn("secret-bearer", item.diagnostic)
        self.assertIn("Fix assertion", item.diagnostic)
        self.assertNotIn("private-value", redact_secrets(raw))

    def test_worker_recovery_prompt_redacts_previous_result_and_error(self):
        prompt = lm.worker_prompt(
            'docs/plans/[G-040] Plan.md', 'API_KEY=private-error',
            'password: private-result', 'verify:digest occurrence=1 revision=1',
            session_recovery=True,
        )
        self.assertNotIn('private-error', prompt)
        self.assertNotIn('private-result', prompt)
        self.assertIn('verify:digest occurrence=1 revision=1', prompt)
