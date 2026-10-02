#!/usr/bin/env python3
"""AI Agent가 Loop Manager의 다음 PR을 만들게 하는 부트스트랩 실행기."""

from __future__ import annotations
import argparse
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
import configparser
import contextlib
from contextvars import ContextVar
import difflib
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Protocol
from urllib.parse import quote

from loop_manager_internal.recovery.scoped_blocking import PR_CLOSED_BLOCK_REASON, GUIDANCE_BLOCK_REASON
from loop_manager_internal.git.source_sync import _base_worktree_index_error
from loop_manager_internal.git.source_sync import _base_local_path_signature
from loop_manager_internal.git.workspace import expected_execplan_workspace
from loop_manager_internal.git.github_observation import classify_pull_request_mergeability

from loop_manager_internal.runtime.contracts import (
    PublicationSafetyError, ManagedWorktreeConflictError,
    WORKSPACE_UNOWNED, WORKSPACE_OWNED, WORKSPACE_RESERVED,
    REBASE_IN_PROGRESS_ERROR, REBASE_CONFLICT_PREFIX,
    PR_CONFLICT_PREFLIGHT_BLOCK_REASON, PR_CONFLICT_PREFLIGHT_ERROR_PREFIX,
    PROJECT_STEP_RETRY, PROJECT_STEP_PLANNING_PENDING,
)
from loop_manager_internal.git import command_adapter
from loop_manager_internal.git import github_observation
from loop_manager_internal.git import workspace
from loop_manager_internal.git import source_sync
from loop_manager_internal.recovery import pull_request_recovery
from loop_manager_internal.recovery import scope_reconciliation
from loop_manager_internal.catalog import archive_git
from loop_manager_internal.recovery import checkpoint_adapter
from loop_manager_internal.recovery import checkpoint_recovery
from loop_manager_internal.recovery.checkpoint_adapter import _BlockedCheckpointResults


from loop_manager_internal.documents.guidance_approval import (
    GuidanceDocument,
    GuidanceFormatError,
    GuidanceProposal,
    apply_patch_to_content,
    contains_guidance_sections,
    content_hash as guidance_content_hash,
    finalized_patch,
    guidance_patch_hash,
    parse_guidance_document,
)
from loop_manager_internal.execution.execution_runtime import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionOutcomeKind,
    ExecutionPhase,
    ExecutionPhaseCoordinator,
    ExecutionPlanMetadata,
    LatestBaseCheckpoint,
    LatestBasePort,
)
from loop_manager_internal.config.managed_config import ManagedConfig, ManagedConfigError, load_managed_config
from loop_manager_internal.notifications import notifications
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.quality.quality_guidance import AGENT_QUALITY_GUIDANCE
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import blocked_remote_observation
from loop_manager_internal.catalog import execplan_archive
from loop_manager_internal.storage import execplan_archive_state
from loop_manager_internal.catalog import requirement_archive
from loop_manager_internal.storage import requirement_archive_state
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
from loop_manager_internal.catalog import project_catalog_refresh
from loop_manager_internal.planning.planning_catalog import (
    MutableReadySnapshot,
    PlanDeletion,
    PlannerValidationError,
    mutable_snapshot_token,
    snapshot_from_dict,
    snapshot_to_dict,
)
from loop_manager_internal.planning.planning_runtime import (
    MAX_PLANNER_VALIDATION_RETRIES,
    PlanningAgentResult,
    PlanningAgentResultKind,
    PlanningContext,
    PlanningLifecycleCoordinator,
    PlanningOutcome,
    PlanningOutcomeKind,
    PlanningResultInvalid,
    PlanningScopeResult,
    PlanningWorkspaceOwnershipError,
)
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.execution import execution_handlers
from loop_manager_internal.git import managed_rebase_publication
from loop_manager_internal.execution import execution_revision
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.agents.agent_invocation import (AgentProfile, AgentRequest, AgentResult, AgentResultKind,
                              AttemptSequence, InvocationContext, ResolvedProfile,
                              TrackedAgentCall, Usage, approval_policy_failure,
                              classify_agent_result)
from loop_manager_internal.agents.agent_provider_usage import claude_usage, codex_log_usage
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.agents import agent_write_isolation
from loop_manager_internal.storage import validation_feedback_state
from loop_manager_internal.execution.validation_feedback import normalize_feedback, redact_secrets
from loop_manager_internal.execution.worker_outcome import worker_outcome_instruction
from loop_manager_internal.execution.verify_process import run_verify_process, VerifyTerminationUncertain
from loop_manager_internal.execution.execution_state import ExecutionStatePort as _ExecutionStatePort
from types import SimpleNamespace
from loop_manager_internal.maintenance.maintenance_planning import (
    MaintenancePlanMode,
    MaintenancePlanningContext,
    MaintenancePlanningError,
    MaintenancePlanningRestart,
    PlanRevision,
    ReadyPlanSnapshot,
    terminal_new_plan_ids,
    validate_barrier_revision,
    validate_refactoring_plan,
    validate_revalidation_revision,
)
from loop_manager_internal.maintenance.maintenance_orchestration import (
    MaintenanceOrchestrationError,
    MaintenanceOutcome,
    MaintenancePlannerDecision,
    all_cycle_plans_merged,
    is_unowned_ready,
    assessment_can_yield,
    cycle_plan_status_error,
    maintenance_planning_fingerprint,
    parse_maintenance_planner_decision,
    select_cycle_ready_execplan,
    validate_prerequisite_plan_ids,
)
from loop_manager_internal.maintenance import maintenance_git
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.documents import document_publication
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.runtime import runtime_scope
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.git import rebase_file_evidence
from loop_manager_internal.git import managed_rebase_evidence
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.git import managed_rebase_validation
from loop_manager_internal.git import managed_rebase_runtime
from loop_manager_internal.git import rebase_block_reconciliation
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode, BlockScope
from loop_manager_internal.runtime import bootstrap
from loop_manager_internal.storage import catalog_migration, planning_publication_state
from loop_manager_internal.storage.planning_publication_state import (
    GeneralPlanningPublication, general_planning_publication,
)
from loop_manager_internal.runtime.contracts import (
    BaseWorktreeSyncResult,
    BaseWorktreeSnapshot,
    RuntimeWorktree,
    BootstrapRefreshHandoff,
    RuntimeMutation,
    GitLockError,
    WorkspaceOwnershipError,
    ProcessLockBusyError,
)
from loop_manager_internal.runtime.process_lock import (
    state_directory,
    is_lock_contention,
    process_lock,
    _write_process_lock_identity,
    _held_process_lock_identity,
)
from loop_manager_internal.runtime.runtime_identity import (
    _canonical_path,
    _runtime_intent_path,
    _runtime_intent_fields,
    _write_runtime_intent,
    _read_runtime_intent,
    _legacy_runtime_adoption_path,
    _validate_legacy_runtime_adoption,
    _record_legacy_runtime_adoption,
    runtime_repository_id,
    expected_runtime_worktree,
    source_config_path,
    runtime_child_arguments,
)
from loop_manager_internal.storage.database import (
    SCHEMA,
    CLEANUP_PENDING_SCHEMA,
    MERGED_WORKTREE_CLEANUP_SCHEMA,
    MANAGER_BASE_FETCH_SCHEMA,
    EXEC_PLANS_SCHEMA,
    GUIDANCE_APPROVALS_SCHEMA,
    EXEC_PLANS_ACTIVE_INDEX,
    EXECPLAN_CANCELLATIONS_SCHEMA,
    GENERAL_PLANNING_PUBLICATIONS_SCHEMA,
    StateConnection,
    _ensure_execplans_schema,
    connect_database,
    _ensure_cleanup_pending_schema,
)
from loop_manager_internal.storage.execplan_catalog_state import (
    serialize_plan_metadata as _execplan_metadata, _merged_execplan_ids, worker_level,
)
from loop_manager_internal.storage.planning_publication_state import (
    _MUTABLE_READY_EVIDENCE_FIELDS, _mutable_ready_cas_where,
)
from loop_manager_internal.storage.catalog_migration import (_insert_recovered_execplan, _quarantine_unverified_historical_runtime)

from loop_manager_internal.agents.scope_contracts import (
    GitSnapshot,
    ManagerGitCheckpoint,
    AgentChange,
    AgentScopeError,
)
from loop_manager_internal.agents.agent_profiles import (
    provider_profile,
    provider_worker_profile,
    validate_provider_profiles,
    agent_environment,
    AGENT_PROVIDERS,
    CLAUDE_EFFORTS,
)
from loop_manager_internal.agents.agent_prompts import (
    planner_prompt,
    maintenance_planner_prompt,
    worker_prompt,
    reviewer_prompt,
)
from loop_manager_internal.agents.agent_invocation import (
    session_resume_unavailable,
    _coerce_agent_result,
    run_agent_with_retries,
    MAX_TRANSIENT_AGENT_RETRIES,
)
from loop_manager_internal.agents import (
    provider_adapters,
    invocation_service,
    scope_observation,
    scope_validation,
    scope_lifecycle,
    scope_recovery,
    scope_baseline,
    agent_profiles,
)
from loop_manager_internal.agents.provider_adapters import (
    codex_sandbox_arguments,
    claude_permission_arguments,
    _claude_result_message,
    AgentProvider,
    CodexAgentAdapter,
    ClaudeAgentAdapter,
    AGENT_CLI_TIMEOUT_SECONDS,
)
from loop_manager_internal.agents.agent_scope_decision import (
    _worker_path_allowed,
    _same_local_snapshot,
    _non_default_index_flags,
    _metadata_changes_after_block,
)
from loop_manager_internal.agents.scope_observation import (
    _GIT_OPERATION_PATHS,
    _git_operation_path_signature,
    _normalized_config_origin,
)
from loop_manager_internal.agents.scope_baseline import _deserialize_agent_baseline
from loop_manager_internal.agents.scope_recovery import _blocked_agent_local_baseline, _diagnosed_blocked_commit_allowances


ROOT = Path(__file__).resolve().parent
MAX_VALIDATION_FINGERPRINT_REPEATS = 3
MAX_REVIEWER_PROTOCOL_ATTEMPTS = 3
PROJECT_STEP_TRANSIENT_PAUSE = 3
PROJECT_STEP_USAGE_PAUSE = 4
TRANSIENT_AGENT_ERROR_PREFIX = "일시적 Agent 실패 (상태 보존):"
USAGE_LIMIT_ERROR_PREFIX = "AI 사용 한도 소진 (상태 보존):"
PLAN_SHA_BACKFILL_ERROR = "계획 commit SHA를 기존 작업에서 안전하게 복구할 수 없습니다."
PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}
ACTIVE_EXECPLAN_STATUSES = runtime_state.ACTIVE_STATUSES
WAITING_EXECPLAN_STATUSES = frozenset({"PR_OPEN", "PLAN_BLOCKED"})
KNOWN_EXECPLAN_STATUSES = ACTIVE_EXECPLAN_STATUSES | WAITING_EXECPLAN_STATUSES | {
    "READY",
    "MERGED",
}
FIELDS = runtime_state.PROJECT_UPDATE_FIELDS


def _provider_adapters_effects() -> provider_adapters.ProviderEffects:
    return provider_adapters.ProviderEffects(role, worker_profile, _agent_profile,
                                            _handoff_popen, _handoff_run)


def _invocation_service_effects() -> invocation_service.InvocationEffects:
    return invocation_service.InvocationEffects(_agent_profile, _check_runtime_handoff,
        _start_agent_invocation, capture_git_snapshot, agent_provider)


def _start_agent_invocation(
    ledger: agent_invocation_state.InvocationRepository,
    invocation: InvocationContext,
    role_name: str,
    provider: str,
    profile: ResolvedProfile,
    session_id: str | None,
    *,
    scope_id: str | None,
) -> str:
    gate = _runtime_handoff_signal.get()
    if gate is None:
        return ledger.start(invocation, role_name, provider, profile, session_id, scope_id=scope_id)
    with gate.launch_lock:
        gate.check()
        return ledger.start(invocation, role_name, provider, profile, session_id, scope_id=scope_id)


def _command_adapter_effects_git() -> command_adapter.GitEffects:
    return command_adapter.GitEffects(
        _ignored_paths_overlapping_git_update=_ignored_paths_overlapping_git_update,
        _rebase_resume_ignored_overlap=_rebase_resume_ignored_overlap,
        command=command,
        is_git_lock_failure=is_git_lock_failure,
    )


def _command_adapter_effects_git_bytes() -> command_adapter.GitBytesEffects:
    return command_adapter.GitBytesEffects(
        _handoff_run=_handoff_run,
        is_git_lock_failure=is_git_lock_failure,
    )


def _github_observation_effects_pr_for_branch() -> github_observation.PrForBranchEffects:
    return github_observation.PrForBranchEffects(
        command=command,
    )


def _workspace_effects_validate_managed_worktree() -> workspace.ValidateManagedWorktreeEffects:
    return workspace.ValidateManagedWorktreeEffects(
        _rebase_in_progress_for_branch=_rebase_in_progress_for_branch,
        _worktree_catalog=_worktree_catalog,
    )


def _workspace_effects_validate_reserved_worktree() -> workspace.ValidateReservedWorktreeEffects:
    return workspace.ValidateReservedWorktreeEffects(
        capture_git_snapshot=capture_git_snapshot,
        validate_managed_worktree=validate_managed_worktree,
    )


def _workspace_effects_make_worktree() -> workspace.MakeWorktreeEffects:
    return workspace.MakeWorktreeEffects(
        _managed_worktree_marker=_managed_worktree_marker,
        _managed_worktree_transition_state=_managed_worktree_transition_state,
        _worktree_catalog=_worktree_catalog,
        _write_managed_worktree_receipt=_write_managed_worktree_receipt,
        git=git,
        validate_managed_worktree=validate_managed_worktree,
        validate_reserved_worktree=validate_reserved_worktree,
    )


def _workspace_effects_make_planning_worktree() -> workspace.MakePlanningWorktreeEffects:
    return workspace.MakePlanningWorktreeEffects(
        git=git,
        validate_managed_worktree=validate_managed_worktree,
        validate_reserved_worktree=validate_reserved_worktree,
    )


def _workspace_effects_validate_execplan_workspace_identity() -> workspace.ValidateExecplanWorkspaceIdentityEffects:
    return workspace.ValidateExecplanWorkspaceIdentityEffects(
        _validate_managed_path_boundary=_validate_managed_path_boundary,
        expected_execplan_workspace=expected_execplan_workspace,
    )


def _source_sync_effects_base_worktree_snapshot() -> source_sync.BaseWorktreeSnapshotEffects:
    return source_sync.BaseWorktreeSnapshotEffects(
        _base_local_path_signature=_base_local_path_signature,
        _base_worktree_index_error=_base_worktree_index_error,
        _git_operation_entries=_git_operation_entries,
        _ignored_worktree_paths=_ignored_worktree_paths,
        _index_entries=_index_entries,
        _nul_git_paths=_nul_git_paths,
        git=git,
    )


def _source_sync_effects_base_paths_overlap() -> source_sync.BasePathsOverlapEffects:
    return source_sync.BasePathsOverlapEffects(
        _paths_overlap=_paths_overlap,
    )


def _source_sync_effects_source_transition_state() -> source_sync.SourceTransitionStateEffects:
    return source_sync.SourceTransitionStateEffects(
        _base_worktree_snapshot=_base_worktree_snapshot,
    )


def _source_sync_effects_source_transition_marker() -> source_sync.SourceTransitionMarkerEffects:
    return source_sync.SourceTransitionMarkerEffects(
        git=git,
    )


def _source_sync_effects_source_expected_index() -> source_sync.SourceExpectedIndexEffects:
    return source_sync.SourceExpectedIndexEffects(
        git_bytes=git_bytes,
    )


def _source_sync_effects_synchronize_base_worktree() -> source_sync.SynchronizeBaseWorktreeEffects:
    return source_sync.SynchronizeBaseWorktreeEffects(
        _base_paths_overlap=_base_paths_overlap,
        _base_worktree_index_error=_base_worktree_index_error,
        _base_worktree_snapshot=_base_worktree_snapshot,
        _git_operation_entries=_git_operation_entries,
        _nul_git_paths=_nul_git_paths,
        _source_expected_index=_source_expected_index,
        _source_transition_marker=_source_transition_marker,
        _source_transition_state=_source_transition_state,
        git=git,
        is_git_lock_failure=is_git_lock_failure,
    )


def _source_sync_effects_ensure_safe_base_worktree() -> source_sync.EnsureSafeBaseWorktreeEffects:
    return source_sync.EnsureSafeBaseWorktreeEffects(
        synchronize_base_worktree=synchronize_base_worktree,
    )


def _source_sync_effects_log_base_worktree_sync() -> source_sync.LogBaseWorktreeSyncEffects:
    return source_sync.LogBaseWorktreeSyncEffects(
        operation_log=operation_log,
    )


def _pull_request_recovery_effects_reconcile_existing_pull_request() -> pull_request_recovery.ReconcileExistingPullRequestEffects:
    return pull_request_recovery.ReconcileExistingPullRequestEffects(
        _block_closed_pull_request=_block_closed_pull_request,
        _fetch_pr_conflict_recovery_refs=_fetch_pr_conflict_recovery_refs,
        _is_pr_conflict_preflight_block=_is_pr_conflict_preflight_block,
        _plan_id_from_path=_plan_id_from_path,
        _record_project_recovery=_record_project_recovery,
        _resume_reopened_pull_request=_resume_reopened_pull_request,
        _update_blocked_error=_update_blocked_error,
        _validate_active_pr_conflict_recovery=_validate_active_pr_conflict_recovery,
        _validate_blocked_pr_without_conflict=_validate_blocked_pr_without_conflict,
        _validate_pr_conflict_recovery_identity=_validate_pr_conflict_recovery_identity,
        _validate_pr_conflict_recovery_ownership=_validate_pr_conflict_recovery_ownership,
        block_active_execplan_from_revision=block_active_execplan_from_revision,
        classify_pull_request_mergeability=classify_pull_request_mergeability,
        complete_merged_execplan_worktree=complete_merged_execplan_worktree,
        make_worktree=make_worktree,
        operation_print=operation_print,
        project_execplan=project_execplan,
        recover_confirmed_pr_conflict_push=recover_confirmed_pr_conflict_push,
        remote_base=remote_base,
        update_project=update_project,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
    )


def _pull_request_recovery_effects_resume_pr_conflict_rebase() -> pull_request_recovery.ResumePrConflictRebaseEffects:
    return pull_request_recovery.ResumePrConflictRebaseEffects(
        _complete_direct_rebase_checkpoint=_complete_direct_rebase_checkpoint,
        _current_rebase_service=_current_rebase_service,
        _finish_owned_direct_rebase=_finish_owned_direct_rebase,
        _journaled_direct_rebase=_journaled_direct_rebase,
        _owned_rebase_state_dir=_owned_rebase_state_dir,
        _rebase_in_progress_for_branch=_rebase_in_progress_for_branch,
        active_execplan=active_execplan,
        block_active_execplan=block_active_execplan,
        git=git,
        is_git_lock_failure=is_git_lock_failure,
        operation_print=operation_print,
        update_project=update_project,
    )


def _scope_reconciliation_effects_resume_project_block_if_safe() -> scope_reconciliation.ResumeProjectBlockIfSafeEffects:
    return scope_reconciliation.ResumeProjectBlockIfSafeEffects(
        _agent_git_resources=_agent_git_resources,
        _clear_active_in_current_transaction=_clear_active_in_current_transaction,
        _clear_project_block_without_losing_planning_error=_clear_project_block_without_losing_planning_error,
        _reconcile_guidance_double_block=_reconcile_guidance_double_block,
        _reconcile_rebase_evidence_block=_reconcile_rebase_evidence_block,
        _reconciled_manager_scope_metadata=_reconciled_manager_scope_metadata,
        _requeue_blocked_execplan=_requeue_blocked_execplan,
        _resume_owned_checkpoint_project_block=_resume_owned_checkpoint_project_block,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        hydrate_execplan_project=hydrate_execplan_project,
        validate_agent_scope=validate_agent_scope,
        validate_blocked_agent_scope=validate_blocked_agent_scope,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
        validate_planning_worktree_ownership=validate_planning_worktree_ownership,
    )


def _scope_reconciliation_effects_resume_plan_blocks() -> scope_reconciliation.ResumePlanBlocksEffects:
    return scope_reconciliation.ResumePlanBlocksEffects(
        plan_recovery_effects=_blocked_plan_reconciliation_effects,
        _clear_active_in_current_transaction=_clear_active_in_current_transaction,
        _reconcile_published_blocked_checkpoint=_reconcile_published_blocked_checkpoint,
        _reconcile_rebase_evidence_block=_reconcile_rebase_evidence_block,
        _resume_interrupted_blocked_rebase=_resume_interrupted_blocked_rebase,
        _resume_legacy_review_count_block=_resume_legacy_review_count_block,
        _update_blocked_error=_update_blocked_error,
        active_execplan=active_execplan,
        hydrate_execplan_project=hydrate_execplan_project,
        plan_blocked_execplans=plan_blocked_execplans,
        project_execplan=project_execplan,
        resume_blocked_execplan_if_inputs_changed=resume_blocked_execplan_if_inputs_changed,
        resume_guidance_approval_if_decided=resume_guidance_approval_if_decided,
        unmet_execplan_dependencies=unmet_execplan_dependencies,
        validate_blocked_agent_scope=validate_blocked_agent_scope,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
    )


def _archive_git_effects() -> archive_git.ArchiveGitEffects:
    return archive_git.ArchiveGitEffects(
        _recorded_base_fetch=_recorded_base_fetch,
        _validate_managed_path_boundary=_validate_managed_path_boundary,
        command=command,
        git=git,
        git_bytes=git_bytes,
        is_git_lock_failure=is_git_lock_failure,
        validate_managed_worktree=validate_managed_worktree,
    )


def _checkpoint_safety_effects() -> checkpoint_adapter.CheckpointSafetyEffects:
    return checkpoint_adapter.CheckpointSafetyEffects(
        _guidance_document=_guidance_document,
        _guidance_document_matches_rows=_guidance_document_matches_rows,
        _guidance_rows=_guidance_rows,
        _owned_checkpoint_remote_oid=_owned_checkpoint_remote_oid,
        _proven_manager_base_refs=_proven_manager_base_refs,
        _proven_merged_cleanup_paths=_proven_merged_cleanup_paths,
        _published_plan_allows_common_plans=_published_plan_allows_common_plans,
        _rebase_in_progress_for_branch=_rebase_in_progress_for_branch,
        capture_git_snapshot=capture_git_snapshot,
        current_scope_effects=lambda: project_blocked_recovery.CurrentScopeEffects(_current_rebase_service),
        git=git,
        github_repository=github_repository,
        pr_for_branch=pr_for_branch,
        validate_agent_scope=validate_agent_scope,
        validate_blocked_agent_scope=validate_blocked_agent_scope,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
    )


def _checkpoint_publication_effects() -> checkpoint_adapter.CheckpointPublicationEffects:
    return checkpoint_adapter.CheckpointPublicationEffects(
        _git_with_index=_git_with_index,
        capture_git_snapshot=capture_git_snapshot,
        command=command,
        git=git,
    )


def _diagnostic_publication_effects() -> checkpoint_adapter.PushDiagnosticCheckpointEffects:
    return checkpoint_adapter.PushDiagnosticCheckpointEffects(
        capture_git_snapshot=capture_git_snapshot,
        git=git,
    )


def _managed_rebase_runtime_effects() -> managed_rebase_runtime.RebaseRuntimeEffects:
    return managed_rebase_runtime.RebaseRuntimeEffects(
        _guidance_document=_guidance_document,
        _guidance_document_matches_rows=_guidance_document_matches_rows,
        _guidance_final_expected=_guidance_final_expected,
        _guidance_target_state=_guidance_target_state,
        _owned_rebase_metadata=_owned_rebase_metadata,
        _path_signature=_path_signature,
        _published_plan_allows_common_plans=_published_plan_allows_common_plans,
        _revision_execplan_input_fingerprint=_revision_execplan_input_fingerprint,
        _worker_path_allowed=_worker_path_allowed,
        _worktree_execplan_input_fingerprint=_worktree_execplan_input_fingerprint,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        live_remote_branch_oid=lambda git_effect, repo_path, branch: manager_git_journal.live_remote_branch_oid(git_effect, repo_path, branch),
        pr_for_branch=pr_for_branch,
        requirement_archive_candidates=requirement_archive_candidates,
        requirement_archive_move_set=requirement_archive_move_set,
        requirement_archive_path=requirement_archive_path,
        validate_active_execplan_metadata=validate_active_execplan_metadata,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
    )


def _blocked_plan_reconciliation_effects() -> blocked_plan_reconciliation.PlanRecoveryEffects:
    return blocked_plan_reconciliation.PlanRecoveryEffects(
        ROOT=ROOT,
        _BlockedCheckpointAdapter=_BlockedCheckpointAdapter,
        _BlockedCheckpointResults=_BlockedCheckpointResults,
        _blocked_execplan_metadata_error=_blocked_execplan_metadata_error,
        _complete_direct_rebase_checkpoint=_complete_direct_rebase_checkpoint,
        _current_rebase_service=_current_rebase_service,
        _evaluate_remote_checkpoint_cause=_evaluate_remote_checkpoint_cause,
        _journaled_direct_rebase=_journaled_direct_rebase,
        _non_default_index_flags=_non_default_index_flags,
        _reconcile_published_blocked_checkpoint=_reconcile_published_blocked_checkpoint,
        _recorded_base_fetch=_recorded_base_fetch,
        _revision_execplan_input_fingerprint=_revision_execplan_input_fingerprint,
        _update_blocked_error=_update_blocked_error,
        _worktree_execplan_input_fingerprint=_worktree_execplan_input_fingerprint,
        _worktree_state_signature=_worktree_state_signature,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        github_repository=github_repository,
        hydrate_execplan_project=hydrate_execplan_project,
        load_managed_config=load_managed_config,
        observe_blocked_plan_remotes=observe_blocked_plan_remotes,
        owned_remote_oid=checkpoint_ownership.owned_remote_oid,
        pr_for_branch=pr_for_branch,
        unmet_execplan_dependencies=unmet_execplan_dependencies,
        validate_blocked_agent_scope=validate_blocked_agent_scope,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
    )


def _project_blocked_recovery_effects() -> project_blocked_recovery.ProjectRecoveryEffects:
    return project_blocked_recovery.ProjectRecoveryEffects(
        _BlockedCheckpointAdapter=_BlockedCheckpointAdapter,
        _BlockedCheckpointResults=_BlockedCheckpointResults,
        _current_rebase_service=_current_rebase_service,
        _journaled_direct_rebase=_journaled_direct_rebase,
        _publish_direct_rebase_checkpoint=_publish_direct_rebase_checkpoint,
        _reconcile_published_blocked_checkpoint=_reconcile_published_blocked_checkpoint,
        _recorded_base_fetch=_recorded_base_fetch,
        _update_blocked_error=_update_blocked_error,
        _worktree_state_signature=_worktree_state_signature,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        github_repository=github_repository,
        observe_blocked_plan_remotes=observe_blocked_plan_remotes,
        owned_remote_oid=checkpoint_ownership.owned_remote_oid,
        pr_for_branch=pr_for_branch,
        push_diagnostic_checkpoint=push_diagnostic_checkpoint,
        reconcile_execplan_catalog=reconcile_execplan_catalog,
        resume_project_block_if_safe=resume_project_block_if_safe,
        unmet_execplan_dependencies=unmet_execplan_dependencies,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
    )


def _checkpoint_recovery_effects_reconcile_published_blocked_checkpoint() -> checkpoint_recovery.ReconcilePublishedBlockedCheckpointEffects:
    return checkpoint_recovery.ReconcilePublishedBlockedCheckpointEffects(
        _BlockedCheckpointAdapter=_BlockedCheckpointAdapter,
        _blocked_execplan_revision_metadata_error=_blocked_execplan_revision_metadata_error,
        _current_rebase_service=_current_rebase_service,
        _evaluate_remote_checkpoint_cause=_evaluate_remote_checkpoint_cause,
        _nul_git_paths=_nul_git_paths,
        _published_plan_allows_common_plans=_published_plan_allows_common_plans,
        _reclassify_remote_checkpoint_block=_reclassify_remote_checkpoint_block,
        _reclassify_remote_specification_deadlock=_reclassify_remote_specification_deadlock,
        _remote_checkpoint_input_identity=_remote_checkpoint_input_identity,
        _remote_checkpoint_refusal=_remote_checkpoint_refusal,
        _resume_remote_corrected_execplan=_resume_remote_corrected_execplan,
        _revision_execplan_input_fingerprint=_revision_execplan_input_fingerprint,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        github_repository=github_repository,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
    )


def _checkpoint_recovery_effects_evaluate_remote_checkpoint_cause() -> checkpoint_recovery.EvaluateRemoteCheckpointCauseEffects:
    return checkpoint_recovery.EvaluateRemoteCheckpointCauseEffects(
        MAX_REVIEWER_PROTOCOL_ATTEMPTS=MAX_REVIEWER_PROTOCOL_ATTEMPTS,
        ROOT=ROOT,
        ReviewerProtocolError=ReviewerProtocolError,
        WorkerResultError=WorkerResultError,
        _remote_checkpoint_has_new_failure=_remote_checkpoint_has_new_failure,
        _remote_checkpoint_review_revision=_remote_checkpoint_review_revision,
        capture_git_snapshot=capture_git_snapshot,
        parse_reviewer_result=parse_reviewer_result,
        parse_worker_result=parse_worker_result,
        reviewer_targeted_test_reason=reviewer_targeted_test_reason,
        run_agent=run_agent,
        validate_agent_scope=validate_agent_scope,
        verify=verify,
    )


def _scope_reconciliation_effects_resume_owned_checkpoint_project_block() -> scope_reconciliation.ResumeOwnedCheckpointProjectBlockEffects:
    return scope_reconciliation.ResumeOwnedCheckpointProjectBlockEffects(
        _clear_active_in_current_transaction=_clear_active_in_current_transaction,
        _direct_rebase_history_proven=_direct_rebase_history_proven,
        _published_review_commit_intent_matches=_published_review_commit_intent_matches,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        pr_for_branch=pr_for_branch,
        remote_branch_oid=remote_branch_oid,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
    )


def _scope_reconciliation_effects_resume_legacy_review_count_block() -> scope_reconciliation.ResumeLegacyReviewCountBlockEffects:
    return scope_reconciliation.ResumeLegacyReviewCountBlockEffects(
        _rebase_in_progress_for_branch=_rebase_in_progress_for_branch,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        pr_for_branch=pr_for_branch,
        remote_branch_oid=remote_branch_oid,
        validate_blocked_agent_scope=validate_blocked_agent_scope,
        validate_execplan_workspace_identity=validate_execplan_workspace_identity,
        validate_managed_worktree=validate_managed_worktree,
    )


def _scope_reconciliation_effects_resume_interrupted_blocked_rebase() -> scope_reconciliation.ResumeInterruptedBlockedRebaseEffects:
    return scope_reconciliation.ResumeInterruptedBlockedRebaseEffects(
        _blocked_execplan_metadata_error=_blocked_execplan_metadata_error,
        _blocked_resume_worktree_matches=_blocked_resume_worktree_matches,
        _complete_direct_rebase_checkpoint=_complete_direct_rebase_checkpoint,
        _current_rebase_service=_current_rebase_service,
        _direct_rebase_history_proven=_direct_rebase_history_proven,
        _finish_owned_direct_rebase=_finish_owned_direct_rebase,
        _owned_rebase_journal_state=_owned_rebase_journal_state,
        _owned_rebase_state_dir=_owned_rebase_state_dir,
        _reconcile_rebase_evidence_block=_reconcile_rebase_evidence_block,
        _record_blocked_resume_head=_record_blocked_resume_head,
        _requeue_blocked_execplan=_requeue_blocked_execplan,
        _update_blocked_error=_update_blocked_error,
        capture_git_snapshot=capture_git_snapshot,
        clear_agent_scope=clear_agent_scope,
        git=git,
        remote_branch_oid=remote_branch_oid,
    )



def _scope_observation_effects() -> scope_observation.ObservationEffects:
    return scope_observation.ObservationEffects(git, git_bytes, _worktree_catalog, _handoff_run, subprocess.run)


def _scope_validation_effects() -> scope_validation.ScopeValidationEffects:
    return scope_validation.ScopeValidationEffects(
        _agent_git_resources=_agent_git_resources,
        repository=runtime_state.RuntimeStateRepository,
        _current_rebase_service=_current_rebase_service,
        _git_common_directory=_git_common_directory,
        _owned_rebase_state_dir=_owned_rebase_state_dir,
        _plan_id_from_path=_plan_id_from_path,
        _rebase_in_progress_for_branch=_rebase_in_progress_for_branch,
        _scope_snapshot_with_start_ignore_rules=_scope_snapshot_with_start_ignore_rules,
        agent_snapshot_changes=agent_snapshot_changes,
        git=git,
        operation_log=operation_log,
        validate_managed_worktree=validate_managed_worktree,
    )


def _scope_lifecycle_effects() -> scope_lifecycle.ScopeLifecycleEffects:
    return scope_lifecycle.ScopeLifecycleEffects(
        repository=runtime_state.RuntimeStateRepository,
        _agent_git_resources=_agent_git_resources,
        _capture_agent_baseline=_capture_agent_baseline,
        _current_rebase_service=_current_rebase_service,
        _guidance_worker_targets=_guidance_worker_targets,
        _owned_rebase_state_dir=_owned_rebase_state_dir,
        _plan_id_from_path=_plan_id_from_path,
        _published_plan_allows_common_plans=_published_plan_allows_common_plans,
        _reconciled_manager_scope_metadata=_reconciled_manager_scope_metadata,
        _remote_adoption_agent_baseline=_remote_adoption_agent_baseline,
        capture_git_snapshot=capture_git_snapshot,
        project_execplan=project_execplan,
        update_project=update_project,
        validate_agent_scope=validate_agent_scope,
    )


def _scope_recovery_effects() -> scope_recovery.ScopeRecoveryEffects:
    return scope_recovery.ScopeRecoveryEffects(
        repository=runtime_state.RuntimeStateRepository,
        _agent_git_resources=_agent_git_resources,
        _config_entries=_config_entries,
        _current_rebase_service=_current_rebase_service,
        _guidance_worker_targets=_guidance_worker_targets,
        _normalize_legacy_ignored_snapshot=_normalize_legacy_ignored_snapshot,
        _owned_checkpoint_remote_oid=_owned_checkpoint_remote_oid,
        _owned_rebase_state_dir=_owned_rebase_state_dir,
        _plan_id_from_path=_plan_id_from_path,
        _proven_manager_base_refs=_proven_manager_base_refs,
        _proven_merged_cleanup_paths=_proven_merged_cleanup_paths,
        _published_plan_allows_common_plans=_published_plan_allows_common_plans,
        _reconciled_manager_scope_metadata=_reconciled_manager_scope_metadata,
        _validate_runtime_lock_origin=_validate_runtime_lock_origin,
        _validated_runtime_registration_marker=_validated_runtime_registration_marker,
        _with_proven_legacy_ignore_config=_with_proven_legacy_ignore_config,
        active_execplan=active_execplan,
        capture_git_snapshot=capture_git_snapshot,
        git=git,
        github_repository=github_repository,
        operation_log=operation_log,
        project_execplan=project_execplan,
        recover_persisted_agent_worktree=recover_persisted_agent_worktree,
        requirement_archive_candidates=requirement_archive_candidates,
        update_project=update_project,
        validate_agent_scope=validate_agent_scope,
        validate_runtime_worktree=validate_runtime_worktree,
    )


def _scope_baseline_effects() -> scope_baseline.ScopeBaselineEffects:
    return scope_baseline.ScopeBaselineEffects(
        _handoff_run=_handoff_run,
        capture_git_snapshot=capture_git_snapshot,
        command=command,
        git=git,
    )


def _agent_profiles_effects() -> agent_profiles.ProfileEffects:
    return agent_profiles.ProfileEffects(
        command=command,
        role=role,
        worker_profile=worker_profile,
        claude_role=claude_role,
        claude_worker_profile=claude_worker_profile,
        codex_model_efforts=codex_model_efforts,
        claude_model_efforts=claude_model_efforts,
    )


class ProjectRunKind(str, Enum):
    COMPLETED = "completed"
    WAITING = "waiting"
    BLOCKED = "blocked"
    TRANSIENT_PAUSE = "transient_pause"
    USAGE_LIMIT_PAUSE = "usage_limit_pause"
    CONFIGURATION_ERROR = "configuration_error"
    OPERATIONAL_ERROR = "operational_error"
    RUNTIME_HANDOFF = "runtime_handoff"


@dataclass(frozen=True)
class ProjectRunResult:
    kind: ProjectRunKind
    exit_code: int
    detail: str | None = None


@dataclass(frozen=True)
class ScheduledProjectResult:
    repo: str
    result: ProjectRunResult


@dataclass(frozen=True)
class CatalogScheduleDecision:
    active: sqlite3.Row | None
    has_ready: bool
    runnable_ready: sqlite3.Row | None
    has_plan_blocked: bool = False
    has_pr_open: bool = False
    project_blocked: bool = False

    @property
    def has_waiting(self) -> bool:
        return self.has_plan_blocked or self.has_pr_open or self.project_blocked


@dataclass(frozen=True)
class ProjectRegistrationInspection:
    root: Path
    base_branch: str
    provider: str
    agents_hash: str
    agents_approved: bool
    origin: str | None = None
    source_revision: str | None = None


# Public compatibility name for integrations written before provider-neutral
# execution was introduced.
CodexResult = AgentResult


@dataclass(frozen=True)
class ValidationFailureDecision:
    stage: str
    fingerprint: str
    occurrences: int
    blocked: bool
    recovery: recovery_runtime.RecoveryDecision | None = None


@dataclass(frozen=True)
class WorkerLevelDecision:
    previous_level: int
    effective_level: int
    review_history: dict[str, dict[str, int]]

    @property
    def escalated(self) -> bool:
        return self.effective_level > self.previous_level


@dataclass(frozen=True)
class CleanupAssessment:
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class WorkerPublication:
    """The Worker-provided metadata used for its commit and pull request."""

    commit_title: str
    summary: str
    test_evidence: str


class RequirementArchiveError(RuntimeError):
    """Requirement archive eligibility or movement cannot be proven safe."""


class RequirementArchiveCollisionError(RequirementArchiveError):
    """A Requirement archive destination is already occupied."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"Requirement archive 대상이 이미 존재합니다: {path}")


class WorkerResultError(RuntimeError):
    """A Worker final response cannot safely be published."""


class ExecPlanMetadataError(RuntimeError):
    """The selected ExecPlan declaration disagrees with the catalog."""


class ReviewerProtocolError(RuntimeError):
    """A failed Reviewer response does not identify actionable issues."""


def local_now() -> datetime:
    """Return the local clock through a patchable boundary for operation logs."""

    return datetime.now()


def operation_log(message: str, *, error: bool = False) -> None:
    """Write timestamped operational output without adding persistent state."""

    prefix = local_now().strftime("%Y-%m-%d %H:%M:%S")
    output = sys.stderr if error else sys.stdout
    for line in str(message).splitlines() or [""]:
        __import__("builtins").print(f"{prefix} {line}", file=output)


def operation_print(*values: object, **kwargs: object) -> None:
    """Route legacy operational output through the shared timestamped boundary."""

    output = kwargs.get("file", sys.stdout)
    separator = str(kwargs.get("sep", " "))
    operation_log(separator.join(str(value) for value in values), error=output is sys.stderr)


def _bootstrap_service() -> bootstrap.BootstrapService:
    return bootstrap.BootstrapService(
        bootstrap.BootstrapGitEffects(
            git=git,
            _git_operation_entries=_git_operation_entries,
            _validate_managed_path_boundary=_validate_managed_path_boundary,
            validate_managed_worktree=validate_managed_worktree,
            _worktree_catalog=_worktree_catalog,
            _ignored_worktree_paths=_ignored_worktree_paths,
            _recorded_base_fetch=_recorded_base_fetch,
            github_repository=github_repository,
            synchronize_base_worktree=synchronize_base_worktree,
            repository_origin=repository_origin,
            operation_log=operation_log,
        ), runtime_root=ROOT,
    )


def _catalog_migration_effects() -> catalog_migration.CatalogMigrationEffects:
    return catalog_migration.CatalogMigrationEffects(
        revision_execplan_snapshot=revision_execplan_snapshot,
        pr_for_branch=pr_for_branch,
        _current_execplan_sha=_current_execplan_sha,
        _LegacyCatalogMergeProof=_LegacyCatalogMergeProof,
    )


def validate_runtime_worktree(
    source: Path,
    state_dir: Path,
    runtime: Path,
    commit_sha: str,
) -> None:
    return _bootstrap_service().validate_runtime_worktree(source, state_dir, runtime, commit_sha)


def _runtime_catalog_record(source: Path, runtime: Path) -> dict[str, str | bool]:
    return _bootstrap_service()._runtime_catalog_record(source, runtime)


def _runtime_transition_state(source: Path, runtime: Path) -> dict[str, object]:
    return _bootstrap_service()._runtime_transition_state(source, runtime)


def _managed_worktree_transition_state(repo: Path, worktree: Path) -> dict[str, object]:
    """Observe the registration and index tree of one ExecPlan workspace."""
    record = next((item for item in _worktree_catalog(repo)
                   if _canonical_path(Path(str(item.get("worktree", ""))))
                   == _canonical_path(worktree)), None)
    exists = worktree.exists() or worktree.is_symlink()
    return {
        "exists": exists,
        "registration": ({"head": record.get("HEAD"),
                          "branch": record.get("branch"),
                          "locked": record.get("locked")}
                         if record is not None else None),
        "index": (git(worktree, "write-tree").stdout.strip()
                  if exists and record is not None else None),
    }


def _managed_worktree_receipt(
    state_dir: Path, intent: Mapping[str, object], kind: str,
) -> Path:
    return state_dir / "managed-worktree-intents" / f"{intent['id']}.{kind}.json"


def _managed_worktree_marker(
    state_dir: Path, intent: Mapping[str, object], kind: str,
) -> bool:
    receipt = _managed_worktree_receipt(state_dir, intent, kind)
    return receipt.is_file() and manager_git_journal._read(receipt) == {
        "id": intent["id"], "repo": intent["repo"],
        "target": intent["target"], "before": intent["before"],
        "expected": intent["expected"],
    }


def _write_managed_worktree_receipt(
    state_dir: Path, token: str, repo: Path, worktree: Path,
    before: Mapping[str, object], expected: Mapping[str, object], kind: str,
) -> None:
    root = state_dir / "managed-worktree-intents"
    root.mkdir(parents=True, exist_ok=True)
    manager_git_journal._sync_directory(state_dir)
    manager_git_journal._write_new(root / f"{token}.{kind}.json", {
        "id": token, "repo": str(_canonical_path(repo)),
        "target": str(_canonical_path(worktree)), "before": dict(before),
        "expected": dict(expected),
    })


def _recover_managed_worktree_removal(
    state_dir: Path, repo: Path, worktree: Path,
) -> bool:
    return manager_git_journal.recover_active(
        state_dir, repo=str(_canonical_path(repo)),
        kind="managed_worktree_remove", target=str(_canonical_path(worktree)),
        observe=lambda: _managed_worktree_transition_state(repo, worktree),
        marker=lambda intent: _managed_worktree_marker(
            state_dir, intent, "remove",
        ),
    )


def _assess_missing_managed_cleanup(
    state_dir: Path, repo: Path, worktree: Path,
) -> CleanupAssessment:
    try:
        _recover_managed_worktree_removal(state_dir, repo, worktree)
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 정리 기록 복구 실패: {error}")
    return CleanupAssessment("missing")


def _runtime_transition_marker(
    state_dir: Path, source: Path, runtime: Path, sha: str,
    intent: Mapping[str, object],
) -> bool:
    return _bootstrap_service()._runtime_transition_marker(state_dir, source, runtime, sha, intent)


def _validate_runtime_lock_origin(
    state_dir: Path, source: Path, runtime: Path, sha: str, lock_reason: object,
) -> None:
    return _bootstrap_service()._validate_runtime_lock_origin(state_dir, source, runtime, sha, lock_reason)


def _validated_runtime_registration_marker(
    source: Path,
    state_dir: Path,
    runtime: Path,
    sha: str,
    mutation_id: str | None,
    *,
    legacy: bool,
    record_legacy_adoption: bool = False,
) -> str | None:
    return _bootstrap_service()._validated_runtime_registration_marker(source, state_dir, runtime, sha, mutation_id, legacy=legacy, record_legacy_adoption=record_legacy_adoption)


def prepare_runtime_worktree(
    source: Path, state_dir: Path, *, mutation_id: str | None = None,
    commit_sha: str | None = None,
) -> RuntimeWorktree:
    return _bootstrap_service().prepare_runtime_worktree(source, state_dir, mutation_id=mutation_id, commit_sha=commit_sha)


def remove_runtime_worktree(
    source: Path, state_dir: Path, runtime: RuntimeWorktree,
) -> None:
    return _bootstrap_service().remove_runtime_worktree(source, state_dir, runtime)


def bootstrap_base_branch(source: Path, state_dir: Path, origin: str) -> str:
    return _bootstrap_service().bootstrap_base_branch(source, state_dir, origin)


def _tracking_ref_oid(repo: Path, refname: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", f"{refname}^{{commit}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


class RemoteBranchMissingError(RuntimeError):
    """A successful remote query confirmed that the requested branch is absent."""


def _recorded_base_fetch(
    repo: Path, state_dir: Path, base: str, *, owner: str,
) -> str:
    """Fetch one base object and move its tracking ref with an owned CAS."""
    refname = f"refs/remotes/origin/{base}"
    remote_ref = f"refs/heads/{base}"
    remote = git(repo, "ls-remote", "--heads", "origin", remote_ref, check=False)
    rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if remote.returncode == 0 and not rows:
        raise RemoteBranchMissingError(f"origin/{base} 원격 branch를 찾을 수 없습니다.")
    if remote.returncode or len(rows) != 1 or rows[0][1:] != [remote_ref]:
        raise RuntimeError(f"origin/{base}의 원격 OID를 확정할 수 없습니다.")
    selected = rows[0][0]
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", selected) is None:
        raise RuntimeError(f"origin/{base}의 원격 OID가 유효하지 않습니다.")
    before = {"oid": _tracking_ref_oid(repo, refname)}

    def apply(token: str) -> None:
        git(repo, "fetch", "--no-tags", "--refmap=", "origin", remote_ref)
        fetched = git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
        if fetched != selected:
            raise RuntimeError(f"origin/{base}가 fetch 중 변경됐습니다.")
        git(repo, "update-ref", "--create-reflog", "-m",
            f"loop-manager-base-fetch:{token}", refname, selected,
            before["oid"] or "0" * len(selected))

    def marker(intent: Mapping[str, object]) -> bool:
        token = intent["id"]
        result = git(repo, "reflog", "show", "-1", "--format=%H%x00%gs", refname,
                     check=False)
        return (
            result.returncode == 0
            and f"{intent['expected']['oid']}\x00loop-manager-base-fetch:{token}"
            in result.stdout.splitlines()
        )

    manager_git_journal.execute(
        state_dir, owner=owner, kind=("bootstrap_fetch" if owner == "bootstrap"
                                      else "project_fetch"),
        repo=str(_canonical_path(repo)), target=refname,
        before=before, expected={"oid": selected},
        observe=lambda: {"oid": _tracking_ref_oid(repo, refname)},
        apply=apply, marker=marker,
    )
    return selected


def fetch_bootstrap_revision(source: Path, state_dir: Path) -> tuple[str, str]:
    return _bootstrap_service().fetch_bootstrap_revision(source, state_dir)


def validate_bootstrap_revision(source: Path, base: str, sha: str) -> None:
    return _bootstrap_service().validate_bootstrap_revision(source, base, sha)


def prepare_bootstrap_runtime(
    source: Path, state_dir: Path, mutation_id: str,
) -> RuntimeWorktree | BootstrapRefreshHandoff:
    return _bootstrap_service().prepare_bootstrap_runtime(source, state_dir, mutation_id)


def run_runtime_child(
    source: Path,
    state_dir: Path,
    config_path: Path,
    runtime: RuntimeWorktree,
    arguments: list[str],
) -> int:
    return _bootstrap_service().run_runtime_child(source, state_dir, config_path, runtime, arguments)


def validate_runtime_child(
    source: Path,
    state_dir: Path,
    commit_sha: str,
) -> None:
    return _bootstrap_service().validate_runtime_child(source, state_dir, commit_sha)


def validate_runtime_mutation(
    source: Path, state_dir: Path, sha: str, mutation_id: str | None,
) -> RuntimeMutation:
    return _bootstrap_service().validate_runtime_mutation(source, state_dir, sha, mutation_id)


def update_project(connection: sqlite3.Connection, repo: str, **values: object) -> None:
    if not values or not set(values).issubset(FIELDS):
        raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
    current = connection.execute(
        "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if current is None:
        notifications.enqueue_current_project_events(connection, repo)
        connection.commit()
        return
    if (
        current["plan_path"]
        and not ("phase" in values and values["phase"] is None)
    ):
        _ensure_execplans_schema(connection)
    runtime_state.RuntimeStateRepository(connection).update_project(repo, values)
    connection.commit()


def command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = _handoff_run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        raise RuntimeError(f"명령 실패 ({' '.join(arguments[:3])}):\n{output}")
    return result


def is_git_lock_failure(output: str) -> bool:
    normalized = " ".join(output.casefold().split())
    return any(
        marker in normalized
        for marker in (
            "another git process",
            "index.lock': file exists",
            "index.lock: file exists",
            "cannot lock ref",
            "could not lock",
            "failed to lock",
            "unable to create",
        )
    ) and ("lock" in normalized or ".lock" in normalized)


def git(
    repo: Path, *arguments: str, check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return command_adapter.git(_command_adapter_effects_git(), repo, *arguments, check=check, env=env)


def github_repository(origin_url: str) -> str:
    match = re.fullmatch(
        r"(?:git@|ssh://git@|https://)(?:www\.)?github\.com[:/](?P<owner>[^/:\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?",
        origin_url.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("origin이 GitHub 저장소가 아닙니다.")
    return f"{match.group('owner')}/{match.group('repo')}".lower()


def backfill_plan_shas(
    connection: sqlite3.Connection,
    *,
    target_repo: str | None = None,
    manage_transaction: bool = True,
) -> None:
    rows = connection.execute(
        """
        SELECT repo, plan_path
        FROM projects
        WHERE plan_sha IS NULL
          AND plan_path IS NOT NULL
          AND phase IN ('WORKING', 'VERIFYING', 'REVIEWING', 'PUBLISHING')
        """
    ).fetchall()
    if target_repo is not None:
        rows = [row for row in rows if row["repo"] == target_repo]
    for row in rows:
        result = git(
            Path(row["repo"]),
            "log",
            "--all",
            "--diff-filter=A",
            "--format=%H",
            "--",
            row["plan_path"],
            check=False,
        )
        commits = [
            value
            for value in result.stdout.splitlines()
            if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
        ]
        if len(commits) == 1:
            connection.execute(
                "UPDATE projects SET plan_sha = ? WHERE repo = ?",
                (commits[0], row["repo"]),
            )
        else:
            project = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?", (row["repo"],)
            ).fetchone()
            scoped_blocking.record_project_block(
                connection,
                str(row["repo"]),
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                PLAN_SHA_BACKFILL_ERROR,
                None,
                plan_id=_plan_id_from_path(str(row["plan_path"] or "")),
                previous_phase=(str(project["phase"] or "") or None),
            )
            notifications.enqueue_current_project_events(connection, str(row["repo"]))
    if manage_transaction:
        connection.commit()


def git_bytes(repo: Path, *arguments: str) -> bytes:
    return command_adapter.git_bytes(_command_adapter_effects_git_bytes(), repo, *arguments)


def _git_tree_entries(repo: Path, revision: str) -> dict[bytes, tuple[bytes, bytes]]:
    tree = git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("Git tree 항목 형식이 올바르지 않습니다.")
        _mode, object_type, object_id = parts
        if object_type not in {b"blob", b"commit"}:
            raise RuntimeError("지원하지 않는 Git tree 항목 형식입니다.")
        entries[path] = (object_type, object_id)
    return entries


def revision_execplan_snapshot(
    repo: Path,
    revision: str,
    *,
    archive_conflict_plan_ids: frozenset[str] = frozenset(),
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    """Read and fully validate active/archive plans from one immutable tree."""

    revision_sha = git(
        repo, "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).stdout.strip()
    tree = git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision_sha)
    documents: dict[str, bytes] = {}
    regular_paths: set[str] = set()
    invalid_plan_entries: dict[str, str] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("Git tree 항목 형식이 올바르지 않습니다.")
        mode, object_type, object_id = parts
        try:
            relative_path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            if raw_path.startswith(b"docs/plans/"):
                raise execplan_catalog.ExecPlanCatalogError(
                    "ExecPlan Git 경로가 UTF-8이 아닙니다.",
                    code="invalid-path-encoding",
                ) from error
            continue
        regular = object_type == b"blob" and mode in {b"100644", b"100755"}
        if regular:
            regular_paths.add(relative_path)
        plan_candidate = bool(
            re.fullmatch(r"docs/plans/[^/]+\.md", relative_path)
            or re.fullmatch(
                r"docs/plans/archive/[^/]+\.md", relative_path
            )
        )
        if not plan_candidate:
            continue
        if not regular:
            invalid_plan_entries[relative_path] = (
                "symlink" if mode == b"120000" else mode.decode("ascii")
            )
            continue
        documents[relative_path] = git_bytes(
            repo, "cat-file", "blob", object_id.decode("ascii")
        )
    return execplan_catalog.snapshot_from_documents(
        documents,
        validate_sources=True,
        available_regular_paths=frozenset(regular_paths),
        invalid_plan_entries=invalid_plan_entries,
        revision_sha=revision_sha,
        archive_conflict_plan_ids=archive_conflict_plan_ids,
    )


def _catalog_archive_conflict_ids(
    connection: sqlite3.Connection, repo_path: Path, repo: str, revision: str,
) -> frozenset[str]:
    """Use read-only, identity-bound PR evidence for archive path conflicts.

    Declaration validation precedes PR transitions. A newly merged PR may
    therefore still be PR_OPEN or blocked on a previously closed PR in SQLite
    when its archive collision is read.
    This proof permits only the existing collision diagnostic, never cleanup
    or an execution-state transition.
    """
    confirmed = set(_merged_execplan_ids(connection, repo))
    paths = (os.fsdecode(path) for path in _git_tree_entries(repo_path, revision))
    conflicts = execplan_catalog.conflicting_archived_plan_paths(paths)
    project = connection.execute(
        "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if project is None:
        return frozenset(confirmed)
    for plan_id in conflicts.keys() - confirmed:
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if plan is None or not plan["branch"]:
            continue
        if not (plan["status"] == "PR_OPEN" or (
            plan["status"] == "PLAN_BLOCKED"
            and plan["blocked_reason"] == PR_CLOSED_BLOCK_REASON
        )):
            continue
        pull_request = pr_for_branch(repo_path, str(plan["branch"]), plan["pr_url"])
        if pull_request is None:
            continue
        if (pull_request.get("baseRefName") != project["base_branch"]
                or pull_request.get("headRefName") != plan["branch"]
                or (plan["pr_url"] and pull_request.get("url") != plan["pr_url"])
                or (plan["pr_head_oid"] and pull_request.get("headRefOid") != plan["pr_head_oid"])):
            continue
        if pull_request.get("state") == "MERGED" or pull_request.get("mergedAt"):
            confirmed.add(plan_id)
    return frozenset(confirmed)


class _GitCatalogDefinitionPort:
    """Pin catalog reads to a fetched remote ref without consulting worktrees."""

    def __init__(
        self,
        repo: Path,
        remote_ref: str,
        archive_conflict_plan_ids: Callable[[], frozenset[str]],
        pinned_revision: str | None = None,
    ):
        self.repo = repo
        self.remote_ref = remote_ref
        self.archive_conflict_plan_ids = archive_conflict_plan_ids
        self.pinned_revision = pinned_revision
        self.loaded: execplan_catalog_state.PinnedCatalog | None = None

    def current_revision(self) -> str:
        return git(
            self.repo,
            "rev-parse",
            "--verify",
            f"{self.remote_ref}^{{commit}}",
        ).stdout.strip()

    def load(self) -> execplan_catalog_state.PinnedCatalog:
        if self.loaded is not None:
            return self.loaded
        revision_sha = self.pinned_revision or self.current_revision()
        try:
            snapshot = revision_execplan_snapshot(
                self.repo,
                revision_sha,
                archive_conflict_plan_ids=self.archive_conflict_plan_ids(),
            )
        except execplan_catalog.ExecPlanCatalogError as error:
            tree = _git_tree_entries(self.repo, revision_sha)
            evidence = []
            for path in error.related_paths:
                entry = tree.get(path.encode("utf-8"))
                if entry is not None:
                    evidence.append(
                        f"{path}:{entry[0].decode('ascii')}:"
                        f"{entry[1].decode('ascii')}"
                    )
                else:
                    evidence.append(f"{path}:missing")
            diagnostic = execplan_catalog_state.ReconciliationDiagnostic(
                revision_sha=revision_sha,
                code=error.code,
                message=str(error),
                path=error.path,
                source=error.source,
                related_paths=error.related_paths,
                fingerprint=execplan_catalog_state.diagnostic_fingerprint(
                    revision_sha,
                    error.code,
                    str(error),
                    tuple(evidence),
                ),
            )
            raise execplan_catalog_state.InvalidPinnedCatalog(
                diagnostic
            ) from error
        tree = git_bytes(
            self.repo, "ls-tree", "-r", "-z", "--full-tree", revision_sha
        )
        regular_paths: set[str] = set()
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, _separator, path = record.partition(b"\t")
            if metadata.split()[:2] not in (
                [b"100644", b"blob"], [b"100755", b"blob"]
            ):
                continue
            try:
                regular_paths.add(path.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        self.loaded = execplan_catalog_state.PinnedCatalog(
            revision_sha, snapshot, frozenset(regular_paths)
        )
        return self.loaded


def reconcile_execplan_catalog(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base_branch: str,
    *,
    record_invalid: bool = True,
    remote_sha: str | None = None,
    on_reconciled: Callable[[execplan_catalog_state.PinnedCatalog], None] | None = None,
) -> execplan_catalog_state.CatalogReconciliationResult:
    """Converge declaration cache or persist a recoverable project diagnostic."""

    remote_ref = f"refs/remotes/origin/{base_branch}"
    if remote_sha is not None:
        current_sha = git(
            repo_path, "rev-parse", "--verify", f"{remote_ref}^{{commit}}"
        ).stdout.strip()
        if current_sha != remote_sha:
            return execplan_catalog_state.CatalogReconciliationResult(
                execplan_catalog_state.ReconciliationKind.STALE,
                revision_sha=remote_sha,
            )
    port = _GitCatalogDefinitionPort(
        repo_path,
        remote_ref,
        lambda: _catalog_archive_conflict_ids(
            connection, repo_path, repo, remote_sha or port.current_revision(),
        ),
        pinned_revision=remote_sha,
    )
    repository = execplan_catalog_state.DefinitionStateRepository(connection)
    service = execplan_catalog_state.CatalogReconciliationService(repository, port)
    legacy = connection.execute(
        "SELECT 1 FROM execplan_catalog_revisions WHERE repo = ?", (repo,),
    ).fetchone() is None
    if legacy and not repository.has_publication_intent(repo):
        # Initial migration and declaration reconciliation share a rollback
        # boundary. Validate the remote graph before recovering runtime rows.
        try:
            with execplan_catalog_state._transaction(connection):
                pinned = port.load()
                if repository.capture(repo).plan_ids - frozenset(pinned.snapshot.all):
                    raise project_catalog_refresh.CatalogRefreshStopped(service.reconcile(repo))
                backfill_plan_shas(connection, target_repo=repo, manage_transaction=False)
                migrate_legacy_execplans(
                    connection, reconcile_history=True, target_repo=repo,
                    ensure_schema=False, manage_transaction=False,
                    pinned_catalog=pinned,
                )
                result = service.reconcile(repo)
                if result.kind is not execplan_catalog_state.ReconciliationKind.RECONCILED:
                    raise project_catalog_refresh.CatalogRefreshStopped(result)
        except execplan_catalog_state.InvalidPinnedCatalog as error:
            result = execplan_catalog_state.CatalogReconciliationResult(
                execplan_catalog_state.ReconciliationKind.INVALID_CATALOG,
                revision_sha=error.diagnostic.revision_sha, diagnostic=error.diagnostic,
            )
        except project_catalog_refresh.CatalogRefreshStopped as error:
            result = error.result
        except execplan_catalog.ExecPlanCatalogError as error:
            revision = port.loaded.revision_sha if port.loaded else (remote_sha or "")
            result = execplan_catalog_state.CatalogReconciliationResult(
                execplan_catalog_state.ReconciliationKind.INVALID_CATALOG,
                revision_sha=revision,
                diagnostic=execplan_catalog_state.ReconciliationDiagnostic(
                    revision, error.code, str(error), error.path, error.source,
                    execplan_catalog_state.diagnostic_fingerprint(
                        revision, error.code, str(error), error.related_paths,
                    ), error.related_paths,
                ),
            )
    else:
        result = service.reconcile(repo)
    if (result.kind is execplan_catalog_state.ReconciliationKind.RECONCILED
            and on_reconciled is not None):
        assert port.loaded is not None
        on_reconciled(port.loaded)
    catalog_reason = BlockReasonCode.EXECPLAN_CATALOG_INVALID.value
    existing_block = scoped_blocking.project_block(connection, repo)
    if result.kind is execplan_catalog_state.ReconciliationKind.INVALID_CATALOG:
        if (not record_invalid or (existing_block is not None
                and existing_block["reason_code"] != catalog_reason)):
            return result
        diagnostic = result.diagnostic
        assert diagnostic is not None
        location = diagnostic.path or "(catalog)"
        source = f"; source={diagnostic.source}" if diagnostic.source else ""
        evidence = (
            "원격 ExecPlan catalog가 유효하지 않습니다: "
            f"revision={diagnostic.revision_sha}; path={location}{source}; "
            f"code={diagnostic.code}; {diagnostic.message}"
        )
        project = connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        plan_id = _plan_id_from_path(diagnostic.path or "")
        with connection:
            scoped_blocking.record_project_block(
                connection,
                repo,
                BlockReasonCode.EXECPLAN_CATALOG_INVALID,
                evidence,
                diagnostic.fingerprint,
                plan_id=plan_id,
                previous_phase=(
                    str(project["phase"])
                    if project is not None and project["phase"]
                    else None
                ),
            )
            notifications.enqueue_current_project_events(connection, repo)
        return result
    if (
        record_invalid
        and result.kind is execplan_catalog_state.ReconciliationKind.RECONCILED
        and existing_block is not None
        and existing_block["reason_code"] == catalog_reason
    ):
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
    return result


def reconcile_execution_execplan_catalog(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base_branch: str,
) -> execplan_catalog_state.CatalogReconciliationResult:
    """Let the execution recovery gate persist its own final decision."""

    return reconcile_execplan_catalog(
        connection, repo_path, repo, base_branch, record_invalid=False
    )


def _is_active_requirement(path: bytes) -> bool:
    return re.fullmatch(rb"docs/requirements/[^/]*\.md", path) is not None


def _is_planning_input(path: bytes) -> bool:
    if path in {b"AGENTS.md", b"docs/PRODUCT.md", b"docs/ARCHITECTURE.md"}:
        return True
    if _is_active_requirement(path):
        return True
    return not path.startswith(b"docs/")


def _is_specification_input(path: bytes) -> bool:
    return path in {b"docs/PRODUCT.md", b"docs/ARCHITECTURE.md"} or (
        _is_active_requirement(path)
    )


def _planning_input_digest(entries: list[tuple[bytes, bytes, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-planning-input-v2\0")
    for path, object_type, contents in sorted(entries, key=lambda entry: entry[0]):
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(object_type).to_bytes(8, "big"))
        digest.update(object_type)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _specification_input_digest(
    entries: list[tuple[bytes, bytes, bytes]],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-specification-input-v1\0")
    for path, object_type, contents in sorted(entries, key=lambda entry: entry[0]):
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(object_type).to_bytes(8, "big"))
        digest.update(object_type)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _planning_input_fingerprint(repo: Path, revision: str) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in _git_tree_entries(repo, revision).items():
        if _is_planning_input(path):
            if object_type == b"blob":
                contents = git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            else:
                contents = object_id
            entries.append((path, object_type, contents))
    return _planning_input_digest(entries)


def planning_input_fingerprint(repo: Path, base: str) -> str:
    return _planning_input_fingerprint(repo, f"origin/{base}")


def _specification_input_fingerprint(repo: Path, revision: str) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in _git_tree_entries(repo, revision).items():
        if not _is_specification_input(path):
            continue
        contents = (
            git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            if object_type == b"blob"
            else object_id
        )
        entries.append((path, object_type, contents))
    return _specification_input_digest(entries)


def specification_input_fingerprint(repo: Path, base: str) -> str:
    return _specification_input_fingerprint(repo, f"origin/{base}")


def _is_requirement_archive_only_change(
    repo: Path,
    previous_revision: str,
    current_revision: str,
) -> bool:
    previous_tree = _git_tree_entries(repo, previous_revision)
    current_tree = _git_tree_entries(repo, current_revision)
    archive_prefix = b"docs/requirements/archive/"

    def relevant(path: bytes) -> bool:
        return _is_planning_input(path) or path.startswith(archive_prefix)

    previous = {
        path.decode("utf-8"): value
        for path, value in previous_tree.items()
        if relevant(path)
    }
    current = {
        path.decode("utf-8"): value
        for path, value in current_tree.items()
        if relevant(path)
    }
    return execplan_catalog.classify_archive_only_tree_change(
        previous, current
    ).requirement_only


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def requirement_archive_path(source: str) -> str:
    return execplan_catalog.requirement_archive_path(source)


def parse_frontmatter(text: str) -> dict[str, object]:
    return execplan_catalog.parse_frontmatter(text)


def _parse_execplan(
    worktree: Path,
    path: Path,
    *,
    require_priority: bool = False,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, object]:
    return execplan_catalog.parse_execplan_file(
        worktree,
        path,
        require_priority=require_priority,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    )


def _validate_dependency_graph(catalog: dict[str, dict[str, object]]) -> None:
    return execplan_catalog.validate_dependency_graph(catalog)


def load_execplan_snapshot(
    worktree: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    return execplan_catalog.load_execplan_snapshot(
        worktree,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    )


def load_execplan_catalog(
    worktree: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, dict[str, object]]:
    return load_execplan_snapshot(
        worktree,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    ).active_metadata()


def validate_execplan(worktree: Path, relative_path: str) -> dict[str, object]:
    catalog = load_execplan_catalog(worktree)
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", relative_path)
    if match:
        plan = catalog.get(match.group(1))
        if plan is not None:
            expected_path = f"docs/plans/[{plan['id']}] {plan['title']}.md"
            if relative_path == expected_path:
                return plan
    raise RuntimeError(f"검증된 ExecPlan 카탈로그에 계획이 없습니다: {relative_path}")


@dataclass(frozen=True)
class ValidatedPlanningResult:
    """Validated files to publish, separated from executable plans."""

    plans: tuple[tuple[str, dict[str, object]], ...]
    publication_paths: tuple[str, ...]
    publication_hashes: tuple[tuple[str, str], ...] = ()
    revisions: tuple[PlanRevision, ...] = ()
    deletions: tuple[PlanDeletion, ...] = ()
    mutable_snapshot: MutableReadySnapshot | None = None
    maintenance_context: MaintenancePlanningContext | None = None
    refactoring_plan_ids: tuple[str, ...] = ()
    revalidation_plan_ids: tuple[str, ...] = ()


_FUTURE_IDEA_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


def _idea_tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[0-9a-zA-Z가-힣]+", text.casefold()))


def _future_idea_items_from_text(text: str) -> tuple[tuple[str, ...], ...]:
    items: list[tuple[str, ...]] = []
    for line in text.splitlines():
        match = _FUTURE_IDEA_ITEM.fullmatch(line)
        if match is None:
            continue
        item = re.sub(r"^\[[ xX]\]\s*", "", match.group(1)).strip()
        tokens = _idea_tokens(item)
        if tokens:
            items.append(tokens)
    return tuple(items)


def _future_idea_items(
    worktree: Path,
    active_sha: str,
) -> tuple[tuple[str, ...], ...]:
    path = worktree / "docs/FUTURE_IDEAS.md"
    current_text = ""
    if path.is_file():
        try:
            current_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError("Planner FUTURE_IDEAS를 읽을 수 없습니다.") from error

    previous = git(
        worktree,
        "show",
        f"{active_sha}:docs/FUTURE_IDEAS.md",
        check=False,
    )
    previous_text = previous.stdout if previous.returncode == 0 else ""
    return tuple(
        dict.fromkeys(
            _future_idea_items_from_text(previous_text)
            + _future_idea_items_from_text(current_text)
        )
    )


def _contains_token_sequence(
    tokens: tuple[str, ...],
    candidate: tuple[str, ...],
) -> bool:
    width = len(candidate)
    return any(
        tokens[offset : offset + width] == candidate
        for offset in range(len(tokens) - width + 1)
    )


def _validate_future_idea_separation(
    worktree: Path,
    active_sha: str,
    plan_paths: list[str],
) -> None:
    future_ideas = _future_idea_items(worktree, active_sha)
    if not future_ideas:
        return
    for plan_path in plan_paths:
        try:
            plan_text = (worktree / plan_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"Planner ExecPlan을 읽을 수 없습니다: {plan_path}") from error
        plan_tokens = _idea_tokens(plan_text)
        if any(
            _contains_token_sequence(plan_tokens, idea)
            for idea in future_ideas
        ):
            raise RuntimeError(
                "docs/FUTURE_IDEAS.md 항목을 같은 계획 주기의 새 ExecPlan에 "
                f"포함할 수 없습니다: {plan_path}"
            )


def validate_planner_result(
    worktree: Path,
    active_sha: str,
    dirty_files: list[str],
    *,
    connection: sqlite3.Connection | None = None,
    repo: str | None = None,
    mutable_snapshot: MutableReadySnapshot | None = None,
) -> ValidatedPlanningResult | None:
    """Validate one create/revise/rename/delete planning batch.

    Scope and ExecPlan catalog errors are deliberately translated to a
    dedicated exception. Operational Git failures must retain their normal
    recovery boundary and never consume the Planner validation retry budget.
    """
    planned_files = sorted(set(dirty_files) | set(changed_files(worktree, active_sha)))
    if not planned_files:
        return None
    snapshot = mutable_snapshot or MutableReadySnapshot(
        repo=repo or "",
        active_sha=active_sha,
        plans=(),
        token=mutable_snapshot_token(repo or "", active_sha, ()),
    )
    if snapshot.active_sha != active_sha or (repo is not None and snapshot.repo != repo):
        raise PlannerValidationError("mutable READY snapshot이 planning 기준과 다릅니다.")

    plan_paths: list[str] = []
    base_paths: set[str] = set()
    current_plans: dict[str, dict[str, object]] = {}
    for changed_path in planned_files:
        if changed_path == "docs/FUTURE_IDEAS.md":
            if not (worktree / changed_path).is_file():
                raise PlannerValidationError(
                    "Planner는 docs/FUTURE_IDEAS.md를 삭제할 수 없습니다."
                )
            continue
        if not re.fullmatch(r"docs/plans/[^/]+\.md", changed_path):
            raise PlannerValidationError(
                "Planner는 docs/plans의 새 계획, mutable READY와 "
                "docs/FUTURE_IDEAS.md만 변경할 수 있습니다."
            )
        existed = git(
            worktree,
            "cat-file",
            "-e",
            f"{active_sha}:{changed_path}",
            check=False,
        ).returncode == 0
        if existed:
            if changed_path not in snapshot.by_path:
                if mutable_snapshot is None:
                    raise PlannerValidationError(
                        "Planner는 기존 ExecPlan을 수정·삭제할 수 없습니다: "
                        f"{changed_path}"
                    )
                raise PlannerValidationError(
                    "Planner는 snapshot 밖 ExecPlan을 변경할 수 없습니다: "
                    f"{changed_path}"
                )
            base_paths.add(changed_path)
        if not (worktree / changed_path).is_file():
            plan_paths.append(changed_path)
            continue
        try:
            planner_text = (worktree / changed_path).read_text(encoding="utf-8")
            parsed = _parse_execplan(
                worktree,
                worktree / changed_path,
                require_priority=not existed,
                allow_archive_fallback=False,
            )
        except (OSError, UnicodeError, RuntimeError) as error:
            raise PlannerValidationError(
                f"Planner ExecPlan을 읽을 수 없습니다: {changed_path} ({error})"
            ) from error
        if contains_guidance_sections(planner_text):
            raise PlannerValidationError("Planner는 AGENTS.md guidance를 제안할 수 없습니다.")
        current_plans[changed_path] = parsed
        plan_paths.append(changed_path)

    try:
        catalog = load_execplan_catalog(worktree)
        plans: list[tuple[str, dict[str, object]]] = []
        revisions: list[PlanRevision] = []
        deletions: list[PlanDeletion] = []
        renamed_ids: set[str] = set()
        displaced_paths: set[str] = set()
        for path in base_paths:
            original = snapshot.by_path.get(path)
            if original is None:
                raise RuntimeError(
                    f"Planner는 snapshot 밖 ExecPlan을 변경할 수 없습니다: {path}"
                )
            current = current_plans.get(path)
            if current is None or str(current["id"]) != original.plan_id:
                displaced_paths.add(path)

        for path, parsed in current_plans.items():
            plan_id = str(parsed["id"])
            path_identity = snapshot.by_path.get(path)
            if path_identity is not None and plan_id == path_identity.plan_id:
                revisions.append(PlanRevision(path, parsed, path_identity))
                continue
            original = snapshot.by_id.get(plan_id)
            if original is not None:
                if original.path not in displaced_paths or plan_id in renamed_ids:
                    raise RuntimeError(
                        f"ExecPlan rename identity가 모호합니다: {plan_id}"
                    )
                revisions.append(PlanRevision(path, parsed, original))
                renamed_ids.add(plan_id)
            else:
                plans.append((path, parsed))

        for path in sorted(displaced_paths):
            original = snapshot.by_path[path]
            if original.plan_id not in renamed_ids:
                deletions.append(PlanDeletion(original))

        if connection is not None and repo is not None and deletions:
            deleted_ids = {item.snapshot.plan_id for item in deletions}
            revised_ids = {item.snapshot.plan_id for item in revisions}
            for row in connection.execute(
                "SELECT plan_id, depends_on FROM exec_plans WHERE repo = ?",
                (repo,),
            ):
                plan_id = str(row["plan_id"])
                if plan_id in deleted_ids or plan_id in revised_ids:
                    continue
                dependencies = set(
                    _stored_string_list(row["depends_on"], "depends_on")
                )
                blocked = sorted(dependencies & deleted_ids)
                if blocked:
                    raise RuntimeError(
                        "삭제 계획을 snapshot 밖 실행 catalog가 참조합니다: "
                        f"{plan_id} -> {', '.join(blocked)}"
                    )

        new_plan_paths = [path for path, _plan in plans]
        _validate_future_idea_separation(worktree, active_sha, new_plan_paths)
        new_ids = {str(plan["id"]) for _, plan in plans}
        reserved_ids = set(catalog) - new_ids
        reserved_ids.update(plan.plan_id for plan in snapshot.plans)
        if connection is not None and repo is not None:
            reserved_ids.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT plan_id FROM exec_plans WHERE repo = ? "
                    "UNION SELECT plan_id FROM execplan_cancellations WHERE repo = ?",
                    (repo, repo),
                )
            )
        archived_directory = worktree / "docs/plans/archive"
        if archived_directory.is_dir():
            for archived in archived_directory.glob("*.md"):
                match = re.match(r"\[(G-\d{3,})\]", archived.name)
                if match is not None:
                    reserved_ids.add(match.group(1))
        if new_ids & reserved_ids:
            raise RuntimeError(
                "새 ExecPlan ID가 기존 또는 취소된 ID를 재사용합니다: "
                + ", ".join(sorted(new_ids & reserved_ids))
            )
        existing_ids = [int(plan_id[2:]) for plan_id in reserved_ids]
        first_id = max(existing_ids, default=0) + 1
        expected = list(range(first_id, first_id + len(plans)))
        actual = sorted(int(str(plan["id"])[2:]) for _, plan in plans)
        if actual != expected:
            raise RuntimeError("새 ExecPlan ID는 기존 최대 번호 다음부터 연속적이어야 합니다.")
        return ValidatedPlanningResult(
            plans=tuple(
                sorted(plans, key=lambda item: int(str(item[1]["id"])[2:]))
            ),
            publication_paths=tuple(planned_files),
            publication_hashes=tuple(
                (path, file_hash(worktree / path))
                for path in planned_files
                if (worktree / path).is_file()
            ),
            revisions=tuple(
                sorted(revisions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            deletions=tuple(
                sorted(deletions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            mutable_snapshot=snapshot,
        )
    except RuntimeError as error:
        raise PlannerValidationError(str(error)) from error


def _stored_string_list(value: object, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise MaintenancePlanningError(
            f"{field} catalog 값이 올바른 JSON 배열이 아닙니다."
        ) from error
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise MaintenancePlanningError(
            f"{field} catalog 값에는 문자열 배열이 필요합니다."
        )
    return tuple(parsed)


def _ready_plan_snapshot_from_contents(
    row: sqlite3.Row,
    contents: bytes,
) -> ReadyPlanSnapshot:
    plan_id = str(row["plan_id"])
    path = str(row["path"])
    ownership = {
        "branch": row["branch"],
        "worktree": row["worktree"],
        "workspace reservation": int(row["workspace_owned"] or 0),
        "Worker session": row["worker_session_id"],
        "Reviewer session": row["reviewer_session_id"],
        "PR URL": row["pr_url"],
        "PR head": row["pr_head_oid"],
        "PR conflict recovery": int(row["pr_conflict_recovery"] or 0),
        "PR conflict head": row["pr_conflict_head_oid"],
        "PR conflict base": row["pr_conflict_base_oid"],
    }
    owned = [
        name
        for name, value in ownership.items()
        if value is not None and value != 0
    ]
    if not is_unowned_ready(row) or owned:
        detail = ", ".join(owned) if owned else str(row["status"])
        raise MaintenancePlanningError(
            f"유지보수 개정 대상은 무소유 READY 계획이어야 합니다: {plan_id} ({detail})"
        )
    plan_sha = str(row["current_revision_sha"] or row["plan_sha"] or "")
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", plan_sha) is None:
        raise MaintenancePlanningError(
            f"계획 revision SHA를 확인할 수 없습니다: {plan_id}"
        )
    content_hash = hashlib.sha256(contents).hexdigest()
    if content_hash != str(row["content_hash"]):
        raise MaintenancePlanningError(
            f"READY ExecPlan content hash가 catalog와 다릅니다: {path}"
        )
    try:
        text = contents.decode("utf-8")
        plan = published_execplan(path, text)
    except (RuntimeError, UnicodeError) as error:
        raise MaintenancePlanningError(str(error)) from error
    sources = _stored_string_list(row["sources"], "sources")
    dependencies = _stored_string_list(row["depends_on"], "depends_on")
    if (
        re.fullmatch(r"G-\d{3,}", str(plan["id"])) is None
        or str(plan["id"]) != plan_id
        or str(plan["title"]) != str(row["title"])
        or tuple(str(item) for item in plan["sources"]) != sources
        or tuple(str(item) for item in plan["depends_on"]) != dependencies
        or str(plan["priority"]) != str(row["priority"])
    ):
        raise MaintenancePlanningError(
            f"READY ExecPlan metadata가 catalog와 다릅니다: {path}"
        )
    return ReadyPlanSnapshot(
        plan_id=plan_id,
        path=path,
        title=str(plan["title"]),
        sources=sources,
        depends_on=dependencies,
        complexity=str(plan["complexity"]),
        risk=str(plan["risk"]),
        priority=str(plan["priority"]),
        content_hash=content_hash,
        plan_sha=plan_sha,
        text=text,
        current_complexity=row["current_complexity"],
        current_risk=row["current_risk"],
        current_metadata_recorded=True,
    )


def _assert_ready_plan_revision(
    repository: Path,
    active_revision: str,
    snapshot: ReadyPlanSnapshot,
    contents: bytes,
) -> None:
    """Bind a READY row to the Git revision that published its exact body."""

    try:
        is_ancestor = _git_commit_is_ancestor(
            repository, snapshot.plan_sha, active_revision
        )
        revision_contents = git_bytes(
            repository, "show", f"{snapshot.plan_sha}:{snapshot.path}"
        )
    except RuntimeError as error:
        raise MaintenancePlanningError(
            "READY ExecPlan plan revision SHA를 확인할 수 없습니다: "
            f"{snapshot.plan_id}"
        ) from error
    if not is_ancestor or revision_contents != contents:
        raise MaintenancePlanningError(
            "READY ExecPlan plan revision SHA가 현재 catalog 본문과 "
            f"일치하지 않습니다: {snapshot.plan_id}"
        )


def _git_path_matches_revision(
    worktree: Path,
    revision: str,
    path: str,
    *,
    cached: bool,
) -> bool:
    """Compare a Git path through its worktree or index checkout filters."""

    arguments = ["diff", "--quiet", "--no-ext-diff"]
    if cached:
        arguments.append("--cached")
    arguments.extend((revision, "--", path))
    result = git(worktree, *arguments, check=False)
    if result.returncode == 1:
        return False
    if result.returncode:
        raise RuntimeError(result.stdout[-6000:].strip())
    return True


def _git_path_has_standard_index_flags(worktree: Path, path: str) -> bool:
    """Require a normally tracked index entry before trusting Git diff output."""

    entries = git_bytes(worktree, "ls-files", "-v", "-z", "--", path).split(
        b"\0"
    )
    records = [entry for entry in entries if entry]
    return records == [b"H " + os.fsencode(path)]


def _ready_plan_snapshot(
    worktree: Path,
    active_sha: str,
    row: sqlite3.Row,
) -> ReadyPlanSnapshot:
    path = str(row["path"])
    target = worktree / path
    if not target.is_file():
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일을 확인할 수 없습니다: {path}"
        )
    try:
        base_contents = git_bytes(worktree, "show", f"{active_sha}:{path}")
        standard_index_flags = _git_path_has_standard_index_flags(worktree, path)
        checkout_matches = _git_path_matches_revision(
            worktree, active_sha, path, cached=False
        )
    except RuntimeError as error:
        raise MaintenancePlanningError(
            f"평가 기준 revision에서 READY ExecPlan을 확인할 수 없습니다: {path}"
        ) from error
    if not standard_index_flags or not checkout_matches:
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일이 평가 기준 revision과 다릅니다: {path}"
        )
    snapshot = _ready_plan_snapshot_from_contents(row, base_contents)
    _assert_ready_plan_revision(worktree, active_sha, snapshot, base_contents)
    try:
        index_matches = _git_path_matches_revision(
            worktree, active_sha, path, cached=True
        )
    except RuntimeError as error:
        raise MaintenancePlanningError(
            f"평가 기준 revision에서 READY ExecPlan을 확인할 수 없습니다: {path}"
        ) from error
    if not index_matches:
        raise MaintenancePlanningError(
            f"READY ExecPlan 파일이 평가 기준 revision과 다릅니다: {path}"
        )
    return snapshot


def _ready_plan_snapshot_at_revision(
    repository: Path,
    revision: str,
    row: sqlite3.Row,
) -> ReadyPlanSnapshot:
    path = str(row["path"])
    try:
        contents = git_bytes(repository, "show", f"{revision}:{path}")
    except RuntimeError as error:
        raise MaintenancePlanningError(
            f"보존된 publication 기준에서 READY ExecPlan을 확인할 수 없습니다: {path}"
        ) from error
    snapshot = _ready_plan_snapshot_from_contents(row, contents)
    _assert_ready_plan_revision(repository, revision, snapshot, contents)
    return snapshot


def _row_is_mutable_ready(row: sqlite3.Row) -> bool:
    """Keep the general Planner boundary conservative and evidence based."""

    if not is_unowned_ready(row):
        return False
    if any(row[field] is not None for field in _MUTABLE_READY_EVIDENCE_FIELDS):
        return False
    if any(
        int(row[field] or 0) != 0
        for field in (
            "workspace_owned",
            "retry_count",
            "pr_conflict_recovery",
        )
    ):
        return False
    return all(
        str(row[field] or "{}") == "{}"
        for field in (
            "verify_failure_history",
            "review_failure_history",
            "review_level_history",
        )
    )


def mutable_ready_snapshot(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    active_sha: str,
) -> MutableReadySnapshot:
    """Bind every never-started READY declaration to Git and SQLite bytes."""

    _ensure_execplans_schema(connection)
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        raise PlannerValidationError(
            "활성 maintenance cycle 중에는 일반 mutable READY snapshot을 만들 수 없습니다."
        )
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY' "
        "ORDER BY plan_id",
        (repo,),
    ).fetchall()
    plans: list[ReadyPlanSnapshot] = []
    for row in rows:
        if not _row_is_mutable_ready(row):
            continue
        try:
            plans.append(
                _ready_plan_snapshot_at_revision(
                    worktree, active_sha, row
                )
            )
        except MaintenancePlanningError as error:
            raise PlannerValidationError(str(error)) from error
    ordered = tuple(sorted(plans, key=lambda item: int(item.plan_id[2:])))
    return MutableReadySnapshot(
        repo=repo,
        active_sha=active_sha,
        plans=ordered,
        token=mutable_snapshot_token(repo, active_sha, ordered),
    )


def _assert_mutable_ready_snapshot_current(
    connection: sqlite3.Connection,
    snapshot: MutableReadySnapshot,
) -> None:
    """Compare-and-snapshot guard used immediately before publication."""

    if maintenance_state.MaintenanceRepository(connection).get(
        snapshot.repo
    ).phase is not maintenance_state.MaintenancePhase.IDLE:
        raise PlannerValidationError(
            "mutable READY snapshot 이후 maintenance cycle이 시작됐습니다."
        )
    for plan in snapshot.plans:
        if not plan.current_metadata_recorded:
            raise PlannerValidationError(
                f"mutable READY snapshot의 SQLite metadata 근거가 없습니다: {plan.plan_id}"
            )
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (snapshot.repo, plan.plan_id),
        ).fetchone()
        if row is None or not _row_is_mutable_ready(row):
            raise PlannerValidationError(
                f"mutable READY snapshot 이후 실행 상태가 변경됐습니다: {plan.plan_id}"
            )
        sources, dependencies, priority = _execplan_metadata(
            {
                "sources": list(plan.sources),
                "depends_on": list(plan.depends_on),
                "priority": plan.priority,
            }
        )
        current = (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            row["current_complexity"],
            row["current_risk"],
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
        )
        expected = (
            plan.path,
            plan.title,
            sources,
            dependencies,
            priority,
            plan.current_complexity,
            plan.current_risk,
            plan.content_hash,
            plan.plan_sha,
        )
        if current != expected:
            raise PlannerValidationError(
                f"mutable READY snapshot metadata가 변경됐습니다: {plan.plan_id}"
            )


def _maintenance_mode_and_rows(
    connection: sqlite3.Connection,
    repo: str,
    cycle: maintenance_state.MaintenanceCycle,
    active_sha: str,
) -> tuple[MaintenancePlanMode, list[sqlite3.Row]]:
    if cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
        if cycle.cycle_base_sha != active_sha:
            raise MaintenancePlanningError(
                "유지보수 assessment 기준 SHA가 planning worktree 기준과 "
                "일치하지 않습니다."
            )
        rows = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
        return MaintenancePlanMode.BARRIER, [row for row in rows if is_unowned_ready(row)]
    if cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
        rows = []
        for plan_id in cycle.revalidation_plan_ids:
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
                "WHERE h.repo = exec_plans.repo "
                "AND h.currently_archived = 1 "
                "AND h.plan_id = exec_plans.plan_id)",
                (repo, plan_id),
            ).fetchone()
            if row is None:
                raise MaintenancePlanningError(
                    f"재검증 대상 계획이 catalog에 없습니다: {plan_id}"
                )
            rows.append(row)
        return MaintenancePlanMode.REVALIDATION, rows
    raise MaintenancePlanningError(
        "활성 유지보수 assessment 또는 post-refactor revalidation 단계가 "
        "없어 기존 ExecPlan 수정 권한을 열 수 없습니다."
    )


def _maintenance_context(
    repo: str,
    active_sha: str,
    cycle: maintenance_state.MaintenanceCycle,
    mode: MaintenancePlanMode,
    snapshots: tuple[ReadyPlanSnapshot, ...],
) -> MaintenancePlanningContext:
    return MaintenancePlanningContext(
        repo=repo,
        mode=mode,
        active_sha=active_sha,
        cycle_revision=cycle.revision,
        cycle_phase=cycle.phase.value,
        eligible_plans=snapshots,
    )


def prepare_maintenance_planning_context(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    active_sha: str,
) -> MaintenancePlanningContext:
    """Bind guarded Planner authority to one active SQLite cycle revision."""

    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    mode, rows = _maintenance_mode_and_rows(
        connection, repo, cycle, active_sha
    )
    snapshots = tuple(
        _ready_plan_snapshot(worktree, active_sha, row) for row in rows
    )
    return _maintenance_context(repo, active_sha, cycle, mode, snapshots)


def _maintenance_context_at_revision(
    connection: sqlite3.Connection,
    repo: str,
    repository: Path,
    active_sha: str,
    cycle: maintenance_state.MaintenanceCycle,
) -> MaintenancePlanningContext:
    mode, rows = _maintenance_mode_and_rows(
        connection, repo, cycle, active_sha
    )
    snapshots = tuple(
        _ready_plan_snapshot_at_revision(repository, active_sha, row)
        for row in rows
    )
    return _maintenance_context(repo, active_sha, cycle, mode, snapshots)


def _assert_maintenance_context_current(
    connection: sqlite3.Connection,
    context: MaintenancePlanningContext,
) -> None:
    cycle = maintenance_state.MaintenanceRepository(connection).get(context.repo)
    if cycle.phase.value != context.cycle_phase or cycle.revision != context.cycle_revision:
        raise MaintenancePlanningError(
            "유지보수 주기 단계 또는 revision이 Planner 실행 뒤 변경됐습니다."
        )
    if cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
        expected_mode = MaintenancePlanMode.BARRIER
        _mode, rows = _maintenance_mode_and_rows(
            connection, context.repo, cycle, context.active_sha
        )
        expected_plan_ids = tuple(str(row["plan_id"]) for row in rows)
    elif cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
        expected_mode = MaintenancePlanMode.REVALIDATION
        expected_plan_ids = cycle.revalidation_plan_ids
    else:
        raise MaintenancePlanningError(
            "현재 유지보수 단계에는 기존 ExecPlan 수정 권한이 없습니다."
        )
    context_plan_ids = tuple(plan.plan_id for plan in context.eligible_plans)
    if context.mode is not expected_mode or context_plan_ids != expected_plan_ids:
        raise MaintenancePlanningError(
            "maintenance mode 또는 eligible READY snapshot이 SQLite 단계와 "
            "일치하지 않습니다."
        )
    for snapshot in context.eligible_plans:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (context.repo, snapshot.plan_id),
        ).fetchone()
        if row is None:
            raise MaintenancePlanningError(
                f"READY snapshot 계획이 catalog에서 사라졌습니다: {snapshot.plan_id}"
            )
        if not is_unowned_ready(row):
            raise MaintenancePlanningError(
                f"READY snapshot의 실행 이력 또는 소유권이 변경됐습니다: {snapshot.plan_id}"
            )
        ownership_values = (
            row["branch"],
            row["worktree"],
            int(row["workspace_owned"] or 0),
            row["worker_session_id"],
            row["reviewer_session_id"],
            row["pr_url"],
            row["pr_head_oid"],
            int(row["pr_conflict_recovery"] or 0),
            row["pr_conflict_head_oid"],
            row["pr_conflict_base_oid"],
        )
        current = (
            str(row["status"]),
            str(row["path"]),
            str(row["title"]),
            _stored_string_list(row["sources"], "sources"),
            _stored_string_list(row["depends_on"], "depends_on"),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            ownership_values,
        )
        expected = (
            "READY",
            snapshot.path,
            snapshot.title,
            snapshot.sources,
            snapshot.depends_on,
            snapshot.priority,
            snapshot.content_hash,
            snapshot.plan_sha,
            (None, None, 0, None, None, None, None, 0, None, None),
        )
        if current != expected:
            raise MaintenancePlanningError(
                "READY snapshot의 상태, metadata 또는 소유권이 변경됐습니다: "
                f"{snapshot.plan_id}"
            )


def validate_maintenance_planner_result(
    connection: sqlite3.Connection,
    context: MaintenancePlanningContext,
    worktree: Path,
    dirty_files: list[str],
    *,
    refactoring_plan_ids: tuple[str, ...] | None = None,
    allow_general_plans: bool = False,
) -> ValidatedPlanningResult | None:
    """Validate output without consuming correction retries on Git failures."""

    # Read Git outside the output-validation boundary. A tree query distinguishes
    # absent paths from an unreadable repository without interpreting cat-file's
    # nonzero status as proof that a path is new.
    planned_files = sorted(
        set(dirty_files) | set(changed_files(worktree, context.active_sha))
    )
    base_paths = _git_tree_entries(worktree, context.active_sha)
    path_moves_by_sha: dict[str, dict[str, str]] = {}
    if context.mode is MaintenancePlanMode.REVALIDATION:
        for snapshot in context.eligible_plans:
            if snapshot.path not in planned_files:
                continue
            if snapshot.plan_sha not in path_moves_by_sha:
                tree = _git_tree_entries(worktree, snapshot.plan_sha)
                # Only unique, byte-identical deleted -> added files establish
                # a move. Similarity, copies and ambiguous replacements cannot
                # prove preservation of the original acceptance criterion.
                old_blobs: dict[bytes, list[bytes]] = {}
                new_blobs: dict[bytes, list[bytes]] = {}
                for entries, grouped in ((tree, old_blobs), (base_paths, new_blobs)):
                    for name, (kind, oid) in entries.items():
                        if kind == b"blob":
                            grouped.setdefault(oid, []).append(name)
                moves = {}
                for oid, old_names in old_blobs.items():
                    new_names = new_blobs.get(oid, [])
                    if len(old_names) == len(new_names) == 1:
                        old, new = old_names[0], new_names[0]
                        if old not in base_paths and new not in tree:
                            moves[old.decode("utf-8")] = new.decode("utf-8")
                path_moves_by_sha[snapshot.plan_sha] = moves
    try:
        _assert_maintenance_context_current(connection, context)
        if not planned_files:
            return None
        eligible = context.eligible_by_path
        planned_contents: dict[str, tuple[bytes, str]] = {}
        new_paths: list[str] = []
        revised_paths: list[str] = []
        for path in planned_files:
            if path == "docs/FUTURE_IDEAS.md" and allow_general_plans:
                if not (worktree / path).is_file():
                    raise MaintenancePlanningError(
                        "Planner는 docs/FUTURE_IDEAS.md를 삭제할 수 없습니다."
                    )
                planned_contents[path] = ((worktree / path).read_bytes(), "")
                continue
            if not re.fullmatch(r"docs/plans/[^/]+\.md", path):
                raise MaintenancePlanningError(
                    "유지보수 Planner는 검증된 docs/plans batch만 변경할 수 있습니다."
                )
            if not (worktree / path).is_file():
                raise MaintenancePlanningError(
                    f"유지보수 Planner는 ExecPlan을 삭제·rename할 수 없습니다: {path}"
                )
            contents = (worktree / path).read_bytes()
            text = contents.decode("utf-8")
            planned_contents[path] = (contents, text)
            if contains_guidance_sections(text):
                raise MaintenancePlanningError(
                    "유지보수 Planner는 AGENTS.md guidance를 제안할 수 없습니다."
                )
            existed = path.encode("utf-8") in base_paths
            if existed:
                if path not in eligible:
                    raise MaintenancePlanningError(
                        f"SQLite snapshot에 없는 기존 ExecPlan은 수정할 수 없습니다: {path}"
                    )
                revised_paths.append(path)
            else:
                if context.mode is not MaintenancePlanMode.BARRIER:
                    raise MaintenancePlanningError(
                        "post-refactor revalidation은 새 ExecPlan을 만들 수 없습니다."
                    )
                new_paths.append(path)

        try:
            catalog_snapshot = load_execplan_snapshot(
                worktree, validate_sources=False
            )
        except RuntimeError as error:
            raise MaintenancePlanningError(str(error)) from error
        catalog = catalog_snapshot.active_metadata()
        new_plans: list[tuple[str, dict[str, object]]] = []
        for path in new_paths:
            try:
                plan = _parse_execplan(
                    worktree,
                    worktree / path,
                    require_priority=True,
                    allow_archive_fallback=False,
                )
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            try:
                validated_plan = published_execplan(path, planned_contents[path][1])
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            if validated_plan != plan:
                raise MaintenancePlanningError(
                    f"검증 중 maintenance ExecPlan이 변경됐습니다: {path}"
                )
            new_plans.append((path, validated_plan))
        new_ids = {str(plan["id"]) for _, plan in new_plans}
        declared_refactoring = (
            new_ids
            if refactoring_plan_ids is None
            else set(refactoring_plan_ids)
        )
        if not declared_refactoring <= new_ids:
            unknown = sorted(declared_refactoring - new_ids)
            raise MaintenancePlanningError(
                "MAINTENANCE_RESULT가 생성되지 않은 refactoring plan을 참조합니다: "
                + ", ".join(unknown)
            )
        general_ids = new_ids - declared_refactoring
        if general_ids and not allow_general_plans:
            raise MaintenancePlanningError(
                "maintenance-only 호출은 일반 기능 ExecPlan을 만들 수 없습니다: "
                + ", ".join(sorted(general_ids))
            )
        for path, plan in new_plans:
            if str(plan["id"]) in declared_refactoring:
                validate_refactoring_plan(path, plan, planned_contents[path][1])
        if allow_general_plans:
            _validate_future_idea_separation(worktree, context.active_sha, new_paths)

        existing_ids = [
            int(plan_id[2:])
            for plan_id in catalog_snapshot.all
            if plan_id not in new_ids
        ]
        first_id = max(existing_ids, default=0) + 1
        expected_ids = list(range(first_id, first_id + len(new_plans)))
        actual_ids = sorted(int(str(plan["id"])[2:]) for _, plan in new_plans)
        if actual_ids != expected_ids:
            raise MaintenancePlanningError(
                "새 리팩터링 ExecPlan ID는 기존 최대 번호 다음부터 연속적이어야 합니다."
            )

        refactoring_plans = [
            item for item in new_plans
            if str(item[1]["id"]) in declared_refactoring
        ]
        terminals = terminal_new_plan_ids(refactoring_plans)
        if context.mode is MaintenancePlanMode.BARRIER:
            if refactoring_plans:
                if set(revised_paths) != set(eligible):
                    missing = sorted(set(eligible) - set(revised_paths))
                    raise MaintenancePlanningError(
                        "barrier mode는 모든 eligible READY 계획에 말단 dependency를 "
                        f"추가해야 합니다: {', '.join(missing)}"
                    )
                for _path, plan in new_plans:
                    if str(plan["id"]) in general_ids and not set(terminals) <= set(plan["depends_on"]):
                        raise MaintenancePlanningError(
                            "combined 일반 계획은 리팩터링 DAG 말단 뒤에 있어야 합니다: "
                            + str(plan["id"])
                        )
                statuses = {
                    str(row["plan_id"]): str(row["status"])
                    for row in connection.execute(
                        "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                        (context.repo,),
                    )
                }
                for _path, plan in refactoring_plans:
                    for dependency in plan["depends_on"]:
                        if dependency not in declared_refactoring and statuses.get(str(dependency)) != "MERGED":
                            raise MaintenancePlanningError(
                                "리팩터링 DAG의 외부 dependency는 이미 MERGED여야 합니다: "
                                f"{plan['id']} -> {dependency}"
                            )
            elif revised_paths:
                raise MaintenancePlanningError(
                    "리팩터링 계획이 없는 assessment는 READY 장벽을 수정할 수 없습니다."
                )

        revisions: list[PlanRevision] = []
        for path in revised_paths:
            snapshot = eligible[path]
            plan = catalog[snapshot.plan_id]
            text = planned_contents[path][1]
            try:
                validated_plan = published_execplan(path, text)
            except RuntimeError as error:
                raise MaintenancePlanningError(str(error)) from error
            if validated_plan != plan:
                raise MaintenancePlanningError(
                    f"검증 중 maintenance ExecPlan이 변경됐습니다: {path}"
                )
            if context.mode is MaintenancePlanMode.BARRIER:
                validate_barrier_revision(
                    snapshot, path, validated_plan, text, terminals
                )
            else:
                validate_revalidation_revision(
                    snapshot, path, validated_plan, text,
                    path_moves=path_moves_by_sha[snapshot.plan_sha],
                )
            revisions.append(PlanRevision(path, validated_plan, snapshot))

        return ValidatedPlanningResult(
            plans=tuple(
                sorted(new_plans, key=lambda item: int(str(item[1]["id"])[2:]))
            ),
            publication_paths=tuple(planned_files),
            publication_hashes=tuple(
                (path, hashlib.sha256(planned_contents[path][0]).hexdigest())
                for path in planned_files
            ),
            revisions=tuple(
                sorted(revisions, key=lambda item: int(item.snapshot.plan_id[2:]))
            ),
            maintenance_context=context,
            refactoring_plan_ids=tuple(
                sorted(declared_refactoring, key=lambda value: int(value[2:]))
            ),
            revalidation_plan_ids=(
                tuple(
                    sorted(
                        {revision.snapshot.plan_id for revision in revisions}
                        | general_ids,
                        key=lambda value: int(value[2:]),
                    )
                ) if refactoring_plans else ()
            ),
        )
    except MaintenancePlanningError as error:
        raise PlannerValidationError(str(error)) from error
    except (OSError, UnicodeError) as error:
        raise PlannerValidationError(str(error)) from error


def _plan_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", path)
    return match.group(1) if match else None


def _register_execplan_rows(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    plan_commit: str,
) -> None:
    """Apply the new-row part shared by normal and maintenance batches."""

    _ensure_execplans_schema(connection)
    for path, plan in plans:
        execplan_catalog_state.register_published_plan_row(
            connection, repo, path, plan, file_hash(worktree / path), plan_commit,
        )


def register_execplans(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    plan_commit: str,
    planning_fingerprint: str,
) -> None:
    """Register one published Planner batch atomically and idempotently."""

    with connection:
        _register_execplan_rows(connection, repo, worktree, plans, plan_commit)
        runtime_state.RuntimeStateRepository(connection).clear(
            repo,
            processed_sha=plan_commit,
            planning_fingerprint=planning_fingerprint,
            enqueue=False,
        )


def _planning_result_manifest(result: ValidatedPlanningResult) -> str:
    snapshot = result.mutable_snapshot
    if snapshot is None:
        raise PlannerValidationError(
            "일반 planning batch에 mutable READY snapshot이 없습니다."
        )
    payload = {
        "version": 1,
        "publication_paths": list(result.publication_paths),
        "publication_hashes": dict(result.publication_hashes),
        "plans": [[path, plan] for path, plan in result.plans],
        "revisions": [
            {
                "path": revision.path,
                "plan": dict(revision.plan),
                "snapshot": snapshot_to_dict(revision.snapshot),
            }
            for revision in result.revisions
        ],
        "deletions": [
            snapshot_to_dict(deletion.snapshot)
            for deletion in result.deletions
        ],
        "mutable_snapshot": {
            "repo": snapshot.repo,
            "active_sha": snapshot.active_sha,
            "token": snapshot.token,
            "plans": [snapshot_to_dict(plan) for plan in snapshot.plans],
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _planning_result_from_manifest(manifest: str) -> ValidatedPlanningResult:
    try:
        payload = json.loads(manifest)
        if payload.get("version") != 1:
            raise ValueError("unsupported version")
        snapshot_value = payload["mutable_snapshot"]
        snapshot_plans = tuple(
            snapshot_from_dict(item) for item in snapshot_value["plans"]
        )
        snapshot = MutableReadySnapshot(
            repo=str(snapshot_value["repo"]),
            active_sha=str(snapshot_value["active_sha"]),
            plans=snapshot_plans,
            token=str(snapshot_value["token"]),
        )
        expected_token = mutable_snapshot_token(
            snapshot.repo, snapshot.active_sha, snapshot.plans
        )
        if snapshot.token != expected_token:
            raise ValueError("snapshot token mismatch")
        plans = tuple(
            (str(item[0]), dict(item[1])) for item in payload["plans"]
        )
        revisions = tuple(
            PlanRevision(
                str(item["path"]),
                dict(item["plan"]),
                snapshot_from_dict(item["snapshot"]),
            )
            for item in payload["revisions"]
        )
        deletions = tuple(
            PlanDeletion(snapshot_from_dict(item))
            for item in payload["deletions"]
        )
        publication_hashes = tuple(
            sorted(
                (str(path), str(value))
                for path, value in payload["publication_hashes"].items()
            )
        )
        return ValidatedPlanningResult(
            plans=plans,
            publication_paths=tuple(
                str(path) for path in payload["publication_paths"]
            ),
            publication_hashes=publication_hashes,
            revisions=revisions,
            deletions=deletions,
            mutable_snapshot=snapshot,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PlannerValidationError(
            "보존된 general planning publication manifest가 유효하지 않습니다."
        ) from error


def _validate_general_planning_commit(
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> None:
    snapshot = result.mutable_snapshot
    if snapshot is None:
        raise PlannerValidationError("일반 planning snapshot이 없습니다.")
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if head != plan_commit:
        raise PlannerValidationError(
            "general planning worktree HEAD가 보존할 commit과 다릅니다."
        )
    ancestry = git(
        worktree, "rev-list", "--parents", "-n", "1", plan_commit
    ).stdout.split()
    if len(ancestry) != 2 or ancestry[1] != snapshot.active_sha:
        raise PlannerValidationError(
            "general planning batch는 시작 기준 바로 다음 단일 commit이어야 합니다."
        )
    committed_paths = tuple(
        sorted(
            path
            for path in git(
                worktree,
                "diff",
                "--no-renames",
                "--name-only",
                snapshot.active_sha,
                plan_commit,
            ).stdout.splitlines()
            if path
        )
    )
    if committed_paths != tuple(sorted(result.publication_paths)):
        raise PlannerValidationError(
            "general planning commit에 검증 batch 밖의 변경이 있습니다."
        )
    hashes = dict(result.publication_hashes)
    if len(hashes) != len(result.publication_hashes):
        raise PlannerValidationError("general planning file hash가 중복됐습니다.")
    expected_existing = {
        path
        for path in result.publication_paths
        if git(
            worktree,
            "cat-file",
            "-e",
            f"{plan_commit}:{path}",
            check=False,
        ).returncode
        == 0
    }
    if set(hashes) != expected_existing:
        raise PlannerValidationError(
            "general planning 결과의 file hash 또는 삭제 manifest가 불완전합니다."
        )
    for path, expected_hash in hashes.items():
        contents = git_bytes(worktree, "show", f"{plan_commit}:{path}")
        if hashlib.sha256(contents).hexdigest() != expected_hash:
            raise PlannerValidationError(
                f"general planning 파일이 검증 후 변경됐습니다: {path}"
            )
    for path in set(result.publication_paths) - expected_existing:
        if git(
            worktree, "cat-file", "-e", f"{plan_commit}:{path}", check=False
        ).returncode == 0:
            raise PlannerValidationError(
                f"삭제 manifest 경로가 planning commit에 남아 있습니다: {path}"
            )


def record_general_planning_publication_intent(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> GeneralPlanningPublication:
    snapshot = result.mutable_snapshot
    if snapshot is None or snapshot.repo != repo:
        raise PlannerValidationError(
            "SQLite-bound mutable READY snapshot이 publication 대상과 다릅니다."
        )
    _assert_mutable_ready_snapshot_current(connection, snapshot)
    _validate_general_planning_commit(worktree, result, plan_commit)
    if is_loop_manager_repository(Path(repo)):
        from loop_manager_internal.documents import document_drafts
        project = connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if project is None:
            raise PlannerValidationError("문서 계획의 등록 프로젝트가 없습니다.")
        drafts = document_drafts.DocumentDraftRepository(connection)
        try:
            for path, plan in result.plans:
                plan_text = document_publication.read_plan_text(worktree / path)
                claim = document_publication.plan_claim(plan_text)
                if claim is not None:
                    try:
                        draft = drafts.get(claim)
                        document_publication.validate_document_plan(plan_text, plan, draft)
                        drafts.assert_current(draft, Path(repo), str(project["base_branch"]))
                    except document_drafts.DocumentDraftError as error:
                        drafts.record_publication_error(claim, str(error))
                        raise
        except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
            raise PlannerValidationError(str(error)) from error
    manifest = _planning_result_manifest(result)
    manifest_hash = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    return planning_publication_state.record_general_planning_publication(
        connection, GeneralPlanningPublication(
            repo, "prepared", snapshot.active_sha, plan_commit, snapshot.token,
            specification_fingerprint, planning_fingerprint, manifest, manifest_hash,
        ),
    )


def _general_planning_batch_already_applied(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> bool:
    project = connection.execute(
        "SELECT processed_sha, specification_fingerprint, planning_fingerprint "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or tuple(project) != (
        plan_commit,
        specification_fingerprint,
        planning_fingerprint,
    ):
        return False
    for path, plan in result.plans:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, str(plan["id"])),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(plan)
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
        ) != (
            path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            file_hash(worktree / path),
            plan_commit,
        ):
            return False
    for revision in result.revisions:
        plan = dict(revision.plan)
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, revision.snapshot.plan_id),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(plan)
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
            bool(row["ever_started"]),
        ) != (
            revision.path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            file_hash(worktree / revision.path),
            plan_commit,
            "READY",
            False,
        ):
            return False
    for deletion in result.deletions:
        original = deletion.snapshot
        row = connection.execute(
            "SELECT * FROM execplan_cancellations "
            "WHERE repo = ? AND plan_id = ?",
            (repo, original.plan_id),
        ).fetchone()
        if row is None or (
            str(row["path"]),
            str(row["content_hash"]),
            str(row["plan_sha"]),
            str(row["deletion_commit"]),
        ) != (
            original.path,
            original.content_hash,
            original.plan_sha,
            plan_commit,
        ):
            return False
        if connection.execute(
            "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, original.plan_id),
        ).fetchone() is not None:
            return False
    return True


def _apply_general_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> None:
    snapshot = result.mutable_snapshot
    if snapshot is None or snapshot.repo != repo:
        raise PlannerValidationError("general planning snapshot 대상이 다릅니다.")
    with runtime_state._transaction(connection):
        intent = general_planning_publication(connection, repo)
        if intent is None:
            if _general_planning_batch_already_applied(
                connection,
                repo,
                worktree,
                result,
                plan_commit,
                specification_fingerprint,
                planning_fingerprint,
            ):
                return
            raise PlannerValidationError(
                "보존된 general planning publication intent가 없습니다."
            )
        if (
            intent.stage,
            intent.base_sha,
            intent.commit_sha,
            intent.snapshot_token,
            intent.specification_fingerprint,
            intent.planning_fingerprint,
        ) != (
            "prepared",
            snapshot.active_sha,
            plan_commit,
            snapshot.token,
            specification_fingerprint,
            planning_fingerprint,
        ):
            raise PlannerValidationError(
                "보존된 general planning publication intent가 batch와 다릅니다."
            )
        _assert_mutable_ready_snapshot_current(connection, snapshot)
        planning_publication_state.apply_mutable_catalog_changes(
            connection, repo, deletions=result.deletions, revisions=result.revisions,
            snapshot_token=snapshot.token, plan_commit=plan_commit,
            content_hash=lambda path: file_hash(worktree / path),
        )
        _register_execplan_rows(
            connection, repo, worktree, list(result.plans), plan_commit
        )
        if is_loop_manager_repository(Path(repo)):
            from loop_manager_internal.documents import document_drafts
            drafts = document_drafts.DocumentDraftRepository(connection)
            try:
                for path, plan in result.plans:
                    plan_text = document_publication.read_plan_text(worktree / path)
                    claim = document_publication.plan_claim(plan_text)
                    if claim is not None:
                        draft = drafts.get(claim)
                        document_publication.validate_document_plan(plan_text, plan, draft)
                        drafts.bind_plan(repo, claim, str(plan["id"]))
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                raise PlannerValidationError(str(error)) from error
        _clear_active_in_current_transaction(
            connection,
            repo,
            processed_sha=plan_commit,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )
        planning_publication_state.delete_general_planning_publication(connection, repo)


def publish_general_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    specification_fingerprint: str,
    planning_fingerprint: str,
) -> None:
    remote = git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if remote != plan_commit:
        raise PlannerValidationError(
            "general planning commit의 fast-forward push를 origin/base에서 확인할 수 없습니다."
        )
    _validate_general_planning_commit(worktree, result, plan_commit)
    _apply_general_planning_batch(
        connection,
        repo,
        worktree,
        result,
        plan_commit,
        specification_fingerprint,
        planning_fingerprint,
    )


def recover_general_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> str:
    """Return pending, applied, or restart for one durable general intent."""

    intent = general_planning_publication(connection, repo)
    if intent is None:
        raise PlannerValidationError(
            "복구할 general planning publication intent가 없습니다."
        )
    result = _planning_result_from_manifest(intent.manifest)
    if result.mutable_snapshot is None or (
        result.mutable_snapshot.token != intent.snapshot_token
    ):
        raise PlannerValidationError(
            "publication intent와 mutable READY snapshot token이 다릅니다."
        )
    _validate_general_planning_commit(worktree, result, intent.commit_sha)
    remote = git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if remote == intent.base_sha:
        return "pending"
    if _git_commit_is_ancestor(worktree, intent.commit_sha, remote):
        if remote != intent.commit_sha:
            _assert_remote_preserves_maintenance_batch(
                worktree,
                intent.base_sha,
                intent.commit_sha,
                remote,
                reject_catalog_drift=True,
            )
        _apply_general_planning_batch(
            connection,
            repo,
            worktree,
            result,
            intent.commit_sha,
            intent.specification_fingerprint,
            intent.planning_fingerprint,
        )
        return "applied"
    if not _git_commit_is_ancestor(worktree, intent.base_sha, remote):
        raise PlannerValidationError(
            "general planning publication base가 origin/base 이력에 없습니다."
        )
    with runtime_state._transaction(connection):
        planning_publication_state.delete_general_planning_publication(connection, repo)
        _clear_active_in_current_transaction(connection, repo)
    return "restart"


def validate_maintenance_publication_base(
    connection: sqlite3.Connection,
    repo: str,
    repository: Path,
    base_branch: str,
    context: MaintenancePlanningContext,
) -> None:
    """Invalidate and restart a stale maintenance result before publication."""

    if context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )

    current = git(
        repository,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    if current == context.active_sha:
        return

    cycle_repository = maintenance_state.MaintenanceRepository(connection)
    expected_cycle = cycle_repository.get(repo)
    _assert_maintenance_context_current(connection, context)
    with connection:
        restarted = cycle_repository.restart_stale_planning_context(
            repo,
            expected=expected_cycle,
            new_base_sha=current,
        )
        _clear_active_in_current_transaction(connection, repo)
    raise MaintenancePlanningRestart(current, restarted.revision)


def _validate_maintenance_planning_commit(
    repository: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> None:
    context = result.maintenance_context
    if context is None:
        raise MaintenancePlanningError(
            "일반 Planner 결과를 maintenance publication API에 전달할 수 없습니다."
        )
    head = git(repository, "rev-parse", "HEAD").stdout.strip()
    if head != plan_commit:
        raise MaintenancePlanningError(
            "maintenance planning worktree HEAD가 보존할 commit과 다릅니다."
        )
    ancestry = git(
        repository,
        "rev-list",
        "--parents",
        "-n",
        "1",
        plan_commit,
    ).stdout.split()
    if len(ancestry) != 2 or ancestry[1] != context.active_sha:
        raise MaintenancePlanningError(
            "maintenance batch는 평가 기준 바로 다음의 단일 planning commit이어야 합니다."
        )
    committed_paths = tuple(
        sorted(
            line
            for line in git(
                repository,
                "diff",
                "--name-only",
                context.active_sha,
                plan_commit,
            ).stdout.splitlines()
            if line
        )
    )
    if committed_paths != tuple(sorted(result.publication_paths)):
        raise MaintenancePlanningError(
            "maintenance planning commit에 검증 batch 밖의 변경이 있습니다."
        )
    validated_hashes = dict(result.publication_hashes)
    if (
        len(validated_hashes) != len(result.publication_hashes)
        or set(validated_hashes) != set(result.publication_paths)
    ):
        raise MaintenancePlanningError(
            "maintenance planning 결과의 검증된 file hash가 불완전합니다."
        )
    expected = {
        path: plan for path, plan in result.plans
    }
    expected.update({revision.path: revision.plan for revision in result.revisions})
    load_execplan_catalog(repository, validate_sources=False)
    for path in result.publication_paths:
        local_contents = (repository / path).read_bytes()
        remote_contents = git_bytes(repository, "show", f"{plan_commit}:{path}")
        expected_hash = validated_hashes[path]
        if (
            hashlib.sha256(local_contents).hexdigest() != expected_hash
            or hashlib.sha256(remote_contents).hexdigest() != expected_hash
        ):
            raise MaintenancePlanningError(
                "maintenance ExecPlan이 Planner 결과 검증 후 "
                f"변경됐습니다: {path}"
            )
        if path == "docs/FUTURE_IDEAS.md":
            continue
        parsed = _parse_execplan(repository, repository / path, validate_sources=False)
        if parsed != expected[path]:
            raise MaintenancePlanningError(
                f"검증 뒤 maintenance ExecPlan metadata가 변경됐습니다: {path}"
            )


def _validate_maintenance_publication_commit(
    repository: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    *,
    require_remote_confirmation: bool = True,
) -> None:
    context = result.maintenance_context
    if context is None:
        raise MaintenancePlanningError(
            "일반 Planner 결과를 maintenance publication API에 전달할 수 없습니다."
        )
    remote = git(
        repository,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    expected_remote = (
        plan_commit if require_remote_confirmation else context.active_sha
    )
    if remote != expected_remote:
        detail = "fast-forward push" if require_remote_confirmation else "평가 기준"
        raise MaintenancePlanningError(
            f"maintenance planning commit의 {detail}을 origin/base에서 "
            "확인할 수 없습니다."
        )
    _validate_maintenance_planning_commit(repository, result, plan_commit)


def _git_commit_is_ancestor(
    repository: Path,
    older: str,
    newer: str,
) -> bool:
    result = git(
        repository, "merge-base", "--is-ancestor", older, newer, check=False
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError("maintenance publication Git ancestry 확인 실패.")
    return result.returncode == 0


def _assert_remote_preserves_maintenance_batch(
    repository: Path,
    base_sha: str,
    plan_commit: str,
    remote_sha: str,
    *,
    reject_catalog_drift: bool = False,
) -> None:
    """Confirm a published batch remains byte-identical at a newer remote tip."""

    if not _git_commit_is_ancestor(repository, plan_commit, remote_sha):
        raise MaintenancePlanningError(
            "보존된 maintenance planning commit이 origin/base 이력에 없습니다."
        )
    base_tree = _git_tree_entries(repository, base_sha)
    plan_tree = _git_tree_entries(repository, plan_commit)
    remote_tree = _git_tree_entries(repository, remote_sha)
    batch_paths = {
        path
        for path in base_tree.keys() | plan_tree.keys()
        if base_tree.get(path) != plan_tree.get(path)
    }
    changed_paths = sorted(
        path
        for path in batch_paths
        if plan_tree.get(path) != remote_tree.get(path)
    )
    if changed_paths:
        rendered = ", ".join(
            path.decode("utf-8", errors="replace") for path in changed_paths
        )
        raise MaintenancePlanningError(
            "게시된 maintenance batch 경로가 후속 원격 commit에서 변경됐습니다: "
            f"{rendered}"
        )

    if not reject_catalog_drift:
        return

    plans_prefix = b"docs/plans/"
    catalog_changes = sorted(
        path
        for path in plan_tree.keys() | remote_tree.keys()
        if path.startswith(plans_prefix)
        and path.endswith(b".md")
        and b"/" not in path[len(plans_prefix) :]
        and plan_tree.get(path) != remote_tree.get(path)
    )
    if catalog_changes:
        rendered = ", ".join(
            path.decode("utf-8", errors="replace") for path in catalog_changes
        )
        raise MaintenancePlanningError(
            "게시 뒤 원격 ExecPlan catalog가 후속 commit에서 변경됐습니다: "
            f"{rendered}"
        )


def record_maintenance_publication_intent(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> maintenance_state.MaintenanceCycle:
    """Persist a validated local batch before attempting its remote push."""

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    _assert_maintenance_context_current(connection, context)
    _validate_maintenance_publication_commit(
        worktree,
        base_branch,
        result,
        plan_commit,
        require_remote_confirmation=False,
    )
    expected_phase = (
        maintenance_state.MaintenancePhase.ASSESSMENT
        if context.mode is MaintenancePlanMode.BARRIER
        else maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
    )
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    evidence = None
    if (expected_phase is maintenance_state.MaintenancePhase.ASSESSMENT
        and cycle.file_assessment_start_merged_count is not None
        and (not is_loop_manager_repository(Path(repo))
             or cycle.full_assessment_completed)):
        evidence = maintenance_file_publication.collect(
            connection, repo, worktree, base_branch,
            context.cycle_revision, context.active_sha,
        )
    with runtime_state._transaction(connection):
        intent = maintenance_state.MaintenanceRepository(
            connection
        ).record_publication_intent(
            repo,
            expected_phase=expected_phase,
            expected_revision=context.cycle_revision,
            base_sha=context.active_sha,
            commit_sha=plan_commit,
            refactoring_plan_ids=result.refactoring_plan_ids,
            revalidation_plan_ids=result.revalidation_plan_ids,
        )
        if evidence is not None:
            maintenance_file_publication.save_intent(
                connection, repo, context.cycle_revision, context.active_sha,
                plan_commit, evidence,
            )
        return intent


def _maintenance_rows_match_published_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> bool:
    for path, plan in result.plans:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, str(plan["id"])),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(plan)
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
        ) != (
            path,
            str(plan["title"]),
            sources,
            dependencies,
            priority,
            file_hash(worktree / path),
            plan_commit,
            "READY",
        ):
            return False
    for revision in result.revisions:
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, revision.snapshot.plan_id),
        ).fetchone()
        sources, dependencies, priority = _execplan_metadata(dict(revision.plan))
        if row is None or (
            str(row["path"]),
            str(row["title"]),
            str(row["sources"]),
            str(row["depends_on"]),
            str(row["priority"]),
            str(row["content_hash"]),
            str(row["current_revision_sha"] or row["plan_sha"] or ""),
            str(row["status"]),
        ) != (
            revision.path,
            revision.snapshot.title,
            sources,
            dependencies,
            priority,
            file_hash(worktree / revision.path),
            plan_commit,
            "READY",
        ):
            return False
    return True


def _assert_maintenance_publication_intent(
    connection: sqlite3.Connection,
    repo: str,
    context: MaintenancePlanningContext,
    plan_commit: str,
    result: ValidatedPlanningResult,
) -> None:
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    current = (
        cycle.publication_stage,
        cycle.publication_base_sha,
        cycle.publication_commit_sha,
        cycle.publication_remote_oid,
        cycle.publication_refactoring_plan_ids,
        cycle.publication_revalidation_plan_ids,
    )
    expected = (
        "prepared", context.active_sha, plan_commit, None,
        result.refactoring_plan_ids, result.revalidation_plan_ids,
    )
    if current != expected:
        raise MaintenancePlanningError(
            "원격 push 전에 보존한 maintenance publication intent가 "
            "현재 batch와 일치하지 않습니다."
        )


def _maintenance_publication_already_applied(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> bool:
    context = result.maintenance_context
    assert context is not None
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if context.mode is MaintenancePlanMode.BARRIER and result.refactoring_plan_ids:
        cycle_matches = (
            cycle.phase is maintenance_state.MaintenancePhase.REFACTORING
            and cycle.revision == context.cycle_revision + 1
            and cycle.refactoring_plan_ids == result.refactoring_plan_ids
            and cycle.revalidation_plan_ids == result.revalidation_plan_ids
            and cycle.publication_base_sha == context.active_sha
            and cycle.publication_commit_sha == plan_commit
            and cycle.publication_remote_oid == plan_commit
        )
    else:
        cycle_matches = (
            cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and cycle.revision == context.cycle_revision + 1
            and cycle.last_success_commit == plan_commit
        )
    if (cycle_matches and context.mode is MaintenancePlanMode.BARRIER
        and cycle.file_assessment_start_merged_count is not None
        and (not is_loop_manager_repository(Path(repo))
             or cycle.full_assessment_completed)):
        evidence = maintenance_file_publication.saved_intent(
            connection, repo, context.cycle_revision, context.active_sha,
            plan_commit,
        )
        success = maintenance_file_ledger.FileReviewRepository(connection).last_success(repo)
        if (success is None or success.source_revision != evidence.inventory.revision
            or cycle.last_success_file_assessment_merged_count is None):
            return False
    return cycle_matches and _maintenance_rows_match_published_batch(
        connection, repo, worktree, result, plan_commit
    )


def publish_maintenance_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
) -> maintenance_state.MaintenanceCycle:
    """Atomically publish catalog revisions after a confirmed remote push.

    Calling this again after a process interruption is idempotent when the
    remote commit, catalog rows, and persisted cycle describe the same batch.
    """

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    _validate_maintenance_publication_commit(
        worktree, base_branch, result, plan_commit
    )
    return _apply_maintenance_planning_batch(
        connection, repo, worktree, base_branch, result, plan_commit
    )


def _apply_maintenance_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
    result: ValidatedPlanningResult,
    plan_commit: str,
    *, current_remote: str | None = None,
) -> maintenance_state.MaintenanceCycle:
    """Apply one already-confirmed maintenance batch in a SQLite transaction."""

    context = result.maintenance_context
    if context is None or context.repo != repo:
        raise MaintenancePlanningError(
            "SQLite-bound maintenance context가 publication 대상과 일치하지 않습니다."
        )
    with runtime_state._transaction(connection):
        repository = maintenance_state.MaintenanceRepository(connection)
        if _maintenance_publication_already_applied(
            connection, repo, worktree, result, plan_commit
        ):
            return repository.get(repo)
        include_general_planning = repository.get(repo).include_general_planning
        _assert_maintenance_publication_intent(
            connection, repo, context, plan_commit, result
        )
        _assert_maintenance_context_current(connection, context)
        _register_execplan_rows(
            connection, repo, worktree, list(result.plans), plan_commit
        )
        for revision in result.revisions:
            snapshot = revision.snapshot
            _sources, dependencies, _priority = _execplan_metadata(
                dict(revision.plan)
            )
            cursor = connection.execute(
                """
                UPDATE exec_plans
                SET depends_on = ?, content_hash = ?, plan_sha = ?,
                    current_revision_sha = ?, current_complexity = ?, current_risk = ?,
                    catalog_generation = catalog_generation + 1
                WHERE repo = ? AND plan_id = ? AND status = 'READY'
                  AND path = ? AND title = ? AND sources = ?
                  AND depends_on = ? AND priority = ?
                  AND content_hash = ?
                  AND COALESCE(current_revision_sha, plan_sha) = ?
                  AND branch IS NULL AND worktree IS NULL
                  AND workspace_owned = 0
                  AND worker_session_id IS NULL
                  AND reviewer_session_id IS NULL AND pr_url IS NULL
                  AND pr_head_oid IS NULL
                  AND pr_conflict_recovery = 0
                  AND pr_conflict_head_oid IS NULL
                  AND pr_conflict_base_oid IS NULL
                """,
                (
                    dependencies,
                    file_hash(worktree / revision.path),
                    plan_commit,
                    plan_commit,
                    str(revision.plan["complexity"]),
                    str(revision.plan["risk"]),
                    repo,
                    snapshot.plan_id,
                    snapshot.path,
                    snapshot.title,
                    json.dumps(
                        snapshot.sources,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        snapshot.depends_on,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    snapshot.priority,
                    snapshot.content_hash,
                    snapshot.plan_sha,
                ),
            )
            if cursor.rowcount != 1:
                raise MaintenancePlanningError(
                    "publication 직전 READY snapshot이 변경됐습니다: "
                    f"{snapshot.plan_id}"
                )

        cycle = repository.get(repo)
        if (context.mode is MaintenancePlanMode.BARRIER
            and cycle.file_assessment_start_merged_count is not None
            and (not is_loop_manager_repository(Path(repo))
                 or cycle.full_assessment_completed)):
            evidence = maintenance_file_publication.saved_intent(
                connection, repo, context.cycle_revision, context.active_sha,
                plan_commit,
            )
            maintenance_file_publication.promote(
                connection, repo, Path(repo), base_branch,
                current_remote or plan_commit, evidence,
            )
            repository.advance_file_assessment_success_baseline(
                repo, expected_revision=context.cycle_revision,
                source_sha=context.active_sha,
                expected_merged_count=evidence.merged_count,
                bump_revision=False,
            )

        if (
            context.mode is MaintenancePlanMode.BARRIER
            and result.refactoring_plan_ids
        ):
            next_cycle = repository.transition(
                repo,
                expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                expected_revision=context.cycle_revision,
                next_phase=maintenance_state.MaintenancePhase.REFACTORING,
                refactoring_plan_ids=result.refactoring_plan_ids,
                revalidation_plan_ids=result.revalidation_plan_ids,
                publication_stage="remote_confirmed",
                publication_base_sha=context.active_sha,
                publication_commit_sha=plan_commit,
                publication_remote_oid=plan_commit,
                include_general_planning=False,
            )
        else:
            next_cycle = repository.advance_success_baseline(
                repo,
                expected_phase=(
                    maintenance_state.MaintenancePhase.ASSESSMENT
                    if context.mode is MaintenancePlanMode.BARRIER
                    else maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
                ),
                expected_revision=context.cycle_revision,
                remote_commit=plan_commit,
            )
        _clear_active_in_current_transaction(
            connection,
            repo,
            processed_sha=plan_commit if include_general_planning else None,
            specification_fingerprint=(
                _specification_input_fingerprint(worktree, plan_commit)
                if include_general_planning
                else None
            ),
            planning_fingerprint=(
                _planning_input_fingerprint(worktree, plan_commit)
                if include_general_planning
                else None
            ),
        )
        return next_cycle


def restart_stale_maintenance_publication(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> maintenance_state.MaintenanceCycle:
    """Restart a batch proven absent from the newly fetched remote base.

    Git files and refs remain intact. The caller creates a fresh planning
    worktree at cycle_base_sha and obtains a new context after this returns.
    """
    repository = maintenance_state.MaintenanceRepository(connection)
    cycle = repository.get(repo)
    if (
        cycle.publication_stage != "prepared"
        or not cycle.publication_base_sha
        or not cycle.publication_commit_sha
        or cycle.publication_remote_oid is not None
    ):
        raise MaintenancePlanningError("재시작할 prepared publication intent가 없습니다.")
    remote = git(
        worktree, "rev-parse", "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    base = cycle.publication_base_sha
    commit = cycle.publication_commit_sha
    if remote in {base, commit}:
        raise MaintenancePlanningError("게시 기준이 이동하지 않았거나 batch가 이미 게시됐습니다.")

    if (
        not _git_commit_is_ancestor(worktree, base, remote)
        or _git_commit_is_ancestor(worktree, commit, remote)
    ):
        raise MaintenancePlanningError("미게시 batch임을 확정할 수 없어 재시작할 수 없습니다.")
    base_tree = _git_tree_entries(worktree, base)
    commit_tree = _git_tree_entries(worktree, commit)
    remote_tree = _git_tree_entries(worktree, remote)
    batch_paths = {
        path for path in base_tree.keys() | commit_tree.keys()
        if base_tree.get(path) != commit_tree.get(path)
    }
    if any(base_tree.get(path) != remote_tree.get(path) for path in batch_paths):
        raise MaintenancePlanningError("원격 파일이 보존된 batch와 겹쳐 재시작할 수 없습니다.")
    with runtime_state._transaction(connection):
        restarted = repository.restart_prepared_batch(
            repo, expected=cycle, new_base_sha=remote
        )
        _clear_active_in_current_transaction(connection, repo)
        return restarted


def recover_maintenance_planning_batch(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    base_branch: str,
) -> maintenance_state.MaintenanceCycle:
    """Recover a pushed batch from its SQLite intent and remote commit."""

    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.publication_stage == "remote_confirmed":
        remote_oid = cycle.publication_remote_oid
        current_remote = git(
            worktree,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base_branch}^{{commit}}",
        ).stdout.strip()
        if remote_oid and current_remote == remote_oid:
            return cycle
        if (
            not remote_oid
            or not cycle.publication_base_sha
            or cycle.publication_commit_sha != remote_oid
        ):
            raise MaintenancePlanningError(
                "적용된 maintenance publication 기록이 완전하지 않습니다."
            )
        _assert_remote_preserves_maintenance_batch(
            worktree,
            cycle.publication_base_sha,
            remote_oid,
            current_remote,
        )
        return cycle
    if (
        cycle.publication_stage != "prepared"
        or cycle.publication_base_sha is None
        or cycle.publication_commit_sha is None
        or cycle.publication_remote_oid is not None
    ):
        raise MaintenancePlanningError(
            "복구할 maintenance publication intent가 없습니다."
        )
    base_sha = cycle.publication_base_sha
    plan_commit = cycle.publication_commit_sha
    current_remote = git(
        worktree,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    remote_advanced_after_publication = False
    if current_remote != plan_commit:
        if current_remote == base_sha:
            raise MaintenancePlanningError(
                "보존된 maintenance planning commit의 push가 아직 확인되지 않았습니다."
            )
        if _git_commit_is_ancestor(worktree, plan_commit, current_remote):
            _assert_remote_preserves_maintenance_batch(
                worktree, base_sha, plan_commit, current_remote,
                reject_catalog_drift=True,
            )
            remote_advanced_after_publication = True
        else:
            return restart_stale_maintenance_publication(
                connection, repo, worktree, base_branch
            )
    if (
        remote_advanced_after_publication
        and cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
        and cycle.file_assessment_start_merged_count is not None
        and (not is_loop_manager_repository(Path(repo))
             or cycle.full_assessment_completed)
    ):
        evidence = maintenance_file_publication.saved_intent(
            connection, repo, cycle.revision, base_sha, plan_commit,
        )
        if not maintenance_file_publication.sources_match_remote(
            Path(repo), base_branch, current_remote, evidence,
        ):
            # The plans remain published. A later catalog reconciliation imports
            # them, while this assessment must inspect the new source revision.
            with runtime_state._transaction(connection):
                restarted = maintenance_state.MaintenanceRepository(
                    connection
                ).restart_prepared_batch(
                    repo, expected=cycle, new_base_sha=current_remote,
                )
                _clear_active_in_current_transaction(connection, repo)
                return restarted
    context = _maintenance_context_at_revision(
        connection, repo, worktree, base_sha, cycle
    )
    try:
        result = validate_maintenance_planner_result(
            connection,
            context,
            worktree,
            changed_files(worktree),
            refactoring_plan_ids=(cycle.publication_refactoring_plan_ids if context.mode is MaintenancePlanMode.BARRIER else ()),
            allow_general_plans=cycle.include_general_planning,
        )
    except PlannerValidationError as error:
        raise MaintenancePlanningError(
            f"보존된 maintenance publication batch가 유효하지 않습니다: {error}"
        ) from error
    if result is None:
        raise MaintenancePlanningError(
            "보존된 maintenance publication commit에 검증할 batch가 없습니다."
        )
    if remote_advanced_after_publication:
        _validate_maintenance_planning_commit(worktree, result, plan_commit)
        return _apply_maintenance_planning_batch(
            connection, repo, worktree, base_branch, result, plan_commit,
            current_remote=current_remote,
        )
    return publish_maintenance_planning_batch(
        connection,
        repo,
        worktree,
        base_branch,
        result,
        plan_commit,
    )


def _legacy_plan_sha(root: Path, revision: str, relative_path: str) -> str:
    result = git(
        root,
        "log",
        revision,
        "--diff-filter=A",
        "--format=%H",
        "--",
        relative_path,
        check=False,
    )
    commits = [
        value
        for value in result.stdout.splitlines()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
    ]
    if len(commits) != 1:
        raise RuntimeError(
            f"Legacy ExecPlan의 게시 commit을 안전하게 확인할 수 없습니다: {relative_path}"
        )
    return commits[0]


def _current_execplan_sha(
    root: Path,
    revision: str,
    entry: execplan_catalog.ExecPlanCatalogEntry,
) -> str:
    """Find the oldest revision that contains the current active plan bytes."""

    result = git(
        root,
        "log",
        revision,
        "--format=%H",
        "--",
        entry.active_path,
        check=False,
    )
    for commit in reversed(result.stdout.splitlines()):
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
            continue
        try:
            content = git_bytes(
                root, "show", f"{commit}:{entry.active_path}"
            )
        except RuntimeError:
            continue
        if hashlib.sha256(content).hexdigest() == entry.content_hash:
            return commit
    raise RuntimeError(
        "현재 active ExecPlan 본문의 게시 commit을 안전하게 확인할 수 없습니다: "
        f"{entry.active_path}"
    )


class _LegacyCatalogMergeProof:
    """GitHub/Git adapter kept outside the parser and catalog repository."""

    def __init__(
        self,
        root: Path,
        revision: str,
        stored: Mapping[str, execplan_catalog_state.StoredPlanIdentity],
    ):
        self.root = root
        self.revision = revision
        self.stored = stored

    def _stored_git_proof(
        self, entry: execplan_catalog.ExecPlanCatalogEntry
    ) -> execplan_catalog_state.MergeProof:
        identity = self.stored.get(entry.plan_id)
        if identity is None:
            return execplan_catalog_state.MergeProof(False)
        return self.verify_stored(entry, identity)

    def verify_stored(
        self,
        entry: execplan_catalog.ExecPlanCatalogEntry,
        identity: execplan_catalog_state.StoredPlanIdentity,
    ) -> execplan_catalog_state.MergeProof:
        if (
            identity.plan_id != entry.plan_id
            or identity.status != "MERGED"
            or not identity.plan_sha
        ):
            return execplan_catalog_state.MergeProof(False)
        try:
            revision_sha = git(
                self.root, "rev-parse", f"{self.revision}^{{commit}}"
            ).stdout.strip()
            published = git_bytes(
                self.root,
                "show",
                f"{identity.plan_sha}:{identity.path}",
            )
            ancestor = _git_commit_is_ancestor(
                self.root, identity.plan_sha, revision_sha
            )
        except RuntimeError as error:
            return execplan_catalog_state.MergeProof(False, detail=str(error))
        published_hash = hashlib.sha256(published).hexdigest()
        if not ancestor or published_hash != identity.content_hash:
            return execplan_catalog_state.MergeProof(False)
        return execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.STORED_AND_GIT,
            plan_sha=identity.plan_sha,
            publication_content_hash=published_hash,
            revision=revision_sha,
        )

    def proof_for(
        self, entry: execplan_catalog.ExecPlanCatalogEntry
    ) -> execplan_catalog_state.MergeProof:
        # SQLite's MERGED transition is the durable result of the first
        # GitHub observation.  Archive reconciliation may still need local
        # publication evidence, but it must never turn an incomplete local
        # proof into another PR query.
        identity = self.stored.get(entry.plan_id)
        if identity is not None and identity.status == "MERGED":
            return self._stored_git_proof(entry)

        pull_request = pr_for_branch(
            self.root, f"loop-manager/{entry.plan_id}"
        )
        if pull_request is None:
            return self._stored_git_proof(entry)
        merged = bool(
            pull_request["state"] == "MERGED" or pull_request.get("mergedAt")
        )
        if not merged:
            return execplan_catalog_state.MergeProof(
                False,
                pr_url=str(pull_request.get("url") or "") or None,
                detail=str(pull_request.get("state") or "unknown"),
            )
        if identity is not None and identity.plan_sha:
            plan_sha = identity.plan_sha
        else:
            # Use the latest active publication, including maintenance revisions.
            # Matching archive bytes against any older revision would accept
            # an archive reverted to an obsolete publication.
            plan_sha = git(
                self.root, "log", self.revision, "--no-renames",
                "--diff-filter=AM", "--format=%H", "-1", "--",
                entry.active_path,
            ).stdout.strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", plan_sha):
                raise RuntimeError("archived ExecPlan의 게시 revision이 없습니다.")
        published_hash = hashlib.sha256(
            git_bytes(self.root, "show", f"{plan_sha}:{entry.active_path}")
        ).hexdigest()
        return execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
            plan_sha=plan_sha,
            publication_content_hash=published_hash,
            revision=(
                str(
                    (pull_request.get("mergeCommit") or {}).get("oid")
                    or ""
                )
                or str(pull_request.get("mergedAt") or "")
                or None
            ),
            pr_url=str(pull_request.get("url") or "") or None,
        )


def _migrate_execplan_catalog(
    connection: sqlite3.Connection,
    *,
    reconcile_history: bool,
    target_repo: str | None,
    manage_transaction: bool = True,
    pinned_catalog: execplan_catalog_state.PinnedCatalog | None = None,
) -> None:
    return catalog_migration._migrate_execplan_catalog(
        connection, effects=_catalog_migration_effects(),
        reconcile_history=reconcile_history, target_repo=target_repo,
        manage_transaction=manage_transaction, pinned_catalog=pinned_catalog,
    )


def migrate_legacy_execplans(
    connection: sqlite3.Connection,
    *,
    reconcile_history: bool = False,
    target_repo: str | None = None,
    ensure_schema: bool = True,
    manage_transaction: bool = True,
    pinned_catalog: execplan_catalog_state.PinnedCatalog | None = None,
) -> None:
    """Conservatively move legacy project-owned execution state into the catalog."""

    if ensure_schema:
        _ensure_execplans_schema(connection)
    _migrate_execplan_catalog(
        connection,
        reconcile_history=reconcile_history,
        target_repo=target_repo,
        manage_transaction=manage_transaction,
        pinned_catalog=pinned_catalog,
    )
    with connection if manage_transaction else contextlib.nullcontext():
        scoped_blocking.migrate_legacy_blocks(connection)
    return


def active_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Return the only plan currently occupying the repository mutation slot."""

    _ensure_execplans_schema(connection)
    return runtime_state.RuntimeStateRepository(connection).active_row(repo)


def project_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Return the plan explicitly mirrored by ``projects``, regardless of status."""

    return runtime_state.RuntimeStateRepository(connection).projected_row(repo)


def _project_pr_merge_confirmed(
    connection: sqlite3.Connection,
    repo: str,
    project: sqlite3.Row,
) -> bool:
    """Resolve a stale projection against this plan's durable execution row."""

    path = str(project["plan_path"] or "").replace(
        "docs/plans/archive/", "docs/plans/", 1,
    )
    plan_id = _plan_id_from_path(path)
    if plan_id is None:
        return False
    plan = connection.execute(
        "SELECT status, branch FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    return bool(
        plan is not None
        and plan["status"] == "MERGED"
        and project["branch"] == (plan["branch"] or f"loop-manager/{plan_id}")
    )


def plan_blocked_execplans(
    connection: sqlite3.Connection, repo: str
) -> tuple[sqlite3.Row, ...]:
    _ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'PLAN_BLOCKED' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
    )


def merge_wait_execplans(
    connection: sqlite3.Connection, repo: str
) -> tuple[sqlite3.Row, ...]:
    _ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'PR_OPEN' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
    )


def mark_execplan_workspace_owned(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        try:
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                plan_id,
                plan_values={"workspace_owned": WORKSPACE_OWNED},
                enqueue=False,
            )
        except runtime_state.RuntimeStateError as error:
            raise RuntimeError(
                "workspace 소유권을 기록할 ExecPlan을 찾을 수 없습니다."
            ) from error


def reserve_execplan_workspace(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        try:
            runtime_state.RuntimeStateRepository(connection).reserve_workspace(
                repo,
                plan_id,
                reserved_value=WORKSPACE_RESERVED,
                allowed_values=(WORKSPACE_UNOWNED, WORKSPACE_RESERVED),
            )
        except runtime_state.RuntimeStateError as error:
            raise RuntimeError(
                "workspace 생성을 예약할 ExecPlan을 찾을 수 없습니다."
            ) from error


def compact_execplan_catalog(connection: sqlite3.Connection, repo: str) -> str:
    """Serialize only non-terminal plan metadata, never plan bodies."""

    _ensure_execplans_schema(connection)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id)",
        (repo,),
    ).fetchall()
    rows = sorted(rows, key=lambda row: int(str(row["plan_id"])[2:]))
    catalog = [
        {
            "id": row["plan_id"],
            "title": row["title"],
            "status": row["status"],
            "sources": json.loads(row["sources"]),
            "depends_on": json.loads(row["depends_on"]),
            "priority": row["priority"],
        }
        for row in rows
    ]
    return json.dumps(catalog, ensure_ascii=False, indent=2)


def prerequisite_validation_rows(
    connection: sqlite3.Connection, repo: str
) -> list[sqlite3.Row]:
    """Load active plans and only their verified, merged dependency closure."""

    _ensure_execplans_schema(connection)
    active = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo AND h.plan_id = exec_plans.plan_id "
        "AND h.currently_archived = 1)",
        (repo,),
    ).fetchall()
    included = {str(row["plan_id"]): row for row in active}
    pending = list(active)
    while pending:
        plan = pending.pop()
        plan_id = str(plan["plan_id"])
        try:
            dependencies = json.loads(str(plan["depends_on"]))
        except json.JSONDecodeError as error:
            raise MaintenanceOrchestrationError(
                f"선행 후보 dependency를 읽을 수 없습니다: {plan_id}"
            ) from error
        if not isinstance(dependencies, list) or any(
            not isinstance(dependency, str) for dependency in dependencies
        ):
            raise MaintenanceOrchestrationError(
                f"선행 후보 dependency 형식이 유효하지 않습니다: {plan_id}"
            )
        for dependency in dependencies:
            if dependency in included:
                continue
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, dependency),
            ).fetchone()
            if row is None:
                raise MaintenanceOrchestrationError(
                    f"선행 후보의 dependency가 catalog에 없습니다: {plan_id} -> {dependency}"
                )
            history = connection.execute(
                "SELECT * FROM execplan_catalog_history "
                "WHERE repo = ? AND plan_id = ? AND currently_archived = 1",
                (repo, dependency),
            ).fetchone()
            if history is None and str(row["path"]).startswith(
                "docs/plans/archive/"
            ):
                raise MaintenanceOrchestrationError(
                    f"archived dependency 검증 실패: {plan_id} -> {dependency}: "
                    "archive identity와 Merge 증명이 없습니다"
                )
            if history is not None:
                identity_ok = (
                    bool(history["identity_verified"])
                    and re.fullmatch(
                        rf"docs/plans/\[{re.escape(dependency)}\] [^/]+\.md",
                        str(history["original_path"]),
                    ) is not None
                    and re.fullmatch(
                        rf"docs/plans/archive/\[{re.escape(dependency)}\] [^/]+\.md",
                        str(history["observed_path"]),
                    ) is not None
                    and bool(history["original_content_hash"])
                    and bool(history["final_content_hash"])
                    and bool(history["plan_sha"])
                    and row["path"] == history["observed_path"]
                    and row["content_hash"] == history["final_content_hash"]
                )
                proof_ok = (
                    bool(history["merge_confirmed"])
                    and history["proof_kind"] in {
                        kind.value for kind in execplan_catalog_state.MergeProofKind
                        if kind is not execplan_catalog_state.MergeProofKind.NONE
                    }
                    and bool(history["proof_revision"])
                    and not history["diagnostic_code"]
                )
                if row["status"] != "MERGED" or not identity_ok or not proof_ok:
                    reason = (
                        "SQLite 상태가 MERGED가 아닙니다" if row["status"] != "MERGED"
                        else "archive identity가 확인되지 않았습니다" if not identity_ok
                        else "Merge 증명이 확인되지 않았습니다"
                    )
                    raise MaintenanceOrchestrationError(
                        f"archived dependency 검증 실패: {plan_id} -> {dependency}: {reason}"
                    )
            included[dependency] = row
            pending.append(row)
    return list(included.values())


def catalog_schedule_decision(
    connection: sqlite3.Connection,
    repo: str,
) -> CatalogScheduleDecision:
    """Separate active work, READY presence, and dependency-gated selection."""

    _ensure_execplans_schema(connection)
    active = active_execplan(connection, repo)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
    ).fetchall()
    historical_confirmation = {
        str(row["plan_id"]): bool(row["merge_confirmed"]) and not row["diagnostic_code"]
        for row in connection.execute(
            """
            SELECT plan_id, merge_confirmed, diagnostic_code
            FROM execplan_catalog_history
            WHERE repo = ?
              AND currently_archived = 1
            """,
            (repo,),
        ).fetchall()
    }
    historical_ids = set(historical_confirmation)
    project_is_blocked = scoped_blocking.project_block(connection, repo) is not None
    statuses = {
        row["plan_id"]: (
            row["status"]
            if str(row["plan_id"]) not in historical_confirmation
            or historical_confirmation[str(row["plan_id"])]
            else "HISTORICAL_UNVERIFIED"
        )
        for row in rows
    }
    ready = [
        row
        for row in rows
        if row["status"] == "READY" and str(row["plan_id"]) not in historical_ids
    ]
    execution_rows = [
        row for row in rows
        if str(row["plan_id"]) not in historical_ids
        or (
            historical_confirmation[str(row["plan_id"])]
            and row["status"] == "MERGED"
        )
    ]
    candidates = []
    for row in ready:
        dependencies = json.loads(row["depends_on"])
        if all(statuses.get(dependency) == "MERGED" for dependency in dependencies):
            candidates.append(row)
    selected = None
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if active is None and not project_is_blocked and cycle.phase in {
        maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
        maintenance_state.MaintenancePhase.REFACTORING,
    }:
        selected = select_cycle_ready_execplan(execution_rows, cycle)
    elif active is None and not project_is_blocked and assessment_can_yield(cycle):
        resumable = [row for row in candidates if not is_unowned_ready(row)]
        if resumable:
            selected = min(resumable, key=lambda row: (
                PRIORITY_ORDER[str(row["priority"])], int(str(row["plan_id"])[2:]),
            ))
    elif (
        active is None
        and not project_is_blocked
        and cycle.phase is maintenance_state.MaintenancePhase.IDLE
        and candidates
    ):
        selected = min(
            candidates,
            key=lambda row: (
                PRIORITY_ORDER[str(row["priority"])],
                int(str(row["plan_id"])[2:]),
            ),
        )
    return CatalogScheduleDecision(
        active=active,
        has_ready=bool(ready),
        runnable_ready=selected,
        has_plan_blocked=any(
            row["status"] == "PLAN_BLOCKED"
            and str(row["plan_id"]) not in historical_ids for row in rows
        ),
        has_pr_open=any(
            row["status"] == "PR_OPEN"
            and str(row["plan_id"]) not in historical_ids for row in rows
        ),
        project_blocked=project_is_blocked,
    )


def select_ready_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Apply the dependency gate before deterministic priority/ID ordering."""

    return catalog_schedule_decision(connection, repo).runnable_ready


def _general_planning_required(
    row: sqlite3.Row,
    repo_path: Path,
    base_sha: str,
    current_fingerprint: str,
) -> bool:
    if row["planning_fingerprint"] == current_fingerprint:
        return False
    if row["planning_fingerprint"] and row["processed_sha"]:
        processed_fingerprint = _planning_input_fingerprint(
            repo_path, str(row["processed_sha"])
        )
        if processed_fingerprint == current_fingerprint:
            return False
        if _is_requirement_archive_only_change(
            repo_path, str(row["processed_sha"]), base_sha
        ):
            return False
    return True


def _maintenance_change_context_text(
    repo_path: Path,
    base_branch: str,
    cycle: maintenance_state.MaintenanceCycle,
) -> str:
    current = str(cycle.cycle_base_sha or "")
    baseline = str(cycle.last_success_commit or current)
    context = maintenance_git.bounded_remote_change_context(
        repo_path, base_branch, baseline, current
    )
    if not context.ok:
        raise MaintenancePlanningError(
            context.error or "bounded 유지보수 변경 문맥을 만들 수 없습니다."
        )
    return json.dumps(
        {
            "baseline_commit": context.baseline_commit,
            "current_commit": context.current_commit,
            "truncated": context.truncated,
            "commits": [
                {
                    "sha": commit.sha,
                    "subject": commit.subject,
                    "paths": list(commit.paths),
                }
                for commit in context.commits
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _advance_maintenance_cycle_after_merges(
    connection: sqlite3.Connection,
    repo: str,
    base_sha: str,
) -> maintenance_state.MaintenanceCycle:
    repository = maintenance_state.MaintenanceRepository(connection)
    cycle = repository.get(repo)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id "
        "AND (h.merge_confirmed = 0 OR h.diagnostic_code IS NOT NULL))",
        (repo,),
    ).fetchall()
    error = cycle_plan_status_error(rows, cycle)
    if error is not None:
        raise MaintenancePlanningError(error)
    if (
        cycle.phase is maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
        and all_cycle_plans_merged(rows, cycle.prerequisite_plan_ids)
    ):
        return repository.transition(
            repo,
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha=base_sha,
            include_general_planning=False,
        )
    if (
        cycle.phase is maintenance_state.MaintenancePhase.REFACTORING
        and all_cycle_plans_merged(rows, cycle.refactoring_plan_ids)
    ):
        return repository.transition(
            repo,
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha=base_sha,
            include_general_planning=False,
        )
    return cycle


def activate_ready_execplan(
    connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    base_sha: str,
) -> sqlite3.Row | None:
    selected = select_ready_execplan(connection, repo)
    if selected is None:
        return None
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    branch = str(selected["branch"] or f"loop-manager/{selected['plan_id']}")
    worktree = (
        Path(str(selected["worktree"]))
        if selected["worktree"]
        else state_dir / "worktrees" / project_id / str(selected["plan_id"])
    )
    activation_sha = (
        git(worktree, "rev-parse", "HEAD").stdout.strip()
        if worktree.is_dir()
        else base_sha
    )
    try:
        runtime_state.RuntimeStateRepository(connection).activate(
            repo,
            str(selected["plan_id"]),
            activation_sha=activation_sha,
            branch=branch,
            worktree=str(worktree),
            preserve_worker_session=(
                assessment_can_yield(
                    maintenance_state.MaintenanceRepository(connection).get(repo)
                )
                and not is_unowned_ready(selected)
            ),
        )
    except runtime_state.RuntimeStateError as error:
        raise RuntimeError(
            "READY ExecPlan 선택 상태가 변경되었습니다."
        ) from error
    connection.commit()
    return connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, selected["plan_id"]),
    ).fetchone()


def hydrate_active_project(connection: sqlite3.Connection, repo: str) -> None:
    """Refresh the legacy execution mirror from the plan-state source of truth."""

    if not runtime_state.RuntimeStateRepository(connection).recovery_slot_available(repo):
        return
    plan = active_execplan(connection, repo)
    if plan is None:
        return
    hydrate_execplan_project(connection, repo, plan)


def hydrate_execplan_project(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
) -> None:
    """Hydrate one explicitly selected plan into the compatibility mirror."""

    if connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is None:
        connection.commit()
        return
    snapshot = runtime_state.ExecutionSnapshot.from_row(plan)
    if snapshot.repo != repo:
        snapshot = replace(snapshot, repo=repo)
    runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(
        repo, snapshot
    )
    connection.commit()


def _mirror_project_update_to_execplan(
    connection: sqlite3.Connection,
    repo: str,
    current: sqlite3.Row | None,
    values: dict[str, object],
) -> None:
    plan_path = (
        str(current["plan_path"])
        if current is not None and current["plan_path"]
        else None
    )
    compatible_values = {
        field: value for field, value in values.items() if field in FIELDS
    }
    if not compatible_values:
        return
    if plan_path and not ("phase" in values and values["phase"] is None):
        _ensure_execplans_schema(connection)
    runtime_state.RuntimeStateRepository(connection).mirror_project_values(
        repo, plan_path, compatible_values
    )


def validate_runtime_prerequisites(
    path: Path,
    *,
    provider: str,
    config_path: Path | None,
) -> None:
    """Validate executable, authentication, and profile prerequisites only."""

    if not shutil.which("git") or not shutil.which("gh"):
        raise RuntimeError("git과 gh CLI가 PATH에 있어야 합니다.")
    if provider not in AGENT_PROVIDERS:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    if config_path is not None:
        validate_provider_runtime(config_path, provider, path)
    elif not shutil.which(provider):
        raise RuntimeError(
            f"선택한 {provider.capitalize()} CLI가 PATH에 설치되어 있지 않습니다."
        )


def repository_origin(root: Path) -> str:
    return github_repository(git(root, "remote", "get-url", "origin").stdout)


def validate_repository(
    path: Path,
    *,
    allow_agents_mismatch: bool,
    provider: str = "codex",
    config_path: Path | None = None,
    registration_checks: bool = True,
    expected_origin: str | None = None,
    origin_result: list[str] | None = None,
) -> Path:
    """Validate runtime prerequisites and stable repository identity only."""

    del allow_agents_mismatch, registration_checks
    path = path.expanduser().resolve()
    validate_runtime_prerequisites(
        path,
        provider=provider,
        config_path=config_path,
    )
    root = Path(git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != path:
        raise RuntimeError(f"저장소 루트 경로를 지정하세요: {root}")
    current_origin = repository_origin(root)
    if origin_result is not None:
        origin_result.append(current_origin)
    if expected_origin is not None and current_origin != expected_origin:
        raise RuntimeError(
            "등록된 GitHub origin과 현재 origin이 다릅니다: "
            f"{current_origin} (expected {expected_origin})"
        )
    return root


def remote_base(
    repo: Path, requested: str | None, *, state_dir: Path | None = None,
) -> str:
    if state_dir is None:
        git(repo, "fetch", "origin")
    if requested:
        base = requested
    else:
        reference = git(
            repo,
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ).stdout.strip()
        base = reference.removeprefix("origin/")
    if state_dir is not None:
        heads = git(repo, "ls-remote", "--heads", "origin").stdout.splitlines()
        branches = []
        for line in heads:
            fields = line.split()
            if len(fields) != 2 or not fields[1].startswith("refs/heads/"):
                raise RuntimeError("project fetch 원격 branch 목록이 유효하지 않습니다.")
            branches.append(fields[1].removeprefix("refs/heads/"))
        if base not in branches:
            raise RuntimeError(f"origin/{base} 원격 branch를 찾을 수 없습니다.")
        for branch in sorted(branches):
            try:
                _recorded_base_fetch(repo, state_dir, branch, owner="project")
            except RemoteBranchMissingError:
                # A merged PR head may be deleted after the list query. Keep
                # its tracking ref and stored PR head for cleanup assessment.
                if branch == base:
                    raise
    git(repo, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}")
    return base


def _revision_blob(
    root: Path,
    entries: Mapping[bytes, tuple[bytes, bytes]],
    relative_path: str,
) -> bytes | None:
    entry = entries.get(os.fsencode(relative_path))
    if entry is None:
        return None
    object_type, object_id = entry
    if object_type != b"blob":
        raise RuntimeError(f"등록 파일이 blob이 아닙니다: {relative_path}")
    return git_bytes(root, "cat-file", "blob", object_id.decode("ascii"))


def inspect_registration_files(
    root: Path,
    revision: str,
    *,
    approve_agents_mismatch: bool,
) -> tuple[str, bool]:
    """Validate registration documents from one immutable commit tree."""

    entries = _git_tree_entries(root, revision)
    required_paths = ("AGENTS.md", "docs/PLANS.md", "docs/ARCHITECTURE.md")
    blobs = {
        relative_path: _revision_blob(root, entries, relative_path)
        for relative_path in required_paths
    }
    missing = [path for path, contents in blobs.items() if contents is None]
    product = _revision_blob(root, entries, "docs/PRODUCT.md")
    active_requirements = tuple(
        path for path in entries if _is_active_requirement(path)
    )
    if product is None and not active_requirements:
        missing.append("docs/PRODUCT.md 또는 docs/requirements/*.md")
    if missing:
        raise RuntimeError("필수 파일이 없습니다: " + ", ".join(missing))

    plans = blobs["docs/PLANS.md"]
    architecture = blobs["docs/ARCHITECTURE.md"]
    agents = blobs["AGENTS.md"]
    assert plans is not None and architecture is not None and agents is not None
    if hashlib.sha256(plans).hexdigest() != file_hash(ROOT / "docs/PLANS.md"):
        raise RuntimeError("docs/PLANS.md가 Loop Manager 기준 파일과 다릅니다.")
    if hashlib.sha256(architecture).hexdigest() == file_hash(
        ROOT / "docs/ARCHITECTURE.template.md"
    ):
        raise RuntimeError(
            "docs/ARCHITECTURE.md가 변경되지 않은 템플릿이라 등록할 수 없습니다."
        )
    if product is not None and hashlib.sha256(product).hexdigest() == file_hash(
        ROOT / "docs/PRODUCT.template.md"
    ):
        raise RuntimeError(
            "docs/PRODUCT.md가 변경되지 않은 템플릿이라 등록할 수 없습니다."
        )

    try:
        target_agents = agents.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise RuntimeError("origin의 AGENTS.md가 UTF-8이 아닙니다.") from error
    common_agents = normalized_text(ROOT / "AGENTS.md")
    agents_hash = hashlib.sha256(agents).hexdigest()
    mismatch = common_agents not in target_agents
    if mismatch and not approve_agents_mismatch:
        raise RuntimeError(
            "AGENTS.md에 공통 규칙이 없습니다. 내용을 확인한 뒤 "
            "`config add --approve-agents-mismatch`로 승인하세요."
        )
    return agents_hash, mismatch


def inspect_project_registration(
    path: Path,
    *,
    config_path: Path,
    provider: str,
    requested_base: str | None,
    approve_agents_mismatch: bool,
    state_dir: Path | None = None,
) -> ProjectRegistrationInspection:
    """Validate one project from its fetched origin revision before registry writes."""

    origins: list[str] = []
    root = validate_repository(
        path,
        allow_agents_mismatch=approve_agents_mismatch,
        provider=provider,
        config_path=config_path,
        registration_checks=False,
        origin_result=origins,
    )
    if not origins:
        raise RuntimeError("검증한 GitHub origin을 확인할 수 없습니다.")
    origin = origins[0]
    base_branch = remote_base(root, requested_base, state_dir=state_dir)
    revision = git(
        root,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    agents_hash, agents_approved = inspect_registration_files(
        root,
        revision,
        approve_agents_mismatch=approve_agents_mismatch,
    )
    return ProjectRegistrationInspection(
        root=root,
        base_branch=base_branch,
        provider=provider,
        agents_hash=agents_hash,
        agents_approved=agents_approved,
        origin=origin,
        source_revision=revision,
    )


def add_registered_project(
    connection: sqlite3.Connection,
    path: Path,
    *,
    config_path: Path,
    provider: str,
    requested_base: str | None,
    approve_agents_mismatch: bool,
    state_dir: Path | None = None,
) -> project_registry.RegisteredProject:
    inspection = inspect_project_registration(
        path,
        config_path=config_path,
        provider=provider,
        requested_base=requested_base,
        approve_agents_mismatch=approve_agents_mismatch,
        state_dir=state_dir,
    )
    project = project_registry.RegisteredProject(
        repo=str(inspection.root),
        base_branch=inspection.base_branch,
        provider=inspection.provider,
        agents_hash=inspection.agents_hash,
        agents_approved=inspection.agents_approved,
        origin=inspection.origin,
    )
    # Schema migration is independent of registration and may commit. Finish it
    # before the registration transaction so a stale remote revision can roll
    # back the registry row together with recovered catalog and baseline data.
    project_registry.ensure_registry_schema(connection)
    _ensure_execplans_schema(connection)
    maintenance_state.ensure_schema(connection)
    with runtime_state._transaction(connection):
        project_registry.add_project(connection, project, manage_transaction=False)
        if inspection.root.is_dir():
            current_sha = git(
                inspection.root, "rev-parse", "--verify",
                f"refs/remotes/origin/{project.base_branch}^{{commit}}",
            ).stdout.strip()
            source_sha = inspection.source_revision or current_sha
            if current_sha != source_sha:
                raise RuntimeError("등록 중 원격 source revision이 변경됐습니다.")
            migrate_legacy_execplans(
                connection,
                reconcile_history=True,
                target_repo=project.repo,
                ensure_schema=False,
                manage_transaction=False,
            )
            snapshot = revision_execplan_snapshot(
                inspection.root, source_sha,
                archive_conflict_plan_ids=_merged_execplan_ids(connection, project.repo),
            )
            current_sha = git(
                inspection.root, "rev-parse", "--verify",
                f"refs/remotes/origin/{project.base_branch}^{{commit}}",
            ).stdout.strip()
            if current_sha != source_sha:
                raise RuntimeError("등록 중 원격 ExecPlan catalog revision이 변경됐습니다.")
            connection.execute(
                "INSERT INTO execplan_catalog_revisions "
                "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET "
                "revision_sha = excluded.revision_sha, "
                "catalog_fingerprint = excluded.catalog_fingerprint",
                (project.repo, source_sha,
                 execplan_catalog_state._catalog_fingerprint(snapshot)),
            )
            repository = maintenance_state.MaintenanceRepository(connection)
            repository.initialize_file_assessment_start(
                project.repo,
                source_sha=source_sha,
                expected_merged_count=repository.merged_count(project.repo),
            )
    return project


def _ignored_worktree_paths(repo: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            _nul_git_paths(
                repo,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            )
        )
    )


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = tuple(part for part in left.replace("\\", "/").split("/") if part)
    right_parts = tuple(part for part in right.replace("\\", "/").split("/") if part)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def _ignored_paths_overlapping_git_update(worktree: Path, target: str) -> list[str]:
    ignored = _ignored_worktree_paths(worktree)
    if not ignored:
        return []
    changed = _nul_git_paths(
        worktree, "diff", "--name-only", "-z", "HEAD", target
    )
    fork = git(worktree, "merge-base", "HEAD", target).stdout.strip()
    for commit in git(worktree, "rev-list", "--reverse", f"{fork}..HEAD").stdout.splitlines():
        changed.extend(_nul_git_paths(
            worktree, "diff-tree", "-m", "--root", "--no-commit-id",
            "--name-only", "-r", "-z", commit,
        ))
    return sorted(
        path for path in ignored
        if any(_paths_overlap(path, candidate) for candidate in changed)
    )


def _remaining_rebase_commits(
    worktree: Path,
    metadata_directory: Path,
    backend: str,
    original_head: str,
    onto: str,
    action: str,
) -> list[str] | None:
    if backend == "rebase-merge":
        todo = metadata_directory / "git-rebase-todo"
        if not todo.is_file():
            return None
        commits = []
        for line in todo.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=2)
            if (
                not fields
                or fields[0].startswith("#")
                or fields[0] in {"drop", "d"}
            ):
                continue
            if fields[0] not in {
                "pick", "p", "reword", "r", "edit", "e",
                "squash", "s", "fixup", "f",
            } or len(fields) < 2:
                return None
            commits.append(
                git_bytes(
                    worktree, "rev-parse", "--verify", f"{fields[1]}^{{commit}}"
                ).decode("ascii").strip()
            )
        return commits

    next_path = metadata_directory / "next"
    last_path = metadata_directory / "last"
    if not next_path.is_file() or not last_path.is_file():
        return None
    commits = git_bytes(
        worktree, "rev-list", "--reverse", original_head, f"^{onto}"
    ).decode("ascii").splitlines()
    try:
        next_index = int(next_path.read_text(encoding="ascii").strip())
        last_index = int(last_path.read_text(encoding="ascii").strip())
    except ValueError:
        return None
    if len(commits) != last_index or not 1 <= next_index <= last_index + 1:
        return None
    return commits[next_index - (1 if action == "--continue" else 0):]


def _rebase_resume_ignored_overlap(worktree: Path, action: str) -> str | None:
    ignored = _ignored_worktree_paths(worktree)
    if not ignored:
        return None
    metadata: tuple[str, str, Path, str] | None = None
    for directory in ("rebase-merge", "rebase-apply"):
        raw_directory = os.fsdecode(
            git_bytes(worktree, "rev-parse", "--git-path", directory).strip()
        )
        metadata_directory = Path(raw_directory)
        if not metadata_directory.is_absolute():
            metadata_directory = worktree / metadata_directory
        values = []
        for name in ("orig-head", "onto"):
            path = metadata_directory / name
            if not path.is_file():
                break
            values.append(path.read_text(encoding="ascii").strip())
        if len(values) == 2:
            metadata = values[0], values[1], metadata_directory, directory
            break
    if metadata is None:
        return "rebase 재개 전 남은 tracked 경로를 확인할 수 없습니다."
    original_head, onto, metadata_directory, backend = metadata
    if action == "--abort":
        changed = _nul_git_paths(
            worktree, "diff", "--name-only", "-z", "HEAD", original_head
        )
    else:
        commits = _remaining_rebase_commits(
            worktree, metadata_directory, backend, original_head, onto, action
        )
        if commits is None:
            return "rebase 재개 전 남은 tracked 경로를 확인할 수 없습니다."
        changed = [
            path
            for commit in commits
            for path in _nul_git_paths(
                worktree, "diff-tree", "-m", "--root", "--no-commit-id",
                "--name-only", "-r", "-z", commit,
            )
        ]
    overlaps = sorted(
        path for path in ignored
        if any(_paths_overlap(path, candidate) for candidate in changed)
    )
    if overlaps:
        return (
            "rebase 재개 tracked 경로와 겹치는 ignored 파일: "
            + ", ".join(overlaps[:8])
        )
    return None


def _base_worktree_snapshot(
    repo: Path,
) -> BaseWorktreeSnapshot:
    return source_sync._base_worktree_snapshot(_source_sync_effects_base_worktree_snapshot(), repo)


def _base_paths_overlap(left: str, right: str) -> bool:
    # A remote checkout can be case insensitive even when the current host is not.
    return source_sync._base_paths_overlap(_source_sync_effects_base_paths_overlap(), left, right)


def _source_transition_state(repo: Path) -> dict[str, object]:
    return source_sync._source_transition_state(_source_sync_effects_source_transition_state(), repo)


def _source_transition_marker(repo: Path, base: str, intent: Mapping[str, object]) -> bool:
    return source_sync._source_transition_marker(_source_sync_effects_source_transition_marker(), repo, base, intent)


def _source_expected_index(
    repo: Path, selected_remote: str, before_index: Mapping[str, object],
    changed_paths: tuple[str, ...],
) -> dict[str, object]:
    return source_sync._source_expected_index(_source_sync_effects_source_expected_index(), repo, selected_remote, before_index, changed_paths)


def synchronize_base_worktree(
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
    owner: str = "project",
) -> BaseWorktreeSyncResult:
    return source_sync.synchronize_base_worktree(_source_sync_effects_synchronize_base_worktree(), repo, base, remote_sha=remote_sha, state_dir=state_dir, owner=owner)


def ensure_safe_base_worktree(
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
) -> BaseWorktreeSyncResult:
    return source_sync.ensure_safe_base_worktree(_source_sync_effects_ensure_safe_base_worktree(), repo, base, remote_sha=remote_sha, state_dir=state_dir)


def log_base_worktree_sync(repo: Path, result: BaseWorktreeSyncResult) -> None:
    return source_sync.log_base_worktree_sync(_source_sync_effects_log_base_worktree_sync(), repo, result)


def _deserialize_review_level_history(
    value: object,
) -> dict[str, dict[str, int]]:
    if value in {None, ""}:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
    history: dict[str, dict[str, int]] = {}
    for level, records in parsed.items():
        if (
            not isinstance(level, str)
            or not level.isdigit()
            or not 1 <= int(level) <= 8
            or not isinstance(records, dict)
        ):
            raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
        normalized_records: dict[str, int] = {}
        for fingerprint, occurrences in records.items():
            if (
                not isinstance(fingerprint, str)
                or not fingerprint
                or type(occurrences) is not int
                or occurrences < 1
            ):
                raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
            normalized_records[fingerprint] = occurrences
        history[level] = normalized_records
    return history


def evaluate_review_worker_level(
    effective_level: int,
    review_history: dict[str, dict[str, int]],
    fingerprints: tuple[str, ...],
) -> WorkerLevelDecision:
    """Return the monotonic level transition for one valid Reviewer FAIL."""

    if type(effective_level) is not int or not 1 <= effective_level <= 8:
        raise ValueError(f"effective Worker level이 유효하지 않습니다: {effective_level}")
    if not fingerprints or any(not fingerprint for fingerprint in fingerprints):
        raise ValueError("Review 승격 평가에는 실패 지문이 하나 이상 필요합니다.")

    history = {
        level: dict(records)
        for level, records in review_history.items()
    }
    current = history.setdefault(str(effective_level), {})
    for fingerprint in sorted(set(fingerprints)):
        current[fingerprint] = current.get(fingerprint, 0) + 1

    should_escalate = (
        any(occurrences >= 2 for occurrences in current.values())
        or len(current) >= 3
    )
    next_level = (
        effective_level + 1
        if should_escalate and effective_level < 8
        else effective_level
    )
    return WorkerLevelDecision(effective_level, next_level, history)


def ensure_worker_level_state(
    connection: sqlite3.Connection,
    repo: str,
    plan: dict[str, object],
) -> int:
    """Backfill and validate the persisted initial/effective Worker levels."""

    catalog_plan = active_execplan(connection, repo)
    if catalog_plan is None:
        raise RuntimeError("Worker level을 저장할 활성 ExecPlan을 찾을 수 없습니다.")
    calculated_initial = worker_level(plan)
    initial = catalog_plan["initial_worker_level"]
    effective = catalog_plan["effective_worker_level"]
    if initial is None and effective is None:
        with connection:
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                str(catalog_plan["plan_id"]),
                plan_values={
                    "initial_worker_level": calculated_initial,
                    "effective_worker_level": calculated_initial,
                    "review_level_history": "{}",
                },
                enqueue=False,
            )
        return calculated_initial
    if initial is None or effective is None:
        raise RuntimeError("저장된 Worker level 상태가 완전하지 않습니다.")
    if (
        type(initial) is not int
        or initial != calculated_initial
        or not 1 <= initial <= 7
        or type(effective) is not int
        or not initial <= effective <= 8
    ):
        raise RuntimeError("저장된 Worker level 상태가 ExecPlan과 일치하지 않습니다.")
    review_history = _deserialize_review_level_history(
        catalog_plan["review_level_history"]
    )
    if any(not 1 <= int(level) <= effective for level in review_history):
        raise RuntimeError("저장된 Review 승격 이력이 Worker level과 일치하지 않습니다.")

    return effective


def codex_model_efforts(worktree: Path | None=None) -> dict[str, frozenset[str]]:
    return agent_profiles.codex_model_efforts(worktree, effects=_agent_profiles_effects())


def codex_profile(
    config_path: Path,
    section: str,
    name: str,
    label: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return provider_profile(
        config_path,
        "codex",
        section,
        name,
        label,
        codex_model_efforts(worktree),
    )


def role(config_path: Path, name: str, worktree: Path | None = None) -> tuple[str, str]:
    return codex_profile(config_path, "codex.roles", name, f"Codex {name}", worktree)


def worker_profile(
    config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    if type(level) is not int or not 1 <= level <= 8:
        raise RuntimeError(f"Codex Worker level이 유효하지 않습니다: {level}")
    return provider_worker_profile(
        config_path,
        "codex",
        level,
        codex_model_efforts(worktree),
    )


def claude_model_efforts(config_path: Path, worktree: Path | None=None) -> dict[str, frozenset[str]]:
    return agent_profiles.claude_model_efforts(config_path, worktree, effects=_agent_profiles_effects())


def claude_role(
    config_path: Path,
    name: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return provider_profile(
        config_path,
        "claude",
        "claude.roles",
        name,
        f"Claude {name}",
        claude_model_efforts(config_path, worktree),
    )


def claude_worker_profile(
    config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return provider_worker_profile(
        config_path,
        "claude",
        level,
        claude_model_efforts(config_path, worktree),
    )


def run_codex(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
    resolved_profile: ResolvedProfile | None = None,
    isolation_arguments: tuple[str, ...] | None = None,
    isolation_evidence: agent_write_isolation.IsolationEvidence | None = None,
    isolation_snapshot_digest: Callable[[], str] | None = None,
    expected_repository: str | None = None,
    on_isolation_checked: Callable[[agent_write_isolation.IsolationEvidence], None] | None = None,
) -> tuple[int, str] | CodexResult:
    return provider_adapters.run_codex(
        worktree,
        state_dir,
        config_path,
        role_name,
        prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=on_session_started,
        resolved_profile=resolved_profile,
        isolation_arguments=isolation_arguments,
        isolation_evidence=isolation_evidence,
        isolation_snapshot_digest=isolation_snapshot_digest,
        expected_repository=expected_repository,
        on_isolation_checked=on_isolation_checked,
        effects=_provider_adapters_effects(),
    )


def _agent_profile(
    config_path: Path,
    provider: str,
    role_name: str,
    level: int | None,
    worktree: Path,
) -> AgentProfile:
    return agent_profiles._agent_profile(
        config_path,
        provider,
        role_name,
        level,
        worktree,
        effects=_agent_profiles_effects(),
    )


def validate_provider_runtime(config_path: Path, provider: str, worktree: Path | None=None) -> None:
    return agent_profiles.validate_provider_runtime(
        config_path,
        provider,
        worktree,
        effects=_agent_profiles_effects(),
    )


def run_claude(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
    resolved_profile: ResolvedProfile | None = None,
    on_isolation_checked: Callable[[agent_write_isolation.IsolationEvidence], None] | None = None,
    isolation_snapshot_digest: Callable[[], str] | None = None,
    expected_repository: str | None = None,
) -> tuple[int, str] | AgentResult:
    return provider_adapters.run_claude(
        worktree,
        state_dir,
        config_path,
        role_name,
        prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=on_session_started,
        resolved_profile=resolved_profile,
        on_isolation_checked=on_isolation_checked,
        isolation_snapshot_digest=isolation_snapshot_digest,
        expected_repository=expected_repository,
        effects=_provider_adapters_effects(),
    )


def agent_provider(name: str) -> AgentProvider:
    if name == "codex":
        return CodexAgentAdapter(run_codex)
    if name == "claude":
        return ClaudeAgentAdapter(run_claude)
    raise RuntimeError(f"지원하지 않는 Agent provider입니다: {name}")


def run_agent(
    provider: str,
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
    connection: sqlite3.Connection | None = None,
    invocation: InvocationContext | None = None,
    resolved_profile: ResolvedProfile | None = None,
) -> tuple[int, str] | AgentResult:
    return invocation_service.run_agent(
        provider,
        worktree,
        state_dir,
        config_path,
        role_name,
        prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=on_session_started,
        connection=connection,
        invocation=invocation,
        resolved_profile=resolved_profile,
        effects=_invocation_service_effects(),
    )


def _run_agent_with_connection(
    connection: sqlite3.Connection, *args: object, **kwargs: object
) -> tuple[int, str] | AgentResult:
    """Keep execution coordinator unaware of the ledger connection."""
    return run_agent(*args, connection=connection, **kwargs)


class VerifyOutput(str):
    """Bounded persisted feedback that retains full output for fingerprinting."""

    fingerprint_source: str
    timed_out: bool
    exit_code: int | None

    def __new__(cls, output: str, *, timed_out: bool = False,
                exit_code: int | None = None) -> VerifyOutput:
        shown = redact_secrets(output) if timed_out else output
        instance = super().__new__(cls, shown.strip()[-8000:])
        instance.fingerprint_source = output
        instance.timed_out = timed_out
        instance.exit_code = exit_code
        return instance


def verify(worktree: Path, timeout_seconds: int = 600) -> tuple[bool, str]:
    _check_runtime_handoff()
    if os.name == "nt":
        script = worktree / "scripts/verify.ps1"
        executable = shutil.which("pwsh") or shutil.which("powershell")
        arguments = [executable or "pwsh", "-NoProfile", "-File", str(script)]
    else:
        script = worktree / "scripts/verify.sh"
        arguments = ["bash", str(script)]
    if not script.is_file():
        return False, f"전체 검증 스크립트가 없습니다: {script.relative_to(worktree)}"
    gate = _runtime_handoff_signal.get()
    passed, timed_out, output = run_verify_process(
        arguments, worktree, timeout_seconds,
        **({"launch_process": _handoff_popen} if gate is not None else {}),
    )
    exit_code = getattr(output, "exit_code", 0 if passed else None)
    if timed_out:
        output += (
            f"\n전체 verify 시간 초과: verify_timeout_seconds={timeout_seconds}. "
            "verify와 자식 프로세스 종료를 확인했습니다."
        )
    return passed, VerifyOutput(output, timed_out=timed_out, exit_code=exit_code)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP_NOISE = re.compile(
    r"(?ix)"
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
)
_DURATION_NOISE = re.compile(
    r"(?i)\b(?:in|after|elapsed[:=]?)\s+\d+(?:[.,]\d+)?\s*(?:ms|s|sec|secs|seconds?)\b"
)
_TEMP_PATH_NOISE = re.compile(
    r"(?i)(?<![\w])"
    r"(?P<root>[a-z]:[\\/][^\r\n]*?[\\/]temp|/(?:private/)?(?:var/)?tmp)"
    r"(?P<tail>[\\/][^\s:]+)"
)
_TEMP_DIRECTORY_CONTEXT = re.compile(
    r"(?i)(?:\b(?:temporary\s+directory|directory|workspace|worktree)\b"
    r"|디렉터리|작업\s*공간)"
)


_REVIEW_ISSUE_ID = re.compile(
    r"(?mi)^[ \t]*ISSUE_ID:[ \t]*(?P<identifier>[^\r\n]+?)[ \t]*$"
)
_REVIEW_ISSUE_DECLARATION = re.compile(r"(?i)^[ \t]*ISSUE_ID\b")
_REVIEW_ISSUE_SYMBOL = re.compile(r"(?i)^[a-z0-9_.<>/-]+$")
_REVIEW_ISSUE_CATEGORY = re.compile(r"(?i)^[a-z0-9][a-z0-9-]*$")
_REVIEW_VERDICT_DECLARATION = re.compile(r"(?i)^[ \t]*VERDICT\b")
_REVIEW_VERDICT = re.compile(r"(?i)^[ \t]*VERDICT:[ \t]*(PASS|FAIL|SPEC_BLOCKED)[ \t]*$")


def _valid_review_issue_path(path: str) -> bool:
    if (
        not path
        or path != path.strip()
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or not path.isprintable()
    ):
        return False
    parts = path.split("/")
    return all(
        part and part == part.strip() and part not in {".", ".."}
        for part in parts
    )


def _valid_review_issue_identifier(identifier: str) -> bool:
    components = identifier.split("::")
    if len(components) != 3:
        return False
    path, symbol, category = components
    return (
        _valid_review_issue_path(path)
        and _REVIEW_ISSUE_SYMBOL.fullmatch(symbol) is not None
        and _REVIEW_ISSUE_CATEGORY.fullmatch(category) is not None
    )


def _normalize_review_issue_ids(output: str) -> tuple[str, ...]:
    output = output.replace("\r\n", "\n").replace("\r", "\n")
    declarations = [
        line
        for line in output.splitlines()
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    if not declarations:
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과에 유효한 ISSUE_ID가 없습니다."
        )
    matches = list(_REVIEW_ISSUE_ID.finditer(output))
    if len(matches) != len(declarations):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    raw_identifiers = [
        match.group("identifier").strip().casefold()
        for match in matches
    ]
    if any(
        not _valid_review_issue_identifier(identifier)
        for identifier in raw_identifiers
    ):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    identifiers = set(raw_identifiers)
    return tuple(f"ISSUE_ID:{identifier}" for identifier in sorted(identifiers))


def parse_reviewer_result(
    output: str,
) -> tuple[str, tuple[str, ...] | reviewer_specification.SpecificationDeadlock]:
    """Validate the Reviewer response contract before any state transition."""

    lines = output.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    meaningful_lines = [
        (index, line)
        for index, line in enumerate(lines)
        if line.strip()
    ]
    verdict_lines = [
        (index, line)
        for index, line in meaningful_lines
        if _REVIEW_VERDICT_DECLARATION.match(line)
    ]
    if len(verdict_lines) != 1:
        raise ReviewerProtocolError(
            "Reviewer 결과는 정확히 하나의 VERDICT를 포함해야 합니다."
        )
    verdict_index, verdict_line = verdict_lines[0]
    verdict_match = _REVIEW_VERDICT.fullmatch(verdict_line)
    if verdict_match is None:
        raise ReviewerProtocolError(
            "Reviewer VERDICT는 PASS, FAIL 또는 SPEC_BLOCKED이어야 합니다."
        )
    if verdict_index != meaningful_lines[-1][0]:
        raise ReviewerProtocolError(
            "Reviewer VERDICT는 마지막 비어 있지 않은 줄이어야 합니다."
        )

    verdict = verdict_match.group(1).upper()
    issue_declarations = [
        line
        for _index, line in meaningful_lines
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    specification_declarations = [
        line for _index, line in meaningful_lines
        if line.lstrip().upper().startswith("SPEC_DEADLOCK:")
    ]
    if verdict == "SPEC_BLOCKED":
        if issue_declarations:
            raise ReviewerProtocolError("SPEC_BLOCKED 결과에는 ISSUE_ID가 없어야 합니다.")
        try:
            return verdict, reviewer_specification.parse_evidence(output)
        except ValueError as error:
            raise ReviewerProtocolError(str(error)) from error
    if specification_declarations:
        raise ReviewerProtocolError("PASS·FAIL 결과에는 SPEC_DEADLOCK이 없어야 합니다.")
    if verdict == "PASS":
        if issue_declarations:
            raise ReviewerProtocolError(
                "Reviewer PASS 결과에는 ISSUE_ID가 없어야 합니다."
            )
        return verdict, ()
    return verdict, _normalize_review_issue_ids(output)


def _review_issue_paths(output: object) -> list[str]:
    if not isinstance(output, str) or not output.strip():
        return []
    try:
        verdict, details = parse_reviewer_result(output)
    except ReviewerProtocolError:
        return []
    if verdict == "SPEC_BLOCKED":
        assert isinstance(details, reviewer_specification.SpecificationDeadlock)
        return sorted(details.paths)
    if verdict != "FAIL":
        return []
    return sorted(
        {
            match.group("identifier").strip().split("::", 1)[0]
            for match in _REVIEW_ISSUE_ID.finditer(output)
        }
    )


def _normalize_temp_path(match: re.Match[str]) -> str:
    tail = match.group("tail").strip("\\/")
    components = [part for part in re.split(r"[\\/]", tail) if part]
    leaf = components[-1]
    line_start = match.string.rfind("\n", 0, match.start()) + 1
    prefix = match.string[line_start:match.start()]
    if len(components) == 1 and _TEMP_DIRECTORY_CONTEXT.search(prefix):
        return "<TEMP_PATH>"
    return f"<TEMP_PATH>/{leaf}"


def normalize_validation_failure(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> str:
    """Remove unstable execution noise and make failure line ordering irrelevant."""

    if stage not in {"verify", "review"}:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    fingerprint_source = (
        output.fingerprint_source if isinstance(output, VerifyOutput) else output
    )
    normalized = fingerprint_source.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ANSI_ESCAPE.sub("", normalized)
    if worktree is not None:
        path_values = {
            str(worktree),
            str(worktree.resolve()),
            str(worktree).replace("\\", "/"),
            str(worktree).replace("/", "\\"),
        }
        for path_value in sorted(path_values, key=len, reverse=True):
            if path_value:
                normalized = re.sub(
                    re.escape(path_value),
                    "<WORKTREE>",
                    normalized,
                    flags=re.IGNORECASE,
                )
    normalized = _TEMP_PATH_NOISE.sub(_normalize_temp_path, normalized)
    normalized = _TIMESTAMP_NOISE.sub("<TIMESTAMP>", normalized)
    normalized = _DURATION_NOISE.sub("<DURATION>", normalized)
    if stage == "review":
        return "\n".join(_normalize_review_issue_ids(normalized))
    lines = []
    for line in normalized.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        lines.append(line)
    return "\n".join(sorted(lines))


def validation_failure_fingerprints(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    normalized = (
        "전체 verify 시간 초과"
        if stage == "verify" and getattr(output, "timed_out", False)
        else normalize_validation_failure(stage, output, worktree)
    )
    failures = normalized.splitlines() if stage == "review" else [normalized]
    fingerprints = []
    for failure in failures:
        digest = hashlib.sha256()
        digest.update(b"loop-manager-validation-failure-v1\0")
        digest.update(stage.encode("utf-8"))
        digest.update(b"\0")
        digest.update(failure.encode("utf-8"))
        fingerprints.append((digest.hexdigest(), failure))
    return tuple(fingerprints)


def validation_failure_fingerprint(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    fingerprints = validation_failure_fingerprints(stage, output, worktree)
    if len(fingerprints) != 1:
        raise ValueError(
            "복수 Review issue는 validation_failure_fingerprints()로 처리해야 합니다."
        )
    return fingerprints[0]


def _deserialize_failure_history(value: object) -> dict[str, dict[str, object]]:
    if value in {None, ""}:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
    history: dict[str, dict[str, object]] = {}
    for fingerprint, record in parsed.items():
        if not isinstance(fingerprint, str) or not isinstance(record, dict):
            raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
        occurrences = record.get("occurrences")
        if type(occurrences) is not int or occurrences < 1:
            raise RuntimeError("저장된 검증 실패 반복 횟수가 유효하지 않습니다.")
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": str(record.get("sample", "")),
        }
    return history


def clear_validation_failure_history(
    connection: sqlite3.Connection,
    repo: str,
    stage: str,
) -> None:
    column = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }.get(stage)
    if column is None:
        raise ValueError(f"검증 성공 단계가 유효하지 않습니다: {stage}")
    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("지문 이력을 초기화할 활성 ExecPlan을 찾을 수 없습니다.")
    fingerprints = tuple(_deserialize_failure_history(plan[column]))
    with connection:
        runtime_state.RuntimeStateRepository(connection).update_plan(
            repo,
            str(plan["plan_id"]),
            plan_values={column: "{}"},
            enqueue=False,
        )
        recovery_state.RecoveryRepository(connection).clear_correction_occurrences(
            repo, str(plan["plan_id"]), fingerprints,
        )


def pr_for_branch(repo: Path, branch: str, url: str | None = None) -> dict[str, object] | None:
    return github_observation.pr_for_branch(_github_observation_effects_pr_for_branch(), repo, branch, url)


def _fetch_pr_conflict_recovery_refs(
    repo_path: Path,
    state_dir: Path,
    base: str,
    branch: str,
) -> None:
    for ref in dict.fromkeys((base, branch)):
        _recorded_base_fetch(repo_path, state_dir, ref, owner="project")


def _validate_pr_conflict_recovery_identity(
    connection: sqlite3.Connection,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
    *,
    expected_status: str,
) -> tuple[sqlite3.Row, Path, str, str]:
    plan = project_execplan(connection, repo)
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    url = str(pull_request.get("url") or "")
    head_name = str(pull_request.get("headRefName") or "")
    head_oid = str(pull_request.get("headRefOid") or "")
    stored_url = str(project["pr_url"] or "")
    stored_head = str(project["pr_head_oid"] or "")
    if (
        plan is None
        or plan["status"] != expected_status
        or plan_id is None
        or str(plan["plan_id"]) != plan_id
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "PR 충돌 복구 workspace의 SQLite 소유권을 확인할 수 없습니다."
        )
    if not stored_url or url != stored_url:
        raise PublicationSafetyError(
            "PR 충돌 복구 URL이 저장된 소유 PR과 일치하지 않습니다."
        )
    if not branch or head_name != branch:
        raise PublicationSafetyError(
            "PR 충돌 복구 head branch가 관리 branch와 일치하지 않습니다."
        )
    if (
        not head_oid
        or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head_oid) is None
        or not stored_head
        or head_oid != stored_head
    ):
        raise PublicationSafetyError(
            "PR 충돌 복구 head OID가 저장된 PR head와 일치하지 않습니다."
        )
    return plan, worktree, branch, head_oid


def _validate_pr_conflict_recovery_ownership(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
    *,
    expected_status: str = "PR_OPEN",
) -> tuple[sqlite3.Row, Path, str, str, str]:
    """Prove the saved URL, branch, workspace, and observed remote head."""

    plan, worktree, branch, head_oid = (
        _validate_pr_conflict_recovery_identity(
            connection,
            repo,
            project,
            pull_request,
            expected_status=expected_status,
        )
    )
    plan_id = str(plan["plan_id"])
    validate_execplan_workspace_identity(
        state_dir, repo, plan_id, branch, worktree
    )
    make_worktree(
        repo_path,
        worktree,
        branch,
        str(project["base_branch"]),
        journal_state_dir=state_dir,
        journal_owner=f"plan:{plan_id}",
        allow_create_branch=False,
        allow_recovery=True,
        allow_existing=True,
    )
    snapshot = capture_git_snapshot(worktree)
    ignored_paths = frozenset(_ignored_worktree_paths(worktree))
    publishable_dirty = {
        path: state
        for path, state in snapshot.dirty.items()
        if path not in ignored_paths
    }
    flagged_paths = sorted(
        path for path, entries in snapshot.index.items()
        if _non_default_index_flags(entries)
    )
    if (
        snapshot.branch != branch or snapshot.head != head_oid
        or publishable_dirty or flagged_paths or snapshot.operations
    ):
        details = []
        if snapshot.branch != branch:
            details.append(f"branch {snapshot.branch} (expected {branch})")
        if snapshot.head != head_oid:
            details.append(f"HEAD {snapshot.head} (expected {head_oid})")
        if publishable_dirty:
            details.append(
                "게시 가능 변경 " + ", ".join(sorted(publishable_dirty)[:8])
            )
        if flagged_paths:
            details.append("index flags " + ", ".join(flagged_paths[:8]))
        if snapshot.operations:
            details.append(
                "Git operation " + ", ".join(sorted(snapshot.operations))
            )
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 시작 전 관리 worktree가 관측한 원격 head의 "
            "clean 상태와 일치하지 않습니다: " + "; ".join(details)
        )
    remote_oid = remote_branch_oid(repo_path, branch)
    if remote_oid != head_oid:
        raise PublicationSafetyError(
            "PR 충돌 복구 시작 전 원격 branch가 관측한 PR head와 "
            "일치하지 않습니다."
        )
    base_oid = git(
        repo_path,
        "rev-parse",
        f"refs/remotes/origin/{project['base_branch']}^{{commit}}",
    ).stdout.strip()
    overlaps = _ignored_paths_overlapping_git_update(worktree, base_oid)
    if overlaps:
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 tracked 경로와 겹치는 ignored 파일: "
            + ", ".join(overlaps[:8])
        )
    return plan, worktree, branch, head_oid, base_oid


def _validate_blocked_pr_without_conflict(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> None:
    """Read-only proof for releasing an old conflict preflight false positive."""

    plan, worktree, branch, head_oid = _validate_pr_conflict_recovery_identity(
        connection, repo, project, pull_request, expected_status="PLAN_BLOCKED"
    )
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    if not worktree.is_dir():
        raise WorkspaceOwnershipError("PR 충돌 복구 worktree가 존재하지 않습니다.")
    snapshot = capture_git_snapshot(worktree)
    ignored = frozenset(_ignored_worktree_paths(worktree))
    dirty = sorted(path for path in snapshot.dirty if path not in ignored)
    flags = sorted(
        path for path, entries in snapshot.index.items()
        if _non_default_index_flags(entries)
    )
    if snapshot.branch != branch or snapshot.head != head_oid or dirty or flags or snapshot.operations:
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 차단 해제 전 관리 worktree 상태 불일치: "
            + "; ".join(filter(None, (
                f"branch {snapshot.branch} (expected {branch})" if snapshot.branch != branch else "",
                f"HEAD {snapshot.head} (expected {head_oid})" if snapshot.head != head_oid else "",
                "게시 가능 변경 " + ", ".join(dirty[:8]) if dirty else "",
                "index flags " + ", ".join(flags[:8]) if flags else "",
                "Git operation " + ", ".join(sorted(snapshot.operations)) if snapshot.operations else "",
            )))
        )
    remote = git(
        repo_path, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
    ).stdout.splitlines()
    if len(remote) != 1 or remote[0].split("\t", 1)[0] != head_oid:
        raise PublicationSafetyError(
            "PR 충돌 복구 차단 해제 전 원격 branch가 관측한 PR head와 일치하지 않습니다."
        )


def _is_pr_conflict_preflight_block(
    project: sqlite3.Row,
    plan: sqlite3.Row | None,
) -> bool:
    if plan is None:
        return False
    return bool(
        plan["blocked_reason"] == PR_CONFLICT_PREFLIGHT_BLOCK_REASON
        or str(project["last_error"] or "").startswith(
            PR_CONFLICT_PREFLIGHT_ERROR_PREFIX
        )
    )


def _validate_active_pr_conflict_recovery(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> None:
    """Revalidate races without resetting an in-progress recovery stage."""

    plan = active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    observed_head = str(project["pr_conflict_head_oid"] or "")
    if (
        plan is None
        or not int(plan["pr_conflict_recovery"])
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "진행 중인 PR 충돌 복구의 SQLite 소유권을 확인할 수 없습니다."
        )
    if (
        str(pull_request.get("url") or "") != str(project["pr_url"] or "")
        or str(pull_request.get("headRefName") or "") != branch
        or not observed_head
        or str(pull_request.get("headRefOid") or "") != observed_head
    ):
        raise PublicationSafetyError(
            "진행 중인 PR의 URL, head branch 또는 head OID가 복구 시작 시 "
            "관측값과 달라졌습니다."
        )
    _fetch_pr_conflict_recovery_refs(
        repo_path, state_dir, str(project["base_branch"]), branch
    )
    if remote_branch_oid(repo_path, branch) != observed_head:
        raise PublicationSafetyError(
            "진행 중인 PR의 원격 head가 복구 시작 시 관측값과 달라졌습니다."
        )
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    make_worktree(
        repo_path,
        worktree,
        branch,
        str(project["base_branch"]),
        journal_state_dir=state_dir,
        journal_owner=f"plan:{plan['plan_id']}",
        allow_create_branch=False,
        allow_recovery=True,
        allow_existing=True,
        allow_rebase=_rebase_in_progress_for_branch(worktree, branch),

    )

def remote_branch_oid(repo: Path, branch: str) -> str | None:
    """Return the fetched remote branch OID without consulting local branch names."""

    result = git(
        repo,
        "show-ref",
        "--hash",
        f"refs/remotes/origin/{branch}",
        check=False,
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        raise RuntimeError(
            f"원격 publication branch 확인 실패 (git 종료 코드 {result.returncode})."
        )
    oid = result.stdout.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid) is None:
        raise RuntimeError("원격 publication branch OID 형식이 유효하지 않습니다.")
    return oid


def _owned_publication_plan(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
) -> sqlite3.Row:
    plan = active_execplan(connection, repo)
    if (
        plan is None
        or plan["status"] != "PUBLISHING"
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "publication branch의 Loop Manager 소유권을 SQLite에서 확인할 수 없습니다."
        )
    validate_execplan_workspace_identity(
        state_dir,
        repo,
        str(plan["plan_id"]),
        branch,
        worktree,
    )
    validate_managed_worktree(repo_path, worktree, branch)
    return plan


def record_remote_publication_branch(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    remote_oid: str | None,
) -> None:
    """Remember only a remote branch proven to be this managed branch."""

    if remote_oid is None:
        return
    plan = _owned_publication_plan(
        connection, repo_path, state_dir, repo, branch, worktree
    )
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    recorded_oid = str(plan["publication_remote_oid"] or "")
    if remote_oid == local_oid:
        if recorded_oid != remote_oid:
            update_project(
                connection,
                repo,
                publication_remote_oid=remote_oid,
            )
        return
    if recorded_oid == remote_oid:
        return
    raise PublicationSafetyError(
        "PR이 없는 원격 branch가 현재 관리 HEAD 또는 저장된 게시 OID와 "
        f"일치하지 않아 갱신을 거부합니다: {branch}"
    )


def push_managed_publication_branch(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    expected_oid: str,
) -> str:
    """Push the expected reviewed candidate with an explicit owned-branch lease."""

    plan = _owned_publication_plan(
        connection, repo_path, state_dir, repo, branch, worktree
    )
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if not expected_oid or local_oid != expected_oid:
        raise PublicationSafetyError('push 대상 HEAD가 verify·Review를 통과한 후보와 다릅니다.')
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(plan['plan_id']))
    def observe_remote():
        return managed_rebase_adoption.checkpoint_remote_oid(
            checkpoint, repo_path, branch, git=git, read_fetched=remote_branch_oid)
    remote_oid = observe_remote()
    if remote_oid is None and plan['publication_remote_oid']:
        raise PublicationSafetyError(
            '저장된 게시 branch가 원격에서 삭제돼 새 branch 생성으로 복구할 수 없습니다.'
        )
    record_remote_publication_branch(
        connection,
        repo_path,
        state_dir,
        repo,
        branch,
        worktree,
        remote_oid,
    )
    if remote_oid == local_oid:
        return local_oid

    # The explicit destination needs no upstream; -u mutates shared config
    # and invalidates snapshots of other interrupted plans.
    push_arguments = [
        "push",
        "origin",
        f"{local_oid}:refs/heads/{branch}",
    ]
    if remote_oid is not None:
        recorded_oid = str(plan["publication_remote_oid"] or "")
        if recorded_oid != remote_oid:
            refreshed = active_execplan(connection, repo)
            recorded_oid = str(
                refreshed["publication_remote_oid"] if refreshed else ""
            )
        if recorded_oid != remote_oid:
            raise PublicationSafetyError(
                "저장된 원격 branch OID와 fetch 결과가 달라 강제 갱신을 거부합니다."
            )
        live = git(repo_path, 'ls-remote', '--heads', 'origin',
                   f'refs/heads/{branch}', check=False)
        if (live.returncode or
                [line.split() for line in live.stdout.splitlines() if line.strip()]
                != [[remote_oid, f'refs/heads/{branch}']]):
            raise PublicationSafetyError(
                'push 직전 원격 branch가 저장된 lease OID와 다릅니다.'
            )
        if plan['pr_url'] or pr_for_branch(repo_path, branch) is not None:
            raise PublicationSafetyError(
                'PR이 있는 branch를 checkpoint 복구 강제 push할 수 없습니다.'
            )
        push_arguments.insert(
            1,
            f"--force-with-lease=refs/heads/{branch}:{remote_oid}",
        )
    else:
        live = git(repo_path, 'ls-remote', '--heads', 'origin',
                   f'refs/heads/{branch}', check=False)
        if live.returncode or live.stdout.strip():
            raise PublicationSafetyError(
                '새 branch push 직전 원격 ref가 생성되거나 조회에 실패했습니다.'
            )
        push_arguments.insert(1, f"--force-with-lease=refs/heads/{branch}:")

    _owned_publication_plan(connection, repo_path, state_dir, repo, branch, worktree)
    if (git(worktree, 'rev-parse', 'HEAD').stdout.strip() != local_oid
            or git(worktree, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip() != local_oid):
        raise PublicationSafetyError('push 직전 소유 branch 또는 관리 HEAD가 변경됐습니다.')
    if pr_for_branch(repo_path, branch) is not None:
        raise PublicationSafetyError('push 직전 게시 대상 branch의 PR이 변경됐습니다.')

    pushed = git(worktree, *push_arguments, check=False)
    if pushed.returncode:
        raise RuntimeError(f"publication branch push 실패:\n{pushed.stdout[-8000:]}")

    _recorded_base_fetch(repo_path, state_dir, branch, owner="project")
    confirmed_oid = observe_remote()
    if confirmed_oid != local_oid:
        raise RuntimeError(
            "publication branch push 결과를 원격에서 확인할 수 없습니다."
        )
    confirmed = git(repo_path, 'ls-remote', '--heads', 'origin',
                    f'refs/heads/{branch}', check=False)
    if (confirmed.returncode or
            [line.split() for line in confirmed.stdout.splitlines() if line.strip()]
            != [[local_oid, f'refs/heads/{branch}']]):
        raise RuntimeError('publication branch push의 새 OID를 원격에서 확인할 수 없습니다.')
    update_project(
        connection,
        repo,
        publication_remote_oid=local_oid,
    )
    return local_oid


def push_owned_pr_conflict_recovery(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    pull_request: Mapping[str, object],
    expected_oid: str,
) -> str:
    """Push the expected reviewed candidate to the owned PR with its exact lease."""

    plan = active_execplan(connection, repo)
    project = connection.execute(
        "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if (
        project is None
        or plan is None
        or plan["status"] != "PUBLISHING"
        or not int(plan["pr_conflict_recovery"])
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "기존 PR 충돌 복구 게시 소유권을 SQLite에서 확인할 수 없습니다."
        )
    url = str(plan["pr_url"] or "")
    observed_head = str(plan["pr_conflict_head_oid"] or "")
    target_base = str(plan["pr_conflict_base_oid"] or "")
    if (
        str(pull_request.get("state") or "").upper() != "OPEN"
        or str(pull_request.get("url") or "") != url
        or str(pull_request.get("headRefName") or "") != branch
        or str(pull_request.get("headRefOid") or "") != observed_head
    ):
        raise PublicationSafetyError(
            "게시 직전 PR URL, state, head branch 또는 head OID가 복구 "
            "시작 시 관측값과 다릅니다."
        )
    if (
        not observed_head
        or not target_base
        or str(plan["active_sha"] or "") != target_base
        or str(plan["validated_base_sha"] or "") != target_base
    ):
        raise PublicationSafetyError(
            "기존 PR 충돌 복구의 lease OID와 검증 base가 완전하지 않습니다."
        )
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    validate_managed_worktree(repo_path, worktree, branch)
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if not expected_oid or local_oid != expected_oid:
        raise PublicationSafetyError('PR push 대상 HEAD가 verify·Review를 통과한 후보와 다릅니다.')
    contains_base = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        local_oid,
        check=False,
    )
    if contains_base.returncode != 0:
        raise PublicationSafetyError(
            "검증된 base OID가 게시할 관리 HEAD의 조상이 아닙니다."
        )

    _fetch_pr_conflict_recovery_refs(
        repo_path,
        state_dir,
        str(project["base_branch"]),
        branch,
    )
    remote_oid = remote_branch_oid(repo_path, branch)
    if remote_oid != observed_head:
        raise PublicationSafetyError(
            "게시 직전 원격 PR head가 복구 시작 시 관측값과 달라졌습니다."
        )
    current_pr = pr_for_branch(repo_path, branch, url)
    if (current_pr is None or any(current_pr.get(key) != pull_request.get(key)
                                 for key in ('url', 'state', 'headRefName', 'headRefOid'))):
        raise PublicationSafetyError('push 직전 PR identity 또는 head가 변경됐습니다.')
    validate_managed_worktree(repo_path, worktree, branch)
    if (git(worktree, 'rev-parse', 'HEAD').stdout.strip() != local_oid
            or git(worktree, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip() != local_oid):
        raise PublicationSafetyError('PR push 직전 소유 branch 또는 관리 HEAD가 변경됐습니다.')
    lease = f"--force-with-lease=refs/heads/{branch}:{observed_head}"
    pushed = git(
        worktree,
        "push",
        lease,
        "origin",
        f"{local_oid}:refs/heads/{branch}",
        check=False,
    )
    if pushed.returncode:
        normalized = pushed.stdout.casefold()
        if any(
            marker in normalized
            for marker in ("stale info", "fetch first", "rejected", "lease")
        ):
            raise PublicationSafetyError(
                "기존 PR branch lease가 거부되어 원격 변경을 덮어쓰지 않았습니다:\n"
                + pushed.stdout[-8000:]
            )
        raise RuntimeError(
            f"기존 PR 충돌 복구 push 실패:\n{pushed.stdout[-8000:]}"
        )

    _recorded_base_fetch(repo_path, state_dir, branch, owner="project")
    if remote_branch_oid(repo_path, branch) != local_oid:
        raise RuntimeError(
            "기존 PR 충돌 복구 push 결과를 원격에서 확인할 수 없습니다."
        )
    return local_oid

def recover_confirmed_pr_conflict_push(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> bool:
    """Recover only a previously pushed local HEAD, never an arbitrary remote head."""

    if (
        project["phase"] != "PUBLISHING"
        or not int(project["pr_conflict_recovery"])
    ):
        return False
    plan = active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    remote_head = str(pull_request.get("headRefOid") or "")
    observed_head = str(project["pr_conflict_head_oid"] or "")
    target_base = str(project["pr_conflict_base_oid"] or "")
    if (
        plan is None
        or plan["status"] != "PUBLISHING"
        or not int(plan["pr_conflict_recovery"])
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(pull_request.get("state") or "").upper() != "OPEN"
        or str(pull_request.get("url") or "") != str(project["pr_url"] or "")
        or str(pull_request.get("headRefName") or "") != branch
        or not remote_head
        or remote_head == observed_head
        or not target_base
        or str(project["active_sha"] or "") != target_base
        or str(project["validated_base_sha"] or "") != target_base
    ):
        return False
    validate_execplan_workspace_identity(
        state_dir, repo, str(plan["plan_id"]), branch, worktree
    )
    validate_managed_worktree(repo_path, worktree, branch)
    local_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if remote_head != local_oid:
        return False
    contains_base = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        local_oid,
        check=False,
    )
    if contains_base.returncode != 0:
        return False
    _fetch_pr_conflict_recovery_refs(
        repo_path, state_dir, str(project["base_branch"]), branch
    )
    if remote_branch_oid(repo_path, branch) != local_oid:
        return False
    runtime_state.RuntimeStateRepository(connection).open_pull_request(
        repo,
        publication_remote_oid=local_oid,
        pr_head_oid=local_oid,
        pr_conflict_recovery=0,
        pr_conflict_head_oid=None,
        pr_conflict_base_oid=None,
    )
    return True


def _validate_managed_path_boundary(
    state_dir: Path,
    worktree: Path,
    expected: Path,
    label: str,
) -> None:
    lexical_worktree = os.path.normcase(
        os.path.abspath(worktree.expanduser())
    )
    lexical_expected = os.path.normcase(
        os.path.abspath(expected.expanduser())
    )
    if lexical_worktree != lexical_expected:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 결정적 소유 경계와 다릅니다: "
            f"{worktree} (expected {expected})"
        )
    state_root = _canonical_path(state_dir)
    resolved = _canonical_path(worktree)
    try:
        resolved.relative_to(state_root)
    except ValueError as error:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 Loop Manager 상태 디렉터리 밖을 가리킵니다: "
            f"{worktree}"
        ) from error

def _worktree_catalog(repo: Path) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    if current:
        records.append(current)
    return records


def _rebase_head_name(worktree: Path) -> str | None:
    for directory in ("rebase-merge", "rebase-apply"):
        result = git(worktree, "rev-parse", "--git-path", f"{directory}/head-name", check=False)
        if result.returncode:
            continue
        git_path = Path(result.stdout.strip())
        if not git_path.is_absolute():
            git_path = worktree / git_path
        if git_path.is_file():
            return git_path.read_text(encoding="utf-8").strip()
    return None


def _rebase_in_progress_for_branch(worktree: Path, branch: str) -> bool:
    return _rebase_head_name(worktree) == f"refs/heads/{branch}"


def _owned_rebase_state_dir(repo: str, plan_id: str, worktree: Path) -> Path | None:
    """Legacy recovery calls can be journaled only for a deterministic plan path."""
    if not re.fullmatch(r"G-\d+", plan_id) or len(worktree.parents) < 3:
        return None
    state_dir = worktree.parents[2]
    _branch, expected = expected_execplan_workspace(state_dir, repo, plan_id)
    return state_dir if _canonical_path(expected) == _canonical_path(worktree) else None


def _owned_rebase_metadata(worktree: Path) -> dict[str, str] | None:
    for directory in ("rebase-merge", "rebase-apply"):
        values = {}
        for name in ("orig-head", "head-name", "onto"):
            result = git(worktree, "rev-parse", "--git-path", f"{directory}/{name}",
                         check=False)
            if result.returncode:
                break
            path = Path(result.stdout.strip())
            if not path.is_absolute():
                path = worktree / path
            if not path.is_file():
                break
            values[name] = path.read_text(encoding="utf-8").strip()
        if len(values) == 3:
            return values
    return None


def _owned_rebase_journal_state(worktree: Path, branch: str) -> dict[str, object]:
    snapshot = capture_git_snapshot(worktree)
    metadata = _owned_rebase_metadata(worktree)
    rebase_head = None
    if metadata is not None:
        result = git(worktree, 'rev-parse', '--verify', 'REBASE_HEAD', check=False)
        if result.returncode == 0:
            rebase_head = result.stdout.strip()
    return json.loads(json.dumps({
        "head": snapshot.head, "branch": snapshot.branch,
        "ref": snapshot.refs.get(f"refs/heads/{branch}"),
        "index": snapshot.index, "dirty": snapshot.dirty,
        "file_evidence": rebase_file_evidence.capture(worktree, snapshot.head, snapshot.dirty),
        "registration": snapshot.worktrees.get(str(_canonical_path(worktree))),
        "operations": snapshot.operations,
        "metadata": metadata, "rebase_head": rebase_head,
        "unmerged": bool(git(worktree, "ls-files", "-u", "-z").stdout),
        "index_clean": git(worktree, "diff", "--cached", "--quiet",
                           check=False).returncode == 0,
    }, sort_keys=True))


def _current_rebase_service(connection, repo_path, state_dir, repo, plan_id,
                            worktree, branch, **options):
    return managed_rebase_runtime.build_service(
        _managed_rebase_runtime_effects(), connection, repo_path, state_dir, repo, plan_id,
        worktree, branch, **options,
    )


def _finish_owned_direct_rebase(
    state_dir, connection, repo_path, repo, plan_id, worktree, branch,
    *, allow_equal=False, agent_session_id=None,
):
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id)
    if checkpoint.operation != 'rebase':
        return False
    service = _current_rebase_service(
        connection, repo_path, state_dir, repo, plan_id, worktree, branch,
    )
    _, decision = service.require_owned()
    if decision.status.value == 'ready_to_start':
        return False
    return manager_git_journal.record_current_rebase_result(
        state_dir, repo=str(_canonical_path(repo_path)),
        target=str(_canonical_path(worktree)), owner=f'plan:{plan_id}',
        decision=decision, observed=_owned_rebase_journal_state(worktree, branch),
    )


def _journaled_direct_rebase(
    connection: sqlite3.Connection, repo_path: Path, repo: str, plan_id: str,
    worktree: Path, branch: str, target_oid: str, *, autostash: bool = False,
    agent_session_id: str | None = None,
    observed_remote: tuple[str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    state_dir = _owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None:
        raise manager_git_journal.MutationEvidenceError(
            'direct rebase: owned journal workspace unavailable'
        )
    before = _owned_rebase_journal_state(worktree, branch)
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    remote_oid = (remote_branch_oid(repo_path, branch) if observed_remote is None
                  else observed_remote[0])
    if current.observed_remote_oid not in (None, remote_oid):
        raise manager_git_journal.MutationEvidenceError(
            "Manager rebase 전 게시 원격 OID가 checkpoint와 다릅니다."
        )
    if manager_git_journal.live_remote_branch_oid(git, repo_path, branch) != remote_oid:
        raise manager_git_journal.MutationEvidenceError(
            'Manager rebase start: live remote differs from fetched OID'
        )
    if current.operation == 'rebase':
        service = _current_rebase_service(connection, repo_path, state_dir, repo,
                                         plan_id, worktree, branch)
        _, decision = service.require_owned()
        if decision.status.value == 'complete':
            _complete_direct_rebase_checkpoint(
                connection, repo, plan_id, worktree, current.target_base_oid,
                repo_path=repo_path, branch=branch, agent_session_id=agent_session_id)
            return subprocess.CompletedProcess([], 0, 'current rebase already complete')
        if decision.status.value != 'ready_to_start':
            return subprocess.CompletedProcess([], 1, 'owned rebase is waiting for resolution')
        if current.target_base_oid != target_oid:
            raise manager_git_journal.MutationEvidenceError('current rebase target differs')
    else:
        try:
            row = connection.execute(
                'SELECT pr_head_oid, pr_conflict_head_oid FROM projects WHERE repo = ?',
                (repo,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        pr_head_oid = (str(row['pr_head_oid'] or row['pr_conflict_head_oid'] or '')
                       or None) if row is not None else None
        if pr_head_oid and pr_head_oid != remote_oid:
            raise manager_git_journal.MutationEvidenceError(
                'Manager rebase start: PR head differs from remote OID'
            )
        checkpoint.record(
            repo, plan_id, expected_generation=current.generation,
            operation='rebase', expected_head=before['head'],
            target_base_oid=target_oid, observed_remote_oid=remote_oid,
            snapshot={**(current.snapshot or {}), 'before_head': before['head'],
                      'rebase_remote_oid': remote_oid, 'rebase_policy': 'current_state'},
        )
    token = manager_git_journal.begin_current_rebase(
        state_dir, owner=f'plan:{plan_id}', repo=str(_canonical_path(repo_path)),
        target=str(_canonical_path(worktree)), before=before,
        expected={'branch': branch, 'before_head': before['head'],
                  'target_base_oid': target_oid, 'remote_oid': remote_oid,
                  'agent_session_id': agent_session_id})
    environment = dict(os.environ)
    environment["GIT_REFLOG_ACTION"] = f"loop-manager-rebase:{token}"
    scoped_options = (("-c", f"core.hooksPath={os.devnull}", "-c", "rebase.updateRefs=false")
                      if observed_remote is not None else ())
    result = git(worktree, *scoped_options, "rebase", *(("--autostash",) if autostash else ()),
                 target_oid, check=False, env=environment)
    if result.returncode == 0:
        manager_git_journal.record_rebase_invocation(
            state_dir, str(_canonical_path(repo_path)),
            str(_canonical_path(worktree)), token, returncode=result.returncode,
        )
    if git(worktree, 'ls-files', '-u', '-z').stdout and not _owned_rebase_metadata(worktree):
        matched = re.search(r'Created autostash: ([0-9a-fA-F]{7,64})', result.stdout)
        if matched:
            stash = git(worktree, 'rev-parse', '--verify', f'{matched.group(1)}^{{commit}}', check=False)
            if stash.returncode == 0:
                current = checkpoint.get(repo, plan_id)
                checkpoint.record(repo, plan_id, expected_generation=current.generation,
                    operation='rebase', expected_head=current.expected_head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot={**(current.snapshot or {}), 'stash_oid': stash.stdout.strip()})
    _finish_owned_direct_rebase(
        state_dir, connection, repo_path, repo, plan_id, worktree, branch,
        allow_equal=True, agent_session_id=agent_session_id,
    )
    return result


def _direct_rebase_history_proven(
    connection, repo_path, repo, plan_id, worktree, branch, before_head,
    after_head, target_base, agent_session_id,
):
    # Compatibility name: assess the current result, not history.
    state_dir = _owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None:
        return False
    try:
        observed, decision = _current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            target=target_base,
        ).require_owned()
        return decision.status.value == 'complete' and observed.head == after_head
    except (RuntimeError, OSError):
        return False


def _direct_rebase_result_matches_observation(
    sealed: Mapping[str, object], observed: Mapping[str, object],
    *, preserved_worktree: bool,
) -> bool:
    if sealed == observed:
        return True
    return bool(
        preserved_worktree and sealed.get('metadata') is None
        and observed.get('metadata') is None
        and all(sealed.get(field) == observed.get(field) for field in (
            'head', 'branch', 'ref', 'registration', 'operations',
        ))
    )


def _direct_rebase_restored_stage_matches(
    intent: Mapping[str, object], sealed: Mapping[str, object],
    observed: Mapping[str, object],
) -> bool:
    """Prove Manager-restored staging from the rebase's frozen before state."""
    before = intent.get('before')
    if (not isinstance(before, dict)
            or not isinstance(before.get('dirty'), dict)
            or not before['dirty']
            or not isinstance(before.get('index'), dict)
            or not isinstance(sealed.get('dirty'), dict)
            or not isinstance(sealed.get('index'), dict)
            or not isinstance(observed.get('dirty'), dict)
            or not isinstance(observed.get('index'), dict)
            or set(sealed['dirty']) != set(before['dirty'])
            or observed['dirty'] != before['dirty']
            or observed.get('file_evidence') != sealed.get('file_evidence')
            or observed.get('rebase_head') != sealed.get('rebase_head')
            or observed.get('unmerged') != sealed.get('unmerged')
            or not _direct_rebase_result_matches_observation(
                sealed, observed, preserved_worktree=True,
            )):
        return False
    staged_paths = {
        path for path, state in before['dirty'].items()
        if (isinstance(state, list) and state
            and isinstance(state[0], list) and 'staged' in state[0])
    }
    if not staged_paths or observed.get('index_clean') is not False:
        return False
    for path in before['index'].keys() | sealed['index'].keys() | observed['index'].keys():
        expected = (before['index'] if path in staged_paths else sealed['index'])
        if observed['index'].get(path) != expected.get(path):
            return False
    return True


def _complete_direct_rebase_checkpoint(
    connection, repo, plan_id, worktree, target_base_oid, *, repo_path,
    branch, agent_session_id, preserved_worktree=False, preserved_marker=None,
    preserved_observation=None,
):
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    saved = current.snapshot or {}
    legacy_completed = (current.operation is None and not saved.get('current_rebase_acceptance')
                        and (saved.get('completed_rebase') or saved.get('rebase_completion')))
    publication_pending = (current.operation == 'rebase_publish'
                           or current.operation is None
                           and managed_rebase_validation.valid_acceptance(saved.get('current_rebase_acceptance')))
    if current.operation != 'rebase' and not legacy_completed and not publication_pending:
        return
    state_dir = _owned_rebase_state_dir(repo, plan_id, worktree)
    if state_dir is None or current.target_base_oid != target_base_oid:
        raise manager_git_journal.MutationEvidenceError('current rebase target or workspace missing')
    if publication_pending:
        _publish_direct_rebase_checkpoint(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch)
        return
    if preserved_observation is not None:
        live = _owned_rebase_journal_state(worktree, branch)
        if live != preserved_observation:
            changed_fields = sorted(key for key in live.keys() | preserved_observation.keys()
                                    if live.get(key) != preserved_observation.get(key))
            raise manager_git_journal.MutationEvidenceError(
                f'direct rebase completion: observation drift={changed_fields}')
    service = _current_rebase_service(
        connection, repo_path, state_dir, repo, plan_id, worktree, branch,
    )
    runtime = runtime_state.RuntimeStateRepository(connection)
    projected = runtime.projected(repo)
    if projected is None or projected.status == 'PLAN_BLOCKED':
        selected = runtime.get(repo, plan_id)
        observed, decision = service.require_owned()
        if selected is None or selected.status != 'PLAN_BLOCKED' or decision.status.value != 'complete':
            raise manager_git_journal.MutationEvidenceError('current rebase execution is not selected')
        if projected is not None:
            runtime.complete_rebase(
                selected, checkpoint, acceptance=decision.acceptance,
                project_values={'phase': selected.phase},
                reobserve=lambda: service.observe()[1],
            )
        else:
            with runtime_state._transaction(connection):
                if runtime.get(repo, plan_id) != selected or checkpoint.get(repo, plan_id) != current:
                    raise manager_git_journal.MutationEvidenceError('current blocked rebase ownership changed')
                if service.observe()[1] != decision or runtime.projected(repo) is not None:
                    raise manager_git_journal.MutationEvidenceError('current blocked rebase state changed')
                values = managed_rebase_adoption.revalidation_values(target_base_oid)
                for field in ('phase', 'last_error', 'revision_resume_status'):
                    values.pop(field)
                checkpoint.accept_current_rebase(
                    repo, plan_id, acceptance=decision.acceptance,
                    prior_artifacts={field: getattr(selected, field) for field in values},
                )
                runtime.transition(repo, plan_id, plan_values=values,
                                   project_values={'phase': connection.execute(
                                       'SELECT phase FROM projects WHERE repo = ?', (repo,)
                                   ).fetchone()[0]},
                                   expected_status='PLAN_BLOCKED', enqueue=False)
    else:
        if projected.plan_id != plan_id:
            raise manager_git_journal.MutationEvidenceError('current rebase execution is not selected')
        service.adopt(_ExecutionStatePort(connection, runtime, repo), checkpoint)
    # Preserve the clean-index publication boundary before a blocked release
    # restores its original staging for the next Worker turn.
    _publish_direct_rebase_checkpoint(
        connection, repo_path, state_dir, repo, plan_id, worktree, branch)


def _publish_direct_rebase_checkpoint(connection, repo_path, state_dir, repo,
                                      plan_id, worktree, branch):
    from loop_manager_internal.execution.execution_state import DirectRebasePublicationStatePort
    project_recovery = scoped_blocking.project_block(connection, repo) is not None
    if project_recovery and not project_blocked_recovery.project_slot_available(connection, repo, plan_id):
        raise manager_git_journal.MutationEvidenceError('project checkpoint publication is not selected')

    def publication_git(target, *arguments, **options):
        if project_recovery and arguments[:1] == ('push',):
            return git(target, '-c', f'core.hooksPath={os.devnull}', *arguments, **options)
        return git(target, *arguments, **options)

    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    state = DirectRebasePublicationStatePort(
        connection, runtime_state.RuntimeStateRepository(connection), repo, plan_id)
    owner = connection.execute(
        "SELECT accepted_remote_oid FROM blocked_checkpoints WHERE repo = ? AND plan_id = ? "
        "AND phase = 'published'", (repo, plan_id)).fetchone()

    def owns_remote(oid, selected):
        if owner is None:
            return True
        original = owner['accepted_remote_oid']
        if not original:
            return False
        return checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, plan_id, branch, worktree, original, git,
            recorded_oid=original, require_live=oid == original) == original

    managed_rebase_publication.publish_checkpoint(
        state=state, checkpoints=checkpoints, repo=repo, plan_id=plan_id,
        repo_path=repo_path, worktree=worktree, branch=branch,
        service=lambda **options: _current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch, **options),
        owns_remote=owns_remote, git=publication_git, lookup_pr=pr_for_branch)


def validate_managed_worktree(
    repo: Path,
    worktree: Path,
    branch: str | None,
    *,
    allow_rebase: bool = False,
) -> None:
    return workspace.validate_managed_worktree(_workspace_effects_validate_managed_worktree(), repo, worktree, branch, allow_rebase=allow_rebase)

def validate_reserved_worktree(
    repo: Path,
    worktree: Path,
    branch: str | None,
    expected_head: str,
) -> None:
    return workspace.validate_reserved_worktree(_workspace_effects_validate_reserved_worktree(), repo, worktree, branch, expected_head)


def make_worktree(
    repo: Path,
    worktree: Path,
    branch: str,
    base: str,
    *,
    allow_recovery: bool = True,
    allow_existing: bool = True,
    allow_rebase: bool = False,
    allow_create_branch: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
    before_create: Callable[[], None] | None = None,
    journal_state_dir: Path | None = None,
    journal_owner: str | None = None,
) -> str:
    return workspace.make_worktree(_workspace_effects_make_worktree(), repo, worktree, branch, base, allow_recovery=allow_recovery, allow_existing=allow_existing, allow_rebase=allow_rebase, allow_create_branch=allow_create_branch, ownership_reserved=ownership_reserved, expected_head=expected_head, before_create=before_create, journal_state_dir=journal_state_dir, journal_owner=journal_owner)


def make_planning_worktree(
    repo: Path,
    worktree: Path,
    base: str,
    *,
    allow_create: bool = True,
    before_create: Callable[[], None] | None = None,
    allow_existing: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
) -> str:
    return workspace.make_planning_worktree(_workspace_effects_make_planning_worktree(), repo, worktree, base, allow_create=allow_create, before_create=before_create, allow_existing=allow_existing, ownership_reserved=ownership_reserved, expected_head=expected_head)


def validate_execplan_workspace_identity(
    state_dir: Path,
    repo: str,
    plan_id: str,
    branch: str,
    worktree: Path,
) -> None:
    return workspace.validate_execplan_workspace_identity(_workspace_effects_validate_execplan_workspace_identity(), state_dir, repo, plan_id, branch, worktree)


def _nul_git_paths(repo: Path, *arguments: str) -> list[str]:
    return scope_observation._nul_git_paths(repo, *arguments, effects=_scope_observation_effects())


def _index_entries(worktree: Path) -> dict[str, tuple[str, ...]]:
    return scope_observation._index_entries(worktree, effects=_scope_observation_effects())


def _shared_ref_entries(worktree: Path) -> dict[str, tuple[str, str]]:
    return scope_observation._shared_ref_entries(worktree, effects=_scope_observation_effects())


def _registered_worktree_entries(worktree: Path) -> dict[str, tuple[str, ...]]:
    return scope_observation._registered_worktree_entries(worktree, effects=_scope_observation_effects())


def _config_entries(worktree: Path) -> dict[str, tuple[str, ...]]:
    return scope_observation._config_entries(worktree, effects=_scope_observation_effects())


def _with_proven_legacy_ignore_config(saved: GitSnapshot, current_config: dict[str, tuple[str, ...]]) -> GitSnapshot:
    return scope_observation._with_proven_legacy_ignore_config(
        saved,
        current_config,
        effects=_scope_observation_effects(),
    )


# AUTO_MERGE is deliberately excluded. The ort strategy may leave this
# auxiliary tree ref behind after a conflicted operation has completed, so it
# does not prove that a Git operation is still active. Controlling markers such
# as MERGE_HEAD and the rebase directories remain protected below.


def _git_operation_entries(worktree: Path) -> dict[str, str]:
    return scope_observation._git_operation_entries(worktree, effects=_scope_observation_effects())


def _path_signature(worktree: Path, relative_path: str) -> str:
    return scope_observation._path_signature(worktree, relative_path, effects=_scope_observation_effects())


def _capture_local_worktree_state(
    worktree: Path,
) -> tuple[str, str | None, dict[str, tuple[str, ...]], dict[str, tuple[tuple[str, ...], str]]]:
    return scope_observation._capture_local_worktree_state(worktree, effects=_scope_observation_effects())


def _worktree_state_signature(worktree: Path) -> str:
    return scope_observation._worktree_state_signature(worktree, effects=_scope_observation_effects())


def _ignore_rule_source(worktree: Path, path: str) -> str | None:
    return scope_observation._ignore_rule_source(worktree, path, effects=_scope_observation_effects())


def _normalize_legacy_ignored_snapshot(worktree: Path, saved: GitSnapshot, current: GitSnapshot) -> GitSnapshot:
    return scope_observation._normalize_legacy_ignored_snapshot(
        worktree,
        saved,
        current,
        effects=_scope_observation_effects(),
    )


def capture_git_snapshot(worktree: Path) -> GitSnapshot:
    return scope_observation.capture_git_snapshot(worktree, effects=_scope_observation_effects())


def agent_snapshot_changes(worktree: Path, before: GitSnapshot, after: GitSnapshot) -> list[AgentChange]:
    return scope_observation.agent_snapshot_changes(worktree, before, after, effects=_scope_observation_effects())


def _scope_snapshot_with_start_ignore_rules(worktree: Path, before: GitSnapshot, after: GitSnapshot) -> GitSnapshot:
    return scope_observation._scope_snapshot_with_start_ignore_rules(
        worktree,
        before,
        after,
        effects=_scope_observation_effects(),
    )


def _agent_git_resources(
    connection: sqlite3.Connection,
    repo_path: Path,
    worktree: Path,
    before: GitSnapshot,
    role: str,
    *,
    selected_plan_path: str | None = None,
) -> agent_scope_decision.GitResources | None:
    return scope_validation._agent_git_resources(
        connection,
        repo_path,
        worktree,
        before,
        role,
        selected_plan_path=selected_plan_path,
        effects=_scope_validation_effects(),
    )


def validate_agent_scope(
    role_name: str,
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    selected_plan_path: str | None = None,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
    allowed_ref_changes: frozenset[str] = frozenset(),
    allowed_worktree_changes: frozenset[str] = frozenset(),
    external_worktree: Path | None = None,
    base_branch: str | None = None,
    connection: sqlite3.Connection | None = None,
    resources: agent_scope_decision.GitResources | None = None,
    owned_rebase: bool = False,
) -> None:
    return scope_validation.validate_agent_scope(
        role_name,
        worktree,
        before,
        after,
        selected_plan_path=selected_plan_path,
        allowed_requirement_archives=allowed_requirement_archives,
        allowed_guidance_targets=allowed_guidance_targets,
        allow_common_plans=allow_common_plans,
        allowed_ref_changes=allowed_ref_changes,
        allowed_worktree_changes=allowed_worktree_changes,
        external_worktree=external_worktree,
        base_branch=base_branch,
        connection=connection,
        resources=resources,
        owned_rebase=owned_rebase,
        effects=_scope_validation_effects(),
    )


def _git_with_index(worktree: Path, index_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return scope_baseline._git_with_index(worktree, index_path, *arguments, effects=_scope_baseline_effects())


def _hash_git_blob(worktree: Path, contents: bytes) -> str:
    return scope_baseline._hash_git_blob(worktree, contents, effects=_scope_baseline_effects())


def _agent_worktree_tree(worktree: Path, snapshot: GitSnapshot, index_tree: str) -> str:
    return scope_baseline._agent_worktree_tree(worktree, snapshot, index_tree, effects=_scope_baseline_effects())


def _commit_tree(worktree: Path, tree: str, parent: str, message: str) -> str:
    return scope_baseline._commit_tree(worktree, tree, parent, message, effects=_scope_baseline_effects())


def _capture_agent_baseline(repo: str, worktree: Path, snapshot: GitSnapshot) -> str | None:
    return scope_baseline._capture_agent_baseline(repo, worktree, snapshot, effects=_scope_baseline_effects())


def begin_agent_scope(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    role_name: str,
    *,
    input_fingerprint: str | None = None,
    state_dir: Path | None = None,
) -> GitSnapshot:
    return scope_lifecycle.begin_agent_scope(
        connection,
        repo,
        worktree,
        role_name,
        input_fingerprint=input_fingerprint,
        state_dir=state_dir,
        effects=_scope_lifecycle_effects(),
    )


def clear_agent_scope(connection: sqlite3.Connection, repo: str) -> None:
    return scope_lifecycle.clear_agent_scope(connection, repo, effects=_scope_lifecycle_effects())


def validate_planning_workspace_identity(
    state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    expected = (
        state_dir / "worktrees" / project_id / "planning" / fingerprint
    )
    _validate_managed_path_boundary(
        state_dir,
        worktree,
        expected,
        "planning worktree",
    )


def validate_planning_worktree_ownership(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    """Require confirmed Manager ownership before removing a planning worktree."""

    project = connection.execute(
        "SELECT worktree, planning_workspace_owned FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if worktree.exists() and (
        project is None
        or int(project["planning_workspace_owned"]) != WORKSPACE_OWNED
        or Path(str(project["worktree"] or "")) != worktree
    ):
        raise WorkspaceOwnershipError(
            "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}"
        )

    if not fingerprint:
        raise WorkspaceOwnershipError(
            "planning worktree의 입력 fingerprint가 저장되지 않았습니다."
        )
    validate_planning_workspace_identity(
        state_dir,
        repo,
        fingerprint,
        worktree,
    )
    if worktree.exists():
        validate_managed_worktree(repo_path, worktree, None)


def changed_files(worktree: Path, since: str | None = None) -> list[str]:
    if since:
        output = git(worktree, "diff", "--name-only", f"{since}..HEAD").stdout
        return sorted(line for line in output.splitlines() if line)
    unstaged = git(worktree, "diff", "--name-only").stdout.splitlines()
    staged = git(worktree, "diff", "--cached", "--name-only").stdout.splitlines()
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted(set(unstaged + staged + untracked))


def branch_has_changes(worktree: Path, base: str) -> bool:
    result = git(
        worktree,
        "diff",
        "--quiet",
        f"origin/{base}...HEAD",
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            f"관리 branch 변경 확인에 실패했습니다 (git 종료 코드 {result.returncode})."
        )
    return result.returncode == 1


def assess_cleanup(worktree: Path, pr_head_oid: str | None) -> CleanupAssessment:
    """Classify a managed worktree without changing it or shared Git state."""
    if not worktree.exists():
        return CleanupAssessment("missing")
    try:
        status_lines = git(
            worktree, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout.splitlines()
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 상태 확인 실패: {error}")

    dirty_kinds: list[str] = []
    if any(line.startswith("??") for line in status_lines):
        dirty_kinds.append("untracked 파일")
    if any(len(line) >= 2 and line[0] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("staged 변경")
    if any(len(line) >= 2 and line[1] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("tracked 파일 수정")
    if dirty_kinds:
        return CleanupAssessment("dirty", ", ".join(dirty_kinds))
    try:
        _head, _branch, _index, dirty = _capture_local_worktree_state(worktree)
        operations = _git_operation_entries(worktree)
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 상태 확인 실패: {error}")
    if dirty or operations:
        return CleanupAssessment(
            "dirty", "index flag 또는 Git operation 변경: "
            + ", ".join(sorted((*dirty, *operations))),
        )

    if not pr_head_oid:
        return CleanupAssessment("unverifiable", "PR head commit 정보가 없습니다.")
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", pr_head_oid) is None:
        return CleanupAssessment("unverifiable", "PR head commit OID 형식이 유효하지 않습니다.")
    try:
        local_head = git(worktree, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        head_object = git(
            worktree, "cat-file", "-e", f"{pr_head_oid}^{{commit}}", check=False
        )
        if head_object.returncode:
            return CleanupAssessment(
                "unverifiable", f"PR head commit object를 찾을 수 없습니다: {pr_head_oid}"
            )
        relation = git(
            worktree,
            "merge-base",
            "--is-ancestor",
            local_head,
            pr_head_oid,
            check=False,
        )
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"commit 관계 확인 실패: {error}")
    if relation.returncode == 0:
        return CleanupAssessment("safe")
    if relation.returncode == 1:
        return CleanupAssessment(
            "local-only commits", "로컬 HEAD에 PR head에 포함되지 않은 commit이 있습니다."
        )
    return CleanupAssessment(
        "unverifiable", f"commit 관계 확인 실패 (git 종료 코드 {relation.returncode})"
    )


def validate_pending_cleanup_worktree(
    repo_path: Path,
    state_dir: Path,
    repo: str,
    worktree: Path,
) -> None:
    """Prove a pending cleanup path belongs to its deterministic ExecPlan."""

    if not worktree.exists():
        return
    branch_result = git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    if branch_result.returncode:
        raise WorkspaceOwnershipError(
            "보존된 cleanup worktree가 branch에 연결되어 있지 않습니다."
        )
    branch = branch_result.stdout.strip()
    match = re.fullmatch(r"loop-manager/(G-\d+)", branch)
    if match is None:
        raise WorkspaceOwnershipError(
            f"보존된 cleanup worktree branch의 소유권을 증명할 수 없습니다: {branch}"
        )
    plan_id = match.group(1)
    validate_execplan_workspace_identity(
        state_dir,
        repo,
        plan_id,
        branch,
        worktree,
    )
    validate_managed_worktree(repo_path, worktree, branch)


def remove_cleanup_worktree(
    repo: Path, worktree: Path, assessment: CleanupAssessment,
    *, state_dir: Path | None = None, owner: str | None = None,
) -> CleanupAssessment:
    if assessment.status != "safe":
        return assessment
    try:
        _head, _branch, _index, dirty = _capture_local_worktree_state(worktree)
        if dirty or _git_operation_entries(worktree):
            return CleanupAssessment("dirty", "worktree 제거 직전 권위 있는 변경이 있습니다.")
        arguments = ("--force",) if _ignored_worktree_paths(worktree) else ()
        if state_dir is None:
            git(repo, "worktree", "remove", *arguments, str(worktree))
        else:
            if not owner:
                raise WorkspaceOwnershipError("관리 worktree 정리 journal의 계획 identity가 없습니다.")
            before = _managed_worktree_transition_state(repo, worktree)
            head = before["registration"]["head"] if before["registration"] else None
            tree = git(repo, "rev-parse", "--verify", f"{head}^{{tree}}").stdout.strip()
            if (not before["exists"] or before["index"] != tree):
                raise WorkspaceOwnershipError("관리 worktree 정리 직전 index가 HEAD와 다릅니다.")
            expected = {"exists": False, "registration": None, "index": None}

            def marker(intent: Mapping[str, object]) -> bool:
                return _managed_worktree_marker(state_dir, intent, "remove")

            def apply(token: str) -> None:
                if _managed_worktree_transition_state(repo, worktree) != before:
                    raise WorkspaceOwnershipError("관리 worktree 정리 직전 상태가 변경됐습니다.")
                if before["registration"]["locked"]:
                    git(repo, "worktree", "unlock", str(worktree))
                git(repo, "worktree", "remove", *arguments, str(worktree))
                _write_managed_worktree_receipt(
                    state_dir, token, repo, worktree, before, expected, "remove",
                )

            manager_git_journal.execute(
                state_dir, owner=owner, kind="managed_worktree_remove",
                repo=str(_canonical_path(repo)),
                target=str(_canonical_path(worktree)), before=before,
                expected=expected,
                observe=lambda: _managed_worktree_transition_state(repo, worktree),
                apply=apply, marker=marker,
            )
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 제거 실패: {error}")
    return assessment


def _store_cleanup_pending(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
    reason: str,
    *,
    workspace_owned: int = WORKSPACE_OWNED,
) -> None:
    connection.execute(
        """
        INSERT INTO cleanup_pending (
            repo, worktree, pr_url, pr_head_oid, reason, workspace_owned
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, worktree) DO UPDATE SET
            pr_url = excluded.pr_url,
            pr_head_oid = excluded.pr_head_oid,
            reason = excluded.reason,
            workspace_owned = excluded.workspace_owned,
            updated_at = CURRENT_TIMESTAMP
        """,
        (repo, str(worktree), pr_url, pr_head_oid, reason, workspace_owned),
    )


def recheck_cleanup_pending(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
) -> list[sqlite3.Row]:
    _ensure_cleanup_pending_schema(connection)
    pending = connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()
    for item in pending:
        worktree = Path(item["worktree"])
        if int(item["workspace_owned"]) != WORKSPACE_OWNED:
            if not worktree.exists() and not worktree.is_symlink():
                connection.execute(
                    "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                    (repo, str(worktree)),
                )
            continue
        try:
            _recover_managed_worktree_removal(state_dir, repo_path, worktree)
            validate_pending_cleanup_worktree(
                repo_path,
                state_dir,
                repo,
                worktree,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            assessment = CleanupAssessment(
                "unverifiable",
                f"cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, item["pr_head_oid"])
            registration = (capture_git_snapshot(repo_path).worktrees.get(str(_canonical_path(worktree)))
                            if assessment.status == 'safe' else None)
            assessment = remove_cleanup_worktree(
                repo_path, worktree, assessment, state_dir=state_dir,
                owner=f"plan:{worktree.name}",
            )
            if (assessment.status == 'safe' and registration and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exec_plans'").fetchone()):
                plan = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND worktree = ? "
                    "AND status = 'MERGED' AND workspace_owned = ? AND pr_head_oid = ?",
                    (repo, str(worktree), WORKSPACE_OWNED, item['pr_head_oid'])).fetchone()
                if plan is not None:
                    _record_merged_worktree_cleanup(connection, repo_path, repo, plan['plan_id'],
                        worktree, plan['branch'], item['pr_head_oid'], assessment, registration)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            connection.execute(
                """
                UPDATE cleanup_pending
                SET reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND worktree = ?
                """,
                (assessment.reason or assessment.status, repo, str(worktree)),
            )
    connection.commit()
    return connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()


def planning_diagnostics_path(
    state_dir: Path,
    repo: str,
    fingerprint: str,
) -> Path:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return state_dir / "logs" / project_id / "planning" / f"{fingerprint}.txt"


def preserve_planning_diagnostics(
    state_dir: Path,
    repo: str,
    row: sqlite3.Row,
) -> Path:
    fingerprint = row["planning_attempt_fingerprint"]
    if not fingerprint:
        fingerprint = row["planning_error_fingerprint"] or "unknown"
    worktree = Path(row["worktree"]) if row["worktree"] else None
    error = (
        row["planning_error"]
        or row["planning_validation_error"]
        or row["last_error"]
        or ""
    )
    diff = ""
    if worktree and worktree.is_dir():
        try:
            base_revision = row["active_sha"] or "HEAD"
            diff = git(worktree, "diff", "--binary", base_revision).stdout
            for relative_path in _nul_git_paths(
                worktree, "ls-files", "--others", "-z"
            ):
                untracked = git(
                    worktree,
                    "diff",
                    "--binary",
                    "--no-index",
                    "--",
                    os.devnull,
                    relative_path,
                    check=False,
                )
                if untracked.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"untracked diff 생성 실패 ({relative_path}): "
                        f"git 종료 코드 {untracked.returncode}"
                    )
                diff += untracked.stdout
        except RuntimeError as diff_error:
            diff = f"diff 보존 실패: {diff_error}"
    diagnostic = planning_diagnostics_path(state_dir, repo, fingerprint)
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    diagnostic.write_text(
        "\n".join(
            [
                f"input_fingerprint: {fingerprint}",
                f"planning_worktree: {worktree or ''}",
                f"planner_session_id: {row['planner_session_id'] or ''}",
                f"validation_retries: {row['planning_retry_count']}",
                "",
                "last_error:",
                str(error),
                "",
                "diff:",
                diff,
            ]
        ),
        encoding="utf-8",
    )
    return diagnostic


def clear_planning_validation_state(
    connection: sqlite3.Connection,
    repo: str,
) -> None:
    update_project(
        connection,
        repo,
        planning_attempt_fingerprint=None,
        planning_retry_count=0,
        planner_session_id=None,
        worker_session_id=None,
        worker_session_binding=None,
        worker_session_transition=None,
        reviewer_session_id=None,
        worker_result=None,
        verify_result=None,
        review_result=None,
        planning_validation_error=None,
        planning_result_pending_validation=0,
        planning_result_completed=0,
        planning_result=None,
        planning_error=None,
        planning_error_fingerprint=None,
        last_error=None,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_baseline=None,
        agent_recovery=None,
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def _clear_active_values(
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> dict[str, object]:
    return runtime_state.clear_project_values(
        processed_sha=processed_sha,
        specification_fingerprint=specification_fingerprint,
        planning_fingerprint=planning_fingerprint,
    )


def _clear_active_in_current_transaction(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    try:
        runtime_state.RuntimeStateRepository(connection).clear(
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
            enqueue=False,
        )
    except runtime_state.RuntimeStateError as error:
        raise RuntimeError(
            "project planning 상태를 원자적으로 정리할 수 없습니다."
        ) from error


def clear_active(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    if connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is None:
        connection.commit()
        return
    runtime_state.RuntimeStateRepository(connection).clear(
        repo,
        processed_sha=processed_sha,
        specification_fingerprint=specification_fingerprint,
        planning_fingerprint=planning_fingerprint,
        enqueue=True,
    )
    connection.commit()


def _execplan_input_paths(plan: sqlite3.Row) -> list[str]:
    sources = json.loads(plan["sources"])
    return [str(plan["path"]), *(str(source) for source in sources)]


def _execplan_input_fingerprint(entries: list[tuple[str, str | None]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-blocked-input-v1\0")
    for relative_path, contents in entries:
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        if contents is None:
            digest.update(b"\0")
            continue
        normalized = contents.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        digest.update(b"\1")
        digest.update(len(normalized).to_bytes(8, "big"))
        digest.update(normalized)
    return digest.hexdigest()


def _worktree_execplan_input_fingerprint(worktree: Path, plan: sqlite3.Row) -> str:
    entries = []
    for relative_path in _execplan_input_paths(plan):
        path = worktree / relative_path
        contents = path.read_text(encoding="utf-8") if path.is_file() else None
        entries.append((relative_path, contents))
    return _execplan_input_fingerprint(entries)


def _revision_execplan_input_fingerprint(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    entries = []
    for relative_path in _execplan_input_paths(plan):
        shown = git(repo_path, "show", f"{revision}:{relative_path}", check=False)
        entries.append((relative_path, shown.stdout if shown.returncode == 0 else None))
    return _execplan_input_fingerprint(entries)


def _revision_execplan_source_fingerprint(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    """Fingerprint only current source documents; the plan body is classified separately."""

    entries = []
    for relative_path in json.loads(str(plan["sources"])):
        shown = git(
            repo_path,
            "show",
            f"{revision}:{relative_path}",
            check=False,
        )
        if shown.returncode and relative_path != "docs/PRODUCT.md":
            archive_path = execplan_catalog.requirement_archive_path(
                str(relative_path)
            )
            shown = git(
                repo_path,
                "show",
                f"{revision}:{archive_path}",
                check=False,
            )
        entries.append(
            (str(relative_path), shown.stdout if shown.returncode == 0 else None)
        )
    return _execplan_input_fingerprint(entries)


def _revision_execplan_body_hash(
    repo_path: Path,
    revision: str,
    path: str,
) -> str:
    shown = git(repo_path, "show", f"{revision}:{path}", check=False)
    if shown.returncode:
        raise RuntimeError(f"최신 ExecPlan 본문을 읽을 수 없습니다: {path}")
    return execution_revision.execplan_body_hash(shown.stdout)


def _revision_changed_paths(
    repo_path: Path,
    previous_revision: str,
    current_revision: str,
) -> tuple[str, ...]:
    if not previous_revision or previous_revision == current_revision:
        return ()
    changed = git(
        repo_path,
        "diff",
        "--name-only",
        "--find-renames",
        previous_revision,
        current_revision,
        "--",
        check=False,
    )
    if changed.returncode:
        raise RuntimeError("실행 base revision 차이를 확인할 수 없습니다.")
    return tuple(
        sorted(
            {
                line.strip()
                for line in changed.stdout.splitlines()
                if line.strip()
            }
        )
    )


def unmet_execplan_dependencies(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[str, ...]:
    """Return every direct or indirect dependency not confirmed MERGED."""

    rows = connection.execute(
        "SELECT plan_id, depends_on, status FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    graph = {
        str(row["plan_id"]): (
            tuple(str(item) for item in json.loads(str(row["depends_on"]))),
            str(row["status"]),
        )
        for row in rows
    }
    if plan_id not in graph:
        raise RuntimeError(f"dependency를 확인할 ExecPlan이 없습니다: {plan_id}")
    unmet: set[str] = set()
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current in visited:
            return
        visited.add(current)
        dependencies, _status = graph[current]
        for dependency in dependencies:
            stored = graph.get(dependency)
            if stored is None or stored[1] != "MERGED":
                unmet.add(dependency)
            if stored is not None:
                visit(dependency)

    visit(plan_id)
    return tuple(sorted(unmet))


def _revision_review_issue_fingerprint(
    repo_path: Path,
    revision: str,
    review_result: object,
) -> str | None:
    paths = _review_issue_paths(review_result)
    if not paths:
        return None
    entries = []
    for relative_path in paths:
        shown = git(repo_path, "show", f"{revision}:{relative_path}", check=False)
        entries.append(
            (relative_path, shown.stdout if shown.returncode == 0 else None)
        )
    return _execplan_input_fingerprint(entries)


def _blocked_validation_base_revision(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
) -> str:
    project = connection.execute(
        "SELECT active_sha FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    return str(
        (project["active_sha"] if project else None)
        or plan["active_sha"]
        or plan["current_revision_sha"]
        or plan["plan_sha"]
        or ""
    )


def _blocked_validation_input_fingerprints(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
) -> tuple[str, str]:
    """Capture comparable base and worktree inputs at the blocking boundary."""

    worktree_fingerprint = _worktree_execplan_input_fingerprint(worktree, plan)
    revision = _blocked_validation_base_revision(connection, repo, plan)
    if not revision:
        return worktree_fingerprint, worktree_fingerprint
    resolved = git(
        repo_path,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        check=False,
    )
    if resolved.returncode:
        return worktree_fingerprint, worktree_fingerprint
    return (
        _revision_execplan_input_fingerprint(repo_path, revision, plan),
        worktree_fingerprint,
    )


def _execplan_metadata_identity_error(
    repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
    subject: str,
) -> str | None:
    del repo_path
    if current["id"] != plan["plan_id"]:
        return f"{subject}의 ID가 reconciled catalog identity와 다릅니다."
    reconciled = _current_execplan_metadata(plan, current)
    metadata_fields = (
        "title",
        "sources",
        "depends_on",
        "complexity",
        "risk",
        "priority",
    )
    if any(current[field] != reconciled[field] for field in metadata_fields):
        return f"{subject} metadata가 reconciled catalog와 다릅니다."
    return None


def _current_execplan_metadata(
    plan: sqlite3.Row,
    fallback: Mapping[str, object],
) -> dict[str, object]:
    """Expose the reconciled declaration cache without publication-audit fields."""

    complexity = plan["current_complexity"] or fallback.get("complexity")
    risk = plan["current_risk"] or fallback.get("risk")
    return {
        "id": str(plan["plan_id"]),
        "title": str(plan["title"]),
        "sources": list(json.loads(str(plan["sources"]))),
        "depends_on": list(json.loads(str(plan["depends_on"]))),
        "complexity": str(complexity),
        "risk": str(risk),
        "priority": str(plan["priority"]),
    }


def _blocked_execplan_metadata_identity_error(
    repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
) -> str | None:
    return _execplan_metadata_identity_error(
        repo_path,
        current,
        plan,
        "BLOCKED ExecPlan",
    )


def validate_active_execplan_metadata(
    repo_path: Path,
    worktree: Path,
    relative_path: str,
    plan: sqlite3.Row,
    *,
    rebase_definition: execution_revision.DefinitionSnapshot | None = None,
) -> dict[str, object]:
    try:
        current = validate_execplan(worktree, relative_path)
    except RuntimeError as error:
        raise ExecPlanMetadataError(str(error)) from error
    comparison_plan = plan
    if rebase_definition is not None:
        # A paused rebase still contains its applied declaration. Scheduling,
        # profile and dependency fields may lag during conflict resolution;
        # all other identity fields and all normal validation remain strict.
        if rebase_definition.path != relative_path:
            raise ExecPlanMetadataError("rebase 기준 ExecPlan 경로가 현재 작업 경로와 다릅니다.")
        comparison_plan = dict(plan)
        for field, column in (
            ('priority', 'priority'),
            ('complexity', 'current_complexity'),
            ('risk', 'current_risk'),
        ):
            applied = getattr(rebase_definition, field)
            if current[field] == applied:
                comparison_plan[column] = applied
        if tuple(current['depends_on']) == rebase_definition.depends_on:
            comparison_plan['depends_on'] = json.dumps(rebase_definition.depends_on)
    error = _execplan_metadata_identity_error(
        repo_path,
        current,
        comparison_plan,
        "활성 ExecPlan",
    )
    if error is not None:
        raise ExecPlanMetadataError(error)
    if relative_path != str(plan["path"]):
        raise ExecPlanMetadataError("활성 ExecPlan 경로가 reconciled catalog와 다릅니다.")
    return _current_execplan_metadata(plan, current)


def validate_worker_workspace_scope(
    repo_path: Path,
    worktree: Path,
    branch: str,
    base_branch: str,
    selected_plan_path: str,
    catalog_plan: sqlite3.Row,
    before: GitSnapshot,
    *,
    allow_rebase: bool = False,
    rebase_definition: execution_revision.DefinitionSnapshot | None = None,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
    connection: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Validate one Worker-controlled operation before Manager Git writes."""

    if rebase_definition is not None and not allow_rebase:
        raise ExecPlanMetadataError("rebase 기준 선언은 충돌 복구 중에만 사용할 수 있습니다.")
    after = capture_git_snapshot(worktree)
    validate_agent_scope(
        "worker",
        worktree,
        before,
        after,
        selected_plan_path=selected_plan_path,
        allowed_requirement_archives=allowed_requirement_archives,
        allowed_guidance_targets=allowed_guidance_targets,
        allow_common_plans=allow_common_plans,
        external_worktree=repo_path,
        base_branch=base_branch,
        connection=connection,
    )
    plan = validate_active_execplan_metadata(
        repo_path,
        worktree,
        selected_plan_path,
        catalog_plan,
        **({'rebase_definition': rebase_definition} if allow_rebase else {}),
    )
    validate_managed_worktree(
        repo_path,
        worktree,
        branch,
        allow_rebase=allow_rebase,
    )
    return plan


def _guidance_document(worktree: Path, plan_path: str) -> GuidanceDocument | None:
    try:
        contents = (worktree / plan_path).read_text(encoding="utf-8")
        return parse_guidance_document(contents)
    except (OSError, GuidanceFormatError) as error:
        raise AgentScopeError(f"Worker guidance 제안 형식 위반: {error}") from error


def _git_common_directory(repository: Path) -> Path:
    value = git(
        repository,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ).stdout.strip()
    path = Path(value)
    return _canonical_path(path if path.is_absolute() else repository / path)


def is_loop_manager_repository(repository: Path) -> bool:
    """Identify the registered Loop Manager source across its managed worktrees."""

    try:
        return _git_common_directory(repository) == _git_common_directory(ROOT)
    except RuntimeError:
        return _canonical_path(repository) == _canonical_path(ROOT)


def _validate_guidance_proposal_scope(
    proposals: tuple[GuidanceProposal, ...],
    *,
    common_allowed: bool,
) -> None:
    for proposal in proposals:
        if common_allowed and proposal.scope == "project":
            raise AgentScopeError(
                "Loop Manager 루트 AGENTS.md는 공통 원본이므로 "
                "Scope common으로 명시적으로 승인해야 합니다."
            )
        if proposal.scope == "common" and not common_allowed:
            raise AgentScopeError(
                "공통 AGENTS.md guidance는 Loop Manager 자체 저장소의 "
                f"Worker만 제안할 수 있습니다: {proposal.proposal_id}"
            )


def _guidance_rows(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[sqlite3.Row, ...]:
    _ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM guidance_approvals "
            "WHERE repo = ? AND plan_id = ? ORDER BY batch_id, sequence, proposal_id",
            (repo, plan_id),
        ).fetchall()
    )


def _guidance_worker_targets(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT DISTINCT target_path FROM guidance_approvals "
        "WHERE repo = ? AND plan_id = ? AND applied = 0 "
        "AND decision != 'pending' ORDER BY target_path",
        (repo, plan_id),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _published_plan_allows_common_plans(
    repo_path: Path,
    catalog_plan: Mapping[str, object],
) -> bool:
    if not is_loop_manager_repository(repo_path):
        return False
    current_revision_sha = (
        catalog_plan["current_revision_sha"]
        if "current_revision_sha" in catalog_plan.keys()
        else None
    )
    plan_sha = str(
        current_revision_sha or catalog_plan["plan_sha"] or ""
    )
    if not plan_sha:
        return False
    shown = git(
        repo_path,
        "show",
        f"{plan_sha}:{catalog_plan['path']}",
        check=False,
    )
    if shown.returncode:
        return False
    acceptance = re.search(
        r"(?ms)^## Acceptance Criteria\s*\n+(.*?)(?=^## |\Z)",
        shown.stdout,
    )
    return bool(
        acceptance
        and re.search(
            r"(?im)^-\s+(?:공통\s+)?`docs/PLANS\.md`(?:에|를|을|의|와|과|\s)"
            r".*(?:변경|문서화|갱신|수정|설명|change|update|document|describe)",
            acceptance.group(1),
        )
    )


def _guidance_target_state(
    worktree: Path,
    target: str,
) -> tuple[bytes, int]:
    path = worktree / target
    try:
        target_stat = path.lstat()
    except FileNotFoundError as error:
        raise GuidanceFormatError(f"guidance 대상 파일이 없습니다: {target}") from error
    if path.is_symlink() or not path.is_file():
        raise GuidanceFormatError(
            f"guidance 대상은 일반 파일이어야 합니다: {target}"
        )
    # Git records executable permission, not local umask/group write bits.
    git_mode = 0o755 if target_stat.st_mode & stat.S_IXUSR else 0o644
    return path.read_bytes(), git_mode


def _guidance_block_message(
    plan: sqlite3.Row,
    proposals: tuple[GuidanceProposal, ...],
    detail: str,
) -> str:
    locations = []
    for proposal in proposals:
        locations.append(
            f"제안 {proposal.proposal_id}: {plan['path']}:{proposal.line}; "
            f"User Decision: {plan['path']}:{proposal.decision.line}"
        )
    return (
        f"AGENTS.md guidance 사용자 결정 대기 ({plan['plan_id']}).\n"
        f"ExecPlan: {plan['path']}\n{detail}\n" + "\n".join(locations)
    )


def _guidance_batch_id(
    repo: str,
    plan_id: str,
    document: GuidanceDocument,
    proposals: tuple[GuidanceProposal, ...],
) -> str:
    encoded = json.dumps(
        {
            "repo": repo,
            "plan_id": plan_id,
            "structure": document.structure_hash,
            "proposals": [proposal.identity for proposal in proposals],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _block_for_new_guidance(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    document: GuidanceDocument,
    new_proposals: tuple[GuidanceProposal, ...],
    worker_result: str,
) -> None:
    if not new_proposals:
        raise RuntimeError("차단할 새 guidance 제안이 없습니다.")
    _validate_guidance_proposal_scope(
        new_proposals,
        common_allowed=is_loop_manager_repository(repo_path),
    )
    for proposal in new_proposals:
        if proposal.decision.decision != "pending":
            raise AgentScopeError(
                "Worker가 새 guidance 제안의 User Decision을 미리 확정할 수 없습니다: "
                f"{proposal.proposal_id}"
            )

    batch_id = _guidance_batch_id(
        repo,
        str(plan["plan_id"]),
        document,
        new_proposals,
    )
    baselines: dict[str, tuple[bytes, int]] = {}
    for proposal in new_proposals:
        if proposal.target not in baselines:
            baselines[proposal.target] = _guidance_target_state(
                worktree,
                proposal.target,
            )
    for proposal in new_proposals:
        baseline, _baseline_mode = baselines[proposal.target]
        apply_patch_to_content(
            baseline,
            proposal.target,
            proposal.patch,
        )
    message = _guidance_block_message(
        plan,
        new_proposals,
        "각 User Decision의 Decision과 Revision을 사용자가 직접 확정해야 합니다.",
    )
    result_snapshot = capture_git_snapshot(worktree).serialize()
    with connection:
        for sequence, proposal in enumerate(new_proposals):
            baseline, baseline_mode = baselines[proposal.target]
            connection.execute(
                """
                INSERT INTO guidance_approvals (
                    repo, plan_id, proposal_id, proposal_hash, target_path,
                    scope, proposal_patch, proposal_patch_hash,
                    proposal_line, decision_line, batch_id, sequence,
                    baseline_content, baseline_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    plan["plan_id"],
                    proposal.proposal_id,
                    proposal.identity,
                    proposal.target,
                    proposal.scope,
                    proposal.patch,
                    guidance_patch_hash(proposal.target, proposal.patch),
                    proposal.line,
                    proposal.decision.line,
                    batch_id,
                    sequence,
                    baseline,
                    baseline_mode,
                ),
            )
        _final_block_recovery_decision(
            connection, repo, plan, "WORKING",
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, message,
            document.structure_hash, None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=GUIDANCE_BLOCK_REASON,
            evidence=message,
            release_fingerprint=document.structure_hash,
            plan_values={
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "guidance_plan_hash": document.structure_hash,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
            },
            project_values={
                "phase": "PLAN_BLOCKED",
                "last_error": message,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
            },
        )


def _guidance_document_matches_rows(
    document: GuidanceDocument,
    rows: tuple[sqlite3.Row, ...],
    *,
    require_decisions: bool,
) -> tuple[dict[str, GuidanceProposal], tuple[GuidanceProposal, ...]]:
    proposals = {proposal.proposal_id: proposal for proposal in document.proposals}
    stored = {str(row["proposal_id"]): row for row in rows}
    missing = sorted(set(stored) - set(proposals))
    if missing:
        raise AgentScopeError(
            "기존 guidance 제안은 삭제할 수 없습니다: " + ", ".join(missing)
        )
    for proposal_id, row in stored.items():
        proposal = proposals[proposal_id]
        if proposal.identity != str(row["proposal_hash"]):
            raise AgentScopeError(
                f"기존 guidance 제안은 수정할 수 없습니다: {proposal_id}"
            )
        decision_is_immutable = require_decisions or bool(row["applied"])
        if decision_is_immutable and row["decision_hash"]:
            if proposal.decision.identity != str(row["decision_hash"]):
                raise AgentScopeError(
                    "Worker는 확정된 guidance User Decision을 수정할 수 없습니다: "
                    f"{proposal_id}"
                )
    new = tuple(
        proposal
        for proposal in document.proposals
        if proposal.proposal_id not in stored
    )
    return proposals, new


def _guidance_final_expected(
    rows: tuple[sqlite3.Row, ...],
    proposals: Mapping[str, GuidanceProposal],
) -> tuple[
    dict[tuple[str, str], tuple[bytes, int]],
    dict[str, str],
]:
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row["batch_id"]), str(row["target_path"])),
            [],
        ).append(row)
    expected: dict[tuple[str, str], tuple[bytes, int]] = {}
    final_hashes: dict[str, str] = {}
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda row: int(row["sequence"]))
        contents = bytes(ordered[0]["baseline_content"])
        mode = int(ordered[0]["baseline_mode"])
        for row in ordered:
            proposal = proposals[str(row["proposal_id"])]
            patch = finalized_patch(proposal)
            if patch is not None:
                contents = apply_patch_to_content(
                    contents,
                    proposal.target,
                    patch,
                )
        expected[key] = (contents, mode)
        final_hash = guidance_content_hash(contents)
        for row in ordered:
            final_hashes[str(row["proposal_id"])] = final_hash
    return expected, final_hashes


def _plan_block_selected_for_release(
    connection: sqlite3.Connection,
    repo: str,
    *,
    blocked_reason: str | None = None,
    require_fingerprint: bool = False,
    plan_id: str | None = None,
) -> sqlite3.Row | None:
    """Return the mirrored block, or hydrate one uniquely matching block."""

    def matches(candidate: sqlite3.Row | None) -> bool:
        return bool(
            candidate is not None
            and candidate["status"] == "PLAN_BLOCKED"
            and (blocked_reason is None or candidate["blocked_reason"] == blocked_reason)
            and (not require_fingerprint or candidate["blocked_fingerprint"])
        )

    if plan_id is not None:
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        return plan if matches(plan) else None
    plan = project_execplan(connection, repo)
    if matches(plan):
        return plan
    clauses = ["repo = ?", "status = 'PLAN_BLOCKED'"]
    parameters: list[object] = [repo]
    if blocked_reason is not None:
        clauses.append("blocked_reason = ?")
        parameters.append(blocked_reason)
    if require_fingerprint:
        clauses.append("blocked_fingerprint IS NOT NULL")
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE " + " AND ".join(clauses) + " ORDER BY plan_id",
        parameters,
    ).fetchall()
    if len(rows) != 1:
        return None
    hydrate_execplan_project(connection, repo, rows[0])
    return rows[0]


def resume_guidance_approval_if_decided(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
    *, plan_id: str | None = None,
) -> bool:
    plan = _plan_block_selected_for_release(
        connection,
        repo,
        blocked_reason=GUIDANCE_BLOCK_REASON,
        plan_id=plan_id,
    )
    if plan is None:
        return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    resuming_rebase = _rebase_in_progress_for_branch(worktree, branch)
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
            allow_rebase=resuming_rebase,
        )
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, str(plan['plan_id']))
        accepted_rebase = bool((checkpoint.snapshot or {}).get('current_rebase_acceptance'))
        if accepted_rebase:
            _, decision = _current_rebase_service(
                connection, repo_path, state_dir, repo, str(plan['plan_id']),
                worktree, branch).require_owned(checkpoint=checkpoint)
            if checkpoint.operation is not None or decision.status.value != 'complete':
                return False
        validate_blocked_agent_scope(
            connection,
            repo_path,
            repo,
            base,
            worktree,
            str(plan["path"]),
            accepted_rebase=accepted_rebase, saved_plan=plan,
        )
        document = _guidance_document(worktree, str(plan["path"]))
        if document is None:
            raise AgentScopeError("저장된 guidance 제안이 ExecPlan에서 사라졌습니다.")
        if document.structure_hash != str(plan["guidance_plan_hash"] or ""):
            raise AgentScopeError(
                "User Decision 이외의 ExecPlan 변경으로 guidance 승인을 해제할 수 없습니다."
            )
        rows = _guidance_rows(connection, repo, str(plan["plan_id"]))
        proposals, new = _guidance_document_matches_rows(
            document,
            rows,
            require_decisions=False,
        )
        if new:
            raise AgentScopeError(
                "사용자 결정 대기 중 새 guidance 제안을 추가할 수 없습니다."
            )
        _validate_guidance_proposal_scope(
            document.proposals,
            common_allowed=is_loop_manager_repository(repo_path),
        )
        pending_rows = tuple(row for row in rows if not int(row["applied"]))
        for row in pending_rows:
            proposal = proposals[str(row["proposal_id"])]
            if proposal.decision.decision == "pending":
                return False
            invalidated = str(row["invalidated_decision_hash"] or "")
            if invalidated and proposal.decision.identity == invalidated:
                _update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    _guidance_block_message(
                        plan,
                        (proposal,),
                        "무효화된 결정과 다른 Revision 또는 대체 patch가 필요합니다.",
                    ),
                )
                return False
        _expected, final_hashes = _guidance_final_expected(
            pending_rows,
            proposals,
        )
        checked_targets: set[str] = set()
        for row in pending_rows:
            target = str(row["target_path"])
            if target in checked_targets:
                continue
            checked_targets.add(target)
            try:
                current_contents, current_mode = _guidance_target_state(worktree, target)
            except GuidanceFormatError:
                if row["invalidated_decision_hash"]:
                    continue
                raise
            if (
                not row["invalidated_decision_hash"]
                and (
                    current_contents != bytes(row["baseline_content"])
                    or current_mode != int(row["baseline_mode"])
                )
            ):
                raise AgentScopeError(
                    f"승인 전 guidance 대상이 변경됐습니다: {target}"
                )
        with connection:
            for row in pending_rows:
                proposal = proposals[str(row["proposal_id"])]
                patch = finalized_patch(proposal)
                connection.execute(
                    """
                    UPDATE guidance_approvals
                    SET decision = ?, decision_revision = ?,
                        decision_hash = ?, approved_patch = ?,
                        approved_patch_hash = ?, expected_content_hash = ?,
                        invalidated_decision_hash = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE repo = ? AND plan_id = ? AND proposal_id = ?
                    """,
                    (
                        proposal.decision.decision,
                        proposal.decision.revision,
                        proposal.decision.identity,
                        patch,
                        guidance_patch_hash(proposal.target, patch or ""),
                        final_hashes[proposal.proposal_id],
                        repo,
                        plan["plan_id"],
                        proposal.proposal_id,
                    ),
                )
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                str(plan["plan_id"]),
                plan_values={
                    "status": "IMPLEMENTING",
                    "blocked_reason": None,
                    "blocked_fingerprint": None,
                    "blocked_worktree_fingerprint": None,
                    "last_error": None,
                },
                project_values={
                    "phase": "WORKING",
                    "last_error": None,
                },
                enqueue=False,
            )
            scoped_blocking.clear_plan_block(
                connection, repo, str(plan["plan_id"])
            )
        if resuming_rebase:
            update_project(
                connection, repo, last_error=REBASE_IN_PROGRESS_ERROR,
                validated_base_sha=None,
            )
        clear_agent_scope(connection, repo)
        return True
    except (AgentScopeError, GuidanceFormatError, RuntimeError) as error:
        _update_blocked_error(
            connection,
            repo,
            str(plan["plan_id"]),
            f"guidance 승인 해제 실패:\n{error}",
        )
        return False


def _invalidate_guidance_approval(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    document: GuidanceDocument,
    proposals: tuple[GuidanceProposal, ...],
    worker_result: str,
    detail: str,
) -> None:
    message = _guidance_block_message(plan, proposals, detail)
    result_snapshot = capture_git_snapshot(worktree).serialize()
    proposal_ids = tuple(proposal.proposal_id for proposal in proposals)
    placeholders = ", ".join("?" for _ in proposal_ids)
    with connection:
        connection.execute(
            f"""
            UPDATE guidance_approvals
            SET invalidated_decision_hash = decision_hash,
                applied = 0, updated_at = CURRENT_TIMESTAMP
            WHERE repo = ? AND plan_id = ?
              AND proposal_id IN ({placeholders})
            """,
            (repo, plan["plan_id"], *proposal_ids),
        )
        # Manager rebase/verify may already have cleared the Agent scope.
        # Freeze the diagnosed state so only the User Decision can change.
        _final_block_recovery_decision(
            connection, repo, plan, "WORKING",
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, message,
            document.structure_hash, None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=GUIDANCE_BLOCK_REASON,
            evidence=message,
            release_fingerprint=document.structure_hash,
            plan_values={
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "guidance_plan_hash": document.structure_hash,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
                "agent_role": "worker",
                "agent_snapshot": result_snapshot,
                "agent_baseline": None,
                "agent_recovery": None,
                "agent_input_plan": None,
                "agent_result_plan": None,
                "agent_input_fingerprint": None,
            },
            clear_projection=True,
        )


def inspect_worker_guidance_result(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    worker_result: str,
) -> bool:
    """Validate approved targets, then block on any newly proposed guidance."""

    document = _guidance_document(worktree, str(plan["path"]))
    rows = _guidance_rows(connection, repo, str(plan["plan_id"]))
    if document is None:
        if rows:
            raise AgentScopeError("기존 guidance 제안이 ExecPlan에서 사라졌습니다.")
        return False
    _validate_guidance_proposal_scope(
        document.proposals,
        common_allowed=is_loop_manager_repository(repo_path),
    )
    proposals, new = _guidance_document_matches_rows(
        document,
        rows,
        require_decisions=True,
    )
    unapplied = tuple(row for row in rows if not int(row["applied"]))
    if unapplied:
        expected, _final_hashes = _guidance_final_expected(
            unapplied,
            proposals,
        )
        mismatched: list[GuidanceProposal] = []
        for (_batch_id, target), (expected_contents, expected_mode) in expected.items():
            try:
                actual_contents, actual_mode = _guidance_target_state(worktree, target)
                exact = actual_contents == expected_contents and actual_mode == expected_mode
            except GuidanceFormatError:
                exact = False
            if not exact:
                mismatched.extend(
                    proposals[str(row["proposal_id"])]
                    for row in unapplied
                    if str(row["target_path"]) == target
                )
        if mismatched:
            _invalidate_guidance_approval(
                connection,
                repo,
                worktree,
                plan,
                document,
                tuple(mismatched),
                worker_result,
                "실제 AGENTS.md 결과가 승인된 patch 결과와 다릅니다. "
                "User Decision Revision을 갱신해 다시 결정해야 합니다.",
            )
            return True
        with connection:
            connection.executemany(
                """
                UPDATE guidance_approvals
                SET applied = 1, updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND plan_id = ? AND proposal_id = ?
                """,
                (
                    (repo, plan["plan_id"], str(row["proposal_id"]))
                    for row in unapplied
                ),
            )
    if new:
        _block_for_new_guidance(
            connection,
            repo_path,
            repo,
            worktree,
            plan,
            document,
            new,
            worker_result,
        )
        return True
    runtime_state.RuntimeStateRepository(connection).update_plan(
        repo,
        str(plan["plan_id"]),
        plan_values={"guidance_plan_hash": document.structure_hash},
        enqueue=False,
    )
    return False


def validate_guidance_publication(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    worker_result: str,
    *,
    previous_base: str | None = None,
    latest_base: str | None = None,
) -> bool:
    """Revalidate the latest approval per target, including after Manager rebase."""

    rows = tuple(connection.execute(
        "SELECT * FROM guidance_approvals WHERE repo = ? AND plan_id = ? "
        "ORDER BY rowid",
        (repo, plan["plan_id"]),
    ).fetchall())
    if not rows:
        return False
    document = _guidance_document(worktree, str(plan["path"]))
    if document is None:
        raise AgentScopeError("게시할 ExecPlan에 guidance 결정이 없습니다.")
    proposals, new = _guidance_document_matches_rows(
        document, rows, require_decisions=True
    )
    if new:
        raise AgentScopeError("게시 전에 승인되지 않은 guidance 제안이 있습니다.")
    latest_batches = {str(row["target_path"]): str(row["batch_id"]) for row in rows}
    selected = tuple(
        row for row in rows
        if str(row["batch_id"]) == latest_batches[str(row["target_path"])]
    )
    mismatched = []
    new_baselines: dict[str, tuple[bytes, int]] = {}
    for target in latest_batches:
        target_rows = tuple(row for row in selected if row["target_path"] == target)
        last = target_rows[-1]
        try:
            contents, mode = _guidance_target_state(worktree, target)
            exact = (
                all(int(row["applied"]) for row in target_rows)
                and guidance_content_hash(contents) == last["expected_content_hash"]
                and mode == int(last["baseline_mode"])
            )
        except GuidanceFormatError:
            exact = False
        if previous_base and latest_base:
            old = git(worktree, "rev-parse", f"{previous_base}:{target}", check=False)
            new_blob = git(worktree, "rev-parse", f"{latest_base}:{target}", check=False)
            old_mode = git(worktree, "ls-tree", previous_base, "--", target).stdout
            new_mode = git(worktree, "ls-tree", latest_base, "--", target).stdout
            if old.returncode or new_blob.returncode or old_mode != new_mode:
                # Reapproval during a paused rebase already binds the decision
                # to this base. Do not invalidate it again after --continue.
                base_approved = False
                shown = subprocess.run(
                    ["git", "show", f"{latest_base}:{target}"],
                    cwd=worktree, check=False, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if shown.returncode == 0 and new_mode.startswith(("100644 ", "100755 ")):
                    new_baselines[target] = (
                        shown.stdout,
                        0o755 if new_mode.startswith("100755 ") else 0o644,
                    )
                    base_approved = all(
                        bytes(item["baseline_content"]) == shown.stdout
                        and int(item["baseline_mode"]) == new_baselines[target][1]
                        for item in target_rows
                    )
                exact = exact and base_approved
        if not exact:
            mismatched.extend(proposals[str(row["proposal_id"])] for row in target_rows)
    if not mismatched:
        return False
    _invalidate_guidance_approval(
        connection, repo, worktree, plan, document, tuple(mismatched),
        worker_result,
        "최신 base 또는 게시할 AGENTS.md가 승인 기준과 다릅니다. "
        "새 기준에 적용할 정확한 대체 patch와 User Decision Revision이 필요합니다.",
    )
    with connection:
        for target, (contents, mode) in new_baselines.items():
            connection.execute(
                "UPDATE guidance_approvals SET baseline_content = ?, baseline_mode = ? "
                "WHERE repo = ? AND plan_id = ? AND target_path = ? AND batch_id = ?",
                (contents, mode, repo, plan["plan_id"], target, latest_batches[target]),
            )
    return True


def guidance_resume_context(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> str:
    rows = tuple(
        row
        for row in _guidance_rows(connection, repo, plan_id)
        if not int(row["applied"])
    )
    if not rows:
        return ""
    payload = [
        {
            "proposal_id": str(row["proposal_id"]),
            "target": str(row["target_path"]),
            "scope": str(row["scope"]),
            "decision": str(row["decision"]),
            "revision": str(row["decision_revision"]),
            "patch": str(row["approved_patch"] or ""),
            "patch_hash": str(row["approved_patch_hash"] or ""),
            "expected_content_hash": str(row["expected_content_hash"] or ""),
        }
        for row in rows
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _blocked_execplan_metadata_error(
    repo_path: Path,
    worktree: Path,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    try:
        current = _parse_execplan(worktree, worktree / path)
    except (OSError, RuntimeError) as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"

    return _blocked_execplan_metadata_identity_error(repo_path, current, plan)


def _blocked_execplan_revision_metadata_error(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    shown = git(repo_path, "show", f"{revision}:{path}", check=False)
    if shown.returncode:
        return "BLOCKED ExecPlan을 remote 기준으로 다시 검증할 수 없습니다."
    try:
        current = published_execplan(path, shown.stdout)
    except RuntimeError as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"
    return _blocked_execplan_metadata_identity_error(repo_path, current, plan)


def _update_blocked_error(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    error: str,
    *, preserve_cause: bool = False,
) -> None:
    with connection:
        runtime = runtime_state.RuntimeStateRepository(connection)
        projected = runtime.projected_row(repo)
        if preserve_cause and (scoped_blocking.project_block(connection, repo) is not None
                or projected is not None and projected['plan_id'] != plan_id):
            runtime.update_plan(repo, plan_id, plan_values={"last_error": error}, enqueue=False)
        else:
            runtime.transition(
                repo, plan_id,
                plan_values={"last_error": error},
                project_values={"last_error": error},
                enqueue=False,
            )
        if not preserve_cause:
            connection.execute(
                "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND plan_id = ?",
                (error, repo, plan_id),
            )
        notifications.enqueue_current_project_events(connection, repo)


def block_active_execplan(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    error: str,
    verify_result: str,
    *,
    reason: BlockReasonCode = BlockReasonCode.PLAN_INPUT_INVALID,
    preserved_agent_result_snapshot: str | None = None,
    recovery_decision: recovery_runtime.RecoveryDecision | None = None,
    release_fingerprint: str | None = None,
    worker_result: str | None = None,
) -> None:
    project = connection.execute(
        "SELECT plan_path, phase, agent_role, agent_snapshot, agent_input_fingerprint, "
        "agent_input_plan "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    plan_id = _plan_id_from_path(str(project["plan_path"] or "")) if project else None
    plan = (
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if plan_id
        else None
    )
    if plan is None:
        plan = project_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    blocked_worktree_fingerprint = _worktree_execplan_input_fingerprint(
        worktree, plan
    )
    fingerprint = str(
        project["agent_input_fingerprint"] or blocked_worktree_fingerprint
    )
    block_fingerprint = release_fingerprint or fingerprint
    blocked_review_fingerprint = None
    if reason is BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK:
        revision = str(plan["active_sha"] or plan["current_revision_sha"] or "")
        block_fingerprint = _revision_execplan_input_fingerprint(
            Path(repo), revision, plan
        )
        blocked_review_fingerprint = _revision_review_issue_fingerprint(
            Path(repo), revision, error
        )
    agent_result_snapshot = preserved_agent_result_snapshot
    if agent_result_snapshot is None:
        try:
            agent_result_snapshot = capture_git_snapshot(worktree).serialize()
        except RuntimeError:
            pass
    agent_result_plan = None
    if (
        project["agent_role"] == "reviewer"
        and project["agent_input_plan"] is not None
    ):
        plan_file = worktree / str(plan["path"])
        if plan_file.is_file():
            agent_result_plan = plan_file.read_text(encoding="utf-8")
    plan_values = {
        "blocked_fingerprint": block_fingerprint,
        "blocked_worktree_fingerprint": blocked_worktree_fingerprint,
        "blocked_review_fingerprint": blocked_review_fingerprint,
        "last_error": error,
        "verify_result": verify_result,
        "agent_result_snapshot": agent_result_snapshot,
        "agent_result_plan": agent_result_plan,
        "agent_manager_snapshot": None,
    }
    if worker_result is not None:
        plan_values["worker_result"] = worker_result
    plan_id = str(plan["plan_id"])
    with runtime_state._transaction(connection):
        decision = _final_block_recovery_decision(
            connection, repo, plan, str(project["phase"] or "") if project else "",
            reason, error, fingerprint, recovery_decision,
        )
        if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                plan_id,
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=block_fingerprint,
                plan_values=plan_values,
                clear_projection=True,
            )
        else:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values={
                    "verify_result": verify_result,
                    "agent_result_snapshot": agent_result_snapshot,
                    "agent_result_plan": agent_result_plan,
                    "agent_manager_snapshot": None,
                },
                enqueue=False,
            )
            scoped_blocking.record_project_block(
                connection,
                repo,
                reason,
                error,
                fingerprint,
                plan_id=plan_id,
                previous_phase=str(project["phase"] or "") or None,
                recovery_decision=decision,
            )
            notifications.enqueue_current_project_events(connection, repo)


def block_verify_timeout_limit(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    worker_result: str,
    evidence: str,
    verify_result: str,
) -> str:
    observed = re.search(
        r"전체 verify 시간 초과: verify_timeout_seconds=([1-9][0-9]*)",
        verify_result,
    )
    if observed is None:
        raise RuntimeError("verify 시간 초과 상한 증거가 없습니다.")
    timeout = int(observed.group(1))
    detail = (
        f"전체 verify에 필요한 검증이 진행 중이었으나 현재 상한 "
        f"verify_timeout_seconds={timeout}초가 부족합니다. "
        f"[loop] verify_timeout_seconds를 늘리세요.\n"
        f"진행 근거: {redact_secrets(evidence)[-1800:]}"
    )
    block_active_execplan(
        connection, repo, worktree, detail, verify_result,
        reason=BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
        release_fingerprint=str(timeout), worker_result=worker_result,
    )
    return detail


def _final_block_recovery_decision(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    phase: str,
    reason: BlockReasonCode,
    error: str,
    fingerprint: str,
    decision: recovery_runtime.RecoveryDecision | None,
) -> recovery_runtime.RecoveryDecision:
    """Accept only a final typed decision in the blocking transaction."""

    if decision is None:
        observation = recovery_runtime.RecoveryObservation(
            reason=recovery_runtime.reason_from_block(reason),
            repo=repo, plan_id=str(plan["plan_id"]), phase=phase,
            plan_revision=str(plan["current_revision_sha"] or plan["plan_sha"] or ""),
            base_revision=str(plan["active_sha"] or ""),
            fingerprint=fingerprint,
            progress_marker=str(plan["active_sha"] or ""), detail=error,
            user_targets=recovery_state.block_user_targets(
                connection, repo, str(plan["plan_id"]), reason,
                str(plan["path"]),
            ),
        )
        decision = recovery_state.RecoveryRepository(connection).observe(
            observation, final_block_reason=reason,
        ).decision
    recovery_runtime.require_final(decision)
    if decision.block_reason != reason or decision.observation.repo != repo or decision.observation.plan_id != str(plan["plan_id"]):
        raise ValueError("recovery decision and block target disagree")
    return decision


def transition_validation_failure(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    stage: str,
    output: str,
) -> ValidationFailureDecision:
    """Persist stage-local failures and own their retry/BLOCKED transition."""

    columns = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }
    labels = {"verify": "전체 verify", "review": "Review"}
    history_column = columns.get(stage)
    if history_column is None:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("검증 실패를 기록할 활성 ExecPlan을 찾을 수 없습니다.")
    failures = validation_failure_fingerprints(stage, output, worktree)
    history = _deserialize_failure_history(plan[history_column])
    recorded_failures = []
    for fingerprint, normalized in failures:
        previous = history.get(fingerprint, {"occurrences": 0})
        occurrences = int(previous["occurrences"]) + 1
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": normalized[-2000:],
        }
        recorded_failures.append((fingerprint, normalized, occurrences))
    blocking_failure = next(
        (
            failure
            for failure in recorded_failures
            if stage == "verify" and failure[2] > MAX_VALIDATION_FINGERPRINT_REPEATS
        ),
        None,
    )
    fingerprint, normalized, occurrences = blocking_failure or max(
        recorded_failures,
        key=lambda failure: failure[2],
    )
    serialized_history = json.dumps(
        history,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    level_decision: WorkerLevelDecision | None = None
    serialized_level_history: str | None = None
    if stage == "review" and plan["effective_worker_level"] is not None:
        level_decision = evaluate_review_worker_level(
            plan["effective_worker_level"],
            _deserialize_review_level_history(plan["review_level_history"]),
            tuple(failure[0] for failure in recorded_failures),
        )
        serialized_level_history = json.dumps(
            level_decision.review_history,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    blocked = blocking_failure is not None
    if blocked:
        error = (
            f"{labels[stage]} 동일 실패가 Worker 수정 후 "
            f"{MAX_VALIDATION_FINGERPRINT_REPEATS}회 다시 발생했습니다.\n"
            f"실패 지문: {fingerprint}\n{output[-8000:]}"
        )
        blocked_input, blocked_worktree_input = (
            _blocked_validation_input_fingerprints(
                connection,
                Path(repo),
                repo,
                worktree,
                plan,
            )
        )
        blocked_review_input = None
        if stage == "review":
            blocked_revision = _blocked_validation_base_revision(
                connection, repo, plan
            )
            if blocked_revision:
                blocked_review_input = _revision_review_issue_fingerprint(
                    Path(repo), blocked_revision, output
                )
    else:
        prefix = "전체 검증 실패" if stage == "verify" else "Review 실패"
        error = f"{prefix}:\n{output[-8000:]}"
        blocked_input = None
        blocked_worktree_input = None
        blocked_review_input = None

    plan_values: dict[str, object] = {
        history_column: serialized_history,
        "validated_base_sha": None,
        "status": "PLAN_BLOCKED" if blocked else "IMPLEMENTING",
        "last_error": error,
    }
    project_values: dict[str, object] = {
        "validated_base_sha": None,
        "phase": "WORKING",
        "last_error": error,
    }
    if stage == "verify":
        plan_values["verify_result"] = output
        project_values["verify_result"] = output
    else:
        plan_values["review_result"] = output
        project_values["review_result"] = output
        if level_decision is not None:
            plan_values["effective_worker_level"] = level_decision.effective_level
            plan_values["review_level_history"] = serialized_level_history
    if blocked:
        plan_values.update(
            blocked_fingerprint=blocked_input,
            blocked_worktree_fingerprint=blocked_worktree_input,
            blocked_review_fingerprint=blocked_review_input,
        )
        try:
            plan_values["agent_result_snapshot"] = capture_git_snapshot(worktree).serialize()
        except (OSError, RuntimeError):
            # Missing evidence must fail closed in the checkpoint assessor.
            plan_values["agent_result_snapshot"] = None
        reason = (
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED
            if stage == "verify"
            else BlockReasonCode.REVIEW_RETRY_EXHAUSTED
        )
        with runtime_state._transaction(connection):
            recovery_decision = _final_block_recovery_decision(
                connection, repo, plan, stage.upper(), reason, error,
                fingerprint, None,
            )
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                str(plan["plan_id"]),
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=blocked_input,
                plan_values=plan_values,
                clear_projection=True,
            )
    else:
        runtime_state.RuntimeStateRepository(connection).transition(
            repo,
            str(plan["plan_id"]),
            plan_values=plan_values,
            project_values=project_values,
            enqueue=False,
        )
    return ValidationFailureDecision(
        stage,
        fingerprint,
        occurrences,
        blocked,
        recovery_decision if blocked else None,
    )


def block_active_execplan_from_revision(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    error: str,
    verify_result: str = "",
    *,
    reason: BlockReasonCode = BlockReasonCode.PLAN_INPUT_INVALID,
    preserved_agent_result_snapshot: str | None = None,
    blocked_reason: str | None = None,
    recovery_decision: recovery_runtime.RecoveryDecision | None = None,
) -> None:
    """Block an active plan when its saved worktree is not safe to read."""

    plan = active_execplan(connection, repo) or project_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    project = connection.execute(
        "SELECT phase, agent_input_fingerprint FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    fingerprint = str(
        (project["agent_input_fingerprint"] if project else None)
        or _revision_execplan_input_fingerprint(repo_path, revision, plan)
    )
    plan_values: dict[str, object] = {
        "blocked_fingerprint": fingerprint,
        "blocked_worktree_fingerprint": None,
        "blocked_review_fingerprint": None,
        "last_error": error,
        "verify_result": verify_result,
        "agent_result_snapshot": preserved_agent_result_snapshot,
        "agent_manager_snapshot": None,
    }
    if blocked_reason is not None:
        plan_values["blocked_reason"] = blocked_reason
    plan_id = str(plan["plan_id"])
    with runtime_state._transaction(connection):
        decision = _final_block_recovery_decision(
            connection, repo, plan,
            str(project["phase"] or "") if project else "", reason, error,
            fingerprint, recovery_decision,
        )
        if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                plan_id,
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=fingerprint,
                plan_values=plan_values,
                clear_projection=True,
            )
        else:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values={
                    "verify_result": verify_result,
                    "agent_result_snapshot": preserved_agent_result_snapshot,
                    "agent_manager_snapshot": None,
                },
                enqueue=False,
            )
            scoped_blocking.record_project_block(
                connection,
                repo,
                reason,
                error,
                fingerprint,
                plan_id=plan_id,
                previous_phase=(
                    str(project["phase"] or "") or None if project else None
                ),
                recovery_decision=decision,
            )
            notifications.enqueue_current_project_events(connection, repo)

def block_persisted_agent_scope_violation(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    row: sqlite3.Row,
    detail: str,
    result_snapshot: GitSnapshot | None = None,
    reason: BlockReasonCode = BlockReasonCode.REPOSITORY_STATE_UNSAFE,
) -> str:
    """Persist a pre-resume scope violation without discarding Agent state."""

    message = f"Agent 범위 선검증 실패:\n{detail}"
    if result_snapshot is not None:
        preserved_snapshot = result_snapshot.serialize()
    elif row["agent_result_snapshot"]:
        preserved_snapshot = str(row["agent_result_snapshot"])
    else:
        preserved_snapshot = str(row["agent_snapshot"])
    plan = active_execplan(connection, repo) or project_execplan(connection, repo)
    if plan is None:
        if (
            row["phase"] == "PLANNING"
            and scoped_blocking.reason_scope(reason) is BlockScope.PLAN
        ):
            update_project(
                connection,
                repo,
                planning_validation_error=detail,
                last_error=message,
                agent_result_snapshot=preserved_snapshot,
            )
        else:
            if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
                reason = BlockReasonCode.REPOSITORY_STATE_UNSAFE
            with connection:
                scoped_blocking.record_project_block(
                    connection,
                    repo,
                    reason,
                    message,
                    None,
                    plan_id=None,
                    previous_phase=str(row["phase"] or "") or None,
                )
                runtime_state.RuntimeStateRepository(connection).update_project(
                    repo,
                    {"agent_result_snapshot": preserved_snapshot},
                    enqueue=False,
                )
                notifications.enqueue_current_project_events(connection, repo)
        return message

    saved_worktree = Path(str(row["worktree"] or ""))
    if saved_worktree.is_dir():
        block_active_execplan(
            connection,
            repo,
            saved_worktree,
            message,
            str(row["verify_result"] or ""),
            reason=reason,
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    else:
        revision = str(
            plan["current_revision_sha"]
            or plan["plan_sha"]
            or row["active_sha"]
            or f"refs/remotes/origin/{row['base_branch']}"
        )
        block_active_execplan_from_revision(
            connection,
            repo_path,
            repo,
            revision,
            message,
            str(row["verify_result"] or ""),
            reason=reason,
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    return message


def recover_persisted_agent_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
) -> str:
    """Recover a missing Agent worktree only from its proven owned branch."""

    worktree = Path(str(project["worktree"] or ""))
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    plan = active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    if (
        project["phase"] == "PLANNING"
        or plan_id is None
        or plan is None
        or plan["plan_id"] != plan_id
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or not branch
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "저장된 Agent worktree의 Loop Manager 소유권을 증명할 수 없습니다: "
            f"{worktree}"
        )
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            plan_id,
            branch,
            worktree,
        )
        return make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan_id}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"저장된 Agent worktree 복구 실패: {error}"
        ) from error


def _proven_recovered_worktree_baseline(
    before: GitSnapshot,
    current: GitSnapshot,
    state_dir: Path,
    repo_path: Path,
    plan_id: str,
    worktree: Path,
) -> GitSnapshot:
    return scope_recovery._proven_recovered_worktree_baseline(
        before,
        current,
        state_dir,
        repo_path,
        plan_id,
        worktree,
        effects=_scope_recovery_effects(),
    )


def validate_persisted_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    allow_planner_worktree_correction: bool = False,
    manager_cleanup_worktrees: frozenset[str] = frozenset(),
    runtime_mutation: RuntimeMutation | None = None,
) -> GitSnapshot | None:
    return scope_recovery.validate_persisted_agent_scope(
        connection,
        repo_path,
        state_dir,
        repo,
        allow_planner_worktree_correction=allow_planner_worktree_correction,
        manager_cleanup_worktrees=manager_cleanup_worktrees,
        runtime_mutation=runtime_mutation,
        effects=_scope_recovery_effects(),
    )


def refresh_persisted_agent_scope_after_manager_git(
    connection: sqlite3.Connection,
    repo: str,
    validated: GitSnapshot,
    manager_worktree_changes: frozenset[str],
    *,
    state_dir: Path | None = None,
) -> GitSnapshot:
    return scope_recovery.refresh_persisted_agent_scope_after_manager_git(
        connection,
        repo,
        validated,
        manager_worktree_changes,
        state_dir=state_dir,
        effects=_scope_recovery_effects(),
    )


def _remote_adoption_agent_baseline(
    connection,
    repo,
    plan_id,
    worktree,
    project,
    before,
    current,
    checkpoint,
    *,
    saved_plan = None,
    accepted_rebase = False,
):
    return scope_recovery._remote_adoption_agent_baseline(
        connection,
        repo,
        plan_id,
        worktree,
        project,
        before,
        current,
        checkpoint,
        saved_plan=saved_plan,
        accepted_rebase=accepted_rebase,
        effects=_scope_recovery_effects(),
    )


def validate_blocked_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    worktree: Path,
    selected_plan_path: str,
    *,
    accepted_rebase: bool = False,
    saved_plan = None,
    require_safe_result: bool = False,
) -> None:
    return scope_recovery.validate_blocked_agent_scope(
        connection,
        repo_path,
        repo,
        base,
        worktree,
        selected_plan_path,
        accepted_rebase=accepted_rebase,
        saved_plan=saved_plan,
        require_safe_result=require_safe_result,
        effects=_scope_recovery_effects(),
    )


def _reconcile_blocked_reviewer_plan(
    relative_path: str,
    before_text: str,
    result_text: str,
    current_text: str,
) -> str:
    """Remove Reviewer edits while retaining separable user edits."""

    if before_text == result_text:
        return current_text

    before_lines = before_text.splitlines(keepends=True)
    result_lines = result_text.splitlines(keepends=True)
    current_lines = current_text.splitlines(keepends=True)
    user_equal_blocks = [
        (result_start, result_end, current_start, current_end)
        for tag, result_start, result_end, current_start, current_end in (
            difflib.SequenceMatcher(
                None,
                result_lines,
                current_lines,
                autojunk=False,
            ).get_opcodes()
        )
        if tag == "equal"
    ]
    reverse_edits: list[tuple[int, int, list[str]]] = []
    agent_edits = difflib.SequenceMatcher(
        None,
        result_lines,
        before_lines,
        autojunk=False,
    ).get_opcodes()
    for tag, result_start, result_end, before_start, before_end in agent_edits:
        if tag == "equal":
            continue
        if result_start == result_end:
            positions = {
                current_start + result_start - equal_start
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start <= equal_end
            }
            if len(positions) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start = current_end = positions.pop()
        else:
            ranges = {
                (
                    current_start + result_start - equal_start,
                    current_start + result_end - equal_start,
                )
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start and result_end <= equal_end
            }
            if len(ranges) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start, current_end = ranges.pop()
        reverse_edits.append(
            (current_start, current_end, before_lines[before_start:before_end])
        )

    for current_start, current_end, replacement in reversed(reverse_edits):
        current_lines[current_start:current_end] = replacement
    return "".join(current_lines)


def _deserialize_blocked_agent_recovery(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent 복구 상태가 유효하지 않습니다: {error}"
        ) from error
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or parsed.get("state") not in {"prepared", "stashed", "isolated"}
        or not isinstance(parsed.get("source_snapshot"), str)
        or not isinstance(parsed.get("original_head"), str)
        or not isinstance(parsed.get("violation_head"), str)
        or not isinstance(parsed.get("branch"), str)
        or not isinstance(parsed.get("stash_marker"), str)
        or not isinstance(parsed.get("violation_ref"), str)
        or (
            parsed.get("plan_temp_path") is not None
            and not isinstance(parsed.get("plan_temp_path"), str)
        )
        or (
            parsed.get("plan_mode") is not None
            and not isinstance(parsed.get("plan_mode"), int)
        )
        or (
            parsed.get("baseline_snapshot") is not None
            and not isinstance(parsed.get("baseline_snapshot"), str)
        )
        or (
            parsed.get("plan_path") is not None
            and not isinstance(parsed.get("plan_path"), str)
        )
        or (
            parsed.get("plan_contents") is not None
            and not isinstance(parsed.get("plan_contents"), str)
        )
    ):
        raise AgentScopeError("저장된 BLOCKED Agent 복구 상태 형식을 지원하지 않습니다.")
    return parsed


def _restore_agent_index_flags(worktree: Path, entries: Mapping[str, tuple[str, ...]]) -> None:
    return scope_baseline._restore_agent_index_flags(worktree, entries, effects=_scope_baseline_effects())


def _restore_agent_staged_entries(worktree: Path, before: GitSnapshot) -> None:
    return scope_baseline._restore_agent_staged_entries(worktree, before, effects=_scope_baseline_effects())


def _restore_agent_baseline(worktree: Path, before: GitSnapshot, serialized: str | None) -> None:
    return scope_baseline._restore_agent_baseline(worktree, before, serialized, effects=_scope_baseline_effects())


def _worktree_relative_target(worktree: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AgentScopeError(f"복구 경로가 worktree 밖을 가리킵니다: {relative_path}")
    target = worktree / candidate
    try:
        target.parent.resolve().relative_to(worktree.resolve())
    except ValueError as error:
        raise AgentScopeError(
            f"복구 경로의 상위 디렉터리가 worktree 밖을 가리킵니다: {relative_path}"
        ) from error
    return target


def _remove_interrupted_recovery_temp(
    worktree: Path,
    relative_path: str | None,
) -> None:
    if relative_path is None:
        return
    target = _worktree_relative_target(worktree, relative_path)
    if target.is_dir() and not target.is_symlink():
        raise AgentScopeError(f"복구 임시 경로가 디렉터리입니다: {relative_path}")
    if target.exists() or target.is_symlink():
        target.unlink()


def _atomic_restore_file(
    worktree: Path,
    relative_path: str,
    temp_relative_path: str,
    contents: str,
    mode: int,
) -> None:
    target = _worktree_relative_target(worktree, relative_path)
    temporary = _worktree_relative_target(worktree, temp_relative_path)
    _remove_interrupted_recovery_temp(worktree, temp_relative_path)
    try:
        with temporary.open("xb") as handle:
            handle.write(contents.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 원자적 복원 실패: {error}"
        ) from error


def _serialize_blocked_agent_recovery(recovery: Mapping[str, object]) -> str:
    return json.dumps(
        recovery,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _blocked_recovery_stash_oid(worktree: Path, marker: str) -> str | None:
    listed = git(
        worktree,
        "stash",
        "list",
        "--format=%H%x00%gs",
        check=False,
    )
    if listed.returncode:
        raise AgentScopeError(
            f"BLOCKED Agent stash 확인 실패:\n{listed.stdout[-8000:]}"
        )
    for line in listed.stdout.splitlines():
        oid, separator, subject = line.partition("\0")
        if separator and subject.endswith(marker):
            return oid
    return None


def _restore_blocked_agent_recovery_plan(
    worktree: Path,
    plan_path: str | None,
    plan_contents: str | None,
    plan_temp_path: str | None,
    plan_mode: int | None,
) -> None:
    if plan_path is None:
        return
    target = _worktree_relative_target(worktree, plan_path)
    try:
        if plan_contents is None:
            if target.exists() or target.is_symlink():
                target.unlink()
        else:
            if plan_temp_path is None or plan_mode is None:
                raise AgentScopeError(
                    "BLOCKED ExecPlan 원자적 복원 정보가 없습니다."
                )
            _atomic_restore_file(
                worktree,
                plan_path,
                plan_temp_path,
                plan_contents,
                plan_mode,
            )
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 복원 실패: {error}"
        ) from error


def _recovered_agent_local_state_matches(
    worktree: Path,
    before: GitSnapshot,
    current: GitSnapshot,
    plan_path: str | None,
    plan_contents: str | None,
    plan_mode: int | None,
) -> bool:
    if (
        current.head != before.head
        or current.branch != before.branch
        or current.index != before.index
        or current.operations != before.operations
    ):
        return False
    current_dirty = dict(current.dirty)
    before_dirty = dict(before.dirty)
    if plan_path is None:
        return current_dirty == before_dirty
    current_dirty.pop(plan_path, None)
    before_dirty.pop(plan_path, None)
    if current_dirty != before_dirty:
        return False
    target = _worktree_relative_target(worktree, plan_path)
    if plan_contents is None:
        return not target.exists() and not target.is_symlink()
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(target_stat.st_mode)
        and stat.S_IMODE(target_stat.st_mode) == plan_mode
        and target.read_text(encoding="utf-8") == plan_contents
    )


def _mark_blocked_agent_isolated(
    connection: sqlite3.Connection,
    repo: str,
    recovery: dict[str, object],
    before: GitSnapshot,
) -> None:
    recovery["state"] = "isolated"
    recovery["baseline_snapshot"] = before.serialize()
    update_project(
        connection,
        repo,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_recovery=_serialize_blocked_agent_recovery(recovery),
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def _complete_blocked_agent_recovery(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
) -> None:
    project = connection.execute(
        "SELECT agent_snapshot, agent_baseline, agent_recovery "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return
    if not project["agent_snapshot"]:
        raise AgentScopeError("BLOCKED Agent 복구에 시작 snapshot이 없습니다.")

    recovery = _deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        source = GitSnapshot.deserialize(str(recovery["source_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 snapshot이 유효하지 않습니다: {error}"
        ) from error

    current_config = _config_entries(worktree)
    if current_config != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    marker = str(recovery["stash_marker"])
    stash_oid = _blocked_recovery_stash_oid(worktree, marker)
    if recovery["state"] == "prepared":
        if stash_oid is None:
            current = capture_git_snapshot(worktree)
            if current.serialize() != source.serialize():
                raise AgentScopeError(
                    "BLOCKED Agent 복구 준비 후 worktree 상태가 추가로 변경됐습니다."
                )
            stashed = git(
                worktree,
                "stash",
                "push",
                "--all",
                "-m",
                marker,
                check=False,
            )
            if stashed.returncode:
                raise AgentScopeError(
                    f"BLOCKED Agent 변경 보존 실패:\n{stashed.stdout[-8000:]}"
                )
            stash_oid = _blocked_recovery_stash_oid(worktree, marker)
        recovery["state"] = "stashed"
        recovery["stash_oid"] = stash_oid
        update_project(
            connection,
            repo,
            agent_recovery=_serialize_blocked_agent_recovery(recovery),
        )

    branch = str(recovery["branch"])
    original_head = str(recovery["original_head"])
    violation_head = str(recovery["violation_head"])
    plan_path_value = recovery.get("plan_path")
    plan_path = str(plan_path_value) if plan_path_value is not None else None
    plan_contents_value = recovery.get("plan_contents")
    plan_contents = (
        str(plan_contents_value) if plan_contents_value is not None else None
    )
    plan_temp_value = recovery.get("plan_temp_path")
    plan_temp_path = (
        str(plan_temp_value) if plan_temp_value is not None else None
    )
    plan_mode_value = recovery.get("plan_mode")
    plan_mode = int(plan_mode_value) if plan_mode_value is not None else None
    _remove_interrupted_recovery_temp(worktree, plan_temp_path)
    current = capture_git_snapshot(worktree)
    if current.branch != branch:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree branch가 저장된 branch와 다릅니다."
        )
    if current.operations:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree에 Git operation이 남아 있습니다."
        )
    if before.operations:
        raise AgentScopeError(
            "Agent 실행 전 Git operation이 있는 worktree는 자동 복구할 수 없습니다."
        )

    if _recovered_agent_local_state_matches(
        worktree,
        before,
        current,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        _mark_blocked_agent_isolated(connection, repo, recovery, before)
        return

    if current.head == violation_head and violation_head != original_head:
        if current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent stash 후 worktree에 새 변경이 생겼습니다."
            )
        violation_ref = str(recovery["violation_ref"])
        existing_ref = git(
            worktree,
            "rev-parse",
            "--verify",
            violation_ref,
            check=False,
        )
        if existing_ref.returncode == 0:
            if existing_ref.stdout.strip() != violation_head:
                raise AgentScopeError(
                    f"BLOCKED Agent 진단 ref 충돌: {violation_ref}"
                )
        else:
            git(worktree, "update-ref", violation_ref, violation_head)
        git(worktree, "reset", "--hard", original_head)
        current = capture_git_snapshot(worktree)
    elif current.head != original_head:
        raise AgentScopeError(
            "BLOCKED Agent 복구 HEAD가 저장된 Agent 결과 또는 시작 commit과 다릅니다."
        )

    if not _same_local_snapshot(before, current):
        if current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent 복구 중 예상하지 못한 새 worktree 변경이 생겼습니다."
            )
        _restore_agent_baseline(
            worktree,
            before,
            (
                str(project["agent_baseline"])
                if project["agent_baseline"] is not None
                else None
            ),
        )

    _restore_blocked_agent_recovery_plan(
        worktree,
        plan_path,
        plan_contents,
        plan_temp_path,
        plan_mode,
    )

    restored = capture_git_snapshot(worktree)
    if not _recovered_agent_local_state_matches(
        worktree,
        before,
        restored,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        raise AgentScopeError("BLOCKED Agent 복구 후 저장된 변경 상태와 일치하지 않습니다.")
    _mark_blocked_agent_isolated(connection, repo, recovery, before)


def preserve_blocked_agent_changes(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan_id: str,
    *,
    preserved_input_path: str | None = None,
) -> GitSnapshot | None:
    """Persist and isolate a diagnosed scope violation crash-safely."""

    project = connection.execute(
        "SELECT agent_role, agent_snapshot, agent_result_snapshot, "
        "agent_recovery, agent_input_plan, agent_result_plan "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        raise AgentScopeError("보존할 BLOCKED Agent 프로젝트가 없습니다.")
    if project["agent_recovery"]:
        recovery = _deserialize_blocked_agent_recovery(
            str(project["agent_recovery"])
        )
        if recovery["state"] == "isolated":
            baseline_snapshot = recovery.get("baseline_snapshot")
            if not isinstance(baseline_snapshot, str):
                raise AgentScopeError("격리된 Agent 시작 snapshot이 없습니다.")
            try:
                return GitSnapshot.deserialize(baseline_snapshot)
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                raise AgentScopeError(
                    f"격리된 Agent 시작 snapshot이 유효하지 않습니다: {error}"
                ) from error
        if not project["agent_snapshot"]:
            raise AgentScopeError("복구 중인 BLOCKED Agent 시작 snapshot이 없습니다.")
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        _complete_blocked_agent_recovery(connection, repo, worktree)
        return before
    if not project["agent_snapshot"] or not project["agent_result_snapshot"]:
        return None
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"보존할 BLOCKED Agent snapshot이 유효하지 않습니다: {error}"
        ) from error
    if before.operations:
        raise AgentScopeError(
            "Agent 실행 전 Git operation이 있는 worktree는 자동 격리할 수 없습니다."
        )
    current = capture_git_snapshot(worktree)
    if current.branch != before.branch or current.head not in {
        before.head,
        result.head,
    }:
        raise AgentScopeError(
            "BLOCKED 이후 HEAD 또는 branch가 추가로 변경되어 자동 보존할 수 없습니다."
        )
    allowed_dirty_paths = (
        frozenset((preserved_input_path,))
        if preserved_input_path is not None
        else frozenset()
    )
    _blocked_agent_local_baseline(
        before,
        result,
        current,
        worktree,
        allowed_dirty_paths=allowed_dirty_paths,
    )
    preserved_paths = frozenset(
        path
        for path in allowed_dirty_paths
        if current.dirty.get(path)
        not in (before.dirty.get(path), result.dirty.get(path))
    )
    plan_path: str | None = None
    plan_contents: str | None = None
    plan_mode: int | None = None
    if preserved_paths:
        if len(preserved_paths) != 1:
            raise AgentScopeError(
                "사용자가 수정한 BLOCKED 입력 경로를 하나로 결정할 수 없습니다."
            )
        plan_path = next(iter(preserved_paths))
        plan_file = worktree / plan_path
        if plan_file.is_symlink() or (plan_file.exists() and not plan_file.is_file()):
            raise AgentScopeError(
                f"사용자가 수정한 BLOCKED ExecPlan 형식이 안전하지 않습니다: {plan_path}"
            )
        plan_contents = (
            plan_file.read_text(encoding="utf-8")
            if plan_file.is_file()
            else None
        )
        plan_mode = (
            stat.S_IMODE(plan_file.lstat().st_mode)
            if plan_file.is_file()
            else None
        )
        if project["agent_role"] == "reviewer":
            before_text = project["agent_input_plan"]
            result_text = project["agent_result_plan"]
            agent_changed_path = (
                before.dirty.get(plan_path) != result.dirty.get(plan_path)
            )
            if before_text is None or result_text is None:
                if agent_changed_path:
                    raise AgentScopeError(
                        "BLOCKED ExecPlan의 Reviewer 변경을 분리할 "
                        f"본문 기준이 없습니다: {plan_path}"
                    )
            elif str(before_text) != str(result_text):
                if plan_contents is None:
                    raise AgentScopeError(
                        f"사용자가 수정한 BLOCKED ExecPlan을 읽을 수 없습니다: {plan_path}"
                    )
                plan_contents = _reconcile_blocked_reviewer_plan(
                    plan_path,
                    str(before_text),
                    str(result_text),
                    plan_contents,
                )

    source_serialized = current.serialize()
    recovery_id = hashlib.sha256(
        json.dumps(
            {
                "repo": repo,
                "plan_id": plan_id,
                "source": source_serialized,
                "plan_path": plan_path,
                "plan_contents": plan_contents,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    plan_temp_path: str | None = None
    if plan_path is not None and plan_contents is not None:
        plan_relative = Path(plan_path)
        plan_temp_path = str(
            plan_relative.parent
            / f".{plan_relative.name}.loop-manager-recovery-{recovery_id}.tmp"
        )
    recovery: dict[str, object] = {
        "version": 1,
        "state": "prepared",
        "source_snapshot": source_serialized,
        "original_head": before.head,
        "violation_head": result.head,
        "branch": str(before.branch or ""),
        "stash_marker": f"loop-manager scope violation {plan_id} {recovery_id}",
        "violation_ref": (
            f"refs/loop-manager/violations/{project_id}/{plan_id}/{recovery_id}"
        ),
        "plan_path": plan_path,
        "plan_contents": plan_contents,
        "plan_temp_path": plan_temp_path,
        "plan_mode": plan_mode,
        "stash_oid": None,
    }
    update_project(
        connection,
        repo,
        agent_recovery=_serialize_blocked_agent_recovery(recovery),
    )
    _complete_blocked_agent_recovery(connection, repo, worktree)
    return before


def resume_interrupted_blocked_agent_recovery(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    """Finish a persisted violation isolation before normal repository work."""

    project = connection.execute(
        "SELECT phase, base_branch, branch, worktree, agent_snapshot, "
        "agent_recovery FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return False
    recovery = _deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    if project["phase"] != "PLAN_BLOCKED":
        raise AgentScopeError(
            "BLOCKED Agent 복구 상태가 활성 BLOCKED 작업과 일치하지 않습니다."
        )
    serialized_before = project["agent_snapshot"]
    if recovery["state"] == "isolated":
        serialized_before = recovery.get("baseline_snapshot")
    if not isinstance(serialized_before, str):
        raise AgentScopeError("BLOCKED Agent 복구 시작 snapshot이 없습니다.")
    try:
        before = GitSnapshot.deserialize(serialized_before)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 시작 snapshot이 유효하지 않습니다: {error}"
        ) from error

    worktree = Path(str(project["worktree"] or ""))
    branch = str(project["branch"] or "")
    config_context = worktree if worktree.is_dir() else repo_path
    if _config_entries(config_context) != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    plan = project_execplan(connection, repo)
    if (
        plan is None
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "BLOCKED Agent 복구 workspace의 Loop Manager 소유권을 증명할 수 없습니다."
        )
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
            allow_rebase=(
                recovery["state"] == "isolated"
                and _rebase_in_progress_for_branch(worktree, branch)
            ),
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 workspace 검증 실패: {error}"
        ) from error

    if recovery["state"] != "isolated":
        _complete_blocked_agent_recovery(connection, repo, worktree)
    return True


def _requeue_blocked_execplan(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    *, publication_remote_oid: str | None = None,
) -> None:
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is not None and int(plan["pr_conflict_recovery"]):
        with connection:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values={
                    "status": "IMPLEMENTING",
                    "blocked_fingerprint": None,
                    "blocked_worktree_fingerprint": None,
                    "blocked_review_fingerprint": None,
                    "retry_count": 0,
                    "blocked_reason": None,
                    "guidance_plan_hash": None,
                    "validated_base_sha": None,
                    "verify_result": None,
                    "review_result": None,
                    "last_error": REBASE_IN_PROGRESS_ERROR,
                    "verify_failure_history": "{}",
                    "review_failure_history": "{}",
                    "effective_worker_level": plan["initial_worker_level"],
                    "review_level_history": "{}",
                    "worker_session_transition": None,
                    "worker_session_binding": (
                        plan["worker_session_binding"]
                        if plan["worker_session_id"] is not None else None
                    ),
                },
                project_values={
                    "phase": "WORKING",
                    "validated_base_sha": None,
                    "verify_result": None,
                    "review_result": None,
                    "last_error": REBASE_IN_PROGRESS_ERROR,
                    "agent_role": None,
                    "agent_snapshot": None,
                    "agent_input_fingerprint": None,
                    "agent_baseline": None,
                    "agent_recovery": None,
                    "agent_input_plan": None,
                    "agent_result_plan": None,
                    "agent_result_snapshot": None,
                    "agent_manager_snapshot": None,
                    "worker_session_id": plan["worker_session_id"],
                    "worker_session_transition": None,
                    "worker_session_binding": (
                        plan["worker_session_binding"]
                        if plan["worker_session_id"] is not None else None
                    ),
                },
                enqueue=False,
            )
            scoped_blocking.clear_plan_block(connection, repo, plan_id)
        return
    with connection:
        runtime_state.RuntimeStateRepository(connection).transition(
            repo,
            plan_id,
            plan_values={
                "status": "READY",
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None,
                "retry_count": 0,
                "blocked_reason": None,
                "guidance_plan_hash": None,
                "last_error": None,
                "worker_session_id": None,
                "worker_session_binding": None,
                "worker_session_transition": None,
                "reviewer_session_id": None,
                "worker_result": None,
                "verify_result": None,
                "review_result": None,
                "validated_base_sha": None,
                "publication_remote_oid": publication_remote_oid,
                "pr_url": None,
                "pr_head_oid": None,
                "verify_failure_history": "{}",
                "review_failure_history": "{}",
                "effective_worker_level": plan["initial_worker_level"],
                "review_level_history": "{}",
            },
            project_values=_clear_active_values(),
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)


def _record_blocked_resume_head(
    connection: sqlite3.Connection, repo: str, plan_id: str,
    owner_oid: str, before_head: str, target_base_oid: str,
    after_head: str | None, before_snapshot: str | None = None,
) -> None:
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    active_rebase = current.operation == 'rebase'
    intent = current.rebase_publication_intent()
    result = (current.snapshot or {}).get('rebase_publication_result') or {}
    published_oid = (intent['head'] if intent is not None
                     and result.get('intent_id') == intent['id']
                     and result.get('head') == after_head == intent['head']
                     else owner_oid)
    marker = {
        'owner_oid': owner_oid, 'before_head': before_head,
        'target_base_oid': target_base_oid, 'after_head': after_head,
        'before_snapshot': (before_snapshot or
            (current.snapshot or {}).get('blocked_resume', {}).get('before_snapshot')),
    }
    checkpoint.record(
        repo, plan_id, expected_generation=current.generation,
        operation='rebase' if active_rebase else None,
        expected_head=current.expected_head if active_rebase else after_head or before_head,
        target_base_oid=target_base_oid,
        observed_remote_oid=published_oid,
        snapshot={**(current.snapshot or {}), 'blocked_resume': marker},
    )


def _blocked_resume_worktree_matches(
    worktree: Path, marker: dict, current: GitSnapshot, *, restore_staged: bool = True,
) -> bool:
    """Match preserved files and staging, allowing only autostash unstaging."""
    serialized = marker.get('before_snapshot')
    if not serialized:
        return False
    before = GitSnapshot.deserialize(serialized)
    if before.head != marker.get('before_head') or before.branch != current.branch:
        return False
    if before.config != current.config or not current.shared_metadata_complete:
        return False
    # Rebase may change blobs, but cannot legitimately introduce index flags.
    for path, entries in current.index.items():
        tags = tuple(entry.split()[0] for entry in entries)
        old_tags = tuple(entry.split()[0] for entry in before.index.get(path, ()))
        if tags != (old_tags if path in before.index else ('H',)):
            return False
    if set(before.dirty) != set(current.dirty):
        return False
    unstaged_by_rebase = False
    for path, (kinds, signature) in before.dirty.items():
        now_kinds, now_signature = current.dirty[path]
        if signature != now_signature:
            return False
        if kinds == now_kinds:
            if 'staged' in kinds and before.index.get(path) != current.index.get(path):
                return False
        elif (not marker.get('after_head') and 'staged' in kinds
              and now_kinds == ('unstaged',)):
            unstaged_by_rebase = True
        else:
            return False
    if unstaged_by_rebase:
        # Only a clean index can be attributed to rebase --autostash.
        if git(worktree, 'diff', '--cached', '--quiet', check=False).returncode:
            return False
        if not restore_staged:
            return True
        _restore_agent_staged_entries(worktree, before)
        restored = capture_git_snapshot(worktree)
        return restored.dirty == before.dirty and all(
            restored.index.get(path) == before.index.get(path)
            for path, (kinds, _) in before.dirty.items() if 'staged' in kinds
        )
    return True


def _resume_interrupted_blocked_rebase(
    connection: sqlite3.Connection, repo_path: Path,
    repo: str, base: str, plan: sqlite3.Row, worktree: Path, branch: str,
) -> bool | None:
    return scope_reconciliation._resume_interrupted_blocked_rebase(
        _scope_reconciliation_effects_resume_interrupted_blocked_rebase(),
        connection, repo_path, repo, base, plan, worktree, branch,
    )


def resume_blocked_execplan_if_inputs_changed(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
    *, plan_id: str | None = None,
) -> bool:
    scope_blocks = [scoped_blocking.plan_block(connection, repo, block['plan_id'])
                    for block in scoped_blocking.plan_blocks(connection, repo)
                    if scoped_blocking.is_scope_baseline_block(block)
                    and (plan_id is None or block['plan_id'] == plan_id)]
    for scope_block in scope_blocks:
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, scope_block['plan_id'])
        recovery = recovery_state.RecoveryRepository(connection).row(repo, scope_block['plan_id'])
        if rebase_block_reconciliation.block_matches(scope_block, recovery, checkpoint):
            return _reconcile_rebase_evidence_block(
                connection, repo_path, state_dir, repo, scope_block, plan_block=True)
        if _reconcile_scope_baseline_plan_block(connection, repo_path, state_dir, repo, scope_block):
            return True
    # Select rebase causes without hydrating a projection or requiring a past
    # input fingerprint. Current Git state, rather than input change, decides.
    rebase_blocks = [scoped_blocking.plan_block(connection, repo, str(block['plan_id']))
                     for block in scoped_blocking.plan_blocks(connection, repo)
                     if rebase_block_reconciliation.could_be_rebase_evidence_block(block)
                     and (plan_id is None or block['plan_id'] == plan_id)]
    if rebase_blocks:
        runtime = runtime_state.RuntimeStateRepository(connection)
        if not runtime.recovery_slot_available(repo) or runtime.active_row(repo) is not None:
            return False
        projected = project_execplan(connection, repo)
        selected = next((block for block in rebase_blocks
                         if projected is not None and block['plan_id'] == projected['plan_id']), None)
        if selected is None and projected is None and len(rebase_blocks) == 1:
            selected = rebase_blocks[0]
        if selected is not None:
            return _reconcile_rebase_evidence_block(
                connection, repo_path, state_dir, repo, selected, plan_block=True)
        if projected is None:
            return False
    if plan_id is not None:
        runtime = runtime_state.RuntimeStateRepository(connection)
        if (not runtime.recovery_slot_available(repo) or runtime.active_row(repo) is not None
                or scoped_blocking.project_block(connection, repo) is not None):
            return False
    plan = _plan_block_selected_for_release(
        connection,
        repo,
        require_fingerprint=True,
        plan_id=plan_id,
    )
    if plan is None:
        return False
    if plan_id is not None:
        hydrate_execplan_project(connection, repo, plan)
    stored = str(plan["blocked_fingerprint"])
    worktree = Path(str(plan["worktree"]))
    branch = str(plan["branch"] or "")
    revision = f"refs/remotes/origin/{base}"
    execplan_input_changed = (
        _revision_execplan_input_fingerprint(repo_path, revision, plan) != stored
    )
    review_input_changed = False
    if plan["blocked_reason"] != BlockReasonCode.USER_DECISION_REQUIRED.value:
        current_review_fingerprint = _revision_review_issue_fingerprint(
            repo_path, revision, plan["review_result"]
        )
        stored_review_fingerprint = plan["blocked_review_fingerprint"]
        if current_review_fingerprint is None:
            review_input_changed = False
        elif stored_review_fingerprint:
            review_input_changed = current_review_fingerprint != str(
                stored_review_fingerprint
            )
        else:
            blocked_revision = str(
                plan["active_sha"]
                or plan["current_revision_sha"]
                or plan["plan_sha"]
                or ""
            )
            blocked_review_fingerprint = (
                _revision_review_issue_fingerprint(
                    repo_path, blocked_revision, plan["review_result"]
                )
                if blocked_revision
                else None
            )
            review_input_changed = (
                blocked_review_fingerprint is not None
                and current_review_fingerprint != blocked_review_fingerprint
            )
    remote_input_changed = execplan_input_changed or review_input_changed
    if int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        if not remote_input_changed:
            return False
        try:
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                str(plan["plan_id"]),
                branch,
                worktree,
            )
        except WorkspaceOwnershipError as error:
            message = f"BLOCKED workspace 충돌 해제 검증 실패:\n{error}"
            _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        branch_state = git(
            repo_path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        conflicts = []
        if worktree.exists() or worktree.is_symlink():
            conflicts.append(f"path {worktree}")
        if branch_state.returncode == 0:
            conflicts.append(f"branch {branch}")
        elif branch_state.returncode != 1:
            conflicts.append(f"branch 상태 확인 실패 {branch}")
        if conflicts:
            message = (
                "BLOCKED workspace 충돌이 해소되지 않았습니다: "
                + ", ".join(conflicts)
            )
            _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        metadata_error = _blocked_execplan_revision_metadata_error(
            repo_path,
            revision,
            plan,
        )
        if metadata_error:
            _update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False
        _requeue_blocked_execplan(connection, repo, str(plan["plan_id"]))
        return True

    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except (RuntimeError, WorkspaceOwnershipError) as error:
        message = f"BLOCKED worktree 복구 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    interrupted = _resume_interrupted_blocked_rebase(
        connection, repo_path, repo, base, plan, worktree, branch,
    )
    if interrupted is not None:
        return interrupted
    try:
        validate_blocked_agent_scope(
            connection,
            repo_path,
            repo,
            base,
            worktree,
            str(plan["path"]),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 범위 재검증 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    current_worktree_fingerprint = (
        _worktree_execplan_input_fingerprint(worktree, plan)
        if worktree.is_dir()
        else None
    )
    blocked_worktree_fingerprint = plan["blocked_worktree_fingerprint"]
    worktree_changed = bool(
        current_worktree_fingerprint != stored
        and current_worktree_fingerprint != blocked_worktree_fingerprint
    )
    if not worktree_changed and not remote_input_changed:
        return False
    owned_oid = None
    checkpoint_row = connection.execute(
        "SELECT phase FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan['plan_id']),
    ).fetchone()
    if checkpoint_row is not None and checkpoint_row['phase'] == 'published':
        remote_oid = remote_branch_oid(repo_path, branch)
        owned_oid = checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, str(plan['plan_id']), branch,
            worktree, remote_oid, git,
            before_head=git(worktree, 'rev-parse', 'HEAD').stdout.strip(),
            recorded_oid=str(plan['publication_remote_oid'] or '') or None,
        ) if not plan['pr_url'] else None
        if owned_oid is None:
            _update_blocked_error(
                connection, repo, str(plan['plan_id']),
                '게시 checkpoint의 원격 OID와 계획 소유권을 증명할 수 없습니다.',
            )
            return False
    try:
        preserved_before = preserve_blocked_agent_changes(
            connection,
            repo,
            worktree,
            str(plan["plan_id"]),
            preserved_input_path=(str(plan["path"]) if worktree_changed else None),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 변경 보존 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    target_base_oid = git(repo_path, 'rev-parse', revision).stdout.strip()
    if owned_oid is not None:
        before_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        if git(repo_path, 'merge-base', '--is-ancestor', owned_oid,
               before_head, check=False).returncode != 0:
            _update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '게시 checkpoint 이후 로컬 HEAD 계보가 다릅니다.')
            return False
        _record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base_oid, None,
            capture_git_snapshot(worktree).serialize(),
        )
    base_ancestry = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        revision,
        "HEAD",
        check=False,
    )
    if base_ancestry.returncode not in (0, 1):
        error = (
            "BLOCKED 해제 전 원격 base ancestry 확인 실패:\n"
            f"{base_ancestry.stdout[-8000:]}"
        )
        _update_blocked_error(connection, repo, str(plan["plan_id"]), error)
        return False
    if base_ancestry.returncode == 1:
        rebased = _journaled_direct_rebase(
            connection, repo_path, repo, str(plan['plan_id']), worktree,
            branch, target_base_oid, autostash=True,
            agent_session_id=plan['worker_session_id'],
        )
        if rebased.returncode:
            error = f"BLOCKED 해제 전 rebase 실패:\n{rebased.stdout[-8000:]}"
            _update_blocked_error(connection, repo, str(plan["plan_id"]), error)
            return False
    if worktree.is_dir():
        metadata_error = _blocked_execplan_metadata_error(repo_path, worktree, plan)
        if metadata_error:
            _update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False

    if owned_oid is not None:
        after_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        if not _direct_rebase_history_proven(
            connection, repo_path, repo, str(plan['plan_id']), worktree,
            branch, before_head, after_head, target_base_oid,
            plan['worker_session_id'],
        ):
            _update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '차단 해제 rebase의 commit 계보를 증명할 수 없습니다.')
            return False
        pending = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, str(plan['plan_id']),
        )
        if not _blocked_resume_worktree_matches(
            worktree, pending.snapshot['blocked_resume'], capture_git_snapshot(worktree),
            restore_staged=False,
        ):
            _update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '차단 해제 rebase 이후 보존한 작업 상태가 다릅니다.')
            return False
        # Adopt the clean rebase result before restoring the original staging.
        # Restored work remains unverified and belongs to the next Worker turn.
        preserved_observation = _owned_rebase_journal_state(worktree, branch)
        if base_ancestry.returncode == 1:
            try:
                _complete_direct_rebase_checkpoint(
                    connection, repo, str(plan['plan_id']), worktree,
                    target_base_oid, repo_path=repo_path, branch=branch,
                    agent_session_id=plan['worker_session_id'],
                    preserved_worktree=True,
                    preserved_marker=pending.snapshot['blocked_resume'],
                    preserved_observation=preserved_observation,
                )
            except recovery_runtime.RecoveryFailure as error:
                _update_blocked_error(connection, repo, str(plan['plan_id']),
                                      f'차단 해제 rebase 게시를 완료할 수 없습니다: {error}')
                return False
    elif base_ancestry.returncode == 1:
        _complete_direct_rebase_checkpoint(
            connection, repo, str(plan['plan_id']), worktree, target_base_oid,
            repo_path=repo_path, branch=branch,
            agent_session_id=plan['worker_session_id'],
        )

    if base_ancestry.returncode == 1 and preserved_before is not None:
        try:
            _restore_agent_staged_entries(worktree, preserved_before)
        except (AgentScopeError, RuntimeError) as error:
            restore_error = f"BLOCKED 해제 후 index 복원 실패:\n{error}"
            _update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                restore_error,
            )
            return False

    if owned_oid is not None:
        _record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base_oid, after_head,
        )

    clear_agent_scope(connection, repo)
    _requeue_blocked_execplan(connection, repo, str(plan["plan_id"]),
                              publication_remote_oid=runtime_state.RuntimeStateRepository(connection).get(
                                  repo, str(plan['plan_id'])).publication_remote_oid or owned_oid)
    return True


def _record_merged_worktree_cleanup(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    worktree: Path,
    branch: str,
    pr_head_oid: str | None,
    assessment: CleanupAssessment,
    registration: tuple[str, ...] | None,
) -> None:
    """Save only a completed removal of a validated Merge workspace."""
    if assessment.status != "safe" or not registration or not pr_head_oid:
        return
    key = str(_canonical_path(worktree))
    if (
        f"branch=refs/heads/{branch}" not in registration
        or capture_git_snapshot(repo_path).worktrees.get(key) is not None
        or worktree.exists() or worktree.is_symlink()
    ):
        raise WorkspaceOwnershipError("Merge cleanup 뒤 worktree 제거를 확인할 수 없습니다.")
    _ensure_cleanup_pending_schema(connection)
    connection.execute(
        "INSERT OR REPLACE INTO merged_worktree_cleanups "
        "(repo, plan_id, worktree, branch, registration, pr_head_oid) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (repo, plan_id, key, branch, json.dumps(registration), pr_head_oid),
    )


def _proven_merged_cleanup_paths(
    connection: sqlite3.Connection,
    repo: str,
    before: GitSnapshot,
    current: GitSnapshot,
) -> frozenset[str]:
    """Allow only the exact registration removed by a recorded Merge cleanup."""
    _ensure_cleanup_pending_schema(connection)
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exec_plans'"
    ).fetchone() is None:
        return frozenset()
    proven: set[str] = set()
    for row in connection.execute(
        "SELECT c.*, p.status, p.branch AS plan_branch, p.worktree AS plan_worktree, "
        "p.workspace_owned, p.pr_head_oid AS plan_pr_head_oid "
        "FROM merged_worktree_cleanups c JOIN exec_plans p "
        "ON p.repo = c.repo AND p.plan_id = c.plan_id WHERE c.repo = ?",
        (repo,),
    ):
        path = str(row["worktree"])
        try:
            registration = tuple(json.loads(str(row["registration"])))
        except (TypeError, ValueError):
            continue
        if (
            row["status"] == "MERGED"
            and int(row["workspace_owned"] or 0) == WORKSPACE_OWNED
            and str(row["plan_branch"] or "") == row["branch"]
            and str(_canonical_path(Path(str(row["plan_worktree"] or "")))) == path
            and row["pr_head_oid"] == row["plan_pr_head_oid"]
            and before.worktrees.get(path) == registration
            and current.worktrees.get(path) is None
            and not Path(path).exists()
            and not Path(path).is_symlink()
        ):
            proven.add(path)
    return frozenset(proven)


def _record_manager_base_fetch_intent(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    base: str,
    before: GitSnapshot,
) -> int:
    """Persist the exact base ref we intend to fetch before changing Git refs."""
    refname = f"refs/remotes/origin/{base}"
    remote = git(
        repo_path, "ls-remote", "--heads", "origin", f"refs/heads/{base}",
        check=False,
    )
    rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if remote.returncode or len(rows) != 1 or rows[0][1:] != [f"refs/heads/{base}"]:
        raise RuntimeError("Manager base fetch의 원격 OID를 확정할 수 없습니다.")
    target = rows[0][0]
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", target) is None:
        raise RuntimeError("Manager base fetch 원격 OID가 유효하지 않습니다.")
    _ensure_cleanup_pending_schema(connection)
    existing = connection.execute(
        "SELECT id, before_ref, target_oid, action_token FROM manager_base_fetches "
        "WHERE repo = ? AND plan_id = ? AND refname = ? AND phase = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (repo, plan_id, refname),
    ).fetchone()
    before_ref = json.dumps(before.refs.get(refname))
    if existing is not None:
        if existing["before_ref"] == before_ref and existing["target_oid"] == target:
            if not existing["action_token"]:
                raise RuntimeError("이전 Manager base fetch intent의 소유 표식이 없습니다.")
            return int(existing["id"])
        try:
            saved = json.loads(str(existing["before_ref"]))
            cursor = tuple(saved) if saved is not None else None
            successors = connection.execute(
                "SELECT * FROM manager_base_fetches WHERE repo = ? AND plan_id = ? "
                "AND refname = ? AND id >= ? ORDER BY id",
                (repo, plan_id, refname, int(existing["id"])),
            ).fetchall()
            for prior in successors:
                previous = json.loads(str(prior["before_ref"]))
                previous = tuple(previous) if previous is not None else None
                if previous != cursor or (
                    prior["phase"] != "complete"
                    and not _manager_base_fetch_applied(repo_path, prior, successors)
                ):
                    raise RuntimeError("이전 Manager base fetch 체인을 증명할 수 없습니다.")
                cursor = (str(prior["target_oid"]), "")
        except (TypeError, ValueError) as error:
            raise RuntimeError("이전 Manager base fetch intent가 유효하지 않습니다.") from error
        if cursor != before.refs.get(refname):
            raise RuntimeError("이전 Manager base fetch intent를 안전하게 재개할 수 없습니다.")
    with connection:
        cursor = connection.execute(
            "INSERT INTO manager_base_fetches "
            "(repo, plan_id, refname, before_ref, target_oid, action_token, phase) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (repo, plan_id, refname, before_ref,
             target, secrets.token_hex(16)),
        )
    return int(cursor.lastrowid)


def _manager_base_fetch_applied(
    repo_path: Path, row: sqlite3.Row,
    successors: list[sqlite3.Row] | None = None,
) -> bool:
    """Require this action at the tip or an uninterrupted owned reflog suffix."""
    token = str(row["action_token"] or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return False
    reflog = git(
        repo_path, "reflog", "show", "--format=%H%x00%gs",
        str(row["refname"]), check=False,
    )
    if reflog.returncode:
        return False
    lines = reflog.stdout.splitlines()
    action = f"{row['target_oid']}\x00loop-manager-base-fetch:{token}"
    if lines.count(action) != 1:
        return False
    position = lines.index(action)
    cursor = str(row["target_oid"])
    remaining = [item for item in (successors or [])
                 if item["refname"] == row["refname"] and item["id"] > row["id"]]
    for line in reversed(lines[:position]):
        matched = None
        for candidate in remaining:
            try:
                previous = json.loads(str(candidate["before_ref"]))
            except (TypeError, ValueError):
                return False
            if (previous == [cursor, ""] and line ==
                    f"{candidate['target_oid']}\x00loop-manager-base-fetch:{candidate['action_token']}"):
                matched = candidate
                break
        if matched is None:
            return False
        cursor = str(matched["target_oid"])
        remaining = [item for item in remaining if item["id"] > matched["id"]]
    return _tracking_ref_oid(repo_path, str(row["refname"])) == cursor


def _perform_manager_base_fetch(
    connection: sqlite3.Connection,
    repo_path: Path,
    intent_id: int,
    base: str,
) -> str:
    """Fetch the object, then update only the intended tracking ref with CAS."""
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ? AND phase = 'pending'",
        (intent_id,),
    ).fetchone()
    if row is None or row["refname"] != f"refs/remotes/origin/{base}":
        raise RuntimeError("Manager base fetch intent가 현재 base와 다릅니다.")
    token = str(row["action_token"] or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise RuntimeError("Manager base fetch action token이 유효하지 않습니다.")
    try:
        previous = json.loads(str(row["before_ref"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.") from error
    if previous is not None and (
        not isinstance(previous, list) or len(previous) != 2
        or previous[1] != ""
        or not isinstance(previous[0], str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", previous[0])
    ):
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.")
    target = str(row["target_oid"])
    git(repo_path, "fetch", "--no-tags", "--refmap=", "origin",
        f"refs/heads/{base}")
    fetched = git(repo_path, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
    if fetched != target:
        raise RuntimeError("Manager base fetch 원격 OID가 저장된 intent와 다릅니다.")
    git(
        repo_path, "update-ref", "--create-reflog", "-m",
        f"loop-manager-base-fetch:{token}", str(row["refname"]), target,
        previous[0] if previous is not None else "0" * len(target),
    )
    return base


def _finish_manager_base_fetch(
    connection: sqlite3.Connection,
    repo_path: Path,
    intent_id: int,
) -> None:
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ?",
        (intent_id,),
    ).fetchone()
    if row is None or git(repo_path, "rev-parse", str(row["refname"])).stdout.strip() != row["target_oid"]:
        raise RuntimeError("Manager base fetch 결과가 저장된 원격 OID와 다릅니다.")
    try:
        previous = json.loads(str(row["before_ref"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.") from error
    if previous != [row["target_oid"], ""] and not _manager_base_fetch_applied(repo_path, row):
        raise RuntimeError("Manager base fetch의 ref 갱신 증거가 없습니다.")
    with connection:
        connection.execute(
            "UPDATE manager_base_fetches SET phase = 'complete' WHERE id = ?",
            (intent_id,),
        )


def _journaled_manager_base_fetch(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    intent_id: int, base: str, plan_id: str,
) -> str:
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ? AND phase = 'pending'",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Manager base fetch intent를 찾을 수 없습니다.")
    previous = json.loads(str(row["before_ref"]))
    before_oid = previous[0] if previous is not None else None
    refname = str(row["refname"])
    target = str(row["target_oid"])

    def marker(_intent: Mapping[str, object]) -> bool:
        return before_oid == target or _manager_base_fetch_applied(repo_path, row)

    manager_git_journal.recover_active(
        state_dir, repo=str(_canonical_path(repo_path)),
        kind="project_fetch", target=refname,
        observe=lambda: {"oid": _tracking_ref_oid(repo_path, refname)},
        marker=marker,
    )
    current = _tracking_ref_oid(repo_path, refname)
    if current != target:
        manager_git_journal.execute(
            state_dir, owner=f"guidance:{plan_id}", kind="project_fetch",
            repo=str(_canonical_path(repo_path)), target=refname,
            before={"oid": before_oid}, expected={"oid": target},
            observe=lambda: {"oid": _tracking_ref_oid(repo_path, refname)},
            apply=lambda _token: _perform_manager_base_fetch(
                connection, repo_path, intent_id, base,
            ),
            marker=marker,
        )
    elif not marker({}):
        raise RuntimeError("Manager base fetch의 소유 ref 갱신 증거가 없습니다.")
    _finish_manager_base_fetch(connection, repo_path, intent_id)
    return base


def _proven_manager_base_refs(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    before: GitSnapshot,
    current: GitSnapshot,
) -> frozenset[str]:
    """Follow completed fetches or an interrupted fetch with its Git action."""
    _ensure_cleanup_pending_schema(connection)
    proven: set[str] = set()
    rows = connection.execute(
        "SELECT id, refname, before_ref, target_oid, action_token, phase "
        "FROM manager_base_fetches "
        "WHERE repo = ? AND plan_id = ? AND phase IN ('pending', 'complete') "
        "ORDER BY id",
        (repo, plan_id),
    ).fetchall()
    for refname in {str(row["refname"]) for row in rows}:
        if before.refs.get(refname) == current.refs.get(refname):
            continue
        cursor = before.refs.get(refname)
        for row in rows:
            if row["refname"] != refname:
                continue
            try:
                previous = json.loads(str(row["before_ref"]))
            except (TypeError, ValueError):
                continue
            previous = tuple(previous) if previous is not None else None
            if previous == cursor and _manager_base_fetch_applied(repo_path, row, rows):
                cursor = (str(row["target_oid"]), "")
        if cursor is not None and cursor == current.refs.get(refname):
            proven.add(refname)
    return frozenset(proven)


def _reconciled_manager_scope_metadata(connection, repo_path, state_dir, repo,
                                      plan_id, before, current, resources=None):
    """Use fetch receipts/CAS effects and exact Merge cleanup, never ref drift."""
    scope_before, scope_current = agent_scope_decision.scoped_metadata(before, current, resources)
    _ensure_cleanup_pending_schema(connection)
    proven_refs = _proven_manager_base_refs(
        connection, repo_path, repo, plan_id or '', scope_before, scope_current)
    sqlite_fetches = []
    for row in connection.execute(
            "SELECT refname, before_ref, target_oid, action_token FROM manager_base_fetches "
            "WHERE repo = ? AND plan_id = ? AND phase IN ('pending', 'complete')",
            (repo, plan_id or '')):
        try:
            previous = json.loads(str(row['before_ref']))
        except (TypeError, ValueError):
            continue
        if (not re.fullmatch(r'[0-9a-f]{32}', str(row['action_token'] or ''))
                or not str(row['refname']).startswith('refs/remotes/origin/')
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', str(row['target_oid']))
                or previous is not None and (not isinstance(previous, list)
                    or len(previous) != 2 or previous[1] != ''
                    or not isinstance(previous[0], str)
                    or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', previous[0]))):
            continue
        sqlite_fetches.append(dict(version=1, id=row['action_token'],
            owner=f"guidance:{plan_id}", repo=str(_canonical_path(repo_path)),
            kind='project_fetch', target=row['refname'],
            before={'oid': previous[0] if previous is not None else None},
            expected={'oid': row['target_oid']}))
    live_refs = {key: value[0] for key, value in scope_current.refs.items() if not value[1]}
    def marker(intent):
        if intent.get('kind') not in {'bootstrap_fetch', 'project_fetch'}:
            return False
        ref = intent['target']
        output = git(repo_path, 'reflog', 'show', '--format=%H%x00%gs', ref, check=False)
        return bool(not output.returncode and manager_git_journal.fetch_reflog_matches(
            state_dir, str(_canonical_path(repo_path)), intent,
            output.stdout.splitlines(), live_refs.get(ref), additional_fetches=sqlite_fetches))
    evidence = manager_git_journal.verified_candidates(
        state_dir, str(_canonical_path(repo_path)),
        baseline_refs={key: value[0] for key, value in scope_before.refs.items() if not value[1]},
        baseline_worktrees={}, live_refs=live_refs, live_worktrees={}, marker=marker,
        include_pending_fetch=True, additional_fetches=sqlite_fetches,
        fetch_refs=frozenset(key for key in scope_before.refs.keys() | scope_current.refs.keys()
                             if key.startswith('refs/remotes/origin/')
                             and scope_before.refs.get(key) != scope_current.refs.get(key)
                             and key not in proven_refs))
    proven_refs |= frozenset(key for key, value in evidence['refs'].items()
                            if current.refs.get(key) == (value, '')
                            and (before.refs.get(key) is None or not before.refs[key][1]))
    return agent_scope_decision.reconcile_manager_metadata(
        before, current, refs=proven_refs,
        worktrees=_proven_merged_cleanup_paths(connection, repo, scope_before, scope_current),
        resources=resources)


def complete_merged_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    project = connection.execute(
        "SELECT plan_path, branch FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        raise WorkspaceOwnershipError("Merge cleanup 프로젝트를 찾을 수 없습니다.")
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    if plan_id is None:
        raise WorkspaceOwnershipError("Merge cleanup ExecPlan ID를 확인할 수 없습니다.")
    branch = str(project["branch"] or "")
    _ensure_execplans_schema(connection)
    plan = connection.execute(
        "SELECT branch, worktree, workspace_owned, title FROM exec_plans "
        "WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is None:
        raise WorkspaceOwnershipError(
            f"Merge cleanup ExecPlan을 찾을 수 없습니다: {plan_id}"
        )
    workspace_owned = WORKSPACE_OWNED
    cleanup_registration: tuple[str, ...] | None = None
    worktree_present = worktree.exists() or worktree.is_symlink()
    if not worktree_present:
        assessment = _assess_missing_managed_cleanup(state_dir, repo_path, worktree)
    elif (
        int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        workspace_owned = 0
        assessment = CleanupAssessment(
            "unverifiable",
            "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}",
        )
    else:
        try:
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                plan_id,
                branch,
                worktree,
            )
            validate_managed_worktree(repo_path, worktree, branch)
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_owned = 0
            assessment = CleanupAssessment(
                "unverifiable",
                f"Merge cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, pr_head_oid)
            if assessment.status == "safe":
                cleanup_registration = capture_git_snapshot(repo_path).worktrees.get(
                    str(_canonical_path(worktree))
                )
            assessment = remove_cleanup_worktree(
                repo_path, worktree, assessment, state_dir=state_dir,
                owner=f"plan:{plan_id}",
            )
    with connection:
        _ensure_execplans_schema(connection)
        _ensure_cleanup_pending_schema(connection)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            _store_cleanup_pending(
                connection,
                repo,
                worktree,
                pr_url,
                pr_head_oid,
                assessment.reason or assessment.status,
                workspace_owned=workspace_owned,
            )
        project_values = _clear_active_values()
        runtime_state.RuntimeStateRepository(connection).mark_merged(
            repo,
            plan_id,
            project_values=project_values,
            enqueue=False,
        )
        _record_merged_worktree_cleanup(
            connection, repo_path, repo, plan_id, worktree, branch,
            pr_head_oid, assessment, cleanup_registration,
        )
        notifications.record_merged_plan(connection, repo, plan_id, str(plan["title"]))
    return assessment


def complete_merged_execplan_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
    plan: sqlite3.Row,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    """Complete exactly one merge-wait plan without hydrating another plan."""

    _ensure_cleanup_pending_schema(connection)
    branch = str(plan["branch"] or "")
    worktree = Path(str(plan["worktree"] or ""))
    workspace_owned = WORKSPACE_OWNED
    cleanup_registration: tuple[str, ...] | None = None
    # Merge recording is plan-scoped; managed worktree cleanup can wait until
    # the planning attempt releases the shared repository mutation slot.
    planning_owns_slot = not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo)
    if planning_owns_slot:
        assessment = CleanupAssessment("deferred", "planning 실행 자리 보존")
    elif not (worktree.exists() or worktree.is_symlink()):
        assessment = _assess_missing_managed_cleanup(state_dir, repo_path, worktree)
    elif int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        workspace_owned = WORKSPACE_UNOWNED
        assessment = CleanupAssessment(
            "unverifiable",
            "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}",
        )
    else:
        try:
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                str(plan["plan_id"]),
                branch,
                worktree,
            )
            validate_managed_worktree(repo_path, worktree, branch)
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_owned = WORKSPACE_UNOWNED
            assessment = CleanupAssessment(
                "unverifiable",
                f"Merge cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, pr_head_oid)
            if assessment.status == "safe":
                cleanup_registration = capture_git_snapshot(repo_path).worktrees.get(
                    str(_canonical_path(worktree))
                )
            assessment = remove_cleanup_worktree(
                repo_path,
                worktree,
                assessment,
                state_dir=state_dir,
                owner=f"plan:{plan['plan_id']}",
            )

    with connection:
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            _store_cleanup_pending(
                connection,
                repo,
                worktree,
                pr_url,
                pr_head_oid,
                assessment.reason or assessment.status,
                workspace_owned=workspace_owned,
            )
        plan_id = str(plan["plan_id"])
        mirrored = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        project_values: dict[str, object] = {}
        if (mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
                and mirrored["plan_path"] == plan["path"]):
            project_values.update(_clear_active_values())
        runtime_state.RuntimeStateRepository(connection).mark_merged(
            repo,
            plan_id,
            project_values=project_values,
            enqueue=False,
        )
        _record_merged_worktree_cleanup(
            connection, repo_path, repo, plan_id, worktree, branch,
            pr_head_oid, assessment, cleanup_registration,
        )
        scoped_blocking.clear_plan_block(connection, repo, str(plan["plan_id"]))
        notifications.record_merged_plan(
            connection, repo, str(plan["plan_id"]), str(plan["title"])
        )
        if assessment.status == "unverifiable":
            scoped_blocking.record_project_block(
                connection,
                repo,
                BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
                str(assessment.reason or assessment.status),
                None,
                plan_id=str(plan["plan_id"]),
                previous_phase=None,
            )
        notifications.enqueue_current_project_events(connection, repo)
    return assessment


@dataclass(frozen=True)
class PullRequestSyncResult:
    step_result: int | None = None
    conflict_started: bool = False
    merged: bool = False


@dataclass(frozen=True)
class WaitingPullRequestObservation:
    conflicts: tuple[tuple[sqlite3.Row, Mapping[str, object]], ...]
    merged: bool
    merged_pr_urls: tuple[str, ...] = ()


def observe_waiting_pull_requests(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    require_base_identity: bool = False,
    read_only: bool = False,
) -> WaitingPullRequestObservation:
    """Commit every merge-wait observation before source or conflict work."""

    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND "
        "(status = 'PR_OPEN' OR (status = 'PLAN_BLOCKED' AND blocked_reason = ?)) "
        "ORDER BY plan_id",
        (repo, PR_CLOSED_BLOCK_REASON),
    ).fetchall()
    registered_base = ""
    if rows:
        project = connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if project is None or not project["base_branch"]:
            raise RuntimeError("Merge 대기 PR의 등록 base branch를 확인할 수 없습니다.")
        registered_base = str(project["base_branch"])
    conflicts: list[tuple[sqlite3.Row, Mapping[str, object]]] = []
    merged = False
    merged_pr_urls: list[str] = []
    for stored in rows:
        branch = str(stored["branch"] or "")
        if not branch:
            raise RuntimeError(
                f"저장된 Merge 대기 PR의 관리 branch를 확인할 수 없습니다: {stored['plan_id']}"
            )
        pull_request = pr_for_branch(
            repo_path,
            branch,
            str(stored["pr_url"] or "") or None,
        )
        if pull_request is None:
            raise RuntimeError(
                f"저장된 Merge 대기 PR의 GitHub 상태를 확인할 수 없습니다: {branch}"
            )
        observed_url = str(pull_request.get("url") or "")
        stored_url = str(stored["pr_url"] or "")
        if stored_url and observed_url and observed_url != stored_url:
            raise PublicationSafetyError(
                f"저장된 PR URL과 GitHub PR identity가 다릅니다: {branch}"
            )
        head_name = pull_request.get("headRefName")
        if head_name and str(head_name) != branch:
            raise PublicationSafetyError(
                f"저장된 PR head branch와 GitHub PR identity가 다릅니다: {branch}"
            )
        observed_base = pull_request.get("baseRefName")
        if observed_base is not None and str(observed_base) != registered_base:
            raise PublicationSafetyError(
                f"GitHub PR base branch가 등록 base와 다릅니다: {branch}"
            )
        if require_base_identity and not observed_base:
            raise PublicationSafetyError(
                f"self PR base branch identity를 확인할 수 없습니다: {branch}"
            )
        url = str(pull_request.get("url") or stored["pr_url"] or "")
        if not url:
            raise RuntimeError(f"저장된 Merge 대기 PR URL을 확인할 수 없습니다: {branch}")
        head_value = pull_request.get("headRefOid")
        head_oid = str(head_value) if head_value else stored["pr_head_oid"]
        state = str(pull_request.get("state") or "OPEN").upper()
        if read_only:
            continue
        if state == "MERGED" or pull_request.get("mergedAt"):
            if scoped_blocking.project_block(connection, repo) is not None:
                continue
            assessment = complete_merged_execplan_worktree(
                connection,
                repo_path,
                repo,
                state_dir,
                stored,
                url,
                str(head_oid) if head_oid else None,
            )
            merged = True
            merged_pr_urls.append(url)
            if assessment.status in {"safe", "missing"}:
                operation_print(f"Merge 확인 및 worktree 정리 완료: {url}")
            else:
                operation_print(
                    f"Merge 확인 완료; worktree 보존: {stored['worktree']} "
                    f"({assessment.reason or assessment.status})",
                    file=sys.stderr,
                )
            continue
        if state == "CLOSED":
            if stored["status"] == "PR_OPEN":
                _block_closed_pull_request(connection, repo, stored, url, head_oid)
            continue
        if state != "OPEN":
            raise RuntimeError(f"지원하지 않는 GitHub PR 상태입니다: {state}")
        if (
            stored["status"] == "PLAN_BLOCKED"
            and stored["blocked_reason"] == PR_CLOSED_BLOCK_REASON
        ):
            expected_head = str(stored["pr_head_oid"] or "")
            if expected_head and head_oid and str(head_oid) != expected_head:
                if not runtime_state.RuntimeStateRepository(
                    connection
                ).recovery_slot_available(repo):
                    # Keep the closed-PR block until its head can be checked
                    # without replacing an unrelated planning projection.
                    continue
                error = (
                    "다시 열린 PR head OID가 저장된 관리 head와 다릅니다: "
                    f"expected {expected_head}, actual {head_oid}"
                )
                with connection:
                    scoped_blocking.record_project_block(
                        connection,
                        repo,
                        BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                        error,
                        expected_head,
                        plan_id=str(stored["plan_id"]),
                        previous_phase=None,
                    )
                    notifications.enqueue_current_project_events(connection, repo)
                continue
            _resume_reopened_pull_request(
                connection,
                repo,
                stored,
                url,
                head_oid,
                recovery=False,
            )
            stored = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, stored["plan_id"]),
            ).fetchone()
        with connection:
            resumed = notifications.record_work_start(
                connection, repo, "PR handling", plan_id=str(stored["plan_id"]),
            )
        if resumed:
            operation_log(f"차단 해제 후 PR 처리 재개: {stored['plan_id']}")
        mergeability = classify_pull_request_mergeability(pull_request)
        with connection:
            plan_values: dict[str, object] = {"pr_url": url}
            # Conflict recovery must compare GitHub's head with the stored
            # ownership OID before adopting a changed head.
            if mergeability != "conflicting":
                plan_values["pr_head_oid"] = head_oid
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                str(stored["plan_id"]),
                plan_values=plan_values,
                enqueue=False,
            )
            notifications.enqueue_current_project_events(connection, repo)
        if mergeability == "conflicting":
            conflicts.append((stored, pull_request))

    # Each completed transition is committed before a remote fetch can fail.
    # A later run fetches again and resumes source synchronization from SQLite.
    if merged:
        project = connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if project is None or not project["base_branch"]:
            raise RuntimeError("Merge 뒤 fetch할 프로젝트 base branch를 확인할 수 없습니다.")
        base = str(project["base_branch"])
        _recorded_base_fetch(repo_path, state_dir, base, owner="project")
    return WaitingPullRequestObservation(
        tuple(conflicts), merged, tuple(merged_pr_urls)
    )


def select_waiting_pull_request_conflict(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    observation: WaitingPullRequestObservation,
) -> PullRequestSyncResult:
    """Select one still-eligible conflict after all observations are durable."""

    if active_execplan(connection, repo) is not None:
        return PullRequestSyncResult(merged=observation.merged)
    if not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo):
        return PullRequestSyncResult(merged=observation.merged)
    if scoped_blocking.project_block(connection, repo) is not None:
        return PullRequestSyncResult(merged=observation.merged)
    current_rows = {
        str(row["plan_id"]): row
        for row in connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
        )
    }
    statuses = {
        plan_id: str(row["status"]) for plan_id, row in current_rows.items()
    }
    eligible: list[tuple[sqlite3.Row, Mapping[str, object]]] = []
    for stored, pull_request in observation.conflicts:
        current = current_rows.get(str(stored["plan_id"]))
        if current is None or current["status"] != "PR_OPEN":
            continue
        if all(
            statuses.get(str(dependency)) == "MERGED"
            for dependency in json.loads(str(current["depends_on"]))
        ):
            eligible.append((current, pull_request))
    if not eligible:
        return PullRequestSyncResult(merged=observation.merged)
    plan, pull_request = min(
        eligible,
        key=lambda item: (
            PRIORITY_ORDER[str(item[0]["priority"])],
            int(str(item[0]["plan_id"])[2:]),
        ),
    )
    hydrate_execplan_project(connection, repo, plan)
    project = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    assert project is not None
    result = reconcile_existing_pull_request(
        connection,
        repo_path,
        state_dir,
        repo,
        project,
        pull_request,
    )
    return PullRequestSyncResult(result, result is None, observation.merged)


def synchronize_waiting_pull_requests(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    after_reconciliation: Callable[[], None] | None = None,
    after_observation: Callable[[WaitingPullRequestObservation], None] | None = None,
    require_base_identity: bool = False,
) -> PullRequestSyncResult:
    observation = observe_waiting_pull_requests(
        connection, repo_path, state_dir, repo,
        require_base_identity=require_base_identity,
    )
    if after_observation is not None:
        after_observation(observation)
    if after_reconciliation is not None:
        after_reconciliation()
    return select_waiting_pull_request_conflict(
        connection, repo_path, state_dir, repo, observation
    )


def _reconcile_guidance_double_block(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    block: sqlite3.Row,
    project: sqlite3.Row,
    plan: sqlite3.Row,
) -> bool:
    """Restore a published guidance wait only from independent Git and DB proof."""
    false_positive_reasons = {
        BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION.value,
        BlockReasonCode.SHARED_REF_MUTATION.value,
        BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION.value,
        BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
    }
    if (
        block["reason_code"] not in false_positive_reasons
        or plan["status"] != "PLAN_BLOCKED"
        or plan["blocked_reason"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}
        or plan["agent_role"] != "worker"
        or not plan["guidance_plan_hash"]
        or not plan["publication_remote_oid"]
        or not plan["worker_session_id"]
        or project["plan_path"] != plan["path"]
        or project["branch"] != plan["branch"]
        or project["worktree"] != plan["worktree"]
        or project["agent_role"] != "worker"
        or project["agent_snapshot"] != plan["agent_snapshot"]
        or project["worker_session_id"] != plan["worker_session_id"]
        or int(plan["workspace_owned"] or 0) != WORKSPACE_OWNED
    ):
        return False
    checkpoint = connection.execute(
        "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan["plan_id"]),
    ).fetchone()
    recorded_block = connection.execute(
        "SELECT * FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
        (repo, plan["plan_id"]),
    ).fetchone()
    if checkpoint is None or recorded_block is None:
        return False
    branch = str(plan["branch"])
    worktree = Path(str(plan["worktree"]))
    oid = str(plan["publication_remote_oid"])
    try:
        validate_execplan_workspace_identity(
            state_dir, repo, str(plan["plan_id"]), branch, worktree,
        )
        validate_managed_worktree(repo_path, worktree, branch)
        before = GitSnapshot.deserialize(str(plan["agent_snapshot"]))
        current = capture_git_snapshot(worktree)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
            repo, str(plan["plan_id"]),
        )
        if _owned_checkpoint_remote_oid(connection, repo_path, request, plan, current) != oid:
            raise ValueError("게시 OID와 원격 head가 일치하지 않습니다.")
        evidence = _guidance_checkpoint_commit_evidence(
            repo_path, str(plan["plan_id"]), oid,
        )
        if evidence is None:
            raise ValueError("guidance checkpoint commit identity 또는 tree가 유효하지 않습니다.")
        document = _guidance_document(worktree, str(plan["path"]))
        rows = _guidance_rows(connection, repo, str(plan["plan_id"]))
        if (
            document is None or document.structure_hash != plan["guidance_plan_hash"]
            or not rows or any(row["applied"] or row["invalidated_decision_hash"] for row in rows)
        ):
            raise ValueError("guidance 제안 또는 승인 대기 상태가 변경됐습니다.")
        proposals, new = _guidance_document_matches_rows(
            document, rows, require_decisions=False,
        )
        if new or any(
            row["decision_hash"]
            and proposals[str(row["proposal_id"])].decision.identity != row["decision_hash"]
            for row in rows
        ):
            raise ValueError("guidance User Decision과 저장된 증거가 다릅니다.")
        for target in {str(row["target_path"]) for row in rows}:
            row = next(row for row in rows if row["target_path"] == target)
            if _guidance_target_state(worktree, target) != (
                bytes(row["baseline_content"]), int(row["baseline_mode"]),
            ):
                raise ValueError(f"승인 전 AGENTS.md 내용이 변경됐습니다: {target}")
        own_path = str(_canonical_path(worktree))
        cleaned = _proven_merged_cleanup_paths(connection, repo, before, current)
        fetched_refs = _proven_manager_base_refs(
            connection, repo_path, repo, str(plan["plan_id"]), before, current,
        )
        remote_ref = f"refs/remotes/origin/{branch}"
        own_ref = f"refs/heads/{branch}"
        if (
            not before.shared_metadata_complete or not current.shared_metadata_complete
            or before.head != oid or current.head != oid
            or before.branch != branch or current.branch != branch
            or before.index != current.index
            or before.config != current.config
            or before.operations != current.operations
            or any(path != str(plan["path"]) for path in current.dirty)
            or any(
                before.refs.get(name) != current.refs.get(name)
                for name in before.refs.keys() | current.refs.keys()
                if name not in {remote_ref, own_ref} | fetched_refs
                and not name.startswith("refs/codex/turn-diffs/")
            )
            or any(
                before.worktrees.get(path) != current.worktrees.get(path)
                for path in before.worktrees.keys() | current.worktrees.keys()
                if path not in cleaned | {own_path}
            )
        ):
            raise ValueError("저장된 Worker snapshot 밖의 Git 변경이 있습니다.")
        if before.worktrees.get(own_path) != current.worktrees.get(own_path):
            raise ValueError("guidance worktree 등록이 변경됐습니다.")
        if checkpoint["phase"] == "published" and checkpoint["published_oid"] != oid:
            raise ValueError("저장된 checkpoint 게시 OID가 다릅니다.")
        if checkpoint["phase"] == "published" and checkpoint["reason_code"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}:
            raise ValueError("저장된 checkpoint 차단 원인이 guidance와 무관합니다.")
        if checkpoint["phase"] != "published" and checkpoint["reason_code"] not in false_positive_reasons:
            raise ValueError("교체된 checkpoint 원인의 소유권을 확인할 수 없습니다.")
        if recorded_block["reason_code"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}:
            raise ValueError("계획 차단 원인이 guidance와 무관합니다.")
        commit_message = git(repo_path, "show", "-s", "--format=%B", oid).stdout
        commit_fields = dict(line.split(": ", 1) for line in commit_message.splitlines() if ": " in line)
        tree = git(repo_path, "rev-parse", f"{oid}^{{tree}}").stdout.strip()
    except (AgentScopeError, GuidanceFormatError, RuntimeError, ValueError, KeyError, TypeError, WorkspaceOwnershipError) as error:
        operation_log(f"guidance 이중 차단 복구 보류 ({plan['plan_id']}): {error}", error=True)
        return False
    identity = blocked_checkpoint_state.request_identity(
        repo, str(plan["plan_id"]), evidence,
        branch, str(worktree), str(plan["guidance_plan_hash"]),
    )
    with runtime_state._transaction(connection):
        connection.execute(
            "UPDATE exec_plans SET blocked_reason = ?, blocked_fingerprint = NULL, "
            "blocked_worktree_fingerprint = NULL, last_error = ? "
            "WHERE repo = ? AND plan_id = ?",
            (GUIDANCE_BLOCK_REASON, evidence, repo, plan["plan_id"]),
        )
        connection.execute(
            "UPDATE execplan_blocks SET reason_code = ?, evidence = ?, "
            "release_fingerprint = ? WHERE repo = ? AND plan_id = ?",
            (GUIDANCE_BLOCK_REASON, evidence, plan["guidance_plan_hash"],
             repo, plan["plan_id"]),
        )
        connection.execute(
            "UPDATE blocked_checkpoints SET request_identity = ?, phase = 'published', "
            "reason_code = ?, evidence = ?, branch = ?, worktree = ?, "
            "commit_identity = ?, tree_oid = ?, local_oid = ?, remote_oid = ?, "
            "published_oid = ?, accepted_remote_oid = ?, result = 'published', "
            "unpublished_reason = NULL, retryable = 0 WHERE repo = ? AND plan_id = ?",
            (identity, evidence, branch, str(worktree),
             commit_fields["Checkpoint-Identity"], tree, oid, oid, oid, oid,
             repo, plan["plan_id"]),
        )
        scoped_blocking.clear_project_block(connection, repo)
        connection.execute(
            "UPDATE projects SET phase = 'PLAN_BLOCKED', last_error = ? WHERE repo = ?",
            (evidence, repo),
        )
    return True


def _published_review_commit_intent_matches(
    repo_path: Path, checkpoint: execution_checkpoint_state.CheckpointRecord,
    head: str,
) -> bool:
    """Prove an unfinished Manager commit intent produced the current HEAD."""
    saved = checkpoint.snapshot or {}
    if (checkpoint.operation != 'commit' or not checkpoint.expected_head
            or checkpoint.expected_head == head
            or not saved.get('commit_tree_oid')):
        return False
    return checkpoint_ownership.committed_tree_matches_intent(
        repo_path, checkpoint.expected_head, head, saved, git,
    )


def _resume_owned_checkpoint_project_block(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, block: sqlite3.Row, project: sqlite3.Row, plan: sqlite3.Row,
) -> bool:
    return scope_reconciliation._resume_owned_checkpoint_project_block(
        _scope_reconciliation_effects_resume_owned_checkpoint_project_block(),
        connection, repo_path, state_dir, repo, block, project, plan,
    )


def _clear_project_block_without_losing_planning_error(
    connection: sqlite3.Connection, repo: str, project: sqlite3.Row,
) -> None:
    scoped_blocking.clear_project_block(connection, repo)
    if (
        runtime_state.planning_owns_execution_slot(project)
        and project["planning_error"]
    ):
        connection.execute(
            "UPDATE projects SET last_error = ? WHERE repo = ?",
            (project["planning_error"], repo),
        )


def _reconcile_rebase_evidence_block(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, block: sqlite3.Row, *, plan_block: bool,
) -> bool:
    """Recheck the owned current rebase and release only its saved cause."""
    plan_id = str(block["plan_id"] or "")
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?", (repo, plan_id),
    ).fetchone()
    project = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if plan is None or project is None or block['repo'] != repo:
        return False
    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    checkpoint = checkpoints.get(repo, plan_id)
    recovery = recovery_state.RecoveryRepository(connection).row(repo, plan_id)
    if not rebase_block_reconciliation.block_matches(block, recovery, checkpoint):
        return False
    if scoped_blocking.is_scope_baseline_block(block) and (
            not plan['agent_snapshot'] or plan['agent_recovery']):
        return False
    active = runtime_state.RuntimeStateRepository(connection).active_row(repo)
    if (runtime_state.planning_owns_execution_slot(project) or project['planning_error']
            or (active is not None and active['plan_id'] != plan_id)
            or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
            or not plan['branch'] or not plan['worktree']
            or unmet_execplan_dependencies(connection, repo, plan_id)
            or (plan_block and (plan['status'] != 'PLAN_BLOCKED'
                or plan['blocked_reason'] != block['reason_code']
                or scoped_blocking.project_block(connection, repo) is not None))
            or (not plan_block and project['phase'] != 'PROJECT_BLOCKED')):
        return False
    worktree, branch = Path(str(plan['worktree'])), str(plan['branch'])
    try:
        service = _current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch)
        def observe():
            observation = service.require_owned(checkpoint=checkpoints.get(repo, plan_id))
            validate_blocked_agent_scope(
                connection, repo_path, repo, str(project['base_branch']), worktree,
                str(plan['path']), accepted_rebase=True, saved_plan=plan)
            if (observation[1].status is managed_rebase_validation.RebaseStatus.WAITING
                    and plan['agent_result_snapshot']
                    and plan['blocked_fingerprint'] and plan['blocked_worktree_fingerprint']):
                before = GitSnapshot.deserialize(str(plan['agent_result_snapshot']))
                current = capture_git_snapshot(worktree)
                if rebase_block_reconciliation.pending_block_unchanged(
                        plan, block, recovery, before, current,
                        revision_input=_revision_execplan_input_fingerprint(
                            repo_path, str(plan['current_revision_sha'] or plan['plan_sha']), plan),
                        worktree_input=_worktree_execplan_input_fingerprint(worktree, plan)):
                    raise RuntimeError('unchanged blocked rebase still needs user input or Git progress')
            return observation
        observation = observe()
        runtime_state.RuntimeStateRepository(connection).resume_rebase_block(
            plan, project, block, checkpoints, observation, expected_checkpoint=checkpoint,
            is_plan_block=plan_block,
            reobserve=observe,
            dependencies_ready=lambda: not unmet_execplan_dependencies(connection, repo, plan_id),
            pending_error=REBASE_IN_PROGRESS_ERROR)
        return True
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as error:
        operation_log(f"관리 rebase 차단 재평가 보류 ({plan_id}): {error}", error=True)
        return False


def _reconcile_scope_baseline_plan_block(connection, repo_path, state_dir, repo, block):
    """Release a scope false positive without input changes or retry resets."""
    plan_id = block['plan_id']
    runtime = runtime_state.RuntimeStateRepository(connection)
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                              (repo, plan_id)).fetchone()
    project = connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()
    if (plan is None or project is None or plan['status'] != 'PLAN_BLOCKED'
            or not scoped_blocking.is_scope_baseline_block(block)
            or plan['blocked_reason'] != block['reason_code']
            or not plan['agent_snapshot'] or plan['agent_recovery']
            or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
            or scoped_blocking.project_block(connection, repo) is not None
            or runtime_state.planning_owns_execution_slot(project) or project['planning_error']
            or not runtime.recovery_slot_available(repo) or runtime.active_row(repo) is not None
            or unmet_execplan_dependencies(connection, repo, plan_id)):
        return False
    worktree, branch = Path(str(plan['worktree'] or '')), str(plan['branch'] or '')
    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    checkpoint = checkpoints.get(repo, plan_id)
    try:
        def observe():
            current = capture_git_snapshot(worktree)
            validate_execplan_workspace_identity(state_dir, repo, plan_id, branch, worktree)
            validate_managed_worktree(repo_path, worktree, branch)
            validate_blocked_agent_scope(connection, repo_path, repo, project['base_branch'],
                                         worktree, plan['path'], saved_plan=plan, require_safe_result=True)
            # Reuse the live remote/PR ownership gate even without a rebase.
            request = connection.execute('SELECT 1 FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                         (repo, plan_id)).fetchone()
            if request is not None:
                saved = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(repo, plan_id)
                if saved.phase.value == 'published' and _owned_checkpoint_remote_oid(
                        connection, repo_path, saved, plan, current) is None:
                    raise AgentScopeError('scope 차단의 게시 checkpoint 소유권 불일치')
            elif plan['publication_remote_oid'] or plan['pr_url']:
                raise AgentScopeError('scope 차단의 원격 소유권 checkpoint가 없습니다.')
            if capture_git_snapshot(worktree) != current:
                raise AgentScopeError('scope 차단 검사 중 Git 상태가 변경됐습니다.')
            return current
        observed = observe()
        with runtime_state._transaction(connection):
            if (dict(connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                         (repo, plan_id)).fetchone()) != dict(plan)
                    or dict(connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()) != dict(project)
                    or checkpoints.get(repo, plan_id) != checkpoint or observe() != observed):
                raise AgentScopeError('scope 차단 해제 중 소유 상태가 변경됐습니다.')
            scoped_blocking.clear_revalidated_rebase_block(connection, block, is_plan_block=True)
            recovery_state.RecoveryRepository(connection).resolve_rebase_block(repo, plan_id, block)
            checkpoints.record(repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**(checkpoint.snapshot or {}), 'scope_block_release_audit': [
                    *(checkpoint.snapshot or {}).get('scope_block_release_audit', ()),
                    {'block': dict(block), 'candidate': observed.serialize(),
                     'prior_artifacts': {key: plan[key] for key in (
                         'worker_result', 'verify_result', 'review_result', 'last_error')}}]})
            values = managed_rebase_adoption.revalidation_values(plan['active_sha'])
            values.pop('phase')
            values.update(status='IMPLEMENTING', blocked_reason=None, blocked_fingerprint=None,
                          blocked_worktree_fingerprint=None, blocked_review_fingerprint=None)
            updated = runtime.update_plan(repo, plan_id, plan_values=values, enqueue=False)
            runtime.hydrate_snapshot(repo, updated)
        return True
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as error:
        operation_log(f'Agent scope 차단 재평가 보류 ({plan_id}): {error}', error=True)
        return False


def resume_project_block_if_safe(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    return scope_reconciliation.resume_project_block_if_safe(_scope_reconciliation_effects_resume_project_block_if_safe(), connection, repo_path, state_dir, repo)


def _remote_checkpoint_input_identity(
    blocked: sqlite3.Row, snapshot: GitSnapshot | None,
    ignored_paths: tuple[str, ...] = (),
) -> str:
    payload = (
        str(blocked["blocked_reason"] or ""),
        str(blocked["blocked_fingerprint"] or ""),
        str(blocked["blocked_worktree_fingerprint"] or ""),
        snapshot.serialize() if snapshot is not None else "unavailable",
        ignored_paths,
    )
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _remote_checkpoint_refusal(
    connection: sqlite3.Connection,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    row: sqlite3.Row,
    oid: str | None,
    input_identity: str,
    reason: str,
    *, retryable: bool = False, preserve_cause: bool = False,
) -> None:
    if (
        row["inspected_remote_oid"] == oid
        and row["inspected_input"] == input_identity
        and row["inspection_reason"] == reason
    ):
        return
    with runtime_state._transaction(connection):
        repository.record_remote_refusal(
            request, oid, input_identity, reason, commit=False,
            retryable=retryable,
        )
        runtime = runtime_state.RuntimeStateRepository(connection)
        projected = runtime.projected_row(request.repo)
        if preserve_cause and (
            scoped_blocking.project_block(connection, request.repo) is not None
            or projected is not None and projected['plan_id'] != request.plan_id
        ):
            runtime.update_plan(request.repo, request.plan_id, plan_values={"last_error": reason}, enqueue=False)
        else:
            runtime.transition(
                request.repo, request.plan_id,
                plan_values={"last_error": reason},
                project_values={"last_error": reason},
                enqueue=False,
            )
        if not preserve_cause:
            connection.execute(
                "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND plan_id = ?",
                (reason, request.repo, request.plan_id),
            )
        notifications.enqueue_current_project_events(connection, request.repo)


def _resume_remote_corrected_execplan(
    connection: sqlite3.Connection, repo: str, plan: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    candidate_oid: str, input_identity: str, reason: str,
    *, new_failure_stage: str | None = None, assessment: str | None = None,
) -> None:
    """Treat a user's committed correction as unverified Worker input."""
    plan_id = str(plan["plan_id"])
    # Hydration commits; finish it before the atomic release boundary.
    hydrate_execplan_project(connection, repo, plan)
    with runtime_state._transaction(connection):
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, plan_id,
            plan_values={
                "status": "IMPLEMENTING", "blocked_reason": None,
                "blocked_fingerprint": None, "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None, "last_error": None,
                "retry_count": 0, "worker_result": None,
                "verify_result": assessment if new_failure_stage == "verify" else None,
                "review_result": assessment if new_failure_stage == "review" else None,
                "validated_base_sha": None, "reviewer_session_id": None,
                "worker_input_token": None, "archive_token": None,
                "verify_token": None, "review_token": None,
                "publication_token": None, "publication_remote_oid": None,
                "verify_failure_history": plan["verify_failure_history"] if new_failure_stage else "{}",
                "review_failure_history": plan["review_failure_history"] if new_failure_stage else "{}",
                "effective_worker_level": plan["initial_worker_level"],
                "review_level_history": "{}",
                "agent_role": None, "agent_snapshot": None,
                "agent_baseline": None, "agent_recovery": None,
                "agent_input_plan": None, "agent_result_plan": None,
                "agent_input_fingerprint": None,
                "agent_result_snapshot": None,
                "agent_manager_snapshot": None,
            },
            project_values={
                "phase": "WORKING", "last_error": None,
                "worker_result": None,
                "verify_result": assessment if new_failure_stage == "verify" else None,
                "review_result": assessment if new_failure_stage == "review" else None, "validated_base_sha": None,
                "worker_session_id": plan["worker_session_id"],
                "agent_role": None, "agent_snapshot": None,
                "agent_baseline": None, "agent_recovery": None,
                "agent_input_plan": None, "agent_result_plan": None,
                "agent_input_fingerprint": None,
                "agent_result_snapshot": None,
                "agent_manager_snapshot": None,
            },
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)
        repository.record_remote_inspection(
            request, candidate_oid, input_identity, reason, commit=False,
        )


def _remote_checkpoint_has_new_failure(
    connection: sqlite3.Connection, repo: str, blocked: sqlite3.Row,
    stage: str, output: str, worktree: Path,
) -> bool:
    """Use the normal validation fingerprint contract to recognize progress."""
    history = _deserialize_failure_history(blocked[f"{stage}_failure_history"])
    previous = blocked[f"{stage}_result"]
    known = set(history)
    if previous:
        known.update(key for key, _ in validation_failure_fingerprints(stage, previous, worktree))
    if not known:
        request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
            repo, str(blocked["plan_id"]),
        )
        if request and request.evidence:
            try:
                known.update(key for key, _ in validation_failure_fingerprints(
                    stage, request.evidence, worktree,
                ))
            except ReviewerProtocolError:
                return False
    return bool(known) and any(
        key not in known for key, _ in validation_failure_fingerprints(stage, output, worktree)
    )


def _remote_checkpoint_review_revision(
    repo_path: Path, worktree: Path, base_branch: str,
    state_dir: Path,
) -> tuple[str, str] | None:
    """Read the latest base and a candidate HEAD that contains that base."""
    base = _recorded_base_fetch(
        repo_path, state_dir, base_branch, owner="project",
    )
    head = git(worktree, "rev-parse", "HEAD^{commit}").stdout.strip()
    if not base or not head:
        raise RuntimeError("원격 checkpoint 검증 revision을 확인할 수 없습니다.")
    ancestry = git(
        repo_path, "merge-base", "--is-ancestor", base, head, check=False,
    ).returncode
    if ancestry not in {0, 1}:
        raise RuntimeError("원격 checkpoint 후보의 base ancestry를 확인할 수 없습니다.")
    return (base, head) if ancestry == 0 else None


def _evaluate_remote_checkpoint_cause(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, blocked: sqlite3.Row, config_path: Path | None,
    timeout_seconds: int | None,
) -> tuple[bool, str, str | None]:
    return checkpoint_recovery._evaluate_remote_checkpoint_cause(
        _checkpoint_recovery_effects_evaluate_remote_checkpoint_cause(),
        connection, repo_path, state_dir, repo, blocked, config_path, timeout_seconds,
    )


def _reclassify_remote_specification_deadlock(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    blocked: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    accepted_oid: str,
    worktree: Path,
    review: str,
) -> None:
    """Persist a newly discovered specification block beside its published checkpoint."""

    verdict, details = parse_reviewer_result(review)
    if verdict != "SPEC_BLOCKED" or not isinstance(
        details, reviewer_specification.SpecificationDeadlock
    ):
        raise ReviewerProtocolError("원격 checkpoint 명세 교착 증거가 유효하지 않습니다.")
    _reclassify_remote_checkpoint_block(
        connection, repo_path, repo, blocked, repository, request,
        accepted_oid, worktree,
        reason=BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
        evidence=review, review_fingerprint_source=review,
        user_targets=details.paths,
    )


def _reclassify_remote_checkpoint_block(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    blocked: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    accepted_oid: str,
    worktree: Path,
    *,
    reason: BlockReasonCode,
    evidence: str,
    review_fingerprint_source: str | None,
    user_targets: tuple[str, ...],
) -> None:
    """Replace one final cause without resetting its published checkpoint."""

    base_revision = str(
        blocked["active_sha"] or blocked["current_revision_sha"] or blocked["plan_sha"] or ""
    )
    release_fingerprint = _revision_execplan_input_fingerprint(
        repo_path, base_revision, blocked,
    )
    review_fingerprint = (
        _revision_review_issue_fingerprint(
            repo_path, base_revision, review_fingerprint_source,
        )
        if review_fingerprint_source is not None else None
    )
    worktree_fingerprint = _worktree_execplan_input_fingerprint(worktree, blocked)
    observation = recovery_runtime.RecoveryObservation(
        reason=recovery_runtime.reason_from_block(reason),
        repo=repo, plan_id=str(blocked["plan_id"]), phase="PLAN_BLOCKED",
        plan_revision=str(blocked["current_revision_sha"] or blocked["plan_sha"] or ""),
        base_revision=base_revision,
        fingerprint=hashlib.sha256(
            f"{release_fingerprint}|{review_fingerprint or evidence}".encode()
        ).hexdigest(),
        progress_marker=accepted_oid, detail=evidence,
        user_targets=user_targets,
    )
    with runtime_state._transaction(connection):
        decision = recovery_state.RecoveryRepository(connection).observe(
            observation, final_block_reason=reason,
        ).decision
        recovery_runtime.require_final(decision)
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, str(blocked["plan_id"]), expected_status="PLAN_BLOCKED",
            plan_values={
                "blocked_reason": reason.value,
                "blocked_fingerprint": release_fingerprint,
                "blocked_worktree_fingerprint": worktree_fingerprint,
                "blocked_review_fingerprint": review_fingerprint,
                "review_result": evidence,
                "last_error": evidence,
                "review_failure_history": "{}",
                "review_level_history": "{}",
                "retry_count": 0,
            },
            project_values={"last_error": evidence},
            enqueue=False,
        )
        changed = connection.execute(
            "UPDATE execplan_blocks SET reason_code = ?, evidence = ?, "
            "release_fingerprint = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND plan_id = ? AND reason_code = ?",
            (
                reason.value, evidence, release_fingerprint, repo,
                str(blocked["plan_id"]), str(blocked["blocked_reason"]),
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("원격 checkpoint 차단 원인이 변경됐습니다.")
        repository.reclassify_published(
            request, reason_code=reason.value, evidence=evidence,
            release_fingerprint=release_fingerprint,
            accepted_oid=accepted_oid,
        )
        notifications.enqueue_current_project_events(connection, repo)


def _reconcile_published_blocked_checkpoint(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    blocked: sqlite3.Row,
    row: sqlite3.Row,
    request: blocked_checkpoint.CheckpointRequest,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    config_path: Path | None = None,
    verify_timeout_seconds: int | None = None,
    *, adopt_only: bool = False, expected_remote_oid: str | None = None,
    project_recovery_context: dict | None = None,
) -> str:
    return checkpoint_recovery._reconcile_published_blocked_checkpoint(
        _checkpoint_recovery_effects_reconcile_published_blocked_checkpoint(),
        connection, repo_path, state_dir, repo, blocked, row, request, repository, config_path, verify_timeout_seconds, adopt_only=adopt_only, expected_remote_oid=expected_remote_oid, project_recovery_context=project_recovery_context,
    )


def _resume_legacy_review_count_block(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, plan: sqlite3.Row,
) -> bool:
    return scope_reconciliation._resume_legacy_review_count_block(
        _scope_reconciliation_effects_resume_legacy_review_count_block(),
        connection, repo_path, state_dir, repo, plan,
    )


def resume_plan_blocks(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    base: str,
    config_path: Path | None = None,
    verify_timeout_seconds: int | None = None,
) -> bool:
    return scope_reconciliation.resume_plan_blocks(_scope_reconciliation_effects_resume_plan_blocks(), connection, repo_path, state_dir, repo, base, config_path, verify_timeout_seconds)


def _block_closed_pull_request(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
) -> None:
    error = "PR이 Merge 없이 닫혔습니다."
    mirrored = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    clear_projection = bool(
        mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
        and mirrored["plan_path"] == plan["path"]
    )
    with runtime_state._transaction(connection):
        _final_block_recovery_decision(
            connection, repo, plan, "PR_OPEN",
            BlockReasonCode.PULL_REQUEST_CLOSED, error,
            str(head_oid or ""), None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=BlockReasonCode.PULL_REQUEST_CLOSED.value,
            evidence=error,
            release_fingerprint=str(head_oid or "") or None,
            plan_values={
                "pr_url": url,
                "pr_head_oid": head_oid,
            },
            clear_projection=clear_projection,
        )


def _resume_reopened_pull_request(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
    *,
    recovery: bool,
    conflict_oids: tuple[str, str] | None = None,
) -> None:
    if conflict_oids is not None and not recovery:
        raise ValueError("PR 충돌 복구 OID는 복구 전이에만 기록할 수 있습니다.")
    phase = "WORKING" if recovery else "PR_OPEN"
    status = "IMPLEMENTING" if recovery else "PR_OPEN"
    last_error = REBASE_IN_PROGRESS_ERROR if recovery else None
    execplan_values: dict[str, object] = {
        "status": status,
        "blocked_reason": None,
        "blocked_fingerprint": None,
        "blocked_worktree_fingerprint": None,
        "blocked_review_fingerprint": None,
        "pr_url": url,
        "pr_head_oid": head_oid,
        "last_error": last_error,
    }
    project_values: dict[str, object] = {
        "phase": phase,
        "pr_url": url,
        "pr_head_oid": head_oid,
        "last_error": last_error,
    }
    if recovery:
        for values in (execplan_values, project_values):
            values.update(
                validated_base_sha=None,
                verify_result=None,
                review_result=None,
            )
            if conflict_oids is not None:
                values.update(
                    pr_conflict_recovery=1,
                    pr_conflict_head_oid=conflict_oids[0],
                    pr_conflict_base_oid=conflict_oids[1],
                )
    with runtime_state._transaction(connection):
        mirrored = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        repository = runtime_state.RuntimeStateRepository(connection)
        if (mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
                and mirrored["plan_path"] == plan["path"]):
            projection = project_values if recovery else _clear_active_values()
            repository.transition(
                repo,
                str(plan["plan_id"]),
                plan_values=execplan_values,
                project_values=projection,
                enqueue=False,
            )
        else:
            repository.update_plan(
                repo,
                str(plan["plan_id"]),
                plan_values=execplan_values,
                enqueue=False,
            )
        scoped_blocking.clear_plan_block(
            connection, repo, str(plan["plan_id"])
        )
        resumed = notifications.record_work_start(
            connection, repo, "PR handling", plan_id=str(plan["plan_id"]),
        )
        notifications.enqueue_current_project_events(connection, repo)
    if resumed:
        operation_log(f"차단 해제 후 PR 처리 재개: {plan['plan_id']}")

def reconcile_existing_pull_request(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> int | None:
    return pull_request_recovery.reconcile_existing_pull_request(_pull_request_recovery_effects_reconcile_existing_pull_request(), connection, repo_path, state_dir, repo, project, pull_request)


def resume_pr_conflict_rebase(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    project: sqlite3.Row,
    worktree: Path,
    branch: str,
) -> int | None:
    return pull_request_recovery.resume_pr_conflict_rebase(_pull_request_recovery_effects_resume_pr_conflict_rebase(), connection, repo_path, repo, project, worktree, branch)


_TARGETED_TEST_FAILURE = re.compile(
    r"(?i)(?<![\w])(?:FAIL(?:ED|URES?)?|ERRORS?|실패"
    r"|(?:did|does|do|has|have|had|was|were|is|are)\s+not\s+pass(?:ed)?"
    r"|(?:not|never|didn't|doesn't|don't)\s+pass(?:ed)?)(?![\w])"
)
_TARGETED_TEST_EXCEPTION_FAILURE = re.compile(
    r"(?m)(?<![\w])[A-Z][A-Za-z0-9_]*(?:Error|Exception)"
    r"(?=[ \t]*(?::|발생|occurred\b|(?:was[ \t]+)?raised\b|$))"
)
_TARGETED_TEST_SUCCESS = re.compile(
    r"(?i)(?<![\w])(?:PASS(?:ED)?|SUCCESS|OK|성공|통과)(?![\w])"
)
_TARGETED_TEST_COMMAND = re.compile(
    r"(?mi)^[ \t]*(?:[-*][ \t]+)?"
    r"(?P<label>FAILED_COMMAND|RERUN):[ \t]*(?P<command>\S.*?)?[ \t]*$"
)


def _targeted_test_failure_matches(evidence: str) -> list[re.Match[str]]:
    failures = []
    command_spans = [
        (match.start(), match.end())
        for match in _TARGETED_TEST_COMMAND.finditer(evidence)
    ]
    candidates = sorted(
        (
            *_TARGETED_TEST_FAILURE.finditer(evidence),
            *_TARGETED_TEST_EXCEPTION_FAILURE.finditer(evidence),
        ),
        key=lambda match: (match.start(), match.end()),
    )
    for match in candidates:
        if any(start <= match.start() < end for start, end in command_spans):
            continue
        prefix = evidence[max(0, match.start() - 32):match.start()]
        count = re.search(
            r"(?i)(?<![\w])(\d+)(?:\s+(?:tests?|cases?))?\s*$",
            prefix,
        )
        if count is not None and int(count.group(1)) == 0:
            continue
        if re.search(r"(?i)\b(?:no|without)\s+$", prefix):
            continue
        failures.append(match)
    return failures


def _has_unresolved_targeted_test_failure(evidence: str) -> bool:
    commands = list(_TARGETED_TEST_COMMAND.finditer(evidence))
    failures = _targeted_test_failure_matches(evidence)
    if not failures:
        return bool(commands)
    recovery_rerun = None

    for index, failure in enumerate(failures):
        prior_commands = [
            match
            for match in commands
            if match.end() <= failure.start()
        ]
        if not prior_commands:
            return True
        failed_run = prior_commands[-1]
        if (
            index == 0
            and failed_run.group("label").upper() != "FAILED_COMMAND"
        ):
            return True
        failed_command = " ".join(
            (failed_run.group("command") or "").split()
        )
        if not failed_command:
            return True

        subsequent_commands = [
            match
            for match in commands
            if match.start() >= failure.end()
        ]
        if not subsequent_commands:
            return True
        rerun = subsequent_commands[0]
        rerun_command = " ".join((rerun.group("command") or "").split())
        if (
            rerun.group("label").upper() != "RERUN"
            or rerun_command != failed_command
        ):
            return True
        recovery_rerun = rerun

    if recovery_rerun is None:
        return True
    if commands[-1].start() != recovery_rerun.start():
        return True
    return (
        _TARGETED_TEST_SUCCESS.search(evidence, recovery_rerun.end())
        is None
    )


def parse_worker_result(message: str, plan: dict[str, object]) -> WorkerPublication:
    """Parse and validate the Worker final-result contract before publishing."""
    match = re.search(
        r"(?ms)^COMMIT_TITLE:[ \t]*(?P<title>[^\r\n]*)\r?\nPR_SUMMARY:[ \t]*(?P<summary>.*?)\r?\nTARGETED_TESTS:\s*(?P<tests>.+)\Z",
        message.strip(),
    )
    if not match:
        title_line = re.search(r"(?m)^COMMIT_TITLE:.*$", message)
        if title_line and not re.match(r"\r?\nPR_SUMMARY:", message[title_line.end():]):
            raise WorkerResultError("Worker commit 제목은 한 줄이어야 합니다.")
        raise WorkerResultError(
            "Worker 결과에는 COMMIT_TITLE, PR_SUMMARY, TARGETED_TESTS 형식이 모두 필요합니다."
        )

    title = match.group("title").strip()
    summary = match.group("summary").strip()
    test_evidence = match.group("tests").strip()
    if not title:
        raise WorkerResultError("Worker commit 제목이 비어 있습니다.")
    if not summary:
        raise WorkerResultError("Worker PR Summary가 비어 있습니다.")
    if not test_evidence:
        raise WorkerResultError("Worker targeted unit test 증거가 비어 있습니다.")
    if _has_unresolved_targeted_test_failure(test_evidence):
        raise WorkerResultError(
            "Worker가 해결되지 않은 targeted unit test 실패를 보고했습니다."
        )
    no_test_reason = reviewer_targeted_test_reason(test_evidence)
    if _NO_RUNNABLE_TARGETED_TEST.search(test_evidence) and not no_test_reason:
        raise WorkerResultError(
            "Worker targeted unit test 미실행 사유에서 성공 내역을 제외하고 구체적인 이유를 보고해야 합니다."
        )
    if not (_TARGETED_TEST_SUCCESS.search(test_evidence) or no_test_reason):
        raise WorkerResultError(
            "Worker targeted unit test 통과 결과 또는 실행하지 못한 구체적인 사유가 필요합니다."
        )

    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    normalized = " ".join(title.split()).casefold().rstrip(" .,:;!?…")
    generic_title_words = {
        "implement", "implementation", "implementations", "change", "changes", "code",
        "update", "updates", "fix", "fixes", "the", "work", "feature", "features",
        "변경", "사항", "구현",
    }
    prohibited = {
        " ".join(plan_title.split()).casefold().rstrip(" .,:;!?…"),
        f"implement {plan_id}".casefold(),
        f"implement {plan_id}: {plan_title}".casefold(),
        f"implement {plan_title}".casefold(),
    }
    if (
        (bool(normalized.split()) and set(normalized.split()) <= generic_title_words)
        or normalized in prohibited
        or re.match(rf"implement\s+(?:execplan\s+)?{re.escape(plan_id.casefold())}\b", normalized)
    ):
        raise WorkerResultError(
            "Worker commit 제목은 ExecPlan 제목이나 일반적인 변경 문구가 아니라 실제 변경을 설명해야 합니다."
        )
    return WorkerPublication(title, summary, test_evidence)


_NO_RUNNABLE_TARGETED_TEST = re.compile(
    r"(?:"
    r"(?:(?:실행 가능한|적용할|관련)\s+)*(?:targeted\s+)?unit\s+tests?(?:가|를|는)?\s+"
    r"(?:없(?:어|어서|었습니다|습니다|다)"
    r"|(?:실행|수행)(?:할\s+수\s+없|하지\s+(?:않았|못했))(?:습니다|다))"
    r"|no runnable targeted (?:unit )?tests?"
    r")",
    re.IGNORECASE,
)
_TARGETED_TEST_METADATA = re.compile(
    r"(?i)(?<![\w])(?:[\w./-]+\s*:\s*)?"
    r"(?:\d+\s+tests?\s+)?"
    r"(?:PASS(?:ED)?|SUCCESS|OK|성공|통과)(?![\w])(?:\s*\([^)]*\))?"
    r"|(?i:(?<![\w])(?:FAILED_COMMAND|RERUN):)"
    r"|(?i:(?<![\w])(?:python\d*(?:\.\d+)?\s+-m\s+unittest|pytest|npm\s+test)\b)"
)
_INLINE_TEST_COMMAND = re.compile(
    r"(?i)(?<![\w])(?:python\d*(?:\.\d+)?\s+-m\s+unittest|pytest|npm\s+test)\b"
)


def reviewer_targeted_test_reason(test_evidence: str) -> str | None:
    """Return only an explicit exceptional no-test disposition for review.

    Successful test prose is deliberately not interpreted: the validated Worker
    result and verify checkpoint are the program's evidence for entering Review.
    """
    for line in test_evidence.splitlines():
        for clause in re.split(r"[;|]", line):
            normalized = " ".join(clause.split())
            match = _NO_RUNNABLE_TARGETED_TEST.search(normalized)
            if match is None:
                continue
            if any(command.start() < match.start()
                   for command in _INLINE_TEST_COMMAND.finditer(normalized)):
                continue
            metadata = list(_TARGETED_TEST_METADATA.finditer(normalized))
            start = max(
                (item.end() for item in metadata if item.end() <= match.start()),
                default=0,
            )
            end = min(
                (item.start() for item in metadata if item.start() >= match.end()),
                default=len(normalized),
            )
            reason = normalized[start:end].lstrip(" :;|,.-").rstrip(" :;|,")
            reason_match = _NO_RUNNABLE_TARGETED_TEST.search(reason)
            if reason_match and (
                reason[:reason_match.start()].strip(" :;|,.-")
                or reason[reason_match.end():].strip(" :;|,.-")
            ):
                return reason[:600]
    return None

def immutable_github_blob_url(origin_url: str, commit_sha: str, relative_path: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
        raise RuntimeError("계획 commit SHA가 유효하지 않습니다.")
    repository = github_repository(origin_url)
    encoded_path = quote(relative_path, safe="/")
    return f"https://github.com/{repository}/blob/{commit_sha}/{encoded_path}"


def execplan_purpose(plan_text: str) -> str:
    body = re.sub(r"(?s)\A---.*?---\s*", "", plan_text, count=1)
    match = re.search(r"(?ms)^## Purpose\s*\n+(.*?)(?=^## |\Z)", body)
    if not match or not match.group(1).strip():
        raise RuntimeError("ExecPlan Purpose를 찾을 수 없습니다.")
    return " ".join(match.group(1).split())


def published_execplan(plan_path: str, plan_text: str) -> dict[str, object]:
    """Read metadata from the immutable ExecPlan commit without live source checks."""
    plan = parse_frontmatter(plan_text)
    required = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    if set(plan) not in {frozenset(required), frozenset(required | {"priority"})}:
        raise RuntimeError("게시된 ExecPlan frontmatter가 올바르지 않습니다.")
    if "priority" not in plan:
        plan["priority"] = "normal"
    if plan["priority"] not in PRIORITY_ORDER:
        raise RuntimeError("게시된 ExecPlan priority가 유효하지 않습니다.")
    plan_id = plan["id"]
    title = plan["title"]
    if not isinstance(plan_id, str) or not isinstance(title, str):
        raise RuntimeError("게시된 ExecPlan ID 또는 제목이 유효하지 않습니다.")
    if Path(plan_path).name != f"[{plan_id}] {title}.md":
        raise RuntimeError("게시된 ExecPlan 경로가 메타데이터와 일치하지 않습니다.")
    sources = plan["sources"]
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("게시된 ExecPlan source가 하나 이상 필요합니다.")
    for source in sources:
        if not isinstance(source, str) or not (
            source == "docs/PRODUCT.md"
            or re.fullmatch(r"docs/requirements/[^/\\]+\.md", source)
        ):
            raise RuntimeError(f"게시된 ExecPlan source가 유효하지 않습니다: {source}")
    dependencies = plan["depends_on"]
    if not isinstance(dependencies, list):
        raise RuntimeError("게시된 ExecPlan depends_on이 유효하지 않습니다.")
    if any(
        not isinstance(dependency, str) or not re.fullmatch(r"G-\d{3,}", dependency)
        for dependency in dependencies
    ):
        raise RuntimeError("게시된 ExecPlan depends_on ID가 유효하지 않습니다.")
    if plan["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("게시된 ExecPlan complexity가 유효하지 않습니다.")
    if plan["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("게시된 ExecPlan risk가 유효하지 않습니다.")
    return plan


def _requirement_archive_tree(
    repo_path: Path,
    revision: str,
) -> dict[bytes, tuple[bytes, bytes]]:
    try:
        return _git_tree_entries(repo_path, revision)
    except GitLockError:
        raise
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision의 Requirement/ExecPlan 이력을 확인할 수 없습니다: "
            f"{revision} ({error})"
        ) from error


def _stored_requirement_sources(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    try:
        return requirement_archive.stored_requirement_sources(rows)
    except requirement_archive.ArchiveSnapshotError as error:
        raise RequirementArchiveError(str(error)) from error


def requirement_archive_sources(
    connection: sqlite3.Connection,
    repo: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT plan_id, sources FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    return _stored_requirement_sources(rows)


def _later_published_execplan(
    repo_path: Path,
    revision: str,
    row: sqlite3.Row,
) -> dict[str, object] | None:
    plan_id = str(row["plan_id"])
    plan_sha = str(row["publication_sha"] or row["plan_sha"] or "")
    if not plan_sha:
        return None
    ancestry = git(
        repo_path,
        "merge-base",
        "--is-ancestor",
        revision,
        plan_sha,
        check=False,
    )
    if ancestry.returncode == 1:
        return None
    if ancestry.returncode != 0:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: "
            f"{ancestry.stdout[-2000:]}"
        )
    path = str(row["publication_path"] or row["path"])
    try:
        plan_text = git_bytes(
            repo_path,
            "show",
            f"{plan_sha}:{path}",
        ).decode("utf-8")
        plan = published_execplan(path, plan_text)
    except (UnicodeDecodeError, RuntimeError) as error:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: {error}"
        ) from error
    if plan["id"] != plan_id:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: ID 불일치"
        )
    return plan


def _revision_execplan_catalog(
    repo_path: Path,
    revision: str,
    *,
    archive_conflict_plan_ids: frozenset[str] = frozenset(),
) -> dict[str, dict[str, object]]:
    try:
        snapshot = revision_execplan_snapshot(
            repo_path,
            revision,
            archive_conflict_plan_ids=archive_conflict_plan_ids,
        )
    except GitLockError:
        raise
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision의 ExecPlan source 관계를 확정할 수 없습니다: {error}"
        ) from error
    return {
        plan_id: {
            **entry.metadata(),
            "path": entry.path,
            "content_hash": entry.content_hash,
        }
        for plan_id, entry in snapshot.all.items()
    }


def requirement_archive_snapshot(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    current_plan_id: str,
    *,
    allow_future_publication: bool = False,
) -> requirement_archive.ArchiveSnapshot:
    """Read candidate scope from one immutable Git and SQLite view."""

    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
    ).fetchall()
    states = {str(row["plan_id"]): row for row in rows}
    if current_plan_id not in states:
        raise RequirementArchiveError(
            f"현재 ExecPlan의 SQLite 상태 없음: {current_plan_id}"
        )
    for plan_id, row in states.items():
        if str(row["status"]) not in KNOWN_EXECPLAN_STATUSES:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )
    catalog = _revision_execplan_catalog(
        repo_path,
        revision,
        archive_conflict_plan_ids=frozenset(
            plan_id for plan_id, row in states.items()
            if str(row["status"]) == "MERGED"
        ),
    )
    historical_rows = connection.execute(
        "SELECT plan_id, merge_confirmed, diagnostic_code FROM execplan_catalog_history "
        "WHERE repo = ? AND currently_archived = 1",
        (repo,),
    ).fetchall()
    unverified_historical_ids = {
        str(row["plan_id"])
        for row in historical_rows
        if not int(row["merge_confirmed"]) or row["diagnostic_code"]
    }
    state_ids = set(states)
    if allow_future_publication:
        for plan_id in sorted(state_ids - set(catalog)):
            later_plan = _later_published_execplan(
                repo_path,
                revision,
                states[plan_id],
            )
            if later_plan is not None:
                catalog[plan_id] = later_plan
    try:
        _validate_dependency_graph(catalog)
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"ExecPlan source 관계를 확정할 수 없습니다: {error}"
        ) from error

    catalog_ids = set(catalog)
    missing_states = catalog_ids - state_ids
    unexpected_missing_states = missing_states - unverified_historical_ids
    if unexpected_missing_states or state_ids - catalog_ids:
        missing_plans = sorted(state_ids - catalog_ids)
        details = []
        if unexpected_missing_states:
            details.append(
                "SQLite 상태 없음: "
                + ", ".join(sorted(unexpected_missing_states))
            )
        if missing_plans:
            details.append("Git ExecPlan 없음: " + ", ".join(missing_plans))
        raise RequirementArchiveError(
            "ExecPlan source 관계 또는 상태를 확정할 수 없습니다 ("
            + "; ".join(details)
            + ")."
        )

    tree = _requirement_archive_tree(repo_path, revision)
    try:
        snapshot = requirement_archive.calculate(
            base_oid=revision,
            catalog=catalog,
            states={plan_id: str(row["status"]) for plan_id, row in states.items()},
            active_requirements=tuple(
                path.decode("utf-8") for path in tree if _is_active_requirement(path)
            ),
            archived_requirements=frozenset(
                path.decode("utf-8") for path in tree
                if path.startswith(b"docs/requirements/archive/")
            ),
            current_plan_id=current_plan_id,
            unverified_historical_ids=frozenset(unverified_historical_ids),
            known_statuses=frozenset(KNOWN_EXECPLAN_STATUSES),
            cache_versions={
                plan_id: tuple(
                    row[field] if field in row.keys() else None
                    for field in (
                        "path", "title", "sources", "depends_on",
                        "current_complexity", "current_risk", "priority",
                        "content_hash", "current_revision_sha",
                    )
                )
                for plan_id, row in states.items()
            },
        )
    except requirement_archive.ArchiveCollisionError as error:
        raise RequirementArchiveCollisionError(error.path) from error
    except requirement_archive.ArchiveSnapshotError as error:
        raise RequirementArchiveError(str(error)) from error
    return snapshot


def requirement_archive_candidates(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    current_plan_id: str,
) -> tuple[str, ...]:
    """Compatibility projection of a revision-pinned archive snapshot."""

    states = {
        str(row["plan_id"]): str(row["status"])
        for row in connection.execute(
            "SELECT plan_id, status FROM exec_plans WHERE repo = ?", (repo,)
        )
    }
    if current_plan_id not in states:
        raise RequirementArchiveError(
            f"현재 ExecPlan의 SQLite 상태 없음: {current_plan_id}"
        )
    for plan_id, status in states.items():
        if status not in KNOWN_EXECPLAN_STATUSES:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )
    if not requirement_archive_sources(connection, repo):
        return ()
    return requirement_archive_snapshot(
        connection, repo_path, repo, revision, current_plan_id,
        allow_future_publication=True,
    ).candidates

def requirement_archive_touched_sources(
    repo_path: Path,
    revision: str,
    worktree: Path,
) -> tuple[str, ...]:
    """Find active Requirement paths whose move pair differs from the base tree."""

    tree = _requirement_archive_tree(repo_path, revision)
    touched = []
    for path in sorted(tree):
        if not _is_active_requirement(path):
            continue
        source = path.decode("utf-8")
        archived = requirement_archive_path(source)
        if archived.encode("utf-8") in tree:
            continue
        if not (worktree / source).is_file() or (worktree / archived).exists():
            touched.append(source)
    return tuple(touched)


def _requirement_archive_blob(
    repo: Path,
    revision: str,
    archived: str,
) -> bytes:
    try:
        output = git_bytes(
            repo,
            "ls-tree",
            "-z",
            revision,
            "--",
            archived,
        )
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Requirement archive의 Git tree 상태를 확인할 수 없습니다: "
            f"{archived} ({error})"
        ) from error
    records = [record for record in output.split(b"\0") if record]
    expected_path = archived.encode("utf-8")
    if len(records) == 1:
        metadata, separator, actual_path = records[0].partition(b"\t")
        parts = metadata.split()
    else:
        separator = b""
        actual_path = b""
        parts = []
    if (
        not separator
        or actual_path != expected_path
        or len(parts) != 3
        or parts[0] not in {b"100644", b"100755"}
        or parts[1] != b"blob"
    ):
        raise RequirementArchiveError(
            f"Requirement archive가 {revision}에 일반 blob으로 추적되지 않았습니다: "
            f"{archived}"
        )
    return parts[2]


def requirement_archive_move_set(
    repo_path: Path,
    revision: str,
    worktree: Path,
    *,
    tracked_revision: str | None = None,
) -> tuple[str, ...]:
    """Return content-preserving Requirement moves relative to one base revision."""

    tree = _requirement_archive_tree(repo_path, revision)
    def ignore_status(path: str) -> tuple[int, str]:
        result = git(
            worktree, "check-ignore", "--quiet", "--", path, check=False
        )
        return result.returncode, result.stdout

    def tracked_contents(path: str) -> bytes:
        object_id = _requirement_archive_blob(worktree, tracked_revision, path)
        return git_bytes(worktree, "cat-file", "blob", object_id.decode("ascii"))

    try:
        return requirement_archive.validated_moves(
            tree, worktree,
            blob_contents=lambda object_id: git_bytes(
                repo_path, "cat-file", "blob", object_id.decode("ascii")
            ),
            tracked_contents=tracked_contents if tracked_revision is not None else None,
            ignore_status=ignore_status if tracked_revision is None else None,
        )
    except requirement_archive.ArchiveCollisionError as error:
        raise RequirementArchiveCollisionError(error.path) from error
    except requirement_archive.ArchiveMoveError as error:
        raise RequirementArchiveError(str(error)) from error

def requirement_archive_reconciliation_error(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
    *,
    revision: str | None = None,
) -> str | None:
    if expected == actual:
        return None
    return (
        "Requirement archive 이동 집합이 최신 eligibility와 다릅니다.\n"
        + (f"기준 revision: {revision}\n" if revision else "")
        + f"기대: {json.dumps(expected, ensure_ascii=False)}\n"
        f"현재: {json.dumps(actual, ensure_ascii=False)}\n"
        "기대 집합만 내용 변경 없이 docs/requirements/archive/로 이동하라."
    )


def build_pr_body(
    worker: WorkerPublication,
    plan: dict[str, object],
    plan_path: str,
    plan_text: str,
    origin_url: str,
    plan_sha: str,
) -> str:
    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    plan_link = immutable_github_blob_url(origin_url, plan_sha, plan_path)
    sources = plan["sources"]
    assert isinstance(sources, list)
    source_lines = [
        f"- [{source}]({immutable_github_blob_url(origin_url, plan_sha, str(source))})"
        for source in sources
    ]
    return "\n".join(
        [
            "## Summary",
            worker.summary,
            "",
            "## ExecPlan",
            f"- [{plan_id}: {plan_title}]({plan_link})",
            f"- Purpose: {execplan_purpose(plan_text)}",
            "",
            "## Sources",
            *source_lines,
        ]
    )


class _PlanningStatePorts:
    """Bind coordinator state transitions to the G-047 repositories."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        runtime_repository: runtime_state.RuntimeStateRepository,
        maintenance_repository: maintenance_state.MaintenanceRepository,
        state_dir: Path,
    ):
        self._connection = connection
        self._runtime_repository = runtime_repository
        self._maintenance_repository = maintenance_repository
        self._state_dir = state_dir

    def planning(self, repo: str) -> runtime_state.PlanningSnapshot:
        return self._runtime_repository.planning(repo)

    def maintenance_cycle(
        self, repo: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.get(repo)

    def update(self, repo: str, **values: object) -> None:
        update_project(self._connection, repo, **values)

    def transaction(self):
        return runtime_state._transaction(self._connection)

    def advance_maintenance_cycle(
        self, repo: str, base_sha: str
    ) -> maintenance_state.MaintenanceCycle:
        return _advance_maintenance_cycle_after_merges(
            self._connection, repo, base_sha
        )

    def schedule(self, repo: str) -> CatalogScheduleDecision:
        return catalog_schedule_decision(self._connection, repo)

    def pending_document_approval(
        self, repo: str, repository: Path, base_branch: str,
    ) -> bool:
        if not is_loop_manager_repository(repository):
            return False
        from loop_manager_internal.documents import document_drafts
        return bool(document_drafts.DocumentDraftRepository(
            self._connection,
        ).unplanned_approved(repo, repository, base_branch))

    def maintenance_due(
        self, repo: str, interval: int, *, self_repository: bool = False,
        completion_candidate: bool = False,
    ) -> maintenance_state.ReviewDue:
        return self._maintenance_repository.file_assessment_due(
            repo, interval, self_repository=self_repository,
            completion_candidate=completion_candidate,
        )

    def begin_maintenance_assessment(
        self,
        repo: str,
        interval: int,
        base_sha: str,
        *,
        include_general_planning: bool,
        self_repository: bool = False,
        completion_candidate: bool = False,
    ) -> tuple[maintenance_state.MaintenanceCycle, bool]:
        return self._maintenance_repository.begin_assessment_if_due(
            repo,
            interval,
            base_sha,
            include_general_planning=include_general_planning,
            self_repository=self_repository,
            completion_candidate=completion_candidate,
        )

    def settle_empty_stabilization_assessment(
        self,
        repo: str,
        interval: int,
        *,
        source_sha: str,
        repository_path: str,
        base_branch: str,
        inventory: maintenance_file_inventory.SourceInventory,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.settle_empty_stabilization_assessment(
            repo,
            interval,
            source_sha=source_sha,
            repository_path=repository_path,
            base_branch=base_branch,
            inventory=inventory,
        )

    def record_full_assessment(
        self, repo: str, *, expected_revision: int, source_sha: str, result: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.record_full_assessment(
            repo, expected_revision=expected_revision, source_sha=source_sha,
            result=result,
        )

    def restart_stale_maintenance(
        self,
        repo: str,
        expected: maintenance_state.MaintenanceCycle,
        base_sha: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.restart_stale_planning_context(
            repo,
            expected=expected,
            new_base_sha=base_sha,
        )

    def activate_ready(self, repo: str, base_sha: str) -> sqlite3.Row | None:
        return activate_ready_execplan(
            self._connection, repo, self._state_dir, base_sha
        )

    def preserve_diagnostics(
        self, repo: str, snapshot: runtime_state.PlanningSnapshot
    ) -> None:
        preserve_planning_diagnostics(self._state_dir, repo, snapshot)

    def clear_active(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None:
        clear_active(
            self._connection,
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )

    def clear_active_in_transaction(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None:
        _clear_active_in_current_transaction(
            self._connection,
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )

    def clear_validation(self, repo: str) -> None:
        clear_planning_validation_state(self._connection, repo)

    def compact_catalog(self, repo: str) -> str:
        return compact_execplan_catalog(self._connection, repo)

    def execplan_rows(self, repo: str) -> list[sqlite3.Row]:
        return prerequisite_validation_rows(self._connection, repo)

    def transition_maintenance(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        next_phase: maintenance_state.MaintenancePhase,
        prerequisite_plan_ids: tuple[str, ...],
        include_general_planning: bool,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.transition(
            repo,
            expected_phase=expected_phase,
            expected_revision=expected_revision,
            next_phase=next_phase,
            prerequisite_plan_ids=prerequisite_plan_ids,
            include_general_planning=include_general_planning,
        )

    def advance_success_baseline(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        remote_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.advance_success_baseline(
            repo,
            expected_phase=expected_phase,
            expected_revision=expected_revision,
            remote_commit=remote_commit,
        )

    def file_assessment_evidence(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
    ) -> maintenance_file_publication.AssessmentEvidence:
        return maintenance_file_publication.collect(
            self._connection, repo, repository, base_branch,
            cycle_revision, source_sha,
        )

    def complete_file_assessment(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
        evidence: maintenance_file_publication.AssessmentEvidence,
    ) -> None:
        with runtime_state._transaction(self._connection):
            maintenance_file_publication.promote(
                self._connection, repo, repository, base_branch, source_sha,
                evidence,
            )
            self._maintenance_repository.advance_file_assessment_success_baseline(
                repo, expected_revision=cycle_revision, source_sha=source_sha,
                expected_merged_count=evidence.merged_count,
                bump_revision=False,
            )

    def recover_maintenance_batch(
        self, repo: str, worktree: Path, base: str
    ) -> maintenance_state.MaintenanceCycle:
        return recover_maintenance_planning_batch(
            self._connection, repo, worktree, base
        )

    def record_maintenance_intent(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return record_maintenance_publication_intent(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
        )

    def publish_maintenance_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return publish_maintenance_planning_batch(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
        )

    def register_execplans(
        self,
        repo: str,
        worktree: Path,
        plans: list[tuple[str, dict[str, object]]],
        plan_commit: str,
        fingerprint: str,
    ) -> None:
        register_execplans(
            self._connection,
            repo,
            worktree,
            plans,
            plan_commit,
            fingerprint,
        )

    def general_publication(
        self, repo: str
    ) -> GeneralPlanningPublication | None:
        return general_planning_publication(self._connection, repo)

    def recover_general_batch(
        self, repo: str, worktree: Path, base: str
    ) -> str:
        return recover_general_planning_batch(
            self._connection, repo, worktree, base
        )

    def record_general_intent(
        self,
        repo: str,
        worktree: Path,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> GeneralPlanningPublication:
        return record_general_planning_publication_intent(
            self._connection,
            repo,
            worktree,
            validated_result,
            plan_commit,
            specification_fingerprint,
            planning_fingerprint,
        )

    def publish_general_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> None:
        publish_general_planning_batch(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
            specification_fingerprint,
            planning_fingerprint,
        )


class _PlanningGitPorts:
    """Bind coordinator Git operations to the managed-worktree helpers."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        repo_path: Path,
        state_dir: Path,
    ):
        self._connection = connection
        self._repo_path = repo_path
        self._state_dir = state_dir

    def run(
        self, repo: Path, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if len(arguments) == 3 and arguments[:2] == ("worktree", "remove"):
            workspace = Path(arguments[2])
            validate_managed_worktree(repo, workspace, None)
            _head, _branch, _index, dirty = _capture_local_worktree_state(workspace)
            if dirty or _git_operation_entries(workspace):
                raise WorkspaceOwnershipError(
                    f"planning worktree에 권위 있는 변경이 남았습니다: {workspace}"
                )
            if _ignored_worktree_paths(workspace):
                arguments = ("worktree", "remove", "--force", str(workspace))
        return git(repo, *arguments, check=check)

    def input_fingerprint(self, repo_path: Path, sha: str) -> str:
        return _planning_input_fingerprint(repo_path, sha)

    def specification_fingerprint(self, repo_path: Path, sha: str) -> str:
        return _specification_input_fingerprint(repo_path, sha)

    def requirement_archive_only(
        self, repo_path: Path, old_sha: str, new_sha: str
    ) -> bool:
        return _is_requirement_archive_only_change(repo_path, old_sha, new_sha)

    def validate_workspace_identity(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None:
        try:
            validate_planning_workspace_identity(
                self._state_dir, repo, fingerprint, worktree
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def validate_worktree_ownership(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None:
        try:
            validate_planning_worktree_ownership(
                self._connection,
                self._repo_path,
                self._state_dir,
                repo,
                fingerprint,
                worktree,
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def make_planning_worktree(
        self,
        repo_path: Path,
        worktree: Path,
        base: str,
        *,
        allow_create: bool,
        allow_existing: bool,
        ownership_reserved: bool,
        expected_head: str,
        before_create: Callable[[], None],
    ) -> None:
        try:
            make_planning_worktree(
                repo_path,
                worktree,
                base,
                allow_create=allow_create,
                allow_existing=allow_existing,
                ownership_reserved=ownership_reserved,
                expected_head=expected_head,
                before_create=before_create,
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def changed_files(
        self, worktree: Path, since: str | None = None
    ) -> list[str]:
        return changed_files(worktree, since)

    def validate_managed_worktree(self, worktree: Path) -> None:
        validate_managed_worktree(self._repo_path, worktree, None)


def _ExecPlanArchiveGitPort(
    repo_path: Path, state_dir: Path, repo: str,
) -> archive_git.ExecPlanArchiveGitPort:
    return archive_git.ExecPlanArchiveGitPort(
        repo_path, state_dir, repo, effects=_archive_git_effects,
    )


def dispatch_execplan_archive_housekeeping(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    base_branch: str,
    *,
    catalog_refresh: project_catalog_refresh.ProjectCatalogRefresh | None = None,
    archive_safety: Callable[[], bool] | None = None,
) -> execplan_archive.ArchiveDispatchOutcome:
    """Assemble the archive service and map its result to operator logging."""

    def refresh_candidates(base_sha: str) -> bool:
        if catalog_refresh is not None:
            return (catalog_refresh.refresh(base_sha) == base_sha
                    and (archive_safety is None or archive_safety()))
        result = reconcile_execplan_catalog(
            connection, repo_path, repo, base_branch
        )
        if result.kind is not execplan_catalog_state.ReconciliationKind.RECONCILED:
            return False
        current = git(
            repo_path, "rev-parse",
            f"refs/remotes/origin/{base_branch}^{{commit}}",
        ).stdout.strip()
        return result.revision_sha == base_sha and current == base_sha

    git_port = _ExecPlanArchiveGitPort(repo_path, state_dir, repo)

    def apply_catalog(base_sha, commit_oid, candidates):
        assert catalog_refresh is not None
        expected = catalog_refresh.repository.capture(repo)
        moves = tuple(execplan_archive.ArchiveMove(
            item.plan_id, item.active_path, item.archive_path, item.content_hash, item.revision_sha,
        ) for item in candidates)
        operation_id = execplan_archive.archive_operation_id(base_sha, moves)
        stored = archive_repository.prepared_operation(repo, operation_id)
        if stored is None or stored.commit_oid != commit_oid:
            return False
        prepared = execplan_archive.PreparedArchive(
            operation_id, base_sha, stored.final_hash, commit_oid,
        )
        if not git_port.commit_matches(prepared, moves):
            return False
        return catalog_refresh.apply_archive(
            base_sha, commit_oid, candidates, expected=expected,
        )

    archive_repository = execplan_archive_state.ExecPlanArchiveRepository(
        connection, apply_catalog=apply_catalog if catalog_refresh else None,
    )
    outcome = execplan_archive.ExecPlanArchiveDispatcher(
        archive_repository,
        git_port,
        refresh_candidates=refresh_candidates,
    ).dispatch(repo, base_branch)
    if outcome.commit_oid:
        operation_print(
            "MERGED ExecPlan archive 게시 완료: "
            + ", ".join(outcome.archived_plan_ids)
        )
    for plan_id, reason in outcome.pending:
        operation_print(
            f"ExecPlan archive 대기 ({plan_id}): {reason}",
            file=sys.stderr,
        )
    return outcome


def _execplan_archive_housekeeping_safe(
    connection: sqlite3.Connection,
    repo: str,
    validated_agent_scope: object | None,
    *,
    maintenance_assessment_enabled: bool,
    refactoring_review_interval: int,
) -> bool:
    """Allow base mutation only between active project lifecycles."""

    if scoped_blocking.project_block(connection, repo) is not None:
        return False
    current = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if (
        current is None
        or current["phase"] is not None
        or runtime_state.planning_owns_execution_slot(current)
        or active_execplan(connection, repo) is not None
        or validated_agent_scope is not None
    ):
        return False
    maintenance_repository = maintenance_state.MaintenanceRepository(
        connection
    )
    maintenance_cycle = maintenance_repository.get(repo)
    if maintenance_cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        return False
    if maintenance_assessment_enabled:
        decision = catalog_schedule_decision(connection, repo)
        if (
            not decision.has_waiting
            and maintenance_repository.file_assessment_due(
                repo, refactoring_review_interval,
                self_repository=is_loop_manager_repository(Path(repo)),
            ).required
        ):
            return False
    return True


class _PlanningAgentPorts:
    """Bind Planner CLI and Git scope validation to the coordinator."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        context: PlanningContext,
        planner_mutex: threading.Lock | None,
    ):
        self._connection = connection
        self._context = context
        self._planner_mutex = planner_mutex

    def begin_scope(self, role: str, worktree: Path) -> GitSnapshot:
        return begin_agent_scope(
            self._connection, self._context.repo, worktree, role,
            state_dir=self._context.state_dir,
        )

    def run_planner(
        self,
        worktree: Path,
        prompt: str,
        session_id: str | None,
        on_session_started: Callable[[str], None],
    ) -> PlanningAgentResult:
        context = self._context
        guard = self._planner_mutex or contextlib.nullcontext()
        with guard:
            with self._connection:
                resumed = notifications.record_work_start(
                    self._connection, context.repo, "Planner",
                )
            if resumed:
                operation_log(f"차단 해제 후 Planner 작업 재개: {context.repo}")
            result = run_agent_with_retries(
                TrackedAgentCall(lambda current_session_id, _session_recovery, invocation: run_agent(
                    context.provider,
                    worktree,
                    context.state_dir,
                    context.config_path,
                    "planner",
                    prompt,
                    read_only=False,
                    persist_session=True,
                    session_id=current_session_id,
                    on_session_started=on_session_started,
                    connection=self._connection,
                    invocation=invocation,
                ), AttemptSequence(context.repo, None)),
                session_id=session_id,
            )
        kind = {
            AgentResultKind.SUCCESS: PlanningAgentResultKind.SUCCESS,
            AgentResultKind.TRANSIENT_FAILURE: (
                PlanningAgentResultKind.TRANSIENT_FAILURE
            ),
            AgentResultKind.USAGE_LIMIT: PlanningAgentResultKind.USAGE_LIMIT,
            AgentResultKind.PERMANENT_CONFIGURATION: (
                PlanningAgentResultKind.PERMANENT_CONFIGURATION
            ),
        }[result.result_kind]
        message = result.message
        if result.code and kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
            message = f"{TRANSIENT_AGENT_ERROR_PREFIX} {message}"
        elif result.code and kind is PlanningAgentResultKind.USAGE_LIMIT:
            message = f"{USAGE_LIMIT_ERROR_PREFIX} {message}"
        return PlanningAgentResult(
            result.code,
            message,
            result.session_id,
            kind,
        )

    def finish_scope(
        self, role: str, worktree: Path, before: GitSnapshot
    ) -> PlanningScopeResult:
        after: GitSnapshot | None = None
        try:
            after = capture_git_snapshot(worktree)
            validate_agent_scope(
                role,
                worktree,
                before,
                after,
                external_worktree=self._context.repo_path,
                base_branch=self._context.base_branch,
                connection=self._connection,
            )
            validate_managed_worktree(
                self._context.repo_path, worktree, None
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            return PlanningScopeResult(
                str(error), after.serialize() if after is not None else None
            )
        return PlanningScopeResult()

    def validate_saved_scope(
        self, role: str, worktree: Path, snapshot: str
    ) -> PlanningScopeResult:
        after: GitSnapshot | None = None
        try:
            before = GitSnapshot.deserialize(snapshot)
            after = capture_git_snapshot(worktree)
            before = _reconciled_manager_scope_metadata(
                self._connection, self._context.repo_path, self._context.state_dir,
                self._context.repo, None, before, after,
                _agent_git_resources(self._connection, self._context.repo_path,
                                     worktree, before, role))
            validate_agent_scope(
                role,
                worktree,
                before,
                after,
                external_worktree=self._context.repo_path,
                base_branch=self._context.base_branch,
                connection=self._connection,
            )
            validate_managed_worktree(
                self._context.repo_path, worktree, None
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            return PlanningScopeResult(
                str(error), after.serialize() if after is not None else None
            )
        return PlanningScopeResult()

    def clear_scope(self, repo: str) -> None:
        clear_agent_scope(self._connection, repo)


class _PlanningValidationPorts:
    """Bind prompt and validation helpers without lifecycle decisions."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def maintenance_context_at_revision(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> MaintenancePlanningContext:
        return _maintenance_context_at_revision(
            self._connection, repo, worktree, active_sha, cycle
        )

    def prepare_maintenance_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MaintenancePlanningContext:
        return prepare_maintenance_planning_context(
            self._connection, repo, worktree, active_sha
        )

    def maintenance_change_context(
        self,
        repo_path: Path,
        base: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> str:
        return _maintenance_change_context_text(repo_path, base, cycle)

    def prepare_general_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MutableReadySnapshot:
        return mutable_ready_snapshot(
            self._connection, repo, worktree, active_sha
        )

    def planner_prompt(
        self,
        error: str | None,
        catalog: str,
        mutable_snapshot: MutableReadySnapshot,
    ) -> str:
        prompt = planner_prompt(error, catalog, mutable_snapshot)
        from loop_manager_internal.documents import document_drafts
        repository = Path(mutable_snapshot.repo)
        if is_loop_manager_repository(repository):
            project = self._connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?",
                (mutable_snapshot.repo,),
            ).fetchone()
            if project is not None:
                candidate = document_drafts.DocumentDraftRepository(
                    self._connection,
                ).planning_candidate(
                    mutable_snapshot.repo, repository, str(project["base_branch"]),
                )
                if candidate is not None:
                    prompt += document_publication.planning_instruction(candidate)
        return prompt

    def maintenance_prompt(
        self,
        context: MaintenancePlanningContext,
        catalog: str,
        change_context: str,
        error: str | None,
        *,
        include_general_planning: bool,
    ) -> str:
        return maintenance_planner_prompt(
            context,
            catalog,
            change_context,
            error,
            include_general_planning=include_general_planning,
        )

    def validate_general_result(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        dirty_files: list[str],
        mutable_snapshot: MutableReadySnapshot,
    ) -> ValidatedPlanningResult | None:
        result = validate_planner_result(
            worktree,
            active_sha,
            dirty_files,
            connection=self._connection,
            repo=repo,
            mutable_snapshot=mutable_snapshot,
        )
        from loop_manager_internal.documents import document_drafts
        repository = Path(repo)
        if not is_loop_manager_repository(repository):
            return result
        project = self._connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if project is None:
            return result
        drafts = document_drafts.DocumentDraftRepository(self._connection)
        candidate = drafts.planning_candidate(
            repo, repository, str(project["base_branch"]),
        )
        claims = []
        for path, plan in result.plans if result is not None else ():
            text = document_publication.read_plan_text(worktree / path)
            try:
                claim = document_publication.plan_claim(text)
            except document_drafts.DocumentDraftError as error:
                raise PlannerValidationError(str(error)) from error
            if claim is not None:
                claims.append((text, plan, claim))
        if candidate is not None:
            if (result is None or len(result.plans) != 1 or result.revisions
                or result.deletions or result.publication_paths != (result.plans[0][0],)
                or len(claims) != 1 or claims[0][2] != candidate.draft_id):
                raise PlannerValidationError(
                    "승인 문서 수정본에는 별도 새 ExecPlan 하나만 필요합니다."
                )
            try:
                drafts.assert_current(candidate, repository, str(project["base_branch"]))
                document_publication.validate_document_plan(
                    claims[0][0], claims[0][1], candidate,
                )
            except document_drafts.DocumentDraftError as error:
                drafts.record_publication_error(candidate.draft_id, str(error))
                raise PlannerValidationError(str(error)) from error
        elif claims:
            raise PlannerValidationError("계획할 승인 문서 수정본이 없습니다.")
        return result

    def validate_maintenance_result(
        self,
        context: MaintenancePlanningContext,
        worktree: Path,
        dirty_files: list[str],
        *,
        refactoring_plan_ids: tuple[str, ...],
        allow_general_plans: bool,
    ) -> ValidatedPlanningResult | None:
        return validate_maintenance_planner_result(
            self._connection,
            context,
            worktree,
            dirty_files,
            refactoring_plan_ids=refactoring_plan_ids,
            allow_general_plans=allow_general_plans,
        )

    def validate_publication_base(
        self,
        repo: str,
        repo_path: Path,
        base: str,
        context: MaintenancePlanningContext,
    ) -> None:
        validate_maintenance_publication_base(
            self._connection, repo, repo_path, base, context
        )


class _PlanningEventPorts:
    def log(self, message: str) -> None:
        operation_log(message)

    def info(self, message: str) -> None:
        operation_print(message)

    def error(self, message: str) -> None:
        operation_print(message, file=sys.stderr)


class _PlanningFileReviewPorts:
    """Keep provisional file units outside the success ledger and Planner row."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._repository = maintenance_file_review.ProvisionalReviewRepository(connection)

    def empty_selection_inventory(
        self, repo: str, repository: Path, base: str, source_revision: str,
    ) -> maintenance_file_inventory.SourceInventory | None:
        inventory = maintenance_file_inventory.cached_source_inventory(
            self._connection, repository, base, source_revision,
        )
        selected = maintenance_file_ledger.FileReviewRepository(
            self._connection,
        ).selected_sources(repo, inventory)
        reusable = maintenance_file_ledger.FileReviewRepository(
            self._connection,
        ).reusable_root_reviews(repo, inventory)
        return None if selected or reusable is None or maintenance_file_ledger.root_move_candidates(reusable) else inventory

    def progress(
        self, repo: str, repository: Path, base: str, cycle_revision: int,
        source_revision: str,
    ) -> maintenance_file_review.FileReviewProgress:
        inventory = maintenance_file_inventory.cached_source_inventory(
            self._connection, repository, base, source_revision,
        )
        return self._repository.progress(
            repo, repository, base, cycle_revision, inventory,
        )

    def content(self, repository: Path, base: str, source: maintenance_file_inventory.TrackedSource) -> bytes:
        return maintenance_file_review.source_bytes(repository, base, source)

    def start(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource) -> None:
        self._repository.start(repo, cycle_revision, source)

    def session(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, session_id: str) -> None:
        self._repository.session(repo, cycle_revision, source, session_id)

    def complete(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, result: dict) -> None:
        self._repository.complete(repo, cycle_revision, source, result)

    def fail(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, error: str) -> None:
        self._repository.fail(repo, cycle_revision, source, error)

    def completed_summary(self, repo: str) -> dict:
        return self._repository.completed_summary(repo)


class _PlanningReconciliationPorts:
    """Keep relation evidence provisional until G-086 publication succeeds."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._repository = maintenance_reconciliation.ReconciliationRepository(connection)
        self._synthesis = maintenance_reconciliation.SynthesisRepository(connection)
        self._findings = maintenance_findings.FindingsRepository(connection)
        self._reviews = maintenance_file_review.ProvisionalReviewRepository(connection)

    def progress(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 source_revision: str) -> maintenance_reconciliation.ReconciliationProgress:
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, source_revision)
        reviews = self._reviews.progress(repo, repository, base, cycle_revision, inventory)
        return self._repository.progress(repo, repository, base, cycle_revision, inventory, reviews)

    def synthesis(self, repo: str, cycle: int, revision: str,
                  progress: maintenance_reconciliation.ReconciliationProgress):
        return self._synthesis.prepare(repo, cycle, revision, progress)

    def synthesis_export(self, repo: str, page, progress, directory: Path) -> Path:
        return self._synthesis.export(repo, page, progress, directory)

    def synthesis_session(self, repo: str, page, session_id: str) -> None:
        self._synthesis.session(repo, page, session_id)

    def synthesis_complete(self, repo: str, page, output: str) -> None:
        self._synthesis.complete(repo, page, output)

    def content(self, repository: Path, base: str, source: maintenance_file_inventory.TrackedSource) -> bytes:
        return maintenance_file_review.source_bytes(repository, base, source)

    def start(self, repo: str, cycle_revision: int, revision: str,
              relation: maintenance_reconciliation.Relation) -> None:
        self._repository.start(repo, cycle_revision, revision, relation)

    def session(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
                session_id: str) -> None:
        self._repository.session(repo, cycle_revision, relation, session_id)

    def complete(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
                 result: dict) -> None:
        self._repository.complete(repo, cycle_revision, relation, result)

    def fail(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
             error: str) -> None:
        self._repository.fail(repo, cycle_revision, relation, error)

    def unit(self, repo: str, relation: maintenance_reconciliation.Relation) -> tuple[str | None, str | None]:
        return self._repository.unit(repo, relation)

    def resolution_session(self, repo, unit, session_id):
        self._synthesis.resolution_session(repo, unit, session_id)

    def resolution_complete(self, repo, unit, output):
        self._synthesis.resolution_complete(repo, unit, output)

    def resolution_fail(self, repo, unit, error):
        self._synthesis.resolution_fail(repo, unit, error)

    def _plan_texts(self, repository: Path, revision: str, existing_plans: dict[str, str]) -> dict[str, str]:
        texts = {}
        for raw_path in _git_tree_entries(repository, revision):
            if not re.fullmatch(rb"docs/plans/[^/]+\.md", raw_path):
                continue
            path = raw_path.decode("utf-8")
            text = git_bytes(repository, "show", f"{revision}:{path}").decode("utf-8")
            plan_id = str(parse_frontmatter(text).get("id", ""))
            if plan_id in existing_plans:
                texts[plan_id] = text
        if set(texts) != set(existing_plans):
            raise maintenance_reconciliation.ReconciliationError("고정 비종료 계획의 원문이 누락됐습니다.")
        return texts

    def findings_unit(self, repo, repository, base, cycle, revision, existing_plans, directory):
        progress = self.progress(repo, repository, base, cycle, revision)
        synthesis = self.synthesis(repo, cycle, revision, progress)
        if synthesis.index != synthesis.total:
            raise maintenance_reconciliation.ReconciliationError("발견 수집 전 모든 증거 페이지가 필요합니다.")
        texts = self._plan_texts(repository, revision, existing_plans)
        unit = self._findings.prepare(repo, cycle, revision, synthesis.digest, existing_plans, texts)
        evidence = self.synthesis_export(repo, synthesis, progress, directory)
        manifest = self._findings.export(repo, unit, existing_plans, texts, evidence)
        return unit, manifest

    def findings_session(self, repo, unit, session_id):
        self._findings.session(repo, unit, session_id)

    def findings_fail(self, repo, unit, error):
        self._findings.fail(repo, unit, error)

    def findings_complete(self, repo, repository, base, cycle, revision, existing_plans, unit, output):
        progress = self.progress(repo, repository, base, cycle, revision)
        synthesis = self.synthesis(repo, cycle, revision, progress)
        texts = self._plan_texts(repository, revision, existing_plans)
        fresh = self._findings.prepare(repo, cycle, revision, synthesis.digest, existing_plans, texts)
        if (fresh.manifest_id, fresh.index) != (unit.manifest_id, unit.index):
            raise maintenance_reconciliation.ReconciliationError("발견 수집 중 source·계획 집합이 바뀌었습니다.")
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, revision)
        sources = {item.path: item for item in inventory.sources}
        class Contents(dict):
            def __missing__(self, path):
                self[path] = maintenance_file_review.source_bytes(repository, base, sources[path])
                return self[path]
        def validate(value):
            new_ids = tuple(sorted({item.get("plan_id", "") for item in value["findings"] if item.get("status") == "new_plan"}))
            maintenance_reconciliation.parse_findings(value, progress, inventory, Contents(),
                existing_plans, new_ids, texts, synthesis_digest=synthesis.digest)
        self._findings.complete(repo, unit, output, existing_plans, validate)

    def validate(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 revision: str, output: str, existing_plans: dict[str, str],
                 new_plan_ids: tuple[str, ...], *, resolution_directory: Path | None = None
                 ) -> dict | maintenance_reconciliation.ResolutionUnit:
        progress = self.progress(repo, repository, base, cycle_revision, revision)
        synthesis = self.synthesis(repo, cycle_revision, revision, progress)
        if synthesis.index != synthesis.total:
            raise maintenance_reconciliation.ReconciliationError("종합 증거 페이지 검토가 완료되지 않았습니다.")
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, revision)
        sources = {source.path: source for source in inventory.sources}

        class CurrentContents(dict):
            def __missing__(self, path: str) -> bytes:
                content = maintenance_file_review.source_bytes(repository, base, sources[path])
                self[path] = content
                return content

        plan_texts = self._plan_texts(repository, revision, existing_plans)
        collection = self._findings.prepare(repo, cycle_revision, revision, synthesis.digest, existing_plans, plan_texts)
        payload = self._findings.resolve(repo, collection, output, existing_plans)

        findings = maintenance_reconciliation.parse_findings(
            payload, progress, inventory, CurrentContents(), existing_plans, new_plan_ids, plan_texts,
            synthesis_digest=synthesis.digest,
        )
        if resolution_directory is not None and synthesis.total:
            manifest = self.synthesis_export(repo, synthesis, progress, resolution_directory)
            unit = self._synthesis.next_resolution(repo, synthesis, findings, manifest)
            if unit is not None:
                return unit
        self._synthesis.validate_resolutions(repo, synthesis, findings)
        return findings


class _PlanningLifecyclePorts:
    """Concrete bootstrap bindings for the independent coordinator."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        runtime_repository: runtime_state.RuntimeStateRepository,
        maintenance_repository: maintenance_state.MaintenanceRepository,
        context: PlanningContext,
        planner_mutex: threading.Lock | None,
    ):
        self.state = _PlanningStatePorts(
            connection, runtime_repository, maintenance_repository, context.state_dir
        )
        self.git = _PlanningGitPorts(
            connection, context.repo_path, context.state_dir
        )
        self.agent = _PlanningAgentPorts(connection, context, planner_mutex)
        self.validation = _PlanningValidationPorts(connection)
        self.file_review = _PlanningFileReviewPorts(connection)
        self.reconciliation = _PlanningReconciliationPorts(connection)
        self.events = _PlanningEventPorts()


def _planning_outcome_step_result(outcome: PlanningOutcome) -> int | None:
    """Translate a typed coordinator result at the process adapter boundary."""

    if outcome.kind in {
        PlanningOutcomeKind.EXECUTION_READY,
        PlanningOutcomeKind.REFACTORING,
    }:
        return None
    if outcome.kind in {
        PlanningOutcomeKind.FINGERPRINT_SKIPPED,
        PlanningOutcomeKind.NO_CHANGE,
        PlanningOutcomeKind.WAITING,
    }:
        return 0
    if outcome.kind in {
        PlanningOutcomeKind.PREREQUISITE_WAIT,
        PlanningOutcomeKind.REMOTE_BASE_RESTART,
        PlanningOutcomeKind.PUBLICATION_RECOVERY,
    }:
        return PROJECT_STEP_RETRY
    if outcome.kind is PlanningOutcomeKind.TRANSIENT_PAUSE:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is PlanningOutcomeKind.USAGE_LIMIT_PAUSE:
        return PROJECT_STEP_USAGE_PAUSE
    return 1


def reconcile_false_positive_runtime_block(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    block: sqlite3.Row,
    runtime_mutation: RuntimeMutation,
) -> bool:
    """Reconcile a saved prevalidation result only when its entire delta is ours."""
    if (
        _canonical_path(repo_path) != _canonical_path(runtime_mutation.source)
        or not str(block["evidence"] or "").startswith("Agent 범위 선검증 실패:")
        or not project["agent_snapshot"]
        or not project["agent_result_snapshot"]
    ):
        return False
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
        saved_worktree = Path(str(project["worktree"] or repo))
        current = capture_git_snapshot(saved_worktree)
        runtime_path = str(_canonical_path(runtime_mutation.path))
        expected = agent_scope_decision.reconcile_runtime_snapshot(
            before, before, current, runtime_path,
        )
        if result != current:
            operation_log(
                f"runtime 차단 재조정 거부: 결과 snapshot 불일치 "
                f"path={runtime_path} sha={runtime_mutation.commit_sha}",
                error=True,
            )
            return False
        if expected != current:
            deltas = []
            for label, older, newer in (
                ("refs", expected.refs, current.refs),
                ("worktrees", expected.worktrees, current.worktrees),
                ("config", expected.config, current.config),
                ("operations", expected.operations, current.operations),
            ):
                changed = sorted(
                    key for key in set(older) | set(newer)
                    if older.get(key) != newer.get(key)
                )
                if changed:
                    deltas.append(f"{label}={','.join(changed[:8])}")
            if any(getattr(expected, key) != getattr(current, key)
                   for key in ("head", "branch", "index", "dirty")):
                deltas.append("local-state")
            operation_log(
                f"runtime 차단 재조정 거부: path={runtime_path} "
                f"sha={runtime_mutation.commit_sha} delta={'; '.join(deltas)}",
                error=True,
            )
            return False
        validate_persisted_agent_scope(
            connection, repo_path, state_dir, repo,
            runtime_mutation=runtime_mutation,
        )
    except (AgentScopeError, RuntimeError, ValueError, KeyError):
        return False
    return True


def observe_blocked_plan_remotes(
    connection: sqlite3.Connection, repo_path: Path, repo: str,
    project: sqlite3.Row,
    *, plan_id: str | None = None,
) -> None:
    """Persist blocked-plan remote input before any project execution gate."""

    blocked_remote_observation.ensure_schema(connection)
    project_block = scoped_blocking.project_block(connection, repo)
    linked_plan_id = str(project_block["plan_id"] or "") if project_block else ""
    plans = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND "
        "(status = 'PLAN_BLOCKED' OR plan_id = ? OR plan_id = ?) "
        "AND (? IS NULL OR plan_id = ?) ORDER BY plan_id",
        (repo, linked_plan_id, plan_id, plan_id, plan_id),
    ).fetchall()
    if not plans:
        return
    configured = git(repo_path, "remote", "get-url", "origin", check=False)
    origin = configured.stdout.strip() if configured.returncode == 0 else ""
    origin_failure = "origin-unavailable"
    registered_origin = str(project["origin"] or "")
    query_origin = ""
    if not registered_origin:
        origin_failure = "registered-origin-missing"
    elif origin:
        try:
            origin_identity = github_repository(origin)
        except RuntimeError:
            origin_identity = origin
        if origin_identity == registered_origin:
            query_origin = origin
        else:
            origin_failure = "registered-origin-mismatch"
    base_ref = f"refs/heads/{project['base_branch']}"
    for plan in plans:
        branch_ref = f"refs/heads/{plan['branch']}" if plan["branch"] else ""
        base, branch = blocked_remote_observation.observe_refs(
            repo_path, query_origin, (base_ref, branch_ref),
            unavailable_reason=origin_failure,
        )
        block = connection.execute(
            "SELECT reason_code, release_fingerprint FROM execplan_blocks "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan["plan_id"]),
        ).fetchone()
        if block is not None:
            reason = str(block["reason_code"])
            fingerprint = str(block["release_fingerprint"] or "")
        elif plan["status"] == "PLAN_BLOCKED":
            reason = str(plan["blocked_reason"] or "")
            fingerprint = str(plan["blocked_fingerprint"] or "")
        else:
            reason = None
            fingerprint = None
        linked_project_block = project_block if linked_plan_id == plan["plan_id"] else None
        worktree_fingerprint = str(plan["blocked_worktree_fingerprint"] or "")
        if plan["worktree"]:
            try:
                worktree_fingerprint = _worktree_state_signature(Path(plan["worktree"]))
            except (OSError, RuntimeError, ValueError):
                worktree_fingerprint = "unavailable"
        blocked_remote_observation.record(
            connection, repo=repo, plan_id=str(plan["plan_id"]),
            registered_origin=registered_origin or None,
            origin_url=origin or None, base_ref=base_ref, branch_ref=branch_ref,
            base=base, branch=branch, blocked_reason=reason,
            blocked_fingerprint=fingerprint,
            project_blocked_reason=(
                str(linked_project_block["reason_code"])
                if linked_project_block else None
            ),
            project_blocked_fingerprint=(
                str(linked_project_block["release_fingerprint"] or "")
                if linked_project_block else None
            ),
            worktree_fingerprint=worktree_fingerprint,
        )
    connection.commit()


def _run_project_step(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    _check_runtime_handoff()
    # Resolve the persisted project key without consulting repository config.
    # An interrupted Agent may have changed origin or hooksPath, so the saved
    # snapshot must be checked before normal repository validation reads it.
    repo_path = Path(repo_arg).expanduser().resolve()
    repo = str(repo_path)
    if not registered_only:
        project_registry.ensure_registry_schema(connection)
        _ensure_execplans_schema(connection)
    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if row is not None and int(row["registered"]) != 1:
        raise RuntimeError(f"등록되지 않은 프로젝트입니다: {repo}")
    if row is None and registered_only:
        raise RuntimeError(f"등록되지 않은 프로젝트입니다: {repo}")
    require_self_pr_base = _self_runtime_project(
        connection, repo, state_dir, runtime_mutation,
    ) is not None
    provider = str(row["provider"]) if row else (requested_provider or "codex")
    if provider not in AGENT_PROVIDERS:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    if (
        row is not None
        and requested_provider is not None
        and requested_provider != provider
    ):
        raise RuntimeError(
            f"등록된 provider는 {provider}이며 {requested_provider}로 변경할 수 없습니다."
        )
    if row is not None:
        observe_blocked_plan_remotes(connection, repo_path, repo, row)
    _ensure_cleanup_pending_schema(connection)
    initial_project_block = (
        scoped_blocking.project_block(connection, repo) if row is not None else None
    )
    if row is not None and initial_project_block is not None and runtime_mutation is not None:
        reconcile_false_positive_runtime_block(
            connection, repo_path, state_dir, repo, row,
            initial_project_block, runtime_mutation,
        )
    if (
        row is not None
        and initial_project_block is not None
        and initial_project_block["reason_code"]
        != BlockReasonCode.EXECPLAN_CATALOG_INVALID.value
    ):
        observe_waiting_pull_requests(
            connection, repo_path, state_dir, repo,
            require_base_identity=require_self_pr_base, read_only=True,
        )
        resume_project_block_if_safe(connection, repo_path, state_dir, repo)
        project_block = scoped_blocking.project_block(connection, repo)
        if project_block is not None and project_block["plan_id"]:
            reconciler = project_blocked_recovery.ProjectBlockedReconciler(
                _project_blocked_recovery_effects(), connection, repo_path, state_dir, repo,
                str(row["base_branch"]), config_path, verify_timeout_seconds,
            )
            linked_plan_id = str(project_block["plan_id"])
            if reconciler.refresh_catalog(linked_plan_id):
                reconciler.reconcile(linked_plan_id)
            project_block = scoped_blocking.project_block(connection, repo)
        if project_block is not None:
            operation_print(
                "프로젝트 안전 차단 상태입니다: "
                + str(project_block["evidence"]),
                file=sys.stderr,
            )
            return 1
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
    cleanup_candidates = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in connection.execute(
            "SELECT worktree FROM cleanup_pending "
            "WHERE repo = ? AND workspace_owned = ?",
            (repo, WORKSPACE_OWNED),
        ).fetchall()
    )
    checkpoint_cleanup_candidates = cleanup_candidates
    if row and row["agent_recovery"]:
        try:
            resume_interrupted_blocked_agent_recovery(
                connection,
                repo_path,
                state_dir,
                repo,
            )
        except (AgentScopeError, RuntimeError) as error:
            recovery_error = f"BLOCKED Agent 중단 복구 실패:\n{error}"
            plan = active_execplan(connection, repo)
            if plan is not None:
                _update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    recovery_error,
                )
            else:
                update_project(connection, repo, last_error=recovery_error)
            operation_print(recovery_error, file=sys.stderr)
            return 1
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?",
            (repo,),
        ).fetchone()
    validated_agent_scope: GitSnapshot | None = None
    if row and row["agent_snapshot"]:
        planner_correction = bool(
            row["phase"] == "PLANNING" and row["planning_validation_error"]
        )
        try:
            validated_agent_scope = validate_persisted_agent_scope(
                connection,
                repo_path,
                state_dir,
                repo,
                allow_planner_worktree_correction=planner_correction,
                manager_cleanup_worktrees=cleanup_candidates,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
        except AgentScopeError as error:
            detail = str(error)
            scope_result_snapshot = error.result_snapshot
            scope_reason = error.reason_code
        except RuntimeError:
            detail = "저장된 Agent Git 상태를 안전하게 읽을 수 없습니다."
            scope_result_snapshot = None
            scope_reason = BlockReasonCode.REPOSITORY_STATE_UNSAFE
        else:
            detail = None
            scope_result_snapshot = None
            scope_reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
        if detail is not None:
            message = block_persisted_agent_scope_violation(
                connection,
                repo_path,
                repo,
                row,
                detail,
                result_snapshot=scope_result_snapshot,
                reason=scope_reason,
            )
            operation_print(message, file=sys.stderr)
            return 1
        if validated_agent_scope is not None:
            if row["agent_manager_snapshot"]:
                checkpoint_cleanup_candidates |= ManagerGitCheckpoint.deserialize(
                    str(row["agent_manager_snapshot"])
                ).cleanup_worktrees
            update_project(
                connection,
                repo,
                agent_manager_snapshot=ManagerGitCheckpoint(
                    validated_agent_scope,
                    checkpoint_cleanup_candidates,
                ).serialize(),
            )
    validated_origins: list[str] = []
    if row is not None:
        repo_path = validate_repository(
            repo_path,
            allow_agents_mismatch=False,
            provider=provider,
            config_path=config_path,
            registration_checks=False,
            expected_origin=(str(row["origin"]) if row["origin"] else None),
            origin_result=validated_origins,
        )
        current_origin = (
            str(row["origin"])
            if row["origin"]
            else (validated_origins[0] if validated_origins else None)
        )
        if row["origin"] is None and current_origin is not None:
            with connection:
                connection.execute(
                    "UPDATE projects SET origin = ? WHERE repo = ?",
                    (current_origin, repo),
                )
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
    else:
        repo_path = validate_repository(
            repo_path,
            allow_agents_mismatch=allow_agents_mismatch,
            provider=provider,
            config_path=config_path,
            origin_result=validated_origins,
        )
        current_origin = validated_origins[0] if validated_origins else None

    selected_base = requested_base or (row["base_branch"] if row else None)
    fetch_intent: int | None = None
    if (
        row is not None and validated_agent_scope is not None
        and row["phase"] == "PLAN_BLOCKED"
        and selected_base == row["base_branch"] and row["plan_path"]
    ):
        plan_id = _plan_id_from_path(str(row["plan_path"]))
        if plan_id:
            waiting_plan = connection.execute(
                "SELECT blocked_reason FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, plan_id),
            ).fetchone()
            if waiting_plan and waiting_plan["blocked_reason"] == GUIDANCE_BLOCK_REASON:
                fetch_intent = _record_manager_base_fetch_intent(
                    connection, repo_path, repo, plan_id,
                    str(selected_base), validated_agent_scope,
                )
    if fetch_intent is not None:
        base = _journaled_manager_base_fetch(
            connection, repo_path, state_dir, fetch_intent, str(selected_base),
            str(plan_id),
        )
    else:
        base = remote_base(repo_path, selected_base, state_dir=state_dir)
    catalog_refresh = project_catalog_refresh.ProjectCatalogRefresh(
        connection, repo,
        lambda: git(repo_path, "rev-parse", "--verify",
                    f"refs/remotes/origin/{base}^{{commit}}").stdout.strip(),
        lambda revision, remember: reconcile_execplan_catalog(
            connection, repo_path, repo, base, remote_sha=revision,
            on_reconciled=remember,
        ),
    )

    history_revision = None

    def refresh_catalog() -> str:
        nonlocal history_revision
        revision = catalog_refresh.refresh()
        if history_revision != revision:
            migrate_legacy_execplans(
                connection, reconcile_history=True, target_repo=repo, ensure_schema=False,
                pinned_catalog=catalog_refresh.pinned,
            )
            history_revision = revision
            catalog_refresh.adopt_history()
        return revision

    def catalog_stop(error) -> int:
        result = error.result
        if result.kind is execplan_catalog_state.ReconciliationKind.STALE:
            _record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE,
                "ExecPlan catalog snapshot이 적용 전에 변경되어 다시 조회합니다: "
                + str(result.revision_sha or ""),
            )
            return PROJECT_STEP_RETRY
        assert result.diagnostic is not None
        operation_print(result.diagnostic.message, file=sys.stderr)
        return 1

    recovered_planning_outcome: PlanningOutcome | None = None
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row is not None and execplan_catalog_state.DefinitionStateRepository(
        connection
    ).has_publication_intent(repo):
        # The owning publication must recover its manifest before the catalog
        # can classify new IDs. No PR/archive/selection consumer runs here.
        context = PlanningContext(
            repo=repo, repo_path=repo_path, state_dir=state_dir, base_branch=base,
            base_sha=catalog_refresh.current_revision(), provider=provider,
            config_path=config_path, registered_only=registered_only,
            refactoring_review_interval=refactoring_review_interval,
            self_repository=registered_only and is_loop_manager_repository(repo_path),
        )
        recovered_planning_outcome = PlanningLifecycleCoordinator(_PlanningLifecyclePorts(
            connection, runtime_state.RuntimeStateRepository(connection),
            maintenance_state.MaintenanceRepository(connection), context, planner_mutex,
        )).run(context)
        recovery_step_result = _planning_outcome_step_result(recovered_planning_outcome)
        if recovery_step_result is not None:
            return recovery_step_result
        # Publication may finish and select its Worker in this same turn.
        # Reconcile the published catalog before consumers, then carry the
        # outcome to execution without selecting or planning a second time.
        recovered_project = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if recovered_project is not None and not recovered_project["agent_snapshot"]:
            # The coordinator completed and cleared the validated Planner
            # scope; it no longer belongs to the selected Worker workspace.
            validated_agent_scope = None
    try:
        base_sha = refresh_catalog()
    except project_catalog_refresh.CatalogRefreshStopped as error:
        return catalog_stop(error)
    backfill_plan_shas(connection, target_repo=repo)
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row:
        hydrate_active_project(connection, repo)
        if (
            active_execplan(connection, repo) is None
            and row["phase"] in {"PR_OPEN", "PLAN_BLOCKED", "BLOCKED"}
            and not runtime_state.planning_owns_execution_slot(row)
            and scoped_blocking.project_block(connection, repo) is None
            and validated_agent_scope is None
        ):
            with connection:
                _clear_active_in_current_transaction(connection, repo)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    _ensure_cleanup_pending_schema(connection)
    cleanup_candidates = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in connection.execute(
            "SELECT worktree FROM cleanup_pending "
            "WHERE repo = ? AND workspace_owned = ?",
            (repo, WORKSPACE_OWNED),
        ).fetchall()
    )
    if row is not None and runtime_state.planning_owns_execution_slot(row):
        cleanup_pending = connection.execute(
            "SELECT * FROM cleanup_pending WHERE repo = ?", (repo,)
        ).fetchall()
    else:
        cleanup_pending = recheck_cleanup_pending(
            connection, repo_path, repo, state_dir
        )
    remaining_cleanup = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in cleanup_pending
    )
    manager_worktree_changes = checkpoint_cleanup_candidates - remaining_cleanup
    for pending in cleanup_pending:
        operation_print(
            f"보존된 worktree: {pending['worktree']} ({pending['reason']})",
            file=sys.stderr,
        )

    def archive_housekeeping_safe() -> bool:
        return _execplan_archive_housekeeping_safe(
            connection,
            repo,
            validated_agent_scope,
            maintenance_assessment_enabled=registered_only,
            refactoring_review_interval=refactoring_review_interval,
        )

    def archive_housekeeping() -> None:
        if not archive_housekeeping_safe():
            return
        try:
            dispatch_execplan_archive_housekeeping(
                connection,
                repo_path,
                state_dir,
                repo,
                base,
                catalog_refresh=catalog_refresh,
                archive_safety=archive_housekeeping_safe,
            )
        except project_catalog_refresh.CatalogRefreshStopped:
            raise
        except Exception as error:
            # Archive maintenance is deliberately non-gating: MERGED state,
            # READY selection, planning, and completion continue unchanged.
            operation_print(
                f"ExecPlan archive housekeeping 실패 (다음 실행에서 재시도): {error}",
                file=sys.stderr,
            )

    def synchronize_source_after_pr_reconciliation() -> None:
        nonlocal base_sha
        # PR observation can introduce a new ownership safety block.
        if scoped_blocking.project_block(connection, repo) is not None:
            return
        # Read the durable project state after all PR transitions and cleanup.
        current_project = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if current_project is not None and str(current_project["base_branch"]) != base:
            raise RuntimeError("PR 상태 동기화 중 프로젝트 base branch가 변경됐습니다.")
        _recorded_base_fetch(repo_path, state_dir, base, owner="project")
        base_sha = refresh_catalog()
        archive_housekeeping()
        base_sha = refresh_catalog()
        if scoped_blocking.project_block(connection, repo) is None:
            waiting_open = connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'PR_OPEN' LIMIT 1",
                (repo,),
            ).fetchone() is not None
            confirmed_merge = connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'MERGED' LIMIT 1",
                (repo,),
            ).fetchone() is not None
            conflict_recovery = bool(
                current_project is not None
                and int(current_project["pr_conflict_recovery"])
            )
            if (waiting_open or conflict_recovery) and not confirmed_merge:
                sync_result = BaseWorktreeSyncResult(
                    "skipped",
                    "PR 충돌 감지/복구 중 source checkout 보존",
                    base_sha,
                )
            else:
                sync_result = ensure_safe_base_worktree(
                    repo_path, base, remote_sha=base_sha, state_dir=state_dir,
                )
            if isinstance(sync_result, BaseWorktreeSyncResult):
                log_base_worktree_sync(repo_path, sync_result)

    try:
        pr_sync = synchronize_waiting_pull_requests(
            connection, repo_path, state_dir, repo,
            after_reconciliation=synchronize_source_after_pr_reconciliation,
            require_base_identity=require_self_pr_base,
            after_observation=lambda observation: _handoff_observed_self_merge(
                connection, repo, state_dir, runtime_mutation, observation,
            ),
        )
        base_sha = refresh_catalog()
    except project_catalog_refresh.CatalogRefreshStopped as error:
        return catalog_stop(error)
    if pr_sync.step_result not in {None, 0}:
        return pr_sync.step_result
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    project_block = scoped_blocking.project_block(connection, repo)
    if project_block is not None:
        resume_project_block_if_safe(
            connection, repo_path, state_dir, repo
        )
        project_block = scoped_blocking.project_block(connection, repo)
    if project_block is not None:
        operation_print(
            "프로젝트 안전 차단 상태입니다: "
            + str(project_block["evidence"]),
            file=sys.stderr,
        )
        return 1
    if (
        row is not None
        and int(row["registered"]) == 1
        and row["phase"] is None
        and active_execplan(connection, repo) is None
    ):
        repository = maintenance_state.MaintenanceRepository(connection)
        state = repository.get(repo)
        if (
            state.phase is maintenance_state.MaintenancePhase.IDLE
            and state.file_assessment_start_merged_count is None
        ):
            current_sha = git(
                repo_path, "rev-parse", "--verify",
                f"refs/remotes/origin/{base}^{{commit}}",
            ).stdout.strip()
            if current_sha != base_sha:
                return PROJECT_STEP_RETRY
            repository.initialize_file_assessment_start(
                repo, source_sha=base_sha,
                expected_merged_count=repository.merged_count(repo),
            )
    if pr_sync.step_result not in {None, 0}:
        return pr_sync.step_result
    if validated_agent_scope is not None:
        try:
            validated_agent_scope = refresh_persisted_agent_scope_after_manager_git(
                connection,
                repo,
                validated_agent_scope,
                manager_worktree_changes,
                state_dir=state_dir,
            )
        except (AgentScopeError, RuntimeError) as error:
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            message = block_persisted_agent_scope_violation(
                connection,
                repo_path,
                repo,
                row,
                str(error),
                reason=getattr(
                    error,
                    "reason_code",
                    BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                ),
            )
            operation_print(message, file=sys.stderr)
            return 1
    resume_plan_blocks(
        connection, repo_path, state_dir, repo, base, config_path,
        verify_timeout_seconds,
    )
    if scoped_blocking.project_block(connection, repo) is not None:
        return 1
    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()

    if (
        row and row["branch"]
        and not runtime_state.planning_owns_execution_slot(row)
        and _project_pr_merge_confirmed(connection, repo, row)
    ):
        # MERGED is durable even if its compatibility projection survived
        # the transition. Release that execution slot without changing the
        # plan's audit, sessions, worktree or pending cleanup evidence.
        if active_execplan(connection, repo) is None:
            with connection:
                _clear_active_in_current_transaction(connection, repo)
        else:
            hydrate_active_project(connection, repo)
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()

    if (row and row["branch"] and not runtime_state.planning_owns_execution_slot(row)
            and not (recovered_planning_outcome is not None
                     and recovered_planning_outcome.newly_activated)):
        existing_pr = pr_for_branch(repo_path, row["branch"], row["pr_url"])
        if existing_pr:
            try:
                reconciliation = reconcile_existing_pull_request(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    row,
                    existing_pr,
                )
                if reconciliation is not None:
                    return reconciliation
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (repo,)
                ).fetchone()
            except PublicationSafetyError as error:
                publication_error = f"기존 PR 복구 거부: {error}"
                block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{row['base_branch']}",
                    publication_error,
                    str(row["verify_result"] or ""),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                operation_print(publication_error, file=sys.stderr)
                return 1

    if not row:
        connection.execute(
            "INSERT INTO projects (repo, base_branch, provider, origin) "
            "VALUES (?, ?, ?, ?)",
            (repo, base, provider, current_origin),
        )
        connection.commit()
        migrate_legacy_execplans(
            connection,
            reconcile_history=True,
            target_repo=repo,
            ensure_schema=not registered_only,
        )
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
    elif not row["phase"]:
        update_project(connection, repo, base_branch=base)
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (repo,)
        ).fetchone()

    assert row is not None
    _recorded_base_fetch(repo_path, state_dir, base, owner="project")
    try:
        base_sha = refresh_catalog()
    except project_catalog_refresh.CatalogRefreshStopped as error:
        return catalog_stop(error)
    runtime_repository = runtime_state.RuntimeStateRepository(connection)
    maintenance_repository = maintenance_state.MaintenanceRepository(connection)
    planning_context = PlanningContext(
        repo=repo,
        repo_path=repo_path,
        state_dir=state_dir,
        base_branch=base,
        base_sha=base_sha,
        provider=provider,
        config_path=config_path,
        registered_only=registered_only,
        refactoring_review_interval=refactoring_review_interval,
        self_repository=registered_only and is_loop_manager_repository(repo_path),
    )
    _check_runtime_handoff()
    if recovered_planning_outcome is not None:
        planning_outcome = replace(recovered_planning_outcome, base_sha=base_sha)
    else:
        planning_outcome = PlanningLifecycleCoordinator(
            _PlanningLifecyclePorts(
                connection,
                runtime_repository,
                maintenance_repository,
                planning_context,
                planner_mutex,
            )
        ).run(planning_context)
    if planning_outcome.kind in {
        PlanningOutcomeKind.REMOTE_BASE_RESTART,
        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
        PlanningOutcomeKind.TRANSIENT_PAUSE,
    }:
        reason = (
            recovery_runtime.RecoveryReason.SERVICE_UNAVAILABLE
            if planning_outcome.kind is PlanningOutcomeKind.TRANSIENT_PAUSE
            else recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE
            if planning_outcome.kind is PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED
            else recovery_runtime.RecoveryReason.BASE_BEHIND
        )
        _record_project_recovery(
            connection, repo, reason,
            planning_outcome.detail or planning_outcome.kind.value,
            attempts=(
                3 if planning_outcome.kind in {
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                    PlanningOutcomeKind.TRANSIENT_PAUSE,
                } else 0
            ),
        )
    planning_step_result = _planning_outcome_step_result(planning_outcome)
    if planning_step_result is not None:
        return planning_step_result
    base_sha = planning_outcome.base_sha or base_sha
    newly_activated = planning_outcome.newly_activated
    execution_ports = _ExecutionLifecyclePorts(
        connection,
        runtime_repository,
        repo=repo,
        repo_path=repo_path,
        state_dir=state_dir,
        base=base,
        base_sha=base_sha,
        provider=provider,
        config_path=config_path,
        verify_timeout_seconds=verify_timeout_seconds,
        newly_activated=newly_activated,
    )
    execution = ExecutionPhaseCoordinator(execution_ports)
    _check_runtime_handoff()
    prepared = execution.prepare()
    if isinstance(prepared, ExecutionOutcome):
        return _execution_outcome_step_result(prepared)
    execution_context = prepared
    while True:
        _check_runtime_handoff()
        execution_outcome = execution.dispatch(execution_context)
        if (
            execution_outcome.kind is ExecutionOutcomeKind.PHASE_ADVANCED
            and execution_outcome.context is not None
        ):
            execution_context = execution_outcome.context
            continue
        return _execution_outcome_step_result(execution_outcome)

def _checkpoint_dispatch_ready(
    connection: sqlite3.Connection,
    repo: str,
) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'blocked_checkpoints')"
        )
    }
    if tables != {"projects", "blocked_checkpoints"}:
        return False
    return connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is not None


def _record_project_recovery(
    connection: sqlite3.Connection,
    repo: str,
    reason: recovery_runtime.RecoveryReason,
    detail: str,
    *,
    attempts: int = 0,
) -> recovery_runtime.RecoveryDecision:
    """Persist a project/PR operation failure without changing execution state."""

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'exec_plans')"
        )
    }
    project = (
        connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        if "projects" in tables else None
    )
    plan = (
        project_execplan(connection, repo)
        if project is not None and "exec_plans" in tables else None
    )
    phase = str(project["phase"] or "") if project is not None else ""
    active_sha = str(project["active_sha"] or "") if project is not None else ""
    branch = str(project["branch"] or "") if project is not None else ""
    marker = f"{active_sha}|{branch}|{project['worker_session_id'] if project is not None else ''}"
    fingerprint = hashlib.sha256(
        f"{reason.value}|{phase}|{active_sha}|{branch}".encode()
    ).hexdigest()
    observation = recovery_runtime.RecoveryObservation(
        reason=reason, repo=repo,
        plan_id=str(plan["plan_id"]) if plan is not None else None,
        phase=phase,
        plan_revision=(str(plan["current_revision_sha"] or plan["plan_sha"] or "")
                       if plan is not None else None),
        base_revision=active_sha, fingerprint=fingerprint,
        progress_marker=marker, detail=detail,
    )
    with runtime_state._transaction(connection):
        return recovery_state.RecoveryRepository(connection).observe(
            observation, attempts=attempts,
        ).decision


def _blocked_recovery_subjects(
    connection: sqlite3.Connection, repo: str,
) -> frozenset[str]:
    """Return subjects whose final BLOCKED event owns this failure instead."""

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('execplan_blocks', 'project_blocks')"
        )
    }
    subjects: set[str] = set()
    if "execplan_blocks" in tables:
        subjects.update(
            str(row[0]) for row in connection.execute(
                "SELECT plan_id FROM execplan_blocks WHERE repo = ?", (repo,)
            )
        )
    if "project_blocks" in tables:
        subjects.update(
            str(row[0]) if row[0] is not None else "__project__"
            for row in connection.execute(
                "SELECT plan_id FROM project_blocks WHERE repo = ?", (repo,)
            )
        )
    return frozenset(subjects)


def run_project_result(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    on_step_completed: Callable[[], None] | None = None,
    runtime_mutation: RuntimeMutation | None = None,
    external_run_id: str | None = None,
) -> ProjectRunResult:
    """Run one scheduler invocation and project its external failure outcome."""

    repo = str(Path(repo_arg).expanduser().resolve())
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    recovery_repository = recovery_state.RecoveryRepository(connection)
    run_id = recovery_state.begin_external_run(connection, external_run_id)
    # The TEMP run marker must not hold the transaction that later makes each
    # recovery observation durable on its own bounded state transition.
    connection.commit()
    outcome: ProjectRunResult | None = None
    try:
        outcome = _run_project_result(
            connection, state_dir, repo_arg, config_path, requested_base,
            allow_agents_mismatch, requested_provider,
            registered_only=registered_only, planner_mutex=planner_mutex,
            refactoring_review_interval=refactoring_review_interval,
            verify_timeout_seconds=verify_timeout_seconds,
            on_step_completed=on_step_completed, runtime_mutation=runtime_mutation,
        )
        return outcome
    except SelfMergeRuntimeHandoff:
        outcome = ProjectRunResult(ProjectRunKind.RUNTIME_HANDOFF, 0)
        raise
    finally:
        # Failure observations are saved in their recovery transaction.  This
        # boundary only resolves an execution that reached a stable outcome.
        with connection:
            recovery_repository.finish_external_run(
                repo, run_id,
                resolved=(
                    outcome is not None
                    and outcome.kind not in {
                        ProjectRunKind.TRANSIENT_PAUSE,
                        ProjectRunKind.USAGE_LIMIT_PAUSE,
                        ProjectRunKind.OPERATIONAL_ERROR,
                        ProjectRunKind.BLOCKED,
                        ProjectRunKind.RUNTIME_HANDOFF,
                    }
                ),
                blocked_subjects=(
                    frozenset() if outcome is not None
                    and outcome.kind is ProjectRunKind.RUNTIME_HANDOFF
                    else _blocked_recovery_subjects(connection, repo)
                ),
            )
            if outcome is None or outcome.kind is not ProjectRunKind.RUNTIME_HANDOFF:
                notifications.enqueue_persistent_execution_failure_events(connection, repo)


def _stable_work_reason(
    connection: sqlite3.Connection, repo: str, *,
    refactoring_review_interval: int,
) -> work_exhaustion.WorkReasonSnapshot | None:
    """Confirm that the latest remote selection has no runnable work."""

    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if row is None or row["phase"] not in {None, "PR_OPEN", "PLAN_BLOCKED"}:
        return None
    if row["pr_conflict_recovery"] or row["planning_error"]:
        return None
    if not row["planning_fingerprint"]:
        return None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'general_planning_publications'"
    ).fetchone() is not None and connection.execute(
        "SELECT 1 FROM general_planning_publications WHERE repo = ?", (repo,),
    ).fetchone() is not None:
        return None
    schedule = catalog_schedule_decision(connection, repo)
    if schedule.active is not None or schedule.runnable_ready is not None:
        return None
    maintenance = maintenance_state.MaintenanceRepository(connection)
    cycle = maintenance.get(repo)
    if (
        cycle.phase is not maintenance_state.MaintenancePhase.IDLE
        or cycle.publication_stage is not None
        or maintenance.file_assessment_due(
            repo, refactoring_review_interval,
            self_repository=is_loop_manager_repository(Path(repo)),
        ).required
    ):
        return None
    repo_path = Path(repo)
    revision = f"refs/remotes/origin/{row['base_branch']}"
    base_sha = git(
        repo_path, "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).stdout.strip()
    current_input = _planning_input_fingerprint(repo_path, base_sha)
    current_specification = _specification_input_fingerprint(repo_path, base_sha)
    archive_only = False
    if current_input != row["planning_fingerprint"] and row["processed_sha"]:
        archive_only = _is_requirement_archive_only_change(
            repo_path, str(row["processed_sha"]), base_sha,
        )
    if _general_planning_required(row, repo_path, base_sha, current_input):
        return None
    if row["specification_fingerprint"] is not None:
        if (
            current_specification != row["specification_fingerprint"]
            and not archive_only
            and not (
                row["processed_sha"]
                and _specification_input_fingerprint(
                    repo_path, str(row["processed_sha"])
                ) == current_specification
            )
        ):
            return None
    active_requirements = tuple(
        path.decode("utf-8") for path in _git_tree_entries(repo_path, base_sha)
        if _is_active_requirement(path)
    )
    snapshot = work_exhaustion.collect(
        connection, repo, cycle=str(row["planning_fingerprint"]),
        active_requirements=active_requirements,
    )
    return None if snapshot is not None and snapshot.completed and row["phase"] else snapshot


def _run_project_result(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    on_step_completed: Callable[[], None] | None = None,
    runtime_mutation: RuntimeMutation | None = None,
) -> ProjectRunResult:
    """Run retryable validation progress to a stable project-level outcome."""

    repo = str(Path(repo_arg).expanduser().resolve())
    checkpoint_attempts: set[str] = set()
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    recovery_attempts: dict[str, int] = {}
    pause_detail: str | None = None
    while True:
        if _checkpoint_dispatch_ready(connection, repo):
            project_before = connection.execute(
                "SELECT phase, pr_conflict_recovery FROM projects WHERE repo = ?",
                (repo,),
            ).fetchone()
            schedule_before = catalog_schedule_decision(connection, repo)
            maintenance_before = maintenance_state.MaintenanceRepository(
                connection
            ).get(repo)
            if (
                schedule_before.active is not None
                or schedule_before.runnable_ready is not None
                or project_before["phase"] not in {
                    None, "PR_OPEN", "PLAN_BLOCKED", "PROJECT_BLOCKED",
                }
                or project_before["pr_conflict_recovery"]
                or maintenance_before.phase is not maintenance_state.MaintenancePhase.IDLE
            ):
                notifications.mark_work_available(connection, repo)
            pending_before = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    connection
                ).pending(repo)
            )
            if any(
                request.request_identity not in checkpoint_attempts
                for request in pending_before
            ):
                dispatch_blocked_checkpoints(
                    connection,
                    Path(repo),
                    state_dir,
                    repo,
                    skip_identities=frozenset(checkpoint_attempts),
                )
                checkpoint_attempts.update(
                    request.request_identity for request in pending_before
                )
        try:
            recovery_before = {
                str(record["subject"]): int(record["generation"])
                for record in connection.execute(
                    "SELECT subject, generation FROM recovery_records WHERE repo = ?",
                    (repo,),
                )
            }
            step_arguments = (
                connection,
                state_dir,
                repo_arg,
                config_path,
                requested_base,
                allow_agents_mismatch,
                requested_provider,
                registered_only,
                planner_mutex,
                refactoring_review_interval,
                verify_timeout_seconds,
            )
            step_result = _run_project_step(
                *step_arguments,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
        except GitLockError as error:
            _record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.GIT_LOCK,
                str(error),
                attempts=3,
            )
            operation_log(
                f"일시적 Git lock 충돌 ({repo_arg}): {error}",
                error=True,
            )
            return ProjectRunResult(
                ProjectRunKind.TRANSIENT_PAUSE,
                0,
                str(error),
            )
        if on_step_completed is not None:
            on_step_completed()
        if step_result == PROJECT_STEP_RETRY:
            for record in connection.execute(
                "SELECT subject, generation, identity, disposition, detail "
                "FROM recovery_records WHERE repo = ?", (repo,),
            ):
                if int(record["generation"]) <= recovery_before.get(str(record["subject"]), 0):
                    continue
                if record["disposition"] not in {"transient_retry", "state_reconcile"}:
                    continue
                identity = str(record["identity"])
                run_key = f"{record['subject']}|{record['disposition']}"
                recovery_attempts[run_key] = recovery_attempts.get(run_key, 0) + 1
                if recovery_attempts[run_key] >= 3:
                    with connection:
                        recovery_state.RecoveryRepository(connection).mark_paused(
                            repo, str(record["subject"]), identity,
                            int(record["generation"]), recovery_attempts[run_key],
                        )
                    step_result = PROJECT_STEP_TRANSIENT_PAUSE
                    pause_detail = str(record["detail"])
                    break
        if _checkpoint_dispatch_ready(connection, repo):
            pending_after = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    connection
                ).pending(repo)
            )
            if any(
                request.request_identity not in checkpoint_attempts
                for request in pending_after
            ):
                dispatch_blocked_checkpoints(
                    connection,
                    Path(repo),
                    state_dir,
                    repo,
                    skip_identities=frozenset(checkpoint_attempts),
                )
                checkpoint_attempts.update(
                    request.request_identity for request in pending_after
                )
            schedule = catalog_schedule_decision(connection, repo)
            if (
                step_result not in {
                    PROJECT_STEP_TRANSIENT_PAUSE,
                    PROJECT_STEP_USAGE_PAUSE,
                    PROJECT_STEP_PLANNING_PENDING,
                }
                and schedule.active is None
                and schedule.runnable_ready is not None
                and not schedule.project_blocked
            ):
                project_after = connection.execute(
                    "SELECT phase, planning_error FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                if project_after is not None and (
                    project_after["phase"] != "PLANNING"
                    and not project_after["planning_error"]
                ):
                    step_result = PROJECT_STEP_RETRY
        if step_result != PROJECT_STEP_RETRY:
            break

    row = connection.execute(
        "SELECT phase, last_error, planning_error FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    detail = str(row["last_error"] or "") if row else None
    if step_result == PROJECT_STEP_TRANSIENT_PAUSE:
        return ProjectRunResult(ProjectRunKind.TRANSIENT_PAUSE, 0, pause_detail or detail)
    if step_result == PROJECT_STEP_USAGE_PAUSE:
        return ProjectRunResult(ProjectRunKind.USAGE_LIMIT_PAUSE, 0, detail)
    if step_result == PROJECT_STEP_PLANNING_PENDING:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    if row is not None and row["phase"] == "PROJECT_BLOCKED":
        project_block = scoped_blocking.project_block(connection, repo)
        reason = (
            f"PROJECT_BLOCKED {project_block['reason_code']}: {project_block['evidence']}"
            if project_block is not None else detail
        )
        operation_log(str(reason), error=True)
        return ProjectRunResult(ProjectRunKind.BLOCKED, 1, reason)
    if row is not None and row["planning_error"]:
        reason = f"planning_error: {row['planning_error']}"
        operation_log(reason, error=True)
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, reason)
    snapshot = None
    if step_result == 0 or (step_result == 1 and connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'PLAN_BLOCKED' "
        "LIMIT 1", (repo,),
    ).fetchone() is not None):
        snapshot = _stable_work_reason(
            connection, repo, refactoring_review_interval=refactoring_review_interval,
        )
    if snapshot is not None:
        reason = snapshot.describe()
        operation_log(f"작업 선택 결과 ({repo}): {reason}")
        if snapshot.completed and step_result == 0:
            notifications.enqueue_completion_if_ready(connection, repo, snapshot)
            return ProjectRunResult(ProjectRunKind.COMPLETED, 0, reason)
        if not snapshot.completed:
            notifications.enqueue_waiting_reason(connection, repo, snapshot)
            return ProjectRunResult(ProjectRunKind.WAITING, 0, reason)
    plan_block = connection.execute(
        "SELECT evidence FROM execplan_blocks WHERE repo = ? "
        "ORDER BY updated_at DESC, plan_id LIMIT 1",
        (repo,),
    ).fetchone()
    if step_result != 0 and plan_block is not None:
        return ProjectRunResult(
            ProjectRunKind.WAITING,
            0,
            str(plan_block["evidence"]),
        )
    if step_result != 0:
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, detail)
    if row is not None and row["phase"] is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    maintenance_repository = maintenance_state.MaintenanceRepository(connection)
    maintenance_cycle = maintenance_repository.get(repo)
    if (
        maintenance_cycle.phase is not maintenance_state.MaintenancePhase.IDLE
        or maintenance_repository.file_assessment_due(
            repo, refactoring_review_interval,
            self_repository=is_loop_manager_repository(Path(repo)),
        ).required
    ):
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    unfinished = connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id) LIMIT 1",
        (repo,),
    ).fetchone()
    if unfinished is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    return ProjectRunResult(ProjectRunKind.WAITING, 0, detail or "Planner 확인 대기")


def run_project(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
) -> int:
    """CLI-compatible adapter for the reusable structured project result."""

    return run_project_result(
        connection,
        state_dir,
        repo_arg,
        config_path,
        requested_base,
        allow_agents_mismatch,
        requested_provider,
        registered_only=registered_only,
        planner_mutex=planner_mutex,
        refactoring_review_interval=refactoring_review_interval,
        verify_timeout_seconds=verify_timeout_seconds,
    ).exit_code


def _run_registered_project(
    project: project_registry.RegisteredProject,
    state_dir: Path,
    config_path: Path,
    telegram_token: str,
    telegram_chat_id: str,
    planner_mutex: threading.Lock,
    refactoring_review_interval: int,
    verify_timeout_seconds: int = 600,
    runtime_mutation: RuntimeMutation | None = None,
    external_run_id: str | None = None,
    handoff_gate: RuntimeHandoffGate | None = None,
) -> ScheduledProjectResult:
    """Run one registry row with state isolated to this worker connection."""

    handoff_token = _runtime_handoff_signal.set(handoff_gate)
    try:
        _check_runtime_handoff()
        # The scheduler connection completed migrations before starting any
        # project threads. Repeating DDL here races with another project's
        # state transaction and can fail with SQLITE_BUSY.
        with connect_database(state_dir, run_migrations=False) as connection:
            notifications.configure_session(
                connection, enabled=bool(telegram_token and telegram_chat_id)
            )
            # TEMP session setup opens a SQLite transaction. Close it before
            # runtime-state repository operations so each lifecycle change
            # owns and durably commits its intended transaction boundary.
            connection.commit()

            def deliver_new_pr_notifications() -> None:
                try:
                    notifications.dispatch_new_pr_notifications(
                        connection,
                        telegram_token,
                        telegram_chat_id,
                        project.repo,
                    )
                except Exception as error:
                    operation_log(
                        f"즉시 PR 알림 전송 실패 ({type(error).__name__})",
                        error=True,
                    )

            result = run_project_result(
                connection,
                state_dir,
                project.repo,
                config_path,
                None,
                False,
                registered_only=True,
                planner_mutex=planner_mutex,
                refactoring_review_interval=refactoring_review_interval,
                verify_timeout_seconds=verify_timeout_seconds,
                on_step_completed=deliver_new_pr_notifications,
                external_run_id=external_run_id,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
    except SelfMergeRuntimeHandoff:
        if handoff_gate is not None:
            handoff_gate.stop()
        result = ProjectRunResult(ProjectRunKind.RUNTIME_HANDOFF, 0)
    except (ManagedConfigError, project_registry.ProjectRegistryError) as error:
        result = ProjectRunResult(
            ProjectRunKind.CONFIGURATION_ERROR,
            1,
            str(error),
        )
        operation_log(
            f"프로젝트 설정 실패 ({project.repo}): {error}",
            error=True,
        )
    except Exception as error:
        result = ProjectRunResult(
            ProjectRunKind.OPERATIONAL_ERROR,
            1,
            str(error),
        )
        operation_log(
            f"프로젝트 처리 실패 ({project.repo}): {error}",
            error=True,
        )
    finally:
        _runtime_handoff_signal.reset(handoff_token)
    return ScheduledProjectResult(project.repo, result)


class SelfMergeRuntimeHandoff(BaseException):
    """A durable self PR merge requires the next external bootstrap revision."""


@dataclass(frozen=True)
class RuntimeHandoffGate:
    """Serialize a handoff decision with the instant a subprocess starts."""

    event: threading.Event
    launch_lock: threading.Lock

    def check(self) -> None:
        if self.event.is_set():
            raise SelfMergeRuntimeHandoff()

    def stop(self) -> None:
        with self.launch_lock:
            self.event.set()

    def popen(self, *args: object, **kwargs: object) -> subprocess.Popen:
        with self.launch_lock:
            self.check()
            return subprocess.Popen(*args, **kwargs)


_runtime_handoff_signal: ContextVar[RuntimeHandoffGate | None] = ContextVar(
    "runtime_handoff_signal", default=None,
)


def _check_runtime_handoff() -> None:
    gate = _runtime_handoff_signal.get()
    if gate is not None:
        gate.check()


def _handoff_popen(*args: object, **kwargs: object) -> subprocess.Popen:
    gate = _runtime_handoff_signal.get()
    return (
        gate.popen(*args, **kwargs) if gate is not None
        else subprocess.Popen(*args, **kwargs)
    )


def _handoff_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
    """Keep the spawn atomic with handoff without holding the lock while waiting."""

    gate = _runtime_handoff_signal.get()
    if gate is None:
        return subprocess.run(*args, **kwargs)
    options = dict(kwargs)
    input_data = options.pop("input", None)
    timeout = options.pop("timeout", None)
    if input_data is not None:
        options["stdin"] = subprocess.PIPE
    with gate.popen(*args, **options) as process:
        try:
            stdout, stderr = process.communicate(input_data, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(error.cmd, error.timeout, stdout, stderr) from error
        except BaseException:
            # Match subprocess.run: stop the child before the context manager
            # closes its pipes and waits, including on KeyboardInterrupt.
            process.kill()
            raise
    return subprocess.CompletedProcess(args[0], process.returncode, stdout, stderr)


def _registered_self_runtime_project(
    project: project_registry.RegisteredProject,
    runtime: RuntimeMutation,
    state_dir: Path,
) -> bool:
    """Match the parent's source checkout, not an alias or another clone."""

    source = _canonical_path(runtime.source)
    if _canonical_path(runtime.path) != _canonical_path(ROOT):
        return False
    if project.repo != str(source) or Path(project.repo).is_symlink():
        return False
    repository = Path(project.repo)
    top_level = _canonical_path(Path(git(
        repository, "rev-parse", "--show-toplevel"
    ).stdout.strip()))
    if top_level != source or _git_common_directory(repository) != _git_common_directory(source):
        raise RuntimeError("등록된 self source repository identity가 다릅니다.")
    if _git_common_directory(runtime.path) != _git_common_directory(source):
        raise RuntimeError("runtime과 self source repository identity가 다릅니다.")
    source_origin = repository_origin(source)
    if repository_origin(repository) != source_origin or repository_origin(runtime.path) != source_origin:
        raise RuntimeError("runtime과 등록된 self source origin이 다릅니다.")
    if project.origin is not None and project.origin != source_origin:
        raise RuntimeError("등록된 self origin이 현재 origin과 다릅니다.")
    if not project.base_branch:
        raise RuntimeError("등록된 self base branch가 비어 있습니다.")
    if bootstrap_base_branch(source, state_dir, source_origin) != project.base_branch:
        raise RuntimeError("runtime과 등록된 self base branch가 다릅니다.")
    if git(runtime.path, "rev-parse", "HEAD").stdout.strip() != runtime.commit_sha:
        raise RuntimeError("현재 runtime SHA가 선택된 SHA와 다릅니다.")
    return True


def _self_runtime_project(
    connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    runtime: RuntimeMutation | None,
) -> project_registry.RegisteredProject | None:
    if runtime is None:
        return None
    for project in project_registry.list_projects(connection):
        if project.repo == repo:
            return project if _registered_self_runtime_project(
                project, runtime, state_dir,
            ) else None
    return None


def _finish_self_merge_handoff(
    connection: sqlite3.Connection,
    project: project_registry.RegisteredProject,
    state_dir: Path,
    runtime: RuntimeMutation,
    observation: WaitingPullRequestObservation,
) -> None:
    """Use the durable merge observation to decide a normal runtime exit."""

    if not observation.merged:
        return
    if not _registered_self_runtime_project(project, runtime, state_dir):
        raise RuntimeError("self Merge 뒤 source identity가 변경됐습니다.")
    repo_path = Path(project.repo)
    base = project.base_branch
    remote_sha = git(
        repo_path, "rev-parse", "--verify",
        f"refs/remotes/origin/{base}^{{commit}}",
    ).stdout.strip()
    if scoped_blocking.project_block(connection, project.repo) is None:
        sync = ensure_safe_base_worktree(
            repo_path, base, remote_sha=remote_sha, state_dir=state_dir,
        )
    else:
        sync = BaseWorktreeSyncResult(
            "skipped", "프로젝트 안전 차단 중 source checkout 보존", remote_sha,
        )
    log_base_worktree_sync(repo_path, sync)
    if git(
        repo_path, "rev-parse", "--verify",
        f"refs/remotes/origin/{base}^{{commit}}",
    ).stdout.strip() != remote_sha:
        raise RuntimeError("self Merge 뒤 origin base ref가 변경됐습니다.")
    if not _registered_self_runtime_project(project, runtime, state_dir):
        raise RuntimeError("self Merge 뒤 source identity가 변경됐습니다.")
    if remote_sha == runtime.commit_sha:
        return
    ancestor = git(
        repo_path, "merge-base", "--is-ancestor",
        runtime.commit_sha, remote_sha, check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError(
            "self Merge 뒤 runtime과 원격 base의 fast-forward 관계를 "
            "확인할 수 없습니다."
        )
    gate = _runtime_handoff_signal.get()
    if gate is not None:
        gate.stop()
    operation_log(
        f"self Merge runtime handoff: repository={project.repo} "
        f"base={base} runtime={runtime.commit_sha} remote={remote_sha} "
        f"prs={','.join(observation.merged_pr_urls)} "
        f"source_sync={sync.status} ({sync.reason})"
    )
    raise SelfMergeRuntimeHandoff()


def _preflight_self_merge_handoff(
    connection: sqlite3.Connection,
    projects: tuple[project_registry.RegisteredProject, ...],
    state_dir: Path,
    runtime: RuntimeMutation | None,
) -> None:
    """Reconcile self PRs before any project thread can start new work."""

    if runtime is None:
        return
    for project in projects:
        if not _registered_self_runtime_project(project, runtime, state_dir):
            continue
        observation = observe_waiting_pull_requests(
            connection, Path(project.repo), state_dir, project.repo,
            require_base_identity=True,
        )
        _finish_self_merge_handoff(
            connection, project, state_dir, runtime, observation,
        )
        return


def _handoff_observed_self_merge(
    connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    runtime: RuntimeMutation | None,
    observation: WaitingPullRequestObservation,
) -> None:
    """Guard a merge newly observed after the scheduler preflight."""

    if runtime is None or not observation.merged:
        return
    project = _self_runtime_project(connection, repo, state_dir, runtime)
    if project is not None:
        _finish_self_merge_handoff(
            connection, project, state_dir, runtime, observation,
        )


def schedule_registered_projects(
    connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
    runtime_mutation: RuntimeMutation | None = None,
) -> tuple[ScheduledProjectResult, ...]:
    """Run each registered project once with bounded cross-project concurrency."""

    projects = project_registry.list_projects(connection)
    if not projects:
        return ()

    if runtime_mutation is not None:
        notifications.configure_session(
            connection, enabled=bool(config.telegram_token and config.telegram_chat_id),
        )
        connection.commit()
    _preflight_self_merge_handoff(connection, projects, state_dir, runtime_mutation)

    planner_mutex = threading.Lock()
    handoff_gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
    external_run_id = secrets.token_hex(16)
    worker_count = min(config.max_workers, len(projects))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="loop-project",
    ) as pool:
        futures = tuple(
            pool.submit(
                _run_registered_project,
                project,
                state_dir,
                config.path,
                config.telegram_token,
                config.telegram_chat_id,
                planner_mutex,
                config.refactoring_review_interval,
                config.verify_timeout_seconds,
                external_run_id=external_run_id,
                handoff_gate=handoff_gate,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
            for project in projects
        )
        return tuple(future.result() for future in futures)


def run_registered_projects(
    connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    """Run all registered projects and aggregate outcomes after every worker exits."""

    try:
        notifications.dispatch_pending(
            connection, config.telegram_token, config.telegram_chat_id
        )
    except Exception as error:
        operation_log(
            f"알림 재시도 실패 ({type(error).__name__})", error=True
        )
    try:
        results = schedule_registered_projects(
            connection, state_dir, config,
            **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
        )
    except SelfMergeRuntimeHandoff:
        return 0
    if any(item.result.kind is ProjectRunKind.RUNTIME_HANDOFF for item in results):
        return 0
    try:
        notifications.dispatch_pending(
            connection, config.telegram_token, config.telegram_chat_id
        )
    except Exception as error:
        operation_log(
            f"알림 재시도 실패 ({type(error).__name__})", error=True
        )
    return 1 if any(item.result.exit_code for item in results) else 0


def configure_registry(
    args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
) -> int:
    if args.config_command == "init":
        load_managed_config(config_path)
        with connect_database(state_dir, create=True):
            pass
        operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0

    with connect_database(state_dir) as connection:
        if args.config_command == "list":
            projects = project_registry.list_projects(connection)
            if not projects:
                operation_print("등록된 프로젝트가 없습니다.")
            for project in projects:
                approval = "approved" if project.agents_approved else "common"
                operation_print(
                    f"{project.repo}\t{project.base_branch}\t"
                    f"{project.provider}\tAGENTS:{approval}"
                )
            return 0
        if args.config_command == "add":
            config = load_managed_config(config_path)
            project = add_registered_project(
                connection,
                Path(args.repository),
                config_path=config.path,
                provider=args.provider,
                requested_base=args.base,
                approve_agents_mismatch=args.approve_agents_mismatch,
                state_dir=state_dir,
            )
            approval = " (AGENTS.md 불일치 승인 저장)" if project.agents_approved else ""
            operation_print(
                f"프로젝트 등록 완료: {project.repo} "
                f"({project.base_branch}, {project.provider}){approval}"
            )
            return 0
        repo = str(Path(args.repository).expanduser().resolve())
        project_registry.remove_project(connection, repo)
        operation_print(f"프로젝트 등록 해제 완료: {repo}")
        return 0


def _document_drafts_command(
    args: argparse.Namespace, state_dir: Path, config_path: Path,
) -> int:
    from loop_manager_internal.documents import document_drafts

    repository = Path(args.repository).expanduser().resolve()
    if not is_loop_manager_repository(repository):
        raise document_drafts.DocumentDraftError(
            "일회성 문서 수정본은 Loop Manager 저장소 자체에서만 사용할 수 있습니다."
        )
    repo = str(repository)
    with connect_database(state_dir) as connection:
        project = connection.execute(
            "SELECT base_branch, provider FROM projects WHERE repo = ? AND registered = 1",
            (repo,),
        ).fetchone()
        if project is None:
            raise document_drafts.DocumentDraftError("등록된 Loop Manager 저장소가 아닙니다.")
        base_branch = str(project["base_branch"])
        drafts = document_drafts.DocumentDraftRepository(connection)

        def display_review(draft: document_drafts.DocumentDraft) -> None:
            sys.stdout.flush()
            output = sys.stdout.buffer
            output.write(document_drafts.review_artifact_bytes(draft))
            output.flush()

        if args.draft_command == "list":
            for path, findings in drafts.candidates(repo).items():
                operation_print(f"candidate\t{path}\t{','.join(findings)}")
            for draft in drafts.list(repo):
                operation_print(
                    f"{draft.state}\t{draft.draft_id}\t{draft.proposed_hash}\t"
                    f"{draft.target_path}"
                )
            return 0
        if args.draft_command == "show":
            draft = drafts.get(args.draft_id)
            if draft.repo != repo:
                raise document_drafts.DocumentDraftError("다른 저장소의 수정본입니다.")
            display_review(draft)
            return 0
        _recorded_base_fetch(
            repository, state_dir, base_branch, owner="project",
        )
        if args.draft_command == "generate":
            prompt, identity = drafts.generation_prompt(
                repo, repository, base_branch, args.target,
            )
            result = _coerce_agent_result(
                run_agent(
                    str(project["provider"]), ROOT, state_dir, config_path,
                    "planner", prompt, read_only=True,
                    connection=connection,
                    invocation=InvocationContext(repo, None),
                ),
                None,
            )
            if result.code:
                raise document_drafts.DocumentDraftError(
                    "문서 수정본 Agent가 성공하지 못했습니다. 원본과 결정은 그대로입니다."
                )
            generated = document_drafts.parse_generated_draft(result.message)
            if generated is None:
                operation_print("의미 보존을 확정하지 못해 수정본을 만들지 않았습니다.")
                return 0
            draft = drafts.create(
                repo, repository, base_branch, args.target, *generated,
                expected_identity=identity,
            )
            connection.commit()
            display_review(draft)
        elif args.draft_command == "create":
            content_file = Path(args.content_file).expanduser().resolve()
            if content_file.is_relative_to(repository):
                raise document_drafts.DocumentDraftError(
                    "수정본 입력 파일은 source checkout 밖에 두어야 합니다."
                )
            draft = drafts.create(
                repo, repository, base_branch, args.target,
                content_file.read_bytes(), args.meaning_evidence,
            )
            connection.commit()
            display_review(draft)
        elif args.draft_command == "decide":
            draft = drafts.get(args.draft_id)
            if draft.repo != repo:
                raise document_drafts.DocumentDraftError("다른 저장소의 수정본입니다.")
            draft = drafts.decide(
                args.draft_id, args.proposed_hash, args.decision,
                repository, base_branch,
                uncertain_reason=args.uncertain_reason,
            )
            connection.commit()
            operation_print(
                f"{draft.state}\t{draft.draft_id}\t{draft.proposed_hash}\t"
                f"{draft.decision_revision}"
            )
            if draft.decision_reason:
                operation_print(draft.decision_reason)
        else:
            for draft in drafts.approved(repo, repository, base_branch):
                operation_print(
                    f"approved\t{draft.draft_id}\t{draft.proposed_hash}\t"
                    f"{draft.target_path}"
                )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="기본 운영체제 상태 디렉터리 대신 사용할 경로")
    parser.add_argument("--config", default=str(ROOT / "config/config.ini"), help="config.ini 경로")
    parser.add_argument(
        "--runtime-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--runtime-source", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-sha", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-mutation-id", help=argparse.SUPPRESS)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help=argparse.SUPPRESS)

    configuration = subcommands.add_parser(
        "config", help="상태 DB와 프로젝트 registry를 명시적으로 관리"
    )
    config_commands = configuration.add_subparsers(
        dest="config_command", required=True
    )
    config_commands.add_parser("init", help="상태 DB 초기화")
    config_commands.add_parser("list", help="등록 프로젝트 목록 조회")
    add = config_commands.add_parser("add", help="프로젝트 검증 및 등록")
    add.add_argument("repository", help="대상 Git 저장소 루트")
    add.add_argument("--base", help="기준 branch. 생략하면 origin/HEAD 사용")
    add.add_argument(
        "--provider",
        choices=sorted(AGENT_PROVIDERS),
        default="codex",
        help="프로젝트 Agent provider (기본값: codex)",
    )
    add.add_argument(
        "--approve-agents-mismatch",
        action="store_true",
        help="현재 AGENTS.md 해시에 결합해 공통 규칙 불일치를 승인",
    )
    remove = config_commands.add_parser("remove", help="프로젝트 등록 해제")
    remove.add_argument("repository", help="등록 해제할 저장소 루트")

    drafts = subcommands.add_parser(
        "document-drafts", help="Loop Manager 전수 평가 문서 수정본 검토와 결정"
    )
    drafts.add_argument("repository", help="등록된 Loop Manager 저장소 루트")
    draft_commands = drafts.add_subparsers(dest="draft_command", required=True)
    draft_commands.add_parser("list", help="문서 후보와 저장된 수정본 조회")
    generate_draft = draft_commands.add_parser("generate", help="평가 발견에서 읽기 전용 수정본 생성")
    generate_draft.add_argument("target", help="평가 결과의 활성 문서 경로")
    create_draft = draft_commands.add_parser("create", help="외부 파일의 수정본 저장")
    create_draft.add_argument("target", help="평가 결과의 활성 문서 경로")
    create_draft.add_argument("content_file", help="원본 checkout 밖의 UTF-8 수정본 파일")
    create_draft.add_argument("--meaning-evidence", required=True,
                              help="기존 의미가 보존된 구체적인 근거")
    show_draft = draft_commands.add_parser("show", help="전체 원본·수정본·diff 조회")
    show_draft.add_argument("draft_id")
    decide_draft = draft_commands.add_parser("decide", help="정확한 수정본 승인 또는 거절")
    decide_draft.add_argument("draft_id")
    decide_draft.add_argument("proposed_hash", help="show의 Proposed SHA256")
    decide_draft.add_argument("decision", choices=("approve", "reject"))
    decide_draft.add_argument("--uncertain-reason", default="",
                              help="의미 보존이 불확실하면 승인 대신 새 검토 요구")
    draft_commands.add_parser("approved", help="현재 원본에 유효한 승인 조회")

    run = subcommands.add_parser("run", help="등록 프로젝트의 다음 작업을 처리")
    run.add_argument(
        "--skip-if-running",
        action="store_true",
        help="다른 Loop Manager가 실행 중이면 스케줄러용으로 조용히 성공 종료",
    )
    return parser


def execute_runtime_command(
    args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    config = load_managed_config(config_path)
    if args.command == "init":
        with connect_database(state_dir, create=True):
            pass
        operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0
    if args.command == "config":
        return configure_registry(args, state_dir, config_path)
    if args.command == "document-drafts":
        return _document_drafts_command(args, state_dir, config_path)
    with connect_database(state_dir) as connection:
        agent_invocation_state.InvocationRepository(connection).reconcile_interrupted()
        return run_registered_projects(
            connection, state_dir, config,
            **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
        )


def main() -> int:
    raw_arguments = sys.argv[1:]
    args = build_parser().parse_args(raw_arguments)
    state_dir = state_directory(args.state_dir)
    config_path = source_config_path(ROOT, args.config)
    try:
        if args.runtime_child:
            if not args.runtime_source or not args.runtime_sha:
                raise RuntimeError("runtime 자식 소유권 정보가 없습니다.")
            source = Path(args.runtime_source).expanduser().resolve()
            validate_runtime_child(source, state_dir, str(args.runtime_sha))
            runtime_mutation = validate_runtime_mutation(
                source, state_dir, str(args.runtime_sha), args.runtime_mutation_id,
            )
            if runtime_mutation is not None and not runtime_mutation.legacy:
                base = bootstrap_base_branch(source, state_dir, repository_origin(source))
                validate_bootstrap_revision(source, base, str(args.runtime_sha))
            return execute_runtime_command(
                args, state_dir, config_path, runtime_mutation,
            )

        source = ROOT
        mutation_id = secrets.token_hex(16)
        with process_lock(state_dir, identity=f"bootstrap:{mutation_id}") as lock_handle:
            runtime = prepare_bootstrap_runtime(source, state_dir, mutation_id)
            if isinstance(runtime, BootstrapRefreshHandoff):
                if args.command != "run":
                    command_name = (
                        f"config {args.config_command}"
                        if args.command == "config" else args.command
                    )
                    raise RuntimeError(
                        f"bootstrap source를 origin/{runtime.base_branch} "
                        f"{runtime.commit_sha}로 갱신해 {command_name} 명령은 "
                        "실행하지 않았습니다. 같은 명령을 다시 실행하세요."
                    )
                return 0
            _write_process_lock_identity(lock_handle, f"runtime:{mutation_id}")
            return run_runtime_child(
                source,
                state_dir,
                config_path,
                runtime,
                raw_arguments,
            )
    except ProcessLockBusyError as error:
        if args.command == "run" and getattr(args, "skip_if_running", False):
            return 0
        operation_log(f"오류: {error}", error=True)
        return 1
    except (
        ManagedConfigError,
        project_registry.ProjectRegistryError,
        RuntimeError,
        OSError,
        sqlite3.DatabaseError,
        json.JSONDecodeError,
    ) as error:
        operation_log(f"오류: {error}", error=True)
        return 1


def _owned_checkpoint_remote_oid(
    connection: sqlite3.Connection,
    repo_path: Path,
    request: blocked_checkpoint.CheckpointRequest,
    plan: sqlite3.Row,
    current: GitSnapshot,
) -> str | None:
    """Prove a changed tracking ref against durable publication and live origin."""
    if not request.branch or current.branch != request.branch:
        return None
    remote_ref = f"refs/remotes/origin/{request.branch}"
    local_ref = f"refs/heads/{request.branch}"
    tracked = current.refs.get(remote_ref)
    if tracked is None or tracked[1] or current.refs.get(local_ref) != (current.head, ""):
        return None
    candidate = tracked[0]
    published = str(plan["publication_remote_oid"] or "")
    durable = candidate == published and bool(published)
    if not durable and request.local_oid == candidate and request.tree_oid and request.commit_identity:
        try:
            durable = (
                current.head == candidate
                and git(repo_path, "rev-parse", f"{candidate}^{{tree}}").stdout.strip()
                == request.tree_oid
                and f"Checkpoint-Identity: {request.commit_identity}" in
                git(repo_path, "show", "-s", "--format=%B", candidate).stdout
            )
        except RuntimeError:
            durable = False
    if not durable:
        return None
    try:
        observed = git(
            repo_path, "ls-remote", "--heads", "origin",
            f"refs/heads/{request.branch}", check=False,
        )
    except RuntimeError:
        return None
    rows = [line.split() for line in observed.stdout.splitlines() if line.strip()]
    if observed.returncode or rows != [[candidate, f"refs/heads/{request.branch}"]]:
        return None
    return candidate


def _guidance_checkpoint_commit_evidence(
    repo_path: Path, plan_id: str, oid: str,
) -> str | None:
    """Return the original guidance evidence only for an intact Manager commit."""
    try:
        message = git(repo_path, "show", "-s", "--format=%B", oid).stdout
        parent_line = git(repo_path, "show", "-s", "--format=%P", oid).stdout.strip()
        parents = parent_line.split()
        tree = git(repo_path, "rev-parse", f"{oid}^{{tree}}").stdout.strip()
        changed = tuple(sorted(_nul_git_paths(
            repo_path, "diff", "--no-renames", "--name-only", "-z", parents[0], oid,
        ))) if len(parents) == 1 else ()
        author = git(repo_path, "show", "-s", "--format=%an <%ae>%n%cn <%ce>", oid).stdout.splitlines()
        fields = dict(line.split(": ", 1) for line in message.splitlines() if ": " in line)
        evidence = json.loads(fields["Evidence"])
        paths = tuple(sorted(
            line[2:] for line in message.split("Paths:\n", 1)[1].splitlines()
            if line.startswith("- ") and line != "- (none)"
        ))
        if (
            len(parents) != 1
            or author != ["Loop Manager <loop-manager@localhost>"] * 2
            or not isinstance(evidence, str)
            or fields.get("Reason") != GUIDANCE_BLOCK_REASON
            or fields.get("Scope") != "plan"
            or fields.get("Evidence-SHA256") != hashlib.sha256(evidence.encode()).hexdigest()
            or paths != changed
            or message.splitlines()[0] != f"[{plan_id}] BLOCKED checkpoint"
            or fields.get("Checkpoint-Identity") != blocked_checkpoint.checkpoint_identity(
                plan_id=plan_id, reason_code=GUIDANCE_BLOCK_REASON,
                evidence=evidence, parent_oid=parents[0], tree_oid=tree,
                included_paths=paths,
            )
        ):
            return None
        return evidence
    except (KeyError, ValueError, IndexError, RuntimeError, json.JSONDecodeError):
        return None


def _BlockedCheckpointAdapter(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path, repo: str,
    *, reconciliation_scope: bool = False, project_recovery_scope: bool = False,
) -> checkpoint_adapter.BlockedCheckpointAdapter:
    return checkpoint_adapter.BlockedCheckpointAdapter(
        connection, repo_path, state_dir, repo,
        reconciliation_scope=reconciliation_scope,
        project_recovery_scope=project_recovery_scope,
        safety_effects=_checkpoint_safety_effects,
        publication_effects=_checkpoint_publication_effects,
    )


def push_diagnostic_checkpoint(adapter, request, head):
    return checkpoint_adapter.push_diagnostic_checkpoint(_diagnostic_publication_effects(), adapter, request, head)


def dispatch_blocked_checkpoints(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    skip_identities: frozenset[str] = frozenset(),
) -> int:
    """Commit prior block state, then process its network-independent request."""

    connection.commit()
    repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
    adapter = _BlockedCheckpointAdapter(
        connection, repo_path, state_dir, repo
    )
    results = _BlockedCheckpointResults(connection)
    dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(
        repository,
        adapter,
        adapter,
        results,
        max_push_attempts=MAX_TRANSIENT_AGENT_RETRIES,
    )
    try:
        return dispatcher.dispatch(repo, skip_identities=skip_identities)
    except blocked_checkpoint.CheckpointPushError as error:
        for request in repository.pending(repo):
            if request.request_identity in skip_identities:
                continue
            repository.finish_unpublished(
                request, str(error), retryable=error.retryable
            )
            results.finalized(request)
        return 0
    except (OSError, RuntimeError) as error:
        for request in repository.pending(repo):
            if request.request_identity in skip_identities:
                continue
            repository.finish_unpublished(
                request,
                f"checkpoint 처리 실패: {error}",
                retryable=True,
            )
            results.finalized(request)
        return 0


def _execution_outcome_step_result(outcome: ExecutionOutcome) -> int:
    """Translate a typed execution result at the process adapter boundary."""

    if (outcome.recovery is not None
            and outcome.recovery.action is recovery_runtime.RecoveryAction.CORRECT
            and outcome.context is not None):
        reason = outcome.recovery.observation.reason
        if (
            reason is recovery_runtime.RecoveryReason.WORKER_CORRECTION
            and outcome.context.phase is ExecutionPhase.IMPLEMENTING
        ) or (
            reason is recovery_runtime.RecoveryReason.REBASE_CONFLICT
            and outcome.context.phase is not ExecutionPhase.IMPLEMENTING
        ):
            # Stop after an initial Manager rebase conflict or a Worker output
            # protocol error; the saved session receives that feedback next run.
            return 1
    if outcome.kind in {
        ExecutionOutcomeKind.SUCCESS,
        ExecutionOutcomeKind.STABLE_WAIT,
    }:
        return 0
    if outcome.kind in {
        ExecutionOutcomeKind.PHASE_ADVANCED,
        ExecutionOutcomeKind.RETRY,
    }:
        return PROJECT_STEP_RETRY
    if outcome.kind is ExecutionOutcomeKind.TRANSIENT_PAUSE:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is ExecutionOutcomeKind.USAGE_PAUSE:
        return PROJECT_STEP_USAGE_PAUSE
    if outcome.kind is ExecutionOutcomeKind.PLANNING_PENDING:
        return PROJECT_STEP_PLANNING_PENDING
    return 1


def _typed_execution_git(path: Path, *arguments: str, **options: object):
    """Keep Git operation identity when a shared subprocess helper fails."""

    command = arguments[0] if arguments else ""
    reason = {
        "fetch": recovery_runtime.RecoveryReason.FETCH_FAILED,
        "status": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "diff": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "rev-parse": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "merge-base": recovery_runtime.RecoveryReason.STATUS_FAILED,
        "add": recovery_runtime.RecoveryReason.STAGE_FAILED,
        "commit": recovery_runtime.RecoveryReason.STAGE_FAILED,
        "rebase": recovery_runtime.RecoveryReason.REBASE_CONTINUE_FAILED,
        "-c": recovery_runtime.RecoveryReason.REBASE_CONTINUE_FAILED,
    }.get(command, recovery_runtime.RecoveryReason.WORKTREE_RECOVERY_FAILED)
    try:
        result = git(path, *arguments, **options)
        if result.returncode and is_git_lock_failure(result.stdout):
            raise recovery_runtime.RecoveryFailure(
                recovery_runtime.RecoveryReason.GIT_LOCK,
                result.stdout[-8000:],
            )
        return result
    except recovery_runtime.RecoveryFailure:
        raise
    except GitLockError as error:
        raise recovery_runtime.RecoveryFailure(
            recovery_runtime.RecoveryReason.GIT_LOCK, str(error)
        ) from error
    except (RuntimeError, OSError) as error:
        raise recovery_runtime.RecoveryFailure(reason, str(error)) from error


def _typed_execution_command(*arguments: object, **options: object):
    try:
        return command(*arguments, **options)
    except (RuntimeError, OSError) as error:
        raise recovery_runtime.RecoveryFailure(
            recovery_runtime.RecoveryReason.PUBLICATION_RETRY, str(error)
        ) from error


def _ExecutionLifecyclePorts(connection, runtime_repository, **identity):
    """Bind named effects to legacy helpers, preserving their patch points."""
    verify_timeout_seconds = identity.pop("verify_timeout_seconds", 600)
    state = _ExecutionStatePort(connection, runtime_repository, identity["repo"])
    effects = execution_handlers.ExecutionEffects(
        state=state,
        checkpoint=execution_checkpoint_state.ExecutionCheckpointRepository(connection),
        contract=SimpleNamespace(**{name: globals()[name] for name in (
            'AgentResultKind', 'AgentScopeError', 'ExecPlanMetadataError',
            'GitLockError', 'MAX_REVIEWER_PROTOCOL_ATTEMPTS',
            'MAX_VALIDATION_FINGERPRINT_REPEATS', 'PROJECT_STEP_RETRY',
            'PROJECT_STEP_TRANSIENT_PAUSE', 'PROJECT_STEP_USAGE_PAUSE',
            'PROJECT_STEP_PLANNING_PENDING', 'PublicationSafetyError',
            'REBASE_CONFLICT_PREFIX', 'REBASE_IN_PROGRESS_ERROR',
            'RequirementArchiveCollisionError', 'RequirementArchiveError',
            'ReviewerProtocolError', 'TRANSIENT_AGENT_ERROR_PREFIX',
            'USAGE_LIMIT_ERROR_PREFIX', 'WORKSPACE_OWNED', 'WORKSPACE_RESERVED',
            'WorkerResultError', 'WorkspaceOwnershipError',
            'reviewer_targeted_test_reason',
            'VerifyTerminationUncertain',
        )}),
        workspace=execution_handlers.WorkspaceEffects(
            is_directory=lambda path: path.is_dir(),
            git=state.bind(_typed_execution_git, name="git", connected=False, mutates=False),
            make_worktree=state.bind(make_worktree, name="make_worktree", connected=False, mutates=False),
            branch_has_changes=state.bind(branch_has_changes, name="branch_has_changes", connected=False, mutates=False),
            remote_branch_oid=state.bind(remote_branch_oid, name="remote_branch_oid", connected=False, mutates=False),
            _rebase_in_progress_for_branch=state.bind(_rebase_in_progress_for_branch, name="_rebase_in_progress_for_branch", connected=False, mutates=False),
            validate_execplan_workspace_identity=state.bind(validate_execplan_workspace_identity, name="validate_execplan_workspace_identity", connected=False, mutates=False),
            validate_managed_worktree=state.bind(validate_managed_worktree, name="validate_managed_worktree", connected=False, mutates=False),
            _plan_id_from_path=state.bind(_plan_id_from_path, name="_plan_id_from_path", connected=False, mutates=False),
            reserve_execplan_workspace=state.bind(reserve_execplan_workspace, name="reserve_execplan_workspace", connected=True, mutates=True),
            mark_execplan_workspace_owned=state.bind(mark_execplan_workspace_owned, name="mark_execplan_workspace_owned", connected=True, mutates=True),
            resume_pr_conflict_rebase=state.bind(resume_pr_conflict_rebase, name="resume_pr_conflict_rebase", connected=True, mutates=True),
            git_operation_entries=state.bind(_git_operation_entries, name="git_operation_entries", connected=False, mutates=False),
            current_rebase_service=lambda context, **options: _current_rebase_service(
                connection, identity["repo_path"], identity["state_dir"], identity["repo"],
                context.plan.plan_id, context.worktree, context.branch, **options),
            live_remote_branch_oid=state.bind(
                lambda repo, branch: manager_git_journal.live_remote_branch_oid(git, repo, branch),
                name="live_remote_branch_oid", connected=False, mutates=False),
        ),
        agent=execution_handlers.AgentEffects(
            begin_agent_scope=state.bind(begin_agent_scope, name="begin_agent_scope", connected=True, mutates=True),
            capture_git_snapshot=state.bind(capture_git_snapshot, name="capture_git_snapshot", connected=False, mutates=False),
            path_signature=state.bind(_path_signature, name="path_signature", connected=False, mutates=False),
            clear_agent_scope=state.bind(clear_agent_scope, name="clear_agent_scope", connected=True, mutates=True),
            validate_agent_scope=state.bind(
                lambda *args, **kwargs: validate_agent_scope(*args, connection=connection, **kwargs),
                name="validate_agent_scope", connected=False, mutates=False),
            validate_worker_workspace_scope=state.bind(
                lambda *args, **kwargs: validate_worker_workspace_scope(*args, connection=connection, **kwargs),
                name="validate_worker_workspace_scope", connected=False, mutates=False),
            _worktree_execplan_input_fingerprint=state.bind(_worktree_execplan_input_fingerprint, name="_worktree_execplan_input_fingerprint", connected=False, mutates=False),
            _revision_execplan_input_fingerprint=state.bind(_revision_execplan_input_fingerprint, name="_revision_execplan_input_fingerprint", connected=False, mutates=False),
            _guidance_worker_targets=state.bind(_guidance_worker_targets, name="_guidance_worker_targets", connected=True, mutates=False),
            guidance_resume_context=state.bind(guidance_resume_context, name="guidance_resume_context", connected=True, mutates=False),
            _published_plan_allows_common_plans=state.bind(_published_plan_allows_common_plans, name="_published_plan_allows_common_plans", connected=False, mutates=False),
            is_loop_manager_repository=state.bind(is_loop_manager_repository, name="is_loop_manager_repository", connected=False, mutates=False),
            inspect_worker_guidance_result=state.bind(inspect_worker_guidance_result, name="inspect_worker_guidance_result", connected=True, mutates=True),
            validate_guidance_publication=state.bind(validate_guidance_publication, name="validate_guidance_publication", connected=True, mutates=True),
            run_agent=state.bind(_run_agent_with_connection, name="run_agent", connected=True, mutates=False),
            run_agent_with_retries=state.bind(run_agent_with_retries, name="run_agent_with_retries", connected=False, mutates=False),
            worker_prompt=state.bind(worker_prompt, name="worker_prompt", connected=False, mutates=False),
            reviewer_prompt=state.bind(reviewer_prompt, name="reviewer_prompt", connected=False, mutates=False),
            resolve_worker_profile=lambda config_path, provider, level, worktree: _agent_profile(
                config_path, provider, "worker", level, worktree,
            ),
        ),
        validation=execution_handlers.ValidationEffects(
            verify=state.bind(
                lambda worktree: verify(
                    worktree, verify_timeout_seconds,
                ),
                name="verify", connected=False, mutates=False,
            ),
            block_verify_timeout_limit=state.bind(
                block_verify_timeout_limit, name="block_verify_timeout_limit",
                connected=True, mutates=True,
            ),
            parse_worker_result=state.bind(parse_worker_result, name="parse_worker_result", connected=False, mutates=False),
            parse_reviewer_result=state.bind(parse_reviewer_result, name="parse_reviewer_result", connected=False, mutates=False),
            validate_active_execplan_metadata=state.bind(validate_active_execplan_metadata, name="validate_active_execplan_metadata", connected=False, mutates=False),
            ensure_worker_level_state=state.bind(ensure_worker_level_state, name="ensure_worker_level_state", connected=True, mutates=True),
            clear_validation_failure_history=state.bind(clear_validation_failure_history, name="clear_validation_failure_history", connected=True, mutates=True),
            transition_validation_failure=state.bind(transition_validation_failure, name="transition_validation_failure", connected=True, mutates=True),
        ),
        feedback=execution_handlers.FeedbackEffects(
            repository=validation_feedback_state.ValidationFeedbackRepository(connection),
            fingerprints=validation_failure_fingerprints,
        ),
        archive=execution_handlers.ArchiveEffects(
            requirement_archive_sources=state.bind(requirement_archive_sources, name="requirement_archive_sources", connected=True, mutates=False),
            requirement_archive_snapshot=state.bind(requirement_archive_snapshot, name="requirement_archive_snapshot", connected=True, mutates=False),
            requirement_archive_candidates=state.bind(requirement_archive_candidates, name="requirement_archive_candidates", connected=True, mutates=False),
            requirement_archive_move_set=state.bind(requirement_archive_move_set, name="requirement_archive_move_set", connected=False, mutates=False),
            requirement_archive_reconciliation_error=state.bind(requirement_archive_reconciliation_error, name="requirement_archive_reconciliation_error", connected=False, mutates=False),
            requirement_archive_touched_sources=state.bind(requirement_archive_touched_sources, name="requirement_archive_touched_sources", connected=False, mutates=False),
        ),
        publication=execution_handlers.PublicationEffects(
            pr_for_branch=state.bind(pr_for_branch, name="pr_for_branch", connected=False, mutates=False),
            reconcile_existing_pull_request=state.bind(reconcile_existing_pull_request, name="reconcile_existing_pull_request", connected=True, mutates=True),
            record_remote_publication_branch=state.bind(record_remote_publication_branch, name="record_remote_publication_branch", connected=True, mutates=True),
            push_managed_publication_branch=state.bind(push_managed_publication_branch, name="push_managed_publication_branch", connected=True, mutates=True),
            push_owned_pr_conflict_recovery=state.bind(push_owned_pr_conflict_recovery, name="push_owned_pr_conflict_recovery", connected=True, mutates=True),
            published_execplan=state.bind(published_execplan, name="published_execplan", connected=False, mutates=False),
            build_pr_body=state.bind(build_pr_body, name="build_pr_body", connected=False, mutates=False),
            command=state.bind(_typed_execution_command, name="command", connected=False, mutates=False),
        ),
        blocking=execution_handlers.BlockingEffects(
            block_active_execplan=state.bind(block_active_execplan, name="block_active_execplan", connected=True, mutates=True),
            block_active_execplan_from_revision=state.bind(block_active_execplan_from_revision, name="block_active_execplan_from_revision", connected=True, mutates=True),
        ),
        events=execution_handlers.EventsEffects(
            operation_log=state.bind(operation_log, name="operation_log", connected=False, mutates=False),
            operation_print=state.bind(operation_print, name="operation_print", connected=False, mutates=False),
        ),
        revision=execution_handlers.RevisionEffects(
            reconcile_catalog=state.bind(reconcile_execution_execplan_catalog, name="reconcile_execplan_catalog", connected=True, mutates=True),
            source_fingerprint=state.bind(_revision_execplan_source_fingerprint, name="_revision_execplan_source_fingerprint", connected=False, mutates=False),
            body_hash=state.bind(_revision_execplan_body_hash, name="_revision_execplan_body_hash", connected=False, mutates=False),
            changed_paths=state.bind(_revision_changed_paths, name="_revision_changed_paths", connected=False, mutates=False),
            unmet_dependencies=state.bind(unmet_execplan_dependencies, name="unmet_execplan_dependencies", connected=True, mutates=False),
        ),
    )
    return execution_handlers.ExecutionHandlers(effects, **identity)


if __name__ == "__main__":
    raise SystemExit(main())
