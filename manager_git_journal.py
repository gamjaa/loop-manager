"""Durable, operation-owned evidence for Manager Git mutations.

The caller holds the process lock and supplies Git observations and an action
marker (a reflog message or a worktree lock reason).  This module never infers
ownership from an OID or elapsed time alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any
from execution_checkpoint import controlling_git_operation, rebase_preserves_worktree_files
import managed_rebase_evidence as replay_evidence


class MutationEvidenceError(RuntimeError):
    """An intended Git transition cannot be attributed to the Manager."""


def rebase_completion_matches(git: Callable[..., Any], repo: Path, *,
                              branch: str, head: str, identity: str,
                              target_base_oid: str) -> bool:
    """Bind completion to the latest branch update, never an older marker."""
    reflog = git(repo, "reflog", "show", "-1", "--format=%H%x00%gs",
                 f"refs/heads/{branch}", check=False)
    return reflog.returncode == 0 and reflog.stdout.rstrip("\n") == (
        f"{head}\0loop-manager-rebase:{identity}"
        f" (finish): refs/heads/{branch} onto {target_base_oid}"
    )


def live_remote_branch_oid(git: Callable[..., Any], repo: Path,
                           branch: str) -> str | None:
    """Observe the publication ref on origin, independent of fetched refs."""
    ref = f'refs/heads/{branch}'
    result = git(repo, 'ls-remote', '--heads', 'origin', ref, check=False)
    if result.returncode:
        raise MutationEvidenceError('rebase remote: live branch query failed')
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if (len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref
            or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', lines[0][0]) is None):
        raise MutationEvidenceError('rebase remote: live branch response is ambiguous')
    return lines[0][0]


def rebase_conflict_matches(git: Callable[..., Any], worktree: Path, *,
                            head: str, identity: str) -> bool:
    """Require this operation at the selected worktree's current HEAD reflog tip."""
    result = git(worktree, "reflog", "show", "-1", "--format=%H%x00%gs",
                 "HEAD", check=False)
    prefix = f"{head}\0loop-manager-rebase:{identity}"
    return result.returncode == 0 and any(
        result.stdout.rstrip("\n").startswith(f"{prefix} ({action}): ")
        for action in ("start", "pick", "continue", "skip")
    )


def record_rebase_invocation(
    state_dir: Path, repo: str, target: str, identity: str, *,
    returncode: int,
) -> None:
    """Seal the Manager's own command outcome for an unchanged rebase."""
    slot = _slot(state_dir, repo, 'managed_rebase', target)
    active = _read(slot / 'active.json')
    if active.get('id') != identity:
        raise MutationEvidenceError('rebase invocation: active identity mismatch')
    receipt = {'id': identity, 'run_id': active['expected'].get('run_id'),
               'returncode': returncode}
    path = slot / f'{identity}.invocation.json'
    if path.exists():
        if _read(path) != receipt:
            raise MutationEvidenceError('rebase invocation: outcome changed')
    else:
        _write_new(path, receipt)


def successful_rebase_invocation(
    state_dir: Path, repo: str, target: str, identity: str,
) -> bool:
    slot = _slot(state_dir, repo, 'managed_rebase', target)
    path = slot / f'{identity}.invocation.json'
    if not path.is_file():
        return False
    receipt = _read(path)
    intent = _read(slot / f'{identity}.intent.json')
    return receipt == {'id': identity,
                       'run_id': intent['expected'].get('run_id'),
                       'returncode': 0}


def rebase_affected_paths(git: Callable[..., Any], repo: Path,
                          original: str, target: str) -> list[str]:
    """Record the paths Git may touch before starting a managed replay."""
    base = git(repo, "merge-base", original, target).stdout.strip()
    if not base:
        # No replay range can be established. A later conflict with changed
        # files cannot be certified against this empty allowance.
        return []
    paths = set(filter(None, git(
        repo, "diff", "--no-renames", "--name-only", "-z", original, target,
    ).stdout.split("\0")))
    commits = git(repo, "rev-list", f"{base}..{original}").stdout.splitlines()
    for commit in commits:
        paths.update(filter(None, git(
            repo, "diff-tree", "--root", "-m", "--no-renames", "--no-commit-id",
            "--name-only", "-r", "-z", commit,
        ).stdout.split("\0")))
    return sorted(paths)


