"""Unconfirmed common-defect observations; never a disposition or approval."""
from __future__ import annotations

import re

from loop_manager_internal.application.result_normalization import normalize_markers, tagged_json
from loop_manager_internal.execution.validation_feedback import redact_secrets


MARKER = "COMMON_DEFECT_REPORT"


def parse_report(message: str) -> dict[str, object] | None:
    """Return one bounded observation, rejecting ambiguous or unsafe fields.

    Locations are reported repository paths, not proof that a file or cause was
    verified. The caller owns result identity and all subsequent interpretation.
    """
    normalized = normalize_markers(message, (MARKER,))
    if not re.search(rf"(?m)^{MARKER}:", normalized):
        return None
    try:
        value = tagged_json(normalized, MARKER, max_bytes=12000)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("invalid common defect report JSON") from error
    if not isinstance(value, dict) or set(value) != {"problem", "scope", "evidence", "locations"}:
        raise ValueError("invalid common defect report fields")

    def text(item):
        if (not isinstance(item, str) or not item.strip() or len(item) > 2000
                or any(ord(char) < 32 and char not in "\n\t" for char in item)):
            raise ValueError("invalid common defect report text")
        return redact_secrets(item.strip())

    locations = value["locations"]
    if not isinstance(locations, list) or not 1 <= len(locations) <= 8:
        raise ValueError("invalid common defect report locations")
    cleaned = []
    for location in locations:
        if not isinstance(location, dict) or set(location) != {"path", "line"}:
            raise ValueError("invalid common defect report location")
        path = text(location["path"])
        if (any(part in {"", ".", ".."} for part in path.split("/"))
                or any(char in path for char in "\\:\n\t")):
            raise ValueError("invalid common defect report path")
        line = location["line"]
        if type(line) is not int or not 1 <= line <= 2147483647:
            raise ValueError("invalid common defect report line")
        cleaned.append({"path": path, "line": line})
    return {**{key: text(value[key]) for key in ("problem", "scope", "evidence")},
            "locations": cleaned}
