---
id: G-338
title: planning 점유 중 원인 복구의 안전한 진입
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-337
complexity: high
risk: high
priority: high
---

## Purpose

planning 게시/해제가 기다리는 작업의 복구를 planning 점유 자체가 거부하는 상호 대기를 해소한다. 독립 자원은 G-336/G-337의 해제로 분리하고, 실제 충돌 자원은 현재 소유권·writer 종료·checkpoint·lease가 확인된 원인 복구에 한해 안전한 진입을 확보한다. 일반 새 호출과 복구의 권한을 구분하며 미완료 복구의 다음 단계가 중복 해제 억제로 영구 배제되지 않게 한다.

현재 최상위 planning `WAITING`은 작업 coordinator 진입을 막지 않고, `recovering_plan()`·`recovery_admission()`은 대상 복구 context를 scheduler에 전달해 일반 실행 순위 부재나 PLAN_BLOCKED 자체가 복구용 호출을 거부하지 않게 한다. 이 직접 구현은 재사용하며 실제 planning 소유권을 해소한 것으로 확대하지 않는다. `ProjectService._reconcile_catalog_and_cleanup()`는 publication 복구의 반환을 catalog·PR/원인 복구보다 먼저 소비한다. `project_block_service._reconcile_rebase_evidence_block()`와 `RuntimeStateRepository.resume_rebase_block()`는 planning 점유를 거부하며, 후자는 같은 WAITING 해제 관측의 반복도 거부한다. 이 각각의 보호를 제거하지 않고 원인을 해소하는 제한된 복구와 일반 단계 배정을 구분한다.

## Acceptance Criteria

- R-055 B04/B05·R-091: planning 해제→작업 rebase 복구→planning 자리 반환의 대기 관계를 현재 operation/소유 자원으로 확인한다. 독립성에 따른 해제 또는 검증된 원인 복구를 통해 다음 단계에 도달하며, 안전 미확정이면 원인 소유자·영향 대상·충돌 자원·서로 기다리는 조건·다음 재평가 조건을 보존한다.
- 실제 충돌 시 복구 대상의 현재 소유권·writer 종료·checkpoint generation·원격/lease와 기존 PROJECT_BLOCKED 복구 허용 범위를 확인한다. competing Agent/verify/게시·다른 복구는 시작하지 않으며 planning 오류·원문·결과/session·intent와 다른 계획의 투영·독립 차단을 보존한다. 일반 명세 반영 순서를 우회해 원래 Worker를 먼저 실행하지 않는다.
- 이미 허가한 미완료 복구는 같은 관측에서 새 해제·새 Worker 교정을 중복 생성하지 않고 남은 Manager 복구 단계만 이어간다. 관련 writer 종료·자원 반환·복구 전이·원인 교정으로 조건이 달라지면 문서 지문이 같아도 그 원인을 재평가한다. 무관한 SHA/새 run·같은 관측의 재저장은 해소나 새 실행 권한이 아니다.
- Git/원격 관측과 실제 복구는 DB 요청 밖에서 수행하고 현재 대상·원인·projection/소유·checkpoint generation을 CAS로 재검사한다. 중단/재시작·lease 경쟁에서 다른 계획 상태를 덮거나 완료한 Git 효과를 반복하지 않는다. 미수행 verify·Review·rebase 성공은 만들지 않는다.
- 조건 재관측과 실제 진행 결과를 기존 구조화 근거로 남겨 G-325의 무진전 판단과 G-194/G-195의 읽기 전용 설명에 연결한다. 로그 text를 실행 지시로 해석하거나 이 작업에 새 정체/알림 엔진을 도입하지 않는다.

## Implementation

- [ ] 확인한 `application/project_service.py:_reconcile_catalog_and_cleanup()`, `planning/planning_runtime.py`의 publication 우선 복구, `recovery/project_block_service.py:_reconcile_rebase_evidence_block()`와 `recovery/scope_reconciliation.py`의 planning 소유 gate를 대조한다. 현재 소유 자원·원인 작업을 확인해 필요한 기존 복구만 진입시키고 G-336/G-337이 이미 분리한 독립 보류를 다시 프로젝트 gate로 만들지 않는다.
- [ ] 확인한 `storage/runtime_state.py:resume_rebase_block()`·`planning_owns_execution_slot()`과 `recovery/plan_recovery.py`의 기존 대상별 admission과 `application/task_scheduling.py`의 `Task.recovery`·`_eligible()`를 재사용한다. `recovering_plan()`의 대상 context 설정/종료 후 복원, 정확한 프로젝트 차단 소유자의 기존 자리 유지·Reviewer/verify 배정은 다시 구현하지 않는다. 현재 남아 있는 planning 점유·publication gate와 허가된 원인 복구의 충돌만 확인한 자원/소유 범위에서 조정하며 일반 실행에 복구 예외를 남기지 않는다. 중복 해제 여부와 허가된 미완료 복구의 후속 실행을 구분하고 원래 `rebase_block_release`·checkpoint·실행 원본·planning 재개 대상을 보존한다. 새로운 전역 phase·소유권 우회 flag·비호환 schema를 만들지 않는다.
- [ ] `planning/selection_service.py`·`planning/publication_service.py`와 프로젝트 결과 소비 경계에서 복구 진행 후 현재 상태를 다시 읽어 남은 게시/해제만 수행한다. 실제 입력 변화 없는 모델 재호출·일괄 block 해제·실패 집계 초기화를 추가하지 않는다. 실패 planning suspend/독립 선택의 남은 연결은 G-278에 맡긴다.
- [ ] 확인한 `tests/test_scope_block_revalidation.py:EvidenceFreeRebaseBlockTests`, `tests/test_planning_publication.py`, `tests/test_project_services.py`, `tests/test_runtime_state.py`의 실제 저장/관측 fixture에 최소 연결을 추가한다. PRODUCT·ARCHITECTURE의 planning 우선·차단 복구 허용과 관리 rebase의 후속 단계 책임을 구현에 맞게 통합한다.

