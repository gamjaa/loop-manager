# [R-023] Self-Hosted Runtime Refresh Handoff

## Context

Loop Manager는 현재 source checkout과 분리된 exact `origin/main` SHA의 detached runtime worktree에서 실행된다. 등록된 self 프로젝트의 base branch는 현재 `main`이지만 앞으로 `develop` 등 다른 branch가 될 수 있으므로, 이 변경의 PR 동기화·source checkout 갱신·runtime handoff는 `main`을 상수로 사용하지 않고 등록 상태에서 해석한 하나의 `<base-branch>`와 `origin/<base-branch>`를 끝까지 사용해야 한다.

현재 bootstrap은 source checkout의 프로그램 revision으로 시작한 뒤 최신 원격 SHA를 fetch해 그 SHA의 runtime 자식을 바로 실행한다. 이 방식은 runtime은 최신이어도 runtime을 선택하고 process lock과 인자를 소유한 bootstrap은 이전 revision일 수 있다. self 저장소의 새 프로그램이 이미 Merge된 다음 실행이라면 PR 상태를 확인하기 전에 bootstrap 자체를 먼저 최신 revision으로 넘겨야 한다.

현재 프로젝트 실행 경로는 저장된 project mirror가 `PR_OPEN`이거나 충돌 복구 marker를 가지고 있으면 GitHub의 실제 PR 상태를 확인하기 전에 기본 worktree 동기화를 건너뛴다. 이 때문에 이미 Merge된 PR도 한 주기 동안 `PR 충돌 감지/복구 중`으로 기록되고, Merge commit을 포함한 원격 base로 source checkout을 갱신할 기회를 놓친 뒤에야 SQLite를 `MERGED`로 전환한다. 여러 `PR_OPEN`을 보존하는 실행 모델에서는 하나의 PR이 Merge됐어도 다른 대기 상태나 낡은 project mirror가 같은 false skip을 만들 수 있다.

Loop Manager 저장소 자체를 등록 프로젝트로 운영할 때는 영향이 더 크다. 현재 실행이 자기 프로젝트의 PR Merge를 새로 확인하면 해당 Merge commit에는 다음 실행부터 사용해야 할 Loop Manager 코드가 포함될 수 있다. Merge 확인 뒤에도 같은 runtime에서 후속 Worker나 다른 프로젝트 작업을 시작하면, 새 원격 revision을 가져왔는데도 Merge 전 프로그램 revision으로 orchestration을 계속할 수 있다.

현재 Agent Git 범위 감시는 관리 worktree뿐 아니라 같은 repository의 worktree와 shared Git metadata까지 하나의 repository-wide 불변 조건으로 비교한다. 그 결과 bootstrap/source checkout의 사용자 변경이나 현재 계획과 무관한 branch·worktree 변경도 Agent 실행 중 우연히 발생하면 변경 주체와 관계없이 `worker Git 범위 위반`과 `PROJECT_BLOCKED`로 기록될 수 있다. Loop Manager가 저장된 Agent 시작 snapshot 뒤에 관리 rebase를 수행한 경우에도 그 HEAD·branch ref·worktree index 변화를 Agent 결과로 오인할 수 있다. 과거 snapshot의 정확한 복원을 요구하면 정상적인 fetch, runtime worktree 생성·정리와 source 갱신까지 차이로 누적되어 차단이 반복된다.

## Requirement

bootstrap은 process lock을 획득한 뒤 runtime worktree를 선택·생성하거나 runtime 자식 및 프로젝트 PR 동기화를 시작하기 전에 self `<base-branch>`를 해석하고 `origin/<base-branch>`를 fetch해야 한다. source checkout의 commit이 fetch된 원격 base와 같으면 staged·unstaged·untracked·ignored 변경의 유무와 관계없이 source는 이미 최신이므로 해당 SHA의 runtime을 선택해 자식을 시작한다. source commit이 원격 base의 조상이라면 dirty non-overlap 허용 규칙으로 source checkout을 safe fast-forward하고, 성공 사실을 기록한 뒤 runtime 자식을 시작하지 않은 채 정상 종료한다. 다음 외부 스케줄 실행은 갱신된 source bootstrap으로 다시 시작한다.

