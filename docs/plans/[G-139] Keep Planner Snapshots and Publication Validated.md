---
id: G-139
title: Keep Planner Snapshots and Publication Validated
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: medium
risk: high
priority: normal
---

## Purpose

Define one validated boundary for saved Planner snapshot fields, deterministic workspace path and publication intent. Current snapshot iteration accepts strings or objects as arrays, duplicated path construction can diverge, and manifest hash validation after commit can leave an invalid intent in SQLite.

## Acceptance Criteria

- Sources and dependencies loaded from a stored Planner snapshot have the expected collection and element types before selection or catalog comparison; malformed values are rejected without making work eligible.
- Planner selection and workspace ownership use the same deterministic path calculation. Valid existing paths and reservations are unchanged.
- Invalid publication manifest/hash input never commits a new intent. A valid intent survives restart and publication recovery with its original identity, session and content. Existing SQLite schema, catalog format and public Planner result contract remain unchanged.

## Implementation

- Reuse existing ExecPlan parser and workspace naming helpers at snapshot intake. Move intent validation inside the existing transaction before insert or commit; keep owner and revision checks at publication.
- The responsibility boundary is stored snapshot parsing and workspace identity versus the Planner publication transition. Preserve user behavior, public contract and storage format by retaining valid snapshot and intent representations and rejecting invalid fields before eligibility or commit.

## Validation

- Worker runs targeted unit tests in `tests/test_planning_assessment.py`, `tests/test_planning_selection.py`, `tests/test_planning_publication.py`, `tests/test_pinned_plan_registration.py` and `tests/test_planner_interruption_recovery.py` for malformed arrays, path agreement, exact pinned revision, invalid hash rollback and checkpoint-based restart.
- Full verify confirms Planner selection, pinned revision lookup, interruption recovery and preserved publication outcomes.

## Progress

- [ ] Validate snapshot shape, path derivation and intent before commit.
- [ ] Add malformed and restart regression cases.

## Decision Log

- Manifest findings 25, 26 and 39 are coupled by saved Planner identity. Findings 63 and 72 identify weak pinned-revision and interruption test boundaries relevant to this plan.
- Observed state paths: `loop_manager_internal/planning/planning_catalog.py`, `loop_manager_internal/planning/planning_workspace.py`, `loop_manager_internal/storage/planning_publication_state.py`.
