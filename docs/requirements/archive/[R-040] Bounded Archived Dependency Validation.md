# [R-040] Bounded Archived Dependency Validation

## Context

유지보수 Planner가 선택한 선행 후보를 검증할 때, 검증용 ExecPlan 조회는 archived 계획을 모두 제외한다. `G-054`가 SQLite에서 `MERGED`이고 archive의 Merge 증명도 확인됐지만, `G-070`의 `depends_on: G-054`는 존재하지 않는 ID로 판정됐다. 반대로 모든 과거 계획을 검증 입력에 포함하면 현재 작업과 무관한 행까지 전달하게 된다.

## Requirement

Loop Manager는 선행 후보 검증에 프로젝트의 대기·진행 중인 비종료 ExecPlan과, 그 계획들의 dependency를 검증하는 데 필요한 `MERGED` ExecPlan 행만 제공한다. 필요한 범위는 직접·간접 선행 관계를 따라 결정하며, archived 계획은 안정적인 ID와 Merge 증명이 확인된 경우에만 완료된 선행 계획으로 인정한다. 관계없는 완료 계획이나 증명이 부족한 archived 계획을 완료된 dependency로 취급하지 않는다.

이 행들은 프로그램 내부의 선행 후보 검증에만 사용한다. Planner에게 전달하는 compact catalog와 계획 본문 입력은 기존처럼 비종료 active 계획으로 제한한다. 기존의 무소유 `READY` 후보 조건, 미완료 dependency의 선행 집합 포함 조건 및 실제 Worker 시작 조건은 유지한다.

## Acceptance Criteria

- `G-070`이 선행 후보이고 archived `G-054`의 안정적인 ID와 `MERGED` 증명이 확인됐으면 `G-070 -> G-054`를 없는 ID로 거부하지 않는다.
- 검증 입력에는 프로젝트의 대기·진행 중인 비종료 계획과 그 계획들의 직접·간접 dependency에 필요한 완료 계획만 포함한다. 관계없는 `MERGED` 계획은 포함하지 않는다.
- archived 계획의 ID 또는 Merge 증명이 확인되지 않거나 실제 dependency ID가 없으면 해당 선행 관계를 충족한 것으로 인정하지 않고 구체적인 검증 오류를 반환한다.
- 미완료 dependency가 선행 후보 집합에서 빠졌거나 선택한 후보가 무소유 `READY`가 아니면 기존대로 거부한다.
- archived 완료 계획의 행이나 본문을 Planner 프롬프트에 추가하지 않고, 다른 프로젝트의 계획도 검증 입력에 섞이지 않는다.

## Product Impact

- 증명이 확인된 archived `MERGED` 계획을 참조하는 대기 계획도 유효한 선행 관계로 검증할 수 있다.

## Non-Goals

- archived ExecPlan을 Planner의 작업 입력이나 실행 후보로 되돌리지 않는다.
- 전체 과거 ExecPlan 행을 검증 입력에 제공하지 않는다.

## Open Questions

- 없음
