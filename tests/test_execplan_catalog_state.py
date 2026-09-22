import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import execplan_catalog
import execplan_catalog_state
import maintenance_state
import loop_manager


def archived_entry(
    plan_id: str = "G-001",
    title: str = "Archived",
    *,
    text_suffix: str = "",
) -> execplan_catalog.ExecPlanCatalogEntry:
    text = f"""---
id: {plan_id}
title: {title}
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

Archived fixture.{text_suffix}
"""
    return execplan_catalog.snapshot_from_documents(
        {f"docs/plans/archive/[{plan_id}] {title}.md": text}
    ).archived[plan_id]


class FakeMergeProof:
    def __init__(self, proof: execplan_catalog_state.MergeProof):
        self.proof = proof
        self.calls = []

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

    def test_recovers_archived_plan_only_with_merge_and_publication_proof(self):
        entry = archived_entry()
        proof = execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
            plan_sha="a" * 40,
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

    def test_hash_mismatch_is_diagnostic_and_skips_external_merge_lookup(self):
        entry = archived_entry(text_suffix=" changed")
        stored = {
            "G-001": execplan_catalog_state.StoredPlanIdentity(
                plan_id="G-001",
                path=entry.active_path,
                content_hash=hashlib.sha256(b"original").hexdigest(),
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
            ),
            stored,
        )

        self.assertEqual(port.calls, [])
        self.assertEqual(result.merged, ())
        self.assertEqual(
            result.diagnostics[0].code, "archived-content-hash-mismatch"
        )
        row = self.repository.historical_row("/repo", "G-001")
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
            ),
        )

        self.assertEqual(port.calls, [])
        self.assertEqual(
            second.diagnostics[0].code, "archived-content-hash-mismatch"
        )
        row = self.repository.historical_row("/repo", "G-001")
        self.assertEqual(row["original_content_hash"], original.content_hash)
        self.assertEqual(row["final_content_hash"], changed.content_hash)


class ArchivedDependencySelectionTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
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


if __name__ == "__main__":
    unittest.main()
