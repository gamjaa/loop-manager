# [R-001] Automation-First Planning and Priority

## Context

현재 Planner는 하나 이상의 ExecPlan을 만들 수 있지만 현재 입력에서 식별 가능한 작업 전체를 한 번에 계획해야 한다는 보장이 없다. READY 계획이 하나라도 있으면 새 계획 입력도 다음 계획 주기까지 반영되지 않고, 실행 순서는 숫자 ID만 사용하므로 사용자의 반복 개입을 줄이는 자동화 기반 작업보다 다른 기능이 먼저 실행될 수 있다.

Loop Manager의 목적은 안전을 유지하면서 계획, 구현, 검증, 복구와 다음 작업 선택을 사용자의 개입 없이 최대한 계속하는 것이다. 따라서 다음 Worker를 시작하기 전에 전체 작업 카탈로그와 자동화 중심 우선순위를 확보해야 한다.

## Requirement

계획 입력이 변경되고 실행 중인 작업이 없으면 Loop Manager는 기존 READY ExecPlan의 존재와 관계없이 Planner를 먼저 실행해야 한다. Planner는 기존 카탈로그와 현재 입력을 대조해 현재 식별 가능한 모든 미충족 작업을 독립적으로 검증하고 Merge할 수 있는 ExecPlan으로 일괄 작성해야 한다.

각 ExecPlan에는 코드가 검증하고 정렬할 수 있는 고정 priority를 기록한다. 값과 순서는 다음과 같다.

1. `critical`: 안전한 무인 실행을 막거나 데이터 손실·보안 위험 및 반복적인 수동 복구를 유발하는 작업
2. `high`: 정상 경로에서 사용자의 반복 개입을 제거하고 계획부터 PR 생성까지의 핵심 자동화를 완성하는 작업
3. `normal`: 그 밖의 Current Goal과 필수 유지보수
4. `low`: 무인 완료에 필요하지 않은 편의 기능과 최적화

실행 중인 작업은 새 입력이나 더 높은 priority로 선점하지 않는다. 다음 Worker 선택 시에는 `depends_on`을 모두 충족한 READY 계획 중 priority가 가장 높은 계획을 선택하고, 같은 priority에서는 숫자 ID가 가장 작은 계획을 선택한다.

## Acceptance Criteria

- 계획 입력이 변경되고 IMPLEMENTING, VERIFYING, REVIEWING, PR_OPEN 또는 BLOCKED 작업이 없으면 READY ExecPlan이 남아 있어도 다음 Worker보다 Planner가 먼저 실행된다.
- Planner는 현재 입력에서 식별 가능한 미충족 작업을 하나만 작성하고 종료하지 않고, 독립적으로 검증·Merge 가능한 전체 ExecPlan 집합과 의존성 그래프를 한 계획 commit에 작성한다.
- 이미 완료됐거나 기존 ExecPlan이 충족하는 작업을 중복 계획하지 않는다.
- ExecPlan의 priority는 `critical`, `high`, `normal`, `low` 중 하나이며 공통 형식과 코드 파서가 검증하고 SQLite에 등록한다. 기능 도입 전에 작성된 ExecPlan에는 `normal`을 적용한다.
- 실행 가능한 READY 계획은 dependency gate를 먼저 적용한 뒤 `critical`, `high`, `normal`, `low` 순서와 숫자 ID 오름차순으로 결정적으로 선택된다.
- 안전한 무인 실행 차단 또는 반복적인 사용자 복구를 제거하는 계획이 편의 기능이나 선택적 최적화보다 먼저 선택된다.
- 새 입력은 실행 중이거나 Merge 대기 중인 작업을 중단하지 않으며, 해당 작업이 끝난 다음 Worker 선택 전에 반영된다.
- 입력이 변경되지 않고 기존 카탈로그가 현재 요구사항을 모두 포함하면 Planner를 불필요하게 다시 호출하지 않는다.
- priority 추가와 선택 순서, 기존 계획 호환성, 변경된 입력이 READY 계획보다 먼저 재계획되는 경계를 자동 테스트로 검증한다.

## Product Impact

- 현재 입력에서 식별 가능한 작업은 다음 구현 시작 전에 한 번에 ExecPlan 카탈로그로 작성된다.
- 작업 순서는 숫자 ID만이 아니라 안전한 무인 자동화라는 제품 목적을 우선한다.
- 실행 중인 작업은 선점하지 않으며 같은 우선순위에서는 결정적인 순서를 유지한다.

## Non-Goals

- 실행 중인 Worker, PR_OPEN 또는 해결되지 않은 BLOCKED 작업을 중단하거나 우회하지 않는다.
- 같은 프로젝트의 ExecPlan을 병렬 실행하지 않는다.
- 제품 문서에 없는 작업을 자동으로 추가하지 않는다.

## Open Questions

- 없음
