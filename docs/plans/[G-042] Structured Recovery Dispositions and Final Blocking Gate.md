---
id: G-042
title: Structured Recovery Dispositions and Final Blocking Gate
sources:
  - docs/requirements/[R-019] Automatic Archive Eligibility Recovery Before Blocking.md
depends_on:
  - G-034
  - G-037
  - G-049
complexity: high
risk: critical
priority: critical
---

## Purpose

G-031의 plan/project 차단 scope와 원인 code를 확장해 모든 사용자-visible `BLOCKED` 후보를 `TRANSIENT_RETRY`, `STATE_RECONCILE`, `AGENT_CORRECTION`, `USER_ACTION_REQUIRED` 중 하나로 결정적으로 분류한다. 자동 복구할 수 있는 앞의 세 범주는 현재 실행 상태와 작업 공간을 보존하고 차단·checkpoint·BLOCKED 알림을 만들지 않으며, 복구 정책을 소진했거나 사용자가 실제로 조치할 수 있는 마지막 범주만 기존 plan/project 차단 흐름으로 보낸다.

## Acceptance Criteria

- 차단 또는 재시도 후보는 자유 형식 오류 문자열이 아니라 enum형 disposition, 안정적인 reason code, plan ID, 현재 phase, 관측한 plan/base revision, 진단 fingerprint와 progress marker를 포함한 구조화 결과로 생성된다.
- G-031의 `PLAN_BLOCKED`/`PROJECT_BLOCKED` scope와 이 계획의 recovery disposition은 별도 축으로 유지된다. `USER_ACTION_REQUIRED`가 확정된 뒤에만 reason code의 영향 범위로 최종 scope를 선택한다.
- timeout, 일시적 서비스 오류, Git lock과 재시도 가능한 fetch·status·stage·rebase continue·worktree 복구 실패는 `TRANSIENT_RETRY`로 분류하고 같은 실행의 제한된 재시도 또는 다음 외부 실행을 위해 상태를 보존한다.
- 최신 base·catalog 시차, 유효한 원격 선언과 SQLite cache 차이 및 실제 Git 상태에서 하나로 재구성할 수 있는 phase·SHA 차이는 `STATE_RECONCILE`로 분류한다.
- Requirement archive 이동 집합 또는 rebase 충돌처럼 같은 Worker 문맥에 결정적인 피드백을 주어 고칠 수 있는 상태는 재시도 한도가 남은 동안 `AGENT_CORRECTION`으로 분류한다.
- guidance 결정, Merge 없이 닫힌 PR, 소유권·Agent 범위·외부 원격 OID 불확실성, 안전한 원본으로 하나의 상태를 확정할 수 없는 손상, 기존 한도까지 반복된 동일 verify·Review 실패와 의미를 자동 확정할 수 없는 무변경 결과는 `USER_ACTION_REQUIRED`로 유지한다.
- 서로 다른 예외를 하나의 catch에서 처리하는 경로도 예외 class 또는 명시적인 operation result로 reason code를 전달한다. 일반 `RuntimeError`의 문구를 검색해 disposition이나 block scope를 정하지 않는다.
- `TRANSIENT_RETRY`, `STATE_RECONCILE`와 재시도 가능한 `AGENT_CORRECTION`은 ExecPlan을 PLAN_BLOCKED/PROJECT_BLOCKED로 바꾸거나 G-034 checkpoint dispatcher 및 G-018 BLOCKED outbox를 호출하지 않는다.
- 자동 복구 상태에는 현재 phase, branch, worktree, workspace ownership, Worker·Reviewer session, guidance 승인, 구현 변경, 검증 원문과 실패 이력을 보존한다. 해당 원인과 무관한 상태를 초기화하지 않는다.
- 같은 프로그램 실행에서 허용된 재시도 횟수를 소진한 `TRANSIENT_RETRY` 또는 원격 snapshot 경쟁은 성공으로 위장하지 않고 프로젝트를 non-blocking pause/wait 결과로 종료하며, 다음 실행에서 같은 recovery record를 재개한다.
- `AGENT_CORRECTION`은 원인별 안정적인 fingerprint, occurrence와 progress marker를 저장한다. 각 도메인의 계획이 정한 반복 한도에 도달하기 전에는 일반 BLOCKED 해제용 사용자 문서 변경을 요구하지 않는다.
- 자동 복구를 소진해 `USER_ACTION_REQUIRED`로 승격할 때 수행한 복구 종류·횟수·마지막 안정 revision, 끝내 확정하지 못한 조건과 실제 사용자 조치 대상을 구조화된 최종 차단 record에 포함한다.
- 최종 PLAN_BLOCKED만 G-034의 안전한 원격 checkpoint 후보가 되며, PROJECT_BLOCKED와 소유권 불명 상태는 기존과 같이 원격에 쓰지 않는다.
- BLOCKED 알림은 최종 차단 transaction과 checkpoint 결과 뒤에만 생성한다. 동일 plan/scope/reason/fingerprint/revision의 재실행은 중복 알림을 만들지 않는다.
- transient·reconcile·correction 운영 로그에는 복구 원인과 재개 지점을 남길 수 있지만 `Files to edit`나 사용자 조치 경로를 추측하지 않는다. 최종 알림의 수정 경로도 reason code가 실제 수정 대상을 제공할 때만 포함한다.
- 기존 guidance 승인, 닫힌 PR reopen, verify·Review 반복 한도, Agent 범위 위반과 프로젝트 안전 차단의 해제·알림·checkpoint 계약은 유지된다.
- 장기적인 복구 disposition, 최종 BLOCKED 의미와 알림 규칙을 `docs/PRODUCT.md`에 반영하되 Requirement와 ARCHITECTURE는 수정하지 않는다.

