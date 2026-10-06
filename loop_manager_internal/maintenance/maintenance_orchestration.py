"""Deterministic decisions for periodic maintenance cycle orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Mapping, Sequence

from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.catalog.effective_priority import effective_priorities


class MaintenanceOrchestrationError(RuntimeError):
    """Raised when a maintenance decision cannot be executed safely."""


class MaintenanceOutcome(str, Enum):
    NO_CHANGE = "no_change"
    PREREQUISITES = "prerequisites"
    REFACTORING = "refactoring"
    REVALIDATED = "revalidated"
    UNSAFE = "unsafe"


@dataclass(frozen=True)
class MaintenancePlannerDecision:
    outcome: MaintenanceOutcome
    plan_ids: tuple[str, ...] = ()
    reason: str | None = None


RESULT_PREFIX = "MAINTENANCE_RESULT: "
_PLAN_ID = re.compile(r"G-\d{3,}")


def _plan_sort_key(plan_id: str) -> tuple[int, str]:
    match = _PLAN_ID.fullmatch(plan_id)
    return (int(plan_id[2:]), plan_id) if match else (2**63 - 1, plan_id)


def parse_maintenance_planner_decision(
    output: str,
    phase: maintenance_state.MaintenancePhase,
) -> MaintenancePlannerDecision:
    """Parse the single JSON result line emitted by a maintenance Planner."""

    payloads = [
        line[len(RESULT_PREFIX) :]
        for line in output.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(payloads) != 1:
        raise MaintenanceOrchestrationError(
            "유지보수 Planner는 MAINTENANCE_RESULT JSON 줄을 정확히 하나 반환해야 합니다."
        )
    try:
        payload = json.loads(payloads[0])
    except json.JSONDecodeError as error:
        raise MaintenanceOrchestrationError(
            "MAINTENANCE_RESULT 값이 올바른 JSON object가 아닙니다."
        ) from error
    if not isinstance(payload, dict):
        raise MaintenanceOrchestrationError(
            "MAINTENANCE_RESULT 값에는 JSON object가 필요합니다."
        )
    allowed_fields = {"outcome", "plan_ids", "reason"}
    unknown = sorted(set(payload) - allowed_fields)
    if unknown:
        raise MaintenanceOrchestrationError(
            "MAINTENANCE_RESULT에 알 수 없는 field가 있습니다: "
            + ", ".join(unknown)
        )
    try:
        outcome = MaintenanceOutcome(str(payload.get("outcome", "")))
    except ValueError as error:
        raise MaintenanceOrchestrationError(
            "MAINTENANCE_RESULT outcome이 유효하지 않습니다."
        ) from error

    raw_ids = payload.get("plan_ids", [])
    if not isinstance(raw_ids, list) or any(
        not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None
        for plan_id in raw_ids
    ):
        raise MaintenanceOrchestrationError(
            "MAINTENANCE_RESULT plan_ids에는 G-000 형식 문자열 배열이 필요합니다."
        )
    if len(raw_ids) != len(set(raw_ids)):
        raise MaintenanceOrchestrationError(
            "MAINTENANCE_RESULT plan_ids에 중복 ID가 있습니다."
        )
    plan_ids = tuple(sorted(raw_ids, key=_plan_sort_key))
    reason_value = payload.get("reason")
    reason = str(reason_value).strip() if isinstance(reason_value, str) else None
    if reason_value is not None and not reason:
        raise MaintenanceOrchestrationError(
            "MAINTENANCE_RESULT reason은 비어 있지 않은 문자열이어야 합니다."
        )

    assessment_outcomes = {
        MaintenanceOutcome.NO_CHANGE,
        MaintenanceOutcome.PREREQUISITES,
        MaintenanceOutcome.REFACTORING,
    }
    revalidation_outcomes = {
        MaintenanceOutcome.REVALIDATED,
        MaintenanceOutcome.UNSAFE,
    }
    expected = (
        assessment_outcomes
        if phase is maintenance_state.MaintenancePhase.ASSESSMENT
        else revalidation_outcomes
        if phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
        else set()
    )
    if outcome not in expected:
        raise MaintenanceOrchestrationError(
            f"{phase.value} 단계에서 사용할 수 없는 유지보수 결과입니다: {outcome.value}"
        )
    if outcome in {
        MaintenanceOutcome.PREREQUISITES,
        MaintenanceOutcome.REFACTORING,
    }:
        expected_fields = {"outcome", "plan_ids"}
        if not plan_ids:
            raise MaintenanceOrchestrationError(
                f"{outcome.value} 결과에는 비어 있지 않은 plan_ids가 필요합니다."
            )
    elif outcome is MaintenanceOutcome.UNSAFE:
        expected_fields = {"outcome", "reason"}
        if reason is None:
            raise MaintenanceOrchestrationError(
                "unsafe 결과에는 진단 가능한 reason이 필요합니다."
            )
    else:
        expected_fields = {"outcome"}
        if plan_ids:
            raise MaintenanceOrchestrationError(
                f"{outcome.value} 결과에는 plan_ids를 기록할 수 없습니다."
            )
    if set(payload) != expected_fields:
        raise MaintenanceOrchestrationError(
            f"{outcome.value} 결과 field는 다음과 정확히 일치해야 합니다: "
            + ", ".join(sorted(expected_fields))
        )
    return MaintenancePlannerDecision(outcome, plan_ids, reason)


def maintenance_planning_fingerprint(
    cycle: maintenance_state.MaintenanceCycle,
    general_fingerprint: str,
) -> str:
    """Bind one planning attempt to the cycle revision, mode, and base SHA."""

    if cycle.phase not in {
        maintenance_state.MaintenancePhase.ASSESSMENT,
        maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
    } or not cycle.cycle_base_sha:
        raise MaintenanceOrchestrationError(
            "유지보수 planning fingerprint를 만들 활성 planning 단계가 없습니다."
        )
    digest = hashlib.sha256()
    digest.update(b"loop-manager-maintenance-planning-v1\0")
    values = (
        cycle.repo,
        cycle.phase.value,
        str(cycle.revision),
        cycle.cycle_base_sha,
        "combined" if cycle.include_general_planning else "maintenance-only",
        general_fingerprint if cycle.include_general_planning else "",
    )
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def is_unowned_ready(row: Mapping[str, object]) -> bool:
    """Recognize never-started declarations without execution ownership."""

    return (
        str(row["status"]) == "READY"
        and not bool(row["ever_started"])
        and all(row[field] is None for field in (
            "branch", "worktree", "worker_session_id", "reviewer_session_id",
            "pr_url", "pr_head_oid", "pr_conflict_head_oid", "pr_conflict_base_oid",
        ))
        and all(int(row[field] or 0) == 0 for field in (
            "workspace_owned", "pr_conflict_recovery",
        ))
    )


def assessment_can_yield(cycle: maintenance_state.MaintenanceCycle) -> bool:
    """Published maintenance sets retain their execution barrier."""

    return (
        cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
        and cycle.publication_stage is None
        and not cycle.prerequisite_plan_ids
        and not cycle.refactoring_plan_ids
        and not cycle.revalidation_plan_ids
    )


def validate_prerequisite_plan_ids(
    rows: Sequence[Mapping[str, object]],
    plan_ids: Sequence[str],
) -> tuple[str, ...]:
    """Require an unowned READY set closed over every unmerged dependency."""

    selected = tuple(sorted(plan_ids, key=_plan_sort_key))
    by_id = {str(row["plan_id"]): row for row in rows}
    unknown = [plan_id for plan_id in selected if plan_id not in by_id]
    if unknown:
        raise MaintenanceOrchestrationError(
            "선행 집합에 catalog에 없는 plan ID가 있습니다: " + ", ".join(unknown)
        )
    selected_set = set(selected)
    for plan_id in selected:
        row = by_id[plan_id]
        ownership = (
            row["branch"],
            row["worktree"],
            int(row["workspace_owned"] or 0),
            row["worker_session_id"],
            row["reviewer_session_id"],
            row["pr_url"],
            row["pr_head_oid"],
            int(row["pr_conflict_recovery"] or 0),
            row["pr_conflict_head_oid"],
            row["pr_conflict_base_oid"],
        )
        if str(row["status"]) != "READY" or ownership != (
            None,
            None,
            0,
            None,
            None,
            None,
            None,
            0,
            None,
            None,
        ):
            raise MaintenanceOrchestrationError(
                f"선행 후보는 무소유 READY 계획이어야 합니다: {plan_id}"
            )
        try:
            dependencies = json.loads(str(row["depends_on"]))
        except json.JSONDecodeError as error:
            raise MaintenanceOrchestrationError(
                f"선행 후보 dependency를 읽을 수 없습니다: {plan_id}"
            ) from error
        if not isinstance(dependencies, list) or any(
            not isinstance(dependency, str) for dependency in dependencies
        ):
            raise MaintenanceOrchestrationError(
                f"선행 후보 dependency 형식이 유효하지 않습니다: {plan_id}"
            )
        for dependency in dependencies:
            dependency_row = by_id.get(dependency)
            if dependency_row is None:
                raise MaintenanceOrchestrationError(
                    f"선행 후보의 dependency가 catalog에 없습니다: {plan_id} -> {dependency}"
                )
            if (
                str(dependency_row["status"]) != "MERGED"
                and dependency not in selected_set
            ):
                raise MaintenanceOrchestrationError(
                    "선행 집합이 unmerged dependency에 대해 닫혀 있지 않습니다: "
                    f"{plan_id} -> {dependency}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(plan_id: str) -> None:
        if plan_id in visiting:
            raise MaintenanceOrchestrationError(
                "선행 집합에 dependency cycle이 있습니다."
            )
        if plan_id in visited:
            return
        visiting.add(plan_id)
        dependencies = json.loads(str(by_id[plan_id]["depends_on"]))
        for dependency in dependencies:
            if dependency in selected_set:
                visit(dependency)
        visiting.remove(plan_id)
        visited.add(plan_id)

    for plan_id in selected:
        visit(plan_id)
    return selected


def select_cycle_ready_execplan(
    rows: Sequence[Mapping[str, object]],
    cycle: maintenance_state.MaintenanceCycle,
    *, catalog_rows: Sequence[Mapping[str, object]] | None = None,
) -> Mapping[str, object] | None:
    """Select only the current cycle's runnable READY plan, if any."""

    priorities = effective_priorities(rows)
    scope_rows = rows if catalog_rows is None else catalog_rows
    if cycle.phase is maintenance_state.MaintenancePhase.PREREQUISITE_WAIT:
        allowed = cycle_scope_plan_ids(scope_rows, cycle)
        order = lambda row: priorities[str(row["plan_id"])].sort_key(str(row["plan_id"]))
    elif cycle.phase in {maintenance_state.MaintenancePhase.REFACTORING,
                         maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION}:
        allowed = cycle_scope_plan_ids(scope_rows, cycle)
        order = lambda row: _plan_sort_key(str(row["plan_id"]))
    else:
        return None
    by_id = {str(row["plan_id"]): row for row in rows}
    candidates = []
    for plan_id in allowed:
        row = by_id.get(plan_id)
        if row is None or plan_id not in priorities or str(row["status"]) != "READY":
            continue
        dependencies = json.loads(str(row["depends_on"]))
        if all(
            dependency in by_id and str(by_id[dependency]["status"]) == "MERGED"
            for dependency in dependencies
        ):
            candidates.append(row)
    return min(candidates, key=order) if candidates else None


