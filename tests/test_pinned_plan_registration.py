import sqlite3
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.catalog import execplan_catalog


class PinnedPublishedPlanRegistrationTest(unittest.TestCase):
    def test_registration_uses_pinned_tree_when_checkout_is_absent(self):
        sha = "a" * 40
        path = "docs/plans/[G-001] Remote.md"
        text = (
            "---\nid: G-001\ntitle: Remote\nsources:\n"
            "  - docs/PRODUCT.md\ndepends_on: []\ncomplexity: high\n"
            "risk: critical\npriority: critical\n---\n\n## Purpose\n\nRemote.\n"
        )
        snapshot = execplan_catalog.snapshot_from_documents(
            {path: text}, revision_sha=sha,
        )
        tree = (
            b"100644 blob " + b"1" * 40 + b"\tdocs/PRODUCT.md\0"
            + b"100644 blob " + b"2" * 40 + b"\t" + path.encode() + b"\0"
        )
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/missing-checkout', 'main')"
        )
        port = loop_manager._GitCatalogDefinitionPort(
            Path("/missing-checkout"), "refs/remotes/origin/main",
            lambda: frozenset(), pinned_revision=sha,
        )
        with (
            mock.patch.object(loop_manager, "revision_execplan_snapshot", return_value=snapshot),
            mock.patch.object(loop_manager, "git_bytes", return_value=tree),
            mock.patch.object(loop_manager, "git", return_value=mock.Mock(stdout=sha)),
        ):
            result = loop_manager.execplan_catalog_state.CatalogReconciliationService(
                loop_manager.execplan_catalog_state.DefinitionStateRepository(connection),
                port,
            ).reconcile("/missing-checkout")
        self.assertEqual(
            result.kind, loop_manager.execplan_catalog_state.ReconciliationKind.RECONCILED
        )
        row = connection.execute(
            "SELECT content_hash, publication_sha, initial_worker_level, status "
            "FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(row["content_hash"], execplan_catalog.plan_content_hash(text))
        self.assertEqual(row["publication_sha"], sha)
        self.assertEqual(row["initial_worker_level"], 7)
        self.assertEqual(row["status"], "READY")