source bootstrap이 원격 base보다 뒤처졌지만 overlap dirty, ahead·diverged, 다른 branch, Git operation, 동시 변경 또는 Git 거부 때문에 safe fast-forward할 수 없으면 구 bootstrap으로 최신 runtime을 실행하지 않는다. reset, stash, autostash, rebase 또는 강제 checkout 없이 source를 보존하고 명확한 진단과 0이 아닌 종료 상태를 남긴다. runtime 자식이 시작될 때는 source bootstrap commit, runtime SHA와 fetch된 `origin/<base-branch>` SHA가 모두 같아야 한다.

Loop Manager는 각 프로젝트의 기본 worktree 동기화 여부를 결정하기 전에 저장된 모든 Merge 대기 PR의 실제 GitHub state, Merge 여부와 identity를 동기화해야 한다. Merge 또는 Close로 종료된 PR의 상태 전이, worktree cleanup 판정과 알림 outbox 기록을 먼저 영속화하고, 갱신된 ExecPlan 및 project 상태를 다시 읽은 뒤 기본 worktree의 safe fast-forward를 평가해야 한다.

같은 실행에서 하나 이상의 PR Merge를 새로 확인했다면 낡은 `PR_OPEN` project mirror, 이미 종료된 PR 또는 별도의 안정된 `PR_OPEN`이 존재한다는 이유만으로 기본 worktree 동기화를 생략하지 않는다. fetch된 `origin/<base-branch>`를 기준으로 safe fast-forward eligibility를 반드시 평가한다. 기본 worktree가 dirty라는 사실만으로도 동기화를 생략하지 않는다. 현재 checkout이 해석한 base branch이고 local `HEAD`가 원격 base의 조상이며 진행 중인 Git operation이 없고, staged·unstaged·untracked·ignored를 포함한 모든 로컬 변경 경로가 fast-forward로 바뀌는 tracked 경로와 부모·자식 관계까지 겹치지 않으면 로컬 변경을 유지한 채 `git merge --ff-only origin/<base-branch>`와 동등한 갱신을 허용한다.

로컬 변경 경로와 원격 변경 경로가 겹치거나 eligibility 확인 뒤 checkout이 바뀌거나 Git이 fast-forward를 거부하면 reset, clean, stash, autostash, 강제 checkout 또는 source checkout rebase 없이 기존 checkout을 보존하고 실제 이유를 기록한다. local base가 ahead·diverged인 경우도 사용자 commit을 자동 rebase하지 않는다. 최신 bootstrap으로 runtime 자식이 이미 시작된 뒤에는 Planner·Worker와 PR 충돌 복구가 source checkout의 파일·index·dirty 상태가 아니라 clean한 `origin/<base-branch>` 기반의 별도 관리 worktree를 사용하므로, 이후 source checkout sync를 건너뛰었다는 이유만으로 관리 worktree의 생성·재개·rebase·검증을 막지 않는다. 시작 시 source bootstrap 자체가 원격보다 뒤처진 경우에는 위 freshness gate가 우선한다.

bootstrap/source checkout의 HEAD·index·tracked·untracked·ignored 파일 상태는 Planner·Worker·Reviewer가 소유하는 관리 worktree 변경 범위가 아니다. source가 dirty하다는 사실이나 Agent 실행 중 그 local 상태가 바뀌었다는 사실만으로 Agent Git 범위 위반 또는 `PROJECT_BLOCKED`를 만들지 않으며, source의 local 상태는 bootstrap freshness gate와 기본 worktree safe fast-forward 판단에서만 평가한다. self bootstrap이 원격보다 뒤처진 경우에는 실제 safe fast-forward가 로컬 상태를 보존하며 성공할 수 있는지가 실행 허용 조건이고, 이미 같은 원격 revision이면 pull이나 파일 상태 정리를 요구하지 않는다.

모든 등록 프로젝트의 Agent Git 범위 판정은 현재 단계의 안전에 필요한 최소 상태만 대상으로 한다. 선택된 관리 worktree의 identity·등록·branch·HEAD·index·허용 파일 변경과 현재 작업이 실제로 사용하는 소유 branch, 원격 base ref, 게시 원격 ref·PR head, 해당 Git 작업에 영향을 주는 config·operation을 검증한다. 사용하지 않는 다른 branch·tag·ref와 다른 worktree의 독립적인 등록·HEAD·index·파일 변화는 Agent 변경으로 귀속하거나 `PLAN_BLOCKED`·`PROJECT_BLOCKED`의 단독 원인으로 삼지 않는다. 해당 자원을 나중에 사용하게 되면 사용 직전에 실제 값과 소유권을 확인한다. 검증된 원격 base 전진은 기존 fetch·rebase·재검증 흐름으로 처리한다. 그 밖에 현재 작업에 영향을 미치는 예상 밖 변화 또는 영향 여부를 증명할 수 없는 변화는 계속 안전 차단한다.

