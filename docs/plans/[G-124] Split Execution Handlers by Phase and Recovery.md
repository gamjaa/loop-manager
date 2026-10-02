---
id: G-124
title: Split Execution Handlers by Phase and Recovery
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-123
complexity: high
risk: high
priority: critical
---

## Purpose

`execution_handlers.py`는 4,902줄이고 하나의 `ExecutionHandlers`가 workspace 준비, rebase 복구, Worker, verify, Review, publication과 문서 승인 처리를 맡는다. 대응 테스트도 7,521줄이다. 기존 phase coordinator와 effect port를 유지하면서 단계별 실행과 공통 검증 경계를 분리한다.

## Acceptance Criteria

- workspace/recovery, revision·validation gate, Worker 실행, verify, Review, publication과 문서 승인 처리는 별도 책임으로 분리된다. 기존 `ExecutionHandlers`는 좁은 구성·위임만 담당하고 같은 규모의 거대한 context/service로 본문을 옮기지 않는다.
- 각 단계는 필요한 typed port와 immutable 실행 context만 받는다. 전체 effects 객체, 임의 `SimpleNamespace` 또는 root module을 넘겨 의존성을 숨기지 않으며 불필요한 전역 가변 상태를 추가하지 않는다.
- session/profile 결정, Worker 결과·scope 확인, feedback delta, latest-base reconciliation, archive eligibility와 validation token의 동일 계약을 기존 service로 재사용한다. 단계마다 필요한 재검증 시점은 보존한다.
- IMPLEMENTING→VERIFYING→REVIEWING→PUBLISHING 전이, retry·usage pause·Review 교정 한도, 중단 후 같은 세션 재개, rebase와 중간 checkpoint, archive 변경 시 재검증을 보존한다.
- Worker targeted test, Manager 전체 verify와 Reviewer 읽기 전용 검토의 책임을 유지한다. 단계별 refactor를 이유로 직접 전체 verify를 호출하거나 Reviewer에게 test 실행을 넘기지 않는다.
- 큰 테스트 파일은 단계별 unit test와 recovery/revision 경계 테스트로 나뉘고 G-119 support를 이용한다. 성공·실패·race·재시작 시나리오와 assertion은 그대로 유지한다.

## Implementation

1. `guard`·`recover`·`run` 등 큰 method의 입력, 효과와 상태 확정을 G-060 대응표에 연결한다. `execution_runtime`의 coordinator와 `execution_revision`, Worker/session, feedback, checkpoint/rebase의 기존 경계를 우선한다.
2. contract/effects는 독립 module로 두고 workspace/recovery, Worker, validation, Review, publication, document 처리의 좁은 handler를 구성한다. 공유하는 실행 context에 새 가변 필드를 누적하지 않는다.
3. 같은 계약의 중복 로직만 담당 service로 통합한다. 서로 다른 단계의 lease·scope·archive 확인을 한 번의 오래된 결과로 대체하지 않는다.
4. `tests/test_execution_handlers.py`를 단계·검증 경계별 모듈로 나눈다. fixture 생성과 phase별 효과 fake를 support로 분리하되 fixture가 구현의 상태 전이를 대신 수행하지 않게 한다.
5. G-123의 application assembly는 새 handler를 조합하도록 최소한으로 갱신한다. 외부 호출 계약과 typed outcome·예외·저장 결과를 보존한다.

## Validation

- Worker는 변경한 각 단계의 정상 결과와 직접 관련 실패·재개 경계만 선택해 targeted unit test를 실행한다. 분할된 모든 테스트를 합쳐 전체 suite처럼 실행하지 않는다.
- Worker session 유지/교체, stale revision token 거부, verify 실패 전달, Review 교정 한도, base 변경 후 재검증, archive eligibility 변화와 게시 실패 재개를 검증한다.
- effect 호출 순서와 transaction 결과를 확인해 partial success를 완료로 기록하지 않는지 검증한다. 문서 승인·정책 결정의 기존 사용자 경계도 보존한다.
- Manager 전체 verify에서 모든 실행 단계·복구·publication 시나리오가 통과해야 한다. Reviewer는 단일 handler에 책임이 다시 누적되거나 공유 gate가 약화되지 않았는지 diff와 전달 결과를 확인한다.

