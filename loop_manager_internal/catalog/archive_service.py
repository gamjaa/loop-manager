"""catalog archive service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.catalog import execplan_archive
from loop_manager_internal.catalog import project_catalog_refresh
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import execplan_archive_state
from loop_manager_internal.storage import execplan_catalog_state
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import sqlite3
import sys


@dataclass(frozen=True)
class DispatchExecplanArchiveHousekeepingEffects:
    """Named effects for dispatch_execplan_archive_housekeeping; no entry-point/module access."""
    _ExecPlanArchiveGitPort: Callable[..., object]
    git: Callable[..., object]
    operation_print: Callable[..., object]
    reconcile_execplan_catalog: Callable[..., object]


def dispatch_execplan_archive_housekeeping(
    effects: DispatchExecplanArchiveHousekeepingEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    base_branch: str,
    *,
    catalog_refresh: project_catalog_refresh.ProjectCatalogRefresh | None = None,
    archive_safety: Callable[[], bool] | None = None,
) -> execplan_archive.ArchiveDispatchOutcome:
    """Assemble the archive service and map its result to operator logging."""

    def refresh_candidates(base_sha: str) -> bool:
        if catalog_refresh is not None:
            return (catalog_refresh.refresh(base_sha) == base_sha
                    and (archive_safety is None or archive_safety()))
        result = effects.reconcile_execplan_catalog(
            connection, repo_path, repo, base_branch
        )
        if result.kind is not execplan_catalog_state.ReconciliationKind.RECONCILED:
            return False
        current = effects.git(
            repo_path, "rev-parse",
            f"refs/remotes/origin/{base_branch}^{{commit}}",
        ).stdout.strip()
        return result.revision_sha == base_sha and current == base_sha

    git_port = effects._ExecPlanArchiveGitPort(repo_path, state_dir, repo)

    def apply_catalog(base_sha, commit_oid, candidates):
        assert catalog_refresh is not None
        expected = catalog_refresh.repository.capture(repo)
        moves = tuple(execplan_archive.ArchiveMove(
            item.plan_id, item.active_path, item.archive_path, item.content_hash, item.revision_sha,
        ) for item in candidates)
        operation_id = execplan_archive.archive_operation_id(base_sha, moves)
        stored = archive_repository.prepared_operation(repo, operation_id)
        if stored is None or stored.commit_oid != commit_oid:
            return False
        prepared = execplan_archive.PreparedArchive(
            operation_id, base_sha, stored.final_hash, commit_oid,
        )
        if not git_port.commit_matches(prepared, moves):
            return False
        return catalog_refresh.apply_archive(
            base_sha, commit_oid, candidates, expected=expected,
        )

    archive_repository = execplan_archive_state.ExecPlanArchiveRepository(
        connection, apply_catalog=apply_catalog if catalog_refresh else None,
    )
    outcome = execplan_archive.ExecPlanArchiveDispatcher(
        archive_repository,
        git_port,
        refresh_candidates=refresh_candidates,
    ).dispatch(repo, base_branch)
    if outcome.commit_oid:
        effects.operation_print(
            "MERGED ExecPlan archive 게시 완료: "
            + ", ".join(outcome.archived_plan_ids)
        )
    for plan_id, reason in outcome.pending:
        effects.operation_print(
            f"ExecPlan archive 대기 ({plan_id}): {reason}",
            file=sys.stderr,
        )
    return outcome


@dataclass(frozen=True)
class ExecplanArchiveHousekeepingSafeEffects:
    """Named effects for _execplan_archive_housekeeping_safe; no entry-point/module access."""
    active_execplan: Callable[..., object]
    catalog_schedule_decision: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def _execplan_archive_housekeeping_safe(
    effects: ExecplanArchiveHousekeepingSafeEffects, connection: sqlite3.Connection,
    repo: str,
    validated_agent_scope: object | None,
    *,
    maintenance_assessment_enabled: bool,
    refactoring_review_interval: int,
) -> bool:
    """Allow base mutation only between active project lifecycles."""

    if scoped_blocking.project_block(connection, repo) is not None:
        return False
    current = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if (
        current is None
        or current["phase"] is not None
        or runtime_state.planning_owns_execution_slot(current)
        or effects.active_execplan(connection, repo) is not None
        or validated_agent_scope is not None
    ):
        return False
    maintenance_repository = maintenance_state.MaintenanceRepository(
        connection
    )
    maintenance_cycle = maintenance_repository.get(repo)
    if maintenance_cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        return False
    if maintenance_assessment_enabled:
        decision = effects.catalog_schedule_decision(connection, repo)
        if (
            not decision.has_waiting
            and maintenance_repository.file_assessment_due(
                repo, refactoring_review_interval,
                self_repository=effects.is_loop_manager_repository(Path(repo)),
            ).required
        ):
            return False
    return True
