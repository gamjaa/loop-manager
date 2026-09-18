import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_worker_level_boundaries_and_cap(self):
        cases = [
            ("low", "low", 1),
            ("low", "medium", 2),
            ("low", "high", 3),
            ("low", "critical", 4),
            ("medium", "low", 3),
            ("medium", "medium", 4),
            ("medium", "high", 5),
            ("medium", "critical", 6),
            ("high", "low", 5),
            ("high", "medium", 6),
            ("high", "high", 7),
            ("high", "critical", 7),
        ]

        for complexity, risk, expected in cases:
            with self.subTest(complexity=complexity, risk=risk):
                plan = {"complexity": complexity, "risk": risk}
                self.assertEqual(loop_manager.worker_level(plan), expected)

    def test_worker_profile_resolves_level_alias(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        self.assertEqual(loop_manager.worker_profile(config, 1), ("gpt-5.6-luna", "medium"))
        self.assertEqual(loop_manager.worker_profile(config, 4), ("gpt-5.6-terra", "high"))
        self.assertEqual(loop_manager.worker_profile(config, 7), ("gpt-5.6-sol", "xhigh"))

    def test_worker_profile_rejects_invalid_level(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        for level in (0, 8, True):
            with self.subTest(level=level):
                with self.assertRaisesRegex(RuntimeError, "Worker level"):
                    loop_manager.worker_profile(config, level)

    def test_worker_profile_rejects_invalid_configuration(self):
        cases = [
            (
                "missing-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel2 = fast,medium\n",
                "level1 설정이 없습니다",
            ),
            (
                "empty-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 =\n",
                "Model alias/Effort 설정이 비어",
            ),
            (
                "malformed-profile",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast\n",
                "Model alias,Effort",
            ),
            (
                "empty-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = ,medium\n",
                "Model alias 설정이 비어",
            ),
            (
                "empty-effort",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,\n",
                "Effort 설정이 비어",
            ),
            (
                "unsupported-effort",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,invalid\n",
                "Effort를 Codex CLI가 지원하지 않습니다: invalid",
            ),
            (
                "missing-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = strong,high\n",
                "Model alias가 .* 없습니다",
            ),
            (
                "empty-model",
                "[codex.models]\nfast =\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model 설정이 비어",
            ),
            (
                "malformed-ini",
                "codex.models]\nfast = model-fast\n",
                "설정 파일 형식이 올바르지 않습니다",
            ),
        ]

        with tempfile.TemporaryDirectory() as directory:
            for name, contents, message in cases:
                with self.subTest(name=name):
                    config = Path(directory) / f"{name}.ini"
                    config.write_text(contents, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, message):
                        loop_manager.worker_profile(config, 1)

    def test_worker_codex_uses_level_model_and_effort(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch("loop_manager.subprocess.run", return_value=completed) as run:
                code, message = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        self.assertEqual((code, message), (0, ""))
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-m") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="high"', arguments)

    def test_unsupported_worker_effort_fails_before_codex_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,invalid\n",
                encoding="utf-8",
            )
            with mock.patch("loop_manager.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Effort를 Codex CLI가 지원하지 않습니다"):
                    loop_manager.run_codex(
                        root / "worktree",
                        root / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=1,
                    )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
