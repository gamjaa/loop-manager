---
id: G-146
title: Redact Secrets at Feedback Boundaries
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-033] Minimal Worker Session Prompts.md
depends_on: []
complexity: low
risk: high
priority: high
---

## Purpose

Keep credential-bearing verify and Review feedback out of durable diagnostics and Worker recovery prompts. Current redaction matches unquoted `password:` keys but can miss quoted JSON keys; the bootstrap command-line secrecy test has no secret-bearing input, so it can pass without exercising that contract.

## Acceptance Criteria

- Feedback redacts ordinary and JSON-quoted credential keys before persistence, log output or Agent prompt assembly, including repeated or multiline values, without losing non-secret failure context.
- Bootstrap child command arguments remain free of configured secret values, and its test proves this with an actual sentinel secret. Existing configuration storage, prompt/result format, verify feedback cursor and approved Agent scope remain unchanged.
- A redaction failure never exposes the raw diagnostic as a fallback; retry and feedback recovery preserve the existing work and session.

## Implementation

- Reuse the current feedback scrubber at its shared persistence/prompt boundary. Expand its input cases rather than adding another independent sanitizer. Keep bootstrap argument test close to its existing fixture.
- The responsibility boundary is feedback sanitization before diagnostic persistence and prompt assembly. Preserve user behavior, public contract and storage format by retaining feedback fields and cursor semantics while replacing credential values before either sink; raw JSON credentials present a concrete security exposure.

## Validation

- Worker runs targeted unit tests in `tests/test_validation_feedback.py` and `tests/test_bootstrap_entry.py` for quoted/unquoted keys, repeated values, persisted feedback and real secret-bearing child arguments.
- Full verify confirms Worker feedback, bootstrap invocation and existing secret-handling regressions.

## Progress

- [x] Cover JSON-formatted credentials at the shared feedback boundary.
- [x] Strengthen secret-bearing bootstrap and feedback tests.

## Decision Log

- Manifest findings 8 and 47 identify a concrete disclosure path and a false-positive secrecy assertion. High priority follows the possibility of credentials entering a durable log or Worker prompt.
- Observed feedback path: `loop_manager_internal/execution/validation_feedback.py`.
- Reused `redact_secrets` for both normalized diagnostics and durable validation-result output, so quoted credential values are removed before either feedback persistence or Worker prompt delivery.
- Preserved the existing verify fingerprint source and cursor identity while redacting stored samples, state, provider Review logs and last-message files. Reviewer log JSON is decoded before applying the shared scrubber to nested result strings.
- Codex Reviewer now reads its final message from JSON stdout without requesting an `-o` file; resumed calls capture stdout through a real subprocess pipe. Invalid JSON lines normalize escaped quotes before applying the same credential scrubber.
- Native Reviewer keeps the previous last-message file when preflight blocks process creation; it replaces that file only after collecting a sanitized message.
- Codex and Claude Reviewer calls, including resumed calls, drain JSONL in bounded chunks and sanitize before a capped log write. Oversized or undecodable JSON lines are omitted, including unicode-escaped credential keys, while later result and session events remain available.
- Bounded Reviewer reads now stop at each newline so a short session-start event is observed before the provider closes stdout; a real pipe test covers delivery before EOF.
