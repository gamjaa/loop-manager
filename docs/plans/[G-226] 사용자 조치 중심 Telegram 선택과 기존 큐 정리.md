---
id: G-226
title: 사용자 조치 중심 Telegram 선택과 기존 큐 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: medium
priority: high
---

## Purpose

Telegram 생성과 조각 전송을 현재 필요한 사용자 행동 또는 실제 자동 복구 불가 최종 중단으로 제한한다. 기존 G-226의 유지보수 시작·종료 무통지를 포함해 공통 판정과 오래된 큐 정리를 이 작업이 맡고, PR 검토 후보 identity는 G-247, draft 질문·링크 중복 방지는 G-294, 전체 후속 소비 감사는 G-189가 맡는다.

구현은 `notifications/policy.py`의 공통 현재성 판정을 생성과 각 조각 claim에 연결한다. 진행·완료·대기·시작/재개 생성과 취소 사건 복원을 제거하고, 기존 outbox·부분 전송·감사와 알림에 독립적인 운영 상태를 보존한다. PR 검토 후보와 draft 세부 identity 확장은 위 후속 작업의 범위를 유지한다.

## Acceptance Criteria

- R-053/R-055: 명시적 미해결 판단·인증/권한·설정·reset 등 현재 사용자 조치, 실제 새 PR 검토/Merge 요청 또는 자동 복구가 불가능하다고 확인한 최종 중단만 생성·전송한다. 상태 코드·오류 문구·retry/교정 묶음 소진·다음 실행 재개 가능 상태만으로 조치 필요를 확정하지 않는다. 근거가 불충분하면 기존 오류·상태를 보존하고 통지를 보류한다.
- planning·Worker·verify·Review·유지보수·프로젝트의 진행/완료, 일반 시작/재개·자동 교정·일시 실패·자원/사용량 대기·checkpoint에는 무음 메시지도 만들거나 보내지 않는다. 기존 cycle 범위·실제 Merge 확인·종료와 프로젝트 완료 상태 기록은 알림 여부와 독립적으로 유지한다.
- 생성 시와 각 미전송 조각을 claim할 때 최신 원인·남은 조치·자동 복구 가능성을 재검사한다. 해소/대체된 원인과 구정책의 pending 사건·부분 전송 잔여 조각을 취소한다. frozen payload/text·확인된 전송 cursor·attempt/error·기존 incident와 취소 감사는 덮거나 삭제하지 않는다. 실패 재시도·시작 복구·전송 확인 callback이 취소 사건을 부활시키지 않는다.
- 같은 현재 조치의 기존 중복 제거를 유지하며 알림 실패가 실제 작업 결과·재시도 횟수·소유권을 바꾸지 않는다. 조치의 사실 확인을 위해 추가 Agent 호출이나 Git 쓰기·새 전송 서비스를 만들지 않는다. 외부 관측은 DB transaction 밖에서 수행하고 적용 때 현재 원인/identity를 다시 비교한다.
- PR과 draft의 현재 유효성은 기존 관측·질문 상태에 연결할 수 있는 공통 경계를 제공한다. 이 작업만으로도 기존의 유효한 사용자 조치는 계속 전송되며 G-247/G-294의 세부 identity 보완을 기다려 전체 알림을 끄지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/notifications/events.py`·`repository.py`의 사건 선택과 `recovery/recovery_runtime.py`·`recovery/project_blocked_recovery.py`의 실제 원인/남은 복구 상태를 대조한다. 현재 사용자 행동의 구체적 근거를 기존 사건 payload/원인에 연결하고 status나 FINAL_BLOCK 이름만으로 판정하는 분기를 교정한다. 실행 복구 정책 자체는 G-313/G-189와 중복 구현하지 않는다.
- [x] 확인한 `notifications/delivery.py:_claim_piece()`와 `_deliver_rows()`의 조각별 현재 판정·취소/confirm 경계에 공통 정책을 적용한다. `dispatch_pending`·새 PR 즉시 전송·오류 재시도 모두 같은 기준을 거치게 하고 전송 중 해소/취소와 확인 callback의 경합에도 이미 전송한 기록을 보존한다.
- [x] 확인한 `notifications/incidents.py`의 `record_work_start`·`recover_interrupted_starts`·`_materialize_resumption`, `events.py`의 `record_process_start`·`record_planning_publication`·`enqueue_completion_if_ready`·`enqueue_waiting_reason`, `storage/maintenance_state.py`의 cycle 사건을 교정한다. Telegram enqueue만 제거하고 실제 시작·대기 episode·완료 및 cycle 범위/선행/Merge/종료 상태와 감사는 유지한다. 과거 outbox 조회로 통지 여부를 결정하던 경로도 새 사건을 재생성하지 않게 한다.
- [x] 확인한 `notifications/work_exhaustion.py`·`rendering.py`와 `tests/support/notification.py`의 기존 원인 표현·fixture를 재사용한다. 사용자의 실제 다음 행동과 근거를 정확히 표시하며 추정 권한 요청·가짜 해결 안내를 만들지 않는다. PRODUCT·ARCHITECTURE의 소유 알림 절에 구현한 생성/전송 계약만 통합한다.

## Validation

- Worker는 `tests/test_notification_events.py`·`test_notification_delivery.py`·`test_notification_incidents.py`·`test_notification_work_cycles.py`·`test_maintenance_state.py`의 변경 관련 최소 unit case를 실행한다. 실제 SQLite·사건 선택·claim/confirm을 사용하고 Telegram 응답과 외부 관측만 double로 제공한다.
- 현재 미해결 질문/인증 조치/실제 복구 불가 중단은 1회 전송하고, retry 소진·다음 실행 재개·CPU/Git/DB/사용량 대기·자동 교정·정상 진행/완료는 0회인지 확인한다. 동일 상태 코드에서 원인과 남은 조치가 다른 경우를 비교하며 enum 반환값만 mock해 판정을 대신하지 않는다.
- 구정책 pending·이미 일부 조각 전송·claim 뒤 원인 해소·전송 실패 뒤 재시작·confirm callback 뒤 재개 생성 사례에서 미전송분 취소와 cursor/payload/attempt/error 보존을 확인한다. 다음 조각 직전 최신 근거가 없으면 전송하지 않아야 한다.
- 과거 시작/재개/maintenance 메시지만 요구하던 assertion은 현재 무통지로 교체한다. 실제 process 시작·cycle 범위·Merge 반영·종료·프로젝트 완료와 outbox 감사의 assertion은 유지한다. 대체 없는 fixture/보존 검증 삭제나 전체 테스트 skip은 하지 않는다.
- 알림 비활성/실패·DB/전송 경합에서도 작업 상태·외부 효과 수·필수 성공 근거가 달라지지 않아야 한다. 선택 adapter는 unittest version 1·launcher Python 3.11–3.14이며 기존 수집·필수 compile·Agent 호출 차단을 유지한다.

## Progress

- [x] 2026-10-08 기준 0e887794의 R-053·R-074, cycle 사건 작성·selection·종료·rendering과 신규 유지보수 알림 테스트를 정적으로 대조했다. 현재 payload/rendering의 범위 누락을 확인했으며 구현·테스트 실행은 하지 않았다.
- [x] revision `4aafffa5b3af1b3a378a93750c7d86292891b5df`의 활성 명세 전문과 생성·incident·delivery/maintenance 경로를 정적으로 대조했다. 기존 유지보수 범위 표시 대신 현행 무통지와 공통 조치 판정의 남은 작업을 확정했다. 실행 검증은 하지 않았다.
- [x] 공통 생성/조각 전송 판정과 구정책 큐·callback 정리
- [x] 운영 상태/감사 보존과 최소 targeted 회귀

### 구정책 조각 전송 정리 checkpoint

`delivery._claim_piece`는 진행·완료·대기 등 허용 사건 종류 밖의 기존 outbox 잔여 조각을 취소한다. 일반 dispatch와 즉시 전송, 재시작 뒤 재시도 및 이미 조회한 행의 재사용에도 같은 경계를 적용한다. 취소 시 payload·고정 본문·확인 cursor·attempts·last_error와 작업 상태는 보존하고 claim 표식만 해제한다. 기존 persistent failure의 현재 block 대조·진단 억제 처리는 유지한다.

- 실행: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.NotificationDeliveryTest.test_legacy_progress_pieces_are_canceled_with_original_audit_after_restart tests.test_notification_delivery.NotificationDeliveryTest.test_immediate_progress_delivery_uses_same_cancellation_policy tests.test_notification_delivery.NotificationDeliveryTest.test_stale_selection_cannot_claim_canceled_progress tests.test_notification_delivery.NotificationDeliveryTest.test_pending_targeted_failure_notices_are_canceled_without_losing_sent_piece_audit`
- 결과: 4개 test 통과. 첫 test는 구정책 사건 9종의 미전송/부분 전송을 실제 SQLite 재연결 뒤 확인한다. `git diff --check`도 통과했다. 전체 verify는 실행하지 않았다.
- 남은 작업: 생성 단계 무통지, blocked의 최신 원인·실제 조치/복구 불가 근거 판정, PR·draft 공통 현재성 경계, 취소 사건을 부활시키는 planning/시작/재개 callback 정리, 알림과 독립적인 완료·cycle·시작 상태 보존, PRODUCT·ARCHITECTURE 반영과 기존 진행/재개 전송 테스트의 명세 전환. 이번 변경만으로 사건 종류가 허용된 block의 실제 조치 필요성을 보장하지 않는다. 과거 진행·재개 전송을 기대하는 기존 테스트는 아직 전환하거나 실행하지 않았다.


