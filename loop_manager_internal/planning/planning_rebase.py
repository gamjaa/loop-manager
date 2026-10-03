"""Recoverable remote-base adoption for a detached planning worktree.

The journal lives beside managed worktrees, outside the project checkout. A
stash is retained until the caller has recorded the new base in SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
from uuid import uuid4

from loop_manager_internal.git.manager_git_journal import _sync_directory


class PlanningRebaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanningRebaseResult:
    target: str
    changed: bool
    conflict: bool = False
    detail: str = ""
    conflict_paths: tuple[str, ...] = ()
    stash_sha: str = ""


def _journal_path(state_dir: Path, repo: str, worktree: Path) -> Path:
    identity = hashlib.sha256((repo + "\0" + str(worktree.resolve())).encode()).hexdigest()
    return state_dir / "planning-rebase" / f"{identity}.json"


def _write(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record, sort_keys=True).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".planning-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)
    _sync_directory(path.parent)


def _read(path: Path, repo: str, worktree: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlanningRebaseError("planning rebase journal을 읽을 수 없습니다.") from error
    if not isinstance(record, dict) or record.get("repo") != repo or record.get("worktree") != str(worktree.resolve()):
        raise PlanningRebaseError("planning rebase journal의 작업 소유권이 다릅니다.")
    if (any(not isinstance(record.get(field), str) or not record[field]
            for field in ("old", "target", "head", "nonce"))
            or not isinstance(record.get("dirty"), bool)):
        raise PlanningRebaseError("planning rebase journal의 원본·대상 정보가 유효하지 않습니다.")
    if record.get("phase") not in {"before_stash", "stashed", "rebasing", "applying", "restored", "conflict", "accepting"}:
        raise PlanningRebaseError("planning rebase journal 단계가 유효하지 않습니다.")
    return record


def _stash(git, worktree: Path, message: str) -> tuple[str, str] | None:
    listed = git(worktree, "stash", "list", "--format=%gd%x00%H%x00%gs").stdout
    matches = []
    for line in listed.splitlines():
        fields = line.split("\0", 2)
        if len(fields) == 3 and fields[2].endswith(message):
            matches.append((fields[0], fields[1]))
    if len(matches) > 1:
        raise PlanningRebaseError("planning rebase stash identity가 중복됐습니다.")
    return matches[0] if matches else None


def _git_ok(git, worktree: Path, *args: str) -> str:
    result = git(worktree, *args, check=False)
    if result.returncode:
        raise PlanningRebaseError(f"planning Git 단계 실패 ({' '.join(args[:2])}): {result.stdout[-2000:]}")
    return result.stdout.strip()


def rebase_in_progress(git, worktree: Path) -> bool:
    """Check Git's active rebase state; REBASE_HEAD can remain after completion."""
    for name in ("rebase-merge", "rebase-apply"):
        location = Path(_git_ok(git, worktree, "rev-parse", "--git-path", name))
        if (location if location.is_absolute() else worktree / location).exists():
            return True
    return False


def _restored_fingerprint(git, worktree: Path, revision: str = "HEAD") -> tuple[object, ...]:
    """Capture the index, tracked worktree and untracked bytes separately."""
    staged = _git_ok(git, worktree, "diff", "--binary", "--cached", revision)
    unstaged = _git_ok(git, worktree, "diff", "--binary")
    untracked = _git_ok(git, worktree, "ls-files", "--others", "--exclude-standard", "-z")
    files = []
    for name in untracked.split("\0"):
        if not name:
            continue
        path = worktree / name
        if path.is_symlink():
            files.append((name, "link", os.readlink(path)))
        elif path.is_file():
            files.append((name, "file", path.read_bytes(), path.stat().st_mode & 0o111))
        else:
            raise PlanningRebaseError(f"복원된 untracked 파일 형식이 유효하지 않습니다: {name}")
    return staged, unstaged, tuple(files)


