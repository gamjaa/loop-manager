---
id: G-133
title: Reassess Preserved Planner Decisions After Relevant Input Changes
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

관련 원격 입력이 바뀌면 파일 수정 없이 끝난 `unsafe`를 포함한 보존된 Planner 판단을 같은 세션에서 다시 평가하고, 새 판단의 실제 검증이 해결한 planning 오류만 해제한다. PRODUCT의 실패·세션·작업물 보존, 관련 입력 재확인과 실제 원인 해소 후 차단 해제 규칙 및 MERGED된 G-118의 복구 계약을 완성하는 결함 수정이다.

현재 `planning_document_relevance.py`는 평가 대상 계획을 `edited_paths`에서만 찾는다. `workspace_service.py`는 이 판별 결과가 비면 완료된 판단을 재판단 대기로 전환하지 않는다. 따라서 대상 snapshot의 계획이 원격에서 바뀌어도 무수정 `unsafe` 결과를 재사용할 수 있다. `assessment_service.py`의 검증 성공 후 오류 해제는 `planning_error == planning_validation_error`에 의존하므로 새 교정 문구와 과거 오류가 달라지면 같은 원인에 대한 차단이 남는다. `publication_service.py`의 미해결 오류 guard는 유지하고 이 guard에 도달하기 전에 검증된 오류 해제를 명시적으로 확정한다.

2026-10-03 사용자 제공 사건에서는 `origin/main`의 `affc16a → 774d11b` 전진과 G-066/G-112/G-113/G-114 변경이 planning worktree와 runtime에 반영됐지만, `edited_paths=[]`인 이전 `unsafe` 판단이 재사용됐다. 수동 재판단 대기 전환 후 같은 session `01a0ff8c-1d02-7b42-a508-51302750e869`가 `revalidated`를 반환하고 Manager 검증도 통과했으나 문자열 불일치로 게시가 거부됐다. 해당 오류만 수동 해제한 뒤 maintenance가 `idle`로 완료된 결과와 `recovery.json`, `validated-error-release.json`, `loop-manager.db`는 사용자 제공 감사 근거다. 이 계획 작성 과정에서는 운영 DB를 수정하거나 해당 사건을 재실행하지 않았다.

## Acceptance Criteria

- 일반·유지보수 Planner의 파일 수정 목록과 명시적 평가 대상을 구분한다. 유지보수의 eligible/revalidation snapshot에 결합된 대상 계획, 그 계획의 `sources`·`depends_on` 문서와 현재 판단에 적용되는 정책 입력을 관련 입력으로 추적한다. 변경 전·후 원격 선언을 모두 대조해 새 source/dependency와 제거된 관계를 놓치지 않는다.
- 파일 변경 없는 `unsafe` 이후 평가 대상 계획만 원격에서 바뀌면 이전 결과·오류·snapshot·검증 진단을 감사에 보존하고, 최신 입력 identity에 대한 재판단을 같은 session·worktree에서 정확히 한 번 예약한다. base 채택과 pending 기록 사이에 재판단을 잃는 중단 구간이 없어야 한다.
- 대상·source·dependency의 실제 내용 또는 관계와 관련 정책 변경은 재확인한다. 무관한 문서, 검증된 내용 보존 archive 이동과 base SHA만 전진한 경우에는 Planner를 재호출하지 않는다. 경로 이동은 안정적인 plan ID와 원격 내용 identity로 대조하되 삭제·동일 ID 경쟁·대상 착수·소유권 변경을 안전한 이동으로 오인하지 않는다.
- 완료 판단의 유효 입력 identity, 과거 오류 사건 identity와 최신 검증 근거를 구분한다. 교정 메시지 문구와 cycle/base revision이 바뀌어도 해결 대상 오류의 연결을 유지하고, 원격 전진이나 지문 재결합만으로 검증 성공 또는 오류 해제를 기록하지 않는다.
- fresh `revalidated` 또는 해당 mode에서 허용된 무변경 결과가 실제 scope·mode·목표 보존·snapshot·catalog 검증을 통과하면 그 검증으로 해소된 과거 planning 오류만 해제한다. 이후 기존 게시 또는 no-change 완료 경로로 catalog·cycle 완료를 확정하며, 성공 기준은 기존 완료 경계 전에는 전진시키지 않는다.
- 독립 project/plan 차단, 사용자 승인 대기, 다른 planning 오류 사건과 실제 scope·mode·snapshot·소유권 불일치는 보존한다. 문자열 불일치를 이유로 해결된 오류를 남기거나 문자열 일치를 이유로 독립 오류를 지우지 않는다. 다시 `unsafe`이거나 검증 실패한 결과는 기존 실패·재시도 정책으로 보존한다.
- base 채택 후 호출 전, 호출 후 검증 전, 검증 후 완료 기록 전의 중단·재시작과 추가 원격 전진을 복구한다. 이미 저장된 완료 호출·검증·게시·catalog 및 cycle 완료를 중복 확정하지 않고, 추가 관련 변경으로 무효화된 판단은 최신 입력에 결합된 대기로 이어간다.
- 세션·작업물·scope·실패 원문·재시도 감사와 기존 Git 소유권·권한·publication intent·transaction 경계를 보존한다. planning 오류 해제와 해당 차단 사건 해제·미전송 알림 취소는 기존 SQLite transaction 안에서 함께 확정하고, 과거 전송·사건 감사는 삭제하지 않는다.

