"""Bounded, structured validation feedback for Worker turns."""

from __future__ import annotations

from dataclasses import dataclass
import re


MAX_DIAGNOSTIC = 1800
_ISSUE = re.compile(r"(?mi)^[ \t]*ISSUE_ID:[ \t]*(?P<id>[^\r\n]+?)[ \t]*$")
_VERDICT = re.compile(r"(?mi)^[ \t]*VERDICT:[ \t]*(?:PASS|FAIL)[ \t]*$")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CREDENTIAL = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|telegram[_-]?token|secret|password)"
    r"\b\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|\d{8,10}:[A-Za-z0-9_-]{35})\b")
_PYTEST_FAILURE = re.compile(r"(?i)^FAILED\s+(?P<name>.+?)(?:\s+-\s+(?P<message>.+))?$")
_UNITTEST_FAILURE = re.compile(r"(?i)^(?:FAIL|ERROR):\s+(?P<name>.+)$")
_COMPILER_FAILURE = re.compile(
    r"(?i)^(?P<name>.+?(?::\d+(?::\d+)?)?):\s*(?P<message>(?:fatal )?error:\s+.+)$"
)
_FAILURE_HEADER = re.compile(r"^_{3,}\s*(?P<name>.+?)\s*_{3,}$")
_ERROR_DETAIL = re.compile(
    r"(?i)^(?:E\s+|(?:assertion|error|exception|traceback)\b|.+(?:error|exception|failed)\b)"
)
_SUMMARY_BOUNDARY = re.compile(
    r"(?i)^(?:=+.*\b(?:failed|errors?)\b.*=+|FAILED\s*\(.*|"
    r"(?:\d+\s+)?(?:failures?|errors?)\b.*)$"
)
_NONZERO_FAILURE_COUNT = re.compile(
    r"(?i)(?<!\d)[1-9]\d*\s+(?:failed|failures?|errors?)\b|"
    r"\b(?:failures?|errors?)\s*[:=]\s*[1-9]\d*\b"
)
_SUCCESS_LOG = re.compile(
    r"(?i)^(?:PASS(?:ED)?|SUCCESS|SUCCEEDED)\b|^ok$|"
    r"^=+.*\b\d+ passed\b.*=+$"
)
_TRUNCATED = "[diagnostic truncated]"
_PYTEST_CONTEXT_LINES = 80


@dataclass(frozen=True)
class Feedback:
    stage: str
    identity: str
    diagnostic: str


def redact_secrets(value: str) -> str:
    redacted = _CREDENTIAL.sub(r"\1<REDACTED>", value)
    redacted = _BEARER.sub(r"\1<REDACTED>", redacted)
    return _TOKEN.sub("<REDACTED>", redacted)


