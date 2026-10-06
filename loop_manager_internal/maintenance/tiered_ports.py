"""Composition boundary for the static index and semantic maintenance units."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import os
import sqlite3

from loop_manager_internal.maintenance import code_index, tiered_review
from loop_manager_internal.storage import maintenance_state


class TieredMaintenancePort:
    def __init__(self, connection):
        self.connection = connection
        self.repository = tiered_review.TieredReviewRepository(connection)

    def select(self, context, revision):
        maintenance = maintenance_state.MaintenanceRepository(self.connection)
        cycle = maintenance.get(context.repo)
        due = self.due(context)
        selection = self.repository.select(context.repo, context.repo_path, context.base_branch, revision,
            context.refactoring_review_interval, maintenance.merged_count(context.repo), force_full=due.required)
        if cycle.phase is maintenance_state.MaintenancePhase.IDLE:
            return replace(selection, required=due.required, paths=selection.paths if due.required else (),
                           reasons=(due.reason,))
        return selection

    def due(self, context):
        return maintenance_state.MaintenanceRepository(self.connection).file_assessment_due(
            context.repo, context.refactoring_review_interval)

    def content(self, repository, revision, path):
        return code_index.git(repository, "show", f"{revision}:{path}")

    def query_context(self, repository, base, revision, directory):
        return prepare_query_context(self.connection, repository, base, revision, directory)

    def prepare(self, run, selection=None):
        context, cycle = run.context, run.maintenance_cycle
        active = self.repository.active(context.repo, cycle.revision, str(run.row.active_sha))
        if active is not None:
            return active
        selection = selection or self.select(context, str(run.row.active_sha))
        if not selection.required:
            selection = replace(selection, required=True)
        if cycle.cycle_start_merged_count is not None:
            selection = replace(selection, merged_count=cycle.cycle_start_merged_count)
        prior = self.repository.latest(context.repo)
        review_mode = None
        if (prior is not None and prior.get("cycle_id") == cycle.cycle_id
                and prior["source_revision"] != str(run.row.active_sha)):
            paths = set(prior["paths"])
            changed = {path for path in paths if prior["records"].get(path) != selection.index.records.get(path)}
            for path in changed:
                for records in (prior["records"], selection.index.records):
                    if path in records:
                        facts = records[path]["facts"]
                        paths.update(facts["dependents"])
                        paths.update(facts["related_tests"])
                        paths.update(ref["path"] for ref in facts["references"])
            if prior["roots"] != list(selection.roots):
                paths.update(path for path in selection.index.records if "/" not in path)
            groups = {code_index.module_group(path) for path in paths}
            paths.update(path for path in selection.index.records if code_index.module_group(path) in groups)
            paths &= selection.index.records.keys()
            mode = prior["mode"]
            if (mode == "full" and paths != selection.index.records.keys()) or (mode == "fast" and selection.mode != "fast"):
                mode = "targeted"
            # Full judgments can be reused for their unchanged original modules;
            # a partial current scope must not claim a full current-source success.
            review_mode = prior.get("review_mode", prior["mode"]) if prior["mode"] != "fast" else mode
            selection = replace(selection, mode=mode, reasons=("resume_related_scope",),
                paths=tuple(sorted(paths)), request_id=prior["request_id"])
        else:
            selection = replace(selection, request_id=cycle.cycle_request_id)
        return self.repository.prepare(context.repo, cycle.revision, selection, cycle_id=cycle.cycle_id, review_mode=review_mode)

    def export(self, run, assessment, unit=None):
        directory = run.context.state_dir / "maintenance-evidence"
        directory.mkdir(parents=True, exist_ok=True)
        identifier = unit["digest"] if unit else assessment["digest"]
        target = directory / ("module-" + identifier + ".json")
        payload = {"kind": "static_context_and_semantic_receipts", "source_revision": assessment["source_revision"],
                   "mode": assessment["mode"], "roots": assessment["roots"], "reasons": assessment["reasons"]}
        if unit:
            payload.update(digest=unit["digest"], parts=unit["parts"], legacy_context=self._legacy_context(run, unit))
            payload["project_modules"] = sorted({code_index.module_group(path) for path in assessment["records"]})
        else:
            payload["assessment"] = self.repository.evidence(run.context.repo, assessment["cycle"], assessment["source_revision"])
        # No operational sessions or SQLite state are exported to Agents.
        target.write_text(code_index.encode(payload) + "\n", encoding="utf-8")
        return target

    def _legacy_context(self, run, unit):
        """Prior source-validated judgments are context, not fabricated new receipts."""
        records = {path: record for part in unit["parts"] for path, record in part["records"].items()}
        result = {}
        for row in self.connection.execute("SELECT path, content_hash, mode, result_json FROM provisional_file_reviews "
                                            "WHERE repo = ? AND result_json IS NOT NULL", (run.context.repo,)):
            record = records.get(row[0])
            if record and (record["content_hash"], record["mode"]) == (row[1], row[2]):
                value = json.loads(row[3])
                judgments = sorted(set(note for key, note in value.get("evidence", {}).items()
                                       if isinstance(note, str)))
                result[row[0]] = {"responsibility": value.get("code_map", {}).get("responsibility", ""),
                                  "judgments": judgments}
        for row in self.connection.execute("SELECT path, content_hash, mode, code_map_json FROM successful_code_maps WHERE repo = ?",
                                          (run.context.repo,)):
            record = records.get(row[0])
            if record and (record["content_hash"], record["mode"]) == (row[1], row[2]) and row[0] not in result:
                result[row[0]] = {"code_map": json.loads(row[3])}
        return result

    def validate_final(self, run, decision):
        evidence = self.repository.evidence(run.context.repo, run.maintenance_cycle.revision, str(run.row.active_sha))
        if evidence is None:
            raise tiered_review.TieredReviewError("정밀 근거가 없습니다.")
        findings = [finding for receipt in evidence["receipts"] for finding in receipt["findings"]]
        # The final Planner must explain how each candidate was disposed of; it
        # may reject a candidate, link an existing plan, or link a newly validated plan.
        if findings:
            from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOutcome
            lines = [line[len("MODULE_FINDINGS_RESULT: "):] for line in str(run.row.planning_result or "").splitlines()
                     if line.startswith("MODULE_FINDINGS_RESULT: ")]
            if len(lines) != 1:
                raise tiered_review.TieredReviewError("각 모듈 발견의 처리 결과가 필요합니다.")
            try:
                dispositions = json.loads(lines[0], object_pairs_hook=tiered_review._unique)
            except ValueError as error:
                raise tiered_review.TieredReviewError("모듈 발견 처리 결과 JSON이 잘못됐습니다.") from error
            if not isinstance(dispositions, list) or len(dispositions) != len(findings):
                raise tiered_review.TieredReviewError("모듈 발견의 처리 결과가 누락됐습니다.")
            known = {str(row["plan_id"]): (str(row["status"]), str(row["path"])) for row in self.connection.execute(
                "SELECT plan_id, status, path FROM exec_plans WHERE repo = ?", (run.context.repo,))}
            def relates(plan_id, index, existing):
                if existing:
                    path = run.planning_worktree / known[plan_id][1]
                else:
                    candidates = [path for path in (run.planning_worktree / "docs/plans").iterdir()
                                  if path.name.startswith("[" + str(plan_id) + "] ") and path.suffix == ".md"]
                    if len(candidates) != 1:
                        return False
                    path = candidates[0]
                return not path.is_symlink() and findings[index]["path"] in path.read_text(encoding="utf-8")
            for index, item in enumerate(dispositions):
                if not isinstance(item, dict) or set(item) != {"index", "status", "plan_id", "reason"} or type(item["index"]) is not int or item["index"] != index:
                    raise tiered_review.TieredReviewError("모듈 발견 처리 identity가 다릅니다.")
                tiered_review._note(item["reason"])
                status, plan_id = item["status"], item["plan_id"]
                if not isinstance(status, str) or (plan_id is not None and not isinstance(plan_id, str)):
                    raise tiered_review.TieredReviewError("모듈 발견 처리 상태·계획 ID 형식이 다릅니다.")
                if status == "rejected" and plan_id is None:
                    continue
                if status == "new_plan" and plan_id in decision.plan_ids and relates(plan_id, index, False):
                    continue
                if status == "existing_plan" and plan_id in known and known[plan_id][0] != "MERGED" and relates(plan_id, index, True):
                    continue
                raise tiered_review.TieredReviewError("모듈 발견이 유효한 처리나 계획에 연결되지 않았습니다.")
            new_ids = {item["plan_id"] for item in dispositions if item["status"] == "new_plan"}
            if new_ids != (set(decision.plan_ids) if decision.outcome is MaintenanceOutcome.REFACTORING else set()):
                raise tiered_review.TieredReviewError("새 리팩터링 계획과 모듈 발견 연결이 다릅니다.")
            return tuple(sorted({item["plan_id"] for item in dispositions if item["status"] == "existing_plan"}))
        elif decision.plan_ids:
            from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOutcome
            if decision.outcome is not MaintenanceOutcome.PREREQUISITES:
                raise tiered_review.TieredReviewError("모듈 발견 없이 리팩터링 계획을 만들 수 없습니다.")
        return ()


def prepare_query_context(connection, repository, base, revision, directory):
    from loop_manager_internal.maintenance.maintenance_file_inventory import cached_source_inventory
    from loop_manager_internal.maintenance.code_context import export_snapshot
    try:
        inventory = cached_source_inventory(connection, repository, base, revision, include_uncertain=True)
        index = code_index.build_index(connection, str(repository.resolve()) + ":remote", inventory,
            lambda source: code_index.git(repository, "show", f"{revision}:{source.path}"))
        snapshot = export_snapshot(directory / "maintenance-evidence", repository, index)
    except (ValueError, OSError, sqlite3.Error):
        return query_instruction()
    return query_instruction(snapshot)


def query_instruction(snapshot=None) -> str:
    script = Path(code_index.__file__).resolve()
    python = "py -3" if os.name == "nt" else "python3"
    index = f' --index "{snapshot}"' if snapshot else ""
    return ("\n필요한 현재 원본의 정적 구조는 다음 읽기 전용 조회로 확인할 수 있다. "
            "--path에는 필요한 저장소 상대경로만 반복 지정하라. 결과는 현재 worktree 파일의 hash와 "
            "심벌·직접 관계이며 의미 검토 성공을 뜻하지 않는다. 동적·미확인 관계는 원본을 확인하라.\n"
            f'{python} "{script}" --worktree .{index} --path <상대경로>\n')
