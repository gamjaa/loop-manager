---
id: G-120
title: Extract Bootstrap and State Foundations
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-119
complexity: high
risk: high
priority: critical
---

## Purpose

`loop_manager.py`의 process lock·runtime bootstrap과 schema·catalog migration·DB 접속은 후속 runtime·Agent·Git 기능이 모두 사용하는 기반이다. 먼저 서로 독립된 runtime 및 storage 경계로 추출해 내부 모듈이 거대한 진입점에 의존하는 원인을 줄인다.

## Acceptance Criteria

- 상태 디렉터리, lock·runtime identity와 공통 immutable 자료형은 필요한 책임의 leaf module에 둔다. storage·bootstrap service가 루트 진입점을 import하거나 실행 process 전체의 전역 객체를 조회하지 않는다.
- `SCHEMA`, `_ensure_execplans_schema`, `_migrate_execplan_catalog`, `connect_database`와 catalog cache/publication persistence의 책임은 기존 repository 계층으로 연결한다. 동일 row 해석·schema 보완·definition serialize가 중복된 경우 같은 저장 계약 안에서 재사용한다.
- SQLite schema·column/default·저장 JSON·감사 기록과 transaction/rollback 범위를 보존한다. 공통 SQL helper가 임의 commit을 수행하거나 서로 다른 event의 transaction을 합치지 않는다.
- lock 획득, 최신 원격 bootstrap SHA 확인, source 갱신 후 handoff, exact-SHA detached runtime 선택과 child 실행의 순서를 보존한다. 사용자 source 변경, config 절대 경로와 interpreter 선택, 실패 exit code를 유지한다.
- `tests/test_origin_isolation.py`의 bootstrap/runtime identity·source sync·registration case를 책임별로 나누고 G-119 support를 사용한다. root resource와 launcher 호출 계약을 유지한다.

## Implementation

1. G-060 대응표에서 공통 자료형·예외·identity와 각 schema/접속 함수의 실제 소비자를 확인한다. runtime과 storage가 서로를 불필요하게 import하지 않도록 작은 contract module 또는 명시적 인자를 사용한다.
2. bootstrap/process-lock/runtime 선택을 G-063 패키지의 runtime 영역으로, 접속·기존 migration orchestration을 storage 영역으로 추출한다. bootstrap의 Git 효과는 명시적인 adapter로 받고 아직 G-122가 담당할 Git 구현을 함께 재설계하지 않는다.
3. `runtime_state.py`, `execplan_catalog_state.py`, maintenance·notification repository와 중복되는 실제 저장 연산만 담당 repository로 모은다. row projection과 원본 실행 상태의 경계, Manager 밖 전체 verify 실행 금지를 유지한다.
4. 필요한 공개 root helper는 명시적 re-export로 보존하고 내부 소비자와 테스트는 실제 module을 사용한다. 저장소 루트·state·config identity를 전달하며 이동한 `__file__` 기준으로 resource 경로를 바꾸지 않는다.
5. import·compile 대상이 바뀐 부분만 verify에 반영하고 후속 추출 대상은 대응표에 남긴다. 무관한 schema 개선이나 runtime 정책 변경은 수행하지 않는다.

## Validation

- Worker는 접속·schema 재개·catalog projection과 bootstrap lock/runtime identity의 직접 관련 test method만 선택해 targeted unit test를 실행한다.
- schema 재적용의 멱등성, 실패 transaction rollback, 저장 JSON의 round trip, 같은 SHA의 정상 runtime 재생성 및 증거가 불충분한 재생성 거부를 검증한다.
- dirty source의 보존, config 경로·interpreter 전달과 resource lookup은 해당 경계의 unit test로 확인한다. 실제 다중 process·Git 흐름의 전체 regression은 Manager verify에 맡긴다.
- Manager 전체 verify에서 Windows/Linux launcher, exact runtime identity, 기존 database 호환성과 모든 origin-isolation 시나리오가 유지돼야 한다.

## Progress

