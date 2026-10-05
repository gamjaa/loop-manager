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
- [x] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 통합·출처·재시작 판단의 결합 때문에 complexity high, 사용자 작업 손실 가능성 때문에 risk critical다. PR에 대한 checkpoint 게시와 충돌 진단은 G-167·G-168로 분리한다.
- 원격 작업 branch의 검증된 사용자 후속 입력과 로컬 전용 작업은 `recovery/work_branch_integration.py`에서 기존 baseline tree·managed rebase·SQLite checkpoint를 사용해 통합한다. staged·unstaged·untracked 원형은 baseline ref와 intent에 남기고, 작업 파일을 reset·clean하지 않고 보존 commit으로 고정한 뒤 작업 branch와 최신 base를 순서대로 반영한다. 새 루트 항목·설정·스키마는 추가하지 않는다.
- 실제 저장된 Agent 결과는 먼저 검증한다. 이후 사용자 입력의 정확한 blob과 선택 자원의 전이만 비교 기준에 반영하며, legacy 결과에서도 무관한 기본 checkout·비소유 branch의 사후 변화는 범위 밖으로 둔다. Agent 출처가 불확실한 로컬 파일·index 변경과 실제 origin·공유 자원·Agent 위반은 계속 거부한다. 변경 후보의 검증·게시 token을 무효화하고 독립 차단·session·기존 결과 감사는 보존한다.
- 새 사용자 push로 소유 rebase를 재시작할 때 충돌 파일의 tree와 모든 index stage를 ref에 보존한 뒤 해당 operation만 abort한다. 충돌 자료를 정상 구현에 무조건 적용하지 않으며, 수동 완료·중단된 확정은 현재 상태와 원격 lease를 재관측해 중복 반영을 피한다. 원격 진단 게시와 PR 확장은 이 계획에 포함하지 않는다.
- targeted 검증: `python3 -m unittest tests.test_work_branch_integration tests.test_checkpoint_adoption.BlockedCheckpointRefreshUnitTests` 32개 통과. 테스트 입력·대기 intent 기대값·Remote fixture 형식의 세 실패를 수정하고 동일 명령으로 재검증했다. `scripts/verify.sh`는 기존 전체 검증 runner의 모듈 탐색으로 새 테스트를 포함하므로 변경하지 않았다. 실제 Git 양쪽 원격 전진·rename/delete/binary·사용자 push 회귀는 추가·갱신했으며 Worker는 실행하지 않았다.
- Manager 검증 피드백 대응: work branch 채택 뒤 base rebase 시작·재시작에는 재관측한 lease를 직접 전달한다. tracking ref가 이전 OID여도 그 값을 새 원격 기대값으로 사용하지 않는다. project의 독립 scope 검사는 로컬 반영 전에 수행해 거부 시 HEAD를 보존한다.
- 사용자 입력 정규화는 scope ID·시작 digest와 완료된 격리 ledger가 있는 실행에 한정한다. 기존 Agent scope 차단은 저장된 결과를 지우지 않고 현재 상태 교정 경로로 재검사하며, 동일 HEAD에서는 ancestry Git 호출을 추가하지 않는다. 명시적인 통합 전 검사에서는 실제 저장된 Agent 결과를 계속 검증한다. rebase scope predicate는 기존 순수 경로 helper를 사용한다.
- 보존 tree를 index에 적용한 뒤 stat 정보만 refresh하고 작업 파일이 달라지면 rebase를 시작하지 않는다. 원래 index·working tree와 보존 intent는 유지한다. 관련 unit 회귀에 stale binary stat, refresh 실패, 같은 HEAD의 무명령 재평가와 scope 교정을 추가했다.
- 피드백 수정 후 targeted unit 58개(통합·checkpoint 채택 모듈, scope baseline class, legacy/Agent 위반/project 거부/소유권 관측/rebase predicate 개별 case)와 pending rebase 시작·기존 상태 재사용 개별 2개가 각각 통과했다. 이번 targeted 실행에는 실패가 없었다. 전체 verify와 실제 Git 통합 테스트는 실행하지 않았다. 전달된 Planner FUTURE_IDEAS 경계 실패는 이번 변경과 관계없는 planning 경계이므로 수정 범위에 추가하지 않았다.
