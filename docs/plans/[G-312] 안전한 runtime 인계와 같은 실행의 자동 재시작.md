---
id: G-312
title: 안전한 runtime 인계와 같은 실행의 자동 재시작
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-056] 원격 기준을 유지하는 비차단 로컬 base 동기화.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-236
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: medium
risk: high
priority: high
---

## Purpose

직접 구현된 runtime 관측 gate와 동일 실행의 자동 재시작을 재사용해 PR callback의 중복 원격 관측과 인계 중 반환·저장의 잔여 연결을 정리하고, G-236의 pause를 부모 재선택/재시작에 통합한다. 전체 pause는 새 bootstrap 원격 효과와 runtime 시작을 막고 프로젝트 pause는 해당 대상만 제외한다. 최초 즉시·성공 관측 뒤 30초 TTL과 확정 종료 결정은 기존 공통 gate를 사용한다.

#293의 `runtime/refresh.py`·bootstrap은 소유 receipt와 부모 lock을 유지해 최신 원격을 재선택하며 논리 run ID를 이어간다. 현재 `runtime/handoff_gate.py`에는 성공 시각·관측 lock·종료 요청과 launch gate가 있고, `application/scheduler.py`는 모든 프로젝트에 같은 gate를 전달한다. `wait_retry()`·`wait_usage()`·`wait_existing_call()`도 원격을 비교하지 않는다. 이 구현은 반복하지 않는다. 다만 `application/project_service.py`의 PR `after_observation`은 `self_runtime.py:_handoff_observed_self_merge()`에서 `_finish_self_merge_handoff()`를 직접 호출해 TTL·확정 종료 결정 재사용을 우회할 수 있다. 이 callback의 관측/상태 반영을 분리하는 보완은 기존 관측 경계 목표에 남긴다.

`refresh.paused()`와 시작 전 pause hook, 같은 context의 pause DB 대기 재진입을 억제하는 `RuntimeHandoffGate.check()`를 재사용한다. bootstrap의 hot journal 한정 복구도 직접 구현된 시작 경계로 소비한다. G-236의 pause 저장/gate는 해당 작업의 실제 Merge 뒤 부모 경계와 대조하며, 이 문서 개정이나 등록으로 G-236의 보류·Merge gate를 해제하지 않는다. 작업별 admission·재배정의 잔여 관측 위치와 대기 해소 뒤 호출 직전 검사는 G-297, 단계별 비차단 복구는 G-313이 맡는다.

## Acceptance Criteria

- G-236의 전체 pause가 자식 반환 직전·부모 재선택 전·새 자식 시작 직전에 접수돼도 이후 새 원격/bootstrap/runtime 효과가 없다. 인계 receipt·원래 단계·session·대기/실패 이력은 남고 resume 명령은 실행·독립 차단 해제를 하지 않는다.
- 프로젝트 pause는 같은 범위의 새 자동 처리를 막으며 다른 프로젝트와 읽기/제어 명령·수동 준비의 기존 계약은 유지한다. 전체 pause와 프로젝트 pause를 같은 플래그로 합치지 않는다. pause DB를 기다리는 중 같은 context의 pause 확인을 재귀 호출하지 않으며, 이 억제가 실제 pause·확정된 runtime 종료 결정·DB 대기 제한을 무시하는 조건으로 확대되지 않는다.
- 현재 전체 gate가 시작한 호출의 반환·종료/저장을 확인한 뒤만 부모에 인계한다. 종료 불명/미확정 저장·게시에서 성공 handoff나 새 호출을 만들지 않는다. 사용자 pause/일반 실패와 소유 runtime refresh 결과를 구별한다.
- 기존 source/repository/base·실행 SHA/identity·one-use receipt·부모 process lock·config/interpreter/PATH/인자·논리 실행 identity를 보존한다. 실제 최신 원격 재선택과 코드 동일 재시작 반복 방지, 물리 호출 reservation·checkpoint·실패/교정/재시도 예산을 초기화하지 않는다.
- `runtime_inputs.changed()`의 실제 실행 SHA 기준 코드/문서 판정과 PR 독립 관측·부모 인계/재시작은 현재 구현을 소비한다. PR Merge 관측 callback은 별도 fetch·입력 비교로 공통 TTL이나 확정 종료 결정을 우회하지 않는다. 실제 PR 상태·cleanup/작업 완료 기록은 기존 수명주기로 반영하고 다음 실제 단계 진입에서 기존 gate를 소비한다. pause 통합은 로컬 source·Git/index를 바꾸거나 verify/Review 성공을 합성하지 않는다.
- 등록 self runtime의 최초 실제 단계 진입에서는 즉시 원격 관측하고, 이후에는 마지막 **성공 관측**으로부터 `time.monotonic()` 기준 30초 미경과 시 fetch·코드 입력 비교를 생략한다. 30초 경과 자체는 관측을 실행하지 않으며 다음 실제 단계 진입에서만 검사한다. 단계 종료·단순 결과 정리는 중복 관측 대상이 아니다. 전체 등록 프로젝트가 하나의 TTL과 검사 결정을 공유하고 동시 진입에서도 원격 관측을 중복 수행하지 않는다.
- retry/usage/Agent·verify 순번·기존 호출 종료·Git 자원 대기에서는 원격 비교를 수행하지 않는다. 사용자 pause·취소 및 이미 확정된 handoff 중단 신호는 TTL·대기와 관계없이 처리한다. 원격 fetch·identity·diff 실패는 성공 관측으로 캐싱하지 않고 기존 보존/재시도 계약을 따른다.
- 검증된 코드 변경을 처음 확인하면 현재 runtime의 인계 요청·종료 필요 결정을 메모리에 고정해 이후 원격 조회·비교·반복 로그를 생략한다. 새 관리 편입·Agent·verify·후속 단계는 시작하지 않되 이미 시작된 물리 호출·결과·checkpoint·필수 정리와 저장은 안전하게 마치도록 허용한다. 대기 중 호출을 억지로 완료시키거나 종료 불명 자원을 회수하지 않는다. 모든 시작된 호출의 종료/저장이 확인된 뒤 기존 one-use receipt·부모 lock·새 runtime 자동 시작으로 인계하고 주기 실행은 기다리지 않는다.

