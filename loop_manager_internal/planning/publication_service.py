"""Planning publication intent, remote confirmation, and recovery."""
from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events
from pathlib import Path
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError, MaintenancePlanningRestart
from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOutcome
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.planning.planning_contracts import _PlanningRun, PlanningOutcome, PlanningOutcomeKind
from loop_manager_internal.planning.planner_validation import MutableReadyPRConflict

class PlanningPublicationService(PlanningService):
    def _defer_mutable_pr_conflict(self, error: MutableReadyPRConflict) -> PlanningOutcome:
        self._ports.events.info(
            "PR 등록 경합으로 계획 후보와 실행 이력을 보존하고 현재 입력을 다시 확인합니다."
        )
        return PlanningOutcome(
            PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
            detail=str(error), error=True,
        )

    def _verify_recovery_workspace(self, run: _PlanningRun, worktree: Path) -> None:
        """Both publication modes require the saved workspace and fresh remote ref."""
        self._ports.git.validate_worktree_ownership(
            run.context.repo, str(run.row.planning_attempt_fingerprint or ""), worktree,
        )
        self._ports.git.fetch_base(run.context.repo_path, run.context.base_branch)

    def _preserve_recovery_error(
        self, run: _PlanningRun, fingerprint: str, error: RuntimeError,
    ) -> PlanningOutcome:
        message = str(error)
        self._ports.state.update(
            run.context.repo,
            planning_error=run.row.planning_error or message,
            planning_error_fingerprint=(
                run.row.planning_error_fingerprint or fingerprint
            ),
            last_error=message,
        )
        self._ports.events.error(message)
        return self._operational_error()

    def _recover_publication(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        context = run.context
        cycle = run.maintenance_cycle
        row = run.row
        general_intent = self._ports.state.general_publication(context.repo)
        if general_intent is not None and row.phase == "PLANNING":
            worktree = Path(str(row.worktree or ""))
            fingerprint = str(row.planning_attempt_fingerprint or "")
            try:
                self._verify_recovery_workspace(run, worktree)
                remote_sha = self._ports.git.run(
                    context.repo_path, "rev-parse", "--verify",
                    f"refs/remotes/origin/{context.base_branch}^{{commit}}",
                ).stdout.strip()
                published = self._ports.git.run(
                    worktree, "merge-base", "--is-ancestor",
                    general_intent.commit_sha, remote_sha, check=False,
                ).returncode == 0
                if remote_sha != general_intent.base_sha and not published:
                    # The intent has not reached origin. Keep its candidate
                    # and session for the owned-worktree rebase boundary.
                    return None
                with self._ports.state.transaction():
                    recovery = self._ports.state.recover_general_batch(
                        context.repo, worktree, context.base_branch
                    )
                    if recovery != "pending" and worktree.exists():
                        self._ports.git.run(
                            context.repo_path, "worktree", "remove", str(worktree)
                        )
            except RuntimeError as error:
                return self._preserve_recovery_error(run, fingerprint, error)
            if recovery == "pending":
                run.publication_recovery_verified = True
                return None
            self._ports.events.info(
                "게시된 일반 planning batch를 복구했습니다."
                if recovery == "applied"
                else "원격 변경에 맞춰 일반 planning batch를 다시 시작합니다."
            )
            return PlanningOutcome(PlanningOutcomeKind.PUBLICATION_RECOVERY)
        if not (
            run.maintenance_planning
            and cycle.publication_stage == "prepared"
            and row.phase == "PLANNING"
        ):
            return None
        worktree = Path(str(row.worktree or ""))
        fingerprint = str(row.planning_attempt_fingerprint or "")
        try:
            self._verify_recovery_workspace(run, worktree)
            remote_sha = self._ports.git.run(
                context.repo_path,
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{context.base_branch}^{{commit}}",
            ).stdout.strip()
            run.base_sha = remote_sha
            recovered_cycle = None
            if remote_sha != cycle.publication_base_sha:
                published = self._ports.git.run(
                    worktree, "merge-base", "--is-ancestor",
                    str(cycle.publication_commit_sha), remote_sha,
                    check=False,
                ).returncode == 0
                if not published:
                    return None
                with self._ports.state.transaction():
                    recovered_cycle = self._ports.state.recover_maintenance_batch(
                        context.repo, worktree, context.base_branch
                    )
                    if worktree.exists():
                        self._ports.git.run(
                            context.repo_path, "worktree", "remove", str(worktree)
                        )
        except RuntimeError as error:
            return self._preserve_recovery_error(run, fingerprint, error)
        if recovered_cycle is None:
            run.publication_recovery_verified = True
            return None
        self._ports.events.info(
            "게시된 유지보수 batch를 fingerprint 정리 전에 복구했습니다."
            if recovered_cycle.publication_stage == "remote_confirmed"
            else "원격 변경에 맞춰 미게시 유지보수 batch를 다시 시작합니다."
        )
        return PlanningOutcome(PlanningOutcomeKind.PUBLICATION_RECOVERY)

    def _publish_planner_result(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = run.planning_worktree
        assert worktree is not None
        validated_result = run.validated_result
        pending_intent = (
            self._ports.state.general_publication(context.repo) is not None
            or run.maintenance_planning
            and run.maintenance_cycle.publication_stage == "prepared"
        )
        if row.planning_error and not pending_intent:
            self._ports.events.error(
                f"해결되지 않은 planning 오류를 보존합니다: {row.planning_error}"
            )
            return self._operational_error()
        if self._file_assessment_active(run) and run.maintenance_cycle.publication_stage is None:
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo, str(row.planning_attempt_fingerprint or run.current_fingerprint or ""), worktree,
                )
                self._ports.git.fetch_base(context.repo_path, context.base_branch)
                self._ports.validation.validate_publication_base(
                    context.repo, context.repo_path, context.base_branch, run.maintenance_context,
                )
                validated_result = self._ports.state.prepare_file_assessment_artifact(
                    context.repo, context.repo_path, context.base_branch,
                    run.maintenance_context, worktree, validated_result,
                )
                run.validated_result = validated_result
                from loop_manager_internal.maintenance.code_map_artifact import ARTIFACT_PATH
                # Preparation may restore a stale candidate to identical remote
                # bytes. Refresh its index and dirty flag before deciding whether
                # this batch needs a commit at all.
                self._ports.git.run(worktree, "add", "--", ARTIFACT_PATH)
                artifact_dirty = self._ports.git.run(
                    worktree, "status", "--porcelain", "--", ARTIFACT_PATH,
                ).stdout.strip()
                run.dirty_files = sorted((set(run.dirty_files or ()) - {ARTIFACT_PATH})
                                        | ({ARTIFACT_PATH} if artifact_dirty else set()))
            except MaintenancePlanningRestart:
                return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED, error=True)
            except RuntimeError as error:
                return self._preserve_recovery_error(run, str(run.current_fingerprint or ""), error)
        if validated_result is None:
            self._ports.git.fetch_base(context.repo_path, context.base_branch)
            latest_sha = self._ports.git.run(
                context.repo_path, "rev-parse", "--verify",
                f"refs/remotes/origin/{context.base_branch}^{{commit}}",
            ).stdout.strip()
            if latest_sha != row.active_sha:
                self._ports.events.info(
                    "planning 완료 기록 전 base가 전진해 작업을 보존합니다."
                )
                return PlanningOutcome(
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                    error=True,
                )
            completed_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, str(row.active_sha or "")
            )
            completed_specification_fingerprint = (
                self._ports.git.specification_fingerprint(
                    context.repo_path, str(row.active_sha or "")
                )
            )
            self._ports.git.validate_worktree_ownership(
                context.repo,
                str(row.planning_attempt_fingerprint or run.current_fingerprint or ""),
                worktree,
            )
            if run.maintenance_planning:
                assert run.maintenance_context is not None
                try:
                    self._ports.git.fetch_base(context.repo_path, context.base_branch)
                    self._ports.validation.validate_publication_base(
                        context.repo,
                        context.repo_path,
                        context.base_branch,
                        run.maintenance_context,
                    )
                except MaintenancePlanningRestart:
                    self._ports.events.error(
                        "유지보수 평가 중 base가 변경되어 관리 worktree를 보존합니다."
                    )
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                        error=True,
                    )
                except RuntimeError as error:
                    message = str(error)
                    self._ports.state.update(
                        context.repo,
                        planning_error=message,
                        planning_error_fingerprint=run.current_fingerprint,
                        last_error=message,
                    )
                    self._ports.events.error(message)
                    return self._operational_error()
                evidence = None
                if self._file_assessment_active(run):
                    evidence = self._ports.state.file_assessment_evidence(
                        context.repo, context.repo_path, context.base_branch,
                        run.maintenance_cycle.revision, str(row.active_sha),
                    )
            try:
                with self._ports.state.transaction():
                    if run.maintenance_planning:
                        if evidence is not None:
                            self._ports.state.complete_file_assessment(
                                context.repo, context.repo_path, context.base_branch,
                                run.maintenance_cycle.revision, str(row.active_sha),
                                evidence,
                            )
                        if run.maintenance_decision is not None and run.maintenance_decision.outcome is MaintenanceOutcome.PREREQUISITES:
                            self._ports.state.transition_maintenance(
                                context.repo, expected_phase=run.maintenance_cycle.phase,
                                expected_revision=run.maintenance_cycle.revision,
                                next_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                                prerequisite_plan_ids=run.maintenance_decision.plan_ids,
                                include_general_planning=False,
                            )
                        else:
                            self._ports.state.advance_success_baseline(
                                context.repo,
                                expected_phase=run.maintenance_cycle.phase,
                                expected_revision=run.maintenance_cycle.revision,
                                remote_commit=str(row.active_sha or ""),
                            )
                        if run.maintenance_cycle.include_general_planning:
                            self._ports.state.record_general_plan_declarations(
                                context.repo, str(row.active_sha or ""),
                                self._ports.git.plan_declarations(
                                    context.repo_path, str(row.active_sha or ""),
                                ),
                            )
                        self._ports.state.clear_active_in_transaction(
                            context.repo,
                            processed_sha=(
                                str(row.active_sha or "")
                                if run.maintenance_cycle.include_general_planning
                                else None
                            ),
                            specification_fingerprint=(
                                completed_specification_fingerprint
                                if run.maintenance_cycle.include_general_planning
                                else None
                            ),
                            planning_fingerprint=(
                                completed_fingerprint
                                if run.maintenance_cycle.include_general_planning
                                else None
                            ),
                        )
                    else:
                        review = getattr(self._ports.state, 'planning_review', lambda *_: None)(context.repo, 'attempt')
                        requirement_only = review is not None and review['purpose'] == 'requirement'
                        if hasattr(self._ports.state, "confirm_planning_review"):
                            self._ports.state.confirm_planning_review(
                                context.repo, str(row.active_sha), str(row.active_sha),
                            )
                        if not requirement_only:
                            self._ports.state.record_general_plan_declarations(
                                context.repo, str(row.active_sha or ""),
                                self._ports.git.plan_declarations(context.repo_path, str(row.active_sha or "")),
                            )
                        self._ports.state.clear_active_in_transaction(
                            context.repo,
                            processed_sha=row.processed_sha if requirement_only else row.active_sha,
                            specification_fingerprint=(
                                row.specification_fingerprint if requirement_only else completed_specification_fingerprint
                            ),
                            planning_fingerprint=row.planning_fingerprint if requirement_only else completed_fingerprint,
                        )
                    self._ports.git.run(
                        context.repo_path,
                        "worktree",
                        "remove",
                        str(worktree),
                    )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=row.planning_error or message,
                    planning_error_fingerprint=(
                        row.planning_error_fingerprint or run.current_fingerprint
                    ),
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            self._ports.events.info(
                "유지보수 평가와 재검증을 완료했습니다."
                if run.maintenance_planning
                else "추가로 계획할 작업이 없습니다."
            )
            run.row = self._ports.state.planning(context.repo)
            return None

        plans = list(validated_result.plans)
        # Validation can add Manager-owned changes after the Planner's dirty snapshot.
        if self._ports.git.changed_files(worktree):
            present, deleted = [], []
            for path in validated_result.publication_paths:
                target = worktree / path
                (present if target.exists() or target.is_symlink() else deleted).append(path)
            # Planner may already have staged a deletion or rename.
            if deleted:
                self._ports.git.run(worktree, "update-index", "--force-remove", "--", *deleted)
            if present:
                self._ports.git.run(worktree, "add", "--", *(f":(literal){path}" for path in present))
            if run.maintenance_planning:
                message = "Update periodic maintenance planning batch"
            elif validated_result.retirement_moves and not (
                plans or validated_result.revisions or validated_result.deletions
            ):
                message = "Retire fulfilled Requirements after merged implementation assessment"
            elif not plans and not (
                validated_result.revisions or validated_result.deletions
            ):
                message = "Update future ideas from planning"
            elif validated_result.revisions or validated_result.deletions:
                message = "Update mutable READY planning batch"
            else:
                first = plans[0][1]
                last = plans[-1][1]
                message = (
                    f"Add ExecPlan {first['id']}: {first['title']}"
                    if len(plans) == 1
                    else f"Add ExecPlans {first['id']}-{last['id']}"
                )
            self._ports.git.run(worktree, "commit", "-m", message)

        plan_commit = self._ports.git.run(
            worktree, "rev-parse", "HEAD"
        ).stdout.strip()
        if run.maintenance_planning:
            assert run.maintenance_context is not None
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo,
                    str(row.planning_attempt_fingerprint or run.current_fingerprint or ""),
                    worktree,
                )
                if run.maintenance_cycle.publication_stage is None:
                    self._ports.validation.validate_publication_base(
                        context.repo,
                        context.repo_path,
                        context.base_branch,
                        run.maintenance_context,
                    )
                    run.maintenance_cycle = (
                        self._ports.state.record_maintenance_intent(
                            context.repo,
                            worktree,
                            context.base_branch,
                            validated_result,
                            plan_commit,
                        )
                    )
                elif run.maintenance_cycle.publication_stage != "prepared":
                    raise MaintenancePlanningError(
                        "복구할 수 없는 maintenance publication 단계입니다."
                    )
            except MaintenancePlanningRestart:
                self._ports.events.error(
                    "유지보수 계획 중 base가 변경되어 관리 worktree를 보존합니다."
                )
                return PlanningOutcome(
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED, error=True
                )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()

        if not run.maintenance_planning:
            assert run.specification_fingerprint is not None
            assert run.general_fingerprint is not None
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo,
                    str(row.planning_attempt_fingerprint or run.current_fingerprint or ""),
                    worktree,
                )
                self._ports.state.record_general_intent(
                    context.repo,
                    worktree,
                    validated_result,
                    plan_commit,
                    run.specification_fingerprint,
                    run.general_fingerprint,
                )
            except MutableReadyPRConflict as error:
                if self._ports.state.general_publication(context.repo) is not None:
                    if not run.publication_recovery_verified:
                        return self._preserve_recovery_error(
                            run, str(run.current_fingerprint or ""), error,
                        )
                    try:
                        self._ports.state.supersede_unpublished_general_intent(context.repo)
                    except RuntimeError as supersede_error:
                        return self._preserve_recovery_error(
                            run, str(run.current_fingerprint or ""), supersede_error,
                        )
                return self._defer_mutable_pr_conflict(error)
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()

        self._ports.git.fetch_base(context.repo_path, context.base_branch)
        latest_sha = self._ports.git.run(
            context.repo_path,
            "rev-parse",
            f"origin/{context.base_branch}",
        ).stdout.strip()
        if (
            not run.maintenance_planning
            and latest_sha not in {row.active_sha, plan_commit}
        ):
            self._ports.events.info(
                "일반 planning 게시 전 base가 전진해 후보와 세션을 보존합니다."
            )
            return PlanningOutcome(
                PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                error=True,
            )
        if run.maintenance_planning and latest_sha not in {
            row.active_sha,
            plan_commit,
        }:
            self._ports.events.info(
                "유지보수 planning 게시 전 base가 전진해 후보와 세션을 보존합니다."
            )
            return PlanningOutcome(
                PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                error=True,
            )

        if not run.maintenance_planning:
            assert run.specification_fingerprint is not None
            assert run.general_fingerprint is not None
            try:
                # The first call durably records the intent. Repeating the
                # idempotent operation after fetch closes the activation race
                # between validation and the fast-forward push.
                self._ports.state.record_general_intent(
                    context.repo,
                    worktree,
                    validated_result,
                    plan_commit,
                    run.specification_fingerprint,
                    run.general_fingerprint,
                )
            except MutableReadyPRConflict as error:
                if latest_sha == row.active_sha:
                    try:
                        self._ports.state.supersede_unpublished_general_intent(context.repo)
                    except RuntimeError as supersede_error:
                        return self._preserve_recovery_error(
                            run, str(run.current_fingerprint or ""), supersede_error,
                        )
                else:
                    return self._preserve_recovery_error(
                        run, str(run.current_fingerprint or ""), error,
                    )
                return self._defer_mutable_pr_conflict(error)
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()

        if latest_sha not in {row.active_sha, plan_commit}:
            published = (
                self._ports.git.run(
                    worktree,
                    "merge-base",
                    "--is-ancestor",
                    plan_commit,
                    f"origin/{context.base_branch}",
                    check=False,
                ).returncode
                == 0
            )
            if not published:
                self._ports.events.error(
                    "계획 중 base가 변경되어 후보와 세션을 보존합니다."
                )
                return PlanningOutcome(
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                    error=True,
                )

        if latest_sha != plan_commit and latest_sha == row.active_sha:
            pushed = self._ports.git.run(
                worktree,
                "push",
                "origin",
                f"HEAD:refs/heads/{context.base_branch}",
                check=False,
            )
            if pushed.returncode:
                message = f"ExecPlan push 실패:\n{pushed.stdout[-8000:]}"
                self._ports.state.update(context.repo, last_error=message)
                self._ports.events.error(pushed.stdout)
                return self._operational_error()
            self._ports.git.fetch_base(context.repo_path, context.base_branch)
            latest_sha = self._ports.git.run(
                context.repo_path,
                "rev-parse",
                f"origin/{context.base_branch}",
            ).stdout.strip()
            if latest_sha != plan_commit:
                message = (
                    "ExecPlan push 결과를 origin/base에서 확인할 수 없습니다."
                )
                self._ports.state.update(context.repo, last_error=message)
                self._ports.events.error(
                    "ExecPlan push 결과 확인에 실패했습니다."
                )
                return self._operational_error()

        try:
            with self._ports.state.transaction():
                if run.maintenance_planning:
                    self._ports.state.publish_maintenance_batch(
                        context.repo,
                        worktree,
                        context.base_branch,
                        validated_result,
                        plan_commit,
                    )
                else:
                    self._ports.git.validate_worktree_ownership(
                        context.repo,
                        str(row.planning_attempt_fingerprint or run.current_fingerprint or ""),
                        worktree,
                    )
                    assert run.specification_fingerprint is not None
                    assert run.general_fingerprint is not None
                    self._ports.state.publish_general_batch(
                        context.repo,
                        worktree,
                        context.base_branch,
                        validated_result,
                        plan_commit,
                        run.specification_fingerprint,
                        run.general_fingerprint,
                    )
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    str(worktree),
                )
        except RuntimeError as error:
            message = str(error)
            self._ports.state.update(
                context.repo,
                planning_error=row.planning_error or message,
                planning_error_fingerprint=(
                    row.planning_error_fingerprint or run.current_fingerprint
                ),
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        events.emit("계획 원격 게시·catalog 반영 완료", revision=plan_commit,
                    next_action="최신 catalog에서 실행 대상 선택")
        run.base_sha = latest_sha
        run.row = self._ports.state.planning(context.repo)
        return None
