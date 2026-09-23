---
id: G-013
title: Bounded Execution Retries and Session Recovery
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-008] Progress-Aware Verification and Review Recovery.md
depends_on:
  - G-012
complexity: high
risk: high
priority: critical
---

## Purpose

Agent, 전체 verify와 Review 실패를 서로 독립적인 실패 지문으로 추적해 새로운 문제를 해결하는 동안 계속 복구하고, 동일 실패의 반복만 제한하며 일시적 외부 실패와 사용자 조치가 필요한 실패를 명확히 구분한다.

## Acceptance Criteria

- 전체 verify와 Review에는 단계별 고정 누적 재시도 한도를 두지 않고, 새로운 실패 지문이 나타나는 동안 총 실패 횟수와 관계없이 같은 ExecPlan과 Worker 세션에서 수정을 계속한다.
- verify와 Review는 서로 다른 지문 영역으로 관리하고, 서로 다른 실패 지문의 반복 횟수를 합산하지 않는다.
- 같은 실패 지문이 Worker 수정 후 세 번 다시 발생하면 진전 없는 반복으로 판단해 현재 ExecPlan을 원인과 지문 정보와 함께 `BLOCKED`로 만든다. 중간에 다른 실패가 발생해도 이전 지문의 횟수는 유지한다.
- 전체 verify 또는 Review가 재시도 가능한 실패를 반환하면 상태, 실패 피드백과 지문 이력을 먼저 저장한 뒤 같은 프로그램 실행에서 같은 Worker 세션의 수정 단계로 되돌아가 재검증하며, 다음 스케줄 실행을 기다려야만 복구되는 중간 종료를 만들지 않는다.
- 재시도 가능한 검증 실패, 동일 지문 반복에 따른 차단, 일시적 외부 실패와 실제 프로그램 오류를 프로젝트 실행 결과에서 구분하고, 계속 수정할 수 있는 검증 실패 자체를 최상위 프로세스 오류로 반환하지 않는다.
- 실패 지문과 반복 횟수는 SQLite에 저장돼 프로세스 재실행과 다른 실패 사이에도 유지된다. verify 성공은 verify 지문 이력을, Review 성공은 Review 지문 이력을 초기화하고 정상적인 BLOCKED 입력 변경은 두 이력을 모두 초기화한다.
- Worker 내부의 targeted unit test 수정·재실행은 Loop Manager의 verify 또는 Review 재시도 횟수를 소비하지 않는다.
- Planner, Worker와 Reviewer 세션 ID를 지속하고 같은 ExecPlan이 PR 생성 준비를 마칠 때까지 실패 피드백을 같은 세션으로 전달한다.
- 저장된 세션을 더 이상 재개할 수 없으면 동일 worktree, ExecPlan, 이전 결과와 실패 내용을 제공해 새 세션을 시작하고 새 세션 ID를 원자적으로 저장한다.
- 외부 CLI 실행 실패, timeout과 명확한 일시 오류는 현재 프로그램 실행에서 최대 3회 재시도하고, 계속 실패하면 ExecPlan 상태와 작업 공간을 유지한 채 이번 실행의 해당 프로젝트 처리를 중단한다.
- AI 사용 한도 소진은 현재 실행에서 재시도하지 않는 별도 결과로 분류하며, 후속 다중 프로젝트 스케줄러가 다른 프로젝트를 계속 처리할 수 있는 반환 경계를 제공한다.
- 상태 변경과 세션 ID 저장은 중단 시 동일 단계에서 재개할 수 있도록 transaction 경계가 명확하며 호환 가능한 SQLite migration으로 기존 데이터를 보존한다.
- targeted test 실패는 Worker가 성공으로 보고할 수 없고, 전체 verify 성공 전에는 Reviewer를 실행하지 않는다.

## Implementation

