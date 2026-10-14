"""SQLite storage boundary for explicitly registered projects."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import sqlite3
from pathlib import Path

from loop_manager_internal.storage.runtime_state import _transaction


BLOCKING_STATUSES = (
    "READY",
    "IMPLEMENTING",
    "VERIFYING",
    "REVIEWING",
    "PUBLISHING",
    "PR_OPEN",
    "PLAN_BLOCKED",
    "BLOCKED",
)


class ProjectRegistryError(RuntimeError):
    """Raised when a requested registry transition is not allowed."""


@dataclass(frozen=True)
class RegisteredProject:
    repo: str
    base_branch: str
    provider: str
    agents_hash: str | None
    agents_approved: bool
    origin: str | None = None


def ensure_registry_schema(connection: sqlite3.Connection) -> None:
    """Apply additive registry migrations while preserving legacy rows."""

    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    migrations = {
        "registered": "INTEGER NOT NULL DEFAULT 1",
        "agents_hash": "TEXT",
        "agents_approved": "INTEGER NOT NULL DEFAULT 0",
        "origin": "TEXT",
    }
    missing = [(column, definition) for column, definition in migrations.items()
               if column not in columns]
    if missing:
        with _transaction(connection):
            # Another process may have migrated the table before we acquired
            # the write lock. Recheck before applying additive DDL.
            current = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
            for column, definition in missing:
                if column not in current:
                    connection.execute(
                        f"ALTER TABLE projects ADD COLUMN {column} {definition}"
                    )
    with _transaction(connection):
        connection.execute("""
            CREATE TABLE IF NOT EXISTS project_agents_approval_audit (
                id INTEGER PRIMARY KEY,
                repo TEXT NOT NULL,
                agents_hash TEXT,
                agents_approved INTEGER NOT NULL,
                origin TEXT,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (repo, agents_hash, agents_approved, origin)
            )
        """)
        # Capture legacy approvals before registration can replace their hash.
        # IS also deduplicates old rows whose hash/origin was never recorded.
        connection.execute("""
            INSERT INTO project_agents_approval_audit
                (repo, agents_hash, agents_approved, origin)
            SELECT p.repo, p.agents_hash, p.agents_approved, p.origin
            FROM projects AS p
            WHERE p.agents_approved = 1 AND NOT EXISTS (
                SELECT 1 FROM project_agents_approval_audit AS a
                WHERE a.repo = p.repo AND a.agents_hash IS p.agents_hash
                    AND a.agents_approved = p.agents_approved AND a.origin IS p.origin
            )
        """)


def list_projects(connection: sqlite3.Connection) -> tuple[RegisteredProject, ...]:
    ensure_registry_schema(connection)
    rows = connection.execute(
        """
        SELECT repo, base_branch, provider, agents_hash, agents_approved, origin
        FROM projects
        WHERE registered = 1
        ORDER BY repo
        """
    ).fetchall()
    return tuple(
        RegisteredProject(
            repo=str(row["repo"]),
            base_branch=str(row["base_branch"]),
            provider=str(row["provider"]),
            agents_hash=(
                str(row["agents_hash"]) if row["agents_hash"] is not None else None
            ),
            agents_approved=bool(row["agents_approved"]),
            origin=(str(row["origin"]) if row["origin"] is not None else None),
        )
        for row in rows
    )


def select_projects(
    connection: sqlite3.Connection,
    repositories: tuple[str, ...] | list[str] | None = None,
) -> tuple[RegisteredProject, ...]:
    """Resolve and validate a requested run scope from the registered rows."""

    projects = list_projects(connection)
    if not repositories:
        return projects

    requested = tuple(dict.fromkeys(
        str(Path(repository).expanduser().resolve()) for repository in repositories
    ))
    registered = {
        str(Path(project.repo).expanduser().resolve())
        for project in projects
    }
    missing = tuple(repository for repository in requested if repository not in registered)
    if missing:
        raise ProjectRegistryError(
            "등록되지 않은 프로젝트입니다: " + ", ".join(missing)
        )

    selected = set(requested)
    return tuple(
        project for project in projects
        if str(Path(project.repo).expanduser().resolve()) in selected
    )


def add_project(
    connection: sqlite3.Connection,
    project: RegisteredProject,
    *,
    manage_transaction: bool = True,
) -> None:
    ensure_registry_schema(connection)
    existing = connection.execute(
        "SELECT registered FROM projects WHERE repo = ?",
        (project.repo,),
    ).fetchone()
    if existing is not None and int(existing["registered"]) == 1:
        raise ProjectRegistryError(f"이미 등록된 프로젝트입니다: {project.repo}")

    with connection if manage_transaction else nullcontext():
        if existing is None:
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, provider, registered,
                    agents_hash, agents_approved, origin
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    project.repo,
                    project.base_branch,
                    project.provider,
                    project.agents_hash,
                    int(project.agents_approved),
                    project.origin,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE projects
                SET base_branch = ?, provider = ?, registered = 1,
                    agents_hash = ?, agents_approved = ?, origin = ?
                WHERE repo = ?
                """,
                (
                    project.base_branch,
                    project.provider,
                    project.agents_hash,
                    int(project.agents_approved),
                    project.origin,
                    project.repo,
                ),
            )


def remove_project(connection: sqlite3.Connection, repo: str) -> None:
    """Unregister a project without deleting its execution audit history."""

    ensure_registry_schema(connection)
    project = connection.execute(
        "SELECT registered, phase, planning_error FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None or int(project["registered"]) != 1:
        raise ProjectRegistryError(f"등록되지 않은 프로젝트입니다: {repo}")

    placeholders = ", ".join("?" for _ in BLOCKING_STATUSES)
    blocking = connection.execute(
        f"""
        SELECT plan_id, status
        FROM exec_plans
        WHERE repo = ? AND status IN ({placeholders})
        ORDER BY plan_id
        LIMIT 1
        """,
        (repo, *BLOCKING_STATUSES),
    ).fetchone()
    legacy_phase = str(project["phase"] or "")
    if blocking is not None:
        raise ProjectRegistryError(
            "진행 중인 ExecPlan이 있어 프로젝트를 제거할 수 없습니다: "
            f"{blocking['plan_id']} ({blocking['status']})"
        )
    has_project_blocks = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_blocks'"
    ).fetchone()
    project_block = (
        connection.execute(
            "SELECT reason_code FROM project_blocks WHERE repo = ?", (repo,)
        ).fetchone()
        if has_project_blocks
        else None
    )
    if project_block is not None:
        raise ProjectRegistryError(
            "PROJECT_BLOCKED 상태가 있어 프로젝트를 제거할 수 없습니다: "
            f"{project_block['reason_code']}"
        )
    if legacy_phase in BLOCKING_STATUSES or legacy_phase in {
        "WORKING",
        "PROJECT_BLOCKED",
    }:
        raise ProjectRegistryError(
            "진행 중인 기존 프로젝트 상태가 있어 제거할 수 없습니다: "
            f"{legacy_phase}"
        )
    if str(project["planning_error"] or "").strip():
        raise ProjectRegistryError(
            "해결되지 않은 planning_error가 있어 프로젝트를 제거할 수 없습니다."
        )

    with connection:
        connection.execute(
            "UPDATE projects SET registered = 0 WHERE repo = ?",
            (repo,),
        )
