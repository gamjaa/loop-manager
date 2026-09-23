---
id: G-005
title: Bounded Planner Validation and Planning Error Gate
sources:
  - docs/PRODUCT.md
depends_on:
  - G-004
complexity: medium
risk: medium
---

## Purpose

Planner가 잘못된 변경 범위나 유효하지 않은 ExecPlan을 반복해서 만들 때 동일 세션에서 제한된 횟수만 수정하게 하고, 계속 실패한 계획 입력은 프로젝트 단위 `planning_error`로 고정해 외부 스케줄러가 같은 실패를 무한 반복하지 않도록 한다.

## Acceptance Criteria

- Planner 결과의 변경 범위, ExecPlan frontmatter 또는 전체 카탈로그·의존성 검증이 실패하면 구체적인 검증 오류를 같은 Planner 세션에 전달하고 최초 실패 후 최대 3회까지 수정과 재검증을 수행한다.
- 검증 재시도 횟수, Planner 세션 식별자, 마지막 검증 오류와 계획 입력 지문을 SQLite에 저장해 프로세스가 중단돼도 같은 planning worktree와 세션에서 남은 재시도를 이어간다.
- 세 번째 수정 결과도 유효하지 않으면 ExecPlan 상태를 만들지 않고 프로젝트의 `planning_error`에 원인과 해당 계획 입력 지문을 저장하며 planning worktree, 검증 기록과 diff를 보존한다.
- 해결되지 않은 `planning_error`의 지문이 현재 계획 입력 지문과 같으면 Planner를 다시 호출하거나 프로젝트를 완료 처리하지 않고, 오류 원인과 보존된 planning worktree 경로를 출력한다.
- 현재 계획 입력 지문이 저장된 오류 지문과 다르면 이전 검증 오류와 diff를 로그에 보존하고 이전 planning worktree를 정리한 뒤, `planning_error`, 재시도 횟수와 Planner 세션을 초기화해 최신 `origin/<base-branch>`에서 새 계획을 시작한다.
- 유효한 ExecPlan 생성 또는 변경 없는 정상 Planner 결과에서는 임시 검증 오류, 재시도 횟수와 Planner 세션 정보를 정리하며, G-004의 계획 입력 지문 저장 조건은 유지한다.
- Planner CLI 실행 실패, timeout, 계획 게시 중 base 변경과 push 실패는 ExecPlan 결과 검증 실패 횟수로 계산하지 않으며 기존 복구 경계를 유지한다.
- 기존 SQLite DB는 저장된 프로젝트 상태를 잃지 않는 호환 가능한 migration으로 새 계획 오류 필드를 추가한다.
- 첫 재시도에서 수정 성공, 재시도 한도 소진, 같은 지문의 재실행 차단, 입력 변경 후 새 planning worktree 시작과 기존 DB migration을 단위 테스트로 검증한다.

## Implementation

- 프로젝트 상태에 계획 검증 재시도 횟수, Planner 세션 ID, `planning_error` 원인과 오류 지문을 저장할 nullable 필드를 추가하고 기존 DB에 보존적 migration을 적용한다.
- Codex Planner 실행 경계를 세션 시작과 재개로 나누고, 검증 실패 진단을 같은 세션에 전달할 수 있도록 세션 ID를 실행 전에 또는 실행 결과와 함께 영속화한다.
- Planner가 반환한 변경을 검증하는 경로를 반복 가능한 단계로 분리해 범위 검사와 `load_execplan_catalog` 오류만 계획 검증 재시도 예산을 소비하게 한다.
- 재시도 한도에 도달하면 planning worktree를 제거하지 않고 오류 지문과 진단 정보를 고정하며, 다음 실행 초기에 동일 지문 차단을 적용한다.
- 입력 지문이 달라진 경우 기존 planning worktree의 상태와 diff를 프로젝트 로그에 남긴 다음 강제 제거가 허용된 Planner 전용 worktree만 정리하고 새 지문 상태를 만든다.
- 유효한 계획 게시, 계획 없음, 원격 기준 변경과 게시 실패 경로가 `planning_error` 및 재시도 상태를 잘못 저장하거나 완료 지문을 조기에 기록하지 않도록 상태 정리 함수를 구분한다.
- Worker와 Reviewer 세션 재개, 전체 ExecPlan 런타임 상태 모델, 외부 CLI 자체의 실행당 3회 재시도는 후속 계획으로 남긴다.

## Validation

- `scripts/verify.ps1`
- `scripts/verify.sh`
- Planner 세션 재개와 검증 실패 횟수 경계 단위 테스트
- `planning_error`의 동일 지문 차단, 입력 변경 초기화, planning worktree 및 진단 보존 단위 테스트
- 기존 SQLite schema migration과 G-004 계획 지문 회귀 단위 테스트

## Progress

- [ ] Planner 세션 및 계획 검증 재시도 상태 영속화
- [ ] 최대 3회 수정과 `planning_error` 전환 구현
- [ ] 동일 지문 차단 및 변경 지문 복구 구현
- [ ] 정상·실패·migration 경계 단위 테스트 추가

## Decision Log

- Worker/Reviewer의 전체 세션 및 재시도 모델보다 먼저 Planner 검증 실패의 무한 반복을 차단한다. `planning_error`는 ExecPlan 상태가 아니라 프로젝트 단위 계획 게이트로 유지한다.