## Implementation

1. 최신 원격 기준에서 관련 입력 판별 → workspace rebase·base 채택 → Planner 호출·결과 저장 → validation → publication/no-change 완료의 실제 경로를 추적한다. `planning_contracts.py`, Git/validation/state ports, `maintenance_validation.py`의 eligible snapshot·cycle 재구성, `storage/runtime_state.py`의 planning 상태·오류 해제 및 notification 사건 처리의 기존 책임을 확인한다. G-118 이후 모듈 분리를 그대로 활용하고 Worker 실행·Review 정책은 변경하지 않는다.
2. `planning_document_relevance.py`에 파일 수정 목록과 독립된 평가 대상 입력을 전달한다. maintenance는 저장된 mode·cycle 및 eligible/revalidation snapshot, 일반 planning은 기존 mutable READY·현재 작성/평가 선언의 근거를 사용한다. 변경 전 Git blob의 선언과 최신 원격 catalog·Git blob을 비교하고 두 선언의 source/dependency 집합을 확인한다. dirty worktree의 결과를 원격 원본이나 개정 권한으로 사용하지 않는다. 기존 내용 보존 archive 판별과 catalog의 ID·path 대응을 재사용하며 읽기 실패·관계 불확실성을 변경 없음으로 성공 처리하지 않는다.
3. `workspace_service.py`와 state port에서 관련 변경에 따른 재판단 예약을 영속화한다. 이전 판단의 입력·대상·결과·오류·재시도 근거를 덮어쓰기 전에 감사로 보존하고, 새 base 채택과 현재 판단의 pending 전환을 같은 SQLite transaction에 결합한다. Git rebase 완료와 DB는 단일 transaction이 아니므로 기존 journal·현재 Git·DB 상태를 대조해 중단 구간을 복구한다. 같은 입력의 pending은 중복 생성하지 않고 추가 전진의 변경 문맥은 최신 목표 입력으로 갱신한다.
4. 완료 판단의 입력·대상 identity와 해결 대상 오류 사건을 명시적으로 연결할 최소 영속 정보를 기존 planning 상태·진단·사건 저장 경계에 둔다. 오류의 원래 입력과 진단은 보존하고 현재 gate의 입력 지문과 혼동하지 않는다. 기존 사건 식별자를 재사용할 수 있는지 먼저 확인하며, 필요한 저장 확장은 기존 DB를 읽을 수 있는 호환 추가로 제한한다. legacy 상태는 보존된 결과·revision·snapshot·오류 감사에서 연결을 검증할 수 있을 때만 복원하고 연결이 불확실하면 자동 해제하지 않는다.
5. `assessment_service.py`는 pending을 읽어 같은 Planner session에 최신 snapshot과 관련 변경 문맥만 전달하고 기존 호출·scope 저장·중단 복구를 재사용한다. 호출 완료와 결과 identity를 저장한 뒤 실제 검증을 수행한다. 완료 결과가 저장된 재시작에서는 Agent를 반복 호출하지 않는다. 변경된 입력에 허용된 교정의 재시도 전이는 기존 정책을 따르되 과거 재시도·실패 감사는 남긴다.
6. 검증 성공은 해결 대상 사건·현재 입력·대상 snapshot·검증 결과가 일치하는 경우에만 planning 오류 해제 근거가 된다. `planning_error == planning_validation_error`를 사건 연결의 근거로 사용하지 않는다. stale 검증, 다른 사건과 실제 mode/scope/소유권 실패는 해제하지 않는다. state port의 좁은 조건부 갱신과 기존 사건 해제 transaction을 사용하고, 감사 보존 또는 상태 대조 실패 시 오류를 미해결로 남긴다.
7. `publication_service.py`의 guard, 원격 재확인·prepared intent 복구와 일반/maintenance 게시·no-change 완료를 유지한다. 오류 해제 뒤에도 원격 입력·권한이 다시 바뀌면 해당 후보를 재검증하고, push·catalog·cycle 확정 전에는 성공 기준을 전진시키지 않는다. 검증 후 중단은 저장된 검증 identity를 재확인해 남은 완료 단계만 수행한다. worktree 정리 또는 완료 transaction 실패를 성공으로 처리하지 않고 기존 rollback·오류 보존 경계를 유지한다. 오류 해제·publication·cycle 완료를 위한 범용 workflow나 별도 서비스는 추가하지 않는다.

