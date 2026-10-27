import subprocess
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from tests.support.guidance import git_result
from tests.support.git import git_test_environment


class CommonPlansScopeTest(unittest.TestCase):
    def test_common_plans_scope_requires_direct_acceptance_criterion(self):
        repo = Path("/fixture/repository")
        incidental_path = "docs/plans/[G-030] Boundary.md"
        explicit_path = "docs/plans/[G-031] Contract.md"
        conjunctive_path = "docs/plans/[G-032] Maintenance Contract.md"
        plan_sha = "a" * 40
        current_revision_sha = "b" * 40
        shown = mock.Mock(side_effect=[
            subprocess.CompletedProcess(
                [], 0,
                "## Acceptance Criteria\n\n"
                "- 후속 계획의 공통 `docs/PLANS.md` 변경을 구분하는 경계를 제공한다.\n",
            ),
            subprocess.CompletedProcess(
                [], 0,
                "## Acceptance Criteria\n\n"
                "- 공통 `docs/PLANS.md`에 guidance 계약을 문서화한다.\n",
            ),
            subprocess.CompletedProcess(
                [], 0,
                "## Acceptance Criteria\n\n"
                "- 공통 `docs/PLANS.md`와 Planner prompt는 유지보수 예외를 설명한다.\n",
            ),
            subprocess.CompletedProcess(
                [], 0,
                "## Acceptance Criteria\n\n"
                "- 공통 `docs/PLANS.md`에 guidance 계약을 문서화한다.\n",
            ),
            subprocess.CompletedProcess([], 128, "fatal: unknown revision\n"),
        ])
        repository_type = mock.Mock(side_effect=[True, True, True, True, True, False])

        with mock.patch.object(loop_manager, "git", shown), \
             mock.patch.object(loop_manager, "is_loop_manager_repository", repository_type), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected Git subprocess")) as process:
            self.assertFalse(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": incidental_path}))
            self.assertTrue(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": explicit_path}))
            self.assertTrue(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": conjunctive_path}))
            self.assertTrue(loop_manager._published_plan_allows_common_plans(
                repo, {"current_revision_sha": current_revision_sha,
                       "plan_sha": "0" * 40, "path": explicit_path}))
            self.assertFalse(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": explicit_path}))
            self.assertFalse(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": explicit_path}))

        self.assertEqual(repository_type.call_args_list, [mock.call(repo)] * 6)
        self.assertEqual(shown.call_args_list, [
            mock.call(repo, "show", f"{plan_sha}:{incidental_path}", check=False),
            mock.call(repo, "show", f"{plan_sha}:{explicit_path}", check=False),
            mock.call(repo, "show", f"{plan_sha}:{conjunctive_path}", check=False),
            mock.call(repo, "show", f"{current_revision_sha}:{explicit_path}", check=False),
            mock.call(repo, "show", f"{plan_sha}:{explicit_path}", check=False),
        ])
        process.assert_not_called()


class GuidanceGitAdapterTest(unittest.TestCase):
    def test_git_result_reuses_isolated_environment_and_preserves_result_contract(self):
        repo = Path("/fixture/repository")
        result = subprocess.CompletedProcess(["git"], 1, "fatal: missing revision\n")
        with mock.patch("tests.support.guidance.subprocess.run", return_value=result) as run:
            self.assertIs(git_result(repo, "show", "missing:path", check=False), result)
        run.assert_called_once_with(
            ["git", "-C", str(repo), "show", "missing:path"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=git_test_environment(),
        )

    def test_git_result_preserves_checked_subprocess_error(self):
        error = subprocess.CalledProcessError(128, ["git"], output="fatal: failure\n")
        with mock.patch("tests.support.guidance.subprocess.run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                git_result(Path("/fixture/repository"), "show", "missing:path")
        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
