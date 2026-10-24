"""git publication service boundaries and named composition effects."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.db_access import CoordinatedConnection
from pathlib import Path
import re
from copy import deepcopy
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


def _current_pr(effects, connection, repo, plan, repo_path, branch, url=None):
    from loop_manager_internal.recovery.deleted_branch_restart import current_pr
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, str(plan['plan_id']))
    return current_pr(checkpoint, effects.pr_for_branch(repo_path, branch, url)
                      if url else effects.pr_for_branch(repo_path, branch))


@dataclass(frozen=True)
class OwnedPublicationPlanEffects:
    """Named effects for _owned_publication_plan; no entry-point/module access."""
    active_execplan: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    git: Callable[..., object] | None = None
    github_repository: Callable[..., object] | None = None


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
    if effects.git is not None and effects.github_repository is not None:
        project = connection.execute('SELECT origin FROM projects WHERE repo=?', (repo,)).fetchone()
        for location in (repo_path, worktree):
            for args in (('remote', 'get-url', '--all', 'origin'), ('remote', 'get-url', '--push', '--all', 'origin')):
                urls = effects.git(location, *args).stdout.splitlines()
                if project is None or len(urls) != 1 or effects.github_repository(urls[0]) != project['origin']:
                    raise PublicationSafetyError('현재 등록 origin과 게시 대상이 다릅니다.')
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
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, str(plan['plan_id']))
    if (checkpoint.snapshot or {}).get('accepted_work_branch') == remote_oid:
        return
    raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
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
        raise RecoveryFailure(RecoveryReason.REMOTE_BRANCH_DELETED,
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
        if recorded_oid != remote_oid and (checkpoint.snapshot or {}).get('accepted_work_branch') != remote_oid:
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                "저장된 원격 branch OID와 fetch 결과가 달라 강제 갱신을 거부합니다."
            )
        live = effects.git(repo_path, 'ls-remote', '--heads', 'origin',
                   f'refs/heads/{branch}', check=False)
        if (live.returncode or
                [line.split() for line in live.stdout.splitlines() if line.strip()]
                != [[remote_oid, f'refs/heads/{branch}']]):
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                'push 직전 원격 branch가 저장된 lease OID와 다릅니다.'
            )
        if plan['pr_url'] or _current_pr(effects, connection, repo, plan, repo_path, branch) is not None:
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
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                '새 branch push 직전 원격 ref가 생성되거나 조회에 실패했습니다.'
            )
        push_arguments.insert(1, f"--force-with-lease=refs/heads/{branch}:")

    effects._owned_publication_plan(connection, repo_path, state_dir, repo, branch, worktree)
    if (effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip() != local_oid
            or effects.git(worktree, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip() != local_oid):
        raise PublicationSafetyError('push 직전 소유 branch 또는 관리 HEAD가 변경됐습니다.')
    if _current_pr(effects, connection, repo, plan, repo_path, branch) is not None:
        raise PublicationSafetyError('push 직전 게시 대상 branch의 PR이 변경됐습니다.')

    pushed = effects.git(worktree, *push_arguments, check=False)
    if pushed.returncode:
        if observe_remote() != remote_oid or any(value in pushed.stdout.lower() for value in ('stale info', 'fetch first', 'non-fast-forward')):
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, pushed.stdout[-8000:])
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
class WorkerCheckpointPublicationEffects:
    """Effects for manager-owned Worker checkpoint commits."""
    active_execplan: Callable[..., object]
    git: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    pr_for_branch: Callable[..., object]
    github_repository: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    update_project: Callable[..., object]


def push_worker_checkpoint_commits(
    effects: WorkerCheckpointPublicationEffects,
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    plan_id: str,
    branch: str,
    worktree: Path,
    base: str,
    remote_oid: str | None,
    pull_request: Mapping[str, object] | None,
    *, allow_common_plans: bool = False,
) -> tuple[str, int]:
    """Push each already-validated linear Worker commit under an exact lease."""
    if connection.in_transaction:
        raise PublicationSafetyError('Worker checkpoint 게시 전에 DB transaction을 종료해야 합니다.')
    plan = effects.active_execplan(connection, repo)
    if (plan is None or plan['plan_id'] != plan_id
            or plan['status'] not in {'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'}
            or int(plan['workspace_owned']) != WORKSPACE_OWNED
            or str(plan['branch'] or '') != branch
            or Path(str(plan['worktree'] or '')) != worktree
            or branch != f'loop-manager/{plan_id}'):
        raise PublicationSafetyError('Worker checkpoint의 작업·branch·worktree 소유권이 다릅니다.')
    project = connection.execute(
        'SELECT origin, base_branch FROM projects WHERE repo=?', (repo,)
    ).fetchone()
    if project is None or project['base_branch'] != base or not project['origin']:
        raise PublicationSafetyError('Worker checkpoint의 등록 저장소·base를 확인할 수 없습니다.')
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    effects.validate_execplan_workspace_identity(
        state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree,
    )
    effects.validate_managed_worktree(repo_path, worktree, branch)
    def validate_origin():
        for location in (repo_path, worktree):
            for arguments in (('remote', 'get-url', '--all', 'origin'),
                              ('remote', 'get-url', '--push', '--all', 'origin')):
                urls = effects.git(location, *arguments).stdout.splitlines()
                if len(urls) != 1 or effects.github_repository(urls[0]) != project['origin']:
                    raise PublicationSafetyError('등록 origin과 Worker checkpoint 대상이 다릅니다.')
    validate_origin()
    branch_head = effects.git(worktree, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
    head = effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    local_ref = effects.git(repo_path, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip()
    if branch_head.returncode or branch_head.stdout.strip() != branch or local_ref != head:
        raise PublicationSafetyError('Worker checkpoint의 관리 branch HEAD가 변경됐습니다.')
    tracked_remote = effects.remote_branch_oid(repo_path, branch)
    if tracked_remote != remote_oid:
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                              'Worker checkpoint의 fetch 원격 OID가 변경됐습니다.')
    live_remote = manager_git_journal.live_remote_branch_oid(effects.git, repo_path, branch)
    if live_remote != remote_oid:
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                              'Worker checkpoint 게시 전 원격 branch가 전진했습니다.')

    if current.operation is not None:
        raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN,
                              'Worker checkpoint 게시 전에 진행 중인 Manager Git operation을 끝내야 합니다.')
    owner = dict(plan)

    def persist(expected, snapshot, *, head, remote, values=None):
        nonlocal owner
        # Observe Git/PR outside this short CAS. A DB retry never repeats a push.
        def apply(connection):
            selected = effects.active_execplan(connection, repo)
            registration = connection.execute('SELECT origin, base_branch FROM projects WHERE repo=?', (repo,)).fetchone()
            if (registration is None or dict(registration) != dict(project)
                    or selected is None or dict(selected) != owner
                    or checkpoint.get(repo, plan_id) != expected):
                raise execution_checkpoint_state.CheckpointRace('Worker checkpoint publication owner changed')
            result = checkpoint.record(
                repo, plan_id, expected_generation=expected.generation,
                operation=expected.operation, expected_head=head,
                target_base_oid=expected.target_base_oid or str(plan['active_sha'] or ''),
                observed_remote_oid=remote, snapshot=snapshot)
            if values:
                effects.update_project(connection, repo, **values)
            return result, dict(effects.active_execplan(connection, repo))
        if isinstance(connection, CoordinatedConnection):
            result, saved_owner = connection.run_request(apply)
        else:
            with runtime_state._transaction(connection):
                result, saved_owner = apply(connection)
        # Publish the in-memory owner only after the transaction commits.
        owner = saved_owner
        return result

    def validate_commit(parent, target):
        from loop_manager_internal.agents.scope_validation import validate_worker_commit
        validate_worker_commit(effects.git, worktree, parent, target, str(plan['path']),
                               base_branch=base, allow_common_plans=allow_common_plans)

    accepted = (current.snapshot or {}).get('accepted_work_branch')
    pending = (current.snapshot or {}).get('worker_checkpoint_pending')
    pending_integrated = False
    if pending is not None:
        valid_oid = lambda value: isinstance(value, str) and re.fullmatch(
            r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', value) is not None
        if (not isinstance(pending, dict) or pending.get('branch') != branch
                or not valid_oid(pending.get('target'))
                or (pending.get('from') is not None and not valid_oid(pending.get('from')))):
            raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN,
                                  '저장된 Worker checkpoint 게시 intent가 유효하지 않습니다.')
        for key, value in {'repo': repo, 'plan_id': plan_id, 'worktree': str(worktree),
                           'origin': project['origin'],
                           'pr_url': pull_request.get('url') if pull_request else None}.items():
            if key in pending and pending[key] != value:
                raise PublicationSafetyError('Worker checkpoint intent identity가 변경됐습니다.')
        pending_parent = effects.git(worktree, 'show', '-s', '--format=%P', pending['target']).stdout.strip()
        validate_commit(pending_parent, pending['target'])
        if ('parent' in pending and pending['parent'] != pending_parent
                or 'tree' in pending and pending['tree'] != effects.git(
                    worktree, 'rev-parse', f"{pending['target']}^{{tree}}").stdout.strip()):
            raise PublicationSafetyError('Worker checkpoint intent parent/tree가 변경됐습니다.')
        if remote_oid not in {pending.get('from'), pending['target']}:
            if remote_oid is not None and effects.git(worktree, 'merge-base', '--is-ancestor',
                    pending['target'], remote_oid, check=False).returncode == 0:
                pending_integrated = True
            else:
                raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN,
                                      '미완료 Worker checkpoint와 현재 원격 branch의 관계가 불명확합니다.')
    recorded_remote = str(plan['publication_remote_oid'] or '') or None
    if remote_oid is None:
        if accepted or recorded_remote or plan['pr_url']:
            raise RecoveryFailure(RecoveryReason.REMOTE_BRANCH_DELETED,
                                  '게시한 Worker checkpoint branch가 원격에서 삭제됐습니다.')
    elif not pending_integrated and remote_oid not in {accepted, recorded_remote,
                            *({pending.get('from'), pending['target']} if pending else set())}:
        raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN,
                              '현재 원격 Worker branch가 저장된 관리 소유권과 일치하지 않습니다.')

    def validate_pr(observed, expected_head):
        if observed is None:
            if plan['pr_url']:
                raise PublicationSafetyError('저장된 기존 PR을 관리 branch에서 찾을 수 없습니다.')
            return
        if (str(observed.get('state') or '').upper() != 'OPEN'
                or observed.get('headRefName') != branch
                or observed.get('baseRefName') != base
                or observed.get('headRefOid') != expected_head
                or (plan['pr_url'] and observed.get('url') != plan['pr_url'])):
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                  'Worker checkpoint push 직전 기존 PR identity 또는 head가 변경됐습니다.')


    validate_pr(pull_request, remote_oid)
    if pending is not None and remote_oid in {pending.get('from'), pending['target']}:
        target_is_local = effects.git(
            worktree, 'merge-base', '--is-ancestor', pending['target'], head, check=False)
        if target_is_local.returncode != 0:
            raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN,
                                  '미완료 Worker checkpoint commit이 현재 작업 이력에 없습니다.')
    if pending is not None and (remote_oid == pending['target'] or pending_integrated):
        confirmed = pending['target']
        snapshot = deepcopy(current.snapshot or {})
        snapshot.setdefault('worker_checkpoint_history', []).append(pending)
        snapshot.pop('worker_checkpoint_pending', None)
        snapshot['accepted_work_branch'] = confirmed
        current = persist(current, snapshot, head=confirmed, remote=confirmed,
            values={'publication_remote_oid': confirmed,
                    **({'pr_head_oid': confirmed} if plan['pr_url'] or pull_request else {}),
                    **({'pr_url': str(pull_request['url'])} if pull_request and not plan['pr_url'] else {})})
        if pending_integrated:
            return head, 0  # The caller integrates the later remote input separately.
    root = remote_oid or str(plan['active_sha'] or '')
    if not root:
        raise PublicationSafetyError('Worker checkpoint의 시작 commit을 확인할 수 없습니다.')
    ancestry = effects.git(worktree, 'merge-base', '--is-ancestor', root, head, check=False)
    if ancestry.returncode not in (0, 1):
        raise RuntimeError('Worker checkpoint commit ancestry를 확인할 수 없습니다.')
    if ancestry.returncode:
        from loop_manager_internal.git.managed_rebase_validation import valid_acceptance
        acceptance = (current.snapshot or {}).get('current_rebase_acceptance')
        if (pending is None and valid_acceptance(acceptance)
                and all(acceptance[key] == value for key, value in {
                    'repo': repo, 'plan_id': plan_id, 'plan_path': str(plan['path']),
                    'worktree': str(worktree), 'branch': branch,
                    'target_base_oid': str(plan['active_sha']),
                }.items())
                and acceptance['remote']['oid'] == remote_oid
                and effects.git(worktree, 'merge-base', '--is-ancestor',
                                acceptance['head'], head, check=False).returncode == 0):
            # Rewritten history belongs to the validated rebase/final publication path.
            return head, 0
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                              '원격 또는 base commit이 Worker checkpoint의 조상이 아닙니다.')
    commits = []
    if root != head:
        history = effects.git(worktree, 'rev-list', '--reverse', '--parents', f'{root}..{head}')
        parent = root
        for line in history.stdout.splitlines():
            values = line.split()
            if (len(values) != 2 or values[1] != parent
                    or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', values[0]) is None):
                raise PublicationSafetyError('Worker checkpoint 이력이 단일 부모 선형 commit이 아닙니다.')
            commits.append(values[0])
            parent = values[0]
        if parent != head:
            raise PublicationSafetyError('Worker checkpoint commit 이력이 HEAD까지 이어지지 않습니다.')

    expected = remote_oid
    for commit in commits:
        if effects.active_execplan(connection, repo) is None or dict(effects.active_execplan(connection, repo)) != owner:
            raise execution_checkpoint_state.CheckpointRace('Worker checkpoint publication owner changed')
        validate_commit(expected or str(plan['active_sha']), commit)
        plan = effects.active_execplan(connection, repo)
        if (plan is None or plan['plan_id'] != plan_id
                or plan['status'] not in {'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'}
                or str(plan['branch'] or '') != branch
                or Path(str(plan['worktree'] or '')) != worktree):
            raise PublicationSafetyError('Worker checkpoint 소유권이 push 도중 변경됐습니다.')
        if (effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip() != head
                or effects.git(repo_path, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip() != head):
            raise PublicationSafetyError('Worker checkpoint push 도중 관리 branch HEAD가 변경됐습니다.')
        current_pr = effects.pr_for_branch(repo_path, branch)
        validate_pr(current_pr, expected)
        validate_origin()
        live = manager_git_journal.live_remote_branch_oid(effects.git, repo_path, branch)
        if live != expected:
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                  'Worker checkpoint의 정확한 lease OID가 push 전에 변경됐습니다.')
        pending = {'branch': branch, 'from': expected, 'target': commit,
                   'repo': repo, 'plan_id': plan_id, 'worktree': str(worktree),
                   'origin': project['origin'], 'pr_url': current_pr.get('url') if current_pr else None,
                   'parent': effects.git(worktree, 'show', '-s', '--format=%P', commit).stdout.strip(),
                   'tree': effects.git(worktree, 'rev-parse', f'{commit}^{{tree}}').stdout.strip()}
        current = persist(current, {**(current.snapshot or {}), 'worker_checkpoint_pending': pending},
                          head=current.expected_head, remote=expected)
        pushed = effects.git(
            worktree, 'push', manager_git_journal.exact_branch_lease(branch, expected),
            'origin', f'{commit}:refs/heads/{branch}', check=False,
        )
        confirmed = manager_git_journal.live_remote_branch_oid(effects.git, repo_path, branch)
        if confirmed != commit:
            if confirmed != expected:
                raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                      'Worker checkpoint push 중 예상하지 않은 원격 commit이 나타났습니다.')
            raise RuntimeError(f'Worker checkpoint push 실패:\n{pushed.stdout[-8000:]}')
        snapshot = deepcopy(current.snapshot or {})
        snapshot.setdefault('worker_checkpoint_history', []).append(pending)
        snapshot.pop('worker_checkpoint_pending', None)
        snapshot['accepted_work_branch'] = commit
        current = persist(current, snapshot, head=commit, remote=commit,
            values={'publication_remote_oid': commit,
                    **({'pr_head_oid': commit} if plan['pr_url'] or current_pr else {}),
                    **({'pr_url': str(current_pr['url'])} if current_pr and not plan['pr_url'] else {})})
        expected = commit
    return head, len(commits)


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
    _owned_publication_plan: Callable[..., object] | None = None


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
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, str(plan['plan_id']))
    observed_head = (checkpoint.snapshot or {}).get('accepted_work_branch') or observed_head
    fresh = bool((checkpoint.snapshot or {}).get('fresh_remote_creation'))
    if fresh and not observed_head:
        observed_head = str(pull_request.get('headRefOid') or '')
    target_base = str((plan["pr_conflict_base_oid"] if recovery
                      else plan["validated_base_sha"]) or "")
    if (
        str(pull_request.get("state") or "").upper() != "OPEN"
        or str(pull_request.get("url") or "") != url
        or str(pull_request.get("headRefName") or "") != branch
        or (pull_request.get("baseRefName") is not None
            and pull_request["baseRefName"] != project["base_branch"])
    ):
        raise PublicationSafetyError(
            "게시 직전 PR URL, state, head branch 또는 head OID가 복구 "
            "시작 시 관측값과 다릅니다."
        )
    if str(pull_request.get('headRefOid') or '') != observed_head:
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '게시 직전 PR head 관측이 변경됐습니다.')
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
    if not recovery and not fresh and not (checkpoint.snapshot or {}).get('current_rebase_acceptance') and effects.git(
            worktree, 'merge-base', '--is-ancestor', observed_head, local_oid,
            check=False).returncode != 0:
        raise PublicationSafetyError('일반 PR 후보가 확인된 원격 checkpoint의 후속 commit이 아닙니다.')

    if fresh:
        effects._recorded_base_fetch(repo_path, state_dir, str(project['base_branch']), owner='project')
    else:
        effects._fetch_pr_conflict_recovery_refs(
        repo_path,
        state_dir,
        str(project["base_branch"]),
        branch,
    )
    from loop_manager_internal.git.manager_git_journal import live_remote_branch_oid
    remote_oid = live_remote_branch_oid(effects.git, repo_path, branch)
    if remote_oid != (None if fresh else observed_head):
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
            "게시 직전 원격 PR head가 복구 시작 시 관측값과 달라졌습니다."
        )
    current_pr = effects.pr_for_branch(repo_path, branch, url)
    if (current_pr is None or any(current_pr.get(key) != pull_request.get(key)
                                 for key in ('url', 'state', 'headRefName', 'baseRefName'))):
        raise PublicationSafetyError('push 직전 PR identity 또는 head가 변경됐습니다.')
    if current_pr.get('headRefOid') != observed_head:
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'push 직전 PR head가 변경됐습니다.')
    effects.validate_managed_worktree(repo_path, worktree, branch)
    if (effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip() != local_oid
            or effects.git(worktree, 'rev-parse', '--verify', f'refs/heads/{branch}').stdout.strip() != local_oid):
        raise PublicationSafetyError('PR push 직전 소유 branch 또는 관리 HEAD가 변경됐습니다.')
    if remote_oid == local_oid:
        # A validated candidate can already be the confirmed PR checkpoint.
        # Reuse its live identity after the same validation gates.
        return local_oid
    if effects._owned_publication_plan is not None:
        effects._owned_publication_plan(connection, repo_path, state_dir, repo, branch, worktree)
    pushed = effects.git(
        worktree,
        "push",
        f"--force-with-lease=refs/heads/{branch}:{'' if fresh else observed_head}",
        "origin",
        f"{local_oid}:refs/heads/{branch}",
        check=False,
    )
    if pushed.returncode:
        normalized = pushed.stdout.casefold()
        if any(
            marker in normalized
            for marker in ("stale info", "fetch first", "non-fast-forward")
        ):
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
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
    *,
    manual_stage_tokens: Mapping[str, str] | None = None,
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
            manual_stage_tokens=manual_stage_tokens,
        )
    return True
