---
id: G-278
title: planning 실패의 보존 작업과 독립 실행 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-297
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
  - G-338
complexity: high
risk: high
priority: high
---

## Purpose

진행 작업을 보존해 일반 planning으로 전환한 뒤 오류가 남는 경우, 보존된 작업의 재개 identity와 실제 변경 실행 자리의 소유를 분리한다. 미확인 영향의 작업은 다음 단계에 진입하지 않되 안전하게 자리를 비운 뒤 검증된 독립 계획은 기존 순서로 진행하고, planning 교정 뒤 원래 작업을 같은 단계에서 재개한다.

현재 정상 전환·일반 planning 성공 뒤 재개와 폐기된 Git 진단의 해소는 직접 구현돼 있다. 최신 `application/project_service.py:_run_coordinators()`는 프로젝트 로컬 재검사에서 planning이 실행 자리를 소유하면 보존된 active row가 있어도 planning으로 전달하고, 실제 실행 소유 또는 명시적 계획 대상의 복구는 기존 실행 경로로 전달한다. 이 진입 교정은 재구현하지 않으며 실패 planning의 안전한 suspend·독립 선택·후속 재개 전체가 완료됐다는 뜻으로 확대하지 않는다. 실패 후 `planning_resume_plan_id`가 남으면 `planning_owns_execution_slot()`은 계속 true이고 `active_row()`도 보존 작업을 반환한다. `catalog_schedule_decision()`의 suspended/독립 READY 분기는 활성 row가 없을 때만 선택하므로 오류를 보존한 원래 작업이 독립 실행까지 막는다. `_recover_ignored_error()` 역시 active 존재만으로 거부해 이 전환에서의 기존 Git 진단 해소에 도달하지 못한다. 진단 해소를 결과 검증 완료로 삼지 않는 최신 `agent_ports.py`를 재사용한다.

## Acceptance Criteria

