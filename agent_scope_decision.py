"""Pure decisions for validating an interrupted Agent's persisted Git scope.

The caller owns persistence, worktree recovery, path canonicalization and Git I/O.
This module only compares already captured snapshots and describes checks to run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from loop_manager import GitSnapshot, ManagerGitCheckpoint


class ScopeDecisionError(RuntimeError):
    """A persisted snapshot cannot be safely reconciled."""


@dataclass(frozen=True)
class ScopeCheck:
    role: str
    before: GitSnapshot
    after: GitSnapshot
    allowed_refs: frozenset[str] = frozenset()
    allowed_worktrees: frozenset[str] = frozenset()
    checkpoint_result: bool = False


@dataclass(frozen=True)
class ScopeDecision:
    checks: tuple[ScopeCheck, ...]
    reconciled_before: GitSnapshot | None = None


def reconcile_runtime_snapshot(
    snapshot: GitSnapshot,
    original: GitSnapshot,
    current: GitSnapshot,
    runtime_path: str,
) -> GitSnapshot:
    """Carry only the verified parent runtime and origin/main delta forward."""
    ref = "refs/remotes/origin/main"
    worktrees = dict(snapshot.worktrees)
    refs = dict(snapshot.refs)
    if snapshot.worktrees.get(runtime_path) == original.worktrees.get(runtime_path):
        worktrees[runtime_path] = current.worktrees[runtime_path]
    if snapshot.refs.get(ref) == original.refs.get(ref):
        refs[ref] = current.refs[ref]
    symbolic_head = "refs/remotes/origin/HEAD"
    if (
        original.refs.get(symbolic_head) != current.refs.get(symbolic_head)
        and snapshot.refs.get(symbolic_head) == original.refs.get(symbolic_head)
    ):
        refs[symbolic_head] = current.refs[symbolic_head]
    return replace(snapshot, worktrees=worktrees, refs=refs)


def metadata_changes_after_block(
    before: Mapping[str, object],
    blocked: Mapping[str, object],
    current: Mapping[str, object],
    candidates: frozenset[str],
) -> frozenset[str]:
    return frozenset(
        key for key in candidates
        if before.get(key) == blocked.get(key)
        and blocked.get(key) != current.get(key)
    )


def blocked_agent_local_baseline(
    before: GitSnapshot,
    result: GitSnapshot,
    current: GitSnapshot,
    worktree_key: str,
    *,
    allowed_dirty_paths: frozenset[str] = frozenset(),
) -> GitSnapshot:
    if not all(snapshot.shared_metadata_complete for snapshot in (before, result, current)):
        raise ScopeDecisionError("저장된 BLOCKED Agent Git metadata 기준 snapshot이 없습니다.")
    new_local_changes: set[str] = set()
    for state_name, before_state, result_state, current_state in (
        ("index", before.index, result.index, current.index),
        ("dirty", before.dirty, result.dirty, current.dirty),
    ):
        for path in set(before_state) | set(result_state) | set(current_state):
            value = current_state.get(path)
            if (
                value not in (before_state.get(path), result_state.get(path))
                and not (state_name == "dirty" and path in allowed_dirty_paths)
            ):
                new_local_changes.add(path)
    if new_local_changes:
        displayed = ", ".join(sorted(new_local_changes)[:8])
        remainder = len(new_local_changes) - 8
        if remainder > 0:
            displayed += f", 외 {remainder}개"
        raise ScopeDecisionError(
            "BLOCKED 이후 새 파일 또는 index 변경을 자동 보존할 수 없습니다: " + displayed
        )
    if current.worktrees.get(worktree_key) is None:
        raise ScopeDecisionError("BLOCKED Agent 결과에 관리 worktree 등록이 없습니다.")
    return replace(before, index=current.index, dirty=current.dirty)


def diagnosed_blocked_commit_allowances(
    before: GitSnapshot,
    blocked_result: GitSnapshot,
    current: GitSnapshot,
    worktree_key: str,
    scope_before: GitSnapshot,
) -> tuple[GitSnapshot, frozenset[str], frozenset[str]]:
    if blocked_result.head == before.head:
        return scope_before, frozenset(), frozenset()
    if (
        before.branch is None
        or blocked_result.branch != before.branch
        or current.branch != before.branch
        or current.head != blocked_result.head
    ):
        return scope_before, frozenset(), frozenset()
    branch_ref = f"refs/heads/{before.branch}"
    result_branch_ref = blocked_result.refs.get(branch_ref)
    if (
        result_branch_ref is None
        or result_branch_ref[0] != blocked_result.head
        or current.refs.get(branch_ref) != result_branch_ref
    ):
        return scope_before, frozenset(), frozenset()
    result_registration = blocked_result.worktrees.get(worktree_key)
    if result_registration is None or current.worktrees.get(worktree_key) != result_registration:
        return scope_before, frozenset(), frozenset()
    return (
        replace(scope_before, head=current.head),
        frozenset((branch_ref,)),
        frozenset((worktree_key,)),
    )


def decide_persisted_scope(
    *,
    role: str,
    phase: str,
    base_branch: str,
    selected_plan_path: str,
    before: GitSnapshot,
    current: GitSnapshot,
    result: GitSnapshot | None,
    checkpoint: ManagerGitCheckpoint | None,
    worktree_key: str,
    repo_key: str,
    planner_correction: bool,
    manager_cleanup_worktrees: frozenset[str],
    manager_runtime: tuple[str, str] | None = None,
    runtime_recreation_proven: bool = False,
) -> ScopeDecision:
    """Describe ordered scope checks without reading Git or persisted state."""
    if role not in {"planner", "maintenance_planner", "worker", "reviewer"}:
        raise ScopeDecisionError(f"저장된 Agent 역할이 유효하지 않습니다: {role or '없음'}")
    if role == "worker" and not selected_plan_path:
        raise ScopeDecisionError("저장된 Worker ExecPlan 경로가 없습니다.")

    if manager_runtime is not None:
        runtime_path, runtime_sha = manager_runtime
        registration = current.worktrees.get(runtime_path)
        origin = current.refs.get("refs/remotes/origin/main")
        previous_origin = before.refs.get("refs/remotes/origin/main")
        previous_head = before.refs.get("refs/remotes/origin/HEAD")
        current_head = current.refs.get("refs/remotes/origin/HEAD")
        previous_registration = before.worktrees.get(runtime_path)
        registration_matches = previous_registration in (None, registration)
        if runtime_recreation_proven and previous_registration is not None and registration is not None:
            registration_matches = tuple(
                field for field in previous_registration if not field.startswith("locked=")
            ) == tuple(field for field in registration if not field.startswith("locked="))
        if (
            registration is None
            or f"HEAD={runtime_sha}" not in registration
            or "detached=True" not in registration
            or origin is None or origin[0] != runtime_sha
            or not registration_matches
            or (previous_head != current_head and (
                previous_origin is None
                or previous_head != (previous_origin[0], "refs/remotes/origin/main")
                or current_head != (runtime_sha, "refs/remotes/origin/main")
            ))
        ):
            raise ScopeDecisionError(
                f"runtime 기준선 증거가 일치하지 않습니다: {runtime_path} sha={runtime_sha}"
            )
        original_before = before
        before = reconcile_runtime_snapshot(before, original_before, current, runtime_path)
        if result is not None:
            result = reconcile_runtime_snapshot(result, original_before, current, runtime_path)
        if checkpoint is not None:
            checkpoint = replace(
                checkpoint,
                snapshot=reconcile_runtime_snapshot(
                    checkpoint.snapshot, original_before, current, runtime_path,
                ),
            )

    if planner_correction:
        correction_worktrees = dict(before.worktrees)
        before_registration = before.worktrees.get(worktree_key)
        current_registration = current.worktrees.get(worktree_key)
        if (
            before_registration is not None
            and current_registration is not None
            and tuple(field for field in before_registration if not field.startswith("HEAD="))
            == tuple(field for field in current_registration if not field.startswith("HEAD="))
        ):
            correction_worktrees[worktree_key] = current_registration
        allowed_refs = frozenset()
        allowed_worktrees = frozenset()
        if result is not None and result.shared_metadata_complete:
            allowed_refs = metadata_changes_after_block(
                before.refs, result.refs, current.refs,
                frozenset((f"refs/heads/{base_branch}", f"refs/remotes/origin/{base_branch}")),
            )
            allowed_worktrees = metadata_changes_after_block(
                before.worktrees, result.worktrees, current.worktrees, frozenset((repo_key,)),
            )
        if result is not None and result.worktrees.get(worktree_key) != current_registration:
            correction_worktrees[worktree_key] = before_registration
        correction_result = result or current
        correction_before = replace(
            correction_result, branch=before.branch, refs=before.refs,
            worktrees=correction_worktrees, config=before.config,
            operations=before.operations, shared_metadata_complete=True,
        )
        return ScopeDecision((ScopeCheck(role, correction_before, current, allowed_refs, allowed_worktrees),), before)

    if checkpoint is not None:
        checks: list[ScopeCheck] = []
        if phase != "PLAN_BLOCKED":
            checks.append(ScopeCheck(role, before, checkpoint.snapshot))
        scope_before = checkpoint.snapshot
        if phase == "PLAN_BLOCKED" and selected_plan_path:
            scope_before = blocked_agent_local_baseline(
                checkpoint.snapshot, checkpoint.snapshot, current, worktree_key,
                allowed_dirty_paths=frozenset((selected_plan_path,)),
            )
        manager_refs = frozenset(
            refname for refname in set(checkpoint.snapshot.refs) | set(current.refs)
            if checkpoint.snapshot.refs.get(refname) != current.refs.get(refname)
            and (
                refname.startswith("refs/remotes/origin/")
                or refname.startswith("refs/tags/")
                or (phase == "PLAN_BLOCKED" and refname == f"refs/heads/{base_branch}")
            )
        )
        allowed_cleanup = checkpoint.cleanup_worktrees | manager_cleanup_worktrees
        if phase == "PLAN_BLOCKED":
            allowed_cleanup |= frozenset((repo_key,))
        checks.append(ScopeCheck(
            "reviewer", scope_before, current, manager_refs, allowed_cleanup,
            checkpoint_result=True,
        ))
        return ScopeDecision(tuple(checks), before)

    scope_before = before
    allowed_refs = frozenset()
    allowed_worktrees = frozenset()
    if phase == "PLAN_BLOCKED" and result is not None and result.shared_metadata_complete:
        scope_before = blocked_agent_local_baseline(
            before, result, current, worktree_key,
            allowed_dirty_paths=(frozenset((selected_plan_path,)) if selected_plan_path else frozenset()),
        )
        scope_before, commit_refs, commit_worktrees = diagnosed_blocked_commit_allowances(
            before, result, current, worktree_key, scope_before,
        )
        manager_candidates = frozenset(
            {f"refs/heads/{base_branch}"}
            | {refname for refname in set(before.refs) | set(current.refs)
               if refname.startswith("refs/remotes/origin/") or refname.startswith("refs/tags/")}
        )
        allowed_refs = metadata_changes_after_block(
            before.refs, result.refs, current.refs, manager_candidates,
        ) | commit_refs
        allowed_worktrees = metadata_changes_after_block(
            before.worktrees, result.worktrees, current.worktrees, frozenset((repo_key,)),
        ) | commit_worktrees
    return ScopeDecision((ScopeCheck(role, scope_before, current, allowed_refs, allowed_worktrees),), before)
