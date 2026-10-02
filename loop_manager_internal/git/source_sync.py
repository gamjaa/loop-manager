"""Preserving source checkout fast-forward effects."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from collections.abc import Mapping
from loop_manager_internal.agents.scope_observation import _git_operation_path_signature
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.runtime.contracts import BaseWorktreeSnapshot
from loop_manager_internal.runtime.contracts import BaseWorktreeSyncResult
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.runtime.runtime_identity import _canonical_path
from pathlib import Path
import os


@dataclass(frozen=True)
class BaseWorktreeSnapshotEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _base_local_path_signature: Callable[..., object]
    _base_worktree_index_error: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _ignored_worktree_paths: Callable[..., object]
    _index_entries: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    git: Callable[..., object]


@dataclass(frozen=True)
class BasePathsOverlapEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _paths_overlap: Callable[..., object]

@dataclass(frozen=True)
class SourceTransitionStateEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _base_worktree_snapshot: Callable[..., object]

@dataclass(frozen=True)
class SourceTransitionMarkerEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    git: Callable[..., object]

@dataclass(frozen=True)
class SourceExpectedIndexEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    git_bytes: Callable[..., object]

@dataclass(frozen=True)
class SynchronizeBaseWorktreeEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _base_paths_overlap: Callable[..., object]
    _base_worktree_index_error: Callable[..., object]
    _base_worktree_snapshot: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    _source_expected_index: Callable[..., object]
    _source_transition_marker: Callable[..., object]
    _source_transition_state: Callable[..., object]
    git: Callable[..., object]
    is_git_lock_failure: Callable[..., object]

@dataclass(frozen=True)
class EnsureSafeBaseWorktreeEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    synchronize_base_worktree: Callable[..., object]

@dataclass(frozen=True)
class LogBaseWorktreeSyncEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    operation_log: Callable[..., object]


def _base_worktree_snapshot(effects: BaseWorktreeSnapshotEffects,
    repo: Path,
) -> BaseWorktreeSnapshot:
    branch_result = effects.git(
        repo,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    head = effects.git(repo, "rev-parse", "HEAD").stdout.strip()
    index = effects._index_entries(repo)
    operations = tuple(sorted(effects._git_operation_entries(repo).items()))
    preliminary = BaseWorktreeSnapshot(
        branch=branch,
        head=head,
        index=tuple(sorted(index.items())),
        changes=(),
        operations=operations,
    )
    if operations or effects._base_worktree_index_error(preliminary):
        return preliminary
    changes = []
    for kind, paths in (
        ("staged", effects._nul_git_paths(
            repo, "diff", "--cached", "--no-renames", "--name-only", "-z",
            "--ignore-submodules=none",
        )),
        ("unstaged", effects._nul_git_paths(
            repo, "diff", "--no-renames", "--name-only", "-z",
            "--ignore-submodules=none",
        )),
        ("untracked", effects._nul_git_paths(
            repo, "ls-files", "--others", "--exclude-standard", "-z",
        )),
        ("ignored", effects._ignored_worktree_paths(repo)),
    ):
        changes.extend((kind, path, effects._base_local_path_signature(repo, path)) for path in paths)
    return BaseWorktreeSnapshot(
        branch=branch,
        head=head,
        index=preliminary.index,
        changes=tuple(sorted(changes)),
        operations=operations,
    )


def _base_local_path_signature(repo: Path, relative_path: str) -> str:
    path = repo / relative_path
    if not path.exists() and not path.is_symlink():
        return "missing"
    return _git_operation_path_signature(path)


def _base_worktree_index_error(snapshot: BaseWorktreeSnapshot) -> str | None:
    for path, entries in snapshot.index:
        if len(entries) != 1:
            return path
        fields = entries[0].split()
        if (
            len(fields) != 4
            or fields[0] != "H"
            or fields[3] != "0"
            or not fields[2].strip("0")
        ):
            return path
    return None


def _base_paths_overlap(effects: BasePathsOverlapEffects, left: str, right: str) -> bool:
    # A remote checkout can be case insensitive even when the current host is not.
    return effects._paths_overlap(left.casefold(), right.casefold())


def _source_transition_state(effects: SourceTransitionStateEffects, repo: Path) -> dict[str, object]:
    snapshot = effects._base_worktree_snapshot(repo)
    return {
        "branch": snapshot.branch, "head": snapshot.head,
        "index": {path: list(entries) for path, entries in snapshot.index},
        "changes": [list(entry) for entry in snapshot.changes],
        "operations": [list(entry) for entry in snapshot.operations],
    }


def _source_transition_marker(effects: SourceTransitionMarkerEffects, repo: Path, base: str, intent: Mapping[str, object]) -> bool:
    token = f"loop-manager-source:{intent['id']}: "
    for refname in (f"refs/heads/{base}", "HEAD"):
        result = effects.git(repo, "reflog", "show", "-1", "--format=%H%x00%gs",
                     refname, check=False)
        if result.returncode or not result.stdout.startswith(
            f"{intent['expected']['head']}\x00{token}"
        ):
            return False
    return True


def _source_expected_index(effects: SourceExpectedIndexEffects,
    repo: Path, selected_remote: str, before_index: Mapping[str, object],
    changed_paths: tuple[str, ...],
) -> dict[str, object]:
    """Keep local entries verbatim and replace only the remote tree delta."""
    expected = dict(before_index)
    remote_entries = {}
    for record in effects.git_bytes(repo, "ls-tree", "-r", "-z", selected_remote).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, _kind, oid = metadata.decode("ascii").split()
            remote_entries[os.fsdecode(path)] = [f"H {mode} {oid} 0"]
    for path in changed_paths:
        expected.pop(path, None)
        if path in remote_entries:
            expected[path] = remote_entries[path]
    return expected


def synchronize_base_worktree(effects: SynchronizeBaseWorktreeEffects,
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
    owner: str = "project",
) -> BaseWorktreeSyncResult:
    """Best-effort fast-forward of an eligible primary checkout."""

    selected_remote = remote_sha or ""
    try:
        if state_dir is not None:
            manager_git_journal.recover_active(
                state_dir, repo=str(_canonical_path(repo)),
                kind="source_fast_forward", target=str(_canonical_path(repo)),
                observe=lambda: effects._source_transition_state(repo),
                marker=lambda intent: effects._source_transition_marker(repo, base, intent),
            )
        observed_remote = effects.git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base}^{{commit}}",
        ).stdout.strip()
        selected_remote = selected_remote or observed_remote
        if selected_remote != observed_remote:
            return BaseWorktreeSyncResult(
                "skipped", "선택한 원격 SHA와 현재 origin ref가 다름", selected_remote
            )
        # Equal HEAD needs no file or index scan: nothing will be checked out.
        if effects.git(repo, "rev-parse", "HEAD").stdout.strip() == selected_remote:
            current_branch = effects.git(
                repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
            )
            if current_branch.returncode:
                return BaseWorktreeSyncResult("skipped", "detached HEAD", selected_remote)
            branch = current_branch.stdout.strip()
            if branch != base:
                return BaseWorktreeSyncResult(
                    "skipped", f"다른 branch checkout: {branch}", selected_remote
                )
            operations = effects._git_operation_entries(repo)
            if operations:
                return BaseWorktreeSyncResult(
                    "skipped",
                    "진행 중 Git operation: " + ", ".join(sorted(operations)),
                    selected_remote,
                )
            return BaseWorktreeSyncResult("equal", "이미 원격 base와 같음", selected_remote)
        before = effects._base_worktree_snapshot(repo)
        if before.branch is None:
            return BaseWorktreeSyncResult("skipped", "detached HEAD", selected_remote)
        if before.branch != base:
            return BaseWorktreeSyncResult(
                "skipped",
                f"다른 branch checkout: {before.branch}",
                selected_remote,
            )
        if before.operations:
            return BaseWorktreeSyncResult(
                "skipped",
                "진행 중 Git operation: " + ", ".join(name for name, _ in before.operations),
                selected_remote,
            )
        if before.head == selected_remote:
            return BaseWorktreeSyncResult("equal", "이미 원격 base와 같음", selected_remote)
        index_error = effects._base_worktree_index_error(before)
        if index_error:
            return BaseWorktreeSyncResult(
                "skipped",
                f"비정상 index entry: {index_error}",
                selected_remote,
            )

        behind = effects.git(
            repo,
            "merge-base",
            "--is-ancestor",
            before.head,
            selected_remote,
            check=False,
        )
        if behind.returncode not in {0, 1}:
            return BaseWorktreeSyncResult(
                "failed",
                "local/remote ancestry 확인 실패: " + behind.stdout[-2000:].strip(),
                selected_remote,
            )
        if behind.returncode == 1:
            ahead = effects.git(
                repo,
                "merge-base",
                "--is-ancestor",
                selected_remote,
                before.head,
                check=False,
            )
            if ahead.returncode not in {0, 1}:
                return BaseWorktreeSyncResult(
                    "failed",
                    "local/remote ancestry 확인 실패: "
                    + ahead.stdout[-2000:].strip(),
                    selected_remote,
                )
            reason = "ahead" if ahead.returncode == 0 else "diverged"
            return BaseWorktreeSyncResult("skipped", reason, selected_remote)

        changed_paths = tuple(
            effects._nul_git_paths(
                repo,
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                before.head,
                selected_remote,
            )
        )
        overlaps = sorted({
            (kind, path)
            for kind, path, _ in before.changes
            if any(effects._base_paths_overlap(path, changed) for changed in changed_paths)
        })
        if overlaps:
            return BaseWorktreeSyncResult(
                "skipped",
                "원격 변경과 겹치는 로컬 경로: "
                + ", ".join(f"{kind} {path}" for kind, path in overlaps[:8]),
                selected_remote,
            )

        rechecked = effects._base_worktree_snapshot(repo)
        if rechecked != before:
            return BaseWorktreeSyncResult(
                "skipped",
                "eligibility 확인 뒤 기본 worktree 상태가 변경됨",
                selected_remote,
            )
        current_remote = effects.git(
            repo, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}"
        ).stdout.strip()
        if current_remote != selected_remote:
            return BaseWorktreeSyncResult(
                "skipped", "eligibility 확인 뒤 origin ref가 변경됨", selected_remote
            )
        if state_dir is None:
            updated = effects.git(
                repo, "merge", "--ff-only", "--no-autostash", selected_remote,
                check=False,
            )
            if updated.returncode:
                output = updated.stdout[-2000:].strip()
                reason = "Git lock 충돌" if effects.is_git_lock_failure(output) else "fast-forward 실패"
                if output:
                    reason += f": {output}"
                return BaseWorktreeSyncResult("failed", reason, selected_remote)
        else:
            before_state = effects._source_transition_state(repo)
            if before_state["head"] != before.head or before_state["branch"] != base:
                raise manager_git_journal.MutationEvidenceError(
                    "source fast-forward 직전 HEAD 또는 branch가 변경됐습니다."
                )
            if before_state != {
                "branch": before.branch, "head": before.head,
                "index": {path: list(entries) for path, entries in before.index},
                "changes": [list(entry) for entry in before.changes],
                "operations": [list(entry) for entry in before.operations],
            }:
                raise manager_git_journal.MutationEvidenceError(
                    "source fast-forward 직전 로컬 상태가 변경됐습니다."
                )
            expected_state = {
                **before_state, "head": selected_remote,
                "index": effects._source_expected_index(
                    repo, selected_remote, before_state["index"], changed_paths,
                ),
            }

            def apply(token: str) -> None:
                environment = {**os.environ,
                               "GIT_REFLOG_ACTION": f"loop-manager-source:{token}"}
                updated = effects.git(
                    repo, "merge", "--ff-only", "--no-autostash", selected_remote,
                    check=False, env=environment,
                )
                if updated.returncode:
                    raise RuntimeError("fast-forward 실패: " + updated.stdout[-2000:].strip())

            manager_git_journal.execute(
                state_dir, owner=owner, kind="source_fast_forward",
                repo=str(_canonical_path(repo)), target=str(_canonical_path(repo)),
                before=before_state, expected=expected_state,
                observe=lambda: effects._source_transition_state(repo), apply=apply,
                marker=lambda intent: effects._source_transition_marker(repo, base, intent),
            )
        final_head = effects.git(repo, "rev-parse", "HEAD").stdout.strip()
        if final_head != selected_remote:
            return BaseWorktreeSyncResult(
                "failed",
                f"fast-forward 결과 HEAD 불일치: {final_head}",
                selected_remote,
            )
        return BaseWorktreeSyncResult("updated", "원격 base로 fast-forward", selected_remote)
    except (GitLockError, RuntimeError, OSError) as error:
        return BaseWorktreeSyncResult("failed", str(error), selected_remote)


def ensure_safe_base_worktree(effects: EnsureSafeBaseWorktreeEffects,
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
) -> BaseWorktreeSyncResult:
    """Compatibility entry point for the non-blocking worktree synchronizer."""

    return effects.synchronize_base_worktree(
        repo, base, remote_sha=remote_sha, state_dir=state_dir,
    )


def log_base_worktree_sync(effects: LogBaseWorktreeSyncEffects, repo: Path, result: BaseWorktreeSyncResult) -> None:
    effects.operation_log(
        f"기본 worktree 동기화 {result.status} ({repo}): {result.reason}",
        error=result.status in {"skipped", "failed"},
    )
