---
id: G-094
title: Planning Error Input and Workspace Recovery
sources:
  - docs/requirements/[R-043] Planning State Preservation During Block Recovery.md
depends_on:
  - G-093
complexity: high
risk: high
priority: critical
---

## Purpose

보존된 planning 오류를 다른 계획 상태나 원격 SHA만으로 초기화하면 같은 입력의 Planner를 반복 실행한다. 입력 변경과 planning worktree 소유권을 독립적으로 검증해야 실제 복구 가능 여부를 결정할 수 있다.

## Acceptance Criteria

- 일반·유지보수 planning의 미해결 오류와 입력 지문은 관련 입력이 같으면 외부 실행을 3회 이상 반복하거나 프로세스를 재시작해도 유지되며 Planner를 다시 호출하지 않는다. 다른 계획의 차단 해제·PR 변경·project 실행 상태 정리와 관련 입력이 그대로인 원격 base 전진은 오류 해제 근거가 아니다.
- `planning_error`만 있고 worktree 경로가 없다는 사실도 해제 근거가 아니다. 기존 planning 경로가 있으나 예약·소유 증거가 없는 경우에는 Git 등록·HEAD·index·파일과 진단, session, 작업물을 그대로 보존하며 경로 존재만으로 채택하거나 삭제하지 않는다.
- 기존 정책이 허용하는 관련 입력 변경이나 검증된 일반·유지보수 게시 복구에서만 소유권, 작업 공간 identity와 정리 결과를 확인한 뒤 오류를 해제하고 재계획·게시 적용을 이어간다. 소유권 검증·정리·게시 재확인이 실패하면 오류, 지문, worktree와 검증 결과를 유지한다.
- planning 없는 정상 상태의 새 계획 시작과 입력 변경 재계획은 유지된다. 비호환 migration이나 근거 없는 workspace 정리를 추가하지 않는다.

## Implementation

- `planning_runtime.py`의 planning 선택, 같은 입력 오류 gate, stale planning 정리 및 publication 복구를 원인별로 분리한다. 특히 worktree 부재나 최신 base SHA만으로 오류를 지우는 경로를 제거하고, 일반·유지보수 입력 지문과 publication intent의 기존 허용 조건을 사용한다.
- `loop_manager.py`에서 PR·차단 재검토 뒤 planning 입력 판정을 실행할 때 G-093의 실행 자리 경계를 유지한다. 소유권 검증과 상태 해제는 실패 시 보존되는 transaction 경계에서 수행한다.
- R-043의 입력·workspace 복구에 관한 장기 규칙은 같은 구현 PR의 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_planning_runtime.py`, `tests/test_loop_manager.py`, `tests/test_maintenance_planning.py`의 targeted unit test만 실행한다.
- 동일 입력의 일반·유지보수 오류, 다른 `PLAN_BLOCKED`와 PR 상태 변화, 원격 base만의 전진을 3회 이상·재시작 뒤 재현해 오류·지문·workspace와 무호출을 검증한다. 예약/소유 증거가 없는 기존 경로와 cleanup 실패를 재현해 Git 상태 및 진단 보존을 확인한다.
- 허용된 입력 변경과 일반·유지보수 게시 복구가 소유권 확인 뒤에만 이어지고, 전체 verify에서 planning 없는 정상 재계획 및 PR 복구가 유지돼야 한다.

## Progress

- [x] planning 오류 해제 조건을 관련 입력·게시 증거에 결합
- [x] worktree 소유권 불명확·정리 실패 보존 검증

## Decision Log

- 오류 해제 시 알림 사건과의 원자적 결합 및 과거 중복 사건 처리는 G-095가 담당한다. 이 계획은 오류가 언제 해제 가능한지와 workspace 안전을 확정한다.
