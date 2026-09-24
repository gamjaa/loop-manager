---
id: G-054
title: Artifact-Tolerant PR Conflict Recovery Gate
sources:
  - docs/requirements/[R-021] Artifact-Tolerant PR Conflict Recovery Gating.md
depends_on:
  - G-051
  - G-053
complexity: medium
risk: high
priority: critical
---

## Purpose

`reconcile_existing_pull_request`가 mergeability 분류 전에 충돌 복구용 fetch와 소유권·clean 검사를 실행할 수 있다. 실제 `MERGEABLE` PR도 ignored bytecode 때문에 차단된 사례가 있다. 이 결함은 자동 실행을 차단하고 반복적인 수동 복구를 요구한다.

## Acceptance Criteria

- PR state와 URL·branch·head OID identity를 먼저 판정한다. 저장된 소유 PR의 `MERGEABLE`·`UNKNOWN`은 충돌 복구용 fetch, worktree 검사, rebase, Agent 호출과 push 없이 `PR_OPEN`으로 유지한다.
- 명시적 `CONFLICTING`에서만 소유권 preflight를 수행한다. local·remote·PR head, workspace, index, tracked/non-ignored 변경과 Git operation을 확인하며, 재기록될 tracked 경로와 겹치지 않는 ignored artifact는 보존한다.
- 혼합 변경이나 OID·소유권 불명은 기존 차단·보존 규칙을 적용한다. 이미 진행 중인 복구와 기존 false-positive 차단 해제는 동일 PR identity 증명 아래 재개한다.
- PR 저장 형식, 한 PR 한 branch 계약, Merge/Close 상태 전이, 사용자 알림 의미와 게시 금지 경계는 유지한다.

## Implementation

- `reconcile_existing_pull_request`의 상태·identity·mergeability 선택을 Git 쓰기 경계 앞에 놓고, `_validate_pr_conflict_recovery_ownership`은 `CONFLICTING` 경로에서만 호출한다.
- G-053의 ignored 분류를 재사용하되 충돌 경로 중첩 검사는 별도로 유지한다. 검사 실패는 worktree를 수정하지 않고 구조화된 원인으로 보존한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 PR reconciliation·conflict recovery 관련 targeted unit test만 실행한다.
- ignored cache가 있는 `MERGEABLE`·`UNKNOWN`의 무변경 유지, `CONFLICTING`의 안전 복구, overlap artifact와 tracked/non-ignored 변경·OID 경합 차단을 검증한다.
- 전체 verify에서 PR Merge·Close 동기화, rebase 복구, 중단 재개와 알림 중복 방지 회귀가 없어야 한다.

## Progress

- [ ] PR 상태 분류를 충돌 preflight 앞에 배치
- [ ] ignored artifact 충돌 및 소유권 targeted test 보강

## Decision Log

- G-053의 ignored-only 판정이 먼저 Merge돼야 PR preflight와 Agent scope가 서로 다른 artifact 정책을 적용하지 않는다.
