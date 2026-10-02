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
