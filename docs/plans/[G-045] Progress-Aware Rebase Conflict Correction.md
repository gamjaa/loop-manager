---
id: G-045
title: Progress-Aware Rebase Conflict Correction
sources:
  - docs/requirements/[R-019] Automatic Archive Eligibility Recovery Before Blocking.md
depends_on:
  - G-044
complexity: high
risk: critical
priority: critical
---

## Purpose

안전하게 보존된 publication 또는 owned PR rebase 충돌을 한 번의 Worker·Agent 비정상 종료나 일시적인 stage/continue 실패만으로 차단하지 않는다. 충돌 상태와 Worker 결과의 결정적인 진전을 추적해 같은 session 또는 기존 fallback으로 correction을 계속하고, 동일 충돌과 동일 무진전 결과가 Worker correction 뒤 세 번 재발하거나 Git 상태를 안전하게 복구할 수 없을 때만 최종 차단한다.

## Acceptance Criteria

- rebase correction record는 plan/definition revision, target base, rebase 원래 head, 충돌 path와 index stage blob OID로 만든 conflict fingerprint, worktree progress marker, Worker session 및 correction occurrence를 저장한다.
- worktree와 rebase ownership이 안전하게 보존된 경우 Worker 또는 Agent subprocess의 첫 비정상 종료를 `AGENT_CORRECTION`으로 유지하고 PLAN_BLOCKED/PROJECT_BLOCKED, checkpoint 및 BLOCKED 알림을 만들지 않는다.
- timeout·사용 한도·일시적 Agent 오류는 기존 Agent pause 정책을 사용하고, 그 밖의 단일 비정상 결과나 Worker 결과 형식 오류도 rebase 상태가 안전하면 저장된 session 또는 session-unavailable fallback으로 다시 correction할 기회를 준다.
- 같은 conflict fingerprint에서도 unmerged index, 충돌 파일 content, staged resolution 또는 rebase position이 달라지면 진전으로 기록하고 무진전 반복 횟수를 증가시키지 않는다. 과거 occurrence audit은 보존한다.
- Worker correction 뒤 동일 conflict fingerprint와 동일 progress marker가 다시 관측된 무진전 결과가 세 번 재발한 경우에만 수행한 correction, 마지막 Worker 결과와 보존된 rebase/worktree 상태를 포함해 `USER_ACTION_REQUIRED`로 승격한다.
- conflict fingerprint가 바뀌면 새 충돌로 추적하되 A→B→A처럼 이전 지문과 동일 progress가 돌아오는 경우의 기존 무진전 이력을 유지해 순환을 탐지한다.
- Worker가 정상 결과를 반환한 뒤 stage 또는 `rebase --continue`/`--skip`이 Git lock, 프로세스 중단이나 일시 오류로 실패하면 명령 실패만으로 차단하지 않는다. G-044 checkpoint에서 index와 rebase state를 다시 읽어 이미 완료된 stage/continue를 반복하지 않고 이어간다.
- `rebase --continue` 뒤 새 충돌이 나타나면 새 fingerprint로 같은 correction flow를 계속하고, rebase가 완료되면 최신 base/HEAD를 저장한 뒤 전체 verify와 Review를 다시 수행한다.
- Worker correction은 현재 ExecPlan과 G-043의 최신 Requirement archive 이동 집합 및 승인된 guidance patch 범위 안에서만 허용된다. 범위 밖 변경이나 workspace ownership 위반은 진전으로 인정하지 않고 기존 final safety block으로 보낸다.
- rebase가 안전하게 복구되지 않거나 실제 Git state를 하나로 해석할 수 없거나 external remote 변경을 덮어써야만 진행할 수 있으면 correction 횟수와 무관하게 최종 사용자 조치로 전환한다.
- correction 또는 rebase 완료 후 게시할 변경이 남지 않으면 해당 ExecPlan이 이미 충족됐거나 MERGED됐다고 추정하지 않고 기존 무변경 `USER_ACTION_REQUIRED` 조건을 유지한다.
- correction 중 base가 다시 바뀌면 낡은 충돌 해결을 게시하지 않고 G-044/G-043 최신 snapshot 흐름으로 돌아가 새 rebase와 archive eligibility를 계산한다.
- 프로세스 중단과 다음 외부 실행에서도 conflict/progress/occurrence, worktree, branch, Worker session, guidance 결정과 implementation diff가 유지되며 중복 Agent turn이나 중복 continue를 안전하게 식별한다.
- G-026의 PR_OPEN 충돌 복구와 PR 없는 G-014 publication rebase가 같은 correction engine을 사용하되 기존 remote lease와 PR 소유권 조건은 각각 유지한다.

