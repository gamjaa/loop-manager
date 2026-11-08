"""git cleanup service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.application.contracts import CleanupAssessment
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import re
import sqlite3


@dataclass(frozen=True)
class AssessCleanupEffects:
    """Named effects for assess_cleanup; no entry-point/module access."""
    _capture_local_worktree_state: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    git: Callable[..., object]


def assess_cleanup(effects: AssessCleanupEffects, worktree: Path, pr_head_oid: str | None) -> CleanupAssessment:
    """Classify a managed worktree without changing it or shared Git state."""
    if not worktree.exists():
        return CleanupAssessment("missing")
    try:
        status_lines = effects.git(
            worktree, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout.splitlines()
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 상태 확인 실패: {error}")

    dirty_kinds: list[str] = []
    if any(line.startswith("??") for line in status_lines):
        dirty_kinds.append("untracked 파일")
    if any(len(line) >= 2 and line[0] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("staged 변경")
    if any(len(line) >= 2 and line[1] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("tracked 파일 수정")
    if dirty_kinds:
        return CleanupAssessment("dirty", ", ".join(dirty_kinds))
    try:
        head, branch, index, dirty = effects._capture_local_worktree_state(worktree)
        operations = effects._git_operation_entries(worktree)
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 상태 확인 실패: {error}")
    active_operations = [name for name in operations
                         if controlling_git_operation({name: operations[name]})]
    if dirty or active_operations:
        return CleanupAssessment(
            "dirty", "index flag 또는 Git operation 변경: "
            + ", ".join(sorted((*dirty, *active_operations))),
        )

    if not pr_head_oid:
        return CleanupAssessment("unverifiable", "PR head commit 정보가 없습니다.")
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", pr_head_oid) is None:
        return CleanupAssessment("unverifiable", "PR head commit OID 형식이 유효하지 않습니다.")
    try:
        local_head = effects.git(worktree, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        head_object = effects.git(
            worktree, "cat-file", "-e", f"{pr_head_oid}^{{commit}}", check=False
        )
        if head_object.returncode:
            return CleanupAssessment(
                "unverifiable", f"PR head commit object를 찾을 수 없습니다: {pr_head_oid}"
            )
        relation = effects.git(
            worktree,
            "merge-base",
            "--is-ancestor",
            local_head,
            pr_head_oid,
            check=False,
        )
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"commit 관계 확인 실패: {error}")
    if relation.returncode == 0:
        if head != local_head:
            return CleanupAssessment("unverifiable", "worktree HEAD가 판정 중 변경됐습니다.")
        return CleanupAssessment(
            "safe", pr_head_oid=pr_head_oid,
            observation=(head, branch, tuple(sorted(index.items()))),
        )
    if relation.returncode == 1:
        return CleanupAssessment(
            "local-only commits", "로컬 HEAD에 PR head에 포함되지 않은 commit이 있습니다."
        )
    return CleanupAssessment(
        "unverifiable", f"commit 관계 확인 실패 (git 종료 코드 {relation.returncode})"
    )


@dataclass(frozen=True)
class ValidatePendingCleanupWorktreeEffects:
    """Named effects for validate_pending_cleanup_worktree; no entry-point/module access."""
    git: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def validate_pending_cleanup_worktree(
    effects: ValidatePendingCleanupWorktreeEffects, repo_path: Path,
    state_dir: Path,
    repo: str,
    worktree: Path,
) -> None:
    """Prove a pending cleanup path belongs to its deterministic ExecPlan."""

    if not worktree.exists():
        return
    branch_result = effects.git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    if branch_result.returncode:
        raise WorkspaceOwnershipError(
            "보존된 cleanup worktree가 branch에 연결되어 있지 않습니다."
        )
    branch = branch_result.stdout.strip()
    match = re.fullmatch(r"loop-manager/(G-\d+)", branch)
    if match is None:
        raise WorkspaceOwnershipError(
            f"보존된 cleanup worktree branch의 소유권을 증명할 수 없습니다: {branch}"
        )
    plan_id = match.group(1)
    effects.validate_execplan_workspace_identity(
        state_dir,
        repo,
        plan_id,
        branch,
        worktree,
    )
    effects.validate_managed_worktree(repo_path, worktree, branch)


@dataclass(frozen=True)
class RemoveCleanupWorktreeEffects:
    """Named effects for remove_cleanup_worktree; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _capture_local_worktree_state: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _managed_worktree_marker: Callable[..., object]
    _managed_worktree_transition_state: Callable[..., object]
    _write_managed_worktree_receipt: Callable[..., object]
    assess_cleanup: Callable[..., CleanupAssessment]
    git: Callable[..., object]


