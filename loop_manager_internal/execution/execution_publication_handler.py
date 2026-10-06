"""Publication execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.execution.execution_runtime import LatestBasePort
from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping
import hashlib
import json
from loop_manager_internal.documents import document_drafts
from loop_manager_internal.git import manager_git_journal
import sys
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure, classify_exception
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind, LatestBaseCheckpoint
from loop_manager_internal.execution.publication_resume import PublicationNext, PublicationResume, assess_publication

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository

@dataclass(frozen=True)
class PublicationHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    checkpoint: ExecutionCheckpointRepository | None
    contract: ExecutionContract
    state: ExecutionStatePort
    branch_has_changes: Callable[..., Any]
    build_pr_body: Callable[..., Any]
    command: Callable[..., Any]
    git: Callable[..., Any]
    live_remote_branch_oid: Callable[..., Any] | None
    operation_print: Callable[..., Any]
    parse_worker_result: Callable[..., Any]
    pr_for_branch: Callable[..., Any]
    published_execplan: Callable[..., Any]
    push_managed_publication_branch: Callable[..., Any]
    push_owned_pr_conflict_recovery: Callable[..., Any]
    reconcile_existing_pull_request: Callable[..., Any]
    record_remote_publication_branch: Callable[..., Any]
    requirement_archive_move_set: Callable[..., Any]
    requirement_archive_reconciliation_error: Callable[..., Any]
    validate_execplan_workspace_identity: Callable[..., Any]
    validate_guidance_publication: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    _archive_candidates_at: Callable[..., tuple[str, ...]]
    _archive_collision: Callable[..., ExecutionOutcome]
    _block_final: Callable[..., ExecutionOutcome]
    _context: Callable[[], ExecutionContext]
    _document_block: Callable[..., ExecutionOutcome]
    _document_draft: Callable[..., Any]
    _document_guard: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _latest_before_publication: Callable[..., Any]
    _owned_checkpoint_remote: Callable[..., Any]
    _post_effect_guard: Callable[[ExecutionContext], ExecutionContext | ExecutionOutcome]
    _record_archive_target: Callable[..., None]
    _recover: Callable[..., ExecutionOutcome]
    _retry: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]
    _step_outcome: Callable[..., Any]


class PublicationHandler:
    """Publication policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: PublicationHandlerPorts):
        self.ports = ports

    def _publication_intent(self, context, operation, remote_oid):
        checkpoint = self.ports.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        if current.operation == operation and current.expected_head == head:
            return
        checkpoint.record(
            self.ports.identity.repo, context.plan.plan_id,
            expected_generation=current.generation, operation=operation,
            expected_head=head, target_base_oid=context.active_sha,
            observed_remote_oid=remote_oid,
            snapshot={**(current.snapshot or {}), 'phase': 'PUBLISHING',
                      'head': head, 'remote_before': remote_oid},
        )

    def _commit_owned(self, context, title, *, worker_confirmed=False):
        checkpoint = self.ports.checkpoint
        if checkpoint is not None:
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            before = self.ports.git(
                context.worktree, 'rev-parse', 'HEAD'
            ).stdout.strip()
            approved_tree = self.ports.git(
                context.worktree, 'write-tree',
            ).stdout.strip()
            checkpoint.record(
                self.ports.identity.repo, context.plan.plan_id,
                expected_generation=current.generation, operation='commit',
                expected_head=before, target_base_oid=context.active_sha,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}),
                          'commit_message': title, 'before_head': before,
                          'commit_tree_oid': approved_tree},
            )
        result = self.ports.git(
            context.worktree, 'commit', '-m', title,
        )
        if checkpoint is not None:
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            after = self.ports.git(
                context.worktree, 'rev-parse', 'HEAD'
            ).stdout.strip()
            checkpoint.record(
                self.ports.identity.repo, context.plan.plan_id,
                expected_generation=current.generation, operation=None,
                expected_head=after, target_base_oid=current.target_base_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'after_head': after,
                          **({'worker_candidate_token': self.ports.snapshot()._definition_snapshot.worker_candidate_token(after)}
                             if worker_confirmed and self.ports.snapshot()._definition_snapshot is not None
                             and (current.snapshot or {}).get('worker_candidate_token') ==
                                 self.ports.snapshot()._definition_snapshot.worker_candidate_token(before) else {})},
            )
        return result

    def _commit_rebase_resolution(self, context, title):
        """Keep the rebase intent durable across every resolution commit boundary."""
        checkpoints = self.ports.checkpoint
        pending = checkpoints.get(self.ports.identity.repo, context.plan.plan_id)
        if pending.operation != 'rebase':
            raise manager_git_journal.MutationEvidenceError('resolution lost its rebase intent')
        git = self.ports.git
        audit = {
            'before_head': git(context.worktree, 'rev-parse', 'HEAD').stdout.strip(),
            'tree': git(context.worktree, 'write-tree').stdout.strip(),
            'message': title,
        }
        pending = checkpoints.record(
            self.ports.identity.repo, context.plan.plan_id, expected_generation=pending.generation,
            operation='rebase', expected_head=pending.expected_head,
            target_base_oid=pending.target_base_oid,
            observed_remote_oid=pending.observed_remote_oid,
            snapshot={**(pending.snapshot or {}), 'rebase_resolution_commit': audit})
        git(context.worktree, 'commit', '-m', title)
        after = git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        checkpoints.record(
            self.ports.identity.repo, context.plan.plan_id, expected_generation=pending.generation,
            operation='rebase', expected_head=pending.expected_head,
            target_base_oid=pending.target_base_oid,
            observed_remote_oid=pending.observed_remote_oid,
            snapshot={**(pending.snapshot or {}), 'after_head': after,
                      'rebase_resolution_commit': {**audit, 'after_head': after}})

    def _complete_publication_intent(self, context):
        checkpoint = self.ports.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        if current.operation not in {'push', 'pr_create'}:
            return
        checkpoint.record(
            self.ports.identity.repo, context.plan.plan_id,
            expected_generation=current.generation, operation=None,
            expected_head=current.expected_head,
            target_base_oid=current.target_base_oid,
            observed_remote_oid=(current.expected_head if current.operation == 'push'
                                 else current.observed_remote_oid),
            snapshot={**(current.snapshot or {}),
                      **({'pushed_checkpoint_head': current.expected_head}
                         if current.operation == 'push' and
                         ((current.snapshot or {}).get('blocked_resume') or
                          (current.snapshot or {}).get('published_review_resume') or
                          (current.snapshot or {}).get('remote_adoption_scope') or
                          (current.snapshot or {}).get('current_rebase_acceptance') or
                          current.completed_rebase()) else {})},
        )

    def _publication_resume(self):
        return PublicationResume(
            self.ports.pr_for_branch,
            self.ports.live_remote_branch_oid or (
                lambda repo, branch: manager_git_journal.live_remote_branch_oid(
                    self.ports.git, repo, branch)),
            self.ports.git,
            self._publication_workspace,
        )

    def _reconcile_terminal_publication(self, context):
        """Resolve a completed PR before the revision gate can rebase its head."""
        row = self.ports._row()
        pr_url = str(row['pr_url'] or '') or None
        try:
            pr = self.ports.pr_for_branch(
                self.ports.identity.repo_path, context.branch, pr_url,
            )
            if pr is None or str(pr.get('state') or '').upper() not in {'MERGED', 'CLOSED'}:
                return None
            # Reuse the live observation boundary, including workspace identity,
            # candidate tokens and a second head check across the PR query.
            observed = self._publication_resume().observe(
                self.ports.identity.repo_path, context.worktree, context.branch,
                str(row['publication_remote_oid'] or '') or None, pr_url,
            )
            pr = observed.pull_request
            if pr is None or str(pr.get('state') or '').upper() not in {'MERGED', 'CLOSED'}:
                return None
            if assess_publication(observed) is not PublicationNext.ATTACH_PR:
                raise self.ports.contract.PublicationSafetyError(
                    '종료 PR의 branch 또는 head가 관리 게시 후보와 다릅니다.'
                )
            reconciliation = self.ports.reconcile_existing_pull_request(
                self.ports.identity.repo_path, self.ports.identity.state_dir, self.ports.identity.repo, row, pr,
            )
            return (self.ports._step_outcome(reconciliation) if reconciliation is not None
                    else self.ports._retry('종료 PR 상태를 다시 확인합니다.'))
        except (self.ports.contract.PublicationSafetyError, self.ports.contract.WorkspaceOwnershipError) as error:
            return self.ports._block_final(
                self.ports.identity.repo, context.worktree, str(error),
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )

    def _publication_workspace(self, repo, worktree, branch, expected_oid):
        row = self.ports._row()
        self.ports.validate_execplan_workspace_identity(
            self.ports.identity.state_dir, self.ports.identity.repo, self.ports.state.plan_id, branch, worktree)
        self.ports.validate_managed_worktree(repo, worktree, branch)
        head = self.ports.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        if expected_oid is not None and head != expected_oid:
            raise self.ports.contract.PublicationSafetyError('게시 관측 중 관리 HEAD가 변경됐습니다.')
        if self.ports.snapshot()._definition_snapshot is not None and (
                row['verify_token'] != self.ports.snapshot()._definition_snapshot.verify_token(str(row['validated_base_sha'] or ''), head)
                or row['review_token'] != self.ports.snapshot()._definition_snapshot.review_token(str(row['validated_base_sha'] or ''), head)):
            raise self.ports.contract.PublicationSafetyError('게시 HEAD가 verify·Review를 통과한 후보와 다릅니다.')

    def publish(self, context: ExecutionContext, latest_base: LatestBasePort) -> ExecutionOutcome:
        self.ports.state.check_context(context)
        row = self.ports._row()
        assert self.ports.snapshot().catalog_plan is not None and self.ports.snapshot().plan is not None
        draft = self.ports._document_draft(context)
        if draft is not None:
            try:
                self.ports._fetch_latest_base()
                self.ports._document_guard(context, draft)
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                return self.ports._document_block(context, draft, error)
        try:
            publication = self._publication_resume().observe(
                self.ports.identity.repo_path, context.worktree, context.branch,
                str(row['publication_remote_oid'] or '') or None,
                str(row['pr_url'] or '') or None,
            )
        except (self.ports.contract.PublicationSafetyError, self.ports.contract.WorkspaceOwnershipError) as error:
            return self.ports._block_final(self.ports.identity.repo, context.worktree, str(error),
                str(row['verify_result'] or ''), reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
        current = (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                   if self.ports.checkpoint else None)
        if not self.ports._owned_checkpoint_remote(
            context, row, publication.remote_oid, current,
            publication.pull_request,
        ):
            return self.ports._block_final(
                self.ports.identity.repo, context.worktree,
                '게시 checkpoint 원격 OID 소유권을 다시 증명할 수 없습니다.',
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
        existing_pr = publication.pull_request
        # A saved PR remains at the last owned checkpoint while the Worker
        # prepares its next candidate. The push gate checks that exact owner.
        saved_head = (row['pr_conflict_head_oid'] if int(row['pr_conflict_recovery'])
                      else row['pr_head_oid'])
        owned_pr = bool(existing_pr and row['pr_url']
            and existing_pr.get('url') == row['pr_url']
            and saved_head == row['publication_remote_oid'])
        attachment = (replace(publication, local_oid=str(saved_head or ''))
                      if int(row['pr_conflict_recovery']) or owned_pr else publication)
        if assess_publication(attachment) is PublicationNext.USER_ACTION:
            return self.ports._block_final(
                self.ports.identity.repo, context.worktree,
                '원격 branch 또는 PR head가 관측한 소유 OID와 다릅니다.',
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
        if existing_pr and (not owned_pr
                or str(existing_pr.get('state') or 'OPEN').upper() != 'OPEN'):
            try:
                reconciliation = self.ports.reconcile_existing_pull_request(
                    self.ports.identity.repo_path,
                    self.ports.identity.state_dir,
                    self.ports.identity.repo,
                    row,
                    existing_pr,
                )
                if reconciliation is not None:
                    return self.ports._step_outcome(reconciliation)
                row = self.ports._row()
            except self.ports.contract.PublicationSafetyError as error:
                publication_error = f"기존 PR 복구 거부: {error}"
                self.ports._block_final(
                    self.ports.identity.repo,
                    context.worktree,
                    publication_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                self.ports.operation_print(publication_error, file=sys.stderr)
                return self.ports._final(publication_error)
        latest_outcome = latest_base.reconcile(context, LatestBaseCheckpoint.BEFORE_PUBLICATION)
        if latest_outcome is not None:
            return latest_outcome
        if draft is not None:
            try:
                self.ports._document_guard(self.ports._context(), draft)
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                return self.ports._document_block(context, draft, error)
        row = self.ports._row()
        latest_sha = str(row['active_sha'] or '')
        if context.archive_sources:
            try:
                final_archive_candidates = self.ports._archive_candidates_at(
                    context, latest_sha
                )
            except RecoveryFailure as error:
                return self.ports._recover(error.reason, str(error), context=context)
            except self.ports.contract.RequirementArchiveCollisionError as error:
                return self.ports._archive_collision(context, error)
            except self.ports.contract.RequirementArchiveError as error:
                archive_error = f"게시 직전 Requirement archive eligibility 확인 실패: {error}"
                if self.ports._fetch_latest_base() != latest_sha:
                    return self.ports._recover(
                        RecoveryReason.REMOTE_SNAPSHOT_RACE,
                        archive_error, context=context,
                    )
                outcome = self.ports._recover(
                    RecoveryReason.CATALOG_INVALID,
                    archive_error, context=context,
                )
                self.ports.operation_print(archive_error, file=sys.stderr)
                return outcome
            try:
                final_archive_moves = self.ports.requirement_archive_move_set(
                    self.ports.identity.repo_path,
                    latest_sha,
                    context.worktree,
                    tracked_revision='HEAD',
                )
            except self.ports.contract.GitLockError as error:
                return self.ports._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
            except self.ports.contract.RequirementArchiveCollisionError as error:
                return self.ports._archive_collision(context, error)
            except self.ports.contract.RequirementArchiveError as error:
                archive_error = (
                    f"게시 직전 Requirement archive 확인 실패: {error}\n"
                    f"기준 revision: {latest_sha}; "
                    f"기대: {json.dumps(final_archive_candidates, ensure_ascii=False)}"
                )
                self.ports.state.update_project(
                    self.ports.identity.repo, phase='WORKING', validated_base_sha=None,
                    worker_input_token=None, archive_token=None,
                    verify_token=None, review_token=None,
                    publication_token=None, last_error=archive_error,
                )
                self.ports.operation_print(
                    '게시 직전 Requirement archive 이동을 다시 확인합니다.',
                    file=sys.stderr,
                )
                try:
                    self.ports._record_archive_target('CORRECTION_PENDING')
                except RecoveryFailure as race:
                    return self.ports._recover(race.reason, str(race), context=context)
                return self.ports._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                )
            archive_error = self.ports.requirement_archive_reconciliation_error(
                final_archive_candidates,
                final_archive_moves,
                revision=latest_sha,
            )
            if archive_error is not None:
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    worker_input_token=None,
                    archive_token=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                    last_error=archive_error,
                )
                self.ports.operation_print(
                    '게시 직전 Requirement archive 이동 집합을 다시 맞춥니다.',
                    file=sys.stderr,
                )
                try:
                    self.ports._record_archive_target(
                        'CORRECTION_PENDING', moves=final_archive_moves
                    )
                except RecoveryFailure as error:
                    return self.ports._recover(error.reason, str(error), context=context)
                return self.ports._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                    fingerprint=hashlib.sha256(
                        json.dumps((latest_sha, context.archive_snapshot_token,
                                    sorted(final_archive_candidates or ()), sorted(final_archive_moves or ()))).encode()
                    ).hexdigest(),
                )
            try:
                self.ports._record_archive_target('VALIDATED', moves=final_archive_moves)
            except RecoveryFailure as error:
                return self.ports._recover(error.reason, str(error), context=context)
        if self.ports.validate_guidance_publication(
            self.ports.identity.repo,
            context.worktree,
            self.ports.snapshot().catalog_plan,
            str(row['worker_result'] or ''),
        ):
            return self.ports._final()
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        row = self.ports._row()
        if not self.ports.branch_has_changes(context.worktree, self.ports.identity.base):
            no_change_error = '게시할 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
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
        if self.ports.snapshot()._definition_snapshot is not None:
            self.ports.state.update_project(
                self.ports.identity.repo,
                publication_token=self.ports.snapshot()._definition_snapshot.publication_token(
                    str(self.ports._row()['validated_base_sha'] or ''),
                    self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip(),
                ),
            )
            context = self.ports._context()
        if row['pr_url']:
            return self._publish_existing_pull_request(context, row, publication.local_oid)
        return self._publish_new_pull_request(context, row)

    def _publish_existing_pull_request(self, context, row, expected_oid):
        current_pr = self.ports.pr_for_branch(self.ports.identity.repo_path, context.branch, str(row['pr_url'] or ''))
        if current_pr is None:
            publication_error = '게시 직전 저장된 기존 PR을 다시 조회할 수 없습니다.'
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
            self.ports.operation_print(publication_error, file=sys.stderr)
            return self.ports._final(publication_error)
        if (int(row['pr_conflict_recovery'])
                or str(current_pr.get('state') or '').upper() != 'OPEN'):
            reconciliation = self.ports.reconcile_existing_pull_request(
                self.ports.identity.repo_path,
                self.ports.identity.state_dir,
                self.ports.identity.repo,
                row,
                current_pr,
            )
            if reconciliation is not None:
                return self.ports._step_outcome(reconciliation)
        row = self.ports._row()
        try:
            if not int(row['pr_conflict_recovery']):
                self._publication_intent(context, 'push', row['publication_remote_oid'])
            head_oid = self.ports.push_owned_pr_conflict_recovery(
                self.ports.identity.repo_path,
                self.ports.identity.state_dir,
                self.ports.identity.repo,
                context.branch,
                context.worktree,
                current_pr,
                expected_oid,
            )
        except (self.ports.contract.PublicationSafetyError, self.ports.contract.WorkspaceOwnershipError) as error:
            publication_error = str(error)
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
            self.ports.operation_print(publication_error, file=sys.stderr)
            return self.ports._final(publication_error)
        except RuntimeError as error:
            publication_error = str(error)
            self.ports.state.update_project(self.ports.identity.repo, phase='PUBLISHING', last_error=publication_error)
            self.ports.operation_print(publication_error, file=sys.stderr)
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                publication_error, context=context,
            )
        if not int(row['pr_conflict_recovery']):
            # Confirm the physical push before a newer definition/base can
            # invalidate its approval and return the candidate to the Worker.
            self.ports.state.confirm_existing_pr_push(self.ports.checkpoint, head_oid)
            context = self.ports._context()
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        self.ports.state.open_pull_request(
            self.ports.identity.repo,
            publication_remote_oid=head_oid,
            pr_head_oid=head_oid,
            pr_conflict_recovery=0,
            pr_conflict_head_oid=None,
            pr_conflict_base_oid=None,
        )
        self.ports.operation_print(f"기존 PR 후보 게시 완료: {row['pr_url']}")
        return ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)

    def _publish_new_pull_request(self, context, row):
        assert self.ports.snapshot().plan is not None
        assert self.ports.snapshot().catalog_plan is not None
        plan_sha = str(
            self.ports.snapshot().catalog_plan['publication_sha']
            or self.ports.snapshot().catalog_plan['plan_sha']
            or ''
        )
        publication_path = str(
            self.ports.snapshot().catalog_plan['publication_path']
            or self.ports.snapshot().catalog_plan['path']
        )
        if not plan_sha:
            plan_error = '계획 commit SHA가 없습니다.'
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                plan_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.ports.operation_print(plan_error, file=sys.stderr)
            return self.ports._final(plan_error)
        plan_text = self.ports.git(
            context.worktree, 'show', f"{plan_sha}:{publication_path}"
        ).stdout
        self.ports.published_execplan(publication_path, plan_text)
        try:
            worker_publication = self.ports.parse_worker_result(str(row['worker_result'] or ''), self.ports.snapshot().plan)
        except self.ports.contract.WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            self.ports.state.update_project(
                self.ports.identity.repo,
                phase='WORKING',
                validated_base_sha=None,
                last_error=validation_error,
            )
            self.ports.operation_print(validation_error, file=sys.stderr)
            return self.ports._recover(
                RecoveryReason.WORKER_CORRECTION, validation_error,
                context=context,
            )
        origin_url = self.ports.git(context.worktree, 'remote', 'get-url', 'origin').stdout.strip()
        body = self.ports.build_pr_body(
            worker_publication,
            self.ports.snapshot().plan,
            publication_path,
            plan_text,
            origin_url,
            plan_sha,
        )
        try:
            publication = self._publication_resume().observe(
                self.ports.identity.repo_path, context.worktree, context.branch,
                str(row['publication_remote_oid'] or '') or None,
            )
            current = (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                       if self.ports.checkpoint else None)
            if not self.ports._owned_checkpoint_remote(
                context, row, publication.remote_oid, current,
                publication.pull_request,
            ):
                raise self.ports.contract.PublicationSafetyError(
                    '게시 직전 checkpoint 원격 OID 소유권을 증명할 수 없습니다.'
                )
            action = assess_publication(publication)
            if action is PublicationNext.USER_ACTION:
                raise self.ports.contract.PublicationSafetyError(
                    '게시 직전 원격 branch 또는 PR head가 변경됐습니다.'
                )
            if action is PublicationNext.ATTACH_PR:
                reconciliation = self.ports.reconcile_existing_pull_request(
                    self.ports.identity.repo_path, self.ports.identity.state_dir, self.ports.identity.repo, row,
                    publication.pull_request,
                )
                return (self.ports._step_outcome(reconciliation) if reconciliation is not None
                        else self.ports._retry('기존 PR을 연결했습니다.'))
            if action is PublicationNext.CREATE_PR:
                self.ports.record_remote_publication_branch(
                    self.ports.identity.repo_path, self.ports.identity.state_dir, self.ports.identity.repo,
                    context.branch, context.worktree, publication.remote_oid,
                )
                head_oid = publication.local_oid
            else:
                self._publication_intent(
                    context, 'push', publication.remote_oid,
                )
                head_oid = self.ports.push_managed_publication_branch(
                    self.ports.identity.repo_path, self.ports.identity.state_dir, self.ports.identity.repo,
                    context.branch, context.worktree, publication.local_oid,
                )
                self._complete_publication_intent(context)
        except (self.ports.contract.PublicationSafetyError, self.ports.contract.WorkspaceOwnershipError) as error:
            publication_error = str(error)
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
            self.ports.operation_print(publication_error, file=sys.stderr)
            return self.ports._final(publication_error)
        except RuntimeError as error:
            publication_error = str(error)
            self.ports.state.update_project(self.ports.identity.repo, phase='PUBLISHING', last_error=publication_error)
            self.ports.operation_print(publication_error, file=sys.stderr)
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                publication_error, context=context,
            )
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        try:
            latest_base = self.ports._fetch_latest_base()
            after_push = self._publication_resume().observe(
                self.ports.identity.repo_path, context.worktree, context.branch, head_oid,
            )
        except (self.ports.contract.PublicationSafetyError, self.ports.contract.WorkspaceOwnershipError) as error:
            return self.ports._block_final(self.ports.identity.repo, context.worktree, str(error),
                str(self.ports._row()['verify_result'] or ''), reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
        if after_push.local_oid != head_oid:
            return self.ports._block_final(self.ports.identity.repo, context.worktree,
                'PR 생성 직전 관리 HEAD가 검증한 게시 commit과 다릅니다.',
                str(self.ports._row()['verify_result'] or ''), reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
        existing_after_push = after_push.pull_request
        if (existing_after_push is not None
                and assess_publication(after_push) is not PublicationNext.ATTACH_PR):
            return self.ports._block_final(
                self.ports.identity.repo, context.worktree,
                'push 뒤 PR head가 검증한 게시 commit과 다릅니다.',
                str(self.ports._row()['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
        # GitHub's terminal PR state remains authoritative after automatic
        # head-branch deletion and base advancement. Reconcile it before
        # considering another rebase; active publication still needs its live ref.
        terminal_pr = (existing_after_push is not None
                       and str(existing_after_push.get('state') or '').upper()
                       in {'CLOSED', 'MERGED'})
        if not terminal_pr:
            if latest_base != self.ports._row()['validated_base_sha']:
                return self.ports._latest_before_publication(context) or self.ports._retry('최신 base를 다시 검증합니다.')
            if after_push.remote_oid != head_oid:
                return self.ports._block_final(self.ports.identity.repo, context.worktree,
                    'PR 생성 직전 원격 게시 ref가 변경됐습니다.',
                    str(self.ports._row()['verify_result'] or ''), reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
            current = (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                       if self.ports.checkpoint else None)
            if not self.ports._owned_checkpoint_remote(
                context, self.ports._row(), after_push.remote_oid, current,
                existing_after_push,
            ):
                return self.ports._block_final(
                    self.ports.identity.repo, context.worktree,
                    'push 뒤 checkpoint branch의 원격 OID 또는 PR 소유권을 증명할 수 없습니다.',
                    str(self.ports._row()['verify_result'] or ''),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
        if existing_after_push is not None:
            reconciliation = self.ports.reconcile_existing_pull_request(
                self.ports.identity.repo_path, self.ports.identity.state_dir, self.ports.identity.repo, self.ports._row(),
                existing_after_push,
            )
            return (self.ports._step_outcome(reconciliation) if reconciliation is not None
                    else self.ports._retry('push 뒤 기존 PR을 연결했습니다.'))
        title = f"Implement {self.ports.snapshot().plan['id']}: {self.ports.snapshot().plan['title']}"
        try:
            self._publication_workspace(self.ports.identity.repo_path, context.worktree, context.branch, head_oid)
            self._publication_intent(context, 'pr_create', head_oid)
            created = self.ports.command(
                ['gh', 'pr', 'create', '--base', self.ports.identity.base, '--head', context.branch, '--title', title, '--body', body],
                cwd=context.worktree,
            )
        except (self.ports.contract.PublicationSafetyError, self.ports.contract.WorkspaceOwnershipError) as error:
            return self.ports._block_final(self.ports.identity.repo, context.worktree, str(error),
                str(self.ports._row()['verify_result'] or ''), reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
        except RuntimeError as error:
            publication_error = str(error)
            self.ports.state.update_project(
                self.ports.identity.repo, phase='PUBLISHING', last_error=publication_error,
            )
            self.ports.operation_print(publication_error, file=sys.stderr)
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                publication_error, context=context,
            )
        url = created.stdout.strip().splitlines()[-1]
        self.ports.state.open_pull_request(self.ports.identity.repo, pr_url=url, pr_head_oid=head_oid)
        self._complete_publication_intent(context)
        self.ports.operation_print(f"PR 생성 완료: {url}")
        return ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)