## Implementation

- [ ] 확인한 `application/project_service.py:_synchronize_pull_requests()`와 `application/self_runtime.py:_handoff_observed_self_merge()`의 직접 관측을 실제 PR 상태 반영과 구분한다. 기존 `runtime/handoff_gate.py:observe_runtime()`·`RuntimeHandoffGate.observe()` 및 scheduler의 공통 observer가 성공 TTL·동시 관측·확정 종료 결정을 맡게 하며, callback 자체를 새 runtime 관측 시점으로 유지하지 않는다. callback의 다른 호출자와 PR 완료 저장 순서를 확인하고 기존 PR 동기화·source 부수 동기화의 의미는 보존한다. `runtime_inputs.py:changed()`·원격 diff·최초 preflight·TTL/캐시 엔진은 다시 만들지 않는다.
- [ ] 확인한 `RuntimeHandoffGate.check()`·`_handoff_popen()`·`application/scheduler.py`의 launch/drain과 `planning/planning_runtime.py`·`application/project_service.py`·`project_runner.py`의 단계 진입을 대조한다. 변경 확정 뒤 새 호출은 거부하면서 이미 시작한 호출의 종료 수집·checkpoint·필수 저장은 완료되는지 확인하고 누락된 인계 소비만 교정한다. 기존 세 wait helper의 원격 미조회·pause/취소/캐시된 종료 확인은 유지한다. G-297의 admission·재등록 관측 위치·대기 해소 뒤 호출 gate와 G-313의 단계별 실패/예산 처리는 이 작업에서 중복 구현하지 않는다.
- [ ] G-236 실제 Merge 뒤 확인한 `loop_manager_internal/runtime/refresh.py:paused()`·`runtime/bootstrap.py`의 부모 pause 검사와 `runtime/pause.py`의 영속 요청/실행 scope를 대조해 같은 저장 의미·대상/중단 결과를 사용하게 한다. 기존 호환 조회와 `runtime/handoff_gate.py`의 pause hook·context별 재진입 억제를 재사용하고 두 pause 엔진·새 DB/daemon을 만들지 않는다. self base 조회의 정상 읽기 전용·hot journal만 기존 DB 쓰기 재조회·`create=False`와 다른 오류 보존도 그대로 소비하며 부모 pause 통합을 이유로 DB 초기화·migration이나 journal 삭제를 추가하지 않는다.
- [ ] 확인한 `loop_manager.py:main()`·`runtime/bootstrap_adapter.py`·`application/scheduler.py`의 소유 인계/부모 loop와 실제 자식 시작 사이 경합을 연결한다. pause 뒤 새 외부 효과가 시작되지 않게 하되 미확정 자식/receipt는 보존한다. G-236의 단순 gate/자원 대기 중 pause 회귀를 반복하지 않고 최신 원격 재선택/자동 재시작의 남은 연결만 보완한다.
- [ ] 확인한 `tests/test_bootstrap_entry.py`·`test_bootstrap_runtime.py`·G-236의 `test_pause.py` fixture를 재사용한다. PRODUCT·ARCHITECTURE의 기존 runtime/pause 소유 절에 실제 연결 조건만 통합한다. 부모 lock·one-use receipt·논리 실행/실패 이력 구현 자체는 직접 반영된 코드를 유지한다.

