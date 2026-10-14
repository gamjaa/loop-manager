---
id: G-278
title: planning 실패의 보존 작업과 독립 실행 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

진행 작업을 보존해 일반 planning으로 전환한 뒤 오류가 남는 경우, 보존된 작업의 재개 identity와 실제 변경 실행 자리의 소유를 분리한다. 미확인 영향의 작업은 다음 단계에 진입하지 않되 안전하게 자리를 비운 뒤 검증된 독립 계획은 기존 순서로 진행하고, planning 교정 뒤 원래 작업을 같은 단계에서 재개한다.

현재 정상 전환·일반 planning 성공 뒤 재개와 폐기된 Git 진단의 해소는 직접 구현돼 있다. 실패 후 `planning_resume_plan_id`가 남으면 `planning_owns_execution_slot()`은 계속 true이고 `active_row()`도 보존 작업을 반환한다. `catalog_schedule_decision()`의 suspended/독립 READY 분기는 활성 row가 없을 때만 선택하므로 오류를 보존한 원래 작업이 독립 실행까지 막는다. `_recover_ignored_error()` 역시 active 존재만으로 거부해 이 전환에서의 기존 Git 진단 해소에 도달하지 못한다. 진단 해소를 결과 검증 완료로 삼지 않는 최신 `agent_ports.py`를 재사용한다.

## Acceptance Criteria

