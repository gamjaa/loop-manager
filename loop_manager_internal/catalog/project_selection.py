"""catalog project selection boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import CatalogScheduleDecision
from loop_manager_internal.catalog.effective_priority import effective_priorities
from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOrchestrationError
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WORKSPACE_RESERVED
from loop_manager_internal.runtime.contracts import WORKSPACE_UNOWNED
from loop_manager_internal.storage import execplan_catalog_state
from loop_manager_internal.storage import execplan_draft_state
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import planner_check_state
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.planning.planner_check import parse_planner_check_result
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
import json
import re
import sqlite3


@dataclass(frozen=True)
class PlanBlockedExecplansEffects:
    """Named effects for plan_blocked_execplans; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def plan_blocked_execplans(
    effects: PlanBlockedExecplansEffects, connection: sqlite3.Connection, repo: str
) -> tuple[sqlite3.Row, ...]:
    effects._ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'PLAN_BLOCKED' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
    )


@dataclass(frozen=True)
class MergeWaitExecplansEffects:
    """Named effects for merge_wait_execplans; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def merge_wait_execplans(
    effects: MergeWaitExecplansEffects, connection: sqlite3.Connection, repo: str
) -> tuple[sqlite3.Row, ...]:
    effects._ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND status = 'PR_OPEN' "
            "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
            "WHERE h.repo = exec_plans.repo "
            "AND h.currently_archived = 1 "
            "AND h.plan_id = exec_plans.plan_id) "
            "ORDER BY plan_id",
            (repo,),
        ).fetchall()
    )


def mark_execplan_workspace_owned(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        try:
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                plan_id,
                plan_values={"workspace_owned": WORKSPACE_OWNED},
                enqueue=False,
            )
        except runtime_state.RuntimeStateError as error:
            raise RuntimeError(
                "workspace 소유권을 기록할 ExecPlan을 찾을 수 없습니다."
            ) from error


def reserve_execplan_workspace(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> None:
    with connection:
        try:
            runtime_state.RuntimeStateRepository(connection).reserve_workspace(
                repo,
                plan_id,
                reserved_value=WORKSPACE_RESERVED,
                allowed_values=(WORKSPACE_UNOWNED, WORKSPACE_RESERVED),
            )
        except runtime_state.RuntimeStateError as error:
            raise RuntimeError(
                "workspace 생성을 예약할 ExecPlan을 찾을 수 없습니다."
            ) from error


@dataclass(frozen=True)
class CompactExecplanCatalogEffects:
    """Named effects for compact_execplan_catalog; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def compact_execplan_catalog(effects: CompactExecplanCatalogEffects, connection: sqlite3.Connection, repo: str) -> str:
    """Serialize only non-terminal plan metadata, never plan bodies."""

    effects._ensure_execplans_schema(connection)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id)",
        (repo,),
    ).fetchall()
    rows = sorted(rows, key=lambda row: int(str(row["plan_id"])[2:]))
    held = {str(row[0]): json.loads(str(row[1])) for row in connection.execute(
        "SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo = ?", (repo,),
    )}
    drafts = execplan_draft_state.pending_ids(connection, repo)
    catalog = [
        {
            "id": row["plan_id"],
            "title": row["title"],
            "status": "CATALOG_HELD" if str(row["plan_id"]) in held else row["status"],
            "sources": json.loads(row["sources"]),
            "depends_on": json.loads(row["depends_on"]),
            "priority": row["priority"],
            **({"draft": True, "decision_wait": execplan_draft_state.prompt_evidence(
                connection, repo, str(row["plan_id"]), include_changes=False)}
               if str(row["plan_id"]) in drafts else {}),
            **({"catalog_error": held[str(row["plan_id"])]} if str(row["plan_id"]) in held else {}),
        }
        for row in rows
    ]
    return json.dumps(catalog, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class PrerequisiteValidationRowsEffects:
    """Named effects for prerequisite_validation_rows; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def prerequisite_validation_rows(
    effects: PrerequisiteValidationRowsEffects, connection: sqlite3.Connection, repo: str
) -> list[sqlite3.Row]:
    """Load active plans and only their verified, merged dependency closure."""

    effects._ensure_execplans_schema(connection)
    active = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo AND h.plan_id = exec_plans.plan_id "
        "AND h.currently_archived = 1)",
        (repo,),
    ).fetchall()
    held = scoped_blocking.catalog_held_plan_ids(connection, repo)
    active = [row for row in active if str(row["plan_id"]) not in held]
    included = {str(row["plan_id"]): row for row in active}
    pending = list(active)
    while pending:
        plan = pending.pop()
        plan_id = str(plan["plan_id"])
        try:
            dependencies = json.loads(str(plan["depends_on"]))
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
            if dependency in included:
                continue
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, dependency),
            ).fetchone()
            if row is None:
                raise MaintenanceOrchestrationError(
                    f"선행 후보의 dependency가 catalog에 없습니다: {plan_id} -> {dependency}"
                )
            history = connection.execute(
                "SELECT * FROM execplan_catalog_history "
                "WHERE repo = ? AND plan_id = ? AND currently_archived = 1",
                (repo, dependency),
            ).fetchone()
            if history is None and str(row["path"]).startswith(
                "docs/plans/archive/"
            ):
                raise MaintenanceOrchestrationError(
                    f"archived dependency 검증 실패: {plan_id} -> {dependency}: "
                    "archive identity와 Merge 증명이 없습니다"
                )
            if history is not None:
                identity_ok = (
                    bool(history["identity_verified"])
                    and re.fullmatch(
                        rf"docs/plans/\[{re.escape(dependency)}\] [^/]+\.md",
                        str(history["original_path"]),
                    ) is not None
                    and re.fullmatch(
                        rf"docs/plans/archive/\[{re.escape(dependency)}\] [^/]+\.md",
                        str(history["observed_path"]),
                    ) is not None
                    and bool(history["original_content_hash"])
                    and bool(history["final_content_hash"])
                    and bool(history["plan_sha"])
                    and row["path"] == history["observed_path"]
                    and row["content_hash"] == history["final_content_hash"]
                )
                proof_ok = (
                    bool(history["merge_confirmed"])
                    and history["proof_kind"] in {
                        kind.value for kind in execplan_catalog_state.MergeProofKind
                        if kind is not execplan_catalog_state.MergeProofKind.NONE
                    }
                    and bool(history["proof_revision"])
                    and not history["diagnostic_code"]
                )
                if row["status"] != "MERGED" or not identity_ok or not proof_ok:
                    reason = (
                        "SQLite 상태가 MERGED가 아닙니다" if row["status"] != "MERGED"
                        else "archive identity가 확인되지 않았습니다" if not identity_ok
                        else "Merge 증명이 확인되지 않았습니다"
                    )
                    raise MaintenanceOrchestrationError(
                        f"archived dependency 검증 실패: {plan_id} -> {dependency}: {reason}"
                    )
            included[dependency] = row
            pending.append(row)
    return list(included.values())


@dataclass(frozen=True)
class CatalogScheduleDecisionEffects:
    """Named effects for catalog_schedule_decision; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]
    active_execplan: Callable[..., object]
    assessment_can_yield: Callable[..., object]
    is_unowned_ready: Callable[..., object]
    select_cycle_ready_execplan: Callable[..., object]