## Validation

- `tests/test_runtime_refresh.py:RuntimeObservationTests`의 성공 뒤 30초·실패 미캐시·동시 진입 1회·기존 transaction 경계 및 `test_pause_database_wait_does_not_reenter_pause_check`를 재사용한다. pause DB 경합 뒤 실제 pause/계속 진행을 구별하고 중첩 확인에서도 확정 종료 결정이 적용되는 근거를 유지한다. bootstrap의 hot journal 복구·다른 readonly 오류 미재시도·DB 삭제 시 미생성은 `tests/test_bootstrap_entry.py`의 기존 실제 SQLite 회귀를 소비하며 새 조합별 자식 실행을 추가하지 않는다. 최초/문서 전용/누적 코드 전진·PR 독립 판정은 `tests/test_registered_project_scheduler.py`·`tests/test_self_merge_handoff.py`의 기존 근거를 유지하며 같은 gate unit을 다시 작성하지 않는다.
- PR callback의 실제 소비 fixture에서 TTL 안/30초 이후 callback만 반복해도 추가 runtime fetch는 0회여야 하며, 그 뒤 첫 실제 단계 진입에서만 TTL에 따른 조건부 관측을 해야 한다. 종료 결정이 이미 고정됐으면 callback과 다른 스레드의 추가 원격 관측·신규 호출은 0회이고 PR 상태/완료 기록과 진행 중 결과·checkpoint는 보존돼야 한다. callback을 직접 관측 함수로 연결해 두던 assertion만 새 소비 계약으로 교체하고 PR identity·Merge·실패 저장 검증은 남긴다.
- `tests/test_task_scheduling.py`의 기존 동시 Worker/verify·handoff 중 미시작 반환/실행 중 예약 보존 회귀를 재사용한다. 이 작업에서 바뀐 callback/부모 인계의 후속 연결에 빠진 assertion만 보강하고 G-297의 대기·재배정 조합은 복제하지 않는다. pause·취소는 TTL 내/대기 중에도 작동하고 종료 불명·미확정 저장은 인계 성공으로 꾸미지 않는다.
- Worker는 위 부모 제어/저장 gate의 최소 unit case를 실행한다. 실제 pause DB·검증한 인계 receipt·부모 제어를 사용하고 자식/원격 효과만 double로 제공한다. 자식 반환 직전·재선택 전·시작 직전 pause에서 추가 fetch/bootstrap/child 0회와 원래 작업/receipt 보존을 검사한다.
- 프로젝트 pause/전체 pause·사용자 중단/일반 실패/유효 refresh를 비교하고 resume 자체의 외부 효과 0회, 다음 명시 run의 기존 gate 재확인을 확인한다. 잘못된 source/base/token·이미 소비한 receipt·종료/저장 불명은 기존 거부/보존 회귀를 재사용한다.
- `test_owned_refresh_restarts_latest_remote_in_one_parent_invocation`·`test_remote_child_handoff_preserves_source_config_interpreter_arguments_and_real_lock`의 최신 재선택/실제 lock 근거를 유지한다. 필요한 pause 연결 한 사례만 확장하며 같은 Git lifecycle을 상태 조합별로 반복하지 않는다. 기존 Linux/Windows 자식 종료·잠금 회귀를 유지하고 mock 결과를 native 실행 성공으로 쓰지 않는다.

## Progress

