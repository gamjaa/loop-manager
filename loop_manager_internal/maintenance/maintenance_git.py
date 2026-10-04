"""Bounded, read-only Git context for maintenance review planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import subprocess
import threading


MAX_CHANGE_COMMITS = 50
MAX_CHANGE_CONTEXT_BYTES = 32_768
MAX_COMMIT_SUBJECT_BYTES = 512
MAX_GIT_ERROR_BYTES = 4_096
GIT_PIPE_READ_BYTES = 8_192


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
    observation_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo), *arguments]
    try:
        return subprocess.run(
            command, check=False, text=True, encoding="utf-8",
            errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError:
        return subprocess.CompletedProcess(command, 127, "")


def _bounded_git_output(
    repo: Path, limit: int, *arguments: str
) -> tuple[subprocess.CompletedProcess[str], bool]:
    command = ["git", "-C", str(repo), *arguments]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        return (
            subprocess.CompletedProcess(
                command,
                127,
                "",
                "Git 명령을 시작할 수 없습니다.",
            ),
            False,
        )
    assert process.stdout is not None
    assert process.stderr is not None
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    stdout_overflow = threading.Event()
    read_failed = threading.Event()

    def drain(name: str, stream: object, capture_limit: int) -> None:
        try:
            while True:
                chunk = stream.read(GIT_PIPE_READ_BYTES)
                if not chunk:
                    break
                remaining = capture_limit - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if name == "stdout" and len(chunk) > max(remaining, 0):
                    stdout_overflow.set()
        except OSError:
            read_failed.set()
        finally:
            stream.close()

    stdout_thread = threading.Thread(
        target=drain,
        args=("stdout", process.stdout, limit),
        name="maintenance-git-stdout",
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=("stderr", process.stderr, MAX_GIT_ERROR_BYTES),
        name="maintenance-git-stderr",
    )
    stdout_thread.start()
    stderr_thread.start()
    returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    if read_failed.is_set() and returncode == 0:
        returncode = 1
        captured["stderr"] = bytearray(
            "Git 출력 스트림을 안전하게 읽을 수 없습니다.".encode("utf-8")
        )
    output = bytes(captured["stdout"]).decode("utf-8", errors="ignore")
    error = bytes(captured["stderr"]).decode("utf-8", errors="ignore")
    return (
        subprocess.CompletedProcess(command, returncode, output, error),
        stdout_overflow.is_set(),
    )


def _context_error(
    baseline: str, current: str, detail: str, *, observation_failed: bool = False,
) -> GitChangeContext:
    return GitChangeContext(baseline, current, (), False, detail, observation_failed)


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
        missing_remote = False
        if remote.returncode:
            missing_remote = _git(
                repo, "show-ref", "--verify", "--quiet", remote_ref
            ).returncode == 1
        return _context_error(
            baseline_commit,
            current_commit,
            f"현재 원격 기준 commit을 확인할 수 없습니다: origin/{base_branch}",
            observation_failed=bool(remote.returncode) and not missing_remote,
        )
    baseline = _git(
        repo, "cat-file", "-e", f"{baseline_commit}^{{commit}}"
    )
    if baseline.returncode:
        return _context_error(
            baseline_commit,
            current_commit,
            "직전 성공 평가 commit이 원격 이력에 없습니다.",
            observation_failed=baseline.returncode != 1,
        )
    ancestor = _git(
        repo, "merge-base", "--is-ancestor", baseline_commit, current_commit
    )
    if ancestor.returncode:
        return _context_error(
            baseline_commit,
            current_commit,
            "직전 성공 평가 commit이 현재 원격 이력의 조상이 아닙니다.",
            observation_failed=ancestor.returncode != 1,
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
            observation_failed=True,
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
                observation_failed=True,
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
