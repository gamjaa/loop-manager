"""git base fetch service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass, replace
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.application.contracts import RemoteBranchMissingError
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.runtime.contracts import RuntimeWorktree
from loop_manager_internal.runtime.resource_lock import fetch_resource
from loop_manager_internal.runtime.runtime_scope import proven_runtime_removal
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from pathlib import Path
import json
import re
import secrets
import sqlite3


def reconcile_external_base(git, repo, base, before, current, resources, *, registered_origin=None):
    """Reobserve a selected base ref only when its remote transition is proven.

    This does not fabricate Manager receipts or alter saved Agent evidence.
    The selected branch and live remote establish a fetch-shaped observation;
    unrelated refs and missing/rewritten refs retain strict comparison.
    """
    ref, head = f'refs/remotes/origin/{base}', 'refs/remotes/origin/HEAD'
    old, new = before.refs.get(ref), current.refs.get(ref)
    if ((resources is not None and ref not in resources.refs)
            or not base or old is None or new is None or old[1] or new[1]):
        return before
    previous_head, observed_head = before.refs.get(head), current.refs.get(head)
    if previous_head != observed_head:
        if observed_head != (new[0], ref) or (previous_head is not None and previous_head != (old[0], ref)):
            return before
    if old == new and previous_head == observed_head:
        return before
    if not registered_origin:
        return before

    remote_ref = f'refs/heads/{base}'

    def retry(detail, error=None):
        failure = RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, detail)
        if error is None:
            raise failure
        raise failure from error

    def remote_oid():
        try:
            remote = git(repo, 'ls-remote', '--heads', 'origin', remote_ref, check=False)
        except (RuntimeError, OSError) as error:
            retry(f'origin/{base} 원격 ref 조회를 재시도합니다.', error)
        if remote.returncode:
            retry(f'origin/{base} 원격 ref 조회를 재시도합니다.')
        rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
        if len(rows) != 1 or rows[0][1:] != [remote_ref]:
            return None
        return rows[0][0]

    def is_ancestor(ancestor, descendant):
        try:
            result = git(repo, 'merge-base', '--is-ancestor', ancestor, descendant, check=False)
        except (RuntimeError, OSError) as error:
            retry(f'origin/{base} ancestry 관측을 재시도합니다.', error)
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        retry(f'origin/{base} ancestry 관측을 재시도합니다.')

    def symbolic_head():
        try:
            symbolic = git(repo, 'ls-remote', '--symref', 'origin', 'HEAD', check=False)
        except (RuntimeError, OSError) as error:
            retry(f'origin/HEAD 관측을 재시도합니다.', error)
        if symbolic.returncode:
            retry(f'origin/HEAD 관측을 재시도합니다.')
        fields = symbolic.stdout.split()
        if len(fields) != 5 or fields[:3] != ['ref:', remote_ref, 'HEAD'] or fields[4] != 'HEAD':
            return None
        return fields[3]

    try:
        from loop_manager_internal.git.git_adapter import github_repository
        try:
            configured = git(repo, 'remote', 'get-url', '--all', 'origin', check=False)
        except (RuntimeError, OSError) as error:
            retry(f'등록 origin 관측을 재시도합니다.', error)
        if configured.returncode:
            retry('등록 origin 관측을 재시도합니다.')
        origin_urls = [line.strip() for line in configured.stdout.splitlines() if line.strip()]
        if len(origin_urls) != 1 or github_repository(origin_urls[0]) != registered_origin:
            return before

        advertised = remote_oid()
        if advertised is None:
            return before
        if not is_ancestor(old[0], new[0]) or not is_ancestor(new[0], advertised):
            return before

        if previous_head is None and observed_head is not None:
            symbolic_oid = symbolic_head()
            if (symbolic_oid is None or not is_ancestor(new[0], symbolic_oid)
                    or not (is_ancestor(advertised, symbolic_oid)
                            or is_ancestor(symbolic_oid, advertised))):
                return before
        # The tracking resource must still match the captured observation.
        try:
            live = git(repo, 'rev-parse', '--verify', ref, check=False)
        except (RuntimeError, OSError) as error:
            retry(f'{ref} live ref 관측을 재시도합니다.', error)
        if live.returncode:
            retry(f'{ref} live ref 관측을 재시도합니다.')
        if live.stdout.strip() != new[0]:
            retry(f'{ref} snapshot이 변경되어 재관측합니다.')

        remote_after = remote_oid()
        if (remote_after is None or not is_ancestor(new[0], remote_after)
                or not is_ancestor(advertised, remote_after)):
            return before
        if previous_head is None and observed_head is not None:
            symbolic_after = symbolic_head()
            if (symbolic_after is None or not is_ancestor(new[0], symbolic_after)
                    or not (is_ancestor(remote_after, symbolic_after)
                            or is_ancestor(symbolic_after, remote_after))):
                return before
    except RecoveryFailure:
        raise
    except (RuntimeError, OSError):
        retry(f'origin/{base} 원격 상태 관측을 재시도합니다.')
    refs = {**before.refs, ref: new}
    if previous_head != observed_head:
        refs[head] = observed_head
    return replace(before, refs=refs)


@dataclass(frozen=True)
class RecordedBaseFetchEffects:
    """Named effects for _recorded_base_fetch; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _tracking_ref_oid: Callable[..., object]
    git: Callable[..., object]