def _unconfirmed_general_targets(
    effects: CatalogScheduleDecisionEffects, connection: sqlite3.Connection,
    repo: str, rows: list[sqlite3.Row], project: sqlite3.Row, cycle: maintenance_state.MaintenanceCycle,
) -> set[str]:
    """A failed handoff does not certify the plans flagged by its check."""
    preserved = runtime_state.PlanningSnapshot.from_row(project)
    if preserved.preserved_unconfirmed_plan_ids is not None:
        return set(preserved.preserved_unconfirmed_plan_ids)
    check = planner_check_state.get(connection, repo)
    if check is None and cycle.phase in {
        maintenance_state.MaintenancePhase.ASSESSMENT,
        maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
    } and not cycle.include_general_planning:
        return set()
    unknown = {str(row['plan_id']) for row in rows if effects.is_unowned_ready(row)}
    if check is None or check['stage'] == 'accepted':
        return {str(row['plan_id']) for row in rows}  # Legacy failures lack scope evidence.
    if check['stage'] in {'result', 'handoff', 'superseded'} and check['result']:
        try:
            decision = parse_planner_check_result(
                check['result'], frozenset(str(row['plan_id']) for row in rows),
            )
        except PlanningResultInvalid:
            return unknown
        if decision.outcome in {'needs_revision', 'uncertain'} and decision.plan_ids:
            return set(decision.plan_ids)
    # Pending/invalid checks and an uncertain result without IDs give no
    # evidence that any never-started declaration remains valid.
    return unknown


