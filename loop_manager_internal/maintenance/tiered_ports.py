"""Composition boundary for the static index and semantic maintenance units."""
from __future__ import annotations

from loop_manager_internal.application.result_normalization import tagged_json

from dataclasses import replace
import json
from pathlib import Path
import os
import re
import sqlite3

from loop_manager_internal.maintenance import code_index, tiered_review
from loop_manager_internal.storage import maintenance_state


def _root_move_plan_covers(path, destination, text):
    from loop_manager_internal.maintenance import maintenance_planning
    try:
        sections = maintenance_planning.markdown_sections(text).values
    except maintenance_planning.MaintenancePlanningError:
        return False
    if any(not sections.get(name, "").strip() for name in
           ("Purpose", "Acceptance Criteria", "Implementation", "Validation")):
        return False
    scope = "\n".join(sections[name] for name in ("Purpose", "Acceptance Criteria", "Implementation"))
    checks = (
        path in scope, destination in scope,
        bool(re.search(r"(?:import|가져오|모듈\s*(?:경로|참조))", scope, re.I)),
        bool(re.search(r"(?:실행\s*(?:명령|명령어)|launcher|command)", scope, re.I)),
        bool(re.search(r"(?:빌드|배포|build|deploy)", scope, re.I)),
        bool(re.search(r"(?:설정\s*(?:탐색|경로|조회)|config(?:uration)?(?:\s*discovery|\s*path))", scope, re.I)),
        bool(re.search(r"(?:상대\s*경로|relative\s*path)", scope, re.I)),
        bool(re.search(r"(?:문서\s*링크|링크|documentation\s*link|doc\s*link)", scope, re.I)),
        bool(re.search(r"(?:보존|preserv)", scope, re.I)),
        bool(re.search(r"(?:사용자\s*동작|user\s*behavio)", scope, re.I)),
        bool(re.search(r"(?:공개\s*(?:계약|API)|public\s*(?:contract|API))", scope, re.I)),
        bool(re.search(r"(?:저장\s*형식|storage\s*format)", scope, re.I)),
    )
    validation = sections["Validation"]
    checks += (
        bool(re.search(r"targeted", validation, re.I)),
        bool(re.search(r"(?:전체\s*verify|full\s*verify)", validation, re.I)),
        bool(re.search(r"(?:읽기\s*전용.{0,40}(?:Review|검토)|read.only.{0,40}review)",
                       validation, re.I)),
    )
    return all(checks)


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
        if (active is not None and active.get("analysis_version", 1) == code_index.VERSION
                and {"base", "root_source", "root_placements", "root_reviews", "root_classifications"} <= active.keys()):
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
        guidance_hash = (prior.get('guidance_hash') if prior is not None
                         and prior.get('cycle_id') == cycle.cycle_id else None)
        return self.repository.prepare(context.repo, cycle.revision, selection, cycle_id=cycle.cycle_id,
                                       review_mode=review_mode, guidance_hash=guidance_hash)

    def export(self, run, assessment, unit=None):
        directory = run.context.state_dir / "maintenance-evidence"
        directory.mkdir(parents=True, exist_ok=True)
        identifier = unit["digest"] if unit else assessment["digest"]
        target = directory / ("module-" + identifier + ".json")
        payload = {"kind": "static_context_and_semantic_receipts", "source_revision": assessment["source_revision"],
                   "mode": assessment["mode"], "roots": assessment["roots"], "reasons": assessment["reasons"]}
        if unit:
            payload.update(digest=unit["digest"], legacy_context=self._legacy_context(run, unit))
            if unit.get("kind") == "root_placement":
                payload.update(kind="root_placement_review", source=unit["source"],
                               placements=unit["placements"])
            else:
                payload["parts"] = unit["parts"]
                payload["project_modules"] = sorted({code_index.module_group(path) for path in assessment["records"]})
        else:
            evidence = self.repository.evidence(run.context.repo, assessment["cycle"], assessment["source_revision"])
            from loop_manager_internal.storage.maintenance_file_ledger import root_move_candidates
            payload["root_move_candidates"] = root_move_candidates(evidence["root_reviews"])
            payload["assessment"] = evidence
        # No operational sessions or SQLite state are exported to Agents.
        target.write_text(code_index.encode(payload) + "\n", encoding="utf-8")
        return target

    def _legacy_context(self, run, unit):
        """Prior source-validated judgments are context, not fabricated new receipts."""
        if unit.get("kind") == "root_placement":
            return {}
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
        from loop_manager_internal.storage.maintenance_file_ledger import root_move_candidates, root_review_hash
        root_candidates = root_move_candidates(evidence["root_reviews"])
        dispositions_scope = [*findings, *(
            {"path": path, "excerpt": item["reason"], "reason": item["reason"],
             "destination": item["destination"], "root_review_hash": root_review_hash(item),
             "root_move": True}
            for path, item in sorted(root_candidates.items())
        )]
        # The final Planner must link root move candidates to a preservation plan;
        # module findings retain their existing reject/existing/new dispositions.
        if dispositions_scope:
            from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOutcome
            try:
                dispositions = tagged_json(str(run.row.planning_result or ""), "MODULE_FINDINGS_RESULT")
            except (ValueError, RecursionError) as error:
                raise tiered_review.TieredReviewError("모듈·루트 후보 처리 결과 JSON이 잘못됐습니다.") from error
            if not isinstance(dispositions, list) or len(dispositions) != len(dispositions_scope):
                raise tiered_review.TieredReviewError("모듈 발견 또는 루트 이동 후보 처리가 누락됐습니다.")
            known = {str(row["plan_id"]): (str(row["status"]), str(row["path"])) for row in self.connection.execute(
                "SELECT plan_id, status, path FROM exec_plans WHERE repo = ?", (run.context.repo,))}
            def relates(plan_id, index, existing):
                if not isinstance(plan_id, str):
                    return None
                if existing:
                    if plan_id not in known:
                        return None
                    path = run.planning_worktree / known[plan_id][1]
                else:
                    candidates = [path for path in (run.planning_worktree / "docs/plans").iterdir()
                                  if path.name.startswith("[" + str(plan_id) + "] ") and path.suffix == ".md"]
                    if len(candidates) != 1:
                        return None
                    path = candidates[0]
                root = run.planning_worktree.resolve()
                if (path.is_symlink() or not path.resolve().is_relative_to(root)
                        or any(parent.is_symlink() for parent in path.parents if parent != root)
                        or not path.is_file()):
                    return None
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as error:
                    raise tiered_review.TieredReviewError("연결할 계획을 UTF-8 원본으로 읽을 수 없습니다.") from error
                finding = dispositions_scope[index]
                if finding.get("root_move"):
                    if (not _root_move_plan_covers(finding["path"], finding["destination"], text)
                            or existing and known[plan_id][0] in {"MERGED", "CANCELLED"}):
                        return None
                elif finding["path"] not in text:
                    return None
                return (path, text)

            root_links = []
            for index, item in enumerate(dispositions):
                if not isinstance(item, dict) or set(item) != {"index", "status", "plan_id", "reason"} or type(item["index"]) is not int or item["index"] != index:
                    raise tiered_review.TieredReviewError("모듈·루트 후보 처리 identity가 다릅니다.")
                tiered_review._note(item["reason"])
                status, plan_id = item["status"], item["plan_id"]
                if not isinstance(status, str) or (plan_id is not None and not isinstance(plan_id, str)):
                    raise tiered_review.TieredReviewError("모듈·루트 후보 처리 상태·계획 ID 형식이 다릅니다.")
                finding = dispositions_scope[index]
                is_root = bool(finding.get("root_move"))
                if status == "rejected" and plan_id is None and not is_root:
                    continue
                existing = status == "existing_plan"
                linked = relates(plan_id, index, existing) if (
                    (status == "new_plan" and plan_id in decision.plan_ids)
                    or (existing and plan_id in known and known[plan_id][0] != "MERGED")
                ) else None
                if linked is None:
                    raise tiered_review.TieredReviewError("모듈 발견 또는 루트 후보가 유효한 계획에 연결되지 않았습니다.")
                if is_root:
                    path, text = linked
                    root_links.append({"path": finding["path"], "destination": finding["destination"],
                        "root_review_hash": finding["root_review_hash"], "status": status,
                        "plan_id": plan_id, "plan_path": path.relative_to(run.planning_worktree).as_posix(),
                        "plan_hash": tiered_review.plan_content_hash(path.read_bytes()),
                        "reason": item["reason"]})
            new_ids = {item["plan_id"] for item in dispositions if item["status"] == "new_plan"}
            if new_ids != (set(decision.plan_ids) if decision.outcome is MaintenanceOutcome.REFACTORING else set()):
                raise tiered_review.TieredReviewError("새 리팩터링 계획과 모듈 발견 연결이 다릅니다.")
            if root_candidates:
                self.repository.record_root_plan_links(run.context.repo, run.maintenance_cycle.revision,
                    str(run.row.active_sha), root_links, plan_repository=run.planning_worktree)
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