@fetch_resource
def _recorded_base_fetch(
    effects: RecordedBaseFetchEffects, repo: Path, state_dir: Path, base: str, *, owner: str,
) -> str:
    """Fetch one base object and move its tracking ref with an owned CAS."""
    refname = f"refs/remotes/origin/{base}"
    remote_ref = f"refs/heads/{base}"
    remote = effects.git(repo, "ls-remote", "--heads", "origin", remote_ref, check=False)
    rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if remote.returncode == 0 and not rows:
        raise RemoteBranchMissingError(f"origin/{base} 원격 branch를 찾을 수 없습니다.")
    if remote.returncode or len(rows) != 1 or rows[0][1:] != [remote_ref]:
        raise RuntimeError(f"origin/{base}의 원격 OID를 확정할 수 없습니다.")
    selected = rows[0][0]
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", selected) is None:
        raise RuntimeError(f"origin/{base}의 원격 OID가 유효하지 않습니다.")
    before = _fetch_ref_state(effects, repo, refname)

    def receive() -> None:
        effects.git(repo, "fetch", "--no-tags", "--refmap=", "origin", remote_ref)
        fetched = effects.git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
        if fetched != selected:
            raise RuntimeError(f"origin/{base}가 fetch 중 변경됐습니다.")

    _record_fetch_ref(effects, repo, state_dir, base, selected, before, owner, receive)
    return selected


def _ensure_direct_fetch_ref(git, repo, refname):
    """Reject aliases even when they resolve to the expected OID or are dangling."""
    symbolic = git(repo, "symbolic-ref", "--quiet", "--no-recurse", refname, check=False)
    if symbolic.returncode != 1:
        raise manager_git_journal.MutationEvidenceError(
            f"fetch 대상의 직접 ref 형상을 확정할 수 없습니다: {refname}: {symbolic.stdout.strip()}")


def _fetch_ref_state(effects, repo, refname):
    """Keep version 1 OID-only receipts; observe their raw shape at every use."""
    _ensure_direct_fetch_ref(effects.git, repo, refname)
    return {"oid": effects._tracking_ref_oid(repo, refname)}


def _update_fetch_ref(git, repo, refname, selected, old, token):
    # no-deref pins the lock/write to the journal target; prepare keeps that
    # lock while we reject aliases, including same-OID replacements.
    git(repo, "update-ref", "--create-reflog", "-m",
        f"loop-manager-base-fetch:{token}", "--no-deref", "--stdin",
        input=f"start\nupdate {refname} {selected} {old or '0' * len(selected)}\nprepare\n",
        before_commit=lambda: _ensure_direct_fetch_ref(git, repo, refname))


def _record_fetch_ref(effects, repo, state_dir, base, selected, before, owner, receive):
    """Keep the existing per-ref journal format for single and bulk receipts."""
    refname = f"refs/remotes/origin/{base}"

    def apply(token: str) -> None:
        receive()
        _update_fetch_ref(effects.git, repo, refname, selected, before["oid"], token)

    def marker(intent: Mapping[str, object]) -> bool:
        _ensure_direct_fetch_ref(effects.git, repo, refname)
        token = intent["id"]
        result = effects.git(repo, "reflog", "show", "-1", "--format=%H%x00%gs", refname,
                     check=False)
        return (
            result.returncode == 0
            and f"{intent['expected']['oid']}\x00loop-manager-base-fetch:{token}"
            in result.stdout.splitlines()
        )

    manager_git_journal.execute(
        state_dir, owner=owner, kind=("bootstrap_fetch" if owner == "bootstrap"
                                      else "project_fetch"),
        repo=str(effects._canonical_path(repo)), target=refname,
        before=before, expected={"oid": selected},
        observe=lambda: _fetch_ref_state(effects, repo, refname),
        apply=apply, marker=marker,
    )


