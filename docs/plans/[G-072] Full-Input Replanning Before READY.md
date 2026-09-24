---
id: G-072
title: Full-Input Replanning Before READY
sources:
  - docs/requirements/[R-037] Replan Before READY on Input Changes.md
depends_on:
  - G-062
  - G-064
complexity: high
risk: medium
priority: high
---

## Purpose

`PlanningLifecycleCoordinator` currently gives runnable READY work priority when only source, tests or configuration changed, and merge cleanup can clear the successful input marker. Compare the full actual planning input before selecting READY, without replanning for plan archive or a merge transition alone.

## Acceptance Criteria

- At each safe selection boundary, run one general Planner before runnable READY when `AGENTS.md`, PRODUCT, ARCHITECTURE, an active Requirement or included source/test/config content differs from the last successful full-input baseline. The Planner can revise/cancel only mutable READY or add plans; a no-change result advances that baseline and then permits valid READY selection.
- An ExecPlan state transition to `MERGED`, a plan file create/edit/archive or byte-identical Requirement archive does not itself invalidate success. A real included input change in the same cycle still wins; simultaneous specification and source changes invoke the general Planner once.
- First planning with no successful baseline remains possible. A failed or unpublished Planner never advances the baseline or allows old READY to run against changed input. Active Worker/Planner and maintenance cycle ordering retain their existing gates.
- With unchanged input and all plans merged, avoid duplicate Planner invocation; unresolved active Requirement or block is not reported as project completion. The G-064 no-runnable reason remains observable.
- Preserve immutable or ever-started plan boundaries, catalog validation and snapshot CAS on replan. Update lasting planning priority in `docs/PRODUCT.md` and explicitly allow the matching shared `docs/PLANS.md` rule update in the implementation PR.

## Implementation

- Change `choose_planning_decision` / `PlanningLifecycleCoordinator` in `planning_runtime.py` to compare the full input before READY activation and recheck the full input if remote SHA advances at activation. Keep due maintenance and already active execution precedence.
- In `loop_manager.py`, remove merge-only clearing of `planning_fingerprint` and any merge short-circuit that masks a simultaneous real input change. Preserve `processed_sha` and successful specification/full-input markers until actual included content changes; use existing path and archive filters.
- Keep no-change planning publication, recovery and success-marker transaction boundaries intact. Align PRODUCT and the shared PLANS priority rule as part of the implementation PR.

## Validation

- Worker runs focused selection and fingerprint cases in `tests/test_planning_runtime.py`, `tests/test_incremental_planning.py` and `tests/test_loop_manager.py`.
- Cover source-only change with runnable READY, merge plus source change, simultaneous specification/source change, no-change Planner result, merge/archive-only no-op, byte-identical Requirement archive, failed/unpublished Planner, first run, active work and maintenance precedence.
- Full verify must retain mutable READY CAS, priority/dependency selection, no-runnable reporting and requirement archive behavior.

## Progress

- [ ] Full-input-first selection and activation race check
- [ ] Merge/archive-only baseline preservation
- [ ] Targeted ordering, failure and completion tests

## Decision Log

- Input content, rather than the fact of a Merge, determines whether general planning is due.
