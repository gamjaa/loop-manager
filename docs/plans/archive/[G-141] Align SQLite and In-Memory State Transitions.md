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

- [x] Align conditional writes and transaction ownership at each storage boundary.
- [x] Add rollback, race and historical-row regressions.

## Decision Log

- Manifest findings 4, 5, 35, 41 and 42 share durable-versus-observed state divergence. High priority follows the concrete rollback and already-started Worker-level corruption paths; the plan preserves schema and state semantics.
- Observed transaction paths: `loop_manager_internal/documents/document_drafts.py`, `loop_manager_internal/execution/execution_state.py`, `loop_manager_internal/runtime/project_registry.py`, `loop_manager_internal/storage/validation_feedback_state.py`, `loop_manager_internal/storage/execplan_catalog_state.py`.
- Draft decisions now check the pending-row CAS count and report the stored decision on a race; the decision remains scoped to that draft and another user's recorded choice is never replaced.
- Rebase completion now updates the expected memory snapshot after the SQLite transaction succeeds, so a failed commit retains the prior snapshot and can retry. Registry schema preparation uses the existing transaction helper and rechecks missing columns under the write lock; it cannot commit the caller's unrelated work.
- A malformed legacy feedback row rolls back all stage audit writes and stays eligible for a corrected retry. Catalog reconciliation treats durable `ever_started` as execution history, preserving an effective Worker level even when other transient evidence has been reset. These paths need no new user action or storage format change; malformed legacy data requires correction before import.
- Added focused regressions for draft CAS races, rebase commit rollback, registry caller rollback, malformed feedback retry, and `ever_started`-only rows. The existing Linux verify entrypoint already delegates to the full verification runner and propagates its exit status.

- Review follow-up: `ExecutionStatePort.complete_rebase` requires an idle connection before any mutation, because releasing a nested savepoint does not commit SQLite. Caller-owned transactions remain untouched and can roll back; completion can retry after the owner finishes. No transaction callback framework or implicit caller commit was introduced.
- Legacy occurrence validation accepts only positive JSON integers within SQLite's signed integer range; fractional, non-finite, boolean, string and out-of-range values leave the source eligible for correction without new partial audit writes.
- Legacy-only prior imports are revalidated even when an old completion marker exists. Malformed input revokes that marker while preserving immutable audit rows; corrected input imports only missing stages. Native validation observations still prevent stale legacy reimport. No schema changes or audit deletion are needed.
- Follow-up validation passed: the two rebase transaction unit cases and `tests.test_validation_feedback_state` (16 tests total), including prior partial-import recovery and invalid occurrence retries. The existing verify script remains suitable and was not executed by Worker.
- Result-report follow-up: reran the exact previously reported catalog command, `python3 -m unittest tests.test_execplan_catalog_state.ExecPlanCatalogStateTest.test_ever_started_alone_preserves_effective_worker_level`, successfully (1 test). The same two rebase cases and feedback module also passed again (16 tests); no unresolved targeted failure was reproduced. No additional implementation change or full verification was needed for this report correction.
- Confirmed the report rejection cause: the result parser interpreted a Korean failure word in the commit-rollback scenario description under `TARGETED_TESTS` as an unresolved test result. Reporting exact test identifiers and PASS avoids that ambiguity without changing the validator. All 17 selected tests passed in one invocation, and the final publication report passed the existing `parse_worker_result` contract.
- Manager verify follow-up: two rebase tests left fixture-only writes uncommitted (run marker/recovery observation and blocked-checkpoint deletion). Production `project_runner` commits the run marker and `RecoveryDispatcher` owns the observation transaction. The fixtures now commit their setup before adoption and assert no open transaction; their existing recovery, session, remote-preservation and no-repeat assertions remain intact. The two reported cases plus the outer-transaction and commit-rollback cases passed (4 targeted tests). Production transaction ownership checks and the full verify entrypoint are unchanged; Worker did not run full verify.
- Partial-import follow-up: a legacy result row alone no longer proves stage completion. Retry reuses the immutable result only when its raw output and identities match the source, preserves existing events, and appends each missing identity event before restoring the completion marker. Mismatched source remains pending rather than attaching feedback to an unrelated audit. The regression covers both zero events and a partially populated multi-identity result, malformed-count correction, audit preservation, delivery and repeated-import idempotency. `python3 -m unittest tests.test_validation_feedback_state` passed all 15 tests; full verify remains Manager-owned.
