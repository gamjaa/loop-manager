"""Lossless result formatting at existing role boundaries; never invent judgments."""
from __future__ import annotations

import json
import re


def normalize_markers(output: str, markers: tuple[str, ...]) -> str:
    """Canonicalize only declared line labels, leaving evidence bodies untouched."""
    names = "|".join(re.escape(marker.rstrip(": ")) for marker in markers)
    pattern = re.compile(
        rf"^[ \t]*(?:[-*][ \t]+)?(?:"
        rf"\*\*(?P<bold_name>{names})(?:\*\*[ \t]*[:：=]|[ \t]*[:：=]\*\*)"
        rf"|(?P<plain_name>{names})[ \t]*[:：=])[ \t]*(?P<body>.*)$", re.I,
    )
    lines = []
    for line in output.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = pattern.fullmatch(line)
        lines.append(f"{(match['bold_name'] or match['plain_name']).upper()}: {match['body']}" if match else line)
    return "\n".join(lines)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate result field: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"non-finite result value: {value}")


def tagged_json(output: str, marker: str, *, max_bytes: int | None = None):
    """Decode one tagged JSON value, allowing indentation, fences and line breaks.

    Other role declarations may follow on subsequent lines. Duplicate markers,
    duplicate object keys and same-line trailing values remain errors.
    """
    marker = marker.rstrip(": ")
    normalized = normalize_markers(output, (marker,))
    matches = list(re.finditer(rf"(?m)^{re.escape(marker)}: *", normalized))
    if len(matches) != 1:
        raise ValueError(f"exactly one {marker} result is required")
    candidate = normalized[matches[0].end():].lstrip()
    fence = re.match(r"```(?:json)?[ \t]*\n", candidate)
    if fence:
        candidate = candidate[fence.end():].lstrip()
    value, end = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_reject_constant).raw_decode(candidate)
    if max_bytes is not None and len(candidate[:end].encode("utf-8")) > max_bytes:
        raise ValueError("result exceeds byte limit")
    trailing = candidate[end:]
    if trailing.strip() and re.match(r"[ \t]*[\n\r]", trailing) is None:
        raise ValueError("unexpected text after result")
    if fence and not trailing.lstrip().startswith("```"):
        raise ValueError("unclosed result fence")
    return value


def bind_identity(value: dict, expected: dict) -> dict:
    """Join verified call identity only; explicit mismatches always fail."""
    result = dict(value)
    for key, identity in expected.items():
        supplied = result.get(key, identity)
        if isinstance(supplied, str):
            supplied = supplied.strip()
        if supplied != identity:
            raise ValueError(f"result identity differs: {key}")
        result[key] = identity
    return result
