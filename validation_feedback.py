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


def normalize_feedback(stage: str, raw: str, fingerprints=()) -> tuple[Feedback, ...]:
    """Use the existing retry-policy fingerprint, but parse issue prose once."""
    if stage == "verify":
        if len(fingerprints) != 1:
            raise ValueError("verify requires one failure fingerprint")
        return (Feedback(stage, f"verify:{fingerprints[0][0]}", bounded_diagnostic(raw)),)
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
