---
id: G-109
title: Unified Managed Rebase Adoption and Revalidation
sources:
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-108
complexity: high
risk: critical
priority: critical
---

## Purpose

G-108의 현재 상태 판정을 일반 구현, 관리 rebase 재시작, PR 충돌 복구와 PR 없는 checkpoint branch에 일관되게 연결한다. 과거 작업 증거 없이 해결·완료한 결과를 채택하되 현재 입력과 최종 후보의 Worker 확인·targeted test·전체 verify·Review를 다시 거치게 한다.

## Acceptance Criteria

- 모든 기존 관리 rebase의 시작·충돌 관측·stage/continue·완료·재진입 경로는 G-108의 현재 상태 기준을 사용한다. 자동 Worker 해결과 사용자의 파일 해결·stage·`rebase --continue`·완료가 같은 결과로 처리된다. 과거 Worker 교정 turn·승인 tree·receipt·reflog 표식·commit별 replay 또는 완전한 journal이 없다는 이유로 진행·완료를 거부하지 않는다.
- 소유 rebase가 진행 중이면 현재 충돌·operation·staged 내용을 보존한다. 해결 대기는 기존 Worker 세션의 교정 경로로 보내고, 현재 범위 검사를 통과한 해결 내용은 Agent 호출 밖의 Manager가 필요한 path만 stage하고 계속한다. 이미 사용자가 stage·continue·완료한 단계는 반복하지 않는다. 미해결 autostash 복원도 완료·게시로 처리하지 않고 기존 보존·교정 경계를 유지한다.
- 일반 handler와 직접 복구 경로, 기존/신규 checkpoint 형식에서 같은 현재 결과를 채택한다. journal seal 전·후 또는 SQLite 반영 전에 중단돼도 현재 상태를 다시 검사해 완료를 한 번만 반영한다. 과거 증거 부족에 의한 반복 `status_failed`만 남기거나 동일 rebase·stage·Worker 교정을 반복하지 않는다. 범위·identity·target·원격 기대값이 불명확하면 작업물과 실패 감사를 보존한다.
- PR 없는 게시 checkpoint의 원격 OID가 rebased HEAD의 조상이 아니어도 동일 계획의 게시 identity·마지막 채택 원격 기대값·현재 소유 branch/worktree·target·허용 최종 변경을 검사해 재개한다. 원격 후속 commit·삭제·재작성·PR head 변경은 이 예외로 수용하지 않는다. 게시 ownership 검사에 rebase journal/replay의 완전성을 다시 요구하지 않으며 정확한 원격 lease와 기존 PR 게시 조건을 유지한다.
- rebase 결과 HEAD 또는 선택 ExecPlan·근거 입력이 바뀌면 이전 Worker 후보 확인과 verify·Review·publication 증거를 무효화한다. 기존 또는 정상 fallback Worker 세션에서 현재 입력과 최종 diff를 확인하고 관련 targeted test 결과를 받은 뒤 Manager 전체 verify와 Reviewer를 실행한다. 중단·재시작·최신 base 재전진 뒤에도 이전 후보의 성공을 재사용하지 않는다. 현재 선언의 dependency·역할별 범위 검사는 기존 revision 계약을 유지하며 현재 입력 확인을 생략하는 근거로 사용하지 않는다.
- G-104가 담당할 R-047의 중간 checkpoint는 작업물 보존 목적의 별도 게시 경계다. 그 경로를 최종 검증 성공·PR 준비·차단 해제로 표시하지 않는다. 이 계획은 조기 push 또는 새 branch 게시 기능을 추가하지 않으며, 최종 PR/게시의 기존 검증·Review·원격 lease 조건을 유지한다.
- rebase로 수용한 현재 결과만 선택 branch·worktree의 기준선에 반영하고 Agent 원래 snapshot·session·허용 변경과 과거 감사를 보존한다. 수동 해결을 Manager 실행 또는 Agent 격리 증거로 꾸미지 않는다. 다른 Manager Git 작업, 실제 Agent 범위 위반, 사용자 결정·Review 실패 및 다른 계획·planning 차단은 별도 기준으로 유지한다.

## Implementation

