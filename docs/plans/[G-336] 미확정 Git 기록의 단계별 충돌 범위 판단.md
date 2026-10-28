---
id: G-336
title: 미확정 Git 기록의 단계별 충돌 범위 판단
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-077] G 계획 수동 구현과 R·코드 일괄 제출.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

미확정 Manager Git 기록을 이번 단계의 실제 자원과 대조해 독립 작업의 진행과 충돌 자원의 보류를 구분한다. planning 해제에 직접 구현된 별도 관리 rebase 판정을 재사용하고, 같은 journal을 소비하는 Requirement 교정·archive·수동 준비/복구의 범위 판단을 일관되게 연결한다. archive 실행 기회는 G-321, 게시 후 정리 분리는 G-337, planning과 복구의 상호 대기는 G-338이 맡는다.

`9dd75b5d`의 `planning/git_ports.py:validate_planning_release()`는 active/불변 intent·작업 branch·worktree 등록으로 별도 rebase를 구분한다. 이 직접 구현과 최초 게시/복구 회귀는 중복 구현하지 않는다. 공통 `has_pending_project_mutation()`에는 포괄적인 boolean과 호출자별 recoverable/targets만 있고, 교정·housekeeping·수동 slot/release에는 프로젝트 전체 pending gate가 남아 있다. 경로 필터만으로 공유 ref·설정과 writer 안전을 증명할 수는 없다.

## Acceptance Criteria

- R-055 B01/B02/B08: operation/intent·소유자·branch/ref·worktree·공유 설정·게시 대상과 이번 단계의 실제 사용 자원을 확인한다. 다른 G ID·서로 다른 경로·오래된 intent만으로 독립성을 승인하지 않는다. 독립성이 확인된 별도 rebase는 해당 작업·의존 작업·충돌 자원만 보류하며 최초 planning 게시와 복구의 해제를 막지 않는다.
- active와 불변 intent 불일치·손상·소유 근거 부족, 같은 branch/worktree·게시 ref·공유 설정 충돌과 실행 중/종료 불명 writer에는 필요한 gate를 유지한다. 현재 소유권·lease·writer 근거가 바뀌면 낡은 판단을 적용하지 않는다. 기존 source 동기화의 비권위 처리와 정확한 수동 own-operation 복구 예외는 유지한다.
- 독립 기록의 active intent·원문·파일/index·checkpoint·차단을 삭제·완료 처리하지 않는다. 이미 완료한 효과의 미확정 기록은 기존 완료 증명으로만 확정하며, 분류 자체는 Git 쓰기·복구나 검증 성공을 만들지 않는다.
- 각 소비자의 source/draft·planning 게시 예약·수동 membership·실제 프로젝트 안전·dependency gate를 보존한다. journal의 독립성 판단은 그 gate의 해소 증명이 아니다. archive의 유휴 gate 분리와 실행 기회는 G-321에 남긴다.
- 허용/보류 결과에 기존 저장·로그 경계가 소비할 원인 소유자·operation/intent·영향 단계·충돌/미확인 자원·다음 조건을 제공한다. 비밀값과 로그 문구 파싱에 의한 제어를 추가하지 않는다. 실제 외부 관측·잠금 대기는 DB 요청 밖에서 수행하고 적용 시 현재 identity를 재확인한다.

## Implementation

