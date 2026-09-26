---
id: G-091
title: Managed Workspace and Rebase Mutation Evidence
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-090
  - G-081
complexity: high
risk: critical
priority: critical
---

## Purpose

관리 worktree 생성·정리와 rebase는 Agent 시작 snapshot 뒤에 HEAD·branch ref·index·등록 상태를 바꾼다. G-090의 journal에 이 작업의 전후 소유권 증거를 연결해 Agent 범위 판정에서 사용할 수 있게 한다.

## Acceptance Criteria

- process lock 아래 관리 worktree 생성·정리·rebase마다 대상 plan·branch·worktree, 실행 전 기대 HEAD/ref/index/등록 상태, 허용 전이와 실제 결과를 G-090의 durable journal에 기록한다. 새 Agent session이나 다른 계획의 작업을 해당 소유권으로 채택하지 않는다.
- Git 성공 뒤 기록 전에 중단돼도 intent, 실제 Git 상태와 작업 identity로 해당 결과를 검증·확정하거나 안전 차단한다. 관측 OID 또는 현재 worktree 등록만으로 Manager 소유권을 추정하지 않는다.
- G-081의 게시 checkpoint OID, rebase 전후 HEAD, 원격 branch 기대 OID 및 `--force-with-lease` 경계를 교차 검증한다. Manager rebase와 Agent의 허용 파일 변경이 함께 있는 경우 파일 변경을 보존하며, 외부 영향권 변화가 섞이면 증거로 인정하지 않는다.
- 검증된 작업별 전이만 G-092의 기준선 재조정 입력으로 제공한다. 이 계획 자체는 기존 `PLAN_BLOCKED`·`PROJECT_BLOCKED`를 자동 해제하지 않는다.

- 소유 게시 checkpoint가 있는 계획의 Manager rebase가 충돌로 detached HEAD에 머무르거나 다음 Loop 실행에서 재개돼도, 실행 checkpoint와 실제 rebase metadata가 소유 작업을 증명하면 `publication_ownership_uncertain`으로 오탐 차단하지 않고 기존 충돌 수정·재개 경로로 진행한다. 기록 없는 rebase, 다른 branch/worktree, 예상 밖 원격 OID와 외부 변경은 계속 거부한다. 이 경로는 G-088의 Agent 격리 구현이나 G-089의 감시 축소에 의존하지 않는다.

## Implementation

- `loop_manager.py`와 `execution_checkpoint.py`의 관리 worktree add/remove 및 rebase 경계에 G-090의 intent·결과 기록을 연결한다. 운영상 기존 cleanup pending과 활성 작업 상태 전이는 유지한다.
- 검증기는 G-081의 소유 게시 OID와 실행 checkpoint를 확인해 rebase 전후 HEAD·ref·index·등록의 변화만 Manager 결과로 산출한다. 충돌·재시작·외부 ref 경쟁은 안전하게 구분한다.
- 현재 `agent_manager_snapshot`의 Agent 결과와 허용 파일 변경은 보존하고, G-092가 작업별 검증 결과를 사용하도록 별도 조회 경계를 제공한다.

- `execution_handlers.py`의 `_owned_checkpoint_remote`부터 rebase 충돌 수정 dispatch까지, 정상 branch 검증과 소유 Manager rebase 중 detached HEAD 검증을 구분한다. 무조건 `allow_rebase`를 켜지 않고 execution checkpoint의 operation·expected HEAD·target base, 게시 OID·원격 기대 OID, 등록 worktree 및 rebase의 `orig-head`·`head-name`·`onto`를 대조한다. 기존 checkpoint도 동일한 증거를 충족할 때만 호환하며, 충돌 상태와 허용 작업물을 보존한다.

## Validation

- Worker는 `tests/test_execution_checkpoint.py`, `tests/test_rebase_correction.py`, `tests/test_origin_isolation.py`와 직접 변경한 Manager journal 검증기의 targeted unit test만 실행한다.
- worktree 생성·정리·rebase 전후 증거, rebase 충돌과 재개, Git/기록 저장 사이 중단, 예상 밖 원격 OID·등록·index 변화, 허용 파일 변경 보존 및 불완전 checkpoint 차단을 검증한다.
- 전체 verify에서 G-081의 원격 게시 lease, PR head 소유권, worktree cleanup·recovery와 Agent 범위 안전이 유지돼야 한다.

- G-088 재발 조건을 재현한다: 소유 게시 OID에서 시작한 Manager rebase가 충돌해 detached HEAD가 된 첫 실행과 process 재시작 뒤 모두 소유권 gate를 통과하고 기존 충돌 수정 경로에 도달해야 한다. `allow_rebase=True` validator 단독 통과만으로 완료하지 않는다. 소유 증거 없는 rebase·metadata 불일치·원격 경쟁은 계속 차단하고, 기존 다른 차단은 자동 해제하지 않는 회귀도 함께 검증한다.

## Progress

- [ ] 관리 worktree 생성·정리의 journal 연결
- [ ] rebase 전후 소유권·OID 검증 및 중단 복구
- [ ] 게시 checkpoint가 있는 rebase 충돌·재시작의 소유권 오탐 회귀 수정
- [ ] 기준선 재조정용 검증된 전이 제공

## Decision Log

- G-090은 source/runtime 작업과 공통 journal을 제공하고, G-081은 게시 checkpoint OID를 보존한다. 이 계획은 관리 worktree와 rebase의 소유 Git 전이만 증명한다.

