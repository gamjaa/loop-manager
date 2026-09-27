"""Source-bound, resumable provisional file reviews for maintenance planning.

Each unit reviews one selected file. These rows are deliberately separate from
the successful file ledger; relationship synthesis and promotion belong to a
later assessment stage.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3
from typing import Sequence

from maintenance_file_inventory import (
    SourceInventory, TrackedSource, _git, cached_source_inventory,
    verify_remote_revision,
)
from maintenance_file_ledger import FileReviewRepository


class FileReviewError(ValueError):
    """A provisional review or its source identity cannot be trusted."""


RESULT_PREFIX = "FILE_REVIEW_RESULT: "
MAX_EMBEDDED_SOURCE_BYTES = 48_000
MAX_FIELD_CHARS = 1_200
MAX_LIST_ITEMS = 32
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


def _string(value: object, label: str, *, minimum: int = 1) -> str:
    if (not isinstance(value, str) or len(value.strip()) < minimum
        or len(value) > MAX_FIELD_CHARS):
        raise FileReviewError(f"{label}에는 구체적인 비어 있지 않은 문자열이 필요합니다.")
    return value.strip()


def _strings(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if (not isinstance(value, list) or len(value) > MAX_LIST_ITEMS
        or (required and not value)):
        raise FileReviewError(f"{label}에는 문자열 배열이 필요합니다.")
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
        if not isinstance(item, dict) or set(item) != {"target_path", "symbol", "evidence"}:
            raise FileReviewError(f"{label} 참조 필드가 유효하지 않습니다.")
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
        if not isinstance(item, dict) or set(item) != {"target_path", "responsibility", "reason"}:
            raise FileReviewError("유사 책임 후보 필드가 유효하지 않습니다.")
        path = _path(item["target_path"], "유사 책임 경로")
        responsibility = _string(item["responsibility"], "유사 책임")
        reason = _string(item["reason"], "유사 책임 후보 근거", minimum=12)
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
        if not isinstance(item, dict) or set(item) != {
            "index", "start", "end", "chunk_hash", "opening_excerpt",
            "middle_excerpt", "closing_excerpt", "review",
        }:
            raise FileReviewError("검토 구간의 결과 형식이 유효하지 않습니다.")
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
        note = _string(item["review"], "구간별 검토 근거", minimum=20)
        if not any(anchor in note for anchor in (opening, middle, closing)):
            raise FileReviewError("구간별 검토 근거가 원본 인용과 연결되지 않았습니다.")
        normalized.append({
            "index": index, "start": start, "end": end,
            "chunk_hash": item["chunk_hash"], "opening_excerpt": opening,
            "middle_excerpt": middle, "closing_excerpt": closing, "review": note,
        })
    return normalized


def parse_review(output: str, source: TrackedSource, content: bytes) -> dict:
    """Validate a single Agent result against content actually read from Git."""
    lines = [line[len(RESULT_PREFIX):] for line in output.splitlines()
             if line.startswith(RESULT_PREFIX)]
    if len(lines) != 1:
        raise FileReviewError("FILE_REVIEW_RESULT JSON 줄이 정확히 하나 필요합니다.")
    # Bound individual fields and lists below, not the serialized document:
    # mandatory chunk evidence grows with the source and JSON escaping varies.
    try:
        result = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise FileReviewError("FILE_REVIEW_RESULT JSON이 유효하지 않습니다.") from error
    expected = {
        "path", "content_hash", "mode", "source_revision", "evidence",
        "code_map", "pending_references", "similar_responsibilities",
    }
    if not isinstance(result, dict) or set(result) != expected:
        raise FileReviewError("FILE_REVIEW_RESULT 필드가 계약과 다릅니다.")
    if tuple(result[key] for key in ("path", "content_hash", "mode", "source_revision")) != (
        source.path, source.content_hash, source.mode, source.revision,
    ):
        raise FileReviewError("검토 결과의 source identity가 선정 파일과 다릅니다.")
    text = _source_text(content)
    evidence = result["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "excerpts", "readability", "responsibility_and_integration",
        "error_recovery", "test_boundary", "duplication", "change_context",
        "chunk_reviews",
    }:
        raise FileReviewError("파일별 검토 근거가 누락됐습니다.")
    excerpts = _strings(evidence["excerpts"], "코드 인용", required=bool(text.strip()))
    if (not text.strip() and excerpts) or any(excerpt not in text for excerpt in excerpts):
        raise FileReviewError("빈 원본 또는 원본에 없는 코드 인용은 검토로 인정할 수 없습니다.")
    if len(text.strip()) >= 8 and all(len(excerpt) < 8 for excerpt in excerpts):
        raise FileReviewError("코드 인용이 실제 검토 근거로 사용하기에 너무 짧습니다.")
    evidence["chunk_reviews"] = _validate_chunk_reviews(
        evidence["chunk_reviews"], content,
    )
    for key in set(evidence) - {"excerpts", "chunk_reviews"}:
        _string(evidence[key], key, minimum=12)
    code_map = result["code_map"]
    if not isinstance(code_map, dict) or set(code_map) != {
        "responsibility", "entry_points", "public_symbols", "direct_references",
        "related_tests",
    }:
        raise FileReviewError("코드맵 필드가 누락됐습니다.")
    _string(code_map["responsibility"], "책임", minimum=12)
    for key in ("entry_points", "public_symbols"):
        symbols = _strings(code_map[key], key)
        if any(symbol not in text for symbol in symbols):
            raise FileReviewError(f"{key}에 원본에 없는 심벌이 있습니다.")
    code_map["related_tests"] = [
        _path(path, "관련 테스트 경로")
        for path in _strings(code_map["related_tests"], "related_tests")
    ]
    code_map["direct_references"] = _references(
        code_map["direct_references"], "직접 참조", text,
    )
    result["pending_references"] = _references(
        result["pending_references"], "미확인 직접 참조", text,
    )
    result["similar_responsibilities"] = _similar_candidates(
        result["similar_responsibilities"],
    )
    return result


def review_prompt(source: TrackedSource, content: bytes) -> str:
    """Give the Agent the exact blob for a bounded, single-file review unit."""
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
    if not text.strip():
        source_instruction += (
            "검증된 원본은 0 byte이거나 공백만 포함한다. excerpts는 []로 기록하고 "
            "각 평가 항목과 code_map.responsibility에 실행 코드가 없는 사실과 "
            "경로에서 확인할 수 있는 역할을 설명하라. 심벌·직접 참조를 만들어내지 마라. "
            "0 byte이면 chunk_reviews도 []이며, 공백 byte가 있으면 모든 구간의 "
            "지문과 공백 인용 및 검토 근거를 기록하라.\n"
        )
    return (
        "당신은 유지보수 Planner다. 아래 Git blob 원본 파일 하나를 실제로 읽고 검토하라. "
        "파일이나 문서를 수정하지 마라. 길이만으로 개선을 강제하지 말고, 읽기 쉬움, "
        "책임과 외부 연동, 오류 복구, 테스트 경계, 중복 및 후속 작업의 관련 없는 문맥 "
        "감소를 각각 구체적으로 평가하라. 현재 원본에서 확인한 직접 참조·유사 책임 중 "
        "나중에 확인할 후보만 pending에 남겨라. 문맥 파일은 검토 완료 대상으로 세지 마라.\n"
        f"source identity: {source_location}\n"
        + source_instruction +
        "최종 응답에는 FILE_REVIEW_RESULT: 다음에 한 줄 JSON object를 정확히 하나 기록하라. "
        "필수 필드: path, content_hash, mode, source_revision, "
        "evidence={excerpts:[원본의 정확한 인용], readability, "
        "responsibility_and_integration, error_recovery, test_boundary, "
        "duplication, change_context, chunk_reviews:[각 구간마다 순서대로 "
        "{index, start, end, chunk_hash:해당 byte 구간 SHA256, "
        "opening_excerpt, middle_excerpt, closing_excerpt, review}]}, "
        "각 구간의 byte 범위는 index*chunk_bytes부터 다음 경계까지이며 "
        "시작·중간·끝 인용과 그중 하나를 직접 언급하는 구체적인 review가 모두 필요하다. "
        "code_map={responsibility, entry_points:[], "
        "public_symbols:[], direct_references:[{target_path:null|상대경로, "
        "symbol:null|심벌, evidence:원본의 정확한 인용}], related_tests:[]}, "
        "pending_references:[같은 참조 객체], "
        "similar_responsibilities:[{target_path:상대경로, responsibility:책임, "
        "reason:구체적 이유}]. 확인된 것이 없는 배열은 비워라."
    )


class ProvisionalReviewRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)
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
        selected = FileReviewRepository(self.connection).selected_sources(repo, inventory)
        expected = {item.path: item for item in selected}
        with self.connection:
            rows = self.connection.execute(
                "SELECT path, cycle_revision, source_revision, content_hash, mode, "
                "session_id, validation_error, result_json "
                "FROM provisional_file_reviews WHERE repo = ?",
                (repo,),
            ).fetchall()
            valid: dict[str, tuple[str | None, str | None, dict | None]] = {}
            for path, cycle, revision, digest, mode, session_id, error, raw in rows:
                source = expected.get(str(path))
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
                            self.connection.execute("INSERT OR REPLACE INTO provisional_review_receipts VALUES (?, ?, ?)",
                                                    (repo, path, identity))
                except (FileReviewError, ValueError):
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
            unit = (ReviewUnit(next_source, *valid.get(next_source.path, (None, None, None))[:2])
                    if next_source else None)
        return FileReviewProgress(selected, completed, unit)

    def start(self, repo: str, cycle_revision: int, source: TrackedSource) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO provisional_file_reviews "
                "(repo, path, cycle_revision, source_revision, content_hash, mode) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo, source.path, cycle_revision, source.revision,
                 source.content_hash, source.mode),
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
