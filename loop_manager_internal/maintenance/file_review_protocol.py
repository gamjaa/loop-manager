"""File-review output contracts shared by the decoder and prompt examples."""

from __future__ import annotations

import hashlib
import json
import re


class FileReviewError(ValueError):
    """A provisional review or its source identity cannot be trusted."""


RESULT_MARKER = "FILE_REVIEW_RESULT:"
RESULT_PREFIX = RESULT_MARKER + " "
MAX_FIELD_CHARS = 1_200
MAX_LIST_ITEMS = 32
MIN_DESCRIPTION_CHARS = 12
MIN_CHUNK_REVIEW_CHARS = 20
MIN_EXCERPT_CHARS = 8
EXAMPLE_CONTENT = b"def calculate(): return 1\n"
EXAMPLE_TREE = ("100644 blob " + "b" * 40 + "\texample.py\n").encode()
EVIDENCE_TEXT_FIELDS = (
    "readability", "responsibility_and_integration", "error_recovery",
    "test_boundary", "duplication", "change_context",
)
REFERENCE_FIELDS = ("target_path", "symbol", "evidence")
SIMILAR_FIELDS = ("target_path", "responsibility", "reason")
ROOT_CATEGORIES = (
    "tool_required", "user_entry", "execution_build_deploy",
    "internal_implementation", "uncertain",
)


def result_example(*, root: bool = False) -> dict:
    """Return a complete, synthetic example; never claim to review real input."""
    content = EXAMPLE_TREE if root else EXAMPLE_CONTENT
    result = {
        "path": "." if root else "example.py",
        "content_hash": hashlib.sha256(content).hexdigest(),
        "mode": "040000" if root else "100644",
        "source_revision": "a" * 40,
    }
    if root:
        result["root_reviews"] = [{
            "path": "example.py", "category": "uncertain",
            "references": [{"path": ".", "excerpt": EXAMPLE_TREE.decode().strip()}],
            "user_path": "사용자 진입 경로를 확인할 근거가 부족합니다.",
            "ownership": "담당하는 모듈과 소유권을 확인할 근거가 부족합니다.",
            "destination": None,
            "reason": "Git tree에서 존재만 확인되어 실제 사용 조사 전에는 이동하지 않습니다.",
        }]
        return result
    excerpt = EXAMPLE_CONTENT.decode().strip()
    result["evidence"] = {
        "excerpts": [excerpt],
        "readability": "calculate의 본문이 짧고 반환 값을 바로 확인할 수 있습니다.",
        "responsibility_and_integration": "calculate는 상수를 반환하며 외부 도구를 호출하지 않습니다.",
        "error_recovery": "이 함수에는 외부 실패나 복구 상태를 다루는 분기가 없습니다.",
        "test_boundary": "calculate의 반환 값을 호출 경계에서 독립적으로 검증할 수 있습니다.",
        "duplication": "이 원본 안에는 같은 기능의 반복 구현이 보이지 않습니다.",
        "change_context": "이 함수의 변경에는 원본 안의 다른 기능 문맥이 필요하지 않습니다.",
        "chunk_reviews": [{
            "index": 0, "start": 0, "end": len(EXAMPLE_CONTENT),
            "chunk_hash": hashlib.sha256(EXAMPLE_CONTENT).hexdigest(),
            "opening_excerpt": excerpt, "middle_excerpt": excerpt,
            "closing_excerpt": excerpt,
            "review": excerpt + "는 단일 상수 반환 함수로 별도 연동이나 상태 변경이 없습니다.",
        }],
    }
    result["code_map"] = {
        "responsibility": "호출자에게 상수 1을 반환하는 단일 함수를 제공합니다.",
        "entry_points": ["calculate"], "public_symbols": ["calculate"],
        "direct_references": [], "related_tests": [],
    }
    result["pending_references"] = []
    result["similar_responsibilities"] = []
    return result


# The prompt example defines the object field sets checked by the parser.
FILE_RESULT_FIELDS = tuple(result_example())
ROOT_RESULT_FIELDS = tuple(result_example(root=True))
EVIDENCE_FIELDS = tuple(result_example()["evidence"])
CODE_MAP_FIELDS = tuple(result_example()["code_map"])
CHUNK_FIELDS = tuple(result_example()["evidence"]["chunk_reviews"][0])
ROOT_ITEM_FIELDS = tuple(result_example(root=True)["root_reviews"][0])
ROOT_REFERENCE_FIELDS = ("path", "excerpt")


def require_fields(
    value: object, fields: tuple[str, ...], label: str, *,
    error_type: type[ValueError] = FileReviewError,
) -> None:
    if not isinstance(value, dict):
        raise error_type(f"{label}에는 JSON object가 필요합니다.")
    missing = sorted(set(fields) - set(value))
    extra = sorted(set(value) - set(fields))
    if missing or extra:
        raise error_type(
            f"{label} 필드가 계약과 다릅니다. "
            f"누락 필드: {', '.join(missing) or '없음'}; "
            f"추가 필드: {', '.join(extra) or '없음'}.",
        )


