---
id: G-322
title: 예약 workspace의 변경 이력 검증과 보존 복구
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-022] Ignored-Only Managed Worktree Tolerance.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-093] 원격 작업 브랜치로 새 환경에서 이어하기.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

R-055 W01–W03·W06의 예약 workspace를 승인된 생성·후속 변경 근거와 현재 자원에 연결해 보존 복구한다. 일반 workspace 준비와 기존 프로젝트 차단 재평가가 같은 소유권 판정을 사용하고, writer 종료·복구 소유권·확정 직전 경쟁을 확인하게 한다. 차단 범위의 분리·축소는 G-323이 이 결과를 소비한다.

현재 `git/workspace.py:reserved_worktree_creation_head()`는 봉인된 생성 결과와 현재 등록·HEAD·index의 완전 일치를 요구한다. `validate_reserved_worktree()`는 dirty·operation을 거부하고 `recovery/scope_reconciliation.py`의 예약 복구도 미완료 checkpoint operation과 dirty를 일괄 거부한다. 생성 HEAD와 `active_sha`의 구분, 깨끗한 예약의 복구·DB rollback·checkpoint 경쟁은 직접 구현돼 있다. 남은 범위는 생성 후 승인된 변경으로 달라진 자원과 작업물을 설명하는 근거, writer/복구 소유권 및 일반 준비 경로의 최종 자원 재확인이다.

## Acceptance Criteria

