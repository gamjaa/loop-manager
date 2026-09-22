import sqlite3
import unittest

import blocked_checkpoint
import blocked_checkpoint_state
import loop_manager
import runtime_state


class BlockedCheckpointRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, branch, worktree, workspace_owned
            ) VALUES ('repo', 'G-034', 'docs/plans/[G-034] Checkpoint.md',
                      'Checkpoint', '["docs/PRODUCT.md"]', '[]', 'critical',
                      'hash', 'PLAN_BLOCKED', 'loop-manager/G-034',
                      '/state/G-034', 1)
            """
        )
        self.repository = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        )

    def tearDown(self):
        self.connection.close()

    def request(self, evidence="failure", release_fingerprint=None):
        return self.repository.request(
            repo="repo",
            plan_id="G-034",
            reason_code="verify_retry_exhausted",
            evidence=evidence,
            branch="loop-manager/G-034",
            worktree="/state/G-034",
            expected_remote_oid=None,
            rebase_recovery=False,
            release_fingerprint=release_fingerprint,
        )

    def test_request_and_phase_progress_are_durable_and_idempotent(self):
        request = self.request()
        prepared = blocked_checkpoint.PreparedCheckpoint(
            parent_oid="a" * 40,
            tree_oid="b" * 40,
            commit_identity="identity",
            message="message",
            included_paths=("code.py",),
        )
        self.repository.record_prepared(request, prepared)
        self.repository.record_local(request, "c" * 40)
        observed = self.repository.record_remote_observation(request, None)
        self.assertTrue(observed.remote_observed)
        self.repository.finish_unpublished(
            observed, "network unavailable", retryable=True
        )

        resumed = self.repository.pending("repo")
        self.assertEqual(len(resumed), 1)
        self.assertIs(resumed[0].phase, blocked_checkpoint.CheckpointPhase.RETRY_PENDING)
        self.assertEqual(resumed[0].local_oid, "c" * 40)
        same = self.request()
        self.assertIs(same.phase, blocked_checkpoint.CheckpointPhase.RETRY_PENDING)
        changed = self.request(evidence="new failure")
        self.assertIs(changed.phase, blocked_checkpoint.CheckpointPhase.REQUESTED)
        self.assertNotEqual(changed.request_identity, same.request_identity)
        changed_fingerprint = self.request(
            evidence="new failure", release_fingerprint="new-input"
        )
        self.assertNotEqual(
            changed_fingerprint.request_identity,
            changed.request_identity,
        )

    def test_published_result_records_remote_oid_on_execplan_without_unblocking(self):
        request = self.request()
        oid = "d" * 40
        self.repository.finish_published(request, oid)

        row = self.connection.execute(
            "SELECT status, publication_remote_oid FROM exec_plans"
        ).fetchone()
        self.assertEqual(tuple(row), ("PLAN_BLOCKED", oid))
        self.assertEqual(self.repository.pending("repo"), ())
        checkpoint = self.connection.execute(
            "SELECT phase, result, remote_oid FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(checkpoint), ("published", "published", oid))

    def test_plan_block_and_checkpoint_request_share_one_transaction(self):
        self.connection.execute(
            "DELETE FROM blocked_checkpoints WHERE repo = 'repo'"
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE repo = 'repo'"
        )
        self.connection.execute(
            "UPDATE projects SET phase = 'WORKING', plan_path = ? "
            "WHERE repo = 'repo'",
            ("docs/plans/[G-034] Checkpoint.md",),
        )
        self.connection.commit()

        def fail(stage):
            if stage == "after_project_update":
                raise RuntimeError(stage)

        repository = runtime_state.RuntimeStateRepository(
            self.connection, fault_hook=fail
        )
        with self.assertRaisesRegex(RuntimeError, "after_project_update"):
            repository.block_plan(
                "repo",
                "G-034",
                reason_code="review_retry_exhausted",
                evidence="review failure",
                release_fingerprint="fingerprint",
                clear_projection=True,
            )
        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(plan["status"], "IMPLEMENTING")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM execplan_blocks WHERE repo = 'repo'"
            ).fetchone()
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM blocked_checkpoints WHERE repo = 'repo'"
            ).fetchone()
        )

        with self.connection:
            runtime_state.RuntimeStateRepository(self.connection).block_plan(
                "repo",
                "G-034",
                reason_code="review_retry_exhausted",
                evidence="review failure",
                release_fingerprint="fingerprint",
                clear_projection=True,
            )
        stored = self.connection.execute(
            "SELECT p.status, b.reason_code, c.phase "
            "FROM exec_plans AS p JOIN execplan_blocks AS b USING (repo, plan_id) "
            "JOIN blocked_checkpoints AS c USING (repo, plan_id)"
        ).fetchone()
        self.assertEqual(
            tuple(stored),
            ("PLAN_BLOCKED", "review_retry_exhausted", "requested"),
        )
