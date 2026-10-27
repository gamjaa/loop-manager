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

- [ ] 확인한 `loop_manager_internal/planning/publication_service.py`의 복구 요청 읽기, DB 밖 복구·정리, 결과 확정을 나눈다. `planning/state_ports.py`·`git_ports.py` 및 기존 effects/repository를 재사용하며, transaction만 외곽으로 옮기고 helper 내부에서 다시 잡는 우회가 없게 한다.
- [ ] 확인한 `planning/planner_publication.py`·`storage/planning_publication_state.py`의 snapshot 검증·`_register_execplan_rows`에서 PR/Git blob 관측을 DB 밖의 준비 단계로 모은다. 검증한 경로·원문·hash/mode·공식 선언·게시 revision을 불변 입력으로 넘기고 실제 등록·mutable CAS·관계/감사 반영은 기존 원자 transaction 안에 둔다. 조회 실패·누락 입력·identity 충돌은 현재 거부를 유지한다.
- [ ] 확인한 `loop_manager_internal/maintenance/maintenance_publication.py`의 같은 등록 경계를 재사용한다. G-330의 ledger prepare/apply와 tiered promote의 외부 원본 관측을 batch 밖에서 준비하고, 확정에는 검증한 근거와 현재 DB 입력만 전달한다. root plan link·barrier 최종 본문 hash·게시 blob 대조와 성공 기준 확정 순서를 보존한다.
- [ ] 확인한 `tests/test_planning_publication.py`·`tests/test_maintenance_publication.py`·`tests/test_tiered_publication.py`의 rollback·재적용·push 후 중단 fixture를 재사용한다. 새 범용 transaction runner·publication 엔진·전역 공유 관측 cache는 추가하지 않는다.
- [ ] 구현한 관측/적용·정리 복구 책임을 ARCHITECTURE의 계획 게시·단계별 반영 절에 통합한다. 실패 planning 자리/독립 선택은 G-278, 실패 원인 소비는 G-189에 남긴다.

## Validation

- Worker targeted unit은 위 게시 모듈의 변경한 prepare/apply·복구·CAS case다. 외부 효과는 관측/명령 경계에서 제어하고 실제 SQLite·batch 적용·rollback·intent 복구는 실행한다.
- PR 조회·blob 읽기·promote 관측·worktree 제거가 대기하는 동안 독립 연결의 DB 요청이 완료돼야 한다. 각 외부 effect 진입에서 DB transaction/접근 소유가 없음을 확인한다.
- 준비 후 snapshot·PR·수동 소유·원문/hash·catalog·cycle/generation 경쟁에서 적용 0회와 원래 intent 보존을 검사한다. 게시 후 DB 실패/재시작, 정리 전·후 DB 실패에서는 이미 끝난 effect 0회와 미완료 적용 1회를 확인한다.
- 전체 verify는 일반/유지보수 batch의 원자 등록·rollback/재적용, 최신 본문과 실제 게시 commit 결합, ID/draft/관계 검증·root link·원래 단계 복원을 보존한다. 읽기 전용 Review에서 외부 관측을 옮긴 대신 snapshot/CAS·원격 검증을 약화하지 않았는지 확인한다.

## Progress

- [x] 저장된 발견·관련 게시 계획과 R-082/R-091 계약을 대조해 ledger 선행과 나머지 batch 경계를 확정했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 외부 처리/DB 적용 분리와 중단·경쟁 회귀 검증
- [x] 공유 신규 row 등록의 호출 내 준비 값과 DB 적용 경계를 구현했다. `_prepare_execplan_rows`는 경로·선언·게시 commit, 로컬 내용 hash·게시 blob hash, draft 입력 지문과 원격 링크를 불변 값으로 준비한다. 일반·유지보수 batch와 `register_execplans`는 자체 transaction 전에 준비하고 `_register_execplan_rows`에 전달한다. 직접 catalog 호출의 기존 facade는 유지한다.
- 등록 준비와 적용 사이 worktree·commit·선언 identity가 달라지면 쓰기 전에 거부한다. 혼합 draft/일반 등록의 두 번째 row에서 중단을 주입해 첫 row와 draft의 원자 rollback을 확인하고, 같은 준비 값을 재사용한 적용·재적용에서 파일·Git·draft 원문 관측을 금지했다. 개별 targeted runner로 이 두 case와 일반 게시 5 case, Requirement 이동 영수증 1 case, 유지보수 게시 2 case, tiered 원자 게시 1 case를 확인했다. 전체 verify는 실행하지 않았다.
- 일반 legacy intent 복구 fixture에 transaction 경계 assertion을 추가한 실행은 fixture의 미종료 transaction 때문에 1회 FAIL이었다. intent 저장 뒤 fixture가 commit하도록 교정한 동일 개별 명령 재실행은 PASS였다. 이 결과는 상위 서비스 transaction의 해소를 뜻하지 않는다.
- 남은 작업은 서비스의 상위 transaction 제거와 소유 worktree 정리 전후 복구, snapshot의 PR 관측/DB CAS 분리, 개정·문서 draft·선언/성공 지문·멱등 판정의 외부 관측 준비, publication intent/cycle/catalog identity 경쟁 검증, ARCHITECTURE 반영이다. 현재 서비스의 상위 transaction은 여전히 준비 호출을 감싸므로 이 보존점만으로 외부 처리 중 독립 DB 접근을 보장하지 않는다. 게시·정리 실패 시 보존 계약과 기존 전체 Acceptance Criteria를 그대로 유지하며 완료 후보로 선언하지 않는다.

## Decision Log

- complexity high: 일반/유지보수의 게시·정리·원격 관측과 원자 batch를 분리하면서 중단 위치별 재개를 보존해야 한다. risk high: 잘못된 확정은 미게시/변경된 계획을 등록하거나 작업을 조기 재개할 수 있다. intent·실제 원격 결과·generation을 보존하고 CAS/rollback으로 낡은 적용을 거부한다. 최초 level은 5다.
- 실제 DB 지연·교착을 측정하지 않았으므로 priority는 normal이다. G-330 뒤 같은 파일의 준비 경계를 소비해 충돌을 줄이며 별도 사후 유지보수 평가를 요구하지 않는다.