사용자는 Agent 실행 중에도 문서나 코드를 `<base-branch>`에 직접 commit·push할 수 있다. 원격 base의 편집·게시 주체가 Loop Manager뿐이라는 전제를 두지 않는다. 이 원격 전진이 현재 원격과 Git 상태로 확인되고 Agent의 공유 Git metadata 쓰기와 구분되면, 진행 중인 계획의 기존 branch·worktree·session을 보존한 채 Manager가 최신 base를 fetch하고 필요한 관리 rebase, 후보 검증과 Review를 다시 수행한다. 사용자 push를 관측했다는 사실만으로 Worker의 공유 ref 쓰기나 원격 lease·소유권 불일치를 허용하지 않는다.

Agent가 선택된 관리 worktree 밖의 파일과 shared Git metadata를 쓸 권한은 없다. 무관한 변화의 범위 검사 제외는 이 쓰기 격리가 실제 실행에서 유효함을 검증할 수 있을 때만 적용한다. 격리를 증명할 수 없는 실행이나 과거 기록에서는 무관한 shared ref·다른 worktree 변화를 단순 관측만으로 외부 변경이라 추정해 허용하지 않는다. 감시 축소는 Agent의 Git 쓰기 권한, worktree·branch·push·PR 소유권을 넓히지 않는다.

Planner·Worker·Reviewer와 유지보수 Planner는 각 호출에서 선택된 관리 worktree를 workspace로 사용하며, provider의 내장 쓰기 제한을 우선 적용한다. 작업 디렉터리 지정만으로 제한이 적용됐다고 간주하지 않는다. 기본 source checkout, 다른 worktree와 shared Git metadata는 쓰기 허용 범위에 포함하지 않고, Reviewer는 선택된 worktree도 읽기 전용으로 사용한다. workspace 쓰기 허용은 해당 역할의 문서·파일 변경 범위를 확대하지 않으며, 기존 역할별 변경 검사는 유지한다. 필요한 임시 작업은 해당 실행에 소유권이 있는 제한된 경로에서만 허용한다.

Codex와 Claude의 자동 승인 검토는 쓰기 제한과 함께 유지한다. 쓰기 경계 확대와 격리 밖 재실행 요청은 자동 검토의 승인으로 허용할 수 없는 거부 대상으로 두고, 나머지 검토 대상은 provider의 자동 검토로 처리한다. 허용 범위 안의 모든 동작에 검토가 발생한다고 보장하지 않으며, 자동 검토가 비활성화된 설정이나 단순 편집 자동 승인 모드를 이 요구사항의 자동 검토 유지로 간주하지 않는다. Worker 결과에 대한 Loop Manager의 Reviewer 검토는 provider의 실행 전 승인 검토와 별개로 유지한다.

새 호출에서 요구한 쓰기 제한과 자동 검토의 적용을 확인할 수 없거나, 격리 때문에 기존 허용 작업을 수행할 수 없으면 제한을 해제하거나 자동 검토를 끈 채 진행하지 않는다. 지원하지 않는 provider·운영체제·CLI 버전, 설정 충돌과 격리 초기화 실패의 구체적 원인을 남기고 작업물·session·재시도 이력을 보존한 채 해당 자동 실행을 중단한다. 내장 sandbox를 지원하지 않는 Claude native Windows는 이러한 미지원 환경으로 명확히 판정하며, 이 요구사항을 근거로 WSL·컨테이너 등 새로운 실행 infrastructure를 자동 도입하지 않는다. 격리 증거가 없는 과거 실행의 Git 판정은 기존 엄격 감시를 유지한다.

