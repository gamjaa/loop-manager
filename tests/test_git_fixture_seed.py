"""초기 Git fixture 재사용과 파일 격리의 최소 unit 회귀."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import git as fixture_git


class GitFixtureSeedTests(unittest.TestCase):
    def setUp(self):
        fixture_git._remote_repository_seed.cache_clear()
        self.addCleanup(fixture_git._remote_repository_seed.cache_clear)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.seed = SimpleNamespace(name=str(self.root / "seed"), cleanup=mock.Mock())
        Path(self.seed.name).mkdir()
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.initialize = patches.enter_context(mock.patch.object(
            fixture_git, "_initialize_remote_repository", side_effect=self.prepare_seed,
        ))
        self.temporary = patches.enter_context(mock.patch.object(
            fixture_git.tempfile, "TemporaryDirectory", return_value=self.seed,
        ))
        self.on_exit = patches.enter_context(mock.patch.object(fixture_git.atexit, "register"))
        self.git = patches.enter_context(mock.patch.object(fixture_git, "git_command", return_value=""))

    def prepare_seed(self, directory):
        root = Path(directory)
        self.files = {
            "remote.git/refs/heads/main": b"initial-ref\n",
            "remote.git/objects/seed": b"initial-object",
            "repo/.git/refs/heads/main": b"initial-ref\n",
            "repo/.git/index": b"initial-index",
            "repo/.git/config": b"initial-config",
            "repo/.git/objects/seed": b"initial-object",
            "repo/src/app.py": b"VALUE = 1\n",
            "repo/docs/requirements/Active.md": b"# Active\n",
        }
        for relative, contents in self.files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return root / "repo"

    def make_repository(self, name):
        parent = self.root / name
        parent.mkdir()
        return fixture_git.make_remote_repository(str(parent))

    def test_repeated_calls_initialize_once_and_rebind_each_destination(self):
        first = self.make_repository("first")
        second = self.make_repository("second")
        self.initialize.assert_called_once_with(self.seed.name)
        self.temporary.assert_called_once_with(prefix="loop-manager-test-seed-")
        self.on_exit.assert_called_once_with(self.seed.cleanup)
        self.git.assert_has_calls([
            mock.call(first, "init", "--initial-branch=main"),
            mock.call(first, "remote", "set-url", "origin", str(first.parent / "remote.git")),
            mock.call(second, "init", "--initial-branch=main"),
            mock.call(second, "remote", "set-url", "origin", str(second.parent / "remote.git")),
        ])
        self.assertEqual(self.git.call_count, 4)
        for relative, contents in self.files.items():
            self.assertEqual((Path(self.seed.name) / relative).read_bytes(), contents)

    def test_files_refs_index_config_and_objects_are_independent(self):
        first = self.make_repository("first")
        second = self.make_repository("second")
        for relative, contents in self.files.items():
            with self.subTest(path=relative):
                (first.parent / relative).write_bytes(b"changed")
                self.assertEqual((second.parent / relative).read_bytes(), contents)
                self.assertEqual((Path(self.seed.name) / relative).read_bytes(), contents)

    def test_initialization_failure_is_cleaned_and_not_cached(self):
        self.initialize.side_effect = RuntimeError("initialization failed")
        with self.assertRaisesRegex(RuntimeError, "initialization failed"):
            fixture_git._remote_repository_seed()
        self.seed.cleanup.assert_called_once_with()
        self.on_exit.assert_not_called()
        self.assertEqual(fixture_git._remote_repository_seed.cache_info().currsize, 0)

        self.initialize.side_effect = self.prepare_seed
        self.assertIs(fixture_git._remote_repository_seed(), self.seed)
        self.assertEqual(self.initialize.call_count, 2)
        self.on_exit.assert_called_once_with(self.seed.cleanup)

    def test_copy_failure_preserves_seed_and_can_retry_elsewhere(self):
        self.make_repository("first")
        occupied = self.root / "occupied"
        (occupied / "repo").mkdir(parents=True)
        user_file = occupied / "repo" / "user.txt"
        user_file.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            fixture_git.make_remote_repository(str(occupied))
        self.assertEqual(user_file.read_bytes(), b"keep")
        for relative, contents in self.files.items():
            self.assertEqual((Path(self.seed.name) / relative).read_bytes(), contents)
        self.make_repository("retry")
        self.initialize.assert_called_once_with(self.seed.name)
