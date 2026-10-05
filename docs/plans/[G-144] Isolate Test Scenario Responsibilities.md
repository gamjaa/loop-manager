---
id: G-144
title: Isolate Test Scenario Responsibilities
sources:
  - docs/PRODUCT.md
depends_on:
  - G-158
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

- Worker runs targeted unit tests in `tests/test_project_catalog_refresh.py`, `tests.test_execplan_catalog.ExecPlanHeaderRenderingTest`, `tests/test_execution_immediate_rebase_publication_phase.py`, `tests/test_execution_verify_phase.py`, `tests/test_execution_workspace_phase.py`, `tests/test_recovery_state.py`, `tests/test_guidance_approval.py` and direct catalog/recovery fixture tests for multi-project update isolation.
- Full verify confirms unchanged catalog ordering, recovery outcomes and cross-platform collection of the refactored tests.

## Progress

- [x] Separate catalog consumers and recovery fixture responsibilities.
- [x] Preserve scenario assertions and scope fixture writes.

## Decision Log

- Manifest findings 52, 67, 68, 71, 73, 76 and 94–96, 100 identify test-side responsibility mixing. The work remains independently reviewable because it does not change production code or G-143's connection lifetime helpers.
- Observed shared fixture paths: `tests/support/blocked_remote.py`, `tests/support/catalog.py`, `tests/support/execution_recovery_scenarios.py`, `tests/support/rebase_block.py`.

- 2026-10-05 사용자 실행 순서 결정: G-158의 archive Requirement 감사·복구 준비 PR이 실제 Merge된 뒤 기존 세션과 후보를 보존하여 재개한다. 미충족 Requirement는 활성 입력으로 유지하고 조기 archive 강제를 완료 조건으로 적용하지 않는다. 재개 후 targeted test, Manager 전체 verify, Reviewer와 PR 게시를 통해 실제 복구를 확인하며 다른 안전 차단은 별도로 판단한다.
- 2026-10-05 구현 기록: catalog cache/CAS와 planning/PR/archive/project consumer 테스트를 소유자별로 묶고, ExecPlan fixture 헤더를 공통 renderer로 합쳐 기본값과 변형 형식을 보존했다. 복구 fixture 준비는 공유 경계로 추출하고 SQL을 대상 repository와 plan으로 한정했으며, setup 재호출·불필요 mixin 상속을 제거하고 guidance Git 명령을 공통 subprocess helper로 연결했다. 생산 코드와 저장 형식은 변경하지 않았다.
