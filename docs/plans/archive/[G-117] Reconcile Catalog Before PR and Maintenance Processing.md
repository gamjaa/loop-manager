---
id: G-117
title: Reconcile Catalog Before PR and Maintenance Processing
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

최신 원격 ExecPlan 선언을 후속 처리의 입력으로 사용하는 기존 PRODUCT 규칙을 복구한다. 현재 `_run_project_step()`은 fetch 뒤 PR 동기화와 archive 사전 판단을 먼저 수행하고 일반 catalog reconciliation을 나중에 실행한다. archive eligibility는 유지보수 DB를 읽으므로 최신 선언이 반영되기 전에 오래된 path, sources, dependency 또는 유지보수 관계를 소비하거나 오류로 종료할 수 있다.

등록 origin과 Agent·작업 공간의 안전 검사를 보존하면서 `fetch → catalog 검증·DB 반영 → PR 동기화·정리와 해당 DB 변경 반영 → 차단 재평가·유지보수·Planner·Worker 판단` 순서를 확립한다. 원격이 그대로이고 처리 결과를 직접 검증할 수 있으면 중복 catalog 처리를 하지 않는다.

## Acceptance Criteria

- 등록 origin, 저장된 Agent 범위, 작업 공간 소유권과 중단된 Git 작업 등 fetch에 필요한 안전 검사를 통과한 뒤 원격 base를 fetch한다. 해당 revision의 유효한 catalog를 DB에 반영하기 전에는 최신 선언을 소비하는 PR 후속 처리, archive eligibility, 유지보수 판단과 다음 작업 선택을 시작하지 않는다.
- 실행 초기에 복구 가능한 차단 경로와 PLANNING 재개 경로에도 같은 최신 선언 경계를 적용한다. 안전 검사를 통과하지 못한 상태의 fetch·worktree 변경을 허용하지 않으며, 별도 프로젝트 안전 차단은 catalog 반영만으로 해제하지 않는다. 기존 차단 중 읽기 전용 원격 관측과 PR 조회는 보존한다.
- catalog 동기화는 원격 base의 active·archived 계획과 source·dependency graph를 검증하고 path·title·sources·depends_on·complexity·risk·priority·hash·revision의 변경분을 기존 SQLite snapshot/CAS 경계 안에서 한 transaction으로 적용한다. 최초 게시 감사 정보, 실행 상태, branch·worktree, session, 실패 이력과 planning 오류를 보존한다. 무효 catalog나 원격·소유권 race는 부분 적용하지 않는다.
- PR Merge·Close·충돌 처리와 worktree cleanup은 확인한 결과에 해당하는 DB 변경을 각각의 기존 상태 전이·소유권 경계에서 즉시 반영한다. PR 상태만 바뀌고 원격 catalog revision이 같으면 전체 catalog를 다시 읽거나 다시 적용하지 않는다.
- Manager의 내용 보존 archive 게시 뒤에는 정확한 source revision, 이동 경로·내용 hash, 게시 commit과 원격 반영을 검증한 뒤 해당 catalog 경로·hash·revision과 archive 감사 결과를 DB에 반영한다. 기존 검증 snapshot에서 결과 catalog의 유효성을 증명할 수 있는 변경만 delta로 적용하고, 원격 확인 전이나 DB CAS 불일치 시 성공으로 기록하지 않는다.
- PR Merge에 따른 base 전진, 사용자 push 또는 예상 밖 원격 변경은 작업 결과만으로 전체 선언이 같다고 추정하지 않는다. 후속 판단 직전에 최신 원격 revision을 확인하고 기존 검증 snapshot과 Git diff로 전체 catalog·source 관계의 유효성을 증명하거나 다시 reconciliation한다. 무관한 문서 변경, 새로운 계획과 dependency 변경을 누락하지 않는다.
- 동일한 검증 revision과 DB 상태에서는 중복 catalog 처리를 생략할 수 있지만 Agent 호출, verify, Review와 게시의 기존 revision/dependency gate는 유지한다. 원격 fast-forward 및 transaction 전후 중단에서도 이미 확정된 PR·cleanup·archive 결과를 중복 실행하지 않고 최신 선언으로 재개한다.
- 기본 worktree 동기화는 기존 safe fast-forward 조건과 PR 확인 순서를 유지한다. 로컬 미게시 파일은 입력으로 삼지 않는다. catalog 동기화를 위한 순서 변경이 자동 Merge, 임의 계획 삭제, 유지보수 성공 기준 전진 또는 새로운 Git 게시 권한을 만들지 않는다.

## Implementation

