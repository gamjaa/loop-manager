---
id: G-122
title: Extract Workspace Publication and Recovery Adapters
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-121
complexity: high
risk: high
priority: critical
---

## Purpose

`loop_manager.py`의 Git/workspace 생성·source 동기화·PR 관측·checkpoint 게시·plan/project 차단 복구는 기존 domain module과 책임이 중복된다. rebase/checkpoint module에 `sys.modules[__name__]`를 넘기는 의존도 남아 있다. 기존 decision·repository를 유지하며 효과의 adapter와 복구 조정을 기능별로 정리한다.

## Acceptance Criteria

- Git 원시 작업/관측, managed workspace/runtime 증거, PR/GitHub 관측, publication/lease와 plan/project 복구의 담당을 명확히 한다. `_BlockedCheckpointAdapter`와 `_ExecPlanArchiveGitPort`를 root에서 담당 package로 옮기고 각 adapter에 필요한 의존성만 주입한다.
- `reconcile_existing_pull_request`, `resume_pr_conflict_rebase`, 저장/차단 scope 복원과 checkpoint 복구는 기존 domain service를 이용한다. root module 전체의 주입·역방향 import·거대한 범용 adapter를 제거한다.
- remote OID·workspace 소유권·현재 rebase 판정·Git 변경 receipt·path scope 등 같은 계약의 반복을 기존 module에 모은다. 관측, 판단, mutation과 SQLite 확정 순서를 명시하고 서로 다른 안전 gate를 일괄 허용하지 않는다.
- branch/worktree identity, process lock, Agent 허용 범위, 정확한 lease, checkpoint 계보, archive eligibility·hash, 현재 증거와 재검증 token을 유지한다. PR 관측만으로 validation이나 소유권을 성공으로 처리하지 않는다.
- `tests/test_scoped_blocking.py`, `tests/test_blocked_checkpoint.py`와 G-119의 큰 PR/recovery class를 plan/project gate, 원인 재검증, checkpoint adapter와 publication race별로 나눈다. fixture와 assertion을 유지한다.

## Implementation

1. G-060 대응표에서 각 Git mutation의 전후 증거와 transaction 소유자를 확인한다. `manager_git_journal`, checkpoint ownership, managed rebase observation/adoption/publication, scope decision과 기존 repository의 책임을 우선한다.
2. 낮은 수준의 Git/worktree 효과와 GitHub 관측을 adapter로 분리하고 checkpoint·archive·PR recovery service에 좁은 port로 연결한다. 전체 API를 받는 소비자를 명시적 의존성으로 바꾼다.
3. 중복 remote/HEAD/index 읽기·lease 생성·소유 증거 해석은 snapshot과 적용 시점이 같은 경우만 공유한다. scope 확인과 게시 직전의 최신 확인을 cache 공유로 생략하지 않는다.
4. plan/project 차단 복구는 원인별 기존 reconciliation에 위임한다. 원인 고유 성공 조건, 다른 차단 보존, retry/session/notification 감사 유지를 service 경계에 기록한다.
5. 큰 테스트를 원인·역할별로 나누고 Git subprocess fake·SQLite fixture는 G-119 support에서 필요한 범위만 사용한다. 실제 Git/PR 통합 테스트는 Worker가 실행하지 않는다.

## Validation

- Worker는 분리한 Git/publication/checkpoint adapter와 복구 판정의 관련 unit test만 실행한다. 정상 동작과 remote lease race, 소유 증거 누락, dirty/unmerged 상태 및 게시 중단·재관측을 선택한다.
- 원격 관측→rebase 채택→중간 checkpoint→원인 재검증→Worker/verify/Review 순서, PR 없는 경우와 PR_OPEN의 차이, 다른 차단 보존을 mock 효과의 순서와 SQLite 결과로 검증한다.
- archive 내용/identity 불일치, source 로컬 변경과 예상 밖 Git config/index flag 변경의 거부를 유지한다.
- Manager 전체 verify는 실제 Git repository의 workspace/source 동기화, rebase 충돌·재개, PR 복구, archive와 cleanup을 확인한다. Reviewer는 diff와 전달 결과로 gate 누락이나 lease·transaction 경계 약화를 판단한다.

## Progress

- [ ] Git/publication/recovery 효과·증거·transaction 대응표 확인
- [ ] adapter 분리, 전체 module 의존 제거와 같은 계약의 중복 정리
- [ ] 큰 block/checkpoint/recovery test 분할 및 targeted unit test
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: Git/PR 경계와 데이터 보호를 유지하는 책임 분리다. 운영 DB의 수동 차단 해제나 원격 변경을 이 계획의 검증으로 실행하지 않는다.
- 겉으로 같은 재검증도 phase와 증거가 다르면 통합하지 않는다. 공유할 snapshot/receipt/parser의 계약과 소비 시점을 기록한다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