def restore_cleanup_unlock(
    intent: Mapping[str, object], current: Mapping[str, object], repo: Path,
    worktree: Path, *, git: Callable[..., object], observe: Callable[[], object],
) -> dict[str, object]:
    """Restore the prior lock state and return the verified workspace state."""
    before = intent.get("before")
    registration = before.get("registration") if isinstance(before, dict) else None
    reason = registration.get("locked") if isinstance(registration, dict) else None
    current_registration = current.get("registration") if isinstance(current, dict) else None
    oid = r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})"
    branch = registration.get("branch") if isinstance(registration, dict) else None
    plan = (re.fullmatch(r"refs/heads/loop-manager/(G-\d+)", branch)
            if isinstance(branch, str) else None)
    if (intent.get("kind") != "managed_worktree_remove"
            or intent.get("expected") != {"exists": False, "registration": None, "index": None}
            or plan is None or intent.get("owner") != f"plan:{plan.group(1)}"
            or Path(str(intent.get("target", ""))).name != plan.group(1)
            or worktree.name != plan.group(1)
            or set(before) != {"exists", "registration", "index"}
            or before["exists"] is not True
            or set(registration) != {"head", "branch", "locked"}
            or not isinstance(registration["head"], str)
            or re.fullmatch(oid, registration["head"]) is None
            or not isinstance(before["index"], str)
            or re.fullmatch(oid, before["index"]) is None
            or (reason is not None and (not isinstance(reason, str)
                                         or not reason.startswith("loop-manager-worktree:")))
            or not isinstance(current_registration, dict)
            or set(current) != {"exists", "registration", "index"}
            or current["exists"] is not True
            or set(current_registration) != set(registration)
            or current_registration.get("branch") != registration.get("branch")
            or current_registration.get("locked") not in (None, reason)
            or not isinstance(current_registration.get("head"), str)
            or re.fullmatch(oid, current_registration["head"]) is None
            or not isinstance(current.get("index"), str)
            or re.fullmatch(oid, current["index"]) is None):
        raise manager_git_journal.MutationEvidenceError(
            "관리 worktree 정리 중간 잠금 상태를 확인할 수 없습니다.")
    relocked = {**current, "registration": {**current_registration, "locked": reason}}
    if reason is not None and current_registration["locked"] is None:
        git(repo, "worktree", "lock", f"--reason={reason}", str(worktree))
    if observe() != relocked:
        raise manager_git_journal.MutationEvidenceError(
            "관리 worktree 정리 잠금 복원이 확인되지 않았습니다.")
    return relocked


def remove_cleanup_worktree(
    effects: RemoveCleanupWorktreeEffects, repo: Path, worktree: Path, assessment: CleanupAssessment,
    *, state_dir: Path | None = None, owner: str | None = None,
) -> CleanupAssessment:
    if assessment.status != "safe":
        return assessment
    try:
        def still_safe() -> CleanupAssessment:
            if not assessment.pr_head_oid or assessment.observation is None:
                return CleanupAssessment("unverifiable", "worktree 정리 판정 근거가 없습니다.")
            current = effects.assess_cleanup(worktree, assessment.pr_head_oid)
            if current.status == "safe" and current.observation != assessment.observation:
                return CleanupAssessment("unverifiable", "worktree 정리 판정 이후 HEAD, branch 또는 index가 변경됐습니다.")
            return current

        current = still_safe()
        if current.status != "safe":
            return current
        _head, _branch, _index, dirty = effects._capture_local_worktree_state(worktree)
        if dirty or controlling_git_operation(effects._git_operation_entries(worktree)):
            return CleanupAssessment("dirty", "worktree 제거 직전 권위 있는 변경이 있습니다.")
        if state_dir is None:
            current = still_safe()
            if current.status != "safe":
                return current
            effects.git(repo, "worktree", "remove", str(worktree))
        else:
            if not owner:
                raise WorkspaceOwnershipError("관리 worktree 정리 journal의 계획 identity가 없습니다.")
            before = effects._managed_worktree_transition_state(repo, worktree)
            head = before["registration"]["head"] if before["registration"] else None
            tree = effects.git(repo, "rev-parse", "--verify", f"{head}^{{tree}}").stdout.strip()
            if (not before["exists"] or before["index"] != tree):
                raise WorkspaceOwnershipError("관리 worktree 정리 직전 index가 HEAD와 다릅니다.")
            expected = {"exists": False, "registration": None, "index": None}

            def marker(intent: Mapping[str, object]) -> bool:
                return effects._managed_worktree_marker(state_dir, intent, "remove")

            def apply(token: str) -> None:
                if effects._managed_worktree_transition_state(repo, worktree) != before:
                    raise WorkspaceOwnershipError("관리 worktree 정리 직전 상태가 변경됐습니다.")
                if still_safe().status != "safe":
                    raise WorkspaceOwnershipError("관리 worktree 정리 직전 안전 판정이 변경됐습니다.")
                if before["registration"]["locked"]:
                    effects.git(repo, "worktree", "unlock", str(worktree))
                try:
                    if still_safe().status != "safe":
                        raise WorkspaceOwnershipError("관리 worktree 정리 직전 안전 판정이 변경됐습니다.")
                    effects.git(repo, "worktree", "remove", str(worktree))
                except RuntimeError:
                    if before["registration"]["locked"]:
                        restore_cleanup_unlock(
                            {"kind": "managed_worktree_remove", "before": before,
                             "expected": expected, "owner": owner,
                             "target": str(effects._canonical_path(worktree))},
                            effects._managed_worktree_transition_state(repo, worktree),
                            repo, worktree, git=effects.git,
                            observe=lambda: effects._managed_worktree_transition_state(repo, worktree),
                        )
                    raise
                effects._write_managed_worktree_receipt(
                    state_dir, token, repo, worktree, before, expected, "remove",
                )

            manager_git_journal.execute(
                state_dir, owner=owner, kind="managed_worktree_remove",
                repo=str(effects._canonical_path(repo)),
                target=str(effects._canonical_path(worktree)), before=before,
                expected=expected,
                observe=lambda: effects._managed_worktree_transition_state(repo, worktree),
                apply=apply, marker=marker,
                restore_intermediate=lambda intent, observed: restore_cleanup_unlock(
                    intent, observed, repo, worktree, git=effects.git,
                    observe=lambda: effects._managed_worktree_transition_state(repo, worktree),
                ),
            )
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 제거 실패: {error}")
    return assessment


