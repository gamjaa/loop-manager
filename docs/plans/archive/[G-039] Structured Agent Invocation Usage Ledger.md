---
id: G-039
title: Structured Agent Invocation Usage Ledger
sources:
  - docs/requirements/[R-018] Cache-Efficient Worker Session Continuity.md
depends_on:
  - G-037
  - G-049
complexity: high
risk: high
priority: low
---

## Purpose

Planner, Worker와 Reviewer의 실제 Agent CLI 호출을 역할, 선택된 provider·Model·effort, 세션 전이, 실행 결과와 provider 사용량에 연결된 구조화된 원장으로 기록한다. 누락되거나 잘못된 usage가 작업 결과와 상태 전이를 방해하지 않는 관측 경계를 먼저 마련해, 후속 세션 재사용과 교체 정책의 cache 효과·latency·재시도 비용을 비교할 수 있게 한다.

## Acceptance Criteria

- 프로그램은 각 실제 Agent CLI 시도 전에 하나의 invocation identity와 시작 시각을 저장하고, 종료 시 종료 시각, 소요 시간과 결과 분류를 같은 record에 기록한다. 상위 역할 호출 안의 transient retry와 session-unavailable fallback도 각각 구분되는 실제 시도로 남는다.
- invocation record에는 프로젝트, Planner·Worker·Reviewer 역할, 관련 ExecPlan ID 또는 Planner의 명시적인 plan 없음, 실제 provider, 해석이 끝난 Model·effort, 세션 ID, 시작·재개·교체·fallback 전이 종류와 구조화된 원인 및 시도 순서가 포함된다.
- Model과 effort는 CLI 인자를 만든 것과 동일한 한 번의 profile 해석 결과를 기록한다. 설정 alias나 저장된 과거 level을 실제 Model 식별자로 오인하지 않는다.
- Codex JSON event와 Claude stream/json 결과에서 provider가 보고한 usage를 provider-neutral 값으로 정규화해 input, cached input, cache write input, output과 reasoning output을 서로 다른 nullable 필드로 저장한다.
- provider가 구분하지 않는 usage 종류는 다른 종류에 합산하거나 0으로 만들지 않고 알 수 없음으로 저장한다. cached input을 무료 또는 일반 input과 같은 비용이라고 가정하지 않는다.
- usage 필드 누락, 음수·비정수 값, 부분 event, 잘못된 JSON 또는 provider 형식 변경은 해당 usage 값이나 record의 수집 상태만 unknown/invalid로 남긴다. 성공한 Agent 결과를 실패로 바꾸거나 ExecPlan 상태 전이를 차단하지 않는다.
- Agent가 session ID를 보고한 시점과 최종 결과 사이에 중단돼도 시작 record와 확인된 session ID를 보존한다. 다음 단일-process 실행은 종료되지 않은 과거 호출을 중복 완료로 만들지 않고 interrupted/unknown 결과로 확정하거나 계속 식별할 수 있다.
- 성공, 영구 설정 오류, transient 오류, 사용 한도, timeout, 예외와 프로세스 중단은 기존 Agent 결과 분류를 유지하면서 원장에 구분된다.
- 저장된 열과 안정적인 invocation/session identity만으로 역할·profile·세션 전이별 호출 수, retry 수, cache 비율, uncached input, 출력량과 latency를 질의할 수 있다. provider 가격이나 실제 청구 비용은 계산하거나 저장하지 않는다.
- additive SQLite migration은 기존 프로젝트, ExecPlan 상태, 세션과 실패 이력을 그대로 보존하며 반복 실행해도 멱등하다.
- prompt, usage parser, 구조화 record와 진단에는 Agent 자격 증명, Telegram token 및 그 밖의 비밀값을 포함하지 않는다.

## Implementation

