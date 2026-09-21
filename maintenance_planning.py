"""Pure contracts for guarded maintenance ExecPlan revisions.

The scheduler integration intentionally lives outside this module.  G-028 only
provides deterministic validation primitives which a later maintenance cycle
orchestrator can invoke after it has acquired a SQLite-backed context.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Mapping, Sequence


class MaintenancePlanningError(RuntimeError):
    """Raised when a maintenance Planner result is unsafe to publish."""


class MaintenancePlanMode(str, Enum):
    BARRIER = "maintenance_barrier"
    REVALIDATION = "maintenance_revalidation"


@dataclass(frozen=True)
class ReadyPlanSnapshot:
    plan_id: str
    path: str
    title: str
    sources: tuple[str, ...]
    depends_on: tuple[str, ...]
    complexity: str
    risk: str
    priority: str
    content_hash: str
    plan_sha: str
    text: str


@dataclass(frozen=True)
class MaintenancePlanningContext:
    repo: str
    mode: MaintenancePlanMode
    active_sha: str
    cycle_revision: int
    cycle_phase: str
    eligible_plans: tuple[ReadyPlanSnapshot, ...]

    @property
    def eligible_by_path(self) -> dict[str, ReadyPlanSnapshot]:
        return {plan.path: plan for plan in self.eligible_plans}

    @property
    def eligible_by_id(self) -> dict[str, ReadyPlanSnapshot]:
        return {plan.plan_id: plan for plan in self.eligible_plans}


@dataclass(frozen=True)
class PlanRevision:
    path: str
    plan: Mapping[str, object]
    snapshot: ReadyPlanSnapshot


@dataclass(frozen=True)
class MarkdownSections:
    order: tuple[str, ...]
    values: Mapping[str, str]


_SECTION = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`\r\n]+)`")
# Path evidence comes from immutable Git trees, not punctuation or extensions.
_HIGH_PRIORITY_JUSTIFICATION = re.compile(
    r"(?:data[ -]?loss|security\s+(?:risk|vulnerabilit(?:y|ies)|breach|"
    r"exposure|flaw|incident)|automatic execution.{0,40}"
    r"\b(?:block(?:ed|ing|s)?|stop(?:ped|ping|s)?)\b|repeated manual recovery|"
    r"데이터\s*손실|보안\s*(?:위험|취약점?|노출|침해|문제)|"
    r"자동\s*실행.{0,20}(?:차단|중단)|반복(?:적인)?\s*수동\s*복구)",
    re.IGNORECASE | re.DOTALL,
)
_HIGH_PRIORITY_NEGATION_BEFORE = re.compile(
    r"(?:\b(?:no|not|never|without|zero|cannot|can't|doesn't|isn't|aren't|won't)\b|"
    r"\bfree\s+of\b|"
    r"\b(?:does|is|are|will|can)\s+not\b|(?:없는|아닌|않는))"
    r"[^.!?;\n]{0,48}$",
    re.IGNORECASE,
)
_HIGH_PRIORITY_NEGATION_INSIDE = re.compile(
    r"\b(?:no|not|never|without|cannot|can't|doesn't|isn't|aren't|won't)\b|"
    r"(?:없|아니|되지\s*않|하지\s*않)",
    re.IGNORECASE,
)
_HIGH_PRIORITY_NEGATION_AFTER = re.compile(
    r"^(?:\s+(?:risk|issue|threat|exposure|problem|danger|possibility))?"
    r"\s*(?:(?:does|do|is|are|was|were|will|can|could|would|has|have)\s+)?"
    r"(?:no\s+longer|not|never|none|zero|absent|unlikely|impossible|nonexistent|free|"
    r"cannot|can't|doesn't|isn't|aren't|won't)\b|"
    r"^[^.!?;\n]{0,24}(?:없|아니|되지\s*않|하지\s*않)",
    re.IGNORECASE,
)


_RESOLVED_PRIORITY_RISK = re.compile(
    r"\b(?:eliminated|resolved|mitigated|remediated|fixed|removed|addressed)\b|"
    r"(?:제거|해결|완화|해소|조치)(?:했|하였|됐|되었|된|한|됨|함)",
    re.IGNORECASE,
)


def _has_high_priority_justification(text: str) -> bool:
    """Return whether the body affirmatively states an allowed priority risk."""

    for match in _HIGH_PRIORITY_JUSTIFICATION.finditer(text):
        # Resolved risks are historical evidence. Reject ambiguous mixed claims
        # in one sentence; a separate current-risk sentence can still qualify.
        start = max(text.rfind(mark, 0, match.start()) for mark in ".!?;\n") + 1
        ends = [text.find(mark, match.end()) for mark in ".!?;\n"]
        end = min((position for position in ends if position >= 0), default=len(text))
        if _RESOLVED_PRIORITY_RISK.search(text[start:end]):
            continue
        before = text[max(0, match.start() - 48) : match.start()]
        after = text[match.end() : match.end() + 48]
        if (
            _HIGH_PRIORITY_NEGATION_BEFORE.search(before)
            or _HIGH_PRIORITY_NEGATION_INSIDE.search(match.group(0))
            or _HIGH_PRIORITY_NEGATION_AFTER.search(after)
        ):
            continue
        return True
    return False


def markdown_sections(text: str) -> MarkdownSections:
    """Parse level-two sections while rejecting ambiguous duplicate names."""

    normalized = text.replace("\r\n", "\n")
    matches = list(_SECTION.finditer(normalized))
    order: list[str] = []
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1).strip()
        if name in values:
            raise MaintenancePlanningError(
                f"ExecPlan에 중복 section이 있습니다: {name}"
            )
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        order.append(name)
        values[name] = normalized[start:end].strip()
    return MarkdownSections(tuple(order), values)


def _body_preamble(text: str) -> str:
    """Return content between frontmatter and the first level-two section."""

    normalized = text.replace("\r\n", "\n")
    lines = normalized.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\n") != "---":
        raise MaintenancePlanningError("ExecPlan frontmatter가 없습니다.")
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\n") != "---":
            continue
        body = "".join(lines[index + 1 :])
        first_section = _SECTION.search(body)
        return body[: first_section.start() if first_section else None].strip()
    raise MaintenancePlanningError("ExecPlan frontmatter 종료 구분자가 없습니다.")


def _frontmatter_without_dependencies(text: str) -> str:
    """Remove only the depends_on entry so barrier diffs stay mechanical."""

    lines = text.replace("\r\n", "\n").splitlines()
    if not lines or lines[0] != "---":
        raise MaintenancePlanningError("ExecPlan frontmatter가 없습니다.")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise MaintenancePlanningError(
            "ExecPlan frontmatter 종료 구분자가 없습니다."
        ) from error
    kept: list[str] = []
    skipping_dependencies = False
    found_dependencies = False
    for line in lines[: end + 1]:
        if re.fullmatch(r"depends_on:\s*(?:\[\])?", line):
            skipping_dependencies = True
            found_dependencies = True
            continue
        if skipping_dependencies and line.startswith("  - "):
            continue
        skipping_dependencies = False
        kept.append(line)
    if not found_dependencies:
        raise MaintenancePlanningError("ExecPlan depends_on frontmatter가 없습니다.")
    kept.extend(lines[end + 1 :])
    return "\n".join(kept).rstrip() + "\n"


def _metadata_tuple(plan: Mapping[str, object]) -> tuple[object, ...]:
    return (
        plan["id"],
        plan["title"],
        tuple(plan["sources"]),
        plan["complexity"],
        plan["risk"],
        plan["priority"],
    )


def _snapshot_metadata(snapshot: ReadyPlanSnapshot) -> tuple[object, ...]:
    return (
        snapshot.plan_id,
        snapshot.title,
        snapshot.sources,
        snapshot.complexity,
        snapshot.risk,
        snapshot.priority,
    )


def _acceptance_criteria_path_shape(
    text: str, path_moves: Mapping[str, str],
) -> str:
    """Rewrite old paths only through independently verified file moves."""

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1)
        if expression in path_moves:
            return f"`{path_moves[expression]}`"
        return match.group(0)

    return _INLINE_CODE.sub(replace, text)


def validate_barrier_revision(
    snapshot: ReadyPlanSnapshot,
    path: str,
    plan: Mapping[str, object],
    text: str,
    terminal_plan_ids: Sequence[str],
) -> None:
    if path != snapshot.path or _metadata_tuple(plan) != _snapshot_metadata(snapshot):
        raise MaintenancePlanningError(
            f"barrier mode는 계획 식별자와 metadata를 변경할 수 없습니다: {path}"
        )
    if _frontmatter_without_dependencies(text) != _frontmatter_without_dependencies(
        snapshot.text
    ):
        raise MaintenancePlanningError(
            f"barrier mode는 depends_on 이외 본문을 변경할 수 없습니다: {path}"
        )
    old_dependencies = set(snapshot.depends_on)
    new_dependencies = tuple(str(item) for item in plan["depends_on"])
    if not old_dependencies.issubset(new_dependencies):
        raise MaintenancePlanningError(
            f"barrier mode는 기존 dependency를 제거할 수 없습니다: {path}"
        )
    additions = set(new_dependencies) - old_dependencies
    expected = set(terminal_plan_ids)
    if additions != expected or len(new_dependencies) != len(set(new_dependencies)):
        raise MaintenancePlanningError(
            "barrier mode는 검증된 리팩터링 DAG 말단 전체만 "
            f"depends_on에 추가해야 합니다: {path}"
        )


def validate_revalidation_revision(
    snapshot: ReadyPlanSnapshot,
    path: str,
    plan: Mapping[str, object],
    text: str,
    *,
    path_moves: Mapping[str, str] | None = None,
) -> None:
    if path != snapshot.path or _metadata_tuple(plan) != _snapshot_metadata(snapshot):
        raise MaintenancePlanningError(
            "revalidation mode는 ID, 경로, title, sources, complexity, risk와 "
            f"priority를 변경할 수 없습니다: {path}"
        )
    previous = markdown_sections(snapshot.text)
    revised = markdown_sections(text)
    if _body_preamble(snapshot.text) != _body_preamble(text):
        raise MaintenancePlanningError(
            "revalidation mode는 허용된 section 밖의 본문을 변경할 수 "
            f"없습니다: {path}"
        )
    if previous.values.get("Purpose") != revised.values.get("Purpose"):
        raise MaintenancePlanningError(
            f"revalidation mode는 원래 Purpose를 변경할 수 없습니다: {path}"
        )
    previous_criteria = previous.values.get("Acceptance Criteria")
    revised_criteria = revised.values.get("Acceptance Criteria")
    if previous_criteria is None or revised_criteria is None:
        raise MaintenancePlanningError(
            "revalidation mode는 Acceptance Criteria section을 제거하거나 "
            f"새로 만들 수 없습니다: {path}"
        )
    if previous_criteria != _acceptance_criteria_path_shape(
        revised_criteria, {new: old for old, new in (path_moves or {}).items()}
    ):
        raise MaintenancePlanningError(
            "revalidation mode는 Acceptance Criteria의 경로 표현 이외 의미를 "
            f"변경할 수 없습니다: {path}"
        )
    allowed = {
        "Acceptance Criteria",
        "Implementation",
        "Validation",
        "Progress",
        "Decision Log",
    }
    for name in set(previous.values) | set(revised.values):
        if name in allowed:
            continue
        if previous.values.get(name) != revised.values.get(name):
            raise MaintenancePlanningError(
                "revalidation mode에서 허용되지 않은 section 변경입니다: "
                f"{path} ({name})"
            )


def terminal_new_plan_ids(
    new_plans: Sequence[tuple[str, Mapping[str, object]]],
) -> tuple[str, ...]:
    new_ids = {str(plan["id"]) for _, plan in new_plans}
    depended_on = {
        str(dependency)
        for _, plan in new_plans
        for dependency in plan["depends_on"]
        if str(dependency) in new_ids
    }
    return tuple(
        sorted(new_ids - depended_on, key=lambda value: int(value[2:]))
    )


def validate_refactoring_plan(
    path: str,
    plan: Mapping[str, object],
    text: str,
) -> None:
    sections = markdown_sections(text)
    missing = [
        name
        for name in ("Purpose", "Acceptance Criteria", "Implementation", "Validation")
        if not sections.values.get(name, "").strip()
    ]
    if missing:
        raise MaintenancePlanningError(
            f"리팩터링 ExecPlan 필수 section이 없습니다: {path} ({', '.join(missing)})"
        )
    evidence = "\n".join(
        sections.values[name]
        for name in ("Purpose", "Acceptance Criteria", "Implementation")
    )
    if not re.search(r"(?:구조|structur|책임|responsibil)", evidence, re.IGNORECASE):
        raise MaintenancePlanningError(
            f"리팩터링 계획에 구체적인 구조 문제와 책임 근거가 없습니다: {path}"
        )
    if not re.search(r"(?:경계|boundary|모듈|module)", evidence, re.IGNORECASE):
        raise MaintenancePlanningError(
            f"리팩터링 계획에 대상 책임 경계가 없습니다: {path}"
        )
    if not re.search(r"(?:보존|preserv)", evidence, re.IGNORECASE) or not re.search(
        r"(?:사용자\s*동작|공개\s*(?:계약|API)|저장\s*형식|user\s*behavio|"
        r"public\s*(?:contract|api)|storage\s*format)",
        evidence,
        re.IGNORECASE,
    ):
        raise MaintenancePlanningError(
            f"리팩터링 계획에 사용자 동작·공개 계약·저장 형식 보존 조건이 없습니다: {path}"
        )
    validation = sections.values["Validation"]
    if not re.search(r"targeted", validation, re.IGNORECASE) or not re.search(
        r"(?:전체\s*verify|full\s*verify)", validation, re.IGNORECASE
    ):
        raise MaintenancePlanningError(
            f"리팩터링 계획 Validation에 targeted unit test와 전체 verify가 필요합니다: {path}"
        )
    priority = str(plan["priority"])
    if priority == "low":
        raise MaintenancePlanningError(
            f"리팩터링 ExecPlan의 기본 priority는 normal입니다: {path}"
        )
    body = "\n".join(sections.values.values())
    if (
        priority in {"high", "critical"}
        and not _has_high_priority_justification(body)
    ):
        raise MaintenancePlanningError(
            f"상향된 리팩터링 priority의 구체적인 근거가 없습니다: {path}"
        )
