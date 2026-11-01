---
id: G-333
title: 정책 테스트의 Git 준비와 판단 경계 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

Git 자체의 효과를 assertion하지 않는 정책·저장 테스트에서 실제 repository 준비와 정책 판단의 책임 경계를 분리한다. 관측 경계에 입력을 공급하고 실제 판단·SQLite·파일 보존 검증을 유지하며, 필요한 Git 통합 회귀는 남긴다.

revision `f5a9d4ea510c62cea769eacd32c604f32bf25425`의 저장된 발견 13·14·18·21·22·27은 공통 setUp이나 조건 행렬이 실제 init/commit/push를 요구하지만 assertion은 schema·상태·재시도·순수 분류에 있는 사례를 확인했다. 여기에 `993b7dfd`의 게시 분리 변경으로 추가된 CAS·DB retry 정책 사례 중 아래 명시한 일곱 case를 포함한다. 해당 사례의 공통 Git 준비를 분리하며 실제 Git 효과 검증이나 이미 완료한 R-080 전환을 다시 계획하지 않는다.

## Acceptance Criteria

- 아래 지정한 정책/저장 사례는 실제 Git subprocess 없이 같은 정상·실패·연속 관측·재시작 조건과 assertion을 검사한다. 실제 운영 판단·DB 전이를 성공 mock으로 바꾸지 않는다.
- 필요한 SQLite와 문서/파일 보존은 실제 fixture로 유지한다. Git 관측은 기존 port/adapter double을 재사용하고 새로운 범용 fixture·전역 cache를 만들지 않는다.
- 사용자 동작과 공개 계약인 CLI/API·수집 범위·필수 Git 통합, DB/문서/receipt의 저장 형식을 보존한다. 테스트 skip·timeout 확대·필수 assertion 삭제로 전환을 대신하지 않는다.
- 전환한 조건과 남긴 통합 회귀의 대응, 변경한 모듈의 실제 Git 호출 감소를 확인할 수 있다. 측정하지 않은 전체 verify 시간·절감률을 주장하지 않는다.
- 게시 CAS·DB retry 사례는 R-091의 준비/적용 분리를 실제 SQLite·transaction rollback·현재 owner/cycle/intent/catalog/membership 비교로 계속 검증한다. 외부 관측 입력만 mock으로 제공하며 같은 준비 값 재사용·준비 호출 횟수·낡은 적용 거부와 경쟁 데이터/기존 intent 보존 assertion을 유지한다. 실제 공통 DB 접근 조율과 독립 연결 쓰기·worktree 제거의 연결 회귀도 유지한다.
- R-055 B08: 게시/해제의 공통 안전 판단을 무조건 성공하는 mock으로 대체하지 않는다. 무관한 pending rebase의 진행 허용과 실제 공유 자원·종료 불명/손상 근거의 거부를 실제 판단과 소비자의 후속 단계/보존 assertion으로 유지한다. DB-only 정책 case가 외부 판단을 범위 밖으로 두면 이를 검증하는 실제 소비자 회귀와 assertion 대응을 명시한다.

## Implementation

