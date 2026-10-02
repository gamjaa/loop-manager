"""Planning workspace creation and ownership checks."""
from __future__ import annotations
from pathlib import Path
import re
import stat
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.planning.planning_rebase import PlanningRebaseError, rebase_in_progress
from loop_manager_internal.maintenance.maintenance_orchestration import maintenance_planning_fingerprint
from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOrchestrationError
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, PlanningOutcome, PlanningOutcomeKind,
    PlanningWorkspaceOwnershipError,
    WORKSPACE_OWNED, WORKSPACE_RESERVED,
)

class PlanningWorkspaceService(PlanningService):
    def _resolve_rebase_conflict(
        self, run: _PlanningRun, worktree: Path, target: str, detail: str,
        collision_paths: tuple[str, ...] = (), stash_sha: str = "",
    ) -> bool:
        unmerged = tuple(sorted(set(self._ports.git.run(
            worktree, "ls-files", "-u", "--name-only",
        ).stdout.splitlines())))
        paths = tuple(sorted(set(unmerged) | set(collision_paths)))
        if not paths:
            return False
        if any(
            path != "docs/FUTURE_IDEAS.md"
            and re.fullmatch(r"docs/plans/[^/]+\.md", path) is None
            for path in paths
        ):
            self._ports.events.error(
                "planning 역할 밖의 rebase 충돌 파일을 보존합니다: "
                + ", ".join(paths)
            )
            return False
        was_rebase = rebase_in_progress(self._ports.git.run, worktree)
        role = "maintenance_planner" if run.maintenance_planning else "planner"
        prompt = (
            "관리 planning rebase 충돌을 같은 권한 안에서 해결하라. "
            "아래 파일의 충돌 내용을 편집하되 Git stage·continue·abort는 실행하지 마라. "
            "기존 계획 목표와 현재 원격 선언을 모두 확인하라.\n"
            + "\n".join(paths) + "\n" + detail
        )
        if collision_paths:
            prompt += (
                "\n보존 stash의 untracked 파일이 원격의 같은 경로와 충돌했다. "
                "원격 파일을 덮어쓰지 말고 각 새 ExecPlan을 별도 ID·파일로 보존해 교정하라. "
                "FUTURE_IDEAS는 두 내용을 현재 파일에 병합하라. 보존된 원본은 "
                f"`git show {stash_sha}^3:<경로>`로 확인할 수 있다."
            )
        before = self._ports.agent.begin_scope(role, worktree)
        result = self._ports.agent.run_planner(
            worktree, prompt, run.row.planner_session_id,
            lambda session_id: self._ports.state.update(
                run.context.repo, planner_session_id=session_id,
            ),
        )
        if result.session_id:
            self._ports.state.update(
                run.context.repo, planner_session_id=result.session_id,
            )
        scope = self._ports.agent.finish_scope(role, worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return False
        self._ports.agent.clear_scope(run.context.repo)
        if result.code:
            self._ports.events.error(result.message)
            return False
        for path in paths:
            file = worktree / path
            try:
                if file.is_symlink() or file.exists() and (
                    not stat.S_ISREG(file.lstat().st_mode)
                    or any(marker in file.read_bytes() for marker in (
                        b"<<<<<<< ", b"\n=======\n", b">>>>>>> ",
                    ))
                ):
                    self._ports.events.error(f"planning 충돌 표식 또는 잘못된 파일 형식: {path}")
                    return False
            except OSError as error:
                self._ports.events.error(f"planning 충돌 파일을 확인할 수 없습니다: {path}: {error}")
                return False
        if collision_paths:
            saved_paths = set(self._ports.git.run(
                worktree, "ls-tree", "-r", "--name-only", f"{stash_sha}^3",
            ).stdout.splitlines())
            candidate_paths = set(self._ports.git.run(
                worktree, "ls-files", "--others", "--exclude-standard",
            ).stdout.splitlines()) | set(self._ports.git.run(
                worktree, "diff", "--cached", "--name-only",
            ).stdout.splitlines())
            corrected_plans = {
                path for path in candidate_paths - saved_paths
                if re.fullmatch(r"docs/plans/[^/]+\.md", path)
                and path not in collision_paths
            }
            plan_collisions = [
                path for path in collision_paths if path.startswith("docs/plans/")
            ]
            if any(self._ports.git.run(
                worktree, "diff", "--quiet", "HEAD", "--", path,
                check=False,
            ).returncode != 0 for path in plan_collisions):
                self._ports.events.error("새 ID 충돌 교정은 원격 ExecPlan을 보존해야 합니다.")
                return False
            if len(corrected_plans) < len(plan_collisions):
                self._ports.events.error(
                    "원격 ID와 충돌한 새 ExecPlan을 별도 파일로 보존해야 합니다."
                )
                return False
            if "docs/FUTURE_IDEAS.md" in collision_paths and self._ports.git.run(
                worktree, "diff", "--quiet", "HEAD", "--", "docs/FUTURE_IDEAS.md",
                check=False,
            ).returncode != 1:
                self._ports.events.error("FUTURE_IDEAS 충돌 내용을 병합해야 합니다.")
                return False
        if unmerged:
            self._ports.git.run(worktree, "add", "--", *unmerged)
        in_rebase = rebase_in_progress(self._ports.git.run, worktree)
        if in_rebase:
            continued = self._ports.git.run(
                worktree, "-c", "core.editor=true", "rebase", "--continue",
                check=False,
            )
            if continued.returncode:
                self._ports.events.error(continued.stdout[-2000:])
                return False
        elif not was_rebase:
            self._ports.git.accept_resolved_stash_conflict(
                run.context.repo, worktree, target,
            )
        return True

    def _prepare_workspace(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = Path(str(row.worktree or ""))
        try:
            self._ports.git.validate_workspace_identity(
                context.repo,
                str(row.planning_attempt_fingerprint or ""),
                worktree,
            )
            planning_started = bool(
                row.planner_session_id
                or row.agent_snapshot
                or row.planning_result_pending_validation
                or row.planning_result_completed
                or row.planning_validation_error
                or row.planning_retry_count
                or row.last_error
            )
            owned = row.planning_workspace_owned == WORKSPACE_OWNED
            reserved = row.planning_workspace_owned == WORKSPACE_RESERVED
            self._ports.git.make_planning_worktree(
                context.repo_path,
                worktree,
                context.base_branch,
                allow_create=not planning_started and (not owned or reserved),
                allow_existing=owned or reserved,
                ownership_reserved=reserved,
                expected_head=str(row.active_sha or ""),
                before_create=lambda: self._ports.state.update(
                    context.repo,
                    planning_workspace_owned=WORKSPACE_RESERVED,
                ),
            )
            self._ports.state.update(
                context.repo,
                planning_workspace_owned=WORKSPACE_OWNED,
            )
        except PlanningWorkspaceOwnershipError as error:
            message = f"planning workspace 소유권 검증 실패: {error}"
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=run.current_fingerprint,
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()

        # This is a safe boundary: no Planner or verifier is using the
        # worktree. Recheck origin even when the project step began earlier.
        try:
            self._ports.git.run(context.repo_path, "fetch", "origin")
            latest = self._ports.git.run(
                context.repo_path, "rev-parse", "--verify",
                f"refs/remotes/origin/{context.base_branch}^{{commit}}",
            ).stdout.strip()
            if latest == row.active_sha:
                # The SQLite base update may have committed immediately before
                # a crash that left the preservation stash and journal behind.
                self._ports.git.complete_planning_rebase(
                    context.repo, worktree, latest,
                )
            if latest != row.active_sha:
                changed_documents = self._ports.git.relevant_document_changes(
                    worktree, str(row.active_sha or ""), latest,
                    sorted(set(self._ports.git.changed_files(
                        worktree, str(row.active_sha or ""),
                    )) | set(self._ports.git.changed_files(worktree))),
                    self._ports.state.execplan_rows(context.repo),
                )
                if row.agent_snapshot:
                    role = "maintenance_planner" if run.maintenance_planning else "planner"
                    if row.agent_role != role:
                        self._ports.events.error("보존된 Agent 역할이 현재 Planner와 다릅니다.")
                        return self._operational_error()
                    scope = self._ports.agent.validate_saved_scope(
                        role, worktree, row.agent_snapshot,
                    )
                    if scope.error:
                        self._ports.events.error(scope.error)
                        return self._operational_error()
                    self._ports.agent.clear_scope(context.repo)
                result = self._ports.git.rebase_planning_workspace(
                    context.repo, str(row.planning_attempt_fingerprint or ""),
                    worktree, str(row.active_sha or ""), latest,
                )
                if result.conflict:
                    if self._resolve_rebase_conflict(
                        run, worktree, result.target, result.detail,
                        getattr(result, "conflict_paths", ()),
                        getattr(result, "stash_sha", ""),
                    ):
                        result = self._ports.git.rebase_planning_workspace(
                            context.repo,
                            str(row.planning_attempt_fingerprint or ""),
                            worktree, str(row.active_sha or ""), latest,
                        )
                if result.conflict:
                    self._ports.events.error(
                        f"planning rebase 충돌; 작업 보존: {worktree}; {result.detail}"
                    )
                    return PlanningOutcome(
                        PlanningOutcomeKind.TRANSIENT_PAUSE,
                        detail=result.detail, error=True,
                    )
                if run.maintenance_planning:
                    cycle = self._ports.state.maintenance_cycle(context.repo)
                    if cycle.publication_stage == "prepared":
                        cycle = self._ports.state.supersede_unpublished_maintenance_intent(
                            context.repo, cycle, result.target,
                        )
                    elif cycle.cycle_base_sha != result.target:
                        cycle = self._ports.state.restart_stale_maintenance(
                            context.repo, cycle, result.target,
                        )
                    run.maintenance_cycle = cycle
                else:
                    self._ports.state.supersede_unpublished_general_intent(context.repo)
                values: dict[str, object] = {"active_sha": result.target}
                if changed_documents and row.planning_result_completed:
                    self._ports.state.preserve_diagnostics(context.repo, row)
                    recheck_message = (
                        "현재 planning 결과의 관련 문서가 변경됐습니다: "
                        + ", ".join(changed_documents)
                    )
                    values.update(
                        planning_result_pending_validation=0,
                        planning_result_completed=0,
                        planning_validation_error=recheck_message,
                    )
                    if row.planning_error and row.planning_error == row.planning_validation_error:
                        values["planning_error"] = recheck_message
                self._ports.state.update(context.repo, **values)
                self._ports.git.complete_planning_rebase(
                    context.repo, worktree, result.target,
                )
                self._ports.events.info(
                    f"planning base 반영: {row.active_sha} -> {result.target}; "
                    f"후보={self._ports.git.run(worktree, 'rev-parse', 'HEAD').stdout.strip()}; "
                    f"재개={'validation' if row.planning_result_completed and not changed_documents else 'planner'}; "
                    f"남은 원인={row.planning_error or row.planning_validation_error or '없음'}"
                )
                row = self._ports.state.planning(context.repo)
                if result.target != latest:
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                        detail="원격 base가 다시 전진해 다음 안전 경계에서 반영합니다.",
                        error=True,
                    )
            run.base_sha = latest
            run.general_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, latest,
            )
            run.specification_fingerprint = self._ports.git.specification_fingerprint(
                context.repo_path, latest,
            )
            run.current_fingerprint = (
                maintenance_planning_fingerprint(
                    run.maintenance_cycle, run.general_fingerprint,
                ) if run.maintenance_planning else run.general_fingerprint
            )
        except (RuntimeError, PlanningRebaseError) as error:
            self._ports.events.error(f"planning 원격 반영 대기: {error}")
            return PlanningOutcome(
                PlanningOutcomeKind.TRANSIENT_PAUSE, detail=str(error), error=True,
            )
        run.row = row

        if run.maintenance_planning:
            try:
                cycle = self._ports.state.maintenance_cycle(context.repo)
                if cycle.publication_stage == "prepared":
                    maintenance_context = (
                        self._ports.validation.maintenance_context_at_revision(
                            context.repo,
                            worktree,
                            str(row.active_sha or ""),
                            cycle,
                        )
                    )
                    change_context = ""
                else:
                    maintenance_context = (
                        self._ports.validation.prepare_maintenance_context(
                            context.repo,
                            worktree,
                            str(row.active_sha or ""),
                        )
                    )
                    change_context = (
                        self._ports.validation.maintenance_change_context(
                            context.repo_path,
                            context.base_branch,
                            cycle,
                        )
                    )
            except (MaintenancePlanningError, MaintenanceOrchestrationError) as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            run.maintenance_cycle = cycle
            run.maintenance_context = maintenance_context
            run.maintenance_change_context = change_context
        else:
            try:
                run.mutable_ready_snapshot = (
                    self._ports.validation.prepare_general_context(
                        context.repo,
                        worktree,
                        str(row.active_sha or ""),
                    )
                )
            except PlanningResultInvalid as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
        run.planning_worktree = worktree
        return None
