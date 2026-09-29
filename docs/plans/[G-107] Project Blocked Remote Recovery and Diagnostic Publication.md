---
id: G-107
title: Project Blocked Remote Recovery and Diagnostic Publication
sources:
  - docs/requirements/[R-046] Blocked Plan Remote Reconciliation and Checkpoint Refresh.md
depends_on:
  - G-106
complexity: high
risk: critical
priority: critical
---

## Purpose

계획과 연결된 `PROJECT_BLOCKED` 중에도 검증 가능한 대상 계획의 원격 변경 복구와 안전한 작업물 보존을 수행한다. 기존 작업 branch의 게시 계보가 불확실하면 그 ref를 덮지 않는 진단 branch를 사용한다.

## Acceptance Criteria

- 프로젝트 차단은 계획별 읽기 관측과 소유권이 증명된 복구 Git 쓰기의 일괄 금지 조건이 아니다. 해당 계획이 기존 변경 자리를 점유했거나 자리가 비어 있고 작업 범위를 독립적으로 검증한 경우에만 G-106의 후속 commit 채택·rebase·원인 재검증·checkpoint 갱신을 실행한다. 다른 계획의 실행, 새 Agent 시작, 일반 Planner 및 기본 worktree 쓰기는 기존 project 안전 gate를 유지한다.
- 원래 작업 branch의 게시 계보와 마지막 원격 OID가 확인되면 관측 OID에 결합된 fast-forward 또는 PR 없는 동일 소유 branch의 검증된 rebase에 한한 정확한 lease로 게시한다. 원래 프로젝트 차단이 남으면 상태와 이유를 유지하고 원격 checkpoint 결과만 기록한다.
- 기존 branch의 계보를 증명할 수 없어도 대상 작업물의 범위·Git 상태·rebase 완료를 검증할 수 있으면 원래 ref를 덮지 않는 결정적 진단 branch에만 게시한다. 같은 plan·작업물 identity의 동일 OID는 재사용하고 다른 OID가 이미 있으면 갱신하지 않는다. 진단 ref·원본 branch·base·원인·내용 OID를 durable하게 연결하며 PR을 만들거나 원래 branch 소유권·차단 해제 근거로 삼지 않는다.
- 작업 범위·Git 상태를 검증하지 못하거나 충돌 중이면 commit·push하지 않고 작업물·세션과 구체적인 local-only 이유를 보존한다. 원격 삭제·재작성·분기, 권한·branch protection 오류, 원격 경쟁 및 불완전 legacy 증거는 예상하지 못한 ref의 force push로 우회하지 않는다.
- 원래 차단 원인만 현재 입력으로 재평가하고 독립된 프로젝트 안전 차단·사용자 결정·다른 계획 차단은 유지한다. 원인 또는 원격·작업물 입력이 같은 반복 실행은 재게시·재검증·알림을 중복하지 않고, 입력이 바뀌면 다시 처리한다.
- 진단 branch 생성·checkpoint commit·push·SQLite 기록 사이에서 중단돼도 실제 원격 OID와 저장 intent를 대조해 같은 작업물을 한 번만 게시 완료로 확정한다. 알림에는 확인된 ref·OID 또는 실제 local-only 사유를 포함한다.

## Implementation

- `loop_manager.py`의 project 안전 gate에 대상 계획과 실행 자리로 한정한 복구 경로를 두고 G-106의 원격 채택·rebase·원인 검사 서비스를 재사용한다. `scoped_blocking.py`에서는 원인별 해제를 적용하고 관련 없는 project block을 자동으로 지우지 않는다.
- `blocked_checkpoint.py`와 저장 모듈에 원래 branch 게시와 분리된 진단 branch intent·결과를 추가한다. `notifications.py`의 차단 사건은 저장된 게시 결과를 참조한다. 원격 ref 생성은 Agent 밖 Manager의 정확한 이름·부재 검사로 제한한다.
- R-046의 project 차단 예외와 안전한 게시 규칙을 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_blocked_checkpoint.py`, `tests/test_blocked_checkpoint_state.py`, `tests/test_scoped_blocking.py`, `tests/test_notifications.py` 중 관련 targeted unit test만 실행한다.
- 실제 Git 이력으로 project 차단 중 base·branch 변경과 재개, 동일 ref checkpoint, 게시 계보 불확실 시 진단 branch, 진단 ref 충돌, 원격 경쟁, 충돌 중 local-only 및 push 후 SQLite 반영 전 재시작을 검증한다.
- 전체 verify는 다른 계획의 상태·실행 자리와 기본 worktree 보존, 진단 branch의 PR 미생성·소유권 비승격, 알림 중복 방지 및 최종 검증 gate를 확인한다.

## Progress

- [ ] project 차단 중 계획별 안전 복구 경계
- [ ] 진단 branch 게시·local-only 결과 및 중단 복구

## Decision Log

- `PROJECT_BLOCKED`의 일반 실행 제한은 유지한다. 이 계획은 R-046에 명시된 검증 가능한 대상 작업의 복구·보존만 예외로 허용한다.
