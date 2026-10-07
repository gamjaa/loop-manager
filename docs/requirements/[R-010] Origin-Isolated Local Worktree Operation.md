# [R-010] Origin-Isolated Local Worktree Operation

## Context

사용자가 기본 worktree의 `PRODUCT.md`, Requirement나 다른 문서를 AI Agent와 수정하는 동안 외부 스케줄러가 Loop Manager를 실행할 수 있다. launcher hotfix 전 run 스크립트는 Loop Manager checkout에서 `git pull --ff-only`를 실행했고, Python 실행기는 대상 저장소의 기본 worktree가 dirty하거나 로컬 branch가 원격보다 앞서거나 diverge됐으면 자동 작업을 중단한다.

run 스크립트의 pull이 Worker 실행 중 시작되면 Loop Manager 저장소의 로컬 `main`, shared refs와 기본 worktree 상태가 바뀐다. Worker의 관리 worktree 내용은 그대로여도 Agent 범위 검사는 이를 Worker가 만든 공유 Git 변경과 구별할 수 없어 안전 위반으로 차단한다.

launcher pull 제거 뒤 pinned runtime이 도입되기 전까지는 자동 실행이 source checkout의 현재 코드만 사용하므로, Planner가 base branch에 계획을 게시하거나 구현 PR이 Merge돼도 로컬 `main`은 자동으로 최신 코드를 받지 않는다. 실행 중 변경 문제를 다시 만들지 않으면서 이 전환 기간의 업데이트 지연을 줄일 한시적 경계가 필요하다.

하지만 Planner 입력 지문, planning worktree와 ExecPlan 구현 worktree는 이미 최신 `origin/<base-branch>`를 기준으로 분리돼 있다. 로컬에서 아직 commit·push하지 않은 사용자 작업을 자동화 입력에서 제외하면서도 그대로 보존할 수 있으므로, 기본 worktree 상태가 별도 관리 worktree의 작업을 막을 필요는 없다.

Agent가 관리 worktree 밖을 수정하지 못하도록 하는 G-012의 안전 경계가 먼저 적용돼야 이 차단을 안전하게 제거할 수 있다.

## Requirement

Loop Manager의 계획과 구현은 대상 저장소 기본 worktree의 파일, index, HEAD와 로컬 전용 commit을 사용하지 않고 최신 `origin/<base-branch>`만 기준으로 수행해야 한다. 기본 worktree가 dirty, staged, untracked, ahead, behind 또는 diverged 상태여도 해당 상태만으로 Planner나 Worker 작업을 중단하지 않아야 한다.

각 등록 프로젝트는 원격 fetch 뒤 Agent나 저장소 변경 단계를 시작하기 전에 등록된 기본 worktree를 안전하게 최신화할 수 있어야 한다. 기본 worktree에 tracked, staged 또는 non-ignored untracked 변경이 없고 저장된 base branch를 checkout했으며 로컬 base가 `origin/<base-branch>`와 같거나 그 조상인 경우에만, 이미 fetch한 원격 ref를 `git merge --ff-only origin/<base-branch>`와 동등한 방식으로 반영한다. 이는 중복 fetch가 없는 `git pull --ff-only`의 update 단계다. behind-only이면 파일, index와 로컬 base ref를 fast-forward하고 이미 같으면 no-op으로 끝낸다.

기본 worktree가 tracked 수정, staged, non-ignored untracked, 다른 branch, detached HEAD, ahead, diverged 또는 진행 중인 Git operation 상태이면 자동 갱신을 건너뛰고 진단 이유를 기록하되 최신 원격 SHA 기반 계획과 구현은 계속한다. `config/config.ini`, `.venv`와 cache 같은 ignored 파일은 존재 자체로 갱신을 막지 않지만, local-to-remote fast-forward가 바꾸는 tracked 경로 또는 그 부모·자식 경로와 충돌하면 해당 ignored 파일을 보호하기 위해 갱신을 건너뛴다. eligibility 확인 뒤 동시 사용자 변경이나 Git lock으로 fast-forward가 실패해도 reset, clean, stash, 강제 checkout 또는 재시도 중 잠금 제거를 하지 않고 기존 checkout을 보존한 채 원격 SHA 기반 작업을 계속한다. 기본 worktree 동기화는 사용자의 편의를 위한 부수 효과이며 Planner fingerprint, 등록 검사와 관리 worktree의 입력은 갱신된 로컬 파일이 아니라 fetch된 `origin/<base-branch>`를 계속 사용한다.