- `execution_handlers.py`의 `_record_rebase_intent`, `_begin_rebase_continuation`, `_rebase_journal_checks`, `_complete_rebase_intent`, `_owned_detached_rebase`, `_owned_completed_rebase`, `_owned_persisted_rebase`와 `recover`가 공통 판정을 소비하게 한다. 현재 scope 검사와 Worker 결과 protocol은 유지하면서 replay·교정 turn·승인 stage의 역사 검증을 필수 gate에서 제거한다.
- `loop_manager.py`의 `_finish_owned_direct_rebase`, `_direct_rebase_history_proven`, `_complete_direct_rebase_checkpoint`, `resume_pr_conflict_rebase`도 같은 서비스에 위임한다. `manager_git_journal.py`의 완료·재조회 및 `checkpoint_ownership.py`의 소유권 호출에 남은 receipt·range-diff·patch 개수·session 일치 요구가 현재 rebase 수용을 가로막지 않게 한다. rebase 이외의 fetch·worktree·commit·push 증거 검사는 축소하지 않는다.
- 현재 상태 조회·판정·저장 orchestration은 공통 서비스로 묶고 handler와 직접 경로에는 adapter만 둔다. `managed_rebase_evidence.py`의 과거 형식 reader와 감사 자료는 보존하되 신규 완료를 위해 원본별 재생을 재구성하지 않는다.
- `execution_revision.py`, `runtime_state.py`와 기존 Worker/validation 경계에 최종 후보·입력에 결합된 재확인과 검증 무효화를 연결한다. rebase 완료를 곧바로 VERIFYING으로 보내 Worker의 현재 후보 확인을 생략하는 경로를 없앤다. 동일 후보에서 재확인이 완료되면 재호출을 반복하지 않는다.
- R-048의 일반 rebase·게시 ownership·재검증 장기 규칙을 같은 구현 PR의 `docs/PRODUCT.md`에 반영한다. ARCHITECTURE, Requirement와 AGENTS.md는 수정하지 않는다.

## Validation

- Worker는 `tests/test_execution_handlers.py`, `tests/test_execution_checkpoint.py`, `tests/test_managed_mutation_evidence.py`, `tests/test_manager_git_journal.py`, `tests/test_execution_revision.py`에서 직접 바뀐 판정·phase·ownership·후보 무효화 unit case만 실행한다. 실제 Git integration case는 작성하고 Manager 전체 verify에 맡긴다.
- 과거 증거 없는 자동·수동 해결과 변경된 commit 내용·개수·순서, journal 없는 진행/완료, 완료 뒤 원격 base 재전진 및 PR 유무를 같은 unit 판정으로 다룬다. 실제 scope 위반·잘못된 metadata·원격 경쟁은 계속 거부한다. 구 증거 gate를 기대하던 테스트는 새 승인 정책의 수용/거부 경계로 갱신하며 검증·Review 자체를 삭제하지 않는다.
- `tests/test_managed_rebase_lifecycle.py` 등 실제 Git/SQLite lifecycle에서 여러 충돌·같은 파일의 후속 변경·수동 stage/continue/완료, Git 완료 후 저장 전 중단과 재시작을 검증한다. 완료가 한 번만 반영되고 현재 후보 Worker 확인·targeted test 결과·Manager verify·Review 뒤에만 최종 게시 단계에 도달해야 한다.
- Manager 전체 verify는 기본 checkout과 다른 계획 보존, 기존 PR의 정확한 lease·중복 PR 방지, checkpoint ownership, 실제 flags·범위 밖 변경 거부 및 최종 후보 변경 후 재검증을 확인한다.

## Progress

- [x] handler·직접 경로의 공통 현재 상태 채택 구현 및 관련 unit 검증
- [x] checkpoint 소유권·중단 복구와 감사 보존 구현 및 관련 unit 검증
- [x] 최종 후보 Worker 확인·검증 무효화 구현과 unit 검증, 실제 Git 회귀 사례 작성
- [ ] Loop Manager 전체 verify·실제 Git lifecycle 실행, Reviewer 검토와 사용자 Merge 확인

