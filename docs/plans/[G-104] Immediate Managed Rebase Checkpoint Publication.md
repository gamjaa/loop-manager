---
id: G-104
title: Immediate Managed Rebase Checkpoint Publication
sources:
  - docs/requirements/[R-047] Immediate Remote Checkpoint After Managed Rebase.md
depends_on:
  - G-100
  - G-101
complexity: high
risk: critical
priority: critical
---

## Purpose

PR이 없는 동일 계획의 기존 원격 작업 branch를 관리 rebase한 뒤, 검증과 Review를 기다리는 동안 재작성된 로컬 HEAD가 원격 checkpoint와 어긋나 있다. 완료된 rebase 결과를 즉시 원격에 보존하고 다음 base rebase보다 먼저 게시 기준을 갱신한다.

## Acceptance Criteria

- 완료 journal의 실행 identity·원본 commit 재생·target base와 checkpoint generation, 계획·branch·worktree·HEAD·ref·index·작업 범위를 증명한 경우에만 중간 게시 intent를 만든다. 충돌, detached HEAD, 미해결 Git operation, 증거 부족 또는 범위 밖 변경은 push 없이 작업물과 이유를 보존한다.
- 게시 직전 정확한 원격 작업 ref와 PR 부재를 다시 확인한다. 마지막으로 채택한 원격 OID가 그대로일 때만 fast-forward하거나 그 OID를 명시한 `--force-with-lease`를 사용한다. 원격 삭제·재작성·후속 commit, PR 출현, 권한·보호 정책 거부는 기존 ref를 덮지 않는다.
- 안전한 rebase 완료 직후, 다음 Worker·전체 verify·Review·추가 base rebase보다 먼저 게시 또는 안전한 거부를 확정한다. 그 사이 base가 다시 전진해도 먼저 완료된 결과의 게시를 처리한다. 이미 같은 OID가 원격에 있으면 commit·push를 반복하지 않는다.
- push 전후와 SQLite 반영 전 중단에서 저장된 intent, 실제 원격 OID와 관리 Git 증거를 재대조해 한 번만 완료 기록을 적용한다. 일시적 실패는 같은 intent로 재개하며 예상 밖 원격 경쟁에서는 lease를 다시 추정하지 않는다.
- G-100처럼 게시되지 않은 관리 rebase가 여러 개 이어진 legacy 상태는 마지막 채택 원격 HEAD부터 현재 로컬 HEAD까지 각 실행의 원본·결과 HEAD와 완료 증거가 끊김 없이 연결될 때만 인정한다. 원격 HEAD가 로컬 HEAD의 조상이 아니거나 공통 base를 공유한다는 사실만으로는 승인하지 않는다.
- 중간 게시는 구현 완료·차단 해제·검증 성공·PR head 승인으로 취급하지 않는다. 이후 base·Worker 변경·후보 HEAD가 달라지면 최종 후보에 필요한 Worker 확인, targeted test, 전체 verify와 Review를 다시 거친다. 기존 PR, 신규 원격 branch 및 다른 계획의 branch는 이 경로에서 게시하지 않는다.

## Implementation

- `execution_handlers.py`의 관리 rebase 완료·재진입 경계에서 중간 게시를 우선 처리하고, `checkpoint_ownership.py`와 `manager_git_journal.py`의 검증된 완료 증거로 한 번 또는 여러 번의 rebase 계보를 증명한다. G-102의 단일 완료 rebase 소유권 판정을 재사용하되 게시되지 않은 연쇄를 별도로 검증한다.
- `execution_checkpoint_state.py`와 관련 SQLite 저장 경계에 게시 intent, 마지막 채택 원격 OID, 완료 journal identity, 게시 결과를 지속 저장한다. 원격 조회·push는 Agent 호출 밖의 Manager가 수행하고, `publication_resume.py`의 최종 PR 게시 판단은 중간 게시와 독립적으로 유지한다.
- R-047의 장기 게시 순서와 검증 경계는 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_execution_handlers.py`, `tests/test_execution_checkpoint_state.py`, `tests/test_manager_git_journal.py`, `tests/test_managed_rebase_lifecycle.py` 중 관련 targeted unit test만 실행한다.
- 실제 Git 이력으로 게시 checkpoint → 첫 rebase → 중간 게시 → 새 base 전진 → 두 번째 rebase를 검증한다. 첫 push 전·후 중단, 충돌 교정, 원격 OID 경쟁, lease·권한 거부, 기존 미게시 다중 rebase 증거 부족을 다룬다.
- 전체 verify는 중간 게시 이후 최종 후보의 검증·Review·PR gate가 유지되고 기존 PR 및 신규 branch가 조기 게시되지 않는지 확인한다.

## Progress

- [ ] 완료 rebase 계보와 중간 게시 intent 검증
- [ ] 원격 lease 게시·중단 복구와 최종 검증 경계 연결

## Decision Log

- G-100·G-101은 R-045의 rebase 완료·오류 차단 복구를 맡는다. 이 계획은 그 결과를 게시하는 R-047의 새 순서와 legacy 다중 rebase 게시 증거만 다룬다.
