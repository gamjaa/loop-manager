# [R-039] PLAN_BLOCKED Remote Branch Change Recovery

## Context

PR이 없는 ExecPlan이 `PLAN_BLOCKED`되면 Loop Manager는 안전한 변경을 해당 작업 브랜치에 checkpoint로 push하고 사용자 처리를 기다린다. 현재 게시된 checkpoint 브랜치의 원격 OID가 달라지면 외부 변경으로 보고 해제 검사를 건너뛰며, 일반적인 차단 입력 변경도 주로 `origin/<base-branch>`와 로컬 worktree에서 확인한다. 사용자가 안내받은 원격 작업 브랜치에 수정 commit을 추가해도 로컬 작업 공간과 해제 판단에 반영되지 않아 계획이 계속 차단된다. 이 Requirement는 현재 PRODUCT의 게시 후 원격 checkpoint 브랜치 변경을 일괄 거부하는 규칙을 안전하게 확인된 후속 commit에 한해 변경한다.

## Requirement

Loop Manager는 게시된 checkpoint를 가진 PR 없는 `PLAN_BLOCKED` ExecPlan마다 저장된 작업 브랜치의 원격 head를 확인한다. 새 원격 commit이 마지막으로 확인한 해당 브랜치 head의 후속이며 기존 checkpoint를 포함하면, 정확한 원격 작업 브랜치를 가져와 관리 branch·worktree의 소유권과 Git 상태를 검증한 뒤 사용자 commit을 보존하는 방식으로 반영한다. `origin/<base-branch>`만 바뀌지 않았다는 이유로 이 변경을 무시하지 않는다.

원격 브랜치의 새 내용은 차단 원인별 기존 해제 조건에 맞춰 재평가한다. ExecPlan·근거 문서뿐 아니라 차단 원인을 해결하기 위해 작업 브랜치에서 수정한 코드·테스트 등도 실제 후보 입력으로 확인한다. 새 commit이 있다는 사실만으로 해제하지 않으며, 계획 선언·source 관계·작업 범위와 안전 조건이 유효하고 원인이 해결됐을 때만 동일 ExecPlan을 기존 branch와 worktree에서 재개한다. 재개 뒤에는 해당 revision의 필요한 Worker 검토, targeted test, 전체 verify 및 Review를 정상 순서대로 수행하고 사용자 commit을 이미 검증된 결과로 간주하지 않는다.

원격 branch가 삭제·재작성·분기됐거나 다른 계획의 branch로 확인되는 경우, 로컬 미게시 commit·변경 파일 또는 진행 중인 Git operation을 안전하게 보존하며 반영할 수 없는 경우에는 자동 덮어쓰기·reset·stash·강제 push 없이 `PLAN_BLOCKED`를 유지하고 구체적인 원인을 알린다. 새 commit을 안전하게 반영했어도 차단 원인이 남아 있으면 현재 원격 head와 검사 결과를 기억해 같은 입력에서 반복 해제 시도나 알림을 만들지 않고 다음 관련 변경을 기다린다. 독립 READY와 다른 차단 계획의 진행은 기존대로 유지한다.

기존 PR이 닫혀 생긴 `PLAN_BLOCKED`는 같은 PR의 reopen 조건을 유지하고, 게시되지 않은 checkpoint나 소유권을 증명할 수 없는 legacy branch는 새 원격 commit만으로 자동 인수하지 않는다. 기존 차단·재개 알림 계약을 적용한다.

## Acceptance Criteria

- PR 없는 `PLAN_BLOCKED` 계획의 checkpoint branch에 사용자가 기존 게시 commit의 후속 commit을 push하고 base branch는 그대로인 경우, 다음 실행에서 정확한 원격 작업 브랜치를 감지·반영하고 해당 계획의 해제 조건을 재평가한다.
- 사용자 commit이 코드·테스트만 바꿔 `origin/<base-branch>`의 ExecPlan·Requirement 지문이 그대로여도, 그 변경이 차단 원인을 해결했는지 확인한다. 관련 결정·검증이 여전히 부족하면 차단을 유지한다.
- 원격 변경과 로컬 branch·worktree를 안전하게 동기화할 수 있고 원인이 해결됐으면 원래 ExecPlan, branch와 worktree를 보존해 정상 실행으로 전환한다. 변경된 revision에 필요한 검증과 Review를 건너뛰거나 사용자 commit만으로 PR을 생성하지 않는다.
- 원격 후속 commit을 반영했으나 원인이 남아 있으면 실제 남은 이유를 기록하고, 원격 head 및 관련 입력이 같을 때 같은 해제 시도와 차단 알림을 반복하지 않는다. 이후 새 commit 또는 다른 관련 입력 변경이 생기면 다시 확인한다.
- 원격 branch 삭제·재작성·분기, 잘못된 plan identity, 소유권 불확실성, 변경 범위 위반 또는 로컬 미게시 변경과 충돌이 있으면 기존 작업을 보존한 채 그 계획만 차단 상태로 유지하고 사용자에게 정확한 원인을 표시한다. 원격 ref나 사용자 commit을 자동 덮어쓰지 않는다.
- 게시된 checkpoint가 없는 차단과 PR close로 인한 차단은 새 원격 commit만으로 해제하지 않는다. 다른 독립 계획의 선택과 기존 차단·재개 알림 중복 방지 규칙은 유지한다.

## Product Impact

- 사용자가 안내된 원격 작업 브랜치에 안전한 후속 commit으로 차단 원인을 수정하면 Loop Manager가 이를 확인하고 같은 계획을 재개할 수 있다.

## Non-Goals

- 원격 branch의 force-push·재작성이나 로컬 사용자 변경을 자동 병합하지 않는다.
- 다른 계획의 branch 또는 소유권이 불명확한 원격 branch를 작업 공간으로 채택하지 않는다.
- 닫힌 PR의 reopen 요건이나 프로젝트 전체 차단의 안전 gate를 완화하지 않는다.

## Open Questions

- 없음