- [x] revision `b811a0f0fcd00bcaa0ce5eaede072425e8627ab1`의 한 번 실행하는 부모/자식 반환과 handoff 0 처리·process lock·관련 테스트를 정적으로 확인했다. G-236 후보는 읽기 전용 문맥이며 최종 통합/검증 성공은 확정하지 않았다.
- [x] 현재 revision의 #293 소유 refresh receipt·부모 자동 재시작·논리 run 보존 및 pause 조회 hook을 정적으로 대조했다. 직접 구현된 인계/재시작 자체는 제외하고 G-236 이후의 부모 pause 통합만 미충족으로 남겼다. G-236 후보는 읽기 전용이며 실행 검증 성공은 확인하지 않았다.
- [x] 지정 revision의 공통 TTL·관측 lock·종료 요청/launch gate와 scheduler 공유, 세 wait helper의 원격 미조회 및 관련 회귀를 정적으로 대조했다. 해당 구현은 남은 작업에서 제외하며 테스트 실행·성공을 뜻하지 않는다.
- [ ] PR callback의 직접 runtime 관측 제거와 인계 중 안전한 반환/저장의 잔여 연결
- [ ] 실제 Merge된 pause 저장/gate와 부모 재선택/재시작의 연결
- [ ] pause 접수 경합·미확정 인계 보존의 최소 targeted 회귀

## Decision Log

- 이번 delta의 `bootstrap_base_branch()` journal 복구·`RuntimeHandoffGate.check()` 재진입 억제와 관련 회귀는 원격 base의 직접 구현으로 정적으로 확인했다. 새 구현 목표로 추가하지 않고 남은 부모 pause 연결의 보존 조건에 통합한다. 기존 ID·경로·metadata·dependency·수행 기록과 사용자 결정을 유지하며 G-236 보류·실제 Merge와 검증 성공을 대신 확정하지 않는다.
- snapshot token `a6cfed44479b43ce3190bc361a58fda763e8ae5db663c86f5bde870a40611258`, revision `4a7f03fff00251bd3b021acac8206178d8e5e293`과 원문 hash `6b83ddfa85ef1bc0429574ed027278087af85876d2cc6ab3cfc72f7a643038e9`를 대조했다. R-082의 보류 선행 참조 허용에 따라 같은 ID·경로·sources·G-236 dependency와 기존 기록을 보존해 개정한다. 직접 구현된 TTL/대기 helper를 제외하고 PR callback의 관측 우회·반환/저장·부모 pause 통합만 남긴다. G-297의 관측 위치 교정과 분리하며 G-236 보류 해제·실제 Merge·검증 성공은 만들지 않는다. 아래 사용자 결정은 보존하고 이번 delta 판단을 프로젝트 최종 전문 확인으로 사용하지 않는다.
- 2026-10-23 사용자 결정: runtime 원격 코드는 실제 단계가 진행될 때만 확인하고, 최초 즉시/이후 성공 확인 기준 30초 TTL을 적용한다. 대기 중 외부 비교는 없애며 코드 변경이 확정되면 종료 결정을 메모리에 캐시해 재검사하지 않는다. 새 단계만 막고 이미 실행 중인 호출의 안전한 반환·저장 뒤 기존 부모 인계로 자동 재시작한다. 변경 판정/인계 엔진을 새로 만들거나 G-236 pause 책임을 중복하지 않는다.
- snapshot의 같은 ID·경로·기존 조사 기록과 G-236 선행을 보존한다. G-236에 새 runtime 엔진이나 역방향 dependency를 부과하지 않는다. 이 작업은 해당 pause 계약을 소비하는 부모 연결만 맡고 G-313의 단계별 복구 정책은 별도로 유지한다.
DEPENDENCY_RECOVERY: {"deleted_id":"G-311","outcome":"obsolete","replacement_ids":[],"sources":["docs/PRODUCT.md","docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md","docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md","docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md"],"reason":"원격 코드 비교·PR 독립 단계 관측·전체 gate는 #293의 self_runtime/runtime_inputs/handoff_gate와 관련 회귀에 직접 구현돼 별도 선행 구현이 남지 않았다. 해당 보장은 계속 소비하며 G-236의 실제 Merge와 부모 pause 통합은 남긴다. 문서 삭제를 검증·Merge 성공으로 사용하지 않는다."}
- complexity medium(최초 level 3): 남은 판단은 기존 두 제어 경계의 pause/시작 경합과 보존 연결이다. risk high: 잘못된 재시작은 사용자 중단을 무시하거나 중복 writer를 만들 수 있으므로 실제 저장·소유 receipt·원자 launch/종료 근거를 유지한다. priority high, Model·Effort는 Manager 설정을 따른다. Planner는 구현·테스트·verify를 실행하지 않았다.