## Progress

- [x] phase·recovery·validation 책임과 기존 service 대응표 확인
- [x] 단계별 handler·좁은 port 추출 및 중복 제거
- [x] 실행 테스트 분리와 targeted unit test
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

### 구현·검증 기록 (2026-10-02)

- G-060의 `G-124: 실행 phase와 recovery 대응`에 원본 method의 입력·효과·상태 확정, 기존 service와 목적지, 현재 root 소유 경계를 기록했다.
- 90개 method를 13개 책임으로 나누고 기존 facade에는 immutable 준비 snapshot의 교체와 명시적 구성·위임만 남겼다. 전체 effects는 조립 단계에서 분해하며 각 handler는 frozen port와 immutable 실행 identity/context만 사용한다. session·feedback·checkpoint·archive·revision 정책 구현을 재사용한다.
- port·snapshot 접근과 annotation을 역대응한 AST 비교에서 89개 원본 method 본문이 일치했다. 나머지 한 곳은 준비 중 rebase identity의 임시 namespace를 frozen context로 바꾼 부분이다. 기존 호출 인자·default와 예외 identity를 유지했다.
- 원본 277개 test method와 assertion을 단계·복구 경계 모듈로 옮겼다. 공유 setup·assertion은 G-119 support를 사용한다. composition test 3개를 추가하고 evidence-only fixture 및 중복 discovery 검사도 새 구성에 맞췄다.
- `scripts/verify.sh`와 `scripts/verify.ps1`의 compile 대상에 새 module을 추가했다. 전체 verify·integration·end-to-end·전체 lint/build/type check는 실행하지 않았다.
- 아래 개별 unit test 선택 명령으로 고유 test case 34개가 최종 통과했다. 재시작·race는 in-memory SQLite와 외부 효과 fake로 검증했으며 실제 Git repository를 생성하는 기존 시나리오는 Manager verify에 남겼다.

```text
python3 -m unittest tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_revision_guard_tokens_follow_complete_phase_lifecycle tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_source_revision_invalidates_artifacts_but_preserves_sessions_and_workspace tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_archive_change_returns_to_worker_before_push -v
python3 -m unittest tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_level_and_alias_change_with_same_resolved_model_resumes_session tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_model_replacement_records_intent_before_agent_and_binds_event tests.test_execution_failure_policy.ExecutionFailurePolicyTests.test_unavailable_fallback_intent_survives_failed_new_start -v
python3 -m unittest tests.test_execution_verify_phase.ExecutionVerifyPhaseTests.test_timeout_verify_uses_stable_fingerprint_and_returns_to_worker tests.test_execution_verify_phase.ExecutionVerifyPhaseTests.test_head_change_during_verify_does_not_save_success tests.test_execution_verify_phase.ExecutionVerifyPhaseTests.test_latest_base_uses_same_intent_and_rebase_for_both_checkpoints -v
python3 -m unittest tests.test_execution_review_phase.ExecutionReviewPhaseTests.test_review_pass_after_third_correction_advances_to_publication tests.test_execution_review_phase.ExecutionReviewPhaseTests.test_review_budget_pauses_before_worker_and_preserves_projection tests.test_execution_review_phase.ExecutionReviewPhaseTests.test_review_retry_interruption_preserves_previous_raw_result -v
python3 -m unittest tests.test_execution_documents.ExecutionDocumentsTests.test_document_plan_applies_program_bytes_without_worker_agent tests.test_execution_documents.ExecutionDocumentsTests.test_document_publication_stops_before_pr_when_original_is_stale tests.test_execution_documents.ExecutionDocumentsTests.test_document_uncertainty_persists_audit_and_blocks_plan -v
python3 -m unittest tests.test_execution_publication_phase.ExecutionPublicationPhaseTests.test_publication_interruption_recovers_existing_pr_without_repush tests.test_execution_publication_phase.ExecutionPublicationPhaseTests.test_pr_conflict_lease_rejection_blocks_without_opening_pr tests.test_execution_task_relevant_publication_boundary.TaskRelevantPublicationBoundaryTests.test_base_advance_before_pr_creation_returns_to_existing_revalidation_flow -v
python3 -m unittest tests.test_execution_workspace_phase.ExecutionWorkspacePhaseTests.test_worktree_recovery_failure_keeps_durable_pending_boundary tests.test_execution_workspace_phase.ExecutionWorkspacePhaseTests.test_workspace_violation_blocks_before_agent tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests.test_partial_stage_resume_only_stages_remaining_dirty_path tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests.test_partial_stage_resume_rejects_replaced_clean_resolution -v
python3 -m unittest tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_manual_completion_without_journal_invalidates_worker_and_validation_once tests.test_execution_immediate_rebase_publication_phase.ImmediateRebasePublicationTests.test_interruption_after_push_confirms_once_without_repeating_push tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_old_success_results_cannot_publish_without_current_worker_confirmation -v
python3 -m unittest tests.test_execution_handlers.ExecutionCompositionTests -v
python3 -m unittest tests.test_rebase_file_evidence.AutostashFileEvidenceTests.test_both_rebase_snapshot_paths_capture_file_evidence tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_resumed_continue_uses_the_saved_journal_identity tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_stage_capture_binds_modes_to_approved_file_signatures -v
python3 -m unittest tests.test_execution_review_phase.ExecutionReviewPhaseTests.test_review_specification_deadlock_blocks_plan_without_worker_correction tests.test_execution_review_phase.ExecutionReviewPhaseTests.test_review_deadlock_accepts_architecture_outside_sources tests.test_blocked_checkpoint.BlockedCheckpointDiscoveryUnitTests.test_git_fixture_cases_are_collected_once_across_consuming_modules -v
```

