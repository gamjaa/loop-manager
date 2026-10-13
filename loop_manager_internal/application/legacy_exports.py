"""Existing public types, module aliases and pure helpers; no service wiring."""
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
from loop_manager_internal.execution import manual_candidate
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

VALUES = {
    'ACTIVE_EXECPLAN_STATUSES': project_state_adapter.ACTIVE_EXECPLAN_STATUSES,
    'AGENT_CLI_TIMEOUT_SECONDS': AGENT_CLI_TIMEOUT_SECONDS,
    'AGENT_PROVIDERS': AGENT_PROVIDERS,
    'AgentChange': AgentChange,
    'AgentProfile': AgentProfile,
    'AgentProvider': AgentProvider,
    'AgentRequest': AgentRequest,
    'AgentResult': AgentResult,
    'AgentResultKind': AgentResultKind,
    'AgentScopeError': AgentScopeError,
    'AttemptSequence': AttemptSequence,
    'BaseWorktreeSnapshot': BaseWorktreeSnapshot,
    'BaseWorktreeSyncResult': BaseWorktreeSyncResult,
    'BlockReasonCode': BlockReasonCode,
    'BlockScope': BlockScope,
    'CLAUDE_EFFORTS': CLAUDE_EFFORTS,
    'CLEANUP_PENDING_SCHEMA': CLEANUP_PENDING_SCHEMA,
    'Callable': Callable,
    'CatalogScheduleDecision': contracts.CatalogScheduleDecision,
    'ClaudeAgentAdapter': ClaudeAgentAdapter,
    'CleanupAssessment': contracts.CleanupAssessment,
    'CodexAgentAdapter': CodexAgentAdapter,
    'CodexResult': contracts.CodexResult,
    'ContextVar': ContextVar,
    'EXECPLAN_CANCELLATIONS_SCHEMA': EXECPLAN_CANCELLATIONS_SCHEMA,
    'EXEC_PLANS_ACTIVE_INDEX': EXEC_PLANS_ACTIVE_INDEX,
    'EXEC_PLANS_SCHEMA': EXEC_PLANS_SCHEMA,
    'Enum': Enum,
    'ExecPlanMetadataError': contracts.ExecPlanMetadataError,
    'ExecutionContext': ExecutionContext,
    'ExecutionOutcome': ExecutionOutcome,
    'ExecutionOutcomeKind': ExecutionOutcomeKind,
    'ExecutionPhase': ExecutionPhase,
    'ExecutionPhaseCoordinator': ExecutionPhaseCoordinator,
    'ExecutionPlanMetadata': ExecutionPlanMetadata,
    'FIELDS': project_state_adapter.FIELDS,
    'GENERAL_PLANNING_PUBLICATIONS_SCHEMA': GENERAL_PLANNING_PUBLICATIONS_SCHEMA,
    'GUIDANCE_APPROVALS_SCHEMA': GUIDANCE_APPROVALS_SCHEMA,
    'GUIDANCE_BLOCK_REASON': GUIDANCE_BLOCK_REASON,
    'GeneralPlanningPublication': GeneralPlanningPublication,
    'GitLockError': GitLockError,
    'GitSnapshot': GitSnapshot,
    'InvocationContext': InvocationContext,
    'KNOWN_EXECPLAN_STATUSES': project_state_adapter.KNOWN_EXECPLAN_STATUSES,
    'LatestBaseCheckpoint': LatestBaseCheckpoint,
    'LatestBasePort': LatestBasePort,
    'MANAGER_BASE_FETCH_SCHEMA': MANAGER_BASE_FETCH_SCHEMA,
    'MAX_PLANNER_VALIDATION_RETRIES': MAX_PLANNER_VALIDATION_RETRIES,
    'MAX_REVIEWER_PROTOCOL_ATTEMPTS': contracts.MAX_REVIEWER_PROTOCOL_ATTEMPTS,
    'MAX_TRANSIENT_AGENT_RETRIES': MAX_TRANSIENT_AGENT_RETRIES,
    'MAX_VALIDATION_FINGERPRINT_REPEATS': contracts.MAX_VALIDATION_FINGERPRINT_REPEATS,
    'MERGED_WORKTREE_CLEANUP_SCHEMA': MERGED_WORKTREE_CLEANUP_SCHEMA,
    'MaintenanceOrchestrationError': MaintenanceOrchestrationError,
    'MaintenanceOutcome': MaintenanceOutcome,
    'MaintenancePlanMode': MaintenancePlanMode,
    'MaintenancePlannerDecision': MaintenancePlannerDecision,
    'MaintenancePlanningContext': MaintenancePlanningContext,
    'MaintenancePlanningError': MaintenancePlanningError,
    'MaintenancePlanningRestart': MaintenancePlanningRestart,
    'ManagedConfig': ManagedConfig,
    'ManagedConfigError': ManagedConfigError,
    'ManagedWorktreeConflictError': ManagedWorktreeConflictError,
    'ManagerGitCheckpoint': ManagerGitCheckpoint,
    'Mapping': Mapping,
    'MutableReadySnapshot': MutableReadySnapshot,
    'PLAN_SHA_BACKFILL_ERROR': contracts.PLAN_SHA_BACKFILL_ERROR,
    'PRIORITY_ORDER': contracts.PRIORITY_ORDER,
    'PROJECT_STEP_PLANNING_PENDING': PROJECT_STEP_PLANNING_PENDING,
    'PROJECT_STEP_RETRY': PROJECT_STEP_RETRY,
    'PROJECT_STEP_TRANSIENT_PAUSE': contracts.PROJECT_STEP_TRANSIENT_PAUSE,
    'PROJECT_STEP_USAGE_PAUSE': contracts.PROJECT_STEP_USAGE_PAUSE,
    'PR_CLOSED_BLOCK_REASON': PR_CLOSED_BLOCK_REASON,
    'PR_CONFLICT_PREFLIGHT_BLOCK_REASON': PR_CONFLICT_PREFLIGHT_BLOCK_REASON,
    'PR_CONFLICT_PREFLIGHT_ERROR_PREFIX': PR_CONFLICT_PREFLIGHT_ERROR_PREFIX,
    'Path': Path,
    'PlanDeletion': PlanDeletion,
    'PlanRevision': PlanRevision,
    'PlannerValidationError': PlannerValidationError,
    'PlanningAgentResult': PlanningAgentResult,
    'PlanningAgentResultKind': PlanningAgentResultKind,
    'PlanningContext': PlanningContext,
    'PlanningLifecycleCoordinator': PlanningLifecycleCoordinator,
    'PlanningOutcome': PlanningOutcome,
    'PlanningOutcomeKind': PlanningOutcomeKind,
    'PlanningResultInvalid': PlanningResultInvalid,
    'PlanningScopeResult': PlanningScopeResult,
    'PlanningWorkspaceOwnershipError': PlanningWorkspaceOwnershipError,
    'ProcessLockBusyError': ProcessLockBusyError,
    'ProjectRegistrationInspection': contracts.ProjectRegistrationInspection,
    'ProjectRunKind': contracts.ProjectRunKind,
    'ProjectRunResult': contracts.ProjectRunResult,
    'Protocol': Protocol,
    'PublicationSafetyError': PublicationSafetyError,
    'PullRequestSyncResult': contracts.PullRequestSyncResult,
    'REBASE_CONFLICT_PREFIX': REBASE_CONFLICT_PREFIX,
    'REBASE_IN_PROGRESS_ERROR': REBASE_IN_PROGRESS_ERROR,
    'ROOT': Path(__file__).resolve().parent,
    'ReadyPlanSnapshot': ReadyPlanSnapshot,
    'RemoteBranchMissingError': contracts.RemoteBranchMissingError,
    'RequirementArchiveCollisionError': contracts.RequirementArchiveCollisionError,
    'RequirementArchiveError': contracts.RequirementArchiveError,
    'ResolvedProfile': ResolvedProfile,
    'ReviewerProtocolError': contracts.ReviewerProtocolError,
    'RuntimeHandoffGate': handoff_gate.RuntimeHandoffGate,
    'RuntimeMutation': RuntimeMutation,
    'RuntimeWorktree': RuntimeWorktree,
    'SCHEMA': SCHEMA,
    'ScheduledProjectResult': contracts.ScheduledProjectResult,
    'SelfMergeRuntimeHandoff': handoff_gate.SelfMergeRuntimeHandoff,
    'Sequence': Sequence,
    'SimpleNamespace': SimpleNamespace,
    'StateConnection': StateConnection,
    'TRANSIENT_AGENT_ERROR_PREFIX': contracts.TRANSIENT_AGENT_ERROR_PREFIX,
    'ThreadPoolExecutor': ThreadPoolExecutor,
    'TrackedAgentCall': TrackedAgentCall,
    'USAGE_LIMIT_ERROR_PREFIX': contracts.USAGE_LIMIT_ERROR_PREFIX,
    'Usage': Usage,
    'ValidatedPlanningResult': contracts.ValidatedPlanningResult,
    'ValidationFailureDecision': contracts.ValidationFailureDecision,
    'VerifyOutput': contracts.VerifyOutput,
    'VerifyTerminationUncertain': VerifyTerminationUncertain,
    'WAITING_EXECPLAN_STATUSES': project_state_adapter.WAITING_EXECPLAN_STATUSES,
    'WORKSPACE_OWNED': WORKSPACE_OWNED,
    'WORKSPACE_RESERVED': WORKSPACE_RESERVED,
    'WORKSPACE_UNOWNED': WORKSPACE_UNOWNED,
    'WaitingPullRequestObservation': contracts.WaitingPullRequestObservation,
    'WorkerLevelDecision': contracts.WorkerLevelDecision,
    'WorkerPublication': contracts.WorkerPublication,
    'WorkerResultError': contracts.WorkerResultError,
    'WorkspaceOwnershipError': WorkspaceOwnershipError,
    '_ANSI_ESCAPE': contracts._ANSI_ESCAPE,
    '_BlockedCheckpointResults': _BlockedCheckpointResults,
    '_DURATION_NOISE': contracts._DURATION_NOISE,
    '_ExecutionStatePort': _ExecutionStatePort,
    '_FUTURE_IDEA_ITEM': contracts._FUTURE_IDEA_ITEM,
    '_GIT_OPERATION_PATHS': _GIT_OPERATION_PATHS,
    '_INLINE_TEST_COMMAND': contracts._INLINE_TEST_COMMAND,
    '_MUTABLE_READY_EVIDENCE_FIELDS': _MUTABLE_READY_EVIDENCE_FIELDS,
    '_NO_RUNNABLE_TARGETED_TEST': contracts._NO_RUNNABLE_TARGETED_TEST,
    '_PlanningFileReviewPorts': review_ports._PlanningFileReviewPorts,
    '_REVIEW_ISSUE_CATEGORY': contracts._REVIEW_ISSUE_CATEGORY,
    '_REVIEW_ISSUE_DECLARATION': contracts._REVIEW_ISSUE_DECLARATION,
    '_REVIEW_ISSUE_ID': contracts._REVIEW_ISSUE_ID,
    '_REVIEW_ISSUE_SYMBOL': contracts._REVIEW_ISSUE_SYMBOL,
    '_REVIEW_VERDICT': contracts._REVIEW_VERDICT,
    '_REVIEW_VERDICT_DECLARATION': contracts._REVIEW_VERDICT_DECLARATION,
    '_TARGETED_TEST_COMMAND': contracts._TARGETED_TEST_COMMAND,
    '_TARGETED_TEST_EXCEPTION_FAILURE': contracts._TARGETED_TEST_EXCEPTION_FAILURE,
    '_TARGETED_TEST_FAILURE': contracts._TARGETED_TEST_FAILURE,
    '_TARGETED_TEST_METADATA': contracts._TARGETED_TEST_METADATA,
    '_TARGETED_TEST_SUCCESS': contracts._TARGETED_TEST_SUCCESS,
    '_TEMP_DIRECTORY_CONTEXT': contracts._TEMP_DIRECTORY_CONTEXT,
    '_TEMP_PATH_NOISE': contracts._TEMP_PATH_NOISE,
    '_TIMESTAMP_NOISE': contracts._TIMESTAMP_NOISE,
    '_assert_maintenance_publication_intent': maintenance_publication._assert_maintenance_publication_intent,
    '_base_local_path_signature': _base_local_path_signature,
    '_base_worktree_index_error': _base_worktree_index_error,
    '_bind_effects': _bind_effects,
    '_blocked_agent_local_baseline': _blocked_agent_local_baseline,
    '_blocked_recovery_subjects': project_outcomes._blocked_recovery_subjects,
    '_blocked_validation_base_revision': plan_validation._blocked_validation_base_revision,
    '_canonical_path': _canonical_path,
    '_check_runtime_handoff': handoff_gate._check_runtime_handoff,
    '_checkpoint_dispatch_ready': project_outcomes._checkpoint_dispatch_ready,
    '_claude_result_message': _claude_result_message,
    '_clear_active_in_current_transaction': project_state_adapter._clear_active_in_current_transaction,
    '_clear_active_values': project_state_adapter._clear_active_values,
    '_clear_project_block_without_losing_planning_error': project_block_service._clear_project_block_without_losing_planning_error,
    '_coerce_agent_result': _coerce_agent_result,
    '_contains_token_sequence': planner_validation._contains_token_sequence,
    '_current_execplan_metadata': plan_validation._current_execplan_metadata,
    '_deserialize_agent_baseline': _deserialize_agent_baseline,
    '_deserialize_blocked_agent_recovery': blocked_agent_service._deserialize_blocked_agent_recovery,
    '_deserialize_failure_history': result_validation._deserialize_failure_history,
    '_deserialize_review_level_history': result_validation._deserialize_review_level_history,
    '_diagnosed_blocked_commit_allowances': _diagnosed_blocked_commit_allowances,
    '_direct_rebase_result_matches_observation': direct_rebase_service._direct_rebase_result_matches_observation,
    '_ensure_cleanup_pending_schema': _ensure_cleanup_pending_schema,
    '_ensure_execplans_schema': _ensure_execplans_schema,
    '_execplan_input_fingerprint': plan_validation._execplan_input_fingerprint,
    '_execplan_input_paths': project_state_adapter._execplan_input_paths,
    '_execplan_metadata': _execplan_metadata,
    '_execution_outcome_step_result': checkpoint_service._execution_outcome_step_result,
    '_final_block_recovery_decision': block_service._final_block_recovery_decision,
    '_git_operation_path_signature': _git_operation_path_signature,
    '_handoff_popen': handoff_gate._handoff_popen,
    '_handoff_run': handoff_gate._handoff_run,
    '_held_process_lock_identity': _held_process_lock_identity,
    '_idea_tokens': catalog_service._idea_tokens,
    '_insert_recovered_execplan': _insert_recovered_execplan,
    '_is_active_requirement': catalog_service._is_active_requirement,
    '_is_pr_conflict_preflight_block': pr_ownership._is_pr_conflict_preflight_block,
    '_legacy_runtime_adoption_path': _legacy_runtime_adoption_path,
    '_maintenance_change_context_text': cycle_service._maintenance_change_context_text,
    '_maintenance_context': maintenance_validation._maintenance_context,
    '_managed_worktree_receipt': bootstrap_adapter._managed_worktree_receipt,
    '_merged_execplan_ids': _merged_execplan_ids,
    '_metadata_changes_after_block': _metadata_changes_after_block,
    '_mutable_ready_cas_where': _mutable_ready_cas_where,
    '_non_default_index_flags': _non_default_index_flags,
    '_nonplan_input_digest': input_fingerprints._nonplan_input_digest,
    '_normalize_temp_path': result_validation._normalize_temp_path,
    '_normalized_config_origin': _normalized_config_origin,
    '_parse_execplan': catalog_service._parse_execplan,
    '_paths_overlap': git_adapter._paths_overlap,
    '_plan_id_from_path': maintenance_validation._plan_id_from_path,
    '_planning_input_digest': input_fingerprints._planning_input_digest,
    '_planning_outcome_step_result': project_outcomes._planning_outcome_step_result,
    '_quarantine_unverified_historical_runtime': _quarantine_unverified_historical_runtime,
    '_read_runtime_intent': _read_runtime_intent,
    '_reconcile_blocked_reviewer_plan': scope_state_service._reconcile_blocked_reviewer_plan,
    '_record_blocked_resume_head': blocked_plan_service._record_blocked_resume_head,
    '_record_legacy_runtime_adoption': _record_legacy_runtime_adoption,
    '_remote_checkpoint_input_identity': checkpoint_service._remote_checkpoint_input_identity,
    '_remote_checkpoint_refusal': checkpoint_service._remote_checkpoint_refusal,
    '_runtime_handoff_signal': handoff_gate._runtime_handoff_signal,
    '_runtime_intent_fields': _runtime_intent_fields,
    '_runtime_intent_path': _runtime_intent_path,
    '_same_local_snapshot': _same_local_snapshot,
    '_serialize_blocked_agent_recovery': blocked_agent_service._serialize_blocked_agent_recovery,
    '_specification_input_digest': input_fingerprints._specification_input_digest,
    '_start_agent_invocation': agent_adapter._start_agent_invocation,
    '_store_cleanup_pending': cleanup_service._store_cleanup_pending,
    '_stored_requirement_sources': requirement_archive_service._stored_requirement_sources,
    '_stored_string_list': planner_validation._stored_string_list,
    '_targeted_test_failure_matches': result_validation._targeted_test_failure_matches,
    '_update_blocked_error': block_service._update_blocked_error,
    '_valid_review_issue_path': result_validation._valid_review_issue_path,
    '_validate_dependency_graph': catalog_service._validate_dependency_graph,
    '_validate_legacy_runtime_adoption': _validate_legacy_runtime_adoption,
    '_worker_path_allowed': _worker_path_allowed,
    '_worktree_relative_target': blocked_agent_service._worktree_relative_target,
    '_write_process_lock_identity': _write_process_lock_identity,
    '_write_runtime_intent': _write_runtime_intent,
    'agent_adapter': agent_adapter,
    'agent_environment': agent_environment,
    'agent_invocation_state': agent_invocation_state,
    'agent_ports': agent_ports,
    'agent_profiles': agent_profiles,
    'agent_scope_decision': agent_scope_decision,
    'agent_write_isolation': agent_write_isolation,
    'all_cycle_plans_merged': all_cycle_plans_merged,
    'approval_policy_failure': approval_policy_failure,
    'archive_git': archive_git,
    'archive_service': archive_service,
    'argparse': argparse,
    'assessment_can_yield': assessment_can_yield,
    'base_fetch_service': base_fetch_service,
    'block_service': block_service,
    'blocked_agent_service': blocked_agent_service,
    'blocked_checkpoint': blocked_checkpoint,
    'blocked_checkpoint_state': blocked_checkpoint_state,
    'blocked_plan_reconciliation': blocked_plan_reconciliation,
    'blocked_plan_service': blocked_plan_service,
    'blocked_remote_observation': blocked_remote_observation,
    'bootstrap': bootstrap,
    'bootstrap_adapter': bootstrap_adapter,
    'catalog_migration': catalog_migration,
    'catalog_service': catalog_service,
    'checkpoint_adapter': checkpoint_adapter,
    'checkpoint_ownership': checkpoint_ownership,
    'checkpoint_recovery': checkpoint_recovery,
    'checkpoint_service': checkpoint_service,
    'classify_agent_result': classify_agent_result,
    'classify_pull_request_mergeability': classify_pull_request_mergeability,
    'claude_permission_arguments': claude_permission_arguments,
    'claude_usage': claude_usage,
    'cleanup_service': cleanup_service,
    'clear_active': project_state_adapter.clear_active,
    'codex_log_usage': codex_log_usage,
    'codex_sandbox_arguments': codex_sandbox_arguments,
    'command_adapter': command_adapter,
    'commands': commands,
    'configparser': configparser,
    'connect_database': connect_database,
    'contextlib': contextlib,
    'contracts': contracts,
    'cycle_plan_status_error': cycle_plan_status_error,
    'cycle_service': cycle_service,
    'dataclass': dataclass,
    'datetime': datetime,
    'diagnostic_preservation': diagnostic_preservation,
    'diagnostics': diagnostics,
    'difflib': difflib,
    'direct_rebase_service': direct_rebase_service,
    'document_publication': document_publication,
    'evaluate_review_worker_level': result_validation.evaluate_review_worker_level,
    'event_ports': event_ports,
    'execplan_archive': execplan_archive,
    'execplan_archive_state': execplan_archive_state,
    'execplan_catalog': execplan_catalog,
    'execplan_catalog_state': execplan_catalog_state,
    'execplan_purpose': result_validation.execplan_purpose,
    'execution_checkpoint_state': execution_checkpoint_state,
    'execution_handlers': execution_handlers,
    'execution_ports': execution_ports,
    'execution_revision': execution_revision,
    'expected_execplan_workspace': expected_execplan_workspace,
    'expected_runtime_worktree': expected_runtime_worktree,
    'field': field,
    'file_hash': input_fingerprints.file_hash,
    'general_planning_publication': general_planning_publication,
    'git_adapter': git_adapter,
    'git_ports': git_ports,
    'github_observation': github_observation,
    'github_repository': git_adapter.github_repository,
    'guidance_service': guidance_service,
    'handoff_gate': handoff_gate,
    'hashlib': hashlib,
    'hydrate_execplan_project': project_state_adapter.hydrate_execplan_project,
    'input_fingerprints': input_fingerprints,
    'invocation_service': invocation_service,
    'is_git_lock_failure': git_adapter.is_git_lock_failure,
    'is_lock_contention': is_lock_contention,
    'is_unowned_ready': is_unowned_ready,
    'json': json,
    'legacy_migration': legacy_migration,
    'load_execplan_snapshot': catalog_service.load_execplan_snapshot,
    'load_managed_config': load_managed_config,
    'local_now': operation_events.local_now,
    'maintenance_file_inventory': maintenance_file_inventory,
    'maintenance_file_ledger': maintenance_file_ledger,
    'maintenance_file_publication': maintenance_file_publication,
    'maintenance_file_review': maintenance_file_review,
    'maintenance_findings': maintenance_findings,
    'maintenance_git': maintenance_git,
    'maintenance_planner_prompt': maintenance_planner_prompt,
    'maintenance_planning_fingerprint': maintenance_planning_fingerprint,
    'maintenance_publication': maintenance_publication,
    'maintenance_reconciliation': maintenance_reconciliation,
    'maintenance_state': maintenance_state,
    'maintenance_validation': maintenance_validation,
    'managed_rebase_adoption': managed_rebase_adoption,
    'managed_rebase_evidence': managed_rebase_evidence,
    'managed_rebase_publication': managed_rebase_publication,
    'managed_rebase_runtime': managed_rebase_runtime,
    'managed_rebase_validation': managed_rebase_validation,
    'manager_git_journal': manager_git_journal,
    'manual_candidate': manual_candidate,
    'manual_plan_generation': manual_plan_generation,
    'manual_work': manual_work,
    'mark_execplan_workspace_owned': project_selection.mark_execplan_workspace_owned,
    'merge_cleanup_service': merge_cleanup_service,
    'mutable_snapshot_token': mutable_snapshot_token,
    'normalize_feedback': normalize_feedback,
    'normalized_text': input_fingerprints.normalized_text,
    'notifications': notifications,
    'operation_events': operation_events,
    'os': os,
    'parse_frontmatter': input_fingerprints.parse_frontmatter,
    'parse_maintenance_planner_decision': parse_maintenance_planner_decision,
    'plan_validation': plan_validation,
    'planner_prompt': planner_prompt,
    'planner_publication': planner_publication,
    'planner_validation': planner_validation,
    'planning_diagnostics_path': diagnostics.planning_diagnostics_path,
    'planning_ports': planning_ports,
    'planning_publication_state': planning_publication_state,
    'planning_workspace': planning_workspace,
    'pr_description': pr_description,
    'pr_ownership': pr_ownership,
    'process_lock': process_lock,
    'project_block_service': project_block_service,
    'project_blocked_recovery': project_blocked_recovery,
    'project_catalog_refresh': project_catalog_refresh,
    'project_execplan': legacy_migration.project_execplan,
    'project_outcomes': project_outcomes,
    'project_registry': project_registry,
    'project_runner': project_runner,
    'project_selection': project_selection,
    'project_service': project_service,
    'project_state_adapter': project_state_adapter,
    'provider_adapters': provider_adapters,
    'provider_profile': provider_profile,
    'provider_worker_profile': provider_worker_profile,
    'publication_service': publication_service,
    'pull_request_recovery': pull_request_recovery,
    'quote': quote,
    're': re,
    'rebase_block_reconciliation': rebase_block_reconciliation,
    'rebase_file_evidence': rebase_file_evidence,
    'recovery_adapter': recovery_adapter,
    'recovery_runtime': recovery_runtime,
    'recovery_state': recovery_state,
    'redact_secrets': redact_secrets,
    'registration_service': registration_service,
    'replace': replace,
    'requirement_archive': requirement_archive,
    'requirement_archive_path': input_fingerprints.requirement_archive_path,
    'requirement_archive_reconciliation_error': requirement_archive_service.requirement_archive_reconciliation_error,
    'requirement_archive_service': requirement_archive_service,
    'requirement_archive_state': requirement_archive_state,
    'reserve_execplan_workspace': project_selection.reserve_execplan_workspace,
    'result_validation': result_validation,
    'review_ports': review_ports,
    'reviewer_prompt': reviewer_prompt,
    'reviewer_specification': reviewer_specification,
    'reviewer_targeted_test_reason': result_validation.reviewer_targeted_test_reason,
    'run_agent_with_retries': run_agent_with_retries,
    'run_verify_process': run_verify_process,
    'runtime_child_arguments': runtime_child_arguments,
    'runtime_repository_id': runtime_repository_id,
    'runtime_scope': runtime_scope,
    'runtime_state': runtime_state,
    'scheduler': scheduler,
    'scope_adapter': scope_adapter,
    'scope_baseline': scope_baseline,
    'scope_lifecycle': scope_lifecycle,
    'scope_observation': scope_observation,
    'scope_reconciliation': scope_reconciliation,
    'scope_recovery': scope_recovery,
    'scope_state_service': scope_state_service,
    'scope_validation': scope_validation,
    'scoped_blocking': scoped_blocking,
    'secrets': secrets,
    'select_cycle_ready_execplan': select_cycle_ready_execplan,
    'self_runtime': self_runtime,
    'session_resume_unavailable': session_resume_unavailable,
    'shutil': shutil,
    'snapshot_from_dict': snapshot_from_dict,
    'snapshot_to_dict': snapshot_to_dict,
    'source_config_path': source_config_path,
    'source_sync': source_sync,
    'sqlite3': sqlite3,
    'stat': stat,
    'state_directory': state_directory,
    'state_ports': state_ports,
    'subprocess': subprocess,
    'sys': sys,
    'tempfile': tempfile,
    'terminal_new_plan_ids': terminal_new_plan_ids,
    'threading': threading,
    'time': time,
    'unmet_execplan_dependencies': plan_validation.unmet_execplan_dependencies,
    'validate_barrier_revision': validate_barrier_revision,
    'validate_prerequisite_plan_ids': validate_prerequisite_plan_ids,
    'validate_provider_profiles': validate_provider_profiles,
    'validate_refactoring_plan': validate_refactoring_plan,
    'validate_revalidation_revision': validate_revalidation_revision,
    'validation_feedback_state': validation_feedback_state,
    'validation_ports': validation_ports,
    'waiting_pr_service': waiting_pr_service,
    'work_exhaustion': work_exhaustion,
    'worker_level': worker_level,
    'worker_outcome_instruction': worker_outcome_instruction,
    'worker_prompt': worker_prompt,
    'workspace': workspace,
    'workspace_service': workspace_service,
}