- 2026-09-30 두 번째 Manager verify 피드백 반영: 완료 후 남은 auxiliary Git 파일을 활성 operation과 구분해 최신 base 재전진의 시작·변경 범위 관측에 같은 기준을 적용하고, stage 재개 fixture를 현재 상태 service에 연결했다. 관련 unit case 13개가 최종 오류 없는 실행에서 통과했다. 새 Manager 전체 verify·Reviewer 검증 대기.

## Decision Log

- 일반 구현과 PR 충돌 복구까지 적용하는 R-048의 전역 정책이다. legacy 오탐 차단만 고쳤던 G-101을 다시 계획하지 않는다. 저장된 최종 차단을 원인별로 해제하는 추가 연결은 G-110이 담당한다.
- G-104의 중간 게시 순서와 G-106·G-107의 차단 중 원격 갱신은 기존 계획에 남긴다. 이 계획의 수용 결과를 그 계획들이 사용하며 과거 replay 증거를 게시 gate로 재도입하지 않는다.

- 2026-09-30 Worker: `managed_rebase_adoption.py`에 공통 관측·수용·재확인 orchestration을, `managed_rebase_runtime.py`에 현재 identity·입력·scope·archive/guidance·원격/PR adapter를 분리했다. handler와 직접 복구가 같은 판정과 transactional recheck/CAS를 사용한다.
- 현재 원래 HEAD의 시작 대기, 소유 detached 충돌과 autostash 복원 대기, 최종 clean index 결과를 구분한다. 수동 완료나 구형 완료 marker는 과거 turn·stage·receipt·replay를 요구하지 않고 한 번만 수용한다. staged-only path는 반복 stage하지 않고 허용된 unstaged/untracked 해결 path만 stage한다.
- 현재 결과 수용은 WORKING/IMPLEMENTING으로 돌아가 이전 Worker·verify·Review·publication 결과와 token을 무효화한다. Worker 확인 token은 현재 입력과 HEAD에 결합하며 확인한 Worker patch의 Manager commit에만 넘긴다. 전체 verify가 만든 새 commit은 Worker 확인을 다시 요구한다.
- 원래 Agent snapshot·session과 실제 Agent 위반 검증은 보존한다. 수용한 선택 branch/worktree를 메모리 비교 기준에만 반영하고 재개한 실제 Agent의 시작 관측은 별도 comparison baseline으로 저장한다. 수동 진행을 Agent 실행 또는 Manager replay receipt로 기록하지 않는다. 이전 scope·rebase 작업 감사도 보존한다.
- 구형 historical reader와 다른 Manager Git 작업의 ownership/receipt 경계는 유지했다. 실제 Git 회귀 사례는 작성했으며 Worker는 실행하지 않았다. PRODUCT에 R-048 장기 규칙을 반영하고 양쪽 verify script에 새 모듈을 등록했다. AGENTS·ARCHITECTURE·PLANS·Requirement·archive를 수정하지 않았고 commit/push/PR을 만들지 않았다.

- 2026-09-30 Manager verify 피드백 반영: rebase 채택이 PLAN_BLOCKED 원인을 해제하지 않도록 하고, 깨끗한 결과 채택과 원래 staged 작업 복원을 분리한다. 현재 수용 HEAD의 비교 기준선과 이후 일반 Manager Git 증거 검사를 구분한다. 기존 차단 해제 정책은 유지한다.

## Worker Validation Results

- 최소 범위로 선택한 관련 unit case 62개가 각각 최종 오류 없는 실행에서 통과했다. 아래 실패 이력은 혼합 성공 실행을 통과로 계산하지 않고 수정 후 **동일 명령**의 최종 성공만 기록한다.
- 메모리 SQLite와 fake Git 관측으로 현재 수용·범위/metadata/index flags·원격 lease·autostash·중단 복구/CAS rollback·Worker token·원래 Agent snapshot 보존을 검증했다. 실제 Git lifecycle·전체 suite·integration/E2E·전체 lint/build/type check·scripts/verify는 Worker 제한에 따라 실행하지 않았다.
- `git diff --check` 통과. 전체 verify와 Reviewer 결과가 아직 없으므로 ExecPlan 완료나 Merge를 선언하지 않는다.

### 수정 후 동일 명령 재실행 이력

