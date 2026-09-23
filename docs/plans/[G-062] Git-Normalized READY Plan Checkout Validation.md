---
id: G-062
title: Git-Normalized READY Plan Checkout Validation
sources:
  - docs/requirements/[R-030] Git Checkout Line Ending Compatibility for READY Plans.md
depends_on:
  - G-051
complexity: low
risk: medium
priority: critical
---

## Purpose

`_ready_plan_snapshot`은 checkout 파일 bytes와 Git tree blob을 직접 비교한다. Windows `core.autocrlf=true`의 정상 LF→CRLF checkout도 불일치로 판정해 READY 계획의 자동 실행이 차단된다.

## Acceptance Criteria

- Git이 실제로 변경되지 않았다고 판정하는 READY ExecPlan은 LF·CRLF checkout 설정 모두에서 통과한다. snapshot의 권위 본문·hash와 저장된 plan revision은 Git blob 기준으로 유지한다.
- 실제 본문 변경, 다른 plan revision, SQLite catalog 불일치와 source·dependency 오류는 기존처럼 거부한다. 사용자 Git 설정이나 저장된 계획 bytes는 변경하지 않는다.
- 일반 Planner mutable READY, 유지보수 barrier/revalidation 및 publication recovery가 같은 검증 의미를 사용한다. 공개 오류 유형, 사용자 상태 전이와 SQLite 저장 형식은 보존한다.

## Implementation

- `loop_manager.py`의 `_ready_plan_snapshot`에서 Git checkout filter를 고려한 clean 판정을 사용하고, 이후 `_ready_plan_snapshot_from_contents`와 revision 비교에는 원격 tree blob bytes를 전달한다.
- CRLF 문자열 정규화만으로 임의 수정 파일을 통과시키지 않고 Git index·tree 관계와 파일 상태를 검증한다.

## Validation

- Worker는 `tests/test_maintenance_planning.py`와 `tests/test_incremental_planning.py`의 READY snapshot 관련 targeted unit test만 실행한다.
- `core.autocrlf=true/false` 실제 checkout, 변환만 있는 파일, 본문 수정, revision·catalog 불일치 및 재개 경로를 검증한다.
- 전체 verify에서 barrier byte 보존 검증, 일반 Planner mutable snapshot, catalog reconciliation과 Git 원격 기준 회귀가 없어야 한다.

## Progress

- [ ] Git 기준 clean 판정과 blob snapshot 연결
- [ ] Windows 줄바꿈 및 실제 수정 targeted test 추가

## Decision Log

- 저장된 hash를 checkout bytes로 다시 계산하지 않고 Git 원본 blob을 계속 사용한다.