1. `_run_project_step()`의 초기 안전 gate, `remote_base()`, `synchronize_waiting_pull_requests()`, `synchronize_source_after_pr_reconciliation()`, `_execplan_archive_housekeeping_safe()`와 planning/execution 진입의 호출 관계를 확인한다. 프로젝트 차단과 PLANNING 조기 반환 경로도 포함해 최신 선언을 필요로 하는 소비자를 식별한다.
2. 프로젝트별 검증된 catalog revision과 DB snapshot을 전달하는 좁은 동기화 경계를 추출한다. 원격 조회·catalog 검증 및 적용·PR 상태 처리·source fast-forward의 책임을 분리하고 기존 `CatalogReconciliationService`와 repository를 재사용한다. 전역 mutable flag나 프로젝트 전체 상태 초기화로 순서를 제어하지 않는다.
3. 안전한 fetch 직후 catalog를 먼저 반영하고, 해당 결과를 PR 후속 처리·archive eligibility·유지보수 판단에 전달한다. PR 관측과 기존 Merge 상태 전이를 보존하며 catalog 검증의 Merge 증거가 필요한 경우 현재 검증된 관측과 durable 기록을 사용한다. archive 사전 판단이 선행 동기화를 재귀적으로 요구하지 않게 한다.
4. PR·cleanup 결과는 기존 repository 전이를 사용한다. archive의 검증된 rename-only 결과는 기존 publication intent와 원격 확인을 catalog snapshot에 결합해 delta 적용한다. 검증 가능한 결과와 외부 원격 변경을 구분하고 적용 전 revision·ownership CAS와 결과 catalog의 source·ID·dependency 검증을 유지한다.
5. 후속 판단 직전과 처리 중 원격 revision이 바뀌면 최신 catalog를 재확인한다. 같은 revision에서는 불필요한 재독해를 생략하고, 결과 확정 뒤 중단된 경우 같은 DB 변경을 멱등하게 채택한다. self-hosted runtime handoff와 진행 중 planning publication의 snapshot·복구 경계를 보존한다.
6. 필요한 코드 변경은 프로젝트 동기화 adapter와 catalog/PR/archive repository 경계 안에서 수행한다. 테스트는 호출 순서와 durable 상태 결과를 검증하고 내부 함수 호출 횟수만으로 안전성을 판단하지 않는다. 기존 Linux verify의 자동 unit test discovery를 유지한다.

## Validation

- Worker는 이번 변경을 위한 `tests/test_project_catalog_refresh.py`의 mock·fake·임시 SQLite unit test를 추가하고 `python3 -m unittest tests.test_project_catalog_refresh -v`를 실행한다. 기존 테스트를 수정한 경우에는 수정한 개별 test method만 추가 실행한다.
- frontmatter의 path·sources·depends_on·priority·complexity·risk 변경을 준비하고 PR 후속 처리·archive eligibility·유지보수·READY 선택이 갱신된 DB를 읽는지 확인한다. 원격 catalog가 무효이면 소비자 호출과 부분 cache 적용이 없는지 확인한다.
- 원격 revision 불변의 PR 상태 변경과 cleanup, 검증된 archive rename delta, PR Merge 또는 사용자 push로 인한 예상 밖 선언 변경을 구분한다. archive 게시 확인 전, 원격 race, SQLite ownership/CAS race와 단계별 중단·재시작을 fake port와 SQLite로 재현한다.
- 프로젝트 차단, PLANNING 재개, 기존 planning_error, 활성 Worker session, dependency 대기와 self-runtime handoff를 검증한다. 안전 검사 실패 시 Git 쓰기가 없는지, 별도 차단과 감사·session·유지보수 성공 기준이 보존되는지 확인한다.
- Manager 전체 verify는 기존 catalog reconciliation, scoped blocking, PR Merge/Close·conflict 복구, cleanup, archive 게시 복구, planning publication, source safe fast-forward와 execution revision gate 회귀를 확인한다. Worker는 전체 verify와 integration test를 직접 실행하지 않는다.

## Progress

- [x] 현재 처리 순서와 fetch 이후 catalog 반영 전 archive/유지보수 조회 경로 확인
- [x] fetch 직후 동기화와 소비자 순서 정리
- [x] PR·cleanup·archive 변경의 DB 반영과 원격 race 재검증
- [x] targeted unit test 작성·실행
- [ ] Manager 전체 verify·Reviewer 확인
- [ ] 사용자 PR Merge

## Decision Log

