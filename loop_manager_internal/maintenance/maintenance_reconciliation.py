"""Source-bound, resumable cross-file maintenance review and findings."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from loop_manager_internal.agents import common_guidance
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, Sequence

from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, TrackedSource, cached_source_inventory
from loop_manager_internal.maintenance.maintenance_file_review import FileReviewError, FileReviewProgress, source_bytes
from loop_manager_internal.storage.maintenance_file_ledger import root_move_candidates, root_review_hash


class ReconciliationError(ValueError):
    """A relationship or finding lacks current-source evidence."""


RELATION_PREFIX = "RELATION_RESULT: "
FINDINGS_PREFIX = "FINDINGS_RESULT: "
_SCHEMA = """
CREATE TABLE IF NOT EXISTS provisional_relations (
    repo TEXT NOT NULL,
    cycle_revision INTEGER NOT NULL,
    source_revision TEXT NOT NULL,
    relation_key TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    result_json TEXT,
    session_id TEXT,
    validation_error TEXT,
    PRIMARY KEY (repo, relation_key),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""
_MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS successful_code_maps (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mode TEXT NOT NULL,
    code_map_json TEXT NOT NULL,
    PRIMARY KEY (repo, path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)
    connection.execute(_MAP_SCHEMA)


@dataclass(frozen=True)
class Relation:
    key: str
    left: TrackedSource
    right: TrackedSource
    signals: tuple[dict, ...]


@dataclass(frozen=True)
class ReconciliationProgress:
    selected: tuple[TrackedSource, ...]
    completed: tuple[dict, ...]
    relations: tuple[Relation, ...]
    next_relation: Relation | None
    excluded_candidates: tuple[dict, ...] = ()
    file_reviews: tuple[dict, ...] = ()
    pending_scan: bool = False
    root_reviews: tuple[dict, ...] = ()


def _note(value: object, field: str, minimum: int = 12) -> str:
    if not isinstance(value, str) or len(value.strip()) < minimum or len(value) > 2000:
        raise ReconciliationError(f"{field}에는 구체적인 설명이 필요합니다.")
    return value.strip()


def _single_json(output: str, prefix: str) -> dict:
    lines = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise ReconciliationError(f"{prefix.strip()} JSON 줄이 정확히 하나 필요합니다.")
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise ReconciliationError("종합 결과 JSON이 유효하지 않습니다.") from error
    if not isinstance(value, dict):
        raise ReconciliationError("종합 결과는 JSON object여야 합니다.")
    return value


def _key(left: str, right: str) -> str:
    return hashlib.sha256(json.dumps(sorted((left, right)), ensure_ascii=False).encode()).hexdigest()


def _valid_map_shape(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "responsibility", "entry_points", "public_symbols", "direct_references", "related_tests",
    }:
        return False
    if not isinstance(value["responsibility"], str):
        return False
    if any(not isinstance(value[field], list) or any(not isinstance(item, str) for item in value[field])
           for field in ("entry_points", "public_symbols", "related_tests")):
        return False
    refs = value["direct_references"]
    return isinstance(refs, list) and all(
        isinstance(ref, dict) and set(ref) == {"target_path", "symbol", "evidence"}
        and (ref["target_path"] is None or isinstance(ref["target_path"], str))
        and (ref["symbol"] is None or isinstance(ref["symbol"], str))
        and isinstance(ref["evidence"], str)
        for ref in refs
    )


def _excluded_candidates(inventory: SourceInventory, progress: FileReviewProgress,
                         maps: Mapping[str, dict]) -> tuple[dict, ...]:
    sources = {item.path for item in inventory.sources}
    selected = {item.path for item in progress.selected}
    classifications = {item.path: item.reason for item in inventory.classifications}
    excluded = set()
    for origin, code_map in maps.items():
        if origin not in selected:
            continue
        for ref in code_map["direct_references"]:
            target = ref["target_path"]
            if target and target not in sources:
                excluded.add((origin, target, "direct_reference", classifications.get(target, "현재 코드·테스트 원본 목록에 없음")))
        for target in code_map["related_tests"]:
            if target not in sources:
                excluded.add((origin, target, "related_test", classifications.get(target, "현재 코드·테스트 원본 목록에 없음")))
    for item in progress.completed:
        for ref in item["pending_references"]:
            target = ref["target_path"]
            if target and target not in sources:
                excluded.add((item["path"], target, "pending_reference", classifications.get(target, "현재 코드·테스트 원본 목록에 없음")))
        for candidate in item["similar_responsibilities"]:
            target = candidate["target_path"]
            if target not in sources:
                excluded.add((item["path"], target, "similar_responsibility", classifications.get(target, "현재 코드·테스트 원본 목록에 없음")))
    return tuple({"origin": origin, "target": target, "kind": kind, "reason": reason}
                 for origin, target, kind, reason in sorted(excluded))


def relation_candidates(
    inventory: SourceInventory, progress: FileReviewProgress,
    previous_maps: Mapping[str, dict], context_contents: Mapping[str, str],
    cached_signals: Sequence[dict] = (),
) -> tuple[Relation, ...]:
    """Combine both directions; source search fills gaps in old map coverage."""
    if progress.next_unit is not None or len(progress.completed) != len(progress.selected):
        raise ReconciliationError("선정된 모든 파일의 실제 검토가 먼저 필요합니다.")
    selected = {item.path for item in progress.selected}
    sources = {item.path: item for item in inventory.sources}
    maps = {path: value for path, value in previous_maps.items() if path in sources}
    maps.update({item["path"]: item["code_map"] for item in progress.completed})
    signals: dict[tuple[str, str], list[dict]] = {}

    def add(origin: str, target: str, kind: str, evidence: str) -> None:
        if origin == target or target not in sources or not ({origin, target} & selected):
            return
        pair = tuple(sorted((origin, target)))
        signal = {"origin": origin, "target": target, "kind": kind, "evidence": evidence}
        if signal not in signals.setdefault(pair, []):
            signals[pair].append(signal)

    for item in progress.completed:
        for ref in item["pending_references"]:
            if ref["target_path"]:
                add(item["path"], ref["target_path"], "pending_reference", ref["evidence"])
            elif ref["symbol"]:
                for target, code_map in maps.items():
                    if ref["symbol"] in code_map["public_symbols"]:
                        add(item["path"], target, "pending_symbol", ref["evidence"])
        for candidate in item["similar_responsibilities"]:
            add(item["path"], candidate["target_path"], "similar_responsibility", candidate["reason"])
    for path, code_map in maps.items():
        for ref in code_map["direct_references"]:
            if ref["target_path"]:
                add(path, ref["target_path"], "direct_reference", ref["evidence"])
            elif ref["symbol"]:
                for target, target_map in maps.items():
                    if ref["symbol"] in target_map["public_symbols"]:
                        add(path, target, "direct_symbol", ref["evidence"])
        for test in code_map["related_tests"]:
            add(path, test, "related_test", test)
    for path in selected:
        responsibility = maps[path]["responsibility"].strip().casefold()
        for target, code_map in maps.items():
            if (target != path and len(responsibility) >= 12
                and code_map["responsibility"].strip().casefold() == responsibility):
                add(path, target, "similar_map_responsibility", responsibility)
    for signal in (*cached_signals, *reverse_signals(progress, maps, context_contents)):
        add(signal["origin"], signal["target"], signal["kind"], signal["evidence"])
    return tuple(
        Relation(_key(*pair), sources[pair[0]], sources[pair[1]],
                 tuple(sorted(values, key=lambda value: json.dumps(value, sort_keys=True))))
        for pair, values in sorted(signals.items())
    )


def _contains_symbol(content: str, symbol: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(symbol) + r"(?!\w)", content) is not None


def reverse_signals(progress: FileReviewProgress, maps: Mapping[str, dict],
                    context: Mapping[str, str]) -> tuple[dict, ...]:
    signals = []
    for path, content in context.items():
        for item in progress.completed:
            target = item["path"]
            paths = (target, target.rsplit("/", 1)[-1])
            symbols = item["code_map"]["public_symbols"]
            for needle in (*paths, *symbols):
                # Short public names (foo, id, x) are still real references.
                # False positives are resolved by the relation verification unit.
                if needle and (needle in content if needle in paths else _contains_symbol(content, needle)):
                    signals.append({"origin": path, "target": target,
                                    "kind": "source_reverse" if path in maps else "unmapped_reverse",
                                    "evidence": needle})
            for ref in item["pending_references"]:
                if ref["target_path"] is None and ref["symbol"] and _contains_symbol(content, ref["symbol"]):
                    signals.append({"origin": target, "target": path,
                                    "kind": "unmapped_symbol_target", "evidence": ref["evidence"]})
    return tuple(signals)


def _relation_metadata(output: str, relation: Relation) -> dict:
    value = _single_json(output, RELATION_PREFIX)
    if set(value) != {"key", "status", "left_excerpt", "right_excerpt", "reason"}:
        raise ReconciliationError("관계 확인 결과 필드가 유효하지 않습니다.")
    if value["key"] != relation.key or not isinstance(value["status"], str) or value["status"] not in {"confirmed", "excluded"}:
        raise ReconciliationError("관계 identity 또는 처리 상태가 다릅니다.")
    for field in ("left_excerpt", "right_excerpt"):
        _note(value[field], field, 4)
    value["reason"] = _note(value["reason"], "관계 확인·제외 근거", 20)
    return value


def parse_relation(output: str, relation: Relation, left: bytes, right: bytes) -> dict:
    value = _relation_metadata(output, relation)
    for field, content in (("left_excerpt", left), ("right_excerpt", right)):
        if value[field].strip() not in content.decode("utf-8", "backslashreplace"):
            raise ReconciliationError(f"{field}가 현재 원본에 없습니다.")
    return value


def relation_prompt(relation: Relation, left: bytes, right: bytes) -> str:
    def excerpt(content: bytes, signals: Sequence[dict]) -> str:
        text = content.decode("utf-8", "backslashreplace")
        positions = [text.find(str(item["evidence"])) for item in signals]
        positions = [position for position in positions if position >= 0]
        if len(content) <= 48_000:
            return text
        if not positions:
            return text[:2000] + "\n[중간 생략: 필요한 Git 원본 부분을 직접 읽어라]"
        return "\n[중간 생략]\n".join(
            text[max(0, pos - 500):pos + 1500] for pos in positions[:8]
        )
    identity = {"key": relation.key, "left": relation.left.__dict__,
                "right": relation.right.__dict__, "signals": relation.signals}
    return (
        "당신은 유지보수 Planner다. 양쪽 현재 Git 원본과 후보를 확인해 관계를 확정하거나 "
        "구체적 근거로 제외하라. 생략된 부분이 필요하면 지정 revision의 Git 원본을 읽어라. "
        "전체 선정 파일을 다시 검토하지 마라. 파일을 수정하지 마라.\n"
        + json.dumps(identity, ensure_ascii=False) + "\nleft 원본:\n"
        + excerpt(left, relation.signals) + "\nright 원본:\n"
        + excerpt(right, relation.signals) + "\n"
        "RELATION_RESULT: 한 줄 JSON으로 key, status(confirmed|excluded), "
        "left_excerpt, right_excerpt(각 현재 원본의 정확한 4자 이상 인용), "
        "reason(확인 또는 제외의 구체적인 이유)를 기록하라."
    )


class ReconciliationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)
        connection.execute("""CREATE TABLE IF NOT EXISTS provisional_reverse_scans (
            repo TEXT NOT NULL, path TEXT NOT NULL, identity TEXT NOT NULL,
            signals_json TEXT NOT NULL, PRIMARY KEY (repo, path))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS provisional_relation_receipts (
            repo TEXT NOT NULL, relation_key TEXT NOT NULL, identity TEXT NOT NULL,
            PRIMARY KEY (repo, relation_key))""")

    def valid_maps(self, repo: str, inventory: SourceInventory) -> dict[str, dict]:
        sources = {item.path: item for item in inventory.sources}
        result = {}
        for path, digest, mode, raw in self.connection.execute(
            "SELECT path, content_hash, mode, code_map_json FROM successful_code_maps WHERE repo = ?", (repo,),
        ):
            source = sources.get(path)
            if source and (digest, mode) == (source.content_hash, source.mode):
                try:
                    value = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if _valid_map_shape(value):
                    result[path] = value
        from loop_manager_internal.maintenance.code_map_artifact import read_maps
        imported = read_maps(Path(repo), inventory, result)
        # This caches verified context only. Neither the reviewed-file ledger,
        # successful assessment baseline nor execution state is reconstructed.
        for path, value in imported.items():
            source = sources[path]
            self.connection.execute(
                "INSERT INTO successful_code_maps VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(repo, path) DO UPDATE SET content_hash = excluded.content_hash, "
                "mode = excluded.mode, code_map_json = excluded.code_map_json",
                (repo, path, source.content_hash, source.mode, json.dumps(value, ensure_ascii=False, sort_keys=True)),
            )
        result.update(imported)
        return result

    def progress(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 inventory: SourceInventory, reviews: FileReviewProgress) -> ReconciliationProgress:
        if cached_source_inventory(self.connection, repository, base, inventory.revision) != inventory:
            raise ReconciliationError("관계 종합 source가 현재 원격과 다릅니다.")
        maps = self.valid_maps(repo, inventory)
        selected = {item.path for item in reviews.selected}
        query = hashlib.sha256(json.dumps(
            ["reverse-symbol-v1", common_guidance.effective_planning_hash(self.connection, repo),
             sorted(reviews.completed, key=lambda item: item["path"])],
            ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        signals, remaining, reads = [], False, 0
        for source in inventory.sources:
            if source.path in selected:
                continue
            identity = json.dumps([cycle_revision, source.__dict__, query, source.path in maps], sort_keys=True)
            row = self.connection.execute(
                "SELECT identity, signals_json FROM provisional_reverse_scans WHERE repo = ? AND path = ?",
                (repo, source.path),
            ).fetchone()
            if row and row[0] == identity:
                signals.extend(json.loads(row[1]))
                continue
            if reads >= 16:
                remaining = True
                continue
            content = source_bytes(repository, base, source).decode("utf-8", "backslashreplace")
            found = reverse_signals(reviews, maps, {source.path: content})
            # Commit each source independently so an interrupted batch resumes at
            # the next source. Store only matched signals, never all source text.
            with self.connection:
                self.connection.execute("INSERT OR REPLACE INTO provisional_reverse_scans VALUES (?, ?, ?, ?)",
                    (repo, source.path, identity, json.dumps(found, ensure_ascii=False)))
            signals.extend(found)
            reads += 1
        relations = relation_candidates(inventory, reviews, maps, {}, signals)
        expected = {item.key: item for item in relations}
        completed = {}
        with self.connection:
            for key, cycle, revision, raw_candidate, raw_result in self.connection.execute(
                "SELECT relation_key, cycle_revision, source_revision, candidate_json, result_json "
                "FROM provisional_relations WHERE repo = ?", (repo,),
            ).fetchall():
                relation = expected.get(key)
                current = json.dumps(relation.signals, ensure_ascii=False, sort_keys=True) if relation else None
                if relation is None or cycle != cycle_revision or revision != inventory.revision or raw_candidate != current:
                    self.connection.execute("DELETE FROM provisional_relations WHERE repo = ? AND relation_key = ?", (repo, key))
                    continue
                if raw_result:
                    try:
                        identity = hashlib.sha256(json.dumps([
                            cycle, revision, relation.left.__dict__, relation.right.__dict__,
                            raw_candidate, raw_result,
                        ], sort_keys=True).encode()).hexdigest()
                        receipt = self.connection.execute(
                            "SELECT identity FROM provisional_relation_receipts WHERE repo = ? AND relation_key = ?",
                            (repo, key),
                        ).fetchone()
                        if receipt and receipt[0] == identity:
                            completed[key] = _relation_metadata(RELATION_PREFIX + raw_result, relation)
                        else:
                            completed[key] = parse_relation(RELATION_PREFIX + raw_result, relation,
                                source_bytes(repository, base, relation.left),
                                source_bytes(repository, base, relation.right))
                            self.connection.execute("INSERT OR REPLACE INTO provisional_relation_receipts VALUES (?, ?, ?)",
                                                    (repo, key, identity))
                    except ReconciliationError:
                        self.connection.execute("DELETE FROM provisional_relations WHERE repo = ? AND relation_key = ?", (repo, key))
        next_relation = next((item for item in relations if item.key not in completed), None)
        return ReconciliationProgress(reviews.selected, tuple(completed[key] for key in sorted(completed)),
                                      relations, next_relation,
                                      _excluded_candidates(inventory, reviews, maps), reviews.completed, remaining, reviews.root_reviews)

    def start(self, repo: str, cycle_revision: int, revision: str, relation: Relation) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO provisional_relations "
                "(repo, cycle_revision, source_revision, relation_key, candidate_json) VALUES (?, ?, ?, ?, ?)",
                (repo, cycle_revision, revision, relation.key,
                 json.dumps(relation.signals, ensure_ascii=False, sort_keys=True)),
            )

    def session(self, repo: str, cycle_revision: int, relation: Relation, session_id: str) -> None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE provisional_relations SET session_id = ? WHERE repo = ? AND relation_key = ? "
                "AND cycle_revision = ? AND result_json IS NULL",
                (session_id, repo, relation.key, cycle_revision),
            )
            if changed.rowcount != 1:
                raise ReconciliationError("관계 session의 source가 바뀌었습니다.")

    def complete(self, repo: str, cycle_revision: int, relation: Relation, result: dict) -> None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE provisional_relations SET result_json = ?, session_id = NULL, validation_error = NULL "
                "WHERE repo = ? AND relation_key = ? AND cycle_revision = ? AND result_json IS NULL",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), repo, relation.key, cycle_revision),
            )
            if changed.rowcount != 1:
                raise ReconciliationError("관계 저장 전 source가 바뀌었습니다.")

    def fail(self, repo: str, cycle_revision: int, relation: Relation, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE provisional_relations SET validation_error = ? WHERE repo = ? AND relation_key = ? "
                "AND cycle_revision = ? AND result_json IS NULL",
                (error[:2000], repo, relation.key, cycle_revision),
            )

    def unit(self, repo: str, relation: Relation) -> tuple[str | None, str | None]:
        row = self.connection.execute(
            "SELECT session_id, validation_error FROM provisional_relations WHERE repo = ? AND relation_key = ?",
            (repo, relation.key),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)


