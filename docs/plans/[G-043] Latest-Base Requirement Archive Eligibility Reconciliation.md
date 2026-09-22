---
id: G-043
title: Latest-Base Requirement Archive Eligibility Reconciliation
sources:
  - docs/requirements/[R-019] Automatic Archive Eligibility Recovery Before Blocking.md
depends_on:
  - G-042
  - G-049
complexity: high
risk: critical
priority: critical
---

## Purpose

관리 branch가 최신 base보다 뒤처지거나 원격 ExecPlan 선언과 SQLite cache의 반영 시점이 달라 Requirement archive eligibility를 확정하지 못하는 경우, 이를 `STATE_RECONCILE`로 복구한다. 최신 원격·PR·파일 원본 catalog를 하나의 안정된 snapshot으로 맞춘 뒤 소유가 확인된 현재 worktree를 최신 base에 반영하고 archive 후보와 실제 이동 집합을 다시 계산해, 사용자가 수정할 것이 없는 시차를 BLOCKED로 노출하지 않는다.

## Acceptance Criteria

- archive eligibility 또는 이동 검증에서 Git/SQLite catalog 불일치가 발견되면 즉시 차단하지 않고 G-042의 `STATE_RECONCILE` record를 저장한 뒤 최신 원격 base와 관련 PR 상태를 갱신한다.
- 현재 실행 revision에 SQLite 등록 계획이 없더라도 저장된 plan ID·최초/현재 path·게시 revision이 유효하고 최신 `origin/<base-branch>`의 검증된 G-036 catalog에서 같은 ID를 하나로 확인할 수 있으면 latest-base 시차로 판정한다.
- 최신 원격 전체 active/archived catalog가 유효하면 G-036으로 path, title, sources, depends_on, profile 입력, priority, content hash와 current plan revision cache를 동기화하고 과거 cache 차이만으로 현재 또는 독립 계획을 차단하지 않는다.
- 최신 원격의 plan 누락·중복 ID·invalid frontmatter·없는 source/dependency·cycle처럼 G-036 검증을 통과하지 못한 상태를 유효한 시차로 가장하지 않는다. 최신 게시 이력까지 확인해도 대응을 하나로 확정할 수 없을 때만 실제 catalog/project 사용자 조치로 승격한다.
- 현재 branch, 결정적 worktree, workspace ownership과 local HEAD가 저장된 plan에 속함을 확인한 뒤에만 G-014/G-037의 latest-base 반영 흐름을 사용한다. 소유권이 불명확하면 rebase, reset, clean, stash 또는 강제 갱신을 수행하지 않는다.
- latest-base 반영 전후에 현재 ExecPlan, 구현 변경, Worker·Reviewer session, guidance 승인, 검증 원문과 실패 이력을 보존한다. reconciliation 자체를 verify·Review 실패나 일반 BLOCKED 해제로 계산하지 않는다.
- 최신 base의 새 계획이 archive 대상 Requirement와 관련 없고 다른 상태 조건도 바꾸지 않으면 기존 후보를 유지한다. 새 계획의 최신 sources가 관련 있거나 상태 조건을 바꾸면 해당 Requirement를 후보에서 제외한다.
- 최신 eligibility와 worktree의 실제 Requirement 이동 집합이 다르면 같은 Worker correction 문맥으로 기대 집합과 revision을 전달한다. 최신 집합으로 수정한 뒤 전체 verify와 Review를 다시 통과하기 전에는 게시하지 않는다.
- 후보, catalog cache, 이동 집합과 검증 결과는 동일한 base OID 및 catalog snapshot token에 결합한다. 계산 중이나 transaction 적용 전후에 원격 base 또는 관련 SQLite 상태가 바뀌면 부분 결과와 낡은 eligibility를 적용하지 않고 최신 snapshot에서 다시 시작한다.
- 한 실행 안의 bounded reconcile 동안 원격 base가 계속 바뀌면 현재 phase와 worktree를 보존한 non-blocking pause로 종료하고 다음 외부 실행에서 다시 시도한다. 이 경쟁만으로 PLAN_BLOCKED/PROJECT_BLOCKED, checkpoint 또는 BLOCKED 알림을 만들지 않는다.
- reconciliation, rebase, Worker correction 또는 snapshot 저장 중 프로세스가 중단돼도 다음 실행은 저장된 target revision과 실제 Git 상태를 대조해 중복 rebase·commit·Agent correction 없이 같은 단계에서 재개한다.
- archive 목적지 충돌, invalid ExecPlan 상태, 최신 원격에서도 사라진 계획, 대응 불명 또는 확정할 수 없는 source 관계는 자동 복구 이력을 포함한 `USER_ACTION_REQUIRED`로 승격하고 실제 원인 plan/source/archive path만 안내한다.
- stale branch에 비관련 계획이 추가된 경우, 관련 계획이 추가된 경우, 선언 cache만 오래된 경우와 연속 base 변경을 서로 다른 자동 테스트로 검증한다.
- G-036의 파일 원본 선언, G-037의 revision-aware 무효화와 기존 Requirement archive 내용 보존·충돌 거부 계약을 유지한다.