중간 checkpoint test의 첫 실행은 공유 fixture가 기대한 `git_operation_entries` mock을 추출 helper에서 None으로 만든 setup 오류로 실패했다. 일반 phase fixture의 원래 mock 기본값을 복원하고 고립된 evidence fake에서만 선택적 port 부재를 표현하도록 수정했다.

FAILED_COMMAND: python3 -m unittest tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_manual_completion_without_journal_invalidates_worker_and_validation_once tests.test_execution_immediate_rebase_publication_phase.ImmediateRebasePublicationTests.test_interruption_after_push_confirms_once_without_repeating_push tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_old_success_results_cannot_publish_without_current_worker_confirmation -v
Initial run: ERROR (1 fixture setup error). git_operation_entries의 원래 mock 기본값을 복원했다.
RERUN: python3 -m unittest tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_manual_completion_without_journal_invalidates_worker_and_validation_once tests.test_execution_immediate_rebase_publication_phase.ImmediateRebasePublicationTests.test_interruption_after_push_confirms_once_without_repeating_push tests.test_execution_evidence_free_recovery_phase.EvidenceFreeRecoveryLifecycleTests.test_old_success_results_cannot_publish_without_current_worker_confirmation -v
최종 결과: 동일 명령의 3개 test 모두 PASS. 나머지 선택 명령도 모두 PASS.

- 2026-10-02 보고 보정: 최초 Worker 응답은 FAILED_COMMAND/RERUN을 TARGETED_TESTS 밖에 두고 그 안에는 실패 시나리오 설명을 남겨 결과 검증에서 거부됐다. 실제 테스트 재실행은 통과했으므로 runtime 정책은 변경하지 않았다. 재실행 기록과 최초 ERROR, 최종 PASS를 TARGETED_TESTS 안에 함께 배치했다.
- 위 checkpoint 선택 명령을 다시 실행해 3개 PASS를 확인했다. `python3 -m unittest tests.test_worker_result_contract.WorkerResultContractTest.test_targeted_test_evidence_requires_a_clean_explicit_rerun -v`도 1개 PASS이며, 최종 응답 원문은 기존 publication/outcome parser로 검증했다. 전체 verify는 실행하지 않았다.

