"""Focused phase and stage checks for managed rebase proof."""

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.git import manager_git_journal as journal
import loop_manager

from loop_manager_internal.git.managed_rebase_evidence import (
    ReplayEvidenceError, reconcile_frozen_sources, source_commits, stage_index_matches,
    stage_matches, stage_modes_match, verify_replay,
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
    def test_modes_require_exact_approved_types_and_executable_bits(self):
        files = {'app.py': 'approved'}
        for mode in ('100644', '100755', '120000', '160000'):
            index = {'app.py': [f'H {mode} ' + 'a' * 40 + ' 0']}
            with self.subTest(mode=mode):
                self.assertTrue(stage_modes_match(files, {'app.py': mode}, index))
                other = '100644' if mode != '100644' else '100755'
                self.assertFalse(stage_modes_match(files, {'app.py': other}, index))
                self.assertFalse(stage_modes_match(files, None, index))
                self.assertFalse(stage_modes_match(files, {}, index))
                self.assertFalse(stage_modes_match(files, {'app.py': None}, index))
        self.assertTrue(stage_modes_match(files, {'app.py': None}, {}))
        self.assertFalse(stage_modes_match(files, {'app.py': '100644'}, {}))

    def test_empty_skip_still_requires_complete_unchanged_index(self):
        index = {'app.py': ['H 100644 ' + 'a' * 40 + ' 0']}
        self.assertTrue(stage_index_matches(
            before_index=index, staged_index=index, allowed_paths=[]))
        self.assertFalse(stage_index_matches(
            before_index=None, staged_index=index, allowed_paths=[]))
        self.assertFalse(stage_index_matches(
            before_index=index, staged_index={}, allowed_paths=[]))
        self.assertFalse(stage_index_matches(
            before_index=index,
            staged_index={'app.py': ['h 100644 ' + 'a' * 40 + ' 0']},
            allowed_paths=[]))

    def test_stage_rejects_other_path_or_index_flag_change(self):
        before = {'app.py': ['H 100644 ' + 'a' * 40 + ' 1'],
                  'other.py': ['H 100644 ' + 'b' * 40 + ' 0']}
        staged = {'app.py': ['H 100644 ' + 'c' * 40 + ' 0'],
                  'other.py': before['other.py']}
        options = dict(before_index=before, allowed_paths=['app.py'],
                       accepted_files={'app.py': 'accepted'},
                       accepted_modes={'app.py': '100644'},
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
    def test_skip_receipt_rechecks_approved_index(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            expected = dict(evidence_version=2, run_id='run', source_base='old-base',
                            source_commits=['source'], before_head='source',
                            target_base_oid='target', checkpoint_generation=1)
            journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before={'head': 'source', 'metadata': None}, expected=expected)
            journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: {'head': 'target', 'metadata': {'onto': 'target'}},
                validate=lambda *_: {})
            original = {'app.py': ['H 100644 ' + 'a' * 40 + ' 1']}
            staged = {'app.py': ['H 100644 ' + 'b' * 40 + ' 0']}
            journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before=dict(head='target', index=staged, index_clean=True,
                            unmerged=False, dirty={}, metadata={'onto': 'target'}),
                expected={**expected, 'checkpoint_generation': 2, 'action': 'skip',
                          'source_commit': 'source', 'correction_turn': 'turn',
                          'skip_reason': 'worker-approved', 'staged_index': staged,
                          'skip_observed_files': {'app.py': 'approved'},
                          'stage_before_index': original,
                          'accepted_files': {'app.py': 'approved'},
                          'accepted_modes': {'app.py': '100644'}})
            active = journal._read(journal._slot(
                state, '/repo', 'managed_rebase', '/worktree') / 'active.json')
            with (mock.patch.object(journal.replay_evidence, 'source_commits',
                                    return_value=('old-base', ['source'])),
                  mock.patch.object(journal.replay_evidence, 'replay_commits', return_value=[]),
                  mock.patch.object(journal.replay_evidence, 'commit_fact', return_value={})):
                observed = {'head': 'target', 'metadata': None}
                proof = journal.rebase_replay_proof(None, state, active, observed)
                self.assertEqual(proof[0]['outcome'], 'skip')
                for files in (None, {}, {'app.py': 'changed'}, {'app.py': 'missing'}):
                    with self.subTest(files=files):
                        active['expected']['skip_observed_files'] = files
                        with self.assertRaisesRegex(journal.MutationEvidenceError, 'rebase skip'):
                            journal.rebase_replay_proof(None, state, active, observed)
                active['expected']['skip_observed_files'] = {'app.py': 'approved'}
                active['before']['dirty'] = {'app.py': [['unstaged'], 'changed']}
                with self.assertRaisesRegex(journal.MutationEvidenceError, 'rebase skip'):
                    journal.rebase_replay_proof(None, state, active, observed)
                active['before']['dirty'] = {}
                for flag in ('S', 'h'):
                    with self.subTest(flag=flag):
                        changed = {'app.py': [flag + ' 100644 ' + 'b' * 40 + ' 0']}
                        active['before']['index'] = changed
                        active['expected']['staged_index'] = changed
                        with self.assertRaisesRegex(journal.MutationEvidenceError, 'rebase skip'):
                            journal.rebase_replay_proof(None, state, active, observed)
                active['before']['index'] = staged
                active['expected']['staged_index'] = staged
                active['expected'].pop('stage_before_index')
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.rebase_replay_proof(None, state, active, observed)

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
            if arguments[:3] == ('rev-list', '--no-merges', '--reverse'):
                commits = source
                return SimpleNamespace(stdout='\n'.join(commits) + '\n', returncode=0)
            if arguments[:2] == ('rev-list', '--min-parents=2'):
                return SimpleNamespace(stdout='', returncode=0)
            if arguments[:2] == ('rev-list', '--reverse'):
                commits = replayed
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
                          'target_base_oid': 'a' * 40, 'evidence_version': 2},
            )
            journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: observed,
                validate=lambda *_: candidates,
                replay_proof=lambda *_: proof,
            )
            def owned_git(_path, *args, **_kwargs):
                if args[:1] == ('rev-parse',):
                    return SimpleNamespace(returncode=0, stdout=observed['head'] + '\n')
                if args[:2] == ('merge-base', '--is-ancestor'):
                    return SimpleNamespace(returncode=0, stdout='')
                raise AssertionError(f'unexpected Git query: {args}')

            with (mock.patch.object(journal, 'rebase_replay_proof', return_value=proof),
                  mock.patch.object(journal, 'verified_rebase_result',
                                    return_value=candidates),
                  mock.patch.object(journal, 'rebase_completion_matches', return_value=True),
                  mock.patch.object(journal, 'live_remote_branch_oid', return_value=None)):
                self.assertEqual(len(journal.verified_managed_transitions(
                    state, '/repo', 'plan:G-099', git=owned_git,
                )), 1)
            with (mock.patch.object(journal, 'rebase_replay_proof', return_value=[]),
                  mock.patch.object(journal, 'verified_rebase_result',
                                    return_value=candidates)):
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.verified_managed_transitions(
                        state, '/repo', 'plan:G-099', git=owned_git,
                    )

    def test_sealed_rebase_remains_owned_after_later_commit_with_finish_receipt(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            before_head = 'a' * 40
            sealed_head = 'b' * 40
            current_head = 'c' * 40
            branch = 'loop-manager/G-099'
            observed = {
                'head': sealed_head, 'index': {}, 'dirty': {},
                'registration': [f'HEAD={sealed_head}'],
                'index_clean': True, 'metadata': None,
            }
            candidates = {'selected': {'head': sealed_head}}
            proof = [{'source': before_head, 'replayed': sealed_head}]
            identity = journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before={'head': before_head},
                expected={'branch': branch, 'before_head': before_head,
                          'target_base_oid': before_head, 'remote_oid': None,
                          'evidence_version': 2},
            )
            journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: observed, validate=lambda *_: candidates,
                replay_proof=lambda *_: proof,
            )
            finish = (f'{sealed_head}\0loop-manager-rebase:{identity} '
                      f'(finish): refs/heads/{branch} onto {before_head}\n')
            receipts = {'reflog': finish}

            def owned_git(_path, *args, **_kwargs):
                if args[:1] == ('rev-parse',):
                    return SimpleNamespace(returncode=0, stdout=current_head + '\n')
                if args[:1] == ('merge-base',):
                    return SimpleNamespace(returncode=0, stdout='')
                if args[:1] == ('reflog',):
                    return SimpleNamespace(returncode=0, stdout=receipts['reflog'])
                raise AssertionError(f'unexpected Git query: {args}')

            with (mock.patch.object(journal, 'rebase_replay_proof', return_value=proof),
                  mock.patch.object(journal, 'verified_rebase_result',
                                    return_value=candidates),
                  mock.patch.object(journal, 'live_remote_branch_oid') as remote):
                self.assertEqual(len(journal.verified_managed_transitions(
                    state, '/repo', 'plan:G-099', git=owned_git,
                )), 1)
                remote.assert_not_called()
                receipts['reflog'] = ''
                self.assertEqual(journal.verified_managed_transitions(
                    state, '/repo', 'plan:G-099', git=owned_git,
                ), [])


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
            if arguments[:3] == ('rev-list', '--no-merges', '--reverse'):
                commits = sources
                return SimpleNamespace(stdout='\n'.join(commits) + '\n', returncode=0)
            if arguments[:2] == ('rev-list', '--min-parents=2'):
                return SimpleNamespace(stdout='', returncode=0)
            if arguments[:2] == ('rev-list', '--reverse'):
                commits = replayed
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
                            'accepted_modes': {'app.py': '100644'},
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
            # Even a matching staged tree and dirty file signature cannot
            # authorize an index-only executable-bit change in a saved receipt.
            changed_index = {'app.py': ['H 100755 ' + '5' * 40 + ' 0']}
            active['expected']['staged_index'] = changed_index
            active['before']['index'] = changed_index
            with self.assertRaisesRegex(journal.MutationEvidenceError, 'stage or turn'):
                journal.rebase_replay_proof(git, state, active, result)
            active['expected']['staged_index'] = staged
            active['before']['index'] = staged
            trees[replayed[1]] = '9' * 40
            with self.assertRaises(journal.MutationEvidenceError):
                journal.rebase_replay_proof(git, state, active, result)


