"""사용자 원격 변경을 근거로 판단 입력을 정리하는 Manager 경계."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from loop_manager_internal.catalog import execplan_drafts, execplan_catalog
from loop_manager_internal.maintenance.maintenance_planning import markdown_sections
from loop_manager_internal.storage import execplan_draft_state
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.application.result_normalization import normalize_markers, tagged_json


PREFIX = "DRAFT_DECISION_RESULT:"


def questions(text):
    value = execplan_drafts.fields(text).get("Question", "")
    return tuple(re.sub(r"^(?:[-*] |\d+[.)] )", "", line.strip())
                 for line in value.splitlines() if line.strip())


def replace_section(text, heading, body):
    pattern = re.compile(r"^##[ \t]+" + re.escape(heading) + r"[ \t]*\n.*?(?=^##[ \t]+|\Z)", re.M | re.S)
    replacement = f"## {heading}\n\n{body.strip()}\n\n" if body.strip() else ""
    return pattern.sub(lambda _match: replacement, text.replace("\r\n", "\n"))


def user_changes(connection, repo, repository, pinned, *, git, git_bytes):
    """고정한 원격 변경과 기존 Manager 게시 감사를 비교한다."""
    result = {}
    waiting = execplan_draft_state.pending_ids(connection, repo)
    if not waiting:
        return result
    email = git(repository, "config", "--get", "user.email", check=False)
    trusted_email = email.stdout.strip() if email.returncode == 0 else ""
    managed = set()
    for row in connection.execute("SELECT publication_sha, publication_remote_oid, pr_head_oid FROM exec_plans WHERE repo=?", (repo,)):
        managed.update(str(value) for value in row if value)
    for row in connection.execute("SELECT plan_sha, proof_revision FROM execplan_catalog_history WHERE repo=?", (repo,)):
        managed.update(str(value) for value in row if value)
    documents = {}
    def requirement_path(source, revision):
        from loop_manager_internal.catalog import requirement_sources
        from loop_manager_internal.git.command_adapter import revision_requirement_documents
        if revision not in documents:
            documents[revision] = revision_requirement_documents(git_bytes, repository, revision)
        return requirement_sources.resolve_requirement_source(source, documents[revision], allow_archive=False).path
    for plan_id in waiting:
        entry = pinned.snapshot.active.get(plan_id)
        saved = execplan_draft_state.evidence(connection, repo, plan_id)
        if entry is None or not saved:
            continue
        metadata = entry.metadata()
        relevant = set(entry.sources) | {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
        same_basis = saved["basis"] == {
            "scope": metadata.get("draft_scope"),
            "inputs": {key: value for key, value in (pinned.draft_inputs or {}).items() if key in relevant}}
        previous = str(saved["remote_revision"])
        changes = dict(saved.get("user_changes", {}))
        ancestry = git(repository, "merge-base", "--is-ancestor", previous, pinned.revision_sha, check=False)
        if ancestry.returncode != 0:
            result[plan_id] = {}
            continue
        aliases = {source: requirement_path(source, pinned.revision_sha) for source in entry.sources
                   if source.startswith("docs/requirements/") and source not in (pinned.regular_paths or ())}
        paths = {entry.path, *entry.sources, *filter(None, aliases.values()), "docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
        for path in paths:
            if not path.startswith("docs/") or path not in (pinned.regular_paths or ()):
                continue
            old = git(repository, "show", f"{previous}:{path}", check=False)
            if old.returncode and path.startswith("docs/requirements/"):
                source = next((source for source, target in aliases.items() if target == path), path)
                previous_path = requirement_path(source, previous)
                if previous_path:
                    old = git(repository, "show", f"{previous}:{previous_path}", check=False)
            if old.returncode:
                changes.pop(path, None)
                continue  # 새 경로만으로 이전 질문의 사용자 답변을 증명하지 않는다.
            current = git_bytes(repository, "show", f"{pinned.revision_sha}:{path}").decode("utf-8")
            if (path == entry.path and same_basis and changes.get(path, {}).get("verified")
                    and metadata.get("draft_decision", {}) == saved.get("decision", {})):
                # Progress/log edits add no decision input. Keep the proven
                # answer's original delta across the checked remote lineage.
                continue
            if execplan_drafts.content_digest(old.stdout.encode()) == execplan_drafts.content_digest(current.encode()):
                # Observation/partial publication may advance the baseline
                # without consuming the answer. Keep its original provenance.
                saved_after = changes.get(path, {}).get("after")
                published_cleanup = path == entry.path and any(
                    audit.get("content_hash") == execplan_catalog.plan_content_hash(current.encode())
                    and audit.get("user_changes", {}).get(path) == changes.get(path)
                    for audit in saved.get("resolutions", ()))
                if saved_after is None or (execplan_drafts.content_digest(saved_after.encode()) != execplan_drafts.content_digest(current.encode())
                                           and not published_cleanup):
                    changes.pop(path, None)
                continue
            author = git(repository, "log", "--follow", "--format=%H%x00%ae", f"{previous}..{pinned.revision_sha}", "--", path, check=False)
            commits = [line.split("\0") for line in author.stdout.splitlines() if line]
            values = commits[0] if commits else []
            # A last user edit cannot adopt intervening Agent/Manager changes.
            verified = bool(author.returncode == 0 and commits and trusted_email and all(
                len(value) == 2 and value[1] == trusted_email and value[0] not in managed for value in commits))
            if verified:
                for revision in managed:
                    for related in {path, *(source for source, target in aliases.items() if target == path)}:
                        source = git(repository, "show", f"{revision}:{related}", check=False)
                        if source.returncode == 0 and source.stdout.replace("\r\n", "\n") == current.replace("\r\n", "\n"):
                            verified = False
                            break
                    if not verified:
                        break
            changes[path] = {"path": path, "revision": pinned.revision_sha,
                             "input_path": next((source for source, target in aliases.items() if target == path), path),
                             "commit": values[0] if len(values) == 2 else "",
                             "author": values[1] if len(values) == 2 else "",
                             "verified": verified, "before": old.stdout, "after": current}
        result[plan_id] = changes
    return result


def prepare(connection, repo, worktree, snapshot):
    """Planner 의미 판단을 검증하고 게시할 같은 ID의 문서만 정리한다."""
    project = connection.execute("SELECT planning_result FROM projects WHERE repo=?", (repo,)).fetchone()
    output = str(project[0] or "") if project else ""
    normalized = normalize_markers(output, (PREFIX,))
    assessments = {}
    if any(line.startswith(PREFIX) for line in normalized.splitlines()):
        try:
            values = tagged_json(output, PREFIX)
            if not isinstance(values, list):
                raise ValueError("결과 배열 필요")
            for item in values:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or item["id"] in assessments or item["id"] not in snapshot.by_id
                        or set(item) - {"id", "revision", "resolved", "remaining", "outcome", "reason"}
                        or item.get("outcome") not in {None, "uncertain", "valid"}):
                    raise ValueError("잘못된 계획 identity")
                assessments[item["id"]] = item
        except (ValueError, TypeError) as error:
            raise PlannerValidationError("draft 질문 해소 결과가 유효하지 않습니다.") from error
    prepared = {}
    for original in snapshot.plans:
        saved = execplan_draft_state.evidence(connection, repo, original.plan_id)
        if not saved or not saved.get("pending") or original.execution_state and json.loads(original.execution_state).get("ever_started"):
            continue
        path = Path(worktree) / original.path
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8")
        receipt = saved.get("prepared_resolution")
        if (receipt and receipt["revision"] == saved["revision"] and receipt["basis"] == saved["basis"]
                and execplan_draft_state.resolution_matches(saved, receipt)
                and receipt["content_hash"] == execplan_catalog.plan_content_hash(path.read_bytes())):
            prepared[original.plan_id] = receipt
            continue
        plan = execplan_catalog.parse_execplan_text(original.path, text, validate_sources=False)
        execplan_drafts.validate_agent_change(plan, original.text)
        assessment = assessments.get(original.plan_id)
        question_items = questions(saved["question"])
        decision = saved.get("decision", {})
        if execplan_drafts.fields(markdown_sections(original.text).values.get("User Decision", "")) != decision:
            raise PlannerValidationError("User Decision의 현재 원문이 변경됐습니다.")
        if assessment is None or assessment.get("outcome") == "uncertain":
            continue
        if (assessment.get("outcome") is None and assessment.get("resolved") == []
                and assessment.get("revision") == saved["revision"] and plan.get("draft")
                and isinstance(assessment.get("remaining"), str)
                and assessment["remaining"].strip() == saved["question"].strip()):
            continue
        if decision.get("Decision") in {"reject", "defer"}:
            raise PlannerValidationError("거절·보류한 사용자 결정을 해소할 수 없습니다.")
        if (decision.get("Decision") and not saved.get("decision_ready")) or (decision.get("Revision") and decision["Revision"] != saved["revision"] and not saved.get("decision_ready")):
            raise PlannerValidationError("User Decision의 현재 사용자 판단 근거를 확인할 수 없습니다.")
        if assessment.get("revision") != saved["revision"]:
            raise PlannerValidationError("draft 판단 revision이 변경됐습니다.")
        if assessment.get("outcome") == "valid":
            if (not saved.get("reassessment") or plan.get("draft") or plan.get("draft_question")
                    or plan.get("draft_decision") or not saved.get("resolutions")
                    or plan.get("draft_scope") != saved["basis"]["scope"]
                    or not isinstance(assessment.get("reason"), str) or not assessment["reason"].strip()
                    or assessment.get("resolved", []) != [] or assessment.get("remaining", "") != ""):
                raise PlannerValidationError("기존 결정의 현재 유효성을 확인할 수 없습니다.")
            note = "- 기존 사용자 결정 유효성 재검토 (" + saved["revision"] + "): " + " ".join(assessment["reason"].split())
            log = markdown_sections(text).values.get("Decision Log")
            cleaned = (replace_section(text, "Decision Log", log.rstrip() + "\n" + note)
                       if log is not None else text.rstrip() + "\n\n## Decision Log\n\n" + note + "\n")
        else:
            try:
                cleaned = _clean_answers(original, text, saved, assessment, question_items)
            except RuntimeError as error:
                raise PlannerValidationError(str(error)) from error
        parsed = execplan_catalog.parse_execplan_text(original.path, cleaned, validate_sources=False)
        if parsed["id"] != original.plan_id or parsed["sources"] != plan["sources"] or parsed["depends_on"] != plan["depends_on"]:
            raise PlannerValidationError("결정 입력 정리가 계획 identity·관계를 변경했습니다.")
        resolution = {"revision": saved["revision"], "basis": saved["basis"],
                      "decision": saved.get("decision"),
                      "decision_evidence": saved.get("decision_evidence"),
                      "assessment": assessment, "user_changes": saved["user_changes"],
                      "original": original.text, "content_hash": execplan_catalog.plan_content_hash(cleaned.encode())}
        cleaned = cleaned.rstrip() + "\n"
        resolution["content_hash"] = execplan_catalog.plan_content_hash(cleaned.encode())
        execplan_draft_state.prepare_resolution(connection, repo, original.plan_id, resolution)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".decision-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(cleaned.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(path.stat().st_mode)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        prepared[original.plan_id] = resolution
    return prepared


def _clean_answers(original, text, saved, assessment, question_items):
    if len(set(question_items)) != len(question_items):
        raise PlannerValidationError("중복된 draft 질문을 해소할 수 없습니다.")
    resolved = assessment.get("resolved")
    remaining = assessment.get("remaining")
    if not isinstance(resolved, list) or not resolved or not isinstance(remaining, str):
        raise PlannerValidationError("draft 질문별 답변과 남은 질문이 필요합니다.")
    fragment = markdown_sections(remaining).values
    if fragment:
        if set(fragment) != {"Draft Decision"}:
            raise PlannerValidationError("남은 질문에 다른 문서 절을 포함할 수 없습니다.")
        remaining = fragment["Draft Decision"]
    answered = set()
    decision_quotes = set()
    for item in resolved:
        item_decision_quotes = set()
        if (not isinstance(item, dict) or not {"question", "answer", "source", "quote"} <= set(item)
                or set(item) - {"question", "answer", "source", "quote", "evidence"}):
            raise PlannerValidationError("draft 질문 해소 근거가 유효하지 않습니다.")
        if not isinstance(item["question"], str) or not isinstance(item["source"], str):
            raise PlannerValidationError("draft 질문·근거 경로는 문자열이어야 합니다.")
        if (item["question"] not in question_items or item["question"] in answered
                or not isinstance(item["answer"], str) or not item["answer"].strip()):
            raise PlannerValidationError("사용자 변경에서 draft 질문 해소를 확인할 수 없습니다.")
        extra = item.get("evidence", [])
        if not isinstance(extra, list):
            raise PlannerValidationError("질문별 추가 근거가 유효하지 않습니다.")
        references = [{"source": item["source"], "quote": item["quote"]}, *extra]
        for reference in references:
            if (not isinstance(reference, dict) or set(reference) != {"source", "quote"}
                    or not isinstance(reference["source"], str) or not isinstance(reference["quote"], str)
                    or not reference["quote"].strip()):
                raise PlannerValidationError("질문별 추가 근거가 유효하지 않습니다.")
            source, quote = reference["source"], reference["quote"]
            change = saved.get("user_changes", {}).get(source, {})
            if not change.get("verified") or quote not in change["after"] or quote in change["before"]:
                raise PlannerValidationError("사용자 변경에서 draft 질문 해소를 확인할 수 없습니다.")
            if source == original.path:
                current = original.text
                decision_body = markdown_sections(current).values.get("User Decision", "")
                if quote in decision_body:
                    after_decision = markdown_sections(change["after"]).values.get("User Decision", "")
                    prior = execplan_drafts.fields(after_decision)
                    current_decision = execplan_drafts.fields(decision_body)
                    consumed = set()
                    for audit in saved.get("resolutions", []):
                        if audit.get("user_changes", {}).get(source) != change:
                            continue
                        for resolved_item in audit.get("assessment", {}).get("resolved", []):
                            refs = [{"source": resolved_item["source"], "quote": resolved_item["quote"]}, *resolved_item.get("evidence", [])]
                            consumed.update(ref["quote"].strip() for ref in refs if ref["source"] == source)
                    remaining_answer = "\n".join(line for line in prior.get("Answer", "").splitlines() if line.strip() not in consumed).strip()
                    same_decision = current_decision == {**prior, "Answer": remaining_answer}
                    evidence = execplan_draft_state.decision_evidence(saved["basis"], saved.get("decision"), change)
                    if (not same_decision or quote not in current_decision.get("Answer", "")
                            or (not saved.get("decision_ready") and saved.get("decision_evidence") != evidence)):
                        raise PlannerValidationError("User Decision의 현재 판단 근거를 확인할 수 없습니다.")
                    decision_quotes.add(quote.strip())
                    item_decision_quotes.add(quote.strip())
            else:
                current = change["after"]
                if saved["basis"]["inputs"].get(change.get("input_path", source)) != execplan_drafts.content_digest(current.encode()):
                    raise PlannerValidationError("사용자 변경의 현재 source 근거가 변경됐습니다.")
            if quote not in current:
                raise PlannerValidationError("현재 원격 원문에 사용자 답변이 없습니다.")
        if item_decision_quotes and not saved.get("decision_ready"):
            decision_revision = saved.get("user_changes", {}).get(original.path, {}).get("revision")
            changed_sources = {path for path, change in saved.get("user_changes", {}).items()
                               if path != original.path and change.get("revision") == decision_revision
                               and change.get("before") != change.get("after")}
            if not changed_sources <= {ref["source"] for ref in references}:
                raise PlannerValidationError("같은 관측의 source 변경에 대한 질문별 판단 근거가 필요합니다.")
        sections = markdown_sections(text).values
        body = "\n".join(value for key, value in sections.items() if key not in {"Draft Decision", "User Decision", "Progress"})
        if item["answer"] not in body:
            raise PlannerValidationError("해소된 선택을 ExecPlan 본문에 반영해야 합니다.")
        answered.add(item["question"])
    outstanding = set(question_items) - answered
    if set(questions(remaining)) != outstanding or len(questions(remaining)) != len(outstanding):
        raise PlannerValidationError("부분 해소의 미해결 질문을 보존해야 합니다.")
    if remaining:
        fields = execplan_drafts.fields(remaining)
        if set(fields) != {"Question", "Facts", "Options", "Impact"} or not all(fields.values()):
            raise PlannerValidationError("남은 Draft Decision 필드가 필요합니다.")
    cleaned = replace_section(text, "Draft Decision", remaining)
    if outstanding:
        decision_body = markdown_sections(text).values.get("User Decision", "")
        # Only consume independently quoted lines; a shared free-form answer
        # cannot safely be split by a substring match.
        consumed = decision_quotes
        decision = execplan_drafts.fields(decision_body)
        answer = decision.get("Answer", "")
        lines = answer.splitlines()
        leftover = [line for line in lines if line.strip() not in consumed]
        if answer and not leftover:
            cleaned = replace_section(cleaned, "User Decision", "")
        elif leftover != lines:
            decision_body = re.sub(r"^Answer:[ \t]*.*?(?=^[A-Za-z]+:[ \t]*|\Z)",
                                   lambda _match: "Answer: " + "\n".join(leftover) + "\n\n",
                                   decision_body, count=1, flags=re.M | re.S)
            cleaned = replace_section(cleaned, "User Decision", decision_body)
    if not outstanding:
        decision = execplan_drafts.fields(markdown_sections(text).values.get("User Decision", ""))
        answer = decision.get("Answer", "")
        if decision and (not answer or (answer.strip() not in decision_quotes and any(
                line.strip() and line.strip() not in decision_quotes for line in answer.splitlines()))):
            raise PlannerValidationError("확인하지 않은 User Decision을 정리할 수 없습니다.")
        cleaned = replace_section(cleaned, "User Decision", "")
        cleaned = re.sub(r"^draft:[ \t]*true[ \t]*\n", "", cleaned, count=1, flags=re.M)
    return cleaned
