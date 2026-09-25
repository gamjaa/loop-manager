---
id: G-092
title: Agent Baseline Reconciliation and Block Recovery
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-089
  - G-091
complexity: high
risk: high
priority: critical
---

## Purpose

Agent 시작 snapshot 뒤에 Manager의 검증된 Git 작업이 있어도 현재 기준선은 작업별 전후 증거로 갱신되지 않는다. 낡은 기준선 때문에 생긴 기존 `PLAN_BLOCKED`·`PROJECT_BLOCKED`도 정확한 과거 snapshot 복원을 요구하면 안전하게 해제할 수 없다.

## Acceptance Criteria

- G-090/G-091이 증명한 Manager 전이 중 실제 Git 상태와 일치하는 부분만 G-089의 사용 자원 기준선에 반영한다. Manager rebase로 관리 HEAD·소유 branch ref·index가 바뀐 뒤 재시작해도 원래 Agent의 허용 파일 변경과 session을 보존하고, Manager 변화와 예상 밖 영향권 변화가 섞였거나 증거가 불완전하면 해당 영향을 차단한다.
- 저장된 차단의 유일한 원인이 격리가 증명된 실행의 무관한 변화 또는 Manager 작업의 낡은 기준선이면, 현재 선택 worktree identity·소유권·허용 변경, 사용 중인 ref·Git 상태와 checkpoint를 재확인해 해당 차단만 해제한다. 격리를 증명할 수 없는 과거 실행의 무관한 변화는 외부 변경으로 추정해 해제하지 않는다.
- 다른 계획의 차단, 기존 Agent session·작업물·retry·승인 기록은 보존한다. 후보 revision이 달라진 경우 필요한 targeted test, 전체 verify와 Review를 다시 거치고 게시 전에 최신 원격 소유권을 재검증한다.
- source가 원격보다 뒤처졌거나 dirty 때문에 safe fast-forward가 불가능한 조건은 차단 해제 뒤 bootstrap freshness 또는 일반 source sync에서 독립적으로 판단한다. `PROJECT_BLOCKED` 상태의 안전 gate를 우회해 Worker나 Git 쓰기를 먼저 시작하지 않는다.

## Implementation

- `agent_scope_decision.py`와 `loop_manager.py`의 저장 snapshot 재검증에서 작업별 checkpoint를 검증한 뒤 현재 작업의 사용 자원에 한해 기준선을 조정한다. 현재의 전체 ref 복사나 관측 변화 허용을 소유권 증명으로 사용하지 않는다.
- `scoped_blocking.py`와 실행 복구 경계에 `default_worktree_mutation` 및 같은 유형의 저장된 차단을 원인별로 재평가하는 좁은 경로를 둔다. 결과는 기존 phase·workspace 소유권과 원자적으로 저장한다.
- `execution_handlers.py`의 재개 흐름에서 후보 revision 변경 시 검증 증거를 무효화하고 기존 session에서 필요한 작업을 다시 수행한다. G-081의 게시 checkpoint OID와 다른 계획의 차단 판단은 보존한다.
- 이 계획으로 확정되는 R-023의 장기 Agent 범위·Manager 증거·차단 복구 규칙은 같은 구현 PR의 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 `tests/test_agent_scope_decision.py`, `tests/test_scoped_blocking.py`, `tests/test_origin_isolation.py`, `tests/test_execution_handlers.py`에서 직접 변경한 재조정·해제 targeted unit test만 실행한다.
- Manager rebase·fetch·worktree 전후 기준선 갱신, Git/기록 저장 사이 중단, 증명된 무관 변화·낡은 `default_worktree_mutation` 해제, 격리 부재·영향권 변화 혼합·checkpoint 불일치의 차단 유지를 검증한다.
- 해제 뒤 session·작업물·다른 차단이 보존되고 후보 revision 변경 시 targeted test·전체 verify·Review가 재실행되며, 전체 verify에서 source freshness와 게시 소유권이 유지돼야 한다.

## Progress

- [ ] 검증된 Manager 전이의 Agent 기준선 반영
- [ ] 저장된 오탐 차단의 증거 기반 해제
- [ ] 재개·재검증 및 다른 차단 보존 회귀 사례

## Decision Log

- 과거 실행에 쓰기 격리 증거가 없으면 무관한 shared ref·worktree 변화를 자동으로 외부 변경이라 단정하지 않는다. G-088/G-089와 G-090/G-091의 독립 증거가 있는 경우에만 해제한다.
