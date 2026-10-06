"""planning state ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import CatalogScheduleDecision
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state, planning_input_state, planner_check_state
from loop_manager_internal.storage.planning_publication_state import GeneralPlanningPublication
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class PlanningStatePortsEffects:
    """Named effects for _PlanningStatePorts; no entry-point/module access."""
    _advance_maintenance_cycle_after_merges: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    activate_ready_execplan: Callable[..., object]
    catalog_schedule_decision: Callable[..., object]
    clear_active: Callable[..., object]
    clear_planning_validation_state: Callable[..., object]
    compact_execplan_catalog: Callable[..., object]
    general_planning_publication: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    prerequisite_validation_rows: Callable[..., object]
    preserve_planning_diagnostics: Callable[..., object]
    publish_general_planning_batch: Callable[..., object]
    publish_maintenance_planning_batch: Callable[..., object]
    record_general_planning_publication_intent: Callable[..., object]
    record_maintenance_publication_intent: Callable[..., object]
    recover_general_planning_batch: Callable[..., object]
    recover_maintenance_planning_batch: Callable[..., object]
    reconcile_execplan_catalog: Callable[..., object]
    register_execplans: Callable[..., object]
    update_project: Callable[..., object]
    preserve_remote_planning_diagnostics: Callable[..., object] | None = None


class _PlanningStatePorts:
    """Bind coordinator state transitions to the G-047 repositories."""

    def __init__(
        self,
        effects: PlanningStatePortsEffects, connection: sqlite3.Connection,
        runtime_repository: runtime_state.RuntimeStateRepository,
        maintenance_repository: maintenance_state.MaintenanceRepository,
        state_dir: Path,
    ):
        self._effects = effects
        self._connection = connection
        self._runtime_repository = runtime_repository
        self._maintenance_repository = maintenance_repository
        self._state_dir = state_dir

    def reconcile_catalog(
        self, repo: str, repository: Path, base_branch: str, revision: str,
    ) -> None:
        result = self._effects.reconcile_execplan_catalog(
            self._connection, repository, repo, base_branch,
            remote_sha=revision,
        )
        if not result.usable:
            detail = result.diagnostic.message if result.diagnostic else result.kind.value
            raise RuntimeError(f"planning catalog 갱신 대기: {detail}")

    def planning(self, repo: str) -> runtime_state.PlanningSnapshot:
        return self._runtime_repository.planning(repo)

    def suspend_planning(
        self, expected: runtime_state.PlanningSnapshot, *, unconfirmed_plan_ids: tuple[str, ...] | None = None,
    ) -> None:
        self._runtime_repository.suspend_planning(expected, unconfirmed_plan_ids=unconfirmed_plan_ids)

    def resume_planning(self, expected: runtime_state.PlanningSnapshot) -> None:
        self._runtime_repository.resume_planning(expected)

    def planning_release_pending(self, repo: str) -> bool:
        tables = {row[0] for row in self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )}
        return "agent_invocations" in tables and self._connection.execute(
            "SELECT 1 FROM agent_invocations WHERE repo = ? AND finished_at IS NULL LIMIT 1",
            (repo,),
        ).fetchone() is not None

    def general_baseline(self, repo: str) -> bool:
        return planning_input_state.has_baseline(self._connection, repo)

    def planner_check(self, repo: str) -> sqlite3.Row | None:
        return planner_check_state.get(self._connection, repo)

    def begin_planner_check(self, repo: str, **values: str) -> None:
        with self.transaction():
            planner_check_state.begin(self._connection, repo, **values)

    def advance_planner_check(
        self, repo: str, expected_stage: str, next_stage: str,
        *, expected_input: str | None = None,
        expected_target: str | None = None, **values: object,
    ) -> bool:
        with self.transaction():
            return planner_check_state.advance(
                self._connection, repo, expected_stage, next_stage,
                expected_input=expected_input, expected_target=expected_target,
                **values,
            )

    def maintenance_cycle(
        self, repo: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.get(repo)

    def update(self, repo: str, **values: object) -> None:
        self._effects.update_project(self._connection, repo, **values)

    def release_planning_correction(
        self, repo: str, *, error: str, active_sha: str, context: str,
    ) -> bool:
        return self._runtime_repository.release_planning_correction(
            repo, error=error, active_sha=active_sha, context=context,
        )

    def transaction(self):
        return runtime_state._transaction(self._connection)

    def advance_maintenance_cycle(
        self, repo: str, base_sha: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._effects._advance_maintenance_cycle_after_merges(
            self._connection, repo, base_sha
        )

    def schedule(self, repo: str) -> CatalogScheduleDecision:
        return self._effects.catalog_schedule_decision(self._connection, repo)

    def pending_document_approval(
        self, repo: str, repository: Path, base_branch: str,
    ) -> bool:
        if not self._effects.is_loop_manager_repository(repository):
            return False
        from loop_manager_internal.documents import document_drafts
        return bool(document_drafts.DocumentDraftRepository(
            self._connection,
        ).unplanned_approved(repo, repository, base_branch))

    def maintenance_due(
        self, repo: str, interval: int, *, self_repository: bool = False,
        completion_candidate: bool = False,
    ) -> maintenance_state.ReviewDue:
        return self._maintenance_repository.file_assessment_due(
            repo, interval, self_repository=self_repository,
            completion_candidate=completion_candidate,
        )

    def begin_maintenance_assessment(
        self,
        repo: str,
        interval: int,
        base_sha: str,
        *,
        include_general_planning: bool,
        self_repository: bool = False,
        completion_candidate: bool = False,
        due_override: maintenance_state.ReviewDue | None = None,
    ) -> tuple[maintenance_state.MaintenanceCycle, bool]:
        return self._maintenance_repository.begin_assessment_if_due(
            repo,
            interval,
            base_sha,
            include_general_planning=include_general_planning,
            self_repository=self_repository,
            completion_candidate=completion_candidate,
            due_override=due_override,
        )

    def settle_empty_stabilization_assessment(
        self,
        repo: str,
        interval: int,
        *,
        source_sha: str,
        repository_path: str,
        base_branch: str,
        inventory: maintenance_file_inventory.SourceInventory,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.settle_empty_stabilization_assessment(
            repo,
            interval,
            source_sha=source_sha,
            repository_path=repository_path,
            base_branch=base_branch,
            inventory=inventory,
        )

    def record_full_assessment(
        self, repo: str, *, expected_revision: int, source_sha: str, result: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.record_full_assessment(
            repo, expected_revision=expected_revision, source_sha=source_sha,
            result=result,
        )

    def restart_stale_maintenance(
        self,
        repo: str,
        expected: maintenance_state.MaintenanceCycle,
        base_sha: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.restart_stale_planning_context(
            repo,
            expected=expected,
            new_base_sha=base_sha,
        )

    def supersede_unpublished_general_intent(self, repo: str) -> None:
        intent = self.general_publication(repo)
        if intent is not None:
            if intent.stage != "prepared":
                raise RuntimeError("게시된 planning intent를 rebase로 대체할 수 없습니다.")
            self._connection.execute(
                "DELETE FROM general_planning_publications WHERE repo = ?", (repo,)
            )
            self._connection.commit()

    def supersede_unpublished_maintenance_intent(
        self, repo: str, cycle: maintenance_state.MaintenanceCycle,
        base_sha: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.restart_prepared_batch(
            repo, expected=cycle, new_base_sha=base_sha,
        )

    def activate_ready(self, repo: str, base_sha: str) -> sqlite3.Row | None:
        return self._effects.activate_ready_execplan(
            self._connection, repo, self._state_dir, base_sha
        )

    def preserve_diagnostics(
        self, repo: str, snapshot: runtime_state.PlanningSnapshot
    ) -> None:
        self._effects.preserve_planning_diagnostics(self._state_dir, repo, snapshot)
        self.retry_diagnostic_preservation(repo, snapshot)

    def retry_diagnostic_preservation(self, repo, snapshot):
        """Retry a separate artifact intent while planning remains suspended."""
        if self._effects.preserve_remote_planning_diagnostics is not None:
            self._effects.preserve_remote_planning_diagnostics(
                self._connection, self._state_dir, repo, snapshot)

    def clear_active(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None:
        self._effects.clear_active(
            self._connection,
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )

    def clear_active_in_transaction(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None:
        self._effects._clear_active_in_current_transaction(
            self._connection,
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
        )

    def plan_declarations_match(
        self, repo: str, entries: dict[str, tuple[str, str]],
    ) -> bool:
        return planning_input_state.matches(self._connection, repo, entries)

    def record_general_plan_declarations(
        self, repo: str, revision: str, entries: dict[str, tuple[str, str]],
    ) -> None:
        planning_input_state.record_general(self._connection, repo, revision, entries)

    def clear_validation(self, repo: str) -> None:
        self._effects.clear_planning_validation_state(self._connection, repo)

    def compact_catalog(self, repo: str) -> str:
        return self._effects.compact_execplan_catalog(self._connection, repo)

    def execplan_rows(self, repo: str) -> list[sqlite3.Row]:
        return self._effects.prerequisite_validation_rows(self._connection, repo)

    def transition_maintenance(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        next_phase: maintenance_state.MaintenancePhase,
        prerequisite_plan_ids: tuple[str, ...],
        include_general_planning: bool,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.transition(
            repo,
            expected_phase=expected_phase,
            expected_revision=expected_revision,
            next_phase=next_phase,
            prerequisite_plan_ids=prerequisite_plan_ids,
            include_general_planning=include_general_planning,
        )

    def record_maintenance_scope(self, repo: str, revision: int, plan_ids: tuple[str, ...],
                                 prerequisite_plan_ids: tuple[str, ...] = ()) -> None:
        self._maintenance_repository.record_assessment_plans(repo, revision, plan_ids, prerequisite_plan_ids)

    def advance_success_baseline(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        remote_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.advance_success_baseline(
            repo,
            expected_phase=expected_phase,
            expected_revision=expected_revision,
            remote_commit=remote_commit,
        )

    def file_assessment_evidence(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
    ) -> maintenance_file_publication.AssessmentEvidence:
        return maintenance_file_publication.collect(
            self._connection, repo, repository, base_branch,
            cycle_revision, source_sha,
        )

    def complete_file_assessment(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
        evidence: maintenance_file_publication.AssessmentEvidence,
    ) -> None:
        with runtime_state._transaction(self._connection):
            maintenance_file_publication.promote(
                self._connection, repo, repository, base_branch, source_sha,
                evidence,
            )
            if evidence.tiered is not None:
                return
            self._maintenance_repository.advance_file_assessment_success_baseline(
                repo, expected_revision=cycle_revision, source_sha=source_sha,
                expected_merged_count=evidence.merged_count,
                bump_revision=False,
            )

    def prepare_file_assessment_artifact(
        self, repo: str, repository: Path, base: str, context, worktree: Path, result,
    ):
        from loop_manager_internal.maintenance import code_map_artifact
        try:
            evidence = maintenance_file_publication.collect(
                self._connection, repo, repository, base, context.cycle_revision, context.active_sha,
            )
            return code_map_artifact.prepare(self._connection, repo, context, worktree, evidence, result)
        except (maintenance_file_inventory.SourceInventoryError, ValueError, OSError) as error:
            raise code_map_artifact.CodeMapArtifactError(str(error)) from error

    def recover_maintenance_batch(
        self, repo: str, worktree: Path, base: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._effects.recover_maintenance_planning_batch(
            self._connection, repo, worktree, base
        )

    def record_maintenance_intent(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._effects.record_maintenance_publication_intent(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
        )

    def publish_maintenance_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle:
        return self._effects.publish_maintenance_planning_batch(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
        )

    def register_execplans(
        self,
        repo: str,
        worktree: Path,
        plans: list[tuple[str, dict[str, object]]],
        plan_commit: str,
        fingerprint: str,
    ) -> None:
        self._effects.register_execplans(
            self._connection,
            repo,
            worktree,
            plans,
            plan_commit,
            fingerprint,
        )

    def general_publication(
        self, repo: str
    ) -> GeneralPlanningPublication | None:
        return self._effects.general_planning_publication(self._connection, repo)

    def recover_general_batch(
        self, repo: str, worktree: Path, base: str
    ) -> str:
        return self._effects.recover_general_planning_batch(
            self._connection, repo, worktree, base
        )

    def record_general_intent(
        self,
        repo: str,
        worktree: Path,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> GeneralPlanningPublication:
        return self._effects.record_general_planning_publication_intent(
            self._connection,
            repo,
            worktree,
            validated_result,
            plan_commit,
            specification_fingerprint,
            planning_fingerprint,
        )

    def publish_general_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> None:
        self._effects.publish_general_planning_batch(
            self._connection,
            repo,
            worktree,
            base,
            validated_result,
            plan_commit,
            specification_fingerprint,
            planning_fingerprint,
        )
