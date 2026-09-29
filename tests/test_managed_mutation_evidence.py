"""Narrow ownership checks for managed workspace and rebase journal entries."""

from pathlib import Path
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
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            state.mkdir()
            repo = Path(directory) / 'repo'
            repo.mkdir()
            repo_key = str(repo.resolve())
            _branch, worktree = loop_manager.expected_execplan_workspace(
                state, repo_key, 'G-091',
            )
            worktree.parent.mkdir(parents=True)
            worktree.mkdir()
            before = {
                'head': OLD, 'branch': BRANCH, 'ref': [OLD, ''],
                'index': {}, 'dirty': {},
                'registration': [f'HEAD={OLD}', f'branch=refs/heads/{BRANCH}'],
                'operations': {}, 'metadata': None, 'index_clean': True,
            }
            observed = before.copy()
            connection = sqlite3.connect(':memory:')
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            ExecutionCheckpointRepository(connection)
            reflog = []
            live_oid = [OLD]

            def git(_path, *args, **kwargs):
                if args[0] == 'ls-remote':
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f'{live_oid[0]}\trefs/heads/{BRANCH}\n',
                    )
                if args[0] == 'merge-base':
                    return SimpleNamespace(returncode=0, stdout=OLD + '\n')
                if args[0] == 'rev-list':
                    return SimpleNamespace(returncode=0, stdout='')
                if args[0] == 'rebase':
                    observed.update(
                        head=NEW, ref=[NEW, ''],
                        registration=[f'HEAD={NEW}', f'branch=refs/heads/{BRANCH}'],
                    )
                    reflog.append(
                        f"{NEW}\0{kwargs['env']['GIT_REFLOG_ACTION']}"
                        f' (finish): refs/heads/{BRANCH} onto {NEW}'
                    )
                    return SimpleNamespace(returncode=0, stdout='')
                if args[0] == 'reflog':
                    self.assertEqual(args, ('reflog', 'show', '-1',
                                            '--format=%H%x00%gs', f'refs/heads/{BRANCH}'))
                    return SimpleNamespace(returncode=0, stdout='\n'.join(reflog[:1]))
                if args[0] == 'rev-parse':
                    return SimpleNamespace(returncode=0, stdout=observed['head'] + '\n')
                return SimpleNamespace(returncode=0, stdout='')

            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, 'remote_branch_oid',
                                    return_value=OLD),
                  mock.patch.object(loop_manager, '_owned_rebase_journal_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager.checkpoint_ownership,
                                    'rebase_preserves_commits', return_value=True),
                  mock.patch.object(journal, '_finish', side_effect=RuntimeError('crash'))):
                with self.assertRaisesRegex(RuntimeError, 'crash'):
                    loop_manager._journaled_direct_rebase(
                        connection, repo, repo_key, 'G-091', worktree,
                        BRANCH, NEW,
                    )
            live_oid[0] = NEW
            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, 'remote_branch_oid',
                                    return_value=NEW),
                  mock.patch.object(loop_manager, '_owned_rebase_journal_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager.checkpoint_ownership,
                                    'rebase_preserves_commits', return_value=True)):
                with self.assertRaises(journal.MutationEvidenceError):
                    loop_manager._finish_owned_direct_rebase(
                        state, connection, repo, repo_key, 'G-091', worktree,
                        BRANCH,
                    )
            live_oid[0] = OLD
            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, 'remote_branch_oid',
                                    return_value=OLD),
                  mock.patch.object(loop_manager, '_owned_rebase_journal_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager.checkpoint_ownership,
                                    'rebase_preserves_commits', return_value=True)):
                owned = reflog[0]
                saved = observed.copy()
                for head, latest in (
                    (TREE, f'{TREE}\0commit: external same patch'),
                    (NEW, f'{NEW}\0reset: external return'),
                    (TREE, owned),
                    (NEW, owned.replace(' (finish)', '-other (finish)')),
                    (NEW, owned.replace(f'onto {NEW}', f'onto {OLD}')),
                ):
                    with self.subTest(head=head, latest=latest):
                        observed.update(head=head, ref=[head, ''], registration=[
                            f'HEAD={head}', f'branch=refs/heads/{BRANCH}',
                        ])
                        reflog[:] = [latest, owned]
                        with self.assertRaises(journal.MutationEvidenceError):
                            loop_manager._finish_owned_direct_rebase(
                                state, connection, repo, repo_key, 'G-091', worktree,
                                BRANCH,
                            )
                        self.assertTrue(list((state / 'manager-git-journal').glob('*/active.json')))
                        self.assertFalse(list((state / 'manager-git-journal').glob('*/*.result.json')))
                observed.clear()
                observed.update(saved)
                reflog[:] = [owned]
                self.assertTrue(loop_manager._finish_owned_direct_rebase(
                    state, connection, repo, repo_key, 'G-091', worktree,
                    BRANCH,
                ))
            self.assertEqual(len(journal.verified_managed_transitions(
                state, repo_key, 'plan:G-091', git=git,
            )), 1)
            live_oid[0] = NEW
            with self.assertRaises(journal.MutationEvidenceError):
                journal.verified_managed_transitions(
                    state, repo_key, 'plan:G-091', git=git,
                )
            live_oid[0] = OLD
            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, '_owned_rebase_journal_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager.runtime_state,
                                    'RuntimeStateRepository') as runtime):
                runtime.return_value.projected.return_value = None
                loop_manager._complete_direct_rebase_checkpoint(
                    connection, repo_key, 'G-091', worktree, NEW,
                    repo_path=repo, branch=BRANCH, agent_session_id=None,
                )
            saved = ExecutionCheckpointRepository(connection).get(repo_key, 'G-091')
            self.assertIsNone(saved.operation)
            self.assertEqual(saved.snapshot['rebase_completion']['head'], NEW)

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
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        original_marker = {
            'owner_oid': OLD, 'before_head': OLD, 'target_base_oid': NEW,
            'after_head': None, 'before_snapshot': 'preserved worktree',
        }
        recorded_marker = {**original_marker, 'after_head': NEW}
        repository.record(
            'repo', 'G-091', expected_generation=0, operation='rebase',
            expected_head=OLD, target_base_oid=NEW,
            observed_remote_oid=OLD,
            snapshot={'rebase_run_id': 'run', 'blocked_resume': recorded_marker},
        )
        sealed = {
            'head': NEW, 'branch': BRANCH, 'ref': [NEW, ''],
            'registration': ['owned'], 'operations': {}, 'metadata': None,
            'index': {}, 'dirty': {},
        }
        observed = {**sealed, 'dirty': {'app.py': [['staged'], 'accepted']}}
        transition = {
            'intent': {'id': 'sealed', 'kind': 'managed_rebase',
                       'target': '/owned', 'expected': {
                           'before_head': OLD, 'target_base_oid': NEW,
                           'remote_oid': OLD, 'run_id': 'run'}},
            'result': {'observed': sealed},
        }
        with (mock.patch.object(loop_manager, '_owned_rebase_state_dir',
                                return_value=Path('/state')),
              mock.patch.object(loop_manager, '_owned_rebase_journal_state',
                                return_value=observed),
              mock.patch.object(loop_manager, 'capture_git_snapshot',
                                return_value=SimpleNamespace()),
              mock.patch.object(loop_manager, '_blocked_resume_worktree_matches',
                                side_effect=lambda _path, marker, _snapshot:
                                marker['after_head'] is None),
              mock.patch.object(journal, 'verified_managed_transitions',
                                return_value=[transition]),
              mock.patch.object(loop_manager.runtime_state,
                                'RuntimeStateRepository') as runtime):
            runtime.return_value.projected.return_value = None
            with self.assertRaises(journal.MutationEvidenceError):
                loop_manager._complete_direct_rebase_checkpoint(
                    connection, 'repo', 'G-091', Path('/owned'), NEW,
                    repo_path=Path('/repo'), branch=BRANCH,
                    agent_session_id=None, preserved_worktree=True,
                )
            with self.assertRaises(journal.MutationEvidenceError):
                loop_manager._complete_direct_rebase_checkpoint(
                    connection, 'repo', 'G-091', Path('/owned'), NEW,
                    repo_path=Path('/repo'), branch=BRANCH,
                    agent_session_id=None, preserved_worktree=True,
                    preserved_marker=original_marker,
                    preserved_observation={**observed, 'head': OLD},
                )
            loop_manager._complete_direct_rebase_checkpoint(
                connection, 'repo', 'G-091', Path('/owned'), NEW,
                repo_path=Path('/repo'), branch=BRANCH,
                agent_session_id=None, preserved_worktree=True,
                preserved_marker=original_marker,
                preserved_observation=observed,
            )
        saved = repository.get('repo', 'G-091')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['rebase_completion']['id'], 'sealed')


