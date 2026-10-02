from tests.support.scope import AgentScopeFixture
import tempfile
import unittest
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command


class ScopeRoleBoundariesTest(AgentScopeFixture, unittest.TestCase):

    def test_role_boundaries_reject_protected_and_read_only_changes(self):
        scenarios = (
            (
                "planner-product",
                "planner",
                lambda repo, _plan: (repo / "docs/PRODUCT.md").write_text(
                    "# changed\n", encoding="utf-8"
                ),
                None,
                "docs/PRODUCT.md",
            ),
            (
                "worker-requirement",
                "worker",
                lambda repo, _plan: (
                    repo / "docs/requirements/Active.md"
                ).write_text("# changed\n", encoding="utf-8"),
                "docs/plans/[G-001] Scope Safety.md",
                "docs/requirements/Active.md",
            ),
            (
                "reviewer-staged-and-untracked",
                "reviewer",
                lambda repo, _plan: (
                    (repo / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    ),
                    git_command(repo, "add", "src/app.py"),
                    (repo / "review.txt").write_text(
                        "unexpected\n", encoding="utf-8"
                    ),
                ),
                None,
                "review.txt",
            ),
        )
        for name, role_name, mutate, selected_plan, expected in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                mutate(repo, plan_path)
                after = loop_manager.capture_git_snapshot(repo)

                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    expected.replace("[", r"\[").replace("]", r"\]"),
                ):
                    loop_manager.validate_agent_scope(
                        role_name,
                        repo,
                        before,
                        after,
                        selected_plan_path=selected_plan,
                    )

    def test_planner_new_plan_and_worker_selected_plan_are_allowed(self):
        for role_name in ("planner", "worker"):
            with self.subTest(role=role_name), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                selected = plan_path
                if role_name == "planner":
                    write_execplan(repo, "G-002", "New Plan")
                    selected = None
                else:
                    (repo / plan_path).write_text(
                        (repo / plan_path).read_text(encoding="utf-8")
                        + "\n## Progress\n\n- [x] complete\n",
                        encoding="utf-8",
                    )
                after = loop_manager.capture_git_snapshot(repo)

                loop_manager.validate_agent_scope(
                    role_name,
                    repo,
                    before,
                    after,
                    selected_plan_path=selected,
                )

    def test_planner_existing_plan_change_reaches_snapshot_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            (repo / plan_path).write_text("changed\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)

            loop_manager.validate_agent_scope("planner", repo, before, after)