- 수정: unit fixture의 원격 branch 기대값을 실제 선택 branch로 수정.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAdoptionTests tests.test_execution_revision.RebaseCandidateRevisionTests tests.test_manager_git_journal.CurrentRebaseAuditTests
RERUN: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAdoptionTests tests.test_execution_revision.RebaseCandidateRevisionTests tests.test_manager_git_journal.CurrentRebaseAuditTests
FINAL_RESULT: PASS (10 tests, failures=0, errors=0)
```

- 수정: adapter fixture의 Git 응답을 명령별로 분리해 unmerged index 오인 수정.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests tests.test_execution_checkpoint.CurrentRebaseBoundaryTests
RERUN: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests tests.test_execution_checkpoint.CurrentRebaseBoundaryTests
FINAL_RESULT: PASS (4 tests, failures=0, errors=0)
```

- 수정: 잘못 지정한 selector를 실제 갱신된 수용 의미의 test 이름으로 정리.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_sealed_rebase_completion_updates_runtime_without_repeating_git tests.test_execution_handlers.ExecutionHandlerTests.test_autostash_completion_reports_rejected_operation_marker tests.test_execution_handlers.ExecutionHandlerTests.test_completed_published_rebase_keeps_remote_lease_only_with_sealed_owner tests.test_execution_handlers.ExecutionHandlerTests.test_published_rebase_completion_accepts_current_ref_without_remote_ancestry tests.test_execution_handlers.ExecutionHandlerTests.test_legacy_published_rebase_completion_accepts_current_ref_without_remote_ancestry tests.test_execution_handlers.ExecutionHandlerTests.test_autostash_published_completion_keeps_start_session_after_worker_replacement
RERUN: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_sealed_rebase_completion_updates_runtime_without_repeating_git tests.test_execution_handlers.ExecutionHandlerTests.test_autostash_completion_reports_rejected_operation_marker tests.test_execution_handlers.ExecutionHandlerTests.test_completed_published_rebase_keeps_remote_lease_only_with_sealed_owner tests.test_execution_handlers.ExecutionHandlerTests.test_published_rebase_completion_accepts_current_ref_without_remote_ancestry tests.test_execution_handlers.ExecutionHandlerTests.test_legacy_published_rebase_completion_accepts_current_ref_without_remote_ancestry tests.test_execution_handlers.ExecutionHandlerTests.test_autostash_published_completion_keeps_start_session_after_worker_replacement
FINAL_RESULT: PASS (6 tests, failures=0, errors=0)
```

- 수정: journal mock을 실제 파일 감사 fixture에서 해제하고 호환 인자 및 새 runtime port의 최신 상태 assertion 수정; 중간 재실행도 실패하여 같은 명령으로 최종 재실행.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_managed_mutation_evidence.ManagedTransitionTests.test_direct_rebase_result_recovers_after_git_before_journal_result tests.test_managed_mutation_evidence.ManagedTransitionTests.test_direct_rebase_completion_rechecks_pre_head_marker_after_head_record tests.test_managed_mutation_evidence.ManagedTransitionTests.test_direct_rebase_completion_accepts_only_original_restored_stage tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_rebase_start_retry_reuses_frozen_run_and_generation tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_versioned_continuation_binds_source_turn_and_stage_before_git tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_skip_checks_approved_index_before_recording_intent tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_continuation_intent_carries_only_matching_saved_stage_files tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_continuation_checks_saved_stage_commit_instead_of_final_file tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_legacy_skip_without_source_receipt_is_rejected tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_published_conflict_reaches_owned_gate_after_restart tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_completed_published_rebase_requires_exact_saved_result
RERUN: python3 -B -m unittest tests.test_managed_mutation_evidence.ManagedTransitionTests.test_direct_rebase_result_recovers_after_git_before_journal_result tests.test_managed_mutation_evidence.ManagedTransitionTests.test_direct_rebase_completion_rechecks_pre_head_marker_after_head_record tests.test_managed_mutation_evidence.ManagedTransitionTests.test_direct_rebase_completion_accepts_only_original_restored_stage tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_rebase_start_retry_reuses_frozen_run_and_generation tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_versioned_continuation_binds_source_turn_and_stage_before_git tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_skip_checks_approved_index_before_recording_intent tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_continuation_intent_carries_only_matching_saved_stage_files tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_continuation_checks_saved_stage_commit_instead_of_final_file tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_legacy_skip_without_source_receipt_is_rejected tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_published_conflict_reaches_owned_gate_after_restart tests.test_managed_mutation_evidence.DetachedRebaseOwnershipTests.test_completed_published_rebase_requires_exact_saved_result
FINAL_RESULT: PASS (11 tests, failures=0, errors=0)
```

