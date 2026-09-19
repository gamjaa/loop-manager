---
id: G-013
title: Bounded Execution Retries and Session Recovery
sources:
  - docs/PRODUCT.md
depends_on:
  - G-012
complexity: high
risk: high
priority: critical
---

## Purpose

Agent, 전체 verify와 Review 실패를 영구적으로 반복하지 않고 저장된 세션과 단계별 재시도 예산 안에서 복구하며, 일시적 외부 실패와 사용자 조치가 필요한 실패를 명확히 구분한다.

## Acceptance Criteria

- 최초 전체 verify 실패 후 최대 3회의 Worker 수정·verify 재시도를 허용하고, 예산을 모두 사용하면 현재 ExecPlan을 원인과 함께 `BLOCKED`로 만든다.
- 최초 Review 실패 후 최대 3회의 Worker 수정·verify·Review 재시도를 별도 예산으로 허용하고, 예산을 모두 사용하면 `BLOCKED`로 만든다.
- 단계별 재시도 횟수는 SQLite에 저장돼 프로세스 재실행으로 초기화되지 않으며 성공 또는 정상적인 BLOCKED 입력 변경 때만 해당 예산을 초기화한다.
- Worker 내부의 targeted unit test 수정·재실행은 Loop Manager의 verify 또는 Review 재시도 횟수를 소비하지 않는다.
- Planner, Worker와 Reviewer 세션 ID를 지속하고 같은 ExecPlan이 PR 생성 준비를 마칠 때까지 실패 피드백을 같은 세션으로 전달한다.
- 저장된 세션을 더 이상 재개할 수 없으면 동일 worktree, ExecPlan, 이전 결과와 실패 내용을 제공해 새 세션을 시작하고 새 세션 ID를 원자적으로 저장한다.
- 외부 CLI 실행 실패, timeout과 명확한 일시 오류는 현재 프로그램 실행에서 최대 3회 재시도하고, 계속 실패하면 ExecPlan 상태와 작업 공간을 유지한 채 이번 실행의 해당 프로젝트 처리를 중단한다.
- AI 사용 한도 소진은 현재 실행에서 재시도하지 않는 별도 결과로 분류하며, 후속 다중 프로젝트 스케줄러가 다른 프로젝트를 계속 처리할 수 있는 반환 경계를 제공한다.
- 상태 변경과 세션 ID 저장은 중단 시 동일 단계에서 재개할 수 있도록 transaction 경계가 명확하며 호환 가능한 SQLite migration으로 기존 데이터를 보존한다.
- targeted test 실패는 Worker가 성공으로 보고할 수 없고, 전체 verify 성공 전에는 Reviewer를 실행하지 않는다.

## Implementation

- Agent 실행 결과를 성공, 일시 실패, 사용 한도 소진, 영구 설정 오류로 분류하는 공통 결과형과 retry wrapper를 만든다.
- verify와 Review 재시도 횟수를 단계별로 저장하고 상태 전이 함수가 예산 검사와 BLOCKED 전환을 단독으로 소유하게 한다.
- Reviewer도 지속 세션 경계를 사용하고, 세션 재개 실패 시 새 세션에 필요한 컨텍스트를 재구성한다.
- 현재 단일 프로젝트 실행에서도 상태 보존 결과를 반환하도록 orchestration 경계를 정리해 이후 스케줄러가 같은 정책을 재사용하게 한다.

## Validation

- Worker는 실행 상태 전이와 Agent retry/session 테스트 class만 targeted unit test로 실행한다.
- verify와 Review의 최초 실패, 재시도 성공, 단계별 3회 소진 및 프로세스 재실행을 검증한다.
- 일시 CLI 실패의 실행당 재시도, timeout, 사용 한도 즉시 중단, 세션 재개와 새 세션 fallback을 검증한다.
- 전체 verify에서 기존 Planner validation 예산, targeted test 경계와 BLOCKED 해제 동작이 영향을 받지 않아야 한다.

## Progress

- [ ] 단계별 retry 상태와 전이 함수 구현
- [ ] 공통 Agent 실패 분류와 실행당 재시도 구현
- [ ] Reviewer 세션 지속 및 세션 fallback 구현
- [ ] 중단·재개와 예산 회귀 테스트 추가

## Decision Log

- Planner 형식 검증 재시도와 구현 verify/Review 재시도는 서로 다른 예산과 상태로 유지한다.
- 새 스케줄러를 기다리지 않고 프로젝트 단위 결과를 구조화해 이후 병렬 실행에서 재사용한다.
