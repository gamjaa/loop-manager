import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

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


class FakeDefinitionPort:
    def __init__(self, pinned, revisions=None):
        self.pinned = pinned
        self.revisions = list(revisions or [pinned.revision_sha] * 8)

    def load(self):
        return self.pinned

    def current_revision(self):
        return self.revisions.pop(0)


def active_snapshot(revision, documents):
    snapshot = execplan_catalog.snapshot_from_documents(
        documents, revision_sha=revision
    )
    return execplan_catalog_state.PinnedCatalog(revision, snapshot)


class CatalogReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        loop_manager.notifications.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, phase, plan_path, "
            "branch, worktree, worker_session_id, pr_url) "
            "VALUES ('/repo', 'main', 'WORKING', ?, 'branch', '/worktree', "
            "'project-worker', 'https://example/pr/1')",
            ("docs/plans/[G-001] Old.md",),
        )
        self._insert(
            "G-001", "Old", status="IMPLEMENTING", effective=8,
            branch="branch", worktree="/worktree", workspace_owned=1,
            worker_session="worker", reviewer_session="reviewer",
            pr_url="https://example/pr/1", retry_count=4,
        )
        self._insert("G-002", "Dependency", status="MERGED")
        self.connection.commit()
        self.repository = execplan_catalog_state.DefinitionStateRepository(
            self.connection
        )

    def tearDown(self):
        self.connection.close()

    def _insert(
        self,
        plan_id,
        title,
        *,
        status,
        effective=1,
        branch=None,
        worktree=None,
        workspace_owned=0,
        worker_session=None,
        reviewer_session=None,
        pr_url=None,
        retry_count=0,
    ):
        path = f"docs/plans/[{plan_id}] {title}.md"
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, current_complexity, current_risk,
                current_revision_sha, publication_path,
                publication_content_hash, publication_sha, status, plan_sha,
                initial_worker_level, effective_worker_level, retry_count,
                branch, worktree, workspace_owned, worker_session_id,
                reviewer_session_id, pr_url
            ) VALUES ('/repo', ?, ?, ?, '["docs/PRODUCT.md"]', '[]',
                      'normal', ?, 'low', 'low', ?, ?, ?, ?, ?, ?, 1, ?,
                      ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                path,
                title,
                f"old-hash-{plan_id}",
                "a" * 40,
                path,
                f"old-hash-{plan_id}",
                "a" * 40,
                status,
                "a" * 40,
                effective,
                retry_count,
                branch,
                worktree,
                workspace_owned,
                worker_session,
                reviewer_session,
                pr_url,
            ),
        )

    @staticmethod
    def _text(
        plan_id,
        title,
        *,
        sources=("docs/PRODUCT.md",),
        depends_on=(),
        complexity="low",
        risk="low",
        priority="normal",
        body="Body.",
    ):
        source_lines = "\n".join(f"  - {source}" for source in sources)
        dependencies = "depends_on: []"
        if depends_on:
            dependencies = "depends_on:\n" + "\n".join(
                f"  - {dependency}" for dependency in depends_on
            )
        return f"""---
id: {plan_id}
title: {title}
sources:
{source_lines}
{dependencies}
complexity: {complexity}
risk: {risk}
priority: {priority}
---

## Purpose

{body}
"""

    def _service(self, pinned, *, fault_hook=None, revisions=None):
        repository = execplan_catalog_state.DefinitionStateRepository(
            self.connection, fault_hook=fault_hook
        )
        return execplan_catalog_state.CatalogReconciliationService(
            repository,
            FakeDefinitionPort(pinned, revisions),
        )

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

    def test_missing_or_unknown_id_is_invalid_without_row_changes(self):
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
        self.assertEqual(result.diagnostic.code, "unknown-plan-id")
        self.assertEqual(result.diagnostic.path, "docs/plans/[G-003] Unknown.md")

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
