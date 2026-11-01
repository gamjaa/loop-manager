from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
import tempfile
import unittest
import loop_manager
from tests.support.git import git_command


@isolated_git_test
class ScopeGitMetadataTest(AgentScopeFixture, unittest.TestCase):

    def test_retained_rebase_head_remains_evidence_without_blocking_checkpoint(self):
        from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
        from loop_manager_internal.recovery import blocked_checkpoint
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            head = git_command(repo, 'rev-parse', 'HEAD')
            git_command(repo, 'update-ref', 'REBASE_HEAD', head)
            (repo / plan_path).write_text((repo / plan_path).read_text() + '\nWorker progress\n')
            observed = loop_manager.capture_git_snapshot(repo)
            self.assertIn('REBASE_HEAD', observed.operations)
            self.assertIsNone(controlling_git_operation(observed.operations))
            self.assertIn(plan_path, observed.dirty)
            verdict = blocked_checkpoint.assess_safety(blocked_checkpoint.SafetyObservation(
                workspace_identity_matches=True, workspace_owned=True, head_oid=observed.head,
                actual_branch=observed.branch, expected_branch=observed.branch,
                operations=tuple(observed.operations), dirty_paths=(blocked_checkpoint.DirtyPath(
                    plan_path, ('unstaged',), True),)))
            self.assertTrue(verdict.safe)
            self.assertEqual(verdict.included_paths, (plan_path,))










    def test_preexisting_index_flags_do_not_hide_agent_file_changes(self):
        scenarios = (
            ("planner", "README.md"),
            ("worker", "docs/PLANS.md"),
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
                        with (self.assertRaises(loop_manager.AgentScopeError) if role_name in {'planner', 'worker'}
                              else self.subTest()) as raised:
                            loop_manager.validate_agent_scope(
                                role_name,
                                repo,
                                before,
                                after,
                                selected_plan_path=(
                                    plan_path if role_name == "worker" else None
                                ),
                            )
                        if role_name in {'planner', 'worker'}:
                            self.assertIn(relative_path, str(raised.exception))
