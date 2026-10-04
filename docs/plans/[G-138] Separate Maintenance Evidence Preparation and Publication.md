---
id: G-138
title: Separate Maintenance Evidence Preparation and Publication
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

Give maintenance assessment evidence a verified publication boundary. Current export writes catalog and plan files sequentially, then treats catalog existence as completion after interruption. The file ledger also mixes root/Git evidence validation with SQLite application and remote publication, which makes partial success and retry hard to reason about.

R-054's due refactoring priority remains unimplemented and is outside this plan's scope. Keep `docs/requirements/[R-054] Due Refactoring Priority.md` active and cancel its archive move in this candidate. This plan does not establish completion of R-054; its unrelated source association is removed so that G-138 cannot authorize that archive move.

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

- [x] Establish complete-generation export and ledger responsibility boundaries.
- [x] Add interruption, malformed-result and retry regressions.

## Decision Log

- Manifest findings 17, 18 and 43 identify the publication and validation boundaries; finding 60 calls out incomplete restart coverage. No root movement, schema migration or new assessment call is authorized by this plan.
- Observed evidence paths: `loop_manager_internal/maintenance/maintenance_findings.py`, `loop_manager_internal/maintenance/maintenance_reconciliation.py`, `loop_manager_internal/storage/maintenance_file_ledger.py`.

- 2026-10-05 복구 실행 결정: 보존된 작업의 정상 자동 실행 복구 요청에 따라, R-070 병합 이후 전체 verify가 드러낸 복구 fixture·Git 관측 회귀는 G-160의 별도 수정으로 처리한다. G-138의 제품 목표와 구현 범위를 확대하지 않는다. 해당 회귀 수정이 원격 main에 반영되면 기존 Worker 세션으로 최신 후보를 확인하고 targeted test·Manager 전체 verify·Reviewer를 다시 수행한다. 이전 archive 이동은 완료 조건으로 요구하지 않으며 R-054 활성 경로를 유지한다.
- The existing findings manifest is the completion marker: export verifies every expected catalog, plan and unit byte before reuse, repairs incomplete files with atomic replacements, then publishes the manifest last. Ledger evidence preparation runs before writes and is rechecked inside the SQLite transaction; the source and root representations remain unchanged.
- User Decision (2026-10-04): “이동하지 않고 유지해줘.” Cancel R-054's archive move and retain the requirement at its active path without changing its contents. Preserve the PRODUCT and ARCHITECTURE links to that active path. Resume G-138's existing implementation and validation after reconciling the candidate with this decision; R-054 must remain available to Planner until its own acceptance criteria are implemented.
