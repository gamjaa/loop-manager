# [R-015] Remote Checkpoint for Safely Blocked ExecPlans

## Context

ExecPlan이 PR 생성 전에 BLOCKED가 되면 구현 변경은 Loop Manager의 로컬 관리 worktree에만 남는다. 사용자는 서버에 직접 접속해야 변경 내용을 확인할 수 있고, 서버나 worktree에 문제가 생기면 조사에 필요한 작업 결과를 잃을 수 있다.

검증이나 Review 실패, 사용자 결정 대기처럼 계획 자체가 차단된 경우에는 Loop Manager가 소유한 작업 branch를 원격에 보존하면 GitHub에서 변경 내용을 확인할 수 있다. 그러나 소유권이 불명확하거나 저장소 안전이 훼손된 BLOCKED까지 무조건 push하면 범위 밖 변경이나 신뢰할 수 없는 Git 상태를 원격에 게시할 수 있다.

## Requirement

Loop Manager는 PR이 없는 ExecPlan이 `PLAN_BLOCKED`로 전환될 때, 해당 관리 worktree와 branch가 안전하다고 코드로 검증되면 현재 작업을 같은 이름의 원격 branch에 checkpoint로 push해야 한다. 이 동작은 변경 내용을 원격에서 확인하고 보존하기 위한 것이며 PR을 생성하지 않는다.

기존 단일 `BLOCKED` 상태를 사용하는 환경에서도 동일한 안전 조건을 만족하는 계획 단위 차단에는 이 동작을 적용할 수 있다. `PROJECT_BLOCKED`, `planning_error`, 프로젝트 등록 실패처럼 안전한 ExecPlan branch를 특정할 수 없는 차단에는 원격 쓰기를 수행하지 않는다.

원격 checkpoint를 만들기 전에 Loop Manager는 다음 조건을 모두 확인해야 한다.

- branch와 worktree가 해당 ExecPlan을 위해 Loop Manager가 생성하고 SQLite에 기록한 대상과 일치한다.
- worktree의 `HEAD`와 branch ref를 확인할 수 있고 detached HEAD나 다른 branch가 아니다.
- merge, rebase, cherry-pick 또는 revert가 미해결 상태로 진행 중이지 않다.
- 기본 worktree의 파일, index와 HEAD를 변경하지 않는다.
- commit되지 않은 변경이 Worker에게 허용된 경로와 승인된 변경 범위 안에만 있다.
- symlink, ignored 파일, 범위 밖 staged 파일과 소유권을 확인할 수 없는 Git ref 변경이 checkpoint에 포함되지 않는다.

안전한 commit되지 않은 변경이 있으면 Loop Manager 코드가 검증된 변경 경로만 stage하고 checkpoint commit을 생성한다. AI Agent에게 commit이나 push를 맡기지 않는다. checkpoint commit 메시지는 해당 ExecPlan ID와 BLOCKED checkpoint임을 명확히 표시하고, 본문에는 구조화된 차단 원인과 포함된 변경 경로를 코드로 기록한다. 이미 모든 변경이 commit돼 있으면 불필요한 빈 commit을 만들지 않는다.

Loop Manager는 상태와 차단 원인을 먼저 SQLite에 보존한 뒤 checkpoint commit과 push를 수행하고, 원격 branch 이름, push한 commit OID와 push 결과를 다시 기록해야 한다. commit 또는 push 도중 프로세스가 중단돼도 다음 실행에서 로컬과 원격 OID를 비교해 같은 commit을 중복 생성하지 않고 이어서 처리해야 한다.

원격 branch가 없으면 현재 Loop Manager 소유 branch를 생성한다. 원격 branch가 있으면 저장된 소유권과 예상 OID가 일치하는 경우에만 fast-forward push한다. PR이 없는 동일한 Loop Manager 소유 branch를 rebase해 commit이 바뀐 기존 복구 흐름에서만 현재 정책과 같은 `--force-with-lease`를 허용한다. 원격 branch가 예상하지 못한 commit으로 변경됐으면 덮어쓰거나 강제 push하지 않고 BLOCKED 원인에 원격 불일치를 추가한다.

checkpoint push 성공 여부와 관계없이 ExecPlan 상태는 BLOCKED로 유지한다. 일시적인 네트워크 또는 Git push 실패는 기존 외부 CLI 재시도 한도를 적용하고, 모두 실패하면 로컬 worktree와 commit을 보존한 채 다음 Loop Manager 실행에서 다시 시도한다. branch protection이나 권한 거부를 우회하지 않는다.

checkpoint branch에는 PR을 생성하지 않으며 Draft PR도 만들지 않는다. 동일한 head branch에 이미 Open, Closed 또는 Merged PR이 있으면 새 PR이나 대체 PR을 만들지 않고 기존 PR 복구 규칙을 따른다. 이미 PR이 존재하는 BLOCKED는 이 Requirement의 새 checkpoint 게시 대상이 아니다.