- R-082: 정상 planning 진입·중단·게시·DB 적용 뒤 같은 작업의 재개는 기존 구현을 유지한다. 원래 ID·실행 단계·branch·worktree·session·실패/level·후보·checkpoint와 planning의 원문·결과·진단은 보존하며 READY 초기화나 다른 작업으로의 교체로 처리하지 않는다.
- R-055/R-082: writer 종료·소유 Git operation·미확정 게시·공유 안전을 검증해 실패 planning의 실행 자리를 비운 경우, 영향 미확인 작업과 그 의존 closure는 계속 보류하고 유효성이 확인된 독립 READY/복구만 기존 priority·dependency·유지보수 gate로 선택한다. 보존된 원래 단계가 실제 실행 중으로 중복 집계되거나 독립 작업과 함께 투영되지 않는다.
- 재개할 작업의 identity만으로 슬롯을 영구 점유하거나, 독립 실행을 허용하기 위해 실행/실패 이력을 지우지 않는다. 독립 작업의 시작·종료·PR 관측이 planning 오류나 원래 작업의 미확인 조건을 해소한 것으로 기록되지 않는다. 실제 동시에 변경하는 작업은 계속 하나다.
- 동일 실패 입력에서는 Planner를 반복하지 않는다. 관련 입력 전진·명시적 로컬 재검사·폐기된 Git 진단 해소는 현재 대상·writer 종료·후보와 CAS 경계에서 보존된 planning으로 돌아간다. 과거 snapshot의 일치나 ignored-only 증명을 복구 진입 조건으로 요구하지 않는다. 결과 변경 범위 오류·독립 sandbox/Review/사용자 판단은 유지하며 진단 해소가 `planning_result_completed`를 만들거나 현재 결과의 검증·교정을 건너뛰지 않는다.
- 독립 작업이 실행 자리를 쓰거나 writer/Git/게시 결과가 미확정이면 planning 복구와 원래 작업 재개를 겹치지 않는다. 게시 후 DB 미적용은 저장 intent의 반영부터 복구하고 후속 원격 입력을 별도로 검토한다. 현재 입력의 유효한 결과 확인 전에 원래 작업을 재개하지 않는다.
- suspend·독립 activation·planning 복원·결과 적용·원래 단계 재투영 사이의 중단과 CAS 경쟁을 멱등 복구한다. 새 dependency·실제 PR·수동 소유·PROJECT_BLOCKED·사용자 결정·유지보수 범위와 모든 별도 실패를 보존하며 복구 불확실에서는 해당 범위만 대기한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/storage/runtime_state.py`의 `planning_owns_execution_slot`, `suspend_planning`, `recovery_slot_available`, `active_row`, `pause_execution_for_planning`, `resume_execution_after_planning`과 `storage/project_state_adapter.py`의 activation/hydration을 함께 대조한다. 보존 작업과 실제 슬롯 사용자를 기존 projection·checkpoint·호환 가능한 저장 근거로 구별하고 실행 원본의 단계·이력을 보존한다. 새 실행 phase나 비호환 migration은 만들지 않는다.
- [ ] `loop_manager_internal/planning/selection_service.py`의 오류 gate·독립 선택·복구와 `catalog/project_selection.py`의 영향 closure·priority 판단을 같은 구분에 연결한다. 기존 `preserved_unconfirmed_plan_ids`와 writer/게시 검사를 재사용하며 단순 코드 변경을 독립성 증거로 취급하지 않는다. 일반 planning 성공·변경 없음의 정상 경로를 다시 구현하지 않는다.
- [ ] `planning/agent_ports.py`의 현재 결과 검증·폐기된 Git 진단 해소, `planning/state_ports.py`, `application/project_service.py`와 기존 planning publication/runtime 저장을 재사용해 원인 재검사와 게시 후 적용·새 원격 검토·원래 작업 복원을 연결한다. 해소 뒤 남은 결과 검증·현재 후보 교정을 실제로 완료한 경우만 재개한다. 보존된 planning worktree와 구현 worktree의 쓰기를 겹치지 않는다.
- [ ] 실제 보완한 실패·보존·독립 선택과 재개 계약만 PRODUCT·ARCHITECTURE의 해당 절에 통합한다. R-082의 정상 전환과 다른 계획의 parser·수동 소유 보호·충돌 교정은 각 기존 담당 작업에 남긴다.

## Validation

- Worker의 최소 targeted unit은 `tests/test_planning_selection.py`, `tests/test_runtime_state.py`, `tests/test_planning_error_recovery.py`의 변경한 실패/재개 case로 구성한다. 기존 `tests/support/planning_case.py`, `tests/support/execution.py`와 실제 SQLite transaction을 사용하고 Git·Agent·원격 관측만 mock한다.
- 영향받은 원래 작업과 독립 READY를 함께 두고, 안전한 suspend 후 독립 작업 한 개의 선택·진행, 보류 작업/의존 closure의 미호출, 동일 오류의 Planner no-call과 오류·session·실패·원문 보존을 확인한다. 독립 작업의 PR 대기와 실제 종료 뒤 planning 복구/원래 단계 재개도 확인한다.
- 종료 불명 writer·소유 operation·prepared 게시·공유 안전·수동 소유·dependency·활성 유지보수와 안전한 독립성 근거 부재에서는 실행 자리를 넘기지 않는다. 정상 planning 성공·변경 없음·명세 무변경 재개는 호출 수·순서와 실제 저장 row로 검증한다.
- 기존 Git 진단 복구에서 보존 작업의 identity만으로 거부하지 않는다. `tests/test_planning_error_recovery.py`의 현재 ignored/config/current_tracked 해소와 결과 오류/ownership/race/독립 오류 보존을 재사용하며, 바뀐 후보의 결과 검증·완료 marker 불변을 연결한다. 사후 Git 권한 검사를 되살리지 않고 테스트 준비 비용 전환은 G-270에 남긴다.
- `tests/test_planning_publication.py`, `tests/test_incremental_planning.py`, `tests/test_execution_checkpoint_recovery_phase.py`의 기존 연결 회귀로 push 후 DB 미적용·후속 base 전진·독립 실행 중 입력 변경·재개 transaction 중단을 확인한다. 모의 결과를 실제 원격/OS 성공으로 기록하지 않고 새로운 전체 실행 반복을 추가하지 않는다.

## Progress

현재 revision의 정상 전환 회귀와 위 실패 gate·선택·activation 소비자를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.

- [ ] 보존 작업/실제 슬롯 구분과 오류·독립 선택 연결
- [ ] 중단·경쟁·오탐 복구의 targeted unit 검증

## Decision Log

- 현재 R-055의 진단 해소와 결과 유효성 검증을 분리한다. 이 작업은 보존된 재개 identity와 실제 슬롯의 연결만 보완하며 이미 구현된 Git 진단 해소·권한 검사 제거를 재구현하지 않는다. 기존 high complexity/risk·목표·의존성·기록과 독립 실패를 보존한다.
- complexity high: 원래 작업의 실행 원본, 실패 planning의 별도 보존, 독립 작업의 실제 슬롯과 게시 후 복원을 중단·CAS 경쟁에도 일관되게 연결하는 판단이 남는다. 정상 성공 경로의 소급 구현 계획은 만들지 않는다.
- risk high: 잘못된 슬롯 해제·재투영은 미확인 조건의 작업 실행이나 같은 자원의 중복 writer를 만들고 수동 복구를 요구한다. 종료/소유/게시 근거·원문·실행 이력을 유지하고 transaction 경쟁과 보류 closure를 검증한다. 실제 Git 결과를 코드 rollback으로 복구한다고 가정하지 않는다.
- priority high: 동일 planning 실패가 검증된 독립 자동 작업까지 중단시키는 잔여 경로를 제거한다. 기존 R-055의 안전한 실행 자리 해제와 R-082의 같은 작업 재개 범위이며 새로운 병렬 실행·사용자 정책을 도입하지 않는다.