run 스크립트는 Git을 실행하지 않고 현재 checkout의 Python 부트스트랩만 시작해야 한다. 부트스트랩은 다른 Git 작업보다 먼저 프로세스 lock을 획득한 뒤 Loop Manager 저장소의 `origin/main`을 fetch하고, 그 immutable commit SHA의 detached runtime worktree를 사용자 상태 디렉터리에 생성하거나 재사용한다. 부모 부트스트랩은 같은 lock을 유지한 채 runtime worktree의 `loop_manager.py`를 자식 프로세스로 실행하고 자식이 끝난 뒤에만 lock을 해제해야 한다.

runtime worktree는 로컬 저장소의 object database를 공유하지만 사용자 기본 worktree와 별도 경로 및 HEAD를 사용한다. 로컬 `main`의 미commit 변경과 로컬 전용 commit은 runtime에 반영하지 않고, push로 `origin/main`이 바뀐 뒤 시작되는 다음 실행만 새 SHA의 runtime을 선택한다. 이미 실행 중인 runtime worktree의 HEAD와 파일은 변경하지 않는다. bootstrap 자체는 Loop Manager 저장소의 사용자 `main`을 갱신하지 않으며 독립 clone을 추가로 관리하지 않는다. Loop Manager 저장소가 등록 프로젝트이기도 하면 runtime 자식의 일반 프로젝트 동기화가 다른 프로젝트와 같은 안전 조건에서만 그 기본 worktree를 fast-forward할 수 있다.

Git에서 제외된 기존 `config/config.ini`는 runtime worktree로 복사, 이동 또는 symlink하지 않는다. source checkout의 bootstrap은 runtime을 시작하기 전에 기본 설정 경로를 source checkout의 기존 `config/config.ini`로 확정하고, 명시적인 `--config` 상대 경로도 source checkout 기준의 절대 경로로 정규화해 runtime 자식에 전달해야 한다. runtime은 전달받은 외부 설정을 시작 시 한 번 읽으며 source checkout의 설정 파일을 수정하지 않는다. 이 설정은 실행 revision을 선택하는 코드 입력이 아니라 사용자가 관리하는 운영 입력이므로, 로컬 `main`의 코드와 문서를 runtime에 포함하지 않는 원칙의 예외가 아니다.

ignored 파일을 일반적으로 runtime에 복제해서는 안 된다. source checkout의 `.venv`는 launcher와 bootstrap이 선택한 동일 Python interpreter를 runtime 자식에도 사용함으로써 재사용하고, `__pycache__`와 도구 cache 같은 파생 파일은 전달하지 않는다. 이후 다른 ignored 파일이 필수 운영 입력이 되면 해당 파일별 외부 경로와 소유·권한·전달 계약을 명시해야 하며, ignored 상태만으로 자동 복사 대상이 되지 않는다.

pinned runtime이 Merge되기 전의 전환 단계에서는 실제 process lock을 획득한 일반 `run`이 모든 Agent와 Manager 작업을 종료 코드 0으로 마친 뒤, lock을 해제하기 직전에만 Loop Manager source checkout을 조건부로 갱신할 수 있다. updater는 source checkout이 clean `main`이고 로컬 `main`이 fetch된 `origin/main`으로 fast-forward 가능한 경우에만 `git pull --ff-only origin main`과 동등한 갱신을 수행한다. `--skip-if-running`으로 lock을 획득하지 못한 중복 실행, 0이 아닌 종료, 다른 branch, dirty·staged·untracked, ahead·diverged 상태에서는 pull하지 않는다. updater가 실제 fast-forward를 시도하다 실패하면 프로젝트 실행 상태를 되돌리지 않고 오류를 기록해 0이 아닌 종료 코드로 끝낸다.

