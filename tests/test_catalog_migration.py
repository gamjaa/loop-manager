from contextlib import closing
from tests.support.database import AutomationCatalogFixture
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager
from loop_manager_internal.catalog.catalog_service import BackfillPlanShasEffects, backfill_plan_shas
from loop_manager_internal.runtime.contracts import GitLockError
from tests.support.catalog import make_catalog_root, write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push, isolated_git_test


class BackfillObservationTests(unittest.TestCase):
    def test_failed_git_history_does_not_block_or_backfill_project(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE projects (repo TEXT, plan_path TEXT, plan_sha TEXT, phase TEXT)"
        )
        connection.execute(
            "INSERT INTO projects VALUES ('/repo', 'docs/plans/plan.md', NULL, 'WORKING')"
        )
        effects = BackfillPlanShasEffects(
            _plan_id_from_path=lambda path: "G-001",
            git=lambda *args, **kwargs: SimpleNamespace(returncode=128, stdout=""),
        )
        with self.assertRaisesRegex(RuntimeError, "backfill"):
            backfill_plan_shas(effects, connection)
        self.assertIsNone(connection.execute("SELECT plan_sha FROM projects").fetchone()[0])
        self.assertEqual(connection.execute("SELECT phase FROM projects").fetchone()[0], "WORKING")
        def locked_git(*args, **kwargs):
            raise GitLockError('different lock diagnostic')
        locked = BackfillPlanShasEffects(
            _plan_id_from_path=lambda path: "G-001", git=locked_git,
        )
        with self.assertRaises(GitLockError):
            backfill_plan_shas(locked, connection)
        connection.close()


