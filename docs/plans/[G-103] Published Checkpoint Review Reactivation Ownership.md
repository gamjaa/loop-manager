---
id: G-103
title: Published Checkpoint Review Reactivation Ownership
sources:
  - docs/PRODUCT.md
depends_on:
  - G-102
complexity: medium
risk: high
priority: critical
---

## Purpose

게시 checkpoint가 있는 `review_retry_exhausted` 계획을 기존 Review 실패 교정 경로로 재활성화할 때, 로컬 HEAD와 저장된 원격 OID가 같아도 실행 경로가 rebase 전용 `blocked_resume` 표식을 요구해 `publication_ownership_uncertain`으로 프로젝트 전체를 차단한다. 게시 소유권이 증명된 무 rebase 재활성화를 일관되게 이어간다.

## Acceptance Criteria

- PR이 없는 동일 plan·branch·관리 worktree의 게시 checkpoint identity·tree·마지막 채택 OID와 현재 원격 ref가 일치하고 로컬 HEAD·branch ref·index·작업 파일·worktree 등록이 안전하면, Review 횟수 차단 해제 직후의 무 rebase 재개에서 원격 소유권을 인정한다. rebase가 없는데 rebase 완료 표식을 만들어내지 않는다.
- 차단 해제 transaction은 게시 소유권의 기준 OID와 실행 checkpoint를 이후 실행 판정이 읽는 형태로 일관되게 보존한다. 재시작 직후와 Worker 교정 뒤에도 같은 근거를 검증하며, 새 Worker 결과의 targeted test·전체 verify·Review 이전에는 push 또는 PR을 만들지 않는다. 이후 실제 rebase가 필요하면 G-102의 별도 완료 증거와 lease 규칙을 적용한다.
- checkpoint 요청 identity·게시 OID·원격 OID, plan/branch/worktree identity, PR 상태 또는 Git 상태가 불일치하거나 소유 증거가 부족하면 기존 안전 차단을 유지한다. 이전 Review 실패 근거·횟수 감사, 작업물 및 별도 계획 차단은 보존한다.
- 이미 이 경로에서 생긴 `publication_ownership_uncertain` 프로젝트 차단은 동일한 게시 증거와 현재 Git 상태를 재검증한 경우에만 안전하게 해제한다. 프로젝트 차단 해제 뒤에도 실제 Review issue가 해결되지 않았으면 해당 계획의 교정·검증을 계속하며 다른 READY 계획의 실행을 막지 않는다.

## Implementation

- `loop_manager.py`의 `_resume_legacy_review_count_block`과 게시 checkpoint 차단 복구, `execution_handlers.py`의 `_owned_checkpoint_remote`가 무 rebase 재활성화에 같은 소유권 증거를 사용하도록 연결한다. `publication_remote_oid`만 남기고 재활성화 근거가 없는 상태를 만들지 않으며, rebase 경로의 증거 요건은 완화하지 않는다.
- 기존 checkpoint와 실패 감사는 덮어쓰지 않는다. 같은 실행 경로의 장기 규칙은 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_execution_handlers.py`, `tests/test_scoped_blocking.py` 중 관련 targeted unit test만 실행한다.
- 게시 checkpoint → Review 횟수 차단 → 재활성화 → 재시작 → Worker 교정 경로를 실제 로컬/원격 OID가 같은 조건으로 검증한다. 잘못된 identity·tree, 외부 원격 변경, 예상 밖 PR·Git 변경, 다른 계획의 checkpoint는 계속 차단하는 회귀를 포함한다.
- Manager 전체 verify는 G-102의 rebase 완료·lease 경로와 정상 게시, 실패 감사·알림 중복 방지 및 독립 READY 선택을 확인한다.

## Progress

- [ ] 무 rebase Review 차단 해제의 게시 소유권 연속성
- [ ] 기존 오탐 복구와 안전 거부 회귀

## Decision Log

- 2026-09-28: G-088의 로컬 HEAD·원격 ref·마지막 채택 OID는 같은 `cf500dd`였고 실행 checkpoint에는 `reactivated_from_head`만 있었다. G-102는 검증된 rebase 이후 로컬 HEAD가 원격 OID와 달라지는 경우를 다루므로 이 무 rebase 재활성화는 별도 계획으로 처리한다. 두 계획이 `execution_handlers.py`를 수정하므로 G-102 Merge 뒤 구현한다.
