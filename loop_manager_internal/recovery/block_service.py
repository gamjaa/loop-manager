"""recovery block service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import MAX_VALIDATION_FINGERPRINT_REPEATS
from loop_manager_internal.application.contracts import ValidationFailureDecision
from loop_manager_internal.application.contracts import WorkerLevelDecision
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import BlockScope
from loop_manager_internal.storage import review_correction_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from pathlib import Path
import json
import re
import sqlite3


@dataclass(frozen=True)
class BlockedExecplanMetadataErrorEffects:
    """Named effects for _blocked_execplan_metadata_error; no entry-point/module access."""
    _blocked_execplan_metadata_identity_error: Callable[..., object]
    _parse_execplan: Callable[..., object]


def _blocked_execplan_metadata_error(
    effects: BlockedExecplanMetadataErrorEffects, repo_path: Path,
    worktree: Path,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    try:
        current = effects._parse_execplan(worktree, worktree / path)
    except (OSError, RuntimeError) as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"

    return effects._blocked_execplan_metadata_identity_error(repo_path, current, plan)


@dataclass(frozen=True)
class BlockedExecplanRevisionMetadataErrorEffects:
    """Named effects for _blocked_execplan_revision_metadata_error; no entry-point/module access."""
    _blocked_execplan_metadata_identity_error: Callable[..., object]
    git: Callable[..., object]
    published_execplan: Callable[..., object]


def _blocked_execplan_revision_metadata_error(
    effects: BlockedExecplanRevisionMetadataErrorEffects, repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    shown = effects.git(repo_path, "show", f"{revision}:{path}", check=False)
    if shown.returncode:
        return "BLOCKED ExecPlan을 remote 기준으로 다시 검증할 수 없습니다."
    try:
        current = effects.published_execplan(path, shown.stdout)
    except RuntimeError as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"
    return effects._blocked_execplan_metadata_identity_error(repo_path, current, plan)


def _update_blocked_error(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    error: str,
    *, preserve_cause: bool = False,
) -> None:
    with connection:
        runtime = runtime_state.RuntimeStateRepository(connection)
        projected = runtime.projected_row(repo)
        if preserve_cause and (scoped_blocking.project_block(connection, repo) is not None
                or projected is not None and projected['plan_id'] != plan_id):
            runtime.update_plan(repo, plan_id, plan_values={"last_error": error}, enqueue=False)
        else:
            runtime.transition(
                repo, plan_id,
                plan_values={"last_error": error},
                project_values={"last_error": error},
                enqueue=False,
            )
        if not preserve_cause:
            connection.execute(
                "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND plan_id = ?",
                (error, repo, plan_id),
            )
        notifications.enqueue_current_project_events(connection, repo)


@dataclass(frozen=True)
class BlockActiveExecplanEffects:
    """Named effects for block_active_execplan; no entry-point/module access."""
    _final_block_recovery_decision: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _revision_review_issue_fingerprint: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    project_execplan: Callable[..., object]


def block_active_execplan(
    effects: BlockActiveExecplanEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    error: str,
    verify_result: str,
    *,
    reason: BlockReasonCode = BlockReasonCode.PLAN_INPUT_INVALID,
    preserved_agent_result_snapshot: str | None = None,
    recovery_decision: recovery_runtime.RecoveryDecision | None = None,
    release_fingerprint: str | None = None,
    worker_result: str | None = None,
) -> recovery_runtime.RecoveryDecision | None:
    project = runtime_state.project_row(connection, repo)
    plan_id = effects._plan_id_from_path(str(project["plan_path"] or "")) if project else None
    plan = (
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if plan_id
        else None
    )
    if plan is None:
        plan = effects.project_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    if recovery_decision is not None and recovery_decision.observation.plan_id != str(plan['plan_id']):
        raise ValueError('recovery decision and block target disagree')
    expected_plan = dict(plan)
    checkpoints = blocked_checkpoint = None
    blocked_git_snapshot = None
    blocked_remote_input = None
    if reason in {BlockReasonCode.PLAN_INPUT_INVALID, BlockReasonCode.MANAGED_WORKTREE_CONFLICT}:
        from loop_manager_internal.storage import execution_checkpoint_state
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
        blocked_checkpoint = checkpoints.get(repo, str(plan['plan_id']))
    if blocked_checkpoint is not None and blocked_checkpoint.operation == 'rebase':
        try:
            observed = effects.capture_git_snapshot(worktree)
            if effects.capture_git_snapshot(worktree) == observed:
                blocked_git_snapshot = observed.serialize()
        except (OSError, RuntimeError, ValueError):
            pass
        if blocked_git_snapshot is not None and project['base_branch']:
            try:
                revision = f"refs/remotes/origin/{project['base_branch']}"
                observed_input = effects._revision_execplan_input_fingerprint(Path(repo), revision, plan)
                if (re.fullmatch(r'[0-9a-f]{64}', observed_input or '')
                        and effects._revision_execplan_input_fingerprint(Path(repo), revision, plan) == observed_input):
                    blocked_remote_input = observed_input
            except (OSError, RuntimeError, ValueError, TypeError):
                pass
    blocked_worktree_fingerprint = effects._worktree_execplan_input_fingerprint(
        worktree, plan
    )
    if (blocked_git_snapshot is not None and effects._worktree_execplan_input_fingerprint(
            worktree, plan) != blocked_worktree_fingerprint):
        from loop_manager_internal.application.task_scheduling import TaskRecheckRequired
        raise TaskRecheckRequired('차단 입력 관측 중 작업 문서가 변경됐습니다.')
    fingerprint = str(
        project["agent_input_fingerprint"] or blocked_worktree_fingerprint
    )
    block_fingerprint = release_fingerprint or fingerprint
    blocked_review_fingerprint = None
    if reason is BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK:
        revision = str(plan["active_sha"] or plan["current_revision_sha"] or "")
        block_fingerprint = effects._revision_execplan_input_fingerprint(
            Path(repo), revision, plan
        )
        blocked_review_fingerprint = effects._revision_review_issue_fingerprint(
            Path(repo), revision, error
        )
    agent_result_snapshot = preserved_agent_result_snapshot
    if agent_result_snapshot is None:
        try:
            agent_result_snapshot = effects.capture_git_snapshot(worktree).serialize()
        except RuntimeError:
            pass
    agent_result_plan = None
    if (
        project["agent_role"] == "reviewer"
        and project["agent_input_plan"] is not None
    ):
        plan_file = worktree / str(plan["path"])
        if plan_file.is_file():
            agent_result_plan = plan_file.read_text(encoding="utf-8")
    plan_values = {
        "blocked_fingerprint": block_fingerprint,
        "blocked_worktree_fingerprint": blocked_worktree_fingerprint,
        "blocked_review_fingerprint": blocked_review_fingerprint,
        "last_error": error,
        "verify_result": verify_result,
        "agent_result_snapshot": agent_result_snapshot,
        "agent_result_plan": agent_result_plan,
        "agent_manager_snapshot": None,
    }
    if worker_result is not None:
        plan_values["worker_result"] = worker_result
    plan_id = str(plan["plan_id"])
    with runtime_state._transaction(connection):
        current = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if current is None or dict(current) != expected_plan:
            from loop_manager_internal.application.task_scheduling import TaskRecheckRequired
            raise TaskRecheckRequired('차단 관측 중 작업 상태가 변경됐습니다.')
        if blocked_git_snapshot is not None and checkpoints.get(repo, plan_id) != blocked_checkpoint:
            from loop_manager_internal.application.task_scheduling import TaskRecheckRequired
            raise TaskRecheckRequired('차단 Git 관측 중 checkpoint가 변경됐습니다.')
        decision = effects._final_block_recovery_decision(
            connection, repo, plan, str(project["phase"] or "") if project else "",
            reason, error, fingerprint, recovery_decision,
        )
        if reason is BlockReasonCode.MANAGED_WORKTREE_CONFLICT:
            if blocked_git_snapshot is None:
                raise recovery_state.RecoveryStateConflict('conflict worktree could not be reobserved before blocking')
            expected_git = GitSnapshot.deserialize(decision.observation.conflict_evidence['worktree_snapshot'])
            actual_git = GitSnapshot.deserialize(blocked_git_snapshot)
            if any(getattr(expected_git, field) != getattr(actual_git, field)
                   for field in ('head', 'branch', 'index', 'dirty', 'operations')):
                raise recovery_state.RecoveryStateConflict('conflict worktree changed before blocking')
        if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                plan_id,
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=block_fingerprint,
                plan_values=plan_values,
                clear_projection=True,
            )
            if blocked_git_snapshot is not None and reason is not BlockReasonCode.MANAGED_WORKTREE_CONFLICT:
                current_checkpoint = checkpoints.get(repo, plan_id)
                block = scoped_blocking.plan_block(connection, repo, plan_id)
                checkpoints.record(
                    repo, plan_id, expected_generation=current_checkpoint.generation,
                    operation=current_checkpoint.operation, expected_head=current_checkpoint.expected_head,
                    target_base_oid=current_checkpoint.target_base_oid,
                    observed_remote_oid=current_checkpoint.observed_remote_oid,
                    snapshot={**(current_checkpoint.snapshot or {}), 'metadata_block_git': {
                        'generation': current_checkpoint.generation + 1,
                        'block': dict(block), 'snapshot': blocked_git_snapshot,
                        'remote_input': blocked_remote_input}},
                )
        else:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values={
                    "verify_result": verify_result,
                    "agent_result_snapshot": agent_result_snapshot,
                    "agent_result_plan": agent_result_plan,
                    "agent_manager_snapshot": None,
                },
                enqueue=False,
            )
            scoped_blocking.record_project_block(
                connection,
                repo,
                reason,
                error,
                fingerprint,
                plan_id=plan_id,
                previous_phase=str(project["phase"] or "") or None,
                recovery_decision=decision,
            )
            notifications.enqueue_current_project_events(connection, repo)
    return decision if reason is BlockReasonCode.MANAGED_WORKTREE_CONFLICT else None


@dataclass(frozen=True)
class BlockVerifyTimeoutLimitEffects:
    """Named effects for block_verify_timeout_limit; no entry-point/module access."""
    block_active_execplan: Callable[..., object]
    redact_secrets: Callable[..., object]


def block_verify_timeout_limit(
    effects: BlockVerifyTimeoutLimitEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    worker_result: str,
    evidence: str,
    verify_result: str,
) -> str:
    observed = re.search(
        r"전체 verify 시간 초과: verify_timeout_seconds=([1-9][0-9]*)",
        verify_result,
    )
    if observed is None:
        raise RuntimeError("verify 시간 초과 상한 증거가 없습니다.")
    timeout = int(observed.group(1))
    detail = (
        f"전체 verify에 필요한 검증이 진행 중이었으나 현재 상한 "
        f"verify_timeout_seconds={timeout}초가 부족합니다. "
        f"[loop] verify_timeout_seconds를 늘리세요.\n"
        f"진행 근거: {effects.redact_secrets(evidence)[-1800:]}"
    )
    effects.block_active_execplan(
        connection, repo, worktree, detail, verify_result,
        reason=BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
        release_fingerprint=str(timeout), worker_result=worker_result,
    )
    return detail


def _final_block_recovery_decision(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    phase: str,
    reason: BlockReasonCode,
    error: str,
    fingerprint: str,
    decision: recovery_runtime.RecoveryDecision | None,
) -> recovery_runtime.RecoveryDecision:
    """Accept only a final typed decision in the blocking transaction."""

    if decision is None:
        observation = recovery_runtime.RecoveryObservation(
            reason=recovery_runtime.reason_from_block(reason),
            repo=repo, plan_id=str(plan["plan_id"]), phase=phase,
            plan_revision=str(plan["current_revision_sha"] or plan["plan_sha"] or ""),
            base_revision=str(plan["active_sha"] or ""),
            fingerprint=fingerprint,
            progress_marker=str(plan["active_sha"] or ""), detail=error,
            user_targets=recovery_state.block_user_targets(
                connection, repo, str(plan["plan_id"]), reason,
                str(plan["path"]),
            ),
        )
        decision = recovery_state.RecoveryRepository(connection).observe(
            observation, final_block_reason=reason,
        ).decision
    recovery_runtime.require_final(decision)
    if (reason is BlockReasonCode.MANAGED_WORKTREE_CONFLICT
            and not recovery_state.conflict_evidence_current(connection, repo, str(plan['plan_id']),
                                                             decision.observation.conflict_evidence)):
        raise recovery_state.RecoveryStateConflict('final conflict assessment changed before blocking')
    if reason is BlockReasonCode.MANAGED_WORKTREE_CONFLICT:
        repository = recovery_state.RecoveryRepository(connection)
        previous = repository.row(repo, str(plan['plan_id']))
        try:
            saved = json.loads(previous['final_evidence']) if previous is not None else None
        except (TypeError, ValueError):
            saved = None
        if (previous is None or previous['identity'] != decision.identity
                or previous['pending_action'] != 'final_block' or not isinstance(saved, dict)
                or saved.get('conflict_evidence') != decision.observation.conflict_evidence
                or saved.get('unavailable_recovery_routes') != decision.observation.unavailable_recovery_routes):
            decision = repository.observe(decision.observation).decision
            recovery_runtime.require_final(decision)
    if decision.block_reason != reason or decision.observation.repo != repo or decision.observation.plan_id != str(plan["plan_id"]):
        raise ValueError("recovery decision and block target disagree")
    return decision


@dataclass(frozen=True)
class TransitionValidationFailureEffects:
    """Named effects for transition_validation_failure; no entry-point/module access."""
    _blocked_validation_base_revision: Callable[..., object]
    _blocked_validation_input_fingerprints: Callable[..., object]
    _deserialize_failure_history: Callable[..., object]
    _deserialize_review_level_history: Callable[..., object]
    _final_block_recovery_decision: Callable[..., object]
    _revision_review_issue_fingerprint: Callable[..., object]
    active_execplan: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    evaluate_review_worker_level: Callable[..., object]
    redact_secrets: Callable[..., object]
    validation_failure_fingerprints: Callable[..., object]


def transition_validation_failure(
    effects: TransitionValidationFailureEffects, connection: sqlite3.Connection,
    repo: str, worktree: Path, stage: str, output: str,
    *, review_level: int | None = None, candidate_head: str | None = None,
    review_source_id: int | None = None,
) -> ValidationFailureDecision:
    """Git 관측 뒤 입력을 재검사하고 실패 이력·차단을 함께 저장한다."""

    from loop_manager_internal.storage.db_access import CoordinatedConnection, DatabaseWaitTimeout
    if isinstance(connection, CoordinatedConnection) and (connection.in_transaction or connection._depth):
        raise DatabaseWaitTimeout('검증 실패 관측 전에 DB transaction을 종료해야 합니다.')

    columns = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }
    labels = {"verify": "전체 verify", "review": "Review"}
    history_column = columns.get(stage)
    if history_column is None:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    plan = effects.active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("검증 실패를 기록할 활성 ExecPlan을 찾을 수 없습니다.")
    expected = dict(plan)
    failures = effects.validation_failure_fingerprints(stage, output, worktree)
    redacted = effects.redact_secrets(str(output))
    history = effects._deserialize_failure_history(plan[history_column])
    recorded_failures = []
    for fingerprint, normalized in failures:
        previous = history.get(fingerprint, {"occurrences": 0})
        occurrences = int(previous["occurrences"]) + 1
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": effects.redact_secrets(normalized)[-2000:],
        }
        recorded_failures.append((fingerprint, normalized, occurrences))
    blocking_failure = next(
        (
            failure
            for failure in recorded_failures
            if stage == "verify" and failure[2] > MAX_VALIDATION_FINGERPRINT_REPEATS
        ),
        None,
    )
    fingerprint, normalized, occurrences = blocking_failure or max(
        recorded_failures,
        key=lambda failure: failure[2],
    )
    serialized_history = json.dumps(
        history,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    level_decision: WorkerLevelDecision | None = None
    serialized_level_history: str | None = None
    if stage == "review" and plan["effective_worker_level"] is not None:
        correction = review_correction_state.deserialize(plan["review_correction_state"])
        level_decision = effects.evaluate_review_worker_level(
            plan["effective_worker_level"],
            effects._deserialize_review_level_history(plan["review_level_history"]),
            tuple(failure[0] for failure in recorded_failures),
            correction,
            review_level=review_level,
            candidate_head=candidate_head,
        )
        if review_source_id is not None and review_level == plan["effective_worker_level"]:
            # Preserve the actual FAIL before rebase invalidates review_result.
            # It is not a completed correction until Worker and verify finish.
            level_decision.correction_state['pending'] = {
                'source': review_source_id, 'level': level_decision.effective_level,
                'completed': False, 'verified_head': None,
            }
        serialized_level_history = json.dumps(
            level_decision.review_history,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    blocked = blocking_failure is not None
    if blocked:
        error = (
            f"{labels[stage]} 동일 실패가 Worker 수정 후 "
            f"{MAX_VALIDATION_FINGERPRINT_REPEATS}회 다시 발생했습니다.\n"
            f"실패 지문: {fingerprint}\n{redacted[-8000:]}"
        )
        blocked_input, blocked_worktree_input = (
            effects._blocked_validation_input_fingerprints(
                connection,
                Path(repo),
                repo,
                worktree,
                plan,
            )
        )
        blocked_review_input = None
        if stage == "review":
            blocked_revision = effects._blocked_validation_base_revision(
                connection, repo, plan
            )
            if blocked_revision:
                blocked_review_input = effects._revision_review_issue_fingerprint(
                Path(repo), blocked_revision, redacted
                )
    else:
        prefix = "전체 검증 실패" if stage == "verify" else "Review 실패"
        error = f"{prefix}:\n{redacted[-8000:]}"
        blocked_input = None
        blocked_worktree_input = None
        blocked_review_input = None

    plan_values: dict[str, object] = {
        history_column: serialized_history,
        "validated_base_sha": None,
        "status": "PLAN_BLOCKED" if blocked else "IMPLEMENTING",
        "last_error": error,
    }
    project_values: dict[str, object] = {
        "validated_base_sha": None,
        "phase": "WORKING",
        "last_error": error,
    }
    if stage == "verify":
        plan_values["verify_result"] = redacted
        project_values["verify_result"] = redacted
    else:
        plan_values["review_result"] = redacted
        project_values["review_result"] = redacted
        if level_decision is not None:
            plan_values["effective_worker_level"] = level_decision.effective_level
            plan_values["review_level_history"] = serialized_level_history
            plan_values["review_correction_state"] = json.dumps(level_decision.correction_state)
    if blocked:
        plan_values.update(
            blocked_fingerprint=blocked_input,
            blocked_worktree_fingerprint=blocked_worktree_input,
            blocked_review_fingerprint=blocked_review_input,
        )
        try:
            plan_values["agent_result_snapshot"] = effects.capture_git_snapshot(worktree).serialize()
        except (OSError, RuntimeError):
            # Missing evidence must fail closed in the checkpoint assessor.
            plan_values["agent_result_snapshot"] = None
        reason = (
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED
            if stage == "verify"
            else BlockReasonCode.REVIEW_RETRY_EXHAUSTED
        )
    with runtime_state._transaction(connection):
        current = effects.active_execplan(connection, repo)
        if current is None or dict(current) != expected:
            from loop_manager_internal.application.task_scheduling import TaskRecheckRequired
            raise TaskRecheckRequired('검증 실패 관측 중 작업 상태가 변경됐습니다.')
        if blocked:
            recovery_decision = effects._final_block_recovery_decision(
                connection, repo, plan, stage.upper(), reason, error,
                fingerprint, None,
            )
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                str(plan["plan_id"]),
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=blocked_input,
                plan_values=plan_values,
                clear_projection=True,
            )
        else:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                str(plan["plan_id"]),
                plan_values=plan_values,
                project_values=project_values,
                enqueue=False,
            )
    return ValidationFailureDecision(
        stage,
        fingerprint,
        occurrences,
        blocked,
        recovery_decision if blocked else None,
    )


@dataclass(frozen=True)
class BlockActiveExecplanFromRevisionEffects:
    """Named effects for block_active_execplan_from_revision; no entry-point/module access."""
    _final_block_recovery_decision: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    active_execplan: Callable[..., object]
    project_execplan: Callable[..., object]


def block_active_execplan_from_revision(
    effects: BlockActiveExecplanFromRevisionEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    error: str,
    verify_result: str = "",
    *,
    reason: BlockReasonCode = BlockReasonCode.PLAN_INPUT_INVALID,
    preserved_agent_result_snapshot: str | None = None,
    blocked_reason: str | None = None,
    recovery_decision: recovery_runtime.RecoveryDecision | None = None,
) -> None:
    """Block an active plan when its saved worktree is not safe to read."""

    plan = effects.active_execplan(connection, repo) or effects.project_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    project = runtime_state.project_row(connection, repo)
    fingerprint = str(
        (project["agent_input_fingerprint"] if project else None)
        or effects._revision_execplan_input_fingerprint(repo_path, revision, plan)
    )
    plan_values: dict[str, object] = {
        "blocked_fingerprint": fingerprint,
        "blocked_worktree_fingerprint": None,
        "blocked_review_fingerprint": None,
        "last_error": error,
        "verify_result": verify_result,
        "agent_result_snapshot": preserved_agent_result_snapshot,
        "agent_manager_snapshot": None,
    }
    if reason in {BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN, BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL}:
        # Ownership uncertainty does not invalidate recorded candidate evidence.
        plan_values = {'last_error': error}
    if blocked_reason is not None:
        plan_values["blocked_reason"] = blocked_reason
    plan_id = str(plan["plan_id"])
    with runtime_state._transaction(connection):
        decision = effects._final_block_recovery_decision(
            connection, repo, plan,
            str(project["phase"] or "") if project else "", reason, error,
            fingerprint, recovery_decision,
        )
        if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                plan_id,
                reason_code=reason.value,
                evidence=error,
                release_fingerprint=fingerprint,
                plan_values=plan_values,
                clear_projection=True,
            )
        else:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values={
                    "verify_result": verify_result,
                    "agent_result_snapshot": preserved_agent_result_snapshot,
                    "agent_manager_snapshot": None,
                },
                enqueue=False,
            )
            scoped_blocking.record_project_block(
                connection,
                repo,
                reason,
                error,
                fingerprint,
                plan_id=plan_id,
                previous_phase=(
                    str(project["phase"] or "") or None if project else None
                ),
                recovery_decision=decision,
            )
            notifications.enqueue_current_project_events(connection, repo)
