# [R-010] Origin-Isolated Local Worktree Operation

## Context

사용자가 기본 worktree의 `PRODUCT.md`, Requirement나 다른 문서를 AI Agent와 수정하는 동안 외부 스케줄러가 Loop Manager를 실행할 수 있다. launcher hotfix 전 run 스크립트는 Loop Manager checkout에서 `git pull --ff-only`를 실행했고, Python 실행기는 대상 저장소의 기본 worktree가 dirty하거나 로컬 branch가 원격보다 앞서거나 diverge됐으면 자동 작업을 중단한다.

run 스크립트의 pull이 Worker 실행 중 시작되면 Loop Manager 저장소의 로컬 `main`, shared refs와 기본 worktree 상태가 바뀐다. Worker의 관리 worktree 내용은 그대로여도 Agent 범위 검사는 이를 Worker가 만든 공유 Git 변경과 구별할 수 없어 안전 위반으로 차단한다.

하지만 Planner 입력 지문, planning worktree와 ExecPlan 구현 worktree는 이미 최신 `origin/<base-branch>`를 기준으로 분리돼 있다. 로컬에서 아직 commit·push하지 않은 사용자 작업을 자동화 입력에서 제외하면서도 그대로 보존할 수 있으므로, 기본 worktree 상태가 별도 관리 worktree의 작업을 막을 필요는 없다.

Agent가 관리 worktree 밖을 수정하지 못하도록 하는 G-012의 안전 경계가 먼저 적용돼야 이 차단을 안전하게 제거할 수 있다.

## Requirement

Loop Manager의 계획과 구현은 대상 저장소 기본 worktree의 파일, index, HEAD와 로컬 전용 commit을 사용하지 않고 최신 `origin/<base-branch>`만 기준으로 수행해야 한다. 기본 worktree가 dirty, staged, untracked, ahead, behind 또는 diverged 상태여도 해당 상태만으로 Planner나 Worker 작업을 중단하지 않아야 한다.

run 스크립트는 Git을 실행하지 않고 현재 checkout의 Python 부트스트랩만 시작해야 한다. 부트스트랩은 다른 Git 작업보다 먼저 프로세스 lock을 획득한 뒤 Loop Manager 저장소의 `origin/main`을 fetch하고, 그 immutable commit SHA의 detached runtime worktree를 사용자 상태 디렉터리에 생성하거나 재사용한다. 부모 부트스트랩은 같은 lock을 유지한 채 runtime worktree의 `loop_manager.py`를 자식 프로세스로 실행하고 자식이 끝난 뒤에만 lock을 해제해야 한다.

runtime worktree는 로컬 저장소의 object database를 공유하지만 사용자 기본 worktree와 별도 경로 및 HEAD를 사용한다. 로컬 `main`의 미commit 변경과 로컬 전용 commit은 runtime에 반영하지 않고, push로 `origin/main`이 바뀐 뒤 시작되는 다음 실행만 새 SHA의 runtime을 선택한다. 이미 실행 중인 runtime worktree의 HEAD와 파일은 변경하지 않는다. Loop Manager 저장소의 사용자 `main` 갱신은 자동 실행과 분리하며 독립 clone을 추가로 관리하지 않는다.

Worker는 관리 worktree의 파일과 targeted test만 담당하며 commit, fetch, pull, rebase, push, worktree 등록과 PR 생성 같은 Git 쓰기를 수행하지 않는다. 이 Git 쓰기는 Agent 호출 밖의 Loop Manager 코드만 담당한다. 따라서 실행 중 사용자 기본 worktree와 로컬 base ref가 바뀌어도 Worker worktree 자체와 Loop Manager 소유 branch가 보존돼 있으면 이를 Worker 범위 위반으로 처리하지 않는다.

프로젝트 등록에 필요한 공통 문서와 파일 내용 검사는 로컬 worktree가 아니라 등록할 `origin/<base-branch>`의 tree와 blob을 기준으로 수행해야 한다. 등록 이후 자동 실행에서는 등록 시에만 필요한 공통 파일 검사를 반복하지 않는다.

이 Requirement에서 생성하는 ExecPlan은 G-012 Agent Scope and Workspace Recovery Safety를 `depends_on`으로 지정하고, G-012가 MERGED된 뒤에만 구현을 시작해야 한다.

## Acceptance Criteria

- `run.sh`와 `run.ps1`은 Git 명령이나 대상 checkout의 파일 변경을 수행하지 않고 현재 checkout의 Python 부트스트랩에 인자를 그대로 전달한다.
- Python 부트스트랩은 fetch나 worktree 변경 전에 `<state-dir>/loop-manager.lock`을 실제로 획득하며, lock 파일 존재 여부만 검사하거나 다른 프로세스의 lock을 삭제·우회하지 않는다.
- 부트스트랩은 `origin/main`을 fetch한 뒤 정확한 commit SHA를 확정하고 `<state-dir>` 아래의 detached runtime worktree에서 해당 SHA의 프로그램을 실행한다. 별도 clone은 만들지 않는다.
- 부모 부트스트랩은 runtime 자식이 종료될 때까지 lock을 보유하고, runtime 자식은 같은 lock을 중복 획득하지 않는다.
- 로컬 `main`의 파일, index, HEAD와 로컬 전용 commit은 runtime 선택에 사용하지 않는다. push된 새 `origin/main`은 다음 실행에만 반영되고 실행 중 runtime은 시작 SHA에 고정된다.
- 기존 runtime 경로가 예상 SHA의 clean detached worktree가 아니거나 소유 경계를 확인할 수 없으면 reset, clean 또는 덮어쓰지 않고 진단 가능한 오류로 중단한다.
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
- Worker가 실행되는 동안 사용자가 기본 worktree의 파일, index, HEAD와 로컬 base branch를 변경해도 관리 worktree나 Loop Manager 소유 branch가 바뀌지 않은 한 Agent 범위 위반으로 차단하지 않는다.
- Worker 반환 후 commit, 최신 base fetch·rebase, push와 PR 생성은 Loop Manager 코드가 수행하며 Worker prompt와 범위 검사는 Worker의 직접 Git 쓰기를 계속 금지한다.
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
- 실행 코드는 push된 `origin/main`의 고정 SHA를 따르고 사용자 `main`은 독립적으로 편집할 수 있다.

## Non-Goals

- 로컬 미commit 변경이나 로컬 전용 commit을 Planner 또는 Worker 입력에 포함하지 않는다.
- 사용자의 기본 worktree를 자동 commit, push, pull, rebase, merge, stash, reset 또는 clean하지 않는다.
- 기본 worktree와 원격 branch의 차이를 자동으로 해소하지 않는다.
- 사용자 기본 worktree의 Loop Manager 프로그램을 자동 pull, checkout, reset 또는 다른 방식으로 업데이트하지 않는다.
- runtime을 위해 별도 Git clone이나 독립 object database를 만들지 않는다.
- 실행 중인 runtime worktree를 새 `origin/main`으로 전환하지 않는다.
- 다른 프로세스가 보유한 Git lock을 제거하거나 우회하지 않는다.
- 관리 worktree 밖을 수정할 수 있는 Agent 권한을 허용하지 않는다.

## Open Questions

- 없음
