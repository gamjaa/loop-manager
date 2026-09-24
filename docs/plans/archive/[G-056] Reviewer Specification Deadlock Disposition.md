---
id: G-056
title: Reviewer Specification Deadlock Disposition
sources:
  - docs/requirements/[R-025] Reviewer Specification Deadlock Blocking.md
depends_on:
  - G-051
complexity: medium
risk: high
priority: normal
---

## Purpose

현재 `parse_reviewer_result`는 `PASS`·`FAIL`과 코드 이슈 ID만 수용하고, `execution_handlers.py`의 Review 실패는 Worker 수정으로 돌아간다. 명세의 누락·모순은 이 경로에서 해결할 수 없어 같은 교정이 반복된다.

## Acceptance Criteria

- Reviewer는 구현 결함과 별개인 명세 교착을 문서 경로·절, 충돌 또는 빠진 결정, 영향을 받는 구현 선택과 사용자 질문을 갖춘 구조화 결과로 반환한다.
- 유효한 교착만 해당 계획을 `PLAN_BLOCKED`로 전환하고 Worker·verify·Reviewer 자동 반복을 멈춘다. 독립 READY 작업은 기존 dependency·직렬 규칙대로 계속 진행한다.
- source 또는 ExecPlan 내용이 바뀔 때만 기존 차단 해제 규칙으로 재평가한다. 진단, 지문, 세션·worktree와 알림의 중단 복구·중복 방지 의미를 보존한다.
- 기존 `PASS`·`FAIL` 응답과 ISSUE_ID, Review 승격·재시도, SQLite의 기존 실행 상태·저장 형식은 변경하지 않는다.

## Implementation

- Reviewer prompt와 parser에 검증 가능한 명세 교착 결과를 추가하고 필수 증거가 빠진 응답은 기존 protocol correction 경로로 처리한다.
- Review phase에서 해당 결과를 구조화된 최종 plan 차단으로 매핑한다. Worker 수정 실패 fingerprint와 혼합하지 않는다.
- 통지 경로에는 실제 사용자 수정 대상 문서와 질문을 전달한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 Reviewer parser, `tests/test_execution_handlers.py`의 Review phase, `tests/test_notifications.py`의 차단 통지 관련 targeted unit test만 실행한다.
- 모순·누락, malformed 교착 보고, 정상 FAIL 교정·PASS, 중단 재개와 source 변경 해제를 검증한다.
- 전체 verify에서 Worker session 재사용, Review level 승격, scoped blocking과 알림 중복 방지 회귀가 없어야 한다.

## Progress

- [x] 구조화된 Reviewer 명세 교착 판정 추가
- [x] 계획 단위 차단·재개 targeted test 추가

## Decision Log

- Reviewer의 판정 불가 이유를 코드 결함 `FAIL`의 issue ID와 분리한다.