## Validation

Worker는 아래 경계에서 수정한 로직과 새 회귀를 직접 검증하는 fake-port 기반 targeted unit case만 작성·실행한다. 새 case 이름은 구현 시 확정해 실행 명령과 결과를 Progress에 기록한다.

| 경계 | targeted unit 검증 |
| --- | --- |
| `tests/test_planning_rebase.py`의 `PlanningDocumentRelevanceTest` | `edited_paths=[]`에서 평가 대상 계획 변경 감지, 변경 전·후 sources/dependency, 정책 변경, rename·ID 대응, 내용 보존 archive·무관 변경·동일 내용 SHA 전진의 무호출 분류. fake Git blob 응답을 사용한다. |
| `tests/test_planning_recovery.py`의 fake ports | 무수정 완료 `unsafe` → 원격 대상 변경 → 감사 보존·pending 1회 → 같은 session 호출. base 채택 후 호출 전 중단, 추가 전진, 완료 결과 재사용, 재시도 감사와 독립 오류 보존. |
| `tests/test_maintenance_planning.py` 및 해당 결과 검증의 fake-port case | `unsafe → revalidated`와 허용된 no-change, 교정 문구 변경 후 사건 연결, 검증 실패·재차 unsafe·mode/snapshot/소유권 drift의 차단 유지, legacy 연결의 확인 가능·불확실 분기. |
| `tests/test_planning_publication.py` 또는 `tests/test_maintenance_publication.py`의 unit case | 연결된 오류만 해제, 독립 차단 보존, validation 후 완료 전 중단, stale 검증 거부, guard 유지, 오류·차단 사건 transaction rollback과 반복 완료·게시의 멱등성. |

