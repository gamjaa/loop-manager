from tests.support.fixed_samples import use_fixed_guidance
import sqlite3
import unittest

from loop_manager_internal.storage import execplan_archive_state, planning_input_state


class ExecPlanArchiveStateTest(unittest.TestCase):
    def setUp(self):
        use_fixed_guidance(self)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE exec_plans (
                repo TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                current_revision_sha TEXT,
                publication_path TEXT,
                publication_content_hash TEXT,
                publication_sha TEXT,
                status TEXT NOT NULL,
                PRIMARY KEY (repo, plan_id)
            )
            """
        )
        self.repository = execplan_archive_state.ExecPlanArchiveRepository(
            self.connection
        )

    def tearDown(self):
        self.connection.close()

    def _insert(self, plan_id, title, status="MERGED", *, path=None):
        active_path = path or f"docs/plans/[{plan_id}] {title}.md"
        self.connection.execute(
            "INSERT INTO exec_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("/repo", plan_id, active_path, f"hash-{plan_id}", "base",
             active_path, f"initial-{plan_id}", "published", status),
        )

    def test_candidates_are_only_merged_rows_with_stable_active_paths(self):
        self._insert("G-001", "Merged")
        for number, status in enumerate(
            ("PR_OPEN", "PLAN_BLOCKED", "REVIEWING", "VERIFYING", "IMPLEMENTING"),
            start=2,
        ):
            self._insert(f"G-{number:03d}", status.title(), status)
        self._insert(
            "G-010",
            "Archived",
            path="docs/plans/archive/[G-010] Archived.md",
        )
        self._insert(
            "G-011",
            "Nested",
            path="docs/plans/nested/[G-011] Nested.md",
        )

        candidates = self.repository.merged_candidates("/repo")

        self.assertEqual(tuple(item.plan_id for item in candidates), ("G-001",))
        self.assertEqual(
            candidates[0].archive_path,
            "docs/plans/archive/[G-001] Merged.md",
        )

    def test_execution_observation_survives_reopen_without_candidate_records(self):
        import tempfile
        from pathlib import Path
        state = dict(category="shared_resource", resource="refs/heads/main",
                     reason="busy", resume_condition="release_base_writer")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with sqlite3.connect(path) as connection:
                connection.row_factory = sqlite3.Row
                repository = execplan_archive_state.ExecPlanArchiveRepository(connection)
                self.assertTrue(repository.observe("/repo", "dispatch", **state))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM execplan_archive_items").fetchone()[0], 0)
            with sqlite3.connect(path) as connection:
                connection.row_factory = sqlite3.Row
                repository = execplan_archive_state.ExecPlanArchiveRepository(connection)
                self.assertFalse(repository.observe("/repo", "dispatch", **state))
                self.assertFalse(repository.observe("/repo", "dispatch", **state, attempted=True))
                row = connection.execute("SELECT * FROM execplan_archive_observations").fetchone()
                self.assertEqual(row["attempts"], 1)
                self.assertEqual(row["resume_condition"], "release_base_writer")
                self.assertTrue(repository.observe("/repo", "dispatch", **{**state, "reason": "writer_unknown"}))

    def test_observation_does_not_change_prepared_operation_or_candidate(self):
        self._insert("G-001", "Merged")
        item = self.repository.merged_candidates("/repo")[0]
        self.repository.record_prepared("/repo", "operation", "base", "tree", "commit", (item,))
        before = tuple(self.repository.item("/repo", item.plan_id))
        saved = self.repository.prepared_operation("/repo", "operation")
        state = dict(resource=item.plan_id, reason="busy", resume_condition="release_owner")
        self.assertTrue(self.repository.observe("/repo", item.plan_id, category="shared_resource", **state))
        self.assertFalse(self.repository.observe("/repo", item.plan_id, category="shared_resource", **state))
        self.assertEqual(tuple(self.repository.item("/repo", item.plan_id)), before)
        self.assertEqual(self.repository.prepared_operation("/repo", "operation"), saved)

    def test_operation_and_item_state_survive_prepare_pending_and_success(self):
        self._insert("G-001", "Merged")
        candidate = self.repository.merged_candidates("/repo")[0]

        self.repository.record_operation_started(
            "/repo", "operation", "base", (candidate,)
        )
        self.repository.record_prepared(
            "/repo",
            "operation",
            "base",
            "tree",
            "commit",
            (candidate,),
        )

        prepared = self.repository.prepared_operation("/repo", "operation")
        self.assertEqual(prepared.commit_oid, "commit")
        self.assertEqual(prepared.final_hash, "tree")

        self.repository.record_operation_pending(
            "/repo",
            "operation",
            (candidate,),
            reason="push_failed:denied",
            push_result="push_failed",
            base_sha="base",
            commit_oid="commit",
            final_hash="tree",
        )
        pending = self.repository.item("/repo", "G-001")
        self.assertEqual(pending["pending_reason"], "push_failed:denied")
        self.assertEqual(pending["archived"], 0)

        self.repository.record_success(
            "/repo",
            (candidate,),
            base_sha="base",
            operation_id="operation",
            commit_oid="commit",
            push_result="pushed:commit",
        )
        archived = self.repository.item("/repo", "G-001")
        self.assertEqual(archived["pending_reason"], None)
        self.assertEqual(archived["archived"], 1)
        self.assertEqual(archived["final_hash"], "hash-G-001")

    def test_verified_archive_consumes_only_matching_active_declaration(self):
        self._insert("G-001", "Merged")
        item = self.repository.merged_candidates("/repo")[0]
        baseline = {item.active_path: ("blob", item.content_hash),
                    "docs/plans/[G-002] User.md": ("blob", "old")}
        planning_input_state.record_general(self.connection, "/repo", "base", baseline)
        repository = execplan_archive_state.ExecPlanArchiveRepository(
            self.connection, apply_catalog=lambda *_args: True,
        )

        result = repository.record_success(
            "/repo", (item,), base_sha="base", operation_id=None,
            commit_oid="archive-commit", push_result="pushed:archive-commit",
        )

        self.assertEqual(result, execplan_archive_state.ArchiveWriteResult.APPLIED)
        self.assertTrue(planning_input_state.matches(
            self.connection, "/repo", {"docs/plans/[G-002] User.md": ("blob", "old")},
        ))
        self.assertFalse(planning_input_state.matches(
            self.connection, "/repo", {"docs/plans/[G-002] User.md": ("blob", "changed")},
        ))

    def test_catalog_preparation_runs_outside_transaction_and_stale_apply_rolls_back_success(self):
        self._insert("G-001", "Merged")
        self.connection.commit()
        item = self.repository.merged_candidates("/repo")[0]
        calls = []
        def prepare(*_args):
            self.assertFalse(self.connection.in_transaction)
            calls.append("observe")
            def apply():
                self.assertTrue(self.connection.in_transaction)
                calls.append("cas")
                return False
            return apply
        repository = execplan_archive_state.ExecPlanArchiveRepository(self.connection, prepare_catalog=prepare)
        result = repository.record_success(
            "/repo", (item,), base_sha="base", operation_id=None,
            commit_oid="commit", push_result="pushed:commit",
        )
        self.assertEqual(result, execplan_archive_state.ArchiveWriteResult.SUPERSEDED)
        self.assertEqual(calls, ["observe", "cas"])
        self.assertIsNone(repository.item("/repo", item.plan_id))

    def test_revision_change_supersedes_old_candidate_and_preserves_audit(self):
        self._insert("G-001", "Original")
        old = self.repository.merged_candidates("/repo")[0]
        self.repository.record_pending("/repo", old, reason="push_failed", base_sha="base")
        self.connection.execute(
            "UPDATE exec_plans SET path = ?, content_hash = ?, "
            "current_revision_sha = ? WHERE repo = ? AND plan_id = ?",
            ("docs/plans/[G-001] Revised.md", "final-hash", "new-base", "/repo", "G-001"),
        )
        new = self.repository.merged_candidates("/repo")[0]
        self.assertEqual(new.archive_path, "docs/plans/archive/[G-001] Revised.md")
        self.assertEqual(new.content_hash, "final-hash")
        self.assertEqual(new.revision_sha, "new-base")
        self.assertIs(
            self.repository.record_operation_started("/repo", "old-op", "base", (old,)),
            execplan_archive_state.ArchiveWriteResult.SUPERSEDED,
        )
        self.repository.record_pending("/repo", new, reason="source_hash_mismatch")
        item = self.repository.item("/repo", "G-001")
        self.assertEqual(item["expected_hash"], "final-hash")
        self.assertEqual(item["current_revision_sha"], "new-base")
        self.assertIsNone(item["base_sha"])
        audit = self.connection.execute(
            "SELECT publication_path, publication_content_hash, publication_sha "
            "FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(tuple(audit),
                         ("docs/plans/[G-001] Original.md", "initial-G-001", "published"))

    def test_remote_move_after_push_recovers_pending_final_blob(self):
        self._insert("G-001", "Original")
        candidate = self.repository.merged_candidates("/repo")[0]
        self.repository.record_operation_started("/repo", "operation", "base", (candidate,))
        self.connection.execute(
            "UPDATE exec_plans SET path = ?, current_revision_sha = ? "
            "WHERE plan_id = 'G-001'",
            (candidate.archive_path, "archive-commit"),
        )
        self.assertEqual(self.repository.merged_candidates("/repo"), (candidate,))
        self.repository.record_success(
            "/repo", (candidate,), base_sha="archive-commit",
            operation_id=None, commit_oid=None, push_result="observed_remote",
        )
        self.assertEqual(self.repository.merged_candidates("/repo"), ())

    def test_remote_archive_with_wrong_final_blob_keeps_pending_identity(self):
        self._insert("G-001", "Original")
        candidate = self.repository.merged_candidates("/repo")[0]
        self.repository.record_operation_started("/repo", "operation", "base", (candidate,))
        self.connection.execute(
            "UPDATE exec_plans SET path = ?, content_hash = ?, "
            "current_revision_sha = ? WHERE plan_id = 'G-001'",
            (candidate.archive_path, "initial-G-001", "archive-commit"),
        )
        self.assertEqual(self.repository.merged_candidates("/repo"), (candidate,))
        self.repository.record_pending(
            "/repo", candidate, reason="archive_hash_mismatch",
            base_sha="archive-commit",
        )
        self.assertEqual(
            self.repository.item("/repo", "G-001")["pending_reason"],
            "archive_hash_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
