"""Own catalog dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.catalog import archive_service
from loop_manager_internal.catalog import catalog_service
from loop_manager_internal.catalog import legacy_migration
from loop_manager_internal.catalog import project_selection
from loop_manager_internal.catalog import requirement_archive_service


def register(assembly: Composition) -> None:
    assembly.bind('backfill_plan_shas', catalog_service.backfill_plan_shas,
        lambda: catalog_service.BackfillPlanShasEffects(
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('git_bytes', catalog_service.git_bytes,
        lambda: catalog_service.GitBytesEffects(_command_adapter_effects_git_bytes=assembly.callback('_command_adapter_effects_git_bytes')),
    )

    assembly.bind('_git_tree_entries', catalog_service._git_tree_entries,
        lambda: catalog_service.GitTreeEntriesEffects(git_bytes=assembly.callback('git_bytes')),
    )

    assembly.bind('revision_execplan_snapshot', catalog_service.revision_execplan_snapshot,
        lambda: catalog_service.RevisionExecplanSnapshotEffects(
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_catalog_archive_conflict_ids', catalog_service._catalog_archive_conflict_ids,
        lambda: catalog_service.CatalogArchiveConflictIdsEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _merged_execplan_ids=assembly.callback('_merged_execplan_ids'),
            pr_for_branch=assembly.callback('pr_for_branch'),
        ),
    )

    assembly.bind('_GitCatalogDefinitionPort', catalog_service._GitCatalogDefinitionPort,
        lambda: catalog_service.GitCatalogDefinitionPortEffects(
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            revision_execplan_snapshot=assembly.callback('revision_execplan_snapshot'),
        ),
    )

    assembly.bind('reconcile_execplan_catalog', catalog_service.reconcile_execplan_catalog,
        lambda: catalog_service.ReconcileExecplanCatalogEffects(
            _GitCatalogDefinitionPort=assembly.callback('_GitCatalogDefinitionPort'),
            _catalog_archive_conflict_ids=assembly.callback('_catalog_archive_conflict_ids'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            backfill_plan_shas=assembly.callback('backfill_plan_shas'),
            git=assembly.callback('git'),
            migrate_legacy_execplans=assembly.callback('migrate_legacy_execplans'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            assess_cleanup=assembly.callback('assess_cleanup'),
            remove_cleanup_worktree=assembly.callback('remove_cleanup_worktree'),
            recover_cleanup_worktree=assembly.callback('_recover_managed_worktree_removal'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            git_operation_entries=assembly.callback('_git_operation_entries'),
        ),
    )

    assembly.bind('reconcile_execution_execplan_catalog', catalog_service.reconcile_execution_execplan_catalog,
        lambda: catalog_service.ReconcileExecutionExecplanCatalogEffects(reconcile_execplan_catalog=assembly.callback('reconcile_execplan_catalog')),
    )

    assembly.bind('_is_planning_input', catalog_service._is_planning_input,
        lambda: catalog_service.IsPlanningInputEffects(_is_active_requirement=assembly.callback('_is_active_requirement')),
    )

    assembly.bind('_is_specification_input', catalog_service._is_specification_input,
        lambda: catalog_service.IsSpecificationInputEffects(_is_active_requirement=assembly.callback('_is_active_requirement')),
    )

    assembly.bind('load_execplan_catalog', catalog_service.load_execplan_catalog,
        lambda: catalog_service.LoadExecplanCatalogEffects(load_execplan_snapshot=assembly.callback('load_execplan_snapshot')),
    )

    assembly.bind('validate_execplan', catalog_service.validate_execplan,
        lambda: catalog_service.ValidateExecplanEffects(load_execplan_catalog=assembly.callback('load_execplan_catalog')),
    )

    assembly.bind('_legacy_plan_sha', legacy_migration._legacy_plan_sha,
        lambda: legacy_migration.LegacyPlanShaEffects(git=assembly.callback('git')),
    )

    assembly.bind('_current_execplan_sha', legacy_migration._current_execplan_sha,
        lambda: legacy_migration.CurrentExecplanShaEffects(
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_LegacyCatalogMergeProof', legacy_migration._LegacyCatalogMergeProof,
        lambda: legacy_migration.LegacyCatalogMergeProofEffects(
            _git_commit_is_ancestor=assembly.callback('_git_commit_is_ancestor'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            pr_for_branch=assembly.callback('pr_for_branch'),
        ),
    )

    assembly.bind('_migrate_execplan_catalog', legacy_migration._migrate_execplan_catalog,
        lambda: legacy_migration.MigrateExecplanCatalogEffects(_catalog_migration_effects=assembly.callback('_catalog_migration_effects')),
    )

    assembly.bind('migrate_legacy_execplans', legacy_migration.migrate_legacy_execplans,
        lambda: legacy_migration.MigrateLegacyExecplansEffects(
            _ensure_execplans_schema=assembly.callback('_ensure_execplans_schema'),
            _migrate_execplan_catalog=assembly.callback('_migrate_execplan_catalog'),
        ),
    )

    assembly.bind('active_execplan', legacy_migration.active_execplan,
        lambda: legacy_migration.ActiveExecplanEffects(_ensure_execplans_schema=assembly.callback('_ensure_execplans_schema')),
    )

    assembly.bind('plan_blocked_execplans', project_selection.plan_blocked_execplans,
        lambda: project_selection.PlanBlockedExecplansEffects(_ensure_execplans_schema=assembly.callback('_ensure_execplans_schema')),
    )

    assembly.bind('merge_wait_execplans', project_selection.merge_wait_execplans,
        lambda: project_selection.MergeWaitExecplansEffects(_ensure_execplans_schema=assembly.callback('_ensure_execplans_schema')),
    )

    assembly.bind('compact_execplan_catalog', project_selection.compact_execplan_catalog,
        lambda: project_selection.CompactExecplanCatalogEffects(_ensure_execplans_schema=assembly.callback('_ensure_execplans_schema')),
    )

    assembly.bind('prerequisite_validation_rows', project_selection.prerequisite_validation_rows,
        lambda: project_selection.PrerequisiteValidationRowsEffects(_ensure_execplans_schema=assembly.callback('_ensure_execplans_schema')),
    )

    assembly.bind('catalog_schedule_decision', project_selection.catalog_schedule_decision,
        lambda: project_selection.CatalogScheduleDecisionEffects(
            _ensure_execplans_schema=assembly.callback('_ensure_execplans_schema'),
            active_execplan=assembly.callback('active_execplan'),
            assessment_can_yield=assembly.callback('assessment_can_yield'),
            is_unowned_ready=assembly.callback('is_unowned_ready'),
            select_cycle_ready_execplan=assembly.callback('select_cycle_ready_execplan'),
        ),
    )

    assembly.bind('select_ready_execplan', project_selection.select_ready_execplan,
        lambda: project_selection.SelectReadyExecplanEffects(
            operation_log=assembly.callback('operation_log'),
            catalog_schedule_decision=assembly.callback('catalog_schedule_decision'),
        ),
    )

    assembly.bind('published_execplan', requirement_archive_service.published_execplan,
        lambda: requirement_archive_service.PublishedExecplanEffects(parse_execplan_text=lambda *args, **kwargs: execplan_catalog.parse_execplan_text(*args, **kwargs)),
    )

    assembly.bind('_requirement_archive_tree', requirement_archive_service._requirement_archive_tree,
        lambda: requirement_archive_service.RequirementArchiveTreeEffects(_git_tree_entries=assembly.callback('_git_tree_entries')),
    )

    assembly.bind('requirement_archive_sources', requirement_archive_service.requirement_archive_sources,
        lambda: requirement_archive_service.RequirementArchiveSourcesEffects(_stored_requirement_sources=assembly.callback('_stored_requirement_sources')),
    )

    assembly.bind('_later_published_execplan', requirement_archive_service._later_published_execplan,
        lambda: requirement_archive_service.LaterPublishedExecplanEffects(
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            published_execplan=assembly.callback('published_execplan'),
        ),
    )

    assembly.bind('_revision_execplan_catalog', requirement_archive_service._revision_execplan_catalog,
        lambda: requirement_archive_service.RevisionExecplanCatalogEffects(revision_execplan_snapshot=assembly.callback('revision_execplan_snapshot')),
    )

    assembly.bind('requirement_archive_snapshot', requirement_archive_service.requirement_archive_snapshot,
        lambda: requirement_archive_service.RequirementArchiveSnapshotEffects(
            _is_active_requirement=assembly.callback('_is_active_requirement'),
            _later_published_execplan=assembly.callback('_later_published_execplan'),
            _requirement_archive_tree=assembly.callback('_requirement_archive_tree'),
            _revision_execplan_catalog=assembly.callback('_revision_execplan_catalog'),
            _validate_dependency_graph=assembly.callback('_validate_dependency_graph'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('requirement_archive_candidates', requirement_archive_service.requirement_archive_candidates,
        lambda: requirement_archive_service.RequirementArchiveCandidatesEffects(
            requirement_archive_snapshot=assembly.callback('requirement_archive_snapshot'),
            requirement_archive_sources=assembly.callback('requirement_archive_sources'),
        ),
    )

    assembly.bind('requirement_archive_touched_sources', requirement_archive_service.requirement_archive_touched_sources,
        lambda: requirement_archive_service.RequirementArchiveTouchedSourcesEffects(
            _is_active_requirement=assembly.callback('_is_active_requirement'),
            _requirement_archive_tree=assembly.callback('_requirement_archive_tree'),
            requirement_archive_path=assembly.callback('requirement_archive_path'),
        ),
    )

    assembly.bind('_requirement_archive_blob', requirement_archive_service._requirement_archive_blob,
        lambda: requirement_archive_service.RequirementArchiveBlobEffects(git_bytes=assembly.callback('git_bytes')),
    )

    assembly.bind('requirement_archive_move_set', requirement_archive_service.requirement_archive_move_set,
        lambda: requirement_archive_service.RequirementArchiveMoveSetEffects(
            _requirement_archive_blob=assembly.callback('_requirement_archive_blob'),
            _requirement_archive_tree=assembly.callback('_requirement_archive_tree'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('dispatch_execplan_archive_housekeeping', archive_service.dispatch_execplan_archive_housekeeping,
        lambda: archive_service.DispatchExecplanArchiveHousekeepingEffects(
            _ExecPlanArchiveGitPort=assembly.callback('_ExecPlanArchiveGitPort'),
            git=assembly.callback('git'),
            operation_print=assembly.callback('operation_print'),
            reconcile_execplan_catalog=assembly.callback('reconcile_execplan_catalog'),
        ),
    )

    assembly.bind('_execplan_archive_housekeeping_safe', archive_service._execplan_archive_housekeeping_safe,
        lambda: archive_service.ExecplanArchiveHousekeepingSafeEffects(
            active_execplan=assembly.callback('active_execplan'),
            catalog_schedule_decision=assembly.callback('catalog_schedule_decision'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
        ),
    )
