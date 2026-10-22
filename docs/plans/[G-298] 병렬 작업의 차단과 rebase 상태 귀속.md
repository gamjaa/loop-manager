---
id: G-298
title: 병렬 작업의 차단과 rebase 상태 귀속
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

R-091의 작업별 실행 원본을 차단·rebase의 남은 소비자까지 연결한다. 프로젝트 대표 투영이 A이고 현재 작업이 B일 때 B의 차단·원격 PR head·복구 단계 판단은 B의 상태만 사용한다. 이미 구현된 `selected_execution()`·`project_row()`와 snapshot/CAS를 재사용하며 실행 저장 구조를 다시 설계하지 않는다.

현재 `recovery/block_service.py:block_active_execplan()`은 `runtime_state.project_row()`로 선택 작업을 읽고 recovery decision의 plan ID를 대조한다. `git/direct_rebase_service.py`의 새 rebase도 명시된 `exec_plans`의 PR head를 읽고 DB 대기를 전파한다. 이 직접 구현과 회귀는 재사용한다. 남은 범위는 `_complete_direct_rebase_checkpoint()`의 명시 plan/선택 context·차단 phase 귀속과 관측 뒤 원자 적용이다. 투영 없는 완료의 `service.observe()`는 이미 transaction 밖이며, 차단 helper는 외부 지문/파일 관측 뒤 처음 읽은 작업 상태가 여전히 같은지 적용 직전에 대조하는 경계가 남아 있다.

직접 반영된 `recovery/plan_recovery.py`의 대상별 lease·차단 투영 context, 비동기 차단 재평가, 대상 ref/worktree만의 rebase 지문과 `RuntimeStateRepository`·`ExecutionStatePort`의 transaction 밖 재관측은 재사용한다. 해당 기능과 기존 user-decision 의미 판단을 새로 구현하지 않으며, 함수 단독 회귀를 모든 차단·direct rebase 소비자의 충족으로 확대하지 않는다.

## Acceptance Criteria

- A를 대표 투영에 둔 채 B에서 차단하면 B의 status·원인·지문·Worker/Reviewer 결과와 복구 근거만 갱신한다. A의 단계·session·scope·실패·검증/게시 token과 프로젝트 planning 기록은 보존한다. 투영 부재에서도 명시적으로 선택한 작업을 사용하고 다른 작업을 fallback 대상으로 고르지 않는다.
- rebase 시작·완료·재개는 해당 plan의 PR head/conflict head·phase·checkpoint·현재 후보를 사용한다. A와 B의 OID가 달라도 B의 정확한 remote/lease 조건이 충족하면 A 때문에 거부하지 않으며 B 자체의 불일치·stale generation·작업 identity 변화는 계속 거부한다.
- 선택 context와 명시적 plan ID가 다르면 효과 전에 거부하고 소유 증거를 보존한다. 계획 선택이 없는 프로젝트 조율·planning 및 명시적 차단 작업 복구는 각각의 기존 책임을 유지하며 등록 origin·base·프로젝트 안전 gate는 여전히 프로젝트 원본에서 읽는다.
- 같은 후보의 writer·verify·Review·rebase를 겹치지 않고 최신 base/명세·최종 후보 재검증을 유지한다. 다른 작업의 Merge·차단·교정이 B의 성공 근거를 대신하거나 A의 실행 이력을 초기화하지 않는다. 실제 프로젝트 차단은 범위를 축소하지 않는다.
- R-091: 투영 없는 차단 작업의 direct rebase 완료에서도 Git·원격 재관측 중 DB 쓰기 잠금을 잡지 않는다. 관측 뒤 짧은 transaction에서 대상 identity·현재 투영·checkpoint generation을 재확인하고 acceptance·단계·outbox를 기존 원자 경계로 반영한다. 경쟁 변경은 덮지 않고 미확정 근거를 보존한다.

## Implementation

