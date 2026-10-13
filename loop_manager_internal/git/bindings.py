"""Own git dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.agents import scope_observation
from loop_manager_internal.git import base_fetch_service
from loop_manager_internal.git import cleanup_service
from loop_manager_internal.git import direct_rebase_service
from loop_manager_internal.git import git_adapter
from loop_manager_internal.git import merge_cleanup_service
from loop_manager_internal.git import publication_service
from loop_manager_internal.git import workspace_service


def register(assembly: Composition) -> None:
    assembly.bind('_command_adapter_effects_git', git_adapter._command_adapter_effects_git,
        lambda: git_adapter.CommandAdapterEffectsGitEffects(
            _ignored_paths_overlapping_git_update=assembly.callback('_ignored_paths_overlapping_git_update'),
            _rebase_resume_ignored_overlap=assembly.callback('_rebase_resume_ignored_overlap'),
            command=assembly.callback('command'),
            is_git_lock_failure=assembly.callback('is_git_lock_failure'),
        ),
    )

    assembly.bind('_command_adapter_effects_git_bytes', git_adapter._command_adapter_effects_git_bytes,
        lambda: git_adapter.CommandAdapterEffectsGitBytesEffects(
            _handoff_run=assembly.callback('_handoff_run'),
            is_git_lock_failure=assembly.callback('is_git_lock_failure'),
        ),
    )

    assembly.bind('_github_observation_effects_pr_for_branch', git_adapter._github_observation_effects_pr_for_branch,
        lambda: git_adapter.GithubObservationEffectsPrForBranchEffects(command=assembly.callback('command')),
    )

    assembly.bind('_workspace_effects_validate_managed_worktree', git_adapter._workspace_effects_validate_managed_worktree,
        lambda: git_adapter.WorkspaceEffectsValidateManagedWorktreeEffects(
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            _worktree_catalog=assembly.callback('_worktree_catalog'),
        ),
    )

    assembly.bind('_workspace_effects_validate_reserved_worktree', git_adapter._workspace_effects_validate_reserved_worktree,
        lambda: git_adapter.WorkspaceEffectsValidateReservedWorktreeEffects(
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_workspace_effects_make_worktree', git_adapter._workspace_effects_make_worktree,
        lambda: git_adapter.WorkspaceEffectsMakeWorktreeEffects(
            _managed_worktree_marker=assembly.callback('_managed_worktree_marker'),
            _managed_worktree_transition_state=assembly.callback('_managed_worktree_transition_state'),
            _worktree_catalog=assembly.callback('_worktree_catalog'),
            _write_managed_worktree_receipt=assembly.callback('_write_managed_worktree_receipt'),
            git=assembly.callback('git'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            validate_reserved_worktree=assembly.callback('validate_reserved_worktree'),
        ),
    )

    assembly.bind('_workspace_effects_make_planning_worktree', git_adapter._workspace_effects_make_planning_worktree,
        lambda: git_adapter.WorkspaceEffectsMakePlanningWorktreeEffects(
            git=assembly.callback('git'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            validate_reserved_worktree=assembly.callback('validate_reserved_worktree'),
        ),
    )

    assembly.bind('_workspace_effects_validate_execplan_workspace_identity', git_adapter._workspace_effects_validate_execplan_workspace_identity,
        lambda: git_adapter.WorkspaceEffectsValidateExecplanWorkspaceIdentityEffects(
            _validate_managed_path_boundary=assembly.callback('_validate_managed_path_boundary'),
            expected_execplan_workspace=assembly.callback('expected_execplan_workspace'),
        ),
    )

    assembly.bind('_source_sync_effects_base_worktree_snapshot', git_adapter._source_sync_effects_base_worktree_snapshot,
        lambda: git_adapter.SourceSyncEffectsBaseWorktreeSnapshotEffects(
            _base_local_path_signature=assembly.callback('_base_local_path_signature'),
            _base_worktree_index_error=assembly.callback('_base_worktree_index_error'),
            _config_entries=assembly.callback('_config_entries'),
            _git_operation_entries=assembly.callback('_git_operation_entries'),
            _ignored_worktree_paths=assembly.callback('_ignored_worktree_paths'),
            _index_entries=assembly.callback('_index_entries'),
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_source_sync_effects_base_paths_overlap', git_adapter._source_sync_effects_base_paths_overlap,
        lambda: git_adapter.SourceSyncEffectsBasePathsOverlapEffects(_paths_overlap=assembly.callback('_paths_overlap')),
    )

    assembly.bind('_source_sync_effects_source_transition_state', git_adapter._source_sync_effects_source_transition_state,
        lambda: git_adapter.SourceSyncEffectsSourceTransitionStateEffects(_base_worktree_snapshot=assembly.callback('_base_worktree_snapshot')),
    )

    assembly.bind('_source_sync_effects_source_transition_marker', git_adapter._source_sync_effects_source_transition_marker,
        lambda: git_adapter.SourceSyncEffectsSourceTransitionMarkerEffects(git=assembly.callback('git')),
    )

    assembly.bind('_source_sync_effects_source_expected_index', git_adapter._source_sync_effects_source_expected_index,
        lambda: git_adapter.SourceSyncEffectsSourceExpectedIndexEffects(git_bytes=assembly.callback('git_bytes')),
    )

    assembly.bind('_source_sync_effects_synchronize_base_worktree', git_adapter._source_sync_effects_synchronize_base_worktree,
        lambda: git_adapter.SourceSyncEffectsSynchronizeBaseWorktreeEffects(
            _base_paths_overlap=assembly.callback('_base_paths_overlap'),
            _base_worktree_index_error=assembly.callback('_base_worktree_index_error'),
            _base_worktree_snapshot=assembly.callback('_base_worktree_snapshot'),
            _git_operation_entries=assembly.callback('_git_operation_entries'),
            _normalize_legacy_ignored_snapshot=assembly.callback('_normalize_legacy_ignored_snapshot'),
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            _source_expected_index=assembly.callback('_source_expected_index'),
            _source_transition_marker=assembly.callback('_source_transition_marker'),
            _source_transition_state=assembly.callback('_source_transition_state'),
            git=assembly.callback('git'),
            is_git_lock_failure=assembly.callback('is_git_lock_failure'),
        ),
    )

    assembly.bind('_source_sync_effects_ensure_safe_base_worktree', git_adapter._source_sync_effects_ensure_safe_base_worktree,
        lambda: git_adapter.SourceSyncEffectsEnsureSafeBaseWorktreeEffects(synchronize_base_worktree=assembly.callback('synchronize_base_worktree')),
    )

    assembly.bind('_source_sync_effects_log_base_worktree_sync', git_adapter._source_sync_effects_log_base_worktree_sync,
        lambda: git_adapter.SourceSyncEffectsLogBaseWorktreeSyncEffects(operation_log=assembly.callback('operation_log')),
    )

    assembly.bind('_archive_git_effects', git_adapter._archive_git_effects,
        lambda: git_adapter.ArchiveGitEffectsEffects(
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            _validate_managed_path_boundary=assembly.callback('_validate_managed_path_boundary'),
            command=assembly.callback('command'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            is_git_lock_failure=assembly.callback('is_git_lock_failure'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_checkpoint_safety_effects', git_adapter._checkpoint_safety_effects,
        lambda: git_adapter.CheckpointSafetyEffectsEffects(
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _owned_checkpoint_remote_oid=assembly.callback('_owned_checkpoint_remote_oid'),
            _proven_manager_base_refs=assembly.callback('_proven_manager_base_refs'),
            _proven_merged_cleanup_paths=assembly.callback('_proven_merged_cleanup_paths'),
            _published_plan_allows_common_plans=assembly.callback('_published_plan_allows_common_plans'),
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            github_repository=assembly.callback('github_repository'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_checkpoint_publication_effects', git_adapter._checkpoint_publication_effects,
        lambda: git_adapter.CheckpointPublicationEffectsEffects(
            _git_with_index=assembly.callback('_git_with_index'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            command=assembly.callback('command'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_diagnostic_publication_effects', git_adapter._diagnostic_publication_effects,
        lambda: git_adapter.DiagnosticPublicationEffectsEffects(
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_recorded_base_fetch', base_fetch_service._recorded_base_fetch,
        lambda: base_fetch_service.RecordedBaseFetchEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _tracking_ref_oid=assembly.callback('_tracking_ref_oid'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('fetch_bootstrap_revision', base_fetch_service.fetch_bootstrap_revision,
        lambda: base_fetch_service.FetchBootstrapRevisionEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('validate_bootstrap_revision', base_fetch_service.validate_bootstrap_revision,
        lambda: base_fetch_service.ValidateBootstrapRevisionEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('prepare_bootstrap_runtime', base_fetch_service.prepare_bootstrap_runtime,
        lambda: base_fetch_service.PrepareBootstrapRuntimeEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('command', git_adapter.command,
        lambda: git_adapter.CommandEffects(_handoff_run=assembly.callback('_handoff_run')),
    )

    assembly.bind('git', git_adapter.git,
        lambda: git_adapter.GitEffects(_command_adapter_effects_git=assembly.callback('_command_adapter_effects_git')),
    )

    assembly.bind('_ignored_worktree_paths', git_adapter._ignored_worktree_paths,
        lambda: git_adapter.IgnoredWorktreePathsEffects(_nul_git_paths=assembly.callback('_nul_git_paths')),
    )

    assembly.bind('_ignored_paths_overlapping_git_update', git_adapter._ignored_paths_overlapping_git_update,
        lambda: git_adapter.IgnoredPathsOverlappingGitUpdateEffects(
            _ignored_worktree_paths=assembly.callback('_ignored_worktree_paths'),
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            _paths_overlap=assembly.callback('_paths_overlap'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_remaining_rebase_commits', git_adapter._remaining_rebase_commits,
        lambda: git_adapter.RemainingRebaseCommitsEffects(git_bytes=assembly.callback('git_bytes')),
    )

    assembly.bind('_rebase_resume_ignored_overlap', git_adapter._rebase_resume_ignored_overlap,
        lambda: git_adapter.RebaseResumeIgnoredOverlapEffects(
            _ignored_worktree_paths=assembly.callback('_ignored_worktree_paths'),
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            _paths_overlap=assembly.callback('_paths_overlap'),
            _remaining_rebase_commits=assembly.callback('_remaining_rebase_commits'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_base_worktree_snapshot', git_adapter._base_worktree_snapshot,
        lambda: git_adapter.BaseWorktreeSnapshotEffects(_source_sync_effects_base_worktree_snapshot=assembly.callback('_source_sync_effects_base_worktree_snapshot')),
    )

    assembly.bind('_base_paths_overlap', git_adapter._base_paths_overlap,
        lambda: git_adapter.BasePathsOverlapEffects(_source_sync_effects_base_paths_overlap=assembly.callback('_source_sync_effects_base_paths_overlap')),
    )

    assembly.bind('_source_transition_state', git_adapter._source_transition_state,
        lambda: git_adapter.SourceTransitionStateEffects(_source_sync_effects_source_transition_state=assembly.callback('_source_sync_effects_source_transition_state')),
    )

    assembly.bind('_source_transition_marker', git_adapter._source_transition_marker,
        lambda: git_adapter.SourceTransitionMarkerEffects(_source_sync_effects_source_transition_marker=assembly.callback('_source_sync_effects_source_transition_marker')),
    )

    assembly.bind('_source_expected_index', git_adapter._source_expected_index,
        lambda: git_adapter.SourceExpectedIndexEffects(_source_sync_effects_source_expected_index=assembly.callback('_source_sync_effects_source_expected_index')),
    )

    assembly.bind('synchronize_base_worktree', git_adapter.synchronize_base_worktree,
        lambda: git_adapter.SynchronizeBaseWorktreeEffects(_source_sync_effects_synchronize_base_worktree=assembly.callback('_source_sync_effects_synchronize_base_worktree')),
    )

    assembly.bind('ensure_safe_base_worktree', git_adapter.ensure_safe_base_worktree,
        lambda: git_adapter.EnsureSafeBaseWorktreeEffects(_source_sync_effects_ensure_safe_base_worktree=assembly.callback('_source_sync_effects_ensure_safe_base_worktree')),
    )

    assembly.bind('log_base_worktree_sync', git_adapter.log_base_worktree_sync,
        lambda: git_adapter.LogBaseWorktreeSyncEffects(_source_sync_effects_log_base_worktree_sync=assembly.callback('_source_sync_effects_log_base_worktree_sync')),
    )

    assembly.bind('remote_branch_oid', publication_service.remote_branch_oid,
        lambda: publication_service.RemoteBranchOidEffects(git=assembly.callback('git')),
    )

    assembly.bind('_owned_publication_plan', publication_service._owned_publication_plan,
        lambda: publication_service.OwnedPublicationPlanEffects(
            active_execplan=assembly.callback('active_execplan'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('record_remote_publication_branch', publication_service.record_remote_publication_branch,
        lambda: publication_service.RecordRemotePublicationBranchEffects(
            _owned_publication_plan=assembly.callback('_owned_publication_plan'),
            git=assembly.callback('git'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('push_managed_publication_branch', publication_service.push_managed_publication_branch,
        lambda: publication_service.PushManagedPublicationBranchEffects(
            _owned_publication_plan=assembly.callback('_owned_publication_plan'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            active_execplan=assembly.callback('active_execplan'),
            git=assembly.callback('git'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            record_remote_publication_branch=assembly.callback('record_remote_publication_branch'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('push_owned_pr_conflict_recovery', publication_service.push_owned_pr_conflict_recovery,
        lambda: publication_service.PushOwnedPrConflictRecoveryEffects(
            _fetch_pr_conflict_recovery_refs=assembly.callback('_fetch_pr_conflict_recovery_refs'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            active_execplan=assembly.callback('active_execplan'),
            git=assembly.callback('git'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('recover_confirmed_pr_conflict_push', publication_service.recover_confirmed_pr_conflict_push,
        lambda: publication_service.RecoverConfirmedPrConflictPushEffects(
            _fetch_pr_conflict_recovery_refs=assembly.callback('_fetch_pr_conflict_recovery_refs'),
            active_execplan=assembly.callback('active_execplan'),
            git=assembly.callback('git'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_validate_managed_path_boundary', workspace_service._validate_managed_path_boundary,
        lambda: workspace_service.ValidateManagedPathBoundaryEffects(_canonical_path=assembly.callback('_canonical_path')),
    )

    assembly.bind('_worktree_catalog', workspace_service._worktree_catalog,
        lambda: workspace_service.WorktreeCatalogEffects(git=assembly.callback('git')),
    )

    assembly.bind('_rebase_head_name', workspace_service._rebase_head_name,
        lambda: workspace_service.RebaseHeadNameEffects(git=assembly.callback('git')),
    )

    assembly.bind('_rebase_in_progress_for_branch', workspace_service._rebase_in_progress_for_branch,
        lambda: workspace_service.RebaseInProgressForBranchEffects(_rebase_head_name=assembly.callback('_rebase_head_name')),
    )

    assembly.bind('_owned_rebase_state_dir', direct_rebase_service._owned_rebase_state_dir,
        lambda: direct_rebase_service.OwnedRebaseStateDirEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            expected_execplan_workspace=assembly.callback('expected_execplan_workspace'),
        ),
    )

    assembly.bind('_owned_rebase_metadata', direct_rebase_service._owned_rebase_metadata,
        lambda: direct_rebase_service.OwnedRebaseMetadataEffects(git=assembly.callback('git')),
    )

    assembly.bind('_owned_rebase_journal_state', direct_rebase_service._owned_rebase_journal_state,
        lambda: direct_rebase_service.OwnedRebaseJournalStateEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _owned_rebase_metadata=assembly.callback('_owned_rebase_metadata'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_current_rebase_service', direct_rebase_service._current_rebase_service,
        lambda: direct_rebase_service.CurrentRebaseServiceEffects(_managed_rebase_runtime_effects=assembly.callback('_managed_rebase_runtime_effects')),
    )

    assembly.bind('_finish_owned_direct_rebase', direct_rebase_service._finish_owned_direct_rebase,
        lambda: direct_rebase_service.FinishOwnedDirectRebaseEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _owned_rebase_journal_state=assembly.callback('_owned_rebase_journal_state'),
        ),
    )

    assembly.bind('_journaled_direct_rebase', direct_rebase_service._journaled_direct_rebase,
        lambda: direct_rebase_service.JournaledDirectRebaseEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _complete_direct_rebase_checkpoint=assembly.callback('_complete_direct_rebase_checkpoint'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _finish_owned_direct_rebase=assembly.callback('_finish_owned_direct_rebase'),
            _owned_rebase_journal_state=assembly.callback('_owned_rebase_journal_state'),
            _owned_rebase_metadata=assembly.callback('_owned_rebase_metadata'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            git=assembly.callback('git'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
        ),
    )

    assembly.bind('_direct_rebase_history_proven', direct_rebase_service._direct_rebase_history_proven,
        lambda: direct_rebase_service.DirectRebaseHistoryProvenEffects(
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
        ),
    )

    assembly.bind('_direct_rebase_restored_stage_matches', direct_rebase_service._direct_rebase_restored_stage_matches,
        lambda: direct_rebase_service.DirectRebaseRestoredStageMatchesEffects(_direct_rebase_result_matches_observation=assembly.callback('_direct_rebase_result_matches_observation')),
    )

    assembly.bind('_complete_direct_rebase_checkpoint', direct_rebase_service._complete_direct_rebase_checkpoint,
        lambda: direct_rebase_service.CompleteDirectRebaseCheckpointEffects(
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _owned_rebase_journal_state=assembly.callback('_owned_rebase_journal_state'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            _publish_direct_rebase_checkpoint=assembly.callback('_publish_direct_rebase_checkpoint'),
        ),
    )

    assembly.bind('_publish_direct_rebase_checkpoint', direct_rebase_service._publish_direct_rebase_checkpoint,
        lambda: direct_rebase_service.PublishDirectRebaseCheckpointEffects(
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            git=assembly.callback('git'),
            pr_for_branch=assembly.callback('pr_for_branch'),
        ),
    )

    assembly.bind('validate_managed_worktree', workspace_service.validate_managed_worktree,
        lambda: workspace_service.ValidateManagedWorktreeEffects(_workspace_effects_validate_managed_worktree=assembly.callback('_workspace_effects_validate_managed_worktree')),
    )

    assembly.bind('validate_reserved_worktree', workspace_service.validate_reserved_worktree,
        lambda: workspace_service.ValidateReservedWorktreeEffects(_workspace_effects_validate_reserved_worktree=assembly.callback('_workspace_effects_validate_reserved_worktree')),
    )

    assembly.bind('make_worktree', workspace_service.make_worktree,
        lambda: workspace_service.MakeWorktreeEffects(_workspace_effects_make_worktree=assembly.callback('_workspace_effects_make_worktree')),
    )

    assembly.bind('make_planning_worktree', workspace_service.make_planning_worktree,
        lambda: workspace_service.MakePlanningWorktreeEffects(_workspace_effects_make_planning_worktree=assembly.callback('_workspace_effects_make_planning_worktree')),
    )

    assembly.bind('validate_execplan_workspace_identity', workspace_service.validate_execplan_workspace_identity,
        lambda: workspace_service.ValidateExecplanWorkspaceIdentityEffects(_workspace_effects_validate_execplan_workspace_identity=assembly.callback('_workspace_effects_validate_execplan_workspace_identity')),
    )

    assembly.bind('changed_files', workspace_service.changed_files,
        lambda: workspace_service.ChangedFilesEffects(
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            _unstaged_git_paths=lambda worktree: scope_observation._unstaged_git_paths(worktree, effects=assembly.value('_scope_observation_effects')()),
        ),
    )

    assembly.bind('branch_has_changes', workspace_service.branch_has_changes,
        lambda: workspace_service.BranchHasChangesEffects(git=assembly.callback('git')),
    )

    assembly.bind('assess_cleanup', cleanup_service.assess_cleanup,
        lambda: cleanup_service.AssessCleanupEffects(
            _capture_local_worktree_state=assembly.callback('_capture_local_worktree_state'),
            _git_operation_entries=assembly.callback('_git_operation_entries'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('validate_pending_cleanup_worktree', cleanup_service.validate_pending_cleanup_worktree,
        lambda: cleanup_service.ValidatePendingCleanupWorktreeEffects(
            git=assembly.callback('git'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('remove_cleanup_worktree', cleanup_service.remove_cleanup_worktree,
        lambda: cleanup_service.RemoveCleanupWorktreeEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _capture_local_worktree_state=assembly.callback('_capture_local_worktree_state'),
            _git_operation_entries=assembly.callback('_git_operation_entries'),
            _managed_worktree_marker=assembly.callback('_managed_worktree_marker'),
            _managed_worktree_transition_state=assembly.callback('_managed_worktree_transition_state'),
            _write_managed_worktree_receipt=assembly.callback('_write_managed_worktree_receipt'),
            assess_cleanup=assembly.callback('assess_cleanup'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('recheck_cleanup_pending', cleanup_service.recheck_cleanup_pending,
        lambda: cleanup_service.RecheckCleanupPendingEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            _record_merged_worktree_cleanup=assembly.callback('_record_merged_worktree_cleanup'),
            _recover_managed_worktree_removal=assembly.callback('_recover_managed_worktree_removal'),
            assess_cleanup=assembly.callback('assess_cleanup'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            remove_cleanup_worktree=assembly.callback('remove_cleanup_worktree'),
            validate_pending_cleanup_worktree=assembly.callback('validate_pending_cleanup_worktree'),
        ),
    )

    assembly.bind('_record_merged_worktree_cleanup', merge_cleanup_service._record_merged_worktree_cleanup,
        lambda: merge_cleanup_service.RecordMergedWorktreeCleanupEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
        ),
    )

    assembly.bind('_proven_merged_cleanup_paths', merge_cleanup_service._proven_merged_cleanup_paths,
        lambda: merge_cleanup_service.ProvenMergedCleanupPathsEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
        ),
    )

    assembly.bind('_record_manager_base_fetch_intent', base_fetch_service._record_manager_base_fetch_intent,
        lambda: base_fetch_service.RecordManagerBaseFetchIntentEffects(
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            _manager_base_fetch_applied=assembly.callback('_manager_base_fetch_applied'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_manager_base_fetch_applied', base_fetch_service._manager_base_fetch_applied,
        lambda: base_fetch_service.ManagerBaseFetchAppliedEffects(
            _tracking_ref_oid=assembly.callback('_tracking_ref_oid'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_perform_manager_base_fetch', base_fetch_service._perform_manager_base_fetch,
        lambda: base_fetch_service.PerformManagerBaseFetchEffects(git=assembly.callback('git')),
    )

    assembly.bind('_finish_manager_base_fetch', base_fetch_service._finish_manager_base_fetch,
        lambda: base_fetch_service.FinishManagerBaseFetchEffects(
            _manager_base_fetch_applied=assembly.callback('_manager_base_fetch_applied'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_journaled_manager_base_fetch', base_fetch_service._journaled_manager_base_fetch,
        lambda: base_fetch_service.JournaledManagerBaseFetchEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _finish_manager_base_fetch=assembly.callback('_finish_manager_base_fetch'),
            _manager_base_fetch_applied=assembly.callback('_manager_base_fetch_applied'),
            _perform_manager_base_fetch=assembly.callback('_perform_manager_base_fetch'),
            _tracking_ref_oid=assembly.callback('_tracking_ref_oid'),
        ),
    )

    assembly.bind('_proven_manager_base_refs', base_fetch_service._proven_manager_base_refs,
        lambda: base_fetch_service.ProvenManagerBaseRefsEffects(
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            _manager_base_fetch_applied=assembly.callback('_manager_base_fetch_applied'),
        ),
    )

    assembly.bind('_reconciled_manager_scope_metadata', base_fetch_service._reconciled_manager_scope_metadata,
        lambda: base_fetch_service.ReconciledManagerScopeMetadataEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            _proven_manager_base_refs=assembly.callback('_proven_manager_base_refs'),
            _proven_merged_cleanup_paths=assembly.callback('_proven_merged_cleanup_paths'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('complete_merged_worktree', merge_cleanup_service.complete_merged_worktree,
        lambda: merge_cleanup_service.CompleteMergedWorktreeEffects(
            pr_for_branch=assembly.callback('pr_for_branch'),
            _assess_missing_managed_cleanup=assembly.callback('_assess_missing_managed_cleanup'),
            _canonical_path=assembly.callback('_canonical_path'),
            _clear_active_values=assembly.callback('_clear_active_values'),
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            _ensure_execplans_schema=assembly.callback('_ensure_execplans_schema'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _record_merged_worktree_cleanup=assembly.callback('_record_merged_worktree_cleanup'),
            _store_cleanup_pending=assembly.callback('_store_cleanup_pending'),
            assess_cleanup=assembly.callback('assess_cleanup'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            remove_cleanup_worktree=assembly.callback('remove_cleanup_worktree'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('complete_merged_execplan_worktree', merge_cleanup_service.complete_merged_execplan_worktree,
        lambda: merge_cleanup_service.CompleteMergedExecplanWorktreeEffects(
            pr_for_branch=assembly.callback('pr_for_branch'),
            _assess_missing_managed_cleanup=assembly.callback('_assess_missing_managed_cleanup'),
            _canonical_path=assembly.callback('_canonical_path'),
            _clear_active_values=assembly.callback('_clear_active_values'),
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            _record_merged_worktree_cleanup=assembly.callback('_record_merged_worktree_cleanup'),
            _store_cleanup_pending=assembly.callback('_store_cleanup_pending'),
            assess_cleanup=assembly.callback('assess_cleanup'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            remove_cleanup_worktree=assembly.callback('remove_cleanup_worktree'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )
