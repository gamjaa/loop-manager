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


from loop_manager_internal.application.compatibility import bind_effects as _bind_effects
from loop_manager_internal.agents import agent_adapter
from loop_manager_internal.agents import scope_adapter
from loop_manager_internal.agents import scope_state_service
from loop_manager_internal.application import commands, manual_work
from loop_manager_internal.planning import manual_plan_generation
from loop_manager_internal.application import contracts
from loop_manager_internal.application import project_outcomes
from loop_manager_internal.application import project_runner
from loop_manager_internal.application import project_service
from loop_manager_internal.application import scheduler
from loop_manager_internal.application import self_runtime
from loop_manager_internal.catalog import archive_service
from loop_manager_internal.catalog import catalog_service
from loop_manager_internal.catalog import legacy_migration
from loop_manager_internal.catalog import project_selection
from loop_manager_internal.catalog import requirement_archive_service
from loop_manager_internal.documents import guidance_service
from loop_manager_internal.documents import pr_description
from loop_manager_internal.execution import execution_ports
from loop_manager_internal.execution import plan_validation
from loop_manager_internal.execution import result_validation
from loop_manager_internal.git import base_fetch_service
from loop_manager_internal.git import cleanup_service
from loop_manager_internal.git import direct_rebase_service
from loop_manager_internal.git import git_adapter
from loop_manager_internal.git import merge_cleanup_service
from loop_manager_internal.git import publication_service
from loop_manager_internal.git import workspace_service
from loop_manager_internal.maintenance import cycle_service
from loop_manager_internal.maintenance import maintenance_publication
from loop_manager_internal.maintenance import maintenance_validation
from loop_manager_internal.maintenance import review_ports
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
from loop_manager_internal.recovery import block_service
from loop_manager_internal.recovery import blocked_agent_service
from loop_manager_internal.recovery import blocked_plan_service
from loop_manager_internal.recovery import checkpoint_service
from loop_manager_internal.recovery import diagnostic_preservation
from loop_manager_internal.recovery import pr_ownership
from loop_manager_internal.recovery import project_block_service
from loop_manager_internal.recovery import recovery_adapter
from loop_manager_internal.recovery import waiting_pr_service
from loop_manager_internal.runtime import bootstrap_adapter
from loop_manager_internal.runtime import handoff_gate
from loop_manager_internal.runtime import operation_events
from loop_manager_internal.runtime import registration_service
from loop_manager_internal.storage import project_state_adapter


# Verified public helpers and CLI composition. No resources are opened here.
ROOT = Path(__file__).resolve().parent

MAX_VALIDATION_FINGERPRINT_REPEATS = contracts.MAX_VALIDATION_FINGERPRINT_REPEATS

MAX_REVIEWER_PROTOCOL_ATTEMPTS = contracts.MAX_REVIEWER_PROTOCOL_ATTEMPTS

PROJECT_STEP_TRANSIENT_PAUSE = contracts.PROJECT_STEP_TRANSIENT_PAUSE

PROJECT_STEP_USAGE_PAUSE = contracts.PROJECT_STEP_USAGE_PAUSE

TRANSIENT_AGENT_ERROR_PREFIX = contracts.TRANSIENT_AGENT_ERROR_PREFIX

USAGE_LIMIT_ERROR_PREFIX = contracts.USAGE_LIMIT_ERROR_PREFIX

PLAN_SHA_BACKFILL_ERROR = contracts.PLAN_SHA_BACKFILL_ERROR

PRIORITY_ORDER = contracts.PRIORITY_ORDER

ACTIVE_EXECPLAN_STATUSES = project_state_adapter.ACTIVE_EXECPLAN_STATUSES

WAITING_EXECPLAN_STATUSES = project_state_adapter.WAITING_EXECPLAN_STATUSES

KNOWN_EXECPLAN_STATUSES = project_state_adapter.KNOWN_EXECPLAN_STATUSES

FIELDS = project_state_adapter.FIELDS

CodexResult = contracts.CodexResult

_FUTURE_IDEA_ITEM = contracts._FUTURE_IDEA_ITEM

_ANSI_ESCAPE = contracts._ANSI_ESCAPE

_TIMESTAMP_NOISE = contracts._TIMESTAMP_NOISE

_DURATION_NOISE = contracts._DURATION_NOISE

_TEMP_PATH_NOISE = contracts._TEMP_PATH_NOISE

_TEMP_DIRECTORY_CONTEXT = contracts._TEMP_DIRECTORY_CONTEXT

_REVIEW_ISSUE_ID = contracts._REVIEW_ISSUE_ID

_REVIEW_ISSUE_DECLARATION = contracts._REVIEW_ISSUE_DECLARATION

_REVIEW_ISSUE_SYMBOL = contracts._REVIEW_ISSUE_SYMBOL

_REVIEW_ISSUE_CATEGORY = contracts._REVIEW_ISSUE_CATEGORY

_REVIEW_VERDICT_DECLARATION = contracts._REVIEW_VERDICT_DECLARATION

_REVIEW_VERDICT = contracts._REVIEW_VERDICT

_TARGETED_TEST_FAILURE = contracts._TARGETED_TEST_FAILURE

_TARGETED_TEST_EXCEPTION_FAILURE = contracts._TARGETED_TEST_EXCEPTION_FAILURE

_TARGETED_TEST_SUCCESS = contracts._TARGETED_TEST_SUCCESS

_TARGETED_TEST_COMMAND = contracts._TARGETED_TEST_COMMAND

_NO_RUNNABLE_TARGETED_TEST = contracts._NO_RUNNABLE_TARGETED_TEST

_TARGETED_TEST_METADATA = contracts._TARGETED_TEST_METADATA

_INLINE_TEST_COMMAND = contracts._INLINE_TEST_COMMAND

_runtime_handoff_signal = handoff_gate._runtime_handoff_signal

_provider_adapters_effects = _bind_effects(
    agent_adapter._provider_adapters_effects, lambda: agent_adapter.ProviderAdaptersEffectsEffects(
        _agent_profile=lambda *args, **kwargs: _agent_profile(*args, **kwargs),
        _handoff_popen=lambda *args, **kwargs: _handoff_popen(*args, **kwargs),
        _handoff_run=lambda *args, **kwargs: _handoff_run(*args, **kwargs),
        role=lambda *args, **kwargs: role(*args, **kwargs),
        worker_profile=lambda *args, **kwargs: worker_profile(*args, **kwargs),
    ),
)