격리 적용·거부와 실제 효력 확인 결과는 provider·CLI 버전·실행 환경·역할·선택된 canonical worktree·repository identity·유효 설정·Agent session 및 개별 호출에 결합해 보존한다. CLI 인자나 설정의 존재만으로 격리 확인을 기록하지 않으며, provider 내부 쓰기를 포함한 경계를 증명할 수 없으면 확인되지 않은 것으로 남긴다. 세션 재개, provider·설정 변경과 중단 뒤의 새 호출에서는 다시 확인한다. 비밀값이나 source checkout의 로컬 파일 내용은 증거에 저장하지 않는다.

Loop Manager 자신이 process lock 아래 수행한 fetch, 관리 worktree 생성·정리·rebase, source safe fast-forward와 runtime worktree 생성·정리는 각 작업의 소유 대상, 실행 전 기대 상태와 실행 뒤 실제 결과를 연결한 durable checkpoint로 증명한다. Agent 시작 snapshot 이후 Manager 작업이 있었다면, 해당 checkpoint와 실제 Git 상태가 정확히 일치하는 부분만 Agent 범위 판정 전에 기준선에 반영한다. Agent의 원래 허용 파일 변경과 session·실행 증거는 보존하며, 불완전한 checkpoint나 관측 OID만으로 Manager 소유권을 추정하지 않는다. Git 작업과 상태 저장 사이에 중단돼도 같은 증거로 재개하고, 다른 주체의 변화가 섞였으면 영향을 받는 작업을 차단한다.

저장된 `PLAN_BLOCKED`·`PROJECT_BLOCKED`의 원인이 격리가 증명된 실행의 무관한 변화 또는 증명된 Manager 작업의 낡은 기준선뿐이라면, 선택된 관리 worktree의 identity·소유권·허용 변경과 사용 중인 ref·Git 상태를 다시 확인한 뒤 해당 차단을 해제하고 안전한 현재 기준으로 재조정한다. 기존 Agent session·작업물과 다른 계획의 차단 원인은 보존하고, 후보 revision이 달라졌다면 필요한 targeted test·전체 verify·Review를 다시 수행한다. 증거가 부족하거나 현재 작업에 영향을 주는 변화가 남으면 차단을 유지한다. source가 원격보다 뒤처졌는지와 dirty 때문에 fast-forward가 불가능한지는 차단 해제 뒤 bootstrap freshness 또는 일반 source sync 단계에서 독립적으로 판정한다.

runtime 자식은 bootstrap이 전달하고 검증한 canonical source checkout, 현재 runtime SHA, repository identity와 origin을 이용해 등록 프로젝트가 실행 중인 Loop Manager 자신인지 결정해야 한다. 자기 자신으로 인정하려면 등록된 canonical repository path가 runtime source와 같고 실제 Git repository 및 origin identity도 일치해야 한다. 디렉터리 이름, 프로젝트 표시명, 원격 저장소 이름 또는 경로 prefix만으로 자기 자신으로 판단하지 않는다.

self 프로젝트에 저장된 `<base-branch>`는 PR의 base, Merge 뒤 fetch·source checkout 갱신과 다음 runtime 선택에 동일하게 사용한다. 현재 기본값이 `main`이라는 이유로 handoff code나 로그에 `origin/main`을 고정하지 않으며, self 등록이 없는 bootstrap의 기존 기본 branch 선택은 이 Requirement에서 변경하지 않는다.

현재 실행이 자기 프로젝트의 PR Merge를 새로 확인하면 Merge 결과를 포함한 `origin/<base-branch>`를 fetch하고, PR·ExecPlan 상태 전이, worktree cleanup 결과와 알림 outbox를 먼저 영속화한다. 이어서 dirty 허용 조건을 포함한 source checkout safe fast-forward를 평가하고 그 결과를 기록한 뒤, fetch된 원격 base SHA와 현재 runtime SHA를 비교한다. 원격 base가 현재 runtime과 같으면 이미 최신 프로그램으로 실행 중이므로 종료하지 않고 정상 처리를 계속한다. 원격 base가 현재 runtime의 fast-forward 후속 commit이면 현재 runtime에서 새 Planner, Worker, Reviewer, 전체 verify, publication 또는 다른 프로젝트의 상태 변경 작업을 시작하지 않고 성공 코드로 종료한다. 다음 외부 스케줄 실행은 같은 `<base-branch>`를 기준으로 다시 fetch한 최신 원격 SHA의 clean detached runtime을 선택하고 저장된 상태부터 작업을 재개한다.

