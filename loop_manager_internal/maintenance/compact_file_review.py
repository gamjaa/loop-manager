"""Compact Agent transport expanded into the existing durable review contract."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import PurePosixPath

from loop_manager_internal.maintenance.file_review_evidence import _DefinedSymbols
from loop_manager_internal.maintenance.file_review_protocol import (
    EVIDENCE_TEXT_FIELDS, FileReviewError, MAX_FIELD_CHARS, MAX_LIST_ITEMS,
    MIN_DESCRIPTION_CHARS, MIN_CHUNK_REVIEW_CHARS, RESULT_PREFIX, require_fields,
    review_string,
)


VERSION = 2
BLANK_CHUNK_NOTE = "Manager 확정 사실: 이 원본 byte 구간에는 공백만 있고 실행 코드가 없습니다."
REQUIRED_FIELDS = ("version", "path", "content_hash", "mode", "source_revision", "summary", "chunks")
OPTIONAL_FIELDS = ("issues", "symbols", "entry_points", "references", "tests", "pending", "similar")


def source_facts(source, content: bytes, source_paths=(), *, chunk_bytes: int) -> dict:
    """Compute source identities, citation anchors and actual Python candidates."""
    chunks = []
    for index, start in enumerate(range(0, len(content), chunk_bytes)):
        end = min(start + chunk_bytes, len(content))
        text = content[start:end].decode("utf-8", "backslashreplace")
        middle = max(0, len(text) // 2 - 128)
        chunks.append({
            "index": index, "start": start, "end": end,
            "chunk_hash": hashlib.sha256(content[start:end]).hexdigest(),
            "opening_excerpt": text[:48], "middle_excerpt": text[middle:middle + 48],
            "closing_excerpt": text[-48:],
        })
    facts = {"chunk_reviews": chunks, "symbols": [], "references": []}
    if not source.path.endswith((".py", ".pyi")):
        return facts
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, UnicodeError):
        return facts
    defined = _DefinedSymbols()
    defined.visit(tree)
    facts["symbols"] = sorted(defined.names)
    modules = {}
    for path in sorted(source_paths):
        if path.endswith((".py", ".pyi")):
            parts = list(PurePosixPath(path).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            modules[".".join(parts)] = path
    lines = content.decode("utf-8", "backslashreplace").splitlines(keepends=True)
    imports = sorted((node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))),
                     key=lambda node: node.lineno)
    for node in imports:
        excerpt = "".join(lines[node.lineno - 1:node.end_lineno]).strip()[:MAX_FIELD_CHARS]
        if not excerpt:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level:
                package = list(PurePosixPath(source.path).parent.parts)
                if node.level > len(package):
                    continue
                package = package[:len(package) - node.level + 1]
                module = ".".join((*package, *(node.module or "").split("."))).rstrip(".")
            else:
                module = node.module or ""
        for alias in node.names:
            if isinstance(node, ast.Import):
                target = modules.get(alias.name)
                symbol = alias.name
            else:
                target = modules.get(f"{module}.{alias.name}") or modules.get(module)
                symbol = alias.name
            reference = {"target_path": target, "symbol": symbol, "evidence": excerpt}
            if reference not in facts["references"]:
                facts["references"].append(reference)
    return facts


def _indexed(items, candidates, label):
    if not isinstance(items, list) or len(items) > MAX_LIST_ITEMS:
        raise FileReviewError(f"{label}은 최대 {MAX_LIST_ITEMS}개의 배열이어야 합니다.")
    result = []
    for item in items:
        if type(item) is int:
            if not 0 <= item < len(candidates):
                raise FileReviewError(f"{label}의 Manager 후보 index가 범위 밖입니다: {item}")
            item = candidates[item]
        result.append(item)
    return result


def expand_result(result: dict, source, content: bytes, source_paths=(), *, chunk_bytes: int) -> dict:
    """Restore legacy fields from supplied judgments and Manager-owned facts."""
    if result.get("version") != VERSION or type(result.get("version")) is not int:
        raise FileReviewError("지원하지 않는 compact 검토 결과 version입니다.")
    unknown = set(result) - set((*REQUIRED_FIELDS, *OPTIONAL_FIELDS))
    if unknown:
        raise FileReviewError("compact 검토 결과에 알 수 없는 필드가 있습니다: " + ", ".join(sorted(unknown)))
    require_fields({key: result[key] for key in REQUIRED_FIELDS if key in result},
                   REQUIRED_FIELDS, "compact 검토 결과")
    if hashlib.sha256(content).hexdigest() != source.content_hash:
        raise FileReviewError("compact 검토의 원본 지문이 선정 파일과 다릅니다.")
    summary = review_string(result["summary"], "summary", minimum=MIN_DESCRIPTION_CHARS)
    issues = result.get("issues", {})
    if not isinstance(issues, dict) or set(issues) - set(EVIDENCE_TEXT_FIELDS):
        raise FileReviewError("issues에는 기존 여섯 평가 항목의 구체적인 판단만 기록할 수 있습니다.")
    for key, note in issues.items():
        review_string(note, f"issues.{key}", minimum=MIN_DESCRIPTION_CHARS)
    facts = source_facts(source, content, source_paths, chunk_bytes=chunk_bytes)
    notes = result["chunks"]
    if not isinstance(notes, list) or len(notes) != len(facts["chunk_reviews"]):
        raise FileReviewError("compact 검토에도 원본의 모든 검토 구간에 대한 근거가 필요합니다.")
    chunks = []
    for fact, note in zip(facts["chunk_reviews"], notes):
        if note is None and not content[fact["start"]:fact["end"]].strip():
            note = BLANK_CHUNK_NOTE
        review_string(note, "chunk review", minimum=MIN_CHUNK_REVIEW_CHARS)
        chunks.append({**fact, "review": note})
    text = content.decode("utf-8", "backslashreplace")
    # The full existing validator subsequently checks every note against these
    # source-bound anchors. This restoration creates no new Agent judgments.
    evidence = {"excerpts": [text.strip()[:64]] if text.strip() else [],
                **{key: issues.get(key, summary) for key in EVIDENCE_TEXT_FIELDS},
                "chunk_reviews": chunks}
    return {
        **{key: result[key] for key in ("path", "content_hash", "mode", "source_revision")},
        "evidence": evidence,
        "code_map": {
            "responsibility": summary,
            "entry_points": _indexed(result.get("entry_points", []), facts["symbols"], "entry_points"),
            "public_symbols": _indexed(result.get("symbols", []), facts["symbols"], "symbols"),
            "direct_references": _indexed(result.get("references", []), facts["references"], "references"),
            "related_tests": result.get("tests", []),
        },
        "pending_references": _indexed(result.get("pending", []), facts["references"], "pending"),
        "similar_responsibilities": result.get("similar", []),
    }


def result_instructions() -> str:
    example = {
        "version": VERSION, "path": "example.py", "content_hash": "위 identity의 hash",
        "mode": "100644", "source_revision": "위 identity의 revision",
        "summary": "calculate는 상수 값을 반환하며 이 원본에서 구조 개선 문제는 발견되지 않았습니다.",
        "chunks": ["def calculate(): return 1은 외부 연동이나 가변 상태 없이 값을 반환합니다."],
        "symbols": [0],
    }
    return (
        "짧은 compact 결과를 사용하라. summary는 책임과 종합 판단을 1~2문장으로 쓰고, "
        "문제가 없는 평가 항목의 장문 설명은 반복하지 마라. six-aspect 검토 자체는 생략하지 마라. "
        "구체적인 개선 후보나 불확실성은 issues에 해당 항목과 근거를 기록하라. "
        f"항목은 {', '.join(EVIDENCE_TEXT_FIELDS)}이며 없으면 issues를 생략할 수 있다. "
        "summary와 issues의 각 값은 12~1200자다. chunks에는 모든 구간의 순서대로 "
        "20~1200자의 구체적인 검토 근거를 하나씩 기록하고, 제공한 세 excerpt 중 하나의 "
        "연속 원본문자열을 12자 이상 그대로 인용하라(더 짧은 원본 인용은 전체 사용). "
        "공백만 있는 구간의 chunks 항목은 null로 두면 Manager가 원본 사실을 결합한다. "
        "내용이 있는 구간 전체를 읽지 못했으면 완료를 주장하지 마라. "
        "Manager가 계산한 hash·범위·인용은 재계산하거나 결과에 복사하지 마라. "
        "symbols·entry_points는 Manager symbols의 0부터 시작하는 index 또는 실제 심벌 문자열이다. "
        "references·pending은 Manager references의 index 또는 기존 "
        "{target_path:상대경로|null,symbol:심벌|null,evidence:정확한 원본 인용} object다. "
        "제공된 후보는 문맥이며 검토 완료나 문제 발견을 의미하지 않는다. 관련 없는 후보는 선택하지 마라. "
        "tests는 관련 테스트 경로 배열이고 similar는 "
        "{target_path:상대경로,responsibility:책임,reason:구체적인 근거} 배열이다. "
        "문자열·근거는 기존 계약의 1200자 제한, 배열은 32개 제한을 유지한다. "
        "선택하지 않은 선택 필드는 생략할 수 있다. 필수 필드 version/path/content_hash/mode/"
        "source_revision/summary/chunks는 모두 필요하다. 아래 가상 예시의 identity와 판단은 "
        "실제 source에 맞게 바꿔라:\n" + RESULT_PREFIX + json.dumps(example, ensure_ascii=False) + "\n"
    )
