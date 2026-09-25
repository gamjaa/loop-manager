---
id: G-065
title: Failure-Focused Verify Feedback
sources:
  - docs/requirements/[R-032] Focused Verify Failure Feedback.md
depends_on:
  - G-057
complexity: medium
risk: medium
priority: normal
---

## Purpose

현재 `validation_feedback.normalize_feedback`의 verify 진단은 전체 출력의 끝부분만 자른다. 실패 구간 뒤에 성공 로그가 길면 Worker가 원인을 받지 못한다. Worker에게 전달하는 진단만 식별 가능한 실패 중심으로 축약한다.

## Acceptance Criteria

- 실패 구간이 출력의 앞이나 중간에 있고 뒤에 긴 성공 로그가 이어져도 식별 가능한 실패 이름, 오류 메시지와 인접 진단, 마지막 실패 요약을 제한된 크기로 Worker에게 전달한다.
- 복수 실패는 총 크기 제한 안에서 각각의 요점을 우선 포함한다. 생략이 발생하면 잘림을 표시하고 무관한 성공 로그는 포함하지 않는다.
- 안전하게 식별할 수 없는 형식은 원인을 추측하지 않고 verify 종료 코드, 길이 제한된 출력 끝부분과 실제 잘림 여부를 전달한다. 프로젝트별 파서 설정을 요구하지 않는다.
- 원문 verify audit, 기존 실패 fingerprint·identity, 동일 실패의 재시도·차단 결과 및 성공 시 피드백 없음은 그대로 유지한다. 비밀값 가림과 durable feedback delta 계약을 유지한다.

## Implementation

- `validation_feedback.py`에 흔한 테스트·검사 출력의 명시적인 실패 marker와 요약을 보수적으로 추출하는 순수 formatter를 둔다. 단일 피드백 identity와 전체 크기 상한을 유지하고, marker가 불명확하면 bounded tail로 되돌린다.
- `execution_handlers.py`는 현재 8,000자 tail인 `str(VerifyOutput)` 대신 보존된 `fingerprint_source` 원문에서 Worker용 실패 구간을 추출한다. 원문 audit·fingerprint 계산 경로는 그대로 두고, `loop_manager.py`의 verify subprocess 종료 코드를 formatter에 전달하되 verify 실행 및 재시도 정책은 바꾸지 않는다.
- G-057의 timeout 진단도 실제 식별 가능한 정보만 전달하고, 실패 원문과 지문은 기존 저장 경계에 보존한다.

## Validation

- Worker는 `tests/test_validation_feedback.py`의 formatter 사례 및 `tests/test_execution_handlers.py`의 verify 피드백 전달 사례에 해당하는 최소 targeted unit test만 실행한다.
- 앞·중간의 실패 뒤 긴 성공 로그, 복수 실패와 잘림 표시, 인식 불가능한 출력·종료 코드, 비밀값, timeout 출력, 원문 audit와 fingerprint 불변 및 성공 피드백 부재를 검증한다.
- 전체 verify는 기존 피드백 cursor, 재시도·차단과 Review 진단 처리가 유지되는지 확인한다.

## Progress

- [ ] 실패 중심의 bounded verify formatter 구현
- [ ] 종료 코드와 원문·Worker 진단 분리
- [ ] 관련 targeted 회귀 사례 추가

## Decision Log

- 출력 marker가 모호하면 실패 원인을 만들어내지 않고 기존 tail을 종료 코드와 함께 제공한다.
