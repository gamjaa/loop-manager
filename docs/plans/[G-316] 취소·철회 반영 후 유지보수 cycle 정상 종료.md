---
id: G-316
title: 취소·철회 반영 후 유지보수 cycle 정상 종료
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
depends_on:
  - G-226
  - G-260
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: medium
risk: medium
priority: high
---

## Purpose

유지보수에서 확정한 대상이 모두 안전하게 취소·철회됐거나 일부 실제 Merge와 나머지 취소로 남은 일이 없어지면, 추가 평가 없이 cycle을 정상 종료하고 일반 작업 선택으로 돌아간다. 종료 시 확인한 실제 MERGED 수를 다음 자동 주기의 기준으로 저장하며, 취소를 Merge·구현·검증·평가 성공으로 기록하지 않는다.

현재 `cycle_plan_status_error()`·`cycle_scope_plan_ids()`는 확정 대상의 row가 없어지면 오류를 반환하고, `finish_cycle()`은 비어 있지 않은 범위 전체의 MERGED만 허용한다. 직접 구현된 취소 증명·활성 row 제거·감사와 G-260의 후속 요청/관계 반영을 소비해 이 종료 판정만 보완한다. 처음부터 계획이 없는 평가와 실제 전체 Merge 뒤의 기존 종료 경로는 재사용한다.

## Acceptance Criteria

