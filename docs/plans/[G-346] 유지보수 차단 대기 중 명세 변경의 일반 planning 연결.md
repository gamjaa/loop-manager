---
id: G-346
title: 유지보수 차단 대기 중 명세 변경의 일반 planning 연결
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

게시가 확정된 활성 유지보수에서 실행 가능한 READY가 없어도 실제 명세 변경·draft 답변의 일반 planning 필요성을 확인한다. 차단 대기 중 변경된 조건을 관련 PR 전 계획에 반영할 수 있게 하며, cycle의 고정 범위·차단·완료 조건과 기존 명시적 재진입 권한은 보존한다.

현재 `planning/selection_service.py:_select_planning_work()`는 `decision.runnable_ready is not None`일 때만 prerequisite/refactoring의 `maintenance_specification_changed`를 계산한다. 모든 대상이 PLAN_BLOCKED·dependency 대기이면 변경된 명세나 질문 답변이 있어도 아래 WAITING 분기로 반환한다. 최신 대기 상세 출력은 남은 ID·상태를 보여주지만 이 입력 확인 누락을 해소하지 않는다. 이는 정적으로 확인한 실행 경로이며 보고 406의 과거 실패 원인을 이 경로로 확정하거나 현재 재현했다고 주장하지 않는다.

## Acceptance Criteria

- R-082/R-055: 미확정 게시가 없고 기존 프로젝트 안전·소유권 조건을 만족하는 prerequisite/refactoring 경계에서, READY 유무와 관계없이 최신 PRODUCT·활성 Requirement·ARCHITECTURE 변경 또는 유효한 draft 답변을 기존 일반 planning 조건으로 확인한다. PLAN_BLOCKED·dependency 대기만으로 검토를 영구 보류하지 않는다. 관련 snapshot 선정·게시·catalog/DB 반영 뒤에만 현재 자격으로 후속 처리를 결정한다.
- R-074: 일반 명세 변경 검토를 유지보수의 새 평가·사후 재검증으로 바꾸지 않는다. 확정한 cycle 범위·필요 dependency·Merge/취소에 따른 종료 기준과 실제 미해소 차단을 보존한다. 기존 후속 확인 단계의 해제·미확정 작성 결과 보존은 현행 경계를 사용한다.
- 변경이 없거나 코드·ExecPlan·SHA만 바뀌면 Planner를 호출하지 않는다. 기존 목적별 성공 기준·저장 결과·publication intent를 재사용하며 입력 조회 실패·의미 불확실은 변경 없음이나 성공으로 기록하지 않는다. 미확정 게시 복구는 새 입력 검토보다 먼저 확정하고 현재 입력을 다시 확인한다.
- R-091: 새 일반 planning이 필요하면 기존 프로젝트 배정 중단·실행 중 호출의 안전 반환을 사용한다. 사용자 pause·PROJECT_BLOCKED·미종료 writer·planning/게시 소유권을 우회하지 않는다. 미확정 게시 없는 cycle의 명시적 수동 제출/차단 해제 재진입은 기존 `cycle_allows_recovery`를 사용하고 일반 신규에는 기존 범위 제한을 유지한다.
- 입력 불변 대기는 기존 남은 작업 ID·상태를 표시하고 모델 무호출로 유지한다. 명세 반영만으로 질문 해소·차단 해제·검증 성공·Merge·프로젝트 최종 전문 확인을 만들지 않으며, 같은 입력의 재진입/재시작에 중복 Planner·개정·게시가 없다.

## Implementation

- [ ] 확인한 `planning/selection_service.py:_select_planning_work()`의 READY 조건부 명세 확인과 WAITING 반환 순서를 조정한다. `_user_decision_pending()`·`_general_planning_required()`·`_specification_planning_required()`의 현재 지문/기준·archive 판정을 재사용하고 별도 명세 탐색기·설정·상태 원본을 만들지 않는다. 필요한 검토가 없을 때만 cycle의 기존 선택/대기를 적용한다.
- [ ] 확인한 `catalog/project_selection.py:catalog_schedule_decision()`·`maintenance/maintenance_orchestration.py:cycle_allows_recovery()`와 `planning/state_ports.py`의 기존 선택·전이를 그대로 소비한다. catalog의 PLAN_BLOCKED·draft·dependency gate를 완화하거나 cycle 범위를 일반 작업 전체로 넓히지 않는다. G-345의 관리 순서/인계 교정과 G-343의 선언 불일치 원인 재평가는 재구현하지 않는다.
- [ ] 확인한 `planning/publication_service.py`의 게시 복구/해제와 `storage/planning_input_state.py`·`planning_publication_state.py`의 목적별 기준을 대조한다. 완료한 게시의 미확정 DB 반영·자리 해제를 먼저 처리하고, 이후 실제 새 명세만 기존 일반 경로로 연결한다. 실패 planning의 suspend/독립 선택은 G-278, 잔여 정리 분리는 G-337, 원인 복구 admission은 G-338에 남긴다.
- [ ] 확인한 `tests/test_planning_selection.py`·`tests/support/planning_case.py`의 FakePorts와 실제 상태 저장 fixture를 재사용해 아래 경계를 검증한다. PRODUCT·ARCHITECTURE에 현재 계약이 이미 있으므로 필요한 구현 결과·설명 불일치만 소유 절에 통합하고 Requirement·다른 ExecPlan은 수정하지 않는다.

