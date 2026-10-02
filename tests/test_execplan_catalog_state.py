from tests.support.catalog import CatalogReconciliationFixture
from tests.support.catalog import archived_entry, FakeDefinitionPort, active_snapshot
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
from loop_manager_internal.storage import maintenance_state
import loop_manager


class FakeMergeProof:
    def __init__(self, proof: execplan_catalog_state.MergeProof):
        self.proof = proof
        self.calls = []
        self.local_calls = []

    def verify_stored(self, entry, identity):
        self.local_calls.append(identity)
        return execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.STORED_AND_GIT,
            plan_sha=identity.plan_sha,
            publication_content_hash=identity.content_hash,
            revision="b" * 40,
        )

    def proof_for(self, entry):
        self.calls.append(entry.plan_id)
        return self.proof


class ExecPlanCatalogStateTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.repository = execplan_catalog_state.CatalogStateRepository(
            self.connection
        )

    def tearDown(self):
        self.connection.close()

    def _recover(self, entry, proof, stored=None):
        snapshot = execplan_catalog.ExecPlanCatalogSnapshot(
            active={}, archived={entry.plan_id: entry}
        )
        port = FakeMergeProof(proof)
        result = execplan_catalog_state.CatalogRecoveryService(
            self.repository, port
        ).reconcile("/repo", snapshot, stored or {})
        return result, port

    def _record_verified_history(self, entry, *, original=None, final=None):
        original = original or entry
        final = final or entry
        self.repository.record(
            "/repo",
            final,
            original_path=original.active_path,
            original_content_hash=original.content_hash,
            plan_sha="a" * 40,
            proof=execplan_catalog_state.MergeProof(
                True,
                kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
                plan_sha="a" * 40,
                publication_content_hash=original.content_hash,
                revision="b" * 40,
                pr_url="https://example.test/pr/1",
            ),
            diagnostic=None,
            identity_verified=True,
        )

    def test_recovers_archived_plan_only_with_merge_and_publication_proof(self):
        entry = archived_entry()
        proof = execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
            plan_sha="a" * 40,
            publication_content_hash=entry.content_hash,
            revision="b" * 40,
            pr_url="https://example.test/pr/1",
        )

        result, port = self._recover(entry, proof)

        self.assertEqual(port.calls, ["G-001"])
        self.assertEqual(len(result.merged), 1)
        self.assertEqual(
            result.merged[0].original_path,
            "docs/plans/[G-001] Archived.md",
        )
        row = self.repository.historical_row("/repo", "G-001")
        self.assertEqual(row["observed_path"], entry.path)
        self.assertEqual(row["original_path"], entry.active_path)
        self.assertEqual(row["final_content_hash"], entry.content_hash)
        self.assertEqual(row["merge_confirmed"], 1)

    def test_file_location_without_merge_proof_stays_non_executable(self):
        entry = archived_entry()

        result, _port = self._recover(
            entry, execplan_catalog_state.MergeProof(False)
        )

        self.assertEqual(result.merged, ())
        self.assertEqual(result.diagnostics[0].code, "merge-proof-missing")
        row = self.repository.historical_row("/repo", "G-001")
        self.assertEqual(row["merge_confirmed"], 0)
        self.assertEqual(row["diagnostic_code"], "merge-proof-missing")

    def test_verified_id_rejects_archive_delta_from_current_cache(self):
        entry = archived_entry(text_suffix=" changed")
        published_hash = hashlib.sha256(b"original").hexdigest()
        stored = {
            "G-001": execplan_catalog_state.StoredPlanIdentity(
                plan_id="G-001",
                path="docs/plans/[G-001] Original.md",
                content_hash=published_hash,
                plan_sha="a" * 40,
                status="MERGED",
            )
        }

        result, port = self._recover(
            entry,
            execplan_catalog_state.MergeProof(
                True,
                kind=execplan_catalog_state.MergeProofKind.GIT_ANCESTRY,
                plan_sha="a" * 40,
                publication_content_hash=published_hash,
            ),
            stored,
        )

        self.assertEqual(port.calls, ["G-001"])
        self.assertEqual(result.merged, ())
        self.assertEqual(
            result.diagnostics[0].code, "archived-content-hash-mismatch"
        )
        row = self.repository.historical_row("/repo", "G-001")
        self.assertEqual(row["original_path"], "docs/plans/[G-001] Original.md")
        self.assertEqual(row["observed_path"], entry.path)
        self.assertNotEqual(row["original_content_hash"], row["final_content_hash"])

    def test_first_observed_hash_remains_stable_before_proof_is_available(self):
        original = archived_entry()
        first, _port = self._recover(
            original, execplan_catalog_state.MergeProof(False)
        )
        self.assertEqual(first.diagnostics[0].code, "merge-proof-missing")

        changed = archived_entry(text_suffix=" tampered")
        second, port = self._recover(
            changed,
            execplan_catalog_state.MergeProof(
                True,
                kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
                plan_sha="a" * 40,
                publication_content_hash=original.content_hash,
            ),
        )

        self.assertEqual(port.calls, ["G-001"])
        self.assertEqual(
            second.diagnostics[0].code, "archived-content-hash-mismatch"
        )
        row = self.repository.historical_row("/repo", "G-001")
        self.assertEqual(row["original_content_hash"], original.content_hash)
        self.assertEqual(row["final_content_hash"], changed.content_hash)
        self.assertEqual(row["identity_verified"], 1)

    def test_fresh_recovery_rejects_archive_changed_before_first_observation(self):
        published = archived_entry()
        tampered = archived_entry(text_suffix=" changed before recovery")

        result, port = self._recover(
            tampered,
            execplan_catalog_state.MergeProof(
                True,
                kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
                plan_sha="a" * 40,
                publication_content_hash=published.content_hash,
            ),
        )

        self.assertEqual(port.calls, ["G-001"])
        self.assertEqual(result.merged, ())
        self.assertEqual(
            result.diagnostics[0].code, "archived-content-hash-mismatch"
        )
        row = self.repository.historical_row("/repo", "G-001")
        self.assertEqual(row["original_content_hash"], published.content_hash)
        self.assertEqual(row["final_content_hash"], tampered.content_hash)
        self.assertEqual(row["identity_verified"], 1)
        self.assertEqual(row["merge_confirmed"], 0)

    def test_verified_id_accepts_revised_archive_hash(self):
        published = archived_entry()
        revised = archived_entry(text_suffix=" valid reconciled revision")
        stored = {
            "G-001": execplan_catalog_state.StoredPlanIdentity(
                plan_id="G-001",
                path=published.active_path,
                content_hash=published.content_hash,
                plan_sha="a" * 40,
                status="MERGED",
                current_content_hash=revised.content_hash,
            )
        }

        result, _port = self._recover(
            revised,
            execplan_catalog_state.MergeProof(
                True,
                kind=execplan_catalog_state.MergeProofKind.STORED_AND_GIT,
                plan_sha="a" * 40,
                publication_content_hash=published.content_hash,
            ),
            stored,
        )

        self.assertEqual(len(result.merged), 1)
        row = self.repository.historical_row("/repo", "G-001")
        self.assertEqual(row["original_content_hash"], published.content_hash)
        self.assertEqual(row["final_content_hash"], revised.content_hash)

    def test_verified_audit_only_history_reuses_merge_proof_without_port_call(self):
        entry = archived_entry()
        self._record_verified_history(entry)

        result, port = self._recover(
            entry, execplan_catalog_state.MergeProof(False)
        )

        self.assertEqual(port.calls, [])
        self.assertEqual(len(result.merged), 1)
        self.assertEqual(
            result.merged[0].proof.kind,
            execplan_catalog_state.MergeProofKind.STORED_AND_GIT,
        )
        self.assertEqual(len(port.local_calls), 1)
        self.assertEqual(result.merged[0].proof.pr_url, "https://example.test/pr/1")

    def test_verified_audit_only_history_rejects_tampered_archive_without_port_call(self):
        original = archived_entry()
        self._record_verified_history(original)
        tampered = archived_entry(text_suffix=" tampered after archive")

        result, port = self._recover(
            tampered, execplan_catalog_state.MergeProof(False)
        )

        self.assertEqual(port.calls, [])
        self.assertEqual(result.merged, ())
        self.assertEqual(result.diagnostics[0].code, "archived-content-hash-mismatch")

    def test_verified_history_with_conflicting_runtime_identity_is_not_reused(self):
        entry = archived_entry()
        self.repository.record(
            "/repo",
            entry,
            original_path="docs/plans/[G-001] Different.md",
            original_content_hash=entry.content_hash,
            plan_sha="a" * 40,
            proof=execplan_catalog_state.MergeProof(
                True,
                kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
                plan_sha="a" * 40,
                publication_content_hash=entry.content_hash,
                revision="b" * 40,
            ),
            diagnostic=None,
            identity_verified=True,
        )

        result, port = self._recover(
            entry, execplan_catalog_state.MergeProof(False),
            {entry.plan_id: execplan_catalog_state.StoredPlanIdentity(
                entry.plan_id, entry.active_path, entry.content_hash,
                "a" * 40, "MERGED",
            )},
        )

        self.assertEqual(port.calls, [])
        self.assertEqual(port.local_calls, [])
        self.assertEqual(result.merged, ())
        self.assertEqual(result.diagnostics[0].code, "stable-identity-mismatch")

    def test_incomplete_confirmed_history_never_falls_back_to_merge_lookup(self):
        entry = archived_entry()
        self._record_verified_history(entry)
        self.connection.execute(
            "UPDATE execplan_catalog_history SET proof_revision = NULL"
        )
        result, port = self._recover(entry, execplan_catalog_state.MergeProof(False))
        self.assertEqual(port.calls, [])
        self.assertEqual(port.local_calls, [])
        self.assertEqual(result.merged, ())
        self.assertEqual(result.diagnostics[0].code, "publication-proof-missing")


class ArchivedDependencySelectionTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        loop_manager.notifications.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')"
        )

    def tearDown(self):
        self.connection.close()

    def _insert_plan(self, plan_id, status, depends_on=()):
        title = f"Plan {plan_id}"
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha
            ) VALUES ('/repo', ?, ?, ?, '["docs/PRODUCT.md"]', ?,
                      'normal', ?, ?, ?)
            """,
            (
                plan_id,
                f"docs/plans/[{plan_id}] {title}.md",
                title,
                json.dumps(depends_on),
                f"hash-{plan_id}",
                status,
                "a" * 40,
            ),
        )

    def _record_historical(self, plan_id, *, confirmed):
        entry = archived_entry(plan_id, f"Plan {plan_id}")
        execplan_catalog_state.CatalogStateRepository(self.connection).record(
            "/repo",
            entry,
            original_path=entry.active_path,
            original_content_hash=f"hash-{plan_id}",
            plan_sha="a" * 40,
            proof=execplan_catalog_state.MergeProof(
                confirmed,
                kind=(
                    execplan_catalog_state.MergeProofKind.GITHUB_PR
                    if confirmed
                    else execplan_catalog_state.MergeProofKind.NONE
                ),
            ),
            diagnostic=(
                None
                if confirmed
                else execplan_catalog_state.CatalogDiagnostic(
                    plan_id, entry.path, "merge-proof-missing", "missing"
                )
            ),
            identity_verified=True,
        )

    def test_confirmed_archived_merged_row_satisfies_active_dependency(self):
        self._insert_plan("G-001", "MERGED")
        self._record_historical("G-001", confirmed=True)
        self._insert_plan("G-002", "READY", ("G-001",))
        self.connection.commit()

        decision = loop_manager.catalog_schedule_decision(
            self.connection, "/repo"
        )
        compact = json.loads(
            loop_manager.compact_execplan_catalog(self.connection, "/repo")
        )

        self.assertEqual(decision.runnable_ready["plan_id"], "G-002")
        self.assertEqual([item["id"] for item in compact], ["G-002"])

    def test_unverified_archived_ready_row_is_never_selected_or_prompted(self):
        self._insert_plan("G-001", "READY")
        self._record_historical("G-001", confirmed=False)
        self.connection.commit()

        decision = loop_manager.catalog_schedule_decision(
            self.connection, "/repo"
        )

        self.assertFalse(decision.has_ready)
        self.assertIsNone(decision.runnable_ready)
        self.assertEqual(
            loop_manager.compact_execplan_catalog(self.connection, "/repo"),
            "[]",
        )

    def test_unverified_archived_merged_row_does_not_satisfy_dependency(self):
        self._insert_plan("G-001", "MERGED")
        self._record_historical("G-001", confirmed=False)
        self._insert_plan("G-002", "READY", ("G-001",))
        self.connection.commit()

        decision = loop_manager.catalog_schedule_decision(
            self.connection, "/repo"
        )
        compact = json.loads(
            loop_manager.compact_execplan_catalog(self.connection, "/repo")
        )

        self.assertTrue(decision.has_ready)
        self.assertIsNone(decision.runnable_ready)
        self.assertEqual([item["id"] for item in compact], ["G-002"])

    def test_maintenance_merge_count_ignores_unverified_archive_history(self):
        self._insert_plan("G-001", "MERGED")
        self._record_historical("G-001", confirmed=False)
        self._insert_plan("G-002", "MERGED")
        self._record_historical("G-002", confirmed=True)
        self._insert_plan("G-003", "MERGED")
        self.connection.commit()

        count = maintenance_state.MaintenanceRepository(
            self.connection
        )._merged_count("/repo")

        self.assertEqual(count, 2)

    def test_durable_merge_with_archive_diagnostic_does_not_satisfy_dependency(self):
        from loop_manager_internal.notifications import work_exhaustion

        self._insert_plan("G-001", "MERGED")
        self._record_historical("G-001", confirmed=True)
        self._insert_plan("G-002", "READY", ("G-001",))
        self.connection.execute(
            "UPDATE execplan_catalog_history SET diagnostic_code = "
            "'archived-content-hash-mismatch', diagnostic_message = 'tampered' "
            "WHERE plan_id = 'G-001'"
        )
        self.connection.commit()

        decision = loop_manager.catalog_schedule_decision(self.connection, "/repo")
        self.assertIsNone(decision.runnable_ready)
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection)._merged_count("/repo"), 0,
        )
        snapshot = work_exhaustion.collect(
            self.connection, "/repo", cycle="cycle", active_requirements=(),
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.plans[0]["dependencies"], [
            {"id": "G-001", "status": "HISTORICAL_UNVERIFIED"},
        ])


    def test_archive_diagnostic_prevents_maintenance_cycle_advancement(self):
        self._insert_plan("G-001", "MERGED")
        self._record_historical("G-001", confirmed=True)
        self.connection.execute(
            "UPDATE execplan_catalog_history SET diagnostic_code = 'merge-proof-missing'"
        )
        cycle = mock.Mock(
            phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            prerequisite_plan_ids=("G-001",),
        )
        with (
            mock.patch.object(maintenance_state.MaintenanceRepository, "get", return_value=cycle),
            mock.patch.object(maintenance_state.MaintenanceRepository, "transition") as transition,
            self.assertRaises(loop_manager.MaintenancePlanningError),
        ):
            loop_manager._advance_maintenance_cycle_after_merges(self.connection, "/repo", "base")
        transition.assert_not_called()

    def test_archive_diagnostic_prevents_related_requirement_candidate(self):
        self._insert_plan("G-001", "MERGED")
        self._record_historical("G-001", confirmed=True)
        self._insert_plan("G-002", "READY")
        source = "docs/requirements/[R-001] Shared.md"
        catalog = {
            plan_id: {**archived_entry(plan_id, f"Plan {plan_id}").metadata(),
                      "sources": [source]}
            for plan_id in ("G-001", "G-002")
        }
        with (
            mock.patch.object(loop_manager, "_revision_execplan_catalog", return_value=catalog),
            mock.patch.object(loop_manager, "_requirement_archive_tree", return_value={
                source.encode(): (b"blob", b"oid"),
            }),
        ):
            self.assertEqual(loop_manager.requirement_archive_snapshot(
                self.connection, Path("/repo"), "/repo", "base", "G-002",
            ).candidates, (source,))
            self.connection.execute(
                "UPDATE execplan_catalog_history SET diagnostic_code = 'merge-proof-missing'"
            )
            self.assertEqual(loop_manager.requirement_archive_snapshot(
                self.connection, Path("/repo"), "/repo", "base", "G-002",
            ).candidates, ())


class CatalogReconciliationTest(CatalogReconciliationFixture, unittest.TestCase):


    def test_additive_migration_backfills_publication_audit_and_current_revision(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        legacy_schema = loop_manager.EXEC_PLANS_SCHEMA
        for line in (
            "    current_complexity TEXT,\n",
            "    current_risk TEXT,\n",
            "    current_revision_sha TEXT,\n",
            "    catalog_generation INTEGER NOT NULL DEFAULT 0,\n",
            "    publication_path TEXT,\n",
            "    publication_content_hash TEXT,\n",
            "    publication_sha TEXT,\n",
        ):
            legacy_schema = legacy_schema.replace(line, "")
        connection.execute(legacy_schema)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('legacy', 'main')"
        )
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha
            ) VALUES ('legacy', 'G-001', 'docs/plans/[G-001] Legacy.md',
                      'Legacy', '["docs/PRODUCT.md"]', '[]', 'normal',
                      'legacy-hash', 'READY', ?)
            """,
            ("a" * 40,),
        )

        loop_manager._ensure_execplans_schema(connection)

        row = connection.execute(
            "SELECT publication_path, publication_content_hash, publication_sha, "
            "current_revision_sha, catalog_generation FROM exec_plans"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                "docs/plans/[G-001] Legacy.md",
                "legacy-hash",
                "a" * 40,
                "a" * 40,
                0,
            ),
        )

    def test_migration_resumes_backfill_after_columns_were_committed(self):
        connection = self.connection
        connection.execute(
            "UPDATE exec_plans SET publication_path = NULL, "
            "publication_content_hash = NULL, publication_sha = NULL, "
            "current_revision_sha = NULL WHERE plan_id = 'G-001'"
        )
        connection.commit()
        loop_manager._ensure_execplans_schema(connection)
        self.assertFalse(connection.in_transaction)
        audit = self.repository.publication_audit('/repo', 'G-001')
        self.assertEqual(audit.path, 'docs/plans/[G-001] Old.md')
        self.assertEqual(audit.content_hash, 'old-hash-G-001')
        self.assertEqual(audit.revision_sha, 'a' * 40)
        self.assertEqual(
            self.repository.current_definition('/repo', 'G-001').revision_sha,
            'a' * 40,
        )
        connection.execute(
            "UPDATE exec_plans SET path = 'changed', content_hash = 'changed' "
            "WHERE plan_id = 'G-001'"
        )
        loop_manager._ensure_execplans_schema(connection)
        self.assertTrue(connection.in_transaction)
        self.assertEqual(self.repository.publication_audit('/repo', 'G-001'), audit)
        connection.rollback()
        self.assertEqual(
            self.repository.current_definition('/repo', 'G-001').path, audit.path
        )

    def test_all_definition_deltas_update_atomically_without_runtime_loss(self):
        source = "docs/requirements/New Source.md"
        review_history = '{"2":{"review-fingerprint":1}}'
        self.connection.execute(
            "UPDATE exec_plans SET review_level_history = ? "
            "WHERE plan_id = 'G-001'",
            (review_history,),
        )
        self.connection.commit()
        pinned = active_snapshot(
            "b" * 40,
            {
                "docs/plans/[G-001] Renamed.md": self._text(
                    "G-001",
                    "Renamed",
                    sources=(source,),
                    depends_on=("G-002",),
                    complexity="high",
                    risk="critical",
                    priority="critical",
                    body="Changed body.",
                ),
                "docs/plans/[G-002] Dependency.md": self._text(
                    "G-002", "Dependency"
                ),
            },
        )

        result = self._service(pinned).reconcile("/repo")

        self.assertEqual(
            result.kind, execplan_catalog_state.ReconciliationKind.RECONCILED
        )
        row = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = '/repo' AND plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(row["path"], "docs/plans/[G-001] Renamed.md")
        self.assertEqual(row["title"], "Renamed")
        self.assertEqual(json.loads(row["sources"]), [source])
        self.assertEqual(json.loads(row["depends_on"]), ["G-002"])
        self.assertEqual(row["priority"], "critical")
        self.assertEqual(row["current_complexity"], "high")
        self.assertEqual(row["current_risk"], "critical")
        self.assertEqual(row["current_revision_sha"], "b" * 40)
        self.assertEqual(row["initial_worker_level"], 7)
        self.assertEqual(row["effective_worker_level"], 8)
        self.assertEqual(row["status"], "IMPLEMENTING")
        self.assertEqual(row["retry_count"], 4)
        self.assertEqual(row["worker_session_id"], "worker")
        self.assertEqual(row["reviewer_session_id"], "reviewer")
        self.assertEqual(row["branch"], "branch")
        self.assertEqual(row["worktree"], "/worktree")
        self.assertEqual(row["workspace_owned"], 1)
        self.assertEqual(row["pr_url"], "https://example/pr/1")
        self.assertEqual(row["review_level_history"], review_history)
        self.assertEqual(
            loop_manager.ensure_worker_level_state(
                self.connection,
                "/repo",
                self.repository.current_definition(
                    "/repo", "G-001"
                ).metadata(),
            ),
            8,
        )
        audit = self.repository.publication_audit("/repo", "G-001")
        self.assertEqual(audit.path, "docs/plans/[G-001] Old.md")
        self.assertEqual(audit.content_hash, "old-hash-G-001")
        self.assertEqual(audit.revision_sha, "a" * 40)
        project = self.connection.execute(
            "SELECT phase, plan_path, branch, worktree, worker_session_id, pr_url "
            "FROM projects WHERE repo = '/repo'"
        ).fetchone()
        self.assertEqual(
            tuple(project),
            (
                "WORKING",
                "docs/plans/[G-001] Renamed.md",
                "branch",
                "/worktree",
                "project-worker",
                "https://example/pr/1",
            ),
        )
        repeated = self._service(pinned).reconcile("/repo")
        self.assertEqual(repeated.changed_plan_ids, ())

    def test_ready_plan_uses_recomputed_level_but_started_plan_never_deescalates(self):
        self.connection.execute(
            "UPDATE exec_plans SET status = 'READY', initial_worker_level = 7, "
            "effective_worker_level = 7 WHERE plan_id = 'G-002'"
        )
        self.connection.commit()
        pinned = active_snapshot(
            "c" * 40,
            {
                "docs/plans/[G-001] Old.md": self._text(
                    "G-001", "Old", complexity="low", risk="low"
                ),
                "docs/plans/[G-002] Dependency.md": self._text(
                    "G-002", "Dependency", complexity="low", risk="low"
                ),
            },
        )

        self._service(pinned).reconcile("/repo")

        started = self.connection.execute(
            "SELECT initial_worker_level, effective_worker_level "
            "FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()
        ready = self.connection.execute(
            "SELECT initial_worker_level, effective_worker_level "
            "FROM exec_plans WHERE plan_id = 'G-002'"
        ).fetchone()
        self.assertEqual(tuple(started), (1, 8))
        self.assertEqual(tuple(ready), (1, 1))

    def test_dependency_and_priority_deltas_drive_only_the_next_selection(self):
        self.connection.execute(
            "UPDATE projects SET phase = NULL, plan_path = NULL, branch = NULL, "
            "worktree = NULL, worker_session_id = NULL, pr_url = NULL "
            "WHERE repo = '/repo'"
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'READY', branch = NULL, "
            "worktree = NULL, workspace_owned = 0, worker_session_id = NULL, "
            "reviewer_session_id = NULL, pr_url = NULL WHERE plan_id = 'G-001'"
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'READY' WHERE plan_id = 'G-002'"
        )
        self._insert("G-003", "Priority Winner", status="READY")
        self.connection.commit()
        pinned = active_snapshot(
            "3" * 40,
            {
                "docs/plans/[G-001] Old.md": self._text(
                    "G-001", "Old", depends_on=("G-002",),
                    priority="critical",
                ),
                "docs/plans/[G-002] Dependency.md": self._text(
                    "G-002", "Dependency", priority="low"
                ),
                "docs/plans/[G-003] Priority Winner.md": self._text(
                    "G-003", "Priority Winner", priority="critical"
                ),
            },
        )

        self._service(pinned).reconcile("/repo")
        selected = loop_manager.select_ready_execplan(
            self.connection, "/repo"
        )

        self.assertEqual(selected["plan_id"], "G-003")
        blocked = self.connection.execute(
            "SELECT depends_on FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(json.loads(blocked["depends_on"]), ["G-002"])

    def test_base_race_and_transaction_interruption_leave_no_partial_delta(self):
        pinned = active_snapshot(
            "d" * 40,
            {
                "docs/plans/[G-001] Changed.md": self._text(
                    "G-001", "Changed"
                ),
                "docs/plans/[G-002] Dependency.md": self._text(
                    "G-002", "Dependency", priority="high"
                ),
            },
        )
        result = self._service(
            pinned, revisions=["d" * 40, "d" * 40, "e" * 40]
        ).reconcile("/repo")
        self.assertEqual(
            result.kind, execplan_catalog_state.ReconciliationKind.STALE
        )
        rows = self.connection.execute(
            "SELECT title, priority FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("Old", "normal"), ("Dependency", "normal")])
        recovered = self._service(pinned).reconcile("/repo")
        self.assertEqual(
            recovered.kind,
            execplan_catalog_state.ReconciliationKind.RECONCILED,
        )
        rows = self.connection.execute(
            "SELECT title, priority FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("Changed", "normal"), ("Dependency", "high")],
        )

    def test_stale_sqlite_ownership_snapshot_is_not_overwritten(self):
        pinned = active_snapshot(
            "e" * 40,
            {
                "docs/plans/[G-001] Changed.md": self._text(
                    "G-001", "Changed"
                ),
                "docs/plans/[G-002] Dependency.md": self._text(
                    "G-002", "Dependency"
                ),
            },
        )
        connection = self.connection

        class MutatingPort(FakeDefinitionPort):
            def current_revision(self):
                connection.execute(
                    "UPDATE exec_plans SET worker_session_id = 'new-owner' "
                    "WHERE plan_id = 'G-001'"
                )
                connection.commit()
                return self.pinned.revision_sha

        result = execplan_catalog_state.CatalogReconciliationService(
            self.repository, MutatingPort(pinned)
        ).reconcile("/repo")

        self.assertEqual(
            result.kind, execplan_catalog_state.ReconciliationKind.STALE
        )
        row = self.connection.execute(
            "SELECT title, worker_session_id FROM exec_plans "
            "WHERE plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(tuple(row), ("Old", "new-owner"))

        def interrupt(stage):
            if stage == "after-plan:G-001":
                raise RuntimeError("interrupted")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self._service(pinned, fault_hook=interrupt).reconcile("/repo")
        rows = self.connection.execute(
            "SELECT title, priority FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("Old", "normal"), ("Dependency", "normal")])

        recovered = self._service(pinned).reconcile("/repo")
        self.assertEqual(
            recovered.kind,
            execplan_catalog_state.ReconciliationKind.RECONCILED,
        )
        rows = self.connection.execute(
            "SELECT title, priority FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("Changed", "normal"), ("Dependency", "normal")],
        )

    def test_missing_id_is_invalid_and_new_id_is_registered_once(self):
        pinned = active_snapshot(
            "f" * 40,
            {
                "docs/plans/[G-001] Old.md": self._text("G-001", "Old"),
            },
        )

        result = self._service(pinned).reconcile("/repo")

        self.assertEqual(
            result.kind,
            execplan_catalog_state.ReconciliationKind.INVALID_CATALOG,
        )
        self.assertEqual(result.diagnostic.code, "missing-plan")
        self.assertIn("G-002", result.diagnostic.message)
        rows = self.connection.execute(
            "SELECT title, current_revision_sha FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("Old", "a" * 40), ("Dependency", "a" * 40)],
        )

        unknown = active_snapshot(
            "f" * 40,
            {
                "docs/plans/[G-001] Old.md": self._text("G-001", "Old"),
                "docs/plans/[G-002] Dependency.md": self._text(
                    "G-002", "Dependency"
                ),
                "docs/plans/[G-003] Unknown.md": self._text(
                    "G-003", "Unknown"
                ),
            },
        )
        result = self._service(unknown).reconcile("/repo")
        self.assertEqual(result.kind, execplan_catalog_state.ReconciliationKind.RECONCILED)
        self.assertIn("G-003", result.changed_plan_ids)
        row = self.connection.execute(
            "SELECT status, path, publication_path, publication_content_hash, "
            "publication_sha, plan_sha, current_revision_sha, initial_worker_level, "
            "effective_worker_level FROM exec_plans WHERE plan_id = 'G-003'"
        ).fetchone()
        self.assertEqual(row["status"], "READY")
        self.assertEqual(row["path"], "docs/plans/[G-003] Unknown.md")
        self.assertEqual(row["publication_path"], row["path"])
        self.assertEqual(
            row["publication_content_hash"],
            unknown.snapshot.active["G-003"].content_hash,
        )
        self.assertEqual(row["publication_sha"], "f" * 40)
        self.assertEqual(row["plan_sha"], "f" * 40)
        self.assertEqual(row["current_revision_sha"], "f" * 40)
        self.assertEqual((row["initial_worker_level"], row["effective_worker_level"]), (1, 1))
        self.assertEqual(self._service(unknown).reconcile("/repo").changed_plan_ids, ())

    def test_multiple_new_ids_and_existing_delta_commit_together(self):
        pinned = active_snapshot("b" * 40, {
            "docs/plans/[G-001] New Title.md": self._text("G-001", "New Title"),
            "docs/plans/[G-002] Dependency.md": self._text("G-002", "Dependency"),
            "docs/plans/[G-003] Third.md": self._text(
                "G-003", "Third", depends_on=("G-002",), priority="critical",
            ),
            "docs/plans/[G-004] Fourth.md": self._text(
                "G-004", "Fourth", depends_on=("G-003",), complexity="high", risk="critical",
            ),
        })
        result = self._service(pinned).reconcile("/repo")
        self.assertEqual(result.kind, execplan_catalog_state.ReconciliationKind.RECONCILED)
        self.assertEqual(result.changed_plan_ids, ("G-001", "G-002", "G-003", "G-004"))
        rows = self.connection.execute(
            "SELECT plan_id, status, initial_worker_level FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("G-001", "IMPLEMENTING", 1), ("G-002", "MERGED", 1),
            ("G-003", "READY", 1), ("G-004", "READY", 7),
        ])
        self.assertEqual(self._service(pinned).reconcile("/repo").changed_plan_ids, ())

    def test_new_registration_rolls_back_on_revision_drift_or_interruption(self):
        pinned = active_snapshot("c" * 40, {
            "docs/plans/[G-001] Changed.md": self._text("G-001", "Changed"),
            "docs/plans/[G-002] Dependency.md": self._text("G-002", "Dependency"),
            "docs/plans/[G-003] Third.md": self._text("G-003", "Third"),
        })
        result = self._service(
            pinned, revisions=["c" * 40, "c" * 40, "d" * 40]
        ).reconcile("/repo")
        self.assertEqual(result.kind, execplan_catalog_state.ReconciliationKind.STALE)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM exec_plans WHERE plan_id = 'G-003'"
        ).fetchone())
        self.assertEqual(self.repository.current_definition("/repo", "G-001").title, "Old")

        def interrupt(stage):
            if stage == "after-plan:G-003":
                raise RuntimeError("interrupted")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self._service(pinned, fault_hook=interrupt).reconcile("/repo")
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM exec_plans WHERE plan_id = 'G-003'"
        ).fetchone())
        self.assertEqual(self.repository.current_definition("/repo", "G-001").title, "Old")
        self.assertEqual(
            self._service(pinned).reconcile("/repo").kind,
            execplan_catalog_state.ReconciliationKind.RECONCILED,
        )

    def test_intent_and_unsafe_new_ids_do_not_partially_register(self):
        documents = {
            "docs/plans/[G-001] Old.md": self._text("G-001", "Old"),
            "docs/plans/[G-002] Dependency.md": self._text("G-002", "Dependency"),
        }
        self.connection.execute(
            "INSERT INTO general_planning_publications "
            "(repo, stage, base_sha, commit_sha, snapshot_token, "
            "specification_fingerprint, planning_fingerprint, manifest, manifest_hash) "
            "VALUES ('/repo', 'prepared', 'a', 'b', 'token', 'spec', 'plan', '{}', 'hash')"
        )
        self.connection.commit()
        candidate = active_snapshot("e" * 40, {
            **documents,
            "docs/plans/[G-003] Third.md": self._text("G-003", "Third"),
        })
        self.assertEqual(
            self._service(candidate).reconcile("/repo").kind,
            execplan_catalog_state.ReconciliationKind.STALE,
        )
        self.connection.execute("DELETE FROM general_planning_publications")
        self.connection.commit()
        maintenance_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO maintenance_cycles (repo, publication_stage) "
            "VALUES ('/repo', 'prepared')"
        )
        self.connection.commit()
        self.assertEqual(
            self._service(candidate).reconcile("/repo").kind,
            execplan_catalog_state.ReconciliationKind.STALE,
        )
        self.connection.execute("DELETE FROM maintenance_cycles")
        self.connection.commit()

        cases = (
            ({"docs/plans/[G-004] Gap.md": self._text("G-004", "Gap")}, "unknown-plan-id"),
            ({"docs/plans/archive/[G-003] Archive.md": self._text("G-003", "Archive")}, "unknown-plan-id"),
            ({"docs/plans/[G-003] Legacy.md": self._text(
                "G-003", "Legacy"
            ).replace("priority: normal\n", "")}, "invalid-plan"),
        )
        for extra, code in cases:
            with self.subTest(extra=extra):
                result = self._service(active_snapshot("e" * 40, {
                    **documents, **extra,
                })).reconcile("/repo")
                self.assertEqual(result.diagnostic.code, code)
                self.assertEqual(self.connection.execute(
                    "SELECT COUNT(*) FROM exec_plans WHERE repo = '/repo'"
                ).fetchone()[0], 2)

        missing = self._service(active_snapshot("e" * 40, {
            "docs/plans/[G-001] Old.md": documents["docs/plans/[G-001] Old.md"],
            "docs/plans/[G-003] Third.md": self._text("G-003", "Third"),
        })).reconcile("/repo")
        self.assertEqual(missing.diagnostic.code, "ambiguous-id-change")

        archived_source = active_snapshot("e" * 40, {
            **documents,
            "docs/plans/[G-003] Third.md": self._text(
                "G-003", "Third", sources=("docs/requirements/Archived.md",),
            ),
        })
        missing_source = execplan_catalog_state.PinnedCatalog(
            archived_source.revision_sha, archived_source.snapshot,
            frozenset({"docs/PRODUCT.md"}),
        )
        self.assertEqual(
            self._service(missing_source).reconcile("/repo").diagnostic.code,
            "missing-source",
        )
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM exec_plans WHERE plan_id = 'G-003'"
        ).fetchone())

    def test_cancelled_number_and_sqlite_ownership_race_reject_registration(self):
        documents = {
            "docs/plans/[G-001] Old.md": self._text("G-001", "Old"),
            "docs/plans/[G-002] Dependency.md": self._text("G-002", "Dependency"),
            "docs/plans/[G-003] Third.md": self._text("G-003", "Third"),
        }
        pinned = active_snapshot("7" * 40, documents)
        self.connection.execute(
            "INSERT INTO execplan_cancellations "
            "(repo, plan_id, path, title, sources, depends_on, complexity, risk, "
            "priority, content_hash, plan_sha, deletion_commit) VALUES "
            "('/repo', 'G-003', 'docs/plans/[G-003] Third.md', 'Third', "
            "'[\"docs/PRODUCT.md\"]', '[]', 'low', 'low', 'normal', 'hash', 'sha', 'sha')"
        )
        self.connection.commit()
        self.assertEqual(
            self._service(pinned).reconcile("/repo").diagnostic.code,
            "unknown-plan-id",
        )
        self.connection.execute("DELETE FROM execplan_cancellations")
        self.connection.commit()

        connection = self.connection

        class MutatingPort(FakeDefinitionPort):
            def current_revision(self):
                connection.execute(
                    "UPDATE exec_plans SET worker_session_id = 'changed-owner' "
                    "WHERE plan_id = 'G-002'"
                )
                connection.commit()
                return self.pinned.revision_sha

        result = execplan_catalog_state.CatalogReconciliationService(
            self.repository, MutatingPort(pinned)
        ).reconcile("/repo")
        self.assertEqual(result.kind, execplan_catalog_state.ReconciliationKind.STALE)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM exec_plans WHERE plan_id = 'G-003'"
        ).fetchone())
        self.assertEqual(
            self._service(pinned).reconcile("/repo").kind,
            execplan_catalog_state.ReconciliationKind.RECONCILED,
        )

    def test_orphaned_execution_history_and_path_collision_reserve_identity(self):
        documents = {
            "docs/plans/[G-001] Old.md": self._text("G-001", "Old"),
            "docs/plans/[G-002] Dependency.md": self._text("G-002", "Dependency"),
            "docs/plans/[G-003] Third.md": self._text("G-003", "Third"),
        }
        pinned = active_snapshot("8" * 40, documents)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS execution_checkpoints (repo TEXT, plan_id TEXT)"
        )
        self.connection.execute(
            "INSERT INTO execution_checkpoints (repo, plan_id) "
            "VALUES ('/repo', 'G-003')"
        )
        self.connection.commit()
        self.assertEqual(
            self._service(pinned).reconcile("/repo").diagnostic.code,
            "unknown-plan-id",
        )
        self.connection.execute("DELETE FROM execution_checkpoints WHERE plan_id = 'G-003'")
        self.connection.execute(
            "UPDATE exec_plans SET path = 'docs/plans/[G-003] Third.md' "
            "WHERE plan_id = 'G-001'"
        )
        self.connection.commit()
        self.assertEqual(
            self._service(pinned).reconcile("/repo").diagnostic.code,
            "unknown-plan-id",
        )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM exec_plans WHERE repo = '/repo'"
        ).fetchone()[0], 2)

    def test_legacy_backfill_rejects_replacement_before_any_history_mutation(self):
        replacement = active_snapshot(
            "f" * 40,
            {
                "docs/plans/[G-001] Old.md": self._text("G-001", "Old"),
                "docs/plans/[G-003] Replacement.md": self._text(
                    "G-003", "Replacement"
                ),
            },
        ).snapshot

        self.connection.execute(
            """
            INSERT INTO execplan_catalog_history (
                repo, plan_id, original_path, observed_path,
                original_content_hash, final_content_hash
            ) VALUES ('/repo', 'G-001', 'docs/plans/[G-001] Old.md',
                      'docs/plans/archive/[G-001] Old.md',
                      'published-hash', 'archived-hash')
            """
        )
        self.connection.commit()

        with (
            mock.patch.object(
                loop_manager,
                "revision_execplan_snapshot",
                return_value=replacement,
            ),
            self.assertRaisesRegex(
                execplan_catalog.ExecPlanCatalogError,
                "ID 변경",
            ),
        ):
            loop_manager.migrate_legacy_execplans(
                self.connection,
                reconcile_history=True,
                target_repo="/repo",
                ensure_schema=False,
            )

        plans = self.connection.execute(
            "SELECT plan_id, status, content_hash "
            "FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in plans],
            [
                ("G-001", "IMPLEMENTING", "old-hash-G-001"),
                ("G-002", "MERGED", "old-hash-G-002"),
            ],
        )
        history = self.connection.execute(
            "SELECT currently_archived, observed_path, final_content_hash "
            "FROM execplan_catalog_history "
            "WHERE repo = '/repo' AND plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(
            tuple(history),
            (1, "docs/plans/archive/[G-001] Old.md", "archived-hash"),
        )

    def test_execution_catalog_invalid_is_returned_before_final_block_gate(self):
        diagnostic = execplan_catalog_state.ReconciliationDiagnostic(
            revision_sha="f" * 40,
            code="missing-source",
            message="missing source",
            path="docs/plans/[G-001] Old.md",
            source="docs/requirements/Missing.md",
            related_paths=("docs/requirements/Missing.md",),
            fingerprint="fingerprint",
        )
        with mock.patch.object(
            loop_manager._GitCatalogDefinitionPort,
            "load",
            side_effect=execplan_catalog_state.InvalidPinnedCatalog(diagnostic),
        ):
            result = loop_manager.reconcile_execution_execplan_catalog(
                self.connection, Path("/repo"), "/repo", "main"
            )
        self.assertEqual(
            result.kind,
            execplan_catalog_state.ReconciliationKind.INVALID_CATALOG,
        )
        self.assertIsNone(loop_manager.scoped_blocking.project_block(
            self.connection, "/repo"
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()[0], "IMPLEMENTING")

    def test_invalid_catalog_uses_project_scope_and_fixed_input_auto_releases(self):
        diagnostic = execplan_catalog_state.ReconciliationDiagnostic(
            revision_sha="f" * 40,
            code="missing-source",
            message="missing source",
            path="docs/plans/[G-001] Old.md",
            source="docs/requirements/Missing.md",
            related_paths=("docs/requirements/Missing.md",),
            fingerprint="fingerprint",
        )
        with mock.patch.object(
            loop_manager._GitCatalogDefinitionPort,
            "load",
            side_effect=execplan_catalog_state.InvalidPinnedCatalog(diagnostic),
        ):
            result = loop_manager.reconcile_execplan_catalog(
                self.connection, Path("/repo"), "/repo", "main"
            )
        block = loop_manager.scoped_blocking.project_block(
            self.connection, "/repo"
        )
        runtime = self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(
            result.kind,
            execplan_catalog_state.ReconciliationKind.INVALID_CATALOG,
        )
        self.assertEqual(
            block["reason_code"],
            loop_manager.BlockReasonCode.EXECPLAN_CATALOG_INVALID.value,
        )
        self.assertEqual(runtime["status"], "IMPLEMENTING")

        pinned = active_snapshot(
            "1" * 40,
            {
                "docs/plans/[G-001] Old.md": self._text("G-001", "Old"),
                "docs/plans/[G-002] Dependency.md": self._text(
                    "G-002", "Dependency"
                ),
            },
        )
        with (
            mock.patch.object(
                loop_manager._GitCatalogDefinitionPort,
                "load",
                return_value=pinned,
            ),
            mock.patch.object(
                loop_manager._GitCatalogDefinitionPort,
                "current_revision",
                return_value="1" * 40,
            ),
        ):
            fixed = loop_manager.reconcile_execplan_catalog(
                self.connection, Path("/repo"), "/repo", "main"
            )
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = '/repo'"
        ).fetchone()
        self.assertEqual(
            fixed.kind, execplan_catalog_state.ReconciliationKind.RECONCILED
        )
        self.assertIsNone(
            loop_manager.scoped_blocking.project_block(
                self.connection, "/repo"
            )
        )
        self.assertEqual(project["phase"], "WORKING")


if __name__ == "__main__":
    unittest.main()
