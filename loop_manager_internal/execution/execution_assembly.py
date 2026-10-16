"""Compose phase ports from selected effect callbacks, never the whole effects bundle."""
from __future__ import annotations
from typing import Any, Callable, Mapping
from loop_manager_internal.execution.execution_effects import ExecutionEffects, ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot, legacy_plan_metadata

from loop_manager_internal.execution.execution_context_handler import ContextHandler, ContextHandlerPorts

from loop_manager_internal.execution.execution_recovery_handler import RecoveryHandler, RecoveryHandlerPorts

from loop_manager_internal.execution.execution_documents_handler import DocumentsHandler, DocumentsHandlerPorts

from loop_manager_internal.execution.execution_ownership_handler import OwnershipHandler, OwnershipHandlerPorts

from loop_manager_internal.execution.execution_rebase_handler import RebaseHandler, RebaseHandlerPorts

from loop_manager_internal.execution.execution_workspace_handler import WorkspaceHandler, WorkspaceHandlerPorts

from loop_manager_internal.execution.execution_revision_handler import RevisionHandler, RevisionHandlerPorts

from loop_manager_internal.execution.execution_archive_handler import ArchiveHandler, ArchiveHandlerPorts

from loop_manager_internal.execution.execution_worker_handler import WorkerHandler, WorkerHandlerPorts

from loop_manager_internal.execution.execution_verify_handler import VerifyHandler, VerifyHandlerPorts

from loop_manager_internal.execution.execution_review_handler import ReviewHandler, ReviewHandlerPorts

from loop_manager_internal.execution.execution_latest_base_handler import LatestBaseHandler, LatestBaseHandlerPorts

from loop_manager_internal.execution.execution_publication_handler import PublicationHandler, PublicationHandlerPorts


def _legacy_plan_callback(callback: Callable[..., Any]) -> Callable[..., Any]:
    """Adapt immutable metadata at the existing helper boundary."""
    def invoke(value: Any, plan: Mapping[str, object], *args: Any, **kwargs: Any) -> Any:
        return callback(value, legacy_plan_metadata(plan), *args, **kwargs)
    return invoke


