import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
import loop_manager
from tests.support import catalog as catalog_fixtures
from tests.support.catalog import execplan_header


def plan_text(
    plan_id: str,
    title: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> str:
    return f"""{execplan_header(plan_id, title, depends_on=depends_on)}

## Purpose

Catalog fixture.
"""


class ExecPlanHeaderRenderingTest(unittest.TestCase):
    def test_default_header_and_empty_source_variants_are_preserved(self):
        self.assertEqual(
            execplan_header("G-001", "Default"),
            "---\n"
            "id: G-001\n"
            "title: Default\n"
            "sources:\n"
            "  - docs/PRODUCT.md\n"
            "depends_on: []\n"
            "complexity: low\n"
            "risk: low\n"
            "priority: normal\n"
            "---",
        )
        self.assertEqual(
            execplan_header(
                "G-002", "Variant", sources=(), depends_on=("G-001",),
                complexity="medium", risk="high", priority=None,
                include_priority=False,
            ),
            "---\n"
            "id: G-002\n"
            "title: Variant\n"
            "sources: []\n"
            "depends_on:\n"
            "  - G-001\n"
            "complexity: medium\n"
            "risk: high\n"
            "---",
        )
        self.assertEqual(
            execplan_header(
                "G-003", "Empty block", sources=(), empty_sources_as_list=False,
            ),
            "---\n"
            "id: G-003\n"
            "title: Empty block\n"
            "sources:\n"
            "\n"
            "depends_on: []\n"
            "complexity: low\n"
            "risk: low\n"
            "priority: normal\n"
            "---",
        )

    def test_catalog_fixtures_share_header_and_keep_their_body_forms(self):
        expected = execplan_header("G-001", "Shared")
        self.assertTrue(
            catalog_fixtures.plan_text("G-001", "Shared").startswith(
                expected + "\n\n## Purpose\n"
            )
        )
        self.assertIn(
            "priority: None\n",
            catalog_fixtures.plan_text("G-001", "Shared", priority=None),
        )
        self.assertTrue(
            catalog_fixtures.archived_entry("G-001", "Shared").text.startswith(
                expected + "\n\n## Purpose\n"
            )
        )

        empty_source_header = execplan_header(
            "G-002", "Empty", sources=(), empty_sources_as_list=False,
        )
        self.assertTrue(
            catalog_fixtures.CatalogReconciliationFixture._text(
                "G-002", "Empty", sources=(),
            ).startswith(empty_source_header + "\n\n## Purpose\n")
        )
        self.assertIn(
            "priority: None\n",
            catalog_fixtures.CatalogReconciliationFixture._text(
                "G-002", "Empty", priority=None,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = catalog_fixtures.make_catalog_root(directory)
            path = catalog_fixtures.write_execplan(root, "G-001", "Shared")
            self.assertTrue(
                path.read_text(encoding="utf-8").startswith(
                    expected + "\n\n## Purpose\n"
                )
            )
            no_priority = catalog_fixtures.write_execplan(
                root, "G-002", "No Priority", priority=None,
            ).read_text(encoding="utf-8")
            self.assertNotIn("priority:", no_priority.split("---", 2)[1])


class ExecPlanCatalogSnapshotTest(unittest.TestCase):
    def test_archive_conflict_scope_uses_only_merged_sqlite_rows(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE exec_plans (repo TEXT, plan_id TEXT, status TEXT)"
            )
            connection.executemany(
                "INSERT INTO exec_plans VALUES (?, ?, ?)",
                (
                    ("/repo", "G-001", "MERGED"),
                    ("/repo", "G-002", "READY"),
                    ("/other", "G-003", "MERGED"),
                ),
            )
            self.assertEqual(
                loop_manager._merged_execplan_ids(connection, "/repo"),
                frozenset({"G-001"}),
            )
        finally:
            connection.close()

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

    def test_remote_snapshot_isolates_active_archive_id_conflict(self):
        active = "docs/plans/[G-001] Revised.md"
        archived = "docs/plans/archive/[G-001] Old.md"
        other = "docs/plans/[G-002] Independent.md"
        documents = {
            active: plan_text("G-001", "Revised"),
            archived: plan_text("G-001", "Old"),
            other: plan_text("G-002", "Independent"),
        }

        snapshot = execplan_catalog.snapshot_from_documents(
            documents,
            revision_sha="a" * 40,
            archive_conflict_plan_ids=frozenset({"G-001"}),
        )

        self.assertEqual(tuple(snapshot.active), ("G-001", "G-002"))
        self.assertEqual(tuple(snapshot.archived), ())
        self.assertEqual(
            execplan_catalog.conflicting_archived_plan_paths(documents),
            {"G-001": (archived,)},
        )
        with self.assertRaisesRegex(execplan_catalog.ExecPlanCatalogError, "중복"):
            execplan_catalog.snapshot_from_documents(documents)

    def test_archive_conflict_requires_merged_id_and_valid_archived_file(self):
        active = "docs/plans/[G-001] Revised.md"
        archived = "docs/plans/archive/[G-001] Old.md"
        documents = {
            active: plan_text("G-001", "Revised"),
            archived: plan_text("G-001", "Old"),
        }
        with self.assertRaisesRegex(execplan_catalog.ExecPlanCatalogError, "중복"):
            execplan_catalog.snapshot_from_documents(
                documents,
                archive_conflict_plan_ids=frozenset(),
            )

        invalid_cases = (
            ({**documents, archived: b"invalid archived bytes: \xff"}, None, "UTF-8"),
            ({**documents, archived: plan_text("G-002", "Old")}, None, "유효하지"),
            ({active: documents[active]}, {archived: "symlink"}, "일반 파일"),
        )
        for invalid_documents, invalid_entries, expected in invalid_cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(
                    execplan_catalog.ExecPlanCatalogError, expected
                ):
                    execplan_catalog.snapshot_from_documents(
                        invalid_documents,
                        invalid_plan_entries=invalid_entries,
                        archive_conflict_plan_ids=frozenset({"G-001"}),
                    )

    def test_remote_git_snapshot_keeps_other_plans_when_archive_id_conflicts(self):
        documents = {
            "docs/PRODUCT.md": b"# Product\n",
            "docs/plans/[G-001] Revised.md": plan_text("G-001", "Revised").encode(),
            "docs/plans/archive/[G-001] Old.md": plan_text(
                "G-001", "Old"
            ).encode(),
            "docs/plans/[G-002] Independent.md": plan_text(
                "G-002", "Independent"
            ).encode(),
        }
        object_ids = {f"{index:040x}": path for index, path in enumerate(documents, 1)}
        tree = b"".join(
            f"100644 blob {object_id}\t{path}".encode() + b"\0"
            for object_id, path in object_ids.items()
        )

        def git_bytes(_repo, *args):
            if args[0] == "ls-tree":
                return tree
            return documents[object_ids[args[2]]]

        with mock.patch.object(
            loop_manager, "git", return_value=SimpleNamespace(stdout="a" * 40)
        ), mock.patch.object(loop_manager, "git_bytes", side_effect=git_bytes):
            snapshot = loop_manager.revision_execplan_snapshot(
                Path("/unused"), "base",
                archive_conflict_plan_ids=frozenset({"G-001"}),
            )
            with self.assertRaisesRegex(
                execplan_catalog.ExecPlanCatalogError, "중복"
            ):
                loop_manager.revision_execplan_snapshot(Path("/unused"), "base")

        self.assertEqual(tuple(snapshot.active), ("G-001", "G-002"))
        self.assertEqual(tuple(snapshot.archived), ())

    def test_requirement_archive_candidate_ignores_only_merged_plan_conflict(self):
        source = "docs/requirements/Independent.md"
        documents = {
            "docs/plans/[G-001] Merged.md": plan_text("G-001", "Merged"),
            "docs/plans/archive/[G-001] Merged.md": plan_text("G-001", "Merged"),
            "docs/plans/[G-002] Current.md": plan_text(
                "G-002", "Current"
            ).replace("docs/PRODUCT.md", source),
        }
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE exec_plans (repo TEXT, plan_id TEXT, path TEXT, "
            "sources TEXT, status TEXT, plan_sha TEXT, "
            "publication_path TEXT, publication_sha TEXT)"
        )
        execplan_catalog_state.ensure_schema(connection)
        connection.executemany(
            "INSERT INTO exec_plans VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (
                ("/repo", "G-001", "docs/plans/[G-001] Merged.md",
                 '["docs/PRODUCT.md"]', "MERGED"),
                ("/repo", "G-002", "docs/plans/[G-002] Current.md",
                 f'["{source}"]', "IMPLEMENTING"),
            ),
        )

        def snapshot(_repo, _revision, *, archive_conflict_plan_ids):
            return execplan_catalog.snapshot_from_documents(
                documents,
                archive_conflict_plan_ids=archive_conflict_plan_ids,
            )

        with mock.patch.object(
            loop_manager, "revision_execplan_snapshot", side_effect=snapshot
        ), mock.patch.object(
            loop_manager, "_requirement_archive_tree",
            return_value={source.encode(): ()},
        ):
            self.assertEqual(
                loop_manager.requirement_archive_candidates(
                    connection, Path("/repo"), "/repo", "base", "G-002"
                ),
                (source,),
            )
            connection.execute(
                "UPDATE exec_plans SET status = 'READY' WHERE plan_id = 'G-001'"
            )
            with self.assertRaisesRegex(
                loop_manager.RequirementArchiveError, "중복 ExecPlan id"
            ):
                loop_manager.requirement_archive_candidates(
                    connection, Path("/repo"), "/repo", "base", "G-002"
                )


if __name__ == "__main__":
    unittest.main()
