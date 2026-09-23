"""Deterministic conflict identity and action tests."""

from unittest import TestCase
from unittest import mock
from types import SimpleNamespace

from rebase_correction import (
    ConflictObservation, CorrectionAction, CorrectionSource, execute_git_step,
    next_action,
    parse_unmerged,
)


class RebaseCorrectionTests(TestCase):
    def observation(self, *, stages=None, files=None, position='commit-a',
                    source=CorrectionSource.REBASE, stash=None):
        return ConflictObservation(
            'definition', 'base', 'before', stages or (('app.py', 1, 'blob-a'),),
            files or (('app.py', 'content-a'),), 'staged-a', position, source, stash,
        )

    def test_fingerprint_uses_stage_blobs_and_progress_uses_content_and_position(self):
        first = self.observation()
        content = self.observation(files=(('app.py', 'resolved'),))
        position = self.observation(position='commit-b')
        stage = self.observation(stages=(('app.py', 2, 'blob-b'),))
        self.assertEqual(first.fingerprint, content.fingerprint)
        self.assertEqual(first.fingerprint, position.fingerprint)
        self.assertNotEqual(first.progress, content.progress)
        self.assertNotEqual(first.progress, position.progress)
        self.assertNotEqual(first.fingerprint, stage.fingerprint)
        staged = ConflictObservation(
            first.plan_revision, first.target_base, first.before_head,
            first.stages, first.worktree_files, 'new staged blob',
            first.rebase_position, first.source, first.stash_oid,
        )
        self.assertEqual(first.fingerprint, staged.fingerprint)
        self.assertNotEqual(first.progress, staged.progress)

    def test_autostash_completion_never_continues_rebase(self):
        observation = self.observation(stages=(), source=CorrectionSource.AUTOSTASH,
                                       stash='stash-oid')
        observation = ConflictObservation(
            observation.plan_revision, observation.target_base,
            observation.before_head, (), observation.worktree_files,
            observation.staged_diff, None, observation.source,
            observation.stash_oid,
        )
        self.assertEqual(next_action(observation, worker_result=True, staged=True),
                         CorrectionAction.VERIFY)
        self.assertEqual(next_action(self.observation(source=CorrectionSource.AUTOSTASH),
                                     worker_result=False, staged=False),
                         CorrectionAction.USER_ACTION)

    def test_index_parser_keeps_conflict_paths_and_stage_oids(self):
        parsed = parse_unmerged('100644 blob-a 1\tapp.py\0'
                                '100644 blob-b 2\tapp.py\0')
        self.assertEqual(parsed, (('app.py', 1, 'blob-a'), ('app.py', 2, 'blob-b')))

    def test_stage_and_continue_actions_use_narrow_git_operations(self):
        git = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='done'))
        stage = execute_git_step(CorrectionAction.STAGE, git=git, worktree='/worktree',
                                 paths=('app.py', 'docs/PRODUCT.md'))
        self.assertEqual(stage.action, CorrectionAction.STAGE)
        git.assert_called_with('/worktree', 'add', '-A', '--', 'app.py',
                               'docs/PRODUCT.md', check=False)
        resolved = ConflictObservation(
            'definition', 'base', 'before', (), (), '', 'position',
            CorrectionSource.REBASE,
        )
        self.assertEqual(next_action(resolved, worker_result=True, staged=True),
                         CorrectionAction.CONTINUE)
        self.assertEqual(next_action(resolved, worker_result=True, staged=False),
                         CorrectionAction.SKIP)
