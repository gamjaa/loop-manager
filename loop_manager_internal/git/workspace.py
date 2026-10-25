"""Managed workspace creation and identity effects."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.agents.agent_scope_decision import index_security_flags
from loop_manager_internal.recovery.checkpoint_ownership import committed_tree_matches_intent
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.runtime.runtime_identity import _canonical_path
from pathlib import Path
import hashlib
import json
import re
import secrets


@dataclass(frozen=True)
class ValidateManagedWorktreeEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _rebase_in_progress_for_branch: Callable[..., object]
    _worktree_catalog: Callable[..., object]

@dataclass(frozen=True)
class ValidateReservedWorktreeEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    capture_git_snapshot: Callable[..., object]
    validate_managed_worktree: Callable[..., object]

@dataclass(frozen=True)
class MakeWorktreeEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _managed_worktree_marker: Callable[..., object]
    _managed_worktree_transition_state: Callable[..., object]
    _worktree_catalog: Callable[..., object]
    _write_managed_worktree_receipt: Callable[..., object]
    git: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_reserved_worktree: Callable[..., object]

@dataclass(frozen=True)
class MakePlanningWorktreeEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    git: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_reserved_worktree: Callable[..., object]


@dataclass(frozen=True)
class ValidateExecplanWorkspaceIdentityEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _validate_managed_path_boundary: Callable[..., object]
    expected_execplan_workspace: Callable[..., object]


def validate_managed_worktree(effects: ValidateManagedWorktreeEffects,
    repo: Path,
    worktree: Path,
    branch: str | None,
    *,
    allow_rebase: bool = False,
) -> None:
    """Prove that a path is registered to this repository and expected branch."""

    if not worktree.is_dir():
        raise WorkspaceOwnershipError(f"관리 worktree 경로가 없습니다: {worktree}")
    expected_path = _canonical_path(worktree)
    record = next(
        (
            item
            for item in effects._worktree_catalog(repo)
            if _canonical_path(Path(str(item.get("worktree", "")))) == expected_path
        ),
        None,
    )
    if record is None:
        raise WorkspaceOwnershipError(
            f"Git에 등록되지 않은 관리 worktree입니다: {worktree}"
        )
    actual_branch = record.get("branch")
    if branch is None:
        if "detached" not in record:
            raise WorkspaceOwnershipError(
                f"planning worktree가 detached HEAD가 아닙니다: {worktree}"
            )
    elif actual_branch != f"refs/heads/{branch}":
        if (
            allow_rebase
            and "detached" in record
            and effects._rebase_in_progress_for_branch(worktree, branch)
        ):
            return
        rendered = actual_branch or "detached"
        raise WorkspaceOwnershipError(
            f"관리 worktree branch 불일치: {worktree} "
            f"(expected refs/heads/{branch}, actual {rendered})"
        )


def validate_reserved_worktree(effects: ValidateReservedWorktreeEffects,
    repo: Path,
    worktree: Path,
    branch: str | None,
    expected_head: str,
) -> None:
    """Prove that a reserved path is the clean worktree Manager intended."""

    if not expected_head:
        raise WorkspaceOwnershipError(
            "예약 workspace의 기준 commit SHA가 저장되지 않았습니다."
        )
    effects.validate_managed_worktree(repo, worktree, branch)
    snapshot = effects.capture_git_snapshot(worktree)
    violations: list[str] = []
    if snapshot.head != expected_head:
        violations.append(
            f"HEAD {snapshot.head} (expected {expected_head})"
        )
    if snapshot.dirty:
        paths = ", ".join(sorted(snapshot.dirty)[:8])
        violations.append(f"변경 파일 {paths}")
    if snapshot.operations:
        operations = ", ".join(sorted(snapshot.operations))
        violations.append(f"Git operation {operations}")
    if violations:
        raise WorkspaceOwnershipError(
            "예약 workspace가 저장된 생성 기준과 다릅니다: "
            + "; ".join(violations)
        )


def reserved_worktree_creation_head(
    state_dir: Path, repo: Path, worktree: Path, branch: str, owner: str,
    live: dict[str, object],
    *, explain: Callable[[dict], bool] | None = None,
) -> str:
    """Resolve one reservation from sealed creation evidence for this resource."""
    for item in manager_git_journal.verified_managed_transitions(
        state_dir, str(_canonical_path(repo)), owner,
        kind="managed_worktree_create", target=str(_canonical_path(worktree)),
    ):
        created = item['intent']['expected']
        if (created != item['result']['observed']
                or not live.get('exists') or not isinstance(live.get('registration'), dict)
                or created.get('registration', {}).get('branch') != f'refs/heads/{branch}'):
            continue
        if explain is None:
            matches = created == live
        else:
            matches = (all(live['registration'].get(key) == value
                           for key, value in created['registration'].items() if key != 'head')
                       and explain(created))
        if matches:
            return str(created['registration']['head'])
    raise WorkspaceOwnershipError('예약 workspace의 Manager 생성 기록이 현재 상태와 다릅니다.')


def validate_reserved_worktree_history(
    state_dir, repo, worktree, branch, plan_id, live, snapshot, *,
    checkpoint, git, base, session=None, sessions=(), agent_scopes=(),
):
    """Explain this local state from sealed creation and existing owned effects.

    This is ownership recovery evidence, never an Agent access or success gate.
    Callers hold the resource leases and CAS the selected checkpoint afterward.
    """
    saved = checkpoint.snapshot or {}
    if (re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', snapshot.head) is None
            or snapshot.branch != branch or not snapshot.shared_metadata_complete
            or not isinstance(live.get('registration'), dict)
            or live['registration'].get('head') != snapshot.head
            or snapshot.operations or checkpoint.operation not in {None, 'commit'}
            or saved.get('worktree', str(worktree)) != str(worktree)
            or saved.get('branch', branch) != branch
            or any(index_security_flags(entries) for entries in snapshot.index.values())):
        raise WorkspaceOwnershipError('예약 workspace의 현재 자원 또는 operation 근거가 유효하지 않습니다.')
    if checkpoint.operation == 'commit' and (
            saved.get('before_head') != checkpoint.expected_head
            or not saved.get('commit_message') or not saved.get('commit_tree_oid')
            or (snapshot.head == checkpoint.expected_head and live.get('index') != saved['commit_tree_oid'])
            or (snapshot.head != checkpoint.expected_head and not committed_tree_matches_intent(
                repo, checkpoint.expected_head, snapshot.head, saved, git))):
        raise WorkspaceOwnershipError('예약 workspace의 Manager commit intent가 현재 상태와 다릅니다.')

    def local(value):
        return json.dumps({key: (value[key] if isinstance(value, dict) else getattr(value, key))
                           for key in ('head', 'branch', 'index', 'dirty', 'operations')}, sort_keys=True)

    clean_states = {}
    def clean(head):
        if head not in clean_states:
            index = {}
            for entry in git(repo, 'ls-tree', '-r', '--full-tree', '-z', head).stdout.split('\0'):
                if not entry:
                    continue
                metadata, path = entry.split('\t', 1)
                mode, _kind, oid = metadata.split()
                index[path] = (f'H {mode} {oid} 0',)
            clean_states[head] = local({'head': head, 'branch': branch, 'index': index,
                                        'dirty': {}, 'operations': {}})
        return clean_states[head]

    def explain(created):
        creation_head = str(created['registration']['head'])
        adoption = saved.get('remote_adoption')
        if adoption is not None and (
                not isinstance(adoption, dict) or adoption.get('version') != 1
                or any(adoption.get(key) != value for key, value in {
                    'repo': str(repo), 'plan_id': plan_id, 'branch': branch,
                    'base': base, 'head': creation_head}.items())):
            return False
        # The unchanged creation path retains its existing no-history contract.
        if created == live and snapshot.head == creation_head and not snapshot.dirty:
            return checkpoint.operation is None
        if not snapshot.dirty and live.get('index') != git(repo, 'rev-parse', f'{snapshot.head}^{{tree}}').stdout.strip():
            return False
        reachable = {clean(creation_head)}
        edges = [(local(start), local(result)) for start, result in agent_scopes
                 if start.head == result.head and start.branch == result.branch == branch
                 and start.operations == result.operations
                 and start.shared_metadata_complete and result.shared_metadata_complete]
        for owned_session in dict.fromkeys((None, session, *sessions)):
            for item in manager_git_journal.verified_managed_transitions(
                    state_dir, str(_canonical_path(repo)), f'plan:{plan_id}',
                    agent_session_id=owned_session, git=git,
                    kind='managed_rebase', target=str(_canonical_path(worktree))):
                if item['intent']['expected']['branch'] == branch:
                    edges.append((local(item['intent']['before']), local(item['result']['observed'])))
        commits = [(saved.get('before_head'), saved.get('after_head') or (
                    snapshot.head if checkpoint.operation == 'commit' else None), saved)]
        for request in saved.get('worker_checkpoint_requests', ()):
            if (isinstance(request, dict) and all(request.get(key) == value for key, value in {
                    'repo': str(repo), 'plan_id': plan_id, 'branch': branch, 'worktree': str(worktree)}.items())):
                commits.append((request.get('start_head'), request.get('head'), {
                    'before_head': request.get('start_head'), 'commit_message': request.get('title'),
                    'commit_tree_oid': request.get('tree')}))
        commit_edges = []
        for before, after, intent in commits:
            if (isinstance(before, str) and isinstance(after, str)
                    and all(re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', oid) for oid in (before, after))
                    and committed_tree_matches_intent(repo, before, after, intent, git)):
                commit_edges.append((before, clean(after)))
        # ponytail: the task-local receipt scan is quadratic; use adjacency
        # queues if long-lived task histories make this observation expensive.
        while True:
            previous = len(reachable)
            reachable.update(after for before, after in edges if before in reachable)
            heads = {json.loads(state)['head'] for state in reachable}
            reachable.update(after for before, after in commit_edges if before in heads)
            if len(reachable) == previous:
                return local(snapshot) in reachable

    return reserved_worktree_creation_head(
        state_dir, repo, worktree, branch, f'plan:{plan_id}', live, explain=explain)


def make_worktree(effects: MakeWorktreeEffects,
    repo: Path,
    worktree: Path,
    branch: str,
    base: str,
    *,
    allow_recovery: bool = True,
    allow_existing: bool = True,
    allow_rebase: bool = False,
    allow_create_branch: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
    start_point: str | None = None,
    before_create: Callable[[], None] | None = None,
    journal_state_dir: Path | None = None,
    journal_owner: str | None = None,
    require_creation_evidence: bool = False,
    reservation_validator: Callable[[dict], str] | None = None,
) -> str:
    """Create, recover, or validate one owned ExecPlan worktree."""

    if reservation_validator is not None and (not ownership_reserved or journal_state_dir is None or not journal_owner):
        raise WorkspaceOwnershipError('예약 변경 이력 검증에는 생성 예약 journal identity가 필요합니다.')

    if start_point is not None and (
        ownership_reserved or expected_head is not None
        or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", start_point)
    ):
        raise WorkspaceOwnershipError("원격 작업 branch 시작 commit이 유효하지 않습니다.")
    if worktree.exists() and not worktree.is_dir():
        raise WorkspaceOwnershipError(
            f"관리 worktree 경로가 디렉터리가 아닙니다: {worktree}"
        )
    if journal_state_dir is not None:
        if not journal_owner:
            raise WorkspaceOwnershipError("관리 worktree journal의 계획 identity가 없습니다.")
        manager_git_journal.recover_active(
            journal_state_dir, repo=str(_canonical_path(repo)),
            kind="managed_worktree_create", target=str(_canonical_path(worktree)),
            observe=lambda: effects._managed_worktree_transition_state(repo, worktree),
            marker=lambda intent: effects._managed_worktree_marker(
                journal_state_dir, intent, "create",
            ),
        )
    reserved_state = None
    if require_creation_evidence or (
        ownership_reserved and journal_state_dir is not None and worktree.is_dir()
    ):
        if journal_state_dir is None or not journal_owner or not ownership_reserved:
            raise WorkspaceOwnershipError('원격 인수 workspace의 생성 예약 근거가 없습니다.')
        live = effects._managed_worktree_transition_state(repo, worktree)
        created_head = (reservation_validator(live) if reservation_validator is not None else
                        reserved_worktree_creation_head(
                            journal_state_dir, repo, worktree, branch, journal_owner, live))
        if require_creation_evidence and created_head != expected_head:
            raise WorkspaceOwnershipError('원격 인수 workspace의 생성 HEAD가 인수 기준과 다릅니다.')
        # 실행 기준은 생성 후 전진할 수 있으므로 봉인된 생성 결과로 예약을 확인한다.
        expected_head = created_head
        reserved_state = live
    if ownership_reserved and not expected_head:
        raise WorkspaceOwnershipError("예약 ExecPlan workspace의 기준 commit SHA가 없습니다.")
    if worktree.is_dir():
        if not allow_existing:
            raise WorkspaceOwnershipError(
                f"새 ExecPlan의 관리 worktree가 이미 존재해 소유권을 증명할 수 없습니다: "
                f"{worktree}"
            )
        if ownership_reserved:
            if reservation_validator is None:
                effects.validate_reserved_worktree(repo, worktree, branch, str(expected_head))
            else:
                reservation_validator(reserved_state)
            if (reserved_state is not None
                    and effects._managed_worktree_transition_state(repo, worktree) != reserved_state):
                raise WorkspaceOwnershipError('예약 workspace 검증 중 Git 등록 또는 index가 변경됐습니다.')
        else:
            effects.validate_managed_worktree(
                repo, worktree, branch, allow_rebase=allow_rebase
            )
        return "existing"

    worktree.parent.mkdir(parents=True, exist_ok=True)
    if journal_state_dir is None:
        effects.git(repo, "worktree", "prune", "--expire", "now")
    branch_exists = (
        effects.git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode
        == 0
    )
    if branch_exists:
        if (allow_create_branch and not ownership_reserved) or not allow_recovery:
            raise WorkspaceOwnershipError(
                f"새 ExecPlan의 관리 branch가 이미 존재해 소유권을 증명할 수 없습니다: "
                f"{branch}"
            )
        for record in effects._worktree_catalog(repo):
            if record.get("branch") == f"refs/heads/{branch}":
                owner = Path(str(record["worktree"]))
                raise WorkspaceOwnershipError(
                    f"관리 branch가 다른 worktree에 연결되어 있습니다: "
                    f"{branch} ({owner})"
                )
        if ownership_reserved:
            branch_head = effects.git(
                repo,
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}",
            ).stdout.strip()
            if branch_head != expected_head:
                raise WorkspaceOwnershipError(
                    "예약 ExecPlan branch가 저장된 기준 commit과 다릅니다: "
                    f"{branch} ({branch_head}, expected {expected_head})"
                )
        arguments = ("worktree", "add", str(worktree), branch)
        outcome = "recovered"
    elif allow_create_branch or ownership_reserved:
        if before_create is not None:
            before_create()
        selected_start = start_point or expected_head or f"origin/{base}"
        if start_point is not None:
            resolved = effects.git(repo, "rev-parse", "--verify", f"{start_point}^{{commit}}")
            if resolved.stdout.strip() != start_point:
                raise WorkspaceOwnershipError("원격 작업 branch 시작 commit이 관측값과 다릅니다.")
        arguments = ("worktree", "add", "-b", branch, str(worktree), selected_start)
        outcome = "created"
    else:
        raise WorkspaceOwnershipError(
            f"저장된 worktree와 관리 branch를 모두 복구할 수 없습니다: "
            f"{worktree} ({branch})"
        )
    if journal_state_dir is None:
        effects.git(repo, *arguments)
    else:
        before = effects._managed_worktree_transition_state(repo, worktree)
        if before != {"exists": False, "registration": None, "index": None}:
            raise WorkspaceOwnershipError("관리 worktree 생성 직전 등록 상태가 변경됐습니다.")
        head = effects.git(
            repo, "rev-parse", "--verify",
            selected_start if outcome == "created" else branch,
        ).stdout.strip()
        tree = effects.git(repo, "rev-parse", "--verify", f"{head}^{{tree}}").stdout.strip()
        mutation_id = secrets.token_hex(16)
        lock_reason = f"loop-manager-worktree:{mutation_id}"
        expected = {
            "exists": True,
            "registration": {"head": head, "branch": f"refs/heads/{branch}",
                             "locked": lock_reason},
            "index": tree,
        }

        def apply(token: str) -> None:
            effects._write_managed_worktree_receipt(
                journal_state_dir, token, repo, worktree, before, expected,
                "create",
            )
            effects.git(repo, "worktree", "add", "--lock", f"--reason={lock_reason}",
                *arguments[2:])

        manager_git_journal.execute(
            journal_state_dir, owner=journal_owner,
            kind="managed_worktree_create", repo=str(_canonical_path(repo)),
            target=str(_canonical_path(worktree)), before=before,
            expected=expected,
            observe=lambda: effects._managed_worktree_transition_state(repo, worktree),
            apply=apply,
            marker=lambda intent: effects._managed_worktree_marker(
                journal_state_dir, intent, "create",
            ),
        )
    if ownership_reserved:
        effects.validate_reserved_worktree(repo, worktree, branch, str(expected_head))
    else:
        effects.validate_managed_worktree(repo, worktree, branch, allow_rebase=allow_rebase)
    return outcome


def make_planning_worktree(effects: MakePlanningWorktreeEffects,
    repo: Path,
    worktree: Path,
    base: str,
    *,
    allow_create: bool = True,
    before_create: Callable[[], None] | None = None,
    allow_existing: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
) -> str:
    if ownership_reserved and not expected_head:
        raise WorkspaceOwnershipError(
            "예약 planning workspace의 기준 commit SHA가 없습니다."
        )
    if worktree.exists() and not worktree.is_dir():
        raise WorkspaceOwnershipError(
            f"planning worktree 경로가 디렉터리가 아닙니다: {worktree}"
        )
    if worktree.is_dir():
        if not allow_existing:
            raise WorkspaceOwnershipError(
                "새 planning worktree 경로가 이미 존재해 소유권을 증명할 수 "
                f"없습니다: {worktree}"
            )
        if ownership_reserved:
            effects.validate_reserved_worktree(repo, worktree, None, str(expected_head))
        else:
            effects.validate_managed_worktree(repo, worktree, None)
        return "existing"
    if not allow_create:
        raise WorkspaceOwnershipError(
            f"보존된 planning worktree를 복구할 branch가 없습니다: "
            f"{worktree}"
        )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if before_create is not None:
        before_create()
    start_point = expected_head or f"origin/{base}"
    effects.git(repo, "worktree", "add", "--detach", str(worktree), start_point)
    if ownership_reserved:
        effects.validate_reserved_worktree(repo, worktree, None, str(expected_head))
    else:
        effects.validate_managed_worktree(repo, worktree, None)
    return "created"


def expected_execplan_workspace(
    state_dir: Path,
    repo: str,
    plan_id: str,
) -> tuple[str, Path]:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return (
        f"loop-manager/{plan_id}",
        state_dir / "worktrees" / project_id / plan_id,
    )


def validate_execplan_workspace_identity(effects: ValidateExecplanWorkspaceIdentityEffects,
    state_dir: Path,
    repo: str,
    plan_id: str,
    branch: str,
    worktree: Path,
) -> None:
    expected_branch, expected_worktree = effects.expected_execplan_workspace(
        state_dir, repo, plan_id
    )
    if branch != expected_branch:
        raise WorkspaceOwnershipError(
            f"ExecPlan 관리 branch가 결정적 소유 경계와 다릅니다: "
            f"{branch} (expected {expected_branch})"
        )
    effects._validate_managed_path_boundary(
        state_dir,
        worktree,
        expected_worktree,
        "ExecPlan worktree",
    )
