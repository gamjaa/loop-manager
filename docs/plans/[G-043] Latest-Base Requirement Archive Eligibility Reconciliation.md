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

- `loop_manager.py`의 Requirement archive source/candidate/move 계산을 새 `requirement_archive.py`의 revision-pinned pure snapshot API로 옮긴다. base OID, G-036 catalog token과 ExecPlan state snapshot을 입력으로 candidate·related status·move set·snapshot token을 한 결과에 담는다.
- 새 `requirement_archive_recovery.py` service가 fetch/PR observation, catalog reconciliation, workspace ownership, G-037 revision gate와 eligibility 재계산 순서를 조정하고 G-042 `RecoveryDecision`을 반환한다. Git·GitHub·Worker correction은 주입 port이며 service가 phase row나 subprocess를 직접 다루지 않는다.
- G-049 `ArchiveEffects`를 snapshot/validate/reconcile port로 교체하고 `ExecutionContext`가 현재 archive snapshot token과 candidate scope를 보유하게 한다. prepare, Worker result, latest-base와 publication preflight는 같은 token을 요구하고 변경 시 handler가 지정된 predecessor phase로만 돌아간다.
- 새 `requirement_archive_state.py` repository가 bounded remote-race attempt와 pending target snapshot을 저장하고 G-042 recovery record에는 그 identity와 disposition만 연결한다. `runtime_state.py` expected execution snapshot과 compare-and-swap으로 적용해 부분 catalog cache나 낡은 move set을 commit하지 않는다.
- `RequirementArchiveError` compatibility wrapper는 typed lag/remote-retry/move-delta/final-invalid 결과로 변환하는 바깥 adapter에 한정하고, `execution_handlers.py` 안에서 즉시 `block_active_execplan`을 호출하는 분기를 제거한다.

## Validation

- Worker는 새 `tests/test_requirement_archive.py`, `tests/test_requirement_archive_recovery.py`와 `tests/test_execution_handlers.py`의 직접 관련 targeted case만 실행한다.
- stale branch의 비관련 새 plan, 관련 새 plan과 cache-only path/title/sources/depends_on/revision delta를 고정 snapshot fake로 검증한다.
- 후보 변경이 같은 Worker session의 typed correction과 stage invalidation을 만들고 최신 move set으로 verify·Review를 다시 통과하기 전 publication port를 호출하지 않는지 검증한다.
- fetch/catalog/worktree/rebase 순서, 단일·연속 base race와 bounded pause가 stale token을 적용하지 않고 BLOCKED/checkpoint/outbox 없이 다음 실행에 재개되는지 repository CAS test로 검증한다.
- rebase, Worker correction과 snapshot transaction 각 중단 지점에서 worktree, session, guidance와 diff가 보존되고 중복 rebase·Agent turn이 없는지 검증한다.
- latest remote의 missing/ambiguous plan, invalid source/dependency/state와 archive collision만 final user action으로 승격하고 실제 plan/source/archive path를 보존하는지 검증한다.
- G-036 definition token, G-037 invalidation gate와 archive move content-preservation 계약이 하나의 snapshot을 공유하는지 targeted integration case로 검증한다.
- 전체 verify에서 Planner validation, catalog reconciliation, Requirement 이동 검증과 latest-base publication 회귀가 없어야 한다.

## Progress

- [ ] revision-pinned Requirement archive snapshot API 구현
- [ ] typed reconciliation service와 target-snapshot repository 구현
- [ ] ExecutionContext archive token과 handler phase invalidation 통합
- [ ] durable target snapshot·remote race CAS 및 중단 복구 구현
- [ ] 관련/비관련 plan·cache·collision targeted 회귀 테스트 추가

## Decision Log

- archive 안전 조건을 완화하지 않는다. Git/SQLite 불일치를 무시하는 대신 최신 신뢰 원격과 G-036 cache를 먼저 일치시킨 뒤 같은 revision에서 다시 판정한다.
- G-042 이후에 구현해 자동 복구 가능한 시차가 checkpoint나 알림을 만들지 않게 하고, G-037의 실행 revision gate를 재사용해 별도의 선언 동기화 경로를 만들지 않는다.
- 최신 plan 존재와 저장된 게시 이력으로 시차를 증명할 수 있는 경우만 자동 rebase한다. catalog 자체가 invalid하거나 소유권이 불명확한 경우에는 이 계획이 원격 또는 worktree를 수정하지 않는다.
- Post-refactor revalidation: Requirement archive 계산은 G-049 `ArchiveEffects` 뒤의 snapshot service로 추출하고 handler는 token 변화에 따른 phase outcome만 적용한다. catalog, Git reconciliation과 Worker correction 제어를 한 함수에 다시 섞지 않는다.
