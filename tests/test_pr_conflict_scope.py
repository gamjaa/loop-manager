from tests.support.pr_recovery import PrRecoveryFixture
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command
from tests.support.worker import dummy_worker_sessions


class PrConflictScopeTest(PrRecoveryFixture, unittest.TestCase):
    def test_shared_ignored_cache_preparation_propagates_metadata_failure(self):
        with mock.patch(
            "tests.support.pr_recovery.git_command",
            side_effect=RuntimeError("metadata unavailable"),
        ), self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
            self._add_ignored_cache(Path("/worktree"))

    def test_shared_ignored_cache_preparation_is_clean_and_absent_from_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, _, worktree, _, _ = self._pr_open_state(directory)
            cache = self._add_ignored_cache(worktree)
            self.assertTrue(cache.is_file())
            self.assertEqual(git_command(worktree, "status", "--porcelain"), "")
            self.assertNotIn(cache.relative_to(worktree).as_posix(),
                             loop_manager.capture_git_snapshot(worktree).dirty)

    @dummy_worker_sessions
    def test_unknown_and_mergeable_prs_do_not_rebase_run_agents_or_push(self):
        for mergeable, advance_base in (("UNKNOWN", False), ("MERGEABLE", True)):
            with self.subTest(mergeable=mergeable), tempfile.TemporaryDirectory() as directory:
                (
                    repo,
                    state_dir,
                    connection,
                    branch,
                    worktree,
                    pr_url,
                    head_oid,
                ) = self._pr_open_state(directory)
                cache = self._add_ignored_cache(worktree)
                if advance_base:
                    self._advance_base(
                        directory,
                        repo,
                        "docs/notes.md",
                        "# Behind only\n",
                        "Advance nonconflicting base",
                    )
                git_calls = []
                original_git = loop_manager.git

                def recording_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] in {"rebase", "push"}:
                        git_calls.append(arguments)
                    return original_git(path, *arguments, **kwargs)

                pull_request = self._pull_request(
                    branch, pr_url, head_oid, mergeable
                )
                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ) as synchronize, mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch(
                    "loop_manager.run_codex"
                ) as agent, mock.patch(
                    "loop_manager.verify"
                ) as verify, mock.patch(
                    "loop_manager.git", side_effect=recording_git
                ), mock.patch(
                    "loop_manager._fetch_pr_conflict_recovery_refs"
                ) as conflict_fetch, mock.patch(
                    "loop_manager._validate_pr_conflict_recovery_ownership"
                ) as conflict_ownership:
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                stored = connection.execute(
                    "SELECT status, pr_conflict_recovery FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(result, 0)
                self.assertEqual(tuple(stored), ("PR_OPEN", 0))
                self.assertEqual(git_calls, [])
                self.assertTrue(cache.is_file())
                conflict_fetch.assert_not_called()
                conflict_ownership.assert_not_called()
                synchronize.assert_not_called()
                agent.assert_not_called()
                verify.assert_not_called()

    def test_conflicting_pr_blocks_ignored_path_overlapping_base_update(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            exclude = Path(git_command(worktree, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = worktree / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\noverlap.txt\n",
                encoding="utf-8",
            )
            artifact = worktree / "overlap.txt"
            artifact.write_text("preserve ignored artifact\n", encoding="utf-8")
            self._advance_base(
                directory, repo, "overlap.txt", "tracked base value\n",
                "Add overlapping base file",
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch("loop_manager.git", wraps=loop_manager.git) as git_spy:
                result = loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "CONFLICTING"),
                )
            stored = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertIn("overlap.txt", stored["last_error"])
            self.assertEqual(artifact.read_text(encoding="utf-8"), "preserve ignored artifact\n")
            self.assertFalse(any(call.args[1:2] == ("rebase",) for call in git_spy.call_args_list))

    def test_conflicting_pr_rejects_nondefault_index_flags_with_ignored_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            git_command(worktree, "update-index", "--skip-worktree", "src/app.py")
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            result = loop_manager.reconcile_existing_pull_request(
                connection, repo, state_dir, str(repo), project,
                self._pull_request(branch, url, head, "CONFLICTING"),
            )
            stored = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertIn("index flags src/app.py", stored["last_error"])
            self.assertTrue(cache.is_file())

    def test_conflicting_pr_still_blocks_nonignored_untracked_change(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            (worktree / "unexpected.txt").write_text(
                "publishable local change\n",
                encoding="utf-8",
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            result = loop_manager.reconcile_existing_pull_request(
                connection,
                repo,
                state_dir,
                str(repo),
                project,
                self._pull_request(branch, pr_url, head_oid, "CONFLICTING"),
            )
            stored = connection.execute(
                """
                SELECT status, blocked_reason, last_error
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()

        self.assertEqual(result, 1)
        self.assertEqual(stored["status"], "PLAN_BLOCKED")
        self.assertEqual(
            stored["blocked_reason"],
            loop_manager.BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value,
        )
        self.assertIn("unexpected.txt", stored["last_error"])
