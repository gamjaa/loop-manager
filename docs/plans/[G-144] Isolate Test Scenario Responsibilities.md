---
id: G-144
title: Isolate Test Scenario Responsibilities
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: medium
risk: low
priority: normal
---

## Purpose

Reduce coupling among test scenario owners. Current catalog refresh tests combine catalog CAS with several independent consumers; recovery fixtures borrow sibling methods, mix preparation and assertion switches, and unscoped fixture updates can change every project in a test database. Some phase tests inherit unused scenario mixins.

## Acceptance Criteria

- Catalog cache/CAS tests and planning, PR, archive and project consumer tests have clear owners while keeping shared setup and event-order assertions.
- Recovery fixtures use shared preparation boundaries rather than invoking sibling fixture methods; updates target the intended repository and plan. Interruption, checkpoint, session, failure-audit and real Git assertions remain effective.
- Direct `setUp` recursion and unused mixin inheritance are removed without weakening the product scenarios. Shared ExecPlan header rendering keeps current defaults and variant forms; guidance Git setup reuses the existing subprocess helper. Production behavior and stored formats remain unchanged.

## Implementation

- Reuse current `tests/support` helpers and split only the mixed scenario setup, fault injection and result assertion responsibilities. Keep test data readable and scoped; avoid a new generic test harness.
- The responsibility boundary is shared fixture preparation versus consumer-specific fault injection and assertions. Preserve user behavior, public contract and storage format by changing test organization only and keeping current production scenarios and seeded rows.

## Validation

- Worker runs targeted unit tests in `tests/test_project_catalog_refresh.py`, `tests/test_execution_immediate_rebase_publication_phase.py`, `tests/test_execution_verify_phase.py`, `tests/test_execution_workspace_phase.py`, `tests/test_recovery_state.py`, `tests/test_guidance_approval.py` and direct catalog/recovery fixture tests for multi-project update isolation.
- Full verify confirms unchanged catalog ordering, recovery outcomes and cross-platform collection of the refactored tests.

## Progress

- [ ] Separate catalog consumers and recovery fixture responsibilities.
- [ ] Preserve scenario assertions and scope fixture writes.

## Decision Log

- Manifest findings 52, 67, 68, 71, 73, 76 and 94–96, 100 identify test-side responsibility mixing. The work remains independently reviewable because it does not change production code or G-143's connection lifetime helpers.
- Observed shared fixture paths: `tests/support/blocked_remote.py`, `tests/support/catalog.py`, `tests/support/execution_recovery_scenarios.py`, `tests/support/rebase_block.py`.