- [x] 확인한 `loop_manager_internal/recovery/block_service.py:block_active_execplan()`의 선택 작업 읽기·decision ID 대조를 재사용한다. 지문·Git snapshot·Reviewer 원문 관측 뒤 짧은 transaction에서 같은 작업 snapshot·소유권을 다시 확인해 낡은 차단 적용을 거부한다. 이번 delta의 `transition_validation_failure()`가 구현한 외부 관측 후 상태 비교·실패 이력/차단 원자 적용을 재사용하고 같은 관측 이동을 반복하지 않는다. `RuntimeStateRepository.block_plan()`·기존 원인별 결정·알림 경계를 유지한다.
- [x] 확인한 `loop_manager_internal/git/direct_rebase_service.py:_complete_direct_rebase_checkpoint()`의 명시 plan·선택 context와 phase/투영 판단을 해당 작업 snapshot에 연결한다. 새 rebase의 `pr_head_oid`·`pr_conflict_head_oid` 조회 교정은 재사용하고 대표 투영을 B로 바꾸어 문제를 숨기지 않는다. 프로젝트 원본의 phase를 보존할 경우와 B의 phase를 적용할 경우를 기존 전이 의미에 맞게 구분하며 checkpoint generation·정확한 원격 OID·게시 후 확인을 유지한다.
- [x] 같은 파일의 `_complete_direct_rebase_checkpoint()`에 직접 구현된 transaction 밖 관측과 대상/현재 checkpoint 비교를 소비한다. 작업 귀속 변경에서도 `storage/runtime_state.py:complete_rebase()`·`execution/execution_state.py:complete_rebase()`의 관측 후 소유권/checkpoint 비교와 원자 적용을 유지한다. 시작·대기·완료의 실제 Git 판정과 사용자 operation 보호를 유지하며 과거 관측을 새로운 완료 증거로 만들지 않는다.
- [x] `storage/runtime_state.py`의 `execution_selection`·`projected()`·`scope_owner()`·`transition_from_snapshot()`과 실행 포트 경계를 재사용한다. Review에서 `recovery/blocked_agent_service.py`의 보존·완료 경로가 선택 plan ID 대신 `projects`의 대표 Agent snapshot을 읽는 누락을 확인했다. 보존·완료와 중단 복구 재진입이 명시 plan의 선택 context에서 최신 상태를 읽고 갱신하도록 연결했다. 재진입은 `owner_plan_id`가 있는 기록만 대상 ExecPlan의 현재 recovery를 읽으며, owner 필드 없는 기존 기록은 `projects` 호환 projection을 읽고 완료·재진입에서도 같은 projection을 기준으로 갱신한다. checkpoint_adapter·merge_cleanup의 프로젝트 등록·planning/coordination 조회는 그대로 두었다.
- [x] 병렬 snapshot fixture와 기존 실제 정책/SQLite fixture에 회귀를 연결했다. 대표 A와 선택 B의 branch·Agent snapshot·복구가 다를 때 B의 BLOCKED Agent 복구가 B 상태로 완료되고 A의 대표 투영이 보존되는 사례를 추가했다. ARCHITECTURE에 snapshot CAS, 명시 target rebase 완료 및 BLOCKED Agent 복구의 대상별 상태 귀속을 반영했다. 사용자에게 보이는 차단·재개 정책은 바꾸지 않아 PRODUCT는 수정하지 않았다.

## Validation

