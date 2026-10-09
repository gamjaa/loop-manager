#!/usr/bin/env python3
"""Print the next local Requirement ID without reserving or changing it."""

from __future__ import annotations

import importlib.util
import re
import stat
import sys
from pathlib import Path

_SHARED_FRONTMATTER = Path(__file__).with_name("frontmatter.py")
if not _SHARED_FRONTMATTER.is_file():
    _SHARED_FRONTMATTER = (
        Path(__file__).resolve().parents[4]
        / "loop_manager_internal/catalog/frontmatter.py"
    )
_FRONTMATTER_SPEC = importlib.util.spec_from_file_location(
    "_loop_manager_frontmatter", _SHARED_FRONTMATTER
)
if _FRONTMATTER_SPEC is None or _FRONTMATTER_SPEC.loader is None:
    raise ImportError(f"공유 frontmatter parser를 읽을 수 없습니다: {_SHARED_FRONTMATTER}")
_FRONTMATTER_MODULE = importlib.util.module_from_spec(_FRONTMATTER_SPEC)
_FRONTMATTER_SPEC.loader.exec_module(_FRONTMATTER_MODULE)
parse_frontmatter = _FRONTMATTER_MODULE.parse_frontmatter


_ID = re.compile(r"R-([0-9]{3,})\Z")
_HEADING = re.compile(r"# \[(R-[0-9]{3,})\] (.+)")


class RequirementIdError(ValueError):
    """A Requirement document tree cannot provide an unambiguous ID set."""


def _valid_id(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise RequirementIdError("Requirement id가 없습니다")
    match = _ID.fullmatch(value)
    if match is None or int(match.group(1)) == 0:
        raise RequirementIdError(f"유효하지 않은 Requirement id입니다: {value}")
    return value, int(match.group(1))


def requirement_identity(text: str) -> tuple[str, str]:
    """Return the source ID and title from one Requirement document.

    Kept importable so Manager-side checks can share this identity rule.
    """
    text = text.removeprefix("\ufeff")
    try:
        frontmatter = parse_frontmatter(text)
    except RuntimeError as error:
        raise RequirementIdError(str(error)) from error
    if frontmatter is not None:
        requirement_id, _ = _valid_id(frontmatter.get("id"))
        title = frontmatter.get("title")
        if not isinstance(title, str) or not title.strip():
            raise RequirementIdError("Requirement title이 없습니다")
        return requirement_id, title

    for line in text.splitlines():
        if line.startswith("# "):
            match = _HEADING.fullmatch(line)
            if match is None or not match.group(2).strip():
                raise RequirementIdError("첫 제목은 `# [R-000] 제목` 형식이어야 합니다")
            requirement_id, _ = _valid_id(match.group(1))
            return requirement_id, match.group(2).strip()
    raise RequirementIdError("Requirement 첫 제목을 찾을 수 없습니다")


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
