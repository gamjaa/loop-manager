"""Resolve runtime instructions once, independently of candidate project files."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

from loop_manager_internal.agents.agent_invocation import InvocationContext
from loop_manager_internal.storage.common_guidance_state import GuidanceRepository

COMMON_PATH = "docs/AGENTS.common.md"
RUNTIME_ROOT = Path(__file__).resolve().parents[2]


class GuidanceError(RuntimeError):
    """A common version cannot safely be delivered; never a session fallback."""


@dataclass(frozen=True)
class CommonGuidance:
    task_id: str
    content: str
    content_hash: str
    source_revision: str
    snapshot_path: Path


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


def runtime_content_hash() -> str:
    return hashlib.sha256(_read(RUNTIME_ROOT / COMMON_PATH)).hexdigest()


def validate_state_root(context: InvocationContext, worktree: Path, state_dir: Path) -> None:
    state_root = state_dir.resolve()
    if any(state_root.is_relative_to(root.resolve())
           for root in (worktree, RUNTIME_ROOT, Path(context.repo))):
        raise GuidanceError("공통 지침 state 경로가 프로젝트 또는 runtime 안에 있습니다.")


def _check_snapshot_directory(directory: Path) -> None:
    try:
        status = directory.lstat()
        if (not stat.S_ISDIR(status.st_mode) or directory.resolve() != directory
                or (os.name != 'nt' and (status.st_uid != os.getuid() or status.st_mode & 0o022))):
            raise GuidanceError('공통 지침 snapshot 디렉터리의 소유권·쓰기 경계가 다릅니다.')
    except OSError as error:
        raise GuidanceError('공통 지침 snapshot 디렉터리를 확인할 수 없습니다.') from error


def task_identity(context: InvocationContext, role: str, worktree: Path) -> str:
    # A plan keeps its version across revised definitions, profiles and roles.
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


def load_saved(task_id: str, row, state_dir: Path) -> CommonGuidance:
    content_hash, revision, reference = row
    if not re.fullmatch(r"[0-9a-f]{64}", str(content_hash)) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", str(revision),
    ):
        raise GuidanceError("저장된 공통 지침 hash/revision이 유효하지 않습니다.")
    expected = state_dir.resolve() / "agent-guidance" / f"{content_hash}.md"
    path = Path(reference)
    if path != expected or path.resolve() != expected:
        raise GuidanceError("저장된 공통 지침 snapshot 경로가 Manager 경계와 다릅니다.")
    _check_snapshot_directory(expected.parent)
    contents = _read(path)
    if hashlib.sha256(contents).hexdigest() != content_hash:
        raise GuidanceError("저장된 공통 지침 snapshot의 내용 hash가 다릅니다.")
    return CommonGuidance(task_id, contents.decode("utf-8"), content_hash, revision, path)


def resolve(repository: GuidanceRepository, context: InvocationContext,
            role: str, worktree: Path, state_dir: Path) -> CommonGuidance:
    identity = task_identity(context, role, worktree)
    try:
        validate_state_root(context, worktree, state_dir)
        saved = repository.get(identity)
        if saved is not None:
            return load_saved(identity, saved, state_dir)
        contents = _read(RUNTIME_ROOT / COMMON_PATH)
        result = subprocess.run(
            ["git", "-C", str(RUNTIME_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
        )
        revision = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise GuidanceError("공통 지침 runtime revision을 확인할 수 없습니다.")
        content_hash = hashlib.sha256(contents).hexdigest()
        directory = state_dir.resolve() / "agent-guidance"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check_snapshot_directory(directory)
        snapshot = directory / f"{content_hash}.md"
        # Exclusive creation preserves a previous snapshot after interruption.
        try:
            with snapshot.open("xb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        if _read(snapshot) != contents:
            raise GuidanceError("공통 지침 snapshot이 기존 내용과 다릅니다.")
        repository.save(identity, context.repo, content_hash, revision, str(snapshot))
        return load_saved(identity, repository.get(identity), state_dir)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        raise GuidanceError("공통 지침 버전 보존 실패") from error


def plan_content_hash(connection: sqlite3.Connection, repo: str, plan_id: str) -> str:
    """Use the ongoing plan's saved version, including at result reuse gates."""
    identity = task_identity(InvocationContext(repo, plan_id), "worker", Path(repo))
    row = GuidanceRepository(connection).get(identity)
    return str(row[0]) if row else runtime_content_hash()


def planning_content_hash(connection: sqlite3.Connection, repo: str) -> str | None:
    """An owned ongoing planning workspace retains its input version."""
    columns = {row[1] for row in connection.execute('PRAGMA table_info(projects)')}
    if not {'phase', 'worktree'} <= columns:
        return None
    row = connection.execute('SELECT phase, worktree FROM projects WHERE repo = ?', (repo,)).fetchone()
    if row is None or row[0] != 'PLANNING' or not row[1]:
        return None
    repository = GuidanceRepository(connection)
    if 'planner_session_id' in columns:
        session = connection.execute('SELECT planner_session_id FROM projects WHERE repo = ?', (repo,)).fetchone()[0]
        if session:
            bound = connection.execute(
                'SELECT DISTINCT v.content_hash FROM common_guidance_sessions s '
                'JOIN common_guidance_versions v USING (task_id) WHERE s.repo = ? AND s.session_id = ?',
                (repo, session),
            ).fetchall()
            if len(bound) == 1:
                return str(bound[0][0])
    context = independent_context(connection, InvocationContext(repo, None), Path(row[1]))
    identity = task_identity(context, 'planner', Path(row[1]))
    saved = repository.get(identity)
    return str(saved[0]) if saved else None


def effective_planning_hash(connection: sqlite3.Connection, repo: str) -> str:
    return planning_content_hash(connection, repo) or runtime_content_hash()