이 updater는 대상 프로젝트를 갱신하지 않고 Loop Manager source checkout에만 적용하는 임시 rollout 경계다. pinned runtime 실행이 구현되는 변경에서 updater와 관련 설정·분기를 제거해야 한다. 이후 runtime 선택은 source `main`을 수정하지 않고 새 `origin/main` SHA를 사용하며, source checkout 갱신은 Loop Manager 저장소 자체가 등록 프로젝트일 때 일반 프로젝트 동기화 조건을 만족하는 경우에만 별도로 수행한다.

Worker는 관리 worktree의 파일과 targeted test만 담당하며 commit, fetch, pull, rebase, push, worktree 등록과 PR 생성 같은 Git 쓰기를 수행하지 않는다. 이 Git 쓰기는 Agent 호출 밖의 Loop Manager 코드만 담당한다. 따라서 실행 중 사용자 기본 worktree와 로컬 base ref가 바뀌어도 Worker worktree 자체와 Loop Manager 소유 branch가 보존돼 있으면 이를 Worker 범위 위반으로 처리하지 않는다.

프로젝트 등록에 필요한 공통 문서와 파일 내용 검사는 로컬 worktree가 아니라 등록할 `origin/<base-branch>`의 tree와 blob을 기준으로 수행해야 한다. 등록 이후 자동 실행에서는 등록 시에만 필요한 공통 파일 검사를 반복하지 않는다.

이 Requirement에서 생성하는 ExecPlan은 G-012 Agent Scope and Workspace Recovery Safety를 `depends_on`으로 지정하고, G-012가 MERGED된 뒤에만 구현을 시작해야 한다.

## Acceptance Criteria

