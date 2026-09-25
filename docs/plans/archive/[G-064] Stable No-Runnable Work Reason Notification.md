---
id: G-064
title: Stable No-Runnable Work Reason Notification
sources:
  - docs/requirements/[R-031] No Runnable Work Reason Notification.md
depends_on:
  - G-061
complexity: high
risk: high
priority: normal
---

## Purpose

현재 `run_project_result`는 시작 가능한 작업이 없는 경우 대기 또는 완료를 반환하지만, 대기 이유를 사용자에게 요약하지 않는다. `notifications.enqueue_completion_if_ready`는 마지막 작업만 표시하고 활성 Requirement의 잔존 여부를 확인하지 않는다. 안정적인 작업 선택 결과를 근거로 완료 또는 대기 사건을 한 번 알린다.

## Acceptance Criteria

- PR 동기화, 복구, 유지보수 및 일반 Planner 필요 여부를 확인한 안정적인 선택 경계에서만 작업 소진을 판정한다. 실행 가능한 READY, 진행·재개 가능한 실행, PR 충돌 복구, 유지보수 작업 및 일시 오류 대기에는 소진 알림을 만들지 않는다.
- Planner가 추가 작업이 없음을 확인했고 활성 Requirement, 비종료 ExecPlan, 미해결 `planning_error`·프로젝트 차단 및 활성·due 유지보수 작업이 모두 없을 때만 기존 완료 사건을 보낸다. 프로젝트명, 설계상 작업 완료와 활성 Requirement 없음이라는 이유, 마지막 완료 계획을 포함한다.
- 완료 조건을 충족하지 못하고 시작 가능한 작업이 없으면 한 건의 대기 요약에 각 비종료 계획의 ID·제목과 확인된 시작 불가 이유를 담는다. 미충족 dependency의 ID·SQLite 상태, `PR_OPEN` Merge 대기, `PLAN_BLOCKED`의 구조화 원인·사용자 조치를 구분한다.
- 비종료 계획이 없지만 활성 Requirement가 남으면 ID와 확인된 archive 또는 추가 계획 대기 이유를 표시한다. 프로젝트 차단이나 `planning_error`가 선택을 막으면 실제 원인을 표시하되 동일 사건의 기존 BLOCKED 알림을 재생성하지 않는다. 미확정 원인을 추측하지 않는다.
- 동일한 안정 상태는 실행 반복·재시작 뒤에도 중복 알림이 없고, 작업 진행 또는 관련 입력·차단 원인 변경 뒤 새 안정 상태에는 한 번 다시 알릴 수 있다. 기존 PR·BLOCKED·완료 사건의 멱등성, Telegram outbox 재시도 및 작업 상태는 유지한다.
- Telegram 비활성 또는 전송 실패 시에도 완료·대기 이유를 실행 결과와 로그에서 확인할 수 있다. 장기 완료·알림 규칙이 바뀌는 부분은 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Implementation

- `loop_manager.py`의 안정적인 작업 선택 반환 경계에서 최신 원격 tree의 활성 Requirement, 비종료 catalog, dependency 상태, 계획·프로젝트 차단과 유지보수·Planner 상태를 읽어 결정적인 이유 snapshot을 만든다. 일시 정지와 진행 중인 단계는 이 경계에서 제외한다.
- `notifications.py`의 기존 완료 outbox와 별도 대기 사건을 위 snapshot에 연결한다. 사건 key는 정규화한 원인, 작업 주기와 작업 진행 뒤 갱신되는 사건 세대를 사용하고, BLOCKED 사건과 겹치는 원인은 중복 전송하지 않도록 조정한다. Telegram 비활성 상태에서도 같은 snapshot을 결과·로그에 남긴다.
- 기존 completion cycle의 마지막 완료 계획 정보를 재사용하고, 활성 Requirement·유지보수·차단 gate를 완료 판단에 추가한다. SQLite 변경이 필요하면 기존 데이터와 호환되는 additive 형태로 제한한다.

## Validation

- Worker는 `tests/test_notifications.py`의 사건·outbox 사례와 `tests/test_loop_manager.py`의 안정적 작업 선택 사례에 해당하는 최소 targeted unit test만 실행한다.
- dependency 대기, `PR_OPEN`, `PLAN_BLOCKED`, 비종료 계획 없는 활성 Requirement, `PROJECT_BLOCKED`, `planning_error`, Planner 미확인 상태, 재시작 중복 방지, 새로운 사건, Telegram 비활성·전송 실패를 검증한다.
- 전체 verify는 기존 PR·BLOCKED·완료 알림 계약과 작업 선택·유지보수·일시 오류 복구에 조기 소진 판정이 없는지 확인한다.

## Progress

- [ ] 안정적 소진 이유 snapshot 및 실행 결과 연결
- [ ] 완료 gate와 대기 outbox·멱등성 구현
- [ ] 관련 targeted 회귀 사례 추가

## Decision Log

- G-061의 장애 알림 outbox 이후에 구현해 같은 알림 저장 경계를 공유한다. 알림을 위해 Agent 또는 Git 작업을 새로 시작하지 않는다.
