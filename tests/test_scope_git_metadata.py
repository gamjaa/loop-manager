from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, advance_remote_file
from tests.support.worker import isolated_scope_resources
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class ScopeGitMetadataTest(AgentScopeFixture, unittest.TestCase):

    def test_agent_commit_and_branch_switch_are_rejected(self):
        mutations = (
            (
                "commit",
                lambda repo: (
                    (repo / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    ),
                    git_command(repo, "add", "src/app.py"),
                    git_command(repo, "commit", "-m", "Agent commit"),
                ),
                "HEAD 변경",
            ),
            (
                "branch",
                lambda repo: git_command(repo, "switch", "-c", "agent-branch"),
                "branch 전환",
            ),
        )
        for name, mutate, expected in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                mutate(repo)
                after = loop_manager.capture_git_snapshot(repo)

                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError, expected
                ):
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )

    def test_unfinished_merge_is_rejected_even_for_worker_allowed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            git_command(repo, "switch", "-c", "merge-source")
            (repo / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "Prepare merge source")
            git_command(repo, "switch", "main")
            before = loop_manager.capture_git_snapshot(repo)

            git_command(
                repo,
                "merge",
                "--no-ff",
                "--no-commit",
                "merge-source",
            )
            after = loop_manager.capture_git_snapshot(repo)

            self.assertNotIn("MERGE_HEAD", before.operations)
            self.assertIn("MERGE_HEAD", after.operations)
            with self.assertRaisesRegex(
                loop_manager.AgentScopeError,
                r"Git operation 상태 변경 .*MERGE_HEAD",
            ):
                loop_manager.validate_agent_scope(
                    "worker",
                    repo,
                    before,
                    after,
                    selected_plan_path=plan_path,
                )

    def test_auto_merge_tree_ref_alone_is_not_an_active_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            auto_merge = Path(
                git_command(repo, "rev-parse", "--git-path", "AUTO_MERGE")
            )
            if not auto_merge.is_absolute():
                auto_merge = repo / auto_merge
            auto_merge.write_text(
                git_command(repo, "write-tree") + "\n",
                encoding="ascii",
            )

            after = loop_manager.capture_git_snapshot(repo)

            self.assertTrue(auto_merge.is_file())
            self.assertNotIn("AUTO_MERGE", after.operations)
            loop_manager.validate_agent_scope(
                "worker",
                repo,
                before,
                after,
                selected_plan_path=plan_path,
            )

    def test_shared_git_metadata_changes_are_rejected(self):
        scenarios = (
            (
                "branch",
                lambda repo, _root: git_command(
                    repo, "branch", "agent-created"
                ),
                "refs",
                r"공유 Git refs 변경 .*refs/heads/agent-created",
            ),
            (
                "tag",
                lambda repo, _root: git_command(repo, "tag", "agent-tag"),
                "refs",
                r"공유 Git refs 변경 .*refs/tags/agent-tag",
            ),
            (
                "worktree",
                lambda repo, root: git_command(
                    repo,
                    "worktree",
                    "add",
                    "--detach",
                    str(root / "agent-worktree"),
                    "HEAD",
                ),
                "worktrees",
                r"Git worktree 등록·HEAD·index·파일 상태 변경 .*agent-worktree",
            ),
        )
        for name, mutate, state_field, expected in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                self.assertEqual(
                    loop_manager.GitSnapshot.deserialize(before.serialize()),
                    before,
                )

                mutate(repo, root)
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotEqual(
                    getattr(before, state_field),
                    getattr(after, state_field),
                )
                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    expected,
                ):
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )

    def test_changes_in_another_registered_worktree_are_rejected(self):
        for mutation in ("file", "index"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    repo, plan_path = self._repository_with_plan(directory)
                    managed = root / "managed"
                    loop_manager.make_worktree(
                        repo,
                        managed,
                        "loop-manager/G-099",
                        "main",
                    )
                    before = loop_manager.capture_git_snapshot(managed)

                    if mutation == "file":
                        (repo / "src/app.py").write_text(
                            "VALUE = 2\n",
                            encoding="utf-8",
                        )
                    else:
                        git_command(
                            repo,
                            "update-index",
                            "--assume-unchanged",
                            "--",
                            "src/app.py",
                        )
                    after = loop_manager.capture_git_snapshot(managed)

                    external_path = str(repo.resolve())
                    self.assertNotEqual(
                        before.worktrees[external_path],
                        after.worktrees[external_path],
                    )
                    with self.assertRaises(
                        loop_manager.AgentScopeError
                    ) as raised:
                        loop_manager.validate_agent_scope(
                            "worker",
                            managed,
                            before,
                            after,
                            selected_plan_path=plan_path,
                        )
                    self.assertIn(external_path, str(raised.exception))

    def test_primary_worktree_and_base_refs_can_change_during_managed_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            managed = root / "managed"
            loop_manager.make_worktree(
                repo,
                managed,
                "loop-manager/G-099",
                "main",
            )
            before = loop_manager.capture_git_snapshot(managed)

            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "Update primary worktree")
            after = loop_manager.capture_git_snapshot(managed)

            primary_path = str(repo.resolve())
            self.assertNotEqual(
                before.worktrees[primary_path],
                after.worktrees[primary_path],
            )
            self.assertNotEqual(
                before.refs["refs/heads/main"],
                after.refs["refs/heads/main"],
            )
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager.validate_agent_scope(
                    "worker",
                    managed,
                    before,
                    after,
                    selected_plan_path=plan_path,
                )
            resources = loop_manager.agent_scope_decision.used_git_resources(
                role='worker', phase='WORKING', base_branch='main',
                branch='loop-manager/G-099', worktree_key=str(managed.resolve()))
            loop_manager.validate_agent_scope(
                "worker",
                managed,
                before,
                after,
                selected_plan_path=plan_path,
                external_worktree=repo,
                base_branch="main",
                resources=resources,
            )

    def test_primary_worktree_registration_removal_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            managed = root / "managed"
            loop_manager.make_worktree(
                repo,
                managed,
                "loop-manager/G-099",
                "main",
            )
            before = loop_manager.capture_git_snapshot(managed)
            primary_path = str(repo.resolve())
            remaining_worktrees = dict(before.worktrees)
            del remaining_worktrees[primary_path]
            after = loop_manager.GitSnapshot(
                head=before.head,
                branch=before.branch,
                index=before.index,
                dirty=before.dirty,
                refs=before.refs,
                worktrees=remaining_worktrees,
                config=before.config,
                operations=before.operations,
            )

            with self.assertRaises(loop_manager.AgentScopeError) as raised:
                loop_manager.validate_agent_scope(
                    "worker",
                    managed,
                    before,
                    after,
                    selected_plan_path=plan_path,
                    external_worktree=repo,
                    base_branch="main",
                )

            self.assertEqual(
                raised.exception.reason_code,
                loop_manager.BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
            )

    @dummy_worker_sessions
    def test_restored_worker_git_metadata_allows_changed_input_to_requeue(self):
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
                state_dir,
                str(repo),
                "G-001",
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch, origin) VALUES (?, 'main', 'owner/repo')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            blocked_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            before = loop_manager.capture_git_snapshot(worktree)

            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Agent commit")
            git_command(worktree, "switch", "-c", "agent-branch")
            git_command(
                worktree,
                "update-ref",
                "refs/tags/agent-tag",
                "HEAD",
            )
            git_command(worktree, "config", "core.hooksPath", ".agent-hooks")
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        blocked_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        agent_role = 'worker', agent_snapshot = ?,
                        agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        blocked_result.serialize(),
                        str(repo),
                    ),
                )

            git_command(worktree, "config", "--unset", "core.hooksPath")
            git_command(worktree, "switch", branch)
            git_command(worktree, "reset", "--hard", plan_sha)
            git_command(worktree, "branch", "-D", "agent-branch")
            git_command(worktree, "update-ref", "-d", "refs/tags/agent-tag")
            advance_remote_file(directory, repo, 'docs/PRODUCT.md', '# Product changed after Git recovery\n', 'Change source after Git recovery')
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                return_value=None,
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=lambda _worktree, _state, _config, role_name, _prompt, **_kwargs:
                    loop_manager.CodexResult(
                        0 if role_name == "planner" else 1,
                        "" if role_name == "planner" else "stop after restored metadata",
                        "planner-session" if role_name == "planner" else "worker-session",
                    ),
            ) as resumed_agent:
                result = 0
                for _ in range(2):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )
                    if result:
                        break

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot, agent_result_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(project["last_error"], "stop after restored metadata")
            self.assertIsNone(project["agent_snapshot"])
            self.assertIsNone(project["agent_result_snapshot"])
            self.assertEqual(plan["status"], "IMPLEMENTING")
            self.assertIsNone(plan["blocked_fingerprint"])
            self.assertEqual(
                [call.args[3] for call in resumed_agent.call_args_list],
                ["planner", "worker"],
            )

    def test_index_flags_are_rejected_for_worker_allowed_path(self):
        for option in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)

                git_command(
                    repo,
                    "update-index",
                    option,
                    "--",
                    "src/app.py",
                )
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotEqual(
                    before.index["src/app.py"],
                    after.index["src/app.py"],
                )
                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    r"src/app\.py .*index flags",
                ):
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )

    def test_preexisting_index_flags_do_not_hide_agent_file_changes(self):
        scenarios = (
            ("planner", "README.md"),
            ("worker", "docs/requirements/Active.md"),
            ("reviewer", "src/app.py"),
        )
        for option in ("--skip-worktree", "--assume-unchanged"):
            for role_name, relative_path in scenarios:
                with self.subTest(option=option, role=role_name):
                    with tempfile.TemporaryDirectory() as directory:
                        repo, plan_path = self._repository_with_plan(directory)
                        git_command(
                            repo,
                            "update-index",
                            option,
                            "--",
                            relative_path,
                        )
                        before = loop_manager.capture_git_snapshot(repo)

                        (repo / relative_path).write_text(
                            f"changed by {role_name}\n",
                            encoding="utf-8",
                        )
                        after = loop_manager.capture_git_snapshot(repo)

                        self.assertEqual(
                            before.index[relative_path],
                            after.index[relative_path],
                        )
                        self.assertNotEqual(
                            before.dirty[relative_path],
                            after.dirty[relative_path],
                        )
                        with self.assertRaises(
                            loop_manager.AgentScopeError
                        ) as raised:
                            loop_manager.validate_agent_scope(
                                role_name,
                                repo,
                                before,
                                after,
                                selected_plan_path=(
                                    plan_path if role_name == "worker" else None
                                ),
                            )
                        self.assertIn(relative_path, str(raised.exception))
