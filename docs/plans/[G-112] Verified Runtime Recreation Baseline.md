---
id: G-112
title: Verified Runtime Recreation Baseline
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-127
  - G-129
complexity: medium
risk: high
priority: critical
---

## Purpose

같은 SHA의 runtime을 Manager가 삭제하고 재생성하면 잠금 mutation ID가 바뀐다. 저장된 Agent snapshot과 runtime 등록 전체의 equality 검사가 정상 재시작을 거부하는 결함을 수정한다.

## Acceptance Criteria

- 현재 runtime의 기존 process lock, clean detached exact SHA, durable 생성 표식 검증을 유지한다.
- 이전 runtime 생성 표식과 일치하는 성공 삭제 receipt 및 journal intent/result를 검증한 경우에만 잠금 ID 변경을 기준선에 반영한다.
- 잠금 외 HEAD, detached, 파일 상태 및 등록 필드 변경이나 삭제 증거 누락·변조는 허용하지 않는다.
- selected runtime만 재조정하며 다른 worktree/ref, Agent session, SQLite 실행 상태와 사용자 source 파일을 변경하지 않는다.

## Implementation

- runtime_scope.py에 읽기 전용 삭제 증거 검증을 분리한다.
- 기존 runtime 소유권 검증 뒤 검증 결과를 순수 scope decision에 전달한다.
- runtime snapshot 비교에서 검증된 잠금 변경만 허용한다.

## Validation

- Worker는 새 runtime 증거 unit test와 runtime scope decision 관련 unit test만 실행한다.
- 성공 재생성, 증거 누락·변조, 잘못된 소유자, 잠금 외 상태 변경 및 무관한 metadata 보존을 검증한다.
- 기존 전체 verify 스크립트가 새 모듈과 unit test를 포함하도록 유지하며 전체 실행은 Manager에 맡긴다.

## Progress

- [x] 삭제 증거 검증 및 좁은 scope 연결
- [x] targeted unit test: runtime 증거 5개와 runtime decision 3개, 총 8개 통과
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-01: 보고된 runtime 선검증 실패와 SQLite snapshot, runtime 생성 intent 및 삭제 receipt를 읽기 전용으로 비교했다. 같은 SHA의 정상 삭제·재생성에 따른 잠금 ID 변경을 확인했다. 기존 G-092는 더 넓은 기준선 복구 계획이며 이 수정은 현재 구현의 runtime equality 결함만 다룬다.
- source checkout 밖의 별도 관리 worktree에서 변경을 준비하며 운영 DB를 수동 수정하지 않는다.
- 2026-10-02 사용자 결정: 리팩터링 우선 요청에 따라 미착수·무소유 READY인 이 계획은 G-127 Merge 이후 진행한다. 원래 runtime 증거·거부 조건·검증 결과와 목표는 유지하고, 실행 시 리팩터링된 runtime·scope 경로를 확인한다. 과거 targeted test 기록을 새로운 revision의 검증 성공으로 사용하지 않는다.
