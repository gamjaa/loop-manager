"""Pure, bounded result contract for read-only Planner checks."""
from __future__ import annotations

from dataclasses import dataclass
import json

from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid


@dataclass(frozen=True)
class PlannerCheckDecision:
    outcome: str
    evidence: str
    reason: str
    plan_ids: tuple[str, ...]


def parse_planner_check_result(message: str, allowed_plan_ids: frozenset[str]) -> PlannerCheckDecision:
    lines = [line.removeprefix("PLANNER_CHECK_RESULT:").strip()
             for line in message.splitlines() if line.startswith("PLANNER_CHECK_RESULT:")]
    if len(lines) != 1:
        raise PlanningResultInvalid("점검 Planner 결과는 정확히 한 줄이어야 합니다.")
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    try:
        value = json.loads(lines[0], object_pairs_hook=unique_fields)
    except (ValueError, RecursionError) as error:
        raise PlanningResultInvalid("점검 Planner JSON 형식이 올바르지 않습니다.") from error
    if not isinstance(value, dict) or set(value) != {"outcome", "evidence", "reason", "plan_ids"}:
        raise PlanningResultInvalid("점검 Planner 결과 필드가 올바르지 않습니다.")
    outcome, evidence, reason, plan_ids = (value[key] for key in ("outcome", "evidence", "reason", "plan_ids"))
    if not isinstance(outcome, str) or outcome not in {"no_change", "needs_revision", "uncertain"}:
        raise PlanningResultInvalid("점검 Planner 결과 종류가 올바르지 않습니다.")
    if not isinstance(evidence, str) or not evidence.strip() or not isinstance(reason, str) or not reason.strip():
        raise PlanningResultInvalid("점검 Planner 근거와 인계 사유가 필요합니다.")
    if (not isinstance(plan_ids, list) or any(not isinstance(item, str) for item in plan_ids)
            or len(set(plan_ids)) != len(plan_ids) or not set(plan_ids) <= allowed_plan_ids):
        raise PlanningResultInvalid("점검 Planner 대상 ID가 입력 snapshot과 일치하지 않습니다.")
    if (outcome == "no_change" and plan_ids) or (outcome == "needs_revision" and not plan_ids):
        raise PlanningResultInvalid("점검 Planner 결과와 대상 ID가 일치하지 않습니다.")
    return PlannerCheckDecision(outcome, evidence.strip(), reason.strip(), tuple(plan_ids))
