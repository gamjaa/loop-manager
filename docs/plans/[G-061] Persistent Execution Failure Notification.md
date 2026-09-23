---
id: G-061
title: Persistent Execution Failure Notification
sources:
  - docs/requirements/[R-029] Persistent Execution Failure Notification.md
depends_on:
  - G-051
  - G-056
  - G-057
complexity: medium
risk: medium
priority: normal
---

## Purpose

현재 `recovery_state.py`는 단계별 복구 원인을 저장하고 `notifications.py`는 PR·BLOCKED·완료 사건을 전송한다. 최종 차단 전의 같은 실패가 외부 실행마다 반복돼도 사용자에게 운영상 조치를 알리는 독립 사건은 없다.

## Acceptance Criteria

- Planner·Worker·Reviewer, verify, 계획 게시와 Git·PR 연동에서 기존 bounded 복구가 먼저 실행된다. 같은 프로젝트·단계·구조화 원인에 실제 시도가 있었고 진전이 없는 연속 세 외부 실행 뒤 한 번 알린다.
- 확정된 설정·권한 오류는 즉시 알린다. 단계·원인 변경, 성공 또는 진전 marker가 있으면 연속 이력을 초기화한다. 한 실행 안의 재시도는 외부 실행 횟수로 계산하지 않는다.
- 같은 사건이 `PLAN_BLOCKED`·`PROJECT_BLOCKED` 통지를 만들면 별도 장애 통지를 만들지 않는다. Telegram이 꺼져도 진단 기록은 보존한다.
- 기존 복구 분류, 세션·worktree·상태 전이와 PR/차단/완료 알림의 event key·payload 및 사용자 동작은 보존한다. 추가 이력은 기존 SQLite 자료를 손상하지 않는 additive 형식으로 기록한다.

## Implementation

- `recovery_state.py`의 구조화 원인과 진전 marker를 관찰하는 프로젝트별 외부 실행 결과 저장소를 둔다. 실제 수행 여부를 scheduler 경계에서 기록하고 재시작을 거쳐 연속성을 검증한다.
- `notifications.py` outbox에 장애 사건을 멱등하게 추가하고 기존 BLOCKED 사건과 원인 identity로 중복을 조정한다. 전송 실패는 실행 상태와 분리한다.
- G-056의 명세 차단과 G-057의 verify 시간 초과는 최종 차단을 우선해 중복 사건이 되지 않도록 한다.

## Validation

- Worker는 `tests/test_recovery_state.py`, `tests/test_notifications.py`, `tests/test_loop_manager.py`의 실행 경계 관련 targeted unit test만 실행한다.
- 동일 원인의 세 외부 실행, 한 실행의 세 재시도, 중간 진전·단계 변경·즉시 오류, BLOCKED 중복 억제, Telegram 비활성·전송 재시도를 검증한다.
- 전체 verify에서 Agent session 복구, rebase correction, scoped blocking과 기존 알림의 멱등성이 유지돼야 한다.

## Progress

- [ ] 외부 실행별 실패 관찰·초기화 구현
- [ ] 장애 outbox 및 중복 방지 targeted test 추가

## Decision Log

- 알림을 위해 새로운 Agent 호출이나 Git 변경을 만들지 않는다.
