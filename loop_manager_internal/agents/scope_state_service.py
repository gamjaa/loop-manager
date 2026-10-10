"""agents scope state service boundaries and named composition effects."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents import scope_recovery
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import BlockScope
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import difflib
import sqlite3


@dataclass(frozen=True)
class BlockPersistedAgentScopeViolationEffects:
    """Named effects for block_persisted_agent_scope_violation; no entry-point/module access."""
    active_execplan: Callable[..., object]
    block_active_execplan: Callable[..., object]
    block_active_execplan_from_revision: Callable[..., object]
    project_execplan: Callable[..., object]
    update_project: Callable[..., object]


def block_persisted_agent_scope_violation(
    effects: BlockPersistedAgentScopeViolationEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    row: sqlite3.Row,
    detail: str,
    result_snapshot: GitSnapshot | None = None,
    reason: BlockReasonCode = BlockReasonCode.REPOSITORY_STATE_UNSAFE,
) -> str:
    """Persist a pre-resume scope violation without discarding Agent state."""

    message = f"Agent 범위 선검증 실패:\n{detail}"
    if result_snapshot is not None:
        preserved_snapshot = result_snapshot.serialize()
    elif row["agent_result_snapshot"]:
        preserved_snapshot = str(row["agent_result_snapshot"])
    else:
        preserved_snapshot = str(row["agent_snapshot"])
    plan = effects.active_execplan(connection, repo) or effects.project_execplan(connection, repo)
    if plan is None:
        if (
            row["phase"] == "PLANNING"
            and scoped_blocking.reason_scope(reason) is BlockScope.PLAN
        ):
            effects.update_project(
                connection,
                repo,
                planning_validation_error=detail,
                last_error=message,
                agent_result_snapshot=preserved_snapshot,
            )
        else:
            if scoped_blocking.reason_scope(reason) is BlockScope.PLAN:
                reason = BlockReasonCode.REPOSITORY_STATE_UNSAFE
            with connection:
                scoped_blocking.record_project_block(
                    connection,
                    repo,
                    reason,
                    message,
                    None,
                    plan_id=None,
                    previous_phase=str(row["phase"] or "") or None,
                )
                runtime_state.RuntimeStateRepository(connection).update_project(
                    repo,
                    {"agent_result_snapshot": preserved_snapshot},
                    enqueue=False,
                )
                notifications.enqueue_current_project_events(connection, repo)
        return message

    saved_worktree = Path(str(row["worktree"] or ""))
    if saved_worktree.is_dir():
        effects.block_active_execplan(
            connection,
            repo,
            saved_worktree,
            message,
            str(row["verify_result"] or ""),
            reason=reason,
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    else:
        revision = str(
            plan["current_revision_sha"]
            or plan["plan_sha"]
            or row["active_sha"]
            or f"refs/remotes/origin/{row['base_branch']}"
        )
        effects.block_active_execplan_from_revision(
            connection,
            repo_path,
            repo,
            revision,
            message,
            str(row["verify_result"] or ""),
            reason=reason,
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    return message


@dataclass(frozen=True)
class RecoverPersistedAgentWorktreeEffects:
    """Named effects for recover_persisted_agent_worktree; no entry-point/module access."""
    _plan_id_from_path: Callable[..., object]
    active_execplan: Callable[..., object]
    make_worktree: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def recover_persisted_agent_worktree(
    effects: RecoverPersistedAgentWorktreeEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
) -> str:
    """Recover a missing Agent worktree only from its proven owned branch."""

    worktree = Path(str(project["worktree"] or ""))
    plan_id = effects._plan_id_from_path(str(project["plan_path"] or ""))
    plan = effects.active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    if (
        project["phase"] == "PLANNING"
        or plan_id is None
        or plan is None
        or plan["plan_id"] != plan_id
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or not branch
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "저장된 Agent worktree의 Loop Manager 소유권을 증명할 수 없습니다: "
            f"{worktree}"
        )
    try:
        effects.validate_execplan_workspace_identity(
            state_dir,
            repo,
            manual_workspace_owner(connection, repo, plan_id),
            branch,
            worktree,
        )
        return effects.make_worktree(
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
        raise AgentScopeError(
            f"저장된 Agent worktree 복구 실패: {error}"
        ) from error


@dataclass(frozen=True)
class ProvenRecoveredWorktreeBaselineEffects:
    """Named effects for _proven_recovered_worktree_baseline; no entry-point/module access."""
    _scope_recovery_effects: Callable[..., object]


def _proven_recovered_worktree_baseline(
    effects: ProvenRecoveredWorktreeBaselineEffects, before: GitSnapshot,
    current: GitSnapshot,
    state_dir: Path,
    repo_path: Path,
    plan_id: str,
    worktree: Path,
) -> GitSnapshot:
    return scope_recovery._proven_recovered_worktree_baseline(
        before,
        current,
        state_dir,
        repo_path,
        plan_id,
        worktree,
        effects=effects._scope_recovery_effects(),
    )


@dataclass(frozen=True)
class ValidatePersistedAgentScopeEffects:
    """Named effects for validate_persisted_agent_scope; no entry-point/module access."""
    _scope_recovery_effects: Callable[..., object]


def validate_persisted_agent_scope(
    effects: ValidatePersistedAgentScopeEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    allow_planner_worktree_correction: bool = False,
    manager_cleanup_worktrees: frozenset[str] = frozenset(),
    runtime_mutation: RuntimeMutation | None = None,
) -> GitSnapshot | None:
    return scope_recovery.validate_persisted_agent_scope(
        connection,
        repo_path,
        state_dir,
        repo,
        allow_planner_worktree_correction=allow_planner_worktree_correction,
        manager_cleanup_worktrees=manager_cleanup_worktrees,
        runtime_mutation=runtime_mutation,
        effects=effects._scope_recovery_effects(),
    )


@dataclass(frozen=True)
class RefreshPersistedAgentScopeAfterManagerGitEffects:
    """Named effects for refresh_persisted_agent_scope_after_manager_git; no entry-point/module access."""
    _scope_recovery_effects: Callable[..., object]


def refresh_persisted_agent_scope_after_manager_git(
    effects: RefreshPersistedAgentScopeAfterManagerGitEffects, connection: sqlite3.Connection,
    repo: str,
    validated: GitSnapshot,
    manager_worktree_changes: frozenset[str],
    *,
    state_dir: Path | None = None,
) -> GitSnapshot:
    return scope_recovery.refresh_persisted_agent_scope_after_manager_git(
        connection,
        repo,
        validated,
        manager_worktree_changes,
        state_dir=state_dir,
        effects=effects._scope_recovery_effects(),
    )


@dataclass(frozen=True)
class RemoteAdoptionAgentBaselineEffects:
    """Named effects for _remote_adoption_agent_baseline; no entry-point/module access."""
    _scope_recovery_effects: Callable[..., object]


def _remote_adoption_agent_baseline(
    effects: RemoteAdoptionAgentBaselineEffects, connection,
    repo,
    plan_id,
    worktree,
    project,
    before,
    current,
    checkpoint,
    *,
    saved_plan = None,
    accepted_rebase = False,
):
    return scope_recovery._remote_adoption_agent_baseline(
        connection,
        repo,
        plan_id,
        worktree,
        project,
        before,
        current,
        checkpoint,
        saved_plan=saved_plan,
        accepted_rebase=accepted_rebase,
        effects=effects._scope_recovery_effects(),
    )


@dataclass(frozen=True)
class ValidateBlockedAgentScopeEffects:
    """Named effects for validate_blocked_agent_scope; no entry-point/module access."""
    _scope_recovery_effects: Callable[..., object]


def validate_blocked_agent_scope(
    effects: ValidateBlockedAgentScopeEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    worktree: Path,
    selected_plan_path: str,
    *,
    accepted_rebase: bool = False,
    saved_plan = None,
    require_safe_result: bool = False,
    changed_candidate: bool = False,
) -> GitSnapshot:
    return scope_recovery.validate_blocked_agent_scope(
        connection,
        repo_path,
        repo,
        base,
        worktree,
        selected_plan_path,
        accepted_rebase=accepted_rebase,
        saved_plan=saved_plan,
        require_safe_result=require_safe_result,
        changed_candidate=changed_candidate,
        effects=effects._scope_recovery_effects(),
    )


def _reconcile_blocked_reviewer_plan(
    relative_path: str,
    before_text: str,
    result_text: str,
    current_text: str,
) -> str:
    """Remove Reviewer edits while retaining separable user edits."""

    if before_text == result_text:
        return current_text

    before_lines = before_text.splitlines(keepends=True)
    result_lines = result_text.splitlines(keepends=True)
    current_lines = current_text.splitlines(keepends=True)
    user_equal_blocks = [
        (result_start, result_end, current_start, current_end)
        for tag, result_start, result_end, current_start, current_end in (
            difflib.SequenceMatcher(
                None,
                result_lines,
                current_lines,
                autojunk=False,
            ).get_opcodes()
        )
        if tag == "equal"
    ]
    reverse_edits: list[tuple[int, int, list[str]]] = []
    agent_edits = difflib.SequenceMatcher(
        None,
        result_lines,
        before_lines,
        autojunk=False,
    ).get_opcodes()
    for tag, result_start, result_end, before_start, before_end in agent_edits:
        if tag == "equal":
            continue
        if result_start == result_end:
            positions = {
                current_start + result_start - equal_start
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start <= equal_end
            }
            if len(positions) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start = current_end = positions.pop()
        else:
            ranges = {
                (
                    current_start + result_start - equal_start,
                    current_start + result_end - equal_start,
                )
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start and result_end <= equal_end
            }
            if len(ranges) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start, current_end = ranges.pop()
        reverse_edits.append(
            (current_start, current_end, before_lines[before_start:before_end])
        )

    for current_start, current_end, replacement in reversed(reverse_edits):
        current_lines[current_start:current_end] = replacement
    return "".join(current_lines)
