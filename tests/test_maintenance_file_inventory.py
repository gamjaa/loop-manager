import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.maintenance import maintenance_file_inventory as source_inventory


class TrackedSourceInventoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        # Exercise Git mode fixtures even when filesystem permissions are ignored.
        self.git("config", "core.filemode", "false")

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
            self.git("add", "--chmod=+x", "--", path)

    def publish(self, *, staged=False):
        if not staged:
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
        self.git("add", "-A")
        # Store the link target as a Git blob without requiring OS symlink privileges.
        self.write("link-target", b"missing")
        target_blob = self.git("hash-object", "-w", "--", "link-target")
        self.git("update-index", "--add", "--cacheinfo", "120000", target_blob, "src/link")
        later = self.publish(staged=True)
        self.assertEqual(
            self.git("ls-tree", later, "--", "src/link"),
            f"120000 blob {target_blob}\tsrc/link",
        )
        with self.assertRaisesRegex(source_inventory.SourceInventoryError, "src/link"):
            source_inventory.tracked_source_inventory(self.repo, "main", later)

    def test_root_dependency_manifest_is_excluded_but_scripts_build_is_source(self):
        self.write("requirements.txt", b"pytest==8.0\n")
        self.write("requirements-dev.txt", b"ruff==0.1\n")
        self.write("scripts/build", b"#!/bin/sh\nexit 0\n", executable=True)
        self.write("build/generated.go", b"package generated\n")
        revision = self.publish()

        inventory = source_inventory.tracked_source_inventory(self.repo, "main", revision)

        self.assertEqual([source.path for source in inventory.sources], ["scripts/build"])
        self.assertEqual(inventory.sources[0].mode, "100755")
        classified = {item.path: item for item in inventory.classifications}
        self.assertEqual(classified["requirements.txt"].disposition, "excluded")
        self.assertEqual(classified["requirements-dev.txt"].reason, "root dependency manifest")
        self.assertEqual(classified["build/generated.go"].disposition, "excluded")

    def test_root_placement_inventory_keeps_fixed_paths_and_marks_root_source_for_review(self):
        files = {
            ".agents/guide.txt": b"agent guidance\n",
            "AGENTS.md": b"# guidance\n",
            "README.md": b"# project\n",
            "config/settings.ini": b"[app]\n",
            "docs/PRODUCT.md": b"# Product\n",
            "tests/check.py": b"assert True\n",
            "engine.py": b"def run(): pass\n",
            "scratch.png": b"\x89PNG\x00asset",
        }
        for path, content in files.items():
            self.write(path, content)
        revision = self.publish()

        inventory = source_inventory.tracked_source_inventory(self.repo, "main", revision)
        placements = {
            item.path: item for item in source_inventory.root_placement_inventory(inventory)
        }

        self.assertEqual(placements[".agents"].category, "tool_required")
        self.assertEqual(placements["AGENTS.md"].category, "tool_required")
        self.assertEqual(placements["docs"].category, "tool_required")
        self.assertEqual(placements["README.md"].category, "user_entry")
        self.assertEqual(placements["config"].category, "execution_build_deploy")
        self.assertEqual(placements["tests"].category, "execution_build_deploy")
        self.assertEqual(placements["scratch.png"].category, "uncertain")
        self.assertEqual(placements["engine.py"].category, "internal_implementation")
        self.assertIn("entry path, references, owner", placements["engine.py"].reason)

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


class InventoryCacheUnitTest(unittest.TestCase):
    def test_cached_inventory_checks_remote_but_does_not_reload_blobs(self):
        import sqlite3
        from unittest import mock
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        inventory = source_inventory.SourceInventory("a" * 40, (), ())
        with (mock.patch.object(source_inventory, "verify_remote_revision") as verify,
              mock.patch.object(source_inventory, "tracked_source_inventory", return_value=inventory) as read):
            for _ in range(3):
                self.assertEqual(source_inventory.cached_source_inventory(connection, Path("repo"), "main", inventory.revision), inventory)
            self.assertEqual(verify.call_count, 3)
            read.assert_called_once()
            verify.side_effect = source_inventory.SourceInventoryError("changed ref")
            with self.assertRaises(source_inventory.SourceInventoryError):
                source_inventory.cached_source_inventory(connection, Path("repo"), "main", inventory.revision)
            self.assertEqual(read.call_count, 1)
            verify.side_effect = None
            read.return_value = source_inventory.SourceInventory("b" * 40, (), ())
            source_inventory.cached_source_inventory(connection, Path("repo"), "main", "b" * 40)
            self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