- 수정: 현재 해결 내용을 stage하는 분기에서 git diff에 잡히지 않는 허용된 untracked 파일도 확인하도록 구현 수정.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_latest_base_uses_same_intent_and_rebase_for_both_checkpoints tests.test_execution_handlers.ExecutionHandlerTests.test_resolved_rebase_then_verify_failure_returns_to_same_worker tests.test_execution_handlers.ExecutionHandlerTests.test_staged_rebase_resume_reuses_saved_worker_result tests.test_execution_handlers.ExecutionHandlerTests.test_staged_autostash_resume_reuses_saved_worker_result tests.test_execution_handlers.ExecutionHandlerTests.test_completed_stage_matching_head_resumes_without_restaging tests.test_execution_handlers.ExecutionHandlerTests.test_completed_autostash_stage_matching_head_resumes_without_rebase tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_resumes_accepted_unstaged_changes tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_resumes_accepted_untracked_files tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_with_unmerged_index_retries_only_accepted_paths tests.test_execution_handlers.ExecutionHandlerTests.test_stage_lock_resume_uses_checkpoint_without_new_worker_turn tests.test_execution_handlers.ExecutionHandlerTests.test_continue_lock_resume_does_not_repeat_stage tests.test_execution_handlers.ExecutionHandlerTests.test_revision_refresh_checkpoints_dirty_verified_worker_patch_before_rebase tests.test_execution_handlers.ExecutionHandlerTests.test_rebase_discards_prior_candidate_evidence_before_verify
RERUN: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_latest_base_uses_same_intent_and_rebase_for_both_checkpoints tests.test_execution_handlers.ExecutionHandlerTests.test_resolved_rebase_then_verify_failure_returns_to_same_worker tests.test_execution_handlers.ExecutionHandlerTests.test_staged_rebase_resume_reuses_saved_worker_result tests.test_execution_handlers.ExecutionHandlerTests.test_staged_autostash_resume_reuses_saved_worker_result tests.test_execution_handlers.ExecutionHandlerTests.test_completed_stage_matching_head_resumes_without_restaging tests.test_execution_handlers.ExecutionHandlerTests.test_completed_autostash_stage_matching_head_resumes_without_rebase tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_resumes_accepted_unstaged_changes tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_resumes_accepted_untracked_files tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_with_unmerged_index_retries_only_accepted_paths tests.test_execution_handlers.ExecutionHandlerTests.test_stage_lock_resume_uses_checkpoint_without_new_worker_turn tests.test_execution_handlers.ExecutionHandlerTests.test_continue_lock_resume_does_not_repeat_stage tests.test_execution_handlers.ExecutionHandlerTests.test_revision_refresh_checkpoints_dirty_verified_worker_patch_before_rebase tests.test_execution_handlers.ExecutionHandlerTests.test_rebase_discards_prior_candidate_evidence_before_verify
FINAL_RESULT: PASS (13 tests, failures=0, errors=0)
```

- 수정: 새 fixture의 필수 operation 인자 및 runtime status/SQLite projection 기대값을 IMPLEMENTING/WORKING으로 수정; 중간 재실행도 실패하여 같은 명령으로 최종 재실행.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAdoptionTests tests.test_runtime_state.CurrentRebaseRuntimeTests tests.test_loop_manager.AgentScopeValidatorTest.test_planner_import_bytecode_is_ignored_without_hiding_scope_violations
RERUN: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAdoptionTests tests.test_runtime_state.CurrentRebaseRuntimeTests tests.test_loop_manager.AgentScopeValidatorTest.test_planner_import_bytecode_is_ignored_without_hiding_scope_violations
FINAL_RESULT: PASS (16 tests, failures=0, errors=0)
```

