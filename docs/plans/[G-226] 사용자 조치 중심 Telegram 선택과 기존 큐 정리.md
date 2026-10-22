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

현재 `notifications/events.py`는 계획·시작·완료·대기와 상태 코드 기반 blocked 사건을 생성한다. `incidents.py`의 시작/재개 및 전송 확인 후 재개 생성도 남아 있다. `delivery.py`는 지속 실행 실패·targeted 실패 등 일부 사건만 취소하며 일반 진행 사건과 복구 가능한 blocked를 공통으로 배제하지 않는다. 기존 outbox·부분 전송·감사와 운영 상태를 재사용한다.

## Acceptance Criteria

- R-053/R-055: 명시적 미해결 판단·인증/권한·설정·reset 등 현재 사용자 조치, 실제 새 PR 검토/Merge 요청 또는 자동 복구가 불가능하다고 확인한 최종 중단만 생성·전송한다. 상태 코드·오류 문구·retry/교정 묶음 소진·다음 실행 재개 가능 상태만으로 조치 필요를 확정하지 않는다. 근거가 불충분하면 기존 오류·상태를 보존하고 통지를 보류한다.
- planning·Worker·verify·Review·유지보수·프로젝트의 진행/완료, 일반 시작/재개·자동 교정·일시 실패·자원/사용량 대기·checkpoint에는 무음 메시지도 만들거나 보내지 않는다. 기존 cycle 범위·실제 Merge 확인·종료와 프로젝트 완료 상태 기록은 알림 여부와 독립적으로 유지한다.
- 생성 시와 각 미전송 조각을 claim할 때 최신 원인·남은 조치·자동 복구 가능성을 재검사한다. 해소/대체된 원인과 구정책의 pending 사건·부분 전송 잔여 조각을 취소한다. frozen payload/text·확인된 전송 cursor·attempt/error·기존 incident와 취소 감사는 덮거나 삭제하지 않는다. 실패 재시도·시작 복구·전송 확인 callback이 취소 사건을 부활시키지 않는다.
- 같은 현재 조치의 기존 중복 제거를 유지하며 알림 실패가 실제 작업 결과·재시도 횟수·소유권을 바꾸지 않는다. 조치의 사실 확인을 위해 추가 Agent 호출이나 Git 쓰기·새 전송 서비스를 만들지 않는다. 외부 관측은 DB transaction 밖에서 수행하고 적용 때 현재 원인/identity를 다시 비교한다.
- PR과 draft의 현재 유효성은 기존 관측·질문 상태에 연결할 수 있는 공통 경계를 제공한다. 이 작업만으로도 기존의 유효한 사용자 조치는 계속 전송되며 G-247/G-294의 세부 identity 보완을 기다려 전체 알림을 끄지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/notifications/events.py`·`repository.py`의 사건 선택과 `recovery/recovery_runtime.py`·`recovery/project_blocked_recovery.py`의 실제 원인/남은 복구 상태를 대조한다. 현재 사용자 행동의 구체적 근거를 기존 사건 payload/원인에 연결하고 status나 FINAL_BLOCK 이름만으로 판정하는 분기를 교정한다. 실행 복구 정책 자체는 G-313/G-189와 중복 구현하지 않는다.
- [ ] 확인한 `notifications/delivery.py:_claim_piece()`와 `_deliver_rows()`의 조각별 현재 판정·취소/confirm 경계에 공통 정책을 적용한다. `dispatch_pending`·새 PR 즉시 전송·오류 재시도 모두 같은 기준을 거치게 하고 전송 중 해소/취소와 확인 callback의 경합에도 이미 전송한 기록을 보존한다.
- [ ] 확인한 `notifications/incidents.py`의 `record_work_start`·`recover_interrupted_starts`·`_materialize_resumption`, `events.py`의 `record_process_start`·`record_planning_publication`·`enqueue_completion_if_ready`·`enqueue_waiting_reason`, `storage/maintenance_state.py`의 cycle 사건을 교정한다. Telegram enqueue만 제거하고 실제 시작·대기 episode·완료 및 cycle 범위/선행/Merge/종료 상태와 감사는 유지한다. 과거 outbox 조회로 통지 여부를 결정하던 경로도 새 사건을 재생성하지 않게 한다.
- [ ] 확인한 `notifications/work_exhaustion.py`·`rendering.py`와 `tests/support/notification.py`의 기존 원인 표현·fixture를 재사용한다. 사용자의 실제 다음 행동과 근거를 정확히 표시하며 추정 권한 요청·가짜 해결 안내를 만들지 않는다. PRODUCT·ARCHITECTURE의 소유 알림 절에 구현한 생성/전송 계약만 통합한다.

## Validation

- Worker는 `tests/test_notification_events.py`·`test_notification_delivery.py`·`test_notification_incidents.py`·`test_notification_work_cycles.py`·`test_maintenance_state.py`의 변경 관련 최소 unit case를 실행한다. 실제 SQLite·사건 선택·claim/confirm을 사용하고 Telegram 응답과 외부 관측만 double로 제공한다.
- 현재 미해결 질문/인증 조치/실제 복구 불가 중단은 1회 전송하고, retry 소진·다음 실행 재개·CPU/Git/DB/사용량 대기·자동 교정·정상 진행/완료는 0회인지 확인한다. 동일 상태 코드에서 원인과 남은 조치가 다른 경우를 비교하며 enum 반환값만 mock해 판정을 대신하지 않는다.
- 구정책 pending·이미 일부 조각 전송·claim 뒤 원인 해소·전송 실패 뒤 재시작·confirm callback 뒤 재개 생성 사례에서 미전송분 취소와 cursor/payload/attempt/error 보존을 확인한다. 다음 조각 직전 최신 근거가 없으면 전송하지 않아야 한다.
- 과거 시작/재개/maintenance 메시지만 요구하던 assertion은 현재 무통지로 교체한다. 실제 process 시작·cycle 범위·Merge 반영·종료·프로젝트 완료와 outbox 감사의 assertion은 유지한다. 대체 없는 fixture/보존 검증 삭제나 전체 테스트 skip은 하지 않는다.
- 알림 비활성/실패·DB/전송 경합에서도 작업 상태·외부 효과 수·필수 성공 근거가 달라지지 않아야 한다. 선택 adapter는 unittest version 1·launcher Python 3.11–3.14이며 기존 수집·필수 compile·Agent 호출 차단을 유지한다.

## Progress

- [x] 2026-10-08 기준 0e887794의 R-053·R-074, cycle 사건 작성·selection·종료·rendering과 신규 유지보수 알림 테스트를 정적으로 대조했다. 현재 payload/rendering의 범위 누락을 확인했으며 구현·테스트 실행은 하지 않았다.
- [x] revision `4aafffa5b3af1b3a378a93750c7d86292891b5df`의 활성 명세 전문과 생성·incident·delivery/maintenance 경로를 정적으로 대조했다. 기존 유지보수 범위 표시 대신 현행 무통지와 공통 조치 판정의 남은 작업을 확정했다. 실행 검증은 하지 않았다.
- [ ] 공통 생성/조각 전송 판정과 구정책 큐·callback 정리
- [ ] 운영 상태/감사 보존과 최소 targeted 회귀

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

## Decision Log

- snapshot token `838df9fc33cc6c5ae03bf160617f85012fc5f000559f00c0b1efee15b7dc097f`의 원문 hash를 대조했다. 미착수 G-226의 ID와 당시 조사 기록은 보존하고 목표를 최신 R-053의 공통 정책 안에서 통합해 title/path를 개정한다. 유지보수 정책의 폐기된 메시지 범위 표시는 구현하지 않으며 R-074의 cycle 수명은 보존한다.
- G-247/G-294는 이 공통 경계를 선행으로 소비한다. G-189에 중복된 공통 구현은 후속 감사로 정리하며 새 알림 엔진·설정·제품 목표는 추가하지 않는다. 과거 G-221 정리와 G-225의 별도 Planner 목표를 복원하지 않는다.
- complexity high(최초 level 5): 사건 생성·부분 전송·확인 후 callback·재시작을 같은 현재 조치에 연결해야 한다. risk medium: 중복/누락 통지가 사용자 판단을 지연할 수 있으므로 원문·cursor·attempt/취소 감사와 조각별 재검사를 유지한다. priority high는 최신 알림 정책의 공통 선행이기 때문이다. Model·Effort는 Manager 설정을 따른다.
- 이번 결과는 full planning의 미충족 작업 구성이다. 구현·테스트·전체 verify 성공, 사용자 승인·Merge 또는 프로젝트 최종 완료를 기록하지 않는다.
