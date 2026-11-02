"""application project outcomes boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
from loop_manager_internal.application.contracts import PROJECT_STEP_USAGE_PAUSE
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.planning.planning_runtime import PlanningOutcome
from loop_manager_internal.planning.planning_runtime import PlanningOutcomeKind
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY
from loop_manager_internal.storage import maintenance_state, planning_input_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import hashlib
import sqlite3


def _planning_outcome_step_result(outcome: PlanningOutcome) -> int | None:
    """Translate a typed coordinator result at the process adapter boundary."""

    if outcome.kind in {
        PlanningOutcomeKind.EXECUTION_READY,
        PlanningOutcomeKind.REFACTORING,
    }:
        return None
    if outcome.kind in {
        PlanningOutcomeKind.FINGERPRINT_SKIPPED,
        PlanningOutcomeKind.NO_CHANGE,
        PlanningOutcomeKind.WAITING,
    }:
        return 0
    if outcome.kind in {
        PlanningOutcomeKind.PREREQUISITE_WAIT,
        PlanningOutcomeKind.REMOTE_BASE_RESTART,
        PlanningOutcomeKind.PUBLICATION_RECOVERY,
    }:
        return PROJECT_STEP_RETRY
    if outcome.kind is PlanningOutcomeKind.TRANSIENT_PAUSE:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is PlanningOutcomeKind.USAGE_LIMIT_PAUSE:
        return PROJECT_STEP_USAGE_PAUSE
    return 1


def _checkpoint_dispatch_ready(
    connection: sqlite3.Connection,
    repo: str,
) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'blocked_checkpoints')"
        )
    }
    if tables != {"projects", "blocked_checkpoints"}:
        return False
    return connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is not None


@dataclass(frozen=True)
class RecordProjectRecoveryEffects:
    """Named effects for _record_project_recovery; no entry-point/module access."""
    project_execplan: Callable[..., object]


def _record_project_recovery(
    effects: RecordProjectRecoveryEffects, connection: sqlite3.Connection,
    repo: str,
    reason: recovery_runtime.RecoveryReason,
    detail: str,
    *,
    attempts: int = 0,
) -> recovery_runtime.RecoveryDecision:
    """Persist a project/PR operation failure without changing execution state."""

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('projects', 'exec_plans')"
        )
    }
    project = (
        connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
        if "projects" in tables else None
    )
    plan = (
        effects.project_execplan(connection, repo)
        if project is not None and "exec_plans" in tables else None
    )
    phase = str(project["phase"] or "") if project is not None else ""
    active_sha = str(project["active_sha"] or "") if project is not None else ""
    branch = str(project["branch"] or "") if project is not None else ""
    marker = f"{active_sha}|{branch}|{project['worker_session_id'] if project is not None else ''}"
    fingerprint = hashlib.sha256(
        f"{reason.value}|{phase}|{active_sha}|{branch}".encode()
    ).hexdigest()
    observation = recovery_runtime.RecoveryObservation(
        reason=reason, repo=repo,
        plan_id=str(plan["plan_id"]) if plan is not None else None,
        phase=phase,
        plan_revision=(str(plan["current_revision_sha"] or plan["plan_sha"] or "")
                       if plan is not None else None),
        base_revision=active_sha, fingerprint=fingerprint,
        progress_marker=marker, detail=detail,
    )
    with runtime_state._transaction(connection):
        return recovery_state.RecoveryRepository(connection).observe(
            observation, attempts=attempts,
        ).decision


def _blocked_recovery_subjects(
    connection: sqlite3.Connection, repo: str,
) -> frozenset[str]:
    """Return subjects whose final BLOCKED event owns this failure instead."""

    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('execplan_blocks', 'project_blocks')"
        )
    }
    subjects: set[str] = set()
    if "execplan_blocks" in tables:
        subjects.update(
            str(row[0]) for row in connection.execute(
                "SELECT plan_id FROM execplan_blocks WHERE repo = ?", (repo,)
            )
        )
    if "project_blocks" in tables:
        subjects.update(
            str(row[0]) if row[0] is not None else "__project__"
            for row in connection.execute(
                "SELECT plan_id FROM project_blocks WHERE repo = ?", (repo,)
            )
        )
    return frozenset(subjects)


@dataclass(frozen=True)
class StableWorkReasonEffects:
    """Named effects for _stable_work_reason; no entry-point/module access."""
    _nonplan_input_fingerprint: Callable[..., object]
    _git_tree_entries: Callable[..., object]
    _is_active_requirement: Callable[..., object]
    _is_requirement_archive_only_change: Callable[..., object]
    _planning_input_fingerprint: Callable[..., object]
    _specification_input_fingerprint: Callable[..., object]
    plan_declarations: Callable[..., object]
    catalog_schedule_decision: Callable[..., object]
    git: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    git_bytes: Callable[..., bytes] | None = None


def _stable_work_reason(
    effects: StableWorkReasonEffects, connection: sqlite3.Connection, repo: str, *,
    refactoring_review_interval: int,
) -> work_exhaustion.WorkReasonSnapshot | None:
    """Confirm that the latest remote selection has no runnable work."""

    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if row is None or row["phase"] not in {None, "PR_OPEN", "PLAN_BLOCKED", "PROJECT_BLOCKED"}:
        return None
    if row["pr_conflict_recovery"]:
        return None
    if row["planning_error"]:
        if (row["planning_error_origin"] != "validation"
                or not row["planning_preserved_state"]
                or runtime_state.planning_owns_execution_slot(row)
                or not row["planning_error_fingerprint"]):
            return None
        try:
            preserved = runtime_state._preserved_planning_values(row["planning_preserved_state"])
        except runtime_state.RuntimeStateError:
            return None
        if preserved["planning_validation_error"] != row["planning_error"]:
            return None
    if not row["planning_fingerprint"]:
        return None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'general_planning_publications'"
    ).fetchone() is not None and connection.execute(
        "SELECT 1 FROM general_planning_publications WHERE repo = ?", (repo,),
    ).fetchone() is not None:
        return None
    if row["phase"] == "PROJECT_BLOCKED" and connection.execute(
        "SELECT 1 FROM project_blocks WHERE repo=? AND reason_code IS NOT NULL AND evidence IS NOT NULL "
        "AND reason_code != '' AND evidence != ''", (repo,),
    ).fetchone() is None:
        return None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
    ).fetchone() and connection.execute(
        "SELECT 1 FROM recovery_records WHERE repo=? AND pending_action!='final_block' LIMIT 1", (repo,),
    ).fetchone():
        return None
    schedule = effects.catalog_schedule_decision(connection, repo)
    if schedule.active is not None or schedule.runnable_ready is not None:
        return None
    maintenance = maintenance_state.MaintenanceRepository(connection)
    cycle = maintenance.get(repo)
    if (
        cycle.phase is not maintenance_state.MaintenancePhase.IDLE
        or cycle.publication_stage is not None
        or maintenance.file_assessment_due(
            repo, refactoring_review_interval,
            self_repository=effects.is_loop_manager_repository(Path(repo)),
        ).required
    ):
        return None
    repo_path = Path(repo)
    revision = f"refs/remotes/origin/{row['base_branch']}"
    base_sha = effects.git(
        repo_path, "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).stdout.strip()
    current_input = effects._planning_input_fingerprint(repo_path, base_sha)
    current_specification = effects._specification_input_fingerprint(repo_path, base_sha)
    if row["planning_error"] and row["planning_error_fingerprint"] != current_input:
        return None
    current_plans = effects.plan_declarations(repo_path, base_sha)
    if not planning_input_state.matches(connection, repo, current_plans):
        return None
    archive_only = False
    if current_input != row["planning_fingerprint"] and row["processed_sha"]:
        archive_only = effects._is_requirement_archive_only_change(
            repo_path, str(row["processed_sha"]), base_sha,
            verified_plan_declarations=True,
        )
    if current_input != row["planning_fingerprint"] and not (
        row["processed_sha"] and (
            archive_only or effects._nonplan_input_fingerprint(
                repo_path, str(row["processed_sha"])
            ) == effects._nonplan_input_fingerprint(repo_path, base_sha)
        )
    ):
        return None
    if row["specification_fingerprint"] is not None:
        if (
            current_specification != row["specification_fingerprint"]
            and not archive_only
            and not (
                row["processed_sha"]
                and effects._specification_input_fingerprint(
                    repo_path, str(row["processed_sha"])
                ) == current_specification
            )
        ):
            return None
    active_requirements = tuple(
        path.decode("utf-8") for path in effects._git_tree_entries(repo_path, base_sha)
        if effects._is_active_requirement(path)
    )
    requirement_documents = None
    if effects.git_bytes is not None and active_requirements:
        from loop_manager_internal.git.command_adapter import revision_requirement_documents
        try:
            requirement_documents = revision_requirement_documents(
                effects.git_bytes, repo_path, base_sha,
            )
        except RuntimeError:
            return None
    snapshot = work_exhaustion.collect(
        connection, repo, cycle=str(row["planning_fingerprint"]),
        active_requirements=active_requirements,
        requirement_documents=requirement_documents, base_revision=base_sha,
    )
    if snapshot is not None and snapshot.completed:
        from loop_manager_internal.storage import planning_review_state
        from loop_manager_internal.planning.specification_review import current_receipt
        if row["phase"] or not current_receipt(
            planning_review_state.current_final(connection, repo),
            repo=repo_path, revision=base_sha, input_fingerprint=current_input,
            specification_fingerprint=current_specification,
            work_cycle=planning_review_state.work_cycle(connection, repo),
            archive_only=effects._is_requirement_archive_only_change,
        ):
            return None
    return snapshot
