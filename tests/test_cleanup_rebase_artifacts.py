import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, make_remote_repository


class CleanupRebaseArtifactsTest(unittest.TestCase):
    def test_rebase_keeps_ignored_file_that_overlaps_target(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            git_command(repo, "checkout", "-b", "target")
            (repo / "collide.txt").write_text("tracked", encoding="utf-8")
            git_command(repo, "add", "collide.txt")
            git_command(repo, "commit", "-m", "Add target file")
            git_command(repo, "checkout", "main")
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
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
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
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
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
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
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
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
