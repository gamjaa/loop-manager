#!/usr/bin/env python3
"""Print the next local Requirement ID without reserving or changing it."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

_DOCUMENTS = Path(__file__).with_name("requirement_documents.py")
_DOCUMENT_MODULE = ModuleType("_specification_requirement_documents")
_DOCUMENT_MODULE.__file__ = str(_DOCUMENTS)
exec(compile(_DOCUMENTS.read_bytes(), str(_DOCUMENTS), "exec"), _DOCUMENT_MODULE.__dict__)
RequirementIdError = _DOCUMENT_MODULE.RequirementIdError
requirement_identity = _DOCUMENT_MODULE.requirement_identity
parse_frontmatter = _DOCUMENT_MODULE.parse_frontmatter
_markdown_files = _DOCUMENT_MODULE.markdown_files


def next_requirement_id(root: Path) -> str:
    """Return max source ID + 1 from active and archived local files."""
    requirement_root = root / "docs/requirements"
    files = (
        *_markdown_files(requirement_root, required=True),
        *_markdown_files(requirement_root / "archive", required=False),
    )
    numbers: list[int] = []
    for path in files:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise RequirementIdError(f"Requirement 파일이 UTF-8이 아닙니다: {path}") from error
        except OSError as error:
            raise RequirementIdError(f"Requirement 파일을 읽을 수 없습니다: {path} ({error})") from error
        try:
            requirement_id, _ = requirement_identity(text)
        except RequirementIdError as error:
            raise RequirementIdError(f"유효하지 않은 Requirement입니다: {path} ({error})") from error
        numbers.append(int(requirement_id[2:]))
    return f"R-{max(numbers, default=0) + 1:03d}"


def main() -> int:
    if len(sys.argv) != 1:
        print("사용법: python .agents/skills/specification-docs/scripts/next_requirement_id.py", file=sys.stderr)
        return 2
    try:
        next_id = next_requirement_id(Path.cwd())
    except (RequirementIdError, OSError) as error:
        print(f"Requirement ID 조회 오류: {error}", file=sys.stderr)
        return 1
    print(next_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