def _validate_no_issue(value: object, progress: ReconciliationProgress) -> None:
    if not isinstance(value, dict) or set(value) != {"files", "relations"}:
        raise ReconciliationError("문제 없음 근거는 모든 파일 검토와 관계에 연결해야 합니다.")
    reviews = {item["path"]: item for item in progress.file_reviews}
    selected = {item.path for item in progress.selected}
    if set(reviews) != selected:
        raise ReconciliationError("문제 없음 근거에 전체 파일 검토가 필요합니다.")
    for field, expected in (("files", reviews), ("relations", {item["key"]: item for item in progress.completed})):
        entries = value[field]
        if not isinstance(entries, list):
            raise ReconciliationError("문제 없음 근거는 배열이어야 합니다.")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "excerpt", "reason"}:
                raise ReconciliationError("문제 없음 근거 필드가 유효하지 않습니다.")
            identity = entry["id"]
            if not isinstance(identity, str) or identity not in expected or identity in seen:
                raise ReconciliationError("문제 없음 근거의 대상이 다르거나 중복됐습니다.")
            seen.add(identity)
            evidence = expected[identity]["evidence"] if field == "files" else expected[identity]["reason"]
            excerpt = _note(entry["excerpt"], "검토 근거 인용", 12)
            if excerpt not in json.dumps(evidence, ensure_ascii=False):
                raise ReconciliationError("문제 없음 근거가 검증된 검토와 다릅니다.")
            _note(entry["reason"], "문제 없음 판단", 20)
        if seen != set(expected):
            raise ReconciliationError("문제 없음 근거에 파일 또는 관계가 누락됐습니다.")


