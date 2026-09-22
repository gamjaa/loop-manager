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

- 현재 provider adapter가 profile을 내부에서 다시 해석하고 tuple/`AgentResult`를 혼용하는 경계를 provider-neutral invocation request/result로 정리한다. CLI 인자에 사용한 `AgentProfile`과 session event·usage metadata를 결과와 함께 반환하되 기존 성공·실패 분류 책임은 유지한다.
- Codex JSONL과 Claude stream/json을 로그 파일에 계속 보존하면서 session 및 usage event를 누적하는 작은 parser 경계를 둔다. provider 원문 key는 adapter 안에서만 다루고 orchestration과 SQLite에는 정규화된 optional counter만 전달한다.
- append 후 완료되는 invocation 저장소를 별도 책임으로 두고 Planner의 project-level 호출과 Worker·Reviewer의 ExecPlan-level 호출을 모두 수용한다. 시작 후 중단된 row를 단일 process lock 아래에서 복구하는 결정적 규칙을 포함한다.
- `run_agent_with_retries`가 각 물리적 시도의 transition kind, 원인과 attempt를 저장소에 전달하게 하되 재시도 횟수, 사용 한도 중단과 session fallback의 기존 동작은 변경하지 않는다. 후속 G-041이 Model 교체 전이를 같은 계약으로 기록할 수 있게 enum과 저장 경계를 마련한다.
- 비교 지표는 원본 counter와 duration에서 SQL 또는 읽기 helper가 계산하게 하고 가격표, 비용 추정 또는 추가 Agent 호출을 도입하지 않는다.

## Validation

- Worker는 invocation 저장소와 provider usage parser에 직접 관련된 개별 test class 또는 새 전용 test module만 targeted unit test로 실행한다.
- Planner, Worker와 Reviewer의 start/resume, transient retry, usage-limit, timeout, session fallback 호출이 각각 정확한 role·plan·profile·transition·result record를 남기는지 검증한다.
- Codex와 Claude의 완전한 usage, 일부 필드만 있는 usage, cache read/write 구분, reasoning 미제공, 잘못된 JSON과 음수·비정수 값을 검증한다.
- session 시작 직후와 invocation 완료 직전 중단을 재현해 시작 row와 session identity가 보존되고 다음 실행에서 중복 완료나 상태 차단 없이 복구되는지 검증한다.
- legacy SQLite migration이 기존 실행 상태를 잃지 않고 새 원장을 만들며 반복 migration이 무해한지 검증한다.
- usage parser 오류가 성공한 Agent 호출, Worker 결과와 ExecPlan 상태 전이를 바꾸지 않고 환경·prompt·record에 manager 비밀값이 남지 않는지 검증한다.
- 전체 verify에서 기존 Codex·Claude 시작/재개, bounded retry, level profile 선택과 Planner/Worker/Reviewer 상태 전이가 유지돼야 한다.

## Progress

- [ ] provider-neutral profile·usage·invocation 결과 계약 추가
- [ ] Codex·Claude usage parser 및 부분 형식 허용 구현
- [ ] additive invocation 원장과 중단 복구 구현
- [ ] 모든 역할과 실제 retry/fallback 호출 경계 연결
- [ ] profile·transition·usage·migration 회귀 테스트 추가

## Decision Log

- 사용량 관측 실패는 제품 작업 실패와 분리한다. provider가 명시적으로 보고한 분류만 보존하고 누락값을 추정하지 않는다.
- invocation은 상위 Worker turn이 아니라 실제 CLI process 시도 단위로 기록해 transient retry와 fallback 비용을 숨기지 않는다.
- G-037 이후의 revision-aware Agent 호출 경계를 사용해 관련 ExecPlan과 입력 revision이 확정된 시점에 telemetry를 결합한다.
