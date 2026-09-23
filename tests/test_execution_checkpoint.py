"""Pure checkpoint decisions and index observation regressions."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager

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
            phase='IMPLEMENTING', rebase_intent=True,
            unmerged_stages=(('app.py', 1, 'a' * 40),
                             ('app.py', 2, 'b' * 40),
                             ('app.py', 3, 'c' * 40)),
        )
        decision = assess(actual)
        self.assertEqual(decision.action, CheckpointAction.CONFLICT)
        self.assertEqual(decision.phase, 'IMPLEMENTING')

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
            stash_oid=None,
        )
        self.assertEqual(actual.unmerged_stages, (
            ('app.py', 1, 'a' * 40), ('app.py', 2, 'b' * 40),
            ('app.py', 3, 'c' * 40),
        ))
        self.assertEqual(actual.staged_paths, ('app.py',))
        self.assertEqual(assess(actual).action, CheckpointAction.CONFLICT)
