"""Narrow ownership checks for managed workspace and rebase journal entries."""

from pathlib import Path
from dataclasses import replace
from managed_rebase_validation import FileChange, IndexEntry
from tests.test_execution_handlers import current_rebase_case
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from execution_checkpoint import owned_detached_rebase, rebase_preserves_worktree_files
from execution_handlers import ExecutionHandlers
from rebase_correction import CorrectionAction
import manager_git_journal as journal
import loop_manager
from execution_checkpoint_state import ExecutionCheckpointRepository


OLD = 'a' * 40
NEW = 'b' * 40
TREE = 'c' * 40
BRANCH = 'loop-manager/G-091'


class ManagedTransitionTests(unittest.TestCase):
    def test_v2_rebase_retry_keeps_intent_after_checkpoint_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            expected = {
                'evidence_version': 2, 'run_id': 'same-run',
                'checkpoint_generation': 3, 'branch': BRANCH,
                'before_head': OLD, 'target_base_oid': NEW,
                'remote_oid': OLD, 'source_commits': [OLD],
                'agent_session_id': 'same-worker',
            }
            arguments = dict(state_dir=state, owner='plan:G-091',
                             repo='/repo', target='/worktree', before={'head': OLD})
            identity = journal.begin_rebase(**arguments, expected=expected)

            self.assertEqual(journal.begin_rebase(
                **arguments, expected={**expected, 'checkpoint_generation': 4},
            ), identity)
            for changed in (
                {**expected, 'checkpoint_generation': 2},
                {**expected, 'checkpoint_generation': 4, 'run_id': 'other-run'},
                {**expected, 'checkpoint_generation': 4,
                 'agent_session_id': 'other-worker'},
            ):
                with self.subTest(changed=changed), self.assertRaises(journal.MutationEvidenceError):
                    journal.begin_rebase(**arguments, expected=changed)

    def test_completed_rebase_accepts_stale_head_marker_but_rejects_active_operation(self):
        before = {
            'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
            'index': {}, 'dirty': {},
            'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
            'metadata': None,
        }
        intent = {
            'target': '/worktree', 'before': before,
            'expected': {
                'branch': BRANCH, 'before_head': OLD,
                'target_base_oid': NEW, 'remote_oid': OLD,
                'evidence_version': 2,
            },
        }
        observed = {
            **before, 'head': NEW, 'ref': [NEW, ''],
            'registration': [f'HEAD={NEW}', f'branch=refs/heads/{BRANCH}'],
            'operations': {'REBASE_HEAD': 'stale-marker'},
            'index_clean': True, 'unmerged': False,
        }
        checks = dict(current_remote_oid=OLD, target_ancestor=True,
                      patches_preserved=False, reflog_marker=True,
                      replay_verified=True)

        self.assertEqual(journal.verified_rebase_result(
            intent, observed, **checks,
        )['selected']['head'], NEW)
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_rebase_result(
                intent, {**observed, 'operations': {
                    **observed['operations'], 'rebase-merge': 'active-marker',
                }}, **checks,
            )
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_rebase_result(
                intent, {**observed, 'operations': None}, **checks,
            )

    def test_recovered_agent_baseline_accepts_only_journaled_lock_change(self):
        old_registration = (f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}')
        lock = 'loop-manager-worktree:owned-token'
        before = loop_manager.GitSnapshot(
            head=OLD, branch=BRANCH, index={}, dirty={}, refs={},
            worktrees={'/worktree': old_registration}, config={}, operations={},
        )
        current = loop_manager.replace(
            before, worktrees={'/worktree': (*old_registration, f'locked={lock}')},
        )
        transition = {'intent': {
            'kind': 'managed_worktree_create', 'target': '/worktree',
            'before': {'exists': False, 'registration': None, 'index': None},
            'expected': {'registration': {'head': OLD,
                                          'branch': f'refs/heads/{BRANCH}',
                                          'locked': lock}},
        }, 'result': {'observed': {'registration': {
            'head': OLD, 'branch': f'refs/heads/{BRANCH}', 'locked': lock,
        }}}}
        def journal_transitions(*_args, git=None, **_kwargs):
            if git is None:
                raise journal.MutationEvidenceError('v2 rebase requires Git observation')
            return [transition]

        with mock.patch.object(journal, 'verified_managed_transitions',
                               side_effect=journal_transitions) as transitions:
            corrected = loop_manager._proven_recovered_worktree_baseline(
                before, current, Path('/state'), Path('/repo'),
                'G-091', Path('/worktree'),
            )
            self.assertEqual(corrected.worktrees, current.worktrees)
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager._proven_recovered_worktree_baseline(
                    before, loop_manager.replace(current, dirty={'app.py':
                        (('unstaged',), 'external')}), Path('/state'),
                    Path('/repo'), 'G-091', Path('/worktree'),
                )
            transition['intent']['expected']['registration']['locked'] = 'other'
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager._proven_recovered_worktree_baseline(
                    before, current, Path('/state'), Path('/repo'),
                    'G-091', Path('/worktree'),
                )
            transitions.assert_called_with(
                Path('/state'), str(Path('/repo').resolve()), 'plan:G-091',
                git=loop_manager.git,
            )

    def test_rebase_continuation_accepts_only_proven_worker_correction(self):
        before = {
            'head': OLD, 'branch': None, 'ref': [OLD, ''],
            'dirty': {'app.py': [['staged'], 'accepted']},
            'registration': [f'HEAD={OLD}', 'detached=True'],
            'metadata': {'orig-head': OLD,
                         'head-name': f'refs/heads/{BRANCH}', 'onto': NEW},
        }
        observed = {
            **before, 'head': NEW, 'branch': BRANCH, 'ref': [NEW, ''],
            'dirty': {}, 'registration': [f'HEAD={NEW}',
                                        f'branch=refs/heads/{BRANCH}'],
            'metadata': None, 'index': {}, 'index_clean': True,
            'unmerged': False,
        }
        intent = {
            'target': '/worktree', 'before': before,
            'expected': {'branch': BRANCH, 'before_head': OLD,
                         'target_base_oid': NEW, 'remote_oid': OLD,
                         'action': 'continue',
                         'accepted_files': {'app.py': 'accepted'},
                         'staged_tree_oid': TREE},
        }
        checks = dict(current_remote_oid=OLD, target_ancestor=True,
                      patches_preserved=False, reflog_marker=True)
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_rebase_result(intent, observed, **checks)
        self.assertEqual(journal.verified_rebase_result(
            intent, observed, accepted_correction=True, **checks,
        )['selected']['head'], NEW)
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_rebase_result(
                intent, observed, accepted_correction=True,
                **{**checks, 'current_remote_oid': NEW},
            )

    def test_conflict_marker_requires_current_head_and_exact_operation(self):
        git = mock.Mock()
        for action in ('start', 'pick', 'continue', 'skip'):
            git.return_value = SimpleNamespace(returncode=0, stdout=(
                f'{NEW}\0loop-manager-rebase:owned ({action}): message\n'
            ))
            self.assertTrue(journal.rebase_conflict_matches(
                git, Path('/selected'), head=NEW, identity='owned',
            ))
        for output in (
            f'{NEW}\0rebase (start): checkout base\n',
            f'{OLD}\0loop-manager-rebase:owned (start): checkout base\n',
            f'{NEW}\0loop-manager-rebase:owned-other (start): checkout base\n',
            f'{NEW}\0reset: external\n', '',
        ):
            with self.subTest(output=output):
                git.return_value = SimpleNamespace(returncode=0, stdout=output)
                self.assertFalse(journal.rebase_conflict_matches(
                    git, Path('/selected'), head=NEW, identity='owned',
                ))
        git.return_value = SimpleNamespace(returncode=1, stdout=(
            f'{NEW}\0loop-manager-rebase:owned (start): checkout base\n'
        ))
        self.assertFalse(journal.rebase_conflict_matches(
            git, Path('/selected'), head=NEW, identity='owned',
        ))
        git.assert_called_with(Path('/selected'), 'reflog', 'show', '-1',
                               '--format=%H%x00%gs', 'HEAD', check=False)

    def test_rebase_conflict_result_requires_matching_metadata_and_remote(self):
        before = {
            'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
            'index': {'app.py': ['old']}, 'dirty': {},
            'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
            'operations': {}, 'metadata': None, 'index_clean': True,
        }
        intent = {
            'id': 'owned', 'target': '/worktree', 'before': before,
            'expected': {'branch': BRANCH, 'before_head': OLD,
                         'target_base_oid': NEW, 'remote_oid': OLD,
                         'affected_paths': ['app.py']},
        }
        observed = {
            **before, 'head': NEW, 'branch': None,
            'index': {'app.py': [f'H 100644 {OLD} 1',
                                  f'H 100644 {NEW} 2']},
            'registration': [f'HEAD={NEW}', 'detached=True'],
            'metadata': {'orig-head': OLD,
                         'head-name': f'refs/heads/{BRANCH}', 'onto': NEW},
            'unmerged': True, 'index_clean': False,
        }
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_rebase_result(
                intent, observed, current_remote_oid=OLD,
                target_ancestor=True, patches_preserved=False, reflog_marker=False,
            )
        candidate = journal.verified_rebase_result(
            intent, observed, current_remote_oid=OLD,
            target_ancestor=True, patches_preserved=False,
            reflog_marker=True,
        )
        self.assertIsNone(candidate['selected']['index'])
        self.assertIsNone(candidate['selected']['dirty'])
        for changed, remote in (({**observed, 'metadata':
                                 {**observed['metadata'], 'onto': OLD}}, OLD),
                                (observed, NEW),
                                ({**observed, 'ref': [NEW, '']}, OLD)):
            with self.subTest(remote=remote, changed=changed):
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.verified_rebase_result(
                        intent, changed, current_remote_oid=remote,
                        target_ancestor=True, patches_preserved=False,
                        reflog_marker=True,
                    )

    def test_pending_continuation_preserves_sealed_conflict_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            target = str((state / 'worktree').resolve())
            before = {
                'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
                'index': {'app.py': ['old']}, 'dirty': {},
                'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
                'operations': {}, 'metadata': None, 'index_clean': True,
            }
            expected = {
                'branch': BRANCH, 'before_head': OLD, 'target_base_oid': NEW,
                'remote_oid': OLD, 'affected_paths': ['app.py'],
                'agent_session_id': 'worker-original',
            }
            conflict = {
                **before, 'head': NEW, 'branch': None,
                'index': {'app.py': [f'H 100644 {OLD} 1', f'H 100644 {NEW} 2']},
                'dirty': {'app.py': [['unstaged'], 'conflict']},
                'registration': [f'HEAD={NEW}', 'detached=True'],
                'metadata': {'orig-head': OLD,
                             'head-name': f'refs/heads/{BRANCH}', 'onto': NEW},
                'unmerged': True, 'index_clean': False,
            }
            sealed_id = journal.begin_rebase(
                state, owner='plan:G-091', repo='/repo', target=target,
                before=before, expected=expected,
            )
            # Intent exists, but an external rebase can produce the same
            # metadata without executing this Manager operation.
            with self.assertRaises(journal.MutationEvidenceError):
                journal.complete_rebase(
                    state, repo='/repo', target=target, observe=lambda: conflict,
                    validate=lambda intent, observed: journal.verified_rebase_result(
                        intent, observed, current_remote_oid=OLD,
                        target_ancestor=True, patches_preserved=False, reflog_marker=False,
                    ),
                )
            slot = journal._slot(state, '/repo', 'managed_rebase', target)
            self.assertTrue((slot / 'active.json').exists())
            self.assertFalse((slot / f'{sealed_id}.result.json').exists())
            journal.complete_rebase(
                state, repo='/repo', target=target, observe=lambda: conflict,
                validate=lambda intent, observed: journal.verified_rebase_result(
                    intent, observed, current_remote_oid=OLD,
                    target_ancestor=True, patches_preserved=False, reflog_marker=True,
                ),
            )
            corrected = {
                **conflict, 'index': {'app.py': [f'H 100644 {TREE} 0']},
                'dirty': {'app.py': [['staged'], 'resolved']}, 'unmerged': False,
            }
            pending_id = journal.begin_rebase(
                state, owner='plan:G-091', repo='/repo', target=target,
                before=corrected, expected={**expected, 'action': 'continue',
                                           'accepted_files': {'app.py': 'resolved'},
                                           'staged_tree_oid': TREE},
            )
            # Restart after saving intent but before invoking Git: no outcome
            # can be sealed, and the same intent must remain available to retry.
            validate = mock.Mock()
            self.assertFalse(journal.complete_rebase(
                state, repo='/repo', target=target,
                observe=lambda: corrected, validate=validate,
            ))
            validate.assert_not_called()
            slot = journal._slot(state, '/repo', 'managed_rebase', target)
            self.assertEqual(journal._read(slot / 'active.json')['id'], pending_id)
            self.assertFalse((slot / f'{pending_id}.result.json').exists())
            transitions = journal.verified_managed_transitions(
                state, '/repo', 'plan:G-091', agent_session_id='worker-original',
            )
            self.assertEqual([item['intent']['id'] for item in transitions], [sealed_id])
            self.assertEqual(journal.verified_managed_transitions(
                state, '/repo', 'plan:G-091', agent_session_id='other-worker',
            ), [])
            handler = object.__new__(ExecutionHandlers)
            handler.state_dir, handler.repo_path = state, Path('/repo')
            handler.effects = SimpleNamespace(workspace=SimpleNamespace(git=mock.Mock()))
            context = SimpleNamespace(
                plan=SimpleNamespace(plan_id='G-091'), worktree=Path(target),
                worker_session_id='worker-original',
            )
            current = SimpleNamespace(expected_head=OLD, target_base_oid=NEW)
            self.assertTrue(handler._matching_rebase_journal_result(
                context, current, OLD, corrected, allow_later_file_changes=True,
            ))
            self.assertFalse(handler._matching_rebase_journal_result(
                context, current, OLD, {**corrected, 'ref': [NEW, '']},
                allow_later_file_changes=True,
            ))
            # A result written before active.json cleanup is still pending.
            # It must not be exposed just because older results are now visible.
            (slot / 'active.json').write_text(
                (slot / f'{sealed_id}.intent.json').read_text(),
            )
            self.assertEqual(journal.verified_managed_transitions(
                state, '/repo', 'plan:G-091', agent_session_id='worker-original',
            ), [])

    def test_conflict_rejects_unrelated_file_and_index_changes(self):
        before = {
            'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
            'index': {'app.py': ['old'], 'other.py': ['unchanged']},
            'dirty': {},
            'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
            'metadata': None,
        }
        intent = {
            'target': '/worktree', 'before': before,
            'expected': {'branch': BRANCH, 'before_head': OLD,
                         'target_base_oid': NEW, 'remote_oid': OLD,
                         'affected_paths': ['app.py']},
        }
        observed = {
            **before, 'head': NEW, 'branch': None,
            'registration': [f'HEAD={NEW}', 'detached=True'],
            'metadata': {'orig-head': OLD,
                         'head-name': f'refs/heads/{BRANCH}', 'onto': NEW},
            'index': {'app.py': [f'H 100644 {OLD} 1',
                                  f'H 100644 {NEW} 2'],
                      'other.py': ['unchanged']},
            'dirty': {'app.py': [['unstaged'], 'conflict']},
            'unmerged': True, 'index_clean': False,
        }
        checks = dict(current_remote_oid=OLD, target_ancestor=True,
                      patches_preserved=False, reflog_marker=True)
        self.assertIsNone(journal.verified_rebase_result(
            intent, observed, **checks,
        )['selected']['dirty'])
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            journal.begin_rebase(
                state, owner='plan:G-091', repo='/repo', target='/worktree',
                before=before, expected=intent['expected'],
            )
            self.assertTrue(journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: observed,
                validate=lambda saved, actual: journal.verified_rebase_result(
                    saved, actual, **checks,
                ),
            ))
            selected = journal.verified_managed_transitions(
                state, '/repo', 'plan:G-091',
            )[0]['result']['candidates']['selected']
            self.assertIsNone(selected['index'])
            self.assertIsNone(selected['dirty'])
        for changed in (
            {**observed, 'dirty': {**observed['dirty'],
                                  'other.py': [['unstaged'], 'external']}},
            {**observed, 'index': {**observed['index'],
                                  'other.py': ['external']}},
            {**observed, 'dirty': {**observed['dirty'],
                                  'app.py': [['untracked'], 'external']}},
            {**observed, 'dirty': {**observed['dirty'],
                                  'other.py': [['unstaged'], 'external']},
             'index': {**observed['index'],
                       'other.py': [f'H 100644 {NEW} 0']}},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.verified_rebase_result(intent, changed, **checks)
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_rebase_result(
                {**intent, 'expected': {**intent['expected'],
                                        'affected_paths': ['app.py', 'other.py']}},
                {**observed, 'dirty': {**observed['dirty'],
                                       'other.py': [['unstaged'], 'external']}},
                **checks,
            )

    def test_rebase_affected_paths_include_intermediate_commit_changes(self):
        def git(_repo, *args):
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout=TREE + '\n')
            if args[0] == 'diff':
                return SimpleNamespace(stdout='base.py\0')
            if args[0] == 'rev-list':
                return SimpleNamespace(stdout=OLD + '\n')
            if args[0] == 'diff-tree':
                return SimpleNamespace(stdout='reverted.py\0')
            raise AssertionError(args)

        self.assertEqual(journal.rebase_affected_paths(
            git, Path('/repo'), OLD, NEW,
        ), ['base.py', 'reverted.py'])

    def test_autostash_conflict_requires_saved_stash_identity(self):
        before = {
            'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
            'index': {}, 'dirty': {'app.py': [['unstaged'], 'original']},
            'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
            'operations': {}, 'metadata': None, 'unmerged': False,
            'index_clean': True,
        }
        intent = {
            'id': 'owned', 'target': '/worktree', 'before': before,
            'expected': {'branch': BRANCH, 'before_head': OLD,
                         'target_base_oid': NEW, 'remote_oid': OLD},
        }
        observed = {
            **before, 'head': NEW, 'ref': [NEW, ''],
            'registration': [f'HEAD={NEW}', f'branch=refs/heads/{BRANCH}'],
            'dirty': {'app.py': [['unmerged'], 'conflict']},
            'unmerged': True,
        }
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_rebase_result(
                intent, observed, current_remote_oid=OLD,
                target_ancestor=True, patches_preserved=True,
                reflog_marker=True,
            )
        self.assertTrue(journal.verified_rebase_result(
            intent, observed, current_remote_oid=OLD,
            target_ancestor=True, patches_preserved=True,
            reflog_marker=True, autostash_owned=True,
        )['selected']['unmerged'])

    def test_successful_rebase_does_not_offer_unproven_staged_index(self):
        before = {
            'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
            'index': {}, 'dirty': {},
            'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
            'metadata': None, 'index_clean': True,
        }
        intent = {
            'id': 'owned', 'target': '/worktree', 'before': before,
            'expected': {'branch': BRANCH, 'before_head': OLD,
                         'target_base_oid': NEW, 'remote_oid': OLD},
        }
        observed = {
            **before, 'head': NEW, 'ref': [NEW, ''],
            'registration': [f'HEAD={NEW}', f'branch=refs/heads/{BRANCH}'],
            'index': {'external.py': ['staged']}, 'index_clean': False,
        }
        candidate = journal.verified_rebase_result(
            intent, observed, current_remote_oid=OLD,
            target_ancestor=True, patches_preserved=True,
            reflog_marker=True,
        )
        self.assertIsNone(candidate['selected']['index'])
        self.assertEqual(observed['index'], {'external.py': ['staged']})

    def test_create_and_remove_require_exact_registration_index_and_marker(self):
        missing = {'exists': False, 'registration': None, 'index': None}
        created = {
            'exists': True,
            'registration': {'head': OLD, 'branch': f'refs/heads/{BRANCH}',
                             'locked': 'loop-manager-worktree:owned'},
            'index': TREE,
        }
        create = {'kind': 'managed_worktree_create', 'target': '/state/G-091',
                  'owner': 'plan:G-091',
                  'before': missing, 'expected': created}
        self.assertEqual(
            journal.verified_transition(create, created, True)['worktrees'],
            {'/state/G-091': created['registration']},
        )
        for observed, marker in ((created, False),
                                 ({**created, 'index': NEW}, True),
                                 ({**created, 'registration':
                                   {**created['registration'], 'head': NEW}}, True)):
            with self.subTest(observed=observed, marker=marker):
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.verified_transition(create, observed, marker)
        remove = {'kind': 'managed_worktree_remove', 'target': '/state/G-091',
                  'owner': 'plan:G-091',
                  'before': created, 'expected': missing}
        self.assertEqual(
            journal.verified_transition(remove, missing, True)['worktrees'],
            {'/state/G-091': None},
        )
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_transition(remove, missing, False)

    def test_rebase_result_recovers_only_matching_active_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            before = {
                'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
                'index': {'app.py': ['old']}, 'dirty': {},
                'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
                'operations': {}, 'metadata': None, 'unmerged': False,
                'index_clean': True,
            }
            expected = {'branch': BRANCH, 'before_head': OLD,
                        'target_base_oid': NEW, 'remote_oid': OLD,
                        'agent_session_id': 'worker-original'}
            identity = journal.begin_rebase(
                state, owner='plan:G-091', repo='/repo', target='/worktree',
                before=before, expected=expected,
            )
            self.assertEqual(identity, journal.begin_rebase(
                state, owner='plan:G-091', repo='/repo', target='/worktree',
                before=before, expected=expected,
            ))
            with self.assertRaises(journal.MutationEvidenceError):
                journal.begin_rebase(
                    state, owner='plan:G-092', repo='/repo', target='/worktree',
                    before=before, expected=expected,
                )
            with self.assertRaises(journal.MutationEvidenceError):
                journal.complete_rebase(
                    state, repo='/repo', target='/worktree',
                    observe=lambda: {'head': 'unexpected'},
                    validate=lambda *_: (_ for _ in ()).throw(
                        journal.MutationEvidenceError('external mutation')),
                )
            observed = {
                'head': NEW, 'branch': BRANCH,
                'index': {'app.py': ['new']}, 'dirty': {},
                'registration': [f'HEAD={NEW}', f'branch=refs/heads/{BRANCH}'],
                'ref': [NEW, ''], 'operations': {}, 'metadata': None,
                'unmerged': False, 'index_clean': True,
            }
            self.assertTrue(journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: observed,
                validate=lambda _intent, actual: {
                    'refs': {f'refs/heads/{BRANCH}': actual['ref']},
                    'worktrees': {'/worktree': actual['registration']},
                    'selected': {'head': actual['head'], 'index': actual['index'],
                                 'dirty': actual['dirty'], 'unmerged': False},
                },
            ))
            # The legacy result has no Git replay proof. Its stored candidate
            # alone cannot establish current ownership on a later read.
            with self.assertRaises(journal.MutationEvidenceError):
                journal.verified_managed_transitions(
                    state, '/repo', 'plan:G-091',
                    agent_session_id='worker-original',
                )
            self.assertEqual(journal.verified_managed_transitions(
                state, '/repo', 'plan:G-091',
                agent_session_id='worker-new',
            ), [])

    def test_managed_creation_journals_registration_before_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            worktree = root / 'state' / 'worktrees' / 'project' / 'G-091'
            state = root / 'state'
            observed = {'exists': False, 'registration': None, 'index': None}
            calls = []

            def git(_repo, *args, **_kwargs):
                calls.append(args)
                if args[0] == 'show-ref':
                    return SimpleNamespace(returncode=1, stdout='')
                if args[0] == 'rev-parse':
                    return SimpleNamespace(returncode=0,
                                           stdout=(TREE if args[-1].endswith('^{tree}') else OLD) + '\n')
                if args[:2] == ('worktree', 'add'):
                    worktree.mkdir()
                    observed.update(exists=True, index=TREE, registration={
                        'head': OLD, 'branch': f'refs/heads/{BRANCH}',
                        'locked': args[3].removeprefix('--reason='),
                    })
                return SimpleNamespace(returncode=0, stdout='')

            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, '_managed_worktree_transition_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager, 'validate_managed_worktree')):
                self.assertEqual(loop_manager.make_worktree(
                    repo, worktree, BRANCH, 'main', journal_state_dir=state,
                    journal_owner='plan:G-091',
                ), 'created')
                self.assertEqual(loop_manager.make_worktree(
                    repo, worktree, BRANCH, 'main', journal_state_dir=state,
                    journal_owner='plan:G-091',
                ), 'existing')
            self.assertTrue(any(args[:3] == ('worktree', 'add', '--lock')
                                for args in calls))
            self.assertEqual(len(journal.verified_managed_transitions(
                state, str(repo.resolve()), 'plan:G-091',
            )), 1)

    def test_creation_recovers_after_git_before_journal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            state = root / 'state'
            worktree = state / 'worktrees' / 'project' / 'G-091'
            observed = {'exists': False, 'registration': None, 'index': None}

            def git(_repo, *args, **_kwargs):
                if args[0] == 'show-ref':
                    return SimpleNamespace(returncode=1, stdout='')
                if args[0] == 'rev-parse':
                    return SimpleNamespace(returncode=0,
                                           stdout=(TREE if args[-1].endswith('^{tree}') else OLD) + '\n')
                if args[:2] == ('worktree', 'add'):
                    worktree.mkdir()
                    observed.update(exists=True, index=TREE, registration={
                        'head': OLD, 'branch': f'refs/heads/{BRANCH}',
                        'locked': args[3].removeprefix('--reason='),
                    })
                return SimpleNamespace(returncode=0, stdout='')

            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, '_managed_worktree_transition_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager, 'validate_managed_worktree')):
                with mock.patch.object(journal, '_finish', side_effect=RuntimeError('crash')):
                    with self.assertRaisesRegex(RuntimeError, 'crash'):
                        loop_manager.make_worktree(
                            repo, worktree, BRANCH, 'main', journal_state_dir=state,
                            journal_owner='plan:G-091',
                        )
                self.assertEqual(loop_manager.make_worktree(
                    repo, worktree, BRANCH, 'main', journal_state_dir=state,
                    journal_owner='plan:G-091',
                ), 'existing')
            self.assertEqual(len(journal.verified_managed_transitions(
                state, str(repo.resolve()), 'plan:G-091',
            )), 1)

    def test_cleanup_crash_without_receipt_preserves_active_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            worktree = root / 'G-091'
            worktree.mkdir()
            state = root / 'state'
            state.mkdir()
            observed = {'exists': True, 'registration': {
                'head': OLD, 'branch': f'refs/heads/{BRANCH}',
                'locked': 'loop-manager-worktree:owned',
            }, 'index': TREE}

            def git(_repo, *args, **_kwargs):
                if args[0] == 'rev-parse':
                    return SimpleNamespace(returncode=0, stdout=TREE + '\n')
                if args[:2] == ('worktree', 'remove'):
                    observed.update(exists=False, registration=None, index=None)
                    worktree.rmdir()
                return SimpleNamespace(returncode=0, stdout='')

            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, '_managed_worktree_transition_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager, '_capture_local_worktree_state',
                                    return_value=(OLD, BRANCH, {}, {})),
                  mock.patch.object(loop_manager, '_git_operation_entries',
                                    return_value={}),
                  mock.patch.object(loop_manager, '_ignored_worktree_paths',
                                    return_value=[]),
                  mock.patch.object(loop_manager, '_write_managed_worktree_receipt',
                                    side_effect=RuntimeError('crash after Git'))):
                result = loop_manager.remove_cleanup_worktree(
                    repo, worktree, loop_manager.CleanupAssessment('safe'),
                    state_dir=state, owner='plan:G-091',
                )
                self.assertEqual(result.status, 'unverifiable')
                with self.assertRaises(journal.MutationEvidenceError):
                    loop_manager._recover_managed_worktree_removal(
                        state, repo, worktree,
                    )
            self.assertEqual(len(list((state / 'manager-git-journal').glob(
                '*/active.json',
            ))), 1)

    def test_direct_rebase_result_recovers_after_git_before_journal_result(self):
        case = current_rebase_case(self)
        case.fixture.journal_start.stop()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            journal.begin_current_rebase(state_dir, owner='plan:G-047', repo='/repo', target='/state/G-047',
                before={'head': 'before'}, expected={'branch': 'loop-manager/G-047', 'before_head': 'before',
                'target_base_oid': 'base', 'remote_oid': 'remote'})
            with (
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=state_dir),
                mock.patch.object(loop_manager, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)),
                mock.patch.object(loop_manager, '_owned_rebase_journal_state', return_value={'head': 'owned-head'}),
            ):
                with mock.patch.object(journal, '_finish', side_effect=RuntimeError('crash')):
                    with self.assertRaisesRegex(RuntimeError, 'crash'):
                        loop_manager._finish_owned_direct_rebase(state_dir, case.fixture.connection,
                            Path('/repo'), 'repo', 'G-047', Path('/state/G-047'), 'loop-manager/G-047')
                loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                    Path('/state/G-047'), 'base', repo_path=Path('/repo'), branch='loop-manager/G-047',
                    agent_session_id='replacement')
                saved = case.checkpoint.get('repo', 'G-047')
                loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                    Path('/state/G-047'), 'base', repo_path=Path('/repo'), branch='loop-manager/G-047',
                    agent_session_id=None)
            self.assertIsNone(saved.operation)
            self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)
            self.assertEqual(saved.snapshot['current_rebase_acceptance']['head'], 'owned-head')
            refreshed = case.fixture.repository.projected('repo')
            self.assertIsNone(refreshed.worker_result)
            self.assertEqual(refreshed.status, 'IMPLEMENTING')
            self.assertTrue((journal._slot(state_dir, '/repo', 'managed_rebase', '/state/G-047') / 'active.json').exists())

    def test_direct_rebase_without_sealed_result_keeps_checkpoint_operation(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        repository.record(
            'repo', 'G-091', expected_generation=0, operation='rebase',
            expected_head=OLD, target_base_oid=NEW,
            observed_remote_oid=OLD, snapshot={'owned': True},
        )
        with self.assertRaises(journal.MutationEvidenceError):
            loop_manager._complete_direct_rebase_checkpoint(
                connection, 'repo', 'G-091', Path('/owned'), NEW,
                repo_path=Path('/repo'), branch=BRANCH,
                agent_session_id=None,
            )
        current = repository.get('repo', 'G-091')
        self.assertEqual(current.operation, 'rebase')
        self.assertEqual(current.expected_head, OLD)
        self.assertEqual(current.observed_remote_oid, OLD)
        self.assertEqual(current.snapshot, {'owned': True})

    def test_direct_rebase_completion_rechecks_pre_head_marker_after_head_record(self):
        case = current_rebase_case(self)
        original = case.checkpoint.get('repo', 'G-047')
        observed = {'head': 'owned-head', 'dirty': {}, 'index': {}}
        with (
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(loop_manager, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)),
            mock.patch.object(loop_manager, '_owned_rebase_journal_state', return_value=observed),
        ):
            for field, value in (('head', 'before'), ('dirty', {'app.py': 'older'}), ('index', {'app.py': 'older'})):
                with self.subTest(field=field), self.assertRaisesRegex(journal.MutationEvidenceError, 'observation drift'):
                    loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                        Path('/state/G-047'), 'base', repo_path=Path('/repo'), branch='loop-manager/G-047',
                        agent_session_id=None, preserved_observation={**observed, field: value})
                self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
            loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                Path('/state/G-047'), 'base', repo_path=Path('/repo'), branch='loop-manager/G-047',
                agent_session_id=None, preserved_observation=observed)
        self.assertIsNone(case.checkpoint.get('repo', 'G-047').operation)

    def test_blocked_resume_head_keeps_completed_rebase_identity(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        completion = {
            'id': 'sealed', 'run_id': 'run', 'intent_generation': 2,
            'before_head': OLD, 'target_base_oid': NEW,
            'remote_oid': OLD, 'head': NEW,
        }
        before_marker = {
            'owner_oid': OLD, 'before_head': OLD, 'target_base_oid': NEW,
            'after_head': None, 'before_snapshot': 'preserved worktree',
        }
        completed = repository.record(
            'repo', 'G-091', expected_generation=0, operation=None,
            expected_head=NEW, target_base_oid=NEW,
            observed_remote_oid=OLD,
            snapshot={'rebase_completion': completion,
                      'blocked_resume': before_marker},
        )
        loop_manager._record_blocked_resume_head(
            connection, 'repo', 'G-091', OLD, OLD, NEW, NEW,
        )
        saved = repository.get('repo', 'G-091')
        self.assertEqual(saved.generation, completed.generation + 1)
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.expected_head, NEW)
        self.assertEqual(saved.snapshot['rebase_completion'], completion)
        self.assertEqual(
            saved.snapshot['blocked_resume'],
            {**before_marker, 'after_head': NEW},
        )

    def test_direct_rebase_completion_accepts_only_original_restored_stage(self):
        case = current_rebase_case(self)
        case.expected = replace(case.expected, autostash_oid='stash')
        original = case.checkpoint.get('repo', 'G-047')
        clean = case.observed
        with (
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(loop_manager, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)),
        ):
            for pending in (
                replace(clean, index_clean=False, staged=(FileChange('app.py', '100644', '100644', 'old', 'manual', 'M'),)),
                replace(clean, index_clean=False, index=(IndexEntry('app.py', '100644', 'unmerged', 2, 'M', 0x2000),)),
                replace(clean, committed=(FileChange('AGENTS.md', '100644', '100644', 'old', 'foreign', 'M'),)),
            ):
                with self.subTest(pending=pending), self.assertRaises(journal.MutationEvidenceError):
                    case.observed = pending
                    loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                        Path('/state/G-047'), 'base', repo_path=Path('/repo'), branch='loop-manager/G-047', agent_session_id=None)
                self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
            case.observed = replace(clean, committed=(FileChange('app.py', '100644', '100755', 'old', 'manual-new-content', 'M'),))
            loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                Path('/state/G-047'), 'base', repo_path=Path('/repo'), branch='loop-manager/G-047', agent_session_id=None)
        self.assertIsNone(case.checkpoint.get('repo', 'G-047').operation)


