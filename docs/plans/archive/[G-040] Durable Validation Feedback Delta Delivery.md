---
id: G-040
title: Durable Validation Feedback Delta Delivery
sources:
  - docs/requirements/[R-018] Cache-Efficient Worker Session Continuity.md
depends_on:
  - G-039
  - G-049
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

- 새 `validation_feedback.py`에 verify fingerprint와 Review `ISSUE_ID`별 normalized feedback, bounded diagnostic, revision/occurrence 및 deterministic delta/recovery-context builder를 정의한다. 원문 parsing과 prompt rendering을 분리해 같은 문자열을 여러 channel에서 재해석하지 않는다.
- 새 `validation_feedback_state.py` repository가 immutable validation result/audit, issue별 feedback lifecycle과 pending/delivered invocation cursor를 저장한다. R-008 rolling failure history는 `runtime_state.py`의 기존 실행 정책으로 유지하고 audit/delivery row와 명시적으로 연결만 한다.
- G-049 `ValidationEffects`를 raw verify/Review observation과 feedback repository port로 나눈다. `ExecutionHandlers.verify/review/run`은 typed result를 저장하고 Worker turn 전에 pending delta를 요청하며, 성공한 G-039 invocation identity에만 delivery cursor를 확정한다.
- Worker prompt builder는 non-validation error와 structured feedback delta를 별도 인자로 받아 각각 한 번만 렌더링한다. session-unavailable recovery는 공통 compact context builder를 호출하고 전체 원문·과거 대화는 repository 밖으로 전달하지 않는다.
- migration은 legacy `verify_result`, `review_result`, failure history와 Worker session을 read-only source로 사용해 안전한 initial cursor를 만들고, 해석 불가능한 과거 결과는 추정하지 않은 채 새 observation부터 기록한다.

## Validation

- Worker는 새 `tests/test_validation_feedback.py`, `tests/test_validation_feedback_state.py`와 `tests/test_execution_handlers.py`의 직접 관련 targeted case만 실행한다.
- 첫 verify 실패, 동일 fingerprint 반복, changed diagnostic과 여러 Review `ISSUE_ID`의 추가·해결·재발·순서 변경을 stable revision/occurrence table test로 검증한다.
- Worker prompt에 하나의 원문이 `last_error`와 validation delta로 중복되지 않고 성공 verify 출력·해결된 feedback·전체 raw output이 포함되지 않는지 snapshot test로 검증한다.
- Worker success, Agent failure, 응답 후 cursor 저장 전 중단과 재실행에서 pending/delivered CAS가 같은 revision의 at-least-once 전달을 보존하는지 repository test로 검증한다.
- session-unavailable recovery context가 최신 plan/worktree 재확인, 이전 Worker 결과와 unresolved compact feedback만 포함하는지 fake invocation port로 검증한다.
- immutable raw audit와 기존 동일-fingerprint block 횟수, A→B→A 및 stage별 성공 초기화가 서로 독립적으로 유지되고 legacy migration이 실행 상태를 잃지 않는지 검증한다.
- 전체 verify에서 Worker result validation, Review level 승격, rebase correction과 Requirement archive correction의 기존 반환 흐름이 유지돼야 한다.

## Progress

- [ ] validation result audit와 feedback normalization 모듈 구현
- [ ] feedback lifecycle·delivery cursor repository 및 migration 구현
- [ ] execution Validation/Agent port와 중복 없는 delta prompt 통합
- [ ] 공통 compact recovery context 및 invocation cursor 연결
- [ ] 반복·중단·해결·legacy targeted 회귀 테스트 추가

## Decision Log

- R-008의 반복 차단용 rolling history와 R-018의 감사·전달 원장을 분리해 성공 시 재시도 정책을 초기화하면서도 원문과 과거 identity를 보존한다.
- 전달 여부가 불확실할 때 exactly-once를 추정하지 않는다. stable identity와 revision을 포함한 at-least-once 전달로 Worker가 같은 issue를 새 문제로 오인하지 않게 한다.
- prompt에는 수정에 필요한 bounded 진단만 넣고 원문 보존은 SQLite audit 책임으로 둔다.
- Post-refactor revalidation: validation feedback는 G-049 phase sequencing과 분리된 domain repository/port로 둔다. `ExecutionHandlers`는 raw 원문을 `last_error`와 prompt에 조합하지 않고 typed delta와 cursor만 소비한다.
