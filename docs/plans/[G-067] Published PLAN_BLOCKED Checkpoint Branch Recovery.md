---
id: G-067
title: Published PLAN_BLOCKED Checkpoint Branch Recovery
sources:
  - docs/requirements/[R-039] PLAN_BLOCKED Remote Branch Change Recovery.md
depends_on:
  - G-052
complexity: high
risk: high
priority: critical
---

## Purpose

`resume_plan_blocks` currently rejects every remote OID change after a published checkpoint. A user can push a corrective commit to the notified branch without changing `origin/<base-branch>`, but the plan never inspects that correction. Recover only a proven successor of the owned checkpoint and reevaluate the actual block cause.

## Acceptance Criteria

- For a PR-less `PLAN_BLOCKED` plan with a published checkpoint, fetch and identify its exact stored remote branch. Accept only a new head that descends from the last accepted remote head and contains the published checkpoint. Base-branch equality does not suppress this check.
- Before adopting the remote commit, prove plan ID, branch, worktree and repository ownership; validate local HEAD, index, tracked and non-ignored untracked content, ignored-file overlap and Git operation state. Preserve local unpublished work. A deleted, rewritten, diverged, foreign or unverifiable branch remains blocked with its specific cause; never reset, stash, overwrite or force-push it.
- Reevaluate the stored reason against the resulting candidate, including worktree code and tests where they can resolve the reason. A new OID alone does not release the block. Keep plan declaration, sources, allowed change scope and guidance decisions authoritative.
- If resolved, resume the same ExecPlan, branch, worktree and eligible session. Treat user commits as new unverified input: required Worker assessment, changed-code targeted tests, full verify and Review run before publication. Closed PR blocks retain the reopen rule; unpublished and legacy unproven checkpoints cannot be adopted on remote movement alone.
- If the cause persists, store the inspected remote head and reason so unchanged input does not repeat work or notifications. A later remote commit or other relevant input triggers a new assessment. Independent READY selection and other plan blocks continue.
- Preserve existing checkpoint, scope and notification behavior outside this PR-less published case. Any change within the Architecture Change Policy must pass its user approval gate before implementation.

## Implementation

- Extend `blocked_checkpoint_state.py` with durable accepted/inspected remote identity and cause-evaluation evidence using compatible additive storage. Keep the publication checkpoint OID separately identifiable from a later observed head.
- Introduce a narrow remote-checkpoint reconciliation boundary near `resume_plan_blocks` in `loop_manager.py`: fetch the exact ref, compare ancestry and ownership, inspect worktree safety, then fast-forward only when local state can be preserved. Reuse `blocked_checkpoint.py`, `scoped_blocking.py` and existing scope validation rather than accepting remote content as Agent output.
- Route the reconciled candidate through the reason-specific release rules and revision invalidation in `execution_handlers.py` and `execution_revision.py`; reset only stale validation evidence, never user changes. Persist pending decisions before mutation so interruption resumes safely.
- Report stable unsafe and unresolved reasons without re-enqueueing an identical BLOCKED event. Document the newly allowed remote branch behavior in `docs/PRODUCT.md` in the implementation PR.

## Validation

- Worker runs only the relevant cases in `tests/test_blocked_checkpoint_state.py`, `tests/test_blocked_checkpoint.py`, `tests/test_scoped_blocking.py` and `tests/test_execution_handlers.py` for the changed boundary.
- Add focused tests for base-unchanged remote successors, code/test-only correction, cause still present, next new commit, crash after fetch or local adoption, changed revision verification, and independent READY progress.
- Reject deletion, rewrite, divergence, wrong branch/plan, unpublished or legacy checkpoint, unsafe local changes, ignored overlap and closed PR; prove no reset, stash or force-push. Full verify must retain existing scope and publication recovery coverage.

## Progress

- [ ] Durable remote observation and safe adoption boundary
- [ ] Cause-specific release and validation invalidation
- [ ] Targeted recovery and refusal tests

## Decision Log

- Remote branch progress is evidence to inspect, not evidence that the original block was resolved.

- Review follow-up: diagnostics need not contain the corrected implementation path. For exhausted verify/review blocks, changes to files in the frozen published checkpoint diff against the execution base also trigger Worker reassessment. The accepted remote head is not used as that baseline, so unrelated adopted commits cannot expand this evidence. This is unverified input, not a successful validation result; Worker, full verify and Review remain required.
- Targeted validation: five individual unit cases passed, covering diagnostic path matching, pathless/checkpoint classification, test-only traceback with implementation-only correction, unrelated README/code commits, and review diagnostic matching. Full verify is left to Loop Manager. R-039 was already archived when this Worker session resumed; its content was not changed.

- Second review follow-up supersedes the checkpoint-file restriction above: diagnostic paths and prior changed-file lists cannot establish implementation dependencies. Any safely adopted execution input change now requests Worker reassessment for exhausted verify/review; clearly documentary changes still need a diagnostic connection. The unnecessary checkpoint diff lookup is removed. Reassessment clears stale validation and does not certify resolution or bypass publication checks.
- Regression coverage now includes a pre-existing dependency absent from the checkpoint diff, test-only diagnostics, pathless diagnostics, configuration/build inputs, and ignored overlap recovery. R-039 remains at its existing archive path without content changes.