- 수정: 다음 rebase 기록 시 기존 snapshot의 nested list alias를 독립 복사해 이전 감사를 변경하지 않도록 구현 수정.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAgentScopeTests tests.test_execution_handlers.CurrentRebaseAdoptionTests.test_new_rebase_preserves_nested_prior_operation_audit_without_cycles
RERUN: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAgentScopeTests tests.test_execution_handlers.CurrentRebaseAdoptionTests.test_new_rebase_preserves_nested_prior_operation_audit_without_cycles
FINAL_RESULT: PASS (3 tests, failures=0, errors=0)
```

### 추가 최소 범위 unit 검증

- `tests.test_execution_handlers.CurrentRebaseAdoptionTests`의 현재 수용·원격 경쟁·stage·현재 Worker 확인·Manager commit·발행 직전 재베이스·이전 감사·legacy 완료 경계: 최종 13 case 모두 통과한 실행 결과 보유.
- `tests.test_execution_handlers.CurrentRebaseAgentScopeTests`: 2 case 통과.
- `tests.test_runtime_state.CurrentRebaseRuntimeTests`: 5 case 통과.
- `tests.test_execution_revision.RebaseCandidateRevisionTests`, `tests.test_manager_git_journal.CurrentRebaseAuditTests`, `tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests`, `tests.test_execution_checkpoint.CurrentRebaseBoundaryTests`: 각각 2 case, 총 8 case 통과.
- `tests.test_loop_manager.AgentScopeValidatorTest.test_planner_import_bytecode_is_ignored_without_hiding_scope_violations`: 1 case 통과.
- 위 실패 이력의 기존 handler 6 case·direct/continuation 11 case·phase 13 case와 `test_owned_pr_correction_stages_only_publishable_resolution`, `test_saved_worker_result_finishes_after_restart_without_second_agent_turn`, `test_preparation_resumes_durable_rebase_continue_without_worker`: 총 33 기존 case 통과.

최종 Worker 상태: 구현과 targeted unit 검증 준비 완료, Loop Manager의 전체 verify·실제 Git integration 실행 및 Reviewer 검토 대기.

## Manager Verify Feedback Revalidation (2026-09-30)

- Manager feedback `verify:aaf6a2b5a09c4ece70479ec68b2ce978e4de1afda1b4e9bd8d91872a6fcdf67e`, occurrence=1, revision=1의 전체 verify 결과는 failures=14, errors=57이었다. Worker는 저장된 진단을 읽고 아래 수정과 최소 범위 unit 검증을 수행했으며 전체 verify를 재실행하지 않았다.
- lifecycle adapter의 잘못된 변수 binding을 수정하고, projected/unprojected PLAN_BLOCKED 상태를 유지하며 후보·검증 결과를 무효화한다. 깨끗한 rebase 수용 뒤 원래 stage를 복원하고 HEAD marker를 기록하며, 복원 뒤 중단도 같은 현재 소유권과 보존 작업 검사로 재개한다.
- 새 Worker scope의 원래 staged index를 유지하고, rebase로 채택한 HEAD 이후의 별도 Manager Git 변경을 수용 기준선으로 흡수하지 않는다. 현재 PR 조회와 별도 최종 Worker 확인에 맞춰 기존 Git lifecycle fixture를 갱신했다. 수동 완료에 가짜 Manager 실행 결과를 요구하지 않는다.
- 현재 수정과 관련된 서로 다른 unit case **21개**가 최종 오류 없는 실행에서 통과했다. 최초 adapter selector 실행 이후 추가된 두 case는 개별 검증 후 같은 selector의 최종 재실행(8 case)에도 포함됐다. 아래 개별 1-case 결과는 총 21개에 중복 포함하지 않는다.
- 실제 Git integration/lifecycle은 작성·갱신만 했고 사용자 제한에 따라 실행하지 않았다. 전체 suite, integration/E2E, 전체 lint/build/type check, scripts/verify 실행, commit/push/PR 생성은 하지 않았다. AGENTS.md·docs/PLANS.md·ARCHITECTURE·Requirement·archive는 변경하지 않았다. 새 Manager 전체 verify와 Reviewer 결과 대기 상태다.

### Feedback targeted unit 실패와 동일 명령 재실행

- 수정: project projection이 없는 차단의 기존 phase를 보존하는 runtime 전이로 수정했다.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests tests.test_execution_checkpoint.CheckpointAssessmentTests.test_pr_rebase_resume_retries_unsealed_active_intent_after_lock
RERUN: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests tests.test_execution_checkpoint.CheckpointAssessmentTests.test_pr_rebase_resume_retries_unsealed_active_intent_after_lock
FINAL_RESULT: PASS (8 tests, failures=0, errors=0)
```

