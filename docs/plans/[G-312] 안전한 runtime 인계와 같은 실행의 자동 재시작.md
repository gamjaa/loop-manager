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
complexity: high
risk: high
priority: high
---

## Purpose

직접 구현된 동일 실행의 runtime 교체와 G-236의 pause를 실제 부모 재선택/재시작 경계에서 통합하고, 과도하게 반복되는 self runtime 원격 코드 관측을 실제 단계 진입 중심으로 줄인다. 전체 pause는 새 bootstrap 원격 효과와 runtime 시작을 막고 프로젝트 pause는 해당 대상만 제외한다. runtime 관측은 최초 즉시·이후 성공 관측 기준 30초 TTL을 공유하며 대기 중에는 수행하지 않는다. 변경 확정 뒤 종료 필요 결정을 메모리에 보존하고 추가 검사 없이 신규 배정을 막되, 진행 중 호출의 안전한 반환·저장을 완료한 다음 기존 부모 자동 재시작을 사용한다.

#293의 `runtime/refresh.py`·bootstrap은 소유 receipt와 부모 lock을 유지해 최신 원격을 재선택하며 논리 run ID를 이어간다. `refresh.paused()`와 시작 전 pause hook도 존재하므로 인계/재시작 엔진을 다시 만들지 않는다. G-236의 `runtime/pause.py`는 아직 충돌 중인 별도 후보에 있어, 해당 작업의 실제 Merge 뒤 공통 pause 저장/gate와 부모 인계의 최소 연결을 완성한다.

## Acceptance Criteria

- G-236의 전체 pause가 자식 반환 직전·부모 재선택 전·새 자식 시작 직전에 접수돼도 이후 새 원격/bootstrap/runtime 효과가 없다. 인계 receipt·원래 단계·session·대기/실패 이력은 남고 resume 명령은 실행·독립 차단 해제를 하지 않는다.
- 프로젝트 pause는 같은 범위의 새 자동 처리를 막으며 다른 프로젝트와 읽기/제어 명령·수동 준비의 기존 계약은 유지한다. 전체 pause와 프로젝트 pause를 같은 플래그로 합치지 않는다.
- 현재 전체 gate가 시작한 호출의 반환·종료/저장을 확인한 뒤만 부모에 인계한다. 종료 불명/미확정 저장·게시에서 성공 handoff나 새 호출을 만들지 않는다. 사용자 pause/일반 실패와 소유 runtime refresh 결과를 구별한다.
- 기존 source/repository/base·실행 SHA/identity·one-use receipt·부모 process lock·config/interpreter/PATH/인자·논리 실행 identity를 보존한다. 실제 최신 원격 재선택과 코드 동일 재시작 반복 방지, 물리 호출 reservation·checkpoint·실패/교정/재시도 예산을 초기화하지 않는다.
- `runtime_inputs.changed()`의 실제 실행 SHA 기준 코드/문서 판정과 PR 독립 관측·부모 인계/재시작은 현재 구현을 소비한다. 단계 전후·대기 루프에서 매번 원격을 조회하는 관측 빈도만 다음 조건으로 개편하며 pause 통합이 실제 PR 상태·로컬 source·Git/index를 바꾸거나 verify/Review 성공을 합성하지 않는다.
- 등록 self runtime의 최초 실제 단계 진입에서는 즉시 원격 관측하고, 이후에는 마지막 **성공 관측**으로부터 `time.monotonic()` 기준 30초 미경과 시 fetch·코드 입력 비교를 생략한다. 30초 경과 자체는 관측을 실행하지 않으며 다음 실제 단계 진입에서만 검사한다. 단계 종료·단순 결과 정리는 중복 관측 대상이 아니다. 전체 등록 프로젝트가 하나의 TTL과 검사 결정을 공유하고 동시 진입에서도 원격 관측을 중복 수행하지 않는다.
- retry/usage/Agent·verify 순번·기존 호출 종료·Git 자원 대기에서는 원격 비교를 수행하지 않는다. 사용자 pause·취소 및 이미 확정된 handoff 중단 신호는 TTL·대기와 관계없이 처리한다. 원격 fetch·identity·diff 실패는 성공 관측으로 캐싱하지 않고 기존 보존/재시도 계약을 따른다.
- 검증된 코드 변경을 처음 확인하면 현재 runtime의 인계 요청·종료 필요 결정을 메모리에 고정해 이후 원격 조회·비교·반복 로그를 생략한다. 새 관리 편입·Agent·verify·후속 단계는 시작하지 않되 이미 시작된 물리 호출·결과·checkpoint·필수 정리와 저장은 안전하게 마치도록 허용한다. 대기 중 호출을 억지로 완료시키거나 종료 불명 자원을 회수하지 않는다. 모든 시작된 호출의 종료/저장이 확인된 뒤 기존 one-use receipt·부모 lock·새 runtime 자동 시작으로 인계하고 주기 실행은 기다리지 않는다.

