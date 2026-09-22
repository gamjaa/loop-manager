---
id: G-041
title: Profile-Bound Worker Session Continuity
sources:
  - docs/requirements/[R-018] Cache-Efficient Worker Session Continuity.md
depends_on:
  - G-040
  - G-049
complexity: high
risk: high
priority: high
---

## Purpose

Worker session을 그 session을 실제로 시작한 provider와 Model identity에 결합한다. 다음 Worker profile의 provider·Model이 같으면 새 effort를 적용해 기존 문맥을 재개하고, provider 또는 Model이 달라지면 worktree와 검증 상태를 보존한 채 G-040의 compact recovery context로 새 session을 시작하도록 중단 복구 가능한 전이로 만든다.

## Acceptance Criteria

- Worker 호출 전에 현재 effective level과 설정에서 실제 provider, Model과 effort를 한 번 해석하고, 저장된 Worker session을 시작한 provider·Model binding과 비교한다.
- 저장 binding과 실제 provider·Model 식별자가 같으면 일반 verify·Review 수정에서 기존 Worker session ID를 재개한다.
- provider·Model은 같고 effort만 달라지면 선택된 새 effort를 CLI 호출에 적용하면서 동일 session ID를 재개한다. effort 변경이나 동일 session 자체가 cache hit를 보장한다고 기록하거나 판단하지 않는다.
- 설정 alias나 Worker level이 바뀌어도 해석된 provider·Model이 같으면 Model 변경으로 취급하지 않는다. 실제 provider 또는 Model 식별자가 달라진 경우에만 기존 session을 재개하지 않는다.
- provider 또는 Model이 달라지면 기존 session과 audit 이력은 보존하되 active resume 대상으로 사용하지 않고, 현재 worktree, 최신 ExecPlan, 이전 Worker 결과와 G-040의 미해결 compact feedback을 사용해 선택된 새 profile의 Worker session을 시작한다.
- Model 교체 결정과 목표 provider·Model·effort 및 원인은 새 Agent process를 시작하기 전에 durable하게 저장한다. 새 session 시작 전이나 session ID 저장 전 프로세스가 중단돼도 다음 실행은 과거 Model session으로 되돌아가지 않고 같은 목표 profile의 시작 또는 이미 확인된 새 session 재개를 계속한다.
- 새 session ID가 provider event에서 확인되면 invocation과 active Worker binding을 같은 전이 identity에 연결한다. 시작 실패, timeout 또는 사용 한도 소진은 현재 worktree, Worker 결과, verify·Review 원문, 실패 history와 목표 profile을 보존하고 기존 외부 오류 정책을 따른다.
- 저장된 session이 unavailable이면 fallback 전이를 먼저 durable하게 기록하고 과거 session을 다시 시도하지 않은 채 같은 목표 profile과 G-040 recovery context로 새 session을 시작한다. 중단 후에도 동일 규칙을 적용한다.
- legacy Worker session에 provider·Model binding이 없어 안전한 동일성을 증명할 수 없으면 과거 session을 임의의 현재 Model로 재개하지 않고 명시적인 unbound-session recovery 원인으로 새 session을 시작한다.
- Review 승격, 운영 profile 변경 및 G-037의 revision-aware complexity·risk 갱신으로 effective profile이 바뀌는 모든 Worker 재진입 경계에서 같은 비교를 적용한다. 현재 실행을 선점하거나 Worker level 승격·BLOCKED 기준을 변경하지 않는다.
- session 교체와 fallback은 G-039에 replacement/fallback 종류, 이전·새 session, 이전·목표 profile과 원인을 기록하고, 이후 역할·profile·전이별 usage와 latency 비교에서 구분된다.
- Planner와 Reviewer의 책임, Review 실행 조건, Worker level 승격 조건, worktree/Agent scope 검증과 성공한 verify 출력 비전달 규칙은 변경하지 않는다.

## Implementation

