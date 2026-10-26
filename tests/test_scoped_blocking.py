import json
from dataclasses import replace
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.notifications import notifications
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode, BlockScope


from tests.support.scoped_recovery import ScopedGateFixture
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository


class ReservedWorkspaceRecoveryTests(ScopedGateFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.worktree = Path('/state/G-001').resolve()
        self.branch = 'loop-manager/G-001'
        self.head = 'a' * 40
        self.plan('G-001', 'IMPLEMENTING', branch=self.branch)
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET workspace_owned=2, worktree=?, active_sha=?, "
                "worker_session_id='saved-session', retry_count=2 WHERE repo=?",
                (str(self.worktree), 'b' * 40, self.repo))
            self.connection.execute(
                "UPDATE projects SET phase='WORKING', plan_path='docs/plans/[G-001] Work.md' WHERE repo=?",
                (self.repo,))
            scoped_blocking.record_project_block(
                self.connection, self.repo, BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
                'reservation HEAD differs from active_sha', 'saved',
                plan_id='G-001', previous_phase='WORKING')
        self.checkpoints = ExecutionCheckpointRepository(self.connection)
        self.before = dict(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        self.live = {'exists': True, 'registration': {
            'head': self.head, 'branch': 'refs/heads/' + self.branch, 'locked': 'owned-marker'},
            'index': 'tree'}
        self.snapshot = loop_manager.GitSnapshot(self.head, self.branch, {}, {}, {}, {}, {}, {})
        self.records = [{'intent': {'expected': self.live}, 'result': {'observed': self.live}}]
        self.rebases = []
        self.enterContext(mock.patch.object(Path, 'is_dir', return_value=True))
        self.enterContext(mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'))
        self.enterContext(mock.patch.object(loop_manager, 'validate_managed_worktree'))
        self.enterContext(mock.patch.object(loop_manager, 'git', return_value=mock.Mock(returncode=0, stdout='')))
        self.enterContext(mock.patch.object(loop_manager, '_managed_worktree_transition_state',
                                           side_effect=lambda *_: self.live))
        self.capture = self.enterContext(mock.patch.object(loop_manager, 'capture_git_snapshot',
                                                           side_effect=lambda *_: self.snapshot))
        self.journal = self.enterContext(mock.patch(
            'loop_manager_internal.git.manager_git_journal.verified_managed_transitions',
            side_effect=lambda *_a, **kwargs: self.records if kwargs.get('kind') == 'managed_worktree_create' else [
                item for item in self.rebases
                if item['intent']['expected'].get('agent_session_id') == kwargs.get('agent_session_id')]))
        self.enterContext(mock.patch('loop_manager_internal.git.manager_git_journal.recover_active'))

    def resume(self):
        return loop_manager.resume_project_block_if_safe(
            self.connection, Path(self.repo), Path('/state'), self.repo)

    def test_owned_rebase_start_recovers_unchanged_clean_creation(self):
        from types import SimpleNamespace
        from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
        self.checkpoints.record(self.repo, 'G-001', expected_generation=0, operation='rebase',
            expected_head=self.head, target_base_oid='b' * 40, observed_remote_oid=None,
            snapshot={'before_head': self.head, 'target_base': 'b' * 40, 'rebase_policy': 'current_state'})
        checkpoint = self.checkpoints.get(self.repo, 'G-001')
        service = mock.Mock(expected=SimpleNamespace(generation=checkpoint.generation,
            before_head=self.head, target_base_oid='b' * 40))
        service.observe.return_value = (SimpleNamespace(head=self.head, branch=self.branch,
            index=(), files=(), operations=()), SimpleNamespace(status=RebaseStatus.START))
        with mock.patch.object(loop_manager, '_current_rebase_service', return_value=service):
            self.assertTrue(self.resume())
        after = self.checkpoints.get(self.repo, 'G-001')
        self.assertEqual((after.operation, after.expected_head, after.target_base_oid),
                         (checkpoint.operation, checkpoint.expected_head, checkpoint.target_base_oid))
        service.adopt.assert_not_called()

    def test_owned_detached_rebase_recovers_reservation_without_completing_operation(self):
        from types import SimpleNamespace
        from loop_manager_internal.git.managed_rebase_validation import IndexEntry, WorktreeFile, RebaseStatus
        current = 'd' * 40
        self.checkpoints.record(self.repo, 'G-001', expected_generation=0, operation='rebase',
            expected_head=self.head, target_base_oid='b' * 40, observed_remote_oid=None,
            snapshot={'before_head': self.head, 'target_base': 'b' * 40, 'rebase_policy': 'current_state'})
        checkpoint = self.checkpoints.get(self.repo, 'G-001')
        self.snapshot = replace(self.snapshot, head=current, branch=None,
            index={'app.py': ('M 100644 conflict 2',)}, dirty={'app.py': (('unstaged',), 'preserved-conflict')},
            operations={'rebase-merge': 'owned-rebase'})
        self.live = {**self.live, 'index': {'unmerged': 'conflict-stage'},
                     'registration': {**self.live['registration'], 'head': current, 'branch': None}}
        observed = SimpleNamespace(head=current, branch=None,
            index=(IndexEntry('app.py', '100644', 'conflict', 2, 'M', 0x2000),),
            files=(WorktreeFile('app.py', ('unstaged',), '100644', 'preserved-conflict'),),
            operations=tuple(self.snapshot.operations.items()))
        service = mock.Mock(expected=SimpleNamespace(generation=checkpoint.generation,
            before_head=self.head, target_base_oid='b' * 40))
        service.observe.return_value = (observed, SimpleNamespace(status=RebaseStatus.WAITING))
        with mock.patch.object(loop_manager, '_current_rebase_service', return_value=service) as factory:
            service.expected.target_base_oid = 'foreign-target'
            self.assertFalse(self.resume())
            service.expected.target_base_oid = 'b' * 40
            self.assertTrue(self.resume())
            self.assertTrue(factory.call_args.kwargs['allow_reserved'])
        after = self.checkpoints.get(self.repo, 'G-001')
        self.assertEqual(after.operation, 'rebase')
        self.assertEqual(after.expected_head, checkpoint.expected_head)
        self.assertEqual(after.target_base_oid, checkpoint.target_base_oid)
        self.assertEqual(self.connection.execute('SELECT active_sha FROM exec_plans').fetchone()[0], self.before['active_sha'])
        service.adopt.assert_not_called()
        service.normalize_frontmatter.assert_not_called()

    def test_existing_adoption_checks_grant_pr_and_keeps_original_head(self):
        from loop_manager_internal.storage import database
        with self.connection:
            self.connection.execute('CREATE TABLE manager_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.connection.execute("INSERT INTO manager_metadata VALUES ('remote_adoption_bootstrap','null')")
            self.connection.execute("UPDATE projects SET origin='fixture/repository'")
        database.record_remote_work_adoption_catalog(self.connection, self.repo, self.head, ('G-001',))
        self.connection.commit()
        self.checkpoints.record(self.repo, 'G-001', expected_generation=0, operation=None,
            expected_head=self.head, target_base_oid='b' * 40, observed_remote_oid=self.head,
            snapshot={'remote_adoption': {'repo': self.repo, 'plan_id': 'G-001', 'version': 1,
                'base': 'main', 'branch': self.branch, 'head': self.head}})
        pr = {'url': 'https://github.com/fixture/repository/pull/1', 'headRefName': self.branch,
              'baseRefName': 'main', 'headRefOid': self.head, 'state': 'OPEN'}
        with mock.patch.object(loop_manager, 'git', return_value=mock.Mock(returncode=0,
                stdout='https://github.com/fixture/repository.git')), \
             mock.patch.object(loop_manager, 'pr_for_branch', return_value={**pr, 'baseRefName': 'foreign'}):
            self.assertFalse(self.resume())
        with mock.patch.object(loop_manager, 'git', return_value=mock.Mock(returncode=0,
                stdout='https://github.com/fixture/repository.git')), \
             mock.patch.object(loop_manager, 'pr_for_branch', return_value=pr) as pr_query:
            pr_query.side_effect = [pr, {**pr, 'headRefOid': 'foreign-head'}]
            self.assertFalse(self.resume())
            self.assertEqual(self.connection.execute('SELECT workspace_owned FROM exec_plans').fetchone()[0], 2)
            pr_query.side_effect = None
            with mock.patch.object(loop_manager, 'reconcile_existing_pull_request') as reconcile:
                for state, merged_at in (('CLOSED', None),):
                    terminal = {**pr, 'state': state, 'mergedAt': merged_at}
                    pr_query.return_value = terminal
                    self.assertFalse(self.resume())
                    self.assertEqual(reconcile.call_args.args[-1]['state'], state)
                    self.assertEqual(reconcile.call_args.args[-2]['branch'], self.branch)
                    self.assertEqual(self.connection.execute('SELECT workspace_owned FROM exec_plans').fetchone()[0], 2)
                self.assertEqual(reconcile.call_count, 1)
            pr_query.return_value = {**pr, 'state': 'UNKNOWN'}
            self.assertFalse(self.resume())
            pr_query.return_value = pr
            grant = self.connection.execute("SELECT value FROM manager_metadata WHERE key LIKE 'remote_adoption:%'").fetchone()[0]
            with self.connection:
                self.connection.execute("UPDATE manager_metadata SET value='{}' WHERE key LIKE 'remote_adoption:%'")
            self.assertFalse(self.resume())
            with self.connection:
                self.connection.execute("UPDATE manager_metadata SET value=? WHERE key LIKE 'remote_adoption:%'", (grant,))
            self.assertTrue(self.resume())
        checkpoint = self.checkpoints.get(self.repo, 'G-001')
        self.assertEqual(checkpoint.snapshot['remote_adoption']['head'], self.head)
        self.assertEqual(checkpoint.snapshot['accepted_work_branch'], self.head)
        self.assertEqual(checkpoint.expected_head, self.head)

    def test_merged_adoption_confirms_reservation_before_real_cleanup_and_releases_block(self):
        self._merged_adoption_cleanup(clean=False)

    def test_clean_merged_adoption_releases_reservation_lease_before_real_removal(self):
        self._merged_adoption_cleanup(clean=True)

    def _merged_adoption_cleanup(self, *, clean):
        from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
        from loop_manager_internal.storage import database
        from loop_manager_internal.application.contracts import CleanupAssessment
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.worktree = root / 'G-001'
        self.worktree.mkdir()
        state_dir = root / 'state'
        resume = lambda: loop_manager.resume_project_block_if_safe(
            self.connection, Path(self.repo), state_dir, self.repo)
        if clean:
            self.enterContext(mock.patch('loop_manager_internal.recovery.plan_recovery.resource_locks',
                                         side_effect=resource_locks))
            self.enterContext(mock.patch.object(loop_manager, '_capture_local_worktree_state',
                return_value=(self.head, self.branch, {}, {})))
            self.enterContext(mock.patch.object(loop_manager, '_git_operation_entries', return_value={}))
            self.enterContext(mock.patch.object(loop_manager, '_managed_worktree_marker', return_value=True))
            self.enterContext(mock.patch.object(loop_manager, '_write_managed_worktree_receipt'))
            self.live = {**self.live, 'index': 'c' * 40}
            self.records = [{'intent': {'expected': self.live}, 'result': {'observed': self.live}}]
        else:
            (self.worktree / 'preserved.txt').write_text('local materials')
        with self.connection:
            self.connection.execute('CREATE TABLE manager_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.connection.execute("INSERT INTO manager_metadata VALUES ('remote_adoption_bootstrap','null')")
            self.connection.execute("UPDATE projects SET origin='fixture/repository'")
            self.connection.execute('UPDATE exec_plans SET worktree=?', (str(self.worktree),))
        database.record_remote_work_adoption_catalog(self.connection, self.repo, self.head, ('G-001',))
        self.connection.commit()
        self.checkpoints.record(self.repo, 'G-001', expected_generation=0, operation=None,
            expected_head=self.head, target_base_oid='b' * 40, observed_remote_oid=self.head,
            snapshot={'remote_adoption': {'repo': self.repo, 'plan_id': 'G-001', 'version': 1,
                'base': 'main', 'branch': self.branch, 'head': self.head}})
        pr = {'url': 'https://github.com/fixture/repository/pull/1', 'headRefName': self.branch,
              'baseRefName': 'main', 'headRefOid': self.head, 'state': 'MERGED', 'mergedAt': 'merged'}
        removals = []
        def git(_repo, *args, **_kwargs):
            if args[:2] == ('worktree', 'remove'):
                self.assertFalse(resource_owned(state_dir, self.repo, 'git:' + str(self.worktree),
                                                owner='workspace-reservation-recovery'))
                self.assertTrue(resource_owned(state_dir, self.repo, 'git:' + str(self.worktree), owner='plan:G-001'))
                self.worktree.rmdir()
                self.live = {'exists': False, 'registration': None, 'index': None}
                removals.append(args)
            elif args[:2] == ('worktree', 'unlock'):
                self.live = {**self.live, 'registration': {**self.live['registration'], 'locked': None}}
            elif clean and args[0] == 'show-ref':
                self.assertTrue(resource_owned(state_dir, self.repo, 'git:' + str(self.worktree)))
            return mock.Mock(returncode=0, stdout=(
                'https://github.com/fixture/repository.git' if args[0] == 'remote' else
                ('c' * 40 if clean else 'tree') if args[0] == 'rev-parse' else ''))
        with mock.patch.object(loop_manager, 'git', side_effect=git), \
             mock.patch.object(loop_manager, 'pr_for_branch', return_value=pr), \
             mock.patch.object(loop_manager, 'remote_base', return_value='b' * 40), \
             mock.patch.object(loop_manager, 'assess_cleanup', return_value=(CleanupAssessment('safe', pr_head_oid=self.head, observation=('stable',))
                 if clean else CleanupAssessment('dirty', 'preserve materials'))), \
             mock.patch.object(loop_manager, 'complete_merged_execplan_worktree',
                               wraps=loop_manager.complete_merged_execplan_worktree) as cleanup:
            records = self.records
            self.records = []
            self.assertFalse(resume())
            cleanup.assert_not_called()
            self.records = records
            self.assertTrue(resume())
            cleanup.assert_called_once()
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual((plan['status'], plan['workspace_owned']), ('MERGED', 1))
        self.assertEqual(plan['active_sha'], self.before['active_sha'])
        pending = self.connection.execute('SELECT * FROM cleanup_pending').fetchone()
        if clean:
            self.assertIsNone(pending)
            self.assertEqual(len(removals), 1)
            self.assertFalse(self.worktree.exists())
        else:
            self.assertEqual(pending['workspace_owned'], 1)
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        if not clean:
            self.assertEqual((self.worktree / 'preserved.txt').read_text(), 'local materials')
        self.assertFalse(resume())

    def test_owned_commit_rebase_and_recorded_dirty_output_preserve_workspace(self):
        import hashlib
        from loop_manager_internal.storage import agent_invocation_state
        agent_invocation_state.ensure_schema(self.connection)
        self.worktree = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        files = {'base.txt': b'committed\n', 'staged.txt': b'staged\n',
                 'unstaged.txt': b'unstaged\n', 'untracked.txt': b'untracked\n'}
        for path, data in files.items():
            (self.worktree / path).write_bytes(data)
        created, committed, rebased, tree, blob = self.head, 'c' * 40, 'd' * 40, 'e' * 40, 'f' * 40
        clean_index = {'base.txt': (f'H 100644 {blob} 0',)}
        clean_commit = replace(self.snapshot, head=committed, index=clean_index)
        start = replace(clean_commit, head=rebased)
        start = replace(start, scope_id='owned-scope',
                        scope_start_digest=hashlib.sha256(start.serialize().encode()).hexdigest())
        accepted = replace(start, index={**clean_index, 'staged.txt': (f'H 100644 {blob} 0',)},
                           dirty={path: ((kind,), loop_manager._path_signature(self.worktree, path))
                                  for path, kind in (('staged.txt', 'staged'), ('unstaged.txt', 'unstaged'),
                                                     ('untracked.txt', 'untracked'))})
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET worktree=?,agent_role='worker',agent_snapshot=?,agent_result_snapshot=?",
                                    (str(self.worktree), start.serialize(), accepted.serialize()))
            self.connection.execute("""INSERT INTO agent_invocations
                (invocation_id,repo,plan_id,scope_id,role,provider,model,effort,session_id,transition,attempt,
                 started_at,execution_status,execution_started_at,execution_finish_evidence,finished_at)
                VALUES ('writer',?,'G-001','owned-scope','worker','codex','model','high','saved-session',
                        'resume',1,'start','executed','started','provider_process_finished','finished')""", (self.repo,))
        self.before = dict(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        self.checkpoints.record(self.repo, 'G-001', expected_generation=0, operation=None,
            expected_head=rebased, target_base_oid='b' * 40, observed_remote_oid=committed,
            snapshot={'before_head': created, 'after_head': committed, 'commit_message': 'Owned commit',
                      'commit_tree_oid': tree})
        self.live = {**self.live, 'registration': {**self.live['registration'], 'head': rebased}, 'index': 'staged-tree'}
        self.rebases = [{'intent': {'expected': {'branch': self.branch}, 'before': json.loads(clean_commit.serialize())},
                         'result': {'observed': json.loads(replace(start, scope_id=None, scope_start_digest=None).serialize())}}]
        def git(_repo, *args, **_kwargs):
            if args[:1] == ('ls-tree',):
                output = '' if args[-1] == created else f'100644 blob {blob}\tbase.txt\0'
            elif args == ('show', '-s', '--format=%P', committed):
                output = created
            elif args == ('show', '-s', '--format=%B', committed):
                output = 'Owned commit\n'
            elif args == ('rev-parse', f'{committed}^{{tree}}'):
                output = tree
            elif args[:1] == ('show-ref',):
                output = ''
            else:
                raise AssertionError(args)
            return mock.Mock(returncode=0, stdout=output)
        with mock.patch.object(loop_manager, 'git', side_effect=git):
            # Matching HEAD never excuses a missing transition or changed file.
            self.snapshot = accepted
            rebase_evidence = self.rebases
            self.rebases = []
            self.assertFalse(self.resume())
            self.rebases = rebase_evidence
            self.snapshot = replace(accepted, dirty={**accepted.dirty, 'untracked.txt': (('untracked',), 'foreign')})
            self.assertFalse(self.resume())
            self.snapshot = accepted
            with mock.patch.object(ExecutionCheckpointRepository, 'record', side_effect=sqlite3.OperationalError('write failed')):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'write failed'):
                    self.resume()
            self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
            self.rebases = [{'intent': {'expected': {'branch': self.branch, 'before_head': committed}},
                             'result': {'status': 'current_state',
                                        'observed': json.loads(replace(start, scope_id=None, scope_start_digest=None).serialize())}}]
            self.assertTrue(self.resume())
        after = dict(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        self.assertEqual(after['workspace_owned'], 1)
        for key in ('active_sha', 'worker_session_id', 'retry_count', 'agent_snapshot', 'agent_result_snapshot',
                    'worker_result', 'verify_token', 'review_token'):
            self.assertEqual(after[key], self.before[key], key)
        self.assertEqual({path: (self.worktree / path).read_bytes() for path in files}, files)
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertFalse(self.resume())

    def test_unconfirmed_writer_prevents_git_observation_and_reservation_release(self):
        from loop_manager_internal.storage import task_calls
        task_calls.ensure_schema(self.connection)
        self.connection.commit()
        owner = task_calls.reserve(self.connection, Path('/state'), self.repo, 'G-001', 'agent', 'run')
        task_calls.mark_uncertain(owner)
        self.assertFalse(self.resume())
        self.capture.assert_not_called()
        self.journal.assert_not_called()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
        self.assertIsNotNone(self.connection.execute('SELECT * FROM task_call_reservations').fetchone())

    def test_not_started_writer_is_preserved_while_reservation_is_confirmed(self):
        from loop_manager_internal.storage import task_calls
        task_calls.ensure_schema(self.connection)
        self.connection.commit()
        owner = task_calls.reserve(self.connection, Path('/state'), self.repo, 'G-001', 'verify', 'run')
        self.assertTrue(self.resume())
        self.assertEqual(self.connection.execute('SELECT token FROM task_call_reservations').fetchone()[0], owner.token)
        self.assertEqual(self.connection.execute('SELECT active_sha FROM exec_plans').fetchone()[0], self.before['active_sha'])

    def test_writer_reservation_added_during_observation_preserves_gate(self):
        from loop_manager_internal.storage import task_calls
        task_calls.ensure_schema(self.connection)
        self.connection.commit()
        calls = 0
        def observe(*_):
            nonlocal calls
            calls += 1
            if calls == 4:
                task_calls.reserve(self.connection, Path('/state'), self.repo, 'G-001', 'agent', 'new-run')
            return self.snapshot
        self.capture.side_effect = observe
        self.assertFalse(self.resume())
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))

    def test_result_and_session_without_physical_agent_exit_do_not_release_reservation(self):
        from loop_manager_internal.storage import agent_invocation_state
        agent_invocation_state.ensure_schema(self.connection)
        with self.connection:
            self.connection.execute("""INSERT INTO agent_invocations
                (invocation_id,repo,plan_id,role,provider,model,effort,session_id,transition,attempt,
                 started_at,execution_status,execution_started_at,finished_at,result_kind,result_code)
                VALUES ('writer',?,'G-001','worker','codex','model','high','saved-session',
                        'resume',1,'start','executed','started','result-collected','success',0)""", (self.repo,))
        self.assertFalse(self.resume())
        self.capture.assert_not_called()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
        with self.connection:
            self.connection.execute("UPDATE agent_invocations SET execution_finish_evidence='provider_process_finished'")
        self.assertTrue(self.resume())

    def test_owned_call_proves_prepared_agent_did_not_start(self):
        from loop_manager_internal.storage import agent_invocation_state, task_calls
        agent_invocation_state.ensure_schema(self.connection)
        task_calls.ensure_schema(self.connection)
        self.connection.commit()
        task_calls.reserve(self.connection, Path('/state'), self.repo, 'G-001', 'agent', 'run')
        with self.connection:
            self.connection.execute("""INSERT INTO agent_invocations
                (invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at,execution_status)
                VALUES ('writer',?,'G-001','worker','codex','model','high','start',1,'start','prepared')""", (self.repo,))
            self.connection.execute("UPDATE task_call_reservations SET invocation_id='writer'")
        self.assertTrue(self.resume())
        self.assertIsNotNone(self.connection.execute('SELECT * FROM task_call_reservations').fetchone())
        self.assertIsNone(self.connection.execute('SELECT execution_started_at FROM agent_invocations').fetchone()[0])

    def test_other_plan_claiming_same_resource_is_not_a_recovery_owner(self):
        self.plan('G-002', 'IMPLEMENTING', branch=self.branch)
        self.assertFalse(self.resume())
        self.capture.assert_not_called()
        self.assertEqual(dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()), self.before)

    def test_manual_owner_prevents_reserved_resource_observation(self):
        from loop_manager_internal.storage import manual_work_state
        manual_work_state.ensure_schema(self.connection)
        with self.connection:
            self.connection.execute("""INSERT INTO manual_work_groups
                (repo,group_id,kind,source_branch,requirements,source_head,base_sha,state,members,documents,owner_id)
                VALUES (?,'manual','existing','source','[]','head','base','MANUAL_WORKING','["G-001"]','[]','G-001')""",
                (self.repo,))
            self.connection.execute("INSERT INTO manual_work_members VALUES (?,'G-001','manual')", (self.repo,))
        self.assertFalse(self.resume())
        self.capture.assert_not_called()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
        # A group can reserve the resource before its execution rows exist.
        with self.connection:
            self.connection.execute('DELETE FROM manual_work_members')
            self.connection.execute("UPDATE manual_work_groups SET members='[]',owner_id=NULL,branch=?,worktree=?",
                                    (self.branch, str(self.worktree)))
        self.assertFalse(self.resume())
        self.capture.assert_not_called()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)

    def test_publication_owner_prevents_reserved_resource_observation(self):
        import hashlib
        from loop_manager_internal.storage.database import GENERAL_PLANNING_PUBLICATIONS_SCHEMA
        self.connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
        manifest = json.dumps({'plans': [['plan.md', {'id': 'G-001'}]],
                               'revisions': [], 'deletions': [], 'publication_paths': []})
        with self.connection:
            self.connection.execute("""INSERT INTO general_planning_publications
                (repo,stage,base_sha,commit_sha,snapshot_token,specification_fingerprint,planning_fingerprint,manifest,manifest_hash)
                VALUES (?,'prepared','base','head','snapshot','spec','planning',?,?)""",
                (self.repo, manifest, hashlib.sha256(manifest.encode()).hexdigest()))
        self.assertFalse(self.resume())
        self.capture.assert_not_called()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)

    def test_reserved_creation_releases_stale_project_block_without_changing_execution_basis(self):
        self.assertTrue(self.resume())
        after = dict(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        for field in ('active_sha', 'worker_session_id', 'retry_count', 'worker_result',
                      'verify_token', 'review_token', 'verify_result', 'review_result'):
            self.assertEqual(after[field], self.before[field], field)
        self.assertEqual(after['workspace_owned'], 1)
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = self.checkpoints.get(self.repo, 'G-001')
        self.assertEqual(saved.snapshot['workspace_reservation_recovered']['head'], self.head)
        self.assertFalse(self.resume())
        self.assertEqual(self.checkpoints.get(self.repo, 'G-001'), saved)

    def test_missing_creation_evidence_preserves_reservation_and_project_gate(self):
        self.records = []
        self.assertFalse(self.resume())
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))

    def test_changed_registration_or_files_are_not_approved_by_matching_head(self):
        for changed in (
            {**self.live, 'index': 'foreign-tree'},
            {**self.live, 'registration': {**self.live['registration'], 'locked': 'foreign-marker'}},
        ):
            with self.subTest(live=changed):
                with mock.patch.object(loop_manager, '_managed_worktree_transition_state', return_value=changed):
                    self.assertFalse(self.resume())
        self.snapshot = replace(self.snapshot, dirty={'user.txt': (('untracked',), 'user-data')})
        self.assertFalse(self.resume())
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)

    def test_checkpoint_race_does_not_confirm_or_release_reservation(self):
        calls = 0
        def observe(*_):
            nonlocal calls
            calls += 1
            if calls == 4:
                previous = self.checkpoints.get(self.repo, 'G-001')
                self.checkpoints.record(self.repo, 'G-001', expected_generation=previous.generation,
                                        operation=None, expected_head=previous.expected_head,
                                        target_base_oid=previous.target_base_oid,
                                        observed_remote_oid=previous.observed_remote_oid,
                                        snapshot={'new-input': True})
            return self.snapshot
        self.capture.side_effect = observe
        self.assertFalse(self.resume())
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))


    def test_checkpoint_write_failure_rolls_back_ownership_and_gate_release(self):
        with mock.patch.object(ExecutionCheckpointRepository, 'record', side_effect=sqlite3.OperationalError('write failed')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'write failed'):
                self.resume()
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))

    def test_conflicting_remote_adoption_is_not_replaced_by_creation_journal(self):
        previous = self.checkpoints.get(self.repo, 'G-001')
        self.checkpoints.record(self.repo, 'G-001', expected_generation=previous.generation,
                                operation=None, expected_head=self.head, target_base_oid='b' * 40,
                                observed_remote_oid=self.head, snapshot={'remote_adoption': {
                                    'repo': 'different-repository', 'plan_id': 'G-001',
                                    'branch': self.branch, 'head': self.head, 'base': 'main'}})
        self.assertFalse(self.resume())
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), self.before)


