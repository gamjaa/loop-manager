"""Document contract for ExecPlan authoring readiness and user decisions."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

from loop_manager_internal.maintenance.maintenance_planning import markdown_sections


def content_digest(content: bytes) -> str:
    # Git checkout line endings do not change a textual decision input.
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def input_digests(root: Path, plan: Mapping[str, object]) -> dict[str, str | None]:
    paths = set(plan["sources"]) | {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
    return {path: content_digest((root / path).read_bytes()) if (root / path).is_file() else None
            for path in sorted(paths)}


def fields(text: str) -> dict[str, str]:
    """Read explicit fields; duplicate declarations cannot authorize execution."""
    result = {}
    for match in re.finditer(r"^([A-Za-z]+):[ \t]*(.*)$", text, re.MULTILINE):
        key, value = match.groups()
        if key in result:
            raise RuntimeError(f"ExecPlan decision field가 중복됐습니다: {key}")
        result[key] = value.strip().strip("`")
    return result


def metadata(text: str, values: Mapping[str, object]) -> dict[str, object]:
    draft = values.get("draft", False)
    if not draft and "## Draft Decision" not in text and "## User Decision" not in text:
        return {}
    sections = markdown_sections(text).values
    question = sections.get("Draft Decision", "")
    decision = sections.get("User Decision", "")
    if not draft and not question and not decision:
        return {}
    if draft:
        questions = fields(question)
        if any(not questions.get(name) for name in ("Question", "Facts", "Options", "Impact")):
            raise RuntimeError("draft ExecPlan의 Draft Decision에는 Question/Facts/Options/Impact가 필요합니다.")
        if not sections.get("Purpose") or not sections.get("Acceptance Criteria"):
            raise RuntimeError("draft ExecPlan에는 확정된 Purpose와 Acceptance Criteria가 필요합니다.")
    scope = {key: value for key, value in values.items() if key != "draft"}
    scope["sections"] = {key: value for key, value in sections.items()
                         if key not in {"Progress", "Decision Log", "User Decision"}}
    result: dict[str, object] = {
        "draft": bool(draft), "draft_question": question,
        "draft_scope": hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "draft_decision": fields(decision),
    }
    if question:
        result["draft_question_identity"] = json.dumps(
            (sorted(values["sources"]), *(
                " ".join(value.split()).casefold() for value in (
                    sections.get("Purpose", ""), sections.get("Acceptance Criteria", ""),
                    fields(question).get("Question", "")))), ensure_ascii=False)
    return result


def validate_agent_change(plan: Mapping[str, object], original_text: str | None = None) -> None:
    """Agent results may pose questions, never supply or consume human approval."""
    old = markdown_sections(original_text).values if original_text else {}
    old_decision = fields(old.get("User Decision", ""))
    if plan.get("draft_decision", {}) != old_decision:
        raise RuntimeError("Agent는 ExecPlan User Decision을 작성·변경할 수 없습니다.")
    if original_text:
        from loop_manager_internal.catalog.execplan_catalog import parse_frontmatter
        if parse_frontmatter(original_text).get("draft") == "true" and not plan.get("draft"):
            raise RuntimeError("Agent는 ExecPlan draft를 해제할 수 없습니다.")


def question_identity(plan: Mapping[str, object]) -> str | None:
    if plan.get("draft_question_identity"):
        return str(plan["draft_question_identity"])
    if not plan.get("draft_question"):
        return None
    question = fields(str(plan["draft_question"])).get("Question", "")
    return json.dumps((sorted(plan["sources"]), " ".join(question.split()).casefold()), ensure_ascii=False)


def validate_questions(catalog: Mapping[str, Mapping[str, object]]) -> None:
    questions = {}
    for plan_id, plan in catalog.items():
        key = question_identity(plan)
        if key is not None and key in questions and questions[key] != plan_id:
            raise RuntimeError(f"같은 사용자 판단 질문을 중복 계획할 수 없습니다: {questions[key]}, {plan_id}")
        if key is not None:
            questions[key] = plan_id
