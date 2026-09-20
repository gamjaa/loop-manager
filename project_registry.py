"""SQLite storage boundary for explicitly registered projects."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3


BLOCKING_STATUSES = (
    "READY",
    "IMPLEMENTING",
    "VERIFYING",
    "REVIEWING",
    "PUBLISHING",
    "PR_OPEN",
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


def ensure_registry_schema(connection: sqlite3.Connection) -> None:
    """Apply additive registry migrations while preserving legacy rows."""

    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    migrations = {
        "registered": "INTEGER NOT NULL DEFAULT 1",
        "agents_hash": "TEXT",
        "agents_approved": "INTEGER NOT NULL DEFAULT 0",
    }
    changed = False
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE projects ADD COLUMN {column} {definition}"
            )
            changed = True
    if changed:
        connection.commit()


def list_projects(connection: sqlite3.Connection) -> tuple[RegisteredProject, ...]:
    ensure_registry_schema(connection)
    rows = connection.execute(
        """
        SELECT repo, base_branch, provider, agents_hash, agents_approved
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
        )
        for row in rows
    )


def add_project(connection: sqlite3.Connection, project: RegisteredProject) -> None:
    ensure_registry_schema(connection)
    existing = connection.execute(
        "SELECT registered FROM projects WHERE repo = ?",
        (project.repo,),
    ).fetchone()
    if existing is not None and int(existing["registered"]) == 1:
        raise ProjectRegistryError(f"이미 등록된 프로젝트입니다: {project.repo}")

    with connection:
        if existing is None:
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, provider, registered,
                    agents_hash, agents_approved
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    project.repo,
                    project.base_branch,
                    project.provider,
                    project.agents_hash,
                    int(project.agents_approved),
                ),
            )
        else:
            connection.execute(
                """
                UPDATE projects
                SET base_branch = ?, provider = ?, registered = 1,
                    agents_hash = ?, agents_approved = ?
                WHERE repo = ?
                """,
                (
                    project.base_branch,
                    project.provider,
                    project.agents_hash,
                    int(project.agents_approved),
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
    if legacy_phase in BLOCKING_STATUSES or legacy_phase == "WORKING":
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
