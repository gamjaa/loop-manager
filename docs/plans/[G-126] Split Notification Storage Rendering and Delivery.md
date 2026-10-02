---
id: G-126
title: Split Notification Storage Rendering and Delivery
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-125
complexity: medium
risk: high
priority: critical
---

## Purpose

`notifications.py`의 1,552줄에는 schema·outbox/차단 사건·완료 주기·event 생성·문구와 URL·Telegram 분할 전송이 섞여 있다. 테스트도 2,379줄이다. 저장, event 판단, 표현과 외부 전송을 분리해 중복 없는 알림과 실패 복구 계약을 유지한다.

## Acceptance Criteria

- notification repository/schema, event·incident 판단, 순수 rendering/분할과 Telegram transport를 기능별 module로 분리한다. HTTP adapter는 저장 상태·제품 event 정책을 직접 결정하지 않는다.
- event identity, payload encoding, incident generation, completion cycle, outbox state와 SQLite transaction을 보존한다. 동일한 차단·대기·완료 사건의 중복 제거, 해제 후 한 번의 재개 알림과 미전송 조각 취소를 유지한다.
- 분할 경계와 순서, `confirmed_pieces`, 전송 시작/확정 기록, 실패 재시도 및 중단 뒤 미확정 조각 처리가 기존과 같다. 전송 실패가 프로젝트 실행 상태를 성공·실패로 바꾸지 않는다.
- 같은 계약의 payload/URL/문구 생성, secret redaction과 상태 조회 중복을 제거한다. 서로 다른 event의 원인·revision·scope identity를 임의의 공통 key로 합치지 않는다.
- `tests/test_notifications.py`는 저장/event 판단, rendering/분할, transport/retry와 재개 시나리오별로 나뉜다. token/chat ID 미설정과 실패 case를 유지하며 실제 Telegram 전송을 하지 않는다.

## Implementation

1. G-060 inventory로 notification 소비자와 저장/event 생성의 transaction owner를 확인한다. G-120의 storage 접속과 기존 notification schema의 책임을 구분한다.
2. repository·event service·renderer/segmenter·transport를 G-063 package에 분리하고 필요한 port로 연결한다. public notification helper는 하나의 실제 구현으로 위임한다.
3. 차단·해제·재개와 work-exhaustion/completion event 생성의 반복 조회·identity 계산은 동일 계약만 재사용한다. Manager의 planning 오류 사건·validation 실패 감사와 알림 감사의 독립성을 유지한다.
4. 큰 테스트를 책임별로 나누고 SQLite/HTTP fake와 support fixture를 필요한 범위에 둔다. 전송 결과 확인을 fixture가 대신 수행하지 않는다.
5. 호출 측은 좁은 event/repository/transport 경계로 갱신한다. 동작·저장 형식·비밀값 취급과 두 verify의 검증 범위를 유지한다.

## Validation

- Worker는 변경한 event identity/repository와 renderer/transport의 관련 targeted unit test만 실행한다.
- 같은 사건 재관측, 부분 전송 후 재시작, 실패 조각만 재시도, block 해제 후 미전송 취소, 재개 알림의 단 한 번 생성과 Telegram 비활성화를 검증한다.
- Unicode·긴 메시지 분할, checkpoint 링크·미게시 사유, redaction과 payload round trip을 확인한다.
- Manager 전체 verify에서 모든 알림·완료·대기·block recovery 시나리오와 사용량/검증 감사가 유지돼야 한다.

## Progress

- [ ] notification 저장·event·rendering·transport 계약 확인
- [ ] 책임별 module 추출과 중복 제거
- [ ] notification test 분리 및 targeted unit test
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: 알림 전달은 외부 도구 효과이고 사건 생성은 제품 규칙이다. 두 책임을 분리하되 기존 SQLite column과 event key를 변경하지 않는다.
- 실제 알림 전송이나 비용이 발생하는 새 서비스를 리팩터링 검증으로 도입하지 않는다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
