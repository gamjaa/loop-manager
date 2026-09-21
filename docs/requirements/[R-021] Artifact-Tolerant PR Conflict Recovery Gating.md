# [R-021] Artifact-Tolerant PR Conflict Recovery Gating

## Context

G-026은 Open PR이 GitHub에서 명시적으로 `CONFLICTING`일 때만 소유 branch와 관리 worktree를 검증해 자동 충돌 복구를 시작하고, `MERGEABLE` 또는 `UNKNOWN`이면 PR과 작업 공간을 변경하지 않은 채 `PR_OPEN`을 유지하도록 정의했다. 그러나 현재 구현은 mergeability를 분류하기 전에 conflict-recovery 전용 worktree clean 검사를 실행한다.

2026-09-21 G-024 PR #28은 GitHub에서 `OPEN`·`MERGEABLE`이고 관리 worktree의 `HEAD`, 원격 branch와 PR head OID가 모두 같았지만, 전체 verify가 생성한 ignored `__pycache__/*.pyc` 때문에 `PR_OPEN 충돌 복구 소유권 검증 실패`로 차단됐다. 일반 `git status`에는 tracked, staged 또는 non-ignored untracked 변경이 없었고 진행 중인 Git operation도 없었다.

Agent 범위 검사가 ignored 파일의 생성과 변경까지 관찰하는 것은 필요한 안전 경계다. 반면 PR 상태 동기화와 충돌 복구 시작 조건은 Agent 호출의 순변경 검사가 아니며, 정상 verify가 남긴 ignored 파생 파일만으로 Merge 가능한 PR을 차단해서는 안 된다. 두 경계가 같은 `dirty` 판정을 공유하면 모든 검증 성공 PR이 반복적인 수동 cache 삭제나 DB 복구를 요구할 수 있다.

## Requirement

Loop Manager는 Open PR을 동기화할 때 PR의 state, 저장된 URL·head branch·head OID와 GitHub mergeability를 먼저 판정해야 한다. Merge 또는 Close는 기존 상태 전이를 적용하고, identity가 저장된 소유 PR과 일치하는 `MERGEABLE` 또는 `UNKNOWN` PR에는 conflict-recovery 전용 fetch, worktree cleanliness 검사, rebase, Agent 호출 또는 push를 실행하지 않아야 한다. `MERGEABLE`은 Merge 대기, `UNKNOWN`은 다음 실행의 재조회 상태로 `PR_OPEN`을 유지한다.

GitHub가 `CONFLICTING`을 명시한 경우에만 충돌 복구 시작 소유권 검사를 수행한다. 이 검사는 저장된 PR URL·head branch·head OID, SQLite workspace 소유권, 결정적 branch·worktree, local `HEAD`, 원격 head, 진행 중인 Git operation과 Git에 게시될 수 있는 변경 상태를 검증해야 한다.

충돌 복구용 cleanliness는 Agent scope snapshot과 별도 의미를 가진다. local `HEAD`와 관측한 원격 PR head가 같고 index, tracked 파일 및 non-ignored untracked 경로에 변경이 없으며 Git operation이 없다면, stage되지 않은 ignored 파생 파일의 존재만으로 worktree를 불결하거나 소유 불명으로 판정하지 않는다. 허용된 ignored 파일은 충돌 복구 commit, push 또는 PR diff에 포함하지 않으며 삭제, clean, stash 또는 reset하지 않고 그대로 보존한다.

다만 충돌 복구가 반영할 tracked 경로와 같거나 부모·자식 관계로 겹쳐 Git 쓰기가 ignored 파일을 덮어쓸 수 있는 경우에는 이를 허용 가능한 cache로 간주하지 않는다. 해당 파일을 삭제하거나 덮어쓰지 않고 복구를 시작하지 않으며, 충돌 경로와 보존 상태를 진단한다. staged 변경, tracked 변경, non-ignored untracked 파일, non-default index flags, 진행 중인 Git operation, branch·HEAD·원격 OID 불일치와 소유권 불명은 기존과 같이 복구를 거부한다.

Agent 실행 전후의 범위 검사는 ignored 파일을 계속 관찰하고, Planner·Worker·Reviewer가 허용되지 않은 ignored 파일을 생성하거나 변경한 경우 기존 정책대로 거부한다. 이 Requirement는 PR_OPEN 상태 판정과 Manager 소유 충돌 복구의 preflight만 분리하며 Agent 권한, rebase·push 권한 또는 force-with-lease 조건을 넓히지 않는다.

현재 오류처럼 PR identity와 local·remote head가 모두 일치하고 설명되지 않은 게시 가능 변경 없이 ignored 파생 파일만 남은 false-positive `BLOCKED`는 cache 삭제나 SQLite 수동 수정 없이 PR 상태를 다시 관측해 `PR_OPEN`으로 자동 복구해야 한다. 실제 소유권 불일치나 게시 가능 변경이 함께 있으면 ignored 파일의 존재를 이유로 전체 상태를 신뢰하지 않는다.