- R-082: 정상 planning 진입·중단·게시·DB 적용 뒤 같은 작업의 재개는 기존 구현을 유지한다. 원래 ID·실행 단계·branch·worktree·session·실패/level·후보·checkpoint와 planning의 원문·결과·진단은 보존하며 READY 초기화나 다른 작업으로의 교체로 처리하지 않는다.
- R-055/R-082/R-091: writer 종료·소유 Git operation·미확정 게시·공유 안전을 검증해 실패 planning의 실행 자리를 비운 경우, 영향 미확인 작업과 그 의존 closure는 계속 보류하고 유효성이 확인된 독립 READY/복구만 기존 priority·dependency·유지보수 gate와 R-091의 한도·배정 순서로 선택한다. 보존된 원래 단계와 실제 실행·대기를 구분해 관리/Agent/verify를 중복 계산하지 않으며 작업별 원본과 대표 투영을 섞지 않는다.
- 현재 소유권과 종료·보존을 확인한 rebase는 작업물·index·원래 target·checkpoint를 그대로 두고 별도 planning을 진행할 수 있다. 실패 planning의 안전한 suspend에서도 이 작업의 자원 예약은 유지하며, 같은 worktree의 다음 writer와 다른 실제 Git operation·미확정 공유 게시 gate는 계속 적용한다. rebase 표식만으로 프로젝트 전체 실행 자리를 영구 점유하거나 rebase 완료·검증 성공을 만들지 않는다. 결과 게시·catalog·DB 적용 뒤 같은 작업을 복원할 때 현재 base/명세와 보존 target을 기존 revision gate로 확인한다.
- 재개할 작업의 identity만으로 슬롯을 영구 점유하거나, 독립 실행을 허용하기 위해 실행/실패 이력을 지우지 않는다. 독립 작업의 시작·종료·PR 관측이 planning 오류나 원래 작업의 미확인 조건을 해소한 것으로 기록되지 않는다. 독립 branch·worktree 작업은 R-091 한도 안에서 병행할 수 있고 같은 후보의 writer·verify·Review·rebase와 공유 planning/게시 자원은 기존 배타 조건을 지킨다.
- 동일 실패 입력에서는 Planner를 반복하지 않는다. R-082의 유효한 결과·게시 intent·미완료 DB 적용을 먼저 복구한다. 실제 입력 변경으로 결과가 적용 불가하거나 필수 의미 판단 누락·결과 오류·계획 작성 범위 충돌을 교정해야 할 때만 기존 세션에서 필요한 판단을 재호출한다. 명시적 로컬 재검사·폐기된 Git 진단 해소 자체는 새 모델 호출 조건이 아니며 현재 대상·writer 종료·후보와 CAS를 다시 확인해 남은 단계로 돌아간다. ExecPlan·코드 변경만을 새 일반·점검 Planner의 시작 조건으로 쓰지 않는다. 과거 snapshot 일치나 ignored-only 증명을 요구하지 않고 독립 sandbox/Review/사용자 판단을 보존하며, 진단 해소가 `planning_result_completed`를 만들거나 현재 결과의 검증을 건너뛰지 않는다.
- 복구 대상 또는 공유 자원의 writer/Git/게시 결과가 미확정이면 그 자원을 사용하는 planning 복구와 원래 작업 재개를 겹치지 않는다. 독립 작업의 존재만으로 프로젝트 전체에 단일 변경 자리를 요구하지 않는다. 게시 후 DB 미적용은 저장 intent의 반영부터 복구하고 후속 원격 입력을 별도로 검토한다. 현재 입력의 유효한 결과 확인 전에 원래 작업을 재개하지 않는다.
- DB 적용 후 정리 실패·제거 후 최종 해제 미확정은 미게시 실패 planning과 구분한다. G-337의 분리 가능한 잔여 정리 보존·최종 해제와 G-338의 원인 복구 경계를 소비하며, `pending_publication_release()`의 소유권을 일반 suspend나 독립 activation으로 덮지 않는다. 안전한 분리·최종 CAS가 확인되면 잔여 정리가 있어도 유효한 작업은 재개하고, 실제 미확정 게시/소유/종료에는 대기한다. 적용한 catalog·목적별 기준과 유지보수 restart의 cycle·게시 OID·intent를 보존하며 이미 끝난 push·batch 적용·제거·Planner를 반복하지 않는다.
- suspend·독립 activation·planning 복원·결과 적용·원래 단계 재투영 사이의 중단과 CAS 경쟁을 멱등 복구한다. 새 dependency·실제 PR·수동 소유·PROJECT_BLOCKED·사용자 결정·유지보수 범위와 모든 별도 실패를 보존하며 복구 불확실에서는 해당 범위만 대기한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/storage/runtime_state.py`의 `planning_owns_execution_slot`, `suspend_planning`, `recovery_slot_available`, `active_row`, `pause_execution_for_planning`, `resume_execution_after_planning`과 `storage/project_state_adapter.py`의 activation/hydration을 함께 대조한다. 보존 작업과 실제 슬롯 사용자를 기존 projection·checkpoint·호환 가능한 저장 근거로 구별하고 실행 원본의 단계·이력을 보존한다. 새 실행 phase나 비호환 migration은 만들지 않는다.
- [ ] 최신 `application/task_scheduling.py:run_project_tasks()`와 G-297의 작업별 대기/공유 중단 구분을 소비한다. 실패 planning의 영향 closure와 관리·실제 호출 소유를 별도로 적용하고, `active_row()`의 대표 한 행만으로 복수 실행이 없거나 모든 작업이 보류됐다고 판정하지 않는다. 작업별 선택 context와 기존 planning mutex·게시 예약을 유지한다. 최신 `ProjectService._run_coordinators()`의 최상위 WAITING 전달과 `project_recheck`의 프로젝트 성공 기준 재관측·미반영 명세 drain은 직접 구현으로 소비한다. coordinator에 도달했다는 사실을 실패 planning의 suspend/독립 실행 허가로 바꾸지 않고, 해당 gate를 실제 보존/충돌 근거로 해소한 후 이 계획의 영향 closure·선택을 적용한다.
- [ ] `loop_manager_internal/planning/selection_service.py`의 오류 gate·독립 선택·복구와 `catalog/project_selection.py`의 영향 closure·priority 판단을 같은 구분에 연결한다. 기존 `preserved_unconfirmed_plan_ids`와 writer/게시 검사를 재사용하며 단순 코드 변경을 독립성 증거로 취급하지 않는다. `planning/git_ports.py:execution_blocks_planning()`의 보존 rebase/다른 operation 구분과 `runtime/resource_lock.py`의 프로젝트 준비·planning 접근 허용을 소비한다. `current_task`의 배정 중단·runtime handoff·실제 자원 잠금은 유지하며 일반 planning 성공·변경 없음의 정상 경로를 다시 구현하지 않는다.
- [ ] `planning/agent_ports.py`의 현재 결과 검증·폐기된 Git 진단 해소, `planning/state_ports.py`, `application/project_service.py`와 기존 planning publication/runtime 저장을 재사용해 원인 재검사와 게시 후 적용·새 원격 검토·원래 작업 복원을 연결한다. `planning/publication_service.py:_recover_publication()`/`_complete_publication_release()`의 적용된 release 우선 복구와 `storage/planning_publication_state.py:retain_planning_after_publication()`을 소비하며, `selection_service.py:_suspend_failed_planning()`·runtime의 suspend/restore가 이 대기를 미게시 실패로 취급하지 않게 연결한다. 일반 intent가 없는 변경 없음 완료·유지보수도 실제 release 근거로 구별한다. 외부 관측·제거는 공통 DB 접근 밖에 두고 최종 해제는 기존 owner·cycle·intent·catalog·membership CAS를 재사용한다. 해소 뒤 남은 결과 검증·현재 후보 교정을 실제로 완료한 경우만 재개한다. 보존된 planning worktree와 구현 worktree의 쓰기를 겹치지 않는다. 최신 project service의 로컬 재검사 전달을 그대로 소비하고 `_recover_ignored_error()`의 실제 소유 파일인 `planning/selection_service.py`에서 남은 active/보존 상태 거부와 독립 선택을 대조한다. 진입 분기를 다시 만들거나 명시적 계획 복구의 대상을 바꾸지 않는다.
- [ ] 같은 복구 경계에서 최신 `planning/selection_service.py`·`specification_review.py`·`storage/planning_review_state.py`의 목적 구분을 유지한다. Requirement 완료 점검의 보존 결과는 해당 판단·게시만 이어가고 프로젝트 일반 planning·최종 전문 확인 성공을 대신하지 않는다. 직접 구현된 완료 점검 우선순위·0 유지보수 주기·새 점검 호출 제거는 다시 구현하지 않는다.
- [ ] 실제 보완한 실패·보존·독립 선택과 재개 계약만 PRODUCT·ARCHITECTURE의 해당 절에 통합한다. R-082의 정상 전환과 다른 계획의 parser·수동 소유 보호·충돌 교정은 각 기존 담당 작업에 남긴다.
- [ ] R-055 B04/B05와 최신 R-082/R-091을 `selection_service.py:_gate_preserved_error()`·`_suspend_failed_planning()`의 실패 입력 판정에 연결한다. G-338이 제공하는 writer 종료·자원 반환·원인 복구 전이/교정 근거가 바뀌면 문서 지문이 같아도 현재 조건을 재검사한다. 무관한 runtime SHA·branch 전진은 해소로 삼지 않고 유효한 저장 결과/intent부터 재사용한다. 원인 해소와 새 Planner 호출 자격을 구분하며 미해소 오류·독립 작업 성공을 섞지 않는다. 정리 분리·복구 admission 자체는 G-337/G-338에서 재구현하지 않는다.

## Validation

- 관련 writer 종료·자원 반환·publication 확정/원인 복구 후 문서 지문이 그대로인 실패 planning을 재진입시킨다. 현재 조건 재검사는 수행하되 유효한 결과/intent가 있으면 Planner 호출은 0회이고, 실제 해소가 없으면 오류·미확인 closure를 보존해야 한다. G-337/G-338이 제공한 최종 해제 뒤만 독립 선택을 허용하고 잔여 정리의 존재만으로 재보류하지 않는다.
- Worker의 최소 targeted unit은 `tests/test_planning_selection.py`, `tests/test_runtime_state.py`, `tests/test_planning_error_recovery.py`의 변경한 실패/재개 case로 구성한다. 기존 `tests/support/planning_case.py`, `tests/support/execution.py`와 실제 SQLite transaction을 사용하고 Git·Agent·원격 관측만 mock한다.
- 영향받은 원래 작업과 복수 독립 READY를 함께 두고, 안전한 suspend 후 R-091 한도 안의 독립 선택·병행, 보류 작업/의존 closure의 미호출, 동일 오류의 Planner no-call과 오류·session·실패·원문 보존을 확인한다. 독립 작업의 verify·PR 대기와 실제 종료 뒤 planning 복구/원래 단계 재개에서도 원본·대표 투영과 세 종류의 자리 계산이 유지돼야 한다. `tests/test_task_scheduling.py`의 SQLite/제어된 호출 fixture를 재사용한다.
- 유효한 결과가 저장된 경우와 실제 교정이 남은 경우의 모델 호출 수를 구분한다. 단순 원격 SHA·코드·ExecPlan 전진, 재시작·객관적 gate 재검사만으로 새 점검/전체 재계획을 하지 않고, 필요한 교정만 기존 세션으로 이어져야 한다. Requirement 목적의 복구에서도 일반·최종 receipt와 현재 실행/유지보수 상태를 보존해야 한다.
- 종료 불명 writer·보존 안전을 확인할 수 없는 operation·미확정 공유 게시·수동 소유·dependency·활성 유지보수와 안전한 독립성 근거 부재에서는 해당 실행·자원을 넘기지 않는다. 소유권·writer 종료·보존을 확인한 rebase는 해당 worktree 예약을 남기며 위 독립성·한도를 통과한 다른 작업을 막지 않는다. 정상 planning 성공·변경 없음·명세 무변경 재개는 호출 수·순서와 실제 저장 row로 검증한다.
- `tests/test_planning_selection.py`의 `test_specification_planning_completes_before_parked_rebase_resumes`, `tests/test_runtime_state.py`의 checkpoint 보존과 `tests/test_task_scheduling.py`의 planning 배정 중단 중 Git 접근/hand-off 회귀를 재사용한다. 같은 fixture에서 실패 planning → 안전한 suspend → 독립 선택 → 결과 게시/DB 적용 → 원래 작업 복원에 빠진 연결만 추가한다. HEAD·index·원래 target·checkpoint·session·실패와 planning 오류가 보존되고, 다른 controlling operation·종료 불명·자원 경쟁은 계속 거부해야 한다. `execution/execution_state.py:recover_rebase_checkpoint()`와 `tests/test_execution_evidence_free_rebase_preparation.py`의 복구·원격 불일치·경쟁 거부 자체는 재구현하거나 조건별 복제하지 않는다.
- 기존 Git 진단 복구에서 보존 작업의 identity만으로 거부하지 않는다. `tests/test_planning_error_recovery.py`의 현재 ignored/config/current_tracked 해소와 결과 오류/ownership/race/독립 오류 보존을 재사용하며, 바뀐 후보의 결과 검증·완료 marker 불변을 연결한다. 사후 Git 권한 검사를 되살리지 않고 테스트 준비 비용 전환은 G-270에 남긴다. `tests/test_project_services.py:ProjectServiceCompositionTests.test_project_recovery_routes_to_planning_while_execution_is_paused`의 실제 SQLite 기반 planning 소유/실행 소유 분기와 request ID·local_environment_recheck 전달을 기존 근거로 재사용한다. 위 회귀와 새 `test_scheduled_planning_wait_still_dispatches_task_recovery`·`tests/test_task_scheduling.py:test_observed_specification_change_drains_before_followup_dispatch`를 기존 진입/명세 반영 근거로 재사용한다. 진입·drain만으로 suspend·독립 실행·원래 단계 재개를 충족했다고 판단하지 않으며 기존 fixture에서 남은 연결만 검증한다.
- `tests/test_planning_publication.py`, `tests/test_incremental_planning.py`, `tests/test_execution_checkpoint_recovery_phase.py`의 기존 연결 회귀로 push 후 DB 미적용·후속 base 전진·독립 실행 중 입력 변경·재개 transaction 중단을 확인한다. `test_applied_publication_recovers_after_cleanup_before_db_release`와 `test_applied_general_batch_retains_owner_and_rejects_stale_cleanup_release`의 실제 SQLite fixture를 소비해 분리 불가능한 release 대기의 suspend/독립 activation 거부, G-337의 잔여 정리 분리·최종 해제 뒤 원래 작업 복원의 최소 연결을 추가한다. 일반 intent가 없는 변경 없음 완료도 같은 보존 판단을 확인하고, 유지보수 restart의 cycle/intent 보존은 `tests/test_tiered_publication.py:test_published_source_drift_retains_intent_until_restart_cleanup_cas`를 재사용한다. 정리 실패로 이미 확정된 성공 기준을 rollback하거나 재호출·재적용하지 않으며 owner/catalog/membership 경쟁은 현재 변경을 보존해야 한다. 모의 결과를 실제 원격/OS 성공으로 기록하지 않고 새로운 전체 실행 반복을 추가하지 않는다.

## Progress

현재 revision의 정상 전환 회귀와 위 실패 gate·선택·activation 소비자를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.

- [ ] 보존 작업/실제 슬롯 구분과 오류·독립 선택 연결
- [ ] 적용된 publication release와 미게시 실패 suspend의 구분·최종 해제 후 복원 연결
- [ ] 동일 문서 지문의 실제 조건 재평가와 분리된 잔여 정리 이후 독립 선택 연결
- [ ] 중단·경쟁·오탐 복구의 targeted unit 검증

## Decision Log

- `9dd75b5d`의 국소 rebase 해제 판정은 직접 구현으로 소비한다. 최신 B04/B05의 공통 자원 판단·정리 분리·원인 복구는 G-338의 선행 closure로 연결하고 이 계획은 실패 입력 gate·suspend·이미 게시된 독립 작업 선택만 보완한다. 기존 적용 후 정리 설명은 안전하게 분리 가능한 경우까지 일괄 보류하지 않도록 교체했다. 원래 미확인 작업의 명세 반영·최종 CAS·오류/기록 보존은 유지한다.

- 현재 delta의 게시 준비/DB 적용·정리·최종 해제는 직접 구현으로 소비한다. 이 작업은 그 경계를 실패 planning의 suspend·독립 선택·원래 작업 복원에 연결하는 남은 범위만 보완한다. G-331 dependency와 기존 ID·목표·metadata·수행 기록을 유지하며 게시 엔진을 중복 계획하지 않는다. DB 적용 뒤 정리 오류가 성공 기준 rollback이나 독립 activation의 조기 허용으로 바뀌지 않게 기존 release CAS를 사용한다. 이번 정적 개정은 구현·검증 성공이나 프로젝트 최종 전문 확인이 아니다.

- `5903c5aa` → `eeb0ff79` delta와 snapshot token `7edcccd922df2c4ed63ee0fb272a2783c5b7c06898a7268b77f1450c157a4608`, 원본 hash `fe925925ce41d0a985d171d1092f08126bd0ae673e773c4196f50dea1ed80e4c`를 대조했다. 직접 구현된 rebase 보존 planning·checkpoint/소유권 복구와 프로젝트 Git 접근을 실패/suspend 소비 경계에도 유지한다. 기존의 포괄적인 소유 operation 거부 표현을 실제 보존·writer·공유 자원 조건으로 구체화했으며 미확인 영향 closure·같은 자원 쓰기·독립 실패 보호는 유지한다. ID·metadata·dependency·원래 목표·수행 기록을 보존하고 정상 구현을 소급 계획하거나 프로젝트 최종 전문 확인·검증 성공을 기록하지 않는다.

- 2026-10-16 delta: R-055/R-091이 프로젝트 단일 변경 실행을 대체하므로 기존 “실제 동시 변경 하나” 조건을 제거하고 같은 후보·공유 자원 배타와 영향 closure 보류를 보존했다. 직접 구현된 병렬 배정기는 재사용하며 G-297의 작업별 결과 처리를 선행 소비한다. 실패 planning의 보존·의미 확인·재개라는 원래 목표·기록과 high 분류는 유지한다. 과거 단일 슬롯 설명은 당시 구조의 조사 기록이다.

- `cdc2bc7bfa93afa5be1ae288b1060ad476d34dfa`의 project service 로컬 재검사 전달과 회귀는 직접 구현으로 소비한다. 이 작업의 잔여 목표는 실패 planning의 실행 자리 해제·독립 선택·복원이며 진입 분기 교정을 소급 계획하거나 전체 재개 성공으로 기록하지 않는다. 기존 ID·metadata·dependency·진행 기록과 명시적 계획 복구·writer/게시 보호를 보존한다.

- 2026-10-15 delta: 직접 구현된 R-082 호출 축소·Requirement 목적 receipt 분리를 실패 복구에도 적용하도록 잔여 작업을 정합화했다. 복구 자격 재관측과 모델 재호출을 구분하며 기존 슬롯 분리 목표·metadata·dependency·수행 기록을 보존한다. 구현·테스트·verify는 실행하지 않았다.
- 현재 R-055의 진단 해소와 결과 유효성 검증을 분리한다. 이 작업은 보존된 재개 identity와 실제 슬롯의 연결만 보완하며 이미 구현된 Git 진단 해소·권한 검사 제거를 재구현하지 않는다. 기존 high complexity/risk·목표·의존성·기록과 독립 실패를 보존한다.
- complexity high: 원래 작업의 실행 원본, 실패 planning의 별도 보존, 독립 작업의 실제 슬롯과 게시 후 복원을 중단·CAS 경쟁에도 일관되게 연결하는 판단이 남는다. 정상 성공 경로의 소급 구현 계획은 만들지 않는다.
- risk high: 잘못된 슬롯 해제·재투영은 미확인 조건의 작업 실행이나 같은 자원의 중복 writer를 만들고 수동 복구를 요구한다. 종료/소유/게시 근거·원문·실행 이력을 유지하고 transaction 경쟁과 보류 closure를 검증한다. 실제 Git 결과를 코드 rollback으로 복구한다고 가정하지 않는다.
- priority high: 동일 planning 실패가 검증된 독립 자동 작업까지 중단시키는 잔여 경로를 제거한다. 기존 R-055의 안전한 실행 자리 해제와 R-082의 같은 작업 재개 범위이며 새로운 병렬 실행·사용자 정책을 도입하지 않는다.
