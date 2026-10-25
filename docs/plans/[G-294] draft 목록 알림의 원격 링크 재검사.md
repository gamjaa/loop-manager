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

현재 `notifications/policy.py:is_current()`는 목록의 질문 ID/identity/revision을 비교하고 legacy 개별 draft도 pending·question 존재·reassessment 부재·revision을 확인한다. 생성과 각 조각 claim의 공통 연결은 이미 구현돼 있다. 남은 범위는 목록 URL의 현재성, 개별 사건의 질문 identity/출처와 질문별 실제 통지 확인이다. 단순히 URL을 사건 identity에 추가하거나 일부 전송 뒤 목록 전체를 재발행하면 최신 R-053의 같은 질문 중복 통지 금지와 충돌한다.

## Acceptance Criteria

- 현재 미해결 사용자 판단이 확인된 질문만 전송한다. 프로젝트별 계획 ID·title·질문 identity/revision과 검증한 원격 링크가 일치하며, 사용자 답변 검증·draft 해제/게시·취소가 끝난 질문은 제외한다. pending 문자열만으로 새로운 사용자 질문을 만들지 않는다.
- 질문/행동 identity는 링크 표시와 구별한다. 같은 질문에서 링크만 변경되면 새 질문·승인·중복 메시지를 만들지 않는다. 이미 통지한 질문은 재전송하지 않고 아직 알리지 않은 질문의 표시만 현재 링크로 준비한다.
- 생성 시와 각 미전송 조각 전송 직전에 최신 목록·질문·링크를 확인한다. 오래된 URL의 unsent 표현은 취소하고 이미 확인된 조각·frozen 원문·cursor·attempt/오류를 보존한다. 기존 row의 본문/URL을 덮어써 보낸 내용을 바꾸지 않는다.
- 일부 전송된 목록에서 질문 해소/추가/내용 변경·링크만 변경이 섞여도 실제 새 행동만 알린다. 표시를 갱신하면서 기존 질문을 다시 요구하지 않으며 통지 확인이 불확실한 경우 기존 전송/실패 증거로 재개한다. 알림을 이유로 사용자 결정을 승인·초기화하거나 Worker를 시작하지 않는다.
- legacy 개별 사건도 현재 유효한 질문에 연결된 경우만 유지한다. 현재 질문 ID/identity 또는 출처가 확인되지 않는 오래된 사건은 보존한 채 미전송분을 취소/보류하며 결정 감사는 삭제하지 않는다.

## Implementation

- [x] `events.py`·`execplan_draft_state.py`의 현재 질문 identity/revision과 ExecPlan catalog·등록 remote에서 계산한 링크를 재사용했다. 질문 통지와 제목·경로·URL 표시 snapshot을 기존 outbox payload·고정 본문·확인 cursor 경계에 연결했으며 별도 판단 저장소와 URL 기반 identity는 추가하지 않았다.
- [x] `policy.is_current()`가 목록·legacy 사건에서 질문 ID/identity/revision·출처·현재 링크를 대조하고 생성과 `_claim_piece()`의 공통 판정으로 동작한다. 확인된 ID가 포함된 outbox 조각에서 질문별 실제 통지를 계산해 이미 통지된 질문은 새 목록에서 제외한다. URL 변경·취소·늦은 확인에도 이전 payload·고정 본문·cursor·attempt/error를 보존한다. G-226의 공통 취소와 blocked 전용 `continued_from` 동작은 변경하지 않았다.
- [x] 기존 `repository.py`의 outbox frozen text/cursor와 renderer 링크 entity, 질문 evidence를 재사용했다. 현재 링크가 없거나 불일치해도 질문 identity는 변경하지 않는다. PRODUCT·ARCHITECTURE의 draft 알림 계약을 구현에 맞게 갱신했고 R-061의 사용자 답변 검증·질문 해소·Planner/Manager 권한은 변경하지 않았다.

## Validation