- 새 `agent_invocation.py`에 provider-neutral resolved profile, invocation request/result, session transition과 nullable usage counter type을 정의하고, 새 `agent_invocation_state.py` repository가 physical process attempt의 시작·session observation·완료/interrupted 전이를 additive ledger에 저장한다.
- Codex/Claude adapter는 CLI 인자에 사용한 동일 profile을 request에 받고 provider event를 typed session/usage observation으로 반환한다. provider 원문 key와 JSON tolerant parsing은 adapter 내부에 남기고 execution/planning coordinator에는 정규화된 result만 노출한다.
- `run_agent_with_retries`를 invocation runner로 좁혀 각 transient retry와 session fallback 전에 ledger intent를 기록하고 결과 뒤 같은 identity를 완료한다. retry/usage-limit/timeout 정책은 유지하되 telemetry 저장 실패는 별도 collection status로 반환해 Agent 결과를 덮어쓰지 않는다.
- `planning_runtime.py`의 Agent port와 G-049 `AgentEffects`가 동일 invocation runner interface를 사용하게 하고 role/plan/definition revision 및 transition cause를 호출 context에서 전달한다. 두 coordinator가 ledger SQL, provider usage parser 또는 profile alias 해석을 중복하지 않게 한다.
- `loop_manager.py`에는 provider subprocess adapter와 repository wiring, process-lock 시작 시 unfinished invocation reconciliation만 남기고 profile/result dataclass와 usage 계산은 새 모듈로 이관한다. 비교 지표는 저장 counter를 읽는 query helper가 계산한다.

## Validation

- Worker는 새 `tests/test_agent_invocation.py`, `tests/test_agent_invocation_state.py`와 provider adapter의 직접 관련 targeted case만 실행한다.
- Planner, Worker와 Reviewer의 start/resume, transient retry, usage-limit, timeout과 session fallback이 physical attempt별 role·plan·resolved profile·transition·result row를 남기는지 검증한다.
- Codex/Claude 완전·부분 usage, cache read/write 구분, reasoning 미제공, malformed JSON과 음수·비정수 값을 table test로 검증하고 누락값이 0으로 바뀌지 않는지 확인한다.
- session event 직후와 invocation 완료 직전 중단에서 durable start/session identity를 보존하고 다음 process-lock 실행이 중복 완료 없이 interrupted/unknown으로 수렴하는지 repository test로 검증한다.
- `tests/test_planning_runtime.py`와 `tests/test_execution_handlers.py`의 targeted fake-port case로 두 coordinator가 같은 resolved profile과 invocation identity를 전달하고 telemetry 오류가 phase outcome을 변경하지 않는지 검증한다.
- legacy migration 반복, query helper의 역할/profile/transition별 retry·cache·latency 집계와 prompt/environment/record의 비밀값 비노출을 검증한다.
- 전체 verify에서 기존 Codex·Claude start/resume, bounded retry, Worker level profile 선택과 Planner/Worker/Reviewer 상태 전이 회귀가 없어야 한다.

## Progress

- [x] provider-neutral invocation/profile/usage 계약 구현
- [x] tolerant Codex·Claude observation parser와 adapter 연결
- [x] additive invocation ledger 및 interrupted reconciliation 구현
- [x] planning/execution Agent port와 physical retry runner 통합
- [x] profile·transition·usage·migration targeted 회귀 테스트 추가

## Decision Log

- 사용량 관측 실패는 제품 작업 실패와 분리한다. provider가 명시적으로 보고한 분류만 보존하고 누락값을 추정하지 않는다.
- invocation은 상위 Worker turn이 아니라 실제 CLI process 시도 단위로 기록해 transient retry와 fallback 비용을 숨기지 않는다.
- G-037 이후의 revision-aware Agent 호출 경계를 사용해 관련 ExecPlan과 입력 revision이 확정된 시점에 telemetry를 결합한다.
- Post-refactor revalidation: invocation ledger는 planning/execution phase 상태가 아니라 공유 외부 호출 관측 경계다. 두 coordinator는 같은 Agent port를 사용하고 provider parsing·SQLite ledger를 `execution_handlers.py`나 `planning_runtime.py`에 내장하지 않는다.
- Recovery (2026-09-23): 보존된 G-039 worktree에서 `python3 -m unittest tests.test_loop_manager.LoopManagerTest.test_verify_success_passes_worker_and_verify_results_to_reviewer`를 다시 실행해 1개 통과를 확인했다. 이전 Worker의 `TARGETED_TESTS` 요약은 결과 판독기가 0건 표현을 실패로 오인했으므로 다음 결과에는 `PASS (1 test)`처럼 최종 통과 결과만 명시한다.
