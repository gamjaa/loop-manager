from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest import mock

import loop_manager


class ProjectFetchBranchDeletionTest(unittest.TestCase):
    repo = Path('/repo')
    oid = 'a' * 40
    branch = 'loop-manager/G-115'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name) / 'state'

    def result(self, stdout='', returncode=0):
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    def test_deleted_listed_pr_branch_does_not_abort_base_fetch(self):
        """Use the real per-branch query to reproduce deletion between reads."""
        refs = f'{self.oid}\trefs/heads/{self.branch}\n{self.oid}\trefs/heads/main\n'
        responses = [
            self.result(refs),       # List still contains the merged PR head.
            self.result(),           # PR branch has since been deleted.
            self.result(f'{self.oid}\trefs/heads/main\n'),
            self.result(self.oid),    # Existing main tracking ref.
            self.result(self.oid),    # Final base verification.
        ]
        with mock.patch.object(loop_manager, 'git', side_effect=responses) as git, \
                mock.patch.object(loop_manager.manager_git_journal, 'execute') as journal:
            self.assertEqual(loop_manager.remote_base(
                self.repo, 'main', state_dir=self.state_dir,
            ), 'main')
        self.assertEqual(git.call_count, len(responses))
        journal.assert_called_once()
        self.assertEqual(journal.call_args.kwargs['target'], 'refs/remotes/origin/main')

    def test_deleted_base_still_fails(self):
        with mock.patch.object(loop_manager, 'git', side_effect=[
            self.result(f'{self.oid}\trefs/heads/main\n'), self.result(),
        ]), mock.patch.object(loop_manager.manager_git_journal, 'execute') as journal:
            with self.assertRaises(loop_manager.RemoteBranchMissingError):
                loop_manager.remote_base(self.repo, 'main', state_dir=self.state_dir)
        journal.assert_not_called()

    def test_missing_base_in_list_still_fails(self):
        with mock.patch.object(loop_manager, 'git', return_value=self.result()), \
                mock.patch.object(loop_manager, '_recorded_base_fetch') as fetch:
            with self.assertRaisesRegex(RuntimeError, 'origin/main'):
                loop_manager.remote_base(self.repo, 'main', state_dir=self.state_dir)
        fetch.assert_not_called()

    def test_non_base_query_failure_and_invalid_response_are_not_skipped(self):
        invalid_results = [
            self.result(returncode=128),
            self.result('invalid-response'),
            self.result(f'{self.oid}\trefs/heads/other\n'),
            self.result(f'not-an-oid\trefs/heads/{self.branch}\n'),
            self.result(f'{self.oid}\trefs/heads/{self.branch}\n' * 2),
        ]
        refs = f'{self.oid}\trefs/heads/{self.branch}\n{self.oid}\trefs/heads/main\n'
        for response in invalid_results:
            with self.subTest(response=response), \
                    mock.patch.object(loop_manager, 'git', side_effect=[
                        self.result(refs), response,
                    ]), mock.patch.object(loop_manager.manager_git_journal, 'execute') as journal:
                with self.assertRaises(RuntimeError) as error:
                    loop_manager.remote_base(self.repo, 'main', state_dir=self.state_dir)
                self.assertNotIsInstance(error.exception, loop_manager.RemoteBranchMissingError)
                journal.assert_not_called()

    def test_direct_required_branch_fetch_still_fails_when_deleted(self):
        with mock.patch.object(loop_manager, 'git', return_value=self.result()), \
                mock.patch.object(loop_manager.manager_git_journal, 'execute') as journal:
            with self.assertRaises(loop_manager.RemoteBranchMissingError):
                loop_manager._recorded_base_fetch(
                    self.repo, self.state_dir, self.branch, owner='project',
                )
        journal.assert_not_called()

    def test_branch_list_failure_is_not_skipped(self):
        with mock.patch.object(loop_manager, 'git', side_effect=RuntimeError('network')), \
                mock.patch.object(loop_manager, '_recorded_base_fetch') as fetch:
            with self.assertRaisesRegex(RuntimeError, 'network'):
                loop_manager.remote_base(self.repo, 'main', state_dir=self.state_dir)
        fetch.assert_not_called()
