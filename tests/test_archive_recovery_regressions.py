"""Unit regressions for archive quarantine and revised publication recovery."""

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
import loop_manager
from loop_manager_internal.storage import runtime_state
from tests.support.catalog import archived_entry


class ArchiveRecoveryRegressionTest(unittest.TestCase):
    def test_setup_registers_connection_cleanup_before_schema_initialization(self):
        fixture = type(self)("test_audit_proof_survives_repeated_tampering_and_database_reconnect")
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError("schema setup failed")

        with mock.patch.object(sqlite3, "connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "schema setup failed"):
                fixture.setUp()
        fixture.doCleanups()
        connection.close.assert_called_once_with()

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')"
        )
        self.entry = archived_entry()
        self.active = replace(self.entry, path=self.entry.active_path)

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

    def test_actual_pr_merge_outranks_unavailable_local_publication(self):
        identity = execplan_catalog_state.StoredPlanIdentity(
            "G-001", self.entry.active_path, self.entry.content_hash,
            "c" * 40, "MERGED",
        )
        with (
            mock.patch("loop_manager.pr_for_branch", return_value={
                'state': 'MERGED', 'mergeCommit': {'oid': 'd' * 40},
                'url': 'https://example.test/pr/1',
            }) as pr_for_branch,
            mock.patch("loop_manager.git", side_effect=RuntimeError("git unavailable")),
        ):
            proof = loop_manager._LegacyCatalogMergeProof(
                Path("/repo"), "origin/main", {"G-001": identity}
            ).proof_for(self.entry)

        pr_for_branch.assert_called_once_with(Path('/repo'), 'loop-manager/G-001')
        self.assertTrue(proof.merged)
        self.assertEqual(proof.kind, execplan_catalog_state.MergeProofKind.GITHUB_PR)
        self.assertEqual(proof.plan_sha, 'c' * 40)
        self.assertEqual(proof.revision, 'd' * 40)

    def test_pr_fallback_does_not_turn_nonmerge_or_lookup_failure_into_completion(self):
        identity = execplan_catalog_state.StoredPlanIdentity(
            'G-001', self.entry.active_path, self.entry.content_hash, 'c' * 40, 'MERGED',
        )
        for observed in ({'state': 'OPEN'}, {'state': 'CLOSED'}, None, RuntimeError('lookup failed')):
            with self.subTest(observed=observed), \
                 mock.patch('loop_manager.git', side_effect=RuntimeError('git unavailable')), \
                 mock.patch('loop_manager.pr_for_branch',
                            **({'side_effect': observed} if isinstance(observed, Exception)
                               else {'return_value': observed})):
                port = loop_manager._LegacyCatalogMergeProof(
                    Path('/repo'), 'origin/main', {'G-001': identity},
                )
                if isinstance(observed, Exception):
                    with self.assertRaisesRegex(RuntimeError, 'lookup failed'):
                        port.proof_for(self.entry)
                else:
                    self.assertFalse(port.proof_for(self.entry).merged)

    def seed_history(self, original, final):
        execplan_catalog_state.CatalogStateRepository(self.connection).record(
            "/repo", final, original_path=original.active_path,
            original_content_hash=original.content_hash, plan_sha="a" * 40,
            proof=execplan_catalog_state.MergeProof(
                True, kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
                plan_sha="a" * 40,
                publication_content_hash=original.content_hash,
                revision="b" * 40,
            ), diagnostic=None, identity_verified=True,
        )
        self.connection.commit()

    def test_verified_archive_recovers_stale_status_and_legacy_sha_on_restart(self):
        for proof_kind in (execplan_catalog_state.MergeProofKind.ARCHIVE_DOCUMENT,
                           execplan_catalog_state.MergeProofKind.GITHUB_PR):
            for status in ('READY', 'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PLAN_BLOCKED', 'MERGED'):
                with self.subTest(status=status, proof_kind=proof_kind):
                    self.connection.execute('DELETE FROM exec_plans')
                    self.connection.execute('DELETE FROM execplan_catalog_history')
                    self.insert_runtime(status)
                    self.connection.execute(
                        "UPDATE exec_plans SET publication_sha=?, publication_path=?, "
                        "publication_content_hash=? WHERE plan_id='G-001'",
                        ('c' * 40, self.entry.active_path, self.entry.content_hash),
                    )
                    history = execplan_catalog_state.CatalogStateRepository(self.connection)
                    history.record(
                        '/repo', self.entry, original_path=self.entry.active_path,
                        original_content_hash=self.entry.content_hash, plan_sha='a' * 40,
                        proof=execplan_catalog_state.MergeProof(
                            True, kind=proof_kind,
                            revision='b' * 40,
                        ), diagnostic=None, identity_verified=True,
                    )
                    self.connection.execute(
                        "UPDATE execplan_catalog_history SET diagnostic_code='stable-identity-mismatch'"
                    )
                    self.connection.commit()
                    with mock.patch('loop_manager._LegacyCatalogMergeProof') as external:
                        for _restart in range(2):
                            self.migrate(archived=True)
                            self.assertEqual(self.row()['status'], 'MERGED')
                            self.assertEqual(self.row()['publication_sha'], 'c' * 40)
                            self.assertEqual(self.row()['worker_session_id'], 'worker-session')
                            self.assertEqual(self.row()['retry_count'], 3)
                            audit = history.historical_row('/repo', 'G-001')
                            self.assertIsNone(audit['diagnostic_code'])
                            self.assertEqual(audit['plan_sha'], 'a' * 40)
                            self.assertEqual(audit['proof_revision'], 'b' * 40)
                        external.return_value.proof_for.assert_not_called()
                        external.return_value.verify_stored.assert_not_called()

    def test_verified_archive_does_not_accept_changed_bytes_or_move_identity(self):
        self.insert_runtime('MERGED')
        history = execplan_catalog_state.CatalogStateRepository(self.connection)
        history.record(
            '/repo', self.entry, original_path=self.entry.active_path,
            original_content_hash=self.entry.content_hash, plan_sha='a' * 40,
            proof=execplan_catalog_state.MergeProof(
                True, kind=execplan_catalog_state.MergeProofKind.ARCHIVE_DOCUMENT,
                revision='b' * 40,
            ), diagnostic=None, identity_verified=True,
        )
        stored = {'G-001': execplan_catalog_state.publication_identity(self.row())}
        for entry in (replace(self.entry, content_hash='tampered'),
                      replace(self.entry, path='docs/plans/archive/[G-001] Other.md')):
            with self.subTest(entry=entry.path):
                snapshot = execplan_catalog.ExecPlanCatalogSnapshot(active={}, archived={'G-001': entry})
                external = mock.Mock()
                external.proof_for.return_value = execplan_catalog_state.MergeProof(False)
                result = execplan_catalog_state.CatalogRecoveryService(history, external).reconcile(
                    '/repo', snapshot, stored,
                )
                self.assertEqual(result.merged, ())
                self.assertTrue(result.diagnostics)
                self.assertEqual(self.row()['status'], 'MERGED')

    def recover_stored_history(self, observed, stored=None):
        snapshot = execplan_catalog.ExecPlanCatalogSnapshot(
            active={}, archived={observed.plan_id: observed},
        )
        return execplan_catalog_state.CatalogRecoveryService(
            execplan_catalog_state.CatalogStateRepository(self.connection),
            loop_manager._LegacyCatalogMergeProof(
                Path("/repo"), "origin/main", stored or {},
            ),
        ).reconcile("/repo", snapshot, stored or {})

    def test_audit_proof_survives_repeated_tampering_and_database_reconnect(self):
        original = self.entry
        revised = archived_entry(text_suffix=" synchronized revision")
        tampered = archived_entry(text_suffix=" tampered")
        self.seed_history(original, revised)
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "history.db")

            def reconnect():
                connection = sqlite3.connect(database)
                try:
                    connection.row_factory = sqlite3.Row
                except BaseException:
                    connection.close()
                    raise
                return connection

            disk = sqlite3.connect(database)
            try:
                self.connection.backup(disk)
            finally:
                disk.close()
            self.connection.close()
            self.connection = reconnect()
            self.addCleanup(self.connection.close)
            try:
                with (
                    mock.patch("loop_manager.pr_for_branch", side_effect=AssertionError("PR query")) as pr,
                    mock.patch("loop_manager.git", return_value=subprocess.CompletedProcess(
                        [], 0, "c" * 40, "",
                    )),
                    mock.patch("loop_manager.git_bytes", return_value=original.text.encode()),
                    mock.patch("loop_manager._git_commit_is_ancestor", return_value=True),
                ):
                    for observed in (tampered, original, tampered):
                        result = self.recover_stored_history(observed)
                        self.assertEqual(result.merged, ())
                        self.assertEqual(result.diagnostics[0].code, "archived-content-hash-mismatch")
                        self.connection.commit()
                        self.connection.close()
                        self.connection = reconnect()
                        self.addCleanup(self.connection.close)
                        audit = execplan_catalog_state.CatalogStateRepository(self.connection).historical_row(
                            "/repo", "G-001",
                        )
                        self.assertEqual(audit["merge_confirmed"], 1)
                        self.assertEqual(audit["final_content_hash"], revised.content_hash)
                        self.assertEqual(audit["proof_revision"], "b" * 40)
                    repaired = self.recover_stored_history(revised)
                    self.assertEqual(repaired.diagnostics, ())
                    self.assertEqual(len(repaired.merged), 1)
                    pr.assert_not_called()
            finally:
                self.connection.close()

    def test_renamed_plan_repeated_recovery_keeps_publication_identity(self):
        original = self.entry
        renamed = archived_entry(title="Renamed")
        identity = execplan_catalog_state.StoredPlanIdentity(
            "G-001", original.active_path, original.content_hash, "a" * 40,
            "MERGED", renamed.content_hash,
        )
        with (
            mock.patch("loop_manager.pr_for_branch", side_effect=AssertionError("PR query")) as pr,
            mock.patch("loop_manager.git", return_value=subprocess.CompletedProcess(
                [], 0, "c" * 40, "",
            )),
            mock.patch("loop_manager.git_bytes", return_value=original.text.encode()) as blob,
            mock.patch("loop_manager._git_commit_is_ancestor", return_value=True),
        ):
            for stored in ({"G-001": identity}, {"G-001": identity}, {}):
                result = self.recover_stored_history(renamed, stored)
                self.assertEqual(result.diagnostics, ())
                self.assertEqual(len(result.merged), 1)
                self.assertEqual(result.merged[0].original_path, original.active_path)
            self.assertEqual(blob.call_count, 3)
            for call in blob.call_args_list:
                self.assertEqual(call.args, (Path("/repo"), "show", "a" * 40 + ":" + original.active_path))
            pr.assert_not_called()

    def test_git_only_history_still_requires_publication_blob_and_git_ancestry(self):
        self.seed_history(self.entry, self.entry)
        self.connection.execute(
            "UPDATE execplan_catalog_history SET proof_kind='stored-and-git'"
        )
        with (
            mock.patch("loop_manager.pr_for_branch", side_effect=AssertionError("PR query")) as pr,
            mock.patch("loop_manager.git", return_value=subprocess.CompletedProcess(
                [], 0, "c" * 40, "",
            )) as git,
            mock.patch("loop_manager.git_bytes") as blob,
            mock.patch("loop_manager._git_commit_is_ancestor") as ancestor,
        ):
            for published, is_ancestor in ((self.entry.text.encode(), False), (b"wrong blob", True)):
                with self.subTest(ancestor=is_ancestor):
                    blob.return_value = published
                    ancestor.return_value = is_ancestor
                    for _ in range(2):
                        result = self.recover_stored_history(self.entry)
                        self.assertEqual(result.merged, ())
                        self.assertEqual(result.diagnostics[0].code, "merge-proof-missing")
            self.assertEqual(git.call_count, 4)
            self.assertEqual(blob.call_count, 4)
            self.assertEqual(ancestor.call_count, 4)
            pr.assert_not_called()

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
