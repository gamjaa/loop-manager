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

    def test_catalog_fixtures_preserve_invalid_metadata_for_regressions(self):
        cases = (
            (catalog_fixtures.plan_text("G-001", "Invalid", priority=None), "priority"),
            (catalog_fixtures.CatalogReconciliationFixture._text(
                "G-001", "Invalid", sources=(),
            ), "frontmatter"),
            (catalog_fixtures.CatalogReconciliationFixture._text(
                "G-001", "Invalid", priority=None,
            ), "priority"),
        )
        for text, field in cases:
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, field):
                execplan_catalog.parse_execplan_text("docs/plans/[G-001] Invalid.md", text)


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

        def snapshot(
            _repo, _revision, *, archive_conflict_plan_ids,
            satisfied_dependencies,
        ):
            return execplan_catalog.snapshot_from_documents(
                documents,
                archive_conflict_plan_ids=archive_conflict_plan_ids,
                satisfied_dependencies=satisfied_dependencies,
            )

        oid = "1" * 40

        def git_bytes(_repo, operation, *args):
            if operation == "ls-tree":
                return f"100644 blob {oid}\t{source}\0".encode()
            if operation == "cat-file":
                return b"# Independent Requirement\n"
            raise AssertionError((operation, args))

        with mock.patch.object(
            loop_manager, "revision_execplan_snapshot", side_effect=snapshot
        ), mock.patch.object(
            loop_manager, "_requirement_archive_tree",
            return_value={source.encode(): ()},
        ), mock.patch.object(
            loop_manager, "git_bytes", side_effect=git_bytes,
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


class ScopedCatalogParsingTest(unittest.TestCase):
    def snapshot(self, documents):
        return execplan_catalog.snapshot_from_documents(
            documents, validate_sources=True,
            available_regular_paths={'docs/PRODUCT.md'}, revision_sha='a' * 40,
            isolate_errors=True,
        )

    def test_missing_source_withholds_transitive_dependents_only(self):
        documents = {
            'docs/plans/[G-001] Invalid.md': execplan_header(
                'G-001', 'Invalid', sources=('docs/requirements/missing.md',)),
            'docs/plans/[G-002] Direct.md': plan_text('G-002', 'Direct', depends_on=('G-001',)),
            'docs/plans/[G-003] Indirect.md': plan_text('G-003', 'Indirect', depends_on=('G-002',)),
            'docs/plans/[G-004] Independent.md': plan_text('G-004', 'Independent'),
        }
        view = self.snapshot(documents)
        self.assertEqual(set(view.active), {'G-004'})
        self.assertEqual(view.withheld_ids, {'G-001', 'G-002', 'G-003'})
        self.assertEqual(view.observed_ids, {'G-001', 'G-002', 'G-003', 'G-004'})
        self.assertEqual(view.issues[0].code, 'missing-source')
        with self.assertRaises(execplan_catalog.ExecPlanCatalogError):
            execplan_catalog.snapshot_from_documents(
                documents, validate_sources=True, available_regular_paths={'docs/PRODUCT.md'})

    def test_archived_source_absence_and_verified_history_dependency_are_historical(self):
        snapshot = execplan_catalog.snapshot_from_documents(
            {
                'docs/plans/archive/[G-001] Archived.md': execplan_header(
                    'G-001', 'Archived', sources=('docs/requirements/Deleted.md',)),
                'docs/plans/[G-002] Current.md': plan_text(
                    'G-002', 'Current', depends_on=('G-001',)),
            },
            validate_sources=True,
            available_regular_paths={'docs/PRODUCT.md'},
            satisfied_dependencies=frozenset({'G-001'}),
            isolate_errors=True,
        )
        self.assertEqual(set(snapshot.archived), {'G-001'})
        self.assertEqual(set(snapshot.active), {'G-002'})
        self.assertEqual(snapshot.issues, ())

    def test_unique_invalid_metadata_and_dependency_cycles_remain_non_executable(self):
        for broken in (
            execplan_header('G-001', 'Invalid', risk='unsupported'),
            execplan_header('G-001', 'Invalid', depends_on=('G-099',)),
            execplan_header('G-001', 'Invalid', depends_on=('G-002',)),
        ):
            with self.subTest(broken=broken):
                view = self.snapshot({
                    'docs/plans/[G-001] Invalid.md': broken,
                    'docs/plans/[G-002] Direct.md': plan_text('G-002', 'Direct', depends_on=('G-001',)),
                    'docs/plans/[G-003] Independent.md': plan_text('G-003', 'Independent'),
                })
                self.assertEqual(set(view.active), {'G-003'})
                self.assertEqual(view.withheld_ids, {'G-001', 'G-002'})

    def test_ambiguous_identity_encoding_and_shared_file_safety_fail_closed(self):
        for documents in (
            {'docs/plans/[G-001] Invalid.md': '---\nid: G-001\nrisk: bad'},
            {'docs/plans/[G-001] Invalid.md': execplan_header('G-002', 'Invalid')},
            {'docs/plans/[G-001] Invalid.md': b'\xff'},
            {'docs/plans/[G-001] Invalid.md': execplan_header('G-001', 'Invalid', risk='bad'),
             'docs/plans/[G-001] Other.md': execplan_header('G-001', 'Other')},
        ):
            with self.subTest(documents=documents), self.assertRaises(execplan_catalog.ExecPlanCatalogError):
                self.snapshot(documents)
        with self.assertRaises(execplan_catalog.ExecPlanCatalogError):
            execplan_catalog.snapshot_from_documents({}, isolate_errors=True,
                invalid_plan_entries={'docs/plans/[G-001] Invalid.md': 'symlink'})

    def test_git_port_pins_isolated_errors_and_execution_view_to_one_tree(self):
        from loop_manager_internal.catalog import catalog_service
        revision = 'a' * 40
        product = 'docs/PRODUCT.md'
        broken = 'docs/plans/[G-001] Broken.md'
        independent = 'docs/plans/[G-002] Independent.md'
        source = 'docs/requirements/[R-066] Original.md'
        renamed = 'docs/requirements/[R-066] Renamed.md'
        requirement = b'# [R-066] Renamed\n\nChanged requirement.\n'
        contents = {
            'broken-oid': execplan_header('G-001', 'Broken', risk='invalid').encode(),
            'valid-oid': execplan_header('G-002', 'Independent', sources=(source,)).encode(),
            'requirement-oid': requirement,
        }
        tree = (f'100644 blob product-oid\t{product}\0'
                f'100644 blob broken-oid\t{broken}\0'
                f'100644 blob valid-oid\t{independent}\0'
                f'100644 blob requirement-oid\t{renamed}\0').encode()
        def git_bytes(_repo, command, *arguments):
            if command == 'show':
                self.assertEqual(arguments[-1], revision + ':' + product)
                return b'# Product\n'
            return tree if command == 'ls-tree' else contents[arguments[-1]]
        effects = SimpleNamespace(git=lambda *args: SimpleNamespace(stdout=revision), git_bytes=git_bytes)
        port = catalog_service._GitCatalogDefinitionPort(
            SimpleNamespace(git=effects.git, git_bytes=effects.git_bytes,
                revision_execplan_snapshot=lambda repo, sha, **kwargs:
                    catalog_service.revision_execplan_snapshot(effects, repo, sha, **kwargs)),
            Path('/repo'), 'origin/main', lambda: frozenset())
        pinned = port.load()
        self.assertEqual((pinned.revision_sha, pinned.snapshot.revision_sha), (revision, revision))
        self.assertEqual(set(pinned.snapshot.active), {'G-002'})
        self.assertEqual(pinned.snapshot.withheld_ids, {'G-001'})
        self.assertEqual(pinned.regular_paths, {product, broken, independent, renamed})
        self.assertEqual(pinned.requirement_documents, {renamed: requirement})
        self.assertEqual(pinned.snapshot.active['G-002'].sources, (source,))