runtime handoff 여부는 source checkout의 fast-forward 성공에는 의존하지 않지만, 현재 runtime SHA와 fetch된 `origin/<base-branch>` SHA의 관계에는 의존한다. source checkout 갱신이 overlap, ahead·diverged 또는 다른 안전 사유로 skipped·failed여도 원격 base가 현재 runtime보다 앞서면 정상 종료한다. bootstrap이 같은 Merge commit을 이미 runtime으로 선택한 상태에서 뒤늦게 SQLite의 Merge를 확인한 경우에는 상태와 source sync 결과만 반영하고 같은 최신 runtime에서 계속한다. 두 SHA의 fast-forward 관계를 증명할 수 없으면 자동 handoff로 취급하지 않고 기존 repository safety 정책에 따라 상태를 보존한다.

self runtime handoff 판정은 같은 실행에서 병렬 프로젝트 작업을 새로 배정하기 전의 안전한 scheduling boundary에서 이루어져야 한다. handoff가 필요하다는 사실을 확인한 뒤 새 Agent subprocess나 검증 작업을 시작하지 않으며, 이미 영속화된 세션·retry·workspace 상태를 지우지 않는다. Merge 뒤 최신 원격 fetch가 실패하거나 repository identity를 검증할 수 없으면 최신 version을 확보했다고 간주하거나 성공 종료하지 않고, 기존 일시 오류 또는 repository safety 정책에 따라 상태를 보존한 채 다음 실행에서 다시 확인한다.

일반적인 self `origin/<base-branch>` 변경만으로는 이 handoff를 시작하지 않는다. Planner가 ExecPlan 문서 commit을 직접 게시했거나 외부 push를 관측했지만 현재 실행에서 self PR의 새 Merge 완료를 확인하지 않았다면 기존 실행 흐름을 유지한다. 다른 등록 프로젝트의 PR Merge도 그 프로젝트의 상태와 기본 worktree만 갱신하며 Loop Manager 프로세스 전체를 종료하지 않는다.

## Acceptance Criteria

