# [R-007] Controlled Shared Agent Guidance Changes

## Context

현재 Agent 작업 범위는 Planner가 `docs/plans/*.md`만 변경하고 Worker가 `AGENTS.md`를 변경하지 못하도록 제한한다. 그러나 Planner가 계획 과정에서 현재 범위 밖의 유용한 아이디어를 발견할 수 있으며, 이를 구현 작업으로 만들지 않고 `docs/FUTURE_IDEAS.md`에 남길 필요가 있다.

또한 드물게 Worker가 현재 프로젝트에만 필요한 지침이 아니라 모든 관리 프로젝트에서 공통으로 사용해야 할 Agent 작업 규칙을 발견할 수 있다. 공통 `AGENTS.md` 변경은 이후 모든 Agent의 권한과 행동에 영향을 주므로 일반 구현 변경처럼 자동 게시해서는 안 된다.

## Requirement

Planner는 ExecPlan 작성과 함께 `docs/FUTURE_IDEAS.md`를 생성하거나 수정할 수 있어야 한다. FUTURE_IDEAS 변경은 현재 작업 범위 밖의 아이디어를 기록하는 용도로만 사용하며, 해당 아이디어를 현재 ExecPlan의 구현 범위에 포함하는 근거가 돼서는 안 된다.

Worker는 Loop Manager 자체 저장소를 작업하면서 모든 프로젝트에 공통으로 적용할 필요가 있는 Agent 규칙을 발견한 경우에만 기준 `AGENTS.md` 변경안을 만들 수 있어야 한다. 프로젝트별 규칙이나 현재 구현을 편하게 만들기 위한 지침은 `AGENTS.md`에 추가할 수 없다.

기준 `AGENTS.md` 변경안은 commit, verify, Review, push 또는 PR 생성 전에 현재 ExecPlan을 `BLOCKED`로 전환해야 한다. 사용자가 보존된 worktree의 정확한 변경 내용을 명시적으로 승인한 뒤에만 같은 ExecPlan을 재개할 수 있으며, 승인된 변경과 실제 diff가 달라지면 다시 승인을 받아야 한다.

승인된 기준 `AGENTS.md`는 향후 프로젝트 설정에 사용하는 공통 원본이 된다. 이미 등록된 프로젝트에는 자동으로 배포하지 않으며 사용자가 필요할 때 공통 파일 복사 도구나 수동 변경으로 반영한다.

## Acceptance Criteria

- Planner의 허용 변경 범위에 `docs/plans/*.md`와 `docs/FUTURE_IDEAS.md`가 포함된다.
- Planner는 현재 범위 밖의 아이디어만 FUTURE_IDEAS에 기록하며 해당 아이디어를 같은 계획 주기의 ExecPlan으로 만들지 않는다.
- Planner가 FUTURE_IDEAS 외의 비허용 파일을 변경하면 기존 계획 범위 위반과 동일하게 거부된다.
- Worker는 Loop Manager 자체 저장소의 기준 `AGENTS.md`만 공통 지침 변경 대상으로 제안할 수 있다.
- 다른 관리 프로젝트의 Worker가 해당 프로젝트의 `AGENTS.md`를 변경하거나 프로젝트 전용 규칙을 공통 규칙으로 추가하려 하면 범위 위반으로 차단된다.
- Worker가 기준 `AGENTS.md`를 변경하면 변경 내용과 worktree를 보존한 채 현재 ExecPlan이 `BLOCKED`로 전환된다.
- `AGENTS.md` 변경을 포함한 commit, verify, Review, push와 PR 생성은 사용자 승인 전에는 실행되지 않는다.
- 사용자 승인은 제안된 `AGENTS.md` diff의 결정적인 hash와 결합돼 SQLite에 기록된다.
- 승인 후 `AGENTS.md` diff가 달라지면 기존 승인은 무효가 되고 최신 변경안에 대한 새 승인을 요구한다.
- 승인된 diff가 그대로이면 같은 Worker 세션과 worktree에서 작업을 재개할 수 있다.
- BLOCKED 사유와 알림에는 공통 `AGENTS.md` 변경 승인이 필요하다는 사실과 사용자가 검토할 worktree 또는 diff 정보가 포함된다.
- 승인된 기준 `AGENTS.md` 변경은 해당 구현 PR에 포함되며 자동 Merge하지 않는다.
- 기존 등록 프로젝트의 `AGENTS.md`는 자동 변경하지 않으며 등록 시에만 수행하는 공통 내용 검사 정책도 바꾸지 않는다.
- Planner FUTURE_IDEAS 허용 범위, 비허용 파일 차단, Worker의 기준·프로젝트별 AGENTS 변경 구분, 승인 전 차단, diff 변경에 따른 재승인과 승인 후 재개를 자동 테스트로 검증한다.

## Product Impact

- Planner가 현재 범위 밖의 아이디어를 구현 계획과 분리해 보존할 수 있다.
- 모든 프로젝트에 영향을 주는 Agent 규칙은 사용자가 정확한 변경 내용을 확인하고 승인한 경우에만 기준 `AGENTS.md`에 반영된다.
- 기준 규칙의 변경이 기존 프로젝트에 예기치 않게 자동 배포되지 않는다.

## Non-Goals

- Planner가 `AGENTS.md`를 변경하도록 허용하지 않는다.
- Worker가 프로젝트별 편의나 특정 구현에만 필요한 규칙을 `AGENTS.md`에 추가하도록 허용하지 않는다.
- 기준 `AGENTS.md` 변경을 기존 등록 프로젝트에 자동으로 commit, push 또는 PR 생성하지 않는다.
- 사용자 승인 없이 문서 변경이나 재실행만으로 `AGENTS.md` 변경 BLOCKED를 해제하지 않는다.
- FUTURE_IDEAS에 기록한 항목을 자동으로 Requirement 또는 ExecPlan으로 승격하지 않는다.

## Open Questions

- 없음