- 새 `worker_session.py`에 resolved provider/Model binding 비교와 same-session, effort-only resume, model/provider replacement, unavailable fallback 및 legacy-unbound 전이 상태 machine을 순수 decision API로 정의한다.
- active binding과 pending target transition은 `runtime_state.py` execution snapshot/repository가 expected plan/status에 결합해 원자적으로 저장한다. G-039 invocation ledger는 각 physical attempt와 전이 identity를 감사하고, 두 저장소의 역할을 합치거나 Agent subprocess 중 raw SQL로 갱신하지 않는다.
- G-049 `ExecutionContext`에 active session binding과 pending transition을 포함하고 `ExecutionHandlers.run`이 Agent port 호출 전에 effective level에서 resolved profile을 한 번 얻어 session decision을 적용한다. adapter는 이미 resolved된 profile을 받아 CLI 인자와 ledger에 동일 값을 사용한다.
- replacement/fallback은 G-040 compact recovery-context builder를, normal/effort-only resume는 pending feedback delta를 사용한다. `on_session_started` observation은 pending transition identity에 compare-and-swap으로 결합하고 실패·중단 시 이전 binding으로 rollback하지 않는다.
- Worker 진입은 execution handler 한 곳으로 수렴시키고 Review escalation, G-037 definition/profile delta, guidance 및 rebase correction 호출자도 같은 Worker-session port를 사용하게 한다. `loop_manager.py`에는 profile configuration adapter와 port wiring만 남긴다.

## Validation

- Worker는 새 `tests/test_worker_session.py`, `tests/test_runtime_state.py`, `tests/test_agent_invocation_state.py`와 `tests/test_execution_handlers.py`의 직접 관련 targeted case만 실행한다.
- 동일 provider·Model·effort, effort-only 상승·하락, 실제 Model/provider 변경, alias/level-only 변경과 legacy unbound binding을 pure transition table로 검증한다.
- replacement intent 직후, process 시작 뒤 session ID 전, session ID 저장 뒤 result 전 중단에서 runtime snapshot CAS가 이전 Model로 돌아가거나 중복 transition을 만들지 않는지 검증한다.
- session-unavailable fallback의 시작 실패·재실행·성공이 worktree, validation audit/history, definition token과 publication marker를 보존하고 동일 recovery context를 사용하는지 handler fake로 검증한다.
- Review escalation과 G-037 complexity/risk delta가 resolved profile 기준으로 resume/replacement를 고르고 기존 escalation 및 동일-fingerprint block 이력을 유지하는지 검증한다.
- G-039 ledger에 이전/목표 profile, 이전/새 session과 transition cause가 연결되며 usage 누락이나 ledger collection 오류가 session decision을 막지 않는지 검증한다.
- 전체 verify에서 rebase correction, guidance approval, Agent scope와 publication 상태가 session transition 때문에 초기화되지 않아야 한다.

## Progress

- [ ] pure Worker session binding/transition decision 모듈 구현
- [ ] runtime-state active binding·pending transition CAS 저장 구현
- [ ] ExecutionContext/handler의 단일 resolved-profile session gate 통합
- [ ] replacement/fallback recovery context와 invocation ledger 연결
- [ ] profile·중단·fallback·escalation targeted 회귀 테스트 추가

## Decision Log

- session 호환성은 설정 alias나 level이 아니라 provider와 실제 Model 식별자로 판단한다. effort는 호출별 선택값이므로 동일 Model session 재개의 조건에서 제외한다.
- 목표 Model 변경을 먼저 저장한 뒤 새 session을 시작해 실패나 중단이 과거 Model session으로의 암묵적 rollback을 만들지 않게 한다.
- 새 session에는 대화 전체를 복사하지 않고 worktree와 저장된 제품 상태를 원본으로 G-040의 bounded recovery context만 전달한다.
- Post-refactor revalidation: 리팩터링 전 `run_project`의 여러 Worker 진입점을 되살리지 않고 G-049 `ExecutionHandlers.run` 앞의 하나의 session gate로 수렴시킨다. session policy, phase orchestration, provider CLI와 persistence는 각각 별도 경계에 둔다.
