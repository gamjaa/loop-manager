---
id: G-095
title: Durable Planning Block Incident Reconciliation
sources:
  - docs/requirements/[R-043] Planning State Preservation During Block Recovery.md
depends_on:
  - G-094
complexity: medium
risk: high
priority: critical
---

## Purpose

planning 오류가 잘못 초기화되면 기존 BLOCKED 알림 사건도 해제되고 같은 원인의 새 사건이 생성된다. 오류의 실제 해제·재개와 알림 사건을 한 상태 전이로 취급해 반복 알림과 허위 재개를 막는다.

## Acceptance Criteria

- 동일한 미해결 planning 오류를 반복 관측하거나 다른 계획·PR 상태만 변경해도 같은 차단 사건을 유지하고 새 BLOCKED 알림을 생성하지 않는다. 배포 전 이미 중복 사건이 있는 경우 기존 사건·전송 감사 이력을 삭제하지 않고 현재 미해결 오류의 추가 분할을 막는다.
- G-094가 확인한 실제 planning 오류 해제와 해당 차단 사건 해제는 원자적으로 기록된다. 두 기록 사이의 의도적 중단·재시작에도 오류가 남은 채 사건만 해제되거나, 오류가 해제됐는데 동일 사건의 새 BLOCKED가 만들어지지 않는다. 소유권·정리 실패 때는 둘 다 미해결로 남는다.
- BLOCKED 전송이 확인된 사건은 유효한 해제 뒤 해당 Planner가 실제로 시작할 때만 재개 알림을 사건당 한 번 만든다. 단순 상태 초기화, 다른 계획의 시작과 PR 처리에는 만들지 않는다. 이후 Planner가 다시 같은 원인으로 차단되면 새 사건을 한 번 만들고 반복 관측에서는 추가하지 않는다.
- 전송 실패의 기존 조각 재시도, 일부 전송 뒤 재개, 미전송 차단의 취소 및 Telegram 비활성화 동작을 유지한다. 차단·재개 알림의 중복 제거가 다른 계획의 알림을 합치지 않는다.

## Implementation

- `runtime_state.py`의 planning 오류 갱신과 `notifications.py`의 `release_block`, `_queue_block`, `record_work_start`를 연결해 오류 해제, 기존 incident release, outbox 취소를 같은 transaction으로 처리한다. 모든 planning 오류 갱신 경로가 이 경계를 사용하도록 확인한다.
- 기존 `notified_block_incidents`에 같은 planning 원인의 과거 중복·허위 release가 있어도 감사 row를 보존하면서 현재 미해결 사건을 결정적으로 식별한다. 진짜 재개 뒤 재발은 별도 사건으로 구분하고, 단순 원격 SHA·ExecPlan 변경은 사건 identity에 넣지 않는다.
- R-043의 차단·재개 알림 장기 규칙을 같은 구현 PR의 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_notifications.py`, `tests/test_runtime_state.py`, `tests/test_loop_manager.py`의 targeted unit test만 실행한다.
- 과거 중복 전송 이력이 있는 미해결 오류, 3회 이상 반복 실행·재시작, 오류/incident 전이 사이 fault injection, 실제 Planner 시작·재발·다른 계획 시작을 검증한다.
- 전체 verify에서 알림 전송 실패 재시도·부분 전송 재개·Telegram 비활성화와 정상 plan/project 차단 알림이 유지돼야 한다.

## Progress

- [ ] planning 오류와 차단 사건의 원자적 해제
- [ ] 과거 중복 사건 보존 및 실제 재개·재발 회귀 사례

## Decision Log

- 기존 감사 사건을 삭제하거나 과거 전송을 취소하지 않는다. 미해결 planning 오류의 현재 사건만 안정적으로 이어 붙인다.