- [x] 확인한 `tests/test_blocked_remote_observation.py`의 schema migration과 `test_remote_failure_cause_changes_are_persisted_as_new_input`를 독립 SQLite/관측 mock fixture로 옮긴다. 실제 원격 변화·재시작·작업 경로를 검증하는 나머지 통합 사례는 유지한다.
- [x] 확인한 `tests/test_catalog_scheduling.py:test_dependency_blocked_ready_is_not_an_empty_catalog`는 Git 관측을 주입하고 현재 scheduler·SQLite·반환 종류·호출 역할·planning baseline·계획 상태를 실제로 검사한다. 기존 관측 port만 바꾸며 G-297의 배정 직전 재검사/대기 정책은 구현하지 않는다. 현재 최상위 planning WAITING 뒤 작업 coordinator가 실행돼도 dependency 대기를 만족하지 못한 G는 Worker를 호출하지 않아야 한다. 코드/ExecPlan만의 전진·archive 예외·기존 성공 기준 재사용과 실제 명세 변경의 선행 planning을 구분하고, 새 원격 source 조회에는 정확한 revision/path 관측만 제공한다. coordinator·selector를 일괄 성공 mock해 dependency/대기 판단을 제거하지 않는다. `test_planning_and_ready_selection_pin_fingerprint_to_resolved_sha`의 실제 선택 SHA/worktree HEAD 연결은 남긴다.
- [x] 확인한 `tests/test_maintenance_file_inventory.py:test_root_placement_inventory_keeps_fixed_paths_and_marks_root_source_for_review`는 명시적 `SourceInventory`로 category·reason을 검사하게 한다. 같은 파일의 multilanguage 원격 blob/mode·로컬 dirty 분리와 unsafe mode/revision 검증은 통합 회귀로 유지한다.
- [x] 확인한 `tests/test_planner_validation_recovery.py`의 세 번째 저장 retry 무추가 호출·첫 validation retry·CLI 실패의 retry 비소비를 `tests/test_planning_recovery.py`·`tests/support/planning_case.py`의 기존 관측 경계와 실제 SQLite/임시 파일로 분리한다. 세션·호출 횟수·pending/error·파일 보존 assertion을 유지하고 실제 commit/게시·충돌/index 검증은 planning publication/rebase에 남긴다.
- [x] 확인한 `tests/test_requirement_archiving.py:test_missing_archived_source_does_not_block_unrelated_requirement_review`를 `tests/test_requirement_archive.py`의 순수 calculate 경계로 통합한다. catalog/states/문서 입력과 candidates assertion을 유지하고 공통 repository/DB setUp 의존만 없앤다. 실제 blob·worktree·이동 보존 사례는 남긴다.
- [x] 확인한 `tests/test_scope_project_recovery.py:test_active_execplan_uses_reconciled_identity_not_publication_equality`의 여섯 metadata/body 조건에 Git 관측/등록 입력을 주입한다. 실제 문서 파싱·reconciled SQLite 선언·필드별 수용과 보존을 유지한다. `tests/test_scope_rebase_recovery.py`·`tests/test_source_sync.py`의 실제 rebase/후보 보존을 대신 제거하지 않는다.
- [x] 확인한 `tests/test_incremental_planning.py:IncrementalPlanningCatalogTest`의 `test_applied_general_batch_retains_owner_and_rejects_stale_cleanup_release`, `test_general_db_retry_rejects_owner_change_without_repeating_preparation`, `test_general_batch_rejects_publication_intent_changed_during_observation`, `test_general_intent_rejects_project_owner_changed_during_observation`를 Git 없는 정책 fixture로 분리한다. 현재 `setUp()`의 init/commit/update-ref와 `prepare_batch()`의 commit·관측 준비를 이 네 case에서 요구하지 않게 하고, 실제 snapshot·manifest·임시 원문·SQLite row와 운영 intent/apply/release 함수는 유지한다. `tests/test_planning_publication.py`의 실제 SQLite·effects fixture와 `tests/test_incremental_planning.py:MutableReadyMetadataCasTest`의 관측 입력 패턴을 먼저 재사용한다. 공유 Git setUp을 단순 mock해 가짜 repository를 나머지 통합 case에 전파하지 않는다.
- [x] 확인한 `tests/test_maintenance_publication.py:MaintenancePublicationTest`의 `test_maintenance_db_retry_rejects_cycle_change_without_repeating_preparation`, `test_batch_rejects_cycle_owner_and_catalog_races_during_observation`, `test_intent_rejects_owner_changed_during_commit_observation`도 같은 원칙으로 분리한다. `tests/support/maintenance_planning_case.py`의 bare/seed init·push·`_validate_barrier()`가 이 정책 사례의 필수 준비가 되지 않도록 `tests/test_maintenance_planning.py`의 기존 SQLite/관측 fixture를 재사용한다. 실제 cycle·수동 membership·catalog·rollback·보존 결과와 정상/경쟁의 입력 순서는 유지하고 publication 함수 자체를 성공 mock으로 바꾸지 않는다. 운영 게시 구현·공개 port·저장 형식은 변경하지 않는다.
- [x] `9dd75b5d`에 직접 추가된 `tests/test_planning_publication.py:test_publication_release_scopes_pending_rebase_on_first_publish_and_recovery`를 공통 안전 판단의 소비자 회귀로 유지한다. 이 테스트는 실제 journal 파일과 `_PlanningGitPorts`의 판정을 사용하며 Git 관측만 제공한다. 전환한 DB 정책 사례가 이 보호를 검사한다고 잘못 표시하지 않고 G-336의 공통화 뒤에도 같은 대응을 유지한다. 새 정책 조합·운영 gate 교정 자체는 이 테스트 준비 분리의 범위로 추가하지 않는다.

