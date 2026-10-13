"""Own planning dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.planning import agent_ports
from loop_manager_internal.planning import diagnostics
from loop_manager_internal.planning import event_ports
from loop_manager_internal.planning import git_ports
from loop_manager_internal.planning import input_fingerprints
from loop_manager_internal.planning import planner_publication
from loop_manager_internal.planning import planner_validation
from loop_manager_internal.planning import planning_ports
from loop_manager_internal.planning import planning_workspace
from loop_manager_internal.planning import state_ports
from loop_manager_internal.planning import validation_ports


def register(assembly: Composition) -> None:
    assembly.bind('_planning_input_fingerprint', input_fingerprints._planning_input_fingerprint,
        lambda: input_fingerprints.InternalPlanningInputFingerprintEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _is_planning_input=assembly.callback('_is_planning_input'),
            _planning_input_digest=assembly.callback('_planning_input_digest'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_nonplan_input_fingerprint', input_fingerprints._nonplan_input_fingerprint,
        lambda: input_fingerprints.InternalPlanningInputFingerprintEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _is_planning_input=assembly.callback('_is_planning_input'),
            _planning_input_digest=assembly.callback('_nonplan_input_digest'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('planning_input_fingerprint', input_fingerprints.planning_input_fingerprint,
        lambda: input_fingerprints.PlanningInputFingerprintEffects(_planning_input_fingerprint=assembly.callback('_planning_input_fingerprint')),
    )

    assembly.bind('_specification_input_fingerprint', input_fingerprints._specification_input_fingerprint,
        lambda: input_fingerprints.InternalSpecificationInputFingerprintEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _is_specification_input=assembly.callback('_is_specification_input'),
            _specification_input_digest=assembly.callback('_specification_input_digest'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('plan_declarations', input_fingerprints.plan_declarations,
        lambda: input_fingerprints.PlanDeclarationsEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('specification_input_fingerprint', input_fingerprints.specification_input_fingerprint,
        lambda: input_fingerprints.SpecificationInputFingerprintEffects(_specification_input_fingerprint=assembly.callback('_specification_input_fingerprint')),
    )

    assembly.bind('_is_requirement_archive_only_change', input_fingerprints._is_requirement_archive_only_change,
        lambda: input_fingerprints.IsRequirementArchiveOnlyChangeEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _is_planning_input=assembly.callback('_is_planning_input'),
        ),
    )

    assembly.bind('_future_idea_items_from_text', planner_validation._future_idea_items_from_text,
        lambda: planner_validation.FutureIdeaItemsFromTextEffects(_idea_tokens=assembly.callback('_idea_tokens')),
    )

    assembly.bind('_future_idea_items', planner_validation._future_idea_items,
        lambda: planner_validation.FutureIdeaItemsEffects(
            _future_idea_items_from_text=assembly.callback('_future_idea_items_from_text'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_validate_future_idea_separation', planner_validation._validate_future_idea_separation,
        lambda: planner_validation.ValidateFutureIdeaSeparationEffects(
            _contains_token_sequence=assembly.callback('_contains_token_sequence'),
            _future_idea_items=assembly.callback('_future_idea_items'),
            _idea_tokens=assembly.callback('_idea_tokens'),
        ),
    )

    assembly.bind('validate_planner_result', planner_validation.validate_planner_result,
        lambda: planner_validation.ValidatePlannerResultEffects(
            _parse_execplan=assembly.callback('_parse_execplan'),
            _stored_string_list=assembly.callback('_stored_string_list'),
            _validate_future_idea_separation=assembly.callback('_validate_future_idea_separation'),
            changed_files=assembly.callback('changed_files'),
            file_hash=assembly.callback('file_hash'),
            git=assembly.callback('git'),
            load_execplan_catalog=assembly.callback('load_execplan_catalog'),
            mutable_snapshot_token=assembly.callback('mutable_snapshot_token'),
        ),
    )

    assembly.bind('_ready_plan_snapshot_from_contents', planner_validation._ready_plan_snapshot_from_contents,
        lambda: planner_validation.ReadyPlanSnapshotFromContentsEffects(
            _stored_string_list=assembly.callback('_stored_string_list'),
            is_unowned_ready=assembly.callback('is_unowned_ready'),
            published_execplan=assembly.callback('published_execplan'),
        ),
    )

    assembly.bind('_assert_ready_plan_revision', planner_validation._assert_ready_plan_revision,
        lambda: planner_validation.AssertReadyPlanRevisionEffects(
            _git_commit_is_ancestor=assembly.callback('_git_commit_is_ancestor'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_git_path_matches_revision', planner_validation._git_path_matches_revision,
        lambda: planner_validation.GitPathMatchesRevisionEffects(git=assembly.callback('git')),
    )

    assembly.bind('_git_path_has_standard_index_flags', planner_validation._git_path_has_standard_index_flags,
        lambda: planner_validation.GitPathHasStandardIndexFlagsEffects(git_bytes=assembly.callback('git_bytes')),
    )

    assembly.bind('_ready_plan_snapshot', planner_validation._ready_plan_snapshot,
        lambda: planner_validation.ReadyPlanSnapshotEffects(
            _assert_ready_plan_revision=assembly.callback('_assert_ready_plan_revision'),
            _git_path_has_standard_index_flags=assembly.callback('_git_path_has_standard_index_flags'),
            _git_path_matches_revision=assembly.callback('_git_path_matches_revision'),
            _ready_plan_snapshot_from_contents=assembly.callback('_ready_plan_snapshot_from_contents'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_ready_plan_snapshot_at_revision', planner_validation._ready_plan_snapshot_at_revision,
        lambda: planner_validation.ReadyPlanSnapshotAtRevisionEffects(
            _assert_ready_plan_revision=assembly.callback('_assert_ready_plan_revision'),
            _ready_plan_snapshot_from_contents=assembly.callback('_ready_plan_snapshot_from_contents'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_row_is_mutable_ready', planner_validation._row_is_mutable_ready,
        lambda: planner_validation.RowIsMutableReadyEffects(is_unowned_ready=assembly.callback('is_unowned_ready')),
    )

    assembly.bind('mutable_ready_snapshot', planner_validation.mutable_ready_snapshot,
        lambda: planner_validation.MutableReadySnapshotEffects(
            _ensure_execplans_schema=assembly.callback('_ensure_execplans_schema'),
            _ready_plan_snapshot_at_revision=assembly.callback('_ready_plan_snapshot_at_revision'),
            _row_is_mutable_ready=assembly.callback('_row_is_mutable_ready'),
            mutable_snapshot_token=assembly.callback('mutable_snapshot_token'),
            pr_for_branch=assembly.callback('pr_for_branch'),
        ),
    )

    assembly.bind('_assert_mutable_ready_snapshot_current', planner_validation._assert_mutable_ready_snapshot_current,
        lambda: planner_validation.AssertMutableReadySnapshotCurrentEffects(
            _execplan_metadata=assembly.callback('_execplan_metadata'),
            _row_is_mutable_ready=assembly.callback('_row_is_mutable_ready'),
            pr_for_branch=assembly.callback('pr_for_branch'),
        ),
    )

    assembly.bind('_register_execplan_rows', planner_publication._register_execplan_rows,
        lambda: planner_publication.RegisterExecplanRowsEffects(
            _ensure_execplans_schema=assembly.callback('_ensure_execplans_schema'),
            file_hash=assembly.callback('file_hash'),
            git_bytes=assembly.callback('git_bytes'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('register_execplans', planner_publication.register_execplans,
        lambda: planner_publication.RegisterExecplansEffects(_register_execplan_rows=assembly.callback('_register_execplan_rows')),
    )

    assembly.bind('_planning_result_manifest', planner_publication._planning_result_manifest,
        lambda: planner_publication.PlanningResultManifestEffects(snapshot_to_dict=assembly.callback('snapshot_to_dict')),
    )

    assembly.bind('_planning_result_from_manifest', planner_publication._planning_result_from_manifest,
        lambda: planner_publication.PlanningResultFromManifestEffects(
            mutable_snapshot_token=assembly.callback('mutable_snapshot_token'),
            snapshot_from_dict=assembly.callback('snapshot_from_dict'),
        ),
    )

    assembly.bind('_validate_general_planning_commit', planner_publication._validate_general_planning_commit,
        lambda: planner_publication.ValidateGeneralPlanningCommitEffects(
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('record_general_planning_publication_intent', planner_publication.record_general_planning_publication_intent,
        lambda: planner_publication.RecordGeneralPlanningPublicationIntentEffects(
            _assert_mutable_ready_snapshot_current=assembly.callback('_assert_mutable_ready_snapshot_current'),
            _planning_result_manifest=assembly.callback('_planning_result_manifest'),
            _validate_general_planning_commit=assembly.callback('_validate_general_planning_commit'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
        ),
    )

    assembly.bind('_general_planning_batch_already_applied', planner_publication._general_planning_batch_already_applied,
        lambda: planner_publication.GeneralPlanningBatchAlreadyAppliedEffects(
            _execplan_metadata=assembly.callback('_execplan_metadata'),
            file_hash=assembly.callback('file_hash'),
            plan_declarations=assembly.callback('plan_declarations'),
        ),
    )

    assembly.bind('_apply_general_planning_batch', planner_publication._apply_general_planning_batch,
        lambda: planner_publication.ApplyGeneralPlanningBatchEffects(
            _assert_mutable_ready_snapshot_current=assembly.callback('_assert_mutable_ready_snapshot_current'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _general_planning_batch_already_applied=assembly.callback('_general_planning_batch_already_applied'),
            _register_execplan_rows=assembly.callback('_register_execplan_rows'),
            file_hash=assembly.callback('file_hash'),
            general_planning_publication=assembly.callback('general_planning_publication'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            plan_declarations=assembly.callback('plan_declarations'),
        ),
    )

    assembly.bind('publish_general_planning_batch', planner_publication.publish_general_planning_batch,
        lambda: planner_publication.PublishGeneralPlanningBatchEffects(
            _apply_general_planning_batch=assembly.callback('_apply_general_planning_batch'),
            _validate_general_planning_commit=assembly.callback('_validate_general_planning_commit'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('recover_general_planning_batch', planner_publication.recover_general_planning_batch,
        lambda: planner_publication.RecoverGeneralPlanningBatchEffects(
            _apply_general_planning_batch=assembly.callback('_apply_general_planning_batch'),
            _assert_remote_preserves_maintenance_batch=assembly.callback('_assert_remote_preserves_maintenance_batch'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _git_commit_is_ancestor=assembly.callback('_git_commit_is_ancestor'),
            _planning_result_from_manifest=assembly.callback('_planning_result_from_manifest'),
            _validate_general_planning_commit=assembly.callback('_validate_general_planning_commit'),
            general_planning_publication=assembly.callback('general_planning_publication'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('validate_planning_workspace_identity', planning_workspace.validate_planning_workspace_identity,
        lambda: planning_workspace.ValidatePlanningWorkspaceIdentityEffects(_validate_managed_path_boundary=assembly.callback('_validate_managed_path_boundary')),
    )

    assembly.bind('validate_planning_worktree_ownership', planning_workspace.validate_planning_worktree_ownership,
        lambda: planning_workspace.ValidatePlanningWorktreeOwnershipEffects(
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            validate_planning_workspace_identity=assembly.callback('validate_planning_workspace_identity'),
        ),
    )

    assembly.bind('preserve_planning_diagnostics', diagnostics.preserve_planning_diagnostics,
        lambda: diagnostics.PreservePlanningDiagnosticsEffects(
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            git=assembly.callback('git'),
            planning_diagnostics_path=assembly.callback('planning_diagnostics_path'),
        ),
    )

    assembly.bind('_PlanningStatePorts', state_ports._PlanningStatePorts,
        lambda: state_ports.PlanningStatePortsEffects(
            _advance_maintenance_cycle_after_merges=assembly.callback('_advance_maintenance_cycle_after_merges'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            activate_ready_execplan=assembly.callback('activate_ready_execplan'),
            catalog_schedule_decision=assembly.callback('catalog_schedule_decision'),
            clear_active=assembly.callback('clear_active'),
            clear_planning_validation_state=assembly.callback('clear_planning_validation_state'),
            compact_execplan_catalog=assembly.callback('compact_execplan_catalog'),
            general_planning_publication=assembly.callback('general_planning_publication'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            prerequisite_validation_rows=assembly.callback('prerequisite_validation_rows'),
            preserve_planning_diagnostics=assembly.callback('preserve_planning_diagnostics'),
            preserve_remote_planning_diagnostics=assembly.callback('preserve_remote_planning_diagnostics'),
            publish_general_planning_batch=assembly.callback('publish_general_planning_batch'),
            publish_maintenance_planning_batch=assembly.callback('publish_maintenance_planning_batch'),
            record_general_planning_publication_intent=assembly.callback('record_general_planning_publication_intent'),
            record_maintenance_publication_intent=assembly.callback('record_maintenance_publication_intent'),
            recover_general_planning_batch=assembly.callback('recover_general_planning_batch'),
            recover_maintenance_planning_batch=assembly.callback('recover_maintenance_planning_batch'),
            reconcile_execplan_catalog=assembly.callback('reconcile_execplan_catalog'),
            register_execplans=assembly.callback('register_execplans'),
            update_project=assembly.callback('update_project'),
        ),
    )

    assembly.bind('_PlanningGitPorts', git_ports._PlanningGitPorts,
        lambda: git_ports.PlanningGitPortsEffects(
            _capture_local_worktree_state=assembly.callback('_capture_local_worktree_state'),
            _git_operation_entries=assembly.callback('_git_operation_entries'),
            _ignored_worktree_paths=assembly.callback('_ignored_worktree_paths'),
            _is_requirement_archive_only_change=assembly.callback('_is_requirement_archive_only_change'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            _planning_input_fingerprint=assembly.callback('_planning_input_fingerprint'),
            _nonplan_input_fingerprint=assembly.callback('_nonplan_input_fingerprint'),
            _nul_git_paths=assembly.callback('_nul_git_paths'),
            _specification_input_fingerprint=assembly.callback('_specification_input_fingerprint'),
            plan_declarations=assembly.callback('plan_declarations'),
            changed_files=assembly.callback('changed_files'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            make_planning_worktree=assembly.callback('make_planning_worktree'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            validate_planning_workspace_identity=assembly.callback('validate_planning_workspace_identity'),
            validate_planning_worktree_ownership=assembly.callback('validate_planning_worktree_ownership'),
        ),
    )

    assembly.bind('_ExecPlanArchiveGitPort', git_ports._ExecPlanArchiveGitPort,
        lambda: git_ports.ExecPlanArchiveGitPortEffects(_archive_git_effects=assembly.callback('_archive_git_effects')),
    )

    assembly.bind('_PlanningAgentPorts', agent_ports._PlanningAgentPorts,
        lambda: agent_ports.PlanningAgentPortsEffects(
            _agent_profile=assembly.callback('_agent_profile'),
            _agent_git_resources=assembly.callback('_agent_git_resources'),
            _reconciled_manager_scope_metadata=assembly.callback('_reconciled_manager_scope_metadata'),
            begin_agent_scope=assembly.callback('begin_agent_scope'),
            _with_proven_legacy_ignore_config=assembly.callback('_with_proven_legacy_ignore_config'),
            _normalize_legacy_ignored_snapshot=assembly.callback('_normalize_legacy_ignored_snapshot'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            clear_agent_scope=assembly.callback('clear_agent_scope'),
            operation_log=assembly.callback('operation_log'),
            run_agent=assembly.callback('run_agent'),
            run_agent_with_retries=assembly.callback('run_agent_with_retries'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_PlanningValidationPorts', validation_ports._PlanningValidationPorts,
        lambda: validation_ports.PlanningValidationPortsEffects(
            _maintenance_change_context_text=assembly.callback('_maintenance_change_context_text'),
            _maintenance_context_at_revision=assembly.callback('_maintenance_context_at_revision'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            maintenance_planner_prompt=assembly.callback('maintenance_planner_prompt'),
            mutable_ready_snapshot=assembly.callback('mutable_ready_snapshot'),
            planner_prompt=assembly.callback('planner_prompt'),
            prepare_maintenance_planning_context=assembly.callback('prepare_maintenance_planning_context'),
            validate_maintenance_planner_result=assembly.callback('validate_maintenance_planner_result'),
            validate_maintenance_publication_base=assembly.callback('validate_maintenance_publication_base'),
            validate_planner_result=assembly.callback('validate_planner_result'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_PlanningEventPorts', event_ports._PlanningEventPorts,
        lambda: event_ports.PlanningEventPortsEffects(
            operation_log=assembly.callback('operation_log'),
            operation_print=assembly.callback('operation_print'),
        ),
    )

    assembly.bind('_PlanningLifecyclePorts', planning_ports._PlanningLifecyclePorts,
        lambda: planning_ports.PlanningLifecyclePortsEffects(
            _PlanningAgentPorts=assembly.callback('_PlanningAgentPorts'),
            _PlanningEventPorts=assembly.callback('_PlanningEventPorts'),
            _PlanningFileReviewPorts=assembly.callback('_PlanningFileReviewPorts'),
            _PlanningGitPorts=assembly.callback('_PlanningGitPorts'),
            _PlanningReconciliationPorts=assembly.callback('_PlanningReconciliationPorts'),
            _PlanningStatePorts=assembly.callback('_PlanningStatePorts'),
            _PlanningValidationPorts=assembly.callback('_PlanningValidationPorts'),
        ),
    )

    assembly.bind('preserve_remote_planning_diagnostics', diagnostics.preserve_remote_planning_diagnostics,
        lambda: diagnostics.RemotePlanningDiagnosticsEffects(
            preservation_effects=assembly.callback('_diagnostic_preservation_effects'),
            adapter_factory=assembly.callback('_BlockedCheckpointAdapter'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            validate_planning_workspace_identity=assembly.callback('validate_planning_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            git=assembly.callback('git'),
        ),
    )