- Worker는 위 파일의 변경 관련 최소 unit case를 실행한다. 프로젝트 투영 A·선택 B·서로 다른 branch/worktree·session·scope·PR OID·token을 두고 실제 차단 helper와 rebase handler를 호출한다. Git/process 관측만 double로 제공하며 실제 SQL·snapshot·checkpoint·원인 결정을 검사한다.
- 기존 `test_parallel_block_uses_selected_plan_and_preserves_representative`·`test_direct_rebase_uses_target_pr_head_instead_of_representative`와 `test_validation_block_git_observation_releases_database_access`·`test_validation_observation_cannot_overwrite_changed_failure_history`를 재사용한다. 미검사인 일반 차단 helper의 관측 중 B 상태/소유권 변경·Reviewer 자료와 direct-rebase 완료의 투영 없음·A 투영/선택 B·명시 plan/context 불일치만 보강한다. 낡은 차단/완료만 거부하고 A·경쟁 변경·기존 근거는 보존해야 한다.
- B의 정상 원격 일치·B 자체의 불일치, 차단 rebase 완료·현재 generation 경쟁·재시작에서 대상별 OID·phase·이력 보존과 필요한 재검증을 확인한다. 다른 후보의 verify/Review/게시 token 재사용은 거부하며 종료 불명 writer와 실제 공유 Git operation 보호는 유지한다.
- `tests/test_runtime_state.py:CurrentRebaseRuntimeTests.test_rebase_reobservation_allows_another_database_writer_before_atomic_apply`의 repository/port 회귀는 재사용하고, `tests/test_execution_current_rebase_adoption_phase.py`·`tests/test_scope_block_revalidation.py`의 기존 fixture로 실제 direct-rebase 완료 소비자에 빠진 경우만 보강한다. 투영 없음·A 투영/선택 B에서 외부 재관측 도중 별도 SQLite 연결의 쓰기가 완료되고, checkpoint/소유 identity 경쟁 시 B의 acceptance·차단 해제만 거부되며 A와 경쟁 변경·미확정 근거가 보존돼야 한다.
- 기존 단일 작업·명시적 차단 복구·대표 투영과 작업 원본 분리 회귀를 유지한다. 정책 조합마다 실제 Git lifecycle을 추가하지 않으며 기존 최소 Git 효과 회귀는 Manager 검증에 남긴다.
- BLOCKED Agent 완료 뒤 `projects`에 `prepared` 값이 남아도 재시작 진입은 owner plan의 최신 `isolated` 상태를 읽어 workspace를 확인하고 완료 helper를 중복 호출하지 않아야 한다. 대표 A·대상 B의 차단 사유, branch, snapshot이 달라도 A 투영은 보존하고 B의 복구 상태만 소비한다.
- owner 필드 없는 기존 `prepared` 기록은 대상 plan recovery가 없거나 양쪽에 있어도 `projects` 상태를 기준으로 완료해 호환 projection이 `isolated`로 바뀌어야 한다. `plan_path`가 ExecPlan을 가리키면 기존 mirror 규칙에 따라 대상 행도 함께 바꾸며, 재시작은 시작 snapshot 누락이나 완료 helper 재호출 없이 격리된 baseline으로 재개해야 한다.
- unittest adapter의 launcher Python과 docs/VERIFY.md의 Python 3.11–3.14 계약을 확인했다. 현재 verify가 보고한 게시 rebase lease 회귀를 재현한 뒤, persisted ownership이 `START`를 소유 근거로 받아들이지 않도록 수정하고 sealed acceptance 없는 상태를 명시하는 단일 case를 검증했다.

  FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_completed_published_rebase_keeps_remote_lease_only_with_sealed_owner
  RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_completed_published_rebase_keeps_remote_lease_only_with_sealed_owner
  최종 동일 명령 PASS — 1 test, OK. 전체 verify는 실행하지 않았으며 Manager가 별도로 실행한다.

### 현재 후보의 중첩 verify timeout 교정

- HEAD `98bb4e60`와 base `2464194a`의 전체 diff·선택 계획·PRODUCT/ARCHITECTURE 및 source 계약을 다시 확인했다. adapter는 `unittest`, Python은 launcher 선택이며 지원 계약은 3.11–3.14다. Manager 진단 `2b4e2b3904f7a598702cddaeb8d639e8`은 4,451 success·7 skip·2 failure를 기록했고, 두 실패는 중첩 실제 runner의 30초 상한 종료였다. timeout 뒤 내부 임시 로그는 정리되어 세부 대기 원인까지 확정할 수 없으며, 제품 rebase 판정 실패의 근거는 관측되지 않았다.
- 동일 테스트에 대한 기존 `5491eab8`의 timeout 조정 패턴을 재사용했다. partial/full fixture와 언어 예제의 내부 예산을 90초로, 독립 subprocess의 바깥 상한을 120초로 조정해 준비·종료 여유를 둔다. coordinated 예제는 기존 Manager timeout 전달 경계에 90초를 전달한다. manifest·성공/실패·native report·예약 반환 assertion과 timeout/종료 불명 정책은 유지했다. 제품 timeout, adapter, verify 입력 목록은 변경하지 않았다.
- 이번 후보에서 targeted runner로 partial timeout 진단 보존·종료 불명 정책 unit case 2개, 관측 실패 진단 보존 case, 대표 A 아래 명시 B direct rebase 완료 case를 실행해 4개 모두 PASS했다. 변경한 두 테스트 파일의 Python 구문도 확인했다. Worker의 integration 실행 금지에 따라 실제 partial/full subprocess case와 언어 예제 integration case는 실행하지 않았다. 이 두 실패의 실제 해소 확인과 전체 verify·Review는 Manager가 수행해야 하며, 이번 unit PASS를 통합 PASS로 보고하지 않는다.

