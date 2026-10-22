---
id: G-226
title: 유지보수 cycle 알림의 대상 범위 표시
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: low
priority: normal
---

## Purpose

R-053의 사용자 개입 중심 정책에 따라 유지보수 cycle 시작·종료 Telegram 진행 알림과 해당 메시지의 대상 범위 표시를 제거한다. 실제 평가 대상·시작/종료 사유·후속 작업 범위는 운영 로그·읽기 전용 status에 남기고 기존 알림 outbox의 미전송분을 안전하게 정리한다. R-074의 평가·실제 Merge 완료·정상 종료 기준은 바꾸지 않는다.

## Acceptance Criteria

- 유지보수 cycle 시작·종료·평가 진행, 후속 Merge 대기 자체에는 일반/무음 Telegram 메시지를 만들지 않는다. 실제 사용자 결정·PR 검토/Merge·자동 복구 불가능한 최종 중단은 각각의 현재 사용자 행동 사건에서만 알린다.
- 실제 selection과 평가 근거에 따른 전체/선정 대상 범위·사유·진행 상태·후속 계획과 dependency를 운영 로그 및 읽기 전용 status에서 확인할 수 있다. 평가 미완료·검증 미완료·Merge 대기를 정상 종료로 표시하지 않는다.
- 예전 정책의 `maintenance_started`·`maintenance_completed` outbox 중 미전송 메시지 또는 일부 전송 후 남은 조각은 최신 정책으로 취소한다. 이미 보낸 조각·사건·실패 감사는 보존한다.
- cycle identity·후속 계획의 실제 Merge·종료 gate 및 전송 오류와 무관한 자동 실행은 유지한다.

## Implementation

- [ ] `storage/maintenance_state.py`의 cycle 시작·종료에서 Telegram 메시지 enqueue만 제거하고 기존 평가·상태와 운영 로그를 유지한다. `notifications/events.py`·`delivery.py`의 오래된 미전송 maintenance 진행 메시지를 전송 전에 취소한다.
- [ ] 기존 selection·평가 기록을 운영 로그·status의 읽기 전용 범위 근거에 연결한다. Telegram 전용 payload·renderer를 늘리거나 새 상태 저장소를 만들지 않는다. 구현 후 PRODUCT·ARCHITECTURE의 기존 무음 진행 알림 설명을 정합화한다.

## Validation

- `test_notification_events.py`·`test_notification_delivery.py`와 maintenance fixture에서 cycle 시작·종료·재시작의 일반/무음 Telegram 0건, 독립적인 사용자 조치 사건의 정상 통지를 확인한다.
- 기존 outbox 부분 전송·재시작/실패의 남은 조각 취소와 원래 전송·감사 보존, R-074의 완료·Merge 상태 불변을 확인한다.

## Progress

- [x] 2026-10-08 기준 0e887794의 R-053·R-074, cycle 사건 작성·selection·종료·rendering과 신규 유지보수 알림 테스트를 정적으로 대조했다. 현재 payload/rendering의 범위 누락을 확인했으며 구현·테스트 실행은 하지 않았다.
- [ ] 확인된 범위 연결과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-22 정책 변경: 기존 maintenance 무음 시작/종료 메시지의 대상 범위 표시 목표는 폐기되었다. 이미 존재하는 활성 G-226의 ID·경로·Progress를 삭제하지 않고 새 정책의 통지 제거 및 운영 상태 기록 보존을 담당한다. 해당 ExecPlan의 DB 소유/실행 상태는 문서 수정만으로 완료나 취소라고 가정하지 않는다.

- complexity medium: selection·cycle 저장·종료 초기화·순수 rendering 사이의 사건 정보 보존을 여러 경계에 연결한다. 기존 상태 전이와 저장 구조를 재사용한다. risk low: 영향은 알림의 대상 설명이며 실행·Merge·receipt 계약은 변경하지 않는다. 잘못된 표시는 코드 교정으로 복구하고 기존 전송 감사는 보존한다. 필수 표시 내용 보완으로 priority normal이다.
- G-221의 제품·설계 문서 반영은 ce7c7c5→0e887794 diff에서 직접 반영되어 mutable READY snapshot의 같은 ID 계획을 삭제한다. G-221을 완료·Merge로 기록하거나 ID를 재사용하지 않는다. 이 계획은 남은 대상 범위 표시만 다루며 기존 구현에 소급 계획·검증 성공을 만들지 않는다.
- G-225의 빠른 Planner 재개와 G-222~G-224의 테스트 관측 전환은 독립 범위다. 이 delta 검토는 프로젝트 최종 전문 확인이 아니다.
