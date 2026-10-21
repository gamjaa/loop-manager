"""recovery blocked agent service boundaries and named composition effects."""
from __future__ import annotations

from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.agents import scope_baseline
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import stat


def _deserialize_blocked_agent_recovery(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent 복구 상태가 유효하지 않습니다: {error}"
        ) from error
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or parsed.get("state") not in {"prepared", "stashed", "restoring_baseline", "isolated"}
        or not isinstance(parsed.get("source_snapshot"), str)
        or not isinstance(parsed.get("original_head"), str)
        or not isinstance(parsed.get("violation_head"), str)
        or not isinstance(parsed.get("branch"), str)
        or not isinstance(parsed.get("stash_marker"), str)
        or not isinstance(parsed.get("violation_ref"), str)
        or (
            parsed.get("plan_temp_path") is not None
            and not isinstance(parsed.get("plan_temp_path"), str)
        )
        or (
            parsed.get("plan_mode") is not None
            and not isinstance(parsed.get("plan_mode"), int)
        )
        or (
            parsed.get("baseline_snapshot") is not None
            and not isinstance(parsed.get("baseline_snapshot"), str)
        )
        or (
            parsed.get("plan_path") is not None
            and not isinstance(parsed.get("plan_path"), str)
        )
        or (
            parsed.get("plan_contents") is not None
            and not isinstance(parsed.get("plan_contents"), str)
        )
        or (any(key in parsed for key in ("owner_plan_id", "owner_block", "owner_request_identity", "checkpoint_generation"))
            and (not isinstance(parsed.get("owner_plan_id"), str)
                 or not isinstance(parsed.get("owner_block"), dict)
                 or not isinstance(parsed.get("owner_request_identity"), str)
                 or not isinstance(parsed.get("checkpoint_generation"), int)))
    ):
        raise AgentScopeError("저장된 BLOCKED Agent 복구 상태 형식을 지원하지 않습니다.")
    return parsed


