---
id: G-007
title: Merge-Independent Worktree Cleanup
sources:
  - docs/requirements/[R-002] Merge-Independent Worktree Cleanup.md
depends_on:
  - G-006
complexity: medium
risk: high
---

## Purpose

Merge된 PR의 head commit을 기준으로 관리 worktree에 보존해야 할 변경이 있는지 판단해 Merge commit, Squash merge와 Rebase merge를 같은 방식으로 안전하게 정리하고, 정리할 수 없는 worktree는 다음 작업을 막지 않는 `cleanup_pending`으로 추적한다.

## Acceptance Criteria

- GitHub에서 Merge된 PR을 확인할 때 PR head commit을 함께 조회하고, 원격 작업 branch가 이미 삭제됐어도 저장하거나 조회한 head commit으로 정리 안전성을 판단한다.
- 관리 worktree가 clean하고 로컬 `HEAD`의 모든 commit이 PR head에 포함돼 있으면 base branch ancestry나 Merge 방식과 관계없이 강제 옵션 없이 worktree를 제거한다.
- Merge commit, Squash merge와 Rebase merge 각각에서 base branch가 로컬 `HEAD`를 직접 포함하지 않는 경우에도 안전한 worktree를 자동 정리한다.
- 추적 파일 수정, staged 변경 또는 untracked 파일이 있으면 worktree를 제거하지 않고 경로와 구체적인 dirty 사유를 `cleanup_pending`에 저장한다.
- 로컬 `HEAD`에 PR head 이후의 로컬 전용 commit이 있으면 worktree를 제거하지 않고 그 사유를 `cleanup_pending`에 저장한다.
- PR head가 없거나 로컬 Git object와의 commit 관계를 확인할 수 없는 경우 안전하다고 추정하지 않고 worktree와 확인 실패 사유를 보존한다.
- Merge된 활성 작업은 worktree 보존 여부와 관계없이 완료 상태에서 분리돼 다음 ExecPlan 처리와 프로젝트 완료를 막지 않는다.
- 이후 실행에서 각 `cleanup_pending`을 다시 확인해 안전 조건이 충족되면 worktree를 제거하고, 경로가 이미 사라졌으면 제거 없이 해당 기록을 해제한다.
- 사용자 변경, 로컬 전용 commit과 원격 작업 branch를 자동 삭제하거나 reset하지 않으며, 정리 과정에서 `git worktree remove --force`를 사용하지 않는다.
- 각 Merge 방식, 삭제된 원격 branch, tracked·staged·untracked 변경, 로컬 전용 commit, PR head 또는 commit 관계 확인 실패, pending 재확인과 다음 작업 비차단을 단위 테스트로 검증한다.

## Implementation

- PR 조회 결과에 immutable head commit OID를 포함하고, Merge 확인 시 해당 OID를 worktree 안전성 판단과 보존 기록에 사용한다.
- worktree 존재 여부, porcelain 상태, 로컬 `HEAD`와 PR head의 commit 포함 관계를 검사해 `safe`, `dirty`, `local-only commits`, `unverifiable`을 구분하는 부작용 없는 판정 경계를 만든다. base branch와의 ancestry는 이 판정에 사용하지 않는다.
- 기존 DB를 보존하는 호환 가능한 SQLite migration으로 프로젝트별 여러 보존 worktree를 기록할 수 있는 `cleanup_pending` 저장소를 추가하고, worktree 경로, PR, PR head와 최신 원인을 원자적으로 기록·해제한다.
- Merge 동기화 경로에서 안전하면 비강제 worktree 제거 후 활성 작업을 비우고, 안전하지 않거나 확인할 수 없으면 pending 기록을 남긴 뒤 활성 작업을 비워 다음 계획과 구현이 진행되게 한다.
- 매 실행 초기에 pending 기록을 독립적으로 재검사해 경로 소실 또는 안전한 정리를 반영하되, 남아 있는 pending은 경고로만 보고 현재 프로젝트의 계획, Worker 선택과 완료 판단을 중단하지 않는다.
- README의 Merge 후 정리 설명을 실제 `cleanup_pending` 동작과 사용자 보존 조건에 맞게 갱신하고, 현재 verify 스크립트가 추가된 단위 테스트를 계속 포함하는지 확인한다.

## Validation

- `scripts/verify.ps1`
- `scripts/verify.sh`
- 임시 Git 저장소에서 PR head와 로컬 `HEAD` 관계 및 base branch 모양을 바꿔 Merge commit, Squash merge, Rebase merge와 로컬 전용 commit을 검증하는 단위 테스트
- tracked·staged·untracked 변경과 PR head object 부재 또는 관계 확인 실패가 구체적인 pending 원인으로 보존되는지 검증하는 단위 테스트
- 삭제된 원격 branch에서도 저장된 PR head로 안전하게 정리하고, pending worktree의 재검사·경로 소실 해제·다음 작업 비차단을 검증하는 상태 전이 단위 테스트
- 기존 SQLite DB에 cleanup 저장소를 추가해도 프로젝트와 활성 작업 상태가 유지되는 migration 단위 테스트

## Progress

- [ ] PR head 기반 worktree 안전성 판정 구현
- [ ] `cleanup_pending` 영속화와 호환 migration 구현
- [ ] Merge 완료 분리 및 pending 재확인 흐름 연결
- [ ] Merge 방식·보존 조건·비차단 경계 단위 테스트 추가

## Decision Log

- base branch의 commit 모양은 Merge 방식에 따라 달라지므로 삭제 안전성의 근거에서 제외하고, PR에 실제 push된 head commit과 로컬 worktree 상태만 사용한다.
- 정리 실패는 Merge된 작업의 실행 상태가 아니므로 별도 pending 저장소로 분리해 여러 보존 worktree를 추적하면서도 후속 작업을 계속한다.