## Implementation

- G-031의 구조화된 block reason 저장소 옆에 recovery disposition, reason code, plan/phase/revision snapshot, fingerprint, progress marker, attempt count와 마지막 결과를 저장하는 additive schema 및 repository API를 둔다. legacy 차단 상태의 의미를 소급 추정하거나 기존 실행 이력을 삭제하지 않는다.
- Git, GitHub, Agent, archive, workspace, validation과 publication 경계가 공통 `RecoveryDecision` 계열 결과를 반환하게 하고, disposition-to-next-action 및 최종 reason-to-scope mapping을 한 orchestration 경계에서 검증한다.
- 현재 `block_active_execplan*` 호출 지점을 전수 이관해 먼저 구조화 분류와 도메인 복구 가능 여부를 확인한다. 최종 차단 helper는 `USER_ACTION_REQUIRED` 결과만 받도록 만들어 우발적인 즉시 BLOCKED 전이를 코드로 거부한다.
- retry/pause, state reconciliation, Agent correction과 final block dispatcher를 분리한다. 상태 저장과 외부 동작 순서를 transaction 및 expected revision/state 조건으로 보호해 중단 후 같은 recovery action을 멱등하게 재개한다.
- G-018 알림 identity와 G-034 checkpoint payload가 최종 structured cause와 검증 revision을 사용하도록 연결하고, 운영 로그 renderer는 사용자 조치가 없는 disposition에 편집 경로를 출력하지 않는다.

## Validation

- Worker는 recovery classification/state repository와 block dispatcher의 직접 관련 test class 또는 새 전용 test module만 targeted unit test로 실행한다.
- 각 disposition의 대표 원인과 같은 `RuntimeError` container 안의 서로 다른 원인이 문자열 추론 없이 올바른 next action을 선택하는지 검증한다.
- transient retry 소진, 원격 경쟁 및 재시작이 phase·worktree·session·변경을 보존하고 BLOCKED 상태, checkpoint, 알림과 편집 경로를 만들지 않는지 검증한다.
- 아직 재시도 가능한 Agent correction과 한도 소진 correction을 구분하고 fingerprint·occurrence·progress가 프로세스 중단 뒤 이어지는지 검증한다.
- guidance 승인, 닫힌 PR, 소유권/범위 위반, 외부 원격 OID 불일치, 동일 verify·Review 반복과 무변경 결과가 최종 사용자 조치 대상으로 남는지 검증한다.
- 동일 cause/fingerprint/revision의 최종 차단 재실행에서 checkpoint 및 알림 identity가 중복되지 않고, cause나 revision이 실제로 바뀌면 새 사건으로 구분되는지 검증한다.
- legacy BLOCKED migration과 G-031의 여러 PLAN_BLOCKED/PR_OPEN scheduler, G-034 checkpoint 및 G-018 notification 회귀를 검증한다.
- 전체 verify에서 외부 CLI pause, validation failure history, guidance approval, PR reopen과 project completion 동작이 약화되지 않아야 한다.

## Progress

- [ ] 구조화 recovery disposition·원인·진전 상태와 additive migration 구현
- [ ] 기존 차단 후보 call site를 공통 분류 및 next-action gate로 이관
- [ ] non-blocking retry/reconcile/correction pause와 최종 block dispatcher 구현
- [ ] checkpoint·알림·운영 로그를 최종 structured cause에 연결
- [ ] 분류, 재시작, 소진, 최종 차단과 중복 방지 회귀 테스트 추가

## Decision Log

- R-019는 G-031의 차단 scope를 대체하지 않고, 차단 여부를 결정하기 전의 복구 가능성 축을 추가한다. scope와 disposition을 하나의 enum으로 합치면 같은 plan 원인도 일시 상태와 최종 사용자 조치를 구분할 수 없다.
- G-034와 G-037이 Merge된 뒤 시작해 final block만 원격 checkpoint 대상이 되게 하고, 최신 선언 revision에 결합된 실행 상태를 분류 입력으로 사용한다.
- 구체적인 archive, checkpoint와 rebase 복구 알고리즘은 후속 계획으로 나눈다. 이 계획은 모든 경로가 즉시 BLOCKED를 우회하지 못하게 하는 공통 상태·분류·알림 계약을 먼저 제공한다.
