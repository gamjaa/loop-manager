import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan, make_catalog_root, planner_git_observations
from tests.support.scope import snapshot
from dataclasses import replace
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import dummy_worker_sessions


class PlannerFutureIdeasTest(unittest.TestCase):
    def test_prompt_separates_future_ideas_from_current_execplans(self):
        prompt = loop_manager.planner_prompt(None)

        self.assertIn("docs/FUTURE_IDEAS.md", prompt)
        self.assertIn("같은 계획 주기의 ExecPlan 범위나 미충족 작업의 근거로 사용하지 말고", prompt)
        self.assertIn(
            "PR 전 snapshot의 계획은 같은 ID를 보존한 채",
            prompt,
        )
        self.assertIn(
            "snapshot 밖의 기존 ExecPlan과 PR 등록·MERGED·archive 계획은 수정하지 마라",
            prompt,
        )
        plans_contract = (loop_manager.ROOT / "docs/PLANS.md").read_text(encoding="utf-8")
        self.assertNotIn("Agent Guidance Proposal", plans_contract)
        self.assertNotIn("Agent Guidance User Decision", plans_contract)

    def test_validation_and_scope_separate_future_ideas_from_new_plans(self):
        baseline = {}
        with tempfile.TemporaryDirectory() as directory, planner_git_observations(baseline):
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            future_ideas = repo / "docs/FUTURE_IDEAS.md"
            future_ideas.write_text("# Future Ideas\n", encoding="utf-8")
            baseline.update({existing.relative_to(repo).as_posix(): existing.read_text(encoding='utf-8'),
                             'docs/FUTURE_IDEAS.md': future_ideas.read_text(encoding='utf-8')})
            active_sha = 'base'
            before = snapshot(head=active_sha, branch='loop-manager/planning')

            future_ideas.write_text(
                "# Future Ideas\n\n- Deferred idea\n",
                encoding="utf-8",
            )
            second = write_execplan(repo, "G-002", "Current Work")
            after = replace(before, dirty={
                'docs/FUTURE_IDEAS.md': (('unstaged',), 'changed-ideas'),
                second.relative_to(repo).as_posix(): (('untracked',), 'new-plan'),
            })

            loop_manager.validate_agent_scope("planner", repo, before, after)
            result = loop_manager.validate_planner_result(
                repo,
                active_sha,
                [
                    future_ideas.relative_to(repo).as_posix(),
                    second.relative_to(repo).as_posix(),
                ],
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(
                [plan["id"] for _path, plan in result.plans],
                ["G-002"],
            )
            self.assertEqual(
                result.publication_paths,
                ("docs/FUTURE_IDEAS.md", "docs/plans/[G-002] Current Work.md"),
            )

            second.unlink()
            future_only = loop_manager.validate_planner_result(
                repo,
                active_sha,
                ["docs/FUTURE_IDEAS.md"],
            )
            self.assertIsNotNone(future_only)
            assert future_only is not None
            self.assertEqual(future_only.plans, ())

    def test_validation_rejects_protected_files_and_existing_plan_changes(self):
        baseline = {}
        with tempfile.TemporaryDirectory() as directory, planner_git_observations(baseline):
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            baseline[existing.relative_to(repo).as_posix()] = existing.read_text(encoding='utf-8')
            active_sha = 'base'
            notes = repo / "docs/notes.md"
            notes.write_text("# Planner changed protected notes\n", encoding="utf-8")
            with self.assertRaisesRegex(
                loop_manager.PlannerValidationError,
                "FUTURE_IDEAS",
            ):
                loop_manager.validate_planner_result(
                    repo,
                    active_sha,
                    ["docs/notes.md"],
                )

            notes.write_text("# Notes\n", encoding="utf-8")
            existing.write_text(
                existing.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                loop_manager.PlannerValidationError,
                "기존 ExecPlan을 수정",
            ):
                loop_manager.validate_planner_result(
                    repo,
                    active_sha,
                    [existing.relative_to(repo).as_posix()],
                )

    def test_validation_rejects_future_idea_promoted_to_new_execplan(self):
        for future_idea_state in ("new", "existing", "removed"):
            baseline = {}
            with self.subTest(
                future_idea_state=future_idea_state
            ), tempfile.TemporaryDirectory() as directory, planner_git_observations(baseline):
                repo = make_catalog_root(directory)
                future_ideas = repo / "docs/FUTURE_IDEAS.md"
                if future_idea_state in {"existing", "removed"}:
                    future_ideas.write_text(
                        "# Future Ideas\n\n- Speculative deployment dashboard\n",
                        encoding="utf-8",
                    )
                    baseline['docs/FUTURE_IDEAS.md'] = future_ideas.read_text(encoding='utf-8')
                active_sha = 'base'
                if future_idea_state == "new":
                    future_ideas.write_text(
                        "# Future Ideas\n\n- Speculative deployment dashboard\n",
                        encoding="utf-8",
                    )
                elif future_idea_state == "removed":
                    future_ideas.write_text("# Future Ideas\n", encoding="utf-8")
                plan = write_execplan(
                    repo,
                    "G-001",
                    "Speculative deployment dashboard",
                )
                changed = [plan.relative_to(repo).as_posix()]
                if future_idea_state != "existing":
                    changed.append(future_ideas.relative_to(repo).as_posix())

                with self.assertRaisesRegex(
                    loop_manager.PlannerValidationError,
                    "FUTURE_IDEAS.*같은 계획 주기",
                ):
                    loop_manager.validate_planner_result(
                        repo,
                        active_sha,
                        changed,
                    )

    def test_validation_uses_token_boundaries_for_future_ideas(self):
        baseline = {}
        with tempfile.TemporaryDirectory() as directory, planner_git_observations(baseline):
            repo = make_catalog_root(directory)
            future_ideas = repo / "docs/FUTURE_IDEAS.md"
            future_ideas.write_text("# Future Ideas\n\n- CI\n", encoding="utf-8")
            plan = write_execplan(repo, "G-001", "Decision Log Cleanup")
            active_sha = 'base'
            result = loop_manager.validate_planner_result(
                repo,
                active_sha,
                [
                    future_ideas.relative_to(repo).as_posix(),
                    plan.relative_to(repo).as_posix(),
                ],
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual([item[1]["id"] for item in result.plans], ["G-001"])


    @dummy_worker_sessions
    def test_future_ideas_publish_with_and_without_execplans(self):
        # Real Git publication verifies both origin file contents and saved
        # plan revision OIDs; the validation condition matrix above uses mocks.
        for with_plan in (False, True):
            with self.subTest(with_plan=with_plan), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                state_dir = Path(directory) / "state"
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)
                original_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")

                def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
                    if role_name == "planner":
                        (worktree / "docs/FUTURE_IDEAS.md").write_text(
                            "# Future Ideas\n\n- Deferred idea\n",
                            encoding="utf-8", newline="\n",
                        )
                        if with_plan:
                            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                            write_execplan(worktree, "G-001", "Current Work")
                        return loop_manager.CodexResult(0, "", "planner-session")
                    return loop_manager.CodexResult(1, "stop after planning", "worker-session")

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.remote_base",
                    return_value="main",
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                self.assertEqual(result, 1 if with_plan else 0)
                self.assertEqual(
                    git_command(repo, "show", "origin/main:docs/FUTURE_IDEAS.md"),
                    "# Future Ideas\n\n- Deferred idea",
                )
                plans = connection.execute(
                    "SELECT plan_id, plan_sha FROM exec_plans ORDER BY plan_id"
                ).fetchall()
                if with_plan:
                    self.assertEqual(
                        [tuple(row) for row in plans],
                        [("G-001", git_command(repo, "rev-parse", "origin/main"))],
                    )
                    self.assertEqual(
                        git_command(
                            repo,
                            "show",
                            "origin/main:docs/plans/[G-001] Current Work.md",
                        ).splitlines()[1],
                        "id: G-001",
                    )
                else:
                    self.assertEqual(plans, [])
                    project = connection.execute(
                        "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    self.assertIsNone(project["phase"])
                    self.assertEqual(project["planning_fingerprint"], original_fingerprint)
