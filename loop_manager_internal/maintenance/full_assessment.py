"""Source-bound inventory and bounded evidence for the self repository review."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess


class FullAssessmentError(ValueError):
    pass


RESULT_PREFIX = "FULL_ASSESSMENT_RESULT: "
_EXCLUDED_PARTS = {"archive", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".pytest_cache", ".mypy_cache", "coverage"}
_PRODUCT_DOCS = {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
_PLAN_ID = re.compile(r"G-\d{3,}")


def source_inventory(repository: Path, revision: str) -> dict[str, str]:
    """Hash tracked source bytes at one immutable Git revision."""
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision) is None:
        raise FullAssessmentError("전수 평가 source revision이 유효하지 않습니다.")
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-rz", "--name-only", revision],
        capture_output=True, check=True,
    )
    paths = sorted(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)
    inventory: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        if any(part in _EXCLUDED_PARTS for part in parts):
            continue
        is_doc = path in _PRODUCT_DOCS or (
            len(parts) == 3 and parts[:2] == ["docs", "requirements"]
            and path.endswith(".md")
        )
        is_code = path.endswith((".py", ".sh", ".ps1")) and (
            parts[0] not in {"docs", ".agents"}
        )
        if not (is_doc or is_code):
            continue
        content = subprocess.run(
            ["git", "-C", str(repository), "show", f"{revision}:{path}"],
            capture_output=True, check=True,
        ).stdout
        inventory[path] = hashlib.sha256(content).hexdigest()
    if not _PRODUCT_DOCS <= inventory.keys():
        raise FullAssessmentError("전수 평가 필수 제품 문서가 source revision에 없습니다.")
    return inventory


def inventory_prompt(inventory: dict[str, str], revision: str) -> str:
    paths = sorted(inventory)
    return (
        f"source revision: {revision}\n"
        f"대상 {len(paths)}개 (추적된 코드·테스트·활성 제품 문서):\n"
        + "\n".join(f"{inventory[path]}  {path}" for path in paths)
    )


def assessment_prompt(
    inventory: dict[str, str], revision: str, saved_result: str | None
) -> str:
    prompt = (
        "\n\n일회성 Loop Manager 전수 평가:\n"
        + inventory_prompt(inventory, revision)
        + "\n각 대상의 책임·상태 전이·외부 연동, 이름·제어 흐름·결합도, "
        "중복과 변경 충돌을 검토하라. 생성물·의존성·archive를 제외하라. "
        "G-051의 Agent scope 책임 분리는 이미 처리된 발견으로 기록하라. "
        "기존 비종료 계획과 대조해 새 리팩터링 계획을 중복 생성하지 마라. "
        "문서 원본과 AGENTS.md를 바꾸지 말고 문서 후보와 정책 결정 대상을 구분하라. "
        "선행 READY 계획 실행이 필요해 prerequisites 결과를 반환하면 평가를 재개할 때 "
        "최종 전수 평가 결과를 작성하라. "
        "코드·테스트·문서 각각의 평가 근거와 발견을 최종 응답에 JSON 한 줄로 기록하라. "
        "형식: FULL_ASSESSMENT_RESULT: "
        '{"source_revision":"<SHA>","coverage":{"code":"<근거>",'
        '"tests":"<근거>","documents":"<근거>"},"findings":'
        '[{"id":"<고유-id>","kind":"code|document",'
        '"path":"<대상 경로>","evidence":"<구체적 근거>",'
        '"status":"planned|existing_plan|resolved|decision_required|no_issue|candidate",'
        '"plan_ids":[],"source_hash":"<inventory의 원본 SHA256>"}]}.'
    )
    if saved_result:
        prompt += "\n이전에 같은 source에서 보존된 평가 결과를 재사용하라: " + saved_result
    return prompt


def parse_assessment_result(
    output: str, inventory: dict[str, str], revision: str,
    new_plan_ids: tuple[str, ...], known_plan_ids: set[str],
) -> str:
    """Validate evidence and return canonical JSON suitable for SQLite."""
    lines = [line[len(RESULT_PREFIX):] for line in output.splitlines()
             if line.startswith(RESULT_PREFIX)]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > 65536:
        raise FullAssessmentError("전수 평가 결과 JSON 줄이 하나 필요하며 64 KiB를 넘을 수 없습니다.")
    try:
        report = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise FullAssessmentError("전수 평가 결과가 올바른 JSON이 아닙니다.") from error
    if not isinstance(report, dict) or set(report) != {"source_revision", "coverage", "findings"}:
        raise FullAssessmentError("전수 평가 결과 필드가 올바르지 않습니다.")
    if report["source_revision"] != revision:
        raise FullAssessmentError("전수 평가 결과의 source revision이 다릅니다.")
    coverage = report["coverage"]
    if (not isinstance(coverage, dict) or set(coverage) != {"code", "tests", "documents"}
        or any(not isinstance(value, str) or len(value.strip()) < 12 for value in coverage.values())):
        raise FullAssessmentError("코드·테스트·문서 평가 근거가 각각 필요합니다.")
    findings = report["findings"]
    if not isinstance(findings, list) or len(findings) > 150:
        raise FullAssessmentError("전수 평가 발견 목록은 최대 150개입니다.")
    seen: set[str] = set()
    linked_new: set[str] = set()
    prior_scope_split_recorded = False
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"id", "kind", "path", "evidence", "status", "plan_ids", "source_hash"}:
            raise FullAssessmentError("발견 항목의 필드가 올바르지 않습니다.")
        identifier, kind, path, evidence, status, plan_ids, source_hash = (
            finding[key] for key in ("id", "kind", "path", "evidence", "status", "plan_ids", "source_hash")
        )
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9-]{3,64}", identifier)
            or identifier in seen
            or not isinstance(kind, str) or kind not in {"code", "document"}
            or not isinstance(path, str) or path not in inventory
            or source_hash != inventory[path]
            or not isinstance(evidence, str) or len(evidence.strip()) < 20
            or not isinstance(status, str)
            or not isinstance(plan_ids, list)
            or any(not isinstance(item, str) or _PLAN_ID.fullmatch(item) is None for item in plan_ids)
            or len(plan_ids) != len(set(plan_ids))):
            raise FullAssessmentError("발견 항목의 근거·경로·원본 지문이 유효하지 않습니다.")
        seen.add(identifier)
        if kind == "document":
            if path not in _PRODUCT_DOCS and not path.startswith("docs/requirements/"):
                raise FullAssessmentError("문서 발견 대상이 활성 제품 문서가 아닙니다.")
            if status not in {"candidate", "decision_required", "no_issue"} or plan_ids:
                raise FullAssessmentError("문서 발견은 수정본 후보·결정 대상·문제 없음으로만 기록합니다.")
        else:
            if status not in {"planned", "existing_plan", "resolved", "decision_required", "no_issue"}:
                raise FullAssessmentError("코드 발견 상태가 유효하지 않습니다.")
            if status in {"planned", "existing_plan"} and not plan_ids:
                raise FullAssessmentError("코드 개선 발견에는 연결된 계획 ID가 필요합니다.")
            if status in {"resolved", "decision_required", "no_issue"} and plan_ids:
                raise FullAssessmentError("계획 없는 코드 발견 상태에 plan ID가 있습니다.")
            if status == "planned":
                linked_new.update(plan_ids)
            if status == "resolved" and "G-051" in evidence:
                prior_scope_split_recorded = True
            if status == "existing_plan" and not set(plan_ids) <= known_plan_ids:
                raise FullAssessmentError("기존 계획으로 연결한 ID가 catalog에 없습니다.")
    if linked_new != set(new_plan_ids):
        raise FullAssessmentError("새 리팩터링 계획과 코드 발견의 연결이 일치하지 않습니다.")
    if not prior_scope_split_recorded:
        raise FullAssessmentError("G-051에서 완료된 Agent scope 책임 분리 이력이 필요합니다.")
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
