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
priority: normal
---

## Purpose

G-260에 반영된 취소 후 cycle 종료를 재사용하고, 남은 Merge/취소 혼합·legacy 조건의 회귀와 종료 로그의 사실 구분을 완성한다. 현재 `MaintenanceRepository.get()`의 취소 범위 투영, `cycle_plan_status_error()`·`_advance_maintenance_cycle_after_merges()`·`finish_cycle()`은 확정 취소의 빈 범위와 실제 Merge된 잔여 범위를 종료한다. 종료 엔진·취소 저장 형식·주기 정책을 다시 구현하지 않는다.

현재 `planning/selection_service.py`는 취소만으로 종료해도 “유지보수 마지막 Merge 확인”을 출력한다. 실제 Merge·확정 취소·현재 남은 범위와 다음 주기 기준을 구별하는 로그와, 기존 단일 전체 취소 회귀가 보장하지 않는 조건의 최소 연결 검증이 이 작업의 잔여 범위다.

## Acceptance Criteria

- R-074/R-055: 현재 구현의 전체 취소·실제 Merge/취소 혼합 종료, 필요한 dependency 대기, prerequisite·refactoring·post 단계와 확정 prerequisite이 있는 legacy assessment의 종료를 검증한다. 추가 Planner/평가·사후 재검증·사용자 판단 없이 실제 남은 범위가 끝난 뒤 일반 READY 선택으로 복귀한다. 취소 자체를 MERGED나 평가 성공으로 기록하지 않는다.
- 취소 근거 없는 row 부재·다른 repository의 ID·취소 표식과 현재 row의 공존·미확정 legacy 평가·미Merge 잔여 dependency·관련 catalog hold·게시 또는 workspace 반환 대기는 종료를 허용하지 않는다. 취소 근거/관계 반영과 현재 cycle·catalog의 CAS 및 rollback을 유지한다. 별도 물리 자원 정리 pending은 유효한 취소를 되돌리는 새 종료 조건이 아니다.
- 정상 종료의 실제 MERGED 수·catalog revision, 원래 평가 receipt·실패·취소 감사와 별도 새 유지보수 요청을 보존한다. 같은 cycle의 재관측·재시작은 종료/주기 기준을 반복 전진시키거나 평가를 다시 시작하지 않는다. 0 설정·주기 도달·새 명시 요청의 기존 정책은 유지한다.
- R-053/R-074: 종료 로그는 실제 Merge와 확정 취소를 구분하고 남은 범위 소진·다음 주기 기준·일반 작업 복귀를 설명한다. 전부 취소된 cycle에서 실제 마지막 Merge가 관측됐다고 출력하지 않는다. 정상 내부 종료/자동 복귀의 Telegram을 추가하거나 프로젝트 최종 완료로 표시하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/maintenance/cycle_service.py:_advance_maintenance_cycle_after_merges()`·`maintenance_orchestration.py:cycle_plan_status_error()`/`cycle_scope_plan_ids()`와 `storage/maintenance_state.py:get()`/`finish_cycle()`의 구현을 재사용한다. `cancelled_scope_plan_ids`·`legacy_prerequisite_wait`, 실제 MERGED 집계·관련 hold·게시/반환·expected 비교와 canonical cycle 반환을 새 원본이나 별도 취소 엔진으로 대체하지 않는다. 테스트에서 현재 명세의 실제 결함을 확인한 경우에만 그 경계의 필요한 국소 교정을 수행한다.
- [ ] 확인한 `tests/test_maintenance_cycle.py:SinglePassMaintenanceCycleTest`, `tests/test_maintenance_cycle_scheduling.py`와 `tests/test_deleted_plan_cleanup.py:test_last_cancelled_cycle_link_survives_until_verified_completion_before_erasure`의 실제 SQLite fixture를 재사용한다. 현재 단일 refactoring 전체 취소→종료→최종 제거 회귀는 그대로 소비하며, Merge/취소 혼합·필요 dependency·legacy 취소/hold·종료 CAS의 남은 조건만 추가한다. 새 case는 추가 예정이며 기존 case의 실행 성공으로 기록하지 않는다. 종료 뒤 별도 교정 소진·게시 lifecycle을 반복하지 않는다.
- [ ] 확인한 `loop_manager_internal/planning/selection_service.py`의 `previous_phase`와 IDLE 전이 로그를 기존 cycle/상태 관측으로 정합화한다. 이전 확정 범위·검증된 취소 투영·실제 잔여 Merge와 반환 cycle의 기준값만 사용한다. 새 DB 필드·삭제/평가 성공 원본·추가 Git/Agent 호출 없이 기존 events 경계를 재사용하며, 취소/종료 source를 확인할 수 없으면 미확인 사실을 보존한다.
- [ ] 기존 selection/상태 fixture에서 사실별 종료 로그와 내부 종료 알림 0건을 확인한다. PRODUCT·ARCHITECTURE의 현재 취소 종료 보장은 이미 반영돼 있으므로 중복 정책 설명·미구현 기능을 추가하지 않고 필요한 경우 해당 로그 설명만 정합화한다.

## Validation

- Worker는 변경한 cycle/selection의 최소 unit case를 실행한다. 실제 SQLite 취소 적용에서 남은 실제 Merge와 dependency를 처리해 종료→일반 READY 선택까지 연결한 혼합 사례 하나를 둔다. 추가 평가·Planner 호출 0회, 취소 ID의 MERGED 생성 0건과 실제 프로젝트 MERGED 기준값·원래 평가 근거 보존을 검사한다. Git/PR 관측만 double로 제공한다.
- legacy 취소/hold·미확정 평가와 게시/반환 대기·취소/실행 row 공존·다른 repository·남은 대체 dependency를 기존 fixture의 필요한 경계에서 검사한다. 같은 조건의 모든 단계 조합을 반복하지 않는다. `finish_cycle` 판정과 확정 사이 cycle/취소 범위·catalog 변경 또는 transaction 중단은 조기 종료를 거부하고 기존 자료를 보존해야 한다.
- 기존 `SinglePassMaintenanceCycleTest`의 실제 Merge 종료·rollback·새 요청 보존·다음 주기·평가 receipt 불변 회귀를 재사용한다. 이미 있는 전체 취소→최종 제거와 공유 평가 부분 제거 테스트를 복제하거나 skip/삭제·assertion 완화하지 않는다. 구현된 일반 종료/주기 엔진의 소급 검증 성공을 만들지 않는다.
- 전체 취소의 종료 로그에는 실제 마지막 Merge 관측이라는 문구가 없어야 하고, 혼합/전체 Merge도 각 확인된 사실·남은 작업 없음·실제 다음 기준을 구별해야 한다. 정상 종료·복귀의 독립 Telegram 0건, 기존 사용자 질문/PR 사건과 취소/전송 감사 보존을 확인한다.

## Progress

- [x] `44931a876fe0901d138946be7268840685e4489b`의 PRODUCT·ARCHITECTURE·활성 Requirement 전문을 읽고 R-074/R-055의 취소 후 정상 종료 목표를 현재 cycle 서비스·저장소·선택 및 관련 테스트와 정적으로 대조했다. 활성 row 삭제/감사는 직접 구현된 것으로 제외했다.
- [x] 비종료 catalog와 G-226/G-260/G-261/G-189의 본문을 대조했다. G-226 보존 후보는 읽기 전용으로 확인했으며 유지보수 통지 제거와 기존 상태 보존을 소비한다. 후보의 기록을 새 verify/Review 성공으로 판단하지 않았다.
- [x] revision `249477cd835923fa7923e92e55482efdc4ddce11`에서 G-260의 실제 Merge·archive와 취소 범위/legacy 투영·종료 transaction·canonical 반환 및 단일 전체 취소 회귀를 정적으로 확인했다. 과거 엔진 보완 목표는 직접 구현으로 소비하며 이번 Planner는 테스트·전체 verify를 실행하지 않았다.
- [ ] 남은 혼합/legacy·guard·CAS의 최소 회귀와 사실별 종료 로그 보완
- [ ] 변경 관련 targeted unit 검증

## Decision Log

- snapshot token `22ad530edef08485a2e956e3db5a8749e250ce80bf27d9743f680c72df20a993`의 원본 hash와 미착수 ID·경로·sources·dependency를 확인했다. G-260의 현재 코드와 `test_last_cancelled_cycle_link_survives_until_verified_completion_before_erasure`가 전체 취소 후 종료·실제 Merge 기준·평가 근거 보존을 이미 구현/검사하므로 재계획하지 않는다. 단순 MERGED 상태만으로 R-074 전체 충족을 확정하지 않고 이 계획의 남은 검증·운영 로그만 보존한다.
- 원래 전수 조건은 현재 core의 성공/거부 계약으로 유지한다. 새 테스트는 현재 단일 전체 취소 회귀와 일반 Merge/legacy 회귀 사이의 혼합·취소 근거 연결을 맡는다. 새로운 정책 선택이나 기존 취소·종료 엔진을 도입하지 않는다. G-260의 과거 본문에 남은 사용자 판단 제안은 현재 R-074의 확정된 종료 목표를 대체하지 않으며 보관 원문은 수정하지 않는다.
- complexity medium: 실제 취소 적용·dependency closure·legacy/hold·종료 CAS와 선택 로그의 경계별 근거를 연결한다. risk medium: 회귀 누락은 미완료 장벽을 풀거나 다음 주기를 왜곡할 수 있어 실제 저장 결과·거부/rollback·현재 원문을 유지한다. priority normal: 정상 종료 엔진의 진행 차단은 직접 구현으로 해소됐고 잔여 범위는 필수 회귀와 사실에 맞는 운영 안내다. Model·Effort는 Manager 설정을 따른다.
- unittest adapter version 1·launcher Python과 Python 3.11–3.14·독립 모듈·필수 compile/runner 통합 계약을 유지한다. 이 개정은 현재 후보의 구현·검증 성공이나 프로젝트 최종 전문 확인·완료 판정이 아니다.
