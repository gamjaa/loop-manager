---
id: G-052
title: Manager Runtime Mutation Scope Reconciliation
sources:
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
depends_on:
  - G-051
complexity: high
risk: high
priority: critical
---

## Purpose

현재 bootstrap의 exact-SHA runtime 등록은 persisted Agent snapshot 뒤에 일어나며, 저장된 검증 경로에는 현재 실행의 Manager mutation 증명이 없다. self-hosted 재실행에서 정상 runtime 등록을 Agent 변경으로 오인하면 자동 실행이 차단되고 반복 수동 복구가 필요하다.

## Acceptance Criteria

- process lock 아래의 부모 bootstrap이 선택한 repository identity, base SHA, 정확한 runtime 경로와 등록 의도를 durable하게 전달한다. 자식은 저장된 Planner·Worker·Reviewer scope 검증 전에 실제 Git catalog의 clean detached `HEAD`와 이 증거를 대조한다.
- 증명된 runtime 한 항목만 이전 기준선에 반영한다. 기존 Agent session, workspace, 변경 결과, retry·failure 기록과 ExecPlan 상태 및 저장 형식은 보존하고 중단 후 재실행에도 멱등하게 처리한다.
- 추가 worktree, branch·ref·config·operation 변경이 섞였거나 경로·repository·SHA·clean 상태가 다르면 기존 범위 위반 또는 bootstrap 안전 오류로 중단한다. 경로 prefix만으로 허용하거나 reset·clean·재등록하지 않는다.
- 부모·자식 CLI 계약과 Git 권한 경계, 사용자에게 보이는 정상 실행 및 차단 의미는 R-020에 명시된 경우를 빼고 유지한다.

## Implementation

- `loop_manager.py`의 `prepare_runtime_worktree`, runtime child 인자와 `validate_persisted_agent_scope`의 Git·SQLite 어댑터 사이에 현재 lock 실행에 결합된 mutation 증거를 잇는다. 저장 전후 중단을 복구할 수 있는 최소 상태만 기록하고 실제 worktree catalog로 재검증한다.
- Git 검증을 마친 정확한 runtime delta만 `agent_scope_decision.py`의 `decide_persisted_scope` 판정에 전달해 새 기준선을 계산한다. 판정 모듈에 Git I/O나 SQLite 조회를 넣지 않고, 저장된 false-positive 결과는 시작 snapshot부터 다시 증명한 경우에만 해소한다.
- 진단에는 허용한 path·SHA와 거부한 delta를 남기되 비밀값은 기록하지 않는다.

## Validation

- Worker는 `tests/test_origin_isolation.py`, `tests/test_agent_scope_decision.py`와 `tests/test_loop_manager.py`의 runtime bootstrap·persisted scope 관련 targeted unit test만 실행한다.
- 실제 동일 state directory에서 SHA A scope 저장 후 SHA B runtime 등록, 같은 SHA 재사용, 등록 직후 중단, 이전 false-positive 결과 복구를 검증한다. Planner·Worker·Reviewer 및 혼합 Git 변경·dirty·attached·wrong-SHA·wrong-repository 거부도 검증한다.
- 전체 verify에서 Agent 범위 위반 차단, 기본 worktree 보존과 process lock 재개 동작이 유지돼야 한다.

## Progress

- [ ] 부모 runtime mutation 증거와 자식 검증 연결
- [ ] 중단 복구·거부 사례 targeted test 추가

## Decision Log

- 이 계획은 현재 bootstrap이 소유한 등록의 귀속만 연결한다. 새 Agent Git 권한이나 임의 runtime 신뢰는 추가하지 않는다.
- G-051 이후 저장된 scope의 정책 판정은 `agent_scope_decision.py`, 실제 Git·SQLite 검증과 예외 변환은 `loop_manager.py`에 있다. runtime 증거도 이 책임 경계를 따라 전달한다.