def _restored_digest(git, worktree: Path, revision: str = "HEAD") -> str:
    return hashlib.sha256(repr(_restored_fingerprint(git, worktree, revision)).encode()).hexdigest()


def _apply_stash(git, worktree: Path, stash_sha: str):
    applied = git(worktree, "stash", "apply", "--index", stash_sha, check=False)
    if applied.returncode and not _git_ok(
        git, worktree, "status", "--porcelain", "--untracked-files=all",
    ):
        # --index can reject its patch before merging worktree content. Retry
        # only a clean checkout to expose three-way conflicts; the stash keeps
        # both staged and unstaged originals for correction.
        applied = git(worktree, "stash", "apply", stash_sha, check=False)
    return applied


def _matches_completed_stash_apply(
    git, repo_path: Path, state_dir: Path, worktree: Path, target: str,
    restored_head: str, stash_sha: str,
) -> bool | None:
    """Resolve the crash window after stash apply without replaying onto dirty files."""
    with tempfile.TemporaryDirectory(dir=state_dir) as directory:
        scratch = Path(directory) / "expected"
        _git_ok(git, repo_path, "worktree", "add", "--detach", str(scratch), restored_head)
        try:
            applied = _apply_stash(git, scratch, stash_sha)
            if applied.returncode:
                return None
            if _git_ok(git, worktree, "rev-parse", "HEAD") == target and restored_head != target:
                _git_ok(git, scratch, "reset", "--soft", target)
            return _restored_fingerprint(git, scratch) == _restored_fingerprint(git, worktree)
        finally:
            _git_ok(git, repo_path, "worktree", "remove", "--force", str(scratch))


def _stash_untracked_collisions(git, worktree: Path, stash_sha: str) -> tuple[str, ...]:
    saved = git(worktree, "ls-tree", "-r", "--name-only", "-z", f"{stash_sha}^3", check=False)
    if saved.returncode:
        return ()
    return tuple(sorted(
        path for path in saved.stdout.split("\0") if path
        and git(worktree, "ls-files", "--error-unmatch", "--", path,
                check=False).returncode == 0
    ))