- R-074: prerequisite·리팩터링·평가 발견에서 확정한 대상의 안전한 취소와 게시·등록·관계 반영이 확인되고, 남은 대상과 필요한 dependency가 없으면 정상 종료한다. 전체 취소와 실제 Merge/취소 혼합에 같은 기준을 적용하며, 목표 재확인 Planner·사후 재검증·새 평가·추가 사용자 판단을 종료 조건으로 만들지 않는다.
- 대상 부재는 기존 R-055 취소 감사와 채택된 원격 삭제/철회 근거로 설명돼야 한다. 문서·row 부재, 조회 실패, 미완료 삭제 판단, 다른 repository/ID의 감사, ID 재등장이나 불확실한 과거 이력을 취소 완료로 해석하지 않는다. 확정 범위와 취소 전 선언·관계·원문·실행/실패 이력을 보존하며, 대상 목록을 비워 정상 종료 근거를 만들지 않는다. 유효하게 확정한 취소 근거에는 이후의 정상 catalog 전진만으로 삭제 revision과 최신 HEAD의 일치를 다시 요구하지 않는다.
- 현재 남은 목표가 참조하는 필요한 dependency는 실제 Merge까지 유지한다. 취소된 선행이 아직 유지 목표에 참조되면 그 목표를 충족한 것으로 보지 않고, 기존 참조 복구·대체 계획 게시와 실제 Merge를 기다린다. 취소된 목표만을 위해 필요했던 선행을 이번 범위에서 제외할 때도 기존 관계 반영의 근거를 소비하며 선행 작업 자체를 취소하거나 일반 dependency를 지우지 않는다.
- 필요한 미완료 대상·관련 catalog hold·종료 범위/관계 반영에 필요한 미확정 일반/유지보수 게시·미완료 workspace 반환·실제 공유 안전 차단이 있으면 cycle과 원인을 보존한다. 취소의 실행/복구 소유권·요청 무효화는 G-260의 확정 결과를 사용하고 종료 판정에서 대신 수행하지 않는다. 별도로 보존된 물리 자원 정리 pending만으로 유효한 취소를 되돌리거나 불필요한 종료 차단을 추가하지 않는다.
- 종료는 현재 cycle revision·채택 catalog·취소/관계 근거를 다시 확인한 짧은 transaction에서 원자적으로 적용한다. 판정 뒤 대상·dependency·hold·게시 상태가 바뀌면 낡은 종료를 확정하지 않는다. 외부 Git/PR 관측 중 DB 쓰기 잠금을 유지하지 않으며, 중단 뒤 재개해도 중복 종료·주기 기준의 반복 전진·확정 평가의 재실행이 없다.
- 모든 정상 종료는 종료 시 확인한 실제 MERGED 수와 원격 catalog revision을 다음 주기 기준에 저장한다. 전체 취소도 같은 기준을 적용하고 취소 개수를 Merge 수에 더하지 않는다. 종료 직후와 재시작 뒤에는 새 명시적 요청이나 새 Merge의 주기 도달 없이 같은 due cycle을 다시 시작하지 않는다. 활성 cycle 도중 들어온 별도의 새 요청은 기존 수락 규칙대로 보존한다.
- 기존 prerequisite 대기·리팩터링·사후 재검증과 prerequisite 확정 뒤의 legacy 평가 단계도 확인 가능한 같은 범위·게시 근거로 종료한다. 확정 범위를 알 수 없는 legacy 상태, 실제 프로젝트 차단·실패·미Merge PR을 작업 없음으로 바꾸지 않는다. 처음부터 계획이 없는 평가의 정상 확정과 현재 전체 Merge 종료는 계속 작동한다.
- cycle 종료로 과거 정밀 평가 성공 원본·receipt를 최신 코드에 옮기지 않는다. 일반 READY의 기존 dependency·우선순위·안전 조건을 지키며 다음 선택으로 복귀하고, 작업이 없으면 기존 대기/완료 판단을 따른다. 유지보수 종료를 프로젝트 최종 완료로 기록하지 않는다.
- 운영 로그에는 원래 확정 범위, 실제 Merge/확정 취소의 구분, 남은 작업 없음의 근거, 일반 작업 복귀와 다음 주기 기준을 남긴다. G-226의 현재 사용자 행동 중심 정책을 소비해 정상 종료·자동 복귀 Telegram을 생성하지 않으며 취소/전송 감사와 독립 사건은 보존한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/maintenance/maintenance_orchestration.py`의 `cycle_plan_status_error()`·`cycle_scope_plan_ids()`·`dependency_plan_ids()`와 cycle READY 선택에 같은 확정 범위 해석을 연결한다. 현재 대상/필요한 dependency와 근거 있는 취소·미확인 부재를 구분하며, 실제 Merge를 뜻하는 `all_cycle_plans_merged()`의 의미를 취소까지 넓히지 않는다. G-260이 보존한 취소 전 범위·관계 자료를 소비하고 별도의 취소 상태 엔진은 추가하지 않는다.
- [ ] 확인한 `loop_manager_internal/storage/planning_publication_state.py:apply_mutable_catalog_changes()`의 취소 감사와 `storage/execplan_catalog_state.py`의 `DefinitionStateRepository.capture()`·현재 catalog/게시 확인 경계를 재사용한다. `execplan_cancellations`의 원문·선언 identity·삭제 revision·실행 보존 근거와 관계 반영이 현재 cycle 대상에 유효한지 확인할 최소 조회를 소유 모듈에 둔다. 원격 삭제 증명·취소 transaction·요청/claim 무효화는 재구현하지 않으며 과거 취소 ID를 새 READY나 MERGED row로 등록하지 않는다.
- [ ] 확인한 `loop_manager_internal/storage/maintenance_state.py`의 `MaintenanceRepository.plan_rows()`·`finish_cycle()`·`_verified_merged_count()`·`advance_success_baseline()`과 `loop_manager_internal/maintenance/cycle_service.py:_advance_maintenance_cycle_after_merges()`를 대조해 근거 있는 남은 작업 없음도 종료하도록 연결한다. transaction에서 현재 범위·관계·관련 hold·게시·legacy workspace 반환을 재확인하고, 실제 MERGED 집계와 cycle 종료 기준만 갱신한다. 평가 receipt·과거 성공 원본·새 요청은 기존 소유 경계에서 보존한다.
- [ ] 확인한 `loop_manager_internal/planning/state_ports.py`의 `advance_maintenance_cycle()`·`schedule()`과 `loop_manager_internal/catalog/project_selection.py:catalog_schedule_decision()`에서 취소 대상 부재가 종료 전 오류나 종료 후 재평가로 되돌아가지 않게 연결한다. 필요한 경우 `loop_manager_internal/maintenance/bindings.py`의 명명된 effect를 같은 계약으로 조립한다. G-226의 유지보수 통지 제거를 소비해 상태/로그만 정상 종료에 연결하고 일반 선택·priority 정책은 바꾸지 않는다.
- [ ] 아래 기존 테스트 fixture와 현재 Merge/계획 없음/주기/중단 회귀를 재사용해 취소 종료의 빠진 조건만 보강한다. PRODUCT·ARCHITECTURE의 유지보수 종료·다음 주기·평가 성공 구분을 구현된 보장으로 통합하며, Requirement·다른 ExecPlan·선택 ExecPlan frontmatter는 수정하지 않는다.

## Validation

- Worker는 `tests/test_maintenance_cycle.py`의 `SinglePassMaintenanceCycleTest`와 `tests/test_maintenance_cycle_scheduling.py`에서 변경 관련 최소 unit case를 실행한다. `test_prerequisite_and_final_merge_close_without_new_assessment_or_receipt`·`test_required_dependency_closure_runs_before_refactor_and_preserves_barrier`·`test_empty_file_selection_finishes_without_assessment_agent`의 기존 assertion을 유지하고, 전체 취소와 실제 Merge/취소 혼합의 종료 → 일반 READY 선택에서 추가 평가/Planner 호출 0과 실제 MERGED 기준값을 확인한다.
- `tests/test_incremental_planning.py`의 `test_remote_deletion_cancels_unstarted_and_registers_independent_gap_once`·`test_proven_finished_writer_cancellation_preserves_started_history`의 실제 SQLite 취소/감사 fixture를 재사용해 최소 한 사례를 실제 취소 적용에서 cycle 종료까지 연결한다. Git/PR 관측만 mock하며, 나머지 조건 조합을 실제 Git/CLI로 반복하거나 취소 helper 전체를 성공 상수로 대체하지 않는다. G-260의 요청/소유권 무효화 회귀는 소비 근거로 연결한다.
- 취소 증명 없는 부재·불일치/재등장·미확정 게시·관련 hold·실제 writer/요청 소유권이 남으면 종료하지 않아야 한다. 일부 살아 있는 목표가 취소 선행을 계속 참조하는 사례에서 가짜 dependency 충족이 없어야 하며, 유효한 관계 반영 뒤 남은 대체 선행의 실제 Merge까지 기다린다. 취소된 목표만의 옛 dependency를 범위에서 제외해도 그 작업의 일반 상태와 참조는 보존한다.
- `tests/test_maintenance_cycle.py`의 `test_legacy_post_phase_closes_only_after_merge_and_publication_confirmation`·`test_legacy_assessment_after_prerequisite_merge_does_not_restart_evaluation`를 확장해 legacy 취소/혼합 종료를 확인한다. 판정 뒤 cycle revision·catalog·취소 근거·관계/hold 변경과 transaction rollback·재시작에서 조기/중복 종료가 없고 보존한 원문·감사·평가 receipt·실패 이력이 유지돼야 한다. 이미 정상 종료된 cycle의 재처리는 기준값을 다시 전진시키지 않는다.
- `tests/test_maintenance_cycle.py`의 `test_interval_uses_end_count_and_requests_are_bound_once_across_restart`·`test_new_request_during_active_cycle_is_preserved_for_next_cycle`와 `tests/test_maintenance_state.py`의 기존 주기·성공 기준 회귀를 재사용한다. 취소 종료 시 실제 MERGED가 N이면 설정 20에서 N+19까지 자동 재평가하지 않고 N+20에 기존 안전 조건으로 전환하며, 0·새 명시적 요청·남은 작업 없음의 기준 보존과 평가 성공 미합성을 확인한다. 같은 주기 조합의 중복 테스트는 추가하지 않는다.
- 일반 작업 복귀에서는 대상 취소/유지보수 로그와 상태를 확인하고 G-226의 실제 outbox 정책을 연결해 정상 종료·복귀 알림 0, 독립 사용자 질문/PR 사건 보존을 검사한다. 기존 실제 Merge·PR 조회 실패/Close·미확정 게시의 회귀를 삭제하거나 assertion을 약화하지 않는다.

## Progress

- [x] `44931a876fe0901d138946be7268840685e4489b`의 PRODUCT·ARCHITECTURE·활성 Requirement 전문을 읽고 R-074/R-055의 취소 후 정상 종료 목표를 현재 cycle 서비스·저장소·선택 및 관련 테스트와 정적으로 대조했다. 활성 row 삭제/감사는 직접 구현된 것으로 제외했다.
- [x] 비종료 catalog와 G-226/G-260/G-261/G-189의 본문을 대조했다. G-226 보존 후보는 읽기 전용으로 확인했으며 유지보수 통지 제거와 기존 상태 보존을 소비한다. 후보의 기록을 새 verify/Review 성공으로 판단하지 않았다.
- [ ] 취소 증거·현재 필요 범위를 소비하는 cycle 종료와 실제 Merge 기준 보존
- [ ] 최소 targeted 회귀·중단/경쟁·일반 선택 연결 및 관련 설계 문서 통합

## Decision Log

- 일반 full planning에서 식별한 미충족 목표다. 현재 `cycle_plan_status_error()`는 대상 row 부재를 오류로 보고 `finish_cycle()`은 비어 있지 않은 전체 MERGED만 허용한다. R-074가 명시한 전체 취소/혼합 종료와 모든 정상 종료의 실제 Merge 기준 갱신을 별도 변경 단위로 다룬다. 이 정적 대조는 구현·테스트·전체 verify 성공이나 프로젝트 최종 전문 확인의 완료 선언이 아니다.
- G-260은 취소 확정 뒤 활성 실행·요청·claim·일반/유지보수 관계의 현재 수명을, G-261은 별도 소유 자원의 물리 정리를 맡는다. 이 계획은 G-260의 확인된 결과로 cycle 종료·다음 주기·일반 선택만 완성한다. G-260의 기존 보류와 실제 Merge dependency는 유지하고, 물리 cleanup pending을 별도 선행 완료 조건으로 확대하지 않는다. G-226 dependency는 현재 유지보수 통지 제거를 재사용하기 위한 것이며 기존 후보·수행 기록·검증 책임을 바꾸지 않는다.
- 처음부터 작업이 없는 평가·전체 실제 Merge 뒤 종료·최초 일반 모드·0 설정·주기 기준 자체는 이미 구현된 범위로 재계획하지 않는다. 새로 필요한 취소/혼합 종료의 회귀를 이 경로들과 연결하며, R-055의 삭제·참조 복구 및 G-189의 후속 전수 감사와 중복 구현하지 않는다.
- complexity medium: 기존 취소 감사·확정 cycle·실제 MERGED 집계를 재사용하고, 범위 판정과 종료 transaction 및 선택 소비의 명확한 경계를 보완한다. risk medium: 잘못된 종료는 미충족 선행의 유지보수 장벽을 풀거나 다음 자동 주기를 왜곡한다. 현재 선언/관계·게시·hold 재확인과 CAS/rollback·재시작 회귀로 방어하며 불확실한 근거는 원래 상태로 보존한다. priority high는 정상 취소 뒤에도 cycle이 끝나지 않아 일반 작업 복귀를 막는 문제에 따른다. 최초 level은 3이며 Model·Effort는 Manager 설정을 따른다.
- `scripts/verify.json`의 unittest adapter version 1·launcher Python과 `docs/VERIFY.md`의 Python 3.11–3.14·독립 모듈·필수 compile 계약을 유지한다. 검증 adapter나 실행 환경 개편은 이 목표에 필요하지 않다. Planner는 구현·테스트·전체 verify를 실행하지 않았다.
