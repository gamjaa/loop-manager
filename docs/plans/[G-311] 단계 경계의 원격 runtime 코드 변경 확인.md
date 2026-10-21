---
id: G-311
title: 단계 경계의 원격 runtime 코드 변경 확인
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-056] 원격 기준을 유지하는 비차단 로컬 base 동기화.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

등록된 self 원격 base의 코드 입력을 실제 실행 runtime과 비교해, PR 조회와 독립적으로 단계 경계에서 교체 필요를 확정한다. 문서 전용 전진은 계속 실행하고 코드 변경을 확인하면 기존 runtime 전체 launch gate로 후속 배정을 멈춘다. bootstrap의 자동 재시작은 G-312가 이 판단을 소비한다.

현재 `application/self_runtime.py`는 `WaitingPullRequestObservation.merged`일 때만 SHA·ancestry를 확인하고 코드/문서를 구별하지 않는다. `_preflight_self_merge_handoff()`는 PR 조회를 먼저 수행한다. 기존 self identity·원격 fetch journal·전체 launch gate와 안전 반환은 재사용하며 원격 선택·Git 접근 권한을 새로 만들지 않는다.

## Acceptance Criteria

- R-023: Planner·Worker·부분/전체 verify·Reviewer·게시·복구의 시작 전과 성공/실패 반환 뒤, 다음 효과를 배정하기 전에 등록 self base의 최신 원격을 확인한다. 실패 후 RETRY와 재시도 대기 중 안전한 재확인도 같은 판단을 사용한다. self 프로젝트가 이번 작업 선택 범위에 없더라도 현재 runtime에 해당하는 등록 self를 식별하며, 다른 clone·프로젝트의 변경을 self로 인정하지 않는다.
- PR 부재·Open·Closed·Merged·조회 실패가 같은 원격 코드에 대한 판단을 바꾸지 않는다. 이 경로에서 PR 조회·Merge 판정·source 동기화를 요구하지 않으며 기존 작업 수명주기의 PR 상태 반영·오류·소유권 조건은 유지한다.
- 실제 실행 SHA와 확인한 원격 SHA 사이에서 소스·실행 스크립트·의존성 선언·로딩에 영향을 주는 실행 설정의 추가/수정/삭제/이동을 비교한다. Requirement·ExecPlan·설계·README·지침·템플릿 등 prose만의 변경은 제외한다. 문서 디렉터리의 실제 실행 코드는 포함하고 경로 이름이나 확장자만으로 실행 여부를 확정하지 않는다. 여러 문서 전용 전진 뒤에도 비교 기준은 실제 runtime이며, 혼합 변경·읽기/비교 실패를 무변경으로 처리하지 않는다.
- 원격 SHA/코드 동일은 계속 실행하고, 코드가 다른 검증된 fast-forward 후속은 전체 runtime의 신규 관리·Agent·verify·후속 게시 배정을 멈춘다. 이미 시작한 호출은 강제 종료하지 않고 반환·정리·저장을 기다린다. gate 확정과 실제 subprocess 시작의 경합에서도 확정 뒤 새 호출이 없다.
- R-020/R-056: 등록 base·canonical source·repository/origin·현재 runtime의 exact path/SHA/소유권을 유지한다. source의 dirty·ahead/diverged·동기화 skip/실패와 무관한 ref 변화는 교체 조건이 아니다. fetch·identity·ancestry·비교를 확인할 수 없으면 원인·단계를 보존해 기존 재관측 경로로 보내며 성공 handoff나 문서 전용으로 기록하지 않는다.
- 문서의 R-082 planning·단계별 반영·고정 instruction snapshot은 유지한다. runtime 비교는 verify 입력 동일성·성공 receipt 또는 프로젝트 전체 planning 성공 증명이 아니다. 진단에는 base·실행/원격 SHA·self identity·코드 비교 결과와 이유만 비밀값 없이 남긴다.

## Implementation

