---
id: G-040
title: Durable Validation Feedback Delta Delivery
sources:
  - docs/requirements/[R-018] Cache-Efficient Worker Session Continuity.md
depends_on:
  - G-039
complexity: high
risk: high
priority: low
---

## Purpose

전체 verify와 Review 원문 및 기존 실패 지문·복구 상태는 감사 원본으로 보존하면서, Worker에게는 직전 성공한 turn 이후 새로 생기거나 달라진 actionable feedback만 안정적인 identity와 revision으로 전달한다. 검증 실패를 `last_error`와 별도 feedback에 중복 삽입하거나 같은 원문을 매 turn 반복하는 현재 prompt 확장을 제거하고, session-unavailable fallback에도 제한된 결정적 recovery context를 제공한다.

## Acceptance Criteria

- 전체 verify와 Review 실행별 원문 결과, 성공·실패 분류, 관련 ExecPlan·definition revision, 생성 시각과 계산된 실패 fingerprint 또는 Review `ISSUE_ID`를 durable audit record로 보존한다. Worker prompt 크기를 줄이기 위해 원문이나 기존 복구 상태를 삭제·축소하지 않는다.
- R-008의 활성 반복 횟수와 성공 시 재시도 이력 초기화 정책은 유지하되, 해당 정책용 rolling state를 immutable 실행·feedback audit와 분리해 이전 원문과 identity를 잃지 않는다.
- verify 실패의 stable feedback identity는 stage와 결정적 실패 fingerprint를 사용하고, 처음 전달할 때 identity, occurrence 및 Worker가 수정할 수 있는 제한된 최신 진단을 포함한다.
- Review 실패는 각 `ISSUE_ID`를 별도 feedback identity로 사용하고, 처음 전달할 때 해당 issue의 제한된 actionable 설명을 함께 제공한다. 한 Review의 여러 issue를 하나의 자유 형식 원문으로만 전달하지 않는다.
- 같은 Worker session에 이전에 전달한 동일 fingerprint 또는 `ISSUE_ID`가 다시 발생하면 identity, 새 occurrence와 feedback revision을 전달하되 전체 설명은 반복하지 않는다. 설명이나 제한된 진단이 실제로 바뀐 경우에만 변경된 bounded 진단을 포함한다.
- 서로 다른 새 실패, 이전 issue의 변경된 진단과 occurrence 증가는 직전 성공한 Worker turn 이후의 delta로 결정적으로 정렬된다. 성공한 전체 verify 출력은 feedback 항목이 되거나 Worker에게 전달되지 않는다.
- 검증 실패 원문은 Worker prompt의 한 feedback channel에서만 표현한다. 같은 내용을 `last_error`, recovery 문구와 별도 validation feedback에 동시에 넣지 않으며, validation이 아닌 Worker 결과 형식 오류·rebase 충돌·guidance 결정 같은 진단은 기존 오류 channel을 유지한다.
- Worker 호출이 성공하고 결과가 유효한 turn까지만 포함된 feedback revision을 delivered로 확정한다. 호출 실패나 프로세스 중단으로 전달 여부가 불확실하면 다음 호출이 같은 identity, revision과 occurrence를 다시 포함해 중복 의미를 구분할 수 있다.
- session-unavailable fallback의 새 Worker session에는 AGENTS, 선택된 최신 ExecPlan과 현재 worktree를 다시 확인하라는 지시, 이전 Worker의 최종 결과 및 현재 미해결 feedback의 stable identity·occurrence·제한된 최신 진단으로 구성된 결정적 recovery context를 전달한다.
- recovery context와 일반 delta prompt에는 전체 이전 대화, 전체 verify·Review 원문 또는 이미 해결된 feedback을 복제하지 않는다. 저장된 원문은 prompt가 아니라 audit·fingerprint·복구 계산의 입력으로만 사용한다.
- fallback 새 session 시작이 실패하거나 중단돼도 worktree, 전체 원문, failure history, pending feedback과 전달 cursor를 보존하며 다음 실행에서 동일한 recovery contract를 사용한다.
- additive migration은 현재 `verify_result`, `review_result`, 실패 지문 history와 Worker session을 유실하지 않고 legacy 활성 작업을 안전하게 feedback record로 전환하거나 새 결과부터 기록한다.