def rebase_replay_proof(
    git: Callable[..., Any], state_dir: Path, intent: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Compare one frozen source sequence with the actual replay and receipts."""
    expected = intent['expected']
    if expected.get('evidence_version') != replay_evidence.EVIDENCE_VERSION:
        raise MutationEvidenceError('rebase replay evidence version is unavailable')
    sources = expected.get('source_commits')
    run_id = expected.get('run_id')
    generation = expected.get('checkpoint_generation')
    if (not isinstance(sources, list) or not isinstance(run_id, str)
            or not run_id or not isinstance(expected.get('source_base'), str)
            or not isinstance(generation, int)):
        raise MutationEvidenceError('rebase start: source sequence is incomplete')
    repo = Path(intent['repo'])
    actual_base, actual_sources = replay_evidence.source_commits(
        git, repo, expected['before_head'], expected['target_base_oid'],
    )
    if actual_base != expected['source_base'] or actual_sources != sources:
        raise MutationEvidenceError('rebase start: frozen source sequence mismatch')
    slot = _slot(state_dir, intent['repo'], 'managed_rebase', intent['target'])
    receipts = []
    for result_path in slot.glob('*.result.json'):
        result = _read(result_path)
        if result.get('status') != 'complete':
            continue
        saved = _read(slot / f"{result.get('id')}.intent.json")
        if (saved.get('expected', {}).get('run_id') == run_id
                and saved.get('id') != intent['id']):
            saved_generation = saved['expected'].get('checkpoint_generation')
            if not isinstance(saved_generation, int):
                raise MutationEvidenceError('rebase replay: receipt generation missing')
            if saved_generation >= generation:
                continue
            if (saved.get('repo') != intent['repo'] or saved.get('target') != intent['target']
                    or saved.get('owner') != intent['owner']
                    or saved['expected'].get('source_commits') != sources
                    or saved['expected'].get('target_base_oid') != expected['target_base_oid']):
                raise MutationEvidenceError('rebase replay: receipt identity mismatch')
            if result.get('id') != saved.get('id') or not isinstance(result.get('candidates'), dict):
                raise MutationEvidenceError('rebase replay: sealed receipt mismatch')
            receipts.append(saved)
    receipts.append(intent)
    if len([saved for saved in receipts if saved['before'].get('metadata') is None
            and saved['before'].get('head') == expected['before_head']
            and not saved['expected'].get('action')]) != 1:
        raise MutationEvidenceError('rebase start: unique initial intent unavailable')
    corrections: dict[str, dict[str, str]] = {}
    skips: dict[str, dict[str, str]] = {}
    for saved in receipts:
        item = saved['expected']
        source = item.get('source_commit')
        action = item.get('action')
        if action not in {'continue', 'skip'}:
            continue
        if source not in sources or item.get('run_id') != run_id:
            raise MutationEvidenceError('rebase resolution: source identity mismatch')
        parent = saved['before']['head']
        if action == 'continue':
            tree = item.get('staged_tree_oid')
            if (not tree or source in corrections or source in skips
                    or item.get('staged_index') != saved['before'].get('index')
                    or not item.get('correction_turn')
                    or not replay_evidence.stage_matches(
                        before_index=item.get('stage_before_index'),
                        staged_index=item.get('staged_index'),
                        allowed_paths=list((item.get('accepted_files') or {}).keys()),
                        accepted_files=item.get('accepted_files') or {},
                        staged_dirty=saved['before'].get('dirty') or {},
                        accepted_modes=item.get('accepted_modes'),
                    )):
                raise MutationEvidenceError('rebase resolution: stage or turn mismatch')
            corrections[source] = {
                'source': source, 'parent': parent, 'tree': tree,
                'intent_id': saved['id'], 'turn_id': item['correction_turn'],
            }
        else:
            if (source in skips or source in corrections
                    or item.get('skip_reason') != 'worker-approved'
                    or not item.get('correction_turn')
                    or item.get('staged_index') != saved['before'].get('index')
                    or not isinstance(item.get('accepted_files'), dict)
                    or not replay_evidence.skip_files_match(
                        item.get('accepted_files'), item.get('skip_observed_files'),
                        saved['before'].get('dirty'),
                    )
                    or not replay_evidence.stage_modes_match(
                        item.get('accepted_files') or {}, item.get('accepted_modes'),
                        item.get('staged_index'),
                    )
                    or not replay_evidence.stage_index_matches(
                        before_index=item.get('stage_before_index'),
                        staged_index=item.get('staged_index'),
                        allowed_paths=list(item.get('accepted_files') or {}),
                    )
                    or not saved['before'].get('index_clean')
                    or saved['before'].get('unmerged')):
                raise MutationEvidenceError('rebase skip: source receipt mismatch')
            skips[source] = {
                'source': source, 'parent': parent, 'reason': item['skip_reason'],
                'intent_id': saved['id'],
            }
    target = expected['target_base_oid']
    head = observed['head']
    stop_source = observed.get('rebase_head') if observed.get('metadata') is not None else None
    if observed.get('metadata') is not None and stop_source not in sources:
        raise MutationEvidenceError('rebase replay: current conflict source missing')
    replayed = [replay_evidence.commit_fact(git, repo, oid) for oid in
                replay_evidence.replay_commits(git, repo, target, head)]
    source_facts = {oid: replay_evidence.commit_fact(git, repo, oid) for oid in sources}
    try:
        proof = replay_evidence.verify_replay(
            sources=sources, replayed=replayed, target=target, head=head,
            corrections=corrections, skips=skips, source_facts=source_facts,
            same_patch=lambda original, actual: replay_evidence.patch_equal(
                git, repo, original, actual,
            ), allow_prefix=observed.get('metadata') is not None,
            stop_source=stop_source,
        )
    except replay_evidence.ReplayEvidenceError as error:
        raise MutationEvidenceError(str(error)) from error
    return proof


def legacy_rebase_replay_proof(
    git: Callable[..., Any], state_dir: Path, intent: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Recover old staged-tree receipts only when their replay is unique."""
    expected = intent['expected']
    repo = Path(intent['repo'])
    _base, sources = replay_evidence.source_commits(
        git, repo, expected['before_head'], expected['target_base_oid'],
    )
    replayed = [replay_evidence.commit_fact(git, repo, oid) for oid in
                replay_evidence.replay_commits(
                    git, repo, expected['target_base_oid'], observed['head'],
                )]
    if len(sources) != len(replayed):
        raise MutationEvidenceError('rebase legacy replay: source count is ambiguous')
    slot = _slot(state_dir, intent['repo'], 'managed_rebase', intent['target'])
    receipts = [intent]
    initial_count = 0
    for path in slot.glob('*.result.json'):
        result = _read(path)
        if result.get('status') != 'complete' or result.get('id') == intent['id']:
            continue
        saved = _read(slot / f"{result.get('id')}.intent.json")
        if (saved.get('owner') == intent['owner']
                and saved.get('expected', {}).get('before_head') == expected['before_head']
                and saved['expected'].get('target_base_oid') == expected['target_base_oid']
                and saved.get('target') == intent['target']):
            if (result.get('id') != saved.get('id')
                    or not isinstance(result.get('candidates'), dict)):
                raise MutationEvidenceError('rebase legacy replay: sealed result mismatch')
            if (not saved['expected'].get('action')
                    and saved['before'].get('metadata') is None
                    and saved['before'].get('head') == expected['before_head']
                    and result.get('observed', {}).get('metadata') is not None):
                initial_count += 1
            receipts.append(saved)
    if initial_count != 1:
        raise MutationEvidenceError('rebase legacy replay: unique initial execution missing')
    corrections: dict[str, dict[str, str]] = {}
    for receipt in receipts:
        item = receipt['expected']
        tree = item.get('staged_tree_oid')
        if item.get('action') != 'continue' or not tree:
            continue
        matches = [index for index, commit in enumerate(replayed)
                   if commit['parent'] == receipt['before']['head']
                   and commit['tree'] == tree]
        if len(matches) != 1:
            raise MutationEvidenceError('rebase legacy resolution: staged tree has no unique commit')
        source = sources[matches[0]]
        accepted = item.get('accepted_files')
        if (source in corrections or not isinstance(accepted, dict) or not accepted
                or any(receipt['before'].get('dirty', {}).get(path, [None, None])[1]
                       != signature for path, signature in accepted.items())):
            raise MutationEvidenceError('rebase legacy resolution: approved files mismatch')
        corrections[source] = {
            'source': source, 'parent': receipt['before']['head'],
            'tree': tree, 'intent_id': receipt['id'],
            'turn_id': item.get('correction_turn') or receipt['id'],
        }
    if not corrections:
        raise MutationEvidenceError('rebase legacy resolution: no approved tree')
    source_facts = {oid: replay_evidence.commit_fact(git, repo, oid) for oid in sources}
    try:
        return replay_evidence.verify_replay(
            sources=sources, replayed=replayed,
            target=expected['target_base_oid'], head=observed['head'],
            corrections=corrections, skips={}, source_facts=source_facts,
            same_patch=lambda source, actual: replay_evidence.patch_equal(
                git, repo, source, actual,
            ),
        )
    except replay_evidence.ReplayEvidenceError as error:
        raise MutationEvidenceError(str(error)) from error


def _conflict_files_bounded(before: Mapping[str, Any],
                            observed: Mapping[str, Any],
                            affected: Any) -> bool:
    """Reject file/index changes outside the immutable replay path set."""
    if (not isinstance(affected, list) or any(
            not isinstance(path, str) or not path for path in affected
    ) or not isinstance(before.get("index"), dict)
            or not isinstance(observed.get("index"), dict)
            or not isinstance(before.get("dirty"), dict)
            or not isinstance(observed.get("dirty"), dict)):
        return False
    allowed = set(affected)
    unmerged_paths = {
        path for path, entries in observed["index"].items()
        if isinstance(entries, list) and any(
            isinstance(entry, str) and entry.rsplit(" ", 1)[-1] in {"1", "2", "3"}
            for entry in entries
        )
    }
    if bool(observed.get("unmerged")) != bool(unmerged_paths):
        return False
    for field in ("index", "dirty"):
        old, new = before[field], observed[field]
        if any(old.get(path) != new.get(path) and path not in allowed
               for path in old.keys() | new.keys()):
            return False
    # A replay does not create untracked files, even on an affected path.
    for path, value in observed["dirty"].items():
        if (not isinstance(value, list) or len(value) != 2
                or not isinstance(value[0], list)):
            return False
        kinds = value[0]
        if ("untracked" in kinds and path not in before["dirty"]
                or "unstaged" in kinds and path not in unmerged_paths
                and before["dirty"].get(path) != value):
            return False
    return True


def verified_rebase_result(
    intent: Mapping[str, Any], observed: Mapping[str, Any], *,
    current_remote_oid: str | None, target_ancestor: bool,
    patches_preserved: bool, reflog_marker: bool,
    autostash_owned: bool = False, accepted_correction: bool = False,
    replay_verified: bool = False,
) -> dict[str, Any]:
    """Validate one initial or continued rebase without owning file edits."""
    before = intent["before"]
    expected = intent["expected"]
    branch = expected["branch"]
    original = expected["before_head"]
    metadata = {
        "orig-head": original, "head-name": f"refs/heads/{branch}",
        "onto": expected["target_base_oid"],
    }
    beginning = before["metadata"] is None
    if (
        current_remote_oid != expected["remote_oid"]
        or not target_ancestor
        or not before["ref"] or before["ref"][0] != original
        or not before["registration"]
        or (beginning and (
            before["head"] != original or before["branch"] != branch
            or f"branch=refs/heads/{branch}" not in before["registration"]
            or f"HEAD={original}" not in before["registration"]
        ))
        or (not beginning and (
            before["metadata"] != metadata
            or "detached=True" not in before["registration"]
        ))
    ):
        raise MutationEvidenceError("Manager rebase 실행 전 소유권 또는 원격 OID가 다릅니다.")
    if observed["metadata"] is not None:
        valid = (
            reflog_marker
            and observed["metadata"] == metadata
            and observed["branch"] is None
            and observed["ref"] == before["ref"]
            and observed["registration"] is not None
            and "detached=True" in observed["registration"]
            and f"HEAD={observed['head']}" in observed["registration"]
            and _conflict_files_bounded(
                before, observed, expected.get("affected_paths"),
            )
        )
    else:
        files_preserved = rebase_preserves_worktree_files(
            before["dirty"], observed["dirty"],
            before_evidence=before.get("file_evidence"),
            after_evidence=observed.get("file_evidence"),
        )
        valid = (
            observed["branch"] == branch
            and observed["ref"] is not None
            and observed["ref"][0] == observed["head"]
            and observed["registration"] is not None
            and f"branch=refs/heads/{branch}" in observed["registration"]
            and f"HEAD={observed['head']}" in observed["registration"]
            and ((replay_verified if expected.get('evidence_version') == 2 else
                  patches_preserved or (expected.get("action") == "skip"
                                        and accepted_correction)
                 or (expected.get("action") == "continue"
                     and expected.get("staged_tree_oid")
                     and accepted_correction)))
            and reflog_marker
            and (expected.get('evidence_version') != 2
                 or ((observed.get('index_clean')
                      and isinstance(observed.get('operations'), Mapping)
                      and not controlling_git_operation(observed.get('operations')))
                     or (autostash_owned and observed.get('unmerged')
                         and not observed.get('metadata'))))
            and (expected.get('evidence_version') != 2
                 or beginning or autostash_owned or not observed.get('dirty'))
            and (not observed.get("unmerged") or autostash_owned)
            and (not beginning or files_preserved
                 or (observed.get("unmerged") and autostash_owned))
        )
    if not valid:
        raise MutationEvidenceError("Manager rebase 결과의 HEAD·ref·index·등록 상태가 다릅니다.")
    return {
        "refs": {f"refs/heads/{branch}": observed["ref"]},
        "worktrees": {intent["target"]: observed["registration"]},
        "selected": {"head": observed["head"],
                     "index": (observed["index"] if observed["metadata"] is None
                               and observed.get("index_clean")
                               and not observed.get("unmerged") else None),
                     "dirty": (observed["dirty"] if observed["metadata"] is None
                               else None),
                     "unmerged": bool(observed.get("unmerged"))},
    }


def verified_transition(
    intent: Mapping[str, Any], observed: Mapping[str, Any], marker: bool,
) -> dict[str, dict[str, Any]]:
    """Return only matching ref/worktree baseline candidates.

    The before/after maps are deliberately small, task-relevant observations.
    A source local delta is result evidence, never an Agent allowance.
    """
    try:
        before = intent["before"]
        expected = intent["expected"]
        kind = intent["kind"]
        target = intent["target"]
    except (KeyError, TypeError) as error:
        raise MutationEvidenceError("Manager Git intent가 불완전합니다.") from error
    if (not isinstance(before, Mapping) or not isinstance(expected, Mapping)
            or not isinstance(kind, str) or not isinstance(target, str)):
        raise MutationEvidenceError("Manager Git intent 형식이 유효하지 않습니다.")
    oid = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
    if kind in {"bootstrap_fetch", "project_fetch"}:
        valid = (
            target.startswith("refs/remotes/origin/")
            and set(before) == set(expected) == {"oid"}
            and isinstance(expected["oid"], str) and oid.fullmatch(expected["oid"])
            and (before["oid"] is None or (
                isinstance(before["oid"], str) and oid.fullmatch(before["oid"])))
        )
    elif kind == "source_fast_forward":
        valid = (
            set(before) == set(expected) == {"head", "branch", "index", "changes", "operations"}
            and isinstance(before["head"], str) and oid.fullmatch(before["head"])
            and isinstance(expected["head"], str) and oid.fullmatch(expected["head"])
            and isinstance(before["branch"], str) and before["branch"] == expected["branch"]
            and isinstance(before["index"], dict)
            and isinstance(expected["index"], dict)
            and before["changes"] == expected["changes"]
            and before["operations"] == expected["operations"] == []
        )
    elif kind in {"runtime_create", "runtime_remove"}:
        valid = (
            set(before) == set(expected) == {"exists", "registration"}
            and isinstance(before["exists"], bool)
            and isinstance(expected["exists"], bool)
            and (kind != "runtime_create" or (
                before == {"exists": False, "registration": None}
                and expected["exists"] is True
                and isinstance(expected["registration"], dict)
                and isinstance(expected["registration"].get("head"), str)
                and oid.fullmatch(expected["registration"]["head"])
                and expected["registration"].get("detached") is True
                and isinstance(expected["registration"].get("locked"), str)
                and expected["registration"]["locked"].startswith("loop-manager-runtime:")
            ))
            and (kind != "runtime_remove" or (
                before["exists"] is True
                and isinstance(before["registration"], dict)
                and isinstance(before["registration"].get("head"), str)
                and oid.fullmatch(before["registration"]["head"])
                and before["registration"].get("detached") is True
                and isinstance(before["registration"].get("locked"), str)
                and before["registration"]["locked"].startswith("loop-manager-runtime:")
                and expected == {"exists": False, "registration": None}
            ))
        )
    elif kind in {"managed_worktree_create", "managed_worktree_remove"}:
        registration = (expected.get("registration") if kind == "managed_worktree_create"
                        else before.get("registration"))
        branch = registration.get("branch") if isinstance(registration, dict) else None
        plan_id = (branch.removeprefix("refs/heads/loop-manager/")
                   if isinstance(branch, str)
                   and branch.startswith("refs/heads/loop-manager/") else None)
        valid = (
            set(before) == set(expected) == {"exists", "registration", "index"}
            and plan_id is not None and re.fullmatch(r"G-\d+", plan_id)
            and intent.get("owner") == f"plan:{plan_id}"
            and Path(target).name == plan_id
            and (kind != "managed_worktree_create" or (
                before == {"exists": False, "registration": None, "index": None}
                and expected["exists"] is True
                and isinstance(expected["registration"], dict)
                and expected["registration"].get("branch", "").startswith("refs/heads/loop-manager/G-")
                and isinstance(expected["registration"].get("head"), str)
                and oid.fullmatch(expected["registration"]["head"])
                and isinstance(expected["registration"].get("locked"), str)
                and expected["registration"]["locked"].startswith("loop-manager-worktree:")
                and isinstance(expected["index"], str) and oid.fullmatch(expected["index"])
            ))
            and (kind != "managed_worktree_remove" or (
                before["exists"] is True
                and isinstance(before["registration"], dict)
                and isinstance(before["index"], str) and oid.fullmatch(before["index"])
                and expected == {"exists": False, "registration": None, "index": None}
            ))
        )
    else:
        valid = False
    if not valid:
        raise MutationEvidenceError("Manager Git intent의 허용 전이가 유효하지 않습니다.")
    if observed != expected or (before != expected and not marker):
        raise MutationEvidenceError("Manager Git 결과 또는 작업 소유 표식이 일치하지 않습니다.")
    if kind in {"bootstrap_fetch", "project_fetch"}:
        return {"refs": {target: expected["oid"]}, "worktrees": {}}
    if kind == "runtime_create":
        return {"refs": {}, "worktrees": {target: expected["registration"]}}
    if kind == "runtime_remove":
        return {"refs": {}, "worktrees": {target: None}}
    if kind == "managed_worktree_create":
        return {"refs": {}, "worktrees": {target: expected["registration"]}}
    if kind == "managed_worktree_remove":
        return {"refs": {}, "worktrees": {target: None}}
    if kind == "source_fast_forward":
        return {"refs": {}, "worktrees": {}}
    raise MutationEvidenceError(f"지원하지 않는 Manager Git 작업: {kind}")


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MutationEvidenceError(f"Manager Git journal을 읽을 수 없습니다: {path}") from error
    if not isinstance(value, dict):
        raise MutationEvidenceError("Manager Git journal 형식이 유효하지 않습니다.")
    return value


def _slot(state_dir: Path, repo: str, kind: str, target: str) -> Path:
    key = json.dumps([repo, kind, target], separators=(",", ":"))
    digest = hashlib.sha256(key.encode()).hexdigest()
    return state_dir / "manager-git-journal" / digest


def execute(
    state_dir: Path, *, owner: str, kind: str, repo: str, target: str,
    before: Mapping[str, Any], expected: Mapping[str, Any],
    observe: Callable[[], dict[str, Any]],
    apply: Callable[[str], None],
    marker: Callable[[Mapping[str, Any]], bool],
) -> str:
    """Execute or recover one transition. Return applied, recovered, or equal.

    The immutable intent is fsynced before Git. A separate result is fsynced
    before clearing active state. An interrupted old intent with no Git effect
    is explicitly recorded as aborted before a new intent can be started.
    """
    if not owner or not repo or not target or not re.fullmatch(r"[a-z_]+", kind):
        raise MutationEvidenceError("Manager Git 작업 identity가 유효하지 않습니다.")
    root = state_dir / "manager-git-journal"
    root.mkdir(parents=True, exist_ok=True)
    _sync_directory(state_dir)
    slot = _slot(state_dir, repo, kind, target)
    slot.mkdir(parents=True, exist_ok=True)
    _sync_directory(root)
    active = slot / "active.json"
    current = observe()
    if active.exists():
        previous = _read(active)
        if (
            previous.get("version") != 1 or previous.get("repo") != repo
            or previous.get("kind") != kind or previous.get("target") != target
            or not re.fullmatch(r"[0-9a-f]{32}", str(previous.get("id", "")))
            or _read(slot / f"{previous['id']}.intent.json") != previous
        ):
            raise MutationEvidenceError("Manager Git 활성 intent가 불완전합니다.")
        if current == previous["expected"]:
            candidates = verified_transition(previous, current, marker(previous))
            _finish(slot, active, previous, current, candidates, "complete")
            if previous["owner"] == owner and previous["expected"] == expected:
                return "recovered"
        elif current == previous["before"]:
            if previous["owner"] != owner or previous["expected"] != expected:
                _finish(slot, active, previous, current, {}, "aborted")
            else:
                apply(str(previous["id"]))
                after = observe()
                candidates = verified_transition(previous, after, marker(previous))
                _finish(slot, active, previous, after, candidates, "complete")
                return "applied"
        else:
            raise MutationEvidenceError("Manager Git intent 뒤 영향권 상태가 예상 밖으로 변경됐습니다.")
        current = observe()
    if current != before:
        raise MutationEvidenceError("Manager Git 작업 직전 상태가 기대값과 다릅니다.")
    if before == expected:
        # Equal is a durable observation, with no Git write to attribute.
        outcome = "equal"
    else:
        outcome = "complete"
    identity = secrets.token_hex(16)
    intent = {
        "version": 1, "id": identity, "owner": owner, "kind": kind,
        "repo": repo, "target": target, "before": dict(before),
        "expected": dict(expected),
    }
    _write_new(slot / f"{identity}.intent.json", intent)
    _write_new(active, intent)
    if before != expected:
        apply(identity)
    after = observe()
    candidates = verified_transition(intent, after, marker(intent))
    _finish(slot, active, intent, after, candidates, outcome)
    return "equal" if before == expected else "applied"


def recover_active(
    state_dir: Path, *, repo: str, kind: str, target: str,
    observe: Callable[[], dict[str, Any]],
    marker: Callable[[Mapping[str, Any]], bool],
) -> bool:
    """Finalize an interrupted write before a caller's early equal return."""
    slot = _slot(state_dir, repo, kind, target)
    active = slot / "active.json"
    if not active.exists():
        return False
    intent = _read(active)
    if (intent.get("repo"), intent.get("kind"), intent.get("target")) != (
        repo, kind, target,
    ) or _read(slot / f"{intent.get('id')}.intent.json") != intent:
        raise MutationEvidenceError("Manager Git 활성 intent가 불완전합니다.")
    current = observe()
    if current == intent["before"]:
        return False
    candidates = verified_transition(intent, current, marker(intent))
    _finish(slot, active, intent, current, candidates, "complete")
    return True


def _finish(
    slot: Path, active: Path, intent: Mapping[str, Any],
    observed: Mapping[str, Any], candidates: Mapping[str, Any], status: str,
    replay_proof: list[dict[str, str]] | None = None,
) -> None:
    result_path = slot / f"{intent['id']}.result.json"
    result = {"id": intent["id"], "status": status,
              "observed": dict(observed), "candidates": dict(candidates)}
    if replay_proof is not None:
        result['replay_proof'] = replay_proof
    if result_path.exists():
        if _read(result_path) != result:
            raise MutationEvidenceError("Manager Git 저장 결과가 현재 증거와 다릅니다.")
    else:
        _write_new(result_path, result)
    active.unlink()
    _sync_directory(slot)


def verified_candidates(
    state_dir: Path, repo: str, *,
    baseline_refs: Mapping[str, Any], baseline_worktrees: Mapping[str, Any],
    live_refs: Mapping[str, Any], live_worktrees: Mapping[str, Any],
    marker: Callable[[Mapping[str, Any]], bool],
) -> dict[str, dict[str, Any]]:
    """Offer only unbroken, owned transitions from a caller's baseline to Git."""
    root = state_dir / "manager-git-journal"
    candidates: dict[str, dict[str, Any]] = {"refs": {}, "worktrees": {}}
    if not root.exists():
        return candidates
    transitions: dict[tuple[str, str], list[tuple[Any, Any]]] = {}
    active_targets: set[tuple[str, str]] = set()
    for slot in root.iterdir():
        if not slot.is_dir():
            continue
        active = slot / "active.json"
        if active.exists():
            intent = _read(active)
            if str(intent.get("kind", "")).startswith("managed_"):
                continue
            category = "refs" if intent.get("kind") in {
                "bootstrap_fetch", "project_fetch",
            } else "worktrees"
            active_targets.add((category, str(intent.get("target"))))
        for result_path in slot.glob("*.result.json"):
            result = _read(result_path)
            if result.get("status") != "complete":
                continue
            identity = str(result.get("id", ""))
            intent = _read(slot / f"{identity}.intent.json")
            if intent.get("repo") != repo:
                continue
            if str(intent.get("kind", "")).startswith("managed_"):
                continue
            validated = verified_transition(
                intent, result["observed"], marker(intent),
            )
            if validated != result["candidates"]:
                raise MutationEvidenceError("Manager Git 저장된 기준선 후보가 불일치합니다.")
            for category in candidates:
                for target, value in validated.get(category, {}).items():
                    before = intent["before"]
                    prior = before["oid"] if category == "refs" else before["registration"]
                    transitions.setdefault((category, target), []).append((prior, value))
    for (category, target), steps in transitions.items():
        if (category, target) in active_targets:
            continue
        baseline = baseline_refs if category == "refs" else baseline_worktrees
        live = live_refs if category == "refs" else live_worktrees
        cursor = baseline.get(target)
        wanted = live.get(target)
        if cursor == wanted:
            continue
        seen: set[str] = set()
        remaining = steps[:]
        while True:
            key = json.dumps(cursor, sort_keys=True)
            if key in seen:
                break
            seen.add(key)
            next_values = [after for before, after in remaining if before == cursor]
            if not next_values or any(value != next_values[0] for value in next_values):
                break
            value = next_values[0]
            remaining = [(before, after) for before, after in remaining
                         if not (before == cursor and after == value)]
            cursor = value
            if cursor == wanted:
                # A later owned transition out of this state means a live
                # reversion cannot be attributed to this earlier result.
                if not any(before == cursor for before, _after in remaining):
                    candidates[category][target] = cursor
                break
    return candidates


def _same_rebase_expectation(saved: object, current: Mapping[str, Any]) -> bool:
    if saved == current:
        return True
    if not isinstance(saved, dict) or saved.get('evidence_version') != 2:
        return False
    old_generation = saved.get('checkpoint_generation')
    new_generation = current.get('checkpoint_generation')
    return (
        type(old_generation) is int and type(new_generation) is int
        and new_generation > old_generation
        and saved == {**current, 'checkpoint_generation': old_generation}
    )


def begin_rebase(
    state_dir: Path, *, owner: str, repo: str, target: str,
    before: Mapping[str, Any], expected: Mapping[str, Any],
) -> str:
    """Persist a rebase intent whose resulting HEAD and index are not predictable."""
    if not owner.startswith("plan:G-") or not repo or not target:
        raise MutationEvidenceError("Manager rebase 작업 identity가 유효하지 않습니다.")
    root = state_dir / "manager-git-journal"
    root.mkdir(parents=True, exist_ok=True)
    _sync_directory(state_dir)
    slot = _slot(state_dir, repo, "managed_rebase", target)
    slot.mkdir(parents=True, exist_ok=True)
    _sync_directory(root)
    active = slot / "active.json"
    if active.exists():
        prior = _read(active)
        if (slot / f"{prior.get('id')}.result.json").exists():
            raise MutationEvidenceError(
                "Manager rebase 결과 기록을 먼저 복구해야 합니다."
            )
        if (
            prior.get("version") != 1 or prior.get("kind") != "managed_rebase"
            or prior.get("repo") != repo or prior.get("target") != target
            or _read(slot / f"{prior.get('id')}.intent.json") != prior
            or prior.get("owner") != owner or prior.get("before") != before
            or not _same_rebase_expectation(prior.get("expected"), expected)
        ):
            raise MutationEvidenceError("Manager rebase 활성 intent가 현재 작업과 다릅니다.")
        return str(prior["id"])
    identity = secrets.token_hex(16)
    intent = {
        "version": 1, "id": identity, "owner": owner,
        "kind": "managed_rebase", "repo": repo, "target": target,
        "before": dict(before), "expected": dict(expected),
    }
    _write_new(slot / f"{identity}.intent.json", intent)
    _write_new(active, intent)
    return identity


def complete_rebase(
    state_dir: Path, *, repo: str, target: str,
    observe: Callable[[], dict[str, Any]],
    validate: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]],
    allow_equal: bool = False,
    replay_proof: Callable[[Mapping[str, Any], Mapping[str, Any]],
                           list[dict[str, str]]] | None = None,
) -> bool:
    """Finalize only an owned Git outcome; leave ambiguous intent active."""
    slot = _slot(state_dir, repo, "managed_rebase", target)
    active = slot / "active.json"
    if not active.exists():
        return False
    intent = _read(active)
    if (
        intent.get("version") != 1 or intent.get("kind") != "managed_rebase"
        or intent.get("repo") != repo or intent.get("target") != target
        or _read(slot / f"{intent.get('id')}.intent.json") != intent
    ):
        raise MutationEvidenceError("Manager rebase 활성 intent가 불완전합니다.")
    observed = observe()
    if observed == intent["before"]:
        if (intent['expected'].get('evidence_version') == 2
                and allow_equal
                and successful_rebase_invocation(
                    state_dir, repo, target, intent['id'],
                )):
            candidates = validate(intent, observed)
            proof = replay_proof(intent, observed) if replay_proof is not None else None
            _finish(slot, active, intent, observed, candidates, 'complete', proof)
            return True
        if allow_equal and intent['expected'].get('evidence_version') != 2:
            _finish(slot, active, intent, observed, {}, "aborted")
            return True
        return False
    candidates = validate(intent, observed)
    proof = replay_proof(intent, observed) if replay_proof is not None else None
    _finish(slot, active, intent, observed, candidates, "complete", proof)
    return True


