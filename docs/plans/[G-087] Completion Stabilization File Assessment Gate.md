---
id: G-087
title: Completion Stabilization File Assessment Gate
sources:
  - docs/requirements/[R-042] Periodic Repository-Wide Code Assessment for Token-Efficient Refactoring.md
depends_on:
  - G-086
complexity: medium
risk: medium
priority: normal
---

## Purpose

정기 주기 전에 프로젝트가 실제 완료 후보가 됐을 때만 한 번의 안정화 평가 기회를 제공한다. 첫 파일별 평가 전이나 검토할 파일이 없는 프로젝트의 완료 알림은 불필요하게 늦추지 않는다.

## Acceptance Criteria

- 현재 PRODUCT·ARCHITECTURE 목표가 충족되고 일반 Planner가 추가 작업 없음으로 확인했으며 비종료 ExecPlan, 활성 작업, 프로젝트 차단이 없는 완료 후보에서만 안정화 조건을 평가한다.
- 성공한 파일별 평가 이력이 있고 그 뒤 새 MERGED가 10개 이상이며 설정 정기 주기에는 못 미치고 G-083의 새 검토 대상이 있을 때만 G-086의 동일한 파일별 평가를 완료 알림 전에 한 번 수행한다. 정기 주기에 도달하면 정기 평가 한 번으로 처리한다.
- 첫 파일별 평가 전, 10개 미만 또는 확인된 빈 선정 집합에서는 완료 알림을 보류하지 않는다. 날짜 경과, 같은 파일 내용, 반복 실행만으로 안정화 평가를 다시 시작하지 않는다.
- inventory 검증 실패나 불확실한 대상 분류를 빈 선정 집합으로 오인하지 않으며, 실패·중단된 안정화 평가와 게시 단계는 기존 유지보수 복구 경로에서 재개한다. 성공 뒤 완료 조건을 다시 확인한다.
- 기존 작업 우선순위, READY·prerequisite·PR_OPEN·PLAN_BLOCKED·PROJECT_BLOCKED gate와 완료 알림의 중복 방지 계약은 유지한다.

## Implementation

- `loop_manager.py`의 완료 후보 판단과 유지보수 due 경계에 G-082의 성공 파일별 평가 count와 G-083의 선정 결과를 사용한 안정화 trigger를 추가한다. 검토 대상이 있을 때만 기존 G-086 assessment cycle로 진입한다.
- 완료 알림 직전의 최신 원격 revision과 일반 Planner 성공 기준을 재확인하고, 정기 due와 안정화 due가 겹치면 하나의 cycle만 예약한다. 빈 대상·10개 미만 결과는 durable 기준에 따라 반복 호출 없이 완료 처리한다.

## Validation

- Worker는 `tests/test_maintenance_state.py`의 안정화 due targeted unit test와 `tests/test_maintenance_orchestration.py` 및 직접 변경한 완료 경계의 targeted unit test만 실행한다.
- 과거 평가만 있는 첫 파일별 평가 전, 9/10개, 정기 주기 경계, 빈 선정·새 대상·불확실한 목록, 반복 실행·재시작, 활성 작업/차단/대기 plan, 실패 후 재개와 완료 알림 한 번을 검증한다.
- 전체 verify는 프로젝트 완료 판정, 알림 중복 방지와 정기 유지보수 cycle 회귀를 확인한다.

## Progress

- [ ] 완료 후보의 안정화 trigger와 빈 대상 단축 경로
- [ ] 중복 cycle 방지 및 완료 알림 회귀 검증

## Decision Log

- 안정화 평가는 G-086과 같은 파일별 검토·확정 계약을 재사용하며 별도 평가 형식을 만들지 않는다.
