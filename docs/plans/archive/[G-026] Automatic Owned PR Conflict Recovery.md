---
id: G-026
title: Automatic Owned PR Conflict Recovery
sources:
  - docs/requirements/[R-011] Automatic PR Conflict Recovery.md
depends_on:
  - G-022
complexity: high
risk: critical
priority: critical
---

## Purpose

Merge를 기다리는 Loop Manager 소유 PR이 최신 원격 base와 충돌하면 사용자의 source checkout을 건드리지 않고 기존 branch, 관리 worktree, Worker 문맥과 PR을 그대로 사용해 충돌 해결, 전체 verify, 읽기 전용 Review와 안전한 재게시를 자동으로 완료한다. GitHub가 충돌을 확정하지 않았거나 원격 head 및 workspace 소유권을 증명할 수 없는 경우에는 branch를 다시 쓰지 않는다.

## Acceptance Criteria

- PR_OPEN 동기화는 PR URL, state, merged 여부, head branch와 head OID에 더해 GitHub의 mergeability를 조회하고, Open PR이 명시적으로 충돌 상태일 때만 자동 복구를 시작한다.
- mergeability가 계산 중이거나 불명확하면 상태와 branch를 변경하지 않고 PR_OPEN을 유지한 채 다음 실행에서 다시 확인한다. 충돌하지 않고 단순히 base보다 뒤처진 PR도 rebase하거나 다시 게시하지 않는다.
- 충돌 복구를 시작하기 전에 최신 원격 base와 관리 branch ref를 fetch하고, 저장된 PR URL 및 head branch, 결정적인 worktree 경로, SQLite workspace 소유권, worktree의 local branch와 관측한 원격 head OID가 모두 일치하는지 검증한다.
- 소유권 검증이 끝나면 관측한 PR head OID와 base OID 및 복구 중이라는 사실을 SQLite에 먼저 영속화하고, PR URL과 기존 Worker·Reviewer 세션을 유지한 채 `PR_OPEN → IMPLEMENTING`으로 전이한다. 프로세스가 중단돼도 같은 복구 상태에서 재개한다.
- 관리 branch를 최신 `origin/<base-branch>` 위로 rebase한다. rebase 충돌은 기존 Worker 세션에 전달하고, Worker가 해결한 결과에는 원래 ExecPlan 메타데이터·변경 범위, Agent 범위와 Requirement archive 정합성 검사를 다시 적용한다.
- rebase가 충돌 없이 완료되거나 Worker가 충돌을 해결하면 `VERIFYING → REVIEWING` 순서로 전체 verify와 읽기 전용 Review를 다시 수행한다. verify 또는 Review 실패는 기존 실패 지문, 재시도, Worker level 승격과 BLOCKED 정책을 그대로 사용한다.
- verify와 Review가 끝나기 전에 원격 base가 다시 바뀌면 최신 base를 다시 반영하고, 동일한 base OID에 대해 두 단계가 모두 성공할 때까지 게시하지 않는다.
- 복구 게시 직전에 같은 PR을 다시 조회한다. PR이 Merge됐으면 branch를 push하지 않고 기존 Merge 완료·worktree cleanup 흐름을 수행하며, Merge 없이 닫혔으면 기존 정책대로 BLOCKED로 전이한다.
- Open PR의 원격 head가 복구 시작 때 관측한 OID와 다르면 외부 변경으로 판단해 어떤 ref도 덮어쓰지 않고 진단 정보와 worktree를 보존한 채 BLOCKED로 전이한다.
- 검증된 복구 commit은 복구 시작 때 저장한 원격 head OID를 명시한 `--force-with-lease=refs/heads/<branch>:<observed-oid>`로 동일한 Loop Manager 소유 branch에만 push한다. 일반 force push, 새 branch, 새 PR 또는 다른 PR의 갱신은 허용하지 않는다.
- lease 거부, 소유권 불명확, 변경 소실, 복구 불가능한 rebase, Agent 범위 위반 또는 기존 재시도 정책의 소진은 원격·로컬 변경을 삭제하지 않고 진단 정보와 관리 worktree를 보존한 채 BLOCKED로 전이한다.
- GitHub, Git 또는 Agent의 일시 오류와 AI 사용 한도 소진은 force push나 파괴적인 정리를 수행하지 않고 복구 marker, 관측 OID, 세션과 workspace를 보존해 다음 실행에서 재개한다.
- push 성공을 원격에서 확인한 뒤에만 새 PR head OID와 검증 base OID를 저장하고 복구 marker를 지운 뒤 동일 PR URL의 PR_OPEN으로 돌아간다. PR 생성 경로와 PR 생성 알림을 다시 실행하지 않는다.
- 복구의 모든 Git 쓰기는 Agent 호출 밖의 Loop Manager 코드가 소유한다. Worker는 충돌 파일 수정과 targeted unit test만 수행하며, 기본 source checkout의 파일, index, HEAD, checkout branch와 local base ref는 성공·실패·중단 경로 모두에서 보존된다.

## Implementation

