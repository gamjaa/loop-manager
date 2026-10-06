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

class MaintenancePublicationTest(MaintenancePlanningCase):
    def test_publication_requires_latest_single_commit_and_is_atomic_and_idempotent(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        loop_manager.validate_maintenance_publication_base(
            self.connection, str(self.repo), self.repo, "main", context
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        self.assertEqual(intent.publication_commit_sha, plan_commit)

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "fast-forward push"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )

        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 2 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "snapshot"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 0 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        self.connection.execute(
            "UPDATE exec_plans SET pr_head_oid = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("b" * 40, str(self.repo)),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "snapshot"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        self.connection.execute(
            "UPDATE exec_plans SET pr_head_oid = NULL "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        self.connection.execute(
            "UPDATE exec_plans SET pr_conflict_recovery = 1, "
            "pr_conflict_head_oid = ?, pr_conflict_base_oid = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("c" * 40, "d" * 40, str(self.repo)),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "snapshot"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        self.connection.execute(
            "UPDATE exec_plans SET pr_conflict_recovery = 0, "
            "pr_conflict_head_oid = NULL, pr_conflict_base_oid = NULL "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        cycle = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        repeated = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        rows = self.connection.execute(
            "SELECT plan_id, depends_on, plan_sha, current_revision_sha, "
            "publication_sha FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()

        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(repeated, cycle)
        self.assertEqual(cycle.refactoring_plan_ids, ("G-002", "G-003"))
        self.assertEqual(cycle.revalidation_plan_ids, ("G-001",))
        self.assertEqual(json.loads(rows[1]["depends_on"]), ["G-000", "G-003"])
        self.assertTrue(all(row["plan_sha"] == plan_commit for row in rows[1:]))
        self.assertTrue(
            all(row["current_revision_sha"] == plan_commit for row in rows[1:])
        )
        self.assertEqual(rows[1]["publication_sha"], self.base_sha)
        self.assertTrue(
            all(row["publication_sha"] == plan_commit for row in rows[2:])
        )

    def test_publication_rejects_body_drift_after_result_validation(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        validated = self.ready_path.read_text(encoding="utf-8")
        self.ready_path.write_text(
            validated.replace(
                "A concrete structural responsibility problem",
                "A tampered product goal",
            ),
            encoding="utf-8",
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Tamper with validated plan body")
        plan_commit = git(self.repo, "rev-parse", "HEAD")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "결과 검증 후 변경"
        ):
            loop_manager.record_maintenance_publication_intent(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        cycle = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertIsNone(cycle.publication_stage)

    def test_publication_rolls_back_new_and_revised_rows_if_cycle_update_fails(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare atomic maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        before = tuple(
            self.connection.execute(
                "SELECT depends_on, content_hash, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(self.repo),),
            ).fetchone()
        )

        with mock.patch.object(
            maintenance_state.MaintenanceRepository,
            "transition",
            side_effect=maintenance_state.MaintenanceStateError(
                "injected cycle update failure"
            ),
        ):
            with self.assertRaisesRegex(
                maintenance_state.MaintenanceStateError, "injected"
            ):
                loop_manager.publish_maintenance_planning_batch(
                    self.connection,
                    str(self.repo),
                    self.repo,
                    "main",
                    result,
                    plan_commit,
                )

        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        after = tuple(
            self.connection.execute(
                "SELECT depends_on, content_hash, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(self.repo),),
            ).fetchone()
        )
        self.assertEqual(after, before)
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(
                str(self.repo)
            ),
            intent,
        )

        completed = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        self.assertEqual(
            completed.phase, maintenance_state.MaintenancePhase.REFACTORING
        )

    def test_pushed_batch_recovers_from_durable_intent_after_restart(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare recoverable maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        row = self.connection.execute(
            "SELECT content_hash FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()
        expected_hash = str(row["content_hash"])
        self.connection.execute(
            "UPDATE exec_plans SET content_hash = 'catalog-drift' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "content hash"
        ):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main"
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )

        self.connection.execute(
            "UPDATE exec_plans SET content_hash = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (expected_hash, str(self.repo)),
        )
        self.connection.commit()
        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        repeated = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        rows = self.connection.execute(
            "SELECT plan_id, depends_on FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(repeated, recovered)
        self.assertEqual(
            [row["plan_id"] for row in rows],
            ["G-000", "G-001", "G-002", "G-003"],
        )
        self.assertEqual(json.loads(rows[1]["depends_on"]), ["G-000", "G-003"])

    def test_published_batch_recovers_after_remote_tip_advances(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main",
            result, plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        other = Path(self.temporary.name) / "published-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        git(other, "commit", "--allow-empty", "-m", "Advance after publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        repeated = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        rows = self.connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()

        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(repeated, recovered)
        self.assertEqual(recovered.publication_commit_sha, plan_commit)
        self.assertEqual(recovered.publication_remote_oid, plan_commit)
        self.assertEqual(
            [row["plan_id"] for row in rows],
            ["G-000", "G-001", "G-002", "G-003"],
        )
        self.assertNotEqual(recovered, intent)

    def test_legacy_pushed_intent_recovers_after_restart_without_claiming_root_review(self):
        plan_commit, _intent, inventory = self._prepare_pushed_file_assessment_batch(legacy=True)
        self.connection.commit()
        restarted_connection = sqlite3.connect(":memory:")
        self.addCleanup(restarted_connection.close)
        self.connection.backup(restarted_connection)
        restarted_connection.row_factory = sqlite3.Row
        # A new manager instance consumes only durable pre-policy evidence.
        recovered = loop_manager.recover_maintenance_planning_batch(
            restarted_connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(recovered.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(recovered.last_success_file_assessment_merged_count, 1)
        ledger = maintenance_file_ledger.FileReviewRepository(restarted_connection)
        self.assertEqual(ledger.last_success(str(self.repo)).source_revision, self.base_sha)
        self.assertEqual(len(ledger.reviews(str(self.repo))), len(inventory.sources))
        self.assertEqual(ledger.root_placements(str(self.repo)), ())
        self.assertIsNone(ledger.reusable_root_reviews(str(self.repo), inventory))
        recovered_again = loop_manager.recover_maintenance_planning_batch(
            restarted_connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(recovered_again, recovered)
        latest = maintenance_file_inventory.tracked_source_inventory(self.repo, "main", plan_commit)
        progress = loop_manager.maintenance_file_review.ProvisionalReviewRepository(restarted_connection).progress(
            str(self.repo), self.repo, "main", recovered.revision, latest,
        )
        self.assertEqual({source.path for source in progress.selected},
                         {source.path for source in latest.sources})
        self.assertIsNotNone(progress.next_unit)
        # Imported legacy judgments remain audit rows; no common version was recorded.
        self.assertEqual(len(ledger.reviews(str(self.repo))), len(inventory.sources))

    def test_published_batch_recovery_rejects_changed_batch_path(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main",
            result, plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        other = Path(self.temporary.name) / "changed-published-batch"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        changed_plan = other / self.ready_path.relative_to(self.repo)
        changed_plan.write_text(
            changed_plan.read_text(encoding="utf-8") + "\nRemote edit.\n",
            encoding="utf-8",
        )
        git(other, "add", changed_plan.relative_to(other).as_posix())
        git(other, "commit", "-m", "Change a published batch path")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "batch 경로"
        ):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main"
            )
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo)),
            intent,
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )

    def test_published_batch_recovery_keeps_added_user_plan_unobserved(self):
        self._assert_published_batch_recovery_preserves_catalog_drift("addition")

    def test_published_batch_recovery_keeps_modified_user_plan_unobserved(self):
        self._assert_published_batch_recovery_preserves_catalog_drift("modification")

    def test_publication_preflight_restarts_when_origin_base_moves(self):
        context = self._begin_barrier()
        before = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        git(self.repo, "commit", "--allow-empty", "-m", "External base move")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        latest = git(self.repo, "rev-parse", "HEAD")

        with self.assertRaises(loop_manager.MaintenancePlanningRestart) as caught:
            loop_manager.validate_maintenance_publication_base(
                self.connection, str(self.repo), self.repo, "main", context
            )
        restarted = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertEqual(caught.exception.new_base_sha, latest)
        self.assertEqual(caught.exception.cycle_revision, restarted.revision)
        self.assertEqual(restarted.phase, before.phase)
        self.assertEqual(restarted.revision, before.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertEqual(restarted.refactoring_plan_ids, before.refactoring_plan_ids)
        self.assertEqual(restarted.revalidation_plan_ids, before.revalidation_plan_ids)
        self.assertIsNone(restarted.publication_stage)
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "단계 또는 revision"
        ):
            loop_manager.validate_maintenance_publication_base(
                self.connection, str(self.repo), self.repo, "main", context
            )
        fresh = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, latest
        )
        self.assertEqual(fresh.active_sha, latest)
        self.assertEqual(fresh.cycle_revision, restarted.revision)
        loop_manager.validate_maintenance_publication_base(
            self.connection, str(self.repo), self.repo, "main", fresh
        )

    def test_refactoring_publication_promotes_file_evidence_once_after_remote_push(self):
        context = self._begin_barrier()
        self.connection.execute(
            "UPDATE maintenance_cycles SET file_assessment_start_merged_count = 1 "
            "WHERE repo = ?", (str(self.repo),),
        )
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, 'catalog')",
            (str(self.repo), self.base_sha),
        )
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        inventory = maintenance_file_inventory.tracked_source_inventory(
            self.repo, "main", self.base_sha,
        )
        reviewed = tuple(maintenance_file_ledger.ReviewedSource(
            source.path, source.content_hash, source.mode, source.revision,
            "Reviewed implementation, dependencies, and related tests.",
        ) for source in inventory.sources)
        maps = tuple((source.path, json.dumps({
            "responsibility": "Review the module responsibility",
            "entry_points": [], "public_symbols": [],
            "direct_references": [], "related_tests": [],
        })) for source in inventory.sources)
        from tests.support.maintenance import root_results_for
        evidence = maintenance_file_publication.AssessmentEvidence(
            inventory, reviewed, maps, 1,
            root_results_for(self.repo, inventory),
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish refactoring plans")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.object(
            maintenance_file_publication, "collect", return_value=evidence,
        ):
            loop_manager.record_maintenance_publication_intent(
                self.connection, str(self.repo), self.repo, "main", result,
                plan_commit,
            )
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        with mock.patch.object(
            maintenance_state.MaintenanceRepository, "transition",
            side_effect=maintenance_state.MaintenanceStateError("injected"),
        ):
            with self.assertRaisesRegex(maintenance_state.MaintenanceStateError, "injected"):
                loop_manager.publish_maintenance_planning_batch(
                    self.connection, str(self.repo), self.repo, "main", result,
                    plan_commit,
                )
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        self.assertIsNone(maintenance_state.MaintenanceRepository(
            self.connection,
        ).get(str(self.repo)).last_success_file_assessment_merged_count)
        published = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(published.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(published.last_success_file_assessment_merged_count, 1)
        self.assertEqual(len(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).reviews(str(self.repo))), len(inventory.sources))
        repeated = loop_manager.publish_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main", result,
            plan_commit,
        )
        self.assertEqual(repeated, published)

    def test_legacy_publication_compatibility_rejects_tampered_and_new_incomplete_evidence(self):
        from dataclasses import replace
        plan_commit, intent, _inventory = self._prepare_pushed_file_assessment_batch(legacy=True)
        row = self.connection.execute("SELECT evidence_json FROM file_assessment_publications WHERE repo = ?",
                                      (str(self.repo),)).fetchone()
        payload = json.loads(row[0])
        evidence = maintenance_file_publication.saved_intent(
            self.connection, str(self.repo), intent.revision, self.base_sha, plan_commit)
        self.assertIsNone(evidence.root_reviews)
        with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
            maintenance_file_publication.promote(self.connection, str(self.repo), self.repo, "main", plan_commit,
                                                replace(evidence, merged_count=999))
        with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
            maintenance_file_publication.save_intent(self.connection, str(self.repo), intent.revision,
                                                    self.base_sha, plan_commit, evidence)
        # An explicit empty array belongs to the new contract, never the legacy exemption.
        payload["root_reviews"] = []
        self.connection.execute("UPDATE file_assessment_publications SET evidence_json = ? WHERE repo = ?",
                                (json.dumps(payload), str(self.repo)))
        modern = maintenance_file_publication.saved_intent(self.connection, str(self.repo), intent.revision,
                                                         self.base_sha, plan_commit)
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            maintenance_file_publication.promote(self.connection, str(self.repo), self.repo, "main", plan_commit, modern)
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(self.connection).last_success(str(self.repo)))