- [x] 확인한 `loop_manager_internal/git/manager_git_journal.py:has_pending_project_mutation()`의 현재 source 예외·intent 읽기와 `planning/git_ports.py:validate_planning_release()`의 증거 판정을 재사용해 단계별 사용 자원과 미확정 작업의 관계를 판단한다. 기존 `runtime/resource_lock.py`·writer 종료/소유 근거를 연결하며 새 범용 lock/journal 서비스나 과거 repository snapshot 비교를 도입하지 않는다.
- [x] 확인한 모든 소비자 `catalog/requirement_correction_publication.py:observe()`, `application/project_service.py:_archive_housekeeping()`, `application/manual_concurrency.py:preparation_gate()`, `application/manual_work.py:slot_available()`, `recovery/manual_work_recovery.py:release_wait()`와 planning 해제를 대조해 공통 판정을 적용한다. targets 밖 기록을 무조건 무시하지 않고 실제 공유 자원을 확인한다. 교정의 writer/source 보호·수동 own-intent 복구·archive의 기존 개별 후보 보존을 유지한다.
- [x] `tests/test_manager_git_journal.py:PendingProjectMutationTest`, `tests/test_planning_publication.py:PlanningPublicationTest`와 소비자별 `tests/test_requirement_correction_publication.py`, `tests/test_manual_concurrency.py`, `tests/test_archive_housekeeping_adapter.py`의 기존 fixture를 사용한다. 판단 helper를 성공 mock으로 바꾸지 않고 journal 파일·SQLite 상태는 실제로, Git/프로세스/원격 관측만 double로 제공한다.
- [x] 실제 구현에 맞춰 PRODUCT의 복구·수동 준비와 ARCHITECTURE의 계획 해제·교정 게시·Git journal 책임을 통합한다. 사용 자원·불확실성의 의미를 소유 절에 두고 소비자마다 정책을 복제하지 않는다.

## Validation

- Worker targeted unit은 위 파일에서 변경한 공통 판정과 소비자 연결의 최소 case다. 기존 `test_publication_release_scopes_pending_rebase_on_first_publish_and_recovery`의 독립/동일 자원·손상/미확인 intent 행렬과 intent bytes 불변·Agent 무호출을 유지한다. 단독 helper의 PASS 외에 실제 해제·교정 또는 수동 후속 단계 도달/거부와 독립 작업 진행을 assertion한다.
- branch/worktree가 달라도 같은 게시 ref·공유 설정, writer 종료 불명 또는 관측 후 소유권/intent 경쟁이면 쓰기·해제를 거부한다. 독립 기록이면 해당 원인·checkpoint를 보존한 채 허용한다. 원격 조회 실패·해당 operation 미완료를 성공값으로 대체하지 않는다.
- 새 조건 조합은 mock 관측으로 검사하고 실제 Git 효과의 기존 게시·수동·archive 회귀를 유지한다. 동일 사유의 반복 출력은 줄여도 사유 변경·실제 시도·해소 근거는 남긴다. 정체 확정/알림과 status 투영은 G-325/G-326·G-194/G-195가 소비한다.

## Progress

