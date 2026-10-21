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
  - G-311
complexity: high
risk: high
priority: high
---

## Purpose

G-311의 코드 변경 handoff를 신뢰된 bootstrap 부모가 받아, 현재 호출의 반환·정리·영속화 뒤 최신 원격 runtime으로 같은 실행을 이어간다. 정상 종료 코드 0과 재시작 요청을 구별하며 외부 주기 실행을 기다리지 않는다. runtime 내부 hot reload·재귀 자식 호출 없이 기존 부모의 runtime 선택·process lock 수명을 확장한다.

현재 `application/scheduler.py:run_registered_projects()`는 handoff를 0으로 반환하고 `loop_manager.py:main()`은 runtime 자식의 종료 코드를 그대로 반환한다. `runtime/bootstrap.py:run_runtime_child()`도 한 번 실행한 뒤 정리한다. 기존 exact-SHA 선택·부모 잠금·bytecode·원격 bootstrap과 G-236의 pause 보존을 소비한다.

## Acceptance Criteria

- R-023/R-091: 전체 launch gate가 닫힌 뒤 이미 실행 중인 Agent·verify·단계 처리의 반환·정리·저장 및 종료를 확인하고 부모에 인계한다. 작업 전체 완료·PR 게시/Merge를 기다리지 않는다. 종료 불명·미확정 저장/게시를 완료로 가정해 같은 작업을 시작하거나 runtime을 지우지 않는다.
- 인계는 source/repository·현재 runtime SHA/실행 identity·등록 base·확인한 원격 목표와 결합한다. 일반 성공/오류·사용자 pause·임의 환경값/파일·Agent 출력이 재시작 요청으로 해석되지 않는다. 인계 중 중단·재처리는 기존 실행/소유 근거와 실제 자식을 확인하며 중복 실행을 만들지 않는다.
- 부모는 소유 process lock을 유지하며 최신 등록 base를 다시 확인하고 검증한 원격 revision의 bootstrap·runtime을 시작한다. 확인 이후 더 전진한 코드도 다시 선택하며 로컬 HEAD·오래된 코드로 대체하지 않는다. 동일 revision/코드에 진전 없는 재시작을 반복하지 않고, fetch·identity·ancestry/인계 검증 실패는 자료·재개 상태를 보존한다.
- source 절대 config·선택 interpreter·PATH·명령 인자/프로젝트 선택·기존 bytecode 정책을 유지한다. 각 물리 runtime의 소유 identity와 논리 실행/작업 주기 identity를 구분해 session·작업물·단계·실패/교정/테스트 집계·재시도 대기/소비 시간·수동 제출 순서·예약을 보존한다. 새 runtime 시작으로 성공 receipt나 사용자 승인·새 Worker session을 합성하지 않는다.
- G-236/R-076의 전체 pause를 인계/재선택/재시작 전에도 적용한다. pause가 있으면 새 bootstrap 원격 효과나 runtime을 시작하지 않으며, 프로젝트 pause는 해당 대상만 제외한다. 사용자 resume는 재시작을 직접 실행하거나 독립 차단을 해제하지 않는다. 현재 runtime을 강제로 종료하거나 source 파일·index·HEAD를 교체하지 않는다.

## Implementation

- [ ] 확인한 `runtime/handoff_gate.py`·`application/scheduler.py`·`application/project_runner.py`와 G-311의 제한된 교체 판단을 연결해 부모가 검증할 인계 결과를 전달한다. `runtime/contracts.py`·`runtime/bootstrap.py`·`runtime/bootstrap_adapter.py`의 기존 소유/실행 계약에 필요한 호환 전이만 추가한다. 종료 코드나 자유 형식 로그만으로 인계를 신뢰하지 않고 과거 인계의 재소비를 방지한다.
- [ ] 확인한 `loop_manager.py:main()`의 부모 process lock·`prepare_bootstrap_runtime()`·`run_runtime_child()`를 재사용해 반환 후 최신 원격 재선택과 반복 실행을 구현한다. 변경된 bootstrap도 검증한 새 revision에서 실행되게 하며 source의 낡은 구현을 재시작 엔진으로 고정하지 않는다. 새 runtime이 자기 자식을 재귀 생성하거나 활성 runtime 파일을 바꾸는 방식은 사용하지 않는다.
- [ ] `runtime/bootstrap.py`의 자식 종료·runtime/cache 정리와 `runtime/process_lock.py`의 실행 identity를 보존한다. 확인한 `application/task_scheduling.py`·`storage/task_calls.py`·`storage/recovery_state.py`의 논리 실행·호출 reservation 및 작업별 checkpoint를 대조해 실제 미완료 단계부터 이어간다. `scheduler.py`가 매 호출 새로 만드는 `external_run_id`와 TEMP run marker가 재시작을 실패 집계 초기화로 만들지 않게 연결한다. 물리 runtime identity는 새 원격에 맞게 검증하고 이전 호출의 귀속/감사는 변경하지 않는다.
- [ ] G-236 Merge 뒤 `runtime/pause.py`·bootstrap 전 pause와 `tests/test_pause.py`의 기존 scope/종료 근거를 사용한다. pause·사용자 중단과 정상 handoff를 구별하고 부모 잠금·자식/예약·인계 자료의 수명이 어긋나지 않게 한다. 종료가 확인된 자기 자원만 정리하며 다른 command runtime·worktree는 유지한다.
- [ ] 확인한 `tests/test_bootstrap_entry.py`·`test_bootstrap_runtime.py`·`test_bootstrap_runtime_selection.py`·`test_registered_project_scheduler.py`·`test_task_scheduling.py`의 부모/자식·lock/receipt fixture에 재시작 연결을 추가한다. PRODUCT·ARCHITECTURE의 다음 외부 실행 전용 handoff 설명과 관련 운영 안내를 실제 동작으로 통합한다.

