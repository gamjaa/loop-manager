"""Revision Gate execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.execution import plan_validation
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot, ExecPlanBodyHash
from dataclasses import replace
from loop_manager_internal.git.command_adapter import ExternalObservationError
import loop_manager as lm
from loop_manager_internal.catalog.requirement_archive import ArchiveSnapshot
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionPhaseCoordinator, ExecutionOutcome, ExecutionOutcomeKind
from tests.support.worker import decision as worker_decision
from tests.support.execution import READY_WORKER_RESULT


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionRevisionGateTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_exact_legacy_worker_token_retains_implementation_after_source_only_change(self):
        self.handler.guard(self.context())
        previous = self.handler._definition_snapshot
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        checkpoints = ExecutionCheckpointRepository(self.connection)
        self.handler.effects = replace(self.effects, checkpoint=checkpoints)
        self.effects.revision.source_fingerprint.return_value = 'changed-source'
        candidate = 'base'
        self.state.update_project(self.repo, phase='VERIFYING', worker_result=READY_WORKER_RESULT,
            definition_snapshot=previous.serialize(), worker_input_token=previous.legacy_worker_token,
            archive_token=previous.archive_token)
        checkpoints.record(self.repo, 'G-047', expected_generation=checkpoints.get(self.repo, 'G-047').generation,
            operation=None, expected_head='base', target_base_oid='base', observed_remote_oid=None,
            snapshot={'current_rebase_acceptance': {'head': 'base'},
                      'worker_candidate_token': previous.legacy_worker_candidate_token(candidate)})
        result = self.handler.guard(self.context())
        self.assertNotIsInstance(result, ExecutionOutcome)
        self.assertEqual(result.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)
        self.assertEqual(self.state.row()['worker_input_token'], previous.worker_token)
        self.effects.agent.run_agent.assert_not_called()

    def test_conflicted_rebase_uses_original_legacy_body_for_record_comparison(self):
        self.handler.guard(self.context())
        text = '---\nid: G-047\n---\n## Purpose\nImplement\n## Progress\n- [ ] Run unit test\n'
        original = ExecPlanBodyHash(text)
        previous = replace(self.handler._definition_snapshot, body_hash=str(original))
        self.state.update_project('repo', phase='WORKING', definition_snapshot=previous.serialize(),
            worker_session_id='saved-session', worker_result=READY_WORKER_RESULT,
            worker_input_token=previous.worker_token,
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict')
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.remote = 'new-base'
        current = ExecPlanBodyHash(text.replace('[ ]', '[x]'))
        self.effects.revision.body_hash.side_effect = lambda _repo, revision, _path: (
            original if revision == 'base' else current)
        result = self.handler.guard(self.context())
        self.assertNotIsInstance(result, ExecutionOutcome)
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)
        self.assertEqual(self.state.row()['definition_snapshot'], previous.serialize())
        self.assertNotIn('rebase', self.calls)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_conflicted_rebase_without_original_body_invalidates_success_for_worker(self):
        self.handler.guard(self.context())
        text = '---\nid: G-047\n---\n## Progress\n- [ ] Run unit test\n'
        previous = replace(self.handler._definition_snapshot, body_hash=str(ExecPlanBodyHash(text)))
        self.state.update_project('repo', phase='WORKING', definition_snapshot=previous.serialize(),
            worker_session_id='saved-session', last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict')
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.remote = 'new-base'
        self.effects.revision.body_hash.side_effect = lambda _repo, revision, _path: (
            'unavailable-original' if revision == 'base' else ExecPlanBodyHash(text.replace('[ ]', '[x]')))
        result = self.handler.guard(self.context())
        self.assertNotIsInstance(result, ExecutionOutcome)
        self.assertTrue(result.resolving_rebase)
        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertIsNone(self.state.row()['worker_input_token'])
        self.assertEqual(self.state.row()['definition_snapshot'], previous.serialize())
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertNotIn('rebase', self.calls)

    def test_conflicted_rebase_changed_inputs_clear_success_without_losing_recovery(self):
        self.handler.guard(self.context())
        previous = self.handler._definition_snapshot
        for field in ('guidance_hash', 'archive_candidates', None):
            with self.subTest(field=field):
                applied = (replace(previous, **{field: ('docs/requirements/old.md',)
                           if field == 'archive_candidates' else 'old-input'}) if field else None)
                self.state.update_project('repo', phase='REVIEWING',
                    definition_snapshot=applied.serialize() if applied else None,
                    worker_session_id='saved-session', worker_result=READY_WORKER_RESULT,
                    verify_result='old verify', review_result='VERDICT: PASS',
                    worker_input_token='old-worker', archive_token='old-archive',
                    verify_token='old-verify', review_token='old-review', publication_token='old-publication',
                    last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict')
                self.effects.workspace.is_directory.return_value = True
                self.effects.workspace._rebase_in_progress_for_branch.return_value = True
                self.effects.revision.changed_paths.return_value = (previous.path,)
                result = self.handler.guard(replace(self.context(), resolving_rebase=True))
                self.assertNotIsInstance(result, ExecutionOutcome)
                self.assertTrue(result.resolving_rebase)
                self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
                row = self.state.row()
                self.assertEqual(row['worker_session_id'], 'saved-session')
                self.assertEqual(row['definition_snapshot'], applied.serialize() if applied else None)
                self.assertTrue(row['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
                for name in ('worker_result', 'verify_result', 'review_result', 'worker_input_token',
                             'archive_token', 'verify_token', 'review_token', 'publication_token'):
                    self.assertIsNone(row[name], name)
                self.effects.blocking.block_active_execplan.assert_not_called()
                self.assertNotIn('rebase', self.calls)

    def test_revision_document_observation_failure_preserves_worker_session(self):
        self.state.update_project('repo', worker_session_id='saved-session')
        def git(_repo, operation, *args, **kwargs):
            if operation == 'show':
                raise RuntimeError('arbitrary Git failure')
            return SimpleNamespace(returncode=0, stdout=f'100644 blob abc\t{args[-1]}\0')

        self.effects.revision.body_hash.side_effect = lambda *args: (
            plan_validation._revision_execplan_body_hash(
                plan_validation.RevisionExecplanBodyHashEffects(git=git), *args
            )
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.recovery.observation.reason, handlers.RecoveryReason.STATUS_FAILED)
        self.assertIn(result.kind, (ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.TRANSIENT_PAUSE))
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.effects.validation.validate_active_execplan_metadata.assert_not_called()

    def test_worktree_document_observation_failure_preserves_worker_session(self):
        self.state.update_project('repo', worker_session_id='saved-session')
        self.effects.validation.validate_active_execplan_metadata.side_effect = (
            ExternalObservationError('Git read failed')
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.recovery.observation.reason, handlers.RecoveryReason.STATUS_FAILED)
        self.assertIn(result.kind, (ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.TRANSIENT_PAUSE))
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_saved_decision_blocks_revision_refresh_before_checkpoint_commit(self):
        self._decision_workspace()
        message = worker_decision()
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result=message,
            agent_role=None, agent_snapshot=None, agent_input_fingerprint=None,
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler._refresh_revision_base(
                self.context(), None, None, 'new-base',
            )
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.assertNotIn('add', self.calls)
        self.assertNotIn('commit', self.calls)


    def test_revision_guard_tokens_follow_complete_phase_lifecycle(self):
        context = self.handler.guard(self.context())
        self.assertNotIsInstance(context, handlers.ExecutionOutcome)

        for method in ('run', 'verify', 'review', 'publish'):
            call = getattr(self.handler, method)
            args = (context, self.handler) if method in {'verify', 'publish'} else (context,)
            result = call(*args)
            context = result.context

        completed = self.repository.get('repo', 'G-047')
        self.assertEqual(completed.status, 'PR_OPEN')
        self.assertIsNotNone(completed.definition_snapshot)
        self.assertIsNotNone(completed.worker_input_token)
        self.assertIsNotNone(completed.archive_token)
        self.assertIsNotNone(completed.verify_token)
        self.assertIsNotNone(completed.review_token)
        self.assertIsNotNone(completed.publication_token)
        snapshot = DefinitionSnapshot.deserialize(completed.definition_snapshot)
        assert snapshot is not None
        self.assertEqual(completed.verify_token, snapshot.verify_token(
            'base', 'base', str(completed.verify_result),
        ))
        self.assertEqual(completed.review_token, snapshot.review_token('base', 'base'))


    def test_global_specification_change_does_not_interrupt_task_revision(self):
        from loop_manager_internal.application import task_scheduling
        planning_check = mock.Mock(return_value=True)
        self.handler.effects = replace(self.effects, revision=SimpleNamespace(
            **{**vars(self.effects.revision), 'planning_required': planning_check}))
        scheduler = mock.Mock()
        token = task_scheduling.current_scheduler.set(scheduler)
        try:
            self.remote = 'new-base'
            self.handler._refresh_revision_base = mock.Mock(
                return_value=ExecutionOutcome(ExecutionOutcomeKind.RETRY))
            outcome = self.handler.guard(self.context())
            self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
            self.effects.revision.reconcile_catalog.assert_called()
            self.handler._refresh_revision_base.assert_called_once()
            planning_check.assert_not_called()
            scheduler.pause_project.assert_not_called()
        finally:
            task_scheduling.current_scheduler.reset(token)

    def test_source_revision_without_execplan_change_keeps_worker_and_rechecks_verification(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        snapshot = DefinitionSnapshot.deserialize(
            self.state.row()['definition_snapshot']
        )
        assert snapshot is not None
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_session_id='worker-session',
            reviewer_session_id='reviewer-session',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            worker_input_token=snapshot.worker_token,
            archive_token=snapshot.archive_token,
            verify_token=snapshot.verify_token('base', 'base', 'verify-result'),
            review_token=snapshot.review_token('base', 'base'),
            publication_token=snapshot.publication_token('base', 'base'),
        )
        self.effects.revision.source_fingerprint.return_value = 'changed-source'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'VERIFYING')
        self.assertEqual(row['worker_result'], 'worker-result')
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['reviewer_session_id'], 'reviewer-session')
        self.assertEqual(row['branch'], 'loop-manager/G-047')
        self.assertEqual(row['worktree'], str(Path('/state/G-047')))


    def test_conflict_revision_dependency_merge_resumes_worker(self):
        workspace_plan = self._conflict_revision_setup()
        applied = self.state.row()['definition_snapshot']
        for plan_id, dependencies in (('G-048', '["G-050"]'), ('G-050', '[]')):
            self.connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "('repo', ?, ?, 'Dependency', '[\"docs/PRODUCT.md\"]', ?, "
                "'normal', 'hash', 'READY')",
                (plan_id, f'docs/plans/[{plan_id}] Dependency.md', dependencies),
            )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on = '[\"G-048\"]' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        self.remote = 'new-base'
        self.effects.revision.unmet_dependencies.side_effect = (
            lambda repo, plan_id: lm.unmet_execplan_dependencies(
                self.connection, repo, plan_id
            )
        )

        outcome = ExecutionPhaseCoordinator(self.handler).dispatch(
            self.handler._context()
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.STABLE_WAIT)
        self.assertIn('G-048', outcome.detail)
        self.assertIn('G-050', outcome.detail)
        self.effects.agent.run_agent.assert_not_called()
        self.assertNotIn('rebase', self.calls)
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'previous worker result')
        self.assertTrue(row['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = 'repo' AND plan_id IN ('G-048', 'G-050')"
        )
        self.connection.commit()
        resumed = self._dispatch_conflict_revision(workspace_plan)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertEqual(
            self.effects.agent.run_agent.call_args.kwargs['session_id'],
            'worker-session',
        )
        self.assertIsNone(self.effects.agent.worker_prompt.call_args.args[2])
        self.assertEqual(self.state.row()['definition_snapshot'], applied)
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertEqual(self.state.row()['branch'], 'loop-manager/G-047')
        self.assertEqual(self.state.row()['worktree'], str(Path('/state/G-047')))
        self.effects.validation.verify.assert_not_called()
        latest_plan = self.handler._cached_plan(self.handler.catalog_plan)
        rerun = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(rerun.context.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        verified = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING)
        reviewed = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(reviewed.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(
            [call for call in self.calls if call in {'worker', 'verify', 'reviewer'}],
            ['worker', 'worker', 'verify', 'reviewer'],
        )


    def test_conflict_revision_priority_change_allows_old_workspace_metadata(self):
        workspace_plan = self._conflict_revision_setup()
        self.connection.execute(
            "UPDATE exec_plans SET priority = 'high' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        self.remote = 'new-base'

        outcome = self._dispatch_conflict_revision(workspace_plan)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertEqual(self.handler.plan['priority'], 'high')
        self.assertIsNone(self.effects.agent.worker_prompt.call_args.args[2])
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)
        self.assertEqual(self.state.row()['worker_input_token'], self.handler._definition_snapshot.worker_token)
        self.assertIsNone(self.state.row()['verify_token'])
        self.assertIsNone(self.state.row()['review_token'])
        self.assertEqual(self.effects.agent.run_agent.call_count, 1)


    def test_conflict_revision_scope_rejects_unapproved_metadata(self):
        workspace_plan = self._conflict_revision_setup()
        baseline = DefinitionSnapshot.deserialize(self.state.row()['definition_snapshot'])
        self.connection.execute(
            "UPDATE exec_plans SET priority = 'high', depends_on = '[\"G-048\"]', "
            "current_complexity = 'medium', current_risk = 'medium' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        catalog = self.repository.active_row('repo')
        for field, value in (
            ('priority', 'critical'), ('depends_on', ['G-099']),
            ('complexity', 'high'), ('risk', 'high'),
            ('title', 'Unauthorized'), ('sources', ['docs/OTHER.md']),
        ):
            with self.subTest(field=field), mock.patch(
                'loop_manager.capture_git_snapshot', return_value=lm.GitSnapshot(
                    'owned-head', 'loop-manager/G-047', {}, {}, {}, {}, {}, {})
            ), mock.patch('loop_manager.validate_agent_scope'), mock.patch(
                'loop_manager.validate_managed_worktree'
            ), mock.patch('loop_manager.validate_execplan', return_value={
                **workspace_plan, field: value,
            }):
                with self.assertRaisesRegex(lm.ExecPlanMetadataError, 'metadata'):
                    lm.validate_worker_workspace_scope(
                        Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                        'main', catalog['path'], catalog, mock.Mock(),
                        allow_rebase=True, rebase_definition=baseline,
                    )
        with mock.patch('loop_manager.capture_git_snapshot', return_value=lm.GitSnapshot(
                'owned-head', 'loop-manager/G-047', {}, {}, {}, {}, {}, {})), mock.patch(
            'loop_manager.validate_agent_scope'
        ), mock.patch('loop_manager.validate_managed_worktree'), mock.patch(
            'loop_manager.validate_execplan', return_value=workspace_plan
        ) as parsed:
            with self.assertRaisesRegex(lm.ExecPlanMetadataError, 'metadata'):
                lm.validate_worker_workspace_scope(
                    Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                    'main', catalog['path'], catalog, mock.Mock(),
                )
            parsed.return_value = {
                **workspace_plan, 'priority': 'high', 'depends_on': ['G-048'],
                'complexity': 'medium', 'risk': 'medium',
            }
            current = lm.validate_worker_workspace_scope(
                Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                'main', catalog['path'], catalog, mock.Mock(),
                allow_rebase=True, rebase_definition=baseline,
            )
            self.assertEqual(current['priority'], 'high')
            self.assertEqual(current['depends_on'], ['G-048'])
            self.assertEqual(current['complexity'], 'medium')
            self.assertEqual(current['risk'], 'medium')
        with self.assertRaisesRegex(RuntimeError, '충돌 복구'):
            lm.validate_worker_workspace_scope(
                Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                'main', catalog['path'], catalog, mock.Mock(),
                rebase_definition=baseline,
            )


    def test_revision_guard_adopts_legacy_phase_artifacts_without_worker_replay(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )

        guarded = self.handler.guard(self.handler._context())

        self.assertEqual(guarded.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        snapshot = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        assert snapshot is not None
        self.assertEqual(row['phase'], 'VERIFYING')
        self.assertEqual(row['worker_result'], 'worker-result')
        self.assertEqual(row['worker_input_token'], snapshot.worker_token)
        self.assertEqual(row['archive_token'], snapshot.archive_token)
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertIsNone(row['verify_token'])
        self.assertIsNone(row['review_token'])
        self.assertEqual(row['phase'], 'VERIFYING')


    def test_revision_guard_retries_when_base_and_catalog_shas_disagree(self):
        self.effects.revision.reconcile_catalog.side_effect = None
        self.effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha='other-base'
        )

        outcome = self.handler.guard(self.context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['definition_snapshot'])
        self.effects.revision.body_hash.assert_not_called()
        self.effects.revision.source_fingerprint.assert_not_called()


    def test_revision_guard_rejects_legacy_artifacts_after_source_change(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )
        self.remote = 'new-base'
        self.effects.revision.changed_paths.return_value = (
            'docs/PRODUCT.md',
        )

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])


    def test_successful_revision_refresh_clears_rebase_recovery_marker(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        self.remote = 'new-base'

        outcome = self.handler.guard(guarded)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertIsNone(row['revision_resume_status'])
        self.assertIsNone(row['last_error'])


    def test_revision_refresh_checkpoints_dirty_verified_worker_patch_before_rebase(self):
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='worker-result'
        )
        self.remote = 'new-base'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertLess(self.calls.index('commit'), self.calls.index('rebase'))
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertIsNone(self.state.row()['worker_result'])


    def test_archive_change_returns_to_worker_before_push(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.archive.requirement_archive_reconciliation_error.return_value = 'archive changed'
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertNotIn('push', self.calls)


    def test_guard_ignores_stale_archive_target_without_resetting_worker_session(self):
        source = 'docs/requirements/source.md'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.handler.archive_sources = (source,)
        self.handler.archive_candidates = (source,)
        self.handler.archive_scope_sources = (source,)
        self.handler._archive_snapshot = ArchiveSnapshot(
            'base', 'old-catalog', 'old-state', 'G-047', (source,), (),
        )
        from loop_manager_internal.storage.requirement_archive_state import ensure_schema
        ensure_schema(self.connection)
        self.connection.execute(
            'INSERT INTO requirement_archive_targets '
            '(repo, plan_id, generation, base_oid, catalog_token, state_token, '
            'snapshot_token, candidates, move_set, stage, attempts) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('repo', 'G-047', 4, 'base', 'stale', 'stale', 'stale',
             '["docs/requirements/source.md"]', None, 'VALIDATED', 0),
        )
        self.state.update_project(
            'repo', worker_session_id='saved-session',
            archive_token='saved-archive-token',
        )

        result = self.handler.guard(self.context())

        self.assertNotIsInstance(result, ExecutionOutcome)
        self.assertEqual(result.archive_sources, ())
        self.assertEqual(result.archive_candidate_scope, ())
        self.assertIsNone(result.archive_snapshot_token)
        target = self.connection.execute(
            'SELECT snapshot_token, stage FROM requirement_archive_targets '
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual((target['snapshot_token'], target['stage']), ('stale', 'VALIDATED'))
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.assertEqual(self.state.row()['archive_token'], 'saved-archive-token')
        self.effects.archive.requirement_archive_sources.assert_not_called()
        self.effects.archive.requirement_archive_snapshot.assert_not_called()
        self.effects.archive.requirement_archive_touched_sources.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_guard_remote_race_preserves_worker_session_without_block(self):
        source = 'docs/requirements/source.md'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git
        fetches = 0

        def git(path, *args, **kwargs):
            nonlocal fetches
            if args[0] == 'fetch':
                fetches += 1
                if fetches == 2:
                    self.remote = 'base-2'
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.state.update_project('repo', worker_session_id='saved-session')

        result = self.handler.guard(self.context())

        self.assertNotIsInstance(result, ExecutionOutcome)
        self.assertLess(fetches, 2)
        self.effects.archive.requirement_archive_snapshot.assert_not_called()
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_guard_refreshes_base_without_archive_target(self):
        source = 'docs/requirements/source.md'
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        snapshot = ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        self.effects.archive.requirement_archive_snapshot.return_value = snapshot
        self.handler._refresh_revision_base = mock.Mock(
            return_value=ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.handler._refresh_revision_base.assert_called_once()
        target = self.connection.execute(
            'SELECT base_oid, snapshot_token, stage '
            "FROM requirement_archive_targets WHERE repo = 'repo'"
        ).fetchone()
        self.assertIsNone(target)
        self.effects.archive.requirement_archive_snapshot.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_guard_invalid_remote_catalog_uses_final_recovery_gate(self):
        path = 'docs/plans/[G-002] Invalid.md'
        self.effects.revision.reconcile_catalog.side_effect = None
        self.effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='invalid-catalog'),
            revision_sha='base',
            diagnostic=SimpleNamespace(message='invalid source', path=path,
                                       source='docs/requirements/missing.md'),
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.CATALOG_INVALID)
        self.assertEqual(result.recovery.observation.user_targets,
                         (path, 'docs/requirements/missing.md'))
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.effects.archive.requirement_archive_snapshot.assert_not_called()


    def test_guard_refuses_archive_rebase_when_head_lacks_plan_ancestry(self):
        source = 'docs/requirements/source.md'
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=1 if path == Path('/state/G-047') else 0
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.effects.archive.requirement_archive_snapshot.assert_not_called()
        self.assertIsNone(self.connection.execute(
            "SELECT snapshot_token FROM requirement_archive_targets WHERE repo = 'repo'"
        ).fetchone())


    def test_guard_archive_rebase_checks_managed_head_not_base_checkout(self):
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (
            'docs/requirements/source.md',
        )
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=0 if path == Path('/state/G-047') else 1
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.handler._refresh_revision_base = mock.Mock(
            return_value=ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.handler._refresh_revision_base.assert_called_once()
        ancestry_paths = [
            call.args[0] for call in self.effects.workspace.git.call_args_list
            if call.args[1:3] == ('merge-base', '--is-ancestor')
        ]
        self.assertEqual(ancestry_paths, [])
        self.effects.archive.requirement_archive_snapshot.assert_not_called()
        self.effects.workspace.validate_managed_worktree.assert_not_called()


    def test_guard_archive_snapshot_ignores_base_checkout_head(self):
        self.effects.archive.requirement_archive_sources.return_value = (
            'docs/requirements/source.md',
        )
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=0 if path == Path('/state/G-047') else 1
                )
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git

        result = self.handler.guard(self.context())

        self.assertNotIsInstance(result, ExecutionOutcome)
        ancestry_paths = [
            call.args[0] for call in self.effects.workspace.git.call_args_list
            if call.args[1:3] == ('merge-base', '--is-ancestor')
        ]
        self.assertEqual(ancestry_paths, [])
        self.effects.archive.requirement_archive_snapshot.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_worker_metadata_mismatch_blocks_without_repeated_correction(self):
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )

        # Declaration errors now enter the input gate on the first result.
        outcome = self.handler.run(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(outcome.recovery.occurrence, 1)
        self.assertEqual(outcome.recovery.observation.reason.value, 'plan_input_invalid')
        self.assertEqual(outcome.recovery.block_reason, lm.BlockReasonCode.PLAN_INPUT_INVALID)
        self.assertEqual(self.state.row()['worker_session_id'], 'session')
        self.effects.agent.run_agent.assert_called_once()
        self.assertNotIn('verify', self.calls)
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_rebase_metadata_block_preserves_resume_context(self):
        self.state.update_project(
            'repo', last_error=lm.REBASE_IN_PROGRESS_ERROR,
            worker_session_id='worker-session', worker_result='saved result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, READY_WORKER_RESULT, session_id='worker-session',
        )
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )

        context = self.handler._context()
        self.assertTrue(context.resolving_rebase)
        self.handler.guard(context)
        delivered = self.handler._definition_snapshot
        outcome = self.handler.run(context)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(outcome.recovery.occurrence, 1)
        self.assertEqual(outcome.recovery.observation.reason.value, 'plan_input_invalid')
        self.assertEqual(outcome.recovery.block_reason, lm.BlockReasonCode.PLAN_INPUT_INVALID)
        self.assertTrue(self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
        self.assertIn('catalog metadata mismatch', self.state.row()['last_error'])
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertEqual(self.state.row()['worker_result'], 'saved result')
        self.assertTrue(self.effects.agent.validate_worker_workspace_scope.call_args.kwargs['allow_rebase'])
        self.assertEqual(self.effects.agent.run_agent.call_args.kwargs['session_id'], 'worker-session')
        saved = self.handler.rebase_corrections.get('repo', 'G-047')
        self.assertEqual(saved.worker_result, READY_WORKER_RESULT)
        self.assertEqual(saved.input_snapshot, delivered.serialize())
        self.assertEqual(saved.worker_session, 'worker-session')
        self.assertTrue(self.handler._context().resolving_rebase)
        self.effects.agent.run_agent.assert_called_once()
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.assertNotIn('verify', self.calls)

    def test_verify_metadata_mismatch_blocks_with_saved_output(self):
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )
        self.state.update_project('repo', phase='VERIFYING', worker_result='saved worker result')
        outcome, output = self.handler._verify_once(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(outcome.recovery.observation.reason.value, 'plan_input_invalid')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['worker_result'], 'saved worker result')
        self.assertEqual(self.state.row()['verify_result'], output)
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.effects.agent.run_agent.assert_not_called()
        self.assertEqual(outcome.recovery.occurrence, 1)
        self.assertEqual(outcome.recovery.block_reason, lm.BlockReasonCode.PLAN_INPUT_INVALID)


    def test_repeated_archive_mismatch_reaches_final_gate_only_at_limit(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.return_value = ('source.md',)
        self.effects.archive.requirement_archive_move_set.return_value = ()
        self.effects.archive.requirement_archive_reconciliation_error.return_value = 'archive changed'

        first = self.handler.publish(self.context(), self.handler)
        self.assertEqual(first.recovery.occurrence, 1)
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.effects.blocking.block_active_execplan.assert_not_called()
        second = self.handler._recover(first.recovery.observation.reason, 'archive changed',
                                       fingerprint=first.recovery.observation.fingerprint)
        self.assertEqual(second.recovery.occurrence, 2)
        self.effects.blocking.block_active_execplan.assert_not_called()
        third = self.handler._recover(first.recovery.observation.reason, 'archive changed',
                                      fingerprint=first.recovery.observation.fingerprint)
        self.assertEqual(third.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()


    def test_archive_move_validation_error_returns_to_worker_without_block(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project(
            'repo', phase='PUBLISHING', validated_base_sha='base',
            worker_session_id='worker-session', verify_result='saved verify',
            review_result='saved review',
        )
        self.effects.archive.requirement_archive_candidates.return_value = ('source.md',)
        self.effects.archive.requirement_archive_move_set.side_effect = lm.RequirementArchiveError('move invalid')

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'archive_move_mismatch')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['verify_result'], 'saved verify')
        self.assertEqual(self.state.row()['review_result'], 'saved review')
        self.effects.archive.requirement_archive_candidates.assert_called_once()
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_archive_destination_collision_is_final_with_actual_path(self):
        self._use_runtime_contract()
        path = 'docs/requirements/archive/source.md'
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.side_effect = (
            lm.RequirementArchiveCollisionError(path)
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.user_targets, (path,))
        self.effects.blocking.block_active_execplan.assert_called_once()


    def test_archive_git_lock_retries_without_blocking(self):
        self._use_runtime_contract()
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.side_effect = (
            lm.GitLockError('locked')
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.GIT_LOCK)
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_publication_uses_immutable_plan_link_with_reconciled_metadata(self):
        self.connection.execute(
            "UPDATE exec_plans SET title = 'Current Runtime', "
            "publication_path = 'docs/plans/[G-047] Original Runtime.md', "
            "publication_content_hash = 'original-hash', "
            "publication_sha = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            ("a" * 40,),
        )
        self.connection.commit()
        self.plan = {
            'id': 'G-047', 'title': 'Current Runtime',
            'sources': ['docs/PRODUCT.md'], 'depends_on': [],
            'complexity': 'low', 'risk': 'low', 'priority': 'normal',
        }
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')
        self.effects.publication.published_execplan.return_value = {
            **self.plan, 'title': 'Original Runtime',
        }
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        show = [
            call for call in self.effects.workspace.git.call_args_list
            if len(call.args) >= 3 and call.args[1] == 'show'
        ]
        self.assertEqual(
            show[-1].args[2],
            f"{'a' * 40}:docs/plans/[G-047] Original Runtime.md",
        )
        build = self.effects.publication.build_pr_body.call_args.args
        self.assertEqual(build[1]['title'], 'Current Runtime')
        self.assertEqual(
            build[2], 'docs/plans/[G-047] Original Runtime.md'
        )
        self.assertEqual(build[5], 'a' * 40)
