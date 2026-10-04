---
id: G-141
title: Align SQLite and In-Memory State Transitions
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

Make transaction ownership and conditional updates explicit across stored decisions. Current draft decision, rebase completion, registry migration, legacy feedback migration and catalog history paths can report a decision that was not written, advance an in-memory expected snapshot before rollback, commit a caller's transaction, retain partial audit rows, or lower an already-started plan's Worker level.

## Acceptance Criteria

- A draft decision returns success only after its pending-row CAS writes; a concurrent decision is reported from the actual stored row without overwriting it.
- Rebase expected snapshot advances only after its SQLite transaction commits. Rollback leaves memory and durable state aligned for retry. Registry schema preparation cannot commit an unrelated caller transaction.
- Invalid legacy feedback remains recoverable without partial migrated audit or false completion, and durable `ever_started` always prevents resetting effective Worker level. Existing SQLite schemas, decision API, stored data, history and user approval remain compatible.

## Implementation

- Keep each state transition in its owning storage/document/execution module. Reuse the existing transaction helper and rowcount/CAS conventions; prepare schema outside caller-owned mutation or let the caller own commit explicitly.
- Do not create a cross-domain transaction manager or irreversible migration. Maintain fail-closed behavior when historical input is malformed.
- The responsibility boundary is the SQLite transaction owner versus in-memory expected state and returned decisions. Preserve user behavior, public contract and storage format by keeping current rows and CAS rules while applying observable success only after commit. An expected snapshot advanced before rollback can leave automatic execution blocked on retry.

## Validation

- Worker runs targeted unit tests in `tests/test_execution_documents.py`, `tests/test_execution_rebase_correction_phase.py`, `tests/test_origin_registration.py`, `tests/test_validation_feedback_state.py` and `tests/test_execplan_catalog_state.py` for races, injected commit failure, legacy malformed values and `ever_started`-only rows.
- Full verify confirms draft, registry, execution rebase, feedback migration and Worker-level history regressions across existing suites.

## Progress

- [ ] Align conditional writes and transaction ownership at each storage boundary.
- [ ] Add rollback, race and historical-row regressions.

## Decision Log

- Manifest findings 4, 5, 35, 41 and 42 share durable-versus-observed state divergence. High priority follows the concrete rollback and already-started Worker-level corruption paths; the plan preserves schema and state semantics.
- Observed transaction paths: `loop_manager_internal/documents/document_drafts.py`, `loop_manager_internal/execution/execution_state.py`, `loop_manager_internal/runtime/project_registry.py`, `loop_manager_internal/storage/validation_feedback_state.py`, `loop_manager_internal/storage/execplan_catalog_state.py`.