def rebase_planning_worktree(
    git, repo_path: Path, state_dir: Path, repo: str,
    worktree: Path, old_base: str, remote_base: str,
) -> PlanningRebaseResult:
    """Move a saved Planner result, preserving the index and untracked files.

    The caller proves canonical worktree ownership and that ``remote_base`` is
    the fetched origin/base commit. A conflict is left intact for the Planner
    correction boundary; no reset or stash drop occurs on failure.
    """
    path = _journal_path(state_dir, repo, worktree)
    record = _read(path, repo, worktree)
    if record is None:
        if old_base == remote_base:
            return PlanningRebaseResult(old_base, False)
        if git(repo_path, "merge-base", "--is-ancestor", old_base, remote_base, check=False).returncode:
            raise PlanningRebaseError("원격 base가 전진하지 않았거나 재작성됐습니다.")
        head = _git_ok(git, worktree, "rev-parse", "HEAD")
        if git(worktree, "merge-base", "--is-ancestor", old_base, head, check=False).returncode:
            raise PlanningRebaseError("planning 후보 HEAD가 저장된 base의 후손이 아닙니다.")
        count = int(_git_ok(git, worktree, "rev-list", "--count", f"{old_base}..{head}"))
        if count > 1:
            raise PlanningRebaseError("planning 후보에 예상 밖의 추가 commit이 있습니다.")
        record = {
            "repo": repo, "worktree": str(worktree.resolve()), "old": old_base,
            "target": remote_base, "head": head, "nonce": uuid4().hex,
            "dirty": bool(_git_ok(git, worktree, "status", "--porcelain", "--untracked-files=all")),
            "phase": "before_stash",
        }
        _write(path, record)
    elif old_base not in {record["old"], record["target"]}:
        raise PlanningRebaseError("planning rebase journal과 저장된 base가 다릅니다.")

    target = str(record["target"])
    if (git(repo_path, "merge-base", "--is-ancestor", str(record["old"]), target,
            check=False).returncode
            or git(repo_path, "merge-base", "--is-ancestor", target, remote_base,
                   check=False).returncode):
        raise PlanningRebaseError("planning rebase 대상과 현재 원격의 전진 관계가 다릅니다.")
    message = "loop-manager-planning-rebase:" + str(record["nonce"])
    phase = str(record["phase"])
    stash = _stash(git, worktree, message)
    if phase == "before_stash":
        if record["dirty"] and stash is None:
            if not _git_ok(git, worktree, "status", "--porcelain", "--untracked-files=all"):
                raise PlanningRebaseError("planning 변경물이 사라졌고 보존 stash를 찾을 수 없습니다.")
            _git_ok(git, worktree, "stash", "push", "--include-untracked", "-m", message)
            stash = _stash(git, worktree, message)
            if stash is None:
                raise PlanningRebaseError("planning 변경물 stash를 확인할 수 없습니다.")
        record["phase"] = phase = "stashed"
        _write(path, record)
    if record["dirty"] and stash is None and phase != "restored":
        raise PlanningRebaseError("planning 변경물의 보존 stash가 없습니다.")

    if phase == "accepting":
        accept_resolved_stash_conflict(git, state_dir, repo, worktree, target)
        return PlanningRebaseResult(target, True)

    just_started_apply = False
    if phase in {"stashed", "rebasing"}:
        if rebase_in_progress(git, worktree):
            return PlanningRebaseResult(target, True, True, "planning rebase 충돌 해결을 기다립니다.")
        head = _git_ok(git, worktree, "rev-parse", "HEAD")
        if head != target and git(worktree, "merge-base", "--is-ancestor", target, head, check=False).returncode:
            record["phase"] = "rebasing"
            _write(path, record)
            if record["head"] == record["old"]:
                result = git(worktree, "switch", "--detach", target, check=False)
            else:
                result = git(worktree, "rebase", "--onto", target, str(record["old"]), check=False)
            if result.returncode:
                return PlanningRebaseResult(target, True, True, result.stdout[-2000:])
            head = _git_ok(git, worktree, "rev-parse", "HEAD")
        if git(worktree, "merge-base", "--is-ancestor", target, head, check=False).returncode:
            raise PlanningRebaseError("planning rebase 완료 HEAD가 대상 base를 포함하지 않습니다.")
        # Restore the stash on the rebased commit first: its index is relative
        # to the prepared commit, not the remote base. Keep that identity for
        # recovery after apply or reset completes immediately before a crash.
        record.pop("correction", None)
        record["restored_head"] = head
        record["phase"] = phase = "applying"
        _write(path, record)
        just_started_apply = True

    if phase == "conflict" and stash is not None and not _git_ok(
        git, worktree, "status", "--porcelain", "--untracked-files=all",
    ):
        # Recover journals written by an interrupted/rejected index restore
        # that never produced unmerged entries or worktree changes.
        record["phase"] = phase = "applying"
        _write(path, record)
        just_started_apply = True

    if phase == "applying":
        if stash is not None:
            # A crash after an apply begins cannot safely replay it over a
            # partially restored index. Keep both the checkout and stash.
            if not just_started_apply and _git_ok(git, worktree, "status", "--porcelain", "--untracked-files=all"):
                matches = _matches_completed_stash_apply(
                    git, repo_path, state_dir, worktree, target,
                    str(record.get("restored_head", target)), stash[1],
                )
                if matches is None:
                    collisions = _stash_untracked_collisions(git, worktree, stash[1])
                    if collisions or git(worktree, "ls-files", "-u", "-z").stdout:
                        record["phase"] = "conflict"
                        _write(path, record)
                        return PlanningRebaseResult(
                            target, True, True, "중단된 stash 복원 충돌을 재개합니다.",
                            collisions, stash[1],
                        )
                    raise PlanningRebaseError("보존 stash의 예상 복원 상태를 재현할 수 없습니다.")
                if not matches:
                    return PlanningRebaseResult(target, True, True, "stash 복원 상태가 보존된 변경물과 다릅니다.")
                if _git_ok(git, worktree, "rev-parse", "HEAD") != target:
                    _git_ok(git, worktree, "reset", "--soft", target)
                record["phase"] = "restored"
                record["restored_digest"] = _restored_digest(git, worktree)
                _write(path, record)
                return PlanningRebaseResult(target, True)
            applied = _apply_stash(git, worktree, stash[1])
            if applied.returncode:
                record["phase"] = "conflict"
                _write(path, record)
                return PlanningRebaseResult(
                    target, True, True, applied.stdout[-2000:],
                    _stash_untracked_collisions(git, worktree, stash[1]),
                    stash[1],
                )
        if _git_ok(git, worktree, "rev-parse", "HEAD") != target:
            _git_ok(git, worktree, "reset", "--soft", target)
        record["restored_digest"] = _restored_digest(git, worktree)
        record["phase"] = "restored"
        _write(path, record)
    if record["phase"] == "conflict":
        return PlanningRebaseResult(
            target, True, True, "planning 변경물 복원 충돌 해결을 기다립니다.",
            _stash_untracked_collisions(git, worktree, stash[1]) if stash else (),
            stash[1] if stash else "",
        )
    if record.get("restored_digest") != _restored_digest(git, worktree):
        raise PlanningRebaseError("보존 stash 복원 후 planning 작업물이 달라졌습니다.")
    return PlanningRebaseResult(target, True)


