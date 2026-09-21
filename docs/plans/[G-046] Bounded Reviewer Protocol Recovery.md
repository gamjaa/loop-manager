---
id: G-046
title: Bounded Reviewer Protocol Recovery
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-008] Progress-Aware Verification and Review Recovery.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

실제 저장소 경로를 사용하는 유효한 Reviewer `ISSUE_ID`를 정확히 수용하고, Reviewer 결과 형식 오류가 Review 실패 지문 이력을 우회한 채 외부 실행마다 무제한 반복되는 경로를 차단한다.

## Acceptance Criteria

- `docs/plans/[G-025] Bootstrap Remaining Completed Requirement Archive.md`처럼 공백과 대괄호가 포함된 실제 저장소 상대 경로를 `ISSUE_ID`의 첫 구성요소로 허용한다.
- 절대 경로, 빈 경로 구성요소, `.` 또는 `..` 경로 구성요소, 역슬래시, colon과 제어문자가 포함된 경로는 거부한다.
- symbol/test와 category slug의 기존 안정성 제약, 대소문자 정규화, 복수 issue 정렬과 중복 제거를 유지한다.
- Reviewer 결과 형식 오류는 같은 Reviewer 세션에 오류 원인과 직전 결과를 전달해 교정을 요청하며 Worker를 다시 실행하거나 verify를 다시 수행하지 않는다.
- 하나의 Review 단계에서 최초 응답을 포함해 최대 세 번만 프로토콜 결과를 받으며, 계속 유효하지 않으면 원인과 마지막 결과를 보존하고 현재 ExecPlan을 `BLOCKED`로 전환한다.
- 프로토콜 교정 시도는 유효한 Review FAIL의 실패 지문, 발생 횟수와 Worker level 승격 이력을 소비하거나 초기화하지 않는다.
- 교정된 `VERDICT: FAIL`은 일반 Review 실패 경로로 한 번만 기록돼 같은 Worker 세션으로 전달되고, 교정된 `VERDICT: PASS`는 게시 단계로 진행한다.
- Reviewer prompt가 실제 repo-relative path에 공백과 대괄호를 허용하되 절대 경로와 traversal은 금지한다는 문법을 명시한다.

## Implementation

- ISSUE_ID 전체를 제한적인 단일 정규식으로 검증하지 않고 `path::symbol::category`로 분리해 path와 나머지 구성요소를 각각 검증한다.
- REVIEWING 단계의 Reviewer 호출과 scope 검증을 최대 세 번의 프로토콜 교정 loop로 감싸고, 각 시도에서 저장된 Reviewer session을 재사용한다.
- 프로토콜 오류 피드백과 직전 원문을 다음 Reviewer prompt에 포함하며 마지막 실패는 일반 Review 지문 전이가 아닌 안전 차단 경계로 보낸다.

## Validation

- Worker는 Reviewer 결과 parsing과 progress-aware Review orchestration 관련 unit test만 targeted로 실행한다.
- G-025의 실제 ISSUE_ID가 정상 FAIL로 parsing되고 대소문자 정규화되는지 검증한다.
- 절대 경로, traversal, 빈 segment, 역슬래시, colon과 제어문자를 포함한 path가 거부되는지 검증한다.
- 첫 프로토콜 오류 뒤 같은 Reviewer session의 PASS 또는 FAIL 교정이 Worker·verify 중복 없이 각각 게시 또는 일반 Review 실패로 전이되는지 검증한다.
- 세 번 연속 프로토콜 오류가 정확히 한 번 BLOCKED로 전이하고 Review 실패 이력과 level 이력을 변경하지 않는지 검증한다.
- 전체 verify에서 기존 유효 FAIL 반복, 서로 다른 issue 진행, session fallback과 Agent scope 검증이 유지돼야 한다.

## Progress

- [x] 안전한 repo-relative ISSUE_ID path 검증 구현
- [x] 같은-session Reviewer 프로토콜 교정과 최대 시도 구현
- [x] prompt 계약과 parser 회귀 테스트 추가
- [x] 교정 성공·지속 실패 상태 전이 테스트 추가

## Decision Log

- Reviewer가 반환한 G-025 issue는 실제 ExecPlan 경로를 사용했지만 기존 parser가 파일명 형식에 필수인 공백과 대괄호를 허용하지 않아 거부했다.
- 프로토콜 오류는 actionable Review FAIL이 아니므로 R-008의 `review_failure_history`와 Worker level 승격 입력에 합산하지 않는다.
- 외부 scheduler 실행마다 오류를 다시 호출하는 방식은 무제한 Agent 호출을 허용하므로 한 Review 단계 안에서 세 번의 같은-session 교정 후 명시적으로 차단한다.
- 2026-09-21 targeted validation: `BoundedExecutionRecoveryTest` 17개와 Reviewer prompt 경계 단일 테스트가 통과했다. 전체 verify는 Loop Manager가 Agent 밖에서 실행하도록 직접 실행하지 않았다.