## Validation

- Worker targeted unit은 위 fixture의 변경 case다. planning publication이 원인 rebase를 기다리고 원인 복구가 planning 점유로 거부되는 입력에서 안전한 복구→해제→저장된 다음 단계에 도달하는지 실제 SQLite와 double 관측으로 검사한다. 같은 worktree의 competing writer/게시 시작은 0회, 독립 작업·계획별 상태는 불변이어야 한다.
- 기존 `test_same_pending_result_cannot_repeat_a_released_correction`의 중복 교정 거부는 유지한다. 이미 허가된 WAITING 복구의 다음 Manager 단계, writer 종료/자원 반환 뒤 문서 지문 불변 재평가를 별도로 연결한다. 이전 해제 표식을 지우거나 같은 결과에 새 교정을 발급해 진행시키면 실패다. `tests/test_project_services.py:test_scheduled_planning_wait_still_dispatches_task_recovery`와 `tests/test_task_scheduling.py`의 `test_blocked_recovery_uses_scheduler_for_review_verify_and_reactivation`·`test_project_recovery_admission_preserves_exact_active_owner_and_planning_gate`를 기존 진입/admission 근거로 재사용한다. planning 소유가 남으면 거부하는 현재 회귀를 전역 완화하지 않고, 검증한 원인 복구→자리 해제의 남은 연결만 검사한다.
- owner·checkpoint·block cause·lease 경쟁, 종료 불명·관측 실패·실제 사용자 판단·dependency와 독립 PROJECT_BLOCKED를 우회하지 않는다. 안전하게 진입할 수 없는 경우 구조화 대기 관계와 조건이 남고 같은 호출을 무진전 반복하지 않아야 한다.
- 복구 진입/효과/저장·planning 해제 전후 중단에서 완료 effect 무반복과 원래 target·index·session·감사 보존을 확인한다. 실제 rebase/index 효과는 기존 최소 통합 회귀에 남기고 정책 행렬은 현재 관측 port로 검사한다. 새 모델/전체 verify 반복으로 순환 해소를 대신하지 않는다.

## Progress

- [x] `9dd75b5d`의 publication 우선 반환·planning 소유 gate·WAITING 중복 해제 거부와 기존 회귀를 정적으로 대조했다. 모든 경계를 실제 장애로 재현했다고 주장하지 않으며 구현·테스트는 실행하지 않았다.
- [ ] 원인 복구의 제한된 admission·후속 실행과 조건 재평가
- [ ] 상호 대기·중복 억제·경쟁/재시작 보존 회귀와 문서 정합성

## Decision Log

- 이번 delta의 WAITING coordinator 진입과 scheduler 복구 context는 직접 구현으로 소비한다. 일반 실행/복구 구분이 생겨도 실제 planning 점유·게시 해제의 상호 대기 목표는 남아 있다. 기존 ID·경로·dependency·분류·수행 기록을 보존하며 공통 진입 교정이나 현재 후보의 검증 성공을 소급 작성하지 않는다.

- R-091이 명시한 기존 PROJECT_BLOCKED 복구 권한과 자원 배타 안의 조율 교정이다. 독립 자원 해제와 잔여 정리 수명은 G-336/G-337을 먼저 소비하고, 실패 planning의 일반 독립 선택·지속 이력/통지는 G-278·G-325/G-326에 남겨 같은 엔진을 중복 계획하지 않는다.
- complexity high: planning 소유권·대상별 복구 권한·중복 억제와 중단 후 후속 전이를 함께 맞춰야 한다. risk high: 잘못된 admission은 같은 자원의 중복 writer/게시나 다른 계획의 복구 상태 훼손을 만들 수 있다. 현재 소유/종료·정확한 lease·generation CAS·경쟁 거부와 보존 회귀로 완화한다. 자동 복구를 영구 배제하는 순환의 교정으로 priority high, 최초 level 5다.