- R-055 W01–W02: 생성 HEAD와 실행·검증 기준 `active_sha`를 독립적으로 유지한다. 생성 근거에서 현재 상태까지 승인된 변경이 설명되면 기존 예약을 한 번 확정하고 HEAD·commit·staged/unstaged/untracked 파일·session·실패 감사를 보존한다. 최신 base 포함·실행 기준과 HEAD의 일치·깨끗한 작업 공간을 소유권 복구의 일괄 선행 조건으로 요구하지 않는다. `active_sha`를 현재 HEAD로 바꾸어 검사를 통과시키지 않는다.
- R-055 W03: 생성 journal의 소유자·repository·작업 ID·canonical 경로·branch·생성 HEAD, 예약과 checkpoint, 현재 Git 등록·HEAD·index·파일·operation을 함께 대조한다. HEAD 전진에는 승인된 변경 연결이 필요하고 미커밋 자료에도 확인 가능한 호출·checkpoint 등 실제 변경 근거를 적용한다. 경로/branch 이름·현재/원격 HEAD의 일치나 예약 값만으로 승인하지 않는다. 불명확한 변경·누락·손상·다른 소유자는 자료와 원래 근거를 보존하고 확정하지 않는다.
- R-091: 이전 writer의 실제 종료 또는 미시작과 해당 작업·자원의 실행/복구 소유권을 확인한 뒤 관측·확정한다. 같은 대상의 동시 복구·Agent/verify·수동 소유·게시 경합을 우회하지 않으며, 다른 작업의 정상 ref/worktree 변화는 이 예약의 거부 근거가 아니다. 호출 결과·session 존재만으로 writer 종료를 추정하지 않는다.
- R-093: 기존 DB의 예약 복구를 DB 없는 새 환경의 신규 인수로 바꾸지 않는다. 저장된 원격 인수 근거가 있으면 기존 version·repository·작업·base·branch·시작 HEAD·PR 연결과 인수 자격을 검증하고 이후 승인된 전이와 구분한다. 생성 journal로 잘못된 인수 근거를 대체하거나 과거 verify/Review 성공을 추정하지 않는다.
- R-055/R-022: 소유 Manager operation은 저장된 target·intent·파일 보존 조건으로 기존 복구 경계에 연결한다. 사용자 operation은 자동 완료·abort하지 않는다. 복구를 위해 중복 생성·삭제·reset·강제 checkout·작업물 일괄 복원을 하지 않으며, ignored-only 변화는 기존 시작 ignore 근거와 경로 충돌 규칙을 따른다. 최신 base 반영과 필요한 rebase·후보 검증은 소유권 복구 뒤 정상 흐름에서 판단한다.
- 검증 이후 자원·writer/복구 소유·작업·checkpoint가 바뀌면 낡은 결과를 적용하지 않는다. 외부 Git/프로세스 관측은 DB 쓰기 처리 밖에서 수행하고 짧은 기존 transaction/CAS로 확정한다. DB 실패·확정 직전 중단·재시작은 원래 예약과 경쟁 변경·감사를 보존하며 완료한 Git 효과를 반복하지 않는다. 복구만으로 Worker·verify·Review·rebase·게시 성공이나 실패 초기화를 만들지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/git/workspace.py`의 생성 근거 조회·예약 검증과 `runtime/bootstrap_adapter.py:_managed_worktree_transition_state()`의 등록/index 관측을 재사용한다. 생성 identity와 현재 변경의 설명 가능성을 구분하는 판정을 두 준비/복구 소비자가 공유하게 한다. `git/manager_git_journal.py:verified_managed_transitions()`의 봉인된 kind/owner/target 검증과 기존 managed rebase·checkpoint commit 근거를 사용하고 범용 journal·새 상태 원본을 만들지 않는다. 과거 Git snapshot을 Agent 접근 권한 gate로 복원하지 않는다.
- [x] 확인한 `execution/execution_workspace_handler.py:WorkspaceHandler.prepare()`와 `recovery/scope_reconciliation.py:resume_project_block_if_safe()`에 후속 변경·operation·원격 인수 근거와 최종 자원 재확인을 연결한다. `storage/execution_checkpoint_state.py`의 generation 및 `catalog/project_selection.py:mark_execplan_workspace_owned()`의 중첩 transaction을 유지한다. 기존 `remote_adoption`·commit/rebase·복구 감사는 새 근거 기록으로 덮어쓰지 않는다.
- [x] 확인한 `application/task_scheduling.py`의 작업 lease, `storage/task_calls.py:execution_confirmed()`와 `runtime/resource_lock.py`를 재사용해 호출자와 비동기 복구의 writer/자원 소유 조건을 확인한다. 이미 보유한 동일 소유 경계를 이어 쓰고 잠금 탈취·자기 대기·종료 불명 예약 회수를 하지 않는다. 필요한 effects는 기존 `recovery/bindings.py`·`recovery/recovery_adapter.py`의 명시적 조립으로 전달한다.
- [x] 아래 기존 fixture의 정상/거부·경쟁·중단 사례를 확장한다. PRODUCT의 차단 해제·작업 공간 보존과 ARCHITECTURE의 Git/worktree·상태 복구 절에는 실제 구현한 예약 근거·현재 상태·재개 경계를 통합한다. 차단 scope 전환·알림 정책과 배정기 자체는 이 작업에서 구현하지 않는다.

## Validation

- Worker는 `tests/test_execution_workspace_phase.py:ExecutionWorkspacePhaseTests`, `tests/test_scoped_blocking.py:ReservedWorkspaceRecoveryTests`, `tests/test_remote_work_adoption.py:RemoteCreationEvidenceTests`에서 변경한 최소 unit case만 실행한다. 실제 판정·SQLite/checkpoint·파일을 검사하고 Git·프로세스 관측만 double로 제공한다.
- 직접 추가된 생성 HEAD≠`active_sha`, 생성 증거 누락/등록 변경, checkpoint 경쟁·DB rollback·인수 identity 불일치 회귀는 재사용한다. 승인된 후속 commit/rebase·기록된 staged/unstaged/untracked 작업물의 보존 복구와, 같은 HEAD여도 파일 근거가 다르거나 후속 전이를 설명할 수 없는 거부를 별도로 확인한다. dirty 전부 허용으로 기존 외부 변경 거부 assertion을 약화하지 않는다.
- live/종료 불명 writer·다른 복구 소유자·수동/게시 충돌, 관측 뒤 자원/operation/checkpoint 변경에서는 ownership 확정·차단 해제·파일 쓰기가 0회여야 한다. 정상 종료 근거가 있는 재시작과 확정 전/후 DB 실패에서는 한 번 확정하고 `active_sha`·session·retry·독립 실패와 기존 성공 근거를 그대로 보존한다. 필요한 후보 검증이 남는지 확인한다.
- `tests/test_workspace_reservations.py:test_execplan_workspace_reservation_survives_post_create_interruption`과 `tests/test_remote_work_adoption.py:RemoteAdoptionGitTests`의 실제 생성·등록·원격 인수 효과를 재사용한다. 기존 fixture에 후속 변경 보존 효과의 최소 사례만 연결하고 정책 조합별 Git 생성·게시를 반복하지 않는다. 실제 Git 사례는 기존 전체 검증 수집에 두며 Worker targeted 실행에 포함하지 않는다.
- `scripts/verify.json`의 unittest/protocol 1·launcher Python 3.11–3.14·독립 모듈 수집·fixture·Agent 실행 차단·필수 compile 계약을 유지한다. adapter·전체 검증 구조와 Windows WSL/native 책임을 바꾸지 않는다.

## Progress

- [x] revision `26140c614cf5881242ce8e3abbcdd060d6cd68c6`와 비교 기준 `b31e33c291b8fb550f1c5cd2f88aff13a5a43d33`의 R-055·직접 코드/회귀를 정적으로 대조했다. 깨끗한 예약 복구와 실행 기준 분리·checkpoint 경쟁/rollback은 구현된 범위로 제외했다. 구현·테스트·verify를 실행하지 않았다.
- [x] 첫 구현 단위로 일반 준비의 예약 검증 전/확정 직전 local snapshot과 대상 worktree 등록을 대조했다. HEAD·index·파일·operation·등록 변경과 불완전한 관측은 DB 소유권 확정 전에 보류하고, 다른 작업의 ref/worktree 변화는 비교에서 제외한다. `make_worktree()`도 생성 증거 검증 뒤 등록/index를 재관측한다. 기존 생성 증거 판정과 변경 거부 조건은 유지한다.
- 이 구현 단위의 targeted runner 실행은 `ExecutionWorkspacePhaseTests.test_reserved_resource_race_does_not_confirm_ownership`, `test_reserved_confirmation_ignores_other_worktree_and_ref_changes`, `test_reserved_checkpoint_race_does_not_confirm_ownership`과 `RemoteCreationEvidenceTests`의 두 case, 총 5개가 통과했다. 전체 verify와 실제 Git 통합 회귀는 실행하지 않았다. 승인된 후속 변경 이력·writer 종료/복구 소유권·공유 판정 및 제품/설계 반영은 아직 구현하지 않았으며 이 보존점은 완료 후보가 아니다.
- [x] 두 번째 구현 단위로 `plan_recovery.reserved_workspace_ownership()`을 일반 준비와 프로젝트 예약 복구에 연결했다. 대상 plan·branch·worktree lease를 유지하고, 현재 process/thread의 열린 handle로 확인되는 기존 lease만 재사용한다. 기존 call 예약의 실제 종료/미시작 판정과 Agent의 종료 evidence/receipt를 대조하며 결과·session만으로 종료를 추정하지 않는다. 대상 자원의 다른 작업·수동 묶음 및 게시 claim을 거부하고, 확정 transaction에서 호출/소유 상태를 재확인한다. 원래 호출 예약·invocation·session은 회수하거나 초기화하지 않는다.
- 이번 호출의 targeted runner는 기존 `ExecutionWorkspacePhaseTests`·`ReservedWorkspaceRecoveryTests`의 12개, 새 writer/미시작/경쟁/수동/게시 사례 10개, `TaskSchedulingTests.test_reserved_workspace_reuses_only_open_same_thread_resource_leases`와 기존 fetch 경합·planning drain 사례 2개를 실행했다. 총 25개 고유 case가 통과했으며, 변경 이후 관련 최소 case를 재실행했다. 실제 lease의 다른 복구 context·다른 thread·해제된 handle은 재사용되지 않음을 확인했다. 전체 verify와 실제 Git 통합 회귀는 실행하지 않았다. 생성 이후 승인된 commit/rebase·staged/unstaged/untracked 변경의 공유 증거 판정·소유 Manager operation 연결과 제품/설계 반영은 남아 있으므로 이 보존점도 완료 후보가 아니다.
- [x] 세 번째 구현 단위로 `git/workspace.validate_reserved_worktree_history()`를 두 소비 경로에 연결했다. 생성의 branch/lock identity를 유지하며, 실제 parent/message/tree가 맞는 기존 commit intent·Worker checkpoint 이력과 봉인된 rebase의 정확한 local 상태 전이, 실제 종료한 Worker scope의 저장된 시작/결과 snapshot으로 현재 상태의 도달 가능성을 판정한다. 생성 HEAD는 반환하되 `active_sha`·checkpoint operation·이전 성공 자료를 수정하지 않는다. 저장 scope는 원래 digest와 invocation scope/role을 대조하며, 예약 복구에 과거 Agent 접근 권한 gate를 다시 적용하지 않는다. 동일 HEAD의 설명되지 않는 파일/index 변경은 계속 거부한다.
- 이 단위에서 기존 24개 unit case 재실행은 아래 최초 오류를 교정한 뒤 통과했고, 승인된 commit→rebase→staged/unstaged/untracked 파일의 실제 bytes 보존·중간 전이 누락·파일 서명 불일치·DB rollback과 `make_worktree()`의 공통 commit 판정을 검사하는 새 2개 case도 통과했다. 첫 실행의 journal double이 kind filter를 무시해 rebase 조회에 생성 record를 돌려준 오류는 실제 필터 계약대로 교정했으며, 깨끗한 현재 index tree와 HEAD tree의 불일치 거부를 추가했다. 실제 Git fixture에는 기존 생성 자원에서 미커밋 출력의 보존 검사를 연결했으나 Worker는 이를 실행하지 않았다.

  FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests tests.test_remote_work_adoption.RemoteCreationEvidenceTests tests.test_execution_workspace_phase.ExecutionWorkspacePhaseTests`

  RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests tests.test_remote_work_adoption.RemoteCreationEvidenceTests tests.test_execution_workspace_phase.ExecutionWorkspacePhaseTests`

  재실행 결과: 24개 통과. 새 case는 별도 최소 실행 및 관련 변경 뒤 재실행에서 2개 통과했다. 전체 verify는 실행하지 않았다. 진행 중 Manager rebase·기타 operation 연결, 원격 인수 자격/PR 연결의 공유 판정과 PRODUCT·ARCHITECTURE 반영은 아직 남아 있으므로 완료 후보가 아니다.
- [x] 승인된 후속 변경·writer/복구 소유권·현재 자원의 공유 검증과 보존 재개
- [x] 최소 unit·기존 실제 Git 보존 회귀 fixture 확장 및 제품/설계 정합성 (실제 Git 실행은 Manager 전체 검증에 남김)

- [x] 최종 구현 단위에서 기존 예약의 원격 인수를 신규 인수와 분리했다. 최초 catalog grant·version/repository/작업/base/branch/원래 HEAD·fetch/push origin·PR 연결을 재확인하고, 작업 확정 transaction에서 grant와 호출/소유 claim도 다시 대조한다. 원격 PR이 관측 중 바뀌거나 grant가 손상되면 확정하지 않는다. 저장된 인수 원문·기존 expected/target·성공 근거는 보존하며 미확정 초기 인수의 원래 remote baseline만 채운다.
- [x] 저장된 rebase는 기존 현재 상태 service를 읽기 전용 관측하는 한정된 예약 경로로 연결했다. unmerged stage는 write-tree 없이 관측하며 commit·push·PR 생성·중간 게시 intent를 보존한다. 현재 정책의 봉인된 완료 receipt는 정확한 예약 자원 조회에서만 소비하고 수락 HEAD의 clean index는 실제 Git tree에서 읽는다. 사용자 rebase metadata만으로 Manager checkpoint를 재구성하지 않는다. 대상 Git lease와 journal 경계는 같은 열린 process/thread handle을 이어 써 자기 대기를 피한다. PRODUCT의 보존 보장과 ARCHITECTURE의 identity/변경 근거·operation·관측/CAS 계약을 구현에 맞춰 통합했다.
- 이번 호출에서 기존 준비·복구·원격 인수의 33개 case와 공통 생성/변경 판정·실제 lease·현재 rebase 정책/adapter·journal의 변경 관련 최소 case를 실행했다. 모두 고유 case 기준 45개이며, 변경 뒤 필요한 해당 case만 다시 실행했다. 아래 두 최초 오류는 각각 실제 GitSnapshot 계약과 잘못된 테스트 계획 경로를 교정한 뒤 동일 명령으로 통과했다. 실제 CLI·전체 verify·실제 Git fixture·Windows 회귀는 실행하지 않았다. 전체 verify와 Reviewer 검토는 Manager에 남아 있다. Worker 구현 및 targeted 검증 후보이며 PR Merge/전체 완료를 선언하지 않는다.

  FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests tests.test_execution_workspace_phase.ExecutionWorkspacePhaseTests tests.test_execution_workspace_phase.RemoteAdoptionPhaseTests`

  RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests tests.test_execution_workspace_phase.ExecutionWorkspacePhaseTests tests.test_execution_workspace_phase.RemoteAdoptionPhaseTests`

  재실행 결과: 33개 통과. 초기 인수 후 재시작 fixture의 불완전한 SimpleNamespace를 실제 등록/metadata 필드를 가진 GitSnapshot으로 교정했다. 기존 거부 assertion을 약화하지 않았다.

  FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_managed_mutation_evidence.ManagedTransitionTests.test_current_rebase_receipt_is_available_only_to_exact_reservation_history tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests.test_existing_adoption_checks_grant_pr_and_keeps_original_head tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests.test_owned_detached_rebase_recovers_reservation_without_completing_operation`

  RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_managed_mutation_evidence.ManagedTransitionTests.test_current_rebase_receipt_is_available_only_to_exact_reservation_history tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests.test_existing_adoption_checks_grant_pr_and_keeps_original_head tests.test_scoped_blocking.ReservedWorkspaceRecoveryTests.test_owned_detached_rebase_recovers_reservation_without_completing_operation`

  재실행 결과: 3개 통과. journal fixture의 plan_id와 plan_path가 서로 다른 오류를 교정했다. 이후 실제 runtime factory의 예약 관측/기존 경계, 미완료 commit·push·PR intent와 unmerged 관측, 사용자 operation 비재구성, 현대/기존 rebase 연결 및 원격 PR 경쟁은 관련 최소 case에서 통과했다. `git diff --check`도 통과했다.

- Manager 전체 verify 피드백 `verify:4b4aa95b47be19e975e0e174148c21194e3ac6e546a144bc30268c71af71154e` occurrence 1/revision 1의 실패 원문을 확인했다. 실제 Git 예약 복구 두 case의 공통 원인은 `workspace_service.make_worktree()`의 `reservation_validator` 인자/전달 누락이었다. 공용 adapter를 교정하고 기존 공통 판정 unit case가 `loop_manager.make_worktree()` 공개 조립 경로를 거치도록 변경했다. 수동 journal 경합 실패는 같은 thread의 임의 Git lease까지 재사용한 것이 원인이어서, 열린 handle의 owner도 추적하고 생성 kind·예약 복구 owner·명시적 plan owner가 맞을 때만 journal 중첩 호출을 허용하도록 제한했다. 거부 assertion이나 native 회귀를 제거·약화하지 않았다.
- 이 피드백 수정의 targeted runner 명령은 `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_remote_work_adoption.RemoteCreationEvidenceTests.test_reserved_owned_commit_uses_shared_history_without_clean_reset tests.test_task_scheduling.TaskSchedulingTests.test_journal_rejects_same_thread_lease_from_another_owner tests.test_task_scheduling.TaskSchedulingTests.test_reserved_workspace_reuses_only_open_same_thread_resource_leases`이며 3개가 통과했다. Git 관측만 double로 제공하는 공개 adapter 경로와, 실제 lease에서 다른 owner/plan의 apply 0회·정상 생성 journal 재사용을 검증했다. `git diff --check` 통과. Worker는 보고된 native Git 통합 case와 전체 verify를 재실행하지 않았고, 이 피드백의 전체 verify 해소 판정은 Manager 재검증에 남아 있다.


- Reviewer occurrence 1/revision 1의 `completed-rebase-dirty-state-lost`, `pending-commit-stage-unreachable`, `pending-rebase-start-rejected`, `terminal-pr-resume`을 교정했다. 현재 정책의 봉인된 완료 rebase는 Git에서 읽은 index와 완료 관측의 unstaged/untracked 파일 서명을 함께 연결한다. commit 직전 중단은 저장된 승인 tree와 현재 index의 일치 및 도달 가능한 출력의 파일 서명 보존으로 staging 전이를 확인한다. 생성 당시 clean 상태의 저장된 rebase도 기존 검증기의 START 판정을 소비하며 operation·원래 HEAD/target은 유지한다. 기존 원격 인수의 CLOSED/MERGED/mergedAt은 준비·프로젝트 차단 복구에서 소유권 확정 전에 기존 terminal PR 처리로 전달하며, 프로젝트 복구는 해당 작업의 명시적 실행 투영을 사용한다. 불명 PR 상태는 보류한다.
- 이번 교정의 targeted runner로 `RemoteCreationEvidenceTests.test_interrupted_staging_and_completed_rebase_keep_recorded_file_state`, `test_reserved_owned_commit_uses_shared_history_without_clean_reset`, `ReservedWorkspaceRecoveryTests.test_owned_rebase_start_recovers_unchanged_clean_creation`, `test_existing_adoption_checks_grant_pr_and_keeps_original_head`, `test_owned_commit_rebase_and_recorded_dirty_output_preserve_workspace`, `test_owned_detached_rebase_recovers_reservation_without_completing_operation`, `RemoteAdoptionPhaseTests.test_existing_reserved_adoption_routes_terminal_pr_before_workspace_recovery`, `test_existing_reserved_adoption_routes_merged_pr_to_terminal_sync`의 고유 8개 unit case가 통과했다. 변경 이후 관련 case만 재실행했다. rebase 전 Worker 출력만 있는 unstaged/untracked 보존, staging 전후 서명 불일치·index tree 불일치·근거 누락 거부, rebase START 보존, terminal/불명 PR 및 작업 투영을 확인했다. 전달된 전체 verify PASS는 이전 후보의 Manager 결과이며 이번 변경의 전체 verify·실제 Git 통합·Windows 검증은 실행하지 않았다. Manager 재검증과 Review가 남아 있다.

## Decision Log

- G-189의 기존 전수 감사와 G-297의 배정 직전 자격 검사는 이 생성→변경 이력의 예약 복구를 구현하지 않는다. 신규 작업은 R-055 W01–W03·W06의 남은 경계를 맡고, G-323은 그 판정으로 차단 범위를 정하며 G-189는 두 작업 뒤 후속 소비만 감사한다. 직접 구현에 소급 ExecPlan이나 검증 성공을 만들지 않는다.
- complexity high: 생성·후속 변경·현재 파일/operation 근거를 writer 종료·자원 lease·checkpoint CAS와 결합하고 두 소비 경로의 중단 재개를 일치시켜야 한다. risk high: 잘못 확정한 소유권은 이후 Git 쓰기의 대상·작업물 보존을 잘못 승인할 수 있다. 근거 없는 형상은 쓰기 전에 보존·거부하고 기존 자료/CAS로 재관측·복구하며 실제 Git 보존과 경쟁 회귀로 검증한다. priority high는 정상 작업의 자동 복구와 소유권 보호에 필요한 교정이다. 최초 level 5이며 Model·Effort는 Manager 설정을 따른다.