def verified_managed_transitions(
    state_dir: Path, repo: str, owner: str, *,
    agent_session_id: str | None = None,
    git: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Offer G-092 sealed per-operation evidence, not a live-state allowance."""
    root = state_dir / "manager-git-journal"
    if not root.exists():
        return []
    transitions = []
    for slot in root.iterdir():
        if not slot.is_dir():
            continue
        active = slot / "active.json"
        active_id = _read(active).get("id") if active.exists() else None
        for result_path in slot.glob("*.result.json"):
            result = _read(result_path)
            # A pending operation must not hide earlier sealed evidence. Its
            # own result remains unavailable until active-intent cleanup ends.
            if result.get("id") == active_id or result.get("status") != "complete":
                continue
            intent = _read(slot / f"{result.get('id')}.intent.json")
            if intent.get("repo") != repo or intent.get("owner") != owner:
                continue
            kind = intent.get("kind")
            if kind not in {
                "managed_worktree_create", "managed_worktree_remove", "managed_rebase",
            }:
                continue
            if (kind == "managed_rebase"
                    and intent["expected"].get("agent_session_id")
                    != agent_session_id):
                continue
            if result.get("id") != intent.get("id") or result.get("candidates") is None:
                raise MutationEvidenceError("Manager Git 전이 결과 identity가 다릅니다.")
            if not isinstance(result.get("observed"), dict):
                raise MutationEvidenceError("Manager Git 저장 결과 상태가 불완전합니다.")
            if kind in {"managed_worktree_create", "managed_worktree_remove"}:
                action = "create" if kind.endswith("create") else "remove"
                receipt = state_dir / "managed-worktree-intents" / f"{intent['id']}.{action}.json"
                marker = receipt.is_file() and _read(receipt) == {
                    "id": intent["id"], "repo": intent["repo"],
                    "target": intent["target"], "before": intent["before"],
                    "expected": intent["expected"],
                }
                expected_candidates = verified_transition(
                    intent, result["observed"], marker,
                )
            else:
                observed = result["observed"]
                branch = intent["expected"]["branch"]
                if (
                    not isinstance(observed, dict)
                    or not isinstance(observed.get("head"), str)
                    or not isinstance(observed.get("index"), dict)
                    or not isinstance(observed.get("dirty"), dict)
                    or not isinstance(observed.get("registration"), list)
                    or type(observed.get("index_clean")) is not bool
                    or intent["owner"] != f"plan:{branch.removeprefix('loop-manager/')}"
                ):
                    raise MutationEvidenceError("Manager rebase 저장 결과가 유효하지 않습니다.")
                try:
                    replay_verified = False
                    if intent['expected'].get('evidence_version') == 2:
                        if git is None:
                            raise MutationEvidenceError('rebase stored replay requires Git observation')
                        proof = rebase_replay_proof(git, state_dir, intent, observed)
                        if result.get('replay_proof') != proof:
                            raise MutationEvidenceError('rebase stored replay proof mismatch')
                        replay_verified = True
                    elif result.get('replay_proof') is not None:
                        if git is None or result['replay_proof'] != legacy_rebase_replay_proof(
                                git, state_dir, intent, observed):
                            raise MutationEvidenceError('rebase stored legacy replay proof mismatch')
                    expected_candidates = verified_rebase_result(
                        intent, observed,
                        current_remote_oid=intent["expected"]["remote_oid"],
                        target_ancestor=True, patches_preserved=True,
                        reflog_marker=True, autostash_owned=True,
                        replay_verified=replay_verified,
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise MutationEvidenceError(
                        "Manager rebase 저장 결과가 불완전합니다."
                    ) from error
            if result["candidates"] != expected_candidates:
                raise MutationEvidenceError("Manager Git 저장된 전이 후보가 불일치합니다.")
            transitions.append({"intent": intent, "result": result})
    return transitions
