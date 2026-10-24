---
id: G-305
title: Agent DB 대기와 실제 호출 결과 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-073] 호출 목적과 사용량 범위 기록 및 조회.md
depends_on:
  - G-303
complexity: high
risk: high
priority: high
---

## Purpose

Agent의 지침·session·격리/실행 identity·호출 기록 저장에서 DB 대기를 실제 provider 실패와 구분한다. 이미 확보한 물리 호출 결과를 보존하고 DB 재시도 때문에 같은 Agent를 다시 실행하지 않는다. 필수 안전 증거가 없는 호출은 시작/승인하지 않으며 선택적인 사용량 수집 실패는 원래 결과를 유지한다.

기존 guidance 준비 대기 전파·완료 결과/session 보존과 `storage/common_guidance_state.py`의 유일한 완료 invocation 귀속 fallback을 재사용한다. G-303의 DB 요청 재시도를 invocation 준비·launch·callback·session·종료 저장에 연결하고, 기존 process receipt의 실제 관측으로 같은 호출의 DB 확정을 재개한다. 역할 응답의 원문이 필요하면 기존 provider 자료와 요청/결과 지문을 확인하며 자료 부족·충돌을 새로운 provider 실행으로 우회하지 않는다.

## Acceptance Criteria

- R-091: 호출 전 필수 DB 준비는 공통 예산에서 최신 상태를 재확인해 제한 재시도한다. 예산 초과/취소는 해당 요청·작업의 보존된 대기로 처리하고 Worker 교정·일반 단계 실패·provider 설정/권한 실패 횟수에 합산하지 않는다. 미시작 대기는 실제 호출 수·실행 timeout을 만들지 않는다.
- 필수 invocation·지침/session 귀속·격리/실행 identity·targeted 호출 결합을 저장하지 못하면 CLI를 시작하거나 검증/게시 승인을 내리지 않는다. 일시 DB 경합과 실제 정책/경로/권한·자료 오류를 구별하며 재시도를 이유로 필수 증거 저장을 선택 사항으로 바꾸지 않는다.
- 실제 시작 뒤 callback·session 확정·호출 종료 저장이 대기하면 이미 관측한 시작/종료·provider 결과/예외·session·사용량과 기존 launch/process receipt를 보존한다. DB 확정만 재시도·재개하고 완료한 provider 실행을 반복하지 않는다. 종료 불명은 계속 불명이며 호출 자리도 기존 실제 종료 근거 없이 해제하지 않는다.
- R-073: 선택적인 ledger/usage 수집 오류는 원래 Agent 결과·예외를 바꾸지 않고 부분 수집/오류로 남는다. 관측한 결과를 DB 오류용 빈 성공/실패로 덮거나 새로운 invocation·가짜 사용량을 만들지 않는다. 자료가 부족하면 해당 후속 처리를 보류하고 실행 성공이나 안전 증거를 추정하지 않는다.
- 정상·실패·중단·재시작에서 같은 물리 호출의 저장은 중복되지 않는다. 원래 오류와 저장 오류를 모두 진단할 수 있고, 독립 작업은 계속한다. 비밀값·prompt·코드/문서 전문을 운영 요약이나 새 감사 자료에 추가하지 않는다.
- R-091의 DB 준비/저장 대기 만료는 해당 요청의 재시도 대기로 전달하고, 실제 차단이 없으면 G-297의 같은 실행 재배정에서 DB 확정만 다시 준비한다. 필수 실행 전 증거가 없으면 미시작을 유지하며 실제 결과가 있으면 provider를 재호출하지 않는다. 실제 provider 실패의 묶음/조건 대기는 G-313이 담당하고 DB 경합과 합산하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/agents/invocation_service.py:run_agent()`의 invocation 준비, `observe_session/isolation/execution_identity/targeted/launch`와 정상/예외 종료 저장에 G-303의 제한된 DB 요청 재시도를 연결한다. 이미 반영된 준비 대기 전파·완료 결과 보존을 다시 구현하지 않는다. 준비/실행/완료 단계별로 DB 확정만 재개하고 임의 provider callback 전체를 replay하지 않으며 공통 대기 오류를 영구 설정/격리 실패로 재분류하지 않는다.
- [x] 확인한 `storage/common_guidance_state.py`·`agent_invocation_state.py`·`task_calls.py`의 task/session·실제 호출/종료·예약 identity를 재사용한다. 새 session 귀속 fallback은 유지하되 ledger 종료도 저장되지 않은 경우까지 해결됐다고 가정하지 않는다. 이미 확보한 결과와 `agents/process_receipt.py`·기존 launch 자료를 같은 invocation에 연결해 저장 대기 뒤 확정하며 새 ledger·인증/설정 캐시·관측 서비스를 만들지 않는다. 실제 provider 결과가 없는 과거 행은 추측으로 채우지 않고 귀속 충돌은 계속 거부한다.
- [x] 확인한 `storage/targeted_test_state.py`와 Worker의 기존 수집 pending 경계를 유지한다. G-292는 Claude의 callback·허용 경로 전달을, G-185는 provider 사용량 범위·파싱·조회와 예외 원문 회수를 담당한다. 이 작업은 공통 DB 경합의 분류·기존 결과 보존만 맡고 해당 기능을 중복 구현하지 않는다.
- [x] 확인한 `tests/test_agent_invocation.py:RunnerTests`·`tests/test_agent_invocation_state.py`·`tests/test_task_scheduling.py`의 실제 저장/호출 예약 fixture에 DB 대기 위치별 최소 unit을 연결한다. PRODUCT·ARCHITECTURE의 Agent 준비·호출 저장 절은 실제 구현한 결과로 정합화한다. G-191의 보존된 Claude 구현·실행 이력은 변경하지 않는다.

## Validation

- Worker는 위 파일의 변경 관련 최소 unit case만 실행한다. provider는 double로 사용하고 실제 invocation/session·예약·targeted 저장 정책을 검사한다. 필수 준비 전 BUSY 해소/한도 초과/취소에서 mock CLI 호출 수가 각각 1/0/0이며 일반 실패·교정 횟수는 늘지 않아야 한다.
- 실제 결과 반환 직후 session 저장 대기, 격리/실행 identity 저장 대기, 시작·종료 callback 저장 대기와 targeted 결합 실패를 구분한다. 선택적 usage 오류와 필수 안전 증거 부재의 결과가 달라야 하고 실행 중 timeout·실제 provider 실패·종료 불명은 그대로 유지해야 한다.
- 물리 성공/실패 뒤 DB 확정 전 중단·재시작에서 기존 invocation·receipt·session과 관측 사용량을 보존하고 provider 재호출·중복 종료·가짜 PASS가 없어야 한다. 새 저장 오류는 원래 오류를 로그에서 가리지 않아야 한다.
- 기존 `test_session_db_wait_keeps_completed_result_and_recoverable_binding`·`test_guidance_database_wait_does_not_start_or_reclassify_provider`·`test_ledger_failure_does_not_change_agent_success`·`test_auxiliary_collection_error_keeps_success_and_usage_in_ledger`와 targeted 중단/retry/fallback·독립 provider 실패 회귀를 재사용한다. session 저장과 ledger 종료가 함께 끊긴 경우·준비별 재시도·물리 종료 뒤 재개라는 미검사 경계만 보강하며 같은 파서/사용량 범위 assertion을 복제하지 않는다. mock 결과를 실제 Claude/Codex 계정·OS 격리 성공으로 기록하지 않는다.

## Progress

- [x] 현재 revision의 invocation 서비스·영속화·실제 결과 보존 회귀를 정적으로 대조했다. 최초 guidance DB 대기 전파와 선택적 ledger 오류의 기존 성공 보존은 재구현 대상에서 제외한다.
- [x] `2219730c40def01f6c38ed1d68b8c35fa44c29e7`에서 추가 준비 대기 전파·정상 결과/session 보존·완료 invocation 귀속 fallback과 두 회귀를 정적으로 확인했다. 구현 수행·실행 검증 성공을 새로 기록하지 않았다.
- [ ] 필수 준비·callback·종료 저장의 DB 요청 재시도와 물리 결과의 비중복 확정/재개
- [ ] 최소 targeted 회귀와 중단 뒤 비중복 확정 검증

### 준비 단계 중간 보존점

- guidance 저장소 초기화·독립 작업 identity·기존 session 귀속 조회, invocation 생성/지침 결합, 격리·실행 identity 저장을 `CoordinatedConnection.run_request()`에 연결했다. 지침 파일 생성과 provider 실행·사용자 callback은 재시도 본문에 넣지 않았다. invocation 생성과 지침 결합은 같은 요청에서 rollback하며 소유 scope를 매번 다시 읽는다. 일반 sqlite3 연결은 기존 repository transaction 동작을 유지한다.
- invocation 준비는 물리 프로세스 생성이 아니므로 `_start_agent_invocation()`에서 launch lock을 잡지 않고 handoff 확인만 수행한다. DB 요청에서 launch lock을 기다리는 순서 역전을 피하며 실제 subprocess 시작의 `gate.popen()` 경계는 유지한다.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest`에 `tests.test_agent_invocation.RunnerTests.` 아래 `test_invocation_preparation_retries_atomically_or_leaves_provider_unstarted`, `test_prelaunch_evidence_db_retry_does_not_replay_provider`, `test_guidance_database_wait_does_not_start_or_reclassify_provider`, `test_session_db_wait_keeps_completed_result_and_recoverable_binding`, `test_ledger_failure_does_not_change_agent_success`, `test_auxiliary_collection_error_keeps_success_and_usage_in_ledger`를 전달해 6개 모두 통과했다. launch lock 수정과 해당 assertion 추가 후 앞의 신규 두 case를 같은 runner로 실행해 2개 모두 통과했다. SQLite BUSY/LOCKED 주입은 실제 coordinated 요청의 rollback/retry를 사용하며 실제 Agent CLI는 호출하지 않았다. 전체 verify는 실행하지 않았다.
- 이 보존점 시점에는 완료 후보가 아니었다. launch/targeted 결합, 시작·종료 callback, session 및 ledger 종료 저장의 대기 보존·재시작 복구, 물리 호출을 반복하지 않는 같은 실행 재배정과 PRODUCT·ARCHITECTURE 정합화가 남아 있다. 이번 checkpoint의 준비 단계 결과로 실제 호출 결과의 영속 복구나 전체 Acceptance Criteria 충족을 주장하지 않는다.

