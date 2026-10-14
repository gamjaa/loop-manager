from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, commit_and_push, advance_remote_file
from tests.support.worker import worker_result, isolated_scope_resources
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class ScopeValidationMutationsTest(AgentScopeFixture, unittest.TestCase):

    @dummy_worker_sessions
    def test_changed_candidate_during_review_revalidates_without_access_violation(self):
        origin_parser = mock.patch(
            "loop_manager_internal.git.git_adapter.github_repository",
            return_value="owner/repo",
        )
        origin_parser.start()
        self.addCleanup(origin_parser.stop)
        manager_origin_parser = mock.patch.object(
            loop_manager, "github_repository", return_value="owner/repo")
        manager_origin_parser.start()
        self.addCleanup(manager_origin_parser.stop)
        isolation = mock.patch.object(loop_manager, '_agent_git_resources', side_effect=isolated_scope_resources)
        isolation.start()
        self.addCleanup(isolation.stop)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Prepare verified candidate")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, origin, active_sha, plan_sha, phase, branch,
                    worktree, plan_path, worker_result, verify_result
                ) VALUES (?, 'main', 'owner/repo', ?, ?, 'VERIFYING', ?, ?, ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan_path,
                    worker_result(),
                    None,
                ),
            )
            connection.commit()
            self._confirm_general_plan_input(connection, repo, plan_sha)
            with connection:
                connection.execute(
                    """
                    UPDATE projects
                    SET planning_fingerprint = ?, specification_fingerprint = ?
                    WHERE repo = ?
                    """,
                    (
                        loop_manager.planning_input_fingerprint(repo, "main"),
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )

            def reviewer(agent_worktree, *_args, **_kwargs):
                if _args[2] == 'worker':
                    return loop_manager.CodexResult(1, 'stop after candidate invalidation', 'saved-worker')
                (agent_worktree / "reviewer.txt").write_text(
                    "keep for diagnosis\n", encoding="utf-8"
                )
                (agent_worktree / "docs/PRODUCT.md").write_text(
                    "# Product changed by Reviewer\n", encoding="utf-8"
                )
                return 0, "VERDICT: PASS"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ), mock.patch(
                "loop_manager.run_codex", side_effect=reviewer
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_role, agent_snapshot, "
                "agent_result_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project['phase'], 'WORKING')
            self.assertEqual(plan['status'], 'IMPLEMENTING')
            self.assertIsNone(plan['blocked_reason'])
            self.assertIsNone(plan['verify_token'])
            self.assertIsNone(plan['review_token'])
            self.assertTrue((worktree / 'reviewer.txt').is_file())
            self.assertEqual((worktree / 'docs/PRODUCT.md').read_text(encoding='utf-8'),
                             '# Product changed by Reviewer\n')
            self.assertIsNone(loop_manager.scoped_blocking.project_block(connection, str(repo)))

    def test_reviewer_scope_block_records_execplan_body_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            worktree = root / "managed"
            loop_manager.make_worktree(
                repo,
                worktree,
                "loop-manager/G-001",
                "main",
            )
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (repo, base_branch, plan_path)
                VALUES (?, 'main', ?)
                """,
                (str(repo), plan_path),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            loop_manager.update_project(connection, str(repo), plan_path=plan_path)
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            input_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            plan_file = worktree / plan_path
            input_plan = plan_file.read_text(encoding="utf-8")
            loop_manager.begin_agent_scope(
                connection,
                str(repo),
                worktree,
                "reviewer",
                input_fingerprint=input_fingerprint,
            )
            result_plan = input_plan.replace("Test plan.", "Reviewer mutation.")
            plan_file.write_text(result_plan, encoding="utf-8")

            loop_manager.block_active_execplan(
                connection,
                str(repo),
                worktree,
                "reviewer scope violation",
                "",
            )

            plan = connection.execute(
                "SELECT agent_input_plan, agent_result_plan "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(plan["agent_input_plan"], input_plan)
            self.assertEqual(plan["agent_result_plan"], result_plan)

    @dummy_worker_sessions
    def test_verify_mutation_of_worker_protected_file_blocks_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Prepare Worker candidate")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            initial_head = git_command(worktree, "rev-parse", "HEAD")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path, worker_result
                ) VALUES (?, 'main', ?, ?, 'VERIFYING', ?, ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan_path,
                    worker_result(),
                ),
            )
            connection.commit()

            def verify_with_protected_mutation(agent_worktree, timeout_seconds):
                self.assertEqual(timeout_seconds, 600)
                (agent_worktree / "docs/PLANS.md").write_text(
                    "# changed by verify\n",
                    encoding="utf-8",
                )
                return True, "verify: PASS"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify", side_effect=verify_with_protected_mutation
            ) as verify, mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, verify_result, agent_role, "
                "agent_snapshot, agent_result_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, last_error, verify_result, agent_role, "
                "agent_snapshot, agent_result_snapshot FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()

            self.assertEqual(result, 0)
            self.assertIsNone(project["phase"])
            self.assertIn("worker 결과 변경 범위 오류", plan["last_error"])
            self.assertIn(
                "docs/PLANS.md",
                plan["last_error"],
            )
            self.assertEqual(plan["verify_result"], "verify: PASS")
            self.assertEqual(plan["agent_role"], "worker")
            self.assertIsNotNone(plan["agent_snapshot"])
            self.assertIsNotNone(plan["agent_result_snapshot"])
            self.assertEqual(plan["status"], "PLAN_BLOCKED")
            self.assertEqual(
                git_command(worktree, "rev-parse", "HEAD"),
                initial_head,
            )
            self.assertEqual(
                (worktree / "docs/PLANS.md").read_text(
                    encoding="utf-8"
                ),
                "# changed by verify\n",
            )
            verify.assert_called_once_with(worktree, 600)
            agent.assert_not_called()

    @dummy_worker_sessions
    def test_rebase_verify_allowed_change_is_committed_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan_path,
                ),
            )
            connection.commit()

            (repo / "README.md").write_text(
                "# base advanced\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Advance base before worker publication")
            roles = []
            verify_calls = []

            def agent(agent_worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                if role_name == "worker":
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 2\n",
                        encoding="utf-8",
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result("Persist rebased verify output"),
                        "worker-session",
                    )
                self.assertEqual(role_name, "reviewer")
                self.assertEqual(
                    git_command(agent_worktree, "status", "--porcelain"),
                    "",
                )
                self.assertEqual(
                    git_command(
                        agent_worktree,
                        "show",
                        "HEAD:verify-output.txt",
                    ),
                    "created after rebase",
                )
                return 1, "stop after publication check"

            def verify_after_rebase(agent_worktree, timeout_seconds):
                self.assertEqual(timeout_seconds, 600)
                verify_calls.append(agent_worktree)
                if len(verify_calls) == 1:
                    (agent_worktree / "verify-output.txt").write_text(
                        "created after rebase\n",
                        encoding="utf-8",
                    )
                return True, f"verify pass {len(verify_calls)}"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ), mock.patch(
                "loop_manager.verify", side_effect=verify_after_rebase
            ), mock.patch(
                "loop_manager_internal.execution.execution_verify_handler.common_guidance_report",
                return_value="",
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            published_paths = git_command(
                worktree,
                "diff",
                "--name-only",
                "origin/main...HEAD",
            ).splitlines()

            self.assertEqual(result, 1)
            self.assertEqual(roles, ["worker", "reviewer"])
            self.assertEqual(len(verify_calls), 2)
            self.assertEqual(project["phase"], "REVIEWING")
            self.assertIn("stop after publication check", project["last_error"])
            self.assertIn("src/app.py", published_paths)
            self.assertIn("verify-output.txt", published_paths)
            self.assertEqual(
                git_command(worktree, "status", "--porcelain"),
                "",
            )
            self.assertEqual(
                git_command(worktree, "log", "-1", "--pretty=%s"),
                "Persist rebased verify output",
            )
