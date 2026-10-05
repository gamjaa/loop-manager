---
id: G-160
title: 복구 fixture와 Git 관측 회귀 수정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-070] 테스트 초기 환경 재사용과 Git 관측 비용 절감.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

R-070 병합 뒤 G-138의 자동 전체 verify가 보고한 복구 경계 fixture와 Git 관측 회귀를 수정하여 보존된 G-138, G-143, G-146의 정상 검증을 재개한다. 기존 격리, checkpoint, journal, 승인과 rebase 보호 조건을 유지한다.

## Acceptance Criteria

- 실패한 복구 경계 재사용은 SQLite 연결, transaction과 fixture 경로를 정확히 보존하고 실패한 복원 뒤에도 다음 변형을 독립적으로 실행한다.
- Git 관측은 index·파일·ref 상태를 바꾸지 않으며 기존 scope 판정과 staged rebase 및 승인 교정 복구를 보존한다.
- Manager가 보고한 복구 테스트 실패를 targeted test로 재현하고 수정 후 통과시킨다. 검증·Review 보호 조건이나 assertion을 약화하지 않는다.
- 전체 verify와 Reviewer는 기존 Manager 경로에서 실행하며 원래 작업 세션과 구현 결과를 보존한다.

## Implementation

- fa966f9a0c0908d5b37ef913a0592137의 실패 증거와 R-070 변경을 대조한다.
- recovery_boundary와 실제 Git 관측의 책임 경계에서 필요한 최소 수정과 재현 테스트를 추가한다.
- 별도 PR로 제출하고 기존 작업의 사용자 결정·검증 결과를 성공으로 위조하지 않는다.

## Validation

- 보고된 checkpoint, scope rebase, PR conflict, publication rebase와 Review protocol의 실패한 단일 test method만 실행한다.
- 공통 fixture 또는 관측 경계 수정에 직접 필요한 재현 unit test를 실행한다.
- Manager 전체 verify에서 복구·승인·범위·게시 회귀가 통과해야 한다.

## Progress

- [x] 운영 복구와 자동 실행에서 실제 실패 증거 확인.
- [x] TEMP recovery_external_run 상태 보존과 stat-stale index 오탐 수정, targeted test 검증.
- [ ] PR 제출과 Manager 검증·Review.

## Decision Log

- 사용자의 세 작업 정상 복구 요청에 필요한 기존 R-070 동작 회귀 수정이다. G-138의 구현 범위를 넓히지 않고 별도 branch에서 처리한다.
- 전체 verify는 직접 실행하지 않는다. 원본 운영 차단 기록과 실패·세션 감사는 유지한다.

- 실제 TEMP 복구 실행 표식 때문에 경계 저장이 실패했고, diff.autoRefreshIndex=false의 name-only 출력은 read-tree 또는 복원 직후의 stat-stale 파일을 실제 수정처럼 열거했다. TEMP tables의 schema·rows를 보존하고 numstat의 내용 확인 결과로 unstaged 경로를 수집해 안전한 읽기 전용 index 관측을 유지한다.
- 기존 verify 스크립트가 전체 테스트를 이미 검색하므로 변경하지 않는다. 전체 verify와 Reviewer는 직접 실행하지 않았다.

- Python 3.12 Manager 환경에서 실패한 단일 method 7개와 직접 관련된 새·기존 회귀 test를 합쳐 16개를 실행해 모두 통과했다. 이어서 TEMP 허용이 별도 attached DB 허용으로 넓어지지 않는 거부 test 1개도 통과했다. TEMP marker/BLOB 반복 복원, attached 상태 거부, index byte 보존, 실제 수정·binary·mode-only·특수 경로와 잘린 numstat 거부, rebase 두 차례 관측을 확인했다.

- 등록된 origin은 git@github.com:gamjaa/loop-manager.git이며 GitHub 조회에서 PRIVATE로 확인했다. branch push와 draft PR 생성은 자동 승인 검토에서 게시 승인이 명시되지 않았다는 이유로 거부되어 실행하지 않았다. 로컬 commit과 PR 본문을 준비했고, 외부 게시 승인·Manager 전체 verify·Reviewer·사용자 Merge는 남아 있다.