- Agent 실행 결과를 성공, 일시 실패, 사용 한도 소진, 영구 설정 오류로 분류하는 공통 결과형과 retry wrapper를 만든다.
- verify와 Review 출력을 결정적으로 정규화해 단계별 실패 지문을 만들고 지문별 반복 횟수를 저장하며, 상태 전이 함수가 동일 지문 한도 검사와 `BLOCKED` 전환을 단독으로 소유하게 한다.
- 프로젝트 orchestration을 Worker 수정, verify와 Review 사이의 명시적인 progress-aware loop로 구성하고, 각 실패 상태를 commit한 뒤 새 지문이거나 동일 지문 한도가 남아 있으면 같은 실행에서 다음 단계로 진행한다.
- Reviewer도 지속 세션 경계를 사용하고, 세션 재개 실패 시 새 세션에 필요한 컨텍스트를 재구성한다.
- 현재 단일 프로젝트 실행에서도 완료, 대기, 차단, 일시 중단과 운영 오류를 구분하는 상태 보존 결과를 반환하도록 orchestration 경계를 정리해 이후 스케줄러와 CLI 종료 코드가 같은 정책을 재사용하게 한다.

## Validation

- Worker는 실행 상태 전이와 Agent retry/session 테스트 class만 targeted unit test로 실행한다.
- verify와 Review에서 서로 다른 실패가 세 번을 넘어도 계속 진행하는 경우, 같은 지문이 세 번 다시 발생해 차단되는 경우와 A→B→A 순환에서 지문별 횟수가 보존되는 경우를 검증한다.
- 시각, 절대·임시 경로와 출력 순서가 달라도 동일 지문을 생성하는 정규화, 프로세스 재실행 시 이력 보존과 단계별 성공·BLOCKED 해제 시 초기화를 검증한다.
- 첫 verify가 실패하고 Worker 수정 뒤 성공하는 시나리오가 하나의 `run` 호출 안에서 같은 세션에 실패 출력을 전달하고 Review까지 진행하며, 중간에 종료 코드 1을 반환하지 않는지 검증한다.
- 첫 Review가 실패하고 Worker 수정·verify·Review가 성공하는 시나리오, Review 수정 중 verify 실패가 Review 횟수에 합산되지 않는 시나리오와 동일 지문 한도 도달 시 정확히 한 번 `BLOCKED`로 전이하는 시나리오를 검증한다.
- 일시 CLI 실패의 실행당 재시도, timeout, 사용 한도 즉시 중단, 세션 재개와 새 세션 fallback을 검증한다.
- 전체 verify에서 기존 Planner validation 예산, targeted test 경계와 BLOCKED 해제 동작이 영향을 받지 않아야 한다.

## Progress

- [ ] 검증 단계·실패 지문별 반복 이력과 상태 전이 함수 구현
- [ ] 단일 실행 내 Worker·verify·Review progress-aware loop와 실행 결과 구분 구현
- [ ] 공통 Agent 실패 분류와 실행당 재시도 구현
- [ ] Reviewer 세션 지속 및 세션 fallback 구현
- [ ] 중단·재개와 예산 회귀 테스트 추가

## Decision Log

- Planner 형식 검증은 기존 고정 횟수 정책을 유지하고, 구현 verify와 Review는 서로 독립적인 실패 지문과 반복 이력으로 관리한다.
- 새 스케줄러를 기다리지 않고 프로젝트 단위 결과를 구조화해 이후 병렬 실행에서 재사용한다.
- 서로 다른 실패는 합산하지 않되 과거 지문이 다시 나타나면 기존 반복 횟수를 이어서 계산해 오류 사이의 순환을 무제한 진행으로 오인하지 않는다.
- 2026-09-19 G-012 전체 verify 실패에서는 `WORKING`과 실패 출력이 정상 저장됐지만 `run_project()`가 즉시 1을 반환해 다음 스케줄 실행에서만 Worker가 재개됐다. 저장 후 재개 가능한 검증 실패는 운영 오류가 아니라 같은 실행의 progress-aware retry 입력으로 처리한다.
