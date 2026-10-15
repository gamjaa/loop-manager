import sqlite3
import unittest
from dataclasses import replace
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
import loop_manager


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

    def test_pr_checkpoint_requires_saved_open_identity_and_exact_head(self):
        pr = {'url': 'saved-pr', 'state': 'OPEN', 'mergedAt': None,
              'headRefName': 'loop/G-167', 'headRefOid': 'head', 'baseRefName': 'main'}
        def matches(value):
            return blocked_checkpoint.checkpoint_pr_matches(value, url='saved-pr',
                branch='loop/G-167', head='head', base='main')
        self.assertTrue(matches(pr))
        for changes in ({'url': 'other'}, {'state': 'CLOSED'}, {'state': 'MERGED'},
                        {'mergedAt': 'merged'}, {'headRefName': 'other'},
                        {'headRefOid': 'user-push'}, {'baseRefName': 'other'}):
            with self.subTest(changes=changes):
                self.assertFalse(matches({**pr, **changes}))
        self.assertFalse(matches(None))
        self.assertFalse(blocked_checkpoint.checkpoint_pr_matches(pr,
            url=None, branch='loop/G-167', head='head'))

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
                "workspace_ownership_uncertain",
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


class ExistingPrCheckpointPublicationTests(unittest.TestCase):
    """Exercise real PR/checkpoint policy and SQLite with fake external effects."""

    def setUp(self):
        from pathlib import Path
        from types import SimpleNamespace
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state, blocked_checkpoint_state
        from loop_manager_internal.recovery.checkpoint_adapter import BlockedCheckpointAdapter

        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.activate('repo', 'G-047', activation_sha='base', branch='loop/G-047', worktree='/owned')
        self.old, self.head = 'a' * 40, 'b' * 40
        runtime.update_plan('repo', 'G-047', plan_values={
            'pr_url': 'saved-pr', 'pr_head_oid': self.old, 'publication_remote_oid': self.old,
            'verify_token': 'old-verify', 'review_token': 'old-review'})
        runtime.block_plan('repo', 'G-047', reason_code='verify_retry_exhausted',
                           evidence='preserved failure', release_fingerprint='inputs', clear_projection=True)
        self.store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        self.request = self.store.get('repo', 'G-047')
        self.remote, self.pr_changes, self.pushes = self.old, {}, []
        self.tracking_failure = False
        self.concurrent_push = False
        self.scope = blocked_checkpoint.SafetyVerdict(True, blocked_checkpoint.SafetyScope.PLAN,
                                                     None, None, self.head, ())
        safety, publication = mock.Mock(), mock.Mock()
        safety.pr_for_branch.side_effect = lambda *_: {
            'url': 'saved-pr', 'state': 'OPEN', 'headRefName': 'loop/G-047',
            'headRefOid': self.remote, 'baseRefName': 'main', **self.pr_changes}
        def git(_path, *args, **_kwargs):
            if 'push' in args:
                self.pushes.append(args)
                if self.concurrent_push:
                    self.remote = 'd' * 40
                    output, code = 'stale info', 1
                else:
                    self.remote = self.head
                    output, code = '', 0
            elif args[0] == 'ls-remote':
                output, code = f'{self.remote}\trefs/heads/loop/G-047\n', 0
            elif args[0] == 'update-ref' and self.tracking_failure:
                output, code = 'tracking ref changed', 1
            else:
                output, code = self.old, 0
            return SimpleNamespace(returncode=code, stdout=output)
        publication.git.side_effect = git
        self.adapter = BlockedCheckpointAdapter(self.connection, Path('/repo'), Path('/state'), 'repo',
            safety_effects=lambda: safety, publication_effects=lambda: publication)
        self.adapter._validated_push_url = mock.Mock(return_value='https://github.com/example/repo.git')
        self.adapter.assess = mock.Mock(side_effect=lambda *_: self.scope)
        self.adapter.prepare = mock.Mock(return_value=blocked_checkpoint.PreparedCheckpoint(
            self.old, 'c' * 40, 'identity', 'message', ()))
        self.adapter.ensure_local_commit = mock.Mock(return_value=self.head)
        self.adapter.is_fast_forward = mock.Mock(return_value=True)
        self.dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(
            self.store, self.adapter, self.adapter, mock.Mock())

    def test_fast_forward_existing_pr_preserves_block_and_invalidates_approval(self):
        self.dispatcher.dispatch('repo')
        self.assertEqual(len(self.pushes), 1)
        self.assertIn('--force-with-lease=refs/heads/loop/G-047:' + self.old, self.pushes[0])
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(plan['status'], 'PLAN_BLOCKED')
        self.assertEqual(plan['pr_url'], 'saved-pr')
        self.assertEqual(plan['pr_head_oid'], self.head)
        self.assertIsNone(plan['verify_token'])
        self.assertIsNone(plan['review_token'])
        self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'published')
        self.assertEqual(self.dispatcher.dispatch('repo'), 0)
        self.assertEqual(len(self.pushes), 1)

    def test_closed_pr_without_agent_scope_stays_plan_blocked_without_observations(self):
        from loop_manager_internal.storage import runtime_state
        runtime_state.RuntimeStateRepository(self.connection).block_plan(
            'repo', 'G-047', reason_code='pull_request_closed',
            evidence='PR closed', release_fingerprint='closed', clear_projection=True)
        del self.adapter.assess
        self.dispatcher.dispatch('repo')
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(plan['status'], 'PLAN_BLOCKED')
        self.assertEqual(plan['blocked_reason'], 'pull_request_closed')
        self.assertEqual(plan['pr_head_oid'], self.old)
        self.assertIsNone(self.connection.execute('SELECT * FROM project_blocks').fetchone())
        self.adapter.safety_effects.pr_for_branch.assert_not_called()
        self.adapter.safety_effects.capture_git_snapshot.assert_not_called()
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'unpublished')

    def test_push_success_tracking_ref_failure_retries_without_another_push(self):
        self.tracking_failure = True
        decision = blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.FAST_FORWARD)
        with self.assertRaisesRegex(RuntimeError, 'tracking ref changed'):
            self.adapter.push(self.request, self.head, decision)
        self.tracking_failure = False
        self.adapter.push(self.request, self.head, decision)
        self.assertEqual(self.adapter.confirm(self.request, self.head), self.head)
        self.assertEqual(len(self.pushes), 1)

    def test_pr_close_merge_user_push_and_branch_changes_prevent_push(self):
        for changes in ({'state': 'CLOSED'}, {'state': 'MERGED'}, {'url': 'other'},
                        {'headRefName': 'other'}, {'headRefOid': 'user-push'}):
            with self.subTest(changes=changes):
                self.pr_changes = changes
                with self.assertRaises(blocked_checkpoint.CheckpointPushError):
                    self.adapter.push(self.request, self.head,
                        blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.FAST_FORWARD))
                self.assertEqual(self.pushes, [])
        self.pr_changes = {}
        self.remote = 'd' * 40
        with self.assertRaises(blocked_checkpoint.CheckpointPushError):
            self.adapter.push(self.request, self.head,
                blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.FAST_FORWARD))
        self.assertEqual(self.pushes, [])

    def test_current_dirty_work_or_origin_change_prevents_push(self):
        self.scope = replace(self.scope, included_paths=('user.py',))
        with self.assertRaises(blocked_checkpoint.CheckpointPushError):
            self.adapter.push(self.request, self.head,
                blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.FAST_FORWARD))
        self.scope = replace(self.scope, included_paths=())
        self.adapter._validated_push_url.side_effect = RuntimeError('origin changed')
        with self.assertRaisesRegex(blocked_checkpoint.CheckpointPushError, 'origin changed'):
            self.adapter.push(self.request, self.head,
                blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.FAST_FORWARD))
        self.assertEqual(self.pushes, [])

    def test_non_fast_forward_pr_requires_current_rebase_acceptance(self):
        with self.assertRaisesRegex(blocked_checkpoint.CheckpointPushError, 'rebase 완료 근거'):
            self.adapter.push(self.request, self.head,
                blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.REBASE_LEASE))
        self.assertEqual(self.pushes, [])

    def test_push_then_sqlite_interruption_recovers_same_checkpoint(self):
        class Exit(BaseException):
            pass
        with mock.patch.object(self.store, 'finish_published', side_effect=Exit()):
            with self.assertRaises(Exit):
                self.dispatcher.dispatch('repo')
        self.assertEqual(self.remote, self.head)
        self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'local_committed')
        self.dispatcher.dispatch('repo')
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.store.get('repo', 'G-047').phase.value, 'published')

    def test_concurrent_push_rejects_original_lease_and_preserves_user_commit(self):
        self.concurrent_push = True
        self.dispatcher.dispatch('repo')
        self.assertEqual(self.remote, 'd' * 40)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn('--force-with-lease=refs/heads/loop/G-047:' + self.old, self.pushes[0])
        request = self.store.get('repo', 'G-047')
        self.assertEqual(request.phase.value, 'unpublished')
        self.assertEqual(request.expected_remote_oid, self.old)
        self.assertEqual(request.local_oid, self.head)

    def test_interrupted_checkpoint_preparation_keeps_recorded_commit_identity(self):
        request = replace(self.request, tree_oid='c' * 40, commit_identity='saved-identity',
                          local_oid=self.head)
        from loop_manager_internal.recovery.checkpoint_adapter import CheckpointPublicationAdapter
        with mock.patch.object(self.adapter, '_tree_for_paths', return_value=request.tree_oid):
            prepared = CheckpointPublicationAdapter.prepare(self.adapter, request, self.scope)
        self.assertEqual(prepared.commit_identity, request.commit_identity)
        self.assertEqual(prepared.parent_oid, self.head)
        self.assertEqual(prepared.included_paths, ())

    def test_checkpoint_does_not_release_independent_project_or_plan_blocks(self):
        self.connection.execute("INSERT INTO project_blocks "
            "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
            "VALUES ('repo', 'project', 'shared_ref_mutation', 'G-047', 'independent safety cause', 'WORKING')")
        before = dict(self.connection.execute('SELECT * FROM project_blocks').fetchone())
        plan_block = dict(self.connection.execute('SELECT * FROM execplan_blocks').fetchone())
        self.dispatcher.dispatch('repo')
        self.assertEqual(dict(self.connection.execute('SELECT * FROM project_blocks').fetchone()), before)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM execplan_blocks').fetchone()), plan_block)

    def test_final_validated_pr_candidate_reuses_confirmed_checkpoint_without_push(self):
        from pathlib import Path
        from types import SimpleNamespace
        from loop_manager_internal.git.publication_service import push_owned_pr_conflict_recovery
        self.dispatcher.dispatch('repo')
        self.connection.execute("UPDATE exec_plans SET status='PUBLISHING', pr_conflict_recovery=1, "
            "pr_conflict_head_oid=?, pr_conflict_base_oid='base', active_sha='base', validated_base_sha='base'",
            (self.head,))
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        effects = mock.Mock()
        effects.active_execplan.return_value = plan
        effects.remote_branch_oid.return_value = self.head
        pr = {'url': 'saved-pr', 'state': 'OPEN', 'headRefName': 'loop/G-047', 'headRefOid': self.head}
        effects.pr_for_branch.return_value = pr
        effects.git.return_value = SimpleNamespace(returncode=0, stdout=self.head)
        def git(_path, *args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=(self.head + '\trefs/heads/loop/G-047\n'
                if args[0] == 'ls-remote' else self.head))
        effects.git.side_effect = git
        self.assertEqual(push_owned_pr_conflict_recovery(effects, self.connection, Path('/repo'),
            Path('/state'), 'repo', 'loop/G-047', Path('/owned'), pr, self.head), self.head)
        self.assertFalse(any(call.args[1] == 'push' for call in effects.git.call_args_list))
        effects._fetch_pr_conflict_recovery_refs.assert_called_once()
        self.assertEqual(len(self.pushes), 1)
