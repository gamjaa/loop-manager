"""Narrow ownership checks for managed workspace and rebase journal entries."""
from tests.support.execution import complete_execution_effects, execution_handler_stub

from pathlib import Path
from contextlib import ExitStack
from dataclasses import replace
import copy
from loop_manager_internal.git.managed_rebase_validation import FileChange, IndexEntry
from tests.support.current_rebase import current_rebase_case
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.execution.execution_checkpoint import owned_detached_rebase, rebase_preserves_worktree_files
from loop_manager_internal.execution.execution_handlers import ExecutionHandlers
from loop_manager_internal.git.rebase_correction import CorrectionAction
from loop_manager_internal.git import cleanup_service, manager_git_journal as journal
import loop_manager
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason


REPO = str(Path('/repo').resolve())
OLD = 'a' * 40
NEW = 'b' * 40
TREE = 'c' * 40
BRANCH = 'loop-manager/G-091'


class ManagedTransitionTests(unittest.TestCase):
    def test_unlocked_cleanup_recovery_accepts_changed_head_without_git_write(self):
        before = {'exists': True, 'registration': {
            'head': OLD, 'branch': f'refs/heads/{BRANCH}', 'locked': None,
        }, 'index': TREE}
        current = {'exists': True, 'registration': {
            'head': NEW, 'branch': f'refs/heads/{BRANCH}', 'locked': None,
        }, 'index': TREE}
        intent = {'kind': 'managed_worktree_remove', 'owner': 'plan:G-091',
                  'target': '/state/G-091', 'before': before,
                  'expected': {'exists': False, 'registration': None, 'index': None}}
        git = mock.Mock()
        self.assertEqual(cleanup_service.restore_cleanup_unlock(
            intent, current, Path(REPO), Path('/state/G-091'),
            git=git, observe=lambda: current), current)
        git.assert_not_called()

    def test_cleanup_unlock_recovery_rejects_changed_ownership(self):
        before = {'exists': True, 'registration': {
            'head': OLD, 'branch': f'refs/heads/{BRANCH}',
            'locked': 'loop-manager-worktree:owned',
        }, 'index': TREE}
        intent = {'kind': 'managed_worktree_remove', 'owner': 'plan:G-091',
                  'target': '/state/G-091', 'before': before,
                  'expected': {'exists': False, 'registration': None, 'index': None}}
        for registration in ({'head': NEW, 'branch': 'refs/heads/other', 'locked': None},
                             {'head': NEW, 'branch': f'refs/heads/{BRANCH}',
                              'locked': 'foreign-owner'}):
            with self.subTest(registration=registration):
                current = {**before, 'registration': registration}
                git = mock.Mock()
                with self.assertRaises(journal.MutationEvidenceError):
                    cleanup_service.restore_cleanup_unlock(
                        intent, current, Path(REPO), Path('/state/G-091'),
                        git=git, observe=lambda: current)
                git.assert_not_called()

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
                             repo=REPO, target=str(Path('/worktree').resolve()), before={'head': OLD})
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
            'target': str(Path('/worktree').resolve()), 'before': before,
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
            worktrees={str(Path('/worktree').resolve()): old_registration}, config={}, operations={},
        )
        current = loop_manager.replace(
            before, worktrees={str(Path('/worktree').resolve()): (*old_registration, f'locked={lock}')},
        )
        transition = {'intent': {
            'kind': 'managed_worktree_create', 'target': str(Path('/worktree').resolve()),
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
                before, current, Path('/state'), Path(REPO),
                'G-091', Path('/worktree'),
            )
            self.assertEqual(corrected.worktrees, current.worktrees)
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager._proven_recovered_worktree_baseline(
                    before, loop_manager.replace(current, dirty={'app.py':
                        (('unstaged',), 'external')}), Path('/state'),
                    Path(REPO), 'G-091', Path('/worktree'),
                )
            transition['intent']['expected']['registration']['locked'] = 'other'
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager._proven_recovered_worktree_baseline(
                    before, current, Path('/state'), Path(REPO),
                    'G-091', Path('/worktree'),
                )
            transitions.assert_called_with(
                Path('/state'), str(Path(REPO).resolve()), 'plan:G-091',
                git=mock.ANY, kind='managed_worktree_create', target=str(Path('/worktree').resolve()),
            )
            observed_git = transitions.call_args.kwargs['git']
            with mock.patch.object(loop_manager, 'git', return_value='observed') as git:
                self.assertEqual(observed_git(Path(REPO), 'rev-parse', 'HEAD', check=False), 'observed')
                git.assert_called_once_with(Path(REPO), 'rev-parse', 'HEAD', check=False)

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
            'target': str(Path('/worktree').resolve()), 'before': before,
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
            'id': 'owned', 'target': str(Path('/worktree').resolve()), 'before': before,
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
                state, owner='plan:G-091', repo=REPO, target=target,
                before=before, expected=expected,
            )
            # Intent exists, but an external rebase can produce the same
            # metadata without executing this Manager operation.
            with self.assertRaises(journal.MutationEvidenceError):
                journal.complete_rebase(
                    state, repo=REPO, target=target, observe=lambda: conflict,
                    validate=lambda intent, observed: journal.verified_rebase_result(
                        intent, observed, current_remote_oid=OLD,
                        target_ancestor=True, patches_preserved=False, reflog_marker=False,
                    ),
                )
            slot = journal._slot(state, REPO, 'managed_rebase', target)
            self.assertTrue((slot / 'active.json').exists())
            self.assertFalse((slot / f'{sealed_id}.result.json').exists())
            journal.complete_rebase(
                state, repo=REPO, target=target, observe=lambda: conflict,
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
                state, owner='plan:G-091', repo=REPO, target=target,
                before=corrected, expected={**expected, 'action': 'continue',
                                           'accepted_files': {'app.py': 'resolved'},
                                           'staged_tree_oid': TREE},
            )
            # Restart after saving intent but before invoking Git: no outcome
            # can be sealed, and the same intent must remain available to retry.
            validate = mock.Mock()
            self.assertFalse(journal.complete_rebase(
                state, repo=REPO, target=target,
                observe=lambda: corrected, validate=validate,
            ))
            validate.assert_not_called()
            slot = journal._slot(state, REPO, 'managed_rebase', target)
            self.assertEqual(journal._read(slot / 'active.json')['id'], pending_id)
            self.assertFalse((slot / f'{pending_id}.result.json').exists())
            transitions = journal.verified_managed_transitions(
                state, REPO, 'plan:G-091', agent_session_id='worker-original',
            )
            self.assertEqual([item['intent']['id'] for item in transitions], [sealed_id])
            self.assertEqual(journal.verified_managed_transitions(
                state, REPO, 'plan:G-091', agent_session_id='other-worker',
            ), [])
            handler = execution_handler_stub()
            handler.state_dir, handler.repo_path = state, Path(REPO)
            handler.effects = complete_execution_effects(SimpleNamespace(workspace=SimpleNamespace(git=mock.Mock())))
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
                state, REPO, 'plan:G-091', agent_session_id='worker-original',
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
            'target': str(Path('/worktree').resolve()), 'before': before,
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
                state, owner='plan:G-091', repo=REPO, target=str(Path('/worktree').resolve()),
                before=before, expected=intent['expected'],
            )
            self.assertTrue(journal.complete_rebase(
                state, repo=REPO, target=str(Path('/worktree').resolve()),
                observe=lambda: observed,
                validate=lambda saved, actual: journal.verified_rebase_result(
                    saved, actual, **checks,
                ),
            ))
            selected = journal.verified_managed_transitions(
                state, REPO, 'plan:G-091',
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
            git, Path(REPO), OLD, NEW,
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
            'id': 'owned', 'target': str(Path('/worktree').resolve()), 'before': before,
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
            'id': 'owned', 'target': str(Path('/worktree').resolve()), 'before': before,
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
                state, owner='plan:G-091', repo=REPO, target=str(Path('/worktree').resolve()),
                before=before, expected=expected,
            )
            self.assertEqual(identity, journal.begin_rebase(
                state, owner='plan:G-091', repo=REPO, target=str(Path('/worktree').resolve()),
                before=before, expected=expected,
            ))
            with self.assertRaises(journal.MutationEvidenceError):
                journal.begin_rebase(
                    state, owner='plan:G-092', repo=REPO, target=str(Path('/worktree').resolve()),
                    before=before, expected=expected,
                )
            with self.assertRaises(journal.MutationEvidenceError):
                journal.complete_rebase(
                    state, repo=REPO, target=str(Path('/worktree').resolve()),
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
                state, repo=REPO, target=str(Path('/worktree').resolve()),
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
                    state, REPO, 'plan:G-091',
                    agent_session_id='worker-original',
                )
            self.assertEqual(journal.verified_managed_transitions(
                state, REPO, 'plan:G-091',
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
                if args[:2] == ('worktree', 'unlock'):
                    observed['registration']['locked'] = None
                if args[:2] == ('worktree', 'lock'):
                    observed['registration']['locked'] = args[2].removeprefix('--reason=')
                if args[:2] == ('worktree', 'remove'):
                    observed.update(exists=False, registration=None, index=None)
                    worktree.rmdir()
                return SimpleNamespace(returncode=0, stdout='')

            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, '_managed_worktree_transition_state',
                                    side_effect=lambda *_: copy.deepcopy(observed)),
                  mock.patch.object(loop_manager, '_capture_local_worktree_state',
                                    return_value=(OLD, BRANCH, {}, {})),
                  mock.patch.object(loop_manager, '_git_operation_entries',
                                    return_value={}),
                  mock.patch.object(loop_manager, '_ignored_worktree_paths',
                                    return_value=[]),
                  mock.patch.object(loop_manager, 'assess_cleanup',
                                    return_value=loop_manager.CleanupAssessment(
                                        'safe', pr_head_oid=OLD,
                                        observation=(OLD, BRANCH, ()))),
                  mock.patch.object(loop_manager, '_write_managed_worktree_receipt',
                                    side_effect=RuntimeError('crash after Git'))):
                result = loop_manager.remove_cleanup_worktree(
                    repo, worktree, loop_manager.CleanupAssessment(
                        'safe', pr_head_oid=OLD, observation=(OLD, BRANCH, ())),
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

    def test_locked_cleanup_restores_journal_after_stale_proof_and_interrupted_unlock(self):
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
            safe = loop_manager.CleanupAssessment(
                'safe', pr_head_oid=OLD, observation=(OLD, BRANCH, ()))
            removed = []

            def git(_repo, *args, **_kwargs):
                if args[0] == 'rev-parse':
                    return SimpleNamespace(returncode=0, stdout=TREE + '\n')
                if args[:2] == ('worktree', 'unlock'):
                    observed['registration']['locked'] = None
                if args[:2] == ('worktree', 'lock'):
                    observed['registration']['locked'] = args[2].removeprefix('--reason=')
                if args[:2] == ('worktree', 'remove'):
                    removed.append(args)
                    observed.update(exists=False, registration=None, index=None)
                    worktree.rmdir()
                return SimpleNamespace(returncode=0, stdout='')

            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, '_managed_worktree_transition_state',
                                    side_effect=lambda *_: copy.deepcopy(observed)),
                  mock.patch.object(loop_manager, '_capture_local_worktree_state',
                                    return_value=(OLD, BRANCH, {}, {})),
                  mock.patch.object(loop_manager, '_git_operation_entries', return_value={}),
                  mock.patch.object(loop_manager, '_ignored_worktree_paths', return_value=[]),
                  mock.patch.object(loop_manager, 'assess_cleanup', side_effect=[
                      safe, loop_manager.CleanupAssessment('unverifiable', 'stale'),
                      safe, safe, KeyboardInterrupt(), safe, safe, safe,
                  ])):
                first = loop_manager.remove_cleanup_worktree(
                    repo, worktree, safe, state_dir=state, owner='plan:G-091')
                self.assertEqual(first.status, 'unverifiable')
                self.assertTrue(worktree.exists())
                self.assertEqual(observed['registration']['locked'], 'loop-manager-worktree:owned')
                self.assertEqual(removed, [])
                self.assertEqual(len(list((state / 'manager-git-journal').glob('*/active.json'))), 1)
                self.assertFalse(loop_manager._recover_managed_worktree_removal(
                    state, repo, worktree))

                with self.assertRaises(KeyboardInterrupt):
                    loop_manager.remove_cleanup_worktree(
                        repo, worktree, safe, state_dir=state, owner='plan:G-091')
                self.assertIsNone(observed['registration']['locked'])
                self.assertFalse(loop_manager._recover_managed_worktree_removal(
                    state, repo, worktree))
                self.assertEqual(observed['registration']['locked'], 'loop-manager-worktree:owned')
                second = loop_manager.remove_cleanup_worktree(
                    repo, worktree, safe, state_dir=state, owner='plan:G-091')
                self.assertEqual(second.status, 'safe', second.reason)
                self.assertFalse(worktree.exists())
                self.assertEqual(removed[0], ('worktree', 'remove', str(worktree)))
                self.assertEqual(list((state / 'manager-git-journal').glob('*/active.json')), [])

    def test_direct_rebase_result_recovers_after_git_before_journal_result(self):
        case = current_rebase_case(self)
        case.fixture.journal_start.stop()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            journal.begin_current_rebase(state_dir, owner='plan:G-047', repo=REPO, target=str(Path('/state/G-047').resolve()),
                before={'head': 'before'}, expected={'branch': 'loop-manager/G-047', 'before_head': 'before',
                'target_base_oid': 'base', 'remote_oid': 'remote'})
            with (
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=state_dir),
                mock.patch.object(loop_manager, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)),
                mock.patch.object(loop_manager, '_owned_rebase_journal_state', return_value={'head': 'owned-head'}),
                # This case owns rebase acceptance; publication has its own
                # lease, interruption and staging tests with durable owners.
                mock.patch.object(loop_manager, '_publish_direct_rebase_checkpoint'),
            ):
                with mock.patch.object(journal, '_finish', side_effect=RuntimeError('crash')):
                    with self.assertRaisesRegex(RuntimeError, 'crash'):
                        loop_manager._finish_owned_direct_rebase(state_dir, case.fixture.connection,
                            Path(REPO), 'repo', 'G-047', Path('/state/G-047').resolve(), 'loop-manager/G-047')
                loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                    Path('/state/G-047').resolve(), 'base', repo_path=Path(REPO), branch='loop-manager/G-047',
                    agent_session_id='replacement')
                saved = case.checkpoint.get('repo', 'G-047')
                loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                    Path('/state/G-047').resolve(), 'base', repo_path=Path(REPO), branch='loop-manager/G-047',
                    agent_session_id=None)
            self.assertIsNone(saved.operation)
            self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)
            self.assertEqual(saved.snapshot['current_rebase_acceptance']['head'], 'owned-head')
            refreshed = case.fixture.repository.projected('repo')
            self.assertIsNone(refreshed.worker_result)
            self.assertEqual(refreshed.status, 'IMPLEMENTING')
            self.assertTrue((journal._slot(state_dir, REPO, 'managed_rebase', str(Path('/state/G-047').resolve())) / 'active.json').exists())

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
                repo_path=Path(REPO), branch=BRANCH,
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
            mock.patch.object(loop_manager, '_publish_direct_rebase_checkpoint'),
        ):
            for field, value in (('head', 'before'), ('dirty', {'app.py': 'older'}), ('index', {'app.py': 'older'})):
                with self.subTest(field=field), self.assertRaisesRegex(journal.MutationEvidenceError, 'observation drift'):
                    loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                        Path('/state/G-047').resolve(), 'base', repo_path=Path(REPO), branch='loop-manager/G-047',
                        agent_session_id=None, preserved_observation={**observed, field: value})
                self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
            loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                Path('/state/G-047').resolve(), 'base', repo_path=Path(REPO), branch='loop-manager/G-047',
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
            mock.patch.object(loop_manager, '_publish_direct_rebase_checkpoint'),
        ):
            for pending, error in (
                (replace(clean, index_clean=False, staged=(FileChange('app.py', '100644', '100644', 'old', 'manual', 'M'),)), journal.MutationEvidenceError),
                (replace(clean, index_clean=False, index=(IndexEntry('app.py', '100644', 'unmerged', 2, 'M', 0x2000),)), journal.MutationEvidenceError),
                (replace(clean, committed=(FileChange('docs/requirements/archive/UserGuidance.md', '100644', '100644', 'old', 'foreign', 'M'),)), RecoveryFailure),
            ):
                with self.subTest(pending=pending), self.assertRaises(error) as caught:
                    case.observed = pending
                    loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                        Path('/state/G-047').resolve(), 'base', repo_path=Path(REPO), branch='loop-manager/G-047', agent_session_id=None)
                if error is RecoveryFailure:
                    self.assertEqual(caught.exception.reason, RecoveryReason.AGENT_SCOPE_VIOLATION)
                self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
            case.observed = replace(clean, committed=(FileChange('app.py', '100644', '100755', 'old', 'manual-new-content', 'M'),))
            loop_manager._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                Path('/state/G-047').resolve(), 'base', repo_path=Path(REPO), branch='loop-manager/G-047', agent_session_id=None)
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
            mock.patch('loop_manager_internal.execution.execution_handlers.managed_rebase_evidence.source_commits', side_effect=AssertionError('no replay')),
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
            handler = execution_handler_stub()
            handler.repo = 'repo'
            handler.effects = complete_execution_effects(SimpleNamespace(
                agent=SimpleNamespace(capture_git_snapshot=lambda *_: observed,
                                      path_signature=loop_manager._path_signature),
                checkpoint=SimpleNamespace(get=lambda *_: SimpleNamespace(
                    snapshot={'rebase_evidence_version': 2}))))
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
            mock.patch('loop_manager_internal.execution.execution_handlers.managed_rebase_evidence.source_commits', side_effect=AssertionError('no historical stage')),
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
            with self.assertRaises(RecoveryFailure) as caught:
                case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.SKIP)
            self.assertEqual(caught.exception.reason, RecoveryReason.AGENT_SCOPE_VIOLATION)
            begin.assert_not_called()

    def test_continuation_intent_carries_only_matching_saved_stage_files(self):
        case = pending_current_case(self)
        current = case.checkpoint.get('repo', 'G-047')
        self.assertEqual(current.snapshot['stage_evidence'], {'old': 'audit'})
        with mock.patch.object(journal, 'begin_current_rebase', return_value='manual') as begin:
            case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE)
        self.assertNotIn('stage_evidence', begin.call_args.kwargs['expected'])
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), current)
        case.observed = replace(case.observed, staged=(FileChange('docs/requirements/archive/UserGuidance.md', '100644', '100644', 'old', 'foreign', 'M'),))
        with mock.patch.object(journal, 'begin_current_rebase') as begin:
            with self.assertRaises(RecoveryFailure) as caught:
                case.handler._begin_rebase_continuation(case.fixture.context(), CorrectionAction.CONTINUE)
            self.assertEqual(caught.exception.reason, RecoveryReason.AGENT_SCOPE_VIOLATION)
            begin.assert_not_called()

    def test_dropped_rebase_patch_leaves_journal_unverified_for_no_change_gate(self):
        handler = execution_handler_stub()
        handler.effects = complete_execution_effects(SimpleNamespace(
            checkpoint=None,
            workspace=SimpleNamespace(
                _rebase_in_progress_for_branch=lambda *_: False,
                branch_has_changes=mock.Mock(return_value=False),
            ),
        ))
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
        handler = execution_handler_stub()
        handler.effects = complete_execution_effects(SimpleNamespace(workspace=SimpleNamespace(git=git)))
        handler._begin_rebase_continuation = mock.Mock(return_value='owned-token')
        context = SimpleNamespace(worktree=Path('/owned'))
        handler._execute_rebase_step(context, CorrectionAction.CONTINUE)
        self.assertEqual(
            git.call_args.kwargs['env']['GIT_REFLOG_ACTION'],
            'loop-manager-rebase:owned-token',
        )

    def test_conflict_dispatch_waits_for_journal_result(self):
        events = []
        handler = execution_handler_stub()
        handler.effects = complete_execution_effects(SimpleNamespace(
            checkpoint=None,
            workspace=SimpleNamespace(
                _rebase_in_progress_for_branch=lambda *_: True,
            ),
            state=SimpleNamespace(update_project=mock.Mock()),
            contract=SimpleNamespace(REBASE_CONFLICT_PREFIX='conflict: '),
        ))
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
            mock.patch('loop_manager_internal.execution.execution_handlers.checkpoint_ownership.owned_remote_oid', return_value='remote'),
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
            mock.patch('loop_manager_internal.execution.execution_handlers.checkpoint_ownership.owned_remote_oid', return_value='remote'),
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
    def test_windows_file_modes_reuse_index_without_per_file_git_queries(self):
        from loop_manager_internal.git import managed_rebase_runtime
        ports, service = self._user_document_service()
        ports.git.reset_mock()
        ports._path_signature.return_value = 'current-bytes'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app.py').write_bytes(b'current bytes')
            (root / 'submodule').mkdir()
            with mock.patch.object(managed_rebase_runtime, 'os', SimpleNamespace(name='nt')):
                for mode in ('100644', '100755', None):
                    self.assertEqual(service.adapters.read_file(root, 'app.py', mode),
                                     (mode or '100644', 'current-bytes'))
                self.assertEqual(service.adapters.read_file(root, 'missing.py', '100755'),
                                 ('000000', 'current-bytes'))
                self.assertEqual(service.adapters.read_file(root, 'submodule', '160000'),
                                 ('160000', 'current-bytes'))
        ports.git.assert_not_called()

    def _user_document_service(self, document_intent=None, *, completed=False):
        from tests.support.runtime import runtime_rebase_fixture
        from loop_manager_internal.git.managed_rebase_runtime import build_service
        connection, _, checkpoints, *_ = runtime_rebase_fixture(self)
        current = checkpoints.get('repo', 'G-047')
        checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid='remote', snapshot={**current.snapshot, 'before_head': 'before',
                **({'rebase_documents': document_intent} if document_intent else {}),
                'work_branch_integration': {'remote': 'remote', 'local_commit': 'local-user'}})
        ports = mock.Mock()
        def git(_root, *args, **kwargs):
            output = ''
            if args == ('rev-parse', '--git-common-dir'):
                output = '/repo/.git'
            elif args[0] == 'diff':
                output = ''.join(':100644 100644 old user M\0' + path + '\0'
                    for path in ('docs/requirements/archive/UserGuidance.md', 'docs/requirements/archive/UserDesign.md'))
            return SimpleNamespace(stdout=output, returncode=0)
        ports.git.side_effect = git
        ports.requirement_archive_candidates.return_value = ()
        ports._published_plan_allows_common_plans.return_value = False
        ports._revision_execplan_input_fingerprint.return_value = 'published-input'
        ports._worktree_execplan_input_fingerprint.return_value = 'current-input'
        ports._owned_rebase_metadata.return_value = {'head-name': 'refs/heads/loop-manager/G-047',
                                                    'orig-head': 'before', 'onto': 'base'}
        ports._worker_path_allowed.side_effect = loop_manager._worker_path_allowed
        service = build_service(ports, connection, Path(REPO), Path('/state'),
                                'repo', 'G-047', Path('/owned'), 'loop-manager/G-047')
        if completed:
            from tests.support.rebase_observations import decision_for, observation
            current = checkpoints.get('repo', 'G-047')
            acceptance = decision_for(service.expected, observation(service.expected,
                input_fingerprint=service.expected.input_fingerprint,
                scope_fingerprint=service.expected.scope_fingerprint)).acceptance
            checkpoints.record('repo', 'G-047', expected_generation=current.generation,
                operation=None, expected_head=acceptance['head'], target_base_oid='base',
                observed_remote_oid='remote', snapshot={**current.snapshot,
                    'current_rebase_acceptance': acceptance})
            ports._owned_rebase_metadata.return_value = None
            service = build_service(ports, connection, Path(REPO), Path('/state'),
                                    'repo', 'G-047', Path('/owned'), 'loop-manager/G-047')
        return ports, service

    def test_adopted_document_pin_does_not_reject_later_worker_notes(self):
        from tests.support.rebase_observations import observation
        from loop_manager_internal.git.managed_rebase_validation import WorktreeFile, assess_scope
        path = 'docs/plans/[G-047] Runtime.md'
        for state in ('prepared', 'staged'):
            record = {'state': state, 'target': 'base', 'before_head': 'before',
                'paths': [path], 'originals': {path: {'base': {'mode': '100644', 'oid': 'pinned'}}}}
            for completed in (False, True):
                with self.subTest(state=state, completed=completed):
                    _, service = self._user_document_service(record, completed=completed)
                    current = observation(service.expected,
                        index=(IndexEntry(path, '100644', 'pinned', 0, 'H', 0),),
                        files=(WorktreeFile(path, ('unstaged',), '100644', 'new-worker-notes'),))
                    def scope(value):
                        return assess_scope(service.expected, value,
                            path_allowed=service.adapters.path_allowed,
                            contracts_allowed=service.adapters.contracts_allowed)
                    self.assertEqual(scope(current).allowed, completed)
                    self.assertFalse(scope(replace(current, files=(replace(
                        current.files[0], path='docs/requirements/Protected.md'),))).allowed)
                    self.assertFalse(scope(replace(current, files=(replace(
                        current.files[0], path='docs/plans/[G-048] Other.md'),))).allowed)

    def test_clean_replay_head_and_staged_document_are_checked_separately(self):
        from tests.support.rebase_observations import observation
        path = 'docs/requirements/[R-001] Condition.md'
        record = {'state': 'staged', 'target': 'base', 'before_head': 'before',
            'paths': [path], 'workspace': {'head': 'clean-replay'},
            'originals': {path: {'base': {'absent': True},
                               'head': {'mode': '100644', 'oid': 'clean-blob'}}}}
        _, service = self._user_document_service(record)
        expected = service.expected
        staged = observation(expected, head='clean-replay',
            metadata=(('head-name', f'refs/heads/{expected.branch}'),
                      ('orig-head', 'before'), ('onto', 'base')),
            committed=(FileChange(path, '100644', '100644', 'old', 'clean-blob', 'M'),),
            staged=(FileChange(path, '100644', '000000', 'clean-blob', '0' * 40, 'D'),),
            index=())
        self.assertTrue(service.adapters.contracts_allowed(staged))
        # A restarted service reads the same durable intermediate HEAD proof.
        _, restarted = self._user_document_service(record)
        self.assertTrue(restarted.adapters.contracts_allowed(staged))
        self.assertFalse(service.adapters.contracts_allowed(replace(staged, head='foreign-head')))
        self.assertFalse(service.adapters.contracts_allowed(replace(staged,
            committed=(replace(staged.committed[0], new_oid='foreign-blob'),))))
        self.assertFalse(service.adapters.contracts_allowed(replace(staged,
            staged=(replace(staged.staged[0], new_mode='100644', new_oid='stale'),))))
        final = replace(staged, metadata=None, staged=(),
                        committed=(replace(staged.staged[0], old_oid='old'),))
        self.assertTrue(service.adapters.contracts_allowed(final))
        self.assertFalse(service.adapters.contracts_allowed(replace(final,
            committed=(), index=(IndexEntry(path, '100644', 'stale', 0, 'H', 0),))))

    def test_owned_user_document_conflicts_allow_preservation_without_final_acceptance(self):
        from tests.support.rebase_observations import observation
        from loop_manager_internal.git.managed_rebase_validation import (
            WorktreeFile, RebaseStatus, assess_scope, assess_current_rebase)
        ports, service = self._user_document_service()
        expected = service.expected
        for path in ('docs/requirements/archive/UserGuidance.md', 'docs/requirements/archive/UserDesign.md'):
            with self.subTest(path=path):
                pending = observation(expected, head='partial', branch=None,
                    branch_ref=('before', ''), registration=('HEAD=partial', 'detached=True'),
                    branch_worktrees=(), operations=(('rebase-merge', 'owned'),),
                    metadata=(('head-name', f'refs/heads/{expected.branch}'),
                              ('orig-head', 'before'), ('onto', 'base')),
                    index_clean=False,
                    index=tuple(IndexEntry(path, '100644', f'blob-{stage}', stage, 'M', stage << 12)
                                for stage in (1, 2, 3)),
                    committed=(FileChange(path, '100644', '100644', 'old', 'partial-user', 'M'),),
                    staged=(FileChange(path, '100644', '000000', 'old', '0', 'U'),),
                    files=(WorktreeFile(path, ('staged', 'unstaged'), '100644', 'conflict bytes'),),
                    input_fingerprint=expected.input_fingerprint, scope_fingerprint=expected.scope_fingerprint)
                scope = assess_scope(expected, pending, path_allowed=service.adapters.path_allowed,
                                     contracts_allowed=service.adapters.contracts_allowed)
                with mock.patch('loop_manager_internal.git.managed_rebase_adoption.observe_current_rebase',
                                return_value=(pending, scope)):
                    _, decision = service.require_owned()
                self.assertEqual(decision.status, RebaseStatus.WAITING)
                self.assertIsNone(decision.acceptance)
                self.assertEqual(len(pending.index), 3)  # The observation and stages remain intact.
                final = observation(expected,
                    committed=(FileChange(path, '100644', '100644', 'old', 'user', 'M'),),
                    index=(IndexEntry(path, '100644', 'user', 0, 'H', 0),),
                    input_fingerprint=expected.input_fingerprint, scope_fingerprint=expected.scope_fingerprint)
                def decision_for(actual):
                    scope = assess_scope(expected, actual, path_allowed=service.adapters.path_allowed,
                                         contracts_allowed=service.adapters.contracts_allowed)
                    return assess_current_rebase(expected, actual, scope)
                self.assertEqual(decision_for(final).status, RebaseStatus.COMPLETE)
                unapproved = replace(final, committed=(replace(final.committed[0], new_oid='agent'),))
                self.assertEqual(decision_for(unapproved).status, RebaseStatus.REJECTED)
                invalid_stage = replace(pending, index=(replace(pending.index[0], flags=0x8000),))
                self.assertEqual(decision_for(invalid_stage).status, RebaseStatus.REJECTED)
                foreign_operation = replace(pending, operations=(*pending.operations, ('MERGE_HEAD', 'foreign')))
                self.assertEqual(decision_for(foreign_operation).status, RebaseStatus.REJECTED)
                ports.reset_mock()
                self.assertTrue(service.adapters.contracts_allowed(pending))
                self.assertEqual(ports.mock_calls, [])  # Preservation policy stays pure.

    def test_user_document_conflict_permission_requires_recorded_input_and_exact_operation(self):
        from tests.support.rebase_observations import observation
        from loop_manager_internal.git.managed_rebase_validation import WorktreeFile
        _, service = self._user_document_service()
        expected = service.expected
        pending = observation(expected,
            metadata=(('head-name', f'refs/heads/{expected.branch}'),
                      ('orig-head', 'before'), ('onto', 'base')),
            committed=(), staged=(FileChange('docs/requirements/archive/UserGuidance.md', '100644', '000000', 'old', '0', 'U'),),
            files=(WorktreeFile('docs/requirements/archive/UserGuidance.md', ('unstaged',), '100644', 'manual bytes'),),
            index=(IndexEntry('docs/requirements/archive/UserGuidance.md', '100644', 'ours', 2, 'M', 0x2000),))
        for metadata in (None, (('head-name', 'refs/heads/other'), ('orig-head', 'before'), ('onto', 'base')),
                         (('head-name', f'refs/heads/{expected.branch}'), ('orig-head', 'other'), ('onto', 'base')),
                         (('head-name', f'refs/heads/{expected.branch}'), ('orig-head', 'before'), ('onto', 'other'))):
            with self.subTest(metadata=metadata):
                self.assertFalse(service.adapters.contracts_allowed(replace(pending, metadata=metadata)))
        unknown = 'docs/requirements/archive/unconfirmed.md'
        self.assertFalse(service.adapters.contracts_allowed(replace(pending,
            staged=(replace(pending.staged[0], path=unknown),),
            files=(replace(pending.files[0], path=unknown),),
            index=(replace(pending.index[0], path=unknown),))))
        # Once stages disappear, remaining manual bytes are subject to the final contract.
        self.assertFalse(service.adapters.contracts_allowed(replace(pending, index=())))

    def test_archive_contract_uses_original_base_only_before_rebase(self):
        from loop_manager_internal.catalog import requirement_archive
        from loop_manager_internal.git.managed_rebase_runtime import build_service
        from tests.support.runtime import runtime_rebase_fixture
        source = 'docs/requirements/old.md'
        archived = 'docs/requirements/archive/old.md'
        original_tree = {source.encode(): (b'blob', b'original')}
        target_tree = {**original_tree,
                       b'docs/requirements/new.md': (b'blob', b'new')}
        cases = (
            ('before', None, '', 1, True),
            ('before', None, '', 0, False),
            ('after', None, '', 1, False),
            ('before', 'rebase-metadata', '', 1, False),
            ('before', None, 'unmerged-index', 1, False),
        )
        for head, metadata, index, ancestry, allowed in cases:
            with self.subTest(head=head, metadata=metadata, index=index, ancestry=ancestry):
                connection, *_ = runtime_rebase_fixture(self)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / archived).parent.mkdir(parents=True)
                    (root / archived).write_bytes(b'body')
                    ports = mock.Mock()
                    ports.WORKSPACE_OWNED = 1
                    ports.requirement_archive_candidates.return_value = (source,)
                    ports.requirement_archive_path.side_effect = loop_manager.requirement_archive_path
                    ports._owned_rebase_metadata.return_value = metadata
                    ports._published_plan_allows_common_plans.return_value = False
                    ports._revision_execplan_input_fingerprint.return_value = 'published-input'
                    ports._worktree_execplan_input_fingerprint.return_value = 'current-input'
                    ports._path_signature.return_value = 'signature'
                    def git(_root, *args, **kwargs):
                        output = {
                            ('rev-parse', '--git-common-dir'): '/repo/.git',
                            ('rev-parse', 'HEAD'): head,
                            ('ls-files', '-u', '-z'): index,
                            ('merge-base', 'base', 'before'): 'common',
                            ('ls-tree', '-z', 'base', '--', source):
                                '100644 blob ' + 'a' * 40 + '\t' + source + '\0',
                        }.get(args, '')
                        code = ancestry if args[:2] == ('merge-base', '--is-ancestor') else 0
                        return SimpleNamespace(stdout=output, returncode=code)
                    ports.git.side_effect = git
                    def moves(_repo, revision, _worktree):
                        return requirement_archive.validated_moves(
                            original_tree if revision == 'common' else target_tree,
                            root, blob_contents=lambda _oid: b'body',
                            ignore_status=lambda _path: (1, ''))
                    ports.requirement_archive_move_set.side_effect = moves
                    def build():
                        return build_service(ports, connection, Path(REPO), Path('/state'),
                                             'repo', 'G-047', Path('/owned'), 'loop-manager/G-047')
                    if allowed:
                        service = build()
                        ports.requirement_archive_move_set.assert_called_once_with(
                            Path(REPO), 'common', Path('/owned'))
                        from tests.support.rebase_observations import observation
                        from loop_manager_internal.git.managed_rebase_validation import FileChange
                        def archived_change(oid):
                            return observation(service.expected, committed=(FileChange(
                                archived, '000000', '100644', '0', oid, 'A'),))
                        # 유효한 archive 근거도 Worker의 Requirement 쓰기 권한은 만들지 않는다.
                        self.assertFalse(service.adapters.contracts_allowed(archived_change('a' * 40)))
                        self.assertFalse(service.adapters.contracts_allowed(archived_change('b' * 40)))
                        # Existing requirements still cannot disappear from both paths.
                        (root / archived).unlink()
                        with self.assertRaises(requirement_archive.ArchiveMoveError):
                            build()
                    else:
                        with self.assertRaises(requirement_archive.ArchiveMoveError):
                            build()
                        ports.requirement_archive_move_set.assert_called_once_with(
                            Path(REPO), 'base', Path('/owned'))

    def test_factory_uses_current_inputs_exact_lease_and_final_scope(self):
        from tests.support.runtime import runtime_rebase_fixture
        from loop_manager_internal.git.managed_rebase_runtime import build_service
        from tests.support.rebase_observations import observation
        connection, runtime, checkpoints, original, expected, decision = runtime_rebase_fixture(self)
        ports = mock.Mock()
        ports.WORKSPACE_OWNED = 1
        ports.git.side_effect = lambda _root, *args, **kwargs: SimpleNamespace(
            stdout='/repo/.git' if args == ('rev-parse', '--git-common-dir') else '', returncode=0)
        ports.requirement_archive_candidates.return_value = ()
        ports._published_plan_allows_common_plans.return_value = False
        ports._revision_execplan_input_fingerprint.return_value = 'published-input'
        ports._worktree_execplan_input_fingerprint.return_value = 'current-input'
        ports._owned_rebase_metadata.return_value = None
        ports.live_remote_branch_oid.return_value = 'remote'
        ports.pr_for_branch.return_value = None
        ports._worker_path_allowed.side_effect = loop_manager._worker_path_allowed
        service = build_service(ports, connection, Path(REPO), Path('/state'),
                                'repo', 'G-047', Path('/owned'), 'loop-manager/G-047')
        self.assertEqual(service.expected.remote.oid, 'remote')
        self.assertEqual(service.expected.before_head, 'before')
        self.assertIsNone(service.adapters.read_remote(service.expected).pull_request)
        self.assertTrue(service.adapters.path_allowed('app.py'))
        self.assertFalse(service.adapters.path_allowed('docs/requirements/Active.md'))
        self.assertFalse(service.adapters.path_allowed('docs/requirements/archive/UserDesign.md'))
        self.assertFalse(service.adapters.path_allowed('docs/plans/[G-048] Other.md'))
        self.assertFalse(service.adapters.path_allowed('docs/requirements/archive/Other.md'))
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

    def test_lifecycle_factory_binds_selected_repository_identity_and_options(self):
        from tests.support.runtime import runtime_rebase_fixture
        connection, runtime, *_ = runtime_rebase_fixture(self)
        handler = loop_manager._ExecutionLifecyclePorts(
            connection, runtime, repo='repo', repo_path=Path(REPO),
            state_dir=Path('/state'), base='main', base_sha='base',
            provider='codex', config_path=Path('/config'), newly_activated=False,
        )
        context = SimpleNamespace(plan=SimpleNamespace(plan_id='G-047'),
                                  worktree=Path('/owned'), branch='loop-manager/G-047')
        service = object()
        with mock.patch.object(loop_manager, '_current_rebase_service', return_value=service) as build:
            self.assertIs(handler.effects.workspace.current_rebase_service(
                context, target='latest', remote_oid=None), service)
        build.assert_called_once_with(connection, Path(REPO), Path('/state'), 'repo',
                                      'G-047', Path('/owned'), 'loop-manager/G-047',
                                      target='latest', remote_oid=None)

    def test_direct_acceptance_preserves_projected_and_unprojected_blocks(self):
        for projected in (True, False):
            with self.subTest(projected=projected):
                case = current_rebase_case(self)
                connection, runtime = case.fixture.connection, case.fixture.repository
                selected = runtime.projected('repo')
                runtime.transition_from_snapshot(selected,
                    plan_values={'status': 'PLAN_BLOCKED', 'blocked_reason': 'agent_scope',
                                 'last_error': 'scope failure'},
                    project_values={'phase': 'PLAN_BLOCKED', 'last_error': 'scope failure'})
                if not projected:
                    runtime.update_project('repo', runtime_state.clear_project_values())
                with (
                    mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
                    mock.patch.object(loop_manager, '_current_rebase_service',
                                      side_effect=lambda *_a, **_k: case.factory(None)),
                    mock.patch.object(loop_manager, '_publish_direct_rebase_checkpoint'),
                ):
                    loop_manager._complete_direct_rebase_checkpoint(connection, 'repo', 'G-047',
                        Path('/state/G-047').resolve(), 'base', repo_path=Path(REPO),
                        branch='loop-manager/G-047', agent_session_id=None)
                result = runtime.get('repo', 'G-047')
                self.assertEqual(result.status, 'PLAN_BLOCKED')
                self.assertEqual(result.last_error, 'scope failure')
                self.assertIsNone(result.worker_result)
                self.assertIsNone(result.verify_result)
                self.assertIsNone(result.review_result)
                self.assertEqual(connection.execute('SELECT blocked_reason FROM exec_plans').fetchone()[0],
                                 'agent_scope')
                self.assertIsNone(case.checkpoint.get('repo', 'G-047').operation)
                self.assertEqual(runtime.projected('repo') is not None, projected)

    def test_blocked_release_records_head_after_adoption_and_stage_restore(self):
        from loop_manager_internal.storage import blocked_checkpoint_state
        case = current_rebase_case(self)
        connection, runtime = case.fixture.connection, case.fixture.repository
        selected = runtime.projected('repo')
        runtime.transition_from_snapshot(selected,
            plan_values={'status': 'PLAN_BLOCKED', 'blocked_reason': 'verify',
                         'blocked_fingerprint': 'old-input'}, project_values={'phase': 'PLAN_BLOCKED'})
        blocked_checkpoint_state.ensure_schema(connection)
        connection.execute("INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'verify', 'failure', "
            "'loop-manager/G-047', ?, 'remote')", (str(Path('/state/G-047').resolve()),))
        before = loop_manager.GitSnapshot('before', 'loop-manager/G-047',
            {'app.py': ('H 100644 staged-blob 0',)}, {'app.py': (('staged',), 'preserved')}, {}, {}, {}, {})
        snapshots, events = [before], []
        plan = connection.execute('SELECT * FROM exec_plans').fetchone()
        def git(_root, *args, **_kwargs):
            return SimpleNamespace(returncode=int(args[:1] == ('merge-base',) and 'refs/remotes/origin/main' in args),
                stdout='base' if 'refs/remotes/origin/main' in args else snapshots[0].head)
        def rebase(*_args, **_kwargs):
            events.append('rebase')
            snapshots[0] = replace(before, head='owned-head', index={'app.py': ('H 100644 rebased-blob 0',)},
                                   dirty={'app.py': (('unstaged',), 'preserved')})
            return SimpleNamespace(returncode=0, stdout='completed')
        actual_complete = loop_manager._complete_direct_rebase_checkpoint
        actual_record = loop_manager._record_blocked_resume_head
        def complete(*args, **kwargs):
            self.assertEqual(snapshots[0].dirty['app.py'][0], ('unstaged',))
            events.append('adopt')
            return actual_complete(*args, **kwargs)
        def restore(*_args):
            self.assertIsNone(case.checkpoint.get('repo', 'G-047').operation)
            events.append('restore')
            snapshots[0] = replace(snapshots[0], index=before.index, dirty=before.dirty)
        def record(*args):
            if args[6] is not None:
                events.append('record-head')
            return actual_record(*args)
        patches = (
            mock.patch.object(loop_manager, '_plan_block_selected_for_release', return_value=plan),
            mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint', return_value='new-input'),
            mock.patch.object(loop_manager, '_revision_review_issue_fingerprint', return_value=None),
            mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint', return_value='old-input'),
            mock.patch.object(Path, 'is_dir', return_value=True),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'make_worktree'),
            mock.patch.object(loop_manager, '_resume_interrupted_blocked_rebase', return_value=None),
            mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
            mock.patch.object(loop_manager, 'preserve_blocked_agent_changes', return_value=before),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value='remote'),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid', return_value='remote'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda _: snapshots[0]),
            mock.patch.object(loop_manager, 'git', side_effect=git),
            mock.patch.object(loop_manager, '_journaled_direct_rebase', side_effect=rebase),
            mock.patch.object(loop_manager, '_blocked_execplan_metadata_error', return_value=None),
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(loop_manager, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)),
            mock.patch.object(loop_manager, '_owned_rebase_journal_state', return_value={'head': 'owned-head'}),
            mock.patch.object(loop_manager, '_complete_direct_rebase_checkpoint', side_effect=complete),
            mock.patch.object(loop_manager, '_publish_direct_rebase_checkpoint',
                              side_effect=lambda *_a, **_k: events.append('publish')),
            mock.patch.object(loop_manager, '_restore_agent_staged_entries', side_effect=restore),
            mock.patch.object(loop_manager, '_record_blocked_resume_head', side_effect=record),
            mock.patch.object(loop_manager, 'clear_agent_scope'),
            mock.patch.object(loop_manager, '_requeue_blocked_execplan', side_effect=lambda *_a, **_k: events.append('requeue')),
        )
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                connection, Path(REPO), 'repo', 'main', Path('/state')))
        self.assertEqual(events, ['rebase', 'adopt', 'publish', 'restore', 'record-head', 'requeue'])
        self.assertEqual(snapshots[0].index, before.index)

    def test_interrupted_blocked_rebase_adopts_clean_index_before_restoring_stage(self):
        from loop_manager_internal.storage import blocked_checkpoint_state
        from loop_manager_internal.git.managed_rebase_validation import WorktreeFile
        case = current_rebase_case(self)
        connection, runtime = case.fixture.connection, case.fixture.repository
        selected = runtime.projected('repo')
        runtime.transition_from_snapshot(selected,
            plan_values={'status': 'PLAN_BLOCKED', 'blocked_reason': 'verify'},
            project_values={'phase': 'PLAN_BLOCKED'})
        blocked_checkpoint_state.ensure_schema(connection)
        connection.execute("INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'verify', 'failure', "
            "'loop-manager/G-047', ?, 'remote')", (str(Path('/state/G-047').resolve()),))
        before = loop_manager.GitSnapshot('before', 'loop-manager/G-047',
            {'app.py': ('H 100644 staged-blob 0',)}, {'app.py': (('staged',), 'preserved')},
            {}, {}, {}, {})
        current = replace(before, head='owned-head', index={'app.py': ('H 100644 rebased-blob 0',)},
                          dirty={'app.py': (('unstaged',), 'preserved')})
        snapshots = [current]
        case.observed = replace(case.observed,
            files=(WorktreeFile('app.py', ('unstaged',), '100644', 'preserved'),))
        loop_manager._record_blocked_resume_head(connection, 'repo', 'G-047', 'remote',
                                               'before', 'base', None, before.serialize())
        events = []
        actual_complete = loop_manager._complete_direct_rebase_checkpoint
        def complete(*args, **kwargs):
            self.assertEqual(snapshots[0].dirty['app.py'][0], ('unstaged',))
            events.append('adopt')
            return actual_complete(*args, **kwargs)
        def restore(_worktree, _before):
            self.assertIsNone(case.checkpoint.get('repo', 'G-047').operation)
            self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
            events.append('restore')
            snapshots[0] = replace(current, index=before.index, dirty=before.dirty)
        plan = connection.execute('SELECT * FROM exec_plans').fetchone()
        with (
            mock.patch.object(loop_manager, 'git', side_effect=lambda _root, *args, **_k: SimpleNamespace(returncode=0,
                stdout='base' if 'refs/remotes/origin/main' in args else 'owned-head')),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value='remote'),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid', return_value='remote'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda _: snapshots[0]),
            mock.patch.object(loop_manager, '_blocked_execplan_metadata_error', return_value=None),
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(loop_manager, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)),
            mock.patch.object(loop_manager, '_finish_owned_direct_rebase'),
            mock.patch.object(loop_manager, '_owned_rebase_journal_state', return_value={'head': 'owned-head'}),
            mock.patch.object(loop_manager, '_complete_direct_rebase_checkpoint', side_effect=complete),
            mock.patch.object(loop_manager, '_publish_direct_rebase_checkpoint',
                              side_effect=lambda *_a, **_k: events.append('publish')),
            mock.patch.object(loop_manager, '_restore_agent_staged_entries', side_effect=restore),
            mock.patch.object(loop_manager, 'clear_agent_scope'),
            mock.patch.object(loop_manager, '_requeue_blocked_execplan', side_effect=lambda *_a, **_k: events.append('requeue')) as requeue,
        ):
            self.assertTrue(loop_manager._resume_interrupted_blocked_rebase(
                connection, Path(REPO), 'repo', 'main', plan, Path('/state/G-047').resolve(), 'loop-manager/G-047'))
            self.assertEqual(events, ['adopt', 'publish', 'restore', 'requeue'])
            saved = case.checkpoint.get('repo', 'G-047')
            events.clear()
            case.observed = replace(case.observed, index_clean=False,
                staged=(FileChange('app.py', '100644', '100644', 'rebased-blob', 'staged-blob', 'M'),))
            with mock.patch.object(loop_manager, '_complete_direct_rebase_checkpoint') as completed:
                self.assertTrue(loop_manager._resume_interrupted_blocked_rebase(
                    connection, Path(REPO), 'repo', 'main', plan, Path('/state/G-047').resolve(), 'loop-manager/G-047'))
                completed.assert_called_once()
            self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)
            staged = case.observed
            for unsafe in (
                replace(staged, index=(IndexEntry('app.py', '100644', 'unmerged', 2, 'M', 0x2000),)),
                replace(staged, committed=(FileChange('docs/requirements/archive/UserGuidance.md', '100644', '100644', 'old', 'unsafe', 'M'),)),
                replace(staged, remote=replace(staged.remote, oid='external')),
            ):
                with self.subTest(unsafe=unsafe):
                    case.observed = unsafe
                    requeue.reset_mock()
                    self.assertFalse(loop_manager._resume_interrupted_blocked_rebase(
                        connection, Path(REPO), 'repo', 'main', plan, Path('/state/G-047').resolve(), 'loop-manager/G-047'))
                    requeue.assert_not_called()
                    self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)
        self.assertEqual(events, ['requeue'])
        self.assertEqual(snapshots[0].dirty, before.dirty)
        self.assertEqual(snapshots[0].index, before.index)
        self.assertIsNone(runtime.get('repo', 'G-047').verify_result)

    def test_observation_failure_keeps_query_detail_without_acceptance(self):
        from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
        from loop_manager_internal.git.managed_rebase_validation import ObservationFailure
        case = current_rebase_case(self)
        original = case.checkpoint.get('repo', 'G-047')
        with mock.patch('loop_manager_internal.git.managed_rebase_adoption.observe_current_rebase',
                        return_value=(ObservationFailure('query_or_scope_failed', 'live PR query failed'), None)):
            with self.assertRaisesRegex(journal.MutationEvidenceError, 'live PR query failed'):
                case.factory(None).require_owned()
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)

    def test_continuation_needs_current_owned_state_not_worker_turn(self):
        from loop_manager_internal.git.managed_rebase_validation import RebaseDecision, RebaseStatus, Remote
        handler = execution_handler_stub()
        handler.repo, handler.repo_path, handler.state_dir = 'repo', Path(REPO), Path('/state')
        checkpoint = SimpleNamespace(operation='rebase', expected_head=OLD, target_base_oid=NEW,
                                     snapshot={})
        service = mock.Mock()
        service.require_owned.return_value = (SimpleNamespace(remote=Remote('repo', BRANCH, OLD)),
                                               RebaseDecision(RebaseStatus.WAITING))
        handler.effects = complete_execution_effects(SimpleNamespace(checkpoint=SimpleNamespace(get=lambda *_: checkpoint),
                                          workspace=SimpleNamespace(current_rebase_service=lambda _: service)))
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
