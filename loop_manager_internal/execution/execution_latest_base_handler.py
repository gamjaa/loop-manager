"""Latest Base execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.runtime import operation_events as events
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome
from dataclasses import dataclass
from typing import Callable, Any, Mapping
from loop_manager_internal.git import managed_rebase_adoption
import sys
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure
from loop_manager_internal.execution.execution_runtime import LatestBaseCheckpoint

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort

@dataclass(frozen=True)
class LatestBaseHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    contract: ExecutionContract
    state: ExecutionStatePort
    _rebase_in_progress_for_branch: Callable[..., Any]
    branch_has_changes: Callable[..., Any]
    git: Callable[..., Any]
    operation_print: Callable[..., Any]
    record_remote_publication_branch: Callable[..., Any]
    _block_final: Callable[..., ExecutionOutcome]
    _checkpoint_remote_oid: Callable[..., Any]
    _complete_rebase_intent: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _latest_before_publication: Callable[..., Any]
    _post_rebase_observation: Callable[..., Any]
    _rebase_latest: Callable[..., Any]
    _record_rebase_intent: Callable[..., Any]
    _recover: Callable[..., ExecutionOutcome]
    _retry: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]
    _run_managed_rebase: Callable[..., Any]


class LatestBaseHandler:
    """Latest Base policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: LatestBaseHandlerPorts):
        self.ports = ports

    def reconcile(self, context: ExecutionContext, checkpoint: LatestBaseCheckpoint,
                  observed_base_sha: str | None = None) -> ExecutionOutcome | None:
        if checkpoint is LatestBaseCheckpoint.BEFORE_VERIFY:
            return self._latest_before_verify(context, observed_base_sha)
        return self.ports._latest_before_publication(context)

    def _fetch_latest_base(self):
        try:
            with resource_locks(self.ports.identity.state_dir, str(self.ports.identity.repo_path.resolve()),
                                ['git:fetch'], owner='latest-base-fetch'):
                self.ports.git(self.ports.identity.repo_path, 'fetch', 'origin')
            return self.ports.git(
                self.ports.identity.repo_path, 'rev-parse', f"origin/{self.ports.identity.base}"
            ).stdout.strip()
        except self.ports.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.FETCH_FAILED, str(error)) from error

    def _rebase_latest(self, context, row, latest_sha, *, publishing):
        """Persist rebase intent, observe Git, then record the applied base."""
        prefix = '게시 직전 ' if publishing else ''
        events.emit("최신 base 반영 시작", old_base=row['active_sha'], target_base=latest_sha)
        self.ports.state.update_project(
            self.ports.identity.repo,
            phase='WORKING',
            validated_base_sha=None,
            revision_resume_status='VERIFYING',
            pr_conflict_base_oid=latest_sha if int(row['pr_conflict_recovery']) else row['pr_conflict_base_oid'],
            last_error=self.ports.contract.REBASE_IN_PROGRESS_ERROR,
        )
        self.ports._record_rebase_intent(context, latest_sha)
        with events.activity("최신 base rebase", heartbeat=True) as report:
            rebased = self.ports._run_managed_rebase(context, latest_sha)
            report.update(result_code=rebased.returncode, result="Git 결과 수신")
        conflict = self.ports._post_rebase_observation(context, latest_sha, rebased.stdout)
        if conflict is not None:
            return conflict
        if rebased.returncode:
            in_progress = self.ports._rebase_in_progress_for_branch(context.worktree, context.branch)
            if in_progress:
                rebase_failure = self.ports.contract.REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:]
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    last_error=rebase_failure,
                )
            else:
                rebase_failure = f"{prefix}rebase 실패:\n{rebased.stdout[-8000:]}"
            self.ports.operation_print(
                rebase_failure if publishing else rebased.stdout,
                file=sys.stderr,
            )
            return self.ports._recover(
                RecoveryReason.REBASE_CONFLICT if in_progress else RecoveryReason.WORKTREE_RECOVERY_FAILED,
                rebase_failure, context=context,
            )
        self.ports._complete_rebase_intent(context,
            project_values=managed_rebase_adoption.revalidation_values(latest_sha),
            worker_definition=self.ports.snapshot()._definition_snapshot)
        events.emit("최신 base 반영 완료: 변경 후보의 검증 근거 재확인", target_base=latest_sha)
        from loop_manager_internal.execution.manual_candidate import get_manual
        manual=get_manual(self.ports.state)
        if manual and manual.user_submission(row):
            # Cleanly integrating base does not require manufacturing a
            # Worker result for a user candidate. Conflict recovery still
            # follows the existing Worker path above.
            self.ports.state.update_project(self.ports.identity.repo,phase='VERIFYING')
        return None

    def _latest_before_verify(self, context, observed_base_sha):
        row = self.ports._row()
        assert self.ports.snapshot().catalog_plan is not None and self.ports.snapshot().plan is not None
        latest_sha = observed_base_sha or self.ports._fetch_latest_base()
        if latest_sha != row['active_sha']:
            outcome = self.ports._rebase_latest(context, row, latest_sha, publishing=False)
            if outcome is not None:
                return outcome
            return self.ports._retry('최신 base를 반영했습니다. 게시 후보와 선언을 다시 확인합니다.')
        return None

    def _latest_before_publication(self, context):
        row = self.ports._row()
        assert self.ports.snapshot().catalog_plan is not None
        latest_sha = self.ports._fetch_latest_base()
        remote_oid = None if int(row['pr_conflict_recovery']) else self.ports._checkpoint_remote_oid(self.ports.identity.repo_path, context.branch)
        try:
            if not int(row['pr_conflict_recovery']):
                self.ports.record_remote_publication_branch(
                    self.ports.identity.repo_path,
                    self.ports.identity.state_dir,
                    self.ports.identity.repo,
                    context.branch,
                    context.worktree,
                    remote_oid,
                )
        except RecoveryFailure as error:
            return self.ports._recover(error.reason, str(error), context=context)
        except self.ports.contract.PublicationSafetyError as error:
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
        if latest_sha != row['validated_base_sha']:
            if latest_sha != row['active_sha']:
                outcome = self.ports._rebase_latest(
                    context,
                    row,
                    latest_sha,
                    publishing=True,
                )
                if outcome is not None:
                    return outcome
                if not self.ports.branch_has_changes(context.worktree, self.ports.identity.base):
                    no_change_error = '게시 직전 rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
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
                return self.ports._retry('최신 rebase 후보의 verify 입력과 Review를 다시 확인합니다.')
            else:
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    validated_base_sha=None,
                    phase='VERIFYING',
                    verify_result=None,
                    review_result=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                    last_error=None,
                )
            self.ports.operation_print(
                '게시 직전 base가 변경되어 전체 verify와 Review를 다시 수행합니다.',
                file=sys.stderr,
            )
            return self.ports._retry()
        if row['active_sha'] != latest_sha:
            ancestry = self.ports.git(
                context.worktree, 'merge-base', '--is-ancestor',
                latest_sha, 'HEAD', check=False,
            )
            if ancestry.returncode not in {0, 1}:
                return self.ports._recover(
                    RecoveryReason.STATUS_FAILED,
                    f"게시 checkpoint ancestry 조회 실패: {ancestry.stdout[-8000:]}",
                    context=context,
                )
            if ancestry.returncode == 1:
                outcome = self.ports._rebase_latest(
                    context, row, latest_sha, publishing=True,
                )
                if outcome is not None:
                    return outcome
                return self.ports._retry('최신 rebase 후보의 verify 입력과 Review를 다시 확인합니다.')
            self.ports.state.update_project(
                self.ports.identity.repo, active_sha=latest_sha, validated_base_sha=None,
                phase='VERIFYING', verify_result=None, review_result=None,
                verify_token=None, review_token=None, publication_token=None,
                last_error=None,
            )
            return self.ports._recover(
                RecoveryReason.CHECKPOINT_STALE,
                '관리 branch의 실제 HEAD와 최신 base에서 게시 checkpoint를 재구성했습니다.',
                context=context,
            )
        return None
