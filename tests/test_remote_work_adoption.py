"""Unit ownership proof and Manager-only native Git regression for remote adoption."""
from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

import loop_manager as lm
from loop_manager_internal.git import workspace
from tests.support.database import runtime_connection
from tests.support.git import git_command, isolated_git_test, local_github_origin, make_remote_repository


class RemoteCreationEvidenceTests(TestCase):
    def test_reserved_registration_observes_unmerged_index_without_write_tree(self):
        from loop_manager_internal.runtime import bootstrap_adapter
        with TemporaryDirectory() as directory:
            worktree = Path(directory)
            raw_index = '100644 ' + 'a' * 40 + ' 2\tapp.py\0'
            git = mock.Mock(return_value=mock.Mock(stdout=raw_index))
            effects = bootstrap_adapter.ManagedWorktreeTransitionStateEffects(
                _canonical_path=lambda path: path.resolve(),
                _worktree_catalog=lambda *_: [{'worktree': str(worktree), 'HEAD': 'head', 'detached': True}],
                git=git)
            observed = bootstrap_adapter._managed_worktree_transition_state(effects, worktree, worktree)
            self.assertEqual(observed['index'], {'unmerged': raw_index})
            git.assert_called_once_with(worktree, 'ls-files', '-u', '-z')

    def test_reserved_owned_commit_uses_shared_history_without_clean_reset(self):
        from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRecord
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree = root / 'repo', root / 'G-047'
            worktree.mkdir()
            (worktree / 'preserved.txt').write_text('unfinished\n')
            created_head, head, tree, branch = 'a' * 40, 'b' * 40, 'c' * 40, 'loop-manager/G-047'
            blob = 'd' * 40
            (worktree / 'new.txt').write_text('new committed content\n')
            created = {'exists': True, 'index': 'initial-tree', 'registration': {
                'head': created_head, 'branch': 'refs/heads/' + branch, 'locked': 'owned-marker'}}
            live = {**created, 'index': tree, 'registration': {**created['registration'], 'head': head}}
            checkpoint = CheckpointRecord(1, None, head, None, None, {
                'before_head': created_head, 'after_head': head,
                'commit_message': 'Owned commit', 'commit_tree_oid': tree})
            snapshot = lm.GitSnapshot(head, branch, {
                'preserved.txt': (f'H 100644 {blob} 0',), 'new.txt': (f'H 100644 {blob} 0',)}, {}, {}, {}, {}, {})
            def git(_repo, *args, **_kwargs):
                output = {('ls-tree', '-r', '--full-tree', '-z', created_head): f'100644 blob {blob}\tpreserved.txt\0',
                          ('ls-tree', '-r', '--full-tree', '-z', head): f'100644 blob {blob}\tpreserved.txt\0' + f'100644 blob {blob}\tnew.txt\0',
                          ('rev-parse', f'{head}^{{tree}}'): tree,
                          ('show', '-s', '--format=%P', head): created_head,
                          ('show', '-s', '--format=%B', head): 'Owned commit\n'}[args]
                return mock.Mock(returncode=0, stdout=output)
            effects = workspace.MakeWorktreeEffects(
                _managed_worktree_marker=mock.Mock(), _managed_worktree_transition_state=mock.Mock(return_value=live),
                _worktree_catalog=mock.Mock(), _write_managed_worktree_receipt=mock.Mock(),
                git=mock.Mock(side_effect=git), validate_managed_worktree=mock.Mock(), validate_reserved_worktree=mock.Mock())
            validator = lambda current: workspace.validate_reserved_worktree_history(
                root / 'state', repo, worktree, branch, 'G-047', current, snapshot,
                checkpoint=checkpoint, git=effects.git, base='main')
            with mock.patch.object(lm, '_workspace_effects_make_worktree', return_value=effects), \
                 mock.patch.object(workspace.manager_git_journal, 'recover_active'), \
                 mock.patch.object(workspace.manager_git_journal, 'verified_managed_transitions',
                    side_effect=lambda *_a, **kwargs: ([{'intent': {'expected': created}, 'result': {'observed': created}}]
                        if kwargs['kind'] == 'managed_worktree_create' else [])):
                self.assertEqual(lm.make_worktree(repo, worktree, branch, 'main',
                    ownership_reserved=True, expected_head=created_head, journal_state_dir=root / 'state',
                    journal_owner='plan:G-047', reservation_validator=validator), 'existing')
                effects.validate_reserved_worktree.assert_not_called()
                effects._write_managed_worktree_receipt.assert_not_called()
                checkpoint = replace(checkpoint, operation='commit', expected_head=created_head)
                self.assertEqual(lm.make_worktree(repo, worktree, branch, 'main',
                    ownership_reserved=True, expected_head=created_head, journal_state_dir=root / 'state',
                    journal_owner='plan:G-047', reservation_validator=validator), 'existing')
                self.assertEqual(checkpoint.operation, 'commit')
                for operation in ('push', 'pr_create'):
                    checkpoint = replace(checkpoint, operation=operation, expected_head=head,
                        snapshot={**checkpoint.snapshot, 'phase': 'PUBLISHING', 'head': head, 'remote_before': None})
                    self.assertEqual(validator(live), created_head)
                    self.assertEqual(checkpoint.operation, operation)
                # Same visible HEAD cannot authorize a different commit intent.
                checkpoint.snapshot['commit_message'] = 'Unapproved message'
                with self.assertRaises(lm.WorkspaceOwnershipError):
                    validator(live)
            self.assertEqual((worktree / 'preserved.txt').read_text(), 'unfinished\n')
            self.assertTrue(all(call.args[1] in {'ls-tree', 'show', 'rev-parse'} for call in effects.git.call_args_list))

    def test_reserved_registration_and_index_are_reobserved_after_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / 'G-047'
            worktree.mkdir()
            repo = root / 'repo'
            head = 'a' * 40
            live = {'exists': True, 'index': 'tree',
                    'registration': {'head': head, 'branch': 'refs/heads/loop-manager/G-047',
                                     'locked': 'loop-manager-worktree:receipt'}}
            effects = workspace.MakeWorktreeEffects(
                _managed_worktree_marker=mock.Mock(), _managed_worktree_transition_state=mock.Mock(),
                _worktree_catalog=mock.Mock(), _write_managed_worktree_receipt=mock.Mock(),
                git=mock.Mock(), validate_managed_worktree=mock.Mock(), validate_reserved_worktree=mock.Mock())
            for changed in (
                {**live, 'index': 'foreign-tree'},
                {**live, 'registration': None},
                {**live, 'registration': {**live['registration'], 'locked': 'foreign-marker'}},
            ):
                with self.subTest(changed=changed), \
                     mock.patch.object(workspace.manager_git_journal, 'recover_active'), \
                     mock.patch.object(workspace.manager_git_journal, 'verified_managed_transitions',
                                       return_value=[{'intent': {'expected': live}, 'result': {'observed': live}}]):
                    effects._managed_worktree_transition_state.side_effect = [live, changed]
                    with self.assertRaisesRegex(lm.WorkspaceOwnershipError, '검증 중'):
                        workspace.make_worktree(effects, repo, worktree, 'loop-manager/G-047', 'main',
                            ownership_reserved=True, expected_head=head,
                            journal_state_dir=root / 'state', journal_owner='plan:G-047')
            effects.git.assert_not_called()
            effects._write_managed_worktree_receipt.assert_not_called()

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
            for name, records, accepted, strict, basis in (
                ('missing', [], False, True, head),
                ('matching', [evidence], True, True, head),
                ('stale', [stale], False, True, head),
                ('ordinary-reservation-with-new-basis', [evidence], True, False, 'd' * 40),
                ('adoption-with-wrong-basis', [evidence], False, True, 'd' * 40),
                ('ordinary-without-creation-proof', [], False, False, head),
            ):
                with self.subTest(evidence=name), \
                     mock.patch.object(workspace.manager_git_journal, 'recover_active'), \
                     mock.patch.object(workspace.manager_git_journal, 'verified_managed_transitions',
                                       return_value=records):
                    effects.validate_reserved_worktree.reset_mock()
                    options = dict(ownership_reserved=True, expected_head=basis,
                        journal_state_dir=root / 'state', journal_owner='plan:G-047',
                        require_creation_evidence=strict)
                    if accepted:
                        self.assertEqual(workspace.make_worktree(effects, repo, worktree,
                            'loop-manager/G-047', 'main', **options), 'existing')
                        effects.validate_reserved_worktree.assert_called_once_with(
                            repo, worktree, 'loop-manager/G-047', head)
                    else:
                        with self.assertRaises(lm.WorkspaceOwnershipError):
                            workspace.make_worktree(effects, repo, worktree,
                                'loop-manager/G-047', 'main', **options)
                        effects.validate_reserved_worktree.assert_not_called()
            effects.git.assert_not_called()


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
            before_output = lm.capture_git_snapshot(worktree)
            user_file = worktree / 'preserved.txt'
            user_file.write_text('unfinished local work\n')
            with self.assertRaises(lm.WorkspaceOwnershipError):
                lm.make_worktree(repo, worktree, branch, 'main', **options)
            self.assertEqual(user_file.read_text(), 'unfinished local work\n')
            # The policy unit tests bind outputs to a stopped invocation. This
            # native slice checks preservation with those prevalidated inputs.
            from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRecord
            recorded_output = lm.capture_git_snapshot(worktree)
            validator = lambda live: workspace.validate_reserved_worktree_history(
                state_dir, repo, worktree, branch, 'G-047', live, lm.capture_git_snapshot(worktree),
                checkpoint=CheckpointRecord(), git=lm.git, base='main',
                agent_scopes=((before_output, recorded_output),))
            self.assertEqual(lm.make_worktree(repo, worktree, branch, 'main',
                **options, reservation_validator=validator), 'existing')
            self.assertEqual(git_command(worktree, 'rev-parse', 'HEAD'), head)
            self.assertEqual(lm.capture_git_snapshot(worktree).index, before_output.index)
            self.assertEqual(user_file.read_text(), 'unfinished local work\n')
            user_file.unlink()
            git_command(repo, 'push', 'origin', '--delete', branch)
            connection = runtime_connection(str(repo))
            self.addCleanup(connection.close)
            runtime = lm.runtime_state.RuntimeStateRepository(connection)
            runtime.activate(str(repo), 'G-047', activation_sha=base, branch=branch, worktree=str(worktree))
            # This scenario has no concurrent planning attempt holding cleanup.
            connection.execute('UPDATE projects SET planning_attempt_fingerprint=NULL')
            connection.commit()
            self.assertTrue(runtime.recovery_slot_available(str(repo)))
            project = connection.execute('SELECT * FROM projects').fetchone()
            pr = {'url': 'https://github.com/fixture/project/pull/1', 'state': 'MERGED',
                  'headRefName': branch, 'baseRefName': 'main', 'headRefOid': head,
                  'mergeCommit': {'oid': head}}
            result = lm.reconcile_existing_pull_request(connection, repo, state_dir, str(repo), project, pr)
            self.assertEqual(result, lm.PROJECT_STEP_PLANNING_PENDING)
            self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'MERGED')
            self.assertEqual(git_command(repo, 'rev-parse', 'origin/main'), base)
            self.assertFalse(worktree.exists())
            self.assertIsNone(connection.execute('SELECT * FROM cleanup_pending').fetchone())
