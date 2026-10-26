"""Push and PR boundary decisions without external commands."""

from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from loop_manager_internal.git import publication_service
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason

from loop_manager_internal.execution.publication_resume import (
    PublicationNext, PublicationObservation, PublicationResume,
    assess_publication,
)


class PublicationResumeTests(unittest.TestCase):
    def test_stale_publication_oid_requests_reconciliation_before_any_write(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        plan = dict(plan_id='G-044', publication_remote_oid='old')
        effects = SimpleNamespace(_owned_publication_plan=mock.Mock(return_value=plan),
            git=mock.Mock(return_value=SimpleNamespace(stdout='local')), update_project=mock.Mock())
        with self.assertRaises(RecoveryFailure) as failure:
            publication_service.record_remote_publication_branch(effects, connection,
                Path('/repo'), Path('/state'), 'repo', 'loop/G-044', Path('/owned'), 'remote')
        self.assertEqual(failure.exception.reason, RecoveryReason.CHECKPOINT_STALE)
        effects.update_project.assert_not_called()

    def test_push_success_before_database_failure_is_not_pushed_twice(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        old, new, branch = 'a' * 40, 'b' * 40, 'loop/G-044'
        plan = dict(plan_id='G-044', publication_remote_oid=old, pr_url=None)
        remote, pushes = [old], []
        def git(path, *args, **kwargs):
            if args[0] == 'push':
                pushes.append(args)
                remote[0] = new
            return SimpleNamespace(returncode=0, stdout=(
                f'{remote[0]}\trefs/heads/{branch}\n' if args[0] == 'ls-remote' else new))
        updater = mock.Mock(side_effect=[OSError('DB write failed'), None])
        effects = SimpleNamespace(_owned_publication_plan=mock.Mock(return_value=plan),
            _recorded_base_fetch=mock.Mock(), active_execplan=mock.Mock(return_value=plan),
            git=git, pr_for_branch=mock.Mock(return_value=None),
            remote_branch_oid=lambda *args: remote[0], update_project=updater)
        recorder = SimpleNamespace(_owned_publication_plan=effects._owned_publication_plan,
            git=git, update_project=updater)
        effects.record_remote_publication_branch = lambda *args: (
            publication_service.record_remote_publication_branch(recorder, *args))
        args = (effects, connection, Path('/repo'), Path('/state'), 'repo', branch, Path('/owned'), new)
        with self.assertRaisesRegex(OSError, 'DB write failed'):
            publication_service.push_managed_publication_branch(*args)
        self.assertEqual(publication_service.push_managed_publication_branch(*args), new)
        self.assertEqual(len(pushes), 1)
        self.assertIn(f'--force-with-lease=refs/heads/{branch}:{old}', pushes[0])
        self.assertEqual(updater.call_count, 2)

    def test_missing_pr_head_cannot_prove_safe_attachment(self):
        observation = PublicationObservation(
            'loop/G-044', 'head', 'head', 'head', {'url': 'existing'},
        )
        self.assertEqual(assess_publication(observation), PublicationNext.USER_ACTION)

    def test_existing_pr_on_old_head_cannot_attach_after_local_rebase(self):
        for state in ('OPEN', 'CLOSED', 'MERGED'):
            with self.subTest(state=state):
                observation = PublicationObservation(
                    'loop/G-044', 'rebased', 'old', 'old',
                    {'state': state, 'headRefName': 'loop/G-044',
                     'headRefOid': 'old'},
                )
                self.assertEqual(assess_publication(observation),
                                 PublicationNext.USER_ACTION)

    def test_remote_equal_local_skips_push_and_existing_pr_skips_create(self):
        observed = PublicationObservation('loop/G-044', 'head', 'head', None, None)
        self.assertEqual(assess_publication(observed), PublicationNext.CREATE_PR)
        self.assertEqual(assess_publication(replace(
            observed, pull_request={
                'state': 'OPEN', 'headRefName': 'loop/G-044',
                'headRefOid': 'head', 'url': 'https://example/pr/1',
            },
        )), PublicationNext.ATTACH_PR)

    def test_open_closed_and_merged_prs_are_reused_by_head(self):
        base = PublicationObservation('loop/G-044', 'head', 'head', 'head', None)
        for state in ('OPEN', 'CLOSED', 'MERGED'):
            with self.subTest(state=state):
                observed = replace(base, pull_request={
                    'state': state, 'headRefName': 'loop/G-044',
                    'headRefOid': 'head', 'url': 'https://example/pr/1',
                })
                self.assertEqual(assess_publication(observed),
                                 PublicationNext.ATTACH_PR)

    def test_only_recorded_remote_oid_allows_existing_branch_push(self):
        observed = PublicationObservation('loop/G-044', 'rebased', 'old', 'old', None)
        self.assertEqual(assess_publication(observed), PublicationNext.PUSH)
        self.assertEqual(assess_publication(replace(
            observed, recorded_remote_oid='other'
        )), PublicationNext.USER_ACTION)

    def test_resume_requeries_pr_and_remote_for_each_boundary(self):
        calls = []
        remote = ['head', 'head']
        prs = [None, {'state': 'OPEN', 'headRefName': 'loop/G-044',
                      'headRefOid': 'head', 'url': 'https://example/pr/1'}]
        service = PublicationResume(
            lambda *_: calls.append('pr') or prs.pop(0),
            lambda *_: calls.append('remote') or remote.pop(0),
            lambda *_: SimpleNamespace(stdout='head\n'),
        )
        first = service.observe('/repo', '/owned', 'loop/G-044', None)
        second = service.observe('/repo', '/owned', 'loop/G-044', None)
        self.assertEqual(assess_publication(first), PublicationNext.CREATE_PR)
        self.assertEqual(assess_publication(second), PublicationNext.ATTACH_PR)
        self.assertEqual(calls, ['remote', 'pr', 'remote', 'pr'])