### 진행 생성 제거와 운영 상태 보존 checkpoint

`events.enqueue_progress`는 draft·PR 종류만 허용하며 일반 계획 게시와 Worker 최초 시작의 enqueue 및 시작 알림용 과거 outbox 조회를 제거했다. 완료 gate를 통과하면 Telegram 설정과 독립적으로 완료 cycle·시각을 기록하고 마지막 Merge 계획은 보존한다. 같은 완료 cycle의 시각은 덮지 않으며 누락된 시각은 채운다. 대기 snapshot 지문·세대·작업 가능 전이는 유지하고 기존 대기 잔여 조각만 취소한다. 유지보수의 알림 전용 helper·목록 조회·enqueue를 제거하되 cycle 요청·범위·실제 Merge·종료와 rollback 조건은 유지한다.

PRODUCT의 완료·대기와 Telegram 절, ARCHITECTURE의 완료 저장·Telegram 계약을 실제 변경에 맞췄다. 아직 남은 시작/재개 callback의 과거 사건 생성과 사건 종류 허용 뒤의 기존 현재성 검사도 ARCHITECTURE에 구분했다. 기존 진행 메시지 본문을 요구하던 테스트는 무통지와 실제 상태·snapshot·재시작·감사 assertion으로 전환했다. `TimelyProgressDeliveryTest`의 폐기된 stage_failed 부분 전송 재시도 시나리오는 제거했다. 같은 감사 보존 조건은 첫 checkpoint의 `test_legacy_progress_pieces_are_canceled_with_original_audit_after_restart`가 stage_failed를 포함한 9종에 대해 실제 재연결·payload·text·cursor·attempts·last_error를 검사하며, transport의 명시적 무음 flag 검사는 유지한다.

실행 기록:

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events tests.test_notification_work_cycles`: 46개 통과. 이후 수정한 생성 종류 행렬·시작·완료 시각 검사는 아래 최소 case로 추가 확인했다.
- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.TimelyProgressDeliveryTest tests.test_notification_delivery.DraftListNotificationTest.test_publication_current_and_waiting_share_one_sorted_list tests.test_notification_work_cycles.WorkReasonOutboxTest.test_waiting_snapshot_and_episode_survive_restart_without_notice`
- 원인: draft 목록 테스트에 남은 `rows[1]`의 구정책 plans_published 기대가 IndexError를 발생시켰다. 현재 단일 draft 목록과 대기 episode 기록 검사로 수정했다.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.TimelyProgressDeliveryTest tests.test_notification_delivery.DraftListNotificationTest.test_publication_current_and_waiting_share_one_sorted_list tests.test_notification_work_cycles.WorkReasonOutboxTest.test_waiting_snapshot_and_episode_survive_restart_without_notice`
- 재실행 결과: 4개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events.TimelyProgressEventTest.test_progress_kinds_never_create_even_silent_outbox_rows tests.test_notification_events.TimelyProgressEventTest.test_selection_is_silent_and_actual_start_is_durable_and_deduplicated tests.test_notification_work_cycles.CompletionCycleTest.test_same_and_archive_only_cycle_preserve_completion_and_new_input_updates_it`: 3개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_maintenance_state.MaintenanceStateRepositoryTest.test_due_is_project_scoped_and_success_baseline_advances_only_on_success tests.test_maintenance_state.MaintenanceStateRepositoryTest.test_legacy_post_completion_requires_merge_and_preserves_review_source tests.test_notification_work_cycles.CompletionCycleTest.test_same_and_archive_only_cycle_preserve_completion_and_new_input_updates_it`: 3개 통과. 마지막 case는 누락 시각 채움도 확인했다.
- 전체 verify와 전체 suite는 실행하지 않았다.

남은 구현은 blocked의 최신 원인·구체적 조치/실제 복구 불가 근거를 생성·각 claim에 연결하는 공통 판정, PR·draft의 기존 관측과 공통 현재성 경계, planning 취소 사건 부활 및 시작/재개 callback enqueue 제거와 해당 incident·전송 경합 테스트 전환이다. 기존 final_evidence는 retry/교정 소진도 USER_ACTION_REQUIRED/FINAL_BLOCK로 분류하므로 그 이름이나 user_targets의 존재만으로 통지를 허용하지 않아야 한다. 현재 PRODUCT의 최종 차단 규칙과 ARCHITECTURE의 기존 차단 검사 설명은 이 남은 구현 뒤 최종 계약으로 다시 통합해야 한다. 이번 checkpoint는 완료 후보가 아니다.


### 시작·재개 callback 무통지 checkpoint

`record_work_start`는 Telegram 활성 여부와 무관하게 기존 incident의 실제 operation·첫 시작 시각·identity·대상 계획을 보존한다. `_materialize_resumption`은 기존 통지 확인·원인 해제·실제 시작의 연결과 `resumed_at`만 확정하고, 반환값은 PR 재개 운영 로그가 사용하는 새 확정 수로 유지했다. 재개 payload와 무통지 차단의 시작 payload를 생성하던 경로를 제거했다. 실패 callback과 시작 복구는 새 메시지를 만들지 않으며 늦은 전송 확인은 기존 조각 cursor·확인 감사만 전진시킨다. PRODUCT·ARCHITECTURE의 시작/재개 계약을 실제 구현에 맞춰 갱신했다.

기존 사건 테스트는 메시지 payload·건수를 요구하던 assertion을 해당 원본 incident payload·operation·첫 시작 시각과 새 시작/재개 outbox 0건으로 전환했다. 확인된 차단·미확인 차단·일부 전송·동일 원인 재발·기존 legacy 통지 채택·계획/프로젝트/planning 구분·다른 계획/독립 planning/안전 gate·PR 재개·재시작의 실제 SQLite 시나리오는 유지한다. 초기 재시작 검사는 scheduler·transport만 double로 제공하며 취소 전후 payload·고정 본문·cursor·attempt/error와 시작 identity·시각 보존을 확인한다. 테스트·fixture를 삭제하거나 skip하지 않았다.

- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_incidents`
- 첫 실행 결과: 38개 실행, 구정책 재개/시작 메시지 기대 및 전환 중인 반환값 계약으로 failures 24·errors 7. callback 내부 assertion 오류도 원래 transport 실패 기록으로 남았다. 실제 조각·incident 검사를 보존해 무통지 기대를 반영하고 반환값을 통지 enqueue와 독립적인 기존 확정 수로 정리했다.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_incidents`
- 재실행 결과: 38개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.InterruptedBlockStartupTest tests.test_notification_events.TimelyProgressEventTest.test_selection_is_silent_and_actual_start_is_durable_and_deduplicated`: 3개 통과.
- 이후 최종 확정 수를 Telegram 활성 여부와 독립화한 변경은 `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_incidents.NotificationOutboxTest.test_disabling_telegram_after_notice_suppresses_late_resumption tests.test_notification_incidents.NotificationOutboxTest.test_reopened_pr_records_resumption_in_transition`로 확인했다: 2개 통과.
- `git diff --check` 통과. 전체 verify는 실행하지 않았다.

