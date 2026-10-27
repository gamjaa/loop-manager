---
id: G-331
title: planning 게시 외부 처리와 DB 적용 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-330
complexity: high
risk: high
priority: normal
---

## Purpose

일반·유지보수 planning 게시의 외부 관측·Git 복구/정리와 catalog/DB 확정을 분리한다. 원격 게시 → 검증된 catalog/DB 반영 → 보존 작업 재개의 순서와 기존 중단 복구를 지키면서 DB 접근 동안 PR/Git 처리를 기다리지 않게 한다.

revision `f5a9d4ea510c62cea769eacd32c604f32bf25425`의 저장된 발견 4는 `planning/publication_service.py`에서 `recover_general_batch()`와 실제 worktree 제거가 transaction 안에 있고, `planner_publication._apply_general_planning_batch()`의 snapshot 검사·등록이 PR 조회와 Git blob 읽기를 포함함을 확인했다. `maintenance_publication`의 등록·promote도 같은 문제를 갖는다. G-330은 ledger의 준비/적용만 소유하며 이 계획은 나머지 게시 batch를 정리한다.

## Acceptance Criteria

- PR·원격/로컬 Git 관측, 게시 복구와 소유 worktree 제거, 원본 재검증은 공통 DB 접근 밖에서 수행한다. DB 요청에는 필요한 읽기·현재 snapshot/owner/generation CAS와 원자 적용만 둔다.
- 준비한 결과와 현재 DB의 snapshot·PR/수동 소유·catalog·cycle·publication intent identity가 다르면 확정하지 않는다. 과거 관측을 현재 소유권으로 대체하거나 실제 변경을 덮지 않는다.
- 같은 원격 게시 결과의 DB 미반영부터 멱등 복구한다. DB 재시도는 완료한 push·commit·worktree 제거·Planner 호출을 재실행하지 않으며, 제거 성공/실패와 DB 확정 사이 중단도 기존 intent와 실제 결과로 복구한다.
- 사용자 동작과 공개 계약인 서비스/port·Git 게시 순서·plan ID allocation·draft/사용자 판단 경계, DB schema·intent/감사의 저장 형식을 보존한다. 일반/유지보수 성공·원래 작업 재개를 한데 합치지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/planning/publication_service.py`의 복구 요청 읽기, DB 밖 복구·정리, 결과 확정을 나눈다. `planning/state_ports.py`·`git_ports.py` 및 기존 effects/repository를 재사용하며, transaction만 외곽으로 옮기고 helper 내부에서 다시 잡는 우회가 없게 한다.
- [x] 확인한 `planning/planner_publication.py`·`storage/planning_publication_state.py`의 snapshot 검증·`_register_execplan_rows`에서 PR/Git blob 관측을 DB 밖의 준비 단계로 모은다. 검증한 경로·원문·hash/mode·공식 선언·게시 revision을 불변 입력으로 넘기고 실제 등록·mutable CAS·관계/감사 반영은 기존 원자 transaction 안에 둔다. 조회 실패·누락 입력·identity 충돌은 현재 거부를 유지한다.
- [x] 확인한 `loop_manager_internal/maintenance/maintenance_publication.py`의 같은 등록 경계를 재사용한다. G-330의 ledger prepare/apply와 tiered promote의 외부 원본 관측을 batch 밖에서 준비하고, 확정에는 검증한 근거와 현재 DB 입력만 전달한다. root plan link·barrier 최종 본문 hash·게시 blob 대조와 성공 기준 확정 순서를 보존한다.
- [x] 확인한 `tests/test_planning_publication.py`·`tests/test_maintenance_publication.py`·`tests/test_tiered_publication.py`의 rollback·재적용·push 후 중단 fixture를 재사용한다. 새 범용 transaction runner·publication 엔진·전역 공유 관측 cache는 추가하지 않는다.
- [x] 구현한 관측/적용·정리 복구 책임을 ARCHITECTURE의 계획 게시·단계별 반영 절에 통합한다. 실패 planning 자리/독립 선택은 G-278, 실패 원인 소비는 G-189에 남긴다.

## Validation

- Worker targeted unit은 위 게시 모듈의 변경한 prepare/apply·복구·CAS case다. 외부 효과는 관측/명령 경계에서 제어하고 실제 SQLite·batch 적용·rollback·intent 복구는 실행한다.
- PR 조회·blob 읽기·promote 관측·worktree 제거가 대기하는 동안 독립 연결의 DB 요청이 완료돼야 한다. 각 외부 effect 진입에서 DB transaction/접근 소유가 없음을 확인한다.
- 준비 후 snapshot·PR·수동 소유·원문/hash·catalog·cycle/generation 경쟁에서 적용 0회와 원래 intent 보존을 검사한다. 게시 후 DB 실패/재시작, 정리 전·후 DB 실패에서는 이미 끝난 effect 0회와 미완료 적용 1회를 확인한다.
- 전체 verify는 일반/유지보수 batch의 원자 등록·rollback/재적용, 최신 본문과 실제 게시 commit 결합, ID/draft/관계 검증·root link·원래 단계 복원을 보존한다. 읽기 전용 Review에서 외부 관측을 옮긴 대신 snapshot/CAS·원격 검증을 약화하지 않았는지 확인한다.

## Progress

- [x] 저장된 발견·관련 게시 계획과 R-082/R-091 계약을 대조해 ledger 선행과 나머지 batch 경계를 확정했다.
- [x] 외부 처리/DB 적용 분리와 중단·경쟁 회귀 검증
- [x] 공유 신규 row 등록의 호출 내 준비 값과 DB 적용 경계를 구현했다. `_prepare_execplan_rows`는 경로·선언·게시 commit, 로컬 내용 hash·게시 blob hash, draft 입력 지문과 원격 링크를 불변 값으로 준비한다. 일반·유지보수 batch와 `register_execplans`는 자체 transaction 전에 준비하고 `_register_execplan_rows`에 전달한다. 직접 catalog 호출의 기존 facade는 유지한다.
- 등록 준비와 적용 사이 worktree·commit·선언 identity가 달라지면 쓰기 전에 거부한다. 혼합 draft/일반 등록의 두 번째 row에서 중단을 주입해 첫 row와 draft의 원자 rollback을 확인하고, 같은 준비 값을 재사용한 적용·재적용에서 파일·Git·draft 원문 관측을 금지했다. 개별 targeted runner로 이 두 case와 일반 게시 5 case, Requirement 이동 영수증 1 case, 유지보수 게시 2 case, tiered 원자 게시 1 case를 확인했다. 전체 verify는 실행하지 않았다.
- 일반 legacy intent 복구 fixture에 transaction 경계 assertion을 추가한 실행은 fixture의 미종료 transaction 때문에 1회 FAIL이었다. intent 저장 뒤 fixture가 commit하도록 교정한 동일 개별 명령 재실행은 PASS였다. 이 결과는 상위 서비스 transaction의 해소를 뜻하지 않는다.
- [x] 일반 게시의 내부 준비/확정 경계를 분리했다. PR 관측은 snapshot token·계획 ID·현재 branch·catalog generation에 결합하며 DB CAS는 관측을 반복하지 않고 실행 상태·수동 소유·현재 branch/generation을 비교한다. intent 예약의 commit·문서 원본 검증도 자체 transaction 전에 수행하고 현재 프로젝트 소유 상태·문서 승인·catalog source 관계를 확정 시 대조한다. batch 적용의 개정 hash·draft 입력·문서 본문·계획 선언과 멱등 판정 입력은 자체 transaction 전에 읽고, 적용 때 준비한 intent 전체 identity가 바뀌면 거부한다.
- 일반 게시 경계 보존점에서 신규 접근 경계/경쟁 6 case와 관련 일반 게시·멱등/rollback·시작한 실행 보존·push 후 복구·문서 승인·Requirement 영수증 회귀를 합쳐 20개 개별 targeted case의 최종 PASS를 확인했다. PR 관측이 Event로 대기하는 동안 같은 파일 DB의 독립 연결이 갱신을 완료했고 활성 transaction과 공통 접근 소유가 없음을 확인했다. 일반 Git/blob/파일/draft 입력 관측도 자체 transaction 밖임을 검사했다. 전체 verify는 실행하지 않았다.
- 혼합 batch 반복 게시 회귀는 적용 완료 후 삭제된 row를 PR 준비가 다시 찾는 ERROR 1회를 발견했다. intent가 없는 재적용에서는 기존 멱등 판정을 사용하고 PR 준비를 생략하도록 교정한 동일 개별 명령 재실행은 PASS였다. 원래 intent가 있거나 준비 중 identity가 변경된 경로의 검증은 유지했다.
- [x] 유지보수 batch의 내부 준비/확정 경계를 분리했다. 개정 hash·draft 입력·계획 선언·지침 hash·두 성공 지문과 repository 구분을 자체 transaction 전에 준비하고, 멱등 판정에도 준비 값을 전달한다. intent 예약과 적용에서 cycle 전체·프로젝트 소유 상태·catalog 전체 및 대상 계획의 수동 membership 지문을 다시 비교한다. 확정 직전 신규 ID 예약과 공유 Requirement scope도 다시 검사한다. root 계획 검증은 불변 `PreparedPublicationIntent`로 전달하며 기존 JSON payload와 DB 저장 형식은 바꾸지 않았다.
- 공유 `planning_input_state.record_general()`의 숨은 지침 원문 재독해도 제거했다. 일반·유지보수 batch는 준비한 지침 hash를 넘기며, 기존 호출의 기본 동작은 유지한다. root intent 준비 값을 적용 중 Git 재관측 없이 rollback 후 재사용하고 다른 게시 identity의 값을 거부하는 회귀를 추가했다.
- 유지보수 보존점에서 개별 targeted 16 case의 최종 PASS를 확인했다. 내부 외부 관측/성공 지문 경계, cycle·owner·catalog generation·수동 membership 경쟁, intent 원본 관측 중 owner 경쟁, 일반/유지보수 원자 rollback·멱등 적용·legacy/CRLF 재시작 복구, 파일 근거 승격·tiered root 연결·원자 rollback, prerequisite scope와 일반 지침 baseline 보존을 확인했다. 전체 verify는 실행하지 않았다.
- 신규 경계 test는 공유 baseline helper가 지침 원문을 transaction 안에서 다시 읽는 FAIL 1회와, 경쟁 fixture의 미종료 초기 transaction에 따른 FAIL 1회를 발견했다. 준비한 지침 hash 전달과 fixture intent commit을 반영한 동일 개별 명령 재실행은 각각 PASS였다. 실패를 성공 이력으로 덮거나 검사를 약화하지 않았다.
- [x] 정상 게시와 변경 없음 완료의 상위 transaction에서 worktree 제거를 분리했다. catalog·성공 기준 적용과 함께 기존 `projects.publication_remote_oid`에 확인한 revision을 저장하고 planning 소유권·결과·session을 유지한다. 일반 intent도 슬롯 해제까지 유지한다. 다음 짧은 DB 읽기로 만든 release identity는 프로젝트·cycle·intent·catalog·수동 membership을 결합하며, 원격 이력/게시 경로·현재 HEAD·Git 등록을 DB 밖에서 확인하고 제거한 뒤 별도 CAS로 슬롯을 해제한다. DB schema와 intent JSON 형식은 바꾸지 않았다.
- 적용 완료 release는 초기 prepared intent 복구보다 먼저 처리한다. 제거 뒤 DB 해제 중단에서도 이미 적용한 batch·push·Planner·제거를 반복하지 않고 남은 해제만 진행한다. 최종 해제는 기존 `run_database_request`를 사용하며 rollback 후 같은 준비 값을 재사용한다. 변경 없음 완료의 선언·지침도 상위 transaction 전에 준비한다. 일반 batch 확정에는 준비 중 프로젝트 소유 상태 변경을 거부하는 CAS를 추가했다.
- 정리 실패 테스트는 원격에서 검증한 DB 적용을 rollback하는 기대 대신 적용 완료·planning 소유권 유지·해제 대기와 결과/session/진단 보존을 검사하도록 갱신했다. prerequisite scope도 게시 적용 뒤 유지한 채 정리 대기임을 검사한다. 실행 재개는 정리와 최종 CAS 뒤에만 허용하며 기존 원자 등록/개정 rollback 검사는 유지한다.
- 슬롯 해제 보존점에서 개별 targeted 13 case의 최종 PASS를 확인했다. 실제 coordinated SQLite에서 제거 double이 대기하는 동안 독립 연결의 쓰기가 완료됐고, 효과 진입의 transaction·공통 접근 소유가 없음을 확인했다. 제거 뒤 DB 중단·재시작과 최종 해제 rollback/재시도에서도 제거 1회·게시/Planner 재호출 0회를 확인했다. 준비 후 owner·catalog generation·수동 membership 변경은 해제를 거부하고 경쟁 데이터를 보존했다. HEAD 변경·삭제 경로의 Git 등록 잔존 거부, 일반/유지보수 적용 후 슬롯 보존과 기존 서비스 실패/재개·조립 회귀도 확인했다. 전체 verify는 실행하지 않았다.
- 새 제거/DB 해제 중단 fixture는 초기 INSERT의 미종료 transaction에 따른 FAIL 1회를 발견했다. intent 저장을 commit한 동일 개별 명령 재실행은 PASS였다. 이후 coordinated 연결과 독립 쓰기·DB-only 재시도 검사로 확장한 동일 test도 PASS였다.
- [x] 초기 prepared intent 복구의 두 상위 transaction을 제거했다. 일반·유지보수 복구는 원격 게시된 결과를 DB에 적용한 뒤 같은 정리/해제 경계를 사용한다. 복구 관측 전 프로젝트·intent 또는 cycle·catalog·수동 소유 근거를 잡고 적용 시 다시 비교한다. 미게시 후보는 기존 소유 worktree 원격 반영 경계를 유지하며, 복구 관측 중 게시 사실이 달라지면 후보와 intent를 보존한다.
- 게시 뒤 원본이 변경된 유지보수 재평가는 prepared cycle·기존 게시 OID·intent를 보존한 정리 대기로 처리한다. 최종 CAS가 재평가 cycle 재시작과 planning 해제를 함께 확정한다. 원본 관측 중 owner 변경은 정리 대기 저장 전에 거부하며, 최종 해제 중단은 cycle·intent·session을 rollback한다. 다음 해제에는 원본 관측·catalog 적용을 반복하지 않는다.
- 공유 등록·일반/유지보수 intent 예약과 batch 적용·legacy restart 확정은 기존 `run_database_request`에 DB-only closure를 넘긴다. 외부 관측은 closure 전에 준비하며 첫 적용 rollback 뒤 같은 closure를 재실행해도 관측을 반복하지 않는다. 재시도 사이 owner·cycle이 바뀌면 기존 intent와 경쟁 데이터를 보존하고 적용을 거부한다. 변경 없음 완료도 선언·지침·승격 근거를 준비한 뒤 DB-only transaction으로 적용하고, 제거·해제는 분리한 경계를 사용한다.
- 이번 호출은 개별 targeted 19 case의 최종 PASS를 확인했다. 일반/유지보수 intent·batch rollback/재시도와 owner·cycle 경쟁, 초기 복구의 상위 transaction 금지, 게시된 원본 변경 재평가와 해제 중단, 기존 coordinated DB 독립 쓰기·제거 후 재시작, 혼합 catalog·draft 등록·유지보수 원자 rollback, 변경 없음·prerequisite 범위 보존·Git 정리 검증을 포함한다. 전체 verify는 실행하지 않았으며 Manager 검증에 인계한다.
- 재시도 fixture 2 case는 autocommit 연결에 transaction을 시작하지 않아 rollback 검사가 각각 1회 FAIL이었다. 명시적 transaction으로 고친 동일 명령 재실행은 PASS였다. 유지보수 초기 복구 case는 옵션의 잘못된 전달 위치와 중복 전달로 연속 2회 ERROR였고, 호출 분기를 교정한 동일 명령 재실행은 PASS였다. 실패·재실행 기록을 보존했다.
- [x] ARCHITECTURE의 계획 게시·단계별 반영 절을 실제 준비/DB CAS/외부 정리/최종 해제와 중단 복구 책임에 맞게 통합했다. schema·intent/감사 JSON, ID/draft 경계와 일반/유지보수 성공 기준을 보존했다. 구현은 검증 후보이며 전체 verify와 Review는 Manager가 수행한다.

## Decision Log

- complexity high: 일반/유지보수의 게시·정리·원격 관측과 원자 batch를 분리하면서 중단 위치별 재개를 보존해야 한다. risk high: 잘못된 확정은 미게시/변경된 계획을 등록하거나 작업을 조기 재개할 수 있다. intent·실제 원격 결과·generation을 보존하고 CAS/rollback으로 낡은 적용을 거부한다. 최초 level은 5다.
- 실제 DB 지연·교착을 측정하지 않았으므로 priority는 normal이다. G-330 뒤 같은 파일의 준비 경계를 소비해 충돌을 줄이며 별도 사후 유지보수 평가를 요구하지 않는다.
