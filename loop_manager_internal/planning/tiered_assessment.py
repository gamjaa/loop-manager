"""Static quick checks and bounded semantic module review in the planning slot."""
from __future__ import annotations

import json
import time

from loop_manager_internal.maintenance import tiered_review
from loop_manager_internal.planning.agent_ports import run_scoped_planner_turn, planner_progress
from loop_manager_internal.planning.planning_contracts import PlanningAgentResultKind, PlanningOutcome, PlanningOutcomeKind


def enabled(ports, run):
    from loop_manager_internal.storage.maintenance_state import MaintenancePhase
    if not (hasattr(ports, "tiered") and run.maintenance_planning
            and run.maintenance_cycle.phase is MaintenancePhase.ASSESSMENT):
        return False
    active = ports.tiered.repository.active(run.context.repo, run.maintenance_cycle.revision, str(run.row.active_sha))
    return active is not None or (run.maintenance_cycle.publication_stage is None and not run.row.planning_result_completed)


def prepare_turn(ports, run, worktree):
    """Return (outcome, final-context); no-file/model result still uses safe publication."""
    port, context = ports.tiered, run.context
    assessment = port.prepare(run)
    pending = port.repository.pending(context.repo, assessment)
    units = port.repository.units(assessment)
    # Count receipts directly instead of regenerating a different batch partition.
    count = 0
    for unit in units:
        row = port.connection.execute("SELECT result_json IS NOT NULL FROM tiered_maintenance_units WHERE repo = ? AND digest = ?",
                                      (context.repo, unit["digest"])).fetchone()
        count += bool(row and row[0])
    ports.events.log(f"유지보수 {assessment['mode']} 점검: source={assessment['source_revision']} "
        f"이유={','.join(assessment['reasons'])} 정적={len(assessment['records'])}파일 "
        f"정밀={count}/{len(units)}묶음")
    if pending is not None:
        manifest = port.export(run, assessment, pending)
        if ports.git.changed_files(worktree) or ports.git.run(worktree, "rev-parse", "HEAD").stdout.strip() != run.row.active_sha:
            raise tiered_review.TieredReviewError("모듈 검토는 고정된 깨끗한 planning worktree에서만 실행합니다.")
        if pending.get("kind") == "root_placement":
            from loop_manager_internal.maintenance.maintenance_file_inventory import RootPlacement, TrackedSource
            from loop_manager_internal.maintenance.maintenance_file_review import review_prompt, source_bytes
            source = TrackedSource(**pending["source"])
            content = source_bytes(context.repo_path, context.base_branch, source)
            prompt = review_prompt(source, content,
                root_placements=tuple(RootPlacement(**item) for item in pending["placements"]))
            prompt += f"\n이 조사는 현재 tiered assessment에 속합니다. unit identity: {pending['digest']}\n"
            progress_label = f"유지보수 루트 조사 ({context.repo})"
            progress_index = "루트 항목"
        else:
            prompt = (
                "현재 작업은 유지보수 모듈 의미 검토다. 파일·문서·Git 상태를 변경하거나 테스트·commit·push를 실행하지 마라. "
                "Manager가 계산한 정적 사실은 파일별 모델 검토 완료를 뜻하지 않는다. manifest의 parts 순서대로 "
                "symbols_truncated 항목은 일부 심벌 개요이므로 필요하면 실제 원본에서 전체 심벌을 조회하라. "
                "모듈 책임·계약·상태 전이·복구·중복·결합과 관련 테스트를 함께 검토하고 필요한 실제 원본을 읽어라. "
                "관련 PRODUCT·활성 Requirement·ARCHITECTURE의 동작 보존 조건도 확인하라. "
                "전체 점검이면 후보가 없는 모듈도 책임·관계를 검토하라. legacy_context는 같은 원본의 기존 "
                "검토 근거이므로 구조 설명을 다시 만드는 대신 의미 판단에 재사용하라. 정적으로 확인된 import 관계를 "
                "별도 파일·쌍별 호출로 재검증하지 마라. whitespace_only가 참인 파일의 빈 내용은 "
                "Manager 확정 사실이므로 설명을 다시 쓰지 말고 같은 모듈의 의미 있는 원본을 인용하라. "
                "묶음 전체가 공백 원본이지만 분석 불확실성이 남아 있으면 그 불확실성과 모듈 배치를 검토하고 "
                "실제 공백 원본을 인용하라. 0 byte 원본의 excerpt는 빈 문자열이며, 미지원 관계를 확정하지 마라. "
                "확인하지 않은 모듈을 완료로 주장하지 마라. "
                "불확실성을 해소할 수 없으면 findings에 구체적인 원본과 이유를 기록하라. 원본에 결합된 판단만 반환하라.\n"
                f"검증된 manifest: {manifest}\n"
                "최종 응답은 다음 prefix의 JSON 한 줄이다. digest는 manifest 값, reviews는 parts와 같은 순서이며 "
                "각 summary는 12~2000자, citations는 해당 part 원본의 path/excerpt 배열(1~16개)이다. "
                "각 excerpt는 원본의 정확한 연속 문자열(최대1200자)이다. findings는 최대64개 path/excerpt/reason "
                "object 배열이며 구조 문제가 없으면 빈 배열이다.\n"
                + tiered_review.RESULT_PREFIX + json.dumps({"digest": pending["digest"], "reviews": [
                    {"module": part["module"], "summary": "실제 모듈 책임과 관계에 대한 구체적 판단",
                     "citations": [{"path": next(iter(part["records"])), "excerpt": "실제 원본 인용"}]}
                    for part in pending["parts"]], "findings": []}, ensure_ascii=False)
            )
            progress_label = f"유지보수 정밀 {count + 1}/{len(units)}묶음 ({context.repo})"
            progress_index = f"{count + 1}/{len(units)}묶음"
        if pending["error"]:
            prompt += "\n이전 결과 검증 오류: " + pending["error"]
        ports.events.log(f"유지보수 정밀 모델 호출 시작: {progress_index}")
        started = time.monotonic()
        with planner_progress(ports.events, progress_label):
            result, scope = run_scoped_planner_turn(ports.agent, context.repo, "maintenance_planner", worktree,
                prompt, pending["session_id"], lambda session: port.repository.session(context.repo, pending, session),
                purpose="maintenance_review", target=str(pending["digest"]), read_only=True)
        ports.events.log(f"유지보수 정밀 모델 호출 종료: {time.monotonic() - started:.0f}초")
        if scope.error:
            ports.events.error(scope.error)
            return PlanningOutcome(PlanningOutcomeKind.OPERATIONAL_ERROR, error=True), None
        if result.code:
            ports.events.error(result.message)
            kind = {PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                    PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE}.get(
                        result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True), None
        if ports.git.changed_files(worktree) or ports.git.run(worktree, "rev-parse", "HEAD").stdout.strip() != run.row.active_sha:
            raise tiered_review.TieredReviewError("읽기 전용 정밀 검토가 planning worktree를 변경했습니다.")
        try:
            port.repository.complete(context.repo, pending, result.message,
                lambda path: port.content(context.repo_path, str(run.row.active_sha), path))
        except tiered_review.TieredReviewError as error:
            port.repository.fail(context.repo, pending, str(error))
            from loop_manager_internal.planning.planning_contracts import MAX_PLANNER_VALIDATION_RETRIES
            retries = run.row.planning_retry_count + 1
            values = {"planning_retry_count": retries}
            if retries >= MAX_PLANNER_VALIDATION_RETRIES:
                values.update(planning_error=str(error), planning_error_fingerprint=run.current_fingerprint)
            ports.state.update(context.repo, **values)
            ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error)), None
        ports.state.update(context.repo, planning_retry_count=0, planning_validation_error=None)
        return PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS), None
    evidence = port.repository.evidence(context.repo, assessment["cycle"], assessment["source_revision"])
    findings = [finding for receipt in evidence["receipts"] for finding in receipt["findings"]]
    from loop_manager_internal.storage.maintenance_file_ledger import root_move_candidates
    root_candidates = root_move_candidates(evidence["root_reviews"])
    if not findings and not root_candidates and not run.maintenance_cycle.include_general_planning:
        ports.state.update(context.repo, last_error=None, planning_result_pending_validation=1,
                           planning_result_completed=1, planning_result='MAINTENANCE_RESULT: {"outcome":"no_change"}')
        run.row = ports.state.planning(context.repo)
        run.dirty_files = ports.git.changed_files(worktree)
        ports.events.log("유지보수 점검 완료: 추가 리팩터링 없음, 안전한 게시 확정 후 일반 작업으로 복귀합니다.")
        return None, "no_model_needed"
    manifest = port.export(run, assessment)
    return None, ("\n모듈 의미 검토와 루트 조사는 완료됐다. 다음 manifest의 저장된 판단·발견을 사용하고 "
        "파일별 검토·관계 확인·종합 페이지 호출을 반복하지 마라. 발견은 receipts 순서와 각 findings 순서로 "
        "모듈 발견을 먼저, root_move_candidates를 경로 순으로 이어서 0부터 번호를 매긴다. "
        "모든 루트 이동 후보는 새 계획 또는 그 목표를 이미 포함한 비종료 계획에 연결해야 하며 rejected로 제외할 수 없다. "
        "계획에는 조사된 원본 경로와 목적지, import·실행 명령·빌드/배포·설정 탐색·상대 경로·문서 링크 보존, "
        "사용자 동작·공개 계약·저장 형식 보존, targeted test·전체 verify·읽기 전용 Review 조건을 기록하라. "
        "모듈 발견은 새 계획, 기존 비종료 계획 또는 근거 있는 제외에 연결하라. "
        "최종 응답에 MODULE_FINDINGS_RESULT: 뒤의 JSON 배열을 한 줄로 포함하라. 각 항목의 필드는 "
        "index,status(new_plan|existing_plan|rejected),plan_id(제외는null),reason(12~2000자)다. "
        f"manifest: {manifest}\n")