### launch 결합 중간 보존점

- `f951d068d1e401c43ee261166cce28433a25b66f`의 게시 확인 뒤 launch의 예약/receipt/invocation 결합과 targeted 결합을 DB 요청 재시도에 연결했다. targeted 경로·inode 검사는 재시도 밖에서 수행하며, SQL은 현재 invocation이 미시작·미완료 Worker인지 재확인한다. 명시적인 caller transaction은 기존 rollback 소유권을 유지한다.
- launch callback이 반환되기 전에 거부되면 기존 Manager 전용 process receipt에 `not_started` 근거와 관측 시각만 남긴다. 기존 receipt는 덮지 않으며 원래 DB 대기와 receipt 쓰기 오류를 모두 진단한다. 미시작 DB 대기의 ledger 종류는 `database_wait`로 분리하고 예외는 그대로 전달한다. 이 근거는 프로세스 종료 성공이나 provider 결과가 아니다. invocation 시작 기록과 충돌하는 미시작 근거는 종료 확정·예약 회수·targeted 수집에 사용하지 않는다.
- 이번 호출에서 공통 targeted runner로 다음 최소 case를 실행했다. `RunnerTests.test_launch_binding_wait_retries_db_only_and_preserves_proven_non_execution`은 예약 receipt·invocation receipt·targeted INSERT의 BUSY rollback과 해소/만료/handoff 9개 조합을 확인한다. `ProcessReceiptTest`의 신규 2개 case는 재시작의 미실행 복구와 기존 시작 근거 보존을 확인한다. 최초 3개 case 통과 후 예약 충돌 assertion과 targeted 소유권 회귀를 보강해 관련 6개 case를 통과했다. caller transaction 보존 추가 후 해당 case와 launch case 2개, 수집 모순 거부 추가 후 해당 case와 기존 재시작 회귀 2개, receipt 쓰기 실패의 원래 오류 보존 case 1개를 각각 통과했다. 실제 Agent·전체 verify·Windows native 검증은 실행하지 않았다.
- 이 보존점 시점에는 완료 후보가 아니었다. 실제 시작 뒤 callback/session/ledger 종료 관측의 영속 보존·DB 확정 재개와 같은 실행 재배정, 독립 `on_targeted_prepared` 경로의 저장 대기, PRODUCT·ARCHITECTURE 계약 정합화가 남아 있다. 다음 작업은 관측 결과를 같은 invocation/receipt에 보존하고 선택적 수집 오류와 필수 후속 보류를 구별하는 경계다. provider 실행 재호출로 복구하지 않는다.

