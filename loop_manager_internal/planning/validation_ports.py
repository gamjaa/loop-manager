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
    git: Callable[..., object] | None = None
    git_bytes: Callable[..., bytes] | None = None


class _PlanningValidationPorts:
    """Bind prompt and validation helpers without lifecycle decisions."""

    def __init__(self, effects: PlanningValidationPortsEffects, connection: sqlite3.Connection,
                 state_dir: Path | None = None):
        self._effects = effects
        self._connection = connection
        self._state_dir = state_dir

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
        prompt += self._requirement_overview_prompt(mutable_snapshot.repo, mutable_snapshot.active_sha)
        prompt += self._verify_request_prompt(mutable_snapshot.repo, mutable_snapshot.active_sha)
        from loop_manager_internal.storage import execplan_draft_state
        import json
        decisions, user_changes, identities = [], {}, {}
        pending = execplan_draft_state.pending_ids(self._connection, mutable_snapshot.repo)
        for plan in mutable_snapshot.plans:
            if plan.plan_id not in pending:
                continue
            saved = execplan_draft_state.evidence(self._connection, mutable_snapshot.repo, plan.plan_id)
            item = dict(id=plan.plan_id, **execplan_draft_state.prompt_evidence(
                self._connection, mutable_snapshot.repo, plan.plan_id))
            references = {}
            for path, change in item.get("user_changes", {}).items():
                identity = json.dumps([path, saved["user_changes"][path]], ensure_ascii=False, sort_keys=True)
                if identity not in identities:
                    key = "change-" + str(len(identities) + 1)
                    identities[identity] = key
                    user_changes[key] = {"path": path, **change}
                references[path] = identities[identity]
            item["user_changes"] = references
            decisions.append(item)
        if decisions:
            prompt += "\n\nManager의 draft 질문·사용자 원격 변경 근거:\n" + json.dumps(
                {"decisions": decisions, "user_changes": user_changes}, ensure_ascii=False)
            prompt += ("\n본문 변경 또는 User Decision의 질문 해소를 판단할 때 한 줄 DRAFT_DECISION_RESULT: JSON 배열을 반환하라. "
                       "각 객체는 id, revision(위 판단 지문), resolved(질문별 question·answer·source·quote), "
                       "remaining(남은 Question/Facts/Options/Impact 본문 또는 빈 문자열)을 가진다. "
                       "decisions의 user_changes는 source 경로별 공통 근거 ID이며 최상위 user_changes[ID].diff가 저장된 before/after의 문맥 diff다. resolved.source에는 근거 ID 대신 해당 source 경로를 반환하라. 필요하면 해당 revision의 원문을 읽어라. "
                       "question은 기존 질문의 정확한 문장, quote는 verified 사용자 변경의 추가 원문이며 저장된 after에 있고 before에는 없어야 한다. "
                       "같은 질문의 여러 근거는 resolved 항목의 evidence 배열에 source·quote 객체로 추가하고, 각 원문이 같은 선택을 뒷받침하는지 확인하라. "
                       "User Decision을 소비하려면 해당 Answer의 실제 근거도 인용하라. 충돌하거나 불충분하면 uncertain으로 유지하라. "
                       "명시 승인 없이 Answer를 인용할 때 같은 관측에서 변경된 각 source의 추가 원문도 그 질문의 근거로 인용하라. "
                       "answer는 제품 동작·설계 선택으로 해당 계획 본문에 통합하라. "
                       "승인 표시나 답변 존재만으로 해소하지 말고 질문별 의미와 충분성을 확인하라. "
                       "일부 해소는 남은 질문을 보존하고 불확실하면 outcome: uncertain으로 반환하라. "
                       "reassessment: true는 양식 정리 후 입력 변경에 따른 기존 결정의 유효성 재검토다. "
                       "resolutions의 이전 판단과 현재 목표·source를 비교하라. 기존 선택이 여전히 유효하면 "
                       "id·revision·outcome: valid·reason(현재에도 유효한 이유)을 반환하며 새 사용자 승인을 만들지 마라. "
                       "새 사용자 판단이 필요하면 해당 계획을 draft로 개정하고 필요한 새 질문만 작성하라. "
                       "draft·User Decision을 직접 제거하지 마라. 검증·정리·대기 전환은 Manager가 수행한다.")
        return prompt

    def _requirement_overview_prompt(self, repo: str, active_sha: str) -> str:
        if self._effects.git_bytes is None:
            return ""
        from loop_manager_internal.planning.requirement_overview import instruction
        return instruction(Path(repo), active_sha, git_bytes=self._effects.git_bytes)

    def _verify_request_prompt(self, repo: str, active_sha: str) -> str:
        if self._effects.git is None or self._effects.git_bytes is None:
            return ""
        project = self._connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
        ).fetchone()
        if project is None:
            return ""
        from loop_manager_internal.planning.verify_input_request import instruction
        return instruction(self._connection, repo, str(project["base_branch"]), active_sha,
                           git=self._effects.git, git_bytes=self._effects.git_bytes)

    def validate_manager_source(self, repo: str, worktree: Path, path: str, plan) -> None:
        from loop_manager_internal.storage import verify_input_requests
        verify_input_requests.validate(self._connection, repo,
            (worktree / path).read_text(encoding="utf-8"), plan["sources"])

    def maintenance_prompt(
        self,
        context: MaintenancePlanningContext,
        catalog: str,
        change_context: str,
        error: str | None,
        *,
        include_general_planning: bool,
    ) -> str:
        prompt = self._effects.maintenance_planner_prompt(
            context,
            catalog,
            change_context,
            error,
            include_general_planning=include_general_planning,
        )
        if include_general_planning:
            prompt += self._requirement_overview_prompt(context.repo, context.active_sha)
            prompt += self._verify_request_prompt(context.repo, context.active_sha)
        return prompt

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
            state_dir=self._state_dir,
        )
        from loop_manager_internal.documents import document_drafts
        repository = Path(repo)
        if not self._effects.is_loop_manager_repository(repository):
            return result
        claims = []
        for path, plan in result.plans if result is not None else ():
            text = document_publication.read_plan_text(worktree / path)
            try:
                claim = document_publication.plan_claim(text)
            except document_drafts.DocumentDraftError as error:
                raise PlannerValidationError(str(error)) from error
            if claim is not None:
                claims.append((path, text, plan, claim))
        if claims:
            project = self._connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
            ).fetchone()
            if project is None:
                return result
            drafts = document_drafts.DocumentDraftRepository(self._connection)
            candidate = drafts.planning_candidate(
                repo, repository, str(project["base_branch"]),
            )
            if (candidate is None or len(claims) != 1
                    or claims[0][3] != candidate.draft_id
                    or claims[0][0] not in result.publication_paths):
                raise PlannerValidationError("승인 문서 ExecPlan identity가 현재 승인과 다릅니다.")
            try:
                drafts.assert_current(candidate, repository, str(project["base_branch"]))
                document_publication.validate_document_plan(
                    claims[0][1], claims[0][2], candidate,
                )
            except document_drafts.DocumentDraftError as error:
                drafts.record_publication_error(candidate.draft_id, str(error))
                raise PlannerValidationError(str(error)) from error
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