- Windows의 관련 문서 분류 초기 실행 대상은 `py -3 -m unittest tests.test_planning_rebase.PlanningDocumentRelevanceTest`다. 다른 파일은 변경한 개별 fake-port case만 지정하며 실제 Git을 사용하는 class와 파일 전체를 targeted 명령에 섞지 않는다.
- Manager 전체 verify는 격리된 실제 remote repository에서 무수정 `unsafe` → 사용자 대상 계획 commit/push → 같은 session의 fresh `revalidated` → 실제 validation → 게시 또는 no-change catalog·cycle 완료 → `idle`, 해당 `planning_error=NULL`의 전체 경로를 확인한다. 운영 저장소·운영 DB·실제 AI 호출은 회귀 fixture로 사용하지 않는다.
- Manager의 실제 Git integration은 세 중단 경계, 추가 원격 전진, 대상·source/dependency rename·변경, 무관 변경·내용 보존 archive, independent 차단·권한 drift와 반복 실행을 포함한다. Planner 호출·pending 예약·catalog 적용·cycle 완료·push 횟수 및 세션·원래 결과·오류 감사 보존을 함께 확인한다.
- Worker는 전체 suite·integration·end-to-end·전체 lint/build/type check와 전체 verify를 실행하지 않는다. 기존 전체 검증 스크립트의 수집 범위를 유지하고 새 모듈이 필요할 때만 해당 환경의 검증 목록을 갱신한다. Reviewer는 전달된 targeted test·Manager verify 결과와 diff에서 사건 연결·권한·transaction·검증 강도 보존을 읽기 전용으로 확인한다.

## Progress

- [x] 사용자 제공 사건과 현재 관련 입력 판별·workspace·validation·publication 코드 경계 확인
- [x] PRODUCT 및 G-118에 근거한 결함 수정 범위와 회귀·검증 책임 정의
- [x] 평가 대상 관련 입력 추적과 재판단 예약·감사 영속화
- [x] 검증된 오류 사건 연결과 조건부 해제·완료 복구
- [x] 관련 targeted unit test 작성·실행 및 검증 목록 유지
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

Worker targeted 실행(모두 최종 통과):

- `python3 -m unittest tests.test_planning_rebase.PlanningDocumentRelevanceTest` — 5 cases. 첫 실행에서 제거된 dependency의 새 원격 경로를 요구해 실패했고 수정 후 동일 명령이 통과했다.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_saved_unsafe_rechecks_changed_target_and_releases_only_linked_error` — 현재·legacy `unsafe`, 독립 오류, 재시작·stale 검증 통과.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_unrelated_advance_does_not_schedule_completed_unsafe_again` — 무관한 원격 전진에서 재호출 대기와 사건 연결을 만들지 않는 1 case 통과.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_additional_related_advance_rebinds_one_pending_correction` — 추가 관련 전진의 경로 문맥·최신 base 재결합과 단일 pending 1 case 통과.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_exhausted_validation_error_retries_once_after_relevant_change` 및 `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_related_document_change_requests_saved_planner_recheck` — 각 1 case 통과.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_fresh_unsafe_records_error_origin_for_later_correction` 및 `python3 -m unittest tests.test_planning_recovery.PlanningDiagnosticsTest.test_successive_bases_keep_original_result_and_error_audit` — 각 1 case 통과.
- `python3 -m unittest tests.test_runtime_state.RuntimeStateRepositoryTest.test_planning_correction_releases_only_bound_incident_atomically` 및 `python3 -m unittest tests.test_runtime_state.RuntimeStateRepositoryTest.test_planning_error_release_rolls_back_with_incident_on_interruption` — 각 1 case 통과.
- `python3 -m unittest tests.test_manager_schema.ManagerSchemaTest.test_existing_database_adds_planning_fingerprint_without_losing_state` — 1 case 통과.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_completed_no_change_turn_resumes_validation_without_agent_replay`, `python3 -m unittest tests.test_planning_recovery.PlanningConflictCorrectionUnitTests.test_workspace_wiring_preserves_error_and_commits_base_before_cleanup`, `python3 -m unittest tests.test_planning_publication.PlanningPublicationTest.test_no_change_cleanup_failure_keeps_saved_planning_result` — 기존 완료 결과 재사용·workspace 복구·publication guard의 각 1 case 통과.
- Manager verify 피드백에서 legacy 유지보수 fixture가 새 tiered 포트와 실제 Codex 모델 조회를 사용해 기존 경로 검증을 중단했다. fixture의 해당 경로만 명시적으로 유지하고 `unused.ini` Planner 역할에 가짜 프로필을 제공했다. 보고된 개별 case 4건과 fixture unit case 1건을 직접 재실행해 통과했다.
- Review 피드백: 일반 Planner는 READY 대상의 `sources` 밖에 새로 생긴 활성 Requirement도 재평가하며, 일반 planning이 결합되지 않은 유지보수 판단은 무관한 Requirement와 전체 지문 변경만으로 소진된 오류를 재시도하지 않는다. 새 fake-port unit case 3건과 내용 보존 archive 회귀 case 1건을 개별 실행해 통과했다.
- Manager verify 피드백: 관련 source 변경을 가정하던 기존 fake-port case에 READY 평가 대상과 변경 문서 응답이 빠져 있었다. 둘을 명시하고 `python3 -m unittest tests.test_planning_selection.PlanningSelectionTest.test_failed_maintenance_input_restarts_after_related_source_change`를 개별 실행해 통과했다.

