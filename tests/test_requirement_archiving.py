import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import loop_manager
from loop_manager_internal.catalog import requirement_archive
from loop_manager_internal.storage import execplan_catalog_state
from tests.support.git import isolated_git_test
from tests.support.catalog import archived_entry


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


@isolated_git_test
class RequirementArchiveEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.email", "loop-manager@example.com")
        git(self.repo, "config", "user.name", "Loop Manager Test")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "docs/plans").mkdir(parents=True)
        (self.repo / "docs/requirements/archive").mkdir(parents=True)
        (self.repo / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")

        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(self.repo),),
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def test_archive_snapshot_accepts_dependency_on_rowless_verified_merge(self):
        source = self.requirement("Current")
        self.plan("G-002", "Current", (source,), "IMPLEMENTING")
        path = self.repo / "docs/plans/[G-002] Current.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "depends_on: []", "depends_on:\n  - G-001",
            ),
            encoding="utf-8",
        )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on='[\"G-001\"]' "
            "WHERE repo=? AND plan_id='G-002'", (str(self.repo),),
        )
        entry = archived_entry("G-001", "Historical")
        execplan_catalog_state.CatalogStateRepository(self.connection).record(
            str(self.repo), entry, original_path=entry.active_path,
            original_content_hash=entry.content_hash, plan_sha="a" * 40,
            proof=execplan_catalog_state.MergeProof(
                True, kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
                plan_sha="a" * 40, publication_content_hash=entry.content_hash,
                revision="b" * 40,
            ), diagnostic=None, identity_verified=True,
        )
        self.connection.commit()
        revision = self.commit("Active plan depends on archived history")

        snapshot = loop_manager.requirement_archive_snapshot(
            self.connection, self.repo, str(self.repo), revision, "G-002",
        )

        self.assertEqual(snapshot.candidates, (source,))

    def requirement(self, name: str, contents: str | None = None) -> str:
        source = f"docs/requirements/{name}.md"
        (self.repo / source).write_text(
            contents or f"# {name}\n",
            encoding="utf-8",
        )
        return source

    def plan(
        self,
        plan_id: str,
        title: str,
        sources: tuple[str, ...],
        status: str,
    ) -> None:
        relative_path = f"docs/plans/[{plan_id}] {title}.md"
        source_lines = "\n".join(f"  - {source}" for source in sources)
        text = (
            "---\n"
            f"id: {plan_id}\n"
            f"title: {title}\n"
            "sources:\n"
            f"{source_lines}\n"
            "depends_on: []\n"
            "complexity: low\n"
            "risk: low\n"
            "priority: normal\n"
            "---\n\n"
            "## Purpose\n\nTest archive behavior.\n"
        )
        (self.repo / relative_path).write_text(text, encoding="utf-8")
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha
            ) VALUES (?, ?, ?, ?, ?, '[]', 'normal', ?, ?, 'plan-sha')
            """,
            (
                str(self.repo),
                plan_id,
                relative_path,
                title,
                json.dumps(sources),
                f"hash-{plan_id}",
                status,
            ),
        )
        self.connection.commit()

    def commit(self, message: str = "Fixture") -> str:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def worker_tree(self) -> Path:
        worker = Path(self.temporary.name) / "worker"
        git(self.repo, "worktree", "add", "--detach", str(worker), "HEAD")
        (worker / "docs/requirements/archive").mkdir(parents=True, exist_ok=True)
        return worker

    def candidates(self, revision: str, current: str) -> tuple[str, ...]:
        return loop_manager.requirement_archive_candidates(
            self.connection,
            self.repo,
            str(self.repo),
            revision,
            current,
        )

    def test_single_multiple_and_historical_requirements_are_all_candidates(self):
        current_only = self.requirement("Current Only")
        shared = self.requirement("Shared")
        historical = self.requirement("Historical")
        self.plan("G-001", "Historical Work", (historical,), "MERGED")
        self.plan("G-002", "Shared Foundation", (shared,), "MERGED")
        self.plan(
            "G-003",
            "Current Work",
            (current_only, shared),
            "IMPLEMENTING",
        )
        revision = self.commit()

        self.assertEqual(
            self.candidates(revision, "G-003"),
            (current_only, historical, shared),
        )

    def test_unverified_historical_plan_does_not_complete_or_block_unrelated_requirement(self):
        historical_source = self.requirement("Historical Unknown")
        current_source = self.requirement("Current Safe")
        self.plan(
            "G-001", "Historical Unknown", (historical_source,), "READY"
        )
        self.plan(
            "G-002", "Current Safe", (current_source,), "IMPLEMENTING"
        )
        active_path = self.repo / "docs/plans/[G-001] Historical Unknown.md"
        archived_path = (
            self.repo / "docs/plans/archive/[G-001] Historical Unknown.md"
        )
        archived_path.parent.mkdir()
        active_path.rename(archived_path)
        self.connection.execute(
            "DELETE FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.execute(
            """
            INSERT INTO execplan_catalog_history (
                repo, plan_id, original_path, observed_path,
                original_content_hash, final_content_hash,
                merge_confirmed, diagnostic_code, diagnostic_message
            ) VALUES (?, 'G-001', ?, ?, 'hash', 'hash', 0,
                      'merge-proof-missing', 'missing')
            """,
            (
                str(self.repo),
                "docs/plans/[G-001] Historical Unknown.md",
                "docs/plans/archive/[G-001] Historical Unknown.md",
            ),
        )
        self.connection.commit()
        revision = self.commit()

        self.assertEqual(
            self.candidates(revision, "G-002"),
            (current_source,),
        )

    def test_every_nonterminal_related_plan_prevents_archiving(self):
        source = self.requirement("Blocked")
        self.plan("G-001", "Merged Foundation", (source,), "MERGED")
        self.plan("G-002", "Remaining Work", (source,), "READY")
        self.plan("G-003", "Current Work", (source,), "IMPLEMENTING")
        revision = self.commit()

        blocking_statuses = (
            "READY",
            "IMPLEMENTING",
            "VERIFYING",
            "REVIEWING",
            "PUBLISHING",
            "PR_OPEN",
            "PLAN_BLOCKED",
        )
        for status in blocking_statuses:
            with self.subTest(status=status):
                if status != "READY":
                    self.connection.execute(
                        "UPDATE exec_plans SET status = 'MERGED' "
                        "WHERE repo = ? AND plan_id = 'G-003'",
                        (str(self.repo),),
                    )
                    self.connection.commit()
                self.connection.execute(
                    "UPDATE exec_plans SET status = ? "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (status, str(self.repo)),
                )
                self.connection.commit()
                self.assertEqual(self.candidates(revision, "G-003"), ())

        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = ? AND plan_id = 'G-002'",
            (str(self.repo),),
        )
        self.connection.commit()
        self.assertEqual(self.candidates(revision, "G-003"), (source,))

    def test_existing_plan_source_survives_renamed_requirement_in_git_snapshot(self):
        source = self.requirement("[R-001] Original", "# [R-001] Original\n")
        self.plan("G-001", "Referenced", (source,), "READY")
        self.plan("G-002", "Current", ("docs/PRODUCT.md",), "IMPLEMENTING")
        self.commit("Original Requirement path")

        renamed = "docs/requirements/[R-001] Renamed.md"
        (self.repo / source).rename(self.repo / renamed)
        (self.repo / renamed).write_text(
            "# [R-001] Renamed\n", encoding="utf-8",
        )
        revision = self.commit("Rename Requirement")
        self.assertEqual(self.candidates(revision, "G-002"), ())

    def test_bootstrap_uses_general_history_for_r003_and_r005(self):
        r001 = self.requirement("[R-001] Automation")
        r002 = self.requirement("[R-002] Cleanup")
        r003 = self.requirement("[R-003] Targeted Worker Unit Tests")
        self.requirement("[R-004] PR Context")
        r005 = self.requirement("[R-005] Automatic Requirement Archiving")
        self.plan("G-001", "Automation", (r001,), "READY")
        self.plan("G-006", "Targeted Tests", (r003,), "MERGED")
        self.plan("G-007", "Cleanup", (r002,), "READY")
        self.plan("G-011", "Archive Runtime", (r005,), "MERGED")
        self.plan("G-023", "Bootstrap Archive", (r005,), "IMPLEMENTING")
        revision = self.commit()

        self.assertEqual(
            self.candidates(revision, "G-023"),
            (r003, r005),
        )

    def test_new_related_plan_on_latest_base_changes_reconciliation(self):
        source = self.requirement("Rebase")
        self.plan("G-001", "Foundation", (source,), "MERGED")
        self.plan("G-002", "Current", (source,), "IMPLEMENTING")
        original_revision = self.commit("Original base")
        self.assertEqual(self.candidates(original_revision, "G-002"), (source,))

        worker = self.worker_tree()
        archived = worker / loop_manager.requirement_archive_path(source)
        archived.parent.mkdir(parents=True, exist_ok=True)
        (worker / source).rename(archived)

        self.plan("G-003", "New Related Work", (source,), "READY")
        latest_revision = self.commit("Add related plan")
        expected = self.candidates(latest_revision, "G-002")
        actual = loop_manager.requirement_archive_move_set(
            self.repo,
            latest_revision,
            worker,
        )

        self.assertEqual(expected, ())
        self.assertEqual(actual, (source,))
        self.assertIn(
            "eligibility와 다릅니다",
            loop_manager.requirement_archive_reconciliation_error(
                expected,
                actual,
            ),
        )

    def test_reconciled_source_removal_does_not_block_independent_current_plan(self):
        source = self.requirement("G031 Source")
        self.plan("G-031", "Changed Source", (source,), "READY")
        self.plan(
            "G-035", "Independent Current", ("docs/PRODUCT.md",),
            "IMPLEMENTING",
        )
        self.commit("Initial catalog")
        plan_path = self.repo / "docs/plans/[G-031] Changed Source.md"
        plan_path.write_text(
            plan_path.read_text(encoding="utf-8").replace(
                f"  - {source}", "  - docs/PRODUCT.md"
            ),
            encoding="utf-8",
        )
        revision = self.commit("Remove stale source relation")
        snapshot = loop_manager.revision_execplan_snapshot(self.repo, revision)
        pinned = execplan_catalog_state.PinnedCatalog(revision, snapshot)

        class Port:
            def load(self):
                return pinned

            def current_revision(self):
                return revision

        result = execplan_catalog_state.CatalogReconciliationService(
            execplan_catalog_state.DefinitionStateRepository(self.connection),
            Port(),
        ).reconcile(str(self.repo))

        self.assertEqual(
            result.kind, execplan_catalog_state.ReconciliationKind.RECONCILED
        )
        changed = self.connection.execute(
            "SELECT sources, status FROM exec_plans WHERE repo = ? "
            "AND plan_id = 'G-031'",
            (str(self.repo),),
        ).fetchone()
        independent = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-035'",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(json.loads(changed["sources"]), ["docs/PRODUCT.md"])
        self.assertEqual(changed["status"], "READY")
        self.assertEqual(independent["status"], "IMPLEMENTING")
        self.assertEqual(self.candidates(revision, "G-035"), ())

    def test_later_registered_plan_uses_immutable_plan_commit(self):
        source = self.requirement("Later Plan")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        worker_revision = self.commit("Worker base")

        self.plan("G-002", "New Related Work", (source,), "READY")
        plan_revision = self.commit("Register later plan")
        self.connection.execute(
            "UPDATE exec_plans SET plan_sha = ? "
            "WHERE repo = ? AND plan_id = 'G-002'",
            (plan_revision, str(self.repo)),
        )
        self.connection.commit()

        self.assertEqual(self.candidates(worker_revision, "G-001"), ())

        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = ? AND plan_id = 'G-002'",
            (str(self.repo),),
        )
        self.connection.commit()
        self.assertEqual(
            self.candidates(worker_revision, "G-001"),
            (source,),
        )

    def test_pinned_snapshot_rejects_plan_only_published_later(self):
        source = self.requirement("Pinned")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        old_revision = self.commit("Worker base")
        self.plan("G-002", "Later", (source,), "READY")
        latest = self.commit("Latest base")
        self.connection.execute(
            "UPDATE exec_plans SET plan_sha = ? "
            "WHERE repo = ? AND plan_id = 'G-002'",
            (latest, str(self.repo)),
        )
        self.connection.commit()

        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError, "Git ExecPlan 없음: G-002"
        ):
            loop_manager.requirement_archive_snapshot(
                self.connection, self.repo, str(self.repo),
                old_revision, "G-001",
            )
        self.assertEqual(
            loop_manager.requirement_archive_snapshot(
                self.connection, self.repo, str(self.repo), latest, "G-001"
            ).candidates,
            (),
        )

    def test_snapshot_token_tracks_validated_plan_content_hash(self):
        source = self.requirement("Hash Input")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        original = self.commit("Initial")
        first = loop_manager.requirement_archive_snapshot(
            self.connection, self.repo, str(self.repo), original, "G-001"
        )
        plan_path = self.repo / "docs/plans/[G-001] Current.md"
        plan_path.write_text(
            plan_path.read_text(encoding="utf-8") + "\nAdditional validation.\n",
            encoding="utf-8",
        )
        latest = self.commit("Changed plan body")
        second = loop_manager.requirement_archive_snapshot(
            self.connection, self.repo, str(self.repo), latest, "G-001"
        )
        self.assertEqual(first.candidates, second.candidates)
        self.assertNotEqual(first.catalog_token, second.catalog_token)

    def test_product_only_plans_do_not_require_archive_revision(self):
        self.plan(
            "G-001",
            "Product Work",
            ("docs/PRODUCT.md",),
            "IMPLEMENTING",
        )

        self.assertEqual(self.candidates("not-a-tree", "G-001"), ())

    def test_invalid_archive_revision_is_reported_as_domain_error(self):
        source = self.requirement("Invalid Revision")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")

        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "Git revision",
        ):
            self.candidates("not-a-tree", "G-001")

    def test_collision_and_uncertain_catalog_are_rejected(self):
        source = self.requirement("Collision")
        archived = self.repo / loop_manager.requirement_archive_path(source)
        archived.write_text("# Existing archive\n", encoding="utf-8")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        revision = self.commit()

        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "이미 존재",
        ):
            self.candidates(revision, "G-001")

        self.connection.execute(
            "DELETE FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "SQLite 상태 없음",
        ):
            self.candidates(revision, "G-001")

    def test_move_set_requires_content_preserving_rename_and_leaves_base_active(self):
        source = self.requirement("Move", "# Move\nOriginal content\n")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        revision = self.commit()
        worker = self.worker_tree()
        archived = worker / loop_manager.requirement_archive_path(source)
        (worker / source).rename(archived)

        self.assertEqual(
            loop_manager.requirement_archive_move_set(
                self.repo,
                revision,
                worker,
            ),
            (source,),
        )
        self.assertTrue((self.repo / source).is_file())

        archived.write_text("# Modified\n", encoding="utf-8")
        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "내용이 변경",
        ):
            loop_manager.requirement_archive_move_set(
                self.repo,
                revision,
                worker,
            )

    def test_move_set_rejects_ignored_untracked_archive(self):
        source = self.requirement("Ignored", "# Ignored\n")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        archived = loop_manager.requirement_archive_path(source)
        (self.repo / ".gitignore").write_text(
            f"/{archived}\n",
            encoding="utf-8",
        )
        revision = self.commit()
        worker = self.worker_tree()
        (worker / source).rename(worker / archived)

        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "Git ignore",
        ):
            loop_manager.requirement_archive_move_set(
                self.repo,
                revision,
                worker,
            )

    def test_move_set_rejects_symlink_archive(self):
        contents = "# Symlink\nOriginal content\n"
        source = self.requirement("Symlink", contents)
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        (self.repo / "docs/archive-target.md").write_text(
            contents,
            encoding="utf-8",
        )
        revision = self.commit()
        worker = self.worker_tree()
        archived = worker / loop_manager.requirement_archive_path(source)
        (worker / source).unlink()
        try:
            archived.symlink_to("../../archive-target.md")
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"symlink creation is unavailable: {error}")
        self.assertTrue(archived.is_file())
        self.assertEqual(archived.read_text(encoding="utf-8"), contents)

        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "일반 파일이 아닙니다",
        ):
            loop_manager.requirement_archive_move_set(
                self.repo,
                revision,
                worker,
            )

    def test_published_move_set_requires_tracked_regular_blob(self):
        source = self.requirement("Tracked", "# Tracked\n")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        revision = self.commit()
        worker = self.worker_tree()
        archived = worker / loop_manager.requirement_archive_path(source)
        (worker / source).rename(archived)

        self.assertEqual(
            loop_manager.requirement_archive_move_set(
                self.repo,
                revision,
                worker,
            ),
            (source,),
        )
        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "일반 blob으로 추적되지 않았습니다",
        ):
            loop_manager.requirement_archive_move_set(
                self.repo,
                revision,
                worker,
                tracked_revision="HEAD",
            )

        git(worker, "add", "-A")
        git(worker, "commit", "-m", "Archive requirement")
        self.assertEqual(
            loop_manager.requirement_archive_move_set(
                self.repo,
                revision,
                worker,
                tracked_revision="HEAD",
            ),
            (source,),
        )

    def test_archived_source_fallback_does_not_allow_new_plan_sources(self):
        root = Path(self.temporary.name) / "catalog"
        (root / "docs/plans").mkdir(parents=True)
        archived = root / "docs/requirements/archive/[R-001] Done.md"
        archived.parent.mkdir(parents=True)
        archived.write_text("# Done\n", encoding="utf-8")
        plan_path = root / "docs/plans/[G-001] Existing.md"
        plan_path.write_text(
            """---
