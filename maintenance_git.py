"""Bounded, read-only Git context for maintenance review planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import subprocess
import tempfile


MAX_CHANGE_COMMITS = 50
MAX_CHANGE_CONTEXT_BYTES = 32_768
MAX_COMMIT_SUBJECT_BYTES = 512


@dataclass(frozen=True)
class ChangedCommit:
    sha: str
    subject: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class GitChangeContext:
    baseline_commit: str
    current_commit: str
    commits: tuple[ChangedCommit, ...]
    truncated: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _bounded_git_output(
    repo: Path, limit: int, *arguments: str
) -> tuple[subprocess.CompletedProcess[str], bool]:
    command = ["git", "-C", str(repo), *arguments]
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        completed = subprocess.run(
            command,
            check=False,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        stdout_file.seek(0)
        raw_output = stdout_file.read(limit + 1)
        stderr_file.seek(0)
        raw_error = stderr_file.read(MAX_CHANGE_CONTEXT_BYTES + 1)
    overflow = len(raw_output) > limit
    output = raw_output[:limit].decode("utf-8", errors="replace")
    error = raw_error[:MAX_CHANGE_CONTEXT_BYTES].decode(
        "utf-8", errors="replace"
    )
    return (
        subprocess.CompletedProcess(command, completed.returncode, output, error),
        overflow,
    )


def _context_error(
    baseline: str, current: str, detail: str
) -> GitChangeContext:
    return GitChangeContext(baseline, current, (), False, detail)


def _normalized_path(raw: str) -> str | None:
    candidate = raw.replace("\\", "/")
    path = PurePosixPath(candidate)
    if (
        not candidate
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None
    return path.as_posix()


def bounded_remote_change_context(
    repo: Path,
    base_branch: str,
    baseline_commit: str,
    current_commit: str,
) -> GitChangeContext:
    """Return deterministic, bounded commit/path context from the remote range."""

    remote_ref = f"refs/remotes/origin/{base_branch}"
    remote = _git(
        repo, "rev-parse", "--verify", f"{remote_ref}^{{commit}}"
    )
    if remote.returncode or remote.stdout.strip() != current_commit:
        return _context_error(
            baseline_commit,
            current_commit,
            f"현재 원격 기준 commit을 확인할 수 없습니다: origin/{base_branch}",
        )
    baseline = _git(
        repo, "cat-file", "-e", f"{baseline_commit}^{{commit}}"
    )
    if baseline.returncode:
        return _context_error(
            baseline_commit,
            current_commit,
            "직전 성공 평가 commit이 원격 이력에 없습니다.",
        )
    ancestor = _git(
        repo, "merge-base", "--is-ancestor", baseline_commit, current_commit
    )
    if ancestor.returncode:
        return _context_error(
            baseline_commit,
            current_commit,
            "직전 성공 평가 commit이 현재 원격 이력의 조상이 아닙니다.",
        )
    listed = _git(
        repo,
        "rev-list",
        "--topo-order",
        f"--max-count={MAX_CHANGE_COMMITS + 1}",
        f"{baseline_commit}..{current_commit}",
    )
    if listed.returncode:
        return _context_error(
            baseline_commit,
            current_commit,
            "원격 변경 commit 범위를 안전하게 읽을 수 없습니다.",
        )
    newest_first = [line for line in listed.stdout.splitlines() if line]
    truncated = len(newest_first) > MAX_CHANGE_COMMITS
    selected = list(reversed(newest_first[:MAX_CHANGE_COMMITS]))
    commits: list[ChangedCommit] = []
    used_bytes = 0
    for sha in selected:
        subject_result, subject_overflow = _bounded_git_output(
            repo,
            MAX_COMMIT_SUBJECT_BYTES,
            "show",
            "-s",
            "--format=%s",
            sha,
        )
        remaining = max(MAX_CHANGE_CONTEXT_BYTES - used_bytes, 0)
        paths_result, paths_overflow = _bounded_git_output(
            repo,
            remaining,
            "diff-tree",
            "--root",
            "-m",
            "--no-commit-id",
            "--name-only",
            "-z",
            "-r",
            sha,
        )
        if (
            subject_result.returncode
            or paths_result.returncode
            or subject_result.stderr.strip()
            or paths_result.stderr.strip()
        ):
            return _context_error(
                baseline_commit,
                current_commit,
                "원격 변경 commit 내용을 안전하게 읽을 수 없습니다.",
            )
        subject = subject_result.stdout.rstrip("\n")
        truncated = truncated or subject_overflow or paths_overflow
        raw_paths = paths_result.stdout.split("\0")
        if paths_overflow and raw_paths:
            raw_paths.pop()
        normalized_paths = sorted(
            {
                normalized
                for raw in raw_paths
                if (normalized := _normalized_path(raw)) is not None
            }
        )
        bounded_paths: list[str] = []
        base_size = len(sha.encode("utf-8")) + len(subject.encode("utf-8"))
        if used_bytes + base_size > MAX_CHANGE_CONTEXT_BYTES:
            truncated = True
            break
        used_bytes += base_size
        for path in normalized_paths:
            path_size = len(path.encode("utf-8"))
            if used_bytes + path_size > MAX_CHANGE_CONTEXT_BYTES:
                truncated = True
                break
            bounded_paths.append(path)
            used_bytes += path_size
        commits.append(ChangedCommit(sha, subject, tuple(bounded_paths)))
        if len(bounded_paths) != len(normalized_paths):
            break
    return GitChangeContext(
        baseline_commit,
        current_commit,
        tuple(commits),
        truncated,
        None,
    )
