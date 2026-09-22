---
id: G-047
title: Stable Blocked Input Baselines
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: high
priority: critical
---

## Purpose

전체 verify 또는 Review의 동일 실패로 차단된 ExecPlan이 실제 입력 변경 없이 자동 재개되는 반복을 막는다.

## Acceptance Criteria

- validation failure로 `BLOCKED`가 될 때 원격 기준 입력 지문과 관리 worktree 입력 지문을 각각 저장한다.
- 차단 전에 Worker가 ExecPlan 본문을 변경했더라도 이후 원격과 worktree 입력이 그대로면 `BLOCKED`를 유지하고 실패 이력과 Agent 세션을 보존한다.
- 차단 뒤 원격 PRODUCT, Requirement 또는 ExecPlan 입력이 변경되면 관리 worktree를 최신 원격 기준으로 rebase하고 재큐한 뒤 이전 차단 지문과 validation failure history를 제거한다.
- rebase 또는 차단 해제 검증에 실패하면 기존 차단 지문과 실패 이력을 보존한다.

## Implementation

- validation failure 전이에서 활성 작업의 base revision 입력과 현재 worktree 입력을 별도로 지문화한다.
- 기존 BLOCKED 재개 로직이 원격은 저장된 원격 지문과, worktree는 저장된 worktree 지문과 비교하도록 저장 계약을 바로잡는다.
- 차단 전 ExecPlan 변경, 무변경 대기, 원격 입력 변경 후 rebase·재큐를 하나의 회귀 테스트로 검증한다.

## Validation

- Worker는 `BoundedExecutionRecoveryTest`의 관련 unit test만 실행한다.
- 전체 verify에서 기존 validation failure history, BLOCKED scope recovery와 rebase recovery 테스트가 유지돼야 한다.

## Progress

- [x] 원격·worktree 차단 입력 지문 분리 저장
- [x] 무변경 대기와 원격 변경 rebase 회귀 테스트 추가

## Decision Log

- 차단 지문은 판정 전에 갱신하지 않는다. 실제 입력 변경으로 rebase와 재큐가 완료된 뒤 이전 지문을 제거하고, 다음 차단에서 최신 기준으로 다시 기록한다.
