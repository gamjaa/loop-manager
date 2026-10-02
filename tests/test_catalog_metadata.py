import tempfile
import unittest
import loop_manager
from tests.support.catalog import make_catalog_root, write_execplan


class CatalogMetadataTest(unittest.TestCase):
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
            nested = root / "docs/plans/archive/nested/[G-999] Ignored.md"
            nested.parent.mkdir(parents=True)
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
