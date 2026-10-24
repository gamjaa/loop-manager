"""Unit ownership proof and Manager-only native Git regression for remote adoption."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

import loop_manager as lm
from loop_manager_internal.git import workspace
from tests.support.database import runtime_connection
from tests.support.git import git_command, isolated_git_test, local_github_origin, make_remote_repository


class RemoteCreationEvidenceTests(TestCase):
    def test_reserved_resume_requires_sealed_creation_matching_live_resource(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree = root / 'repo', root / 'G-047'
            worktree.mkdir()
            head = 'a' * 40
            live = {'exists': True, 'index': 'b' * 40,
                    'registration': {'head': head, 'branch': 'refs/heads/loop-manager/G-047',
                                     'locked': 'loop-manager-worktree:receipt'}}
            evidence = {'intent': {'expected': live}, 'result': {'observed': live}}
            effects = workspace.MakeWorktreeEffects(
                _managed_worktree_marker=mock.Mock(), _managed_worktree_transition_state=mock.Mock(return_value=live),
                _worktree_catalog=mock.Mock(), _write_managed_worktree_receipt=mock.Mock(),
                git=mock.Mock(), validate_managed_worktree=mock.Mock(), validate_reserved_worktree=mock.Mock())
            stale = {'intent': {'expected': {**live, 'index': 'c' * 40}}, 'result': {'observed': live}}
            for name, records, accepted in (('missing', [], False), ('matching', [evidence], True),
                                             ('stale', [stale], False)):
                with self.subTest(evidence=name), \
                     mock.patch.object(workspace.manager_git_journal, 'recover_active'), \
                     mock.patch.object(workspace.manager_git_journal, 'verified_managed_transitions',
                                       return_value=records):
                    options = dict(ownership_reserved=True, expected_head=head,
                        journal_state_dir=root / 'state', journal_owner='plan:G-047',
                        require_creation_evidence=True)
                    if accepted:
                        self.assertEqual(workspace.make_worktree(effects, repo, worktree,
                            'loop-manager/G-047', 'main', **options), 'existing')
                    else:
                        with self.assertRaisesRegex(lm.WorkspaceOwnershipError, '생성 기록'):
                            workspace.make_worktree(effects, repo, worktree,
                                'loop-manager/G-047', 'main', **options)
            effects.git.assert_not_called()
            effects.validate_reserved_worktree.assert_called_once_with(repo, worktree, 'loop-manager/G-047', head)


@isolated_git_test
class RemoteAdoptionGitTests(TestCase):
    def test_exact_oid_owned_resume_and_merge_after_remote_branch_deletion(self):
        # One native repository covers effects that observation doubles cannot prove.
        # Worker does not run this integration test; Manager full verify owns it.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            local_github_origin(self, repo)
            state_dir = root / 'state'
            branch, worktree = lm.expected_execplan_workspace(state_dir, str(repo), 'G-047')
            base = git_command(repo, 'rev-parse', 'HEAD')
            git_command(repo, 'switch', '-c', 'remote-fixture')
            (repo / 'src/app.py').write_text('VALUE = 2\n')
            git_command(repo, 'add', 'src/app.py')
            git_command(repo, 'commit', '-m', 'Preserved remote work')
            head = git_command(repo, 'rev-parse', 'HEAD')
            git_command(repo, 'push', 'origin', f'HEAD:refs/heads/{branch}')
            git_command(repo, 'switch', 'main')
            self.assertNotEqual(base, head)
            self.assertEqual(lm.make_worktree(repo, worktree, branch, 'main', start_point=head,
                allow_existing=False, journal_state_dir=state_dir, journal_owner='plan:G-047'), 'created')
            self.assertEqual(git_command(worktree, 'rev-parse', 'HEAD'), head)
            self.assertEqual((worktree / 'src/app.py').read_text(), 'VALUE = 2\n')
            # Exact OID alone does not permit taking a user-owned local branch.
            other_branch, other_tree = lm.expected_execplan_workspace(state_dir, str(repo), 'G-048')
            git_command(repo, 'branch', other_branch, head)
            with self.assertRaises(lm.WorkspaceOwnershipError):
                lm.make_worktree(repo, other_tree, other_branch, 'main', start_point=head,
                    journal_state_dir=state_dir, journal_owner='plan:G-048')
            self.assertEqual(git_command(repo, 'rev-parse', other_branch), head)
            self.assertFalse(other_tree.exists())
            options = dict(ownership_reserved=True, expected_head=head, require_creation_evidence=True,
                allow_create_branch=False, journal_state_dir=state_dir, journal_owner='plan:G-047')
            self.assertEqual(lm.make_worktree(repo, worktree, branch, 'main', **options), 'existing')
            user_file = worktree / 'preserved.txt'
            user_file.write_text('unfinished local work\n')
            with self.assertRaises(lm.WorkspaceOwnershipError):
                lm.make_worktree(repo, worktree, branch, 'main', **options)
            self.assertEqual(user_file.read_text(), 'unfinished local work\n')
            user_file.unlink()
            git_command(repo, 'push', 'origin', '--delete', branch)
            connection = runtime_connection(str(repo))
            self.addCleanup(connection.close)
            runtime = lm.runtime_state.RuntimeStateRepository(connection)
            runtime.activate(str(repo), 'G-047', activation_sha=base, branch=branch, worktree=str(worktree))
            project = connection.execute('SELECT * FROM projects').fetchone()
            pr = {'url': 'https://github.com/fixture/project/pull/1', 'state': 'MERGED',
                  'headRefName': branch, 'baseRefName': 'main', 'headRefOid': head,
                  'mergeCommit': {'oid': head}}
            result = lm.reconcile_existing_pull_request(connection, repo, state_dir, str(repo), project, pr)
            self.assertEqual(result, lm.PROJECT_STEP_PLANNING_PENDING)
            self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'MERGED')
            self.assertEqual(git_command(repo, 'rev-parse', 'origin/main'), base)
            self.assertFalse(worktree.exists())
