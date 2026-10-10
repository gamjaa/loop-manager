#!/usr/bin/env python3
"""Print the next local Requirement ID without reserving or changing it."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import ModuleType

_SHARED_FRONTMATTER = Path(__file__).with_name("frontmatter.py")
if not _SHARED_FRONTMATTER.is_file():
    _SHARED_FRONTMATTER = (
        Path(__file__).resolve().parents[4]
        / "loop_manager_internal/catalog/frontmatter.py"
    )
_FRONTMATTER_MODULE = ModuleType("_loop_manager_frontmatter")
_FRONTMATTER_MODULE.__file__ = str(_SHARED_FRONTMATTER)
exec(
    compile(_SHARED_FRONTMATTER.read_bytes(), str(_SHARED_FRONTMATTER), "exec"),
    _FRONTMATTER_MODULE.__dict__,
)
parse_frontmatter = _FRONTMATTER_MODULE.parse_frontmatter
RequirementIdError = _FRONTMATTER_MODULE.RequirementIdError
requirement_identity = _FRONTMATTER_MODULE.requirement_identity


def _markdown_files(directory: Path, *, required: bool) -> tuple[Path, ...]:
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError as error:
        if not required:
            return ()
        raise RequirementIdError(f"활성 Requirement 디렉터리가 없습니다: {directory}") from error
    except OSError as error:
        raise RequirementIdError(f"Requirement 디렉터리를 확인할 수 없습니다: {directory} ({error})") from error
    if not stat.S_ISDIR(mode):
        raise RequirementIdError(f"Requirement 경로가 디렉터리가 아닙니다: {directory}")

    try:
        candidates = sorted(
            (path for path in directory.iterdir() if path.suffix == ".md"),
            key=lambda path: path.name,
        )
    except OSError as error:
        raise RequirementIdError(f"Requirement 디렉터리를 읽을 수 없습니다: {directory} ({error})") from error

    files: list[Path] = []
    for path in candidates:
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise RequirementIdError(f"Requirement 파일을 확인할 수 없습니다: {path} ({error})") from error
        if not stat.S_ISREG(mode):
            raise RequirementIdError(f"Requirement 경로가 일반 파일이 아닙니다: {path}")
        files.append(path)
    return tuple(files)


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