def _remote_branch_oids(git, repo: Path) -> dict[str, str]:
    result = git(repo, "ls-remote", "--heads", "origin")
    branches: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if (len(fields) != 2 or not fields[1].startswith("refs/heads/")
                or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", fields[0]) is None):
            raise RuntimeError("project fetch 원격 branch 목록이 유효하지 않습니다.")
        branch = fields[1].removeprefix("refs/heads/")
        if (not branch or branch in branches or branch.endswith(("/", "."))
                or any(part.startswith(".") or part.endswith(".lock") or not part for part in branch.split("/"))
                or any(token in branch for token in ("..", "@{", "~", "^", ":", "?", "*", "[", "\\"))
                or any(ord(char) < 32 or ord(char) == 127 for char in branch)):
            raise RuntimeError("project fetch 원격 branch가 유효하지 않거나 중복됩니다.")
        branches[branch] = fields[0]
    return branches


def _fetched_branch_oids(git, repo: Path) -> dict[str, str]:
    path = Path(git(repo, "rev-parse", "--git-path", "FETCH_HEAD").stdout.strip())
    if not path.is_absolute():
        path = repo / path
    branches: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"([0-9a-fA-F]{40}|[0-9a-fA-F]{64})\t(?:not-for-merge)?\tbranch '([^\s]+)' of .+", line,
        )
        if match is None or match[2] in branches:
            raise RuntimeError("project fetch branch별 수신 결과가 유효하지 않습니다.")
        branches[match[2]] = match[1]
    return branches


@fetch_resource
def _recorded_project_fetch(
    effects: RecordedBaseFetchEffects, repo: Path, state_dir: Path, base: str,
) -> None:
    """Receive branch objects together, then publish individually owned refs."""
    branches = _remote_branch_oids(effects.git, repo)
    for attempt in range(2):
        if base not in branches:
            raise RemoteBranchMissingError(f"origin/{base} 원격 branch를 찾을 수 없습니다.")
        before = {branch: _fetch_ref_state(effects, repo, f"refs/remotes/origin/{branch}")
                  for branch in branches}
        changed = {branch: oid for branch, oid in branches.items() if before[branch]["oid"] != oid}
        if changed:
            result = effects.git(
                repo, "fetch", "--no-tags", "--no-prune", "--no-prune-tags", "--refmap=",
                "--recurse-submodules=no", "--stdin", "origin", check=False,
                input="".join(f"refs/heads/{branch}\n" for branch in sorted(changed)),
            )
            received = _fetched_branch_oids(effects.git, repo) if not result.returncode else None
            if (received is not None and received.keys() == changed.keys()
                    and received.get(base, branches[base]) == branches[base]):
                # 다른 작업 branch는 실제 수신 OID를 기록하고 선택한 base만 고정한다.
                changed = received
                branches = {**branches, **received}
            if result.returncode or received != changed:
                if attempt == 0:
                    refreshed = _remote_branch_oids(effects.git, repo)
                    # Only a new remote observation can explain a failed receive.
                    if refreshed != branches:
                        branches = refreshed
                        continue
                reason = result.stdout[-2000:] if result.returncode else "branch별 원격 OID 관측 경쟁"
                raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE if not result.returncode
                                      else RecoveryReason.FETCH_FAILED, "project fetch 미완료: " + reason)
            for oid in set(changed.values()):
                effects.git(repo, "cat-file", "-e", f"{oid}^{{commit}}")
        for branch, oid in sorted(branches.items()):
            # Equal refs still enter the journal to finish interrupted receipts.
            _record_fetch_ref(effects, repo, state_dir, branch, oid, before[branch], "project", lambda: None)
        return


