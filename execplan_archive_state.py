"""Durable state for post-merge ExecPlan archive housekeeping."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
import sqlite3
from typing import Iterator, Sequence


ARCHIVE_ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_archive_items (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    active_path TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    expected_hash TEXT NOT NULL,
    base_sha TEXT,
    final_hash TEXT,
    operation_id TEXT,
    commit_oid TEXT,
    push_result TEXT,
    pending_reason TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id)
)
"""

ARCHIVE_OPERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_archive_operations (
    repo TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    final_hash TEXT,
    commit_oid TEXT,
    push_result TEXT,
    pending_reason TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, operation_id)
)
"""

_ACTIVE_PLAN_PATH = re.compile(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md")


@dataclass(frozen=True)
class MergedArchiveCandidate:
    plan_id: str
    active_path: str
    archive_path: str
    content_hash: str


@dataclass(frozen=True)
class PreparedArchiveOperation:
    operation_id: str
    base_sha: str
    final_hash: str
    commit_oid: str


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(ARCHIVE_ITEMS_SCHEMA)
    connection.execute(ARCHIVE_OPERATIONS_SCHEMA)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if not connection.in_transaction:
        with connection:
            yield
        return
    savepoint = "execplan_archive_state"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {savepoint}")
        connection.execute(f"RELEASE {savepoint}")
        raise
    else:
        connection.execute(f"RELEASE {savepoint}")


class ExecPlanArchiveRepository:
    """SQLite boundary that never changes ExecPlan execution status or path."""

    def __init__(self, connection: sqlite3.Connection, *, ensure: bool = True):
        self.connection = connection
        if ensure:
            ensure_schema(connection)

    def merged_candidates(self, repo: str) -> tuple[MergedArchiveCandidate, ...]:
        candidates: list[MergedArchiveCandidate] = []
        rows = self.connection.execute(
            "SELECT plan_id, path, content_hash FROM exec_plans "
            "WHERE repo = ? AND status = 'MERGED' ORDER BY plan_id",
            (repo,),
        ).fetchall()
        for row in rows:
            plan_id = str(row["plan_id"])
            active_path = str(row["path"])
            match = _ACTIVE_PLAN_PATH.fullmatch(active_path)
            if match is None or match.group(1) != plan_id:
                continue
            filename = PurePosixPath(active_path).name
            candidates.append(
                MergedArchiveCandidate(
                    plan_id=plan_id,
                    active_path=active_path,
                    archive_path=f"docs/plans/archive/{filename}",
                    content_hash=str(row["content_hash"]),
                )
            )
        return tuple(candidates)

    def prepared_operation(
        self, repo: str, operation_id: str
    ) -> PreparedArchiveOperation | None:
        row = self.connection.execute(
            "SELECT operation_id, base_sha, final_hash, commit_oid "
            "FROM execplan_archive_operations "
            "WHERE repo = ? AND operation_id = ? AND commit_oid IS NOT NULL "
            "AND final_hash IS NOT NULL",
            (repo, operation_id),
        ).fetchone()
        if row is None:
            return None
        return PreparedArchiveOperation(
            operation_id=str(row["operation_id"]),
            base_sha=str(row["base_sha"]),
            final_hash=str(row["final_hash"]),
            commit_oid=str(row["commit_oid"]),
        )

    def record_pending(
        self,
        repo: str,
        candidate: MergedArchiveCandidate,
        *,
        reason: str,
        base_sha: str | None = None,
        operation_id: str | None = None,
        commit_oid: str | None = None,
        final_hash: str | None = None,
        push_result: str | None = None,
    ) -> None:
        with _transaction(self.connection):
            self._upsert_item(
                repo,
                candidate,
                base_sha=base_sha,
                final_hash=final_hash,
                operation_id=operation_id,
                commit_oid=commit_oid,
                push_result=push_result,
                pending_reason=reason,
                archived=False,
            )

    def record_operation_started(
        self,
        repo: str,
        operation_id: str,
        base_sha: str,
        candidates: Sequence[MergedArchiveCandidate],
    ) -> None:
        with _transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO execplan_archive_operations (
                    repo, operation_id, base_sha, push_result, pending_reason
                ) VALUES (?, ?, ?, 'preparing', NULL)
                ON CONFLICT(repo, operation_id) DO UPDATE SET
                    base_sha = excluded.base_sha,
                    push_result = CASE
                        WHEN execplan_archive_operations.commit_oid IS NULL
                        THEN 'preparing'
                        ELSE execplan_archive_operations.push_result
                    END,
                    pending_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, operation_id, base_sha),
            )
            for candidate in candidates:
                self._upsert_item(
                    repo,
                    candidate,
                    base_sha=base_sha,
                    operation_id=operation_id,
                    push_result="preparing",
                    pending_reason=None,
                    archived=False,
                )

    def record_prepared(
        self,
        repo: str,
        operation_id: str,
        base_sha: str,
        final_hash: str,
        commit_oid: str,
        candidates: Sequence[MergedArchiveCandidate],
    ) -> None:
        with _transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO execplan_archive_operations (
                    repo, operation_id, base_sha, final_hash, commit_oid,
                    push_result, pending_reason
                ) VALUES (?, ?, ?, ?, ?, 'prepared', NULL)
                ON CONFLICT(repo, operation_id) DO UPDATE SET
                    base_sha = excluded.base_sha,
                    final_hash = excluded.final_hash,
                    commit_oid = excluded.commit_oid,
                    push_result = 'prepared',
                    pending_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, operation_id, base_sha, final_hash, commit_oid),
            )
            for candidate in candidates:
                self._upsert_item(
                    repo,
                    candidate,
                    base_sha=base_sha,
                    final_hash=candidate.content_hash,
                    operation_id=operation_id,
                    commit_oid=commit_oid,
                    push_result="prepared",
                    pending_reason=None,
                    archived=False,
                )

    def record_operation_pending(
        self,
        repo: str,
        operation_id: str,
        candidates: Sequence[MergedArchiveCandidate],
        *,
        reason: str,
        push_result: str,
        base_sha: str,
        commit_oid: str | None = None,
        final_hash: str | None = None,
    ) -> None:
        with _transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO execplan_archive_operations (
                    repo, operation_id, base_sha, final_hash, commit_oid,
                    push_result, pending_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, operation_id) DO UPDATE SET
                    final_hash = COALESCE(
                        excluded.final_hash,
                        execplan_archive_operations.final_hash
                    ),
                    commit_oid = COALESCE(
                        excluded.commit_oid,
                        execplan_archive_operations.commit_oid
                    ),
                    push_result = excluded.push_result,
                    pending_reason = excluded.pending_reason,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    repo,
                    operation_id,
                    base_sha,
                    final_hash,
                    commit_oid,
                    push_result,
                    reason,
                ),
            )
            for candidate in candidates:
                self._upsert_item(
                    repo,
                    candidate,
                    base_sha=base_sha,
                    final_hash=candidate.content_hash,
                    operation_id=operation_id,
                    commit_oid=commit_oid,
                    push_result=push_result,
                    pending_reason=reason,
                    archived=False,
                )

    def record_success(
        self,
        repo: str,
        candidates: Sequence[MergedArchiveCandidate],
        *,
        base_sha: str,
        operation_id: str | None,
        commit_oid: str | None,
        push_result: str,
    ) -> None:
        with _transaction(self.connection):
            if operation_id is not None:
                self.connection.execute(
                    """
                    UPDATE execplan_archive_operations
                    SET push_result = ?, pending_reason = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE repo = ? AND operation_id = ?
                    """,
                    (push_result, repo, operation_id),
                )
            for candidate in candidates:
                self._upsert_item(
                    repo,
                    candidate,
                    base_sha=base_sha,
                    final_hash=candidate.content_hash,
                    operation_id=operation_id,
                    commit_oid=commit_oid,
                    push_result=push_result,
                    pending_reason=None,
                    archived=True,
                )

    def item(self, repo: str, plan_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM execplan_archive_items WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()

    def _upsert_item(
        self,
        repo: str,
        candidate: MergedArchiveCandidate,
        *,
        base_sha: str | None,
        final_hash: str | None = None,
        operation_id: str | None = None,
        commit_oid: str | None = None,
        push_result: str | None = None,
        pending_reason: str | None,
        archived: bool,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO execplan_archive_items (
                repo, plan_id, active_path, archive_path, expected_hash,
                base_sha, final_hash, operation_id, commit_oid, push_result,
                pending_reason, archived
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, plan_id) DO UPDATE SET
                active_path = excluded.active_path,
                archive_path = excluded.archive_path,
                expected_hash = excluded.expected_hash,
                base_sha = COALESCE(
                    excluded.base_sha, execplan_archive_items.base_sha
                ),
                final_hash = COALESCE(
                    excluded.final_hash, execplan_archive_items.final_hash
                ),
                operation_id = COALESCE(
                    excluded.operation_id, execplan_archive_items.operation_id
                ),
                commit_oid = COALESCE(
                    excluded.commit_oid, execplan_archive_items.commit_oid
                ),
                push_result = excluded.push_result,
                pending_reason = excluded.pending_reason,
                archived = excluded.archived,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                repo,
                candidate.plan_id,
                candidate.active_path,
                candidate.archive_path,
                candidate.content_hash,
                base_sha,
                final_hash,
                operation_id,
                commit_oid,
                push_result,
                pending_reason,
                int(archived),
            ),
        )
