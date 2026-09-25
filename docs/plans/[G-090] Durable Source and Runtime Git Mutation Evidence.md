---
id: G-090
title: Durable Source and Runtime Git Mutation Evidence
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on: []
complexity: high
risk: critical
priority: critical
---

## Purpose

현재 `ManagerGitCheckpoint`는 주로 Agent 기준 snapshot과 cleanup worktree 집합을 보존하고, bootstrap 및 project fetch·source safe fast-forward·runtime worktree 생성/정리의 전후 상태를 작업별로 증명하지 못한다. 이 계획은 공통 durable journal과 source/runtime 작업의 증거를 만든다.

## Acceptance Criteria

- process lock 아래 Manager가 수행하는 bootstrap·project fetch, source safe fast-forward와 runtime worktree 생성·정리마다 대상 repository·ref/worktree, 실행 전 기대 상태, 허용된 전이, 실제 결과와 소유 작업 identity를 durable하게 연결한다. 관리 worktree 생성·정리와 rebase에는 G-091이 같은 journal을 연결한다.
- Git 작업과 결과 저장 사이에 중단되면 intent, 실제 Git 상태와 소유권 증거를 대조해 같은 작업을 재개·확정한다. 관측 OID나 작업 시간이 맞는다는 사실만으로 Manager 소유권을 부여하지 않는다. 다른 주체의 영향권 변화나 불완전한 증거는 해당 작업을 안전 차단한다.
- 기록된 전후 상태와 실제 Git 결과가 일치하는 부분만 후속 Agent 범위 판정의 기준선 후보로 제공한다. Agent의 원래 허용 파일 변경·session·실행 증거는 덮어쓰지 않는다.
- source checkout local delta는 safe fast-forward의 결과 확인에만 사용하고 다른 Agent의 변경으로 귀속하지 않는다. 기존 runtime exact SHA·process lock과 bootstrap freshness gate를 보존한다.

## Implementation

- `loop_manager.py`의 fetch·source·runtime Git 호출 경계에 작업별 intent/결과 journal을 둔다. bootstrap에서 DB가 아직 없는 경우에도 사용자 상태 디렉터리와 process lock을 사용해 durable 기록·복구가 가능하도록 한다.
- `agent_scope_decision.py`가 소비할 검증된 전이만 산출하는 좁은 검증기를 별도 모듈로 분리한다. 현재 `agent_manager_snapshot`과 cleanup 증거는 호환·감사 자료로 보존하고, 이를 단독 소유권 증명으로 사용하지 않는다.
- 저장된 runtime 및 source 작업 기록은 재시작 후 중복 Git 쓰기나 사용자 checkout의 원격 덮어쓰기를 만들지 않는다. G-091의 관리 worktree·rebase 경계는 같은 검증 계약을 사용한다.

## Validation

- Worker는 새 Manager Git 전이 검증기와 직접 변경한 `tests/test_origin_isolation.py`, `tests/test_loop_manager.py`의 fetch·source·runtime targeted unit test만 실행한다.
- fetch·source fast-forward·runtime 생성/정리 각각의 전후 일치, Git 성공/결과 저장 사이 중단, 재시작 멱등성, 영향권 외부 변경 혼합과 불완전 checkpoint의 차단을 검증한다.
- 전체 verify에서 source local 상태 보존, exact-SHA runtime과 process lock 회귀가 없어야 한다.

## Progress

- [ ] fetch·source·runtime 작업별 durable intent·결과 기록
- [ ] 실제 Git 상태와 전후 증거 대조·중단 복구
- [ ] 후속 관리 worktree 작업도 사용할 전이 검증 계약

## Decision Log

- G-081은 PR 없는 게시 branch의 소유 원격 OID를 rebase 재개까지 연결한다. G-091은 그 rebase와 관리 worktree 작업을 이 계획의 journal에 연결한다.