def bounded_diagnostic(value: str) -> str:
    """Preserve the useful tail without inserting an unbounded raw log."""
    clean = _ANSI.sub("", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return redact_secrets(clean)[-MAX_DIAGNOSTIC:]


def _bounded_failure_diagnostic(parts: list[str]) -> str:
    """Keep failure excerpts useful without allowing one result to dominate a prompt."""
    rendered = "\n\n".join(part for part in parts if part).strip()
    if len(rendered) <= MAX_DIAGNOSTIC:
        return rendered
    available = MAX_DIAGNOSTIC - len(_TRUNCATED) - 1
    return f"{rendered[:available].rstrip()}\n{_TRUNCATED}"


def _limit_excerpt(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    if limit <= len(_TRUNCATED) + 1:
        return value[:limit], True
    return f"{value[:limit - len(_TRUNCATED) - 1].rstrip()}\n{_TRUNCATED}", True


def _is_failure_summary(line: str) -> bool:
    return bool(_SUMMARY_BOUNDARY.match(line) and _NONZERO_FAILURE_COUNT.search(line))


def _pytest_diagnostic_start(lines: list[str], marker_index: int) -> int:
    """Locate pytest's preceding failure section without interpreting prose."""
    summary = _PYTEST_FAILURE.match(lines[marker_index])
    if not summary:
        return marker_index
    test_name = summary.group("name").split("::")[-1]
    start = max(0, marker_index - _PYTEST_CONTEXT_LINES)
    headers = [(index, match.group("name").strip())
               for index in range(start, marker_index)
               if (match := _FAILURE_HEADER.match(lines[index]))]
    matches = []
    for position, (index, name) in enumerate(headers):
        if name != test_name:
            continue
        next_index = (headers[position + 1][0]
                      if position + 1 < len(headers) else marker_index)
        if any(_ERROR_DETAIL.match(line) for line in lines[index + 1:next_index]):
            matches.append(index)
    return matches[0] if len(matches) == 1 else marker_index


def _failure_excerpt(lines: list[str], marker_index: int, marker: str,
                     diagnostic_start: int) -> str:
    context_end = next((index for index in range(diagnostic_start + 1, marker_index)
                        if _FAILURE_HEADER.match(lines[index])), marker_index)
    preceding = [
        line for line in lines[diagnostic_start + 1:context_end]
        if line.strip() and not _SUCCESS_LOG.match(line)
    ][-8:]
    following = []
    for line in lines[marker_index + 1:marker_index + 9]:
        if _SUCCESS_LOG.match(line) or _PYTEST_FAILURE.match(line) or _UNITTEST_FAILURE.match(line):
            break
        if _SUMMARY_BOUNDARY.match(line):
            break
        if line.strip():
            following.append(line)
    return "\n".join([marker, *preceding, *following])


def _render_failure_excerpts(prefix: str, excerpts: list[str],
                             summaries: list[str]) -> str:
    """Prioritize complete failure names; never spend space on name fragments."""
    summaries = list(dict.fromkeys(summaries[-1:]))
    summary = summaries[0] if summaries else ""
    summary, summary_omitted = _limit_excerpt(summary, MAX_DIAGNOSTIC // 4)
    fixed = [prefix, summary] if summary else [prefix]
    selected: list[str] = []
    omitted = 0
    # Each excerpt begins with a normalized, complete `FAILED: <name>` line.
    # Select only entries whose names fit, reserving space to say what did not.
    for index, excerpt in enumerate(excerpts):
        remaining = len(excerpts) - index - 1
        omission = f"[{remaining} recognizable failures omitted]" if remaining else ""
        candidate = [*fixed, *selected, excerpt.split("\n", 1)[0], omission]
        if summary_omitted or remaining or any("\n" in item for item in excerpts[:index + 1]):
            candidate.append(_TRUNCATED)
        if len("\n\n".join(part for part in candidate if part)) > MAX_DIAGNOSTIC:
            omitted = remaining + 1
            break
        selected.append(excerpt.split("\n", 1)[0])
    else:
        omitted = 0

    omission = f"[{omitted} recognizable failures omitted]" if omitted else ""
    details = [excerpt.split("\n", 1)[1] if "\n" in excerpt else "" for excerpt in excerpts[:len(selected)]]
    marker_needed = summary_omitted or bool(omission) or any(details)
    base = [prefix, *selected, omission, summary]
    base_length = len("\n\n".join(part for part in base if part))
    marker_space = len(_TRUNCATED) + 2 if marker_needed else 0
    available = max(0, MAX_DIAGNOSTIC - base_length - marker_space)
    nonempty = sum(bool(detail) for detail in details)
    each = max(0, (available - nonempty) // nonempty) if nonempty else 0
    detail_parts = [detail[:each].rstrip() for detail in details]
    detail_omitted = any(len(part) < len(detail) for part, detail in zip(detail_parts, details))
    rendered = [prefix]
    for name, detail in zip(selected, detail_parts):
        rendered.append(f"{name}\n{detail}" if detail else name)
    if omission:
        rendered.append(omission)
    if summary:
        rendered.append(summary)
    if summary_omitted or detail_omitted or omission:
        rendered.append(_TRUNCATED)
    return "\n\n".join(rendered)


def _fallback_verify_diagnostic(raw: str, exit_code: int | None) -> str:
    clean = redact_secrets(_ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")).strip()
    prefix = (
        f"verify exit code: {exit_code}" if exit_code is not None
        else "verify exit code: unavailable"
    )
    available = MAX_DIAGNOSTIC - len(prefix) - 1
    truncated = len(clean) > available
    tail = clean[-available:]
    result = f"{prefix}\n{tail}".rstrip()
    if truncated:
        available = MAX_DIAGNOSTIC - len(prefix) - len(_TRUNCATED) - 2
        result = f"{prefix}\n{_TRUNCATED}\n{clean[-available:]}"
    return result


def format_verify_failure(raw: str, *, exit_code: int | None = None,
                          timed_out: bool = False) -> str:
    """Conservatively render recognizable verify failures for Worker feedback.

    This deliberately does not alter the raw output used for audit or retry
    fingerprints.  If no explicit, broadly recognizable failure marker is
    present, the bounded tail remains more honest than a guessed diagnosis.
    """
    clean = redact_secrets(_ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")).strip()
    lines = clean.splitlines()
    markers: list[tuple[int, str, int]] = []
    summaries: list[str] = []
    for index, line in enumerate(lines):
        pytest = _PYTEST_FAILURE.match(line)
        unittest = _UNITTEST_FAILURE.match(line)
        compiler = _COMPILER_FAILURE.match(line)
        if pytest and not _SUMMARY_BOUNDARY.match(line):
            markers.append((index, f"FAILED: {pytest.group('name')}" + (
                f" - {pytest.group('message')}" if pytest.group('message') else ""
            ), _pytest_diagnostic_start(lines, index)))
        elif unittest:
            markers.append((index, f"FAILED: {unittest.group('name')}", index))
        elif compiler:
            markers.append((index, f"FAILED: {compiler.group('name')} - {compiler.group('message')}", index))
        if _is_failure_summary(line):
            summaries.append(line)

    if timed_out:
        timeout_lines = [line for line in lines if "전체 verify 시간 초과" in line]
        if timeout_lines:
            return _bounded_failure_diagnostic([
                f"verify exit code: {exit_code}" if exit_code is not None else "verify exit code: unavailable",
                *timeout_lines,
            ])
    if not markers:
        return _fallback_verify_diagnostic(clean, exit_code)

    excerpts = [
        _failure_excerpt(lines, marker_index, marker, diagnostic_start)
        for marker_index, marker, diagnostic_start in markers
    ]
    prefix = (
        f"verify exit code: {exit_code}" if exit_code is not None
        else "verify exit code: unavailable"
    )
    return _render_failure_excerpts(prefix, excerpts, summaries)


def normalize_feedback(stage: str, raw: str, fingerprints=(), *,
                       exit_code: int | None = None,
                       timed_out: bool = False) -> tuple[Feedback, ...]:
    """Use the existing retry-policy fingerprint, but parse issue prose once."""
    if stage == "verify":
        if len(fingerprints) != 1:
            raise ValueError("verify requires one failure fingerprint")
        return (Feedback(
            stage, f"verify:{fingerprints[0][0]}",
            format_verify_failure(raw, exit_code=exit_code, timed_out=timed_out),
        ),)
    if stage != "review":
        raise ValueError(f"unknown validation stage: {stage}")
    matches = list(_ISSUE.finditer(raw))
    explanations: dict[str, set[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        explanation = _VERDICT.sub("", raw[match.end():end]).strip()
        identifier = match.group("id").strip().casefold()
        explanations.setdefault(identifier, set()).add(explanation)
    # Match retry-policy identity semantics and count each issue once per Review.
    # Preserve distinct explanations deterministically, even if declarations move.
    return tuple(
        Feedback(stage, f"review:{identifier}",
                 bounded_diagnostic("\n\n".join(sorted(explanations[identifier]))))
        for identifier in sorted(explanations)
    )


def render_delta(rows) -> str:
    """Render repository-provided rows; never interpret stored raw output."""
    lines = []
    for row in sorted(rows, key=lambda item: (item["stage"], item["identity"])):
        line = (f"{row['identity']} occurrence={row['occurrence']} "
                f"revision={row['revision']}")
        if row["diagnostic"]:
            line += f"\n{row['diagnostic']}"
        lines.append(line)
    return "\n\n".join(lines)


def is_validation_error(error: str | None, verify_result: str | None,
                        review_result: str | None) -> bool:
    if not error:
        return False
    if error.startswith(("전체 검증 실패:", "전체 verify 실패:",
                         "rebase 후 전체 verify 실패:", "Review 실패:")):
        return True
    return error in {verify_result, review_result}
