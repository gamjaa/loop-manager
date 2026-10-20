"""Evidence Free Rebase Preparation regression tests."""

from tests.support.current_rebase import current_rebase_case
from dataclasses import replace
from unittest import TestCase

from loop_manager_internal.execution import execution_handlers as handlers




class EvidenceFreeRebasePreparationTests(TestCase):
    def setUp(self):
        self.case = current_rebase_case(self)
        self.case.fixture.effects.workspace.is_directory.return_value = True
        self.case.handler.catalog_plan = self.case.state.active_execplan('repo')

    def pending(self):
        from loop_manager_internal.git.managed_rebase_validation import IndexEntry
        self.case.observed = replace(self.case.observed, head='partial', branch=None,
            branch_ref=('before', ''), registration=('HEAD=partial', 'detached=True'),
            branch_worktrees=(), operations=(('rebase-merge', 'operation'),),
            metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                      ('orig-head', 'before'), ('onto', 'base')),
            index=(IndexEntry('app.py', '100644', 'manual', 0, 'H', 0),), index_clean=False)

    def test_missing_checkpoint_is_rebuilt_from_current_rebase_without_losing_work(self):
        self.pending()
        case = self.case
        case.fixture.effects.workspace._rebase_in_progress_for_branch.return_value = True
        case.fixture.connection.execute("DELETE FROM execution_checkpoints")
        case.state.update_project('repo', last_error='prior failure', publication_remote_oid='remote')
        case.state.repository.update_plan('repo', 'G-047', plan_values={'workspace_owned': 0}, enqueue=False)
        case.state.expected = case.state.repository.projected('repo')
        case.handler.catalog_plan = case.state.active_execplan('repo')
        before = dict(case.state.row())
        self.assertTrue(case.handler._resolving_rebase(case.state.row()))
        restored = case.checkpoint.get('repo', 'G-047')
        self.assertEqual((restored.operation, restored.expected_head, restored.target_base_oid,
                          restored.observed_remote_oid), ('rebase', 'before', 'base', 'remote'))
        self.assertEqual(restored.snapshot['recovered_rebase_metadata'], dict(case.observed.metadata))
        self.assertEqual(case.state.active_execplan('repo')['workspace_owned'], 1)
        for field in ('worker_session_id', 'worker_result', 'verify_result', 'review_result', 'last_error'):
            self.assertEqual(case.state.row()[field], before[field])
        # Reentry consumes the same checkpoint instead of inventing another operation.
        case.handler.catalog_plan = case.state.active_execplan('repo')
        self.assertTrue(case.handler._resolving_rebase(case.state.row()))
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), restored)

    def test_missing_checkpoint_rejects_unverified_remote_without_writing_recovery(self):
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        self.pending()
        case = self.case
        case.fixture.effects.workspace._rebase_in_progress_for_branch.return_value = True
        case.fixture.connection.execute("DELETE FROM execution_checkpoints")
        case.state.update_project('repo', publication_remote_oid='remote')
        case.state.repository.update_plan('repo', 'G-047', plan_values={'workspace_owned': 0}, enqueue=False)
        case.state.expected = case.state.repository.projected('repo')
        case.handler.catalog_plan = case.state.active_execplan('repo')
        case.observed = replace(case.observed, remote=replace(case.observed.remote, oid='foreign'))
        with self.assertRaises(RecoveryFailure) as caught:
            case.handler._resolving_rebase(case.state.row())
        self.assertEqual(caught.exception.reason, RecoveryReason.OWNERSHIP_UNCERTAIN)
        self.assertEqual(case.checkpoint.get('repo', 'G-047').generation, 0)
        self.assertEqual(case.state.active_execplan('repo')['workspace_owned'], 0)

    def test_missing_checkpoint_preserves_concurrent_runtime_change(self):
        from unittest import mock
        from tests.support.rebase_observations import scope_for
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        self.pending()
        case = self.case
        case.fixture.effects.workspace._rebase_in_progress_for_branch.return_value = True
        case.fixture.connection.execute('DELETE FROM execution_checkpoints')
        case.state.update_project('repo', publication_remote_oid='remote')
        calls = 0
        def observe(expected, _adapters):
            nonlocal calls
            calls += 1
            if calls == 2:
                case.state.repository.update_plan('repo', 'G-047',
                    plan_values={'last_error': 'concurrent failure'}, enqueue=False)
            return case.observed, scope_for(expected, case.observed)
        with mock.patch('loop_manager_internal.git.managed_rebase_adoption.observe_current_rebase',
                        side_effect=observe):
            with self.assertRaises(RecoveryFailure) as caught:
                case.handler._resolving_rebase(case.state.row())
        self.assertEqual(caught.exception.reason, RecoveryReason.REMOTE_SNAPSHOT_RACE)
        self.assertEqual(case.checkpoint.get('repo', 'G-047').generation, 0)
        self.assertEqual(case.state.repository.get('repo', 'G-047').last_error, 'concurrent failure')

    def test_owned_detached_rebase_is_recognized_without_old_error_marker(self):
        self.pending()
        self.case.state.update_project('repo', last_error='different historical failure')
        self.assertTrue(self.case.handler._resolving_rebase(self.case.state.row()))

    def test_detached_metadata_alone_cannot_bypass_current_remote_or_target_check(self):
        self.pending()
        original = self.case.observed
        for observed in (replace(original, remote=replace(original.remote, oid='foreign')),
                         replace(original, metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                                                     ('orig-head', 'before'), ('onto', 'foreign')))):
            with self.subTest(observed=observed):
                self.case.observed = observed
                with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
                    self.case.handler._resolving_rebase(self.case.state.row())

    def test_initial_and_detached_context_validate_current_existing_pr_lease(self):
        from loop_manager_internal.git.managed_rebase_validation import PullRequest
        remote = replace(self.case.expected.remote, repository='repo', oid='remote', pull_request=PullRequest(
            'https://example.test/pr/47', 'repo', 'loop-manager/G-047',
            'remote', 'OPEN'))
        self.case.expected = replace(self.case.expected, remote=remote)
        self.case.observed = replace(self.case.observed, remote=remote)
        self.case.state.update_project('repo', pr_url=remote.pull_request.identity, pr_head_oid=remote.oid)
        self.assertFalse(self.case.handler._context().resolving_rebase)
        self.pending()
        self.assertTrue(self.case.handler._context().resolving_rebase)
        self.case.observed = replace(self.case.observed, remote=replace(remote, pull_request=None))
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            self.case.handler._context()
