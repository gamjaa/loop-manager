"""Standalone Requirement document rules owned by the specification Skill."""

from __future__ import annotations

import re
import stat
from pathlib import Path


_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_REQUIREMENT_ID = re.compile(r"R-([0-9]{3,})\Z")
_REQUIREMENT_HEADING = re.compile(r"# \[(R-[0-9]{3,})\] (.+)")


class RequirementIdError(ValueError):
    """A Requirement document cannot provide an unambiguous source ID."""


def _valid_requirement_id(value: object) -> str:
    if not isinstance(value, str):
        raise RequirementIdError("Requirement id가 없습니다")
    match = _REQUIREMENT_ID.fullmatch(value)
    if match is None or int(match.group(1)) == 0:
        raise RequirementIdError(f"유효하지 않은 Requirement id입니다: {value}")
    return value


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """Parse fixed key/value and indented-list frontmatter, if present."""

    lines = text.replace("\r\n", "\n").splitlines()
    if not lines:
        return None
    if lines[0] != "---":
        if lines[0].startswith("---"):
            raise RuntimeError("frontmatter 시작 구분자가 유효하지 않습니다.")
        return None
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise RuntimeError("frontmatter 종료 구분자가 없습니다.") from error

    values: dict[str, object] = {}
    current_list: str | None = None
    for line in lines[1:end]:
        if line.startswith("  - "):
            if current_list is None:
                raise RuntimeError(f"유효하지 않은 frontmatter 줄입니다: {line}")
            value = values[current_list]
            assert isinstance(value, list)
            value.append(line[4:].strip())
            continue
        if ":" not in line:
            raise RuntimeError(f"유효하지 않은 frontmatter 줄입니다: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        if not _KEY.fullmatch(key):
            raise RuntimeError(f"유효하지 않은 frontmatter key입니다: {key}")
        if key in values:
            raise RuntimeError(f"중복 frontmatter key입니다: {key}")
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


def requirement_identity(text: str) -> tuple[str, str]:
    """Return a Requirement ID and title using frontmatter or its first heading."""
    text = text.removeprefix("\ufeff")
    try:
        frontmatter = parse_frontmatter(text)
    except RuntimeError as error:
        raise RequirementIdError(str(error)) from error
    if frontmatter is not None:
        requirement_id = _valid_requirement_id(frontmatter.get("id"))
        title = frontmatter.get("title")
        if not isinstance(title, str) or not title.strip():
            raise RequirementIdError("Requirement title이 없습니다")
        return requirement_id, title

    for line in text.splitlines():
        if line.startswith("# "):
            match = _REQUIREMENT_HEADING.fullmatch(line)
            if match is None or not match.group(2).strip():
                raise RequirementIdError("첫 제목은 `# [R-000] 제목` 형식이어야 합니다")
            return _valid_requirement_id(match.group(1)), match.group(2).strip()
    raise RequirementIdError("Requirement 첫 제목을 찾을 수 없습니다")


def markdown_files(directory: Path, *, required: bool) -> tuple[Path, ...]:
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



def requirement_summary(text: str) -> str | None:
    """Read the first level-two Summary outside fenced code blocks."""
    fence = None
    fence_length = 0
    collecting = False
    content: list[str] = []
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if collecting:
                content.append(line)
            if (marker and marker[1][0] == fence
                    and len(marker[1]) >= fence_length and not marker[2].strip()):
                fence = None
            continue
        if marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence, fence_length = marker[1][0], len(marker[1])
        else:
            heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$", line)
            if heading:
                level = len(heading[1])
                title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip()
                if collecting and level <= 2:
                    break
                if level == 2 and title == "Summary":
                    collecting = True
                    continue
        if collecting:
            content.append(line)
    return "\n".join(content).strip() or None