- 결과 검증 교정 호출에서 현재 worktree의 기존 timeout 조정을 재확인하고 `tests.test_partial_verify_runner.PartialRunnerTests.test_timeout_normalization_preserves_partial_diagnostic_report`와 `tests.test_partial_verify_runner.PartialRunnerTests.test_manager_partial_timeout_and_unknown_termination_keep_normal_policy`를 targeted runner로 실행했다. 이번 호출은 2 tests, OK, exit code 0이며 미해결 targeted unit test 실패는 없다. 테스트 내부에 의도적으로 주입한 TIMEOUT·종료 불명 로그는 unit test 실패가 아니다. 전달된 실제 통합 실패의 재실행은 Worker 실행 범위 밖이므로 Manager 검증 대상으로 유지하며 unit 결과와 분리한다. 코드 추가 변경은 하지 않았다.

## Progress

- [x] revision `1f8b97e6c04868281cd23672396f1fd3d47ee98e`에서 대표 투영 직접 읽기와 새 작업별 선택 경계, 관련 SQLite/handler 회귀를 정적으로 확인했다. 이 정적 감사에서 차단 Agent 복구 소비자의 직접 대표 상태 읽기를 놓쳤다. 구현·테스트·verify는 실행하지 않았다.
- [x] revision `41f8c16d78f203701a1cdd5cba1cebd6c04be465`에서 대상별 복구 context·lease와 repository/port 재관측의 직접 구현·회귀를 확인했다. 기존 두 소비자의 대표 행 조회와 direct rebase의 투영 없는 완료 transaction은 남아 있음을 정적으로 확인했으며 실행 성공은 기록하지 않았다.
- [x] `2219730c40def01f6c38ed1d68b8c35fa44c29e7`에서 일반 차단의 선택 작업 읽기·decision ID 확인, 새 rebase의 대상 PR head 조회와 validation 실패의 관측/원자 적용 및 관련 회귀를 정적으로 확인했다. 실행 검증 성공은 기록하지 않았다.
- [x] 차단 관측 후 현재 상태 재검사, direct-rebase 완료 귀속과 BLOCKED Agent 보존·완료의 선택 plan 상태 귀속을 교정했다.
- [x] 현재 revision의 direct rebase 완료가 외부 재관측을 transaction 밖에서 수행하고 대상/checkpoint를 비교하는 직접 구현을 정적으로 확인했다. 과거 조사 기록은 당시 상태로 보존하며 실행 성공을 뜻하지 않는다.
- [x] Review에서 대표 A와 선택 B의 branch·snapshot이 다른 BLOCKED Agent 복구 실패를 재현했다. preserve/completion을 명시 plan context의 상태 읽기·갱신으로 연결하고, 기존 대표 투영 보존과 선택 recovery 완료를 targeted 회귀로 확인했다.
- [x] Review에서 완료 이후 재시작 진입이 `projects`의 오래된 `prepared` 복구를 다시 소비하는 누락을 확인했다. `owner_plan_id`로 대상 선택 뒤 최신 plan recovery를 읽도록 연결하고, 비폐기 차단 사유에서 A 대표 투영·B의 `isolated` 상태를 보존하며 완료를 반복하지 않는 회귀를 추가했다.
- [x] Review에서 owner 필드 없는 기존 기록도 completion에 plan ID를 전달해 상태 원본이 갈리는 누락을 확인했다. 호환 경로는 `projects` projection을 기준으로 완료하도록 연결하고, plan recovery가 없거나 양쪽에 있는 경우 완료 후 재진입 회귀를 추가했다.
- [x] 작업 귀속 교정 뒤 복수 작업의 차단·rebase·복구 상태와 기존 외부 관측/DB 반영 회귀를 targeted 검증했다.

