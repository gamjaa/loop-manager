"""Compatibility facade: immutable preparation state, composition and phase delegation."""
from __future__ import annotations
from dataclasses import replace
from types import MappingProxyType
from loop_manager_internal.execution import execution_assembly
from loop_manager_internal.execution.execution_effects import (
    ExecutionEffects, ExecutionContract, WorkspaceEffects, AgentEffects, ValidationEffects,
    FeedbackEffects, ArchiveEffects, PublicationEffects, BlockingEffects, EventsEffects, RevisionEffects,
)
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome
from loop_manager_internal.execution.worker_outcome import WorkerOutcome
from loop_manager_internal.recovery.recovery_dispatcher import RecoveryDispatcher
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.rebase_correction_state import RebaseCorrectionRepository
# Preserve module patch points consumed by the existing recovery fixtures.
from loop_manager_internal.git import manager_git_journal, managed_rebase_evidence
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.documents import document_drafts, document_publication
from loop_manager_internal.storage import recovery_state

class ExecutionHandlers:
    """Delegate the coordinator's existing contract to independently scoped handlers."""
    def __init__(self, effects: ExecutionEffects, *, repo, repo_path, state_dir, base,
                 base_sha, provider, config_path, newly_activated):
        self.effects = effects
        self._identity = ExecutionIdentity(repo, repo_path, state_dir, base, base_sha,
                                           provider, config_path, newly_activated)
        self._preparation = PreparationSnapshot()
        self.recovery = RecoveryDispatcher(effects.state.connection,
            effects.blocking.block_active_execplan, effects.blocking.block_active_execplan_from_revision)
        self.rebase_corrections = RebaseCorrectionRepository(effects.state.connection)

    def _snapshot(self) -> PreparationSnapshot:
        return getattr(self, '_preparation', PreparationSnapshot())

    def _update_snapshot(self, **changes: object) -> None:
        for field in ('plan', 'catalog_plan'):
            if changes.get(field) is not None:
                changes[field] = MappingProxyType({
                    key: tuple(value) if isinstance(value, list) else value
                    for key, value in dict(changes[field]).items()
                })
        self._preparation = replace(self._snapshot(), **changes)

    @property
    def repo(self):
        return getattr(self, '_identity', ExecutionIdentity()).repo

    @repo.setter
    def repo(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), repo=value)

    @property
    def repo_path(self):
        return getattr(self, '_identity', ExecutionIdentity()).repo_path

    @repo_path.setter
    def repo_path(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), repo_path=value)

    @property
    def state_dir(self):
        return getattr(self, '_identity', ExecutionIdentity()).state_dir

    @state_dir.setter
    def state_dir(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), state_dir=value)

    @property
    def base(self):
        return getattr(self, '_identity', ExecutionIdentity()).base

    @base.setter
    def base(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), base=value)

    @property
    def base_sha(self):
        return getattr(self, '_identity', ExecutionIdentity()).base_sha

    @base_sha.setter
    def base_sha(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), base_sha=value)

    @property
    def provider(self):
        return getattr(self, '_identity', ExecutionIdentity()).provider

    @provider.setter
    def provider(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), provider=value)

    @property
    def config_path(self):
        return getattr(self, '_identity', ExecutionIdentity()).config_path

    @config_path.setter
    def config_path(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), config_path=value)

    @property
    def newly_activated(self):
        return getattr(self, '_identity', ExecutionIdentity()).newly_activated

    @newly_activated.setter
    def newly_activated(self, value):
        self._identity = replace(getattr(self, '_identity', ExecutionIdentity()), newly_activated=value)

    @property
    def catalog_plan(self):
        return self._snapshot().catalog_plan

    @catalog_plan.setter
    def catalog_plan(self, value):
        self._update_snapshot(catalog_plan=value)

    @property
    def plan(self):
        return self._snapshot().plan

    @plan.setter
    def plan(self, value):
        self._update_snapshot(plan=value)

    @property
    def level(self):
        return self._snapshot().level

    @level.setter
    def level(self, value):
        self._update_snapshot(level=value)

    @property
    def archive_sources(self):
        return self._snapshot().archive_sources

    @archive_sources.setter
    def archive_sources(self, value):
        self._update_snapshot(archive_sources=value)

    @property
    def archive_candidates(self):
        return self._snapshot().archive_candidates

    @archive_candidates.setter
    def archive_candidates(self, value):
        self._update_snapshot(archive_candidates=value)

    @property
    def archive_scope_sources(self):
        return self._snapshot().archive_scope_sources

    @archive_scope_sources.setter
    def archive_scope_sources(self, value):
        self._update_snapshot(archive_scope_sources=value)

    @property
    def _archive_snapshot(self):
        return self._snapshot()._archive_snapshot

    @_archive_snapshot.setter
    def _archive_snapshot(self, value):
        self._update_snapshot(_archive_snapshot=value)

    @property
    def _latest_verify_output(self):
        return self._snapshot()._latest_verify_output

    @_latest_verify_output.setter
    def _latest_verify_output(self, value):
        self._update_snapshot(_latest_verify_output=value)

    @property
    def _definition_snapshot(self):
        return self._snapshot()._definition_snapshot

    @_definition_snapshot.setter
    def _definition_snapshot(self, value):
        self._update_snapshot(_definition_snapshot=value)

    def _context_handler(self):
        return execution_assembly.context(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_resolving_rebase": lambda *a, **kw: self._resolving_rebase(*a, **kw),
            },
        )

    def _row(self):
        return self._context_handler()._row()

    def _context(self):
        return self._context_handler()._context()

    @staticmethod
    def _cached_plan(catalog_plan):
        return execution_assembly.ContextHandler._cached_plan(catalog_plan)

    def _advanced(self):
        return self._context_handler()._advanced()

    def work_started(self, context):
        return self._context_handler().work_started(context)

    def _recovery_handler(self):
        return execution_assembly.recovery(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_row": lambda *a, **kw: self._row(*a, **kw),
            },
            recovery=self.recovery,
        )

    def recover_failure(self, context, error):
        return self._recovery_handler().recover_failure(context, error)

    @staticmethod
    def _final(detail=None):
        return execution_assembly.RecoveryHandler._final(detail)

    @staticmethod
    def _retry(detail=None):
        return execution_assembly.RecoveryHandler._retry(detail)

    def _recover(self, reason, detail, *, context=None, fingerprint=None, progress_marker=None, final_block_reason=None, from_revision=False, correction_limit=None, attempts=0, user_targets=(), verify_result=None, worker_result=None, worker_decision=None):
        return self._recovery_handler()._recover(reason, detail, context=context, fingerprint=fingerprint, progress_marker=progress_marker, final_block_reason=final_block_reason, from_revision=from_revision, correction_limit=correction_limit, attempts=attempts, user_targets=user_targets, verify_result=verify_result, worker_result=worker_result, worker_decision=worker_decision)

    def _block_final(self, repo, worktree, detail, verify_result, *, reason, recovery_reason=None, user_targets=()):
        return self._recovery_handler()._block_final(repo, worktree, detail, verify_result, reason=reason, recovery_reason=recovery_reason, user_targets=user_targets)

    def _block_from_revision_final(self, repo_path, repo, revision, detail, verify_result='', *, reason):
        return self._recovery_handler()._block_from_revision_final(repo_path, repo, revision, detail, verify_result, reason=reason)

    def _step_outcome(self, step_result):
        return self._recovery_handler()._step_outcome(step_result)

    def _documents_handler(self):
        return execution_assembly.documents(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_block_final": lambda *a, **kw: self._block_final(*a, **kw),
                "_document_block": lambda *a, **kw: self._document_block(*a, **kw),
                "_finish_worker": lambda *a, **kw: self._finish_worker(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
            },
        )

    def _document_draft(self, context):
        return self._documents_handler()._document_draft(context)

    def _document_block(self, context, draft, error):
        return self._documents_handler()._document_block(context, draft, error)

    def _document_guard(self, context, draft):
        return self._documents_handler()._document_guard(context, draft)

    def _document_review_prompt(self, base_prompt, draft):
        return self._documents_handler()._document_review_prompt(base_prompt, draft)

    def _run_document(self, context, draft):
        return self._documents_handler()._run_document(context, draft)

    def _ownership_handler(self):
        return execution_assembly.ownership(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_completed_autostash_correction": lambda *a, **kw: self._completed_autostash_correction(*a, **kw),
                "_current_rebase_service": lambda *a, **kw: self._current_rebase_service(*a, **kw),
                "_rebase_journal_state": lambda *a, **kw: self._rebase_journal_state(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
            },
        )

    def _owned_checkpoint_remote(self, context, row, remote_oid, current, pull_request=None, **evidence):
        return self._ownership_handler()._owned_checkpoint_remote(context, row, remote_oid, current, pull_request, **evidence)

    def _owned_published_review_resume(self, context, row, remote_oid, current, checkpoint, marker, pull_request):
        return self._ownership_handler()._owned_published_review_resume(context, row, remote_oid, current, checkpoint, marker, pull_request)

    def _current_rebase_service(self, context, **options):
        return self._ownership_handler()._current_rebase_service(context, **options)

    def _checkpoint_remote_oid(self, repo_path, branch):
        return self._ownership_handler()._checkpoint_remote_oid(repo_path, branch)

    def _owned_current_checkpoint_rebase(self, context, row, remote_oid, current, checkpoint, pull_request):
        return self._ownership_handler()._owned_current_checkpoint_rebase(context, row, remote_oid, current, checkpoint, pull_request)

    def _owned_detached_rebase(self, context, row, remote_oid, current, checkpoint, pull_request):
        return self._ownership_handler()._owned_detached_rebase(context, row, remote_oid, current, checkpoint, pull_request)

    @staticmethod
    def _rebase_completion_session(context, current):
        return execution_assembly.OwnershipHandler._rebase_completion_session(context, current)

    def _matching_rebase_journal_result(self, context, current, remote_oid, observed, *, allow_later_file_changes=False):
        return self._ownership_handler()._matching_rebase_journal_result(context, current, remote_oid, observed, allow_later_file_changes=allow_later_file_changes)

    def _owned_persisted_rebase(self, context, row, remote_oid, current, checkpoint, pull_request):
        return self._ownership_handler()._owned_persisted_rebase(context, row, remote_oid, current, checkpoint, pull_request)

    def _completed_rebase_proof(self, context, current, remote_oid, observed):
        return self._ownership_handler()._completed_rebase_proof(context, current, remote_oid, observed)

    def _owned_completed_rebase(self, context, row, remote_oid, current, checkpoint, pull_request, local_head):
        return self._ownership_handler()._owned_completed_rebase(context, row, remote_oid, current, checkpoint, pull_request, local_head)

    def _legacy_current_rebase(self, context, current):
        return self._ownership_handler()._legacy_current_rebase(context, current)

    def _rebase_handler(self):
        return execution_assembly.rebase(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_begin_rebase_continuation": lambda *a, **kw: self._begin_rebase_continuation(*a, **kw),
                "_checkpoint_remote_oid": lambda *a, **kw: self._checkpoint_remote_oid(*a, **kw),
                "_complete_rebase_intent": lambda *a, **kw: self._complete_rebase_intent(*a, **kw),
                "_current_rebase_service": lambda *a, **kw: self._current_rebase_service(*a, **kw),
                "_finish_rebase_journal": lambda *a, **kw: self._finish_rebase_journal(*a, **kw),
                "_legacy_current_rebase": lambda *a, **kw: self._legacy_current_rebase(*a, **kw),
                "_owned_checkpoint_remote": lambda *a, **kw: self._owned_checkpoint_remote(*a, **kw),
                "_publish_rebased_checkpoint": lambda *a, **kw: self._publish_rebased_checkpoint(*a, **kw),
                "_rebase_conflict": lambda *a, **kw: self._rebase_conflict(*a, **kw),
                "_rebase_journal_state": lambda *a, **kw: self._rebase_journal_state(*a, **kw),
                "_rebase_metadata": lambda *a, **kw: self._rebase_metadata(*a, **kw),
                "_rebase_stage_evidence": lambda *a, **kw: self._rebase_stage_evidence(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
                "_stage_evidence_preserved": lambda *a, **kw: self._stage_evidence_preserved(*a, **kw),
                "_unmerged_stages": lambda *a, **kw: self._unmerged_stages(*a, **kw),
            },
            rebase_corrections=self.rebase_corrections,
        )

    def _resolving_rebase(self, row):
        return self._rebase_handler()._resolving_rebase(row)

    def _unmerged_stages(self, worktree):
        return self._rebase_handler()._unmerged_stages(worktree)

    def _rebase_observation(self, context, *, target=None, stash_oid=None):
        return self._rebase_handler()._rebase_observation(context, target=target, stash_oid=stash_oid)

    def _rebase_conflict(self, context, detail, *, corrected=False, worker_result=None, abnormal_result=False, target=None, stash_oid=None, recovery_reason=RecoveryReason.REBASE_CONFLICT):
        return self._rebase_handler()._rebase_conflict(context, detail, corrected=corrected, worker_result=worker_result, abnormal_result=abnormal_result, target=target, stash_oid=stash_oid, recovery_reason=recovery_reason)

    def _record_rebase_intent(self, context, target_oid):
        return self._rebase_handler()._record_rebase_intent(context, target_oid)

    def _rebase_metadata(self, context):
        return self._rebase_handler()._rebase_metadata(context)

    def _rebase_journal_state(self, context):
        return self._rebase_handler()._rebase_journal_state(context)

    def _begin_rebase_continuation(self, context, action):
        return self._rebase_handler()._begin_rebase_continuation(context, action)

    def _execute_rebase_step(self, context, action):
        return self._rebase_handler()._execute_rebase_step(context, action)

    def _rebase_journal_checks(self, context):
        return self._rebase_handler()._rebase_journal_checks(context)

    def _finish_rebase_journal(self, context, *, allow_equal=False):
        return self._rebase_handler()._finish_rebase_journal(context, allow_equal=allow_equal)

    def _run_managed_rebase(self, context, target_oid, *, autostash=False):
        return self._rebase_handler()._run_managed_rebase(context, target_oid, autostash=autostash)

    def _record_rebase_suboperation(self, context, pending, *, worker_result=None):
        return self._rebase_handler()._record_rebase_suboperation(context, pending, worker_result=worker_result)

    def _rebase_stage_evidence(self, context):
        return self._rebase_handler()._rebase_stage_evidence(context)

    def _stage_rebase_correction(self, context):
        return self._rebase_handler()._stage_rebase_correction(context)

    def _stage_evidence_preserved(self, context, accepted, observed, tracked_paths):
        return self._rebase_handler()._stage_evidence_preserved(context, accepted, observed, tracked_paths)

    def _post_rebase_observation(self, context, target_oid, output, *, corrected=False, worker_result=None):
        return self._rebase_handler()._post_rebase_observation(context, target_oid, output, corrected=corrected, worker_result=worker_result)

    def _completed_autostash_correction(self, context, intent, sealed, live, *, rejection=None):
        return self._rebase_handler()._completed_autostash_correction(context, intent, sealed, live, rejection=rejection)

    def _complete_rebase_intent(self, context, *, project_values=None):
        return self._rebase_handler()._complete_rebase_intent(context, project_values=project_values)

    def _publish_rebased_checkpoint(self, context):
        return self._rebase_handler()._publish_rebased_checkpoint(context)

    def _workspace_handler(self):
        return execution_assembly.workspace(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_block_worker_decision": lambda *a, **kw: self._block_worker_decision(*a, **kw),
                "_cached_plan": lambda *a, **kw: self._cached_plan(*a, **kw),
                "_checkpoint_remote_oid": lambda *a, **kw: self._checkpoint_remote_oid(*a, **kw),
                "_commit_rebase_resolution": lambda *a, **kw: self._commit_rebase_resolution(*a, **kw),
                "_complete_rebase_intent": lambda *a, **kw: self._complete_rebase_intent(*a, **kw),
                "_context": lambda *a, **kw: self._context(*a, **kw),
                "_current_rebase_service": lambda *a, **kw: self._current_rebase_service(*a, **kw),
                "_execute_rebase_step": lambda *a, **kw: self._execute_rebase_step(*a, **kw),
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_final": lambda *a, **kw: self._final(*a, **kw),
                "_finish_rebase_journal": lambda *a, **kw: self._finish_rebase_journal(*a, **kw),
                "_finish_worker": lambda *a, **kw: self._finish_worker(*a, **kw),
                "_legacy_current_rebase": lambda *a, **kw: self._legacy_current_rebase(*a, **kw),
                "_owned_checkpoint_remote": lambda *a, **kw: self._owned_checkpoint_remote(*a, **kw),
                "_parse_worker_outcome": lambda *a, **kw: self._parse_worker_outcome(*a, **kw),
                "_post_rebase_observation": lambda *a, **kw: self._post_rebase_observation(*a, **kw),
                "_publish_rebased_checkpoint": lambda *a, **kw: self._publish_rebased_checkpoint(*a, **kw),
                "_rebase_observation": lambda *a, **kw: self._rebase_observation(*a, **kw),
                "_record_rebase_intent": lambda *a, **kw: self._record_rebase_intent(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_resolving_rebase": lambda *a, **kw: self._resolving_rebase(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
                "_run_managed_rebase": lambda *a, **kw: self._run_managed_rebase(*a, **kw),
                "_stage_rebase_correction": lambda *a, **kw: self._stage_rebase_correction(*a, **kw),
            },
        )

    def _workspace_boundary(self, plan_id, row, *, pending):
        return self._workspace_handler()._workspace_boundary(plan_id, row, pending=pending)

    def recover(self, context):
        return self._workspace_handler().recover(context)

    def prepare(self):
        return self._workspace_handler().prepare()

    def _revision_handler(self):
        return execution_assembly.revision(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_block_final": lambda *a, **kw: self._block_final(*a, **kw),
                "_block_worker_decision": lambda *a, **kw: self._block_worker_decision(*a, **kw),
                "_cached_plan": lambda *a, **kw: self._cached_plan(*a, **kw),
                "_checkpoint_remote_oid": lambda *a, **kw: self._checkpoint_remote_oid(*a, **kw),
                "_commit_owned": lambda *a, **kw: self._commit_owned(*a, **kw),
                "_complete_rebase_intent": lambda *a, **kw: self._complete_rebase_intent(*a, **kw),
                "_context": lambda *a, **kw: self._context(*a, **kw),
                "_document_draft": lambda *a, **kw: self._document_draft(*a, **kw),
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_final": lambda *a, **kw: self._final(*a, **kw),
                "_parse_worker_outcome": lambda *a, **kw: self._parse_worker_outcome(*a, **kw),
                "_post_rebase_observation": lambda *a, **kw: self._post_rebase_observation(*a, **kw),
                "_publish_rebased_checkpoint": lambda *a, **kw: self._publish_rebased_checkpoint(*a, **kw),
                "_reconcile_terminal_publication": lambda *a, **kw: self._reconcile_terminal_publication(*a, **kw),
                "_record_rebase_intent": lambda *a, **kw: self._record_rebase_intent(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_refresh_revision_base": lambda *a, **kw: self._refresh_revision_base(*a, **kw),
                "_retry": lambda *a, **kw: self._retry(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
                "_run_managed_rebase": lambda *a, **kw: self._run_managed_rebase(*a, **kw),
                "guard": lambda *a, **kw: self.guard(*a, **kw),
            },
        )

    @staticmethod
    def _project_phase(status):
        return execution_assembly.RevisionHandler._project_phase(status)

    def _revision_state_values(self, snapshot, decision, latest_sha, *, legacy_baseline=False):
        return self._revision_handler()._revision_state_values(snapshot, decision, latest_sha, legacy_baseline=legacy_baseline)

    def _install_revision_state(self, snapshot, decision, latest_sha, *, legacy_baseline=False):
        return self._revision_handler()._install_revision_state(snapshot, decision, latest_sha, legacy_baseline=legacy_baseline)

    def _refresh_revision_base(self, context, snapshot, decision, latest_sha, *, legacy_baseline=False):
        return self._revision_handler()._refresh_revision_base(context, snapshot, decision, latest_sha, legacy_baseline=legacy_baseline)

    def _revision_error(self, context, detail):
        return self._revision_handler()._revision_error(context, detail)

    def guard(self, context):
        return self._revision_handler().guard(context)

    def _post_effect_guard(self, context):
        return self._revision_handler()._post_effect_guard(context)

    def _metadata_correction(self, context, error, *, verify_result=None):
        return self._revision_handler()._metadata_correction(context, error, verify_result=verify_result)

    def _archive_handler(self):
        return execution_assembly.archive(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
            },
        )

    def _archive_collision(self, context, error):
        return self._archive_handler()._archive_collision(context, error)

    def _archive_workspace_owned(self, context):
        return self._archive_handler()._archive_workspace_owned(context)

    def _archive_candidates_at(self, context, revision):
        return self._archive_handler()._archive_candidates_at(context, revision)

    def _record_archive_target(self, stage, *, moves=None):
        return self._archive_handler()._record_archive_target(stage, moves=moves)

    def _worker_handler(self):
        return execution_assembly.worker(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_advanced": lambda *a, **kw: self._advanced(*a, **kw),
                "_archive_candidates_at": lambda *a, **kw: self._archive_candidates_at(*a, **kw),
                "_archive_collision": lambda *a, **kw: self._archive_collision(*a, **kw),
                "_block_final": lambda *a, **kw: self._block_final(*a, **kw),
                "_commit_rebase_resolution": lambda *a, **kw: self._commit_rebase_resolution(*a, **kw),
                "_complete_rebase_intent": lambda *a, **kw: self._complete_rebase_intent(*a, **kw),
                "_current_rebase_service": lambda *a, **kw: self._current_rebase_service(*a, **kw),
                "_document_draft": lambda *a, **kw: self._document_draft(*a, **kw),
                "_execute_rebase_step": lambda *a, **kw: self._execute_rebase_step(*a, **kw),
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_final": lambda *a, **kw: self._final(*a, **kw),
                "_finish_worker": lambda *a, **kw: self._finish_worker(*a, **kw),
                "_metadata_correction": lambda *a, **kw: self._metadata_correction(*a, **kw),
                "_post_effect_guard": lambda *a, **kw: self._post_effect_guard(*a, **kw),
                "_post_rebase_observation": lambda *a, **kw: self._post_rebase_observation(*a, **kw),
                "_rebase_conflict": lambda *a, **kw: self._rebase_conflict(*a, **kw),
                "_rebase_observation": lambda *a, **kw: self._rebase_observation(*a, **kw),
                "_record_archive_target": lambda *a, **kw: self._record_archive_target(*a, **kw),
                "_record_rebase_suboperation": lambda *a, **kw: self._record_rebase_suboperation(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_retry": lambda *a, **kw: self._retry(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
                "_run_document": lambda *a, **kw: self._run_document(*a, **kw),
                "_stage_rebase_correction": lambda *a, **kw: self._stage_rebase_correction(*a, **kw),
                "_unmerged_stages": lambda *a, **kw: self._unmerged_stages(*a, **kw),
            },
            rebase_corrections=self.rebase_corrections,
        )

    def _parse_worker_outcome(self, context, message, *, allow_legacy_ready=False):
        return self._worker_handler()._parse_worker_outcome(context, message, allow_legacy_ready=allow_legacy_ready)

    def _block_worker_decision(self, context, message, outcome: WorkerOutcome):
        return self._worker_handler()._block_worker_decision(context, message, outcome)

    def run(self, context):
        return self._worker_handler().run(context)

    def _resume_saved_rebase_worker(self, context, saved, observation):
        return self._worker_handler()._resume_saved_rebase_worker(context, saved, observation)

    def _finish_worker(self, context, row, message, *, staged=False):
        return self._worker_handler()._finish_worker(context, row, message, staged=staged)

    def _verify_handler(self):
        return execution_assembly.verify(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_advanced": lambda *a, **kw: self._advanced(*a, **kw),
                "_block_final": lambda *a, **kw: self._block_final(*a, **kw),
                "_block_worker_decision": lambda *a, **kw: self._block_worker_decision(*a, **kw),
                "_commit_owned": lambda *a, **kw: self._commit_owned(*a, **kw),
                "_context": lambda *a, **kw: self._context(*a, **kw),
                "_document_block": lambda *a, **kw: self._document_block(*a, **kw),
                "_document_draft": lambda *a, **kw: self._document_draft(*a, **kw),
                "_document_guard": lambda *a, **kw: self._document_guard(*a, **kw),
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_final": lambda *a, **kw: self._final(*a, **kw),
                "_metadata_correction": lambda *a, **kw: self._metadata_correction(*a, **kw),
                "_parse_worker_outcome": lambda *a, **kw: self._parse_worker_outcome(*a, **kw),
                "_post_effect_guard": lambda *a, **kw: self._post_effect_guard(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_retry": lambda *a, **kw: self._retry(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
            },
        )

    def _verify_once(self, context, candidate_head=None):
        return self._verify_handler()._verify_once(context, candidate_head)

    def verify(self, context, latest_base):
        return self._verify_handler().verify(context, latest_base)

    def _finish_verified(self, context):
        return self._verify_handler()._finish_verified(context)

    def _review_handler(self):
        return execution_assembly.review(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_advanced": lambda *a, **kw: self._advanced(*a, **kw),
                "_block_final": lambda *a, **kw: self._block_final(*a, **kw),
                "_document_block": lambda *a, **kw: self._document_block(*a, **kw),
                "_document_draft": lambda *a, **kw: self._document_draft(*a, **kw),
                "_document_guard": lambda *a, **kw: self._document_guard(*a, **kw),
                "_document_review_prompt": lambda *a, **kw: self._document_review_prompt(*a, **kw),
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_final": lambda *a, **kw: self._final(*a, **kw),
                "_latest_before_verify": lambda *a, **kw: self._latest_before_verify(*a, **kw),
                "_post_effect_guard": lambda *a, **kw: self._post_effect_guard(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_retry": lambda *a, **kw: self._retry(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
            },
        )

    def _run_reviewer_attempt(self, context, *args, **kwargs):
        return self._review_handler()._run_reviewer_attempt(context, *args, **kwargs)

    def _review_entry_evidence(self, context):
        return self._review_handler()._review_entry_evidence(context)

    def review(self, context):
        return self._review_handler().review(context)

    def _latest_base_handler(self):
        return execution_assembly.latest_base(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_block_final": lambda *a, **kw: self._block_final(*a, **kw),
                "_checkpoint_remote_oid": lambda *a, **kw: self._checkpoint_remote_oid(*a, **kw),
                "_complete_rebase_intent": lambda *a, **kw: self._complete_rebase_intent(*a, **kw),
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_final": lambda *a, **kw: self._final(*a, **kw),
                "_latest_before_publication": lambda *a, **kw: self._latest_before_publication(*a, **kw),
                "_post_rebase_observation": lambda *a, **kw: self._post_rebase_observation(*a, **kw),
                "_rebase_latest": lambda *a, **kw: self._rebase_latest(*a, **kw),
                "_record_rebase_intent": lambda *a, **kw: self._record_rebase_intent(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_retry": lambda *a, **kw: self._retry(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
                "_run_managed_rebase": lambda *a, **kw: self._run_managed_rebase(*a, **kw),
            },
        )

    def reconcile(self, context, checkpoint, observed_base_sha=None):
        return self._latest_base_handler().reconcile(context, checkpoint, observed_base_sha)

    def _fetch_latest_base(self):
        return self._latest_base_handler()._fetch_latest_base()

    def _rebase_latest(self, context, row, latest_sha, *, publishing):
        return self._latest_base_handler()._rebase_latest(context, row, latest_sha, publishing=publishing)

    def _latest_before_verify(self, context, observed_base_sha):
        return self._latest_base_handler()._latest_before_verify(context, observed_base_sha)

    def _latest_before_publication(self, context):
        return self._latest_base_handler()._latest_before_publication(context)

    def _publication_handler(self):
        return execution_assembly.publication(
            self.effects, getattr(self, "_identity", ExecutionIdentity()),
            self._snapshot, self._update_snapshot,
            {
                "_archive_candidates_at": lambda *a, **kw: self._archive_candidates_at(*a, **kw),
                "_archive_collision": lambda *a, **kw: self._archive_collision(*a, **kw),
                "_block_final": lambda *a, **kw: self._block_final(*a, **kw),
                "_context": lambda *a, **kw: self._context(*a, **kw),
                "_document_block": lambda *a, **kw: self._document_block(*a, **kw),
                "_document_draft": lambda *a, **kw: self._document_draft(*a, **kw),
                "_document_guard": lambda *a, **kw: self._document_guard(*a, **kw),
                "_fetch_latest_base": lambda *a, **kw: self._fetch_latest_base(*a, **kw),
                "_final": lambda *a, **kw: self._final(*a, **kw),
                "_latest_before_publication": lambda *a, **kw: self._latest_before_publication(*a, **kw),
                "_owned_checkpoint_remote": lambda *a, **kw: self._owned_checkpoint_remote(*a, **kw),
                "_post_effect_guard": lambda *a, **kw: self._post_effect_guard(*a, **kw),
                "_record_archive_target": lambda *a, **kw: self._record_archive_target(*a, **kw),
                "_recover": lambda *a, **kw: self._recover(*a, **kw),
                "_retry": lambda *a, **kw: self._retry(*a, **kw),
                "_row": lambda *a, **kw: self._row(*a, **kw),
                "_step_outcome": lambda *a, **kw: self._step_outcome(*a, **kw),
            },
        )

    def _publication_intent(self, context, operation, remote_oid):
        return self._publication_handler()._publication_intent(context, operation, remote_oid)

    def _commit_owned(self, context, title, *, worker_confirmed=False):
        return self._publication_handler()._commit_owned(context, title, worker_confirmed=worker_confirmed)

    def _commit_rebase_resolution(self, context, title):
        return self._publication_handler()._commit_rebase_resolution(context, title)

    def _complete_publication_intent(self, context):
        return self._publication_handler()._complete_publication_intent(context)

    def _publication_resume(self):
        return self._publication_handler()._publication_resume()

    def _reconcile_terminal_publication(self, context):
        return self._publication_handler()._reconcile_terminal_publication(context)

    def _publication_workspace(self, repo, worktree, branch, expected_oid):
        return self._publication_handler()._publication_workspace(repo, worktree, branch, expected_oid)

    def publish(self, context, latest_base):
        return self._publication_handler().publish(context, latest_base)

    def _publish_existing_pull_request(self, context, row, expected_oid):
        return self._publication_handler()._publish_existing_pull_request(context, row, expected_oid)

    def _publish_new_pull_request(self, context, row):
        return self._publication_handler()._publish_new_pull_request(context, row)
