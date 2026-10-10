from tests.support.git import isolated_git_test
from tests.support.workspace import CleanupFixture
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, make_remote_repository


@isolated_git_test
class WorktreeCleanupTest(CleanupFixture, unittest.TestCase):

    def test_commit_added_after_assessment_preserves_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, _branch, pr_head = self._managed_worktree(Path(directory))
            assessment = loop_manager.assess_cleanup(worktree, pr_head)
            self.assertEqual(assessment.status, "safe")
            (worktree / "README.md").write_text("# New local work\n", encoding="utf-8")
            git_command(worktree, "add", "README.md")
            git_command(worktree, "commit", "-m", "local after assessment")

            result = loop_manager.remove_cleanup_worktree(repo, worktree, assessment)

            self.assertEqual(result.status, "local-only commits")
            self.assertTrue(worktree.exists())

    def test_unverifiable_pr_head_after_assessment_preserves_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            marker = worktree / 'keep.txt'
            marker.write_bytes(b'keep')
            assessment = loop_manager.CleanupAssessment('safe', pr_head_oid='a' * 40,
                                                        observation=('a' * 40, 'branch', ()))
            with mock.patch.object(loop_manager, "assess_cleanup", return_value=
                loop_manager.CleanupAssessment("unverifiable", "PR head unavailable")) as observe, mock.patch(
                'subprocess.Popen', side_effect=AssertionError('예상 밖 process 실행')
            ) as process:
                repo = worktree / 'repo'
                result = loop_manager.remove_cleanup_worktree(repo, worktree, assessment)
                observe.assert_called_once_with(worktree, 'a' * 40)
                process.assert_not_called()
            self.assertEqual(result.status, "unverifiable")
            self.assertTrue(worktree.exists())
            self.assertEqual(marker.read_bytes(), b'keep')

    def test_changed_clean_head_after_assessment_preserves_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, _branch, pr_head = self._managed_worktree(Path(directory))
            assessment = loop_manager.assess_cleanup(worktree, pr_head)
            parent = git_command(worktree, "rev-parse", "HEAD^")
            git_command(worktree, "reset", "--hard", parent)
            result = loop_manager.remove_cleanup_worktree(repo, worktree, assessment)
            self.assertEqual(result.status, "unverifiable")
            self.assertTrue(worktree.exists())

    def test_ignored_only_worktree_is_removed_after_safe_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, _branch, head = self._managed_worktree(Path(directory))
            exclude = Path(git_command(worktree, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = worktree / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n*.cache\n",
                encoding="utf-8",
            )
            (worktree / "verify.cache").write_text("artifact", encoding="utf-8")
            assessment = loop_manager.assess_cleanup(worktree, head)
            self.assertEqual(assessment.status, "safe")
            self.assertEqual(
                loop_manager.remove_cleanup_worktree(repo, worktree, assessment).status,
                "safe",
            )
            self.assertFalse(worktree.exists())

    def test_file_created_at_removal_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, _branch, head = self._managed_worktree(Path(directory))
            assessment = loop_manager.assess_cleanup(worktree, head)
            self.assertEqual(assessment.status, "safe")
            original_git = loop_manager.git

            def create_file_before_remove(path, *arguments, **kwargs):
                if arguments[:2] == ("worktree", "remove"):
                    (worktree / "late.txt").write_text("user change\n", encoding="utf-8")
                return original_git(path, *arguments, **kwargs)

            with mock.patch.object(loop_manager, "git", side_effect=create_file_before_remove):
                result = loop_manager.remove_cleanup_worktree(repo, worktree, assessment)
            self.assertEqual(result.status, "unverifiable")
            self.assertEqual((worktree / "late.txt").read_text(encoding="utf-8"), "user change\n")

    def test_file_created_after_managed_unlock_is_preserved_and_relocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree, _branch, head = self._managed_worktree(root, "G-135")
            git_command(repo, "worktree", "lock", "--reason=loop-manager-worktree:owned", str(worktree))
            assessment = loop_manager.assess_cleanup(worktree, head)
            self.assertEqual(assessment.status, "safe")
            original_git = loop_manager.git

            def create_file_before_remove(path, *arguments, **kwargs):
                if arguments[:2] == ("worktree", "remove"):
                    (worktree / "late.txt").write_text("user change\n", encoding="utf-8")
                return original_git(path, *arguments, **kwargs)

            state_dir = root / "state"
            with mock.patch.object(loop_manager, "git", side_effect=create_file_before_remove):
                result = loop_manager.remove_cleanup_worktree(
                    repo, worktree, assessment, state_dir=state_dir, owner="plan:G-135")
            self.assertEqual(result.status, "unverifiable")
            self.assertEqual((worktree / "late.txt").read_text(encoding="utf-8"), "user change\n")
            self.assertFalse(loop_manager._recover_managed_worktree_removal(state_dir, repo, worktree))
            record = next(item for item in loop_manager._worktree_catalog(repo)
                          if Path(item["worktree"]) == worktree)
            self.assertEqual(record.get("locked"), "loop-manager-worktree:owned")

    def test_head_or_index_change_after_unlock_aborts_stale_journal(self):
        for mutation in ("head", "index"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, worktree, _branch, head = self._managed_worktree(root, "G-135")
                git_command(repo, "worktree", "lock", "--reason=loop-manager-worktree:owned", str(worktree))
                assessment = loop_manager.assess_cleanup(worktree, head)
                self.assertEqual(assessment.status, "safe")
                original_git = loop_manager.git

                def mutate_after_unlock(path, *arguments, **kwargs):
                    result = original_git(path, *arguments, **kwargs)
                    if arguments[:2] == ("worktree", "unlock"):
                        if mutation == "head":
                            git_command(worktree, "reset", "--hard", "HEAD^")
                        else:
                            (worktree / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
                            git_command(worktree, "add", "src/app.py")
                    return result

                state_dir = root / "state"
                with mock.patch.object(loop_manager, "git", side_effect=mutate_after_unlock):
                    result = loop_manager.remove_cleanup_worktree(
                        repo, worktree, assessment, state_dir=state_dir, owner="plan:G-135")
                self.assertEqual(result.status, "unverifiable")
                self.assertTrue(worktree.exists())
                record = next(item for item in loop_manager._worktree_catalog(repo)
                              if Path(item["worktree"]) == worktree)
                self.assertEqual(record.get("locked"), "loop-manager-worktree:owned")
                fresh = loop_manager.assess_cleanup(worktree, head)
                if mutation == "head":
                    self.assertEqual(fresh.status, "safe")
                    self.assertEqual(loop_manager.remove_cleanup_worktree(
                        repo, worktree, fresh, state_dir=state_dir,
                        owner="plan:G-135").status, "safe")
                else:
                    self.assertFalse(loop_manager._recover_managed_worktree_removal(
                        state_dir, repo, worktree))
                    self.assertEqual(fresh.status, "dirty")
                    self.assertEqual((worktree / "src/app.py").read_text(encoding="utf-8"), "VALUE = 3\n")
                self.assertEqual(list((state_dir / "manager-git-journal").glob("*/active.json")), [])

    def test_interrupted_cleanup_with_changed_head_recovers_for_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree, _branch, head = self._managed_worktree(root, "G-135")
            git_command(repo, "worktree", "lock", "--reason=loop-manager-worktree:owned", str(worktree))
            assessment = loop_manager.assess_cleanup(worktree, head)
            original_git = loop_manager.git
            original_assess = loop_manager.assess_cleanup
            unlocked = False

            def mutate_after_unlock(path, *arguments, **kwargs):
                nonlocal unlocked
                result = original_git(path, *arguments, **kwargs)
                if arguments[:2] == ("worktree", "unlock"):
                    git_command(worktree, "reset", "--hard", "HEAD^")
                    unlocked = True
                return result

            def interrupt_after_unlock(*arguments):
                if unlocked:
                    raise KeyboardInterrupt()
                return original_assess(*arguments)

            state_dir = root / "state"
            with (mock.patch.object(loop_manager, "git", side_effect=mutate_after_unlock),
                  mock.patch.object(loop_manager, "assess_cleanup", side_effect=interrupt_after_unlock),
                  self.assertRaises(KeyboardInterrupt)):
                loop_manager.remove_cleanup_worktree(
                    repo, worktree, assessment, state_dir=state_dir, owner="plan:G-135")
            self.assertFalse(loop_manager._recover_managed_worktree_removal(state_dir, repo, worktree))
            self.assertEqual(list((state_dir / "manager-git-journal").glob("*/active.json")), [])
            fresh = loop_manager.assess_cleanup(worktree, head)
            self.assertEqual(fresh.status, "safe")
            self.assertEqual(loop_manager.remove_cleanup_worktree(
                repo, worktree, fresh, state_dir=state_dir, owner="plan:G-135").status, "safe")

    def test_unlocked_cleanup_head_change_aborts_stale_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree, _branch, head = self._managed_worktree(root, "G-135")
            assessment = loop_manager.assess_cleanup(worktree, head)
            original_assess = loop_manager.assess_cleanup
            calls = 0

            def change_head_on_journal_recheck(*arguments):
                nonlocal calls
                calls += 1
                if calls == 2:
                    git_command(worktree, "reset", "--hard", "HEAD^")
                return original_assess(*arguments)

            state_dir = root / "state"
            with mock.patch.object(loop_manager, "assess_cleanup",
                                   side_effect=change_head_on_journal_recheck):
                result = loop_manager.remove_cleanup_worktree(
                    repo, worktree, assessment, state_dir=state_dir, owner="plan:G-135")
            self.assertEqual(result.status, "unverifiable")
            self.assertTrue(worktree.exists())
            self.assertFalse(loop_manager._recover_managed_worktree_removal(state_dir, repo, worktree))
            self.assertEqual(list((state_dir / "manager-git-journal").glob("*/active.json")), [])
            fresh = loop_manager.assess_cleanup(worktree, head)
            self.assertEqual(fresh.status, "safe")
            self.assertEqual(loop_manager.remove_cleanup_worktree(
                repo, worktree, fresh, state_dir=state_dir, owner="plan:G-135").status, "safe")

    def test_planning_cleanup_accepts_ignored_artifact_but_preserves_dirty_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            planning = root / "planning"
            git_command(repo, "worktree", "add", "--detach", str(planning), "origin/main")
            exclude = Path(git_command(planning, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = planning / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n*.cache\n",
                encoding="utf-8",
            )
            (planning / "build.cache").write_text("artifact", encoding="utf-8")
            port = loop_manager._PlanningGitPorts(sqlite3.connect(":memory:"), repo, root)
            (planning / "unexpected.txt").write_text("publishable", encoding="utf-8")
            with self.assertRaises(loop_manager.WorkspaceOwnershipError):
                port.run(repo, "worktree", "remove", str(planning))
            self.assertTrue(planning.exists())
            (planning / "unexpected.txt").unlink()
            port.run(repo, "worktree", "remove", str(planning))
            self.assertFalse(planning.exists())

    def test_merge_modes_do_not_use_base_ancestry_for_cleanup(self):
        for mode in ("merge", "squash", "rebase"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, worktree, branch, pr_head = self._managed_worktree(root)

                if mode == "merge":
                    git_command(repo, "merge", "--no-ff", "-m", "Merge PR", branch)
                elif mode == "squash":
                    git_command(repo, "merge", "--squash", branch)
                    git_command(repo, "commit", "-m", "Squash PR")
                else:
                    (repo / "README.md").write_text("# Base changed\n", encoding="utf-8")
                    git_command(repo, "add", "README.md")
                    git_command(repo, "commit", "-m", "Advance base")
                    git_command(repo, "cherry-pick", pr_head)

                base_contains_head = subprocess.run(
                    ["git", "-C", str(repo), "merge-base", "--is-ancestor", pr_head, "main"]
                ).returncode == 0
                self.assertEqual(base_contains_head, mode == "merge")
                assessment = loop_manager.assess_cleanup(worktree, pr_head)
                self.assertEqual(assessment.status, "safe")
                self.assertEqual(
                    loop_manager.remove_cleanup_worktree(repo, worktree, assessment).status,
                    "safe",
                )
                self.assertFalse(worktree.exists())

    def test_deleted_remote_branch_still_uses_saved_pr_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree, branch, pr_head = self._managed_worktree(root)
            git_command(worktree, "push", "-u", "origin", branch)
            git_command(repo, "push", "origin", "--delete", branch)

            self.assertEqual(loop_manager.assess_cleanup(worktree, pr_head).status, "safe")

    def test_dirty_reasons_cover_tracked_staged_and_untracked_changes(self):
        changes = {'tracked 파일 수정': ' M src/app.py\n',
                   'staged 변경': 'M  src/app.py\n',
                   'untracked 파일': '?? untracked.txt\n'}
        for expected_reason, output in changes.items():
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as directory:
                worktree = Path(directory)
                marker = worktree / 'keep.txt'
                marker.write_bytes(b'keep')
                with mock.patch.object(loop_manager, 'git', return_value=
                    subprocess.CompletedProcess([], 0, output)) as git, mock.patch(
                    'subprocess.Popen', side_effect=AssertionError('예상 밖 process 실행')
                ) as process:
                    assessment = loop_manager.assess_cleanup(worktree, 'a' * 40)
                    git.assert_called_once_with(worktree, 'status', '--porcelain=v1', '--untracked-files=all')
                    process.assert_not_called()

                self.assertEqual(assessment.status, "dirty")
                self.assertIn(expected_reason, assessment.reason)
                self.assertTrue(worktree.exists())
                self.assertEqual(marker.read_bytes(), b'keep')

    def test_local_only_and_unverifiable_commits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _repo, worktree, _branch, pr_head = self._managed_worktree(root)
            (worktree / "README.md").write_text("# Local only\n", encoding="utf-8")
            git_command(worktree, "add", "README.md")
            git_command(worktree, "commit", "-m", "Local only")

            local_only = loop_manager.assess_cleanup(worktree, pr_head)
            missing_head = loop_manager.assess_cleanup(worktree, None)
            missing_object = loop_manager.assess_cleanup(worktree, "f" * 40)

            self.assertEqual(local_only.status, "local-only commits")
            self.assertIn("PR head에 포함되지 않은", local_only.reason)
            self.assertEqual(missing_head.status, "unverifiable")
            self.assertIn("PR head commit 정보", missing_head.reason)
            self.assertEqual(missing_object.status, "unverifiable")
            self.assertIn("commit object", missing_object.reason)

    def test_pr_queries_request_immutable_head_oid(self):
        response = subprocess.CompletedProcess(
            ["gh"], 0, stdout=(
                '{"url":"https://example.test/pr/7","state":"MERGED","mergedAt":"t",'
                '"headRefOid":"abc","headRefName":"loop-manager/G-007",'
                '"baseRefName":"main","mergeable":"UNKNOWN"}'
            ),
        )
        with mock.patch("loop_manager.command", return_value=response) as command:
            row = loop_manager.pr_for_branch(
                Path("/repo"), "loop-manager/G-007", "https://example.test/pr/7"
            )

        self.assertEqual(row["headRefOid"], "abc")
        self.assertEqual(row["baseRefName"], "main")
        self.assertIn("headRefOid", command.call_args.args[0][-1])
        self.assertIn("baseRefName", command.call_args.args[0][-1])
        self.assertIn("mergeable", command.call_args.args[0][-1])