## Decision Log

- 현재 delta의 차단 대상·새 rebase PR head 조회와 validation 실패 원자 적용은 직접 구현으로 제외한다. 남은 일반 차단 관측의 CAS와 direct-rebase 완료 귀속을 같은 ID에서 보완하고 기존 dependency·목표를 유지한다. 대표 투영을 바꾸는 우회나 실제 프로젝트 차단 축소는 하지 않는다.

- 이번 delta에서 완료 재관측의 transaction 분리는 직접 구현됐으므로 남은 Implementation을 그 경계의 소비/회귀로 개정한다. 차단 대상·PR OID·phase의 작업 귀속 목표와 기존 metadata·dependency·기록은 보존한다. 같은 관측 이동을 소급 구현하거나 테스트 성공으로 기록하지 않는다.

- complexity medium: 기존 작업 snapshot·선택 context·CAS의 명확한 경계에 남은 소비자를 연결한다. risk high: 다른 작업의 차단/결과나 PR OID를 사용하면 정상 작업을 중단하거나 잘못된 복구·게시 판단을 만들 수 있다. 효과 전 identity 검증·원본/감사 보존과 소비자별 복수 작업 회귀로 방지하고 불명확한 대상은 보존한다. priority high는 병렬 실행에서 작업 격리를 유지하는 기준이다.
- G-297의 배정/대기와 G-278의 실패 planning 복원은 별도 목표다. G-189의 전수 감사와 중복하지 않도록 확인된 소비자 교정을 먼저 독립 Merge하고 감사는 그 결과를 추적한다. 새 사용자 정책·승인·상태 원본·migration을 만들지 않으며 Model·Effort는 Manager 설정을 따른다.
- 최신 R-055/R-091의 대상별 차단 재평가와 transaction 밖 재관측은 직접 구현을 소비하되 같은 direct-rebase 파일에 남은 귀속·완료 적용만 보완한다. 공통 wrapper·예약·의미 판단을 재설계하지 않으며, PRODUCT 없는 등록 프로젝트의 사용자 질문 source 조회는 G-301이 별도로 담당한다. 최초 level 3에 해당하는 medium complexity와 기존 high risk·priority, ID·경로·sources·dependency·수행 기록을 유지한다.