- [x] foundation 소비자와 transaction·bootstrap 순서 대응표 확인
- [x] runtime·storage·leaf contract 추출 및 관련 중복 제거
- [x] targeted unit test와 origin-isolation test 책임 분리
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: G-120~G-123은 같은 `loop_manager.py`의 기능을 순차 추출한다. PR별로 구현·검증·사용자 Merge를 끝내고 다음 변경의 기준선을 갱신한다.
- 이 계획은 기반 책임의 위치와 재사용을 바꾸며 상태 모델·저장 형식·bootstrap safety 정책을 바꾸지 않는다. 길이가 큰 repository라도 한 책임으로 응집돼 있으면 그 근거를 기록한다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.

- 2026-10-02 구현 대응표: G-060의 루트 참조·소유권·목적지 조사와 G-063의 패키지를 재사용했다. 새 루트 항목은 없으며 launcher, `ROOT`, 외부 config, interpreter 및 공통 문서 경로를 유지한다.

| 기반 책임과 실제 소비자 | 구현 위치·저장/효과 경계 |
| --- | --- |
| bootstrap·source sync·scope가 공유하는 immutable identity와 오류 | `runtime/contracts.py`; 루트는 같은 객체를 명시적으로 re-export한다. |
| CLI의 사용자 상태 경로·부모 lock과 runtime lease | `runtime/process_lock.py`; lock 수명과 identity 갱신 순서는 기존 `main` composition에 유지한다. |
| runtime 경로·intent·legacy 증거·자식 CLI 경로 | `runtime/runtime_identity.py`; resource root를 자체 `__file__`로 다시 계산하지 않는다. |
| runtime 생성·검증·제거·bootstrap refresh·child 실행 | `runtime/bootstrap.py`의 `BootstrapService`; Git 연산·source sync·로그는 명시적인 effect adapter로 받고 runtime root/interpreter는 인자로 확정한다. |
| 모든 repository와 scheduler connection의 schema·접속·cleanup schema | `storage/database.py`; schema 문자열과 초기화 함수의 AST가 기존 구현과 동일하다. connection-local marker, savepoint와 기존 commit 순서를 보존한다. |
| legacy project projection 이관·archive quarantine | `storage/catalog_migration.py`; 원격 snapshot·PR·Git proof만 주입하고 catalog/runtime repository를 재사용한다. `manage_transaction=False`에서는 caller transaction을 유지한다. |
| catalog 선언 JSON·publication identity·등록·worker level | 기존 `storage/execplan_catalog_state.py`; 같은 metadata serialize와 row fallback을 등록·복구·audit 조회에 재사용한다. 저장 helper는 commit하지 않는다. |
| general planning intent·mutable catalog 변경·취소 감사 | `storage/planning_publication_state.py`; intent event의 기존 connection context를 보존하고 batch SQL은 기존 caller transaction에서만 실행한다. planning 오류 계약은 기존 leaf `planning/planning_catalog.py`로 옮겼다. |

- catalog repository는 선언 cache와 최초 publication·archive audit라는 같은 저장 계약을 유지한다. 길이만을 이유로 recovery 판정까지 다른 계층으로 나누지 않았다. G-122의 Git helper·journal·source synchronizer와 G-125의 planning lifecycle/manifest orchestration은 기존 adapter로 남기며 이번 변경에서 재설계하지 않는다.
- 기존 origin-isolation 79개 case를 bootstrap entry, runtime lifecycle, registration, source sync 및 scope 검증으로 분리했다. 공유 fixture는 `tests/support/origin.py`에 두고 G-119의 Git/DB/catalog support를 재사용한다. 함수 이름 집합을 대조해 누락·중복이 없음을 확인했다.
- targeted unit test 27개 통과: foundation 모듈 16개, schema 재개·catalog atomicity·bootstrap 호출 계약의 기존 method 7개, foundation import·공개 helper identity·runtime resource·verify compile 목록 method 4개. 실제 Git/multiprocess regression과 전체 verify는 실행하지 않았으며 Manager 검증·Review·사용자 Merge는 대기 중이다.
- 첫 기존 method 선택 실행은 이동된 runtime-child case의 클래스 경로를 잘못 지정해 실패했다. case를 bootstrap entry 책임으로 배치한 뒤 같은 명령을 재실행해 7개 모두 통과했다. 아래 명령은 공백 정규화 후 동일하다.