def _validate_plan_comparisons(finding: dict, existing: Mapping[str, str],
                               texts: Mapping[str, str]) -> None:
    comparisons = finding["plan_comparisons"]
    active = {key for key, status in existing.items() if status != "MERGED"}
    if not isinstance(comparisons, list):
        raise ReconciliationError("계획 범위 비교가 필요합니다.")
    seen, matches = set(), set()
    for comparison in comparisons:
        if not isinstance(comparison, dict) or set(comparison) != {
            "plan_id", "plan_excerpt", "finding_excerpt", "same_issue", "reason",
        }:
            raise ReconciliationError("계획 범위 비교 필드가 유효하지 않습니다.")
        plan_id = comparison["plan_id"]
        if not isinstance(plan_id, str) or plan_id not in active or plan_id in seen:
            raise ReconciliationError("계획 범위 비교 대상이 다르거나 중복됐습니다.")
        seen.add(plan_id)
        excerpt = _note(comparison["plan_excerpt"], "계획 범위 인용", 20)
        if excerpt not in texts.get(plan_id, ""):
            raise ReconciliationError("계획 범위 근거가 원본과 다릅니다.")
        finding_excerpt = _note(comparison["finding_excerpt"], "발견 책임 경계 인용", 20)
        if finding_excerpt not in finding["boundary"]:
            raise ReconciliationError("계획 비교가 발견 책임 경계와 다릅니다.")
        if type(comparison["same_issue"]) is not bool:
            raise ReconciliationError("계획의 동일 문제 판단이 필요합니다.")
        _note(comparison["reason"], "계획 범위 비교 이유", 20)
        if comparison["same_issue"]:
            matches.add(plan_id)
    if seen != active:
        raise ReconciliationError("모든 비종료 계획의 범위 비교가 필요합니다.")
    if ((finding["status"] == "existing_plan" and finding["plan_id"] not in matches)
        or (finding["status"] == "new_plan" and matches)):
        raise ReconciliationError("같은 문제의 기존 계획 연결 또는 중복 방지가 필요합니다.")


