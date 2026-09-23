---
id: G-017
title: Concurrent Registered-Project Scheduler
sources:
  - docs/PRODUCT.md
depends_on:
  - G-014
  - G-016
complexity: high
risk: high
priority: high
---

## Purpose

한 번의 외부 스케줄 실행에서 등록된 여러 프로젝트를 `max_workers` 한도 안에서 병렬 처리하되, 프로젝트 내부 작업과 Planner 호출은 안전하게 직렬화한다.

## Acceptance Criteria

- 일반 `run`은 SQLite에 등록된 프로젝트를 대상으로 하며 동시에 처리하는 프로젝트 수가 설정된 `max_workers`를 넘지 않는다.
- 서로 다른 프로젝트만 병렬로 실행하고, 같은 프로젝트에서는 Planner, 활성 ExecPlan, branch, worktree와 Worker가 항상 하나만 존재한다.
- 하나의 프로젝트에 여러 READY ExecPlan이 있어도 dependency gate와 priority/ID 순서로 하나만 활성화한다.
- 계획 입력이 바뀐 프로젝트는 실행 중인 작업이 없을 때 기존 READY Worker보다 Planner를 먼저 실행하고, 전체 프로세스에서 Planner 호출은 한 번에 하나만 수행한다.
- 한 프로젝트의 BLOCKED, dirty/ahead/diverged 기본 worktree, 일시 CLI 실패 또는 사용 한도 소진은 다른 실행 가능한 프로젝트의 처리를 중단하지 않는다.
- 기본 worktree가 안전하지 않은 프로젝트도 PR 상태 동기화, Merge 확인과 알림 event 기록 같은 읽기 전용 처리는 계속 수행한다.
- 각 worker는 독립 SQLite connection과 명시적인 transaction을 사용하며 동시에 다른 프로젝트 상태를 덮어쓰지 않는다.
- 프로세스 전체 lock은 동일 사용자 상태 디렉터리의 두 Loop Manager 실행을 계속 막고 `--skip-if-running` 동작을 유지한다.
- 중단 후 다음 실행은 프로젝트별 저장 상태와 worktree에서 재개하며 이미 활성화된 작업을 다른 worker가 중복 소유하지 않는다.
- Windows와 Linux에서 등록 프로젝트가 0개, 1개, `max_workers`보다 적거나 많은 경우를 결정적으로 처리한다.

## Implementation

- `run_project`를 한 프로젝트의 동기화/한 단계 진행 결과를 반환하는 함수로 유지하고, registry를 순회하는 상위 scheduler를 추가한다.
- 프로젝트 worker pool과 별도 Planner mutex를 두며 SQLite connection을 thread 간 공유하지 않는다.
- 프로젝트 결과를 성공, 대기, 차단, 일시 실패와 설정 실패로 집계하되 각 프로젝트의 상태 원본은 기존 SQLite 행으로 유지한다.
- 현재 run 진입점과 launcher 문서를 등록 프로젝트 기반 반복 실행 방식으로 갱신한다.

## Validation

- Worker는 scheduler와 concurrency 테스트 class만 targeted unit test로 실행한다.
- max_workers 상한, 서로 다른 프로젝트 병렬 실행, 같은 프로젝트 단일 활성 작업과 전역 Planner 직렬화를 검증한다.
- 한 프로젝트의 dirty 상태, BLOCKED, quota와 예외가 다른 프로젝트를 막지 않는지 검증한다.
- 전체 verify에서 dependency/priority 선택, 전역 lock, skip-if-running 및 단일 프로젝트 재개 동작이 유지돼야 한다.

## Progress

- [ ] 프로젝트 단위 실행 결과 경계와 scheduler 구현
- [ ] worker pool, Planner mutex와 SQLite connection 격리 구현
- [ ] 프로젝트별 실패 격리와 읽기 전용 동기화 구현
- [ ] 다중 프로젝트 동시성 및 재개 테스트 추가

## Decision Log

- 동일 프로젝트 병렬화는 도입하지 않고 동시성은 등록 프로젝트 사이에만 허용한다.
- 외부 스케줄러와 전역 process lock은 유지하며 내장 스케줄러를 추가하지 않는다.
