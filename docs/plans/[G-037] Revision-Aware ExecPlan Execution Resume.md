---
id: G-037
title: Revision-Aware ExecPlan Execution Resume
sources:
  - docs/requirements/[R-017] File-Authoritative ExecPlan Source Reconciliation.md
depends_on:
  - G-036
  - G-049
complexity: high
risk: critical
priority: critical
---

## Purpose

G-036이 동기화한 최신 선언 revision을 실행 수명주기의 안전 gate로 연결한다. 실행 중인 계획의 정의가 바뀌어도 branch, worktree와 재개 가능한 Agent session은 보존하되, 낡은 입력·archive 후보·검증 결과로 Agent 호출이나 게시를 진행하지 않고 최신 원격 계획과 source를 반영한 정확한 단계에서 재개한다.

## Acceptance Criteria

- IMPLEMENTING, VERIFYING, REVIEWING 또는 PUBLISHING 계획의 current definition revision이 바뀌어도 기존 branch, worktree, workspace ownership, Worker·Reviewer session, retry·failure history와 PR 복구 정보는 삭제하거나 새 계획으로 교체하지 않는다.
- 프로그램은 다음 Agent 호출, 전체 verify, Review, commit·rebase 결과 승인과 branch/PR 게시 직전에 원격 base를 갱신하고 G-036 reconciliation을 수행한다. 사용하려던 revision과 달라지면 해당 단계의 낡은 결과를 소비하지 않고 최신 revision에서 gate를 다시 평가한다.
- 실행 중 계획에 새로 추가된 직접 또는 간접 dependency가 아직 MERGED가 아니면 작업 공간과 상태를 보존한 채 Worker, verify, Review와 게시를 진행하지 않는다. dependency가 충족된 다음 실행에서 같은 plan identity와 작업 공간으로 재개하며 미충족 dependency의 변경을 현재 계획의 실패나 차단으로 세지 않는다.
- current plan path, title, sources 또는 본문이 바뀌면 다음 Worker 입력과 plan/source fingerprint를 최신 값으로 다시 만들고, 최신 원격 base를 관리 worktree에 안전하게 반영한 뒤 같은 Worker session을 재개한다. rename된 plan path와 제거·추가된 source가 낡은 경로로 prompt 또는 범위 검증에 남지 않는다.
- depends_on, sources, plan path 또는 구현·Acceptance Criteria·Validation 본문처럼 구현·검증 의미에 영향을 주는 변경은 기존 worker result, archive 후보, successful verify와 Review 결과 및 validated base/revision을 무효화한다. 변경 이후에는 최신 입력을 사용한 Worker 단계와 전체 verify·Review를 다시 통과해야 게시할 수 있다.
- title만 바뀐 경우 최신 로그·알림·prompt 표시를 사용하고, priority만 바뀐 경우 현재 실행을 선점하거나 통과한 구현·검증 결과를 지우지 않는다. 두 경우 모두 게시 직전 current revision 일치 검사는 유지한다.
- complexity와 risk 변경은 G-036이 계산한 새 initial level을 이후 Worker 선택에 사용하되 effective level은 기존 값과 새 initial level 중 낮아지지 않는 값으로 유지하고 Review 승격 이력을 지우지 않는다.
- source 변경 뒤 BLOCKED 재평가 fingerprint와 guidance 외 일반 해제 조건은 최신 plan path와 source 집합을 사용한다. 원인 계획이 아닌 다른 계획의 과거 source cache 때문에 차단 해제가 불가능해지지 않는다.
- Requirement archive 후보는 Worker prompt 생성 직전과 게시 직전에 최신 동기화 관계로 다시 계산한다. 후보가 바뀌면 허용 범위와 Worker 결과를 최신 후보에 맞추고 전체 verify와 Review를 다시 수행한다.
- 정의 변경을 worktree에 반영하는 rebase에서 충돌하거나 최신 선언과 기존 Worker 변경의 의미를 안전하게 결합할 수 없으면 G-031의 실제 대상 plan에 대한 구조화된 원인으로 보존·차단하며, 무관한 계획을 원인으로 기록하거나 작업 공간을 폐기하지 않는다.
- PR_OPEN 및 MERGED row도 G-036의 선언 cache 갱신과 최신 title/path 조회는 받지만 파일 변경만으로 실행 상태를 되돌리거나 Merge를 추정하지 않는다. 기존 PR 링크와 GitHub 소유 상태는 그대로 유지한다.
- 프로세스가 reconciliation, worktree 최신화 또는 결과 무효화 사이에 중단돼도 저장된 applied definition revision과 stage revision을 대조해 같은 단계에서 멱등하게 재개하며, 낡은 성공 결과로 건너뛰지 않는다.

