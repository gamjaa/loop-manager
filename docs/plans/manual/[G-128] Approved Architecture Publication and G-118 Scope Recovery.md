---
id: G-128
title: Approved Architecture Publication and G-118 Scope Recovery
sources:
  - docs/requirements/[R-051] Remote Change Rebase and Managed Work Resumption.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

사용자가 검토 후 승인한 G-118의 ARCHITECTURE 변경을 별도 문서 commit으로 게시하고, Worker 권한을 확대하지 않고 같은 세션과 작업물을 보존해 scope 차단을 복구한다.

## Acceptance Criteria

- 승인된 ARCHITECTURE 내용만 별도 문서 게시에 포함하고 G-118에서 해당 파일을 다시 수정하도록 하는 지시를 제거한다.
- process lock 안에서 SQLite, 원래 Agent snapshot 및 작업물을 백업한다. 승인된 경로 외 파일·index·HEAD와 session/retry를 보존한다.
- 원래 결과 snapshot은 감사 자료로 보존하고 ARCHITECTURE 경로만 원래 기준선으로 복원한 결과를 기존 범위 검사에 전달한다. 다른 범위 차이는 허용하지 않는다.
- 기존 차단 재평가 함수로 실제 원인을 재검증하고 정상 IMPLEMENTING 단계로 복구한다. 전체 verify·Review 성공이나 Merge 완료를 주장하지 않는다.

## Implementation

1. 현재 원격·소유권·차단 원인·Agent snapshot을 대조하고 별도 백업을 만든다.
2. 독립 임시 clone에서 승인된 ARCHITECTURE와 G-118의 사용자 결정·문서 수정 지시를 게시한다. 기본 checkout과 관리 worktree의 Git metadata를 직접 게시에 사용하지 않는다.
3. 관리 worktree의 ARCHITECTURE만 원래 HEAD 내용으로 복원하고 저장된 결과 snapshot의 같은 dirty 경로만 원래 기준선으로 복원한다. 원본 결과와 정확한 hash·승인·백업 위치를 execution checkpoint audit에 보존한다.
4. 기존 validate_blocked_agent_scope와 scope block reconciliation으로 검증한다. Manager가 최신 원격 계획을 반영하고 기존 세션에서 이어가도록 한다.

## Validation

- 운영 상태·파일 hash·index·HEAD·session/retry의 전후 대조와 기존 범위 검사로 확인한다.
- 코드 변경이 없는 운영·문서 복구이므로 Worker unit test는 실행하지 않는다. 전체 verify와 Review는 정상 Manager 실행에 맡긴다.

## Progress

- [x] 차단 원인과 현재 파일·snapshot 복구 경계 조사
- [ ] 백업과 승인 문서 게시
- [ ] 기존 범위 검사 및 상태 복구
- [ ] 후속 Manager 실행의 실제 재개 확인

## Decision Log

- 2026-10-02: 사용자는 ARCHITECTURE diff 확인 후 “문제 없는 것 같은데 반영해도 돼. 차단 풀면 또 차단될텐데 어떻게 해야돼?”라고 승인했다. 이번 정확한 변경의 별도 반영·운영 복구 승인으로 처리하며 일반 Worker 파일 권한 확대나 자동 Merge 승인은 포함하지 않는다.
- 이 수동 기록은 자동 catalog 밖에 보존한다. 구현 완료 또는 검증·Review·Merge 완료를 주장하지 않는다.
