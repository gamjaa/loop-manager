import sqlite3
import unittest
from dataclasses import replace
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
import loop_manager


class BlockedCheckpointDiscoveryUnitTests(unittest.TestCase):
    def test_git_fixture_cases_are_collected_once_across_consuming_modules(self):
        from collections import Counter

        def cases(suite):
            for item in suite:
                if isinstance(item, unittest.TestSuite):
                    yield from cases(item)
                else:
                    yield item.id()

        loader = unittest.TestLoader()
        suites = [loader.discover('tests', pattern=pattern)
                  for pattern in ('test_checkpoint_git_safety.py', 'test_checkpoint_remote_recovery.py', 'test_execution_*.py')]
        counts = Counter(case for suite in suites for case in cases(suite))
        fixture_ids = {case for case in counts
                       if case.startswith(('test_checkpoint_git_safety.', 'test_checkpoint_remote_recovery.'))}
        self.assertTrue(fixture_ids)
        self.assertEqual({counts[case] for case in fixture_ids}, {1})


class BlockedCheckpointPolicyTest(unittest.TestCase):
    def test_project_exception_still_requires_current_scope_and_git_safety(self):
        denied = blocked_checkpoint.assess_safety(self.observation(project_blocked=True))
        self.assertFalse(denied.safe)
        self.assertTrue(blocked_checkpoint.assess_safety(self.observation(
            project_blocked=True, project_recovery_verified=True)).safe)
        for changes in ({'workspace_owned': False}, {'shared_metadata_owned': False},
                        {'operations': ('rebase-merge',)}, {'actual_branch': None},
                        {'legacy_scope_uncertain': True},
                        {'dirty_paths': (blocked_checkpoint.DirtyPath('AGENTS.md', ('staged',), False),)}):
            with self.subTest(changes=changes):
                self.assertFalse(blocked_checkpoint.assess_safety(self.observation(
                    project_blocked=True, project_recovery_verified=True, **changes)).safe)

    def test_diagnostic_refs_are_immutable_and_bound_to_the_artifact(self):
        values = ('repo', 'G-107', 'loop-manager/G-107', 'a' * 40, 'b' * 40, 'c' * 40)
        branch = blocked_checkpoint.diagnostic_branch(*values)
        self.assertEqual(branch, blocked_checkpoint.diagnostic_branch(*values))
        self.assertNotEqual(branch, blocked_checkpoint.diagnostic_branch(*values[:-2], 'd' * 40, values[-1]))
        for remote, mode in ((None, blocked_checkpoint.PushMode.CREATE),
                             ('b' * 40, blocked_checkpoint.PushMode.NONE),
                             ('d' * 40, blocked_checkpoint.PushMode.REJECT)):
            self.assertEqual(blocked_checkpoint.decide_diagnostic_push('b' * 40, remote).mode, mode)

    def test_existing_base_fetch_table_adds_action_token_column(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "CREATE TABLE manager_base_fetches ("
                "id INTEGER PRIMARY KEY, repo TEXT NOT NULL, "
                "plan_id TEXT NOT NULL, refname TEXT NOT NULL, "
                "before_ref TEXT, target_oid TEXT NOT NULL, phase TEXT NOT NULL)"
            )
            loop_manager._ensure_cleanup_pending_schema(connection)
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(manager_base_fetches)"
                )
            }
            self.assertIn("action_token", columns)
        finally:
            connection.close()

    def observation(self, **changes):
        values = {
            "workspace_identity_matches": True,
            "workspace_owned": True,
            "head_oid": "a" * 40,
            "actual_branch": "loop-manager/G-034",
            "expected_branch": "loop-manager/G-034",
            "dirty_paths": (
                blocked_checkpoint.DirtyPath(
                    "code.py", ("unstaged",), True
                ),
            ),
        }
        values.update(changes)
        return blocked_checkpoint.SafetyObservation(**values)

    def test_assessor_accepts_only_owned_worker_paths_and_clean_git_state(self):
        verdict = blocked_checkpoint.assess_safety(self.observation())
        self.assertTrue(verdict.safe)
        self.assertEqual(verdict.included_paths, ("code.py",))

        cases = (
            (
                {"actual_branch": None},
                blocked_checkpoint.SafetyScope.PLAN,
                "detached_head",
            ),
            (
                {"operations": ("rebase-merge",)},
                blocked_checkpoint.SafetyScope.PLAN,
                "git_operation_in_progress",
            ),
            (
                {"agent_scope_recovery_pending": True},
                blocked_checkpoint.SafetyScope.PLAN,
                "agent_scope_recovery_pending",
            ),
            (
                {
                    "dirty_paths": (
                        blocked_checkpoint.DirtyPath(
                            "AGENTS.md", ("staged",), False
                        ),
                    )
                },
                blocked_checkpoint.SafetyScope.PLAN,
                "dirty_scope_violation",
            ),
            (
                {"shared_metadata_owned": False},
                blocked_checkpoint.SafetyScope.PROJECT,
                "shared_ref_mutation",
            ),
        )
        for changes, scope, reason in cases:
            with self.subTest(reason=reason):
                verdict = blocked_checkpoint.assess_safety(
                    self.observation(**changes)
                )
                self.assertFalse(verdict.safe)
                self.assertIs(verdict.scope, scope)
                self.assertEqual(verdict.reason_code, reason)

    def test_identity_message_and_push_policy_are_deterministic_and_narrow(self):
        fields = {
            "plan_id": "G-034",
            "reason_code": "verify_retry_exhausted",
            "evidence": "same failure",
            "parent_oid": "a" * 40,
            "tree_oid": "b" * 40,
            "included_paths": ("z.py", "a.py"),
        }
        identity = blocked_checkpoint.checkpoint_identity(**fields)
        self.assertEqual(identity, blocked_checkpoint.checkpoint_identity(**fields))
        message = blocked_checkpoint.checkpoint_message(
            plan_id=fields["plan_id"],
            reason_code=fields["reason_code"],
            evidence=fields["evidence"],
            identity=identity,
            included_paths=fields["included_paths"],
        )
        self.assertTrue(message.startswith("[G-034] BLOCKED checkpoint"))
        self.assertIn('Evidence: "same failure"', message)
        self.assertLess(message.index("- a.py"), message.index("- z.py"))
        self.assertIn(f"Checkpoint-Identity: {identity}", message)

        create = blocked_checkpoint.decide_push(
            local_oid="b" * 40,
            remote_oid=None,
            expected_remote_oid=None,
            remote_observed=True,
            fast_forward=False,
            rebase_recovery=False,
        )
        self.assertIs(create.mode, blocked_checkpoint.PushMode.CREATE)
        mismatch = blocked_checkpoint.decide_push(
            local_oid="b" * 40,
            remote_oid="c" * 40,
            expected_remote_oid="a" * 40,
            remote_observed=True,
            fast_forward=False,
            rebase_recovery=True,
        )
        self.assertIs(mismatch.mode, blocked_checkpoint.PushMode.REJECT)
        lease = blocked_checkpoint.decide_push(
            local_oid="b" * 40,
            remote_oid="a" * 40,
            expected_remote_oid="a" * 40,
            remote_observed=True,
            fast_forward=False,
            rebase_recovery=True,
        )
        self.assertIs(lease.mode, blocked_checkpoint.PushMode.REBASE_LEASE)

    def test_dispatcher_bounds_transient_push_retries_and_publishes_once(self):
        request = blocked_checkpoint.CheckpointRequest(
            repo="repo",
            plan_id="G-034",
            request_identity="request",
            phase=blocked_checkpoint.CheckpointPhase.REQUESTED,
            reason_code="verify_retry_exhausted",
            evidence="failure",
            branch="loop-manager/G-034",
            worktree="/state/G-034",
        )
        local_oid = "b" * 40
        state = mock.Mock()
        state.pending.return_value = (request,)
        state.record_remote_observation.side_effect = lambda item, remote: replace(
            item,
            expected_remote_oid=None,
            remote_observed=True,
            remote_oid=remote,
        )
        safety = mock.Mock()
        safety.assess.return_value = blocked_checkpoint.SafetyVerdict(
            True,
            blocked_checkpoint.SafetyScope.PLAN,
            None,
            None,
            "a" * 40,
            ("code.py",),
        )
        prepared = blocked_checkpoint.PreparedCheckpoint(
            "a" * 40,
            "c" * 40,
            "identity",
            "message",
            ("code.py",),
        )
        publication = mock.Mock()
        publication.existing_pull_request.return_value = None
        publication.prepare.return_value = prepared
        publication.ensure_local_commit.return_value = local_oid
        publication.observe_remote.side_effect = (None, local_oid)
        publication.push.side_effect = (
            blocked_checkpoint.CheckpointPushError("network", retryable=True),
            blocked_checkpoint.CheckpointPushError("network", retryable=True),
            None,
        )
        results = mock.Mock()

        completed = blocked_checkpoint.BlockedCheckpointDispatcher(
            state, safety, publication, results, max_push_attempts=3
        ).dispatch("repo")

        self.assertEqual(completed, 1)
        self.assertEqual(publication.push.call_count, 3)
        observed_request = replace(request, remote_observed=True, remote_oid=None)
        state.finish_published.assert_called_once_with(
            observed_request, local_oid
        )
        state.finish_unpublished.assert_not_called()
        results.finalized.assert_called_once_with(observed_request)

    def test_dispatcher_does_not_retry_permission_rejection(self):
        request = blocked_checkpoint.CheckpointRequest(
            repo="repo",
            plan_id="G-034",
            request_identity="request",
            phase=blocked_checkpoint.CheckpointPhase.LOCAL_COMMITTED,
            reason_code="review_retry_exhausted",
            evidence="failure",
            branch="loop-manager/G-034",
            worktree="/state/G-034",
        )
        state = mock.Mock()
        state.pending.return_value = (request,)
        state.record_remote_observation.side_effect = lambda item, remote: replace(
            item,
            expected_remote_oid=None,
            remote_observed=True,
            remote_oid=remote,
        )
        safety = mock.Mock()
        safety.assess.return_value = blocked_checkpoint.SafetyVerdict(
            True,
            blocked_checkpoint.SafetyScope.PLAN,
            None,
            None,
            "a" * 40,
            (),
        )
        publication = mock.Mock()
        publication.existing_pull_request.return_value = None
        publication.prepare.return_value = blocked_checkpoint.PreparedCheckpoint(
            "a" * 40, "c" * 40, "identity", "message", ()
        )
        publication.ensure_local_commit.return_value = "b" * 40
        publication.observe_remote.return_value = None
        publication.push.side_effect = blocked_checkpoint.CheckpointPushError(
            "permission denied", retryable=False
        )
        results = mock.Mock()

        blocked_checkpoint.BlockedCheckpointDispatcher(
            state, safety, publication, results, max_push_attempts=3
        ).dispatch("repo")

        publication.push.assert_called_once()
        state.finish_unpublished.assert_called_once_with(
            replace(request, remote_observed=True, remote_oid=None),
            "permission denied",
            retryable=False,
        )
