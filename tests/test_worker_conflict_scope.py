import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager


class WorkerConflictScopeUnitTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.repo, self.worktree = Path('/repo'), Path('/managed')
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')")
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status, branch, worktree, workspace_owned) VALUES "
            "('/repo', 'G-092', 'docs/plans/[G-092] Work.md', 'Work', '[]', '[]', 'normal', 'hash', "
            "'IMPLEMENTING', 'loop-manager/G-092', '/managed', 1)")
        self.plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.before = loop_manager.GitSnapshot('partial', None,
            {'app.py': tuple(f'M 100644 old-{stage} {stage}' for stage in (1, 2, 3))}, {},
            {'refs/heads/loop-manager/G-092': ('before', '')},
            {'/managed': ('HEAD=partial', 'detached=True')}, {}, {'rebase-merge': 'owned'})
        self.after = replace(self.before, index={'app.py': ('H 100644 resolved 0',)},
                             dirty={'app.py': (('staged',), 'resolved-file')})
        self.service = mock.Mock()
        self.service.require_owned.return_value = (None, SimpleNamespace(
            status=loop_manager.managed_rebase_validation.RebaseStatus.WAITING))
        for patcher in (
            mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.after),
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(loop_manager, '_current_rebase_service', return_value=self.service),
            mock.patch.object(loop_manager, 'validate_active_execplan_metadata', return_value={'id': 'G-092'}),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'git', side_effect=AssertionError('unit test has no Git writes')),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def validate(self):
        return loop_manager.validate_worker_workspace_scope(
            self.repo, self.worktree, 'loop-manager/G-092', 'main', self.plan['path'],
            self.plan, self.before, allow_rebase=True, connection=self.connection)

    def test_normal_stage_resolution_requires_and_uses_current_owned_rebase(self):
        self.assertEqual(self.validate(), {'id': 'G-092'})
        self.service.require_owned.assert_called_once()

    def test_allow_rebase_alone_cannot_accept_uncertain_metadata_remote_or_scope(self):
        self.service.require_owned.side_effect = RuntimeError('current ownership or remote mismatch')
        with self.assertRaisesRegex(loop_manager.AgentScopeError, '소유 rebase'):
            self.validate()
        loop_manager.validate_active_execplan_metadata.assert_not_called()

    def test_flag_change_mixed_with_resolution_still_fails_after_owned_rebase(self):
        for tag in ('S', 'h'):
            self.after = replace(self.after, index={'app.py': (f'{tag} 100644 resolved 0',)})
            with self.subTest(tag=tag), self.assertRaisesRegex(loop_manager.AgentScopeError, 'index flags'):
                self.validate()