- bootstrap은 process lock을 획득한 뒤 runtime worktree 선택·생성, runtime 자식 시작과 프로젝트 PR 조회보다 먼저 self `<base-branch>`를 해석하고 `origin/<base-branch>`를 fetch한다.
- source bootstrap commit과 fetch된 원격 base SHA가 같을 때만 같은 SHA의 clean detached runtime 자식을 시작하며, 자식 시작 시 세 revision이 정확히 일치한다.
- source bootstrap commit이 fetch된 원격 base SHA와 같으면 source checkout에 staged·unstaged·untracked 또는 ignored 변경이 있어도 정리나 pull을 요구하지 않고 같은 SHA의 runtime 자식을 시작한다.
- source bootstrap commit이 원격 base의 조상이고 safe fast-forward가 가능하면 non-overlap dirty 변경을 보존해 source를 갱신하고 성공 종료하며, runtime worktree나 자식을 만들거나 PR Merge 상태를 조회하지 않는다.
- source bootstrap이 뒤처졌지만 overlap dirty, ahead·diverged, 다른 branch, Git operation, 동시 변경 또는 Git 거부로 갱신할 수 없으면 사용자 상태를 보존하고 0이 아닌 코드로 종료하며 최신 runtime 자식을 시작하지 않는다.
- source를 갱신해 종료한 다음 외부 실행은 갱신된 bootstrap으로 같은 원격 SHA의 runtime을 시작한 뒤에만 저장된 PR Merge 상태를 동기화한다.
- 저장 상태가 `PR_OPEN`인 PR이 GitHub에서 이미 Merge된 실행에서 PR 상태 동기화와 `MERGED` 전이, cleanup 판정 및 알림 outbox 기록이 기본 worktree skip 판정보다 먼저 완료된다.
- 새 Merge가 확인된 실행은 남아 있는 다른 안정된 `PR_OPEN`이나 낡은 project mirror 때문에 `PR 충돌 감지/복구 중 source checkout 보존`으로 분류되지 않고, fetch된 `origin/<base-branch>`에 대해 safe fast-forward eligibility를 평가한다.
- Merge 뒤 기본 worktree가 clean한 저장 base branch의 behind-only 상태이면 같은 실행에서 정확한 `origin/<base-branch>`로 fast-forward된다.
- 기본 worktree에 staged·unstaged·untracked 또는 ignored 변경이 있어도 모든 로컬 변경 경로가 원격 fast-forward 변경 경로와 부모·자식 관계까지 겹치지 않으면 local delta와 파일 내용을 보존한 채 fast-forward한다.
- 로컬과 원격 변경 경로가 겹치거나 다른 branch·detached·ahead·diverged·Git operation·동시 변경 또는 Git 거부가 있으면 reset·stash·autostash·source rebase 없이 보존하고 실제 skip 이유를 기록한다.
- source bootstrap과 원격 base가 같은 revision으로 runtime 자식이 시작된 뒤의 source checkout dirty 또는 sync skip은 clean한 `origin/<base-branch>`에서 수행하는 Planner·Worker workspace 생성, 기존 workspace 재개, PR conflict rebase와 검증을 막지 않는다.
- Planner·Worker·Reviewer 실행 전후 bootstrap/default worktree의 HEAD·index·파일·dirty 상태만 달라진 경우 이를 해당 Agent의 변경으로 귀속하거나 `PROJECT_BLOCKED`를 만들지 않는다.
- 선택된 관리 worktree와 현재 단계가 실제로 사용하는 ref·worktree·Git config·operation만 Agent 범위와 Git 작업 직전 안전 검사의 대상으로 삼는다. 실행과 무관한 `draft` 등 다른 local branch·tag·ref 또는 다른 worktree의 독립적인 변동만으로 Agent 범위 위반이나 프로젝트 차단을 만들지 않는다. 사용하게 되는 자원은 사용 직전에 다시 검증한다.
- 사용자가 Worker 실행 중 `<base-branch>`에 문서 또는 코드 commit을 직접 push하고 원격 base가 전진해도, 확인된 외부 변경을 Worker 범위 위반으로 차단하지 않는다. Manager는 기존 작업물과 session을 보존해 최신 base에 rebase하고 변경된 후보의 필요한 Worker 확인·targeted test·전체 verify·Review를 거친다. 같은 시점의 실제 Agent 공유 ref 변경이나 증명되지 않은 Git 변화는 이 예외로 해제하지 않는다.
- 선택된 관리 worktree의 범위 밖 파일·index·branch·HEAD 변화, 소유 branch·게시 ref·PR head의 예상 밖 변화, 검증된 fetch·rebase로 처리할 수 없는 원격 base 변화, 사용 중인 worktree 등록·Git operation·config 불일치 또는 영향 여부를 증명할 수 없는 변화는 기존 안전 차단을 유지한다. Agent의 다른 worktree·shared Git metadata 쓰기 격리가 검증되지 않으면 무관한 변화에 최소 범위 예외를 적용하지 않는다.
- Agent 시작 뒤 Manager가 수행한 fetch·worktree 생성/정리·rebase·source 갱신은 실행 전후 상태와 durable checkpoint가 일치할 때만 기준선에 반영한다. Manager rebase로 HEAD·소유 branch ref·선택된 worktree index가 바뀐 뒤 재시작해도 낡은 Agent 시작 snapshot과의 차이만으로 차단하지 않으며, Agent의 허용 파일 변경과 session을 보존한다. Manager 변화와 예상 밖 영향권 변화가 섞였거나 checkpoint가 불완전하면 해당 영향을 안전 차단한다.
- 기존 `default_worktree_mutation` 및 같은 유형의 저장된 `PLAN_BLOCKED`·`PROJECT_BLOCKED`는 격리 증거가 필요한 경우 이를 확인하고 과거 snapshot의 정확한 복원 대신 현재 작업에 필요한 소유권, 허용 변경, Manager checkpoint와 실제 Git 상태를 검증해 다른 위반이 없으면 자동 해제한다. 다른 계획의 차단과 저장된 작업물·session은 보존하고, 후보 revision 변경에 따른 검증과 Review는 다시 수행한다.
- `MERGEABLE` 또는 `UNKNOWN`인 Open PR만 있고 새 Merge 완료가 없으면 기존 PR 대기 및 충돌 복구 권한을 변경하지 않는다. `CONFLICTING` 판정도 기존 소유권, worktree와 explicit lease 조건을 유지한다.
- runtime source와 같은 canonical repository path이며 실제 repository 및 origin identity가 일치하는 등록 프로젝트만 self로 판정한다. 같은 이름, 같은 origin만 가진 다른 clone, source 하위 경로, symlink alias 또는 state directory 아래 runtime 경로를 self source로 오인하지 않는다.
- 현재 실행이 self 프로젝트의 새 PR Merge 완료를 확인하면 Merge 결과를 포함한 최신 `origin/<base-branch>` fetch, 관련 SQLite와 outbox 영속화 및 source checkout 동기화 시도를 완료하고 fetch된 SHA를 현재 runtime SHA와 비교한다.
- source checkout fast-forward가 로컬 안전 조건 때문에 skipped 또는 failed여도 self Merge 뒤 원격 base가 현재 runtime SHA의 fast-forward 후속이면 handoff는 정상 종료한다. 다음 bootstrap은 source local `<base-branch>`가 아니라 fetch된 `origin/<base-branch>`를 다시 조회해 runtime을 선택한다.
- 현재 runtime HEAD가 이미 fetch된 self `origin/<base-branch>`와 같으면 Merge 완료와 source sync 결과를 반영한 뒤 종료하지 않고 정상 처리를 계속한다.
- fetch된 self `origin/<base-branch>`와 현재 runtime SHA의 fast-forward 관계를 증명할 수 없거나 최신 원격 fetch·repository identity 검증이 실패하면 성공 handoff나 강제 동기화를 수행하지 않고 기존 일시 오류 또는 안전 오류와 재개 가능한 상태를 유지한다.
- self Planner의 직접 planning commit이나 일반 외부 push만 관측했고 self PR Merge 완료를 새로 확인하지 않은 경우에는 runtime handoff를 시작하지 않는다.
- 다른 등록 프로젝트의 PR Merge는 해당 프로젝트 상태와 기본 worktree를 정상 갱신하지만 Loop Manager 프로세스 전체의 runtime handoff를 만들지 않는다.
- runtime handoff는 해석한 `<base-branch>`, 현재 runtime SHA, fetch된 원격 SHA, self repository, Merge된 PR과 source sync 결과를 비밀값 없이 로그에 남기며 오류나 BLOCKED 알림으로 취급하지 않는다.
- 자동 테스트는 `main`과 대체 base branch, 시작 시 equal bootstrap/runtime의 clean·dirty 진행, 시작 시 behind source의 clean·non-overlap dirty 갱신 후 runtime·PR 미시작, overlap dirty·ahead·diverged에서 구 bootstrap의 최신 runtime 실행 거부, 다음 실행의 최신 bootstrap/runtime 일치, Merge 직전 `PR_OPEN` mirror, 다중 Open PR 중 하나의 Merge, Agent 실행 중 bootstrap dirty 변화의 비귀속과 stale `default_worktree_mutation` 자동 해제, source dirty 상태의 managed rebase 지속, 실행 중 runtime보다 앞선 self Merge의 종료, 이미 최신 runtime에서 새로 확인한 Merge의 계속 실행, self Planner push 비트리거, 다른 clone·origin·runtime path 오인 방지, fetch 실패·non-fast-forward SHA 관계와 다중 프로젝트 scheduling 중 새 작업 미시작을 검증한다.
- Agent 범위 테스트는 다른 local branch·tag·ref와 다른 worktree가 독립적으로 변경된 경우의 비차단, 실제 작업에 쓰는 ref·worktree 및 범위 밖 파일·index 변경의 차단, 격리 검증 실패 시 완화 거부, Manager rebase·fetch·worktree 작업 전후의 기준선 재조정, Git 작업과 checkpoint 저장 사이의 중단·재시작, Manager 변화와 영향권 외부 변화의 혼합, 기존 차단의 증거 기반 복구와 session·작업물 보존을 검증한다.
- 각 역할의 신규 호출과 session 재개에서 workspace 및 허용 파일 쓰기와 source checkout·다른 worktree·shared Git metadata 쓰기 거부를 검증한다. Reviewer의 worktree 쓰기도 거부하며, provider 내부 쓰기를 분리해 증명할 수 없는 경우에는 격리 확인을 기록하지 않는다.
- Codex·Claude의 지원 환경에서 쓰기 경계 확대와 격리 밖 재실행은 거부하면서 나머지 승인 검토 대상의 자동 검토가 유지됨을 검증한다. 단순 편집 자동 승인으로 대체하거나 자동 검토를 전부 비활성화한 설정은 실패로 판정한다. 기존 Loop Manager Reviewer 흐름도 유지한다.
- CLI 버전·provider·설정 변경, session 재개와 중단 뒤의 증거 재확인, 설정 충돌·격리 초기화 실패·Claude native Windows 미지원 시 안전 중단과 상태 보존, 증거 없는 과거 실행의 엄격 판정을 검증한다. 확인·거부 기록에는 비밀값과 source checkout의 로컬 파일 내용을 포함하지 않는다.
- 전체 verify에서 exact-SHA runtime bootstrap, process lock, Manager runtime scope reconciliation, PR conflict recovery, source checkout 보존과 origin-only Planner·Worker 입력 규칙이 유지된다.

