"""Own execution dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.execution import execution_ports
from loop_manager_internal.execution import plan_validation
from loop_manager_internal.execution import result_validation


def register(assembly: Composition) -> None:
    assembly.bind('ensure_worker_level_state', result_validation.ensure_worker_level_state,
        lambda: result_validation.EnsureWorkerLevelStateEffects(
            _deserialize_review_level_history=assembly.callback('_deserialize_review_level_history'),
            active_execplan=assembly.callback('active_execplan'),
            worker_level=assembly.callback('worker_level'),
        ),
    )

    assembly.bind('codex_model_efforts', result_validation.codex_model_efforts,
        lambda: result_validation.CodexModelEffortsEffects(_agent_profiles_effects=assembly.callback('_agent_profiles_effects')),
    )

    assembly.bind('verify', result_validation.verify,
        lambda: result_validation.VerifyEffects(
            _check_runtime_handoff=assembly.callback('_check_runtime_handoff'),
            _handoff_popen=assembly.callback('_handoff_popen'),
            run_verify_process=assembly.callback('run_verify_process'),
        ),
    )

    assembly.bind('_valid_review_issue_identifier', result_validation._valid_review_issue_identifier,
        lambda: result_validation.ValidReviewIssueIdentifierEffects(_valid_review_issue_path=assembly.callback('_valid_review_issue_path')),
    )

    assembly.bind('_normalize_review_issue_ids', result_validation._normalize_review_issue_ids,
        lambda: result_validation.NormalizeReviewIssueIdsEffects(_valid_review_issue_identifier=assembly.callback('_valid_review_issue_identifier')),
    )

    assembly.bind('parse_reviewer_result', result_validation.parse_reviewer_result,
        lambda: result_validation.ParseReviewerResultEffects(_normalize_review_issue_ids=assembly.callback('_normalize_review_issue_ids')),
    )

    assembly.bind('_review_issue_paths', result_validation._review_issue_paths,
        lambda: result_validation.ReviewIssuePathsEffects(parse_reviewer_result=assembly.callback('parse_reviewer_result')),
    )

    assembly.bind('normalize_validation_failure', result_validation.normalize_validation_failure,
        lambda: result_validation.NormalizeValidationFailureEffects(
            _normalize_review_issue_ids=assembly.callback('_normalize_review_issue_ids'),
            _normalize_temp_path=assembly.callback('_normalize_temp_path'),
        ),
    )

    assembly.bind('validation_failure_fingerprints', result_validation.validation_failure_fingerprints,
        lambda: result_validation.ValidationFailureFingerprintsEffects(normalize_validation_failure=assembly.callback('normalize_validation_failure')),
    )

    assembly.bind('validation_failure_fingerprint', result_validation.validation_failure_fingerprint,
        lambda: result_validation.ValidationFailureFingerprintEffects(validation_failure_fingerprints=assembly.callback('validation_failure_fingerprints')),
    )

    assembly.bind('clear_validation_failure_history', result_validation.clear_validation_failure_history,
        lambda: result_validation.ClearValidationFailureHistoryEffects(
            _deserialize_failure_history=assembly.callback('_deserialize_failure_history'),
            active_execplan=assembly.callback('active_execplan'),
        ),
    )

    assembly.bind('pr_for_branch', result_validation.pr_for_branch,
        lambda: result_validation.PrForBranchEffects(_github_observation_effects_pr_for_branch=assembly.callback('_github_observation_effects_pr_for_branch')),
    )

    assembly.bind('_worktree_execplan_input_fingerprint', plan_validation._worktree_execplan_input_fingerprint,
        lambda: plan_validation.WorktreeExecplanInputFingerprintEffects(
            _execplan_input_fingerprint=assembly.callback('_execplan_input_fingerprint'),
            _execplan_input_paths=assembly.callback('_execplan_input_paths'),
        ),
    )

    assembly.bind('_revision_execplan_input_fingerprint', plan_validation._revision_execplan_input_fingerprint,
        lambda: plan_validation.RevisionExecplanInputFingerprintEffects(
            _execplan_input_fingerprint=assembly.callback('_execplan_input_fingerprint'),
            _execplan_input_paths=assembly.callback('_execplan_input_paths'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_revision_execplan_source_fingerprint', plan_validation._revision_execplan_source_fingerprint,
        lambda: plan_validation.RevisionExecplanSourceFingerprintEffects(
            _execplan_input_fingerprint=assembly.callback('_execplan_input_fingerprint'),
            git=assembly.callback('git'),
            git_bytes=assembly.callback('git_bytes'),
        ),
    )

    assembly.bind('_revision_execplan_body_hash', plan_validation._revision_execplan_body_hash,
        lambda: plan_validation.RevisionExecplanBodyHashEffects(git=assembly.callback('git')),
    )

    assembly.bind('_revision_changed_paths', plan_validation._revision_changed_paths,
        lambda: plan_validation.RevisionChangedPathsEffects(git=assembly.callback('git')),
    )

    assembly.bind('_revision_review_issue_fingerprint', plan_validation._revision_review_issue_fingerprint,
        lambda: plan_validation.RevisionReviewIssueFingerprintEffects(
            _execplan_input_fingerprint=assembly.callback('_execplan_input_fingerprint'),
            _review_issue_paths=assembly.callback('_review_issue_paths'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_blocked_validation_input_fingerprints', plan_validation._blocked_validation_input_fingerprints,
        lambda: plan_validation.BlockedValidationInputFingerprintsEffects(
            _blocked_validation_base_revision=assembly.callback('_blocked_validation_base_revision'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
            git=assembly.callback('git'),
        ),
    )

    assembly.bind('_execplan_metadata_identity_error', plan_validation._execplan_metadata_identity_error,
        lambda: plan_validation.ExecplanMetadataIdentityErrorEffects(_current_execplan_metadata=assembly.callback('_current_execplan_metadata')),
    )

    assembly.bind('_blocked_execplan_metadata_identity_error', plan_validation._blocked_execplan_metadata_identity_error,
        lambda: plan_validation.BlockedExecplanMetadataIdentityErrorEffects(_execplan_metadata_identity_error=assembly.callback('_execplan_metadata_identity_error')),
    )

    assembly.bind('validate_active_execplan_metadata', plan_validation.validate_active_execplan_metadata,
        lambda: plan_validation.ValidateActiveExecplanMetadataEffects(
            _current_execplan_metadata=assembly.callback('_current_execplan_metadata'),
            _execplan_metadata_identity_error=assembly.callback('_execplan_metadata_identity_error'),
            validate_execplan=assembly.callback('validate_execplan'),
        ),
    )

    assembly.bind('_has_unresolved_targeted_test_failure', result_validation._has_unresolved_targeted_test_failure,
        lambda: result_validation.HasUnresolvedTargetedTestFailureEffects(_targeted_test_failure_matches=assembly.callback('_targeted_test_failure_matches')),
    )

    assembly.bind('parse_worker_result', result_validation.parse_worker_result,
        lambda: result_validation.ParseWorkerResultEffects(
            _has_unresolved_targeted_test_failure=assembly.callback('_has_unresolved_targeted_test_failure'),
            reviewer_targeted_test_reason=assembly.callback('reviewer_targeted_test_reason'),
        ),
    )

    assembly.bind('immutable_github_blob_url', result_validation.immutable_github_blob_url,
        lambda: result_validation.ImmutableGithubBlobUrlEffects(github_repository=assembly.callback('github_repository')),
    )

    assembly.bind('_typed_execution_git', execution_ports._typed_execution_git,
        lambda: execution_ports.TypedExecutionGitEffects(
            git=assembly.callback('git'),
            is_git_lock_failure=assembly.callback('is_git_lock_failure'),
        ),
    )

    assembly.bind('_typed_execution_command', execution_ports._typed_execution_command,
        lambda: execution_ports.TypedExecutionCommandEffects(command=assembly.callback('command')),
    )

    assembly.bind('_ExecutionLifecyclePorts', execution_ports._ExecutionLifecyclePorts,
        lambda: execution_ports.ExecutionLifecyclePortsEffects(
            remote_integration_effects=assembly.callback('_checkpoint_recovery_effects_reconcile_published_blocked_checkpoint'),
            validate_provider_runtime=assembly.callback('validate_provider_runtime'),
            mutable_ready_snapshot=assembly.callback('mutable_ready_snapshot'),
            validate_planner_result=assembly.callback('validate_planner_result'),
            _assert_mutable_ready_snapshot_current=assembly.callback('_assert_mutable_ready_snapshot_current'),
            git_bytes=assembly.callback('git_bytes'),
            _agent_profile=assembly.callback('_agent_profile'),
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _git_operation_entries=assembly.callback('_git_operation_entries'),
            _path_signature=assembly.callback('_path_signature'),
            _plan_id_from_path=assembly.callback('_plan_id_from_path'),
            _published_plan_allows_common_plans=assembly.callback('_published_plan_allows_common_plans'),
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            _revision_changed_paths=assembly.callback('_revision_changed_paths'),
            _specification_input_fingerprint=assembly.callback('_specification_input_fingerprint'),
            plan_declarations=assembly.callback('plan_declarations'),
            _is_requirement_archive_only_change=assembly.callback('_is_requirement_archive_only_change'),
            _revision_execplan_body_hash=assembly.callback('_revision_execplan_body_hash'),
            _revision_execplan_input_fingerprint=assembly.callback('_revision_execplan_input_fingerprint'),
            _revision_execplan_source_fingerprint=assembly.callback('_revision_execplan_source_fingerprint'),
            _run_agent_with_connection=assembly.callback('_run_agent_with_connection'),
            _typed_execution_command=assembly.callback('_typed_execution_command'),
            _typed_execution_git=assembly.callback('_typed_execution_git'),
            _worktree_execplan_input_fingerprint=assembly.callback('_worktree_execplan_input_fingerprint'),
            begin_agent_scope=assembly.callback('begin_agent_scope'),
            block_active_execplan=assembly.callback('block_active_execplan'),
            block_active_execplan_from_revision=assembly.callback('block_active_execplan_from_revision'),
            block_verify_timeout_limit=assembly.callback('block_verify_timeout_limit'),
            branch_has_changes=assembly.callback('branch_has_changes'),
            build_pr_body=assembly.callback('build_pr_body'),
            capture_git_snapshot=assembly.callback('capture_git_snapshot'),
            clear_agent_scope=assembly.callback('clear_agent_scope'),
            clear_validation_failure_history=assembly.callback('clear_validation_failure_history'),
            ensure_worker_level_state=assembly.callback('ensure_worker_level_state'),
            git=assembly.callback('git'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
            make_worktree=assembly.callback('make_worktree'),
            mark_execplan_workspace_owned=assembly.callback('mark_execplan_workspace_owned'),
            operation_log=assembly.callback('operation_log'),
            operation_print=assembly.callback('operation_print'),
            parse_reviewer_result=assembly.callback('parse_reviewer_result'),
            parse_worker_result=assembly.callback('parse_worker_result'),
            pr_for_branch=assembly.callback('pr_for_branch'),
            published_execplan=assembly.callback('published_execplan'),
            push_managed_publication_branch=assembly.callback('push_managed_publication_branch'),
            push_owned_pr_conflict_recovery=assembly.callback('push_owned_pr_conflict_recovery'),
            reconcile_execution_execplan_catalog=assembly.callback('reconcile_execution_execplan_catalog'),
            reconcile_existing_pull_request=assembly.callback('reconcile_existing_pull_request'),
            record_remote_publication_branch=assembly.callback('record_remote_publication_branch'),
            remote_branch_oid=assembly.callback('remote_branch_oid'),
            requirement_archive_candidates=lambda *args, **kwargs: (),
            requirement_archive_move_set=assembly.callback('requirement_archive_move_set'),
            requirement_archive_reconciliation_error=assembly.callback('requirement_archive_reconciliation_error'),
            requirement_archive_snapshot=assembly.callback('requirement_archive_snapshot'),
            requirement_archive_sources=assembly.callback('requirement_archive_sources'),
            requirement_archive_touched_sources=assembly.callback('requirement_archive_touched_sources'),
            reserve_execplan_workspace=assembly.callback('reserve_execplan_workspace'),
            resume_pr_conflict_rebase=assembly.callback('resume_pr_conflict_rebase'),
            reviewer_prompt=assembly.callback('reviewer_prompt'),
            reviewer_targeted_test_reason=assembly.callback('reviewer_targeted_test_reason'),
            run_agent_with_retries=assembly.callback('run_agent_with_retries'),
            transition_validation_failure=assembly.callback('transition_validation_failure'),
            unmet_execplan_dependencies=assembly.callback('unmet_execplan_dependencies'),
            validate_active_execplan_metadata=assembly.callback('validate_active_execplan_metadata'),
            validate_agent_scope=assembly.callback('validate_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
            validate_managed_worktree=assembly.callback('validate_managed_worktree'),
            validate_worker_workspace_scope=assembly.callback('validate_worker_workspace_scope'),
            validation_failure_fingerprints=assembly.callback('validation_failure_fingerprints'),
            verify=assembly.callback('verify'),
            worker_prompt=assembly.callback('worker_prompt'),
        ),
    )
