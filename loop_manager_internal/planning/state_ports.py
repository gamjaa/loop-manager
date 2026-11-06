"""planning state ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import CatalogScheduleDecision
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state, planning_input_state, planner_check_state, planning_review_state
from loop_manager_internal.storage.planning_publication_state import GeneralPlanningPublication
from loop_manager_internal.storage import planning_publication_state, execplan_catalog_state
from loop_manager_internal.storage.db_access import run_database_request
from loop_manager_internal.planning.planning_contracts import PlanningPublicationRelease
from loop_manager_internal.planning.planning_workspace import legacy_release_identity
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
    preserve_remote_planning_diagnostics: Callable[..., object] | None = None
    planning_result_from_manifest: Callable[..., object] | None = None
    deletion_resources_available: Callable[..., bool] | None = None
    pr_for_branch: Callable[..., object] | None = None


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
            remote_sha=revision, state_dir=self._state_dir,
        )
        if not result.usable:
            detail = result.diagnostic.message if result.diagnostic else result.kind.value
            raise RuntimeError(f"planning catalog 갱신 대기: {detail}")

    def planning(self, repo: str) -> runtime_state.PlanningSnapshot:
        return self._runtime_repository.planning(repo)

    def retain_planning_after_publication(self, repo: str, revision: str, **values) -> None:
        planning_publication_state.retain_planning_after_publication(self._connection, repo, revision, **values)

    def pending_publication_release(self, repo: str) -> PlanningPublicationRelease | None:
        with self.transaction():
            project = self._connection.execute("SELECT * FROM projects WHERE repo=?", (repo,)).fetchone()
            if project is None or project['phase'] != 'PLANNING' or not project['publication_remote_oid']:
                return None
            if not (project['worktree'] and project['planning_attempt_fingerprint']
                    and project['active_sha'] and project['planning_workspace_owned'] == 1):
                raise runtime_state.RuntimeStateError('게시 후 planning 정리의 소유 근거가 없습니다.')
            cycle = self._maintenance_repository.get(repo)
            intent = self.general_publication(repo)
            revision = str(project['publication_remote_oid'])
            if intent is not None:
                requirement_review = planning_review_state.get(self._connection, repo, 'requirement')
                requirement_applied = (requirement_review is not None
                    and requirement_review['revision'] == intent.base_sha
                    and requirement_review['confirmed_revision'] == revision
                    and requirement_review['result'] is not None and requirement_review['outcome'] is not None)
                general_applied = (
                    project['processed_sha'], project['specification_fingerprint'], project['planning_fingerprint'],
                ) == (revision, intent.specification_fingerprint, intent.planning_fingerprint)
                if (revision != intent.commit_sha or project['active_sha'] != intent.base_sha
                        or not (requirement_applied or general_applied)):
                    raise runtime_state.RuntimeStateError('planning 정리와 publication intent가 다릅니다.')
            elif revision not in {project['active_sha'], cycle.publication_remote_oid, cycle.last_cycle_commit,
                                  cycle.publication_commit_sha if cycle.publication_stage == 'prepared' else None}:
                raise runtime_state.RuntimeStateError('planning 정리의 적용된 게시 근거가 없습니다.')
            if (intent is None and cycle.publication_stage == 'prepared'
                    and revision == cycle.publication_commit_sha
                    and project['active_sha'] != cycle.publication_base_sha):
                raise runtime_state.RuntimeStateError('재평가 정리와 publication 원본 기준이 다릅니다.')
            catalog = execplan_catalog_state.DefinitionStateRepository(self._connection).capture(repo)
            tables = {row[0] for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()}
            manual = tuple((table, tuple(tuple(row) for row in self._connection.execute(
                f"SELECT * FROM {table} WHERE repo=? ORDER BY rowid", (repo,),
            ).fetchall())) for table in ('manual_work_groups', 'manual_work_members') if table in tables)
            return PlanningPublicationRelease(repo, Path(str(project['worktree'])), str(project['active_sha']),
                revision, str(project['planning_attempt_fingerprint']), (tuple(project), cycle, intent, catalog, manual),
                restart=(intent is None and cycle.publication_stage == 'prepared'
                         and revision == cycle.publication_commit_sha))

    def validate_publication_release(self, release: PlanningPublicationRelease) -> None:
        if (self.pending_publication_release(release.repo) != release
                or self.planning_release_pending(release.repo)):
            raise runtime_state.RuntimeStateError('게시 후 planning 정리의 소유권·catalog·호출 상태가 변경됐습니다.')

    def complete_publication_release(self, release: PlanningPublicationRelease, *, restart_revision: str | None = None) -> None:
        def apply(_connection):
            self.validate_publication_release(release)
            if release.restart:
                if not restart_revision:
                    raise runtime_state.RuntimeStateError('재평가 정리의 원격 기준이 없습니다.')
                self._maintenance_repository.restart_prepared_batch(
                    release.repo, expected=release.identity[1], new_base_sha=restart_revision,
                )
            self.clear_active_in_transaction(release.repo)
            planning_publication_state.delete_general_planning_publication(self._connection, release.repo)
        run_database_request(self._connection, apply)

    def pause_execution_for_planning(self, expected, plan_id: str) -> None:
        if self.planning_release_pending(expected.repo):
            raise runtime_state.RuntimeStateError('미종료 Agent 실행이 남아 있습니다.')
        self._runtime_repository.pause_execution_for_planning(expected, plan_id)

    def resume_execution_after_planning(self, repo: str) -> None:
        self._runtime_repository.resume_execution_after_planning(repo)

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
        return ("task_call_reservations" in tables and self._connection.execute(
            "SELECT 1 FROM task_call_reservations WHERE repo = ? LIMIT 1", (repo,),
        ).fetchone() is not None) or ("agent_invocations" in tables and self._connection.execute(
            "SELECT 1 FROM agent_invocations WHERE repo = ? AND finished_at IS NULL LIMIT 1",
            (repo,),
        ).fetchone() is not None)

    def prepare_legacy_release(self, expected: runtime_state.PlanningSnapshot,
                               cycle: maintenance_state.MaintenanceCycle) -> maintenance_state.MaintenanceCycle:
        with self.transaction():
            if (self.planning(expected.repo) != expected or self.planning_release_pending(expected.repo)
                    or expected.phase != "PLANNING" or expected.planning_workspace_owned != 1
                    or not expected.worktree or not expected.planning_attempt_fingerprint
                    or expected.planning_result_completed):
                raise runtime_state.RuntimeStateError("workspace 해제 전 planning 입력 또는 실행이 변경됐습니다.")
            return self._maintenance_repository.record_legacy_release(expected.repo, expected=cycle,
                identity=legacy_release_identity(expected, cycle))

    def complete_legacy_release(self, expected: runtime_state.PlanningSnapshot,
                                cycle: maintenance_state.MaintenanceCycle) -> None:
        with self.transaction():
            if (self.planning(expected.repo) != expected or self.planning_release_pending(expected.repo)
                    or cycle.legacy_release_identity != legacy_release_identity(expected, cycle)):
                raise runtime_state.RuntimeStateError("workspace 해제 후 planning 입력 또는 실행이 변경됐습니다.")
            self._maintenance_repository.clear_legacy_release(expected.repo, expected=cycle)
            self.clear_active_in_transaction(expected.repo)

    def general_baseline(self, repo: str) -> bool:
        return planning_input_state.has_baseline(self._connection, repo)

    def general_reference_revision(self, repo: str) -> str | None:
        if not self.general_baseline(repo):
            return None
        row = self._connection.execute('SELECT reference_revision FROM planning_input_baselines '
            'WHERE repo = ? AND scope = ?', (repo, planning_input_state.SCOPE)).fetchone()
        return row[0] if row else None

    def planning_review(self, repo: str, slot: str):
        if slot == "final":
            return planning_review_state.current_final(self._connection, repo)
        return planning_review_state.get(self._connection, repo, slot)

    def review_work_cycle(self, repo: str) -> str:
        return planning_review_state.work_cycle(self._connection, repo)

    def review_validation_context(self, repo: str) -> str:
        import json
        return json.dumps(planning_review_state.validation_evidence(self._connection, repo), ensure_ascii=False)

    def final_review_candidate(self, repo: str, interval: int, *, self_repository: bool = False) -> bool:
        from loop_manager_internal.notifications import work_exhaustion
        row = self.planning(repo)
        schedule = self.schedule(repo)
        cycle = self.maintenance_cycle(repo)
        snapshot = work_exhaustion.collect(self._connection, repo, cycle="", active_requirements=())
        return bool(snapshot is not None and snapshot.completed
            and not schedule.project_blocked and schedule.active is None
            and not schedule.has_ready and not schedule.has_waiting
            and not row.planning_error and not row.planning_preserved_state
            and cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and cycle.publication_stage is None and self.general_publication(repo) is None
            and not self.maintenance_due(repo, interval, self_repository=self_repository).required)

    def begin_planning_review(self, repo: str, **values) -> None:
        with self.transaction():
            planning_review_state.begin(self._connection, repo, **values)

    def bind_common_defect_snapshot(self, repo: str, revision: str, report_ids: list[int]) -> None:
        with self.transaction():
            planning_review_state.bind_common_defect_snapshot(self._connection, repo, revision, report_ids)

    def validate_planning_review(self, repo: str, revision: str, result: str, outcome: str,
                                 *, common_defect_links=None) -> None:
        with self.transaction():
            planning_review_state.validate(self._connection, repo, revision, result, outcome,
                                           common_defect_links=common_defect_links)

    def confirm_planning_review(self, repo: str, reviewed_revision: str, confirmed_revision: str) -> None:
        planning_review_state.confirm(self._connection, repo, reviewed_revision, confirmed_revision)

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

    def record_planning_result(self, repo, identity, result):
        from loop_manager_internal.notifications import notifications
        plans = [{"id": str(plan["id"]), "title": str(plan["title"])}
                 for _path, plan in (result.plans if result else ())]
        with self._connection:
            notifications.enqueue_progress(
                self._connection, repo, "planning_result", identity + "\0" + repr(plans),
                plans=plans, status="작성 완료 · 미게시" if plans else "새 계획 없음", silent=True,
            )

    def flush_notifications(self, repo: str) -> None:
        from loop_manager_internal.notifications import notifications
        notifications.flush_project_events(self._connection, repo)

    def update(self, repo: str, **values: object) -> None:
        self._runtime_repository.update_project(repo, values)
        self.flush_notifications(repo)

    def local_recheck_consumed(self, request_id: str) -> bool:
        from loop_manager_internal.storage import local_recovery_state
        return local_recovery_state.recheck_consumed(self._connection, request_id, "planning", "planner")

    def update_local_recheck_result(self, repo: str, request_id: str, **values: object) -> None:
        from loop_manager_internal.storage import local_recovery_state
        with self.transaction():
            self.update(repo, **values)
            local_recovery_state.consume_recheck(self._connection, request_id, "planning", "planner", repo=repo)

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
        cycle = self._effects._advance_maintenance_cycle_after_merges(
            self._connection, repo, base_sha
        )
        self.flush_notifications(repo)
        return cycle

    def schedule(self, repo: str) -> CatalogScheduleDecision:
        return self._effects.catalog_schedule_decision(self._connection, repo)

    def pending_user_decision(self, repo: str, entries) -> bool:
        from loop_manager_internal.storage import execplan_draft_state
        return execplan_draft_state.needs_semantic_review(self._connection, repo, entries)

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
        result = self._maintenance_repository.begin_assessment_if_due(
            repo,
            interval,
            base_sha,
            include_general_planning=include_general_planning,
            self_repository=self_repository,
            completion_candidate=completion_candidate,
            due_override=due_override,
        )
        self.flush_notifications(repo)
        return result

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

    def consume_owned_records(self, repo, revision, entries) -> None:
        planning_input_state.consume_owned_records(self._connection, repo, revision, entries)

    def record_general_plan_declarations(
        self, repo: str, revision: str, entries: dict[str, tuple[str, str]],
        *, guidance_hash: str | None = None,
    ) -> None:
        planning_input_state.record_general(self._connection, repo, revision, entries, guidance_hash=guidance_hash)

    def publication_guidance_hash(self, repo: str) -> str:
        from loop_manager_internal.agents import common_guidance
        return common_guidance.planning_content_hash(self._connection, repo) or common_guidance.runtime_content_hash()

    def clear_validation(self, repo: str) -> None:
        self._effects.clear_planning_validation_state(self._connection, repo)

    def compact_catalog(self, repo: str) -> str:
        return self._effects.compact_execplan_catalog(self._connection, repo)

    def common_defect_reports(self, repo: str) -> list[dict[str, object]]:
        from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository
        return ValidationFeedbackRepository(self._connection).common_defect_reports(repo)

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
                                 prerequisite_plan_ids: tuple[str, ...] = ()) -> maintenance_state.MaintenanceCycle:
        return self._maintenance_repository.record_assessment_plans(repo, revision, plan_ids, prerequisite_plan_ids)

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

    def prepare_file_assessment(
        self, repo: str, repository: Path, base_branch: str, source_sha: str,
        evidence: maintenance_file_publication.AssessmentEvidence,
    ) -> maintenance_file_publication.PreparedPromotion:
        return maintenance_file_publication.prepare_promotion(
            self._connection, repo, repository, base_branch, source_sha, evidence,
        )

    def complete_file_assessment(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
        evidence: maintenance_file_publication.AssessmentEvidence | maintenance_file_publication.PreparedPromotion,
    ) -> None:
        prepared = (evidence if isinstance(evidence, maintenance_file_publication.PreparedPromotion)
                    else self.prepare_file_assessment(repo, repository, base_branch, source_sha, evidence))
        if prepared.repo != repo or prepared.published_revision != source_sha:
            raise maintenance_file_publication.FileAssessmentPublicationError("준비된 평가의 게시 identity가 다릅니다.")
        with runtime_state._transaction(self._connection):
            maintenance_file_publication.apply_promotion(self._connection, prepared)
            if prepared.ledger is None:
                return
            self._maintenance_repository.advance_file_assessment_success_baseline(
                repo, expected_revision=cycle_revision, source_sha=source_sha,
                expected_merged_count=prepared.evidence.merged_count,
                bump_revision=False,
            )

    def prepare_file_assessment_artifact(
        self, repo: str, repository: Path, base: str, context, worktree: Path, result,
    ):
        from loop_manager_internal.maintenance import code_map_artifact
        try:
            evidence = maintenance_file_publication.collect(
                self._connection, repo, repository, base, context.cycle_revision, context.active_sha,
                plan_repository=worktree,
            )
            return code_map_artifact.prepare(self._connection, repo, context, worktree, evidence, result)
        except (maintenance_file_inventory.SourceInventoryError, ValueError, OSError) as error:
            raise code_map_artifact.CodeMapArtifactError(str(error)) from error

    def recover_maintenance_batch(
        self, repo: str, worktree: Path, base: str
    ) -> maintenance_state.MaintenanceCycle:
        return self._effects.recover_maintenance_planning_batch(
            self._connection, repo, worktree, base, retain_planning=True
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
            retain_planning=True,
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
            self._connection, repo, worktree, base, retain_planning=True
        )

    def assert_general_deletion_resources(
        self, repo: str, result: ValidatedPlanningResult | None = None,
    ) -> None:
        if result is None:
            intent = self.general_publication(repo)
            if intent is None:
                return
            if self._effects.planning_result_from_manifest is None:
                raise RuntimeError('철회 게시의 저장된 manifest를 확인할 수 없습니다.')
            result = self._effects.planning_result_from_manifest(intent.manifest)
        if not result.deletions:
            return
        if self._connection.in_transaction:
            raise RuntimeError('철회 게시의 외부 자원 확인은 SQLite transaction 밖에서 수행해야 합니다.')
        from loop_manager_internal.storage.work_claims import cancellation_available
        for deletion in result.deletions:
            plan_id = deletion.snapshot.plan_id
            row = self._connection.execute(
                'SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id),
            ).fetchone()
            if not cancellation_available(self._connection, repo, plan_id, row):
                raise RuntimeError(f'철회 게시의 현재 writer·소유권을 확인할 수 없습니다: {plan_id}')
            if row['branch'] and self._effects.pr_for_branch is not None:
                if self._effects.pr_for_branch(Path(repo), str(row['branch'])) is not None:
                    from .planner_validation import MutableReadyPRConflict
                    raise MutableReadyPRConflict(f'철회 게시 전에 PR이 등록됐습니다: {plan_id}')
            if (row['branch'] or row['worktree']) and (
                self._effects.deletion_resources_available is None
                or not self._effects.deletion_resources_available(
                    row, repo, Path(repo), state_dir=self._state_dir)
            ):
                raise RuntimeError(f'철회 게시의 PR·worktree·Git operation·journal 안전을 확인할 수 없습니다: {plan_id}')

    def record_general_intent(
        self,
        repo: str,
        worktree: Path,
        validated_result: ValidatedPlanningResult,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> GeneralPlanningPublication:
        self.assert_general_deletion_resources(repo, validated_result)
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
            retain_planning=True,
        )