## Implementation

- Requirement archive 계산을 고정 base OID, G-036 catalog revision과 ExecPlan state snapshot을 입력으로 받는 순수 eligibility 결과로 분리하고, 후보·관련 plan 상태·이동 집합 및 snapshot token을 함께 반환한다.
- `RequirementArchiveError`를 latest-base/catalog lag, retryable remote observation, Agent-correctable move delta와 final invalid catalog/archive collision로 구분하는 typed result로 교체한다. G-042 dispatcher가 각 결과를 해당 recovery action에 연결하게 한다.
- reconciliation service는 fetch 및 전체 Open PR 동기화, G-036 catalog reconciliation, current workspace ownership 검증, G-037 latest definition/worktree 반영, 동일 snapshot eligibility 재계산 순서를 명시적으로 실행한다.
- 현재 Worker 입력, archive 후보, verify·Review와 publication preflight가 같은 snapshot token을 요구하게 하고 후보 변경 시 G-037의 stage invalidation과 기존 Worker session correction을 사용한다.
- bounded remote-race attempt와 pending target snapshot을 durable하게 기록하고 compare-and-swap으로 완료해 fetch 후 중단, rebase 후 저장 전 중단과 최신 base 재변경에서 부분 cache나 낡은 이동을 게시하지 않는다.

## Validation

- Worker는 archive eligibility snapshot/reconciliation과 publication preflight의 직접 관련 test class 또는 새 전용 test module만 targeted unit test로 실행한다.
- stale 관리 branch에서 새 비관련 ExecPlan이 추가되면 최신 catalog를 동기화하고 기존 archive 후보를 유지한 채 차단 없이 진행하는지 검증한다.
- 새 관련 ExecPlan이나 상태가 추가되면 후보를 제거하고 같은 Worker session에 최신 이동 집합을 전달한 뒤 verify·Review를 다시 수행하는지 검증한다.
- SQLite의 path, title, sources, depends_on 또는 revision cache만 오래된 경우 G-036 값으로 원자 갱신하고 독립 계획을 차단하지 않는지 검증한다.
- fetch/reconciliation/rebase 중 base가 한 번 또는 계속 변경되는 경우 낡은 eligibility를 쓰지 않으며 bounded 소진은 알림 없는 pause와 다음 실행 재개가 되는지 검증한다.
- rebase·Worker correction·snapshot transaction 각 중단 지점에서 worktree, session, guidance 결정과 구현 diff가 보존되고 중복 동작 없이 재개되는지 검증한다.
- 최신 원격에도 plan이 없거나 ID/source/dependency 대응이 불명확한 경우, archive 목적지 충돌과 invalid state만 최종 block이 되고 복구 시도 및 정확한 사용자 대상이 기록되는지 검증한다.
- 전체 verify에서 G-005 Planner validation, G-036/G-037 catalog revision, Requirement archive 이동 검증과 latest-base publication 회귀가 없어야 한다.

## Progress

- [ ] revision-pinned archive eligibility snapshot과 typed 결과 구현
- [ ] fetch·PR·catalog·workspace·latest-base reconciliation pipeline 구현
- [ ] 후보 변경 Worker correction 및 verify·Review invalidation 연결
- [ ] 원격 경쟁·중단 복구용 durable target snapshot과 CAS 적용
- [ ] 비관련/관련 계획, stale cache, 경쟁과 최종 오류 테스트 추가

## Decision Log

- archive 안전 조건을 완화하지 않는다. Git/SQLite 불일치를 무시하는 대신 최신 신뢰 원격과 G-036 cache를 먼저 일치시킨 뒤 같은 revision에서 다시 판정한다.
- G-042 이후에 구현해 자동 복구 가능한 시차가 checkpoint나 알림을 만들지 않게 하고, G-037의 실행 revision gate를 재사용해 별도의 선언 동기화 경로를 만들지 않는다.
- 최신 plan 존재와 저장된 게시 이력으로 시차를 증명할 수 있는 경우만 자동 rebase한다. catalog 자체가 invalid하거나 소유권이 불명확한 경우에는 이 계획이 원격 또는 worktree를 수정하지 않는다.
