---
id: G-086
title: Atomic File Assessment Publication and Recovery
sources:
  - docs/requirements/[R-042] Periodic Repository-Wide Code Assessment for Token-Efficient Refactoring.md
depends_on:
  - G-082
  - G-085
complexity: high
risk: high
priority: normal
---

## Purpose

준비된 일정·inventory·파일 검토·관계 발견을 기존 유지보수 실행과 계획 게시 경계에 연결한다. 검토와 필요한 리팩터링 계획 게시가 모두 성공했을 때만 파일별 기록과 마지막 성공 파일별 평가 기준을 함께 확정한다.

## Acceptance Criteria

- G-082의 첫/후속 주기가 due이고 기존 안전 선택 조건을 충족하면 최신 원격 한 revision의 G-083 전체 inventory와 선정 집합으로 G-084/G-085 평가를 시작한다. 기존 활성 cycle은 먼저 복구·완료하고 self 일회성 G-059 기록·문서 승인 흐름을 다른 프로젝트로 확대하지 않는다.
- 선정 파일이 없으면 현재 원격 목록과 분류를 검증한 뒤 평가용 Agent 호출이나 형식적인 계획 파일 없이 마지막 성공 파일별 평가 count를 확정할 수 있다. 목록 검증 실패, 불확실한 분류 또는 source drift는 성공으로 기록하지 않는다.
- 선정 파일이 있으면 모두 검토하고 필요한 관계를 확인한 검증 결과만 수락한다. 구조 발견에 대한 새 동작 보존 리팩터링 ExecPlan은 기존 maintenance DAG·prerequisite·READY 장벽·사후 재검증 경로로 게시하며, 같은 문제의 기존 비종료 계획은 연결만 한다.
- 필요한 계획의 원격 fast-forward 게시와 catalog 적용이 확인된 뒤에만 실제 검토한 파일의 성공 ledger·코드맵과 마지막 성공 파일별 평가 MERGED 수를 하나의 SQLite transaction으로 확정한다. 검토하지 않은 파일은 갱신하지 않고 기존 일반 성공 이력을 보존한다.
- pending publication, 검증 실패, 누락 파일·관계, 잘못된 Agent 결과, 원격 race 또는 계획 게시 실패는 성공 count를 전진시키지 않는다. 중단 뒤 같은 revision의 검증된 진행 결과와 publication intent를 재사용하고, 원격 내용·경로가 바뀌면 inventory·기록·코드맵을 다시 대조한다.
- 기존 Worker·Review·전체 verify·PR 및 사용자 Merge, 프로젝트 차단·작업 공간 소유권과 유지보수 사후 재검증 안전 경계는 유지한다. R-042의 장기 규칙은 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Implementation

- `planning_runtime.py`/유지보수 coordinator의 assessment 진입과 결과 검증에 G-082~G-085 경계를 연결한다. 기존 bounded Git 변경 문맥은 보조 정보로만 두고 선정 집합의 출처가 되지 않게 한다.
- 현재 `maintenance_state.py`의 `advance_success_baseline` 및 planning publication transaction과 파일별 ledger 확정을 결합한다. 리팩터링 DAG 게시로 cycle이 계속될 때에도 파일별 성공 시점의 MERGED 수를 기록하고, 사후 재검증은 기존 cycle 완료를 담당하되 마지막 파일별 검토 시점을 임의로 다시 쓰지 않게 한다.
- 빈 대상, 평가 없음, 기존 plan 연결, 새 plan 게시, 게시 후 재개와 source drift 각각에서 한 번만 확정되는 경계를 만든다. 호환 가능한 additive 저장만 사용하고 불확실한 기존 상태는 보존한다.

## Validation

- Worker는 `tests/test_maintenance_orchestration.py`, `tests/test_maintenance_planning.py`, `tests/test_maintenance_state.py`에서 직접 바꾼 통합 경계의 targeted unit test만 실행한다.
- 첫 20개 시점의 미검토 기존 파일, 이후 변경 파일만 선정, 빈 대상의 무 Agent 완료, 기존·새 plan 연결, 원자 확정, push/SQLite 실패 재개, source drift·누락·불확실성 차단, self 전용 평가 분리와 장벽·사후 재검증을 확인한다.
- 전체 verify는 기존 maintenance publication, Worker 검증·Review, PR 소유권과 모든 등록 프로젝트의 일정 적용을 확인한다.

## Progress

- [ ] 파일별 평가 실행 및 빈 대상 완료 연결
- [ ] 계획 게시와 파일 ledger·성공 기준의 원자 확정·복구

## Decision Log

- 첫 파일별 평가 전 과거 평가 이력은 감사 자료이며 대상 파일의 성공 검토 증거가 아니다. self 전용 일회성 문서 평가는 그대로 분리한다.
