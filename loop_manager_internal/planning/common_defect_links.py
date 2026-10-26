"""Validate Planner cause/goal claims against pinned reports, code and sources."""
from __future__ import annotations

import json
import re

from loop_manager_internal.application.result_normalization import normalize_markers, tagged_json
from loop_manager_internal.git.command_adapter import revision_document_or_none, revision_requirement_documents
from loop_manager_internal.catalog import requirement_sources
from loop_manager_internal.execution.validation_feedback import redact_secrets


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4000
            or any(ord(char) < 32 and char not in "\n\t" for char in value)
            or redact_secrets(value) != value):
        raise ValueError("공통 교정 근거 문자열이 유효하지 않습니다.")
    return value.strip()


def _path(value):
    value = _text(value)
    if (any(part in {"", ".", "..", ".git"} for part in value.split("/"))
            or any(char in value for char in "\\:\n\t")):
        raise ValueError("공통 교정 근거 경로가 유효하지 않습니다.")
    return value


def validate(output, revision, reports, catalog, result, git, repository) -> list[dict]:
    marker = "COMMON_DEFECT_LINKS"
    normalized = normalize_markers(output, (marker,))
    if not re.search(rf"(?m)^{marker}:", normalized):
        return []
    try:
        value = tagged_json(normalized, marker, max_bytes=64000)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("공통 교정 연결 JSON이 유효하지 않습니다.") from error
    if (not isinstance(value, dict) or set(value) != {"revision", "links"}
            or value["revision"] != revision or not isinstance(value["links"], list)
            or len(value["links"]) > 64):
        raise ValueError("공통 교정 연결의 revision·형식이 유효하지 않습니다.")
    originals = {row["id"]: row for row in reports if row["report"] is not None}
    targets = {str(row["plan_id"]): dict(row) for row in catalog}
    allocations = {}
    if result is not None:
        allocations = {(item.source_id, item.source_path): (item.target_id, item.target_path)
                       for item in result.id_allocations}
        proposed = [(path, plan) for path, plan in result.plans]
        proposed += [(item.path, item.plan) for item in result.revisions]
        for path, plan in proposed:
            targets[str(plan["id"])] = dict(plan, plan_id=plan["id"], path=path, target_revision=None)
        for deletion in result.deletions:
            targets.pop(deletion.snapshot.plan_id, None)
    links = []
    seen = set()
    requirements = None
    for raw in value["links"]:
        if not isinstance(raw, dict) or set(raw) != {"reports", "plan_id", "plan_path", "source", "quote", "cause", "completion", "code"}:
            raise ValueError("공통 교정 연결 필드가 유효하지 않습니다.")
        ids = raw["reports"]
        if (not isinstance(ids, list) or not ids or any(type(item) is not int or item not in originals for item in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("공통 교정 연결은 전달된 유효 보고를 인용해야 합니다.")
        requested = _text(raw["plan_id"])
        requested_path = _path(raw['plan_path'])
        plan_id, target_path = allocations.get((requested, requested_path), (requested, requested_path))
        target = targets.get(plan_id)
        if target is None or target['path'] != target_path:
            raise ValueError("공통 교정 계획이 현재 catalog·허용 결과에 없습니다.")
        source, quote = _path(raw["source"]), _text(raw["quote"])
        sources = target["sources"]
        sources = json.loads(sources) if isinstance(sources, str) else sources
        if source not in sources or not (source == "docs/PRODUCT.md" or re.fullmatch(r"docs/requirements/[^/]+\.md", source)):
            raise ValueError("공통 교정은 교정 계획의 현재 활성 source에 속해야 합니다.")
        contents = revision_document_or_none(git, repository, revision, source)
        if contents is None and requirement_sources.source_requirement_id(source) is not None:
            if requirements is None:
                requirements = revision_requirement_documents(
                    lambda root, *args: git(root, *args).stdout.encode('utf-8'), repository, revision)
            resolved = requirement_sources.resolve_requirement_source(source, requirements, allow_archive=False)
            if resolved.status is requirement_sources.ResolutionStatus.RESOLVED:
                contents = resolved.contents
        if contents is None or quote not in contents:
            raise ValueError("공통 교정의 현재 명세 인용을 확인할 수 없습니다.")
        code = raw["code"]
        if not isinstance(code, list) or not 1 <= len(code) <= 8:
            raise ValueError("공통 원인을 확인한 실제 코드 인용이 필요합니다.")
        verified_code = []
        for item in code:
            if not isinstance(item, dict) or set(item) != {"path", "quote"}:
                raise ValueError("공통 원인 코드 근거가 유효하지 않습니다.")
            path, excerpt = _path(item["path"]), _text(item["quote"])
            contents = revision_document_or_none(git, repository, revision, path)
            if contents is None or excerpt not in contents:
                raise ValueError("공통 원인의 실제 코드 인용을 확인할 수 없습니다.")
            verified_code.append(dict(path=path, quote=excerpt))
        link = dict(reports=sorted(ids), plan_id=plan_id, source=source, quote=quote,
                    cause=_text(raw["cause"]), completion=_text(raw["completion"]), code=verified_code,
                    target_path=target["path"], target_revision=(target["target_revision"] if "target_revision" in target
                        else target.get("current_revision_sha") or target.get("plan_sha")))
        from loop_manager_internal.storage.common_defect_state import goal_key
        for result_id in ids:
            identity = (result_id, goal_key(link))
            if identity in seen:
                raise ValueError("같은 보고·교정 목표가 중복됐습니다.")
            seen.add(identity)
        links.append(link)
    return links
