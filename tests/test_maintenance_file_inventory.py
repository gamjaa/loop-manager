import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import maintenance_file_inventory as source_inventory


class TrackedSourceInventoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def write(self, path, data, *, executable=False):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if executable:
            target.chmod(0o755)

    def publish(self):
        self.git("add", "-A")
        self.git("commit", "-m", "fixture")
        revision = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", revision)
        return revision

    def test_multilanguage_scripts_and_exclusions_are_classified_in_tree_order(self):
        files = {
            "main.go": b"package main\nfunc main() {}\n",
            "README.py": b"def documented_name(): pass\n",
            "src/engine.unknown_language": b"source code\n",
            "src/workflow.yaml": b"steps: []\n",
            "config/bootstrap.py": b"def configure(): pass\n",
            "tests/check.rs": b"fn check() {}\n",
            "bin/deploy": b"#!/bin/sh\nexit 0\n",
            "scripts/verify.sh": b"#!/bin/sh\nexit 0\n",
            "package.json": b'{"scripts":{"test":"true"}}\n',
        }
        for path, content in files.items():
            self.write(path, content, executable=path == "bin/deploy")
        for path in (
            "README.md", "docs/PRODUCT.md", "docs/plans/G-001.md",
            "docs/plans/archive/G-000.md", "build/generated.go", "node_modules/vendor.js",
            "config/settings.toml", "assets/logo.png",
        ):
            self.write(path, b"excluded\n")
        revision = self.publish()
        inventory = source_inventory.tracked_source_inventory(self.repo, "main", revision)

        self.assertEqual(tuple(item.path for item in inventory.sources), tuple(sorted(files)))
        self.assertEqual(
            {item.path: item.content_hash for item in inventory.sources},
            {path: hashlib.sha256(data).hexdigest() for path, data in files.items()},
        )
        self.assertEqual(next(item.mode for item in inventory.sources if item.path == "bin/deploy"), "100755")
        self.assertEqual(len(inventory.classifications), len(files) + 8)
        self.assertTrue(all(item.reason for item in inventory.classifications))
        self.assertEqual({item.disposition for item in inventory.classifications}, {"source", "excluded"})
        self.write("main.go", b"dirty local bytes\n")
        self.assertEqual(
            source_inventory.tracked_source_inventory(self.repo, "main", revision), inventory
        )

    def test_ambiguous_and_unsupported_tracked_files_stop_inventory(self):
        self.write("mystery.dat", b"unknown purpose\n")
        revision = self.publish()
        with self.assertRaisesRegex(source_inventory.SourceInventoryError, "mystery.dat"):
            source_inventory.tracked_source_inventory(self.repo, "main", revision)
        (self.repo / "mystery.dat").unlink()
        (self.repo / "src").mkdir()
        (self.repo / "src/link").symlink_to("missing")
        later = self.publish()
        with self.assertRaisesRegex(source_inventory.SourceInventoryError, "src/link"):
            source_inventory.tracked_source_inventory(self.repo, "main", later)

    def test_revision_drift_and_invalid_revision_fail_before_inventory(self):
        self.write("main.go", b"package main\n")
        first = self.publish()
        self.write("other.go", b"package main\n")
        second = self.publish()
        with self.assertRaisesRegex(source_inventory.SourceInventoryError, "revision"):
            source_inventory.tracked_source_inventory(self.repo, "main", first)
        with self.assertRaises(source_inventory.SourceInventoryError):
            source_inventory.tracked_source_inventory(self.repo, "main", "not-an-oid")
        self.assertEqual(source_inventory.tracked_source_inventory(self.repo, "main", second).revision, second)

    def test_inconsistent_tree_type_and_mode_cannot_silently_omit_source(self):
        self.write("main.go", b"package main\n")
        revision = self.publish()
        actual_git = source_inventory._git

        def malformed_tree(repo, *args):
            if args[0] == "ls-tree":
                return b"100644 commit " + b"0" * 40 + b"\tmain.go\0"
            return actual_git(repo, *args)

        with mock.patch.object(source_inventory, "_git", side_effect=malformed_tree):
            with self.assertRaisesRegex(source_inventory.SourceInventoryError, "mode"):
                source_inventory.tracked_source_inventory(self.repo, "main", revision)

        def malformed_path(repo, *args):
            if args[0] == "ls-tree":
                return b"100644 blob " + b"0" * 40 + b"\tsrc/../main.go\0"
            return actual_git(repo, *args)

        with mock.patch.object(source_inventory, "_git", side_effect=malformed_path):
            with self.assertRaisesRegex(source_inventory.SourceInventoryError, "path|경로"):
                source_inventory.tracked_source_inventory(self.repo, "main", revision)


if __name__ == "__main__":
    unittest.main()