남은 작업은 blocked의 최신 원인·구체적 사용자 행동/실제 복구 불가 근거를 생성과 각 claim에 연결하는 공통 정책, PR·draft의 기존 관측과 공통 현재성 경계, planning 취소 사건을 되살리는 `_carry_planning_delivery`·`_queue_block`의 구정책 복원 제거, 해당 생성/조각 검증 및 최종 설계 문서 통합이다. 현재 원인별 action 근거가 없는 일반 block/planning_error도 여전히 생성될 수 있으며, 해당 상태 이름·final_evidence의 분류만으로는 실제 조치/복구 불가를 입증하지 못한다. 이 checkpoint는 전체 구현 완료 후보가 아니다.


### 공통 현재 조치 판정과 planning 복원 제거 checkpoint

`notifications/policy.py`를 생성·각 조각 claim의 공통 경계로 연결했다. 현재 block 대상·원인·release fingerprint와 recovery의 pending action·최종 근거·현재 revision을 대조하고, 실제 Worker 질문·허용 문서의 SPEC_DEADLOCK·현재 pending 지침 승인·같은 닫힌 PR·게시 branch 삭제의 명시적 재시작·기록된 verify 상한 변경 근거만 선택한다. 새로운 blocked payload의 선택적 action snapshot은 현재 조치와 다시 비교한다. 기존 payload는 현재 원본으로 확인할 수 있으면 재작성 없이 사용한다. PR·draft도 같은 경계에서 기존 상태/질문 관측을 재사용하며 Git 쓰기·외부 호출을 추가하지 않았다.

사건 생성은 이전 pending을 취소한 뒤 실행하며, 조각 전송은 남은 조치가 없거나 복구가 pending이면 원문·고정 본문·cursor·attempt/error를 보존하고 취소한다. planning_error 생성, `_queue_block`의 과거 planning 재활성화와 missing outbox 재생성, `_carry_planning_delivery`의 canceled/released 복원은 제거했다. 첫 확인 원인과 legacy 감사 연결은 유지한다. 폐기된 stage_failed hook은 호환용 무통지 함수로 남기고 불필요한 실패 투영 조회를 없앴다. PRODUCT·ARCHITECTURE에는 현재 판정과 action 저장 계약을 반영했다.

