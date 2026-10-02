import unittest
from pathlib import Path
from unittest import mock
from tests.support.publication import matching_branch_pr_lookup


class PublicationFixtureTest(unittest.TestCase):
    def test_matching_pr_reads_head_only_when_queried(self):
        with mock.patch('tests.support.publication.git_command', side_effect=['first', 'rebased']) as git:
            lookup = matching_branch_pr_lookup([None, {'url': 'pr'}, {'url': 'pr'}])
            git.assert_not_called()
            self.assertIsNone(lookup(Path('/repo'), 'feature'))
            git.assert_not_called()
            self.assertEqual(lookup(Path('/repo'), 'feature')['headRefOid'], 'first')
            self.assertEqual(lookup(Path('/repo'), 'feature')['headRefOid'], 'rebased')
            self.assertEqual(git.call_args_list, [
                mock.call(Path('/repo'), 'rev-parse', 'refs/heads/feature'),
                mock.call(Path('/repo'), 'rev-parse', 'refs/heads/feature'),
            ])

    def test_callable_fixture_preserves_explicit_stale_head(self):
        response = {'url': 'pr', 'headRefName': 'other', 'headRefOid': 'stale'}
        source = mock.Mock(return_value=response)
        with mock.patch('tests.support.publication.git_command') as git:
            lookup = matching_branch_pr_lookup(source)
            self.assertIs(lookup(Path('/repo'), 'feature', 'saved-url'), response)
            source.assert_called_once_with(Path('/repo'), 'feature', 'saved-url')
            git.assert_not_called()
