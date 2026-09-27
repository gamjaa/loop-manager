"""Provider-independent contract for a Worker turn's disposition."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
from typing import Mapping


class WorkerOutcomeError(ValueError):
    """A Worker disposition is ambiguous or outside its documented inputs."""


@dataclass(frozen=True)
class DecisionLocation:
    path: str
    section: str


@dataclass(frozen=True)
class WorkerOutcome:
    kind: str
    problem: str = ""
    proposal: str = ""
    locations: tuple[DecisionLocation, ...] = ()
    question: str = ""

    def evidence(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "problem": self.problem,
            "proposal": self.proposal,
            "locations": [
                {"path": item.path, "section": item.section}
                for item in self.locations
            ],
            "question": self.question,
        }


_DECLARATION = re.compile(r"(?m)^WORKER_OUTCOME:[ \t]*(.*)$")
_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_PUBLICATION = re.compile(r"(?m)^[ \t]*(?:COMMIT_TITLE|PR_SUMMARY|TARGETED_TESTS):")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise WorkerOutcomeError(f"Worker 결과의 중복 JSON 필드: {key}")
        result[key] = value
    return result


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise WorkerOutcomeError(f"Worker 결정 대기의 {name} 값이 비어 있거나 너무 깁니다.")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise WorkerOutcomeError(f"Worker 결정 대기의 {name} 값에 제어 문자가 있습니다.")
    return value.strip()


def parse_worker_outcome(
    message: str,
    *,
    plan_path: str,
    sources: tuple[str, ...],
    documents: Mapping[str, str],
) -> WorkerOutcome:
    """Read one explicit disposition; legacy publication text remains ready."""

    declarations = _DECLARATION.findall(message)
    if re.search(r"(?m)^[ \t]+WORKER_OUTCOME:", message):
        raise WorkerOutcomeError("Worker 결과 선언은 줄 시작에서 작성해야 합니다.")
    if not declarations:
        return WorkerOutcome("ready_for_validation")
    if len(declarations) != 1 or len(message) > 65536 or len(declarations[0]) > 12000:
        raise WorkerOutcomeError("Worker 결과 선언이 중복되거나 너무 깁니다.")
    try:
        value = json.loads(declarations[0], object_pairs_hook=_unique_object)
    except (ValueError, TypeError) as error:
        raise WorkerOutcomeError("Worker 결과 선언 JSON이 유효하지 않습니다.") from error
    if not isinstance(value, dict):
        raise WorkerOutcomeError("Worker 결과 선언은 JSON 객체여야 합니다.")
    kind = value.get("kind")
    if kind == "ready_for_validation":
        if set(value) != {"kind"}:
            raise WorkerOutcomeError("완료 후보 결과 선언에 결정 대기 필드가 섞였습니다.")
        return WorkerOutcome(kind)
    if kind != "needs_user_decision":
        raise WorkerOutcomeError("알 수 없는 Worker 결과 kind입니다.")
    if set(value) != {"kind", "problem", "proposal", "locations", "question"}:
        raise WorkerOutcomeError("결정 대기 결과 필드가 누락되거나 충돌합니다.")
    if _PUBLICATION.search(message):
        raise WorkerOutcomeError("결정 대기와 완료 후보 publication 선언이 충돌합니다.")
    problem = _required_text(value["problem"], "problem")
    proposal = _required_text(value["proposal"], "proposal")
    question = _required_text(value["question"], "question")
    raw_locations = value["locations"]
    if not isinstance(raw_locations, list) or not 1 <= len(raw_locations) <= 8:
        raise WorkerOutcomeError("결정 근거 위치가 비어 있거나 너무 많습니다.")
    allowed = {plan_path, *sources, "docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
    locations = []
    for raw in raw_locations:
        if not isinstance(raw, dict) or set(raw) != {"path", "section"}:
            raise WorkerOutcomeError("결정 근거 위치의 경로·절이 유효하지 않습니다.")
        path = _required_text(raw["path"], "location.path")
        section = _required_text(raw["section"], "location.section")
        parts = PurePosixPath(path)
        if parts.is_absolute() or any(part in {".", ".."} for part in path.split("/")) or "\\" in path:
            raise WorkerOutcomeError("결정 근거 경로에 traversal이 있습니다.")
        if path not in allowed or path not in documents:
            raise WorkerOutcomeError("결정 근거 경로가 현재 계획의 허용 문서 밖입니다.")
        if section not in {match.group(1).strip() for match in _HEADING.finditer(documents[path])}:
            raise WorkerOutcomeError("결정 근거 절이 문서에 존재하지 않습니다.")
        locations.append(DecisionLocation(path, section))
    return WorkerOutcome(kind, problem, proposal, tuple(locations), question)


def worker_outcome_instruction() -> str:
    """One prompt contract shared by new and resumed provider sessions."""

    return (
        "최종 결과에 한 줄 JSON 선언을 정확히 하나 포함하라. 구현이 완료됐다면 "
        'WORKER_OUTCOME: {"kind":"ready_for_validation"} 을 쓰고 뒤에 '
        "COMMIT_TITLE, PR_SUMMARY, TARGETED_TESTS를 보고하라. 사용자 결정이 필요해 "
        "구현을 중단한다면 WORKER_OUTCOME: "
        '{"kind":"needs_user_decision","problem":"구체적 문제",'
        '"proposal":"필요한 변경과 영향","locations":'
        '[{"path":"docs/PRODUCT.md","section":"실제 절 제목"}],'
        '"question":"사용자가 답해야 할 질문"} 을 보고하라. '
        "결정 대기에는 완료 제목이나 성공한 targeted test를 요구하지 않는다. "
        "locations는 선택된 ExecPlan, 해당 sources, PRODUCT, ARCHITECTURE의 "
        "실제로 존재하는 경로·절만 사용하라."
    )