- 수정: checkpoint lease fixture의 선택 ExecPlan 경로와 PR repository identity를 실제 값에 맞췄다. 동일 명령의 중간 재실행도 실패했고 최종 재실행만 통과로 계산했다.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_completed_stage_accepts_replaced_clean_resolution_before_skip tests.test_execution_handlers.ExecutionHandlerTests.test_completed_autostash_stage_accepts_replaced_clean_resolution tests.test_execution_handlers.ExecutionHandlerTests.test_staged_rebase_resume_accepts_allowed_added_files tests.test_execution_handlers.ExecutionHandlerTests.test_confirmed_checkpoint_push_remains_owned_during_pr_creation tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_revision_priority_change_allows_old_workspace_metadata tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_profile_complexity_change_resumes_same_worker tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_profile_risk_change_resumes_same_worker tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_profile_downgrade_preserves_escalated_level tests.test_scoped_blocking.ScopedBlockingTest.test_project_publication_block_recovers_only_matching_owned_rebase
RERUN: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_completed_stage_accepts_replaced_clean_resolution_before_skip tests.test_execution_handlers.ExecutionHandlerTests.test_completed_autostash_stage_accepts_replaced_clean_resolution tests.test_execution_handlers.ExecutionHandlerTests.test_staged_rebase_resume_accepts_allowed_added_files tests.test_execution_handlers.ExecutionHandlerTests.test_confirmed_checkpoint_push_remains_owned_during_pr_creation tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_revision_priority_change_allows_old_workspace_metadata tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_profile_complexity_change_resumes_same_worker tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_profile_risk_change_resumes_same_worker tests.test_execution_handlers.ExecutionHandlerTests.test_conflict_profile_downgrade_preserves_escalated_level tests.test_scoped_blocking.ScopedBlockingTest.test_project_publication_block_recovers_only_matching_owned_rebase
FINAL_RESULT: PASS (9 tests, failures=0, errors=0)
```

- 수정: stage 복원 후 marker 기록이 generation을 증가시키므로, 같은 계획·worktree·HEAD·target·원격의 유효한 현재 수용 기록과 원래 stage를 함께 재검사하도록 수정했다. unmerged index·보호 문서 변경·원격 경쟁은 계속 거부한다.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_interrupted_blocked_rebase_adopts_clean_index_before_restoring_stage
RERUN: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_interrupted_blocked_rebase_adopts_clean_index_before_restoring_stage
FINAL_RESULT: PASS (1 tests, failures=0, errors=0)
```

- 수정: 별도 Manager HEAD 변경 검사에 필요한 fake git_bytes 응답을 추가했다. 현재 수용 HEAD만 비교 기준에 반영하고 이후 Manager Git 변경은 독립 증거 검사를 유지한다.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAgentScopeTests
RERUN: python3 -B -m unittest tests.test_execution_handlers.CurrentRebaseAgentScopeTests
FINAL_RESULT: PASS (4 tests, failures=0, errors=0)
```

- 수정: 많은 독립 mock context를 ExitStack으로 바꿔 Python의 정적 block 수 제한을 해결했다.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_blocked_release_records_head_after_adoption_and_stage_restore
RERUN: python3 -B -m unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_blocked_release_records_head_after_adoption_and_stage_restore
FINAL_RESULT: PASS (1 tests, failures=0, errors=0)
```


## Manager Verify Delta Follow-up (2026-09-30)

