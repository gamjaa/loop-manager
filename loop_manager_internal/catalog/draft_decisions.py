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
                continue  # 새 경로만으로 이전 질문의 사용자 답변을 증명하지 않는다.
            current = git_bytes(repository, "show", f"{pinned.revision_sha}:{path}").decode("utf-8")
            if execplan_drafts.content_digest(old.stdout.encode()) == execplan_drafts.content_digest(current.encode()):
                continue
            author = git(repository, "log", "-1", "--format=%H%x00%ae", pinned.revision_sha, "--", path)
            values = author.stdout.strip().split("\0")
            verified = bool(len(values) == 2 and trusted_email and values[1] == trusted_email and values[0] not in managed)
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
                             "commit": values[0] if len(values) == 2 else "",
                             "author": values[1] if len(values) == 2 else "",
                             "verified": verified, "before": old.stdout, "after": current}
        result[plan_id] = changes
    return result


def prepare(connection, repo, worktree, snapshot):
    """Planner 의미 판단을 검증하고 게시할 같은 ID의 문서만 정리한다."""
    project = connection.execute("SELECT planning_result FROM projects WHERE repo=?", (repo,)).fetchone()
    output = str(project[0] or "") if project else ""
    lines = [line[len(PREFIX):].strip() for line in output.splitlines() if line.startswith(PREFIX)]
    assessments = {}
    if lines:
        try:
            if len(lines) != 1:
                raise ValueError("중복 결과")
            values = json.loads(lines[0])
            if not isinstance(values, list):
                raise ValueError("결과 배열 필요")
            for item in values:
                if not isinstance(item, dict) or item.get("id") in assessments or item.get("id") not in snapshot.by_id:
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
                and receipt["content_hash"] == execplan_catalog.plan_content_hash(path.read_bytes())):
            prepared[original.plan_id] = receipt
            continue
        plan = execplan_catalog.parse_execplan_text(original.path, text, validate_sources=False)
        execplan_drafts.validate_agent_change(plan, original.text)
        assessment = assessments.get(original.plan_id)
        question_items = questions(saved["question"])
        decision = saved.get("decision", {})
        if assessment is None or assessment.get("outcome") == "uncertain":
            continue
        if decision.get("Decision") in {"reject", "defer"}:
            raise PlannerValidationError("거절·보류한 사용자 결정을 해소할 수 없습니다.")
        if assessment.get("revision") != saved["revision"]:
            raise PlannerValidationError("draft 판단 revision이 변경됐습니다.")
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
        for item in resolved:
            if not isinstance(item, dict) or set(item) != {"question", "answer", "source", "quote"}:
                raise PlannerValidationError("draft 질문 해소 근거가 유효하지 않습니다.")
            if not isinstance(item["question"], str) or not isinstance(item["source"], str):
                raise PlannerValidationError("draft 질문·근거 경로는 문자열이어야 합니다.")
            change = saved.get("user_changes", {}).get(item["source"], {})
            if (item["question"] not in question_items or item["question"] in answered
                    or not isinstance(item["answer"], str) or not item["answer"].strip()
                    or not isinstance(item["quote"], str) or not item["quote"].strip()
                    or not change.get("verified") or item["quote"] not in change["after"]
                    or item["quote"] in change["before"]):
                raise PlannerValidationError("사용자 변경에서 draft 질문 해소를 확인할 수 없습니다.")
            if (item["source"] == original.path
                    and item["quote"] in markdown_sections(change["after"]).values.get("User Decision", "")
                    and not saved.get("decision_ready")):
                raise PlannerValidationError("User Decision의 현재 판단 근거를 확인할 수 없습니다.")
            sections = markdown_sections(text).values
            body = "\n".join(value for key, value in sections.items() if key not in {"Draft Decision", "User Decision", "Progress"})
            if item["answer"] not in body:
                raise PlannerValidationError("해소된 선택을 ExecPlan 본문에 반영해야 합니다.")
            answered.add(item["question"])
        outstanding = set(question_items) - answered
        if set(questions(remaining)) != outstanding:
            raise PlannerValidationError("부분 해소의 미해결 질문을 보존해야 합니다.")
        if remaining and any(not execplan_drafts.fields(remaining).get(key) for key in ("Question", "Facts", "Options", "Impact")):
            raise PlannerValidationError("남은 Draft Decision 필드가 필요합니다.")
        cleaned = replace_section(replace_section(text, "Draft Decision", remaining), "User Decision", "")
        if not outstanding:
            cleaned = re.sub(r"^draft:[ \t]*true[ \t]*\n", "", cleaned, count=1, flags=re.M)
        parsed = execplan_catalog.parse_execplan_text(original.path, cleaned, validate_sources=False)
        if parsed["id"] != original.plan_id or parsed["sources"] != plan["sources"] or parsed["depends_on"] != plan["depends_on"]:
            raise PlannerValidationError("결정 입력 정리가 계획 identity·관계를 변경했습니다.")
        resolution = {"revision": saved["revision"], "basis": saved["basis"],
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
