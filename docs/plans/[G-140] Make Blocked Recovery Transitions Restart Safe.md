---
id: G-140
title: Make Blocked Recovery Transitions Restart Safe
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

Consolidate blocked recovery decisions and their final state transition. The review found interruption windows in dirty baseline restoration and project-gate release, provisional PR failures recorded as final refusal, unchecked diagnostic intent ownership, and generation unchanged when a rebase cause clears. These can strand recoverable work or apply a stale unblock.

## Acceptance Criteria

- Dirty baseline restoration and project block release resume from persisted stage and verified tree/index, cause identity, owner and generation after interruption; user changes and unresolved blocks still stop recovery.
- A failed PR/remote observation affects only attempted requests and remains pending where evidence is transient. Unattempted requests keep their prior state. Diagnostic result writes require matching repo, plan, intent and updated row.
- Plan/project block transitions share the final scope and audit decision while preserving their distinct evidence intake. Clearing a rebase cause advances its CAS generation. Existing SQLite format, stored session/worktree, notification intent and user-facing block categories are preserved.

## Implementation

- Keep recovery orchestration in `recovery/` and intent/CAS writes in `storage/`. Combine only the final common block transition; retain per-cause proof and a transaction that covers gate release plus candidate revalidation or an explicit persisted resume stage.
- Preserve PR, Git and user approval authority. Do not treat missing proof as automatic unblock.
- The responsibility boundary is cause-specific proof versus the shared durable unblock transition. Preserve user behavior, public contract and storage format by retaining current block identities and stages while making an interrupted transition replayable without repeated manual recovery.

## Validation

- Worker runs targeted unit tests in `tests/test_blocked_checkpoint_state.py`, `tests/test_plan_block_revalidation.py`, `tests/test_scope_project_recovery.py`, `tests/test_checkpoint_remote_recovery.py`, `tests/test_checkpoint_adoption.py`, `tests/test_pr_conflict_corrections.py` and persisted-scope recovery cases for each interruption window, transient PR failure, wrong intent, stale generation and unrelated plan preservation.
- Full verify confirms blocked and independent project recovery, checkpoint publication and no duplicate notifications after restart.

## Progress

- [ ] Make recovery stage and CAS transitions explicit and restartable.
- [ ] Add interrupted, stale-owner and transient-observation regressions.

## Decision Log

- Manifest findings 27–29, 31–33, 38 and 40 identify related recovery transition defects. Findings 48, 53, 64, 80 and 81 show missing tests for adoption, notification rollback and resumed recovery. High priority follows the documented repeated manual recovery risk after gate release interruption.
- Observed recovery paths: `loop_manager_internal/recovery/blocked_agent_service.py`, `loop_manager_internal/recovery/checkpoint_service.py`, `loop_manager_internal/recovery/blocked_plan_reconciliation.py`, `loop_manager_internal/recovery/project_block_service.py`, `loop_manager_internal/recovery/project_blocked_recovery.py`, `loop_manager_internal/recovery/block_service.py`, `loop_manager_internal/storage/blocked_checkpoint_state.py`, `loop_manager_internal/storage/recovery_state.py`, `tests/test_scope_ignored_artifacts.py`.