def catalog_schedule_decision(
    effects: CatalogScheduleDecisionEffects, connection: sqlite3.Connection,
    repo: str,
) -> CatalogScheduleDecision:
    """Separate active work, READY presence, and dependency-gated selection."""

    effects._ensure_execplans_schema(connection)
    active = effects.active_execplan(connection, repo)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
    ).fetchall()
    historical_confirmation = {
        str(row["plan_id"]): bool(row["merge_confirmed"]) and not row["diagnostic_code"]
        for row in connection.execute(
            """
            SELECT plan_id, merge_confirmed, diagnostic_code
            FROM execplan_catalog_history
            WHERE repo = ?
              AND currently_archived = 1
            """,
            (repo,),
        ).fetchall()
    }
    historical_ids = set(historical_confirmation)
    project_is_blocked = scoped_blocking.project_block(connection, repo) is not None
    statuses = {
        row["plan_id"]: (
            row["status"]
            if str(row["plan_id"]) not in historical_confirmation
            or historical_confirmation[str(row["plan_id"])]
            else "HISTORICAL_UNVERIFIED"
        )
        for row in rows
    }
    held = scoped_blocking.catalog_held_plan_ids(connection, repo)
    drafts = execplan_draft_state.pending_ids(connection, repo)
    ready = [
        row
        for row in rows
        if row["status"] == "READY" and str(row["plan_id"]) not in historical_ids | held | drafts
    ]
    execution_rows = [
        row for row in rows
        if str(row["plan_id"]) not in held | drafts and (str(row["plan_id"]) not in historical_ids
        or (
            historical_confirmation[str(row["plan_id"])]
            and row["status"] == "MERGED"
        ))
    ]
    priorities = effective_priorities(execution_rows)
    candidates = []
    for row in ready:
        if str(row["plan_id"]) not in priorities:
            continue
        dependencies = json.loads(row["depends_on"])
        if all(dependency not in held and statuses.get(dependency) == "MERGED" for dependency in dependencies):
            candidates.append(row)
    selected = None
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    project = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    suspended = bool(project is not None and project["planning_preserved_state"]
                     and not runtime_state.planning_owns_execution_slot(project))
    if suspended:
        affected = _unconfirmed_general_targets(effects, connection, repo, rows, project, cycle)
        if cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
            # These goals still need validation after refactoring. An error
            # releases the mutation slot, not the target's execution gate.
            affected.update(cycle.revalidation_plan_ids)
        while affected:
            dependents = {str(row["plan_id"]) for row in rows
                          if affected.intersection(json.loads(row["depends_on"]))}
            if dependents.issubset(affected):
                break
            affected.update(dependents)
        priorities = effective_priorities([row for row in execution_rows
                                           if str(row["plan_id"]) not in affected])
        candidates = [row for row in candidates if str(row["plan_id"]) in priorities]
        if active is None and not project_is_blocked and candidates:
            selected = min(candidates, key=lambda row: priorities[str(row["plan_id"])].sort_key(str(row["plan_id"])))
    elif active is None and not project_is_blocked and cycle.phase in {
        maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
        maintenance_state.MaintenancePhase.REFACTORING,
        maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
    }:
        selected = effects.select_cycle_ready_execplan(execution_rows, cycle, catalog_rows=rows)
    elif active is None and not project_is_blocked and effects.assessment_can_yield(cycle):
        resumable = [row for row in candidates if not effects.is_unowned_ready(row)]
        if resumable:
            selected = min(resumable, key=lambda row: priorities[str(row["plan_id"])].sort_key(str(row["plan_id"])))
    elif (
        active is None
        and not project_is_blocked
        and cycle.phase is maintenance_state.MaintenancePhase.IDLE
        and candidates
    ):
        selected = min(
            candidates,
            key=lambda row: priorities[str(row["plan_id"])].sort_key(str(row["plan_id"])),
        )
    return CatalogScheduleDecision(
        selection_reason=(priorities[str(selected["plan_id"])].reason(str(selected["plan_id"]))
                          if selected is not None else None),
        active=active,
        has_ready=bool(ready),
        runnable_ready=selected,
        has_plan_blocked=bool(held) or connection.execute(
            "SELECT 1 FROM manual_work_groups WHERE repo=? AND state NOT IN ('MERGED','PR_OPEN') LIMIT 1",
            (repo,)).fetchone() is not None or any(
            row["status"] in {"PLAN_BLOCKED", "MANUAL_WORKING", "MANUAL_PENDING"}
            and str(row["plan_id"]) not in historical_ids for row in rows
        ),
        has_pr_open=any(
            row["status"] == "PR_OPEN"
            and str(row["plan_id"]) not in historical_ids for row in rows
        ),
        project_blocked=project_is_blocked,
        has_draft=bool(drafts),
    )


@dataclass(frozen=True)
class SelectReadyExecplanEffects:
    """Named effects for select_ready_execplan; no entry-point/module access."""
    catalog_schedule_decision: Callable[..., object]
    operation_log: Callable[..., object]


def select_ready_execplan(effects: SelectReadyExecplanEffects, connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Apply the dependency gate before deterministic priority/ID ordering."""

    decision = effects.catalog_schedule_decision(connection, repo)
    if decision.runnable_ready is not None:
        effects.operation_log(f"READY 선택 ({repo}): {decision.selection_reason}")
    return decision.runnable_ready