- PR 조회 결과에 GitHub mergeability를 포함하고, Open PR을 `conflicting`, `mergeable`, `unknown`으로 결정적으로 분류하는 경계를 추가한다. Merge와 Close 판정은 mergeability보다 먼저 처리하고 `unknown`은 대기 결과로 취급한다.
- PR_OPEN 전용 동기화 경로를 일반적인 기존 PR 연결 경로에서 분리한다. 충돌 복구가 이미 IMPLEMENTING, VERIFYING, REVIEWING 또는 게시 단계에 있으면 Open PR이라는 이유만으로 PR_OPEN으로 되돌리지 말고, head·Merge·Close race만 재검증한 뒤 저장된 단계에서 계속한다.
- ExecPlan 상태 원본에 충돌 복구 marker, 시작 시 관측한 원격 head OID와 목표 base OID를 내구성 있게 저장하고 project mirror, schema migration, hydration과 clear 경계를 함께 갱신한다. 상태와 OID를 기록한 transaction이 완료된 뒤에만 rebase를 시작한다.
- 기존 publication rebase/Worker 충돌 해결/verify/Review 코드를 PR 충돌 복구에서도 재사용할 수 있는 단계별 함수로 분리하되, PR 없는 publication 복구와 PR_OPEN 복구의 push 권한을 별도 helper로 유지한다.
- PR_OPEN 복구용 push helper는 저장된 PR URL·branch·workspace 소유권, 현재 PR state/head, local HEAD와 검증 base를 다시 확인하고 정확한 lease가 있을 때만 기존 원격 branch를 갱신한다. 기존 `push_managed_publication_branch`의 PR 없는 branch 예외를 일반 Open PR에 포괄적으로 확장하지 않는다.
- rebase 충돌 중단, Worker 결과 검증 실패, verify/Review 반복과 재실행에서도 복구 marker 및 관측 lease OID를 유지한다. 성공한 push 확인 또는 Merge 완료에서만 이를 지우며 Close 및 안전 실패의 BLOCKED에서는 진단을 위해 보존한다.
- base 재변경 시 이전 verify/Review 결과를 무효화하고 기존 validation failure history의 단계별 성공·실패 계약을 지킨다. 복구 중에도 원래 ExecPlan 외 변경을 허용하거나 새 제품 작업을 Worker에게 맡기지 않는다.
- G-022가 제공하는 origin-only fetch와 workspace identity 경계를 사용해 remote base/head를 읽고 관리 worktree에서만 rebase한다. source checkout은 repository object database 접근 anchor로만 사용한다.

## Validation

- Worker는 PR conflict recovery 전용 test class와 직접 수정한 publication/state helper 테스트만 targeted unit test로 실행한다.
- GitHub가 `CONFLICTING`을 반환하는 경우와 mergeability가 계산 중·불명확한 경우를 구분하고, 후자 및 단순 behind PR에서는 rebase, Agent 호출과 push가 전혀 발생하지 않는지 검증한다.
- 소유 PR의 성공 경로에서 기존 branch·worktree·Worker 세션·PR URL을 재사용하고, rebase 뒤 전체 verify와 Review를 통과한 commit을 시작 시 head OID의 명시적 lease로 push한 뒤 같은 PR_OPEN으로 돌아가는지 검증한다.
- rebase 충돌을 같은 Worker 세션이 해결하는 경로, Worker 해결 실패와 기존 실패 지문 반복 한도에 따른 BLOCKED를 검증한다.
- verify 또는 Review 중 base가 다시 바뀌면 rebase 및 두 검증을 다시 수행하고, 한 base OID에서 모두 성공한 commit만 push하는지 검증한다.
- 복구 시작 전 local HEAD/remote head 불일치, 복구 중 외부 원격 head 변경과 lease 실패에서 push하지 않고 worktree와 원격 commit을 모두 보존하는지 검증한다.
- 복구 중 PR Merge와 Close race를 각각 기존 Merge 완료 흐름과 BLOCKED 흐름으로 처리하고, 새 branch나 `gh pr create`를 호출하지 않는지 검증한다.
- GitHub mergeability 조회, fetch, rebase, Agent 및 push의 일시 오류와 사용 한도 소진 뒤 상태·관측 OID·세션이 보존되고 다음 실행에서 중복 rebase나 무조건 force push 없이 재개되는지 검증한다.
- dirty·staged·untracked 또는 local base가 ahead·behind·diverged인 source checkout에서 성공, BLOCKED와 일시 중단 경로를 실행해 파일 snapshot, index tree, HEAD, checkout branch와 local base ref가 모두 불변인지 검증한다.
- 전체 verify에서 기존 PR 없는 publication recovery, Open/Closed/Reopened/Merged PR 재연결, Merge 방식별 cleanup, Agent scope와 G-022 origin 격리 테스트가 유지돼야 한다.

## Progress

- [x] PR mergeability 분류와 durable conflict-recovery 상태 추가
- [x] 소유권 검증 후 동일 worktree·세션 rebase 복구 구현
- [x] 최신 base 고정 verify·Review 반복과 Merge/Close race 처리 구현
- [x] 기존 PR branch 전용 lease push와 원격 확인 구현
- [x] 중단 재개, 외부 head 변경과 source checkout 불변 회귀 테스트 추가

## Decision Log

- R-011은 Architecture Change Policy의 worktree, branch, push와 PR 생성 경계 변경에 대한 사용자 승인 근거다.
- G-022가 Merge된 뒤 시작해 원격-only 입력과 사용자 source checkout 비간섭을 충돌 복구의 전제로 사용한다.
- 상태 감지, rebase, 검증과 동일 PR 재게시를 하나의 계획에 둔다. Open PR branch 재작성 권한만 먼저 Merge하거나 검증 재실행만 따로 Merge하면 안전하게 사용할 수 없는 중간 상태가 생긴다.
- GitHub가 충돌을 확정한 경우만 자동 복구한다. 최신 base보다 뒤처졌다는 사실만으로 사용자가 검토 중인 PR commit을 다시 쓰지 않는다.
- 기존 PR 없는 publication 복구의 force-with-lease 예외와 PR_OPEN 충돌 복구 권한을 분리해, 후자의 URL·head·workspace·검증 base 조건이 다른 게시 경로로 우회되지 않게 한다.
