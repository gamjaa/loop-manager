"""Own runtime dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.runtime import bootstrap_adapter
from loop_manager_internal.runtime import operation_events
from loop_manager_internal.runtime import registration_service


def register(assembly: Composition) -> None:
    assembly.bind('operation_log', operation_events.operation_log,
        lambda: operation_events.OperationLogEffects(local_now=assembly.callback('local_now')),
    )

    assembly.bind('operation_print', operation_events.operation_print,
        lambda: operation_events.OperationPrintEffects(operation_log=assembly.callback('operation_log')),
    )

    assembly.bind('_bootstrap_service', bootstrap_adapter._bootstrap_service,
        lambda: bootstrap_adapter.BootstrapServiceEffects(
            ROOT=assembly.value('ROOT'),
            _git_operation_entries=assembly.callback('_git_operation_entries'),
            _ignored_worktree_paths=assembly.callback('_ignored_worktree_paths'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            _validate_managed_path_boundary=assembly.callback('_validate_managed_path_boundary'),
            _worktree_catalog=assembly.callback('_worktree_catalog'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            github_repository=assembly.callback('github_repository'),
            operation_log=assembly.callback('operation_log'),
            repository_origin=assembly.callback('repository_origin'),
            synchronize_base_worktree=assembly.callback('synchronize_base_worktree'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
        ),
    )

    assembly.bind('_catalog_migration_effects', bootstrap_adapter._catalog_migration_effects,
        lambda: bootstrap_adapter.CatalogMigrationEffectsEffects(
            _LegacyCatalogMergeProof=assembly.callback('_LegacyCatalogMergeProof'),
            _current_execplan_sha=assembly.callback('_current_execplan_sha'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            revision_execplan_snapshot=assembly.callback('revision_execplan_snapshot'),
        ),
    )

    assembly.bind('validate_runtime_worktree', bootstrap_adapter.validate_runtime_worktree,
        lambda: bootstrap_adapter.ValidateRuntimeWorktreeEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('_runtime_catalog_record', bootstrap_adapter._runtime_catalog_record,
        lambda: bootstrap_adapter.RuntimeCatalogRecordEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('_runtime_transition_state', bootstrap_adapter._runtime_transition_state,
        lambda: bootstrap_adapter.RuntimeTransitionStateEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('_managed_worktree_transition_state', bootstrap_adapter._managed_worktree_transition_state,
        lambda: bootstrap_adapter.ManagedWorktreeTransitionStateEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _worktree_catalog=assembly.callback('_worktree_catalog'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_managed_worktree_marker', bootstrap_adapter._managed_worktree_marker,
        lambda: bootstrap_adapter.ManagedWorktreeMarkerEffects(_managed_worktree_receipt=assembly.callback('_managed_worktree_receipt')),
    )

    assembly.bind('_write_managed_worktree_receipt', bootstrap_adapter._write_managed_worktree_receipt,
        lambda: bootstrap_adapter.WriteManagedWorktreeReceiptEffects(_canonical_path=assembly.callback('_canonical_path')),
    )

    assembly.bind('_recover_managed_worktree_removal', bootstrap_adapter._recover_managed_worktree_removal,
        lambda: bootstrap_adapter.RecoverManagedWorktreeRemovalEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            _managed_worktree_marker=assembly.callback('_managed_worktree_marker'),
            _managed_worktree_transition_state=assembly.callback('_managed_worktree_transition_state'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_assess_missing_managed_cleanup', bootstrap_adapter._assess_missing_managed_cleanup,
        lambda: bootstrap_adapter.AssessMissingManagedCleanupEffects(_recover_managed_worktree_removal=assembly.callback('_recover_managed_worktree_removal')),
    )

    assembly.bind('_runtime_transition_marker', bootstrap_adapter._runtime_transition_marker,
        lambda: bootstrap_adapter.RuntimeTransitionMarkerEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('_validate_runtime_lock_origin', bootstrap_adapter._validate_runtime_lock_origin,
        lambda: bootstrap_adapter.ValidateRuntimeLockOriginEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('_validated_runtime_registration_marker', bootstrap_adapter._validated_runtime_registration_marker,
        lambda: bootstrap_adapter.ValidatedRuntimeRegistrationMarkerEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('prepare_runtime_worktree', bootstrap_adapter.prepare_runtime_worktree,
        lambda: bootstrap_adapter.PrepareRuntimeWorktreeEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('remove_runtime_worktree', bootstrap_adapter.remove_runtime_worktree,
        lambda: bootstrap_adapter.RemoveRuntimeWorktreeEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('bootstrap_base_branch', bootstrap_adapter.bootstrap_base_branch,
        lambda: bootstrap_adapter.BootstrapBaseBranchEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('_tracking_ref_oid', bootstrap_adapter._tracking_ref_oid,
        lambda: bootstrap_adapter.TrackingRefOidEffects(git=assembly.callback('git')),
    )

    assembly.bind('run_runtime_child', bootstrap_adapter.run_runtime_child,
        lambda: bootstrap_adapter.RunRuntimeChildEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('validate_runtime_child', bootstrap_adapter.validate_runtime_child,
        lambda: bootstrap_adapter.ValidateRuntimeChildEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('validate_runtime_mutation', bootstrap_adapter.validate_runtime_mutation,
        lambda: bootstrap_adapter.ValidateRuntimeMutationEffects(_bootstrap_service=assembly.callback('_bootstrap_service')),
    )

    assembly.bind('validate_runtime_prerequisites', registration_service.validate_runtime_prerequisites,
        lambda: registration_service.ValidateRuntimePrerequisitesEffects(validate_provider_runtime=assembly.callback('validate_provider_runtime')),
    )

    assembly.bind('repository_origin', registration_service.repository_origin,
        lambda: registration_service.RepositoryOriginEffects(
            git=assembly.callback('git'),
            github_repository=assembly.callback('github_repository'),
        ),
    )

    assembly.bind('validate_repository', registration_service.validate_repository,
        lambda: registration_service.ValidateRepositoryEffects(
            git=assembly.callback('git'),
            repository_origin=assembly.callback('repository_origin'),
            validate_runtime_prerequisites=assembly.callback('validate_runtime_prerequisites'),
        ),
    )

    assembly.bind('remote_base', registration_service.remote_base,
        lambda: registration_service.RemoteBaseEffects(
            _recorded_project_fetch=assembly.callback('_recorded_project_fetch'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_revision_blob', registration_service._revision_blob,
        lambda: registration_service.RevisionBlobEffects(git_bytes=assembly.callback('git_bytes')),
    )

    assembly.bind('inspect_registration_files', registration_service.inspect_registration_files,
        lambda: registration_service.InspectRegistrationFilesEffects(
            ROOT=assembly.value('ROOT'),
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _is_active_requirement=assembly.callback('_is_active_requirement'),
            _revision_blob=assembly.callback('_revision_blob'),
            file_hash=assembly.callback('file_hash'),
            normalized_text=assembly.callback('normalized_text'),
        ),
    )

    assembly.bind('inspect_project_registration', registration_service.inspect_project_registration,
        lambda: registration_service.InspectProjectRegistrationEffects(
            git=assembly.callback('git'),
            inspect_registration_files=assembly.callback('inspect_registration_files'),
            remote_base=assembly.callback('remote_base'),
            validate_repository=assembly.callback('validate_repository'),
        ),
    )

    assembly.bind('add_registered_project', registration_service.add_registered_project,
        lambda: registration_service.AddRegisteredProjectEffects(
            _ensure_execplans_schema=assembly.callback('_ensure_execplans_schema'),
            _merged_execplan_ids=assembly.callback('_merged_execplan_ids'),
            git=assembly.callback('git'),
            inspect_project_registration=assembly.callback('inspect_project_registration'),
            migrate_legacy_execplans=assembly.callback('migrate_legacy_execplans'),
            revision_execplan_snapshot=assembly.callback('revision_execplan_snapshot'),
        ),
    )

    assembly.bind('_git_common_directory', bootstrap_adapter._git_common_directory,
        lambda: bootstrap_adapter.GitCommonDirectoryEffects(
            _canonical_path=assembly.callback('_canonical_path'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('is_loop_manager_repository', bootstrap_adapter.is_loop_manager_repository,
        lambda: bootstrap_adapter.IsLoopManagerRepositoryEffects(
            ROOT=assembly.value('ROOT'),
            _canonical_path=assembly.callback('_canonical_path'),
            _git_common_directory=assembly.callback('_git_common_directory'),
        ),
    )
