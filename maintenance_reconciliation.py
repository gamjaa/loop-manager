"""Source-bound, resumable cross-file maintenance review and findings."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence

from maintenance_file_inventory import SourceInventory, TrackedSource, tracked_source_inventory
from maintenance_file_review import FileReviewError, FileReviewProgress, source_bytes


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
    # A missing map cannot prove the absence of reverse references. Search only
    # context files without a current map, using selected public names/paths.
    for path, content in context_contents.items():
        if path in maps or path not in sources:
            continue
        for item in progress.completed:
            target = item["path"]
            needles = (target, target.rsplit("/", 1)[-1],
                       *item["code_map"]["public_symbols"])
            for needle in needles:
                if len(needle) >= 4 and needle in content:
                    add(path, target, "unmapped_reverse", needle)
            for ref in item["pending_references"]:
                if ref["target_path"] is None and ref["symbol"] and ref["symbol"] in content:
                    add(target, path, "unmapped_symbol_target", ref["evidence"])
    return tuple(
        Relation(_key(*pair), sources[pair[0]], sources[pair[1]],
                 tuple(sorted(values, key=lambda value: json.dumps(value, sort_keys=True))))
        for pair, values in sorted(signals.items())
    )


def parse_relation(output: str, relation: Relation, left: bytes, right: bytes) -> dict:
    value = _single_json(output, RELATION_PREFIX)
    if set(value) != {"key", "status", "left_excerpt", "right_excerpt", "reason"}:
        raise ReconciliationError("관계 확인 결과 필드가 유효하지 않습니다.")
    if value["key"] != relation.key or value["status"] not in {"confirmed", "excluded"}:
        raise ReconciliationError("관계 identity 또는 처리 상태가 다릅니다.")
    for field, content in (("left_excerpt", left), ("right_excerpt", right)):
        excerpt = _note(value[field], field, minimum=4)
        if excerpt not in content.decode("utf-8", "backslashreplace"):
            raise ReconciliationError(f"{field}가 현재 원본에 없습니다.")
    value["reason"] = _note(value["reason"], "관계 확인·제외 근거", minimum=20)
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
        return result

    def progress(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 inventory: SourceInventory, reviews: FileReviewProgress) -> ReconciliationProgress:
        if tracked_source_inventory(repository, base, inventory.revision) != inventory:
            raise ReconciliationError("관계 종합 source가 현재 원격과 다릅니다.")
        maps = self.valid_maps(repo, inventory)
        context = {}
        selected = {item.path for item in reviews.selected}
        for source in inventory.sources:
            if source.path not in selected and source.path not in maps:
                context[source.path] = source_bytes(repository, base, source).decode("utf-8", "backslashreplace")
        relations = relation_candidates(inventory, reviews, maps, context)
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
                        completed[key] = parse_relation(
                            RELATION_PREFIX + raw_result, relation,
                            source_bytes(repository, base, relation.left),
                            source_bytes(repository, base, relation.right),
                        )
                    except ReconciliationError:
                        self.connection.execute("DELETE FROM provisional_relations WHERE repo = ? AND relation_key = ?", (repo, key))
        next_relation = next((item for item in relations if item.key not in completed), None)
        return ReconciliationProgress(reviews.selected, tuple(completed[key] for key in sorted(completed)),
                                      relations, next_relation,
                                      _excluded_candidates(inventory, reviews, maps), reviews.completed)

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


def parse_findings(output: str, progress: ReconciliationProgress,
                   inventory: SourceInventory, contents: Mapping[str, bytes],
                   existing_plans: Mapping[str, str], new_plan_ids: Sequence[str],
                   plan_texts: Mapping[str, str] | None = None) -> dict:
    if progress.next_relation is not None or len(progress.completed) != len(progress.relations):
        raise ReconciliationError("미확인 파일 간 관계가 남았습니다.")
    value = _single_json(output, FINDINGS_PREFIX)
    if set(value) != {"source_revision", "findings", "no_issue_reason"} or value["source_revision"] != inventory.revision:
        raise ReconciliationError("발견 결과의 source 또는 필드가 다릅니다.")
    findings = value["findings"]
    if not isinstance(findings, list):
        raise ReconciliationError("발견은 배열이어야 합니다.")
    if not findings:
        _validate_no_issue(value["no_issue_reason"], progress)
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
            or not set(targets) <= sources.keys() or not set(targets) & selected):
            raise ReconciliationError("발견 대상은 선정된 현재 원본을 포함해야 합니다.")
        evidence = finding["evidence"]
        if not isinstance(evidence, list) or set(targets) != {item.get("path") for item in evidence if isinstance(item, dict)}:
            raise ReconciliationError("모든 발견 대상의 코드 근거가 필요합니다.")
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"path", "content_hash", "mode", "excerpt"}:
                raise ReconciliationError("발견 근거의 source identity가 필요합니다.")
            source = sources[item["path"]]
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
        status, plan_id = finding["status"], finding["plan_id"]
        if (not isinstance(plan_id, str)
            or (status == "existing_plan" and (plan_id not in existing_plans or existing_plans[plan_id] == "MERGED"))
            or (status == "new_plan" and plan_id not in new_plan_ids)
            or status not in {"existing_plan", "new_plan"}):
            raise ReconciliationError("발견의 비종료 또는 새 계획 연결이 유효하지 않습니다.")
        _validate_plan_comparisons(finding, existing_plans, plan_texts or {})
    if {item["plan_id"] for item in findings if item["status"] == "new_plan"} != set(new_plan_ids):
        raise ReconciliationError("새 리팩터링 계획마다 근거 있는 발견 연결이 필요합니다.")
    return value
