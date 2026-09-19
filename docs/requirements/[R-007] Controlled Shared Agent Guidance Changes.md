# [R-007] Controlled Shared Agent Guidance Changes

## Context

현재 Agent 작업 범위는 Planner가 `docs/plans/*.md`만 변경하고 Worker가 `AGENTS.md`를 변경하지 못하도록 제한한다. 그러나 Planner가 계획 과정에서 현재 범위 밖의 유용한 아이디어를 발견할 수 있으며, 이를 구현 작업으로 만들지 않고 `docs/FUTURE_IDEAS.md`에 남길 필요가 있다.

Worker는 구현 중 현재 프로젝트에 필요한 지침이나 모든 관리 프로젝트에 공통으로 적용해야 할 Agent 작업 규칙을 발견할 수 있다. 그렇다고 Worker가 자신과 후속 Agent의 권한·행동을 결정하는 `AGENTS.md`를 먼저 수정하게 하면 승인되지 않은 규칙이 작업 공간과 후속 검증에 영향을 줄 수 있다. 변경 필요성은 보존하되 실제 파일을 수정하기 전에 사용자가 정확한 내용을 결정할 수 있는 제안 흐름이 필요하다.

## Requirement

Planner는 ExecPlan 작성과 함께 `docs/FUTURE_IDEAS.md`를 생성하거나 수정할 수 있어야 한다. FUTURE_IDEAS 변경은 현재 작업 범위 밖의 아이디어를 기록하는 용도로만 사용하며, 해당 아이디어를 현재 ExecPlan의 구현 범위에 포함하는 근거가 돼서는 안 된다.

Worker는 현재 ExecPlan을 수행하는 데 필요한 Agent 규칙 변경을 발견하면 `AGENTS.md`를 직접 수정하지 않고 선택된 ExecPlan의 `Decision Log`에 변경 제안을 기록해야 한다. 제안에는 대상 `AGENTS.md`, 프로젝트 전용 또는 공통 규칙이라는 범위, 변경 이유, 변경하지 않을 때 현재 작업에 미치는 영향과 실제 적용할 정확한 patch가 포함돼야 한다. 단순한 구현 편의나 현재 작업과 무관한 규칙은 제안하지 않고 `FUTURE_IDEAS.md`에 분리한다.

모든 프로젝트의 Worker는 현재 프로젝트의 `AGENTS.md` 변경을 제안할 수 있다. Loop Manager 자체 저장소의 Worker만 향후 프로젝트 설정에 사용하는 기준 `AGENTS.md`의 공통 규칙 변경을 제안할 수 있다. Planner와 Reviewer는 `AGENTS.md` 변경을 제안하거나 적용하지 않는다.

Worker가 유효한 제안을 기록하면 Loop Manager는 `AGENTS.md`를 수정하거나 commit, verify, Review, push 또는 PR 생성을 진행하지 않고 현재 ExecPlan을 `BLOCKED`로 전환한다. 제안이 기록된 ExecPlan과 기존 구현 변경 및 Worker 세션은 그대로 보존한다.

사용자는 보존된 관리 worktree의 같은 ExecPlan에서 제안별 `User Decision`을 수정해 승인, 거절 또는 대체 patch를 명시한다. ExecPlan이나 근거 문서의 다른 부분이 바뀐 사실만으로는 이 BLOCKED를 해제하지 않는다. Loop Manager는 대상 경로와 사용자가 확정한 patch의 결정적인 hash를 승인 기록에 결합한 뒤 같은 ExecPlan과 Worker 세션을 재개한다.

승인 또는 대체된 patch가 있으면 Worker는 그 내용만 `AGENTS.md`에 적용한다. 실제 diff가 사용자가 확정한 patch와 다르거나 재개 후 새로운 변경이 필요해지면 기존 승인은 무효이며, Worker는 최신 정확한 patch를 ExecPlan에 다시 제안하고 승인을 기다려야 한다. 사용자가 거절하면 Worker는 `AGENTS.md`를 변경하지 않고 재개하며, 승인된 규칙 없이 Current Goals를 달성할 수 없으면 그 이유와 새 결정 사항을 기록하고 다시 `BLOCKED`로 전환한다.

승인된 기준 `AGENTS.md`는 향후 프로젝트 설정에 사용하는 공통 원본이 된다. 프로젝트 전용 승인은 해당 프로젝트에만 적용한다. 어느 경우에도 이미 등록된 다른 프로젝트에는 자동으로 배포하지 않으며 사용자가 필요할 때 공통 파일 복사 도구나 별도 변경으로 반영한다.

## Acceptance Criteria

