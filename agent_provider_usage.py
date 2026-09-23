"""Tolerant Codex and Claude CLI usage observation parsers."""

from __future__ import annotations

import json
from typing import Mapping

from agent_invocation import Usage


def _counter(value: object) -> tuple[int | None, bool]:
    if value is None:
        return None, False
    if type(value) is int and value >= 0:
        return value, False
    return None, True


def _usage(fields: Mapping[str, object], names: tuple[str, ...]) -> Usage:
    values = [_counter(fields.get(name)) for name in names]
    invalid = any(bad for _, bad in values)
    present = any(value is not None for value, _ in values)
    return Usage(*(value for value, _ in values),
                 status="invalid" if invalid else ("known" if present else "unknown"))


def codex_usage(event: Mapping[str, object]) -> Usage:
    """Use final turn usage only; totals in earlier events can overlap."""
    if event.get("type") != "turn.completed":
        return Usage()
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return Usage(status="invalid" if usage is not None else "unknown")
    result = _usage(usage, ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                            "output_tokens", "reasoning_output_tokens"))
    cached = result.cached_input_tokens
    reasoning = result.reasoning_output_tokens
    invalid = result.status == "invalid"
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, Mapping) and cached is None:
        cached, bad = _counter(input_details.get("cached_tokens"))
        invalid |= bad
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, Mapping) and reasoning is None:
        reasoning, bad = _counter(output_details.get("reasoning_tokens"))
        invalid |= bad
    if (cached is not None and result.input_tokens is not None
            and cached > result.input_tokens):
        cached = None
        invalid = True
    if (reasoning is not None and result.output_tokens is not None
            and reasoning > result.output_tokens):
        reasoning = None
        invalid = True
    return Usage(result.input_tokens, cached, result.cache_write_input_tokens,
                 result.output_tokens, reasoning,
                 "invalid" if invalid else result.status)


def claude_usage(event: Mapping[str, object]) -> Usage:
    if event.get("type") != "result":
        return Usage()
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return Usage(status="invalid" if usage is not None else "unknown")
    # Claude's input_tokens excludes cache reads and writes. Preserve its
    # provider meaning and do not invent a combined input total.
    result = _usage(usage, ("input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens", "output_tokens",
                            "reasoning_output_tokens"))
    reasoning = result.reasoning_output_tokens
    invalid = result.status == "invalid"
    details = usage.get("output_tokens_details")
    if isinstance(details, Mapping) and reasoning is None:
        reasoning, bad = _counter(details.get("thinking_tokens"))
        invalid |= bad
    if (reasoning is not None and result.output_tokens is not None
            and reasoning > result.output_tokens):
        reasoning = None
        invalid = True
    return Usage(result.input_tokens, result.cached_input_tokens,
                 result.cache_write_input_tokens, result.output_tokens, reasoning,
                 "invalid" if invalid else result.status)


def codex_log_usage(raw: str) -> Usage:
    latest = Usage()
    malformed = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            malformed = True
            continue
        if isinstance(event, Mapping) and event.get("type") == "turn.completed":
            latest = codex_usage(event)
    if latest.status == "unknown" and malformed:
        return Usage(status="invalid")
    return latest
