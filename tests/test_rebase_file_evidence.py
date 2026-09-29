"""Unit evidence checks; Git observations are mocked, no repository is mutated."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import manager_git_journal as journal
import rebase_file_evidence as files
from execution_checkpoint import rebase_preserves_worktree_files


def evidence(base, local, signature):
    return {'base': base, 'local': local, 'signature': signature,
            'mode': 33188, 'base_mode': '100644'}


class AutostashFileEvidenceTests(unittest.TestCase):
    def test_nonoverlapping_base_change_is_preserved_and_sealed(self):
        old = evidence(['a', 'b', 'c', 'd'], ['local', 'b', 'c', 'd'], 'old-hash')
        new = evidence(['a', 'base', 'c', 'd'], ['local', 'base', 'c', 'd'], 'new-hash')
        before = {
            'head': 'old', 'branch': 'loop-manager/G-091', 'ref': ['old', ''],
            'registration': ['HEAD=old', 'branch=refs/heads/loop-manager/G-091'],
            'index': {}, 'dirty': {'app.py': [['staged'], 'old-hash']},
            'metadata': None, 'file_evidence': {'app.py': old},
        }
        after = {
            **before, 'head': 'new', 'ref': ['new', ''],
            'registration': ['HEAD=new', 'branch=refs/heads/loop-manager/G-091'],
            'dirty': {'app.py': [['unstaged'], 'new-hash']},
            'file_evidence': {'app.py': new}, 'unmerged': False, 'index_clean': True,
        }
        expected = {'branch': 'loop-manager/G-091', 'before_head': 'old',
                    'target_base_oid': 'base', 'remote_oid': 'old'}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            journal.begin_rebase(state, owner='plan:G-091', repo='/repo',
                                 target='/worktree', before=before, expected=expected)
            self.assertTrue(journal.complete_rebase(
                state, repo='/repo', target='/worktree', observe=lambda: after,
                validate=lambda intent, observed: journal.verified_rebase_result(
                    intent, observed, current_remote_oid='old', target_ancestor=True,
                    patches_preserved=True, reflog_marker=True,
                ),
            ))
            with self.assertRaises(journal.MutationEvidenceError):
                journal.verified_managed_transitions(
                    state, '/repo', 'plan:G-091',
                )

    def test_adjacent_edits_preserve_both_changes(self):
        base = ['a', 'b', 'c']
        for local, upstream, merged in (
            (['local', 'b', 'c'], ['a', 'base', 'c'], ['local', 'base', 'c']),
            (['a', 'local', 'c'], ['base', 'b', 'c'], ['base', 'local', 'c']),
            (['b', 'c'], ['a', 'base', 'c'], ['base', 'c']),
            (['local', 'b', 'c'], ['a', 'c'], ['local', 'c']),
        ):
            with self.subTest(local=local, upstream=upstream):
                self.assertTrue(files.preserved(
                    evidence(base, local, 'old'), evidence(upstream, merged, 'new'),
                    'old', 'new',
                ))
                self.assertFalse(files.preserved(
                    evidence(base, local, 'old'),
                    evidence(upstream, merged + ['external'], 'new'), 'old', 'new',
                ))

    def test_overlaps_and_ambiguous_insertions_remain_rejected(self):
        base = ['a', 'b', 'c']
        for local, upstream, merged in (
            (['local', 'c'], ['a', 'base', 'c'], ['local', 'base', 'c']),
            (['local', 'b', 'c'], ['base', 'b', 'c'], ['local', 'b', 'c']),
            (['a', 'local', 'b', 'c'], ['a', 'base', 'b', 'c'],
             ['a', 'local', 'base', 'b', 'c']),
            (['local', 'b', 'c'], ['a', 'insert', 'b', 'c'],
             ['local', 'insert', 'b', 'c']),
        ):
            with self.subTest(local=local, upstream=upstream):
                self.assertFalse(files.preserved(
                    evidence(base, local, 'old'), evidence(upstream, merged, 'new'),
                    'old', 'new',
                ))

    def test_missing_external_or_overlapping_evidence_is_rejected(self):
        old = evidence(['a', 'b', 'c', 'd'], ['local', 'b', 'c', 'd'], 'old')
        new = evidence(['a', 'b', 'c', 'base'], ['local', 'b', 'c', 'base'], 'new')
        for changed in (
            None,
            {**new, 'local': ['external', 'b', 'c', 'base']},
            {**new, 'signature': 'different'},
            {**new, 'mode': 33261},
            {**new, 'base_mode': '100755'},
            {**new, 'base': ['overlap', 'b', 'c', 'd']},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(rebase_preserves_worktree_files(
                    {'app.py': [['staged'], 'old']},
                    {'app.py': [['unstaged'], 'new']},
                    before_evidence={'app.py': old}, after_evidence={'app.py': changed},
                ))
        # Untracked files and evidence-free old journal entries remain strict.
        self.assertFalse(rebase_preserves_worktree_files(
            {'x': [['untracked'], 'old']}, {'x': [['untracked'], 'new']},
        ))

    def test_both_rebase_snapshot_paths_capture_file_evidence(self):
        import loop_manager
        from execution_handlers import ExecutionHandlers

        worktree = Path('/owned')
        branch = 'loop-manager/G-091'
        snapshot = SimpleNamespace(
            head='head', branch=branch, refs={f'refs/heads/{branch}': ['head', '']},
            index={}, dirty={'app.py': [['unstaged'], 'signature']},
            worktrees={str(worktree.resolve()): ['HEAD=head']}, operations={},
        )
        saved = {'app.py': evidence(['a'], ['b'], 'signature')}
        git = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=''))
        handler = object.__new__(ExecutionHandlers)
        handler.effects = SimpleNamespace(
            agent=SimpleNamespace(capture_git_snapshot=lambda *_: snapshot),
            workspace=SimpleNamespace(git=git),
        )
        handler._rebase_metadata = lambda *_: None
        handler._unmerged_stages = lambda *_: ''
        with (mock.patch.object(files, 'capture', return_value=saved) as capture,
              mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
              mock.patch.object(loop_manager, '_owned_rebase_metadata', return_value=None),
              mock.patch.object(loop_manager, 'git', git)):
            self.assertEqual(handler._rebase_journal_state(SimpleNamespace(
                worktree=worktree, branch=branch,
            ))['file_evidence'], saved)
            self.assertEqual(loop_manager._owned_rebase_journal_state(
                worktree, branch,
            )['file_evidence'], saved)
            self.assertEqual(capture.call_args_list, [
                mock.call(worktree, 'head', snapshot.dirty),
                mock.call(worktree, 'head', snapshot.dirty),
            ])

    def test_capture_binds_raw_line_hashes_to_observed_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'app.py'
            content = b'local\r\nunchanged\n'
            path.write_bytes(content)
            mode = path.stat().st_mode
            signature = hashlib.sha256(str(mode).encode() + b'\0' + content).hexdigest()
            results = [
                SimpleNamespace(returncode=0, stdout=b'100644 blob abc\tapp.py\0'),
                SimpleNamespace(returncode=0, stdout=b'base\r\nunchanged\n'),
            ]
            with mock.patch.object(files.subprocess, 'run', side_effect=results) as run:
                saved = files.capture(root, 'owned-head', {'app.py': [['staged'], signature]})
                self.assertEqual(saved['app.py']['local'], [
                    hashlib.sha256(line).hexdigest() for line in content.splitlines(True)
                ])
                self.assertEqual(saved['app.py']['signature'], signature)
                self.assertEqual(run.call_args_list[0].args[0][-3:], ['owned-head', '--', 'app.py'])
            with mock.patch.object(files.subprocess, 'run') as run:
                self.assertEqual(files.capture(root, 'owned-head', {
                    'app.py': [['staged'], 'stale-signature'],
                }), {})
                run.assert_not_called()