def parse_findings(output: str | dict, progress: ReconciliationProgress,
                   inventory: SourceInventory, contents: Mapping[str, bytes],
                   existing_plans: Mapping[str, str], new_plan_ids: Sequence[str],
                   plan_texts: Mapping[str, str] | None = None,
                   synthesis_digest: str | None = None) -> dict:
    if progress.pending_scan or progress.next_relation is not None or len(progress.completed) != len(progress.relations):
        raise ReconciliationError("미확인 파일 간 관계가 남았습니다.")
    value = _single_json(output, FINDINGS_PREFIX) if isinstance(output, str) else output
    if (not isinstance(value, dict) or set(value) != {"source_revision", "findings", "no_issue_reason"}
        or value["source_revision"] != inventory.revision):
        raise ReconciliationError("발견 결과의 source 또는 필드가 다릅니다.")
    roots = root_move_candidates(progress.root_reviews)
    findings = value["findings"]
    if not isinstance(findings, list):
        raise ReconciliationError("발견은 배열이어야 합니다.")
    if not findings:
        if roots:
            raise ReconciliationError("확인된 루트 이동 후보마다 계획 연결이 필요합니다.")
        reason = value["no_issue_reason"]
        if synthesis_digest is not None and isinstance(reason, dict) and set(reason) == {"synthesis_digest", "summary"}:
            if reason["synthesis_digest"] != synthesis_digest:
                raise ReconciliationError("문제 없음 근거의 종합 snapshot이 다릅니다.")
            _note(reason["summary"], "문제 없음 요약", 20)
            # This is a provisional decision. The adapter must validate every
            # durable page resolution before returning the final result.
        else:
            _validate_no_issue(reason, progress)
    elif value["no_issue_reason"] is not None:
        raise ReconciliationError("발견과 문제 없음 근거가 상충합니다.")
    sources = {item.path: item for item in inventory.sources}
    selected = {item.path for item in progress.selected}
    seen = set()
    confirmed = {item["key"] for item in progress.completed if item["status"] == "confirmed"}
    relations = {item.key: item for item in progress.relations}
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {
            "issue_key", "targets", "evidence", "relation_keys", "boundary",
            "context_reduction", "integration_cost", "status", "plan_id", "plan_comparisons",
        }:
            raise ReconciliationError("발견 필드가 유효하지 않습니다.")
        issue_key = _note(finding["issue_key"], "문제 식별자", minimum=4)
        if issue_key in seen:
            raise ReconciliationError("같은 문제의 발견이 중복됐습니다.")
        seen.add(issue_key)
        targets = finding["targets"]
        if (not isinstance(targets, list) or not targets
            or any(not isinstance(path, str) for path in targets)
            or len(targets) != len(set(targets))
            or not set(targets) <= sources.keys() | roots.keys()
            or not set(targets) & (selected | roots.keys())):
            raise ReconciliationError("발견 대상은 선정된 현재 원본을 포함해야 합니다.")
        evidence = finding["evidence"]
        if (not isinstance(evidence, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("path"), str) for item in evidence)
            or set(targets) != {item["path"] for item in evidence}):
            raise ReconciliationError("모든 발견 대상의 코드 근거가 필요합니다.")
        for item in evidence:
            if isinstance(item, dict) and set(item) == {"path", "root_review_hash", "excerpt"}:
                root = roots.get(item["path"])
                if (root is None or item["root_review_hash"] != root_review_hash(root)
                    or _note(item["excerpt"], "루트 조사 근거", 12) not in json.dumps(root, ensure_ascii=False, sort_keys=True)):
                    raise ReconciliationError("루트 발견의 조사 근거 또는 목적지가 다릅니다.")
                continue
            if not isinstance(item, dict) or set(item) != {"path", "content_hash", "mode", "excerpt"}:
                raise ReconciliationError("발견 근거의 source identity가 필요합니다.")
            source = sources.get(item["path"])
            if source is None:
                raise ReconciliationError("제외된 루트 항목에는 검증된 루트 조사 근거가 필요합니다.")
            if (item["content_hash"], item["mode"]) != (source.content_hash, source.mode) or _note(item["excerpt"], "코드 근거", 4) not in contents[item["path"]].decode("utf-8", "backslashreplace"):
                raise ReconciliationError("발견 근거가 현재 원본과 다릅니다.")
        relation_keys = finding["relation_keys"]
        if (not isinstance(relation_keys, list)
            or any(not isinstance(key, str) for key in relation_keys)
            or len(relation_keys) != len(set(relation_keys))
            or not set(relation_keys) <= confirmed
            or (len(targets) > 1 and not relation_keys)):
            raise ReconciliationError("발견 관계가 확인되지 않았습니다.")
        connected = {targets[0]}
        edges = [(relations[key].left.path, relations[key].right.path) for key in relation_keys]
        while True:
            grown = connected | {right for left, right in edges if left in connected} | {
                left for left, right in edges if right in connected
            }
            if grown == connected:
                break
            connected = grown
        if not set(targets) <= connected:
            raise ReconciliationError("확인된 관계가 발견 대상들을 연결하지 않습니다.")
        for field in ("boundary", "context_reduction", "integration_cost"):
            _note(finding[field], field, minimum=20)
        for path in set(targets) & roots.keys():
            if roots[path]["destination"] not in finding["boundary"]:
                raise ReconciliationError("루트 이동 계획은 조사한 안전한 목적지를 유지해야 합니다.")
        status, plan_id = finding["status"], finding["plan_id"]
        if (not isinstance(status, str) or not isinstance(plan_id, str)
            or (status == "existing_plan" and (plan_id not in existing_plans or existing_plans[plan_id] == "MERGED"))
            or (status == "new_plan" and plan_id not in new_plan_ids)
            or status not in {"existing_plan", "new_plan"}):
            raise ReconciliationError("발견의 비종료 또는 새 계획 연결이 유효하지 않습니다.")
        _validate_plan_comparisons(finding, existing_plans, plan_texts or {})
    covered_roots = {path for finding in findings for path in finding["targets"]}
    if not roots.keys() <= covered_roots:
        raise ReconciliationError("확인된 루트 이동 후보의 계획 연결이 누락됐습니다.")
    if {item["plan_id"] for item in findings if item["status"] == "new_plan"} != set(new_plan_ids):
        raise ReconciliationError("새 리팩터링 계획마다 근거 있는 발견 연결이 필요합니다.")
    return value


