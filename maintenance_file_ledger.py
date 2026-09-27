"""Project-scoped successful file reviews, independent of legacy maintenance state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Sequence
from uuid import uuid4

from maintenance_file_inventory import (
    SourceInventory, TrackedSource, tracked_source_inventory, verify_remote_revision,
)


class FileReviewLedgerError(ValueError):
    """A file review is stale, incomplete, or not bound to the source tree."""


@dataclass(frozen=True)
class FileReview:
    path: str
    content_hash: str
    mode: str
    source_revision: str
    reviewed_at: str
    active: bool


@dataclass(frozen=True)
class AssessmentSuccess:
    source_revision: str
    reviewed_at: str


@dataclass(frozen=True)
class ReviewedSource:
    """Validated assessment evidence for one selected file, supplied by G-084/086."""

    path: str
    content_hash: str
    mode: str
    source_revision: str
    evidence: str


FILE_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_review_ledger (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mode TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    PRIMARY KEY (repo, path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

FILE_ASSESSMENT_SUCCESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_assessment_successes (
    repo TEXT PRIMARY KEY,
    source_revision TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Add the ledger without changing existing maintenance rows or markers."""
    connection.execute(FILE_REVIEW_SCHEMA)
    connection.execute(FILE_ASSESSMENT_SUCCESS_SCHEMA)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    name = f"file_review_{uuid4().hex}"
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {name}")
        connection.execute(f"RELEASE {name}")
        raise
    else:
        connection.execute(f"RELEASE {name}")


class FileReviewRepository:
    """Read source identity and record only a fully reviewed selected set."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def last_success(self, repo: str) -> AssessmentSuccess | None:
        row = self.connection.execute(
            "SELECT source_revision, reviewed_at FROM file_assessment_successes WHERE repo = ?",
            (repo,),
        ).fetchone()
        return AssessmentSuccess(str(row[0]), str(row[1])) if row else None

    def reviews(self, repo: str, *, include_retired: bool = False) -> tuple[FileReview, ...]:
        rows = self.connection.execute(
            "SELECT path, content_hash, mode, source_revision, reviewed_at, active "
            "FROM file_review_ledger WHERE repo = ? "
            + ("" if include_retired else "AND active = 1 ")
            + "ORDER BY path",
            (repo,),
        ).fetchall()
        return tuple(
            FileReview(str(path), str(content_hash), str(mode), str(revision),
                       str(reviewed_at), bool(active))
            for path, content_hash, mode, revision, reviewed_at, active in rows
        )

    def selected_sources(
        self, repo: str, inventory: SourceInventory,
    ) -> tuple[TrackedSource, ...]:
        if any(source.revision != inventory.revision for source in inventory.sources):
            raise FileReviewLedgerError("inventory 파일과 source revision이 다릅니다.")
        paths = [source.path for source in inventory.sources]
        if paths != sorted(set(paths)):
            raise FileReviewLedgerError("inventory 경로가 중복되거나 정렬되지 않았습니다.")
        previous = {review.path: review for review in self.reviews(repo)}
        return tuple(
            source for source in inventory.sources
            if source.path not in previous
            or (source.content_hash, source.mode) != (
                previous[source.path].content_hash, previous[source.path].mode
            )
        )

    def record_success(
        self, repo: str, repository_path: str, base_branch: str,
        inventory: SourceInventory, reviewed: Sequence[ReviewedSource],
        *, published_revision: str | None = None,
    ) -> AssessmentSuccess:
        """Commit only complete, source-bound review evidence in one transaction.

        Empty evidence is valid only when no source file needs review. Retired
        paths remain in the ledger as audit rows and are ignored for selection.
        """
        source_repo = Path(repository_path)
        if Path(repo).resolve() != source_repo.resolve():
            raise FileReviewLedgerError("프로젝트와 원격 Git 저장소 경로가 다릅니다.")
        project = self.connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if project is None or str(project[0]) != base_branch:
            raise FileReviewLedgerError("등록된 프로젝트의 base branch가 다릅니다.")
        remote_revision = published_revision or inventory.revision
        current = tracked_source_inventory(source_repo, base_branch, remote_revision)
        if remote_revision == inventory.revision and current != inventory:
            raise FileReviewLedgerError("inventory가 검증된 원격 Git tree와 다릅니다.")
        if tuple((item.path, item.content_hash, item.mode, item.reason)
                 for item in current.sources) != tuple(
                     (item.path, item.content_hash, item.mode, item.reason)
                     for item in inventory.sources
                 ):
            raise FileReviewLedgerError("평가 후 원격 파일 목록 또는 내용이 변경됐습니다.")
        with _transaction(self.connection):
            selected = self.selected_sources(repo, inventory)
            if len(reviewed) != len(selected):
                raise FileReviewLedgerError("선정된 모든 파일의 검토 근거가 필요합니다.")
            expected = {source.path: source for source in selected}
            seen: set[str] = set()
            for result in reviewed:
                if not isinstance(result, ReviewedSource) or not isinstance(result.path, str):
                    raise FileReviewLedgerError("검토 결과 형식이 유효하지 않습니다.")
                source = expected.get(result.path)
                if (source is None or result.path in seen
                    or (result.content_hash, result.mode, result.source_revision)
                    != (source.content_hash, source.mode, source.revision)
                    or not re.fullmatch(r"[0-9a-f]{64}", result.content_hash)
                    or not isinstance(result.evidence, str)
                    or len(result.evidence.strip()) < 20):
                    raise FileReviewLedgerError("검토 근거 또는 파일 원본 identity가 유효하지 않습니다.")
                seen.add(result.path)
            if seen != set(expected):
                raise FileReviewLedgerError("선정된 파일의 검토가 누락됐습니다.")
            reviewed_at = datetime.now(timezone.utc).isoformat()
            current_paths = {source.path for source in inventory.sources}
            for review in self.reviews(repo):
                if review.path not in current_paths:
                    self.connection.execute(
                        "UPDATE file_review_ledger SET active = 0 WHERE repo = ? AND path = ?",
                        (repo, review.path),
                    )
            for source in selected:
                self.connection.execute(
                    "INSERT INTO file_review_ledger "
                    "(repo, path, content_hash, mode, source_revision, reviewed_at, active) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(repo, path) DO UPDATE SET "
                    "content_hash = excluded.content_hash, mode = excluded.mode, "
                    "source_revision = excluded.source_revision, reviewed_at = excluded.reviewed_at, "
                    "active = 1",
                    (repo, source.path, source.content_hash, source.mode,
                     inventory.revision, reviewed_at),
                )
            self.connection.execute(
                "INSERT INTO file_assessment_successes (repo, source_revision, reviewed_at) "
                "VALUES (?, ?, ?) ON CONFLICT(repo) DO UPDATE SET "
                "source_revision = excluded.source_revision, reviewed_at = excluded.reviewed_at",
                (repo, inventory.revision, reviewed_at),
            )
            verify_remote_revision(source_repo, base_branch, remote_revision)
        return AssessmentSuccess(inventory.revision, reviewed_at)