### 실제 호출 관측 영속화 중간 보존점

- `96db3fffd36ba40d88616d950657be6aedbc4be4`의 게시 확인 뒤 시작·종료·session callback과 정상/예외 종료의 DB 요청 재시도를 연결했다. 임의 `on_session_started` callback은 DB 요청 본문에 넣지 않는다. 반복 시작 callback은 첫 관측을 유지하며, 종료 시각·실행 시간·결과 시각은 DB 재시도 시각으로 바꾸지 않는다.
- 기존 Manager 전용 process receipt의 `invocation_observation` version 1에 invocation ID, 관측 시작/종료 시각·실행 시간, session, 결과 종류·코드·관측 시각·사용량·수집 상태를 저장한다. prompt·결과 메시지·코드/문서 전문이나 새 ledger는 추가하지 않는다. 기존 프로세스 시작/종료 receipt 갱신은 이 필드를 보존한다. 경로·identity·필드·타입·사용량·시각을 검증하며 손상·다른 invocation 자료는 pending으로 남긴다.
- `InvocationRepository.apply_observation()`은 같은 invocation의 ledger 확정만 수행한다. `reconcile_interrupted()`는 먼저 저장된 실제 관측을 반영해 원래 성공/실패/예외를 보존하고, 관측이 없을 때만 기존 interrupted 처리를 유지한다. DB 적용 대기·자료 오류가 있는 invocation은 interrupted로 덮지 않는다. 예약 확인/회수도 해당 invocation의 저장 재개를 먼저 수행하고 pending이면 예약을 유지한다. 기본 SQLite caller transaction에서도 관측 적용의 중간 commit 없이 rollback한다.
- 실제 provider 결과를 확보한 뒤 session 저장에서 오류가 나면 이미 관측한 결과를 빈 예외 결과로 바꾸지 않는다. 원래 provider 예외를 처리하는 동안 저장이 handoff로 중단돼도 원래 예외와 저장 오류 종류를 함께 진단한다. session 결합과 ledger 종료가 모두 끊긴 경우, receipt의 실제 결과·사용량·시작/종료·session을 재개해 기존 완료 invocation 귀속 fallback으로 확인한다. 자료가 없는 과거 행은 채우지 않는다.
- 공통 targeted runner로 신규 `RunnerTests.test_receipt_recovers_observed_result_and_exception_after_all_callback_storage_waits`(성공/실패/원래 예외 3개 재시작 조합), `test_callback_database_requests_retry_without_replaying_the_session_callback`(시작/session/종료 BUSY와 중복 callback), `test_storage_handoff_does_not_replace_the_original_provider_exception`, `ProcessReceiptTest.test_process_receipt_updates_preserve_the_invocation_observation`, `test_malformed_or_foreign_invocation_observation_preserves_pending_state_and_reservation`을 통과했다. 변경 경계에 맞춰 기존 session fallback·실행 시간·선택적 사용량 오류·receipt/caller transaction·미시작 모순·interrupted session 회귀를 함께 실행해 통과했다. scoped 예약 재수집과 중복 callback 보강 후 관련 최소 case를 재실행해 통과했다. 실제 CLI·전체 verify·Windows native 검증은 실행하지 않았다.
- 신규 native caller rollback 회귀의 최초 실행은 fixture가 transaction을 시작하지 않아 1회 실패했다. 명시적 `BEGIN IMMEDIATE`로 caller transaction을 시작하도록 fixture를 교정하고 동일 assertion·명령으로 통과했다.

  FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_invocation_state.InvocationRepositoryTests.test_observation_application_keeps_the_callers_atomic_rollback

  RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_invocation_state.InvocationRepositoryTests.test_observation_application_keeps_the_callers_atomic_rollback

  재실행 결과: 1개 통과, exit 0.

