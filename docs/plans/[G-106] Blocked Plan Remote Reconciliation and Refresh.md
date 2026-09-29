---
id: G-106
title: Blocked Plan Remote Reconciliation and Refresh
sources:
  - docs/requirements/[R-046] Blocked Plan Remote Reconciliation and Checkpoint Refresh.md
depends_on:
  - G-101
  - G-104
  - G-105
complexity: high
risk: critical
priority: critical
---

## Purpose

`PLAN_BLOCKED` 계획의 원격 작업 branch 또는 base가 바뀌면 안전한 사용자 commit과 최신 base를 관리 worktree에 반영하고, 원래 차단 원인을 현재 입력에서 다시 판단한다.

## Acceptance Criteria

- 계획별 복구는 같은 프로젝트의 변경 실행 자리가 비었거나 차단된 동일 계획이 그 자리를 점유한 때만 시작한다. 다른 활성 계획, PR 충돌 복구, planning publication 및 유지보수 작업을 선점하지 않고 관측한 입력을 다음 실행까지 보존한다.
- 작업 직전 원격 ref·PR·계획·branch·worktree·로컬 작업 범위를 재확인한다. 원격 작업 branch가 마지막 게시 checkpoint의 안전한 후속 commit이면 R-039의 보존 조건으로 먼저 채택한다. 삭제·재작성·분기, 기존 PR, 예상 밖 로컬 변경이나 fetch 경쟁은 사용자 commit과 로컬 작업물을 보존하고 정확한 이유로 중단한다.
- base가 전진했으면 계획·Requirement 내용 지문이 그대로여도 해당 관리 worktree에서 최신 base로 rebase한다. 작업 branch와 base가 모두 바뀐 경우 branch 변경을 먼저 채택한다. 원본 commit·target base·원격 OID·작업 identity·journal을 결합해 R-045 단계별 증거로 완료를 검증하고, 충돌은 기존 Worker 교정으로 넘기거나 미해결 상태를 보존한다.
- PR 없는 기존 원격 작업 branch의 rebase 완료 결과는 G-104의 중간 checkpoint가 게시되거나 안전하게 거부된 뒤 원인을 재평가한다. 원래 차단 원인만 현재 계획·근거 문서·작업물에 대해 검사하고, 다른 사용자 결정·PR reopen·소유권·안전 차단과 다른 계획의 상태는 유지한다.
- 원인이 해결됐으면 기존 세션·branch·worktree와 실패 감사를 보존해 재개하고 변경된 후보 revision에 필요한 Worker 확인·targeted test·전체 verify·Review를 다시 수행한 뒤에만 PR을 허용한다. 원인이 남으면 `PLAN_BLOCKED`와 이유를 유지하면서 안전한 현재 작업물을 기존 checkpoint 경계로 게시한다. 원격에 같은 HEAD가 이미 있으면 빈 commit·반복 push를 만들지 않는다.
- 관측, fetch, rebase, 원인 재검증, checkpoint commit·push와 SQLite 반영 사이의 중단에서 실제 Git 이력·저장 identity·마지막 처리 입력을 대조해 중복 작업이나 잘못된 해제를 막는다. 동일 입력·동일 원인의 반복은 rebase·검증·차단 알림을 반복하지 않는다.

## Implementation

- G-105의 계획별 관측 결과를 `loop_manager.py`의 차단 해제 선택과 연결하되, 현재 `_plan_block_selected_for_release`의 단일 mirror 가정에 의존하지 않고 대상 계획 ID를 명시한다. 원격 후속 commit 채택에는 기존 R-039 검사와 `checkpoint_ownership.py`를 재사용한다.
- 차단 rebase는 `manager_git_journal.py`, `execution_checkpoint_state.py`의 소유 실행·복구 경계를 사용한다. `blocked_checkpoint.py`의 기존 branch checkpoint 게시와 `scoped_blocking.py`의 원인별 해제·사건 기록을 연결하고 저장 단계별 멱등 전이를 둔다.
- 관련 R-046 장기 복구 규칙은 G-107과 함께 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_blocked_checkpoint.py`, `tests/test_execution_handlers.py`, `tests/test_scoped_blocking.py` 중 관련 targeted unit test만 실행한다.
- 실제 Git 이력으로 base만 전진, branch 후속 commit만 전진, 동시 변경, 원인 지속·해소, 동일 입력 반복, 충돌·원격 경쟁·중단 재시작을 검증한다. 사용자 결정·닫힌 PR·별도 안전 차단과 다른 계획의 차단은 유지돼야 한다.
- 전체 verify는 재개 후 최종 후보의 Worker·verify·Review·PR gate와 기존 checkpoint lease·알림 중복 방지, 기본 worktree 보존을 확인한다.

## Progress

- [ ] 실행 자리와 후속 commit 채택·관리 rebase 연결
- [ ] 원인별 재검증·checkpoint 갱신 및 단계별 복구

## Decision Log

- G-101은 R-045의 기존 오탐 차단만 복구한다. 이 계획은 새 원격 입력으로 다시 평가해야 하는 R-046의 계획별 복구를 맡는다.