## Validation

- Worker는 새 인계/부모 제어의 최소 unit을 실행한다. 자식 실행 double과 실제 상태·소유 검사를 사용해 A 실행→유효한 코드 변경 인계→최신 B 실행→정상 종료를 한 부모 호출로 확인한다. 준비/자식/정리 순서·lock 유지·config/interpreter/인자/PATH 보존·작업 session/실패 집계 연속성을 assertion으로 둔다.
- `tests/test_bootstrap_entry.py`의 실제 Git·부모 lock·원격 bytes 확인 회귀를 필요한 최소 재시작 사례에 확장한다. 로컬 dirty/동기화 skip 조합은 기존 회귀와 mock으로 보완하며 각 역할별 전체 Git lifecycle을 반복하지 않는다. 인계 후 C 전진, 동일 목표 재처리, 잘못된 source/base/실행 identity, fetch 실패·비FF·불완전 인계에서 새 자식이 잘못 시작되지 않아야 한다.
- A Worker·B verify의 안전 반환과 queued Reviewer 미시작을 확인한 뒤에만 부모 인계/새 runtime이 일어난다. 저장/종료 불명·중단·재시작에서 기존 receipt·checkpoint·원문을 보존하고 Agent/verify/게시의 중복 실행·예약 유실·이력 초기화가 없어야 한다. 완료한 단계를 다시 실행해 재개를 흉내 내지 않는다.
- 전체 pause를 반환 직전·부모 재선택 전·새 자식 시작 직전에 접수한다. 실제 pause 저장과 gate를 사용해 새 외부 효과가 0회이며 기존 인계/작업이 보존되는지 확인한다. 프로젝트 pause·수동 명령의 독립 runtime·일반 실패 exit code와 사용자 중단도 구분한다. 기존 Linux 자식/Windows Job 종료·파일 잠금 native 회귀는 필요한 연결만 유지하고 mock을 실제 OS 실행 성공으로 기록하지 않는다.

## Progress

- [x] revision `b811a0f0fcd00bcaa0ce5eaede072425e8627ab1`의 한 번 실행하는 부모/자식 반환과 handoff 0 처리·process lock·관련 테스트를 정적으로 확인했다. G-236 후보는 읽기 전용 문맥이며 최종 통합/검증 성공은 확정하지 않았다.
- [ ] 소유 인계·최신 원격 재선택·같은 실행의 자동 재시작
- [ ] pause/종료 불명·중단·논리 실행 보존의 최소 회귀

## Decision Log

- R-023의 자동 재시작은 기존 bootstrap 부모에 제어를 돌려 구현한다. 같은 문서의 runtime hot reload/재귀 자식 금지는 유지하며 새 daemon·서비스·스케줄러를 만들지 않는다. PR 조회는 G-311과 분리된 일반 수명주기에 남긴다.
- G-236의 진행 중 pause 구현과 같은 bootstrap/process 경계를 사용하므로 그 실제 Merge를 선행으로 둔다. G-236에는 새 runtime 기능 구현을 떠넘기거나 새 dependency를 추가하지 않는다. G-311의 확정한 코드 비교를 소비하고 G-313의 비차단 실패 대기는 별도 단계 정책이다.
- complexity high: 부모/자식 실행 identity·잠금·정리·논리 실행/실패 이력의 수명을 중단/재시작에서 연결해야 한다. risk high: 잘못된 인계/종료 확인은 중복 writer·게시 또는 작업 유실을 만든다. 소유 근거·실제 종료·현재 원격 재확인과 중단/경합 회귀로 방지하며 미확인은 보존한다. priority high, 최초 level 5이며 Model·Effort는 Manager 설정을 따른다.
