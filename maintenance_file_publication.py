"""Promote source-bound maintenance evidence only at a confirmed publication."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3

from maintenance_file_inventory import (
    ClassifiedPath, SourceInventory, TrackedSource, cached_source_inventory,
    tracked_source_inventory,
)
from maintenance_file_ledger import FileReviewRepository, ReviewedSource, assessment_input_tree
from maintenance_file_review import ProvisionalReviewRepository
from maintenance_reconciliation import (
    ReconciliationRepository, SynthesisRepository, _valid_map_shape,
)


class FileAssessmentPublicationError(RuntimeError):
    """Validated file evidence cannot be promoted safely."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_assessment_publications (
    repo TEXT PRIMARY KEY,
    cycle_revision INTEGER NOT NULL,
    source_revision TEXT NOT NULL,
    publication_commit TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


@dataclass(frozen=True)
class AssessmentEvidence:
    inventory: SourceInventory
    reviewed: tuple[ReviewedSource, ...]
    code_maps: tuple[tuple[str, str], ...]
    merged_count: int
    root_reviews: tuple[dict, ...] = ()


def sources_match_remote(
    repository: Path, base_branch: str, revision: str,
    evidence: AssessmentEvidence,
) -> bool:
    """Recheck the live remote source set before promoting saved evidence."""
    current = tracked_source_inventory(repository, base_branch, revision)
    return (assessment_input_tree(repository, revision)
            == assessment_input_tree(repository, evidence.inventory.revision)) and tuple(
        (source.path, source.content_hash, source.mode, source.reason)
        for source in current.sources
    ) == tuple(
        (source.path, source.content_hash, source.mode, source.reason)
        for source in evidence.inventory.sources
    )


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)


def collect(
    connection: sqlite3.Connection, repo: str, repository: Path,
    base_branch: str, cycle_revision: int, source_revision: str,
) -> AssessmentEvidence:
    """Freeze only completed selected reviews and fully reconciled relations."""
    inventory = cached_source_inventory(connection, repository, base_branch, source_revision)
    reviews = ProvisionalReviewRepository(connection).progress(
        repo, repository, base_branch, cycle_revision, inventory,
    )
    if reviews.next_unit is not None or len(reviews.completed) != len(reviews.selected):
        raise FileAssessmentPublicationError("선정된 파일의 검토가 완료되지 않았습니다.")
    if reviews.selected:
        progress = ReconciliationRepository(connection).progress(
            repo, repository, base_branch, cycle_revision, inventory, reviews,
        )
        if (progress.pending_scan or progress.next_relation is not None
            or len(progress.completed) != len(progress.relations)):
            raise FileAssessmentPublicationError("파일 간 관계 확인이 완료되지 않았습니다.")
        page = SynthesisRepository(connection).prepare(
            repo, cycle_revision, source_revision, progress,
        )
        if page.index != page.total:
            raise FileAssessmentPublicationError("관계 종합 검토가 완료되지 않았습니다.")
    selected = {source.path: source for source in reviews.selected}
    completed = {result["path"]: result for result in reviews.completed}
    if set(selected) != set(completed):
        raise FileAssessmentPublicationError("검토 완료 파일 집합이 선정 집합과 다릅니다.")
    return AssessmentEvidence(
        inventory,
        tuple(ReviewedSource(
            source.path, source.content_hash, source.mode, source.revision,
            json.dumps(completed[source.path]["evidence"], ensure_ascii=False),
        ) for source in reviews.selected),
        tuple((source.path, json.dumps(completed[source.path]["code_map"],
                                     ensure_ascii=False, sort_keys=True))
              for source in reviews.selected),
        _merged_count(connection, repo),
        reviews.root_reviews,
    )


def _merged_count(connection: sqlite3.Connection, repo: str) -> int:
    # Local import avoids a module cycle: maintenance_state creates this schema.
    import maintenance_state
    return maintenance_state.MaintenanceRepository(connection).merged_count(repo)