SYNTHESIS_PREFIX = "SYNTHESIS_PAGE_RESULT: "
SYNTHESIS_PAGE_CHARS = 12_000


@dataclass(frozen=True)
class SynthesisPage:
    digest: str
    index: int
    total: int
    text: str
    session_id: str | None

    @property
    def key(self) -> str:
        return f"{self.digest}:{self.index}"


def synthesis_pages(progress: ReconciliationProgress) -> tuple[str, ...]:
    """Losslessly page validated evidence, including oversized single-file records."""
    if progress.pending_scan or progress.next_relation is not None or len(progress.completed) != len(progress.relations):
        raise ReconciliationError("미확인 관계가 있어 종합을 시작할 수 없습니다.")
    records = (
        *(('selected', source.__dict__) for source in progress.selected),
        *(('file_review', review) for review in progress.file_reviews),
        *(('root_placement', review) for review in progress.root_reviews),
        *(('relation', result) for result in progress.completed),
        *(('excluded_candidate', candidate) for candidate in progress.excluded_candidates),
    )
    fragments = []
    for record_index, (kind, record) in enumerate(records):
        text = json.dumps({"kind": kind, "value": record}, ensure_ascii=False, sort_keys=True)
        # Split oversized records losslessly, then pack small records together.
        # Neighbouring fragments remain retrievable from the evidence bundle.
        fragment_limit = SYNTHESIS_PAGE_CHARS - 256  # Reserve space for record/part metadata.
        count = (len(text) + fragment_limit - 1) // fragment_limit
        width = (len(text) + count - 1) // count
        fragments.extend((record_index, text[offset:offset + width])
                         for offset in range(0, len(text), width))
    pages, body, first, last = [], "", 0, 0
    for record_index, fragment in fragments:
        if body and len(body) + len(fragment) > SYNTHESIS_PAGE_CHARS - 256:
            pages.append(f"records={first}-{last}\n" + body)
            body = ""
        if not body:
            first = record_index
        last = record_index
        body += fragment
    if body:
        pages.append(f"records={first}-{last}\n" + body)
    return tuple(pages)


