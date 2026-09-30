"""Pure checkpoint decisions and index observation regressions."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
import checkpoint_ownership
import blocked_checkpoint
import execution_checkpoint_state

from execution_checkpoint import (
    CheckpointAction, CheckpointSnapshot, assess, observe,
)


def snapshot(**changes):
    values = dict(
        plan_id='G-044', phase='PUBLISHING', definition_revision='definition',
        base_oid='new-base', branch='loop/G-044', worktree_owned=True,
        local_branch='loop/G-044', local_head='head', base_is_ancestor=True,
        recorded_local_head=None, pending_operation=None,
        active_sha='new-base', validated_base_sha='new-base',
        verify_token='verify', review_token='review', archive_token='archive',
        publication_token='publication', git_operation=None,
        git_operation_markers=(),
        unmerged_stages=(), rebase_intent=False, rebase_before_head=None,
        rebase_target_oid=None, stash_oid=None, staged_paths=(),
        remote_oid=None, recorded_remote_oid=None, pr_state=None,
        pr_head_oid=None, recorded_pr_head_oid=None,
    )
    values.update(changes)
    return CheckpointSnapshot(**values)


class CheckpointAssessmentTests(unittest.TestCase):
    def test_published_owner_requires_commit_tree_and_exact_live_ref(self):
        row = {
            'phase': 'published', 'result': 'published',
            'branch': 'loop/G-044', 'worktree': '/owned',
            'accepted_remote_oid': 'a' * 40, 'published_oid': 'a' * 40,
            'pending_remote_oid': None, 'commit_identity': 'identity',
            'tree_oid': 'tree',
        }
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = row

        def git(_repo, *args, **_kwargs):
            if args[0] == 'show':
                return SimpleNamespace(
                    stdout='[G-044] BLOCKED checkpoint\n\nScope: plan\n'
                           'Checkpoint-Identity: identity\n', returncode=0,
                )
            if args[0] == 'rev-parse':
                return SimpleNamespace(stdout='tree\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(stdout=f"{'a' * 40}\trefs/heads/loop/G-044\n", returncode=0)
            return SimpleNamespace(stdout='', returncode=0)

        def owner(**changes):
            arguments = dict(
                connection=connection, repo_path=Path('/repo'), repo='repo',
                plan_id='G-044', branch='loop/G-044', worktree=Path('/owned'),
                remote_oid='a' * 40, git=git, before_head='before',
                recorded_oid='a' * 40,
            )
            arguments.update(changes)
            return checkpoint_ownership.owned_remote_oid(**arguments)

        self.assertEqual(owner(), 'a' * 40)
        row['tree_oid'] = 'forged'
        self.assertIsNone(owner())
        row['tree_oid'] = 'tree'
        self.assertIsNone(owner(branch='other'))
        self.assertIsNone(owner(remote_oid='b' * 40))

    def test_rebase_owner_requires_equal_replayed_patch_series(self):
        def git(_repo, *args, **_kwargs):
            outputs = {
                'merge-base': 'old-base\n', 'rev-list': '1\n',
                'range-diff': '1:  aaaaaaa = 1:  bbbbbbb same change\n',
            }
            return SimpleNamespace(stdout=outputs[args[0]], returncode=0)

        self.assertTrue(checkpoint_ownership.rebase_preserves_commits(
            Path('/repo'), 'old-head', 'new-head', 'new-base', git,
        ))

        def changed(_repo, *args, **kwargs):
            result = git(_repo, *args, **kwargs)
            if args[0] == 'range-diff':
                return SimpleNamespace(stdout=result.stdout.replace(' = ', ' ! '), returncode=0)
            return result

        self.assertFalse(checkpoint_ownership.rebase_preserves_commits(
            Path('/repo'), 'old-head', 'new-head', 'new-base', changed,
        ))

    def test_rebase_owner_accepts_only_exact_empty_patch_fast_forward(self):
        def git(_repo, *args, **_kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(stdout='', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='old-head\n', returncode=0)
            if args[0] == 'rev-list':
                return SimpleNamespace(stdout='0\n', returncode=0)
            self.fail('An empty patch series must not invoke range-diff')

        self.assertTrue(checkpoint_ownership.rebase_preserves_commits(
            Path('/repo'), 'old-head', 'new-base', 'new-base', git,
        ))
        self.assertFalse(checkpoint_ownership.rebase_preserves_commits(
            Path('/repo'), 'old-head', 'unexpected-head', 'new-base', git,
        ))

    def test_clean_checkpoint_proves_preexisting_commit_without_marker(self):
        oid = 'a' * 40
        identity = blocked_checkpoint.checkpoint_identity(
            plan_id='G-044', reason_code='review_retry_exhausted',
            evidence='review failed', parent_oid=oid,
            tree_oid='tree', included_paths=(),
        )
        row = {
            'phase': 'published', 'result': 'published',
            'branch': 'loop/G-044', 'worktree': '/owned',
            'accepted_remote_oid': oid, 'published_oid': oid,
            'pending_remote_oid': None, 'commit_identity': identity,
            'tree_oid': 'tree', 'local_oid': oid,
            'reason_code': 'review_retry_exhausted', 'evidence': 'review failed',
        }
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = row

        def git(_repo, *args, **_kwargs):
            if args[0] == 'show':
                return SimpleNamespace(stdout='Existing Worker commit\n', returncode=0)
            if args[0] == 'rev-parse':
                return SimpleNamespace(stdout='tree\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout=f'{oid}\trefs/heads/loop/G-044\n', returncode=0,
                )
            return SimpleNamespace(stdout='', returncode=0)

        self.assertEqual(checkpoint_ownership.owned_remote_oid(
            connection, Path('/repo'), 'repo', 'G-044', 'loop/G-044',
            Path('/owned'), oid, git,
        ), oid)
        row.update(
            identity_reason_code='review_retry_exhausted',
            identity_evidence='review failed',
            reason_code='reclassified', evidence='later failure',
        )
        self.assertEqual(checkpoint_ownership.owned_remote_oid(
            connection, Path('/repo'), 'repo', 'G-044', 'loop/G-044',
            Path('/owned'), oid, git,
        ), oid)
        row['identity_evidence'] = 'forged'
        self.assertIsNone(checkpoint_ownership.owned_remote_oid(
            connection, Path('/repo'), 'repo', 'G-044', 'loop/G-044',
            Path('/owned'), oid, git,
        ))
        row['identity_evidence'] = 'review failed'
        row['commit_identity'] = 'forged'
        self.assertIsNone(checkpoint_ownership.owned_remote_oid(
            connection, Path('/repo'), 'repo', 'G-044', 'loop/G-044',
            Path('/owned'), oid, git,
        ))

    def test_real_rebase_preserves_owned_patch_series(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(path, *arguments, check=True):
                return subprocess.run(
                    ['git', '-C', str(path), *arguments], text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=check,
                )

            git(repo, 'init', '-q')
            git(repo, 'config', 'user.name', 'Unit Test')
            git(repo, 'config', 'user.email', 'unit@example.test')
            (repo / 'app.py').write_text('VALUE = 1\n')
            git(repo, 'add', 'app.py')
            git(repo, 'commit', '-qm', 'base')
            git(repo, 'branch', 'owned')
            (repo / 'notes.md').write_text('new base\n')
            git(repo, 'add', 'notes.md')
            git(repo, 'commit', '-qm', 'advance base')
            target = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'checkout', '-q', 'owned')
            (repo / 'app.py').write_text('VALUE = 2\n')
            git(repo, 'add', 'app.py')
            git(repo, 'commit', '-qm', 'owned patch')
            before = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'rebase', target)
            after = git(repo, 'rev-parse', 'HEAD').stdout.strip()

            self.assertTrue(checkpoint_ownership.rebase_preserves_commits(
                repo, before, after, target, git,
            ))

    def test_pr_rebase_resume_does_not_complete_unmerged_index_without_metadata(self):
        project = {
            'pr_conflict_recovery': 1, 'phase': 'WORKING',
            'last_error': loop_manager.REBASE_IN_PROGRESS_ERROR,
        }
        with (
            mock.patch.object(loop_manager, '_rebase_in_progress_for_branch',
                              return_value=False),
            mock.patch.object(loop_manager, 'git') as git,
            mock.patch.object(loop_manager, 'update_project') as update,
        ):
            git.return_value = SimpleNamespace(
                stdout='100644 ' + 'a' * 40 + ' 1\tapp.py\0', returncode=0,
            )
            result = loop_manager.resume_pr_conflict_rebase(
                object(), Path('/repo'), 'repo', project,
                Path('/owned'), 'loop/G-044',
            )
        self.assertEqual(result, 0)
        self.assertEqual(update.call_args.kwargs['phase'], 'WORKING')
        self.assertNotIn('active_sha', update.call_args.kwargs)

    def test_pr_rebase_resume_retries_unsealed_active_intent_after_lock(self):
        project = {
            'pr_conflict_recovery': 1, 'phase': 'WORKING',
            'last_error': loop_manager.REBASE_IN_PROGRESS_ERROR,
            'pr_conflict_base_oid': 'base', 'pr_conflict_head_oid': 'head',
            'base_branch': 'main',
        }
        plan = {'plan_id': 'G-044', 'pr_conflict_recovery': 1,
                'worker_session_id': 'worker'}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            active = loop_manager.manager_git_journal._slot(
                state, '/repo', 'managed_rebase', '/owned',
            ) / 'active.json'
            active.parent.mkdir(parents=True)
            active.write_text('{}', encoding='utf-8')

            def observed_git(_path, *args, **_kwargs):
                if args[0] == 'ls-files':
                    return SimpleNamespace(stdout='', returncode=0)
                if args[0] == 'rev-parse':
                    return SimpleNamespace(stdout='base\n', returncode=0)
                if args[0] == 'merge-base':
                    return SimpleNamespace(stdout='', returncode=0)
                raise AssertionError(args)

            with (
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch',
                                  return_value=False),
                mock.patch.object(loop_manager, 'git', side_effect=observed_git),
                mock.patch.object(loop_manager, 'active_execplan', return_value=plan),
                mock.patch.object(loop_manager, '_owned_rebase_state_dir',
                                  return_value=state),
                mock.patch.object(loop_manager, '_current_rebase_service',
                                  return_value=SimpleNamespace(require_owned=lambda: (None,
                                      SimpleNamespace(status=SimpleNamespace(value='ready_to_start'))))),
                mock.patch.object(loop_manager, '_finish_owned_direct_rebase',
                                  return_value=False),
                mock.patch.object(loop_manager.execution_checkpoint_state,
                                  'ExecutionCheckpointRepository') as checkpoints,
                mock.patch.object(loop_manager, '_complete_direct_rebase_checkpoint') as complete,
                mock.patch.object(loop_manager, '_journaled_direct_rebase',
                                  return_value=SimpleNamespace(
                                      returncode=1, stdout='temporary failure',
                                  )) as retry,
                mock.patch.object(loop_manager, 'update_project') as update,
            ):
                checkpoints.return_value.get.return_value = SimpleNamespace(
                    operation='rebase',
                )
                result = loop_manager.resume_pr_conflict_rebase(
                    object(), Path('/repo'), '/repo', project,
                    Path('/owned'), 'loop-manager/G-044',
                )
            self.assertEqual(result, loop_manager.PROJECT_STEP_RETRY)
            retry.assert_called_once()
            complete.assert_not_called()
            update.assert_not_called()

    def test_pr_rebase_resume_blocks_when_active_checkpoint_has_no_journal(self):
        project = {
            'pr_conflict_recovery': 1, 'phase': 'WORKING',
            'last_error': loop_manager.REBASE_IN_PROGRESS_ERROR,
            'pr_conflict_base_oid': 'base', 'pr_conflict_head_oid': 'head',
            'verify_result': '',
        }
        plan = {'plan_id': 'G-044', 'pr_conflict_recovery': 1}
        with (
            mock.patch.object(loop_manager, '_rebase_in_progress_for_branch',
                              return_value=False),
            mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(
                stdout='', returncode=0,
            )),
            mock.patch.object(loop_manager, 'active_execplan', return_value=plan),
            mock.patch.object(loop_manager, '_owned_rebase_state_dir',
                              return_value=None),
            mock.patch.object(loop_manager.execution_checkpoint_state,
                              'ExecutionCheckpointRepository') as checkpoints,
            mock.patch.object(loop_manager, 'block_active_execplan') as block,
            mock.patch.object(loop_manager, '_complete_direct_rebase_checkpoint') as complete,
        ):
            checkpoints.return_value.get.return_value = SimpleNamespace(
                operation='rebase',
            )
            result = loop_manager.resume_pr_conflict_rebase(
                object(), Path('/repo'), '/repo', project,
                Path('/owned'), 'loop-manager/G-044',
            )
        self.assertEqual(result, 1)
        block.assert_called_once()
        complete.assert_not_called()

    def test_direct_rebase_completion_allows_only_preserved_worktree_delta(self):
        sealed = {
            'head': 'rebased', 'branch': 'loop-manager/G-044',
            'ref': ['rebased', ''], 'registration': ['owned'],
            'operations': {}, 'metadata': None,
            'index': {'code.py': ['old']}, 'dirty': {},
        }
        restored = {**sealed, 'index': {'code.py': ['restored']},
                    'dirty': {'code.py': [['staged'], 'saved']}}
        matches = loop_manager._direct_rebase_result_matches_observation
        self.assertFalse(matches(sealed, restored, preserved_worktree=False))
        self.assertTrue(matches(sealed, restored, preserved_worktree=True))
        self.assertFalse(matches(
            sealed, {**restored, 'head': 'foreign'}, preserved_worktree=True,
        ))
        self.assertFalse(matches(
            sealed, {**restored, 'metadata': {'onto': 'other'}},
            preserved_worktree=True,
        ))

    def test_blocked_resume_head_keeps_active_rebase_identity(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(
            connection,
        )
        checkpoint.record(
            'repo', 'G-044', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid='published',
            snapshot={'rebase_run_id': 'run-1', 'blocked_resume': {
                'before_snapshot': 'saved-worktree',
            }},
        )
        loop_manager._record_blocked_resume_head(
            connection, 'repo', 'G-044', 'published', 'before', 'base', 'after',
        )
        saved = checkpoint.get('repo', 'G-044')
        self.assertEqual(saved.operation, 'rebase')
        self.assertEqual(saved.expected_head, 'before')
        self.assertEqual(saved.snapshot['rebase_run_id'], 'run-1')
        self.assertEqual(saved.snapshot['blocked_resume']['after_head'], 'after')
        self.assertEqual(saved.snapshot['blocked_resume']['before_snapshot'],
                         'saved-worktree')

    def test_unmerged_index_never_reaches_baseline_write_tree(self):
        before = loop_manager.GitSnapshot(
            head='head', branch='loop/G-044', index={},
            dirty={'app.py': (('modified',), 'content')}, refs={},
            worktrees={}, config={}, operations={},
        )
        with mock.patch.object(loop_manager, 'git') as git:
            git.return_value = SimpleNamespace(stdout='100644 ' + 'a' * 40 + ' 1\tapp.py\0')
            baseline = loop_manager._capture_agent_baseline(
                'repo', Path('/owned'), before,
            )
        self.assertIsNone(baseline)
        self.assertEqual(git.call_args.args[1:4], ('ls-files', '-u', '-z'))

    def test_stale_markers_replay_only_validation(self):
        for changes in (
            {'active_sha': 'old-base'},
            {'validated_base_sha': 'old-base'},
            {'validated_base_sha': None},
            {'rebase_intent': True, 'rebase_before_head': 'old-head'},
        ):
            with self.subTest(changes=changes):
                decision = assess(snapshot(**changes))
                self.assertEqual(decision.action, CheckpointAction.CORRECT)
                self.assertEqual(decision.phase, 'VERIFYING')
                self.assertTrue(decision.invalidate_validation)
                self.assertEqual(decision.active_sha, 'new-base')

    def test_legacy_review_result_without_revision_token_reaches_revision_gate(self):
        decision = assess(snapshot(
            phase='REVIEWING', validated_base_sha=None,
            verify_token=None, review_token=None,
        ))
        self.assertEqual(decision.action, CheckpointAction.CONTINUE)

    def test_autostash_unmerged_index_wins_over_successful_head_ancestry(self):
        actual = snapshot(
            phase='IMPLEMENTING', rebase_intent=True, stash_oid='stash-object',
            unmerged_stages=(('app.py', 1, 'a' * 40),
                             ('app.py', 2, 'b' * 40),
                             ('app.py', 3, 'c' * 40)),
        )
        decision = assess(actual)
        self.assertEqual(decision.action, CheckpointAction.CONFLICT)
        self.assertEqual(decision.phase, 'IMPLEMENTING')

    def test_autostash_without_identity_requires_user_before_worker_correction(self):
        actual = snapshot(rebase_intent=True,
                          unmerged_stages=(('app.py', 1, 'blob'),))
        self.assertEqual(assess(actual).action, CheckpointAction.USER_ACTION)
        self.assertEqual(assess(actual).reason, 'autostash_identity_unavailable')
        self.assertEqual(assess(replace(actual, git_operation='rebase')).action,
                         CheckpointAction.CONFLICT)

    def test_unowned_or_external_remote_state_requires_user_action(self):
        for actual in (
            snapshot(worktree_owned=False),
            snapshot(local_branch='other'),
            snapshot(remote_oid='unknown'),
            snapshot(unmerged_stages=(('x', 1, 'a' * 40),)),
        ):
            with self.subTest(actual=actual):
                self.assertEqual(assess(actual).action, CheckpointAction.USER_ACTION)

    def test_unobserved_local_commit_requires_intent(self):
        unobserved = snapshot(recorded_local_head='previous-head')
        self.assertEqual(assess(unobserved).action,
                         CheckpointAction.USER_ACTION)
        committed = replace(unobserved, pending_operation='commit')
        self.assertEqual(assess(committed).action,
                         CheckpointAction.CORRECT)
        self.assertEqual(assess(replace(
            committed, base_is_ancestor=False,
        )).action, CheckpointAction.REBASE)

    def test_other_git_operation_pauses_without_phase_correction(self):
        actual = snapshot(
            git_operation='MERGE_HEAD',
            git_operation_markers=(('MERGE_HEAD', 'marker-hash'),),
        )
        self.assertEqual(assess(actual).action, CheckpointAction.PAUSE)

    def test_completed_rebase_auxiliary_markers_do_not_pause_worker_retry(self):
        def git(_worktree, *args, **_kwargs):
            if args[0] == 'symbolic-ref':
                return SimpleNamespace(stdout='loop/G-044\n', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=0)
            if args[0] in {'ls-files', 'diff'}:
                return SimpleNamespace(stdout='', returncode=0)
            return SimpleNamespace(stdout='head\n', returncode=0)

        row = dict(phase='WORKING', active_sha='base',
                   validated_base_sha=None, verify_token=None,
                   review_token=None, archive_token=None,
                   publication_token=None, publication_remote_oid=None,
                   pr_head_oid=None)
        arguments = dict(
            plan_id='G-044', row=row, definition_revision='definition',
            base_oid='base', branch='loop/G-044', worktree=Path('/owned'),
            owned=True, git=git, rebase_in_progress=lambda *_: False,
            remote_oid=None, pull_request=None, rebase_intent=False,
            rebase_before_head=None, rebase_target_oid=None, stash_oid=None,
        )
        completed = observe(
            **arguments,
            operation_markers={'MERGE_MSG': 'message', 'MERGE_MODE': 'mode',
                               'REBASE_HEAD': 'commit'},
        )
        self.assertIsNone(completed.git_operation)
        self.assertEqual(len(completed.git_operation_markers), 3)
        self.assertEqual(assess(completed).action, CheckpointAction.CONTINUE)

        in_progress = observe(
            **arguments,
            operation_markers={'MERGE_MSG': 'message', 'MERGE_HEAD': 'merge'},
        )
        self.assertEqual(in_progress.git_operation, 'MERGE_HEAD')
        self.assertEqual(assess(in_progress).action, CheckpointAction.PAUSE)

    def test_observer_preserves_each_conflict_stage_blob(self):
        def git(_worktree, *args, **_kwargs):
            if args[:2] == ('ls-files', '-u'):
                output = ''.join(f'100644 {oid} {stage}\tapp.py\0'
                                 for stage, oid in ((1, 'a' * 40),
                                                    (2, 'b' * 40),
                                                    (3, 'c' * 40)))
            elif args[:2] == ('diff', '--cached'):
                output = 'app.py\0'
            elif args[0] == 'symbolic-ref':
                output = 'loop/G-044\n'
            else:
                output = 'head\n'
            return SimpleNamespace(stdout=output, returncode=0)

        row = dict(phase='WORKING', active_sha='old-base',
                   validated_base_sha=None, verify_token=None,
                   review_token=None, archive_token='archive',
                   publication_token=None, publication_remote_oid=None,
                   pr_head_oid=None)
        actual = observe(
            plan_id='G-044', row=row, definition_revision='definition',
            base_oid='new-base', branch='loop/G-044', worktree=Path('/owned'),
            owned=True, git=git, rebase_in_progress=lambda *_: False,
            remote_oid=None, pull_request=None, rebase_intent=True,
            rebase_before_head='before', rebase_target_oid='new-base',
            stash_oid='stash-object',
        )
        self.assertEqual(actual.unmerged_stages, (
            ('app.py', 1, 'a' * 40), ('app.py', 2, 'b' * 40),
            ('app.py', 3, 'c' * 40),
        ))
        self.assertEqual(actual.staged_paths, ('app.py',))
        self.assertEqual(assess(actual).action, CheckpointAction.CONFLICT)

class CurrentRebaseBoundaryTests(unittest.TestCase):
    def test_original_owned_head_can_start_without_receipt_or_original_commit_list(self):
        from dataclasses import replace
        from tests.test_managed_rebase_validation import expectation, observation, decision_for
        from managed_rebase_validation import RebaseStatus
        expected = expectation()
        original = observation(expected, head='before', branch_ref=('before', ''),
            registration=('HEAD=before', 'branch=refs/heads/loop-manager/G-108'),
            base_is_ancestor=False)
        self.assertEqual(decision_for(expected, original).status, RebaseStatus.START)
        self.assertEqual(decision_for(expected, replace(original,
            remote=replace(expected.remote, oid='external'))).status, RebaseStatus.REJECTED)

    def test_autostash_restoration_waits_and_other_operations_are_rejected(self):
        from dataclasses import replace
        from tests.test_managed_rebase_validation import expectation, observation, decision_for
        from managed_rebase_validation import RebaseStatus, IndexEntry
        expected = expectation(autostash_oid='owned-stash')
        pending = observation(expected, index_clean=False,
            index=(IndexEntry('app.py', '100644', 'conflict', 2, 'M', 0x2000),))
        self.assertEqual(decision_for(expected, pending).status, RebaseStatus.WAITING)
        self.assertEqual(decision_for(expected, replace(pending,
            operations=(('MERGE_HEAD', 'unowned merge'),))).status, RebaseStatus.REJECTED)
        self.assertEqual(decision_for(replace(expected, autostash_oid=None), pending).status,
                         RebaseStatus.REJECTED)
