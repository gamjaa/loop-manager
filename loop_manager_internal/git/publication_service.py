"""git publication service boundaries and named composition effects."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import re
import sqlite3


@dataclass(frozen=True)
class RemoteBranchOidEffects:
    """Named effects for remote_branch_oid; no entry-point/module access."""
    git: Callable[..., object]


def remote_branch_oid(effects: RemoteBranchOidEffects, repo: Path, branch: str) -> str | None:
    """Return the fetched remote branch OID without consulting local branch names."""

    result = effects.git(
        repo,
        "show-ref",
        "--hash",
        f"refs/remotes/origin/{branch}",
        check=False,
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        raise RuntimeError(
            f"원격 publication branch 확인 실패 (git 종료 코드 {result.returncode})."
        )
    oid = result.stdout.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid) is None:
        raise RuntimeError("원격 publication branch OID 형식이 유효하지 않습니다.")
    return oid


@dataclass(frozen=True)
class OwnedPublicationPlanEffects:
    """Named effects for _owned_publication_plan; no entry-point/module access."""
    active_execplan: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _owned_publication_plan(
    effects: OwnedPublicationPlanEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
) -> sqlite3.Row:
    plan = effects.active_execplan(connection, repo)
    if (
        plan is None
        or plan["status"] != "PUBLISHING"
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "publication branch의 Loop Manager 소유권을 SQLite에서 확인할 수 없습니다."
        )
    from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
    effects.validate_execplan_workspace_identity(
        state_dir,
        repo,
        ManualWorkRepository(connection).workspace_owner(repo,str(plan['plan_id'])),
        branch,
        worktree,
    )
    effects.validate_managed_worktree(repo_path, worktree, branch)
    return plan


@dataclass(frozen=True)
class RecordRemotePublicationBranchEffects:
    """Named effects for record_remote_publication_branch; no entry-point/module access."""
    _owned_publication_plan: Callable[..., object]
    git: Callable[..., object]
    update_project: Callable[..., object]


def record_remote_publication_branch(
    effects: RecordRemotePublicationBranchEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    remote_oid: str | None,
) -> None:
    """Remember only a remote branch proven to be this managed branch."""

    if remote_oid is None:
        return
    plan = effects._owned_publication_plan(
        connection, repo_path, state_dir, repo, branch, worktree
    )
    local_oid = effects.git(worktree, "rev-parse", "HEAD").stdout.strip()
    recorded_oid = str(plan["publication_remote_oid"] or "")
    if remote_oid == local_oid:
        if recorded_oid != remote_oid:
            effects.update_project(
                connection,
                repo,
                publication_remote_oid=remote_oid,
            )
        return
    if recorded_oid == remote_oid:
        return
    raise PublicationSafetyError(
        "PR이 없는 원격 branch가 현재 관리 HEAD 또는 저장된 게시 OID와 "
        f"일치하지 않아 갱신을 거부합니다: {branch}"
    )


@dataclass(frozen=True)
class PushManagedPublicationBranchEffects:
    """Named effects for push_managed_publication_branch; no entry-point/module access."""
    _owned_publication_plan: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    active_execplan: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]
    record_remote_publication_branch: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    update_project: Callable[..., object]


def push_managed_publication_branch(
    effects: PushManagedPublicationBranchEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    expected_oid: str,
) -> str:
    """Push the expected reviewed candidate with an explicit owned-branch lease."""

    plan = effects._owned_publication_plan(
        connection, repo_path, state_dir, repo, branch, worktree
    )
    local_oid = effects.git(worktree, "rev-parse", "HEAD").stdout.strip()
    if not expected_oid or local_oid != expected_oid:
        raise PublicationSafetyError('push 대상 HEAD가 verify·Review를 통과한 후보와 다릅니다.')
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(plan['plan_id']))
    def observe_remote():
        return managed_rebase_adoption.checkpoint_remote_oid(
            checkpoint, repo_path, branch, git=effects.git, read_fetched=effects.remote_branch_oid)
    remote_oid = observe_remote()
    if remote_oid is None and plan['publication_remote_oid']:
        raise PublicationSafetyError(
            '저장된 게시 branch가 원격에서 삭제돼 새 branch 생성으로 복구할 수 없습니다.'
        )
    effects.record_remote_publication_branch(
        connection,
        repo_path,
        state_dir,
        repo,
        branch,
        worktree,
        remote_oid,
    )
    if remote_oid == local_oid:
        return local_oid

    # The explicit destination needs no upstream; -u mutates shared config
    # and invalidates snapshots of other interrupted plans.
    push_arguments = [
        "push",
        "origin",
        f"{local_oid}:refs/heads/{branch}",
    ]
    if remote_oid is not None:
        recorded_oid = str(plan["publication_remote_oid"] or "")
        if recorded_oid != remote_oid:
            refreshed = effects.active_execplan(connection, repo)
            recorded_oid = str(
                refreshed["publication_remote_oid"] if refreshed else ""
            )
        if recorded_oid != remote_oid:
            raise PublicationSafetyError(
                "저장된 원격 branch OID와 fetch 결과가 달라 강제 갱신을 거부합니다."
            )
        live = effects.git(repo_path, 'ls-remote', '--heads', 'origin',
                   f'refs/heads/{branch}', check=False)
        if (live.returncode or
                [line.split() for line in live.stdout.splitlines() if line.strip()]
                != [[remote_oid, f'refs/heads/{branch}']]):
            raise PublicationSafetyError(
                'push 직전 원격 branch가 저장된 lease OID와 다릅니다.'
            )
        if plan['pr_url'] or effects.pr_for_branch(repo_path, branch) is not None:
            raise PublicationSafetyError(
                'PR이 있는 branch를 checkpoint 복구 강제 push할 수 없습니다.'
            )
        push_arguments.insert(
            1,
            f"--force-with-lease=refs/heads/{branch}:{remote_oid}",
        )
    else:
        live = effects.git(repo_path, 'ls-remote', '--heads', 'origin',
                   f'refs/heads/{branch}', check=False)
        if live.returncode or live.stdout.strip():
            raise PublicationSafetyError(
                '새 branch push 직전 원격 ref가 생성되거나 조회에 실패했습니다.'
            )
        push_arguments.insert(1, f"--force-with-lease=refs/heads/{branch}:")

    effects._owned_publication_plan(connection, repo_path, state_dir, repo, branch, worktree)
    if (effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip() != local_oid
            or effects.git(worktree, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip() != local_oid):
        raise PublicationSafetyError('push 직전 소유 branch 또는 관리 HEAD가 변경됐습니다.')
    if effects.pr_for_branch(repo_path, branch) is not None:
        raise PublicationSafetyError('push 직전 게시 대상 branch의 PR이 변경됐습니다.')

    pushed = effects.git(worktree, *push_arguments, check=False)
    if pushed.returncode:
        raise RuntimeError(f"publication branch push 실패:\n{pushed.stdout[-8000:]}")

    effects._recorded_base_fetch(repo_path, state_dir, branch, owner="project")
    confirmed_oid = observe_remote()
    if confirmed_oid != local_oid:
        raise RuntimeError(
            "publication branch push 결과를 원격에서 확인할 수 없습니다."
        )
    confirmed = effects.git(repo_path, 'ls-remote', '--heads', 'origin',
                    f'refs/heads/{branch}', check=False)
    if (confirmed.returncode or
            [line.split() for line in confirmed.stdout.splitlines() if line.strip()]
            != [[local_oid, f'refs/heads/{branch}']]):
        raise RuntimeError('publication branch push의 새 OID를 원격에서 확인할 수 없습니다.')
    effects.update_project(
        connection,
        repo,
        publication_remote_oid=local_oid,
    )
    return local_oid


@dataclass(frozen=True)
class PushOwnedPrConflictRecoveryEffects:
    """Named effects for push_owned_pr_conflict_recovery; no entry-point/module access."""
    _fetch_pr_conflict_recovery_refs: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    active_execplan: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def push_owned_pr_conflict_recovery(
    effects: PushOwnedPrConflictRecoveryEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    branch: str,
    worktree: Path,
    pull_request: Mapping[str, object],
    expected_oid: str,
) -> str:
    """Publish a reviewed candidate to the saved PR; rewrites require rebase ownership."""

    plan = effects.active_execplan(connection, repo)
    project = connection.execute(
        "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if (
        project is None
        or plan is None
        or plan["status"] != "PUBLISHING"
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "기존 PR 충돌 복구 게시 소유권을 SQLite에서 확인할 수 없습니다."
        )
    url = str(plan["pr_url"] or "")
    recovery = bool(plan["pr_conflict_recovery"])
    observed_head = str((plan["pr_conflict_head_oid"] if recovery
                         else plan["publication_remote_oid"]) or "")
    target_base = str((plan["pr_conflict_base_oid"] if recovery
                      else plan["validated_base_sha"]) or "")
    if (
        str(pull_request.get("state") or "").upper() != "OPEN"
        or str(pull_request.get("url") or "") != url
        or str(pull_request.get("headRefName") or "") != branch
        or str(pull_request.get("headRefOid") or "") != observed_head
        or (pull_request.get("baseRefName") is not None
            and pull_request["baseRefName"] != project["base_branch"])
        or (not recovery and str(plan["pr_head_oid"] or "") != observed_head)
    ):
        raise PublicationSafetyError(
            "게시 직전 PR URL, state, head branch 또는 head OID가 복구 "
            "시작 시 관측값과 다릅니다."
        )
    if (
        not observed_head
        or not target_base
        or str(plan["active_sha"] or "") != target_base
        or str(plan["validated_base_sha"] or "") != target_base
    ):
        raise PublicationSafetyError(
            "기존 PR 충돌 복구의 lease OID와 검증 base가 완전하지 않습니다."
        )
    effects.validate_execplan_workspace_identity(
        state_dir, repo, manual_workspace_owner(connection, repo, str(plan["plan_id"])), branch, worktree
    )
    effects.validate_managed_worktree(repo_path, worktree, branch)
    local_oid = effects.git(worktree, "rev-parse", "HEAD").stdout.strip()
    if not expected_oid or local_oid != expected_oid:
        raise PublicationSafetyError('PR push 대상 HEAD가 verify·Review를 통과한 후보와 다릅니다.')
    contains_base = effects.git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        local_oid,
        check=False,
    )
    if contains_base.returncode != 0:
        raise PublicationSafetyError(
            "검증된 base OID가 게시할 관리 HEAD의 조상이 아닙니다."
        )
    if not recovery and effects.git(
            worktree, 'merge-base', '--is-ancestor', observed_head, local_oid,
            check=False).returncode != 0:
        raise PublicationSafetyError('일반 PR 후보가 확인된 원격 checkpoint의 후속 commit이 아닙니다.')

    effects._fetch_pr_conflict_recovery_refs(
        repo_path,
        state_dir,
        str(project["base_branch"]),
        branch,
    )
    remote_oid = effects.remote_branch_oid(repo_path, branch)
    if remote_oid != observed_head:
        raise PublicationSafetyError(
            "게시 직전 원격 PR head가 복구 시작 시 관측값과 달라졌습니다."
        )
    current_pr = effects.pr_for_branch(repo_path, branch, url)
    if (current_pr is None or any(current_pr.get(key) != pull_request.get(key)
                                 for key in ('url', 'state', 'headRefName', 'headRefOid', 'baseRefName'))):
        raise PublicationSafetyError('push 직전 PR identity 또는 head가 변경됐습니다.')
    effects.validate_managed_worktree(repo_path, worktree, branch)
    if (effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip() != local_oid
            or effects.git(worktree, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip() != local_oid):
        raise PublicationSafetyError('PR push 직전 소유 branch 또는 관리 HEAD가 변경됐습니다.')
    if remote_oid == local_oid:
        # A validated candidate can already be the confirmed PR checkpoint.
        # Reuse its live identity after the same validation gates.
        return local_oid
    pushed = effects.git(
        worktree,
        "push",
        *([f"--force-with-lease=refs/heads/{branch}:{observed_head}"] if recovery else []),
        "origin",
        f"{local_oid}:refs/heads/{branch}",
        check=False,
    )
    if pushed.returncode:
        normalized = pushed.stdout.casefold()
        if any(
            marker in normalized
            for marker in ("stale info", "fetch first", "rejected", "lease")
        ):
            raise PublicationSafetyError(
                "기존 PR branch lease가 거부되어 원격 변경을 덮어쓰지 않았습니다:\n"
                + pushed.stdout[-8000:]
            )
        raise RuntimeError(
            f"기존 PR 충돌 복구 push 실패:\n{pushed.stdout[-8000:]}"
        )

    effects._recorded_base_fetch(repo_path, state_dir, branch, owner="project")
    if effects.remote_branch_oid(repo_path, branch) != local_oid:
        raise RuntimeError(
            "기존 PR 충돌 복구 push 결과를 원격에서 확인할 수 없습니다."
        )
    return local_oid


@dataclass(frozen=True)
class RecoverConfirmedPrConflictPushEffects:
    """Named effects for recover_confirmed_pr_conflict_push; no entry-point/module access."""
    _fetch_pr_conflict_recovery_refs: Callable[..., object]
    active_execplan: Callable[..., object]
    git: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def recover_confirmed_pr_conflict_push(
    effects: RecoverConfirmedPrConflictPushEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> bool:
    """Recover only a previously pushed local HEAD, never an arbitrary remote head."""

    if project["phase"] != "PUBLISHING":
        return False
    plan = effects.active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    remote_head = str(pull_request.get("headRefOid") or "")
    recovery = bool(project["pr_conflict_recovery"])
    observed_head = str((project["pr_conflict_head_oid"] if recovery
                         else project["publication_remote_oid"]) or "")
    target_base = str((project["pr_conflict_base_oid"] if recovery
                      else project["validated_base_sha"]) or "")
    if (
        plan is None
        or plan["status"] != "PUBLISHING"
        or bool(plan["pr_conflict_recovery"]) != recovery
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
        or str(pull_request.get("state") or "").upper() != "OPEN"
        or str(pull_request.get("url") or "") != str(project["pr_url"] or "")
        or str(pull_request.get("headRefName") or "") != branch
        or (pull_request.get("baseRefName") is not None
            and pull_request["baseRefName"] != project["base_branch"])
        or not remote_head
        or not observed_head
        or remote_head == observed_head
        or (not recovery and (plan["pr_head_oid"] not in {observed_head, remote_head}
                              or project["pr_head_oid"] != plan["pr_head_oid"]
                              or plan["publication_remote_oid"] != observed_head))
        or not target_base
        or str(project["active_sha"] or "") != target_base
        or str(project["validated_base_sha"] or "") != target_base
        or str(plan["active_sha"] or "") != target_base
        or str(plan["validated_base_sha"] or "") != target_base
        or not project["publication_token"]
        or plan["publication_token"] != project["publication_token"]
    ):
        return False
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(plan["plan_id"]))
    if checkpoint.operation not in {None, "push"}:
        return False
    if (not recovery and plan['pr_head_oid'] != observed_head
            and checkpoint.operation != 'push'):
        return False
    if checkpoint.operation == 'push' and (
            checkpoint.expected_head != remote_head
            or (checkpoint.snapshot or {}).get('remote_before') != observed_head):
        return False
    definition = DefinitionSnapshot.deserialize(plan["definition_snapshot"])
    if (definition is None
            or plan["publication_token"] != definition.publication_token(target_base, remote_head)):
        return False
    effects.validate_execplan_workspace_identity(
        state_dir, repo, manual_workspace_owner(connection, repo, str(plan["plan_id"])), branch, worktree
    )
    effects.validate_managed_worktree(repo_path, worktree, branch)
    local_oid = effects.git(worktree, "rev-parse", "HEAD").stdout.strip()
    if remote_head != local_oid:
        return False
    contains_base = effects.git(
        worktree,
        "merge-base",
        "--is-ancestor",
        target_base,
        local_oid,
        check=False,
    )
    if contains_base.returncode != 0:
        return False
    if not recovery and effects.git(
            worktree, 'merge-base', '--is-ancestor', observed_head, local_oid,
            check=False).returncode != 0:
        return False
    effects._fetch_pr_conflict_recovery_refs(
        repo_path, state_dir, str(project["base_branch"]), branch
    )
    if effects.remote_branch_oid(repo_path, branch) != local_oid:
        return False
    if effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip() != local_oid:
        return False
    with runtime_state._transaction(connection):
        current_plan = effects.active_execplan(connection, repo)
        repository = runtime_state.RuntimeStateRepository(connection)
        projected = repository.projected(repo)
        ownership_fields = ('plan_id', 'status', 'branch', 'worktree', 'workspace_owned',
            'pr_url', 'pr_head_oid', 'publication_remote_oid', 'active_sha', 'validated_base_sha',
            'pr_conflict_recovery', 'pr_conflict_head_oid', 'pr_conflict_base_oid',
            'definition_snapshot', 'publication_token')
        if (current_plan is None or projected is None or projected.plan_id != plan['plan_id']
                or projected.status != 'PUBLISHING'
                or any(current_plan[key] != plan[key] for key in ownership_fields)):
            raise PublicationSafetyError('최종 PR push 확인 중 실행 소유권이 변경됐습니다.')
        if checkpoint.operation == 'push':
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, str(plan['plan_id']), expected_generation=checkpoint.generation,
                operation=None, expected_head=local_oid,
                target_base_oid=checkpoint.target_base_oid, observed_remote_oid=local_oid,
                snapshot={**(checkpoint.snapshot or {}), 'pushed_checkpoint_head': local_oid})
        repository.open_pull_request(
            repo,
            publication_remote_oid=local_oid,
            pr_head_oid=local_oid,
            pr_conflict_recovery=0,
            pr_conflict_head_oid=None,
            pr_conflict_base_oid=None,
        )
    return True
