import json
import sqlite3
import tempfile
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
import blocked_checkpoint_state
import execution_checkpoint_state
import maintenance_state
import notifications
import project_registry
import scoped_blocking
from scoped_blocking import BlockReasonCode, BlockScope


class ScopedBlockingTest(unittest.TestCase):
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
                          'commit_message': 'Worker correction'},
            )
        current = SimpleNamespace(
            head=corrected, branch=branch, operations={}, dirty={}, index={},
            shared_metadata_complete=True, refs={
                f'refs/heads/{branch}': (corrected, ''),
                f'refs/remotes/origin/{branch}': (published, ''),
            },
        )
        commit = {'parent': published}

        def git(_repo, *args, **_kwargs):
            if args[:3] == ('show', '-s', '--format=%P'):
                return SimpleNamespace(stdout=commit['parent'] + '\n')
            if args[:3] == ('show', '-s', '--format=%B'):
                return SimpleNamespace(stdout='Worker correction\n')
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
                "UPDATE exec_plans SET worktree = '/managed/G-003' "
                "WHERE repo = ? AND plan_id = 'G-003'", (self.repo,),
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
            "SELECT 1 FROM cleanup_pending WHERE repo = ? AND worktree = '/managed/G-003'",
            (self.repo,),
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
                "UPDATE exec_plans SET worktree = '/managed', worker_session_id = 'session' "
                "WHERE repo = ? AND plan_id = 'G-081'", (self.repo,),
            )
            self.connection.execute(
                "UPDATE projects SET phase = 'PROJECT_BLOCKED', plan_path = ?, "
                "branch = 'loop/G-081', worktree = '/managed' WHERE repo = ?",
                (plan['path'], self.repo),
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
        owner.assert_called_once()
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

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)
        notifications.ensure_schema(self.connection)
        self.repo = "/projects/scoped"
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

    def test_initialized_worker_queries_do_not_reenter_schema_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            parent = loop_manager.connect_database(state, create=True)
            self.addCleanup(parent.close)
            parent.execute("BEGIN IMMEDIATE")

            def query_worker(repo):
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
                list(pool.map(query_worker, ("repo-a", "repo-b")))
            parent.rollback()

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
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
            BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
            BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
            BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
            BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
            BlockReasonCode.USER_DECISION_REQUIRED,
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

    def test_waiting_rows_do_not_occupy_the_single_mutation_slot(self):
        self.plan("G-001", "PR_OPEN", branch="loop-manager/G-001")
        self.plan("G-002", "PR_OPEN", branch="loop-manager/G-002")
        self.plan("G-003", "IMPLEMENTING")
        with self.assertRaises(sqlite3.IntegrityError):
            self.plan("G-004", "VERIFYING")

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

    def test_agent_scope_ignores_codex_turn_refs_but_structures_real_boundaries(self):
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
        with self.assertRaises(loop_manager.AgentScopeError) as raised:
            loop_manager.validate_agent_scope(
                "reviewer", Path(self.repo), baseline, shared_ref
            )
        self.assertEqual(
            raised.exception.reason_code,
            BlockReasonCode.SHARED_REF_MUTATION,
        )
        self.assertIs(
            scoped_blocking.reason_scope(raised.exception.reason_code),
            BlockScope.PROJECT,
        )

        local_change = loop_manager.GitSnapshot(
            **{
                **baseline.__dict__,
                "dirty": {"module.py": (("unstaged",), "changed")},
            }
        )
        with self.assertRaises(loop_manager.AgentScopeError) as raised:
            loop_manager.validate_agent_scope(
                "reviewer", Path(self.repo), baseline, local_change
            )
        self.assertEqual(
            raised.exception.reason_code,
            BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
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
        self.plan("G-001", "IMPLEMENTING")
        with self.connection:
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "first evidence",
                "one",
            )
            checkpoint_repository = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    self.connection
                )
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(self.repo, "G-001"),
                "first local-only result",
                retryable=False,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
            scoped_blocking.record_plan_block(
                self.connection,
                self.repo,
                "G-001",
                BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
                "updated evidence",
                "two",
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(self.repo, "G-001"),
                "updated local-only result",
                retryable=False,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
        rows = self.connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'blocked'"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(
            (payload["scope"], payload["reason_code"], payload["execplan_id"]),
            ("plan", "review_retry_exhausted", "G-001"),
        )

        with self.connection:
            scoped_blocking.record_project_block(
                self.connection,
                self.repo,
                BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
                "shared config changed",
                "config-fingerprint",
                plan_id="G-001",
                previous_phase=None,
            )
            notifications.enqueue_current_project_events(
                self.connection, self.repo
            )
        project_payload = next(
            parsed
            for parsed in (
                json.loads(row[0])
                for row in self.connection.execute(
                    "SELECT payload FROM notification_outbox "
                    "WHERE event_type = 'blocked'"
                )
            )
            if parsed["scope"] == "project"
        )
        self.assertEqual(
            (
                project_payload["scope"],
                project_payload["reason_code"],
                project_payload["execplan_id"],
                project_payload["execplan_title"],
            ),
            ("project", "shared_git_config_mutation", "G-001", "Work G-001"),
        )
        self.assertEqual(
            project_payload["paths"],
            [],
        )

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
            loop_manager._BlockedCheckpointAdapter,
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


if __name__ == "__main__":
    unittest.main()
