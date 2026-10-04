---
id: G-135
title: Revalidate Git Cleanup and Publication Targets
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

Close the gap between a safe Git assessment and its destructive effect. The current cleanup path can remove a worktree after a new clean local commit appears, and checkpoint push can use a changed `origin` URL after repository identity was checked. These races can lose local work or publish to the wrong remote.

## Acceptance Criteria

- Immediately before managed worktree removal, the current HEAD, PR head relation, index and local changes still satisfy the assessed cleanup proof; otherwise the worktree and pending record are preserved for recheck.
- A checkpoint push is bound to the registered remote identity at use time, with the existing branch lease and ownership checks. A changed or unverifiable URL prevents push and keeps the checkpoint recoverable.
- Normal cleanup and publication still proceed; no extra user Git branch, credential, state format or push authority is introduced. Stored worktrees, PR state and retry history remain intact on interruption.

## Implementation

- Keep cleanup eligibility in `git/cleanup_service.py` and bind the removal effect to a fresh immutable observation. Keep checkpoint target validation beside the push adapter, reusing existing origin identity and lease helpers.
- Do not alter the documented worktree/branch/commit/push authority; make the existing preconditions effective at the mutation boundary.
- The responsibility boundary is Git eligibility proof versus the removal or push effect. Preserve user behavior, public contract and storage format by retaining the current cleanup and checkpoint records while refusing only stale proofs. Current cleanup can delete a newly created local commit after a stale safe assessment; this is a concrete data loss risk.

## Validation

- Worker runs targeted unit tests in `tests/test_worktree_cleanup.py` and checkpoint publication or `tests/test_checkpoint_git_safety.py` cases: commit added after assessment, local-only or unverifiable commit at removal, URL changed after assessment, exact lease, and successful unchanged target.
- Full verify confirms cleanup, checkpoint recovery and source-sync preservation behavior across the existing Git suite.

## Progress

- [x] Recheck cleanup and push proofs at their mutation boundaries.
- [x] Add race and restart regression cases.

## Decision Log

- Manifest findings 10 and 30 identify distinct Git mutations with the same assessment-to-use hazard; finding 90 shows the cleanup test only checks assessment. High priority follows the concrete local-commit loss and wrong-remote publication paths, not file size.
- Observed mutation paths: `loop_manager_internal/git/cleanup_service.py`, `loop_manager_internal/recovery/checkpoint_adapter.py`.
- Cleanup assessment now carries an immutable HEAD, branch and index observation with the PR head OID. Removal repeats the full assessment before Git removes the worktree, including inside the managed journal application. A stale or unverifiable assessment leaves the worktree for `cleanup_pending` recheck.
- Checkpoint publication resolves and validates the registered fetch and push identity immediately before push, then pushes to that explicit URL under the existing exact branch lease. The explicit URL does not update a local remote-tracking ref, so recovery continues to use the durable checkpoint and live remote observation.
