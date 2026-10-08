"""execution execution ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.agent_invocation import AgentResultKind
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.application.contracts import ExecPlanMetadataError
from loop_manager_internal.application.contracts import MAX_REVIEWER_PROTOCOL_ATTEMPTS
from loop_manager_internal.application.contracts import MAX_VALIDATION_FINGERPRINT_REPEATS
from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
from loop_manager_internal.application.contracts import PROJECT_STEP_USAGE_PAUSE
from loop_manager_internal.application.contracts import RequirementArchiveCollisionError
from loop_manager_internal.application.contracts import RequirementArchiveError
from loop_manager_internal.application.contracts import ReviewerProtocolError
from loop_manager_internal.application.contracts import TRANSIENT_AGENT_ERROR_PREFIX
from loop_manager_internal.application.contracts import USAGE_LIMIT_ERROR_PREFIX
from loop_manager_internal.application.contracts import WorkerResultError
from loop_manager_internal.execution import execution_handlers, result_validation
from loop_manager_internal.execution.execution_state import ExecutionStatePort as _ExecutionStatePort
from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.runtime.contracts import PROJECT_STEP_PLANNING_PENDING
from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.runtime.contracts import REBASE_CONFLICT_PREFIX
from loop_manager_internal.runtime.contracts import REBASE_IN_PROGRESS_ERROR
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WORKSPACE_RESERVED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import validation_feedback_state
from pathlib import Path
from loop_manager_internal.execution.execution_effects import ExecutionContract


@dataclass(frozen=True)
class TypedExecutionGitEffects:
    """Named effects for _typed_execution_git; no entry-point/module access."""
    git: Callable[..., object]
    is_git_lock_failure: Callable[..., object]


def _typed_execution_git(effects: TypedExecutionGitEffects, path: Path, *arguments: str, **options: object):
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
        result = effects.git(path, *arguments, **options)
        if result.returncode and effects.is_git_lock_failure(result.stdout):
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


@dataclass(frozen=True)
class TypedExecutionCommandEffects:
    """Named effects for _typed_execution_command; no entry-point/module access."""
    command: Callable[..., object]


def _typed_execution_command(effects: TypedExecutionCommandEffects, *arguments: object, **options: object):
    try:
        return effects.command(*arguments, **options)
    except (RuntimeError, OSError) as error:
        raise recovery_runtime.RecoveryFailure(
            recovery_runtime.RecoveryReason.PUBLICATION_RETRY, str(error)
        ) from error


@dataclass(frozen=True)
class ExecutionLifecyclePortsEffects:
    """Named effects for _ExecutionLifecyclePorts; no entry-point/module access."""
    _agent_profile: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _path_signature: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    _revision_changed_paths: Callable[..., object]
    _revision_execplan_body_hash: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _revision_execplan_source_fingerprint: Callable[..., object]
    _run_agent_with_connection: Callable[..., object]
    _typed_execution_command: Callable[..., object]
    _typed_execution_git: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    begin_agent_scope: Callable[..., object]
    block_active_execplan: Callable[..., object]
    block_active_execplan_from_revision: Callable[..., object]
    block_verify_timeout_limit: Callable[..., object]
    branch_has_changes: Callable[..., object]
    build_pr_body: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    clear_agent_scope: Callable[..., object]
    clear_validation_failure_history: Callable[..., object]
    ensure_worker_level_state: Callable[..., object]
    git: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    make_worktree: Callable[..., object]
    mark_execplan_workspace_owned: Callable[..., object]
    operation_log: Callable[..., object]
    operation_print: Callable[..., object]
    parse_reviewer_result: Callable[..., object]
    parse_worker_result: Callable[..., object]
    pr_for_branch: Callable[..., object]
    published_execplan: Callable[..., object]
    push_managed_publication_branch: Callable[..., object]
    push_owned_pr_conflict_recovery: Callable[..., object]
    reconcile_execution_execplan_catalog: Callable[..., object]
    reconcile_existing_pull_request: Callable[..., object]
    record_remote_publication_branch: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    requirement_archive_candidates: Callable[..., object]
    requirement_archive_move_set: Callable[..., object]
    requirement_archive_reconciliation_error: Callable[..., object]
    requirement_archive_snapshot: Callable[..., object]
    requirement_archive_sources: Callable[..., object]
    requirement_archive_touched_sources: Callable[..., object]
    reserve_execplan_workspace: Callable[..., object]
    resume_pr_conflict_rebase: Callable[..., object]
    reviewer_prompt: Callable[..., object]
    reviewer_targeted_test_reason: Callable[..., object]
    run_agent_with_retries: Callable[..., object]
    transition_validation_failure: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    validate_active_execplan_metadata: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_worker_workspace_scope: Callable[..., object]
    validation_failure_fingerprints: Callable[..., object]
    verify: Callable[..., object]
    worker_prompt: Callable[..., object]
    mutable_ready_snapshot: Callable[..., object]
    validate_planner_result: Callable[..., object]
    _assert_mutable_ready_snapshot_current: Callable[..., object]
    git_bytes: Callable[..., bytes] | None = None


def _ExecutionLifecyclePorts(dependencies: ExecutionLifecyclePortsEffects, connection, runtime_repository, **identity):
    """Bind named effects to legacy helpers, preserving their patch points."""
    verify_timeout_seconds = identity.pop("verify_timeout_seconds", 600)
    state = _ExecutionStatePort(connection, runtime_repository, identity["repo"])
    from loop_manager_internal.execution.manual_candidate import ManualCandidate, MANUAL_TEST_EVIDENCE
    manual_git_bytes = dependencies.git_bytes or (
        lambda repo, *args: dependencies.git(repo, *args).stdout.encode("utf-8")
    )
    manual = ManualCandidate(connection, identity['repo'], state.plan_id, dependencies.git,
                             manual_git_bytes, dependencies.pr_for_branch,
                             identity.get('config_path'), identity.get('provider'),
                             dependencies._agent_profile)
    state.manual = manual
    def workspace_identity(state_dir, repo, plan_id, branch, worktree):
        return dependencies.validate_execplan_workspace_identity(state_dir, repo,
            manual.store.workspace_owner(repo, plan_id), branch, worktree)
    def publication(message, plan):
        return manual.publication(message, plan, dependencies.parse_worker_result)
    def targeted_reason(evidence):
        return ('수동 제출의 외부 targeted test 결과는 Manager가 확인하지 않았습니다.'
                if evidence == MANUAL_TEST_EVIDENCE else dependencies.reviewer_targeted_test_reason(evidence))
    from loop_manager_internal.execution import stage_documents
    document_checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    document_effects = stage_documents.StageDocumentEffects(
        git=state.bind(dependencies._typed_execution_git, name="document_git", connected=False, mutates=False),
        git_bytes=dependencies.git_bytes,
        capture_scope=dependencies.capture_git_snapshot,
        validate_scope=lambda *a, **kw: dependencies.validate_agent_scope(*a, connection=connection, **kw),
        run_agent=state.bind(dependencies._run_agent_with_connection, name="document_planner", connected=True, mutates=False),
        prepare_snapshot=dependencies.mutable_ready_snapshot,
        assert_snapshot=dependencies._assert_mutable_ready_snapshot_current,
        validate_result=dependencies.validate_planner_result,
        validate_worktree=dependencies.validate_managed_worktree,
        pr_for_branch=dependencies.pr_for_branch,
        log=dependencies.operation_log,
        run_with_retries=dependencies.run_agent_with_retries,
    )
    from loop_manager_internal.execution.execution_session import ExecutionIdentity
    document_identity = ExecutionIdentity(**identity)
    effects = execution_handlers.ExecutionEffects(
        state=state,
        checkpoint=execution_checkpoint_state.ExecutionCheckpointRepository(connection),
        contract=ExecutionContract(AgentResultKind=AgentResultKind, AgentScopeError=AgentScopeError, ExecPlanMetadataError=ExecPlanMetadataError, GitLockError=GitLockError, MAX_REVIEWER_PROTOCOL_ATTEMPTS=MAX_REVIEWER_PROTOCOL_ATTEMPTS, MAX_VALIDATION_FINGERPRINT_REPEATS=MAX_VALIDATION_FINGERPRINT_REPEATS, PROJECT_STEP_RETRY=PROJECT_STEP_RETRY, PROJECT_STEP_TRANSIENT_PAUSE=PROJECT_STEP_TRANSIENT_PAUSE, PROJECT_STEP_USAGE_PAUSE=PROJECT_STEP_USAGE_PAUSE, PROJECT_STEP_PLANNING_PENDING=PROJECT_STEP_PLANNING_PENDING, PublicationSafetyError=PublicationSafetyError, REBASE_CONFLICT_PREFIX=REBASE_CONFLICT_PREFIX, REBASE_IN_PROGRESS_ERROR=REBASE_IN_PROGRESS_ERROR, RequirementArchiveCollisionError=RequirementArchiveCollisionError, RequirementArchiveError=RequirementArchiveError, ReviewerProtocolError=ReviewerProtocolError, TRANSIENT_AGENT_ERROR_PREFIX=TRANSIENT_AGENT_ERROR_PREFIX, USAGE_LIMIT_ERROR_PREFIX=USAGE_LIMIT_ERROR_PREFIX, WORKSPACE_OWNED=WORKSPACE_OWNED, WORKSPACE_RESERVED=WORKSPACE_RESERVED, WorkerResultError=WorkerResultError, WorkspaceOwnershipError=WorkspaceOwnershipError, reviewer_targeted_test_reason=targeted_reason, VerifyTerminationUncertain=VerifyTerminationUncertain),
        workspace=execution_handlers.WorkspaceEffects(
            is_directory=lambda path: path.is_dir(),
            git=state.bind(dependencies._typed_execution_git, name="git", connected=False, mutates=False),
            git_bytes=dependencies.git_bytes,
            make_worktree=state.bind(dependencies.make_worktree, name="make_worktree", connected=False, mutates=False),
            branch_has_changes=state.bind(dependencies.branch_has_changes, name="branch_has_changes", connected=False, mutates=False),
            remote_branch_oid=state.bind(dependencies.remote_branch_oid, name="remote_branch_oid", connected=False, mutates=False),
            _rebase_in_progress_for_branch=state.bind(dependencies._rebase_in_progress_for_branch, name="_rebase_in_progress_for_branch", connected=False, mutates=False),
            validate_execplan_workspace_identity=state.bind(workspace_identity, name="validate_execplan_workspace_identity", connected=False, mutates=False),
            validate_managed_worktree=state.bind(dependencies.validate_managed_worktree, name="validate_managed_worktree", connected=False, mutates=False),
            _plan_id_from_path=state.bind(dependencies._plan_id_from_path, name="_plan_id_from_path", connected=False, mutates=False),
            reserve_execplan_workspace=state.bind(dependencies.reserve_execplan_workspace, name="reserve_execplan_workspace", connected=True, mutates=True),
            mark_execplan_workspace_owned=state.bind(dependencies.mark_execplan_workspace_owned, name="mark_execplan_workspace_owned", connected=True, mutates=True),
            resume_pr_conflict_rebase=state.bind(dependencies.resume_pr_conflict_rebase, name="resume_pr_conflict_rebase", connected=True, mutates=True),
            git_operation_entries=state.bind(dependencies._git_operation_entries, name="git_operation_entries", connected=False, mutates=False),
            current_rebase_service=lambda context, **options: dependencies._current_rebase_service(
                connection, identity["repo_path"], identity["state_dir"], identity["repo"],
                context.plan.plan_id, context.worktree, context.branch, **options),
            live_remote_branch_oid=state.bind(
                lambda repo, branch: manager_git_journal.live_remote_branch_oid(dependencies.git, repo, branch),
                name="live_remote_branch_oid", connected=False, mutates=False),
        ),
        agent=execution_handlers.AgentEffects(
            begin_agent_scope=state.bind(dependencies.begin_agent_scope, name="begin_agent_scope", connected=True, mutates=True),
            capture_git_snapshot=state.bind(dependencies.capture_git_snapshot, name="capture_git_snapshot", connected=False, mutates=False),
            path_signature=state.bind(dependencies._path_signature, name="path_signature", connected=False, mutates=False),
            clear_agent_scope=state.bind(dependencies.clear_agent_scope, name="clear_agent_scope", connected=True, mutates=True),
            validate_agent_scope=state.bind(
                lambda *args, **kwargs: dependencies.validate_agent_scope(*args, connection=connection, **kwargs),
                name="validate_agent_scope", connected=False, mutates=False),
            validate_worker_workspace_scope=state.bind(
                lambda *args, **kwargs: dependencies.validate_worker_workspace_scope(*args, connection=connection, **kwargs),
                name="validate_worker_workspace_scope", connected=False, mutates=False),
            _worktree_execplan_input_fingerprint=state.bind(dependencies._worktree_execplan_input_fingerprint, name="_worktree_execplan_input_fingerprint", connected=False, mutates=False),
            _revision_execplan_input_fingerprint=state.bind(dependencies._revision_execplan_input_fingerprint, name="_revision_execplan_input_fingerprint", connected=False, mutates=False),
            _published_plan_allows_common_plans=state.bind(dependencies._published_plan_allows_common_plans, name="_published_plan_allows_common_plans", connected=False, mutates=False),
            is_loop_manager_repository=state.bind(dependencies.is_loop_manager_repository, name="is_loop_manager_repository", connected=False, mutates=False),
            run_agent=state.bind(dependencies._run_agent_with_connection, name="run_agent", connected=True, mutates=False),
            run_agent_with_retries=state.bind(dependencies.run_agent_with_retries, name="run_agent_with_retries", connected=False, mutates=False),
            worker_prompt=state.bind(lambda *a, **kw: manual.prompt(dependencies.worker_prompt(*a, **kw)), name="worker_prompt", connected=False, mutates=False),
            reviewer_prompt=state.bind(lambda *a, **kw: manual.prompt(dependencies.reviewer_prompt(*a, **kw), review=True), name="reviewer_prompt", connected=False, mutates=False),
            resolve_worker_profile=lambda config_path, provider, level, worktree: dependencies._agent_profile(
                config_path, provider, "worker", level, worktree,
            ),
        ),
        validation=execution_handlers.ValidationEffects(
            observe_verify_inputs=state.bind(
                result_validation.observe_verify_inputs,
                name="observe_verify_inputs", connected=False, mutates=False,
            ),
            verify=state.bind(
                lambda worktree: dependencies.verify(
                    worktree, verify_timeout_seconds,
                ),
                name="verify", connected=False, mutates=False,
            ),
            block_verify_timeout_limit=state.bind(
                dependencies.block_verify_timeout_limit, name="block_verify_timeout_limit",
                connected=True, mutates=True,
            ),
            parse_worker_result=state.bind(publication, name="parse_worker_result", connected=False, mutates=False),
            parse_reviewer_result=state.bind(dependencies.parse_reviewer_result, name="parse_reviewer_result", connected=False, mutates=False),
            validate_active_execplan_metadata=state.bind(dependencies.validate_active_execplan_metadata, name="validate_active_execplan_metadata", connected=False, mutates=False),
            ensure_worker_level_state=state.bind(dependencies.ensure_worker_level_state, name="ensure_worker_level_state", connected=True, mutates=True),
            clear_validation_failure_history=state.bind(dependencies.clear_validation_failure_history, name="clear_validation_failure_history", connected=True, mutates=True),
            transition_validation_failure=state.bind(dependencies.transition_validation_failure, name="transition_validation_failure", connected=True, mutates=True),
        ),
        feedback=execution_handlers.FeedbackEffects(
            repository=validation_feedback_state.ValidationFeedbackRepository(connection),
            fingerprints=dependencies.validation_failure_fingerprints,
        ),
        archive=execution_handlers.ArchiveEffects(
            requirement_archive_sources=state.bind(dependencies.requirement_archive_sources, name="requirement_archive_sources", connected=True, mutates=False),
            requirement_archive_snapshot=state.bind(dependencies.requirement_archive_snapshot, name="requirement_archive_snapshot", connected=True, mutates=False),
            requirement_archive_candidates=state.bind(dependencies.requirement_archive_candidates, name="requirement_archive_candidates", connected=True, mutates=False),
            requirement_archive_move_set=state.bind(dependencies.requirement_archive_move_set, name="requirement_archive_move_set", connected=False, mutates=False),
            requirement_archive_reconciliation_error=state.bind(dependencies.requirement_archive_reconciliation_error, name="requirement_archive_reconciliation_error", connected=False, mutates=False),
            requirement_archive_touched_sources=state.bind(dependencies.requirement_archive_touched_sources, name="requirement_archive_touched_sources", connected=False, mutates=False),
        ),
        publication=execution_handlers.PublicationEffects(
            pr_for_branch=state.bind(dependencies.pr_for_branch, name="pr_for_branch", connected=False, mutates=False),
            reconcile_existing_pull_request=state.bind(dependencies.reconcile_existing_pull_request, name="reconcile_existing_pull_request", connected=True, mutates=True),
            record_remote_publication_branch=state.bind(dependencies.record_remote_publication_branch, name="record_remote_publication_branch", connected=True, mutates=True),
            push_managed_publication_branch=state.bind(dependencies.push_managed_publication_branch, name="push_managed_publication_branch", connected=True, mutates=True),
            push_owned_pr_conflict_recovery=state.bind(dependencies.push_owned_pr_conflict_recovery, name="push_owned_pr_conflict_recovery", connected=True, mutates=True),
            published_execplan=state.bind(dependencies.published_execplan, name="published_execplan", connected=False, mutates=False),
            build_pr_body=state.bind(lambda *a, **kw: manual.decorate_body(dependencies.build_pr_body(*a, **kw)), name="build_pr_body", connected=False, mutates=False),
            command=state.bind(dependencies._typed_execution_command, name="command", connected=False, mutates=False),
        ),
        blocking=execution_handlers.BlockingEffects(
            block_active_execplan=state.bind(dependencies.block_active_execplan, name="block_active_execplan", connected=True, mutates=True),
            block_active_execplan_from_revision=state.bind(dependencies.block_active_execplan_from_revision, name="block_active_execplan_from_revision", connected=True, mutates=True),
        ),
        events=execution_handlers.EventsEffects(
            operation_log=state.bind(dependencies.operation_log, name="operation_log", connected=False, mutates=False),
            operation_print=state.bind(dependencies.operation_print, name="operation_print", connected=False, mutates=False),
        ),
        revision=execution_handlers.RevisionEffects(
            reconcile_catalog=state.bind(dependencies.reconcile_execution_execplan_catalog, name="reconcile_execplan_catalog", connected=True, mutates=True),
            source_fingerprint=state.bind(dependencies._revision_execplan_source_fingerprint, name="_revision_execplan_source_fingerprint", connected=False, mutates=False),
            body_hash=state.bind(dependencies._revision_execplan_body_hash, name="_revision_execplan_body_hash", connected=False, mutates=False),
            changed_paths=state.bind(dependencies._revision_changed_paths, name="_revision_changed_paths", connected=False, mutates=False),
            unmet_dependencies=state.bind(dependencies.unmet_execplan_dependencies, name="unmet_execplan_dependencies", connected=True, mutates=False),
            refresh_documents=lambda context, previous, definition, changed: None if manual.enabled() else stage_documents.refresh(
                document_effects, state, document_checkpoints, document_identity,
                context, previous, definition, changed),
        ),
    )
    return execution_handlers.ExecutionHandlers(effects, **identity)