- [ ] 확인한 `loop_manager_internal/application/self_runtime.py`의 `_registered_self_runtime_project()`·`_self_runtime_project()`를 재사용하고, PR 관측 객체에 의존하지 않는 원격 코드 비교/교체 판단 경계를 둔다. 확인한 `runtime/bootstrap.py`의 선택 SHA·`runtime/contracts.py`의 `RuntimeMutation`, `git/base_fetch_service.py`·`manager_git_journal.py`의 fetch/자원 잠금을 사용한다. 등록 self 조회는 실행할 프로젝트 목록과 구별한다. Git·파일 관측 중 DB 쓰기 transaction이나 scheduler condition을 유지하지 않는다.
- [ ] 두 commit의 실제 실행 입력과 경로/kind/mode 변경을 대조한다. 기존 Git diff/tree 조회를 재사용하고 필요하면 `application/runtime_inputs.py`를 이 비교 전용으로 추가한다. `scripts/verify-inputs.json`은 검증 입력 목록이므로 runtime 실행 입력의 완전성으로 간주하지 않는다. 일반 prose 제외와 문서 경로의 실행 코드 포함 근거를 구현에 남기고 미확인 입력은 보수적으로 처리한다. 새 설정·범용 registry·환경 재현성 증명은 도입하지 않는다.
- [ ] 확인한 `application/scheduler.py`·`project_service.py`·`task_scheduling.py`, `execution/execution_runtime.py`, `planning/selection_service.py`의 단계/호출 경계와 `application/bindings.py`의 이름 있는 effects 조립에 판단을 연결한다. 병렬 관측을 조율해 같은 경계의 중복 fetch/판정을 줄이되 실제 runtime 기준과 원격 재관측을 건너뛰지 않는다. `_handoff_observed_self_merge()`의 기존 PR 확정 책임은 일반 수명주기로 유지하고 Merge 여부를 runtime 조건으로 사용하던 분기를 대체한다.
- [ ] 확인한 `runtime/handoff_gate.py`의 원자 launch 차단·대기 반환과 `runtime/resource_lock.py`의 중단 확인을 재사용한다. 확정한 self/base/실행/목표 정보를 G-312가 신뢰된 부모 인계로 전달할 수 있게 제한된 결과로 보존한다. 자유 형식 로그/Agent 출력으로 인계를 승인하지 않는다. R-091 비차단 재배정의 대기 수명은 G-297/G-313이 담당한다.
- [ ] 확인한 `tests/test_self_merge_handoff.py`·`test_registered_project_scheduler.py`·`test_task_scheduling.py`·`test_bootstrap_runtime_selection.py`와 `tests/support/origin.py`·`pr_recovery.py`의 fixture를 재사용한다. PRODUCT·ARCHITECTURE의 기존 self Merge 전용 판정 설명을 실제 코드 기준 동작으로 통합하고 R-082의 문서 처리 의미를 유지한다.

## Validation

- Worker는 변경한 판단/배정 경계의 최소 unit만 실행한다. Git/원격/프로세스 관측을 mock하고 실제 gate·상태 보존·단계 dispatch를 검사한다. main/대체 base, PR 상태 4종과 조회 실패에서 runtime 판단이 같고 runtime 경로의 PR 조회 호출 수가 0인지 확인한다. 일반 PR 조회 실패의 기존 처리를 삭제하지 않는다.
- 소스·스크립트·의존성/실행 설정, 문서 아래 실행 코드, 추가/삭제/이동·mode 변경, prose 전용/혼합, 문서 B→C 뒤 코드 D를 실제 runtime A와 비교하는 경우와 관측 실패를 확인한다. 같은 코드의 SHA 전진은 drain 0회이며 비교 오류는 동일 코드 성공이 아니다. 분류 unit에 고정된 코드/prose 표본을 사용하고 정책 조합마다 실제 Git repository를 만들지 않는다.
- 실행 중 A Worker와 B verify, 대기 Reviewer·신규 READY가 있는 fixture에서 성공/실패 경계의 코드 변경을 주입한다. 실제 판단→전체 gate→미시작 반환을 연결해 새 effect 0회, 기존 호출의 안전 반환·checkpoint/session/실패 보존을 확인한다. 테스트가 gate를 직접 stop하는 기존 회귀는 launch 경합/종료 불명 근거로 재사용한다.
- 기존 `test_local_sync_status_never_gates_verified_self_merge_handoff`·`test_self_merge_requires_pr_targeting_registered_base` 등에서 source/PR identity·보존 assertion은 해당 일반 PR/원격 선택 경계에 남긴다. 문서 전용 Merge나 일반 push를 무조건 구별하던 낡은 handoff expectation만 새 코드 비교로 대체한다. `test_runtime_handoff_unwinds_waiters_without_reclaiming_running_calls`와 exact-SHA runtime·dirty/attached/잘못된 origin 거부는 유지한다. 실제 fetch/tree 연결이 필요한 최소 Git 회귀만 보강한다.

## Progress

- [x] revision `b811a0f0fcd00bcaa0ce5eaede072425e8627ab1`의 R-023/R-055/R-056/R-091 delta, self 판정·PR 전용 handoff·launch gate·관련 테스트를 정적으로 대조했다. 실행 검증은 하지 않았다.
- [ ] 코드 입력 비교·PR 독립 관측과 단계 경계의 전체 gate 연결
- [ ] 최소 targeted 회귀와 실제 구현에 맞춘 설계 정합화

## Decision Log

- 현재 R-023이 기존 Merge 관측 전용 handoff를 명시적으로 바꾼다. R-020의 추적 ref 불일치 오탐 제거와 runtime 파일 고정은 유지하고, 검증된 원격 코드 전진에 한해 별도 교체를 판단한다. 기존 catalog에는 이 변경을 구현할 작업이 없으며 G-189는 후속 감사, G-297은 대기/배정 소비자다.
- G-312는 부모 인계·최신 runtime 재시작을 맡는다. 이 계획은 기존 전체 gate와 반환을 사용해 코드 변경 판단까지 독립 Merge할 수 있으며 기존 bootstrap·PR 처리·pause를 재구현하지 않는다.
- complexity high: 실행 입력의 제외/포함 근거, 병렬 단계의 원격 관측·launch 경합과 실패 반환을 함께 보존해야 한다. risk high: 누락된 입력/경계는 낡은 orchestration을 계속 실행시키고 잘못된 gate는 전체 작업을 멈춘다. 실제 runtime 기준·관측 실패 보존·원자 gate·단계/경합 회귀로 완화한다. priority high, 최초 level 5이며 Model·Effort는 Manager 설정을 따른다.
