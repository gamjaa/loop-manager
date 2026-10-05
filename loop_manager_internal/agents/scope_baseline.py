"""Agent local tree capture/restoration through the existing Git adapter."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import os
import stat
import tempfile

from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentScopeError
from loop_manager_internal.agents.agent_scope_decision import _same_local_snapshot


@dataclass(frozen=True)
class ScopeBaselineEffects:
    """Explicit effects supplied by the composition boundary."""

    _handoff_run: Callable[..., subprocess.CompletedProcess[bytes]]
    capture_git_snapshot: Callable[..., GitSnapshot]
    command: Callable[..., subprocess.CompletedProcess[str]]
    git: Callable[..., subprocess.CompletedProcess[str]]


def _git_with_index(
    worktree: Path,
    index_path: Path,
    *arguments: str,
    effects: ScopeBaselineEffects,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index_path)
    return effects.command(
        ["git", "-C", str(worktree), *arguments],
        env=environment,
    )


def _hash_git_blob(worktree: Path, contents: bytes, *, effects: ScopeBaselineEffects) -> str:
    result = effects._handoff_run(
        ["git", "-C", str(worktree), "hash-object", "-w", "--stdin"],
        input=contents,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        output = result.stdout[-6000:].decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Agent 시작 파일 보존 실패:\n{output}")
    return result.stdout.decode("ascii").strip()


def _agent_worktree_tree(
    worktree: Path,
    snapshot: GitSnapshot,
    index_tree: str,
    *,
    effects: ScopeBaselineEffects,
) -> str:
    """Write the exact dirty worktree state through an isolated index."""

    with tempfile.TemporaryDirectory(prefix="loop-manager-index-") as directory:
        temporary_index = Path(directory) / "index"
        _git_with_index(worktree, temporary_index, "read-tree", index_tree, effects=effects)
        for relative_path in sorted(snapshot.dirty):
            target = worktree / relative_path
            try:
                target_stat = target.lstat()
            except FileNotFoundError:
                _git_with_index(
                    worktree,
                    temporary_index,
                    "update-index",
                    "--force-remove",
                    "--",
                    relative_path,
                    effects=effects,
                )
                continue

            if stat.S_ISREG(target_stat.st_mode):
                mode = "100755" if target_stat.st_mode & 0o111 else "100644"
                object_id = _hash_git_blob(worktree, target.read_bytes(), effects=effects)
            elif stat.S_ISLNK(target_stat.st_mode):
                mode = "120000"
                object_id = _hash_git_blob(
                    worktree,
                    os.fsencode(os.readlink(target)),
                    effects=effects,
                )
            elif stat.S_ISDIR(target_stat.st_mode):
                nested_head = effects.git(
                    target,
                    "rev-parse",
                    "--verify",
                    "HEAD",
                    check=False,
                )
                if nested_head.returncode:
                    raise RuntimeError(
                        "Agent 시작 시점의 directory 변경을 안전하게 "
                        f"보존할 수 없습니다: {relative_path}"
                    )
                mode = "160000"
                object_id = nested_head.stdout.strip()
            else:
                raise RuntimeError(
                    "Agent 시작 시점의 특수 파일을 안전하게 보존할 수 "
                    f"없습니다: {relative_path}"
                )
            _git_with_index(
                worktree,
                temporary_index,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                object_id,
                relative_path,
                effects=effects,
            )
        return _git_with_index(
            worktree,
            temporary_index,
            "write-tree",
            effects=effects,
        ).stdout.strip()


def _commit_tree(worktree: Path, tree: str, parent: str, message: str, *, effects: ScopeBaselineEffects) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Loop Manager",
            "GIT_AUTHOR_EMAIL": "loop-manager@localhost",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
            "GIT_COMMITTER_NAME": "Loop Manager",
            "GIT_COMMITTER_EMAIL": "loop-manager@localhost",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
        }
    )
    return effects.command(
        [
            "git",
            "-C",
            str(worktree),
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            message,
        ],
        env=environment,
    ).stdout.strip()


def _capture_agent_baseline(
    repo: str,
    worktree: Path,
    snapshot: GitSnapshot,
    *,
    effects: ScopeBaselineEffects,
    preserve_conflicts: bool = False,
) -> str | None:
    if not snapshot.dirty:
        return None
    if snapshot.operations and not preserve_conflicts:
        # Rebase-conflict resolution intentionally starts inside a Git
        # operation. Its scope is still validated, but it cannot use the
        # ordinary HEAD/index tree recovery representation.
        return None
    unmerged = effects.git(worktree, "ls-files", "-u", "-z").stdout
    if unmerged and not preserve_conflicts:
        # Autostash can leave an unmerged index after rebase metadata vanishes.
        # Its stage OIDs are captured by the execution checkpoint, while this
        # ordinary baseline cannot represent them with write-tree.
        return None
    stage_trees = {}
    if unmerged:
        # Represent every stage as a tree so even manually staged blobs remain
        # reachable after an owned rebase is restarted. The original stage
        # entries and operation signatures stay in the caller's snapshot.
        with tempfile.TemporaryDirectory(prefix='loop-manager-conflict-index-') as directory:
            for stage in range(4):
                index_path = Path(directory) / str(stage)
                _git_with_index(worktree, index_path, 'read-tree', '--empty', effects=effects)
                for path, entries in sorted(snapshot.index.items()):
                    for entry in entries:
                        _tag, mode, oid, entry_stage = entry.split()
                        if entry_stage == str(stage):
                            _git_with_index(worktree, index_path, 'update-index', '--add',
                                            '--cacheinfo', mode, oid, path, effects=effects)
                stage_trees[str(stage)] = _git_with_index(
                    worktree, index_path, 'write-tree', effects=effects).stdout.strip()
        index_tree = effects.git(worktree, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    else:
        index_tree = effects.git(worktree, "write-tree").stdout.strip()
    worktree_tree = _agent_worktree_tree(worktree, snapshot, index_tree, effects=effects)
    identity = f"{snapshot.head}\0{index_tree}\0{worktree_tree}"
    if stage_trees:
        identity += '\0' + json.dumps(stage_trees, sort_keys=True)
    baseline_id = hashlib.sha256(identity.encode('ascii')).hexdigest()
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    refname = f"refs/loop-manager/baselines/{project_id}/{baseline_id}"
    index_commit = _commit_tree(
        worktree,
        index_tree,
        snapshot.head,
        f"Loop Manager baseline index {baseline_id}",
        effects=effects,
    )
    for stage, tree in sorted(stage_trees.items()):
        index_commit = _commit_tree(worktree, tree, index_commit,
                                   f'Loop Manager conflict stage {stage} {baseline_id}', effects=effects)
    worktree_commit = _commit_tree(
        worktree,
        worktree_tree,
        index_commit,
        f"Loop Manager baseline worktree {baseline_id}",
        effects=effects,
    )
    existing = effects.git(
        worktree,
        "rev-parse",
        "--verify",
        refname,
        check=False,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != worktree_commit:
            raise RuntimeError(f"Agent 시작 상태 보존 ref 충돌: {refname}")
    else:
        effects.git(worktree, "update-ref", refname, worktree_commit)
    return json.dumps(
        {
            "version": 2 if stage_trees else 1,
            "ref": refname,
            "ref_target": worktree_commit,
            "index_tree": index_tree,
            "worktree_tree": worktree_tree,
            **({'stage_trees': stage_trees} if stage_trees else {}),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _deserialize_agent_baseline(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise AgentScopeError(
            f"저장된 Agent 시작 상태가 유효하지 않습니다: {error}"
        ) from error
    required = ("ref", "ref_target", "index_tree", "worktree_tree")
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or any(not isinstance(parsed.get(key), str) for key in required)
    ):
        raise AgentScopeError("저장된 Agent 시작 상태 형식을 지원하지 않습니다.")
    return {key: str(parsed[key]) for key in required}


def _restore_agent_index_flags(
    worktree: Path,
    entries: Mapping[str, tuple[str, ...]],
    *,
    effects: ScopeBaselineEffects,
) -> None:
    for relative_path, states in sorted(entries.items()):
        tags = {state.split(" ", 1)[0] for state in states}
        if any(tag.lower() == "s" for tag in tags):
            effects.git(worktree, "update-index", "--skip-worktree", "--", relative_path)
        if any(tag.islower() for tag in tags):
            effects.git(worktree, "update-index", "--assume-unchanged", "--", relative_path)


def _restore_agent_staged_entries(worktree: Path, before: GitSnapshot, *, effects: ScopeBaselineEffects) -> None:
    """Restore the pre-Agent stage-0 entries after rebase autostash."""

    for relative_path, (kinds, _signature) in sorted(before.dirty.items()):
        if "staged" not in kinds:
            continue
        stage_zero: tuple[str, str] | None = None
        for entry in before.index.get(relative_path, ()):
            fields = entry.split()
            if len(fields) != 4:
                raise AgentScopeError(
                    f"저장된 Agent index 항목이 유효하지 않습니다: {relative_path}"
                )
            _tag, mode, object_id, stage = fields
            if stage == "0":
                stage_zero = (mode, object_id)
        if stage_zero is None:
            effects.git(worktree, "update-index", "--force-remove", "--", relative_path)
        else:
            mode, object_id = stage_zero
            effects.git(
                worktree,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                object_id,
                relative_path,
            )
    _restore_agent_index_flags(worktree, before.index, effects=effects)


def _restore_agent_baseline(
    worktree: Path,
    before: GitSnapshot,
    serialized: str | None,
    *,
    effects: ScopeBaselineEffects,
) -> None:
    if not before.dirty:
        return
    if serialized is None:
        raise AgentScopeError(
            "dirty Agent 시작 상태를 복원할 보존 tree가 없습니다."
        )
    baseline = _deserialize_agent_baseline(serialized)
    ref_target = effects.git(
        worktree,
        "rev-parse",
        "--verify",
        baseline["ref"],
        check=False,
    )
    if ref_target.returncode or ref_target.stdout.strip() != baseline["ref_target"]:
        raise AgentScopeError("Agent 시작 상태 보존 ref가 변경되거나 사라졌습니다.")
    worktree_tree = effects.git(
        worktree,
        "rev-parse",
        baseline["ref_target"] + "^{tree}",
    ).stdout.strip()
    index_tree = effects.git(
        worktree,
        "rev-parse",
        baseline["ref_target"] + "^1^{tree}",
    ).stdout.strip()
    if (
        worktree_tree != baseline["worktree_tree"]
        or index_tree != baseline["index_tree"]
    ):
        raise AgentScopeError("Agent 시작 상태 보존 tree가 저장된 값과 다릅니다.")

    effects.git(worktree, "read-tree", "--reset", "-u", worktree_tree)
    effects.git(worktree, "read-tree", index_tree)
    _restore_agent_index_flags(worktree, before.index, effects=effects)
    restored = effects.capture_git_snapshot(worktree)
    if not _same_local_snapshot(before, restored):
        raise AgentScopeError("Agent 시작 시점의 기존 변경을 정확히 복원하지 못했습니다.")
