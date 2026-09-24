---
id: G-069
title: Notified Block Resumption Alert
sources:
  - docs/requirements/[R-034] Notified Block Resumption Alert.md
depends_on:
  - G-064
  - G-068
complexity: high
risk: medium
priority: normal
---

## Purpose

Users who received a BLOCKED notification cannot tell when that same work actually resumes. Connect each visible block incident to the later start of its own plan, project or Planner work.

## Acceptance Criteria

- Record a durable incident identity, prior scope, plan, cause and whether at least one BLOCKED message piece was confirmed delivered. A later separate notified block with the same reason can create a separate resumption incident.
- Enqueue one resumption event only after the notified block clears and the corresponding normal work starts: that ExecPlan's Worker or PR handling for `PLAN_BLOCKED`, project work after the safe gate for `PROJECT_BLOCKED`, or Planner work after `planning_error`. Clearing a record or running an unrelated READY plan does not count.
- Include project, previous scope, related ExecPlan, previously notified cause and the actual resumed operation. Do not claim automatic recovery when user action may have resolved it.
- Repeat runs, restart and send retry preserve one event per incident. No event is created for transient errors, undelivered block notices or disabled Telegram. A partly delivered block counts as notified; its stale remaining pieces are not sent after release.
- Alert delivery remains on the shared outbox and cannot change work selection or execution state. Runtime logs and result still show resumption when Telegram is off or fails. Existing PR, BLOCKED, failure, no-runnable and completion events remain idempotent.

## Implementation

- Add compatible incident/resumption state in `notifications.py` tied to the G-068 piece cursor and current `scoped_blocking.py` / `planning_error` identity. Capture the original cause at notification time, not from a later mutable error field.
- Emit only at actual start boundaries in `planning_runtime.py`, `loop_manager.py` and the execution/PR reconciliation path. Commit incident transition and outbox enqueue together; keep dispatch outside execution-state transactions.
- Ensure stale block-piece cancellation precedes resumed-work delivery and distinguish a new block incident after resumption from an old event with the same reason code. Update `docs/PRODUCT.md` for the lasting alert rule in the implementation PR.

## Validation

- Worker runs focused cases in `tests/test_notifications.py`, `tests/test_scoped_blocking.py` and `tests/test_planning_runtime.py` for changed boundaries.
- Cover guidance approval, closed PR reopen, failed recovery release, `planning_error`, project safe-gate release, delayed actual start, unrelated READY work, repeated same-cause block, restart, partial BLOCKED delivery, disabled Telegram and send failure.
- Full verify must retain existing block, PR, no-runnable and completion outbox behavior.

## Progress

- [ ] Durable notified incident and resumption identity
- [ ] Actual-work-start enqueue integration
- [ ] Targeted timing, idempotency and partial-delivery tests

## Decision Log

- Resumption describes observed work starting; the block's release alone is insufficient.