@dataclass(frozen=True)
class FetchBootstrapRevisionEffects:
    """Named effects for fetch_bootstrap_revision; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def fetch_bootstrap_revision(effects: FetchBootstrapRevisionEffects, source: Path, state_dir: Path) -> tuple[str, str]:
    return effects._bootstrap_service().fetch_bootstrap_revision(source, state_dir)


@dataclass(frozen=True)
class ValidateBootstrapRevisionEffects:
    """Named effects for validate_bootstrap_revision; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def validate_bootstrap_revision(effects: ValidateBootstrapRevisionEffects, source: Path, base: str, sha: str) -> None:
    return effects._bootstrap_service().validate_bootstrap_revision(source, base, sha)


@dataclass(frozen=True)
class PrepareBootstrapRuntimeEffects:
    """Named effects for prepare_bootstrap_runtime; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def prepare_bootstrap_runtime(
    effects: PrepareBootstrapRuntimeEffects, source: Path, state_dir: Path, mutation_id: str,
    *, concurrent: bool = False,
) -> RuntimeWorktree:
    return effects._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, mutation_id, concurrent=concurrent)


@dataclass(frozen=True)
class RecordManagerBaseFetchIntentEffects:
    """Named effects for _record_manager_base_fetch_intent; no entry-point/module access."""
    _ensure_cleanup_pending_schema: Callable[..., object]
    _manager_base_fetch_applied: Callable[..., object]
    git: Callable[..., object]


def _record_manager_base_fetch_intent(
    effects: RecordManagerBaseFetchIntentEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    base: str,
    before: GitSnapshot,
) -> int:
    """Persist the exact base ref we intend to fetch before changing Git refs."""
    refname = f"refs/remotes/origin/{base}"
    remote = effects.git(
        repo_path, "ls-remote", "--heads", "origin", f"refs/heads/{base}",
        check=False,
    )
    rows = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if remote.returncode or len(rows) != 1 or rows[0][1:] != [f"refs/heads/{base}"]:
        raise RuntimeError("Manager base fetch의 원격 OID를 확정할 수 없습니다.")
    target = rows[0][0]
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", target) is None:
        raise RuntimeError("Manager base fetch 원격 OID가 유효하지 않습니다.")
    effects._ensure_cleanup_pending_schema(connection)
    existing = connection.execute(
        "SELECT id, before_ref, target_oid, action_token FROM manager_base_fetches "
        "WHERE repo = ? AND plan_id = ? AND refname = ? AND phase = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (repo, plan_id, refname),
    ).fetchone()
    before_ref = json.dumps(before.refs.get(refname))
    if existing is not None:
        if existing["before_ref"] == before_ref and existing["target_oid"] == target:
            if not existing["action_token"]:
                raise RuntimeError("이전 Manager base fetch intent의 소유 표식이 없습니다.")
            return int(existing["id"])
        try:
            saved = json.loads(str(existing["before_ref"]))
            cursor = tuple(saved) if saved is not None else None
            successors = connection.execute(
                "SELECT * FROM manager_base_fetches WHERE repo = ? AND plan_id = ? "
                "AND refname = ? AND id >= ? ORDER BY id",
                (repo, plan_id, refname, int(existing["id"])),
            ).fetchall()
            for prior in successors:
                previous = json.loads(str(prior["before_ref"]))
                previous = tuple(previous) if previous is not None else None
                if previous != cursor or (
                    prior["phase"] != "complete"
                    and not effects._manager_base_fetch_applied(repo_path, prior, successors)
                ):
                    raise RuntimeError("이전 Manager base fetch 체인을 증명할 수 없습니다.")
                cursor = (str(prior["target_oid"]), "")
        except (TypeError, ValueError) as error:
            raise RuntimeError("이전 Manager base fetch intent가 유효하지 않습니다.") from error
        if cursor != before.refs.get(refname):
            raise RuntimeError("이전 Manager base fetch intent를 안전하게 재개할 수 없습니다.")
    with connection:
        cursor = connection.execute(
            "INSERT INTO manager_base_fetches "
            "(repo, plan_id, refname, before_ref, target_oid, action_token, phase) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (repo, plan_id, refname, before_ref,
             target, secrets.token_hex(16)),
        )
    return int(cursor.lastrowid)


@dataclass(frozen=True)
class ManagerBaseFetchAppliedEffects:
    """Named effects for _manager_base_fetch_applied; no entry-point/module access."""
    _tracking_ref_oid: Callable[..., object]
    git: Callable[..., object]


def _manager_base_fetch_applied(
    effects: ManagerBaseFetchAppliedEffects, repo_path: Path, row: sqlite3.Row,
    successors: list[sqlite3.Row] | None = None,
) -> bool:
    """Require this action at the tip or an uninterrupted owned reflog suffix."""
    token = str(row["action_token"] or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return False
    reflog = effects.git(
        repo_path, "reflog", "show", "--format=%H%x00%gs",
        str(row["refname"]), check=False,
    )
    if reflog.returncode:
        return False
    lines = reflog.stdout.splitlines()
    action = f"{row['target_oid']}\x00loop-manager-base-fetch:{token}"
    if lines.count(action) != 1:
        return False
    position = lines.index(action)
    cursor = str(row["target_oid"])
    remaining = [item for item in (successors or [])
                 if item["refname"] == row["refname"] and item["id"] > row["id"]]
    for line in reversed(lines[:position]):
        matched = None
        for candidate in remaining:
            try:
                previous = json.loads(str(candidate["before_ref"]))
            except (TypeError, ValueError):
                return False
            if (previous == [cursor, ""] and line ==
                    f"{candidate['target_oid']}\x00loop-manager-base-fetch:{candidate['action_token']}"):
                matched = candidate
                break
        if matched is None:
            return False
        cursor = str(matched["target_oid"])
        remaining = [item for item in remaining if item["id"] > matched["id"]]
    return effects._tracking_ref_oid(repo_path, str(row["refname"])) == cursor


@dataclass(frozen=True)
class PerformManagerBaseFetchEffects:
    """Named effects for _perform_manager_base_fetch; no entry-point/module access."""
    git: Callable[..., object]


def _perform_manager_base_fetch(
    effects: PerformManagerBaseFetchEffects, connection: sqlite3.Connection,
    repo_path: Path,
    intent_id: int,
    base: str,
) -> str:
    """Fetch the object, then update only the intended tracking ref with CAS."""
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ? AND phase = 'pending'",
        (intent_id,),
    ).fetchone()
    if row is None or row["refname"] != f"refs/remotes/origin/{base}":
        raise RuntimeError("Manager base fetch intent가 현재 base와 다릅니다.")
    token = str(row["action_token"] or "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise RuntimeError("Manager base fetch action token이 유효하지 않습니다.")
    try:
        previous = json.loads(str(row["before_ref"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.") from error
    if previous is not None and (
        not isinstance(previous, list) or len(previous) != 2
        or previous[1] != ""
        or not isinstance(previous[0], str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", previous[0])
    ):
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.")
    target = str(row["target_oid"])
    effects.git(repo_path, "fetch", "--no-tags", "--refmap=", "origin",
        f"refs/heads/{base}")
    fetched = effects.git(repo_path, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
    if fetched != target:
        raise RuntimeError("Manager base fetch 원격 OID가 저장된 intent와 다릅니다.")
    _update_fetch_ref(effects.git, repo_path, str(row["refname"]), target,
                      previous[0] if previous is not None else None, token)
    return base


@dataclass(frozen=True)
class FinishManagerBaseFetchEffects:
    """Named effects for _finish_manager_base_fetch; no entry-point/module access."""
    _manager_base_fetch_applied: Callable[..., object]
    git: Callable[..., object]


def _finish_manager_base_fetch(
    effects: FinishManagerBaseFetchEffects, connection: sqlite3.Connection,
    repo_path: Path,
    intent_id: int,
) -> None:
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ?",
        (intent_id,),
    ).fetchone()
    if row is not None:
        _ensure_direct_fetch_ref(effects.git, repo_path, str(row["refname"]))
    if row is None or effects.git(repo_path, "rev-parse", str(row["refname"])).stdout.strip() != row["target_oid"]:
        raise RuntimeError("Manager base fetch 결과가 저장된 원격 OID와 다릅니다.")
    try:
        previous = json.loads(str(row["before_ref"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Manager base fetch 이전 ref가 유효하지 않습니다.") from error
    if previous != [row["target_oid"], ""] and not effects._manager_base_fetch_applied(repo_path, row):
        raise RuntimeError("Manager base fetch의 ref 갱신 증거가 없습니다.")
    with connection:
        connection.execute(
            "UPDATE manager_base_fetches SET phase = 'complete' WHERE id = ?",
            (intent_id,),
        )


@dataclass(frozen=True)
class JournaledManagerBaseFetchEffects:
    """Named effects for _journaled_manager_base_fetch; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _finish_manager_base_fetch: Callable[..., object]
    _manager_base_fetch_applied: Callable[..., object]
    _perform_manager_base_fetch: Callable[..., object]
    _tracking_ref_oid: Callable[..., object]
    git: Callable[..., object]