## Validation

- Worker targeted unit은 위 명시한 기존 여섯 전환 대상과 게시 정책 일곱 case의 변경분이다. 실제 Git 명령 경계가 호출되면 실패하는 guard를 fixture 준비 전부터 적용해 Git 호출 0회를 확인하면서 실제 판단/SQLite assertion을 실행한다. 공통 setUp의 실행을 guard 범위 밖으로 빼서 0회를 주장하지 않는다.
- 이전 조건·호출 횟수·session·오류·pending·문서 보존·category/candidates assertion의 대응을 검토하고, 오류/경쟁 관측도 mock의 일괄 성공으로 사라지지 않는지 확인한다.
- 전체 verify는 남긴 exact-SHA/worktree·원격 변화·blob/mode·게시/rebase/index·archive 보존 통합 회귀와 전환한 정책 사례를 함께 포함해야 한다. 같은 모듈 준비 비용 비교가 필요하면 동일 계측/수집 범위에서 비교하며 전체 실행을 반복하는 새 완료 조건을 만들지 않는다.
- 게시 정책의 이전/이후 대응은 release의 session·catalog generation·수동 membership 경쟁, 준비 중 intent/owner 변화, rollback 뒤 owner/cycle 변화 거부와 준비 1회·row/intent 보존 assertion별로 확인한다. `tests/test_incremental_planning.py`의 `test_general_batch_observes_git_and_files_before_its_transaction`·`test_remote_confirmed_intent_recovers_after_push_before_sqlite_apply`·`test_mutable_snapshot_uses_git_blob_with_crlf_checkout`, `tests/test_maintenance_publication.py`의 `test_batch_observes_git_files_and_success_fingerprints_before_transaction`·`test_crlf_batch_recovers_from_durable_intent_after_restart`·`test_published_batch_recovers_after_remote_tip_advances`는 실제 게시 원문/adapter·원격 이력 연결의 회귀로 유지한다. `tests/test_db_access.py:test_planning_pr_observation_releases_access_before_waiting`와 `tests/test_planning_publication.py:test_applied_publication_recovers_after_cleanup_before_db_release`의 실제 조율된 SQLite·독립 쓰기·정리/재시작 검증을 mock으로 대체하지 않는다. 삭제·이동한 assertion과 남긴 검증의 대응을 결과에 기록한다.
- catalog 정책 fixture는 위 WAITING에서도 Worker 무호출·실제 planning baseline/계획 상태를 유지한다. `tests/test_project_services.py:test_scheduled_planning_wait_still_dispatches_task_recovery`·`tests/test_planning_selection.py:test_execution_observation_reuses_specification_baseline_and_archive_policy`는 이미 있는 운영 연결/선택 회귀로 소비하며 이 테스트 준비 분리에서 복제하지 않는다.
- 읽기 전용 Review에서 실제 Git 효과 assertion이 없는 사례만 전환했는지, 판단 함수를 mock하지 않았는지와 모든 이동/삭제 assertion의 대응을 확인한다.
- 원격의 위 rebase 해제 회귀가 최초 게시/복구의 실제 해제·worktree 제거 여부, active intent bytes 불변·원인 작업 보존과 Agent 무호출을 계속 검사하는지 정적으로 대조한다. 전환 대상의 판단을 성공 mock으로 대체해 이 검증을 우회하거나 보호된 실패 조건을 제거하면 실패다. 해당 회귀를 다시 작성하지 않고 기존 assertion의 소비 위치를 남긴다.

