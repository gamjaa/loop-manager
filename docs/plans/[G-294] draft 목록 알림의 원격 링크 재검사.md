---
id: G-294
title: draft 목록 알림의 원격 링크 재검사
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
depends_on: []
complexity: low
risk: medium
priority: normal
---

## Purpose

draft 목록 알림의 원격 링크를 현재 저장된 판단 근거와 대조해 오래된 링크를 가진 미전송 목록을 교체한다. 현재 목록 생성·전송 재검사는 ID·질문 identity·판단 revision을 비교하지만 URL을 비교하지 않는다. 이 세 값이 같은 상태에서 확인된 링크가 추가·변경되면 이전 목록을 그대로 재사용하거나 전송할 수 있다. R-053의 현재 ID 링크와 미전송 목록 재검사, R-061의 질문·원격 입력 연결 및 ARCHITECTURE의 링크 변경 재검사 계약에서 이 누락만 보완한다.

## Acceptance Criteria

- 실제 사용자 판단이 이미 자동 해소됐거나 질문이 유효하지 않으면 링크 변경만으로 목록을 다시 보내지 않으며 기존 미전송·잔여 조각을 취소한다. 보존된 decision·전송 감사는 유지한다.
- 기존 `draft_decision` 개별 legacy 알림도 pending/revision 일치만으로 계속 전달하지 않고 현재 미해결 질문·사용자 행동 필요성을 재확인한다. 자동 복구로 더는 사용자 질문이 없으면 오래된 미전송 메시지를 취소한다.

- R-053/R-061: 현재 실제 사용자 질문 목록의 ID·질문 identity·판단 revision이 같아도 저장된 원격 링크가 달라지면 오래된 목록의 미전송 조각을 취소한다. 현재 확인한 링크를 가진 목록을 한 번 생성하고, 같은 목록·같은 근거의 재관측과 재시작은 중복 전송하지 않는다.
- 목록 생성 뒤 전송 직전 또는 조각 사이에 링크가 바뀌어도 다음 조각을 보내기 전에 재검사한다. 이전 payload·고정 텍스트·이미 확인된 전송 위치와 전송/취소 감사를 보존하고, 현재 목록은 기존 ID 링크·UTF-16 entity 처리로 전달한다. 전송 완료된 과거 알림을 수정하거나 취소하지 않는다.
- 링크가 없는 항목도 ID·제목을 표시하는 기존 계약을 유지한다. 비교에는 Manager가 이미 관측해 저장한 링크만 사용하며 링크를 추측하거나 GitHub 조회·외부 전송을 새로 수행하지 않는다. 링크 표현 변화가 사용자 답변·새 질문·판단 revision 변경이나 실행 대기 해제의 근거가 되지 않는다.
- 자동 유효성 재평가는 목록과 건수에서 계속 제외하고 다른 실제 질문은 유지한다. 기존 개별 `draft_decision` 알림도 현재 실제 미해결 사용자 질문이 남아 있는 경우에만 pending/revision·판단 근거를 재검사해 전달한다. 이미 해결된 질문이나 새 정책에서 불필요한 미전송 조각은 취소하며 전송된 과거 조각·payload·감사는 유지한다.

## Implementation

- [ ] `notifications/delivery.py:_claim_piece()`의 legacy `draft_decision`에도 현재 미해결 질문과 R-053의 사용자 행동 필요성 검사를 적용한다. 링크 재검사·목록 dedup은 기존 구현 경계로 유지한다.

- [ ] 확인한 `loop_manager_internal/notifications/events.py:enqueue_draft_decisions()`의 목록 중복 판정과 event identity를 현재 목록의 저장된 URL에도 연결한다. 기존 `decisions()`의 ID·질문 identity·revision 비교와 outbox 취소/생성 경계를 재사용하고 동일 입력의 반복 생성과 링크 변경 누락을 함께 막는다. 새 notification 종류·DB schema·공통 상태 저장소를 추가하지 않는다.
- [ ] 확인한 `loop_manager_internal/notifications/delivery.py:_claim_piece()`의 `draft_decisions` 분기에서 payload의 링크와 현재 evidence의 링크를 대조한다. 기존 `user_question_ids()`와 pending·revision·identity 검사를 유지하고 stale 목록의 남은 조각만 취소한다. 같은 파일의 legacy `draft_decision` 분기와 전송 확인·재시도·entity 경계를 보존한다.
- [ ] 확인한 `loop_manager_internal/storage/execplan_draft_state.py:observe()`는 scope/source가 같은 pending draft의 판단 revision을 유지하면서 제공된 `remote_url`로 URL을 만들 수 있다. 과거 저장 근거에 링크가 없고 재관측에서 확인된 링크가 생기는 경우도 구분한다. `catalog/catalog_service.py`의 origin 기반 관측, `catalog/execplan_drafts.py`의 scope와 파일명/title 검증은 재사용하고 링크 갱신을 위해 승인 지문·출처·계보·소유권 검사를 바꾸지 않는다. 경로 rename만으로 revision이 유지된다고 가정하지 않는다.
- [ ] 확인한 `tests/test_notification_delivery.py:DraftListNotificationTest`와 `tests/test_notification_events.py:TimelyProgressEventTest`, `tests/test_execplan_drafts.py`의 SQLite·실제 draft 관측 fixture를 확장한다. 현재 질문/revision/link를 함께 바꾸는 테스트와 별도로 링크만 다른 조건을 검사한다. PRODUCT·ARCHITECTURE는 이미 선언한 현재 링크/전송 계약과 최종 구현을 대조하고 필요한 설명만 정합화하며 Requirement를 수정하지 않는다.

