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

- Worker session의 active binding과 pending transition을 ExecPlan 실행 상태에 additive하게 저장한다. provider·Model identity, 마지막 적용 effort, 이전 session, 목표 profile, transition kind/reason과 새 session ID 확정 여부를 명시적인 상태로 관리한다.
- `run_project`의 모든 Worker 진입이 공통 session-selection 함수에서 G-037이 확정한 최신 plan revision과 effective level의 profile을 해석하도록 한다. adapter 내부의 중복 profile 해석을 제거하고 G-039 invocation request에 같은 값을 전달한다.
- same-provider/model resume, effort-only resume, model replacement, provider replacement, unavailable fallback과 legacy-unbound recovery를 결정적 전이로 구현한다. replacement/fallback은 G-040 recovery-context builder를 사용하고 일반 same-session resume는 feedback delta만 전달한다.
- `on_session_started` 중단 경계를 포함해 목표 profile과 새 session identity 저장 순서를 transaction과 idempotent state machine으로 보호한다. CLI 실패 때문에 이전 session binding으로 rollback하지 않는다.
- session 교체는 Agent conversation만 바꾸며 worktree, branch, Worker 결과, validation audit/history, definition revision과 publication 상태를 초기화하지 않는다.

## Validation

- Worker는 Worker session selection·transition과 provider adapter 연결에 직접 관련된 개별 test class 또는 새 전용 test module만 targeted unit test로 실행한다.
- 동일 provider·Model·effort 재개와 동일 provider·Model에서 effort만 상승·하락하는 재개가 같은 session ID를 사용하고 새 effort CLI 인자를 적용하는지 Codex와 Claude 양쪽에서 검증한다.
- 실제 Model 변경, provider 변경, alias/level만 변경됐지만 실제 Model은 같은 경우와 legacy unbound session을 각각 검증한다.
- Model 교체 결정 직후, 새 process 시작 후 session ID 전, session ID 저장 후 결과 전 중단을 재현해 다음 실행이 이전 Model로 돌아가지 않고 중복 session 전이를 만들지 않는지 검증한다.
- session-unavailable fallback의 시작 실패·재실행과 새 session 성공이 worktree 및 전체 validation 상태를 보존하고 동일 recovery context 계약을 사용하는지 검증한다.
- Review 승격으로 effort만 바뀌는 level 전이와 Model까지 바뀌는 level 전이가 각각 resume와 replacement를 선택하며 기존 승격 history와 동일-fingerprint BLOCKED 횟수를 유지하는지 검증한다.
- G-039 invocation 원장에 resume/replacement/fallback 원인과 이전·새 session/profile이 정확히 연결되고 usage 누락이 전이를 막지 않는지 검증한다.
- 전체 verify에서 revision-aware resume, rebase conflict recovery, guidance 승인 재개, Agent scope와 publication 상태가 session 교체 때문에 초기화되지 않아야 한다.

## Progress

- [ ] durable Worker session binding과 pending transition 상태 추가
- [ ] 실제 profile 기반 session 선택 및 effort-only resume 구현
- [ ] Model/provider replacement와 legacy recovery 구현
- [ ] unavailable fallback과 중단 복구 state machine 통합
- [ ] provider별 profile·전이·복구 회귀 테스트 추가

## Decision Log

- session 호환성은 설정 alias나 level이 아니라 provider와 실제 Model 식별자로 판단한다. effort는 호출별 선택값이므로 동일 Model session 재개의 조건에서 제외한다.
- 목표 Model 변경을 먼저 저장한 뒤 새 session을 시작해 실패나 중단이 과거 Model session으로의 암묵적 rollback을 만들지 않게 한다.
- 새 session에는 대화 전체를 복사하지 않고 worktree와 저장된 제품 상태를 원본으로 G-040의 bounded recovery context만 전달한다.
