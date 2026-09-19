#!/usr/bin/env python3
"""Codex가 Loop Manager의 다음 PR을 만들게 하는 최소 부트스트랩 실행기."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import configparser
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
MAX_PLANNER_VALIDATION_RETRIES = 3
PLAN_SHA_BACKFILL_ERROR = "계획 commit SHA를 기존 작업에서 안전하게 복구할 수 없습니다."
FIELDS = {
    "base_branch",
    "processed_sha",
    "planning_fingerprint",
    "planning_attempt_fingerprint",
    "planning_retry_count",
    "planner_session_id",
    "worker_session_id",
    "worker_result",
    "verify_result",
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
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    repo TEXT PRIMARY KEY,
    base_branch TEXT NOT NULL,
    processed_sha TEXT,
    planning_fingerprint TEXT,
    planning_attempt_fingerprint TEXT,
    planning_retry_count INTEGER NOT NULL DEFAULT 0,
    planner_session_id TEXT,
    worker_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
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
    last_error TEXT
)
"""

CLEANUP_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup_pending (
    repo TEXT NOT NULL,
    worktree TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    pr_head_oid TEXT,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, worktree),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


@dataclass(frozen=True)
class CodexResult:
    code: int
    message: str
    session_id: str | None = None

    def __iter__(self):
        # Keep two-value unpacking compatible with callers that do not manage sessions.
        return iter((self.code, self.message))


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

class WorkerResultError(RuntimeError):
    """A Worker final response cannot safely be published."""

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
        raise RuntimeError("다른 Loop Manager 프로세스가 실행 중입니다.") from error
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def connect_database(state_dir: Path, *, create: bool = False) -> sqlite3.Connection:
    database = state_dir / "loop-manager.db"
    if not create and not database.is_file():
        raise RuntimeError("상태 DB가 없습니다. 먼저 `loop_manager.py init`을 실행하세요.")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    if create:
        connection.execute(SCHEMA)
        connection.execute(CLEANUP_PENDING_SCHEMA)
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
        "planning_retry_count": "INTEGER NOT NULL DEFAULT 0",
        "planner_session_id": "TEXT",
        "plan_sha": "TEXT",
        "worker_session_id": "TEXT",
        "worker_result": "TEXT",
        "verify_result": "TEXT",
        "planning_validation_error": "TEXT",
        "planning_result_pending_validation": "INTEGER NOT NULL DEFAULT 0",
        "planning_result_completed": "INTEGER NOT NULL DEFAULT 0",
        "planning_error": "TEXT",
        "planning_error_fingerprint": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {definition}")
    if not migrations.keys() <= columns:
        connection.commit()
    connection.execute(CLEANUP_PENDING_SCHEMA)
    connection.commit()
    backfill_plan_shas(connection)
    return connection


def update_project(connection: sqlite3.Connection, repo: str, **values: object) -> None:
    if not values or not set(values).issubset(FIELDS):
        raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
    assignments = ", ".join(f"{name} = ?" for name in values)
    connection.execute(
        f"UPDATE projects SET {assignments} WHERE repo = ?",
        [*values.values(), repo],
    )
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