def review_string(
    value: object, label: str, *, minimum: int = 1,
    error_type: type[ValueError] = FileReviewError,
) -> str:
    if not isinstance(value, str):
        raise error_type(f"{label}에는 문자열이 필요합니다 (현재 {type(value).__name__}).")
    if len(value.strip()) < minimum:
        raise error_type(
            f"{label} 문자열이 너무 짧습니다: 앞뒤 공백 제거 후 {len(value.strip())}자, "
            f"최소 {minimum}자 필요.",
        )
    if len(value) > MAX_FIELD_CHARS:
        raise error_type(
            f"{label} 문자열이 너무 깁니다: {len(value)}자, 최대 {MAX_FIELD_CHARS}자.",
        )
    return value.strip()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise FileReviewError(f"FILE_REVIEW_RESULT JSON에 중복 key가 있습니다: {key}.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise FileReviewError(f"FILE_REVIEW_RESULT JSON에 허용되지 않은 값이 있습니다: {value}.")


def decode_result(output: str) -> dict:
    """Decode one tagged object across harmless whitespace and JSON line breaks."""
    markers = list(re.finditer(r"(?m)^[ \t]*FILE_REVIEW_RESULT:[ \t]*", output))
    if len(markers) != 1:
        raise FileReviewError("FILE_REVIEW_RESULT JSON 결과가 정확히 하나 필요합니다.")
    candidate = output[markers[0].end():].lstrip()
    # Also accept a JSON code fence immediately after the marker. A fence
    # wrapping the marker itself is already outside the decoded document.
    fence = re.match(r"```(?:json)?[ \t]*\r?\n", candidate)
    if fence:
        candidate = candidate[fence.end():].lstrip()
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    try:
        result, end = decoder.raw_decode(candidate)
    except json.JSONDecodeError as error:
        raise FileReviewError(
            f"FILE_REVIEW_RESULT JSON이 유효하지 않습니다: {error.msg} "
            f"(line {error.lineno}, column {error.colno}).",
        ) from error
    trailing = candidate[end:].strip()
    if trailing not in {"", "```"}:
        raise FileReviewError("FILE_REVIEW_RESULT JSON 뒤에는 추가 결과나 설명을 붙이지 마세요.")
    if not isinstance(result, dict):
        raise FileReviewError("FILE_REVIEW_RESULT에는 JSON object가 필요합니다.")
    return result


def result_instructions(*, root: bool = False) -> str:
    """Use valid JSON rather than prose-shaped pseudocode for the result layout."""
    instructions = (
        "최종 응답은 FILE_REVIEW_RESULT:로 시작하는 JSON object 하나만 작성하라. "
        "콜론 뒤 공백·줄바꿈과 JSON 들여쓰기는 허용한다. "
        "아래 예시와 정확히 같은 필드만 사용하라. 필드를 생략하거나 추가하지 마라. "
        "이 예시는 가상 example.py의 형식 예시다. 값은 복사하지 말고 위 source identity와 "
        "실제 조사 근거로 모두 교체하라.\n"
        "```text\n" + RESULT_PREFIX
        + json.dumps(result_example(root=root), ensure_ascii=False, indent=2) + "\n```\n"
    )
    if root:
        return instructions + (
            f"root_reviews는 모든 루트 항목을 한 번씩 포함하라. category는 {', '.join(ROOT_CATEGORIES)} "
            f"중 하나다. user_path, ownership, reason은 앞뒤 공백 제거 후 {MIN_DESCRIPTION_CHARS}~{MAX_FIELD_CHARS}자다. "
            f"references는 항목마다 1~{MAX_LIST_ITEMS}개이고 excerpt는 정확한 원본 인용 "
            f"{MIN_EXCERPT_CHARS}~{MAX_FIELD_CHARS}자다. destination은 안전한 상대 경로 또는 null이며 "
            "확인된 internal_implementation만 이동 후보가 될 수 있다.\n"
        )
    reference = dict(zip(REFERENCE_FIELDS, (None, "helper", "helper()")))
    similar = dict(zip(SIMILAR_FIELDS, ("src/other.py", "연관된 책임 설명", "조사한 구체적인 후보 근거")))
    return instructions + (
        f"evidence의 {', '.join(EVIDENCE_TEXT_FIELDS)}와 code_map.responsibility는 "
        f"각각 앞뒤 공백 제거 후 최소 {MIN_DESCRIPTION_CHARS}자, 전체 최대 {MAX_FIELD_CHARS}자다. "
        f"chunk_reviews의 review는 최소 {MIN_CHUNK_REVIEW_CHARS}자, 최대 {MAX_FIELD_CHARS}자다. "
        f"그 외 문자열은 비어 있지 않고 최대 {MAX_FIELD_CHARS}자다. "
        f"배열은 최대 {MAX_LIST_ITEMS}개다. 예외로 chunk_reviews는 제한 없이 "
        "원본의 모든 byte 구간을 순서대로 포함한다. 인용은 실제 원본에 있어야 하며 "
        f"excerpts 중 하나는 최소 {MIN_EXCERPT_CHARS}자다 (그보다 짧은 원본은 가능한 실제 인용). "
        "확인된 것이 없는 배열은 []로 남겨라.\n"
        "direct_references와 pending_references 각 항목의 JSON 형식: "
        + json.dumps(reference, ensure_ascii=False)
        + ". target_path는 상대 경로 또는 null, symbol은 심벌 또는 null이며 "
        "둘 중 하나는 필요하고 evidence는 정확한 원본 인용이다.\n"
        "similar_responsibilities 각 항목의 JSON 형식: "
        + json.dumps(similar, ensure_ascii=False)
        + f". target_path는 상대 경로, reason은 {MIN_DESCRIPTION_CHARS}~{MAX_FIELD_CHARS}자다.\n"
    )