def _require_blocked_agent_recovery_owner(
    connection: sqlite3.Connection, repo: str, worktree: Path,
    recovery: Mapping[str, object],
) -> None:
    plan_id = recovery.get("owner_plan_id")
    if plan_id is None:  # Existing persisted recovery records have no owner fields.
        return
    plan = connection.execute(
        "SELECT status, branch, worktree, workspace_owned FROM exec_plans "
        "WHERE repo = ? AND plan_id = ?", (repo, plan_id),
    ).fetchone()
    block = scoped_blocking.plan_block(connection, repo, str(plan_id))
    request = connection.execute(
        "SELECT request_identity FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    checkpoint = ExecutionCheckpointRepository(connection).get(repo, str(plan_id))
    generation = recovery["checkpoint_generation"]
    generation_valid = (
        checkpoint.generation >= generation
        if recovery["state"] == "isolated"
        else checkpoint.generation == generation
    )
    if (plan is None or plan["status"] != "PLAN_BLOCKED"
            or plan["branch"] != recovery["branch"]
            or plan["worktree"] != str(worktree)
            or int(plan["workspace_owned"] or 0) != WORKSPACE_OWNED
            or block is None or any(
                block[field] != recovery["owner_block"].get(field)
                for field in ("reason_code", "release_fingerprint", "created_at"))
            or request is None or request["request_identity"] != recovery["owner_request_identity"]
            or not generation_valid):
        raise AgentScopeError("BLOCKED Agent 복구의 계획·원인·checkpoint generation이 변경됐습니다.")


@dataclass(frozen=True)
class RestoreAgentIndexFlagsEffects:
    """Named effects for _restore_agent_index_flags; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _restore_agent_index_flags(effects: RestoreAgentIndexFlagsEffects, worktree: Path, entries: Mapping[str, tuple[str, ...]]) -> None:
    return scope_baseline._restore_agent_index_flags(worktree, entries, effects=effects._scope_baseline_effects())


@dataclass(frozen=True)
class RestoreAgentStagedEntriesEffects:
    """Named effects for _restore_agent_staged_entries; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _restore_agent_staged_entries(effects: RestoreAgentStagedEntriesEffects, worktree: Path, before: GitSnapshot) -> None:
    return scope_baseline._restore_agent_staged_entries(worktree, before, effects=effects._scope_baseline_effects())


@dataclass(frozen=True)
class RestoreAgentBaselineEffects:
    """Named effects for _restore_agent_baseline; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _restore_agent_baseline(effects: RestoreAgentBaselineEffects, worktree: Path, before: GitSnapshot, serialized: str | None) -> None:
    return scope_baseline._restore_agent_baseline(worktree, before, serialized, effects=effects._scope_baseline_effects())


def _worktree_relative_target(worktree: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AgentScopeError(f"복구 경로가 worktree 밖을 가리킵니다: {relative_path}")
    target = worktree / candidate
    try:
        target.parent.resolve().relative_to(worktree.resolve())
    except ValueError as error:
        raise AgentScopeError(
            f"복구 경로의 상위 디렉터리가 worktree 밖을 가리킵니다: {relative_path}"
        ) from error
    return target


@dataclass(frozen=True)
class RemoveInterruptedRecoveryTempEffects:
    """Named effects for _remove_interrupted_recovery_temp; no entry-point/module access."""
    _worktree_relative_target: Callable[..., object]


def _remove_interrupted_recovery_temp(
    effects: RemoveInterruptedRecoveryTempEffects, worktree: Path,
    relative_path: str | None,
) -> None:
    if relative_path is None:
        return
    target = effects._worktree_relative_target(worktree, relative_path)
    if target.is_dir() and not target.is_symlink():
        raise AgentScopeError(f"복구 임시 경로가 디렉터리입니다: {relative_path}")
    if target.exists() or target.is_symlink():
        target.unlink()


@dataclass(frozen=True)
class AtomicRestoreFileEffects:
    """Named effects for _atomic_restore_file; no entry-point/module access."""
    _remove_interrupted_recovery_temp: Callable[..., object]
    _worktree_relative_target: Callable[..., object]


def _atomic_restore_file(
    effects: AtomicRestoreFileEffects, worktree: Path,
    relative_path: str,
    temp_relative_path: str,
    contents: str,
    mode: int,
) -> None:
    target = effects._worktree_relative_target(worktree, relative_path)
    temporary = effects._worktree_relative_target(worktree, temp_relative_path)
    effects._remove_interrupted_recovery_temp(worktree, temp_relative_path)
    try:
        with temporary.open("xb") as handle:
            handle.write(contents.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        if os.name != "nt":
            directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 원자적 복원 실패: {error}"
        ) from error


def _serialize_blocked_agent_recovery(recovery: Mapping[str, object]) -> str:
    return json.dumps(
        recovery,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class BlockedRecoveryStashOidEffects:
    """Named effects for _blocked_recovery_stash_oid; no entry-point/module access."""
    git: Callable[..., object]


def _blocked_recovery_stash_oid(effects: BlockedRecoveryStashOidEffects, worktree: Path, marker: str) -> str | None:
    listed = effects.git(
        worktree,
        "stash",
        "list",
        "--format=%H%x00%gs",
        check=False,
    )
    if listed.returncode:
        raise AgentScopeError(
            f"BLOCKED Agent stash 확인 실패:\n{listed.stdout[-8000:]}"
        )
    for line in listed.stdout.splitlines():
        oid, separator, subject = line.partition("\0")
        if separator and subject.endswith(marker):
            return oid
    return None


@dataclass(frozen=True)
class RestoreBlockedAgentRecoveryPlanEffects:
    """Named effects for _restore_blocked_agent_recovery_plan; no entry-point/module access."""
    _atomic_restore_file: Callable[..., object]
    _worktree_relative_target: Callable[..., object]


def _restore_blocked_agent_recovery_plan(
    effects: RestoreBlockedAgentRecoveryPlanEffects, worktree: Path,
    plan_path: str | None,
    plan_contents: str | None,
    plan_temp_path: str | None,
    plan_mode: int | None,
) -> None:
    if plan_path is None:
        return
    target = effects._worktree_relative_target(worktree, plan_path)
    try:
        if plan_contents is None:
            if target.exists() or target.is_symlink():
                target.unlink()
        else:
            if plan_temp_path is None or plan_mode is None:
                raise AgentScopeError(
                    "BLOCKED ExecPlan 원자적 복원 정보가 없습니다."
                )
            effects._atomic_restore_file(
                worktree,
                plan_path,
                plan_temp_path,
                plan_contents,
                plan_mode,
            )
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 복원 실패: {error}"
        ) from error


@dataclass(frozen=True)
class RecoveredAgentLocalStateMatchesEffects:
    """Named effects for _recovered_agent_local_state_matches; no entry-point/module access."""
    _worktree_relative_target: Callable[..., object]


def _recovered_agent_local_state_matches(
    effects: RecoveredAgentLocalStateMatchesEffects, worktree: Path,
    before: GitSnapshot,
    current: GitSnapshot,
    plan_path: str | None,
    plan_contents: str | None,
    plan_mode: int | None,
) -> bool:
    if (
        current.head != before.head
        or current.branch != before.branch
        or current.index != before.index
        or current.operations != before.operations
    ):
        return False
    current_dirty = dict(current.dirty)
    before_dirty = dict(before.dirty)
    if plan_path is None:
        return current_dirty == before_dirty
    current_dirty.pop(plan_path, None)
    before_dirty.pop(plan_path, None)
    if current_dirty != before_dirty:
        return False
    target = effects._worktree_relative_target(worktree, plan_path)
    if plan_contents is None:
        return not target.exists() and not target.is_symlink()
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(target_stat.st_mode)
        and stat.S_IMODE(target_stat.st_mode) == plan_mode
        and target.read_text(encoding="utf-8") == plan_contents
    )


@dataclass(frozen=True)
class MarkBlockedAgentIsolatedEffects:
    """Named effects for _mark_blocked_agent_isolated; no entry-point/module access."""
    _serialize_blocked_agent_recovery: Callable[..., object]
    update_project: Callable[..., object]


def _mark_blocked_agent_isolated(
    effects: MarkBlockedAgentIsolatedEffects, connection: sqlite3.Connection,
    repo: str,
    recovery: dict[str, object],
    before: GitSnapshot,
) -> None:
    recovery["state"] = "isolated"
    recovery["baseline_snapshot"] = before.serialize()
    effects.update_project(
        connection,
        repo,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_recovery=effects._serialize_blocked_agent_recovery(recovery),
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


@dataclass(frozen=True)
class CompleteBlockedAgentRecoveryEffects:
    """Named effects for _complete_blocked_agent_recovery; no entry-point/module access."""
    _blocked_recovery_stash_oid: Callable[..., object]
    _config_entries: Callable[..., object]
    _deserialize_blocked_agent_recovery: Callable[..., object]
    _mark_blocked_agent_isolated: Callable[..., object]
    _recovered_agent_local_state_matches: Callable[..., object]
    _remove_interrupted_recovery_temp: Callable[..., object]
    _restore_agent_baseline: Callable[..., object]
    _restore_blocked_agent_recovery_plan: Callable[..., object]
    _same_local_snapshot: Callable[..., object]
    _serialize_blocked_agent_recovery: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    update_project: Callable[..., object]


def _complete_blocked_agent_recovery(
    effects: CompleteBlockedAgentRecoveryEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
) -> None:
    project = connection.execute(
        "SELECT agent_snapshot, agent_baseline, agent_recovery "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return
    if not project["agent_snapshot"]:
        raise AgentScopeError("BLOCKED Agent 복구에 시작 snapshot이 없습니다.")

    recovery = effects._deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    _require_blocked_agent_recovery_owner(connection, repo, worktree, recovery)
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        source = GitSnapshot.deserialize(str(recovery["source_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 snapshot이 유효하지 않습니다: {error}"
        ) from error

    current_config = effects._config_entries(worktree)
    if current_config != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    marker = str(recovery["stash_marker"])
    stash_oid = effects._blocked_recovery_stash_oid(worktree, marker)
    if recovery["state"] == "prepared":
        if stash_oid is None:
            current = effects.capture_git_snapshot(worktree)
            if current.serialize() != source.serialize():
                raise AgentScopeError(
                    "BLOCKED Agent 복구 준비 후 worktree 상태가 추가로 변경됐습니다."
                )
            stashed = effects.git(
                worktree,
                "stash",
                "push",
                "--all",
                "-m",
                marker,
                check=False,
            )
            if stashed.returncode:
                raise AgentScopeError(
                    f"BLOCKED Agent 변경 보존 실패:\n{stashed.stdout[-8000:]}"
                )
            stash_oid = effects._blocked_recovery_stash_oid(worktree, marker)
        recovery["state"] = "stashed"
        recovery["stash_oid"] = stash_oid
        effects.update_project(
            connection,
            repo,
            agent_recovery=effects._serialize_blocked_agent_recovery(recovery),
        )
    elif stash_oid is None or stash_oid != recovery.get("stash_oid"):
        raise AgentScopeError("BLOCKED Agent 복구 stash 소유권이 변경됐습니다.")

    branch = str(recovery["branch"])
    original_head = str(recovery["original_head"])
    violation_head = str(recovery["violation_head"])
    plan_path_value = recovery.get("plan_path")
    plan_path = str(plan_path_value) if plan_path_value is not None else None
    plan_contents_value = recovery.get("plan_contents")
    plan_contents = (
        str(plan_contents_value) if plan_contents_value is not None else None
    )
    plan_temp_value = recovery.get("plan_temp_path")
    plan_temp_path = (
        str(plan_temp_value) if plan_temp_value is not None else None
    )
    plan_mode_value = recovery.get("plan_mode")
    plan_mode = int(plan_mode_value) if plan_mode_value is not None else None
    effects._remove_interrupted_recovery_temp(worktree, plan_temp_path)
    current = effects.capture_git_snapshot(worktree)
    if current.branch != branch:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree branch가 저장된 branch와 다릅니다."
        )
    if current.operations:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree에 Git operation이 남아 있습니다."
        )
    if before.operations:
        raise AgentScopeError(
            "Agent 실행 전 Git operation이 있는 worktree는 자동 복구할 수 없습니다."
        )
    if recovery["state"] == "restoring_baseline" and current.head != original_head:
        raise AgentScopeError("BLOCKED Agent baseline 복구 HEAD가 저장된 시작 commit과 다릅니다.")

    if effects._recovered_agent_local_state_matches(
        worktree,
        before,
        current,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        effects._mark_blocked_agent_isolated(connection, repo, recovery, before)
        return

    if current.head == violation_head and violation_head != original_head:
        if current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent stash 후 worktree에 새 변경이 생겼습니다."
            )
        violation_ref = str(recovery["violation_ref"])
        existing_ref = effects.git(
            worktree,
            "rev-parse",
            "--verify",
            violation_ref,
            check=False,
        )
        if existing_ref.returncode == 0:
            if existing_ref.stdout.strip() != violation_head:
                raise AgentScopeError(
                    f"BLOCKED Agent 진단 ref 충돌: {violation_ref}"
                )
        else:
            effects.git(worktree, "update-ref", violation_ref, violation_head)
        effects.git(worktree, "reset", "--hard", original_head)
        current = effects.capture_git_snapshot(worktree)
    elif current.head != original_head:
        raise AgentScopeError(
            "BLOCKED Agent 복구 HEAD가 저장된 Agent 결과 또는 시작 commit과 다릅니다."
        )

    if not effects._same_local_snapshot(before, current):
        if recovery["state"] == "restoring_baseline":
            try:
                baseline = json.loads(str(project["agent_baseline"]))
                clean_trees = {
                    str(baseline["worktree_tree"]),
                    effects.git(worktree, "rev-parse", original_head + "^{tree}").stdout.strip(),
                }
                index_tree = effects.git(worktree, "write-tree").stdout.strip()
            except (KeyError, TypeError, ValueError, RuntimeError, OSError) as error:
                raise AgentScopeError("BLOCKED Agent 복원 중간 tree를 확인할 수 없습니다.") from error
            if current.dirty:
                contents = lambda entries: {
                    path: tuple(entry.partition(' ')[2] for entry in values)
                    for path, values in entries.items()}
                if index_tree == baseline["worktree_tree"]:
                    # The first read-tree populates both index and worktree
                    # from the saved worktree tree. Former unstaged/untracked
                    # paths now appear staged against HEAD.
                    safe = (all(kinds == ("staged",) for kinds, _ in current.dirty.values())
                            and all(entry.startswith('H ') for values in current.index.values()
                                    for entry in values))
                else:
                    # The second read-tree restores staged entries before
                    # their saved flags; compare their content and dirty state.
                    safe = (index_tree == baseline["index_tree"]
                            and current.dirty == before.dirty
                            and all(' ' in entry for values in current.index.values()
                                    for entry in values)
                            and contents(current.index) == contents(before.index)
                            and all(entry.startswith('H ') or entry in before.index.get(path, ())
                                    for path, values in current.index.items() for entry in values))
            else:
                safe = (index_tree in clean_trees
                        and all(entry.startswith('H ') for values in current.index.values()
                                for entry in values))
            if not safe:
                raise AgentScopeError("BLOCKED Agent 복원 중 사용자 변경 또는 index 차이가 발견됐습니다.")
        elif current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent 복구 중 예상하지 못한 새 worktree 변경이 생겼습니다."
            )
        if recovery["state"] != "restoring_baseline":
            recovery["state"] = "restoring_baseline"
            effects.update_project(
                connection, repo,
                agent_recovery=effects._serialize_blocked_agent_recovery(recovery),
            )
        effects._restore_agent_baseline(
            worktree,
            before,
            (
                str(project["agent_baseline"])
                if project["agent_baseline"] is not None
                else None
            ),
        )

    effects._restore_blocked_agent_recovery_plan(
        worktree,
        plan_path,
        plan_contents,
        plan_temp_path,
        plan_mode,
    )

    restored = effects.capture_git_snapshot(worktree)
    if not effects._recovered_agent_local_state_matches(
        worktree,
        before,
        restored,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        raise AgentScopeError("BLOCKED Agent 복구 후 저장된 변경 상태와 일치하지 않습니다.")
    effects._mark_blocked_agent_isolated(connection, repo, recovery, before)


@dataclass(frozen=True)
class PreserveBlockedAgentChangesEffects:
    capture_git_snapshot: Callable[..., object]


def preserve_blocked_agent_changes(
    effects: PreserveBlockedAgentChangesEffects, connection: sqlite3.Connection,
    repo: str, worktree: Path, plan_id: str, *, validated_snapshot: GitSnapshot,
) -> GitSnapshot:
    """Keep current input intact; historical Agent snapshots are audit only."""
    if not isinstance(validated_snapshot, GitSnapshot):
        raise AgentScopeError("BLOCKED 현재 작업물의 검증 snapshot이 없습니다.")
    current = effects.capture_git_snapshot(worktree)
    if not agent_scope_decision._same_local_snapshot(current, validated_snapshot):
        raise AgentScopeError("BLOCKED 현재 작업물이 검증 이후 변경되어 재관측이 필요합니다.")
    return current


@dataclass(frozen=True)
class ResumeInterruptedBlockedAgentRecoveryEffects:
    """Named effects for resume_interrupted_blocked_agent_recovery; no entry-point/module access."""
    _complete_blocked_agent_recovery: Callable[..., object]
    _config_entries: Callable[..., object]
    _deserialize_blocked_agent_recovery: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    make_worktree: Callable[..., object]
    project_execplan: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def resume_interrupted_blocked_agent_recovery(
    effects: ResumeInterruptedBlockedAgentRecoveryEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    """Finish a persisted violation isolation before normal repository work."""

    project = connection.execute(
        "SELECT phase, base_branch, branch, worktree, agent_snapshot, "
        "agent_recovery FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return False
    plan = effects.project_execplan(connection, repo)
    block = (scoped_blocking.plan_block(connection, repo, str(plan['plan_id']))
             if plan is not None else None)
    retired = (scoped_blocking.is_scope_baseline_block(block)
               or scoped_blocking.is_scope_baseline_block(scoped_blocking.project_block(connection, repo)))
    if retired:
        if (plan is None or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
                or not plan['worktree'] or not plan['branch']
                or plan['worktree'] != project['worktree'] or plan['branch'] != project['branch']):
            raise WorkspaceOwnershipError('폐기된 Git 복구의 현재 작업 공간이 일치하지 않습니다.')
        worktree, branch = Path(plan['worktree']), str(plan['branch'])
        effects.validate_execplan_workspace_identity(
            state_dir, repo, manual_workspace_owner(connection, repo, str(plan['plan_id'])), branch, worktree)
        effects.make_worktree(repo_path, worktree, branch, str(project['base_branch']),
            journal_state_dir=state_dir, journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False, allow_recovery=False, allow_existing=True,
            allow_rebase=effects._rebase_in_progress_for_branch(worktree, branch))
        from loop_manager_internal.storage import runtime_state
        with runtime_state._transaction(connection):
            current = runtime_state.project_row(connection, repo)
            if current['agent_recovery'] != project['agent_recovery']:
                raise WorkspaceOwnershipError('폐기된 Git 복구 기록이 처리 중 변경됐습니다.')
            checkpoints = ExecutionCheckpointRepository(connection)
            checkpoint = checkpoints.get(repo, str(plan['plan_id']))
            saved = checkpoint.snapshot or {}
            checkpoints.record(repo, str(plan['plan_id']), expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**saved, 'retired_agent_recoveries': [
                    *saved.get('retired_agent_recoveries', ()), project['agent_recovery']]})
            runtime_state.RuntimeStateRepository(connection).update_project(
                repo, {'agent_recovery': None}, enqueue=False)
        return True
    recovery = effects._deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    if project["phase"] != "PLAN_BLOCKED":
        raise AgentScopeError(
            "BLOCKED Agent 복구 상태가 활성 BLOCKED 작업과 일치하지 않습니다."
        )
    serialized_before = project["agent_snapshot"]
    if recovery["state"] == "isolated":
        serialized_before = recovery.get("baseline_snapshot")
    if not isinstance(serialized_before, str):
        raise AgentScopeError("BLOCKED Agent 복구 시작 snapshot이 없습니다.")
    try:
        before = GitSnapshot.deserialize(serialized_before)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 시작 snapshot이 유효하지 않습니다: {error}"
        ) from error

    worktree = Path(str(project["worktree"] or ""))
    branch = str(project["branch"] or "")
    config_context = worktree if worktree.is_dir() else repo_path
    if effects._config_entries(config_context) != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    plan = effects.project_execplan(connection, repo)
    if (
        plan is None
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "BLOCKED Agent 복구 workspace의 Loop Manager 소유권을 증명할 수 없습니다."
        )
    _require_blocked_agent_recovery_owner(connection, repo, worktree, recovery)
    try:
        effects.validate_execplan_workspace_identity(
            state_dir,
            repo,
            manual_workspace_owner(connection, repo, str(plan["plan_id"])),
            branch,
            worktree,
        )
        effects.make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
            allow_rebase=(
                recovery["state"] == "isolated"
                and effects._rebase_in_progress_for_branch(worktree, branch)
            ),
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 workspace 검증 실패: {error}"
        ) from error

    if recovery["state"] != "isolated":
        effects._complete_blocked_agent_recovery(connection, repo, worktree)
    return True
