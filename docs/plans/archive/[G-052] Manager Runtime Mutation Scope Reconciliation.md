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
- 구버전 부모가 mutation ID 없이 source·SHA 인자만 전달한 전환 호출은 R-020의 승인된 예외로만 처리한다. 구버전 부모 lock과 정확한 repository·`origin/main` SHA·runtime 경로·clean detached 상태를 검증하고, 예상 `origin/main` 이동과 runtime 한 항목 외의 Git 변화는 거부한다. 새 형식 호출의 소유 증명은 완화하지 않는다.
- 증명된 runtime 또는 승인된 구버전 전환 runtime 한 항목만 이전 기준선에 반영한다. 기존 Agent session, workspace, 변경 결과, retry·failure 기록과 ExecPlan 상태 및 저장 형식은 보존하고 중단 후 재실행에도 멱등하게 처리한다.
- 추가 worktree, branch·ref·config·operation 변경이 섞였거나 경로·repository·SHA·clean 상태가 다르면 기존 범위 위반 또는 bootstrap 안전 오류로 중단한다. 경로 prefix만으로 허용하거나 reset·clean·재등록하지 않는다.
- 부모·자식 CLI 계약과 Git 권한 경계, 사용자에게 보이는 정상 실행 및 차단 의미는 R-020에 명시된 경우를 빼고 유지한다.

## Implementation

- `loop_manager.py`의 `prepare_runtime_worktree`, runtime child 인자와 `validate_persisted_agent_scope`의 Git·SQLite 어댑터 사이에 현재 lock 실행에 결합된 mutation 증거를 잇는다. 저장 전후 중단을 복구할 수 있는 최소 상태만 기록하고 실제 worktree catalog로 재검증한다.
- 구버전 호출의 mutation ID 누락은 별도 전환 경로로 분기한다. 구버전 lock 표식과 실제 lock 보유, legacy 부모 인자, exact-SHA runtime 및 나머지 Git 상태를 검증한 뒤 R-020에서 승인한 한 항목만 scope 판정에 전달한다. ID가 있는 호출이나 불완전한 호출에는 이 경로를 사용하지 않는다.
- Git 검증을 마친 정확한 runtime delta만 `agent_scope_decision.py`의 `decide_persisted_scope` 판정에 전달해 새 기준선을 계산한다. 판정 모듈에 Git I/O나 SQLite 조회를 넣지 않고, 저장된 false-positive 결과는 시작 snapshot부터 다시 증명한 경우에만 해소한다.
- 진단에는 허용한 path·SHA와 거부한 delta를 남기되 비밀값은 기록하지 않는다.

## Validation

- Worker는 `tests/test_origin_isolation.py`, `tests/test_agent_scope_decision.py`와 `tests/test_loop_manager.py`의 runtime bootstrap·persisted scope 관련 targeted unit test만 실행한다.
- 실제 동일 state directory에서 SHA A scope 저장 후 SHA B runtime 등록, 같은 SHA 재사용, 등록 직후 중단, 이전 false-positive 결과 복구를 검증한다. Planner·Worker·Reviewer 및 혼합 Git 변경·dirty·attached·wrong-SHA·wrong-repository 거부도 검증한다.
- 구버전 부모의 인자와 lock을 사용한 첫 self-hosted 전환, 같은 SHA 재실행, 중단 복구를 검증한다. mutation ID만 누락된 새 형식 호출, lock 없는 호출, 불완전한 인자 및 다른 worktree·ref·config 변화는 거부한다.
- 전체 verify에서 Agent 범위 위반 차단, 기본 worktree 보존과 process lock 재개 동작이 유지돼야 한다.

## Progress

- [ ] 부모 runtime mutation 증거와 자식 검증 연결
- [ ] 중단 복구·거부 사례 targeted test 추가
- [ ] 구버전 부모 전환 예외와 관련 targeted test 구현

## Decision Log

- 이 계획은 현재 bootstrap이 소유한 등록의 귀속만 연결한다. 새 Agent Git 권한이나 임의 runtime 신뢰는 추가하지 않는다.
- G-051 이후 저장된 scope의 정책 판정은 `agent_scope_decision.py`, 실제 Git·SQLite 검증과 예외 변환은 `loop_manager.py`에 있다. runtime 증거도 이 책임 경계를 따라 전달한다.
- 등록 직후 identity 저장 전 중단도 재개해야 하므로, `git worktree add --lock --reason`으로 등록 자체에 durable intent의 실행 ID를 결합한다. 재개 시 동일 표식, repository·경로·clean detached SHA와 나머지 catalog를 모두 검증한 뒤 identity를 저장한다. 표식이 없거나 다른 외부 등록은 거부하며, 잠금은 등록 증거로 유지한다. 기존의 미확정 등록 일괄 거부 판단은 이 복구 방식으로 대체한다.

- 구버전 부모 → 신버전 자식 전환 검토: G-052 이전 revision `978af81`의 부모는 `--runtime-source`와 `--runtime-sha`만 전달하고, lock 파일에는 실행별 identity 대신 `0`을 남긴다. 부모의 `prepare_runtime_worktree`는 기존 clean detached exact-SHA worktree도 그대로 재사용한다. 따라서 신버전 자식이 관측하는 CLI·lock·Git catalog만으로 구버전 부모의 신규 등록과 외부 등록 재사용을 구분할 수 없다. 누락된 mutation ID를 자식이 합성하거나 구버전 인자를 소유 증명으로 인정하면 앞서 차단한 외부 등록 수용 문제가 재발한다.
- 전환 정책 검토 당시에는 등록 증거를 기록하는 부모 bootstrap의 선배포와 구버전 부모가 전달한 runtime을 제한적으로 신뢰하는 예외 중 사용자 결정이 필요해 구현을 중단했다.
- 사용자 결정(2026-09-24): 구버전 부모가 `--runtime-source`·`--runtime-sha`만 전달하는 경우, R-020에 기록한 제한적인 전환 예외를 허용한다. 인자 누락은 소유권 증명이 아니며 구버전 부모가 외부 등록을 재사용했을 가능성을 완전히 배제할 수 없다는 위험을 수용한다. 새 형식 부모와 일반적인 소유권 불명 runtime의 검사는 유지한다.
