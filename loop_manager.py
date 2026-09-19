#!/usr/bin/env python3
"""Codex가 Loop Manager의 다음 PR을 만들게 하는 최소 부트스트랩 실행기."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import configparser
import contextlib
import difflib
import errno
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
MAX_PLANNER_VALIDATION_RETRIES = 3
MAX_VALIDATION_FINGERPRINT_REPEATS = 3
MAX_TRANSIENT_AGENT_RETRIES = 3
AGENT_CLI_TIMEOUT_SECONDS = 3600
PROJECT_STEP_RETRY = 2
PROJECT_STEP_TRANSIENT_PAUSE = 3
PROJECT_STEP_USAGE_PAUSE = 4
PROJECT_STEP_PLANNING_PENDING = 5
TRANSIENT_AGENT_ERROR_PREFIX = "일시적 Agent 실패 (상태 보존):"
USAGE_LIMIT_ERROR_PREFIX = "AI 사용 한도 소진 (상태 보존):"
PLAN_SHA_BACKFILL_ERROR = "계획 commit SHA를 기존 작업에서 안전하게 복구할 수 없습니다."
WORKSPACE_UNOWNED = 0
WORKSPACE_OWNED = 1
WORKSPACE_RESERVED = 2
REBASE_IN_PROGRESS_ERROR = "Manager rebase 진행 중"
REBASE_CONFLICT_PREFIX = "rebase 충돌 해결 필요:\n"
PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}
ACTIVE_EXECPLAN_STATUSES = frozenset(
    {"IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING", "PR_OPEN", "BLOCKED"}
)
FIELDS = {
    "base_branch",
    "processed_sha",
    "planning_fingerprint",
    "planning_attempt_fingerprint",
    "planning_workspace_owned",
    "planning_retry_count",
    "planner_session_id",
    "worker_session_id",
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
    "planning_validation_error",
    "planning_result_pending_validation",
    "planning_result_completed",
    "planning_error",
    "planning_error_fingerprint",
    "active_sha",
    "plan_sha",
    "phase",
    "branch",
    "worktree",
    "plan_path",
    "pr_url",
    "last_error",
    "agent_role",
    "agent_snapshot",
    "agent_input_fingerprint",
    "agent_baseline",
    "agent_recovery",
    "agent_input_plan",
    "agent_result_plan",
    "agent_result_snapshot",
    "agent_manager_snapshot",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    repo TEXT PRIMARY KEY,
    base_branch TEXT NOT NULL,
    processed_sha TEXT,
    planning_fingerprint TEXT,
    planning_attempt_fingerprint TEXT,
    planning_workspace_owned INTEGER NOT NULL DEFAULT 0,
    planning_retry_count INTEGER NOT NULL DEFAULT 0,
    planner_session_id TEXT,
    worker_session_id TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    planning_validation_error TEXT,
    planning_result_pending_validation INTEGER NOT NULL DEFAULT 0,
    planning_result_completed INTEGER NOT NULL DEFAULT 0,
    planning_error TEXT,
    planning_error_fingerprint TEXT,
    active_sha TEXT,
    plan_sha TEXT,
    phase TEXT,
    branch TEXT,
    worktree TEXT,
    plan_path TEXT,
    pr_url TEXT,
    last_error TEXT,
    agent_role TEXT,
    agent_snapshot TEXT,
    agent_input_fingerprint TEXT,
    agent_baseline TEXT,
    agent_recovery TEXT,
    agent_input_plan TEXT,
    agent_result_plan TEXT,
    agent_result_snapshot TEXT,
    agent_manager_snapshot TEXT
)
"""

CLEANUP_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup_pending (
    repo TEXT NOT NULL,
    worktree TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    pr_head_oid TEXT,
    reason TEXT NOT NULL,
    workspace_owned INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, worktree),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

EXEC_PLANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_plans (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    sources TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    priority TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_sha TEXT,
    active_sha TEXT,
    branch TEXT,
    worktree TEXT,
    pr_url TEXT,
    last_error TEXT,
    blocked_fingerprint TEXT,
    blocked_worktree_fingerprint TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    worker_session_id TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    verify_failure_history TEXT NOT NULL DEFAULT '{}',
    review_failure_history TEXT NOT NULL DEFAULT '{}',
    workspace_owned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, plan_id),
    UNIQUE (repo, path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

EXEC_PLANS_ACTIVE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execplan_per_project
ON exec_plans(repo)
WHERE status IN ('IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING', 'PR_OPEN', 'BLOCKED')
"""


class AgentResultKind(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient_failure"
    USAGE_LIMIT = "usage_limit"
    PERMANENT_CONFIGURATION = "permanent_configuration"


class ProjectRunKind(str, Enum):
    COMPLETED = "completed"
    WAITING = "waiting"
    BLOCKED = "blocked"
    TRANSIENT_PAUSE = "transient_pause"
    USAGE_LIMIT_PAUSE = "usage_limit_pause"
    OPERATIONAL_ERROR = "operational_error"


@dataclass(frozen=True)
class ProjectRunResult:
    kind: ProjectRunKind
    exit_code: int
    detail: str | None = None


@dataclass(frozen=True)
class CodexResult:
    code: int
    message: str
    session_id: str | None = None
    kind: AgentResultKind | None = None

    def __iter__(self):
        # Keep two-value unpacking compatible with callers that do not manage sessions.
        return iter((self.code, self.message))

    @property
    def result_kind(self) -> AgentResultKind:
        return self.kind or classify_agent_result(self.code, self.message)


@dataclass(frozen=True)
class ValidationFailureDecision:
    stage: str
    fingerprint: str
    occurrences: int
    blocked: bool


@dataclass(frozen=True)
class CleanupAssessment:
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class WorkerPublication:
    """The Worker-provided metadata used for its commit and pull request."""

    commit_title: str
    summary: str
    test_evidence: str

@dataclass(frozen=True)
class GitSnapshot:
    """Content-free Git state captured across one Agent invocation."""

    head: str
    branch: str | None
    index: dict[str, tuple[str, ...]]
    dirty: dict[str, tuple[tuple[str, ...], str]]
    refs: dict[str, tuple[str, str]]
    worktrees: dict[str, tuple[str, ...]]
    config: dict[str, tuple[str, ...]]
    operations: dict[str, str]
    shared_metadata_complete: bool = True

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": 4,
                "head": self.head,
                "branch": self.branch,
                "index": {path: list(entries) for path, entries in self.index.items()},
                "dirty": {
                    path: [list(kinds), signature]
                    for path, (kinds, signature) in self.dirty.items()
                },
                "refs": {name: list(state) for name, state in self.refs.items()},
                "worktrees": {
                    path: list(state) for path, state in self.worktrees.items()
                },
                "config": {
                    key: list(values) for key, values in self.config.items()
                },
                "operations": self.operations,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def deserialize(cls, value: str) -> GitSnapshot:
        parsed = json.loads(value)
        version = parsed.get("version")
        if version not in {1, 2, 3, 4}:
            raise RuntimeError("저장된 Agent Git snapshot 형식을 지원하지 않습니다.")
        return cls(
            head=str(parsed["head"]),
            branch=(str(parsed["branch"]) if parsed["branch"] is not None else None),
            index={
                str(path): tuple(str(entry) for entry in entries)
                for path, entries in parsed["index"].items()
            },
            dirty={
                str(path): (
                    tuple(str(kind) for kind in state[0]),
                    str(state[1]),
                )
                for path, state in parsed["dirty"].items()
            },
            refs={
                str(name): (str(state[0]), str(state[1]))
                for name, state in parsed.get("refs", {}).items()
            },
            worktrees={
                str(path): tuple(str(entry) for entry in state)
                for path, state in parsed.get("worktrees", {}).items()
            },
            config={
                str(key): tuple(str(value) for value in values)
                for key, values in parsed.get("config", {}).items()
            },
            operations={
                str(name): str(signature)
                for name, signature in parsed.get("operations", {}).items()
            },
            shared_metadata_complete=version == 4,
        )


@dataclass(frozen=True)
class ManagerGitCheckpoint:
    """Validated Agent state before Manager-owned Git mutations."""

    snapshot: GitSnapshot
    cleanup_worktrees: frozenset[str]

    def serialize(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "snapshot": self.snapshot.serialize(),
                "cleanup_worktrees": sorted(self.cleanup_worktrees),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def deserialize(cls, value: str) -> ManagerGitCheckpoint:
        parsed = json.loads(value)
        if parsed.get("version") != 1:
            raise RuntimeError("저장된 Manager Git checkpoint 형식을 지원하지 않습니다.")
        return cls(
            snapshot=GitSnapshot.deserialize(str(parsed["snapshot"])),
            cleanup_worktrees=frozenset(
                str(path) for path in parsed.get("cleanup_worktrees", [])
            ),
        )


@dataclass(frozen=True)
class AgentChange:
    path: str
    kinds: tuple[str, ...]


class AgentScopeError(RuntimeError):
    """An Agent changed Git state outside its role boundary."""

    def __init__(
        self,
        message: str,
        result_snapshot: GitSnapshot | None = None,
    ) -> None:
        super().__init__(message)
        self.result_snapshot = result_snapshot


class WorkspaceOwnershipError(RuntimeError):
    """A managed worktree cannot be proven to belong to its saved task."""


class WorkerResultError(RuntimeError):
    """A Worker final response cannot safely be published."""


class ReviewerProtocolError(RuntimeError):
    """A failed Reviewer response does not identify actionable issues."""


class ProcessLockBusyError(RuntimeError):
    """The single Loop Manager process lock is already held."""


def classify_agent_result(code: int, message: str) -> AgentResultKind:
    """Classify an Agent CLI result without another Agent call."""

    if code == 0:
        return AgentResultKind.SUCCESS
    normalized = " ".join(message.casefold().split())
    if any(
        phrase in normalized
        for phrase in (
            "usage limit",
            "usage cap",
            "quota exceeded",
            "insufficient_quota",
            "credit balance",
        )
    ):
        return AgentResultKind.USAGE_LIMIT
    if any(
        phrase in normalized
        for phrase in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "service unavailable",
            "connection reset",
            "connection refused",
            "network error",
            "dns error",
            "gateway timeout",
            "internal server error",
            "try again later",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "too many requests",
        )
    ):
        return AgentResultKind.TRANSIENT_FAILURE
    return AgentResultKind.PERMANENT_CONFIGURATION


def session_resume_unavailable(message: str) -> bool:
    normalized = " ".join(message.casefold().split())
    return bool(
        re.search(
            r"(?:session|thread|rollout).{0,80}(?:not found|does not exist|expired)",
            normalized,
        )
    ) or any(
        phrase in normalized
        for phrase in (
            "unknown session",
            "unknown thread",
            "cannot resume",
            "could not resume",
            "unable to resume",
        )
    )


def _coerce_codex_result(
    result: tuple[int, str] | CodexResult,
    session_id: str | None,
) -> CodexResult:
    if isinstance(result, CodexResult):
        return result
    code, message = result
    return CodexResult(code, message, session_id)


def run_agent_with_retries(
    invoke: Callable[[str | None, bool], tuple[int, str] | CodexResult],
    *,
    session_id: str | None,
) -> CodexResult:
    """Retry transient CLI failures and replace an unavailable saved session once."""

    active_session_id = session_id
    transient_retries = 0
    session_fallback = False
    while True:
        try:
            raw_result = invoke(active_session_id, session_fallback)
            result = _coerce_codex_result(raw_result, active_session_id)
        except subprocess.TimeoutExpired as error:
            result = CodexResult(
                1,
                f"Agent CLI timeout: {error}",
                active_session_id,
                AgentResultKind.TRANSIENT_FAILURE,
            )
        except OSError as error:
            result = CodexResult(
                1,
                f"Agent CLI 실행 실패: {error}",
                active_session_id,
                AgentResultKind.TRANSIENT_FAILURE,
            )

        if result.session_id:
            active_session_id = result.session_id
        if result.result_kind is AgentResultKind.SUCCESS:
            return result
        if (
            result.result_kind is not AgentResultKind.USAGE_LIMIT
            and active_session_id is not None
            and not session_fallback
            and session_resume_unavailable(result.message)
        ):
            active_session_id = None
            session_fallback = True
            continue
        if (
            result.result_kind is AgentResultKind.TRANSIENT_FAILURE
            and transient_retries < MAX_TRANSIENT_AGENT_RETRIES
        ):
            transient_retries += 1
            continue
        return result


def local_now() -> datetime:
    """Return the local clock through a patchable boundary for operation logs."""

    return datetime.now()


def operation_log(message: str, *, error: bool = False) -> None:
    """Write timestamped operational output without adding persistent state."""

    prefix = local_now().strftime("%Y-%m-%d %H:%M:%S")
    output = sys.stderr if error else sys.stdout
    for line in str(message).splitlines() or [""]:
        __import__("builtins").print(f"{prefix} {line}", file=output)


def operation_print(*values: object, **kwargs: object) -> None:
    """Route legacy operational output through the shared timestamped boundary."""

    output = kwargs.get("file", sys.stdout)
    separator = str(kwargs.get("sep", " "))
    operation_log(separator.join(str(value) for value in values), error=output is sys.stderr)

def state_directory(value: str | None = None) -> Path:

    if value:
        return Path(value).expanduser().resolve()
    if configured := os.environ.get("LOOP_MANAGER_STATE_DIR"):
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        parent = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return parent / "loop-manager"


def is_lock_contention(error: OSError) -> bool:
    """Return whether a non-blocking lock failure means another process holds it."""

    return error.errno in {errno.EACCES, errno.EAGAIN} or (
        os.name == "nt" and error.winerror in {32, 33}
    )


@contextlib.contextmanager
def process_lock(state_dir: Path):
    # ponytail: 부트스트랩은 전역 직렬화한다. 실제 병렬 처리 도입 시 프로젝트별 잠금으로 교체한다.
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "loop-manager.lock"
    handle = lock_path.open("a+b")
    handle.seek(0)
    if handle.read(1) == b"":
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        if is_lock_contention(error):
            raise ProcessLockBusyError("다른 Loop Manager 프로세스가 실행 중입니다.") from error
        raise
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _ensure_execplans_schema(connection: sqlite3.Connection) -> None:
    connection.execute(EXEC_PLANS_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(exec_plans)")}
    if "blocked_fingerprint" not in columns:
        connection.execute("ALTER TABLE exec_plans ADD COLUMN blocked_fingerprint TEXT")
    if "blocked_worktree_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN blocked_worktree_fingerprint TEXT"
        )
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 0"
        )
    migrations = {
        "reviewer_session_id": "TEXT",
        "review_result": "TEXT",
        "verify_failure_history": "TEXT NOT NULL DEFAULT '{}'",
        "review_failure_history": "TEXT NOT NULL DEFAULT '{}'",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE exec_plans ADD COLUMN {column} {definition}"
            )
    connection.execute(EXEC_PLANS_ACTIVE_INDEX)


def connect_database(state_dir: Path, *, create: bool = False) -> sqlite3.Connection:
    database = state_dir / "loop-manager.db"
    if not create and not database.is_file():
        raise RuntimeError("상태 DB가 없습니다. 먼저 `loop_manager.py init`을 실행하세요.")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    if create:
        connection.execute(SCHEMA)
        _ensure_execplans_schema(connection)
        _ensure_cleanup_pending_schema(connection)
        connection.commit()
    else:
        try:
            connection.execute("SELECT 1 FROM projects LIMIT 1")
        except sqlite3.DatabaseError as error:
            connection.close()
            raise RuntimeError("상태 DB를 읽을 수 없습니다. 백업을 복원하거나 다시 초기화하세요.") from error
    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    migrations = {
        "plan_path": "TEXT",
        "planning_fingerprint": "TEXT",
        "planning_attempt_fingerprint": "TEXT",
        "planning_workspace_owned": "INTEGER NOT NULL DEFAULT 0",
        "planning_retry_count": "INTEGER NOT NULL DEFAULT 0",
        "planner_session_id": "TEXT",
        "plan_sha": "TEXT",
        "worker_session_id": "TEXT",
        "reviewer_session_id": "TEXT",
        "worker_result": "TEXT",
        "verify_result": "TEXT",
        "review_result": "TEXT",
        "planning_validation_error": "TEXT",
        "planning_result_pending_validation": "INTEGER NOT NULL DEFAULT 0",
        "planning_result_completed": "INTEGER NOT NULL DEFAULT 0",
        "planning_error": "TEXT",
        "planning_error_fingerprint": "TEXT",
        "agent_role": "TEXT",
        "agent_snapshot": "TEXT",
        "agent_input_fingerprint": "TEXT",
        "agent_baseline": "TEXT",
        "agent_recovery": "TEXT",
        "agent_input_plan": "TEXT",
        "agent_result_plan": "TEXT",
        "agent_result_snapshot": "TEXT",
        "agent_manager_snapshot": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {definition}")
    if not migrations.keys() <= columns:
        connection.commit()
    _ensure_execplans_schema(connection)
    _ensure_cleanup_pending_schema(connection)
    connection.commit()
    # Repository-aware migrations belong in run_project, after persisted Agent
    # config has been checked against its pre-invocation snapshot.
    return connection


def update_project(connection: sqlite3.Connection, repo: str, **values: object) -> None:
    if not values or not set(values).issubset(FIELDS):
        raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
    current = connection.execute(
        "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    assignments = ", ".join(f"{name} = ?" for name in values)
    connection.execute(
        f"UPDATE projects SET {assignments} WHERE repo = ?",
        [*values.values(), repo],
    )
    _mirror_project_update_to_execplan(connection, repo, current, values)
    connection.commit()


def command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        raise RuntimeError(f"명령 실패 ({' '.join(arguments[:3])}):\n{output}")
    return result


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return command(["git", "-C", str(repo), *arguments], check=check)


def github_repository(origin_url: str) -> str:
    match = re.fullmatch(
        r"(?:git@|ssh://git@|https://)(?:www\.)?github\.com[:/](?P<owner>[^/:\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?",
        origin_url.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("origin이 GitHub 저장소가 아닙니다.")
    return f"{match.group('owner')}/{match.group('repo')}".lower()


def backfill_plan_shas(
    connection: sqlite3.Connection,
    *,
    target_repo: str | None = None,
) -> None:
    rows = connection.execute(
        """
        SELECT repo, plan_path
        FROM projects
        WHERE plan_sha IS NULL
          AND plan_path IS NOT NULL
          AND phase IN ('WORKING', 'VERIFYING', 'REVIEWING', 'PUBLISHING')
        """
    ).fetchall()
    if target_repo is not None:
        rows = [row for row in rows if row["repo"] == target_repo]
    for row in rows:
        result = git(
            Path(row["repo"]),
            "log",
            "--all",
            "--diff-filter=A",
            "--format=%H",
            "--",
            row["plan_path"],
            check=False,
        )
        commits = [
            value
            for value in result.stdout.splitlines()
            if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
        ]
        if len(commits) == 1:
            connection.execute(
                "UPDATE projects SET plan_sha = ? WHERE repo = ?",
                (commits[0], row["repo"]),
            )
        else:
            connection.execute(
                "UPDATE projects SET phase = 'BLOCKED', last_error = ? WHERE repo = ?",
                (
                    PLAN_SHA_BACKFILL_ERROR,
                    row["repo"],
                ),
            )
    connection.commit()


def git_bytes(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        output = result.stdout[-6000:].decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"명령 실패 (git -C {repo}):\n{output}")
    return result.stdout


def _git_tree_entries(repo: Path, revision: str) -> dict[bytes, tuple[bytes, bytes]]:
    tree = git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("Git tree 항목 형식이 올바르지 않습니다.")
        _mode, object_type, object_id = parts
        if object_type not in {b"blob", b"commit"}:
            raise RuntimeError("지원하지 않는 Git tree 항목 형식입니다.")
        entries[path] = (object_type, object_id)
    return entries


def _is_active_requirement(path: bytes) -> bool:
    return re.fullmatch(rb"docs/requirements/[^/]*\.md", path) is not None


def _is_planning_input(path: bytes) -> bool:
    if path in {b"AGENTS.md", b"docs/PRODUCT.md", b"docs/ARCHITECTURE.md"}:
        return True
    if _is_active_requirement(path):
        return True
    return not path.startswith(b"docs/")


def _planning_input_digest(entries: list[tuple[bytes, bytes, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-planning-input-v2\0")
    for path, object_type, contents in sorted(entries, key=lambda entry: entry[0]):
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(object_type).to_bytes(8, "big"))
        digest.update(object_type)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _planning_input_fingerprint(repo: Path, revision: str) -> str:
    entries: list[tuple[bytes, bytes, bytes]] = []
    for path, (object_type, object_id) in _git_tree_entries(repo, revision).items():
        if _is_planning_input(path):
            if object_type == b"blob":
                contents = git_bytes(repo, "cat-file", "blob", object_id.decode("ascii"))
            else:
                contents = object_id
            entries.append((path, object_type, contents))
    return _planning_input_digest(entries)


def planning_input_fingerprint(repo: Path, base: str) -> str:
    return _planning_input_fingerprint(repo, f"origin/{base}")


def _is_requirement_archive_only_change(
    repo: Path,
    previous_revision: str,
    current_revision: str,
) -> bool:
    previous_tree = _git_tree_entries(repo, previous_revision)
    current_tree = _git_tree_entries(repo, current_revision)
    previous_inputs = {
        path: object_id for path, object_id in previous_tree.items() if _is_planning_input(path)
    }
    current_inputs = {
        path: object_id for path, object_id in current_tree.items() if _is_planning_input(path)
    }

    removed = previous_inputs.keys() - current_inputs.keys()
    added = current_inputs.keys() - previous_inputs.keys()
    changed = {
        path
        for path in previous_inputs.keys() & current_inputs.keys()
        if previous_inputs[path] != current_inputs[path]
    }
    if not removed or added or changed:
        return False

    requirement_prefix = b"docs/requirements/"
    archive_prefix = b"docs/requirements/archive/"
    for path in removed:
        if not _is_active_requirement(path):
            return False
        archived_path = archive_prefix + path.removeprefix(requirement_prefix)
        if archived_path in previous_tree or current_tree.get(archived_path) != previous_inputs[path]:
            return False
    return True


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def parse_frontmatter(text: str) -> dict[str, object]:
    lines = text.replace("\r\n", "\n").splitlines()
    if not lines or lines[0] != "---":
        raise RuntimeError("ExecPlan frontmatter가 없습니다.")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise RuntimeError("ExecPlan frontmatter 종료 구분자가 없습니다.") from error
    values: dict[str, object] = {}
    current_list: str | None = None
    for line in lines[1:end]:
        if line.startswith("  - ") and current_list:
            assert isinstance(values[current_list], list)
            values[current_list].append(line[4:].strip())
            continue
        if ":" not in line:
            raise RuntimeError(f"유효하지 않은 frontmatter 줄입니다: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        if key in values:
            raise RuntimeError(f"중복 frontmatter key입니다: {key}")
        if value == "":
            values[key] = []
            current_list = key
        elif value == "[]":
            values[key] = []
            current_list = None
        else:
            values[key] = value
            current_list = None
    return values


def _parse_execplan(
    worktree: Path,
    path: Path,
    *,
    require_priority: bool = False,
    validate_sources: bool = True,
) -> dict[str, object]:
    legacy = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    allowed = legacy | {"priority"}
    values = parse_frontmatter(path.read_text(encoding="utf-8"))
    if set(values) not in {frozenset(legacy), frozenset(allowed)}:
        raise RuntimeError("ExecPlan frontmatter key가 docs/PLANS.md 형식과 다릅니다.")
    if "priority" not in values:
        if require_priority:
            raise RuntimeError("새 ExecPlan에는 priority가 필요합니다.")
        values["priority"] = "normal"
    plan_id = values["id"]
    title = values["title"]
    if not isinstance(plan_id, str) or not re.fullmatch(r"G-\d{3,}", plan_id):
        raise RuntimeError("ExecPlan id는 G-000 형식이어야 합니다.")
    if not isinstance(title, str) or path.name != f"[{plan_id}] {title}.md":
        raise RuntimeError("ExecPlan 파일명은 `[id] title.md`와 일치해야 합니다.")
    sources = values["sources"]
    dependencies = values["depends_on"]
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("ExecPlan sources가 하나 이상 필요합니다.")
    if not isinstance(dependencies, list):
        raise RuntimeError("ExecPlan depends_on이 유효하지 않습니다.")
    for source in sources:
        if not isinstance(source, str) or not (
            source == "docs/PRODUCT.md"
            or re.fullmatch(r"docs/requirements/[^/\\]+\.md", source)
        ):
            raise RuntimeError(f"허용되지 않는 ExecPlan source입니다: {source}")
        if validate_sources and not (worktree / source).is_file():
            raise RuntimeError(f"존재하지 않는 ExecPlan source입니다: {source}")
    for dependency in dependencies:
        if not isinstance(dependency, str) or not re.fullmatch(r"G-\d{3,}", dependency):
            raise RuntimeError(f"ExecPlan depends_on ID 형식이 유효하지 않습니다: {dependency}")
    if values["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("ExecPlan complexity가 유효하지 않습니다.")
    if values["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("ExecPlan risk가 유효하지 않습니다.")
    if values["priority"] not in PRIORITY_ORDER:
        raise RuntimeError("ExecPlan priority가 유효하지 않습니다.")
    return values


def _validate_dependency_graph(catalog: dict[str, dict[str, object]]) -> None:
    for plan_id, plan in catalog.items():
        dependencies = plan["depends_on"]
        assert isinstance(dependencies, list)
        missing = sorted(dependency for dependency in dependencies if dependency not in catalog)
        if missing:
            raise RuntimeError(
                f"ExecPlan {plan_id}의 존재하지 않는 depends_on ID입니다: "
                + ", ".join(missing)
            )

    states: dict[str, str] = {}
    stack: list[str] = []

    def visit(plan_id: str) -> None:
        states[plan_id] = "visiting"
        stack.append(plan_id)
        dependencies = catalog[plan_id]["depends_on"]
        assert isinstance(dependencies, list)
        for dependency in sorted(dependencies):
            if states.get(dependency) == "visiting":
                cycle_start = stack.index(dependency)
                cycle = [*stack[cycle_start:], dependency]
                raise RuntimeError("순환 ExecPlan 의존성입니다: " + " -> ".join(cycle))
            if states.get(dependency) is None:
                visit(dependency)
        stack.pop()
        states[plan_id] = "visited"

    for plan_id in catalog:
        if states.get(plan_id) is None:
            visit(plan_id)


def load_execplan_catalog(worktree: Path, *, validate_sources: bool = True) -> dict[str, dict[str, object]]:
    entries: list[tuple[str, dict[str, object]]] = []
    plans_directory = worktree / "docs/plans"
    plan_paths = sorted(
        (path for path in plans_directory.glob("*.md") if path.is_file()),
        key=lambda path: path.name,
    )
    for path in plan_paths:
        plan = _parse_execplan(worktree, path, validate_sources=validate_sources)
        plan_id = plan["id"]
        assert isinstance(plan_id, str)
        entries.append((plan_id, plan))

    catalog: dict[str, dict[str, object]] = {}
    for plan_id, plan in sorted(entries, key=lambda entry: entry[0]):
        if plan_id in catalog:
            raise RuntimeError(f"중복 ExecPlan id입니다: {plan_id}")
        catalog[plan_id] = plan

    _validate_dependency_graph(catalog)
    return catalog


def validate_execplan(worktree: Path, relative_path: str) -> dict[str, object]:
    catalog = load_execplan_catalog(worktree)
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", relative_path)
    if match:
        plan = catalog.get(match.group(1))
        if plan is not None:
            expected_path = f"docs/plans/[{plan['id']}] {plan['title']}.md"
            if relative_path == expected_path:
                return plan
    raise RuntimeError(f"검증된 ExecPlan 카탈로그에 계획이 없습니다: {relative_path}")


class PlannerValidationError(RuntimeError):
    """A Planner output error that is eligible for a correction attempt."""


def validate_planner_result(
    worktree: Path,
    active_sha: str,
    dirty_files: list[str],
) -> list[tuple[str, dict[str, object]]] | None:
    """Validate a batch of new Planner-owned ExecPlans before publication.

    Scope and ExecPlan catalog errors are deliberately translated to a
    dedicated exception. Operational Git failures must retain their normal
    recovery boundary and never consume the Planner validation retry budget.
    """
    planned_files = sorted(set(dirty_files) | set(changed_files(worktree, active_sha)))
    if not planned_files:
        return None
    for plan_path in planned_files:
        if not re.fullmatch(r"docs/plans/[^/]+\.md", plan_path):
            raise PlannerValidationError("Planner는 docs/plans의 새 ExecPlan만 작성해야 합니다.")
        if not (worktree / plan_path).is_file():
            raise PlannerValidationError("Planner는 기존 ExecPlan을 삭제할 수 없습니다.")
        existed = git(
            worktree,
            "cat-file",
            "-e",
            f"{active_sha}:{plan_path}",
            check=False,
        ).returncode == 0
        if existed:
            raise PlannerValidationError(f"Planner는 기존 ExecPlan을 수정할 수 없습니다: {plan_path}")

    try:
        catalog = load_execplan_catalog(worktree, validate_sources=False)
        plans: list[tuple[str, dict[str, object]]] = []
        for plan_path in planned_files:
            parsed = _parse_execplan(worktree, worktree / plan_path, require_priority=True)
            plans.append((plan_path, parsed))
        new_ids = {str(plan["id"]) for _, plan in plans}
        existing_ids = [int(plan_id[2:]) for plan_id in catalog if plan_id not in new_ids]
        first_id = max(existing_ids, default=0) + 1
        expected = list(range(first_id, first_id + len(plans)))
        actual = sorted(int(str(plan["id"])[2:]) for _, plan in plans)
        if actual != expected:
            raise RuntimeError("새 ExecPlan ID는 기존 최대 번호 다음부터 연속적이어야 합니다.")
        return sorted(plans, key=lambda item: int(str(item[1]["id"])[2:]))
    except RuntimeError as error:
        raise PlannerValidationError(str(error)) from error


def _plan_id_from_path(path: str) -> str | None:
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", path)
    return match.group(1) if match else None


def _execplan_metadata(plan: dict[str, object]) -> tuple[str, str, str]:
    return (
        json.dumps(plan["sources"], ensure_ascii=False, separators=(",", ":")),
        json.dumps(plan["depends_on"], ensure_ascii=False, separators=(",", ":")),
        str(plan["priority"]),
    )


def register_execplans(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plans: list[tuple[str, dict[str, object]]],
    plan_commit: str,
    planning_fingerprint: str,
) -> None:
    """Register one published Planner batch atomically and idempotently."""

    with connection:
        _ensure_execplans_schema(connection)
        for path, plan in plans:
            plan_id = str(plan["id"])
            sources, dependencies, priority = _execplan_metadata(plan)
            content_hash = file_hash(worktree / path)
            existing = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND (plan_id = ? OR path = ?)",
                (repo, plan_id, path),
            ).fetchone()
            if existing:
                immutable = (
                    existing["plan_id"],
                    existing["path"],
                    existing["title"],
                    existing["sources"],
                    existing["depends_on"],
                    existing["priority"],
                    existing["content_hash"],
                    existing["plan_sha"],
                )
                expected = (
                    plan_id,
                    path,
                    str(plan["title"]),
                    sources,
                    dependencies,
                    priority,
                    content_hash,
                    plan_commit,
                )
                if immutable != expected:
                    raise RuntimeError(f"게시된 ExecPlan 등록 정보가 충돌합니다: {plan_id}")
                continue
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, plan_sha
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?)
                """,
                (
                    repo,
                    plan_id,
                    path,
                    str(plan["title"]),
                    sources,
                    dependencies,
                    priority,
                    content_hash,
                    plan_commit,
                ),
            )
        values = _clear_active_values(
            processed_sha=plan_commit,
            planning_fingerprint=planning_fingerprint,
        )
        assignments = ", ".join(f"{name} = ?" for name in values)
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?", (*values.values(), repo)
        )


