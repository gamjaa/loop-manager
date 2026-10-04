"""maintenance cycle service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.maintenance import maintenance_git
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.storage import maintenance_state
from pathlib import Path
import json
import sqlite3


def _maintenance_change_context_text(
    repo_path: Path,
    base_branch: str,
    cycle: maintenance_state.MaintenanceCycle,
) -> str:
    current = str(cycle.cycle_base_sha or "")
    baseline = str(cycle.last_success_commit or current)
    context = maintenance_git.bounded_remote_change_context(
        repo_path, base_branch, baseline, current
    )
    if not context.ok:
        if context.observation_failed:
            raise RuntimeError(
                context.error or "bounded 유지보수 변경 문맥 관측에 실패했습니다."
            )
        raise MaintenancePlanningError(
            context.error or "bounded 유지보수 변경 문맥을 만들 수 없습니다."
        )
    return json.dumps(
        {
            "baseline_commit": context.baseline_commit,
            "current_commit": context.current_commit,
            "truncated": context.truncated,
            "commits": [
                {
                    "sha": commit.sha,
                    "subject": commit.subject,
                    "paths": list(commit.paths),
                }
                for commit in context.commits
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


@dataclass(frozen=True)
class AdvanceMaintenanceCycleAfterMergesEffects:
    """Named effects for _advance_maintenance_cycle_after_merges; no entry-point/module access."""
    all_cycle_plans_merged: Callable[..., object]
    cycle_plan_status_error: Callable[..., object]


def _advance_maintenance_cycle_after_merges(
    effects: AdvanceMaintenanceCycleAfterMergesEffects, connection: sqlite3.Connection,
    repo: str,
    base_sha: str,
) -> maintenance_state.MaintenanceCycle:
    repository = maintenance_state.MaintenanceRepository(connection)
    cycle = repository.get(repo)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id "
        "AND (h.merge_confirmed = 0 OR h.diagnostic_code IS NOT NULL))",
        (repo,),
    ).fetchall()
    error = effects.cycle_plan_status_error(rows, cycle)
    if error is not None:
        raise MaintenancePlanningError(error)
    if (
        cycle.phase is maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
        and effects.all_cycle_plans_merged(rows, cycle.prerequisite_plan_ids)
    ):
        return repository.transition(
            repo,
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha=base_sha,
            include_general_planning=False,
        )
    if (
        cycle.phase is maintenance_state.MaintenancePhase.REFACTORING
        and effects.all_cycle_plans_merged(rows, cycle.refactoring_plan_ids)
    ):
        return repository.transition(
            repo,
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha=base_sha,
            include_general_planning=False,
        )
    return cycle