id: G-001
title: Existing
sources:
  - docs/requirements/[R-001] Done.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

Test fallback.
""",
            encoding="utf-8",
        )

        catalog = loop_manager.load_execplan_catalog(root)
        self.assertEqual(catalog["G-001"]["sources"], [
            "docs/requirements/[R-001] Done.md"
        ])
        with self.assertRaisesRegex(RuntimeError, "존재하지 않는 ExecPlan source"):
            loop_manager._parse_execplan(
                root,
                plan_path,
                require_priority=True,
                allow_archive_fallback=False,
            )

    def test_worker_prompt_defers_requirement_retirement(self):
        source = "docs/requirements/[R-001] Done.md"
        archived = loop_manager.requirement_archive_path(source)

        self.assertFalse(
            loop_manager._worker_path_allowed(
                source,
                "docs/plans/[G-001] Plan.md",
                (source,),
            )
        )
        self.assertFalse(
            loop_manager._worker_path_allowed(
                archived,
                "docs/plans/[G-001] Plan.md",
                (source,),
            )
        )
        self.assertFalse(
            loop_manager._worker_path_allowed(
                "docs/requirements/archive/[R-002] Other.md",
                "docs/plans/[G-001] Plan.md",
                (source,),
            )
        )
        prompt = loop_manager.worker_prompt(
            "docs/plans/[G-001] Plan.md",
            None,
            archive_candidates=(source,),
        )
        self.assertNotIn("이동하라", prompt)
        self.assertIn(source, prompt)
        self.assertNotIn('Requirement의 완료 판단·archive 이동', prompt)

    def test_unknown_status_is_rejected(self):
        source = self.requirement("Unknown Status")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        revision = self.commit()
        self.connection.execute(
            "UPDATE exec_plans SET status = 'UNKNOWN' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        with self.assertRaisesRegex(
            loop_manager.RequirementArchiveError,
            "source 관계 또는 상태",
        ):
            self.candidates(revision, "G-001")

    def test_blocked_fingerprint_ignores_archived_fallback_contents(self):
        """Legacy selector; archive fallback content now participates in the fingerprint."""
        source = self.requirement("Fingerprint", "# Original\n")
        self.plan("G-001", "Current", (source,), "IMPLEMENTING")
        self.commit()
        worker = self.worker_tree()
        archived = worker / loop_manager.requirement_archive_path(source)
        (worker / source).rename(archived)
        row = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()

        first = loop_manager._worktree_execplan_input_fingerprint(worker, row)
        archived.write_text("# Changed archive\n", encoding="utf-8")
        second = loop_manager._worktree_execplan_input_fingerprint(worker, row)
        self.assertNotEqual(first, second)

        (worker / source).write_text("# Reactivated\n", encoding="utf-8")
        third = loop_manager._worktree_execplan_input_fingerprint(worker, row)
        self.assertNotEqual(second, third)

if __name__ == "__main__":
    unittest.main()
