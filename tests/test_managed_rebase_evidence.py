"""Focused phase and stage checks for managed rebase proof."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import manager_git_journal as journal

from managed_rebase_evidence import (
    ReplayEvidenceError, stage_matches, verify_replay,
)


class ReplayVerdictTests(unittest.TestCase):
    def setUp(self):
        self.sources = ['source-1', 'source-2', 'source-3']
        self.facts = {oid: {'oid': oid, 'parent': 'old', 'tree': oid}
                      for oid in self.sources}
        self.replayed = [
            {'oid': 'new-1', 'parent': 'base', 'tree': 'first-tree'},
            {'oid': 'new-2', 'parent': 'new-1', 'tree': 'resolved-tree'},
            {'oid': 'new-3', 'parent': 'new-2', 'tree': 'later-tree'},
        ]
        self.correction = {'source-2': {
            'source': 'source-2', 'parent': 'new-1', 'tree': 'resolved-tree',
            'intent_id': 'continue-1', 'turn_id': 'worker-turn-1',
        }}

    def verdict(self, *, replayed=None, corrections=None, skips=None,
                sources=None, head='new-3', same_patch=None, allow_prefix=False):
        return verify_replay(
            sources=self.sources if sources is None else sources,
            replayed=self.replayed if replayed is None else replayed,
            target='base', head=head,
            corrections=self.correction if corrections is None else corrections,
            skips={} if skips is None else skips,
            source_facts=self.facts,
            same_patch=same_patch or (lambda source, actual: (
                source['oid'], actual['oid']) in {
                    ('source-1', 'new-1'), ('source-3', 'new-3'),
                }
            ),
            allow_prefix=allow_prefix,
        )

    def test_middle_resolution_is_checked_at_its_commit_not_final_tree(self):
        mapping = self.verdict()
        self.assertEqual([item['outcome'] for item in mapping],
                         ['patch', 'resolved', 'patch'])
        self.assertEqual(mapping[1]['tree'], 'resolved-tree')
        self.assertEqual(mapping[-1]['tree'], 'later-tree')

    def test_multiple_resolutions_have_separate_trees_and_turns(self):
        corrections = {**self.correction, 'source-3': {
            'source': 'source-3', 'parent': 'new-2', 'tree': 'later-tree',
            'intent_id': 'continue-2', 'turn_id': 'worker-turn-2',
        }}
        self.assertEqual(self.verdict(corrections=corrections)[-1]['outcome'],
                         'resolved')
        for altered in (
            {**corrections, 'source-3': {**corrections['source-3'],
                                        'turn_id': ''}},
            {**corrections, 'source-3': {**corrections['source-3'],
                                        'tree': 'other'}},
        ):
            with self.subTest(altered=altered), self.assertRaises(ReplayEvidenceError):
                self.verdict(corrections=altered)

    def test_missing_added_reordered_or_altered_replay_is_rejected(self):
        cases = (
            self.replayed[:2],
            self.replayed + [{'oid': 'extra', 'parent': 'new-3', 'tree': 'extra'}],
            [self.replayed[1], self.replayed[0], self.replayed[2]],
            [self.replayed[0], {**self.replayed[1], 'tree': 'tampered'},
             self.replayed[2]],
        )
        for commits in cases:
            with self.subTest(commits=commits), self.assertRaises(ReplayEvidenceError):
                self.verdict(replayed=commits)
        with self.assertRaises(ReplayEvidenceError):
            self.verdict(same_patch=lambda *_: False)

    def test_skip_requires_source_bound_receipt(self):
        replayed = [self.replayed[0],
                    {'oid': 'new-3', 'parent': 'new-1', 'tree': 'later-tree'}]
        receipt = {'source': 'source-2', 'parent': 'new-1',
                   'reason': 'worker-approved', 'intent_id': 'skip-1'}
        self.assertEqual(self.verdict(
            replayed=replayed, corrections={}, skips={'source-2': receipt},
        )[1]['outcome'], 'skip')
        for wrong in ({**receipt, 'parent': 'base'},
                      {**receipt, 'intent_id': ''},
                      {**receipt, 'reason': 'unknown'}):
            with self.subTest(receipt=wrong), self.assertRaises(ReplayEvidenceError):
                self.verdict(replayed=replayed, corrections={},
                             skips={'source-2': wrong})

    def test_conflict_prefix_requires_exact_parent_chain(self):
        self.assertEqual(len(self.verdict(
            replayed=self.replayed[:1], head='new-1', allow_prefix=True,
        )), 1)
        with self.assertRaises(ReplayEvidenceError):
            self.verdict(replayed=self.replayed[:1], head='new-2',
                         allow_prefix=True)


class StageVerdictTests(unittest.TestCase):
    def test_stage_rejects_other_path_or_index_flag_change(self):
        before = {'app.py': ['H 100644 ' + 'a' * 40 + ' 1'],
                  'other.py': ['H 100644 ' + 'b' * 40 + ' 0']}
        staged = {'app.py': ['H 100644 ' + 'c' * 40 + ' 0'],
                  'other.py': before['other.py']}
        options = dict(before_index=before, allowed_paths=['app.py'],
                       accepted_files={'app.py': 'accepted'},
                       staged_dirty={'app.py': [['staged'], 'accepted']})
        self.assertTrue(stage_matches(staged_index=staged, **options))
        for changed in (
            {**staged, 'other.py': ['H 100644 ' + 'd' * 40 + ' 0']},
            {**staged, 'app.py': ['S 100644 ' + 'c' * 40 + ' 0']},
            {**staged, 'app.py': ['H 100755 ' + 'c' * 40 + ' 2']},
        ):
            with self.subTest(index=changed):
                self.assertFalse(stage_matches(staged_index=changed, **options))
        self.assertFalse(stage_matches(staged_index=staged, **{
            **options, 'staged_dirty': {'app.py': [['staged'], 'changed']},
        }))


class JournalReplayProofTests(unittest.TestCase):
    def test_legacy_staged_tree_recovers_only_unique_resolution_commit(self):
        base, target = '0' * 40, 'd' * 40
        source = ['1' * 40, '2' * 40]
        replayed = ['a' * 40, 'b' * 40]
        parents = {source[0]: base, source[1]: source[0],
                   replayed[0]: target, replayed[1]: replayed[0]}
        trees = {replayed[0]: 'e' * 40, replayed[1]: 'f' * 40}

        def git(_repo, *arguments, **_kwargs):
            if arguments[0] == 'merge-base':
                return SimpleNamespace(stdout=base + '\n', returncode=0)
            if arguments[:2] == ('rev-list', '--reverse'):
                commits = source if arguments[2] == f'{base}..{source[-1]}' else replayed
                return SimpleNamespace(stdout='\n'.join(commits) + '\n', returncode=0)
            if arguments[:2] == ('rev-list', '--parents'):
                oid = arguments[-1]
                return SimpleNamespace(stdout=f'{oid} {parents[oid]}\n', returncode=0)
            if arguments[0] == 'rev-parse':
                oid = arguments[1][:40]
                return SimpleNamespace(stdout=trees.get(oid, oid) + '\n', returncode=0)
            if arguments[0] == 'range-diff':
                return SimpleNamespace(stdout=f'1: {source[1]} = 1: {replayed[1]} same\n',
                                       returncode=0)
            raise AssertionError(arguments)

        with TemporaryDirectory() as directory:
            state = Path(directory)
            expected = {'branch': 'loop-manager/G-099', 'before_head': source[-1],
                        'target_base_oid': target, 'remote_oid': None}
            journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before={'head': source[-1], 'metadata': None}, expected=expected,
            )
            journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: {'head': target, 'metadata': {'onto': target}},
                validate=lambda *_: {},
            )
            journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before={'head': target, 'metadata': {'onto': target},
                        'dirty': {'app.py': [['staged'], 'approved']}},
                expected={**expected, 'action': 'continue',
                          'staged_tree_oid': trees[replayed[0]],
                          'accepted_files': {'app.py': 'approved'}},
            )
            active = journal._read(journal._slot(
                state, '/repo', 'managed_rebase', '/worktree',
            ) / 'active.json')
            result = {'head': replayed[-1], 'metadata': None}
            proof = journal.legacy_rebase_replay_proof(git, state, active, result)
            self.assertEqual([item['outcome'] for item in proof], ['resolved', 'patch'])
            self.assertNotEqual(proof[0]['tree'], proof[1]['tree'])
            trees[replayed[0]] = '9' * 40
            with self.assertRaises(journal.MutationEvidenceError):
                journal.legacy_rebase_replay_proof(git, state, active, result)

    def test_stored_result_rechecks_its_phase_proof(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            candidates = {'refs': {}, 'worktrees': {}, 'selected': {'head': 'b' * 40}}
            proof = [{'source': 'a' * 40, 'replayed': 'b' * 40}]
            observed = {
                'head': 'b' * 40, 'index': {}, 'dirty': {},
                'registration': ['HEAD=' + 'b' * 40], 'index_clean': True,
                'metadata': None,
            }
            journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before={'head': 'a' * 40},
                expected={'branch': 'loop-manager/G-099', 'remote_oid': None,
                          'evidence_version': 2},
            )
            journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: observed,
                validate=lambda *_: candidates,
                replay_proof=lambda *_: proof,
            )
            with (mock.patch.object(journal, 'rebase_replay_proof', return_value=proof),
                  mock.patch.object(journal, 'verified_rebase_result',
                                    return_value=candidates)):
                self.assertEqual(len(journal.verified_managed_transitions(
                    state, '/repo', 'plan:G-099', git=lambda *_: None,
                )), 1)
            with (mock.patch.object(journal, 'rebase_replay_proof', return_value=[]),
                  mock.patch.object(journal, 'verified_rebase_result',
                                    return_value=candidates)):
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.verified_managed_transitions(
                        state, '/repo', 'plan:G-099', git=lambda *_: None,
                    )

    def test_unchanged_rebase_requires_its_own_successful_invocation(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            before = {'head': 'a' * 40, 'metadata': None}
            identity = journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before=before,
                expected={'evidence_version': 2, 'run_id': 'same-run'},
            )
            validate = lambda *_: {'refs': {}}
            observe = lambda: before.copy()
            self.assertFalse(journal.complete_rebase(
                state, repo='/repo', target='/worktree', observe=observe,
                validate=validate, allow_equal=True,
            ))
            journal.record_rebase_invocation(
                state, '/repo', '/worktree', identity, returncode=0,
            )
            self.assertTrue(journal.complete_rebase(
                state, repo='/repo', target='/worktree', observe=observe,
                validate=validate, allow_equal=True,
                replay_proof=lambda *_: [],
            ))
            result = journal._read(journal._slot(
                state, '/repo', 'managed_rebase', '/worktree',
            ) / f'{identity}.result.json')
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['replay_proof'], [])

    def test_middle_correction_receipt_maps_to_its_commit_before_later_change(self):
        base, target = '0' * 40, 'd' * 40
        sources = ['1' * 40, '2' * 40, '3' * 40]
        replayed = ['a' * 40, 'b' * 40, 'c' * 40]
        parents = dict(zip(sources, [base, *sources[:2]]))
        parents.update(zip(replayed, [target, *replayed[:2]]))
        trees = {oid: oid for oid in parents}
        trees[replayed[1]] = 'e' * 40
        trees[replayed[2]] = 'f' * 40

        def git(_repo, *arguments, **_kwargs):
            if arguments[0] == 'merge-base':
                return SimpleNamespace(stdout=base + '\n', returncode=0)
            if arguments[:2] == ('rev-list', '--reverse'):
                commits = sources if arguments[2] == f'{base}..{sources[-1]}' else replayed
                return SimpleNamespace(stdout='\n'.join(commits) + '\n', returncode=0)
            if arguments[:2] == ('rev-list', '--parents'):
                oid = arguments[-1]
                return SimpleNamespace(stdout=f'{oid} {parents[oid]}\n', returncode=0)
            if arguments[0] == 'rev-parse':
                return SimpleNamespace(stdout=trees[arguments[1][:40]] + '\n', returncode=0)
            if arguments[0] == 'range-diff':
                return SimpleNamespace(stdout=f'1: {sources[0]} = 1: {replayed[0]} same\n',
                                       returncode=0)
            raise AssertionError(arguments)

        with TemporaryDirectory() as directory:
            state = Path(directory)
            expected = dict(branch='loop-manager/G-099', before_head=sources[-1],
                            target_base_oid=target, remote_oid=None,
                            evidence_version=2, run_id='one-run', source_base=base,
                            source_commits=sources, affected_paths=['app.py'],
                            checkpoint_generation=1)
            initial = {'head': sources[-1], 'metadata': None}
            journal.begin_rebase(state, owner='plan:G-099', repo='/repo',
                                 target='/worktree', before=initial, expected=expected)
            journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: {'head': replayed[0], 'metadata': {'onto': target}},
                validate=lambda *_: {},
            )
            pre = {'app.py': ['H 100644 ' + '4' * 40 + ' 1']}
            staged = {'app.py': ['H 100644 ' + '5' * 40 + ' 0']}
            before = {'head': replayed[0], 'metadata': {'onto': target},
                      'index': staged,
                      'dirty': {'app.py': [['staged'], 'approved']}}
            continuation = {**expected, 'action': 'continue',
                            'checkpoint_generation': 3,
                            'source_commit': sources[1],
                            'staged_tree_oid': trees[replayed[1]],
                            'stage_before_index': pre, 'staged_index': staged,
                            'accepted_files': {'app.py': 'approved'},
                            'correction_turn': 'turn-1'}
            identity = journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before=before, expected=continuation,
            )
            active = journal._read(journal._slot(
                state, '/repo', 'managed_rebase', '/worktree',
            ) / 'active.json')
            self.assertEqual(active['id'], identity)
            result = {'head': replayed[-1], 'metadata': None}
            mapping = journal.rebase_replay_proof(git, state, active, result)
            self.assertEqual([item['outcome'] for item in mapping],
                             ['patch', 'resolved', 'patch'])
            self.assertEqual(mapping[1]['tree'], trees[replayed[1]])
            self.assertNotEqual(mapping[2]['tree'], mapping[1]['tree'])
            trees[replayed[1]] = '9' * 40
            with self.assertRaises(journal.MutationEvidenceError):
                journal.rebase_replay_proof(git, state, active, result)


if __name__ == '__main__':
    unittest.main()