BLOCKED Telegram 알림을 사용하는 경우 checkpoint push가 성공했으면 원격 branch 링크와 commit OID를 포함한다. push하지 못했으면 변경이 로컬에만 보존된 이유를 포함한다. 알림은 push 결과가 확정된 뒤 생성하며 기존 BLOCKED 알림 중복 방지 규칙을 유지한다.

차단 원인이 해소되면 Loop Manager는 기존 관리 worktree와 branch에서 작업을 재개한다. 원격 checkpoint는 읽기 전용 백업으로 취급하며 사용자가 원격 branch를 직접 수정한 결과를 자동으로 합치거나 덮어쓰지 않는다. 최종 검증과 Review가 통과하면 같은 branch로 기존 게시 흐름을 계속하고 그때 처음 PR을 생성한다.

## Acceptance Criteria

- PR이 없는 ExecPlan이 계획 내부 원인으로 BLOCKED가 되고 소유권과 변경 범위가 검증되면 해당 Loop Manager 소유 branch가 원격에 push된다.
- commit되지 않은 안전한 변경은 검증된 경로만 포함하는 checkpoint commit으로 보존되고 GitHub에서 diff를 확인할 수 있다.
- 모든 변경이 이미 commit돼 있으면 빈 checkpoint commit을 추가하지 않고 현재 HEAD를 push한다.
- checkpoint commit 제목은 ExecPlan ID와 BLOCKED checkpoint를 식별하며 본문에는 구조화된 차단 원인과 변경 경로가 포함된다.
- checkpoint commit과 push는 AI Agent가 아니라 Loop Manager 코드가 수행한다.
- checkpoint branch를 push할 때 Open, Draft 또는 일반 PR을 생성하지 않는다.
- BLOCKED 전환과 차단 원인은 push보다 먼저 SQLite에 저장되며 push 실패가 상태 저장을 되돌리지 않는다.
- 프로세스가 commit 이후 또는 push 이후 중단돼도 다음 실행에서 같은 checkpoint commit을 중복 생성하지 않는다.
- 같은 원격 OID가 이미 존재하면 불필요한 push를 반복하지 않는다.
- 원격 branch가 예상하지 못한 commit으로 변경됐으면 force push하지 않고 로컬 작업을 보존한다.
- `--force-with-lease`는 PR이 없는 동일한 Loop Manager 소유 branch의 기존 rebase 복구 조건에서만 사용할 수 있다.
- 소유권 불명, 범위 밖 변경, 미해결 Git 작업, 신뢰할 수 없는 ref 또는 `PROJECT_BLOCKED` 상태에서는 commit과 push를 수행하지 않고 구체적인 사유를 기록한다.
- `planning_error`나 branch 생성 전 실패에는 원격 branch를 새로 만들지 않는다.
- 동일한 head branch에 기존 PR이 있으면 새 PR이나 대체 PR을 만들지 않고 기존 PR 상태를 유지한다.
- 일시적인 push 실패는 정해진 횟수만 재시도하고, 실패 후에도 다음 실행에서 같은 로컬 checkpoint를 다시 push할 수 있다.
- BLOCKED 알림에는 성공 시 branch 링크와 commit OID가, 미게시 시에는 push하지 않은 이유가 포함된다.
- 차단 해제 후에는 checkpoint와 같은 branch에서 작업을 재개하고 verify와 Review를 통과한 뒤에만 최초 PR을 생성한다.
- 안전한 dirty 변경, 이미 commit된 변경, 변경 없음, 원격 branch 신규 생성, 재실행 멱등성, 원격 불일치, push 실패, 기존 PR, 미해결 rebase, 범위 위반과 PROJECT_BLOCKED를 자동 테스트로 검증한다.

## Product Impact

- 사용자는 PR을 만들 수 없는 BLOCKED 작업도 원격 branch에서 변경 내용을 확인할 수 있다.
- BLOCKED 시점의 안전한 작업 결과가 원격에 보존돼 서버 worktree에만 의존하지 않는다.
- 검증과 Review를 통과하기 전에는 PR이 생성되지 않는 기존 품질 경계를 유지한다.

## Non-Goals

- 안전 조건을 만족하지 않는 worktree나 branch를 원격에 push하지 않는다.
- checkpoint push를 구현 완료, 검증 성공 또는 PR 생성으로 간주하지 않는다.
- BLOCKED 작업을 위한 Draft PR이나 임시 PR을 생성하지 않는다.
- 사용자가 원격 checkpoint branch에서 직접 추가한 commit을 자동으로 merge, rebase 또는 덮어쓰지 않는다.
- checkpoint를 위해 기본 worktree나 base branch를 변경하지 않는다.
- 원격 checkpoint branch를 BLOCKED 상태에서 자동 삭제하지 않는다.

## Open Questions

- 없음