## Product Impact

- PR의 실제 완료 상태가 source checkout 동기화 판단보다 먼저 반영되며, 종료된 PR의 낡은 상태 때문에 기본 worktree 갱신이 불필요하게 지연되지 않는다.
- source bootstrap이 원격 base보다 뒤처졌으면 source를 안전하게 갱신하고 현재 실행을 끝내며, 최신 runtime은 다음 외부 실행에서 같은 revision의 최신 bootstrap만 시작한다.
- Loop Manager가 자기 저장소의 PR Merge를 새로 확인하면 해석한 `origin/<base-branch>`를 가져오고, 현재 runtime보다 새로운 경우에만 현재 실행을 끝내 다음 외부 실행에 제어를 넘긴다.
- non-overlap dirty source checkout은 로컬 변경을 유지한 채 원격 base로 전진할 수 있다.
- 최신 bootstrap으로 runtime이 이미 시작된 뒤 source checkout을 갱신할 수 없는 로컬 상태는 관리 worktree 작업을 막지 않는다. 시작 시 bootstrap이 원격보다 뒤처졌다면 갱신되기 전까지 runtime 자식을 시작하지 않는다.
- bootstrap이 이미 원격 base revision이면 dirty 상태를 정리하지 않고 실행하며, Agent 실행 중 bootstrap 파일 상태가 바뀌어도 관리 worktree 위반으로 오인하거나 과거 signature에 프로젝트를 영구 차단하지 않는다.
- Agent 범위 검사는 현재 계획의 안전에 필요한 Git 상태로 제한된다. 무관한 branch·worktree 변화는 실행을 막지 않고, Manager 소유 Git 작업은 검증된 checkpoint로 재개되며, 실제 영향권 위반은 계속 차단된다.
- provider의 내장 workspace 쓰기 제한과 자동 승인 검토를 함께 사용한다. 격리 밖 쓰기 권한을 자동 승인하지 않으며, 지원하지 않거나 확인할 수 없는 환경에서는 상태를 보존하고 자동 실행을 멈춘다.

