---
id: G-115
title: Exclude Owned READY Plans from Maintenance Revision
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-027] Shared Quality Guidance and Full Project Refactoring Assessment.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

유지보수 assessment가 모든 READY 계획을 개정 snapshot 후보로 가져온 뒤 기존 실행 자원을 가진 계획의 안전 검사에서 실패해 프로젝트 전체 planning을 차단하는 결함을 수정한다. 2026-10-01 운영 SQLite의 G-058은 READY이지만 ever_started=1이고 branch, worktree, workspace reservation과 Worker session을 보유했다. G-097에도 실행 이력이 있다. READY로 돌아온 기존 작업은 개정 가능한 미착수 계획과 같지 않으며, 이 정상적인 상태를 planning_error로 만들면 안전한 무인 실행이 중단된다.

## Acceptance Criteria

- assessment의 barrier 개정 대상은 READY이면서 미착수이고 branch, worktree, workspace reservation, Worker·Reviewer session과 PR 등 실행 소유권 증거가 없는 계획으로 제한한다. 실행 이력이 있는 READY는 기존 작업을 보존하며 개정 snapshot에서 제외한다.
- 제외된 READY의 정상 존재만으로 planning_error 또는 PROJECT_BLOCKED를 생성하지 않는다. 대상이 하나도 없는 경우도 유효한 유지보수 평가 입력으로 처리한다. 제외 계획은 읽기 전용 catalog에서 확인할 수 있지만 Planner에 수정 권한을 주지 않는다.
- due 평가와 미게시 assessment에서 기존 실행 자원이 있는 재개 가능한 READY를 만나면, 유지보수 개정으로 해당 작업을 선점하거나 사이클 장벽으로 영구 대기시키지 않는다. 안전한 선택 경계에서 평가를 보류하고 기존 실행을 재개할 수 있게 한다. 평가 성공 기준과 파일 ledger를 실제 평가 없이 전진시키지 않는다.
- 이미 게시된 prerequisite·refactoring 집합과 post-refactor revalidation 장벽은 그대로 보존한다. 명시된 사후 재검증 대상이 없거나 상태·소유권·revision이 snapshot과 달라지면 기존 안전 검사를 계속 적용하며 조용히 제외하지 않는다.
- 후보 선정 뒤 소유권 획득 또는 catalog revision 변경은 게시 전 다시 검증한다. 후보 제외는 snapshot 권한 검사를 약화하거나 원격 게시·SQLite 원자 적용을 우회하지 않는다.
- 기존 branch, worktree, Worker session, 실행 이력, dependency와 다른 차단 원인을 삭제하거나 초기화하지 않는다. 일반 Planner의 mutable READY 권한과 유지보수의 개정 권한을 확대하지 않는다.

## Implementation

- loop_manager.py의 _maintenance_mode_and_rows 및 maintenance planning context 생성 경계를 정리해 상태·실행 이력·소유권에 따른 개정 eligibility와 게시 후 snapshot 유효성 검사를 분리한다. 일반 mutable READY eligibility와 공통으로 쓸 수 있는 좁은 판단 함수를 재사용하되 역할별 권한 차이를 보존한다.
- maintenance_orchestration.py와 planning_runtime.py의 due 평가·미게시 assessment 선택 경계에서 기존 작업의 재개를 우선할 조건을 명시한다. 정당한 제외·평가 보류를 typed 결과로 전달하고 planning_error로 변환하지 않는다. 다른 활성 작업·publication intent·프로젝트 안전 gate를 우회하지 않는다.
- 미게시 assessment 보류는 재시작에도 유지보수 성공을 가장하지 않고 재평가 가능해야 한다. PR_OPEN·PLAN_BLOCKED의 기존 평가 보류 정책과 안전한 실행 자리 판단을 재사용한다. 유지보수 집합 게시 뒤에는 기존 장벽을 보존한다.
- 선택된 무소유 READY가 실행 자원을 얻는 race는 기존 게시 전 상태 확인으로 거부하거나 최신 snapshot으로 재시작한다. post-refactor revalidation의 명시적 대상 검증은 assessment의 후보 필터와 분리한다.
- 운영 복구는 새 계획을 원격 main에 게시한 뒤, 아직 게시 결과가 없는 assessment를 이 계획만 포함한 prerequisite_wait로 전환한다. 보존된 planning worktree의 소유권과 변경 부재를 확인하고 진단과 DB를 백업한 뒤 해당 오류·planning 실행 자리만 해제한다. G-058·G-097의 실행 row와 평가 성공 기준은 보존한다. G-115 Merge 뒤 Manager가 최신 런타임에서 assessment를 재개한다.

## Validation

- tests/test_maintenance_planning.py에서 기존 실행 이력 또는 각 소유권 필드가 있는 READY의 assessment 제외, 적격 계획 포함, 빈 대상, 명시적 사후 재검증 대상의 소유권 오류를 검증하는 targeted unit test를 작성·실행한다.
- tests/test_maintenance_orchestration.py와 tests/test_planning_runtime.py에서 G-058과 같은 기존 자원 보유 READY의 due 평가 보류·기존 실행 재개, 재시작, 평가 성공 기준 보존, 게시된 cycle 장벽 보존, 선택 뒤 ownership race의 게시 거부를 검증하는 직접 관련 targeted unit test만 실행한다.
- 운영 복구 후 planning_error가 해제되고 G-115가 cycle의 실행 가능한 선행 계획으로 선택되는지 확인한다. G-058·G-097 row와 유지보수 성공 기준이 복구 전후 동일한지 대조한다.
- 전체 verify는 Manager가 수행한다. 전체 검증에서 일반 mutable READY 권한, 유지보수 publication recovery, dependency gate, Worker session 재개와 planning 차단 사건 해제의 회귀가 없어야 한다. Worker는 전체 verify를 직접 실행하지 않는다.

## Progress

- [x] 운영 SQLite와 assessment 후보 선정·소유권 검사에서 원인 확인
- [ ] 원격 main 계획 게시 및 운영 prerequisite 복구
- [ ] eligibility와 안전한 평가 보류·기존 실행 재개 구현
- [ ] targeted unit test 및 Manager 전체 verify·Review
- [ ] 사용자 PR Merge 뒤 유지보수 평가와 기존 READY 작업 정상 진행 확인

## Decision Log

- 2026-10-01: 사용자가 계획 작성·main push·정상 진행을 위한 복구를 요청했다. 기존 PRODUCT의 무소유 READY 개정 권한과 실행 중 작업 보존 규칙을 복구하는 결함 수정이며 새로운 Requirement나 AGENTS.md 변경을 만들지 않는다.
- 단순 planning_error 삭제는 같은 assessment에서 다시 실패한다. 실행 이력이 있는 계획의 자원을 지워 미착수로 위장하거나 평가 성공 기준을 올려 cycle을 건너뛰지 않고, 미게시 cycle의 정상 prerequisite 경로로 수정 계획을 먼저 실행한다. 이 복구는 구현·검증·사용자 Merge를 대신하지 않는다.