## Validation

- Worker targeted unit은 `tests/test_planning_selection.py`의 변경한 cycle 선택 사례다. prerequisite/refactoring에서 runnable READY 없음·차단 대상/미Merge 선행·새 명세 또는 draft 답변을 두고 일반 planning 선택과 기존 차단/고정 범위 보존을 확인한다. 구현 부족과 사용자 답변 의미 판단 자체를 성공 mock으로 대체하지 않는다.
- 동일 fixture에서 실제 성공 기준을 고정한 무변경 대기·코드/ExecPlan만 변경·변경 입력 조회 오류를 구분한다. 새 `test_maintenance_wait_reports_its_actual_blocked_target`는 유효한 기준을 명시한 안정 대기로 유지하고 남은 ID/상태·범위 밖 ID 제외·Planner 0회 assertion을 보존한다. 누락된 기준을 임의 성공으로 채우지 않는다.
- 미확정 publication·PROJECT_BLOCKED·pause·미종료 호출과 입력/소유권 경쟁은 기존 `tests/test_planning_publication.py`·`tests/test_task_scheduling.py`의 저장/관측 회귀를 소비한다. 효과 시작과 실제 게시/반영 전에는 기존 보호가 남아야 하며, 현재 수동 활성 단계·대표 투영·session·checkpoint가 불변이어야 한다. 새 전체 lifecycle이나 실제 Git 정책 행렬을 추가하지 않는다.
- `tests/test_maintenance_cycle_publication.py:test_published_combined_batch_recovers_before_changed_fingerprint_cleanup`의 완료한 push 무반복·새 명세의 별도 검토·cycle/일반 기준·dependency·선택 ID assertion을 유지한다. 보고 406의 호출 횟수 실패는 현재 원인 확인이 필요하므로 이 선택 경로 교정으로 해결됐다고 미리 기록하지 않는다. Worker는 변경 경계의 unit만 실행하고 기존 실제 Git 게시 복구 회귀는 Manager 검증에 남긴다. 별개 실패가 확인되면 조건/기대값을 약화하지 않고 근거를 보고한다.

## Progress

- [x] revision `393831851402252f6b5e2aeafd40c90cd4b24fd9`의 delta·관련 명세·선택 코드·대기 회귀와 공통 결함 보고 406을 정적으로 대조했다. 현재 READY 조건부 확인 누락과 원인이 미확정인 과거 진단을 구분했으며 구현·테스트·verify는 실행하지 않았다.
- [ ] 차단 대기의 명세 변경/답변 감지와 일반 planning 연결
- [ ] 무변경·미확정 게시·차단/소유권·재시작 보존 최소 회귀

## Decision Log

- R-082는 실제 명세 변경을 일반 planning으로 반영하고 PR 전 PLAN_BLOCKED도 관련 개정에 포함하도록 명시한다. R-074의 한 번 평가·고정 범위는 일반 변경 검토를 없애는 규칙이 아니다. 기존 catalog의 G-278/G-337/G-338은 실패/게시 뒤 복구를, G-345는 명시 요청의 관리 순서를 맡는다. 이 작업은 정상 선택 경계의 입력 확인 누락만 담당하므로 필요한 미Merge 선행 없이 독립 검증·Merge할 수 있다. catalog 밖 감사 계획을 수정하지 않는다.
- 보고 369·387·389/390·405의 기존 교정과 보고 406의 partial/Perl 실제 실행 이동은 현재 코드·회귀에서 소비한다. 보고 406의 남은 호출 횟수 진단과 새 선택 경로의 원인 동일성은 입증하지 못했으므로 공통 결함 연결이나 timeout 완화·테스트 삭제로 처리하지 않는다.
- complexity medium·최초 level 3: 이미 있는 명세 판정·cycle 선택·게시 복구의 순서와 명확한 상태 경계를 연결한다. risk medium: 잘못된 순서는 명세 반영 누락이나 중복 Planner를 만들 수 있어 기존 지문/intent·소유권 gate와 호출 수/저장 상태 회귀로 제한하며 작업물·감사를 보존한다. priority high는 차단 대기의 조건 변경을 같은 자동 흐름에서 반영하기 위한 것이다. Model·Effort는 Manager의 명시 설정을 따른다.