def cycle_plan_status_error(
    rows: Sequence[Mapping[str, object]],
    cycle: maintenance_state.MaintenanceCycle,
) -> str | None:
    """Return a diagnostic when a cycle plan set cannot make safe progress."""

    legacy_prerequisites = (cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                            and cycle.cycle_id is None and cycle.prerequisite_plan_ids)
    if not legacy_prerequisites and cycle.phase not in {maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                           maintenance_state.MaintenancePhase.REFACTORING,
                           maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION}:
        return None
    try:
        plan_ids = cycle_scope_plan_ids(rows, cycle)
    except MaintenanceOrchestrationError as error:
        return str(error)
    label = "확정 범위"
    if not plan_ids:
        return "기존 유지보수 cycle의 확정 범위와 종료 경계를 확인할 수 없습니다."
    by_id = {str(row["plan_id"]): row for row in rows}
    for plan_id in plan_ids:
        row = by_id.get(plan_id)
        if row is None:
            return f"활성 유지보수 주기의 {label} 계획이 catalog에 없습니다: {plan_id}"
        status = str(row["status"])
        if status not in {"READY", "MERGED", "PR_OPEN", "PLAN_BLOCKED"}:
            return f"활성 유지보수 주기의 {label} 계획 상태가 유효하지 않습니다: {plan_id} ({status})"
    return None


