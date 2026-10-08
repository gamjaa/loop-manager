import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support import publication as publication_fixture
from tests.support.publication import PublicationFixture, matching_branch_pr_lookup
from loop_manager_internal.storage import planning_input_state


class PublicationFixtureTest(unittest.TestCase):
    def test_publication_setup_registers_and_closes_owned_connection_on_return(self):
        fixture = PublicationFixtureTest()
        connection = mock.MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            (repo / 'docs').mkdir()
            worktree = Path(directory) / 'worktree'
            (worktree / 'src').mkdir(parents=True)
            with mock.patch.object(
                publication_fixture, 'make_remote_repository', return_value=repo
            ), mock.patch.object(
                publication_fixture, 'commit_and_push'
            ), mock.patch.object(
                publication_fixture, 'git_command', return_value='head-sha'
            ), mock.patch.object(
                publication_fixture.sqlite3, 'connect', return_value=connection
            ), mock.patch.object(
                loop_manager, 'register_execplans'
            ), mock.patch.object(
                loop_manager, '_parse_execplan', return_value=object()
            ), mock.patch.object(
                loop_manager, 'planning_input_fingerprint', return_value='fingerprint'
            ), mock.patch.object(
                planning_input_state, 'record_general'
            ), mock.patch.object(
                loop_manager, 'plan_declarations', return_value=[]
            ), mock.patch.object(
                loop_manager, 'expected_execplan_workspace',
                return_value=('branch', worktree),
            ), mock.patch.object(
                loop_manager, 'make_worktree'
            ):
                prepared = PublicationFixture._publication_state(fixture, directory)

            self.assertIs(prepared[2], connection)
            self.assertEqual(len(prepared), 6)
            fixture.doCleanups()
            connection.close.assert_called_once_with()

    def test_publication_setup_closes_owned_connection_when_database_setup_fails(self):
        fixture = PublicationFixtureTest()
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError('schema setup failed')
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            (repo / 'docs').mkdir()
            with mock.patch.object(
                publication_fixture, 'make_remote_repository', return_value=repo
            ), mock.patch.object(
                publication_fixture, 'commit_and_push'
            ), mock.patch.object(
                publication_fixture, 'git_command', return_value='head-sha'
            ), mock.patch.object(
                publication_fixture.sqlite3, 'connect', return_value=connection
            ):
                with self.assertRaisesRegex(RuntimeError, 'schema setup failed'):
                    PublicationFixture._publication_state(fixture, directory)
            connection.close.assert_called_once_with()
            fixture.doCleanups()
            connection.close.assert_called_once_with()

    def test_publication_setup_closes_owned_connection_when_worktree_creation_fails(self):
        fixture = PublicationFixtureTest()
        connection = mock.MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            (repo / 'docs').mkdir()
            with mock.patch.object(
                publication_fixture, 'make_remote_repository', return_value=repo
            ), mock.patch.object(
                publication_fixture, 'commit_and_push'
            ), mock.patch.object(
                publication_fixture, 'git_command', return_value='head-sha'
            ), mock.patch.object(
                publication_fixture.sqlite3, 'connect', return_value=connection
            ), mock.patch.object(
                loop_manager, 'register_execplans'
            ), mock.patch.object(
                loop_manager, '_parse_execplan', return_value=object()
            ), mock.patch.object(
                loop_manager, 'planning_input_fingerprint', return_value='fingerprint'
            ), mock.patch.object(
                planning_input_state, 'record_general'
            ), mock.patch.object(
                loop_manager, 'plan_declarations', return_value=[]
            ), mock.patch.object(
                loop_manager, 'expected_execplan_workspace',
                return_value=('branch', Path(directory) / 'worktree'),
            ), mock.patch.object(
                loop_manager, 'make_worktree',
                side_effect=RuntimeError('git worktree setup failed'),
            ):
                with self.assertRaisesRegex(RuntimeError, 'git worktree setup failed'):
                    PublicationFixture._publication_state(fixture, directory)
            connection.close.assert_called_once_with()
            fixture.doCleanups()
            connection.close.assert_called_once_with()

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
