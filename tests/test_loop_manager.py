import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
import loop_manager


class PublicFacadeTest(unittest.TestCase):
    def test_codex_uses_mutually_exclusive_sandbox_options(self):
        for read_only, mode in ((False, "workspace-write"), (True, "read-only")):
            with self.subTest(read_only=read_only):
                args = loop_manager.codex_sandbox_arguments(read_only)
                self.assertEqual(args[:2], ["--sandbox", mode])
                self.assertNotIn("--approve-for-me", args)
                self.assertIn('approvals_reviewer="auto_review"', args)
                self.assertTrue(any("sandbox_approval=false" in item and
                                    "request_permissions=false" in item for item in args))

    def test_worker_and_reviewer_prompts_define_test_execution_boundary(self):
        planner = loop_manager.planner_prompt(None)
        worker = loop_manager.worker_prompt("docs/plans/[G-001] Worker Boundary.md", None)
        reviewer = loop_manager.reviewer_prompt("main")
        no_test_reviewer = loop_manager.reviewer_prompt(
            "main", "실행 가능한 관련 targeted unit test가 없습니다: 문서 변경만 포함합니다.",
        )

        self.assertNotIn("targeted: PASS", reviewer)
        self.assertNotIn("verify: PASS", reviewer)
        self.assertIn("문서 변경만 포함합니다", no_test_reviewer)
        self.assertIn("테스트 command와 scripts/verify를 실행하지 말고", reviewer)
        self.assertIn("ISSUE_ID: relative/path::stable-symbol-or-test::category-slug", reviewer)
        self.assertIn("이전과 정확히 같은 ID", reviewer)
        self.assertIn("COMMIT_TITLE:", worker)
        self.assertIn("PR_SUMMARY:", worker)
        self.assertIn("TARGETED_TESTS:", worker)
        for prompt in (planner, worker, reviewer):
            with self.subTest(prompt=prompt[:32]):
                self.assertNotIn("기존 구현·공유 helper·관련 테스트와 표준 도구", prompt)
        self.assertIsNone(
            loop_manager.reviewer_targeted_test_reason("targeted test: PASS")
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(
                "실행 가능한 관련 targeted unit test가 없습니다: 문서 변경만 포함합니다."
            ),
            "실행 가능한 관련 targeted unit test가 없습니다: 문서 변경만 포함합니다.",
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(
                "문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다."
            ),
            "문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다.",
        )


class LauncherArgumentsTest(unittest.TestCase):
    def test_main_rejects_unknown_options_before_state_creation(self):
        state_dir = Path("state")
        with mock.patch(
            "sys.argv",
            [
                "loop_manager.py",
                "init",
                "--future-option=value",
            ],
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ) as state_directory, mock.patch(
            "loop_manager.process_lock"
        ) as process_lock, mock.patch(
            "loop_manager.connect_database"
        ) as connect_database, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                loop_manager.main()

        self.assertEqual(raised.exception.code, 2)
        state_directory.assert_not_called()
        process_lock.assert_not_called()
        connect_database.assert_not_called()