def cycle_scope_plan_ids(rows, cycle) -> tuple[str, ...]:
    """The fixed finding targets plus their required dependency closure."""
    roots = set(cycle.prerequisite_plan_ids) | set(cycle.refactoring_plan_ids) | set(cycle.assessment_plan_ids)
    return dependency_plan_ids(rows, roots)


def dependency_plan_ids(rows, roots) -> tuple[str, ...]:
    """Validate the catalog identities and acyclic closure of required targets."""
    by_id = {str(row["plan_id"]): row for row in rows}
    visited, visiting = set(), set()
    def visit(plan_id):
        if plan_id in visiting:
            raise MaintenanceOrchestrationError(f"유지보수 dependency 순환: {plan_id}")
        if plan_id in visited:
            return
        row = by_id.get(plan_id)
        if row is None:
            raise MaintenanceOrchestrationError(f"유지보수 확정 범위 dependency가 catalog에 없습니다: {plan_id}")
        visiting.add(plan_id)
        try:
            dependencies = json.loads(str(row["depends_on"]))
        except (ValueError, TypeError) as error:
            raise MaintenanceOrchestrationError(f"유지보수 dependency 형식 오류: {plan_id}") from error
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise MaintenanceOrchestrationError(f"유지보수 dependency 형식 오류: {plan_id}")
        for dependency in dependencies:
            visit(dependency)
        visiting.remove(plan_id)
        visited.add(plan_id)
    for plan_id in roots:
        visit(plan_id)
    return tuple(sorted(visited, key=_plan_sort_key))


def all_cycle_plans_merged(
    rows: Sequence[Mapping[str, object]], plan_ids: Sequence[str]
) -> bool:
    by_id = {str(row["plan_id"]): str(row["status"]) for row in rows}
    return bool(plan_ids) and all(by_id.get(plan_id) == "MERGED" for plan_id in plan_ids)
