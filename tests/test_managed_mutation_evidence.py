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
                         'target_base_oid': NEW, 'remote_oid': OLD},
        }
        observed = {
            **before, 'head': NEW, 'branch': None,
            'index': {'app.py': ['conflict']},
            'registration': [f'HEAD={NEW}', 'detached=True'],
            'metadata': {'orig-head': OLD,
                         'head-name': f'refs/heads/{BRANCH}', 'onto': NEW},
            'unmerged': True, 'index_clean': False,
        }
        self.assertEqual(
            journal.verified_rebase_result(
                intent, observed, current_remote_oid=OLD,
                target_ancestor=True, patches_preserved=False,
                reflog_marker=False,
            )['selected']['index'], None,
        )
        for changed, remote in (({**observed, 'metadata':
                                 {**observed['metadata'], 'onto': OLD}}, OLD),
                                (observed, NEW),
                                ({**observed, 'ref': [NEW, '']}, OLD)):
            with self.subTest(remote=remote, changed=changed):
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.verified_rebase_result(
                        intent, changed, current_remote_oid=remote,
                        target_ancestor=True, patches_preserved=False,
                        reflog_marker=False,
                    )

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
            self.assertEqual(
                journal.verified_managed_transitions(
                    state, '/repo', 'plan:G-091',
                    agent_session_id='worker-original',
                )[0]['result']['observed']['head'], NEW,
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

            def git(_path, *args, **kwargs):
                if args[0] == 'rebase':
                    observed.update(
                        head=NEW, ref=[NEW, ''],
                        registration=[f'HEAD={NEW}', f'branch=refs/heads/{BRANCH}'],
                    )
                    reflog.append(kwargs['env']['GIT_REFLOG_ACTION'])
                    return SimpleNamespace(returncode=0, stdout='')
                if args[0] == 'reflog':
                    return SimpleNamespace(returncode=0, stdout='\n'.join(reflog))
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
            with (mock.patch.object(loop_manager, 'git', side_effect=git),
                  mock.patch.object(loop_manager, 'remote_branch_oid',
                                    return_value=OLD),
                  mock.patch.object(loop_manager, '_owned_rebase_journal_state',
                                    side_effect=lambda *_: observed.copy()),
                  mock.patch.object(loop_manager.checkpoint_ownership,
                                    'rebase_preserves_commits', return_value=True)):
                self.assertTrue(loop_manager._finish_owned_direct_rebase(
                    state, connection, repo, repo_key, 'G-091', worktree,
                    BRANCH,
                ))
            self.assertEqual(len(journal.verified_managed_transitions(
                state, repo_key, 'plan:G-091',
            )), 1)

    def test_completed_direct_rebase_clears_only_its_checkpoint_operation(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repository = ExecutionCheckpointRepository(connection)
        repository.record(
            'repo', 'G-091', expected_generation=0, operation='rebase',
            expected_head=OLD, target_base_oid=NEW,
            observed_remote_oid=OLD, snapshot={'owned': True},
        )
        with mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(
            stdout=NEW + '\n', returncode=0,
        )):
            loop_manager._complete_direct_rebase_checkpoint(
                connection, 'repo', 'G-091', Path('/owned'), NEW,
            )
        current = repository.get('repo', 'G-091')
        self.assertIsNone(current.operation)
        self.assertEqual(current.expected_head, NEW)
        self.assertEqual(current.observed_remote_oid, OLD)
        self.assertEqual(current.snapshot, {'owned': True})


class DetachedRebaseOwnershipTests(unittest.TestCase):
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
                worktree=worktree,
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
                return instance

            with mock.patch('execution_handlers.checkpoint_ownership.owned_remote_oid',
                            return_value=OLD):
                for _ in range(2):
                    instance = handler()
                    self.assertTrue(instance._owned_checkpoint_remote(
                        context, {'pr_url': None, 'publication_remote_oid': OLD},
                        OLD, current,
                    ))
                    workspace.validate_managed_worktree.assert_called_with(
                        Path('/repo'), worktree, BRANCH, allow_rebase=True,
                    )
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
