"""Named assembly effects and immutable execution policy contract."""
from __future__ import annotations
from dataclasses import dataclass, fields
from typing import Any, Callable
from loop_manager_internal.agents.agent_invocation import AgentResultKind, ResolvedProfile
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.application import contracts as application
from loop_manager_internal.runtime import contracts as runtime
from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository


@dataclass(frozen=True)
class ExecutionContract:
    """Explicit policy constants; no module or anonymous namespace crosses a phase port."""
    AgentResultKind: type = AgentResultKind
    AgentScopeError: type = AgentScopeError
    ExecPlanMetadataError: type = application.ExecPlanMetadataError
    GitLockError: type = runtime.GitLockError
    MAX_REVIEWER_PROTOCOL_ATTEMPTS: int = application.MAX_REVIEWER_PROTOCOL_ATTEMPTS
    MAX_VALIDATION_FINGERPRINT_REPEATS: int = application.MAX_VALIDATION_FINGERPRINT_REPEATS
    PROJECT_STEP_PLANNING_PENDING: int = runtime.PROJECT_STEP_PLANNING_PENDING
    PROJECT_STEP_RETRY: int = runtime.PROJECT_STEP_RETRY
    PROJECT_STEP_TRANSIENT_PAUSE: int = application.PROJECT_STEP_TRANSIENT_PAUSE
    PROJECT_STEP_USAGE_PAUSE: int = application.PROJECT_STEP_USAGE_PAUSE
    PublicationSafetyError: type = runtime.PublicationSafetyError
    REBASE_CONFLICT_PREFIX: str = runtime.REBASE_CONFLICT_PREFIX
    REBASE_IN_PROGRESS_ERROR: str = runtime.REBASE_IN_PROGRESS_ERROR
    RequirementArchiveCollisionError: type = application.RequirementArchiveCollisionError
    RequirementArchiveError: type = application.RequirementArchiveError
    ReviewerProtocolError: type = application.ReviewerProtocolError
    TRANSIENT_AGENT_ERROR_PREFIX: str = application.TRANSIENT_AGENT_ERROR_PREFIX
    USAGE_LIMIT_ERROR_PREFIX: str = application.USAGE_LIMIT_ERROR_PREFIX
    VerifyTerminationUncertain: type = VerifyTerminationUncertain
    WORKSPACE_OWNED: int = runtime.WORKSPACE_OWNED
    WORKSPACE_RESERVED: int = runtime.WORKSPACE_RESERVED
    WorkerResultError: type = application.WorkerResultError
    WorkspaceOwnershipError: type = runtime.WorkspaceOwnershipError
    reviewer_targeted_test_reason: Callable[..., str] | None = None

    @classmethod
    def from_source(cls, source: object) -> ExecutionContract:
        """Normalize legacy assembly inputs while preserving exception identities."""
        if isinstance(source, cls):
            return source
        defaults = cls()
        return cls(**{field.name: getattr(source, field.name, getattr(defaults, field.name))
                      for field in fields(cls)})


@dataclass(frozen=True)
class WorkspaceEffects:
    is_directory: Callable[..., Any]
    git: Callable[..., Any]
    make_worktree: Callable[..., Any]
    branch_has_changes: Callable[..., Any]
    remote_branch_oid: Callable[..., Any]
    _rebase_in_progress_for_branch: Callable[..., Any]
    validate_execplan_workspace_identity: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    _plan_id_from_path: Callable[..., Any]
    reserve_execplan_workspace: Callable[..., Any]
    mark_execplan_workspace_owned: Callable[..., Any]
    resume_pr_conflict_rebase: Callable[..., Any]
    git_operation_entries: Callable[..., Any] | None = None
    current_rebase_service: Callable[..., Any] | None = None
    live_remote_branch_oid: Callable[..., Any] | None = None
    git_bytes: Callable[..., bytes] | None = None
    reconcile_remote: Callable[..., Any] | None = None

