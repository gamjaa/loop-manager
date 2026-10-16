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

유지보수 시작·종료 알림에서 실제 대상 범위를 확인할 수 있게 한다. 현재 `MaintenanceRepository._notify_cycle`은 프로젝트·일반 operation·사유·plan 목록·상태를 저장하지만 평가 범위를 담지 않는다. 최초 시작과 후속 계획 없는 정상 종료는 plan 목록도 비어 있어 메시지에서 대상을 구분할 수 없다. 이미 구현된 cycle 사건 생성·종료 gate·즉시 전송은 재구현하지 않는다.

## Acceptance Criteria

- 계획 ID가 없는 cycle 시작과 후속 계획 없는 정상 종료에도 실제 평가 대상 범위, 시작/종료 사유와 다음 진행 상태가 표시된다. 전체·선정 범위는 실제 selection·평가 근거로 구분하며 프로젝트 이름이나 일반적인 단계명만으로 범위 표시를 대신하지 않는다.
- 후속 계획이 있는 정상 종료는 이번 확정 범위와 필요한 선행 dependency를 포함한 실제 Merge 대상 및 일반 작업 복귀를 표시한다. PR 대기·실패·미확정 게시·프로젝트 차단을 종료로 표시하지 않는다.
- 범위 정보는 사건 발생 때 확인된 값으로 보존된다. cycle 재개·전송 실패·재시작 및 종료 시 상태 초기화 뒤에도 다른 cycle이나 최신 원본의 범위로 바뀌지 않는다. legacy 상태는 보존된 근거로 확인 가능한 범위만 표시하고 새 평가 성공을 합성하지 않는다.
- 같은 cycle의 시작·종료는 각각 한 번의 무음 사건으로 유지하고 내부 묶음 평가·교정은 별도 알림을 만들지 않는다. 범위 변경을 이유로 새 cycle·추가 평가·성공 receipt를 만들거나 기존 종료·주기 기준을 바꾸지 않는다.
- 정책 적용 전 outbox의 미전송·부분 전송 사건은 기존 내용·주의 수준·cursor로 이어 보낸다. 새 범위 표시와 긴 메시지 분할에도 필요한 사유·대상·상태가 보존되며 전송 실패는 실행을 차단하지 않는다.

## Implementation

- planning selection/평가의 확인된 범위와 저장된 cycle 처리 범위를 notification payload·rendering에 연결한다. 기존 tiered selection·cycle identity·평가/plan 근거를 재사용하고, owning transaction에서 사건 값을 고정한다. 상세 파일 목록이나 새 저장 구조가 필요한 것으로 미리 가정하지 않는다.
- 시작 범위와 종료 처리 범위의 책임을 구분해 평가 근거 또는 확정 계획에서 실제 표시할 값을 얻는다. cycle 필드가 초기화되기 전에 필요한 값을 보존하며, renderer가 현재 DB를 다시 읽어 과거 사건을 재해석하지 않게 한다.
- `storage/maintenance_state.py`·planning state/selection 경계·`notifications/rendering.py`의 필요한 책임만 변경한다. 기존 commit 후 flush, R-074의 한 번 평가·실제 Merge 종료와 기존 outbox 호환성을 유지하고 관련 ARCHITECTURE 설명은 필요한 절에 통합한다.

## Validation

- Worker는 `tests/test_notification_events.py`의 `MaintenanceProgressEventTest`를 확장해 계획 없는 시작/종료와 후속 계획·dependency 종료의 대상·사유·상태를 최소 unit case로 검사한다. selection 관측은 명시적 double로 제공하고 실제 cycle/SQLite 저장·renderer 결과를 확인한다.
- rollback·같은 cycle 반복·저장 후 재시작·종료 필드 초기화와 서로 다른 cycle의 범위가 섞이지 않는지 검사한다. PR 대기·원격/catalog 미확정에서 종료하지 않는 기존 assertion을 유지하고, 실제 Git·외부 서비스는 호출하지 않는다.
- Manager 전체 verify는 유지보수의 단일 평가·정상 종료·다음 주기 기준과 알림의 발생 시각·무음·긴 메시지·legacy 부분 전송·실패 재시도를 보존해야 한다. 범위 표시를 검증하려고 전체 평가나 게시를 반복하지 않는다.

## Progress

- [x] 2026-10-08 기준 0e887794의 R-053·R-074, cycle 사건 작성·selection·종료·rendering과 신규 유지보수 알림 테스트를 정적으로 대조했다. 현재 payload/rendering의 범위 누락을 확인했으며 구현·테스트 실행은 하지 않았다.
- [ ] 확인된 범위 연결과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: selection·cycle 저장·종료 초기화·순수 rendering 사이의 사건 정보 보존을 여러 경계에 연결한다. 기존 상태 전이와 저장 구조를 재사용한다. risk low: 영향은 알림의 대상 설명이며 실행·Merge·receipt 계약은 변경하지 않는다. 잘못된 표시는 코드 교정으로 복구하고 기존 전송 감사는 보존한다. 필수 표시 내용 보완으로 priority normal이다.
- G-221의 제품·설계 문서 반영은 ce7c7c5→0e887794 diff에서 직접 반영되어 mutable READY snapshot의 같은 ID 계획을 삭제한다. G-221을 완료·Merge로 기록하거나 ID를 재사용하지 않는다. 이 계획은 남은 대상 범위 표시만 다루며 기존 구현에 소급 계획·검증 성공을 만들지 않는다.
- G-225의 빠른 Planner 재개와 G-222~G-224의 테스트 관측 전환은 독립 범위다. 이 delta 검토는 프로젝트 최종 전문 확인이 아니다.
