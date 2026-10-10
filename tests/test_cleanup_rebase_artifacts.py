from tests.support.git import isolated_git_test
import subprocess
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, make_remote_repository
from tests.support.workspace import WorkspaceFixture


@isolated_git_test
class CleanupRebaseArtifactsTest(WorkspaceFixture, unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows의 실제 대소문자 경로 alias를 검사한다.')
    def test_windows_case_alias_ignored_file_is_preserved_before_rebase(self):
        # 실제 파일 alias와 Git의 overwrite 효과는 관측 mock만으로 증명할 수 없다.
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            ignore = repo / '.gitignore'
            ignore.write_text((ignore.read_text() if ignore.exists() else '') + '\ncache/\n')
            git_command(repo, 'add', '.gitignore')
            git_command(repo, 'commit', '-m', 'Ignore cache')
            git_command(repo, 'checkout', '-b', 'target')
            (repo / 'cache').mkdir()
            (repo / 'cache/file').write_text('remote tracked')
            git_command(repo, 'add', '-f', 'cache/file')
            git_command(repo, 'commit', '-m', 'Track target path')
            git_command(repo, 'checkout', 'main')
            (repo / 'CACHE').mkdir()
            ignored = repo / 'CACHE/file'
            ignored.write_text('user ignored')
            head = git_command(repo, 'rev-parse', 'HEAD')
            result = loop_manager.git(repo, 'rebase', 'target', check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('CACHE/file', result.stdout)
            self.assertEqual(ignored.read_text(), 'user ignored')
            self.assertEqual(git_command(repo, 'rev-parse', 'HEAD'), head)

    def test_git_metadata_path_preserves_absolute_and_resolves_relative_paths(self):
        repo = Path("/repository")
        absolute = Path("/git/common/info/exclude")
        with mock.patch(
            "tests.support.workspace.git_command", return_value=str(absolute)
        ):
            self.assertEqual(self._git_metadata_path(repo, "info/exclude"), absolute)
        with mock.patch(
            "tests.support.workspace.git_command", return_value=".git/info/exclude"
        ):
            self.assertEqual(
                self._git_metadata_path(repo, "info/exclude"),
                repo / ".git/info/exclude",
            )

    def test_rebase_keeps_ignored_file_that_overlaps_target(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            git_command(repo, "checkout", "-b", "target")
            (repo / "collide.txt").write_text("tracked", encoding="utf-8")
            git_command(repo, "add", "collide.txt")
            git_command(repo, "commit", "-m", "Add target file")
            git_command(repo, "checkout", "main")
            exclude = self._git_metadata_path(repo, "info/exclude")
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\ncollide.txt\n",
                encoding="utf-8",
            )
            (repo / "collide.txt").write_text("local ignored", encoding="utf-8")
            head = git_command(repo, "rev-parse", "HEAD")
            result = loop_manager.git(repo, "rebase", "target", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("collide.txt", result.stdout)
            self.assertEqual((repo / "collide.txt").read_text(encoding="utf-8"), "local ignored")
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)

    def test_rebase_resume_commands_preserve_ignored_next_commit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            onto = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "-b", "feature")
            (repo / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "First feature commit")
            (repo / "later.txt").write_text("tracked", encoding="utf-8")
            git_command(repo, "add", "later.txt")
            git_command(repo, "commit", "-m", "Next feature commit")
            original_head = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "main")
            exclude = self._git_metadata_path(repo, "info/exclude")
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nlater.txt\n",
                encoding="utf-8",
            )
            ignored = repo / "later.txt"
            ignored.write_text("local ignored", encoding="utf-8")
            rebase_dir = Path(
                git_command(repo, "rev-parse", "--git-path", "rebase-merge")
            )
            if not rebase_dir.is_absolute():
                rebase_dir = repo / rebase_dir
            rebase_dir.mkdir()
            (rebase_dir / "orig-head").write_text(original_head, encoding="ascii")
            (rebase_dir / "onto").write_text(onto, encoding="ascii")
            (rebase_dir / "git-rebase-todo").write_text(
                f"pick {original_head} Next feature commit\n", encoding="utf-8"
            )
            for arguments in (
                ("-c", "core.editor=true", "rebase", "--continue"),
                ("rebase", "--skip"),
                ("rebase", "--abort"),
            ):
                with self.subTest(arguments=arguments), mock.patch(
                    "loop_manager.command",
                    side_effect=AssertionError("Git write started"),
                ):
                    result = loop_manager.git(repo, *arguments, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("later.txt", result.stdout)
                    self.assertEqual(ignored.read_text(encoding="utf-8"), "local ignored")
            ignored.unlink()
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n*.cache\n",
                encoding="utf-8",
            )
            (repo / "unrelated.cache").write_text("keep", encoding="utf-8")
            with mock.patch(
                "loop_manager.command",
                return_value=subprocess.CompletedProcess([], 0, "continued"),
            ) as command_call:
                allowed = loop_manager.git(
                    repo, "-c", "core.editor=true", "rebase", "--continue",
                    check=False,
                )
            self.assertEqual(allowed.returncode, 0)
            command_call.assert_called_once()

    def test_rebase_resume_ignores_paths_from_already_replayed_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            onto = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "-b", "feature")
            (repo / "old.txt").write_text("tracked", encoding="utf-8")
            git_command(repo, "add", "old.txt")
            git_command(repo, "commit", "-m", "Add old path")
            first_commit = git_command(repo, "rev-parse", "HEAD")
            (repo / "old.txt").unlink()
            git_command(repo, "add", "-u")
            git_command(repo, "commit", "-m", "Remove old path")
            second_commit = git_command(repo, "rev-parse", "HEAD")
            (repo / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "Change app")
            original_head = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "main")
            exclude = self._git_metadata_path(repo, "info/exclude")
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nold.txt\n",
                encoding="utf-8",
            )
            ignored = repo / "old.txt"
            ignored.write_text("local ignored", encoding="utf-8")
            rebase_dir = Path(git_command(repo, "rev-parse", "--git-path", "rebase-merge"))
            if not rebase_dir.is_absolute():
                rebase_dir = repo / rebase_dir
            rebase_dir.mkdir()
            (rebase_dir / "orig-head").write_text(original_head, encoding="ascii")
            (rebase_dir / "onto").write_text(onto, encoding="ascii")
            (rebase_dir / "done").write_text(
                f"pick {first_commit} Add old path\npick {second_commit} Remove old path\n",
                encoding="utf-8",
            )
            (rebase_dir / "git-rebase-todo").write_text(
                f"pick {original_head} Change app\n", encoding="utf-8"
            )
            for arguments in (("rebase", "--continue"), ("rebase", "--skip")):
                with self.subTest(arguments=arguments), mock.patch(
                    "loop_manager.command",
                    return_value=subprocess.CompletedProcess([], 0, "resumed"),
                ) as command_call:
                    result = loop_manager.git(repo, *arguments, check=False)
                    self.assertEqual(result.returncode, 0)
                    command_call.assert_called_once()
                    self.assertEqual(ignored.read_text(encoding="utf-8"), "local ignored")

    def test_apply_rebase_resume_checks_only_remaining_patch_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            onto = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "-b", "feature")
            for filename in ("old.txt", "later.txt"):
                (repo / filename).write_text("tracked", encoding="utf-8")
                git_command(repo, "add", filename)
                git_command(repo, "commit", "-m", f"Add {filename}")
            original_head = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "main")
            exclude = self._git_metadata_path(repo, "info/exclude")
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nold.txt\nlater.txt\n",
                encoding="utf-8",
            )
            (repo / "old.txt").write_text("old ignored", encoding="utf-8")
            (repo / "later.txt").write_text("later ignored", encoding="utf-8")
            rebase_dir = Path(git_command(repo, "rev-parse", "--git-path", "rebase-apply"))
            if not rebase_dir.is_absolute():
                rebase_dir = repo / rebase_dir
            rebase_dir.mkdir()
            for name, value in (
                ("orig-head", original_head), ("onto", onto),
                ("next", "2"), ("last", "2"),
            ):
                (rebase_dir / name).write_text(value, encoding="ascii")
            with mock.patch(
                "loop_manager.command",
                side_effect=AssertionError("Git write started"),
            ):
                blocked = loop_manager.git(repo, "rebase", "--continue", check=False)
            self.assertIn("later.txt", blocked.stdout)
            with mock.patch(
                "loop_manager.command",
                return_value=subprocess.CompletedProcess([], 0, "skipped"),
            ) as command_call:
                skipped = loop_manager.git(repo, "rebase", "--skip", check=False)
            self.assertEqual(skipped.returncode, 0)
            command_call.assert_called_once()
            (repo / "later.txt").unlink()
            with mock.patch(
                "loop_manager.command",
                return_value=subprocess.CompletedProcess([], 0, "resumed"),
            ) as command_call:
                allowed = loop_manager.git(repo, "rebase", "--continue", check=False)
            self.assertEqual(allowed.returncode, 0)
            command_call.assert_called_once()
            self.assertEqual((repo / "old.txt").read_text(encoding="utf-8"), "old ignored")
