---
id: G-073
title: PR-State-First Source Synchronization
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-054
complexity: high
risk: high
priority: high
---

## Purpose

`_run_project_step`가 저장된 `PR_OPEN` project mirror를 보고 기본 worktree 동기화를 먼저 건너뛴 뒤 실제 PR 상태를 조회한다. 이미 Merge된 PR이 있어도 낡은 mirror와 다른 Open PR 때문에 원격 base 반영이 한 실행 늦어진다. 모든 Merge 대기 PR의 실제 상태를 먼저 영속화하고 갱신된 상태에서 source 동기화를 판단한다.

## Acceptance Criteria

- 프로젝트 안전 gate를 유지하면서 저장된 모든 Merge 대기 PR의 GitHub identity와 Open·Merged·Closed 상태를 source 동기화 판단 전에 확인한다. 새 Merge·Close 전이, 안전한 worktree cleanup 판정, 알림 outbox를 먼저 durable하게 기록하고 최신 ExecPlan·project 상태를 다시 읽는다.
- 새 Merge가 확인되면 `origin/<base-branch>`를 fetch한 뒤 실제 remote SHA에 대해 기본 worktree fast-forward eligibility를 평가한다. 이미 종료된 PR의 mirror, 다른 안정된 `PR_OPEN` 또는 충돌 marker만으로 평가를 생략하지 않는다. G-074가 Merge되기 전에는 기존 clean checkout 조건을 유지한다.
- Open PR의 `MERGEABLE`·`UNKNOWN` 대기와 `CONFLICTING` 소유권·lease 검증은 유지한다. 상태 조회와 cleanup 뒤 필요한 PR 충돌 복구는 기존 직렬 실행 경계에서 선택하며, 한 PR의 Merge가 다른 PR 상태나 관리 worktree를 지우지 않는다.
- 원격 fetch, GitHub 조회 또는 cleanup 증명 실패 시 성공한 상태 전이만 보존하고 미확정 상태를 Merge로 가정하지 않는다. source checkout의 사용자 변경, index·HEAD와 로컬 commit을 reset·stash·rebase·강제 checkout하지 않는다.
- 장기 제품 순서가 바뀌므로 구현 PR에서 `docs/PRODUCT.md`를 갱신한다. Architecture Change Policy에 해당하는 경계 변경은 Worker 시작 전에 사용자 승인 상태를 확인한다.

## Implementation

- `loop_manager.py`의 `synchronize_waiting_pull_requests`에서 모든 PR의 상태 관측·영속화와 충돌 복구 선택을 분리한다. `_run_project_step`의 선행 PR reconciliation 결과 및 최신 project row를 받은 뒤 `ensure_safe_base_worktree`를 호출하도록 순서를 옮긴다.
- Merge 뒤 fetch된 정확한 `origin/<base-branch>` SHA를 source synchronizer와 catalog reconciliation에 전달한다. 낡은 `row["phase"]`의 `PR_OPEN` 여부만으로 source sync를 skip하는 분기를 제거하고, 실패·재시작 시 이미 처리한 PR과 outbox를 멱등하게 복구한다.
- self handoff 판정이나 다중 프로젝트 scheduling 변경은 G-075에서 연결한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 PR 상태 동기화·project-step 순서와 `tests/test_origin_isolation.py`의 source sync 관련 targeted unit test만 실행한다.
- 이미 Merge된 단일 PR, 다중 Open PR 중 하나의 Merge, Closed PR, stale project mirror, clean behind source, GitHub·fetch 실패와 중단 재개를 검증한다. PR 상태·cleanup·outbox commit이 sync 판정보다 앞서고 다른 Open PR 및 기존 충돌 lease 규칙이 유지됨을 확인한다.
- 전체 verify에서 PR 충돌 복구, 원격 기준 catalog, cleanup pending, 기본 worktree 보존과 알림 멱등성이 유지돼야 한다.

## Progress

- [ ] PR 상태 관측·영속화와 충돌 선택 경계 분리
- [ ] 최신 상태 기반 source sync 순서 연결
- [ ] 단일·다중 PR 및 실패 targeted test 추가

## Decision Log

- PR 상태는 GitHub가, 계획 실행 상태는 SQLite가 소유한다. source sync는 둘을 조정한 뒤의 remote SHA와 최신 project 상태를 사용한다.
