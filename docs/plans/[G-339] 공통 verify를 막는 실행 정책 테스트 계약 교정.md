---
id: G-339
title: 공통 verify를 막는 실행 정책 테스트 계약 교정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: low
risk: low
priority: high
---

## Purpose

공통 결함 보고 369에서 확인한 두 테스트 계약 오류를 교정해 현재 실행 정책의 필수 회귀를 복구한다. `execution_policy_fixture()`는 Manager 문서 결합의 Git 관측을 제공하지 않고, 물리 CLI 재시도 테스트는 실제 실패 세 번 뒤 5회 도달 표식을 기대한다. 두 원인은 구분하되 어느 하나만 고치면 같은 필수 전체 verify가 나머지 실패로 막히므로 하나의 검증·Merge 단위로 처리한다. 이미 반영된 R-061의 작업 base 결합과 R-055의 5회 기준·수집·차단 정책은 유지한다.

## Acceptance Criteria

- 코드만 변경하는 정책 fixture에서 작업 base 조회와 commit 전 문서 경로 관측, commit 후 후보 관측을 제공한다. base·HEAD·dirty/commit 전이가 일치하며 문서 변경이 없다는 관측을 실제 문서 검증 생략으로 대체하지 않는다.
- 현재 `merge-base base HEAD`와 head 인수가 없는 `diff --no-ext-diff --name-status -z --find-renames base`, 기존 base/candidate diff를 필요한 정확한 형태로 지원한다. 예상 밖 명령·다른 인수는 계속 실패하며 모든 `diff`·`merge-base`를 일괄 성공 처리하지 않는다.
- 기존 verify/Review 실패·재시도·session 유지·profile 승격·최종 상태와 실패 이력 assertion이 실제 handler·SQLite에서 유지된다. 실제 Git·Agent를 실행하거나 문서 보고 함수·정책 판단을 성공 mock으로 바꾸지 않는다.
- `test_targeted_records_follow_each_physical_retry_and_session_fallback`은 기존 세 물리 호출과 `old-session`, `old-session`, 새 session의 fallback 순서, 서로 다른 invocation/run 소유권과 결과 세 건을 계속 검사한다. 실제 SQLite에서 같은 `suite.test_one`의 실패 누계 3·차단 도달 표식 없음과 각 실행의 test ID·실패 결과·호출 귀속을 확인한다. session 전환에 따른 초기화나 기록 누락이 통과하지 못하게 한다.
- 다섯 번째 고유 실행에서만 도달하는 기존 `TargetedHistoryTests`와 성공·skip·expected failure 초기화, 중복 비소비·다른 test ID 비합산·Worker 진입 차단 회귀를 유지한다. 물리 CLI 재시도 수를 늘리거나 운영 threshold·fallback·수집/ledger를 변경하지 않으며 잘못된 assertion만 삭제하지 않는다.
- `test_execution_documents.py`의 실제 설명 누락·patch 변경·선택 ExecPlan 제외·작업 base 조회 실패 회귀와 현재 verify 수집·필수 compile을 유지한다. 운영 보고·게시·차단 정책이나 adapter를 개편하지 않는다.

## Implementation

- [x] `execution_policy_fixture()`가 `merge-base base HEAD`와 후보 SHA 변형, head 인자 없는 정확한 base name-status diff에만 응답하도록 했다. 기존 후보 diff·base/candidate binary diff와 예상 밖 Git 거부 assertion을 유지했다.
- [x] 세 물리 재시도 테스트가 SQLite의 실패 누계 3·비도달 표식·세 test 결과·각기 다른 run 및 invocation 귀속을 확인하도록 교정했다. 실행 수와 운영 임계값은 바꾸지 않았다.
- [x] 문서 결합·재시도 실패·session 및 Review 경계 테스트와 기존 다섯 번째 고유 실패 도달 테스트를 targeted 실행했다.
- [x] verify 피드백의 복구·publication 테스트 Git double에도 정확한 작업 base merge-base와 head 없는 문서 경로 diff 관측을 추가했다. checkpoint recovery double에서는 ancestry 검사 외 merge-base를 원래 fixture에 위임해 복구된 VERIFYING context 검증을 유지했다. 제품 코드·상태 assertion은 변경하지 않았다.