@isolated_git_test
class CatalogMigrationTest(AutomationCatalogFixture, unittest.TestCase):
    def test_priority_parser_supports_legacy_default_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            legacy = write_execplan(root, "G-001", "Legacy", priority=None)
            invalid = write_execplan(root, "G-002", "Invalid", priority="urgent")

            self.assertEqual(
                loop_manager._parse_execplan(root, legacy)["priority"],
                "normal",
            )
            with self.assertRaisesRegex(RuntimeError, "priority"):
                loop_manager._parse_execplan(root, legacy, require_priority=True)
            with self.assertRaisesRegex(RuntimeError, "priority"):
                loop_manager._parse_execplan(root, invalid)

    def test_legacy_database_migration_reconciles_only_confirmed_merge_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            write_execplan(root, "G-001", "Completed", priority=None)
            write_execplan(root, "G-002", "Unconfirmed")
            active = write_execplan(root, "G-003", "Active", priority="high")
            commit_and_push(root, "Add legacy plans")
            plan_sha = git_command(root, "rev-parse", "HEAD")
            write_execplan(root, "G-004", "Local Only")
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA)
            database.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, phase, plan_path, plan_sha, active_sha,
                    branch, worktree, pr_url, worker_session_id
                ) VALUES (?, 'main', 'PR_OPEN', ?, ?, 'base-sha',
                          'loop-manager/G-003', 'managed', 'https://pr/3', 'worker-session')
                """,
                (str(root), active.relative_to(root).as_posix(), plan_sha),
            )
            database.commit()
            database.close()

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
                loop_manager.migrate_legacy_execplans(connection, target_repo=str(root))
                before_reconciliation = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                    (str(root),),
                ).fetchall()
                looked_up = []

                def history(_repo, branch, _url=None):
                    looked_up.append(branch)
                    if branch == "loop-manager/G-001":
                        return {
                            "url": "https://pr/1",
                            "state": "MERGED",
                            "mergedAt": "2026-01-01T00:00:00Z",
                        }
                    return None

                with mock.patch("loop_manager.pr_for_branch", side_effect=history):
                    loop_manager.migrate_legacy_execplans(
                        connection,
                        reconcile_history=True,
                        target_repo=str(root),
                    )
                rows = connection.execute(
                    """
                    SELECT plan_id, status, priority, plan_sha, pr_url, worker_session_id
                    FROM exec_plans WHERE repo = ? ORDER BY plan_id
                    """,
                    (str(root),),
                ).fetchall()

        self.assertEqual(
            [tuple(row) for row in before_reconciliation],
            [("G-003", "PR_OPEN")],
        )
        self.assertEqual(
            looked_up,
            ["loop-manager/G-001", "loop-manager/G-002"],
        )
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("G-001", "MERGED", "normal", plan_sha, "https://pr/1", None),
                ("G-002", "READY", "normal", plan_sha, None, None),
                ("G-003", "PR_OPEN", "high", plan_sha, "https://pr/3", "worker-session"),
            ],
        )

    def test_legacy_migration_preserves_existing_active_publication_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            write_execplan(root, "G-001", "Stable Active")
            commit_and_push(root, "Add stable active plan")
            connection = self._connection(str(root))
            self._insert_plan(
                connection,
                "G-001",
                "READY",
                repo=str(root),
                title="Stable Active",
                plan_sha="stored-publication-sha",
            )

            with mock.patch("loop_manager.pr_for_branch") as lookup:
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            row = connection.execute(
                "SELECT path, content_hash, plan_sha, status "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        lookup.assert_not_called()
        self.assertEqual(row["path"], "docs/plans/[G-001] Stable Active.md")
        self.assertEqual(row["content_hash"], "hash-G-001")
        self.assertEqual(row["plan_sha"], "stored-publication-sha")
        self.assertEqual(row["status"], "READY")

    def test_restored_active_plan_is_not_hidden_by_stale_archive_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Restored Active")
            commit_and_push(root, "Add active plan")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            commit_and_push(root, "Archive active plan")
            archived_content = archived.read_text(encoding="utf-8")
            archived.rename(active)
            commit_and_push(root, "Restore active plan")
            connection = self._connection(str(root))
            archived_entry = loop_manager.execplan_catalog.snapshot_from_documents(
                {
                    "docs/plans/archive/[G-001] Restored Active.md":
                        archived_content
                }
            ).archived["G-001"]
            repository = (
                loop_manager.execplan_catalog_state.CatalogStateRepository(
                    connection
                )
            )
            repository.record(
                str(root),
                archived_entry,
                original_path="docs/plans/[G-001] Restored Active.md",
                original_content_hash=archived_entry.content_hash,
                plan_sha=None,
                proof=loop_manager.execplan_catalog_state.MergeProof(False),
                diagnostic=loop_manager.execplan_catalog_state.CatalogDiagnostic(
                    "G-001",
                    archived_entry.path,
                    "merge-proof-missing",
                    "missing",
                ),
                identity_verified=False,
            )
            connection.execute(
                "INSERT INTO execplan_catalog_revisions "
                "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, ?)",
                (str(root), publication_sha, "initialized-archive"),
            )
            connection.commit()

            with mock.patch("loop_manager.pr_for_branch", return_value=None) as lookup:
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            history = repository.historical_row(str(root), "G-001")
            compact = json.loads(
                loop_manager.compact_execplan_catalog(connection, str(root))
            )
            decision = loop_manager.catalog_schedule_decision(
                connection, str(root)
            )
            runtime = connection.execute(
                "SELECT status, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            diagnostics = repository.diagnostics(str(root))
            diagnostic_count = connection.execute(
                "SELECT COUNT(*) FROM execplan_catalog_diagnostics "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()[0]
            with mock.patch(
                "loop_manager._run_project_step",
                return_value=loop_manager.PROJECT_STEP_PLANNING_PENDING,
            ):
                project_result = loop_manager.run_project_result(
                    connection,
                    Path(directory) / "state",
                    str(root),
                    Path(directory) / "config.json",
                    None,
                    False,
                )
            connection.close()

        lookup.assert_called_once_with(root, "loop-manager/G-001")
        self.assertEqual(history["currently_archived"], 0)
        self.assertEqual(
            history["observed_path"],
            "docs/plans/[G-001] Restored Active.md",
        )
        self.assertEqual([item["id"] for item in compact], ["G-001"])
        self.assertEqual(runtime["status"], "READY")
        self.assertEqual(runtime["plan_sha"], publication_sha)
        self.assertEqual(decision.runnable_ready["plan_id"], "G-001")
        self.assertEqual(project_result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(diagnostics, ())
        self.assertEqual(diagnostic_count, 0)

    def test_legacy_migration_recovers_archived_identity_without_merge_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            first = write_execplan(root, "G-001", "Confirmed")
            second = write_execplan(root, "G-002", "Unconfirmed")
            commit_and_push(root, "Add historical plans")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            first.rename(archive / first.name)
            second.rename(archive / second.name)
            commit_and_push(root, "Archive historical plans")
            archive_sha = git_command(root, "rev-parse", "HEAD")

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            connection.commit()

            with mock.patch("loop_manager.pr_for_branch", side_effect=AssertionError("unexpected PR lookup")) as lookup:
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            rows = connection.execute(
                "SELECT plan_id, path, status, plan_sha FROM exec_plans "
                "WHERE repo = ? ORDER BY plan_id",
                (str(root),),
            ).fetchall()
            history = connection.execute(
                "SELECT plan_id, original_path, observed_path, merge_confirmed, "
                "diagnostic_code FROM execplan_catalog_history "
                "WHERE repo = ? ORDER BY plan_id",
                (str(root),),
            ).fetchall()
            connection.close()

        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (
                    "G-001",
                    "docs/plans/archive/[G-001] Confirmed.md",
                    "MERGED",
                    archive_sha,
                ),
                ("G-002", "docs/plans/archive/[G-002] Unconfirmed.md", "MERGED", archive_sha),
            ],
        )
        self.assertEqual(history[0]["merge_confirmed"], 1)
        self.assertEqual(history[0]["observed_path"], "docs/plans/archive/[G-001] Confirmed.md")
        self.assertEqual(history[1]["merge_confirmed"], 1)
        self.assertIsNone(history[1]["diagnostic_code"])
        lookup.assert_not_called()

    def test_fresh_fallback_accepts_archive_without_old_publication_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            git_command(root, "config", "core.autocrlf", "false")
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Tampered Archive")
            commit_and_push(root, "Publish plan")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            archived.write_text(
                archived.read_text(encoding="utf-8")
                + "\nTampered before first recovery.\n",
                encoding="utf-8",
            )
            tampered_hash = hashlib.sha256(archived.read_bytes()).hexdigest()
            commit_and_push(root, "Archive tampered plan")
            archive_sha = git_command(root, "rev-parse", "HEAD")

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            connection.commit()
            with mock.patch(
                "loop_manager.pr_for_branch", side_effect=AssertionError("unexpected PR lookup")
            ) as lookup:
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            runtime = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            history = connection.execute(
                "SELECT original_content_hash, final_content_hash, plan_sha, "
                "identity_verified, merge_confirmed, diagnostic_code "
                "FROM execplan_catalog_history "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        lookup.assert_not_called()
        self.assertEqual(runtime["status"], "MERGED")
        self.assertEqual(history["original_content_hash"], tampered_hash)
        self.assertEqual(history["final_content_hash"], tampered_hash)
        self.assertEqual(history["plan_sha"], archive_sha)
        self.assertEqual(history["identity_verified"], 1)
        self.assertEqual(history["merge_confirmed"], 1)
        self.assertIsNone(history["diagnostic_code"])

    def test_archived_merged_row_recovers_from_stored_state_and_git_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            git_command(root, "config", "core.autocrlf", "false")
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Stored Merge")
            commit_and_push(root, "Add stored merge plan")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            publication_hash = hashlib.sha256(active.read_bytes()).hexdigest()
            active.write_text(
                active.read_text(encoding="utf-8")
                + "\nReconciled before archive.\n",
                encoding="utf-8",
            )
            commit_and_push(root, "Revise stored merge plan")
            current_hash = hashlib.sha256(active.read_bytes()).hexdigest()
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            commit_and_push(root, "Archive stored merge plan")
            self.assertEqual(
                hashlib.sha256(archived.read_bytes()).hexdigest(),
                current_hash,
            )

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, publication_path,
                    publication_content_hash, publication_sha,
                    status, plan_sha
                ) VALUES (?, 'G-001', ?, 'Stored Merge',
                          '["docs/PRODUCT.md"]', '[]', 'normal', ?, ?, ?, ?,
                          'MERGED', ?)
                """,
                (
                    str(root),
                    "docs/plans/[G-001] Stored Merge.md",
                    current_hash,
                    "docs/plans/[G-001] Stored Merge.md",
                    publication_hash,
                    publication_sha,
                    publication_sha,
                ),
            )
            connection.commit()

            with mock.patch("loop_manager.pr_for_branch", return_value=None):
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            history = connection.execute(
                "SELECT merge_confirmed, proof_kind, plan_sha "
                "FROM execplan_catalog_history WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(history["merge_confirmed"], 1)
        self.assertEqual(history["proof_kind"], "stored-and-git")
        self.assertEqual(history["plan_sha"], publication_sha)

    def test_unverified_archived_active_runtime_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Interrupted")
            commit_and_push(root, "Add interrupted plan")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            commit_and_push(root, "Archive interrupted plan")
            content_hash = hashlib.sha256(archived.read_bytes()).hexdigest()
            active_path = "docs/plans/[G-001] Interrupted.md"

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, phase, plan_path, plan_sha,
                    branch, worktree
                ) VALUES (?, 'main', 'WORKING', ?, ?,
                          'loop-manager/G-001', '/tmp/interrupted')
                """,
                (str(root), active_path, publication_sha),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, plan_sha, branch, worktree
                ) VALUES (?, 'G-001', ?, 'Interrupted',
                          '["docs/PRODUCT.md"]', '[]', 'normal', ?,
                          'IMPLEMENTING', ?, 'loop-manager/G-001', '/tmp/interrupted')
                """,
                (str(root), active_path, content_hash, publication_sha),
            )
            connection.commit()

            with mock.patch("loop_manager.pr_for_branch", return_value=None):
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            plan = connection.execute(
                "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            project = connection.execute(
                "SELECT phase, plan_path FROM projects WHERE repo = ?",
                (str(root),),
            ).fetchone()
            history = connection.execute(
                "SELECT merge_confirmed, diagnostic_code "
                "FROM execplan_catalog_history WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIsNone(project["phase"])
        self.assertIsNone(project["plan_path"])
        self.assertEqual(history["merge_confirmed"], 0)
        self.assertEqual(history["diagnostic_code"], "merge-proof-missing")

    def test_batch_validation_allocates_after_archived_maximum_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans/archive").mkdir(parents=True)
            archived = repo / "docs/plans/archive/[G-009] Historical.md"
            archived.write_text(
                write_execplan(repo, "G-009", "Historical").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            (repo / "docs/plans/[G-009] Historical.md").unlink()
            commit_and_push(repo, "Add archived catalog maximum")
            planned = write_execplan(repo, "G-010", "Next Active")

            result = loop_manager.validate_planner_result(
                repo,
                git_command(repo, "rev-parse", "HEAD"),
                [planned.relative_to(repo).as_posix()],
            )

        self.assertEqual(
            [str(plan["id"]) for _path, plan in result.plans],
            ["G-010"],
        )
