---
id: G-091
title: Managed Workspace and Rebase Mutation Evidence
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-090
  - G-081
complexity: high
risk: critical
priority: critical
---

## Purpose

관리 worktree 생성·정리와 rebase는 Agent 시작 snapshot 뒤에 HEAD·branch ref·index·등록 상태를 바꾼다. G-090의 journal에 이 작업의 전후 소유권 증거를 연결해 Agent 범위 판정에서 사용할 수 있게 한다.

## Acceptance Criteria

- process lock 아래 관리 worktree 생성·정리·rebase마다 대상 plan·branch·worktree, 실행 전 기대 HEAD/ref/index/등록 상태, 허용 전이와 실제 결과를 G-090의 durable journal에 기록한다. 새 Agent session이나 다른 계획의 작업을 해당 소유권으로 채택하지 않는다.
- Git 성공 뒤 기록 전에 중단돼도 intent, 실제 Git 상태와 작업 identity로 해당 결과를 검증·확정하거나 안전 차단한다. 관측 OID 또는 현재 worktree 등록만으로 Manager 소유권을 추정하지 않는다.
- G-081의 게시 checkpoint OID, rebase 전후 HEAD, 원격 branch 기대 OID 및 `--force-with-lease` 경계를 교차 검증한다. Manager rebase와 Agent의 허용 파일 변경이 함께 있는 경우 파일 변경을 보존하며, 외부 영향권 변화가 섞이면 증거로 인정하지 않는다.
- 검증된 작업별 전이만 G-092의 기준선 재조정 입력으로 제공한다. 이 계획 자체는 기존 `PLAN_BLOCKED`·`PROJECT_BLOCKED`를 자동 해제하지 않는다.

## Implementation

- `loop_manager.py`와 `execution_checkpoint.py`의 관리 worktree add/remove 및 rebase 경계에 G-090의 intent·결과 기록을 연결한다. 운영상 기존 cleanup pending과 활성 작업 상태 전이는 유지한다.
- 검증기는 G-081의 소유 게시 OID와 실행 checkpoint를 확인해 rebase 전후 HEAD·ref·index·등록의 변화만 Manager 결과로 산출한다. 충돌·재시작·외부 ref 경쟁은 안전하게 구분한다.
- 현재 `agent_manager_snapshot`의 Agent 결과와 허용 파일 변경은 보존하고, G-092가 작업별 검증 결과를 사용하도록 별도 조회 경계를 제공한다.

## Validation

- Worker는 `tests/test_execution_checkpoint.py`, `tests/test_rebase_correction.py`, `tests/test_origin_isolation.py`와 직접 변경한 Manager journal 검증기의 targeted unit test만 실행한다.
- worktree 생성·정리·rebase 전후 증거, rebase 충돌과 재개, Git/기록 저장 사이 중단, 예상 밖 원격 OID·등록·index 변화, 허용 파일 변경 보존 및 불완전 checkpoint 차단을 검증한다.
- 전체 verify에서 G-081의 원격 게시 lease, PR head 소유권, worktree cleanup·recovery와 Agent 범위 안전이 유지돼야 한다.

## Progress

- [ ] 관리 worktree 생성·정리의 journal 연결
- [ ] rebase 전후 소유권·OID 검증 및 중단 복구
- [ ] 기준선 재조정용 검증된 전이 제공

## Decision Log

- G-090은 source/runtime 작업과 공통 journal을 제공하고, G-081은 게시 checkpoint OID를 보존한다. 이 계획은 관리 worktree와 rebase의 소유 Git 전이만 증명한다.
