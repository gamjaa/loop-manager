"""Revision execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.agents import common_guidance
from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping
import json
import sys
from pathlib import Path
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot, RevisionGateKind, StageArtifacts, WORKER_INVALIDATING_DELTAS, decide_revision_gate
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind, ExecutionPhase
from loop_manager_internal.execution.worker_outcome import WorkerOutcomeError
from loop_manager_internal.git.command_adapter import ExternalObservationError
from loop_manager_internal.planning.planning_catalog import PlannerValidationError

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage import runtime_state

@dataclass(frozen=True)
class RevisionHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    update_snapshot: Callable[..., None]
    checkpoint: ExecutionCheckpointRepository | None
    contract: ExecutionContract
    state: ExecutionStatePort
    _rebase_in_progress_for_branch: Callable[..., Any]
    body_hash: Callable[..., Any]
    branch_has_changes: Callable[..., Any]
    changed_paths: Callable[..., Any]
    ensure_worker_level_state: Callable[..., Any]
    git: Callable[..., Any]
    operation_log: Callable[..., Any]
    operation_print: Callable[..., Any]
    parse_worker_result: Callable[..., Any]
    reconcile_catalog: Callable[..., Any]
    record_remote_publication_branch: Callable[..., Any]
    source_fingerprint: Callable[..., Any]
    unmet_dependencies: Callable[..., Any]
    validate_active_execplan_metadata: Callable[..., Any]
    _block_final: Callable[..., ExecutionOutcome]
    _block_worker_decision: Callable[..., ExecutionOutcome]
    _cached_plan: Callable[..., Mapping[str, object]]
    _checkpoint_remote_oid: Callable[..., Any]
    _commit_owned: Callable[..., Any]
    _complete_rebase_intent: Callable[..., Any]
    _context: Callable[[], ExecutionContext]
    _document_draft: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _parse_worker_outcome: Callable[..., Any]
    _post_rebase_observation: Callable[..., Any]
    _publish_rebased_checkpoint: Callable[..., Any]
    _reconcile_terminal_publication: Callable[..., Any]
    _record_rebase_intent: Callable[..., Any]
    _recover: Callable[..., ExecutionOutcome]
    _refresh_revision_base: Callable[..., Any]
    _retry: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]
    _run_managed_rebase: Callable[..., Any]
    guard: Callable[[ExecutionContext], ExecutionContext | ExecutionOutcome]
    observe_verify_inputs: Callable[..., Any] | None = None
    refresh_documents: Callable[..., Any] | None = None


class RevisionHandler:
    """Revision policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: RevisionHandlerPorts):
        self.ports = ports

    @staticmethod
    def _project_phase(status):
        return 'WORKING' if status == 'IMPLEMENTING' else status

    def _revision_state_values(
        self, snapshot, decision, latest_sha, *, legacy_baseline=False
    ):
        row = self.ports._row()
        target = decision.phase
        values = {
            'definition_snapshot': snapshot.serialize(),
            'phase': self._project_phase(target),
            'revision_resume_status': None,
        }
        if decision.kind is RevisionGateKind.REFRESH_WORKTREE:
            # The refresh intent marker is crash recovery state. Once rebase
            # succeeds it must not be mistaken for an unfinished legacy
            # latest-base rebase on the next process step.
            values['last_error'] = None
        if legacy_baseline:
            if row['worker_result'] is not None:
                values['worker_input_token'] = snapshot.worker_token
                values['archive_token'] = snapshot.archive_token
            # Legacy validation has no durable candidate HEAD. Retain its
            # audit text but require validation on the current candidate.
        if target == 'IMPLEMENTING' and (
            decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE
            or str(row['phase']) != 'WORKING'
            or bool(decision.deltas & WORKER_INVALIDATING_DELTAS)
        ):
            values.update(
                validated_base_sha=None,
                worker_result=None,
                verify_result=None,
                review_result=None,
                worker_input_token=None,
                archive_token=None,
                verify_token=None,
                review_token=None,
                publication_token=None,
                last_error=None,
            )
        elif target == 'VERIFYING' and (
            decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE
            or str(row['phase']) not in {'WORKING', 'VERIFYING'}
        ):
            values.update(
                validated_base_sha=None,
                verify_result=None,
                review_result=None,
                verify_token=None,
                review_token=None,
                publication_token=None,
                last_error=None,
            )
        elif target == 'REVIEWING' and (
            decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE
            or str(row['phase']) == 'PUBLISHING'
        ):
            values.update(
                review_result=None,
                review_token=None,
                publication_token=None,
                last_error=None,
            )
        return values

    def _install_revision_state(
        self, snapshot, decision, latest_sha, *, legacy_baseline=False
    ):
        self.ports.state.update_project(
            self.ports.identity.repo,
            **self._revision_state_values(
                snapshot,
                decision,
                latest_sha,
                legacy_baseline=legacy_baseline,
            ),
        )
        if decision.phase == 'VERIFYING' and self.ports.checkpoint is not None:
            row = self.ports._row()
            from loop_manager_internal.execution.manual_candidate import get_manual
            manual = get_manual(self.ports.state)
            if (manual and manual.user_submission(row)
                    or row['worker_result'] and row['worker_input_token'] == snapshot.worker_token):
                plan_id = str(self.ports.snapshot().catalog_plan['plan_id'])
                current = self.ports.checkpoint.get(self.ports.identity.repo, plan_id)
                head = self.ports.git(Path(row['worktree']), 'rev-parse', 'HEAD').stdout.strip()
                self.ports.checkpoint.record(self.ports.identity.repo, plan_id,
                    expected_generation=current.generation, operation=current.operation,
                    expected_head=current.expected_head, target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot={**(current.snapshot or {}), 'worker_candidate_token': snapshot.worker_candidate_token(head)})
        self.ports.update_snapshot(_definition_snapshot=snapshot)
        self.ports.update_snapshot(catalog_plan=self.ports.state.active_execplan(self.ports.identity.repo))
        assert self.ports.snapshot().catalog_plan is not None
        self.ports.update_snapshot(plan=self.ports._cached_plan(self.ports.snapshot().catalog_plan))

    def _refresh_revision_base(
        self,
        context,
        snapshot,
        decision,
        latest_sha,
        *,
        legacy_baseline=False,
    ):
        row = self.ports._row()
        if row['worker_result']:
            try:
                disposition = self.ports._parse_worker_outcome(
                    context, str(row['worker_result']), allow_legacy_ready=True,
                )
                if disposition.kind == 'needs_user_decision':
                    return self.ports._block_worker_decision(
                        context, str(row['worker_result']), disposition,
                    )
            except WorkerOutcomeError as error:
                detail = f'저장된 Worker 결과 검증 실패: {error}'
                self.ports.state.update_project(
                    self.ports.identity.repo, phase='WORKING', last_error=detail,
                )
                return self.ports._recover(
                    RecoveryReason.WORKER_CORRECTION, detail,
                    context=context,
                )
        if context.phase is ExecutionPhase.PUBLISHING and not int(
            row['pr_conflict_recovery']
        ):
            remote_oid = self.ports._checkpoint_remote_oid(
                self.ports.identity.repo_path, context.branch
            )
            try:
                self.ports.record_remote_publication_branch(
                    self.ports.identity.repo_path,
                    self.ports.identity.state_dir,
                    self.ports.identity.repo,
                    context.branch,
                    context.worktree,
                    remote_oid,
                )
            except self.ports.contract.PublicationSafetyError as error:
                publication_error = str(error)
                self.ports._block_final(
                    self.ports.identity.repo,
                    context.worktree,
                    publication_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                self.ports.operation_print(
                    publication_error, file=sys.stderr
                )
                return self.ports._final(publication_error)
            row = self.ports._row()
        if (
            context.phase is ExecutionPhase.VERIFYING
            and decision.phase == 'VERIFYING'
            and self.ports.git(
                context.worktree, 'status', '--porcelain'
            ).stdout.strip()
        ):
            # Preserve the uncommitted Worker patch as a local checkpoint so
            # a conflicting base rebase remains a resumable Git operation.
            worker_publication = self.ports.parse_worker_result(
                str(row['worker_result'] or ''), self.ports.snapshot().plan
            )
            self.ports.update_snapshot(plan=self.ports.validate_active_execplan_metadata(
                self.ports.identity.repo_path,
                context.worktree,
                context.plan.path,
                self.ports.snapshot().catalog_plan,
            ))
            self.ports.git(context.worktree, 'add', '-A')
            self.ports._commit_owned(context, worker_publication.commit_title)
        refresh_values = {
            'phase': 'WORKING',
            'validated_base_sha': None,
            'revision_resume_status': decision.phase,
            'last_error': self.ports.contract.REBASE_IN_PROGRESS_ERROR,
        }
        if int(row['pr_conflict_recovery']):
            refresh_values['pr_conflict_base_oid'] = latest_sha
        self.ports.state.update_project(self.ports.identity.repo, **refresh_values)
        self.ports._record_rebase_intent(context, latest_sha)
        rebased = self.ports._run_managed_rebase(context, latest_sha, autostash=True)
        conflict = self.ports._post_rebase_observation(context, latest_sha, rebased.stdout)
        if conflict is not None:
            return conflict
        if rebased.returncode:
            rebase_failure = self.ports.contract.REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:]
            in_progress = self.ports._rebase_in_progress_for_branch(
                context.worktree, context.branch
            )
            if in_progress:
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    last_error=rebase_failure,
                )
            self.ports.operation_print(rebase_failure, file=sys.stderr)
            return self.ports._recover(
                RecoveryReason.REBASE_CONFLICT if in_progress else RecoveryReason.WORKTREE_RECOVERY_FAILED,
                rebase_failure, context=context,
            )
        if (
            decision.phase != 'IMPLEMENTING'
            and not self.ports.branch_has_changes(
                context.worktree, self.ports.identity.base
            )
        ):
            prefix = (
                '게시 직전 '
                if context.phase is ExecutionPhase.PUBLISHING
                else ''
            )
            no_change_error = (
                f'{prefix}rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
            )
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                no_change_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_reason=RecoveryReason.NO_CHANGE,
            )
            self.ports.operation_print(no_change_error, file=sys.stderr)
            return self.ports._final(no_change_error)
        from loop_manager_internal.execution import stage_documents
        document_revision = (stage_documents.pending(self.ports.checkpoint, self.ports.identity.repo,
                             context.plan.plan_id) if self.ports.checkpoint else None)
        document_only = bool(document_revision and document_revision['stage'] == 'rebase'
                             and document_revision['document_only'])
        unchanged_worker = (decision.phase != 'IMPLEMENTING'
                            and context.phase is not ExecutionPhase.IMPLEMENTING
                            and (document_only or not bool(decision.deltas & WORKER_INVALIDATING_DELTAS)))
        self.ports._complete_rebase_intent(context, worker_definition=snapshot if unchanged_worker else None, project_values={
            'phase': 'WORKING', 'active_sha': latest_sha,
            'validated_base_sha': None, 'last_error': None,
            'verify_result': None, 'review_result': None,
            'verify_token': None, 'review_token': None,
            'publication_token': None,
        })
        from loop_manager_internal.execution.manual_candidate import get_manual
        manual=get_manual(self.ports.state)
        retained_worker = (unchanged_worker and row['worker_result']
                           and row['worker_input_token'] == snapshot.worker_token
                           and row['archive_token'] == snapshot.archive_token)
        resumed_phase='VERIFYING' if retained_worker or (manual and manual.user_submission(row)) else 'IMPLEMENTING'
        if document_only:
            resumed_phase = document_revision['phase']
        self._install_revision_state(
            snapshot,
            replace(decision, kind=RevisionGateKind.INVALIDATE_TO_PHASE, phase=resumed_phase),
            latest_sha,
            legacy_baseline=False,
        )
        return self.ports._retry('최신 ExecPlan revision을 관리 worktree에 반영했습니다.')

    def _revision_error(self, context, detail):
        self.ports._block_final(
            self.ports.identity.repo,
            context.worktree,
            detail,
            str(self.ports._row()['verify_result'] or ''),
            reason=BlockReasonCode.PLAN_INPUT_INVALID,
            user_targets=(context.plan.path,),
        )
        self.ports.operation_print(detail, file=sys.stderr)
        return self.ports._final(detail)

    def guard(self, context: ExecutionContext) -> ExecutionContext | ExecutionOutcome:
        """Refresh and reconcile before any phase effect consumes its snapshot."""

        self.ports.state.check_context(context)
        from loop_manager_internal.execution.execution_revision import advance_owned_record, record_only_definition
        advance_owned_record(self.ports, context)
        from loop_manager_internal.execution.manual_candidate import get_manual
        pending_manual = get_manual(self.ports.state)
        if pending_manual and pending_manual.enabled() and pending_manual.group()['pending_source_head'] \
                and not pending_manual.group()['source_update_conflict']:
            return ExecutionOutcome(ExecutionOutcomeKind.STABLE_WAIT,context=context,
                detail='수락한 새 source 후보를 안전한 실행 자리에서 반영하기 위해 대기합니다.')
        if context.phase is ExecutionPhase.PUBLISHING:
            terminal = self.ports._reconcile_terminal_publication(context)
            if terminal is not None:
                return terminal
        self.ports._publish_rebased_checkpoint(context)
        latest_sha = self.ports._fetch_latest_base()
        try:
            result = self.ports.reconcile_catalog(
                self.ports.identity.repo_path, self.ports.identity.repo, self.ports.identity.base
            )
        except self.ports.contract.GitLockError as error:
            return self.ports._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
        except (RuntimeError, OSError) as error:
            return self.ports._recover(
                RecoveryReason.STATUS_FAILED, str(error), context=context
            )
        kind = getattr(getattr(result, 'kind', None), 'value', None)
        if kind == 'stale':
            return self.ports._recover(
                RecoveryReason.CATALOG_CACHE_STALE,
                'ExecPlan catalog revision이 변경되어 다시 평가합니다.',
                context=context,
            )
        if kind == 'invalid-catalog':
            diagnostic = getattr(result, 'diagnostic', None)
            detail = str(getattr(diagnostic, 'message', None) or '원격 ExecPlan catalog가 유효하지 않습니다.')
            return self.ports._recover(
                RecoveryReason.CATALOG_INVALID, detail, context=context,
                user_targets=tuple(
                    path for path in (getattr(diagnostic, 'path', None),
                                      getattr(diagnostic, 'source', None)) if path
                ),
            )
        if str(getattr(result, 'revision_sha', '') or '') != latest_sha:
            return self.ports._recover(
                RecoveryReason.REMOTE_SNAPSHOT_RACE,
                '원격 base와 reconciled ExecPlan revision이 달라 다시 평가합니다.',
                context=context,
            )

        catalog_plan = self.ports.state.active_execplan(self.ports.identity.repo)
        if catalog_plan is None:
            return ExecutionOutcome(
                ExecutionOutcomeKind.OPERATIONAL_ERROR,
                detail='revision gate에서 활성 ExecPlan을 찾을 수 없습니다.',
                error=True,
            )
        self.ports.update_snapshot(catalog_plan=catalog_plan)
        self.ports.update_snapshot(plan=self.ports._cached_plan(catalog_plan))
        try:
            self.ports._document_draft(context)
            from loop_manager_internal.execution.manual_candidate import get_manual
            manual = get_manual(self.ports.state)
            if manual and manual.enabled():
                snapshot = manual.definition(context, catalog_plan, latest_sha)
                try:
                    manual.confirm_dependencies(context)
                except RuntimeError as error:
                    return ExecutionOutcome(ExecutionOutcomeKind.STABLE_WAIT,context=context,detail=str(error))
            else:
                observed_body = self.ports.body_hash(
                    self.ports.identity.repo_path, latest_sha, str(catalog_plan['path'])
                )
                snapshot = DefinitionSnapshot(
                    revision_sha=latest_sha,
                    path=str(catalog_plan['path']),
                    title=str(catalog_plan['title']),
                    sources=tuple(json.loads(str(catalog_plan['sources']))),
                    depends_on=tuple(json.loads(str(catalog_plan['depends_on']))),
                    complexity=str(catalog_plan['current_complexity'] or self.ports.snapshot().plan['complexity']),
                    risk=str(catalog_plan['current_risk'] or self.ports.snapshot().plan['risk']),
                    priority=str(catalog_plan['priority']),
                    body_hash=str(observed_body),
                    body_text=(observed_body.body_text
                               if isinstance(getattr(observed_body, 'body_text', None), str) else None),
                    source_fingerprint=self.ports.source_fingerprint(
                        self.ports.identity.repo_path, latest_sha, catalog_plan
                    ),
                    archive_candidates=(),
                    guidance_hash=common_guidance.plan_content_hash(
                        self.ports.state.connection, self.ports.identity.repo, str(catalog_plan['plan_id']),
                    ),
                    worker_guidance_hash=common_guidance.worker_content_hash(
                        self.ports.state.connection, self.ports.identity.repo, str(catalog_plan['plan_id']),
                    ),
                )
                if self.ports.checkpoint:
                    from loop_manager_internal.execution import stage_documents
                    branch_document = stage_documents.pending(self.ports.checkpoint, self.ports.identity.repo, context.plan.plan_id)
                    adoption = (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id).snapshot or {}).get('remote_adoption_scope')
                    if (branch_document and branch_document.get('work_branch') and isinstance(adoption, dict)
                            and adoption.get('remote') == branch_document['remote']):
                        head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
                        branch_body = self.ports.body_hash(context.worktree, head, snapshot.path)
                        snapshot = replace(snapshot,
                            body_hash=str(branch_body), body_text=getattr(branch_body, 'body_text', None),
                            source_fingerprint=self.ports.source_fingerprint(context.worktree, head, catalog_plan))
            previous = DefinitionSnapshot.deserialize(
                self.ports._row()['definition_snapshot']
            )
            stored_previous = previous
            if self.ports.checkpoint is not None:
                record = (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id).snapshot or {}).get('document_record')
                if record and record.get('applied') and isinstance(record.get('text'), str):
                    snapshot = record_only_definition(snapshot, record['text'])
            if previous is not None and previous.body_text is None and snapshot.body_text is not None:
                # Upgrade only from the original revision, never today's plan.
                original = self.ports.body_hash(
                    self.ports.identity.repo_path, previous.revision_sha, previous.path,
                )
                if original == previous.body_hash and isinstance(getattr(original, 'body_text', None), str):
                    old_token = previous.worker_token
                    legacy_token = previous.legacy_worker_token
                    previous = replace(previous, body_text=original.body_text)
                    if self.ports._row()['worker_input_token'] in {old_token, legacy_token}:
                        self.ports.state.update_project(
                            self.ports.identity.repo, worker_input_token=previous.worker_token,
                        )
            unmet = self.ports.unmet_dependencies(
                self.ports.identity.repo, str(catalog_plan['plan_id'])
            )
            changed_paths = self.ports.changed_paths(
                self.ports.identity.repo_path,
                str(self.ports._row()['active_sha'] or ''),
                latest_sha,
            )
        except RecoveryFailure:
            raise
        except self.ports.contract.GitLockError as error:
            return self.ports._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
        except (ExternalObservationError, OSError) as error:
            return self.ports._recover(RecoveryReason.STATUS_FAILED, str(error), context=context)
        except (RuntimeError, ValueError) as error:
            return self._revision_error(
                context, f"ExecPlan revision gate 확인 실패: {error}"
            )

        row = self.ports._row()
        if previous is not None and row['worker_input_token'] == previous.legacy_worker_token:
            self.ports.state.update_project(self.ports.identity.repo, worker_input_token=previous.worker_token)
            row = self.ports._row()
        active_sha = str(row['active_sha'] or '')
        document_revision = None
        if self.ports.refresh_documents is not None and self.ports.checkpoint is not None:
            try:
                document_revision = self.ports.refresh_documents(context, previous, snapshot, changed_paths)
                if isinstance(document_revision, ExecutionOutcome):
                    return document_revision
            except PlannerValidationError as error:
                return self._revision_error(context, str(error))
            except self.ports.contract.AgentScopeError as error:
                return self.ports._block_final(self.ports.identity.repo, context.worktree, str(error),
                    str(row['verify_result'] or ''), reason=BlockReasonCode.PLAN_INPUT_INVALID)
            except RecoveryFailure:
                raise
            except (RuntimeError, OSError) as error:
                return self.ports._recover(RecoveryReason.STATUS_FAILED, str(error), context=context)
            if document_revision and document_revision['base'] != latest_sha:
                return self.ports._retry('현재 ExecPlan 개정을 원격에 게시했습니다. 최신 문서와 작업 branch를 반영합니다.')
            if (document_revision and document_revision['stage'] == 'done'
                    and document_revision['document_only'] and not document_revision.get('decision_applied')):
                from loop_manager_internal.execution import stage_documents
                phase = document_revision['phase']
                if phase == 'PUBLISHING':
                    phase = 'REVIEWING'
                if phase == 'VERIFYING' and document_revision['assessment']['implementation'] == 'needed':
                    phase = 'IMPLEMENTING'
                values = {'phase': self._project_phase(phase), 'definition_snapshot': snapshot.serialize(),
                          'validated_base_sha': None, 'verify_token': None, 'review_token': None, 'publication_token': None}
                if phase == 'IMPLEMENTING':
                    values.update(worker_result=None, worker_input_token=None, archive_token=None)
                elif document_revision['worker_result'] is not None:
                    values.update(worker_result=document_revision['worker_result'],
                                  worker_input_token=snapshot.worker_token, archive_token=snapshot.archive_token)
                candidate = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
                with runtime_state._transaction(self.ports.state.connection):
                    self.ports.state.update_project(self.ports.identity.repo, preserve_transaction=True, **values)
                    current = self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                    self.ports.checkpoint.record(self.ports.identity.repo, context.plan.plan_id,
                        expected_generation=current.generation, operation=current.operation, expected_head=current.expected_head,
                        target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                        snapshot={**(current.snapshot or {}),
                            'document_revision': {**document_revision, 'decision_applied': True, 'review_candidate': candidate},
                            'worker_candidate_token': snapshot.worker_candidate_token(candidate)})
                self.ports.update_snapshot(_definition_snapshot=snapshot)
                return self.ports._retry('최신 문서의 영향 판단에 따라 현재 작업의 필요한 단계부터 이어갑니다.')
        # 문서 영향 판단 이후에도 기존 후보·Git·아카이브 경계를 적용한다.
        legacy_definition_changed = str(catalog_plan['path']) in changed_paths
        legacy_baseline = bool(
            previous is None
            and active_sha
            and not legacy_definition_changed
        )
        candidate_head = self.ports.git(
            context.worktree, 'rev-parse', 'HEAD'
        ).stdout.strip()
        worker_candidate_token = ((self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id).snapshot or {}).get('worker_candidate_token')
                                  if self.ports.checkpoint else None)
        if stored_previous is not None and previous is not None and worker_candidate_token in (
                stored_previous.worker_candidate_token(candidate_head),
                stored_previous.legacy_worker_candidate_token(candidate_head)):
            worker_candidate_token = previous.worker_candidate_token(candidate_head)
        from loop_manager_internal.storage import verify_receipts
        from loop_manager_internal.verification.inputs import InputObservation
        reused = verify_receipts.reused_candidate(self.ports.state.connection,
            self.ports.identity.repo, context.plan.plan_id, candidate_head,
            str(row['verify_result']) if row['verify_result'] is not None else None)
        reusable_now = True
        if reused is not None:
            observer = self.ports.observe_verify_inputs
            try:
                observed = observer(context.worktree) if observer else None
                reusable_now = (isinstance(observed, InputObservation) and observed.proven
                                and observed.fingerprint == reused['fingerprint'])
            except (OSError, RuntimeError, ValueError):
                reusable_now = False
        from loop_manager_internal.execution import preliminary_review
        preliminary_pending = preliminary_review.active(self.ports, context, {'partial_pass', 'review_running'})
        decision = decide_revision_gate(
            phase=ExecutionPhase(str(catalog_plan['status'])).value,
            # Rows created before G-037 have valid phase artifacts but no
            # revision tokens. Adopt the reconciled definition as their
            # baseline, then let real base and definition deltas invalidate
            # only the stages that must run again.
            previous=snapshot if legacy_baseline else previous,
            current=snapshot,
            active_base_token=active_sha,
            current_base_token=latest_sha,
            artifacts=StageArtifacts(
                preliminary_review_pending=bool(preliminary_pending),
                document_review_pending=bool(self.ports.checkpoint and document_revision
                    and document_revision['stage'] == 'done'
                    and document_revision.get('verification_pending')
                    and document_revision.get('review_candidate') == candidate_head
                    and document_revision.get('definition') == snapshot.definition_token),
                manual_submission=bool(manual and manual.user_submission(row)),
                require_worker_candidate=bool(self.ports.checkpoint and
                    (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id).snapshot or {}).get('current_rebase_acceptance')),
                worker_candidate_token=worker_candidate_token,
                worker_result=row['worker_result'] is not None,
                verify_result=reusable_now and (manual.valid_evidence(context,'verify',row['verify_result'])
                               if manual and manual.enabled() else row['verify_result'] is not None),
                verify_output=(str(row['verify_result'])
                               if row['verify_result'] is not None else None),
                review_result=(manual.valid_evidence(context,'review',row['review_result'])
                               if manual and manual.enabled() else row['review_result'] is not None),
                worker_input_token=(
                    snapshot.worker_token
                    if legacy_baseline and row['worker_result'] is not None
                    else str(row['worker_input_token'])
                    if row['worker_input_token'] is not None
                    else None
                ),
                archive_token=(
                    snapshot.archive_token
                    if legacy_baseline and row['worker_result'] is not None
                    else str(row['archive_token'])
                    if row['archive_token'] is not None
                    else None
                ),
                verify_token=(
                    str(row['verify_token'])
                    if row['verify_token'] is not None
                    else None
                ),
                review_token=(
                    str(row['review_token'])
                    if row['review_token'] is not None
                    else None
                ),
                publication_token=(str(row['publication_token']) if row['publication_token'] is not None else None),
            ),
            unmet_dependencies=tuple(unmet),
            candidate_head=candidate_head,
        )
        self.ports.update_snapshot(
            archive_sources=(),
            archive_candidates=(),
            archive_scope_sources=(),
            _archive_snapshot=None,
        )
        if decision.kind is RevisionGateKind.FINAL_SAFETY_ERROR:
            return self._revision_error(context, str(decision.detail))
        if decision.kind is RevisionGateKind.WAIT:
            # A conflicted rebase cannot accept a second refresh or clear its
            # recovery marker. Still reconcile dependencies before Worker.
            if not context.resolving_rebase:
                self._install_revision_state(
                    snapshot,
                    decision,
                    latest_sha,
                    legacy_baseline=legacy_baseline,
                )
            self.ports.operation_log(str(decision.detail))
            return ExecutionOutcome(
                ExecutionOutcomeKind.STABLE_WAIT,
                context=context if context.resolving_rebase else self.ports._context(),
                detail=decision.detail,
            )
        if (document_revision and document_revision['stage'] == 'rebase'
                and document_revision['document_only'] and not context.resolving_rebase):
            decision = replace(decision, kind=RevisionGateKind.REFRESH_WORKTREE,
                               phase=document_revision['phase'])
        if context.resolving_rebase:
            if (
                (previous is None and not legacy_baseline)
                or decision.deltas & WORKER_INVALIDATING_DELTAS
            ):
                # Keep the applied definition and rebase target until recovery
                # completes; changed inputs require fresh Worker validation.
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    worker_result=None,
                    verify_result=None,
                    review_result=None,
                    worker_input_token=None,
                    archive_token=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                )
            # The ongoing Git rebase remains pinned to its original target.
            # Do not install the new stage state until it has been resolved.
            self.ports.update_snapshot(_definition_snapshot=snapshot)
            self.ports.update_snapshot(level=self.ports.ensure_worker_level_state(
                self.ports.identity.repo, self.ports.snapshot().plan
            ))
            return self.ports._context()
        if str(row['active_sha'] or '') != latest_sha:
            return self.ports._refresh_revision_base(
                context,
                snapshot,
                decision,
                latest_sha,
                legacy_baseline=legacy_baseline,
            )
        self._install_revision_state(
            snapshot,
            decision,
            latest_sha,
            legacy_baseline=legacy_baseline,
        )
        if decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE:
            return self.ports._retry('ExecPlan revision 변경으로 필요한 실행 단계부터 재개합니다.')
        try:
            self.ports.update_snapshot(plan=self.ports.validate_active_execplan_metadata(
                self.ports.identity.repo_path,
                context.worktree,
                str(self.ports._row()['plan_path']),
                self.ports.snapshot().catalog_plan,
            ))
            self.ports.update_snapshot(level=self.ports.ensure_worker_level_state(
                self.ports.identity.repo, self.ports.snapshot().plan
            ))
        except RecoveryFailure:
            raise
        except (ExternalObservationError, OSError) as error:
            return self.ports._recover(RecoveryReason.STATUS_FAILED, str(error), context=context)
        except RuntimeError as error:
            return self._revision_error(context, str(error))
        return self.ports._context()

    def _post_effect_guard(self, context):
        # Direct policy unit tests can still exercise a handler without the
        # coordinator. Real execution always installs this snapshot first.
        if self.ports.snapshot()._definition_snapshot is None:
            return context
        return self.ports.guard(self.ports._context())

    def _metadata_correction(self, context, error, *, verify_result=None):
        detail = f"ExecPlan metadata 검증 실패:\n{error}"
        if context.resolving_rebase:
            detail = self.ports.contract.REBASE_CONFLICT_PREFIX + detail
        values = {'phase': 'WORKING', 'last_error': detail}
        if verify_result is not None:
            values['verify_result'] = verify_result
        self.ports.state.update_project(self.ports.identity.repo, **values)
        self.ports.operation_print(detail, file=sys.stderr)
        return self.ports._recover(
            RecoveryReason.PLAN_INPUT_INVALID, detail, context=context,
            verify_result=verify_result,
            final_block_reason=BlockReasonCode.PLAN_INPUT_INVALID,
        )