## Implementation

- verify/Review 실행 원문과 issue별 normalized feedback을 저장하는 validation result·feedback 저장소를 orchestration에서 분리한다. full raw output, fingerprint 계산용 normalized 값, bounded Worker diagnostic과 delivery metadata의 역할을 서로 구분한다.
- Review protocol parser가 각 `ISSUE_ID`에 대응하는 actionable 설명 범위를 결정적으로 추출하게 하고, verify는 기존 full-output fingerprint를 유지하면서 Worker용 진단만 별도 크기 제한을 적용한다.
- feedback 항목에 plan, stage, stable identity, occurrence, diagnostic hash/revision, 해결 여부와 전달을 시도·확정한 G-039 invocation identity를 기록한다. Agent 응답 경계의 불확실성에서는 at-least-once 전달을 허용하되 같은 revision임을 prompt에서 명확히 한다.
- Worker prompt builder가 구조화 delta와 non-validation error를 별도 입력으로 받아 한 번만 렌더링하게 한다. `last_error` 문자열에서 validation 원문을 다시 추론하거나 failure history 전체를 매번 이어 붙이지 않는다.
- 모든 새 Worker session recovery가 재사용할 compact recovery-context builder를 만들고 현재 session-unavailable fallback에 연결한다. G-041은 Model 교체 시 같은 builder를 사용한다.
- verify/Review 성공은 active retry/blocking history를 기존 정책대로 정리하면서 unresolved feedback을 해결 상태로 전환한다. audit row와 전달 이력은 삭제하지 않는다.

## Validation

- Worker는 validation feedback 저장소, delta builder와 Worker prompt에 직접 관련된 개별 test class 또는 새 전용 test module만 targeted unit test로 실행한다.
- 첫 verify 실패가 fingerprint와 bounded 진단을 한 번 전달하고 같은 원문 반복은 identity·occurrence·revision만 전달하며, 진단 변화가 있을 때 변경분만 추가되는지 검증한다.
- 여러 Review `ISSUE_ID`의 설명 분리, 일부 issue 해결·재발, 동일 ID 설명 변경과 issue 순서 변경을 검증한다.
- 하나의 실패 원문이 Worker prompt의 `last_error`와 validation delta에 중복되지 않고 성공한 verify 출력이 전달되지 않는지 검증한다.
- 성공한 Worker turn, Agent 실패, 응답 후 저장 전 중단과 프로세스 재실행에서 delivery cursor가 손실되지 않고 재전달 항목이 동일 revision으로 식별되는지 검증한다.
- session-unavailable fallback context가 최신 plan/worktree 재확인, 이전 Worker 결과와 미해결 compact feedback만 포함하고 전체 대화·verify·Review 원문을 포함하지 않는지 검증한다.
- full raw audit 보존과 R-008의 동일 fingerprint BLOCKED 횟수, A→B→A 탐지, stage별 성공 초기화가 함께 유지되는지 검증한다.
- legacy migration과 부분 저장 상태에서 현재 worktree·session·failure history를 잃지 않고 다음 Worker 호출을 이어가는지 검증한다.
- 전체 verify에서 Worker result validation, Review level 승격, rebase 복구와 Requirement archive 재검증의 기존 Worker 반환 흐름이 유지돼야 한다.

## Progress

- [ ] 검증 원문 audit와 issue별 feedback 저장 경계 추가
- [ ] stable feedback revision·occurrence 및 delivery cursor 구현
- [ ] 중복 없는 Worker delta prompt builder 구현
- [ ] 공통 compact recovery context와 fallback 연결
- [ ] 중단·반복·해결·migration 회귀 테스트 추가

## Decision Log

- R-008의 반복 차단용 rolling history와 R-018의 감사·전달 원장을 분리해 성공 시 재시도 정책을 초기화하면서도 원문과 과거 identity를 보존한다.
- 전달 여부가 불확실할 때 exactly-once를 추정하지 않는다. stable identity와 revision을 포함한 at-least-once 전달로 Worker가 같은 issue를 새 문제로 오인하지 않게 한다.
- prompt에는 수정에 필요한 bounded 진단만 넣고 원문 보존은 SQLite audit 책임으로 둔다.
