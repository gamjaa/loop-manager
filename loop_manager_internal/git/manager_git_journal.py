"""Durable, operation-owned evidence for Manager Git mutations.

Each transition holds a resource lease and consumes Git observations and an action
marker (a reflog message or a worktree lock reason).  This module never infers
ownership from an OID or elapsed time alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation, rebase_preserves_worktree_files
from loop_manager_internal.git import managed_rebase_evidence as replay_evidence
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.runtime.resource_lock import journal_resource


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
    return parse_remote_branch_oid(result.stdout, ref)


def parse_remote_branch_oid(output: str, ref: str) -> str | None:
    """Parse one exact live ref; never grant ownership from this observation."""
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    if (len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref
            or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', lines[0][0]) is None):
        raise MutationEvidenceError('rebase remote: live branch response is ambiguous')
    return lines[0][0]


def exact_branch_lease(branch: str, expected_oid: str | None) -> str:
    """Encode a pinned OID or absence; callers retain their own safety gates."""
    return f'--force-with-lease=refs/heads/{branch}:{expected_oid or ""}'


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
    frozen_sources = expected.get('source_commits')
    run_id = expected.get('run_id')
    generation = expected.get('checkpoint_generation')
    if (not isinstance(frozen_sources, list) or not isinstance(run_id, str)
            or not run_id or not isinstance(expected.get('source_base'), str)
            or not isinstance(generation, int)):
        raise MutationEvidenceError('rebase start: source sequence is incomplete')
    repo = Path(intent['repo'])
    actual_base, actual_sources = replay_evidence.source_commits(
        git, repo, expected['before_head'], expected['target_base_oid'],
    )
    if actual_base != expected['source_base']:
        raise MutationEvidenceError('rebase start: frozen source sequence mismatch')
    sources = replay_evidence.reconcile_frozen_sources(
        git, repo, original=expected['before_head'],
        target=expected['target_base_oid'], base=actual_base,
        frozen=frozen_sources, current=actual_sources,
    )
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
                    or saved['expected'].get('source_commits') != frozen_sources
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
        if (saved.get('repo') == intent['repo']
                and saved.get('owner') == intent['owner']
                and saved.get('expected', {}).get('agent_session_id')
                == expected.get('agent_session_id')
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
                or item.get('agent_session_id') != expected.get('agent_session_id')
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


def _matching_state(intent, saved, current, state_matches=None):
    if state_matches is None:
        return saved == current
    if intent.get("kind") != "source_fast_forward":
        raise MutationEvidenceError("ignored 비교는 source fast-forward에만 적용할 수 있습니다.")
    return state_matches(intent, saved, current)


def verified_transition(
    intent: Mapping[str, Any], observed: Mapping[str, Any], marker: bool, *, state_matches=None,
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
    if kind in {"bootstrap_fetch", "project_fetch", "manual_branch_push"}:
        valid = (
            target.startswith("refs/heads/loop-manager/G-" if kind == "manual_branch_push" else "refs/remotes/origin/")
            and set(before) == set(expected) == {"oid"}
            and isinstance(expected["oid"], str) and oid.fullmatch(expected["oid"])
            and (before["oid"] is None or (
                isinstance(before["oid"], str) and oid.fullmatch(before["oid"])))
        )
    elif kind in {"source_fast_forward", "manual_fast_forward"}:
        valid = (
            set(before) == set(expected)
            and set(before) in ({"head", "branch", "index", "changes", "operations"},
                                {"head", "branch", "index", "changes", "operations", "config"})
            and isinstance(before["head"], str) and oid.fullmatch(before["head"])
            and isinstance(expected["head"], str) and oid.fullmatch(expected["head"])
            and isinstance(before["branch"], str) and before["branch"] == expected["branch"]
            and isinstance(before["index"], dict)
            and isinstance(expected["index"], dict)
            and before["changes"] == expected["changes"]
            and before["operations"] == expected["operations"] == []
            and before.get("config") == expected.get("config")
            and ("config" not in before or isinstance(before["config"], dict))
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
    if not _matching_state(intent, expected, observed, state_matches) or (before != expected and not marker):
        raise MutationEvidenceError("Manager Git 결과 또는 작업 소유 표식이 일치하지 않습니다.")
    if kind in {"bootstrap_fetch", "project_fetch", "manual_branch_push"}:
        return {"refs": {target: expected["oid"]}, "worktrees": {}}
    if kind == "runtime_create":
        return {"refs": {}, "worktrees": {target: expected["registration"]}}
    if kind == "runtime_remove":
        return {"refs": {}, "worktrees": {target: None}}
    if kind == "managed_worktree_create":
        return {"refs": {}, "worktrees": {target: expected["registration"]}}
    if kind == "managed_worktree_remove":
        return {"refs": {}, "worktrees": {target: None}}
    if kind in {"source_fast_forward", "manual_fast_forward"}:
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


def _read(path: Path, *, require_rebase_branch: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MutationEvidenceError(f"Manager Git journal을 읽을 수 없습니다: {path}") from error
    if not isinstance(value, dict):
        raise MutationEvidenceError("Manager Git journal 형식이 유효하지 않습니다.")
    if path.name == 'active.json' or path.name.endswith('.intent.json'):
        legacy_rebase = (path.name.endswith('.intent.json')
                         and value.get('kind') is None
                         and isinstance(value.get('expected'), dict)
                         and isinstance(value['expected'].get('branch'), str))
        if (not isinstance(value.get('id'), str)
                or not isinstance(value.get('owner'), str)
                or not (isinstance(value.get('kind'), str) or legacy_rebase)
                or not isinstance(value.get('repo'), str)
                or not isinstance(value.get('target'), str)
                or not isinstance(value.get('before'), dict)
                or not isinstance(value.get('expected'), dict)):
            raise MutationEvidenceError("Manager Git intent 형식이 유효하지 않습니다.")
        before, expected = value['before'], value['expected']
        if value.get('kind') in {'bootstrap_fetch', 'project_fetch', 'manual_branch_push'}:
            valid = (isinstance(before.get('oid'), (str, type(None)))
                     and isinstance(expected.get('oid'), str))
        elif value.get('kind') in {'source_fast_forward', 'manual_fast_forward'}:
            valid = all(isinstance(item.get('index'), dict)
                        and isinstance(item.get('changes'), list)
                        and isinstance(item.get('operations'), list)
                        for item in (before, expected))
        elif value.get('kind') == 'source_rebase':
            valid = (value.get('version') == 1
                     and re.fullmatch(r'[0-9a-f]{32}', value['id']) is not None
                     and all(isinstance(before.get(key), str) for key in (
                         'head', 'branch', 'ref', 'remote', 'origin', 'head_reflog', 'branch_reflog'))
                     and isinstance(before.get('index'), dict)
                     and isinstance(before.get('files'), dict)
                     and isinstance(before.get('changes'), list)
                     and isinstance(before.get('operations'), list)
                     and isinstance(expected.get('base'), str)
                     and isinstance(expected.get('remote'), str)
                     and isinstance(expected.get('affected'), list))
        elif value.get('kind') == 'managed_rebase' or legacy_rebase:
            valid = (isinstance(before.get('head'), str)
                     and ('branch' not in expected
                          or isinstance(expected['branch'], str))
                     and all(isinstance(item[key], dict)
                             for item in (before, expected)
                             for key in ('index', 'dirty', 'accepted_files', 'staged_index')
                             if key in item))
        else:
            valid = all(isinstance(item.get('exists'), bool)
                        and isinstance(item.get('registration'), (dict, type(None)))
                        for item in (before, expected))
        if not valid:
            raise MutationEvidenceError("Manager Git intent의 중첩 근거가 유효하지 않습니다.")
        # Partial rebase audits remain readable; a sealed transition needs
        # the branch before it can provide mutation evidence.
        if (require_rebase_branch and value.get('kind') == 'managed_rebase'
                and not expected.get('branch')):
            raise MutationEvidenceError("Manager rebase 저장 intent에 branch가 없습니다.")
    elif path.name.endswith('.result.json'):
        if (not isinstance(value.get('id'), str)
                or not isinstance(value.get('status'), str)
                or not isinstance(value.get('observed'), dict)
                or not isinstance(value.get('candidates'), dict)):
            raise MutationEvidenceError("Manager Git 결과 형식이 유효하지 않습니다.")
        candidates = value['candidates']
        observed = value['observed']
        if (any(not isinstance(candidates[key], dict)
                for key in ('refs', 'worktrees', 'selected') if key in candidates)
                or ('index' in observed and not (
                    isinstance(observed['index'], (str, type(None)))
                    if 'exists' in observed else isinstance(observed['index'], dict)))
                or ('dirty' in observed and not isinstance(observed['dirty'], dict))
                or ('metadata' in observed and observed['metadata'] is not None
                    and not isinstance(observed['metadata'], dict))):
            raise MutationEvidenceError("Manager Git 결과의 중첩 근거가 유효하지 않습니다.")
    return value


def _slot(state_dir: Path, repo: str, kind: str, target: str) -> Path:
    key = json.dumps([repo, kind, target], separators=(",", ":"))
    digest = hashlib.sha256(key.encode()).hexdigest()
    return state_dir / "manager-git-journal" / digest


def rebase_uses_separate_workspace(
    state_dir: Path, repo: str, intent: Mapping[str, Any], *,
    worktrees: Sequence[Path], git: Callable[..., Any], refs: Sequence[str] = (),
) -> bool:
    """Check the saved rebase's resource separation, without accepting its effects.

    This is the planning release's resource proof, not writer termination or a
    lease grant. Consumers must retain their own writer, ownership and safety
    gates. In particular, paths alone cannot authorize another Git mutation.
    """
    if intent.get('repo') != repo or intent.get('kind') != 'managed_rebase' or not worktrees:
        return False
    target = Path(intent['target']).resolve()
    branch = intent['expected'].get('branch')
    if (target in {Path(repo).resolve(), *(path.resolve() for path in worktrees)}
            or not branch or not branch.startswith('loop-manager/G-')
            or intent['owner'] != f"plan:{branch.removeprefix('loop-manager/')}"
            or intent['before'].get('branch') != branch
            or f'refs/heads/{branch}' in refs
            or f'refs/remotes/origin/{branch}' in refs
            or re.fullmatch(r'[0-9a-f]{32}', intent['id']) is None):
        return False
    slot = _slot(state_dir, repo, intent['kind'], intent['target'])

    def unchanged():
        return (_read(slot / f"{intent['id']}.intent.json") == intent
                and _read(slot / 'active.json') == intent)

    if not unchanged():
        return False
    registration = git(Path(repo), 'worktree', 'list', '--porcelain', '-z')
    if registration.returncode:
        return False
    found = False
    for record in registration.stdout.split('\0\0'):
        fields = dict(item.split(' ', 1) for item in record.split('\0') if ' ' in item)
        path = fields.get('worktree')
        if not path:
            continue
        registered = Path(path).resolve()
        ref = fields.get('branch')
        if ref == f'refs/heads/{branch}' and registered != target:
            return False
        if registered == target:
            if not fields.get('HEAD') or ref not in (None, f'refs/heads/{branch}'):
                return False
            found = True
    # Git observation may yield to another operation. Never reuse an intent
    # which changed or was replaced while the registration was being read.
    return found and unchanged()


def _rebase_scope_owners(connection, repo, intent):
    """Read the current resource claims and writer identities, without observing Git."""
    if connection is None:
        raise MutationEvidenceError('rebase scope: current ownership ledger unavailable')
    plan_id = intent['owner'].removeprefix('plan:')
    plans = tuple(dict(row) for row in connection.execute(
        'SELECT plan_id,branch,worktree,workspace_owned FROM exec_plans WHERE repo=? ORDER BY plan_id', (repo,)))
    plan = next((row for row in plans if row['plan_id'] == plan_id), None)
    branch, target = intent['expected'].get('branch'), Path(intent['target']).resolve()
    if (not isinstance(branch, str) or not branch.startswith('loop-manager/G-')
            or intent['owner'] != 'plan:' + branch.removeprefix('loop-manager/')
            or plan is None or plan['workspace_owned'] != 1 or plan['branch'] != branch
            or not plan['worktree'] or Path(plan['worktree']).resolve() != target
            or any(row['plan_id'] != plan_id and (row['branch'] == branch or
                   row['worktree'] and Path(row['worktree']).resolve() == target) for row in plans)):
        raise MutationEvidenceError('rebase scope: current resource owner differs')
    project = connection.execute('SELECT origin,base_branch,worktree FROM projects WHERE repo=?', (repo,)).fetchone()
    if (project is None or project['base_branch'] == branch
            or project['worktree'] and Path(project['worktree']).resolve() == target):
        raise MutationEvidenceError('rebase scope: project resource owner differs')
    manual = tuple(dict(row) for row in connection.execute(
        "SELECT group_id,branch,worktree,state FROM manual_work_groups WHERE repo=? "
        "AND state NOT IN ('PR_OPEN','CLOSED','MERGED') ORDER BY group_id", (repo,)))
    if any(row['branch'] == branch or row['worktree'] and Path(row['worktree']).resolve() == target for row in manual):
        raise MutationEvidenceError('rebase scope: manual resource owner differs')
    calls = tuple(dict(row) for row in connection.execute(
        'SELECT * FROM task_call_reservations WHERE repo=? AND plan_id=? ORDER BY token', (repo, plan_id)))
    invocations = tuple(dict(row) for row in connection.execute(
        'SELECT * FROM agent_invocations WHERE repo=? AND (plan_id=? OR plan_id IS NULL) ORDER BY invocation_id',
        (repo, plan_id)))
    checkpoint = connection.execute(
        'SELECT * FROM execution_checkpoints WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
    return plans, dict(project), manual, calls, invocations, dict(checkpoint) if checkpoint else None


def _stage_rebase_is_independent(state_dir, repo, intent, *, connection, git, worktrees, refs):
    from loop_manager_internal.recovery.plan_recovery import check_stopped_writers
    from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned, resource_is_held
    from loop_manager_internal.runtime.contracts import ProcessLockBusyError
    from loop_manager_internal.storage.task_calls import CallDeferred

    if intent['kind'] != 'managed_rebase' or git is None:
        return False, 'operation_resources_unknown'
    # No observation or lock wait is allowed while holding a coordinated DB request.
    if getattr(connection, '_held', False) or connection is not None and connection.in_transaction:
        return False, 'database_boundary_busy'
    try:
        owners = _rebase_scope_owners(connection, repo, intent)
        branch = intent['expected'].get('branch')
        resources = ['plan:' + intent['owner'].removeprefix('plan:'), 'branch:' + branch,
                     'worktree:' + intent['target'], 'git:' + intent['target'], 'git:config']
        missing = [resource for resource in resources if not resource_owned(state_dir, repo, resource, owner='pending-git-scope')]
        if any(resource_is_held(state_dir, repo, resource) for resource in missing):
            return False, 'resource_lease_busy'
        with resource_locks(state_dir, repo, missing, owner='pending-git-scope'):
            if owners != _rebase_scope_owners(connection, repo, intent):
                return False, 'resource_owner_changed'
            check_stopped_writers(connection, state_dir, owners[3], owners[4])

            def shared_identity():
                identities = []
                for context in (Path(repo), Path(intent['target'])):
                    result = git(context, 'rev-parse', '--path-format=absolute', '--git-common-dir', check=False)
                    if result.returncode or not result.stdout.strip():
                        raise MutationEvidenceError('rebase scope: shared repository unavailable')
                    common = Path(result.stdout.strip())
                    identities.append((common if common.is_absolute() else context / common).resolve())
                if identities[0] != identities[1]:
                    raise MutationEvidenceError('rebase scope: shared repository differs')
                config = git(Path(repo), 'config', '--null', '--local', '--includes', '--list', check=False)
                if config.returncode or not config.stdout:
                    raise MutationEvidenceError('rebase scope: shared configuration unavailable')
                ref = f'refs/heads/{branch}'
                symbolic = git(Path(repo), 'symbolic-ref', '--quiet', ref, check=False)
                tip = git(Path(repo), 'show-ref', '--verify', '--hash', ref, check=False)
                if (symbolic.returncode != 1 or tip.returncode
                        or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', tip.stdout.strip()) is None):
                    raise MutationEvidenceError('rebase scope: local branch ref unavailable or indirect')
                return identities[0], config.stdout, tip.stdout.strip()

            shared = shared_identity()
            separate = rebase_uses_separate_workspace(
                state_dir, repo, intent, worktrees=worktrees, refs=refs, git=git)
            check_stopped_writers(connection, state_dir, owners[3], owners[4])
            if shared != shared_identity():
                return False, 'shared_configuration_or_repository_changed'
            slot = _slot(state_dir, repo, intent['kind'], intent['target'])
            if (owners != _rebase_scope_owners(connection, repo, intent)
                    or _read(slot / 'active.json') != intent
                    or _read(slot / f"{intent['id']}.intent.json") != intent):
                return False, 'resource_identity_changed'
            return separate, 'separate_rebase' if separate else 'resource_conflict_or_incomplete_intent'
    except (MutationEvidenceError, CallDeferred, ProcessLockBusyError, OSError, sqlite3.Error):
        return False, 'ownership_writer_or_observation_unconfirmed'


def has_conflicting_project_mutation(
    state_dir, repo, *, stage, connection, git, worktrees, refs=(), recoverable=None,
):
    """Classify every pending operation against this stage; never complete a journal.

    The decision is used immediately at the caller's existing safety boundary.
    It is not a reusable lease or a completion receipt. Exact own-operation
    recovery remains the caller's exception; unrelated records require proof.
    """
    from loop_manager_internal.runtime import operation_events

    def independent(intent):
        slot = _slot(state_dir, repo, intent['kind'], intent['target'])
        if (re.fullmatch(r'[0-9a-f]{32}', intent['id']) is None
                or _read(slot / f"{intent['id']}.intent.json") != intent
                or _read(slot / 'active.json') != intent):
            allowed, reason = False, 'active_intent_mismatch'
        elif recoverable is not None and recoverable(intent):
            allowed, reason = True, 'exact_own_operation_recovery'
        else:
            allowed, reason = _stage_rebase_is_independent(
                state_dir, repo, intent, connection=connection, git=git,
                worktrees=worktrees, refs=refs)
        operation_events.emit('미확정 Git 작업의 단계별 자원 판단', project=repo, stage=stage,
            owner=intent['owner'], operation=intent['kind'], intent_id=intent['id'],
            resource=intent['target'],
            resources=(intent['target'], intent['expected'].get('branch'), 'shared_git_config', 'writer'),
            decision='allow' if allowed else 'hold', reason=reason,
            next_condition='current_stage_gates' if allowed else 'confirm_owner_writer_and_resources')
        return allowed

    # targets filtering cannot prove that an operation outside the list does not
    # touch a publication ref or shared configuration. Classify all records.
    try:
        return has_pending_project_mutation(state_dir, repo, recoverable=independent)
    except MutationEvidenceError:
        operation_events.emit('미확정 Git 기록을 확인할 수 없어 단계를 보류합니다.',
            project=repo, stage=stage, owner='unknown', operation='unknown', intent_id='unknown',
            resource='journal', decision='hold', reason='journal_unreadable',
            next_condition='restore_active_and_immutable_intent_evidence')
        return True


def project_mutation_identity(state_dir, repo, connection):
    """Capture journal and DB identities for a short CAS; perform no Git/process query."""
    identities = []
    def capture(intent):
        slot = _slot(state_dir, repo, intent['kind'], intent['target'])
        if re.fullmatch(r'[0-9a-f]{32}', intent['id']) is None:
            raise MutationEvidenceError('Manager Git intent identity is invalid')
        saved = _read(slot / f"{intent['id']}.intent.json")
        try:
            owners = (_rebase_scope_owners(connection, repo, intent)
                      if intent['kind'] == 'managed_rebase' else None)
        except (MutationEvidenceError, sqlite3.Error):
            owners = ('unconfirmed',)
        identities.append((intent, saved, owners))
        return False
    has_pending_project_mutation(state_dir, repo, recoverable=capture)
    return tuple(sorted(identities, key=lambda item: item[0]['id']))


def has_pending_project_mutation(state_dir: Path, repo: str, *, recoverable=None, targets=None) -> bool:
    """Unsealed managed-work intents block release; source sync is independent."""
    root = state_dir / "manager-git-journal"
    if not root.exists():
        return False
    pending = False
    source_slots = {_slot(state_dir, repo, kind, repo)
                    for kind in ("source_fast_forward", "source_rebase")}
    for slot in root.iterdir():
        # Even an unreadable source journal is diagnosed by source sync rather
        # than becoming a gate for a separate, verified planning workspace.
        if slot in source_slots:
            continue
        active = slot / "active.json"
        if active.is_file():
            try:
                intent = _read(active)
            except MutationEvidenceError:
                # A torn optional-source pointer belonging to another project
                # still has its immutable intent. Prove the resource slot;
                # never infer a managed-work exception from a damaged pointer.
                optional_source = False
                for path in slot.glob("*.intent.json"):
                    saved = _read(path)
                    if (saved.get("kind") in {"source_fast_forward", "source_rebase"}
                            and saved.get("target") == saved.get("repo")
                            and slot == _slot(state_dir, saved["repo"], saved["kind"], saved["target"])):
                        optional_source = True
                        break
                if optional_source:
                    continue
                raise
            if (intent.get("repo") == repo and (targets is None or intent.get("target") in targets)
                    and intent.get("kind") not in {"source_fast_forward", "source_rebase"}):
                if slot != _slot(state_dir, repo, intent['kind'], intent['target']):
                    raise MutationEvidenceError('Manager Git active resource slot differs')
                if not (recoverable and recoverable(intent)):
                    pending = True
    return pending


@journal_resource
def execute(
    state_dir: Path, *, owner: str, kind: str, repo: str, target: str,
    before: Mapping[str, Any], expected: Mapping[str, Any],
    observe: Callable[[], dict[str, Any]],
    apply: Callable[[str], None],
    marker: Callable[[Mapping[str, Any]], bool],
    restore_intermediate: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
    state_matches: Callable[[Mapping, Mapping, Mapping], bool] | None = None,
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
    matches = lambda intent, saved, observed: _matching_state(intent, saved, observed, state_matches)
    if active.exists():
        previous = _read(active)
        if (
            previous.get("version") != 1 or previous.get("repo") != repo
            or previous.get("kind") != kind or previous.get("target") != target
            or not re.fullmatch(r"[0-9a-f]{32}", str(previous.get("id", "")))
            or _read(slot / f"{previous['id']}.intent.json") != previous
        ):
            raise MutationEvidenceError("Manager Git 활성 intent가 불완전합니다.")
        restored = None
        if (not matches(previous, previous["before"], current) and not matches(previous, previous["expected"], current)
                and restore_intermediate is not None):
            restored = restore_intermediate(previous, current)
            current = observe()
            if not matches(previous, restored, current):
                raise MutationEvidenceError("Manager Git 중간 상태 복원 뒤 자원이 다시 변경됐습니다.")
        if restored is not None and not matches(previous, previous["before"], current):
            _finish(slot, active, previous, current, {}, "aborted", state_matches=state_matches)
        elif matches(previous, previous["expected"], current):
            candidates = verified_transition(previous, current, marker(previous), state_matches=state_matches)
            status = "equal" if previous["before"] == previous["expected"] else "complete"
            _finish(slot, active, previous, current, candidates, status, state_matches=state_matches)
            if previous["owner"] == owner and matches(previous, previous["expected"], expected):
                return "recovered"
        elif matches(previous, previous["before"], current):
            if previous["owner"] != owner or not matches(previous, previous["expected"], expected):
                _finish(slot, active, previous, current, {}, "aborted", state_matches=state_matches)
            else:
                apply(str(previous["id"]))
                after = observe()
                candidates = verified_transition(previous, after, marker(previous), state_matches=state_matches)
                _finish(slot, active, previous, after, candidates, "complete", state_matches=state_matches)
                return "applied"
        else:
            raise MutationEvidenceError("Manager Git intent 뒤 영향권 상태가 예상 밖으로 변경됐습니다.")
        current = observe()
    if not matches({"kind": kind, "before": before}, before, current):
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
    candidates = verified_transition(intent, after, marker(intent), state_matches=state_matches)
    _finish(slot, active, intent, after, candidates, outcome, state_matches=state_matches)
    return "equal" if before == expected else "applied"


@journal_resource
def recover_active(
    state_dir: Path, *, repo: str, kind: str, target: str,
    observe: Callable[[], dict[str, Any]],
    marker: Callable[[Mapping[str, Any]], bool],
    restore_intermediate: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
    state_matches: Callable[[Mapping, Mapping, Mapping], bool] | None = None,
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
    matches = lambda saved, observed: _matching_state(intent, saved, observed, state_matches)
    restored = None
    if (not matches(intent["before"], current) and not matches(intent["expected"], current)
            and restore_intermediate is not None):
        restored = restore_intermediate(intent, current)
        current = observe()
        if not matches(restored, current):
            raise MutationEvidenceError("Manager Git 중간 상태 복원 뒤 자원이 다시 변경됐습니다.")
    if restored is not None and not matches(intent["before"], current):
        _finish(slot, active, intent, current, {}, "aborted", state_matches=state_matches)
        return False
    if matches(intent["before"], current):
        if intent["before"] == intent["expected"] and (slot / f"{intent['id']}.result.json").exists():
            candidates = verified_transition(intent, current, marker(intent), state_matches=state_matches)
            _finish(slot, active, intent, current, candidates, "equal", state_matches=state_matches)
        return False
    candidates = verified_transition(intent, current, marker(intent), state_matches=state_matches)
    _finish(slot, active, intent, current, candidates, "complete", state_matches=state_matches)
    return True


def _finish(
    slot: Path, active: Path, intent: Mapping[str, Any],
    observed: Mapping[str, Any], candidates: Mapping[str, Any], status: str,
    replay_proof: list[dict[str, str]] | None = None,
    *, state_matches: Callable[[Mapping, Mapping, Mapping], bool] | None = None,
) -> None:
    result_path = slot / f"{intent['id']}.result.json"
    result = {"id": intent["id"], "status": status,
              "observed": dict(observed), "candidates": dict(candidates)}
    if replay_proof is not None:
        result['replay_proof'] = replay_proof
    if result_path.exists():
        saved = _read(result_path)
        if (saved != result and not (
                state_matches is not None
                and {key: value for key, value in saved.items() if key != "observed"}
                == {key: value for key, value in result.items() if key != "observed"}
                and _matching_state(intent, saved["observed"], observed, state_matches))):
            raise MutationEvidenceError("Manager Git 저장 결과가 현재 증거와 다릅니다.")
    else:
        _write_new(result_path, result)
    active.unlink()
    _sync_directory(slot)


def fetch_reflog_matches(state_dir: Path, repo: str, intent: Mapping[str, Any],
                         lines: list[str], live_oid: str | None, *,
                         additional_fetches: Sequence[Mapping[str, Any]] = ()) -> bool:
    """Require this fetch and its suffix, including bound SQLite actions."""
    # SQLite's action token differs from the journal identity. Bind an alias
    # to the same owner, resource and exact transition before accepting it.
    tokens = {intent['id']} | {
        saved['id'] for saved in additional_fetches
        if all(saved.get(key) == intent.get(key)
               for key in ('repo', 'owner', 'kind', 'target', 'before', 'expected'))}
    actions = {f"{intent['expected']['oid']}\0loop-manager-base-fetch:{token}" for token in tokens}
    positions = [index for index, line in enumerate(lines) if line in actions]
    if len(positions) != 1:
        return False
    position = positions[0]
    if (position + 1 < len(lines)
            and lines[position + 1].partition('\0')[0] != intent['before']['oid']):
        return False
    cursor = intent['expected']['oid']
    for line in reversed(lines[:position]):
        match = re.fullmatch(r'([0-9a-fA-F]{40}|[0-9a-fA-F]{64})\x00loop-manager-base-fetch:([0-9a-f]{32})', line)
        if match is None:
            return False
        oid, token = match.groups()
        successors = []
        for kind in ('bootstrap_fetch', 'project_fetch'):
            path = _slot(state_dir, repo, kind, intent['target']) / f'{token}.intent.json'
            if path.exists():
                saved = _read(path)
                if (saved.get('id') != token or saved.get('repo') != repo
                        or saved.get('kind') != kind or saved.get('target') != intent['target']
                        or saved.get('before') != {'oid': cursor}
                        or saved.get('expected') != {'oid': oid}):
                    return False
                successors.append(saved)
        successors.extend(saved for saved in additional_fetches if saved.get('id') == token)
        if len(successors) != 1:
            return False
        saved = successors[0]
        if (saved.get('repo') != repo or saved.get('kind') not in {'bootstrap_fetch', 'project_fetch'}
                or saved.get('target') != intent['target']
                or saved.get('before') != {'oid': cursor}
                or saved.get('expected') != {'oid': oid}):
            return False
        verified_transition(saved, {'oid': oid}, True)
        cursor = oid
    return cursor == live_oid


def verified_candidates(
    state_dir: Path, repo: str, *,
    baseline_refs: Mapping[str, Any], baseline_worktrees: Mapping[str, Any],
    live_refs: Mapping[str, Any], live_worktrees: Mapping[str, Any],
    marker: Callable[[Mapping[str, Any]], bool],
    include_pending_fetch: bool = False,
    fetch_refs: frozenset[str] | None = None,
    additional_fetches: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, Any]]:
    """Offer only unbroken, owned transitions from a caller's baseline to Git."""
    root = state_dir / "manager-git-journal"
    candidates: dict[str, dict[str, Any]] = {"refs": {}, "worktrees": {}}
    if not root.exists() and not additional_fetches:
        return candidates
    transitions: dict[tuple[str, str], list[tuple[Any, Any, Mapping[str, Any]]]] = {}
    active_targets: set[tuple[str, str]] = set()
    slots = ((root.iterdir() if root.exists() else ()) if fetch_refs is None else (
        _slot(state_dir, repo, kind, ref)
        for kind in ('bootstrap_fetch', 'project_fetch') for ref in fetch_refs))
    source_slots = {_slot(state_dir, repo, kind, repo) for kind in ('source_fast_forward', 'source_rebase')}
    for slot in slots:
        if slot in source_slots or not slot.is_dir():
            continue
        active = slot / "active.json"
        if active.exists():
            intent = _read(active)
            if fetch_refs is not None and (
                    intent.get('repo') != repo
                    or intent.get('kind') not in {'bootstrap_fetch', 'project_fetch'}
                    or intent.get('target') not in fetch_refs
                    or _slot(state_dir, repo, intent['kind'], intent['target']) != slot):
                raise MutationEvidenceError('Manager pending fetch resource identity differs')
            if str(intent.get("kind", "")).startswith("managed_"):
                continue
            category = "refs" if intent.get("kind") in {
                "bootstrap_fetch", "project_fetch",
            } else "worktrees"
            target = str(intent.get('target'))
            if (include_pending_fetch and intent.get('repo') == repo
                    and intent.get('kind') in {'bootstrap_fetch', 'project_fetch'}
                    and _read(slot / f"{intent.get('id')}.intent.json") == intent
                    and live_refs.get(target) == intent['expected'].get('oid')
                    and marker(intent)):
                # Read-only acceptance of a completed Git effect before its
                # receipt save. Do not fabricate or finalize execution audit.
                verified_transition(intent, {'oid': live_refs[target]}, True)
                transitions.setdefault(('refs', target), []).append(
                    (intent['before']['oid'], intent['expected']['oid'], intent))
            else:
                active_targets.add((category, target))
        for result_path in slot.glob("*.result.json"):
            result = _read(result_path)
            if result.get("status") != "complete":
                continue
            identity = str(result.get("id", ""))
            intent = _read(slot / f"{identity}.intent.json")
            if intent.get('id') != identity or result_path.name != f'{identity}.result.json':
                raise MutationEvidenceError('Manager Git result identity differs')
            if intent.get("repo") != repo:
                continue
            if fetch_refs is not None and (
                    intent.get('kind') not in {'bootstrap_fetch', 'project_fetch'}
                    or intent.get('target') not in fetch_refs
                    or _slot(state_dir, repo, intent['kind'], intent['target']) != slot):
                raise MutationEvidenceError('Manager fetch journal resource identity differs')
            if str(intent.get("kind", "")).startswith("managed_"):
                continue
            validated = verified_transition(
                intent, result["observed"], True,
            )
            if validated != result["candidates"]:
                raise MutationEvidenceError("Manager Git 저장된 기준선 후보가 불일치합니다.")
            for category in candidates:
                for target, value in validated.get(category, {}).items():
                    before = intent["before"]
                    prior = before["oid"] if category == "refs" else before["registration"]
                    transitions.setdefault((category, target), []).append((prior, value, intent))
    for intent in additional_fetches:
        if (intent.get('repo') != repo or intent.get('kind') not in {'bootstrap_fetch', 'project_fetch'}
                or fetch_refs is not None and intent.get('target') not in fetch_refs
                or not marker(intent)):
            continue
        verified_transition(intent, intent['expected'], True)
        transitions.setdefault(('refs', intent['target']), []).append(
            (intent['before']['oid'], intent['expected']['oid'], intent))
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
            # A completed record before this Agent baseline need not own the
            # current reflog suffix. Check only transitions we actually follow.
            next_steps = [step for step in remaining if step[0] == cursor]
            owned_steps = [step for step in next_steps if marker(step[2])]
            if next_steps and not owned_steps:
                raise MutationEvidenceError("Manager Git 결과 또는 작업 소유 표식이 일치하지 않습니다.")
            next_values = [step[1] for step in owned_steps]
            if not next_values or any(value != next_values[0] for value in next_values):
                break
            value = next_values[0]
            remaining = [step for step in remaining
                         if not (step[0] == cursor and step[1] == value)]
            cursor = value
            if cursor == wanted:
                # A later owned transition out of this state means a live
                # reversion cannot be attributed to this earlier result.
                if not any(step[0] == cursor and marker(step[2]) for step in remaining):
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