## Implementation

- [ ] `loop_manager_internal/runtime/handoff_gate.py`의 `observe_runtime()`·`RuntimeHandoffGate.observe()`와 `application/scheduler.py`의 self observer를 공통 30초 TTL·최초 즉시 관측·동시 관측 1회·확정 종료 결정 캐시로 연결한다. `time.monotonic()`의 성공 시각만 재사용하고 실패는 무변경/성공으로 캐시하지 않는다. stage 진행이 없는 동안 timer/poller를 새로 두지 않고 `runtime_inputs.py:changed()`의 원격 diff 판정·실행 SHA 기준을 그대로 사용한다. `self_runtime.py:_finish_self_merge_handoff()`의 확정된 `RuntimeRefresh`를 동일 run의 모든 작업에서 공유하고 이후에는 fetch·비교·반복 로그를 만들지 않는다.
- [ ] `planning/planning_runtime.py`, `application/project_service.py`·`project_runner.py`, `application/task_scheduling.py:scheduled_call()`의 실제 단계 진입·후속 배정 경계에서만 TTL 관측을 요구하고, 단계 종료·결과 저장과 `wait_retry()`·`wait_usage()`·`wait_existing_call()`·자원/slot 대기의 반복 원격 관측은 제거한다. 단, 사용자 pause·취소·이미 캐싱된 종료 결정은 대기 중에도 빠짐없이 확인한다. `RuntimeHandoffGate.check()`·`_handoff_popen()`·scheduler의 launch gate를 구별해 새 물리 호출은 원자적으로 거부하면서 이미 시작한 호출의 종료 수집·checkpoint 영속화는 막지 않는다. G-297은 작업별 대기·재배정의 호출 경계만 연결하고 G-313은 복구 소비만 맡는다.
- [ ] G-236 실제 Merge 뒤 확인한 `loop_manager_internal/runtime/refresh.py:paused()`·`runtime/bootstrap.py`의 부모 pause 검사와 `runtime/pause.py`의 영속 요청/실행 scope를 대조해 같은 저장 의미·대상/중단 결과를 사용하게 한다. 기존 호환 조회와 `runtime/handoff_gate.py`의 pause hook을 재사용하고 두 pause 엔진·새 DB/daemon을 만들지 않는다.
- [ ] 확인한 `loop_manager.py:main()`·`runtime/bootstrap_adapter.py`·`application/scheduler.py`의 소유 인계/부모 loop와 실제 자식 시작 사이 경합을 연결한다. pause 뒤 새 외부 효과가 시작되지 않게 하되 미확정 자식/receipt는 보존한다. G-236의 단순 gate/자원 대기 중 pause 회귀를 반복하지 않고 최신 원격 재선택/자동 재시작의 남은 연결만 보완한다.
- [ ] 확인한 `tests/test_bootstrap_entry.py`·`test_bootstrap_runtime.py`·G-236의 `test_pause.py` fixture를 재사용한다. PRODUCT·ARCHITECTURE의 기존 runtime/pause 소유 절에 실제 연결 조건만 통합한다. 부모 lock·one-use receipt·논리 실행/실패 이력 구현 자체는 직접 반영된 코드를 유지한다.

## Validation

