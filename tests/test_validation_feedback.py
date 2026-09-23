import unittest

import loop_manager as lm
from validation_feedback import (MAX_DIAGNOSTIC, is_validation_error,
                                 normalize_feedback, redact_secrets, render_delta)


class ValidationFeedbackTests(unittest.TestCase):
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
