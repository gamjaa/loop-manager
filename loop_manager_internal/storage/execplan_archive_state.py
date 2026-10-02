"""Durable state for post-merge ExecPlan archive housekeeping."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import PurePosixPath
import re
import sqlite3
from typing import Callable, Iterator, Sequence


ARCHIVE_ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_archive_items (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    active_path TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    expected_hash TEXT NOT NULL,
    current_revision_sha TEXT,
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
    current_revisions TEXT,
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
    revision_sha: str


@dataclass(frozen=True)
class PreparedArchiveOperation:
    operation_id: str
    base_sha: str
    final_hash: str
    commit_oid: str


class ArchiveWriteResult(str, Enum):
    APPLIED = "applied"
    SUPERSEDED = "superseded"


class _ArchiveCatalogSuperseded(RuntimeError):
    pass


def _revision_token(candidates: Sequence[MergedArchiveCandidate]) -> str:
    return json.dumps(
        [
            (item.plan_id, item.revision_sha, item.content_hash)
            for item in sorted(candidates, key=lambda item: item.plan_id)
        ],
        separators=(",", ":"),
    )


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(ARCHIVE_ITEMS_SCHEMA)
    connection.execute(ARCHIVE_OPERATIONS_SCHEMA)
    for table, column, definition in (
        ("execplan_archive_items", "current_revision_sha", "TEXT"),
        ("execplan_archive_operations", "current_revisions", "TEXT"),
    ):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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

    def __init__(self, connection: sqlite3.Connection, *, ensure: bool = True,
                 apply_catalog: Callable[..., bool] | None = None):
        self.connection = connection
        self.apply_catalog = apply_catalog
        if ensure:
            ensure_schema(connection)

    def merged_candidates(self, repo: str) -> tuple[MergedArchiveCandidate, ...]:
        candidates: list[MergedArchiveCandidate] = []
        rows = self.connection.execute(
            "SELECT plan_id, path, content_hash, current_revision_sha FROM exec_plans "
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
                    revision_sha=str(row["current_revision_sha"] or ""),
                )
            )
        # A push may have succeeded before SQLite recorded success. Reconcile
        # then points the current definition at the archived file; retain the
        # previous move identity only for this exact final blob.
        recovered = self.connection.execute(
            "SELECT i.plan_id, i.active_path, i.archive_path, i.expected_hash, "
            "i.current_revision_sha, p.path, p.content_hash "
            "FROM execplan_archive_items i JOIN exec_plans p "
            "ON p.repo = i.repo AND p.plan_id = i.plan_id "
            "WHERE i.repo = ? AND i.archived = 0 AND p.status = 'MERGED'",
            (repo,),
        ).fetchall()
        for row in recovered:
            if any(item.plan_id == row["plan_id"] for item in candidates):
                continue
            if row["path"] == row["archive_path"]:
                candidates.append(
                    MergedArchiveCandidate(
                        str(row["plan_id"]),
                        str(row["active_path"]),
                        str(row["archive_path"]),
                        str(row["expected_hash"]),
                        str(row["current_revision_sha"] or ""),
                    )
                )
        candidates.sort(key=lambda item: item.plan_id)
        return tuple(candidates)

    def candidates_current(
        self, repo: str, candidates: Sequence[MergedArchiveCandidate]
    ) -> bool:
        current = {item.plan_id: item for item in self.merged_candidates(repo)}
        return all(current.get(item.plan_id) == item for item in candidates)

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
    ) -> ArchiveWriteResult:
        with _transaction(self.connection):
            if not self.candidates_current(repo, candidates):
                return ArchiveWriteResult.SUPERSEDED
            self.connection.execute(
                """
                INSERT INTO execplan_archive_operations (
                    repo, operation_id, base_sha, current_revisions,
                    push_result, pending_reason
                ) VALUES (?, ?, ?, ?, 'preparing', NULL)
                ON CONFLICT(repo, operation_id) DO UPDATE SET
                    base_sha = excluded.base_sha,
                    current_revisions = excluded.current_revisions,
                    push_result = CASE
                        WHEN execplan_archive_operations.commit_oid IS NULL
                        THEN 'preparing'
                        ELSE execplan_archive_operations.push_result
                    END,
                    pending_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, operation_id, base_sha, _revision_token(candidates)),
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
        return ArchiveWriteResult.APPLIED

    def record_prepared(
        self,
        repo: str,
        operation_id: str,
        base_sha: str,
        final_hash: str,
        commit_oid: str,
        candidates: Sequence[MergedArchiveCandidate],
    ) -> ArchiveWriteResult:
        with _transaction(self.connection):
            if not self.candidates_current(repo, candidates):
                return ArchiveWriteResult.SUPERSEDED
            self.connection.execute(
                """
                INSERT INTO execplan_archive_operations (
                    repo, operation_id, base_sha, current_revisions,
                    final_hash, commit_oid,
                    push_result, pending_reason
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', NULL)
                ON CONFLICT(repo, operation_id) DO UPDATE SET
                    base_sha = excluded.base_sha,
                    current_revisions = excluded.current_revisions,
                    final_hash = excluded.final_hash,
                    commit_oid = excluded.commit_oid,
                    push_result = 'prepared',
                    pending_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo, operation_id, base_sha, _revision_token(candidates),
                 final_hash, commit_oid),
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
        return ArchiveWriteResult.APPLIED

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
    ) -> ArchiveWriteResult:
        with _transaction(self.connection):
            if not self.candidates_current(repo, candidates):
                return ArchiveWriteResult.SUPERSEDED
            self.connection.execute(
                """
                INSERT INTO execplan_archive_operations (
                    repo, operation_id, base_sha, current_revisions,
                    final_hash, commit_oid,
                    push_result, pending_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                    _revision_token(candidates),
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
        return ArchiveWriteResult.APPLIED

    def record_success(
        self,
        repo: str,
        candidates: Sequence[MergedArchiveCandidate],
        *,
        base_sha: str,
        operation_id: str | None,
        commit_oid: str | None,
        push_result: str,
    ) -> ArchiveWriteResult:
        try:
            with _transaction(self.connection):
                if not self.candidates_current(repo, candidates):
                    return ArchiveWriteResult.SUPERSEDED
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
                if (commit_oid is not None and self.apply_catalog is not None
                        and not self.apply_catalog(base_sha, commit_oid, candidates)):
                    raise _ArchiveCatalogSuperseded
        except _ArchiveCatalogSuperseded:
            return ArchiveWriteResult.SUPERSEDED
        return ArchiveWriteResult.APPLIED

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
        if not self.candidates_current(repo, (candidate,)):
            return
        self.connection.execute(
            """
            INSERT INTO execplan_archive_items (
                repo, plan_id, active_path, archive_path, expected_hash,
                current_revision_sha,
                base_sha, final_hash, operation_id, commit_oid, push_result,
                pending_reason, archived
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, plan_id) DO UPDATE SET
                active_path = excluded.active_path,
                archive_path = excluded.archive_path,
                expected_hash = excluded.expected_hash,
                current_revision_sha = excluded.current_revision_sha,
                base_sha = excluded.base_sha,
                final_hash = excluded.final_hash,
                operation_id = excluded.operation_id,
                commit_oid = excluded.commit_oid,
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
                candidate.revision_sha,
                base_sha,
                final_hash,
                operation_id,
                commit_oid,
                push_result,
                pending_reason,
                int(archived),
            ),
        )
