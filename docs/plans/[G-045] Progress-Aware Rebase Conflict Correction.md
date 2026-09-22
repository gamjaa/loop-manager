---
id: G-045
title: Progress-Aware Rebase Conflict Correction
sources:
  - docs/requirements/[R-019] Automatic Archive Eligibility Recovery Before Blocking.md
depends_on:
  - G-044
  - G-049
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

- 새 `rebase_correction.py`에 Git rebase metadata 정규화, conflict fingerprint, allowed progress marker와 next action을 정의하고, 새 `rebase_correction_state.py` repository가 occurrence, no-progress count와 pending Agent/Git operation identity를 저장한다.
- G-049 `LatestBasePort`와 PR-conflict preparation adapter가 동일 correction engine을 호출하도록 하고, `execution_handlers.py`의 직접 `git add -A`, `rebase --continue/--skip` 및 즉시 block 분기를 typed engine result로 교체한다.
- Worker turn은 기존 Agent/session port를 사용하되 호출 전 pending turn identity를 저장하고 result·scope snapshot을 compare-and-swap으로 적용한다. 일반 validation history와 rebase no-progress history는 별도 repository와 reset rule을 유지한다.
- stage/continue/skip은 G-044 durable Git operation port를 사용하고 매 시도 뒤 index/rebase observation을 다시 읽어 completed, next-conflict, transient와 unsafe를 판정한다. 허용 pathspec만 stage하며 범위/ownership 위반은 progress로 기록하지 않는다.
- publication rebase와 owned PR conflict adapter는 context별 remote lease·PR ownership·archive scope를 engine 입력 policy로 제공한다. engine은 push/PR 권한이나 base worktree를 직접 수정하지 않고 G-042 recovery decision 및 다음 execution phase만 반환한다.

## Validation

- Worker는 새 `tests/test_rebase_correction.py`, `tests/test_rebase_correction_state.py`, `tests/test_execution_handlers.py`와 G-044 operation adapter의 직접 관련 targeted case만 실행한다.
- 첫 Worker abnormal result, result format error, session fallback과 process restart가 safe rebase를 final block하지 않고 동일 pending turn을 중복 호출하지 않는지 검증한다.
- 동일 fingerprint/progress 세 번 재발, 실제 file/index/rebase-position 진전, 새 conflict와 A→B→A 순환을 pure marker 및 repository history test로 검증한다.
- stage/continue 전후 중단, Git lock과 transient failure에서 actual state 재관측이 중복 stage/continue 없이 completed/next-conflict로 수렴하는지 검증한다.
- base/archive scope 변경이 stale correction을 폐기하고 최신 G-043/G-044 snapshot에서 같은 Worker context로 돌아가는지 handler fake로 검증한다.
- 범위 위반, ownership 불명, external remote mutation, unrecoverable rebase와 no-change만 즉시 또는 한도 후 final user action으로 전환하고 worktree를 삭제하지 않는지 검증한다.
- PR 없는 publication과 PR_OPEN conflict가 같은 engine을 쓰면서 각각의 session, source checkout, lease와 동일 PR 재사용 계약을 보존하는지 검증한다.
- 전체 verify에서 validation history, session fallback, latest-base publication과 owned PR conflict recovery 회귀가 없어야 한다.

## Progress

- [ ] conflict fingerprint/progress pure policy와 occurrence repository 구현
- [ ] LatestBase/PR-conflict adapter의 공통 correction engine 통합
- [ ] durable Worker turn identity와 no-progress CAS 적용
- [ ] G-044 stage/continue operation 재관측 연결
- [ ] 진전·순환·중단·최종 차단 targeted 회귀 테스트 추가

## Decision Log

- verify·Review의 실패 지문과 rebase 충돌 지문은 입력과 성공 조건이 다르므로 별도 이력으로 관리한다. 기존 R-008 한도를 rebase에 문자열 기반으로 재사용하지 않는다.
- G-044가 실제 Git checkpoint를 안전하게 재구성한 뒤 실행해 Worker 결과보다 index와 rebase metadata를 원본으로 삼는다.
- 단일 Agent 실패는 자동 복구 가능 상태지만 무제한 재시도하지 않는다. 같은 충돌과 같은 무진전 결과가 correction 뒤 세 번 재발하는 R-019의 한도를 최종 사용자 조치 경계로 사용한다.
- Post-refactor revalidation: rebase correction은 G-049 handler 안의 Git 절차가 아니라 LatestBase/PR-conflict adapter가 공유하는 engine이다. conflict policy, durable history, Agent turn과 Git operation을 분리해 publication 경로의 중복 제어 흐름을 제거한다.
