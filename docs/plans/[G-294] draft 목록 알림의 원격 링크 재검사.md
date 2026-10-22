---
id: G-294
title: draft 목록 알림의 원격 링크 재검사
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
depends_on:
  - G-226
complexity: medium
risk: medium
priority: normal
---

## Purpose

미해결 draft 질문의 통지 identity와 현재 원격 링크 표시를 분리해 링크 교체만으로 재통지하지 않는다. 새로 알려야 할 질문에는 검증한 현재 링크를 사용하고, 해소된 질문·오래된 미전송 표현은 취소한다. G-226의 공통 현재 조치 판정과 기존 outbox/질문 저장을 사용한다.

현재 목록은 질문 ID/identity/revision을 비교하지만 payload의 URL은 다시 확인하지 않는다. legacy 개별 draft는 pending/revision만으로 유효성을 판단할 수 있다. 단순히 URL을 사건 identity에 추가하거나 일부 전송 뒤 목록 전체를 재발행하면 최신 R-053의 같은 질문 중복 통지 금지와 충돌한다.

## Acceptance Criteria

- 현재 미해결 사용자 판단이 확인된 질문만 전송한다. 프로젝트별 계획 ID·title·질문 identity/revision과 검증한 원격 링크가 일치하며, 사용자 답변 검증·draft 해제/게시·취소가 끝난 질문은 제외한다. pending 문자열만으로 새로운 사용자 질문을 만들지 않는다.
- 질문/행동 identity는 링크 표시와 구별한다. 같은 질문에서 링크만 변경되면 새 질문·승인·중복 메시지를 만들지 않는다. 이미 통지한 질문은 재전송하지 않고 아직 알리지 않은 질문의 표시만 현재 링크로 준비한다.
- 생성 시와 각 미전송 조각 전송 직전에 최신 목록·질문·링크를 확인한다. 오래된 URL의 unsent 표현은 취소하고 이미 확인된 조각·frozen 원문·cursor·attempt/오류를 보존한다. 기존 row의 본문/URL을 덮어써 보낸 내용을 바꾸지 않는다.
- 일부 전송된 목록에서 질문 해소/추가/내용 변경·링크만 변경이 섞여도 실제 새 행동만 알린다. 표시를 갱신하면서 기존 질문을 다시 요구하지 않으며 통지 확인이 불확실한 경우 기존 전송/실패 증거로 재개한다. 알림을 이유로 사용자 결정을 승인·초기화하거나 Worker를 시작하지 않는다.
- legacy 개별 사건도 현재 유효한 질문에 연결된 경우만 유지한다. 현재 질문 ID/identity 또는 출처가 확인되지 않는 오래된 사건은 보존한 채 미전송분을 취소/보류하며 결정 감사는 삭제하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/notifications/events.py:enqueue_draft_decisions()`와 `storage/execplan_draft_state.py`의 현재 질문 집합·identity/revision·검증한 원격 위치를 재사용한다. 실제 질문/행동의 통지 확인과 링크를 포함한 표시 snapshot을 구별하는 최소 기록을 기존 사건/질문 경계에 연결한다. 별도 판단 DB·알림 서비스나 URL 기반 사용자 승인 identity는 만들지 않는다.
- [ ] 확인한 `notifications/delivery.py:_claim_piece()`의 목록/개별 draft 재검사와 claim/confirm 경합에 G-226의 정책을 적용한다. 전체 목록을 재발행해 기전송 질문을 다시 통지하지 않고, 알리지 않은 현재 질문만 현재 표현으로 이어간다. 취소/전송 확인이 교차해도 실제 확인 기록을 잃지 않는다.
- [ ] 확인한 `notifications/repository.py`·`rendering.py`·`tests/support/notification.py`의 frozen text·cursor·사건/질문 fixture를 재사용한다. 원격 URL 검증 실패를 새 질문으로 바꾸지 않는다. PRODUCT·ARCHITECTURE의 draft 알림 소유 절에 질문 identity/표시 분리를 통합한다. R-061의 답변 출처 검증·질문 해소·Planner/Manager 권한은 바꾸지 않는다.

## Validation

- Worker는 `tests/test_notification_events.py`·`test_notification_delivery.py`·`test_notification_incidents.py` 중 변경한 최소 unit을 실행한다. 실제 SQLite·현재 질문 선택·claim/confirm·renderer를 사용하고 Telegram 응답/원격 관측만 mock한다.
- 미전송·일부 전송·전체 전송 각각에서 같은 질문의 링크만 바꾼다. 현재 링크의 표시 정합성, 이미 통지한 질문의 재전송 0회와 frozen 원문/cursor 보존을 확인한다. 단순 사건 수나 URL 문자열 비교만으로 질문 중복 방지를 대신하지 않는다.
- 질문 해소/추가/내용 변경과 링크 변경을 섞고 첫 조각 뒤·claim 뒤·confirm 저장 실패·재시작을 제어한다. 해소된 질문 전송 0회, 실제 새 질문/행동만 1회, 이전 질문/결정/부분 전송 감사 보존을 확인한다.
- legacy pending/revision은 같지만 현재 질문 ID가 없는 경우를 거부하고 유효한 legacy 질문은 계속 알린다. 기존 사용자 답변 검증·draft 보류·부분 전송/오류 보존 회귀는 유지한다. 과거 링크만 변경 시 전체 목록 재발행을 요구하던 assertion만 현행 계약으로 대체한다.

## Progress

- [x] 비교 기준 `656432eb4c384eb1146665a87d4342f5e42712fb`부터 현재 `191fde0b15510f80879784b1ec0f1fcd2f516962`까지의 문서·알림·draft 상태·관련 회귀 변경을 정적으로 대조했다. URL을 payload에 저장하지만 생성/전송 identity 비교에는 포함하지 않는 잔여 범위를 확인했다. 구현·테스트·verify는 실행하지 않았다.
- [x] 현재 R-053 전문의 링크만 변경 시 중복 금지와 R-061·목록/legacy claim 경계를 정적으로 대조했다. 이전 조사에서 지적한 URL 부재를 사건 identity 추가로 해결하지 않고 질문과 표시의 분리로 개정한다. 실행 검증은 하지 않았다.
- [ ] 현재 질문/표시 분리와 조각별 링크·유효성 재검사
- [ ] 부분 전송·legacy·링크만 변경의 최소 targeted 회귀

## Decision Log

- 제공된 snapshot의 같은 ID·경로·source와 기존 조사 기록을 보존한다. G-226의 공통 정책을 선행 소비하고 이 계획은 draft 질문/표시만 담당한다. G-244의 과거 archive나 답변 검증 구현을 복원·중복 구현하지 않는다.
- URL만을 새로운 사건 identity로 넣는 과거 수정 방향은 현행 R-053의 질문 중복 금지로 대체됐다. 이미 보낸 표시를 덮거나 질문을 다시 발행해 감사와 사용자 판단을 바꾸지 않는다.
- complexity medium(최초 level 3): 질문 집합·표시 snapshot·부분 전송 확인을 재시작/경합에서 일치시켜야 한다. risk medium: 누락/중복 질문은 사용자 판단을 지연할 수 있으므로 원문·cursor·attempt와 질문별 실제 통지 근거를 보존한다. priority normal이며 Model·Effort는 Manager 설정을 따른다.
- unittest adapter version 1·launcher Python 3.11–3.14와 기존 수집·필수 compile·Agent 호출 차단을 유지한다. 이번 full planning은 구현·테스트·verify 성공이나 프로젝트 최종 완료가 아니다.