- 이 보존점 시점에는 완료 후보가 아니었다. 같은 실행의 요청 재배정에서 저장 완료 뒤 원래 역할 결과를 소비하는 연결과 결과 본문 근거 부족 시 provider를 재호출하지 않는 후속 보류, 직접 `on_targeted_prepared` 경계의 미시작 대기, PRODUCT·ARCHITECTURE 정합화가 남아 있다. 이번 receipt는 결과 본문을 새로 저장하지 않으므로 역할 응답을 그 metadata만으로 복원했다고 주장하지 않는다. 필요한 실제 원문은 기존 provider 자료에서만 확인하고 부족하면 후속 처리를 보류해야 한다.

### 최초 구현 후보와 targeted 검증 (2026-10-24)

- `37c0a34c5cdd515ae5329eda01ef6bce5328251d`의 게시 확인 뒤 요청 재개와 결과 전달을 연결했다. 준비 DB 대기는 기존 RETRY 배정에서 원래 작업 입력을 재확인하며, callback 전체나 provider 실행을 DB 재시도 본문에 넣지 않는다. Planner 등 독립 Agent 요청도 같은 실행에서 DB 준비·확정을 재시도하고 local pause/handoff를 확인한다. DB 재시도 대기 중 runtime 원격 관측은 하지 않고 준비 후 실제 단계 진입에서 관측한다.
- 실제 결과를 받은 관리 호출은 원래 스택의 결과/예외를 유지하며 저장만 재개한다. 예약 정리 DB 대기가 발생해도 반환된 응답을 다시 호출하지 않는다. 실제 종료가 확인되면 실행 개수만 먼저 반환하고 저장 대기 예약은 유지하며, 재시작과 나중의 종료 확인에서도 같은 분류로 한 번만 반환한다. 실제 종료 불명은 계속 실행 자리를 보존한다. 원래 예외와 예약 정리 오류도 함께 진단한다.
- receipt의 `database_pending`과 요청/응답 SHA-256으로 미전달 결과를 구별한다. 재시작 뒤 같은 요청·확정 ledger 결과/session/사용량·session 귀속을 대조하고 기존 `last-message.txt` 원문 지문을 확인해 같은 invocation의 응답을 전달한다. 원문 부재·변경·다른 요청·다른 ledger/session 귀속·원래 예외 자료 부족은 CallDeferred이며 새 provider 실행·가짜 PASS·새 ledger/usage를 만들지 않는다. 원문을 새 감사 자료에 복사하지 않는다.
- 직접 `on_targeted_prepared` 경로도 검증된 receipt·invocation을 기존 호출 예약에 결합한다. 실행 전 DB 대기에는 미시작 근거를 남기고, 실행 시작 뒤 종료 근거가 없으면 예약을 유지한다. 선택적 callback 관측의 일반 SQL 오류는 저장 가능한 원래 결과를 수집 오류로 보존하며 DB 경합과 구별한다. 부재한 안전 증거를 보충하지 않는다.
- PRODUCT의 Agent 호출 기록과 ARCHITECTURE의 호출 관측·DB 확정 계약을 실제 구현으로 갱신했다. 활성 Requirement 목록과 R-091/R-073의 DB·사용량 조건 및 관련 runtime handoff 기준을 대조했다. Requirement·다른 ExecPlan·선택 계획 frontmatter·사용자 결정은 수정하지 않았다. 기존 verify 입력 목록의 코드·테스트 glob이 변경한 실행 입력을 포함하며 새 의존성·설정·외부 서비스를 추가하지 않았다.

