---
id: G-143
title: Give Test Database Fixtures Clear Ownership
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: medium
risk: low
priority: normal
---

## Purpose

Make SQLite fixture lifetime explicit so assertions, setup failures and Windows temporary-directory cleanup test the intended behavior. Several shared helpers return or create connections before cleanup is registered, and one reconnection remains open until after its temporary directory exits.

## Acceptance Criteria

- Shared fixture creators close an internally owned connection on initialization failure; callers that own returned connections register cleanup immediately. Temporary file databases close before their directory context exits on success and failure.
- Existing fixture return values, schema seeds, test scenarios and production behavior remain unchanged. Tests no longer depend on garbage collection or `tearDown` running after failed `setUp`.
- Repeated test cases and subtests do not retain extra connections or patchers into the next scenario.
- Windows verification preserves virtualenv, `py -3`, then `python` selection, invokes the shared runner through one command site, and returns its exit code without skipping checks.

## Implementation

- Reuse `unittest.addCleanup`, context managers and existing fixture helpers in `tests/support/` and direct caller tests. Keep ownership at the helper that creates a connection unless it intentionally returns the live handle.
- Limit changes to resource lifetime and assertions needed to prove closure; do not replace the fixture framework.
- User-approved scope extension: repair the Windows verify-script command contract in `scripts/verify.ps1` and its targeted script-contract tests as needed. Select the Python executable and launcher arguments first, then invoke the shared runner once. Preserve interpreter precedence, UTF-8 setup/restoration, working-directory restoration and failure exit codes. This verification repair is permitted alongside fixture ownership work; production behavior remains unchanged.
- The responsibility boundary is connection creation versus caller-owned lifetime in shared test helpers. Preserve user behavior, public contract and storage format by keeping production files untouched and retaining every fixture's returned data and database schema.

## Validation

- Worker runs targeted unit tests in `tests/test_archive_recovery_regressions.py`, `tests/test_catalog_scheduling.py`, `tests/test_guidance_approval.py`, `tests/test_maintenance_state.py` and direct `tests/support` fixture tests, including injected setup/SQL failures.
- Full verify confirms Linux and Windows compatible cleanup, with no changed assertions about product state or catalog behavior.
- Run the script-contract unit tests in `tests/test_catalog_checkpoint_wiring.py` and `tests/test_internal_packages.py`; Manager performs the full verification afterward.

## Progress

- [ ] Assign connection ownership and immediate cleanup in shared and direct fixtures.
- [ ] Add failure-path lifetime checks where existing tests cannot observe closure.

## Decision Log

- 2026-10-05: User explicitly approved including the verify-script repair in G-143 after repeated full-verify failures from the Windows runner command-count mismatch. Do not block this repair as unrelated fixture scope or weaken the required verification checks. Preserve unrelated Agent scope-violation evidence and resolve it through the existing recovery gates.
- Manifest findings 51, 56, 69, 70, 87–89, 91–93, 97–99 and 101 identify the same fixture ownership problem across callers. This plan changes tests only and is independent of production storage transitions.
- Observed fixture paths: `tests/support/database.py`, `tests/support/maintenance_planning_case.py`, `tests/support/notification.py`, `tests/support/recovery.py`, `tests/support/review_escalation.py`, `tests/support/scoped_recovery.py`, `tests/support/workspace.py`, `tests/test_scope_project_recovery.py`, `tests/test_state_foundations.py`, `tests/test_worker_publication_validation.py`.