- `run.sh`와 `run.ps1`은 Git 명령이나 대상 checkout의 파일 변경을 수행하지 않고 현재 checkout의 Python 부트스트랩에 인자를 그대로 전달한다.
- Python 부트스트랩은 fetch나 worktree 변경 전에 `<state-dir>/loop-manager.lock`을 실제로 획득하며, lock 파일 존재 여부만 검사하거나 다른 프로세스의 lock을 삭제·우회하지 않는다.
- 부트스트랩은 `origin/main`을 fetch한 뒤 정확한 commit SHA를 확정하고 `<state-dir>` 아래의 detached runtime worktree에서 해당 SHA의 프로그램을 실행한다. 별도 clone은 만들지 않는다.
- 부모 부트스트랩은 runtime 자식이 종료될 때까지 lock을 보유하고, runtime 자식은 같은 lock을 중복 획득하지 않는다.
- 기존 source checkout의 `config/config.ini`는 이동·복사·symlink 없이 계속 사용된다. bootstrap은 기본 경로와 명시적인 `--config`를 source checkout 기준의 절대 경로로 확정해 runtime 자식에 전달하며, runtime은 자신의 `ROOT/config/config.ini`로 경로를 다시 계산하지 않는다.
- runtime 준비는 ignored 파일 전체를 복제하지 않는다. `.venv`는 현재 launcher가 선택한 interpreter를 재사용하고 cache와 그 밖의 ignored 파일은 runtime worktree에 전달하지 않는다.
- 로컬 `main`의 파일, index, HEAD와 로컬 전용 commit은 runtime 선택에 사용하지 않는다. push된 새 `origin/main`은 다음 실행에만 반영되고 실행 중 runtime은 시작 SHA에 고정된다.
- 기존 runtime 경로가 예상 SHA의 clean detached worktree가 아니거나 소유 경계를 확인할 수 없으면 reset, clean 또는 덮어쓰지 않고 진단 가능한 오류로 중단한다.
- pinned runtime 도입 전의 임시 updater는 process lock을 획득한 `run`의 종료 코드가 0이고 모든 Agent subprocess가 끝난 뒤에만, 같은 lock 안에서 Loop Manager source checkout의 clean `main`을 `origin/main`으로 fast-forward한다.
- `--skip-if-running` 중복 실행, 오류 종료, `init`, 다른 checkout branch, dirty·staged·untracked, ahead 또는 diverged source checkout은 임시 pull을 실행하지 않는다. pull을 건너뛴 사실은 프로젝트 상태나 원래 종료 코드를 바꾸지 않는다.
- 임시 fast-forward 시도가 실패하면 이미 완료된 프로젝트 상태와 작업 공간을 유지하고 오류를 기록한 뒤 0이 아닌 종료 코드를 반환한다.
- pinned runtime 구현이 Merge되면 bootstrap의 임시 updater가 제거되고 bootstrap은 source checkout의 파일, index, HEAD와 local `main`을 변경하지 않는다. 등록 프로젝트 동기화는 runtime 자식의 별도 경계에서만 수행한다.
- 대상 저장소의 tracked 수정, staged 변경과 untracked 파일이 있어도 계획 또는 구현 작업을 시작할 수 있다.
- 로컬 base branch가 `origin/<base-branch>`보다 ahead, behind 또는 diverged 상태여도 자동 작업의 입력과 시작 여부에는 영향을 주지 않는다.
- 등록 프로젝트의 기본 worktree에 tracked·staged·non-ignored untracked 변경이 없고 저장된 base branch를 checkout했으며 로컬 base가 원격 base와 같거나 behind-only이면 fetch 직후 `origin/<base-branch>`로 fast-forward한다. 이미 같은 경우 파일과 ref를 변경하지 않는다.
- tracked 수정, staged, non-ignored untracked, 다른 branch, detached HEAD, ahead·diverged 또는 진행 중인 Git operation이면 기본 worktree 갱신을 건너뛰고 이유를 기록하지만 원격 기준 자동 작업은 계속한다.
- ignored 파일은 존재 자체로 갱신을 막지 않는다. fast-forward가 변경할 tracked 경로와 ignored 파일 경로가 같거나 부모·자식으로 겹치면 ignored 파일을 덮어쓰지 않고 갱신을 건너뛴다.
- eligibility 확인 뒤 동시 변경, Git lock 또는 fast-forward 오류가 발생하면 사용자 상태를 reset·clean·stash하거나 잠금을 제거하지 않고 갱신을 건너뛴다. 이 실패만으로 ExecPlan이나 프로젝트를 BLOCKED로 전환하지 않는다.
- 기본 worktree가 어떤 branch를 checkout하고 있는지와 관계없이 자동 작업은 `origin/<base-branch>`에서 시작한다.
- Planner 입력 지문은 `origin/<base-branch>`의 commit tree만 사용하며 로컬 파일, index와 로컬 전용 commit을 포함하지 않는다.
- planning worktree는 `origin/<base-branch>`의 detached HEAD에서 만들고, ExecPlan 구현 branch와 worktree도 해당 원격 기준에서 만든다.
- 기존 활성 ExecPlan은 저장된 Loop Manager 소유 branch와 관리 worktree에서만 재개한다.
- 프로젝트 등록 시 필수 문서와 `docs/PLANS.md` 검사는 `origin/<base-branch>`에 commit된 파일을 기준으로 수행한다. R-064에 따라 실행 지침은 선택 runtime의 공통·역할별 원본을 사용한다.
- 로컬에서만 추가·수정·삭제한 문서 때문에 등록 검사가 성공하거나 실패하지 않는다.
- 등록 이후 자동 실행은 등록 시 전용 공통 파일 검사를 로컬 worktree에서 다시 수행하지 않는다.
- safe fast-forward 대상이 아닌 기본 worktree의 파일 내용, index와 HEAD는 실행 전후에 동일하게 유지된다. 대상이면 HEAD, index와 tracked 파일만 정확한 `origin/<base-branch>`로 함께 전진한다.
- 로컬 미commit 변경과 로컬 전용 commit은 관리 worktree로 복사하거나 자동 commit·push하지 않는다.
- Planner가 base branch에 ExecPlan을 push하거나 구현 PR이 Merge돼 로컬 branch가 원격보다 뒤처지면 다음 fetch 뒤 safe fast-forward 조건을 다시 평가해 등록된 기본 worktree를 갱신한다.
- Worker가 실행되는 동안 사용자가 기본 worktree의 파일, index, HEAD와 로컬 base branch를 변경해도 관리 worktree나 Loop Manager 소유 branch가 바뀌지 않은 한 Agent 범위 위반으로 차단하지 않는다.
- Worker 반환 후 commit, 최신 base fetch·rebase, push와 PR 생성은 Loop Manager 코드가 수행하며 공통+Worker 역할 instruction과 범위 검사는 Worker의 직접 Git 쓰기를 계속 금지한다.
- `git fetch`, 검증된 `git worktree add/remove`와 위 조건을 만족하는 기본 worktree의 `git merge --ff-only origin/<base-branch>`는 Loop Manager 코드가 수행할 수 있다.
- 동시 commit 등으로 Git lock 충돌이 발생하면 dirty 상태와 별개인 일시적 Git 오류로 처리하며 잠금을 우회하거나 사용자 작업을 변경하지 않는다.
- Loop Manager 저장소 자체가 관리 대상인 경우에도 같은 safe fast-forward 조건을 적용한다. 로컬 문서 변경이나 branch 차이로 갱신을 건너뛰더라도 자동 작업은 차단하지 않는다.
- G-012가 MERGED되지 않은 동안 이 Requirement의 ExecPlan은 Worker가 시작할 수 없다.
- 변경 없는 base branch의 equal·behind-only fast-forward, tracked 수정, staged, non-ignored untracked, ahead·diverged, detached·다른 checkout branch, 진행 중인 Git operation, 동시 사용자 변경과 Git lock 충돌을 자동 테스트로 검증한다.
- `config/config.ini`, `.venv`와 cache 같은 ignored 파일이 겹치지 않으면 fast-forward가 가능하고, 원격 변경 경로와 같거나 부모·자식으로 겹치면 파일을 보존한 채 skip하는지 검증한다.
- 각 테스트는 eligible checkout만 원격 base로 정확히 전진하고 그 밖의 기본 worktree 상태는 보존되며, 어느 경우에도 관리 worktree가 원격 기준만 포함하는지 확인한다.