_invocation_service_effects = _bind_effects(
    agent_adapter._invocation_service_effects, lambda: agent_adapter.InvocationServiceEffectsEffects(
        _agent_profile=lambda *args, **kwargs: _agent_profile(*args, **kwargs),
        _check_runtime_handoff=lambda *args, **kwargs: _check_runtime_handoff(*args, **kwargs),
        _start_agent_invocation=lambda *args, **kwargs: _start_agent_invocation(*args, **kwargs),
        agent_provider=lambda *args, **kwargs: agent_provider(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
    ),
)

_start_agent_invocation = agent_adapter._start_agent_invocation

_command_adapter_effects_git = _bind_effects(
    git_adapter._command_adapter_effects_git, lambda: git_adapter.CommandAdapterEffectsGitEffects(
        _ignored_paths_overlapping_git_update=lambda *args, **kwargs: _ignored_paths_overlapping_git_update(*args, **kwargs),
        _rebase_resume_ignored_overlap=lambda *args, **kwargs: _rebase_resume_ignored_overlap(*args, **kwargs),
        command=lambda *args, **kwargs: command(*args, **kwargs),
        is_git_lock_failure=lambda *args, **kwargs: is_git_lock_failure(*args, **kwargs),
    ),
)

_command_adapter_effects_git_bytes = _bind_effects(
    git_adapter._command_adapter_effects_git_bytes, lambda: git_adapter.CommandAdapterEffectsGitBytesEffects(
        _handoff_run=lambda *args, **kwargs: _handoff_run(*args, **kwargs),
        is_git_lock_failure=lambda *args, **kwargs: is_git_lock_failure(*args, **kwargs),
    ),
)

_github_observation_effects_pr_for_branch = _bind_effects(
    git_adapter._github_observation_effects_pr_for_branch, lambda: git_adapter.GithubObservationEffectsPrForBranchEffects(
        command=lambda *args, **kwargs: command(*args, **kwargs),
    ),
)

_workspace_effects_validate_managed_worktree = _bind_effects(
    git_adapter._workspace_effects_validate_managed_worktree, lambda: git_adapter.WorkspaceEffectsValidateManagedWorktreeEffects(
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        _worktree_catalog=lambda *args, **kwargs: _worktree_catalog(*args, **kwargs),
    ),
)

_workspace_effects_validate_reserved_worktree = _bind_effects(
    git_adapter._workspace_effects_validate_reserved_worktree, lambda: git_adapter.WorkspaceEffectsValidateReservedWorktreeEffects(
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_workspace_effects_make_worktree = _bind_effects(
    git_adapter._workspace_effects_make_worktree, lambda: git_adapter.WorkspaceEffectsMakeWorktreeEffects(
        _managed_worktree_marker=lambda *args, **kwargs: _managed_worktree_marker(*args, **kwargs),
        _managed_worktree_transition_state=lambda *args, **kwargs: _managed_worktree_transition_state(*args, **kwargs),
        _worktree_catalog=lambda *args, **kwargs: _worktree_catalog(*args, **kwargs),
        _write_managed_worktree_receipt=lambda *args, **kwargs: _write_managed_worktree_receipt(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        validate_reserved_worktree=lambda *args, **kwargs: validate_reserved_worktree(*args, **kwargs),
    ),
)

_workspace_effects_make_planning_worktree = _bind_effects(
    git_adapter._workspace_effects_make_planning_worktree, lambda: git_adapter.WorkspaceEffectsMakePlanningWorktreeEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        validate_reserved_worktree=lambda *args, **kwargs: validate_reserved_worktree(*args, **kwargs),
    ),
)

_workspace_effects_validate_execplan_workspace_identity = _bind_effects(
    git_adapter._workspace_effects_validate_execplan_workspace_identity, lambda: git_adapter.WorkspaceEffectsValidateExecplanWorkspaceIdentityEffects(
        _validate_managed_path_boundary=lambda *args, **kwargs: _validate_managed_path_boundary(*args, **kwargs),
        expected_execplan_workspace=lambda *args, **kwargs: expected_execplan_workspace(*args, **kwargs),
    ),
)

_source_sync_effects_base_worktree_snapshot = _bind_effects(
    git_adapter._source_sync_effects_base_worktree_snapshot, lambda: git_adapter.SourceSyncEffectsBaseWorktreeSnapshotEffects(
        _base_local_path_signature=lambda *args, **kwargs: _base_local_path_signature(*args, **kwargs),
        _base_worktree_index_error=lambda *args, **kwargs: _base_worktree_index_error(*args, **kwargs),
        _git_operation_entries=lambda *args, **kwargs: _git_operation_entries(*args, **kwargs),
        _ignored_worktree_paths=lambda *args, **kwargs: _ignored_worktree_paths(*args, **kwargs),
        _index_entries=lambda *args, **kwargs: _index_entries(*args, **kwargs),
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_source_sync_effects_base_paths_overlap = _bind_effects(
    git_adapter._source_sync_effects_base_paths_overlap, lambda: git_adapter.SourceSyncEffectsBasePathsOverlapEffects(
        _paths_overlap=lambda *args, **kwargs: _paths_overlap(*args, **kwargs),
    ),
)

_source_sync_effects_source_transition_state = _bind_effects(
    git_adapter._source_sync_effects_source_transition_state, lambda: git_adapter.SourceSyncEffectsSourceTransitionStateEffects(
        _base_worktree_snapshot=lambda *args, **kwargs: _base_worktree_snapshot(*args, **kwargs),
    ),
)

_source_sync_effects_source_transition_marker = _bind_effects(
    git_adapter._source_sync_effects_source_transition_marker, lambda: git_adapter.SourceSyncEffectsSourceTransitionMarkerEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_source_sync_effects_source_expected_index = _bind_effects(
    git_adapter._source_sync_effects_source_expected_index, lambda: git_adapter.SourceSyncEffectsSourceExpectedIndexEffects(
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_source_sync_effects_synchronize_base_worktree = _bind_effects(
    git_adapter._source_sync_effects_synchronize_base_worktree, lambda: git_adapter.SourceSyncEffectsSynchronizeBaseWorktreeEffects(
        _base_paths_overlap=lambda *args, **kwargs: _base_paths_overlap(*args, **kwargs),
        _base_worktree_index_error=lambda *args, **kwargs: _base_worktree_index_error(*args, **kwargs),
        _base_worktree_snapshot=lambda *args, **kwargs: _base_worktree_snapshot(*args, **kwargs),
        _git_operation_entries=lambda *args, **kwargs: _git_operation_entries(*args, **kwargs),
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        _source_expected_index=lambda *args, **kwargs: _source_expected_index(*args, **kwargs),
        _source_transition_marker=lambda *args, **kwargs: _source_transition_marker(*args, **kwargs),
        _source_transition_state=lambda *args, **kwargs: _source_transition_state(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_git_lock_failure=lambda *args, **kwargs: is_git_lock_failure(*args, **kwargs),
    ),
)

_source_sync_effects_ensure_safe_base_worktree = _bind_effects(
    git_adapter._source_sync_effects_ensure_safe_base_worktree, lambda: git_adapter.SourceSyncEffectsEnsureSafeBaseWorktreeEffects(
        synchronize_base_worktree=lambda *args, **kwargs: synchronize_base_worktree(*args, **kwargs),
    ),
)

_source_sync_effects_log_base_worktree_sync = _bind_effects(
    git_adapter._source_sync_effects_log_base_worktree_sync, lambda: git_adapter.SourceSyncEffectsLogBaseWorktreeSyncEffects(
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
    ),
)

_pull_request_recovery_effects_reconcile_existing_pull_request = _bind_effects(
    recovery_adapter._pull_request_recovery_effects_reconcile_existing_pull_request, lambda: recovery_adapter.PullRequestRecoveryEffectsReconcileExistingPullRequestEffects(
        _block_closed_pull_request=lambda *args, **kwargs: _block_closed_pull_request(*args, **kwargs),
        _fetch_pr_conflict_recovery_refs=lambda *args, **kwargs: _fetch_pr_conflict_recovery_refs(*args, **kwargs),
        _is_pr_conflict_preflight_block=lambda *args, **kwargs: _is_pr_conflict_preflight_block(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _record_project_recovery=lambda *args, **kwargs: _record_project_recovery(*args, **kwargs),
        _resume_reopened_pull_request=lambda *args, **kwargs: _resume_reopened_pull_request(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        _validate_active_pr_conflict_recovery=lambda *args, **kwargs: _validate_active_pr_conflict_recovery(*args, **kwargs),
        _validate_blocked_pr_without_conflict=lambda *args, **kwargs: _validate_blocked_pr_without_conflict(*args, **kwargs),
        _validate_pr_conflict_recovery_identity=lambda *args, **kwargs: _validate_pr_conflict_recovery_identity(*args, **kwargs),
        _validate_pr_conflict_recovery_ownership=lambda *args, **kwargs: _validate_pr_conflict_recovery_ownership(*args, **kwargs),
        block_active_execplan_from_revision=lambda *args, **kwargs: block_active_execplan_from_revision(*args, **kwargs),
        classify_pull_request_mergeability=lambda *args, **kwargs: classify_pull_request_mergeability(*args, **kwargs),
        complete_merged_execplan_worktree=lambda *args, **kwargs: complete_merged_execplan_worktree(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
        recover_confirmed_pr_conflict_push=lambda *args, **kwargs: recover_confirmed_pr_conflict_push(*args, **kwargs),
        remote_base=lambda *args, **kwargs: remote_base(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_pull_request_recovery_effects_resume_pr_conflict_rebase = _bind_effects(
    recovery_adapter._pull_request_recovery_effects_resume_pr_conflict_rebase, lambda: recovery_adapter.PullRequestRecoveryEffectsResumePrConflictRebaseEffects(
        _complete_direct_rebase_checkpoint=lambda *args, **kwargs: _complete_direct_rebase_checkpoint(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _finish_owned_direct_rebase=lambda *args, **kwargs: _finish_owned_direct_rebase(*args, **kwargs),
        _journaled_direct_rebase=lambda *args, **kwargs: _journaled_direct_rebase(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        block_active_execplan=lambda *args, **kwargs: block_active_execplan(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_git_lock_failure=lambda *args, **kwargs: is_git_lock_failure(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

_scope_reconciliation_effects_resume_project_block_if_safe = _bind_effects(
    recovery_adapter._scope_reconciliation_effects_resume_project_block_if_safe, lambda: recovery_adapter.ScopeReconciliationEffectsResumeProjectBlockIfSafeEffects(
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _clear_project_block_without_losing_planning_error=lambda *args, **kwargs: _clear_project_block_without_losing_planning_error(*args, **kwargs),
        _reconcile_rebase_evidence_block=lambda *args, **kwargs: _reconcile_rebase_evidence_block(*args, **kwargs),
        _reconciled_manager_scope_metadata=lambda *args, **kwargs: _reconciled_manager_scope_metadata(*args, **kwargs),
        _requeue_blocked_execplan=lambda *args, **kwargs: _requeue_blocked_execplan(*args, **kwargs),
        _resume_owned_checkpoint_project_block=lambda *args, **kwargs: _resume_owned_checkpoint_project_block(*args, **kwargs),
        _with_proven_legacy_ignore_config=lambda *args, **kwargs: _with_proven_legacy_ignore_config(*args, **kwargs),
        _normalize_legacy_ignored_snapshot=lambda *args, **kwargs: _normalize_legacy_ignored_snapshot(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        validate_planning_worktree_ownership=lambda *args, **kwargs: validate_planning_worktree_ownership(*args, **kwargs),
    ),
)

_scope_reconciliation_effects_resume_plan_blocks = _bind_effects(
    recovery_adapter._scope_reconciliation_effects_resume_plan_blocks, lambda: recovery_adapter.ScopeReconciliationEffectsResumePlanBlocksEffects(
        ROOT=ROOT,
        _blocked_plan_reconciliation_effects=lambda *args, **kwargs: _blocked_plan_reconciliation_effects(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _reconcile_published_blocked_checkpoint=lambda *args, **kwargs: _reconcile_published_blocked_checkpoint(*args, **kwargs),
        _reconcile_rebase_evidence_block=lambda *args, **kwargs: _reconcile_rebase_evidence_block(*args, **kwargs),
        _resume_interrupted_blocked_rebase=lambda *args, **kwargs: _resume_interrupted_blocked_rebase(*args, **kwargs),
        _resume_legacy_review_count_block=lambda *args, **kwargs: _resume_legacy_review_count_block(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
        load_managed_config=lambda *args, **kwargs: load_managed_config(*args, **kwargs),
        plan_blocked_execplans=lambda *args, **kwargs: plan_blocked_execplans(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
        resume_blocked_execplan_if_inputs_changed=lambda *args, **kwargs: resume_blocked_execplan_if_inputs_changed(*args, **kwargs),
        resume_retired_guidance_block=lambda *args, **kwargs: resume_retired_guidance_block(*args, **kwargs),
        unmet_execplan_dependencies=lambda *args, **kwargs: unmet_execplan_dependencies(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_archive_git_effects = _bind_effects(
    git_adapter._archive_git_effects, lambda: git_adapter.ArchiveGitEffectsEffects(
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        _validate_managed_path_boundary=lambda *args, **kwargs: _validate_managed_path_boundary(*args, **kwargs),
        command=lambda *args, **kwargs: command(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        is_git_lock_failure=lambda *args, **kwargs: is_git_lock_failure(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_checkpoint_safety_effects = _bind_effects(
    git_adapter._checkpoint_safety_effects, lambda: git_adapter.CheckpointSafetyEffectsEffects(
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _owned_checkpoint_remote_oid=lambda *args, **kwargs: _owned_checkpoint_remote_oid(*args, **kwargs),
        _proven_manager_base_refs=lambda *args, **kwargs: _proven_manager_base_refs(*args, **kwargs),
        _proven_merged_cleanup_paths=lambda *args, **kwargs: _proven_merged_cleanup_paths(*args, **kwargs),
        _published_plan_allows_common_plans=lambda *args, **kwargs: _published_plan_allows_common_plans(*args, **kwargs),
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_checkpoint_publication_effects = _bind_effects(
    git_adapter._checkpoint_publication_effects, lambda: git_adapter.CheckpointPublicationEffectsEffects(
        _git_with_index=lambda *args, **kwargs: _git_with_index(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        command=lambda *args, **kwargs: command(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_diagnostic_publication_effects = _bind_effects(
    git_adapter._diagnostic_publication_effects, lambda: git_adapter.DiagnosticPublicationEffectsEffects(
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_managed_rebase_runtime_effects = _bind_effects(
    recovery_adapter._managed_rebase_runtime_effects, lambda: recovery_adapter.ManagedRebaseRuntimeEffectsEffects(
        _owned_rebase_metadata=lambda *args, **kwargs: _owned_rebase_metadata(*args, **kwargs),
        _path_signature=lambda *args, **kwargs: _path_signature(*args, **kwargs),
        _published_plan_allows_common_plans=lambda *args, **kwargs: _published_plan_allows_common_plans(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _worker_path_allowed=lambda *args, **kwargs: _worker_path_allowed(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        requirement_archive_candidates=lambda *args, **kwargs: (),
        requirement_archive_move_set=lambda *args, **kwargs: requirement_archive_move_set(*args, **kwargs),
        requirement_archive_path=lambda *args, **kwargs: requirement_archive_path(*args, **kwargs),
        validate_active_execplan_metadata=lambda *args, **kwargs: validate_active_execplan_metadata(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_blocked_plan_reconciliation_effects = _bind_effects(
    recovery_adapter._blocked_plan_reconciliation_effects, lambda: recovery_adapter.BlockedPlanReconciliationEffectsEffects(
        ROOT=ROOT,
        _BlockedCheckpointAdapter=lambda *args, **kwargs: _BlockedCheckpointAdapter(*args, **kwargs),
        _blocked_execplan_metadata_error=lambda *args, **kwargs: _blocked_execplan_metadata_error(*args, **kwargs),
        _complete_direct_rebase_checkpoint=lambda *args, **kwargs: _complete_direct_rebase_checkpoint(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _evaluate_remote_checkpoint_cause=lambda *args, **kwargs: _evaluate_remote_checkpoint_cause(*args, **kwargs),
        _journaled_direct_rebase=lambda *args, **kwargs: _journaled_direct_rebase(*args, **kwargs),
        _non_default_index_flags=lambda *args, **kwargs: _non_default_index_flags(*args, **kwargs),
        _reconcile_published_blocked_checkpoint=lambda *args, **kwargs: _reconcile_published_blocked_checkpoint(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
        _worktree_state_signature=lambda *args, **kwargs: _worktree_state_signature(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
        load_managed_config=lambda *args, **kwargs: load_managed_config(*args, **kwargs),
        observe_blocked_plan_remotes=lambda *args, **kwargs: observe_blocked_plan_remotes(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        unmet_execplan_dependencies=lambda *args, **kwargs: unmet_execplan_dependencies(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_project_blocked_recovery_effects = _bind_effects(
    recovery_adapter._project_blocked_recovery_effects, lambda: recovery_adapter.ProjectBlockedRecoveryEffectsEffects(
        _BlockedCheckpointAdapter=lambda *args, **kwargs: _BlockedCheckpointAdapter(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _journaled_direct_rebase=lambda *args, **kwargs: _journaled_direct_rebase(*args, **kwargs),
        _publish_direct_rebase_checkpoint=lambda *args, **kwargs: _publish_direct_rebase_checkpoint(*args, **kwargs),
        _reconcile_published_blocked_checkpoint=lambda *args, **kwargs: _reconcile_published_blocked_checkpoint(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        _worktree_state_signature=lambda *args, **kwargs: _worktree_state_signature(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
        observe_blocked_plan_remotes=lambda *args, **kwargs: observe_blocked_plan_remotes(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        push_diagnostic_checkpoint=lambda *args, **kwargs: push_diagnostic_checkpoint(*args, **kwargs),
        preserve_blocked_diagnostics=lambda *args, **kwargs: preserve_blocked_diagnostics(*args, **kwargs),
        reconcile_execplan_catalog=lambda *args, **kwargs: reconcile_execplan_catalog(*args, **kwargs),
        resume_project_block_if_safe=lambda *args, **kwargs: resume_project_block_if_safe(*args, **kwargs),
        unmet_execplan_dependencies=lambda *args, **kwargs: unmet_execplan_dependencies(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_checkpoint_recovery_effects_reconcile_published_blocked_checkpoint = _bind_effects(
    recovery_adapter._checkpoint_recovery_effects_reconcile_published_blocked_checkpoint, lambda: recovery_adapter.CheckpointRecoveryEffectsReconcilePublishedBlockedCheckpointEffects(
        _capture_agent_baseline=lambda *args, **kwargs: _capture_agent_baseline(*args, **kwargs),
        _git_with_index=lambda *args, **kwargs: _git_with_index(*args, **kwargs),
        _commit_tree=lambda *args, **kwargs: _commit_tree(*args, **kwargs),
        _journaled_direct_rebase=lambda *args, **kwargs: _journaled_direct_rebase(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        _BlockedCheckpointAdapter=lambda *args, **kwargs: _BlockedCheckpointAdapter(*args, **kwargs),
        _blocked_execplan_revision_metadata_error=lambda *args, **kwargs: _blocked_execplan_revision_metadata_error(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _evaluate_remote_checkpoint_cause=lambda *args, **kwargs: _evaluate_remote_checkpoint_cause(*args, **kwargs),
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        _published_plan_allows_common_plans=lambda *args, **kwargs: _published_plan_allows_common_plans(*args, **kwargs),
        _reclassify_remote_checkpoint_block=lambda *args, **kwargs: _reclassify_remote_checkpoint_block(*args, **kwargs),
        _reclassify_remote_specification_deadlock=lambda *args, **kwargs: _reclassify_remote_specification_deadlock(*args, **kwargs),
        _remote_checkpoint_input_identity=lambda *args, **kwargs: _remote_checkpoint_input_identity(*args, **kwargs),
        _remote_checkpoint_refusal=lambda *args, **kwargs: _remote_checkpoint_refusal(*args, **kwargs),
        _resume_remote_corrected_execplan=lambda *args, **kwargs: _resume_remote_corrected_execplan(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_checkpoint_recovery_effects_evaluate_remote_checkpoint_cause = _bind_effects(
    recovery_adapter._checkpoint_recovery_effects_evaluate_remote_checkpoint_cause, lambda: recovery_adapter.CheckpointRecoveryEffectsEvaluateRemoteCheckpointCauseEffects(
        ROOT=ROOT,
        _remote_checkpoint_has_new_failure=lambda *args, **kwargs: _remote_checkpoint_has_new_failure(*args, **kwargs),
        _remote_checkpoint_review_revision=lambda *args, **kwargs: _remote_checkpoint_review_revision(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        load_managed_config=lambda *args, **kwargs: load_managed_config(*args, **kwargs),
        parse_reviewer_result=lambda *args, **kwargs: parse_reviewer_result(*args, **kwargs),
        parse_worker_result=lambda *args, **kwargs: parse_worker_result(*args, **kwargs),
        reviewer_targeted_test_reason=lambda *args, **kwargs: reviewer_targeted_test_reason(*args, **kwargs),
        run_agent=lambda *args, **kwargs: run_agent(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
        verify=lambda *args, **kwargs: verify(*args, **kwargs),
    ),
)

_scope_reconciliation_effects_resume_owned_checkpoint_project_block = _bind_effects(
    recovery_adapter._scope_reconciliation_effects_resume_owned_checkpoint_project_block, lambda: recovery_adapter.ScopeReconciliationEffectsResumeOwnedCheckpointProjectBlockEffects(
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _direct_rebase_history_proven=lambda *args, **kwargs: _direct_rebase_history_proven(*args, **kwargs),
        _published_review_commit_intent_matches=lambda *args, **kwargs: _published_review_commit_intent_matches(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        unmet_execplan_dependencies=lambda *args, **kwargs: unmet_execplan_dependencies(*args, **kwargs),
    ),
)

_scope_reconciliation_effects_resume_legacy_review_count_block = _bind_effects(
    recovery_adapter._scope_reconciliation_effects_resume_legacy_review_count_block, lambda: recovery_adapter.ScopeReconciliationEffectsResumeLegacyReviewCountBlockEffects(
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        _with_proven_legacy_ignore_config=lambda *args, **kwargs: _with_proven_legacy_ignore_config(*args, **kwargs),
        _normalize_legacy_ignored_snapshot=lambda *args, **kwargs: _normalize_legacy_ignored_snapshot(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_scope_reconciliation_effects_resume_interrupted_blocked_rebase = _bind_effects(
    recovery_adapter._scope_reconciliation_effects_resume_interrupted_blocked_rebase, lambda: recovery_adapter.ScopeReconciliationEffectsResumeInterruptedBlockedRebaseEffects(
        _blocked_execplan_metadata_error=lambda *args, **kwargs: _blocked_execplan_metadata_error(*args, **kwargs),
        _blocked_resume_worktree_matches=lambda *args, **kwargs: _blocked_resume_worktree_matches(*args, **kwargs),
        _complete_direct_rebase_checkpoint=lambda *args, **kwargs: _complete_direct_rebase_checkpoint(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _direct_rebase_history_proven=lambda *args, **kwargs: _direct_rebase_history_proven(*args, **kwargs),
        _finish_owned_direct_rebase=lambda *args, **kwargs: _finish_owned_direct_rebase(*args, **kwargs),
        _owned_rebase_journal_state=lambda *args, **kwargs: _owned_rebase_journal_state(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
        _reconcile_rebase_evidence_block=lambda *args, **kwargs: _reconcile_rebase_evidence_block(*args, **kwargs),
        _record_blocked_resume_head=lambda *args, **kwargs: _record_blocked_resume_head(*args, **kwargs),
        _requeue_blocked_execplan=lambda *args, **kwargs: _requeue_blocked_execplan(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        clear_agent_scope=lambda *args, **kwargs: clear_agent_scope(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
    ),
)

_scope_observation_effects = _bind_effects(
    recovery_adapter._scope_observation_effects, lambda: recovery_adapter.ScopeObservationEffectsEffects(
        _handoff_run=lambda *args, **kwargs: _handoff_run(*args, **kwargs),
        _worktree_catalog=lambda *args, **kwargs: _worktree_catalog(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_scope_validation_effects = _bind_effects(
    agent_adapter._scope_validation_effects, lambda: agent_adapter.ScopeValidationEffectsEffects(
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _git_common_directory=lambda *args, **kwargs: _git_common_directory(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        _scope_snapshot_with_start_ignore_rules=lambda *args, **kwargs: _scope_snapshot_with_start_ignore_rules(*args, **kwargs),
        agent_snapshot_changes=lambda *args, **kwargs: agent_snapshot_changes(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_scope_lifecycle_effects = _bind_effects(
    agent_adapter._scope_lifecycle_effects, lambda: agent_adapter.ScopeLifecycleEffectsEffects(
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        _capture_agent_baseline=lambda *args, **kwargs: _capture_agent_baseline(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _published_plan_allows_common_plans=lambda *args, **kwargs: _published_plan_allows_common_plans(*args, **kwargs),
        _reconciled_manager_scope_metadata=lambda *args, **kwargs: _reconciled_manager_scope_metadata(*args, **kwargs),
        _remote_adoption_agent_baseline=lambda *args, **kwargs: _remote_adoption_agent_baseline(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
    ),
)

_scope_recovery_effects = _bind_effects(
    agent_adapter._scope_recovery_effects, lambda: agent_adapter.ScopeRecoveryEffectsEffects(
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        _config_entries=lambda *args, **kwargs: _config_entries(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _normalize_legacy_ignored_snapshot=lambda *args, **kwargs: _normalize_legacy_ignored_snapshot(*args, **kwargs),
        _owned_checkpoint_remote_oid=lambda *args, **kwargs: _owned_checkpoint_remote_oid(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _proven_manager_base_refs=lambda *args, **kwargs: _proven_manager_base_refs(*args, **kwargs),
        _proven_merged_cleanup_paths=lambda *args, **kwargs: _proven_merged_cleanup_paths(*args, **kwargs),
        _published_plan_allows_common_plans=lambda *args, **kwargs: _published_plan_allows_common_plans(*args, **kwargs),
        _reconciled_manager_scope_metadata=lambda *args, **kwargs: _reconciled_manager_scope_metadata(*args, **kwargs),
        _validate_runtime_lock_origin=lambda *args, **kwargs: _validate_runtime_lock_origin(*args, **kwargs),
        _validated_runtime_registration_marker=lambda *args, **kwargs: _validated_runtime_registration_marker(*args, **kwargs),
        _with_proven_legacy_ignore_config=lambda *args, **kwargs: _with_proven_legacy_ignore_config(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
        recover_persisted_agent_worktree=lambda *args, **kwargs: recover_persisted_agent_worktree(*args, **kwargs),
        requirement_archive_candidates=lambda *args, **kwargs: (),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
        validate_runtime_worktree=lambda *args, **kwargs: validate_runtime_worktree(*args, **kwargs),
    ),
)

_scope_baseline_effects = _bind_effects(
    agent_adapter._scope_baseline_effects, lambda: agent_adapter.ScopeBaselineEffectsEffects(
        _handoff_run=lambda *args, **kwargs: _handoff_run(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        command=lambda *args, **kwargs: command(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_agent_profiles_effects = _bind_effects(
    agent_adapter._agent_profiles_effects, lambda: agent_adapter.AgentProfilesEffectsEffects(
        claude_model_efforts=lambda *args, **kwargs: claude_model_efforts(*args, **kwargs),
        claude_role=lambda *args, **kwargs: claude_role(*args, **kwargs),
        claude_worker_profile=lambda *args, **kwargs: claude_worker_profile(*args, **kwargs),
        codex_model_efforts=lambda *args, **kwargs: codex_model_efforts(*args, **kwargs),
        command=lambda *args, **kwargs: command(*args, **kwargs),
        role=lambda *args, **kwargs: role(*args, **kwargs),
        worker_profile=lambda *args, **kwargs: worker_profile(*args, **kwargs),
    ),
)

ProjectRunKind = contracts.ProjectRunKind

ProjectRunResult = contracts.ProjectRunResult

ScheduledProjectResult = contracts.ScheduledProjectResult

CatalogScheduleDecision = contracts.CatalogScheduleDecision

ProjectRegistrationInspection = contracts.ProjectRegistrationInspection

ValidationFailureDecision = contracts.ValidationFailureDecision

WorkerLevelDecision = contracts.WorkerLevelDecision

CleanupAssessment = contracts.CleanupAssessment

WorkerPublication = contracts.WorkerPublication

RequirementArchiveError = contracts.RequirementArchiveError

RequirementArchiveCollisionError = contracts.RequirementArchiveCollisionError

WorkerResultError = contracts.WorkerResultError

ExecPlanMetadataError = contracts.ExecPlanMetadataError

ReviewerProtocolError = contracts.ReviewerProtocolError

local_now = operation_events.local_now

operation_log = _bind_effects(
    operation_events.operation_log, lambda: operation_events.OperationLogEffects(
        local_now=lambda *args, **kwargs: local_now(*args, **kwargs),
    ),
)

operation_print = _bind_effects(
    operation_events.operation_print, lambda: operation_events.OperationPrintEffects(
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
    ),
)

_bootstrap_service = _bind_effects(
    bootstrap_adapter._bootstrap_service, lambda: bootstrap_adapter.BootstrapServiceEffects(
        ROOT=ROOT,
        _git_operation_entries=lambda *args, **kwargs: _git_operation_entries(*args, **kwargs),
        _ignored_worktree_paths=lambda *args, **kwargs: _ignored_worktree_paths(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        _validate_managed_path_boundary=lambda *args, **kwargs: _validate_managed_path_boundary(*args, **kwargs),
        _worktree_catalog=lambda *args, **kwargs: _worktree_catalog(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        repository_origin=lambda *args, **kwargs: repository_origin(*args, **kwargs),
        synchronize_base_worktree=lambda *args, **kwargs: synchronize_base_worktree(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_catalog_migration_effects = _bind_effects(
    bootstrap_adapter._catalog_migration_effects, lambda: bootstrap_adapter.CatalogMigrationEffectsEffects(
        _LegacyCatalogMergeProof=lambda *args, **kwargs: _LegacyCatalogMergeProof(*args, **kwargs),
        _current_execplan_sha=lambda *args, **kwargs: _current_execplan_sha(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        revision_execplan_snapshot=lambda *args, **kwargs: revision_execplan_snapshot(*args, **kwargs),
    ),
)

validate_runtime_worktree = _bind_effects(
    bootstrap_adapter.validate_runtime_worktree, lambda: bootstrap_adapter.ValidateRuntimeWorktreeEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

_runtime_catalog_record = _bind_effects(
    bootstrap_adapter._runtime_catalog_record, lambda: bootstrap_adapter.RuntimeCatalogRecordEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

_runtime_transition_state = _bind_effects(
    bootstrap_adapter._runtime_transition_state, lambda: bootstrap_adapter.RuntimeTransitionStateEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

_managed_worktree_transition_state = _bind_effects(
    bootstrap_adapter._managed_worktree_transition_state, lambda: bootstrap_adapter.ManagedWorktreeTransitionStateEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _worktree_catalog=lambda *args, **kwargs: _worktree_catalog(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_managed_worktree_receipt = bootstrap_adapter._managed_worktree_receipt

_managed_worktree_marker = _bind_effects(
    bootstrap_adapter._managed_worktree_marker, lambda: bootstrap_adapter.ManagedWorktreeMarkerEffects(
        _managed_worktree_receipt=lambda *args, **kwargs: _managed_worktree_receipt(*args, **kwargs),
    ),
)

_write_managed_worktree_receipt = _bind_effects(
    bootstrap_adapter._write_managed_worktree_receipt, lambda: bootstrap_adapter.WriteManagedWorktreeReceiptEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
    ),
)

_recover_managed_worktree_removal = _bind_effects(
    bootstrap_adapter._recover_managed_worktree_removal, lambda: bootstrap_adapter.RecoverManagedWorktreeRemovalEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _managed_worktree_marker=lambda *args, **kwargs: _managed_worktree_marker(*args, **kwargs),
        _managed_worktree_transition_state=lambda *args, **kwargs: _managed_worktree_transition_state(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_assess_missing_managed_cleanup = _bind_effects(
    bootstrap_adapter._assess_missing_managed_cleanup, lambda: bootstrap_adapter.AssessMissingManagedCleanupEffects(
        _recover_managed_worktree_removal=lambda *args, **kwargs: _recover_managed_worktree_removal(*args, **kwargs),
    ),
)

_runtime_transition_marker = _bind_effects(
    bootstrap_adapter._runtime_transition_marker, lambda: bootstrap_adapter.RuntimeTransitionMarkerEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

_validate_runtime_lock_origin = _bind_effects(
    bootstrap_adapter._validate_runtime_lock_origin, lambda: bootstrap_adapter.ValidateRuntimeLockOriginEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

_validated_runtime_registration_marker = _bind_effects(
    bootstrap_adapter._validated_runtime_registration_marker, lambda: bootstrap_adapter.ValidatedRuntimeRegistrationMarkerEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

prepare_runtime_worktree = _bind_effects(
    bootstrap_adapter.prepare_runtime_worktree, lambda: bootstrap_adapter.PrepareRuntimeWorktreeEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

remove_runtime_worktree = _bind_effects(
    bootstrap_adapter.remove_runtime_worktree, lambda: bootstrap_adapter.RemoveRuntimeWorktreeEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

bootstrap_base_branch = _bind_effects(
    bootstrap_adapter.bootstrap_base_branch, lambda: bootstrap_adapter.BootstrapBaseBranchEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

_tracking_ref_oid = _bind_effects(
    bootstrap_adapter._tracking_ref_oid, lambda: bootstrap_adapter.TrackingRefOidEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

RemoteBranchMissingError = contracts.RemoteBranchMissingError

_recorded_base_fetch = _bind_effects(
    base_fetch_service._recorded_base_fetch, lambda: base_fetch_service.RecordedBaseFetchEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _tracking_ref_oid=lambda *args, **kwargs: _tracking_ref_oid(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

fetch_bootstrap_revision = _bind_effects(
    base_fetch_service.fetch_bootstrap_revision, lambda: base_fetch_service.FetchBootstrapRevisionEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

validate_bootstrap_revision = _bind_effects(
    base_fetch_service.validate_bootstrap_revision, lambda: base_fetch_service.ValidateBootstrapRevisionEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

prepare_bootstrap_runtime = _bind_effects(
    base_fetch_service.prepare_bootstrap_runtime, lambda: base_fetch_service.PrepareBootstrapRuntimeEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

run_runtime_child = _bind_effects(
    bootstrap_adapter.run_runtime_child, lambda: bootstrap_adapter.RunRuntimeChildEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

validate_runtime_child = _bind_effects(
    bootstrap_adapter.validate_runtime_child, lambda: bootstrap_adapter.ValidateRuntimeChildEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

validate_runtime_mutation = _bind_effects(
    bootstrap_adapter.validate_runtime_mutation, lambda: bootstrap_adapter.ValidateRuntimeMutationEffects(
        _bootstrap_service=lambda *args, **kwargs: _bootstrap_service(*args, **kwargs),
    ),
)

update_project = _bind_effects(
    project_state_adapter.update_project, lambda: project_state_adapter.UpdateProjectEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
    ),
)

command = _bind_effects(
    git_adapter.command, lambda: git_adapter.CommandEffects(
        _handoff_run=lambda *args, **kwargs: _handoff_run(*args, **kwargs),
    ),
)

is_git_lock_failure = git_adapter.is_git_lock_failure

git = _bind_effects(
    git_adapter.git, lambda: git_adapter.GitEffects(
        _command_adapter_effects_git=lambda *args, **kwargs: _command_adapter_effects_git(*args, **kwargs),
    ),
)

github_repository = git_adapter.github_repository

backfill_plan_shas = _bind_effects(
    catalog_service.backfill_plan_shas, lambda: catalog_service.BackfillPlanShasEffects(
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

git_bytes = _bind_effects(
    catalog_service.git_bytes, lambda: catalog_service.GitBytesEffects(
        _command_adapter_effects_git_bytes=lambda *args, **kwargs: _command_adapter_effects_git_bytes(*args, **kwargs),
    ),
)

_git_tree_entries = _bind_effects(
    catalog_service._git_tree_entries, lambda: catalog_service.GitTreeEntriesEffects(
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

revision_execplan_snapshot = _bind_effects(
    catalog_service.revision_execplan_snapshot, lambda: catalog_service.RevisionExecplanSnapshotEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_catalog_archive_conflict_ids = _bind_effects(
    catalog_service._catalog_archive_conflict_ids, lambda: catalog_service.CatalogArchiveConflictIdsEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _merged_execplan_ids=lambda *args, **kwargs: _merged_execplan_ids(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
    ),
)

_GitCatalogDefinitionPort = _bind_effects(
    catalog_service._GitCatalogDefinitionPort, lambda: catalog_service.GitCatalogDefinitionPortEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        revision_execplan_snapshot=lambda *args, **kwargs: revision_execplan_snapshot(*args, **kwargs),
    ),
)

reconcile_execplan_catalog = _bind_effects(
    catalog_service.reconcile_execplan_catalog, lambda: catalog_service.ReconcileExecplanCatalogEffects(
        _GitCatalogDefinitionPort=lambda *args, **kwargs: _GitCatalogDefinitionPort(*args, **kwargs),
        _catalog_archive_conflict_ids=lambda *args, **kwargs: _catalog_archive_conflict_ids(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        backfill_plan_shas=lambda *args, **kwargs: backfill_plan_shas(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        migrate_legacy_execplans=lambda *args, **kwargs: migrate_legacy_execplans(*args, **kwargs),
    ),
)

reconcile_execution_execplan_catalog = _bind_effects(
    catalog_service.reconcile_execution_execplan_catalog, lambda: catalog_service.ReconcileExecutionExecplanCatalogEffects(
        reconcile_execplan_catalog=lambda *args, **kwargs: reconcile_execplan_catalog(*args, **kwargs),
    ),
)

_is_active_requirement = catalog_service._is_active_requirement

_is_planning_input = _bind_effects(
    catalog_service._is_planning_input, lambda: catalog_service.IsPlanningInputEffects(
        _is_active_requirement=lambda *args, **kwargs: _is_active_requirement(*args, **kwargs),
    ),
)

_is_specification_input = _bind_effects(
    catalog_service._is_specification_input, lambda: catalog_service.IsSpecificationInputEffects(
        _is_active_requirement=lambda *args, **kwargs: _is_active_requirement(*args, **kwargs),
    ),
)

_planning_input_digest = input_fingerprints._planning_input_digest
_nonplan_input_digest = input_fingerprints._nonplan_input_digest

_specification_input_digest = input_fingerprints._specification_input_digest

_planning_input_fingerprint = _bind_effects(
    input_fingerprints._planning_input_fingerprint, lambda: input_fingerprints.InternalPlanningInputFingerprintEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _is_planning_input=lambda *args, **kwargs: _is_planning_input(*args, **kwargs),
        _planning_input_digest=lambda *args, **kwargs: _planning_input_digest(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_nonplan_input_fingerprint = _bind_effects(
    input_fingerprints._nonplan_input_fingerprint, lambda: input_fingerprints.InternalPlanningInputFingerprintEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _is_planning_input=lambda *args, **kwargs: _is_planning_input(*args, **kwargs),
        _planning_input_digest=lambda *args, **kwargs: _nonplan_input_digest(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

planning_input_fingerprint = _bind_effects(
    input_fingerprints.planning_input_fingerprint, lambda: input_fingerprints.PlanningInputFingerprintEffects(
        _planning_input_fingerprint=lambda *args, **kwargs: _planning_input_fingerprint(*args, **kwargs),
    ),
)

_specification_input_fingerprint = _bind_effects(
    input_fingerprints._specification_input_fingerprint, lambda: input_fingerprints.InternalSpecificationInputFingerprintEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _is_specification_input=lambda *args, **kwargs: _is_specification_input(*args, **kwargs),
        _specification_input_digest=lambda *args, **kwargs: _specification_input_digest(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

plan_declarations = _bind_effects(
    input_fingerprints.plan_declarations, lambda: input_fingerprints.PlanDeclarationsEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

specification_input_fingerprint = _bind_effects(
    input_fingerprints.specification_input_fingerprint, lambda: input_fingerprints.SpecificationInputFingerprintEffects(
        _specification_input_fingerprint=lambda *args, **kwargs: _specification_input_fingerprint(*args, **kwargs),
    ),
)

_is_requirement_archive_only_change = _bind_effects(
    input_fingerprints._is_requirement_archive_only_change, lambda: input_fingerprints.IsRequirementArchiveOnlyChangeEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _is_planning_input=lambda *args, **kwargs: _is_planning_input(*args, **kwargs),
    ),
)

file_hash = input_fingerprints.file_hash

normalized_text = input_fingerprints.normalized_text

requirement_archive_path = input_fingerprints.requirement_archive_path

parse_frontmatter = input_fingerprints.parse_frontmatter

_parse_execplan = catalog_service._parse_execplan

_validate_dependency_graph = catalog_service._validate_dependency_graph

load_execplan_snapshot = catalog_service.load_execplan_snapshot

load_execplan_catalog = _bind_effects(
    catalog_service.load_execplan_catalog, lambda: catalog_service.LoadExecplanCatalogEffects(
        load_execplan_snapshot=lambda *args, **kwargs: load_execplan_snapshot(*args, **kwargs),
    ),
)

validate_execplan = _bind_effects(
    catalog_service.validate_execplan, lambda: catalog_service.ValidateExecplanEffects(
        load_execplan_catalog=lambda *args, **kwargs: load_execplan_catalog(*args, **kwargs),
    ),
)

ValidatedPlanningResult = contracts.ValidatedPlanningResult

_idea_tokens = catalog_service._idea_tokens

_future_idea_items_from_text = _bind_effects(
    planner_validation._future_idea_items_from_text, lambda: planner_validation.FutureIdeaItemsFromTextEffects(
        _idea_tokens=lambda *args, **kwargs: _idea_tokens(*args, **kwargs),
    ),
)

_future_idea_items = _bind_effects(
    planner_validation._future_idea_items, lambda: planner_validation.FutureIdeaItemsEffects(
        _future_idea_items_from_text=lambda *args, **kwargs: _future_idea_items_from_text(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_contains_token_sequence = planner_validation._contains_token_sequence

_validate_future_idea_separation = _bind_effects(
    planner_validation._validate_future_idea_separation, lambda: planner_validation.ValidateFutureIdeaSeparationEffects(
        _contains_token_sequence=lambda *args, **kwargs: _contains_token_sequence(*args, **kwargs),
        _future_idea_items=lambda *args, **kwargs: _future_idea_items(*args, **kwargs),
        _idea_tokens=lambda *args, **kwargs: _idea_tokens(*args, **kwargs),
    ),
)

validate_planner_result = _bind_effects(
    planner_validation.validate_planner_result, lambda: planner_validation.ValidatePlannerResultEffects(
        _parse_execplan=lambda *args, **kwargs: _parse_execplan(*args, **kwargs),
        _stored_string_list=lambda *args, **kwargs: _stored_string_list(*args, **kwargs),
        _validate_future_idea_separation=lambda *args, **kwargs: _validate_future_idea_separation(*args, **kwargs),
        changed_files=lambda *args, **kwargs: changed_files(*args, **kwargs),
        file_hash=lambda *args, **kwargs: file_hash(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        load_execplan_catalog=lambda *args, **kwargs: load_execplan_catalog(*args, **kwargs),
        mutable_snapshot_token=lambda *args, **kwargs: mutable_snapshot_token(*args, **kwargs),
    ),
)

_stored_string_list = planner_validation._stored_string_list

_ready_plan_snapshot_from_contents = _bind_effects(
    planner_validation._ready_plan_snapshot_from_contents, lambda: planner_validation.ReadyPlanSnapshotFromContentsEffects(
        _stored_string_list=lambda *args, **kwargs: _stored_string_list(*args, **kwargs),
        is_unowned_ready=lambda *args, **kwargs: is_unowned_ready(*args, **kwargs),
        published_execplan=lambda *args, **kwargs: published_execplan(*args, **kwargs),
    ),
)

_assert_ready_plan_revision = _bind_effects(
    planner_validation._assert_ready_plan_revision, lambda: planner_validation.AssertReadyPlanRevisionEffects(
        _git_commit_is_ancestor=lambda *args, **kwargs: _git_commit_is_ancestor(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_git_path_matches_revision = _bind_effects(
    planner_validation._git_path_matches_revision, lambda: planner_validation.GitPathMatchesRevisionEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_git_path_has_standard_index_flags = _bind_effects(
    planner_validation._git_path_has_standard_index_flags, lambda: planner_validation.GitPathHasStandardIndexFlagsEffects(
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_ready_plan_snapshot = _bind_effects(
    planner_validation._ready_plan_snapshot, lambda: planner_validation.ReadyPlanSnapshotEffects(
        _assert_ready_plan_revision=lambda *args, **kwargs: _assert_ready_plan_revision(*args, **kwargs),
        _git_path_has_standard_index_flags=lambda *args, **kwargs: _git_path_has_standard_index_flags(*args, **kwargs),
        _git_path_matches_revision=lambda *args, **kwargs: _git_path_matches_revision(*args, **kwargs),
        _ready_plan_snapshot_from_contents=lambda *args, **kwargs: _ready_plan_snapshot_from_contents(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_ready_plan_snapshot_at_revision = _bind_effects(
    planner_validation._ready_plan_snapshot_at_revision, lambda: planner_validation.ReadyPlanSnapshotAtRevisionEffects(
        _assert_ready_plan_revision=lambda *args, **kwargs: _assert_ready_plan_revision(*args, **kwargs),
        _ready_plan_snapshot_from_contents=lambda *args, **kwargs: _ready_plan_snapshot_from_contents(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_row_is_mutable_ready = _bind_effects(
    planner_validation._row_is_mutable_ready, lambda: planner_validation.RowIsMutableReadyEffects(
        is_unowned_ready=lambda *args, **kwargs: is_unowned_ready(*args, **kwargs),
    ),
)

mutable_ready_snapshot = _bind_effects(
    planner_validation.mutable_ready_snapshot, lambda: planner_validation.MutableReadySnapshotEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
        _ready_plan_snapshot_at_revision=lambda *args, **kwargs: _ready_plan_snapshot_at_revision(*args, **kwargs),
        _row_is_mutable_ready=lambda *args, **kwargs: _row_is_mutable_ready(*args, **kwargs),
        mutable_snapshot_token=lambda *args, **kwargs: mutable_snapshot_token(*args, **kwargs),
    ),
)

_assert_mutable_ready_snapshot_current = _bind_effects(
    planner_validation._assert_mutable_ready_snapshot_current, lambda: planner_validation.AssertMutableReadySnapshotCurrentEffects(
        _execplan_metadata=lambda *args, **kwargs: _execplan_metadata(*args, **kwargs),
        _row_is_mutable_ready=lambda *args, **kwargs: _row_is_mutable_ready(*args, **kwargs),
    ),
)

_maintenance_mode_and_rows = _bind_effects(
    maintenance_validation._maintenance_mode_and_rows, lambda: maintenance_validation.MaintenanceModeAndRowsEffects(
        is_unowned_ready=lambda *args, **kwargs: is_unowned_ready(*args, **kwargs),
    ),
)

_maintenance_context = maintenance_validation._maintenance_context

prepare_maintenance_planning_context = _bind_effects(
    maintenance_validation.prepare_maintenance_planning_context, lambda: maintenance_validation.PrepareMaintenancePlanningContextEffects(
        _maintenance_context=lambda *args, **kwargs: _maintenance_context(*args, **kwargs),
        _maintenance_mode_and_rows=lambda *args, **kwargs: _maintenance_mode_and_rows(*args, **kwargs),
        _ready_plan_snapshot=lambda *args, **kwargs: _ready_plan_snapshot(*args, **kwargs),
        _ready_plan_snapshot_at_revision=lambda *args, **kwargs: _ready_plan_snapshot_at_revision(*args, **kwargs),
    ),
)

_maintenance_context_at_revision = _bind_effects(
    maintenance_validation._maintenance_context_at_revision, lambda: maintenance_validation.MaintenanceContextAtRevisionEffects(
        _maintenance_context=lambda *args, **kwargs: _maintenance_context(*args, **kwargs),
        _maintenance_mode_and_rows=lambda *args, **kwargs: _maintenance_mode_and_rows(*args, **kwargs),
        _ready_plan_snapshot_at_revision=lambda *args, **kwargs: _ready_plan_snapshot_at_revision(*args, **kwargs),
    ),
)

_assert_maintenance_context_current = _bind_effects(
    maintenance_validation._assert_maintenance_context_current, lambda: maintenance_validation.AssertMaintenanceContextCurrentEffects(
        _maintenance_mode_and_rows=lambda *args, **kwargs: _maintenance_mode_and_rows(*args, **kwargs),
        _stored_string_list=lambda *args, **kwargs: _stored_string_list(*args, **kwargs),
        is_unowned_ready=lambda *args, **kwargs: is_unowned_ready(*args, **kwargs),
    ),
)

validate_maintenance_planner_result = _bind_effects(
    maintenance_validation.validate_maintenance_planner_result, lambda: maintenance_validation.ValidateMaintenancePlannerResultEffects(
        _assert_maintenance_context_current=lambda *args, **kwargs: _assert_maintenance_context_current(*args, **kwargs),
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _parse_execplan=lambda *args, **kwargs: _parse_execplan(*args, **kwargs),
        _validate_future_idea_separation=lambda *args, **kwargs: _validate_future_idea_separation(*args, **kwargs),
        changed_files=lambda *args, **kwargs: changed_files(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        load_execplan_snapshot=lambda *args, **kwargs: load_execplan_snapshot(*args, **kwargs),
        published_execplan=lambda *args, **kwargs: published_execplan(*args, **kwargs),
        terminal_new_plan_ids=lambda *args, **kwargs: terminal_new_plan_ids(*args, **kwargs),
        validate_barrier_revision=lambda *args, **kwargs: validate_barrier_revision(*args, **kwargs),
        validate_refactoring_plan=lambda *args, **kwargs: validate_refactoring_plan(*args, **kwargs),
        validate_revalidation_revision=lambda *args, **kwargs: validate_revalidation_revision(*args, **kwargs),
    ),
)

_plan_id_from_path = maintenance_validation._plan_id_from_path

_register_execplan_rows = _bind_effects(
    planner_publication._register_execplan_rows, lambda: planner_publication.RegisterExecplanRowsEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
        file_hash=lambda *args, **kwargs: file_hash(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

register_execplans = _bind_effects(
    planner_publication.register_execplans, lambda: planner_publication.RegisterExecplansEffects(
        _register_execplan_rows=lambda *args, **kwargs: _register_execplan_rows(*args, **kwargs),
    ),
)

_planning_result_manifest = _bind_effects(
    planner_publication._planning_result_manifest, lambda: planner_publication.PlanningResultManifestEffects(
        snapshot_to_dict=lambda *args, **kwargs: snapshot_to_dict(*args, **kwargs),
    ),
)

_planning_result_from_manifest = _bind_effects(
    planner_publication._planning_result_from_manifest, lambda: planner_publication.PlanningResultFromManifestEffects(
        mutable_snapshot_token=lambda *args, **kwargs: mutable_snapshot_token(*args, **kwargs),
        snapshot_from_dict=lambda *args, **kwargs: snapshot_from_dict(*args, **kwargs),
    ),
)

_validate_general_planning_commit = _bind_effects(
    planner_publication._validate_general_planning_commit, lambda: planner_publication.ValidateGeneralPlanningCommitEffects(
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

record_general_planning_publication_intent = _bind_effects(
    planner_publication.record_general_planning_publication_intent, lambda: planner_publication.RecordGeneralPlanningPublicationIntentEffects(
        _assert_mutable_ready_snapshot_current=lambda *args, **kwargs: _assert_mutable_ready_snapshot_current(*args, **kwargs),
        _planning_result_manifest=lambda *args, **kwargs: _planning_result_manifest(*args, **kwargs),
        _validate_general_planning_commit=lambda *args, **kwargs: _validate_general_planning_commit(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
    ),
)

_general_planning_batch_already_applied = _bind_effects(
    planner_publication._general_planning_batch_already_applied, lambda: planner_publication.GeneralPlanningBatchAlreadyAppliedEffects(
        _execplan_metadata=lambda *args, **kwargs: _execplan_metadata(*args, **kwargs),
        file_hash=lambda *args, **kwargs: file_hash(*args, **kwargs),
        plan_declarations=lambda *args, **kwargs: plan_declarations(*args, **kwargs),
    ),
)

_apply_general_planning_batch = _bind_effects(
    planner_publication._apply_general_planning_batch, lambda: planner_publication.ApplyGeneralPlanningBatchEffects(
        _assert_mutable_ready_snapshot_current=lambda *args, **kwargs: _assert_mutable_ready_snapshot_current(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _general_planning_batch_already_applied=lambda *args, **kwargs: _general_planning_batch_already_applied(*args, **kwargs),
        _register_execplan_rows=lambda *args, **kwargs: _register_execplan_rows(*args, **kwargs),
        file_hash=lambda *args, **kwargs: file_hash(*args, **kwargs),
        general_planning_publication=lambda *args, **kwargs: general_planning_publication(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        plan_declarations=lambda *args, **kwargs: plan_declarations(*args, **kwargs),
    ),
)

publish_general_planning_batch = _bind_effects(
    planner_publication.publish_general_planning_batch, lambda: planner_publication.PublishGeneralPlanningBatchEffects(
        _apply_general_planning_batch=lambda *args, **kwargs: _apply_general_planning_batch(*args, **kwargs),
        _validate_general_planning_commit=lambda *args, **kwargs: _validate_general_planning_commit(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

recover_general_planning_batch = _bind_effects(
    planner_publication.recover_general_planning_batch, lambda: planner_publication.RecoverGeneralPlanningBatchEffects(
        _apply_general_planning_batch=lambda *args, **kwargs: _apply_general_planning_batch(*args, **kwargs),
        _assert_remote_preserves_maintenance_batch=lambda *args, **kwargs: _assert_remote_preserves_maintenance_batch(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _git_commit_is_ancestor=lambda *args, **kwargs: _git_commit_is_ancestor(*args, **kwargs),
        _planning_result_from_manifest=lambda *args, **kwargs: _planning_result_from_manifest(*args, **kwargs),
        _validate_general_planning_commit=lambda *args, **kwargs: _validate_general_planning_commit(*args, **kwargs),
        general_planning_publication=lambda *args, **kwargs: general_planning_publication(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

validate_maintenance_publication_base = _bind_effects(
    maintenance_publication.validate_maintenance_publication_base, lambda: maintenance_publication.ValidateMaintenancePublicationBaseEffects(
        _assert_maintenance_context_current=lambda *args, **kwargs: _assert_maintenance_context_current(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_validate_maintenance_planning_commit = _bind_effects(
    maintenance_publication._validate_maintenance_planning_commit, lambda: maintenance_publication.ValidateMaintenancePlanningCommitEffects(
        _parse_execplan=lambda *args, **kwargs: _parse_execplan(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        load_execplan_catalog=lambda *args, **kwargs: load_execplan_catalog(*args, **kwargs),
        load_execplan_snapshot=lambda *args, **kwargs: load_execplan_snapshot(*args, **kwargs),
    ),
)

_validate_maintenance_publication_commit = _bind_effects(
    maintenance_publication._validate_maintenance_publication_commit, lambda: maintenance_publication.ValidateMaintenancePublicationCommitEffects(
        _validate_maintenance_planning_commit=lambda *args, **kwargs: _validate_maintenance_planning_commit(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_git_commit_is_ancestor = _bind_effects(
    maintenance_publication._git_commit_is_ancestor, lambda: maintenance_publication.GitCommitIsAncestorEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_assert_remote_preserves_maintenance_batch = _bind_effects(
    maintenance_publication._assert_remote_preserves_maintenance_batch, lambda: maintenance_publication.AssertRemotePreservesMaintenanceBatchEffects(
        _git_commit_is_ancestor=lambda *args, **kwargs: _git_commit_is_ancestor(*args, **kwargs),
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
    ),
)

record_maintenance_publication_intent = _bind_effects(
    maintenance_publication.record_maintenance_publication_intent, lambda: maintenance_publication.RecordMaintenancePublicationIntentEffects(
        _assert_maintenance_context_current=lambda *args, **kwargs: _assert_maintenance_context_current(*args, **kwargs),
        _validate_maintenance_publication_commit=lambda *args, **kwargs: _validate_maintenance_publication_commit(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
    ),
)

_maintenance_rows_match_published_batch = _bind_effects(
    maintenance_publication._maintenance_rows_match_published_batch, lambda: maintenance_publication.MaintenanceRowsMatchPublishedBatchEffects(
        _execplan_metadata=lambda *args, **kwargs: _execplan_metadata(*args, **kwargs),
        file_hash=lambda *args, **kwargs: file_hash(*args, **kwargs),
    ),
)

_assert_maintenance_publication_intent = maintenance_publication._assert_maintenance_publication_intent

_maintenance_publication_already_applied = _bind_effects(
    maintenance_publication._maintenance_publication_already_applied, lambda: maintenance_publication.MaintenancePublicationAlreadyAppliedEffects(
        _maintenance_rows_match_published_batch=lambda *args, **kwargs: _maintenance_rows_match_published_batch(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
    ),
)

publish_maintenance_planning_batch = _bind_effects(
    maintenance_publication.publish_maintenance_planning_batch, lambda: maintenance_publication.PublishMaintenancePlanningBatchEffects(
        _apply_maintenance_planning_batch=lambda *args, **kwargs: _apply_maintenance_planning_batch(*args, **kwargs),
        _validate_maintenance_publication_commit=lambda *args, **kwargs: _validate_maintenance_publication_commit(*args, **kwargs),
    ),
)

_apply_maintenance_planning_batch = _bind_effects(
    maintenance_publication._apply_maintenance_planning_batch, lambda: maintenance_publication.ApplyMaintenancePlanningBatchEffects(
        _assert_maintenance_context_current=lambda *args, **kwargs: _assert_maintenance_context_current(*args, **kwargs),
        _assert_maintenance_publication_intent=lambda *args, **kwargs: _assert_maintenance_publication_intent(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _execplan_metadata=lambda *args, **kwargs: _execplan_metadata(*args, **kwargs),
        _maintenance_publication_already_applied=lambda *args, **kwargs: _maintenance_publication_already_applied(*args, **kwargs),
        _planning_input_fingerprint=lambda *args, **kwargs: _planning_input_fingerprint(*args, **kwargs),
        plan_declarations=lambda *args, **kwargs: plan_declarations(*args, **kwargs),
        _register_execplan_rows=lambda *args, **kwargs: _register_execplan_rows(*args, **kwargs),
        _specification_input_fingerprint=lambda *args, **kwargs: _specification_input_fingerprint(*args, **kwargs),
        file_hash=lambda *args, **kwargs: file_hash(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
    ),
)

restart_stale_maintenance_publication = _bind_effects(
    maintenance_publication.restart_stale_maintenance_publication, lambda: maintenance_publication.RestartStaleMaintenancePublicationEffects(
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _git_commit_is_ancestor=lambda *args, **kwargs: _git_commit_is_ancestor(*args, **kwargs),
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

recover_maintenance_planning_batch = _bind_effects(
    maintenance_publication.recover_maintenance_planning_batch, lambda: maintenance_publication.RecoverMaintenancePlanningBatchEffects(
        _apply_maintenance_planning_batch=lambda *args, **kwargs: _apply_maintenance_planning_batch(*args, **kwargs),
        _assert_remote_preserves_maintenance_batch=lambda *args, **kwargs: _assert_remote_preserves_maintenance_batch(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _git_commit_is_ancestor=lambda *args, **kwargs: _git_commit_is_ancestor(*args, **kwargs),
        _maintenance_context_at_revision=lambda *args, **kwargs: _maintenance_context_at_revision(*args, **kwargs),
        _validate_maintenance_planning_commit=lambda *args, **kwargs: _validate_maintenance_planning_commit(*args, **kwargs),
        changed_files=lambda *args, **kwargs: changed_files(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        publish_maintenance_planning_batch=lambda *args, **kwargs: publish_maintenance_planning_batch(*args, **kwargs),
        restart_stale_maintenance_publication=lambda *args, **kwargs: restart_stale_maintenance_publication(*args, **kwargs),
        validate_maintenance_planner_result=lambda *args, **kwargs: validate_maintenance_planner_result(*args, **kwargs),
    ),
)

_legacy_plan_sha = _bind_effects(
    legacy_migration._legacy_plan_sha, lambda: legacy_migration.LegacyPlanShaEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_current_execplan_sha = _bind_effects(
    legacy_migration._current_execplan_sha, lambda: legacy_migration.CurrentExecplanShaEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_LegacyCatalogMergeProof = _bind_effects(
    legacy_migration._LegacyCatalogMergeProof, lambda: legacy_migration.LegacyCatalogMergeProofEffects(
        _git_commit_is_ancestor=lambda *args, **kwargs: _git_commit_is_ancestor(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
    ),
)

_migrate_execplan_catalog = _bind_effects(
    legacy_migration._migrate_execplan_catalog, lambda: legacy_migration.MigrateExecplanCatalogEffects(
        _catalog_migration_effects=lambda *args, **kwargs: _catalog_migration_effects(*args, **kwargs),
    ),
)

migrate_legacy_execplans = _bind_effects(
    legacy_migration.migrate_legacy_execplans, lambda: legacy_migration.MigrateLegacyExecplansEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
        _migrate_execplan_catalog=lambda *args, **kwargs: _migrate_execplan_catalog(*args, **kwargs),
    ),
)

active_execplan = _bind_effects(
    legacy_migration.active_execplan, lambda: legacy_migration.ActiveExecplanEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
    ),
)

project_execplan = legacy_migration.project_execplan

_project_pr_merge_confirmed = _bind_effects(
    waiting_pr_service._project_pr_merge_confirmed, lambda: waiting_pr_service.ProjectPrMergeConfirmedEffects(
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
    ),
)

plan_blocked_execplans = _bind_effects(
    project_selection.plan_blocked_execplans, lambda: project_selection.PlanBlockedExecplansEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
    ),
)

merge_wait_execplans = _bind_effects(
    project_selection.merge_wait_execplans, lambda: project_selection.MergeWaitExecplansEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
    ),
)

mark_execplan_workspace_owned = project_selection.mark_execplan_workspace_owned

reserve_execplan_workspace = project_selection.reserve_execplan_workspace

compact_execplan_catalog = _bind_effects(
    project_selection.compact_execplan_catalog, lambda: project_selection.CompactExecplanCatalogEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
    ),
)

prerequisite_validation_rows = _bind_effects(
    project_selection.prerequisite_validation_rows, lambda: project_selection.PrerequisiteValidationRowsEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
    ),
)

catalog_schedule_decision = _bind_effects(
    project_selection.catalog_schedule_decision, lambda: project_selection.CatalogScheduleDecisionEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        assessment_can_yield=lambda *args, **kwargs: assessment_can_yield(*args, **kwargs),
        is_unowned_ready=lambda *args, **kwargs: is_unowned_ready(*args, **kwargs),
        select_cycle_ready_execplan=lambda *args, **kwargs: select_cycle_ready_execplan(*args, **kwargs),
    ),
)

select_ready_execplan = _bind_effects(
    project_selection.select_ready_execplan, lambda: project_selection.SelectReadyExecplanEffects(
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        catalog_schedule_decision=lambda *args, **kwargs: catalog_schedule_decision(*args, **kwargs),
    ),
)

_maintenance_change_context_text = cycle_service._maintenance_change_context_text

_advance_maintenance_cycle_after_merges = _bind_effects(
    cycle_service._advance_maintenance_cycle_after_merges, lambda: cycle_service.AdvanceMaintenanceCycleAfterMergesEffects(
        all_cycle_plans_merged=lambda *args, **kwargs: all_cycle_plans_merged(*args, **kwargs),
        cycle_plan_status_error=lambda *args, **kwargs: cycle_plan_status_error(*args, **kwargs),
    ),
)

activate_ready_execplan = _bind_effects(
    project_state_adapter.activate_ready_execplan, lambda: project_state_adapter.ActivateReadyExecplanEffects(
        assessment_can_yield=lambda *args, **kwargs: assessment_can_yield(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_unowned_ready=lambda *args, **kwargs: is_unowned_ready(*args, **kwargs),
        select_ready_execplan=lambda *args, **kwargs: select_ready_execplan(*args, **kwargs),
    ),
)

hydrate_active_project = _bind_effects(
    project_state_adapter.hydrate_active_project, lambda: project_state_adapter.HydrateActiveProjectEffects(
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
    ),
)

hydrate_execplan_project = project_state_adapter.hydrate_execplan_project

_mirror_project_update_to_execplan = _bind_effects(
    project_state_adapter._mirror_project_update_to_execplan, lambda: project_state_adapter.MirrorProjectUpdateToExecplanEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
    ),
)

validate_runtime_prerequisites = _bind_effects(
    registration_service.validate_runtime_prerequisites, lambda: registration_service.ValidateRuntimePrerequisitesEffects(
        validate_provider_runtime=lambda *args, **kwargs: validate_provider_runtime(*args, **kwargs),
    ),
)

repository_origin = _bind_effects(
    registration_service.repository_origin, lambda: registration_service.RepositoryOriginEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
    ),
)

validate_repository = _bind_effects(
    registration_service.validate_repository, lambda: registration_service.ValidateRepositoryEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        repository_origin=lambda *args, **kwargs: repository_origin(*args, **kwargs),
        validate_runtime_prerequisites=lambda *args, **kwargs: validate_runtime_prerequisites(*args, **kwargs),
    ),
)

remote_base = _bind_effects(
    registration_service.remote_base, lambda: registration_service.RemoteBaseEffects(
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_revision_blob = _bind_effects(
    registration_service._revision_blob, lambda: registration_service.RevisionBlobEffects(
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

inspect_registration_files = _bind_effects(
    registration_service.inspect_registration_files, lambda: registration_service.InspectRegistrationFilesEffects(
        ROOT=ROOT,
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _is_active_requirement=lambda *args, **kwargs: _is_active_requirement(*args, **kwargs),
        _revision_blob=lambda *args, **kwargs: _revision_blob(*args, **kwargs),
        file_hash=lambda *args, **kwargs: file_hash(*args, **kwargs),
        normalized_text=lambda *args, **kwargs: normalized_text(*args, **kwargs),
    ),
)

inspect_project_registration = _bind_effects(
    registration_service.inspect_project_registration, lambda: registration_service.InspectProjectRegistrationEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        inspect_registration_files=lambda *args, **kwargs: inspect_registration_files(*args, **kwargs),
        remote_base=lambda *args, **kwargs: remote_base(*args, **kwargs),
        validate_repository=lambda *args, **kwargs: validate_repository(*args, **kwargs),
    ),
)

add_registered_project = _bind_effects(
    registration_service.add_registered_project, lambda: registration_service.AddRegisteredProjectEffects(
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
        _merged_execplan_ids=lambda *args, **kwargs: _merged_execplan_ids(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        inspect_project_registration=lambda *args, **kwargs: inspect_project_registration(*args, **kwargs),
        migrate_legacy_execplans=lambda *args, **kwargs: migrate_legacy_execplans(*args, **kwargs),
        revision_execplan_snapshot=lambda *args, **kwargs: revision_execplan_snapshot(*args, **kwargs),
    ),
)

_ignored_worktree_paths = _bind_effects(
    git_adapter._ignored_worktree_paths, lambda: git_adapter.IgnoredWorktreePathsEffects(
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
    ),
)

_paths_overlap = git_adapter._paths_overlap

_ignored_paths_overlapping_git_update = _bind_effects(
    git_adapter._ignored_paths_overlapping_git_update, lambda: git_adapter.IgnoredPathsOverlappingGitUpdateEffects(
        _ignored_worktree_paths=lambda *args, **kwargs: _ignored_worktree_paths(*args, **kwargs),
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        _paths_overlap=lambda *args, **kwargs: _paths_overlap(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_remaining_rebase_commits = _bind_effects(
    git_adapter._remaining_rebase_commits, lambda: git_adapter.RemainingRebaseCommitsEffects(
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_rebase_resume_ignored_overlap = _bind_effects(
    git_adapter._rebase_resume_ignored_overlap, lambda: git_adapter.RebaseResumeIgnoredOverlapEffects(
        _ignored_worktree_paths=lambda *args, **kwargs: _ignored_worktree_paths(*args, **kwargs),
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        _paths_overlap=lambda *args, **kwargs: _paths_overlap(*args, **kwargs),
        _remaining_rebase_commits=lambda *args, **kwargs: _remaining_rebase_commits(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_base_worktree_snapshot = _bind_effects(
    git_adapter._base_worktree_snapshot, lambda: git_adapter.BaseWorktreeSnapshotEffects(
        _source_sync_effects_base_worktree_snapshot=lambda *args, **kwargs: _source_sync_effects_base_worktree_snapshot(*args, **kwargs),
    ),
)

_base_paths_overlap = _bind_effects(
    git_adapter._base_paths_overlap, lambda: git_adapter.BasePathsOverlapEffects(
        _source_sync_effects_base_paths_overlap=lambda *args, **kwargs: _source_sync_effects_base_paths_overlap(*args, **kwargs),
    ),
)

_source_transition_state = _bind_effects(
    git_adapter._source_transition_state, lambda: git_adapter.SourceTransitionStateEffects(
        _source_sync_effects_source_transition_state=lambda *args, **kwargs: _source_sync_effects_source_transition_state(*args, **kwargs),
    ),
)

_source_transition_marker = _bind_effects(
    git_adapter._source_transition_marker, lambda: git_adapter.SourceTransitionMarkerEffects(
        _source_sync_effects_source_transition_marker=lambda *args, **kwargs: _source_sync_effects_source_transition_marker(*args, **kwargs),
    ),
)

_source_expected_index = _bind_effects(
    git_adapter._source_expected_index, lambda: git_adapter.SourceExpectedIndexEffects(
        _source_sync_effects_source_expected_index=lambda *args, **kwargs: _source_sync_effects_source_expected_index(*args, **kwargs),
    ),
)

synchronize_base_worktree = _bind_effects(
    git_adapter.synchronize_base_worktree, lambda: git_adapter.SynchronizeBaseWorktreeEffects(
        _source_sync_effects_synchronize_base_worktree=lambda *args, **kwargs: _source_sync_effects_synchronize_base_worktree(*args, **kwargs),
    ),
)

ensure_safe_base_worktree = _bind_effects(
    git_adapter.ensure_safe_base_worktree, lambda: git_adapter.EnsureSafeBaseWorktreeEffects(
        _source_sync_effects_ensure_safe_base_worktree=lambda *args, **kwargs: _source_sync_effects_ensure_safe_base_worktree(*args, **kwargs),
    ),
)

log_base_worktree_sync = _bind_effects(
    git_adapter.log_base_worktree_sync, lambda: git_adapter.LogBaseWorktreeSyncEffects(
        _source_sync_effects_log_base_worktree_sync=lambda *args, **kwargs: _source_sync_effects_log_base_worktree_sync(*args, **kwargs),
    ),
)

_deserialize_review_level_history = result_validation._deserialize_review_level_history

evaluate_review_worker_level = result_validation.evaluate_review_worker_level

ensure_worker_level_state = _bind_effects(
    result_validation.ensure_worker_level_state, lambda: result_validation.EnsureWorkerLevelStateEffects(
        _deserialize_review_level_history=lambda *args, **kwargs: _deserialize_review_level_history(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        worker_level=lambda *args, **kwargs: worker_level(*args, **kwargs),
    ),
)

codex_model_efforts = _bind_effects(
    result_validation.codex_model_efforts, lambda: result_validation.CodexModelEffortsEffects(
        _agent_profiles_effects=lambda *args, **kwargs: _agent_profiles_effects(*args, **kwargs),
    ),
)

codex_profile = _bind_effects(
    agent_adapter.codex_profile, lambda: agent_adapter.CodexProfileEffects(
        codex_model_efforts=lambda *args, **kwargs: codex_model_efforts(*args, **kwargs),
        provider_profile=lambda *args, **kwargs: provider_profile(*args, **kwargs),
    ),
)

role = _bind_effects(
    agent_adapter.role, lambda: agent_adapter.RoleEffects(
        codex_profile=lambda *args, **kwargs: codex_profile(*args, **kwargs),
    ),
)

worker_profile = _bind_effects(
    agent_adapter.worker_profile, lambda: agent_adapter.WorkerProfileEffects(
        codex_model_efforts=lambda *args, **kwargs: codex_model_efforts(*args, **kwargs),
        provider_worker_profile=lambda *args, **kwargs: provider_worker_profile(*args, **kwargs),
    ),
)

claude_model_efforts = _bind_effects(
    agent_adapter.claude_model_efforts, lambda: agent_adapter.ClaudeModelEffortsEffects(
        _agent_profiles_effects=lambda *args, **kwargs: _agent_profiles_effects(*args, **kwargs),
    ),
)

claude_role = _bind_effects(
    agent_adapter.claude_role, lambda: agent_adapter.ClaudeRoleEffects(
        claude_model_efforts=lambda *args, **kwargs: claude_model_efforts(*args, **kwargs),
        provider_profile=lambda *args, **kwargs: provider_profile(*args, **kwargs),
    ),
)

claude_worker_profile = _bind_effects(
    agent_adapter.claude_worker_profile, lambda: agent_adapter.ClaudeWorkerProfileEffects(
        claude_model_efforts=lambda *args, **kwargs: claude_model_efforts(*args, **kwargs),
        provider_worker_profile=lambda *args, **kwargs: provider_worker_profile(*args, **kwargs),
    ),
)

run_codex = _bind_effects(
    agent_adapter.run_codex, lambda: agent_adapter.RunCodexEffects(
        _provider_adapters_effects=lambda *args, **kwargs: _provider_adapters_effects(*args, **kwargs),
    ),
)

_agent_profile = _bind_effects(
    agent_adapter._agent_profile, lambda: agent_adapter.AgentProfileEffects(
        _agent_profiles_effects=lambda *args, **kwargs: _agent_profiles_effects(*args, **kwargs),
    ),
)

validate_provider_runtime = _bind_effects(
    agent_adapter.validate_provider_runtime, lambda: agent_adapter.ValidateProviderRuntimeEffects(
        _agent_profiles_effects=lambda *args, **kwargs: _agent_profiles_effects(*args, **kwargs),
    ),
)

run_claude = _bind_effects(
    agent_adapter.run_claude, lambda: agent_adapter.RunClaudeEffects(
        _provider_adapters_effects=lambda *args, **kwargs: _provider_adapters_effects(*args, **kwargs),
    ),
)

agent_provider = _bind_effects(
    agent_adapter.agent_provider, lambda: agent_adapter.AgentProviderEffects(
        run_claude=lambda *args, **kwargs: run_claude(*args, **kwargs),
        run_codex=lambda *args, **kwargs: run_codex(*args, **kwargs),
    ),
)

run_agent = _bind_effects(
    agent_adapter.run_agent, lambda: agent_adapter.RunAgentEffects(
        _invocation_service_effects=lambda *args, **kwargs: _invocation_service_effects(*args, **kwargs),
    ),
)

_run_agent_with_connection = _bind_effects(
    agent_adapter._run_agent_with_connection, lambda: agent_adapter.RunAgentWithConnectionEffects(
        run_agent=lambda *args, **kwargs: run_agent(*args, **kwargs),
    ),
)

VerifyOutput = contracts.VerifyOutput

verify = _bind_effects(
    result_validation.verify, lambda: result_validation.VerifyEffects(
        _check_runtime_handoff=lambda *args, **kwargs: _check_runtime_handoff(*args, **kwargs),
        _handoff_popen=lambda *args, **kwargs: _handoff_popen(*args, **kwargs),
        run_verify_process=lambda *args, **kwargs: run_verify_process(*args, **kwargs),
    ),
)

_valid_review_issue_path = result_validation._valid_review_issue_path

_valid_review_issue_identifier = _bind_effects(
    result_validation._valid_review_issue_identifier, lambda: result_validation.ValidReviewIssueIdentifierEffects(
        _valid_review_issue_path=lambda *args, **kwargs: _valid_review_issue_path(*args, **kwargs),
    ),
)

_normalize_review_issue_ids = _bind_effects(
    result_validation._normalize_review_issue_ids, lambda: result_validation.NormalizeReviewIssueIdsEffects(
        _valid_review_issue_identifier=lambda *args, **kwargs: _valid_review_issue_identifier(*args, **kwargs),
    ),
)

parse_reviewer_result = _bind_effects(
    result_validation.parse_reviewer_result, lambda: result_validation.ParseReviewerResultEffects(
        _normalize_review_issue_ids=lambda *args, **kwargs: _normalize_review_issue_ids(*args, **kwargs),
    ),
)

_review_issue_paths = _bind_effects(
    result_validation._review_issue_paths, lambda: result_validation.ReviewIssuePathsEffects(
        parse_reviewer_result=lambda *args, **kwargs: parse_reviewer_result(*args, **kwargs),
    ),
)

_normalize_temp_path = result_validation._normalize_temp_path

normalize_validation_failure = _bind_effects(
    result_validation.normalize_validation_failure, lambda: result_validation.NormalizeValidationFailureEffects(
        _normalize_review_issue_ids=lambda *args, **kwargs: _normalize_review_issue_ids(*args, **kwargs),
        _normalize_temp_path=lambda *args, **kwargs: _normalize_temp_path(*args, **kwargs),
    ),
)

validation_failure_fingerprints = _bind_effects(
    result_validation.validation_failure_fingerprints, lambda: result_validation.ValidationFailureFingerprintsEffects(
        normalize_validation_failure=lambda *args, **kwargs: normalize_validation_failure(*args, **kwargs),
    ),
)

validation_failure_fingerprint = _bind_effects(
    result_validation.validation_failure_fingerprint, lambda: result_validation.ValidationFailureFingerprintEffects(
        validation_failure_fingerprints=lambda *args, **kwargs: validation_failure_fingerprints(*args, **kwargs),
    ),
)

_deserialize_failure_history = result_validation._deserialize_failure_history

clear_validation_failure_history = _bind_effects(
    result_validation.clear_validation_failure_history, lambda: result_validation.ClearValidationFailureHistoryEffects(
        _deserialize_failure_history=lambda *args, **kwargs: _deserialize_failure_history(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
    ),
)

pr_for_branch = _bind_effects(
    result_validation.pr_for_branch, lambda: result_validation.PrForBranchEffects(
        _github_observation_effects_pr_for_branch=lambda *args, **kwargs: _github_observation_effects_pr_for_branch(*args, **kwargs),
    ),
)

_fetch_pr_conflict_recovery_refs = _bind_effects(
    pr_ownership._fetch_pr_conflict_recovery_refs, lambda: pr_ownership.FetchPrConflictRecoveryRefsEffects(
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
    ),
)

_validate_pr_conflict_recovery_identity = _bind_effects(
    pr_ownership._validate_pr_conflict_recovery_identity, lambda: pr_ownership.ValidatePrConflictRecoveryIdentityEffects(
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
    ),
)

_validate_pr_conflict_recovery_ownership = _bind_effects(
    pr_ownership._validate_pr_conflict_recovery_ownership, lambda: pr_ownership.ValidatePrConflictRecoveryOwnershipEffects(
        _ignored_paths_overlapping_git_update=lambda *args, **kwargs: _ignored_paths_overlapping_git_update(*args, **kwargs),
        _ignored_worktree_paths=lambda *args, **kwargs: _ignored_worktree_paths(*args, **kwargs),
        _non_default_index_flags=lambda *args, **kwargs: _non_default_index_flags(*args, **kwargs),
        _validate_pr_conflict_recovery_identity=lambda *args, **kwargs: _validate_pr_conflict_recovery_identity(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_validate_blocked_pr_without_conflict = _bind_effects(
    pr_ownership._validate_blocked_pr_without_conflict, lambda: pr_ownership.ValidateBlockedPrWithoutConflictEffects(
        _ignored_worktree_paths=lambda *args, **kwargs: _ignored_worktree_paths(*args, **kwargs),
        _non_default_index_flags=lambda *args, **kwargs: _non_default_index_flags(*args, **kwargs),
        _validate_pr_conflict_recovery_identity=lambda *args, **kwargs: _validate_pr_conflict_recovery_identity(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_is_pr_conflict_preflight_block = pr_ownership._is_pr_conflict_preflight_block

_validate_active_pr_conflict_recovery = _bind_effects(
    pr_ownership._validate_active_pr_conflict_recovery, lambda: pr_ownership.ValidateActivePrConflictRecoveryEffects(
        _fetch_pr_conflict_recovery_refs=lambda *args, **kwargs: _fetch_pr_conflict_recovery_refs(*args, **kwargs),
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

remote_branch_oid = _bind_effects(
    publication_service.remote_branch_oid, lambda: publication_service.RemoteBranchOidEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_owned_publication_plan = _bind_effects(
    publication_service._owned_publication_plan, lambda: publication_service.OwnedPublicationPlanEffects(
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

record_remote_publication_branch = _bind_effects(
    publication_service.record_remote_publication_branch, lambda: publication_service.RecordRemotePublicationBranchEffects(
        _owned_publication_plan=lambda *args, **kwargs: _owned_publication_plan(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

push_managed_publication_branch = _bind_effects(
    publication_service.push_managed_publication_branch, lambda: publication_service.PushManagedPublicationBranchEffects(
        _owned_publication_plan=lambda *args, **kwargs: _owned_publication_plan(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        record_remote_publication_branch=lambda *args, **kwargs: record_remote_publication_branch(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

push_owned_pr_conflict_recovery = _bind_effects(
    publication_service.push_owned_pr_conflict_recovery, lambda: publication_service.PushOwnedPrConflictRecoveryEffects(
        _fetch_pr_conflict_recovery_refs=lambda *args, **kwargs: _fetch_pr_conflict_recovery_refs(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

recover_confirmed_pr_conflict_push = _bind_effects(
    publication_service.recover_confirmed_pr_conflict_push, lambda: publication_service.RecoverConfirmedPrConflictPushEffects(
        _fetch_pr_conflict_recovery_refs=lambda *args, **kwargs: _fetch_pr_conflict_recovery_refs(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_validate_managed_path_boundary = _bind_effects(
    workspace_service._validate_managed_path_boundary, lambda: workspace_service.ValidateManagedPathBoundaryEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
    ),
)

_worktree_catalog = _bind_effects(
    workspace_service._worktree_catalog, lambda: workspace_service.WorktreeCatalogEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_rebase_head_name = _bind_effects(
    workspace_service._rebase_head_name, lambda: workspace_service.RebaseHeadNameEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_rebase_in_progress_for_branch = _bind_effects(
    workspace_service._rebase_in_progress_for_branch, lambda: workspace_service.RebaseInProgressForBranchEffects(
        _rebase_head_name=lambda *args, **kwargs: _rebase_head_name(*args, **kwargs),
    ),
)

_owned_rebase_state_dir = _bind_effects(
    direct_rebase_service._owned_rebase_state_dir, lambda: direct_rebase_service.OwnedRebaseStateDirEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        expected_execplan_workspace=lambda *args, **kwargs: expected_execplan_workspace(*args, **kwargs),
    ),
)

_owned_rebase_metadata = _bind_effects(
    direct_rebase_service._owned_rebase_metadata, lambda: direct_rebase_service.OwnedRebaseMetadataEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_owned_rebase_journal_state = _bind_effects(
    direct_rebase_service._owned_rebase_journal_state, lambda: direct_rebase_service.OwnedRebaseJournalStateEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _owned_rebase_metadata=lambda *args, **kwargs: _owned_rebase_metadata(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_current_rebase_service = _bind_effects(
    direct_rebase_service._current_rebase_service, lambda: direct_rebase_service.CurrentRebaseServiceEffects(
        _managed_rebase_runtime_effects=lambda *args, **kwargs: _managed_rebase_runtime_effects(*args, **kwargs),
    ),
)

_finish_owned_direct_rebase = _bind_effects(
    direct_rebase_service._finish_owned_direct_rebase, lambda: direct_rebase_service.FinishOwnedDirectRebaseEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _owned_rebase_journal_state=lambda *args, **kwargs: _owned_rebase_journal_state(*args, **kwargs),
    ),
)

_journaled_direct_rebase = _bind_effects(
    direct_rebase_service._journaled_direct_rebase, lambda: direct_rebase_service.JournaledDirectRebaseEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _complete_direct_rebase_checkpoint=lambda *args, **kwargs: _complete_direct_rebase_checkpoint(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _finish_owned_direct_rebase=lambda *args, **kwargs: _finish_owned_direct_rebase(*args, **kwargs),
        _owned_rebase_journal_state=lambda *args, **kwargs: _owned_rebase_journal_state(*args, **kwargs),
        _owned_rebase_metadata=lambda *args, **kwargs: _owned_rebase_metadata(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
    ),
)

_direct_rebase_history_proven = _bind_effects(
    direct_rebase_service._direct_rebase_history_proven, lambda: direct_rebase_service.DirectRebaseHistoryProvenEffects(
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
    ),
)

_direct_rebase_result_matches_observation = direct_rebase_service._direct_rebase_result_matches_observation

_direct_rebase_restored_stage_matches = _bind_effects(
    direct_rebase_service._direct_rebase_restored_stage_matches, lambda: direct_rebase_service.DirectRebaseRestoredStageMatchesEffects(
        _direct_rebase_result_matches_observation=lambda *args, **kwargs: _direct_rebase_result_matches_observation(*args, **kwargs),
    ),
)

_complete_direct_rebase_checkpoint = _bind_effects(
    direct_rebase_service._complete_direct_rebase_checkpoint, lambda: direct_rebase_service.CompleteDirectRebaseCheckpointEffects(
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _owned_rebase_journal_state=lambda *args, **kwargs: _owned_rebase_journal_state(*args, **kwargs),
        _owned_rebase_state_dir=lambda *args, **kwargs: _owned_rebase_state_dir(*args, **kwargs),
        _publish_direct_rebase_checkpoint=lambda *args, **kwargs: _publish_direct_rebase_checkpoint(*args, **kwargs),
    ),
)

_publish_direct_rebase_checkpoint = _bind_effects(
    direct_rebase_service._publish_direct_rebase_checkpoint, lambda: direct_rebase_service.PublishDirectRebaseCheckpointEffects(
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
    ),
)

validate_managed_worktree = _bind_effects(
    workspace_service.validate_managed_worktree, lambda: workspace_service.ValidateManagedWorktreeEffects(
        _workspace_effects_validate_managed_worktree=lambda *args, **kwargs: _workspace_effects_validate_managed_worktree(*args, **kwargs),
    ),
)

validate_reserved_worktree = _bind_effects(
    workspace_service.validate_reserved_worktree, lambda: workspace_service.ValidateReservedWorktreeEffects(
        _workspace_effects_validate_reserved_worktree=lambda *args, **kwargs: _workspace_effects_validate_reserved_worktree(*args, **kwargs),
    ),
)

make_worktree = _bind_effects(
    workspace_service.make_worktree, lambda: workspace_service.MakeWorktreeEffects(
        _workspace_effects_make_worktree=lambda *args, **kwargs: _workspace_effects_make_worktree(*args, **kwargs),
    ),
)

make_planning_worktree = _bind_effects(
    workspace_service.make_planning_worktree, lambda: workspace_service.MakePlanningWorktreeEffects(
        _workspace_effects_make_planning_worktree=lambda *args, **kwargs: _workspace_effects_make_planning_worktree(*args, **kwargs),
    ),
)

validate_execplan_workspace_identity = _bind_effects(
    workspace_service.validate_execplan_workspace_identity, lambda: workspace_service.ValidateExecplanWorkspaceIdentityEffects(
        _workspace_effects_validate_execplan_workspace_identity=lambda *args, **kwargs: _workspace_effects_validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_nul_git_paths = _bind_effects(
    scope_adapter._nul_git_paths, lambda: scope_adapter.NulGitPathsEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_index_entries = _bind_effects(
    scope_adapter._index_entries, lambda: scope_adapter.IndexEntriesEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_shared_ref_entries = _bind_effects(
    scope_adapter._shared_ref_entries, lambda: scope_adapter.SharedRefEntriesEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_registered_worktree_entries = _bind_effects(
    scope_adapter._registered_worktree_entries, lambda: scope_adapter.RegisteredWorktreeEntriesEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_config_entries = _bind_effects(
    scope_adapter._config_entries, lambda: scope_adapter.ConfigEntriesEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_with_proven_legacy_ignore_config = _bind_effects(
    scope_adapter._with_proven_legacy_ignore_config, lambda: scope_adapter.WithProvenLegacyIgnoreConfigEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_git_operation_entries = _bind_effects(
    scope_adapter._git_operation_entries, lambda: scope_adapter.GitOperationEntriesEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_path_signature = _bind_effects(
    scope_adapter._path_signature, lambda: scope_adapter.PathSignatureEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_capture_local_worktree_state = _bind_effects(
    scope_adapter._capture_local_worktree_state, lambda: scope_adapter.CaptureLocalWorktreeStateEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_worktree_state_signature = _bind_effects(
    scope_adapter._worktree_state_signature, lambda: scope_adapter.WorktreeStateSignatureEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_ignore_rule_source = _bind_effects(
    scope_adapter._ignore_rule_source, lambda: scope_adapter.IgnoreRuleSourceEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_normalize_legacy_ignored_snapshot = _bind_effects(
    scope_adapter._normalize_legacy_ignored_snapshot, lambda: scope_adapter.NormalizeLegacyIgnoredSnapshotEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

capture_git_snapshot = _bind_effects(
    scope_adapter.capture_git_snapshot, lambda: scope_adapter.CaptureGitSnapshotEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

agent_snapshot_changes = _bind_effects(
    scope_adapter.agent_snapshot_changes, lambda: scope_adapter.AgentSnapshotChangesEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_scope_snapshot_with_start_ignore_rules = _bind_effects(
    scope_adapter._scope_snapshot_with_start_ignore_rules, lambda: scope_adapter.ScopeSnapshotWithStartIgnoreRulesEffects(
        _scope_observation_effects=lambda *args, **kwargs: _scope_observation_effects(*args, **kwargs),
    ),
)

_agent_git_resources = _bind_effects(
    scope_adapter._agent_git_resources, lambda: scope_adapter.AgentGitResourcesEffects(
        _scope_validation_effects=lambda *args, **kwargs: _scope_validation_effects(*args, **kwargs),
    ),
)

validate_agent_scope = _bind_effects(
    scope_adapter.validate_agent_scope, lambda: scope_adapter.ValidateAgentScopeEffects(
        _scope_validation_effects=lambda *args, **kwargs: _scope_validation_effects(*args, **kwargs),
    ),
)

_git_with_index = _bind_effects(
    scope_adapter._git_with_index, lambda: scope_adapter.GitWithIndexEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

_hash_git_blob = _bind_effects(
    scope_adapter._hash_git_blob, lambda: scope_adapter.HashGitBlobEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

_agent_worktree_tree = _bind_effects(
    scope_adapter._agent_worktree_tree, lambda: scope_adapter.AgentWorktreeTreeEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

_commit_tree = _bind_effects(
    scope_adapter._commit_tree, lambda: scope_adapter.CommitTreeEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

_capture_agent_baseline = _bind_effects(
    scope_adapter._capture_agent_baseline, lambda: scope_adapter.CaptureAgentBaselineEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

begin_agent_scope = _bind_effects(
    scope_adapter.begin_agent_scope, lambda: scope_adapter.BeginAgentScopeEffects(
        _scope_lifecycle_effects=lambda *args, **kwargs: _scope_lifecycle_effects(*args, **kwargs),
    ),
)

clear_agent_scope = _bind_effects(
    scope_adapter.clear_agent_scope, lambda: scope_adapter.ClearAgentScopeEffects(
        _scope_lifecycle_effects=lambda *args, **kwargs: _scope_lifecycle_effects(*args, **kwargs),
    ),
)

validate_planning_workspace_identity = _bind_effects(
    planning_workspace.validate_planning_workspace_identity, lambda: planning_workspace.ValidatePlanningWorkspaceIdentityEffects(
        _validate_managed_path_boundary=lambda *args, **kwargs: _validate_managed_path_boundary(*args, **kwargs),
    ),
)

validate_planning_worktree_ownership = _bind_effects(
    planning_workspace.validate_planning_worktree_ownership, lambda: planning_workspace.ValidatePlanningWorktreeOwnershipEffects(
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        validate_planning_workspace_identity=lambda *args, **kwargs: validate_planning_workspace_identity(*args, **kwargs),
    ),
)

changed_files = _bind_effects(
    workspace_service.changed_files, lambda: workspace_service.ChangedFilesEffects(
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        _unstaged_git_paths=lambda worktree: scope_observation._unstaged_git_paths(
            worktree, effects=_scope_observation_effects()),
    ),
)

branch_has_changes = _bind_effects(
    workspace_service.branch_has_changes, lambda: workspace_service.BranchHasChangesEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

assess_cleanup = _bind_effects(
    cleanup_service.assess_cleanup, lambda: cleanup_service.AssessCleanupEffects(
        _capture_local_worktree_state=lambda *args, **kwargs: _capture_local_worktree_state(*args, **kwargs),
        _git_operation_entries=lambda *args, **kwargs: _git_operation_entries(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

validate_pending_cleanup_worktree = _bind_effects(
    cleanup_service.validate_pending_cleanup_worktree, lambda: cleanup_service.ValidatePendingCleanupWorktreeEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

remove_cleanup_worktree = _bind_effects(
    cleanup_service.remove_cleanup_worktree, lambda: cleanup_service.RemoveCleanupWorktreeEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _capture_local_worktree_state=lambda *args, **kwargs: _capture_local_worktree_state(*args, **kwargs),
        _git_operation_entries=lambda *args, **kwargs: _git_operation_entries(*args, **kwargs),
        _managed_worktree_marker=lambda *args, **kwargs: _managed_worktree_marker(*args, **kwargs),
        _managed_worktree_transition_state=lambda *args, **kwargs: _managed_worktree_transition_state(*args, **kwargs),
        _write_managed_worktree_receipt=lambda *args, **kwargs: _write_managed_worktree_receipt(*args, **kwargs),
        assess_cleanup=lambda *args, **kwargs: assess_cleanup(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_store_cleanup_pending = cleanup_service._store_cleanup_pending

recheck_cleanup_pending = _bind_effects(
    cleanup_service.recheck_cleanup_pending, lambda: cleanup_service.RecheckCleanupPendingEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        _record_merged_worktree_cleanup=lambda *args, **kwargs: _record_merged_worktree_cleanup(*args, **kwargs),
        _recover_managed_worktree_removal=lambda *args, **kwargs: _recover_managed_worktree_removal(*args, **kwargs),
        assess_cleanup=lambda *args, **kwargs: assess_cleanup(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        remove_cleanup_worktree=lambda *args, **kwargs: remove_cleanup_worktree(*args, **kwargs),
        validate_pending_cleanup_worktree=lambda *args, **kwargs: validate_pending_cleanup_worktree(*args, **kwargs),
    ),
)

planning_diagnostics_path = diagnostics.planning_diagnostics_path

preserve_planning_diagnostics = _bind_effects(
    diagnostics.preserve_planning_diagnostics, lambda: diagnostics.PreservePlanningDiagnosticsEffects(
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        planning_diagnostics_path=lambda *args, **kwargs: planning_diagnostics_path(*args, **kwargs),
    ),
)

clear_planning_validation_state = _bind_effects(
    project_state_adapter.clear_planning_validation_state, lambda: project_state_adapter.ClearPlanningValidationStateEffects(
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

_clear_active_values = project_state_adapter._clear_active_values

_clear_active_in_current_transaction = project_state_adapter._clear_active_in_current_transaction

clear_active = project_state_adapter.clear_active

_execplan_input_paths = project_state_adapter._execplan_input_paths

_execplan_input_fingerprint = plan_validation._execplan_input_fingerprint

_worktree_execplan_input_fingerprint = _bind_effects(
    plan_validation._worktree_execplan_input_fingerprint, lambda: plan_validation.WorktreeExecplanInputFingerprintEffects(
        _execplan_input_fingerprint=lambda *args, **kwargs: _execplan_input_fingerprint(*args, **kwargs),
        _execplan_input_paths=lambda *args, **kwargs: _execplan_input_paths(*args, **kwargs),
    ),
)

_revision_execplan_input_fingerprint = _bind_effects(
    plan_validation._revision_execplan_input_fingerprint, lambda: plan_validation.RevisionExecplanInputFingerprintEffects(
        _execplan_input_fingerprint=lambda *args, **kwargs: _execplan_input_fingerprint(*args, **kwargs),
        _execplan_input_paths=lambda *args, **kwargs: _execplan_input_paths(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_revision_execplan_source_fingerprint = _bind_effects(
    plan_validation._revision_execplan_source_fingerprint, lambda: plan_validation.RevisionExecplanSourceFingerprintEffects(
        _execplan_input_fingerprint=lambda *args, **kwargs: _execplan_input_fingerprint(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_revision_execplan_body_hash = _bind_effects(
    plan_validation._revision_execplan_body_hash, lambda: plan_validation.RevisionExecplanBodyHashEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_revision_changed_paths = _bind_effects(
    plan_validation._revision_changed_paths, lambda: plan_validation.RevisionChangedPathsEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

unmet_execplan_dependencies = plan_validation.unmet_execplan_dependencies

_revision_review_issue_fingerprint = _bind_effects(
    plan_validation._revision_review_issue_fingerprint, lambda: plan_validation.RevisionReviewIssueFingerprintEffects(
        _execplan_input_fingerprint=lambda *args, **kwargs: _execplan_input_fingerprint(*args, **kwargs),
        _review_issue_paths=lambda *args, **kwargs: _review_issue_paths(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_blocked_validation_base_revision = plan_validation._blocked_validation_base_revision

_blocked_validation_input_fingerprints = _bind_effects(
    plan_validation._blocked_validation_input_fingerprints, lambda: plan_validation.BlockedValidationInputFingerprintsEffects(
        _blocked_validation_base_revision=lambda *args, **kwargs: _blocked_validation_base_revision(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_execplan_metadata_identity_error = _bind_effects(
    plan_validation._execplan_metadata_identity_error, lambda: plan_validation.ExecplanMetadataIdentityErrorEffects(
        _current_execplan_metadata=lambda *args, **kwargs: _current_execplan_metadata(*args, **kwargs),
    ),
)

_current_execplan_metadata = plan_validation._current_execplan_metadata

_blocked_execplan_metadata_identity_error = _bind_effects(
    plan_validation._blocked_execplan_metadata_identity_error, lambda: plan_validation.BlockedExecplanMetadataIdentityErrorEffects(
        _execplan_metadata_identity_error=lambda *args, **kwargs: _execplan_metadata_identity_error(*args, **kwargs),
    ),
)

validate_active_execplan_metadata = _bind_effects(
    plan_validation.validate_active_execplan_metadata, lambda: plan_validation.ValidateActiveExecplanMetadataEffects(
        _current_execplan_metadata=lambda *args, **kwargs: _current_execplan_metadata(*args, **kwargs),
        _execplan_metadata_identity_error=lambda *args, **kwargs: _execplan_metadata_identity_error(*args, **kwargs),
        validate_execplan=lambda *args, **kwargs: validate_execplan(*args, **kwargs),
    ),
)

validate_worker_workspace_scope = _bind_effects(
    scope_adapter.validate_worker_workspace_scope, lambda: scope_adapter.ValidateWorkerWorkspaceScopeEffects(
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        validate_active_execplan_metadata=lambda *args, **kwargs: validate_active_execplan_metadata(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)


_git_common_directory = _bind_effects(
    bootstrap_adapter._git_common_directory, lambda: bootstrap_adapter.GitCommonDirectoryEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

is_loop_manager_repository = _bind_effects(
    bootstrap_adapter.is_loop_manager_repository, lambda: bootstrap_adapter.IsLoopManagerRepositoryEffects(
        ROOT=ROOT,
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _git_common_directory=lambda *args, **kwargs: _git_common_directory(*args, **kwargs),
    ),
)


_published_plan_allows_common_plans = _bind_effects(
    guidance_service._published_plan_allows_common_plans, lambda: guidance_service.PublishedPlanAllowsCommonPlansEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
    ),
)


_plan_block_selected_for_release = _bind_effects(
    guidance_service._plan_block_selected_for_release, lambda: guidance_service.PlanBlockSelectedForReleaseEffects(
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
    ),
)

resume_retired_guidance_block = _bind_effects(
    guidance_service.resume_retired_guidance_block, lambda: guidance_service.ResumeRetiredGuidanceBlockEffects(
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _plan_block_selected_for_release=lambda *args, **kwargs: _plan_block_selected_for_release(*args, **kwargs),
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        clear_agent_scope=lambda *args, **kwargs: clear_agent_scope(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)


_blocked_execplan_metadata_error = _bind_effects(
    block_service._blocked_execplan_metadata_error, lambda: block_service.BlockedExecplanMetadataErrorEffects(
        _blocked_execplan_metadata_identity_error=lambda *args, **kwargs: _blocked_execplan_metadata_identity_error(*args, **kwargs),
        _parse_execplan=lambda *args, **kwargs: _parse_execplan(*args, **kwargs),
    ),
)

_blocked_execplan_revision_metadata_error = _bind_effects(
    block_service._blocked_execplan_revision_metadata_error, lambda: block_service.BlockedExecplanRevisionMetadataErrorEffects(
        _blocked_execplan_metadata_identity_error=lambda *args, **kwargs: _blocked_execplan_metadata_identity_error(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        published_execplan=lambda *args, **kwargs: published_execplan(*args, **kwargs),
    ),
)

_update_blocked_error = block_service._update_blocked_error

block_active_execplan = _bind_effects(
    block_service.block_active_execplan, lambda: block_service.BlockActiveExecplanEffects(
        _final_block_recovery_decision=lambda *args, **kwargs: _final_block_recovery_decision(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _revision_review_issue_fingerprint=lambda *args, **kwargs: _revision_review_issue_fingerprint(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
    ),
)

block_verify_timeout_limit = _bind_effects(
    block_service.block_verify_timeout_limit, lambda: block_service.BlockVerifyTimeoutLimitEffects(
        block_active_execplan=lambda *args, **kwargs: block_active_execplan(*args, **kwargs),
        redact_secrets=lambda *args, **kwargs: redact_secrets(*args, **kwargs),
    ),
)

_final_block_recovery_decision = block_service._final_block_recovery_decision

transition_validation_failure = _bind_effects(
    block_service.transition_validation_failure, lambda: block_service.TransitionValidationFailureEffects(
        _blocked_validation_base_revision=lambda *args, **kwargs: _blocked_validation_base_revision(*args, **kwargs),
        _blocked_validation_input_fingerprints=lambda *args, **kwargs: _blocked_validation_input_fingerprints(*args, **kwargs),
        _deserialize_failure_history=lambda *args, **kwargs: _deserialize_failure_history(*args, **kwargs),
        _deserialize_review_level_history=lambda *args, **kwargs: _deserialize_review_level_history(*args, **kwargs),
        _final_block_recovery_decision=lambda *args, **kwargs: _final_block_recovery_decision(*args, **kwargs),
        _revision_review_issue_fingerprint=lambda *args, **kwargs: _revision_review_issue_fingerprint(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        evaluate_review_worker_level=lambda *args, **kwargs: evaluate_review_worker_level(*args, **kwargs),
        redact_secrets=lambda *args, **kwargs: redact_secrets(*args, **kwargs),
        validation_failure_fingerprints=lambda *args, **kwargs: validation_failure_fingerprints(*args, **kwargs),
    ),
)

block_active_execplan_from_revision = _bind_effects(
    block_service.block_active_execplan_from_revision, lambda: block_service.BlockActiveExecplanFromRevisionEffects(
        _final_block_recovery_decision=lambda *args, **kwargs: _final_block_recovery_decision(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
    ),
)

block_persisted_agent_scope_violation = _bind_effects(
    scope_state_service.block_persisted_agent_scope_violation, lambda: scope_state_service.BlockPersistedAgentScopeViolationEffects(
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        block_active_execplan=lambda *args, **kwargs: block_active_execplan(*args, **kwargs),
        block_active_execplan_from_revision=lambda *args, **kwargs: block_active_execplan_from_revision(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

recover_persisted_agent_worktree = _bind_effects(
    scope_state_service.recover_persisted_agent_worktree, lambda: scope_state_service.RecoverPersistedAgentWorktreeEffects(
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_proven_recovered_worktree_baseline = _bind_effects(
    scope_state_service._proven_recovered_worktree_baseline, lambda: scope_state_service.ProvenRecoveredWorktreeBaselineEffects(
        _scope_recovery_effects=lambda *args, **kwargs: _scope_recovery_effects(*args, **kwargs),
    ),
)

validate_persisted_agent_scope = _bind_effects(
    scope_state_service.validate_persisted_agent_scope, lambda: scope_state_service.ValidatePersistedAgentScopeEffects(
        _scope_recovery_effects=lambda *args, **kwargs: _scope_recovery_effects(*args, **kwargs),
    ),
)

refresh_persisted_agent_scope_after_manager_git = _bind_effects(
    scope_state_service.refresh_persisted_agent_scope_after_manager_git, lambda: scope_state_service.RefreshPersistedAgentScopeAfterManagerGitEffects(
        _scope_recovery_effects=lambda *args, **kwargs: _scope_recovery_effects(*args, **kwargs),
    ),
)

_remote_adoption_agent_baseline = _bind_effects(
    scope_state_service._remote_adoption_agent_baseline, lambda: scope_state_service.RemoteAdoptionAgentBaselineEffects(
        _scope_recovery_effects=lambda *args, **kwargs: _scope_recovery_effects(*args, **kwargs),
    ),
)

validate_blocked_agent_scope = _bind_effects(
    scope_state_service.validate_blocked_agent_scope, lambda: scope_state_service.ValidateBlockedAgentScopeEffects(
        _scope_recovery_effects=lambda *args, **kwargs: _scope_recovery_effects(*args, **kwargs),
    ),
)

_reconcile_blocked_reviewer_plan = scope_state_service._reconcile_blocked_reviewer_plan

_deserialize_blocked_agent_recovery = blocked_agent_service._deserialize_blocked_agent_recovery

_restore_agent_index_flags = _bind_effects(
    blocked_agent_service._restore_agent_index_flags, lambda: blocked_agent_service.RestoreAgentIndexFlagsEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

_restore_agent_staged_entries = _bind_effects(
    blocked_agent_service._restore_agent_staged_entries, lambda: blocked_agent_service.RestoreAgentStagedEntriesEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

_restore_agent_baseline = _bind_effects(
    blocked_agent_service._restore_agent_baseline, lambda: blocked_agent_service.RestoreAgentBaselineEffects(
        _scope_baseline_effects=lambda *args, **kwargs: _scope_baseline_effects(*args, **kwargs),
    ),
)

_worktree_relative_target = blocked_agent_service._worktree_relative_target

_remove_interrupted_recovery_temp = _bind_effects(
    blocked_agent_service._remove_interrupted_recovery_temp, lambda: blocked_agent_service.RemoveInterruptedRecoveryTempEffects(
        _worktree_relative_target=lambda *args, **kwargs: _worktree_relative_target(*args, **kwargs),
    ),
)

_atomic_restore_file = _bind_effects(
    blocked_agent_service._atomic_restore_file, lambda: blocked_agent_service.AtomicRestoreFileEffects(
        _remove_interrupted_recovery_temp=lambda *args, **kwargs: _remove_interrupted_recovery_temp(*args, **kwargs),
        _worktree_relative_target=lambda *args, **kwargs: _worktree_relative_target(*args, **kwargs),
    ),
)

_serialize_blocked_agent_recovery = blocked_agent_service._serialize_blocked_agent_recovery

_blocked_recovery_stash_oid = _bind_effects(
    blocked_agent_service._blocked_recovery_stash_oid, lambda: blocked_agent_service.BlockedRecoveryStashOidEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_restore_blocked_agent_recovery_plan = _bind_effects(
    blocked_agent_service._restore_blocked_agent_recovery_plan, lambda: blocked_agent_service.RestoreBlockedAgentRecoveryPlanEffects(
        _atomic_restore_file=lambda *args, **kwargs: _atomic_restore_file(*args, **kwargs),
        _worktree_relative_target=lambda *args, **kwargs: _worktree_relative_target(*args, **kwargs),
    ),
)

_recovered_agent_local_state_matches = _bind_effects(
    blocked_agent_service._recovered_agent_local_state_matches, lambda: blocked_agent_service.RecoveredAgentLocalStateMatchesEffects(
        _worktree_relative_target=lambda *args, **kwargs: _worktree_relative_target(*args, **kwargs),
    ),
)

_mark_blocked_agent_isolated = _bind_effects(
    blocked_agent_service._mark_blocked_agent_isolated, lambda: blocked_agent_service.MarkBlockedAgentIsolatedEffects(
        _serialize_blocked_agent_recovery=lambda *args, **kwargs: _serialize_blocked_agent_recovery(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

_complete_blocked_agent_recovery = _bind_effects(
    blocked_agent_service._complete_blocked_agent_recovery, lambda: blocked_agent_service.CompleteBlockedAgentRecoveryEffects(
        _blocked_recovery_stash_oid=lambda *args, **kwargs: _blocked_recovery_stash_oid(*args, **kwargs),
        _config_entries=lambda *args, **kwargs: _config_entries(*args, **kwargs),
        _deserialize_blocked_agent_recovery=lambda *args, **kwargs: _deserialize_blocked_agent_recovery(*args, **kwargs),
        _mark_blocked_agent_isolated=lambda *args, **kwargs: _mark_blocked_agent_isolated(*args, **kwargs),
        _recovered_agent_local_state_matches=lambda *args, **kwargs: _recovered_agent_local_state_matches(*args, **kwargs),
        _remove_interrupted_recovery_temp=lambda *args, **kwargs: _remove_interrupted_recovery_temp(*args, **kwargs),
        _restore_agent_baseline=lambda *args, **kwargs: _restore_agent_baseline(*args, **kwargs),
        _restore_blocked_agent_recovery_plan=lambda *args, **kwargs: _restore_blocked_agent_recovery_plan(*args, **kwargs),
        _same_local_snapshot=lambda *args, **kwargs: _same_local_snapshot(*args, **kwargs),
        _serialize_blocked_agent_recovery=lambda *args, **kwargs: _serialize_blocked_agent_recovery(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

preserve_blocked_agent_changes = _bind_effects(
    blocked_agent_service.preserve_blocked_agent_changes, lambda: blocked_agent_service.PreserveBlockedAgentChangesEffects(
        _blocked_agent_local_baseline=lambda *args, **kwargs: _blocked_agent_local_baseline(*args, **kwargs),
        _complete_blocked_agent_recovery=lambda *args, **kwargs: _complete_blocked_agent_recovery(*args, **kwargs),
        _deserialize_blocked_agent_recovery=lambda *args, **kwargs: _deserialize_blocked_agent_recovery(*args, **kwargs),
        _reconcile_blocked_reviewer_plan=lambda *args, **kwargs: _reconcile_blocked_reviewer_plan(*args, **kwargs),
        _serialize_blocked_agent_recovery=lambda *args, **kwargs: _serialize_blocked_agent_recovery(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

resume_interrupted_blocked_agent_recovery = _bind_effects(
    blocked_agent_service.resume_interrupted_blocked_agent_recovery, lambda: blocked_agent_service.ResumeInterruptedBlockedAgentRecoveryEffects(
        _complete_blocked_agent_recovery=lambda *args, **kwargs: _complete_blocked_agent_recovery(*args, **kwargs),
        _config_entries=lambda *args, **kwargs: _config_entries(*args, **kwargs),
        _deserialize_blocked_agent_recovery=lambda *args, **kwargs: _deserialize_blocked_agent_recovery(*args, **kwargs),
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_requeue_blocked_execplan = _bind_effects(
    blocked_plan_service._requeue_blocked_execplan, lambda: blocked_plan_service.RequeueBlockedExecplanEffects(
        _clear_active_values=lambda *args, **kwargs: _clear_active_values(*args, **kwargs),
    ),
)

_record_blocked_resume_head = blocked_plan_service._record_blocked_resume_head

_blocked_resume_worktree_matches = _bind_effects(
    blocked_plan_service._blocked_resume_worktree_matches, lambda: blocked_plan_service.BlockedResumeWorktreeMatchesEffects(
        _restore_agent_staged_entries=lambda *args, **kwargs: _restore_agent_staged_entries(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_resume_interrupted_blocked_rebase = _bind_effects(
    blocked_plan_service._resume_interrupted_blocked_rebase, lambda: blocked_plan_service.ResumeInterruptedBlockedRebaseEffects(
        _scope_reconciliation_effects_resume_interrupted_blocked_rebase=lambda *args, **kwargs: _scope_reconciliation_effects_resume_interrupted_blocked_rebase(*args, **kwargs),
    ),
)

resume_blocked_execplan_if_inputs_changed = _bind_effects(
    blocked_plan_service.resume_blocked_execplan_if_inputs_changed, lambda: blocked_plan_service.ResumeBlockedExecplanIfInputsChangedEffects(
        _blocked_execplan_metadata_error=lambda *args, **kwargs: _blocked_execplan_metadata_error(*args, **kwargs),
        _blocked_execplan_revision_metadata_error=lambda *args, **kwargs: _blocked_execplan_revision_metadata_error(*args, **kwargs),
        _blocked_resume_worktree_matches=lambda *args, **kwargs: _blocked_resume_worktree_matches(*args, **kwargs),
        _complete_direct_rebase_checkpoint=lambda *args, **kwargs: _complete_direct_rebase_checkpoint(*args, **kwargs),
        _direct_rebase_history_proven=lambda *args, **kwargs: _direct_rebase_history_proven(*args, **kwargs),
        _journaled_direct_rebase=lambda *args, **kwargs: _journaled_direct_rebase(*args, **kwargs),
        _owned_rebase_journal_state=lambda *args, **kwargs: _owned_rebase_journal_state(*args, **kwargs),
        _plan_block_selected_for_release=lambda *args, **kwargs: _plan_block_selected_for_release(*args, **kwargs),
        _reconcile_rebase_evidence_block=lambda *args, **kwargs: _reconcile_rebase_evidence_block(*args, **kwargs),
        _reconcile_scope_baseline_plan_block=lambda *args, **kwargs: _reconcile_scope_baseline_plan_block(*args, **kwargs),
        _record_blocked_resume_head=lambda *args, **kwargs: _record_blocked_resume_head(*args, **kwargs),
        _requeue_blocked_execplan=lambda *args, **kwargs: _requeue_blocked_execplan(*args, **kwargs),
        _restore_agent_staged_entries=lambda *args, **kwargs: _restore_agent_staged_entries(*args, **kwargs),
        _resume_interrupted_blocked_rebase=lambda *args, **kwargs: _resume_interrupted_blocked_rebase(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _revision_review_issue_fingerprint=lambda *args, **kwargs: _revision_review_issue_fingerprint(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        clear_agent_scope=lambda *args, **kwargs: clear_agent_scope(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        preserve_blocked_agent_changes=lambda *args, **kwargs: preserve_blocked_agent_changes(*args, **kwargs),
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
    ),
)

_record_merged_worktree_cleanup = _bind_effects(
    merge_cleanup_service._record_merged_worktree_cleanup, lambda: merge_cleanup_service.RecordMergedWorktreeCleanupEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
    ),
)

_proven_merged_cleanup_paths = _bind_effects(
    merge_cleanup_service._proven_merged_cleanup_paths, lambda: merge_cleanup_service.ProvenMergedCleanupPathsEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
    ),
)

_record_manager_base_fetch_intent = _bind_effects(
    base_fetch_service._record_manager_base_fetch_intent, lambda: base_fetch_service.RecordManagerBaseFetchIntentEffects(
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        _manager_base_fetch_applied=lambda *args, **kwargs: _manager_base_fetch_applied(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_manager_base_fetch_applied = _bind_effects(
    base_fetch_service._manager_base_fetch_applied, lambda: base_fetch_service.ManagerBaseFetchAppliedEffects(
        _tracking_ref_oid=lambda *args, **kwargs: _tracking_ref_oid(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_perform_manager_base_fetch = _bind_effects(
    base_fetch_service._perform_manager_base_fetch, lambda: base_fetch_service.PerformManagerBaseFetchEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_finish_manager_base_fetch = _bind_effects(
    base_fetch_service._finish_manager_base_fetch, lambda: base_fetch_service.FinishManagerBaseFetchEffects(
        _manager_base_fetch_applied=lambda *args, **kwargs: _manager_base_fetch_applied(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_journaled_manager_base_fetch = _bind_effects(
    base_fetch_service._journaled_manager_base_fetch, lambda: base_fetch_service.JournaledManagerBaseFetchEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _finish_manager_base_fetch=lambda *args, **kwargs: _finish_manager_base_fetch(*args, **kwargs),
        _manager_base_fetch_applied=lambda *args, **kwargs: _manager_base_fetch_applied(*args, **kwargs),
        _perform_manager_base_fetch=lambda *args, **kwargs: _perform_manager_base_fetch(*args, **kwargs),
        _tracking_ref_oid=lambda *args, **kwargs: _tracking_ref_oid(*args, **kwargs),
    ),
)

_proven_manager_base_refs = _bind_effects(
    base_fetch_service._proven_manager_base_refs, lambda: base_fetch_service.ProvenManagerBaseRefsEffects(
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        _manager_base_fetch_applied=lambda *args, **kwargs: _manager_base_fetch_applied(*args, **kwargs),
    ),
)

_reconciled_manager_scope_metadata = _bind_effects(
    base_fetch_service._reconciled_manager_scope_metadata, lambda: base_fetch_service.ReconciledManagerScopeMetadataEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        _proven_manager_base_refs=lambda *args, **kwargs: _proven_manager_base_refs(*args, **kwargs),
        _proven_merged_cleanup_paths=lambda *args, **kwargs: _proven_merged_cleanup_paths(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

complete_merged_worktree = _bind_effects(
    merge_cleanup_service.complete_merged_worktree, lambda: merge_cleanup_service.CompleteMergedWorktreeEffects(
        pr_for_branch=lambda *a, **kw: pr_for_branch(*a, **kw),
        _assess_missing_managed_cleanup=lambda *args, **kwargs: _assess_missing_managed_cleanup(*args, **kwargs),
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _clear_active_values=lambda *args, **kwargs: _clear_active_values(*args, **kwargs),
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _record_merged_worktree_cleanup=lambda *args, **kwargs: _record_merged_worktree_cleanup(*args, **kwargs),
        _store_cleanup_pending=lambda *args, **kwargs: _store_cleanup_pending(*args, **kwargs),
        assess_cleanup=lambda *args, **kwargs: assess_cleanup(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        remove_cleanup_worktree=lambda *args, **kwargs: remove_cleanup_worktree(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

complete_merged_execplan_worktree = _bind_effects(
    merge_cleanup_service.complete_merged_execplan_worktree, lambda: merge_cleanup_service.CompleteMergedExecplanWorktreeEffects(
        pr_for_branch=lambda *a, **kw: pr_for_branch(*a, **kw),
        _assess_missing_managed_cleanup=lambda *args, **kwargs: _assess_missing_managed_cleanup(*args, **kwargs),
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _clear_active_values=lambda *args, **kwargs: _clear_active_values(*args, **kwargs),
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        _record_merged_worktree_cleanup=lambda *args, **kwargs: _record_merged_worktree_cleanup(*args, **kwargs),
        _store_cleanup_pending=lambda *args, **kwargs: _store_cleanup_pending(*args, **kwargs),
        assess_cleanup=lambda *args, **kwargs: assess_cleanup(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        remove_cleanup_worktree=lambda *args, **kwargs: remove_cleanup_worktree(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

PullRequestSyncResult = contracts.PullRequestSyncResult

WaitingPullRequestObservation = contracts.WaitingPullRequestObservation

observe_waiting_pull_requests = _bind_effects(
    waiting_pr_service.observe_waiting_pull_requests, lambda: waiting_pr_service.ObserveWaitingPullRequestsEffects(
        base_head=lambda repo, base: git(repo, "rev-parse", f"refs/remotes/origin/{base}").stdout.strip(),
        _block_closed_pull_request=lambda *args, **kwargs: _block_closed_pull_request(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        _resume_reopened_pull_request=lambda *args, **kwargs: _resume_reopened_pull_request(*args, **kwargs),
        classify_pull_request_mergeability=lambda *args, **kwargs: classify_pull_request_mergeability(*args, **kwargs),
        complete_merged_execplan_worktree=lambda *args, **kwargs: complete_merged_execplan_worktree(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
    ),
)

select_waiting_pull_request_conflict = _bind_effects(
    waiting_pr_service.select_waiting_pull_request_conflict, lambda: waiting_pr_service.SelectWaitingPullRequestConflictEffects(
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
        reconcile_existing_pull_request=lambda *args, **kwargs: reconcile_existing_pull_request(*args, **kwargs),
    ),
)

synchronize_waiting_pull_requests = _bind_effects(
    waiting_pr_service.synchronize_waiting_pull_requests, lambda: waiting_pr_service.SynchronizeWaitingPullRequestsEffects(
        observe_waiting_pull_requests=lambda *args, **kwargs: observe_waiting_pull_requests(*args, **kwargs),
        select_waiting_pull_request_conflict=lambda *args, **kwargs: select_waiting_pull_request_conflict(*args, **kwargs),
    ),
)


_published_review_commit_intent_matches = _bind_effects(
    project_block_service._published_review_commit_intent_matches, lambda: project_block_service.PublishedReviewCommitIntentMatchesEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_resume_owned_checkpoint_project_block = _bind_effects(
    project_block_service._resume_owned_checkpoint_project_block, lambda: project_block_service.ResumeOwnedCheckpointProjectBlockEffects(
        _scope_reconciliation_effects_resume_owned_checkpoint_project_block=lambda *args, **kwargs: _scope_reconciliation_effects_resume_owned_checkpoint_project_block(*args, **kwargs),
    ),
)

_clear_project_block_without_losing_planning_error = project_block_service._clear_project_block_without_losing_planning_error

_reconcile_rebase_evidence_block = _bind_effects(
    project_block_service._reconcile_rebase_evidence_block, lambda: project_block_service.ReconcileRebaseEvidenceBlockEffects(
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        unmet_execplan_dependencies=lambda *args, **kwargs: unmet_execplan_dependencies(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
    ),
)

_reconcile_scope_baseline_plan_block = _bind_effects(
    project_block_service._reconcile_scope_baseline_plan_block, lambda: project_block_service.ReconcileScopeBaselinePlanBlockEffects(
        _owned_checkpoint_remote_oid=lambda *args, **kwargs: _owned_checkpoint_remote_oid(*args, **kwargs),
        _with_proven_legacy_ignore_config=lambda *args, **kwargs: _with_proven_legacy_ignore_config(*args, **kwargs),
        _normalize_legacy_ignored_snapshot=lambda *args, **kwargs: _normalize_legacy_ignored_snapshot(*args, **kwargs),
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        _reconciled_manager_scope_metadata=lambda *args, **kwargs: _reconciled_manager_scope_metadata(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        unmet_execplan_dependencies=lambda *args, **kwargs: unmet_execplan_dependencies(*args, **kwargs),
        validate_blocked_agent_scope=lambda *args, **kwargs: validate_blocked_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

resume_project_block_if_safe = _bind_effects(
    project_block_service.resume_project_block_if_safe, lambda: project_block_service.ResumeProjectBlockIfSafeEffects(
        _scope_reconciliation_effects_resume_project_block_if_safe=lambda *args, **kwargs: _scope_reconciliation_effects_resume_project_block_if_safe(*args, **kwargs),
    ),
)

_remote_checkpoint_input_identity = checkpoint_service._remote_checkpoint_input_identity

_remote_checkpoint_refusal = checkpoint_service._remote_checkpoint_refusal

_resume_remote_corrected_execplan = _bind_effects(
    checkpoint_service._resume_remote_corrected_execplan, lambda: checkpoint_service.ResumeRemoteCorrectedExecplanEffects(
        hydrate_execplan_project=lambda *args, **kwargs: hydrate_execplan_project(*args, **kwargs),
    ),
)

_remote_checkpoint_has_new_failure = _bind_effects(
    checkpoint_service._remote_checkpoint_has_new_failure, lambda: checkpoint_service.RemoteCheckpointHasNewFailureEffects(
        _deserialize_failure_history=lambda *args, **kwargs: _deserialize_failure_history(*args, **kwargs),
        validation_failure_fingerprints=lambda *args, **kwargs: validation_failure_fingerprints(*args, **kwargs),
    ),
)

_remote_checkpoint_review_revision = _bind_effects(
    checkpoint_service._remote_checkpoint_review_revision, lambda: checkpoint_service.RemoteCheckpointReviewRevisionEffects(
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_evaluate_remote_checkpoint_cause = _bind_effects(
    checkpoint_service._evaluate_remote_checkpoint_cause, lambda: checkpoint_service.EvaluateRemoteCheckpointCauseEffects(
        _checkpoint_recovery_effects_evaluate_remote_checkpoint_cause=lambda *args, **kwargs: _checkpoint_recovery_effects_evaluate_remote_checkpoint_cause(*args, **kwargs),
    ),
)

_reclassify_remote_specification_deadlock = _bind_effects(
    checkpoint_service._reclassify_remote_specification_deadlock, lambda: checkpoint_service.ReclassifyRemoteSpecificationDeadlockEffects(
        _reclassify_remote_checkpoint_block=lambda *args, **kwargs: _reclassify_remote_checkpoint_block(*args, **kwargs),
        parse_reviewer_result=lambda *args, **kwargs: parse_reviewer_result(*args, **kwargs),
    ),
)

_reclassify_remote_checkpoint_block = _bind_effects(
    checkpoint_service._reclassify_remote_checkpoint_block, lambda: checkpoint_service.ReclassifyRemoteCheckpointBlockEffects(
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _revision_review_issue_fingerprint=lambda *args, **kwargs: _revision_review_issue_fingerprint(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
    ),
)

_reconcile_published_blocked_checkpoint = _bind_effects(
    checkpoint_service._reconcile_published_blocked_checkpoint, lambda: checkpoint_service.ReconcilePublishedBlockedCheckpointEffects(
        _checkpoint_recovery_effects_reconcile_published_blocked_checkpoint=lambda *args, **kwargs: _checkpoint_recovery_effects_reconcile_published_blocked_checkpoint(*args, **kwargs),
    ),
)

_resume_legacy_review_count_block = _bind_effects(
    checkpoint_service._resume_legacy_review_count_block, lambda: checkpoint_service.ResumeLegacyReviewCountBlockEffects(
        _scope_reconciliation_effects_resume_legacy_review_count_block=lambda *args, **kwargs: _scope_reconciliation_effects_resume_legacy_review_count_block(*args, **kwargs),
    ),
)

resume_plan_blocks = _bind_effects(
    checkpoint_service.resume_plan_blocks, lambda: checkpoint_service.ResumePlanBlocksEffects(
        _scope_reconciliation_effects_resume_plan_blocks=lambda *args, **kwargs: _scope_reconciliation_effects_resume_plan_blocks(*args, **kwargs),
    ),
)

_block_closed_pull_request = _bind_effects(
    waiting_pr_service._block_closed_pull_request, lambda: waiting_pr_service.BlockClosedPullRequestEffects(
        _final_block_recovery_decision=lambda *args, **kwargs: _final_block_recovery_decision(*args, **kwargs),
    ),
)

_resume_reopened_pull_request = _bind_effects(
    waiting_pr_service._resume_reopened_pull_request, lambda: waiting_pr_service.ResumeReopenedPullRequestEffects(
        _clear_active_values=lambda *args, **kwargs: _clear_active_values(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
    ),
)

reconcile_existing_pull_request = _bind_effects(
    waiting_pr_service.reconcile_existing_pull_request, lambda: waiting_pr_service.ReconcileExistingPullRequestEffects(
        _pull_request_recovery_effects_reconcile_existing_pull_request=lambda *args, **kwargs: _pull_request_recovery_effects_reconcile_existing_pull_request(*args, **kwargs),
    ),
)

resume_pr_conflict_rebase = _bind_effects(
    waiting_pr_service.resume_pr_conflict_rebase, lambda: waiting_pr_service.ResumePrConflictRebaseEffects(
        _pull_request_recovery_effects_resume_pr_conflict_rebase=lambda *args, **kwargs: _pull_request_recovery_effects_resume_pr_conflict_rebase(*args, **kwargs),
    ),
)

_targeted_test_failure_matches = result_validation._targeted_test_failure_matches

_has_unresolved_targeted_test_failure = _bind_effects(
    result_validation._has_unresolved_targeted_test_failure, lambda: result_validation.HasUnresolvedTargetedTestFailureEffects(
        _targeted_test_failure_matches=lambda *args, **kwargs: _targeted_test_failure_matches(*args, **kwargs),
    ),
)

parse_worker_result = _bind_effects(
    result_validation.parse_worker_result, lambda: result_validation.ParseWorkerResultEffects(
        _has_unresolved_targeted_test_failure=lambda *args, **kwargs: _has_unresolved_targeted_test_failure(*args, **kwargs),
        reviewer_targeted_test_reason=lambda *args, **kwargs: reviewer_targeted_test_reason(*args, **kwargs),
    ),
)

reviewer_targeted_test_reason = result_validation.reviewer_targeted_test_reason

immutable_github_blob_url = _bind_effects(
    result_validation.immutable_github_blob_url, lambda: result_validation.ImmutableGithubBlobUrlEffects(
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
    ),
)

execplan_purpose = result_validation.execplan_purpose

published_execplan = _bind_effects(
    requirement_archive_service.published_execplan, lambda: requirement_archive_service.PublishedExecplanEffects(
        parse_execplan_text=lambda *args, **kwargs: execplan_catalog.parse_execplan_text(*args, **kwargs),
    ),
)

_requirement_archive_tree = _bind_effects(
    requirement_archive_service._requirement_archive_tree, lambda: requirement_archive_service.RequirementArchiveTreeEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
    ),
)

_stored_requirement_sources = requirement_archive_service._stored_requirement_sources

requirement_archive_sources = _bind_effects(
    requirement_archive_service.requirement_archive_sources, lambda: requirement_archive_service.RequirementArchiveSourcesEffects(
        _stored_requirement_sources=lambda *args, **kwargs: _stored_requirement_sources(*args, **kwargs),
    ),
)

_later_published_execplan = _bind_effects(
    requirement_archive_service._later_published_execplan, lambda: requirement_archive_service.LaterPublishedExecplanEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        published_execplan=lambda *args, **kwargs: published_execplan(*args, **kwargs),
    ),
)

_revision_execplan_catalog = _bind_effects(
    requirement_archive_service._revision_execplan_catalog, lambda: requirement_archive_service.RevisionExecplanCatalogEffects(
        revision_execplan_snapshot=lambda *args, **kwargs: revision_execplan_snapshot(*args, **kwargs),
    ),
)

requirement_archive_snapshot = _bind_effects(
    requirement_archive_service.requirement_archive_snapshot, lambda: requirement_archive_service.RequirementArchiveSnapshotEffects(
        _is_active_requirement=lambda *args, **kwargs: _is_active_requirement(*args, **kwargs),
        _later_published_execplan=lambda *args, **kwargs: _later_published_execplan(*args, **kwargs),
        _requirement_archive_tree=lambda *args, **kwargs: _requirement_archive_tree(*args, **kwargs),
        _revision_execplan_catalog=lambda *args, **kwargs: _revision_execplan_catalog(*args, **kwargs),
        _validate_dependency_graph=lambda *args, **kwargs: _validate_dependency_graph(*args, **kwargs),
    ),
)

requirement_archive_candidates = _bind_effects(
    requirement_archive_service.requirement_archive_candidates, lambda: requirement_archive_service.RequirementArchiveCandidatesEffects(
        requirement_archive_snapshot=lambda *args, **kwargs: requirement_archive_snapshot(*args, **kwargs),
        requirement_archive_sources=lambda *args, **kwargs: requirement_archive_sources(*args, **kwargs),
    ),
)

requirement_archive_touched_sources = _bind_effects(
    requirement_archive_service.requirement_archive_touched_sources, lambda: requirement_archive_service.RequirementArchiveTouchedSourcesEffects(
        _is_active_requirement=lambda *args, **kwargs: _is_active_requirement(*args, **kwargs),
        _requirement_archive_tree=lambda *args, **kwargs: _requirement_archive_tree(*args, **kwargs),
        requirement_archive_path=lambda *args, **kwargs: requirement_archive_path(*args, **kwargs),
    ),
)

_requirement_archive_blob = _bind_effects(
    requirement_archive_service._requirement_archive_blob, lambda: requirement_archive_service.RequirementArchiveBlobEffects(
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

requirement_archive_move_set = _bind_effects(
    requirement_archive_service.requirement_archive_move_set, lambda: requirement_archive_service.RequirementArchiveMoveSetEffects(
        _requirement_archive_blob=lambda *args, **kwargs: _requirement_archive_blob(*args, **kwargs),
        _requirement_archive_tree=lambda *args, **kwargs: _requirement_archive_tree(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

requirement_archive_reconciliation_error = requirement_archive_service.requirement_archive_reconciliation_error

build_pr_body = _bind_effects(
    pr_description.build_pr_body, lambda: pr_description.BuildPrBodyEffects(
        execplan_purpose=lambda *args, **kwargs: execplan_purpose(*args, **kwargs),
        immutable_github_blob_url=lambda *args, **kwargs: immutable_github_blob_url(*args, **kwargs),
    ),
)

_PlanningStatePorts = _bind_effects(
    state_ports._PlanningStatePorts, lambda: state_ports.PlanningStatePortsEffects(
        _advance_maintenance_cycle_after_merges=lambda *args, **kwargs: _advance_maintenance_cycle_after_merges(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        activate_ready_execplan=lambda *args, **kwargs: activate_ready_execplan(*args, **kwargs),
        catalog_schedule_decision=lambda *args, **kwargs: catalog_schedule_decision(*args, **kwargs),
        clear_active=lambda *args, **kwargs: clear_active(*args, **kwargs),
        clear_planning_validation_state=lambda *args, **kwargs: clear_planning_validation_state(*args, **kwargs),
        compact_execplan_catalog=lambda *args, **kwargs: compact_execplan_catalog(*args, **kwargs),
        general_planning_publication=lambda *args, **kwargs: general_planning_publication(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        prerequisite_validation_rows=lambda *args, **kwargs: prerequisite_validation_rows(*args, **kwargs),
        preserve_planning_diagnostics=lambda *args, **kwargs: preserve_planning_diagnostics(*args, **kwargs),
        preserve_remote_planning_diagnostics=lambda *args, **kwargs: preserve_remote_planning_diagnostics(*args, **kwargs),
        publish_general_planning_batch=lambda *args, **kwargs: publish_general_planning_batch(*args, **kwargs),
        publish_maintenance_planning_batch=lambda *args, **kwargs: publish_maintenance_planning_batch(*args, **kwargs),
        record_general_planning_publication_intent=lambda *args, **kwargs: record_general_planning_publication_intent(*args, **kwargs),
        record_maintenance_publication_intent=lambda *args, **kwargs: record_maintenance_publication_intent(*args, **kwargs),
        recover_general_planning_batch=lambda *args, **kwargs: recover_general_planning_batch(*args, **kwargs),
        recover_maintenance_planning_batch=lambda *args, **kwargs: recover_maintenance_planning_batch(*args, **kwargs),
        reconcile_execplan_catalog=lambda *args, **kwargs: reconcile_execplan_catalog(*args, **kwargs),
        register_execplans=lambda *args, **kwargs: register_execplans(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
    ),
)

_PlanningGitPorts = _bind_effects(
    git_ports._PlanningGitPorts, lambda: git_ports.PlanningGitPortsEffects(
        _capture_local_worktree_state=lambda *args, **kwargs: _capture_local_worktree_state(*args, **kwargs),
        _git_operation_entries=lambda *args, **kwargs: _git_operation_entries(*args, **kwargs),
        _ignored_worktree_paths=lambda *args, **kwargs: _ignored_worktree_paths(*args, **kwargs),
        _is_requirement_archive_only_change=lambda *args, **kwargs: _is_requirement_archive_only_change(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        _planning_input_fingerprint=lambda *args, **kwargs: _planning_input_fingerprint(*args, **kwargs),
        _nonplan_input_fingerprint=lambda *args, **kwargs: _nonplan_input_fingerprint(*args, **kwargs),
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        _specification_input_fingerprint=lambda *args, **kwargs: _specification_input_fingerprint(*args, **kwargs),
        plan_declarations=lambda *args, **kwargs: plan_declarations(*args, **kwargs),
        changed_files=lambda *args, **kwargs: changed_files(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        make_planning_worktree=lambda *args, **kwargs: make_planning_worktree(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        validate_planning_workspace_identity=lambda *args, **kwargs: validate_planning_workspace_identity(*args, **kwargs),
        validate_planning_worktree_ownership=lambda *args, **kwargs: validate_planning_worktree_ownership(*args, **kwargs),
    ),
)

_ExecPlanArchiveGitPort = _bind_effects(
    git_ports._ExecPlanArchiveGitPort, lambda: git_ports.ExecPlanArchiveGitPortEffects(
        _archive_git_effects=lambda *args, **kwargs: _archive_git_effects(*args, **kwargs),
    ),
)

dispatch_execplan_archive_housekeeping = _bind_effects(
    archive_service.dispatch_execplan_archive_housekeeping, lambda: archive_service.DispatchExecplanArchiveHousekeepingEffects(
        _ExecPlanArchiveGitPort=lambda *args, **kwargs: _ExecPlanArchiveGitPort(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        reconcile_execplan_catalog=lambda *args, **kwargs: reconcile_execplan_catalog(*args, **kwargs),
    ),
)

_execplan_archive_housekeeping_safe = _bind_effects(
    archive_service._execplan_archive_housekeeping_safe, lambda: archive_service.ExecplanArchiveHousekeepingSafeEffects(
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        catalog_schedule_decision=lambda *args, **kwargs: catalog_schedule_decision(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
    ),
)

_PlanningAgentPorts = _bind_effects(
    agent_ports._PlanningAgentPorts, lambda: agent_ports.PlanningAgentPortsEffects(
        _agent_profile=lambda *args, **kwargs: _agent_profile(*args, **kwargs),
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        _reconciled_manager_scope_metadata=lambda *args, **kwargs: _reconciled_manager_scope_metadata(*args, **kwargs),
        begin_agent_scope=lambda *args, **kwargs: begin_agent_scope(*args, **kwargs),
        _with_proven_legacy_ignore_config=lambda *args, **kwargs: _with_proven_legacy_ignore_config(*args, **kwargs),
        _normalize_legacy_ignored_snapshot=lambda *args, **kwargs: _normalize_legacy_ignored_snapshot(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        clear_agent_scope=lambda *args, **kwargs: clear_agent_scope(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        run_agent=lambda *args, **kwargs: run_agent(*args, **kwargs),
        run_agent_with_retries=lambda *args, **kwargs: run_agent_with_retries(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
    ),
)

_PlanningValidationPorts = _bind_effects(
    validation_ports._PlanningValidationPorts, lambda: validation_ports.PlanningValidationPortsEffects(
        _maintenance_change_context_text=lambda *args, **kwargs: _maintenance_change_context_text(*args, **kwargs),
        _maintenance_context_at_revision=lambda *args, **kwargs: _maintenance_context_at_revision(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        maintenance_planner_prompt=lambda *args, **kwargs: maintenance_planner_prompt(*args, **kwargs),
        mutable_ready_snapshot=lambda *args, **kwargs: mutable_ready_snapshot(*args, **kwargs),
        planner_prompt=lambda *args, **kwargs: planner_prompt(*args, **kwargs),
        prepare_maintenance_planning_context=lambda *args, **kwargs: prepare_maintenance_planning_context(*args, **kwargs),
        validate_maintenance_planner_result=lambda *args, **kwargs: validate_maintenance_planner_result(*args, **kwargs),
        validate_maintenance_publication_base=lambda *args, **kwargs: validate_maintenance_publication_base(*args, **kwargs),
        validate_planner_result=lambda *args, **kwargs: validate_planner_result(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
    ),
)

_PlanningEventPorts = _bind_effects(
    event_ports._PlanningEventPorts, lambda: event_ports.PlanningEventPortsEffects(
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
    ),
)

_PlanningFileReviewPorts = review_ports._PlanningFileReviewPorts

_PlanningReconciliationPorts = _bind_effects(
    review_ports._PlanningReconciliationPorts, lambda: review_ports.PlanningReconciliationPortsEffects(
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        parse_frontmatter=lambda *args, **kwargs: parse_frontmatter(*args, **kwargs),
    ),
)

_PlanningLifecyclePorts = _bind_effects(
    planning_ports._PlanningLifecyclePorts, lambda: planning_ports.PlanningLifecyclePortsEffects(
        _PlanningAgentPorts=lambda *args, **kwargs: _PlanningAgentPorts(*args, **kwargs),
        _PlanningEventPorts=lambda *args, **kwargs: _PlanningEventPorts(*args, **kwargs),
        _PlanningFileReviewPorts=lambda *args, **kwargs: _PlanningFileReviewPorts(*args, **kwargs),
        _PlanningGitPorts=lambda *args, **kwargs: _PlanningGitPorts(*args, **kwargs),
        _PlanningReconciliationPorts=lambda *args, **kwargs: _PlanningReconciliationPorts(*args, **kwargs),
        _PlanningStatePorts=lambda *args, **kwargs: _PlanningStatePorts(*args, **kwargs),
        _PlanningValidationPorts=lambda *args, **kwargs: _PlanningValidationPorts(*args, **kwargs),
    ),
)

_planning_outcome_step_result = project_outcomes._planning_outcome_step_result

reconcile_false_positive_runtime_block = _bind_effects(
    project_block_service.reconcile_false_positive_runtime_block, lambda: project_block_service.ReconcileFalsePositiveRuntimeBlockEffects(
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        validate_persisted_agent_scope=lambda *args, **kwargs: validate_persisted_agent_scope(*args, **kwargs),
        _agent_git_resources=lambda *args, **kwargs: _agent_git_resources(*args, **kwargs),
        _reconciled_manager_scope_metadata=lambda *args, **kwargs: _reconciled_manager_scope_metadata(*args, **kwargs),
    ),
)

observe_blocked_plan_remotes = _bind_effects(
    project_block_service.observe_blocked_plan_remotes, lambda: project_block_service.ObserveBlockedPlanRemotesEffects(
        _worktree_state_signature=lambda *args, **kwargs: _worktree_state_signature(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        github_repository=lambda *args, **kwargs: github_repository(*args, **kwargs),
    ),
)

_run_project_step = _bind_effects(
    project_service._run_project_step, lambda: project_service.RunProjectStepEffects(
        resume_manual_work=lambda *args, **kwargs: _resume_manual_work(*args, **kwargs),
        ExecutionPhaseCoordinator=lambda *args, **kwargs: ExecutionPhaseCoordinator(*args, **kwargs),
        ManagerGitCheckpoint=ManagerGitCheckpoint,
        _ExecutionLifecyclePorts=lambda *args, **kwargs: _ExecutionLifecyclePorts(*args, **kwargs),
        _PlanningLifecyclePorts=lambda *args, **kwargs: _PlanningLifecyclePorts(*args, **kwargs),
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _check_runtime_handoff=lambda *args, **kwargs: _check_runtime_handoff(*args, **kwargs),
        _clear_active_in_current_transaction=lambda *args, **kwargs: _clear_active_in_current_transaction(*args, **kwargs),
        _ensure_cleanup_pending_schema=lambda *args, **kwargs: _ensure_cleanup_pending_schema(*args, **kwargs),
        _ensure_execplans_schema=lambda *args, **kwargs: _ensure_execplans_schema(*args, **kwargs),
        _execplan_archive_housekeeping_safe=lambda *args, **kwargs: _execplan_archive_housekeeping_safe(*args, **kwargs),
        _execution_outcome_step_result=lambda *args, **kwargs: _execution_outcome_step_result(*args, **kwargs),
        _handoff_observed_self_merge=lambda *args, **kwargs: _handoff_observed_self_merge(*args, **kwargs),
        _journaled_manager_base_fetch=lambda *args, **kwargs: _journaled_manager_base_fetch(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _planning_outcome_step_result=lambda *args, **kwargs: _planning_outcome_step_result(*args, **kwargs),
        _project_blocked_recovery_effects=lambda *args, **kwargs: _project_blocked_recovery_effects(*args, **kwargs),
        _project_pr_merge_confirmed=lambda *args, **kwargs: _project_pr_merge_confirmed(*args, **kwargs),
        _publish_direct_rebase_checkpoint=lambda *args, **kwargs: _publish_direct_rebase_checkpoint(*args, **kwargs),
        _record_manager_base_fetch_intent=lambda *args, **kwargs: _record_manager_base_fetch_intent(*args, **kwargs),
        _record_project_recovery=lambda *args, **kwargs: _record_project_recovery(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        _self_runtime_project=lambda *args, **kwargs: _self_runtime_project(*args, **kwargs),
        _update_blocked_error=lambda *args, **kwargs: _update_blocked_error(*args, **kwargs),
        active_execplan=lambda *args, **kwargs: active_execplan(*args, **kwargs),
        backfill_plan_shas=lambda *args, **kwargs: backfill_plan_shas(*args, **kwargs),
        block_active_execplan_from_revision=lambda *args, **kwargs: block_active_execplan_from_revision(*args, **kwargs),
        block_persisted_agent_scope_violation=lambda *args, **kwargs: block_persisted_agent_scope_violation(*args, **kwargs),
        dispatch_execplan_archive_housekeeping=lambda *args, **kwargs: dispatch_execplan_archive_housekeeping(*args, **kwargs),
        ensure_safe_base_worktree=lambda *args, **kwargs: ensure_safe_base_worktree(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        hydrate_active_project=lambda *args, **kwargs: hydrate_active_project(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        log_base_worktree_sync=lambda *args, **kwargs: log_base_worktree_sync(*args, **kwargs),
        migrate_legacy_execplans=lambda *args, **kwargs: migrate_legacy_execplans(*args, **kwargs),
        observe_blocked_plan_remotes=lambda *args, **kwargs: observe_blocked_plan_remotes(*args, **kwargs),
        observe_waiting_pull_requests=lambda *args, **kwargs: observe_waiting_pull_requests(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        recheck_cleanup_pending=lambda *args, **kwargs: recheck_cleanup_pending(*args, **kwargs),
        reconcile_execplan_catalog=lambda *args, **kwargs: reconcile_execplan_catalog(*args, **kwargs),
        reconcile_existing_pull_request=lambda *args, **kwargs: reconcile_existing_pull_request(*args, **kwargs),
        reconcile_false_positive_runtime_block=lambda *args, **kwargs: reconcile_false_positive_runtime_block(*args, **kwargs),
        refresh_persisted_agent_scope_after_manager_git=lambda *args, **kwargs: refresh_persisted_agent_scope_after_manager_git(*args, **kwargs),
        remote_base=lambda *args, **kwargs: remote_base(*args, **kwargs),
        resume_interrupted_blocked_agent_recovery=lambda *args, **kwargs: resume_interrupted_blocked_agent_recovery(*args, **kwargs),
        resume_plan_blocks=lambda *args, **kwargs: resume_plan_blocks(*args, **kwargs),
        resume_project_block_if_safe=lambda *args, **kwargs: resume_project_block_if_safe(*args, **kwargs),
        synchronize_waiting_pull_requests=lambda *args, **kwargs: synchronize_waiting_pull_requests(*args, **kwargs),
        update_project=lambda *args, **kwargs: update_project(*args, **kwargs),
        validate_persisted_agent_scope=lambda *args, **kwargs: validate_persisted_agent_scope(*args, **kwargs),
        validate_repository=lambda *args, **kwargs: validate_repository(*args, **kwargs),
    ),
)

_checkpoint_dispatch_ready = project_outcomes._checkpoint_dispatch_ready

_record_project_recovery = _bind_effects(
    project_outcomes._record_project_recovery, lambda: project_outcomes.RecordProjectRecoveryEffects(
        project_execplan=lambda *args, **kwargs: project_execplan(*args, **kwargs),
    ),
)

_blocked_recovery_subjects = project_outcomes._blocked_recovery_subjects

run_project_result = _bind_effects(
    project_runner.run_project_result, lambda: project_runner.RunProjectResultEffects(
        _blocked_recovery_subjects=lambda *args, **kwargs: _blocked_recovery_subjects(*args, **kwargs),
        _run_project_result=lambda *args, **kwargs: _run_project_result(*args, **kwargs),
    ),
)

_stable_work_reason = _bind_effects(
    project_outcomes._stable_work_reason, lambda: project_outcomes.StableWorkReasonEffects(
        _nonplan_input_fingerprint=lambda *args, **kwargs: _nonplan_input_fingerprint(*args, **kwargs),
        _git_tree_entries=lambda *args, **kwargs: _git_tree_entries(*args, **kwargs),
        _is_active_requirement=lambda *args, **kwargs: _is_active_requirement(*args, **kwargs),
        _is_requirement_archive_only_change=lambda *args, **kwargs: _is_requirement_archive_only_change(*args, **kwargs),
        _planning_input_fingerprint=lambda *args, **kwargs: _planning_input_fingerprint(*args, **kwargs),
        _specification_input_fingerprint=lambda *args, **kwargs: _specification_input_fingerprint(*args, **kwargs),
        plan_declarations=lambda *args, **kwargs: plan_declarations(*args, **kwargs),
        catalog_schedule_decision=lambda *args, **kwargs: catalog_schedule_decision(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
    ),
)

_run_project_result = _bind_effects(
    project_runner._run_project_result, lambda: project_runner.InternalRunProjectResultEffects(
        _checkpoint_dispatch_ready=lambda *args, **kwargs: _checkpoint_dispatch_ready(*args, **kwargs),
        _record_project_recovery=lambda *args, **kwargs: _record_project_recovery(*args, **kwargs),
        _run_project_step=lambda *args, **kwargs: _run_project_step(*args, **kwargs),
        _stable_work_reason=lambda *args, **kwargs: _stable_work_reason(*args, **kwargs),
        catalog_schedule_decision=lambda *args, **kwargs: catalog_schedule_decision(*args, **kwargs),
        dispatch_blocked_checkpoints=lambda *args, **kwargs: dispatch_blocked_checkpoints(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
    ),
)

run_project = _bind_effects(
    project_runner.run_project, lambda: project_runner.RunProjectEffects(
        run_project_result=lambda *args, **kwargs: run_project_result(*args, **kwargs),
    ),
)

_run_registered_project = _bind_effects(
    scheduler._run_registered_project, lambda: scheduler.RunRegisteredProjectEffects(
        _check_runtime_handoff=lambda *args, **kwargs: _check_runtime_handoff(*args, **kwargs),
        connect_database=lambda *args, **kwargs: connect_database(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        run_project_result=lambda *args, **kwargs: run_project_result(*args, **kwargs),
    ),
)

SelfMergeRuntimeHandoff = handoff_gate.SelfMergeRuntimeHandoff

RuntimeHandoffGate = handoff_gate.RuntimeHandoffGate

_check_runtime_handoff = handoff_gate._check_runtime_handoff

_handoff_popen = handoff_gate._handoff_popen

_handoff_run = handoff_gate._handoff_run

_registered_self_runtime_project = _bind_effects(
    self_runtime._registered_self_runtime_project, lambda: self_runtime.RegisteredSelfRuntimeProjectEffects(
        ROOT=ROOT,
        _canonical_path=lambda *args, **kwargs: _canonical_path(*args, **kwargs),
        _git_common_directory=lambda *args, **kwargs: _git_common_directory(*args, **kwargs),
        bootstrap_base_branch=lambda *args, **kwargs: bootstrap_base_branch(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        repository_origin=lambda *args, **kwargs: repository_origin(*args, **kwargs),
    ),
)

_self_runtime_project = _bind_effects(
    self_runtime._self_runtime_project, lambda: self_runtime.SelfRuntimeProjectEffects(
        _registered_self_runtime_project=lambda *args, **kwargs: _registered_self_runtime_project(*args, **kwargs),
    ),
)

_finish_self_merge_handoff = _bind_effects(
    self_runtime._finish_self_merge_handoff, lambda: self_runtime.FinishSelfMergeHandoffEffects(
        _registered_self_runtime_project=lambda *args, **kwargs: _registered_self_runtime_project(*args, **kwargs),
        ensure_safe_base_worktree=lambda *args, **kwargs: ensure_safe_base_worktree(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        log_base_worktree_sync=lambda *args, **kwargs: log_base_worktree_sync(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
    ),
)

_preflight_self_merge_handoff = _bind_effects(
    self_runtime._preflight_self_merge_handoff, lambda: self_runtime.PreflightSelfMergeHandoffEffects(
        _finish_self_merge_handoff=lambda *args, **kwargs: _finish_self_merge_handoff(*args, **kwargs),
        _registered_self_runtime_project=lambda *args, **kwargs: _registered_self_runtime_project(*args, **kwargs),
        observe_waiting_pull_requests=lambda *args, **kwargs: observe_waiting_pull_requests(*args, **kwargs),
    ),
)

_handoff_observed_self_merge = _bind_effects(
    self_runtime._handoff_observed_self_merge, lambda: self_runtime.HandoffObservedSelfMergeEffects(
        _finish_self_merge_handoff=lambda *args, **kwargs: _finish_self_merge_handoff(*args, **kwargs),
        _self_runtime_project=lambda *args, **kwargs: _self_runtime_project(*args, **kwargs),
    ),
)

schedule_registered_projects = _bind_effects(
    scheduler.schedule_registered_projects, lambda: scheduler.ScheduleRegisteredProjectsEffects(
        _preflight_self_merge_handoff=lambda *args, **kwargs: _preflight_self_merge_handoff(*args, **kwargs),
        _run_registered_project=lambda *args, **kwargs: _run_registered_project(*args, **kwargs),
    ),
)

run_registered_projects = _bind_effects(
    scheduler.run_registered_projects, lambda: scheduler.RunRegisteredProjectsEffects(
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        schedule_registered_projects=lambda *args, **kwargs: schedule_registered_projects(*args, **kwargs),
    ),
)

configure_registry = _bind_effects(
    commands.configure_registry, lambda: commands.ConfigureRegistryEffects(
        add_registered_project=lambda *args, **kwargs: add_registered_project(*args, **kwargs),
        connect_database=lambda *args, **kwargs: connect_database(*args, **kwargs),
        load_managed_config=lambda *args, **kwargs: load_managed_config(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
    ),
)

_document_drafts_command = _bind_effects(
    commands._document_drafts_command, lambda: commands.DocumentDraftsCommandEffects(
        ROOT=ROOT,
        _coerce_agent_result=lambda *args, **kwargs: _coerce_agent_result(*args, **kwargs),
        _recorded_base_fetch=lambda *args, **kwargs: _recorded_base_fetch(*args, **kwargs),
        connect_database=lambda *args, **kwargs: connect_database(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        run_agent=lambda *args, **kwargs: run_agent(*args, **kwargs),
    ),
)

_run_manual_project = _bind_effects(
    manual_work.run_manual_project, lambda: manual_work.ManualProjectDriverEffects(
        load_managed_config=lambda *a, **kw: load_managed_config(*a, **kw),
        run_project_result=lambda *a, **kw: run_project_result(*a, **kw),
    ),
)

_manual_work_effects = lambda: manual_work.ManualWorkEffects(
        connect_database=lambda *a, **kw: connect_database(*a, **kw),
        validate_repository=lambda *a, **kw: validate_repository(*a, **kw),
        fetch_base=lambda *a, **kw: _recorded_base_fetch(*a, **kw),
        git=lambda *a, **kw: git(*a, **kw),
        git_bytes=lambda *a, **kw: git_bytes(*a, **kw),
        make_worktree=lambda *a, **kw: make_worktree(*a, **kw),
        validate_workspace=lambda *a, **kw: validate_managed_worktree(*a, **kw),
        capture_snapshot=lambda *a, **kw: capture_git_snapshot(*a, **kw),
        pr_for_branch=lambda *a, **kw: pr_for_branch(*a, **kw),
        generate_plans=lambda *a, **kw: manual_plan_generation.generate(
            manual_plan_generation.ManualGenerationEffects(
                git=lambda *ga, **gkw: git(*ga, **gkw),
                run_agent=lambda *aa, **akw: run_agent(*aa, **akw),
                prepare_worktree=lambda *pa, **pkw: prepare_runtime_worktree(*pa, **pkw),
                remove_worktree=lambda *ra, **rkw: remove_runtime_worktree(*ra, **rkw)), *a, **kw),
        run_project=lambda *a, **kw: _run_manual_project(*a, **kw),
        operation_print=lambda *a, **kw: operation_print(*a, **kw),
    )


_manual_command = _bind_effects(manual_work.manual_command, lambda: _manual_work_effects())

_resume_manual_work = _bind_effects(manual_work.resume_manual_work, lambda: _manual_work_effects())


execute_runtime_command = _bind_effects(
    commands.execute_runtime_command, lambda: commands.ExecuteRuntimeCommandEffects(
        _document_drafts_command=lambda *args, **kwargs: _document_drafts_command(*args, **kwargs),
        configure_registry=lambda *args, **kwargs: configure_registry(*args, **kwargs),
        connect_database=lambda *args, **kwargs: connect_database(*args, **kwargs),
        load_managed_config=lambda *args, **kwargs: load_managed_config(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        run_registered_projects=lambda *args, **kwargs: run_registered_projects(*args, **kwargs),
        manual_command=lambda *args, **kwargs: _manual_command(*args, **kwargs),
        run_project_result=lambda *args, **kwargs: run_project_result(*args, **kwargs),
    ),
)

_owned_checkpoint_remote_oid = _bind_effects(
    checkpoint_service._owned_checkpoint_remote_oid, lambda: checkpoint_service.OwnedCheckpointRemoteOidEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_guidance_checkpoint_commit_evidence = _bind_effects(
    checkpoint_service._guidance_checkpoint_commit_evidence, lambda: checkpoint_service.GuidanceCheckpointCommitEvidenceEffects(
        _nul_git_paths=lambda *args, **kwargs: _nul_git_paths(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
    ),
)

_BlockedCheckpointAdapter = _bind_effects(
    checkpoint_service._BlockedCheckpointAdapter, lambda: checkpoint_service.BlockedCheckpointAdapterEffects(
        _checkpoint_publication_effects=lambda *args, **kwargs: _checkpoint_publication_effects(*args, **kwargs),
        _checkpoint_safety_effects=lambda *args, **kwargs: _checkpoint_safety_effects(*args, **kwargs),
    ),
)

push_diagnostic_checkpoint = _bind_effects(
    checkpoint_service.push_diagnostic_checkpoint, lambda: checkpoint_service.PushDiagnosticCheckpointEffects(
        _diagnostic_publication_effects=lambda *args, **kwargs: _diagnostic_publication_effects(*args, **kwargs),
    ),
)

dispatch_blocked_checkpoints = _bind_effects(
    checkpoint_service.dispatch_blocked_checkpoints, lambda: checkpoint_service.DispatchBlockedCheckpointsEffects(
        _BlockedCheckpointAdapter=lambda *args, **kwargs: _BlockedCheckpointAdapter(*args, **kwargs),
        preserve_blocked_diagnostics=lambda *args, **kwargs: preserve_blocked_diagnostics(*args, **kwargs),
    ),
)


_diagnostic_preservation_effects = lambda: diagnostic_preservation.PreservationEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        git_bytes=lambda *args, **kwargs: git_bytes(*args, **kwargs),
        push_diagnostic_checkpoint=lambda *args, **kwargs: push_diagnostic_checkpoint(*args, **kwargs))


preserve_blocked_diagnostics = _bind_effects(
    checkpoint_service.preserve_blocked_diagnostics,
    lambda: checkpoint_service.PreserveBlockedDiagnosticsEffects(
        preservation_effects=lambda: _diagnostic_preservation_effects(),
        adapter_factory=lambda *args, **kwargs: _BlockedCheckpointAdapter(*args, **kwargs)),
)


preserve_remote_planning_diagnostics = _bind_effects(
    diagnostics.preserve_remote_planning_diagnostics,
    lambda: diagnostics.RemotePlanningDiagnosticsEffects(
        preservation_effects=lambda: _diagnostic_preservation_effects(),
        adapter_factory=lambda *args, **kwargs: _BlockedCheckpointAdapter(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        validate_planning_workspace_identity=lambda *args, **kwargs: validate_planning_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs)),
)

_execution_outcome_step_result = checkpoint_service._execution_outcome_step_result

_typed_execution_git = _bind_effects(
    execution_ports._typed_execution_git, lambda: execution_ports.TypedExecutionGitEffects(
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_git_lock_failure=lambda *args, **kwargs: is_git_lock_failure(*args, **kwargs),
    ),
)

_typed_execution_command = _bind_effects(
    execution_ports._typed_execution_command, lambda: execution_ports.TypedExecutionCommandEffects(
        command=lambda *args, **kwargs: command(*args, **kwargs),
    ),
)

_ExecutionLifecyclePorts = _bind_effects(
    execution_ports._ExecutionLifecyclePorts, lambda: execution_ports.ExecutionLifecyclePortsEffects(
        _agent_profile=lambda *args, **kwargs: _agent_profile(*args, **kwargs),
        _current_rebase_service=lambda *args, **kwargs: _current_rebase_service(*args, **kwargs),
        _git_operation_entries=lambda *args, **kwargs: _git_operation_entries(*args, **kwargs),
        _path_signature=lambda *args, **kwargs: _path_signature(*args, **kwargs),
        _plan_id_from_path=lambda *args, **kwargs: _plan_id_from_path(*args, **kwargs),
        _published_plan_allows_common_plans=lambda *args, **kwargs: _published_plan_allows_common_plans(*args, **kwargs),
        _rebase_in_progress_for_branch=lambda *args, **kwargs: _rebase_in_progress_for_branch(*args, **kwargs),
        _revision_changed_paths=lambda *args, **kwargs: _revision_changed_paths(*args, **kwargs),
        _revision_execplan_body_hash=lambda *args, **kwargs: _revision_execplan_body_hash(*args, **kwargs),
        _revision_execplan_input_fingerprint=lambda *args, **kwargs: _revision_execplan_input_fingerprint(*args, **kwargs),
        _revision_execplan_source_fingerprint=lambda *args, **kwargs: _revision_execplan_source_fingerprint(*args, **kwargs),
        _run_agent_with_connection=lambda *args, **kwargs: _run_agent_with_connection(*args, **kwargs),
        _typed_execution_command=lambda *args, **kwargs: _typed_execution_command(*args, **kwargs),
        _typed_execution_git=lambda *args, **kwargs: _typed_execution_git(*args, **kwargs),
        _worktree_execplan_input_fingerprint=lambda *args, **kwargs: _worktree_execplan_input_fingerprint(*args, **kwargs),
        begin_agent_scope=lambda *args, **kwargs: begin_agent_scope(*args, **kwargs),
        block_active_execplan=lambda *args, **kwargs: block_active_execplan(*args, **kwargs),
        block_active_execplan_from_revision=lambda *args, **kwargs: block_active_execplan_from_revision(*args, **kwargs),
        block_verify_timeout_limit=lambda *args, **kwargs: block_verify_timeout_limit(*args, **kwargs),
        branch_has_changes=lambda *args, **kwargs: branch_has_changes(*args, **kwargs),
        build_pr_body=lambda *args, **kwargs: build_pr_body(*args, **kwargs),
        capture_git_snapshot=lambda *args, **kwargs: capture_git_snapshot(*args, **kwargs),
        clear_agent_scope=lambda *args, **kwargs: clear_agent_scope(*args, **kwargs),
        clear_validation_failure_history=lambda *args, **kwargs: clear_validation_failure_history(*args, **kwargs),
        ensure_worker_level_state=lambda *args, **kwargs: ensure_worker_level_state(*args, **kwargs),
        git=lambda *args, **kwargs: git(*args, **kwargs),
        is_loop_manager_repository=lambda *args, **kwargs: is_loop_manager_repository(*args, **kwargs),
        make_worktree=lambda *args, **kwargs: make_worktree(*args, **kwargs),
        mark_execplan_workspace_owned=lambda *args, **kwargs: mark_execplan_workspace_owned(*args, **kwargs),
        operation_log=lambda *args, **kwargs: operation_log(*args, **kwargs),
        operation_print=lambda *args, **kwargs: operation_print(*args, **kwargs),
        parse_reviewer_result=lambda *args, **kwargs: parse_reviewer_result(*args, **kwargs),
        parse_worker_result=lambda *args, **kwargs: parse_worker_result(*args, **kwargs),
        pr_for_branch=lambda *args, **kwargs: pr_for_branch(*args, **kwargs),
        published_execplan=lambda *args, **kwargs: published_execplan(*args, **kwargs),
        push_managed_publication_branch=lambda *args, **kwargs: push_managed_publication_branch(*args, **kwargs),
        push_owned_pr_conflict_recovery=lambda *args, **kwargs: push_owned_pr_conflict_recovery(*args, **kwargs),
        reconcile_execution_execplan_catalog=lambda *args, **kwargs: reconcile_execution_execplan_catalog(*args, **kwargs),
        reconcile_existing_pull_request=lambda *args, **kwargs: reconcile_existing_pull_request(*args, **kwargs),
        record_remote_publication_branch=lambda *args, **kwargs: record_remote_publication_branch(*args, **kwargs),
        remote_branch_oid=lambda *args, **kwargs: remote_branch_oid(*args, **kwargs),
        requirement_archive_candidates=lambda *args, **kwargs: (),
        requirement_archive_move_set=lambda *args, **kwargs: requirement_archive_move_set(*args, **kwargs),
        requirement_archive_reconciliation_error=lambda *args, **kwargs: requirement_archive_reconciliation_error(*args, **kwargs),
        requirement_archive_snapshot=lambda *args, **kwargs: requirement_archive_snapshot(*args, **kwargs),
        requirement_archive_sources=lambda *args, **kwargs: requirement_archive_sources(*args, **kwargs),
        requirement_archive_touched_sources=lambda *args, **kwargs: requirement_archive_touched_sources(*args, **kwargs),
        reserve_execplan_workspace=lambda *args, **kwargs: reserve_execplan_workspace(*args, **kwargs),
        resume_pr_conflict_rebase=lambda *args, **kwargs: resume_pr_conflict_rebase(*args, **kwargs),
        reviewer_prompt=lambda *args, **kwargs: reviewer_prompt(*args, **kwargs),
        reviewer_targeted_test_reason=lambda *args, **kwargs: reviewer_targeted_test_reason(*args, **kwargs),
        run_agent_with_retries=lambda *args, **kwargs: run_agent_with_retries(*args, **kwargs),
        transition_validation_failure=lambda *args, **kwargs: transition_validation_failure(*args, **kwargs),
        unmet_execplan_dependencies=lambda *args, **kwargs: unmet_execplan_dependencies(*args, **kwargs),
        validate_active_execplan_metadata=lambda *args, **kwargs: validate_active_execplan_metadata(*args, **kwargs),
        validate_agent_scope=lambda *args, **kwargs: validate_agent_scope(*args, **kwargs),
        validate_execplan_workspace_identity=lambda *args, **kwargs: validate_execplan_workspace_identity(*args, **kwargs),
        validate_managed_worktree=lambda *args, **kwargs: validate_managed_worktree(*args, **kwargs),
        validate_worker_workspace_scope=lambda *args, **kwargs: validate_worker_workspace_scope(*args, **kwargs),
        validation_failure_fingerprints=lambda *args, **kwargs: validation_failure_fingerprints(*args, **kwargs),
        verify=lambda *args, **kwargs: verify(*args, **kwargs),
        worker_prompt=lambda *args, **kwargs: worker_prompt(*args, **kwargs),
    ),
)


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
        help=argparse.SUPPRESS,
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

    manual = subcommands.add_parser('manual', help='수동 G 구현 시작·완료와 원격 R·코드 제출')
    manual_commands = manual.add_subparsers(dest='manual_command', required=True)
    for name in ('start', 'complete', 'submit'):
        command = manual_commands.add_parser(name)
        command.add_argument('repository', help='등록된 Git 저장소 루트')
        if name == 'submit':
            command.add_argument('--branch', required=True, help='읽기 입력 원격 branch')
            command.add_argument('--requirement', action='append', required=True, help='제출 commit의 R 문서 경로')
        else:
            command.add_argument('plan_id', help='정확한 G ID')
        if name != 'start':
            command.add_argument('--skip-verify', action='store_true')
            command.add_argument('--skip-review', action='store_true')
            command.add_argument('--expected-head')
            command.add_argument('--skip-reason')

    maintenance = subcommands.add_parser("maintenance-review", help="등록 프로젝트의 전체 정밀 점검을 다음 안전한 경계에 요청")
    maintenance.add_argument("repository", help="등록된 Git 저장소 루트")

    recover = subcommands.add_parser(
        "recover", help="등록 프로젝트의 로컬 환경 원인을 제한해 재검사",
    )
    recover.add_argument("repository", help="재검사할 등록 저장소 루트")
    recover.add_argument("--plan", help="이 프로젝트 안의 특정 ExecPlan만 재검사")

    run = subcommands.add_parser("run", help="등록 프로젝트의 다음 작업을 처리")
    run.add_argument(
        "--skip-if-running",
        action="store_true",
        help="다른 Loop Manager가 실행 중이면 스케줄러용으로 조용히 성공 종료",
    )
    return parser


def main() -> int:
    raw_arguments = sys.argv[1:]
    args = build_parser().parse_args(raw_arguments)
    if args.command == 'manual' and args.manual_command != 'start':
        try:
            manual_work.validate_options(args)
        except ValueError as error:
            build_parser().error(str(error))
    state_dir = state_directory(args.state_dir)
    config_path = source_config_path(ROOT, args.config)
    try:
        if args.runtime_child:
            if not args.runtime_source or not args.runtime_sha:
                raise RuntimeError("runtime 자식 소유권 정보가 없습니다.")
            source = Path(args.runtime_source).expanduser().resolve()
            concurrent = args.command in {"manual", "recover"}
            runtime_options = {"concurrent": True} if concurrent else {}
            child_options = {"command_id": args.runtime_mutation_id} if concurrent else {}
            validate_runtime_child(source, state_dir, str(args.runtime_sha), **child_options)
            runtime_mutation = validate_runtime_mutation(
                source, state_dir, str(args.runtime_sha), args.runtime_mutation_id, **runtime_options,
            )
            # Mutation validation binds the selected revision and verifies any
            # later live base advance; equality here would reject that proof.
            return execute_runtime_command(
                args, state_dir, config_path, runtime_mutation,
            )

        source = ROOT
        mutation_id = secrets.token_hex(16)
        concurrent = args.command in {"manual", "recover"}
        lock_options = {"name": f"manual-{mutation_id}.lock"} if concurrent else {}
        runtime_options = {"concurrent": True} if concurrent else {}
        with process_lock(state_dir, identity=f"bootstrap:{mutation_id}", **lock_options) as lock_handle:
            runtime = prepare_bootstrap_runtime(source, state_dir, mutation_id, **runtime_options)
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


if __name__ == "__main__":
    raise SystemExit(main())
