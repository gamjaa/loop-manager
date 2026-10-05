"""Project-scoped successful file reviews, independent of legacy maintenance state."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import json
import sqlite3
from typing import Iterator, Sequence
from uuid import uuid4

from loop_manager_internal.maintenance.file_review_protocol import (
    MAX_FIELD_CHARS, MAX_LIST_ITEMS, MIN_DESCRIPTION_CHARS, MIN_EXCERPT_CHARS,
    ROOT_CATEGORIES, ROOT_ITEM_FIELDS, ROOT_REFERENCE_FIELDS,
    require_fields, review_string,
)
from loop_manager_internal.maintenance.maintenance_file_inventory import (
    RootPlacement, SourceInventory, TrackedSource, root_placement_inventory,
    _git, tracked_source_inventory, verify_remote_revision,
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


@dataclass(frozen=True)
class PreparedAssessment:
    selected: tuple[TrackedSource, ...]
    placements: tuple[dict, ...]
    remote_revision: str
    legacy: bool


ROOT_REVIEW_PATH = "."
ROOT_REVIEW_REASON = "revision-bound root placement assessment"


def root_review_source(repository: Path, inventory: SourceInventory) -> TrackedSource:
    content = _git(repository, "ls-tree", inventory.revision)
    return TrackedSource(ROOT_REVIEW_PATH, hashlib.sha256(content).hexdigest(),
                         "040000", inventory.revision, ROOT_REVIEW_REASON)


def root_move_candidates(reviews: Sequence[dict]) -> dict[str, dict]:
    """Only investigated internal items with a safe destination can become plans."""
    return {item["path"]: item for item in reviews
            if item.get("category") == "internal_implementation" and item.get("destination") is not None}


def root_review_hash(review: dict) -> str:
    return hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def assessment_input_tree(repository: Path, revision: str) -> tuple[bytes, ...]:
    """Compare investigated input blobs while allowing normal Planner output."""
    return tuple(line for line in _git(repository, "ls-tree", "-r", revision).splitlines()
                 if not line.split(b"\t", 1)[-1].startswith(b"docs/plans/")
                 and line.split(b"\t", 1)[-1] not in {
                     b"docs/FUTURE_IDEAS.md", b"docs/maintenance/code-map.jsonl",
                 })


def validate_root_reviews(repository: Path, inventory: SourceInventory, results: object) -> tuple[dict, ...]:
    """Require investigated placement decisions and exact Git evidence for every root."""
    roots = {item.path for item in root_placement_inventory(inventory)}
    if not isinstance(results, (list, tuple)) or len(results) != len(roots):
        raise FileReviewLedgerError("모든 루트 항목의 실제 조사 근거가 필요합니다.")
    if not roots:
        return ()
    seen = set()
    tree = _git(repository, "ls-tree", inventory.revision).decode("utf-8", "backslashreplace")
    paths = {item.path for item in inventory.classifications}
    contents = {}
    for result in results:
        require_fields(result, ROOT_ITEM_FIELDS, "root_reviews 항목",
                       error_type=FileReviewLedgerError)
        path = result["path"]
        if not isinstance(path, str) or path not in roots or path in seen:
            raise FileReviewLedgerError("루트 조사 경로가 누락되거나 중복됐습니다.")
        seen.add(path)
        if not isinstance(result["category"], str) or result["category"] not in ROOT_CATEGORIES:
            raise FileReviewLedgerError("루트 조사 분류가 유효하지 않습니다.")
        for key in ("user_path", "ownership", "reason"):
            value = result[key].strip() if isinstance(result[key], str) else result[key]
            review_string(value, f"root_reviews[{path}].{key}", minimum=MIN_DESCRIPTION_CHARS,
                          error_type=FileReviewLedgerError)
        destination = result["destination"]
        if destination is not None and (
            not isinstance(destination, str) or not destination or destination.startswith("/")
            or "\\" in destination or any(part in {"", ".", ".."} for part in destination.split("/"))
        ):
            raise FileReviewLedgerError("안전한 상대 목적지가 필요합니다.")
        if destination is not None and (result["category"] != "internal_implementation" or destination == path):
            raise FileReviewLedgerError("필수·불확실한 루트 항목은 이동 후보가 될 수 없습니다.")
        if path in {".agents", "docs", "scripts", "AGENTS.md"} and (
            result["category"] != "tool_required" or destination is not None
        ):
            raise FileReviewLedgerError("필수 루트 경로는 유지해야 합니다.")
        references = result["references"]
        if not isinstance(references, list) or not 1 <= len(references) <= MAX_LIST_ITEMS:
            raise FileReviewLedgerError("루트 조사에는 원본 참조가 필요합니다.")
        linked = False
        for ref in references:
            require_fields(ref, ROOT_REFERENCE_FIELDS, f"root_reviews[{path}].references 항목",
                           error_type=FileReviewLedgerError)
            reference_path, excerpt = ref["path"], ref["excerpt"]
            if reference_path == ".":
                text = tree
                if result["category"] not in {"uncertain", "tool_required"}:
                    raise FileReviewLedgerError("파일 목록만으로 실행·사용 계약을 증명할 수 없습니다.")
            elif isinstance(reference_path, str) and reference_path in paths:
                if reference_path not in contents:
                    contents[reference_path] = _git(repository, "show", f"{inventory.revision}:{reference_path}").decode("utf-8", "backslashreplace")
                text = contents[reference_path]
            else:
                raise FileReviewLedgerError("루트 참조 경로가 현재 원본에 없습니다.")
            if (not isinstance(excerpt, str)
                or not MIN_EXCERPT_CHARS <= len(excerpt) <= MAX_FIELD_CHARS or excerpt not in text):
                raise FileReviewLedgerError("루트 참조 인용이 현재 원본과 다릅니다.")
            linked = linked or (isinstance(reference_path, str) and reference_path.split("/", 1)[0] == path) or path in excerpt
        if not linked:
            raise FileReviewLedgerError("원본 참조가 조사할 루트 항목과 연결되지 않았습니다.")
    return tuple(results)


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

ROOT_PLACEMENT_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS root_placement_reviews (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    PRIMARY KEY (repo, path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Add the ledger without changing existing maintenance rows or markers."""
    connection.execute(FILE_REVIEW_SCHEMA)
    connection.execute(FILE_ASSESSMENT_SUCCESS_SCHEMA)
    connection.execute(ROOT_PLACEMENT_REVIEW_SCHEMA)


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

    def root_placements(
        self, repo: str, *, include_retired: bool = False,
    ) -> tuple[RootPlacement, ...]:
        rows = self.connection.execute(
            "SELECT path, category, reason FROM root_placement_reviews WHERE repo = ? "
            + ("" if include_retired else "AND active = 1 ")
            + "ORDER BY path",
            (repo,),
        ).fetchall()
        placements = []
        for path, category, reason in rows:
            try:
                note = json.loads(reason)["reason"]
            except (ValueError, TypeError, KeyError):
                note = reason
            placements.append(RootPlacement(str(path), str(category), str(note)))
        return tuple(placements)

    def reusable_root_reviews(self, repo: str, inventory: SourceInventory) -> tuple[dict, ...] | None:
        if not root_placement_inventory(inventory):
            return ()
        rows = self.connection.execute(
            "SELECT reason, source_revision FROM root_placement_reviews WHERE repo = ? AND active = 1 ORDER BY path",
            (repo,),
        ).fetchall()
        try:
            if any(revision != inventory.revision for _, revision in rows):
                return None
            results = [json.loads(raw) for raw, _ in rows]
            return validate_root_reviews(Path(repo), inventory, results)
        except (ValueError, TypeError):
            return None

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

    def _prepare_evidence(
        self, repo: str, repository_path: str, base_branch: str,
        inventory: SourceInventory, reviewed: Sequence[ReviewedSource],
        *, published_revision: str | None = None, root_reviews: Sequence[dict] | None = None,
        legacy_publication_commit: str | None = None,
    ) -> PreparedAssessment:
        """Validate source, root, and review evidence before any ledger write."""
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
        legacy = legacy_publication_commit is not None
        if legacy:
            row = self.connection.execute(
                "SELECT p.evidence_json FROM file_assessment_publications p "
                "JOIN maintenance_cycles c ON c.repo = p.repo AND c.revision = p.cycle_revision "
                "WHERE p.repo = ? AND p.source_revision = ? AND p.publication_commit = ? "
                "AND c.phase = 'assessment' AND c.publication_stage = 'prepared' "
                "AND c.publication_base_sha = p.source_revision "
                "AND c.publication_commit_sha = p.publication_commit",
                (repo, inventory.revision, legacy_publication_commit),
            ).fetchone()
            payload = json.loads(row[0]) if row else {}
            if (published_revision is None or root_reviews is not None
                or "root_reviews" in payload
                or payload.get("inventory") != json.loads(json.dumps(asdict(inventory)))
                or payload.get("reviewed") != [asdict(item) for item in reviewed]
                or _git(source_repo, "merge-base", legacy_publication_commit, remote_revision).decode().strip()
                   != legacy_publication_commit):
                raise FileReviewLedgerError("게시된 이전 평가의 intent·원본 identity가 다릅니다.")
            placements = ()
        else:
            if root_reviews is None:
                root_reviews = self.reusable_root_reviews(repo, inventory)
            placements = validate_root_reviews(source_repo, inventory, root_reviews)
        # Publication may add plans, but must not change investigated root entries
        # or any evidence-bearing input outside Planner-owned output paths.
        if (assessment_input_tree(source_repo, inventory.revision)
            != assessment_input_tree(source_repo, remote_revision)
            or (not legacy and {item.path for item in root_placement_inventory(current)}
                != {item["path"] for item in placements})):
            raise FileReviewLedgerError("루트 조사 후 참조 또는 소유 대상이 변경됐습니다.")
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
        # Keep the first validated decision independent of caller-owned root
        # dictionaries, including their nested reference evidence.
        return PreparedAssessment(selected, deepcopy(placements), remote_revision, legacy)

    def record_success(
        self, repo: str, repository_path: str, base_branch: str,
        inventory: SourceInventory, reviewed: Sequence[ReviewedSource],
        *, published_revision: str | None = None, root_reviews: Sequence[dict] | None = None,
        legacy_publication_commit: str | None = None,
    ) -> AssessmentSuccess:
        """Commit prepared evidence and recheck publication inside the transaction."""
        options = dict(published_revision=published_revision, root_reviews=root_reviews,
                       legacy_publication_commit=legacy_publication_commit)
        prepared = self._prepare_evidence(repo, repository_path, base_branch, inventory, reviewed, **options)
        source_repo = Path(repository_path)
        with _transaction(self.connection):
            if self._prepare_evidence(repo, repository_path, base_branch, inventory, reviewed, **options) != prepared:
                raise FileReviewLedgerError("게시 직전 평가 근거 또는 선정 파일이 변경됐습니다.")
            selected, placements, legacy = prepared.selected, prepared.placements, prepared.legacy
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
            current_root_paths = {item["path"] for item in placements}
            for placement in (() if legacy else self.root_placements(repo, include_retired=True)):
                if placement.path not in current_root_paths:
                    self.connection.execute(
                        "UPDATE root_placement_reviews SET active = 0 "
                        "WHERE repo = ? AND path = ?",
                        (repo, placement.path),
                    )
            for placement in placements:
                self.connection.execute(
                    "INSERT INTO root_placement_reviews "
                    "(repo, path, category, reason, source_revision, reviewed_at, active) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1) "
                    "ON CONFLICT(repo, path) DO UPDATE SET "
                    "category = excluded.category, reason = excluded.reason, "
                    "source_revision = excluded.source_revision, "
                    "reviewed_at = excluded.reviewed_at, active = 1",
                    (repo, placement["path"], placement["category"], json.dumps(placement, ensure_ascii=False, sort_keys=True),
                     inventory.revision, reviewed_at),
                )
            self.connection.execute(
                "INSERT INTO file_assessment_successes (repo, source_revision, reviewed_at) "
                "VALUES (?, ?, ?) ON CONFLICT(repo) DO UPDATE SET "
                "source_revision = excluded.source_revision, reviewed_at = excluded.reviewed_at",
                (repo, inventory.revision, reviewed_at),
            )
            project = self.connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
            ).fetchone()
            if project is None or str(project[0]) != base_branch:
                raise FileReviewLedgerError("등록된 프로젝트의 base branch가 다릅니다.")
            verify_remote_revision(source_repo, base_branch, prepared.remote_revision)
        return AssessmentSuccess(inventory.revision, reviewed_at)
