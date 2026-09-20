# [R-010] Origin-Isolated Local Worktree Operation

## Context

사용자가 기본 worktree의 `PRODUCT.md`, Requirement나 다른 문서를 AI Agent와 수정하는 동안 외부 스케줄러가 Loop Manager를 실행할 수 있다. 현재 run 스크립트는 Loop Manager checkout에서 `git pull --ff-only`를 실행하고, Python 실행기는 대상 저장소의 기본 worktree가 dirty하거나 로컬 branch가 원격보다 앞서거나 diverge됐으면 자동 작업을 중단한다.

하지만 Planner 입력 지문, planning worktree와 ExecPlan 구현 worktree는 이미 최신 `origin/<base-branch>`를 기준으로 분리돼 있다. 로컬에서 아직 commit·push하지 않은 사용자 작업을 자동화 입력에서 제외하면서도 그대로 보존할 수 있으므로, 기본 worktree 상태가 별도 관리 worktree의 작업을 막을 필요는 없다.

Agent가 관리 worktree 밖을 수정하지 못하도록 하는 G-012의 안전 경계가 먼저 적용돼야 이 차단을 안전하게 제거할 수 있다.

## Requirement

Loop Manager의 계획과 구현은 대상 저장소 기본 worktree의 파일, index, HEAD와 로컬 전용 commit을 사용하지 않고 최신 `origin/<base-branch>`만 기준으로 수행해야 한다. 기본 worktree가 dirty, staged, untracked, ahead, behind 또는 diverged 상태여도 해당 상태만으로 Planner나 Worker 작업을 중단하지 않아야 한다.

run 스크립트는 실행 전에 Loop Manager checkout을 pull하거나 다른 방식으로 수정하지 않고 현재 설치된 Python 프로그램만 실행해야 한다. Loop Manager 자체 업데이트는 자동 작업 실행과 분리한다.

프로젝트 등록에 필요한 공통 문서와 파일 내용 검사는 로컬 worktree가 아니라 등록할 `origin/<base-branch>`의 tree와 blob을 기준으로 수행해야 한다. 등록 이후 자동 실행에서는 등록 시에만 필요한 공통 파일 검사를 반복하지 않는다.

이 Requirement에서 생성하는 ExecPlan은 G-012 Agent Scope and Workspace Recovery Safety를 `depends_on`으로 지정하고, G-012가 MERGED된 뒤에만 구현을 시작해야 한다.

## Acceptance Criteria

- `run.sh`와 `run.ps1`은 `git pull`, checkout, reset, stash, clean, commit 또는 대상 checkout의 파일 변경을 수행하지 않는다.
- run 스크립트의 제거된 pull 동작을 Python 실행기나 다른 자동 갱신 동작으로 대체하지 않는다.
- 대상 저장소의 tracked 수정, staged 변경과 untracked 파일이 있어도 계획 또는 구현 작업을 시작할 수 있다.
- 로컬 base branch가 `origin/<base-branch>`보다 ahead, behind 또는 diverged 상태여도 자동 작업의 입력과 시작 여부에는 영향을 주지 않는다.
- 기본 worktree가 어떤 branch를 checkout하고 있는지와 관계없이 자동 작업은 `origin/<base-branch>`에서 시작한다.
- Planner 입력 지문은 `origin/<base-branch>`의 commit tree만 사용하며 로컬 파일, index와 로컬 전용 commit을 포함하지 않는다.
- planning worktree는 `origin/<base-branch>`의 detached HEAD에서 만들고, ExecPlan 구현 branch와 worktree도 해당 원격 기준에서 만든다.
- 기존 활성 ExecPlan은 저장된 Loop Manager 소유 branch와 관리 worktree에서만 재개한다.
- 프로젝트 등록 시 필수 문서, 공통 `AGENTS.md` 내용과 `docs/PLANS.md` 검사는 `origin/<base-branch>`에 commit된 파일을 기준으로 수행한다.
- 로컬에서만 추가·수정·삭제한 문서 때문에 등록 검사가 성공하거나 실패하지 않는다.
- 등록 이후 자동 실행은 등록 시 전용 공통 파일 검사를 로컬 worktree에서 다시 수행하지 않는다.
- 기본 worktree의 파일 내용, index와 HEAD는 실행 전후에 동일하게 유지된다.
- 로컬 미commit 변경과 로컬 전용 commit은 관리 worktree로 복사하거나 자동 commit·push하지 않는다.
- Planner가 base branch에 ExecPlan을 push하거나 구현 PR이 Merge돼 로컬 branch가 원격보다 뒤처져도 로컬 worktree를 자동 갱신하지 않는다.
- `git fetch`와 `git worktree add/remove`처럼 기본 worktree의 파일, index와 HEAD를 바꾸지 않는 공유 Git metadata 작업은 계속 허용한다.
- 동시 commit 등으로 Git lock 충돌이 발생하면 dirty 상태와 별개인 일시적 Git 오류로 처리하며 잠금을 우회하거나 사용자 작업을 변경하지 않는다.
- Loop Manager 저장소 자체가 관리 대상인 경우에도 문서의 로컬 미commit 변경만으로 자동 작업이 차단되지 않는다.
- G-012가 MERGED되지 않은 동안 이 Requirement의 ExecPlan은 Worker가 시작할 수 없다.
- dirty·staged·untracked, ahead·behind·diverged, 다른 checkout branch, 로컬 문서 추가·수정·삭제와 Git lock 충돌을 자동 테스트로 검증한다.
- 각 테스트는 자동 작업 후 기본 worktree의 파일, index와 HEAD가 보존되고 관리 worktree가 원격 기준만 포함하는지 확인한다.

## Product Impact

- 사용자는 기본 worktree에서 문서를 작성하는 동안에도 Loop Manager의 원격 기준 자동 작업을 계속 실행할 수 있다.
- 로컬에서 진행 중인 변경과 개별 ExecPlan 구현은 서로의 입력이나 작업 공간에 포함되지 않는다.
- Loop Manager 실행과 Loop Manager 자체 코드 업데이트가 분리된다.

## Non-Goals

- 로컬 미commit 변경이나 로컬 전용 commit을 Planner 또는 Worker 입력에 포함하지 않는다.
- 사용자의 기본 worktree를 자동 commit, push, pull, rebase, merge, stash, reset 또는 clean하지 않는다.
- 기본 worktree와 원격 branch의 차이를 자동으로 해소하지 않는다.
- Loop Manager 프로그램을 run 스크립트에서 자동 업데이트하지 않는다.
- 다른 프로세스가 보유한 Git lock을 제거하거나 우회하지 않는다.
- 관리 worktree 밖을 수정할 수 있는 Agent 권한을 허용하지 않는다.

## Open Questions

- 없음