## Validation

- Worker는 위 세 테스트 모듈에서 변경 동작의 최소 unit case만 실행한다. 실제 SQLite/outbox·`execplan_draft_state.observe()`·목록 생성/dispatch를 사용하고 transport는 기존 mock으로 제공한다. 실제 Telegram·Agent·원격 Git은 호출하지 않는다.
- ID·identity·판단 revision을 고정하고 URL만 변경한 경우, 링크 없음에서 확인된 링크가 생긴 경우를 검사한다. 생성 경계와 dispatch 경계를 각각 확인해 이전 미전송 알림은 취소되고 최신 목록의 ID entity가 현재 저장 링크를 가리키며 같은 입력의 추가 전송은 없어야 한다. 실제 observe 연결 사례에서는 판단 대기·revision·결정 감사가 링크 관측 때문에 바뀌지 않는지도 확인한다.
- 긴 목록의 첫 조각 전송 후 링크만 변경한 경우와 전송 실패 뒤 재시작을 검사한다. 오래된 목록의 남은 조각 미전송, 원래 payload·확정 cursor 보존과 최신 목록의 한 번 전송을 확인한다. 이미 전송 완료된 알림의 감사는 유지한다.
- 기존 자동 재평가 제외·빈 목록 취소·부분 해소·한글/이모지 UTF-16 링크·legacy 개별 알림 재시작 회귀를 재사용한다. 링크 비교 추가가 다른 질문 목록을 제거하거나 과거 질문 알림·Planner 호출·새 승인을 만들면 실패해야 한다.

## Progress

- [x] 비교 기준 `656432eb4c384eb1146665a87d4342f5e42712fb`부터 현재 `191fde0b15510f80879784b1ec0f1fcd2f516962`까지의 문서·알림·draft 상태·관련 회귀 변경을 정적으로 대조했다. URL을 payload에 저장하지만 생성/전송 identity 비교에는 포함하지 않는 잔여 범위를 확인했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 목록 생성과 조각 전송의 현재 링크 재검사
- [ ] 링크만 변경된 조건·중단 재시도·기존 알림 보존의 최소 unit 검증

## Decision Log

- 2026-10-22 정책 변경: 실제 미해결 사용자 판단이 없으면 draft 목록/개별 알림을 보내지 않는다. 단순 링크 교체는 새로운 질문·승인·중복 메시지가 아니다. 부분 전송과 과거 결정 감사의 보존 계약은 유지한다.

- 이번 검토는 delta이며 프로젝트 최종 전문 확인이 아니다. 같은 변경의 답변 출처·공통 diff·부분 정리·자동 재평가 제외·단일 의미 검토는 현재 코드와 회귀에 이미 반영됐다. G-244의 같은 ID archive 이동을 목표 삭제로 해석하거나 소급 계획·검증 성공을 만들지 않는다. 이 계획은 그 구현 이후 남은 목록 URL 비교만 담당하며 catalog의 실패 알림·유지보수 알림·차단 전수 감사 작업을 중복 구현하지 않는다.
- 새 ID는 활성/archive 파일의 최대 293 다음 번호다. 선행 구현은 현재 base에 반영돼 있어 `depends_on`은 비어 있다. mutable snapshot 밖의 계획·사용자 결정·실행 기록을 변경하지 않으며 `docs/FUTURE_IDEAS.md` 항목을 작업 근거로 쓰지 않았다.
- complexity low: 기존 목록 비교·outbox·claim 경계의 국소 보완이다. risk medium: 잘못된 링크나 중복/누락 알림은 사용자 판단을 지연할 수 있으나 실행 승인·Git 쓰기와 분리돼 있고 현재 목록 재발행으로 복구할 수 있다. 부분 전송 감사와 legacy 보존을 회귀로 확인한다. priority normal은 기존 사용자 알림 계약의 필수 정합성이다. Model·Effort는 Manager 설정을 따른다.
- 선택 verify는 `scripts/verify.json`의 unittest version 1·launcher Python·독립 tests 수집과 필수 compile이다. `docs/VERIFY.md`의 Python 3.11–3.14·fixture/native 결과·Agent 호출 차단 계약과 기존 입력 범위를 유지한다. adapter·검증 체계 변경이나 Planner의 테스트/전체 verify 실행은 이 작업에 포함하지 않는다.
