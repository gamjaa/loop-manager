---
id: G-051
title: Persisted Agent Scope Decision Boundary
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
  - docs/requirements/[R-022] Ignored-Only Managed Worktree Tolerance.md
depends_on: []
complexity: medium
risk: high
priority: normal
---

## Purpose

`loop_manager.py`의 `validate_persisted_agent_scope`는 SQLite 조회, 관리 worktree 복구, Planner 교정·BLOCKED·Manager checkpoint별 기준선 계산과 Git 범위 판정을 한 함수에서 수행한다. 이 책임 혼합이 구체적인 구조 문제다. `_run_project_step`과 Agent 호출 경계가 이 함수를 반복 호출하므로 R-020의 Manager 소유 runtime 귀속과 R-022의 ignored artifact 정책을 추가하면 저장 상태와 허용 범위가 더 강하게 결합된다. 기존 검증 의미를 보존하면서 저장 상태 해석과 snapshot 판정을 독립적으로 시험할 수 있는 경계를 만든다.

## Acceptance Criteria

- 저장된 Agent scope의 역할, phase, 시작·결과·Manager snapshot과 허용된 cleanup 경로를 입력으로 받는 판정 단위를 분리하고, SQLite 조회·worktree 복구·실제 Git snapshot 수집은 명시적인 외부 어댑터에 둔다.
- Planner 교정, Worker, Reviewer, `PLAN_BLOCKED`와 Manager checkpoint 경로에서 허용하는 ref·worktree·파일 변화, 차단 scope와 오류 원문이 기존과 같다. 알 수 없는 역할, 누락·손상 snapshot, 소유권 불명 상태는 계속 닫힌 경계로 거부한다.
- `loop_manager.py`의 공개 계약과 사용자 동작, 저장 형식인 `GitSnapshot`·`ManagerGitCheckpoint` 직렬화 및 SQLite schema, PR·worktree·branch 소유 규칙과 사용자에게 보이는 상태 전이를 보존한다.
- 새로운 전역 가변 상태나 범위 우회 예외를 도입하지 않고, 허용 변화는 판정 결과에 명시적으로 담는다.

## Implementation

- `GitSnapshot`·`ManagerGitCheckpoint`의 자료형과 직렬화 의미를 보존한 채 순환 import 없는 정책 모듈 경계를 정한다. `_blocked_agent_local_baseline`, `_diagnosed_blocked_commit_allowances`, `_metadata_changes_after_block`과 `validate_persisted_agent_scope`의 분기에서 반복하는 snapshot 허용 계산을 옮기고, 기존 `loop_manager.py` helper 이름은 호환 표면으로 유지한다. SQLite 및 Git 호출 위치는 어댑터에 둔다.
- 현재 `validate_agent_scope`가 역할별 파일·ref·worktree 변경을 최종 검증하는 경계를 유지한다. 새 판정 결과를 받은 뒤 어댑터가 동일한 순서로 검증과 오류 전파를 수행한다.
- 기존 테스트의 공개 helper 사용과 저장 snapshot fixture를 유지하면서 순수 판정 사례를 직접 검증할 수 있게 한다.

## Validation

- Worker는 새 판정 모듈의 targeted unit test와 `tests/test_loop_manager.py`의 persisted Agent scope 관련 targeted test만 실행한다.
- Planner 교정, BLOCKED 결과, Manager checkpoint, Worker·Reviewer 재개, 잘못된 snapshot과 설명되지 않은 shared Git 변화에 대한 기존 결과를 보존하는 테스트를 추가한다.
- 전체 verify에서 Agent scope 격리, origin isolation, blocked checkpoint 및 중단 재개 회귀가 없어야 한다.

## Progress

- [x] 저장된 상태 해석과 snapshot 판정 경계 분리
- [x] 기존 범위·복구 의미를 검증하는 targeted test 추가

## Decision Log

- 현재 코드의 혼합 책임을 확인한 경로는 `loop_manager.py`의 `validate_persisted_agent_scope`, `validate_agent_scope`, `_run_project_step`이다. 함수 크기만을 근거로 한 파일 이동은 범위에서 제외한다.
