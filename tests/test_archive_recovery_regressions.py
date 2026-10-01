"""Unit regressions for archive quarantine and revised publication recovery."""

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest import mock

import execplan_catalog
import execplan_catalog_state
import loop_manager
import runtime_state
from tests.test_execplan_catalog_state import archived_entry


class ArchiveRecoveryRegressionTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')"
        )
        self.entry = archived_entry()
        self.active = replace(self.entry, path=self.entry.active_path)
        self.addCleanup(self.connection.close)

    def migrate(self, *, archived):
        snapshot = execplan_catalog.ExecPlanCatalogSnapshot(
            active={} if archived else {"G-001": self.active},
            archived={"G-001": self.entry} if archived else {},
        )
        with (
            mock.patch("loop_manager.git"),
            mock.patch("loop_manager.pr_for_branch", return_value=None),
            mock.patch("loop_manager.revision_execplan_snapshot", return_value=snapshot),
        ):
            loop_manager.migrate_legacy_execplans(
                self.connection, reconcile_history=True, target_repo="/repo"
            )

    def insert_runtime(self, status):
        self.connection.execute(
            """INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha, branch, worktree,
                worker_session_id, reviewer_session_id, retry_count
            ) VALUES ('/repo', 'G-001', ?, 'Archived', '["docs/PRODUCT.md"]',
                      '[]', 'normal', ?, ?, ?, 'loop-manager/G-001',
                      '/owned/worktree', 'worker-session', 'reviewer-session', 3)""",
            (self.entry.active_path, self.entry.content_hash, status, "a" * 40),
        )
        self.connection.commit()

    def row(self):
        return self.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()

    def test_quarantine_restores_each_execution_phase_and_sessions(self):
        for status in sorted(runtime_state.ACTIVE_STATUSES):
            with self.subTest(status=status):
                self.connection.execute("DELETE FROM exec_plans")
                self.insert_runtime(status)
                runtime_state.RuntimeStateRepository(self.connection).hydrate(
                    "/repo", "G-001"
                )
                self.migrate(archived=True)
                self.migrate(archived=True)  # repeated reconciliation is idempotent
                self.assertEqual(self.row()["status"], "PLAN_BLOCKED")
                self.assertEqual(self.row()["archive_resume_status"], status)
                self.migrate(archived=False)
                self.migrate(archived=False)
                row = self.row()
                self.assertEqual(row["status"], status)
                self.assertIsNone(row["archive_resume_status"])
                self.assertEqual(row["worker_session_id"], "worker-session")
                self.assertEqual(row["reviewer_session_id"], "reviewer-session")
                self.assertEqual(row["retry_count"], 3)
                self.assertEqual(row["plan_sha"], "a" * 40)
                project = self.connection.execute("SELECT * FROM projects").fetchone()
                self.assertEqual(project["phase"], runtime_state.project_phase_for_status(status))
                self.assertEqual(project["plan_path"], self.entry.active_path)
                self.assertEqual(project["worktree"], "/owned/worktree")
                self.assertEqual(
                    loop_manager.catalog_schedule_decision(self.connection, "/repo")
                    .active["plan_id"], "G-001"
                )

    def test_unrelated_block_is_not_released(self):
        self.insert_runtime("PLAN_BLOCKED")
        self.migrate(archived=True)
        self.migrate(archived=False)
        self.assertEqual(self.row()["status"], "PLAN_BLOCKED")
        self.assertIsNone(self.row()["archive_resume_status"])

    def test_changed_active_bytes_do_not_release_quarantine(self):
        self.insert_runtime("VERIFYING")
        self.migrate(archived=True)
        self.active = replace(self.active, content_hash="different")
        self.migrate(archived=False)
        self.assertEqual(self.row()["status"], "PLAN_BLOCKED")
        self.active = replace(self.active, content_hash=self.entry.content_hash)
        self.migrate(archived=False)
        self.assertEqual(self.row()["status"], "VERIFYING")

    def test_quarantine_waits_for_other_project_phase_to_finish(self):
        self.insert_runtime("REVIEWING")
        self.migrate(archived=True)
        self.connection.execute(
            "UPDATE projects SET phase = 'PR_OPEN', plan_path = 'other-plan'"
        )
        self.migrate(archived=False)
        self.assertEqual(self.row()["status"], "PLAN_BLOCKED")
        self.assertEqual(
            self.connection.execute("SELECT phase FROM projects").fetchone()[0],
            "PR_OPEN",
        )
        self.connection.execute(
            "UPDATE projects SET phase = NULL, plan_path = NULL"
        )
        self.migrate(archived=False)
        self.assertEqual(self.row()["status"], "REVIEWING")

    def test_stored_revised_publication_sha_is_preserved(self):
        published = b"stored revision"
        identity = execplan_catalog_state.StoredPlanIdentity(
            "G-001", self.entry.active_path, hashlib.sha256(published).hexdigest(),
            "c" * 40, "MERGED",
        )
        with (
            mock.patch("loop_manager.pr_for_branch") as pr_for_branch,
            mock.patch(
                "loop_manager.git",
                return_value=subprocess.CompletedProcess([], 0, "d" * 40 + "\n", ""),
            ) as git,
            mock.patch("loop_manager.git_bytes", return_value=published) as blob,
            mock.patch("loop_manager._git_commit_is_ancestor", return_value=True),
        ):
            proof = loop_manager._LegacyCatalogMergeProof(
                Path("/repo"), "origin/main", {"G-001": identity}
            ).proof_for(self.entry)
        pr_for_branch.assert_not_called()
        git.assert_called_once_with(Path("/repo"), "rev-parse", "origin/main^{commit}")
        blob.assert_called_once_with(
            Path("/repo"), "show", "c" * 40 + ":" + self.entry.active_path
        )
        self.assertEqual(proof.plan_sha, "c" * 40)
        self.assertEqual(
            proof.kind, execplan_catalog_state.MergeProofKind.STORED_AND_GIT
        )

    def test_stored_merge_with_unavailable_git_does_not_query_pr_again(self):
        identity = execplan_catalog_state.StoredPlanIdentity(
            "G-001", self.entry.active_path, self.entry.content_hash,
            "c" * 40, "MERGED",
        )
        with (
            mock.patch("loop_manager.pr_for_branch") as pr_for_branch,
            mock.patch("loop_manager.git", side_effect=RuntimeError("git unavailable")),
        ):
            proof = loop_manager._LegacyCatalogMergeProof(
                Path("/repo"), "origin/main", {"G-001": identity}
            ).proof_for(self.entry)

        pr_for_branch.assert_not_called()
        self.assertFalse(proof.merged)
        self.assertEqual(proof.detail, "git unavailable")

    def publication_recovery(self, observed, published):
        revised_sha = "b" * 40
        snapshot = execplan_catalog.ExecPlanCatalogSnapshot(
            active={}, archived={"G-001": observed}
        )
        # External Git port: latest active publication is the maintenance
        # revision; the following archive commit has no active-path blob.
        with (
            mock.patch("loop_manager.pr_for_branch", return_value={
                "state": "MERGED", "url": "https://example.test/pr/1",
            }),
            mock.patch("loop_manager.git", return_value=subprocess.CompletedProcess(
                [], 0, revised_sha + "\n", ""
            )) as git,
            mock.patch("loop_manager.git_bytes", return_value=published) as blob,
        ):
            result = execplan_catalog_state.CatalogRecoveryService(
                execplan_catalog_state.CatalogStateRepository(self.connection),
                loop_manager._LegacyCatalogMergeProof(Path("/repo"), "origin/main", {}),
            ).reconcile("/repo", snapshot, {})
        git.assert_called_once_with(
            Path("/repo"), "log", "origin/main", "--no-renames",
            "--diff-filter=AM", "--format=%H", "-1", "--", observed.active_path,
        )
        blob.assert_called_once_with(
            Path("/repo"), "show", revised_sha + ":" + observed.active_path
        )
        return result

    def test_revised_archive_recovers_in_fresh_database(self):
        published = b"maintenance-revised publication"
        revised = replace(self.entry, content_hash=hashlib.sha256(published).hexdigest())
        result = self.publication_recovery(revised, published)
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.merged[0].plan_sha, "b" * 40)
        self.assertEqual(result.merged[0].original_content_hash, revised.content_hash)

    def test_archive_reverted_to_original_publication_is_rejected(self):
        result = self.publication_recovery(self.entry, b"maintenance-revised publication")
        self.assertEqual(result.merged, ())
        self.assertEqual(result.diagnostics[0].code, "archived-content-hash-mismatch")
