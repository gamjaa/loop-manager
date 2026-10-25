"""Workspace execution policy and narrow typed ports."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping
from pathlib import Path
from dataclasses import asdict
import re
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.git.workspace import validate_reserved_worktree_history, validate_registered_remote, reserved_rebase_before_head
from loop_manager_internal.agents.agent_scope_decision import _same_local_snapshot
from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus, valid_acceptance
import sys
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery.plan_recovery import reserved_workspace_ownership
from loop_manager_internal.storage.task_calls import CallDeferred
from loop_manager_internal.runtime.contracts import ProcessLockBusyError
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure, classify_exception
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind, ExecutionPlanMetadata
from loop_manager_internal.execution.execution_checkpoint import CheckpointAction, assess, observe
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace
from loop_manager_internal.storage.runtime_state import RuntimeStateError
from loop_manager_internal.git.rebase_correction import CorrectionAction, next_action
from loop_manager_internal.execution.worker_outcome import WorkerOutcomeError

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot, RebaseWorkspaceContext
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage import database
from loop_manager_internal.storage import runtime_state

@dataclass(frozen=True)
class WorkspaceHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    update_snapshot: Callable[..., None]
    checkpoint: ExecutionCheckpointRepository | None
    contract: ExecutionContract
    state: ExecutionStatePort
    _plan_id_from_path: Callable[..., Any]
    _rebase_in_progress_for_branch: Callable[..., Any]
    ensure_worker_level_state: Callable[..., Any]
    git: Callable[..., Any]
    git_operation_entries: Callable[..., Any] | None
    make_worktree: Callable[..., Any]
    mark_execplan_workspace_owned: Callable[..., Any]
    operation_print: Callable[..., Any]
    pr_for_branch: Callable[..., Any]
    reconcile_existing_pull_request: Callable[..., Any]
    capture_git_snapshot: Callable[..., Any]
    validate_worker_workspace_scope: Callable[..., Any]
    reserve_execplan_workspace: Callable[..., Any]
    resume_pr_conflict_rebase: Callable[..., Any]
    validate_execplan_workspace_identity: Callable[..., Any]
    _block_worker_decision: Callable[..., ExecutionOutcome]
    _cached_plan: Callable[..., Mapping[str, object]]
    _checkpoint_remote_oid: Callable[..., Any]
    _commit_rebase_resolution: Callable[..., Any]
    _complete_rebase_intent: Callable[..., Any]
    _context: Callable[[], ExecutionContext]
    _current_rebase_service: Callable[..., Any]
    _execute_rebase_step: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _finish_rebase_journal: Callable[..., Any]
    _finish_worker: Callable[..., ExecutionOutcome]
    _guard_rebase_worker_result: Callable[..., Any]
    _legacy_current_rebase: Callable[..., Any]
    _owned_checkpoint_remote: Callable[..., Any]
    _parse_worker_outcome: Callable[..., Any]
    _post_rebase_observation: Callable[..., Any]
    _publish_rebased_checkpoint: Callable[..., Any]
    _rebase_observation: Callable[..., Any]
    _record_rebase_intent: Callable[..., Any]
    _recover: Callable[..., ExecutionOutcome]
    _resolving_rebase: Callable[..., bool]
    _row: Callable[[], Mapping[str, object]]
    _run_managed_rebase: Callable[..., Any]
    _stage_rebase_correction: Callable[..., Any]
    github_repository: Callable[..., Any] | None = None
    publish_worker_checkpoints: Callable[..., Any] | None = None
    reconcile_remote: Callable[..., Any] | None = None
    git_bytes: Callable[..., bytes] | None = None


class WorkspaceHandler:
    """Workspace policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: WorkspaceHandlerPorts):
        self.ports = ports

    def _validate_registered_remote(self, worktree: Path | None = None) -> None:
        project = self.ports.state.connection.execute(
            'SELECT origin, base_branch FROM projects WHERE repo=?',
            (self.ports.identity.repo,),
        ).fetchone()
        if (project is None or project['base_branch'] != self.ports.identity.base
                or not project['origin'] or self.ports.github_repository is None):
            raise self.ports.contract.WorkspaceOwnershipError(
                '등록 저장소·base와 원격 인수 조건을 확인할 수 없습니다.')
        validate_registered_remote(self.ports.identity.repo_path,
            worktree if worktree is not None and worktree.is_dir() else None, project['origin'],
            git=self.ports.git, github_repository=self.ports.github_repository)

    def _terminal_adopted_pr(self, plan_id, pull_request):
        result = self.ports.reconcile_existing_pull_request(
            self.ports.identity.repo_path,
            self.ports.identity.state_dir, self.ports.identity.repo,
            self.ports._row(), pull_request,
        )
        if result == self.ports.contract.PROJECT_STEP_PLANNING_PENDING:
            return ExecutionOutcome(
                ExecutionOutcomeKind.PLANNING_PENDING,
                detail='기존 PR 병합 완료를 반영하고 base/catalog 동기화를 예약했습니다.',
            )
        if result == self.ports.contract.PROJECT_STEP_RETRY:
            return ExecutionOutcome(ExecutionOutcomeKind.RETRY,
                                    detail='기존 terminal PR의 후속 처리를 재시도합니다.')
        return ExecutionOutcome(
            ExecutionOutcomeKind.FINAL_BLOCK,
            detail=('병합되지 않은 닫힌 PR의 명시적 재개 결정을 기다립니다.'
                    if str(pull_request.get('state') or '').upper() == 'CLOSED'
                    else '기존 PR의 terminal 상태 처리를 재시도합니다.'),
            error=True,
        )

    def _workspace_boundary(self, plan_id, row, *, pending):
        checkpoint = self.ports.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.ports.identity.repo, plan_id)
        checkpoint.record(
            self.ports.identity.repo, plan_id, expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={
                **(current.snapshot or {}),
                'worktree_recovery_pending': pending,
                'worktree': str(row['worktree'] or ''),
                'branch': str(row['branch'] or ''),
                'stored_active_sha': str(row['active_sha'] or ''),
            },
        )

    def recover(self, context: ExecutionContext) -> ExecutionContext | ExecutionOutcome:
        """Observe owned Git and reconcile stale markers before phase dispatch."""
        checkpoint = self.ports.checkpoint
        if checkpoint is None:
            return context
        try:
            self.ports.state.check_context(context)
            row = self.ports._row()
            catalog = self.ports.state.active_execplan(self.ports.identity.repo)
            catalog_marker = (
                int(catalog['catalog_generation']),
                str(catalog['current_revision_sha'])
                if catalog['current_revision_sha'] is not None else None,
            )
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            if current.operation == 'rebase':
                service = self.ports._current_rebase_service(context)
                service.normalize_frontmatter()
                _, decision = service.require_owned()
                saved = current.snapshot or {}
                if (decision.status is RebaseStatus.COMPLETE
                        and (saved.get('worker_checkpoint_request')
                             or saved.get('worker_checkpoint_pending')
                             or not (saved.get('pending') in {'stage', 'continue'}
                                     and saved.get('worker_result')))):
                    self.ports._finish_rebase_journal(context)
                    self.ports._complete_rebase_intent(context)
                    return self.ports._context()
            if (current.snapshot or {}).get('worker_checkpoint_request'):
                # Its stopped result and exact local effect must resume before
                # normal fetch/rebase recovery can change the candidate.
                return context
            self.ports._publish_rebased_checkpoint(context)
            row = self.ports._row()
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            if self.ports.identity.newly_activated and current.operation is None and current.expected_head:
                adopted = valid_acceptance((current.snapshot or {}).get('current_rebase_acceptance'))
                remote_adoption = (current.snapshot or {}).get('remote_adoption')
                # Preserve an adopted candidate and restored work across activation.
                # Ordinary stale checkpoints still use the newly prepared workspace.
                current = checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=current.expected_head if adopted or remote_adoption else None,
                    target_base_oid=current.target_base_oid if adopted or remote_adoption else None,
                    observed_remote_oid=current.observed_remote_oid if adopted or remote_adoption else None,
                    snapshot={
                        **(current.snapshot or {} if adopted else {}),
                        'reactivated_from_head': current.expected_head,
                        **{key: current.snapshot[key] for key in (
                            'deleted_branch_restart', 'fresh_remote_creation', 'remote_adoption',
                            'accepted_work_branch', 'worker_checkpoint_pending')
                           if current.snapshot and key in current.snapshot},
                        **({'blocked_resume': current.snapshot['blocked_resume']}
                           if current.snapshot and 'blocked_resume' in current.snapshot
                           else {}),
                        **({'published_review_resume': current.snapshot['published_review_resume']}
                           if current.snapshot and 'published_review_resume' in current.snapshot
                           else {}),
                    },
                )
            current = checkpoint.record(
                self.ports.identity.repo, context.plan.plan_id,
                expected_generation=current.generation,
                operation=current.operation,
                expected_head=current.expected_head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'fetch_in_progress': True},
            )
            base_oid = self.ports._fetch_latest_base()
            current = checkpoint.record(
                self.ports.identity.repo, context.plan.plan_id,
                expected_generation=current.generation,
                operation=current.operation,
                expected_head=current.expected_head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'fetch_in_progress': False,
                          'fetched_base_oid': base_oid},
            )
            remote_oid = self.ports._checkpoint_remote_oid(self.ports.identity.repo_path, context.branch)
            # A PR needs a remote branch.  With neither a remote branch nor a
            # recorded PR there is no publication state to reconcile here;
            # the publication service will query again before creating one.
            inspect_pr = remote_oid is not None or bool(row['pr_url'])
            pull_request = (
                self.ports.pr_for_branch(
                    self.ports.identity.repo_path, context.branch, str(row['pr_url'] or '') or None
                ) if inspect_pr else None
            )
            snapshot = observe(
                plan_id=context.plan.plan_id,
                row={**row, 'phase': context.phase.value},
                definition_revision=catalog_marker[1],
                base_oid=base_oid, branch=context.branch,
                worktree=context.worktree, owned=True,
                git=self.ports.git,
                rebase_in_progress=self.ports._rebase_in_progress_for_branch,
                remote_oid=remote_oid, pull_request=pull_request,
                rebase_intent=(
                    current.operation == 'rebase'
                    or bool(
                        row['pr_conflict_recovery'] and row['pr_conflict_base_oid']
                        and row['phase'] == 'WORKING'
                        and (
                            row['last_error'] == self.ports.contract.REBASE_IN_PROGRESS_ERROR
                            or str(row['last_error'] or '').startswith(
                                self.ports.contract.REBASE_CONFLICT_PREFIX
                            )
                        )
                    )
                ),
                rebase_before_head=(current.expected_head or row['pr_conflict_head_oid']),
                rebase_target_oid=(current.target_base_oid or row['pr_conflict_base_oid']),
                stash_oid=(current.snapshot or {}).get('stash_oid'),
                recorded_local_head=current.expected_head,
                pending_operation=current.operation,
                operation_markers=(
                    self.ports.git_operation_entries(context.worktree)
                    if self.ports.git_operation_entries else None
                ),
            )
            stable_base = self.ports.git(
                self.ports.identity.repo_path, 'rev-parse', f'origin/{self.ports.identity.base}'
            ).stdout.strip()
            stable_remote = self.ports._checkpoint_remote_oid(self.ports.identity.repo_path, context.branch)
            stable_pr = (
                self.ports.pr_for_branch(
                    self.ports.identity.repo_path, context.branch, str(row['pr_url'] or '') or None
                ) if inspect_pr else None
            )
            if (stable_base != base_oid or stable_remote != remote_oid
                    or stable_pr != pull_request):
                return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                     'checkpoint observation changed', context=context)
            from loop_manager_internal.execution import stage_documents
            if ((current.snapshot or {}).get('worker_checkpoint_pending')
                    and current.operation != 'rebase'
                    and self.ports.publish_worker_checkpoints is not None):
                published = self.ports.publish_worker_checkpoints(context,
                    allowed_requirement_archives=self.ports.snapshot().archive_scope_sources)
                if isinstance(published, ExecutionOutcome):
                    return published
                return self.ports._context()
            if self.ports.reconcile_remote is not None:
                if self.ports.reconcile_remote(context, remote_oid, pull_request):
                    context = self.ports._context()
                    if self.ports.publish_worker_checkpoints is not None:
                        published = self.ports.publish_worker_checkpoints(
                            context,
                            allowed_requirement_archives=self.ports.snapshot().archive_scope_sources,
                        )
                        if isinstance(published, ExecutionOutcome):
                            return published
                        context = self.ports._context()
                    return context
                row = self.ports._row()
                current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                snapshot = replace(snapshot, recorded_remote_oid=remote_oid,
                    recorded_pr_head_oid=remote_oid if pull_request else snapshot.recorded_pr_head_oid)
            from loop_manager_internal.execution.manual_candidate import get_manual
            manual = get_manual(self.ports.state)
            if not manual or not manual.enabled():
                stage_documents.remember_work_branch(self.ports.git, self.ports.state, checkpoint,
                    self.ports.identity, context, remote_oid, snapshot.local_head, base_oid, pull_request,
                    git_bytes=self.ports.git_bytes)
                current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            if manual and manual.adopt_remote(context,remote_oid,base_oid,pull_request,
                                               self.ports.state,checkpoint):
                return self.ports._context()
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            pending_worker_push = bool((current.snapshot or {}).get('worker_checkpoint_pending'))
            if (self.ports.publish_worker_checkpoints is not None
                    and current.operation != 'rebase'
                    and not (manual and manual.enabled())
                    and (context.phase.value == 'IMPLEMENTING' or pending_worker_push
                         or (current.snapshot or {}).get('after_head') == snapshot.local_head)):
                published = self.ports.publish_worker_checkpoints(
                    context,
                    allowed_requirement_archives=self.ports.snapshot().archive_scope_sources,
                )
                if isinstance(published, ExecutionOutcome):
                    return published
                if published is not None and published.get('candidate_changed'):
                    return self.ports._context()
                row = self.ports._row()
                current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                if published is not None and published.get('pushed'):
                    # The following ownership decision must observe the completed push.
                    return self.ports._context()
            current_catalog = self.ports.state.active_execplan(self.ports.identity.repo)
            if (
                int(current_catalog['catalog_generation']),
                str(current_catalog['current_revision_sha'])
                if current_catalog['current_revision_sha'] is not None else None,
            ) != catalog_marker:
                return self.ports._recover(
                    RecoveryReason.REMOTE_SNAPSHOT_RACE,
                    'checkpoint catalog revision changed', context=context,
                )
            if (self.ports.identity.newly_activated
                    and valid_acceptance((current.snapshot or {}).get('current_rebase_acceptance'))
                    and snapshot.local_head != current.expected_head):
                return self.ports._recover(
                    RecoveryReason.REMOTE_OID_UNCERTAIN,
                    'reactivated rebase candidate HEAD differs', context=context)
            published_before = self.ports.state.connection.execute(
                'SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                (self.ports.identity.repo, context.plan.plan_id)).fetchone()
            if not self.ports._owned_checkpoint_remote(
                context, row, remote_oid, current, pull_request,
            ):
                return self.ports._recover(
                    RecoveryReason.REMOTE_OID_UNCERTAIN,
                    '게시 checkpoint의 원격 OID 또는 재활성화 계보가 유효하지 않습니다.',
                    context=context,
                )
            published_after = self.ports.state.connection.execute(
                'SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                (self.ports.identity.repo, context.plan.plan_id)).fetchone()
            if published_before != published_after:
                return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                    'checkpoint publication changed during ownership observation', context=context)
            saved = current.snapshot or {}
            if (row['publication_remote_oid'] and current.operation in {None, 'commit'}
                    and not any(saved.get(key) for key in (
                        'published_review_resume', 'blocked_resume', 'current_rebase_acceptance',
                        'completed_rebase', 'rebase_completion', 'remote_adoption_scope'))):
                receipt = checkpoint_ownership.publication_receipt(
                    published_before, row['publication_remote_oid'])
                if receipt is not None:
                    self.ports.state.check_context(context)
                    current = checkpoint.record(
                        self.ports.identity.repo, context.plan.plan_id,
                        expected_generation=current.generation, operation=current.operation,
                        expected_head=current.expected_head, target_base_oid=current.target_base_oid,
                        observed_remote_oid=current.observed_remote_oid,
                        snapshot={**saved, 'published_review_resume': {
                            'request_identity': receipt['request_identity'],
                            'owner_oid': receipt['accepted_remote_oid'],
                            'head': receipt['accepted_remote_oid']}},
                    )
            if (current.operation is None
                    and valid_acceptance((current.snapshot or {}).get('current_rebase_acceptance'))):
                observed, _ = self.ports._current_rebase_service(context).require_owned(checkpoint=current)
                if observed.remote.oid != remote_oid:
                    return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                         'adopted rebase remote observation changed', context=context)
                # The current service has checked this exact lease and PR.
                # A rebased local HEAD need not equal its prior remote HEAD.
                snapshot = replace(snapshot, recorded_remote_oid=remote_oid)
            if (current.operation is None and not (current.snapshot or {}).get('current_rebase_acceptance')
                    and ((current.snapshot or {}).get('completed_rebase')
                         or (current.snapshot or {}).get('rebase_completion')
                         or self.ports._legacy_current_rebase(context, current))):
                self.ports._complete_rebase_intent(context)
                return self.ports._context()
            if current.operation == 'rebase':
                service = self.ports._current_rebase_service(context)
                observed, accepted = service.require_owned()
                if accepted.status is RebaseStatus.START:
                    self.ports._record_rebase_intent(context, current.target_base_oid)
                    rebased = self.ports._run_managed_rebase(context, current.target_base_oid, autostash=True)
                    conflict = self.ports._post_rebase_observation(context, current.target_base_oid, rebased.stdout)
                    if conflict is not None:
                        return conflict
                    if rebased.returncode:
                        return self.ports._recover(RecoveryReason.WORKTREE_RECOVERY_FAILED, rebased.stdout, context=context)
                    self.ports._complete_rebase_intent(context)
                    return self.ports._context()
                if ((current.snapshot or {}).get('pending') == 'continue'
                        and any(entry.stage for entry in observed.index)):
                    # Recover a continue that reached another conflict before its
                    # post-command checkpoint, without consuming the old READY.
                    conflict = self.ports._post_rebase_observation(
                        context, current.target_base_oid, 'resume next rebase conflict')
                    return conflict if conflict is not None else self.ports._context()
                if ((current.snapshot or {}).get('pending') in {'stage', 'continue'}
                        and (current.snapshot or {}).get('worker_result')):
                    saved_result = current.snapshot['worker_result']
                    try:
                        disposition = self.ports._parse_worker_outcome(
                            context, saved_result, allow_legacy_ready=True,
                        )
                        if disposition.kind == 'needs_user_decision':
                            return self.ports._block_worker_decision(
                                context, saved_result, disposition,
                            )
                    except WorkerOutcomeError as error:
                        return self.ports._recover(
                            RecoveryReason.WORKER_CORRECTION,
                            f'저장된 Worker 결과 검증 실패: {error}',
                            context=context,
                        )
                    guarded = self.ports._guard_rebase_worker_result(
                        replace(context, resolving_rebase=True),
                        current.snapshot.get('rebase_worker_input'),
                    )
                    if isinstance(guarded, ExecutionOutcome):
                        return guarded
                    context = guarded
                    row = self.ports._row()
                    current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                    resumed = self.ports._finish_worker(
                        replace(context, resolving_rebase=True), row, saved_result,
                        staged=current.snapshot['pending'] == 'continue',
                    )
                    return resumed.context if resumed.kind is ExecutionOutcomeKind.PHASE_ADVANCED else resumed
                if accepted.status is RebaseStatus.COMPLETE:
                    self.ports._finish_rebase_journal(context)
                    self.ports._complete_rebase_intent(context)
                    return self.ports._context()
                from loop_manager_internal.git.rebase_documents import has_pending_documents
                if observed.metadata is not None and has_pending_documents(current, observed):
                    conflict = self.ports._post_rebase_observation(
                        context, current.target_base_oid, 'resume document conflict resolution')
                    if conflict is not None:
                        return conflict
                    self.ports._complete_rebase_intent(context)
                    return self.ports._context()
                if ((observed.metadata is not None
                         or accepted.reason == 'owned_autostash_restore_pending')
                        and not any(entry.stage for entry in observed.index)):
                    unstaged = self.ports.git(
                        context.worktree, 'diff', '--quiet', check=False)
                    if unstaged.returncode not in {0, 1}:
                        raise manager_git_journal.MutationEvidenceError('current resolution diff unavailable')
                    untracked = self.ports.git(
                        context.worktree, 'ls-files', '--others', '--exclude-standard', '-z', check=False)
                    if untracked.returncode:
                        raise manager_git_journal.MutationEvidenceError('current untracked resolution unavailable')
                    if unstaged.returncode or untracked.stdout:
                        stage = self.ports._stage_rebase_correction(context)
                        if stage.returncode:
                            return self.ports._recover(RecoveryReason.STAGE_FAILED, stage.output, context=context)
                    staged = self.ports.git(
                        context.worktree, 'diff', '--cached', '--quiet', check=False)
                    if staged.returncode not in {0, 1}:
                        raise manager_git_journal.MutationEvidenceError('current resolution index unavailable')
                    action = next_action(self.ports._rebase_observation(context),
                                         worker_result=True, staged=staged.returncode == 1)
                    continued = self.ports._execute_rebase_step(context, action)
                    if action is CorrectionAction.VERIFY and staged.returncode == 1:
                        self.ports._commit_rebase_resolution(
                            context, f'Resolve autostash for {context.plan.plan_id}')
                    conflict = self.ports._post_rebase_observation(
                        context, current.target_base_oid, continued.output)
                    if conflict is not None:
                        return conflict
                    if continued.returncode:
                        return self.ports._recover(RecoveryReason.REBASE_CONTINUE_FAILED,
                                             continued.output, context=context)
                    self.ports._complete_rebase_intent(context)
                    return self.ports._context()
            marker = (current.snapshot or {}).get('blocked_resume')
            # An owned pending rebase reaches conflict recovery before it has an after_head.
            if (self.ports.identity.newly_activated and marker and current.operation != 'rebase'
                    and (snapshot.local_head != marker['after_head']
                         or (current.snapshot or {}).get('reactivated_from_head')
                         != marker['after_head'])):
                return self.ports._recover(
                    RecoveryReason.REMOTE_OID_UNCERTAIN,
                    '재활성화 전후 HEAD가 게시 checkpoint 기록과 다릅니다.',
                    context=context,
                )
            self.ports.state.check_context(context)
            if (current.operation == 'push' and remote_oid == snapshot.local_head
                    or current.operation == 'pr_create' and pull_request is not None):
                completed_push = current.operation == 'push'
                if completed_push and (marker or current.completed_rebase()
                                       or (current.snapshot or {}).get('current_rebase_acceptance')
                                       or (current.snapshot or {}).get('remote_adoption_scope')
                                       or (current.snapshot or {}).get('published_review_resume')):
                    self.ports.state.update_project(
                        self.ports.identity.repo, publication_remote_oid=remote_oid,
                    )
                current = checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=snapshot.local_head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=remote_oid,
                    snapshot={**(current.snapshot or {}),
                              **({'pushed_checkpoint_head': snapshot.local_head}
                                 if completed_push and
                                 ((current.snapshot or {}).get('blocked_resume') or
                                  (current.snapshot or {}).get('published_review_resume') or
                                  (current.snapshot or {}).get('remote_adoption_scope') or
                                  (current.snapshot or {}).get('current_rebase_acceptance') or
                                  current.completed_rebase())
                                 else {})},
                )
            current = checkpoint.record(
                self.ports.identity.repo, context.plan.plan_id,
                expected_generation=current.generation,
                operation=current.operation,
                expected_head=current.expected_head or snapshot.local_head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=remote_oid,
                snapshot={**(current.snapshot or {}), **asdict(snapshot)},
            )
            self.ports.state.check_context(context)
            decision = assess(snapshot)
            if (current.operation == 'commit'
                    and snapshot.local_head != current.expected_head
                    and decision.reason in {
                        'commit_completed_before_checkpoint',
                        'commit_completed_before_rebase',
                    }):
                saved = current.snapshot or {}
                if not checkpoint_ownership.committed_tree_matches_intent(
                        self.ports.identity.repo_path, current.expected_head,
                        snapshot.local_head, saved, self.ports.git,
                        strict_observation=True):
                    record = saved.get('document_record')
                    if isinstance(record, dict) and record.get('applied') is not True:
                        record = {**record, 'unresolved_reason': 'commit_result_unconfirmed'}
                    else:
                        record = None
                    checkpoint.record(
                        self.ports.identity.repo, context.plan.plan_id,
                        expected_generation=current.generation, operation='commit',
                        expected_head=current.expected_head,
                        target_base_oid=current.target_base_oid,
                        observed_remote_oid=current.observed_remote_oid,
                        snapshot={**saved, 'commit_recovery': {
                            'status': 'unconfirmed', 'head': snapshot.local_head,
                        }, **({'document_record': record} if record is not None else {})},
                    )
                    return self.ports._recover(
                        RecoveryReason.OWNERSHIP_UNCERTAIN,
                        '중단된 commit의 parent·message·tree 결과를 intent와 일치시킬 수 없습니다.',
                        context=context,
                    )

                record = saved.get('document_record')
                if isinstance(record, dict) and record.get('applied') is not True:
                    intent = record.get('commit_intent')
                    if intent == {
                            'before_head': saved.get('before_head'),
                            'message': saved.get('commit_message'),
                            'tree': saved.get('commit_tree_oid'),
                    }:
                        record = {**record, 'commit': snapshot.local_head}
                        record.pop('unresolved_reason', None)
                    else:
                        record = {**record, 'unresolved_reason': 'commit_intent_mismatch'}
                confirmed = {
                    **saved,
                    'after_head': snapshot.local_head,
                    'commit_recovery': {
                        'status': 'confirmed', 'head': snapshot.local_head,
                    },
                    **({'document_record': record} if isinstance(record, dict) else {}),
                }
                self.ports.state.correct_checkpoint(
                    phase='VERIFYING', active_sha=base_oid,
                    validated_base_sha=None, invalidate_validation=True,
                    expected_catalog=catalog_marker,
                )
                checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=snapshot.local_head,
                    target_base_oid=base_oid, observed_remote_oid=remote_oid,
                    snapshot={**asdict(snapshot), **confirmed},
                )
                return self.ports._context()
            if decision.action is CheckpointAction.CONTINUE:
                return context
            if decision.action is CheckpointAction.CONFLICT:
                # A correction already in progress owns its diagnostic and
                # Worker session.  Re-observation must not replace the
                # actionable failure with a generic index description.
                if context.resolving_rebase:
                    return context
                self.ports.state.update_project(
                    self.ports.identity.repo, phase='WORKING', validated_base_sha=None,
                    revision_resume_status='VERIFYING',
                    last_error=self.ports.contract.REBASE_CONFLICT_PREFIX + decision.reason,
                )
                return self.ports._context()
            if decision.action is CheckpointAction.CORRECT:
                completion = None
                if current.operation == 'rebase':
                    return self.ports._recover(
                        RecoveryReason.OWNERSHIP_UNCERTAIN,
                        'rebase checkpoint correction lacks a sealed completion',
                        context=context,
                    )
                self.ports.state.correct_checkpoint(
                    phase=decision.phase, active_sha=decision.active_sha,
                    validated_base_sha=decision.validated_base_sha,
                    invalidate_validation=decision.invalidate_validation,
                    expected_catalog=catalog_marker,
                )
                checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=snapshot.local_head,
                    target_base_oid=base_oid, observed_remote_oid=remote_oid,
                    snapshot={**asdict(snapshot), **(
                        {'completed_rebase': completion} if completion else
                        {'completed_rebase': current.completed_rebase()}
                        if current.completed_rebase() else {}
                    ), **{key: current.snapshot[key] for key in
                          ('accepted_work_branch', 'deleted_branch_restart', 'fresh_remote_creation', 'rebase_files',
                           'document_record', 'before_head', 'after_head', 'commit_message', 'commit_tree_oid',
                           'commit_recovery', 'blocked_resume', 'worker_candidate_token')
                          if current.snapshot and key in current.snapshot}, **(
                        {'published_review_resume': current.snapshot['published_review_resume']}
                        if (current.snapshot or {}).get('published_review_resume')
                        else {}
                    ), **(
                        {key: current.snapshot[key] for key in
                         ('remote_adoption_scope', 'last_adopted_remote_oid', 'pushed_checkpoint_head')
                         if key in current.snapshot}
                        if (current.snapshot or {}).get('remote_adoption_scope')
                        else {}
                    )},
                )
                return self.ports._context()
            if decision.action is CheckpointAction.REBASE:
                # The revision gate decides which artifacts a changed base
                # invalidates. VERIFYING commits its Worker result before
                # reconciling the latest base and starting full verify.
                return context
            if decision.action is CheckpointAction.PAUSE:
                return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE if decision.reason == 'remote_input_changed'
                                          else RecoveryReason.STATUS_FAILED, decision.reason,
                                     context=context, attempts=3)
            if decision.reason in {'external_remote_oid', 'external_pr_head'}:
                return self.ports._recover(
                    RecoveryReason.REMOTE_OID_UNCERTAIN,
                    '관측하지 않은 원격 branch 또는 PR head 갱신을 거부합니다: '
                    + decision.reason,
                    context=context,
                )
            return self.ports._recover(
                RecoveryReason.OWNERSHIP_UNCERTAIN, decision.reason,
                context=context,
                final_block_reason=(BlockReasonCode.REPOSITORY_STATE_UNSAFE
                                    if decision.reason == 'autostash_identity_unavailable'
                                    else BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
            )
        except CheckpointRace:
            return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                 'checkpoint generation changed', context=context)
        except RuntimeStateError as error:
            return ExecutionOutcome(
                ExecutionOutcomeKind.TRANSIENT_PAUSE,
                detail=f'execution row changed during checkpoint observation: {error}',
            )
        except RecoveryFailure as error:
            return self.ports._recover(error.reason, str(error), context=context)
        except (RuntimeError, OSError) as error:
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                str(error), context=context,
            )

    def prepare(self) -> ExecutionContext | ExecutionOutcome:
        plan = self.ports.state.active_execplan(self.ports.identity.repo)
        if plan is None or int(plan['workspace_owned']) != self.ports.contract.WORKSPACE_RESERVED:
            return self._prepare()
        try:
            with reserved_workspace_ownership(self.ports.state.connection, self.ports.identity.state_dir,
                                              self.ports.identity.repo, str(plan['plan_id'])) as owner:
                return self._prepare(reservation_owner=owner)
        except (CallDeferred, ProcessLockBusyError, RuntimeStateError) as error:
            return ExecutionOutcome(ExecutionOutcomeKind.TRANSIENT_PAUSE, detail=str(error))

    def _prepare(self, *, reservation_owner=None) -> ExecutionContext | ExecutionOutcome:
        row = self.ports._row()
        branch = str(row['branch'] or '')
        worktree = Path(str(row['worktree'] or ''))
        plan_id = self.ports._plan_id_from_path(str(row['plan_path'] or ''))
        catalog_plan = self.ports.state.active_execplan(self.ports.identity.repo)
        self.ports.update_snapshot(catalog_plan=catalog_plan)
        try:
            resolving_rebase = self.ports._resolving_rebase(row)
        except (RecoveryFailure, RuntimeError, OSError) as error:
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                str(error),
            )
        row = self.ports._row()
        catalog_plan = self.ports.state.active_execplan(self.ports.identity.repo)
        self.ports.update_snapshot(catalog_plan=catalog_plan)
        workspace_ownership = int(catalog_plan['workspace_owned'] if catalog_plan else 0)
        workspace_owned = workspace_ownership == self.ports.contract.WORKSPACE_OWNED
        workspace_reserved = workspace_ownership == self.ports.contract.WORKSPACE_RESERVED
        try:
            if plan_id is None:
                raise self.ports.contract.WorkspaceOwnershipError('활성 ExecPlan 경로에서 ID를 확인할 수 없습니다.')
            if catalog_plan is None or catalog_plan['plan_id'] != plan_id:
                raise self.ports.contract.WorkspaceOwnershipError('활성 ExecPlan의 catalog 상태를 확인할 수 없습니다.')
            self.ports.validate_execplan_workspace_identity(
                self.ports.identity.state_dir,
                self.ports.identity.repo,
                plan_id,
                branch,
                worktree,
            )
            self._workspace_boundary(plan_id, row, pending=True)
            remote_start = None
            remote_observed = None
            start_point = None
            adoption_expected_head = None
            adopted_pr = None
            adoption_pending = False
            local_branch_exists = False
            existing_adoption = bool(workspace_reserved and worktree.is_dir() and self.ports.checkpoint is not None
                and (self.ports.checkpoint.get(self.ports.identity.repo, plan_id).snapshot or {}).get('remote_adoption'))
            can_adopt = (
                self.ports.checkpoint is not None
                and not workspace_owned and not existing_adoption
                and database.remote_work_adoption_allowed(
                    self.ports.state.connection, self.ports.identity.repo, plan_id)
            )
            if can_adopt:
                if branch != f'loop-manager/{plan_id}' or catalog_plan['plan_id'] != plan_id:
                    raise self.ports.contract.WorkspaceOwnershipError(
                        '활성 ExecPlan의 정확한 관리 branch identity가 아닙니다.')
                self._validate_registered_remote()
                observed_checkpoint = self.ports.checkpoint.get(self.ports.identity.repo, plan_id)
                adopted_pr = self.ports.pr_for_branch(
                    self.ports.identity.repo_path, branch, history=True,
                )
                self.ports.state.check()
                if (self.ports.state.active_execplan(self.ports.identity.repo)['catalog_generation']
                        != catalog_plan['catalog_generation']
                        or not database.remote_work_adoption_allowed(
                            self.ports.state.connection, self.ports.identity.repo, plan_id)
                        or self.ports.checkpoint.get(self.ports.identity.repo, plan_id)
                        != observed_checkpoint):
                    raise CheckpointRace('원격 인수 PR 관측 중 catalog/checkpoint가 변경됐습니다.')
                if adopted_pr is not None and (
                        adopted_pr.get('headRefName') != branch
                        or adopted_pr.get('baseRefName') != self.ports.identity.base):
                    raise self.ports.contract.WorkspaceOwnershipError(
                        '원격 작업 PR의 head/base가 등록 대상과 다릅니다.')
                if adopted_pr is not None and (
                        adopted_pr.get('state') in {'MERGED', 'CLOSED'} or adopted_pr.get('mergedAt')):
                    return self._terminal_adopted_pr(plan_id, adopted_pr)
                self.ports._fetch_latest_base()
                remote_observed = self.ports._checkpoint_remote_oid(
                    self.ports.identity.repo_path, branch)
                saved = observed_checkpoint.snapshot or {}
                prior_adoption = saved.get('remote_adoption')
                if prior_adoption is not None and (
                        not isinstance(prior_adoption, dict)
                        or prior_adoption.get('version') != 1
                        or prior_adoption.get('repo') != self.ports.identity.repo
                        or prior_adoption.get('plan_id') != plan_id
                        or prior_adoption.get('branch') != branch
                        or prior_adoption.get('base') != self.ports.identity.base
                        or not isinstance(prior_adoption.get('head'), str)
                        or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', prior_adoption['head']) is None):
                    raise self.ports.contract.WorkspaceOwnershipError(
                        '중단된 원격 작업 인수의 저장 identity가 유효하지 않습니다.')
                if adopted_pr is not None:
                    pr_state = str(adopted_pr.get('state') or '').upper()
                    if (pr_state != 'OPEN' or adopted_pr.get('baseRefName') != self.ports.identity.base
                            or remote_observed is None
                            or adopted_pr.get('headRefOid') != remote_observed):
                        raise RecoveryFailure(
                            RecoveryReason.REMOTE_SNAPSHOT_RACE,
                            '열린 PR과 원격 작업 branch의 정확한 head를 확인할 수 없습니다.',
                        )
                    if row['pr_url'] and row['pr_url'] != adopted_pr.get('url'):
                        raise self.ports.contract.WorkspaceOwnershipError(
                            '저장된 PR과 원격 작업 branch의 PR identity가 다릅니다.')
                elif row['pr_url']:
                    raise self.ports.contract.WorkspaceOwnershipError(
                        '저장된 기존 PR을 원격 작업 branch에서 찾을 수 없습니다.')
                local_branch_exists = self.ports.git(
                    self.ports.identity.repo_path, 'show-ref', '--verify', '--quiet',
                    f'refs/heads/{branch}', check=False).returncode == 0
                local_present = local_branch_exists or worktree.exists() or worktree.is_symlink()
                if local_present and (prior_adoption is None or not workspace_reserved):
                    raise self.ports.contract.WorkspaceOwnershipError(
                        '원격 인수의 저장 intent와 예약 소유권 없는 로컬 자원이 이미 존재합니다.')
                if prior_adoption is not None and remote_observed is None:
                    raise RecoveryFailure(RecoveryReason.REMOTE_BRANCH_DELETED,
                                          '인수 중 원격 작업 branch가 삭제됐습니다.')
                if prior_adoption is not None and (local_branch_exists or worktree.is_dir()):
                    remote_start = str(prior_adoption['head'])
                    adoption_expected_head = remote_start
                    adoption_pending = True
                    if local_branch_exists:
                        local_head = self.ports.git(
                            self.ports.identity.repo_path, 'rev-parse', '--verify',
                            f'refs/heads/{branch}').stdout.strip()
                        if local_head != remote_start:
                            raise self.ports.contract.WorkspaceOwnershipError(
                                '중단된 원격 작업 인수의 로컬 branch가 저장된 HEAD와 다릅니다.')
                elif prior_adoption is not None:
                    remote_start = str(prior_adoption['head'])
                    start_point = remote_start
                    adoption_expected_head = remote_start
                    adoption_pending = True
                elif remote_observed is not None:
                    remote_start = remote_observed
                    start_point = remote_start
                    adoption_expected_head = remote_start
                    adoption_pending = True
                    current = observed_checkpoint
                    observed_checkpoint = self.ports.checkpoint.record(
                        self.ports.identity.repo, plan_id,
                        expected_generation=current.generation, operation=current.operation,
                        expected_head=current.expected_head, target_base_oid=current.target_base_oid,
                        observed_remote_oid=current.observed_remote_oid,
                        snapshot={**(current.snapshot or {}), 'remote_adoption': {
                            'version': 1, 'repo': self.ports.identity.repo,
                            'plan_id': plan_id, 'branch': branch,
                            'base': self.ports.identity.base, 'head': remote_start}},
                    )
                elif adopted_pr is not None:
                    raise RecoveryFailure(
                        RecoveryReason.REMOTE_BRANCH_DELETED,
                        '열린 PR의 원격 작업 branch를 찾을 수 없습니다.',
                    )
                if remote_start is not None:
                    live = manager_git_journal.live_remote_branch_oid(
                        self.ports.git, self.ports.identity.repo_path, branch)
                    if live != remote_observed:
                        raise RecoveryFailure(
                            RecoveryReason.REMOTE_SNAPSHOT_RACE,
                            '원격 작업 branch가 fetch 이후 변경됐습니다.',
                        )
                self.ports.state.check()
                if (self.ports.state.active_execplan(self.ports.identity.repo)['catalog_generation']
                        != catalog_plan['catalog_generation']
                        or not database.remote_work_adoption_allowed(
                            self.ports.state.connection, self.ports.identity.repo, plan_id)
                        or self.ports.checkpoint.get(self.ports.identity.repo, plan_id) != observed_checkpoint):
                    raise CheckpointRace('원격 인수 fetch/로컬 관측 중 대상이 변경됐습니다.')
            adoption_reserved = bool(
                adoption_pending and workspace_reserved
                and (local_branch_exists or worktree.is_dir())
            )
            adoption_create_branch = bool(
                adoption_pending and not local_branch_exists and not worktree.is_dir()
            )
            reserved_checkpoint = (self.ports.checkpoint.get(self.ports.identity.repo, plan_id)
                                   if workspace_reserved and self.ports.checkpoint is not None else None)
            reserved_observation = (self.ports.capture_git_snapshot(worktree)
                                    if workspace_reserved and worktree.is_dir() else None)
            reservation_validator = None
            adoption_observation = None
            if reserved_observation is not None and reservation_owner is not None and reserved_checkpoint is not None:
                def observe_adoption():
                    return reservation_owner.remote_adoption(catalog_plan, reserved_checkpoint,
                        self.ports.identity.repo_path, worktree, git=self.ports.git,
                        github_repository=self.ports.github_repository, pr_for_branch=self.ports.pr_for_branch)
                adoption_observation = observe_adoption()
                adopted_pr = dict(adoption_observation or ())
                if adopted_pr and (str(adopted_pr.get('state') or '').upper() in {'CLOSED', 'MERGED'}
                                   or adopted_pr.get('mergedAt')):
                    self.ports.state.check()
                    reservation_owner.check()
                    if self.ports.checkpoint.get(self.ports.identity.repo, plan_id) != reserved_checkpoint:
                        raise CheckpointRace('기존 인수 PR 관측 중 checkpoint가 변경됐습니다.')
                    return self._terminal_adopted_pr(plan_id, adopted_pr)
                scopes = reservation_owner.agent_scopes(row, reserved_checkpoint)
                def reservation_validator(live):
                    snapshot = self.ports.capture_git_snapshot(worktree)
                    before_head = None
                    if reserved_checkpoint.operation == 'rebase':
                        context = RebaseWorkspaceContext(ExecutionPlanMetadata(plan_id=plan_id, title='',
                            path=str(row['plan_path']), plan_sha=None), worktree, branch)
                        before_head = reserved_rebase_before_head(self.ports._current_rebase_service(
                            context, allow_reserved=True), reserved_checkpoint, snapshot)
                    return validate_reserved_worktree_history(
                        self.ports.identity.state_dir, self.ports.identity.repo_path, worktree, branch, plan_id,
                        live, snapshot, checkpoint=reserved_checkpoint,
                        git=self.ports.git, base=self.ports.identity.base, session=row['worker_session_id'],
                        sessions=reservation_owner.worker_sessions, agent_scopes=scopes,
                        adoption_eligible=reservation_owner.adoption_eligible, rebase_before_head=before_head)
            self.ports.make_worktree(
                self.ports.identity.repo_path,
                worktree,
                branch,
                self.ports.identity.base,
                allow_create_branch=(
                    (self.ports.identity.newly_activated or adoption_create_branch)
                    and not workspace_owned and not adoption_reserved
                ),
                allow_recovery=workspace_owned or workspace_reserved or adoption_pending,
                allow_existing=(workspace_owned or (workspace_reserved and not adoption_pending)
                                or adoption_reserved),
                ownership_reserved=(workspace_reserved and not adoption_pending) or adoption_reserved,
                expected_head=(
                    adoption_expected_head if adoption_reserved else
                    reserved_checkpoint.snapshot['remote_adoption']['head'] if existing_adoption else
                    str(row['active_sha'] or '') if remote_start is None else None
                ),
                start_point=start_point,
                before_create=lambda: self.ports.reserve_execplan_workspace(self.ports.identity.repo, plan_id),
                allow_rebase=resolving_rebase,
                journal_state_dir=self.ports.identity.state_dir,
                journal_owner=f'plan:{plan_id}',
                **({'require_creation_evidence': True} if adoption_reserved or existing_adoption else {}),
                **({'reservation_validator': reservation_validator} if reservation_validator is not None else {}),
            )
            if remote_start is not None:
                self._validate_registered_remote(worktree)
                self.ports.validate_worker_workspace_scope(
                    self.ports.identity.repo_path, worktree, branch,
                    self.ports.identity.base, str(row['plan_path']), catalog_plan,
                    self.ports.capture_git_snapshot(worktree), scope_phase='IMPLEMENTING',
                )
                head = self.ports.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
                if (head != adoption_expected_head
                        or manager_git_journal.live_remote_branch_oid(
                            self.ports.git, self.ports.identity.repo_path, branch) != remote_observed):
                    raise RecoveryFailure(
                        RecoveryReason.REMOTE_SNAPSHOT_RACE,
                        '관리 worktree 구성 중 원격 작업 branch가 변경됐습니다.',
                    )
                self.ports.state.check()
                if (self.ports.state.active_execplan(self.ports.identity.repo)['catalog_generation']
                        != catalog_plan['catalog_generation']
                        or not database.remote_work_adoption_allowed(
                            self.ports.state.connection, self.ports.identity.repo, plan_id)):
                    raise CheckpointRace('원격 인수 worktree 생성 중 catalog/등록 대상이 변경됐습니다.')
                current = observed_checkpoint
                self.ports.checkpoint.record(
                    self.ports.identity.repo, plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=adoption_expected_head, target_base_oid=str(row['active_sha'] or ''),
                    observed_remote_oid=adoption_expected_head,
                    snapshot={**(current.snapshot or {}), 'accepted_work_branch': adoption_expected_head,
                              'remote_adoption': {'branch': branch, 'head': remote_start,
                                                  'base': self.ports.identity.base,
                                                  'repo': self.ports.identity.repo,
                                                  'plan_id': plan_id, 'version': 1}},
                )
                if adopted_pr is not None:
                    self.ports.state.update_project(
                        self.ports.identity.repo, pr_url=str(adopted_pr['url']),
                        pr_head_oid=remote_start,
                    )
            if workspace_reserved and remote_start is None:
                if reservation_validator is not None and observe_adoption() != adoption_observation:
                    raise RuntimeStateError('예약 workspace 검증 중 원격 인수 PR이 변경됐습니다.')
                if reserved_observation is not None:
                    confirmed = self.ports.capture_git_snapshot(worktree)
                    resource = str(_canonical_path(worktree))
                    if (not _same_local_snapshot(reserved_observation, confirmed)
                            or not reserved_observation.shared_metadata_complete
                            or not confirmed.shared_metadata_complete
                            or resource not in reserved_observation.worktrees
                            or reserved_observation.worktrees[resource] != confirmed.worktrees.get(resource)):
                        raise RuntimeStateError('예약 workspace 검증 중 대상 Git 자원이 변경됐습니다.')
                with runtime_state._transaction(self.ports.state.connection):
                    if reservation_owner is not None:
                        reservation_owner.check()
                    self.ports.state.check()
                    if (dict(self.ports.state.active_execplan(self.ports.identity.repo)) != dict(catalog_plan)
                            or (reserved_checkpoint is not None
                                and self.ports.checkpoint.get(self.ports.identity.repo, plan_id) != reserved_checkpoint)):
                        raise RuntimeStateError('예약 workspace 검증 중 작업/checkpoint가 변경됐습니다.')
                    self.ports.mark_execplan_workspace_owned(self.ports.identity.repo, plan_id)
                    if existing_adoption and 'accepted_work_branch' not in (reserved_checkpoint.snapshot or {}):
                        self.ports.checkpoint.record(self.ports.identity.repo, plan_id,
                            expected_generation=reserved_checkpoint.generation, operation=reserved_checkpoint.operation,
                            expected_head=reserved_checkpoint.expected_head, target_base_oid=reserved_checkpoint.target_base_oid,
                            observed_remote_oid=reserved_checkpoint.observed_remote_oid,
                            snapshot={**reserved_checkpoint.snapshot,
                                      'accepted_work_branch': reserved_checkpoint.snapshot['remote_adoption']['head']})
            else:
                with runtime_state._transaction(self.ports.state.connection):
                    if reservation_owner is not None:
                        reservation_owner.check()
                    self.ports.mark_execplan_workspace_owned(self.ports.identity.repo, plan_id)
            database.record_execplan_workspace_seen(
                self.ports.state.connection, self.ports.identity.repo, plan_id)
            self._workspace_boundary(plan_id, row, pending=False)
        except RuntimeStateError as error:
            return ExecutionOutcome(ExecutionOutcomeKind.TRANSIENT_PAUSE,
                                    detail=f'원격 인수 중 실행 상태가 변경됐습니다: {error}')
        except self.ports.contract.WorkspaceOwnershipError as error:
            workspace_error = f"관리 workspace 소유권/복구 검증 실패: {error}"
            outcome = self.ports._recover(
                RecoveryReason.OWNERSHIP_UNCERTAIN, workspace_error,
                from_revision=True,
            )
            self.ports.operation_print(workspace_error, file=sys.stderr)
            return outcome
        except RecoveryFailure as error:
            return self.ports._recover(error.reason, str(error))
        except (RuntimeError, OSError) as error:
            detail = f"관리 workspace 복구 재시도: {error}"
            self.ports.operation_print(detail, file=sys.stderr)
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.WORKTREE_RECOVERY_FAILED),
                detail,
            )
        try:
            if int(row['pr_conflict_recovery']):
                recovery_rebase = self.ports.resume_pr_conflict_rebase(self.ports.identity.repo_path, self.ports.identity.repo, row, worktree, branch)
                if recovery_rebase == 1:
                    return self.ports._final()
                if recovery_rebase == self.ports.contract.PROJECT_STEP_RETRY:
                    return self.ports._recover(
                        RecoveryReason.REBASE_CONTINUE_FAILED,
                        str(self.ports._row()['last_error'] or 'PR rebase 복구 재시도'),
                    )
                row = self.ports._row()
                resolving_rebase = self.ports._resolving_rebase(row)
            rebase_error = str(row['last_error'] or '')
            rebase_recovery = bool(
                row['phase'] == 'WORKING' and (rebase_error == self.ports.contract.REBASE_IN_PROGRESS_ERROR or rebase_error.startswith(self.ports.contract.REBASE_CONFLICT_PREFIX)),
            )
            if rebase_recovery and (not resolving_rebase) and (not int(row['pr_conflict_recovery'])):
                resume_status = str(row['revision_resume_status'] or 'VERIFYING')
                resume_phase = 'WORKING' if resume_status == 'IMPLEMENTING' else resume_status
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase=resume_phase,
                    revision_resume_status=None,
                    last_error=None,
                )
                row = self.ports._row()
            assert catalog_plan is not None
            self.ports.update_snapshot(plan=self.ports._cached_plan(catalog_plan))
            self.ports.update_snapshot(level=self.ports.ensure_worker_level_state(
                self.ports.identity.repo, self.ports.snapshot().plan
            ))
            self.ports.update_snapshot(catalog_plan=catalog_plan)
            return self.ports._context()
        except (RecoveryFailure, manager_git_journal.MutationEvidenceError) as error:
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                str(error),
            )