def synthesis_prompt(page: SynthesisPage, manifest: Path) -> str:
    return (
        "유지보수 종합의 증거 페이지를 검토하라. 파일을 수정하지 마라. "
        "한 페이지에는 연속된 여러 JSON 기록 또는 큰 기록의 조각이 포함될 수 있다. "
        "이것은 이미 검증된 파일 검토·관계 결과이며 원본 전체를 다시 읽지 마라. "
        "큰 JSON 기록은 여러 페이지에 걸칠 수 있다. 단일 파일의 구조 문제도 포함해 "
        "관련 기록을 함께 판단하라. 다른 조각의 문맥이 필요하면 아래 증거 manifest와 "
        "같은 record의 인접 페이지 또는 관련 관계만 제한된 크기로 조회하라. "
        "이전 세션의 기억에 의존하지 마라. "
        "최종 계획 및 FINDINGS_RESULT는 모든 페이지를 처리한 다음 요청한다.\n"
        f"evidence_manifest={manifest}\npage_key={page.key}, page={page.index + 1}/{page.total}\n"
        + page.text
        + "\nSYNTHESIS_PAGE_RESULT: 한 줄 JSON {key:page_key, "
        "excerpt:이 페이지의 정확한 12자 이상 인용, has_issue:구조 문제 후보 유무 boolean, "
        "assessment:발견 후보 또는 문제 없음 근거와 앞뒤 페이지의 연결을 설명하는 20~2000자}를 반환하라."
    )


RESOLUTION_PREFIX = "SYNTHESIS_RESOLUTION: "
MAX_RESOLUTION_BYTES = 24_000


@dataclass(frozen=True)
class ResolutionUnit:
    digest: str
    decision_digest: str
    index: int
    assessment: dict
    finding_keys: frozenset[str]
    manifest: Path
    session_id: str | None = None
    error: str | None = None


def resolution_prompt(unit: ResolutionUnit) -> str:
    return (
        "최종 발견에 대한 종합 페이지 하나의 처리 결과를 확인하라. 파일을 수정하지 마라. "
        "다른 세션 기억에 의존하지 말고 필요한 근거와 발견만 manifest의 개별 파일에서 조회하라. "
        "이 페이지의 저장된 판단을 발견에 연결하거나 구체적인 이유로 제외하라. "
        "has_issue=true인 후보를 no_issue로 처리하지 마라.\n"
        f"decision_manifest={unit.manifest}\n"
        + json.dumps(unit.assessment, ensure_ascii=False)
        + "\nSYNTHESIS_RESOLUTION: 한 줄 JSON {key:위 페이지 key, "
        "excerpt:저장된 assessment의 정확한 12자 이상 인용,issue_keys:연결된 발견 식별자 배열(최대 32개), "
        "disposition:linked|excluded|no_issue,reason:연결 또는 제외의 구체적 20~2000자 근거}. "
        "현재 페이지 항목 하나만 응답하라. 전체 페이지 배열을 출력하지 마라. 응답은 24000 bytes 이내다."
        + ("\n이전 결과 오류를 수정하라: " + unit.error[:2000] if unit.error else "")
    )


def _validate_resolution(item: dict, expected: dict, issue_keys: frozenset[str]) -> None:
    if not isinstance(item, dict) or set(item) != {"key", "excerpt", "issue_keys", "reason", "disposition"}:
        raise ReconciliationError("종합 페이지 처리 필드가 다릅니다.")
    if item["key"] != expected["key"]:
        raise ReconciliationError("종합 페이지 처리 identity가 다릅니다.")
    if _note(item["excerpt"], "저장된 판단 인용", 12) not in expected["assessment"]:
        raise ReconciliationError("종합 페이지의 저장된 판단과 다릅니다.")
    keys = item["issue_keys"]
    if (not isinstance(keys, list) or len(keys) > 32
        or any(not isinstance(k, str) or k not in issue_keys for k in keys)):
        raise ReconciliationError("페이지 발견 연결이 다릅니다.")
    disposition = item["disposition"]
    if (not isinstance(disposition, str) or disposition not in {"linked", "excluded", "no_issue"}
        or (disposition == "linked" and not keys) or (disposition != "linked" and keys)
        or (disposition == "no_issue" and expected["has_issue"])):
        raise ReconciliationError("저장된 문제 후보는 발견 연결 또는 명시적인 제외가 필요합니다.")
    _note(item["reason"], "페이지 발견 연결 또는 제외 이유", 20)


