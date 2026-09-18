import sqlite3
import unittest

import loop_manager


class LoopManagerTest(unittest.TestCase):
    def test_codex_uses_mutually_exclusive_sandbox_options(self):
        self.assertEqual(loop_manager.codex_sandbox_arguments(False), ["--approve-for-me"])
        self.assertEqual(loop_manager.codex_sandbox_arguments(True), ["--sandbox", "read-only"])

    def test_schema_and_project_update(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")

        loop_manager.update_project(connection, "repo", phase="WORKING", active_sha="abc")

        row = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertEqual((row["phase"], row["active_sha"]), ("WORKING", "abc"))

    def test_execplan_frontmatter_parser(self):
        values = loop_manager.parse_frontmatter(
            """---
id: G-002
title: Small Task
sources:
  - docs/PRODUCT.md
depends_on:
  - G-001
complexity: low
risk: medium
---
"""
        )

        self.assertEqual(values["id"], "G-002")
        self.assertEqual(values["sources"], ["docs/PRODUCT.md"])
        self.assertEqual(values["depends_on"], ["G-001"])

    def test_current_execplan_is_valid(self):
        values = loop_manager.validate_execplan(
            loop_manager.ROOT,
            "docs/plans/[G-001] Minimal Bootstrap Runner.md",
        )

        self.assertEqual(values["id"], "G-001")

    def test_role_resolves_model_alias(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        self.assertEqual(loop_manager.role(config, "planner"), ("gpt-5.6-sol", "xhigh"))


if __name__ == "__main__":
    unittest.main()
