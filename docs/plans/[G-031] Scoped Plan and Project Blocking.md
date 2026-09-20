---
id: G-031
title: Scoped Plan and Project Blocking
sources:
  - docs/requirements/[R-013] Scoped Plan and Project Blocking.md
depends_on:
  - G-018
  - G-020
  - G-026
  - G-029
complexity: high
risk: critical
priority: high
---

## Purpose

현재의 단일 `BLOCKED` 상태와 프로젝트 전체 정지 동작을 구조화된 `PLAN_BLOCKED`와 `PROJECT_BLOCKED` 경계로 교체한다. 계획 내부 문제는 해당 계획과 dependency 후속 계획만 보류하고 독립적인 READY 계획을 기존 직렬·우선순위 규칙으로 계속 처리하되, 저장소·공유 Git 상태·작업 공간의 안전을 증명할 수 없는 경우에는 프로젝트 전체의 변경 작업을 중단한다.

## Acceptance Criteria

- 차단 전이는 자유 형식 오류 문자열을 해석하지 않고 코드가 생성한 구조화된 원인 code, scope, 관련 ExecPlan ID, 판단 근거와 해제 fingerprint를 하나의 transaction으로 저장한다.
- verify 또는 Review 동일 지문 반복, guidance 승인, 계획별 사용자 결정, Merge 없이 닫힌 PR과 안전한 관리 worktree 안의 계획 내부 충돌은 `PLAN_BLOCKED`로 분류된다.
- 기본 worktree mutation, 소유하지 않은 공유 ref 또는 `refs/codex/turn-diffs/*` mutation, 관리 worktree 밖 변경, workspace 소유권 불명과 안전한 실행 위치를 결정할 수 없는 저장소 상태는 `PROJECT_BLOCKED`로 분류된다.
- 관리 worktree 내부의 범위 위반과 외부·공유 Git mutation을 동일한 generic Agent 오류로 뭉치지 않고 실제 영향 경계에 맞는 구조화된 code로 기록한다.
- `PLAN_BLOCKED` 행은 프로젝트의 유일한 실행 자리를 점유하지 않으며 여러 계획이 동시에 `PLAN_BLOCKED`로 남을 수 있다. IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING 또는 PR_OPEN인 현재 작업은 프로젝트별 최대 하나로 유지된다.
- READY 선택은 직접 또는 간접 dependency가 `PLAN_BLOCKED`이거나 MERGED가 아닌 계획을 건너뛰고, 독립적으로 실행 가능한 계획 하나만 기존 priority와 숫자 ID 순서로 선택한다.
- 하나 이상의 `PLAN_BLOCKED`가 있어도 독립 READY 계획은 실행하지만, 독립 READY가 모두 소진되면 일반 Planner나 유지보수 Planner를 시작해 차단을 우회하지 않고 대기한다.
- `PROJECT_BLOCKED` 동안 Planner, maintenance Planner, Worker와 저장소 변경 단계는 시작하지 않는다. GitHub PR 상태 확인과 durable notification dispatch처럼 저장소를 바꾸지 않는 관리 작업은 계속할 수 있다.
- 계획별 입력 변경, guidance 결정과 닫힌 PR 재개 조건은 대상 `PLAN_BLOCKED`만 READY 또는 PR_OPEN으로 되돌리고 다른 차단 계획의 fingerprint, 원인과 작업 공간을 변경하지 않는다.
- 프로젝트 안전이 검증되면 `PROJECT_BLOCKED`만 해제하고 이전 실행 상태 또는 READY 선택을 복구한다. 같은 ExecPlan에 별도 계획 내부 원인이 남아 있으면 해당 계획은 `PLAN_BLOCKED`로 유지한다.
- 계획 처리 중 프로젝트 범위 원인이 발견되면 현재 단계와 작업 공간을 보존한 채 즉시 `PROJECT_BLOCKED`로 승격하고 다른 Worker를 시작하지 않는다.
- 기존 `BLOCKED` 기록은 저장된 structured guidance, validation, PR 또는 scope 정보로 범위를 결정할 수 있을 때만 대응 상태로 이관한다. 범위를 증명할 수 없는 legacy 기록은 `PROJECT_BLOCKED`로 보수적으로 이관한다.
- 프로젝트 제거, compact catalog, dependency 검증, 재시작 복구와 완료 판정은 새 범위를 인식한다. 어느 범위의 차단이든 남아 있으면 프로젝트 완료 또는 완료 알림을 만들지 않는다.
- G-018의 BLOCKED outbox event에는 scope, 구조화된 원인과 관련 ExecPlan을 포함하고 scope·원인·계획 identity가 같은 사건만 중복 제거한다.
- PR reopen/Merge 동기화와 G-026 충돌 복구는 특정 계획의 저장된 row를 사용하며, 다른 `PLAN_BLOCKED`가 존재한다는 이유만으로 현재 독립 작업을 덮어쓰거나 project mirror를 잘못 hydrate하지 않는다.
- 장기 제품 규칙과 상태 전이 설명을 R-013에 맞게 `docs/PRODUCT.md`에 갱신하되 Requirement와 ARCHITECTURE는 수정하지 않는다.