FAILED_COMMAND: python3 -B -m unittest tests.test_execplan_catalog_state.CatalogReconciliationTest.test_additive_migration_backfills_publication_audit_and_current_revision tests.test_execplan_catalog_state.CatalogReconciliationTest.test_migration_resumes_backfill_after_columns_were_committed tests.test_execplan_catalog_state.CatalogReconciliationTest.test_all_definition_deltas_update_atomically_without_runtime_loss tests.test_execplan_catalog_state.CatalogReconciliationTest.test_base_race_and_transaction_interruption_leave_no_partial_delta tests.test_bootstrap_entry.BootstrapEntryTests.test_bootstrap_holds_lock_around_runtime_and_normalizes_external_config tests.test_bootstrap_entry.BootstrapEntryTests.test_runtime_child_does_not_reacquire_process_lock tests.test_bootstrap_runtime.BootstrapRuntimeTests.test_runtime_child_removes_only_after_exit_and_preserves_exit_code -v
Initial run: ERROR (선택한 클래스에 runtime-child test method가 없었음).
RERUN: python3 -B -m unittest tests.test_execplan_catalog_state.CatalogReconciliationTest.test_additive_migration_backfills_publication_audit_and_current_revision tests.test_execplan_catalog_state.CatalogReconciliationTest.test_migration_resumes_backfill_after_columns_were_committed tests.test_execplan_catalog_state.CatalogReconciliationTest.test_all_definition_deltas_update_atomically_without_runtime_loss tests.test_execplan_catalog_state.CatalogReconciliationTest.test_base_race_and_transaction_interruption_leave_no_partial_delta tests.test_bootstrap_entry.BootstrapEntryTests.test_bootstrap_holds_lock_around_runtime_and_normalizes_external_config tests.test_bootstrap_entry.BootstrapEntryTests.test_runtime_child_does_not_reacquire_process_lock tests.test_bootstrap_runtime.BootstrapRuntimeTests.test_runtime_child_removes_only_after_exit_and_preserves_exit_code -v
Final run: PASS (7 tests).

- 2026-10-02 Worker 결과 교정: 구현 테스트 오류가 아니라 최종 보고의 증거 배치 문제였다. `parse_worker_result`는 `TARGETED_TESTS` 영역만 검증하므로 이 영역 안에 동일 FAILED_COMMAND/RERUN, 최초 ERROR와 최종 PASS를 순서대로 보고한다. 기존 7-method 명령을 다시 실행해 PASS를 확인했고 `WorkerResultContractTest.test_two_corrected_runs_keep_initial_outcomes_and_clean_final_results`도 PASS였다. 검증기 코드는 변경하지 않으며 최종 보고 자체를 기존 parser로 사전 확인한다.

- 2026-10-02 Manager verify 피드백 `verify:637ed3a4a3e60d545f4159cba99d639457605c6eea90599cfecf305a8d417293` (occurrence 1, revision 1) 교정: 분리된 `BootstrapRuntimeTests`에 빠진 `OriginScopeFixture` 상속을 복원했다. cleanup callback은 같은 service instance의 원본 bound method를 patch 전에 저장해 루트 wrapper를 통한 재귀를 없앴다. lock errno test는 표준 `errno`와 실제 process-lock module을 사용한다. scope prevalidation의 lock patch는 현재 소비자인 루트 orchestration에, CAS patch는 SQL을 실행하는 `planning_publication_state`에 결합했다. 기존 assertions·CAS SQL·runtime safety 정책은 변경하지 않았다.
- 이번 교정의 targeted unit test 6개 PASS: lock errno, fake-Git runtime prevalidation, publication metadata CAS의 delete/revise/rename, fake-Git runtime cleanup 후 같은 SHA 재생성, legacy/new parent lease identity, 공유 scope fixture의 snapshot 저장. CAS의 stale cache 거부와 rollback, scope의 외부 변경 거부를 그대로 확인했다. 실제 Git·child process를 사용하는 Manager 실패 case 3개와 전체 verify는 Worker가 실행하지 않았으며 Manager 재검증 대상으로 남긴다.