class DetachedRebaseOwnershipTests(unittest.TestCase):
    def test_rebase_start_retry_reuses_frozen_run_and_generation(self):
        case = current_rebase_case(self)
        case.observed = replace(case.observed, head='before', branch_ref=('before', ''),
            registration=('HEAD=before', 'branch=refs/heads/loop-manager/G-047'), base_is_ancestor=False,
            committed=(), staged=(), index_clean=True)
        original = case.checkpoint.get('repo', 'G-047')
        with (
            mock.patch.object(journal, 'begin_current_rebase', return_value='audit-only') as begin,
            mock.patch('execution_handlers.managed_rebase_evidence.source_commits', side_effect=AssertionError('no replay')),
        ):
            case.handler._record_rebase_intent(case.fixture.context(), 'base')
            case.handler._record_rebase_intent(case.fixture.context(), 'base')
            self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
            self.assertEqual(begin.call_count, 2)
            self.assertEqual(begin.call_args_list[0].kwargs['expected'], begin.call_args_list[1].kwargs['expected'])
        case.observed = replace(case.observed, remote=replace(case.observed.remote, oid='race'))
        with mock.patch.object(journal, 'begin_current_rebase') as begin:
            with self.assertRaises(journal.MutationEvidenceError):
                case.handler._record_rebase_intent(case.fixture.context(), 'base')
            begin.assert_not_called()

    def test_stage_capture_binds_modes_to_approved_file_signatures(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            path = worktree / 'app.py'
            path.write_text('approved content')
            path.chmod(0o644)
            signature = loop_manager._path_signature(worktree, 'app.py')
            observed = SimpleNamespace(
                head=OLD, branch=None, index={},
                dirty={'app.py': (('unstaged',), signature),
                       'removed.py': (('unstaged',), 'missing')})
            handler = object.__new__(ExecutionHandlers)
            handler.repo = 'repo'
            handler.effects = SimpleNamespace(
                agent=SimpleNamespace(capture_git_snapshot=lambda *_: observed,
                                      path_signature=loop_manager._path_signature),
                checkpoint=SimpleNamespace(get=lambda *_: SimpleNamespace(
                    snapshot={'rebase_evidence_version': 2})))
            context = SimpleNamespace(worktree=worktree,
                                      plan=SimpleNamespace(plan_id='G-099'))
            evidence = handler._rebase_stage_evidence(context)
            self.assertEqual(evidence['modes'], {'app.py': '100644', 'removed.py': None})
            path.write_text('changed after approval')
            with self.assertRaisesRegex(RuntimeError, 'changed while capturing'):
                handler._rebase_stage_evidence(context)

    def test_versioned_continuation_binds_source_turn_and_stage_before_git(self):
        case = pending_current_case(self)
        with (
            mock.patch.object(journal, 'begin_current_rebase', return_value='current-step') as begin,
            mock.patch('execution_handlers.managed_rebase_evidence.source_commits', side_effect=AssertionError('no historical stage')),
        ):
            token = case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE)
        self.assertEqual(token, 'current-step')
        self.assertEqual(begin.call_args.kwargs['expected']['before_head'], 'before')
        self.assertEqual(begin.call_args.kwargs['expected']['target_base_oid'], 'base')
        self.assertEqual(begin.call_args.kwargs['expected']['action'], 'continue')
        self.assertNotIn('source_commits', begin.call_args.kwargs['expected'])
        case.observed = replace(case.observed, metadata=(('head-name', 'refs/heads/other'), ('orig-head', 'before'), ('onto', 'base')))
        with mock.patch.object(journal, 'begin_current_rebase') as begin:
            with self.assertRaises(journal.MutationEvidenceError):
                case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE)
            begin.assert_not_called()

    def test_skip_checks_approved_index_before_recording_intent(self):
        case = pending_current_case(self)
        case.observed = replace(case.observed, index_clean=True, staged=())
        with mock.patch.object(journal, 'begin_current_rebase', return_value='skip-step') as begin:
            self.assertEqual(case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.SKIP), 'skip-step')
        self.assertEqual(begin.call_args.kwargs['expected']['action'], 'skip')
        case.observed = replace(case.observed, index=(IndexEntry('app.py', '100644', 'blob', 0, 'h', 0x8000),))
        with mock.patch.object(journal, 'begin_current_rebase') as begin:
            with self.assertRaises(journal.MutationEvidenceError):
                case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.SKIP)
            begin.assert_not_called()

    def test_continuation_intent_carries_only_matching_saved_stage_files(self):
        case = pending_current_case(self)
        current = case.checkpoint.get('repo', 'G-047')
        self.assertEqual(current.snapshot['stage_evidence'], {'old': 'audit'})
        with mock.patch.object(journal, 'begin_current_rebase', return_value='manual') as begin:
            case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE)
        self.assertNotIn('stage_evidence', begin.call_args.kwargs['expected'])
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), current)
        case.observed = replace(case.observed, staged=(FileChange('AGENTS.md', '100644', '100644', 'old', 'foreign', 'M'),))
        with mock.patch.object(journal, 'begin_current_rebase') as begin:
            with self.assertRaises(journal.MutationEvidenceError):
                case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE)
            begin.assert_not_called()

    def test_dropped_rebase_patch_leaves_journal_unverified_for_no_change_gate(self):
        handler = object.__new__(ExecutionHandlers)
        handler.effects = SimpleNamespace(
            checkpoint=None,
            workspace=SimpleNamespace(
                _rebase_in_progress_for_branch=lambda *_: False,
                branch_has_changes=mock.Mock(return_value=False),
            ),
        )
        handler.base = 'main'
        handler._unmerged_stages = lambda *_: ''
        handler._finish_rebase_journal = mock.Mock(side_effect=
            journal.MutationEvidenceError('patch was dropped'))
        context = SimpleNamespace(worktree=Path('/worktree'), branch=BRANCH)
        self.assertIsNone(handler._post_rebase_observation(
            context, NEW, 'rebase completed',
        ))
        handler._finish_rebase_journal.assert_called_once()
        handler.effects.workspace.branch_has_changes.return_value = True
        with self.assertRaises(journal.MutationEvidenceError):
            handler._post_rebase_observation(context, NEW, 'rebase completed')

    def test_continuation_checks_saved_stage_commit_instead_of_final_file(self):
        case = pending_current_case(self)
        # The current manually resolved blob may differ from every historical approval.
        case.observed = replace(case.observed, index=(IndexEntry('app.py', '100755', 'manual', 0, 'H', 0),),
            staged=(FileChange('app.py', '100644', '100755', 'old', 'manual', 'M'),))
        with mock.patch.object(journal, 'begin_current_rebase', return_value='current'):
            self.assertEqual(case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE), 'current')
        case.observed = replace(case.observed, operations=(*case.observed.operations, ('CHERRY_PICK_HEAD', 'foreign')))
        with mock.patch.object(journal, 'begin_current_rebase') as begin:
            with self.assertRaises(journal.MutationEvidenceError):
                case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE)
            begin.assert_not_called()

    def test_legacy_skip_without_source_receipt_is_rejected(self):
        case = pending_current_case(self)
        self.assertNotIn('rebase_evidence_version', case.checkpoint.get('repo', 'G-047').snapshot)
        case.observed = replace(case.observed, index_clean=True, staged=())
        with (
            mock.patch.object(journal, 'begin_current_rebase', return_value='legacy-current'),
            mock.patch.object(journal, 'verified_managed_transitions', side_effect=AssertionError('no receipts')),
        ):
            self.assertEqual(case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.SKIP), 'legacy-current')
        case.observed = replace(case.observed, metadata=(('head-name', 'refs/heads/loop-manager/G-047'), ('orig-head', 'before'), ('onto', 'wrong-base')))
        with self.assertRaises(journal.MutationEvidenceError):
            case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.SKIP)

    def test_resumed_continue_uses_the_saved_journal_identity(self):
        git = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=''))
        handler = object.__new__(ExecutionHandlers)
        handler.effects = SimpleNamespace(workspace=SimpleNamespace(git=git))
        handler._begin_rebase_continuation = mock.Mock(return_value='owned-token')
        context = SimpleNamespace(worktree=Path('/owned'))
        handler._execute_rebase_step(context, CorrectionAction.CONTINUE)
        self.assertEqual(
            git.call_args.kwargs['env']['GIT_REFLOG_ACTION'],
            'loop-manager-rebase:owned-token',
        )

    def test_conflict_dispatch_waits_for_journal_result(self):
        events = []
        handler = object.__new__(ExecutionHandlers)
        handler.effects = SimpleNamespace(
            checkpoint=None,
            workspace=SimpleNamespace(
                _rebase_in_progress_for_branch=lambda *_: True,
            ),
            state=SimpleNamespace(update_project=mock.Mock()),
            contract=SimpleNamespace(REBASE_CONFLICT_PREFIX='conflict: '),
        )
        handler.repo = 'repo'
        handler._unmerged_stages = lambda *_: 'unmerged'
        handler._finish_rebase_journal = lambda *_args, **_kwargs: events.append('journal')
        handler._rebase_conflict = lambda *_args, **_kwargs: events.append('worker')
        context = SimpleNamespace(worktree=Path('/owned'), branch=BRANCH)
        handler._post_rebase_observation(context, NEW, 'conflict')
        self.assertEqual(events, ['journal', 'worker'])

    def test_rebase_preserves_allowed_file_contents_across_staging_change(self):
        before = {'app.py': [['staged'], 'content-hash']}
        after = {'app.py': [['unstaged'], 'content-hash']}
        self.assertTrue(rebase_preserves_worktree_files(before, after))
        self.assertFalse(rebase_preserves_worktree_files(
            before, {'app.py': [['unstaged'], 'external-edit']},
        ))
        self.assertFalse(rebase_preserves_worktree_files(
            before, {**after, 'external.py': [['untracked'], 'extra']},
        ))

    def test_pure_gate_rejects_missing_or_mismatched_evidence(self):
        evidence = dict(
            operation='rebase', expected_head=OLD, target_base_oid=NEW,
            recorded_before_head=OLD, branch=BRANCH, remote_oid=OLD,
            accepted_remote_oid=OLD, recorded_remote_oid=OLD,
            checkpoint_remote_oid=OLD,
            head_name=f'refs/heads/{BRANCH}', orig_head=OLD, onto=NEW,
        )
        self.assertTrue(owned_detached_rebase(**evidence))
        for change in ({'operation': None}, {'orig_head': NEW},
                       {'onto': OLD}, {'remote_oid': NEW},
                       {'checkpoint_remote_oid': NEW},
                       {'recorded_before_head': NEW}, {'head_name': 'refs/heads/other'}):
            with self.subTest(change=change):
                self.assertFalse(owned_detached_rebase(**{**evidence, **change}))

    def test_published_conflict_reaches_owned_gate_after_restart(self):
        case = pending_current_case(self)
        row = dict(case.state.row(), publication_remote_oid='remote')
        checkpoint = {'accepted_remote_oid': 'remote'}
        with (
            mock.patch('execution_handlers.checkpoint_ownership.owned_remote_oid', return_value='remote'),
            mock.patch.object(journal, 'verified_managed_transitions', side_effect=AssertionError('no receipt gate')),
        ):
            for _ in range(2):
                self.assertTrue(case.handler._owned_detached_rebase(case.fixture.context(), row, 'remote',
                    case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            case.observed = replace(case.observed, metadata=(('head-name', 'refs/heads/other'), ('orig-head', 'before'), ('onto', 'base')))
            self.assertFalse(case.handler._owned_detached_rebase(case.fixture.context(), row, 'remote',
                case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            self.assertFalse(case.handler._owned_detached_rebase(case.fixture.context(), row, 'race',
                case.checkpoint.get('repo', 'G-047'), checkpoint, None))

    def test_completed_published_rebase_requires_exact_saved_result(self):
        case = current_rebase_case(self)
        row = dict(case.state.row(), publication_remote_oid='remote')
        checkpoint = {'accepted_remote_oid': 'remote'}
        with (
            mock.patch('execution_handlers.checkpoint_ownership.owned_remote_oid', return_value='remote'),
            mock.patch.object(journal, 'verified_managed_transitions', side_effect=AssertionError('no journal gate')),
        ):
            self.assertTrue(case.handler._owned_completed_rebase(case.fixture.context(), row, 'remote',
                case.checkpoint.get('repo', 'G-047'), checkpoint, None, case.observed.head))
            # A rewritten current head is accepted while its scope/target/lease stays owned.
            case.observed = replace(case.observed, head='manual-rewrite', branch_ref=('manual-rewrite', ''),
                registration=('HEAD=manual-rewrite', 'branch=refs/heads/loop-manager/G-047'))
            self.assertTrue(case.handler._owned_completed_rebase(case.fixture.context(), row, 'remote',
                case.checkpoint.get('repo', 'G-047'), checkpoint, None, case.observed.head))
            case.observed = replace(case.observed, remote=replace(case.observed.remote, oid='external'))
            self.assertFalse(case.handler._owned_completed_rebase(case.fixture.context(), row, 'remote',
                case.checkpoint.get('repo', 'G-047'), checkpoint, None, case.observed.head))



class CurrentRebaseRuntimeAdapterTests(unittest.TestCase):
    """The production factory's identity/input adapters, with no Git process."""
    def test_factory_uses_current_inputs_exact_lease_and_final_scope(self):
        from tests.test_runtime_state import CurrentRebaseRuntimeTests
        from managed_rebase_runtime import build_service
        from tests.test_managed_rebase_validation import observation
        fixture = CurrentRebaseRuntimeTests()
        connection, runtime, checkpoints, original, expected, decision = fixture.fixture()
        self.addCleanup(fixture.doCleanups)
        ports = mock.Mock()
        ports.WORKSPACE_OWNED = 1
        ports.git.side_effect = lambda _root, *args, **kwargs: SimpleNamespace(
            stdout='/repo/.git' if args == ('rev-parse', '--git-common-dir') else '', returncode=0)
        ports.requirement_archive_candidates.return_value = ()
        ports._published_plan_allows_common_plans.return_value = False
        ports._revision_execplan_input_fingerprint.return_value = 'published-input'
        ports._worktree_execplan_input_fingerprint.return_value = 'current-input'
        ports._owned_rebase_metadata.return_value = None
        ports.manager_git_journal.live_remote_branch_oid.return_value = 'remote'
        ports.pr_for_branch.return_value = None
        ports._worker_path_allowed.side_effect = loop_manager._worker_path_allowed
        service = build_service(ports, connection, Path('/repo'), Path('/state'),
                                'repo', 'G-047', Path('/owned'), 'loop-manager/G-047')
        self.assertEqual(service.expected.remote.oid, 'remote')
        self.assertEqual(service.expected.before_head, 'before')
        self.assertIsNone(service.adapters.read_remote(service.expected).pull_request)
        self.assertTrue(service.adapters.path_allowed('app.py'))
        self.assertFalse(service.adapters.path_allowed('docs/ARCHITECTURE.md'))
        self.assertFalse(service.adapters.path_allowed('docs/plans/[G-048] Other.md'))
        first = service.adapters.read_inputs(service.expected)
        ports._worktree_execplan_input_fingerprint.return_value = 'edited-plan'
        self.assertNotEqual(service.adapters.read_inputs(service.expected), first)
        ports.validate_execplan_workspace_identity.assert_not_called()
        service.adapters.validate_identity(service.expected)
        ports.validate_execplan_workspace_identity.assert_called_once()
        ports.validate_active_execplan_metadata.assert_called_once()
        # Scope predicates receive frozen contract results and perform no I/O.
        ports.reset_mock()
        self.assertTrue(service.adapters.contracts_allowed(observation(service.expected)))
        self.assertEqual(ports.mock_calls, [])

    def test_continuation_needs_current_owned_state_not_worker_turn(self):
        from managed_rebase_validation import RebaseDecision, RebaseStatus, Remote
        handler = object.__new__(ExecutionHandlers)
        handler.repo, handler.repo_path, handler.state_dir = 'repo', Path('/repo'), Path('/state')
        checkpoint = SimpleNamespace(operation='rebase', expected_head=OLD, target_base_oid=NEW,
                                     snapshot={})
        service = mock.Mock()
        service.require_owned.return_value = (SimpleNamespace(remote=Remote('repo', BRANCH, OLD)),
                                               RebaseDecision(RebaseStatus.WAITING))
        handler.effects = SimpleNamespace(checkpoint=SimpleNamespace(get=lambda *_: checkpoint),
                                          workspace=SimpleNamespace(current_rebase_service=lambda _: service))
        handler._rebase_journal_state = lambda _: {'head': 'partial'}
        context = SimpleNamespace(plan=SimpleNamespace(plan_id='G-091'), worktree=Path('/owned'),
                                  branch=BRANCH, worker_session_id=None)
        with mock.patch.object(journal, 'begin_current_rebase', return_value='current') as begin:
            self.assertEqual(handler._begin_rebase_continuation(context, CorrectionAction.CONTINUE), 'current')
            self.assertNotIn('correction_turn', begin.call_args.kwargs['expected'])
            self.assertNotIn('source_commits', begin.call_args.kwargs['expected'])
            service.require_owned.side_effect = journal.MutationEvidenceError('scope mismatch')
            with self.assertRaises(journal.MutationEvidenceError):
                handler._begin_rebase_continuation(context, CorrectionAction.CONTINUE)
            self.assertEqual(begin.call_count, 1)


def pending_current_case(test):
    case = current_rebase_case(test)
    case.observed = replace(case.observed, head='partial', branch=None, branch_ref=('before', ''),
        registration=('HEAD=partial', 'detached=True'), branch_worktrees=(),
        operations=(('rebase-merge', 'operation'),),
        metadata=(('head-name', 'refs/heads/loop-manager/G-047'), ('orig-head', 'before'), ('onto', 'base')),
        index_clean=False, index=(IndexEntry('app.py', '100644', 'manual', 0, 'H', 0),),
        staged=(FileChange('app.py', '100644', '100644', 'old', 'manual', 'M'),))
    case.handler._begin_rebase_continuation = ExecutionHandlers._begin_rebase_continuation.__get__(case.handler)
    return case


if __name__ == '__main__':
    unittest.main()
