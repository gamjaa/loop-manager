import sqlite3
import unittest

import execplan_archive_state


class ExecPlanArchiveStateTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE exec_plans (
                repo TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
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
            "INSERT INTO exec_plans VALUES (?, ?, ?, ?, ?)",
            ("/repo", plan_id, active_path, f"hash-{plan_id}", status),
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


if __name__ == "__main__":
    unittest.main()
