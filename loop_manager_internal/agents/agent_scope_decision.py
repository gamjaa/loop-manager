"""Pure decisions for validating an interrupted Agent's persisted Git scope.

The caller owns persistence, worktree recovery, path canonicalization and Git I/O.
This module only compares already captured snapshots and describes checks to run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from loop_manager_internal.agents.scope_contracts import GitSnapshot, ManagerGitCheckpoint


class ScopeDecisionError(RuntimeError):
    """A persisted snapshot cannot be safely reconciled."""


def index_security_flags(entries: tuple[str, ...]) -> tuple[str, ...]:
    """Separate ls-files status/stage from assume-unchanged and skip-worktree.

    M is Git's unmerged status, not an index flag. Multiple conflict stages
    share one path's flags, so their count must not affect this comparison.
    Unknown or malformed markers remain protected rather than disappearing.
    """
    flags = set()
    for entry in entries:
        fields = entry.split()
        tag = fields[0] if fields else ''
        stage = fields[-1] if len(fields) >= 3 else ''
        if tag == 'H' or (tag == 'M' and stage in {'1', '2', '3'}):
            continue
        if tag in {'h', 'm', 's'}:
            flags.add('assume-unchanged')
        if tag in {'S', 's'}:
            flags.add('skip-worktree')
        if tag not in {'h', 'S', 's'} and not (tag == 'm' and stage in {'1', '2', '3'}):
            flags.add('unknown:' + entry)
    return tuple(sorted(flags))


def changed_conflict_paths(before: GitSnapshot, after: GitSnapshot) -> frozenset[str]:
    return frozenset(path for path in before.index.keys() | after.index.keys()
        if before.index.get(path) != after.index.get(path)
        and any(entry.split()[-1:] in (['1'], ['2'], ['3'])
                for entry in (*before.index.get(path, ()), *after.index.get(path, ()))))


def reconcile_manager_metadata(snapshot: GitSnapshot, current: GitSnapshot, *,
                               refs: frozenset[str], worktrees: frozenset[str],
                               resources: GitResources | None = None) -> GitSnapshot:
    """Apply only operation-proven keys; preserve local Agent work and audit.

    The caller must prove each chain from this snapshot to current Git. With
    invocation isolation, unrelated resources need no baseline advancement.
    """
    def advance(previous, observed, keys):
        updated = dict(previous)
        for key in keys:
            if key in observed:
                updated[key] = observed[key]
            else:
                updated.pop(key, None)
        return updated
    if resources is not None:
        refs &= resources.refs
        worktrees &= resources.worktrees
    # A symbolic ref's OID is derived from its target. Carry that OID only
    # when the unchanged target itself has an operation-proven transition.
    refs |= frozenset(key for key, previous in snapshot.refs.items()
        if previous[1] in refs
        and previous == (snapshot.refs.get(previous[1], (None, ''))[0], previous[1])
        and current.refs.get(key) == (current.refs.get(previous[1], (None, ''))[0], previous[1]))
    return replace(snapshot, refs=advance(snapshot.refs, current.refs, refs),
                   worktrees=advance(snapshot.worktrees, current.worktrees, worktrees))


def manager_comparison_baseline(original: GitSnapshot, comparison: GitSnapshot,
                                saved: dict, role: str) -> GitSnapshot:
    """Read the proven metadata at invocation start without moving local work."""
    baseline = saved.get('manager_metadata_agent_baseline')
    if baseline is None:
        return comparison
    if not isinstance(baseline, dict):
        raise ScopeDecisionError('Manager metadata Agent 기준선이 유효하지 않습니다.')
    if (baseline.get('role') != role
            or baseline.get('original') != hashlib.sha256(original.serialize().encode()).hexdigest()):
        return comparison
    try:
        effective = type(original).deserialize(baseline['snapshot'])
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ScopeDecisionError('Manager metadata Agent snapshot이 유효하지 않습니다.') from error
    def advance(previous, observed, at_start):
        updated = dict(observed)
        for key in previous.keys() | at_start.keys():
            if previous.get(key) != at_start.get(key) and observed.get(key) == previous.get(key):
                if key in at_start:
                    updated[key] = at_start[key]
                else:
                    updated.pop(key, None)
        return updated
    return replace(comparison,
        refs=advance(original.refs, comparison.refs, effective.refs),
        worktrees=advance(original.worktrees, comparison.worktrees, effective.worktrees))


@dataclass(frozen=True)
class GitResources:
    """Resources used by this role and phase, after invocation proof validation."""

    refs: frozenset[str]
    worktrees: frozenset[str]
    branches: frozenset[str]

    def config(self, entries: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        # Only known branch-local settings have a provably unrelated effect.
        # Includes, remote settings, hooks and unknown keys remain protected.
        result = {}
        for key, value in entries.items():
            _, _, name = key.partition(":")
            branch, _, setting = name.removeprefix("branch.").rpartition(".")
            if (name.startswith("branch.") and branch not in self.branches
                    and setting.lower() in {"remote", "merge", "pushremote", "rebase", "description"}):
                continue
            result[key] = value
        return result


def used_git_resources(*, role: str, phase: str, base_branch: str,
                       branch: str | None, worktree_key: str) -> GitResources | None:
    """Unknown roles/stages retain repository-wide comparison."""
    stages = {
        "planner": {"PLANNING", "PLAN_BLOCKED"},
        "planner_check": {"PLANNING", "PLAN_BLOCKED"},
        "maintenance_planner": {"PLANNING", "PLAN_BLOCKED"},
        "worker": {"WORKING", "IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING", "PLAN_BLOCKED"},
        "reviewer": {"REVIEWING", "PUBLISHING", "PLAN_BLOCKED"},
    }
    if phase not in stages.get(role, ()) or not base_branch or not worktree_key:
        return None
    refs = {f"refs/remotes/origin/{base_branch}", "refs/remotes/origin/HEAD"}
    branches = {base_branch}
    if role in {"worker", "reviewer"}:
        if not branch:
            return None
        branches.add(branch)
        refs.update((f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"))
    elif branch is not None:
        return None  # Planning must stay detached.
    return GitResources(frozenset(refs), frozenset((worktree_key,)), frozenset(branches))


def scoped_metadata(before: GitSnapshot, after: GitSnapshot,
                    resources: GitResources | None, *,
                    default_worktree: str | None = None,
                    selected_worktree: str | None = None,
                    base_branch: str | None = None) -> tuple[GitSnapshot, GitSnapshot]:
    """Keep full snapshots for audit; project only the comparison operands."""
    if resources is None and default_worktree is None and not base_branch:
        return before, after
    compare_default_registration = bool(
        default_worktree and default_worktree != selected_worktree
    )
    default_was_registered = bool(
        compare_default_registration and default_worktree in before.worktrees
    )
    def project(snapshot):
        refs = ({key: value for key, value in snapshot.refs.items() if key in resources.refs}
                if resources is not None else dict(snapshot.refs))
        worktrees = ({key: value for key, value in snapshot.worktrees.items()
                      if key in resources.worktrees or key == default_worktree}
                     if resources is not None else dict(snapshot.worktrees))
        if compare_default_registration:
            if not default_was_registered:
                worktrees.pop(default_worktree, None)
            elif default_worktree in worktrees:
                # Ignore valid checkout state but retain registration identity
                # so a removed or malformed primary worktree still fails.
                worktrees[default_worktree] = tuple(
                    field for field in worktrees[default_worktree]
                    if not field.startswith(("HEAD=", "state="))
                    and not (
                        field == "detached"
                        or field.startswith("branch=refs/heads/")
                        and len(field) > len("branch=refs/heads/")
                    )
                )
        if base_branch:
            refs.pop(f"refs/heads/{base_branch}", None)
        return replace(snapshot,
            refs=refs,
            worktrees=worktrees,
            config=resources.config(snapshot.config) if resources is not None else snapshot.config)
    return project(before), project(after)


def invocation_scope_phase(role: str) -> str | None:
    """Stable invocation stage, independent of later resume/block transitions."""
    return {
        'planner': 'PLANNING', 'planner_check': 'PLANNING',
        'maintenance_planner': 'PLANNING', 'worker': 'IMPLEMENTING',
        'reviewer': 'REVIEWING',
    }.get(role)


def invocation_scope_digest(snapshot: GitSnapshot, *, role: str, phase: str,
                            base_branch: str, branch: str | None,
                            worktree_key: str,
                            default_worktree: str | None = None) -> str:
    """Fingerprint the current workspace and the Git resources this call uses."""
    # Captured preflight snapshots have no ledger identity. Saved start
    # snapshots do; exclude those audit fields from the same content contract.
    snapshot = replace(snapshot, scope_id=None, scope_start_digest=None)
    resources = used_git_resources(
        role=role, phase=phase, base_branch=base_branch, branch=branch,
        worktree_key=worktree_key,
    )
    _, projected = scoped_metadata(
        snapshot, snapshot, resources,
        default_worktree=default_worktree, selected_worktree=worktree_key,
        base_branch=base_branch,
    )
    identity = "\0".join((role, phase, base_branch, branch or "", worktree_key,
                           default_worktree or ""))
    return hashlib.sha256((identity + "\0" + projected.serialize()).encode()).hexdigest()


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
    base_branch: str = "main",
) -> GitSnapshot:
    """Carry only the verified parent runtime and registered base delta forward."""
    ref = f"refs/remotes/origin/{base_branch}"
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
        and original.refs.get(ref) is not None
        and original.refs.get(symbolic_head) == (original.refs[ref][0], ref)
        and current.refs.get(ref) is not None
        and current.refs.get(symbolic_head) == (current.refs[ref][0], ref)
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
    accept_current_input: bool = False,
) -> GitSnapshot:
    if not all(snapshot.shared_metadata_complete for snapshot in (before, result, current)):
        raise ScopeDecisionError("저장된 BLOCKED Agent Git metadata 기준 snapshot이 없습니다.")
    flagged_paths = sorted(path for path in before.index.keys() | current.index.keys()
        if index_security_flags(before.index.get(path, ())) != index_security_flags(current.index.get(path, ())))
    if flagged_paths:
        raise ScopeDecisionError('BLOCKED index flags 변경: ' + ', '.join(flagged_paths[:8]))
    if accept_current_input:
        if before.operations or current.operations:
            raise ScopeDecisionError("BLOCKED 인계에 미해결 Git operation이 있습니다.")
        if before.branch != current.branch:
            raise ScopeDecisionError("BLOCKED 인계의 소유 branch가 변경됐습니다.")
        previous_registration = before.worktrees.get(worktree_key)
        current_registration = current.worktrees.get(worktree_key)
        if previous_registration is None or current_registration is None:
            raise ScopeDecisionError("BLOCKED 인계 worktree 등록을 확인할 수 없습니다.")
        def without_head(registration):
            return tuple(field for field in registration if not field.startswith("HEAD="))
        if without_head(previous_registration) != without_head(current_registration):
            raise ScopeDecisionError("BLOCKED 인계 worktree identity가 변경됐습니다.")
        if f"HEAD={current.head}" not in current_registration:
            raise ScopeDecisionError("BLOCKED 인계 worktree HEAD가 현재 입력과 다릅니다.")
        if current.branch is None:
            if current.head != before.head:
                raise ScopeDecisionError("detached BLOCKED worktree의 새 commit 소유권을 확인할 수 없습니다.")
        else:
            ref = f"refs/heads/{current.branch}"
            if (before.refs.get(ref) != (before.head, "")
                    or current.refs.get(ref) != (current.head, "")
                    or f"branch={ref}" not in current_registration):
                raise ScopeDecisionError("BLOCKED 인계 branch ref 소유권이 일치하지 않습니다.")
        refs = dict(before.refs)
        if current.branch is not None:
            refs[f"refs/heads/{current.branch}"] = current.refs[f"refs/heads/{current.branch}"]
        return replace(
            before, head=current.head, index=current.index, dirty=current.dirty,
            refs=refs, worktrees={**before.worktrees, worktree_key: current_registration},
        )
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
    manager_refs: frozenset[str] = frozenset(),
    runtime_base_oid: str | None = None,
) -> ScopeDecision:
    """Describe ordered scope checks without reading Git or persisted state."""
    if role not in {"planner", "planner_check", "maintenance_planner", "worker", "reviewer"}:
        raise ScopeDecisionError(f"저장된 Agent 역할이 유효하지 않습니다: {role or '없음'}")
    if role == "worker" and not selected_plan_path:
        raise ScopeDecisionError("저장된 Worker ExecPlan 경로가 없습니다.")

    if manager_runtime is not None:
        runtime_path, runtime_sha = manager_runtime
        registration = current.worktrees.get(runtime_path)
        ref = f"refs/remotes/origin/{base_branch}"
        origin = current.refs.get(ref)
        previous_origin = before.refs.get(ref)
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
            or origin is None or origin != (runtime_base_oid or runtime_sha, "")
            or not registration_matches
            or (previous_head != current_head and (
                previous_origin is None
                or previous_head != (previous_origin[0], ref)
                or current_head != (runtime_base_oid or runtime_sha, ref)
            ))
        ):
            raise ScopeDecisionError(
                f"runtime 기준선 증거가 일치하지 않습니다: {runtime_path} sha={runtime_sha}"
            )
        original_before = before
        before = reconcile_runtime_snapshot(before, original_before, current, runtime_path, base_branch)
        if result is not None:
            result = reconcile_runtime_snapshot(result, original_before, current, runtime_path, base_branch)
        if checkpoint is not None:
            checkpoint = replace(
                checkpoint,
                snapshot=reconcile_runtime_snapshot(
                    checkpoint.snapshot, original_before, current, runtime_path, base_branch,
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
            allowed_refs = manager_refs
            allowed_worktrees = manager_cleanup_worktrees
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
                accept_current_input=True,
            )
        allowed_cleanup = checkpoint.cleanup_worktrees | manager_cleanup_worktrees
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
            accept_current_input=True,
        )
        scope_before, commit_refs, commit_worktrees = diagnosed_blocked_commit_allowances(
            before, result, current, worktree_key, scope_before,
        )
        allowed_refs = manager_refs | commit_refs
        allowed_worktrees = manager_cleanup_worktrees | commit_worktrees
    return ScopeDecision((ScopeCheck(role, scope_before, current, allowed_refs | manager_refs,
                                    allowed_worktrees | manager_cleanup_worktrees),), before)


def _worker_path_allowed(
    path: str,
    selected_plan_path: str,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
) -> bool:
    normalized = path.replace("\\", "/")
    lowered = normalized.casefold()
    if normalized == selected_plan_path:
        return True
    if lowered in {"docs/product.md", "docs/architecture.md", "docs/future_ideas.md"}:
        return True
    if lowered == "docs/plans.md":
        return allow_common_plans
    if lowered.startswith("docs/requirements/"):
        return False
    if lowered.startswith("docs/plans/"):
        return False
    return True


def _same_local_snapshot(left: GitSnapshot, right: GitSnapshot) -> bool:
    return (
        left.head == right.head
        and left.branch == right.branch
        and left.index == right.index
        and left.dirty == right.dirty
        and left.operations == right.operations
    )


def _non_default_index_flags(entries: tuple[str, ...]) -> tuple[str, ...]:
    return index_security_flags(entries)


def _metadata_changes_after_block(
    before: Mapping[str, object],
    blocked: Mapping[str, object],
    current: Mapping[str, object],
    candidates: frozenset[str],
) -> frozenset[str]:
    """Compatibility surface for callers of the persisted-scope policy."""
    return metadata_changes_after_block(
        before, blocked, current, candidates
    )
