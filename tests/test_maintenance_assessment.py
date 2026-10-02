from tests.support.catalog import plan_text
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.maintenance import full_assessment
from loop_manager_internal.storage import execplan_catalog_state
import loop_manager
from loop_manager_internal.maintenance import maintenance_planning
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, TrackedSource
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token


from tests.support.maintenance_planning_case import MaintenancePlanningCase, git

class MaintenanceAssessmentTest(MaintenancePlanningCase):



    def test_legacy_published_source_drift_restarts_instead_of_promoting(self):
        plan_commit, intent, _inventory = self._prepare_pushed_file_assessment_batch(legacy=True)
        other = Path(self.temporary.name) / "legacy-drift-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        (other / "legacy/module.py").write_text("# changed reviewed implementation\n", encoding="utf-8")
        git(other, "add", "legacy/module.py")
        git(other, "commit", "-m", "Change source after legacy publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        restarted = loop_manager.recover_maintenance_planning_batch(self.connection, str(self.repo), self.repo, "main")
        self.assertEqual(restarted.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        self.assertEqual(restarted.revision, intent.revision + 1)
        self.assertIsNone(restarted.publication_stage)
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(self.connection).last_success(str(self.repo)))

    def test_published_batch_unchanged_sources_promote_saved_evidence(self):
        plan_commit, _intent, inventory = self._prepare_pushed_file_assessment_batch()
        other = Path(self.temporary.name) / "unchanged-source-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        git(other, "commit", "--allow-empty", "-m", "Advance after publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(recovered.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(recovered.last_success_file_assessment_merged_count, 1)
        self.assertEqual(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)).source_revision, self.base_sha)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM successful_code_maps WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()[0], len(inventory.sources))

    def test_published_batch_source_drift_restarts_file_assessment(self):
        plan_commit, intent, _inventory = self._prepare_pushed_file_assessment_batch()
        other = Path(self.temporary.name) / "source-drift-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        source = other / "legacy/module.py"
        source.write_text("# changed after plan publication\n", encoding="utf-8")
        git(other, "add", "legacy/module.py")
        git(other, "commit", "-m", "Change reviewed source")
        git(other, "push", "origin", "HEAD:main")
        latest = git(other, "rev-parse", "HEAD")
        git(self.repo, "fetch", "origin")
        self.connection.execute(
            "UPDATE projects SET phase = 'PLANNING', active_sha = ?, worktree = ? "
            "WHERE repo = ?", (self.base_sha, str(self.repo), str(self.repo)),
        )

        with mock.patch(
            "loop_manager._clear_active_in_current_transaction",
            side_effect=RuntimeError("injected project cleanup failure"),
        ), self.assertRaisesRegex(RuntimeError, "injected project cleanup failure"):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main",
            )
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo)),
            intent,
        )

        restarted = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(restarted.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        self.assertEqual(restarted.revision, intent.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertIsNone(restarted.publication_stage)
        self.assertIsNone(restarted.last_success_file_assessment_merged_count)
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM file_review_ledger WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()[0], 0)
        maintenance_reconciliation.ensure_schema(self.connection)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM successful_code_maps WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (str(self.repo),),
        ).fetchone()[0], None)
        self.assertTrue((other / "docs/plans/[G-002] Extract Boundary.md").is_file())
        loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main", remote_sha=latest,
        )
        self.assertEqual({row[0] for row in self.connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ?", (str(self.repo),),
        )}, {"G-000", "G-001", "G-002", "G-003"})
        self.assertEqual(
            loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), other, latest,
            ).active_sha,
            latest,
        )

