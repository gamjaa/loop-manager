"""Bounded multi-unit Agent calls with the existing individual evidence contracts."""
from __future__ import annotations

import json
import re
import time

from loop_manager_internal.maintenance import maintenance_file_review as files
from loop_manager_internal.maintenance import maintenance_reconciliation as relations
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError
from loop_manager_internal.planning.agent_ports import run_scoped_planner_turn
from loop_manager_internal.planning.planning_contracts import (
    PlanningAgentResultKind, PlanningOutcome, PlanningOutcomeKind,
)


MAX_BATCH_UNITS = 12
MAX_BATCH_BYTES = 192_000


class BatchResultError(ValueError):
    """An Agent returned duplicate, foreign or malformed batch receipts."""


def batch_outputs(output: str, prefix: str, identity: str,
                  expected: set[str]) -> dict[str, str]:
    """Keep each JSON receipt separate and reject foreign/duplicate identities."""
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise BatchResultError(f"일괄 검토 JSON의 중복 key: {key}")
            value[key] = item
        return value

    def reject_constant(value):
        raise BatchResultError(f"일괄 검토 JSON에 허용되지 않은 값: {value}")

    decoder = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=reject_constant)
    markers = list(re.finditer(r"(?m)^[ \t]*" + re.escape(prefix.rstrip()) + r"[ \t]*", output))
    receipts = {}
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(output)
        candidate = output[marker.end():end].lstrip()
        fence = re.match(r"```(?:json)?[ \t]*\r?\n", candidate)
        if fence:
            candidate = candidate[fence.end():].lstrip()
        try:
            value, end = decoder.raw_decode(candidate)
        except ValueError as error:
            raise BatchResultError("일괄 검토 결과 JSON이 올바르지 않습니다.") from error
        if candidate[end:].strip() not in {"", "```"}:
            raise BatchResultError("일괄 검토 JSON 뒤에 별도의 결과 또는 설명이 있습니다.")
        key = value.get(identity) if isinstance(value, dict) else None
        if not isinstance(key, str) or key not in expected or key in receipts:
            raise BatchResultError("일괄 검토 결과에 중복 또는 범위 밖 identity가 있습니다.")
        receipts[key] = prefix + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return receipts


def batch_prompt(prompts: list[str], prefix: str) -> str:
    return (
        "다음 검토 단위를 하나의 작업에서 함께 검토하라. 관련 문맥은 공유하고 이미 읽은 "
        "원본을 불필요하게 다시 읽지 마라. 모든 단위의 기존 근거·identity 계약은 유지한다. "
        "파일과 문서는 수정하지 마라. 각 단위마다 해당 identity의 결과를 정확히 한 번, "
        f"독립된 {prefix}한 줄 JSON으로 반환하라. 배열이나 Markdown으로 묶지 마라. "
        "실제로 검토하지 못한 단위의 완료 결과를 만들지 마라.\n\n"
        + "\n\n--- 다음 검토 단위 ---\n\n".join(prompts)
        + "\n\n위 예시의 단일 object 조건은 각 단위에 적용한다. 최종 응답에는 요청한 모든 "
        "단위의 identity별 결과를 각각의 marker와 함께 기록하라. 마지막 단위만 응답하지 마라."
    )