def save_intent(
    connection: sqlite3.Connection, repo: str, cycle_revision: int,
    source_revision: str, publication_commit: str, evidence: AssessmentEvidence,
) -> None:
    ensure_schema(connection)
    if evidence.inventory.revision != source_revision:
        raise FileAssessmentPublicationError("평가 증거의 source가 다릅니다.")
    import maintenance_state
    maintenance_state.MaintenanceRepository(connection)._verified_merged_count(
        repo, source_revision, evidence.merged_count,
    )
    payload = json.dumps({
        "inventory": asdict(evidence.inventory),
        "reviewed": [asdict(item) for item in evidence.reviewed],
        "code_maps": list(evidence.code_maps),
        "merged_count": evidence.merged_count,
        "root_reviews": list(evidence.root_reviews),
    }, ensure_ascii=False, sort_keys=True)
    row = connection.execute(
        "SELECT cycle_revision, source_revision, publication_commit, evidence_json "
        "FROM file_assessment_publications WHERE repo = ?", (repo,),
    ).fetchone()
    expected = (cycle_revision, source_revision, publication_commit, payload)
    if row and int(row[0]) == cycle_revision:
        if tuple(row) != expected:
            raise FileAssessmentPublicationError("보존된 파일 평가 publication intent가 다릅니다.")
        return
    connection.execute(
        "INSERT INTO file_assessment_publications VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(repo) DO UPDATE SET cycle_revision = excluded.cycle_revision, "
        "source_revision = excluded.source_revision, "
        "publication_commit = excluded.publication_commit, evidence_json = excluded.evidence_json",
        (repo, *expected),
    )


def saved_intent(
    connection: sqlite3.Connection, repo: str, cycle_revision: int,
    source_revision: str, publication_commit: str,
) -> AssessmentEvidence:
    ensure_schema(connection)
    row = connection.execute(
        "SELECT evidence_json FROM file_assessment_publications "
        "WHERE repo = ? AND cycle_revision = ? AND source_revision = ? "
        "AND publication_commit = ?",
        (repo, cycle_revision, source_revision, publication_commit),
    ).fetchone()
    if row is None:
        raise FileAssessmentPublicationError("검증된 파일 평가 publication intent가 없습니다.")
    try:
        value = json.loads(row[0])
        inventory = SourceInventory(
            value["inventory"]["revision"],
            tuple(TrackedSource(**item) for item in value["inventory"]["sources"]),
            tuple(ClassifiedPath(**item) for item in value["inventory"]["classifications"]),
        )
        if inventory.revision != source_revision:
            raise FileAssessmentPublicationError("파일 평가 intent의 source가 다릅니다.")
        return AssessmentEvidence(
            inventory,
            tuple(ReviewedSource(**item) for item in value["reviewed"]),
            tuple((str(path), str(raw)) for path, raw in value["code_maps"]),
            int(value["merged_count"]),
            tuple(value.get("root_reviews", ())),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise FileAssessmentPublicationError("파일 평가 publication intent가 손상됐습니다.") from error


def promote(
    connection: sqlite3.Connection, repo: str, repository: Path,
    base_branch: str, published_revision: str, evidence: AssessmentEvidence,
) -> None:
    """Caller owns the transaction that also advances the assessment baseline."""
    selected = {item.path: item for item in evidence.reviewed}
    if (len(selected) != len(evidence.reviewed)
        or len({path for path, _ in evidence.code_maps}) != len(evidence.code_maps)
        or {path for path, _ in evidence.code_maps} != set(selected)):
        raise FileAssessmentPublicationError("검토 파일과 코드맵 집합이 다릅니다.")
    for _path, raw in evidence.code_maps:
        try:
            valid = _valid_map_shape(json.loads(raw))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise FileAssessmentPublicationError("검토 파일의 코드맵이 유효하지 않습니다.")
    FileReviewRepository(connection).record_success(
        repo, str(repository), base_branch, evidence.inventory, evidence.reviewed,
        published_revision=published_revision, root_reviews=evidence.root_reviews,
    )
    for path, raw in evidence.code_maps:
        source = selected[path]
        connection.execute(
            "INSERT INTO successful_code_maps "
            "(repo, path, content_hash, mode, code_map_json) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(repo, path) DO UPDATE SET "
            "content_hash = excluded.content_hash, mode = excluded.mode, "
            "code_map_json = excluded.code_map_json",
            (repo, path, source.content_hash, source.mode, raw),
        )