공유 `NotificationFixture.decision_block`는 실제 Worker 결과 파서와 recovery·block·checkpoint 저장소를 통해 구체적 질문을 만든다. 새 CurrentActionPolicyTest는 같은 상태의 근거 유무, retry/targets만 있는 최종 분류의 무통지, 부분 전송 중 pending 복구/조치 해소, 질문 대체, planning 취소 보존, PR 완료, 재시작 소비, 손상 근거/revision 변경과 구체적 PR/상한 조치를 실제 SQLite로 검사한다. 외부 전송만 double로 제공한다. 기존 Unicode incident/payload 보존 case도 이 실제 질문 fixture로 전환했다.

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest`: 당시 7개 통과. 이후 추가 case는 아래 최소 단위로 실행했다.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events.NotificationEventTest.test_guidance_block_reports_exact_decision_lines_and_recovery_attempts tests.test_notification_events.NotificationEventTest.test_specification_deadlock_notification_names_documents_and_question tests.test_notification_delivery.DraftListNotificationTest`: 12개 통과.
- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_deleted_published_branch_requires_restart_until_request_is_recorded tests.test_notification_delivery.CurrentActionPolicyTest.test_invalid_recovery_evidence_and_changed_revision_preserve_unsent_audit tests.test_notification_delivery.CurrentActionPolicyTest.test_same_status_needs_concrete_current_question_before_one_notice tests.test_notification_delivery.CurrentActionPolicyTest.test_pr_claim_requires_current_merge_wait_for_same_pr`
- 첫 결과: 4개 실행 중 재시작 소비 fixture 1개 실패. checkpoint 행이 없는데 UPDATE만 수행해 소비 사실을 저장하지 못했다. 실제 ExecutionCheckpointRepository.record로 수정했다.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_deleted_published_branch_requires_restart_until_request_is_recorded tests.test_notification_delivery.CurrentActionPolicyTest.test_invalid_recovery_evidence_and_changed_revision_preserve_unsent_audit tests.test_notification_delivery.CurrentActionPolicyTest.test_same_status_needs_concrete_current_question_before_one_notice tests.test_notification_delivery.CurrentActionPolicyTest.test_pr_claim_requires_current_merge_wait_for_same_pr`
- 재실행 결과: 4개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_closed_pr_and_recorded_verify_limit_expose_only_their_current_action`: 1개 통과(두 조치 subcase).
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events.NotificationEventTest.test_targeted_worker_failure_retains_recovery_history_without_notifications tests.test_notification_events.TimelyProgressEventTest.test_review_failure_preserves_state_without_notification tests.test_notification_events.TimelyProgressEventTest.test_review_protocol_failure_and_candidate_changes_do_not_notify`: 3개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_incidents.IncidentPayloadEncodingTest tests.test_notification_delivery.CurrentActionPolicyTest.test_invalid_recovery_evidence_and_changed_revision_preserve_unsent_audit`: 2개 통과.
- `scripts/verify-inputs.json`의 기존 loop_manager_internal/**·tests/**가 새 policy와 변경 테스트를 포함한다. 전체 verify는 실행하지 않았다.

남은 작업은 기존 notification events/delivery/incidents 테스트의 상태명-only/일반 planning_error 생성 fixture와 assertion을 현행 구체적 조치·legacy 감사 fixture로 전환하고 필요한 최소 회귀를 실행하는 것이다. 해당 기존 모듈 전체는 이번 호출에서 실행하지 않았으며 통과로 간주하지 않는다. 조치가 없는 실제 복구 불가 중단을 확정하는 근거 경로와 1회 통지 검증도 아직 확인하지 않았다. 현재 저장된 일반 final disposition만으로는 이를 증명하지 못하므로 보류하며 가짜 사용자 행동이나 복구 불가 선언을 만들지 않는다. 최종 원인별 정책·외부 관측/현재 설정 연결의 충분성을 점검하고 문서·최종 diff 보고를 통합해야 한다. 이번 결과는 구현 완료 후보가 아닌 보존점이다.

### 최종 구현 후보와 targeted 검증

실제 Worker 질문·명세 교착·지침 승인·PR 재개·게시 branch 재시작·현재 verify 상한 조치를 기존 원본에 연결했다. 상한은 scheduler·recover·독립 task 연결이 전달한 현재 비밀값 없는 TEMP 설정과 대조하며, 미확인/변경 값에서는 과거 요청을 취소한다. 복구 소유자가 `RecoveryObservation.unavailable_recovery_routes`로 재관측·재시도·교정·조건 대기·다음 실행·안전 복구 각각의 불가 근거를 명시할 수 있게 했다. 기존 final 분류·실패 횟수로 이 근거를 합성하지 않고 필드가 없는 기존 기록은 보류한다. 실제 SQLite에서 완전한 근거가 있는 프로젝트 최종 중단 1회 전송과 retry/next_run 근거 누락 시 0회 전송을 확인했다. 조치 없는 중단에는 가짜 사용자 질문을 만들지 않는다. 복구 정책·예산·영속 DB schema와 외부 실행은 변경하지 않았다.

첫 확인 incident 원문을 보존한 채 질문이 바뀌는 경우의 중복 판정은 현재 outbox payload를 사용한다. 재투영·전송 후에도 새 질문을 다시 생성하지 않으며 첫 통지 원문은 유지한다. PRODUCT의 목표·불변식·완료/대기·Telegram 절과 ARCHITECTURE의 완료 저장·공통 판정·선택적 근거/설정·전송/시작 계약을 구현과 대조했다.

일반 planning_error 통지 생성·재활성화만 요구하던 10개 테스트는 제거하고 `LegacyPlanningPolicyTest` 두 case로 통합했다. 기존 planning notice를 명시적으로 seed해 미전송/부분 전송/이미 전송, 같은·변경·해소된 원인, 실제 SQLite 재연결, 중복 active/released incident·첫 확인 원문·실제 Planner 시작을 검사한다. payload·frozen text·cursor·시각·attempt/error·취소/확인 감사 assertion은 유지한다. 삭제 대상은 `test_inflight_planning_piece_confirmed_after_start_still_resumes`, `test_released_partial_planning_notice_retries_original_remaining_piece`, `test_same_second_planner_start_closes_unsent_planning_episode`, `test_released_planning_duplicates_keep_delivered_notice_not_unsent_latest`, `test_release_of_duplicate_active_planning_incidents_resumes_once`, `test_planning_incident_survives_database_reopen_and_repeated_observation`, `test_historical_planning_duplicates_keep_one_incident_until_real_restart`, `test_prior_planning_notice_does_not_notify_new_error_with_reused_key`, `test_unsent_legacy_planning_notice_is_canceled_before_new_error_dispatch`, `test_identical_planning_error_after_later_planner_start_is_new_incident`이다. 일반 project safety 알림을 요구하던 테스트는 통지 0건과 실제 diagnostic ref/OID/local-only 이유·worktree 보존 검사로 전환했다. 전송·incident의 유효 조치 fixture는 실제 Worker 결과 파서와 recovery/block/checkpoint 저장소를 사용하며 PR·draft 역시 현재 관측을 설정한다. 고정 본문 재시도는 identity를 유지한 표시 변경으로 검증한다. 관계가 없어진 import도 제거했다.

이번 호출의 실행 기록(모두 공통 targeted runner 사용):

- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events tests.test_notification_delivery tests.test_notification_incidents > /tmp/g226-notification-regressions.log 2>&1`
- 원인/수정: 초기 123개에서 폐기된 정책/fixture 기대 failures 27·errors 23. 위 fixture 전환과 legacy 통합 뒤.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events tests.test_notification_delivery tests.test_notification_incidents > /tmp/g226-notification-regressions.log 2>&1`
- 재실행 결과: 116개 통과.
- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events tests.test_notification_delivery.SegmentedDeliveryTest tests.test_notification_delivery.InterruptedBlockStartupTest tests.test_notification_incidents.LegacyPlanningPolicyTest > /tmp/g226-migrated-fixtures.log 2>&1`
- 원인/수정: 53개에서 failure 1·error 1: frozen payload의 identity 삭제와 게시 전 질문 근거 누락을 수정한 뒤.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_events tests.test_notification_delivery.SegmentedDeliveryTest tests.test_notification_delivery.InterruptedBlockStartupTest tests.test_notification_incidents.LegacyPlanningPolicyTest > /tmp/g226-migrated-fixtures.log 2>&1`
- 재실행 결과: 53개 통과.
- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_explicit_route_assessment_allows_unrecoverable_stop_without_fake_user_action tests.test_notification_incidents > /tmp/g226-incidents-migrated.log 2>&1`
- 원인/수정: 31개에서 failures 4·errors 2: 남은 legacy/질문/PR 관측 fixture 및 내부 key import를 수정한 뒤.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_explicit_route_assessment_allows_unrecoverable_stop_without_fake_user_action tests.test_notification_incidents > /tmp/g226-incidents-migrated.log 2>&1`
- 재실행 결과: 31개 통과.
- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_current_limit_change_or_missing_settings_cancel_old_configuration_action tests.test_notification_delivery.CurrentActionPolicyTest.test_closed_pr_and_recorded_verify_limit_expose_only_their_current_action tests.test_notification_delivery.NotificationDeliveryTest.test_registered_project_wires_immediate_delivery_after_commit tests.test_notification_delivery.NotificationDeliveryTest.test_scheduler_retries_pending_when_project_processing_is_skipped`
- 원인/수정: 4개에서 failure 1: Mock 설정값이 SQLite에 전달돼 scheduler dispatch가 생략됨. 세션 경계에서 양의 정수 외에는 미확인 NULL로 정규화한 뒤.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_current_limit_change_or_missing_settings_cancel_old_configuration_action tests.test_notification_delivery.CurrentActionPolicyTest.test_closed_pr_and_recorded_verify_limit_expose_only_their_current_action tests.test_notification_delivery.NotificationDeliveryTest.test_registered_project_wires_immediate_delivery_after_commit tests.test_notification_delivery.NotificationDeliveryTest.test_scheduler_retries_pending_when_project_processing_is_skipped`
- 재실행 결과: 4개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_work_cycles`: 8개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_task_scheduling.TaskSchedulingTests.test_project_dispatch_overlaps_independent_workers_and_keeps_verify_serial tests.test_task_scheduling.TaskSchedulingTests.test_independent_block_rechecks_use_target_ownership_and_one_recovery_lease`: 독립 실행/차단 재검사 연결의 2개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest tests.test_local_environment_recovery.LocalEnvironmentRecoveryCommandTests.test_recovery_run_reaches_timeout_recheck_without_pr_sync`: 최종 조치 정책 13개와 recover 경로 1개, 총 14개 통과. 첫 확인 후 질문 교체 중복 제거도 포함한다.

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_incidents > /tmp/g226-final-incidents.log 2>&1`: 현재 outbox 기반 중복 판정 변경 뒤 incident/legacy/시작·확인 경합 30개 통과.
- 최종 `git diff --check` 통과.

앞선 checkpoint의 유지보수 cycle/실제 Merge/완료 시각 검증은 유지한다. 전체 suite·integration·전체 verify는 Worker에서 실행하지 않았다. `scripts/verify-inputs.json`의 기존 입력 glob이 변경 코드·테스트·문서를 포함한다. Manager의 전체 verify·독립 Review·사용자 Merge가 남아 있으며 이 기록은 구현 완료 후보의 targeted 검증만 뜻한다.

### 전체 verify 피드백 교정 (c4ee5079)

Manager verify `40c823a82a0f6b805073d22232c0f7ee4ce0458c541f080c042a1b981f8f0555` occurrence=1/revision=1은 c4ee507935f2524e4a963a59160c0e4b32938b93에서 종료 코드 1이었다. summary의 실제 결과는 success 4492·failure 15·error 3·skip 7이며 성공으로 바꾸어 기록하지 않는다. 실패/오류 18개 방법 전체를 조회해 관련 원인부터 교정했다.

네이티브 Worker 질문의 recovery fingerprint는 실패 원인 hash이고 block release fingerprint는 원격 입력 지문이어서 서로 다르다. 기존 공통 정책의 값 일치 조건 때문에 실제 미해결 질문이 억제되던 오류를 수정했다. 기존 `RecoveryObservation.identity`로 recovery 필드의 무결성을 재검증하고 현재 대상·원문 원인·최종 근거·revision 검사는 유지한다. 실제 Worker 파서→복구 판단→차단→checkpoint→SQLite 재연결 3회의 기존 시나리오가 통지 두 버전·checkpoint·실패 이력을 보존하는지 확인했으며, 두 지문이 실제로 다르다는 assertion을 추가했다. 변조한 recovery fingerprint는 전송하지 않고 원문/감사를 보존한다. ARCHITECTURE의 지문 계약도 해당 의미로 교정했다.

폐기된 진행/재개 통지를 기대하던 검사는 실제 시작 ledger·incident started_at/resumed_at·보존 원문과 새 통지 0건을 검사한다. planning 해제/교정의 원자성은 구정책 incident를 명시적으로 seed해 rollback·다른 사건 보존·정상 해제를 계속 검사한다. checkpoint/outbox 원자성과 scope-owned 해제 rollback에는 실제 파싱한 사용자 질문 근거를 공급한다. checkpoint 검사는 앞선 outbox를 삭제하지 않고 새로운 질문의 추가 1건 및 중단/재시도 원자성을 검사한다. 일반 review 소진·Git 설정 오류의 fingerprint/scope 보존, diagnostic receipt의 local-only 오류, 안정적 wait episode, 원격 OID·rebase 충돌·재검사 원인/실패 이력은 알림 없이 보존한다. 구정책 stage_failed의 과거 전송 실패는 본문·확인 cursor·attempt/error를 seed하고 이후 실제 transport 호출 없이 취소·감사가 유지되는지 확인한다. 설정 전달 전용 Mock DB 테스트는 notification 세션 경계를 double로 제공하고 현재 timeout=42 전달을 명시적으로 검사한다. 테스트 삭제·skip·실패 근거 제거는 하지 않았다.

실행 기록:

- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_worker_decision_checkpoint_wait_survives_three_restarted_runs tests.test_managed_configuration.ManagedConfigurationAndProjectRegistryTest.test_scheduler_passes_configured_verify_timeout_to_project_worker > /tmp/g226-feedback-probe.log 2>&1`
- 결과: failure 1·error 1. 실제 질문 fingerprint 억제와 세션 SQL을 받을 수 없는 Mock DB를 재현했다.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_worker_decision_checkpoint_wait_survives_three_restarted_runs tests.test_managed_configuration.ManagedConfigurationAndProjectRegistryTest.test_scheduler_passes_configured_verify_timeout_to_project_worker > /tmp/g226-feedback-probe.log 2>&1`
- 재실행 결과: 원인/fixture 교정 뒤 2개 통과.
- FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_invocation.ProcessStartNotificationTests tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest.test_terminal_result_and_outbox_rollback_and_retry_together tests.test_diagnostic_preservation.PreservationBoundaryTests.test_unavailable_artifact_receipt_replaces_notice_once_across_retries tests.test_execution_failure_policy.ExecutionFailurePolicyTests.test_project_resume_is_recorded_when_execution_phase_starts tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_worker_return_preserves_legacy_pending_event_after_delivery_error tests.test_scope_owned_publication.OwnedScopePublicationRecoveryTests.test_releasing_notified_plan_block_rolls_back_notice_cancellation tests.test_scoped_blocking.ScopedBlockingGateTests.test_block_notifications_distinguish_new_fingerprint_and_scope tests.test_stable_work_reason.StableWorkReasonBoundaryTest.test_stable_result_reports_reason_and_pause_skips_notification tests.test_planning_selection.PlanningSelectionTest.test_real_planner_port_records_resumption_at_agent_start tests.test_runtime_state.RuntimeStateRepositoryTest.test_planning_error_release_rolls_back_with_incident_on_interruption tests.test_runtime_state.RuntimeStateRepositoryTest.test_planning_correction_releases_only_bound_incident_atomically tests.test_notification_delivery.CurrentActionPolicyTest > /tmp/g226-feedback-units.log 2>&1`
- 결과: 27개 중 failure 1·errors 2. checkpoint fixture의 동일 질문/outbox 삭제, invocation의 잘못된 id 컬럼, legacy helper의 schema 초기화 누락을 수정했다. 질문별 기존 감사는 유지하며 새 조치로 구분했다.
- RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_agent_invocation.ProcessStartNotificationTests tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest.test_terminal_result_and_outbox_rollback_and_retry_together tests.test_diagnostic_preservation.PreservationBoundaryTests.test_unavailable_artifact_receipt_replaces_notice_once_across_retries tests.test_execution_failure_policy.ExecutionFailurePolicyTests.test_project_resume_is_recorded_when_execution_phase_starts tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_worker_return_preserves_legacy_pending_event_after_delivery_error tests.test_scope_owned_publication.OwnedScopePublicationRecoveryTests.test_releasing_notified_plan_block_rolls_back_notice_cancellation tests.test_scoped_blocking.ScopedBlockingGateTests.test_block_notifications_distinguish_new_fingerprint_and_scope tests.test_stable_work_reason.StableWorkReasonBoundaryTest.test_stable_result_reports_reason_and_pause_skips_notification tests.test_planning_selection.PlanningSelectionTest.test_real_planner_port_records_resumption_at_agent_start tests.test_runtime_state.RuntimeStateRepositoryTest.test_planning_error_release_rolls_back_with_incident_on_interruption tests.test_runtime_state.RuntimeStateRepositoryTest.test_planning_correction_releases_only_bound_incident_atomically tests.test_notification_delivery.CurrentActionPolicyTest > /tmp/g226-feedback-units.log 2>&1`
- 재실행 결과: 27개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_checkpoint_remote_recovery.CheckpointCauseGitRevalidationTests.test_rewritten_remote_branch_enters_owned_conflict_without_discarding_commits tests.test_checkpoint_remote_recovery.CheckpointCauseGitRevalidationTests.test_remote_noncode_change_remains_blocked_and_records_inspection tests.test_checkpoint_remote_recovery.CheckpointCauseGitRevalidationTests.test_unrelated_code_keeps_cause_and_history_until_probe_passes tests.test_checkpoint_remote_recovery.CheckpointCauseGitRevalidationTests.test_fetch_failure_marks_remote_inspection_retryable > /tmp/g226-feedback-git.log 2>&1`: 실패한 네이티브 Git 원인 재검사 4개 통과. 원격 변경·충돌·일시 fetch 오류의 무통지와 실제 파일/HEAD/원인·재검사 이력 검증은 유지했다.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_worker_decision_checkpoint_wait_survives_three_restarted_runs tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_worker_return_preserves_legacy_pending_event_after_delivery_error`: 지문 도메인/legacy 실패 감사 assertion 보강 뒤 2개 통과.

최종 `git diff --check` 통과. 선택 ExecPlan frontmatter와 Requirement·다른 ExecPlan은 변경하지 않았다. Worker는 전체 verify를 재실행하지 않았으며 Manager의 새 후보 전체 verify·Review·Merge가 남아 있다.

### targeted 결과 보고 교정

같은 verify identity/occurrence/revision에 이어진 Worker publication 검증의 `Worker가 해결되지 않은 targeted unit test 실패를 보고했습니다.`는 새 코드 검증 결과가 아니라 이전 최종 `TARGETED_TESTS`의 이력 서술에서 발생했다. 해당 필드에 과거 결과를 뜻하는 “실패·오류 18개”와 “실패 명령”을 썼지만 기존 결과 파서는 FAILED_COMMAND/RERUN 없이 나타난 표현을 현재 미해결 결과로 처리한다. 판정 함수를 변경하거나 과거 결과를 삭제하지 않고 최종 보고에서 실제 현재 결과와 과거 실행 이력을 구분한다. 과거 명령·교정·재실행 기록은 위 절에 보존한다.

이전 호출 `call-1k8for6q/targeted-*.jsonl`의 실제 test_end를 ID별 마지막 관측 시각으로 집계해 최신 개별 결과 33개 모두 success임을 확인했다. 현재 파일에 추가 코드 수정은 없으며 이미 통과한 검사를 반복하지 않았다. 기존 `_has_unresolved_targeted_test_failure`에 이전 문구를 넣으면 True, 실제 통과 결과만 적은 교정 문구는 False임을 직접 호출과 assertion으로 확인했다. 이것은 publication 보고 판정 확인이며 새 unit 실행·전체 verify 통과를 뜻하지 않는다. Manager의 전체 verify 재실행·Review·Merge는 계속 대기한다.

### Review 교정: 취소 조치 재확인과 독립 scope

시작 HEAD 8ace07b83787cbdf722dfddc5a0fe0965d6447c4의 Review 피드백 `incidents.py::_queue_block::canceled-action-dedup`, `policy.py::_block_action::cross-scope-recovery-shadowing`(각 occurrence=1/revision=1)을 교정했다. 전달된 해당 후보의 Manager 전체 verify PASS는 이전 후보 근거이며 이번 수정의 전체 verify 결과로 간주하지 않는다.

`_queue_block`의 같은 block/action 중복 판정은 현재 outbox의 취소·전송 완료 상태도 확인한다. 유효한 조치를 재확인했고 이전 행은 취소됐지만 전체 전송이 확인되지 않았으면 기존 incident의 다음 version에 새 사건을 기록한다. 이전 canceled_at·payload·고정 본문·cursor·attempt/error는 그대로 유지한다. 같은 조치의 부분 전송은 새 사건에 기존 고정 본문·cursor를 승계해 확인된 조각을 반복하지 않으며, 전송 완료·미취소·이력 누락은 이 재생성 대상에서 제외한다. 구정책 planning 생성이나 canceled/released 행의 재활성화는 복원하지 않는다.

`block_recovery`는 최종 evidence의 코드 소유 차단 원인으로 scope를 판별해 다른 scope의 final recovery를 분리한다. 생성·정책·claim의 키 계산이 같은 관측 경계를 사용한다. 지침 승인·명세 교착의 사건 identity는 자체 승인/파싱 원문에 연결하고 공유 recovery identity를 제외해 독립 프로젝트 차단·해제에 따른 중복도 방지한다. 같은 대상의 pending recovery와 손상 근거는 기존 검증을 유지한다. Worker 질문 등 별도 최종 recovery 근거가 필요한 조치를 상태명만으로 허용하지 않는다. 복구 기록·실행 정책·schema를 바꾸거나 프로젝트 차단 해제 시 과거 recovery를 합성하지 않았다.

추가 회귀는 실제 SQLite와 질문·지침 파서를 사용하며 외부 Telegram만 double로 제공한다. 취소 질문의 미전송·부분 전송·이미 전송 완료를 비교하고 SQLite 파일을 닫아 재연결한 뒤 동일 recovery identity의 재확인, 기존 취소 행 전체 불변, 잔여 조각의 정확한 내용·건수와 반복 projection/dispatch 중복 방지를 검사한다. 승인 회귀는 pending·이미 통지·최초 비활성화 세 경우에서 같은 계획에 독립 프로젝트 안전 차단을 기록·해제한 뒤 승인 1회 전송을 확인한다. 승인 원본·계획 차단은 유지되고 공유 recovery에는 프로젝트 원인이 그대로 남아 있음을 확인한다. PRODUCT·ARCHITECTURE의 소유 절에 재확인·scope 분리 계약을 통합했다.

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest > /tmp/g226-review-actions.log 2>&1`: 15개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_incidents.NotificationOutboxTest tests.test_notification_events.NotificationEventTest > /tmp/g226-review-incidents-events.log 2>&1`: 사건 생성·incident·시작/확인 관련 29개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest.test_reconfirmed_canceled_action_gets_new_event_without_rewriting_old_delivery tests.test_notification_delivery.CurrentActionPolicyTest.test_guidance_action_survives_other_scope_recovery_and_project_release > /tmp/g226-review-restoration.log 2>&1`: SQLite 재연결과 최초 비활성화 subcase 보강 뒤 2개 통과.
- `git diff --check` 통과. 전체 verify는 Worker에서 재실행하지 않았다. Manager의 새 후보 전체 verify·독립 Review·Merge를 기다린다.