def record_current_rebase_result(state_dir, *, repo, target, owner, decision, observed):
    """Close an existing audit pointer without claiming replay or Agent evidence.

    Missing/incomplete old audit is retained and does not gate current acceptance.
    Non-rebase journals and their receipts are unaffected.
    """
    from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
    if decision.status not in {RebaseStatus.WAITING, RebaseStatus.COMPLETE}:
        raise MutationEvidenceError('cannot record rejected current rebase')
    slot = _slot(state_dir, repo, 'managed_rebase', target)
    active = slot / 'active.json'
    if not active.exists():
        return False
    try:
        intent = _read(active)
    except MutationEvidenceError:
        return False
    if (intent.get('repo') != repo or intent.get('target') != target
            or intent.get('owner') != owner or not intent.get('id')):
        return False
    # 기록 대상이 확인된 뒤에만 비용이 큰 현재 Git 상태를 수집한다.
    observed = observed() if callable(observed) else observed
    if decision.status is RebaseStatus.COMPLETE:
        from loop_manager_internal.git.managed_rebase_validation import valid_journal_observation
        if not valid_journal_observation(observed, decision.acceptance):
            raise MutationEvidenceError('current rebase observation differs from accepted state')
    # A result sealed by an older policy is audit data. Do not rewrite it.
    result_path = slot / f"{intent['id']}.result.json"
    if result_path.exists():
        _write_new(slot / f"{intent['id']}.{secrets.token_hex(8)}.acceptance.json", {
            'kind': 'current_rebase_observation', 'acceptance': decision.acceptance,
            'status': decision.status.value, 'observed': observed,
        })
        active.unlink()
        _sync_directory(slot)
    else:
        _finish(slot, active, intent, observed, {
            'kind': 'current_rebase_observation', 'acceptance': decision.acceptance,
            'status': decision.status.value,
        }, 'current_state')
    return True


