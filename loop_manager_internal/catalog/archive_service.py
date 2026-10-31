"""catalog archive service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from contextlib import ExitStack
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
    correction_safety: Callable[[], bool] | None = None,
    candidate_safety: Callable[[execplan_archive_state.MergedArchiveCandidate], str | None] | None = None,
) -> execplan_archive.ArchiveDispatchOutcome:
    """Assemble the archive service and map its result to operator logging."""

    def refresh_candidates(base_sha: str) -> bool:
        if catalog_refresh is not None:
            return catalog_refresh.refresh(base_sha) == base_sha
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

    # Keep the correction's source/draft/writer protections independent of
    # archive execution. Existing callers retain their shared safety gate.
    correction_safety = correction_safety or archive_safety
    if catalog_refresh is not None and correction_safety is not None:
        from loop_manager_internal.catalog.requirement_correction_publication import CorrectionPending, RequirementCorrectionPublisher
        try:
            RequirementCorrectionPublisher(
                connection, git_port, refresh=refresh_candidates, safe=correction_safety,
            ).dispatch(base_branch)
        except project_catalog_refresh.CatalogRefreshStopped:
            raise
        except Exception as error:
            raise CorrectionPending(str(error)) from error

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
        execution_safety=archive_safety,
        candidate_safety=candidate_safety,
    ).dispatch(repo, base_branch)
    if not any(reason.startswith("execution_safety_") for _, reason in outcome.pending):
        archive_repository.observe(
            repo, "dispatch", category="execution_result", resource=f"refs/heads/{base_branch}",
            reason=outcome.kind.value, resume_condition="next_safe_boundary", attempted=True,
        )
    if outcome.archived_plan_ids:
        effects.operation_print(
            "MERGED ExecPlan archive 완료 확인: "
            + ", ".join(outcome.archived_plan_ids)
        )
    for plan_id, reason in outcome.pending:
        if archive_repository.observe(
            repo, plan_id, category=_pending_category(reason), resource=plan_id,
            reason=reason, resume_condition="reobserve_candidate_and_resources_at_next_safe_boundary",
            attempted=True,
        ):
            effects.operation_print(
                f"ExecPlan archive 대기 ({plan_id}): {reason}; 다음 안전 경계에서 후보·자원 재확인",
                file=sys.stderr,
            )
    for plan_id in outcome.archived_plan_ids:
        archive_repository.observe(
            repo, plan_id, category="complete", resource=plan_id,
            reason="archived", resume_condition="none", attempted=True,
        )
    return outcome


def _pending_category(reason: str) -> str:
    if reason.startswith(("execution_safety_", "candidate_resource_")):
        return "shared_resource"
    if reason.startswith(("remote_", "pre_commit_base_", "pre_push_base_", "incomplete_candidate_observation")):
        return "remote_observation"
    if reason.startswith(("commit_preparation_", "workspace_cleanup_", "push_", "catalog_")):
        return "publication_confirmation"
    return "candidate"


class ArchiveExecutionResources:
    """Keep concrete archive leases through publication; never wait for another writer."""

    def __init__(self, connection, state_dir, repo_path, repo, base, git, leases: ExitStack, defer):
        self.connection, self.state_dir, self.repo_path = connection, state_dir, repo_path
        self.repo, self.base, self.git = repo, base, git
        self.leases, self.defer = leases, defer
        self.candidates = {}

    def _hold(self, resource, reason, resume="release_owner_and_reobserve_at_next_safe_boundary"):
        self.defer("shared_resource", resource, reason, resume)
        return False

    def _reserve(self, resources, *, reusable_owners=()):
        from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
        missing = [item for item in resources if not any(
            resource_owned(self.state_dir, self.repo, item, owner=owner)
            for owner in ("execplan-archive", *reusable_owners)
        )]
        if missing:
            self.leases.enter_context(resource_locks(
                self.state_dir, self.repo, missing, owner="execplan-archive", wait=False,
            ))

    def safe(self, *, correction_pending=True):
        from loop_manager_internal.git import manager_git_journal
        from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
        from loop_manager_internal.runtime.contracts import ProcessLockBusyError
        _check_runtime_handoff()
        connection, repo = self.connection, self.repo
        from loop_manager_internal.application.task_scheduling import current_scheduler
        scheduler = current_scheduler.get()
        if scheduler is not None and repo in scheduler.draining_projects:
            return self._hold("project", "planning_drain", "finish_planning_and_recheck_execution_eligibility")
        if connection.in_transaction or getattr(connection, "_held", False):
            return self._hold("database", "database_boundary_busy", "finish_current_database_request")
        if scoped_blocking.project_block(connection, repo) is not None:
            return self._hold("project", "project_safety_block", "resolve_actual_project_safety_block")
        current = connection.execute("SELECT base_branch FROM projects WHERE repo=?", (repo,)).fetchone()
        if current is None or current["base_branch"] != self.base:
            return self._hold("project", "base_owner_changed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, condition in (
            ("general_planning_publications", "1"),
            ("maintenance_cycles", "publication_stage IS NOT NULL"),
            ("requirement_correction_publications", "stage='prepared'"),
        ):
            if table == "requirement_correction_publications" and not correction_pending:
                continue
            if table in tables and connection.execute(
                f"SELECT 1 FROM {table} WHERE repo=? AND ({condition}) LIMIT 1", (repo,),
            ).fetchone():
                return self._hold(f"refs/heads/{self.base}", f"base_publication_reserved:{table}")
        try:
            self._reserve([f"archive:{self.base}", f"branch:{self.base}",
                           f"git:refs/heads/{self.base}", "git:fetch"])
            if manager_git_journal.has_conflicting_project_mutation(
                self.state_dir, repo, stage="archive_housekeeping", connection=connection,
                git=self.git, worktrees=(self.repo_path,),
                refs=(f"refs/heads/{self.base}", f"refs/remotes/origin/{self.base}"),
                lease_stack=self.leases,
            ):
                return self._hold("manager_git_journal", "conflicting_project_mutation",
                                  "confirm_writer_termination_and_independent_resources")
            # An independent pending rebase already owns this lease through the
            # supplied stack. Otherwise protect shared configuration ourselves.
            self._reserve(["git:config"], reusable_owners=("pending-git-scope",))
            for candidate in tuple(self.candidates.values()):
                if self.candidate_reason(candidate) is not None:
                    return self._hold(candidate.plan_id, "candidate_resource_changed")
        except ProcessLockBusyError as error:
            return self._hold("archive_leases", str(error))
        return True

    def candidate_reason(self, candidate):
        from loop_manager_internal.recovery.plan_recovery import check_stopped_writers
        from loop_manager_internal.runtime.contracts import ProcessLockBusyError
        from loop_manager_internal.storage import work_claims
        from loop_manager_internal.storage.task_calls import CallDeferred
        connection, repo = self.connection, self.repo
        row = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
                                 (repo, candidate.plan_id)).fetchone()
        if row is None or row["status"] != "MERGED":
            return "candidate_resource_definition_changed"
        try:
            if work_claims.manual_plan_ownership(connection, repo, candidate.plan_id, row)[1]:
                return "candidate_resource_manual_owner"
            resources = ["plan:" + candidate.plan_id]
            resources.extend(key + ":" + row[key] for key in ("branch", "worktree") if row[key])
            from loop_manager_internal.application.task_scheduling import current_task
            task = current_task.get()
            reusable = ("task-execution",) if task is not None and (task.repo, task.plan_id) == (repo, candidate.plan_id) else ()
            self._reserve(resources, reusable_owners=reusable)
            tables = {item[0] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            calls = tuple(dict(item) for item in connection.execute(
                "SELECT * FROM task_call_reservations WHERE repo=? AND plan_id=?", (repo, candidate.plan_id),
            )) if "task_call_reservations" in tables else ()
            invocations = tuple(dict(item) for item in connection.execute(
                "SELECT * FROM agent_invocations WHERE repo=? AND plan_id=?", (repo, candidate.plan_id),
            )) if "agent_invocations" in tables else ()
            check_stopped_writers(connection, self.state_dir, calls, invocations)
        except (ProcessLockBusyError, CallDeferred):
            return "candidate_resource_writer_or_lease_busy"
        self.candidates[candidate.plan_id] = candidate
        return None


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
