---
id: G-024
title: Defer Replanning Until READY Catalog Exhaustion
sources:
  - docs/requirements/[R-001] Automation-First Planning and Priority.md
depends_on:
  - G-017
complexity: medium
risk: high
priority: high
---

## Purpose

활성 작업이 없는 프로젝트에서도 `READY` ExecPlan이 하나라도 남아 있으면 계획 입력 변경보다 기존 카탈로그 실행을 우선하고, 모든 `READY` 계획을 소진한 뒤에만 최신 원격 입력에 대한 새 계획 주기를 시작한다. 이미 구현된 compact catalog, dependency/priority 선택과 입력 지문 재사용은 유지하면서 R-001의 남은 scheduling 정책을 G-017의 등록 프로젝트 scheduler에 일관되게 적용한다.

## Acceptance Criteria

- 이 계획이 도입되는 단계에서는 `IMPLEMENTING`, `VERIFYING`, `REVIEWING`, `PUBLISHING`, `PR_OPEN` 또는 `BLOCKED` ExecPlan이 있는 프로젝트는 기존처럼 해당 작업의 처리·대기·복구를 먼저 수행하고 Planner나 다른 Worker를 시작하지 않는다. 여러 Merge 대기 PR과 독립 READY 진행은 G-026과 G-029 뒤의 G-031에서 활성화한다.
- 활성 작업이 없고 `READY` ExecPlan이 하나라도 있으면 현재 `origin/<base-branch>`의 계획 입력이 저장된 지문과 달라도 Planner와 planning worktree를 시작하지 않는다.
- 의존성을 충족한 `READY` 계획이 있으면 기존 dependency gate를 적용한 뒤 `critical`, `high`, `normal`, `low`와 숫자 ID 순서로 하나를 선택해 Worker를 시작한다.
- `READY` 계획은 남아 있지만 모두 `depends_on`을 충족하지 못한 프로젝트는 READY가 없는 것으로 취급하지 않고, 새 Planner를 실행하거나 새 ExecPlan을 만들어 우회하지 않은 채 대기 상태를 반환한다.
- 새 PRODUCT, Requirement, ARCHITECTURE, AGENTS, 코드, 테스트 또는 설정 변경은 실행 중인 작업과 남은 READY 카탈로그를 선점하지 않으며, 마지막 READY 계획의 PR Merge가 확인되고 활성 작업도 없어진 다음 선택 시점까지 보류된다.
- 활성 작업과 `READY` ExecPlan이 모두 없을 때만 최신 `origin/<base-branch>`에서 계획 입력 지문을 계산해 Planner 실행 여부를 판단한다.
- 마지막으로 정상 완료한 계획 주기의 지문과 최신 입력 지문이 같으면 Planner를 호출하지 않고, 다르면 최신 원격 기준 planning worktree에서 Planner를 정확히 한 계획 주기 시작한다.
- Planner가 유효한 계획 batch 또는 추가 계획 없음으로 정상 종료한 경우에만 해당 입력 지문을 완료된 계획 주기로 저장하며, CLI·검증·게시 실패는 기존 `planning_error`와 재시도 정책을 유지한다.
- 등록 프로젝트 scheduler는 위 gate를 프로젝트별로 적용한다. 한 프로젝트의 dependency 대기는 다른 실행 가능한 프로젝트의 Worker나 허용된 Planner 실행을 막지 않으며, 전체 Planner mutex와 `max_workers` 한도는 유지한다.
- 비종료 compact catalog, 새 ExecPlan batch 검증, priority/ID 결정 순서, MERGED 계획 제외와 archive-only 지문 예외는 변경하지 않는다.
- 현재 PRODUCT의 “입력 변경 시 기존 READY보다 Planner 우선” 흐름과 Business Rules를 R-001의 READY 소진 후 재계획 정책으로 같은 구현 PR에서 갱신한다.

## Implementation

