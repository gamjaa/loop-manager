"""Compose current runtime instructions without retaining task snapshots."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from loop_manager_internal.agents.agent_invocation import InvocationContext
from loop_manager_internal.storage.common_guidance_state import GuidanceRepository

COMMON_PATH = "docs/AGENTS.common.md"
ROLE_PATHS = {
    "planner": "docs/AGENTS.planner.md",
    "maintenance_planner": "docs/AGENTS.planner.md",
    "planner_check": "docs/AGENTS.planner_check.md",
    "worker": "docs/AGENTS.worker.md",
    "reviewer": "docs/AGENTS.reviewer.md",
}
PONYTAIL_PATH = 'docs/AGENTS.ponytail.md'
LEGACY_GUIDANCE_PATHS = (COMMON_PATH, *dict.fromkeys(ROLE_PATHS.values()))
GUIDANCE_PATHS = (*LEGACY_GUIDANCE_PATHS, PONYTAIL_PATH)
RUNTIME_ROOT = Path(__file__).resolve().parents[2]


class GuidanceError(RuntimeError):
    """Current runtime instructions cannot safely be delivered; never a session fallback."""


@dataclass(frozen=True)
class CommonGuidance:
    task_id: str
    content: str
    content_hash: str
    instruction_path: Path


def _read(path: Path) -> bytes:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise GuidanceError(f"공통 지침이 regular file이 아닙니다: {path}")
        contents = path.read_bytes()
        if not contents.decode("utf-8").strip() or b"\0" in contents:
            raise GuidanceError(f"공통 지침이 비었거나 NUL을 포함합니다: {path}")
        return contents
    except (OSError, UnicodeError) as error:
        raise GuidanceError(f"공통 지침 읽기/UTF-8 확인 실패: {path}") from error


def _runtime_bundle() -> bytes:
    files = {path: _read(RUNTIME_ROOT / path).decode("utf-8") for path in GUIDANCE_PATHS}
    return (json.dumps({"version": 2, "files": files}, ensure_ascii=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def runtime_content_hash() -> str:
    """Fingerprint all instructions, including roles not yet called by a task."""
    return hashlib.sha256(_runtime_bundle()).hexdigest()


def validate_state_root(context: InvocationContext, worktree: Path, state_dir: Path) -> None:
    state_root = state_dir.resolve()
    if any(state_root.is_relative_to(root.resolve())
           for root in (worktree, RUNTIME_ROOT, Path(context.repo))):
        raise GuidanceError("공통 지침 state 경로가 프로젝트 또는 runtime 안에 있습니다.")


def _check_instruction_directory(directory: Path) -> None:
    try:
        status = directory.lstat()
        if (not stat.S_ISDIR(status.st_mode) or directory.resolve() != directory
                or (os.name != 'nt' and (status.st_uid != os.getuid() or status.st_mode & 0o022))):
            raise GuidanceError('공통 지침 실행용 디렉터리의 소유권·쓰기 경계가 다릅니다.')
    except OSError as error:
        raise GuidanceError('공통 지침 실행용 디렉터리를 확인할 수 없습니다.') from error


def task_identity(context: InvocationContext, role: str, worktree: Path) -> str:
    # Keep session ownership stable across runtime and instruction changes.
    # Independent work uses the existing persisted workspace/input identity.
    value = [context.repo, "plan", context.plan_id] if context.plan_id else [
        context.repo, "independent", context.task_id or str(worktree.resolve()),
    ]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def independent_context(connection: sqlite3.Connection, context: InvocationContext,
                        worktree: Path) -> InvocationContext:
    if context.plan_id or context.task_id:
        return context
    columns = {row[1] for row in connection.execute('PRAGMA table_info(projects)')}
    if not {'phase', 'worktree', 'planning_attempt_fingerprint'} <= columns:
        return context
    row = connection.execute(
        'SELECT phase, worktree, planning_attempt_fingerprint FROM projects WHERE repo = ?',
        (context.repo,),
    ).fetchone()
    if row is None or row[0] != 'PLANNING' or not row[1] or Path(row[1]).resolve() != worktree.resolve():
        return context
    cycle_columns = {row[1] for row in connection.execute('PRAGMA table_info(maintenance_cycles)')}
    if {'cycle_id', 'phase'} <= cycle_columns:
        cycle = connection.execute('SELECT cycle_id, phase FROM maintenance_cycles WHERE repo = ?',
                                   (context.repo,)).fetchone()
        if cycle and cycle[0] and cycle[1] in {'assessment', 'post_refactor_revalidation'}:
            return replace(context, task_id='maintenance:' + str(cycle[0]))
    return replace(context, task_id='planning:' + str(row[2])) if row[2] else context


def _store_instructions(directory: Path, contents: bytes, suffix: str) -> Path:
    snapshot = directory / (hashlib.sha256(contents).hexdigest() + suffix)
    # Publish complete bytes atomically; concurrent tasks may share this hash.
    if not snapshot.exists():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".guidance-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, snapshot)
            except FileExistsError:
                pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if _read(snapshot) != contents:
        raise GuidanceError("공통 지침 실행용 파일이 기존 내용과 다릅니다.")
    return snapshot


def resolve(repository: GuidanceRepository, context: InvocationContext,
            role: str, worktree: Path, state_dir: Path) -> CommonGuidance:
    validate_state_root(context, worktree, state_dir)
    if role not in ROLE_PATHS:
        raise GuidanceError(f"지원하지 않는 Agent 지침 역할입니다: {role}")
    files = json.loads(_runtime_bundle())["files"]
    contents = (files[COMMON_PATH].rstrip("\n") + "\n\n" + files[ROLE_PATHS[role]]).encode("utf-8")
    if role == 'worker':
        contents = contents.rstrip(b'\n') + b'\n\n' + files[PONYTAIL_PATH].encode('utf-8')
    directory = state_dir.resolve() / "agent-instructions"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_instruction_directory(directory)
        path = _store_instructions(directory, contents, ".md")
    except OSError as error:
        raise GuidanceError("공통 지침 실행용 파일 생성 실패") from error
    return CommonGuidance(task_identity(context, role, worktree), contents.decode("utf-8"),
                          hashlib.sha256(contents).hexdigest(), path)


def plan_content_hash(connection: sqlite3.Connection, repo: str, plan_id: str) -> str:
    """Candidate reuse must compare against the current runtime instructions."""
    return runtime_content_hash()


def worker_content_hash(connection: sqlite3.Connection, repo: str, plan_id: str) -> str:
    """Actual delivery affects candidate reuse, independently of the current runtime source."""
    identity = task_identity(InvocationContext(repo, plan_id), 'worker', Path(repo))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_invocations'").fetchone():
        worker = connection.execute(
            "SELECT guidance_content_hash FROM agent_invocations WHERE repo=? AND role='worker' "
            "AND guidance_task_id=? AND guidance_content_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1",
            (repo, identity),
        ).fetchone()
        if worker:
            return str(worker[0])
    return ''


def planning_content_hash(connection: sqlite3.Connection, repo: str) -> str:
    """Planning, including resumed work, uses the current runtime instructions."""
    return runtime_content_hash()


def effective_planning_hash(connection: sqlite3.Connection, repo: str) -> str:
    return planning_content_hash(connection, repo)
