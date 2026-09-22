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

- exec_plans 실행 row에 각 Worker input, archive candidate, verify, Review와 publication 결과가 어느 current definition revision을 사용했는지 durable하게 기록하고 G-036 current revision과 비교하는 공통 stage gate를 추가한다.
- declaration delta를 identity/display, scheduling/profile, Worker input/archive, validation 의미로 분류하는 결정적 invalidation matrix를 코드로 정의한다. 자유 형식 본문이나 Agent 설명으로 무효화 범위를 추론하지 않는다.
- G-031/G-029 scheduler에 unmet-current-dependency wait를 추가해 현재 실행 slot과 worktree는 보존하되 변경 단계 호출은 막고, dependency Merge와 reconciliation 뒤 기존 phase의 안전한 predecessor로 복귀시킨다.
- G-026의 latest-base rebase 및 Agent scope snapshot 경계를 재사용해 current plan/source 파일을 관리 worktree에 반영한다. manager가 만든 rebase 변화와 Worker 범위 밖 변경을 구분하고 기본 worktree는 사용하지 않는다.
- Worker prompt, blocked fingerprint, archive candidate, verify/Review 저장 및 publication preflight가 동일한 reconciled snapshot token을 요구하게 해 check와 use 사이의 원격 변경을 거부한다.

## Validation

- Worker는 execution revision gate와 scheduler resume의 직접 관련 test class 또는 새 revision-aware resume test module만 targeted unit test로 실행한다.
- IMPLEMENTING, VERIFYING, REVIEWING과 PUBLISHING 각각에서 plan body/source/path/dependency가 변경될 때 workspace·session은 유지되고 필요한 이전 결과만 무효화되는지 검증한다.
- 새 미충족 직접·간접 dependency가 Agent·verify·Review·push를 막고 Merge 뒤 같은 작업 공간과 phase 문맥에서 재개되는지 검증한다.
- source 추가·제거와 path rename이 최신 prompt, scope, BLOCKED fingerprint와 archive 후보에 반영되고 낡은 source로 독립 계획을 차단하지 않는지 검증한다.
- priority/title-only 변경, complexity·risk의 effective level 비감소, PR_OPEN/MERGED 상태 불변을 검증한다.
- Agent 전, verify·Review 전후 및 게시 직전 remote race가 낡은 결과를 폐기하고 최신 revision에서 필요한 단계만 다시 실행하는지 검증한다.
- rebase 충돌, 의미 보존 불가와 중단 복구에서 작업 공간을 보존하고 올바른 plan-scoped 원인을 기록하며 중복 Agent 호출이나 낡은 publication을 만들지 않는지 검증한다.
- 전체 verify에서 maintenance revalidation, PR conflict recovery, scoped blocking, validation failure history와 Requirement archive 재검증 흐름이 유지돼야 한다.

## Progress

- [ ] 단계별 definition revision token과 invalidation matrix 구현
- [ ] 미충족 dependency wait 및 동일 workspace 재개 구현
- [ ] 최신 plan/source worktree 반영과 Agent 입력 재생성 구현
- [ ] archive·verify·Review·publication preflight revision gate 통합
- [ ] 상태별 변경·원격 race·중단 복구 테스트 추가

## Decision Log

- 선언 cache 동기화 자체와 실행 단계 무효화를 분리해 G-036이 안정적인 snapshot·transaction API를 먼저 제공하도록 한다.
- 실행 상태를 문서 변경으로 재작성하지 않는다. 대신 각 단계 산출물에 사용한 definition revision을 기록하고 최신 선언과 맞지 않는 산출물만 결정적으로 무효화한다.
- priority 같은 scheduling-only 변경은 현재 작업을 선점하지 않지만, 구현 의미가 바뀐 결과를 재사용하지 않는 안전 경계는 게시 직전까지 반복 적용한다.
