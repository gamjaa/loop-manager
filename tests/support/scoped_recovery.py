from tests.support.rebase_block import RebaseBlockFixture
import json
from dataclasses import replace
from contextlib import nullcontext
import sqlite3
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode

class ScopedGateFixture:
    def setUp(self):
        self.enterContext(mock.patch(
            "loop_manager_internal.recovery.plan_recovery.resource_locks",
            side_effect=lambda *args, **kwargs: nullcontext()))
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)
        notifications.ensure_schema(self.connection)
        self.repo = str(Path("/projects/scoped").resolve())
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (self.repo,),
        )
        self.connection.commit()


    def plan(
        self,
        plan_id,
        status="READY",
        *,
        priority="normal",
        dependencies=(),
        branch=None,
        pr_url=None,
    ):
        path = f"docs/plans/[{plan_id}] Work.md"
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, branch, pr_url, workspace_owned
            ) VALUES (?, ?, ?, ?, '[]', ?, ?, 'hash', ?, ?, ?, 1)
            """,
            (
                self.repo,
                plan_id,
                path,
                f"Work {plan_id}",
                json.dumps(list(dependencies)),
                priority,
                status,
                branch,
                pr_url,
            ),
        )
        self.connection.commit()
        return self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (self.repo, plan_id),
        ).fetchone()


class ScopeBaselineFixture:
    def setUp(self):
        from tests.support.current_rebase import current_rebase_case
        from loop_manager_internal.agents.agent_scope_decision import used_git_resources
        self.case = current_rebase_case(self)
        self.connection = self.case.fixture.connection
        self.checkpoints = self.case.checkpoint
        self.worktree = Path('/state/G-047').resolve()
        self.repo_path = Path('/repo').resolve()
        self.before = loop_manager.GitSnapshot('owned-head', 'loop-manager/G-047', {}, {},
            {'refs/heads/loop-manager/G-047': ('owned-head', '')},
            {str(self.worktree): ('HEAD=owned-head', 'branch=refs/heads/loop-manager/G-047'),
             str(self.repo_path): ('HEAD=source', 'state=clean')}, {}, {})
        self.current = replace(self.before, dirty={'app.py': (('unstaged',), 'saved-work')},
            worktrees={**self.before.worktrees, str(self.repo_path): ('HEAD=source', 'state=user-dirty')})
        self.resources = used_git_resources(role='worker', phase='WORKING', base_branch='main',
            branch='loop-manager/G-047', worktree_key=str(self.worktree))
        self.isolated = True
        checkpoint = self.checkpoints.get('repo', 'G-047')
        self.checkpoints.record('repo', 'G-047', expected_generation=checkpoint.generation,
            operation=None, expected_head='owned-head', target_base_oid='base',
            observed_remote_oid=None, snapshot={})
        self.connection.execute("UPDATE projects SET planning_attempt_fingerprint = NULL")
        self.connection.execute("UPDATE exec_plans SET workspace_owned = 1, agent_role = 'worker', "
            "agent_snapshot = ?, agent_result_snapshot = ?, retry_count = 3, review_failure_history = '{\"failure\":2}'",
            (self.before.serialize(), self.current.serialize()))
        self.connection.commit()
        for patcher in (
            mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(loop_manager, '_agent_git_resources',
                              side_effect=lambda *_a, **_k: self.resources if self.isolated else None),
            mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False),
            mock.patch.object(loop_manager, 'git', side_effect=AssertionError('recovery must not write Git or start an Agent')),
            mock.patch.object(loop_manager, 'git_bytes', return_value=b''),
            mock.patch.object(loop_manager, 'operation_log'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


    def project_block(self):
        scoped_blocking.record_project_block(self.connection, 'repo',
            BlockReasonCode.DEFAULT_WORKTREE_MUTATION, 'source checkout changed', 'scope-fingerprint',
            plan_id='G-047', previous_phase='WORKING')


    def resume_project(self):
        return loop_manager.resume_project_block_if_safe(
            self.connection, self.repo_path, Path('/state'), 'repo')


class CurrentRebaseBlockFixture(RebaseBlockFixture):
    def scope_stage_result(self):
        self.pending(staged=True)
        path, branch = str(Path('/state/G-047').resolve()), 'loop-manager/G-047'
        before = loop_manager.GitSnapshot('partial', None,
            {'app.py': tuple(f'M 100644 old-{stage} {stage}' for stage in (1, 2, 3))}, {},
            {f'refs/heads/{branch}': ('before', '')},
            {path: ('HEAD=partial', 'detached=True')}, {}, {'rebase-merge': 'same'})
        result = replace(before, index={'app.py': ('H 100644 manual 0',)},
                         dirty={'app.py': (('staged',), 'manual-file')})
        self.current_scope = result
        self.connection.execute("UPDATE exec_plans SET agent_role = 'worker', agent_snapshot = ?, "
            "agent_result_snapshot = ?, retry_count = 3, review_failure_history = '{\"issue\":2}'",
            (before.serialize(), result.serialize()))
        self.connection.execute("UPDATE projects SET agent_role = 'worker', agent_snapshot = ?, "
            "agent_result_snapshot = ?", (before.serialize(), result.serialize()))
        self.connection.commit()
        for patcher in (
            mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current_scope),
            mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False),
            mock.patch.object(loop_manager, 'requirement_archive_candidates', return_value=()),
            mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=None),
            mock.patch.object(loop_manager, '_proven_merged_cleanup_paths', return_value=frozenset()),
            mock.patch.object(loop_manager, 'git_bytes', return_value=b''),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return before, result


    def exhausted_pending(self):
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        detail = self.block()
        self.pending()
        RecoveryRepository(self.connection).observe(RecoveryObservation(
            RecoveryReason.REBASE_CONFLICT, 'repo', 'G-047', 'WORKING',
            'old-revision', 'old-base', 'conflict', 'old-progress', detail), correction_limit=1)
        # Model a persisted block from the old counter-based policy. Current
        # observations pause instead and must not be used to create this fixture.
        self.connection.execute(
            "UPDATE recovery_records SET pending_action='final_block', "
            "disposition='user_action_required', final_evidence=? "
            "WHERE repo='repo' AND subject='G-047'",
            (json.dumps({'block_reason_code': BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value}),),
        )
        snapshot = loop_manager.GitSnapshot('partial', None,
            {'app.py': ('M 100644 manual 2',)},
            {'app.py': (('unstaged',), 'unresolved-content')},
            {'refs/heads/loop-manager/G-047': ('before', '')},
            {str(Path('/state/G-047').resolve()): ('HEAD=partial', 'detached=True')}, {}, {'rebase-merge': 'operation'})
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET blocked_fingerprint = 'same-input', "
                "blocked_worktree_fingerprint = 'same-input', agent_result_snapshot = ?",
                (snapshot.serialize(),))
        return snapshot
