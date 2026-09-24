"""Reviewer evidence for a specification decision that needs the user."""

from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class SpecificationDeadlock:
    kind: str
    locations: tuple[tuple[str, str], ...]
    problem: str
    implementation_choice: str
    question: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for path, _ in self.locations))


def parse_evidence(output: str) -> SpecificationDeadlock:
    declarations = [
        line.strip() for line in output.splitlines()
        if line.lstrip().upper().startswith("SPEC_DEADLOCK:")
    ]
    if len(declarations) != 1:
        raise ValueError("SPEC_DEADLOCK 증거가 정확히 하나 필요합니다.")
    try:
        value = json.loads(declarations[0].split(":", 1)[1].strip())
    except (ValueError, TypeError) as error:
        raise ValueError("SPEC_DEADLOCK JSON 형식이 유효하지 않습니다.") from error
    if not isinstance(value, dict) or set(value) != {
        "kind", "locations", "problem", "implementation_choice", "question"
    }:
        raise ValueError("SPEC_DEADLOCK 필수 항목이 누락됐습니다.")
    kind = value["kind"]
    if not isinstance(kind, str) or kind not in {"conflict", "missing"}:
        raise ValueError("SPEC_DEADLOCK kind는 conflict 또는 missing이어야 합니다.")
    locations = value["locations"]
    if not isinstance(locations, list) or len(locations) < (2 if kind == "conflict" else 1):
        raise ValueError("SPEC_DEADLOCK 문서 위치가 부족합니다.")
    parsed = []
    for location in locations:
        if not isinstance(location, dict) or set(location) != {"path", "section"}:
            raise ValueError("SPEC_DEADLOCK 문서 위치 형식이 유효하지 않습니다.")
        path, section = location["path"], location["section"]
        if (
            not isinstance(path, str) or not path or path != path.strip()
            or path.startswith("/") or "\\" in path or ":" in path
            or not path.isprintable()
            or any(not part or part in {".", ".."} or part != part.strip()
                   for part in path.split("/"))
            or not isinstance(section, str) or not section.strip()
            or not section.isprintable()
        ):
            raise ValueError("SPEC_DEADLOCK 문서 경로·절이 유효하지 않습니다.")
        parsed.append((path, section.strip()))
    if len(set(parsed)) != len(parsed):
        raise ValueError("SPEC_DEADLOCK 문서 위치가 중복됐습니다.")
    for field in ("problem", "implementation_choice", "question"):
        item = value[field]
        if not isinstance(item, str) or not item.strip() or not item.isprintable():
            raise ValueError(f"SPEC_DEADLOCK {field}가 필요합니다.")
    return SpecificationDeadlock(
        kind, tuple(parsed), value["problem"].strip(),
        value["implementation_choice"].strip(), value["question"].strip(),
    )
