"""Workspace execution policy and narrow typed ports."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping
from pathlib import Path
from dataclasses import asdict
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus, valid_acceptance
import sys
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure, classify_exception
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.execution.execution_checkpoint import CheckpointAction, assess, observe
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace
from loop_manager_internal.storage.runtime_state import RuntimeStateError
from loop_manager_internal.git.rebase_correction import CorrectionAction, next_action
from loop_manager_internal.execution.worker_outcome import WorkerOutcomeError

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository

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


class WorkspaceHandler:
    """Workspace policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: WorkspaceHandlerPorts):
        self.ports = ports

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
            self.ports._publish_rebased_checkpoint(context)
            row = self.ports._row()
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            if self.ports.identity.newly_activated and current.operation is None and current.expected_head:
                adopted = valid_acceptance((current.snapshot or {}).get('current_rebase_acceptance'))
                # Preserve an adopted candidate and restored work across activation.
                # Ordinary stale checkpoints still use the newly prepared workspace.
                current = checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=current.expected_head if adopted else None,
                    target_base_oid=current.target_base_oid if adopted else None,
                    observed_remote_oid=current.observed_remote_oid if adopted else None,
                    snapshot={
                        **(current.snapshot or {} if adopted else {}),
                        'reactivated_from_head': current.expected_head,
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
            from loop_manager_internal.execution.manual_candidate import get_manual
            manual = get_manual(self.ports.state)
            if not manual or not manual.enabled():
                stage_documents.remember_work_branch(self.ports.git, self.ports.state, checkpoint,
                    self.ports.identity, context, remote_oid, snapshot.local_head, base_oid, pull_request)
                current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            if manual and manual.adopt_remote(context,remote_oid,base_oid,pull_request,
                                               self.ports.state,checkpoint):
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
                if accepted.status is RebaseStatus.COMPLETE:
                    self.ports._finish_rebase_journal(context)
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
            if (self.ports.identity.newly_activated and marker
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
            if (current.operation == 'rebase'
                    and decision.action not in {CheckpointAction.USER_ACTION, CheckpointAction.PAUSE}
                    and (current.snapshot or {}).get('pending') == 'stage'
                    and (current.snapshot or {}).get('worker_result')):
                self.ports._current_rebase_service(context).require_owned()
                unstaged = self.ports.git(context.worktree, 'diff', '--quiet', check=False)
                if unstaged.returncode not in {0, 1}:
                    raise manager_git_journal.MutationEvidenceError('current stage observation failed')
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
                untracked = self.ports.git(
                    context.worktree, 'ls-files', '--others', '--exclude-standard', '-z',
                ).stdout
                # The intent precedes staging. Identical accepted contents
                # may still need staging, including accepted new files.
                if unstaged.returncode == 1 or untracked or snapshot.unmerged_stages:
                    try:
                        stage = self.ports._stage_rebase_correction(context)
                    except (RuntimeError, OSError) as error:
                        return self.ports._recover(
                            classify_exception(error, operation=RecoveryReason.STAGE_FAILED),
                            str(error), context=context,
                        )
                    if stage.returncode:
                        return self.ports._recover(RecoveryReason.STAGE_FAILED,
                                             stage.output[-8000:], context=context)
                resumed = self.ports._finish_worker(
                    replace(context, resolving_rebase=True), row,
                    current.snapshot['worker_result'], staged=True,
                )
                return (resumed.context
                        if resumed.kind is ExecutionOutcomeKind.PHASE_ADVANCED
                        else resumed)
            if (current.operation == 'rebase'
                    and decision.action not in {CheckpointAction.USER_ACTION, CheckpointAction.PAUSE}
                    and (current.snapshot or {}).get('pending') == 'continue'
                    and snapshot.git_operation == 'rebase'
                    and not snapshot.unmerged_stages):
                staged = self.ports.git(
                    context.worktree, 'diff', '--cached', '--quiet', check=False
                )
                if staged.returncode not in {0, 1}:
                    return self.ports._recover(RecoveryReason.STAGE_FAILED,
                                         'staged rebase correction observation failed',
                                         context=context)
                observation = self.ports._rebase_observation(context)
                action = next_action(
                    observation, worker_result=True,
                    staged=staged.returncode == 1,
                )
                try:
                    continued = self.ports._execute_rebase_step(context, action)
                except (RuntimeError, OSError) as error:
                    return self.ports._recover(
                        classify_exception(error, operation=RecoveryReason.REBASE_CONTINUE_FAILED),
                        str(error), context=context,
                    )
                conflict = self.ports._post_rebase_observation(
                    context, str(current.target_base_oid or base_oid),
                    continued.output, corrected=True,
                    worker_result=(current.snapshot or {}).get('worker_result'),
                )
                if conflict is not None:
                    return conflict
                if continued.returncode:
                    return self.ports._recover(RecoveryReason.REBASE_CONTINUE_FAILED,
                                         continued.output[-8000:], context=context)
                self.ports._complete_rebase_intent(context, project_values={
                    'phase': 'VERIFYING',
                    'active_sha': str(current.target_base_oid or base_oid),
                    'validated_base_sha': None, 'last_error': None,
                    'revision_resume_status': None,
                    'verify_result': None, 'review_result': None,
                    'verify_token': None, 'review_token': None,
                    'publication_token': None,
                })
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
                    ), **(
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
                return self.ports._recover(RecoveryReason.STATUS_FAILED, decision.reason,
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
        row = self.ports._row()
        branch = str(row['branch'] or '')
        worktree = Path(str(row['worktree'] or ''))
        plan_id = self.ports._plan_id_from_path(str(row['plan_path'] or ''))
        catalog_plan = self.ports.state.active_execplan(self.ports.identity.repo)
        self.ports.update_snapshot(catalog_plan=catalog_plan)
        workspace_ownership = int(catalog_plan['workspace_owned'] if catalog_plan else 0)
        workspace_owned = workspace_ownership == self.ports.contract.WORKSPACE_OWNED
        workspace_reserved = workspace_ownership == self.ports.contract.WORKSPACE_RESERVED
        try:
            resolving_rebase = self.ports._resolving_rebase(row)
        except (RecoveryFailure, RuntimeError, OSError) as error:
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                str(error),
            )
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
            self.ports.make_worktree(
                self.ports.identity.repo_path,
                worktree,
                branch,
                self.ports.identity.base,
                allow_create_branch=self.ports.identity.newly_activated and (not workspace_owned) and (not workspace_reserved),
                allow_recovery=workspace_owned or workspace_reserved,
                allow_existing=workspace_owned or workspace_reserved,
                ownership_reserved=workspace_reserved,
                expected_head=str(row['active_sha'] or ''),
                before_create=lambda: self.ports.reserve_execplan_workspace(self.ports.identity.repo, plan_id),
                allow_rebase=resolving_rebase,
                journal_state_dir=self.ports.identity.state_dir,
                journal_owner=f'plan:{plan_id}',
            )
            self.ports.mark_execplan_workspace_owned(self.ports.identity.repo, plan_id)
            self._workspace_boundary(plan_id, row, pending=False)
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
