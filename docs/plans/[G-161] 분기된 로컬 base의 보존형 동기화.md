---
id: G-161
title: 분기된 로컬 base의 보존형 동기화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-056] Non-Blocking Local Base Synchronization.md
depends_on: []
complexity: high
risk: critical
priority: high
---

## Purpose

source_sync가 현재 건너뛰는 clean diverged base에 대해 확인된 로컬 전용 commit을 원격 base 위로 안전하게 재적용한다.

## Acceptance Criteria

- equal은 무변경, ahead는 로컬 작업 보존, behind는 안전한 fast-forward, clean diverged는 확인된 로컬 commit 보존 rebase로 처리한다. dirty·사용자 Git operation·소유권 불확실에서는 로컬 동기화만 보류하고 원격 입력의 유효한 자동 작업은 계속한다.
- 충돌·일시 실패·중단에서 시작 전 ref·index·파일과 Manager 소유 intent를 보존한다. 소유권이 증명된 자동 작업만 abort·재개하며 사용자 operation을 해제하거나 dirty 작업을 임의 autostash하지 않는다.
- 동일 입력의 진전 없는 반복 Git 쓰기는 없고 새 관련 원격·로컬 입력에서 재검사한다. bootstrap·handoff 실패와 프로젝트 안전 차단을 혼동하지 않는다.

## Implementation

- git/source_sync.py와 runtime/bootstrap.py의 기존 동기화·journal·handoff 경계를 재사용한다. 로컬 분기 재적용의 준비·적용·확정·복구를 명시적 상태로 결합한다.
- 로컬 base 상태를 원격 계획 입력의 gate로 다시 도입하지 않는다. 사용자가 만든 다른 branch·기본 checkout의 작업은 이 동기화 대상 밖에 둔다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_source_sync.py와 test_bootstrap_foundations.py에서 equal/ahead/behind/diverged·dirty·충돌·중단·소유 intent 판정 관련 최소 unit case를 실행한다. 필요한 실제 Git fixture를 재사용한다.
- Manager 전체 verify는 보존된 로컬 commit·index·untracked 자료, 중단 후 복구, 안전 차단과 비차단 handoff를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 여러 Git 상태와 복구 경계가 연결되어 complexity high다. 사용자 commit 손실을 막아야 하므로 risk critical이며 로컬 동기화의 실패를 전체 작업 차단으로 확대하지 않는다.