- Manager feedback `verify:ca95e9e07bbe3d100ef84faeb2f892c2cf68b5531a9c9af6ece6c835bb769ef7`, occurrence=1, revision=1은 errors=2였다. 저장된 전체 진단을 읽고 두 경계만 수정했다. 전체 verify를 Worker가 재실행하지 않았다.
- stage 재개 helper가 production current service를 제공하지 않아 historical stage-evidence fallback으로 들어가던 fixture를 수정했다. 현재 허용된 unstaged/untracked path를 stage하고 staged-only path는 유지하며, 새 허용 파일이 과거 stage 감사에 없다는 이유로 거부하지 않는 기대값을 적용했다.
- Review 중 base 재전진 뒤 새 rebase의 START와 common-ancestor diff 관측에서 `REBASE_HEAD`·`MERGE_MSG`·`MERGE_MODE`의 잔존을 활성 operation으로 취급하지 않도록 기존 controlling-marker 정책을 재사용한다. 파일을 삭제하거나 실행 증거를 만들지 않는다. 실제 활성 merge/cherry-pick/revert/rebase/sequencer/bisect, 미해결·staged index, HEAD·scope·원격 불일치는 계속 거부한다.
- fake Git 관측의 새 회귀 case로 START의 auxiliary/controlling marker 구분과 새 base의 보호 문서 변경을 선택 branch 변경으로 오인하지 않는 common-ancestor 범위를 검증했다. 실제 Git integration 실패 case는 사용자 제한에 따라 실행하지 않았으며 새 Manager verify 결과 대기다.

### Delta targeted unit 실패와 동일 명령 재실행

- 새 회귀 case의 최초 실행은 failures=5(두 case의 subtest 포함)였다. 시작 판정과 관측에 controlling-marker 기준을 적용한 뒤 같은 명령을 재실행했다. 최초 혼합 실행을 통과로 계산하지 않는다.

```text
FAILED_COMMAND: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_accepts_current_allowed_changes_before_staging tests.test_managed_rebase_validation.CurrentRebasePolicyTests.test_new_rebase_start_retains_auxiliary_files_but_rejects_active_operations tests.test_managed_rebase_validation.CurrentRebaseObservationTests.test_new_base_observation_uses_common_ancestor_with_retained_auxiliary_files
RERUN: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_accepts_current_allowed_changes_before_staging tests.test_managed_rebase_validation.CurrentRebasePolicyTests.test_new_rebase_start_retains_auxiliary_files_but_rejects_active_operations tests.test_managed_rebase_validation.CurrentRebaseObservationTests.test_new_base_observation_uses_common_ancestor_with_retained_auxiliary_files
FINAL_RESULT: PASS (3 tests, failures=0, errors=0)
```

- 변경한 stage helper의 나머지 9개 case와 실제 snapshot parser를 사용하는 detached 관측 1개 case를 최소 selector로 추가 검증했다.

```text
COMMAND: python3 -B -m unittest tests.test_execution_handlers.ExecutionHandlerTests.test_staged_rebase_resume_reuses_saved_worker_result tests.test_execution_handlers.ExecutionHandlerTests.test_staged_autostash_resume_reuses_saved_worker_result tests.test_execution_handlers.ExecutionHandlerTests.test_completed_stage_matching_head_resumes_without_restaging tests.test_execution_handlers.ExecutionHandlerTests.test_completed_autostash_stage_matching_head_resumes_without_rebase tests.test_execution_handlers.ExecutionHandlerTests.test_completed_stage_accepts_replaced_clean_resolution_before_skip tests.test_execution_handlers.ExecutionHandlerTests.test_completed_autostash_stage_accepts_replaced_clean_resolution tests.test_execution_handlers.ExecutionHandlerTests.test_staged_rebase_resume_accepts_allowed_added_files tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_resumes_accepted_unstaged_changes tests.test_execution_handlers.ExecutionHandlerTests.test_pending_stage_resumes_accepted_untracked_files tests.test_managed_rebase_validation.CurrentRebaseObservationTests.test_adapter_preserves_actual_detached_snapshot_and_waits_for_resolution
FINAL_RESULT: PASS (10 tests, failures=0, errors=0)
```

- 서로 다른 관련 unit case 총 13개 통과. 전체 suite, integration/E2E, 전체 lint/build/type check, scripts/verify 실행과 commit/push/PR 생성은 하지 않았다. AGENTS.md·docs/PLANS.md·PRODUCT·ARCHITECTURE·Requirement·archive를 변경하지 않았다. Worker 준비 완료이며 전체 verify와 Reviewer 결과를 기다린다.
