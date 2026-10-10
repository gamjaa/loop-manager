"""Parser for the deliberately small frontmatter subset used by project docs."""

from __future__ import annotations

import re


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