def context(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> ContextHandler:
    return ContextHandler(ContextHandlerPorts(
        identity=identity,
        snapshot=snapshot,
        update_snapshot=update_snapshot,
        state=effects.state,
        operation_log=effects.events.operation_log,
        _resolving_rebase=callbacks["_resolving_rebase"],
    ))


def recovery(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> RecoveryHandler:
    return RecoveryHandler(RecoveryHandlerPorts(
        identity=identity,
        snapshot=snapshot,
        contract=ExecutionContract.from_source(effects.contract),
        state=effects.state,
        recovery=recovery,
        _row=callbacks["_row"],
        capture_git_snapshot=effects.agent.capture_git_snapshot,
    ))


def documents(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> DocumentsHandler:
    return DocumentsHandler(DocumentsHandlerPorts(
        identity=identity,
        snapshot=snapshot,
        state=effects.state,
        is_loop_manager_repository=effects.agent.is_loop_manager_repository,
        _block_final=callbacks["_block_final"],
        _document_block=callbacks["_document_block"],
        _finish_worker=callbacks["_finish_worker"],
        _row=callbacks["_row"],
    ))


def ownership(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> OwnershipHandler:
    return OwnershipHandler(OwnershipHandlerPorts(
        identity=identity,
        checkpoint=effects.checkpoint,
        state=effects.state,
        current_rebase_service=getattr(effects.workspace, "current_rebase_service", None),
        git=effects.workspace.git,
        remote_branch_oid=effects.workspace.remote_branch_oid,
        validate_execplan_workspace_identity=effects.workspace.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.workspace.validate_managed_worktree,
        _completed_autostash_correction=callbacks["_completed_autostash_correction"],
        _current_rebase_service=callbacks["_current_rebase_service"],
        _rebase_journal_state=callbacks["_rebase_journal_state"],
        _row=callbacks["_row"],
    ))


def rebase(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> RebaseHandler:
    return RebaseHandler(RebaseHandlerPorts(
        identity=identity,
        snapshot=snapshot,
        checkpoint=effects.checkpoint,
        contract=ExecutionContract.from_source(effects.contract),
        state=effects.state,
        rebase_corrections=rebase_corrections,
        _rebase_in_progress_for_branch=effects.workspace._rebase_in_progress_for_branch,
        branch_has_changes=effects.workspace.branch_has_changes,
        capture_git_snapshot=effects.agent.capture_git_snapshot,
        current_rebase_service=getattr(effects.workspace, "current_rebase_service", None),
        git=effects.workspace.git,
        is_directory=effects.workspace.is_directory,
        path_signature=effects.agent.path_signature,
        pr_for_branch=effects.publication.pr_for_branch,
        validate_managed_worktree=effects.workspace.validate_managed_worktree,
        _begin_rebase_continuation=callbacks["_begin_rebase_continuation"],
        _checkpoint_remote_oid=callbacks["_checkpoint_remote_oid"],
        _complete_rebase_intent=callbacks["_complete_rebase_intent"],
        _current_rebase_service=callbacks["_current_rebase_service"],
        _finish_rebase_journal=callbacks["_finish_rebase_journal"],
        _legacy_current_rebase=callbacks["_legacy_current_rebase"],
        _owned_checkpoint_remote=callbacks["_owned_checkpoint_remote"],
        _publish_rebased_checkpoint=callbacks["_publish_rebased_checkpoint"],
        _rebase_conflict=callbacks["_rebase_conflict"],
        _rebase_journal_state=callbacks["_rebase_journal_state"],
        _rebase_metadata=callbacks["_rebase_metadata"],
        _rebase_stage_evidence=callbacks["_rebase_stage_evidence"],
        _recover=callbacks["_recover"],
        _row=callbacks["_row"],
        _stage_evidence_preserved=callbacks["_stage_evidence_preserved"],
        _unmerged_stages=callbacks["_unmerged_stages"],
        git_bytes=getattr(effects.workspace, "git_bytes", None),
    ))


def workspace(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> WorkspaceHandler:
    return WorkspaceHandler(WorkspaceHandlerPorts(
        reconcile_remote=effects.workspace.reconcile_remote,
        identity=identity,
        snapshot=snapshot,
        update_snapshot=update_snapshot,
        checkpoint=effects.checkpoint,
        contract=ExecutionContract.from_source(effects.contract),
        state=effects.state,
        _plan_id_from_path=effects.workspace._plan_id_from_path,
        _rebase_in_progress_for_branch=effects.workspace._rebase_in_progress_for_branch,
        ensure_worker_level_state=_legacy_plan_callback(effects.validation.ensure_worker_level_state),
        git=effects.workspace.git,
        git_bytes=getattr(effects.workspace, "git_bytes", None),
        git_operation_entries=getattr(effects.workspace, "git_operation_entries", None),
        make_worktree=effects.workspace.make_worktree,
        mark_execplan_workspace_owned=effects.workspace.mark_execplan_workspace_owned,
        operation_print=effects.events.operation_print,
        pr_for_branch=effects.publication.pr_for_branch,
        reserve_execplan_workspace=effects.workspace.reserve_execplan_workspace,
        resume_pr_conflict_rebase=effects.workspace.resume_pr_conflict_rebase,
        validate_execplan_workspace_identity=effects.workspace.validate_execplan_workspace_identity,
        _block_worker_decision=callbacks["_block_worker_decision"],
        _cached_plan=callbacks["_cached_plan"],
        _checkpoint_remote_oid=callbacks["_checkpoint_remote_oid"],
        _commit_rebase_resolution=callbacks["_commit_rebase_resolution"],
        _complete_rebase_intent=callbacks["_complete_rebase_intent"],
        _context=callbacks["_context"],
        _current_rebase_service=callbacks["_current_rebase_service"],
        _execute_rebase_step=callbacks["_execute_rebase_step"],
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _final=callbacks["_final"],
        _finish_rebase_journal=callbacks["_finish_rebase_journal"],
        _finish_worker=callbacks["_finish_worker"],
        _guard_rebase_worker_result=callbacks["_guard_rebase_worker_result"],
        _legacy_current_rebase=callbacks["_legacy_current_rebase"],
        _owned_checkpoint_remote=callbacks["_owned_checkpoint_remote"],
        _parse_worker_outcome=callbacks["_parse_worker_outcome"],
        _post_rebase_observation=callbacks["_post_rebase_observation"],
        _publish_rebased_checkpoint=callbacks["_publish_rebased_checkpoint"],
        _rebase_observation=callbacks["_rebase_observation"],
        _record_rebase_intent=callbacks["_record_rebase_intent"],
        _recover=callbacks["_recover"],
        _resolving_rebase=callbacks["_resolving_rebase"],
        _row=callbacks["_row"],
        _run_managed_rebase=callbacks["_run_managed_rebase"],
        _stage_rebase_correction=callbacks["_stage_rebase_correction"],
    ))


def revision(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> RevisionHandler:
    return RevisionHandler(RevisionHandlerPorts(
        refresh_documents=effects.revision.refresh_documents,
        planning_required=effects.revision.planning_required,
        observe_verify_inputs=effects.validation.observe_verify_inputs,
        identity=identity,
        snapshot=snapshot,
        update_snapshot=update_snapshot,
        checkpoint=effects.checkpoint,
        contract=ExecutionContract.from_source(effects.contract),
        state=effects.state,
        _rebase_in_progress_for_branch=effects.workspace._rebase_in_progress_for_branch,
        body_hash=effects.revision.body_hash,
        branch_has_changes=effects.workspace.branch_has_changes,
        changed_paths=effects.revision.changed_paths,
        ensure_worker_level_state=_legacy_plan_callback(effects.validation.ensure_worker_level_state),
        git=effects.workspace.git,
        operation_log=effects.events.operation_log,
        operation_print=effects.events.operation_print,
        parse_worker_result=_legacy_plan_callback(effects.validation.parse_worker_result),
        reconcile_catalog=effects.revision.reconcile_catalog,
        record_remote_publication_branch=effects.publication.record_remote_publication_branch,
        source_fingerprint=effects.revision.source_fingerprint,
        unmet_dependencies=effects.revision.unmet_dependencies,
        validate_active_execplan_metadata=effects.validation.validate_active_execplan_metadata,
        _block_final=callbacks["_block_final"],
        _block_worker_decision=callbacks["_block_worker_decision"],
        _cached_plan=callbacks["_cached_plan"],
        _checkpoint_remote_oid=callbacks["_checkpoint_remote_oid"],
        _commit_owned=callbacks["_commit_owned"],
        _complete_rebase_intent=callbacks["_complete_rebase_intent"],
        _context=callbacks["_context"],
        _document_draft=callbacks["_document_draft"],
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _final=callbacks["_final"],
        _parse_worker_outcome=callbacks["_parse_worker_outcome"],
        _post_rebase_observation=callbacks["_post_rebase_observation"],
        _publish_rebased_checkpoint=callbacks["_publish_rebased_checkpoint"],
        _reconcile_terminal_publication=callbacks["_reconcile_terminal_publication"],
        _record_rebase_intent=callbacks["_record_rebase_intent"],
        _recover=callbacks["_recover"],
        _refresh_revision_base=callbacks["_refresh_revision_base"],
        _retry=callbacks["_retry"],
        _row=callbacks["_row"],
        _run_managed_rebase=callbacks["_run_managed_rebase"],
        guard=callbacks["guard"],
    ))


def archive(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> ArchiveHandler:
    return ArchiveHandler(ArchiveHandlerPorts(
        identity=identity,
        snapshot=snapshot,
        contract=ExecutionContract.from_source(effects.contract),
        state=effects.state,
        git=effects.workspace.git,
        requirement_archive_candidates=effects.archive.requirement_archive_candidates,
        requirement_archive_snapshot=effects.archive.requirement_archive_snapshot,
        validate_managed_worktree=effects.workspace.validate_managed_worktree,
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _recover=callbacks["_recover"],
    ))


def worker(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> WorkerHandler:
    return WorkerHandler(WorkerHandlerPorts(
        identity=identity,
        snapshot=snapshot,
        update_snapshot=update_snapshot,
        checkpoint=effects.checkpoint,
        contract=ExecutionContract.from_source(effects.contract),
        feedback=effects.feedback,
        state=effects.state,
        rebase_corrections=rebase_corrections,
        _published_plan_allows_common_plans=effects.agent._published_plan_allows_common_plans,
        _rebase_in_progress_for_branch=effects.workspace._rebase_in_progress_for_branch,
        _revision_execplan_input_fingerprint=effects.agent._revision_execplan_input_fingerprint,
        begin_agent_scope=effects.agent.begin_agent_scope,
        block_verify_timeout_limit=effects.validation.block_verify_timeout_limit,
        branch_has_changes=effects.workspace.branch_has_changes,
        clear_agent_scope=effects.agent.clear_agent_scope,
        current_rebase_service=getattr(effects.workspace, "current_rebase_service", None),
        git=effects.workspace.git,
        is_loop_manager_repository=effects.agent.is_loop_manager_repository,
        operation_log=effects.events.operation_log,
        operation_print=effects.events.operation_print,
        parse_worker_result=_legacy_plan_callback(effects.validation.parse_worker_result),
        requirement_archive_move_set=effects.archive.requirement_archive_move_set,
        requirement_archive_reconciliation_error=effects.archive.requirement_archive_reconciliation_error,
        resolve_worker_profile=effects.agent.resolve_worker_profile,
        run_agent=effects.agent.run_agent,
        run_agent_with_retries=effects.agent.run_agent_with_retries,
        validate_worker_workspace_scope=effects.agent.validate_worker_workspace_scope,
        worker_prompt=effects.agent.worker_prompt,
        _advanced=callbacks["_advanced"],
        guard=callbacks["guard"],
        _archive_candidates_at=callbacks["_archive_candidates_at"],
        _archive_collision=callbacks["_archive_collision"],
        _block_final=callbacks["_block_final"],
        _commit_rebase_resolution=callbacks["_commit_rebase_resolution"],
        _complete_rebase_intent=callbacks["_complete_rebase_intent"],
        _current_rebase_service=callbacks["_current_rebase_service"],
        _document_draft=callbacks["_document_draft"],
        _execute_rebase_step=callbacks["_execute_rebase_step"],
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _final=callbacks["_final"],
        _finish_worker=callbacks["_finish_worker"],
        _metadata_correction=callbacks["_metadata_correction"],
        _post_effect_guard=callbacks["_post_effect_guard"],
        _post_rebase_observation=callbacks["_post_rebase_observation"],
        _rebase_conflict=callbacks["_rebase_conflict"],
        _rebase_observation=callbacks["_rebase_observation"],
        _record_archive_target=callbacks["_record_archive_target"],
        _record_rebase_suboperation=callbacks["_record_rebase_suboperation"],
        _recover=callbacks["_recover"],
        _retry=callbacks["_retry"],
        _row=callbacks["_row"],
        _run_document=callbacks["_run_document"],
        _stage_rebase_correction=callbacks["_stage_rebase_correction"],
        _unmerged_stages=callbacks["_unmerged_stages"],
    ))


def verify(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> VerifyHandler:
    return VerifyHandler(VerifyHandlerPorts(
        checkpoint=effects.checkpoint,
        partial_verify_enabled=effects.validation.partial_verify_enabled,
        partial_verify=effects.validation.partial_verify,
        pre_review_identity=effects.validation.pre_review_identity,
        observe_verify_inputs=effects.validation.observe_verify_inputs,
        identity=identity,
        snapshot=snapshot,
        update_snapshot=update_snapshot,
        contract=ExecutionContract.from_source(effects.contract),
        feedback=effects.feedback,
        state=effects.state,
        _worktree_execplan_input_fingerprint=effects.agent._worktree_execplan_input_fingerprint,
        begin_agent_scope=effects.agent.begin_agent_scope,
        branch_has_changes=effects.workspace.branch_has_changes,
        clear_agent_scope=effects.agent.clear_agent_scope,
        clear_validation_failure_history=effects.validation.clear_validation_failure_history,
        git=effects.workspace.git,
        operation_log=effects.events.operation_log,
        operation_print=effects.events.operation_print,
        parse_worker_result=_legacy_plan_callback(effects.validation.parse_worker_result),
        transition_validation_failure=effects.validation.transition_validation_failure,
        validate_active_execplan_metadata=effects.validation.validate_active_execplan_metadata,
        validate_worker_workspace_scope=effects.agent.validate_worker_workspace_scope,
        verify=effects.validation.verify,
        _advanced=callbacks["_advanced"],
        _block_final=callbacks["_block_final"],
        _block_worker_decision=callbacks["_block_worker_decision"],
        _commit_owned=callbacks["_commit_owned"],
        _context=callbacks["_context"],
        _document_block=callbacks["_document_block"],
        _document_draft=callbacks["_document_draft"],
        _document_guard=callbacks["_document_guard"],
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _final=callbacks["_final"],
        _metadata_correction=callbacks["_metadata_correction"],
        _parse_worker_outcome=callbacks["_parse_worker_outcome"],
        _post_effect_guard=callbacks["_post_effect_guard"],
        _recover=callbacks["_recover"],
        _retry=callbacks["_retry"],
        _row=callbacks["_row"],
    ))


def review(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> ReviewHandler:
    return ReviewHandler(ReviewHandlerPorts(
        pre_review_identity=effects.validation.pre_review_identity,
        checkpoint=effects.checkpoint,
        identity=identity,
        snapshot=snapshot,
        contract=ExecutionContract.from_source(effects.contract),
        feedback=effects.feedback,
        state=effects.state,
        _worktree_execplan_input_fingerprint=effects.agent._worktree_execplan_input_fingerprint,
        begin_agent_scope=effects.agent.begin_agent_scope,
        capture_git_snapshot=effects.agent.capture_git_snapshot,
        clear_agent_scope=effects.agent.clear_agent_scope,
        clear_validation_failure_history=effects.validation.clear_validation_failure_history,
        git=effects.workspace.git,
        operation_log=effects.events.operation_log,
        operation_print=effects.events.operation_print,
        parse_reviewer_result=effects.validation.parse_reviewer_result,
        parse_worker_result=_legacy_plan_callback(effects.validation.parse_worker_result),
        reviewer_prompt=effects.agent.reviewer_prompt,
        run_agent=effects.agent.run_agent,
        run_agent_with_retries=effects.agent.run_agent_with_retries,
        transition_validation_failure=effects.validation.transition_validation_failure,
        validate_agent_scope=effects.agent.validate_agent_scope,
        validate_managed_worktree=effects.workspace.validate_managed_worktree,
        _advanced=callbacks["_advanced"],
        _block_final=callbacks["_block_final"],
        _document_block=callbacks["_document_block"],
        _document_draft=callbacks["_document_draft"],
        _document_guard=callbacks["_document_guard"],
        _document_review_prompt=callbacks["_document_review_prompt"],
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _final=callbacks["_final"],
        _latest_before_verify=callbacks["_latest_before_verify"],
        _post_effect_guard=callbacks["_post_effect_guard"],
        _recover=callbacks["_recover"],
        _retry=callbacks["_retry"],
        _row=callbacks["_row"],
        observe_verify_inputs=effects.validation.observe_verify_inputs,
    ))


def latest_base(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> LatestBaseHandler:
    return LatestBaseHandler(LatestBaseHandlerPorts(
        identity=identity,
        snapshot=snapshot,
        contract=ExecutionContract.from_source(effects.contract),
        state=effects.state,
        _rebase_in_progress_for_branch=effects.workspace._rebase_in_progress_for_branch,
        branch_has_changes=effects.workspace.branch_has_changes,
        git=effects.workspace.git,
        operation_print=effects.events.operation_print,
        record_remote_publication_branch=effects.publication.record_remote_publication_branch,
        _block_final=callbacks["_block_final"],
        _checkpoint_remote_oid=callbacks["_checkpoint_remote_oid"],
        _complete_rebase_intent=callbacks["_complete_rebase_intent"],
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _final=callbacks["_final"],
        _latest_before_publication=callbacks["_latest_before_publication"],
        _post_rebase_observation=callbacks["_post_rebase_observation"],
        _rebase_latest=callbacks["_rebase_latest"],
        _record_rebase_intent=callbacks["_record_rebase_intent"],
        _recover=callbacks["_recover"],
        _retry=callbacks["_retry"],
        _row=callbacks["_row"],
        _run_managed_rebase=callbacks["_run_managed_rebase"],
    ))


def publication(effects: ExecutionEffects, identity: ExecutionIdentity,
        snapshot: Callable[[], PreparationSnapshot], update_snapshot: Callable[..., None],
        callbacks: dict[str, Callable], *, recovery=None, rebase_corrections=None) -> PublicationHandler:
    return PublicationHandler(PublicationHandlerPorts(
        update_snapshot=callbacks.get('_update_snapshot'),
        observe_verify_inputs=effects.validation.observe_verify_inputs,
        pre_review_identity=effects.validation.pre_review_identity,
        identity=identity,
        snapshot=snapshot,
        checkpoint=effects.checkpoint,
        contract=ExecutionContract.from_source(effects.contract),
        state=effects.state,
        branch_has_changes=effects.workspace.branch_has_changes,
        build_pr_body=_legacy_plan_callback(effects.publication.build_pr_body),
        command=effects.publication.command,
        git=effects.workspace.git,
        live_remote_branch_oid=getattr(effects.workspace, "live_remote_branch_oid", None),
        operation_print=effects.events.operation_print,
        parse_worker_result=_legacy_plan_callback(effects.validation.parse_worker_result),
        pr_for_branch=effects.publication.pr_for_branch,
        published_execplan=effects.publication.published_execplan,
        push_managed_publication_branch=effects.publication.push_managed_publication_branch,
        push_owned_pr_conflict_recovery=effects.publication.push_owned_pr_conflict_recovery,
        reconcile_existing_pull_request=effects.publication.reconcile_existing_pull_request,
        record_remote_publication_branch=effects.publication.record_remote_publication_branch,
        requirement_archive_move_set=effects.archive.requirement_archive_move_set,
        requirement_archive_reconciliation_error=effects.archive.requirement_archive_reconciliation_error,
        validate_execplan_workspace_identity=effects.workspace.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.workspace.validate_managed_worktree,
        _archive_candidates_at=callbacks["_archive_candidates_at"],
        _archive_collision=callbacks["_archive_collision"],
        _block_final=callbacks["_block_final"],
        _context=callbacks["_context"],
        _document_block=callbacks["_document_block"],
        _document_draft=callbacks["_document_draft"],
        _document_guard=callbacks["_document_guard"],
        _fetch_latest_base=callbacks["_fetch_latest_base"],
        _final=callbacks["_final"],
        _latest_before_publication=callbacks["_latest_before_publication"],
        _owned_checkpoint_remote=callbacks["_owned_checkpoint_remote"],
        _post_effect_guard=callbacks["_post_effect_guard"],
        _record_archive_target=callbacks["_record_archive_target"],
        _recover=callbacks["_recover"],
        _retry=callbacks["_retry"],
        _row=callbacks["_row"],
        _step_outcome=callbacks["_step_outcome"],
    ))