def backfill_plan_shas(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT repo, plan_path
        FROM projects
        WHERE plan_sha IS NULL
          AND plan_path IS NOT NULL
          AND phase IN ('WORKING', 'VERIFYING', 'REVIEWING', 'PUBLISHING')
        """
    ).fetchall()
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


def _parse_execplan(worktree: Path, path: Path) -> dict[str, object]:
    allowed = {"id", "title", "sources", "depends_on", "complexity", "risk"}
    values = parse_frontmatter(path.read_text(encoding="utf-8"))
    if set(values) != allowed:
        raise RuntimeError("ExecPlan frontmatter key가 docs/PLANS.md 형식과 다릅니다.")
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
        if not (worktree / source).is_file():
            raise RuntimeError(f"존재하지 않는 ExecPlan source입니다: {source}")
    for dependency in dependencies:
        if not isinstance(dependency, str) or not re.fullmatch(r"G-\d{3,}", dependency):
            raise RuntimeError(f"ExecPlan depends_on ID 형식이 유효하지 않습니다: {dependency}")
    if values["complexity"] not in {"low", "medium", "high"}:
        raise RuntimeError("ExecPlan complexity가 유효하지 않습니다.")
    if values["risk"] not in {"low", "medium", "high", "critical"}:
        raise RuntimeError("ExecPlan risk가 유효하지 않습니다.")
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


def load_execplan_catalog(worktree: Path) -> dict[str, dict[str, object]]:
    entries: list[tuple[str, dict[str, object]]] = []
    plans_directory = worktree / "docs/plans"
    plan_paths = sorted(
        (path for path in plans_directory.glob("*.md") if path.is_file()),
        key=lambda path: path.name,
    )
    for path in plan_paths:
        plan = _parse_execplan(worktree, path)
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
) -> tuple[str, dict[str, object]] | None:
    """Validate only the Planner-owned output, leaving Git publication outside.

    Scope and ExecPlan catalog errors are deliberately translated to a
    dedicated exception. Operational Git failures must retain their normal
    recovery boundary and never consume the Planner validation retry budget.
    """
    if dirty_files:
        if len(dirty_files) != 1 or not re.fullmatch(r"docs/plans/[^/]+\.md", dirty_files[0]):
            raise PlannerValidationError("Planner는 docs/plans의 ExecPlan 하나만 변경해야 합니다.")
        plan_path = dirty_files[0]
    else:
        planned_files = changed_files(worktree, active_sha)
        if not planned_files:
            return None
        if len(planned_files) != 1 or not re.fullmatch(r"docs/plans/[^/]+\.md", planned_files[0]):
            raise PlannerValidationError("Planner commit에는 ExecPlan 하나만 있어야 합니다.")
        plan_path = planned_files[0]

    try:
        return plan_path, validate_execplan(worktree, plan_path)
    except RuntimeError as error:
        raise PlannerValidationError(str(error)) from error


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
            )
        returncode = result.returncode
    message = last_message.read_text(encoding="utf-8") if last_message.is_file() else ""
    if returncode and not message:
        message = log.read_text(encoding="utf-8")[-6000:]
    if persist_session and not resolved_session_id:
        if returncode == 0:
            return CodexResult(
                1,
                f"Codex {role_name} 실행 결과에서 세션 ID를 확인할 수 없습니다.",
            )
    if persist_session or session_id:
        return CodexResult(returncode, message.strip(), resolved_session_id)
    return returncode, message.strip()


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
    return result.returncode == 0, result.stdout[-8000:].strip()


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


def make_worktree(repo: Path, worktree: Path, branch: str, base: str) -> None:
    if worktree.is_dir():
        return
    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    if branch_exists:
        git(repo, "worktree", "add", str(worktree), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")


def make_planning_worktree(repo: Path, worktree: Path, base: str) -> None:
    if worktree.is_dir():
        return
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(worktree), f"origin/{base}")


def changed_files(worktree: Path, since: str | None = None) -> list[str]:
    if since:
        output = git(worktree, "diff", "--name-only", f"{since}..HEAD").stdout
        return sorted(line for line in output.splitlines() if line)
    unstaged = git(worktree, "diff", "--name-only").stdout.splitlines()
    staged = git(worktree, "diff", "--cached", "--name-only").stdout.splitlines()
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted(set(unstaged + staged + untracked))


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


def _store_cleanup_pending(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
    reason: str,
) -> None:
    connection.execute(
        """
        INSERT INTO cleanup_pending (repo, worktree, pr_url, pr_head_oid, reason)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(repo, worktree) DO UPDATE SET
            pr_url = excluded.pr_url,
            pr_head_oid = excluded.pr_head_oid,
            reason = excluded.reason,
            updated_at = CURRENT_TIMESTAMP
        """,
        (repo, str(worktree), pr_url, pr_head_oid, reason),
    )


def recheck_cleanup_pending(
    connection: sqlite3.Connection, repo_path: Path, repo: str
) -> list[sqlite3.Row]:
    _ensure_cleanup_pending_schema(connection)
    pending = connection.execute(
        "SELECT * FROM cleanup_pending WHERE repo = ? ORDER BY worktree", (repo,)
    ).fetchall()
    for item in pending:
        worktree = Path(item["worktree"])
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
    diff = ""
    if worktree and worktree.is_dir():
        try:
            # Compare with the planning base rather than HEAD so an invalid
            # Planner commit remains in the diagnostic when this worktree is
            # forcibly removed after its input changes. Intent-to-add makes
            # untracked output visible as well.
            git(worktree, "add", "--intent-to-add", "-A")
            base_revision = row["active_sha"] or "HEAD"
            diff = git(worktree, "diff", "--binary", base_revision).stdout
        except RuntimeError as error:
            diff = f"diff 보존 실패: {error}"
    diagnostic = planning_diagnostics_path(state_dir, repo, fingerprint)
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    error = (
        row["planning_error"]
        or row["planning_validation_error"]
        or row["last_error"]
        or ""
    )
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
        worker_result=None,
        verify_result=None,
        planning_validation_error=None,
        planning_result_pending_validation=0,
        planning_result_completed=0,
        planning_error=None,
        planning_error_fingerprint=None,
        last_error=None,
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
        "planning_retry_count": 0,
        "planner_session_id": None,
        "worker_session_id": None,
        "worker_result": None,
        "verify_result": None,
        "planning_validation_error": None,
        "planning_result_pending_validation": 0,
        "planning_result_completed": 0,
        "planning_error": None,
        "planning_error_fingerprint": None,
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


def complete_merged_worktree(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    assessment = assess_cleanup(worktree, pr_head_oid)
    assessment = remove_cleanup_worktree(repo_path, worktree, assessment)
    values = _clear_active_values()
    assignments = ", ".join(f"{name} = ?" for name in values)
    with connection:
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
            )
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE repo = ?",
            [*values.values(), repo],
        )
    return assessment


def planner_prompt(error: str | None) -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    return f"""
당신은 Loop Manager의 Planner다.
AGENTS.md, docs/PRODUCT.md, docs/ARCHITECTURE.md, docs/PLANS.md와 기존 docs/plans를 먼저 읽어라.
현재 미구현 목표 중 가장 작고 독립적인 한 단계가 있으면 docs/PLANS.md 형식의 ExecPlan 하나만 docs/plans에 작성하라.
docs/plans 아래의 해당 파일 외에는 수정하지 마라. 구현, 테스트, commit, push와 PR 생성은 하지 마라.
추가 계획이 필요 없으면 파일을 변경하지 마라.
{failure}
""".strip()


def worker_prompt(plan_path: str, error: str | None) -> str:
    failure = f"\n이전 실행에서 다음 문제가 발생했다. 원인을 해결하라:\n{error[-6000:]}\n" if error else ""
    return f"""
당신은 Loop Manager의 Worker다.
AGENTS.md와 {plan_path}를 읽고 해당 ExecPlan 하나만 구현하라.
관련 targeted test를 작성하되 commit, push, PR 생성과 scripts/verify 실행은 하지 마라.
docs/plans에 새 ExecPlan을 만들지 말고 불필요한 후속 기능도 구현하지 마라.
구현과 관련 unit test를 작성하거나 갱신한 뒤, 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 targeted unit test만 실행하라. 개별 test case, test class, test module 또는 이에 준하는 좁은 단위만 허용된다.
targeted unit test가 실패하면 같은 세션에서 원인을 수정하고 같은 targeted test를 다시 실행하라. 문서·설정 전용 변경이거나 실행 가능한 관련 unit test가 없으면 테스트 범위를 확대하지 말고, 실행하지 못한 구체적인 사유를 최종 결과에 남겨라.
전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check 및 scripts/verify 실행은 절대 실행하지 마라. 전체 verify는 Worker 종료 뒤 Loop Manager가 실행하며, 실패한 경우에만 이 세션으로 진단을 전달한다.
작업이 완료되면 최종 응답 마지막에 아래 형식을 정확히 포함하라. 제목과 Summary는 최종 diff 전체를 설명해야 하며 ExecPlan 제목이나 일반적인 변경 문구를 쓰지 마라:
COMMIT_TITLE: 한 줄의 구체적인 변경 제목
PR_SUMMARY: 실제 변경을 간결히 설명하는 Summary
TARGETED_TESTS:
실행한 최소 범위 targeted unit test와 결과, 또는 실행하지 못한 구체적인 사유
{failure}
""".strip()



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
    if set(plan) != required:
        raise RuntimeError("게시된 ExecPlan frontmatter가 올바르지 않습니다.")
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
) -> str:
    worker_evidence = (worker_result or "Worker가 targeted unit test 결과 또는 미실행 사유를 보고하지 않았습니다.")[-6000:]
    verify_evidence = (verify_result or "Loop Manager 전체 verify가 성공했습니다(출력 없음).")[-6000:]
    return f"""
origin/{base}와 현재 HEAD의 차이를 읽기 전용으로 검토하라.
AGENTS.md, 근거 PRODUCT/Requirement, ARCHITECTURE와 ExecPlan을 기준으로 correctness, 회귀, 테스트 누락, verify 약화를 확인하라.
Worker가 보고한 targeted unit test 결과 또는 미실행 사유:
{worker_evidence}

Loop Manager가 실행한 전체 verify 결과:
{verify_evidence}

테스트 command와 scripts/verify를 실행하지 말고, 코드·테스트·verify 스크립트의 diff 및 위 결과만으로 판단하라.
blocking issue가 있으면 간결히 설명하고 마지막 줄을 `VERDICT: FAIL`로 끝내라.
blocking issue가 없으면 마지막 줄을 `VERDICT: PASS`로 끝내라. 파일은 수정하지 마라.
""".strip()


def run_project(
    connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
) -> int:
    repo_path = validate_repository(Path(repo_arg), allow_agents_mismatch=allow_agents_mismatch)
    repo = str(repo_path)
    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    base = row["base_branch"] if row else requested_base

    cleanup_pending = recheck_cleanup_pending(connection, repo_path, repo)
    for pending in cleanup_pending:
        print(
            f"보존된 worktree: {pending['worktree']} ({pending['reason']})",
            file=sys.stderr,
        )

    if row and row["branch"]:
        existing_pr = pr_for_branch(repo_path, row["branch"], row["pr_url"])
        if existing_pr:
            update_project(connection, repo, pr_url=existing_pr["url"])
            if existing_pr["state"] == "OPEN":
                print(f"Merge 대기 중: {existing_pr['url']}")
                return 0
            if existing_pr["state"] == "MERGED" or existing_pr.get("mergedAt"):
                git(repo_path, "fetch", "origin")
                worktree = Path(row["worktree"])
                head_value = existing_pr.get("headRefOid")
                pr_head_oid = str(head_value) if head_value else None
                assessment = complete_merged_worktree(
                    connection,
                    repo_path,
                    repo,
                    worktree,
                    str(existing_pr["url"]),
                    pr_head_oid,
                )
                if assessment.status in {"safe", "missing"}:
                    print(f"Merge 확인 및 worktree 정리 완료: {existing_pr['url']}")
                else:
                    print(
                        f"Merge 확인 완료; worktree 보존: {worktree} "
                        f"({assessment.reason or assessment.status})",
                        file=sys.stderr,
                    )
                return 0
            update_project(connection, repo, phase="BLOCKED", last_error="PR이 Merge 없이 닫혔습니다.")
            print(f"닫힌 PR을 다시 열어야 합니다: {existing_pr['url']}", file=sys.stderr)
            return 1

    if row and row["phase"] == "BLOCKED":
        print(
            f"작업이 차단되어 있습니다: {row['last_error'] or '사용자 조치가 필요합니다.'}",
            file=sys.stderr,
        )
        return 1

    base = remote_base(repo_path, requested_base or (row["base_branch"] if row else None))
    base_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
    current_fingerprint = planning_input_fingerprint(repo_path, base)

    # A fixed planning error must remain reportable even when the user's base
    # worktree later becomes unsafe. The fingerprint reads origin directly,
    # so this check neither reads nor alters the user's local changes.
    if row and row["planning_error"] and row["planning_error_fingerprint"] == current_fingerprint:
        print(
            f"계획 오류가 해결되지 않았습니다: {row['planning_error']}\n"
            f"보존된 planning worktree: {row['worktree']}",
            file=sys.stderr,
        )
        return 1

    ensure_safe_base_worktree(repo_path, base)

    if not row:
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, ?)", (repo, base))
        connection.commit()
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    elif not row["phase"]:
        update_project(connection, repo, base_branch=base)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if row["planning_error"]:
        preserve_planning_diagnostics(state_dir, repo, row)
        stale_worktree = Path(row["worktree"]) if row["worktree"] else None
        if stale_worktree and stale_worktree.exists():
            git(repo_path, "worktree", "remove", "--force", str(stale_worktree))
        clear_active(connection, repo)
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if row["phase"] == "PLANNING":
        started_fingerprint = row["planning_attempt_fingerprint"]
        if not started_fingerprint:
            started_fingerprint = _planning_input_fingerprint(repo_path, row["active_sha"])
        if started_fingerprint != current_fingerprint:
            if row["planning_validation_error"]:
                preserve_planning_diagnostics(state_dir, repo, row)
            stale_worktree = Path(row["worktree"])
            if stale_worktree.exists():
                git(repo_path, "worktree", "remove", "--force", str(stale_worktree))
            clear_active(connection, repo)
            row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    if not row["phase"] and row["planning_fingerprint"] == current_fingerprint:
        update_project(connection, repo, processed_sha=base_sha)
        print("계획 입력이 바뀌지 않아 Codex를 실행하지 않습니다.")
        return 0

    if not row["phase"] and row["planning_fingerprint"] and row["processed_sha"]:
        processed_fingerprint = _planning_input_fingerprint(repo_path, row["processed_sha"])
        if processed_fingerprint == current_fingerprint:
            update_project(connection, repo, processed_sha=base_sha)
            print("계획 입력이 바뀌지 않아 Codex를 실행하지 않습니다.")
            return 0
        if _is_requirement_archive_only_change(repo_path, row["processed_sha"], base_sha):
            update_project(connection, repo, processed_sha=base_sha)
            print("Requirement archive 이동 외의 계획 입력이 바뀌지 않아 Codex를 실행하지 않습니다.")
            return 0

    if not row["phase"]:
        project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
        worktree = state_dir / "worktrees" / project_id / "planning" / current_fingerprint
        update_project(
            connection,
            repo,
            active_sha=base_sha,
            plan_sha=None,
            planning_attempt_fingerprint=current_fingerprint,
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
        make_planning_worktree(repo_path, planning_worktree, base)
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
            result = run_codex(
                planning_worktree,
                state_dir,
                config_path,
                "planner",
                planner_prompt(row["planning_validation_error"] or row["last_error"]),
                read_only=False,
                persist_session=True,
                session_id=row["planner_session_id"],
                on_session_started=lambda started_session_id: update_project(
                    connection,
                    repo,
                    planner_session_id=started_session_id,
                ),
            )
            code, message = result
            result_session_id = getattr(result, "session_id", None)
            if result_session_id:
                update_project(
                    connection,
                    repo,
                    planner_session_id=result_session_id,
                )
            if code:
                update_project(
                    connection,
                    repo,
                    last_error=message or f"Planner 종료 코드 {code}",
                    planning_result_pending_validation=0,
                    planning_result_completed=0,
                )
                print(message or "Planner 실행에 실패했습니다.", file=sys.stderr)
                return 1
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

        try:
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
            print(str(error), file=sys.stderr)
            return 1

        if validated_result is None:
            completed_fingerprint = _planning_input_fingerprint(repo_path, row["active_sha"])
            git(repo_path, "worktree", "remove", str(planning_worktree))
            clear_active(
                connection,
                repo,
                processed_sha=row["active_sha"],
                planning_fingerprint=completed_fingerprint,
            )
            print("추가로 계획할 작업이 없습니다.")
            return 0

        plan_path, plan = validated_result
        if dirty_files:
            # Publishing failures are operational failures, not Planner
            # validation failures. Leave the validated result for retry.
            git(planning_worktree, "add", "--", plan_path)
            git(planning_worktree, "commit", "-m", f"Add ExecPlan {plan['id']}: {plan['title']}")

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
                git(repo_path, "worktree", "remove", str(planning_worktree))
                clear_active(connection, repo)
                print("계획 중 base가 변경되어 최신 기준으로 다시 계획합니다.", file=sys.stderr)
                return 1
        if latest_sha != plan_commit:
            if latest_sha == row["active_sha"]:
                pushed = git(
                    planning_worktree,
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{base}",
                    check=False,
                )
                if pushed.returncode:
                    update_project(connection, repo, last_error=f"ExecPlan push 실패:\n{pushed.stdout[-8000:]}")
                    print(pushed.stdout, file=sys.stderr)
                    return 1
                git(repo_path, "fetch", "origin")
                latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
                if latest_sha != plan_commit:
                    update_project(connection, repo, last_error="ExecPlan push 결과를 origin/base에서 확인할 수 없습니다.")
                    print("ExecPlan push 결과 확인에 실패했습니다.", file=sys.stderr)
                    return 1

        git(repo_path, "worktree", "remove", str(planning_worktree))
        branch = f"loop-manager/{plan['id']}"
        worktree = state_dir / "worktrees" / project_id / str(plan["id"])
        update_project(
            connection,
            repo,
            active_sha=latest_sha,
            plan_sha=plan_commit,
            phase="WORKING",
            branch=branch,
            worktree=str(worktree),
            plan_path=plan_path,
            last_error=None,
        )
        base_sha = latest_sha
        row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()

    branch = row["branch"]
    worktree = Path(row["worktree"])
    make_worktree(repo_path, worktree, branch, base)

    if row["phase"] == "WORKING":
        plan = validate_execplan(worktree, row["plan_path"])
        level = worker_level(plan)
        result = run_codex(
            worktree,
            state_dir,
            config_path,
            "worker",
            worker_prompt(row["plan_path"], row["last_error"]),
            read_only=False,
            level=level,
            persist_session=True,
            session_id=row["worker_session_id"],
            on_session_started=lambda started_session_id: update_project(
                connection,
                repo,
                worker_session_id=started_session_id,
            ),
        )
        code, message = result
        result_session_id = getattr(result, "session_id", None)
        if result_session_id:
            update_project(connection, repo, worker_session_id=result_session_id)

        if code:
            update_project(connection, repo, phase="WORKING", last_error=message or f"Codex 종료 코드 {code}")
            print(message or "Codex 실행에 실패했습니다.", file=sys.stderr)
            return 1
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
            print(validation_error, file=sys.stderr)
            return 1
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
        plan = validate_execplan(worktree, row["plan_path"])
        try:
            worker_publication = parse_worker_result(str(row["worker_result"] or ""), plan)
        except WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            update_project(connection, repo, phase="WORKING", last_error=validation_error)
            print(validation_error, file=sys.stderr)
            return 1

        passed, output = verify(worktree)
        if not passed:
            update_project(
                connection,
                repo,
                phase="WORKING",
                verify_result=None,
                last_error=f"전체 검증 실패:\n{output}",
            )
            print(output, file=sys.stderr)
            return 1

        dirty = bool(git(worktree, "status", "--porcelain").stdout.strip())
        ahead = int(git(worktree, "rev-list", "--count", f"origin/{base}..HEAD").stdout)
        if not dirty and not ahead:
            git(repo_path, "worktree", "remove", str(worktree))
            clear_active(connection, repo)
            print("추가로 구현할 작업이 없습니다.")
            return 0
        if dirty:
            git(worktree, "add", "-A")
            plan = validate_execplan(worktree, row["plan_path"])
            git(worktree, "commit", "-m", worker_publication.commit_title)

        git(repo_path, "fetch", "origin")
        latest_sha = git(repo_path, "rev-parse", f"origin/{base}").stdout.strip()
        if latest_sha != row["active_sha"]:
            rebased = git(worktree, "rebase", f"origin/{base}", check=False)
            if rebased.returncode:
                update_project(connection, repo, phase="WORKING", last_error=f"rebase 실패:\n{rebased.stdout[-8000:]}")
                print(rebased.stdout, file=sys.stderr)
                return 1
            passed, output = verify(worktree)
            if not passed:
                update_project(
                    connection,
                    repo,
                    phase="WORKING",
                    verify_result=None,
                    last_error=f"rebase 후 전체 검증 실패:\n{output}",
                )
                print(output, file=sys.stderr)
                return 1
            update_project(connection, repo, active_sha=latest_sha, verify_result=output)
        else:
            update_project(connection, repo, verify_result=output)
        update_project(connection, repo, phase="REVIEWING", last_error=None)

    row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if row["phase"] == "REVIEWING":
        code, review = run_codex(
            worktree,
            state_dir,
            config_path,
            "reviewer",
            reviewer_prompt(base, row["worker_result"], row["verify_result"]),
            read_only=True,
        )
        passed = code == 0 and re.search(r"(?mi)^VERDICT:\s*PASS\s*$", review) and not re.search(
            r"(?mi)^VERDICT:\s*FAIL\s*$", review
        )
        if not passed:
            update_project(connection, repo, phase="WORKING", last_error=f"Review 실패:\n{review[-8000:]}")
            print(review or "Reviewer 실행에 실패했습니다.", file=sys.stderr)
            return 1
        update_project(connection, repo, phase="PUBLISHING", last_error=None)

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
                print(validation_error, file=sys.stderr)
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
        print(f"PR 생성 완료: {url}")
    return 0


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
    return parser


def main() -> int:
    args, _ = build_parser().parse_known_args()
    state_dir = state_directory(args.state_dir)
    try:
        with process_lock(state_dir):
            if args.command == "init":
                with connect_database(state_dir, create=True):
                    pass
                print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
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
    except (RuntimeError, OSError, sqlite3.DatabaseError, json.JSONDecodeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
