---
id: G-093
title: Planning Slot Isolation During Block Recovery
sources:
  - docs/requirements/[R-043] Planning State Preservation During Block Recovery.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

`PLAN_BLOCKED` 재검토가 차단 계획을 `projects` 실행 투영에 올린 뒤 공통 clear를 호출하면, 진행 중인 일반·유지보수 planning의 작업 공간과 오류까지 사라진다. planning이 실행 자리를 소유하는 동안 계획별 복구가 그 자리를 바꾸지 못하도록 한다.

## Acceptance Criteria

- `PLANNING`이 진행 중이거나 planning 작업·오류가 보존된 상태에서는 다른 `PLAN_BLOCKED`의 재검토, 프로젝트 차단 재검토와 PR 상태 동기화가 planning의 기준 revision·입력/시도 지문, 경로·예약/소유 기록, Planner session, Agent snapshot, 재시도·검증 결과와 일반·유지보수 게시 진행을 덮어쓰거나 지우지 않는다. 신규 작업, 예약 뒤 중단, 검증 실패 및 게시 복구 상태를 포함한다.
- 이 상태에서는 차단 계획을 project row에 임시 투영하거나 공통 active clear로 planning 필드를 정리하지 않는다. 계획별 읽기·해제 조건 판정은 허용하되 Worker·verify·Review·게시와 Git 변경 복구가 실행 자리를 필요로 하면 planning이 유효하게 종료될 때까지 미룬다. 다른 계획 하나의 성공·실패가 나머지 차단 계획이나 planning 상태를 변경하지 않는다.
- 저장소 안전 gate가 허용한 PR Merge·Close 확인과 plan row/outbox 기록은 계속할 수 있다. 안정된 PR의 변경이나 다른 계획의 복구만으로 planning을 종료하거나 새 실행을 시작하지 않는다.
- planning이 없는 정상 상태의 다중 `PLAN_BLOCKED` 해제, PR 상태 동기화 및 독립 READY 선택은 기존 동작을 유지한다. 비호환 SQLite 상태 변경이나 Git 소유권 완화 없이 적용한다.

## Implementation

- `runtime_state.py`에 planning 소유 실행 자리의 판정과 execution 투영·clear 경계를 명확히 두고, `loop_manager.py`의 `resume_plan_blocks`, `resume_project_block_if_safe`, PR 동기화 및 실행 선택 경로에서 이 경계를 사용한다. 차단 계획의 읽기·진단은 해당 `exec_plans` row를 직접 사용하고, 실행 자리 전환이 필요한 복구만 연기한다.
- 계획별 복구가 실패했을 때의 project clear가 planning 전용 필드나 공유된 worktree/Agent 값을 초기화하지 않게 한다. 일반 planning publication intent와 활성 유지보수 cycle도 같은 복구 경계에서 보존한다.
- R-043에서 확정된 planning 실행 자리·상태 보존 규칙은 구현 PR의 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 복구·투영 함수의 `tests/test_runtime_state.py`, `tests/test_scoped_blocking.py`, `tests/test_loop_manager.py` targeted unit test만 실행한다.
- `PLANNING`과 복수 `PLAN_BLOCKED`의 동시 상태에서 신규·예약·검증·게시 단계의 전후 project row, planning publication 및 유지보수 cycle을 비교하고, 계획별 미해제·해제 가능·소유권 실패가 섞여도 실행 자리가 유지되는지 검증한다.
- 전체 verify에서 planning 없는 정상 차단 해제, 허용된 PR Merge·Close 및 독립 READY 실행이 유지돼야 한다.

## Progress

- [ ] planning 실행 자리와 계획별 복구 투영 경계 분리
- [ ] PR 동기화·다중 차단·중단 상태 보존 회귀 사례

## Decision Log

- G-092는 Agent Git 기준선과 저장된 범위 차단의 증거 기반 해제를 다룬다. 이 계획은 다른 계획의 복구가 planning 소유 실행 자리를 침범하는 문제만 다룬다.
