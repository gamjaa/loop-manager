import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager


def make_catalog_root(directory: str) -> Path:
    root = Path(directory)
    (root / "docs/plans").mkdir(parents=True)
    (root / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
    return root


def write_execplan(
    root: Path,
    plan_id: str,
    title: str,
    *,
    sources: tuple[str, ...] = ("docs/PRODUCT.md",),
    depends_on: tuple[str, ...] = (),
    complexity: str = "low",
    risk: str = "low",
) -> Path:
    source_lines = "sources: []" if not sources else "sources:\n" + "".join(
        f"  - {source}\n" for source in sources
    ).rstrip("\n")
    dependency_lines = "depends_on: []" if not depends_on else "depends_on:\n" + "".join(
        f"  - {dependency}\n" for dependency in depends_on
    ).rstrip("\n")
    plan = root / "docs/plans" / f"[{plan_id}] {title}.md"
    plan.write_text(
        f"""---
id: {plan_id}
title: {title}
{source_lines}
{dependency_lines}
complexity: {complexity}
risk: {risk}
---

## Purpose

Test plan.
""",
        encoding="utf-8",
    )
    return plan


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

    def test_execplan_catalog_is_deterministic_and_indexed_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            requirement = root / "docs/requirements/[R-001] Active.md"
            requirement.parent.mkdir()
            requirement.write_text("# Requirement\n", encoding="utf-8")
            write_execplan(root, "G-003", "Third", depends_on=("G-002",))
            write_execplan(root, "G-001", "First")
            write_execplan(
                root,
                "G-002",
                "Second",
                sources=("docs/requirements/[R-001] Active.md",),
                depends_on=("G-001",),
            )
            nested = root / "docs/plans/archive/[G-999] Ignored.md"
            nested.parent.mkdir()
            nested.write_text("not an active plan", encoding="utf-8")

            catalog = loop_manager.load_execplan_catalog(root)

        self.assertEqual(list(catalog), ["G-001", "G-002", "G-003"])
        self.assertEqual(catalog["G-002"]["title"], "Second")

    def test_execplan_catalog_rejects_duplicate_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "First")
            write_execplan(root, "G-001", "Duplicate")

            with self.assertRaisesRegex(RuntimeError, "중복 ExecPlan id.*G-001"):
                loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_restricts_sources_to_active_product_inputs(self):
        invalid_sources = (
            (),
            ("/docs/PRODUCT.md",),
            ("../PRODUCT.md",),
            ("docs/requirements/archive/Old.md",),
            ("README.md",),
            ("docs/requirements/Missing.md",),
        )
        for sources in invalid_sources:
            with self.subTest(sources=sources), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                (root / "README.md").write_text("# Readme\n", encoding="utf-8")
                archived = root / "docs/requirements/archive/Old.md"
                archived.parent.mkdir(parents=True)
                archived.write_text("# Old\n", encoding="utf-8")
                write_execplan(root, "G-001", "Invalid Source", sources=sources)

                with self.assertRaisesRegex(RuntimeError, "ExecPlan source"):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_validates_fixed_metadata(self):
        cases = (
            ("id", "BAD", "Plan", "low", "low", "id는 G-000"),
            ("filename", "G-001", "Plan", "low", "low", "파일명"),
            ("complexity", "G-001", "Plan", "tiny", "low", "complexity"),
            ("risk", "G-001", "Plan", "low", "extreme", "risk"),
            ("keys", "G-001", "Plan", "low", "low", "frontmatter key"),
        )
        for name, plan_id, title, complexity, risk, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                plan = write_execplan(
                    root, plan_id, title, complexity=complexity, risk=risk
                )
                if name == "filename":
                    plan.rename(plan.with_name("[G-001] Wrong.md"))
                elif name == "keys":
                    contents = plan.read_text(encoding="utf-8")
                    plan.write_text(
                        contents.replace("risk: low\n", "risk: low\nstatus: READY\n"),
                        encoding="utf-8",
                    )

                with self.assertRaisesRegex(RuntimeError, message):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_invalid_and_missing_dependencies(self):
        cases = (("G-1", "depends_on ID 형식"), ("G-999", "존재하지 않는 depends_on"))
        for dependency, message in cases:
            with self.subTest(dependency=dependency), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                write_execplan(root, "G-001", "Plan", depends_on=(dependency,))

                with self.assertRaisesRegex(RuntimeError, message):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_self_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "Plan", depends_on=("G-001",))

            with self.assertRaisesRegex(RuntimeError, r"G-001 -> G-001"):
                loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_multistep_cycle_with_all_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "First", depends_on=("G-003",))
            write_execplan(root, "G-002", "Second", depends_on=("G-001",))
            write_execplan(root, "G-003", "Third", depends_on=("G-002",))

            with self.assertRaises(RuntimeError) as raised:
                loop_manager.load_execplan_catalog(root)

        message = str(raised.exception)
        self.assertIn("순환 ExecPlan 의존성", message)
        for plan_id in ("G-001", "G-002", "G-003"):
            self.assertIn(plan_id, message)

    def test_selected_execplan_requires_valid_whole_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            selected = write_execplan(root, "G-001", "Selected")
            write_execplan(root, "G-002", "Broken", depends_on=("G-999",))
            relative_path = selected.relative_to(root).as_posix()

            with self.assertRaisesRegex(RuntimeError, "G-002.*G-999"):
                loop_manager.validate_execplan(root, relative_path)

    def test_role_resolves_model_alias(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        with mock.patch(
            "loop_manager.codex_model_efforts",
            return_value={"gpt-5.6-sol": frozenset({"xhigh"})},
        ):
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

        model_efforts = {
            "gpt-5.6-luna": frozenset({"medium"}),
            "gpt-5.6-terra": frozenset({"high"}),
            "gpt-5.6-sol": frozenset({"xhigh"}),
        }
        with mock.patch("loop_manager.codex_model_efforts", return_value=model_efforts):
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
                "[codex.models]\nfast = gpt-5.6-sol\n[codex.worker]\nlevel1 = fast,minimal\n",
                "Model/Effort 조합을 Codex CLI가 지원하지 않습니다: gpt-5.6-sol,minimal",
            ),
            (
                "unsupported-model",
                "[codex.models]\nfast = unavailable\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model을 Codex CLI가 지원하지 않습니다: unavailable",
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
            (
                "malformed-profile-interpolation",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,%\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.worker\] level1",
            ),
            (
                "malformed-model-interpolation",
                "[codex.models]\nfast = model-%\n[codex.worker]\nlevel1 = fast,medium\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.models\] fast",
            ),
        ]

        with tempfile.TemporaryDirectory() as directory:
            for name, contents, message in cases:
                with self.subTest(name=name):
                    config = Path(directory) / f"{name}.ini"
                    config.write_text(contents, encoding="utf-8")
                    with mock.patch(
                        "loop_manager.codex_model_efforts",
                        return_value={
                            "gpt-5.6-sol": frozenset(
                                {"low", "medium", "high", "xhigh", "max", "ultra"}
                            )
                        },
                    ):
                        with self.assertRaisesRegex(RuntimeError, message):
                            loop_manager.worker_profile(config, 1)

    def test_codex_model_efforts_reads_effective_catalog_for_worktree(self):
        catalog = {
            "models": [
                {
                    "slug": "gpt-reserve",
                    "supported_reasoning_levels": [
                        {"effort": "max"}, {"effort": "ultra"}
                    ],
                }
            ]
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(catalog))
        worktree = Path("/project/worktree")
        with mock.patch("loop_manager.command", return_value=completed) as command:
            efforts = loop_manager.codex_model_efforts(worktree)

        self.assertEqual(efforts, {"gpt-reserve": frozenset({"max", "ultra"})})
        self.assertEqual(command.call_args.args[0], ["codex", "debug", "models"])
        self.assertEqual(command.call_args.kwargs["cwd"], worktree)

    def test_run_codex_validates_profile_in_worker_catalog(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-terra": frozenset({"high"})},
            ) as model_efforts, mock.patch(
                "loop_manager.subprocess.run", return_value=completed
            ):
                loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        model_efforts.assert_called_once_with(worktree)

    def test_profiles_validate_model_specific_efforts(self):
        model_efforts = {
            "gpt-5.6-sol": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
            "gpt-5.6-luna": frozenset({"low", "medium", "high", "xhigh", "max"}),
        }
        contents = (
            "[codex.models]\n"
            "strong = gpt-5.6-sol\n"
            "fast = gpt-5.6-luna\n"
            "[codex.roles]\n"
            "planner = strong,max\n"
            "reviewer = strong,ultra\n"
            "[codex.worker]\n"
            "level1 = fast,max\n"
            "level2 = strong,minimal\n"
            "level3 = fast,ultra\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.ini"
            config.write_text(contents, encoding="utf-8")
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value=model_efforts,
            ):
                self.assertEqual(loop_manager.role(config, "planner"), ("gpt-5.6-sol", "max"))
                self.assertEqual(loop_manager.role(config, "reviewer"), ("gpt-5.6-sol", "ultra"))
                self.assertEqual(loop_manager.worker_profile(config, 1), ("gpt-5.6-luna", "max"))
                for level in (2, 3):
                    with self.subTest(level=level):
                        with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                            loop_manager.worker_profile(config, level)

    def test_worker_codex_uses_level_model_and_effort(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-terra": frozenset({"high"})},
            ), mock.patch("loop_manager.subprocess.run", return_value=completed) as run:
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
                "[codex.models]\nstrong = gpt-5.6-sol\n[codex.worker]\nlevel1 = strong,minimal\n",
                encoding="utf-8",
            )
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={
                    "gpt-5.6-sol": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
                },
            ), mock.patch("loop_manager.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
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
