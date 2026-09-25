---
id: G-070
title: Rebase Before Initial Full Verify
sources:
  - docs/requirements/[R-036] Rebase Before Full Verify.md
depends_on:
  - G-054
complexity: high
risk: high
priority: normal
---

## Purpose

`ExecutionHandlers.verify` currently runs full verify before fetching and rebasing the latest base, then may run it again. Verify the final candidate only after its required rebase and conflict correction.

## Acceptance Criteria

- After a valid Worker result and scope check, fetch the latest `origin/<base-branch>`, checkpoint any owned edits, commit as needed and complete rebase before the first full verify. Neither verify nor Reviewer starts with an unresolved rebase, unsafe worktree or failed fetch.
- Rebase conflicts use the existing Worker session and workspace; corrected code and tests receive relevant targeted tests before full verify. Preserve the current bounded correction and safety gates.
- With stable base and no later changes, run full verify once for that Worker result. Verify failure still returns focused feedback to the same Worker. A later Worker change, base advance, rebase or candidate revision change invalidates prior verify and Review evidence and causes required validation on the final revision.
- Keep the publication-time latest-base check, archive reconciliation, failure fingerprint and final PR safety gate. No unverified candidate can be published; the revision and base proven by verify remain durable across interruption.

## Implementation

- Move the latest-base fetch, owned commit and `_rebase_latest` sequence ahead of `_verify_once` in `execution_handlers.py`, preserving `execution_checkpoint_state.py` intent and rebase-correction recovery. Remove the unconditional pre-rebase verify path in `_latest_after_verify`.
- Bind verify evidence to the actual candidate HEAD and base, not merely the plan/source definition token. Invalidate it on any later candidate change while preserving raw audit and failure-history behavior. Align resume paths in `execution_revision.py` and `execution_runtime.py`.
- Update the lasting execution order in `docs/PRODUCT.md` in the implementation PR.

## Validation

- Worker runs focused phase-transition and interruption cases in `tests/test_execution_handlers.py`, `tests/test_execution_revision.py` and `tests/test_rebase_correction.py`.
- Assert call order for stable base, changed base, conflict and conflict correction; count one verify for stable input and new verify after any later revision change. Test targeted-test evidence after conflict correction, verify failure, restart and final publication race.
- Full verify must retain archive, feedback, scope, PR conflict and no-unverified-publication regressions.

## Progress

- [ ] Rebase-first phase transition and durable candidate revision
- [ ] Conflict correction and invalidation integration
- [ ] Targeted call-order and restart tests

## Decision Log

- A single verify is the stable-input outcome, not a guarantee when the candidate changes.
