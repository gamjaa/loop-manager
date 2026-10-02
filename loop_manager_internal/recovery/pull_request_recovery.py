"""Existing PR reconciliation and owned conflict rebase recovery."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from collections.abc import Mapping
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.runtime.contracts import ManagedWorktreeConflictError
from loop_manager_internal.runtime.contracts import PROJECT_STEP_PLANNING_PENDING
from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY
from loop_manager_internal.recovery.scoped_blocking import PR_CLOSED_BLOCK_REASON
from loop_manager_internal.runtime.contracts import PR_CONFLICT_PREFLIGHT_BLOCK_REASON
from loop_manager_internal.runtime.contracts import PR_CONFLICT_PREFLIGHT_ERROR_PREFIX
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.runtime.contracts import REBASE_CONFLICT_PREFIX
from loop_manager_internal.runtime.contracts import REBASE_IN_PROGRESS_ERROR
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import sqlite3
import sys


@dataclass(frozen=True)
class ReconcileExistingPullRequestEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _block_closed_pull_request: Callable[..., object]
    _fetch_pr_conflict_recovery_refs: Callable[..., object]
    _is_pr_conflict_preflight_block: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _record_project_recovery: Callable[..., object]
    _resume_reopened_pull_request: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    _validate_active_pr_conflict_recovery: Callable[..., object]
    _validate_blocked_pr_without_conflict: Callable[..., object]
    _validate_pr_conflict_recovery_identity: Callable[..., object]
    _validate_pr_conflict_recovery_ownership: Callable[..., object]
    block_active_execplan_from_revision: Callable[..., object]
    classify_pull_request_mergeability: Callable[..., object]
    complete_merged_execplan_worktree: Callable[..., object]
    make_worktree: Callable[..., object]
    operation_print: Callable[..., object]
    project_execplan: Callable[..., object]
    recover_confirmed_pr_conflict_push: Callable[..., object]
    remote_base: Callable[..., object]
    update_project: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]

@dataclass(frozen=True)
class ResumePrConflictRebaseEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _finish_owned_direct_rebase: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    active_execplan: Callable[..., object]
    block_active_execplan: Callable[..., object]
    git: Callable[..., object]
    is_git_lock_failure: Callable[..., object]
    operation_print: Callable[..., object]
    update_project: Callable[..., object]


def reconcile_existing_pull_request(effects: ReconcileExistingPullRequestEffects,
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> int | None:
    """Attach one existing head-branch PR and apply its durable state."""

    branch = str(project["branch"] or "")
    url = str(pull_request.get("url") or project["pr_url"] or "")
    if not url:
        raise RuntimeError("기존 PR URL을 확인할 수 없습니다.")
    head_name = pull_request.get("headRefName")
    if head_name and str(head_name) != branch:
        raise PublicationSafetyError(
            f"기존 PR head branch가 관리 branch와 다릅니다: {head_name}"
        )
    head_value = pull_request.get("headRefOid")
    head_oid = str(head_value) if head_value else project["pr_head_oid"]
    state = str(pull_request.get("state") or "OPEN").upper()
    blocked_plan = effects.project_execplan(connection, repo)
    if blocked_plan is not None and blocked_plan["status"] != "PLAN_BLOCKED":
        blocked_plan = None
    reopening_closed_pr = bool(
        state == "OPEN"
        and blocked_plan is not None
        and blocked_plan["blocked_reason"] == PR_CLOSED_BLOCK_REASON
    )
    recovering_preflight_block = bool(
        state == "OPEN"
        and project["phase"] == "PLAN_BLOCKED"
        and effects._is_pr_conflict_preflight_block(project, blocked_plan)
    )
    if (
        state in {"OPEN", "CLOSED"}
        and project["phase"] == "PLAN_BLOCKED"
        and blocked_plan is not None
        and blocked_plan["blocked_reason"] != PR_CLOSED_BLOCK_REASON
        and not recovering_preflight_block
    ):
        effects.operation_print(
            "PR 충돌 복구 차단 상태를 유지합니다: "
            + str(project["last_error"] or "사용자 조치가 필요합니다."),
            file=sys.stderr,
        )
        return 1
    if state != "OPEN" or (
        project["phase"] not in {"PR_OPEN", "PLAN_BLOCKED"}
        and not int(project["pr_conflict_recovery"])
    ):
        effects.update_project(
            connection,
            repo,
            pr_url=url,
            pr_head_oid=head_oid,
        )

    if state == "OPEN":
        if recovering_preflight_block:
            assert blocked_plan is not None
            try:
                mergeability = effects.classify_pull_request_mergeability(pull_request)
                if mergeability == "conflicting":
                    effects._validate_pr_conflict_recovery_identity(
                        connection, repo, project, pull_request,
                        expected_status="PLAN_BLOCKED",
                    )
                    effects._fetch_pr_conflict_recovery_refs(
                        repo_path, state_dir, str(project["base_branch"]), branch
                    )
                    (_plan, _worktree, _branch, observed_head, base_oid) = (
                        effects._validate_pr_conflict_recovery_ownership(
                            connection, repo_path, state_dir, repo, project,
                            pull_request, expected_status="PLAN_BLOCKED",
                        )
                    )
                else:
                    effects._validate_blocked_pr_without_conflict(
                        connection, repo_path, state_dir, repo, project,
                        pull_request,
                    )
            except (
                PublicationSafetyError,
                WorkspaceOwnershipError,
            ) as error:
                recovery_error = (
                    f"{PR_CONFLICT_PREFLIGHT_ERROR_PREFIX} {error}"
                )
                effects._update_blocked_error(
                    connection,
                    repo,
                    str(blocked_plan["plan_id"]),
                    recovery_error,
                )
                effects.operation_print(recovery_error, file=sys.stderr)
                return 1
            effects._resume_reopened_pull_request(
                connection,
                repo,
                blocked_plan,
                url,
                head_oid,
                recovery=mergeability == "conflicting",
                conflict_oids=(observed_head, base_oid)
                if mergeability == "conflicting" else None,
            )
            if mergeability == "conflicting":
                effects.operation_print(f"PR 충돌 자동 복구 재개: {url}")
                return None
            effects.operation_print(f"PR_OPEN 충돌 복구 false positive 자동 해제: {url}")
            return 0

        if int(project["pr_conflict_recovery"]):
            try:
                if effects.recover_confirmed_pr_conflict_push(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    project,
                    pull_request,
                ):
                    effects.operation_print(
                        f"기존 PR 충돌 복구 push 확인 완료: {url}"
                    )
                    return 0
                if reopening_closed_pr:
                    effects._validate_pr_conflict_recovery_ownership(
                        connection,
                        repo_path,
                        state_dir,
                        repo,
                        project,
                        pull_request,
                        expected_status="PLAN_BLOCKED",
                    )
                else:
                    effects._validate_active_pr_conflict_recovery(
                        connection,
                        repo_path,
                        state_dir,
                        repo,
                        project,
                        pull_request,
                    )
            except (
                PublicationSafetyError,
                WorkspaceOwnershipError,
            ) as error:
                recovery_error = f"PR 충돌 복구 안전 검증 실패: {error}"
                effects.block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{project['base_branch']}",
                    recovery_error,
                    str(project["verify_result"] or ""),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                effects.operation_print(recovery_error, file=sys.stderr)
                return 1
            if project["phase"] in {"PR_OPEN", "PLAN_BLOCKED"}:
                if reopening_closed_pr:
                    assert blocked_plan is not None
                    effects._resume_reopened_pull_request(
                        connection,
                        repo,
                        blocked_plan,
                        url,
                        head_oid,
                        recovery=True,
                    )
                else:
                    effects.update_project(
                        connection,
                        repo,
                        phase="WORKING",
                        validated_base_sha=None,
                        verify_result=None,
                        review_result=None,
                        last_error=REBASE_IN_PROGRESS_ERROR,
                    )
            return None

        if project["phase"] == "PR_OPEN":
            try:
                effects._validate_pr_conflict_recovery_identity(
                    connection,
                    repo,
                    project,
                    pull_request,
                    expected_status="PR_OPEN",
                )
            except PublicationSafetyError as error:
                recovery_error = (
                    f"{PR_CONFLICT_PREFLIGHT_ERROR_PREFIX} {error}"
                )
                effects.block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{project['base_branch']}",
                    recovery_error,
                    str(project["verify_result"] or ""),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                    blocked_reason=PR_CONFLICT_PREFLIGHT_BLOCK_REASON,
                )
                effects.operation_print(recovery_error, file=sys.stderr)
                return 1
            mergeability = effects.classify_pull_request_mergeability(pull_request)
            if mergeability != "conflicting":
                if mergeability == "unknown":
                    effects.operation_print(
                        f"PR mergeability 계산 대기 중: {url}"
                    )
                else:
                    effects.operation_print(f"Merge 대기 중: {url}")
                return 0
            try:
                effects._fetch_pr_conflict_recovery_refs(
                    repo_path, state_dir, str(project["base_branch"]), branch
                )
                (
                    _plan,
                    _worktree,
                    _branch,
                    observed_head,
                    base_oid,
                ) = effects._validate_pr_conflict_recovery_ownership(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    project,
                    pull_request,
                )
            except (
                PublicationSafetyError,
                WorkspaceOwnershipError,
            ) as error:
                recovery_error = f"PR_OPEN 충돌 복구 소유권 검증 실패: {error}"
                reason = (
                    BlockReasonCode.MANAGED_WORKTREE_CONFLICT
                    if isinstance(error, ManagedWorktreeConflictError)
                    else BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN
                )
                effects.block_active_execplan_from_revision(
                    connection,
                    repo_path,
                    repo,
                    f"refs/remotes/origin/{project['base_branch']}",
                    recovery_error,
                    str(project["verify_result"] or ""),
                    reason=reason,
                    blocked_reason=PR_CONFLICT_PREFLIGHT_BLOCK_REASON,
                )
                effects.operation_print(recovery_error, file=sys.stderr)
                return 1

            effects.update_project(
                connection,
                repo,
                phase="WORKING",
                validated_base_sha=None,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=observed_head,
                pr_conflict_base_oid=base_oid,
                verify_result=None,
                review_result=None,
                last_error=REBASE_IN_PROGRESS_ERROR,
            )
            effects.operation_print(f"PR 충돌 자동 복구 시작: {url}")
            return None
        plan_id = effects._plan_id_from_path(str(project["plan_path"] or ""))
        worktree = Path(str(project["worktree"] or ""))
        try:
            if plan_id is None:
                raise WorkspaceOwnershipError(
                    "PR_OPEN ExecPlan ID를 확인할 수 없습니다."
                )
            plan = effects.project_execplan(connection, repo)
            if (
                plan is None
                or plan["plan_id"] != plan_id
                or int(plan["workspace_owned"]) != WORKSPACE_OWNED
            ):
                raise WorkspaceOwnershipError(
                    "PR_OPEN workspace의 Loop Manager 소유권이 확인되지 않았습니다."
                )
            effects.validate_execplan_workspace_identity(
                state_dir,
                repo,
                plan_id,
                branch,
                worktree,
            )
            effects.make_worktree(
                repo_path,
                worktree,
                branch,
                str(project["base_branch"]),
                journal_state_dir=state_dir,
                journal_owner=f"plan:{plan_id}",
                allow_create_branch=False,
                allow_recovery=True,
                allow_existing=True,
            )
        except WorkspaceOwnershipError as error:
            workspace_error = (
                f"PR_OPEN workspace 소유권/복구 검증 실패: {error}"
            )
            effects.block_active_execplan_from_revision(
                connection,
                repo_path,
                repo,
                f"refs/remotes/origin/{project['base_branch']}",
                workspace_error,
                reason=BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
            )
            effects.operation_print(workspace_error, file=sys.stderr)
            return 1
        except RuntimeError as error:
            detail = f"PR_OPEN workspace 복구 재시도: {error}"
            effects._record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.WORKTREE_RECOVERY_FAILED,
                detail,
            )
            effects.operation_print(detail, file=sys.stderr)
            return PROJECT_STEP_RETRY
        if reopening_closed_pr:
            assert blocked_plan is not None
            effects._resume_reopened_pull_request(
                connection,
                repo,
                blocked_plan,
                url,
                head_oid,
                recovery=False,
            )
        else:
            runtime_state.RuntimeStateRepository(connection).open_pull_request(
                repo,
                pr_url=url,
                pr_head_oid=head_oid,
            )
        effects.operation_print(f"Merge 대기 중: {url}")
        return 0

    if state == "MERGED" or pull_request.get("mergedAt"):
        effects.remote_base(
            repo_path, str(project["base_branch"]), state_dir=state_dir,
        )
        persisted = connection.execute(
            "SELECT pr_head_oid FROM projects WHERE repo = ?",
            (repo,),
        ).fetchone()
        stored_head_oid = (
            str(persisted["pr_head_oid"])
            if persisted and persisted["pr_head_oid"]
            else None
        )
        plan = effects.project_execplan(connection, repo)
        if plan is None:
            raise RuntimeError("병합된 PR과 연결된 ExecPlan을 찾을 수 없습니다.")
        worktree = Path(str(plan["worktree"] or ""))
        try:
            assessment = effects.complete_merged_execplan_worktree(
                connection,
                repo_path,
                repo,
                state_dir,
                plan,
                url,
                stored_head_oid,
            )
        except WorkspaceOwnershipError as error:
            workspace_error = (
                f"Merge cleanup workspace 소유권 검증 실패: {error}"
            )
            effects.block_active_execplan_from_revision(
                connection,
                repo_path,
                repo,
                f"refs/remotes/origin/{project['base_branch']}",
                workspace_error,
                reason=BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
            )
            effects.operation_print(workspace_error, file=sys.stderr)
            return 1
        except RuntimeError as error:
            detail = f"Merge cleanup workspace 조회 재시도: {error}"
            effects._record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.WORKTREE_RECOVERY_FAILED,
                detail,
            )
            effects.operation_print(detail, file=sys.stderr)
            return PROJECT_STEP_RETRY
        if assessment.status in {"safe", "missing"}:
            effects.operation_print(f"Merge 확인 및 worktree 정리 완료: {url}")
        else:
            effects.operation_print(
                f"Merge 확인 완료; worktree 보존: {worktree} "
                f"({assessment.reason or assessment.status})",
                file=sys.stderr,
            )
        return PROJECT_STEP_PLANNING_PENDING

    if state == "CLOSED":
        plan = effects.project_execplan(connection, repo)
        if plan is None:
            raise RuntimeError("닫힌 PR과 연결된 활성 ExecPlan을 찾을 수 없습니다.")
        effects._block_closed_pull_request(connection, repo, plan, url, head_oid)
        effects.operation_print(f"닫힌 PR을 다시 열어야 합니다: {url}", file=sys.stderr)
        return 1

    raise RuntimeError(f"지원하지 않는 GitHub PR 상태입니다: {state}")


def resume_pr_conflict_rebase(effects: ResumePrConflictRebaseEffects,
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    project: sqlite3.Row,
    worktree: Path,
    branch: str,
) -> int | None:
    """Resume the Manager-owned rebase only after its intent is durable."""

    if (
        not int(project["pr_conflict_recovery"])
        or project["phase"] != "WORKING"
        or project["last_error"] != REBASE_IN_PROGRESS_ERROR
        or effects._rebase_in_progress_for_branch(worktree, branch)
    ):
        return None
    if effects.git(worktree, "ls-files", "-u", "-z").stdout:
        effects.update_project(
            connection, repo, phase="WORKING", validated_base_sha=None,
            last_error=REBASE_CONFLICT_PREFIX + "unmerged index after rebase/autostash",
        )
        return 0
    plan = effects.active_execplan(connection, repo)
    target_base = str(project["pr_conflict_base_oid"] or "")
    observed_head = str(project["pr_conflict_head_oid"] or "")
    if (
        plan is None
        or not int(plan["pr_conflict_recovery"])
        or not target_base
        or not observed_head
    ):
        error = "PR 충돌 복구 rebase marker 또는 관측 OID가 유효하지 않습니다."
        effects.block_active_execplan(
            connection,
            repo,
            worktree,
            error,
            str(project["verify_result"] or ""),
            reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
        )
        effects.operation_print(error, file=sys.stderr)
        return 1

    state_dir = effects._owned_rebase_state_dir(repo, str(plan['plan_id']), worktree)
    rebase_retry_pending = False
    checkpoint_record = execution_checkpoint_state.ExecutionCheckpointRepository(
        connection,
    ).get(repo, str(plan['plan_id']))
    if state_dir is None and checkpoint_record.operation == 'rebase':
        error = 'PR 충돌 복구 rebase journal workspace가 없습니다.'
        effects.block_active_execplan(
            connection, repo, worktree, error,
            str(project['verify_result'] or ''),
            reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
        )
        effects.operation_print(error, file=sys.stderr)
        return 1
    if state_dir is not None:
        try:
            effects._finish_owned_direct_rebase(
                state_dir, connection, repo_path, repo,
                str(plan['plan_id']), worktree, branch,
                agent_session_id=plan['worker_session_id'],
            )
            if checkpoint_record.operation == 'rebase':
                _, current_decision = effects._current_rebase_service(
                    connection, repo_path, state_dir, repo,
                    str(plan['plan_id']), worktree, branch).require_owned()
                if current_decision.status.value == 'ready_to_start':
                    rebase_retry_pending = True
                elif current_decision.status.value == 'complete':
                    effects._complete_direct_rebase_checkpoint(
                        connection, repo, str(plan['plan_id']), worktree,
                        target_base_oid=checkpoint_record.target_base_oid,
                        repo_path=repo_path, branch=branch,
                        agent_session_id=plan['worker_session_id'])
                    return 0
                else:
                    return None
        except manager_git_journal.MutationEvidenceError as error:
            detail = f'PR 충돌 복구 rebase 증거가 다릅니다: {error}'
            effects.block_active_execplan(
                connection, repo, worktree, detail,
                str(project['verify_result'] or ''),
                reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
            )
            effects.operation_print(detail, file=sys.stderr)
            return 1

    latest_base = effects.git(
        repo_path,
        "rev-parse",
        f"refs/remotes/origin/{project['base_branch']}^{{commit}}",
    ).stdout.strip()
    if latest_base != target_base:
        target_base = latest_base
        effects.update_project(
            connection,
            repo,
            pr_conflict_base_oid=target_base,
            validated_base_sha=None,
            verify_result=None,
            review_result=None,
            last_error=REBASE_IN_PROGRESS_ERROR,
        )

    ancestry = effects.git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        "HEAD",
        check=False,
    )
    if ancestry.returncode == 0 and not rebase_retry_pending:
        effects.update_project(
            connection,
            repo,
            active_sha=target_base,
            phase="WORKING",
            worker_result=None, worker_input_token=None, archive_token=None,
            verify_result=None, review_result=None, verify_token=None,
            review_token=None, publication_token=None, validated_base_sha=None,
            last_error=None,
        )
        return 0
    if ancestry.returncode not in (0, 1):
        error = (
            "PR 충돌 복구 base ancestry 확인 실패:\n"
            + ancestry.stdout[-8000:]
        )
        effects.operation_print(error, file=sys.stderr)
        return PROJECT_STEP_RETRY

    rebased = effects._journaled_direct_rebase(
        connection, repo_path, repo, str(plan['plan_id']), worktree,
        branch, target_base, agent_session_id=plan['worker_session_id'],
    )
    if rebased.returncode and effects.is_git_lock_failure(rebased.stdout):
        raise GitLockError(rebased.stdout[-8000:])
    if effects.git(worktree, "ls-files", "-u", "-z").stdout:
        effects.update_project(
            connection, repo, phase="WORKING", validated_base_sha=None,
            last_error=REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:],
        )
        return 0
    if rebased.returncode == 0:
        if state_dir is not None:
            effects._complete_direct_rebase_checkpoint(
                connection, repo, str(plan['plan_id']), worktree, target_base,
                repo_path=repo_path, branch=branch,
                agent_session_id=plan['worker_session_id'],
            )
        effects.update_project(
            connection,
            repo,
            active_sha=target_base,
            phase="WORKING",
            worker_result=None, worker_input_token=None, archive_token=None,
            verify_result=None, review_result=None, verify_token=None,
            review_token=None, publication_token=None, validated_base_sha=None,
            last_error=None,
        )
        return 0
    if effects._rebase_in_progress_for_branch(worktree, branch):
        effects.update_project(
            connection,
            repo,
            phase="WORKING",
            validated_base_sha=None,
            last_error=REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:],
        )
        return 0

    error = f"PR 충돌 복구 rebase 실패:\n{rebased.stdout[-8000:]}"
    effects.operation_print(error, file=sys.stderr)
    return PROJECT_STEP_RETRY