def _legacy_plan_sha(root: Path, revision: str, relative_path: str) -> str:
    result = git(
        root,
        "log",
        revision,
        "--diff-filter=A",
        "--format=%H",
        "--",
        relative_path,
        check=False,
    )
    commits = [
        value
        for value in result.stdout.splitlines()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
    ]
    if len(commits) != 1:
        raise RuntimeError(
            f"Legacy ExecPlan의 게시 commit을 안전하게 확인할 수 없습니다: {relative_path}"
        )
    return commits[0]


def migrate_legacy_execplans(
    connection: sqlite3.Connection,
    *,
    reconcile_history: bool = False,
    target_repo: str | None = None,
) -> None:
    """Conservatively move legacy project-owned execution state into the catalog."""

    _ensure_execplans_schema(connection)
    phase_status = {
        "WORKING": "IMPLEMENTING",
        "VERIFYING": "VERIFYING",
        "REVIEWING": "REVIEWING",
        "PUBLISHING": "PUBLISHING",
        "PR_OPEN": "PR_OPEN",
        "BLOCKED": "BLOCKED",
    }
    if target_repo is None:
        projects = connection.execute("SELECT * FROM projects ORDER BY repo").fetchall()
    else:
        projects = connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (target_repo,)
        ).fetchall()
    with connection:
        for project in projects:
            if project["phase"] == "PLANNING":
                continue
            root = Path(project["repo"])
            if reconcile_history:
                git(root, "fetch", "origin")
            active_path = str(project["plan_path"] or "")
            documents: dict[str, str] = {}

            revision = f"refs/remotes/origin/{project['base_branch']}"
            listed = git(
                root,
                "ls-tree",
                "-r",
                "--name-only",
                revision,
                "--",
                "docs/plans",
                check=False,
            )
            if listed.returncode == 0:
                for relative_path in listed.stdout.splitlines():
                    if not re.fullmatch(r"docs/plans/[^/]+\.md", relative_path):
                        continue
                    shown = git(root, "show", f"{revision}:{relative_path}", check=False)
                    if shown.returncode == 0:
                        documents[relative_path] = shown.stdout

            if active_path and active_path not in documents and project["plan_sha"]:
                shown = git(
                    root,
                    "show",
                    f"{project['plan_sha']}:{active_path}",
                    check=False,
                )
                if shown.returncode == 0:
                    documents[active_path] = shown.stdout

            for relative_path, plan_text in sorted(documents.items()):
                try:
                    plan = published_execplan(relative_path, plan_text)
                except RuntimeError:
                    continue
                plan_id = str(plan["id"])
                if connection.execute(
                    "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = ?",
                    (project["repo"], plan_id),
                ).fetchone():
                    continue
                is_active = relative_path == active_path and project["phase"] in phase_status
                if not is_active and not reconcile_history:
                    continue
                historical_pr = None
                if is_active:
                    status = phase_status[str(project["phase"])]
                    plan_sha = project["plan_sha"]
                else:
                    historical_pr = pr_for_branch(root, f"loop-manager/{plan_id}")
                    if historical_pr is None:
                        status = "READY"
                    elif historical_pr["state"] == "MERGED" or historical_pr.get("mergedAt"):
                        status = "MERGED"
                    else:
                        raise RuntimeError(
                            f"Legacy ExecPlan의 PR 상태를 자동 이관할 수 없습니다: "
                            f"{plan_id} ({historical_pr['state']})"
                        )
                    plan_sha = _legacy_plan_sha(root, revision, relative_path)
                sources, dependencies, priority = _execplan_metadata(plan)
                connection.execute(
                    """
                    INSERT INTO exec_plans (
                        repo, plan_id, path, title, sources, depends_on, priority,
                        content_hash, status, plan_sha, active_sha, branch, worktree,
                        pr_url, last_error, worker_session_id, worker_result, verify_result,
                        workspace_owned
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project["repo"],
                        plan_id,
                        relative_path,
                        str(plan["title"]),
                        sources,
                        dependencies,
                        priority,
                        hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
                        status,
                        plan_sha,
                        project["active_sha"] if is_active else None,
                        project["branch"] if is_active else None,
                        project["worktree"] if is_active else None,
                        project["pr_url"] if is_active else (
                            str(historical_pr["url"]) if historical_pr else None
                        ),
                        project["last_error"] if is_active else None,
                        project["worker_session_id"] if is_active else None,
                        project["worker_result"] if is_active else None,
                        project["verify_result"] if is_active else None,
                        1 if is_active else 0,
                    ),
                )


def active_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    _ensure_execplans_schema(connection)
    placeholders = ", ".join("?" for _ in ACTIVE_EXECPLAN_STATUSES)
    return connection.execute(
        f"SELECT * FROM exec_plans WHERE repo = ? AND status IN ({placeholders})",
        (repo, *sorted(ACTIVE_EXECPLAN_STATUSES)),
    ).fetchone()


def mark_execplan_workspace_owned(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        changed = connection.execute(
            "UPDATE exec_plans SET workspace_owned = 1 "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        )
        if changed.rowcount != 1:
            raise RuntimeError("workspace 소유권을 기록할 ExecPlan을 찾을 수 없습니다.")


def reserve_execplan_workspace(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        changed = connection.execute(
            "UPDATE exec_plans SET workspace_owned = ? "
            "WHERE repo = ? AND plan_id = ? AND workspace_owned IN (?, ?)",
            (
                WORKSPACE_RESERVED,
                repo,
                plan_id,
                WORKSPACE_UNOWNED,
                WORKSPACE_RESERVED,
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("workspace 생성을 예약할 ExecPlan을 찾을 수 없습니다.")


def compact_execplan_catalog(connection: sqlite3.Connection, repo: str) -> str:
    """Serialize only non-terminal plan metadata, never plan bodies."""

    _ensure_execplans_schema(connection)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED'",
        (repo,),
    ).fetchall()
    rows = sorted(rows, key=lambda row: int(str(row["plan_id"])[2:]))
    catalog = [
        {
            "id": row["plan_id"],
            "title": row["title"],
            "status": row["status"],
            "sources": json.loads(row["sources"]),
            "depends_on": json.loads(row["depends_on"]),
            "priority": row["priority"],
        }
        for row in rows
    ]
    return json.dumps(catalog, ensure_ascii=False, indent=2)


def select_ready_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Apply the dependency gate before deterministic priority/ID ordering."""

    _ensure_execplans_schema(connection)
    if active_execplan(connection, repo):
        return None
    statuses = {
        row["plan_id"]: row["status"]
        for row in connection.execute(
            "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
            (repo,),
        )
    }
    candidates = []
    for row in connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status = 'READY'",
        (repo,),
    ):
        dependencies = json.loads(row["depends_on"])
        if all(statuses.get(dependency) == "MERGED" for dependency in dependencies):
            candidates.append(row)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (
            PRIORITY_ORDER[str(row["priority"])],
            int(str(row["plan_id"])[2:]),
        ),
    )


def activate_ready_execplan(
    connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    base_sha: str,
) -> sqlite3.Row | None:
    selected = select_ready_execplan(connection, repo)
    if selected is None:
        return None
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    branch = str(selected["branch"] or f"loop-manager/{selected['plan_id']}")
    worktree = (
        Path(str(selected["worktree"]))
        if selected["worktree"]
        else state_dir / "worktrees" / project_id / str(selected["plan_id"])
    )
    activation_sha = (
        git(worktree, "rev-parse", "HEAD").stdout.strip()
        if worktree.is_dir()
        else base_sha
    )
    with connection:
        changed = connection.execute(
            """
            UPDATE exec_plans
            SET status = 'IMPLEMENTING', active_sha = ?, branch = ?, worktree = ?,
                last_error = NULL
            WHERE repo = ? AND plan_id = ? AND status = 'READY'
            """,
            (activation_sha, branch, str(worktree), repo, selected["plan_id"]),
        )
        if changed.rowcount != 1:
            raise RuntimeError("READY ExecPlan 선택 상태가 변경되었습니다.")
        connection.execute(
            """
            UPDATE projects
            SET active_sha = ?, plan_sha = ?, phase = 'WORKING', branch = ?,
                worktree = ?, plan_path = ?, pr_url = NULL, last_error = NULL,
                worker_session_id = NULL, reviewer_session_id = NULL,
                worker_result = NULL, verify_result = NULL, review_result = NULL
            WHERE repo = ?
            """,
            (
                activation_sha,
                selected["plan_sha"],
                branch,
                str(worktree),
                selected["path"],
                repo,
            ),
        )
    return connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, selected["plan_id"]),
    ).fetchone()


def hydrate_active_project(connection: sqlite3.Connection, repo: str) -> None:
    """Refresh the legacy execution mirror from the plan-state source of truth."""

    plan = active_execplan(connection, repo)
    if plan is None:
        return
    phase = "WORKING" if plan["status"] == "IMPLEMENTING" else plan["status"]
    connection.execute(
        """
        UPDATE projects
        SET active_sha = ?, plan_sha = ?, phase = ?, branch = ?, worktree = ?,
            plan_path = ?, pr_url = ?, last_error = ?, worker_session_id = ?,
            reviewer_session_id = ?, worker_result = ?, verify_result = ?,
            review_result = ?
        WHERE repo = ?
        """,
        (
            plan["active_sha"],
            plan["plan_sha"],
            phase,
            plan["branch"],
            plan["worktree"],
            plan["path"],
            plan["pr_url"],
            plan["last_error"],
            plan["worker_session_id"],
            plan["reviewer_session_id"],
            plan["worker_result"],
            plan["verify_result"],
            plan["review_result"],
            repo,
        ),
    )
    connection.commit()


def _mirror_project_update_to_execplan(
    connection: sqlite3.Connection,
    repo: str,
    current: sqlite3.Row | None,
    values: dict[str, object],
) -> None:
    if current is None or not current["plan_path"]:
        return
    if "phase" in values and values["phase"] is None:
        return
    plan_id = _plan_id_from_path(str(current["plan_path"]))
    if plan_id is None:
        return
    _ensure_execplans_schema(connection)
    translated: dict[str, object] = {}
    for field in (
        "active_sha",
        "plan_sha",
        "branch",
        "worktree",
        "pr_url",
        "last_error",
        "worker_session_id",
        "reviewer_session_id",
        "worker_result",
        "verify_result",
        "review_result",
    ):
        if field in values:
            translated[field] = values[field]
    if "phase" in values:
        status = "IMPLEMENTING" if values["phase"] == "WORKING" else values["phase"]
        if status != "PLANNING":
            translated["status"] = status
    if not translated:
        return
    assignments = ", ".join(f"{name} = ?" for name in translated)
    connection.execute(
        f"UPDATE exec_plans SET {assignments} WHERE repo = ? AND plan_id = ?",
        (*translated.values(), repo, plan_id),
    )


def validate_repository(path: Path, *, allow_agents_mismatch: bool) -> Path:
    if not shutil.which("git") or not shutil.which("gh") or not shutil.which("codex"):
        raise RuntimeError("git, gh와 codex CLI가 PATH에 있어야 합니다.")
    path = path.expanduser().resolve()
    root = Path(git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != path:
        raise RuntimeError(f"저장소 루트 경로를 지정하세요: {root}")
    github_repository(git(root, "remote", "get-url", "origin").stdout)
    required = [root / "AGENTS.md", root / "docs/PLANS.md", root / "docs/ARCHITECTURE.md"]
    missing = [str(item.relative_to(root)) for item in required if not item.is_file()]
    requirements = root / "docs/requirements"
    if not (root / "docs/PRODUCT.md").is_file() and not any(requirements.glob("*.md")):
        missing.append("docs/PRODUCT.md 또는 docs/requirements/*.md")
    if missing:
        raise RuntimeError("필수 파일이 없습니다: " + ", ".join(missing))
    if file_hash(root / "docs/PLANS.md") != file_hash(ROOT / "docs/PLANS.md"):
        raise RuntimeError("docs/PLANS.md가 Loop Manager 기준 파일과 다릅니다.")
    common_agents = normalized_text(ROOT / "AGENTS.md")
    target_agents = normalized_text(root / "AGENTS.md")
    if common_agents not in target_agents and not allow_agents_mismatch:
        raise RuntimeError(
            "AGENTS.md에 공통 규칙이 없습니다. 확인 후 --allow-agents-mismatch로 강제 실행하세요."
        )
    return root


def remote_base(repo: Path, requested: str | None) -> str:
    git(repo, "fetch", "origin")
    if requested:
        base = requested
    else:
        reference = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        base = reference.removeprefix("origin/")
    git(repo, "rev-parse", "--verify", f"origin/{base}")
    return base


def ensure_safe_base_worktree(repo: Path, base: str) -> None:
    if git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("기본 worktree가 dirty 상태라서 작업을 시작하지 않습니다.")
    counts = git(repo, "rev-list", "--left-right", "--count", f"HEAD...origin/{base}").stdout.split()
    if int(counts[0]):
        raise RuntimeError("기본 worktree가 원격보다 앞서 있거나 diverge되어 작업을 시작하지 않습니다.")


def worker_level(plan: dict[str, object]) -> int:
    complexity_scores = {"low": 1, "medium": 3, "high": 5}
    risk_scores = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    try:
        complexity = complexity_scores[plan["complexity"]]
        risk = risk_scores[plan["risk"]]
    except (KeyError, TypeError) as error:
        raise ValueError("검증된 ExecPlan complexity와 risk가 필요합니다.") from error
    return min(complexity + risk, 7)


def codex_model_efforts(worktree: Path | None = None) -> dict[str, frozenset[str]]:
    try:
        catalog = json.loads(
            command(
                ["codex", "debug", "models"],
                cwd=worktree,
                env=agent_environment(),
            ).stdout
        )
        models = catalog["models"]
        return {
            model["slug"]: frozenset(level["effort"] for level in model["supported_reasoning_levels"])
            for model in models
        }
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Codex CLI model catalog 형식이 올바르지 않습니다.") from error


def codex_profile(
    config_path: Path,
    section: str,
    name: str,
    label: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    parser = configparser.ConfigParser()
    try:
        loaded = parser.read(config_path, encoding="utf-8")
    except configparser.Error as error:
        raise RuntimeError(f"Codex 설정 파일 형식이 올바르지 않습니다: {config_path}") from error
    if not loaded:
        raise RuntimeError(f"설정 파일이 없습니다: {config_path}")
    if section not in parser:
        raise RuntimeError(f"{label} 설정 section [{section}]이 없습니다.")
    if name not in parser[section]:
        raise RuntimeError(f"{label} 설정이 없습니다: [{section}] {name}")

    try:
        value = parser[section][name]
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: [{section}] {name}"
        ) from error
    if not value.strip():
        raise RuntimeError(f"{label} Model alias/Effort 설정이 비어 있습니다.")
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise RuntimeError(f"{label} 설정은 `Model alias,Effort` 형식이어야 합니다.")
    alias, effort = parts
    if not alias:
        raise RuntimeError(f"{label} Model alias 설정이 비어 있습니다.")
    if not effort:
        raise RuntimeError(f"{label} Effort 설정이 비어 있습니다.")
    if "codex.models" not in parser:
        raise RuntimeError(f"{label} Model alias section [codex.models]가 없습니다.")
    if alias not in parser["codex.models"]:
        raise RuntimeError(f"{label} Model alias가 [codex.models]에 없습니다: {alias}")
    try:
        model = parser["codex.models"][alias].strip()
    except configparser.InterpolationError as error:
        raise RuntimeError(
            f"{label} 설정 값 형식이 올바르지 않습니다: [codex.models] {alias}"
        ) from error
    if not model:
        raise RuntimeError(f"{label} Model 설정이 비어 있습니다: [codex.models] {alias}")
    supported = codex_model_efforts(worktree).get(model)
    if supported is None:
        raise RuntimeError(f"{label} Model을 Codex CLI가 지원하지 않습니다: {model}")
    if effort not in supported:
        raise RuntimeError(
            f"{label} Model/Effort 조합을 Codex CLI가 지원하지 않습니다: {model},{effort}"
        )
    return model, effort


def role(config_path: Path, name: str, worktree: Path | None = None) -> tuple[str, str]:
    return codex_profile(config_path, "codex.roles", name, f"Codex {name}", worktree)


def worker_profile(
    config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    if type(level) is not int or not 1 <= level <= 7:
        raise RuntimeError(f"Codex Worker level이 유효하지 않습니다: {level}")
    name = f"level{level}"
    return codex_profile(config_path, "codex.worker", name, f"Codex Worker {name}", worktree)


def agent_environment() -> dict[str, str]:
    allowed = {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SSH_AUTH_SOCK",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def codex_sandbox_arguments(read_only: bool) -> list[str]:
    return ["--sandbox", "read-only"] if read_only else ["--approve-for-me"]


def run_codex(
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
) -> tuple[int, str] | CodexResult:
    if role_name == "worker":
        if level is None:
            raise RuntimeError("Codex Worker level이 없습니다.")
        model, effort = worker_profile(config_path, level, worktree)
    else:
        if level is not None:
            raise RuntimeError(f"Codex {role_name}에 Worker level을 지정할 수 없습니다.")
        model, effort = role(config_path, role_name, worktree)
    project_dir = state_dir / "logs" / hashlib.sha256(str(worktree).encode()).hexdigest()[:16]
    project_dir.mkdir(parents=True, exist_ok=True)
    last_message = project_dir / f"{role_name}-last.txt"
    log = project_dir / f"{role_name}.log"
    last_message.unlink(missing_ok=True)
    if session_id:
        arguments = [
            "codex",
            "exec",
            *codex_sandbox_arguments(read_only),
            "-C",
            str(worktree),
            "resume",
            "--json",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-o",
            str(last_message),
            session_id,
            "-",
        ]
    else:
        arguments = [
            "codex",
            "exec",
            *(["--json"] if persist_session else ["--ephemeral"]),
            *codex_sandbox_arguments(read_only),
            "-C",
            str(worktree),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-o",
            str(last_message),
            "-",
        ]
    resolved_session_id = session_id
    timeout_triggered = threading.Event()
    if persist_session and not session_id:
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=agent_environment(),
                cwd=worktree,
            )
            assert process.stdin is not None
            assert process.stdout is not None

            def terminate_on_timeout() -> None:
                timeout_triggered.set()
                try:
                    process.kill()
                except OSError:
                    pass

            timeout = threading.Timer(
                AGENT_CLI_TIMEOUT_SECONDS,
                terminate_on_timeout,
            )
            timeout.daemon = True
            timeout.start()
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    output.write(line)
                    output.flush()
                    if resolved_session_id:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "thread.started" and isinstance(
                        event.get("thread_id"), str
                    ):
                        resolved_session_id = event["thread_id"]
                        if on_session_started:
                            on_session_started(resolved_session_id)
                returncode = process.wait()
            except BaseException:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise
            finally:
                timeout.cancel()
                process.stdout.close()
    else:
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(
                arguments,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=output,
                stderr=subprocess.STDOUT,
                env=agent_environment(),
                cwd=worktree,
                timeout=AGENT_CLI_TIMEOUT_SECONDS,
            )
        returncode = result.returncode
    message = last_message.read_text(encoding="utf-8") if last_message.is_file() else ""
    if timeout_triggered.is_set():
        message = f"Codex {role_name} 실행 timeout ({AGENT_CLI_TIMEOUT_SECONDS}초)"
    if returncode and not message:
        message = log.read_text(encoding="utf-8")[-6000:]
    if persist_session and not resolved_session_id:
        if returncode == 0:
            return CodexResult(
                1,
                f"Codex {role_name} 실행 결과에서 세션 ID를 확인할 수 없습니다.",
            )
    if persist_session or session_id:
        return CodexResult(
            returncode,
            message.strip(),
            resolved_session_id,
            (
                AgentResultKind.TRANSIENT_FAILURE
                if timeout_triggered.is_set()
                else None
            ),
        )
    return returncode, message.strip()


class VerifyOutput(str):
    """Bounded persisted feedback that retains full output for fingerprinting."""

    fingerprint_source: str

    def __new__(cls, output: str) -> VerifyOutput:
        fingerprint_source = output.strip()
        instance = super().__new__(cls, fingerprint_source[-8000:])
        instance.fingerprint_source = fingerprint_source
        return instance


def verify(worktree: Path) -> tuple[bool, str]:
    if os.name == "nt":
        script = worktree / "scripts/verify.ps1"
        executable = shutil.which("pwsh") or shutil.which("powershell")
        arguments = [executable or "pwsh", "-NoProfile", "-File", str(script)]
    else:
        script = worktree / "scripts/verify.sh"
        arguments = ["bash", str(script)]
    if not script.is_file():
        return False, f"전체 검증 스크립트가 없습니다: {script.relative_to(worktree)}"
    result = command(arguments, cwd=worktree, check=False)
    return result.returncode == 0, VerifyOutput(result.stdout)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP_NOISE = re.compile(
    r"(?ix)"
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
)
_DURATION_NOISE = re.compile(
    r"(?i)\b(?:in|after|elapsed[:=]?)\s+\d+(?:[.,]\d+)?\s*(?:ms|s|sec|secs|seconds?)\b"
)
_TEMP_PATH_NOISE = re.compile(
    r"(?i)(?<![\w])"
    r"(?P<root>[a-z]:[\\/][^\r\n]*?[\\/]temp|/(?:private/)?(?:var/)?tmp)"
    r"(?P<tail>[\\/][^\s:]+)"
)
_TEMP_DIRECTORY_CONTEXT = re.compile(
    r"(?i)(?:\b(?:temporary\s+directory|directory|workspace|worktree)\b"
    r"|디렉터리|작업\s*공간)"
)


_REVIEW_ISSUE_ID = re.compile(
    r"(?mi)^[ \t]*ISSUE_ID:[ \t]*(?P<identifier>[^\r\n]+?)[ \t]*$"
)
_REVIEW_ISSUE_DECLARATION = re.compile(r"(?i)^[ \t]*ISSUE_ID\b")
_REVIEW_ISSUE_IDENTIFIER = re.compile(
    r"(?i)^(?!/)(?!.*(?:^|/)\.\.(?:/|$))"
    r"[a-z0-9_.\-/]+::[a-z0-9_.<>/-]+::[a-z0-9][a-z0-9-]*$"
)
_REVIEW_VERDICT_DECLARATION = re.compile(r"(?i)^[ \t]*VERDICT\b")
_REVIEW_VERDICT = re.compile(r"(?i)^[ \t]*VERDICT:[ \t]*(PASS|FAIL)[ \t]*$")


def _normalize_review_issue_ids(output: str) -> tuple[str, ...]:
    output = output.replace("\r\n", "\n").replace("\r", "\n")
    declarations = [
        line
        for line in output.splitlines()
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    if not declarations:
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과에 유효한 ISSUE_ID가 없습니다."
        )
    matches = list(_REVIEW_ISSUE_ID.finditer(output))
    if len(matches) != len(declarations):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    raw_identifiers = [
        " ".join(match.group("identifier").split()).casefold()
        for match in matches
    ]
    if any(
        _REVIEW_ISSUE_IDENTIFIER.fullmatch(identifier) is None
        for identifier in raw_identifiers
    ):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    identifiers = set(raw_identifiers)
    return tuple(f"ISSUE_ID:{identifier}" for identifier in sorted(identifiers))


def parse_reviewer_result(output: str) -> tuple[str, tuple[str, ...]]:
    """Validate the Reviewer response contract before any state transition."""

    lines = output.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    meaningful_lines = [
        (index, line)
        for index, line in enumerate(lines)
        if line.strip()
    ]
    verdict_lines = [
        (index, line)
        for index, line in meaningful_lines
        if _REVIEW_VERDICT_DECLARATION.match(line)
    ]
    if len(verdict_lines) != 1:
        raise ReviewerProtocolError(
            "Reviewer 결과는 정확히 하나의 VERDICT를 포함해야 합니다."
        )
    verdict_index, verdict_line = verdict_lines[0]
    verdict_match = _REVIEW_VERDICT.fullmatch(verdict_line)
    if verdict_match is None:
        raise ReviewerProtocolError(
            "Reviewer VERDICT는 PASS 또는 FAIL이어야 합니다."
        )
    if verdict_index != meaningful_lines[-1][0]:
        raise ReviewerProtocolError(
            "Reviewer VERDICT는 마지막 비어 있지 않은 줄이어야 합니다."
        )

    verdict = verdict_match.group(1).upper()
    issue_declarations = [
        line
        for _index, line in meaningful_lines
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    if verdict == "PASS":
        if issue_declarations:
            raise ReviewerProtocolError(
                "Reviewer PASS 결과에는 ISSUE_ID가 없어야 합니다."
            )
        return verdict, ()
    return verdict, _normalize_review_issue_ids(output)


def _normalize_temp_path(match: re.Match[str]) -> str:
    tail = match.group("tail").strip("\\/")
    components = [part for part in re.split(r"[\\/]", tail) if part]
    leaf = components[-1]
    line_start = match.string.rfind("\n", 0, match.start()) + 1
    prefix = match.string[line_start:match.start()]
    if len(components) == 1 and _TEMP_DIRECTORY_CONTEXT.search(prefix):
        return "<TEMP_PATH>"
    return f"<TEMP_PATH>/{leaf}"


def normalize_validation_failure(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> str:
    """Remove unstable execution noise and make failure line ordering irrelevant."""

    if stage not in {"verify", "review"}:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    fingerprint_source = (
        output.fingerprint_source if isinstance(output, VerifyOutput) else output
    )
    normalized = fingerprint_source.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ANSI_ESCAPE.sub("", normalized)
    if worktree is not None:
        path_values = {
            str(worktree),
            str(worktree.resolve()),
            str(worktree).replace("\\", "/"),
            str(worktree).replace("/", "\\"),
        }
        for path_value in sorted(path_values, key=len, reverse=True):
            if path_value:
                normalized = re.sub(
                    re.escape(path_value),
                    "<WORKTREE>",
                    normalized,
                    flags=re.IGNORECASE,
                )
    normalized = _TEMP_PATH_NOISE.sub(_normalize_temp_path, normalized)
    normalized = _TIMESTAMP_NOISE.sub("<TIMESTAMP>", normalized)
    normalized = _DURATION_NOISE.sub("<DURATION>", normalized)
    if stage == "review":
        return "\n".join(_normalize_review_issue_ids(normalized))
    lines = []
    for line in normalized.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        lines.append(line)
    return "\n".join(sorted(lines))


def validation_failure_fingerprints(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    normalized = normalize_validation_failure(stage, output, worktree)
    failures = normalized.splitlines() if stage == "review" else [normalized]
    fingerprints = []
    for failure in failures:
        digest = hashlib.sha256()
        digest.update(b"loop-manager-validation-failure-v1\0")
        digest.update(stage.encode("utf-8"))
        digest.update(b"\0")
        digest.update(failure.encode("utf-8"))
        fingerprints.append((digest.hexdigest(), failure))
    return tuple(fingerprints)


def validation_failure_fingerprint(
    stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    fingerprints = validation_failure_fingerprints(stage, output, worktree)
    if len(fingerprints) != 1:
        raise ValueError(
            "복수 Review issue는 validation_failure_fingerprints()로 처리해야 합니다."
        )
    return fingerprints[0]


def _deserialize_failure_history(value: object) -> dict[str, dict[str, object]]:
    if value in {None, ""}:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
    history: dict[str, dict[str, object]] = {}
    for fingerprint, record in parsed.items():
        if not isinstance(fingerprint, str) or not isinstance(record, dict):
            raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
        occurrences = record.get("occurrences")
        if type(occurrences) is not int or occurrences < 1:
            raise RuntimeError("저장된 검증 실패 반복 횟수가 유효하지 않습니다.")
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": str(record.get("sample", "")),
        }
    return history


def clear_validation_failure_history(
    connection: sqlite3.Connection,
    repo: str,
    stage: str,
) -> None:
    column = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }.get(stage)
    if column is None:
        raise ValueError(f"검증 성공 단계가 유효하지 않습니다: {stage}")
    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("지문 이력을 초기화할 활성 ExecPlan을 찾을 수 없습니다.")
    with connection:
        connection.execute(
            f"UPDATE exec_plans SET {column} = '{{}}' "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan["plan_id"]),
        )


def pr_for_branch(repo: Path, branch: str, url: str | None = None) -> dict[str, object] | None:
    fields = "url,state,mergedAt,headRefOid"
    if url:
        result = command(["gh", "pr", "view", url, "--json", fields], cwd=repo)
        return json.loads(result.stdout)
    result = command(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", fields],
        cwd=repo,
    )
    rows = json.loads(result.stdout)
    return rows[0] if rows else None


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)



def _validate_managed_path_boundary(
    state_dir: Path,
    worktree: Path,
    expected: Path,
    label: str,
) -> None:
    lexical_worktree = os.path.normcase(
        os.path.abspath(worktree.expanduser())
    )
    lexical_expected = os.path.normcase(
        os.path.abspath(expected.expanduser())
    )
    if lexical_worktree != lexical_expected:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 결정적 소유 경계와 다릅니다: "
            f"{worktree} (expected {expected})"
        )
    state_root = _canonical_path(state_dir)
    resolved = _canonical_path(worktree)
    try:
        resolved.relative_to(state_root)
    except ValueError as error:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 Loop Manager 상태 디렉터리 밖을 가리킵니다: "
            f"{worktree}"
        ) from error

