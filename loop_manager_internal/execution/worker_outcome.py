"""Provider-independent contract for a Worker turn's disposition."""

from __future__ import annotations

from loop_manager_internal.application.result_normalization import normalize_markers, tagged_json

from dataclasses import dataclass
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
    plan_id: str = ""
    start_head: str = ""
    commit_title: str = ""
    conflict_assessment: dict[str, object] | None = None

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
    allow_legacy_ready: bool = False,
) -> WorkerOutcome:
    """Read one explicit disposition, except for a stored legacy completion."""

    message = normalize_markers(message, ("WORKER_OUTCOME", "COMMIT_TITLE", "PR_SUMMARY", "TARGETED_TESTS"))
    declarations = _DECLARATION.findall(message)
    if not declarations:
        if allow_legacy_ready:
            return WorkerOutcome("ready_for_validation")
        raise WorkerOutcomeError("새 Worker 결과에 WORKER_OUTCOME 선언이 없습니다.")
    if len(declarations) != 1 or len(message) > 65536 or len(declarations[0]) > 12000:
        raise WorkerOutcomeError("Worker 결과 선언이 중복되거나 너무 깁니다.")
    try:
        value = tagged_json(message, "WORKER_OUTCOME", max_bytes=12000)
    except (ValueError, TypeError) as error:
        raise WorkerOutcomeError("Worker 결과 선언 JSON이 유효하지 않습니다.") from error
    if not isinstance(value, dict):
        raise WorkerOutcomeError("Worker 결과 선언은 JSON 객체여야 합니다.")
    kind = value.get("kind")
    if kind == 'pr_conflict_unrecoverable':
        from loop_manager_internal.recovery.recovery_runtime import unavailable_routes_complete
        from loop_manager_internal.git.managed_rebase_validation import _safe_path
        if (set(value) != {'kind', 'problem', 'conflict_paths', 'attempted_routes',
                          'unavailable_recovery_routes', 'release_conditions'} or _PUBLICATION.search(message)):
            raise WorkerOutcomeError('PR 충돌 관측은 완료·checkpoint·사용자 질문과 혼합할 수 없습니다.')
        problem = _required_text(value['problem'], 'problem')
        paths = value['conflict_paths']
        attempts = value['attempted_routes']
        releases = value['release_conditions']
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 128
                or any(not isinstance(path, str) or not _safe_path(path) for path in paths)
                or len(set(paths)) != len(paths)
                or not isinstance(attempts, dict)
                or set(attempts) != {'worker_correction', 'file_restore_rework', 'revalidation'}
                or not unavailable_routes_complete(value['unavailable_recovery_routes'])
                or not isinstance(releases, list) or not 1 <= len(releases) <= 8):
            raise WorkerOutcomeError('PR 충돌 관측의 파일·시도 경로·불가 사유·재평가 조건이 유효하지 않습니다.')
        for key, text in {**attempts, **value['unavailable_recovery_routes']}.items():
            _required_text(text, key)
        for text in releases:
            _required_text(text, 'release_condition')
        return WorkerOutcome(kind, problem=problem, conflict_assessment=value)
    if kind == "common_defect_pending":
        from loop_manager_internal.execution.common_defect_report import parse_report
        try:
            report = parse_report(message)
        except ValueError as error:
            raise WorkerOutcomeError(str(error)) from error
        if set(value) != {"kind"} or report is None or _PUBLICATION.search(message):
            raise WorkerOutcomeError("공통 교정 대기는 유효한 결함 보고를 요구하며 완료·사용자 질문과 혼합할 수 없습니다.")
        return WorkerOutcome(kind)
    if kind == "ready_for_validation":
        if set(value) != {"kind"}:
            raise WorkerOutcomeError("완료 후보 결과 선언에 결정 대기 필드가 섞였습니다.")
        return WorkerOutcome(kind)
    if kind == "checkpoint":
        if set(value) != {"kind", "plan_id", "start_head"}:
            raise WorkerOutcomeError("checkpoint 요청의 작업·시작 HEAD 필드가 유효하지 않습니다.")
        if (not isinstance(value['plan_id'], str) or re.fullmatch(r'G-\d{3,}', value['plan_id']) is None
                or not isinstance(value['start_head'], str)
                or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['start_head']) is None):
            raise WorkerOutcomeError("checkpoint 요청의 작업·시작 HEAD가 유효하지 않습니다.")
        labels = _PUBLICATION.findall(message)
        titles = re.findall(r'(?m)^COMMIT_TITLE:[ \t]*(\S[^\r\n]*)$', message)
        if len(labels) != 1 or len(titles) != 1 or len(titles[0]) > 200 or any(ord(c) < 32 for c in titles[0]):
            raise WorkerOutcomeError("checkpoint는 COMMIT_TITLE 하나만 보고하며 완료 선언과 혼합할 수 없습니다.")
        return WorkerOutcome(kind, plan_id=value['plan_id'], start_head=value['start_head'],
                             commit_title=titles[0].strip())
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
        "COMMIT_TITLE: 구체적 변경 제목, PR_SUMMARY: 최종 변경 설명, "
        "TARGETED_TESTS: 실제 실행 결과 또는 미실행 사유를 보고하라. 사용자 결정이 필요해 "
        "구현을 중단한다면 WORKER_OUTCOME: "
        '{"kind":"needs_user_decision","problem":"구체적 문제",'
        '"proposal":"필요한 변경과 영향","locations":'
        '[{"path":"docs/PRODUCT.md","section":"실제 절 제목"}],'
        '"question":"사용자가 답해야 할 질문"} 을 보고하라. '
        "결정 대기에는 완료 제목이나 성공한 targeted test를 요구하지 않는다. "
        "locations는 선택된 ExecPlan, 해당 sources, PRODUCT, ARCHITECTURE의 "
        "실제로 존재하는 경로·절만 사용하라."
        ' 기존 명세가 정한 범위 밖 공통 교정 때문에 미완료라면 유효한 COMMON_DEFECT_REPORT와 '
        'WORKER_OUTCOME: {"kind":"common_defect_pending"}만 보고하고 완료 publication 선언은 쓰지 마라. '
        ' 전체 구현·교정 완료를 기본 목표로 하며 작은 변경마다 종료하지 마라. '
        '복구점 확보 등 중간 보존이 필요한 경우에만 본문에 이유·전진한 완료 조건·잔여 작업을 기록하고 '
        'WORKER_OUTCOME: {"kind":"checkpoint",'
        '"plan_id":"선택 G-ID","start_head":"이번 호출 시작 HEAD"} 와 '
        'COMMIT_TITLE: 구체적인 보존점 제목만 반환하라. PR_SUMMARY·TARGETED_TESTS와 '
        '완료 성공을 합성하지 마라. Manager가 종료·범위 검사 후 commit·게시한다.'
        ' 소유 PR의 rebase 충돌에서만 안전한 자동 경로가 모두 불가능하다는 실제 관측을 '
        'WORKER_OUTCOME: {"kind":"pr_conflict_unrecoverable","problem":"원인",'
        '"conflict_paths":["실제 충돌 파일"],"attempted_routes":{"worker_correction":"실제 시도와 결과",'
        '"file_restore_rework":"원본 복원/재작업의 관측","revalidation":"재검증 결과 또는 적용 불가 사유"},'
        '"unavailable_recovery_routes":{"reobservation":"불가 근거","retry":"불가 근거",'
        '"correction":"불가 근거","condition_wait":"대기할 자원이 없는 근거",'
        '"next_run":"재시작으로 바뀌지 않는 근거","safe_recovery":"안전 복구 불가 근거"},'
        '"release_conditions":["원인별 재평가 조건"]} 로 보고할 수 있다. '
        '이 보고는 관측이며 Manager의 차단 결정이 아니다. 횟수 소진·사용 한도·조회 실패·잠금·'
        '종료 불명만으로 보고하지 말고, 성공·질문·publication 선언을 섞지 마라. '
        'Manager가 현재 입력·소유권과 파일 복원/재작업 가능성을 재검사한다.'
    )