### 관리 rebase 뒤 현재 후보 확인

사용자가 수용한 현재 HEAD f1e2dc05f13ddb2cbe4ce621d3c6b709f8ba6a71과 base 4af7b49a6c91a60e3986ee4bad711579288b9e1e(origin/main, merge-base 일치)을 기준으로 재검토했다. 선택 ExecPlan의 현재 sources·AC와 R-053의 전송 허용/금지·구정책 큐 보존, R-055의 차단/통지 분리, R-074의 cycle 범위·Merge·종료 보존을 다시 대조했다. 선언된 G-265/266/267/268은 동일 ID의 archive 파일로 확인했다. adapter는 현재 scripts/verify.json과 docs/VERIFY.md의 unittest v1/launcher 계약을 유지한다.

base 대비 HEAD의 최종 diff 33개 파일 전체를 대상으로 생성·정책·조각 claim/confirm·시작 callback·독립 scope·현재 설정 전달·복구 근거·완료/대기/maintenance 상태와 테스트 fixture/기대값 전환을 대조했다. 문서는 PRODUCT·ARCHITECTURE·선택 ExecPlan만 바뀌며 Requirement·다른 ExecPlan·verify 선언/스크립트·지침 원본과 새 루트 항목의 변경은 없다. 제거한 진행/planning 통지 전용 검사의 감사·실제 시작·보존 검증은 위 대응 회귀에 유지된다. 추가 구현 교정은 필요하지 않았으며 이번 호출은 이 검토·현재 후보 실행 기록만 본문에 남긴다. frontmatter와 과거 실행/판단 이력은 유지한다.

