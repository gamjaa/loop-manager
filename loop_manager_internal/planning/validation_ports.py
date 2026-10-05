"""planning validation ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.documents import document_publication
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningContext
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.storage import maintenance_state
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class PlanningValidationPortsEffects:
    """Named effects for _PlanningValidationPorts; no entry-point/module access."""
    _maintenance_change_context_text: Callable[..., object]
    _maintenance_context_at_revision: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    maintenance_planner_prompt: Callable[..., object]
    mutable_ready_snapshot: Callable[..., object]
    planner_prompt: Callable[..., object]
    prepare_maintenance_planning_context: Callable[..., object]
    validate_maintenance_planner_result: Callable[..., object]
    validate_maintenance_publication_base: Callable[..., object]
    validate_planner_result: Callable[..., object]


class _PlanningValidationPorts:
    """Bind prompt and validation helpers without lifecycle decisions."""

    def __init__(self, effects: PlanningValidationPortsEffects, connection: sqlite3.Connection):
        self._effects = effects
        self._connection = connection

    def maintenance_context_at_revision(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> MaintenancePlanningContext:
        return self._effects._maintenance_context_at_revision(
            self._connection, repo, worktree, active_sha, cycle
        )

    def prepare_maintenance_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MaintenancePlanningContext:
        return self._effects.prepare_maintenance_planning_context(
            self._connection, repo, worktree, active_sha
        )

    def maintenance_change_context(
        self,
        repo_path: Path,
        base: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> str:
        return self._effects._maintenance_change_context_text(repo_path, base, cycle)

    def prepare_general_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MutableReadySnapshot:
        return self._effects.mutable_ready_snapshot(
            self._connection, repo, worktree, active_sha
        )

    def planner_prompt(
        self,
        error: str | None,
        catalog: str,
        mutable_snapshot: MutableReadySnapshot,
    ) -> str:
        prompt = self._effects.planner_prompt(error, catalog, mutable_snapshot)
        from loop_manager_internal.documents import document_drafts
        repository = Path(mutable_snapshot.repo)
        if self._effects.is_loop_manager_repository(repository):
            project = self._connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?",
                (mutable_snapshot.repo,),
            ).fetchone()
            if project is not None:
                candidate = document_drafts.DocumentDraftRepository(
                    self._connection,
                ).planning_candidate(
                    mutable_snapshot.repo, repository, str(project["base_branch"]),
                )
                if candidate is not None:
                    prompt += document_publication.planning_instruction(candidate)
        return prompt

    def maintenance_prompt(
        self,
        context: MaintenancePlanningContext,
        catalog: str,
        change_context: str,
        error: str | None,
        *,
        include_general_planning: bool,
    ) -> str:
        return self._effects.maintenance_planner_prompt(
            context,
            catalog,
            change_context,
            error,
            include_general_planning=include_general_planning,
        )

    def validate_general_result(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        dirty_files: list[str],
        mutable_snapshot: MutableReadySnapshot,
        *, manager_paths: tuple[str, ...] = (),
    ) -> ValidatedPlanningResult | None:
        result = self._effects.validate_planner_result(
            worktree,
            active_sha,
            dirty_files,
            connection=self._connection,
            repo=repo,
            mutable_snapshot=mutable_snapshot,
            manager_paths=manager_paths,
        )
        from loop_manager_internal.documents import document_drafts
        repository = Path(repo)
        if not self._effects.is_loop_manager_repository(repository):
            return result
        project = self._connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if project is None:
            return result
        drafts = document_drafts.DocumentDraftRepository(self._connection)
        candidate = drafts.planning_candidate(
            repo, repository, str(project["base_branch"]),
        )
        claims = []
        for path, plan in result.plans if result is not None else ():
            text = document_publication.read_plan_text(worktree / path)
            try:
                claim = document_publication.plan_claim(text)
            except document_drafts.DocumentDraftError as error:
                raise PlannerValidationError(str(error)) from error
            if claim is not None:
                claims.append((text, plan, claim))
        if candidate is not None:
            if (result is None or len(result.plans) != 1 or result.revisions
                or result.deletions or result.publication_paths != (result.plans[0][0],)
                or len(claims) != 1 or claims[0][2] != candidate.draft_id):
                raise PlannerValidationError(
                    "승인 문서 수정본에는 별도 새 ExecPlan 하나만 필요합니다."
                )
            try:
                drafts.assert_current(candidate, repository, str(project["base_branch"]))
                document_publication.validate_document_plan(
                    claims[0][0], claims[0][1], candidate,
                )
            except document_drafts.DocumentDraftError as error:
                drafts.record_publication_error(candidate.draft_id, str(error))
                raise PlannerValidationError(str(error)) from error
        elif claims:
            raise PlannerValidationError("계획할 승인 문서 수정본이 없습니다.")
        return result

    def validate_maintenance_result(
        self,
        context: MaintenancePlanningContext,
        worktree: Path,
        dirty_files: list[str],
        *,
        refactoring_plan_ids: tuple[str, ...],
        allow_general_plans: bool,
    ) -> ValidatedPlanningResult | None:
        return self._effects.validate_maintenance_planner_result(
            self._connection,
            context,
            worktree,
            dirty_files,
            refactoring_plan_ids=refactoring_plan_ids,
            allow_general_plans=allow_general_plans,
        )

    def validate_publication_base(
        self,
        repo: str,
        repo_path: Path,
        base: str,
        context: MaintenancePlanningContext,
    ) -> None:
        self._effects.validate_maintenance_publication_base(
            self._connection, repo, repo_path, base, context
        )
