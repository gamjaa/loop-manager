"""maintenance cycle service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.maintenance import maintenance_git
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.maintenance.maintenance_orchestration import cycle_scope_plan_ids
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
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
    rows = repository.plan_rows(repo)
    error = effects.cycle_plan_status_error(rows, cycle)
    if error is not None:
        raise MaintenancePlanningError(error)
    legacy_prerequisites = (cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                            and cycle.cycle_id is None and cycle.prerequisite_plan_ids)
    if (legacy_prerequisites or cycle.phase in {
        maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
        maintenance_state.MaintenancePhase.REFACTORING,
        maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
    }) and cycle.publication_stage in (None, "remote_confirmed"):
        scope = cycle_scope_plan_ids(rows, cycle)
        if catalog_held_plan_ids(connection, repo).intersection(scope):
            return cycle
        if effects.all_cycle_plans_merged(rows, scope):
            return repository.finish_cycle(repo, expected=cycle, source_sha=base_sha)
        if legacy_prerequisites:
            return repository.transition(repo, expected_phase=cycle.phase, expected_revision=cycle.revision,
                next_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT, include_general_planning=False)
    return cycle