def _worktree_catalog(repo: Path) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    if current:
        records.append(current)
    return records


def _rebase_head_name(worktree: Path) -> str | None:
    for directory in ("rebase-merge", "rebase-apply"):
        result = git(worktree, "rev-parse", "--git-path", f"{directory}/head-name", check=False)
        if result.returncode:
            continue
        git_path = Path(result.stdout.strip())
        if not git_path.is_absolute():
            git_path = worktree / git_path
        if git_path.is_file():
            return git_path.read_text(encoding="utf-8").strip()
    return None


def _rebase_in_progress_for_branch(worktree: Path, branch: str) -> bool:
    return _rebase_head_name(worktree) == f"refs/heads/{branch}"


def validate_managed_worktree(
    repo: Path,
    worktree: Path,
    branch: str | None,
    *,
    allow_rebase: bool = False,
) -> None:
    """Prove that a path is registered to this repository and expected branch."""

    if not worktree.is_dir():
        raise WorkspaceOwnershipError(f"관리 worktree 경로가 없습니다: {worktree}")
    expected_path = _canonical_path(worktree)
    record = next(
        (
            item
            for item in _worktree_catalog(repo)
            if _canonical_path(Path(str(item.get("worktree", "")))) == expected_path
        ),
        None,
    )
    if record is None:
        raise WorkspaceOwnershipError(
            f"Git에 등록되지 않은 관리 worktree입니다: {worktree}"
        )
    actual_branch = record.get("branch")
    if branch is None:
        if "detached" not in record:
            raise WorkspaceOwnershipError(
                f"planning worktree가 detached HEAD가 아닙니다: {worktree}"
            )
    elif actual_branch != f"refs/heads/{branch}":
        if (
            allow_rebase
            and "detached" in record
            and _rebase_in_progress_for_branch(worktree, branch)
        ):
            return
        rendered = actual_branch or "detached"
        raise WorkspaceOwnershipError(
            f"관리 worktree branch 불일치: {worktree} "
            f"(expected refs/heads/{branch}, actual {rendered})"
        )

def validate_reserved_worktree(
    repo: Path,
    worktree: Path,
    branch: str | None,
    expected_head: str,
) -> None:
    """Prove that a reserved path is the clean worktree Manager intended."""

    if not expected_head:
        raise WorkspaceOwnershipError(
            "예약 workspace의 기준 commit SHA가 저장되지 않았습니다."
        )
    validate_managed_worktree(repo, worktree, branch)
    snapshot = capture_git_snapshot(worktree)
    violations: list[str] = []
    if snapshot.head != expected_head:
        violations.append(
            f"HEAD {snapshot.head} (expected {expected_head})"
        )
    if snapshot.dirty:
        paths = ", ".join(sorted(snapshot.dirty)[:8])
        violations.append(f"변경 파일 {paths}")
    if snapshot.operations:
        operations = ", ".join(sorted(snapshot.operations))
        violations.append(f"Git operation {operations}")
    if violations:
        raise WorkspaceOwnershipError(
            "예약 workspace가 저장된 생성 기준과 다릅니다: "
            + "; ".join(violations)
        )



def make_worktree(
    repo: Path,
    worktree: Path,
    branch: str,
    base: str,
    *,
    allow_recovery: bool = True,
    allow_existing: bool = True,
    allow_rebase: bool = False,
    allow_create_branch: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
    before_create: Callable[[], None] | None = None,
) -> str:
    """Create, recover, or validate one owned ExecPlan worktree."""

    if ownership_reserved and not expected_head:
        raise WorkspaceOwnershipError(
            "예약 ExecPlan workspace의 기준 commit SHA가 없습니다."
        )
    if worktree.exists() and not worktree.is_dir():
        raise WorkspaceOwnershipError(
            f"관리 worktree 경로가 디렉터리가 아닙니다: {worktree}"
        )
    if worktree.is_dir():
        if not allow_existing:
            raise WorkspaceOwnershipError(
                f"새 ExecPlan의 관리 worktree가 이미 존재해 소유권을 증명할 수 없습니다: "
                f"{worktree}"
            )
        if ownership_reserved:
            validate_reserved_worktree(repo, worktree, branch, str(expected_head))
        else:
            validate_managed_worktree(
                repo, worktree, branch, allow_rebase=allow_rebase
            )
        return "existing"

    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "prune", "--expire", "now")
    branch_exists = (
        git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode
        == 0
    )
    if branch_exists:
        if (allow_create_branch and not ownership_reserved) or not allow_recovery:
            raise WorkspaceOwnershipError(
                f"새 ExecPlan의 관리 branch가 이미 존재해 소유권을 증명할 수 없습니다: "
                f"{branch}"
            )
        for record in _worktree_catalog(repo):
            if record.get("branch") == f"refs/heads/{branch}":
                owner = Path(str(record["worktree"]))
                raise WorkspaceOwnershipError(
                    f"관리 branch가 다른 worktree에 연결되어 있습니다: "
                    f"{branch} ({owner})"
                )
        if ownership_reserved:
            branch_head = git(
                repo,
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}",
            ).stdout.strip()
            if branch_head != expected_head:
                raise WorkspaceOwnershipError(
                    "예약 ExecPlan branch가 저장된 기준 commit과 다릅니다: "
                    f"{branch} ({branch_head}, expected {expected_head})"
                )
        git(repo, "worktree", "add", str(worktree), branch)
        outcome = "recovered"
    elif allow_create_branch or ownership_reserved:
        if before_create is not None:
            before_create()
        start_point = expected_head or f"origin/{base}"
        git(repo, "worktree", "add", "-b", branch, str(worktree), start_point)
        outcome = "created"
    else:
        raise WorkspaceOwnershipError(
            f"저장된 worktree와 관리 branch를 모두 복구할 수 없습니다: "
            f"{worktree} ({branch})"
        )
    if ownership_reserved:
        validate_reserved_worktree(repo, worktree, branch, str(expected_head))
    else:
        validate_managed_worktree(repo, worktree, branch, allow_rebase=allow_rebase)
    return outcome


def make_planning_worktree(
    repo: Path,
    worktree: Path,
    base: str,
    *,
    allow_create: bool = True,
    before_create: Callable[[], None] | None = None,
    allow_existing: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
) -> str:
    if ownership_reserved and not expected_head:
        raise WorkspaceOwnershipError(
            "예약 planning workspace의 기준 commit SHA가 없습니다."
        )
    if worktree.exists() and not worktree.is_dir():
        raise WorkspaceOwnershipError(
            f"planning worktree 경로가 디렉터리가 아닙니다: {worktree}"
        )
    if worktree.is_dir():
        if not allow_existing:
            raise WorkspaceOwnershipError(
                "새 planning worktree 경로가 이미 존재해 소유권을 증명할 수 "
                f"없습니다: {worktree}"
            )
        if ownership_reserved:
            validate_reserved_worktree(repo, worktree, None, str(expected_head))
        else:
            validate_managed_worktree(repo, worktree, None)
        return "existing"
    if not allow_create:
        raise WorkspaceOwnershipError(
            f"보존된 planning worktree를 복구할 branch가 없습니다: "
            f"{worktree}"
        )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if before_create is not None:
        before_create()
    start_point = expected_head or f"origin/{base}"
    git(repo, "worktree", "add", "--detach", str(worktree), start_point)
    if ownership_reserved:
        validate_reserved_worktree(repo, worktree, None, str(expected_head))
    else:
        validate_managed_worktree(repo, worktree, None)
    return "created"


def expected_execplan_workspace(
    state_dir: Path,
    repo: str,
    plan_id: str,
) -> tuple[str, Path]:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return (
        f"loop-manager/{plan_id}",
        state_dir / "worktrees" / project_id / plan_id,
    )


def validate_execplan_workspace_identity(
    state_dir: Path,
    repo: str,
    plan_id: str,
    branch: str,
    worktree: Path,
) -> None:
    expected_branch, expected_worktree = expected_execplan_workspace(
        state_dir, repo, plan_id
    )
    if branch != expected_branch:
        raise WorkspaceOwnershipError(
            f"ExecPlan 관리 branch가 결정적 소유 경계와 다릅니다: "
            f"{branch} (expected {expected_branch})"
        )
    _validate_managed_path_boundary(
        state_dir,
        worktree,
        expected_worktree,
        "ExecPlan worktree",
    )


def _nul_git_paths(repo: Path, *arguments: str) -> list[str]:
    return [
        os.fsdecode(record)
        for record in git_bytes(repo, *arguments).split(b"\0")
        if record
    ]


def _index_entries(worktree: Path) -> dict[str, tuple[str, ...]]:
    entries: dict[str, list[str]] = {}
    for record in git_bytes(
        worktree, "ls-files", "--stage", "-v", "-z"
    ).split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise RuntimeError("Git index snapshot을 해석할 수 없습니다.")
        entries.setdefault(os.fsdecode(raw_path), []).append(
            metadata.decode("ascii", errors="replace")
        )
    return {
        path: tuple(sorted(metadata))
        for path, metadata in entries.items()
    }


def _non_default_index_flags(entries: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            entry.split(" ", 1)[0]
            for entry in entries
            if not entry.startswith("H ")
        )
    )


def _shared_ref_entries(worktree: Path) -> dict[str, tuple[str, str]]:
    refs: dict[str, tuple[str, str]] = {}
    output = git(
        worktree,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(symref)",
    ).stdout
    for line in output.splitlines():
        fields = line.split("\0")
        if len(fields) != 3 or not fields[0]:
            raise RuntimeError("Git ref snapshot을 해석할 수 없습니다.")
        refname, objectname, symref = fields
        if refname in refs:
            raise RuntimeError(f"Git ref snapshot에 중복 ref가 있습니다: {refname}")
        refs[refname] = (objectname, symref)
    return refs


def _registered_worktree_entries(
    worktree: Path,
) -> dict[str, tuple[str, ...]]:
    entries: dict[str, tuple[str, ...]] = {}
    active_path = _canonical_path(worktree)
    for record in _worktree_catalog(worktree):
        raw_path = record.get("worktree")
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeError("Git worktree snapshot에 경로가 없습니다.")
        path = str(_canonical_path(Path(raw_path)))
        if path in entries:
            raise RuntimeError(
                f"Git worktree snapshot에 중복 경로가 있습니다: {path}"
            )
        state = [
            f"{key}={value}" for key, value in record.items() if key != "worktree"
        ]
        if _canonical_path(Path(raw_path)) != active_path:
            state.append(f"state={_worktree_state_signature(Path(raw_path))}")
        entries[path] = tuple(sorted(state))
    return entries


def _normalized_config_origin(worktree: Path, raw_origin: bytes) -> bytes:
    if not raw_origin.startswith(b"file:"):
        return raw_origin
    origin_path = Path(os.fsdecode(raw_origin.removeprefix(b"file:")))
    if not origin_path.is_absolute():
        origin_path = worktree / origin_path
    return b"file:" + os.fsencode(_canonical_path(origin_path))


def _config_entries(worktree: Path) -> dict[str, tuple[str, ...]]:
    records = git_bytes(
        worktree,
        "config",
        "--null",
        "--show-origin",
        "--show-scope",
        "--list",
        "--includes",
    ).split(b"\0")
    if records and not records[-1]:
        records.pop()
    if len(records) % 3:
        raise RuntimeError("Git config snapshot을 해석할 수 없습니다.")

    entries: dict[str, list[str]] = {}
    for offset in range(0, len(records), 3):
        raw_scope, raw_origin, definition = records[offset : offset + 3]
        raw_name, separator, raw_value = definition.partition(b"\n")
        if not raw_scope or not raw_origin or not raw_name or not separator:
            raise RuntimeError("Git config snapshot을 해석할 수 없습니다.")
        scope = raw_scope.decode("ascii", errors="replace")
        name = raw_name.decode("utf-8", errors="replace")
        origin = _normalized_config_origin(worktree, raw_origin)
        digest = hashlib.sha256(origin + b"\0" + raw_value).hexdigest()
        entries.setdefault(f"{scope}:{name}", []).append(digest)
    return {
        key: tuple(sorted(values))
        for key, values in entries.items()
    }


_GIT_OPERATION_PATHS = (
    "MERGE_HEAD",
    "MERGE_MODE",
    "MERGE_MSG",
    "AUTO_MERGE",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "BISECT_HEAD",
    "BISECT_LOG",
    "BISECT_START",
    "BISECT_TERMS",
)


def _git_operation_path_signature(path: Path) -> str:
    digest = hashlib.sha256()

    def include(candidate: Path, relative: str) -> None:
        stat = candidate.lstat()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_mode).encode("ascii"))
        digest.update(b"\0")
        if candidate.is_symlink():
            digest.update(os.fsencode(os.readlink(candidate)))
        elif candidate.is_file():
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)

    include(path, ".")
    if path.is_dir() and not path.is_symlink():
        for candidate in sorted(
            path.rglob("*"),
            key=lambda entry: entry.relative_to(path).as_posix(),
        ):
            include(candidate, candidate.relative_to(path).as_posix())
    return digest.hexdigest()


def _git_operation_entries(worktree: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for name in _GIT_OPERATION_PATHS:
        resolved = git(
            worktree,
            "rev-parse",
            "--git-path",
            name,
        ).stdout.strip()
        path = Path(resolved)
        if not path.is_absolute():
            path = worktree / path
        if path.exists() or path.is_symlink():
            entries[name] = _git_operation_path_signature(path)
    return entries


def _metadata_changes_after_block(
    before: Mapping[str, object],
    blocked: Mapping[str, object],
    current: Mapping[str, object],
    candidates: frozenset[str],
) -> frozenset[str]:
    return frozenset(
        key
        for key in candidates
        if before.get(key) == blocked.get(key)
        and blocked.get(key) != current.get(key)
    )


def _path_signature(worktree: Path, relative_path: str) -> str:
    path = worktree / relative_path
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    digest = hashlib.sha256()
    digest.update(str(stat.st_mode).encode("ascii"))
    digest.update(b"\0")
    try:
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.is_dir():
            head = git(path, "rev-parse", "--verify", "HEAD", check=False)
            status = git(
                path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                check=False,
            )
            digest.update(head.stdout.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(status.stdout.encode("utf-8", errors="replace"))
        else:
            digest.update(f"special:{stat.st_size}".encode("ascii"))
    except OSError as error:
        digest.update(f"unreadable:{type(error).__name__}".encode("ascii"))
    return digest.hexdigest()


def _capture_local_worktree_state(
    worktree: Path,
) -> tuple[
    str,
    str | None,
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], str]],
]:
    """Capture one worktree's HEAD, branch, index and file state."""

    head = git(worktree, "rev-parse", "--verify", "HEAD").stdout.strip()
    branch_result = git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    index = _index_entries(worktree)
    path_kinds: dict[str, set[str]] = {}
    path_groups = (
        (
            "unstaged",
            _nul_git_paths(
                worktree,
                "diff",
                "--name-only",
                "-z",
                "--ignore-submodules=none",
            ),
        ),
        (
            "staged",
            _nul_git_paths(
                worktree,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--ignore-submodules=none",
            ),
        ),
        (
            "untracked",
            _nul_git_paths(
                worktree,
                "ls-files",
                "--others",
                "-z",
            ),
        ),
    )
    for kind, paths in path_groups:
        for relative_path in paths:
            path_kinds.setdefault(relative_path, set()).add(kind)
    for relative_path, entries in index.items():
        if _non_default_index_flags(entries):
            path_kinds.setdefault(relative_path, set()).add("index-hidden")
    dirty = {
        path: (tuple(sorted(kinds)), _path_signature(worktree, path))
        for path, kinds in sorted(path_kinds.items())
    }
    return head, branch, index, dirty


