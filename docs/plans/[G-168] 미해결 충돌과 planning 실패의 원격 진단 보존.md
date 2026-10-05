---
id: G-168
title: 미해결 충돌과 planning 실패의 원격 진단 보존
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-165
  - G-166
  - G-167
complexity: high
risk: critical
priority: critical
---

## Purpose

자동 해결하지 못한 충돌·최종 계획/프로젝트 차단·planning 실패의 작업 자료를 원래 branch 또는 별도 진단 ref에 안전하게 보존한다.

## Acceptance Criteria

- 현재 작업물·단계·원인을 원격에 남긴다. 원래 branch 게시가 불가능하면 검증된 별도 진단 보존을 시도하고 실제 게시 불가 사유·누락 범위를 기록한다. 새 PR·Draft PR을 생성하지 않는다.
- 진단 commit에서 diff·파일 원형·base/ours/theirs·관련 commit·남은 반영 작업을 확인할 수 있다. rename/delete/binary·충돌 중 수동 수정과 mode를 보존하고 원래 index·rebase를 자료 게시로 변경하지 않는다.
- 보존 대상은 선택 작업의 검증된 허용 자료다. 인증 정보·비밀값·무관한 파일을 게시하지 않으며 충돌 자료가 충분하지 않으면 완전 보존을 주장하지 않는다.
- 수집·commit·push·SQLite 기록·차단 해제의 중단/재시작에서 동일 자료를 중복 게시하지 않는다. 기록에 작업 branch·진단 ref·commit·수정 위치·남은 원인/게시 불가 사유가 있고 사용자 원격 수정 뒤 해결된 원인만 재검사한다.

## Implementation

- 기존 checkpoint_service·blocked_checkpoint_state와 rebase_file_evidence의 원형 blob 수집을 재사용한다. 원본 작업 공간을 변경하지 않는 관리 진단 작업 공간과 보존 manifest/게시 intent를 기존 경계에 최소 추가한다.
- planning 오류의 별도 저장 문맥과 계획/프로젝트 차단을 공통 보존 요청으로 연결하되 owner·scope·자료 완결성을 각각 검증한다. 진단 보존이 구현 승인이나 충돌 해결을 대신하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- checkpoint·rebase evidence·planning 오류 테스트에서 미해결 충돌 각 파일 형태, 수동 수정, 원본 index 불변, 비밀/무관 자료, 원래 branch 불가와 중단의 최소 unit case를 실행한다.
- Manager 전체 verify는 실제 Git 진단 자료 복원 가능성, 원격 경쟁·중복 게시 방지, 사용자 해결 재관측과 독립 승인·차단 보존을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 자료 수집·격리 게시·재시작의 복합 경계로 complexity high, 원형 손실·민감 자료 게시 영향으로 risk critical다. 자료 게시의 실패가 독립 구현 작업을 새로 차단하지 않는다.