class MaintenanceAssessmentBatches:
    def __init__(self, ports):
        self.ports = ports

    def _invoke(self, run, worktree, prompts, prefix, session, save_session):
        started = time.monotonic()
        result, scope = run_scoped_planner_turn(
            self.ports.agent, run.context.repo, "maintenance_planner", worktree,
            batch_prompt(prompts, prefix), session, save_session,
        )
        self.ports.events.log(f"유지보수 일괄 호출 종료: {len(prompts)}개, "
                              f"{time.monotonic() - started:.0f}초")
        if scope.error:
            self.ports.events.error(scope.error)
            return PlanningOutcome(PlanningOutcomeKind.OPERATIONAL_ERROR, error=True)
        if result.code:
            self.ports.events.error(result.message)
            kind = {
                PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE,
            }.get(result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True)
        return result

    def _failure(self, error):
        self.ports.events.error(str(error))
        kind = (PlanningOutcomeKind.REMOTE_BASE_RESTART
                if isinstance(error, SourceInventoryError) else PlanningOutcomeKind.VALIDATION_RETRY)
        return PlanningOutcome(kind, detail=str(error))

    @staticmethod
    def _progress():
        return PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS)

    def review_files(self, run, worktree):
        context, port = run.context, self.ports.file_review
        try:
            progress = port.progress(context.repo, context.repo_path, context.base_branch,
                                     run.maintenance_cycle.revision, str(run.row.active_sha))
            pending = port.pending_units(context.repo, progress)
            if not pending:
                return None
            units, prompts, size = [], [], 0
            for unit in pending:
                if units and (len(units) >= MAX_BATCH_UNITS or unit.session_id or unit.error):
                    break
                content = port.content(context.repo_path, context.base_branch, unit.source)
                prompt = files.review_prompt(unit.source, content, root_placements=progress.root_placements)
                if unit.error:
                    prompt += "\n이전 결과 오류를 수정하라: " + unit.error
                weight = max(len(content), len(prompt.encode()))
                if units and size + weight > MAX_BATCH_BYTES:
                    break
                units.append(unit)
                prompts.append(prompt)
                size += weight
                if unit.session_id or unit.error:
                    break
            for unit in units:
                port.start(context.repo, run.maintenance_cycle.revision, unit.source)
            self.ports.events.log(
                f"유지보수 파일 검토: {len(progress.completed)}/{len(progress.selected)} 완료, "
                f"이번 {len(units)}개, {units[0].source.path}부터")
        except (files.FileReviewError, SourceInventoryError) as error:
            return self._failure(error)

        def save_session(session):
            for unit in units:
                port.session(context.repo, run.maintenance_cycle.revision, unit.source, session)

        result = self._invoke(run, worktree, prompts, files.RESULT_PREFIX,
                              units[0].session_id, save_session)
        if isinstance(result, PlanningOutcome):
            return result
        try:
            receipts = batch_outputs(result.message, files.RESULT_PREFIX, "path",
                                     {unit.source.path for unit in units})
        except BatchResultError as error:
            for unit in units:
                port.fail(context.repo, run.maintenance_cycle.revision, unit.source, str(error))
            return self._failure(error)
        errors, accepted = [], 0
        for unit in units:
            try:
                if unit.source.path not in receipts:
                    raise BatchResultError(f"일괄 검토 결과 누락: {unit.source.path}")
                content = port.content(context.repo_path, context.base_branch, unit.source)
                parsed = files.parse_review(receipts[unit.source.path], unit.source, content)
                port.complete(context.repo, run.maintenance_cycle.revision, unit.source, parsed)
                accepted += 1
            except SourceInventoryError as error:
                return self._failure(error)
            except (BatchResultError, files.FileReviewError) as error:
                port.fail(context.repo, run.maintenance_cycle.revision, unit.source, str(error))
                errors.append(str(error))
        self.ports.events.log(f"유지보수 파일 검토 저장: {accepted}/{len(units)}개")
        return self._failure(BatchResultError("\n".join(errors))) if errors else self._progress()

    def review_relations(self, run, worktree, progress):
        context, port = run.context, self.ports.reconciliation
        completed = {item["key"] for item in progress.completed}
        pending = [item for item in progress.relations if item.key not in completed]
        units, prompts, size, cache = [], [], 0, {}
        try:
            for relation in pending:
                session, error = port.unit(context.repo, relation)
                if units and (len(units) >= MAX_BATCH_UNITS or session or error):
                    break
                for source in (relation.left, relation.right):
                    if source.path not in cache:
                        cache[source.path] = port.content(context.repo_path, context.base_branch, source)
                prompt = relations.relation_prompt(relation, cache[relation.left.path], cache[relation.right.path])
                if error:
                    prompt += "\n이전 결과 오류를 수정하라: " + error
                weight = len(prompt.encode())
                if units and size + weight > MAX_BATCH_BYTES:
                    break
                units.append((relation, session))
                prompts.append(prompt)
                size += weight
                if session or error:
                    break
            if not units:
                return None
            for relation, _ in units:
                port.start(context.repo, run.maintenance_cycle.revision, str(run.row.active_sha), relation)
            self.ports.events.log(f"유지보수 관계 검토: {len(completed)}/{len(progress.relations)} 완료, "
                                  f"이번 {len(units)}개")
        except (relations.ReconciliationError, files.FileReviewError, SourceInventoryError) as error:
            return self._failure(error)

        def save_session(session):
            for relation, _ in units:
                port.session(context.repo, run.maintenance_cycle.revision, relation, session)

        result = self._invoke(run, worktree, prompts, relations.RELATION_PREFIX, units[0][1], save_session)
        if isinstance(result, PlanningOutcome):
            return result
        try:
            receipts = batch_outputs(result.message, relations.RELATION_PREFIX, "key",
                                     {relation.key for relation, _ in units})
        except BatchResultError as error:
            for relation, _ in units:
                port.fail(context.repo, run.maintenance_cycle.revision, relation, str(error))
            return self._failure(error)
        errors = []
        for relation, _ in units:
            try:
                if relation.key not in receipts:
                    raise BatchResultError(f"관계 결과 누락: {relation.key}")
                left = port.content(context.repo_path, context.base_branch, relation.left)
                right = port.content(context.repo_path, context.base_branch, relation.right)
                parsed = relations.parse_relation(receipts[relation.key], relation, left, right)
                port.complete(context.repo, run.maintenance_cycle.revision, relation, parsed)
            except SourceInventoryError as error:
                return self._failure(error)
            except (BatchResultError, relations.ReconciliationError, files.FileReviewError) as error:
                port.fail(context.repo, run.maintenance_cycle.revision, relation, str(error))
                errors.append(str(error))
        return self._failure(BatchResultError("\n".join(errors))) if errors else self._progress()

    def synthesize(self, run, worktree, progress, page):
        context, port = run.context, self.ports.reconciliation
        try:
            candidates = port.synthesis_batch(page, progress, MAX_BATCH_UNITS)
            pages, size = [], 0
            for candidate in candidates:
                weight = len(candidate.text.encode())
                if pages and size + weight > MAX_BATCH_BYTES:
                    break
                pages.append(candidate)
                size += weight
            manifest = port.synthesis_export(context.repo, page, progress,
                                             context.state_dir / "maintenance-evidence")
            self.ports.events.log(f"유지보수 종합: {page.index}/{page.total} 완료, 이번 {len(pages)}개")
            prompts = [relations.synthesis_prompt(item, manifest) for item in pages]
        except (relations.ReconciliationError, SourceInventoryError) as error:
            return self._failure(error)
        result = self._invoke(run, worktree, prompts, relations.SYNTHESIS_PREFIX, page.session_id,
                              lambda session: port.synthesis_session(context.repo, page, session))
        if isinstance(result, PlanningOutcome):
            return result
        try:
            receipts = batch_outputs(result.message, relations.SYNTHESIS_PREFIX, "key",
                                     {item.key for item in pages})
            for item in pages:
                if item.key not in receipts:
                    raise BatchResultError(f"종합 결과 누락: {item.key}")
                current = port.progress(context.repo, context.repo_path, context.base_branch,
                                        run.maintenance_cycle.revision, str(run.row.active_sha))
                fresh = port.synthesis(context.repo, run.maintenance_cycle.revision,
                                       str(run.row.active_sha), current)
                if fresh.key != item.key:
                    raise relations.ReconciliationError("종합 중 source가 바뀌었습니다.")
                session = result.session_id or fresh.session_id
                if not session:
                    raise relations.ReconciliationError("종합 호출의 session이 없습니다.")
                port.synthesis_session(context.repo, fresh, session)
                port.synthesis_complete(context.repo, fresh, receipts[item.key])
        except (BatchResultError, relations.ReconciliationError, files.FileReviewError,
                SourceInventoryError) as error:
            return self._failure(error)
        return self._progress()

    def resolve(self, run, worktree, first):
        context, port = run.context, self.ports.reconciliation
        try:
            units, prompts, size = [], [], 0
            for unit in port.resolution_batch(context.repo, first, MAX_BATCH_UNITS):
                prompt = relations.resolution_prompt(unit)
                if units and size + len(prompt.encode()) > MAX_BATCH_BYTES:
                    break
                units.append(unit)
                prompts.append(prompt)
                size += len(prompt.encode())
            if not units:
                raise relations.ReconciliationError("처리할 최종 판단 단위가 없습니다.")
            self.ports.events.log(f"유지보수 최종 판단 확인: {first.index}번부터 {len(units)}개")
        except relations.ReconciliationError as error:
            return self._failure(error)

        def save_session(session):
            for unit in units:
                port.resolution_session(context.repo, unit, session)

        result = self._invoke(run, worktree, prompts, relations.RESOLUTION_PREFIX,
                              first.session_id, save_session)
        if isinstance(result, PlanningOutcome):
            return result
        try:
            receipts = batch_outputs(result.message, relations.RESOLUTION_PREFIX, "key",
                                     {unit.assessment["key"] for unit in units})
        except BatchResultError as error:
            for unit in units:
                port.resolution_fail(context.repo, unit, str(error))
            return self._failure(error)
        errors = []
        for unit in units:
            try:
                key = unit.assessment["key"]
                if key not in receipts:
                    raise BatchResultError(f"최종 판단 결과 누락: {key}")
                port.resolution_complete(context.repo, unit, receipts[key])
            except (BatchResultError, relations.ReconciliationError) as error:
                port.resolution_fail(context.repo, unit, str(error))
                errors.append(str(error))
        return self._failure(BatchResultError("\n".join(errors))) if errors else self._progress()