이전 후보의 성공 결과를 재사용하지 않고 현재 HEAD에서 아래 최소 unit 검사를 새로 실행했다.

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest tests.test_notification_events.TimelyProgressEventTest.test_progress_kinds_never_create_even_silent_outbox_rows tests.test_notification_work_cycles.CompletionCycleTest.test_same_and_archive_only_cycle_preserve_completion_and_new_input_updates_it tests.test_notification_work_cycles.WorkReasonOutboxTest.test_waiting_snapshot_and_episode_survive_restart_without_notice tests.test_maintenance_state.MaintenanceStateRepositoryTest.test_due_is_project_scoped_and_success_baseline_advances_only_on_success tests.test_maintenance_state.MaintenanceStateRepositoryTest.test_legacy_post_completion_requires_merge_and_preserves_review_source tests.test_agent_invocation.ProcessStartNotificationTests tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest.test_terminal_result_and_outbox_rollback_and_retry_together tests.test_diagnostic_preservation.PreservationBoundaryTests.test_unavailable_artifact_receipt_replaces_notice_once_across_retries tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_worker_decision_checkpoint_wait_survives_three_restarted_runs tests.test_notification_delivery.NotificationDeliveryTest.test_registered_project_wires_immediate_delivery_after_commit tests.test_notification_delivery.NotificationDeliveryTest.test_scheduler_retries_pending_when_project_processing_is_skipped tests.test_scope_owned_publication.OwnedScopePublicationRecoveryTests.test_releasing_notified_plan_block_rolls_back_notice_cancellation > /tmp/g226-rebased-targeted.log 2>&1`: 30개 통과, 4.276초. 조치 정책 15개와 생성 금지 행렬·완료/대기·maintenance 상태·실제 시작·checkpoint 원자성·진단 보존·네이티브 Worker 질문·scheduler 전송 연결을 포함한다.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.NotificationDeliveryTest.test_legacy_progress_pieces_are_canceled_with_original_audit_after_restart tests.test_notification_delivery.InterruptedBlockStartupTest tests.test_notification_incidents.LegacyPlanningPolicyTest tests.test_notification_work_cycles.WorkReasonOutboxTest.test_disabled_wait_transition_cancels_legacy_notice_and_preserves_audit > /tmp/g226-rebased-legacy.log 2>&1`: 6개 통과, 21.997초. 구정책 9종의 미전송/부분 전송과 SQLite 재연결, 시작 복구·planning 중복 취소·대기 감사 보존을 확인했다.

