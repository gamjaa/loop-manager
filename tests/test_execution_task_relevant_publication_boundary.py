"""Task Relevant Publication Boundary regression tests."""

from tests.support.execution import execution_fixture
from dataclasses import replace
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import loop_manager as lm
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind


from tests.support.execution_assertions import _assert_post_push_pr_attachment


class TaskRelevantPublicationBoundaryTests(TestCase):
    def setUp(self):
        self.case = execution_fixture(self)
        self.handler = self.case.handler
        self.effects = self.case.effects

    def observation(self):
        context = self.case.context('PUBLISHING')
        return self.handler._publication_resume().observe(
            context.repo_path, context.worktree, context.branch, None)

    def test_publication_reads_live_remote_instead_of_unused_tracking_cache(self):
        live = mock.Mock(return_value='base')
        self.handler.effects = replace(self.effects, workspace=replace(
            self.effects.workspace, live_remote_branch_oid=live))
        self.effects.workspace.remote_branch_oid.return_value = 'stale'
        self.assertEqual(self.observation().remote_oid, 'base')
        self.effects.workspace.remote_branch_oid.assert_not_called()
        live.assert_called_once()
        self.assertEqual(self.effects.workspace.validate_managed_worktree.call_count, 2)

    def test_observation_rejects_head_change_during_pr_query(self):
        self.effects.publication.pr_for_branch.side_effect = lambda *args: setattr(self.case, 'remote', 'changed')
        with self.assertRaises(lm.PublicationSafetyError):
            self.observation()

    def test_observation_rechecks_selected_worktree_ownership(self):
        self.effects.workspace.validate_managed_worktree.side_effect = [None, lm.WorkspaceOwnershipError('registration changed')]
        with self.assertRaises(lm.WorkspaceOwnershipError):
            self.observation()
        self.effects.publication.command.assert_not_called()

    def test_candidate_changed_since_review_cannot_be_observed_for_publication(self):
        self.case.review_context()
        self.case.state.update_project('repo', phase='PUBLISHING')
        self.case.remote = 'changed'
        with self.assertRaises(lm.PublicationSafetyError):
            self.observation()

    def test_remote_change_between_push_and_pr_creation_stops_creation(self):
        self.case.state.update_project('repo', validated_base_sha='base')
        live = mock.Mock(side_effect=[None, 'competitor'])
        self.handler.effects = replace(self.effects, workspace=replace(
            self.effects.workspace, live_remote_branch_oid=live))
        result = self.handler._publish_new_pull_request(self.case.context('PUBLISHING'), self.case.state.row())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.command.assert_not_called()

    def test_merged_pr_with_deleted_head_after_push_is_reconciled(self):
        self.handler._owned_checkpoint_remote = mock.Mock(side_effect=lambda *args:
            'push' not in self.case.calls or args[2] is not None)
        _assert_post_push_pr_attachment(self, self.case,
            'base', True, pr_state='MERGED', remote_deleted=True)
        self.assertEqual(self.handler._owned_checkpoint_remote.call_count, 2)
        pr = self.effects.publication.reconcile_existing_pull_request.call_args.args[-1]
        self.assertEqual(pr['state'], 'MERGED')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_merged_pr_with_advanced_base_and_deleted_head_is_reconciled(self):
        fetched_bases = []
        def fetch_base():
            base = 'merged-base' if 'push' in self.case.calls else 'base'
            fetched_bases.append(base)
            return base
        self.handler._fetch_latest_base = mock.Mock(side_effect=fetch_base)
        self.handler._latest_before_publication = mock.Mock(side_effect=lambda *_:
            self.assertNotIn('push', self.case.calls))
        self.handler._owned_checkpoint_remote = mock.Mock(side_effect=lambda *args:
            'push' not in self.case.calls or args[2] is not None)

        _assert_post_push_pr_attachment(self, self.case,
            'base', True, pr_state='MERGED', remote_deleted=True)

        self.assertIn('merged-base', fetched_bases)
        self.handler._latest_before_publication.assert_called_once()
        pr = self.effects.publication.reconcile_existing_pull_request.call_args.args[-1]
        self.assertEqual(pr['state'], 'MERGED')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_post_push_terminal_pr_precedes_real_publication_guard_refresh(self):
        for state, head, accepted, kind in (
            ('MERGED', 'base', True, ExecutionOutcomeKind.SUCCESS),
            ('CLOSED', 'base', True, ExecutionOutcomeKind.FINAL_BLOCK),
            ('MERGED', 'other-head', False, ExecutionOutcomeKind.FINAL_BLOCK),
        ):
            with self.subTest(state=state, head=head):
                case = execution_fixture(self)
                case.review_context()
                snapshot = case.handler._definition_snapshot
                case.state.update_project(
                    'repo', phase='PUBLISHING', definition_snapshot=snapshot.serialize(),
                    review_result='PASS', review_token=snapshot.review_token('base', 'base'),
                )
                case.handler._fetch_latest_base = mock.Mock(side_effect=lambda:
                    'merged-base' if 'push' in case.calls else 'base')
                case.effects.revision.reconcile_catalog.side_effect = lambda *_: SimpleNamespace(
                    kind=SimpleNamespace(value='reconciled'),
                    revision_sha='merged-base' if 'push' in case.calls else 'base')
                case.effects.publication.reconcile_existing_pull_request.side_effect = (
                    lambda *_: 1 if state == 'CLOSED' else 0)
                with mock.patch.object(case.handler, 'guard', wraps=case.handler.guard) as guard, \
                     mock.patch.object(case.handler, '_refresh_revision_base',
                                       wraps=case.handler._refresh_revision_base) as refresh, \
                     mock.patch.object(case.handler, '_publish_rebased_checkpoint',
                                       wraps=case.handler._publish_rebased_checkpoint) as checkpoint:
                    _assert_post_push_pr_attachment(self, case,
                        head, accepted, pr_state=state, remote_deleted=True,
                        reconciled_kind=kind)
                self.assertEqual(guard.call_count, 2)
                refresh.assert_not_called()
                checkpoint.assert_called_once()
                self.assertNotIn('rebase', case.calls)
                case.effects.revision.reconcile_catalog.assert_called_once()

    def test_deleted_head_after_push_does_not_authorize_mismatched_or_open_pr(self):
        for state, head in (('MERGED', 'other-head'), ('OPEN', 'base')):
            with self.subTest(state=state):
                case = execution_fixture(self)
                _assert_post_push_pr_attachment(self, case,
                    head, False, pr_state=state, remote_deleted=True)

    def test_closed_pr_with_deleted_head_after_push_keeps_closed_pr_outcome(self):
        self.effects.publication.reconcile_existing_pull_request.side_effect = lambda *_: 1
        _assert_post_push_pr_attachment(self, self.case,
            'base', True, pr_state='CLOSED', remote_deleted=True,
            reconciled_kind=ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_base_advance_before_pr_creation_returns_to_existing_revalidation_flow(self):
        self.case.state.update_project('repo', validated_base_sha='base')
        self.handler._fetch_latest_base = mock.Mock(return_value='advanced')
        expected = ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        self.handler._latest_before_publication = mock.Mock(return_value=expected)
        result = self.handler._publish_new_pull_request(self.case.context('PUBLISHING'), self.case.state.row())
        self.assertEqual(result, expected)
        self.effects.publication.command.assert_not_called()
        self.handler._latest_before_publication.assert_called_once()

    def push_fixture(self, *, branch_changed=False, remote_race=False, existing_pr=False,
                     candidate_changed=False):
        branch = 'loop-manager/G-047'
        head = 'a' * 40
        remote = None
        def git(path, *args, **kwargs):
            nonlocal remote
            if args[0] == 'rev-parse':
                value = ('b' * 40 if candidate_changed or branch_changed and '--verify' in args
                         else head)
                return SimpleNamespace(stdout=value, returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(stdout=f'{remote}\trefs/heads/{branch}\n' if remote else '', returncode=0)
            if args[0] == 'push':
                if remote_race:
                    remote = 'c' * 40
                    return SimpleNamespace(stdout='lease rejected', returncode=1)
                remote = head
            return SimpleNamespace(stdout='', returncode=0)
        stack = ExitStack()
        self.addCleanup(stack.close)
        owned = stack.enter_context(mock.patch.object(lm, '_owned_publication_plan', return_value={
            'plan_id': 'G-047', 'publication_remote_oid': None, 'pr_url': None}))
        command = stack.enter_context(mock.patch.object(lm, 'git', side_effect=git))
        stack.enter_context(mock.patch.object(lm, 'remote_branch_oid', side_effect=lambda *args: remote))
        stack.enter_context(mock.patch.object(lm, '_recorded_base_fetch'))
        stack.enter_context(mock.patch.object(lm, 'pr_for_branch', return_value={'headRefOid': head} if existing_pr else None))
        return branch, head, owned, command

    def push(self, branch, expected_oid='a' * 40):
        return lm.push_managed_publication_branch(self.case.connection, Path('/repo'),
            Path('/state'), 'repo', branch, Path('/state/G-047'), expected_oid)

    def test_candidate_changed_between_observation_and_push_helper_cannot_be_pushed(self):
        branch, head, _, command = self.push_fixture(candidate_changed=True)
        self.case.remote = head
        self.case.review_context()
        snapshot = self.handler._definition_snapshot
        self.case.state.update_project('repo',
            verify_token=snapshot.verify_token('base', head),
            review_token=snapshot.review_token('base', head))
        self.effects.publication.push_managed_publication_branch.side_effect = (
            lambda *args: lm.push_managed_publication_branch(self.case.connection, *args))
        result = self.handler._publish_new_pull_request(
            self.case.context('PUBLISHING'), self.case.state.row())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.push_managed_publication_branch.assert_called_once_with(
            Path('/repo'), Path('/state'), 'repo', branch, Path('/state/G-047'), head)
        self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))
        self.effects.publication.command.assert_not_called()

    def test_push_uses_confirmed_object_oid_and_create_only_lease(self):
        branch, head, owned, command = self.push_fixture()
        self.assertEqual(self.push(branch), head)
        pushed = next(call for call in command.call_args_list if call.args[1] == 'push')
        self.assertIn(f'{head}:refs/heads/{branch}', pushed.args)
        self.assertIn(f'--force-with-lease=refs/heads/{branch}:', pushed.args)
        self.assertEqual(owned.call_count, 2)

    def test_push_rejects_changed_owned_branch_or_new_pr_before_mutation(self):
        for options in ({'branch_changed': True}, {'existing_pr': True}):
            with self.subTest(options=options):
                branch, _, _, command = self.push_fixture(**options)
                with self.assertRaises(lm.PublicationSafetyError):
                    self.push(branch)
                self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))

    def test_remote_creation_race_cannot_be_overwritten_by_first_push(self):
        branch, _, _, command = self.push_fixture(remote_race=True)
        with self.assertRaisesRegex(RuntimeError, 'lease rejected'):
            self.push(branch)
        pushed = next(call for call in command.call_args_list if call.args[1] == 'push')
        self.assertIn(f'--force-with-lease=refs/heads/{branch}:', pushed.args)

    def conflict_push_fixture(self):
        branch = 'loop-manager/G-047'
        head, base, old = 'a' * 40, 'b' * 40, 'c' * 40
        plan = dict(self.case.repository.active_row('repo'))
        plan.update(status='PUBLISHING', pr_conflict_recovery=1, workspace_owned=lm.WORKSPACE_OWNED,
            branch=branch, worktree='/state/G-047', pr_url='https://example/pr/1',
            pr_conflict_head_oid=old, pr_conflict_base_oid=base, active_sha=base, validated_base_sha=base)
        pr = {'url': plan['pr_url'], 'state': 'OPEN', 'headRefName': branch, 'headRefOid': old}
        stack = ExitStack()
        self.addCleanup(stack.close)
        for patch in (
                mock.patch.object(lm, 'active_execplan', return_value=plan),
                mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                mock.patch.object(lm, 'validate_managed_worktree'),
                mock.patch.object(lm, '_fetch_pr_conflict_recovery_refs'),
                mock.patch.object(lm, 'remote_branch_oid', return_value=old)):
            stack.enter_context(patch)
        command = stack.enter_context(mock.patch.object(lm, 'git',
            return_value=SimpleNamespace(stdout=head, returncode=0)))
        lookup = stack.enter_context(mock.patch.object(lm, 'pr_for_branch', return_value=pr))
        return branch, head, pr, command, lookup

    def test_conflict_push_requeries_actual_pr_head_after_fetch(self):
        branch, head, pr, command, lookup = self.conflict_push_fixture()
        lookup.return_value = {**pr, 'headRefOid': 'd' * 40}
        with self.assertRaises(lm.PublicationSafetyError):
            lm.push_owned_pr_conflict_recovery(self.case.connection, Path('/repo'),
                Path('/state'), 'repo', branch, Path('/state/G-047'), pr, head)
        self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))

    def test_conflict_push_rejects_candidate_changed_before_helper_entry(self):
        branch, head, pr, command, lookup = self.conflict_push_fixture()
        command.return_value.stdout = 'd' * 40
        with self.assertRaises(lm.PublicationSafetyError):
            lm.push_owned_pr_conflict_recovery(self.case.connection, Path('/repo'),
                Path('/state'), 'repo', branch, Path('/state/G-047'), pr, head)
        self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))
        lookup.assert_not_called()