## Implementation

- rebase metadata에서 충돌 path, stage 1/2/3 blob, current rebase todo/position과 original/onto OID를 정규화해 conflict fingerprint를 만들고, 허용 worktree/index 변화로 별도의 progress marker를 계산한다.
- ExecPlan별 conflict occurrence와 Worker correction attempt를 additive table 또는 정규화된 recovery record로 저장한다. Agent 호출 전 pending turn identity를 기록하고 결과·snapshot 적용을 compare-and-swap해 응답 경계 중단을 멱등하게 처리한다.
- current rebase Worker 진입을 G-042 correction dispatcher와 기존 session resume/fallback 경계에 연결한다. 일반 verify·Review failure history와 conflict no-progress history를 합산하거나 서로의 성공으로 지우지 않는다.
- stage/continue/skip을 G-044 pending Git operation으로 실행하고 매 시도 뒤 실제 index·rebase state를 관측해 completed, next-conflict, transient 또는 unsafe 결과로 분류한다.
- publication 및 owned PR conflict recovery의 중복 분기를 공통 correction engine으로 모으되 push·PR 권한, archive scope와 Agent 범위 검사는 호출 context별로 유지한다.

## Validation

- Worker는 rebase conflict fingerprint/progress repository와 correction engine의 직접 관련 test class 또는 새 전용 test module만 targeted unit test로 실행한다.
- 첫 Worker 비정상 종료, Worker 결과 형식 오류, session-unavailable fallback과 프로세스 재실행이 안전한 rebase를 차단하지 않고 같은 correction 상태를 이어가는지 검증한다.
- 동일 fingerprint·동일 progress의 세 번 재발에서만 최종 차단되고, 충돌 파일·index·rebase position의 실제 진전은 횟수를 늘리지 않는지 검증한다.
- A→B→A conflict 순환, 새 충돌 연속 해결과 occurrence의 재시작 보존을 검증한다.
- stage 전·후와 continue 전·후 중단, Git lock 및 일시 실패에서 실제 state 재관측으로 중복 stage/continue 없이 완료하는지 검증한다.
- base 재변경, archive 후보 변경과 correction 범위 변경 시 낡은 결과를 폐기하고 같은 Worker 문맥에서 최신 correction·verify·Review를 수행하는지 검증한다.
- 범위 위반, ownership 불명, external remote mutation, 복구 불가능한 rebase와 무변경 결과가 정확한 final block 원인을 만들고 작업 공간을 삭제하지 않는지 검증한다.
- PR 없는 publication 및 PR_OPEN 충돌 양쪽에서 session, source checkout 불변성, 명시적 lease와 동일 PR 재사용 회귀를 검증한다.

## Progress

- [ ] conflict fingerprint·progress marker와 durable occurrence 저장 구현
- [ ] 안전한 Worker 비정상 종료 재시도 및 session fallback 연결
- [ ] stage/continue 재관측과 중단·Git lock 멱등 복구 구현
- [ ] publication·owned PR 공통 correction engine 통합
- [ ] 진전·무진전·순환·최종 차단 및 회귀 테스트 추가

## Decision Log

- verify·Review의 실패 지문과 rebase 충돌 지문은 입력과 성공 조건이 다르므로 별도 이력으로 관리한다. 기존 R-008 한도를 rebase에 문자열 기반으로 재사용하지 않는다.
- G-044가 실제 Git checkpoint를 안전하게 재구성한 뒤 실행해 Worker 결과보다 index와 rebase metadata를 원본으로 삼는다.
- 단일 Agent 실패는 자동 복구 가능 상태지만 무제한 재시도하지 않는다. 같은 충돌과 같은 무진전 결과가 correction 뒤 세 번 재발하는 R-019의 한도를 최종 사용자 조치 경계로 사용한다.
