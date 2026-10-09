"""Parser for the deliberately small frontmatter subset used by project docs."""

from __future__ import annotations

import re


_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


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