## Acceptance Criteria

- Open PR의 Merge, Close와 identity 판정 뒤 mergeability를 분류하며, conflict-recovery 전용 worktree 검사와 ref fetch는 `CONFLICTING`인 경우에만 실행한다.
- 저장된 URL·head branch·head OID와 일치하는 `MERGEABLE` PR은 ignored `__pycache__` 또는 동일 성격의 파생 파일이 관리 worktree에 있어도 `PR_OPEN`과 기존 검증·Review 결과를 유지하고 rebase, Agent 호출, push 또는 BLOCKED 알림을 만들지 않는다.
- 동일한 조건의 `UNKNOWN` PR은 worktree와 branch를 변경하지 않고 다음 실행의 재조회를 기다리며 conflict-recovery marker를 만들지 않는다.
- `CONFLICTING` PR에서 local branch·HEAD와 관측한 원격 head가 일치하고 index, tracked 파일, non-ignored untracked 경로와 Git operation이 clean하면, 충돌 복구가 쓸 tracked 경로와 겹치지 않는 ignored 파생 파일이 있어도 기존 branch·worktree·Worker 및 Reviewer 세션으로 복구를 시작한다.
- 허용된 ignored 파일은 rebase 전후에 삭제, reset, clean, stash, stage, commit 또는 push되지 않으며 PR diff에 포함되지 않는다.
- ignored 파일이 충돌 복구가 쓸 tracked 경로와 같거나 부모·자식으로 겹치면 파일을 보존하고 rebase나 checkout을 시작하지 않으며 정확한 충돌 경로를 진단한다.
- staged 변경, tracked 변경, non-ignored untracked 파일, non-default index flags, 진행 중인 Git operation, 예상하지 않은 branch·HEAD 또는 원격 PR head 변경은 기존과 같이 안전 실패로 처리하고 어떤 원격 ref도 덮어쓰지 않는다.
- PR identity 또는 게시 가능한 Git 상태의 실제 불일치와 ignored 파생 파일이 함께 있으면 파생 파일 허용을 근거로 전체 worktree를 clean으로 판정하지 않는다.
- PR_OPEN 동기화와 충돌 복구용 cleanliness 변경은 Agent scope snapshot에 영향을 주지 않으며, Agent가 만든 ignored 파일의 순변경을 허용하지 않는다.
- PR #28과 동등하게 전체 verify 뒤 ignored Python bytecode cache만 남은 `MERGEABLE` 관리 worktree를 실제 `reconcile_existing_pull_request` 및 프로젝트 실행 경로로 통과시키고, `PR_OPEN` 유지, Agent·rebase·push 미호출과 BLOCKED 알림 부재를 검증한다.
- `UNKNOWN`, `MERGEABLE`, `CONFLICTING` 각각에 ignored cache가 있는 회귀 테스트를 추가하고, 충돌하는 ignored 경로, staged·tracked·non-ignored untracked, Git operation 및 local·remote head 불일치 거부 테스트를 함께 유지한다.
- 이미 동일 false positive로 `BLOCKED`인 소유 PR은 GitHub PR identity·mergeability, local·remote head와 게시 가능한 변경 상태를 재검증해 안전 조건이 충족되면 worktree·세션·검증 결과를 보존한 채 `PR_OPEN`으로 멱등하게 복구한다.
- 전체 verify에서 기존 PR conflict recovery의 explicit lease, 외부 head 변경 거부, Merge·Close race, Agent scope ignored-file 검사와 R-016의 Planner bytecode 격리가 유지된다.

## Product Impact

- 정상 verify가 생성한 ignored 파생 파일 때문에 Merge 가능한 PR과 프로젝트 자동 실행이 반복 차단되지 않는다.
- 실제 충돌 PR만 기존 소유권·lease 경계 안에서 자동 복구하고, PR identity 또는 게시 가능한 변경의 불일치는 계속 차단한다.
- Agent 범위 안전성과 Worker 테스트 환경을 완화하거나 cache 삭제를 요구하지 않고 PR 상태 동기화의 단계별 의미를 분리한다.

## Non-Goals

- Agent scope 검사에서 ignored 파일을 제외하거나 Agent가 ignored 파일을 자유롭게 변경하도록 허용하지 않는다.
- 관리 worktree의 ignored 파일을 자동 삭제, clean, reset, stash 또는 commit하지 않는다.
- GitHub가 `CONFLICTING`으로 확정하지 않은 PR을 rebase하거나 다시 게시하지 않는다.
- 저장된 PR URL·head branch·head OID, workspace 소유권, local·remote head 또는 explicit force-with-lease 조건을 완화하지 않는다.
- tracked, staged, non-ignored untracked 변경이나 Git operation이 있는 worktree를 ignored cache와 같은 상태로 취급하지 않는다.
- G-026의 Merge된 역사 문서를 수정하거나 기존 PR conflict recovery의 제품 정책을 확장하지 않는다.

## Open Questions

- 없음