검증 결과:

- 최초 후보 제출 호출의 공통 targeted runner 기록에서 서로 다른 unit case 29개의 최신 결과가 모두 success다. 실제 SQLite 파일/재연결·요청 rollback을 사용하고 provider는 double로 유지했다. 준비·필수 증거·launch·session·실제 시작/종료·선택 수집·예약 정리·동일 실행 재개·재시작 원문 확인·독립 배정·handoff·종료 불명을 최소 case로 검증했다. 중복 테스트나 fixture를 제거하지 않았다.
- 기존 ledger 부재 회귀가 새 복구 조회의 일반 SQL 오류 전파로 1회 error였다. DB 대기는 계속 전달하고 일반 조회 오류는 기존 선택적 수집 오류로 처리하도록 고쳐 기존 assertion을 유지한 채 같은 6개 case 명령을 재실행해 모두 통과했다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_invocation.RunnerTests.test_storage_pending_restart_consumes_existing_message_or_holds_without_provider tests.test_agent_invocation.RunnerTests.test_direct_targeted_preparation_binds_the_reservation_and_preserves_unknown_exit tests.test_agent_invocation.RunnerTests.test_launch_binding_wait_retries_db_only_and_preserves_proven_non_execution tests.test_task_scheduling.TaskSchedulingTests.test_restart_separates_stopped_storage_wait_from_an_unknown_agent_exit tests.test_agent_invocation.RunnerTests.test_transient_retry_and_timeout_have_distinct_rows tests.test_agent_invocation.RunnerTests.test_ledger_failure_does_not_change_agent_success

RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_invocation.RunnerTests.test_storage_pending_restart_consumes_existing_message_or_holds_without_provider tests.test_agent_invocation.RunnerTests.test_direct_targeted_preparation_binds_the_reservation_and_preserves_unknown_exit tests.test_agent_invocation.RunnerTests.test_launch_binding_wait_retries_db_only_and_preserves_proven_non_execution tests.test_task_scheduling.TaskSchedulingTests.test_restart_separates_stopped_storage_wait_from_an_unknown_agent_exit tests.test_agent_invocation.RunnerTests.test_transient_retry_and_timeout_have_distinct_rows tests.test_agent_invocation.RunnerTests.test_ledger_failure_does_not_change_agent_success

재실행 결과: 6개 통과, exit 0. 이후 변경 경계의 최소 회귀도 모두 통과했다. 당시 targeted runner의 마지막 실행·수집 결과에는 미해결 failure/error·unexpected success가 없었다.

- 전체 verify·실제 Agent CLI/계정·Windows native 검증은 실행하지 않았다. 전체 verify와 Reviewer 검토·사용자 PR Merge는 Manager의 다음 단계다. Worker 결과는 구현 후보이며 자동 작업 완료나 Merge 성공 선언이 아니다.

### verify 피드백 교정 중간 보존점 (2026-10-24)

- 현재 delta는 `verify:37fa3d96428f8cb8ada8c7fb8ac2998b9d4e6c7177d2bcbc59bee5a5461b5b42`, occurrence 1, revision 1이다. Manager의 `c7bcddfad263704d27e70b42af914b6d064af05d` 전체 verify는 exit 1이며 두 case가 error였다. 진단은 `/home/ubuntu/.local/state/loop-manager/logs/verify/6a82c1b3ae0820f692d00e9237a6c589/summary.json`이다. 전체 verify를 Worker가 재실행하지 않았다.
- 두 case를 최소 targeted 명령으로 재현했다. callback/직전 단계가 남긴 활성 transaction 때문에 `_database_request()`의 `run_request()`가 거부됐다. 완료된 암시적 쓰기는 기존 `finish_implicit_request()`로 한 번 확정한 뒤 DB 전용 요청을 시작하도록 수정했다. callback 자체는 replay하지 않으며 명시적 transaction·중첩 요청은 인수하거나 중간 commit하지 않는다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_origin_isolation.TaskRelevantInvocationScopeTests.test_run_agent_binds_the_physical_invocation_to_the_saved_scope tests.test_planner_interruption_recovery.PlannerInterruptionRecoveryTest.test_interrupted_planner_persists_started_session_and_resumes