class RealGitJournalReplayTests(unittest.TestCase):
    def test_saved_merge_with_manual_content_is_not_silently_omitted(self):
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            def git(*args, **kwargs):
                return loop_manager.git(repo, *args, **kwargs)

            git('config', 'user.name', 'Rebase Test')
            git('config', 'user.email', 'rebase@example.test')
            (repo / 'initial.txt').write_text('initial\n', encoding='utf-8')
            git('add', 'initial.txt')
            git('commit', '-m', 'Initial')
            git('switch', '-c', 'work')
            (repo / 'work.txt').write_text('work\n', encoding='utf-8')
            git('add', 'work.txt')
            git('commit', '-m', 'Work')
            git('switch', 'main')
            (repo / 'main.txt').write_text('main\n', encoding='utf-8')
            git('add', 'main.txt')
            git('commit', '-m', 'Main')
            target = git('rev-parse', 'HEAD').stdout.strip()
            git('switch', 'work')
            git('merge', '--no-commit', 'main')
            (repo / 'manual.txt').write_text('unrecorded resolution\n', encoding='utf-8')
            git('add', 'manual.txt')
            git('commit', '-m', 'Manual merge addition')
            original = git('rev-parse', 'HEAD').stdout.strip()
            base = git('merge-base', original, target).stdout.strip()
            current = git('rev-list', '--no-merges', '--reverse',
                          f'{base}..{original}').stdout.splitlines()
            frozen = git('rev-list', '--reverse', f'{base}..{original}').stdout.splitlines()
            self.assertEqual(len(current), 1)
            self.assertEqual(len(frozen), 2)
            with self.assertRaisesRegex(ReplayEvidenceError,
                                        'omitted merge has unproven content'):
                source_commits(loop_manager.git, repo, original, target)
            with self.assertRaisesRegex(ReplayEvidenceError,
                                        'omitted merge has unproven content'):
                reconcile_frozen_sources(
                    loop_manager.git, repo, original=original, target=target,
                    base=base, frozen=frozen, current=current,
                )

    def test_middle_conflict_later_content_and_mode_survive_journal_recovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            state = root / 'state'
            state.mkdir()
            origin = root / 'origin.git'
            subprocess.run(
                ['git', 'init', '--bare', str(origin)], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            subprocess.run(
                ['git', 'init', '-b', 'main', str(repo)], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

            def git(*args, **kwargs):
                return loop_manager.git(repo, *args, **kwargs)

            git('config', 'user.name', 'Rebase Test')
            git('config', 'user.email', 'rebase@example.test')
            git('remote', 'add', 'origin', str(origin))
            app = repo / 'app.txt'
            original_lines = ['value=initial\n'] + [f'line-{n}\n' for n in range(1, 25)]
            app.write_text(''.join(original_lines), encoding='utf-8')
            git('add', 'app.txt')
            git('commit', '-m', 'Initial application')
            initial = git('rev-parse', 'HEAD').stdout.strip()

            branch = 'loop-manager/G-099'
            git('switch', '-c', branch)
            (repo / 'notes.txt').write_text('first replayed commit\n', encoding='utf-8')
            git('add', 'notes.txt')
            git('commit', '-m', 'Add independent note')
            git('switch', 'main')
            (repo / 'base-note.txt').write_text('merged base note\n', encoding='utf-8')
            git('add', 'base-note.txt')
            git('commit', '-m', 'Advance base before merge')
            git('switch', branch)
            git('merge', '--no-edit', 'main')
            merged = git('rev-parse', 'HEAD').stdout.strip()
            changed = original_lines.copy()
            changed[0] = 'value=worker\n'
            app.write_text(''.join(changed), encoding='utf-8')
            git('add', 'app.txt')
            git('commit', '-m', 'Change value on worker branch')
            changed[20] = 'line-20-after-resolution\n'
            app.write_text(''.join(changed), encoding='utf-8')
            git('add', 'app.txt')
            app.chmod(0o755)
            git('update-index', '--chmod=+x', '--', 'app.txt')
            git('commit', '-m', 'Change later content and mode')
            changed[12] = 'line-12-after-resolution\n'
            app.write_text(''.join(changed), encoding='utf-8')
            git('add', 'app.txt')
            git('commit', '-m', 'Change same file in another later commit')
            original_head = git('rev-parse', 'HEAD').stdout.strip()

            git('switch', 'main')
            changed_base = original_lines.copy()
            changed_base[0] = 'value=base\n'
            app.write_text(''.join(changed_base), encoding='utf-8')
            git('add', 'app.txt')
            git('commit', '-m', 'Advance base with conflicting value')
            target = git('rev-parse', 'HEAD').stdout.strip()
            git('switch', branch)

            repo_key = str(repo.resolve())
            source_base, sources = source_commits(loop_manager.git, repo, original_head, target)
            self.assertEqual(len(sources), 4)
            with self.assertRaisesRegex(ReplayEvidenceError,
                                        'omitted merge has unproven content'):
                source_commits(loop_manager.git, repo, original_head, initial)
            frozen_sources = git('rev-list', '--reverse',
                                 f'{source_base}..{original_head}').stdout.splitlines()
            self.assertEqual(len(frozen_sources), 5)
            self.assertIn(merged, frozen_sources)
            self.assertEqual(reconcile_frozen_sources(
                loop_manager.git, repo, original=original_head, target=target,
                base=source_base, frozen=frozen_sources, current=sources,
            ), sources)
            with self.assertRaisesRegex(ReplayEvidenceError, 'frozen source sequence mismatch'):
                reconcile_frozen_sources(
                    loop_manager.git, repo, original=original_head, target=target,
                    base=source_base, frozen=frozen_sources[:-1], current=sources,
                )
            expected = {
                'branch': branch, 'before_head': original_head,
                'target_base_oid': target, 'remote_oid': None,
                'affected_paths': journal.rebase_affected_paths(
                    loop_manager.git, repo, original_head, target,
                ),
                'agent_session_id': None, 'evidence_version': 2,
                'run_id': 'owned-run', 'source_base': source_base,
                'source_commits': frozen_sources, 'checkpoint_generation': 1,
                'pr_head_oid': None,
            }
            first = journal.begin_rebase(
                state, owner='plan:G-099', repo=repo_key, target=repo_key,
                before=loop_manager._owned_rebase_journal_state(repo, branch),
                expected=expected,
            )

            def rebase(*args, identity):
                environment = dict(os.environ)
                environment['GIT_REFLOG_ACTION'] = f'loop-manager-rebase:{identity}'
                environment['GIT_EDITOR'] = 'true'
                return git('rebase', *args, check=False, env=environment)

            started = rebase(target, identity=first)
            self.assertNotEqual(started.returncode, 0)
            conflict = loop_manager._owned_rebase_journal_state(repo, branch)
            self.assertEqual(conflict['rebase_head'], sources[1])

            def seal():
                def validate(intent, observed):
                    journal.rebase_replay_proof(loop_manager.git, state, intent, observed)
                    if observed['metadata'] is None:
                        marker = journal.rebase_completion_matches(
                            loop_manager.git, repo, branch=branch,
                            head=observed['head'], identity=intent['id'],
                            target_base_oid=target,
                        )
                    else:
                        marker = journal.rebase_conflict_matches(
                            loop_manager.git, repo, head=observed['head'],
                            identity=intent['id'],
                        )
                    ancestor = git(
                        'merge-base', '--is-ancestor', target, observed['head'],
                        check=False,
                    ).returncode == 0
                    return journal.verified_rebase_result(
                        intent, observed, current_remote_oid=None,
                        target_ancestor=ancestor, patches_preserved=False,
                        reflog_marker=marker, replay_verified=True,
                    )

                return journal.complete_rebase(
                    state, repo=repo_key, target=repo_key,
                    observe=lambda: loop_manager._owned_rebase_journal_state(repo, branch),
                    validate=validate,
                    replay_proof=lambda intent, observed: journal.rebase_replay_proof(
                        loop_manager.git, state, intent, observed,
                    ),
                )

            self.assertTrue(seal())
            resolved = changed_base.copy()
            resolved[0] = 'value=resolved\n'
            app.write_text(''.join(resolved), encoding='utf-8')
            git('add', 'app.txt')
            staged = loop_manager._owned_rebase_journal_state(repo, branch)
            # Reproduce the mode-only attack that Git diff can conceal when
            # core.fileMode is false. The file and its approval stay unchanged.
            file_mode = git('config', '--get', 'core.fileMode').stdout.strip()
            git('config', 'core.fileMode', 'false')
            git('update-index', '--chmod=+x', '--', 'app.txt')
            self.assertEqual(git('diff', '--quiet', '--', 'app.txt',
                                 check=False).returncode, 0)
            tampered = loop_manager._owned_rebase_journal_state(repo, branch)
            self.assertEqual(tampered['dirty']['app.txt'][1], staged['dirty']['app.txt'][1])
            self.assertFalse(stage_matches(
                before_index=conflict['index'], staged_index=tampered['index'],
                allowed_paths=['app.txt'],
                accepted_files={'app.txt': staged['dirty']['app.txt'][1]},
                accepted_modes={'app.txt': '100644'}, staged_dirty=tampered['dirty']))
            git('update-index', '--chmod=-x', '--', 'app.txt')
            git('config', 'core.fileMode', file_mode)
            staged_tree = git('write-tree').stdout.strip()
            continuation = journal.begin_rebase(
                state, owner='plan:G-099', repo=repo_key, target=repo_key,
                before=staged,
                expected={
                    **expected, 'checkpoint_generation': 2,
                    'action': 'continue', 'source_commit': sources[1],
                    'correction_turn': 'worker-turn-1',
                    'staged_tree_oid': staged_tree,
                    'stage_before_index': conflict['index'],
                    'staged_index': staged['index'],
                    'accepted_files': {'app.txt': staged['dirty']['app.txt'][1]},
                    'accepted_modes': {'app.txt': '100644'},
                },
            )
            continued = rebase('--continue', identity=continuation)
            self.assertEqual(continued.returncode, 0, continued.stdout)
            completed = loop_manager._owned_rebase_journal_state(repo, branch)
            self.assertIsNone(completed['metadata'])
            slot = journal._slot(state, repo_key, 'managed_rebase', repo_key)
            self.assertTrue((slot / 'active.json').is_file())
            self.assertFalse((slot / f'{continuation}.result.json').exists())

            # Resume from durable Git and journal state after the completed command.
            original_git = loop_manager.git

            def read_only_recovery(path, *args, **kwargs):
                if args and args[0] == 'rebase':
                    raise AssertionError('completed rebase was invoked again')
                return original_git(path, *args, **kwargs)

            with mock.patch.object(loop_manager, 'git', side_effect=read_only_recovery):
                self.assertTrue(seal())
            intent = journal._read(slot / f'{continuation}.intent.json')
            proof = journal.rebase_replay_proof(loop_manager.git, state, intent, completed)
            self.assertEqual([item['outcome'] for item in proof],
                             ['patch', 'resolved', 'patch', 'patch'])
            self.assertEqual(proof[1]['tree'], staged_tree)
            self.assertNotEqual(git('rev-parse', 'HEAD^{tree}').stdout.strip(), staged_tree)
            self.assertEqual(app.read_text(encoding='utf-8').splitlines()[0],
                             'value=resolved')
            self.assertEqual(app.read_text(encoding='utf-8').splitlines()[12],
                             'line-12-after-resolution')
            self.assertEqual(app.read_text(encoding='utf-8').splitlines()[20],
                             'line-20-after-resolution')
            self.assertTrue(git('ls-tree', 'HEAD', 'app.txt').stdout.startswith('100755 '))
            self.assertEqual(len(journal.verified_managed_transitions(
                state, repo_key, 'plan:G-099', git=loop_manager.git,
            )), 2)


if __name__ == '__main__':
    unittest.main()