### 검증 피드백 대응 (2026-10-02)

- immutable snapshot의 `sources`·`depends_on` tuple이 기존 helper의 dict/list 계약에 그대로 전달되어 문서 승인 guard, 저장된 Worker 결과의 재시작 복구와 PR 본문 생성에서 회귀가 발생했다. snapshot을 가변으로 되돌리지 않고 기존 helper 호출 경계에서 독립된 dict/list 복사본을 전달한다. Worker 결과 parser 5개 phase, workspace/revision level helper와 publication renderer에 같은 변환을 적용하고 문서 승인 validator도 기존 입력 계약을 유지한다.
- 분리된 blocked scope test가 다른 module의 test class 이름을 참조해 import되지 않았다. 공통 setup을 기존 `tests/support/current_rebase.py`의 non-TestCase fixture로 추출하고 두 module에서 재사용한다. 기존 scenario와 assertion은 보존하고 중복 discovery를 피한다.
- PR publication 검증의 실제 Git 시나리오는 실행하지 않았다. 해당 traceback의 `build_pr_body` list assertion을 실제 renderer를 사용하는 composition unit test로 직접 검증했다. parser 입력의 독립성, `sources`·`depends_on` 불변성 및 preparation/revision helper 경계도 새 unit test로 확인했다.

FAILED_COMMAND: python3 -m unittest tests.test_execution_documents.ExecutionDocumentsTests.test_document_execution_guard_preserves_crlf_approved_diff tests.test_execution_failure_policy.ExecutionFailurePolicyTests.test_saved_worker_result_finishes_after_restart_without_second_agent_turn tests.test_execution_evidence_free_blocked_scope -v
Initial run: ERROR (test module import NameError). fixture 수정 뒤 동일 명령의 중간 실행은 FAIL 1개, ERROR 1개였으며 helper metadata 입력 계약을 복원했다.
RERUN: python3 -m unittest tests.test_execution_documents.ExecutionDocumentsTests.test_document_execution_guard_preserves_crlf_approved_diff tests.test_execution_failure_policy.ExecutionFailurePolicyTests.test_saved_worker_result_finishes_after_restart_without_second_agent_turn tests.test_execution_evidence_free_blocked_scope -v
최종 결과: 8개 모두 PASS, exit code 0.

추가 targeted unit test:

```text
python3 -m unittest tests.test_execution_handlers.ExecutionCompositionTests.test_phase_parsers_receive_owned_legacy_metadata_without_mutating_snapshot tests.test_execution_handlers.ExecutionCompositionTests.test_publication_renderer_accepts_immutable_plan_sources tests.test_execution_current_rebase_agent_scope.CurrentRebaseAgentScopeTests.test_resumed_scope_keeps_original_and_records_observed_comparison_start tests.test_blocked_checkpoint.BlockedCheckpointDiscoveryUnitTests.test_git_fixture_cases_are_collected_once_across_consuming_modules -v
```

최종 결과: 4개 모두 PASS, exit code 0.

```text
python3 -m unittest tests.test_execution_handlers.ExecutionCompositionTests.test_preparation_and_revision_level_helpers_receive_owned_metadata -v
```

최종 결과: 1개 PASS, exit code 0. Worker는 전체 verify를 실행하지 않았다.

## Decision Log

- 2026-10-02: 이미 분리된 `execution_runtime` coordinator를 다시 만들지 않는다. phase 전이는 coordinator, 외부 효과는 adapter, phase의 작업은 해당 handler가 담당하도록 기존 경계를 완성한다.
- 실제 저장 형식과 실행 결과를 보존하는 리팩터링이며 새로운 phase, retry 정책과 검증 생략을 도입하지 않는다.
- 2026-10-02: phase 전이는 기존 coordinator에 남겼다. 공유 준비 정보는 frozen snapshot 교체로만 갱신하며 각 효과 뒤의 revision·scope·archive·후보 재검사를 합치거나 생략하지 않았다. 기존 recovery fixture의 private patch 지점은 facade의 명시적 callback으로 유지한다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