def _correction_digest(git, worktree: Path, paths: tuple[str, ...]) -> str:
    # Index stages change during "add"; bind the approved worktree bytes and
    # all other index changes instead. Include deletions and untracked output.
    names = set(paths)
    for args in (("diff", "--name-only", "HEAD", "-z"),
                 ("ls-files", "--others", "--exclude-standard", "-z")):
        names.update(filter(None, _git_ok(git, worktree, *args).split("\0")))
    files = []
    for name in sorted(names):
        file = worktree / name
        if file.is_symlink():
            value = ("link", os.readlink(file))
        elif file.is_file():
            value = ("file", file.read_bytes(), file.stat().st_mode & 0o111)
        elif not file.exists():
            value = ("deleted",)
        else:
            raise PlanningRebaseError(f"planning 교정 파일 형식이 유효하지 않습니다: {name}")
        files.append((name, value))
    other_index = _git_ok(
        git, worktree, "diff", "--cached", "--binary", "HEAD", "--", ".",
        *(f":(literal,exclude){name}" for name in paths),
    )
    return hashlib.sha256(repr((files, other_index)).encode()).hexdigest()


def save_planning_correction(
    git, state_dir: Path, repo: str, worktree: Path, target: str,
    paths: tuple[str, ...], was_rebase: bool,
) -> None:
    """Persist a scope-checked correction before Manager stages its paths."""
    path = _journal_path(state_dir, repo, worktree)
    record = _read(path, repo, worktree)
    if record is None or record["target"] != target or record["phase"] not in {"rebasing", "conflict"}:
        raise PlanningRebaseError("planning 교정을 저장할 rebase 단계가 다릅니다.")
    record["correction"] = {
        "paths": list(paths), "was_rebase": was_rebase,
        "head": _git_ok(git, worktree, "rev-parse", "HEAD"),
        "digest": _correction_digest(git, worktree, paths),
    }
    _write(path, record)


