"""Promote source-bound maintenance evidence only at a confirmed publication."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3

from loop_manager_internal.maintenance.maintenance_file_inventory import (
    ClassifiedPath, SourceInventory, TrackedSource, cached_source_inventory,
    tracked_source_inventory,
)
from loop_manager_internal.storage.maintenance_file_ledger import FileReviewRepository, ReviewedSource, PreparedAssessment, release_observation_access, assessment_input_tree, root_move_candidates
from loop_manager_internal.maintenance.maintenance_file_review import ProvisionalReviewRepository
from loop_manager_internal.maintenance.maintenance_reconciliation import (
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
    root_reviews: tuple[dict, ...] | None = ()
    tiered: dict | None = None


def _validate_tiered_root_links(tiered: dict) -> tuple[dict, ...]:
    from loop_manager_internal.maintenance.tiered_review import (
        _root_move_candidates, _validate_root_plan_links,
    )
    reviews = tiered.get("root_reviews")
    candidates = _root_move_candidates(reviews)
    links = tiered.get("root_plan_links")
    if candidates and links is None:
        raise FileAssessmentPublicationError("루트 이동 후보의 계획 처리 결과가 없습니다.")
    normalized = _validate_root_plan_links(reviews, links, required=bool(candidates))
    if links is not None and list(normalized) != links:
        raise FileAssessmentPublicationError("루트 계획 처리 intent의 순서·identity가 다릅니다.")
    return normalized


def _verify_root_plan_files(repository: Path, tiered: dict, *, revision: str | None = None) -> None:
    links = _validate_tiered_root_links(tiered)
    from loop_manager_internal.maintenance.tiered_ports import _root_move_plan_covers
    from loop_manager_internal.maintenance.maintenance_file_inventory import _git
    for item in links:
        if revision is None:
            target = repository / item["plan_path"]
            root = repository.resolve()
            if (target.is_symlink() or not target.resolve().is_relative_to(root)
                    or any(parent.is_symlink() for parent in target.parents if parent != root)
                    or not target.is_file()):
                raise FileAssessmentPublicationError("루트 이동 계획 경로가 안전한 일반 파일이 아닙니다.")
            try:
                contents = target.read_bytes()
            except OSError as error:
                raise FileAssessmentPublicationError("루트 이동 계획을 publication worktree에서 읽을 수 없습니다.") from error
        else:
            try:
                contents = _git(repository, "show", f"{revision}:{item['plan_path']}")
            except (ValueError, OSError) as error:
                raise FileAssessmentPublicationError("게시된 원본에 루트 이동 계획이 없습니다.") from error
        from loop_manager_internal.maintenance import tiered_review
        if tiered_review.plan_content_hash(contents) != item["plan_hash"]:
            raise FileAssessmentPublicationError("루트 이동 계획의 게시 내용이 intent와 다릅니다.")
        try:
            plan_text = contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FileAssessmentPublicationError("루트 이동 계획이 UTF-8 문서가 아닙니다.") from error
        if not _root_move_plan_covers(item["path"], item["destination"], plan_text):
            raise FileAssessmentPublicationError("루트 이동 계획의 보존·검증 조건이 부족합니다.")


def _verify_root_plan_rows(connection: sqlite3.Connection, repo: str, tiered: dict) -> None:
    for item in _validate_tiered_root_links(tiered):
        row = connection.execute("SELECT path, status FROM exec_plans WHERE repo = ? AND plan_id = ?",
                                 (repo, item["plan_id"])).fetchone()
        if (row is None or str(row[0]) != item["plan_path"]
                or str(row[1]) in {"MERGED", "CANCELLED"}):
            raise FileAssessmentPublicationError("루트 이동 계획이 게시 catalog의 비종료 계획과 다릅니다.")


def sources_match_remote(
    repository: Path, base_branch: str, revision: str,
    evidence: AssessmentEvidence,
) -> bool:
    """Recheck the live remote source set before promoting saved evidence."""
    current = tracked_source_inventory(repository, base_branch, revision,
                                       **({"include_uncertain": True} if evidence.tiered is not None else {}))
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
    base_branch: str, cycle_revision: int, source_revision: str, *,
    plan_repository: Path | None = None,
) -> AssessmentEvidence:
    """Freeze only completed selected reviews and fully reconciled relations."""
    from loop_manager_internal.maintenance import tiered_review
    module_assessment = tiered_review.has_assessment(connection, repo, cycle_revision, source_revision)
    inventory = cached_source_inventory(connection, repository, base_branch, source_revision,
                                       **({"include_uncertain": True} if module_assessment else {}))
    if module_assessment:
        evidence = tiered_review.TieredReviewRepository(connection).evidence(repo, cycle_revision, source_revision)
        _verify_root_plan_files(plan_repository or repository, evidence)
        return AssessmentEvidence(inventory, (), (), _merged_count(connection, repo),
                                  tuple(evidence["root_reviews"]), evidence)
    reviews = ProvisionalReviewRepository(connection).progress(
        repo, repository, base_branch, cycle_revision, inventory,
    )
    if reviews.next_unit is not None or len(reviews.completed) != len(reviews.selected):
        raise FileAssessmentPublicationError("선정된 파일의 검토가 완료되지 않았습니다.")
    if reviews.selected or root_move_candidates(reviews.root_reviews):
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
    from loop_manager_internal.storage import maintenance_state
    return maintenance_state.MaintenanceRepository(connection).merged_count(repo)


def save_intent(
    connection: sqlite3.Connection, repo: str, cycle_revision: int,
    source_revision: str, publication_commit: str, evidence: AssessmentEvidence, *,
    plan_repository: Path | None = None,
) -> None:
    ensure_schema(connection)
    if evidence.root_reviews is None:
        raise FileAssessmentPublicationError("새 publication에 이전 형식 증거를 사용할 수 없습니다.")
    if evidence.inventory.revision != source_revision:
        raise FileAssessmentPublicationError("평가 증거의 source가 다릅니다.")
    if evidence.tiered is not None:
        _verify_root_plan_files(plan_repository or Path(repo), evidence.tiered)
    from loop_manager_internal.storage import maintenance_state
    maintenance_state.MaintenanceRepository(connection)._verified_merged_count(
        repo, source_revision, evidence.merged_count,
    )
    payload = json.dumps({
        "inventory": asdict(evidence.inventory),
        "reviewed": [asdict(item) for item in evidence.reviewed],
        "code_maps": list(evidence.code_maps),
        "merged_count": evidence.merged_count,
        "root_reviews": list(evidence.root_reviews),
        **({"tiered": evidence.tiered} if evidence.tiered is not None else {}),
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
    return _decode_evidence(row[0], source_revision)


def _decode_evidence(raw: str, source_revision: str) -> AssessmentEvidence:
    try:
        value = json.loads(raw)
        inventory = SourceInventory(
            value["inventory"]["revision"],
            tuple(TrackedSource(**item) for item in value["inventory"]["sources"]),
            tuple(ClassifiedPath(**item) for item in value["inventory"]["classifications"]),
        )
        if inventory.revision != source_revision:
            raise FileAssessmentPublicationError("파일 평가 intent의 source가 다릅니다.")
        tiered = value.get("tiered")
        if tiered is not None:
            _validate_tiered_root_links(tiered)
        return AssessmentEvidence(
            inventory,
            tuple(ReviewedSource(**item) for item in value["reviewed"]),
            tuple((str(path), str(raw)) for path, raw in value["code_maps"]),
            int(value["merged_count"]),
            tuple(value["root_reviews"]) if "root_reviews" in value else None,
            tiered,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise FileAssessmentPublicationError("파일 평가 publication intent가 손상됐습니다.") from error


def prepare_promotion(
    connection: sqlite3.Connection, repo: str, repository: Path,
    base_branch: str, published_revision: str, evidence: AssessmentEvidence,
) -> PreparedPromotion:
    """Observe publication inputs before entering the batch transaction."""
    release_observation_access(connection)
    ledger = FileReviewRepository(connection)
    identity = ledger._evidence_identity(repo)
    if Path(repo).resolve() != repository.resolve():
        raise FileAssessmentPublicationError("프로젝트와 원격 Git 저장소 경로가 다릅니다.")
    project = connection.execute("SELECT base_branch FROM projects WHERE repo = ?", (repo,)).fetchone()
    if project is None or str(project[0]) != base_branch:
        raise FileAssessmentPublicationError("등록된 프로젝트의 base branch가 다릅니다.")
    payload = asdict(evidence)
    if evidence.root_reviews is None:
        payload.pop("root_reviews")
    evidence_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    evidence = _decode_evidence(evidence_json, evidence.inventory.revision)
    if evidence.tiered is not None:
        if not sources_match_remote(repository, base_branch, published_revision, evidence):
            raise FileAssessmentPublicationError("정적 코드맵·정밀 근거의 원본이 게시된 원본과 다릅니다.")
        _verify_root_plan_files(repository, evidence.tiered, revision=published_revision)
        expected = {source.path: (source.content_hash, source.mode) for source in evidence.inventory.sources}
        records = evidence.tiered.get("records", {})
        if (not isinstance(records, dict) or {path: (record.get("content_hash"), record.get("mode"))
            for path, record in records.items()} != expected or evidence.reviewed or evidence.code_maps
            or evidence.tiered.get("source_revision") != evidence.inventory.revision):
            raise FileAssessmentPublicationError("정밀 평가와 정적 코드맵의 원본 범위가 다릅니다.")
        from loop_manager_internal.maintenance.tiered_review import TieredReviewRepository
        if TieredReviewRepository(connection).evidence(
                repo, evidence.tiered["cycle"], evidence.inventory.revision) != evidence.tiered:
            raise FileAssessmentPublicationError("보존된 정밀 근거가 현재 평가와 다릅니다.")
        from loop_manager_internal.maintenance.maintenance_file_inventory import verify_remote_revision
        verify_remote_revision(repository, base_branch, published_revision)
        return PreparedPromotion(repo, evidence.inventory.revision, published_revision, evidence_json, identity, None)
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
    legacy_commit = None
    if evidence.root_reviews is None:
        # Only a durable pre-policy intent can use its original assessment scope.
        # A missing root investigation remains pending for the next normal cycle.
        row = connection.execute(
            "SELECT cycle_revision, source_revision, publication_commit "
            "FROM file_assessment_publications WHERE repo = ?", (repo,),
        ).fetchone()
        if row is None or saved_intent(connection, repo, int(row[0]), str(row[1]), str(row[2])) != evidence:
            raise FileAssessmentPublicationError("이전 publication 원본 증거를 확인할 수 없습니다.")
        legacy_commit = str(row[2])
    prepared = ledger._prepare_evidence(
        repo, str(repository), base_branch, evidence.inventory, evidence.reviewed,
        published_revision=published_revision, root_reviews=evidence.root_reviews,
        legacy_publication_commit=legacy_commit,
    )
    return PreparedPromotion(repo, evidence.inventory.revision, published_revision, evidence_json, identity, prepared)


@dataclass(frozen=True)
class PreparedPromotion:
    repo: str
    source_revision: str
    published_revision: str
    evidence_json: str
    db_identity: tuple
    ledger: PreparedAssessment | None

    @property
    def evidence(self) -> AssessmentEvidence:
        return _decode_evidence(self.evidence_json, self.source_revision)


def apply_promotion(connection: sqlite3.Connection, prepared: PreparedPromotion) -> None:
    """Apply observed evidence within the caller's atomic batch."""
    from loop_manager_internal.storage import runtime_state
    with runtime_state._transaction(connection):
        repo = prepared.repo
        ledger = FileReviewRepository(connection)
        if ledger._evidence_identity(repo) != prepared.db_identity:
            raise FileAssessmentPublicationError("게시 직전 평가 소유권·선정 범위·intent가 변경됐습니다.")
        evidence = prepared.evidence
        if evidence.tiered is not None:
            _verify_root_plan_rows(connection, repo, evidence.tiered)
            from loop_manager_internal.maintenance.tiered_review import TieredReviewRepository
            TieredReviewRepository(connection)._write_success(repo, evidence.tiered)
            return
        ledger.apply_success(prepared.ledger)
        selected = {item.path: item for item in evidence.reviewed}
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


def promote(
    connection: sqlite3.Connection, repo: str, repository: Path,
    base_branch: str, published_revision: str, evidence: AssessmentEvidence,
) -> None:
    """Compatibility facade; batch callers prepare before their transaction."""
    prepared = prepare_promotion(connection, repo, repository, base_branch, published_revision, evidence)
    apply_promotion(connection, prepared)
