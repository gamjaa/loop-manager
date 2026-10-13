"""Own maintenance dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.maintenance import cycle_service
from loop_manager_internal.maintenance import maintenance_publication
from loop_manager_internal.maintenance import maintenance_validation
from loop_manager_internal.maintenance import review_ports


def register(assembly: Composition) -> None:
    assembly.bind('_maintenance_mode_and_rows', maintenance_validation._maintenance_mode_and_rows,
        lambda: maintenance_validation.MaintenanceModeAndRowsEffects(is_unowned_ready=assembly.callback('is_unowned_ready')),
    )

    assembly.bind('prepare_maintenance_planning_context', maintenance_validation.prepare_maintenance_planning_context,
        lambda: maintenance_validation.PrepareMaintenancePlanningContextEffects(
            _maintenance_context=assembly.callback('_maintenance_context'),
            _maintenance_mode_and_rows=assembly.callback('_maintenance_mode_and_rows'),
            _ready_plan_snapshot=assembly.callback('_ready_plan_snapshot'),
            _ready_plan_snapshot_at_revision=assembly.callback('_ready_plan_snapshot_at_revision'),
        ),
    )

    assembly.bind('_maintenance_context_at_revision', maintenance_validation._maintenance_context_at_revision,
        lambda: maintenance_validation.MaintenanceContextAtRevisionEffects(
            _maintenance_context=assembly.callback('_maintenance_context'),
            _maintenance_mode_and_rows=assembly.callback('_maintenance_mode_and_rows'),
            _ready_plan_snapshot_at_revision=assembly.callback('_ready_plan_snapshot_at_revision'),
        ),
    )

    assembly.bind('_assert_maintenance_context_current', maintenance_validation._assert_maintenance_context_current,
        lambda: maintenance_validation.AssertMaintenanceContextCurrentEffects(
            _maintenance_mode_and_rows=assembly.callback('_maintenance_mode_and_rows'),
            _stored_string_list=assembly.callback('_stored_string_list'),
            is_unowned_ready=assembly.callback('is_unowned_ready'),
        ),
    )

    assembly.bind('validate_maintenance_planner_result', maintenance_validation.validate_maintenance_planner_result,
        lambda: maintenance_validation.ValidateMaintenancePlannerResultEffects(
            _assert_maintenance_context_current=assembly.callback('_assert_maintenance_context_current'),
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _parse_execplan=assembly.callback('_parse_execplan'),
            _validate_future_idea_separation=assembly.callback('_validate_future_idea_separation'),
            changed_files=assembly.callback('changed_files'),
            git=assembly.callback('git'),
            load_execplan_snapshot=assembly.callback('load_execplan_snapshot'),
            published_execplan=assembly.callback('published_execplan'),
            terminal_new_plan_ids=assembly.callback('terminal_new_plan_ids'),
            validate_barrier_revision=assembly.callback('validate_barrier_revision'),
            validate_refactoring_plan=assembly.callback('validate_refactoring_plan'),
            validate_revalidation_revision=assembly.callback('validate_revalidation_revision'),
        ),
    )

    assembly.bind('validate_maintenance_publication_base', maintenance_publication.validate_maintenance_publication_base,
        lambda: maintenance_publication.ValidateMaintenancePublicationBaseEffects(
            _assert_maintenance_context_current=assembly.callback('_assert_maintenance_context_current'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_validate_maintenance_planning_commit', maintenance_publication._validate_maintenance_planning_commit,
        lambda: maintenance_publication.ValidateMaintenancePlanningCommitEffects(
            _parse_execplan=assembly.callback('_parse_execplan'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            load_execplan_catalog=assembly.callback('load_execplan_catalog'),
            load_execplan_snapshot=assembly.callback('load_execplan_snapshot'),
        ),
    )

    assembly.bind('_validate_maintenance_publication_commit', maintenance_publication._validate_maintenance_publication_commit,
        lambda: maintenance_publication.ValidateMaintenancePublicationCommitEffects(
            _validate_maintenance_planning_commit=assembly.callback('_validate_maintenance_planning_commit'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_git_commit_is_ancestor', maintenance_publication._git_commit_is_ancestor,
        lambda: maintenance_publication.GitCommitIsAncestorEffects(git=assembly.callback('git')),
    )

    assembly.bind('_assert_remote_preserves_maintenance_batch', maintenance_publication._assert_remote_preserves_maintenance_batch,
        lambda: maintenance_publication.AssertRemotePreservesMaintenanceBatchEffects(
            _git_commit_is_ancestor=assembly.callback('_git_commit_is_ancestor'),
            _git_tree_entries=assembly.callback('_git_tree_entries'),
        ),
    )

    assembly.bind('record_maintenance_publication_intent', maintenance_publication.record_maintenance_publication_intent,
        lambda: maintenance_publication.RecordMaintenancePublicationIntentEffects(
            _assert_maintenance_context_current=assembly.callback('_assert_maintenance_context_current'),
            _validate_maintenance_publication_commit=assembly.callback('_validate_maintenance_publication_commit'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
        ),
    )

    assembly.bind('_maintenance_rows_match_published_batch', maintenance_publication._maintenance_rows_match_published_batch,
        lambda: maintenance_publication.MaintenanceRowsMatchPublishedBatchEffects(
            _execplan_metadata=assembly.callback('_execplan_metadata'),
            file_hash=assembly.callback('file_hash'),
        ),
    )

    assembly.bind('_maintenance_publication_already_applied', maintenance_publication._maintenance_publication_already_applied,
        lambda: maintenance_publication.MaintenancePublicationAlreadyAppliedEffects(
            _maintenance_rows_match_published_batch=assembly.callback('_maintenance_rows_match_published_batch'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
        ),
    )

    assembly.bind('publish_maintenance_planning_batch', maintenance_publication.publish_maintenance_planning_batch,
        lambda: maintenance_publication.PublishMaintenancePlanningBatchEffects(
            _apply_maintenance_planning_batch=assembly.callback('_apply_maintenance_planning_batch'),
            _validate_maintenance_publication_commit=assembly.callback('_validate_maintenance_publication_commit'),
        ),
    )

    assembly.bind('_apply_maintenance_planning_batch', maintenance_publication._apply_maintenance_planning_batch,
        lambda: maintenance_publication.ApplyMaintenancePlanningBatchEffects(
            _assert_maintenance_context_current=assembly.callback('_assert_maintenance_context_current'),
            _assert_maintenance_publication_intent=assembly.callback('_assert_maintenance_publication_intent'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _execplan_metadata=assembly.callback('_execplan_metadata'),
            _maintenance_publication_already_applied=assembly.callback('_maintenance_publication_already_applied'),
            _planning_input_fingerprint=assembly.callback('_planning_input_fingerprint'),
            plan_declarations=assembly.callback('plan_declarations'),
            _register_execplan_rows=assembly.callback('_register_execplan_rows'),
            _specification_input_fingerprint=assembly.callback('_specification_input_fingerprint'),
            file_hash=assembly.callback('file_hash'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
        ),
    )

    assembly.bind('restart_stale_maintenance_publication', maintenance_publication.restart_stale_maintenance_publication,
        lambda: maintenance_publication.RestartStaleMaintenancePublicationEffects(
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _git_commit_is_ancestor=assembly.callback('_git_commit_is_ancestor'),
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('recover_maintenance_planning_batch', maintenance_publication.recover_maintenance_planning_batch,
        lambda: maintenance_publication.RecoverMaintenancePlanningBatchEffects(
            _apply_maintenance_planning_batch=assembly.callback('_apply_maintenance_planning_batch'),
            _assert_remote_preserves_maintenance_batch=assembly.callback('_assert_remote_preserves_maintenance_batch'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _git_commit_is_ancestor=assembly.callback('_git_commit_is_ancestor'),
            _maintenance_context_at_revision=assembly.callback('_maintenance_context_at_revision'),
            _validate_maintenance_planning_commit=assembly.callback('_validate_maintenance_planning_commit'),
            changed_files=assembly.callback('changed_files'),
            git=assembly.callback('git'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            publish_maintenance_planning_batch=assembly.callback('publish_maintenance_planning_batch'),
            restart_stale_maintenance_publication=assembly.callback('restart_stale_maintenance_publication'),
            validate_maintenance_planner_result=assembly.callback('validate_maintenance_planner_result'),
        ),
    )

    assembly.bind('_advance_maintenance_cycle_after_merges', cycle_service._advance_maintenance_cycle_after_merges,
        lambda: cycle_service.AdvanceMaintenanceCycleAfterMergesEffects(
            all_cycle_plans_merged=assembly.callback('all_cycle_plans_merged'),
            cycle_plan_status_error=assembly.callback('cycle_plan_status_error'),
        ),
    )

    assembly.bind('_PlanningReconciliationPorts', review_ports._PlanningReconciliationPorts,
        lambda: review_ports.PlanningReconciliationPortsEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            git_bytes=assembly.callback('git_bytes'),
            parse_frontmatter=assembly.callback('parse_frontmatter'),
        ),
    )