def begin_current_rebase(state_dir, *, owner, repo, target, before, expected):
    """Start Manager execution audit without requiring the old audit chain."""
    slot = _slot(state_dir, repo, 'managed_rebase', target)
    active = slot / 'active.json'
    if active.exists():
        # Preserve the old pointer verbatim before replacing it. Its contents
        # are not represented as execution evidence for this new command.
        prior = active.read_bytes()
        archive = slot / f'{secrets.token_hex(16)}.superseded-active.json'
        with archive.open('xb') as stream:
            stream.write(prior)
            stream.flush()
            os.fsync(stream.fileno())
        active.unlink()
        _sync_directory(slot)
    return begin_rebase(state_dir, owner=owner, repo=repo, target=target,
                        before=before, expected=expected)


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


def verified_rebase_completion(
    state_dir: Path, *, repo: str, target: str, owner: str,
    branch: str, before_head: str, target_base_oid: str,
    remote_oid: str | None, agent_session_id: str | None,
    run_id: str | None, checkpoint_generation: int,
    observe: Callable[[], dict[str, Any]],
    validate: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]],
    replay_proof: Callable[[Mapping[str, Any], Mapping[str, Any]],
                           list[dict[str, str]] | None],
    later_correction: Callable[[Mapping[str, Any], Mapping[str, Any],
                                Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Recheck one sealed completion against the current owned Git state.

    A result file is durable evidence, not an allowance to adopt a later HEAD
    or another run. The caller's validator must check the live marker, remote,
    ownership and replay using the same rules as initial finalization.
    """
    slot = _slot(state_dir, repo, 'managed_rebase', target)
    observed = observe()
    matches = []
    owned_intents = 0
    unverified_corrections = 0
    for result_path in slot.glob('*.result.json'):
        result = _read(result_path)
        sealed_observed = result.get('observed')
        if result.get('status') != 'complete' or not isinstance(sealed_observed, dict):
            continue
        identity = result.get('id')
        if not isinstance(identity, str) or re.fullmatch(r'[0-9a-f]{32}', identity) is None:
            raise MutationEvidenceError('rebase stored completion: invalid result identity')
        intent = _read(slot / f'{identity}.intent.json')
        expected = intent.get('expected')
        if (not isinstance(expected, dict) or intent.get('version') != 1
                or intent.get('kind') != 'managed_rebase'
                or intent.get('id') != identity or intent.get('repo') != repo
                or intent.get('target') != target or intent.get('owner') != owner
                or expected.get('branch') != branch
                or expected.get('before_head') != before_head
                or expected.get('target_base_oid') != target_base_oid
                or expected.get('remote_oid') != remote_oid
                or expected.get('agent_session_id') != agent_session_id
                or expected.get('run_id') != run_id
                or (run_id is not None and (
                    type(expected.get('checkpoint_generation')) is not int
                    or expected['checkpoint_generation'] > checkpoint_generation
                ))):
            continue
        owned_intents += 1
        if (sealed_observed != observed
                and (later_correction is None
                     or not later_correction(intent, sealed_observed, observed))):
            unverified_corrections += 1
            continue
        if (result.get('candidates') != validate(intent, sealed_observed)
                or result.get('replay_proof') != replay_proof(intent, sealed_observed)):
            raise MutationEvidenceError('rebase stored completion: evidence changed')
        matches.append({'intent': intent, 'result': result})
    if len(matches) != 1:
        raise MutationEvidenceError(
            'rebase stored completion: unique owned result unavailable '
            f'(owned={owned_intents}, unverified correction={unverified_corrections}, '
            f'matching={len(matches)})'
        )
    return matches[0]


def verified_managed_transitions(
    state_dir: Path, repo: str, owner: str, *,
    agent_session_id: str | None = None,
    git: Callable[..., Any] | None = None,
    kind: str | None = None, target: str | None = None,
    include_current_rebase: bool = False,
) -> list[dict[str, Any]]:
    """Offer G-092 sealed per-operation evidence, not a live-state allowance."""
    root = state_dir / "manager-git-journal"
    if not root.exists():
        return []
    transitions = []
    if include_current_rebase and kind != 'managed_rebase':
        raise MutationEvidenceError('current rebase history requires an exact resource filter')
    if (kind is None) != (target is None):
        raise MutationEvidenceError('Manager Git resource filter requires kind and target')
    slots = root.iterdir() if kind is None else (_slot(state_dir, repo, kind, target),)
    source_slots = {_slot(state_dir, repo, source_kind, repo) for source_kind in ('source_fast_forward', 'source_rebase')}
    for slot in slots:
        if slot in source_slots or not slot.is_dir():
            continue
        active = slot / "active.json"
        active_id = _read(active).get("id") if active.exists() else None
        for result_path in slot.glob("*.result.json"):
            result = _read(result_path)
            # A pending operation must not hide earlier sealed evidence. Its
            # own result remains unavailable until active-intent cleanup ends.
            current_result = include_current_rebase and result.get('status') == 'current_state'
            if result.get("id") == active_id or (result.get("status") != "complete" and not current_result):
                continue
            intent = _read(slot / f"{result.get('id')}.intent.json",
                           require_rebase_branch=True)
            if kind is not None and (intent.get('kind') != kind or intent.get('target') != target):
                raise MutationEvidenceError('Manager Git resource identity differs')
            if intent.get("repo") != repo or intent.get("owner") != owner:
                continue
            operation_kind = intent.get("kind")
            if operation_kind not in {
                "managed_worktree_create", "managed_worktree_remove", "managed_rebase",
            }:
                continue
            if (operation_kind == "managed_rebase"
                    and intent["expected"].get("agent_session_id")
                    != agent_session_id):
                continue
            if result.get("id") != intent.get("id") or result.get("candidates") is None:
                raise MutationEvidenceError("Manager Git 전이 결과 identity가 다릅니다.")
            if not isinstance(result.get("observed"), dict):
                raise MutationEvidenceError("Manager Git 저장 결과 상태가 불완전합니다.")
            if current_result:
                from loop_manager_internal.git.managed_rebase_validation import valid_journal_observation
                candidates = result['candidates']
                observed = result['observed']
                accepted = candidates.get('acceptance') if isinstance(candidates, dict) else None
                if (operation_kind != 'managed_rebase' or not isinstance(candidates, dict)
                        or candidates.get('kind') != 'current_rebase_observation'
                        or candidates.get('status') not in {'waiting', 'complete'}
                        or not all(key in intent['before'] and key in observed for key in
                                   ('head', 'branch', 'index', 'dirty', 'operations'))
                        or (candidates['status'] == 'waiting' and accepted is not None)
                        or (candidates['status'] == 'complete' and (
                            observed.get('branch') != intent['expected']['branch']
                            or controlling_git_operation(observed.get('operations'))
                            or not valid_journal_observation(observed, accepted) or any(accepted[key] != value for key, value in {
                                'repo': repo, 'plan_id': owner.removeprefix('plan:'), 'worktree': intent['target'],
                                'branch': intent['expected']['branch'],
                                'target_base_oid': intent['expected']['target_base_oid'], 'head': observed['head']}.items())))):
                    raise MutationEvidenceError('Manager current rebase 저장 전이 identity가 다릅니다.')
                if candidates['status'] == 'complete':
                    transitions.append({'intent': intent, 'result': result})
                continue
            if operation_kind in {"managed_worktree_create", "managed_worktree_remove"}:
                action = "create" if operation_kind.endswith("create") else "remove"
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
                    accepted_correction = False
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
                        accepted_correction = bool(result['replay_proof'])
                    elif intent['expected'].get('staged_tree_oid'):
                        raise MutationEvidenceError('rebase stored legacy resolution proof missing')
                    marker = True
                    remote_oid = intent['expected']['remote_oid']
                    ancestor = True
                    if git is not None and observed.get('metadata') is None:
                        live_head = git(Path(intent['target']), 'rev-parse', 'HEAD', check=False)
                        live_ref = git(Path(intent['repo']), 'rev-parse', '--verify',
                                       f'refs/heads/{branch}', check=False)
                        current_head = live_head.stdout.strip()
                        if (live_head.returncode or live_ref.returncode
                                or live_ref.stdout.strip() != current_head):
                            continue
                        historical = current_head != observed['head']
                        if historical:
                            # A later owned commit may move the ref beyond a
                            # sealed result. Preserve the old finish receipt
                            # only while that result remains in this history.
                            if git(
                                Path(intent['target']), 'merge-base', '--is-ancestor',
                                observed['head'], current_head, check=False,
                            ).returncode != 0:
                                continue
                            reflog = git(
                                Path(intent['repo']), 'reflog', 'show',
                                '--format=%H%x00%gs', f'refs/heads/{branch}',
                                check=False,
                            )
                            finish = (
                                f"{observed['head']}\0loop-manager-rebase:{intent['id']}"
                                f" (finish): refs/heads/{branch} onto "
                                f"{intent['expected']['target_base_oid']}"
                            )
                            marker = (reflog.returncode == 0
                                      and finish in reflog.stdout.splitlines())
                        else:
                            marker = rebase_completion_matches(
                                git, Path(intent['repo']), branch=branch,
                                head=observed['head'], identity=intent['id'],
                                target_base_oid=intent['expected']['target_base_oid'],
                            )
                        if (not marker and observed == intent['before']):
                            marker = successful_rebase_invocation(
                                state_dir, intent['repo'], intent['target'], intent['id'],
                            )
                        if not marker:
                            continue
                        if not historical:
                            remote_oid = live_remote_branch_oid(
                                git, Path(intent['repo']), branch,
                            )
                        # A historical lease is the sealed value; current
                        # publication callers check their live lease again.
                        ancestor = git(
                            Path(intent['target']), 'merge-base', '--is-ancestor',
                            intent['expected']['target_base_oid'], observed['head'],
                            check=False,
                        ).returncode == 0
                    patches_preserved = (
                        False if intent['expected'].get('evidence_version') == 2
                        else (accepted_correction or (
                            git is not None and checkpoint_ownership.rebase_preserves_commits(
                                Path(intent['repo']),
                                intent['expected']['before_head'], observed['head'],
                                intent['expected']['target_base_oid'], git,
                            )
                        ))
                    )
                    expected_candidates = verified_rebase_result(
                        intent, observed,
                        current_remote_oid=remote_oid,
                        target_ancestor=ancestor, patches_preserved=patches_preserved,
                        reflog_marker=marker, autostash_owned=True,
                        accepted_correction=accepted_correction,
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
