"""Git subprocess effects with ignored-path and lock guards."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.runtime.contracts import ManagedWorktreeConflictError
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class GitEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _ignored_paths_overlapping_git_update: Callable[..., object]
    _rebase_resume_ignored_overlap: Callable[..., object]
    command: Callable[..., object]
    is_git_lock_failure: Callable[..., object]

@dataclass(frozen=True)
class GitBytesEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _handoff_run: Callable[..., object]
    is_git_lock_failure: Callable[..., object]


def git(effects: GitEffects,
    repo: Path, *arguments: str, check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_index = 0
    while command_index < len(arguments) and arguments[command_index] == "-c":
        command_index += 2
    if arguments[command_index : command_index + 1] == ("rebase",):
        rebase_arguments = arguments[command_index + 1:]
        overlap_error = None
        if rebase_arguments and rebase_arguments[-1] in {
            "--continue", "--skip", "--abort"
        }:
            overlap_error = effects._rebase_resume_ignored_overlap(repo, rebase_arguments[-1])
        elif rebase_arguments and not rebase_arguments[-1].startswith("-"):
            overlaps = effects._ignored_paths_overlapping_git_update(
                repo, rebase_arguments[-1]
            )
            if overlaps:
                overlap_error = (
                    "rebase 대상 tracked 경로와 겹치는 ignored 파일: "
                    + ", ".join(overlaps[:8])
                )
        if overlap_error:
            if check:
                raise ManagedWorktreeConflictError(overlap_error)
            return subprocess.CompletedProcess(
                ["git", "-C", str(repo), *arguments], 1, overlap_error
            )
    result = effects.command(
        ["git", "-C", str(repo), *arguments], check=False,
        **({"env": env} if env is not None else {}),
    )
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        message = f"명령 실패 (git -C {repo}):\n{output}"
        if effects.is_git_lock_failure(output):
            raise GitLockError(message)
        raise RuntimeError(message)
    return result


def git_bytes(effects: GitBytesEffects, repo: Path, *arguments: str) -> bytes:
    result = effects._handoff_run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        output = result.stdout[-6000:].decode("utf-8", errors="replace").strip()
        message = f"명령 실패 (git -C {repo}):\n{output}"
        if effects.is_git_lock_failure(output):
            raise GitLockError(message)
        raise RuntimeError(message)
    return result.stdout


class ExternalObservationError(RuntimeError):
    """A read failed before absence or an ownership decision could be made."""


def revision_path_exists(git: Callable[..., object], repo: Path,
                         revision: str, path: str) -> bool:
    """Require a successful exact tree query before classifying a path."""
    try:
        tree = git(repo, "ls-tree", "-z", "--full-tree", revision, "--", path)
    except GitLockError:
        raise
    except (RuntimeError, OSError) as error:
        raise ExternalObservationError(f"Git revision tree 조회에 실패했습니다: {path}") from error
    if tree.returncode:
        raise ExternalObservationError(f"Git revision tree 조회에 실패했습니다: {path}")
    records = [record for record in tree.stdout.split("\0") if record]
    if any(record.partition("\t")[2] != path for record in records):
        raise ExternalObservationError(f"Git tree 경로 관측이 유효하지 않습니다: {path}")
    return bool(records)


def revision_document_or_none(git: Callable[..., object], repo: Path,
                              revision: str, path: str) -> str | None:
    """Read a revision path; only a successful tree query can prove absence."""
    try:
        shown = git(repo, "show", f"{revision}:{path}")
    except GitLockError:
        raise
    except (RuntimeError, OSError) as error:
        if not revision_path_exists(git, repo, revision, path):
            return None
        raise ExternalObservationError(f"Git revision 문서 조회에 실패했습니다: {path}") from error
    if shown.returncode == 0:
        return shown.stdout
    if not revision_path_exists(git, repo, revision, path):
        return None
    raise ExternalObservationError(f"Git revision 문서 조회에 실패했습니다: {path}")
