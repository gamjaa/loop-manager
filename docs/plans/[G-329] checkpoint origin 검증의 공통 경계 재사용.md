---
id: G-329
title: checkpoint origin 검증의 공통 경계 재사용
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: medium
priority: normal
---

## Purpose

Worker checkpoint 게시의 등록 origin 검증 책임을 기존 workspace helper로 모은다. repository/worktree 양쪽의 fetch·push URL과 GitHub repository identity를 검사하는 보안 계약, 호출 경계의 예외와 push 직전 재검사를 그대로 유지한다.

revision `f5a9d4ea510c62cea769eacd32c604f32bf25425`의 저장된 발견 3은 `git/publication_service.py:push_worker_checkpoint_commits()`의 중첩 `validate_origin()`이 `git/workspace.py:validate_registered_remote()`와 같은 판정을 별도로 구현함을 확인했다. 두 구현의 후속 변경이 갈라질 수 있는 구체적인 중복이며, 새로운 원격 허용 규칙을 추가하는 작업이 아니다.

## Acceptance Criteria

- repo/worktree 각각의 origin fetch URL과 push URL이 정확히 하나이고 모두 등록된 repository identity와 일치해야 게시한다. URL 누락·복수·다른 identity·관측 실패는 기존 거부를 유지한다.
- 검증을 호출하는 시점과 횟수의 안전상 의미, checkpoint 소비자의 `PublicationSafetyError` 경계와 기존 소유권·lease·원격 후보 재검사를 유지한다.
- 사용자 동작과 공개 계약인 함수 시그니처·CLI, checkpoint/intent·Git journal·DB의 저장 형식과 이미 성공한 push의 재개를 보존한다. 인증 정보나 원격 URL의 비밀값을 새 진단에 기록하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/git/publication_service.py`의 URL/identity 판단을 `git/workspace.py:validate_registered_remote()` 재사용으로 바꾼다. 기존 effects 조립을 통해 같은 관측 경계를 사용하고 helper 예외를 게시 소비자의 기존 예외로 변환한다. 새로운 validator·전역 설정·캐시를 추가하지 않는다.
- [ ] 확인한 `tests/test_workspace_publication_adapters.py`에서 실제 helper와 게시 호출을 연결하는 mock 관측 unit을 보강한다. 현재 `push_worker_checkpoint_commits()`의 새 원격 입력 대기·완료 통합 뒤 낡은 intent 폐기에서도 최초 origin 검증이 적용되고 실제 push 경로의 직전 재검사가 유지돼야 한다. `tests/test_worker_checkpoint_publication.py`의 실제 commit/push·중단 재개·`confirmed: false` 폐기 감사 회귀는 그대로 남기고 같은 Git 준비를 새 정책 행렬마다 반복하지 않는다. 관계 판단·통합·게시 요청 확정은 이 공통화의 구현 범위로 확대하지 않는다.

## Validation

- Worker targeted unit은 `tests.test_workspace_publication_adapters`의 변경한 origin/게시 연결 case다. 양쪽 위치 × fetch/push의 정상·누락·복수·identity 불일치·명령 실패를 검사하고 실제 판정·예외 변환을 성공 mock으로 대체하지 않는다.
- 최초 검사 뒤 push 직전 origin 변경은 기존 위치에서 다시 거부해야 한다. 예외의 호출자 분류와 게시 effect 0회를 확인한다.
- 새 원격 입력을 먼저 통합하도록 반환하는 경로와 완료 통합의 낡은 intent를 폐기하는 경로에서도 잘못된 repo/worktree origin은 무쓰기 거부여야 한다. 정상 origin에서는 기존 요청/원형·폐기 감사·`pushed: 0`·게시 OID 미확정 의미를 보존한다. 관측 double과 실제 helper로 기존 URL 행렬에 필요한 연결만 추가하고 `test_rewritten_remote_retires_integrated_pending_push_without_claiming_success`의 실제 Git fixture를 복제하지 않는다.
- 전체 verify는 Worker checkpoint의 정상 게시·정확한 lease·게시 대기·commit/push 뒤 저장 중단과 중복 효과 방지를 유지한다. 읽기 전용 Review는 공통화 후 검증 지점 삭제·판정 완화·예외 삼킴이 없는지 확인한다.

## Progress

- [x] 저장된 중복 판정의 인용·공통 helper·게시 회귀 책임을 확인했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] helper 연결·예외 보존과 targeted unit 검증

## Decision Log

- revision `7b1a2e22`의 새 원격 입력 대기·낡은 intent 폐기 분기는 직접 구현으로 소비한다. R-055의 입력 수용/게시 권한 분리와 R-093의 게시 대기·복구점 의미가 이 helper 교체의 회귀 조건에도 적용되므로 최초/직전 origin 검사와 기존 감사·효과를 유지한다. snapshot token `52eb05094dd88c347febe6a00f6aaccac53cefd38d15a456e3a5d24cfe98d4c5`, 개정 전 hash `a2ab20969ec5814882579fa712ec4cc809ddc78783703de45370b8a347187efe`를 확인했으며 started ID·경로·metadata·기존 수행 기록과 차단을 보존한다. 새로운 사용자 결정·차단 해제·테스트 성공은 기록하지 않는다.

- complexity low: 기존 helper와 effects를 재사용하는 국소 변경이다. risk medium: 예외 변환이나 재검사 누락은 잘못된 원격 게시로 이어질 수 있어 양쪽 URL 계약·effect 0회·중단 재개를 유지한다. 최초 level은 1이다.
- 현재 origin 정책을 바꾸거나 원격 쓰기 범위를 확대하지 않는다. 중복만으로 실제 보안 사고를 관측했다고 주장하지 않으며 priority는 normal로 둔다.
