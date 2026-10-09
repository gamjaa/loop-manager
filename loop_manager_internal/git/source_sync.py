"""Preserving source checkout synchronization effects."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from collections.abc import Mapping
from loop_manager_internal.agents.scope_observation import _git_operation_path_signature
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git import manager_git_journal, source_rebase
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
    _config_entries: Callable[..., object]
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
    _normalize_legacy_ignored_snapshot: Callable[..., object]
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
        config=tuple(sorted(effects._config_entries(repo).items())),
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
        config=preliminary.config,
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
    return _snapshot_state(effects._base_worktree_snapshot(repo))


def _snapshot_state(snapshot: BaseWorktreeSnapshot) -> dict[str, object]:
    return {
        "branch": snapshot.branch, "head": snapshot.head,
        "index": {path: list(entries) for path, entries in snapshot.index},
        "changes": [list(entry) for entry in snapshot.changes],
        "operations": [list(entry) for entry in snapshot.operations],
        "config": {key: list(value) for key, value in snapshot.config},
    }


def comparison_view(effects, repo: Path, start: Mapping, saved: Mapping, current: Mapping) -> dict:
    """원본 관측은 보존하고 시작 규칙으로 증명된 ignored 차이만 제외한다."""
    for state in (start, saved, current):
        if (not isinstance(state, Mapping) or not isinstance(state.get("head"), str)
                or not isinstance(state.get("branch"), (str, type(None)))
                or not isinstance(state.get("operations"), list)
                or not isinstance(state.get("index"), dict)
                or any(not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value)
                       for value in state["index"].values())
                or not isinstance(state.get("changes"), list)
                or any(not isinstance(entry, (list, tuple)) or len(entry) != 3
                       or not all(isinstance(value, str) for value in entry) for entry in state["changes"])
                or not isinstance(state.get("config", {}), dict)
                or not isinstance(state.get("files", {}), dict)
                or any(not isinstance(value, str) for value in state.get("files", {}).values())
                or any(not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value)
                       for value in state.get("config", {}).values())):
            raise manager_git_journal.MutationEvidenceError("source Git 비교 근거의 형식이 유효하지 않습니다.")
    view = dict(saved)

    def snapshot(state, *, ignored=None):
        kinds, signatures = {}, {}
        for kind, path, signature in state["changes"]:
            if kind != "ignored":
                kinds.setdefault(path, set()).add(kind)
                signatures[path] = signature
        dirty = {path: (tuple(sorted(value)), signatures[path]) for path, value in kinds.items()}
        dirty.update({path: (("untracked",), signature)
                      for path, signature in (ignored or {}).items() if path not in dirty})
        return GitSnapshot(
            state["head"], state["branch"],
            {path: tuple(entries) for path, entries in state["index"].items()},
            dirty, {}, {}, {key: tuple(value) for key, value in state.get("config", {}).items()}, {},
        )

    indexed = set(start["index"]) | set(saved["index"]) | set(current["index"])
    ignored = {path: signature for kind, path, signature in saved["changes"]
               if kind == "ignored" and path not in indexed}
    ignored.update({path: signature for path, signature in saved.get("files", {}).items()
                    if path not in indexed})
    if "config" not in start:
        known = {path for kind, path, _signature in start["changes"] if kind == "ignored"}
        ignored = {path: signature for path, signature in ignored.items() if path in known}
    if ignored:
        observed = snapshot(current)
        candidate = snapshot(saved, ignored=ignored)
        normalized = effects._normalize_legacy_ignored_snapshot(
            repo, candidate, observed, start=snapshot(start),
        )
        excluded = ignored.keys() - normalized.dirty.keys()
        view["changes"] = [entry for entry in saved["changes"]
                           if entry[0] != "ignored" or entry[1] not in excluded]
        if "files" in view:
            view["files"] = {path: signature for path, signature in saved["files"].items()
                             if path not in excluded}
    # 구형 journal에는 config 관측이 없다. 외부 ignore 증거를 새로 추정하지 않는다.
    if "config" not in start:
        view.pop("config", None)
    return view


def state_matches(effects, repo: Path, start: Mapping, saved: Mapping, current: Mapping) -> bool:
    return comparison_view(effects, repo, start, saved, current) == comparison_view(
        effects, repo, start, current, current,
    )


def _overlaps(effects, changes, changed_paths):
    return sorted({(kind, path) for kind, path, _signature in changes
                   if any(effects._base_paths_overlap(path, changed) for changed in changed_paths)})


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


def _source_git_lock_state(effects, repo: Path, base: str) -> dict[str, str]:
    """Observe relevant user locks without acquiring, removing or bypassing them."""
    state = {}
    for name in ("index.lock", "HEAD.lock", "ORIG_HEAD.lock", "REBASE_HEAD.lock",
                 "AUTO_MERGE.lock", "packed-refs.lock", f"refs/heads/{base}.lock"):
        path = Path(effects.git(repo, "rev-parse", "--git-path", name).stdout.strip())
        if not path.is_absolute():
            path = repo / path
        if path.exists() or path.is_symlink():
            stat = path.lstat()
            state[name] = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:" + _git_operation_path_signature(path)
    return state


def synchronize_base_worktree(effects: SynchronizeBaseWorktreeEffects,
    repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
    owner: str = "project",
) -> BaseWorktreeSyncResult:
    """Best-effort synchronization; source results never gate remote work."""

    selected_remote = remote_sha or ""
    try:
        if state_dir is not None:
            recovered = source_rebase.recover(effects, repo, state_dir, base)
            if recovered is not None and (recovered.status != "updated" or effects.git(
                    repo, "rev-parse", f"refs/remotes/origin/{base}").stdout.strip() == recovered.remote_sha):
                return recovered
            manager_git_journal.recover_active(
                state_dir, repo=str(_canonical_path(repo)),
                kind="source_fast_forward", target=str(_canonical_path(repo)),
                observe=lambda: effects._source_transition_state(repo),
                marker=lambda intent: effects._source_transition_marker(repo, base, intent),
                state_matches=lambda intent, saved, current: state_matches(
                    effects, repo, intent["before"], saved, current),
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
            if ahead.returncode == 0:
                return BaseWorktreeSyncResult("skipped", "ahead", selected_remote)
            return source_rebase.synchronize(
                effects, repo, state_dir, base, selected_remote, before, owner,
            )

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
        overlaps = _overlaps(effects, before.changes, changed_paths)
        if overlaps:
            return BaseWorktreeSyncResult(
                "skipped",
                "원격 변경과 겹치는 로컬 경로: "
                + ", ".join(f"{kind} {path}" for kind, path in overlaps[:8]),
                selected_remote,
            )

        rechecked = effects._base_worktree_snapshot(repo)
        if not state_matches(effects, repo, _snapshot_state(before), _snapshot_state(before), _snapshot_state(rechecked)):
            return BaseWorktreeSyncResult(
                "skipped",
                "eligibility 확인 뒤 기본 worktree 상태가 변경됨",
                selected_remote,
            )
        overlaps = _overlaps(effects, rechecked.changes, changed_paths)
        if overlaps:
            return BaseWorktreeSyncResult(
                "skipped", "Git 쓰기 직전 경로 충돌: " + ", ".join(path for _kind, path in overlaps[:8]), selected_remote,
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
                repo, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", selected_remote,
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
            if not state_matches(effects, repo, _snapshot_state(before), _snapshot_state(before), before_state):
                raise manager_git_journal.MutationEvidenceError(
                    "source fast-forward 직전 로컬 상태가 변경됐습니다."
                )
            expected_state = {
                **before_state, "head": selected_remote,
                "index": effects._source_expected_index(
                    repo, selected_remote, before_state["index"], changed_paths,
                ),
            }
            slot = manager_git_journal._slot(
                state_dir, str(_canonical_path(repo)), "source_fast_forward",
                str(_canonical_path(repo)),
            )
            matches = lambda intent, saved, current: state_matches(effects, repo, intent["before"], saved, current)
            if (slot / "active.json").exists():
                prior = manager_git_journal._read(slot / "active.json")
                if matches(prior, prior["before"], before_state) and matches(prior, prior["expected"], expected_state):
                    receipt_path = slot / f"{prior['id']}.invocation.json"
                    started_path = slot / f"{prior['id']}.started.json"
                    if receipt_path.exists() or started_path.exists():
                        receipt = manager_git_journal._read(receipt_path if receipt_path.exists() else started_path)
                        if receipt.get("id") != prior["id"]:
                            raise manager_git_journal.MutationEvidenceError("source fast-forward invocation identity 불일치")
                        if receipt_path.exists() and (
                            not receipt.get("returncode") or not matches(prior, receipt.get("observed"), before_state)
                        ):
                            raise manager_git_journal.MutationEvidenceError("source fast-forward 결과와 현재 상태 불일치; 기존 근거 보존")
                        same_locks = receipt.get("locks") == _source_git_lock_state(effects, repo, base)
                        if receipt_path.exists() and same_locks:
                            return BaseWorktreeSyncResult(
                                "failed", "동일 입력의 확인된 실패 fast-forward 반복 보류; intent 보존", selected_remote,
                            )
                        # Lock disappearance is a relevant new input. Retire
                        # only an unchanged source attempt, retaining its audit.
                        if not same_locks:
                            manager_git_journal._finish(slot, slot / "active.json", prior, before_state, {}, "aborted", state_matches=matches)
                    # A start marker without a result does not prove failure.
                    # execute() rechecks the unchanged source before resuming.

            def apply(token: str) -> None:
                current = effects._source_transition_state(repo)
                if (not state_matches(effects, repo, before_state, before_state, current)
                        or _overlaps(effects, current["changes"], changed_paths)):
                    raise manager_git_journal.MutationEvidenceError("source fast-forward 직전 상태 변경 또는 경로 충돌; 파일 보존")
                locks_before = _source_git_lock_state(effects, repo, base)
                start = {"id": token, "locks": locks_before}
                start_path = slot / f"{token}.started.json"
                if start_path.exists():
                    if manager_git_journal._read(start_path) != start:
                        raise manager_git_journal.MutationEvidenceError("source fast-forward 시작 입력 변경; intent 보존")
                else:
                    manager_git_journal._write_new(start_path, start)
                environment = {**os.environ,
                               "GIT_REFLOG_ACTION": f"loop-manager-source:{token}"}
                updated = effects.git(
                    repo, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", selected_remote,
                    check=False, env=environment,
                )
                locks_after = _source_git_lock_state(effects, repo, base)
                manager_git_journal._write_new(slot / f"{token}.invocation.json", {
                    "id": token, "returncode": updated.returncode,
                    "locks": (locks_after or locks_before) if updated.returncode else locks_after,
                    "observed": effects._source_transition_state(repo),
                    "diagnostic": updated.stdout[-2000:].strip(),
                })
                if updated.returncode:
                    raise RuntimeError("fast-forward 실패: " + updated.stdout[-2000:].strip())

            manager_git_journal.execute(
                state_dir, owner=owner, kind="source_fast_forward",
                repo=str(_canonical_path(repo)), target=str(_canonical_path(repo)),
                before=before_state, expected=expected_state,
                observe=lambda: effects._source_transition_state(repo), apply=apply,
                marker=lambda intent: effects._source_transition_marker(repo, base, intent),
                state_matches=matches,
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
        f"기본 worktree 동기화 {result.status} ({repo}, remote={result.remote_sha}): {result.reason}",
        error=result.status in {"skipped", "failed"},
    )