@fetch_resource
def _journaled_manager_base_fetch(
    effects: JournaledManagerBaseFetchEffects, connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    intent_id: int, base: str, plan_id: str,
) -> str:
    row = connection.execute(
        "SELECT * FROM manager_base_fetches WHERE id = ? AND phase = 'pending'",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Manager base fetch intent를 찾을 수 없습니다.")
    previous = json.loads(str(row["before_ref"]))
    before_oid = previous[0] if previous is not None else None
    refname = str(row["refname"])
    target = str(row["target_oid"])

    def marker(_intent: Mapping[str, object]) -> bool:
        _ensure_direct_fetch_ref(effects.git, repo_path, refname)
        return before_oid == target or effects._manager_base_fetch_applied(repo_path, row)

    manager_git_journal.recover_active(
        state_dir, repo=str(effects._canonical_path(repo_path)),
        kind="project_fetch", target=refname,
        observe=lambda: _fetch_ref_state(effects, repo_path, refname),
        marker=marker,
    )
    current = _fetch_ref_state(effects, repo_path, refname)["oid"]
    if current != target:
        manager_git_journal.execute(
            state_dir, owner=f"guidance:{plan_id}", kind="project_fetch",
            repo=str(effects._canonical_path(repo_path)), target=refname,
            before={"oid": before_oid}, expected={"oid": target},
            observe=lambda: _fetch_ref_state(effects, repo_path, refname),
            apply=lambda _token: effects._perform_manager_base_fetch(
                connection, repo_path, intent_id, base,
            ),
            marker=marker,
        )
    elif not marker({}):
        raise RuntimeError("Manager base fetch의 소유 ref 갱신 증거가 없습니다.")
    effects._finish_manager_base_fetch(connection, repo_path, intent_id)
    return base


@dataclass(frozen=True)
class ProvenManagerBaseRefsEffects:
    """Named effects for _proven_manager_base_refs; no entry-point/module access."""
    _ensure_cleanup_pending_schema: Callable[..., object]
    _manager_base_fetch_applied: Callable[..., object]


def _proven_manager_base_refs(
    effects: ProvenManagerBaseRefsEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    before: GitSnapshot,
    current: GitSnapshot,
) -> frozenset[str]:
    """Follow completed fetches or an interrupted fetch with its Git action."""
    effects._ensure_cleanup_pending_schema(connection)
    proven: set[str] = set()
    rows = connection.execute(
        "SELECT id, refname, before_ref, target_oid, action_token, phase "
        "FROM manager_base_fetches "
        "WHERE repo = ? AND plan_id = ? AND phase IN ('pending', 'complete') "
        "ORDER BY id",
        (repo, plan_id),
    ).fetchall()
    for refname in {str(row["refname"]) for row in rows}:
        if before.refs.get(refname) == current.refs.get(refname):
            continue
        cursor = before.refs.get(refname)
        for row in rows:
            if row["refname"] != refname:
                continue
            try:
                previous = json.loads(str(row["before_ref"]))
            except (TypeError, ValueError):
                continue
            previous = tuple(previous) if previous is not None else None
            if previous == cursor and effects._manager_base_fetch_applied(repo_path, row, rows):
                cursor = (str(row["target_oid"]), "")
        if cursor is not None and cursor == current.refs.get(refname):
            proven.add(refname)
    return frozenset(proven)


@dataclass(frozen=True)
class ReconciledManagerScopeMetadataEffects:
    """Named effects for _reconciled_manager_scope_metadata; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _ensure_cleanup_pending_schema: Callable[..., object]
    _proven_manager_base_refs: Callable[..., object]
    _proven_merged_cleanup_paths: Callable[..., object]
    git: Callable[..., object]


def _reconciled_manager_scope_metadata(effects: ReconciledManagerScopeMetadataEffects, connection, repo_path, state_dir, repo,
                                      plan_id, before, current, resources=None):
    """Use fetch receipts/CAS effects and exact Merge cleanup, never ref drift."""
    saved = ExecutionCheckpointRepository(connection).get(repo, plan_id or '').snapshot or {}
    baseline = saved.get('manager_metadata_agent_baseline')
    if isinstance(baseline, dict):
        repository = RuntimeStateRepository(connection)
        owner = (repository.scope_owner(repo, plan_id)
                 if plan_id and repository.scope_catalog_available() else None)
        if owner is None:
            owner = repository.scope_owner(repo)
        if owner is not None and owner['agent_snapshot']:
            original = GitSnapshot.deserialize(str(owner['agent_snapshot']))
            before = agent_scope_decision.manager_comparison_baseline(
                original, before, saved, str(owner['agent_role'] or ''))
    removed = frozenset(path for path, registration in before.worktrees.items()
        if path not in current.worktrees and proven_runtime_removal(
            state_dir, Path(repo), Path(path), registration))
    before = agent_scope_decision.reconcile_manager_metadata(
        before, current, refs=frozenset(), worktrees=removed)
    project = connection.execute('SELECT base_branch, origin FROM projects WHERE repo = ?', (repo,)).fetchone()
    base_branch = str(project['base_branch']) if project is not None else None
    if project is not None:
        before = reconcile_external_base(
            effects.git, repo_path, base_branch, before, current, resources,
            registered_origin=str(project['origin'] or ''),
        )
    scope_before, scope_current = agent_scope_decision.scoped_metadata(
        before, current, resources,
        default_worktree=str(effects._canonical_path(repo_path)),
        base_branch=base_branch,
    )
    effects._ensure_cleanup_pending_schema(connection)
    proven_refs = effects._proven_manager_base_refs(
        connection, repo_path, repo, plan_id or '', scope_before, scope_current)
    sqlite_fetches = []
    for row in connection.execute(
            "SELECT refname, before_ref, target_oid, action_token FROM manager_base_fetches "
            "WHERE repo = ? AND plan_id = ? AND phase IN ('pending', 'complete')",
            (repo, plan_id or '')):
        try:
            previous = json.loads(str(row['before_ref']))
        except (TypeError, ValueError):
            continue
        if (not re.fullmatch(r'[0-9a-f]{32}', str(row['action_token'] or ''))
                or not str(row['refname']).startswith('refs/remotes/origin/')
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', str(row['target_oid']))
                or previous is not None and (not isinstance(previous, list)
                    or len(previous) != 2 or previous[1] != ''
                    or not isinstance(previous[0], str)
                    or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', previous[0]))):
            continue
        sqlite_fetches.append(dict(version=1, id=row['action_token'],
            owner=f"guidance:{plan_id}", repo=str(effects._canonical_path(repo_path)),
            kind='project_fetch', target=row['refname'],
            before={'oid': previous[0] if previous is not None else None},
            expected={'oid': row['target_oid']}))
    live_refs = {key: value[0] for key, value in scope_current.refs.items() if not value[1]}
    def marker(intent):
        if intent.get('kind') not in {'bootstrap_fetch', 'project_fetch'}:
            return False
        ref = intent['target']
        output = effects.git(repo_path, 'reflog', 'show', '--format=%H%x00%gs', ref, check=False)
        return bool(not output.returncode and manager_git_journal.fetch_reflog_matches(
            state_dir, str(effects._canonical_path(repo_path)), intent,
            output.stdout.splitlines(), live_refs.get(ref), additional_fetches=sqlite_fetches))
    evidence = manager_git_journal.verified_candidates(
        state_dir, str(effects._canonical_path(repo_path)),
        baseline_refs={key: value[0] for key, value in scope_before.refs.items() if not value[1]},
        baseline_worktrees={}, live_refs=live_refs, live_worktrees={}, marker=marker,
        include_pending_fetch=True, additional_fetches=sqlite_fetches,
        fetch_refs=frozenset(key for key in scope_before.refs.keys() | scope_current.refs.keys()
                             if key.startswith('refs/remotes/origin/')
                             and scope_before.refs.get(key) != scope_current.refs.get(key)
                             and key not in proven_refs))
    proven_refs |= frozenset(key for key, value in evidence['refs'].items()
                            if current.refs.get(key) == (value, '')
                            and (before.refs.get(key) is None or not before.refs[key][1]))
    return agent_scope_decision.reconcile_manager_metadata(
        before, current, refs=proven_refs,
        worktrees=effects._proven_merged_cleanup_paths(connection, repo, scope_before, scope_current),
        resources=resources)
