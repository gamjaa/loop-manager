"""Run and apply an input-bound, read-only check before general planning."""
from __future__ import annotations

import json
from pathlib import Path

from loop_manager_internal.agents.agent_prompts import planner_check_prompt
from loop_manager_internal.execution.validation_feedback import bounded_diagnostic, redact_secrets
from loop_manager_internal.planning.agent_ports import planner_progress, run_scoped_planner_turn
from loop_manager_internal.planning.planner_check import parse_planner_check_result
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.planning.planning_contracts import (
    MAX_PLANNER_VALIDATION_RETRIES, PlanningAgentResultKind, PlanningOutcome,
    PlanningOutcomeKind, _PlanningRun,
)
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.storage.planner_check_state import target_fingerprint


class PlanningCheckService(PlanningService):
    def _fail(self, run: _PlanningRun, message: str) -> PlanningOutcome:
        message = bounded_diagnostic(message)
        self._ports.state.update(
            run.context.repo, planning_error=message,
            planning_error_fingerprint=run.current_fingerprint,
            last_error=message,
        )
        self._ports.events.error(message)
        return self._operational_error()

    def _prepare_general_handoff(self, run: _PlanningRun) -> None:
        repo = run.context.repo
        row = self._ports.state.planning(repo)
        if (row.agent_role == "planner" or row.planner_session_id
                or row.planning_result_pending_validation
                or row.planning_result_completed):
            return
        if row.agent_role == "planner_check":
            self._ports.agent.clear_scope(repo)
        self._ports.state.update(
            repo, planner_session_id=None, planning_result_pending_validation=0,
            planning_result_completed=0, planning_result=None,
            planning_validation_error=None, planning_retry_count=0,
            agent_result_snapshot=None, last_error=None,
        )
        run.row = self._ports.state.planning(repo)

    def _handoff(
        self, run: _PlanningRun, reason: str,
    ) -> PlanningOutcome | None:
        repo = run.context.repo
        current = self._ports.state.planner_check(repo)
        if current is not None and current["stage"] != "handoff":
            changed = self._ports.state.advance_planner_check(
                repo, str(current["stage"]), "handoff",
                expected_input=str(current["input_fingerprint"]),
                expected_target=str(current["target_fingerprint"]), error=reason,
            )
            if not changed:
                return self._fail(run, "점검 인계 상태가 변경됐습니다.")
        self._prepare_general_handoff(run)
        self._ports.events.log(f"점검 Planner → 일반 Planner 인계: {reason}")
        return None

    def _finish_accepted(self, run: _PlanningRun) -> PlanningOutcome | None:
        repo = run.context.repo
        row = self._ports.state.planning(repo)
        worktree = Path(str(row.worktree or ""))
        try:
            if worktree.exists():
                self._ports.git.validate_worktree_ownership(
                    repo, str(row.planning_attempt_fingerprint or ""), worktree,
                )
                self._ports.git.run(run.context.repo_path, "worktree", "remove", str(worktree))
            else:
                self._ports.git.validate_workspace_identity(
                    repo, str(row.planning_attempt_fingerprint or ""), worktree,
                )
            with self._ports.state.transaction():
                self._ports.state.clear_active_in_transaction(
                    repo, processed_sha=row.processed_sha,
                    specification_fingerprint=row.specification_fingerprint,
                    planning_fingerprint=row.planning_fingerprint,
                )
        except RuntimeError as error:
            return self._fail(run, f"점검 workspace 정리 실패: {error}")
        run.row = self._ports.state.planning(repo)
        run.fingerprint_skipped = True
        self._ports.events.log("점검 Planner: 수정 불필요; 예정 READY를 유지합니다.")
        return None

    def _resume_accepted(self, run: _PlanningRun) -> PlanningOutcome | None:
        if run.row.phase == "PLANNING" and not run.maintenance_planning:
            check = getattr(self._ports.state, "planner_check", lambda _repo: None)(run.context.repo)
            if check is not None and check["stage"] == "accepted":
                return self._finish_accepted(run)
        return None

    def _resume_handoff(self, run: _PlanningRun) -> PlanningOutcome | None:
        if run.row.phase == "PLANNING" and not run.maintenance_planning:
            check = getattr(self._ports.state, "planner_check", lambda _repo: None)(run.context.repo)
            if check is not None and check["stage"] == "handoff":
                self._prepare_general_handoff(run)
        return None

    def _run_check(self, run: _PlanningRun) -> PlanningOutcome | None:
        repo = run.context.repo
        row = run.row
        if row.phase != "PLANNING" or run.maintenance_planning:
            return None
        check = getattr(self._ports.state, "planner_check", lambda _repo: None)(repo)
        if check is None or check["stage"] in {"handoff", "superseded"}:
            return None
        if check["stage"] == "accepted":
            return self._finish_accepted(run)
        worktree = run.planning_worktree
        assert worktree is not None

        if self._ports.git.fetch_base(
            run.context.repo_path, run.context.base_branch,
        ) != run.base_sha:
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)

        # The workspace phase has reconciled the remote catalog. A result is
        # never accepted against an input or mutable target that has moved.
        snapshot = run.mutable_ready_snapshot
        assert snapshot is not None
        identity = target_fingerprint(snapshot)
        guidance = self._ports.git.run(
            run.context.repo_path, "diff", "--name-only", str(check["comparison_sha"]),
            run.base_sha, "--", "AGENTS.md",
        ).stdout.strip()
        specification = self._ports.git.specification_fingerprint(
            run.context.repo_path, run.base_sha,
        )
        current_input = self._ports.git.input_fingerprint(
            run.context.repo_path, run.base_sha,
        )
        if (guidance or specification != check["specification_fingerprint"]
                or not self._ports.state.plan_declarations_match(
                    repo, self._ports.git.plan_declarations(run.context.repo_path, run.base_sha),
                ) or not snapshot.plans):
            return self._handoff(
                run, "점검 중 문서·지침 또는 예정 작업이 변경됐습니다. 최신 입력을 일반 Planner가 확인합니다.",
            )
        if (current_input != check["input_fingerprint"]
                or identity != check["target_fingerprint"]):
            self._ports.agent.clear_scope(repo)
            self._ports.state.begin_planner_check(
                repo, input_fingerprint=current_input,
                specification_fingerprint=specification,
                target_fingerprint=identity, base_sha=run.base_sha,
                comparison_sha=str(check["base_sha"]),
            )
            self._ports.state.update(
                repo, planner_session_id=None, planning_result_pending_validation=0,
                planning_result_completed=0, planning_result=None,
                planning_validation_error=None, planning_retry_count=0,
                agent_result_snapshot=None, last_error=None,
            )
            check = self._ports.state.planner_check(repo)
            row = self._ports.state.planning(repo)
            run.row = row
        if check["stage"] == "pending":
            outcome = self._run_pending(run, check, snapshot, worktree, row)
            if outcome is not None:
                return outcome
            check = self._ports.state.planner_check(repo)
        if check is None or check["stage"] != "result":
            return self._fail(run, "점검 결과 저장 상태를 확인할 수 없습니다.")
        return self._apply_result(run, check, snapshot, worktree)

    def _run_pending(self, run, check, snapshot, worktree, row) -> PlanningOutcome | None:
        repo = run.context.repo
        if row.agent_snapshot:
            if row.agent_role != "planner_check":
                return self._fail(run, "저장된 점검 Agent 역할이 다릅니다.")
            scope = self._ports.agent.validate_saved_scope(
                "planner_check", worktree, row.agent_snapshot,
            )
            if scope.error:
                return self._fail(run, scope.error)
            self._ports.agent.clear_scope(repo)
        changes = self._ports.git.run(
            run.context.repo_path, "diff", "--name-status",
            str(check["comparison_sha"]), run.base_sha,
        ).stdout.strip()
        changes = "\n".join(
            line for line in changes.splitlines()
            if any(path != "AGENTS.md" and not path.startswith("docs/")
                   for path in line.split("\t")[1:])
        )
        change_context = (
            f"비교 revision: {check['comparison_sha']} → {run.base_sha}\n"
            f"변경 경로:\n{changes}"
        )
        prompt = planner_check_prompt(
            change_context, self._ports.state.compact_catalog(repo), snapshot,
            str(check["error"]) if check["error"] and check["retry_count"] else None,
        )
        def save_session(session_id: str) -> None:
            self._ports.state.advance_planner_check(
                repo, "pending", "pending",
                expected_input=str(check["input_fingerprint"]),
                expected_target=str(check["target_fingerprint"]),
                session_id=session_id,
            )
        self._ports.events.log("점검 Planner 시작: 예정 READY 영향 확인")
        with planner_progress(self._ports.events, f"planner_check ({repo})"):
            result, scope = run_scoped_planner_turn(
                self._ports.agent, repo, "planner_check", worktree, prompt,
                str(check["session_id"]) if check["session_id"] else None,
                save_session, read_only=True,
            )
        if scope.error:
            if scope.result_snapshot:
                self._ports.state.update(repo, agent_result_snapshot=scope.result_snapshot)
            return self._fail(run, scope.error)
        if result.code:
            message = bounded_diagnostic(
                result.message or f"점검 Planner 종료 코드 {result.code}"
            )
            self._ports.state.advance_planner_check(
                repo, "pending", "pending",
                expected_input=str(check["input_fingerprint"]),
                expected_target=str(check["target_fingerprint"]), error=message,
            )
            self._ports.state.update(repo, last_error=message)
            self._ports.events.error(message)
            kind = (
                PlanningOutcomeKind.USAGE_LIMIT_PAUSE
                if result.kind is PlanningAgentResultKind.USAGE_LIMIT else
                PlanningOutcomeKind.TRANSIENT_PAUSE
                if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE else
                PlanningOutcomeKind.OPERATIONAL_ERROR
            )
            return PlanningOutcome(kind, error=True)
        try:
            if len(result.message) > 10000:
                raise PlanningResultInvalid("점검 Planner 결과가 허용 길이를 초과했습니다.")
            parsed = parse_planner_check_result(
                result.message, frozenset(snapshot.by_id),
            )
            if len(parsed.evidence) > 2000 or len(parsed.reason) > 2000:
                raise PlanningResultInvalid("점검 Planner 근거가 허용 길이를 초과했습니다.")
            if (redact_secrets(parsed.evidence) != parsed.evidence
                    or redact_secrets(parsed.reason) != parsed.reason):
                raise PlanningResultInvalid("점검 Planner 결과에 비밀값이 포함됐습니다.")
        except PlanningResultInvalid as error:
            count = int(check["retry_count"]) + 1
            self._ports.state.advance_planner_check(
                repo, "pending", "pending",
                expected_input=str(check["input_fingerprint"]),
                expected_target=str(check["target_fingerprint"]),
                error=str(error), retry_count=count,
            )
            if count >= MAX_PLANNER_VALIDATION_RETRIES:
                return self._fail(run, str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, error=True)
        normalized = "PLANNER_CHECK_RESULT: " + json.dumps({
            "outcome": parsed.outcome, "evidence": parsed.evidence,
            "reason": parsed.reason, "plan_ids": list(parsed.plan_ids),
        }, ensure_ascii=True, separators=(",", ":"))
        if not self._ports.state.advance_planner_check(
            repo, "pending", "result",
            expected_input=str(check["input_fingerprint"]),
            expected_target=str(check["target_fingerprint"]),
            result=normalized, error=None,
        ):
            return self._fail(run, "점검 결과 저장 상태가 변경됐습니다.")
        self._ports.agent.clear_scope(repo)
        return None

    def _apply_result(self, run, check, snapshot, worktree) -> PlanningOutcome | None:
        repo = run.context.repo
        saved_row = self._ports.state.planning(repo)
        if saved_row.agent_snapshot:
            if saved_row.agent_role != "planner_check":
                return self._fail(run, "저장된 점검 Agent 역할이 다릅니다.")
            scope = self._ports.agent.validate_saved_scope(
                "planner_check", worktree, saved_row.agent_snapshot,
            )
            if scope.error:
                return self._fail(run, scope.error)
            self._ports.agent.clear_scope(repo)

        # Recheck the remote and mutable declaration inside the acceptance
        # transaction. A changed revision with identical content is reusable.
        latest = self._ports.git.fetch_base(
            run.context.repo_path, run.context.base_branch,
        )
        if (self._ports.git.input_fingerprint(run.context.repo_path, latest) != check["input_fingerprint"]
                or self._ports.git.specification_fingerprint(run.context.repo_path, latest) != check["specification_fingerprint"]
                or self._ports.git.plan_declarations(run.context.repo_path, latest)
                != self._ports.git.plan_declarations(run.context.repo_path, run.base_sha)):
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)
        try:
            decision = parse_planner_check_result(
                str(check["result"]), frozenset(snapshot.by_id),
            )
        except PlanningResultInvalid as error:
            return self._fail(run, str(error))
        reason = (
            f"{decision.outcome}: {decision.reason}; 확인={decision.evidence}; "
            f"대상={','.join(decision.plan_ids)}"
        )
        stale_target = False
        try:
            with self._ports.state.transaction():
                current = self._ports.validation.prepare_general_context(
                    repo, run.context.repo_path, latest,
                )
                if target_fingerprint(current) != check["target_fingerprint"]:
                    stale_target = True
                    advanced = False
                else:
                    advanced = self._ports.state.advance_planner_check(
                        repo, "result",
                        "accepted" if decision.outcome == "no_change" else "handoff",
                        expected_input=str(check["input_fingerprint"]),
                        expected_target=str(check["target_fingerprint"]),
                        **({"base_sha": latest} if decision.outcome == "no_change"
                           else {"error": reason}),
                    )
        except PlanningResultInvalid:
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)
        if stale_target:
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)
        if not advanced:
            return self._fail(run, "점검 결과 확정 상태가 변경됐습니다.")
        if decision.outcome != "no_change":
            self._prepare_general_handoff(run)
            self._ports.events.log(f"점검 Planner → 일반 Planner 인계: {reason}")
            return None
        return self._finish_accepted(run)
