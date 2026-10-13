"""Own recovery dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.execution import manual_candidate
from loop_manager_internal.recovery import block_service
from loop_manager_internal.recovery import blocked_agent_service
from loop_manager_internal.recovery import blocked_plan_service
from loop_manager_internal.recovery import checkpoint_service
from loop_manager_internal.recovery import diagnostic_preservation
from loop_manager_internal.recovery import pr_ownership
from loop_manager_internal.recovery import project_block_service
from loop_manager_internal.recovery import recovery_adapter
from loop_manager_internal.recovery import waiting_pr_service


def register(assembly: Composition) -> None:
    assembly.bind('_pull_request_recovery_effects_reconcile_existing_pull_request', recovery_adapter._pull_request_recovery_effects_reconcile_existing_pull_request,
        lambda: recovery_adapter.PullRequestRecoveryEffectsReconcileExistingPullRequestEffects(
            _block_closed_pull_request=assembly.callback('_block_closed_pull_request'),
            _fetch_pr_conflict_recovery_refs=assembly.callback('_fetch_pr_conflict_recovery_refs'),
            _is_pr_conflict_preflight_block=assembly.callback('_is_pr_conflict_preflight_block'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _record_project_recovery=assembly.callback('_record_project_recovery'),
            _resume_reopened_pull_request=assembly.callback('_resume_reopened_pull_request'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            _validate_active_pr_conflict_recovery=assembly.callback('_validate_active_pr_conflict_recovery'),
            _validate_blocked_pr_without_conflict=assembly.callback('_validate_blocked_pr_without_conflict'),
            _validate_pr_conflict_recovery_identity=assembly.callback('_validate_pr_conflict_recovery_identity'),
            _validate_pr_conflict_recovery_ownership=assembly.callback('_validate_pr_conflict_recovery_ownership'),
            block_active_execplan_from_revision=assembly.callback('block_active_execplan_from_revision'),
            classify_pull_request_mergeability=assembly.callback('classify_pull_request_mergeability'),
            complete_merged_execplan_worktree=assembly.callback('complete_merged_execplan_worktree'),
            make_worktree=assembly.callback('make_worktree'),
            operation_print=assembly.callback('operation_print'),
            project_execplan=assembly.callback('project_execplan'),
            recover_confirmed_pr_conflict_push=assembly.callback('recover_confirmed_pr_conflict_push'),
            remote_base=assembly.callback('remote_base'),
            update_project=assembly.callback('update_project'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            manual_publication_stage_tokens=lambda connection, repo, plan_id, config_path, provider: manual_candidate.recovery_publication_stage_tokens(
                connection, repo, plan_id, config_path, provider,
                assembly.callback('_agent_profile'),
                assembly.callback('git'), assembly.callback('git_bytes')),
        ),
    )

    assembly.bind('_pull_request_recovery_effects_resume_pr_conflict_rebase', recovery_adapter._pull_request_recovery_effects_resume_pr_conflict_rebase,
        lambda: recovery_adapter.PullRequestRecoveryEffectsResumePrConflictRebaseEffects(
            _complete_direct_rebase_checkpoint=assembly.callback('_complete_direct_rebase_checkpoint'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _finish_owned_direct_rebase=assembly.callback('_finish_owned_direct_rebase'),
            _journaled_direct_rebase=assembly.callback('_journaled_direct_rebase'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            active_execplan=assembly.callback('active_execplan'),
            block_active_execplan=assembly.callback('block_active_execplan'),
            git=assembly.callback('git'),
            is_git_lock_failure=assembly.callback('is_git_lock_failure'),
            operation_print=assembly.callback('operation_print'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('_scope_reconciliation_effects_resume_project_block_if_safe', recovery_adapter._scope_reconciliation_effects_resume_project_block_if_safe,
        lambda: recovery_adapter.ScopeReconciliationEffectsResumeProjectBlockIfSafeEffects(
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _clear_project_block_without_losing_planning_error=assembly.callback('_clear_project_block_without_losing_planning_error'),
            _reconcile_rebase_evidence_block=assembly.callback('_reconcile_rebase_evidence_block'),
            _reconciled_manager_scope_metadata=assembly.callback('_reconciled_manager_scope_metadata'),
            _requeue_blocked_execplan=assembly.callback('_requeue_blocked_execplan'),
            _resume_owned_checkpoint_project_block=assembly.callback('_resume_owned_checkpoint_project_block'),
            _with_proven_legacy_ignore_config=assembly.callback('_with_proven_legacy_ignore_config'),
            _normalize_legacy_ignored_snapshot=assembly.callback('_normalize_legacy_ignored_snapshot'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            hydrate_execplan_project=assembly.callback('hydrate_execplan_project'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            validate_planning_worktree_ownership=assembly.callback('validate_planning_worktree_ownership'),
        ),
    )

    assembly.bind('_scope_reconciliation_effects_resume_plan_blocks', recovery_adapter._scope_reconciliation_effects_resume_plan_blocks,
        lambda: recovery_adapter.ScopeReconciliationEffectsResumePlanBlocksEffects(
            ROOT=assembly.value('ROOT'),
            _blocked_plan_reconciliation_effects=assembly.callback('_blocked_plan_reconciliation_effects'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _reconcile_published_blocked_checkpoint=assembly.callback('_reconcile_published_blocked_checkpoint'),
            _reconcile_rebase_evidence_block=assembly.callback('_reconcile_rebase_evidence_block'),
            _resume_interrupted_blocked_rebase=assembly.callback('_resume_interrupted_blocked_rebase'),
            _resume_legacy_review_count_block=assembly.callback('_resume_legacy_review_count_block'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            active_execplan=assembly.callback('active_execplan'),
            hydrate_execplan_project=assembly.callback('hydrate_execplan_project'),
            load_managed_config=assembly.callback('load_managed_config'),
            plan_blocked_execplans=assembly.callback('plan_blocked_execplans'),
            project_execplan=assembly.callback('project_execplan'),
            resume_blocked_execplan_if_inputs_changed=assembly.callback('resume_blocked_execplan_if_inputs_changed'),
            resume_retired_guidance_block=assembly.callback('resume_retired_guidance_block'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('_managed_rebase_runtime_effects', recovery_adapter._managed_rebase_runtime_effects,
        lambda: recovery_adapter.ManagedRebaseRuntimeEffectsEffects(
            _owned_rebase_metadata=assembly.callback('_owned_rebase_metadata'),
            _normalize_legacy_ignored_snapshot=assembly.callback('_normalize_legacy_ignored_snapshot'),
            _path_signature=assembly.callback('_path_signature'),
            _published_plan_allows_common_plans=assembly.callback('_published_plan_allows_common_plans'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _worker_path_allowed=assembly.callback('_worker_path_allowed'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            requirement_archive_candidates=lambda *args, **kwargs: (),
            requirement_archive_move_set=assembly.callback('requirement_archive_move_set'),
            requirement_archive_path=assembly.callback('requirement_archive_path'),
            validate_active_execplan_metadata=assembly.callback('validate_active_execplan_metadata'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_blocked_plan_reconciliation_effects', recovery_adapter._blocked_plan_reconciliation_effects,
        lambda: recovery_adapter.BlockedPlanReconciliationEffectsEffects(
            ROOT=assembly.value('ROOT'),
            _BlockedCheckpointAdapter=assembly.callback('_BlockedCheckpointAdapter'),
            _blocked_execplan_metadata_error=assembly.callback('_blocked_execplan_metadata_error'),
            _complete_direct_rebase_checkpoint=assembly.callback('_complete_direct_rebase_checkpoint'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _evaluate_remote_checkpoint_cause=assembly.callback('_evaluate_remote_checkpoint_cause'),
            _journaled_direct_rebase=assembly.callback('_journaled_direct_rebase'),
            _non_default_index_flags=assembly.callback('_non_default_index_flags'),
            _reconcile_published_blocked_checkpoint=assembly.callback('_reconcile_published_blocked_checkpoint'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
            _worktree_state_signature=assembly.callback('_worktree_state_signature'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            github_repository=assembly.callback('github_repository'),
            hydrate_execplan_project=assembly.callback('hydrate_execplan_project'),
            load_managed_config=assembly.callback('load_managed_config'),
            observe_blocked_plan_remotes=assembly.callback('observe_blocked_plan_remotes'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_project_blocked_recovery_effects', recovery_adapter._project_blocked_recovery_effects,
        lambda: recovery_adapter.ProjectBlockedRecoveryEffectsEffects(
            _BlockedCheckpointAdapter=assembly.callback('_BlockedCheckpointAdapter'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _journaled_direct_rebase=assembly.callback('_journaled_direct_rebase'),
            _publish_direct_rebase_checkpoint=assembly.callback('_publish_direct_rebase_checkpoint'),
            _reconcile_published_blocked_checkpoint=assembly.callback('_reconcile_published_blocked_checkpoint'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            _worktree_state_signature=assembly.callback('_worktree_state_signature'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            github_repository=assembly.callback('github_repository'),
            observe_blocked_plan_remotes=assembly.callback('observe_blocked_plan_remotes'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            push_diagnostic_checkpoint=assembly.callback('push_diagnostic_checkpoint'),
            preserve_blocked_diagnostics=assembly.callback('preserve_blocked_diagnostics'),
            reconcile_execplan_catalog=assembly.callback('reconcile_execplan_catalog'),
            resume_project_block_if_safe=assembly.callback('resume_project_block_if_safe'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_checkpoint_recovery_effects_reconcile_published_blocked_checkpoint', recovery_adapter._checkpoint_recovery_effects_reconcile_published_blocked_checkpoint,
        lambda: recovery_adapter.CheckpointRecoveryEffectsReconcilePublishedBlockedCheckpointEffects(
            _capture_agent_baseline=assembly.callback('_capture_agent_baseline'),
            _git_with_index=assembly.callback('_git_with_index'),
            _commit_tree=assembly.callback('_commit_tree'),
            _journaled_direct_rebase=assembly.callback('_journaled_direct_rebase'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            _BlockedCheckpointAdapter=assembly.callback('_BlockedCheckpointAdapter'),
            _blocked_execplan_revision_metadata_error=assembly.callback('_blocked_execplan_revision_metadata_error'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _evaluate_remote_checkpoint_cause=assembly.callback('_evaluate_remote_checkpoint_cause'),
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            _published_plan_allows_common_plans=assembly.callback('_published_plan_allows_common_plans'),
            _reclassify_remote_checkpoint_block=assembly.callback('_reclassify_remote_checkpoint_block'),
            _reclassify_remote_specification_deadlock=assembly.callback('_reclassify_remote_specification_deadlock'),
            _remote_checkpoint_input_identity=assembly.callback('_remote_checkpoint_input_identity'),
            _remote_checkpoint_refusal=assembly.callback('_remote_checkpoint_refusal'),
            _resume_remote_corrected_execplan=assembly.callback('_resume_remote_corrected_execplan'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            github_repository=assembly.callback('github_repository'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_checkpoint_recovery_effects_evaluate_remote_checkpoint_cause', recovery_adapter._checkpoint_recovery_effects_evaluate_remote_checkpoint_cause,
        lambda: recovery_adapter.CheckpointRecoveryEffectsEvaluateRemoteCheckpointCauseEffects(
            ROOT=assembly.value('ROOT'),
            _remote_checkpoint_has_new_failure=assembly.callback('_remote_checkpoint_has_new_failure'),
            _remote_checkpoint_review_revision=assembly.callback('_remote_checkpoint_review_revision'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            load_managed_config=assembly.callback('load_managed_config'),
            parse_reviewer_result=assembly.callback('parse_reviewer_result'),
            parse_worker_result=assembly.callback('parse_worker_result'),
            reviewer_targeted_test_reason=assembly.callback('reviewer_targeted_test_reason'),
            run_agent=assembly.callback('run_agent'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
            verify=assembly.callback('verify'),
        ),
    )

    assembly.bind('_scope_reconciliation_effects_resume_owned_checkpoint_project_block', recovery_adapter._scope_reconciliation_effects_resume_owned_checkpoint_project_block,
        lambda: recovery_adapter.ScopeReconciliationEffectsResumeOwnedCheckpointProjectBlockEffects(
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _direct_rebase_history_proven=assembly.callback('_direct_rebase_history_proven'),
            _published_review_commit_intent_matches=assembly.callback('_published_review_commit_intent_matches'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
        ),
    )

    assembly.bind('_scope_reconciliation_effects_resume_legacy_review_count_block', recovery_adapter._scope_reconciliation_effects_resume_legacy_review_count_block,
        lambda: recovery_adapter.ScopeReconciliationEffectsResumeLegacyReviewCountBlockEffects(
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            _with_proven_legacy_ignore_config=assembly.callback('_with_proven_legacy_ignore_config'),
            _normalize_legacy_ignored_snapshot=assembly.callback('_normalize_legacy_ignored_snapshot'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_scope_reconciliation_effects_resume_interrupted_blocked_rebase', recovery_adapter._scope_reconciliation_effects_resume_interrupted_blocked_rebase,
        lambda: recovery_adapter.ScopeReconciliationEffectsResumeInterruptedBlockedRebaseEffects(
            _blocked_execplan_metadata_error=assembly.callback('_blocked_execplan_metadata_error'),
            _blocked_resume_worktree_matches=assembly.callback('_blocked_resume_worktree_matches'),
            _complete_direct_rebase_checkpoint=assembly.callback('_complete_direct_rebase_checkpoint'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _direct_rebase_history_proven=assembly.callback('_direct_rebase_history_proven'),
            _finish_owned_direct_rebase=assembly.callback('_finish_owned_direct_rebase'),
            _owned_rebase_journal_state=assembly.callback('_owned_rebase_journal_state'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            _reconcile_rebase_evidence_block=assembly.callback('_reconcile_rebase_evidence_block'),
            _record_blocked_resume_head=assembly.callback('_record_blocked_resume_head'),
            _requeue_blocked_execplan=assembly.callback('_requeue_blocked_execplan'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            clear_agent_scope=assembly.callback('clear_agent_scope'),
            git=assembly.callback('git'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
        ),
    )

    assembly.bind('_scope_observation_effects', recovery_adapter._scope_observation_effects,
        lambda: recovery_adapter.ScopeObservationEffectsEffects(
            _handoff_run=assembly.callback('_handoff_run'),
            _worktree_catalog=assembly.callback('_worktree_catalog'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_project_pr_merge_confirmed', waiting_pr_service._project_pr_merge_confirmed,
        lambda: waiting_pr_service.ProjectPrMergeConfirmedEffects(_plan_id_from_path=assembly.callback('_plan_id_from_path')),
    )

    assembly.bind('_fetch_pr_conflict_recovery_refs', pr_ownership._fetch_pr_conflict_recovery_refs,
        lambda: pr_ownership.FetchPrConflictRecoveryRefsEffects(_recorded_base_fetch=assembly.callback('_recorded_base_fetch')),
    )

    assembly.bind('_validate_pr_conflict_recovery_identity', pr_ownership._validate_pr_conflict_recovery_identity,
        lambda: pr_ownership.ValidatePrConflictRecoveryIdentityEffects(
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            project_execplan=assembly.callback('project_execplan'),
        ),
    )

    assembly.bind('_validate_pr_conflict_recovery_ownership', pr_ownership._validate_pr_conflict_recovery_ownership,
        lambda: pr_ownership.ValidatePrConflictRecoveryOwnershipEffects(
            _ignored_paths_overlapping_git_update=assembly.callback('_ignored_paths_overlapping_git_update'),
            _ignored_worktree_paths=assembly.callback('_ignored_worktree_paths'),
            _non_default_index_flags=assembly.callback('_non_default_index_flags'),
            _validate_pr_conflict_recovery_identity=assembly.callback('_validate_pr_conflict_recovery_identity'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            make_worktree=assembly.callback('make_worktree'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('_validate_blocked_pr_without_conflict', pr_ownership._validate_blocked_pr_without_conflict,
        lambda: pr_ownership.ValidateBlockedPrWithoutConflictEffects(
            _ignored_worktree_paths=assembly.callback('_ignored_worktree_paths'),
            _non_default_index_flags=assembly.callback('_non_default_index_flags'),
            _validate_pr_conflict_recovery_identity=assembly.callback('_validate_pr_conflict_recovery_identity'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('_validate_active_pr_conflict_recovery', pr_ownership._validate_active_pr_conflict_recovery,
        lambda: pr_ownership.ValidateActivePrConflictRecoveryEffects(
            _fetch_pr_conflict_recovery_refs=assembly.callback('_fetch_pr_conflict_recovery_refs'),
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            active_execplan=assembly.callback('active_execplan'),
            make_worktree=assembly.callback('make_worktree'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('_blocked_execplan_metadata_error', block_service._blocked_execplan_metadata_error,
        lambda: block_service.BlockedExecplanMetadataErrorEffects(
            _blocked_execplan_metadata_identity_error=assembly.callback('_blocked_execplan_metadata_identity_error'),
            _parse_execplan=assembly.callback('_parse_execplan'),
        ),
    )

    assembly.bind('_blocked_execplan_revision_metadata_error', block_service._blocked_execplan_revision_metadata_error,
        lambda: block_service.BlockedExecplanRevisionMetadataErrorEffects(
            _blocked_execplan_metadata_identity_error=assembly.callback('_blocked_execplan_metadata_identity_error'),
            git=assembly.callback('git'),
            published_execplan=assembly.callback('published_execplan'),
        ),
    )

    assembly.bind('block_active_execplan', block_service.block_active_execplan,
        lambda: block_service.BlockActiveExecplanEffects(
            _final_block_recovery_decision=assembly.callback('_final_block_recovery_decision'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _revision_review_issue_fingerprint=assembly.callback('_revision_review_issue_fingerprint'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            project_execplan=assembly.callback('project_execplan'),
        ),
    )

    assembly.bind('block_verify_timeout_limit', block_service.block_verify_timeout_limit,
        lambda: block_service.BlockVerifyTimeoutLimitEffects(
            block_active_execplan=assembly.callback('block_active_execplan'),
            redact_secrets=assembly.callback('redact_secrets'),
        ),
    )

    assembly.bind('transition_validation_failure', block_service.transition_validation_failure,
        lambda: block_service.TransitionValidationFailureEffects(
            _blocked_validation_base_revision=assembly.callback('_blocked_validation_base_revision'),
            _blocked_validation_input_fingerprints=assembly.callback('_blocked_validation_input_fingerprints'),
            _deserialize_failure_history=assembly.callback('_deserialize_failure_history'),
            _deserialize_review_level_history=assembly.callback('_deserialize_review_level_history'),
            _final_block_recovery_decision=assembly.callback('_final_block_recovery_decision'),
            _revision_review_issue_fingerprint=assembly.callback('_revision_review_issue_fingerprint'),
            active_execplan=assembly.callback('active_execplan'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            evaluate_review_worker_level=assembly.callback('evaluate_review_worker_level'),
            redact_secrets=assembly.callback('redact_secrets'),
            validation_failure_fingerprints=assembly.callback('validation_failure_fingerprints'),
        ),
    )

    assembly.bind('block_active_execplan_from_revision', block_service.block_active_execplan_from_revision,
        lambda: block_service.BlockActiveExecplanFromRevisionEffects(
            _final_block_recovery_decision=assembly.callback('_final_block_recovery_decision'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            active_execplan=assembly.callback('active_execplan'),
            project_execplan=assembly.callback('project_execplan'),
        ),
    )

    assembly.bind('_restore_agent_index_flags', blocked_agent_service._restore_agent_index_flags,
        lambda: blocked_agent_service.RestoreAgentIndexFlagsEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('_restore_agent_staged_entries', blocked_agent_service._restore_agent_staged_entries,
        lambda: blocked_agent_service.RestoreAgentStagedEntriesEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('_restore_agent_baseline', blocked_agent_service._restore_agent_baseline,
        lambda: blocked_agent_service.RestoreAgentBaselineEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('_remove_interrupted_recovery_temp', blocked_agent_service._remove_interrupted_recovery_temp,
        lambda: blocked_agent_service.RemoveInterruptedRecoveryTempEffects(_worktree_relative_target=assembly.callback('_worktree_relative_target')),
    )

    assembly.bind('_atomic_restore_file', blocked_agent_service._atomic_restore_file,
        lambda: blocked_agent_service.AtomicRestoreFileEffects(
            _remove_interrupted_recovery_temp=assembly.callback('_remove_interrupted_recovery_temp'),
            _worktree_relative_target=assembly.callback('_worktree_relative_target'),
        ),
    )

    assembly.bind('_blocked_recovery_stash_oid', blocked_agent_service._blocked_recovery_stash_oid,
        lambda: blocked_agent_service.BlockedRecoveryStashOidEffects(git=assembly.callback('git')),
    )

    assembly.bind('_restore_blocked_agent_recovery_plan', blocked_agent_service._restore_blocked_agent_recovery_plan,
        lambda: blocked_agent_service.RestoreBlockedAgentRecoveryPlanEffects(
            _atomic_restore_file=assembly.callback('_atomic_restore_file'),
            _worktree_relative_target=assembly.callback('_worktree_relative_target'),
        ),
    )

    assembly.bind('_recovered_agent_local_state_matches', blocked_agent_service._recovered_agent_local_state_matches,
        lambda: blocked_agent_service.RecoveredAgentLocalStateMatchesEffects(_worktree_relative_target=assembly.callback('_worktree_relative_target')),
    )

    assembly.bind('_mark_blocked_agent_isolated', blocked_agent_service._mark_blocked_agent_isolated,
        lambda: blocked_agent_service.MarkBlockedAgentIsolatedEffects(
            _serialize_blocked_agent_recovery=assembly.callback('_serialize_blocked_agent_recovery'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('_complete_blocked_agent_recovery', blocked_agent_service._complete_blocked_agent_recovery,
        lambda: blocked_agent_service.CompleteBlockedAgentRecoveryEffects(
            _blocked_recovery_stash_oid=assembly.callback('_blocked_recovery_stash_oid'),
            _config_entries=assembly.callback('_config_entries'),
            _deserialize_blocked_agent_recovery=assembly.callback('_deserialize_blocked_agent_recovery'),
            _mark_blocked_agent_isolated=assembly.callback('_mark_blocked_agent_isolated'),
            _recovered_agent_local_state_matches=assembly.callback('_recovered_agent_local_state_matches'),
            _remove_interrupted_recovery_temp=assembly.callback('_remove_interrupted_recovery_temp'),
            _restore_agent_baseline=assembly.callback('_restore_agent_baseline'),
            _restore_blocked_agent_recovery_plan=assembly.callback('_restore_blocked_agent_recovery_plan'),
            _same_local_snapshot=assembly.callback('_same_local_snapshot'),
            _serialize_blocked_agent_recovery=assembly.callback('_serialize_blocked_agent_recovery'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('preserve_blocked_agent_changes', blocked_agent_service.preserve_blocked_agent_changes,
        lambda: blocked_agent_service.PreserveBlockedAgentChangesEffects(
            _blocked_agent_local_baseline=assembly.callback('_blocked_agent_local_baseline'),
            _complete_blocked_agent_recovery=assembly.callback('_complete_blocked_agent_recovery'),
            _deserialize_blocked_agent_recovery=assembly.callback('_deserialize_blocked_agent_recovery'),
            _reconcile_blocked_reviewer_plan=assembly.callback('_reconcile_blocked_reviewer_plan'),
            _serialize_blocked_agent_recovery=assembly.callback('_serialize_blocked_agent_recovery'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('resume_interrupted_blocked_agent_recovery', blocked_agent_service.resume_interrupted_blocked_agent_recovery,
        lambda: blocked_agent_service.ResumeInterruptedBlockedAgentRecoveryEffects(
            _complete_blocked_agent_recovery=assembly.callback('_complete_blocked_agent_recovery'),
            _config_entries=assembly.callback('_config_entries'),
            _deserialize_blocked_agent_recovery=assembly.callback('_deserialize_blocked_agent_recovery'),
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            make_worktree=assembly.callback('make_worktree'),
            project_execplan=assembly.callback('project_execplan'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('_requeue_blocked_execplan', blocked_plan_service._requeue_blocked_execplan,
        lambda: blocked_plan_service.RequeueBlockedExecplanEffects(_clear_active_values=assembly.callback('_clear_active_values')),
    )

    assembly.bind('_blocked_resume_worktree_matches', blocked_plan_service._blocked_resume_worktree_matches,
        lambda: blocked_plan_service.BlockedResumeWorktreeMatchesEffects(
            _restore_agent_staged_entries=assembly.callback('_restore_agent_staged_entries'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_resume_interrupted_blocked_rebase', blocked_plan_service._resume_interrupted_blocked_rebase,
        lambda: blocked_plan_service.ResumeInterruptedBlockedRebaseEffects(_scope_reconciliation_effects_resume_interrupted_blocked_rebase=assembly.callback('_scope_reconciliation_effects_resume_interrupted_blocked_rebase')),
    )

    assembly.bind('resume_blocked_execplan_if_inputs_changed', blocked_plan_service.resume_blocked_execplan_if_inputs_changed,
        lambda: blocked_plan_service.ResumeBlockedExecplanIfInputsChangedEffects(
            _blocked_execplan_metadata_error=assembly.callback('_blocked_execplan_metadata_error'),
            _blocked_execplan_revision_metadata_error=assembly.callback('_blocked_execplan_revision_metadata_error'),
            _blocked_resume_worktree_matches=assembly.callback('_blocked_resume_worktree_matches'),
            _complete_direct_rebase_checkpoint=assembly.callback('_complete_direct_rebase_checkpoint'),
            _direct_rebase_history_proven=assembly.callback('_direct_rebase_history_proven'),
            _journaled_direct_rebase=assembly.callback('_journaled_direct_rebase'),
            _owned_rebase_journal_state=assembly.callback('_owned_rebase_journal_state'),
            _plan_block_selected_for_release=assembly.callback('_plan_block_selected_for_release'),
            _reconcile_rebase_evidence_block=assembly.callback('_reconcile_rebase_evidence_block'),
            _reconcile_scope_baseline_plan_block=assembly.callback('_reconcile_scope_baseline_plan_block'),
            _record_blocked_resume_head=assembly.callback('_record_blocked_resume_head'),
            _requeue_blocked_execplan=assembly.callback('_requeue_blocked_execplan'),
            _restore_agent_staged_entries=assembly.callback('_restore_agent_staged_entries'),
            _resume_interrupted_blocked_rebase=assembly.callback('_resume_interrupted_blocked_rebase'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _revision_review_issue_fingerprint=assembly.callback('_revision_review_issue_fingerprint'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            clear_agent_scope=assembly.callback('clear_agent_scope'),
            git=assembly.callback('git'),
            hydrate_execplan_project=assembly.callback('hydrate_execplan_project'),
            make_worktree=assembly.callback('make_worktree'),
            preserve_blocked_agent_changes=assembly.callback('preserve_blocked_agent_changes'),
            project_execplan=assembly.callback('project_execplan'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('observe_waiting_pull_requests', waiting_pr_service.observe_waiting_pull_requests,
        lambda: waiting_pr_service.ObserveWaitingPullRequestsEffects(
            base_head=lambda repo, base: assembly.value('git')(repo, 'rev-parse', f'refs/remotes/origin/{base}').stdout.strip(),
            _block_closed_pull_request=assembly.callback('_block_closed_pull_request'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            _resume_reopened_pull_request=assembly.callback('_resume_reopened_pull_request'),
            classify_pull_request_mergeability=assembly.callback('classify_pull_request_mergeability'),
            complete_merged_execplan_worktree=assembly.callback('complete_merged_execplan_worktree'),
            operation_log=assembly.callback('operation_log'),
            operation_print=assembly.callback('operation_print'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            resolve_profile=assembly.callback('_agent_profile'),
        ),
    )

    assembly.bind('select_waiting_pull_request_conflict', waiting_pr_service.select_waiting_pull_request_conflict,
        lambda: waiting_pr_service.SelectWaitingPullRequestConflictEffects(
            active_execplan=assembly.callback('active_execplan'),
            hydrate_execplan_project=assembly.callback('hydrate_execplan_project'),
            reconcile_existing_pull_request=assembly.callback('reconcile_existing_pull_request'),
        ),
    )

    assembly.bind('synchronize_waiting_pull_requests', waiting_pr_service.synchronize_waiting_pull_requests,
        lambda: waiting_pr_service.SynchronizeWaitingPullRequestsEffects(
            observe_waiting_pull_requests=assembly.callback('observe_waiting_pull_requests'),
            select_waiting_pull_request_conflict=assembly.callback('select_waiting_pull_request_conflict'),
        ),
    )

    assembly.bind('_published_review_commit_intent_matches', project_block_service._published_review_commit_intent_matches,
        lambda: project_block_service.PublishedReviewCommitIntentMatchesEffects(git=assembly.callback('git')),
    )

    assembly.bind('_resume_owned_checkpoint_project_block', project_block_service._resume_owned_checkpoint_project_block,
        lambda: project_block_service.ResumeOwnedCheckpointProjectBlockEffects(_scope_reconciliation_effects_resume_owned_checkpoint_project_block=assembly.callback('_scope_reconciliation_effects_resume_owned_checkpoint_project_block')),
    )

    assembly.bind('_reconcile_rebase_evidence_block', project_block_service._reconcile_rebase_evidence_block,
        lambda: project_block_service.ReconcileRebaseEvidenceBlockEffects(
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            operation_log=assembly.callback('operation_log'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
        ),
    )

    assembly.bind('_reconcile_scope_baseline_plan_block', project_block_service._reconcile_scope_baseline_plan_block,
        lambda: project_block_service.ReconcileScopeBaselinePlanBlockEffects(
            _owned_checkpoint_remote_oid=assembly.callback('_owned_checkpoint_remote_oid'),
            _with_proven_legacy_ignore_config=assembly.callback('_with_proven_legacy_ignore_config'),
            _normalize_legacy_ignored_snapshot=assembly.callback('_normalize_legacy_ignored_snapshot'),
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            _reconciled_manager_scope_metadata=assembly.callback('_reconciled_manager_scope_metadata'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            operation_log=assembly.callback('operation_log'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('resume_project_block_if_safe', project_block_service.resume_project_block_if_safe,
        lambda: project_block_service.ResumeProjectBlockIfSafeEffects(_scope_reconciliation_effects_resume_project_block_if_safe=assembly.callback('_scope_reconciliation_effects_resume_project_block_if_safe')),
    )

    assembly.bind('_resume_remote_corrected_execplan', checkpoint_service._resume_remote_corrected_execplan,
        lambda: checkpoint_service.ResumeRemoteCorrectedExecplanEffects(hydrate_execplan_project=assembly.callback('hydrate_execplan_project')),
    )

    assembly.bind('_remote_checkpoint_has_new_failure', checkpoint_service._remote_checkpoint_has_new_failure,
        lambda: checkpoint_service.RemoteCheckpointHasNewFailureEffects(
            _deserialize_failure_history=assembly.callback('_deserialize_failure_history'),
            validation_failure_fingerprints=assembly.callback('validation_failure_fingerprints'),
        ),
    )

    assembly.bind('_remote_checkpoint_review_revision', checkpoint_service._remote_checkpoint_review_revision,
        lambda: checkpoint_service.RemoteCheckpointReviewRevisionEffects(
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_evaluate_remote_checkpoint_cause', checkpoint_service._evaluate_remote_checkpoint_cause,
        lambda: checkpoint_service.EvaluateRemoteCheckpointCauseEffects(_checkpoint_recovery_effects_evaluate_remote_checkpoint_cause=assembly.callback('_checkpoint_recovery_effects_evaluate_remote_checkpoint_cause')),
    )

    assembly.bind('_reclassify_remote_specification_deadlock', checkpoint_service._reclassify_remote_specification_deadlock,
        lambda: checkpoint_service.ReclassifyRemoteSpecificationDeadlockEffects(
            _reclassify_remote_checkpoint_block=assembly.callback('_reclassify_remote_checkpoint_block'),
            parse_reviewer_result=assembly.callback('parse_reviewer_result'),
        ),
    )

    assembly.bind('_reclassify_remote_checkpoint_block', checkpoint_service._reclassify_remote_checkpoint_block,
        lambda: checkpoint_service.ReclassifyRemoteCheckpointBlockEffects(
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _revision_review_issue_fingerprint=assembly.callback('_revision_review_issue_fingerprint'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
        ),
    )

    assembly.bind('_reconcile_published_blocked_checkpoint', checkpoint_service._reconcile_published_blocked_checkpoint,
        lambda: checkpoint_service.ReconcilePublishedBlockedCheckpointEffects(_checkpoint_recovery_effects_reconcile_published_blocked_checkpoint=assembly.callback('_checkpoint_recovery_effects_reconcile_published_blocked_checkpoint')),
    )

    assembly.bind('_resume_legacy_review_count_block', checkpoint_service._resume_legacy_review_count_block,
        lambda: checkpoint_service.ResumeLegacyReviewCountBlockEffects(_scope_reconciliation_effects_resume_legacy_review_count_block=assembly.callback('_scope_reconciliation_effects_resume_legacy_review_count_block')),
    )

    assembly.bind('resume_plan_blocks', checkpoint_service.resume_plan_blocks,
        lambda: checkpoint_service.ResumePlanBlocksEffects(_scope_reconciliation_effects_resume_plan_blocks=assembly.callback('_scope_reconciliation_effects_resume_plan_blocks')),
    )

    assembly.bind('_block_closed_pull_request', waiting_pr_service._block_closed_pull_request,
        lambda: waiting_pr_service.BlockClosedPullRequestEffects(_final_block_recovery_decision=assembly.callback('_final_block_recovery_decision')),
    )

    assembly.bind('_resume_reopened_pull_request', waiting_pr_service._resume_reopened_pull_request,
        lambda: waiting_pr_service.ResumeReopenedPullRequestEffects(
            _clear_active_values=assembly.callback('_clear_active_values'),
            operation_log=assembly.callback('operation_log'),
        ),
    )

    assembly.bind('reconcile_existing_pull_request', waiting_pr_service.reconcile_existing_pull_request,
        lambda: waiting_pr_service.ReconcileExistingPullRequestEffects(_pull_request_recovery_effects_reconcile_existing_pull_request=assembly.callback('_pull_request_recovery_effects_reconcile_existing_pull_request')),
    )

    assembly.bind('resume_pr_conflict_rebase', waiting_pr_service.resume_pr_conflict_rebase,
        lambda: waiting_pr_service.ResumePrConflictRebaseEffects(_pull_request_recovery_effects_resume_pr_conflict_rebase=assembly.callback('_pull_request_recovery_effects_resume_pr_conflict_rebase')),
    )

    assembly.bind('reconcile_false_positive_runtime_block', project_block_service.reconcile_false_positive_runtime_block,
        lambda: project_block_service.ReconcileFalsePositiveRuntimeBlockEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            operation_log=assembly.callback('operation_log'),
            validate_persisted_agent_scope=assembly.callback('validate_persisted_agent_scope'),
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            _reconciled_manager_scope_metadata=assembly.callback('_reconciled_manager_scope_metadata'),
        ),
    )

    assembly.bind('observe_blocked_plan_remotes', project_block_service.observe_blocked_plan_remotes,
        lambda: project_block_service.ObserveBlockedPlanRemotesEffects(
            _worktree_state_signature=assembly.callback('_worktree_state_signature'),
            git=assembly.callback('git'),
            github_repository=assembly.callback('github_repository'),
        ),
    )

    assembly.bind('_owned_checkpoint_remote_oid', checkpoint_service._owned_checkpoint_remote_oid,
        lambda: checkpoint_service.OwnedCheckpointRemoteOidEffects(git=assembly.callback('git')),
    )

    assembly.bind('_guidance_checkpoint_commit_evidence', checkpoint_service._guidance_checkpoint_commit_evidence,
        lambda: checkpoint_service.GuidanceCheckpointCommitEvidenceEffects(
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_BlockedCheckpointAdapter', checkpoint_service._BlockedCheckpointAdapter,
        lambda: checkpoint_service.BlockedCheckpointAdapterEffects(
            _checkpoint_publication_effects=assembly.callback('_checkpoint_publication_effects'),
            _checkpoint_safety_effects=assembly.callback('_checkpoint_safety_effects'),
        ),
    )

    assembly.bind('push_diagnostic_checkpoint', checkpoint_service.push_diagnostic_checkpoint,
        lambda: checkpoint_service.PushDiagnosticCheckpointEffects(_diagnostic_publication_effects=assembly.callback('_diagnostic_publication_effects')),
    )

    assembly.bind('dispatch_blocked_checkpoints', checkpoint_service.dispatch_blocked_checkpoints,
        lambda: checkpoint_service.DispatchBlockedCheckpointsEffects(
            _BlockedCheckpointAdapter=assembly.callback('_BlockedCheckpointAdapter'),
            preserve_blocked_diagnostics=assembly.callback('preserve_blocked_diagnostics'),
        ),
    )

    assembly.factory('_diagnostic_preservation_effects',
        lambda: diagnostic_preservation.PreservationEffects(
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            push_diagnostic_checkpoint=assembly.callback('push_diagnostic_checkpoint'),
        ),
    )

    assembly.bind('preserve_blocked_diagnostics', checkpoint_service.preserve_blocked_diagnostics,
        lambda: checkpoint_service.PreserveBlockedDiagnosticsEffects(
            preservation_effects=assembly.callback('_diagnostic_preservation_effects'),
            adapter_factory=assembly.callback('_BlockedCheckpointAdapter'),
        ),
    )