- `tests/test_runtime_refresh.py`·`tests/test_registered_project_scheduler.py`·`tests/test_self_merge_handoff.py`의 실제 관측·인계 판단과 제어된 monotonic clock/스레드 fixture로 최초 관측 1회, 29.9초 이후 단계 진입의 fetch 0회, 30초 도달 이후 첫 단계에서 관측 1회, 30초 이상 단계 없는 동안 0회, 병렬 진입 중복 0회, 동일 runtime에서 code_changed=False인 문서 전진 뒤 TTL 재사용을 검증한다. 원격 관측 실패는 성공 TTL을 갱신하지 않고 기존 보존 오류 경계로 반환해야 한다.
- `tests/test_task_scheduling.py`의 실제 Worker/verify 동시 실행·대기 fixture에서 retry/usage/slot/기존 호출 종료 대기 중 원격 관측 0회, 대기 해소 후 첫 실제 단계에서 조건부 관측 1회, 변경 확정 후 모든 스레드의 추가 원격 관측·새 호출 0회와 현재 시작된 단계의 결과·checkpoint·종료 확인 뒤 부모 자동 재시작을 검증한다. pause·취소는 TTL 내/대기 중에도 즉시 작동하며 종료 불명은 인계 성공으로 꾸미지 않는다. G-297은 대기/배정 연결 사례만 유지한다.
- Worker는 위 부모 제어/저장 gate의 최소 unit case를 실행한다. 실제 pause DB·검증한 인계 receipt·부모 제어를 사용하고 자식/원격 효과만 double로 제공한다. 자식 반환 직전·재선택 전·시작 직전 pause에서 추가 fetch/bootstrap/child 0회와 원래 작업/receipt 보존을 검사한다.
- 프로젝트 pause/전체 pause·사용자 중단/일반 실패/유효 refresh를 비교하고 resume 자체의 외부 효과 0회, 다음 명시 run의 기존 gate 재확인을 확인한다. 잘못된 source/base/token·이미 소비한 receipt·종료/저장 불명은 기존 거부/보존 회귀를 재사용한다.
- `test_owned_refresh_restarts_latest_remote_in_one_parent_invocation`·`test_remote_child_handoff_preserves_source_config_interpreter_arguments_and_real_lock`의 최신 재선택/실제 lock 근거를 유지한다. 필요한 pause 연결 한 사례만 확장하며 같은 Git lifecycle을 상태 조합별로 반복하지 않는다. 기존 Linux/Windows 자식 종료·잠금 회귀를 유지하고 mock 결과를 native 실행 성공으로 쓰지 않는다.

## Progress

- [x] revision `b811a0f0fcd00bcaa0ce5eaede072425e8627ab1`의 한 번 실행하는 부모/자식 반환과 handoff 0 처리·process lock·관련 테스트를 정적으로 확인했다. G-236 후보는 읽기 전용 문맥이며 최종 통합/검증 성공은 확정하지 않았다.
- [x] 현재 revision의 #293 소유 refresh receipt·부모 자동 재시작·논리 run 보존 및 pause 조회 hook을 정적으로 대조했다. 직접 구현된 인계/재시작 자체는 제외하고 G-236 이후의 부모 pause 통합만 미충족으로 남겼다. G-236 후보는 읽기 전용이며 실행 검증 성공은 확인하지 않았다.
- [ ] 최초·30초 TTL·종료 결정 공유와 대기 중 원격 관측 제거, 이미 시작한 호출의 안전한 반환/저장
- [ ] 실제 Merge된 pause 저장/gate와 부모 재선택/재시작의 연결
- [ ] pause 접수 경합·미확정 인계 보존의 최소 targeted 회귀

## Decision Log

- 2026-10-23 사용자 결정: runtime 원격 코드는 실제 단계가 진행될 때만 확인하고, 최초 즉시/이후 성공 확인 기준 30초 TTL을 적용한다. 대기 중 외부 비교는 없애며 코드 변경이 확정되면 종료 결정을 메모리에 캐시해 재검사하지 않는다. 새 단계만 막고 이미 실행 중인 호출의 안전한 반환·저장 뒤 기존 부모 인계로 자동 재시작한다. 변경 판정/인계 엔진을 새로 만들거나 G-236 pause 책임을 중복하지 않는다.
- snapshot의 같은 ID·경로·기존 조사 기록과 G-236 선행을 보존한다. G-236에 새 runtime 엔진이나 역방향 dependency를 부과하지 않는다. 이 작업은 해당 pause 계약을 소비하는 부모 연결만 맡고 G-313의 단계별 복구 정책은 별도로 유지한다.
DEPENDENCY_RECOVERY: {"deleted_id":"G-311","outcome":"obsolete","replacement_ids":[],"sources":["docs/PRODUCT.md","docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md","docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md","docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md"],"reason":"원격 코드 비교·PR 독립 단계 관측·전체 gate는 #293의 self_runtime/runtime_inputs/handoff_gate와 관련 회귀에 직접 구현돼 별도 선행 구현이 남지 않았다. 해당 보장은 계속 소비하며 G-236의 실제 Merge와 부모 pause 통합은 남긴다. 문서 삭제를 검증·Merge 성공으로 사용하지 않는다."}
- complexity medium(최초 level 3): 남은 판단은 기존 두 제어 경계의 pause/시작 경합과 보존 연결이다. risk high: 잘못된 재시작은 사용자 중단을 무시하거나 중복 writer를 만들 수 있으므로 실제 저장·소유 receipt·원자 launch/종료 근거를 유지한다. priority high, Model·Effort는 Manager 설정을 따른다. Planner는 구현·테스트·verify를 실행하지 않았다.
