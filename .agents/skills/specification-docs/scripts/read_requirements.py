#!/usr/bin/env python3
"""Print local Requirement existence and active document summaries as JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

_DOCUMENTS = Path(__file__).with_name("requirement_documents.py")
_DOCUMENT_MODULE = ModuleType("_specification_requirement_documents")
_DOCUMENT_MODULE.__file__ = str(_DOCUMENTS)
exec(compile(_DOCUMENTS.read_bytes(), str(_DOCUMENTS), "exec"), _DOCUMENT_MODULE.__dict__)
RequirementIdError = _DOCUMENT_MODULE.RequirementIdError
requirement_identity = _DOCUMENT_MODULE.requirement_identity


def read_requirements(root: Path) -> dict:
    directory = root / "docs/requirements"
    active = _DOCUMENT_MODULE.markdown_files(directory, required=False)
    archived = _DOCUMENT_MODULE.markdown_files(directory / "archive", required=False)
    requirements = []
    for path in active:
        try:
            text = path.read_bytes().decode("utf-8-sig")
            _, title = requirement_identity(text)
        except (OSError, UnicodeError, RequirementIdError) as error:
            raise RequirementIdError(f"Requirement를 읽을 수 없습니다: {path} ({error})") from error
        item = {"title": title, "path": path.relative_to(root).as_posix()}
        summary = _DOCUMENT_MODULE.requirement_summary(text)
        if summary is not None:
            item["summary"] = summary
        requirements.append(item)
    return {"has_active": bool(active), "has_archive": bool(archived), "requirements": requirements}


def main() -> int:
    if len(sys.argv) != 1:
        print("사용법: python .agents/skills/specification-docs/scripts/read_requirements.py", file=sys.stderr)
        return 2
    try:
        result = read_requirements(Path.cwd())
    except (OSError, RequirementIdError) as error:
        print(f"Requirement 조회 오류: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
