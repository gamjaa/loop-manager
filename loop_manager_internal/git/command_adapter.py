"""Git subprocess effects with ignored-path and lock guards."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.runtime.handoff_gate import _handoff_popen
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
    input: str | None = None,
    before_commit: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_index = 0
    while command_index < len(arguments) and arguments[command_index].startswith('-'):
        command_index += 2 if arguments[command_index] == '-c' else 1
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
    # Manager fetch가 origin/HEAD를 자동 생성해 Agent scope에 섞지 않도록 한다.
    options = ("-c", "remote.origin.followRemoteHEAD=never") if (
        arguments[command_index:command_index + 1] == ("fetch",)
    ) else ()
    from loop_manager_internal.runtime.resource_lock import git_command_turn
    command = arguments[command_index] if command_index < len(arguments) else ''
    with git_command_turn(repo, command, arguments[command_index + 1:]):
        result = effects.command(
            ["git", "-C", str(repo), *options, *arguments], check=False,
            **({"env": env} if env is not None else {}),
            **({"input": input} if input is not None else {}),
            **({"before_commit": before_commit} if before_commit is not None else {}),
        )
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        message = f"명령 실패 (git -C {repo}):\n{output}"
        if effects.is_git_lock_failure(output):
            raise GitLockError(message)
        raise RuntimeError(message)
    return result


def commit_ref_transaction(arguments, *, input, before_commit, cwd=None, env=None):
    """Keep Git's exact ref lock from prepare through the caller's raw-ref check.

    EOF aborts a started transaction. Never send commit after a failed check.
    Binary pipes preserve LF on Windows as well as UTF-8 ref names.
    """
    with _handoff_popen(arguments, cwd=cwd, env=env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        try:
            process.stdin.write(input.encode("utf-8"))
            process.stdin.flush()
            output = process.stdout.readline().replace(b"\r\n", b"\n")
            if output == b"start: ok\n":
                output += process.stdout.readline().replace(b"\r\n", b"\n")
            if output != b"start: ok\nprepare: ok\n":
                remainder, _ = process.communicate()
                return subprocess.CompletedProcess(
                    arguments, process.returncode or 1,
                    (output + remainder).decode("utf-8", errors="replace"))
            before_commit()
            remainder, _ = process.communicate(b"commit\n")
            output += remainder
        except BaseException:
            # Closing stdin aborts even a prepared transaction and releases its
            # lock. Reap the child before returning the original failure.
            process.communicate()
            raise
    return subprocess.CompletedProcess(arguments, process.returncode,
                                       output.decode("utf-8", errors="replace"))


def git_bytes(effects: GitBytesEffects, repo: Path, *arguments: str) -> bytes:
    # Disable diff's index writes; scope observation verifies file contents
    # with numstat rather than accepting stale stat-only name matches.
    options = ('-c', 'diff.autoRefreshIndex=false') if arguments[:1] == ('diff',) else ()
    result = effects._handoff_run(
        ["git", "-C", str(repo), *options, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        output = (result.stdout + (result.stderr or b""))[-6000:].decode("utf-8", errors="replace").strip()
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
    if path.startswith("manager:"):
        from loop_manager_internal.catalog.manager_sources import source_content
        return source_content(path)
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


def revision_requirement_documents(
    git_bytes: Callable[..., bytes], repo: Path, revision: str,
) -> dict[str, bytes]:
    """Read direct regular Requirement documents from one pinned Git tree."""
    try:
        tree = git_bytes(
            repo, "ls-tree", "-r", "-z", "--full-tree", revision,
            "--", "docs/requirements",
        )
    except GitLockError:
        raise
    except (RuntimeError, OSError) as error:
        raise ExternalObservationError(
            f"Git revision Requirement tree 조회에 실패했습니다: {revision}"
        ) from error

    def read_blob(object_id: str) -> bytes:
        try:
            return git_bytes(repo, "cat-file", "blob", object_id)
        except GitLockError:
            raise
        except (RuntimeError, OSError) as error:
            raise ExternalObservationError(
                f"Git revision Requirement blob을 읽을 수 없습니다: {object_id}"
            ) from error

    return requirement_documents_from_tree(tree, read_blob)


def requirement_documents_from_tree(
    tree: bytes, read_blob: Callable[[str], bytes],
) -> dict[str, bytes]:
    """Read the eligible Requirement blobs from an already observed Git tree."""
    from loop_manager_internal.catalog.requirement_sources import (
        RequirementSourceLookupError,
    )

    documents: dict[str, bytes] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise ExternalObservationError("Git Requirement tree 항목 형식이 올바르지 않습니다.")
        if not raw_path.startswith(b"docs/requirements/") or not raw_path.endswith(b".md"):
            continue
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExternalObservationError(
                "Git Requirement 경로가 UTF-8이 아닙니다."
            ) from error
        if not (
            path.count("/") == 2
            or path.startswith("docs/requirements/archive/")
            and path.count("/") == 3
        ):
            continue
        mode, object_type, object_id = fields
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise RequirementSourceLookupError(path, "일반 파일이 아닙니다")
        try:
            contents = read_blob(object_id.decode("ascii"))
        except GitLockError:
            raise
        except (RuntimeError, OSError) as error:
            raise RequirementSourceLookupError(path, "Git blob을 읽을 수 없습니다") from error
        documents[path] = contents
    return documents
