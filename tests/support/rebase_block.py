from tests.support.lifecycle import fixture_cleanup
from dataclasses import replace
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.git import rebase_block_reconciliation


class RebaseBlockFixture:
    """Reusable setup and adapters; no discoverable test methods."""

    def setUp(self):
        from tests.support.current_rebase import current_rebase_case
        self.case = current_rebase_case(self)
        self.connection = self.case.fixture.connection
        self.checkpoints = self.case.checkpoint
        self.repo, self.plan_id = 'repo', 'G-047'
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute("UPDATE projects SET planning_attempt_fingerprint = NULL")
        self.connection.execute("UPDATE exec_plans SET workspace_owned = 1")
        self.connection.commit()
        self.is_plan_block = True
        def patch(*args, **kwargs):
            patcher = mock.patch.object(*args, **kwargs)
            value = patcher.start()
            self.addCleanup(patcher.stop)
            return value
        self.factory = patch(loop_manager, '_current_rebase_service',
            side_effect=lambda *_a, **_k: self.case.factory(None))
        self.git_write = patch(loop_manager, 'git',
            side_effect=AssertionError('recovery must not write or invoke Git directly'))
        patch(loop_manager, 'operation_log')

    def block(self, *, project=False, separate=None, reason=None):
        self.is_plan_block = not project
        reason = reason or ('repository_state_unsafe' if project else 'managed_worktree_conflict')
        evidence = (rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC
                    if project else 'G-088 repeated conflict with changed plan inputs')
        with self.connection:
            if project:
                self.connection.execute("UPDATE projects SET phase = 'PROJECT_BLOCKED'")
                self.connection.execute("INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES ('repo', 'project', ?, 'G-047', ?, 'WORKING')", (reason, evidence))
            if not project or separate:
                cause = separate or reason
                self.connection.execute("UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
                    "blocked_reason = ?, last_error = 'original block text', "
                    "blocked_fingerprint = 'old-input'", (cause,))
                self.connection.execute("INSERT INTO execplan_blocks "
                    "(repo, plan_id, scope, reason_code, evidence) "
                    "VALUES ('repo', 'G-047', 'plan', ?, ?)",
                    (cause, 'independent original cause' if separate else evidence))
                if not project:
                    self.connection.execute("UPDATE projects SET phase = 'PLAN_BLOCKED'")
            subject = 'project' if project else 'plan:G-047'
            self.connection.execute("INSERT INTO notified_block_incidents "
                "(repo, subject, block_key, event_key, payload) VALUES ('repo', ?, 'block', 'event', '{}')",
                (subject,))
        return evidence

    def resume(self):
        if self.is_plan_block:
            return loop_manager.resume_plan_blocks(self.connection, Path('/repo'), Path('/state'), 'repo', 'main')
        return loop_manager.resume_project_block_if_safe(self.connection, Path('/repo'), Path('/state'), 'repo')

    def snapshot(self):
        return {table: [dict(row) for row in self.connection.execute('SELECT * FROM ' + table)]
                for table in ('projects', 'exec_plans', 'execplan_blocks', 'project_blocks',
                              'execution_checkpoints', 'recovery_records', 'notified_block_incidents')}

    def pending(self, *, staged=False):
        from dataclasses import replace
        from loop_manager_internal.git.managed_rebase_validation import FileChange, IndexEntry
        self.case.observed = replace(self.case.observed, head='partial', branch=None,
            branch_ref=('before', ''), registration=('HEAD=partial', 'detached=True'),
            branch_worktrees=(), operations=(('rebase-merge', 'operation'),),
            metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                      ('orig-head', 'before'), ('onto', 'base')),
            index=(IndexEntry('app.py', '100644', 'manual', 0 if staged else 2,
                              'H' if staged else 'M', 0 if staged else 0x2000),),
            staged=(FileChange('app.py', '100644', '100644', 'old', 'manual', 'M'),) if staged else (),
            index_clean=False)


def rebase_block_fixture(test):
    fixture = fixture_cleanup(test, RebaseBlockFixture())
    fixture.setUp()
    return fixture