def _worktree_state_signature(worktree: Path) -> str:
    if not worktree.is_dir():
        return "missing"
    head, branch, index, dirty = _capture_local_worktree_state(worktree)
    encoded = json.dumps(
        {
            "head": head,
            "branch": branch,
            "index": index,
            "dirty": dirty,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_git_snapshot(worktree: Path) -> GitSnapshot:
    """Capture local and repository-wide Git state without staging content."""

    head, branch, index, dirty = _capture_local_worktree_state(worktree)
    return GitSnapshot(
        head=head,
        branch=branch,
        index=index,
        dirty=dirty,
        refs=_shared_ref_entries(worktree),
        worktrees=_registered_worktree_entries(worktree),
        config=_config_entries(worktree),
        operations=_git_operation_entries(worktree),
    )


def agent_snapshot_changes(
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
) -> list[AgentChange]:
    changes: dict[str, set[str]] = {}
    for path in set(before.index) | set(after.index):
        before_entries = before.index.get(path, ())
        after_entries = after.index.get(path, ())
        if before_entries != after_entries:
            changes.setdefault(path, set()).add("index")
            before_flags = _non_default_index_flags(before_entries)
            after_flags = _non_default_index_flags(after_entries)
            if before_flags != after_flags:
                changes[path].add("index flags")
    for path in set(before.dirty) | set(after.dirty):
        if before.dirty.get(path) != after.dirty.get(path):
            before_kinds = before.dirty.get(path, ((), ""))[0]
            after_kinds = after.dirty.get(path, ((), ""))[0]
            changes.setdefault(path, set()).update(before_kinds)
            changes[path].update(after_kinds)
            changes[path].add("worktree")
    if before.head != after.head:
        for path in _nul_git_paths(
            worktree,
            "diff",
            "--name-only",
            "-z",
            before.head,
            after.head,
        ):
            changes.setdefault(path, set()).add("HEAD")
    return [
        AgentChange(path, tuple(sorted(kinds)))
        for path, kinds in sorted(changes.items())
    ]


def _worker_path_allowed(path: str, selected_plan_path: str) -> bool:
    normalized = path.replace("\\", "/")
    lowered = normalized.casefold()
    if normalized == selected_plan_path:
        return True
    if lowered in {"docs/product.md", "docs/future_ideas.md"}:
        return True
    if Path(normalized).name.casefold() == "agents.md":
        return False
    if lowered in {"docs/architecture.md", "docs/plans.md"}:
        return False
    if lowered.startswith("docs/requirements/"):
        return False
    if re.fullmatch(r"docs/plans/[^/]+\.md", normalized):
        return False
    return True


def validate_agent_scope(
    role_name: str,
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    selected_plan_path: str | None = None,
    allowed_ref_changes: frozenset[str] = frozenset(),
    allowed_worktree_changes: frozenset[str] = frozenset(),
) -> None:
    """Validate the net Git mutation made by one Agent role."""

    changes = agent_snapshot_changes(worktree, before, after)
    violations: list[str] = []
    if before.head != after.head:
        violations.append(f"HEAD 변경 ({before.head} -> {after.head})")
    if before.branch != after.branch:
        violations.append(
            f"branch 전환 ({before.branch or 'detached'} -> "
            f"{after.branch or 'detached'})"
        )

    if not before.shared_metadata_complete or not after.shared_metadata_complete:
        violations.append("공유 Git metadata 기준 snapshot 누락")
    else:
        metadata_states = (
            (
                "공유 Git refs",
                before.refs,
                after.refs,
                allowed_ref_changes,
            ),
            (
                "Git worktree 등록·HEAD·index·파일 상태",
                before.worktrees,
                after.worktrees,
                allowed_worktree_changes,
            ),
            (
                "Git config",
                before.config,
                after.config,
                frozenset(),
            ),
            (
                "Git operation 상태",
                before.operations,
                after.operations,
                frozenset(),
            ),
        )
        for label, before_state, after_state, allowed_changes in metadata_states:
            changed_keys = sorted(
                key
                for key in set(before_state) | set(after_state)
                if before_state.get(key) != after_state.get(key)
                and key not in allowed_changes
            )
            if changed_keys:
                displayed = ", ".join(changed_keys[:8])
                remainder = len(changed_keys) - 8
                if remainder > 0:
                    displayed += f", 외 {remainder}개"
                violations.append(f"{label} 변경 ({displayed})")

    for change in changes:
        if "index flags" in change.kinds:
            violations.append(
                f"{change.path} ({', '.join(change.kinds)})"
            )
            continue
        allowed = False
        if role_name == "planner":
            allowed = bool(
                re.fullmatch(r"docs/plans/[^/]+\.md", change.path)
                and change.path not in before.index
            )
        elif role_name == "worker":
            if selected_plan_path is None:
                raise ValueError("Worker scope 검증에 선택된 ExecPlan 경로가 없습니다.")
            allowed = _worker_path_allowed(change.path, selected_plan_path)
        elif role_name == "reviewer":
            allowed = False
        else:
            raise ValueError(f"알 수 없는 Agent 역할입니다: {role_name}")
        if not allowed:
            violations.append(
                f"{change.path} ({', '.join(change.kinds) or 'Git 상태'})"
            )

    if violations:
        raise AgentScopeError(
            f"{role_name} Git 범위 위반: " + "; ".join(violations),
            result_snapshot=after,
        )


def _same_local_snapshot(left: GitSnapshot, right: GitSnapshot) -> bool:
    return (
        left.head == right.head
        and left.branch == right.branch
        and left.index == right.index
        and left.dirty == right.dirty
        and left.operations == right.operations
    )


def _git_with_index(
    worktree: Path,
    index_path: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index_path)
    return command(
        ["git", "-C", str(worktree), *arguments],
        env=environment,
    )


def _hash_git_blob(worktree: Path, contents: bytes) -> str:
    result = subprocess.run(
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
) -> str:
    """Write the exact dirty worktree state through an isolated index."""

    with tempfile.TemporaryDirectory(prefix="loop-manager-index-") as directory:
        temporary_index = Path(directory) / "index"
        _git_with_index(worktree, temporary_index, "read-tree", index_tree)
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
                )
                continue

            if stat.S_ISREG(target_stat.st_mode):
                mode = "100755" if target_stat.st_mode & 0o111 else "100644"
                object_id = _hash_git_blob(worktree, target.read_bytes())
            elif stat.S_ISLNK(target_stat.st_mode):
                mode = "120000"
                object_id = _hash_git_blob(
                    worktree,
                    os.fsencode(os.readlink(target)),
                )
            elif stat.S_ISDIR(target_stat.st_mode):
                nested_head = git(
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
            )
        return _git_with_index(
            worktree,
            temporary_index,
            "write-tree",
        ).stdout.strip()


def _commit_tree(
    worktree: Path,
    tree: str,
    parent: str,
    message: str,
) -> str:
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
    return command(
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
) -> str | None:
    if not snapshot.dirty:
        return None
    if snapshot.operations:
        # Rebase-conflict resolution intentionally starts inside a Git
        # operation. Its scope is still validated, but it cannot use the
        # ordinary HEAD/index tree recovery representation.
        return None
    index_tree = git(worktree, "write-tree").stdout.strip()
    worktree_tree = _agent_worktree_tree(worktree, snapshot, index_tree)
    baseline_id = hashlib.sha256(
        f"{snapshot.head}\0{index_tree}\0{worktree_tree}".encode("ascii")
    ).hexdigest()
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    refname = f"refs/loop-manager/baselines/{project_id}/{baseline_id}"
    index_commit = _commit_tree(
        worktree,
        index_tree,
        snapshot.head,
        f"Loop Manager baseline index {baseline_id}",
    )
    worktree_commit = _commit_tree(
        worktree,
        worktree_tree,
        index_commit,
        f"Loop Manager baseline worktree {baseline_id}",
    )
    existing = git(
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
        git(worktree, "update-ref", refname, worktree_commit)
    return json.dumps(
        {
            "version": 1,
            "ref": refname,
            "ref_target": worktree_commit,
            "index_tree": index_tree,
            "worktree_tree": worktree_tree,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def begin_agent_scope(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    role_name: str,
    *,
    input_fingerprint: str | None = None,
) -> GitSnapshot:
    row = connection.execute(
        "SELECT agent_role, agent_snapshot, plan_path "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Agent scope를 저장할 프로젝트가 없습니다.")
    if row["agent_snapshot"]:
        if row["agent_role"] != role_name:
            raise RuntimeError(
                f"저장된 Agent 역할이 현재 역할과 다릅니다: "
                f"{row['agent_role']} -> {role_name}"
            )
        return GitSnapshot.deserialize(str(row["agent_snapshot"]))
    preliminary_snapshot = capture_git_snapshot(worktree)
    agent_baseline = _capture_agent_baseline(
        repo,
        worktree,
        preliminary_snapshot,
    )
    snapshot = (
        capture_git_snapshot(worktree)
        if agent_baseline is not None
        else preliminary_snapshot
    )
    if not _same_local_snapshot(preliminary_snapshot, snapshot):
        raise RuntimeError("Agent 시작 상태를 보존하는 동안 worktree가 변경됐습니다.")
    plan_path = str(row["plan_path"] or "")
    plan_file = worktree / plan_path
    agent_input_plan = (
        plan_file.read_text(encoding="utf-8")
        if role_name == "reviewer" and plan_path and plan_file.is_file()
        else None
    )
    update_project(
        connection,
        repo,
        agent_role=role_name,
        agent_snapshot=snapshot.serialize(),
        agent_input_fingerprint=input_fingerprint,
        agent_baseline=agent_baseline,
        agent_recovery=None,
        agent_input_plan=agent_input_plan,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )
    return snapshot


def clear_agent_scope(connection: sqlite3.Connection, repo: str) -> None:
    update_project(
        connection,
        repo,
        agent_role=None,
        agent_snapshot=None,
        agent_baseline=None,
        agent_recovery=None,
        agent_input_plan=None,
        agent_result_plan=None,
        agent_input_fingerprint=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def validate_planning_workspace_identity(
    state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    expected = (
        state_dir / "worktrees" / project_id / "planning" / fingerprint
    )
    _validate_managed_path_boundary(
        state_dir,
        worktree,
        expected,
        "planning worktree",
    )


def validate_planning_worktree_ownership(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    """Require confirmed Manager ownership before removing a planning worktree."""

    project = connection.execute(
        "SELECT worktree, planning_workspace_owned FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if worktree.exists() and (
        project is None
        or int(project["planning_workspace_owned"]) != WORKSPACE_OWNED
        or Path(str(project["worktree"] or "")) != worktree
    ):
        raise WorkspaceOwnershipError(
            "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}"
        )

    if not fingerprint:
        raise WorkspaceOwnershipError(
            "planning worktree의 입력 fingerprint가 저장되지 않았습니다."
        )
    validate_planning_workspace_identity(
        state_dir,
        repo,
        fingerprint,
        worktree,
    )
    if worktree.exists():
        validate_managed_worktree(repo_path, worktree, None)


def changed_files(worktree: Path, since: str | None = None) -> list[str]:
    if since:
        output = git(worktree, "diff", "--name-only", f"{since}..HEAD").stdout
        return sorted(line for line in output.splitlines() if line)
    unstaged = git(worktree, "diff", "--name-only").stdout.splitlines()
    staged = git(worktree, "diff", "--cached", "--name-only").stdout.splitlines()
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted(set(unstaged + staged + untracked))


def branch_has_changes(worktree: Path, base: str) -> bool:
    result = git(
        worktree,
        "diff",
        "--quiet",
        f"origin/{base}...HEAD",
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            f"관리 branch 변경 확인에 실패했습니다 (git 종료 코드 {result.returncode})."
        )
    return result.returncode == 1


def assess_cleanup(worktree: Path, pr_head_oid: str | None) -> CleanupAssessment:
    """Classify a managed worktree without changing it or shared Git state."""
    if not worktree.exists():
        return CleanupAssessment("missing")
    try:
        status_lines = git(
            worktree, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout.splitlines()
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 상태 확인 실패: {error}")

    dirty_kinds: list[str] = []
    if any(line.startswith("??") for line in status_lines):
        dirty_kinds.append("untracked 파일")
    if any(len(line) >= 2 and line[0] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("staged 변경")
    if any(len(line) >= 2 and line[1] not in {" ", "?"} for line in status_lines):
        dirty_kinds.append("tracked 파일 수정")
    if dirty_kinds:
        return CleanupAssessment("dirty", ", ".join(dirty_kinds))

    if not pr_head_oid:
        return CleanupAssessment("unverifiable", "PR head commit 정보가 없습니다.")
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", pr_head_oid) is None:
        return CleanupAssessment("unverifiable", "PR head commit OID 형식이 유효하지 않습니다.")
    try:
        local_head = git(worktree, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        head_object = git(
            worktree, "cat-file", "-e", f"{pr_head_oid}^{{commit}}", check=False
        )
        if head_object.returncode:
            return CleanupAssessment(
                "unverifiable", f"PR head commit object를 찾을 수 없습니다: {pr_head_oid}"
            )
        relation = git(
            worktree,
            "merge-base",
            "--is-ancestor",
            local_head,
            pr_head_oid,
            check=False,
        )
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"commit 관계 확인 실패: {error}")
    if relation.returncode == 0:
        return CleanupAssessment("safe")
    if relation.returncode == 1:
        return CleanupAssessment(
            "local-only commits", "로컬 HEAD에 PR head에 포함되지 않은 commit이 있습니다."
        )
    return CleanupAssessment(
        "unverifiable", f"commit 관계 확인 실패 (git 종료 코드 {relation.returncode})"
    )


def validate_pending_cleanup_worktree(
    repo_path: Path,
    state_dir: Path,
    repo: str,
    worktree: Path,
) -> None:
    """Prove a pending cleanup path belongs to its deterministic ExecPlan."""

    if not worktree.exists():
        return
    branch_result = git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
    )
    if branch_result.returncode:
        raise WorkspaceOwnershipError(
            "보존된 cleanup worktree가 branch에 연결되어 있지 않습니다."
        )
    branch = branch_result.stdout.strip()
    match = re.fullmatch(r"loop-manager/(G-\d+)", branch)
    if match is None:
        raise WorkspaceOwnershipError(
            f"보존된 cleanup worktree branch의 소유권을 증명할 수 없습니다: {branch}"
        )
    plan_id = match.group(1)
    validate_execplan_workspace_identity(
        state_dir,
        repo,
        plan_id,
        branch,
        worktree,
    )
    validate_managed_worktree(repo_path, worktree, branch)


def remove_cleanup_worktree(
    repo: Path, worktree: Path, assessment: CleanupAssessment
) -> CleanupAssessment:
    if assessment.status != "safe":
        return assessment
    try:
        git(repo, "worktree", "remove", str(worktree))
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 제거 실패: {error}")
    return assessment


def _ensure_cleanup_pending_schema(connection: sqlite3.Connection) -> None:
    connection.execute(CLEANUP_PENDING_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cleanup_pending)")
    }
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE cleanup_pending ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 1"
        )


def _store_cleanup_pending(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
    reason: str,
    *,
    workspace_owned: int = WORKSPACE_OWNED,
) -> None:
    connection.execute(
        """
        INSERT INTO cleanup_pending (
            repo, worktree, pr_url, pr_head_oid, reason, workspace_owned
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, worktree) DO UPDATE SET
            pr_url = excluded.pr_url,
            pr_head_oid = excluded.pr_head_oid,
            reason = excluded.reason,
            workspace_owned = excluded.workspace_owned,
            updated_at = CURRENT_TIMESTAMP
        """,
        (repo, str(worktree), pr_url, pr_head_oid, reason, workspace_owned),
    )


def recheck_cleanup_pending(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
) -> list[sqlite3.Row]:
    _ensure_cleanup_pending_schema(connection)
    pending = connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()
    for item in pending:
        worktree = Path(item["worktree"])
        if int(item["workspace_owned"]) != WORKSPACE_OWNED:
            if not worktree.exists() and not worktree.is_symlink():
                connection.execute(
                    "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                    (repo, str(worktree)),
                )
            continue
        try:
            validate_pending_cleanup_worktree(
                repo_path,
                state_dir,
                repo,
                worktree,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            assessment = CleanupAssessment(
                "unverifiable",
                f"cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, item["pr_head_oid"])
            assessment = remove_cleanup_worktree(repo_path, worktree, assessment)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            connection.execute(
                """
                UPDATE cleanup_pending
                SET reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND worktree = ?
                """,
                (assessment.reason or assessment.status, repo, str(worktree)),
            )
    connection.commit()
    return connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()


def planning_diagnostics_path(
    state_dir: Path,
    repo: str,
    fingerprint: str,
) -> Path:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return state_dir / "logs" / project_id / "planning" / f"{fingerprint}.txt"


def preserve_planning_diagnostics(
    state_dir: Path,
    repo: str,
    row: sqlite3.Row,
) -> Path:
    fingerprint = row["planning_attempt_fingerprint"]
    if not fingerprint:
        fingerprint = row["planning_error_fingerprint"] or "unknown"
    worktree = Path(row["worktree"]) if row["worktree"] else None
    error = (
        row["planning_error"]
        or row["planning_validation_error"]
        or row["last_error"]
        or ""
    )
    diff = ""
    if worktree and worktree.is_dir():
        try:
            base_revision = row["active_sha"] or "HEAD"
            diff = git(worktree, "diff", "--binary", base_revision).stdout
            for relative_path in _nul_git_paths(
                worktree, "ls-files", "--others", "-z"
            ):
                untracked = git(
                    worktree,
                    "diff",
                    "--binary",
                    "--no-index",
                    "--",
                    os.devnull,
                    relative_path,
                    check=False,
                )
                if untracked.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"untracked diff 생성 실패 ({relative_path}): "
                        f"git 종료 코드 {untracked.returncode}"
                    )
                diff += untracked.stdout
        except RuntimeError as diff_error:
            diff = f"diff 보존 실패: {diff_error}"
    diagnostic = planning_diagnostics_path(state_dir, repo, fingerprint)
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    diagnostic.write_text(
        "\n".join(
            [
                f"input_fingerprint: {fingerprint}",
                f"planning_worktree: {worktree or ''}",
                f"planner_session_id: {row['planner_session_id'] or ''}",
                f"validation_retries: {row['planning_retry_count']}",
                "",
                "last_error:",
                str(error),
                "",
                "diff:",
                diff,
            ]
        ),
        encoding="utf-8",
    )
    return diagnostic


def clear_planning_validation_state(
    connection: sqlite3.Connection,
    repo: str,
) -> None:
    update_project(
        connection,
        repo,
        planning_attempt_fingerprint=None,
        planning_retry_count=0,
        planner_session_id=None,
        worker_session_id=None,
        reviewer_session_id=None,
        worker_result=None,
        verify_result=None,
        review_result=None,
        planning_validation_error=None,
        planning_result_pending_validation=0,
        planning_result_completed=0,
        planning_error=None,
        planning_error_fingerprint=None,
        last_error=None,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_baseline=None,
        agent_recovery=None,
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def _clear_active_values(
    *,
    processed_sha: str | None = None,
    planning_fingerprint: str | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "active_sha": None,
        "plan_sha": None,
        "phase": None,
        "branch": None,
        "worktree": None,
        "plan_path": None,
        "pr_url": None,
        "last_error": None,
        "planning_attempt_fingerprint": None,
        "planning_workspace_owned": 0,
        "planning_retry_count": 0,
        "planner_session_id": None,
        "worker_session_id": None,
        "reviewer_session_id": None,
        "worker_result": None,
        "verify_result": None,
        "review_result": None,
        "planning_validation_error": None,
        "planning_result_pending_validation": 0,
        "planning_result_completed": 0,
        "planning_error": None,
        "planning_error_fingerprint": None,
        "agent_role": None,
        "agent_snapshot": None,
        "agent_baseline": None,
        "agent_recovery": None,
        "agent_input_plan": None,
        "agent_result_plan": None,
        "agent_input_fingerprint": None,
        "agent_result_snapshot": None,
        "agent_manager_snapshot": None,
    }
    if processed_sha is not None:
        values["processed_sha"] = processed_sha
    if planning_fingerprint is not None:
        values["planning_fingerprint"] = planning_fingerprint
    return values


def clear_active(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    update_project(
        connection,
        repo,
        **_clear_active_values(
            processed_sha=processed_sha,
            planning_fingerprint=planning_fingerprint,
        ),
    )


def _execplan_input_paths(plan: sqlite3.Row) -> list[str]:
    sources = json.loads(plan["sources"])
    return [str(plan["path"]), *(str(source) for source in sources)]


def _execplan_input_fingerprint(entries: list[tuple[str, str | None]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-blocked-input-v1\0")
    for relative_path, contents in entries:
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        if contents is None:
            digest.update(b"\0")
            continue
        normalized = contents.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        digest.update(b"\1")
        digest.update(len(normalized).to_bytes(8, "big"))
        digest.update(normalized)
    return digest.hexdigest()


def _worktree_execplan_input_fingerprint(worktree: Path, plan: sqlite3.Row) -> str:
    entries = []
    for relative_path in _execplan_input_paths(plan):
        path = worktree / relative_path
        contents = path.read_text(encoding="utf-8") if path.is_file() else None
        entries.append((relative_path, contents))
    return _execplan_input_fingerprint(entries)


def _revision_execplan_input_fingerprint(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    entries = []
    for relative_path in _execplan_input_paths(plan):
        shown = git(repo_path, "show", f"{revision}:{relative_path}", check=False)
        entries.append((relative_path, shown.stdout if shown.returncode == 0 else None))
    return _execplan_input_fingerprint(entries)


def _execplan_metadata_identity_error(
    repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
    subject: str,
) -> str | None:
    path = str(plan["path"])
    plan_sha = str(plan["plan_sha"] or "")
    if plan_sha:
        shown = git(repo_path, "show", f"{plan_sha}:{path}", check=False)
        if shown.returncode:
            return f"{subject}의 게시 메타데이터를 확인할 수 없습니다."
        try:
            published = published_execplan(path, shown.stdout)
        except RuntimeError as error:
            return f"{subject}의 게시 메타데이터가 유효하지 않습니다: {error}"
        metadata_fields = (
            "id",
            "title",
            "sources",
            "depends_on",
            "complexity",
            "risk",
            "priority",
        )
        if any(current[field] != published[field] for field in metadata_fields):
            return f"{subject}의 고정 메타데이터는 변경할 수 없습니다."
        return None

    sources, dependencies, priority = _execplan_metadata(current)
    if (
        current["id"] != plan["plan_id"]
        or current["title"] != plan["title"]
        or sources != plan["sources"]
        or dependencies != plan["depends_on"]
        or priority != plan["priority"]
    ):
        return f"{subject}의 SQLite 메타데이터와 현재 파일이 일치하지 않습니다."
    return None


def _blocked_execplan_metadata_identity_error(
    repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
) -> str | None:
    return _execplan_metadata_identity_error(
        repo_path,
        current,
        plan,
        "BLOCKED ExecPlan",
    )


def validate_active_execplan_metadata(
    repo_path: Path,
    worktree: Path,
    relative_path: str,
    plan: sqlite3.Row,
) -> dict[str, object]:
    current = validate_execplan(worktree, relative_path)
    error = _execplan_metadata_identity_error(
        repo_path,
        current,
        plan,
        "활성 ExecPlan",
    )
    if error is not None:
        raise RuntimeError(error)
    return current


def validate_worker_workspace_scope(
    repo_path: Path,
    worktree: Path,
    branch: str,
    selected_plan_path: str,
    catalog_plan: sqlite3.Row,
    before: GitSnapshot,
    *,
    allow_rebase: bool = False,
) -> dict[str, object]:
    """Validate one Worker-controlled operation before Manager Git writes."""

    after = capture_git_snapshot(worktree)
    validate_agent_scope(
        "worker",
        worktree,
        before,
        after,
        selected_plan_path=selected_plan_path,
    )
    plan = validate_active_execplan_metadata(
        repo_path,
        worktree,
        selected_plan_path,
        catalog_plan,
    )
    validate_managed_worktree(
        repo_path,
        worktree,
        branch,
        allow_rebase=allow_rebase,
    )
    return plan


def _blocked_execplan_metadata_error(
    repo_path: Path,
    worktree: Path,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    try:
        current = _parse_execplan(worktree, worktree / path)
    except (OSError, RuntimeError) as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"

    return _blocked_execplan_metadata_identity_error(repo_path, current, plan)


def _blocked_execplan_revision_metadata_error(
    repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str | None:
    path = str(plan["path"])
    shown = git(repo_path, "show", f"{revision}:{path}", check=False)
    if shown.returncode:
        return "BLOCKED ExecPlan을 remote 기준으로 다시 검증할 수 없습니다."
    try:
        current = published_execplan(path, shown.stdout)
    except RuntimeError as error:
        return f"BLOCKED ExecPlan을 다시 검증할 수 없습니다: {error}"
    return _blocked_execplan_metadata_identity_error(repo_path, current, plan)


def _update_blocked_error(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    error: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE exec_plans SET last_error = ? WHERE repo = ? AND plan_id = ?",
            (error, repo, plan_id),
        )
        connection.execute(
            "UPDATE projects SET last_error = ? WHERE repo = ?",
            (error, repo),
        )


def block_active_execplan(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    error: str,
    verify_result: str,
    *,
    preserved_agent_result_snapshot: str | None = None,
) -> None:
    project = connection.execute(
        "SELECT plan_path, agent_role, agent_snapshot, agent_input_fingerprint, "
        "agent_input_plan "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    plan_id = _plan_id_from_path(str(project["plan_path"] or "")) if project else None
    plan = (
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if plan_id
        else None
    )
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    blocked_worktree_fingerprint = _worktree_execplan_input_fingerprint(
        worktree, plan
    )
    fingerprint = str(
        project["agent_input_fingerprint"] or blocked_worktree_fingerprint
    )
    agent_result_snapshot = preserved_agent_result_snapshot
    if agent_result_snapshot is None and project["agent_snapshot"]:
        try:
            agent_result_snapshot = capture_git_snapshot(worktree).serialize()
        except RuntimeError:
            pass
    agent_result_plan = None
    if (
        project["agent_role"] == "reviewer"
        and project["agent_input_plan"] is not None
    ):
        plan_file = worktree / str(plan["path"])
        if plan_file.is_file():
            agent_result_plan = plan_file.read_text(encoding="utf-8")
    with connection:
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'BLOCKED', blocked_fingerprint = ?,
                blocked_worktree_fingerprint = ?, last_error = ?, verify_result = ?
            WHERE repo = ? AND plan_id = ?
            """,
            (
                fingerprint,
                blocked_worktree_fingerprint,
                error,
                verify_result,
                repo,
                plan_id,
            ),
        )
        connection.execute(
            """
            UPDATE projects
            SET phase = 'BLOCKED', last_error = ?, verify_result = ?,
                agent_result_snapshot = ?, agent_result_plan = ?,
                agent_manager_snapshot = NULL
            WHERE repo = ?
            """,
            (error, verify_result, agent_result_snapshot, agent_result_plan, repo),
        )


def transition_validation_failure(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    stage: str,
    output: str,
) -> ValidationFailureDecision:
    """Persist stage-local failures and own their retry/BLOCKED transition."""

    columns = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }
    labels = {"verify": "전체 verify", "review": "Review"}
    history_column = columns.get(stage)
    if history_column is None:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("검증 실패를 기록할 활성 ExecPlan을 찾을 수 없습니다.")
    failures = validation_failure_fingerprints(stage, output, worktree)
    history = _deserialize_failure_history(plan[history_column])
    recorded_failures = []
    for fingerprint, normalized in failures:
        previous = history.get(fingerprint, {"occurrences": 0})
        occurrences = int(previous["occurrences"]) + 1
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": normalized[-2000:],
        }
        recorded_failures.append((fingerprint, normalized, occurrences))
    blocking_failure = next(
        (
            failure
            for failure in recorded_failures
            if failure[2] > MAX_VALIDATION_FINGERPRINT_REPEATS
        ),
        None,
    )
    fingerprint, normalized, occurrences = blocking_failure or max(
        recorded_failures,
        key=lambda failure: failure[2],
    )
    serialized_history = json.dumps(
        history,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    blocked = blocking_failure is not None
    if blocked:
        error = (
            f"{labels[stage]} 동일 실패가 Worker 수정 후 "
            f"{MAX_VALIDATION_FINGERPRINT_REPEATS}회 다시 발생했습니다.\n"
            f"실패 지문: {fingerprint}\n{output[-8000:]}"
        )
        blocked_input = _worktree_execplan_input_fingerprint(worktree, plan)
    else:
        prefix = "전체 검증 실패" if stage == "verify" else "Review 실패"
        error = f"{prefix}:\n{output[-8000:]}"
        blocked_input = None

    execplan_assignments = [
        f"{history_column} = ?",
        "status = ?",
        "last_error = ?",
    ]
    execplan_values: list[object] = [
        serialized_history,
        "BLOCKED" if blocked else "IMPLEMENTING",
        error,
    ]
    project_assignments = ["phase = ?", "last_error = ?"]
    project_values: list[object] = ["BLOCKED" if blocked else "WORKING", error]
    if stage == "verify":
        execplan_assignments.append("verify_result = ?")
        execplan_values.append(output)
        project_assignments.append("verify_result = ?")
        project_values.append(output)
    else:
        execplan_assignments.append("review_result = ?")
        execplan_values.append(output)
        project_assignments.append("review_result = ?")
        project_values.append(output)
    if blocked:
        execplan_assignments.extend(
            ["blocked_fingerprint = ?", "blocked_worktree_fingerprint = ?"]
        )
        execplan_values.extend([blocked_input, blocked_input])
        project_assignments.append("agent_manager_snapshot = NULL")

    with connection:
        connection.execute(
            f"UPDATE exec_plans SET {', '.join(execplan_assignments)} "
            "WHERE repo = ? AND plan_id = ?",
            (*execplan_values, repo, plan["plan_id"]),
        )
        connection.execute(
            f"UPDATE projects SET {', '.join(project_assignments)} WHERE repo = ?",
            (*project_values, repo),
        )
    return ValidationFailureDecision(
        stage,
        fingerprint,
        occurrences,
        blocked,
    )


def block_active_execplan_from_revision(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    error: str,
    verify_result: str = "",
    *,
    preserved_agent_result_snapshot: str | None = None,
) -> None:
    """Block an active plan when its saved worktree is not safe to read."""

    plan = active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("차단할 활성 ExecPlan을 찾을 수 없습니다.")
    project = connection.execute(
        "SELECT agent_input_fingerprint FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    fingerprint = str(
        (project["agent_input_fingerprint"] if project else None)
        or _revision_execplan_input_fingerprint(repo_path, revision, plan)
    )
    with connection:
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'BLOCKED', blocked_fingerprint = ?,
                blocked_worktree_fingerprint = NULL, last_error = ?, verify_result = ?
            WHERE repo = ? AND plan_id = ?
            """,
            (fingerprint, error, verify_result, repo, plan["plan_id"]),
        )
        connection.execute(
            """
            UPDATE projects
            SET phase = 'BLOCKED', last_error = ?, verify_result = ?,
                agent_result_snapshot = ?, agent_manager_snapshot = NULL
            WHERE repo = ?
            """,
            (error, verify_result, preserved_agent_result_snapshot, repo),
        )


def block_persisted_agent_scope_violation(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    row: sqlite3.Row,
    detail: str,
    result_snapshot: GitSnapshot | None = None,
) -> str:
    """Persist a pre-resume scope violation without discarding Agent state."""

    message = f"Agent 범위 선검증 실패:\n{detail}"
    preserved_snapshot = (
        result_snapshot.serialize()
        if result_snapshot is not None
        else str(row["agent_snapshot"])
    )
    plan = active_execplan(connection, repo)
    if plan is None:
        if row["phase"] == "PLANNING":
            update_project(
                connection,
                repo,
                planning_validation_error=detail,
                last_error=message,
                agent_result_snapshot=preserved_snapshot,
            )
        else:
            update_project(
                connection,
                repo,
                phase="BLOCKED",
                last_error=message,
                agent_result_snapshot=preserved_snapshot,
            )
        return message

    saved_worktree = Path(str(row["worktree"] or ""))
    if saved_worktree.is_dir():
        block_active_execplan(
            connection,
            repo,
            saved_worktree,
            message,
            str(row["verify_result"] or ""),
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    else:
        revision = str(
            plan["plan_sha"]
            or row["active_sha"]
            or f"refs/remotes/origin/{row['base_branch']}"
        )
        block_active_execplan_from_revision(
            connection,
            repo_path,
            repo,
            revision,
            message,
            str(row["verify_result"] or ""),
            preserved_agent_result_snapshot=preserved_snapshot,
        )
    return message


def recover_persisted_agent_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
) -> None:
    """Recover a missing Agent worktree only from its proven owned branch."""

    worktree = Path(str(project["worktree"] or ""))
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    plan = active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    if (
        project["phase"] == "PLANNING"
        or plan_id is None
        or plan is None
        or plan["plan_id"] != plan_id
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or not branch
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "저장된 Agent worktree의 Loop Manager 소유권을 증명할 수 없습니다: "
            f"{worktree}"
        )
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            plan_id,
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"저장된 Agent worktree 복구 실패: {error}"
        ) from error


def _blocked_agent_local_baseline(
    before: GitSnapshot,
    result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    *,
    allowed_dirty_paths: frozenset[str] = frozenset(),
) -> GitSnapshot:
    """Accept a diagnosed result plus explicit user-edited input files."""

    if (
        not before.shared_metadata_complete
        or not result.shared_metadata_complete
        or not current.shared_metadata_complete
    ):
        raise AgentScopeError("저장된 BLOCKED Agent Git metadata 기준 snapshot이 없습니다.")

    new_local_changes: set[str] = set()
    for state_name, before_state, result_state, current_state in (
        ("index", before.index, result.index, current.index),
        ("dirty", before.dirty, result.dirty, current.dirty),
    ):
        for path in set(before_state) | set(result_state) | set(current_state):
            value = current_state.get(path)
            if (
                value not in (before_state.get(path), result_state.get(path))
                and not (state_name == "dirty" and path in allowed_dirty_paths)
            ):
                new_local_changes.add(path)
    if new_local_changes:
        displayed = ", ".join(sorted(new_local_changes)[:8])
        remainder = len(new_local_changes) - 8
        if remainder > 0:
            displayed += f", 외 {remainder}개"
        raise AgentScopeError(
            "BLOCKED 이후 새 파일 또는 index 변경을 자동 보존할 수 없습니다: "
            + displayed
        )

    worktree_key = str(_canonical_path(worktree))
    if current.worktrees.get(worktree_key) is None:
        raise AgentScopeError("BLOCKED Agent 결과에 관리 worktree 등록이 없습니다.")
    return GitSnapshot(
        head=before.head,
        branch=before.branch,
        index=current.index,
        dirty=current.dirty,
        refs=before.refs,
        worktrees=before.worktrees,
        config=before.config,
        operations=before.operations,
    )


def _diagnosed_blocked_commit_allowances(
    before: GitSnapshot,
    blocked_result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    scope_before: GitSnapshot,
) -> tuple[GitSnapshot, frozenset[str], frozenset[str]]:
    """Allow only the exact diagnosed commit while it awaits isolation."""

    if blocked_result.head == before.head:
        return scope_before, frozenset(), frozenset()
    if (
        before.branch is None
        or blocked_result.branch != before.branch
        or current.branch != before.branch
        or current.head != blocked_result.head
    ):
        return scope_before, frozenset(), frozenset()

    branch_ref = f"refs/heads/{before.branch}"
    result_branch_ref = blocked_result.refs.get(branch_ref)
    if (
        result_branch_ref is None
        or result_branch_ref[0] != blocked_result.head
        or current.refs.get(branch_ref) != result_branch_ref
    ):
        return scope_before, frozenset(), frozenset()

    worktree_key = str(_canonical_path(worktree))
    result_registration = blocked_result.worktrees.get(worktree_key)
    if (
        result_registration is None
        or current.worktrees.get(worktree_key) != result_registration
    ):
        return scope_before, frozenset(), frozenset()

    return (
        GitSnapshot(
            head=current.head,
            branch=scope_before.branch,
            index=scope_before.index,
            dirty=scope_before.dirty,
            refs=scope_before.refs,
            worktrees=scope_before.worktrees,
            config=scope_before.config,
            operations=scope_before.operations,
        ),
        frozenset((branch_ref,)),
        frozenset((worktree_key,)),
    )


def validate_persisted_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    allow_planner_worktree_correction: bool = False,
    manager_cleanup_worktrees: frozenset[str] = frozenset(),
) -> GitSnapshot | None:
    """Validate persisted Agent output before repository-aware Git work."""

    project = connection.execute(
        "SELECT phase, base_branch, branch, worktree, agent_role, agent_snapshot, "
        "agent_result_snapshot, agent_manager_snapshot, plan_path FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_snapshot"]:
        return None
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"저장된 Agent Git snapshot이 유효하지 않습니다: {error}"
        ) from error
    if not before.shared_metadata_complete:
        raise AgentScopeError("저장된 Agent Git metadata 기준 snapshot이 없습니다.")

    saved_worktree = Path(str(project["worktree"] or ""))
    config_context = saved_worktree if saved_worktree.is_dir() else repo_path
    current_config = _config_entries(config_context)
    changed_keys = sorted(
        key
        for key in set(before.config) | set(current_config)
        if before.config.get(key) != current_config.get(key)
    )
    if changed_keys:
        displayed = ", ".join(changed_keys[:8])
        remainder = len(changed_keys) - 8
        if remainder > 0:
            displayed += f", 외 {remainder}개"
        raise AgentScopeError(f"저장된 Agent Git config 변경 ({displayed})")
    role_name = str(project["agent_role"] or "")
    if role_name not in {"planner", "worker", "reviewer"}:
        raise AgentScopeError(
            f"저장된 Agent 역할이 유효하지 않습니다: {role_name or '없음'}"
        )
    selected_plan_path = str(project["plan_path"] or "")
    if role_name == "worker" and not selected_plan_path:
        raise AgentScopeError("저장된 Worker ExecPlan 경로가 없습니다.")
    if not saved_worktree.is_dir():
        recover_persisted_agent_worktree(
            connection, repo_path, state_dir, repo, project
        )

    current = capture_git_snapshot(saved_worktree)
    if allow_planner_worktree_correction:
        # The existing invalid commit/file diff is handled by the Planner
        # correction path below. Accept that already-diagnosed HEAD, index and
        # worktree state here while still rejecting branch switches and every
        # shared Git metadata mutation before Manager Git commands can run.
        planner_result: GitSnapshot | None = None
        correction_worktrees = dict(before.worktrees)
        saved_worktree_key = str(_canonical_path(saved_worktree))
        before_registration = before.worktrees.get(saved_worktree_key)
        current_registration = current.worktrees.get(saved_worktree_key)
        if (
            before_registration is not None
            and current_registration is not None
            and tuple(
                field
                for field in before_registration
                if not field.startswith("HEAD=")
            )
            == tuple(
                field
                for field in current_registration
                if not field.startswith("HEAD=")
            )
        ):
            correction_worktrees[saved_worktree_key] = current_registration
        allowed_ref_changes: frozenset[str] = frozenset()
        allowed_worktree_changes: frozenset[str] = frozenset()
        if project["agent_result_snapshot"]:
            try:
                planner_result = GitSnapshot.deserialize(
                    str(project["agent_result_snapshot"])
                )
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                raise AgentScopeError(
                    f"저장된 Planner 결과 snapshot이 유효하지 않습니다: {error}"
                ) from error
            if planner_result.shared_metadata_complete:
                base = str(project["base_branch"])
                allowed_ref_changes = _metadata_changes_after_block(
                    before.refs,
                    planner_result.refs,
                    current.refs,
                    frozenset(
                        (
                            f"refs/heads/{base}",
                            f"refs/remotes/origin/{base}",
                        )
                    ),
                )
                allowed_worktree_changes = _metadata_changes_after_block(
                    before.worktrees,
                    planner_result.worktrees,
                    current.worktrees,
                    frozenset((str(_canonical_path(repo_path)),)),
                )
        if (
            planner_result is not None
            and planner_result.worktrees.get(saved_worktree_key) != current_registration
        ):
            correction_worktrees[saved_worktree_key] = before_registration
        correction_result = planner_result or current
        correction_before = GitSnapshot(
            head=correction_result.head,
            branch=before.branch,
            index=correction_result.index,
            dirty=correction_result.dirty,
            refs=before.refs,
            worktrees=correction_worktrees,
            config=before.config,
            operations=before.operations,
        )
        validate_agent_scope(
            "planner",
            saved_worktree,
            correction_before,
            current,
            allowed_ref_changes=allowed_ref_changes,
            allowed_worktree_changes=allowed_worktree_changes,
        )
        return current

    if project["agent_manager_snapshot"]:
        try:
            checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
        if project["phase"] != "BLOCKED":
            validate_agent_scope(
                role_name,
                saved_worktree,
                before,
                checkpoint.snapshot,
                selected_plan_path=(
                    selected_plan_path if role_name == "worker" else None
                ),
            )
        checkpoint_scope_before = checkpoint.snapshot
        if project["phase"] == "BLOCKED" and selected_plan_path:
            checkpoint_scope_before = _blocked_agent_local_baseline(
                checkpoint.snapshot,
                checkpoint.snapshot,
                current,
                saved_worktree,
                allowed_dirty_paths=frozenset((selected_plan_path,)),
            )
        manager_refs = frozenset(
            refname
            for refname in set(checkpoint.snapshot.refs) | set(current.refs)
            if checkpoint.snapshot.refs.get(refname) != current.refs.get(refname)
            and (
                refname.startswith("refs/remotes/origin/")
                or refname.startswith("refs/tags/")
                or (
                    project["phase"] == "BLOCKED"
                    and refname == f"refs/heads/{project['base_branch']}"
                )
            )
        )
        allowed_cleanup = checkpoint.cleanup_worktrees | manager_cleanup_worktrees
        if project["phase"] == "BLOCKED":
            allowed_cleanup |= frozenset((str(_canonical_path(repo_path)),))
        try:
            validate_agent_scope(
                "reviewer",
                saved_worktree,
                checkpoint_scope_before,
                current,
                allowed_ref_changes=manager_refs,
                allowed_worktree_changes=allowed_cleanup,
            )
        except AgentScopeError as error:
            raise AgentScopeError(
                f"중단된 Manager Git 작업 후 Agent scope 검증 실패: {error}"
            ) from error
        return current

    scope_before = before
    allowed_ref_changes: frozenset[str] = frozenset()
    allowed_worktree_changes: frozenset[str] = frozenset()
    if project["phase"] == "BLOCKED" and project["agent_result_snapshot"]:
        try:
            blocked_result = GitSnapshot.deserialize(
                str(project["agent_result_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Agent 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error
        if blocked_result.shared_metadata_complete:
            scope_before = _blocked_agent_local_baseline(
                before,
                blocked_result,
                current,
                saved_worktree,
                allowed_dirty_paths=(
                    frozenset((selected_plan_path,))
                    if selected_plan_path
                    else frozenset()
                ),
            )
            (
                scope_before,
                commit_ref_changes,
                commit_worktree_changes,
            ) = _diagnosed_blocked_commit_allowances(
                before,
                blocked_result,
                current,
                saved_worktree,
                scope_before,
            )
            base = str(project["base_branch"])
            manager_ref_candidates = frozenset(
                {f"refs/heads/{base}"}
                | {
                    refname
                    for refname in set(before.refs) | set(current.refs)
                    if refname.startswith("refs/remotes/origin/")
                    or refname.startswith("refs/tags/")
                }
            )
            allowed_ref_changes = _metadata_changes_after_block(
                before.refs,
                blocked_result.refs,
                current.refs,
                manager_ref_candidates,
            )
            allowed_worktree_changes = _metadata_changes_after_block(
                before.worktrees,
                blocked_result.worktrees,
                current.worktrees,
                frozenset((str(_canonical_path(repo_path)),)),
            )
            allowed_ref_changes |= commit_ref_changes
            allowed_worktree_changes |= commit_worktree_changes

    validate_agent_scope(
        role_name,
        saved_worktree,
        scope_before,
        current,
        selected_plan_path=(
            selected_plan_path if role_name == "worker" else None
        ),
        allowed_ref_changes=allowed_ref_changes,
        allowed_worktree_changes=allowed_worktree_changes,
    )

    return current


def refresh_persisted_agent_scope_after_manager_git(
    connection: sqlite3.Connection,
    repo: str,
    validated: GitSnapshot,
    manager_worktree_changes: frozenset[str],
) -> GitSnapshot:
    """Merge trusted Manager refs into an interrupted Agent's start snapshot."""

    project = connection.execute(
        "SELECT worktree, agent_snapshot FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_snapshot"]:
        raise AgentScopeError("갱신할 저장된 Agent Git snapshot이 없습니다.")
    stored = GitSnapshot.deserialize(str(project["agent_snapshot"]))
    worktree = Path(str(project["worktree"] or ""))
    if not worktree.is_dir():
        raise AgentScopeError(
            f"저장된 Agent worktree를 확인할 수 없습니다: {worktree}"
        )

    current = capture_git_snapshot(worktree)
    manager_refs = frozenset(
        refname
        for refname in set(validated.refs) | set(current.refs)
        if validated.refs.get(refname) != current.refs.get(refname)
        and (
            refname.startswith("refs/remotes/origin/")
            or refname.startswith("refs/tags/")
        )
    )
    try:
        validate_agent_scope(
            "reviewer",
            worktree,
            validated,
            current,
            allowed_ref_changes=manager_refs,
            allowed_worktree_changes=manager_worktree_changes,
        )
    except AgentScopeError as error:
        raise AgentScopeError(
            f"Manager Git 작업 후 Agent scope 갱신 실패: {error}"
        ) from error
    refreshed_worktrees = dict(stored.worktrees)
    for path in manager_worktree_changes:
        registration = current.worktrees.get(path)
        if registration is None:
            refreshed_worktrees.pop(path, None)
        else:
            refreshed_worktrees[path] = registration
    refreshed = GitSnapshot(
        head=stored.head,
        branch=stored.branch,
        index=stored.index,
        dirty=stored.dirty,
        refs=current.refs,
        worktrees=refreshed_worktrees,
        config=stored.config,
        operations=stored.operations,
    )
    update_project(
        connection,
        repo,
        agent_snapshot=refreshed.serialize(),
        agent_manager_snapshot=None,
    )
    return refreshed


def validate_blocked_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    worktree: Path,
    selected_plan_path: str,
) -> None:
    """Revalidate preserved Agent output before a BLOCKED plan can resume."""

    project = connection.execute(
        "SELECT agent_role, agent_snapshot, agent_result_snapshot, agent_manager_snapshot "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        raise AgentScopeError("BLOCKED Agent 범위를 확인할 프로젝트가 없습니다.")
    serialized = project["agent_snapshot"]
    if not serialized:
        return
    role_name = str(project["agent_role"] or "")
    if role_name not in {"planner", "worker", "reviewer"}:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent 역할이 유효하지 않습니다: {role_name or '없음'}"
        )
    try:
        before = GitSnapshot.deserialize(str(serialized))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent Git snapshot이 유효하지 않습니다: {error}"
        ) from error
    current = capture_git_snapshot(worktree)
    scope_before = before
    allowed_ref_changes: frozenset[str] = frozenset()
    allowed_worktree_changes: frozenset[str] = frozenset()
    if project["agent_manager_snapshot"]:
        try:
            manager_checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
        allowed_worktree_changes |= manager_checkpoint.cleanup_worktrees
    result_serialized = project["agent_result_snapshot"]
    if result_serialized:
        try:
            blocked_result = GitSnapshot.deserialize(str(result_serialized))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Agent 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error

        if (
            before.shared_metadata_complete
            and blocked_result.shared_metadata_complete
            and current.shared_metadata_complete
        ):
            scope_before = _blocked_agent_local_baseline(
                before,
                blocked_result,
                current,
                worktree,
                allowed_dirty_paths=frozenset((selected_plan_path,)),
            )
            (
                scope_before,
                commit_ref_changes,
                commit_worktree_changes,
            ) = _diagnosed_blocked_commit_allowances(
                before,
                blocked_result,
                current,
                worktree,
                scope_before,
            )
            manager_ref_candidates = frozenset(
                {f"refs/heads/{base}"}
                | {
                    refname
                    for refname in set(before.refs) | set(current.refs)
                    if refname.startswith("refs/remotes/origin/")
                    or refname.startswith("refs/tags/")
                }
            )
            allowed_ref_changes = _metadata_changes_after_block(
                before.refs,
                blocked_result.refs,
                current.refs,
                manager_ref_candidates,
            )
            primary_worktree = str(_canonical_path(repo_path))
            allowed_worktree_changes |= _metadata_changes_after_block(
                before.worktrees,
                blocked_result.worktrees,
                current.worktrees,
                frozenset((primary_worktree,)),
            )
            allowed_ref_changes |= commit_ref_changes
            allowed_worktree_changes |= commit_worktree_changes

    validate_agent_scope(
        role_name,
        worktree,
        scope_before,
        current,
        selected_plan_path=(
            selected_plan_path if role_name == "worker" else None
        ),
        allowed_ref_changes=allowed_ref_changes,
        allowed_worktree_changes=allowed_worktree_changes,
    )


def _reconcile_blocked_reviewer_plan(
    relative_path: str,
    before_text: str,
    result_text: str,
    current_text: str,
) -> str:
    """Remove Reviewer edits while retaining separable user edits."""

    if before_text == result_text:
        return current_text

    before_lines = before_text.splitlines(keepends=True)
    result_lines = result_text.splitlines(keepends=True)
    current_lines = current_text.splitlines(keepends=True)
    user_equal_blocks = [
        (result_start, result_end, current_start, current_end)
        for tag, result_start, result_end, current_start, current_end in (
            difflib.SequenceMatcher(
                None,
                result_lines,
                current_lines,
                autojunk=False,
            ).get_opcodes()
        )
        if tag == "equal"
    ]
    reverse_edits: list[tuple[int, int, list[str]]] = []
    agent_edits = difflib.SequenceMatcher(
        None,
        result_lines,
        before_lines,
        autojunk=False,
    ).get_opcodes()
    for tag, result_start, result_end, before_start, before_end in agent_edits:
        if tag == "equal":
            continue
        if result_start == result_end:
            positions = {
                current_start + result_start - equal_start
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start <= equal_end
            }
            if len(positions) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start = current_end = positions.pop()
        else:
            ranges = {
                (
                    current_start + result_start - equal_start,
                    current_start + result_end - equal_start,
                )
                for equal_start, equal_end, current_start, _ in user_equal_blocks
                if equal_start <= result_start and result_end <= equal_end
            }
            if len(ranges) != 1:
                raise AgentScopeError(
                    "BLOCKED ExecPlan의 Reviewer 변경과 사용자 변경을 "
                    f"안전하게 분리할 수 없습니다: {relative_path}"
                )
            current_start, current_end = ranges.pop()
        reverse_edits.append(
            (current_start, current_end, before_lines[before_start:before_end])
        )

    for current_start, current_end, replacement in reversed(reverse_edits):
        current_lines[current_start:current_end] = replacement
    return "".join(current_lines)


def _deserialize_blocked_agent_recovery(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent 복구 상태가 유효하지 않습니다: {error}"
        ) from error
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != 1
        or parsed.get("state") not in {"prepared", "stashed", "isolated"}
        or not isinstance(parsed.get("source_snapshot"), str)
        or not isinstance(parsed.get("original_head"), str)
        or not isinstance(parsed.get("violation_head"), str)
        or not isinstance(parsed.get("branch"), str)
        or not isinstance(parsed.get("stash_marker"), str)
        or not isinstance(parsed.get("violation_ref"), str)
        or (
            parsed.get("plan_temp_path") is not None
            and not isinstance(parsed.get("plan_temp_path"), str)
        )
        or (
            parsed.get("plan_mode") is not None
            and not isinstance(parsed.get("plan_mode"), int)
        )
        or (
            parsed.get("baseline_snapshot") is not None
            and not isinstance(parsed.get("baseline_snapshot"), str)
        )
        or (
            parsed.get("plan_path") is not None
            and not isinstance(parsed.get("plan_path"), str)
        )
        or (
            parsed.get("plan_contents") is not None
            and not isinstance(parsed.get("plan_contents"), str)
        )
    ):
        raise AgentScopeError("저장된 BLOCKED Agent 복구 상태 형식을 지원하지 않습니다.")
    return parsed


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
) -> None:
    for relative_path, states in sorted(entries.items()):
        tags = {state.split(" ", 1)[0] for state in states}
        if any(tag.lower() == "s" for tag in tags):
            git(worktree, "update-index", "--skip-worktree", "--", relative_path)
        if any(tag.islower() for tag in tags):
            git(worktree, "update-index", "--assume-unchanged", "--", relative_path)


def _restore_agent_staged_entries(
    worktree: Path,
    before: GitSnapshot,
) -> None:
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
            git(worktree, "update-index", "--force-remove", "--", relative_path)
        else:
            mode, object_id = stage_zero
            git(
                worktree,
                "update-index",
                "--add",
                "--cacheinfo",
                mode,
                object_id,
                relative_path,
            )
    _restore_agent_index_flags(worktree, before.index)


def _restore_agent_baseline(
    worktree: Path,
    before: GitSnapshot,
    serialized: str | None,
) -> None:
    if not before.dirty:
        return
    if serialized is None:
        raise AgentScopeError(
            "dirty Agent 시작 상태를 복원할 보존 tree가 없습니다."
        )
    baseline = _deserialize_agent_baseline(serialized)
    ref_target = git(
        worktree,
        "rev-parse",
        "--verify",
        baseline["ref"],
        check=False,
    )
    if ref_target.returncode or ref_target.stdout.strip() != baseline["ref_target"]:
        raise AgentScopeError("Agent 시작 상태 보존 ref가 변경되거나 사라졌습니다.")
    worktree_tree = git(
        worktree,
        "rev-parse",
        baseline["ref_target"] + "^{tree}",
    ).stdout.strip()
    index_tree = git(
        worktree,
        "rev-parse",
        baseline["ref_target"] + "^1^{tree}",
    ).stdout.strip()
    if (
        worktree_tree != baseline["worktree_tree"]
        or index_tree != baseline["index_tree"]
    ):
        raise AgentScopeError("Agent 시작 상태 보존 tree가 저장된 값과 다릅니다.")

    git(worktree, "read-tree", "--reset", "-u", worktree_tree)
    git(worktree, "read-tree", index_tree)
    _restore_agent_index_flags(worktree, before.index)
    restored = capture_git_snapshot(worktree)
    if not _same_local_snapshot(before, restored):
        raise AgentScopeError("Agent 시작 시점의 기존 변경을 정확히 복원하지 못했습니다.")


def _worktree_relative_target(worktree: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AgentScopeError(f"복구 경로가 worktree 밖을 가리킵니다: {relative_path}")
    target = worktree / candidate
    try:
        target.parent.resolve().relative_to(worktree.resolve())
    except ValueError as error:
        raise AgentScopeError(
            f"복구 경로의 상위 디렉터리가 worktree 밖을 가리킵니다: {relative_path}"
        ) from error
    return target


def _remove_interrupted_recovery_temp(
    worktree: Path,
    relative_path: str | None,
) -> None:
    if relative_path is None:
        return
    target = _worktree_relative_target(worktree, relative_path)
    if target.is_dir() and not target.is_symlink():
        raise AgentScopeError(f"복구 임시 경로가 디렉터리입니다: {relative_path}")
    if target.exists() or target.is_symlink():
        target.unlink()


def _atomic_restore_file(
    worktree: Path,
    relative_path: str,
    temp_relative_path: str,
    contents: str,
    mode: int,
) -> None:
    target = _worktree_relative_target(worktree, relative_path)
    temporary = _worktree_relative_target(worktree, temp_relative_path)
    _remove_interrupted_recovery_temp(worktree, temp_relative_path)
    try:
        with temporary.open("xb") as handle:
            handle.write(contents.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 원자적 복원 실패: {error}"
        ) from error


def _serialize_blocked_agent_recovery(recovery: Mapping[str, object]) -> str:
    return json.dumps(
        recovery,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _blocked_recovery_stash_oid(worktree: Path, marker: str) -> str | None:
    listed = git(
        worktree,
        "stash",
        "list",
        "--format=%H%x00%gs",
        check=False,
    )
    if listed.returncode:
        raise AgentScopeError(
            f"BLOCKED Agent stash 확인 실패:\n{listed.stdout[-8000:]}"
        )
    for line in listed.stdout.splitlines():
        oid, separator, subject = line.partition("\0")
        if separator and subject.endswith(marker):
            return oid
    return None


def _restore_blocked_agent_recovery_plan(
    worktree: Path,
    plan_path: str | None,
    plan_contents: str | None,
    plan_temp_path: str | None,
    plan_mode: int | None,
) -> None:
    if plan_path is None:
        return
    target = _worktree_relative_target(worktree, plan_path)
    try:
        if plan_contents is None:
            if target.exists() or target.is_symlink():
                target.unlink()
        else:
            if plan_temp_path is None or plan_mode is None:
                raise AgentScopeError(
                    "BLOCKED ExecPlan 원자적 복원 정보가 없습니다."
                )
            _atomic_restore_file(
                worktree,
                plan_path,
                plan_temp_path,
                plan_contents,
                plan_mode,
            )
    except OSError as error:
        raise AgentScopeError(
            f"BLOCKED ExecPlan 사용자 변경 복원 실패: {error}"
        ) from error


def _recovered_agent_local_state_matches(
    worktree: Path,
    before: GitSnapshot,
    current: GitSnapshot,
    plan_path: str | None,
    plan_contents: str | None,
    plan_mode: int | None,
) -> bool:
    if (
        current.head != before.head
        or current.branch != before.branch
        or current.index != before.index
        or current.operations != before.operations
    ):
        return False
    current_dirty = dict(current.dirty)
    before_dirty = dict(before.dirty)
    if plan_path is None:
        return current_dirty == before_dirty
    current_dirty.pop(plan_path, None)
    before_dirty.pop(plan_path, None)
    if current_dirty != before_dirty:
        return False
    target = _worktree_relative_target(worktree, plan_path)
    if plan_contents is None:
        return not target.exists() and not target.is_symlink()
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(target_stat.st_mode)
        and stat.S_IMODE(target_stat.st_mode) == plan_mode
        and target.read_text(encoding="utf-8") == plan_contents
    )


def _mark_blocked_agent_isolated(
    connection: sqlite3.Connection,
    repo: str,
    recovery: dict[str, object],
    before: GitSnapshot,
) -> None:
    recovery["state"] = "isolated"
    recovery["baseline_snapshot"] = before.serialize()
    update_project(
        connection,
        repo,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_recovery=_serialize_blocked_agent_recovery(recovery),
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def _complete_blocked_agent_recovery(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
) -> None:
    project = connection.execute(
        "SELECT agent_snapshot, agent_baseline, agent_recovery "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return
    if not project["agent_snapshot"]:
        raise AgentScopeError("BLOCKED Agent 복구에 시작 snapshot이 없습니다.")

    recovery = _deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        source = GitSnapshot.deserialize(str(recovery["source_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 snapshot이 유효하지 않습니다: {error}"
        ) from error

    current_config = _config_entries(worktree)
    if current_config != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    marker = str(recovery["stash_marker"])
    stash_oid = _blocked_recovery_stash_oid(worktree, marker)
    if recovery["state"] == "prepared":
        if stash_oid is None:
            current = capture_git_snapshot(worktree)
            if current.serialize() != source.serialize():
                raise AgentScopeError(
                    "BLOCKED Agent 복구 준비 후 worktree 상태가 추가로 변경됐습니다."
                )
            stashed = git(
                worktree,
                "stash",
                "push",
                "--all",
                "-m",
                marker,
                check=False,
            )
            if stashed.returncode:
                raise AgentScopeError(
                    f"BLOCKED Agent 변경 보존 실패:\n{stashed.stdout[-8000:]}"
                )
            stash_oid = _blocked_recovery_stash_oid(worktree, marker)
        recovery["state"] = "stashed"
        recovery["stash_oid"] = stash_oid
        update_project(
            connection,
            repo,
            agent_recovery=_serialize_blocked_agent_recovery(recovery),
        )

    branch = str(recovery["branch"])
    original_head = str(recovery["original_head"])
    violation_head = str(recovery["violation_head"])
    plan_path_value = recovery.get("plan_path")
    plan_path = str(plan_path_value) if plan_path_value is not None else None
    plan_contents_value = recovery.get("plan_contents")
    plan_contents = (
        str(plan_contents_value) if plan_contents_value is not None else None
    )
    plan_temp_value = recovery.get("plan_temp_path")
    plan_temp_path = (
        str(plan_temp_value) if plan_temp_value is not None else None
    )
    plan_mode_value = recovery.get("plan_mode")
    plan_mode = int(plan_mode_value) if plan_mode_value is not None else None
    _remove_interrupted_recovery_temp(worktree, plan_temp_path)
    current = capture_git_snapshot(worktree)
    if current.branch != branch:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree branch가 저장된 branch와 다릅니다."
        )
    if current.operations:
        raise AgentScopeError(
            "BLOCKED Agent 복구 worktree에 Git operation이 남아 있습니다."
        )
    if before.operations:
        raise AgentScopeError(
            "Agent 실행 전 Git operation이 있는 worktree는 자동 복구할 수 없습니다."
        )

    if _recovered_agent_local_state_matches(
        worktree,
        before,
        current,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        _mark_blocked_agent_isolated(connection, repo, recovery, before)
        return

    if current.head == violation_head and violation_head != original_head:
        if current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent stash 후 worktree에 새 변경이 생겼습니다."
            )
        violation_ref = str(recovery["violation_ref"])
        existing_ref = git(
            worktree,
            "rev-parse",
            "--verify",
            violation_ref,
            check=False,
        )
        if existing_ref.returncode == 0:
            if existing_ref.stdout.strip() != violation_head:
                raise AgentScopeError(
                    f"BLOCKED Agent 진단 ref 충돌: {violation_ref}"
                )
        else:
            git(worktree, "update-ref", violation_ref, violation_head)
        git(worktree, "reset", "--hard", original_head)
        current = capture_git_snapshot(worktree)
    elif current.head != original_head:
        raise AgentScopeError(
            "BLOCKED Agent 복구 HEAD가 저장된 Agent 결과 또는 시작 commit과 다릅니다."
        )

    if not _same_local_snapshot(before, current):
        if current.dirty:
            raise AgentScopeError(
                "BLOCKED Agent 복구 중 예상하지 못한 새 worktree 변경이 생겼습니다."
            )
        _restore_agent_baseline(
            worktree,
            before,
            (
                str(project["agent_baseline"])
                if project["agent_baseline"] is not None
                else None
            ),
        )

    _restore_blocked_agent_recovery_plan(
        worktree,
        plan_path,
        plan_contents,
        plan_temp_path,
        plan_mode,
    )

    restored = capture_git_snapshot(worktree)
    if not _recovered_agent_local_state_matches(
        worktree,
        before,
        restored,
        plan_path,
        plan_contents,
        plan_mode,
    ):
        raise AgentScopeError("BLOCKED Agent 복구 후 저장된 변경 상태와 일치하지 않습니다.")
    _mark_blocked_agent_isolated(connection, repo, recovery, before)


def preserve_blocked_agent_changes(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan_id: str,
    *,
    preserved_input_path: str | None = None,
) -> GitSnapshot | None:
    """Persist and isolate a diagnosed scope violation crash-safely."""

    project = connection.execute(
        "SELECT agent_role, agent_snapshot, agent_result_snapshot, "
        "agent_recovery, agent_input_plan, agent_result_plan "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        raise AgentScopeError("보존할 BLOCKED Agent 프로젝트가 없습니다.")
    if project["agent_recovery"]:
        recovery = _deserialize_blocked_agent_recovery(
            str(project["agent_recovery"])
        )
        if recovery["state"] == "isolated":
            baseline_snapshot = recovery.get("baseline_snapshot")
            if not isinstance(baseline_snapshot, str):
                raise AgentScopeError("격리된 Agent 시작 snapshot이 없습니다.")
            try:
                return GitSnapshot.deserialize(baseline_snapshot)
            except (KeyError, TypeError, ValueError, RuntimeError) as error:
                raise AgentScopeError(
                    f"격리된 Agent 시작 snapshot이 유효하지 않습니다: {error}"
                ) from error
        if not project["agent_snapshot"]:
            raise AgentScopeError("복구 중인 BLOCKED Agent 시작 snapshot이 없습니다.")
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        _complete_blocked_agent_recovery(connection, repo, worktree)
        return before
    if not project["agent_snapshot"] or not project["agent_result_snapshot"]:
        return None
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"보존할 BLOCKED Agent snapshot이 유효하지 않습니다: {error}"
        ) from error
    if before.operations:
        raise AgentScopeError(
            "Agent 실행 전 Git operation이 있는 worktree는 자동 격리할 수 없습니다."
        )
    current = capture_git_snapshot(worktree)
    if current.branch != before.branch or current.head not in {
        before.head,
        result.head,
    }:
        raise AgentScopeError(
            "BLOCKED 이후 HEAD 또는 branch가 추가로 변경되어 자동 보존할 수 없습니다."
        )
    allowed_dirty_paths = (
        frozenset((preserved_input_path,))
        if preserved_input_path is not None
        else frozenset()
    )
    _blocked_agent_local_baseline(
        before,
        result,
        current,
        worktree,
        allowed_dirty_paths=allowed_dirty_paths,
    )
    preserved_paths = frozenset(
        path
        for path in allowed_dirty_paths
        if current.dirty.get(path)
        not in (before.dirty.get(path), result.dirty.get(path))
    )
    plan_path: str | None = None
    plan_contents: str | None = None
    plan_mode: int | None = None
    if preserved_paths:
        if len(preserved_paths) != 1:
            raise AgentScopeError(
                "사용자가 수정한 BLOCKED 입력 경로를 하나로 결정할 수 없습니다."
            )
        plan_path = next(iter(preserved_paths))
        plan_file = worktree / plan_path
        if plan_file.is_symlink() or (plan_file.exists() and not plan_file.is_file()):
            raise AgentScopeError(
                f"사용자가 수정한 BLOCKED ExecPlan 형식이 안전하지 않습니다: {plan_path}"
            )
        plan_contents = (
            plan_file.read_text(encoding="utf-8")
            if plan_file.is_file()
            else None
        )
        plan_mode = (
            stat.S_IMODE(plan_file.lstat().st_mode)
            if plan_file.is_file()
            else None
        )
        if project["agent_role"] == "reviewer":
            before_text = project["agent_input_plan"]
            result_text = project["agent_result_plan"]
            agent_changed_path = (
                before.dirty.get(plan_path) != result.dirty.get(plan_path)
            )
            if before_text is None or result_text is None:
                if agent_changed_path:
                    raise AgentScopeError(
                        "BLOCKED ExecPlan의 Reviewer 변경을 분리할 "
                        f"본문 기준이 없습니다: {plan_path}"
                    )
            elif str(before_text) != str(result_text):
                if plan_contents is None:
                    raise AgentScopeError(
                        f"사용자가 수정한 BLOCKED ExecPlan을 읽을 수 없습니다: {plan_path}"
                    )
                plan_contents = _reconcile_blocked_reviewer_plan(
                    plan_path,
                    str(before_text),
                    str(result_text),
                    plan_contents,
                )

    source_serialized = current.serialize()
    recovery_id = hashlib.sha256(
        json.dumps(
            {
                "repo": repo,
                "plan_id": plan_id,
                "source": source_serialized,
                "plan_path": plan_path,
                "plan_contents": plan_contents,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    plan_temp_path: str | None = None
    if plan_path is not None and plan_contents is not None:
        plan_relative = Path(plan_path)
        plan_temp_path = str(
            plan_relative.parent
            / f".{plan_relative.name}.loop-manager-recovery-{recovery_id}.tmp"
        )
    recovery: dict[str, object] = {
        "version": 1,
        "state": "prepared",
        "source_snapshot": source_serialized,
        "original_head": before.head,
        "violation_head": result.head,
        "branch": str(before.branch or ""),
        "stash_marker": f"loop-manager scope violation {plan_id} {recovery_id}",
        "violation_ref": (
            f"refs/loop-manager/violations/{project_id}/{plan_id}/{recovery_id}"
        ),
        "plan_path": plan_path,
        "plan_contents": plan_contents,
        "plan_temp_path": plan_temp_path,
        "plan_mode": plan_mode,
        "stash_oid": None,
    }
    update_project(
        connection,
        repo,
        agent_recovery=_serialize_blocked_agent_recovery(recovery),
    )
    _complete_blocked_agent_recovery(connection, repo, worktree)
    return before


def resume_interrupted_blocked_agent_recovery(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    """Finish a persisted violation isolation before normal repository work."""

    project = connection.execute(
        "SELECT phase, base_branch, branch, worktree, agent_snapshot, "
        "agent_recovery FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or not project["agent_recovery"]:
        return False
    recovery = _deserialize_blocked_agent_recovery(
        str(project["agent_recovery"])
    )
    if project["phase"] != "BLOCKED":
        raise AgentScopeError(
            "BLOCKED Agent 복구 상태가 활성 BLOCKED 작업과 일치하지 않습니다."
        )
    serialized_before = project["agent_snapshot"]
    if recovery["state"] == "isolated":
        serialized_before = recovery.get("baseline_snapshot")
    if not isinstance(serialized_before, str):
        raise AgentScopeError("BLOCKED Agent 복구 시작 snapshot이 없습니다.")
    try:
        before = GitSnapshot.deserialize(serialized_before)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 시작 snapshot이 유효하지 않습니다: {error}"
        ) from error

    worktree = Path(str(project["worktree"] or ""))
    branch = str(project["branch"] or "")
    config_context = worktree if worktree.is_dir() else repo_path
    if _config_entries(config_context) != before.config:
        raise AgentScopeError("BLOCKED Agent 복구 전 Git config가 변경됐습니다.")

    plan = active_execplan(connection, repo)
    if (
        plan is None
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise AgentScopeError(
            "BLOCKED Agent 복구 workspace의 Loop Manager 소유권을 증명할 수 없습니다."
        )
    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            str(project["base_branch"]),
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
            allow_rebase=(
                recovery["state"] == "isolated"
                and _rebase_in_progress_for_branch(worktree, branch)
            ),
        )
    except WorkspaceOwnershipError as error:
        raise AgentScopeError(
            f"BLOCKED Agent 복구 workspace 검증 실패: {error}"
        ) from error

    if recovery["state"] != "isolated":
        _complete_blocked_agent_recovery(connection, repo, worktree)
    return True


def _requeue_blocked_execplan(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    values = _clear_active_values()
    assignments = ", ".join(f"{name} = ?" for name in values)
    with connection:
        connection.execute(
            """
            UPDATE exec_plans
            SET status = 'READY', blocked_fingerprint = NULL,
                blocked_worktree_fingerprint = NULL, retry_count = 0,
                last_error = NULL, worker_session_id = NULL,
                reviewer_session_id = NULL, worker_result = NULL,
                verify_result = NULL, review_result = NULL,
                verify_failure_history = '{}', review_failure_history = '{}'
            WHERE repo = ? AND plan_id = ?
            """,
            (repo, plan_id),
        )
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?",
            (*values.values(), repo),
        )


def resume_blocked_execplan_if_inputs_changed(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
) -> bool:
    plan = active_execplan(connection, repo)
    if plan is None or plan["status"] != "BLOCKED" or not plan["blocked_fingerprint"]:
        return False
    stored = str(plan["blocked_fingerprint"])
    worktree = Path(str(plan["worktree"]))
    branch = str(plan["branch"] or "")
    revision = f"refs/remotes/origin/{base}"
    remote_input_changed = (
        _revision_execplan_input_fingerprint(repo_path, revision, plan) != stored
    )
    if int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        if not remote_input_changed:
            return False
        try:
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                str(plan["plan_id"]),
                branch,
                worktree,
            )
        except WorkspaceOwnershipError as error:
            message = f"BLOCKED workspace 충돌 해제 검증 실패:\n{error}"
            _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        branch_state = git(
            repo_path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        conflicts = []
        if worktree.exists() or worktree.is_symlink():
            conflicts.append(f"path {worktree}")
        if branch_state.returncode == 0:
            conflicts.append(f"branch {branch}")
        elif branch_state.returncode != 1:
            conflicts.append(f"branch 상태 확인 실패 {branch}")
        if conflicts:
            message = (
                "BLOCKED workspace 충돌이 해소되지 않았습니다: "
                + ", ".join(conflicts)
            )
            _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        metadata_error = _blocked_execplan_revision_metadata_error(
            repo_path,
            revision,
            plan,
        )
        if metadata_error:
            _update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False
        _requeue_blocked_execplan(connection, repo, str(plan["plan_id"]))
        return True

    try:
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except (RuntimeError, WorkspaceOwnershipError) as error:
        message = f"BLOCKED worktree 복구 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    try:
        validate_blocked_agent_scope(
            connection,
            repo_path,
            repo,
            base,
            worktree,
            str(plan["path"]),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 범위 재검증 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    current_worktree_fingerprint = (
        _worktree_execplan_input_fingerprint(worktree, plan)
        if worktree.is_dir()
        else None
    )
    blocked_worktree_fingerprint = plan["blocked_worktree_fingerprint"]
    worktree_changed = bool(
        current_worktree_fingerprint != stored
        and current_worktree_fingerprint != blocked_worktree_fingerprint
    )
    if not worktree_changed and not remote_input_changed:
        return False
    try:
        preserved_before = preserve_blocked_agent_changes(
            connection,
            repo,
            worktree,
            str(plan["plan_id"]),
            preserved_input_path=(str(plan["path"]) if worktree_changed else None),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 변경 보존 실패:\n{error}"
        _update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    base_ancestry = git(
        worktree,
        "merge-base",
        "--is-ancestor",
        revision,
        "HEAD",
        check=False,
    )
    if base_ancestry.returncode not in (0, 1):
        error = (
            "BLOCKED 해제 전 원격 base ancestry 확인 실패:\n"
            f"{base_ancestry.stdout[-8000:]}"
        )
        _update_blocked_error(connection, repo, str(plan["plan_id"]), error)
        return False
    if base_ancestry.returncode == 1:
        rebased = git(
            worktree,
            "rebase",
            "--autostash",
            f"origin/{base}",
            check=False,
        )
        if rebased.returncode:
            error = f"BLOCKED 해제 전 rebase 실패:\n{rebased.stdout[-8000:]}"
            _update_blocked_error(connection, repo, str(plan["plan_id"]), error)
            return False
        if preserved_before is not None:
            try:
                _restore_agent_staged_entries(worktree, preserved_before)
            except (AgentScopeError, RuntimeError) as error:
                restore_error = f"BLOCKED 해제 후 index 복원 실패:\n{error}"
                _update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    restore_error,
                )
                return False
    if worktree.is_dir():
        metadata_error = _blocked_execplan_metadata_error(repo_path, worktree, plan)
        if metadata_error:
            _update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False

    clear_agent_scope(connection, repo)
    _requeue_blocked_execplan(connection, repo, str(plan["plan_id"]))
    return True


def complete_merged_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    project = connection.execute(
        "SELECT plan_path, branch FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        raise WorkspaceOwnershipError("Merge cleanup 프로젝트를 찾을 수 없습니다.")
    plan_id = _plan_id_from_path(str(project["plan_path"] or ""))
    if plan_id is None:
        raise WorkspaceOwnershipError("Merge cleanup ExecPlan ID를 확인할 수 없습니다.")
    branch = str(project["branch"] or "")
    _ensure_execplans_schema(connection)
    plan = connection.execute(
        "SELECT branch, worktree, workspace_owned FROM exec_plans "
        "WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is None:
        raise WorkspaceOwnershipError(
            f"Merge cleanup ExecPlan을 찾을 수 없습니다: {plan_id}"
        )
    workspace_owned = WORKSPACE_OWNED
    worktree_present = worktree.exists() or worktree.is_symlink()
    if not worktree_present:
        assessment = CleanupAssessment("missing")
    elif (
        int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        workspace_owned = 0
        assessment = CleanupAssessment(
            "unverifiable",
            "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}",
        )
    else:
        try:
            validate_execplan_workspace_identity(
                state_dir,
                repo,
                plan_id,
                branch,
                worktree,
            )
            validate_managed_worktree(repo_path, worktree, branch)
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_owned = 0
            assessment = CleanupAssessment(
                "unverifiable",
                f"Merge cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = assess_cleanup(worktree, pr_head_oid)
            assessment = remove_cleanup_worktree(repo_path, worktree, assessment)
    values = _clear_active_values()
    with connection:
        _ensure_execplans_schema(connection)
        _ensure_cleanup_pending_schema(connection)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            _store_cleanup_pending(
                connection,
                repo,
                worktree,
                pr_url,
                pr_head_oid,
                assessment.reason or assessment.status,
                workspace_owned=workspace_owned,
            )
        if plan_id:
            connection.execute(
                "UPDATE exec_plans SET status = 'MERGED', last_error = NULL WHERE repo = ? AND plan_id = ?",
                (repo, plan_id),
            )
        unfinished = connection.execute(
            "SELECT 1 FROM exec_plans "
            "WHERE repo = ? AND status != 'MERGED' LIMIT 1",
            (repo,),
        ).fetchone()
        if unfinished is None:
            values["processed_sha"] = None
            values["planning_fingerprint"] = None
        assignments = ", ".join(f"{name} = ?" for name in values)
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?",
            [*values.values(), repo],
        )
    return assessment


def planner_prompt(error: str | None, compact_catalog: str = "[]") -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    return f"""
당신은 Loop Manager의 Planner다.
AGENTS.md, 활성 PRODUCT와 Requirement, docs/ARCHITECTURE.md, docs/PLANS.md 및 현재 코드·테스트를 읽어라.
아래 compact catalog는 SQLite의 비종료 ExecPlan만 코드로 직렬화한 것이다. MERGED 계획은 제외됐다.
{compact_catalog}

현재 입력에서 식별 가능한 미충족 작업 전체를 독립적으로 검증·Merge 가능한 ExecPlan들로 나누어 한 번에 작성하라.
catalog가 이미 충족하는 작업이나 코드·테스트에 이미 구현된 작업은 중복 계획하지 마라.
catalog에서 현재 입력과 겹치는 비종료 계획만 필요할 때 전문을 읽고, 기존 docs/plans 전체를 기본 입력으로 읽지 마라.
새 ExecPlan마다 priority를 기록하고, 기존 파일을 수정하지 말며 docs/plans 바로 아래에 새 파일만 작성하라.
구현, 테스트, commit, push와 PR 생성은 하지 마라. 추가 계획이 필요 없으면 파일을 변경하지 마라.
{failure}
""".strip()


def worker_prompt(
    plan_path: str,
    error: str | None,
    previous_result: str | None = None,
    validation_feedback: str | None = None,
    *,
    session_recovery: bool = False,
) -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    saved_feedback = (
        "\nSQLite에 보존된 전체 verify 또는 Review 실패 피드백:\n"
        f"{validation_feedback[-6000:]}\n"
        if validation_feedback
        else ""
    )
    recovery = ""
    if session_recovery:
        recovery = (
            "\n저장된 Worker 세션을 재개할 수 없어 새 세션에서 이어간다. "
            "현재 worktree와 아래 이전 Worker 결과 및 실패 내용을 기준으로 계속하라:\n"
            f"{(previous_result or '이전 Worker 결과 없음')[-6000:]}\n"
        )
    return f"""
당신은 Loop Manager의 Worker다.
AGENTS.md와 {plan_path}를 읽고 해당 ExecPlan 하나만 구현하라.
관련 targeted test를 작성하되 commit, push, PR 생성과 scripts/verify 실행은 하지 마라.
docs/plans에 새 ExecPlan을 만들지 말고 불필요한 후속 기능도 구현하지 마라.
구현과 관련 unit test를 작성하거나 갱신한 뒤, 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 targeted unit test만 실행하라. 개별 test case, test class, test module 또는 이에 준하는 좁은 단위만 허용된다.
targeted unit test가 실패하면 같은 세션에서 원인을 수정하고 같은 targeted test를 다시 실행하라. 문서·설정 전용 변경이거나 실행 가능한 관련 unit test가 없으면 테스트 범위를 확대하지 말고, 실행하지 못한 구체적인 사유를 최종 결과에 남겨라.
실패 이력을 보고할 때는 실패한 명령만 적은 `FAILED_COMMAND:` 줄과 수정 후 동일 명령만 적은 `RERUN:` 줄을 각각 두고, 재실행의 실패가 없는 최종 결과를 그 뒤에 명시하라. 두 명령은 공백을 정규화한 뒤 정확히 같아야 하며 하나의 실행에서 성공·실패가 섞인 요약은 통과로 보고하지 마라.
전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check 및 scripts/verify 실행은 절대 실행하지 마라. 전체 verify는 Worker 종료 뒤 Loop Manager가 실행하며, 실패한 경우에만 이 세션으로 진단을 전달한다.
작업이 완료되면 최종 응답 마지막에 아래 형식을 정확히 포함하라. 제목과 Summary는 최종 diff 전체를 설명해야 하며 ExecPlan 제목이나 일반적인 변경 문구를 쓰지 마라:
COMMIT_TITLE: 한 줄의 구체적인 변경 제목
PR_SUMMARY: 실제 변경을 간결히 설명하는 Summary
TARGETED_TESTS:
실행한 최소 범위 targeted unit test와 결과, 또는 실행하지 못한 구체적인 사유
{recovery}
{saved_feedback}
{failure}
""".strip()



_TARGETED_TEST_FAILURE = re.compile(
    r"(?i)(?<![\w])(?:FAIL(?:ED|URES?)?|ERRORS?|실패)(?![\w])"
)
_TARGETED_TEST_SUCCESS = re.compile(
    r"(?i)(?<![\w])(?:PASS(?:ED)?|SUCCESS|OK|성공|통과)(?![\w])"
)
_TARGETED_TEST_COMMAND = re.compile(
    r"(?mi)^[ \t]*(?:[-*][ \t]+)?"
    r"(?P<label>FAILED_COMMAND|RERUN):[ \t]*(?P<command>\S.*?)?[ \t]*$"
)


def _targeted_test_failure_matches(evidence: str) -> list[re.Match[str]]:
    failures = []
    command_spans = [
        (match.start(), match.end())
        for match in _TARGETED_TEST_COMMAND.finditer(evidence)
    ]
    for match in _TARGETED_TEST_FAILURE.finditer(evidence):
        if any(start <= match.start() < end for start, end in command_spans):
            continue
        prefix = evidence[max(0, match.start() - 32):match.start()]
        count = re.search(
            r"(?i)(?<![\w])(\d+)(?:\s+(?:tests?|cases?))?\s*$",
            prefix,
        )
        if count is not None and int(count.group(1)) == 0:
            continue
        if re.search(r"(?i)\b(?:no|without)\s+$", prefix):
            continue
        failures.append(match)
    return failures


def _has_unresolved_targeted_test_failure(evidence: str) -> bool:
    commands = list(_TARGETED_TEST_COMMAND.finditer(evidence))
    failures = _targeted_test_failure_matches(evidence)
    if not failures:
        return bool(commands)
    recovery_rerun = None

    for index, failure in enumerate(failures):
        prior_commands = [
            match
            for match in commands
            if match.end() <= failure.start()
        ]
        if not prior_commands:
            return True
        failed_run = prior_commands[-1]
        if (
            index == 0
            and failed_run.group("label").upper() != "FAILED_COMMAND"
        ):
            return True
        failed_command = " ".join(
            (failed_run.group("command") or "").split()
        )
        if not failed_command:
            return True

        subsequent_commands = [
            match
            for match in commands
            if match.start() >= failure.end()
        ]
        if not subsequent_commands:
            return True
        rerun = subsequent_commands[0]
        rerun_command = " ".join((rerun.group("command") or "").split())
        if (
            rerun.group("label").upper() != "RERUN"
            or rerun_command != failed_command
        ):
            return True
        recovery_rerun = rerun

    if recovery_rerun is None:
        return True
    if commands[-1].start() != recovery_rerun.start():
        return True
    return (
        _TARGETED_TEST_SUCCESS.search(evidence, recovery_rerun.end())
        is None
    )


def parse_worker_result(message: str, plan: dict[str, object]) -> WorkerPublication:
    """Parse and validate the Worker final-result contract before publishing."""
    match = re.search(
        r"(?ms)^COMMIT_TITLE:[ \t]*(?P<title>[^\r\n]*)\r?\nPR_SUMMARY:[ \t]*(?P<summary>.*?)\r?\nTARGETED_TESTS:\s*(?P<tests>.+)\Z",
        message.strip(),
    )
    if not match:
        title_line = re.search(r"(?m)^COMMIT_TITLE:.*$", message)
        if title_line and not re.match(r"\r?\nPR_SUMMARY:", message[title_line.end():]):
            raise WorkerResultError("Worker commit 제목은 한 줄이어야 합니다.")
        raise WorkerResultError(
            "Worker 결과에는 COMMIT_TITLE, PR_SUMMARY, TARGETED_TESTS 형식이 모두 필요합니다."
        )

    title = match.group("title").strip()
    summary = match.group("summary").strip()
    test_evidence = match.group("tests").strip()
    if not title:
        raise WorkerResultError("Worker commit 제목이 비어 있습니다.")
    if not summary:
        raise WorkerResultError("Worker PR Summary가 비어 있습니다.")
    if not test_evidence:
        raise WorkerResultError("Worker targeted unit test 증거가 비어 있습니다.")
    if _has_unresolved_targeted_test_failure(test_evidence):
        raise WorkerResultError(
            "Worker가 해결되지 않은 targeted unit test 실패를 보고했습니다."
        )

    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    normalized = " ".join(title.split()).casefold().rstrip(" .,:;!?…")
    generic_title_words = {
        "implement", "implementation", "implementations", "change", "changes", "code",
        "update", "updates", "fix", "fixes", "the", "work", "feature", "features",
        "변경", "사항", "구현",
    }
    prohibited = {
        " ".join(plan_title.split()).casefold().rstrip(" .,:;!?…"),
        f"implement {plan_id}".casefold(),
        f"implement {plan_id}: {plan_title}".casefold(),
        f"implement {plan_title}".casefold(),
    }
    if (
        (bool(normalized.split()) and set(normalized.split()) <= generic_title_words)
        or normalized in prohibited
        or re.match(rf"implement\s+(?:execplan\s+)?{re.escape(plan_id.casefold())}\b", normalized)
    ):
        raise WorkerResultError(
            "Worker commit 제목은 ExecPlan 제목이나 일반적인 변경 문구가 아니라 실제 변경을 설명해야 합니다."
        )
    return WorkerPublication(title, summary, test_evidence)

def immutable_github_blob_url(origin_url: str, commit_sha: str, relative_path: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
        raise RuntimeError("계획 commit SHA가 유효하지 않습니다.")
    repository = github_repository(origin_url)
    encoded_path = quote(relative_path, safe="/")
    return f"https://github.com/{repository}/blob/{commit_sha}/{encoded_path}"


def execplan_purpose(plan_text: str) -> str:
    body = re.sub(r"(?s)\A---.*?---\s*", "", plan_text, count=1)
    match = re.search(r"(?ms)^## Purpose\s*\n+(.*?)(?=^## |\Z)", body)
    if not match or not match.group(1).strip():
        raise RuntimeError("ExecPlan Purpose를 찾을 수 없습니다.")
    return " ".join(match.group(1).split())


def published_execplan(plan_path: str, plan_text: str) -> dict[str, object]:
    """Read metadata from the immutable ExecPlan commit without live source checks."""
    plan = parse_frontmatter(plan_text)
    required = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    if set(plan) not in {frozenset(required), frozenset(required | {"priority"})}:
        raise RuntimeError("게시된 ExecPlan frontmatter가 올바르지 않습니다.")
    if "priority" not in plan:
        plan["priority"] = "normal"
    if plan["priority"] not in PRIORITY_ORDER:
        raise RuntimeError("게시된 ExecPlan priority가 유효하지 않습니다.")
    plan_id = plan["id"]
    title = plan["title"]
    if not isinstance(plan_id, str) or not isinstance(title, str):
        raise RuntimeError("게시된 ExecPlan ID 또는 제목이 유효하지 않습니다.")
    if Path(plan_path).name != f"[{plan_id}] {title}.md":
        raise RuntimeError("게시된 ExecPlan 경로가 메타데이터와 일치하지 않습니다.")
    sources = plan["sources"]
    if not isinstance(sources, list) or not sources:
        raise RuntimeError("게시된 ExecPlan source가 하나 이상 필요합니다.")
    for source in sources:
        if not isinstance(source, str) or not (
            source == "docs/PRODUCT.md"
            or re.fullmatch(r"docs/requirements/[^/\\]+\.md", source)
        ):
            raise RuntimeError(f"게시된 ExecPlan source가 유효하지 않습니다: {source}")
    dependencies = plan["depends_on"]
    if not isinstance(dependencies, list):
        raise RuntimeError("게시된 ExecPlan depends_on이 유효하지 않습니다.")
    if any(
        not isinstance(dependency, str) or not re.fullmatch(r"G-\d{3,}", dependency)
        for dependency in dependencies
    ):
        raise RuntimeError("게시된 ExecPlan depends_on ID가 유효하지 않습니다.")
    if plan["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("게시된 ExecPlan complexity가 유효하지 않습니다.")
    if plan["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("게시된 ExecPlan risk가 유효하지 않습니다.")
    return plan


def build_pr_body(
    worker: WorkerPublication,
    plan: dict[str, object],
    plan_path: str,
    plan_text: str,
    origin_url: str,
    plan_sha: str,
) -> str:
    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    plan_link = immutable_github_blob_url(origin_url, plan_sha, plan_path)
    sources = plan["sources"]
    assert isinstance(sources, list)
    source_lines = [
        f"- [{source}]({immutable_github_blob_url(origin_url, plan_sha, str(source))})"
        for source in sources
    ]
    return "\n".join(
        [
            "## Summary",
            worker.summary,
            "",
            "## ExecPlan",
            f"- [{plan_id}: {plan_title}]({plan_link})",
            f"- Purpose: {execplan_purpose(plan_text)}",
            "",
            "## Sources",
            *source_lines,
        ]
    )

def reviewer_prompt(
    base: str,
    worker_result: str | None = None,
    verify_result: str | None = None,
    review_result: str | None = None,
    *,
    session_recovery: bool = False,
) -> str:
    worker_evidence = (worker_result or "Worker가 targeted unit test 결과 또는 미실행 사유를 보고하지 않았습니다.")[-6000:]
    verify_evidence = (verify_result or "Loop Manager 전체 verify가 성공했습니다(출력 없음).")[-6000:]
    recovery = ""
    if session_recovery:
        recovery = (
            "\n저장된 Reviewer 세션을 재개할 수 없어 새 세션에서 이어간다. "
            "아래 이전 Review 결과를 포함해 현재 diff를 다시 검토하라:\n"
            f"{(review_result or '이전 Review 결과 없음')[-6000:]}\n"
        )
    return f"""
origin/{base}와 현재 HEAD의 차이를 읽기 전용으로 검토하라.
AGENTS.md, 근거 PRODUCT/Requirement, ARCHITECTURE와 ExecPlan을 기준으로 correctness, 회귀, 테스트 누락, verify 약화를 확인하라.
Worker가 보고한 targeted unit test 결과 또는 미실행 사유:
{worker_evidence}

Loop Manager가 실행한 전체 verify 결과:
{verify_evidence}
{recovery}
테스트 command와 scripts/verify를 실행하지 말고, 코드·테스트·verify 스크립트의 diff 및 위 결과만으로 판단하라.
blocking issue마다 설명 직전에 `ISSUE_ID: relative/path::stable-symbol-or-test::category-slug`를 한 줄로 기록하라.
ISSUE_ID에는 줄 번호나 자연어 설명을 넣지 말고, 같은 issue가 재발하면 위치·표현이 바뀌어도 이전과 정확히 같은 ID를 사용하라.
`VERDICT: FAIL`은 하나 이상의 ISSUE_ID가 있어야 하며, 서로 다른 issue는 서로 다른 ID를 사용하라.
blocking issue가 있으면 간결히 설명하고 마지막 줄을 `VERDICT: FAIL`로 끝내라.
blocking issue가 없으면 마지막 줄을 `VERDICT: PASS`로 끝내라. 파일은 수정하지 마라.
""".strip()


def _run_project_step(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
) -> int:
    # Resolve the persisted project key without consulting repository config.
    # An interrupted Agent may have changed origin or hooksPath, so the saved
    # snapshot must be checked before normal repository validation reads it.
    repo_path = Path(repo_arg).expanduser().resolve()
    repo = str(repo_path)
    _ensure_execplans_schema(connection)
    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    _ensure_cleanup_pending_schema(connection)
    cleanup_candidates = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in connection.execute(
            "SELECT worktree FROM cleanup_pending "
            "WHERE repo = ? AND workspace_owned = ?",
            (repo, WORKSPACE_OWNED),
        ).fetchall()
    )
    checkpoint_cleanup_candidates = cleanup_candidates
    if row and row["agent_recovery"]:
        try:
            resume_interrupted_blocked_agent_recovery(
                connection,
                repo_path,
                state_dir,
                repo,
            )
        except (AgentScopeError, RuntimeError) as error:
            recovery_error = f"BLOCKED Agent 중단 복구 실패:\n{error}"
            plan = active_execplan(connection, repo)
            if plan is not None:
                _update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    recovery_error,
                )
            else:
                update_project(connection, repo, last_error=recovery_error)
            operation_print(recovery_error, file=sys.stderr)
            return 1
        row = connection.execute(
            "SELECT * FROM projects WHERE repo = ?",
            (repo,),
        ).fetchone()
    validated_agent_scope: GitSnapshot | None = None
    if row and row["agent_snapshot"]:
        planner_correction = bool(
            row["phase"] == "PLANNING" and row["planning_validation_error"]
        )
        try:
            validated_agent_scope = validate_persisted_agent_scope(
                connection,
                repo_path,
                state_dir,
                repo,
                allow_planner_worktree_correction=planner_correction,
                manager_cleanup_worktrees=cleanup_candidates,
            )
        except AgentScopeError as error:
            detail = str(error)
            scope_result_snapshot = error.result_snapshot
        except RuntimeError:
            detail = "저장된 Agent Git 상태를 안전하게 읽을 수 없습니다."
            scope_result_snapshot = None
        else:
            detail = None
            scope_result_snapshot = None
        if detail is not None:
            message = block_persisted_agent_scope_violation(
                connection,
                repo_path,
                repo,
                row,
                detail,
                result_snapshot=scope_result_snapshot,
            )
            operation_print(message, file=sys.stderr)
            return 1
        if validated_agent_scope is not None:
            if row["agent_manager_snapshot"]:
                checkpoint_cleanup_candidates |= ManagerGitCheckpoint.deserialize(
                    str(row["agent_manager_snapshot"])
                ).cleanup_worktrees
            update_project(
                connection,
                repo,
                agent_manager_snapshot=ManagerGitCheckpoint(
                    validated_agent_scope,
                    checkpoint_cleanup_candidates,
                ).serialize(),
            )
    repo_path = validate_repository(
        repo_path,
        allow_agents_mismatch=allow_agents_mismatch,
    )
    backfill_plan_shas(connection, target_repo=repo)
    migrate_legacy_execplans(
        connection,
        reconcile_history=True,
        target_repo=repo,
    )
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row:
        hydrate_active_project(connection, repo)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    base = row["base_branch"] if row else requested_base

    _ensure_cleanup_pending_schema(connection)
    cleanup_candidates = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in connection.execute(
            "SELECT worktree FROM cleanup_pending "
            "WHERE repo = ? AND workspace_owned = ?",
            (repo, WORKSPACE_OWNED),
        ).fetchall()
    )
    cleanup_pending = recheck_cleanup_pending(
        connection, repo_path, repo, state_dir
    )
    remaining_cleanup = frozenset(
        str(_canonical_path(Path(item["worktree"])))
        for item in cleanup_pending
    )
    manager_worktree_changes = checkpoint_cleanup_candidates - remaining_cleanup
    for pending in cleanup_pending:
        operation_print(
            f"보존된 worktree: {pending['worktree']} ({pending['reason']})",
            file=sys.stderr,
        )

    if row and row["branch"]:
        existing_pr = pr_for_branch(repo_path, row["branch"], row["pr_url"])
        if existing_pr:
            update_project(connection, repo, pr_url=existing_pr["url"])
            if existing_pr["state"] == "OPEN":
                open_plan_id = _plan_id_from_path(str(row["plan_path"] or ""))
                open_worktree = Path(str(row["worktree"] or ""))
                open_branch = str(row["branch"])
                try:
                    if open_plan_id is None:
                        raise WorkspaceOwnershipError(
                            "PR_OPEN ExecPlan ID를 확인할 수 없습니다."
                        )
                    open_plan = active_execplan(connection, repo)
                    if (
                        open_plan is None
                        or open_plan["plan_id"] != open_plan_id
                        or int(open_plan["workspace_owned"]) != WORKSPACE_OWNED
                    ):
                        raise WorkspaceOwnershipError(
                            "PR_OPEN workspace의 Loop Manager 소유권이 확인되지 않았습니다."
                        )
                    validate_execplan_workspace_identity(
                        state_dir,
                        repo,
                        open_plan_id,
                        open_branch,
                        open_worktree,
                    )
                    make_worktree(
                        repo_path,
                        open_worktree,
                        open_branch,
                        str(row["base_branch"]),
                        allow_create_branch=False,
                        allow_recovery=True,
                        allow_existing=True,
                    )
                except (RuntimeError, WorkspaceOwnershipError) as error:
                    workspace_error = (
                        f"PR_OPEN workspace 소유권/복구 검증 실패: {error}"
                    )
                    block_active_execplan_from_revision(
                        connection,
                        repo_path,
                        repo,
                        f"refs/remotes/origin/{row['base_branch']}",
                        workspace_error,
                    )
                    operation_print(workspace_error, file=sys.stderr)
                    return 1
                operation_print(f"Merge 대기 중: {existing_pr['url']}")
                return 0
            if existing_pr["state"] == "MERGED" or existing_pr.get("mergedAt"):
                git(repo_path, "fetch", "origin")
                worktree = Path(row["worktree"])
                head_value = existing_pr.get("headRefOid")
                pr_head_oid = str(head_value) if head_value else None
                try:
                    assessment = complete_merged_worktree(
                        connection,
                        repo_path,
                        repo,
                        state_dir,
                        worktree,
                        str(existing_pr["url"]),
                        pr_head_oid,
                    )
                except (RuntimeError, WorkspaceOwnershipError) as error:
                    workspace_error = (
                        f"Merge cleanup workspace 소유권 검증 실패: {error}"
                    )
                    block_active_execplan_from_revision(
                        connection,
                        repo_path,
                        repo,
                        f"refs/remotes/origin/{row['base_branch']}",
                        workspace_error,
                    )
                    operation_print(workspace_error, file=sys.stderr)
                    return 1
                if assessment.status in {"safe", "missing"}:
                    operation_print(f"Merge 확인 및 worktree 정리 완료: {existing_pr['url']}")
                else:
                    operation_print(
                        f"Merge 확인 완료; worktree 보존: {worktree} "
                        f"({assessment.reason or assessment.status})",
                        file=sys.stderr,
                    )
                return PROJECT_STEP_PLANNING_PENDING
            update_project(connection, repo, phase="BLOCKED", last_error="PR이 Merge 없이 닫혔습니다.")
            operation_print(f"닫힌 PR을 다시 열어야 합니다: {existing_pr['url']}", file=sys.stderr)
            return 1

    if row and row["phase"] == "BLOCKED":
        git(repo_path, "fetch", "origin")
        if resume_blocked_execplan_if_inputs_changed(
            connection,
            repo_path,
            repo,
            str(row["base_branch"]),
            state_dir,
        ):
            validated_agent_scope = None
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            operation_print(
                f"작업이 차단되어 있습니다: {row['last_error'] or '사용자 조치가 필요합니다.'}",
                file=sys.stderr,
            )
            return 1

    base = remote_base(repo_path, requested_base or (row["base_branch"] if row else None))
    if validated_agent_scope is not None:
        try:
            validated_agent_scope = refresh_persisted_agent_scope_after_manager_git(
                connection,
                repo,
                validated_agent_scope,
                manager_worktree_changes,
            )
        except (AgentScopeError, RuntimeError) as error:
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            detail = str(error)
            message = block_persisted_agent_scope_violation(
                connection,
                repo_path,
                repo,
                row,
                detail,
            )
            operation_print(message, file=sys.stderr)
            return 1
    base_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
    current_fingerprint = planning_input_fingerprint(repo_path, base)

    # A fixed planning error must remain reportable even when the user's base
    # worktree later becomes unsafe. The fingerprint reads origin directly,
    # so this check neither reads nor alters the user's local changes.
    if row and row["planning_error"] and row["planning_error_fingerprint"] == current_fingerprint:
        operation_print(
            f"계획 오류가 해결되지 않았습니다: {row['planning_error']}\n"
            f"보존된 planning worktree: {row['worktree']}",
            file=sys.stderr,
        )
        return 1

    ensure_safe_base_worktree(repo_path, base)

    if not row:
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, ?)", (repo, base))
        connection.commit()
        migrate_legacy_execplans(
            connection,
            reconcile_history=True,
            target_repo=repo,
        )
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    elif not row["phase"]:
        update_project(connection, repo, base_branch=base)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if row["planning_error"]:
        stale_worktree = Path(row["worktree"]) if row["worktree"] else None
        stored_fingerprint = str(
            row["planning_attempt_fingerprint"]
            or row["planning_error_fingerprint"]
            or ""
        )
        try:
            if stale_worktree is None:
                raise WorkspaceOwnershipError(
                    "planning worktree 경로가 저장되지 않았습니다."
                )
            validate_planning_worktree_ownership(
                connection,
                repo_path,
                state_dir,
                repo,
                stored_fingerprint,
                stale_worktree,
            )
        except (RuntimeError, WorkspaceOwnershipError) as error:
            cleanup_error = (
                f"planning workspace cleanup 소유권 검증 실패: {error}"
            )
            update_project(
                connection,
                repo,
                planning_error=cleanup_error,
                planning_error_fingerprint=current_fingerprint,
                last_error=cleanup_error,
            )
            operation_print(cleanup_error, file=sys.stderr)
            return 1
        preserve_planning_diagnostics(state_dir, repo, row)
        if stale_worktree.exists():
            git(repo_path, "worktree", "remove", "--force", str(stale_worktree))
        clear_active(connection, repo)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if row["phase"] == "PLANNING":
        started_fingerprint = row["planning_attempt_fingerprint"]
        if not started_fingerprint:
            started_fingerprint = _planning_input_fingerprint(repo_path, row["active_sha"])
        if started_fingerprint != current_fingerprint:
            stale_worktree = Path(row["worktree"]) if row["worktree"] else None
            try:
                if stale_worktree is None:
                    raise WorkspaceOwnershipError(
                        "planning worktree 경로가 저장되지 않았습니다."
                    )
                validate_planning_worktree_ownership(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    str(started_fingerprint),
                    stale_worktree,
                )
            except (RuntimeError, WorkspaceOwnershipError) as error:
                cleanup_error = (
                    f"planning workspace cleanup 소유권 검증 실패: {error}"
                )
                update_project(
                    connection,
                    repo,
                    planning_error=cleanup_error,
                    planning_error_fingerprint=current_fingerprint,
                    last_error=cleanup_error,
                )
                operation_print(cleanup_error, file=sys.stderr)
                return 1
            if row["planning_validation_error"]:
                preserve_planning_diagnostics(state_dir, repo, row)
            if stale_worktree.exists():
                git(repo_path, "worktree", "remove", "--force", str(stale_worktree))
            clear_active(connection, repo)
            row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    planning_required = not row["phase"]

    if not row["phase"] and row["planning_fingerprint"] == current_fingerprint:
        update_project(connection, repo, processed_sha=base_sha)
        operation_print("계획 입력이 바뀌지 않아 Codex를 실행하지 않습니다.")
        planning_required = False

    if planning_required and row["planning_fingerprint"] and row["processed_sha"]:
        processed_fingerprint = _planning_input_fingerprint(repo_path, row["processed_sha"])
        if processed_fingerprint == current_fingerprint:
            update_project(connection, repo, processed_sha=base_sha)
            operation_print("계획 입력이 바뀌지 않아 Codex를 실행하지 않습니다.")
            planning_required = False
        elif _is_requirement_archive_only_change(repo_path, row["processed_sha"], base_sha):
            update_project(connection, repo, processed_sha=base_sha)
            operation_print("Requirement archive 이동 외의 계획 입력이 바뀌지 않아 Codex를 실행하지 않습니다.")
            planning_required = False

    if planning_required:
        project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
        worktree = state_dir / "worktrees" / project_id / "planning" / current_fingerprint
        update_project(
            connection,
            repo,
            active_sha=base_sha,
            plan_sha=None,
            planning_attempt_fingerprint=current_fingerprint,
            planning_workspace_owned=0,
            planning_retry_count=0,
            planner_session_id=None,
            worker_session_id=None,
            worker_result=None,
            verify_result=None,
            planning_validation_error=None,
            planning_result_pending_validation=0,
            planning_result_completed=0,
            planning_error=None,
            planning_error_fingerprint=None,
            phase="PLANNING",
            branch=None,
            worktree=str(worktree),
            plan_path=None,
            pr_url=None,
            last_error=None,
        )
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    if row["phase"] == "PLANNING":
        planning_worktree = Path(row["worktree"])
        try:
            validate_planning_workspace_identity(
                state_dir,
                repo,
                str(row["planning_attempt_fingerprint"]),
                planning_worktree,
            )
            planning_started = bool(
                row["planner_session_id"]
                or row["agent_snapshot"]
                or row["planning_result_pending_validation"]
                or row["planning_result_completed"]
                or row["planning_validation_error"]
                or row["planning_retry_count"]
                or row["last_error"]
            )
            planning_workspace_owned = (
                row["planning_workspace_owned"] == WORKSPACE_OWNED
            )
            planning_workspace_reserved = (
                row["planning_workspace_owned"] == WORKSPACE_RESERVED
            )
            make_planning_worktree(
                repo_path,
                planning_worktree,
                base,
                allow_create=(
                    not planning_started
                    and (
                        not planning_workspace_owned
                        or planning_workspace_reserved
                    )
                ),
                allow_existing=(
                    planning_workspace_owned or planning_workspace_reserved
                ),
                ownership_reserved=planning_workspace_reserved,
                expected_head=str(row["active_sha"] or ""),
                before_create=lambda: update_project(
                    connection,
                    repo,
                    planning_workspace_owned=WORKSPACE_RESERVED,
                ),
            )
            update_project(
                connection,
                repo,
                planning_workspace_owned=WORKSPACE_OWNED,
            )
        except WorkspaceOwnershipError as error:
            validation_error = f"planning workspace 소유권 검증 실패: {error}"
            update_project(
                connection,
                repo,
                planning_error=validation_error,
                planning_error_fingerprint=current_fingerprint,
                last_error=validation_error,
            )
            operation_print(validation_error, file=sys.stderr)
            return 1
        head = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
        dirty_files = changed_files(planning_worktree)
        if row["planning_validation_error"] and head != row["active_sha"]:
            # The Planner is not allowed to commit. Turn an invalid Planner
            # commit back into a worktree change so the persisted validation
            # error can be repaired in the same session on the next run.
            git(planning_worktree, "reset", "--mixed", str(row["active_sha"]))
            head = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
            dirty_files = changed_files(planning_worktree)
        planner_result_in_flight = bool(
            row["planning_result_pending_validation"]
            and not row["planning_result_completed"]
        )
        planner_scope_error: PlannerValidationError | None = None
        if planner_result_in_flight or (
            head == row["active_sha"]
            and (
                not dirty_files
                or row["planning_validation_error"]
                or row["last_error"]
            )
            and not row["planning_result_pending_validation"]
            and not (
                row["planning_validation_error"]
                and row["planning_retry_count"] >= MAX_PLANNER_VALIDATION_RETRIES
            )
        ):
            operation_log("Planner 시작: 다음 ExecPlan 계획을 생성합니다.")
            # A Planner can modify the worktree before its CLI invocation
            # returns. Persist this boundary first so an interrupted process
            # resumes that same session before validating its partial output.
            # A completed result is the only pending result that may be
            # validated after an interruption.
            if not planner_result_in_flight:
                update_project(
                    connection,
                    repo,
                    last_error=None,
                    planning_result_pending_validation=1,
                    planning_result_completed=0,
                )
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (repo,)
                ).fetchone()
            planner_scope_before = begin_agent_scope(
                connection,
                repo,
                planning_worktree,
                "planner",
            )
            result = run_agent_with_retries(
                lambda current_session_id, _session_recovery: run_codex(
                    planning_worktree,
                    state_dir,
                    config_path,
                    "planner",
                    planner_prompt(
                        row["planning_validation_error"] or row["last_error"],
                        compact_execplan_catalog(connection, repo),
                    ),
                    read_only=False,
                    persist_session=True,
                    session_id=current_session_id,
                    on_session_started=lambda started_session_id: update_project(
                        connection,
                        repo,
                        planner_session_id=started_session_id,
                    ),
                ),
                session_id=row["planner_session_id"],
            )
            code, message = result
            result_session_id = getattr(result, "session_id", None)
            if result_session_id:
                update_project(
                    connection,
                    repo,
                    planner_session_id=result_session_id,
                )
            planner_scope_after: GitSnapshot | None = None
            try:
                planner_scope_after = capture_git_snapshot(planning_worktree)
                validate_agent_scope(
                    "planner",
                    planning_worktree,
                    planner_scope_before,
                    planner_scope_after,
                )
                validate_managed_worktree(repo_path, planning_worktree, None)
            except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
                planner_scope_error = PlannerValidationError(str(error))
                if planner_scope_after is not None:
                    update_project(
                        connection,
                        repo,
                        agent_result_snapshot=planner_scope_after.serialize(),
                    )
            else:
                clear_agent_scope(connection, repo)

            if code:
                agent_error = message or f"Planner 종료 코드 {code}"
                if result.result_kind is AgentResultKind.TRANSIENT_FAILURE:
                    agent_error = f"{TRANSIENT_AGENT_ERROR_PREFIX} {agent_error}"
                    step_result = PROJECT_STEP_TRANSIENT_PAUSE
                elif result.result_kind is AgentResultKind.USAGE_LIMIT:
                    agent_error = f"{USAGE_LIMIT_ERROR_PREFIX} {agent_error}"
                    step_result = PROJECT_STEP_USAGE_PAUSE
                else:
                    step_result = 1
                update_project(
                    connection,
                    repo,
                    last_error=agent_error,
                    planning_result_pending_validation=0,
                    planning_result_completed=0,
                )
                operation_print(agent_error, file=sys.stderr)
                return step_result
            update_project(
                connection,
                repo,
                last_error=None,
                planning_result_completed=1,
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (repo,)
            ).fetchone()
            dirty_files = changed_files(planning_worktree)

        if planner_scope_error is None:
            scope_row = connection.execute(
                "SELECT agent_role, agent_snapshot FROM projects WHERE repo = ?",
                (repo,),
            ).fetchone()
            if scope_row["agent_snapshot"]:
                persisted_after: GitSnapshot | None = None
                try:
                    if scope_row["agent_role"] != "planner":
                        raise AgentScopeError(
                            "저장된 Agent 역할이 Planner와 다릅니다."
                        )
                    persisted_before = GitSnapshot.deserialize(
                        str(scope_row["agent_snapshot"])
                    )
                    persisted_after = capture_git_snapshot(planning_worktree)
                    validate_agent_scope(
                        "planner",
                        planning_worktree,
                        persisted_before,
                        persisted_after,
                    )
                    validate_managed_worktree(repo_path, planning_worktree, None)
                except (
                    AgentScopeError,
                    WorkspaceOwnershipError,
                    RuntimeError,
                ) as error:
                    planner_scope_error = PlannerValidationError(str(error))
                    if persisted_after is not None:
                        update_project(
                            connection,
                            repo,
                            agent_result_snapshot=persisted_after.serialize(),
                        )
                else:
                    clear_agent_scope(connection, repo)


        try:
            if planner_scope_error is not None:
                raise planner_scope_error
            validated_result = validate_planner_result(
                planning_worktree, str(row["active_sha"]), dirty_files
            )
        except PlannerValidationError as error:
            validation_error = str(error)
            retry_count = row["planning_retry_count"]
            if row["planning_result_pending_validation"] and row["planning_validation_error"]:
                retry_count += 1
            values = {
                "planning_validation_error": validation_error,
                "planning_result_pending_validation": 0,
                "planning_result_completed": 0,
                "last_error": None,
                "planning_retry_count": retry_count,
            }
            if retry_count >= MAX_PLANNER_VALIDATION_RETRIES:
                values.update(
                    planning_error=validation_error,
                    planning_error_fingerprint=current_fingerprint,
                )
            update_project(connection, repo, **values)
            operation_print(str(error), file=sys.stderr)
            return 1

        if validated_result is None:
            completed_fingerprint = _planning_input_fingerprint(repo_path, row["active_sha"])
            validate_planning_worktree_ownership(
                connection,
                repo_path,
                state_dir,
                repo,
                current_fingerprint,
                planning_worktree,
            )
            git(repo_path, "worktree", "remove", str(planning_worktree))
            clear_active(
                connection,
                repo,
                processed_sha=row["active_sha"],
                planning_fingerprint=completed_fingerprint,
            )
            operation_print("추가로 계획할 작업이 없습니다.")
            row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        else:
            plans = validated_result
            if dirty_files:
                # Publishing failures are operational failures, not Planner
                # validation failures. Leave the validated result for retry.
                plan_paths = [path for path, _plan in plans]
                git(planning_worktree, "add", "--", *plan_paths)
                first = plans[0][1]
                last = plans[-1][1]
                if len(plans) == 1:
                    message = f"Add ExecPlan {first['id']}: {first['title']}"
                else:
                    message = f"Add ExecPlans {first['id']}-{last['id']}"
                git(planning_worktree, "commit", "-m", message)

            clear_planning_validation_state(connection, repo)
            plan_commit = git(planning_worktree, "rev-parse", "HEAD").stdout.strip()
            git(repo_path, "fetch", "origin")
            latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
            if latest_sha not in {row["active_sha"], plan_commit}:
                published = git(
                    planning_worktree,
                    "merge-base",
                    "--is-ancestor",
                    plan_commit,
                    f"origin/{base}",
                    check=False,
                ).returncode == 0
                if not published:
                    validate_planning_worktree_ownership(
                        connection,
                        repo_path,
                        state_dir,
                        repo,
                        current_fingerprint,
                        planning_worktree,
                    )
                    git(repo_path, "worktree", "remove", str(planning_worktree))
                    clear_active(connection, repo)
                    operation_print(
                        "계획 중 base가 변경되어 최신 기준으로 다시 계획합니다.",
                        file=sys.stderr,
                    )
                    return 1
            if latest_sha != plan_commit and latest_sha == row["active_sha"]:
                pushed = git(
                    planning_worktree,
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{base}",
                    check=False,
                )
                if pushed.returncode:
                    update_project(
                        connection,
                        repo,
                        last_error=f"ExecPlan push 실패:\n{pushed.stdout[-8000:]}",
                    )
                    operation_print(pushed.stdout, file=sys.stderr)
                    return 1
                git(repo_path, "fetch", "origin")
                latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
                if latest_sha != plan_commit:
                    update_project(
                        connection,
                        repo,
                        last_error="ExecPlan push 결과를 origin/base에서 확인할 수 없습니다.",
                    )
                    operation_print("ExecPlan push 결과 확인에 실패했습니다.", file=sys.stderr)
                    return 1

            validate_planning_worktree_ownership(
                connection,
                repo_path,
                state_dir,
                repo,
                current_fingerprint,
                planning_worktree,
            )
            register_execplans(
                connection,
                repo,
                planning_worktree,
                plans,
                plan_commit,
                current_fingerprint,
            )
            git(repo_path, "worktree", "remove", str(planning_worktree))
            base_sha = latest_sha
            row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    newly_activated = False
    if not row["phase"]:
        git(repo_path, "fetch", "origin")
        latest_selection_sha = git(
            repo_path, "rev-parse", f"origin/{base}"
        ).stdout.strip()
        latest_selection_fingerprint = planning_input_fingerprint(repo_path, base)
        if latest_selection_fingerprint != current_fingerprint:
            operation_print(
                "계획 중 입력이 변경되어 READY Worker보다 재계획을 우선합니다.",
                file=sys.stderr,
            )
            return 1
        base_sha = latest_selection_sha
        selected = activate_ready_execplan(connection, repo, state_dir, base_sha)
        if selected is None:
            operation_print("실행 가능한 READY ExecPlan이 없습니다.")
            return 0
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        newly_activated = True
    branch = str(row["branch"] or "")
    worktree = Path(str(row["worktree"] or ""))
    plan_id = _plan_id_from_path(str(row["plan_path"] or ""))
    catalog_plan = active_execplan(connection, repo)
    workspace_ownership = int(catalog_plan["workspace_owned"] if catalog_plan else 0)
    workspace_owned = workspace_ownership == WORKSPACE_OWNED
    workspace_reserved = workspace_ownership == WORKSPACE_RESERVED
    rebase_error = str(row["last_error"] or "")
    rebase_recovery = bool(
        row["phase"] == "WORKING"
        and (
            rebase_error == REBASE_IN_PROGRESS_ERROR
            or rebase_error.startswith(REBASE_CONFLICT_PREFIX)
        )
    )
    resolving_rebase = bool(
        rebase_recovery
        and worktree.is_dir()
        and _rebase_in_progress_for_branch(worktree, branch)
    )
    try:
        if plan_id is None:
            raise WorkspaceOwnershipError(
                "활성 ExecPlan 경로에서 ID를 확인할 수 없습니다."
            )
        if catalog_plan is None or catalog_plan["plan_id"] != plan_id:
            raise WorkspaceOwnershipError(
                "활성 ExecPlan의 catalog 상태를 확인할 수 없습니다."
            )
        validate_execplan_workspace_identity(
            state_dir,
            repo,
            plan_id,
            branch,
            worktree,
        )
        make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            allow_create_branch=(
                newly_activated
                and not workspace_owned
                and not workspace_reserved
            ),
            allow_recovery=workspace_owned or workspace_reserved,
            allow_existing=workspace_owned or workspace_reserved,
            ownership_reserved=workspace_reserved,
            expected_head=str(row["active_sha"] or ""),
            before_create=lambda: reserve_execplan_workspace(
                connection,
                repo,
                plan_id,
            ),
            allow_rebase=resolving_rebase,
        )
        mark_execplan_workspace_owned(connection, repo, plan_id)
    except (RuntimeError, WorkspaceOwnershipError) as error:
        workspace_error = f"관리 workspace 소유권/복구 검증 실패: {error}"
        block_active_execplan_from_revision(
            connection,
            repo_path,
            repo,
            f"refs/remotes/origin/{base}",
            workspace_error,
            str(row["verify_result"] or ""),
        )
        operation_print(workspace_error, file=sys.stderr)
        return 1

    if rebase_recovery and not resolving_rebase:
        update_project(
            connection,
            repo,
            phase="VERIFYING",
            last_error=None,
        )
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    assert catalog_plan is not None
    try:
        plan = validate_active_execplan_metadata(
            repo_path,
            worktree,
            str(row["plan_path"]),
            catalog_plan,
        )
    except RuntimeError as error:
        metadata_error = str(error)
        block_active_execplan(
            connection,
            repo,
            worktree,
            metadata_error,
            str(row["verify_result"] or ""),
        )
        operation_print(metadata_error, file=sys.stderr)
        return 1

    if row["phase"] == "WORKING":
        operation_log(f"Worker 시작: {plan['id']} {plan['title']}")
        level = worker_level(plan)
        worker_scope_before = begin_agent_scope(
            connection,
            repo,
            worktree,
            "worker",
            input_fingerprint=_worktree_execplan_input_fingerprint(
                worktree, catalog_plan
            ),
        )
        persisted_validation_feedback = []
        if _deserialize_failure_history(
            catalog_plan["verify_failure_history"]
        ):
            persisted_validation_feedback.append(str(row["verify_result"] or ""))
        if _deserialize_failure_history(
            catalog_plan["review_failure_history"]
        ):
            persisted_validation_feedback.append(str(row["review_result"] or ""))
        validation_feedback = "\n\n".join(
            feedback for feedback in persisted_validation_feedback if feedback
        )
        result = run_agent_with_retries(
            lambda current_session_id, session_recovery: run_codex(
                worktree,
                state_dir,
                config_path,
                "worker",
                worker_prompt(
                    row["plan_path"],
                    row["last_error"],
                    row["worker_result"],
                    validation_feedback,
                    session_recovery=session_recovery,
                ),
                read_only=False,
                level=level,
                persist_session=True,
                session_id=current_session_id,
                on_session_started=lambda started_session_id: update_project(
                    connection,
                    repo,
                    worker_session_id=started_session_id,
                ),
            ),
            session_id=row["worker_session_id"],
        )
        code, message = result
        result_session_id = getattr(result, "session_id", None)
        if result_session_id:
            update_project(connection, repo, worker_session_id=result_session_id)

        try:
            plan = validate_worker_workspace_scope(
                repo_path,
                worktree,
                branch,
                str(row["plan_path"]),
                catalog_plan,
                worker_scope_before,
                allow_rebase=resolving_rebase,
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                scope_error,
                str(row["verify_result"] or ""),
            )
            operation_print(scope_error, file=sys.stderr)
            return 1
        clear_agent_scope(connection, repo)


        if code:
            worker_error = message or f"Codex 종료 코드 {code}"
            if resolving_rebase:
                worker_error = REBASE_CONFLICT_PREFIX + worker_error
            if result.result_kind is AgentResultKind.TRANSIENT_FAILURE:
                worker_error = f"{TRANSIENT_AGENT_ERROR_PREFIX} {worker_error}"
                step_result = PROJECT_STEP_TRANSIENT_PAUSE
            elif result.result_kind is AgentResultKind.USAGE_LIMIT:
                worker_error = f"{USAGE_LIMIT_ERROR_PREFIX} {worker_error}"
                step_result = PROJECT_STEP_USAGE_PAUSE
            else:
                step_result = 1
            update_project(
                connection, repo, phase="WORKING", last_error=worker_error
            )
            operation_print(worker_error, file=sys.stderr)
            return step_result
        try:
            parse_worker_result(message, plan)
        except WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            update_project(
                connection,
                repo,
                phase="WORKING",
                last_error=validation_error,
            )
            operation_print(validation_error, file=sys.stderr)
            return 1
        if resolving_rebase:
            git(worktree, "add", "-A")
            continued = git(
                worktree,
                "-c",
                "core.editor=true",
                "rebase",
                "--continue",
                check=False,
            )
            if continued.returncode:
                continuation_error = REBASE_CONFLICT_PREFIX + continued.stdout[-8000:]
                if not _rebase_in_progress_for_branch(worktree, branch):
                    block_active_execplan(
                        connection,
                        repo,
                        worktree,
                        continuation_error,
                        str(row["verify_result"] or ""),
                    )
                else:
                    update_project(
                        connection,
                        repo,
                        phase="WORKING",
                        last_error=continuation_error,
                    )
                operation_print(continuation_error, file=sys.stderr)
                return 1
            latest_sha = git(
                worktree,
                "rev-parse",
                f"origin/{base}",
            ).stdout.strip()
            update_project(connection, repo, active_sha=latest_sha)

        update_project(
            connection,
            repo,
            phase="VERIFYING",
            worker_result=message,
            verify_result=None,
            last_error=None,
        )

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "VERIFYING":
        operation_log(f"전체 verify 시작: {plan['id']} {plan['title']}")
        try:
            worker_publication = parse_worker_result(str(row["worker_result"] or ""), plan)
        except WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            update_project(connection, repo, phase="WORKING", last_error=validation_error)
            operation_print(validation_error, file=sys.stderr)
            return 1

        verify_scope_before = begin_agent_scope(
            connection,
            repo,
            worktree,
            "worker",
            input_fingerprint=_worktree_execplan_input_fingerprint(
                worktree, catalog_plan
            ),
        )
        passed, output = verify(worktree)
        try:
            plan = validate_worker_workspace_scope(
                repo_path,
                worktree,
                branch,
                str(row["plan_path"]),
                catalog_plan,
                verify_scope_before,
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                scope_error,
                output,
            )
            operation_print(scope_error, file=sys.stderr)
            return 1
        clear_agent_scope(connection, repo)
        if not passed:
            operation_log(f"Worker 반환: {plan['id']} {plan['title']} (전체 verify 실패)", error=True)
            decision = transition_validation_failure(
                connection,
                repo,
                worktree,
                "verify",
                output,
            )
            operation_print(output, file=sys.stderr)
            return 1 if decision.blocked else PROJECT_STEP_RETRY

        # Every successful verify is a progress boundary. Clear its stage-local
        # history before checking for a newer base or running a second verify
        # after rebase, so later failures cannot inherit pre-success repeats.
        clear_validation_failure_history(connection, repo, "verify")

        dirty = bool(git(worktree, "status", "--porcelain").stdout.strip())
        if not dirty and not branch_has_changes(worktree, base):
            no_change_error = (
                "Worker가 변경을 만들지 않아 PR을 생성할 수 없습니다. "
                "ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다."
            )
            block_active_execplan(
                connection,
                repo,
                worktree,
                no_change_error,
                output,
            )
            operation_print(no_change_error, file=sys.stderr)
            return 1
        if dirty:
            git(worktree, "add", "-A")
            plan = validate_active_execplan_metadata(
                repo_path,
                worktree,
                str(row["plan_path"]),
                catalog_plan,
            )
            git(worktree, "commit", "-m", worker_publication.commit_title)

        git(repo_path, "fetch", "origin")
        latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
        if latest_sha != row["active_sha"]:
            update_project(
                connection,
                repo,
                phase="WORKING",
                last_error=REBASE_IN_PROGRESS_ERROR,
            )
            rebased = git(worktree, "rebase", f"origin/{base}", check=False)
            if rebased.returncode:
                if _rebase_in_progress_for_branch(worktree, branch):
                    rebase_failure = REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:]
                else:
                    rebase_failure = f"rebase 실패:\n{rebased.stdout[-8000:]}"
                update_project(
                    connection, repo, phase="WORKING", last_error=rebase_failure
                )
                operation_print(rebased.stdout, file=sys.stderr)
                return 1
            # The rebase completed even if its follow-up verify later fails.
            # Persist the new base now so recovery does not repeat that rebase.
            update_project(connection, repo, active_sha=latest_sha)
            operation_log(f"전체 verify 시작: {plan['id']} {plan['title']} (rebase 후)")
            verify_scope_before = begin_agent_scope(
                connection,
                repo,
                worktree,
                "worker",
                input_fingerprint=_worktree_execplan_input_fingerprint(
                    worktree, catalog_plan
                ),
            )
            passed, output = verify(worktree)
            try:
                plan = validate_worker_workspace_scope(
                    repo_path,
                    worktree,
                    branch,
                    str(row["plan_path"]),
                    catalog_plan,
                    verify_scope_before,
                )
            except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
                scope_error = str(error)
                block_active_execplan(
                    connection,
                    repo,
                    worktree,
                    scope_error,
                    output,
                )
                operation_print(scope_error, file=sys.stderr)
                return 1
            clear_agent_scope(connection, repo)
            if not passed:
                operation_log(f"Worker 반환: {plan['id']} {plan['title']} (rebase 후 전체 verify 실패)", error=True)
                decision = transition_validation_failure(
                    connection,
                    repo,
                    worktree,
                    "verify",
                    output,
                )
                operation_print(output, file=sys.stderr)
                return 1 if decision.blocked else PROJECT_STEP_RETRY
            clear_validation_failure_history(connection, repo, "verify")
            if git(worktree, "status", "--porcelain").stdout.strip():
                git(worktree, "add", "-A")
                plan = validate_active_execplan_metadata(
                    repo_path,
                    worktree,
                    str(row["plan_path"]),
                    catalog_plan,
                )
                git(worktree, "commit", "-m", worker_publication.commit_title)
            update_project(connection, repo, active_sha=latest_sha, verify_result=output)
        else:
            update_project(connection, repo, verify_result=output)
        try:
            plan = validate_active_execplan_metadata(
                repo_path,
                worktree,
                str(row["plan_path"]),
                catalog_plan,
            )
        except RuntimeError as error:
            metadata_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                metadata_error,
                output,
            )
            operation_print(metadata_error, file=sys.stderr)
            return 1

        if not branch_has_changes(worktree, base):
            no_change_error = (
                "rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다. "
                "ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다."
            )
            block_active_execplan(
                connection,
                repo,
                worktree,
                no_change_error,
                output,
            )
            operation_print(no_change_error, file=sys.stderr)
            return 1
        update_project(connection, repo, phase="REVIEWING", last_error=None)

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "REVIEWING":
        operation_log(f"Reviewer 시작: {plan['id']} {plan['title']}")
        reviewer_scope_before = begin_agent_scope(
            connection,
            repo,
            worktree,
            "reviewer",
            input_fingerprint=_worktree_execplan_input_fingerprint(
                worktree, catalog_plan
            ),
        )

        review_result = run_agent_with_retries(
            lambda current_session_id, session_recovery: run_codex(
                worktree,
                state_dir,
                config_path,
                "reviewer",
                reviewer_prompt(
                    base,
                    row["worker_result"],
                    row["verify_result"],
                    row["review_result"],
                    session_recovery=session_recovery,
                ),
                read_only=True,
                persist_session=True,
                session_id=current_session_id,
                on_session_started=lambda started_session_id: update_project(
                    connection,
                    repo,
                    reviewer_session_id=started_session_id,
                ),
            ),
            session_id=row["reviewer_session_id"],
        )
        code, review = review_result
        if review_result.session_id:
            update_project(
                connection,
                repo,
                reviewer_session_id=review_result.session_id,
            )
        try:
            reviewer_scope_after = capture_git_snapshot(worktree)
            validate_agent_scope(
                "reviewer",
                worktree,
                reviewer_scope_before,
                reviewer_scope_after,
            )
            validate_managed_worktree(repo_path, worktree, branch)
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            block_active_execplan(
                connection,
                repo,
                worktree,
                scope_error,
                str(row["verify_result"] or ""),
            )
            operation_print(scope_error, file=sys.stderr)
            return 1
        clear_agent_scope(connection, repo)

        if code:
            reviewer_error = review or f"Reviewer 종료 코드 {code}"
            if review_result.result_kind is AgentResultKind.TRANSIENT_FAILURE:
                reviewer_error = f"{TRANSIENT_AGENT_ERROR_PREFIX} {reviewer_error}"
                step_result = PROJECT_STEP_TRANSIENT_PAUSE
            elif review_result.result_kind is AgentResultKind.USAGE_LIMIT:
                reviewer_error = f"{USAGE_LIMIT_ERROR_PREFIX} {reviewer_error}"
                step_result = PROJECT_STEP_USAGE_PAUSE
            else:
                step_result = 1
            update_project(
                connection,
                repo,
                phase="REVIEWING",
                last_error=reviewer_error,
                review_result=review,
            )
            operation_print(reviewer_error, file=sys.stderr)
            return step_result

        try:
            verdict, _issue_ids = parse_reviewer_result(review)
        except ReviewerProtocolError as error:
            reviewer_error = f"Reviewer 결과 프로토콜 오류: {error}"
            update_project(
                connection,
                repo,
                phase="REVIEWING",
                last_error=reviewer_error,
                review_result=review,
            )
            operation_print(reviewer_error, file=sys.stderr)
            return 1

        if verdict == "FAIL":
            decision = transition_validation_failure(
                connection,
                repo,
                worktree,
                "review",
                review,
            )
            operation_log(f"Worker 반환: {plan['id']} {plan['title']} (Review 실패)", error=True)
            operation_print(review or "Reviewer 결과가 유효하지 않습니다.", file=sys.stderr)
            return 1 if decision.blocked else PROJECT_STEP_RETRY
        clear_validation_failure_history(connection, repo, "review")
        update_project(
            connection,
            repo,
            phase="PUBLISHING",
            last_error=None,
            review_result=review,
        )

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "PUBLISHING":
        existing_pr = pr_for_branch(repo_path, branch)
        if not existing_pr:
            plan_sha = str(row["plan_sha"] or "")
            if not plan_sha:
                update_project(connection, repo, phase="BLOCKED", last_error="계획 commit SHA가 없습니다.")
                return 1
            plan_text = git(worktree, "show", f"{plan_sha}:{row['plan_path']}").stdout
            plan = published_execplan(str(row["plan_path"]), plan_text)
            try:
                worker_publication = parse_worker_result(str(row["worker_result"] or ""), plan)
            except WorkerResultError as error:
                validation_error = f"Worker 결과 검증 실패:\n{error}"
                update_project(connection, repo, phase="WORKING", last_error=validation_error)
                operation_print(validation_error, file=sys.stderr)
                return 1
            origin_url = git(worktree, "remote", "get-url", "origin").stdout.strip()
            body = build_pr_body(
                worker_publication,
                plan,
                str(row["plan_path"]),
                plan_text,
                origin_url, plan_sha,
            )
            git(worktree, "push", "-u", "origin", branch)
            title = f"Implement {plan['id']}: {plan['title']}"
            created = command(
                [
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    base,
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body",
                    body,
                ],
                cwd=worktree,
            )
            url = created.stdout.strip().splitlines()[-1]
        else:
            url = str(existing_pr["url"])
        update_project(connection, repo, phase="PR_OPEN", pr_url=url, last_error=None)
        operation_print(f"PR 생성 완료: {url}")
    return 0


def run_project_result(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
) -> ProjectRunResult:
    """Run retryable validation progress to a stable project-level outcome."""

    while True:
        step_result = _run_project_step(
            connection,
            state_dir,
            repo_arg,
            config_path,
            requested_base,
            allow_agents_mismatch,
        )
        if step_result != PROJECT_STEP_RETRY:
            break

    repo = str(Path(repo_arg).expanduser().resolve())
    row = connection.execute(
        "SELECT phase, last_error FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    detail = str(row["last_error"] or "") if row else None
    if step_result == PROJECT_STEP_TRANSIENT_PAUSE:
        return ProjectRunResult(ProjectRunKind.TRANSIENT_PAUSE, 0, detail)
    if step_result == PROJECT_STEP_USAGE_PAUSE:
        return ProjectRunResult(ProjectRunKind.USAGE_LIMIT_PAUSE, 0, detail)
    if step_result == PROJECT_STEP_PLANNING_PENDING:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    if row is not None and row["phase"] == "BLOCKED":
        return ProjectRunResult(ProjectRunKind.BLOCKED, 1, detail)
    if step_result != 0:
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, detail)
    if row is not None and row["phase"] is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    unfinished = connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' LIMIT 1",
        (repo,),
    ).fetchone()
    if unfinished is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    return ProjectRunResult(ProjectRunKind.COMPLETED, 0, detail)


def run_project(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
) -> int:
    """CLI-compatible adapter for the reusable structured project result."""

    return run_project_result(
        connection,
        state_dir,
        repo_arg,
        config_path,
        requested_base,
        allow_agents_mismatch,
    ).exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="기본 운영체제 상태 디렉터리 대신 사용할 경로")
    parser.add_argument("--config", default=str(ROOT / "config/config.ini"), help="config.ini 경로")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="상태 디렉터리와 SQLite를 초기화")
    run = subcommands.add_parser("run", help="프로젝트에서 다음 최소 작업 하나를 처리")
    run.add_argument("repository", help="대상 Git 저장소 루트")
    run.add_argument("--base", help="기준 branch. 생략하면 origin/HEAD 사용")
    run.add_argument(
        "--allow-agents-mismatch",
        action="store_true",
        help="공통 AGENTS.md가 포함되지 않은 프로젝트를 확인 후 강제 실행",
    )
    run.add_argument(
        "--skip-if-running",
        action="store_true",
        help="다른 Loop Manager가 실행 중이면 스케줄러용으로 조용히 성공 종료",
    )
    return parser


def main() -> int:
    args, _ = build_parser().parse_known_args()
    state_dir = state_directory(args.state_dir)
    try:
        with process_lock(state_dir):
            if args.command == "init":
                with connect_database(state_dir, create=True):
                    pass
                operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
                return 0
            with connect_database(state_dir) as connection:
                return run_project(
                    connection,
                    state_dir,
                    args.repository,
                    Path(args.config).expanduser().resolve(),
                    args.base,
                    args.allow_agents_mismatch,
                )
    except ProcessLockBusyError as error:
        if args.command == "run" and args.skip_if_running:
            return 0
        operation_log(f"오류: {error}", error=True)
        return 1
    except (RuntimeError, OSError, sqlite3.DatabaseError, json.JSONDecodeError) as error:
        operation_log(f"오류: {error}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
