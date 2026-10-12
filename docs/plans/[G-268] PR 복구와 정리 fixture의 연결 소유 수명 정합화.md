---
id: G-268
title: PR 복구와 정리 fixture의 연결 소유 수명 정합화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on:
  - G-223
complexity: low
risk: low
priority: normal
---

## Purpose

테스트 fixture가 생성한 SQLite 연결의 소유·종료 책임을 생성 즉시 기존 cleanup 경계에 연결한다. 저장된 발견 40의 `tests/support/pr_recovery.py:75`는 연결을 만든 뒤 schema·등록·worktree·후보 준비를 수행하지만 중간 실패에서 호출자에게 반환되지 않은 연결을 닫을 책임이 없다. 발견 36의 `tests/test_worktree_cleanup.py:241`은 차용 port에 넘긴 연결을 생성한 case에서 종료 책임을 등록하지 않는다. 같은 저장소의 PublicationFixture는 `tests/support/lifecycle.py`의 `register_connection_cleanup`으로 이 책임을 이미 처리한다.

## Acceptance Criteria

- `_pr_open_state`가 소유한 연결은 생성 직후 cleanup을 등록한다. 준비 도중 schema/catalog/workspace/후보 단계의 예외와 정상 반환 모두에서 테스트 소유자가 종료를 보장한다. 아직 반환되지 않았다는 이유로 객체 회수에 맡기지 않는다.
- worktree cleanup case도 생성한 연결의 종료 책임을 즉시 등록한다. `_PlanningGitPorts`는 계속 차용자이며 운영 port가 연결을 임의로 닫거나 새 소유권을 인수하지 않는다.
- 기존 helper의 idempotent cleanup을 재사용해 명시적 조기 종료/cleanup 및 setup 실패의 중복 정리가 안전하다. 종료 등록 순서는 연결을 사용하는 정리 작업과 임시 디렉터리의 수명을 보존한다.
- 사용자 동작과 공개 계약은 운영 PR 충돌 복구·cleanup 판정·Git/원격 lease·후보 검증과 fixture 반환/차용 계약을 보존한다. 연결은 case에서 필요로 하는 동안 유효하고 실제 PR identity·session·검증 token 및 ignored-only 제거/권위 변경 보존 assertion은 유지한다.
- 저장 형식은 SQLite schema/row·후보/감사 token과 파일/Git bytes를 보존한다. 운영 SQLite 상태 변경, schema migration·새 fixture framework·전역 연결 registry는 추가하지 않는다.

## Implementation

`tests/support/pr_recovery.py`와 `tests/test_worktree_cleanup.py`의 연결 생성 지점에서 기존 `register_connection_cleanup` 또는 기존 owner/cleanup 구조에 맞는 수명 helper를 재사용한다. 실제 소비자인 `tests/test_pr_conflict_corrections.py`에서 준비 실패에도 cleanup이 가능한지 확인한다. fixture가 보유한 연결과 port가 차용한 연결의 책임을 섞지 않는다.

G-223이 PR 복구 fixture와 관련 테스트의 관측/실제 Git 경계를 먼저 전환한 뒤 그 fixture 위에서 수명만 정합화한다. Git mock 전환과 재시도/게시 정책은 이 작업에서 중복 구현하지 않는다.

## Validation

- Worker targeted unit test는 `_pr_open_state`의 연결 생성 이후 준비 실패를 주입해 등록된 cleanup이 연결을 닫고 예외·진단을 보존하는지 확인한다. 연결 생성 전 실패, 정상 사용 뒤 cleanup과 명시적 close 이후 재cleanup의 결과를 필요한 최소 경계로 확인한다. 새 연결 검증을 위해 실제 원격 준비/게시를 반복하지 않는다.
- 기존 `tests/test_pr_conflict_corrections.py`와 `tests/test_worktree_cleanup.py`에서 변경한 최소 case로 PR/session/token 보존 및 ignored-only 제거·권위 있는 변경 보존을 확인한다. 실제 효과 검증이 필요한 기존 통합 case는 유지한다.
- Manager 전체 verify는 PR 충돌/lease/중단 재개와 worktree cleanup의 소유권·잠금 복원·실패 시 보존, 기존 공통 lifecycle helper의 실패/중첩 fixture 회귀를 통과해야 한다. 수집·assertion·필수 compile/verify를 축소하지 않는다.

## Progress

- [x] 2026-10-12 source revision `c401d563589399dbca83ffe4c9cfe13419ff128e`의 module manifest `c10544e0999d36ee6e8143dedecacb414a03b8f2205fbfd2bbbb0b472d1e00b6` 발견 36·40을 재사용하고 인용/HEAD 일치를 확인했다. 기존 lifecycle helper와 차용 문맥만 확인했으며 구현·테스트·verify는 실행하지 않았다.
- [ ] 소유 연결 cleanup 정합화와 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity low: 기존 helper를 두 소유 연결 경계에 적용하는 국소 변경이며 운영 복구 설계를 바꾸지 않는다. risk low: 테스트 자원의 종료 시점에 한정되고 기존 반환/차용 계약과 회귀를 유지하며 코드 rollback으로 복구할 수 있다. priority normal이다.
- G-223의 Git 관측 분리와 이 작업의 연결 수명 책임은 다르다. 같은 PR fixture를 순차 변경하므로 G-223을 선행으로 두고 그 기존 파일·metadata·수행 기록은 개정하지 않는다. G-224는 이미 Merge된 기반이다.
- 새 DAG의 미충족 선행 closure는 G-222·G-223이고, 두 계획의 G-224 dependency는 이미 충족됐다. 새 네 작업 사이에는 선행 관계가 없으며 각각 독립 검증·Merge 가능한 말단이다. Manager가 closure 밖의 eligible READY에 말단 dependency를 적용한다.
- 기존 G-222/G-223 범위의 Git 경계 발견은 새 작업으로 복제하지 않는다. G-189/G-246의 별도 기능/정책 교정과 단독 테스트 공백·진입점/미사용 import 정리는 새 구조 작업에서 제외한다. 이번 확정 범위 밖의 추가 평가·사후 재검증을 종료 조건으로 두지 않는다.
