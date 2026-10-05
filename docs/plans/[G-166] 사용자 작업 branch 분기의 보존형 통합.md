---
id: G-166
title: 사용자 작업 branch 분기의 보존형 통합
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: high
risk: critical
priority: critical
---

## Purpose

로컬·원격 작업 branch의 정상 분기를 후속 commit ancestry만으로 거부하는 checkpoint 채택을 사용자 작업 보존형 통합으로 바꾼다.

## Acceptance Criteria

- 확인된 사용자 변경은 Agent scope 위반으로 오인하지 않는다. 원격 work branch와 base가 함께 전진해도 로컬 commit·staged·unstaged·untracked 작업을 보존해 work branch를 먼저 반영하고 최신 base를 통합한다.
- 기본 checkout의 코드·index·HEAD·base ref 변화와 Manager 비소유 branch 생성·수정·삭제는 선택 자원이 유효하면 자동 작업을 막지 않는다. 이름 유사성으로 branch 소유권을 채택하지 않는다.
- 첫 충돌·일시 실패·중단은 기존 세션·marker·작업물 보존 복구를 먼저 시도한다. 동일 원인의 무진전 반복은 제한하고 사용자 원격 push·수동 해결은 정상 실행/재시작에서 재관측해 완료한 반영을 중복하지 않는다.
- 실제 origin·공유 자원·Agent 위반·불확실한 사용자 변경은 계속 거부한다. 바뀐 후보는 기존 검증·Review·게시 gate를 충족하고 해결된 원인만 해제한다.

## Implementation

- recovery/checkpoint_recovery.py·checkpoint_service.py와 현재 managed rebase·scope provenance helper를 재사용한다. 사용자 branch 입력, local 보존 자료, 원격 lease와 반영 완료 상태를 결합한다.
- 단순 HEAD ancestry 대신 안전하게 확인한 입력·소유권·반영 증거로 재개 여부를 판단한다. 로컬 base 동기화는 R-056/G-161과 분리하고 미완료 rebase를 새 성공으로 채택하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- checkpoint 복구·managed rebase·scope 회귀에서 양쪽 원격 전진+로컬 각 변경 형태, 사용자 수동 해결, 다른 branch, 중단·재시작 관련 최소 unit case를 실행한다.
- Manager 전체 verify는 실제 Git rename/delete/binary 보존, Agent 위반 거부, 이중 관측·lease·SQLite 확정과 독립 차단 보존을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 통합·출처·재시작 판단의 결합 때문에 complexity high, 사용자 작업 손실 가능성 때문에 risk critical다. PR에 대한 checkpoint 게시와 충돌 진단은 G-167·G-168로 분리한다.
