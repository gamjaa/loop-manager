---
id: G-225
title: 빠른 Planner 실제 시작의 차단 재개 알림 연결
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] Timely Progress Notifications.md
  - docs/requirements/[R-034] Notified Block Resumption Alert.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

빠른 Planner가 통지된 차단 해제 뒤 실제 시작한 경우에도 기존 사건의 재개 알림을 전달한다. 현재 `agents/invocation_service.py`의 process callback과 `notifications/events.py`의 역할 gate는 `planner_check`를 제외한다. `planning/agent_ports.py`는 이 역할로 실제 호출하므로, 빠른 점검이 프로젝트의 첫 정상 작업이면 물리적 시작이 차단 사건에 연결되지 않는다. 일반 Planner 시작 알림을 복원하지 않는다.

## Acceptance Criteria

- 전송이 확인된 프로젝트 차단 또는 planning 차단이 정상 해제된 뒤 빠른 Planner가 실제 시작하면, 해당 사건에 이전 scope·원인과 실제 시작한 작업을 연결하고 무음 재개 알림을 한 번 만든다. 해당 정상 작업의 시작 전에는 알리지 않는다.
- 일반·빠른·유지보수 Planner의 평상시 시작, Reviewer 시작·종료, 일시 오류의 자동 재시도에는 새 시작·재개 알림이 없다. 다른 계획의 독립 진행이 미해결 planning 사건을 소비하지 않는다.
- 역할의 알림 연결은 호출 권한과 분리된다. `planner_check`의 read-only 실행, profile·session 역할 검사, invocation ledger·usage·격리 감사와 결과 판정은 그대로 보존한다.
- 프로세스 실행 전 거부·launch 실패·미전송 차단·단순 차단 해제에는 재개 알림이 없다. 부분 전송 확인과 해제 중의 전송 경쟁, 같은 시작의 반복 callback·재시작은 기존 incident·outbox 경계로 처리하고 일반 Worker 시작과 중복하지 않는다.
- 확정한 알림은 owning transaction의 commit 뒤 같은 실행에서 전송을 시도한다. 전송 실패·비활성화가 실제 호출과 기존 상태 보존을 막지 않으며, 기존 outbox 내용·주의 수준·부분 전송 이력은 변경하지 않는다.

## Implementation

- invocation service의 물리적 시작 callback과 notification의 역할 해석에 빠른 Planner를 연결한다. 알림에 필요한 Planner 동작 분류만 기존 incident 소비자로 전달하고, provider request·실행 권한·저장된 원래 역할을 일반 Planner로 바꾸지 않는다.
- 기존 `record_work_start`·process callback·commit 후 flush와 provider double을 재사용한다. 선택·scope 준비·session 등록을 물리적 시작으로 취급하거나 별도 시작 저장소를 추가하지 않는다.
- R-053의 새 사건 제외 정책과 R-034의 통지 확인 조건을 함께 적용한다. 구현으로 영향받는 ARCHITECTURE의 알림 책임 설명만 필요한 경우 맞추며 작업 선택·차단 해제 정책을 변경하지 않는다. G-189는 이 교정 Merge 뒤 전체 재개 소비자를 감사한다.

## Validation

- Worker는 `tests/test_agent_invocation.py`의 `ProcessStartNotificationTests`와 빠른 Planner ledger case를 재사용해 실제 invocation service와 provider의 시작 callback을 잇는 최소 unit case를 실행한다. 실제 SQLite에 차단 전송 확인·해제를 준비하고 callback 전/후 outbox, 한 번의 재개, read-only·role·session·usage 보존을 검사한다. Git·Agent·Telegram 실행은 double로 처리한다.
- `tests/test_notification_incidents.py`·`test_notification_delivery.py`의 기존 부분 전송·시작 전 거부·미전송·독립 계획·재시작 case와 조건을 대응시킨다. 새 역할 연결에 필요한 최소 case만 추가하고 일반 Planner 시작 무통지는 `test_notification_events.py`의 기존 회귀와 함께 유지한다.
- Manager 전체 verify는 Planner check의 역할·격리·입력 gate, 사건별 단일 재개와 전송 실패 중 정상 진행, 기존 outbox의 이어 전송을 통과해야 한다. 단독 event helper 호출만으로 실제 시작 연결의 성공을 판단하지 않는다.

## Progress

- [x] 2026-10-08 기준 0e887794의 R-053·R-034, invocation/Planner 호출 경로와 관련 테스트를 정적으로 대조했다. callback과 event gate의 역할 제외를 확인했으며 구현·테스트 실행은 하지 않았다.
- [ ] 실제 시작 연결과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity low: 기존 callback과 incident의 역할 연결을 국소적으로 보완하며 새 상태 전이나 복구 설계가 필요하지 않다. risk low: 실패 영향은 재개 알림의 누락·중복에 한정하고 호출 권한·ledger·차단 정책을 보존한다. 기존 감사와 코드 교정으로 복구 가능하며 priority normal이다.
- G-189의 전수 감사와 중복 구현하지 않는다. 이 계획은 확인된 빠른 Planner 연결 누락만 수정하고 G-189는 Merge 뒤 기준별 소비자 근거를 확인한다. 직접 반영된 R-053의 제외·주의 수준·Worker 중복 제거를 소급 계획하지 않는다.