- 현재 Worker 변경은 일반 차단 관측 뒤 대상 ExecPlan 전체 row를 CAS로 재검사하고, 명시적 plan ID와 선택 context 불일치를 효과 전에 거부한다. direct-rebase 완료는 명시 target 선택 아래 해당 plan의 phase/checkpoint/candidate만 갱신해 다른 대표 투영을 보존한다. 차단 대상도 기존 차단 상태·원인을 유지하며 acceptance만 저장하고 후속 해제 판단은 기존 복구 경계에 맡긴다.
- 기존 감사는 `blocked_agent_service.py`의 보존·완료 경로에서 프로젝트 대표 Agent 상태를 직접 읽는 누락을 놓쳤다. Review에서 확인한 두 helper를 선택 plan context로 연결했다. `checkpoint_adapter.py`·`merge_cleanup_service.py`는 명시 대상 또는 프로젝트 coordination/등록 원본을 읽는 경계여서 변경하지 않았다.
- verify 선언의 adapter는 `unittest`, Python은 launcher 선택이며 docs/VERIFY.md 계약은 stdlib unittest 3.11–3.14다. 프로젝트 Python의 targeted runner로 차단 CAS·PR head 선택·선택 불일치·대표 투영 아래 활성/차단 대상 완료·checkpoint 경합·상위 rebase 재관측의 10개 targeted test를 실행했고 모두 통과했다. 전체 verify는 실행하지 않았다.
- Review 회귀와 변경한 completion 호출 assertion을 프로젝트 Python targeted runner로 각각 실행해 수정 후 통과했다. 전체 verify와 기존 10개 targeted test는 재실행하지 않았다.
- 새 A/B BLOCKED Agent 복구 회귀와 `test_blocked_agent_preservation_resumes_pending_recovery_for_current_input`이 수정 후 각각 통과했다. 대표 project의 branch·snapshot·planning projection은 보존됐고 B의 recovery만 격리 상태로 완료됐다.
- 현재 후보에서 비폐기 차단 사유의 A/B 복구 완료 후 재진입과 기존 격리 recovery 재시작 targeted case를 실행했다. 대표 `projects`에 남은 오래된 recovery가 완료 재호출의 근거가 되지 않고, 대상 plan의 현재 상태가 재진입 결과를 결정함을 확인했다.
- 완료 후 중단 복구 재진입도 대표 projection을 다시 사용하면 이미 `isolated`인 대상 plan에 stale `prepared` snapshot을 적용해 실패할 수 있다. 새 복구는 owner ID로 plan을 선택해 최신 recovery를 소비하며 representative `projects` 값은 그대로 두어 선택 외 작업을 덮지 않는다.
- 현재 후보의 verify에서 게시 rebase lease 확인이 `base_is_ancestor=False`인 `START` 상태를 소유 완료로 허용하는 회귀를 받았다. persisted rebase ownership은 `WAITING`·`COMPLETE`만 인정하고, sealed acceptance가 없는 시작 가능 상태로 게시 lease를 유지하지 않도록 했다. 해당 조건을 명시한 단일 targeted case의 결과는 아래 검증 기록에 남긴다.
- 현재 verify delta에서 rebase checkpoint의 최종 Worker scope 검사가 active replay/stash marker가 없을 때 current rebase 판단을 건너뛰고, `managed_worktree_conflict` 재평가도 `START`를 해제 근거로 받아들이는 누락을 확인했다. 최종 scope 검사는 현재 decision을 다시 확인해 `START`를 거부하고, 중간 `WAITING`만 기존처럼 최종 파일 검사 전 보류한다. 반복 충돌 차단은 `START`로 해제하지 않되 `plan_input_invalid`의 기존 재개 계약은 유지한다. base ancestry mismatch 전용 차단 회귀를 분리해 추가했다.
- 현재 후보의 프로젝트 Python targeted runner에서 index/operation mismatch matrix, 새 unstarted-rebase block preservation, 완료 rebase current-result policy, 중간 rebase scope deferral case를 각각 실행했고 모두 통과했다. 처음 실행은 base ancestry mismatch와 final scope policy 누락을 재현했고, 보완 뒤 동일한 case 명령을 다시 실행해 통과했다. 전체 verify는 실행하지 않았으며 이후 Manager가 실행한다.

- 관리 rebase를 수용한 HEAD `dd221214`와 base `2464194a`의 최종 diff 및 최신 sources를 다시 확인했다. Review delta의 `observe()` 실패 결과가 content 검사로 전달되는 오류를 수정했다. `ObservationFailure`·`UNAVAILABLE`은 상태별 처리 전에 거부하며 원래 진단을 보존한다. checkpoint mock은 `observe()`의 관측/decision 반환 계약과 완료 관측의 ancestry 필드를 제공하도록 갱신했고, Git 조회 실패·관측 경쟁 진단 보존 회귀를 추가했다. 사용하지 않는 replay/stash 사전 계산은 제거했으며 관련 검증은 실제 관측 기반 content 판정과 기존 Worker scope 회귀가 유지한다.
- 이번 세션에서 launcher Python의 targeted runner로 `tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests` 18개와 `tests.test_scope_block_revalidation.EvidenceFreeRebaseBlockTests.test_remote_scope_index_and_operation_mismatches_preserve_block`, `test_unstarted_rebase_state_preserves_managed_conflict_block` 2개를 실행해 모두 PASS했다. 전달된 verify 실패 matrix는 현재 후보에서 통과했고 추가 차단 정책 변경은 하지 않았다. 이번 세션의 targeted 실패는 없으며 이전 후보의 성공을 재사용하지 않았다. 전체 verify·Review는 Manager가 후속 실행한다.