class LegacyBlockedScopeUnitTests(unittest.TestCase):
    def test_missing_plan_catalog_preserves_scope_validation(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        before = loop_manager.GitSnapshot('head', 'loop-manager/G-001', {}, {},
            {'refs/heads/loop-manager/G-001': ('head', '')},
            {str(Path('/managed').resolve()): ('HEAD=head', 'branch=refs/heads/loop-manager/G-001')}, {}, {})
        connection.execute(
            "INSERT INTO projects (repo, base_branch, agent_role, agent_snapshot, agent_result_snapshot) "
            "VALUES ('repo', 'main', 'reviewer', ?, ?)",
            (before.serialize(), before.serialize()))
        connection.commit()
        with (mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=before) as capture,
              mock.patch.object(loop_manager, '_proven_manager_base_refs', return_value=frozenset()),
              mock.patch.object(loop_manager, '_proven_merged_cleanup_paths', return_value=frozenset())):
            loop_manager.validate_blocked_agent_scope(connection, Path('/repo'), 'repo', 'main',
                Path('/managed'), 'docs/plans/[G-001] Work.md')
            changed = replace(before, refs={'refs/heads/loop-manager/G-099': ('foreign', '')})
            connection.execute("UPDATE projects SET agent_result_snapshot = ?", (changed.serialize(),))
            capture.return_value = changed
            with self.subTest():
                loop_manager.validate_blocked_agent_scope(connection, Path('/repo'), 'repo', 'main',
                    Path('/managed'), 'docs/plans/[G-001] Work.md')
        self.assertIsNone(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'exec_plans'").fetchone())


class ScopedBlockingGateTests(ScopedGateFixture, unittest.TestCase):
    def test_initialized_worker_queries_do_not_reenter_schema_ddl(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            state = Path(directory)
            parent = loop_manager.connect_database(state, create=True)
            cleanup.callback(parent.close)
            parent.execute("BEGIN IMMEDIATE")
            started = threading.Barrier(3)

            def query_worker(repo):
                started.wait(timeout=5)
                connection = loop_manager.connect_database(state, run_migrations=False)
                try:
                    connection.execute("PRAGMA busy_timeout = 100")
                    statements = []
                    connection.set_trace_callback(statements.append)
                    self.assertIsNone(loop_manager.active_execplan(connection, repo))
                    self.assertEqual(loop_manager.plan_blocked_execplans(connection, repo), ())
                    self.assertEqual(loop_manager.merge_wait_execplans(connection, repo), ())
                    self.assertEqual(loop_manager.compact_execplan_catalog(connection, repo), "[]")
                    self.assertIsNone(loop_manager.catalog_schedule_decision(connection, repo).active)
                    self.assertFalse(any(
                        sql.lstrip().upper().startswith(("CREATE", "DROP", "ALTER"))
                        and any(name in sql.lower() for name in (
                            "exec_plans", "one_active_execplan_per_project",
                            "execplan_blocks", "project_blocks", "guidance_approvals",
                        ))
                        for sql in statements
                    ), statements)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(query_worker, repo) for repo in ("repo-a", "repo-b")]
                started.wait(timeout=5)
                parent.rollback()
                for future in futures:
                    future.result()


    def test_reopened_conflict_precedes_independent_ready(self):
        plan = self.plan("G-001", "PR_OPEN", branch="loop-manager/G-001")
        self.plan("G-002", priority="critical")
        loop_manager._block_closed_pull_request(self.connection, self.repo, plan, "https://pr/1", "a" * 40)
        opened = {"state": "OPEN", "url": "https://pr/1", "headRefOid": "a" * 40, "mergeable": "CONFLICTING"}

        def start_recovery(connection, _root, _state, repo, project, _pr):
            self.assertIn("[G-001]", project["plan_path"])
            self.assertEqual(loop_manager.project_execplan(connection, repo)["status"], "PR_OPEN")
            loop_manager.update_project(connection, repo, phase="WORKING", pr_conflict_recovery=1)

        with mock.patch("loop_manager.pr_for_branch", return_value=opened), mock.patch(
            "loop_manager.reconcile_existing_pull_request", side_effect=start_recovery
        ) as recover:
            result = loop_manager.synchronize_waiting_pull_requests(self.connection, Path(self.repo), Path("/state"), self.repo)
        recover.assert_called_once()
        self.assertTrue(result.conflict_started)
        decision = loop_manager.catalog_schedule_decision(self.connection, self.repo)
        self.assertEqual(decision.active["plan_id"], "G-001")
        self.assertIsNone(decision.runnable_ready)


    def test_cleanup_project_block_stops_later_merge_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            for plan_id in ("G-001", "G-002"):
                self.plan(plan_id, "PR_OPEN", branch=f"loop-manager/{plan_id}")
            self.connection.execute("UPDATE exec_plans SET workspace_owned = 0, worktree = ?", (directory,))
            self.connection.commit()
            merged = {"state": "MERGED", "url": "https://pr/1", "headRefOid": "a" * 40}
            with mock.patch("loop_manager.pr_for_branch", return_value=merged) as read, mock.patch("loop_manager._recorded_base_fetch", return_value="a" * 40) as fetched, mock.patch(
                "loop_manager.complete_merged_execplan_worktree", wraps=loop_manager.complete_merged_execplan_worktree
            ) as cleanup:
                loop_manager.synchronize_waiting_pull_requests(self.connection, Path(self.repo), Path(directory), self.repo)
            self.assertEqual(read.call_count, 2)
            cleanup.assert_called_once()
            fetched.assert_called_once_with(
                Path(self.repo), Path(directory), "main", owner="project",
            )
            self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
            statuses = dict(self.connection.execute("SELECT plan_id, status FROM exec_plans"))
            self.assertEqual(statuses, {"G-001": "MERGED", "G-002": "PR_OPEN"})


    def test_reason_mapping_and_atomic_plan_project_records(self):
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
                "same verify fingerprint",
                "verify-fingerprint",
            )

        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        block = self.connection.execute(
            "SELECT * FROM execplan_blocks WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(
            (block["scope"], block["reason_code"], block["release_fingerprint"]),
            ("plan", "verify_retry_exhausted", "verify-fingerprint"),
        )

        with self.assertRaises(ValueError):
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.PULL_REQUEST_CLOSED,
                "wrong scope",
                None,
                plan_id="G-001",
                previous_phase=None,
            )

        self.plan("G-002", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.SHARED_REF_MUTATION,
                "refs/heads/main changed",
                "repository-fingerprint",
                plan_id="G-002",
                previous_phase="WORKING",
            )
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        active = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
        self.assertEqual(active["status"], "IMPLEMENTING")
        scoped_blocking.validate_records(self.connection, self.repo)

        expected_plan_reasons = {
            BlockReasonCode.REMOTE_BRANCH_DELETED,
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
            BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
            BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
            BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
            BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
            BlockReasonCode.USER_DECISION_REQUIRED,
            BlockReasonCode.COMMON_DEFECT_PENDING,
            BlockReasonCode.PULL_REQUEST_CLOSED,
            BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
            BlockReasonCode.PLAN_INPUT_INVALID,
            BlockReasonCode.WORKER_OUTPUT_INVALID,
        }
        self.assertEqual(
            {
                reason
                for reason in BlockReasonCode
                if scoped_blocking.reason_scope(reason) is BlockScope.PLAN
            },
            expected_plan_reasons,
        )


    def test_waiting_rows_do_not_occupy_the_default_management_slot(self):
        self.plan("G-001", "PR_OPEN", branch="loop-manager/G-001")
        self.plan("G-002", "PR_OPEN", branch="loop-manager/G-002")
        self.plan("G-003", "IMPLEMENTING")
        self.plan("G-004", "READY")
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository, RuntimeStateError
        with self.assertRaises(RuntimeStateError):
            RuntimeStateRepository(self.connection).activate(self.repo,'G-004',activation_sha='base',
                branch='loop-manager/G-004',worktree='/managed/G-004')

        self.connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED' "
            "WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        )
        self.connection.execute(
            "INSERT INTO execplan_blocks "
            "(repo, plan_id, scope, reason_code, evidence) "
            "VALUES (?, 'G-003', 'plan', 'plan_input_invalid', 'decision')",
            (self.repo,),
        )
        self.plan("G-005", "REVIEWING")
        self.assertEqual(
            loop_manager.active_execplan(self.connection, self.repo)["plan_id"],
            "G-005",
        )


    def test_dependency_gate_selects_independent_ready_while_others_wait(self):
        blocked = self.plan("G-001", "IMPLEMENTING", priority="critical")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.USER_DECISION_REQUIRED,
                "decision",
                "decision-fingerprint",
            )
        self.plan("G-002", dependencies=("G-001",), priority="critical")
        self.plan("G-003", priority="normal")
        self.plan(
            "G-004",
            "PR_OPEN",
            priority="high",
            branch="loop-manager/G-004",
        )
        self.plan("G-005", priority="high")

        decision = loop_manager.catalog_schedule_decision(
            self.connection, self.repo
        )
        self.assertIsNone(decision.active)
        self.assertTrue(decision.has_plan_blocked)
        self.assertTrue(decision.has_pr_open)
        self.assertEqual(decision.runnable_ready["plan_id"], "G-005")
        self.assertNotEqual(blocked["plan_id"], decision.runnable_ready["plan_id"])

        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                "cannot determine safe repository state",
                None,
                plan_id="G-001",
                previous_phase=None,
            )
        blocked_decision = loop_manager.catalog_schedule_decision(
            self.connection, self.repo
        )
        self.assertTrue(blocked_decision.project_blocked)
        self.assertIsNone(blocked_decision.runnable_ready)


    def test_reviewer_git_observations_do_not_attribute_access_violations(self):
        baseline = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="loop-manager/G-001",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        turn_ref = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {"refs/codex/turn-diffs/1": ("b" * 40, "")},
            }
        )
        loop_manager.validate_agent_scope(
            "reviewer", Path(self.repo), baseline, turn_ref
        )

        shared_ref = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {"refs/tags/agent-created": ("b" * 40, "")},
            }
        )
        loop_manager.validate_agent_scope(
            "reviewer", Path(self.repo), baseline, shared_ref
        )


        local_change = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "dirty": {"module.py": (("unstaged",), "changed")},
            }
        )
        loop_manager.validate_agent_scope(
            "reviewer", Path(self.repo), baseline, local_change
        )


    def test_existing_turn_diff_project_block_releases_automatically(self):
        baseline = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="loop-manager/G-001",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        current = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "refs": {
                    "refs/codex/turn-diffs/captures/1/session/base": (
                        "b" * 40,
                        "",
                    )
                },
            }
        )
        self.plan(
            "G-001",
            "IMPLEMENTING",
            branch="loop-manager/G-001",
        )
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/state/G-001', "
                "agent_role = 'worker', agent_snapshot = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (baseline.serialize(), self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'WORKING', "
                "plan_path = 'docs/plans/[G-001] Work.md' WHERE repo = ?",
                (self.repo,),
            )
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.TURN_DIFF_REF_MUTATION,
                "worker Git 범위 위반: Codex turn ref changed",
                "repository-fingerprint",
                plan_id="G-001",
                previous_phase="WORKING",
            )

        with mock.patch(
            "loop_manager.validate_execplan_workspace_identity"
        ), mock.patch("loop_manager.validate_managed_worktree"), mock.patch(
            "loop_manager.capture_git_snapshot", return_value=current
        ):
            released = loop_manager.resume_project_block_if_safe(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )

        self.assertTrue(released)
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertEqual(project["phase"], "WORKING")


    def test_all_open_prs_are_read_before_one_conflict_is_selected(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            priority="normal",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        self.plan(
            "G-002",
            "PR_OPEN",
            priority="high",
            branch="loop-manager/G-002",
            pr_url="https://pr/2",
        )
        pulls = {
            "loop-manager/G-001": {
                "url": "https://pr/1",
                "state": "OPEN",
                "headRefName": "loop-manager/G-001",
                "headRefOid": "1" * 40,
                "mergeable": "UNKNOWN",
            },
            "loop-manager/G-002": {
                "url": "https://pr/2",
                "state": "OPEN",
                "headRefName": "loop-manager/G-002",
                "headRefOid": "2" * 40,
                "mergeable": "CONFLICTING",
            },
        }
        observed = []

        def lookup(_repo, branch, _url=None):
            observed.append(branch)
            return pulls[branch]

        with mock.patch("loop_manager.pr_for_branch", side_effect=lookup), mock.patch(
            "loop_manager.reconcile_existing_pull_request", return_value=None
        ) as reconcile:
            result = loop_manager.synchronize_waiting_pull_requests(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )

        self.assertEqual(
            observed,
            ["loop-manager/G-001", "loop-manager/G-002"],
        )
        self.assertTrue(result.conflict_started)
        self.assertEqual(reconcile.call_count, 1)
        mirrored = self.connection.execute(
            "SELECT plan_path FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertIn("[G-002]", mirrored["plan_path"])


    def test_closed_and_reopened_pr_only_updates_its_plan(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        self.plan(
            "G-002",
            "PR_OPEN",
            branch="loop-manager/G-002",
            pr_url="https://pr/2",
        )

        def pulls(state):
            return {
                "loop-manager/G-001": {
                    "url": "https://pr/1",
                    "state": state,
                    "headRefOid": "1" * 40,
                    "mergeable": "UNKNOWN",
                },
                "loop-manager/G-002": {
                    "url": "https://pr/2",
                    "state": "OPEN",
                    "headRefOid": "2" * 40,
                    "mergeable": "UNKNOWN",
                },
            }

        closed = pulls("CLOSED")
        with mock.patch(
            "loop_manager.pr_for_branch",
            side_effect=lambda _repo, branch, _url=None: closed[branch],
        ):
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        statuses = {
            row["plan_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                (self.repo,),
            )
        }
        self.assertEqual(statuses, {"G-001": "PLAN_BLOCKED", "G-002": "PR_OPEN"})

        reopened = pulls("OPEN")
        with mock.patch(
            "loop_manager.pr_for_branch",
            side_effect=lambda _repo, branch, _url=None: reopened[branch],
        ):
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        statuses = {
            row["plan_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                (self.repo,),
            )
        }
        self.assertEqual(statuses, {"G-001": "PR_OPEN", "G-002": "PR_OPEN"})


    def test_project_block_allows_pr_read_but_skips_merge_mutation(self):
        self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                "default worktree changed",
                "git-snapshot",
                plan_id="G-001",
                previous_phase=None,
            )
        merged = {
            "url": "https://pr/1",
            "state": "MERGED",
            "mergedAt": "2026-01-01T00:00:00Z",
            "headRefOid": "1" * 40,
        }
        with mock.patch("loop_manager.pr_for_branch", return_value=merged) as read_pr, mock.patch(
            "loop_manager.git"
        ) as git, mock.patch(
            "loop_manager.complete_merged_execplan_worktree"
        ) as complete:
            loop_manager.synchronize_waiting_pull_requests(
                self.connection, Path(self.repo), Path("/state"), self.repo
            )
        read_pr.assert_called_once()
        git.assert_not_called()
        complete.assert_not_called()
        status = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()[0]
        self.assertEqual(status, "PR_OPEN")


    def test_selected_pr_can_escalate_without_occupying_active_status(self):
        plan = self.plan(
            "G-001",
            "PR_OPEN",
            branch="loop-manager/G-001",
            pr_url="https://pr/1",
        )
        loop_manager.hydrate_execplan_project(self.connection, self.repo, plan)
        with mock.patch(
            "loop_manager._revision_execplan_input_fingerprint",
            return_value="repository-fingerprint",
        ):
            loop_manager.block_active_execplan_from_revision(
                self.connection,
                Path(self.repo),
                self.repo,
                "refs/remotes/origin/main",
                "shared refs changed",
                reason=BlockReasonCode.SHARED_REF_MUTATION,
            )
        stored = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        project_block = scoped_blocking.project_block(self.connection, self.repo)
        self.assertEqual(stored["status"], "PR_OPEN")
        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
        self.assertEqual(project_block["plan_id"], "G-001")
        self.assertEqual(project_block["reason_code"], "shared_ref_mutation")

        with mock.patch(
            "loop_manager.validate_execplan_workspace_identity"
        ), mock.patch("loop_manager.validate_managed_worktree"):
            # An inactive PR needs no local writer or historical Git permission proof.
            self.assertTrue(
                loop_manager.resume_project_block_if_safe(
                    self.connection,
                    Path(self.repo),
                    Path("/state"),
                    self.repo,
                )
            )
        released_project = self.connection.execute(
            "SELECT phase, plan_path FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        self.assertEqual(tuple(released_project), (None, None))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))


    def test_inactive_pr_scope_release_preserves_changed_execution_input(self):
        self.plan('G-001', 'PR_OPEN', branch='loop-manager/G-001', pr_url='https://pr/1')
        with self.connection:
            scoped_blocking.record_project_block(
                self.connection, self.repo, BlockReasonCode.SHARED_REF_MUTATION,
                'old Git comparison', 'same', plan_id='G-001', previous_phase=None)
        matches = scoped_blocking.project_cause_matches
        for table in ('projects', 'exec_plans'):
            def change_input(connection, block):
                matched = matches(connection, block)
                connection.execute(f'UPDATE {table} SET last_error = ? WHERE repo = ?',
                                   (f'new-{table}', self.repo))
                return matched
            with self.subTest(table=table), mock.patch.object(
                    scoped_blocking, 'project_cause_matches', side_effect=change_input):
                self.assertFalse(loop_manager.resume_project_block_if_safe(
                    self.connection, Path(self.repo), Path('/state'), self.repo))
                self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
                self.assertEqual(self.connection.execute(
                    f'SELECT last_error FROM {table} WHERE repo = ?', (self.repo,)).fetchone()[0],
                    f'new-{table}')

    def test_project_release_restores_phase_and_preserves_plan_block(self):
        snapshot = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="main",
            index={},
            dirty={},
            refs={},
            worktrees={},
            config={},
            operations={},
        )
        blocked = self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.USER_DECISION_REQUIRED,
                "plan decision",
                "plan-fingerprint",
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PLANNING', worktree = ?, "
                "agent_role = 'planner', agent_snapshot = ? WHERE repo = ?",
                (self.repo, snapshot.serialize(), self.repo),
            )
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                "default worktree changed",
                "repository-fingerprint",
                plan_id=None,
                previous_phase="PLANNING",
            )
        with mock.patch("loop_manager.capture_git_snapshot", return_value=snapshot):
            released = loop_manager.resume_project_block_if_safe(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
            )
        project = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone()
        retained = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (self.repo, blocked["plan_id"]),
        ).fetchone()
        self.assertTrue(released)
        self.assertEqual(project["phase"], "PLANNING")
        self.assertEqual(retained["status"], "PLAN_BLOCKED")
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))


    def test_block_notifications_distinguish_new_fingerprint_and_scope(self):
        notifications.configure_session(self.connection, enabled=True)
        self.plan('G-001', 'IMPLEMENTING')
        checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        for evidence, fingerprint in (('first evidence', 'one'), ('updated evidence', 'two')):
            with self.connection:
                scoped_blocking.record_plan_block(self.connection, self.repo, 'G-001',
                    BlockReasonCode.REVIEW_RETRY_EXHAUSTED, evidence, fingerprint)
                checkpoints.finish_unpublished(checkpoints.get(self.repo, 'G-001'),
                    'local-only result', retryable=False)
                notifications.enqueue_current_project_events(self.connection, self.repo)
            block = scoped_blocking.plan_block(self.connection, self.repo, 'G-001')
            self.assertEqual((block['scope'], block['evidence'], block['release_fingerprint']),
                             ('plan', evidence, fingerprint))
            self.assertEqual(checkpoints.get(self.repo, 'G-001').phase.value, 'unpublished')
        with self.connection:
            scoped_blocking.record_project_block(self.connection, self.repo,
                BlockReasonCode.SHARED_GIT_CONFIG_MUTATION, 'shared config changed',
                'config-fingerprint', plan_id='G-001', previous_phase=None)
            notifications.enqueue_current_project_events(self.connection, self.repo)
        block = scoped_blocking.project_block(self.connection, self.repo)
        self.assertEqual((block['scope'], block['reason_code'], block['plan_id'], block['release_fingerprint']),
                         ('project', 'shared_git_config_mutation', 'G-001', 'config-fingerprint'))
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-001')['release_fingerprint'], 'two')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)


    def test_plan_block_is_waiting_and_registry_removal_is_rejected(self):
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.PLAN_INPUT_INVALID,
                "invalid plan input",
                "input-fingerprint",
            )
        with mock.patch("loop_manager._run_project_step", return_value=1):
            result = loop_manager.run_project_result(
                self.connection,
                Path("/state"),
                self.repo,
                Path("config.ini"),
                None,
                False,
            )
        self.assertEqual(
            (result.kind, result.exit_code, result.detail),
            (loop_manager.ProjectRunKind.WAITING, 0, "invalid plan input"),
        )
        with self.assertRaises(project_registry.ProjectRegistryError):
            project_registry.remove_project(self.connection, self.repo)


    def test_checkpoint_retry_does_not_occupy_independent_ready_slot(self):
        self.plan("G-001", "IMPLEMENTING", priority="critical")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "review failure",
                "review-fingerprint",
            )
        self.plan("G-002", "READY", priority="normal")
        steps = []

        def project_step(*_args, **_kwargs):
            steps.append(len(steps) + 1)
            if len(steps) == 2:
                self.connection.execute(
                    "UPDATE exec_plans SET status = 'MERGED' "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (self.repo,),
                )
                self.connection.commit()
                return 0
            return 1

        with mock.patch(
            "loop_manager.dispatch_blocked_checkpoints", return_value=0
        ) as checkpoint, mock.patch(
            "loop_manager._run_project_step", side_effect=project_step
        ):
            result = loop_manager.run_project_result(
                self.connection,
                Path("/state"),
                self.repo,
                Path("config.ini"),
                None,
                False,
            )

        self.assertEqual(steps, [1, 2])
        checkpoint.assert_called_once()
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        blocked = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(blocked["status"], "PLAN_BLOCKED")


    def test_legacy_remote_checkpoint_change_prevents_plan_resume(self):
        self.plan(
            "G-001",
            "IMPLEMENTING",
            branch="loop-manager/G-001",
        )
        self.plan("G-002", "READY")
        self.connection.execute(
            "UPDATE exec_plans SET worktree = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("/state/G-001", self.repo),
        )
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.PLAN_INPUT_INVALID,
                "input changed",
                "old-input",
            )
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        )
        repository.finish_published(
            repository.get(self.repo, "G-001"), "a" * 40
        )

        with mock.patch.object(
            loop_manager.checkpoint_adapter.BlockedCheckpointAdapter,
            "observe_remote",
            return_value="b" * 40,
        ):
            resumed = loop_manager.resume_plan_blocks(
                self.connection,
                Path(self.repo),
                Path("/state"),
                self.repo,
                "main",
            )

        self.assertFalse(resumed)
        plan = self.connection.execute(
            "SELECT status, last_error FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIn("소유권", plan["last_error"])
        self.assertEqual(
            loop_manager.catalog_schedule_decision(
                self.connection, self.repo,
            ).runnable_ready["plan_id"],
            "G-002",
        )


    def test_legacy_unscoped_block_is_conservatively_project_blocked(self):
        self.plan("G-001", "READY")
        self.connection.execute(
            "UPDATE exec_plans SET status = 'BLOCKED', last_error = 'legacy text' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        )
        self.connection.execute(
            "UPDATE projects SET phase = 'BLOCKED', plan_path = ? WHERE repo = ?",
            ("docs/plans/[G-001] Work.md", self.repo),
        )
        self.connection.commit()

        loop_manager._ensure_execplans_schema(self.connection)

        project_block = scoped_blocking.project_block(
            self.connection, self.repo
        )
        plan = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(
            project_block["reason_code"], "legacy_unscoped_block"
        )
