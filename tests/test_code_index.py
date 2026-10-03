import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from loop_manager_internal.maintenance import code_index
from tests.support.maintenance import inventory


class CodeIndexTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)

    def build(self, contents, loaded=None, scope="remote"):
        def load(source):
            if loaded is not None:
                loaded.append(source.path)
            return contents[source.path]
        return code_index.build_index(self.connection, scope, inventory(contents), load)

    def test_incremental_parse_and_module_move_update_unchanged_importers(self):
        sources = {"pkg/consumer.py": b"from pkg import target\n", "pkg/target.py": b"def value():\n    return 1\n"}
        first = self.build(sources)
        self.assertEqual(first.records["pkg/consumer.py"]["facts"]["references"][0]["path"], "pkg/target.py")
        loaded = []
        unchanged = self.build(sources, loaded)
        self.assertEqual(loaded, [])
        self.assertEqual(unchanged.reused, 2)
        sources["pkg/moved.py"] = sources.pop("pkg/target.py")
        moved = self.build(sources, loaded)
        self.assertEqual(loaded, ["pkg/moved.py"])
        self.assertEqual(moved.records["pkg/consumer.py"]["facts"]["references"], [])
        self.assertIn("unresolved_internal_import:pkg.target", moved.records["pkg/consumer.py"]["facts"]["uncertainties"])
        self.assertNotIn("pkg/target.py", moved.records)

    def test_version_and_hash_invalidate_cache_without_review_state(self):
        sources = {"a.py": b"def value():\n    return 1\n"}
        self.build(sources)
        self.connection.execute("UPDATE static_code_index SET version = 0")
        loaded = []
        self.build(sources, loaded)
        self.assertEqual(loaded, ["a.py"])
        sources["a.py"] = b"def value():\n    return 2\n"
        index = self.build(sources)
        lines = index.serialize().splitlines()
        self.assertEqual(json.loads(lines[0]), {"version": 2, "kind": "static", "analysis_version": 1})
        self.assertNotIn("responsibility", json.loads(lines[1])["facts"])
        self.assertEqual(index.serialize(), self.build(sources).serialize())

    def test_boundary_and_unknowns_are_explicit(self):
        cases = {"pkg/storage.py": b"def save():\n    return 1\n", "bad.py": b"def broken(\n",
                 "dyn.py": b"import importlib\nimportlib.import_module(name)\n", "scripts/run.sh": b"echo ready\n"}
        index = self.build(cases)
        self.assertTrue(index.records["pkg/storage.py"]["facts"]["boundary"])
        self.assertEqual(index.records["bad.py"]["facts"]["uncertainties"], ["parse_failed"])
        self.assertIn("dynamic_reference", index.records["dyn.py"]["facts"]["uncertainties"])
        self.assertIn("unsupported_source_format", index.records["scripts/run.sh"]["facts"]["uncertainties"])

    def test_cache_rejects_wrong_source_bytes(self):
        source = inventory({"a.py": b"return expected"})
        with self.assertRaises(ValueError):
            code_index.build_index(self.connection, "scope", source, lambda _: b"different")

    def test_deleted_private_symbol_invalidates_unchanged_module_consumer(self):
        sources = {"pkg/consumer.py": b"from pkg import target\ndef consume():\n    return target._helper()\n",
                   "pkg/target.py": b"def _helper():\n    return 1\n"}
        first = self.build(sources)
        self.assertEqual(first.records["pkg/consumer.py"]["facts"]["uncertainties"], [])
        sources["pkg/target.py"] = b"def _renamed():\n    return 1\n"
        updated = self.build(sources)
        self.assertIn("unverified_module_attribute:pkg/target.py:_helper", updated.records["pkg/consumer.py"]["facts"]["uncertainties"])

    def test_class_field_changes_are_in_the_public_contract_projection(self):
        before = code_index.parse_source("model.py", b"class Record:\n    value: int = 1\n")
        after = code_index.parse_source("model.py", b"class Record:\n    value: str = 'one'\n")
        self.assertNotEqual(before["contracts"], after["contracts"])

    def test_public_constructor_signature_changes_are_contract_changes(self):
        before = code_index.parse_source("model.py", b"class Record:\n    def __init__(self, value):\n        self.value = value\n")
        after = code_index.parse_source("model.py", b"class Record:\n    def __init__(self, value, owner):\n        self.value = value\n")
        self.assertNotEqual(before["contracts"], after["contracts"])

    def test_worktree_query_reads_dirty_and_new_files_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.py").write_text("from pkg import target\n")
            (repo / "pkg").mkdir()
            (repo / "pkg/target.py").write_text("def current():\n    return 1\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            (repo / "pkg/target.py").write_text("def updated():\n    return 2\n")
            before = code_index.git(repo, "status", "--porcelain")
            result = code_index.query_worktree(repo, ["a.py", "pkg/target.py"])
            self.assertEqual(result["records"]["a.py"]["facts"]["references"][0]["path"], "pkg/target.py")
            self.assertEqual(result["records"]["pkg/target.py"]["facts"]["symbols"][0]["name"], "updated")
            self.assertEqual(before, code_index.git(repo, "status", "--porcelain"))
            (repo / "new.py").write_text("def new():\n    pass\n")
            self.assertIn("new.py", code_index.query_worktree(repo, ["new.py"])["records"])
            with self.assertRaises(ValueError):
                code_index.query_worktree(repo, ["../outside.py"])
            (repo / "link.py").symlink_to(repo / "a.py")
            with self.assertRaises(ValueError):
                code_index.query_worktree(repo, ["link.py"])


if __name__ == "__main__":
    unittest.main()