## Implementation

- 새 `execution_revision.py`에 declaration delta 분류, deterministic invalidation matrix와 stage gate 결과를 정의한다. 파일 parsing과 cache 갱신은 G-036 service에 남기고 이 모듈은 이전/현재 definition token 및 phase artifact token만 비교한다.
- `execution_runtime.py`의 immutable `ExecutionContext`와 plan metadata에 current definition/base token 및 Worker input·archive·verify·Review·publication stage token을 명시하고, `runtime_state.py` repository가 expected snapshot에 결합해 이 token과 무효화를 원자적으로 저장한다.
- `ExecutionPhaseCoordinator`는 각 Agent·verify·Review·publication dispatch 전에 injected revision/dependency gate를 호출한다. `execution_handlers.py`는 typed `continue`, `wait`, `refresh-worktree`, `invalidate-to-phase`, `final-safety-error` 결과를 적용하고 원격 catalog나 raw SQL을 직접 조회하지 않는다.
- unmet current dependency는 prepare 단계의 `STABLE_WAIT` outcome으로 반환해 execution identity와 workspace를 보존하고 외부 port 호출을 막는다. 최신 plan/source 반영은 기존 `LatestBasePort`가 수행하고 성공 뒤 gate가 지정한 가장 이른 phase로만 되돌린다.
- Worker prompt/archive scope, blocked fingerprint, validation 저장과 publication preflight가 같은 reconciled snapshot token을 요구하도록 Agent·Archive·Validation·Publication effect 계약을 좁게 확장한다. `loop_manager.py`에는 concrete port wiring과 legacy helper adapter만 남긴다.

## Validation

- Worker는 새 `tests/test_execution_revision.py`, `tests/test_execution_runtime.py`, `tests/test_execution_handlers.py`와 `tests/test_runtime_state.py`의 직접 관련 targeted case만 실행한다.
- IMPLEMENTING, VERIFYING, REVIEWING과 PUBLISHING에서 plan body/source/path/dependency delta별 invalidation matrix가 workspace·session을 보존하고 필요한 stage artifact만 지우는지 pure table test로 검증한다.
- 새 미충족 직접·간접 dependency가 `STABLE_WAIT`를 반환해 Agent·verify·Review·Git port를 호출하지 않고 Merge 뒤 같은 execution snapshot에서 재개되는지 coordinator fake로 검증한다.
- source 추가·제거와 path rename이 최신 prompt/scope/BLOCKED fingerprint/archive 후보에 반영되고 priority/title-only 및 complexity·risk 변경은 정의된 최소 무효화만 수행하는지 handler test로 검증한다.
- Agent 전, verify·Review 전후와 publication 직전 token race에서 stale outcome 적용을 거부하고 최신 revision의 가장 이른 필요 phase로 돌아가는지 repository CAS test로 검증한다.
- latest-base rebase 충돌·의미 보존 불가·중단 복구가 workspace를 보존하고 올바른 plan-scoped 결과를 반환하며 중복 Agent/Git 효과를 만들지 않는지 검증한다.
- 전체 verify에서 maintenance revalidation, PR conflict recovery, scoped blocking, validation history와 Requirement archive 재검증 회귀가 없어야 한다.

## Progress

- [ ] `execution_revision.py` delta 분류와 invalidation matrix 구현
- [ ] ExecutionContext/runtime-state stage revision token과 CAS 저장 구현
- [ ] coordinator pre-dispatch dependency/revision gate 및 stable wait 구현
- [ ] latest-base·Agent·archive·validation·publication token 통합
- [ ] phase별 delta·race·중단 targeted 회귀 테스트 추가

## Decision Log

- 선언 cache 동기화 자체와 실행 단계 무효화를 분리해 G-036이 안정적인 snapshot·transaction API를 먼저 제공하도록 한다.
- 실행 상태를 문서 변경으로 재작성하지 않는다. 대신 각 단계 산출물에 사용한 definition revision을 기록하고 최신 선언과 맞지 않는 산출물만 결정적으로 무효화한다.
- priority 같은 scheduling-only 변경은 현재 작업을 선점하지 않지만, 구현 의미가 바뀐 결과를 재사용하지 않는 안전 경계는 게시 직전까지 반복 적용한다.
- Post-refactor revalidation: revision-aware resume는 `_run_project_step` 분기가 아니라 G-049의 immutable context와 phase dispatch 전 gate로 구현한다. 파일 reconciliation, phase sequencing과 persistence를 각각 G-036, execution handler, runtime-state repository에 유지한다.