## Progress

- [x] 저장된 6건의 정책/실제 효과 구분과 남길 통합 회귀를 확인하고, 현재 관측 port에서 독립적으로 전환할 범위를 확정했다. 구현·테스트·verify는 실행하지 않았다.
- [x] 보존된 G-333 후보의 catalog fixture와 미커밋 관측 보완을 읽기 전용으로 확인했다. 후보의 구현·기록·작업 공간은 수정하지 않았으며 이 확인을 테스트·verify·Review 성공으로 사용하지 않았다.
- 보존된 원격 문서 관측 보완을 포함한 현재 후보에서 `CatalogSchedulingObservationTest.test_dependency_blocked_ready_is_not_an_empty_catalog`가 targeted runner로 통과했다. 실패 Planner와 독립 Worker의 역할·SQLite 상태를 확인했으며, 최신 조건의 planning WAITING 뒤 dependency 대기와 일곱 게시 정책 사례는 아직 확인하지 않았다.
- 의존성 차단·실행 가능·안정 입력 대기의 세 프로젝트를 실제 scheduler/SQLite와 Git 관측 double로 검사했다. WAITING 뒤 차단된 G의 Worker 무호출·baseline·상태, 명세 변경의 Planner와 독립 프로젝트 Worker 역할을 확인했으며 targeted unit은 통과했다. 동일 조건의 기존 Git 준비 사례를 제거하고 선택 SHA/worktree HEAD 통합 회귀 및 별도 코드/명세·archive 선택 회귀는 유지했다.
- [x] rebase된 후보에서 기존 여섯 전환 대상의 9개 단위 사례를 targeted runner로 다시 실행해 통과했다. Git 통합 회귀와 R-055 B08 소비자 회귀는 유지한다.
- 일반 planning DB retry 한 사례를 Git 없는 SQLite fixture로 이동했다. 실제 임시 문서와 snapshot, 혼합 생성·개정·삭제 batch 및 운영 intent/apply를 사용하며 rollback·준비 1회·경쟁 owner/intent/catalog 보존 assertion을 유지한다. 이 fixture의 batch는 기존 validation 결과에 대응하는 입력으로 직접 구성하며, 실제 commit·게시 원문과 pending rebase 안전 판정은 기존 publication/rebase 회귀가 담당한다. 해당 targeted unit과 이번 후보의 기존 전환 대상 9개 단위 사례는 통과했다.
- 일반 planning 나머지 세 사례를 같은 독립 SQLite fixture로 옮겼다. intent·owner 관측 경쟁과 복구, 해제 직전 session·catalog generation·수동 membership 경쟁 및 row/intent 보존을 실제 publication/release 함수로 확인했다. Git 원문·원격 효과와 pending rebase 안전 판단은 기존 `test_general_batch_observes_git_and_files_before_its_transaction`, `test_remote_confirmed_intent_recovers_after_push_before_sqlite_apply`, `test_publication_release_scopes_pending_rebase_on_first_publish_and_recovery`가 각각 담당한다. 변경한 세 targeted unit은 통과했다.
  FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_incremental_planning.GeneralPublicationPolicyTest.test_general_intent_rejects_project_owner_changed_during_observation
  RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_incremental_planning.GeneralPublicationPolicyTest.test_general_intent_rejects_project_owner_changed_during_observation
  결과: 원격 revision 관측 누락으로 첫 실행 실패, 해당 관측 보완 후 동일 명령 통과.