## Product Impact

- 사용자는 기본 worktree에서 문서를 작성하는 동안에도 Loop Manager의 원격 기준 자동 작업을 계속 실행할 수 있다.
- 로컬에서 진행 중인 변경과 개별 ExecPlan 구현은 서로의 입력이나 작업 공간에 포함되지 않는다.
- Loop Manager 실행과 Loop Manager 자체 코드 업데이트가 분리된다.
- 실행 코드는 push된 `origin/main`의 고정 SHA를 따르고 사용자 `main`은 독립적으로 편집할 수 있다.
- 등록 프로젝트의 clean한 기본 checkout은 원격 base를 따라 자동 fast-forward되며, 사용 중이거나 별도 이력이 있는 checkout은 그대로 보존된다.
- 기존 `config/config.ini`와 source checkout의 Python 환경은 runtime revision과 분리된 운영 입력으로 계속 사용할 수 있다.
- pinned runtime 전환 전의 source updater는 한시적으로 유지되고, 전환 뒤에는 runtime 자식의 등록 프로젝트 동기화가 동일한 목적을 일반화한다.

## Non-Goals

- 로컬 미commit 변경이나 로컬 전용 commit을 Planner 또는 Worker 입력에 포함하지 않는다.
- safe fast-forward 조건 밖에서 대상 프로젝트의 기본 worktree를 자동 commit, push, pull, rebase, merge, checkout, stash, reset 또는 clean하지 않는다.
- 대상 프로젝트 기본 worktree의 ahead·diverged 이력, 로컬 변경, 다른 checkout branch 또는 진행 중인 Git operation을 자동으로 해소하지 않는다.
- 임시 updater는 Loop Manager source checkout을 force, rebase, merge commit, stash, reset 또는 clean하지 않고 fast-forward 이외의 차이를 해소하지 않는다.
- runtime을 위해 별도 Git clone이나 독립 object database를 만들지 않는다.
- `config/config.ini`, `.venv` 또는 다른 ignored 파일을 runtime worktree마다 복제하거나 runtime의 clean 검증 예외로 허용하지 않는다.
- 실행 중인 runtime worktree를 새 `origin/main`으로 전환하지 않는다.
- 다른 프로세스가 보유한 Git lock을 제거하거나 우회하지 않는다.
- 관리 worktree 밖을 수정할 수 있는 Agent 권한을 허용하지 않는다.

## Open Questions

- 없음