## Non-Goals

- 현재 프로세스 안에서 runtime module을 hot reload하거나 새 runtime 자식을 재귀적으로 시작하지 않는다.
- overlap dirty, ahead, diverged, 다른 branch 또는 소유권 불명의 source checkout을 강제로 최신화하지 않는다.
- source checkout의 사용자 commit이나 dirty 변경을 자동 rebase, stash 또는 autostash하지 않는다.
- 저장소 이름이나 GitHub URL만으로 임의의 clone을 현재 bootstrap source와 동일한 checkout으로 신뢰하지 않는다.
- 기존 PR 충돌 복구의 rebase, force-with-lease, branch 소유권 또는 Agent 권한을 확대하지 않는다.
- runtime refresh를 이유로 영속화되지 않은 작업을 폐기하거나 관리 worktree, Agent session, retry history를 정리하지 않는다.
- self Planner의 planning commit, 일반 외부 push 또는 다른 프로젝트 PR Merge를 프로세스 종료 신호로 확대하지 않는다.
- 무관한 Git 변화의 비차단을 이유로 Agent의 다른 worktree·shared Git metadata 쓰기, 현재 작업의 소유 branch·base·게시 ref 변경 수용 또는 검증되지 않은 checkpoint 신뢰를 허용하지 않는다.

## Open Questions

- 없음
