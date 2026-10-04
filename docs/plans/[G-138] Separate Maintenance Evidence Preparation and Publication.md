---
id: G-138
title: Separate Maintenance Evidence Preparation and Publication
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-054] Due Refactoring Priority.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

Give maintenance assessment evidence a verified publication boundary. Current export writes catalog and plan files sequentially, then treats catalog existence as completion after interruption. The file ledger also mixes root/Git evidence validation with SQLite application and remote publication, which makes partial success and retry hard to reason about.

## Acceptance Criteria

- A maintenance export is reused only when catalog and every referenced plan file match a complete verified generation; partial writes are repaired or regenerated without accepting truncated evidence.
- Malformed model evidence, including non-string paths, yields the existing reconciliation error and correction path instead of an uncaught type error.
- Root-placement and source-revision evidence is validated before persistence; the final SQLite transaction rechecks identity and publication conditions. Current root decisions, ledger schema, source citations, plan content and public result protocol stay unchanged.

## Implementation

- Separate evidence preparation/validation from storage application in the maintenance ledger and reconciliation services. Give export a completion marker or equivalent atomic generation check using existing workspace storage conventions.
- Reuse the existing source inventory and file-review protocol. Do not regenerate or reinterpret successful semantic receipts for this change.
- The responsibility boundary is source-bound evidence preparation versus ledger commit and export publication. Preserve user behavior, public contract and storage format by keeping the current receipt and plan representations and requiring complete generation proof before reuse.

## Validation

- Worker runs targeted unit tests in `tests/test_maintenance_findings.py`, `tests/test_maintenance_reconciliation.py`, `tests/test_maintenance_file_review.py` and `tests/test_compact_file_review.py` for interruption between catalog/files, malformed path values, empty result objects, identity race and successful retry.
- Full verify confirms maintenance assessment, ledger and publication resume behavior without accepting partial or stale evidence.

## Progress

- [ ] Establish complete-generation export and ledger responsibility boundaries.
- [ ] Add interruption, malformed-result and retry regressions.

## Decision Log

- Manifest findings 17, 18 and 43 identify the publication and validation boundaries; finding 60 calls out incomplete restart coverage. No root movement, schema migration or new assessment call is authorized by this plan.
- Observed evidence paths: `loop_manager_internal/maintenance/maintenance_findings.py`, `loop_manager_internal/maintenance/maintenance_reconciliation.py`, `loop_manager_internal/storage/maintenance_file_ledger.py`.