- Review 후속: 일반 planning을 포함하는 유지보수 cycle에서 문서 변경 없이 전체 입력 지문만 변경돼도 소진된 validation 오류의 교정을 예약하도록 조건을 보완했다. 내용 보존 archive와 일반 planning이 없는 유지보수의 무관 입력은 계속 오류를 보존한다.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_combined_maintenance_code_change_resets_exhausted_error_once tests.test_planning_recovery.PlanningRecoveryTest.test_unrelated_maintenance_requirement_does_not_reset_exhausted_error` — 최초 실행은 새 fake-port fixture의 maintenance context 응답 누락으로 실패했다. 응답을 명시한 뒤 동일 명령 재실행에서 2 cases 모두 통과했다.
- `tests/test_maintenance_cycle.py::MaintenanceCycleTest.test_remote_target_edit_recovers_unsafe_in_same_session_and_completes_cycle`를 추가했다. 기존 격리 remote·실제 Git/SQLite/catalog/validation fixture에서 무수정 unsafe 보존, 동일 입력 무호출, 별도 clone의 대상 commit/push, 같은 session/worktree의 revalidated, idle·오류 해제·catalog 반영·원래 오류 감사와 불필요한 push 부재를 확인한다. Worker의 integration 실행 금지에 따라 실행하지 않았으며 기존 verify의 unittest discover로 Manager가 실행한다. 새 루트 파일이나 검증 스크립트 변경은 없다.


- Review 2차 후속: 완료된 결과의 재판단도 문서 변경과 실제 전체 입력 지문 변경을 함께 사용한다. 일반 planning 및 일반 planning을 포함한 유지보수는 코드만 바뀌어도 이전 결과를 감사에 보존하고 base 채택과 pending·오류 사건 연결을 함께 저장한다. 실패 gate 지문 불일치 자체를 입력 변경으로 간주하지 않으며, 동일 내용·내용 보존 archive와 유지보수 전용의 무관 코드 변경은 재호출하지 않는다.
- `python3 -m unittest tests.test_planning_recovery.PlanningRecoveryTest.test_completed_results_recheck_code_input_before_recording_success tests.test_planning_recovery.PlanningRecoveryTest.test_combined_maintenance_code_change_resets_exhausted_error_once tests.test_planning_recovery.PlanningRecoveryTest.test_unrelated_maintenance_requirement_does_not_reset_exhausted_error tests.test_planning_recovery.PlanningRecoveryTest.test_saved_unsafe_rechecks_changed_target_and_releases_only_linked_error` — 4 cases 모두 첫 실행 통과. 새 case는 3개 mode × 3개 변경 유형에서 완료 결과 재판단, base 채택 후 재시작, 동일 세션 1회 호출, 검증 후 연결 오류 해제와 publication 전 성공 기준 보존을 확인한다. 전체 verify·integration은 실행하지 않았다.

- Manager verify 후속(2개 실패): 기존 no-change 테스트가 전체 입력 지문 또는 실제 `src/app.py`를 바꾸면서도 재호출하지 않는 이전 기대를 사용했다. fake-port case를 `test_remote_input_identity_controls_completed_result_reuse_after_pause`로 개정해 동일 입력의 결과 재사용과 변경 입력의 같은 세션 재호출을 함께 검증한다. 실제 Git case는 코드 변경 후 두 번째 Planner 호출과 최신 코드·동일 세션을 확인하도록 수정했다. 제품 코드는 변경하지 않았다.
- `python3 -m unittest tests.test_planning_selection.PlanningSelectionTest.test_remote_input_identity_controls_completed_result_reuse_after_pause` — 1 case(2 subtests) 첫 실행 통과. 수정한 `test_catalog_scheduling.CatalogSchedulingTest.test_remote_input_change_after_planner_defers_published_ready_worker`는 실제 Git integration이므로 Worker에서 실행하지 않았으며 Manager 전체 verify의 재검증이 필요하다. 기존 verify 수집 범위는 유지했다.

## Decision Log

- 2026-10-03: 사용자는 “관련 원격 입력 변경 후 보존된 Planner 판단 재평가와 오류 해제” 범위의 새 ExecPlan 작성을 요청했다. 기존 PRODUCT와 MERGED된 G-118의 누락된 복구 동작을 수정하므로 새 Requirement나 제품 정책을 추가하지 않는다. `sources`는 현재 PRODUCT로 두고 G-118은 복구 계약·감사 참조로 사용한다. 이미 구현된 경계의 보완이므로 새 미완료 선행 계획은 없으며 `depends_on`은 비워 둔다.
- 2026-10-03: 코드 수정 없이 문서만 작성했다. 사건 SHA·session·수동 복구 결과는 사용자 제공 증거와 코드 경로 확인을 구분하며 운영 DB, 사건 감사 파일이나 기존 계획을 수정하지 않는다. 기존 G-130·G-131·G-132와의 ID 충돌을 피하도록 사용자 판단에 따라 이 계획의 번호를 G-133으로 변경했다. 기존 입력 분류 G-130과 G-131·G-132의 참조는 유지한다.
- 2026-10-03: priority는 안전한 무인 재개를 막고 반복 수동 복구를 요구하는 결함이므로 `critical`, 복잡도와 위험은 입력·사건·중단 복구·게시 transaction을 함께 다루므로 `high`로 둔다. 실제 보호 실패를 해제하거나 publication guard를 제거하는 우회는 구현하지 않는다.
- 2026-10-03: 기본 worktree 동기화, Worker/Reviewer의 문서 재확인 범위, Planner 권한, maintenance mode·장벽, 재시도 한도와 사용자 Merge 규칙은 변경하지 않는다. PRODUCT·Requirement·ARCHITECTURE·AGENTS·PLANS는 이 문서 작성에서 수정하지 않는다. 비호환 schema·상태 모델 변경, 데이터 삭제·비가역 migration이나 Git/PR 경계 변경이 필요해지면 기존 Architecture Change Policy에 따라 구현 전에 사용자 결정을 받는다.
- 2026-10-03 Worker: 원격 전후 Git 선언과 stable ID를 비교해 eligible READY 또는 revalidation 대상의 본문·source·dependency·정책 변경을 판별한다. 무수정 완료 결과의 재판단은 기존 session과 worktree에 pending으로 저장하며 이전 결과·오류·재시도는 base별 진단에 보존한다. 새 SQLite 열은 nullable인 호환 추가이며 기존 데이터 삭제나 비호환 상태 전이는 없다.
- 2026-10-03 Worker: 오류 원인 종류와 사건 ID, 재판단 base SHA 및 maintenance phase/revision을 연결하고 fresh 결과가 실제 검증을 통과한 뒤 원격 SHA와 현재 사건을 다시 확인해 기존 transaction에서 오류와 알림 차단을 해제한다. Legacy 상태는 보존 결과·재시도·진단에서 연결을 확인할 때만 보완한다. 독립 오류와 재차 `unsafe`는 유지한다. `scripts/verify.sh`의 기존 unittest discover·compile 목록이 변경 파일을 이미 포함하므로 검증 스크립트는 수정하지 않았다. 전체 verify·Review·Merge는 Loop Manager의 후속 경계에 남긴다.