## Implementation

- ExecPlan별 block reason record와 프로젝트별 project-block record를 additive SQLite migration으로 추가한다. 실행 중인 계획의 단일성 index에서는 `PLAN_BLOCKED`를 제외하고, project block과 plan block이 함께 존재할 수 있는 명시적인 repository API를 둔다.
- `active_execplan`, project mirror hydration, READY 선택, BLOCKED 해제, PR 동기화와 프로젝트 결과 분류를 현재 실행 plan, plan-blocked 집합과 project-block record를 각각 조회하도록 분리한다. 여러 blocked row 중 임의의 첫 행을 active plan으로 취급하지 않는다.
- 모든 `block_active_execplan*`, validation failure, guidance 승인, PR close/conflict, workspace 복구와 Agent scope call site가 enum형 reason code를 반드시 전달하도록 바꾸고 reason-to-scope mapping을 한 코드 경계에서 검증한다.
- PLAN_BLOCKED 재평가는 각 계획의 원래 활성 path, branch, worktree, PR과 fingerprint를 사용해 독립적으로 순회한다. project mirror는 현재 실행 중인 하나의 계획만 반영하고 blocked 계획의 durable 실행 문맥은 `exec_plans` row에 유지한다.
- G-024와 G-029의 선택 순서를 project block gate, current execution recovery, due maintenance gate, 독립 READY selection, blocked-only wait, 일반 planning과 완료 판정 순으로 확장한다. maintenance prerequisite와 barrier dependency도 blocked dependency를 우회하지 못하게 한다.
- G-018 outbox key와 payload builder, project registry removal guard 및 legacy schema migration을 새 scope에 맞게 확장한다. migration과 state transition은 반복 실행해도 같은 결과가 되도록 한다.

## Validation

- Worker는 새 scoped-blocking test module 또는 상태 repository와 scheduler의 직접 관련 test class만 targeted unit test로 실행한다.
- verify/review 반복, guidance 승인, 닫힌 PR, 계획 내부 충돌과 관리 worktree 내부 범위 위반이 `PLAN_BLOCKED`가 되는 경우를 검증한다.
- 기본 worktree·공유 ref·외부 worktree mutation, workspace ownership 불명과 저장소 손상이 `PROJECT_BLOCKED`가 되고 어떤 Planner나 Worker Git 쓰기도 시작하지 않는지 검증한다.
- 여러 PLAN_BLOCKED, blocked dependency의 직접·간접 closure, 독립 READY priority 선택과 프로젝트 내 단일 Worker를 검증한다.
- blocked-only catalog가 Planner 및 maintenance 평가를 시작하지 않는 경우, project block 중 read-only PR/notification 동기화, 모든 block이 완료 판정과 완료 알림을 막는 경우를 검증한다.
- 계획별 해제, PR reopen, project block 해제 뒤 남은 plan block, plan에서 project scope로 승격과 프로세스 재실행을 검증한다.
- legacy BLOCKED의 안전한 변환과 불명확 기록의 보수적인 project block 이관, notification identity/payload 및 project removal guard를 검증한다.
- 전체 verify에서 G-018 알림 멱등성, G-020 guidance 승인, G-024 READY 소진, G-026 PR 충돌 복구와 G-029 유지보수 주기 회귀가 없어야 한다.

## Progress

- [ ] 구조화된 차단 원인·scope schema와 legacy migration 구현
- [ ] 모든 차단 call site의 결정적 분류와 plan/project 전이 구현
- [ ] 다중 PLAN_BLOCKED 저장·해제와 독립 READY scheduler 구현
- [ ] PR·maintenance·registry·완료·알림 경계 통합
- [ ] scoped blocking 및 회귀 테스트 추가

## Decision Log

- G-018, G-020, G-026과 G-029가 각각 알림, guidance 전용 차단, PR 충돌 및 최종 scheduling 경계를 도입한 뒤 전체 차단 call site를 한 계약으로 이관한다. 일부 call site만 새 상태를 사용하면 같은 원인이 실행 경로에 따라 프로젝트 전체 또는 계획 하나를 임의로 막을 수 있다.
- PLAN_BLOCKED는 실행 slot이 아니지만 durable plan 상태다. 프로젝트 phase 하나로 여러 blocked plan을 표현하지 않고 ExecPlan row를 원본으로 유지한다.
- R-013은 기존보다 자동 진행 범위를 넓히므로 영향 범위를 증명하지 못한 legacy 또는 workspace 오류는 PROJECT_BLOCKED로 보수적으로 처리한다.