- Worker는 `tests/test_notification_events.py`·`test_notification_delivery.py`·`test_notification_incidents.py` 중 변경한 최소 unit을 실행한다. 실제 SQLite·현재 질문 선택·claim/confirm·renderer를 사용하고 Telegram 응답/원격 관측만 mock한다.
- 미전송·일부 전송·전체 전송 각각에서 같은 질문의 링크만 바꾼다. 현재 링크의 표시 정합성, 이미 통지한 질문의 재전송 0회와 frozen 원문/cursor 보존을 확인한다. 단순 사건 수나 URL 문자열 비교만으로 질문 중복 방지를 대신하지 않는다.
- 질문 해소/추가/내용 변경과 링크 변경을 섞고 첫 조각 뒤·claim 뒤·confirm 저장 실패·재시작을 제어한다. 해소된 질문 전송 0회, 실제 새 질문/행동만 1회, 이전 질문/결정/부분 전송 감사 보존을 확인한다.
- legacy pending/revision은 같지만 질문 identity/출처가 현재 질문과 연결되지 않는 경우를 거부하고 유효한 legacy 질문은 계속 알린다. 현재 구현의 question 부재·reassessment 거부와 `tests/test_notification_delivery.py:DraftListNotificationTest`의 해소/identity/revision 변경 취소·재시작·분할 전송 회귀는 재사용하며 같은 조건을 복제하지 않는다. 기존 사용자 답변 검증·draft 보류·부분 전송/오류 보존 회귀는 유지한다. 과거 링크만 변경 시 전체 목록 재발행을 요구하던 assertion만 현행 계약으로 대체한다.
- `tests/test_execplan_drafts.py::ExecPlanDraftCatalogTest.test_reassessment_coexists_with_user_question_list_and_new_question`는 재평가 후 새 질문을 알리면서 앞서 확인한 질문을 다시 싣지 않는 통합 회귀로 유지한다.

## Progress

- [x] 비교 기준 `656432eb4c384eb1146665a87d4342f5e42712fb`부터 현재 `191fde0b15510f80879784b1ec0f1fcd2f516962`까지의 문서·알림·draft 상태·관련 회귀 변경을 정적으로 대조했다. URL을 payload에 저장하지만 생성/전송 identity 비교에는 포함하지 않는 잔여 범위를 확인했다. 구현·테스트·verify는 실행하지 않았다.
- [x] 현재 R-053 전문의 링크만 변경 시 중복 금지와 R-061·목록/legacy claim 경계를 정적으로 대조했다. `e69a2399992a7e598217741cdcd3833d9b0257de`에서 공통 정책 이동과 legacy question/reassessment 검사는 구현된 범위로 제외했다. URL 현재성·개별 질문 identity/출처·질문별 통지 확인은 남아 있으며 URL을 사건 identity에 추가하지 않고 질문과 표시를 분리한다. 실행 검증은 하지 않았다.
- [x] 현재 질문 snapshot은 저장된 pending evidence, ExecPlan catalog 경로·revision, 등록 remote에서 계산한다. 생성·각 조각 claim에서 이 snapshot과 대조하고 outbox 고정 본문·확인 cursor에서 질문별 확인을 계산한다.
- [x] Worker targeted test: `DraftListNotificationTest` 14개와 `tests.test_notification_events` 41개가 통과했다. 최초 delivery class 실행에서 URL 형식에 고정된 예전 assertion이 실패해 현재 catalog 경로 기반 URL을 assertion으로 반영하고 동일 명령을 재실행했다. 최초 event module 실행에서 identity/source 필드가 빠진 legacy fixture가 새 계약에 거부되어 유효한 legacy fixture와 거부 회귀를 추가한 뒤 동일 명령을 재실행했다. 두 재실행은 통과했다.
- [x] Verify feedback에서 재평가 통합 회귀가 앞서 알린 G-002까지 재통지하는 이전 기대값을 확인했다. 테스트를 첫 알림의 G-002와 재평가 후 새 G-001만 각각 확인하도록 조정하고 같은 targeted test를 재실행해 통과했다.

## Decision Log

- 제공된 snapshot의 같은 ID·경로·metadata·source·dependency와 기존 조사 기록을 보존한다. G-226의 공통 정책은 현재 `notifications/policy.py`를 재사용하고 이 계획은 draft 질문/표시와 실제 통지 확인만 담당한다. 생성/claim의 정책 연결·구정책 큐 정리·blocked 조치 계보를 재구현하거나 G-244의 과거 archive·답변 검증 구현을 복원하지 않는다. 이번 delta의 정적 대조는 구현·검증 성공이나 프로젝트 최종 전문 확인이 아니다.
- URL만을 새로운 사건 identity로 넣는 과거 수정 방향은 현행 R-053의 질문 중복 금지로 대체됐다. 이미 보낸 표시를 덮거나 질문을 다시 발행해 감사와 사용자 판단을 바꾸지 않는다.
- complexity medium(최초 level 3): 질문 집합·표시 snapshot·부분 전송 확인을 재시작/경합에서 일치시켜야 한다. risk medium: 누락/중복 질문은 사용자 판단을 지연할 수 있으므로 원문·cursor·attempt와 질문별 실제 통지 근거를 보존한다. priority normal이며 Model·Effort는 Manager 설정을 따른다.
- unittest adapter version 1·launcher Python 3.11–3.14와 기존 수집·필수 compile·Agent 호출 차단을 유지한다. 이번 full planning은 구현·테스트·verify 성공이나 프로젝트 최종 완료가 아니다.
