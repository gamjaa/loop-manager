"""Own agents dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.agents import agent_adapter
from loop_manager_internal.agents import scope_adapter
from loop_manager_internal.agents import scope_state_service


def register(assembly: Composition) -> None:
    assembly.bind('_provider_adapters_effects', agent_adapter._provider_adapters_effects,
        lambda: agent_adapter.ProviderAdaptersEffectsEffects(
            _agent_profile=assembly.callback('_agent_profile'),
            _handoff_popen=assembly.callback('_handoff_popen'),
            _handoff_run=assembly.callback('_handoff_run'),
            role=assembly.callback('role'),
            worker_profile=assembly.callback('worker_profile'),
            validate_provider_runtime=assembly.callback('validate_provider_runtime'),
        ),
    )

    assembly.bind('_invocation_service_effects', agent_adapter._invocation_service_effects,
        lambda: agent_adapter.InvocationServiceEffectsEffects(
            _agent_profile=assembly.callback('_agent_profile'),
            _check_runtime_handoff=assembly.callback('_check_runtime_handoff'),
            _start_agent_invocation=assembly.callback('_start_agent_invocation'),
            agent_provider=assembly.callback('agent_provider'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_scope_validation_effects', agent_adapter._scope_validation_effects,
        lambda: agent_adapter.ScopeValidationEffectsEffects(
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _git_common_directory=assembly.callback('_git_common_directory'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            _scope_snapshot_with_start_ignore_rules=assembly.callback('_scope_snapshot_with_start_ignore_rules'),
            agent_snapshot_changes=assembly.callback('agent_snapshot_changes'),
            git=assembly.callback('git'),
            operation_log=assembly.callback('operation_log'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_scope_lifecycle_effects', agent_adapter._scope_lifecycle_effects,
        lambda: agent_adapter.ScopeLifecycleEffectsEffects(
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            _capture_agent_baseline=assembly.callback('_capture_agent_baseline'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _published_plan_allows_common_plans=assembly.callback('_published_plan_allows_common_plans'),
            _reconciled_manager_scope_metadata=assembly.callback('_reconciled_manager_scope_metadata'),
            _remote_adoption_agent_baseline=assembly.callback('_remote_adoption_agent_baseline'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            project_execplan=assembly.callback('project_execplan'),
            update_project=assembly.callback('update_project'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
        ),
    )

    assembly.bind('_scope_recovery_effects', agent_adapter._scope_recovery_effects,
        lambda: agent_adapter.ScopeRecoveryEffectsEffects(
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            _config_entries=assembly.callback('_config_entries'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _normalize_legacy_ignored_snapshot=assembly.callback('_normalize_legacy_ignored_snapshot'),
            _owned_checkpoint_remote_oid=assembly.callback('_owned_checkpoint_remote_oid'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _proven_manager_base_refs=assembly.callback('_proven_manager_base_refs'),
            _proven_merged_cleanup_paths=assembly.callback('_proven_merged_cleanup_paths'),
            _published_plan_allows_common_plans=assembly.callback('_published_plan_allows_common_plans'),
            _reconciled_manager_scope_metadata=assembly.callback('_reconciled_manager_scope_metadata'),
            _validate_runtime_lock_origin=assembly.callback('_validate_runtime_lock_origin'),
            _validated_runtime_registration_marker=assembly.callback('_validated_runtime_registration_marker'),
            _with_proven_legacy_ignore_config=assembly.callback('_with_proven_legacy_ignore_config'),
            active_execplan=assembly.callback('active_execplan'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            git=assembly.callback('git'),
            github_repository=assembly.callback('github_repository'),
            operation_log=assembly.callback('operation_log'),
            project_execplan=assembly.callback('project_execplan'),
            recover_persisted_agent_worktree=assembly.callback('recover_persisted_agent_worktree'),
            requirement_archive_candidates=lambda *args, **kwargs: (),
            update_project=assembly.callback('update_project'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
            validate_runtime_worktree=assembly.callback('validate_runtime_worktree'),
        ),
    )

    assembly.bind('_scope_baseline_effects', agent_adapter._scope_baseline_effects,
        lambda: agent_adapter.ScopeBaselineEffectsEffects(
            _handoff_run=assembly.callback('_handoff_run'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            command=assembly.callback('command'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_agent_profiles_effects', agent_adapter._agent_profiles_effects,
        lambda: agent_adapter.AgentProfilesEffectsEffects(
            claude_model_efforts=assembly.callback('claude_model_efforts'),
            claude_role=assembly.callback('claude_role'),
            claude_worker_profile=assembly.callback('claude_worker_profile'),
            codex_model_efforts=assembly.callback('codex_model_efforts'),
            command=assembly.callback('command'),
            role=assembly.callback('role'),
            worker_profile=assembly.callback('worker_profile'),
        ),
    )

    assembly.bind('codex_profile', agent_adapter.codex_profile,
        lambda: agent_adapter.CodexProfileEffects(
            codex_model_efforts=assembly.callback('codex_model_efforts'),
            provider_profile=assembly.callback('provider_profile'),
        ),
    )

    assembly.bind('role', agent_adapter.role,
        lambda: agent_adapter.RoleEffects(codex_profile=assembly.callback('codex_profile')),
    )

    assembly.bind('worker_profile', agent_adapter.worker_profile,
        lambda: agent_adapter.WorkerProfileEffects(
            codex_model_efforts=assembly.callback('codex_model_efforts'),
            provider_worker_profile=assembly.callback('provider_worker_profile'),
        ),
    )

    assembly.bind('claude_model_efforts', agent_adapter.claude_model_efforts,
        lambda: agent_adapter.ClaudeModelEffortsEffects(_agent_profiles_effects=assembly.callback('_agent_profiles_effects')),
    )

    assembly.bind('claude_role', agent_adapter.claude_role,
        lambda: agent_adapter.ClaudeRoleEffects(
            claude_model_efforts=assembly.callback('claude_model_efforts'),
            provider_profile=assembly.callback('provider_profile'),
        ),
    )

    assembly.bind('claude_worker_profile', agent_adapter.claude_worker_profile,
        lambda: agent_adapter.ClaudeWorkerProfileEffects(
            claude_model_efforts=assembly.callback('claude_model_efforts'),
            provider_worker_profile=assembly.callback('provider_worker_profile'),
        ),
    )

    assembly.bind('run_codex', agent_adapter.run_codex,
        lambda: agent_adapter.RunCodexEffects(_provider_adapters_effects=assembly.callback('_provider_adapters_effects')),
    )

    assembly.bind('_agent_profile', agent_adapter._agent_profile,
        lambda: agent_adapter.AgentProfileEffects(_agent_profiles_effects=assembly.callback('_agent_profiles_effects')),
    )

    assembly.bind('validate_provider_runtime', agent_adapter.validate_provider_runtime,
        lambda: agent_adapter.ValidateProviderRuntimeEffects(_agent_profiles_effects=assembly.callback('_agent_profiles_effects')),
    )

    assembly.bind('run_claude', agent_adapter.run_claude,
        lambda: agent_adapter.RunClaudeEffects(_provider_adapters_effects=assembly.callback('_provider_adapters_effects')),
    )

    assembly.bind('agent_provider', agent_adapter.agent_provider,
        lambda: agent_adapter.AgentProviderEffects(
            run_claude=assembly.callback('run_claude'),
            run_codex=assembly.callback('run_codex'),
        ),
    )

    assembly.bind('run_agent', agent_adapter.run_agent,
        lambda: agent_adapter.RunAgentEffects(_invocation_service_effects=assembly.callback('_invocation_service_effects')),
    )

    assembly.bind('_run_agent_with_connection', agent_adapter._run_agent_with_connection,
        lambda: agent_adapter.RunAgentWithConnectionEffects(run_agent=assembly.callback('run_agent')),
    )

    assembly.bind('_nul_git_paths', scope_adapter._nul_git_paths,
        lambda: scope_adapter.NulGitPathsEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_index_entries', scope_adapter._index_entries,
        lambda: scope_adapter.IndexEntriesEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_shared_ref_entries', scope_adapter._shared_ref_entries,
        lambda: scope_adapter.SharedRefEntriesEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_registered_worktree_entries', scope_adapter._registered_worktree_entries,
        lambda: scope_adapter.RegisteredWorktreeEntriesEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_config_entries', scope_adapter._config_entries,
        lambda: scope_adapter.ConfigEntriesEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_with_proven_legacy_ignore_config', scope_adapter._with_proven_legacy_ignore_config,
        lambda: scope_adapter.WithProvenLegacyIgnoreConfigEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_git_operation_entries', scope_adapter._git_operation_entries,
        lambda: scope_adapter.GitOperationEntriesEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_path_signature', scope_adapter._path_signature,
        lambda: scope_adapter.PathSignatureEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_capture_local_worktree_state', scope_adapter._capture_local_worktree_state,
        lambda: scope_adapter.CaptureLocalWorktreeStateEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_worktree_state_signature', scope_adapter._worktree_state_signature,
        lambda: scope_adapter.WorktreeStateSignatureEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_ignore_rule_source', scope_adapter._ignore_rule_source,
        lambda: scope_adapter.IgnoreRuleSourceEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_normalize_legacy_ignored_snapshot', scope_adapter._normalize_legacy_ignored_snapshot,
        lambda: scope_adapter.NormalizeLegacyIgnoredSnapshotEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('capture_git_snapshot', scope_adapter.capture_git_snapshot,
        lambda: scope_adapter.CaptureGitSnapshotEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('agent_snapshot_changes', scope_adapter.agent_snapshot_changes,
        lambda: scope_adapter.AgentSnapshotChangesEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_scope_snapshot_with_start_ignore_rules', scope_adapter._scope_snapshot_with_start_ignore_rules,
        lambda: scope_adapter.ScopeSnapshotWithStartIgnoreRulesEffects(_scope_observation_effects=assembly.callback('_scope_observation_effects')),
    )

    assembly.bind('_agent_git_resources', scope_adapter._agent_git_resources,
        lambda: scope_adapter.AgentGitResourcesEffects(_scope_validation_effects=assembly.callback('_scope_validation_effects')),
    )

    assembly.bind('validate_agent_scope', scope_adapter.validate_agent_scope,
        lambda: scope_adapter.ValidateAgentScopeEffects(_scope_validation_effects=assembly.callback('_scope_validation_effects')),
    )

    assembly.bind('_git_with_index', scope_adapter._git_with_index,
        lambda: scope_adapter.GitWithIndexEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('_hash_git_blob', scope_adapter._hash_git_blob,
        lambda: scope_adapter.HashGitBlobEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('_agent_worktree_tree', scope_adapter._agent_worktree_tree,
        lambda: scope_adapter.AgentWorktreeTreeEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('_commit_tree', scope_adapter._commit_tree,
        lambda: scope_adapter.CommitTreeEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('_capture_agent_baseline', scope_adapter._capture_agent_baseline,
        lambda: scope_adapter.CaptureAgentBaselineEffects(_scope_baseline_effects=assembly.callback('_scope_baseline_effects')),
    )

    assembly.bind('begin_agent_scope', scope_adapter.begin_agent_scope,
        lambda: scope_adapter.BeginAgentScopeEffects(_scope_lifecycle_effects=assembly.callback('_scope_lifecycle_effects')),
    )

    assembly.bind('clear_agent_scope', scope_adapter.clear_agent_scope,
        lambda: scope_adapter.ClearAgentScopeEffects(_scope_lifecycle_effects=assembly.callback('_scope_lifecycle_effects')),
    )

    assembly.bind('validate_worker_workspace_scope', scope_adapter.validate_worker_workspace_scope,
        lambda: scope_adapter.ValidateWorkerWorkspaceScopeEffects(
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            _owned_rebase_state_dir=assembly.callback('_owned_rebase_state_dir'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            validate_active_execplan_metadata=assembly.callback('validate_active_execplan_metadata'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            git=assembly.callback('git'), git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('block_persisted_agent_scope_violation', scope_state_service.block_persisted_agent_scope_violation,
        lambda: scope_state_service.BlockPersistedAgentScopeViolationEffects(
            active_execplan=assembly.callback('active_execplan'),
            block_active_execplan=assembly.callback('block_active_execplan'),
            block_active_execplan_from_revision=assembly.callback('block_active_execplan_from_revision'),
            project_execplan=assembly.callback('project_execplan'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('recover_persisted_agent_worktree', scope_state_service.recover_persisted_agent_worktree,
        lambda: scope_state_service.RecoverPersistedAgentWorktreeEffects(
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            active_execplan=assembly.callback('active_execplan'),
            make_worktree=assembly.callback('make_worktree'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('_proven_recovered_worktree_baseline', scope_state_service._proven_recovered_worktree_baseline,
        lambda: scope_state_service.ProvenRecoveredWorktreeBaselineEffects(_scope_recovery_effects=assembly.callback('_scope_recovery_effects')),
    )

    assembly.bind('validate_persisted_agent_scope', scope_state_service.validate_persisted_agent_scope,
        lambda: scope_state_service.ValidatePersistedAgentScopeEffects(_scope_recovery_effects=assembly.callback('_scope_recovery_effects')),
    )

    assembly.bind('refresh_persisted_agent_scope_after_manager_git', scope_state_service.refresh_persisted_agent_scope_after_manager_git,
        lambda: scope_state_service.RefreshPersistedAgentScopeAfterManagerGitEffects(_scope_recovery_effects=assembly.callback('_scope_recovery_effects')),
    )

    assembly.bind('_remote_adoption_agent_baseline', scope_state_service._remote_adoption_agent_baseline,
        lambda: scope_state_service.RemoteAdoptionAgentBaselineEffects(_scope_recovery_effects=assembly.callback('_scope_recovery_effects')),
    )

    assembly.bind('validate_blocked_agent_scope', scope_state_service.validate_blocked_agent_scope,
        lambda: scope_state_service.ValidateBlockedAgentScopeEffects(_scope_recovery_effects=assembly.callback('_scope_recovery_effects')),
    )