## Validation

- Git fixture 교정의 Worker targeted unit은 `tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_distinct_verify_failures_recover_in_one_run_with_persistent_sessions`, 같은 class의 예상 밖 Git 거부 case, `tests.test_recovery_history.RecoveryHistoryTest.test_review_protocol_errors_do_not_retry_worker_or_consume_history`, `tests.test_review_profile_escalation.ReviewDrivenWorkerProfileEscalationTest.test_reviewer_cli_failure_does_not_escalate_worker`다. 기존 역할/호출 횟수·session·phase/status·실패/승격 이력과 정확한 base/HEAD·commit 전후 관측, Git subprocess 0회를 확인한다.
- targeted 기록 교정의 Worker unit은 위 `RunnerTests` 한 case와 `tests.test_targeted_test_streak.TargetedHistoryTests.test_only_five_distinct_runs_of_the_same_test_trigger_the_limit`이다. 세 호출·세 run의 귀속/결과·실패 누계 3·표식 없음과 다섯 번째 도달을 실제 저장 결과로 확인한다.
- 전체 수집의 기존 문서 보고 회귀는 실제 누락/patch 변경·base 조회 실패를 계속 거부하고, targeted 회귀는 session/commit 변경 뒤 이력 유지·중복 비소비·수집 실패 보존·5회 뒤 성공 초기화·Worker 진입 차단을 유지한다. 정책 모듈의 나머지 조건도 기존 수집대로 실행하며 새로운 실패는 별도 원인으로 남긴다. 새 repository·실제 CLI/게시 시나리오나 검증 체계를 추가하지 않는다.

## Progress

- [x] revision `7b2979597ed1af97b19409c375057747ee3b0704`에서 보고 369의 두 관측을 현재 명세·코드와 정적으로 대조했다. 지역 Git override에는 commit 전 diff·현재 merge-base 응답이 없고 재시도 테스트는 세 결과 뒤 5회 표식을 기대한다. 테스트는 실행하지 않았다.
- [x] 정확한 Git 관측 추가와 기존 정책 assertion 확인
- [x] 세 실패의 누계·비도달·귀속 assertion 교정과 기존 5회 회귀 확인

## Validation Record

- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_distinct_verify_failures_recover_in_one_run_with_persistent_sessions tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_git_observation_failure_pauses_without_discarding_saved_worker tests.test_recovery_history.RecoveryHistoryTest.test_review_protocol_errors_do_not_retry_worker_or_consume_history tests.test_review_profile_escalation.ReviewDrivenWorkerProfileEscalationTest.test_reviewer_cli_failure_does_not_escalate_worker tests.test_agent_invocation.RunnerTests.test_targeted_records_follow_each_physical_retry_and_session_fallback tests.test_targeted_test_streak.TargetedHistoryTests.test_only_five_distinct_runs_of_the_same_test_trigger_the_limit` — 첫 실행에서 fixture의 `merge-base base HEAD` 미지원으로 실패.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_distinct_verify_failures_recover_in_one_run_with_persistent_sessions tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_git_observation_failure_pauses_without_discarding_saved_worker tests.test_recovery_history.RecoveryHistoryTest.test_review_protocol_errors_do_not_retry_worker_or_consume_history tests.test_review_profile_escalation.ReviewDrivenWorkerProfileEscalationTest.test_reviewer_cli_failure_does_not_escalate_worker tests.test_agent_invocation.RunnerTests.test_targeted_records_follow_each_physical_retry_and_session_fallback tests.test_targeted_test_streak.TargetedHistoryTests.test_only_five_distinct_runs_of_the_same_test_trigger_the_limit` — 같은 명령 재실행 및 후속 재실행에서 모두 6개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_distinct_verify_failures_recover_in_one_run_with_persistent_sessions tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_git_observation_failure_pauses_without_discarding_saved_worker tests.test_recovery_history.RecoveryHistoryTest.test_review_protocol_errors_do_not_retry_worker_or_consume_history tests.test_review_profile_escalation.ReviewDrivenWorkerProfileEscalationTest.test_reviewer_cli_failure_does_not_escalate_worker tests.test_agent_invocation.RunnerTests.test_targeted_records_follow_each_physical_retry_and_session_fallback tests.test_targeted_test_streak.TargetedHistoryTests.test_only_five_distinct_runs_of_the_same_test_trigger_the_limit` — 6 tests passed.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_documents.CommonGuidancePrReportTests.test_selected_execplan_alone_needs_no_report_or_patch_hash tests.test_execution_documents.CommonGuidancePrReportTests.test_only_selected_execplan_is_excluded_from_mixed_document_reports tests.test_execution_documents.CommonGuidancePrReportTests.test_manager_binds_reports_and_rejects_changed_patch_or_result tests.test_execution_documents.CommonGuidancePrReportTests.test_all_missing_changed_and_malformed_reports_are_returned_together tests.test_execution_documents.WorkerDocumentReportBaseTests` — 6 tests passed.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_preparation_resumes_durable_rebase_continue_without_worker tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_current_pr_lease_survives_recovery_and_fresh_publication tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_existing_remote_head_creates_pr_without_repeated_push tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_live_remote_creation_before_publication_is_rejected_despite_absent_cache tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_manual_stage_continue_rechecks_worker_verify_review_and_publication_gate tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_stale_cache_recovery_reaches_actual_push_adapter_after_fresh_validation tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_unsuccessful_fresh_verify_does_not_open_review_or_publication` — 7 tests passed.
- 최초 실행은 fixture가 `merge-base base HEAD` 관측을 빠뜨려 4개 정책 테스트를 실패시켰다. exact 인수 허용을 추가한 뒤 동일 targeted 명령으로 모두 통과했다.
- 추가 verify 피드백의 6개 publication/recovery 실패와 1개 checkpoint recovery 오류도 Git double이 문서 보고의 작업 base 조회를 제공하지 않은 같은 원인이었다. 정확한 관측을 보완해 전달된 7개 케이스가 통과했다.
- 반복 전달된 동일 verify 피드백에 대해 지목된 7개 케이스와 G-339 정책 targeted 6개를 다시 실행해 모두 통과했다. Worker 범위에서 전체 verify를 실행하지 않았다.
- 전체 verify는 Worker 범위 밖이므로 실행하지 않았다.

## Decision Log

- 두 원인은 Git 관측 누락과 targeted 테스트 기대 오류로 다르며 원인·source·완료 조건을 각각 추적한다. 같은 필수 verify를 막는 확인된 회귀여서 함께 교정해 독립 Merge 가능한 한 단위로 둔다. 상호 dependency나 먼저 Merge돼야 할 교정의 순환을 만들지 않는다.
- G-333은 지정 사례의 실제 Git 준비 제거를 소유하며 이 지역 double의 누락 교정과 완료 조건이 다르다. G-189는 전체 후속 차단 감사, G-292는 Claude 기록 전달을 계속 소유한다. 이 계획은 운영 정책과 그 작업 범위를 확대하지 않는다.
- complexity low·최초 level 1: 기존 관측 double과 실제 저장 결과 assertion의 국소 교정이다. risk low: 테스트 전용이며 예상 밖 입력 거부·실제 정책/DB·정확한 누계/귀속 assertion을 보존해 회귀 은폐를 막는다. priority high는 여러 정책 모듈과 G-332의 필수 verify를 막는 확인된 결함을 해소하기 위한 분류다.
- 현재 protocol/선언 version 1, launcher Python의 unittest 독립 모듈 수집과 필수 compile을 유지한다. 계획 작성·정적 대조·게시를 교정 적용·테스트/verify 성공·차단 해제로 취급하지 않으며 실제 반영 뒤 영향 작업의 원인별 재평가는 Manager가 수행한다.
