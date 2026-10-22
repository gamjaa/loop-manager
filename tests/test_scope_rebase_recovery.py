from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
import loop_manager
from loop_manager_internal.recovery import blocked_agent_service
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.git import git_command, advance_remote_file
from tests.support.worker import isolated_scope_resources
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class ScopeRebaseRecoveryTest(AgentScopeFixture, unittest.TestCase):
    def test_shared_worktree_changes_do_not_discard_saved_user_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            state = Path(directory) / 'state'
            branch, worktree = loop_manager.expected_execplan_workspace(state, str(repo), 'G-001')
            loop_manager.make_worktree(repo, worktree, branch, 'main')
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (str(repo),))
            base = git_command(repo, 'rev-parse', 'origin/main')
            loop_manager.register_execplans(connection, str(repo), repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))], base,
                loop_manager.planning_input_fingerprint(repo, 'main'))
            connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', active_sha=?, branch=?, "
                "worktree=?, workspace_owned=1, worker_session_id='saved session'",
                (base, branch, str(worktree)))
            before = loop_manager.capture_git_snapshot(worktree)
            (worktree / 'src/app.py').write_text('VALUE = 2\n', encoding='utf-8')
            git_command(worktree, 'add', 'src/app.py')
            git_command(worktree, 'commit', '-m', 'Preserved user commit')
            other = Path(directory) / 'other'
            git_command(repo, 'worktree', 'add', '-b', 'other-task', str(other), 'HEAD')
            (other / 'README.md').write_text('Other task work\n', encoding='utf-8')
            git_command(repo, 'tag', 'other-task-marker')
            current = loop_manager.capture_git_snapshot(worktree)
            role = 'worker'
            connection.execute("UPDATE exec_plans SET agent_role=?, agent_snapshot=?, agent_result_snapshot=?",
                               (role, before.serialize(), current.serialize()))
            connection.commit()
            runtime = loop_manager.runtime_state.RuntimeStateRepository(connection)
            runtime.block_plan(str(repo), 'G-001', reason_code='managed_worktree_scope_violation',
                               evidence='legacy Git access comparison', release_fingerprint='same input')
            block = loop_manager.scoped_blocking.plan_block(connection, str(repo), 'G-001')
            self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
                connection, repo, state, str(repo), block))
            after = loop_manager.capture_git_snapshot(worktree)
            self.assertEqual((after.head, after.index, after.dirty), (current.head, current.index, current.dirty))
            row = connection.execute('SELECT * FROM exec_plans').fetchone()
            self.assertEqual(row['worker_session_id'], 'saved session')
            self.assertIsNone(row['blocked_reason'])
            self.assertEqual(row['agent_snapshot'], before.serialize())
            self.assertEqual((other / 'README.md').read_text(encoding='utf-8'), 'Other task work\n')
            self.assertEqual((worktree / 'src/app.py').read_text(encoding='utf-8'), 'VALUE = 2\n')

    def test_same_input_recovery_keeps_staged_unstaged_and_untracked_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            state = Path(directory) / 'state'
            branch, worktree = loop_manager.expected_execplan_workspace(state, str(repo), 'G-001')
            loop_manager.make_worktree(repo, worktree, branch, 'main')
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (str(repo),))
            base = git_command(repo, 'rev-parse', 'origin/main')
            loop_manager.register_execplans(connection, str(repo), repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))], base,
                loop_manager.planning_input_fingerprint(repo, 'main'))
            connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', active_sha=?, branch=?, "
                "worktree=?, workspace_owned=1, worker_session_id='saved session'",
                (base, branch, str(worktree)))
            before = loop_manager.capture_git_snapshot(worktree)
            (worktree / 'src/app.py').write_text('VALUE = 3\n', encoding='utf-8')
            git_command(worktree, 'add', 'src/app.py')
            (worktree / 'README.md').write_text('User unstaged work\n', encoding='utf-8')
            (worktree / 'tests/evidence.txt').write_text('User evidence\n', encoding='utf-8')
            current = loop_manager.capture_git_snapshot(worktree)
            role = 'reviewer'
            connection.execute("UPDATE exec_plans SET agent_role=?, agent_snapshot=?, agent_result_snapshot=?",
                               (role, before.serialize(), current.serialize()))
            connection.commit()
            runtime = loop_manager.runtime_state.RuntimeStateRepository(connection)
            runtime.block_plan(str(repo), 'G-001', reason_code='managed_worktree_scope_violation',
                               evidence='legacy Git access comparison', release_fingerprint='same input')
            block = loop_manager.scoped_blocking.plan_block(connection, str(repo), 'G-001')
            self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
                connection, repo, state, str(repo), block))
            after = loop_manager.capture_git_snapshot(worktree)
            self.assertEqual((after.head, after.index, after.dirty), (current.head, current.index, current.dirty))
            row = connection.execute('SELECT * FROM exec_plans').fetchone()
            self.assertEqual(row['worker_session_id'], 'saved session')
            self.assertIsNone(row['blocked_reason'])
            self.assertEqual(row['agent_snapshot'], before.serialize())
            self.assertIn('staged', after.dirty['src/app.py'][0])
            self.assertIn('unstaged', after.dirty['README.md'][0])
            self.assertIn('untracked', after.dirty['tests/evidence.txt'][0])


