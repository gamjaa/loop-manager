# [R-011] Automatic PR Conflict Recovery

## Context

PR을 생성할 때 최신 base를 반영했더라도 사용자의 Merge를 기다리는 동안 base branch가 변경되면 Open PR에 새 충돌이 생길 수 있다. 현재 Loop Manager는 PR_OPEN 상태에서 Merge 또는 Close 여부만 동기화하므로, 충돌은 사용자가 관리 worktree와 상태 DB의 소유권 조건을 직접 이해하고 해결해야 한다.

## Requirement

Loop Manager는 PR_OPEN 상태의 소유 PR이 최신 원격 base와 충돌하는 것을 확인하면 같은 관리 branch, worktree, Worker 문맥과 PR을 사용해 충돌 해결부터 검증·Review·게시까지 자동으로 재개해야 한다.

## Acceptance Criteria

- PR_OPEN 상태를 처리할 때 원격 base와 PR head를 갱신하고, PR이 Open이며 GitHub가 충돌을 확정한 경우에만 자동 복구를 시작한다. mergeability가 계산 중이거나 불명확하면 branch를 변경하지 않고 다음 실행에서 다시 확인한다.
- 저장된 PR URL과 head branch, 관리 worktree 소유권, 로컬 branch와 관측한 원격 head의 일치를 모두 확인한 뒤에만 관리 branch를 최신 `origin/<base-branch>` 위로 rebase한다.
- 자동 복구를 시작하면 PR URL과 head 소유권 정보는 유지한 채 `PR_OPEN → IMPLEMENTING`으로 전이하고, 충돌 해결 뒤 `VERIFYING → REVIEWING → PR_OPEN` 순서로 진행한다. 이 복구가 진행되는 동안 같은 프로젝트의 다른 ExecPlan이나 Planner를 시작하지 않는다.
- rebase 충돌은 기존 Worker 세션과 재시도 정책으로 전달한다. Worker가 충돌을 해결하면 원래 ExecPlan 범위와 Agent 범위 검증을 다시 적용하고, 사용자 source checkout의 파일·index·HEAD·로컬 base ref는 변경하지 않는다.
- rebase 결과에 대해 전체 verify와 읽기 전용 Review를 다시 실행한다. 그동안 원격 base가 다시 바뀌면 최신 base가 고정된 한 시점에서 두 단계가 모두 성공할 때까지 복구 절차를 반복한다.
- 검증된 결과는 복구 시작 시 관측한 원격 PR head를 명시한 `--force-with-lease`로 같은 관리 branch에만 게시한다. 새 branch나 새 PR을 만들지 않으며 성공 후 저장된 PR head와 검증 base를 갱신하고 PR_OPEN으로 돌아간다.
- 복구 중 PR이 Merge되면 branch를 다시 쓰지 않고 기존 Merge 완료 흐름을 수행한다. PR이 Merge 없이 닫히면 기존 정책대로 BLOCKED로 전이한다.
- 원격 head가 외부에서 변경됐거나 소유권·workspace 상태가 불확실하면 원격 또는 로컬 변경을 덮어쓰지 않는다. 자동으로 재평가할 수 없는 충돌, 변경 소실, 범위 위반 또는 재시도 소진은 진단 정보와 보존된 worktree를 남기고 BLOCKED로 전이한다.
- 일시적인 GitHub·Git·Agent 오류와 사용 한도 소진은 파괴적인 정리나 force push 없이 재개 가능한 상태를 보존하고 기존 재시도 정책을 따른다.
- 충돌 확정과 계산 중 상태, 성공적인 동일 PR 복구, 복구 중 base 재변경, Worker 해결 실패, 외부 head 변경에 따른 lease 실패, Merge·Close race와 source checkout 불변성을 자동 테스트로 검증한다.

## Product Impact

- Merge 대기 중인 Loop Manager 소유 PR에 base 변경으로 충돌이 생기면 안전 조건을 확인한 뒤 같은 PR에서 자동으로 해결과 재검증을 시도한다.
- 자동 복구된 PR head도 최신 base에 대한 전체 verify와 Review를 모두 통과해야 한다.
- Loop Manager는 관측하지 않은 원격 변경이나 사용자 source checkout을 자동 충돌 해결 과정에서 덮어쓰지 않는다.

## Non-Goals

- PR을 자동 Merge하거나 사용자 승인을 대신하지 않는다.
- 충돌하지 않고 단순히 base보다 뒤처진 PR을 최신화하기 위해 branch를 다시 쓰지 않는다.
- Loop Manager가 소유하지 않은 branch나 PR, 사용자가 별도로 수정한 원격 head를 자동으로 rebase하거나 force push하지 않는다.
- Review 요청이나 새로운 제품 요구로 발생한 일반 구현 변경을 PR 충돌 복구로 처리하지 않는다.

## Open Questions

- 없음
