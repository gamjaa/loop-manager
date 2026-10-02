---
id: G-125
title: Split Planning Lifecycle and Assessment Services
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-124
complexity: high
risk: high
priority: critical
---

## Purpose

`planning_runtime.py`의 coordinator는 2,168줄에 입력 선택, workspace, Planner 호출, 파일·관계 검토, 결과 검증, 게시와 READY activation을 함께 담당한다. 게시 method만 460줄이며 일회성 `full_assessment`와 상시 파일 평가 경로가 결합돼 있다. 기존 결정·repository·publication intent를 활용해 단계와 평가 책임을 분리한다.

## Acceptance Criteria

- 입력 선택·안전 gate, workspace/소유권, Planner 호출·scope, 파일/관계 평가, 결과 검증, planning publication/recovery와 activation을 필요한 typed port를 가진 service로 나눈다. coordinator는 저장된 phase를 읽고 해당 단계를 진행하는 역할만 맡는다.
- 일반 planning과 maintenance의 공통 source/ownership/publication 검증은 같은 계약의 부분만 재사용한다. mutable READY, barrier, post-refactor revalidation의 서로 다른 수정 권한을 보존한다.
- 명세/전체 입력 지문, 자체 게시·archive 예외, 오류 사건, 성공 기준과 cycle publication의 transaction 경계를 유지한다. 검증·원격 게시·catalog 반영 전 성공 기준을 전진시키지 않는다.
- 파일 inventory·ledger·source-bound code map·관계/발견 검토는 기존 maintenance service를 이용한다. 재시작에서 이미 검토한 같은 identity를 재사용하되 source drift·소유권 변경 시 오래된 결과를 거부한다.
- `full_assessment.py`의 소비는 하나의 명시적인 평가 경계로 모으고 G-127에서 필요한 runtime 대체 경로를 연결할 수 있게 한다. 이 단계에서는 미완료 일회성 평가·문서 검토와 저장 감사 정보를 삭제하거나 성공 처리하지 않는다.
- `tests/test_planning_runtime.py`, `tests/test_maintenance_planning.py`, `tests/test_maintenance_orchestration.py` 등 큰 planning·maintenance 테스트를 입력 선택, 권한, publication 복구, 파일 평가와 cycle 상태별로 나누고 test-module fixture import를 제거한다.

## Implementation

1. `_select_planning_work`, `_reconcile_inputs`, `_run_planner_turn`, `_validate_planner_result`, `_publish_planner_result`의 효과와 durable state를 기존 `planning_catalog`, maintenance/file-review/publication service에 대응시킨다.
2. immutable planning model/좁은 contracts, 단계별 service와 coordinator를 분리한다. service 사이에 coordinator 전체나 가변 `_PlanningRun`을 넘기지 않고 필요한 snapshot과 typed 결과만 전달한다.
3. 일반·maintenance 게시의 동일한 lease/원격 확인/intent 복구를 통합하되 각 mode의 scope·원래 목표 보존 검증과 사후 장벽을 독립적으로 유지한다.
4. 일회성 평가의 inventory·prompt·결과 검증을 명시적인 assessment port로 연결한다. G-127의 제거 전까지 기존 serialized report와 pending cycle 재개를 그대로 지원한다.
5. planning·maintenance 테스트를 기능별로 분리하고 G-119 support를 사용한다. code-only 입력에서 신규 Planner role을 도입하는 R-049 구현은 포함하지 않는다.

## Validation

- Worker는 변경한 planning 단계, mode 권한, assessment adapter와 publication intent의 관련 targeted unit test만 실행한다.
- 같은 입력 재관측, source drift, READY의 실행 이력/소유권 변경, 게시 전후 중단, catalog 실패, 유지보수 prerequisite·DAG·사후 재검증 장벽을 확인한다.
- 빈 파일 평가, ledger 재사용, 오래된 code map 거부, 일회성 평가 중단·재개와 잘못된 report의 실패를 검증한다. 평가 실패를 추가 작업 없음이나 평가 완료로 처리하지 않는다.
- Manager 전체 verify에서 일반/유지보수 planning, document draft, publication 회복과 다음 Worker 선택 순서가 유지돼야 한다. Reviewer는 mode별 권한·성공 기준·transaction이 약화되지 않았는지 읽기 전용으로 확인한다.

## Progress

- [ ] planning 단계·권한·평가·publication 대응표 확인
- [ ] 단계별 service·assessment port 추출과 같은 계약의 중복 제거
- [ ] planning/maintenance test 분리 및 targeted unit test
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: 이미 있는 planning·maintenance coordinator와 source-bound 검토 기록을 활용한다. 새 평가 시스템이나 새 SQLite schema를 만들지 않는다.
- `full_assessment.py`가 현재 runtime에서 사용되므로 먼저 소비 경계를 분리한다. module 제거와 임시 코드 정리는 모든 기능 분리 뒤 G-127에서 실제 참조와 복구 호환성을 확인하고 수행한다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
