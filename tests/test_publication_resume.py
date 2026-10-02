"""Push and PR boundary decisions without external commands."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from loop_manager_internal.execution.publication_resume import (
    PublicationNext, PublicationObservation, PublicationResume,
    assess_publication,
)


class PublicationResumeTests(unittest.TestCase):
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