- [x] 유지보수 게시 세 사례를 SQLite와 고정 Git 관측으로 분리하고 실제 barrier revision·cycle·수동 membership·catalog·rollback 및 intent 보존을 확인했다. 기존 `MaintenancePlanningCase`의 문서·cycle helper와 `tests/test_maintenance_planning.py`의 SQLite/관측 패턴을 사용했으며 실제 blob·게시 이력과 pending rebase 판단은 Validation에 열거된 통합·소비자 회귀에 남긴다. 세 targeted unit이 통과했다. 수동 membership 사례의 첫 실행은 fixture에 READY revision이 없어 실패했고 해당 revision을 복원한 뒤 같은 명령으로 통과했다.
- 현재 후보에서 전환 대상 16개와 pending rebase 소비자 회귀 1개가 targeted runner로 통과했다. 소비자 회귀는 최초 게시와 복구 각각에서 독립 rebase의 해제·worktree 제거, 충돌·손상·소유자 불명의 거부, journal bytes와 원인 worktree 보존 및 Agent 무호출을 검사한다. 첫 validation retry의 subprocess guard를 fixture 생성 전으로 옮기고 scheduler 문서 관측을 고정 revision의 등록 경로로 제한한 뒤 변경된 두 사례도 재실행해 통과했다. 전체 verify와 Review는 Manager가 수행한다.

## Decision Log

- 이번 delta의 planning WAITING 전달·프로젝트 단위 명세 재관측에 맞춰 catalog 정책 fixture의 입력과 assertion을 보존한다. 기존 시작한 ID·경로·무dependency·분류와 후보의 구현/실패 기록은 유지하며 운영 배정 교정이나 새 fixture 체계로 범위를 넓히지 않는다.

- R-055 B08은 직접 sources 밖에서도 이 작업의 게시/DB 정책 전환에 실질적으로 적용되므로 source와 assertion 대응에 연결한다. 새 직접 구현의 pending rebase 회귀는 기존 검증으로 보존하며 운영 공통화는 G-336에 맡긴다. started ID·경로·기존 수행 기록·무dependency·분류를 유지하고 차단 해제나 검증 성공은 선언하지 않는다.

- `597e5b3c` → `993b7dfd`의 새 테스트는 운영 게시 분리의 구현 근거로 소비한다. 위 일곱 case는 실제 ref·index·worktree 효과가 아니라 DB 적용/보존·준비 횟수·경쟁 거부를 assertion하지만 공통 fixture가 실제 Git 준비를 요구하므로 R-080의 남은 전환 범위다. 같은 교정 목표의 이 started 계획을 개정하며 새 중복 계획은 만들지 않는다. R-091을 source로 연결해 외부 관측/DB 적용 경계와 경쟁·재시도 assertion을 보존하고, 실제 Git/blob·CRLF·게시 이력과 독립 DB 접근 연결은 남긴다. 기존 ID·경로·complexity/risk·priority·dependency와 수행 기록을 유지하며 차단 해제·구현·테스트·verify 성공을 선언하지 않는다.

- complexity medium: 지정한 기존 정책 사례와 추가된 게시 사례의 관측 경계는 명확하지만 실제 SQLite·연속 입력·상태 보존을 서로 맞춰야 한다. risk medium: 과도한 mock은 실제 정책 회귀를 가릴 수 있어 입력 관측만 대체하고 판단/저장과 필요한 통합을 유지한다. 최초 level은 3이다.
- 현재 scheduler와 이미 원격에 반영된 게시 구현의 관측 입력만 분리하므로 G-297의 새 배정·대기 구현을 선행으로 요구하지 않는다. G-297은 해당 동작 변경을 계속 소유하며 이 계획은 현재 판단·저장 assertion을 보존한다. 위 명시한 사례만 바꾸며 다른 느린 테스트로 범위를 확대하거나 게시 엔진을 재구현하지 않는다.
