#!/usr/bin/env python3
"""Print the next local Requirement ID without reserving or changing it."""

from __future__ import annotations

import re
import stat
import sys
from pathlib import Path


_ID = re.compile(r"R-([0-9]{3,})\Z")
_HEADING = re.compile(r"# \[(R-[0-9]{3,})\] (.+)")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


class RequirementIdError(ValueError):
    """A Requirement document tree cannot provide an unambiguous ID set."""


def _parse_frontmatter(text: str) -> dict[str, str | list[str]] | None:
    lines = text.splitlines()
    if not lines:
        return None
    if lines[0] != "---":
        if lines[0].startswith("---"):
            raise RequirementIdError("frontmatter 시작 구분자가 유효하지 않습니다")
        return None
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise RequirementIdError("frontmatter 종료 구분자가 없습니다") from error

    values: dict[str, str | list[str]] = {}
    current_list: str | None = None
    for line in lines[1:end]:
        if line.startswith("  - "):
            if current_list is None:
                raise RequirementIdError(f"유효하지 않은 frontmatter 줄입니다: {line}")
            value = values[current_list]
            assert isinstance(value, list)
            value.append(line[4:].strip())
            continue
        if ":" not in line:
            raise RequirementIdError(f"유효하지 않은 frontmatter 줄입니다: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        if not _KEY.fullmatch(key):
            raise RequirementIdError(f"유효하지 않은 frontmatter key입니다: {key}")
        if key in values:
            raise RequirementIdError(f"중복 frontmatter key입니다: {key}")
        if value == "":
            values[key] = []
            current_list = key
        elif value == "[]":
            values[key] = []
            current_list = None
        else:
            values[key] = value
            current_list = None
    return values


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
    frontmatter = _parse_frontmatter(text)
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