class DetachedRebaseOwnershipTests(unittest.TestCase):
    def test_rebase_start_retry_reuses_frozen_run_and_generation(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        checkpoints = ExecutionCheckpointRepository(connection)
        handler = object.__new__(ExecutionHandlers)
        handler.repo, handler.repo_path, handler.state_dir = 'repo', Path('/repo'), Path('/state')
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-099'), branch='loop-manager/G-099',
            worktree=Path('/worktree'), worker_session_id='worker-session',
        )

        def git(_path, *args, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=OLD + '\n' if args == ('rev-parse', 'HEAD') else '',
            )

        handler.effects = SimpleNamespace(
            checkpoint=checkpoints,
            workspace=SimpleNamespace(git=git, remote_branch_oid=lambda *_: None),
        )
        handler._row = lambda: {'pr_head_oid': None, 'pr_conflict_head_oid': None}
        handler._rebase_journal_state = lambda *_: {'head': OLD, 'metadata': None}
        with (mock.patch.object(journal, 'live_remote_branch_oid', return_value=None),
              mock.patch('execution_handlers.managed_rebase_evidence.source_commits',
                         return_value=(OLD, [TREE])),
              mock.patch.object(journal, 'rebase_affected_paths', return_value=['app.py']),
              mock.patch.object(journal, 'begin_rebase', return_value='same-intent') as begin):
            handler._record_rebase_intent(context, NEW)
            first = checkpoints.get('repo', 'G-099')
            handler._record_rebase_intent(context, NEW)
            second = checkpoints.get('repo', 'G-099')
            self.assertEqual(first.generation, second.generation)
            self.assertEqual(first.snapshot['rebase_run_id'],
                             second.snapshot['rebase_run_id'])
            self.assertEqual(begin.call_args_list[0].kwargs['expected'],
                             begin.call_args_list[1].kwargs['expected'])

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
        handler = object.__new__(ExecutionHandlers)
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-091'), branch=BRANCH,
            worktree=Path('/worktree'), worker_session_id='worker-session',
        )
        pre_index = {'app.py': [f'H 100644 {OLD} 1']}
        staged_index = {'app.py': [f'H 100644 {TREE} 0']}
        before = {
            'head': NEW, 'branch': None, 'index': staged_index,
            'dirty': {'app.py': [['staged'], 'approved']},
            'index_clean': False, 'unmerged': False,
        }
        snapshot = {
            'pending': 'continue', 'worker_result': 'saved-result',
            'stage_evidence': {'head': NEW, 'branch': None,
                               'files': {'app.py': 'approved'}, 'index': pre_index,
                               'modes': {'app.py': '100644'}},
            'rebase_evidence_version': 2, 'rebase_run_id': 'run-one',
            'rebase_source_base': OLD, 'rebase_source_commits': [TREE],
            'rebase_remote_oid': OLD,
        }
        checkpoint = SimpleNamespace(
            operation='rebase', expected_head=OLD, target_base_oid=NEW,
            generation=7, snapshot=snapshot,
        )
        correction = SimpleNamespace(
            pending_turn='turn-one', worker_result='saved-result',
            worker_session='worker-session', generation=9,
        )
        diff_code = [0]

        def git(_path, *args, **_kwargs):
            if args[0] == 'write-tree':
                return SimpleNamespace(stdout=TREE + '\n', returncode=0)
            if args[:3] == ('rev-parse', '--verify', 'REBASE_HEAD'):
                return SimpleNamespace(stdout=TREE + '\n', returncode=0)
            if args[:2] == ('diff', '--quiet'):
                return SimpleNamespace(stdout='', returncode=diff_code[0])
            raise AssertionError(args)

        handler.effects = SimpleNamespace(
            checkpoint=SimpleNamespace(get=lambda *_: checkpoint),
            workspace=SimpleNamespace(git=git, remote_branch_oid=lambda *_: OLD),
        )
        handler.rebase_corrections = SimpleNamespace(get=lambda *_: correction)
        handler.repo, handler.repo_path, handler.state_dir = 'repo', Path('/repo'), Path('/state')
        handler._rebase_journal_state = lambda *_: before.copy()
        with (mock.patch.object(journal, 'live_remote_branch_oid', return_value=OLD),
              mock.patch.object(journal, 'rebase_affected_paths', return_value=['app.py']),
              mock.patch.object(journal, 'begin_rebase', return_value='intent') as begin):
            self.assertEqual(handler._begin_rebase_continuation(
                context, CorrectionAction.CONTINUE,
            ), 'intent')
            expected = begin.call_args.kwargs['expected']
            self.assertEqual(expected['source_commit'], TREE)
            self.assertEqual(expected['correction_turn'], 'turn-one')
            self.assertEqual(expected['staged_index'], staged_index)
            self.assertEqual(expected['accepted_modes'], {'app.py': '100644'})
            # core.fileMode=false can report a clean worktree diff even when
            # update-index --chmod changes only the staged executable bit.
            before['index'] = {'app.py': [f'H 100755 {TREE} 0']}
            with self.assertRaises(journal.MutationEvidenceError):
                handler._begin_rebase_continuation(context, CorrectionAction.CONTINUE)
            self.assertEqual(begin.call_count, 1)
            before['index'] = staged_index
            diff_code[0] = 1
            with self.assertRaises(journal.MutationEvidenceError):
                handler._begin_rebase_continuation(context, CorrectionAction.CONTINUE)
            self.assertEqual(begin.call_count, 1)
            diff_code[0] = 0
            correction.pending_turn = None
            with self.assertRaises(journal.MutationEvidenceError):
                handler._begin_rebase_continuation(context, CorrectionAction.CONTINUE)
            self.assertEqual(begin.call_count, 1)

    def test_skip_checks_approved_index_before_recording_intent(self):
        handler = object.__new__(ExecutionHandlers)
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-099'), branch=BRANCH,
            worktree=Path('/worktree'), worker_session_id='session',
        )
        original = {'app.py': [f'H 100644 {OLD} 1'],
                    'other.py': [f'H 100644 {OLD} 0']}
        staged = {'app.py': [f'H 100644 {NEW} 0'],
                  'other.py': original['other.py']}
        before = dict(head=NEW, branch=None, index=staged,
                      dirty={}, index_clean=True, unmerged=False)
        snapshot = dict(
            pending='continue', worker_result='approved',
            stage_evidence=dict(head=NEW, branch=None,
                                files={'app.py': 'signature'}, index=original,
                                modes={'app.py': '100644'}),
            rebase_evidence_version=2, rebase_run_id='run',
            rebase_source_base=OLD, rebase_source_commits=[TREE],
            rebase_remote_oid=None,
        )
        handler.repo, handler.repo_path, handler.state_dir = 'repo', Path('/repo'), Path('/state')
        live_signature = ['signature']
        handler.effects = SimpleNamespace(
            agent=SimpleNamespace(path_signature=lambda *_: live_signature[0]),
            checkpoint=SimpleNamespace(get=lambda *_: SimpleNamespace(
                operation='rebase', expected_head=OLD, target_base_oid=NEW,
                generation=3, snapshot=snapshot)),
            workspace=SimpleNamespace(
                git=lambda *_a, **_k: SimpleNamespace(stdout=TREE, returncode=0),
                remote_branch_oid=lambda *_: None),
        )
        handler.rebase_corrections = SimpleNamespace(get=lambda *_: SimpleNamespace(
            pending_turn='turn', worker_result='approved', worker_session='session',
            generation=2))
        handler._rebase_journal_state = lambda *_: before.copy()
        with (mock.patch.object(journal, 'live_remote_branch_oid', return_value=None),
              mock.patch.object(journal, 'rebase_affected_paths', return_value=['app.py']),
              mock.patch.object(journal, 'begin_rebase', return_value='intent') as begin):
            self.assertEqual(handler._begin_rebase_continuation(
                context, CorrectionAction.SKIP), 'intent')
            self.assertEqual(begin.call_args.kwargs['expected']['skip_observed_files'],
                             {'app.py': 'signature'})
            for changed in ('unstaged content', 'missing'):
                with self.subTest(signature=changed):
                    live_signature[0] = changed
                    before['dirty'] = {'app.py': [['unstaged'], changed]}
                    with self.assertRaisesRegex(journal.MutationEvidenceError, 'signatures'):
                        handler._execute_rebase_step(context, CorrectionAction.SKIP)
                    self.assertEqual(begin.call_count, 1)
            # Also read clean tracked paths directly instead of trusting dirty.
            before['dirty'] = {}
            with self.assertRaisesRegex(journal.MutationEvidenceError, 'signatures'):
                handler._begin_rebase_continuation(context, CorrectionAction.SKIP)
            live_signature[0] = 'signature'
            # A changing snapshot during signature capture is not a stable receipt.
            snapshots = iter((before.copy(), before.copy(), {**before, 'head': OLD}))
            handler._rebase_journal_state = lambda *_: next(snapshots)
            with self.assertRaisesRegex(journal.MutationEvidenceError, 'signatures'):
                handler._begin_rebase_continuation(context, CorrectionAction.SKIP)
            handler._rebase_journal_state = lambda *_: before.copy()
            for index in (
                {**staged, 'app.py': [f'S 100644 {NEW} 0']},
                {**staged, 'app.py': [f'h 100644 {NEW} 0']},
                {**staged, 'other.py': [f'S 100644 {OLD} 0']},
                {**staged, 'other.py': [f'H 100755 {OLD} 0']},
            ):
                with self.subTest(index=index):
                    before['index'] = index
                    with self.assertRaisesRegex(journal.MutationEvidenceError, 'rebase skip'):
                        handler._begin_rebase_continuation(context, CorrectionAction.SKIP)
            self.assertEqual(begin.call_count, 1)
            before['index'] = staged
            snapshot['stage_evidence'].pop('index')
            with self.assertRaises(journal.MutationEvidenceError):
                handler._begin_rebase_continuation(context, CorrectionAction.SKIP)
            self.assertEqual(begin.call_count, 1)

    def test_continuation_intent_carries_only_matching_saved_stage_files(self):
        handler = object.__new__(ExecutionHandlers)
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-091'), branch=BRANCH,
            worktree=Path('/worktree'), worker_session_id='owned-session',
        )
        checkpoint = SimpleNamespace(
            operation='rebase', expected_head=OLD, target_base_oid=NEW,
            snapshot={'pending': 'continue', 'stage_evidence': {
                'head': OLD, 'branch': None, 'files': {'app.py': 'accepted'},
            }},
        )
        handler.effects = SimpleNamespace(
            checkpoint=SimpleNamespace(get=lambda *_: checkpoint),
            workspace=SimpleNamespace(
                remote_branch_oid=lambda *_: OLD,
                git=lambda *_: SimpleNamespace(stdout=TREE + '\n'),
            ),
        )
        handler.repo = 'repo'
        handler.repo_path = Path('/repo')
        handler.state_dir = Path('/state')
        handler._rebase_journal_state = lambda *_: {'head': OLD, 'branch': None}
        with mock.patch.object(journal, 'begin_rebase', return_value='owned') as begin:
            handler._begin_rebase_continuation(context, CorrectionAction.CONTINUE)
            self.assertEqual(
                begin.call_args.kwargs['expected']['accepted_files'],
                {'app.py': 'accepted'},
            )
            self.assertEqual(
                begin.call_args.kwargs['expected']['staged_tree_oid'], TREE,
            )
            checkpoint.snapshot['stage_evidence']['head'] = NEW
            handler._begin_rebase_continuation(context, CorrectionAction.CONTINUE)
            self.assertIsNone(begin.call_args.kwargs['expected']['accepted_files'])
            self.assertIsNone(begin.call_args.kwargs['expected']['staged_tree_oid'])
            checkpoint.snapshot['stage_evidence']['head'] = OLD
            states = iter(({'head': OLD, 'branch': None},
                           {'head': NEW, 'branch': None}))
            handler._rebase_journal_state = lambda *_: next(states)
            with self.assertRaises(journal.MutationEvidenceError):
                handler._begin_rebase_continuation(
                    context, CorrectionAction.CONTINUE,
                )

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
        handler = object.__new__(ExecutionHandlers)
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-091'), branch=BRANCH,
            worktree=Path('/worktree'), worker_session_id='owned-session',
        )
        checkpoint = SimpleNamespace(
            operation='rebase', expected_head=OLD, target_base_oid=NEW,
            observed_remote_oid=OLD, snapshot={},
        )
        signature = mock.Mock(return_value='accepted-content')
        def git(_path, *args, **_kwargs):
            return SimpleNamespace(
                stdout=f'{NEW}\0loop-manager-rebase:owned-token (finish): refs/heads/{BRANCH} onto {NEW}\n', returncode=0,
            )

        handler.effects = SimpleNamespace(
            checkpoint=SimpleNamespace(get=lambda *_: checkpoint),
            agent=SimpleNamespace(path_signature=signature),
            workspace=SimpleNamespace(
                git=mock.Mock(side_effect=git),
                remote_branch_oid=lambda *_: OLD,
            ),
        )
        handler.repo = 'repo'
        handler.repo_path = Path('/repo')
        handler.state_dir = Path('/state')
        handler._rebase_journal_state = lambda _context: {'head': NEW}
        intent = {
            'id': 'owned-token', 'owner': 'plan:G-091',
            'before': {'head': OLD,
                       'dirty': {'app.py': [['staged'], 'accepted-content']}},
            'expected': {
                'branch': BRANCH, 'before_head': OLD,
                'target_base_oid': NEW, 'remote_oid': OLD,
                'agent_session_id': 'owned-session', 'action': 'continue',
                'accepted_files': {'app.py': 'accepted-content'},
                'staged_tree_oid': TREE,
            },
        }
        observed = {'metadata': None, 'head': NEW}

        def complete(_state, **kwargs):
            return kwargs['validate'](intent, observed)

        with (mock.patch.object(journal, 'complete_rebase', side_effect=complete),
              mock.patch.object(journal, 'verified_rebase_result',
                                return_value={}) as verified,
              mock.patch.object(journal, 'legacy_rebase_replay_proof',
                                return_value=[{'source': OLD, 'replayed': NEW}]) as proof,
              mock.patch('execution_handlers.checkpoint_ownership.rebase_preserves_commits',
                         return_value=False)):
            handler._finish_rebase_journal(context)
            self.assertTrue(verified.call_args.kwargs['accepted_correction'])
            signature.return_value = 'external-edit'
            handler._finish_rebase_journal(context)
            self.assertTrue(verified.call_args.kwargs['accepted_correction'])
            signature.assert_not_called()
            proof.side_effect = journal.MutationEvidenceError('resolution tree mismatch')
            with self.assertRaises(journal.MutationEvidenceError):
                handler._finish_rebase_journal(context)

    def test_legacy_skip_without_source_receipt_is_rejected(self):
        for scenario in ('owned', 'external-head', 'external-return',
                         'wrong-oid', 'other-intent', 'wrong-base', 'missing-base',
                         'missing', 'git-error'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                context = SimpleNamespace(
                    plan=SimpleNamespace(plan_id='G-091'), branch=BRANCH,
                    worktree=state / 'worktree', worker_session_id='owned-session',
                )
                target = str(context.worktree.resolve())
                before = {
                    'head': NEW, 'branch': None, 'ref': [OLD, ''],
                    'index': {}, 'dirty': {},
                    'registration': [f'HEAD={NEW}', 'detached=True'],
                    'metadata': {'orig-head': OLD,
                                 'head-name': f'refs/heads/{BRANCH}', 'onto': NEW},
                }
                expected = {
                    'branch': BRANCH, 'before_head': OLD, 'target_base_oid': NEW,
                    'remote_oid': OLD, 'action': 'skip',
                    'agent_session_id': 'owned-session',
                }
                token = journal.begin_rebase(
                    state, owner='plan:G-091', repo='/repo', target=target,
                    before=before, expected=expected,
                )
                head = TREE if scenario == 'external-head' else NEW
                observed = {
                    'head': head, 'branch': BRANCH, 'ref': [head, ''],
                    'index': {}, 'dirty': {}, 'metadata': None,
                    'registration': [f'HEAD={head}', f'branch=refs/heads/{BRANCH}'],
                    'index_clean': True, 'unmerged': False,
                }
                owned_entry = (
                    f'{NEW}\0loop-manager-rebase:{token} (finish): refs/heads/{BRANCH} onto {NEW}'
                )
                entries = {
                    'owned': [owned_entry],
                    'external-head': [f'{TREE}\0commit: external', owned_entry],
                    'external-return': [f'{NEW}\0reset: external', owned_entry],
                    'wrong-oid': [owned_entry.replace(NEW, TREE, 1)],
                    'wrong-base': [owned_entry.replace(f'onto {NEW}', f'onto {OLD}')],
                    'missing-base': [owned_entry.removesuffix(f' onto {NEW}')],
                    'other-intent': [owned_entry.replace(token, token + '-other')],
                    'missing': [], 'git-error': [owned_entry],
                }[scenario]

                def git(_path, *args, **_kwargs):
                    if args[0] == 'reflog':
                        self.assertEqual(args, (
                            'reflog', 'show', '-1', '--format=%H%x00%gs',
                            f'refs/heads/{BRANCH}',
                        ))
                        return SimpleNamespace(
                            returncode=int(scenario == 'git-error'),
                            stdout='\n'.join(entries[:1]) + '\n',
                        )
                    return SimpleNamespace(returncode=0, stdout='')

                handler = object.__new__(ExecutionHandlers)
                handler.repo, handler.repo_path, handler.state_dir = 'repo', Path('/repo'), state
                handler._rebase_journal_state = lambda *_: observed
                handler.effects = SimpleNamespace(
                    checkpoint=SimpleNamespace(get=lambda *_: SimpleNamespace(
                        operation='rebase', expected_head=OLD, target_base_oid=NEW,
                        observed_remote_oid=OLD, snapshot={},
                    )),
                    workspace=SimpleNamespace(git=git, remote_branch_oid=lambda *_: OLD),
                )
                slot = journal._slot(state, '/repo', 'managed_rebase', target)
                with mock.patch(
                    'execution_handlers.checkpoint_ownership.rebase_preserves_commits',
                    return_value=False,
                ):
                    with self.assertRaises(journal.MutationEvidenceError):
                        handler._finish_rebase_journal(context)
                    self.assertTrue((slot / 'active.json').exists())
                    self.assertFalse((slot / f'{token}.result.json').exists())

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
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            metadata = worktree / 'rebase-merge'
            metadata.mkdir()
            for name, value in (('head-name', f'refs/heads/{BRANCH}'),
                                ('orig-head', OLD), ('onto', NEW)):
                (metadata / name).write_text(value)
            checkpoint = {'phase': 'published', 'accepted_remote_oid': OLD}
            connection = mock.Mock()
            connection.execute.return_value.fetchone.return_value = checkpoint

            def git(_path, *args, **_kwargs):
                if args[0] == 'symbolic-ref':
                    return SimpleNamespace(returncode=1, stdout='')
                if args[:2] == ('rev-parse', '--git-path'):
                    return SimpleNamespace(returncode=0,
                                           stdout=str(worktree / args[2]) + '\n')
                if args[0] == 'rev-parse':
                    return SimpleNamespace(returncode=0, stdout=(
                        OLD if args[-1] == f'refs/heads/{BRANCH}' else NEW
                    ) + '\n')
                return SimpleNamespace(returncode=0, stdout='')

            workspace = SimpleNamespace(
                git=git,
                validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
                _rebase_in_progress_for_branch=mock.Mock(return_value=True),
            )
            current = SimpleNamespace(
                operation='rebase', expected_head=OLD, target_base_oid=NEW,
                observed_remote_oid=OLD,
                snapshot={'before_head': OLD},
            )
            context = SimpleNamespace(
                plan=SimpleNamespace(plan_id='G-091'), branch=BRANCH,
                worktree=worktree, worker_session_id='worker-original',
            )

            def handler():
                instance = object.__new__(ExecutionHandlers)
                instance.effects = SimpleNamespace(
                    state=SimpleNamespace(connection=connection), workspace=workspace,
                )
                instance.state_dir = worktree
                instance.repo = 'repo'
                instance.repo_path = Path('/repo')
                instance._finish_rebase_journal = mock.Mock(return_value=False)
                instance._rebase_journal_state = mock.Mock(return_value=observed)
                return instance

            observed = {'head': NEW, 'branch': None,
                        'metadata': {'orig-head': OLD,
                                     'head-name': f'refs/heads/{BRANCH}',
                                     'onto': NEW}}
            saved = {'intent': {
                'kind': 'managed_rebase', 'target': str(worktree.resolve()),
                'expected': {'before_head': OLD, 'target_base_oid': NEW,
                             'remote_oid': OLD},
            }, 'result': {'observed': observed}}
            with mock.patch('execution_handlers.checkpoint_ownership.owned_remote_oid',
                            return_value=OLD), mock.patch(
                                'execution_handlers.manager_git_journal.verified_managed_transitions',
                                return_value=[],
                            ) as transitions:
                self.assertFalse(handler()._owned_checkpoint_remote(
                    context, {'pr_url': None, 'publication_remote_oid': OLD},
                    OLD, current,
                ))
                transitions.return_value = [saved]
                for _ in range(2):
                    instance = handler()
                    self.assertTrue(instance._owned_checkpoint_remote(
                        context, {'pr_url': None, 'publication_remote_oid': OLD},
                        OLD, current,
                    ))
                    workspace.validate_managed_worktree.assert_called_with(
                        Path('/repo'), worktree, BRANCH, allow_rebase=True,
                    )
                transitions.assert_called_with(
                    worktree, str(Path('/repo').resolve()), 'plan:G-091',
                    agent_session_id='worker-original', git=git,
                )
                corrected = handler()
                corrected._rebase_journal_state.return_value = {
                    **observed, 'index': {'app.py': ['resolved']},
                    'dirty': {'app.py': [['staged'], 'accepted']},
                }
                self.assertTrue(corrected._owned_checkpoint_remote(
                    context, {'pr_url': None, 'publication_remote_oid': OLD},
                    OLD, current,
                ))
                saved['result']['observed'] = {**observed, 'head': OLD}
                self.assertFalse(handler()._owned_checkpoint_remote(
                    context, {'pr_url': None, 'publication_remote_oid': OLD},
                    OLD, current,
                ))
                saved['result']['observed'] = observed
                (metadata / 'onto').write_text(OLD)
                self.assertFalse(handler()._owned_checkpoint_remote(
                    context, {'pr_url': None, 'publication_remote_oid': OLD},
                    OLD, current,
                ))
                (metadata / 'onto').write_text(NEW)
                self.assertFalse(handler()._owned_checkpoint_remote(
                    context, {'pr_url': None, 'publication_remote_oid': OLD},
                    NEW, current,
                ))

    def test_completed_published_rebase_requires_exact_saved_result(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            context = SimpleNamespace(
                plan=SimpleNamespace(plan_id='G-091'), branch=BRANCH,
                worktree=worktree,
            )
            observed = {'head': NEW, 'unmerged': False, 'index': {},
                        'registration': [f'HEAD={NEW}']}
            checkpoint = {'phase': 'published', 'accepted_remote_oid': OLD}
            connection = mock.Mock()
            connection.execute.return_value.fetchone.return_value = checkpoint

            def git(_path, *args, **_kwargs):
                if args[0] == 'symbolic-ref':
                    return SimpleNamespace(returncode=0, stdout=BRANCH + '\n')
                return SimpleNamespace(returncode=0, stdout=NEW + '\n')

            workspace = SimpleNamespace(
                git=git, validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
            )
            handler = object.__new__(ExecutionHandlers)
            handler.effects = SimpleNamespace(
                state=SimpleNamespace(connection=connection), workspace=workspace,
            )
            handler.state_dir = worktree
            handler.repo = 'repo'
            handler.repo_path = Path('/repo')
            handler._finish_rebase_journal = mock.Mock(return_value=False)
            handler._rebase_journal_state = mock.Mock(return_value=observed)
            current = SimpleNamespace(
                operation='rebase', expected_head=OLD, target_base_oid=NEW,
                observed_remote_oid=OLD, snapshot={},
            )
            saved = {'intent': {
                'kind': 'managed_rebase', 'target': str(worktree.resolve()),
                'expected': {'before_head': OLD, 'target_base_oid': NEW,
                             'remote_oid': OLD},
            }, 'result': {'observed': observed}}
            with (mock.patch('execution_handlers.checkpoint_ownership.owned_remote_oid',
                             return_value=OLD),
                  mock.patch('execution_handlers.manager_git_journal.verified_managed_transitions',
                             return_value=[saved])):
                self.assertTrue(handler._owned_checkpoint_remote(
                    context, {'pr_url': None, 'publication_remote_oid': OLD},
                    OLD, current,
                ))
                saved['result']['observed'] = {**observed, 'head': 'external'}
                self.assertFalse(handler._owned_checkpoint_remote(
                    context, {'pr_url': None, 'publication_remote_oid': OLD},
                    OLD, current,
                ))


if __name__ == '__main__':
    unittest.main()