def planning_correction(
    git, state_dir: Path, repo: str, worktree: Path, target: str,
) -> tuple[tuple[str, ...], bool] | None:
    """Resume only the unchanged correction whose Agent scope already passed."""
    record = _read(_journal_path(state_dir, repo, worktree), repo, worktree)
    if record is None or "correction" not in record:
        return None
    saved = record["correction"]
    if (record["target"] != target or not isinstance(saved, dict)
            or not isinstance(saved.get("paths"), list)
            or not isinstance(saved.get("was_rebase"), bool)
            or any(not isinstance(name, str) or (
                name != "docs/FUTURE_IDEAS.md"
                and re.fullmatch(r"docs/plans/[^/]+\.md", name) is None
            ) for name in saved["paths"])):
        raise PlanningRebaseError("planning 교정 기록의 대상·경로가 유효하지 않습니다.")
    if saved.get("head") != _git_ok(git, worktree, "rev-parse", "HEAD"):
        # A continued rebase can reach another commit's conflict. Its correction
        # must come from the Planner, not the preceding commit's record.
        if saved["was_rebase"] and record["phase"] == "rebasing":
            return None
        raise PlanningRebaseError("planning 교정 HEAD가 바뀌었습니다.")
    paths = tuple(saved["paths"])
    if saved.get("digest") != _correction_digest(git, worktree, paths):
        raise PlanningRebaseError("검증된 planning 교정 후 작업물이 달라졌습니다.")
    return paths, saved["was_rebase"]


def complete_planning_rebase(git, state_dir: Path, repo: str, worktree: Path, target: str) -> None:
    """Release preserved stash only after SQLite records the adopted base."""
    path = _journal_path(state_dir, repo, worktree)
    record = _read(path, repo, worktree)
    if record is None:
        return
    if record["phase"] != "restored" or record["target"] != target:
        raise PlanningRebaseError("완료할 planning rebase 단계 또는 대상이 다릅니다.")
    if record.get("restored_digest") != _restored_digest(git, worktree):
        raise PlanningRebaseError("planning rebase 완료 전 작업물이 달라졌습니다.")
    message = "loop-manager-planning-rebase:" + str(record["nonce"])
    stash = _stash(git, worktree, message)
    if record["dirty"] and stash is not None:
        _git_ok(git, worktree, "stash", "drop", stash[0])
    path.unlink()


def accept_resolved_stash_conflict(
    git, state_dir: Path, repo: str, worktree: Path, target: str,
) -> None:
    """Adopt an explicitly corrected stash conflict after Git reports no unmerged paths."""
    path = _journal_path(state_dir, repo, worktree)
    record = _read(path, repo, worktree)
    if record is None or record["phase"] not in {"conflict", "accepting"} or record["target"] != target:
        raise PlanningRebaseError("해결할 planning stash 충돌 단계가 없습니다.")
    if git(worktree, "ls-files", "-u", "-z").stdout:
        raise PlanningRebaseError("planning stash 충돌이 아직 index에 남아 있습니다.")
    head = _git_ok(git, worktree, "rev-parse", "HEAD")
    restored_head = str(record.get("restored_head", target))
    if record["phase"] == "conflict":
        if head != restored_head:
            raise PlanningRebaseError("planning stash 충돌의 base HEAD가 바뀌었습니다.")
        if not _git_ok(git, worktree, "status", "--porcelain", "--untracked-files=all"):
            raise PlanningRebaseError("해결된 planning 후보 변경물이 없습니다.")
        # Bind index/worktree bytes to the pre-reset commit, so the identity
        # remains valid on either side of the soft-reset crash window.
        record["accepting_digest"] = _restored_digest(git, worktree, restored_head)
        record["phase"] = "accepting"
        _write(path, record)
    elif (head not in {restored_head, target}
          or record.get("accepting_digest") != _restored_digest(git, worktree, restored_head)):
        raise PlanningRebaseError("planning stash 교정 확정 중 작업물이 달라졌습니다.")
    if head != target:
        _git_ok(git, worktree, "reset", "--soft", target)
    record["restored_digest"] = _restored_digest(git, worktree)
    record["phase"] = "restored"
    _write(path, record)