base 대비 HEAD와 최종 작업 diff의 `git diff --check`도 통과했다. 두 실행은 모두 이번 호출의 targeted runner 결과이며 전체 verify·integration·전체 suite는 실행하지 않았다. 문서 patch 보고는 이전 d0ed33bb 기준 hash를 재사용하지 않고 현재 base 4af7b49a 기준으로 다시 계산한다. Manager의 현재 후보 전체 verify·Review·Merge가 남아 있다.

### Review 교정: 늦은 확인 계보와 Worker 질문의 scope 보존

시작 HEAD b3ccbd9db7751fecf4fc581b147b5717f03a533a에서 `incidents.py::_queue_block::reconfirmed-inflight-cursor` occurrence=1/revision=1과 `policy.py::_block_action::cross-scope-recovery-shadowing` occurrence=2/revision=2를 교정했다. 앞선 지침 승인 전용 scope 검사는 Worker 질문의 최종 근거 보존을 보장하지 못했다.

같은 조치를 재확인해 만든 새 payload에는 직전 event_key를 `continued_from`으로 기록한다. 성공 callback은 원래 조각 확인과 같은 transaction에서 동일 고정 본문을 이어받은 후속 version 계보의 cursor와 전체 완료도 전진시킨다. 두 번 이상 재확인해도 이미 확인한 조각을 다시 보내지 않으며, 질문이 바뀐 version에는 계보를 연결하지 않아 원래 확인이 새 질문으로 새지 않는다. 원래 취소 행·payload·실패 감사는 보존하고 잘못된 JSON payload는 계보 조회에서 제외한다. 확인 전 외부 수신 불명 상태의 기존 중복 가능성 계약은 유지한다.

계획 차단에는 nullable `recovery_snapshot` JSON 컬럼을 호환 추가했다. `RuntimeStateRepository.block_plan`은 현재 원인과 일치하는 최종 recovery의 identity·revision·원문/Worker 결정 근거를 차단·checkpoint와 같은 transaction에 보존한다. 공유 per-plan recovery가 독립 프로젝트 최종 차단으로 덮이거나 없어지면 이 소유 snapshot을 읽는다. 공유 같은-scope pending/손상 근거는 여전히 우선하며 snapshot으로 검증을 우회하지 않는다. 현재 원인·scope·revision·identity를 재검사하고 block 교체/삭제 시 근거도 함께 교체/삭제한다. 기존 행은 보존하며 NULL인 과거 근거를 자동 합성하지 않는다. 복구 예산·실행·공유 recovery의 소유 정책은 바꾸지 않았다. PRODUCT·ARCHITECTURE의 소유 알림 절에 이 호환·보존 계약을 반영했다.

추가 실제 SQLite 회귀는 원래 HTTP 진행 중 취소/재확인 두 번→원래 성공 callback→다음 dispatch 순서를 단일/다중 조각과 다른 질문으로 비교한다. Worker 질문은 미전송·이미 전송·최초 비활성화와 revision 변경을 비교하고 프로젝트 차단 해제 뒤 DB 재연결에도 원래 질문 1회 통지·revision 변경 시 억제·공유 프로젝트 recovery 유지·계획 질문 snapshot 보존을 검사한다. 구형 schema의 반복 migration도 기존 원문/행을 유지하며 nullable 값만 추가하는지 확인했다.

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest > /tmp/g226-review-late-scope.log 2>&1`: 18개 통과.
- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest.test_plan_block_and_checkpoint_request_share_one_transaction tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest.test_terminal_result_and_outbox_rollback_and_retry_together tests.test_runtime_state.RuntimeStateRepositoryTest.test_nonfinal_recovery_decision_cannot_enter_block_transaction tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_worker_decision_checkpoint_wait_survives_three_restarted_runs tests.test_notification_delivery.SegmentedDeliveryTest tests.test_notification_delivery.CurrentActionPolicyTest.test_old_block_schema_adds_nullable_proof_without_changing_existing_rows > /tmp/g226-review-storage-callback.log 2>&1`: fresh/migrated schema 컬럼 순서 정합화 뒤 15개 통과. 차단/checkpoint rollback·최종 판단 경계·네이티브 Worker 질문·분할 전송/실패 재시도를 포함한다.
- `git diff --check` 통과. 전체 verify는 Worker에서 실행하지 않았다. Manager의 새 후보 전체 verify·Review·Merge가 남아 있다.

### 재수용한 rebase 후보의 Review 교정 확인