class SynthesisRepository:
    """A source/evidence-bound cursor; completion never depends on an in-memory run."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute("""
            CREATE TABLE IF NOT EXISTS provisional_synthesis (
                repo TEXT PRIMARY KEY REFERENCES projects(repo),
                digest TEXT NOT NULL,
                next_page INTEGER NOT NULL,
                session_id TEXT,
                results_json TEXT NOT NULL
            )
        """)

        connection.execute("""CREATE TABLE IF NOT EXISTS provisional_synthesis_resolutions (
            repo TEXT NOT NULL REFERENCES projects(repo), page_index INTEGER NOT NULL,
            digest TEXT NOT NULL, decision_digest TEXT NOT NULL, result_json TEXT,
            session_id TEXT, validation_error TEXT, PRIMARY KEY (repo, page_index))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS provisional_synthesis_history (
            repo TEXT NOT NULL, digest TEXT NOT NULL, snapshot_json TEXT NOT NULL,
            PRIMARY KEY (repo, digest))""")

    def prepare(self, repo: str, cycle: int, revision: str,
                progress: ReconciliationProgress) -> SynthesisPage:
        pages = synthesis_pages(progress)
        digest = hashlib.sha256(json.dumps(
            ["packed-pages-v3", cycle, revision, pages,
             common_guidance.effective_planning_hash(self.connection, repo)], ensure_ascii=False,
        ).encode()).hexdigest()
        with self.connection:
            row = self.connection.execute(
                "SELECT digest, next_page, session_id, results_json FROM provisional_synthesis WHERE repo = ?", (repo,),
            ).fetchone()
            if row is None or row[0] != digest:
                if row is not None:
                    resolutions = self.connection.execute(
                        'SELECT page_index, digest, decision_digest, result_json, session_id, validation_error '
                        'FROM provisional_synthesis_resolutions WHERE repo = ? AND digest = ?',
                        (repo, row[0]),
                    ).fetchall()
                    self.connection.execute(
                        'INSERT OR IGNORE INTO provisional_synthesis_history VALUES (?, ?, ?)',
                        (repo, row[0], json.dumps({'next_page': row[1], 'session_id': row[2],
                            'results_json': row[3], 'resolutions': [tuple(item) for item in resolutions]}, ensure_ascii=False)),
                    )
                self.connection.execute(
                    "INSERT OR REPLACE INTO provisional_synthesis VALUES (?, ?, 0, NULL, '[]')",
                    (repo, digest),
                )
                index, session = 0, None
            else:
                index, session = row[1], row[2]
        return SynthesisPage(digest, index, len(pages), pages[index] if index < len(pages) else "", session)

    def session(self, repo: str, page: SynthesisPage, session_id: str) -> None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE provisional_synthesis SET session_id = ? WHERE repo = ? AND digest = ? AND next_page = ?",
                (session_id, repo, page.digest, page.index),
            )
            if changed.rowcount != 1:
                raise ReconciliationError("종합 페이지의 source가 바뀌었습니다.")

    def complete(self, repo: str, page: SynthesisPage, output: str) -> None:
        value = _single_json(output, SYNTHESIS_PREFIX)
        if set(value) != {"key", "excerpt", "assessment", "has_issue"} or value["key"] != page.key:
            raise ReconciliationError("종합 페이지 identity가 다릅니다.")
        if _note(value["excerpt"], "페이지 근거", 12) not in page.text.partition("\n")[2]:
            raise ReconciliationError("종합 페이지 근거가 다릅니다.")
        _note(value["assessment"], "페이지 판단", 20)
        if type(value["has_issue"]) is not bool:
            raise ReconciliationError("페이지의 문제 후보 유무가 필요합니다.")
        with self.connection:
            row = self.connection.execute(
                "SELECT results_json, session_id FROM provisional_synthesis "
                "WHERE repo = ? AND digest = ? AND next_page = ?", (repo, page.digest, page.index),
            ).fetchone()
            if row is None or not row[1]:
                raise ReconciliationError("종합 페이지의 source 또는 세션이 다릅니다.")
            results = json.loads(row[0])
            results.append(value)
            self.connection.execute(
                "UPDATE provisional_synthesis SET next_page = next_page + 1, session_id = NULL, results_json = ? WHERE repo = ?",
                (json.dumps(results, ensure_ascii=False), repo),
            )


    def results(self, repo: str, page: SynthesisPage) -> list[dict]:
        row = self.connection.execute(
            "SELECT results_json FROM provisional_synthesis WHERE repo = ? AND digest = ? AND next_page = ?",
            (repo, page.digest, page.index),
        ).fetchone()
        if row is None:
            raise ReconciliationError("종합 증거 snapshot이 바뀌었습니다.")
        results = json.loads(row[0])
        if len(results) != page.index:
            raise ReconciliationError("종합 페이지 결과가 누락됐습니다.")
        return results

    def export(self, repo: str, page: SynthesisPage, progress: ReconciliationProgress,
               directory: Path) -> Path:
        """Materialize disposable, lossless input snapshots outside the worktree.

        SQLite remains authoritative; agent edits to exported files never change
        receipts or final coverage checks. Each file is independently retrievable.
        """
        directory = directory / hashlib.sha256(repo.encode()).hexdigest() / page.digest
        directory.mkdir(parents=True, exist_ok=True)
        pages, results = synthesis_pages(progress), self.results(repo, page)
        if len(pages) != page.total:
            raise ReconciliationError("종합 증거 페이지 집합이 다릅니다.")
        manifest = directory / "manifest.json"
        previous = None
        if manifest.exists():
            try:
                previous = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        initialized = (isinstance(previous, dict) and previous.get("digest") == page.digest
                       and previous.get("total") == page.total
                       and type(previous.get("completed")) is int
                       and 0 <= previous["completed"] <= page.index)
        # Evidence is immutable for a digest. Publish it once, then update only
        # newly completed receipts; do not reread every exported file per turn.
        indices = range(previous["completed"], page.index) if initialized else range(len(pages))
        for index in indices:
            record = {"key": f"{page.digest}:{index}", "text": pages[index],
                      "result": results[index] if index < len(results) else None}
            path = directory / f"page-{index:08d}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        manifest.write_text(json.dumps({"digest": page.digest, "total": page.total,
            "completed": page.index, "page_pattern": "page-{index:08d}.json",
            "instructions": "Read individual pages in bounded ranges; text and result are lossless durable evidence."},
            ensure_ascii=False), encoding="utf-8")
        return manifest

    @staticmethod
    def decision_digest(page: SynthesisPage, findings: dict) -> str:
        return hashlib.sha256(json.dumps([page.digest, findings], ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _resolution_rows(self, repo: str, page: SynthesisPage, findings: dict) -> dict:
        return {row[0]: row[1:] for row in self.connection.execute(
            "SELECT page_index, result_json, session_id, validation_error FROM provisional_synthesis_resolutions "
            "WHERE repo = ? AND digest = ? AND decision_digest = ?",
            (repo, page.digest, self.decision_digest(page, findings)),
        )}

    def next_resolution(self, repo: str, page: SynthesisPage, findings: dict,
                        evidence_manifest: Path) -> ResolutionUnit | None:
        results = self.results(repo, page)
        if page.index != page.total:
            raise ReconciliationError("종합 증거 페이지 검토가 완료되지 않았습니다.")
        rows = self._resolution_rows(repo, page, findings)
        decision_digest = self.decision_digest(page, findings)
        keys = frozenset(item["issue_key"] for item in findings["findings"])
        for index, assessment in enumerate(results):
            row = rows.get(index)
            if row and row[0] is not None:
                continue
            directory = evidence_manifest.parent / decision_digest
            manifest = directory / "decision.json"
            if not manifest.exists():
                directory.mkdir(parents=True, exist_ok=True)
                for number, finding in enumerate(findings["findings"]):
                    (directory / f"finding-{number:08d}.json").write_text(
                        json.dumps(finding, ensure_ascii=False), encoding="utf-8")
                manifest.write_text(json.dumps({"digest": page.digest, "decision_digest": decision_digest,
                    "evidence_manifest": str(evidence_manifest), "finding_count": len(findings["findings"]),
                    "finding_pattern": "finding-{index:08d}.json", "no_issue_reason": findings["no_issue_reason"]},
                    ensure_ascii=False), encoding="utf-8")
            if row is None:
                with self.connection:
                    self.connection.execute("INSERT OR REPLACE INTO provisional_synthesis_resolutions "
                        "VALUES (?, ?, ?, ?, NULL, NULL, NULL)", (repo, index, page.digest, decision_digest))
            return ResolutionUnit(page.digest, decision_digest, index, assessment, keys, manifest,
                                  row[1] if row else None, row[2] if row else None)
        return None

    def resolution_session(self, repo: str, unit: ResolutionUnit, session_id: str) -> None:
        self._update_resolution(repo, unit, "session_id = ?", (session_id,))

    def resolution_batch(self, repo: str, unit: ResolutionUnit, count: int) -> tuple[ResolutionUnit, ...]:
        row = self.connection.execute(
            "SELECT results_json FROM provisional_synthesis WHERE repo = ? AND digest = ?",
            (repo, unit.digest),
        ).fetchone()
        if row is None:
            raise ReconciliationError("최종 판단의 종합 근거가 바뀌었습니다.")
        assessments = json.loads(row[0])
        saved = {item[0]: item[1:] for item in self.connection.execute(
            "SELECT page_index, result_json, session_id, validation_error "
            "FROM provisional_synthesis_resolutions WHERE repo = ? AND digest = ? AND decision_digest = ?",
            (repo, unit.digest, unit.decision_digest),
        )}
        units = []
        for index in range(unit.index, len(assessments)):
            previous = saved.get(index)
            if previous and previous[0] is not None:
                continue
            session, error = previous[1:] if previous else (None, None)
            if units and (len(units) >= count or session or error):
                break
            if previous is None:
                with self.connection:
                    self.connection.execute(
                        "INSERT OR REPLACE INTO provisional_synthesis_resolutions VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
                        (repo, index, unit.digest, unit.decision_digest),
                    )
            units.append(ResolutionUnit(unit.digest, unit.decision_digest, index, assessments[index],
                                        unit.finding_keys, unit.manifest, session, error))
            if session or error:
                break
        return tuple(units)

    def resolution_fail(self, repo: str, unit: ResolutionUnit, error: str) -> None:
        self._update_resolution(repo, unit, "validation_error = ?", (error[:2000],))

    def _update_resolution(self, repo: str, unit: ResolutionUnit, assignment: str, values: tuple) -> None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE provisional_synthesis_resolutions SET " + assignment
                + " WHERE repo = ? AND page_index = ? AND digest = ? AND decision_digest = ? AND result_json IS NULL "
                "AND EXISTS (SELECT 1 FROM provisional_synthesis WHERE repo = ? AND digest = ?)",
                (*values, repo, unit.index, unit.digest, unit.decision_digest, repo, unit.digest),
            )
            if changed.rowcount != 1:
                raise ReconciliationError("페이지 처리 결과의 source 또는 최종 판단이 바뀌었습니다.")

    def resolution_complete(self, repo: str, unit: ResolutionUnit, output: str) -> None:
        if len(output.encode()) > MAX_RESOLUTION_BYTES:
            raise ReconciliationError("페이지 처리 응답이 크기 제한을 넘었습니다.")
        value = _single_json(output, RESOLUTION_PREFIX)
        _validate_resolution(value, unit.assessment, unit.finding_keys)
        self._update_resolution(repo, unit, "result_json = ?, session_id = NULL, validation_error = NULL",
                                (json.dumps(value, ensure_ascii=False),))

    def validate_resolutions(self, repo: str, page: SynthesisPage, findings: dict) -> None:
        if page.total == 0:
            return
        results = self.results(repo, page)
        rows = self._resolution_rows(repo, page, findings)
        if page.index != page.total or set(rows) != set(range(page.total)):
            raise ReconciliationError("모든 종합 페이지의 저장된 판단 처리가 필요합니다.")
        keys = frozenset(item["issue_key"] for item in findings["findings"])
        for index, expected in enumerate(results):
            if rows[index][0] is None:
                raise ReconciliationError("미확인 페이지 처리 결과가 남았습니다.")
            _validate_resolution(json.loads(rows[index][0]), expected, keys)