def _store_cleanup_pending(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
    reason: str,
    *,
    workspace_owned: int = WORKSPACE_OWNED,
) -> None:
    connection.execute(
        """
        INSERT INTO cleanup_pending (
            repo, worktree, pr_url, pr_head_oid, reason, workspace_owned
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, worktree) DO UPDATE SET
            pr_url = excluded.pr_url,
            pr_head_oid = excluded.pr_head_oid,
            reason = excluded.reason,
            workspace_owned = excluded.workspace_owned,
            updated_at = CURRENT_TIMESTAMP
        """,
        (repo, str(worktree), pr_url, pr_head_oid, reason, workspace_owned),
    )


@dataclass(frozen=True)
class RecheckCleanupPendingEffects:
    """Named effects for recheck_cleanup_pending; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _ensure_cleanup_pending_schema: Callable[..., object]
    _record_merged_worktree_cleanup: Callable[..., object]
    _recover_managed_worktree_removal: Callable[..., object]
    assess_cleanup: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    remove_cleanup_worktree: Callable[..., object]
    validate_pending_cleanup_worktree: Callable[..., object]


def recheck_cleanup_pending(
    effects: RecheckCleanupPendingEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
) -> list[sqlite3.Row]:
    effects._ensure_cleanup_pending_schema(connection)
    pending = connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()
    from loop_manager_internal.storage import task_calls
    def pending_writer(worktree):
        if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exec_plans'").fetchone():
            return False
        return any(task_calls.pending_for_plan(connection, repo, row[0])
                   for row in connection.execute(
                       'SELECT plan_id FROM exec_plans WHERE repo=? AND worktree=?',
                       (repo, str(worktree))).fetchall())

    for item in pending:
        worktree = Path(item["worktree"])
        if pending_writer(worktree):
            continue
        if int(item["workspace_owned"]) != WORKSPACE_OWNED:
            if not worktree.exists() and not worktree.is_symlink():
                connection.execute(
                    "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                    (repo, str(worktree)),
                )
            continue
        try:
            effects._recover_managed_worktree_removal(state_dir, repo_path, worktree)
            effects.validate_pending_cleanup_worktree(
                repo_path,
                state_dir,
                repo,
                worktree,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            assessment = CleanupAssessment(
                "unverifiable",
                f"cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = effects.assess_cleanup(worktree, item["pr_head_oid"])
            registration = (effects.capture_git_snapshot(repo_path).worktrees.get(str(effects._canonical_path(worktree)))
                            if assessment.status == 'safe' else None)
            if pending_writer(worktree):
                continue
            assessment = effects.remove_cleanup_worktree(
                repo_path, worktree, assessment, state_dir=state_dir,
                owner=f"plan:{worktree.name}",
            )
            if (assessment.status == 'safe' and registration and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exec_plans'").fetchone()):
                plan = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND worktree = ? "
                    "AND status = 'MERGED' AND workspace_owned = ? AND pr_head_oid = ?",
                    (repo, str(worktree), WORKSPACE_OWNED, item['pr_head_oid'])).fetchone()
                if plan is not None:
                    effects._record_merged_worktree_cleanup(connection, repo_path, repo, plan['plan_id'],
                        worktree, plan['branch'], item['pr_head_oid'], assessment, registration)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            connection.execute(
                """
                UPDATE cleanup_pending
                SET reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND worktree = ?
                """,
                (assessment.reason or assessment.status, repo, str(worktree)),
            )
    connection.commit()
    return connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()
