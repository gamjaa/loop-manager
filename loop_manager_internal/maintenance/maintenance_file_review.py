"""Source-bound, resumable provisional file reviews for maintenance planning.

Each unit reviews one selected file. These rows are deliberately separate from
the successful file ledger; relationship synthesis and promotion belong to a
later assessment stage.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from loop_manager_internal.agents import common_guidance
import json
from pathlib import Path
import re
import shlex
import sqlite3
from typing import Sequence

from loop_manager_internal.maintenance.file_review_protocol import (
    CHUNK_FIELDS, CODE_MAP_FIELDS, EVIDENCE_FIELDS, EVIDENCE_TEXT_FIELDS,
    FILE_RESULT_FIELDS, ROOT_RESULT_FIELDS, REFERENCE_FIELDS, SIMILAR_FIELDS,
    FileReviewError, MAX_FIELD_CHARS, MAX_LIST_ITEMS, MIN_CHUNK_REVIEW_CHARS,
    MIN_DESCRIPTION_CHARS, MIN_EXCERPT_CHARS, RESULT_PREFIX,
    MIN_REVIEW_CITATION_CHARS,
    decode_result, require_fields, result_instructions, review_string as _string,
)
from loop_manager_internal.maintenance.file_review_evidence import (
    review_cites_source, unverified_symbols,
)
from loop_manager_internal.maintenance import compact_file_review
from loop_manager_internal.maintenance.maintenance_file_inventory import (
    RootPlacement, SourceInventory, SourceInventoryError, TrackedSource, _git, cached_source_inventory,
    root_placement_inventory,
    verify_remote_revision,
)
from loop_manager_internal.storage.maintenance_file_ledger import (
    FileReviewRepository, FileReviewLedgerError, ROOT_REVIEW_PATH, ROOT_REVIEW_REASON,
    root_review_source, validate_root_reviews,
)


MAX_EMBEDDED_SOURCE_BYTES = 48_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS provisional_file_reviews (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    cycle_revision INTEGER NOT NULL,
    source_revision TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mode TEXT NOT NULL,
    session_id TEXT,
    validation_error TEXT,
    result_json TEXT,
    PRIMARY KEY (repo, path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA)


@dataclass(frozen=True)
class ReviewUnit:
    source: TrackedSource
    session_id: str | None
    error: str | None


@dataclass(frozen=True)
class FileReviewProgress:
    selected: tuple[TrackedSource, ...]
    completed: tuple[dict, ...]
    next_unit: ReviewUnit | None
    root_placements: tuple[RootPlacement, ...] = ()
    root_reviews: tuple[dict, ...] = ()
    source_paths: tuple[str, ...] = ()


def _strings(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise FileReviewError(f"{label}에는 문자열 배열이 필요합니다.")
    if len(value) > MAX_LIST_ITEMS or (required and not value):
        raise FileReviewError(
            f"{label} 배열 길이가 유효하지 않습니다: {len(value)}개, "
            f"허용 {int(required)}~{MAX_LIST_ITEMS}개.",
        )
    strings = tuple(_string(item, label) for item in value)
    if len(strings) != len(set(strings)):
        raise FileReviewError(f"{label}에 중복 값이 있습니다.")
    return strings


def _path(value: object, label: str) -> str:
    path = _string(value, label)
    if (path.startswith("/") or "\\" in path or "\n" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise FileReviewError(f"{label}은 상대 경로여야 합니다.")
    return path


def _references(value: object, label: str, text: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise FileReviewError(f"{label}에는 구조화된 참조 배열이 필요합니다.")
    normalized = []
    keys = set()
    for item in value:
        require_fields(item, REFERENCE_FIELDS, label)
        path = _path(item["target_path"], label) if item["target_path"] is not None else None
        symbol = _string(item["symbol"], label) if item["symbol"] is not None else None
        excerpt = _string(item["evidence"], label)
        if (not path and not symbol) or excerpt not in text:
            raise FileReviewError(f"{label}에 확인할 대상 또는 원본 인용이 없습니다.")
        key = (path, symbol, excerpt)
        if key in keys:
            raise FileReviewError(f"{label}에 중복 후보가 있습니다.")
        keys.add(key)
        normalized.append({"target_path": path, "symbol": symbol, "evidence": excerpt})
    return sorted(normalized, key=lambda item: (
        item["target_path"] or "", item["symbol"] or "", item["evidence"],
    ))


def _similar_candidates(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise FileReviewError("유사 책임 후보에는 구조화된 배열이 필요합니다.")
    normalized = []
    seen = set()
    for item in value:
        require_fields(item, SIMILAR_FIELDS, "similar_responsibilities 항목")
        path = _path(item["target_path"], "유사 책임 경로")
        responsibility = _string(item["responsibility"], "유사 책임")
        reason = _string(item["reason"], "유사 책임 후보 근거", minimum=MIN_DESCRIPTION_CHARS)
        key = (path, responsibility)
        if key in seen:
            raise FileReviewError("유사 책임 후보가 중복됐습니다.")
        seen.add(key)
        normalized.append({"target_path": path, "responsibility": responsibility,
                           "reason": reason})
    return sorted(normalized, key=lambda item: (item["target_path"], item["responsibility"]))


def source_bytes(repository: Path, base: str, source: TrackedSource) -> bytes:
    """Read one exact Git blob and reject a moved ref or forged inventory row."""
    verify_remote_revision(repository, base, source.revision)
    if source.reason == ROOT_REVIEW_REASON:
        content = _git(repository, "ls-tree", source.revision)
        if source.path != ROOT_REVIEW_PATH or source.mode != "040000" or source.content_hash != hashlib.sha256(content).hexdigest():
            raise FileReviewError("루트 조사 source identity가 변경됐습니다.")
        verify_remote_revision(repository, base, source.revision)
        return content
    if not source.path or source.path.startswith("/") or ".." in source.path.split("/"):
        raise FileReviewError("검토 경로가 유효하지 않습니다.")
    entries = _git(repository, "ls-tree", "-z", source.revision, "--", source.path)
    try:
        metadata, path = entries.rstrip(b"\0").split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
    except ValueError as error:
        raise FileReviewError("선정된 파일을 source tree에서 찾을 수 없습니다.") from error
    if (path.decode("utf-8") != source.path or kind != b"blob"
        or mode.decode("ascii") != source.mode
        or re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid) is None):
        raise FileReviewError("검토 파일의 경로 또는 mode가 source tree와 다릅니다.")
    content = _git(repository, "cat-file", "blob", oid.decode("ascii"))
    if hashlib.sha256(content).hexdigest() != source.content_hash:
        raise FileReviewError("검토 파일의 내용 지문이 source tree와 다릅니다.")
    verify_remote_revision(repository, base, source.revision)
    return content


def _source_text(content: bytes) -> str:
    """Render arbitrary NUL-free source bytes without losing their identity."""
    return content.decode("utf-8", "backslashreplace")


def _chunk_spans(content: bytes) -> tuple[tuple[int, int], ...]:
    return tuple(
        (start, min(start + MAX_EMBEDDED_SOURCE_BYTES, len(content)))
        for start in range(0, len(content), MAX_EMBEDDED_SOURCE_BYTES)
    )


def _validate_chunk_reviews(value: object, content: bytes) -> list[dict]:
    """Require separately grounded evidence for every deterministic byte span."""
    spans = _chunk_spans(content)
    if not isinstance(value, list) or len(value) != len(spans):
        raise FileReviewError("원본의 모든 검토 구간에 대한 근거가 필요합니다.")
    normalized = []
    for index, ((start, end), item) in enumerate(zip(spans, value)):
        require_fields(item, CHUNK_FIELDS, f"evidence.chunk_reviews[{index}]")
        if (type(item["index"]) is not int or item["index"] != index
            or type(item["start"]) is not int or item["start"] != start
            or type(item["end"]) is not int or item["end"] != end):
            raise FileReviewError("검토 구간이 누락되거나 source byte 범위와 다릅니다.")
        rendered = _source_text(content[start:end])
        middle_start = max(0, len(rendered) // 2 - 128)
        middle_end = min(len(rendered), len(rendered) // 2 + 128)
        opening = item["opening_excerpt"]
        middle = item["middle_excerpt"]
        closing = item["closing_excerpt"]
        if (item["chunk_hash"] != hashlib.sha256(content[start:end]).hexdigest()
            or not isinstance(opening, str) or not opening or len(opening) > MAX_FIELD_CHARS
            or not isinstance(middle, str) or not middle or len(middle) > MAX_FIELD_CHARS
            or not isinstance(closing, str) or not closing or len(closing) > MAX_FIELD_CHARS
            or opening not in rendered[:256]
            or middle not in rendered[middle_start:middle_end]
            or closing not in rendered[-256:]):
            raise FileReviewError("검토 구간의 지문 또는 시작·중간·끝 인용이 원본과 다릅니다.")
        note = _string(item["review"], f"evidence.chunk_reviews[{index}].review",
                       minimum=MIN_CHUNK_REVIEW_CHARS)
        if not review_cites_source(note, (opening, middle, closing)):
            raise FileReviewError(
                f"evidence.chunk_reviews[{index}].review의 검토 근거가 원본 인용과 연결되지 않았습니다. "
                f"세 excerpt 중 하나에 포함된 {MIN_REVIEW_CITATION_CHARS}자 이상의 연속 원본문자열을 "
                "review에 그대로 인용하세요 (더 짧은 인용은 전체 사용).",
            )
        normalized.append({
            "index": index, "start": start, "end": end,
            "chunk_hash": item["chunk_hash"], "opening_excerpt": opening,
            "middle_excerpt": middle, "closing_excerpt": closing, "review": note,
        })
    return normalized


def parse_review(output: str, source: TrackedSource, content: bytes, *,
                 source_paths: tuple[str, ...] = ()) -> dict:
    """Validate a single Agent result against content actually read from Git."""
    # Bound individual fields and lists below, not the serialized document:
    # mandatory chunk evidence grows with the source and JSON escaping varies.
    result = decode_result(output)
    if source.reason != ROOT_REVIEW_REASON and "version" in result:
        result = compact_file_review.expand_result(
            result, source, content, source_paths, chunk_bytes=MAX_EMBEDDED_SOURCE_BYTES,
        )
    require_fields(result, ROOT_RESULT_FIELDS if source.reason == ROOT_REVIEW_REASON
                   else FILE_RESULT_FIELDS, "FILE_REVIEW_RESULT")
    if source.reason == ROOT_REVIEW_REASON:
        if any(result[key] != getattr(source, key if key != "source_revision" else "revision")
                 for key in ("path", "content_hash", "mode", "source_revision")):
            raise FileReviewError("루트 조사 결과 identity 또는 계약이 다릅니다.")
        if hashlib.sha256(content).hexdigest() != source.content_hash or not isinstance(result["root_reviews"], list):
            raise FileReviewError("루트 조사 원본 또는 결과가 유효하지 않습니다.")
        return result
    if tuple(result[key] for key in ("path", "content_hash", "mode", "source_revision")) != (
        source.path, source.content_hash, source.mode, source.revision,
    ):
        raise FileReviewError("검토 결과의 source identity가 선정 파일과 다릅니다.")
    text = _source_text(content)
    evidence = result["evidence"]
    require_fields(evidence, EVIDENCE_FIELDS, "evidence")
    excerpts = _strings(evidence["excerpts"], "코드 인용", required=bool(text.strip()))
    if (not text.strip() and excerpts) or any(excerpt not in text for excerpt in excerpts):
        raise FileReviewError("빈 원본 또는 원본에 없는 코드 인용은 검토로 인정할 수 없습니다.")
    if (len(text.strip()) >= MIN_EXCERPT_CHARS
        and all(len(excerpt) < MIN_EXCERPT_CHARS for excerpt in excerpts)):
        raise FileReviewError("코드 인용이 실제 검토 근거로 사용하기에 너무 짧습니다.")
    evidence["chunk_reviews"] = _validate_chunk_reviews(
        evidence["chunk_reviews"], content,
    )
    for key in EVIDENCE_TEXT_FIELDS:
        _string(evidence[key], f"evidence.{key}", minimum=MIN_DESCRIPTION_CHARS)
    result["code_map"] = validate_code_map(result["code_map"], source, content)
    result["pending_references"] = _references(
        result["pending_references"], "미확인 직접 참조", text,
    )
    result["similar_responsibilities"] = _similar_candidates(
        result["similar_responsibilities"],
    )
    return result


def validate_code_map(code_map: object, source: TrackedSource, content: bytes) -> dict:
    """Validate map context independently; this never marks a file reviewed."""
    text = _source_text(content)
    require_fields(code_map, CODE_MAP_FIELDS, "code_map")
    _string(code_map["responsibility"], "code_map.responsibility", minimum=MIN_DESCRIPTION_CHARS)
    for key in ("entry_points", "public_symbols"):
        symbols = _strings(code_map[key], key)
        missing = unverified_symbols(symbols, source.path, content, text)
        if missing:
            raise FileReviewError(
                f"code_map.{key}에 원본에서 확인되지 않은 심벌이 있습니다: "
                + json.dumps(missing, ensure_ascii=False)
                + ". 실제 원본의 심벌 또는 AST로 확인되는 Class.method를 사용하세요.",
            )
    code_map["related_tests"] = [
        _path(path, "관련 테스트 경로")
        for path in _strings(code_map["related_tests"], "related_tests")
    ]
    code_map["direct_references"] = _references(
        code_map["direct_references"], "직접 참조", text,
    )
    return code_map


def review_prompt(
    source: TrackedSource, content: bytes, *,
    root_placements: tuple[RootPlacement, ...] = (),
    compact: bool = False, source_paths: tuple[str, ...] = (),
    include_contract: bool = True,
) -> str:
    """Give the Agent the exact blob for a bounded, single-file review unit."""
    if source.reason == ROOT_REVIEW_REASON:
        return (
            "당신은 유지보수 Planner다. 파일이나 문서를 수정하지 마라. "
            "이 정상 평가에서 모든 루트 항목의 실제 참조·사용자 경로·소유권·안전한 목적지를 조사하라. "
            "Git tree 목록은 inventory이며 조사 완료 근거가 아니다. "
            "source_revision의 원본을 git show REVISION:PATH로 확인하고 실제 사용 근거를 인용하라. "
            "확인할 수 없는 항목은 category=uncertain, destination=null과 구체적인 판단 근거를 남겨라. "
            "도구 필수 루트는 category=tool_required, destination=null로 보존하라.\n"
            "source identity: " + json.dumps({"path": source.path, "content_hash": source.content_hash,
                "mode": source.mode, "source_revision": source.revision}, ensure_ascii=False) + "\n"
            "revision-bound root placement inventory: " + json.dumps(
                [item.__dict__ for item in root_placements], ensure_ascii=False) + "\n"
            "Git tree:\n" + _source_text(content) + "\n"
            "binary 등 참조를 증명하지 못한 항목은 uncertain으로 남기고 references에 "
            "path=., excerpt=Git tree의 정확한 행을 사용할 수 있다. 필수 도구 경로도 tree 참조를 허용한다.\n"
            + result_instructions(root=True)
        )
    text = _source_text(content)
    source_location = json.dumps({
        "source_revision": source.revision, "path": source.path,
        "content_hash": source.content_hash, "mode": source.mode,
        "bytes": len(content),
        "chunk_bytes": MAX_EMBEDDED_SOURCE_BYTES,
        "chunk_count": len(_chunk_spans(content)),
    }, ensure_ascii=False)
    if len(content) <= MAX_EMBEDDED_SOURCE_BYTES:
        source_instruction = "원본 시작\n" + text + "\n원본 끝\n"
    else:
        spec = shlex.quote(f"{source.revision}:{source.path}")
        source_instruction = (
            "이 파일은 prompt에 모두 넣기에는 크다. INDEX를 0부터 "
            f"{len(_chunk_spans(content)) - 1}까지 바꾸어 각 Git blob 구간을 "
            "작은 연속 구간으로 끝까지 읽어라. 비 UTF-8 byte는 \\xNN으로 표시된다. "
            "미확인 구간이 남으면 완료 JSON을 만들지 마라.\n"
            "python3 -c 'import subprocess,sys; "
            "b=subprocess.check_output([\"git\",\"show\",sys.argv[1]]); "
            f"i=int(sys.argv[2]); n={MAX_EMBEDDED_SOURCE_BYTES}; "
            "print(b[i*n:(i+1)*n].decode(\"utf-8\",\"backslashreplace\"),end=\"\")' "
            f"{spec} INDEX\n"
        )
    if compact:
        if not text.strip():
            source_instruction += (
                "검증된 원본은 0 byte이거나 공백만 포함한다. summary에는 실행 코드가 없는 "
                "사실과 경로에서 확인할 수 있는 역할을 설명하라. 심벌·직접 참조를 만들어내지 마라. "
                "0 byte이면 chunks는 []이며 공백 byte가 있으면 모든 구간의 공백 인용과 검토 근거가 필요하다.\n"
            )
        facts = compact_file_review.source_facts(
            source, content, source_paths, chunk_bytes=MAX_EMBEDDED_SOURCE_BYTES,
        )
        common = (
            "당신은 유지보수 Planner다. 각 Git blob 전체를 실제로 읽고 파일이나 문서를 수정하지 마라. "
            "길이만으로 개선을 강제하지 말고 읽기 쉬움, 책임과 외부 연동, 오류 복구, 테스트 경계, "
            "중복 및 후속 작업의 관련 없는 문맥 감소를 모두 평가하라. 직접 참조·유사 책임 중 "
            "나중에 확인할 후보만 pending·similar에 남기고 문맥 파일은 완료 대상으로 세지 마라.\n"
            "루트 항목의 실제 참조·사용자 경로·소유권·안전한 목적지 조사는 별도 root 조사 단위에서 기록한다. "
            "이 일반 파일 검토에서 root inventory는 문맥이며 root 전용 결과 필드를 추가하지 마라. "
            "근거가 불명확하면 이동·삭제 후보로 만들지 마라.\n"
            "revision-bound root placement inventory: " + json.dumps(
                [item.__dict__ for item in root_placements], ensure_ascii=False) + "\n"
            + compact_file_review.result_instructions()
        ) if include_contract else "이 단위에도 앞의 compact 검토 계약을 적용하라.\n"
        return (common + f"source identity: {source_location}\n"
                + "Manager source facts (배열 후보의 index는 0부터): "
                + json.dumps(facts, ensure_ascii=False, separators=(",", ":")) + "\n"
                + source_instruction)
    if not text.strip():
        source_instruction += (
            "검증된 원본은 0 byte이거나 공백만 포함한다. excerpts는 []로 기록하고 "
            "각 평가 항목과 code_map.responsibility에 실행 코드가 없는 사실과 "
            "경로에서 확인할 수 있는 역할을 설명하라. 심벌·직접 참조를 만들어내지 마라. "
            "0 byte이면 chunk_reviews도 []이며, 공백 byte가 있으면 모든 구간의 "
            "지문과 공백 인용 및 검토 근거를 기록하라.\n"
        )
    root_inventory = json.dumps(
        [item.__dict__ for item in root_placements], ensure_ascii=False,
    )
    return (
        "당신은 유지보수 Planner다. 아래 Git blob 원본 파일 하나를 실제로 읽고 검토하라. "
        "파일이나 문서를 수정하지 마라. 길이만으로 개선을 강제하지 말고, 읽기 쉬움, "
        "책임과 외부 연동, 오류 복구, 테스트 경계, 중복 및 후속 작업의 관련 없는 문맥 "
        "감소를 각각 구체적으로 평가하라. 현재 원본에서 확인한 직접 참조·유사 책임 중 "
        "나중에 확인할 후보만 pending에 남겨라. 문맥 파일은 검토 완료 대상으로 세지 마라.\n"
        f"source identity: {source_location}\n"
        "revision-bound root placement inventory: " + root_inventory + "\n"
        "루트 항목의 실제 참조·사용자 경로·소유권·안전한 목적지 조사는 별도 root 조사 단위에서 기록한다. "
        "이 일반 파일 검토에서 root inventory는 문맥이며 root 전용 결과 필드를 추가하지 마라. "
        "현재 source와 직접 연결되는 근거는 기존 evidence, code_map과 후보 배열에 기록하라. "
        "근거가 불명확하면 이동·삭제 후보로 만들지 마라.\n"
        + source_instruction +
        "각 구간의 byte 범위는 index*chunk_bytes부터 다음 경계 또는 원본 끝까지이며 "
        "chunk_hash는 해당 byte 구간 SHA256이다. opening_excerpt는 구간의 처음 256자, "
        "middle_excerpt는 구간의 중간을 중심으로 앞뒤 128자, closing_excerpt는 마지막 256자에서 "
        "각각 인용하라. review에는 이 인용 중 하나를 직접 언급하는 구체적인 근거가 필요하다.\n"
        + result_instructions()
    )


class ProvisionalReviewRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(provisional_file_reviews)')}
        for field in ('guidance_hash', 'prior_guidance_results'):
            if field not in columns:
                connection.execute(f'ALTER TABLE provisional_file_reviews ADD COLUMN {field} TEXT')
        connection.execute("""CREATE TABLE IF NOT EXISTS provisional_file_review_guidance_history (
            repo TEXT NOT NULL, path TEXT NOT NULL, record_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, PRIMARY KEY (repo, path, record_hash))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS provisional_review_receipts (
            repo TEXT NOT NULL, path TEXT NOT NULL, identity TEXT NOT NULL,
            PRIMARY KEY (repo, path))""")

    def progress(
        self, repo: str, repository: Path, base: str,
        cycle_revision: int, inventory: SourceInventory,
    ) -> FileReviewProgress:
        """Reconcile every saved unit with the current selected source set."""
        if cached_source_inventory(self.connection, repository, base, inventory.revision) != inventory:
            raise FileReviewError("잠정 검토 inventory가 원격 source와 다릅니다.")
        ledger = FileReviewRepository(self.connection)
        selected = ledger.selected_sources(repo, inventory)
        reusable_roots = ledger.reusable_root_reviews(repo, inventory)
        root_source = root_review_source(repository, inventory) if reusable_roots is None else None
        expected = {item.path: item for item in selected}
        if root_source is not None:
            expected[root_source.path] = root_source
        with self.connection:
            guidance_hash = common_guidance.effective_planning_hash(self.connection, repo)
            for path, saved_hash, session, error, raw, prior, cycle, revision, source_hash, mode in self.connection.execute(
                'SELECT path, guidance_hash, session_id, validation_error, result_json, prior_guidance_results, '
                'cycle_revision, source_revision, content_hash, mode '
                'FROM provisional_file_reviews WHERE repo = ?', (repo,),
            ).fetchall():
                if saved_hash != guidance_hash:
                    snapshot = json.dumps({'guidance_hash': saved_hash, 'cycle_revision': cycle,
                        'source_revision': revision, 'content_hash': source_hash, 'mode': mode,
                        'session_id': session, 'validation_error': error, 'result_json': raw},
                        ensure_ascii=False, sort_keys=True)
                    self.connection.execute(
                        'INSERT OR IGNORE INTO provisional_file_review_guidance_history VALUES (?, ?, ?, ?)',
                        (repo, path, hashlib.sha256(snapshot.encode()).hexdigest(), snapshot),
                    )
                    history = json.loads(prior or '[]')
                    history.append({'guidance_hash': saved_hash, 'session_id': session,
                                    'validation_error': error, 'result_json': raw})
                    self.connection.execute(
                        'UPDATE provisional_file_reviews SET guidance_hash = ?, prior_guidance_results = ?, '
                        'result_json = NULL, session_id = NULL WHERE repo = ? AND path = ?',
                        (guidance_hash, json.dumps(history, ensure_ascii=False), repo, path),
                    )
            rows = self.connection.execute(
                "SELECT path, cycle_revision, source_revision, content_hash, mode, "
                "session_id, validation_error, result_json "
                "FROM provisional_file_reviews WHERE repo = ?",
                (repo,),
            ).fetchall()
            valid: dict[str, tuple[str | None, str | None, dict | None]] = {}
            for path, cycle, revision, digest, mode, session_id, error, raw in rows:
                source = expected.get(str(path))
                if source is not None and (cycle != cycle_revision or revision != source.revision):
                    try:
                        same_source = (
                            digest == source.content_hash and mode == source.mode
                            and _git(repository, "merge-base", str(revision), source.revision).strip()
                            == str(revision).encode("ascii")
                        )
                        if same_source and raw is not None:
                            preserved = json.loads(raw)
                            if (not isinstance(preserved, dict)
                                    or preserved.get("source_revision") != revision):
                                same_source = False
                            else:
                                preserved["source_revision"] = source.revision
                                raw = json.dumps(preserved, ensure_ascii=False, sort_keys=True)
                                parsed = parse_review(
                                    RESULT_PREFIX + raw, source,
                                    source_bytes(repository, base, source),
                                )
                                if source.reason == ROOT_REVIEW_REASON:
                                    validate_root_reviews(repository, inventory, parsed["root_reviews"])
                        if same_source:
                            self.connection.execute(
                                "UPDATE provisional_file_reviews SET cycle_revision = ?, "
                                "source_revision = ?, result_json = ? "
                                "WHERE repo = ? AND path = ?",
                                (cycle_revision, source.revision, raw, repo, path),
                            )
                            cycle, revision = cycle_revision, source.revision
                    except (SourceInventoryError, FileReviewError, FileReviewLedgerError,
                            ValueError, UnicodeError):
                        pass
                if (source is None or cycle != cycle_revision or revision != source.revision
                    or digest != source.content_hash or mode != source.mode):
                    self.connection.execute(
                        "DELETE FROM provisional_file_reviews WHERE repo = ? AND path = ?",
                        (repo, path),
                    )
                    continue
                try:
                    result = None
                    if raw is not None:
                        identity = hashlib.sha256(json.dumps(
                            [cycle_revision, source.__dict__, raw], sort_keys=True,
                        ).encode()).hexdigest()
                        receipt = self.connection.execute(
                            "SELECT identity FROM provisional_review_receipts WHERE repo = ? AND path = ?",
                            (repo, path),
                        ).fetchone()
                        if receipt and receipt[0] == identity:
                            result = json.loads(raw)
                        else:
                            result = parse_review(RESULT_PREFIX + str(raw), source,
                                                  source_bytes(repository, base, source))
                            if source.reason == ROOT_REVIEW_REASON:
                                validate_root_reviews(repository, inventory, result["root_reviews"])
                            self.connection.execute("INSERT OR REPLACE INTO provisional_review_receipts VALUES (?, ?, ?)",
                                                    (repo, path, identity))
                except (FileReviewError, FileReviewLedgerError, ValueError):
                    self.connection.execute(
                        "DELETE FROM provisional_file_reviews WHERE repo = ? AND path = ?",
                        (repo, path),
                    )
                    continue
                valid[str(path)] = (str(session_id) if session_id else None,
                                    str(error) if error else None, result)
            completed = tuple(valid[item.path][2] for item in selected
                              if item.path in valid and valid[item.path][2] is not None)
            next_source = next((item for item in selected
                                if item.path not in valid or valid[item.path][2] is None), None)
            roots = reusable_roots or ()
            if root_source is not None:
                root_result = valid.get(root_source.path, (None, None, None))[2]
                if root_result is not None:
                    roots = tuple(root_result["root_reviews"])
                elif next_source is None:
                    next_source = root_source
            unit = (ReviewUnit(next_source, *valid.get(next_source.path, (None, None, None))[:2])
                    if next_source else None)
        return FileReviewProgress(
            selected, completed, unit, root_placement_inventory(inventory), roots,
            tuple(source.path for source in inventory.sources),
        )

    def pending_units(self, repo: str, progress: FileReviewProgress) -> tuple[ReviewUnit, ...]:
        """Return individually resumable units after progress reconciled identities."""
        completed = {item["path"] for item in progress.completed}
        saved = {path: (session, error) for path, session, error in self.connection.execute(
            "SELECT path, session_id, validation_error FROM provisional_file_reviews WHERE repo = ?",
            (repo,),
        )}
        units = tuple(ReviewUnit(source, *saved.get(source.path, (None, None)))
                      for source in progress.selected if source.path not in completed)
        if not units and progress.next_unit is not None:
            return (progress.next_unit,)
        return units

    def start(self, repo: str, cycle_revision: int, source: TrackedSource) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO provisional_file_reviews "
                "(repo, path, cycle_revision, source_revision, content_hash, mode, guidance_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo, source.path, cycle_revision, source.revision,
                 source.content_hash, source.mode, common_guidance.effective_planning_hash(self.connection, repo)),
            )

    def session(self, repo: str, cycle_revision: int, source: TrackedSource, session_id: str) -> None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE provisional_file_reviews SET session_id = ? "
                "WHERE repo = ? AND path = ? AND cycle_revision = ? "
                "AND source_revision = ? AND content_hash = ? "
                "AND mode = ? AND result_json IS NULL",
                (session_id, repo, source.path, cycle_revision,
                 source.revision, source.content_hash, source.mode),
            )
            if changed.rowcount != 1:
                raise FileReviewError("검토 세션의 source identity가 변경됐습니다.")

    def complete(self, repo: str, cycle_revision: int, source: TrackedSource, result: dict) -> None:
        if source.reason == ROOT_REVIEW_REASON:
            try:
                base = self.connection.execute("SELECT base_branch FROM projects WHERE repo = ?", (repo,)).fetchone()[0]
                inventory = cached_source_inventory(self.connection, Path(repo), base, source.revision)
                validate_root_reviews(Path(repo), inventory, result.get("root_reviews"))
            except FileReviewLedgerError as error:
                raise FileReviewError(str(error)) from error
        with self.connection:
            changed = self.connection.execute(
                "UPDATE provisional_file_reviews SET result_json = ?, session_id = NULL, "
                "validation_error = NULL "
                "WHERE repo = ? AND path = ? AND cycle_revision = ? "
                "AND source_revision = ? AND content_hash = ? "
                "AND mode = ? AND result_json IS NULL",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), repo,
                 source.path, cycle_revision, source.revision,
                 source.content_hash, source.mode),
            )
            if changed.rowcount != 1:
                raise FileReviewError("잠정 검토 저장 전에 source identity가 변경됐습니다.")

    def fail(self, repo: str, cycle_revision: int, source: TrackedSource, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE provisional_file_reviews SET validation_error = ? "
                "WHERE repo = ? AND path = ? AND cycle_revision = ? "
                "AND source_revision = ? AND result_json IS NULL",
                (error[:2000], repo, source.path, cycle_revision, source.revision),
            )

    def completed_summary(self, repo: str) -> dict:
        """Bound legacy Planner context after progress has validated all rows."""
        rows = self.connection.execute(
            "SELECT result_json FROM provisional_file_reviews "
            "WHERE repo = ? AND result_json IS NOT NULL ORDER BY path LIMIT 13",
            (repo,),
        ).fetchall()
        preview = []
        for row in rows[:12]:
            result = json.loads(str(row[0]))
            if result["path"] == ROOT_REVIEW_PATH:
                continue
            code_map = result["code_map"]
            preview.append({
                "path": result["path"], "content_hash": result["content_hash"],
                "mode": result["mode"], "source_revision": result["source_revision"],
                "code_map": {
                    "responsibility": code_map["responsibility"],
                    **{key: code_map[key][:8] for key in (
                        "entry_points", "public_symbols", "direct_references", "related_tests",
                    )},
                },
                "pending_references": result["pending_references"][:8],
                "similar_responsibilities": result["similar_responsibilities"][:8],
            })
        return {"preview": preview, "more_than_preview": len(rows) > 12}
