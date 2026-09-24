---
id: G-068
title: Durable Telegram Message Segmentation
sources:
  - docs/requirements/[R-038] Telegram Message Length Segmentation.md
depends_on:
  - G-061
complexity: high
risk: medium
priority: high
---

## Purpose

`notifications._deliver_rows` sends each logical outbox event with one `sendMessage` call. Long BLOCKED content can exceed Telegram's limit and leave the entire event pending. Deliver the exact text in durable ordered pieces through the same path used by immediate PR and normal pending dispatch.

## Acceptance Criteria

- Send text of at most 4096 characters unchanged in one request. Split longer plain text into nonempty pieces of at most 4096 characters, as full as possible, whose concatenation exactly reproduces the original Unicode text, including Korean, emoji and line breaks.
- Claim, send and record each piece in order. Only mark the logical event sent after the final successful piece. On failure, stop before later pieces; on the next run, resume from the first unconfirmed piece without resending confirmed pieces. Preserve the existing uncertainty rule for a process death between Telegram success and durable acknowledgement.
- Use the same delivery logic for `dispatch_pending`, immediate `dispatch_new_pr_notifications` and all current or later event types. Reobserving an event does not create duplicate logical events or pieces.
- Cancel an unsent or partially sent BLOCKED event when its underlying block has cleared. Retain the confirmed-piece count so a later actual-resumption alert can recognize that the user saw the block. Do not cancel unrelated pending events.
- Disabled Telegram and delivery failure never change Planner, Worker, PR or completion state. Errors and logs contain no token, chat ID or notification body. Existing single-message outbox rows remain compatible.

## Implementation

- Add compatible, per-event delivery progress to `notifications.py` outbox storage. Store the rendered message or a stable content identity before its first piece so later payload changes cannot shift the cursor. Make claim, acknowledgement, retry and cancellation transitions transactionally durable.
- Centralize segmentation in the transport path, leaving `_message` as the logical event renderer. Preserve `attempts`, `delivery_started_at`, `sent_at` and event-key behavior for old rows while tracking the next confirmed piece.
- Reconcile BLOCKED event liveness against the current scoped block or `planning_error` before sending each pending piece. Expose confirmed partial delivery to the later resumption-alert plan without treating it as a completed logical event.

## Validation

- Worker runs focused cases in `tests/test_notifications.py` and any directly changed notification-state tests.
- Test 4096/4097 boundaries, Unicode and newline round-trip, multiple pieces, failure on a later piece, restart and uncertain in-flight send, immediate PR dispatch, stale BLOCKED cancellation and secret-free errors.
- Full verify must retain existing outbox idempotency, PR-before-next-step delivery and disabled-Telegram behavior.

## Progress

- [ ] Exact plain-text segmentation and durable piece cursor
- [ ] Shared dispatch and stale BLOCKED cancellation
- [ ] Targeted boundary, retry and restart tests

## Decision Log

- A logical event keeps one event key; piece progress is subordinate to it.