- 2026-10-01: 사용자가 원격 frontmatter 변경의 신속한 DB 반영을 요청하고 fetch → catalog 동기화 → PR·정리 순서 및 처리 단계에서 확인한 DB 변경의 즉시 반영에 합의한 뒤 계획 작성·push를 요청했다. 기존 PRODUCT의 원격 선언 원본·원자적 cache 동기화 규칙을 구현하는 계획이며 새 Requirement나 ARCHITECTURE 수정 없이 PRODUCT를 source로 사용한다.
- 원격이 동일하거나 Manager 결과를 기존 snapshot과 게시 증거로 검증할 수 있으면 중복 전체 동기화를 피한다. PR Merge commit이나 예상 밖 원격 전진에는 새로운 선언·source 변경이 포함될 수 있으므로 DB 상태 전이만으로 catalog 최신화를 대신하지 않는다.
- 계획 삭제 후 유지보수 집합에 남은 참조를 자동 취소하는 정책은 이 계획의 범위가 아니다. 실제로 존재하지 않는 미취소 계획을 조용히 제외하거나 MERGED로 가장하지 않는다.
- 이 게시에는 새 계획 문서만 포함한다. 구현과 targeted 검증, Manager 전체 verify·Review 및 구현 PR의 사용자 Merge는 후속 Loop Manager 실행에서 수행한다.

- 2026-10-01 Worker: fetch 뒤 PR·cleanup·archive eligibility 전에 catalog를 반영한다. 프로젝트별 검증 snapshot은 선언 변경 또는 원격 전진에서만 다시 읽으며, 검증된 archive 게시의 경로·revision과 감사 결과는 같은 transaction/CAS로 적용한다. 별도 안전 차단은 읽기 전용 PR 조회와 검증된 연결 작업의 catalog refresh를 거쳐 기존 복구 경계를 유지하고, prepared planning publication은 소유 coordinator에서 먼저 복구한다.
- Worker 검증: `python3 -m unittest tests.test_project_catalog_refresh -v`의 29건과 수정한 기존 unit test 개별 7건이 통과했다. archive publication의 기존 integration assertion은 최초 source 검증 + archive delta 반영에 맞춰 갱신했으며 Worker의 integration 실행 금지에 따라 Manager 전체 verify에 남긴다. Linux verify의 unit discovery는 유지하고 새 catalog refresh 모듈을 compile 목록에 추가했다. 전체 verify·Review·Merge는 아직 수행하지 않았다.
- 2026-10-01 Worker 재검증: verify delta `cc089cc1a78e4f4f3fac9c5fa39a8a9f8b6197e057a67bc5261e891da90cf63b`의 누락 import fixture와 maintenance 게시 복구 뒤 불필요한 재시도를 수정했다. 복구 coordinator가 선택한 Worker는 최신 catalog 반영 뒤 같은 실행으로 이어가고, 완료된 Planner scope는 Worker scope로 재사용하지 않는다. 별도 프로젝트 안전 차단 중 Close/Reopen transition을 하지 않는 기존 읽기 전용 경계에 맞춰 integration 기대값을 갱신했다.
- 이번 Worker의 `tests.test_project_catalog_refresh` 32건과 import fixture 목록 검사 개별 unit test 1건이 통과했다. 새 차단 fixture의 `ever_started` 초기값이 schema backfill과 달라 발생한 실패는 실행 이력이 있는 fixture로 바로잡고 동일 module 명령으로 재실행해 통과했다. 실제 Git을 사용하는 관련 integration 3건과 전체 verify는 실행 금지에 따라 Manager 재검증에 남긴다.
- 2026-10-01 Worker 리뷰 수정: archive 충돌의 읽기 전용 Merge 증거 조회에 `PR_CLOSED_BLOCK_REASON`으로 `PLAN_BLOCKED`인 Merge 대기 계획을 포함한다. 기존 base·branch·URL·head identity 검증은 유지하고 다른 차단 사유는 제외한다. PR 관측 중 새 프로젝트 안전 차단이 기록되면 후속 fetch·archive·source worktree 동기화 전에 반환해 기존 차단 복구 경계로 넘긴다.
- 리뷰 회귀 검증: 닫힌 PR의 Merge 증거로 active/archive 충돌을 격리하고 catalog 반영 뒤 Merge 관측에 도달하는 테스트와, 다른 head로 reopen되어 새 안전 차단이 생겼을 때 Git 변경·archive·Agent 호출 없이 차단을 보존하는 테스트를 추가했다. `python3 -m unittest tests.test_project_catalog_refresh -v`의 34건이 모두 통과했다. 전체 verify와 integration test는 실행하지 않았다.