시작 HEAD 1bf4b689c062391c0a40b3cf266098d44f0355d9와 base 85e7fe732e3ba6bee0af1421c54dc3211a1da8b3을 확인했다. 직전 Review 교정 9개 파일은 rebase 뒤 작업 트리에 보존돼 있으며 HEAD에만 포함된 것으로 간주하지 않았다. 이 보존 수정까지 합친 최종 후보의 base 대비 전체 diff는 35개 파일이다. 이전 base 4af7b49a에서 새 base로 전진한 5개 문서는 이 35개 파일과 겹치지 않는다. 현재 코드·문서·검사의 실제 변경을 다시 대조했고 추가 구현 교정은 필요하지 않았다.

현재 ExecPlan의 sources·AC와 R-053의 사용자 조치/구정책 큐/전송 확인 보존, R-055의 독립 차단·복구 근거, R-074의 cycle 수명·Merge·종료 조건을 다시 읽었다. G-265/266/267/268의 동일 ID archive 경로와 unittest v1/launcher·필수 compile 계약도 확인했다. 최종 문서 변경은 PRODUCT·ARCHITECTURE·선택 ExecPlan뿐이며 Requirement·다른 ExecPlan·verify·지침 원본의 작업 변경은 없다.

반복 전달된 두 지적은 현재 작업 트리의 `continued_from` 동일 본문 계보 확인과 계획 차단의 원자적 recovery_snapshot으로 교정돼 있다. 생성→재확인→늦은 callback→다음 claim과 계획 질문→프로젝트 최종 차단→해제→재연결의 경로를 현재 원본에서 확인했다. schema 추가는 nullable 호환 migration이며 과거 질문 근거를 합성하거나 공유 pending/손상 기록을 우회하지 않는다. 원래 취소·본문·cursor·attempt/error 감사, 같은 조치/다른 질문 구분과 실제 Worker 질문 보존 검사는 유지한다.

이전 후보의 통과 결과를 재사용하지 않고 현재 작업 트리에서 새로 실행했다.

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_notification_delivery.CurrentActionPolicyTest tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest.test_plan_block_and_checkpoint_request_share_one_transaction tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest.test_terminal_result_and_outbox_rollback_and_retry_together tests.test_runtime_state.RuntimeStateRepositoryTest.test_nonfinal_recovery_decision_cannot_enter_block_transaction tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_worker_decision_checkpoint_wait_survives_three_restarted_runs tests.test_notification_delivery.SegmentedDeliveryTest > /tmp/g226-rebased-late-scope.log 2>&1`
- 결과: 32개 통과, 10.804초. 조치 정책 18개와 계획 차단/checkpoint 원자성·최종 판단 경계·네이티브 Worker 질문·분할 전송의 관련 최소 단위를 포함한다.

base 대비 전체 diff와 이번 추가 기록의 `git diff --check` 통과. 이번 호출의 변경은 이 본문 검토/검증 기록뿐이며 이전 교정 파일과 frontmatter는 유지했다. 현재 base 대비 문서 patch hash를 다시 계산했다. 전체 verify는 실행하지 않았고 Manager의 현재 후보 전체 verify·독립 Review·Merge가 남아 있다.

### verify 자식 정리 중단 진단

시작 HEAD 1590993d59ffd63e948c041ffe19611363977efd에서 Manager가 전달한 verify identity `38c7da5b7c6d1337368626750d295749d5ab4650567076653317a980748c20b4` occurrence=1/revision=1은 종료 코드 2다. `/home/ubuntu/.local/state/loop-manager/logs/verify/93cabc3a5039202e3a273225139eb3e3/summary.json`과 runner/worker 로그를 확인했다. 완료 관측은 success 295개이며 보고된 assertion failure/error 결과는 없고 summary.result는 null이다. 완료되지 않은 검사를 통과·skip 또는 unit 실패 횟수로 바꾸어 기록하지 않는다.

두 worker는 bootstrap 저장 scope/runtime 선택 시나리오를 실행 중이었다. resource_wait는 CPU/사용자 한도 2와 실행 중 2개를 관측했고 이것만으로 중단 원인을 단정하지 않는다. runner는 약 92초 뒤 finally의 worker_shutdown으로 들어가 각 worker 종료를 약 10초 대기한 뒤 두 종료 시간 초과를 보고했다. 전체 1800초 상한 소진은 관측되지 않았다. 이 정리 예외가 남은 자료에서는 최초 진입 예외를 확인할 수 없어 primary 원인을 확정하지 못했다. worker 로그에는 해당 미완료 방법의 최종 결과/traceback도 없다.

현재 base 대비 verification core·verify_process·bootstrap 저장 scope fixture와 해당 두 test 모듈의 변경은 없다. G-226의 snapshot/확인 경로에 연결되는 원인도 확인되지 않아 timeout 확대·검사 삭제/skip·범위 밖 verifier 수정은 하지 않았다. bootstrap의 실제 CLI/Git 전체 시나리오나 전체 verify는 Worker에서 직접 실행하지 않았다. Manager가 최초 예외의 전체 runner 출력을 확보하고 같은 현재 후보의 전체 verify를 재실행해야 한다. 재현되는 원인이 확인되면 그 근거에 따라 후속 교정 범위를 판단한다.

- `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_verify_process.VerifyProcessTests.test_cleanup_uncertainty_keeps_the_primary_failure tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_worker_decision_checkpoint_wait_survives_three_restarted_runs tests.test_notification_delivery.CurrentActionPolicyTest.test_late_confirmation_updates_only_continued_versions_of_same_action tests.test_notification_delivery.CurrentActionPolicyTest.test_worker_question_survives_project_recovery_without_restoring_shared_record > /tmp/g226-verify-interruption-units.log 2>&1`: 이번 호출에서 4개 통과, 92.713초. 정리 불명 시 원래 오류 보존의 mock unit과 G-226의 실제 SQLite 질문/checkpoint·늦은 확인·독립 scope를 확인했다. 이 결과로 Manager 전체 verify 중단이 해소됐다고 간주하지 않는다.
- `git diff --check` 통과. 이번 호출은 선택 ExecPlan 본문에 진단/검증 기록만 추가했다. 현재 후보의 전체 verify·Review·Merge는 여전히 대기한다.

## Decision Log

- snapshot token `838df9fc33cc6c5ae03bf160617f85012fc5f000559f00c0b1efee15b7dc097f`의 원문 hash를 대조했다. 미착수 G-226의 ID와 당시 조사 기록은 보존하고 목표를 최신 R-053의 공통 정책 안에서 통합해 title/path를 개정한다. 유지보수 정책의 폐기된 메시지 범위 표시는 구현하지 않으며 R-074의 cycle 수명은 보존한다.
- G-247/G-294는 이 공통 경계를 선행으로 소비한다. G-189에 중복된 공통 구현은 후속 감사로 정리하며 새 알림 엔진·설정·제품 목표는 추가하지 않는다. 과거 G-221 정리와 G-225의 별도 Planner 목표를 복원하지 않는다.
- complexity high(최초 level 5): 사건 생성·부분 전송·확인 후 callback·재시작을 같은 현재 조치에 연결해야 한다. risk medium: 중복/누락 통지가 사용자 판단을 지연할 수 있으므로 원문·cursor·attempt/취소 감사와 조각별 재검사를 유지한다. priority high는 최신 알림 정책의 공통 선행이기 때문이다. Model·Effort는 Manager 설정을 따른다.
- 계획 수립 당시 결과는 full planning의 미충족 작업 구성이었으며 구현·검증 완료를 선언하지 않았다. 아래 구현 기록은 Worker targeted 결과이며 전체 verify·Review·사용자 Merge 또는 프로젝트 최종 완료를 대신하지 않는다.
