"""Own application dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.application import commands, manual_work
from loop_manager_internal.planning import manual_plan_generation
from loop_manager_internal.application import project_outcomes
from loop_manager_internal.application import project_runner
from loop_manager_internal.application import project_service
from loop_manager_internal.application import scheduler
from loop_manager_internal.application import self_runtime


def register(assembly: Composition) -> None:
    assembly.bind('_reset_deleted_branch', commands.reset_deleted_branch,
        lambda: commands.ResetDeletedBranchEffects(
            connect_database=assembly.callback('connect_database'),
            remote_integration_effects=assembly.callback('_checkpoint_recovery_effects_reconcile_published_blocked_checkpoint'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            complete_merged_execplan_worktree=assembly.callback('complete_merged_execplan_worktree'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
            operation_print=assembly.callback('operation_print')))
    assembly.bind('_run_project_step', project_service._run_project_step,
        lambda: project_service.RunProjectStepEffects(
            resume_manual_work=assembly.callback('_resume_manual_work'),
            ExecutionPhaseCoordinator=assembly.callback('ExecutionPhaseCoordinator'),
            ManagerGitCheckpoint=assembly.value('ManagerGitCheckpoint'),
            _ExecutionLifecyclePorts=assembly.callback('_ExecutionLifecyclePorts'),
            _PlanningLifecyclePorts=assembly.callback('_PlanningLifecyclePorts'),
            _canonical_path=assembly.callback('_canonical_path'),
            _check_runtime_handoff=assembly.callback('_check_runtime_handoff'),
            _clear_active_in_current_transaction=assembly.callback('_clear_active_in_current_transaction'),
            _ensure_cleanup_pending_schema=assembly.callback('_ensure_cleanup_pending_schema'),
            _ensure_execplans_schema=assembly.callback('_ensure_execplans_schema'),
            _execplan_archive_housekeeping_safe=assembly.callback('_execplan_archive_housekeeping_safe'),
            _execution_outcome_step_result=assembly.callback('_execution_outcome_step_result'),
            _handoff_observed_self_merge=assembly.callback('_handoff_observed_self_merge'),
            _journaled_manager_base_fetch=assembly.callback('_journaled_manager_base_fetch'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _planning_outcome_step_result=assembly.callback('_planning_outcome_step_result'),
            _project_blocked_recovery_effects=assembly.callback('_project_blocked_recovery_effects'),
            _project_pr_merge_confirmed=assembly.callback('_project_pr_merge_confirmed'),
            _publish_direct_rebase_checkpoint=assembly.callback('_publish_direct_rebase_checkpoint'),
            _record_manager_base_fetch_intent=assembly.callback('_record_manager_base_fetch_intent'),
            _record_project_recovery=assembly.callback('_record_project_recovery'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            _self_runtime_project=assembly.callback('_self_runtime_project'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            active_execplan=assembly.callback('active_execplan'),
            activate_ready_execplan=assembly.callback('activate_ready_execplan'),
            backfill_plan_shas=assembly.callback('backfill_plan_shas'),
            block_active_execplan_from_revision=assembly.callback('block_active_execplan_from_revision'),
            block_persisted_agent_scope_violation=assembly.callback('block_persisted_agent_scope_violation'),
            dispatch_execplan_archive_housekeeping=assembly.callback('dispatch_execplan_archive_housekeeping'),
            ensure_safe_base_worktree=assembly.callback('ensure_safe_base_worktree'),
            git=assembly.callback('git'),
            hydrate_active_project=assembly.callback('hydrate_active_project'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            log_base_worktree_sync=assembly.callback('log_base_worktree_sync'),
            migrate_legacy_execplans=assembly.callback('migrate_legacy_execplans'),
            observe_blocked_plan_remotes=assembly.callback('observe_blocked_plan_remotes'),
            observe_waiting_pull_requests=assembly.callback('observe_waiting_pull_requests'),
            operation_print=assembly.callback('operation_print'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            recheck_cleanup_pending=assembly.callback('recheck_cleanup_pending'),
            reconcile_execplan_catalog=assembly.callback('reconcile_execplan_catalog'),
            reconcile_existing_pull_request=assembly.callback('reconcile_existing_pull_request'),
            reconcile_false_positive_runtime_block=assembly.callback('reconcile_false_positive_runtime_block'),
            refresh_persisted_agent_scope_after_manager_git=assembly.callback('refresh_persisted_agent_scope_after_manager_git'),
            remote_base=assembly.callback('remote_base'),
            resume_interrupted_blocked_agent_recovery=assembly.callback('resume_interrupted_blocked_agent_recovery'),
            resume_plan_blocks=assembly.callback('resume_plan_blocks'),
            resume_project_block_if_safe=assembly.callback('resume_project_block_if_safe'),
            synchronize_waiting_pull_requests=assembly.callback('synchronize_waiting_pull_requests'),
            update_project=assembly.callback('update_project'),
            validate_persisted_agent_scope=assembly.callback('validate_persisted_agent_scope'),
            validate_repository=assembly.callback('validate_repository'),
        ),
    )

    assembly.bind('_record_project_recovery', project_outcomes._record_project_recovery,
        lambda: project_outcomes.RecordProjectRecoveryEffects(project_execplan=assembly.callback('project_execplan')),
    )

    assembly.bind('run_project_result', project_runner.run_project_result,
        lambda: project_runner.RunProjectResultEffects(
            _blocked_recovery_subjects=assembly.callback('_blocked_recovery_subjects'),
            _run_project_result=assembly.callback('_run_project_result'),
            prepare_diagnostic_cleanup=assembly.callback('prepare_diagnostic_cleanup'),
        ),
    )

    assembly.bind('_stable_work_reason', project_outcomes._stable_work_reason,
        lambda: project_outcomes.StableWorkReasonEffects(
            _nonplan_input_fingerprint=assembly.callback('_nonplan_input_fingerprint'),
            _git_tree_entries=assembly.callback('_git_tree_entries'),
            _is_active_requirement=assembly.callback('_is_active_requirement'),
            _is_requirement_archive_only_change=assembly.callback('_is_requirement_archive_only_change'),
            _planning_input_fingerprint=assembly.callback('_planning_input_fingerprint'),
            _specification_input_fingerprint=assembly.callback('_specification_input_fingerprint'),
            plan_declarations=assembly.callback('plan_declarations'),
            catalog_schedule_decision=assembly.callback('catalog_schedule_decision'),
            git=assembly.callback('git'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_run_project_result', project_runner._run_project_result,
        lambda: project_runner.InternalRunProjectResultEffects(
            _checkpoint_dispatch_ready=assembly.callback('_checkpoint_dispatch_ready'),
            _record_project_recovery=assembly.callback('_record_project_recovery'),
            _run_project_step=assembly.callback('_run_project_step'),
            _stable_work_reason=assembly.callback('_stable_work_reason'),
            catalog_schedule_decision=assembly.callback('catalog_schedule_decision'),
            dispatch_blocked_checkpoints=assembly.callback('dispatch_blocked_checkpoints'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            operation_log=assembly.callback('operation_log'),
        ),
    )

    assembly.bind('run_project', project_runner.run_project,
        lambda: project_runner.RunProjectEffects(run_project_result=assembly.callback('run_project_result')),
    )

    assembly.bind('_run_registered_project', scheduler._run_registered_project,
        lambda: scheduler.RunRegisteredProjectEffects(
            _check_runtime_handoff=assembly.callback('_check_runtime_handoff'),
            connect_database=assembly.callback('connect_database'),
            operation_log=assembly.callback('operation_log'),
            run_project_result=assembly.callback('run_project_result'),
        ),
    )

    assembly.bind('_registered_self_runtime_project', self_runtime._registered_self_runtime_project,
        lambda: self_runtime.RegisteredSelfRuntimeProjectEffects(
            ROOT=assembly.value('ROOT'),
            _canonical_path=assembly.callback('_canonical_path'),
            _git_common_directory=assembly.callback('_git_common_directory'),
            bootstrap_base_branch=assembly.callback('bootstrap_base_branch'),
            git=assembly.callback('git'),
            repository_origin=assembly.callback('repository_origin'),
        ),
    )

    assembly.bind('_self_runtime_project', self_runtime._self_runtime_project,
        lambda: self_runtime.SelfRuntimeProjectEffects(_registered_self_runtime_project=assembly.callback('_registered_self_runtime_project')),
    )

    assembly.bind('_finish_self_merge_handoff', self_runtime._finish_self_merge_handoff,
        lambda: self_runtime.FinishSelfMergeHandoffEffects(
            _registered_self_runtime_project=assembly.callback('_registered_self_runtime_project'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            operation_log=assembly.callback('operation_log'),
            repository_origin=assembly.callback('repository_origin'),
        ),
    )

    assembly.bind('_preflight_self_merge_handoff', self_runtime._preflight_self_merge_handoff,
        lambda: self_runtime.PreflightSelfMergeHandoffEffects(
            _finish_self_merge_handoff=assembly.callback('_finish_self_merge_handoff'),
            _registered_self_runtime_project=assembly.callback('_registered_self_runtime_project'),
        ),
    )

    assembly.bind('_handoff_observed_self_merge', self_runtime._handoff_observed_self_merge,
        lambda: self_runtime.HandoffObservedSelfMergeEffects(
            _finish_self_merge_handoff=assembly.callback('_finish_self_merge_handoff'),
            _self_runtime_project=assembly.callback('_self_runtime_project'),
        ),
    )

    assembly.bind('schedule_registered_projects', scheduler.schedule_registered_projects,
        lambda: scheduler.ScheduleRegisteredProjectsEffects(
            connect_database=assembly.callback('connect_database'),
            catalog_schedule_decision=assembly.callback('catalog_schedule_decision'),
            _preflight_self_merge_handoff=assembly.callback('_preflight_self_merge_handoff'),
            _run_registered_project=assembly.callback('_run_registered_project'),
        ),
    )

    assembly.bind('run_registered_projects', scheduler.run_registered_projects,
        lambda: scheduler.RunRegisteredProjectsEffects(
            operation_log=assembly.callback('operation_log'),
            schedule_registered_projects=assembly.callback('schedule_registered_projects'),
        ),
    )

    assembly.bind('configure_registry', commands.configure_registry,
        lambda: commands.ConfigureRegistryEffects(
            add_registered_project=assembly.callback('add_registered_project'),
            connect_database=assembly.callback('connect_database'),
            load_managed_config=assembly.callback('load_managed_config'),
            operation_print=assembly.callback('operation_print'),
        ),
    )

    assembly.bind('_document_drafts_command', commands._document_drafts_command,
        lambda: commands.DocumentDraftsCommandEffects(
            ROOT=assembly.value('ROOT'),
            _coerce_agent_result=assembly.callback('_coerce_agent_result'),
            _recorded_base_fetch=assembly.callback('_recorded_base_fetch'),
            connect_database=assembly.callback('connect_database'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            operation_print=assembly.callback('operation_print'),
            run_agent=assembly.callback('run_agent'),
        ),
    )

    assembly.bind('_run_manual_project', manual_work.run_manual_project,
        lambda: manual_work.ManualProjectDriverEffects(
            load_managed_config=assembly.callback('load_managed_config'),
            run_project_result=assembly.callback('run_project_result'),
        ),
    )

    assembly.factory('_manual_work_effects',
        lambda: manual_work.ManualWorkEffects(
            connect_database=assembly.callback('connect_database'),
            validate_repository=assembly.callback('validate_repository'),
            fetch_base=assembly.callback('_recorded_base_fetch'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
            make_worktree=assembly.callback('make_worktree'),
            validate_workspace=assembly.callback('validate_managed_worktree'),
            capture_snapshot=assembly.callback('capture_git_snapshot'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            generate_plans=lambda *a, **kw: manual_plan_generation.generate(
                manual_plan_generation.ManualGenerationEffects(
                    git=assembly.callback('git'),
                    run_agent=assembly.callback('run_agent'),
                    prepare_worktree=assembly.callback('prepare_runtime_worktree'),
                    remove_worktree=assembly.callback('remove_runtime_worktree'),
                    compact_execplan_catalog=assembly.callback('compact_execplan_catalog'),
                ), *a, **kw),
            run_project=assembly.callback('_run_manual_project'),
            operation_print=assembly.callback('operation_print'),
            resolve_profile=assembly.callback('_agent_profile'),
        ),
    )

    assembly.bind('_manual_command', manual_work.manual_command,
        assembly.callback('_manual_work_effects'),
    )

    assembly.bind('_resume_manual_work', manual_work.resume_manual_work,
        assembly.callback('_manual_work_effects'),
    )

    assembly.bind('execute_runtime_command', commands.execute_runtime_command,
        lambda: commands.ExecuteRuntimeCommandEffects(
            reset_command=assembly.callback('_reset_deleted_branch'),
            _document_drafts_command=assembly.callback('_document_drafts_command'),
            configure_registry=assembly.callback('configure_registry'),
            connect_database=assembly.callback('connect_database'),
            load_managed_config=assembly.callback('load_managed_config'),
            operation_print=assembly.callback('operation_print'),
            run_registered_projects=assembly.callback('run_registered_projects'),
            manual_command=assembly.callback('_manual_command'),
            run_project_result=assembly.callback('run_project_result'),
        ),
    )
