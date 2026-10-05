# [R-002] Merge-Independent Worktree Cleanup

## Context

현재 관리 worktree 정리는 로컬 HEAD가 `origin/<base-branch>`의 조상인지 검사한다. 이 관계는 Merge commit에는 성립하지만 Squash merge와 Rebase merge에서는 commit hash가 바뀌므로, 모든 변경이 push되고 PR이 정상 Merge됐어도 worktree가 보존되고 사용자가 수동으로 정리해야 한다.

이 오인은 반복적인 사용자 개입을 만들고 다음 자동 실행을 방해한다. worktree 삭제 안전성은 base branch의 commit 모양이 아니라 미커밋 변경과 PR에 push되지 않은 로컬 commit의 존재로 판단해야 한다.

## Requirement

Loop Manager는 GitHub가 PR Merge를 확인한 뒤 PR의 head commit과 관리 worktree의 상태를 사용해 삭제 안전성을 판단해야 한다. Merge commit, Squash merge와 Rebase merge를 동일하게 지원하며, base branch ancestry를 push 여부의 근거로 사용하지 않아야 한다.

worktree가 clean하고 로컬 HEAD에 PR head 이후의 로컬 전용 commit이 없으면 원격 작업 branch가 삭제됐더라도 worktree를 자동 정리한다. 미커밋 변경, PR에 포함되지 않은 로컬 commit 또는 안전 여부를 확인할 수 없는 상태만 보존한다.

## Acceptance Criteria

- Merge commit으로 완료된 PR의 clean worktree를 자동 정리한다.
- Squash merge로 완료된 PR의 clean worktree를 base branch ancestry 실패로 보존하지 않고 자동 정리한다.
- Rebase merge로 완료된 PR의 clean worktree를 base branch ancestry 실패로 보존하지 않고 자동 정리한다.
- GitHub에서 원격 head branch가 삭제됐어도 저장되거나 조회된 PR head와 로컬 HEAD의 관계를 확인할 수 있으면 자동 정리한다.
- 추적 파일 수정, staged 변경 또는 untracked 파일이 있으면 worktree를 보존하고 원인을 `cleanup_pending`에 기록한다.
- PR head에 포함되지 않은 로컬 commit이 있으면 worktree를 보존하고 원인을 `cleanup_pending`에 기록한다.
- PR head 정보나 commit 관계를 확인할 수 없어 삭제 안전성을 증명하지 못하면 worktree를 보존하고 확인 실패 원인을 기록한다.
- `cleanup_pending`은 다음 ExecPlan과 프로젝트 완료를 막지 않으며, 이후 안전 조건이 충족되거나 경로가 사라지면 자동으로 해제된다.
- 각 Merge 방식, 삭제된 원격 branch, dirty worktree, 로컬 전용 commit과 확인 불가 상태를 자동 테스트로 검증한다.

## Product Impact

- GitHub의 Merge 방식과 관계없이 안전한 관리 worktree는 자동 정리된다.
- 실제로 보존해야 하는 로컬 변경과 확인 불가 상태만 사용자 확인 대상으로 남는다.

## Non-Goals

- PR을 자동 Merge하지 않는다.
- 미커밋 변경이나 PR에 없는 로컬 commit을 자동 삭제, reset 또는 강제 정리하지 않는다.
- 원격 작업 branch를 자동 삭제하지 않는다.

## Open Questions

- 없음
