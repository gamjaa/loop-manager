"""recovery recovery adapter boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents import scope_observation
from loop_manager_internal.application.contracts import MAX_REVIEWER_PROTOCOL_ATTEMPTS
from loop_manager_internal.application.contracts import ReviewerProtocolError
from loop_manager_internal.application.contracts import WorkerResultError
from loop_manager_internal.git import managed_rebase_runtime
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import checkpoint_recovery
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.recovery import pull_request_recovery
from loop_manager_internal.recovery import scope_reconciliation
from loop_manager_internal.recovery.checkpoint_adapter import _BlockedCheckpointResults
import subprocess


@dataclass(frozen=True)
class PullRequestRecoveryEffectsReconcileExistingPullRequestEffects:
    """Named effects for _pull_request_recovery_effects_reconcile_existing_pull_request; no entry-point/module access."""
    _block_closed_pull_request: Callable[..., object]
    _fetch_pr_conflict_recovery_refs: Callable[..., object]
    _is_pr_conflict_preflight_block: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _record_project_recovery: Callable[..., object]
    _resume_reopened_pull_request: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    _validate_active_pr_conflict_recovery: Callable[..., object]
    _validate_blocked_pr_without_conflict: Callable[..., object]
    _validate_pr_conflict_recovery_identity: Callable[..., object]
    _validate_pr_conflict_recovery_ownership: Callable[..., object]
    block_active_execplan_from_revision: Callable[..., object]
    classify_pull_request_mergeability: Callable[..., object]
    complete_merged_execplan_worktree: Callable[..., object]
    make_worktree: Callable[..., object]
    operation_print: Callable[..., object]
    project_execplan: Callable[..., object]
    recover_confirmed_pr_conflict_push: Callable[..., object]
    remote_base: Callable[..., object]
    update_project: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    manual_publication_stage_tokens: Callable[..., object]
    remote_integration_effects: Callable[..., object] | None = None


def _pull_request_recovery_effects_reconcile_existing_pull_request(effects: PullRequestRecoveryEffectsReconcileExistingPullRequestEffects, ) -> pull_request_recovery.ReconcileExistingPullRequestEffects:
    return pull_request_recovery.ReconcileExistingPullRequestEffects(
        _block_closed_pull_request=effects._block_closed_pull_request,
        _fetch_pr_conflict_recovery_refs=effects._fetch_pr_conflict_recovery_refs,
        _is_pr_conflict_preflight_block=effects._is_pr_conflict_preflight_block,
        _plan_id_from_path=effects._plan_id_from_path,
        _record_project_recovery=effects._record_project_recovery,
        _resume_reopened_pull_request=effects._resume_reopened_pull_request,
        _update_blocked_error=effects._update_blocked_error,
        _validate_active_pr_conflict_recovery=effects._validate_active_pr_conflict_recovery,
        _validate_blocked_pr_without_conflict=effects._validate_blocked_pr_without_conflict,
        _validate_pr_conflict_recovery_identity=effects._validate_pr_conflict_recovery_identity,
        _validate_pr_conflict_recovery_ownership=effects._validate_pr_conflict_recovery_ownership,
        block_active_execplan_from_revision=effects.block_active_execplan_from_revision,
        classify_pull_request_mergeability=effects.classify_pull_request_mergeability,
        complete_merged_execplan_worktree=effects.complete_merged_execplan_worktree,
        make_worktree=effects.make_worktree,
        operation_print=effects.operation_print,
        project_execplan=effects.project_execplan,
        recover_confirmed_pr_conflict_push=effects.recover_confirmed_pr_conflict_push,
        remote_base=effects.remote_base,
        update_project=effects.update_project,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        manual_publication_stage_tokens=effects.manual_publication_stage_tokens,
        remote_integration_effects=effects.remote_integration_effects,
    )


@dataclass(frozen=True)
class PullRequestRecoveryEffectsResumePrConflictRebaseEffects:
    """Named effects for _pull_request_recovery_effects_resume_pr_conflict_rebase; no entry-point/module access."""
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _finish_owned_direct_rebase: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    active_execplan: Callable[..., object]
    block_active_execplan: Callable[..., object]
    git: Callable[..., object]
    is_git_lock_failure: Callable[..., object]
    operation_print: Callable[..., object]
    update_project: Callable[..., object]


def _pull_request_recovery_effects_resume_pr_conflict_rebase(effects: PullRequestRecoveryEffectsResumePrConflictRebaseEffects, ) -> pull_request_recovery.ResumePrConflictRebaseEffects:
    return pull_request_recovery.ResumePrConflictRebaseEffects(
        _complete_direct_rebase_checkpoint=effects._complete_direct_rebase_checkpoint,
        _current_rebase_service=effects._current_rebase_service,
        _finish_owned_direct_rebase=effects._finish_owned_direct_rebase,
        _journaled_direct_rebase=effects._journaled_direct_rebase,
        _owned_rebase_state_dir=effects._owned_rebase_state_dir,
        _rebase_in_progress_for_branch=effects._rebase_in_progress_for_branch,
        active_execplan=effects.active_execplan,
        block_active_execplan=effects.block_active_execplan,
        git=effects.git,
        is_git_lock_failure=effects.is_git_lock_failure,
        operation_print=effects.operation_print,
        update_project=effects.update_project,
    )


@dataclass(frozen=True)
class ScopeReconciliationEffectsResumeProjectBlockIfSafeEffects:
    """Named effects for _scope_reconciliation_effects_resume_project_block_if_safe; no entry-point/module access."""
    _agent_git_resources: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _clear_project_block_without_losing_planning_error: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _reconciled_manager_scope_metadata: Callable[..., object]
    _requeue_blocked_execplan: Callable[..., object]
    _resume_owned_checkpoint_project_block: Callable[..., object]
    _with_proven_legacy_ignore_config: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_planning_worktree_ownership: Callable[..., object]


def _scope_reconciliation_effects_resume_project_block_if_safe(effects: ScopeReconciliationEffectsResumeProjectBlockIfSafeEffects, ) -> scope_reconciliation.ResumeProjectBlockIfSafeEffects:
    return scope_reconciliation.ResumeProjectBlockIfSafeEffects(
        _agent_git_resources=effects._agent_git_resources,
        _clear_active_in_current_transaction=effects._clear_active_in_current_transaction,
        _clear_project_block_without_losing_planning_error=effects._clear_project_block_without_losing_planning_error,
        _reconcile_rebase_evidence_block=effects._reconcile_rebase_evidence_block,
        _reconciled_manager_scope_metadata=effects._reconciled_manager_scope_metadata,
        _requeue_blocked_execplan=effects._requeue_blocked_execplan,
        _resume_owned_checkpoint_project_block=effects._resume_owned_checkpoint_project_block,
        _with_proven_legacy_ignore_config=effects._with_proven_legacy_ignore_config,
        _normalize_legacy_ignored_snapshot=effects._normalize_legacy_ignored_snapshot,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        hydrate_execplan_project=effects.hydrate_execplan_project,
        validate_agent_scope=effects.validate_agent_scope,
        validate_blocked_agent_scope=effects.validate_blocked_agent_scope,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
        validate_planning_worktree_ownership=effects.validate_planning_worktree_ownership,
    )


@dataclass(frozen=True)
class ScopeReconciliationEffectsResumePlanBlocksEffects:
    """Named effects for _scope_reconciliation_effects_resume_plan_blocks; no entry-point/module access."""
    ROOT: Path
    _blocked_plan_reconciliation_effects: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _reconcile_published_blocked_checkpoint: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _resume_interrupted_blocked_rebase: Callable[..., object]
    _resume_legacy_review_count_block: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    active_execplan: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    load_managed_config: Callable[..., object]
    plan_blocked_execplans: Callable[..., object]
    project_execplan: Callable[..., object]
    resume_blocked_execplan_if_inputs_changed: Callable[..., object]
    resume_retired_guidance_block: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def _scope_reconciliation_effects_resume_plan_blocks(effects: ScopeReconciliationEffectsResumePlanBlocksEffects, ) -> scope_reconciliation.ResumePlanBlocksEffects:
    return scope_reconciliation.ResumePlanBlocksEffects(
        ROOT=effects.ROOT,
        load_managed_config=effects.load_managed_config,
        plan_recovery_effects=effects._blocked_plan_reconciliation_effects,
        _clear_active_in_current_transaction=effects._clear_active_in_current_transaction,
        _reconcile_published_blocked_checkpoint=effects._reconcile_published_blocked_checkpoint,
        _reconcile_rebase_evidence_block=effects._reconcile_rebase_evidence_block,
        _resume_interrupted_blocked_rebase=effects._resume_interrupted_blocked_rebase,
        _resume_legacy_review_count_block=effects._resume_legacy_review_count_block,
        _update_blocked_error=effects._update_blocked_error,
        active_execplan=effects.active_execplan,
        hydrate_execplan_project=effects.hydrate_execplan_project,
        plan_blocked_execplans=effects.plan_blocked_execplans,
        project_execplan=effects.project_execplan,
        resume_blocked_execplan_if_inputs_changed=effects.resume_blocked_execplan_if_inputs_changed,
        resume_retired_guidance_block=effects.resume_retired_guidance_block,
        unmet_execplan_dependencies=effects.unmet_execplan_dependencies,
        validate_blocked_agent_scope=effects.validate_blocked_agent_scope,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
    )


@dataclass(frozen=True)
class ManagedRebaseRuntimeEffectsEffects:
    """Named effects for _managed_rebase_runtime_effects; no entry-point/module access."""
    _owned_rebase_metadata: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
    _path_signature: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _worker_path_allowed: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]
    requirement_archive_candidates: Callable[..., object]
    requirement_archive_move_set: Callable[..., object]
    requirement_archive_path: Callable[..., object]
    validate_active_execplan_metadata: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _managed_rebase_runtime_effects(effects: ManagedRebaseRuntimeEffectsEffects, ) -> managed_rebase_runtime.RebaseRuntimeEffects:
    return managed_rebase_runtime.RebaseRuntimeEffects(
        _owned_rebase_metadata=effects._owned_rebase_metadata,
        _normalize_legacy_ignored_snapshot=effects._normalize_legacy_ignored_snapshot,
        _path_signature=effects._path_signature,
        _published_plan_allows_common_plans=effects._published_plan_allows_common_plans,
        _revision_execplan_input_fingerprint=effects._revision_execplan_input_fingerprint,
        _worker_path_allowed=effects._worker_path_allowed,
        _worktree_execplan_input_fingerprint=effects._worktree_execplan_input_fingerprint,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        live_remote_branch_oid=lambda git_effect, repo_path, branch: manager_git_journal.live_remote_branch_oid(git_effect, repo_path, branch),
        pr_for_branch=effects.pr_for_branch,
        requirement_archive_candidates=effects.requirement_archive_candidates,
        requirement_archive_move_set=effects.requirement_archive_move_set,
        requirement_archive_path=effects.requirement_archive_path,
        validate_active_execplan_metadata=effects.validate_active_execplan_metadata,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class BlockedPlanReconciliationEffectsEffects:
    """Named effects for _blocked_plan_reconciliation_effects; no entry-point/module access."""
    ROOT: Path
    _BlockedCheckpointAdapter: Callable[..., object]
    _blocked_execplan_metadata_error: Callable[..., object]
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _evaluate_remote_checkpoint_cause: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    _non_default_index_flags: Callable[..., object]
    _reconcile_published_blocked_checkpoint: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    _worktree_state_signature: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., bytes]
    github_repository: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    load_managed_config: Callable[..., object]
    observe_blocked_plan_remotes: Callable[..., object]
    pr_for_branch: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    remote_integration_effects: Callable[..., object] | None = None


def _blocked_plan_reconciliation_effects(effects: BlockedPlanReconciliationEffectsEffects, ) -> blocked_plan_reconciliation.PlanRecoveryEffects:
    return blocked_plan_reconciliation.PlanRecoveryEffects(
        remote_integration_effects=effects.remote_integration_effects,
        ROOT=effects.ROOT,
        _BlockedCheckpointAdapter=effects._BlockedCheckpointAdapter,
        _BlockedCheckpointResults=_BlockedCheckpointResults,
        _blocked_execplan_metadata_error=effects._blocked_execplan_metadata_error,
        _complete_direct_rebase_checkpoint=effects._complete_direct_rebase_checkpoint,
        _current_rebase_service=effects._current_rebase_service,
        _evaluate_remote_checkpoint_cause=effects._evaluate_remote_checkpoint_cause,
        _journaled_direct_rebase=effects._journaled_direct_rebase,
        _non_default_index_flags=effects._non_default_index_flags,
        _reconcile_published_blocked_checkpoint=effects._reconcile_published_blocked_checkpoint,
        _recorded_base_fetch=effects._recorded_base_fetch,
        _revision_execplan_input_fingerprint=effects._revision_execplan_input_fingerprint,
        _update_blocked_error=effects._update_blocked_error,
        _worktree_execplan_input_fingerprint=effects._worktree_execplan_input_fingerprint,
        _worktree_state_signature=effects._worktree_state_signature,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        git_bytes=effects.git_bytes,
        github_repository=effects.github_repository,
        hydrate_execplan_project=effects.hydrate_execplan_project,
        load_managed_config=effects.load_managed_config,
        observe_blocked_plan_remotes=effects.observe_blocked_plan_remotes,
        owned_remote_oid=checkpoint_ownership.owned_remote_oid,
        pr_for_branch=effects.pr_for_branch,
        unmet_execplan_dependencies=effects.unmet_execplan_dependencies,
        validate_blocked_agent_scope=effects.validate_blocked_agent_scope,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class ProjectBlockedRecoveryEffectsEffects:
    """Named effects for _project_blocked_recovery_effects; no entry-point/module access."""
    _BlockedCheckpointAdapter: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    _publish_direct_rebase_checkpoint: Callable[..., object]
    _reconcile_published_blocked_checkpoint: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    _worktree_state_signature: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    observe_blocked_plan_remotes: Callable[..., object]
    pr_for_branch: Callable[..., object]
    push_diagnostic_checkpoint: Callable[..., object]
    reconcile_execplan_catalog: Callable[..., object]
    resume_project_block_if_safe: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    preserve_blocked_diagnostics: Callable[..., object] | None = None


def _project_blocked_recovery_effects(effects: ProjectBlockedRecoveryEffectsEffects, ) -> project_blocked_recovery.ProjectRecoveryEffects:
    return project_blocked_recovery.ProjectRecoveryEffects(
        preserve_blocked_diagnostics=effects.preserve_blocked_diagnostics,
        _BlockedCheckpointAdapter=effects._BlockedCheckpointAdapter,
        _BlockedCheckpointResults=_BlockedCheckpointResults,
        _current_rebase_service=effects._current_rebase_service,
        _journaled_direct_rebase=effects._journaled_direct_rebase,
        _publish_direct_rebase_checkpoint=effects._publish_direct_rebase_checkpoint,
        _reconcile_published_blocked_checkpoint=effects._reconcile_published_blocked_checkpoint,
        _recorded_base_fetch=effects._recorded_base_fetch,
        _update_blocked_error=effects._update_blocked_error,
        _worktree_state_signature=effects._worktree_state_signature,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        github_repository=effects.github_repository,
        observe_blocked_plan_remotes=effects.observe_blocked_plan_remotes,
        owned_remote_oid=checkpoint_ownership.owned_remote_oid,
        pr_for_branch=effects.pr_for_branch,
        # This reconciler may outlive one publication attempt. Resolve the
        # injected publication boundary when the attempt actually starts.
        push_diagnostic_checkpoint=lambda adapter, request, head: effects.push_diagnostic_checkpoint(adapter, request, head),
        reconcile_execplan_catalog=effects.reconcile_execplan_catalog,
        resume_project_block_if_safe=effects.resume_project_block_if_safe,
        unmet_execplan_dependencies=effects.unmet_execplan_dependencies,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class CheckpointRecoveryEffectsReconcilePublishedBlockedCheckpointEffects:
    """Named effects for _checkpoint_recovery_effects_reconcile_published_blocked_checkpoint; no entry-point/module access."""
    _BlockedCheckpointAdapter: Callable[..., object]
    _blocked_execplan_revision_metadata_error: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _evaluate_remote_checkpoint_cause: Callable[..., object]
    _ignored_paths_overlapping_git_update: Callable[..., object]
    _rebase_resume_ignored_overlap: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _reclassify_remote_checkpoint_block: Callable[..., object]
    _reclassify_remote_specification_deadlock: Callable[..., object]
    _remote_checkpoint_input_identity: Callable[..., object]
    _remote_checkpoint_refusal: Callable[..., object]
    _resume_remote_corrected_execplan: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    _capture_agent_baseline: Callable[..., object]
    _git_with_index: Callable[..., object]
    _commit_tree: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]


def _checkpoint_recovery_effects_reconcile_published_blocked_checkpoint(effects: CheckpointRecoveryEffectsReconcilePublishedBlockedCheckpointEffects, ) -> checkpoint_recovery.ReconcilePublishedBlockedCheckpointEffects:
    return checkpoint_recovery.ReconcilePublishedBlockedCheckpointEffects(
        _ignored_paths_overlapping_git_update=effects._ignored_paths_overlapping_git_update,
        _rebase_resume_ignored_overlap=effects._rebase_resume_ignored_overlap,
        _BlockedCheckpointAdapter=effects._BlockedCheckpointAdapter,
        _capture_agent_baseline=effects._capture_agent_baseline,
        _git_with_index=effects._git_with_index,
        _commit_tree=effects._commit_tree,
        _journaled_direct_rebase=effects._journaled_direct_rebase,
        validate_blocked_agent_scope=effects.validate_blocked_agent_scope,
        _blocked_execplan_revision_metadata_error=effects._blocked_execplan_revision_metadata_error,
        _current_rebase_service=effects._current_rebase_service,
        _evaluate_remote_checkpoint_cause=effects._evaluate_remote_checkpoint_cause,
        _nul_git_paths=effects._nul_git_paths,
        _published_plan_allows_common_plans=effects._published_plan_allows_common_plans,
        _reclassify_remote_checkpoint_block=effects._reclassify_remote_checkpoint_block,
        _reclassify_remote_specification_deadlock=effects._reclassify_remote_specification_deadlock,
        _remote_checkpoint_input_identity=effects._remote_checkpoint_input_identity,
        _remote_checkpoint_refusal=effects._remote_checkpoint_refusal,
        _resume_remote_corrected_execplan=effects._resume_remote_corrected_execplan,
        _revision_execplan_input_fingerprint=effects._revision_execplan_input_fingerprint,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        github_repository=effects.github_repository,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class CheckpointRecoveryEffectsEvaluateRemoteCheckpointCauseEffects:
    """Named effects for _checkpoint_recovery_effects_evaluate_remote_checkpoint_cause; no entry-point/module access."""
    ROOT: Path
    git: Callable[..., object]
    _remote_checkpoint_has_new_failure: Callable[..., object]
    _remote_checkpoint_review_revision: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    load_managed_config: Callable[..., object]
    parse_reviewer_result: Callable[..., object]
    parse_worker_result: Callable[..., object]
    reviewer_targeted_test_reason: Callable[..., object]
    run_agent: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    verify: Callable[..., object]


def _checkpoint_recovery_effects_evaluate_remote_checkpoint_cause(effects: CheckpointRecoveryEffectsEvaluateRemoteCheckpointCauseEffects, ) -> checkpoint_recovery.EvaluateRemoteCheckpointCauseEffects:
    return checkpoint_recovery.EvaluateRemoteCheckpointCauseEffects(
        MAX_REVIEWER_PROTOCOL_ATTEMPTS=MAX_REVIEWER_PROTOCOL_ATTEMPTS,
        ROOT=effects.ROOT,
        git=effects.git,
        ReviewerProtocolError=ReviewerProtocolError,
        WorkerResultError=WorkerResultError,
        _remote_checkpoint_has_new_failure=effects._remote_checkpoint_has_new_failure,
        _remote_checkpoint_review_revision=effects._remote_checkpoint_review_revision,
        capture_git_snapshot=effects.capture_git_snapshot,
        load_managed_config=effects.load_managed_config,
        parse_reviewer_result=effects.parse_reviewer_result,
        parse_worker_result=effects.parse_worker_result,
        reviewer_targeted_test_reason=effects.reviewer_targeted_test_reason,
        run_agent=effects.run_agent,
        validate_agent_scope=effects.validate_agent_scope,
        verify=effects.verify,
    )


@dataclass(frozen=True)
class ScopeReconciliationEffectsResumeOwnedCheckpointProjectBlockEffects:
    """Named effects for _scope_reconciliation_effects_resume_owned_checkpoint_project_block; no entry-point/module access."""
    _clear_active_in_current_transaction: Callable[..., object]
    _direct_rebase_history_proven: Callable[..., object]
    _published_review_commit_intent_matches: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    remote_integration_effects: Callable[..., object] | None = None


def _scope_reconciliation_effects_resume_owned_checkpoint_project_block(effects: ScopeReconciliationEffectsResumeOwnedCheckpointProjectBlockEffects, ) -> scope_reconciliation.ResumeOwnedCheckpointProjectBlockEffects:
    return scope_reconciliation.ResumeOwnedCheckpointProjectBlockEffects(
        remote_integration_effects=effects.remote_integration_effects,
        _clear_active_in_current_transaction=effects._clear_active_in_current_transaction,
        _direct_rebase_history_proven=effects._direct_rebase_history_proven,
        _published_review_commit_intent_matches=effects._published_review_commit_intent_matches,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        pr_for_branch=effects.pr_for_branch,
        remote_branch_oid=effects.remote_branch_oid,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
        validate_blocked_agent_scope=effects.validate_blocked_agent_scope,
        unmet_execplan_dependencies=effects.unmet_execplan_dependencies,
    )


@dataclass(frozen=True)
class ScopeReconciliationEffectsResumeLegacyReviewCountBlockEffects:
    """Named effects for _scope_reconciliation_effects_resume_legacy_review_count_block; no entry-point/module access."""
    _rebase_in_progress_for_branch: Callable[..., object]
    _with_proven_legacy_ignore_config: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _scope_reconciliation_effects_resume_legacy_review_count_block(effects: ScopeReconciliationEffectsResumeLegacyReviewCountBlockEffects, ) -> scope_reconciliation.ResumeLegacyReviewCountBlockEffects:
    return scope_reconciliation.ResumeLegacyReviewCountBlockEffects(
        _rebase_in_progress_for_branch=effects._rebase_in_progress_for_branch,
        _with_proven_legacy_ignore_config=effects._with_proven_legacy_ignore_config,
        _normalize_legacy_ignored_snapshot=effects._normalize_legacy_ignored_snapshot,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        pr_for_branch=effects.pr_for_branch,
        remote_branch_oid=effects.remote_branch_oid,
        validate_blocked_agent_scope=effects.validate_blocked_agent_scope,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class ScopeReconciliationEffectsResumeInterruptedBlockedRebaseEffects:
    """Named effects for _scope_reconciliation_effects_resume_interrupted_blocked_rebase; no entry-point/module access."""
    _blocked_execplan_metadata_error: Callable[..., object]
    _blocked_resume_worktree_matches: Callable[..., object]
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _direct_rebase_history_proven: Callable[..., object]
    _finish_owned_direct_rebase: Callable[..., object]
    _owned_rebase_journal_state: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _record_blocked_resume_head: Callable[..., object]
    _requeue_blocked_execplan: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    clear_agent_scope: Callable[..., object]
    git: Callable[..., object]
    remote_branch_oid: Callable[..., object]


def _scope_reconciliation_effects_resume_interrupted_blocked_rebase(effects: ScopeReconciliationEffectsResumeInterruptedBlockedRebaseEffects, ) -> scope_reconciliation.ResumeInterruptedBlockedRebaseEffects:
    return scope_reconciliation.ResumeInterruptedBlockedRebaseEffects(
        _blocked_execplan_metadata_error=effects._blocked_execplan_metadata_error,
        _blocked_resume_worktree_matches=effects._blocked_resume_worktree_matches,
        _complete_direct_rebase_checkpoint=effects._complete_direct_rebase_checkpoint,
        _current_rebase_service=effects._current_rebase_service,
        _direct_rebase_history_proven=effects._direct_rebase_history_proven,
        _finish_owned_direct_rebase=effects._finish_owned_direct_rebase,
        _owned_rebase_journal_state=effects._owned_rebase_journal_state,
        _owned_rebase_state_dir=effects._owned_rebase_state_dir,
        _reconcile_rebase_evidence_block=effects._reconcile_rebase_evidence_block,
        _record_blocked_resume_head=effects._record_blocked_resume_head,
        _requeue_blocked_execplan=effects._requeue_blocked_execplan,
        _update_blocked_error=effects._update_blocked_error,
        capture_git_snapshot=effects.capture_git_snapshot,
        clear_agent_scope=effects.clear_agent_scope,
        git=effects.git,
        remote_branch_oid=effects.remote_branch_oid,
    )


@dataclass(frozen=True)
class ScopeObservationEffectsEffects:
    """Named effects for _scope_observation_effects; no entry-point/module access."""
    _handoff_run: Callable[..., object]
    _worktree_catalog: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]


def _scope_observation_effects(effects: ScopeObservationEffectsEffects, ) -> scope_observation.ObservationEffects:
    return scope_observation.ObservationEffects(effects.git, effects.git_bytes, effects._worktree_catalog, effects._handoff_run, subprocess.run)