- G-017의 프로젝트 scheduling 경계에 활성 ExecPlan, 전체 `READY` 존재 여부와 실행 가능한 `READY` 선택을 분리한 결정 함수를 둔다.
- 활성 작업이 없을 때 전체 `READY` 존재 여부를 먼저 확인하고, 존재하면 계획 입력 지문 검사와 planning state 생성을 건너뛴 뒤 실행 가능한 계획을 선택하거나 dependency 대기를 반환한다.
- `READY`가 없을 때만 기존 planning fingerprint, archive-only 변화, planning error와 planning worktree 복구 흐름으로 진입하도록 프로젝트 상태 전이를 재배치한다.
- READY 소진 뒤에는 그동안 누적된 원격 입력을 한 번의 최신 지문으로 평가하고, 성공한 계획 주기만 저장하는 기존 원자적 등록·게시 경계를 재사용한다.
- 장기 제품 규칙이 R-001과 일치하도록 `docs/PRODUCT.md`의 ExecPlan 생성 흐름, 작업 선택 규칙과 관련 invariant를 구현과 함께 최소 범위로 갱신한다.

## Validation

- Worker는 READY/Planner 선택 결정 함수와 등록 프로젝트 scheduler의 관련 test class 또는 개별 test case만 targeted unit test로 실행한다.
- 계획 입력이 변경된 상태에서 실행 가능한 READY 계획이 있으면 Planner가 아니라 Worker가 시작되는 경우를 검증하고, 기존의 반대 기대를 가진 회귀 테스트를 R-001 정책으로 교체한다.
- dependency가 충족되지 않은 READY만 남은 경우 Planner·Worker 모두 시작하지 않고 대기하며 READY 행과 지문 상태를 보존하는지 검증한다.
- 여러 READY 계획의 dependency, priority와 ID 순서는 그대로 유지되고 입력 변경이 해당 선택을 선점하지 않는지 검증한다.
- 마지막 READY 계획 Merge 뒤 최신 입력 지문이 다를 때 Planner를 한 번 실행하고, 같을 때는 반복 실행에서도 Planner를 호출하지 않는지 검증한다.
- 활성 상태별 재개 우선, planning error 재시도, Planner 결과 검증·게시 실패, archive-only 이동과 compact catalog 회귀를 검증한다.
- G-017 scheduler에서 READY 대기 프로젝트와 재계획 가능 프로젝트가 함께 있을 때 프로젝트 간 진행과 전역 Planner 직렬화가 유지되는지 검증한다.
- 전체 verify에서 기존 Agent 세션 복구, 최신-base publication, 완료 판정과 알림 주기 테스트가 새 gate로 약화되지 않아야 한다.

## Progress

- [ ] 프로젝트별 READY 존재 우선 gate와 dependency 대기 결과 구현
- [ ] READY 소진 뒤 planning fingerprint 평가 및 상태 전이 재배치
- [ ] 단일·다중 프로젝트 scheduling 회귀 테스트 갱신
- [ ] PRODUCT의 장기 계획 순서 규칙 정합화

## Decision Log

- R-001은 현재 코드와 PRODUCT에 구현된 입력 변경 우선 정책을 READY 카탈로그 소진 우선 정책으로 바꾸는 활성 Requirement다. 기존 동작을 구현한 계획을 다시 작성하지 않고 남은 정책 차이만 다룬다.
- G-017은 현재 PRODUCT 정책으로 다중 프로젝트 scheduling 경계를 도입하므로 기존 계획 파일을 수정하지 않고, 해당 경계가 Merge된 뒤 이 계획에서 R-001 정책을 적용한다.
- dependency를 충족하지 못한 READY도 계획된 작업이므로 Planner 실행 조건을 만들기 위해 숨기거나 삭제하지 않는다.
- R-001의 최종 Merge 대기 비점유 정책은 다중 PR 동기화와 충돌 복구가 준비되기 전 부분 활성화하지 않는다. G-024는 READY 소진 순서만 먼저 도입하고, G-031이 이 계획의 scheduler 경계를 확장해 안정된 `PR_OPEN`과 독립적인 READY 실행을 허용한다.