최초 targeted 결과: 2개 error, exit 1.

RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_origin_isolation.TaskRelevantInvocationScopeTests.test_run_agent_binds_the_physical_invocation_to_the_saved_scope tests.test_planner_interruption_recovery.PlannerInterruptionRecoveryTest.test_interrupted_planner_persists_started_session_and_resumes

수정 후 동일 명령 결과: scope 귀속 case 1개 success, Planner 중단/재개 case 1개 error, exit 1. 통과로 집계하지 않는다.

- 신규 `RunnerTests.test_database_request_confirms_implicit_writes_and_preserves_explicit_rollback`과 기존 `test_callback_database_requests_retry_without_replaying_the_session_callback`을 같은 공통 targeted runner로 실행해 2개 모두 통과했다. 전자는 완료된 암시적 쓰기의 확정과 명시적 caller transaction의 rollback·연산 비실행을 확인한다. 기존 실패 case의 assertion·fixture·skip은 변경하지 않았다.
- Planner 재개는 두 번째 Agent 진입의 guidance 저장소 준비에서 caller transaction으로 인식되는 활성 상태가 남아 `run_request()`가 거부된다. 이 transaction의 소유·수명과 DB 요청 경계의 결합은 미해결이다. Manager verify 1회와 targeted 2회의 같은 Planner 본문 error를 확인했으므로 연속 3회 실패 제한에 따라 추가 구현 수정을 멈췄다. 다음 작업은 이 활성 transaction 경계의 원인을 확인하고 caller의 원자성·session 보존을 유지해 재개하는 것이다. 성공한 scope 귀속과 callback 비재실행 조건을 유지해야 한다.
- 현재 결과는 checkpoint이며 완료 후보가 아니다. Requirement·다른 ExecPlan·선택 frontmatter·사용자 결정을 변경하지 않았고, Manager 상태나 실패를 성공으로 기록하지 않았다.

## Decision Log

- 이번 R-091 delta에서 요청 한도 소진을 다음 외부 실행 전용 pause로 고정하지 않는다. 같은 실행의 저장 재개에서도 실제 provider 결과·호출 identity·필수 증거 gate를 보존하며 기존 G-303 dependency와 독립된 DB 확정 목표를 유지한다. 대기열·단계 실패 정책은 G-297/G-313의 책임이며 새 순환 dependency를 만들지 않는다. 구현·테스트는 실행하지 않았다.

- 이번 delta의 준비 대기 분류·정상 결과/session 보존·완료 invocation 귀속 fallback은 직접 구현으로 소비한다. G-303 뒤 DB 확정의 제한 재시도와 저장 중단 재개만 남기며 완료한 provider의 재실행을 복구 수단으로 사용하지 않는다. 필수 증거 거부·선택 관측의 부분 수집을 구분하고 G-191의 보존 후보를 다시 구현하거나 검증 성공으로 판단하지 않는다.
- complexity high: 필수 안전 증거와 선택 수집을 구분하면서 실제 CLI 시작 전/실행 중/완료 뒤의 대기·취소·결과 보존을 기존 session·호출 예약 수명과 결합해야 한다. risk high: 잘못된 분류는 증거 없는 호출·승인이나 완료한 writer의 재실행을 만들 수 있다. 실제 receipt·invocation identity·기존 자료를 유지하고 불확실하면 해당 후속 처리만 보류하며 준비/완료 경계의 중단 회귀로 검증한다. priority high는 무인 실행의 정확한 호출·안전 경계 보완 기준이다.
- R-073의 사용량 관측 확대나 G-191의 Claude 정책 구현과 별개다. 일반 DB 대기를 provider 실패로 처리하지 않되 실제 격리·인증·권한·도구 오류의 안전 거부는 유지한다. 실계정 실행·새 infrastructure·권한 확대나 사용자 결정을 추가하지 않는다.
