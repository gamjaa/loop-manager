---
id: G-076
title: Bounded Archived Dependency Validation
sources:
  - docs/requirements/[R-040] Bounded Archived Dependency Validation.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

유지보수 Planner의 선행 후보 검증은 archived ExecPlan 행을 모두 제외해, Merge가 증명된 `G-054`를 참조하는 READY `G-070`도 존재하지 않는 dependency로 거부한다. 현재 활성 작업과 그 선행 관계에 필요한 완료 계획만 검증 입력에 포함해 이 차단을 해소한다.

## Acceptance Criteria

- 선행 후보 검증에는 해당 프로젝트의 비종료 active ExecPlan 전체와 이 계획들의 직접·간접 dependency에 필요한 `MERGED` ExecPlan 행만 제공한다. 관계없는 `MERGED` 행과 다른 프로젝트의 행은 제외한다.
- archived dependency는 같은 프로젝트에서 안정적인 plan ID와 archive identity 및 Merge 증명이 확인되고 SQLite 상태가 `MERGED`인 경우에만 충족된 선행 계획으로 인정한다. 이 조건을 만족하는 `G-054`를 참조한 `G-070` 선행 후보를 검증할 수 있다.
- archived ID·identity·Merge 증명이 부족하거나 dependency ID 자체가 없으면 해당 관계를 완료로 간주하지 않고 원인과 관련 plan ID를 식별할 수 있는 검증 오류를 낸다.
- 선택한 선행 후보의 무소유 `READY` 조건, 미완료 dependency의 선행 집합 포함 조건, 순환 관계 검증과 실제 Worker 시작 dependency gate를 유지한다.
- archived 완료 계획의 행 또는 본문은 Planner prompt·compact catalog·실행 후보에 추가하지 않는다.

## Implementation

- `loop_manager.py`의 planning state port가 `planning_runtime.py`의 선행 후보 검증에 제공하는 `execplan_rows` 조회를 비종료 active 행에서 시작하는 bounded dependency closure로 바꾼다. 같은 프로젝트의 저장된 dependency를 따라 필요한 완료 행만 추가하며, unrelated 완료 이력은 조회 결과에서 제거한다.
- archived 완료 행을 포함할 때 `execplan_catalog_history`의 현재 archive 표식, 검증된 stable identity 및 Merge 증명을 SQLite `MERGED` 상태와 결합해 확인한다. 필요한 archived 관계를 확인하지 못한 경우 `maintenance_orchestration.py` 검증 경계에 구체적인 오류를 전달한다.
- 내부 검증 조회만 변경하고 `compact_execplan_catalog`와 Planner 본문 입력, 유지보수 snapshot 권한, 실제 READY 선택·Worker 시작 경로는 변경하지 않는다.

## Validation

- Worker는 `tests/test_maintenance_orchestration.py`와 `tests/test_planning_runtime.py`의 관련 targeted unit test 및 새 bounded 조회 테스트만 실행한다.
- 직접 archived `MERGED` dependency `G-070 -> G-054`, 여러 단계의 간접 dependency, 공유된 선행 관계와 관계없는 `MERGED` 행 제외를 검증한다.
- archived identity 또는 Merge 증명 누락·불일치, 실제 없는 ID, 다른 프로젝트 행, 미완료 dependency 누락, 소유된 READY 후보를 각각 거부하는지 검증한다.
- 전체 verify에서 compact catalog·Planner prompt에 archived 계획이 유입되지 않고 기존 실행 dependency gate와 유지보수 cycle 흐름이 유지돼야 한다.

## Progress

- [ ] 선행 관계에 한정한 조회와 archived Merge 증명 검증 구현
- [ ] 정상·오류 경로 targeted test 추가

## Decision Log

- 조회 범위는 유지보수 Planner의 선행 후보 검증에 한정한다. 일반 Planner 입력과 실행 후보 범위를 넓히지 않는다.
