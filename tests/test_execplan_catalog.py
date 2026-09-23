import os
from pathlib import Path
import tempfile
import unittest

import execplan_catalog


def plan_text(
    plan_id: str,
    title: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> str:
    dependencies = "depends_on: []"
    if depends_on:
        dependencies = "depends_on:\n" + "\n".join(
            f"  - {dependency}" for dependency in depends_on
        )
    return f"""---
id: {plan_id}
title: {title}
sources:
  - docs/PRODUCT.md
{dependencies}
complexity: low
risk: low
priority: normal
---

## Purpose

Catalog fixture.
"""


class ExecPlanCatalogSnapshotTest(unittest.TestCase):
    def test_final_hash_compares_bytes_without_publication_hash(self):
        final = execplan_catalog.plan_content_hash("revised body\n")
        self.assertEqual(
            final,
            execplan_catalog.plan_content_hash(b"revised body\n"),
        )
        self.assertTrue(execplan_catalog.final_content_matches(final, final))
        self.assertFalse(execplan_catalog.final_content_matches(
            execplan_catalog.plan_content_hash(b"published body\n"), final,
        ))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "docs/plans/archive").mkdir(parents=True)
        (self.root / "docs/PRODUCT.md").write_text(
            "# Product\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _write(
        self,
        plan_id: str,
        title: str,
        *,
        archived: bool = False,
        depends_on: tuple[str, ...] = (),
    ) -> Path:
        directory = self.root / "docs/plans"
        if archived:
            directory /= "archive"
        path = directory / f"[{plan_id}] {title}.md"
        path.write_text(
            plan_text(plan_id, title, depends_on=depends_on), encoding="utf-8"
        )
        return path

    def test_separates_active_and_historical_while_validating_full_graph(self):
        historical = self._write("G-004", "Merged Base", archived=True)
        self._write(
            "G-007", "Active Followup", depends_on=("G-004",)
        )

        snapshot = execplan_catalog.load_execplan_snapshot(self.root)

        self.assertEqual(tuple(snapshot.active), ("G-007",))
        self.assertEqual(tuple(snapshot.archived), ("G-004",))
        self.assertEqual(snapshot.next_plan_id, "G-008")
        self.assertEqual(
            snapshot.archived["G-004"].path,
            historical.relative_to(self.root).as_posix(),
        )
        self.assertEqual(
            snapshot.archived["G-004"].active_path,
            "docs/plans/[G-004] Merged Base.md",
        )
        self.assertEqual(tuple(snapshot.active_metadata()), ("G-007",))

    def test_rejects_identity_collision_across_active_and_archive(self):
        self._write("G-003", "Original")
        self._write("G-003", "Archived Copy", archived=True)

        with self.assertRaisesRegex(RuntimeError, "중복 ExecPlan id.*G-003"):
            execplan_catalog.load_execplan_snapshot(self.root)

    def test_ignores_nested_directories_and_symlinks_as_plan_files(self):
        self._write("G-001", "Active")
        nested = self.root / "docs/plans/archive/nested"
        nested.mkdir()
        (nested / "[G-900] Nested.md").write_text(
            plan_text("G-900", "Nested"), encoding="utf-8"
        )
        target = self.root / "outside.md"
        target.write_text(plan_text("G-901", "Linked"), encoding="utf-8")
        link = self.root / "docs/plans/archive/[G-901] Linked.md"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink를 만들 수 없는 환경입니다")

        snapshot = execplan_catalog.load_execplan_snapshot(self.root)

        self.assertEqual(tuple(snapshot.all), ("G-001",))

    def test_detects_cycle_crossing_active_and_archive(self):
        self._write(
            "G-001", "Archived", archived=True, depends_on=("G-002",)
        )
        self._write("G-002", "Active", depends_on=("G-001",))

        with self.assertRaisesRegex(
            RuntimeError, "G-001.*G-002.*G-001|G-002.*G-001.*G-002"
        ):
            execplan_catalog.load_execplan_snapshot(self.root)

    def test_revision_pinned_snapshot_reports_missing_source_and_nonregular_plan(self):
        relative_path = "docs/plans/[G-001] Active.md"
        text = plan_text("G-001", "Active").replace(
            "docs/PRODUCT.md", "docs/requirements/Missing.md"
        )
        with self.assertRaises(execplan_catalog.ExecPlanCatalogError) as caught:
            execplan_catalog.snapshot_from_documents(
                {relative_path: text},
                validate_sources=True,
                available_regular_paths=frozenset({relative_path}),
                revision_sha="a" * 40,
            )
        self.assertEqual(caught.exception.code, "missing-source")
        self.assertEqual(caught.exception.path, relative_path)
        self.assertEqual(
            caught.exception.source, "docs/requirements/Missing.md"
        )

        with self.assertRaises(execplan_catalog.ExecPlanCatalogError) as caught:
            execplan_catalog.snapshot_from_documents(
                {},
                invalid_plan_entries={relative_path: "symlink"},
                revision_sha="b" * 40,
            )
        self.assertEqual(caught.exception.code, "non-regular-plan")
        self.assertEqual(caught.exception.path, relative_path)

    def test_revision_pinned_definitions_share_one_revision_token(self):
        documents = {
            "docs/plans/[G-001] First.md": plan_text("G-001", "First"),
            "docs/plans/[G-002] Second.md": plan_text(
                "G-002", "Second", depends_on=("G-001",)
            ),
        }
        snapshot = execplan_catalog.snapshot_from_documents(
            documents, revision_sha="c" * 40
        )

        definitions = snapshot.definitions()

        self.assertEqual(tuple(definitions), ("G-001", "G-002"))
        self.assertEqual(definitions["G-002"].revision_sha, "c" * 40)
        self.assertEqual(definitions["G-002"].depends_on, ("G-001",))


if __name__ == "__main__":
    unittest.main()