@dataclass(frozen=True)
class AgentEffects:
    begin_agent_scope: Callable[..., Any]
    capture_git_snapshot: Callable[..., Any]
    path_signature: Callable[..., str]
    clear_agent_scope: Callable[..., Any]
    validate_agent_scope: Callable[..., Any]
    validate_worker_workspace_scope: Callable[..., Any]
    _worktree_execplan_input_fingerprint: Callable[..., Any]
    _revision_execplan_input_fingerprint: Callable[..., Any]
    _published_plan_allows_common_plans: Callable[..., Any]
    is_loop_manager_repository: Callable[..., Any]
    run_agent: Callable[..., Any]
    run_agent_with_retries: Callable[..., Any]
    worker_prompt: Callable[..., Any]
    reviewer_prompt: Callable[..., Any]
    resolve_worker_profile: Callable[..., ResolvedProfile]

@dataclass(frozen=True)
class ValidationEffects:
    verify: Callable[..., Any]
    block_verify_timeout_limit: Callable[..., Any]
    parse_worker_result: Callable[..., Any]
    parse_reviewer_result: Callable[..., Any]
    validate_active_execplan_metadata: Callable[..., Any]
    ensure_worker_level_state: Callable[..., Any]
    clear_validation_failure_history: Callable[..., Any]
    transition_validation_failure: Callable[..., Any]
    observe_verify_inputs: Callable[..., Any] | None = None
    partial_verify_enabled: Callable[..., bool] | None = None
    partial_verify: Callable[..., Any] | None = None
    pre_review_identity: Callable[..., Any] | None = None

@dataclass(frozen=True)
class FeedbackEffects:
    repository: ValidationFeedbackRepository
    fingerprints: Callable[..., Any]

@dataclass(frozen=True)
class ArchiveEffects:
    requirement_archive_sources: Callable[..., Any]
    requirement_archive_snapshot: Callable[..., Any]
    requirement_archive_candidates: Callable[..., Any]
    requirement_archive_move_set: Callable[..., Any]
    requirement_archive_reconciliation_error: Callable[..., Any]
    requirement_archive_touched_sources: Callable[..., Any]

@dataclass(frozen=True)
class PublicationEffects:
    pr_for_branch: Callable[..., Any]
    reconcile_existing_pull_request: Callable[..., Any]
    record_remote_publication_branch: Callable[..., Any]
    push_managed_publication_branch: Callable[..., Any]
    push_owned_pr_conflict_recovery: Callable[..., Any]
    published_execplan: Callable[..., Any]
    build_pr_body: Callable[..., Any]
    command: Callable[..., Any]
    github_repository: Callable[..., Any] | None = None
    publish_worker_checkpoints: Callable[..., Any] | None = None

@dataclass(frozen=True)
class BlockingEffects:
    block_active_execplan: Callable[..., Any]
    block_active_execplan_from_revision: Callable[..., Any]

@dataclass(frozen=True)
class EventsEffects:
    operation_log: Callable[..., Any]
    operation_print: Callable[..., Any]


@dataclass(frozen=True)
class RevisionEffects:
    reconcile_catalog: Callable[..., Any]
    source_fingerprint: Callable[..., Any]
    body_hash: Callable[..., Any]
    changed_paths: Callable[..., Any]
    unmet_dependencies: Callable[..., Any]
    refresh_documents: Callable[..., Any] | None = None

@dataclass(frozen=True)
class ExecutionEffects:
    state: ExecutionStatePort
    contract: ExecutionContract
    workspace: WorkspaceEffects
    agent: AgentEffects
    validation: ValidationEffects
    archive: ArchiveEffects
    publication: PublicationEffects
    blocking: BlockingEffects
    events: EventsEffects
    revision: RevisionEffects
    checkpoint: ExecutionCheckpointRepository | None = None
    feedback: FeedbackEffects | None = None
