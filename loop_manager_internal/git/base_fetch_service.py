"""git base fetch service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.application.contracts import RemoteBranchMissingError
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.runtime.contracts import BootstrapRefreshHandoff
from loop_manager_internal.runtime.contracts import RuntimeWorktree
from pathlib import Path
import json
import re
import secrets
import sqlite3


@dataclass(frozen=True)
class RecordedBaseFetchEffects:
    """Named effects for _recorded_base_fetch; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _tracking_ref_oid: Callable[..., object]
    git: Callable[..., object]


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
    before = {"oid": effects._tracking_ref_oid(repo, refname)}

    def apply(token: str) -> None:
        effects.git(repo, "fetch", "--no-tags", "--refmap=", "origin", remote_ref)
        fetched = effects.git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
        if fetched != selected:
            raise RuntimeError(f"origin/{base}가 fetch 중 변경됐습니다.")
        effects.git(repo, "update-ref", "--create-reflog", "-m",
            f"loop-manager-base-fetch:{token}", refname, selected,
            before["oid"] or "0" * len(selected))

    def marker(intent: Mapping[str, object]) -> bool:
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
        observe=lambda: {"oid": effects._tracking_ref_oid(repo, refname)},
        apply=apply, marker=marker,
    )
    return selected


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
) -> RuntimeWorktree | BootstrapRefreshHandoff:
    return effects._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, mutation_id)


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
    effects.git(
        repo_path, "update-ref", "--create-reflog", "-m",
        f"loop-manager-base-fetch:{token}", str(row["refname"]), target,
        previous[0] if previous is not None else "0" * len(target),
    )
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
        return before_oid == target or effects._manager_base_fetch_applied(repo_path, row)

    manager_git_journal.recover_active(
        state_dir, repo=str(effects._canonical_path(repo_path)),
        kind="project_fetch", target=refname,
        observe=lambda: {"oid": effects._tracking_ref_oid(repo_path, refname)},
        marker=marker,
    )
    current = effects._tracking_ref_oid(repo_path, refname)
    if current != target:
        manager_git_journal.execute(
            state_dir, owner=f"guidance:{plan_id}", kind="project_fetch",
            repo=str(effects._canonical_path(repo_path)), target=refname,
            before={"oid": before_oid}, expected={"oid": target},
            observe=lambda: {"oid": effects._tracking_ref_oid(repo_path, refname)},
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
    scope_before, scope_current = agent_scope_decision.scoped_metadata(before, current, resources)
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