- Planner의 허용 변경 범위에 `docs/plans/*.md`와 `docs/FUTURE_IDEAS.md`가 포함된다.
- Planner는 현재 범위 밖의 아이디어만 FUTURE_IDEAS에 기록하며 해당 아이디어를 같은 계획 주기의 ExecPlan으로 만들지 않는다.
- Planner가 FUTURE_IDEAS 외의 비허용 파일을 변경하면 기존 계획 범위 위반과 동일하게 거부된다.
- Worker는 `AGENTS.md`를 직접 수정하지 않고 선택된 ExecPlan의 `Decision Log`에 대상, 범위, 이유, 영향과 정확한 patch를 기록한다.
- 모든 프로젝트의 Worker는 현재 프로젝트의 `AGENTS.md` 변경을 제안할 수 있다.
- Loop Manager 자체 저장소의 Worker만 향후 프로젝트 설정에 사용하는 기준 `AGENTS.md` 공통 규칙 변경을 제안할 수 있다.
- Planner와 Reviewer가 `AGENTS.md` 변경을 제안하거나 적용하면 기존 역할 범위 위반으로 차단된다.
- 유효한 제안을 기록한 Worker의 현재 ExecPlan은 기존 변경과 세션을 보존한 채 `BLOCKED`로 전환된다.
- 승인 전에 `AGENTS.md` 파일, commit, verify, Review, push와 PR 생성이 변경되거나 실행되지 않는다.
- 사용자는 보존된 관리 worktree의 같은 ExecPlan에 승인, 거절 또는 정확한 대체 patch를 기록할 수 있다.
- 제안·결정란 이외의 ExecPlan 수정이나 근거 문서 변경만으로는 AGENTS 변경 승인을 기다리는 BLOCKED를 해제하지 않는다.
- 사용자 결정은 대상 `AGENTS.md` 경로와 확정된 patch의 결정적인 hash에 결합돼 SQLite에 기록된다.
- 승인 또는 대체 patch가 있으면 같은 Worker 세션과 worktree에서 재개해 확정된 patch만 적용한다.
- 사용자가 거절하면 `AGENTS.md`를 변경하지 않고 같은 Worker 세션을 재개한다.
- 승인 후 실제 `AGENTS.md` diff가 확정된 patch와 다르면 기존 승인을 무효화하고 최신 patch에 대한 새 결정을 요구한다.
- 승인된 변경은 관련 targeted test와 전체 verify 및 읽기 전용 Review를 정상적으로 통과해야 한다.
- BLOCKED 사유와 알림에는 대상 ExecPlan, 제안이 기록된 위치와 사용자가 수정할 `User Decision` 위치가 포함된다.
- 승인된 `AGENTS.md` 변경은 해당 구현 PR에 포함되며 자동 Merge하지 않는다.
- 기존 등록 프로젝트의 `AGENTS.md`는 자동 변경하지 않으며 등록 시에만 수행하는 공통 내용 검사 정책도 바꾸지 않는다.
- 공통 `docs/PLANS.md`에 `Decision Log`의 AGENTS 변경 제안과 `User Decision` 형식 및 사용자 편집 방법을 문서화한다.
- Planner FUTURE_IDEAS 허용 범위, 비허용 파일 차단, 프로젝트 전용·공통 제안 구분, 승인 전 파일 불변, 명시적 사용자 결정, 거절 후 재개, patch 변경에 따른 재승인과 승인 후 정확한 적용을 자동 테스트로 검증한다.

## Product Impact

- Planner가 현재 범위 밖의 아이디어를 구현 계획과 분리해 보존할 수 있다.
- Worker가 구현 중 발견한 프로젝트 전용 또는 공통 Agent 규칙 변경 필요성은 실제 파일을 먼저 수정하지 않고 현재 ExecPlan에 보존된다.
- Agent 규칙은 사용자가 ExecPlan에서 정확한 patch를 확정한 경우에만 `AGENTS.md`에 반영된다.
- 기준 규칙의 변경이 기존 프로젝트에 예기치 않게 자동 배포되지 않는다.

## Non-Goals

- Planner가 `AGENTS.md`를 변경하도록 허용하지 않는다.
- Reviewer가 `AGENTS.md`를 변경하거나 변경안을 작성하도록 허용하지 않는다.
- Worker가 승인 전에 `AGENTS.md`를 직접 수정하도록 허용하지 않는다.
- 현재 ExecPlan과 무관한 편의 규칙을 같은 작업의 `AGENTS.md` 변경 제안으로 포함하지 않는다.
- 기준 `AGENTS.md` 변경을 기존 등록 프로젝트에 자동으로 commit, push 또는 PR 생성하지 않는다.
- 지정된 `User Decision` 이외의 문서 변경이나 재실행만으로 `AGENTS.md` 변경 BLOCKED를 해제하지 않는다.
- FUTURE_IDEAS에 기록한 항목을 자동으로 Requirement 또는 ExecPlan으로 승격하지 않는다.

## Open Questions

- 없음