- [x] `993b7dfd` → `9dd75b5d` delta와 현재 helper의 모든 호출자·직접 planning 회귀를 정적으로 대조했다. planning의 국소 구현과 나머지 소비자/공유 안전의 미충족을 구분했으며 실행 검증은 하지 않았다.
- [x] 보존점 1: planning의 별도 rebase 자원 증거 판정을 `manager_git_journal.rebase_uses_separate_workspace()`로 추출하고 planning 해제에서 재사용한다. 단계별 worktree·local/published ref 입력을 비교하며 active·불변 intent를 Git 등록 관측 전후에 재확인한다. 이는 자원 분리 근거만이며 writer 종료·lease·공유 설정 안전이나 다른 소비자의 진행 허가를 대신하지 않는다.
- [x] 공통 자원/증거 판정과 소비자 연결: `has_conflicting_project_mutation()`을 planning 해제·교정·archive 진입·수동 준비/slot/release에 연결했다. 현재 소유 완료 row·동일 자원 claim·checkpoint·호출 예약/실제 writer 종료 근거, 기존 lease, 실제 Git 등록·common directory·직접 branch ref와 include를 적용한 공유 local config를 대조한다. targets 밖 기록도 검사하고 정확한 수동 own-target 복구와 source 비권위 예외를 유지한다. 기존 writer 판정을 `check_stopped_writers()`로 추출해 예약 복구와 재사용한다. 수동 CAS/자리 반환은 외부 관측 뒤 `project_mutation_identity()`와 현재 대상·묶음을 재확인하며 Git·프로세스를 DB 안에서 호출하지 않는다.
- [x] 보존점 1 targeted: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_manager_git_journal.PendingProjectMutationTest tests.test_planning_publication.PlanningPublicationTest.test_publication_release_scopes_pending_rebase_on_first_publish_and_recovery`를 실행했다. 첫 실행은 새 테스트의 active 교체 fixture가 손상 JSON을 사용해 오류 1건이었고, 유효한 다른 intent fixture로 수정 후 같은 명령의 5개 테스트가 통과했다. 독립/충돌 ref·worktree·등록 실패·intent 경쟁과 journal bytes 보존, 실제 최초 게시/복구 해제 및 Agent 무호출을 확인했다. 전체 verify는 실행하지 않았다.
- [x] 허용·거부·보존 및 후속 단계 도달의 최소 회귀·문서 정합성: 실제 SQLite/journal과 Git 관측 double로 독립 rebase의 교정 commit/push·archive dispatch·수동 준비/slot/자리 반환 도달, 같은 게시 ref·미종료 writer·다른 소유 lease·공유 config/소유권/intent/checkpoint 경쟁·간접/누락 branch ref의 거부와 journal bytes 보존을 확인했다. archive 진입 회귀는 기존 ProjectService fixture가 있는 `test_requirement_correction_publication.py`에 두어 fixture를 복제하지 않았다. PRODUCT의 복구·수동 준비와 ARCHITECTURE의 공통 자원 계약·계획 해제·교정 게시 설명을 통합했다.
- [x] 이번 호출 targeted: 관련 단위 13개가 통과했다(`PendingProjectMutationTest`, `ManualJournalScopeTests`, planning 최초 게시/복구 1개, 교정 후속 게시 1개, 기존 예약 lease 복구 2개, 외부 Git 무호출 수동 complete/submit 2개). 이후 직접 branch ref·config include 보호 변경의 영향 단위 5개도 통과했다(공통 행렬·수동 준비/slot/release·planning 최초 게시/복구·교정 게시·archive 진입). 기존 교정 writer 종료·pending rebase의 실행 복구 도달 회귀도 별도 통과했다. 실제 Git 효과·전체 suite/verify·Windows native는 실행하지 않았다.
- 이번 호출의 실패와 수정: 공통 행렬의 다른 소유 lease 재사용 실패 1회는 helper 소유 lease만 허용하도록 고친 후 동일 명령으로 통과했다. 소비자 3개 명령의 첫 실행은 교정 fixture의 draft 테이블 누락과 수동 identity 비교의 소유권 변경 오류였고, fixture 및 미확인 identity 처리 수정 후 동일 명령을 재실행했다. 다음 실행의 수동 slot 실패는 fixture에 남은 planning 예약 때문이어서 예약을 명시적으로 비우고 해당 gate가 다시 켜지면 계속 거부함을 검증했다. 세 번째 동일 명령은 3개 모두 통과했다. 같은 테스트의 연속 3회 실패는 없고 미해결 targeted 실패는 없다.

## Decision Log

- 원격의 직접 구현에 소급 작업을 만들지 않는다. R-055가 지정한 공통 원인과 호출자 전체의 같은 완료 조건을 한 작업으로 묶고, archive 실행 조율·게시 정리·복구 조율은 각 독립 작업에 남긴다. 단순 boolean false나 광범위 예외 무시는 교정이 아니다.
- complexity high: 서로 다른 작업의 실제 공유 쓰기·현재 소유/종료 근거와 단계별 사용 자원을 연결하는 판단이 남는다. risk high: 잘못된 독립성 승인은 경쟁 게시·index/작업물 훼손으로 수동 복구를 요구할 수 있다. 정확한 자원 lease·현재 근거 재검사·손상/경쟁 거부와 소비자 보존 assertion으로 완화한다. 무인 진행을 막는 공통 gate의 교정으로 priority high, 최초 level 5다.
