from tests.support.recovery import BoundedRecoveryFixture
import tempfile
import unittest
import loop_manager
from tests.support.worker import dummy_worker_sessions


class ReviewProtocolRecoveryTest(BoundedRecoveryFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_review_protocol_error_is_corrected_to_pass_in_same_session(self):
        invalid = "blocking issue without an identifier\nVERDICT: FAIL"
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory,
                (invalid, "VERDICT: PASS"),
                publish=True,
            )

        self.assertEqual(data["result"].kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(data["result"].exit_code, 0)
        self.assertEqual(data["roles"], ["worker", "reviewer", "reviewer"])
        self.assertEqual(data["reviewer_sessions"], [None, "reviewer-session"])
        self.assertEqual(data["verify_calls"], 1)
        self.assertEqual(data["transitions"], [])
        self.assertIsNone(data["project"]["phase"])
        self.assertEqual(data["plan"]["status"], "PR_OPEN")
        self.assertIsNone(data["plan"]["last_error"])
        self.assertIn(
            "Reviewer FAIL 결과에 유효한 ISSUE_ID가 없습니다",
            data["reviewer_prompts"][1],
        )
        self.assertIn(invalid, data["reviewer_prompts"][1])

    @dummy_worker_sessions
    def test_review_protocol_error_is_corrected_to_fail_without_worker_retry(self):
        invalid = "blocking issue without an identifier\nVERDICT: FAIL"
        valid_fail = (
            "ISSUE_ID: src/app.py::value::incorrect\n"
            "value remains incorrect\nVERDICT: FAIL"
        )
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory,
                (invalid, valid_fail, "VERDICT: PASS"),
                publish=True,
            )

        self.assertEqual(data["result"].kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(data["result"].exit_code, 0)
        self.assertEqual(
            data["roles"],
            ["worker", "reviewer", "reviewer", "worker", "reviewer"],
        )
        self.assertEqual(data["verify_calls"], 2)
        self.assertEqual(
            [
                (stage, occurrences, blocked)
                for stage, _fingerprint, occurrences, blocked in data["transitions"]
            ],
            [("review", 1, False)],
        )
        self.assertIn(invalid, data["reviewer_prompts"][1])
        self.assertNotIn(
            "직전 Reviewer 결과가 아래 프로토콜 검증에 실패",
            data["reviewer_prompts"][2],
        )
