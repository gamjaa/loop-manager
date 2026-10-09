import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.git import rebase_block_reconciliation
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode


from tests.support.scoped_recovery import ScopedGateFixture


class OwnedScopePublicationRecoveryTests(ScopedGateFixture, unittest.TestCase):
    def test_incomplete_legacy_rebase_plan_block_stays_blocked(self):
        self.plan('G-001', 'PLAN_BLOCKED', branch='loop-manager/G-001')
        reason = rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET blocked_reason = 'managed_worktree_conflict' "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, "
                "evidence) VALUES (?, 'G-001', 'plan', "
                "'managed_worktree_conflict', ?)", (self.repo, reason),
            )
        with (
            mock.patch.object(loop_manager, '_reconcile_rebase_evidence_block',
                              return_value=False) as proof,
            mock.patch.object(loop_manager, 'hydrate_execplan_project') as generic,
        ):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'),
                self.repo, 'main',
            ))
        proof.assert_called_once()
        generic.assert_not_called()
        self.assertIsNotNone(scoped_blocking.plan_block(
            self.connection, self.repo, 'G-001',
        ))


    def test_rebase_false_block_cannot_take_generic_project_release(self):
        plan = self.plan('G-001', 'VERIFYING', branch='loop-manager/G-001')
        reason = rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/managed', active_sha = 'base', "
                "worker_session_id = 'worker' WHERE repo = ? AND plan_id = 'G-001'",
                (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = 'loop-manager/G-001', worktree = '/managed' WHERE repo = ?",
                (plan['path'], self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, "
                "evidence, previous_phase) VALUES (?, 'project', "
                "'repository_state_unsafe', 'G-001', ?, 'VERIFYING')",
                (self.repo, reason),
            )
            self.connection.execute(
                "INSERT INTO recovery_records (repo, subject, identity, reason_code, "
                "disposition, pending_action, phase, base_revision, fingerprint, progress_marker, "
                "occurrence, attempts, detail) VALUES (?, 'G-001', 'retry', "
                "'status_failed', 'transient_retry', 'pause', 'VERIFYING', 'base', "
                "'fingerprint', 'base|worker|', 1, 3, ?)",
                (self.repo, reason),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation='rebase',
                expected_head='before', target_base_oid='base',
                observed_remote_oid=None, snapshot={},
            )
        with (
            mock.patch.object(loop_manager, '_reconcile_rebase_evidence_block',
                              return_value=False) as proof,
            mock.patch.object(loop_manager, 'validate_managed_worktree') as generic,
        ):
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        proof.assert_called_once()
        generic.assert_not_called()
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager.manager_git_journal, 'live_remote_branch_oid',
                              return_value='external-head'),
            mock.patch.object(loop_manager, '_ExecutionLifecyclePorts') as construction,
        ):
            self.assertFalse(loop_manager._reconcile_rebase_evidence_block(
                self.connection, Path(self.repo), Path('/state'), self.repo,
                scoped_blocking.project_block(self.connection, self.repo),
                plan_block=False,
            ))
        construction.assert_not_called()
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager.manager_git_journal, 'live_remote_branch_oid', return_value=None),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, '_ExecutionLifecyclePorts',
                              side_effect=RuntimeError('injected evidence failure')),
        ):
            self.assertFalse(loop_manager._reconcile_rebase_evidence_block(
                self.connection, Path(self.repo), Path('/state'), self.repo,
                scoped_blocking.project_block(self.connection, self.repo),
                plan_block=False,
            ))
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()['phase'], 'PROJECT_BLOCKED')


    def test_project_owner_recovers_pushed_unrebased_review_commit(self):
        branch, published, corrected = 'loop-manager/G-001', 'a' * 40, 'b' * 40
        worktree = Path('/managed')
        plan = self.plan('G-001', 'VERIFYING', branch=branch)
        marker = {'request_identity': 'review-request',
                  'owner_oid': published, 'head': published}
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), corrected, self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'remote inspection failed', 'VERIFYING')",
                (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO blocked_checkpoints "
                "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                "branch, worktree, accepted_remote_oid) "
                "VALUES (?, 'G-001', 'review-request', 'published', "
                "'review_retry_exhausted', 'review', ?, ?, ?)",
                (self.repo, branch, str(worktree), published),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation='push',
                expected_head=corrected, target_base_oid='base',
                observed_remote_oid=published,
                snapshot={'published_review_resume': marker,
                          'remote_before': published},
            )
        current = SimpleNamespace(
            head=corrected, branch=branch, operations={}, dirty={}, index={},
            shared_metadata_complete=True, refs={
                f'refs/heads/{branch}': (corrected, ''),
                f'refs/remotes/origin/{branch}': (corrected, ''),
            },
        )
        live = {'oid': corrected}

        def git(_repo, *args, **_kwargs):
            self.assertEqual(args[0], 'ls-remote')
            return SimpleNamespace(
                returncode=0,
                stdout=f"{live['oid']}\trefs/heads/{branch}\n",
            )

        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value=corrected),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, 'git', side_effect=git),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=published) as owner,
        ):
            live['oid'] = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            live['oid'] = corrected
            owner.return_value = None
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            owner.return_value = published
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            self.assertFalse(owner.call_args.kwargs['require_live'])
            execution = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            )
            pending = execution.get(self.repo, 'G-001')
            self.assertNotIn('blocked_resume', pending.snapshot)
            execution.record(
                self.repo, 'G-001', expected_generation=pending.generation,
                operation=None, expected_head=corrected,
                target_base_oid='base', observed_remote_oid=corrected,
                snapshot={**pending.snapshot,
                          'pushed_checkpoint_head': corrected},
            )
            with self.connection:
                self.connection.execute(
                    "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?",
                    (self.repo,),
                )
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES (?, 'project', 'publication_ownership_uncertain', "
                    "'G-001', 'remote inspection failed again', 'VERIFYING')",
                    (self.repo,),
                )
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertEqual(saved.snapshot['published_review_resume'], marker)
        self.assertNotIn('blocked_resume', saved.snapshot)


    def test_legacy_rebased_project_owner_uses_rebase_proof(self):
        branch, published, rebased = 'loop-manager/G-001', 'a' * 40, 'b' * 40
        worktree = Path('/managed')
        plan = self.plan('G-001', 'IMPLEMENTING', branch=branch)
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = NULL "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'old rebase projection', 'WORKING')", (self.repo,),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation=None,
                expected_head=rebased, target_base_oid='base',
                observed_remote_oid=published,
                snapshot={'reactivated_from_head': published,
                          'base_oid': 'base'},
            )
        current = SimpleNamespace(
            head=rebased, branch=branch, operations={}, dirty={}, index={},
        )
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value=published),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, '_direct_rebase_history_proven',
                              return_value=True) as rebase_proof,
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=published),
        ):
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        rebase_proof.assert_called_once()
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertEqual(saved.snapshot['blocked_resume']['before_head'], published)
        self.assertEqual(saved.snapshot['blocked_resume']['after_head'], rebased)
        self.assertNotIn('published_review_resume', saved.snapshot)


    def test_project_owner_recovers_pending_unrebased_worker_commit(self):
        branch, published, corrected = 'loop-manager/G-001', 'a' * 40, 'b' * 40
        worktree = Path('/managed')
        plan = self.plan('G-001', 'VERIFYING', branch=branch)
        review_marker = {
            'request_identity': 'review-request', 'owner_oid': published,
            'head': published,
        }
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), published, self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'remote inspection failed', 'VERIFYING')",
                (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO blocked_checkpoints "
                "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                "branch, worktree, accepted_remote_oid) "
                "VALUES (?, 'G-001', 'review-request', 'published', "
                "'review_retry_exhausted', 'review', ?, ?, ?)",
                (self.repo, branch, str(worktree), published),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation='commit',
                expected_head=published, target_base_oid='base',
                observed_remote_oid=published,
                snapshot={'published_review_resume': review_marker,
                          'before_head': published,
                          'commit_message': 'Worker correction',
                          'commit_tree_oid': 'c' * 40},
            )
        current = SimpleNamespace(
            head=corrected, branch=branch, operations={}, dirty={}, index={},
            shared_metadata_complete=True, refs={
                f'refs/heads/{branch}': (corrected, ''),
                f'refs/remotes/origin/{branch}': (published, ''),
            },
        )
        commit = {'parent': published, 'tree': 'c' * 40}

        def git(_repo, *args, **_kwargs):
            if args[:3] == ('show', '-s', '--format=%P'):
                return SimpleNamespace(stdout=commit['parent'] + '\n')
            if args[:3] == ('show', '-s', '--format=%B'):
                return SimpleNamespace(stdout='Worker correction\n')
            if args[:1] == ('rev-parse',):
                return SimpleNamespace(stdout=commit['tree'] + '\n')
            raise AssertionError(args)

        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid',
                              return_value=published) as remote,
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager, 'git', side_effect=git),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=published) as owner,
        ):
            commit['parent'] = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            commit['parent'] = published
            commit['tree'] = 'd' * 40
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            commit['tree'] = 'c' * 40
            remote.return_value = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            remote.return_value = published
            owner.return_value = None
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            owner.return_value = published
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            self.assertEqual(owner.call_args.kwargs['before_head'], corrected)
            execution = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            )
            pending = execution.get(self.repo, 'G-001')
            self.assertEqual(pending.operation, 'commit')
            self.assertNotIn('blocked_resume', pending.snapshot)
            execution.record(
                self.repo, 'G-001', expected_generation=pending.generation,
                operation=None, expected_head=corrected,
                target_base_oid='base', observed_remote_oid=published,
                snapshot=pending.snapshot,
            )
            with self.connection:
                self.connection.execute(
                    "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?",
                    (self.repo,),
                )
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES (?, 'project', 'publication_ownership_uncertain', "
                    "'G-001', 'remote inspection failed again', 'VERIFYING')",
                    (self.repo,),
                )
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['published_review_resume'], review_marker)
        self.assertNotIn('blocked_resume', saved.snapshot)


    def test_published_review_release_seals_owner_without_rebase_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            branch, oid = 'loop-manager/G-001', 'a' * 40
            snapshot = loop_manager.GitSnapshot(
                head=oid, branch=branch, index={}, dirty={}, refs={
                    f'refs/heads/{branch}': (oid, ''),
                    f'refs/remotes/origin/{branch}': (oid, ''),
                }, worktrees={}, config={}, operations={},
            )
            self.plan('G-001', 'IMPLEMENTING', branch=branch)
            with self.connection:
                self.connection.execute(
                    "UPDATE exec_plans SET worktree = ?, publication_remote_oid = NULL, "
                    "review_result = 'VERDICT: FAIL', agent_result_snapshot = ? "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (str(worktree), snapshot.serialize(), self.repo),
                )
                scoped_blocking.record_plan_block(
                    self.connection, self.repo, 'G-001',
                    BlockReasonCode.REVIEW_RETRY_EXHAUSTED, 'review failure', 'old',
                )
                self.connection.execute(
                    "UPDATE blocked_checkpoints SET request_identity = ?, "
                    "phase = 'published', "
                    "result = 'published', branch = ?, worktree = ?, "
                    "reason_code = 'review_retry_exhausted', published_oid = ?, "
                    "accepted_remote_oid = ? WHERE repo = ? AND plan_id = 'G-001'",
                    (blocked_checkpoint_state.request_identity(
                        self.repo, 'G-001', 'review_retry_exhausted',
                        'review failure', branch, str(worktree), 'old',
                    ), branch, str(worktree), oid, oid, self.repo),
                )
            plan = self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (self.repo,),
            ).fetchone()
            with (
                mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
                mock.patch.object(loop_manager, 'validate_managed_worktree'),
                mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch', return_value=False),
                mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
                mock.patch.object(loop_manager, 'remote_branch_oid', return_value=oid),
                mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
                mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                                  return_value=oid) as owner,
                mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(
                    stdout=f'{oid}\trefs/heads/{branch}\n', returncode=0,
                )),
            ):
                self.connection.execute(
                    "UPDATE blocked_checkpoints SET request_identity = 'wrong' "
                    "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
                )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                self.connection.execute(
                    "UPDATE blocked_checkpoints SET request_identity = ? "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (blocked_checkpoint_state.request_identity(
                        self.repo, 'G-001', 'review_retry_exhausted',
                        'review failure', branch, str(worktree), 'old',
                    ), self.repo),
                )
                owner.return_value = None
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                owner.return_value = oid
                self.assertTrue(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
            saved = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).get(self.repo, 'G-001')
            self.assertEqual(saved.snapshot['published_review_resume']['owner_oid'], oid)
            self.assertNotIn('blocked_resume', saved.snapshot)
            self.assertEqual(self.connection.execute(
                "SELECT publication_remote_oid FROM exec_plans WHERE repo = ? "
                "AND plan_id = 'G-001'", (self.repo,),
            ).fetchone()[0], oid)
            self.assertEqual(self.connection.execute(
                "SELECT publication_remote_oid FROM projects WHERE repo = ?",
                (self.repo,),
            ).fetchone()[0], oid)


    def test_project_ownership_block_recovers_unrebased_review_owner(self):
        branch, oid, worktree = 'loop-manager/G-001', 'a' * 40, Path('/managed')
        plan = self.plan('G-001', 'IMPLEMENTING', branch=branch)
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, publication_remote_oid = ? "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(worktree), oid, self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = ?, worktree = ? WHERE repo = ?",
                (plan['path'], branch, str(worktree), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-001', 'uncertain', 'WORKING')", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO blocked_checkpoints "
                "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                "branch, worktree, accepted_remote_oid) "
                "VALUES (?, 'G-001', 'request', 'published', "
                "'review_retry_exhausted', 'review', ?, ?, ?)",
                (self.repo, branch, str(worktree), oid),
            )
            execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).record(
                self.repo, 'G-001', expected_generation=0, operation=None,
                expected_head=oid, target_base_oid=None,
                observed_remote_oid=oid,
                snapshot={'reactivated_from_head': oid},
            )
        current = SimpleNamespace(
            head=oid, branch=branch, operations={}, dirty={}, index={},
            shared_metadata_complete=True, refs={
                f'refs/heads/{branch}': (oid, ''),
                f'refs/remotes/origin/{branch}': (oid, ''),
            },
        )
        with (
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value=oid) as remote,
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value=oid),
        ):
            remote.return_value = 'external'
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            remote.return_value = oid
            current.refs[f'refs/heads/{branch}'] = ('other-local', '')
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            current.refs[f'refs/heads/{branch}'] = (oid, '')
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            first = execution_checkpoint_state.ExecutionCheckpointRepository(
                self.connection,
            ).get(self.repo, 'G-001')
            with self.connection:
                self.connection.execute(
                    "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?",
                    (self.repo,),
                )
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                    "VALUES (?, 'project', 'publication_ownership_uncertain', "
                    "'G-001', 'uncertain again', 'WORKING')", (self.repo,),
                )
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(self.repo, 'G-001')
        self.assertEqual(saved.generation, first.generation)
        self.assertNotIn('blocked_resume', saved.snapshot)
        self.assertEqual(saved.snapshot['published_review_resume'], {
            'request_identity': 'request', 'owner_oid': oid, 'head': oid,
        })


    def test_published_review_checkpoint_reconciles_before_legacy_count_release(self):
        self.plan('G-001', 'IMPLEMENTING', branch='loop-manager/G-001')
        loop_manager.runtime_state.RuntimeStateRepository(self.connection).block_plan(
            self.repo, 'G-001', reason_code='review_retry_exhausted',
            evidence='prior issue', release_fingerprint='prior-input',
            clear_projection=True,
        )
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection,
        )
        checkpoint.finish_published(checkpoint.get(self.repo, 'G-001'), 'published-oid')

        with (
            mock.patch.object(loop_manager, '_reconcile_published_blocked_checkpoint',
                              return_value='blocked') as reconcile,
            mock.patch.object(loop_manager, '_resume_legacy_review_count_block',
                              return_value=True) as release,
        ):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
            ))
            reconcile.assert_called_once()
            release.assert_not_called()
            reconcile.return_value = 'unchanged'
            self.assertTrue(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
            ))
            release.assert_called_once()


    def test_legacy_review_count_block_releases_only_owned_non_rebase_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            snapshot = loop_manager.GitSnapshot(
                head='owned-head', branch='loop-manager/G-001',
                index={}, dirty={}, refs={}, worktrees={}, config={}, operations={},
            )
            self.plan('G-001', 'IMPLEMENTING', branch='loop-manager/G-001')
            with self.connection:
                self.connection.execute(
                    "UPDATE exec_plans SET worktree = ?, worker_session_id = 'worker-session', "
                    "reviewer_session_id = 'review-session', review_result = 'VERDICT: FAIL', "
                    "agent_result_snapshot = ?, "
                    "review_failure_history = '{\"old\":{\"occurrences\":4}}' "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (str(worktree), snapshot.serialize(), self.repo),
                )
                scoped_blocking.record_plan_block(
                    self.connection, self.repo, 'G-001',
                    BlockReasonCode.REVIEW_RETRY_EXHAUSTED, 'old count limit', 'old',
                )
                checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
                    self.connection,
                )
                checkpoint.finish_unpublished(
                    checkpoint.get(self.repo, 'G-001'), 'local only', retryable=False,
                )
            plan = self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (self.repo,),
            ).fetchone()
            loop_manager.hydrate_execplan_project(self.connection, self.repo, plan)
            with self.connection:
                self.connection.execute(
                    "INSERT INTO project_blocks "
                    "(repo, scope, reason_code, plan_id, evidence) "
                    "VALUES (?, 'project', 'repository_state_unsafe', 'G-001', 'unsafe')",
                    (self.repo,),
                )
            with (
                mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
                mock.patch.object(loop_manager, 'validate_managed_worktree'),
                mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch', return_value=True) as rebase,
                mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot) as captured,
                mock.patch.object(loop_manager, 'remote_branch_oid', return_value=None),
                mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(
                    stdout='', returncode=0,
                )) as git_effect,
                mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            ):
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                with self.connection:
                    self.connection.execute(
                        "DELETE FROM project_blocks WHERE repo = ?", (self.repo,),
                    )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                rebase.return_value = False
                captured.return_value = loop_manager.GitSnapshot(
                    **{**snapshot.__dict__, 'dirty': {'new.py': (('unstaged',), 'changed')}}
                )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                captured.return_value = snapshot
                git_effect.return_value = SimpleNamespace(
                    stdout='other-head\trefs/heads/loop-manager/G-001\n',
                    returncode=0,
                )
                self.assertFalse(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
                git_effect.return_value = SimpleNamespace(stdout='', returncode=0)
                self.assertTrue(loop_manager._resume_legacy_review_count_block(
                    self.connection, Path(self.repo), Path('/state'), self.repo, plan,
                ))
            saved = self.connection.execute(
                "SELECT status, worker_session_id, reviewer_session_id, "
                "review_result, review_failure_history FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            ).fetchone()
            self.assertEqual(tuple(saved), (
                'IMPLEMENTING', 'worker-session', 'review-session',
                'VERDICT: FAIL', '{"old":{"occurrences":4}}',
            ))
            self.assertIsNone(scoped_blocking.plan_block(
                self.connection, self.repo, 'G-001',
            ))
            self.assertIsNotNone(self.connection.execute(
                "SELECT final_evidence FROM recovery_records "
                "WHERE repo = ? AND subject = 'G-001'", (self.repo,),
            ).fetchone()['final_evidence'])
            self.assertEqual(self.connection.execute(
                "SELECT phase FROM blocked_checkpoints "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            ).fetchone()['phase'], 'unpublished')


    def test_planning_attempt_keeps_slot_across_multiple_plan_blocks_and_pr_close(self):
        self.plan('G-001', 'PLAN_BLOCKED')
        self.plan('G-002', 'PLAN_BLOCKED')
        self.plan('G-003', 'PR_OPEN', branch='loop/G-003',
                  pr_url='https://example.test/pr/3')
        with self.connection:
            self.connection.execute(loop_manager.GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
            self.connection.execute(
                "INSERT INTO general_planning_publications "
                "(repo, stage, base_sha, commit_sha, snapshot_token, "
                "specification_fingerprint, planning_fingerprint, manifest, "
                "manifest_hash) VALUES (?, 'prepared', 'base', 'commit', "
                "'snapshot', 'spec', 'input', '{}', 'hash')", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO maintenance_cycles "
                "(repo, phase, cycle_base_sha, publication_stage, revision) "
                "VALUES (?, 'assessment', 'base', 'prepared', 3)", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PLANNING', active_sha = 'base', "
                "planning_attempt_fingerprint = 'attempt', "
                "planning_workspace_owned = 1, worktree = '/planning', "
                "planner_session_id = 'planner', planning_retry_count = 2, "
                "agent_role = 'planner', agent_snapshot = 'snapshot', "
                "planning_result_pending_validation = 1, "
                "planning_result = 'saved' WHERE repo = ?", (self.repo,),
            )
        before = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone())
        publication_before = dict(self.connection.execute(
            "SELECT * FROM general_planning_publications WHERE repo = ?",
            (self.repo,),
        ).fetchone())
        cycle_before = dict(self.connection.execute(
            "SELECT * FROM maintenance_cycles WHERE repo = ?", (self.repo,),
        ).fetchone())
        with mock.patch.object(loop_manager, 'hydrate_execplan_project') as hydrate:
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
            ))
        hydrate.assert_not_called()
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={
            'state': 'CLOSED', 'url': 'https://example.test/pr/3',
            'headRefName': 'loop/G-003', 'headRefOid': 'head',
        }):
            loop_manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM general_planning_publications WHERE repo = ?",
            (self.repo,),
        ).fetchone()), publication_before)
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM maintenance_cycles WHERE repo = ?", (self.repo,),
        ).fetchone()), cycle_before)
        statuses = dict(self.connection.execute(
            "SELECT plan_id, status FROM exec_plans WHERE repo = ?", (self.repo,),
        ).fetchall())
        self.assertEqual(statuses, {
            'G-001': 'PLAN_BLOCKED', 'G-002': 'PLAN_BLOCKED',
            'G-003': 'PLAN_BLOCKED',
        })
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={
            'state': 'OPEN', 'url': 'https://example.test/pr/3',
            'headRefName': 'loop/G-003', 'headRefOid': 'head',
        }):
            loop_manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        ).fetchone()[0], 'PR_OPEN')


    def test_plan_project_block_releases_only_after_validation_without_clearing_planning(self):
        plan = self.plan('G-001', 'PLAN_BLOCKED', branch='loop/G-001')
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = '/managed/G-001' "
                "WHERE repo = ? AND plan_id = 'G-001'", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', "
                "planning_error = 'planner failed', "
                "planning_attempt_fingerprint = 'attempt', "
                "worktree = '/planning', agent_snapshot = 'snapshot' "
                "WHERE repo = ?", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, "
                "evidence, previous_phase) VALUES (?, 'project', "
                "'workspace_ownership_uncertain', 'G-001', 'blocked', 'PLANNING')",
                (self.repo,),
            )
        before = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone())
        with mock.patch.object(
            loop_manager, 'validate_execplan_workspace_identity',
            side_effect=loop_manager.WorkspaceOwnershipError('not owned'),
        ):
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'), \
                mock.patch.object(loop_manager, 'validate_managed_worktree'), \
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch',
                                  return_value=False):
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        after = self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()
        self.assertEqual(after['phase'], 'PLANNING')
        self.assertEqual(after['planning_error'], 'planner failed')
        self.assertEqual(after['last_error'], 'planner failed')
        for field in ('planning_attempt_fingerprint', 'worktree', 'agent_snapshot'):
            self.assertEqual(after[field], before[field])
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(plan['plan_id'], 'G-001')


    def test_merged_pr_records_plan_and_defers_cleanup_during_planning(self):
        self.plan('G-003', 'PR_OPEN', branch='loop/G-003',
                  pr_url='https://example.test/pr/3')
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ? "
                "WHERE repo = ? AND plan_id = 'G-003'", (str(Path('/managed/G-003').resolve()), self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PLANNING', worktree = '/planning', "
                "planning_attempt_fingerprint = 'attempt', "
                "planning_result_completed = 1, "
                "agent_snapshot = 'planner-snapshot' WHERE repo = ?", (self.repo,),
            )
        before = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone())
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={
            'state': 'MERGED', 'url': 'https://example.test/pr/3',
            'headRefName': 'loop/G-003', 'headRefOid': 'head',
        }), mock.patch.object(loop_manager, 'remove_cleanup_worktree') as cleanup, \
                mock.patch.object(loop_manager, '_recorded_base_fetch'):
            observed = loop_manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertTrue(observed.merged)
        cleanup.assert_not_called()
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()), before)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        ).fetchone()[0], 'MERGED')
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM cleanup_pending WHERE repo = ? AND worktree = ?",
            (self.repo, str(Path('/managed/G-003').resolve())),
        ).fetchone())


    def test_missing_merged_worktree_uses_managed_cleanup_evidence_without_planning(self):
        self.plan('G-003', 'PR_OPEN', branch='loop/G-003')
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / 'missing'
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ? "
                "WHERE repo = ? AND plan_id = 'G-003'",
                (str(worktree), self.repo),
            )
            plan = self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
                (self.repo,),
            ).fetchone()
            with mock.patch.object(
                loop_manager, '_assess_missing_managed_cleanup',
                return_value=loop_manager.CleanupAssessment('missing'),
            ) as assess:
                result = loop_manager.complete_merged_execplan_worktree(
                    self.connection, Path(self.repo), self.repo,
                    Path('/state'), plan, 'https://example.test/pr/3', 'head',
                )
        assess.assert_called_once_with(Path('/state'), Path(self.repo), worktree)
        self.assertEqual(result.status, 'missing')
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
            (self.repo,),
        ).fetchone()[0], 'MERGED')


    def test_conflicting_pr_waits_for_planning_slot(self):
        plan = self.plan('G-003', 'PR_OPEN', branch='loop/G-003')
        self.connection.execute(
            "UPDATE projects SET phase = 'PLANNING', "
            "planning_attempt_fingerprint = 'attempt' WHERE repo = ?",
            (self.repo,),
        )
        self.connection.commit()
        observation = loop_manager.WaitingPullRequestObservation(
            ((plan, {'state': 'OPEN', 'mergeable': 'CONFLICTING'}),), False,
        )
        with mock.patch.object(loop_manager, 'hydrate_execplan_project') as hydrate:
            result = loop_manager.select_waiting_pull_request_conflict(
                self.connection, Path(self.repo), Path('/state'),
                self.repo, observation,
            )
        hydrate.assert_not_called()
        self.assertFalse(result.conflict_started)
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (self.repo,),
        ).fetchone()[0], 'PLANNING')


    def test_releasing_notified_plan_block_rolls_back_notice_cancellation(self):
        self.plan("G-001", "PLAN_BLOCKED")
        notifications.configure_session(self.connection, enabled=True)
        with self.connection:
            self.connection.execute(
                "INSERT INTO execplan_blocks "
                "(repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-001', 'plan', 'user_decision_required', 'decision')",
                (self.repo,),
            )
            notifications.enqueue_current_project_events(self.connection, self.repo)
        event = self.connection.execute(
            "SELECT event_key FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchone()
        self.assertIsNotNone(event)

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.connection:
                scoped_blocking.clear_plan_block(self.connection, self.repo, "G-001")
                raise RuntimeError("rollback")

        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM execplan_blocks WHERE repo = ? AND plan_id = 'G-001'",
            (self.repo,),
        ).fetchone())
        self.assertIsNone(self.connection.execute(
            "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
            (event[0],),
        ).fetchone()[0])
        self.assertIsNone(self.connection.execute(
            "SELECT released_at FROM notified_block_incidents WHERE event_key = ?",
            (event[0],),
        ).fetchone()[0])


    def test_requeued_plan_keeps_only_verified_checkpoint_remote_oid(self):
        self.plan('G-081', 'PLAN_BLOCKED', branch='loop/G-081')
        loop_manager._requeue_blocked_execplan(
            self.connection, self.repo, 'G-081',
            publication_remote_oid='a' * 40,
        )
        row = self.connection.execute(
            "SELECT status, publication_remote_oid FROM exec_plans WHERE repo = ? AND plan_id = 'G-081'",
            (self.repo,),
        ).fetchone()
        self.assertEqual(row['status'], 'READY')
        self.assertEqual(row['publication_remote_oid'], 'a' * 40)


    def test_project_publication_block_recovers_only_matching_owned_rebase(self):
        plan = self.plan('G-081', 'IMPLEMENTING', branch='loop/G-081')
        marker = {'owner_oid': 'a' * 40, 'before_head': 'before',
                  'after_head': 'after', 'target_base_oid': 'base'}
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        checkpoint.record(
            self.repo, 'G-081', expected_generation=0, operation=None,
            expected_head='after', target_base_oid='base',
            observed_remote_oid='a' * 40,
            snapshot={'blocked_resume': marker},
        )
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET worktree = ?, worker_session_id = 'session' "
                "WHERE repo = ? AND plan_id = 'G-081'", (str(Path('/managed').resolve()), self.repo),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = 'loop/G-081', worktree = ? WHERE repo = ?",
                (plan['path'], str(Path('/managed').resolve()), self.repo),
            )
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', 'G-081', 'old error', 'WORKING')",
                (self.repo,),
            )
        current = SimpleNamespace(
            head='after', branch='loop/G-081', operations={}, dirty={},
        )
        with (
            mock.patch.object(loop_manager, '_direct_rebase_history_proven', return_value=True),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value='a' * 40),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value='a' * 40) as owner,
            mock.patch.object(loop_manager.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
        ):
            owner.return_value = None
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
            self.assertIsNotNone(scoped_blocking.project_block(
                self.connection, self.repo,
            ))
            owner.reset_mock()
            owner.return_value = 'a' * 40
            recovered = loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            )
        self.assertTrue(recovered)
        self.assertEqual(owner.call_count, 2)  # 최초 검사와 차단 해제 transaction의 재검증.
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        saved = self.connection.execute(
            "SELECT status, publication_remote_oid, worker_session_id FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-081'", (self.repo,),
        ).fetchone()
        self.assertEqual(tuple(saved), ('IMPLEMENTING', 'a' * 40, 'session'))

        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
                "blocked_reason = 'review_retry_exhausted' "
                "WHERE repo = ? AND plan_id = 'G-081'", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = NULL, "
                "branch = NULL, worktree = NULL WHERE repo = ?", (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO execplan_blocks "
                "(repo, plan_id, scope, reason_code, evidence) VALUES "
                "(?, 'G-081', 'plan', 'review_retry_exhausted', 'separate block')",
                (self.repo,),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-081', 'old error', 'PLAN_BLOCKED')", (self.repo,),
            )
        with (
            mock.patch.object(loop_manager, '_direct_rebase_history_proven', return_value=True),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current),
            mock.patch.object(loop_manager, 'remote_branch_oid', return_value='a' * 40),
            mock.patch.object(loop_manager, 'pr_for_branch', return_value=None),
            mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                              return_value='a' * 40),
            mock.patch.object(loop_manager.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
        ):
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, Path(self.repo), Path('/state'), self.repo,
            ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-081'",
            (self.repo,),
        ).fetchone()[0], 'PLAN_BLOCKED')
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM execplan_blocks WHERE repo = ? AND plan_id = 'G-081'",
            (self.repo,),
        ).fetchone())