- 2026-09-26: G-088에서 Manager rebase 충돌 뒤 detached HEAD를 일반 branch 검사로 거부해 프로젝트 차단이 반복됐다. 사용자는 별도 구현 대신 기존 개선 계획을 먼저 진행하도록 요청했다. G-088은 이 계획의 Merge를 기다리며, 이 계획에서는 Manager 작업 증명과 충돌 재개 gate를 먼저 수정하고 G-092의 일반 차단 자동 복구 범위는 유지한다.

- 2026-09-26: 재시작 시 Git 실행 전 continuation intent가 남아 있어도 같은 slot의 이전 확정 충돌 증거를 조회하도록 수정했다. 활성 operation ID의 결과만 제외하며, 기존 결과의 owner·session·전이 검증은 유지한다. 새 targeted unit case에서 intent 보존, 이전 충돌 증거 기반 게시 소유권 매칭, 다른 session·외부 ref 거부와 활성 결과 제외를 검증했다. 관련 복구·게시 gate 기존 case를 포함한 3개 unit test가 통과했다. 전체 verify는 Worker에서 실행하지 않았다.

- 2026-09-26: skip 완료 시 과거 reflog의 intent 표식만으로 외부 HEAD를 확정하지 않도록 최신 소유 branch reflog 한 건의 OID와 정확한 intent 완료 메시지를 관측 HEAD에 결합했다. 정상 skip과 외부 branch 이동·복귀, OID 불일치, 다른 intent, reflog 누락·조회 오류 및 기존 continue 내용 검증을 다루는 targeted unit case 2개가 통과했다. 전체 verify는 실행하지 않았다.

- 2026-09-26: 전체 verify 피드백 후 설치된 Git의 branch reflog 완료 형식이 `<intent> (finish): <branch> onto <base OID>`임을 확인했다. 이전 exact-match 검증에서 누락한 `onto`와 checkpoint의 target base OID를 포함하고 fixture를 실제 형식으로 수정했다. 잘못된 base·누락된 base 거부를 추가한 skip case와 continue case 2개가 통과했다. 전달된 통합 시나리오와 전체 verify는 Worker에서 재실행하지 않았으며 Loop Manager의 재검증이 필요하다.

- 2026-09-26: autostash가 같은 파일의 로컬 수정과 새 base 변경을 합칠 때 전체 파일 해시가 달라지는 정상 결과를 검증하도록 줄별 해시 기반 증거를 추가했다. 두 rebase snapshot 경로에서 원래 base·로컬 파일의 줄별 해시와 mode·signature를 기록하고, 비중첩 변경의 병합 결과가 실제 파일 증거와 일치할 때만 보존으로 인정한다. 원문은 journal에 기록하지 않는다. 증거가 없는 과거 기록, 외부 수정, 겹치는 변경과 mode 불일치는 기존 엄격 판정을 유지한다. 관련 unit test 5개가 통과했으며 새 모듈을 Linux verify 검사 목록에 추가했지만 전체 verify는 실행하지 않았다.

- 2026-09-26: Planner import fixture의 복사 목록에서 새 의존성 `rebase_file_evidence.py`가 누락된 것을 수정했다. 해당 case는 임시 Git 저장소의 commit·push와 Agent scope 검증을 함께 수행하는 통합 시나리오이므로 Worker에서 실행하지 않았다. 이번 변경은 테스트 fixture 목록과 이 기록뿐이며 별도로 실행할 관련 unit test는 없다. 전체 verify에서 import 및 기존 scope 위반 검증을 재확인해야 한다.

- 2026-09-26: 줄별 증거의 치환·삭제 범위를 반열린 구간으로 비교해 `[0,1)`과 `[1,2)` 같은 인접 수정을 허용했다. 길이가 없는 삽입은 같은 위치와 치환 경계에서 순서가 불명확하므로 보수적 거부를 유지한다. 양방향 인접 치환·삭제, 외부 내용 추가, 실제 겹침·삽입 모호성과 인접 변경의 journal 확정·재조회를 다루는 targeted unit case 3개가 통과했다. 전체 verify는 실행하지 않았다.

- 2026-09-26: 직접 rebase 복구와 실행 handler의 완료 검증을 공통 journal 함수로 통합했다. 두 경로 모두 최신 branch reflog의 OID·정확한 intent·target base가 일치해야 결과를 확정한다. 직접 복구에서 패치 동등성이 참이어도 외부 HEAD·외부 복귀·OID/intent/base 불일치는 intent를 보존하고 확정을 거부하는 회귀를 추가했다. 직접 복구·skip·continue unit case 3개가 통과했으며 전체 verify는 실행하지 않았다.

- 2026-09-26: 충돌 중인 rebase 결과에도 실행 표식을 필수로 요구했다. 직접 복구와 handler 모두 선택된 worktree의 최신 HEAD reflog OID 및 정확한 intent의 start/pick/continue/skip 표식을 확인한다. 같은 metadata를 가진 외부 rebase는 확정하지 않고 active intent를 보존한다. 표식·OID·operation 불일치, journal 미확정 및 기존 충돌 범위/재개를 다루는 unit case 4개가 통과했고, 중단 지점 검증을 보강한 case도 재실행하여 통과했다. 전체 verify는 실행하지 않았다.
