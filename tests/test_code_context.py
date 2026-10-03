from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.maintenance import code_context, code_index
from tests.support.maintenance import inventory


class CodeContextTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.command("init", "-q")
        self.command("config", "user.name", "Unit Test")
        self.command("config", "user.email", "test@example.com")
        self.sources = {"pkg/a.py": b"def _calculate():\n    return 1\n",
                        "pkg/b.py": b"from pkg import a\ndef consume():\n    return a._calculate()\n",
                        "tests/test_a.py": b"from pkg import a\ndef test_a():\n    assert a._calculate() == 1\n"}
        for path, content in self.sources.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.command("add", ".")
        self.command("commit", "-qm", "Initial source")
        sha = self.command("rev-parse", "HEAD").decode().strip()
        with closing(sqlite3.connect(":memory:")) as connection:
            self.index = code_index.build_index(connection, "repo", inventory(self.sources, sha), lambda source: self.sources[source.path])
        self.snapshot = code_context.export_snapshot(self.root / "context", self.repo, self.index)

    def command(self, *args):
        return code_index.git(self.repo, *args)

    def test_readonly_snapshot_reuses_facts_and_supplies_incoming_tests(self):
        before = self.snapshot.read_bytes()
        with mock.patch.object(code_index, "parse_source", side_effect=AssertionError("unchanged facts cannot be reparsed")):
            result = code_context.query_snapshot(self.repo, ["pkg/a.py"], self.snapshot)
        facts = result["records"]["pkg/a.py"]["facts"]
        self.assertEqual(facts["dependents"], ["pkg/b.py", "tests/test_a.py"])
        self.assertEqual(facts["related_tests"], ["tests/test_a.py"])
        self.assertEqual(before, self.snapshot.read_bytes())
        self.assertEqual(code_context.export_snapshot(self.root / "context", self.repo, self.index), self.snapshot)

    def test_dirty_overlay_new_consumer_and_symbol_change_refresh_relations(self):
        (self.repo / "pkg/a.py").write_text("def _renamed():\n    return 2\n")
        (self.repo / "tests/test_new.py").write_text("from pkg import a\nassert a._renamed() == 2\n")
        result = code_context.query_snapshot(self.repo, ["pkg/a.py", "pkg/b.py"], self.snapshot)
        self.assertEqual(result["records"]["pkg/a.py"]["facts"]["symbols"][0]["name"], "_renamed")
        self.assertIn("tests/test_new.py", result["records"]["pkg/a.py"]["facts"]["dependents"])
        self.assertIn("unverified_module_attribute:pkg/a.py:_calculate", result["records"]["pkg/b.py"]["facts"]["uncertainties"])

    def test_snapshot_from_different_repository_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        with self.assertRaises(ValueError):
            code_context.query_snapshot(other, ["pkg/a.py"], self.snapshot)

    def test_fixed_provider_clears_old_resolved_uncertainty_in_unchanged_consumer(self):
        (self.repo / "pkg/a.py").write_text("def _other():\n    return 1\n")
        self.command("add", ".")
        self.command("commit", "-qm", "Unverified consumer")
        contents = {path: (self.repo / path).read_bytes() for path in self.sources}
        sha = self.command("rev-parse", "HEAD").decode().strip()
        with closing(sqlite3.connect(":memory:")) as connection:
            index = code_index.build_index(connection, "repo", inventory(contents, sha), lambda source: contents[source.path])
        self.assertTrue(index.records["pkg/b.py"]["facts"]["uncertainties"])
        snapshot = code_context.export_snapshot(self.root / "context", self.repo, index)
        (self.repo / "pkg/a.py").write_bytes(self.sources["pkg/a.py"])
        result = code_context.query_snapshot(self.repo, ["pkg/b.py"], snapshot)
        self.assertEqual(result["records"]["pkg/b.py"]["facts"]["uncertainties"], [])

    def test_changed_base_updates_incoming_edges_even_when_worktree_is_clean(self):
        (self.repo / "pkg/b.py").write_text("def unrelated():\n    return 1\n")
        self.command("add", ".")
        self.command("commit", "-qm", "Change base relations")
        result = code_context.query_snapshot(self.repo, ["pkg/a.py"], self.snapshot)
        self.assertEqual(result["records"]["pkg/a.py"]["facts"]["dependents"], ["tests/test_a.py"])

    def test_registered_worktree_can_query_shared_snapshot_at_an_older_base(self):
        older_sha = self.command("rev-parse", "HEAD").decode().strip()
        (self.repo / "pkg/b.py").write_text("def unrelated():\n    return 1\n")
        self.command("add", ".")
        self.command("commit", "-qm", "Remove dependency")
        contents = {path: (self.repo / path).read_bytes() for path in self.sources}
        sha = self.command("rev-parse", "HEAD").decode().strip()
        with closing(sqlite3.connect(":memory:")) as connection:
            index = code_index.build_index(connection, "repo", inventory(contents, sha), lambda source: contents[source.path])
        snapshot = code_context.export_snapshot(self.root / "context", self.repo, index)
        worktree = self.root / "worker"
        self.command("worktree", "add", "--detach", str(worktree), older_sha)
        result = code_context.query_snapshot(worktree, ["pkg/a.py"], snapshot)
        self.assertEqual(result["records"]["pkg/a.py"]["facts"]["dependents"], ["pkg/b.py", "tests/test_a.py"])
        self.assertIn("pkg/b.py", result["parsed_paths"])


if __name__ == "__main__":
    unittest.main()