@isolated_git_test
class BlockedAgentBaselineResumeUnitTests(unittest.TestCase):
    def test_interrupted_dirty_baseline_restore_reuses_verified_tree(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        loop_manager.scoped_blocking.ensure_schema(connection)
        loop_manager.blocked_checkpoint_state.ensure_schema(connection)
        before = loop_manager.GitSnapshot(
            'a' * 40, 'loop-manager/G-001', {},
            {'notes.txt': (('unstaged',), 'saved-content')}, {}, {}, {}, {})
        clean = loop_manager.GitSnapshot(
            before.head, before.branch, {}, {}, {}, {}, {}, {})
        baseline = json.dumps({'worktree_tree': 'b' * 40, 'index_tree': 'c' * 40})
        recovery = dict(version=1, state='stashed', source_snapshot=before.serialize(),
                        original_head=before.head, violation_head=before.head,
                        branch=before.branch, stash_marker='owned-stash', stash_oid='stash',
                        violation_ref='refs/loop-manager/diagnostic/test')
        connection.execute(
            'INSERT INTO projects (repo, base_branch, phase, agent_snapshot, agent_baseline, agent_recovery) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            ('repo', 'main', 'PLAN_BLOCKED', before.serialize(), baseline, json.dumps(recovery)))
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, "
            "content_hash, status, branch, worktree, workspace_owned) "
            "VALUES ('repo', 'G-001', 'docs/plans/[G-001] Work.md', 'Work', '[]', '[]', "
            "'high', 'hash', 'PLAN_BLOCKED', 'loop-manager/G-001', ?, 1)", (str(Path('/state/G-001')),)
        )
        connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'plan', 'default_worktree_mutation', 'saved cause')"
        )
        connection.execute(
            "INSERT INTO blocked_checkpoints (repo, plan_id, request_identity, phase, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'owned-request', 'requested', 'default_worktree_mutation', 'saved cause')"
        )
        block = dict(connection.execute(
            "SELECT * FROM execplan_blocks WHERE plan_id = 'G-001'").fetchone())
        recovery.update(owner_plan_id='G-001', owner_block=block,
                        owner_request_identity='owned-request', checkpoint_generation=0)
        connection.execute(
            "UPDATE projects SET agent_recovery = ? WHERE repo = 'repo'",
            (json.dumps(recovery),))
        connection.commit()

        def update_project(db, repo, **values):
            db.execute('UPDATE projects SET agent_recovery = ? WHERE repo = ?',
                       (values['agent_recovery'], repo))
            db.commit()

        def effects(*, interrupted, observed=clean):
            port = mock.Mock()
            port._deserialize_blocked_agent_recovery.side_effect = blocked_agent_service._deserialize_blocked_agent_recovery
            port._serialize_blocked_agent_recovery.side_effect = blocked_agent_service._serialize_blocked_agent_recovery
            port._blocked_recovery_stash_oid.return_value = 'stash'
            port._config_entries.return_value = before.config
            port.capture_git_snapshot.side_effect = [observed, before]
            port._same_local_snapshot.return_value = False
            port._recovered_agent_local_state_matches.side_effect = [False, True]
            port.update_project.side_effect = update_project
            port.git.side_effect = lambda _worktree, command, *args: SimpleNamespace(
                stdout=('b' * 40 if command == 'write-tree' else 'd' * 40))
            if interrupted:
                port._restore_agent_baseline.side_effect = KeyboardInterrupt()
            return port

        first = effects(interrupted=True)
        with self.assertRaises(KeyboardInterrupt):
            blocked_agent_service._complete_blocked_agent_recovery(
                first, connection, 'repo', Path('/state/G-001'))
        saved = json.loads(connection.execute(
            "SELECT agent_recovery FROM projects WHERE repo = 'repo'").fetchone()[0])
        self.assertEqual(saved['state'], 'restoring_baseline')

        changed = loop_manager.GitSnapshot(
            before.head, before.branch, {},
            {'notes.txt': (('unstaged',), 'later-user-edit')}, {}, {}, {}, {})
        unsafe = effects(interrupted=False, observed=changed)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                unsafe, connection, 'repo', Path('/state/G-001'))
        unsafe._restore_agent_baseline.assert_not_called()

        connection.execute('SAVEPOINT changed_cause')
        connection.execute(
            "UPDATE blocked_checkpoints SET request_identity = 'new-cause' WHERE plan_id = 'G-001'")
        stale_cause = effects(interrupted=False)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                stale_cause, connection, 'repo', Path('/state/G-001'))
        stale_cause._restore_agent_baseline.assert_not_called()
        connection.execute('ROLLBACK TO changed_cause')
        connection.execute('RELEASE changed_cause')

        connection.execute('SAVEPOINT stale_generation')
        ExecutionCheckpointRepository(connection).record(
            'repo', 'G-001', expected_generation=0, operation=None,
            expected_head=None, target_base_oid=None, observed_remote_oid=None, snapshot=None)
        stale = effects(interrupted=False)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                stale, connection, 'repo', Path('/state/G-001'))
        stale._restore_agent_baseline.assert_not_called()
        connection.execute('ROLLBACK TO stale_generation')
        connection.execute('RELEASE stale_generation')

        first_read_tree = loop_manager.GitSnapshot(
            before.head, before.branch,
            {'notes.txt': ('H 100644 blob ' + 'e' * 40 + '\tnotes.txt',)},
            {'notes.txt': (('staged',), 'saved-content')}, {}, {}, {}, {})
        interrupted_after_read_tree = effects(interrupted=False, observed=first_read_tree)
        blocked_agent_service._complete_blocked_agent_recovery(
            interrupted_after_read_tree, connection, 'repo', Path('/state/G-001'))
        interrupted_after_read_tree._restore_agent_baseline.assert_called_once()
        interrupted_after_read_tree._mark_blocked_agent_isolated.assert_called_once()

        changed_after_read_tree = loop_manager.GitSnapshot(
            before.head, before.branch, first_read_tree.index,
            {'notes.txt': (('staged', 'unstaged'), 'later-user-edit')}, {}, {}, {}, {})
        unsafe_after_read_tree = effects(interrupted=False, observed=changed_after_read_tree)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                unsafe_after_read_tree, connection, 'repo', Path('/state/G-001'))
        unsafe_after_read_tree._restore_agent_baseline.assert_not_called()

        restarted = effects(interrupted=False)
        blocked_agent_service._complete_blocked_agent_recovery(
            restarted, connection, 'repo', Path('/state/G-001'))
        restarted._restore_agent_baseline.assert_called_once()
        restarted._mark_blocked_agent_isolated.assert_called_once()

    def test_isolated_recovery_accepts_advanced_checkpoint_generation(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        loop_manager.scoped_blocking.ensure_schema(connection)
        loop_manager.blocked_checkpoint_state.ensure_schema(connection)
        before = loop_manager.GitSnapshot(
            'a' * 40, 'loop-manager/G-001', {}, {}, {}, {}, {}, {})
        connection.execute(
            'INSERT INTO projects (repo, base_branch, phase, branch, worktree) '
            "VALUES ('repo', 'main', 'PLAN_BLOCKED', 'loop-manager/G-001', ?)", (str(Path('/state/G-001')),))
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, "
            "content_hash, status, branch, worktree, workspace_owned) "
            "VALUES ('repo', 'G-001', 'docs/plans/[G-001] Work.md', 'Work', '[]', '[]', "
            "'high', 'hash', 'PLAN_BLOCKED', 'loop-manager/G-001', ?, 1)", (str(Path('/state/G-001')),))
        connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'plan', 'default_worktree_mutation', 'saved cause')")
        connection.execute(
            "INSERT INTO blocked_checkpoints (repo, plan_id, request_identity, phase, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'owned-request', 'requested', 'default_worktree_mutation', 'saved cause')")
        block = dict(loop_manager.scoped_blocking.plan_block(connection, 'repo', 'G-001'))
        recovery = dict(version=1, state='isolated', source_snapshot=before.serialize(),
                        baseline_snapshot=before.serialize(), original_head=before.head,
                        violation_head=before.head, branch=before.branch,
                        stash_marker='owned-stash', violation_ref='refs/loop-manager/diagnostic/test',
                        owner_plan_id='G-001', owner_block=block,
                        owner_request_identity='owned-request', checkpoint_generation=0)
        connection.execute("UPDATE projects SET agent_recovery = ? WHERE repo = 'repo'",
                           (json.dumps(recovery),))
        connection.execute("UPDATE exec_plans SET agent_recovery = ? "
                           "WHERE repo = 'repo' AND plan_id = 'G-001'",
                           (json.dumps(recovery),))
        ExecutionCheckpointRepository(connection).record(
            'repo', 'G-001', expected_generation=0, operation='rebase',
            expected_head=before.head, target_base_oid='b' * 40,
            observed_remote_oid=None, snapshot=None)
        connection.commit()

        effects = mock.Mock()
        effects._deserialize_blocked_agent_recovery.side_effect = blocked_agent_service._deserialize_blocked_agent_recovery
        effects._config_entries.return_value = before.config
        effects.project_execplan.return_value = connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-001'").fetchone()
        effects._rebase_in_progress_for_branch.return_value = True
        self.assertTrue(blocked_agent_service.resume_interrupted_blocked_agent_recovery(
            effects, connection, Path('/repo'), Path('/state'), 'repo'))
        self.assertTrue(effects.make_worktree.call_args.kwargs['allow_rebase'])
        effects._complete_blocked_agent_recovery.assert_not_called()

        self.assertEqual(connection.execute(
            "SELECT agent_recovery FROM projects WHERE repo = 'repo'").fetchone()[0],
            json.dumps(recovery),
        )
        self.assertIsNone(connection.execute(
            "SELECT agent_recovery FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-001'"
        ).fetchone()[0])
        checkpoint = ExecutionCheckpointRepository(connection).get('repo', 'G-001')
        self.assertEqual(checkpoint.snapshot['retired_agent_recoveries'], [json.dumps(recovery)])
        effects._config_entries.assert_not_called()
        effects.make_worktree.reset_mock()
        self.assertFalse(blocked_agent_service.resume_interrupted_blocked_agent_recovery(
            effects, connection, Path('/repo'), Path('/state'), 'repo'))
        effects.make_worktree.assert_not_called()
