# [R-023] Self-Hosted Runtime Refresh Handoff

## Summary

로컬 source 상태와 분리해 검증한 origin/<base-branch>의 bootstrap·runtime을 실행하고, 실제 PR 상태를 먼저 반영한 뒤 로컬 동기화를 평가한다. 실제 단계 진행 경계에서 최초 즉시, 이후 성공 관측으로부터 최소 30초 간격으로 최신 원격의 코드·설정·실행 지침 입력을 현재 runtime과 비교한다. 대기 중에는 조회하지 않고, 실행 입력 변경이 확인되면 종료 결정을 메모리에 유지해 추가 비교 없이 새 단계 진입을 막고 진행 중 호출의 안전한 반환·상태 보존 뒤 자동으로 갱신·재시작한다. runtime 갱신은 PR 조회·Merge 판정과 독립적으로 실행 입력 차이만으로 결정한다. Agent 접근은 provider 내장 쓰기 제한·자동 승인 검토로 구성한다. Claude는 일반적인 호출을 공식 문서·모의 테스트로 개발하며 실환경 완전 지원과 실행 중 managed 정책 변경 대응은 완료 조건에서 제외한다. bootstrap의 미완료 SQLite journal은 한정된 정상 롤백으로 복구하며 정상 Git 변화의 오탐 복구와 호출별 실제 실패 처리는 유지한다. runtime 교체의 전체 배정 중단과 안전한 호출 반환은 R-091을 따른다.

## Context

Loop Manager는 현재 source checkout과 분리된 등록 origin/<base-branch>의 exact SHA의 detached runtime worktree에서 실행된다. 등록된 self 프로젝트의 base branch는 현재 `main`이지만 앞으로 `develop` 등 다른 branch가 될 수 있으므로, 이 변경의 PR 동기화·source checkout 갱신·runtime handoff는 `main`을 상수로 사용하지 않고 등록 상태에서 해석한 하나의 `<base-branch>`와 `origin/<base-branch>`를 끝까지 사용해야 한다.

현재 bootstrap은 검증한 원격 bootstrap·runtime을 같은 실행에서 시작하며 로컬 source 동기화는 별도 부수 작업이다. 로컬 동기화는 R-056, 정상 Git 변경·현재 입력·게시 분리는 R-055, 역할별 문서 권한은 R-061을 따른다.

현재 프로젝트 실행 경로는 저장된 project mirror가 `PR_OPEN`이거나 충돌 복구 marker를 가지고 있으면 GitHub의 실제 PR 상태를 확인하기 전에 기본 worktree 동기화를 건너뛴다. 이 때문에 이미 Merge된 PR도 한 주기 동안 `PR 충돌 감지/복구 중`으로 기록되고, Merge commit을 포함한 원격 base로 source checkout을 갱신할 기회를 놓친 뒤에야 SQLite를 `MERGED`로 전환한다. 여러 `PR_OPEN`을 보존하는 실행 모델에서는 하나의 PR이 Merge됐어도 다른 대기 상태나 낡은 project mirror가 같은 false skip을 만들 수 있다.

Loop Manager 저장소 자체의 원격 코드가 바뀌어도 PR Merge 관측에 의존하면 이전 runtime에서 후속 작업을 계속할 수 있다. runtime 갱신은 코드 차이로 판단하고, 일반 작업 완료를 위한 PR 상태 반영과 분리해야 한다.

현재 Agent Git 범위 감시는 관리 worktree뿐 아니라 같은 repository의 worktree와 shared Git metadata까지 하나의 repository-wide 불변 조건으로 비교한다. 그 결과 bootstrap/source checkout의 사용자 변경이나 현재 계획과 무관한 branch·worktree 변경도 Agent 실행 중 우연히 발생하면 변경 주체와 관계없이 `worker Git 범위 위반`과 `PROJECT_BLOCKED`로 기록될 수 있다. Loop Manager가 저장된 Agent 시작 snapshot 뒤에 관리 rebase를 수행한 경우에도 그 HEAD·branch ref·worktree index 변화를 Agent 결과로 오인할 수 있다. 과거 snapshot의 정확한 복원을 요구하면 정상적인 fetch, runtime worktree 생성·정리와 source 갱신까지 차이로 누적되어 차단이 반복된다.

## Requirement

runtime handoff의 배정 중단 범위는 R-091대로 현재 runtime 전체다. 실행 중 호출은 강제로 취소하지 않고 안전한 반환·정리와 종료 확인을 기다리며, 호출 대기 작업은 시작하지 않고 저장된 단계로 남긴다. 작업 전체의 PR 게시·Merge를 기다리지 않는다.

bootstrap은 process lock을 획득한 뒤 runtime worktree를 선택·생성하거나 runtime 자식 및 프로젝트 PR 동기화를 시작하기 전에 self `<base-branch>`를 해석하고 `origin/<base-branch>`를 fetch해야 한다. 최신으로 검증한 원격 revision의 bootstrap·runtime으로 실행을 넘기며, source checkout의 commit·파일·동기화 성공은 그 revision의 선택과 실행 조건으로 사용하지 않는다. source에 있는 로컬 전용 코드·문서는 자동 실행 입력에 포함하지 않는다.

self base 조회는 정상 상태에서 읽기 전용으로 수행한다. 비정상 종료 뒤 hot rollback journal 때문에 SQLite가 `SQLITE_READONLY_ROLLBACK`을 반환한 경우에만 쓰기 가능한 연결로 한 번 다시 열어 SQLite의 미완료 트랜잭션 롤백과 기존 등록 정보 조회를 허용한다. 새 DB 생성·schema migration·등록 내용 변경은 수행하지 않으며, 다른 읽기 전용·권한·손상 오류나 재시도 실패는 원래 오류로 보존한다. 이 복구는 기존 실행·DB 접근 조율 안에서 수행한다.

source bootstrap이 원격 base보다 뒤처졌거나 로컬 동기화를 할 수 없어도 검증한 원격 bootstrap·runtime을 실행할 수 있어야 한다. 원격 실행 revision·소유권 자체를 확인할 수 없는 오류는 상태를 보존해 재시도하며, 로컬 갱신 실패와 구분한다. 원격 실행 코드와 fetch된 원격 SHA는 일치해야 하지만 source checkout의 HEAD까지 일치하도록 요구하지 않는다.

Loop Manager는 각 프로젝트의 기본 worktree 동기화 여부를 결정하기 전에 저장된 모든 Merge 대기 PR의 실제 GitHub state, Merge 여부와 identity를 동기화해야 한다. Merge 또는 Close로 종료된 PR의 상태 전이, worktree cleanup 판정과 알림 outbox 기록을 먼저 영속화하고, 갱신된 ExecPlan 및 project 상태를 다시 읽은 뒤 기본 worktree의 safe fast-forward를 평가해야 한다.

같은 실행에서 하나 이상의 PR Merge를 새로 확인했다면 낡은 PR_OPEN project mirror나 다른 안정된 PR 때문에 기본 worktree 동기화를 생략하지 않는다. 최신 원격 OID로 R-056의 상태별 eligibility를 평가한다. dirty behind는 모든 로컬 경로·index를 보존할 수 있을 때 FF-only를 적용하고 dirty diverged는 rebase하지 않는다.

로컬 동기화는 R-056의 equal·ahead·behind·diverged 처리와 사용자 작업 보존 조건을 적용한다. 겹치는 변경·사용자 operation·동시 수정 등으로 안전하게 처리할 수 없으면 기존 checkout을 보존하고 실제 skip 이유를 기록한다. 이 실패나 skip은 검증한 원격 bootstrap·runtime 또는 관리 worktree의 생성·재개·rebase·검증을 막지 않는다.

bootstrap/source checkout의 HEAD·index·tracked·untracked·ignored 파일 상태는 Planner·Worker·Reviewer가 소유하는 관리 worktree 변경 범위가 아니다. source가 dirty하거나 Agent 실행 중 local 상태가 바뀌었다는 사실만으로 Agent Git 범위 위반 또는 `PROJECT_BLOCKED`를 만들지 않는다. 그 상태는 R-056의 부수적인 로컬 동기화 판단에만 사용하며 자동 작업 입력·실행·차단 해제 조건에서 제외한다.

모든 등록 프로젝트의 Agent 접근 권한은 provider의 sandbox와 역할별 쓰기 정책으로 강제한다. Git 상태의 전후 차이를 접근 권한 위반으로 판정하는 검사는 R-055에 따라 제거한다. Manager는 작업 ID와 등록 규칙으로 정한 대상에서 동기화·게시·정리를 수행하고, 해당 Git 동작과 후보 검증에 필요한 현재 조건만 확인한다. 다른 작업의 branch·ref·worktree 변경이나 과거 snapshot 불일치는 Agent 위반·프로젝트 차단의 근거가 아니다.

사용자는 Agent 실행 중에도 문서나 코드를 `<base-branch>`에 직접 commit·push할 수 있다. Manager는 현재 원격 원본을 기준으로 기존 branch·worktree·session을 보존해 필요한 rebase·후보 검증·Review를 수행한다. 원격 작업 브랜치에 추가된 작업의 받아오기·통합과 기존 게시 이력 재작성에 대한 조건부 force push는 R-055의 변경 관계·이번 게시 OID 기준으로 구분한다.

다른 프로세스의 fetch/push로 remote-tracking ref·FETCH_HEAD가 바뀌는 것은 모든 단계에서 정상 관측 입력이다. ref 변화만으로 Agent 위반·프로젝트 차단을 만들지 않고 현재 origin·선택 자원·실제 원격을 재확인한다. 실제 후보·검증 입력이 바뀐 경우만 필요한 통합·재검증으로 연결한다. 관측 경쟁은 제한 재시도·대기로 처리하고, 확인 가능한 동일 작업의 이력 재작성은 R-055의 정상 처리로 연결한다. 현재 작업 목표·소유 대상·복구 원본이 확인된 충돌 복구 불가 파일은 R-055의 파일별 원본 복원·재작업으로 처리하고 다른 파일·index 상태와 기존 commit을 유지한다. 원격 삭제 의도·필수 결정·작업 대상이나 실제 게시 권한을 확정할 수 없으면 해당 통합·게시만 보류하며 로컬 수용과 분리한다.

Manager가 생성·게시한 원격 작업 branch가 실제 삭제됐고 PR이 Merge되지 않았다면 R-055에 따라 해당 작업을 중단하고 명시적 재시작을 기다린다. runtime 또는 Manager 프로세스의 정상 재시작·handoff만으로 그 작업을 재개하거나 branch를 재생성하지 않는다. 사용자의 작업 재시작 요청은 최신 원본에서 새 실행을 준비하는 입력이며 과거 작업·session·게시 감사와 독립 gate를 유지한다.

journal·receipt·reflog·원본 Agent snapshot과 호출·정책 감사는 보존한다. 과거 격리 ledger 부재나 호출 실패·재시도·단계 전환을 이유로 Git snapshot 접근 권한 검사를 재활성화하거나 격리 성공을 합성하지 않는다.

refs/remotes/origin/HEAD를 원격 선택에 실제로 사용하는 경우 현재 symbolic target과 원격을 확인한다. 파생 OID 변화·최초 생성·삭제는 접근 권한 판정에 사용하지 않으며, 필요한 원격 기준을 확인할 수 없는 경우 해당 원격 선택만 재관측·보류한다.

위 책임 분리를 Agent 결과·저장 단계 재개·PLAN_BLOCKED·PROJECT_BLOCKED 재평가와 정상·진단 checkpoint에 동일하게 적용한다. 실제 Git 작업 조건과 후보 검증은 유지하고 낡은 비교 기준으로 정상 변경을 다시 거부하지 않는다. 기존 Git 오탐·지속 실행 실패의 자동 해소와 수동 입력 수용은 R-055를 따른다.

Agent는 선택 관리 worktree 밖 파일·shared Git metadata를 쓸 권한이 없으며 이를 sandbox로 제한한다. 일반 verify는 실행한 후보·검증 입력을 확인하고 branch·push·PR의 Manager 책임을 확대하지 않는다.

Planner·Worker·Reviewer와 유지보수 Planner는 각 호출에서 선택된 관리 worktree를 workspace로 사용하며, provider의 내장 쓰기 제한을 적용한다. 작업 디렉터리 지정만으로 제한이 적용됐다고 간주하지 않는다. 기본 source checkout, 다른 worktree와 shared Git metadata는 쓰기 허용 범위에 포함하지 않고, Reviewer는 선택된 worktree도 읽기 전용으로 사용한다. workspace 쓰기 허용은 해당 역할의 문서·파일 변경 범위를 확대하지 않으며 역할별 최종 문서·결과 검증은 유지한다. 필요한 임시 작업은 해당 실행에 소유권이 있는 제한된 경로에서만 허용한다.

Codex를 기본 provider로 유지하고 Claude는 공식 문서에 근거해 일반적인 호출 범위에서 지원을 구현한다. Claude의 모든 기능·OS·CLI 조합이나 Codex와 동등한 실환경 검증 수준을 완료 조건으로 요구하지 않는다. 기존 내장 기능의 실행별 설정, 파일 권한과 명령 sandbox로 역할별 쓰기 경계를 구성하며 추가 디렉터리·쓰기 경로 허용만으로 범위 밖 쓰기가 모두 차단됐다고 판단하지 않는다. 시작 시 적용하는 설정의 우선순위·병합·기본 허용 경로는 문서와 모의 테스트로 확인한다.

Claude 지원 구현은 공식 웹 문서의 호출·설정 계약과 신규·재개·fallback의 모의 테스트로 완료 여부를 판단한다. CLI 설치·계정 확보·유료 호출·금지 쓰기 실연이나 실환경 완전성 증명을 개발 시작·완료 조건으로 추가하지 않는다. 실제 호출 검증이 없어도 구현을 진행하고 완료할 수 있으며, 문서·mock으로 확인한 범위와 실환경 미확인 범위를 구분해 보고한다. 미확인만으로 지원 구현 전체나 독립 Codex 작업을 차단하거나 선택 provider를 임의 전환하지 않는다.

실행 중 조직 managed 정책 변경의 감시·적용 전 차단·지속 제한 보장은 현재 범위에서 제외한다. 이를 해결하기 위한 별도 hook·polling·외부 격리나 조직 관리 환경 도입을 요구하지 않는다. 실제 인증·역할별 쓰기·자동 검토·session·OS/CLI 호환성과 managed 변경 대응의 후속 점검은 [FUTURE_IDEAS](../FUTURE_IDEAS.md#claude-실환경-후속-점검)에 남기며 현재 작업의 선행·완료 조건으로 승격하지 않는다.

Codex와 Claude의 자동 승인 검토는 쓰기 제한과 함께 유지한다. 쓰기 경계 확대와 격리 밖 재실행 요청은 자동 검토의 승인으로 허용할 수 없는 거부 대상으로 두고, 나머지 검토 대상은 provider의 자동 검토로 처리한다. 허용 범위 안의 모든 동작에 검토가 발생한다고 보장하지 않으며, 자동 검토가 비활성화된 설정이나 단순 편집 자동 승인 모드를 이 요구사항의 자동 검토 유지로 간주하지 않는다. Worker 결과에 대한 Loop Manager의 Reviewer 검토는 provider의 실행 전 승인 검토와 별개로 유지한다.

실제 선택 호출에서 시작 설정의 정책 충돌·미지원·격리 초기화 실패가 확인되거나 실행 중 실제 권한·인증·도구 오류가 관측되면, 제한을 해제하거나 자동 검토를 끈 채 진행하지 않는다. 구체적인 원인과 작업물·session·재시도 이력을 보존해 해당 호출을 중단한다. 이 실제 실패 처리와 실환경 미검증·범위 밖 managed 변경 대응을 구분하며 후자의 미확인만으로 개발을 차단하지 않는다. 내장 sandbox가 없는 Claude native Windows는 현재 미지원 호출로 처리하고 WSL·컨테이너 등 새로운 infrastructure를 자동 도입하지 않는다. 과거 실행의 기본 checkout 제외와 실제 위반 보존은 R-055를 따른다.

격리 적용·거부와 실제 효력 확인 결과는 provider·CLI 버전·역할·canonical worktree·repository identity·시작 설정·session 및 개별 호출에 결합해 보존한다. Codex는 R-050의 직접 실행과 내장 정책 확인 범위를 유지한다. 프로세스 전체의 내부 쓰기·mount 상태나 중첩 sandbox 호환성을 추가 증명 조건으로 요구하지 않으며 정상 인증·session·로그와 허용된 임시 ref를 workspace 밖 쓰기 권한과 구분한다. Claude는 문서·모의 테스트로 확인한 호출 구성을 실제 실행 관측과 구분해 기록하고, 설정 인자나 mock 성공을 실제 격리 성공으로 기록하지 않는다. 신규·재개·fallback과 provider·시작 설정 변경에는 해당 호출 구성을 다시 확인하며 실시간 managed 변경 감시는 요구하지 않는다. 비밀값이나 source checkout의 로컬 파일 내용은 증거에 저장하지 않는다.

Loop Manager가 수행한 fetch·worktree 생성·정리·rebase·source 동기화는 실제 동작과 중단 복구에 필요한 대상·기대 상태·결과를 durable checkpoint로 보존한다. 이 기록은 미완료 Git 작업의 재개에 사용하며 Agent의 과거 snapshot을 현재 상태에 맞추는 절차를 요구하지 않는다. 중단 뒤 현재 원격·작업물과 완료 상태를 확인해 필요한 동기화·확정·재검증부터 이어간다.

저장된 Git 오탐의 planning 오류·PLAN_BLOCKED·PROJECT_BLOCKED·지속 실행 실패는 R-055에 따라 입력 지문 변경이나 수동 DB 수정 없이 자동 재평가한다. 사용자 인계 뒤 파일·staging·commit·rebase 결과를 현재 입력으로 수용하며 session·작업물·실패 감사와 독립 차단을 보존한다. 로컬 동기화 실패나 과거 snapshot 불일치는 별도 해제 gate가 아니다.

runtime 자식은 bootstrap이 전달하고 검증한 canonical source checkout, 현재 runtime SHA, repository identity와 origin을 이용해 등록 프로젝트가 실행 중인 Loop Manager 자신인지 결정해야 한다. 자기 자신으로 인정하려면 등록된 canonical repository path가 runtime source와 같고 실제 Git repository 및 origin identity도 일치해야 한다. 디렉터리 이름, 프로젝트 표시명, 원격 저장소 이름 또는 경로 prefix만으로 자기 자신으로 판단하지 않는다.

self 프로젝트에 저장된 `<base-branch>`는 PR의 base, Merge 뒤 fetch·source checkout 갱신과 다음 runtime 선택에 동일하게 사용한다. 현재 기본값이 `main`이라는 이유로 handoff code나 로그에 `origin/main`을 고정하지 않으며, self 등록이 없는 bootstrap의 기존 기본 branch 선택은 이 Requirement에서 변경하지 않는다.

Planner·Worker·부분/전체 verify·Reviewer·게시·복구 등의 **실제 다음 단계 진입 경계**에서 등록된 self `origin/<base-branch>`의 최신 원격 SHA와 현재 runtime SHA를 확인한다. runtime 최초 실행의 단계 진입에서는 즉시 관측하고, 이후에는 같은 runtime에서 마지막 **성공한 관측**으로부터 30초가 지나기 전이면 원격 조회와 실행 입력 비교를 생략한다. 30초는 검사 간 최소 간격이며 시간만 지나거나 단계가 진행되지 않았다는 이유로 검사를 실행하지 않는다. 병렬 프로젝트·작업은 같은 runtime의 관측 시각과 종료 결정을 공유하고 동시에 도달한 경계의 중복 원격 조회를 억제한다. 단계 종료는 다음 단계 진입 없이 별도의 원격 재검사를 만들지 않는다.

재시도 시점·Agent/verify 자리·사용량·종료 불명 호출·공유 자원 등의 **대기 중에는 runtime 원격 조회·입력 비교를 수행하지 않는다**. 대기 중에도 사용자 pause·취소와 이미 확정된 runtime 종료 결정을 확인하며, 대기 해소 후 실제 다음 단계로 진입할 때 위 관측 조건을 적용한다. 원격 변경의 게시 경로나 PR 존재·Merge 여부를 조회·판정하지 않고 실행 입력이 달라진 원격 전진만 갱신 대상으로 삼는다. 일반 문서만 바뀐 원격 SHA 전진은 runtime 교체 사유가 아니다. 일반 PR 상태 반영은 기존 작업 수명주기에서 수행하며 runtime 변경 확인·갱신의 선행 조건으로 두지 않는다. source checkout 동기화의 성공 여부는 runtime 갱신 조건이 아니다.

runtime 갱신 비교는 현재 실행 중인 runtime과 최신 원격 revision 사이의 실행 입력을 기준으로 한다. 소스 코드·실행 스크립트의 추가·수정·삭제·이동과 실행 코드의 해석·로딩을 바꾸는 의존성 선언·실행 설정 변경을 감지한다. 공통·역할·Ponytail 실행 지침인 `docs/AGENTS.*.md`도 갱신 입력에 포함한다. Requirement·ExecPlan·PRODUCT·ARCHITECTURE·README·템플릿 등 일반 문서 변경은 재시작 판정에서 제외한다. 문서 경로 아래라도 실제 실행 코드인 파일은 실행 입력으로 취급한다. 문서와 코드가 함께 바뀌면 갱신하며, 일반 문서만 바뀌어 계속 실행한 뒤에도 비교 기준은 실제 실행 runtime으로 유지해 누적된 코드 변경을 놓치지 않는다. 변경 비교 실패를 문서 전용 변경으로 간주하지 않는다. 문서의 planning·단계별 반영·R-064의 호출별 현재 지침 적용 규칙은 유지하며 이 제외는 runtime 재시작 판정에만 적용한다.

runtime handoff 여부는 source checkout의 fast-forward 성공에는 의존하지 않지만, 현재 runtime SHA와 fetch된 `origin/<base-branch>` SHA의 관계에는 의존한다. source checkout 갱신이 overlap, ahead·diverged 또는 다른 안전 사유로 skipped·failed여도 원격 base가 현재 runtime보다 앞서고 실행 입력도 달라졌으면 자동 갱신·재시작한다. bootstrap이 이미 같은 실행 입력을 선택한 경우에는 원격 SHA나 PR 상태가 달라도 같은 runtime에서 계속한다. 두 SHA의 fast-forward 관계를 증명할 수 없으면 자동 handoff로 취급하지 않고 기존 repository safety 정책에 따라 상태를 보존한다.

self runtime handoff 판정은 같은 실행에서 병렬 프로젝트 작업을 새로 배정하기 전의 안전한 단계 진입 경계에서 이루어져야 한다. 검증된 코드 변경으로 종료 필요가 확정되면 대상 원격 SHA와 인계 요청을 현재 runtime의 메모리에 한 번 고정하고, 그 이후에는 TTL과 무관하게 추가 원격 조회·입력 비교 없이 해당 결정을 사용한다. 새 관리 편입·Agent subprocess·verify·후속 단계를 시작하지 않으며, 이미 영속화된 세션·retry·workspace 상태를 지우지 않는다. 진행 중인 호출의 결과 수집·checkpoint 확정·안전한 반환은 신규 단계 진입과 구분해 허용하고, 대기 중 호출은 시작하지 않은 채 반환한다. 원격 fetch·입력 비교 또는 repository identity 검증이 실패하면 이를 성공 관측이나 무변경으로 캐싱하지 않고, 기존 일시 오류 또는 repository safety 정책에 따라 상태를 보존한 채 R-091의 비차단 재개 경로에서 실제 단계 진입 시 다시 확인한다.

원격 SHA가 같거나 실행 입력이 동일하면 기존 runtime에서 계속 실행한다. 실행 입력이 달라진 검증된 fast-forward 후속이면 R-091대로 전체 신규·후속 배정을 멈추고 이미 실행 중인 단계만 반환·정리한 뒤 저장된 상태를 보존한다. 작업 전체 완료·PR 게시·Merge나 주기 실행을 기다리지 않고 bootstrap으로 제어를 넘겨 최신 원격을 다시 확인하고 새 runtime을 자동 시작한다. 실행 중 runtime 파일을 교체하거나 진행 중 Agent·verify를 강제로 종료하지 않으며, 재시작 후 같은 작업을 중복 실행하거나 실패·교정 이력을 초기화하지 않는다. 다른 등록 프로젝트의 원격 변경은 self runtime 변경 근거가 아니다.

## Acceptance Criteria

- 실제 비정상 종료로 hot journal이 남아 읽기 전용 조회가 실패해도 기존 self base와 데이터를 롤백 후 읽어 시작할 수 있다. 일반 조회는 읽기 전용이며 다른 오류에는 쓰기 재시도나 DB 초기화를 하지 않는다.

- bootstrap은 process lock을 획득한 뒤 runtime worktree 선택·생성, runtime 자식 시작과 프로젝트 PR 조회보다 먼저 self `<base-branch>`를 해석하고 `origin/<base-branch>`를 fetch한다.
- bootstrap·runtime의 원격 실행 revision과 검증한 원격 SHA가 일치한다. source checkout의 HEAD 일치나 동기화 성공을 자식 실행 조건으로 요구하지 않는다.
- source checkout의 equal·behind·ahead·diverged·dirty·다른 branch 상태에서도 사용할 원격 revision이 검증되면 원격 bootstrap·runtime 실행과 PR 상태 조회가 진행된다. 로컬 전용 코드·문서는 실행 입력으로 사용하지 않는다.
- 안전한 로컬 fast-forward·rebase 또는 그 실패·skip은 R-056대로 처리하며, 사용자 작업을 보존하고 원격 기준 실행을 막지 않는다. 기본 checkout을 정리하거나 로컬 갱신만을 위해 다음 외부 실행을 기다리도록 요구하지 않는다.
- 저장 상태가 `PR_OPEN`인 PR이 GitHub에서 이미 Merge된 실행에서 PR 상태 동기화와 `MERGED` 전이, cleanup 판정 및 알림 outbox 기록이 기본 worktree skip 판정보다 먼저 완료된다.
- 새 Merge가 확인된 실행은 남아 있는 다른 안정된 `PR_OPEN`이나 낡은 project mirror 때문에 `PR 충돌 감지/복구 중 source checkout 보존`으로 분류되지 않고, fetch된 `origin/<base-branch>`에 대해 safe fast-forward eligibility를 평가한다.
- Merge 뒤 기본 worktree가 clean한 저장 base branch의 behind-only 상태이면 같은 실행에서 정확한 `origin/<base-branch>`로 fast-forward된다.
- 기본 worktree에 staged·unstaged·untracked 또는 ignored 변경이 있어도 모든 로컬 변경 경로가 원격 fast-forward 변경 경로와 부모·자식 관계까지 겹치지 않으면 local delta와 파일 내용을 보존한 채 fast-forward한다.
- 로컬 변경과 충돌하거나 소유권·동시 변경 때문에 안전한 동기화가 불가능하면 보존·skip 이유를 기록한다. ahead-only는 보존하고, 안전한 diverged의 로컬 commit rebase는 R-056에 한해서 허용하며 reset·stash·autostash·강제 checkout은 수행하지 않는다.
- source checkout의 dirty·sync skip·HEAD 차이는 검증한 `origin/<base-branch>`에서 수행하는 Planner·Worker workspace 생성, 기존 workspace 재개, PR conflict rebase와 검증을 막지 않는다.
- Planner·Worker·Reviewer 실행 전후 bootstrap/default worktree의 HEAD·index·파일·dirty 상태만 달라진 경우 이를 해당 Agent의 변경으로 귀속하거나 `PROJECT_BLOCKED`를 만들지 않는다.
- Agent 접근 권한은 sandbox로 강제되고 Git 상태 차이를 위반으로 판정하는 검사는 신규·legacy·재개·fallback·일반 verify·checkpoint 경로에서 제거된다. Manager Git 작업과 후보 검증의 현재 조건은 유지된다.
- 호출 실패·성공한 재시도·ledger 부재·역할 단계 전환과 다른 작업의 ref/worktree 변경이 snapshot 비교를 재활성화하거나 최종 차단을 만들지 않는다.
- 파생 `origin/HEAD`의 OID 변화·최초 생성은 Agent 위반을 만들지 않는다. 실제 사용하는 원격 선택 기준을 확인할 수 없으면 해당 선택만 재관측·보류하며 과거 snapshot 복원을 요구하지 않는다.
- 사용자가 Worker 실행 중 `<base-branch>`에 push해도 Manager는 기존 작업물·session을 보존해 최신 원본에 rebase하고 바뀐 후보의 필요한 Worker 확인·targeted test·전체 verify·Review를 거친다. 원격 작업 브랜치의 추가 작업 통합과 조건부 force push는 R-055대로 구분된다.
- 실제 Git 작업의 대상·origin·동일 자원 writer·미완료 operation과 최종 문서·구현 결과는 해당 동작의 조건으로 확인한다. sandbox의 실제 거부·정책 오류는 Git 상태 변화와 별도로 보존한다.
- Manager의 fetch·worktree 생성/정리·rebase·source 갱신 뒤 중단돼도 실제 완료 상태에서 재개하며 낡은 Agent snapshot이나 부족한 범위 비교 기준선 때문에 거부하지 않는다. 후보 변경은 필요한 재검증으로 연결된다.
- 기존 Git 오탐 차단과 pending 확인 요청은 같은 입력의 정상 실행·재시작에서 자동 해소된다. 사용자 작업·session·역사 감사·독립 차단을 보존하고 실제 미해결 원인은 해당 동작의 복구·보류로 처리한다.
- `MERGEABLE` 또는 `UNKNOWN`인 Open PR만 있고 새 Merge 완료가 없으면 기존 PR 대기 및 충돌 복구 권한을 변경하지 않는다. `CONFLICTING` 판정도 기존 소유권, worktree와 explicit lease 조건을 유지한다.
- runtime source와 같은 canonical repository path이며 실제 repository 및 origin identity가 일치하는 등록 프로젝트만 self로 판정한다. 같은 이름, 같은 origin만 가진 다른 clone, source 하위 경로, symlink alias 또는 state directory 아래 runtime 경로를 self source로 오인하지 않는다.
- runtime 확인·갱신 경로는 PR 목록·Merge 상태 조회를 요구하지 않는다. PR이 없거나 Open·Closed·Merged·조회 실패인 경우에도 같은 원격 실행 입력에는 동일한 갱신 판단을 내린다. 일반 작업의 PR 상태 반영·오류 처리는 기존 수명주기에서 유지한다.
- source checkout fast-forward가 로컬 안전 조건 때문에 skipped 또는 failed여도 원격 base가 현재 runtime SHA의 fast-forward 후속이고 실행 입력이 달라졌으면 정상 handoff 후 자동 재시작한다. 다음 bootstrap은 source local `<base-branch>`가 아니라 fetch된 `origin/<base-branch>`를 다시 조회해 runtime을 선택한다.
- 현재 runtime과 원격 실행 입력이 같으면 PR Merge 상태·원격 SHA 전진과 관계없이 runtime 재시작 없이 정상 처리를 계속한다.
- fetch된 self `origin/<base-branch>`와 현재 runtime SHA의 fast-forward 관계를 증명할 수 없거나 최신 원격 fetch·repository identity 검증이 실패하면 성공 handoff나 강제 동기화를 수행하지 않고 기존 일시 오류 또는 안전 오류와 재개 가능한 상태를 유지한다.
- self Planner 직접 게시·일반 외부 push로 등록 base의 실행 입력이 바뀐 경우에도 최초·30초 경과 후 다음 실제 단계 진입 경계에서 변경을 발견해 갱신하며 PR 조회·Merge 관측을 요구하지 않는다.
- 다른 등록 프로젝트의 PR Merge는 해당 프로젝트 상태와 기본 worktree를 정상 갱신하지만 Loop Manager 프로세스 전체의 runtime handoff를 만들지 않는다.
- 일반 문서만 변경된 self PR Merge·Planner 게시·외부 push에는 단계 배정 중단·runtime 재시작이 없다. 코드 또는 runtime 실행 지침이 변경되면 갱신한다. 코드 파일의 추가·삭제·이동, 의존성·실행 설정 변경, 여러 문서 전용 revision 뒤의 코드 변경을 실제 runtime과 비교해 감지하며 비교 실패를 변경 없음으로 처리하지 않는다.
- 최초 실제 단계 진입에서는 runtime 원격 코드를 즉시 확인한다. 성공 관측 후 30초 미만의 다른 단계 진입·성공/실패 종료·병렬 작업 경계와 30초 이상 진행 없는 구간에서는 추가 fetch·입력 비교가 없다. 30초 이상 경과한 뒤 실제 단계에 진입할 때만 다시 관측하며 같은 runtime의 병렬 진입에서 중복 조회를 수행하지 않는다.
- 재시도·사용량·기존 호출 종료·Agent/verify 자리·Git 자원 대기 중에는 runtime 원격 조회를 하지 않고 pause·취소·메모리에 확정된 종료 신호를 처리한다. 기다림 해소 뒤 실제 단계로 재진입할 때는 TTL에 따라 확인하며, 원격 관측 실패는 성공 검사 시각을 갱신하거나 변경 없음으로 판정하지 않는다.
- 코드 변경이 확정되면 현재 runtime 전체가 하나의 종료 결정과 확인한 인계 요청을 공유하고 이후 fetch·입력 비교·반복 로그 없이 새 관리 편입·Agent·verify·후속 단계를 중단한다. 이미 시작한 호출은 강제 종료하지 않고 결과·checkpoint·종료 근거를 안전하게 보존한 뒤 다음 주기 실행 없이 최신 runtime에서 재개한다. 종료 불명 호출을 빈 자리로 간주하지 않으며 같은 원격 revision에는 재시작을 반복하지 않고 fetch·identity·ancestry 확인 실패를 성공 갱신으로 기록하지 않는다.
- runtime handoff는 해석한 `<base-branch>`, 현재 runtime SHA, fetch된 원격 SHA, self repository와 실행 입력 비교 결과를 비밀값 없이 로그에 남기며 오류나 BLOCKED 알림으로 취급하지 않는다.
- 자동 테스트는 main·대체 base, 모든 로컬 source 상태의 원격 revision 선택·비차단 동기화, PR 조회와 독립적인 코드 변경 감지·handoff와 이미 최신 runtime의 계속 실행을 확인한다. Planner push·외부 push의 self base 전진에 실행 입력 변경이 있으면 갱신 대상이며 다른 프로젝트 Merge·원격 전진 없는 fetch는 handoff를 만들지 않는다. origin/source identity 오류·fetch 실패·non-fast-forward 관계·handoff 뒤 새 작업 미시작과 중단·재시작도 검증한다.
- 접근 정책·Manager Git 작업·재개 테스트는 R-055의 T01–T06·T10–T14·T16–T17·T22·T25–T26에 대응한다. sandbox 적용, 호출 실패·단계 전환 뒤 Git 범위 검사 미재활성화, 원격 동기화·게시와 기존 오탐 자동 해제를 실제 소비자에 연결한다.
- 각 역할의 신규 호출과 session 재개에서 workspace 및 허용 파일 쓰기와 source checkout·다른 worktree·shared Git metadata 쓰기 거부를 검증한다. Reviewer의 worktree 쓰기도 거부한다. Codex는 R-050의 직접 실행과 내장 정책 확인 범위를 적용하며, 파일 쓰기 제한과 R-064의 common+Reviewer 역할 instruction에 담긴 테스트 재실행 금지를 구분한다. Claude는 문서에 대응한 호출 구성·역할별 정책을 모의 검증하고 실제 호출 결과와 구분해 기록한다. Codex와 동등한 실환경 검증을 요구하지 않는다.
- Codex·Claude의 지원 환경에서 쓰기 경계 확대와 격리 밖 재실행은 거부하면서 나머지 승인 검토 대상의 자동 검토가 유지됨을 검증한다. 단순 편집 자동 승인으로 대체하거나 자동 검토를 전부 비활성화한 설정은 실패로 판정한다. 기존 Loop Manager Reviewer 흐름도 유지한다.
- CLI 버전·provider·설정 변경, session 재개와 중단 뒤의 정책 재확인, 설정 충돌·격리 초기화 실패·Claude native Windows 미지원 시 해당 호출의 중단과 상태 보존을 검증한다. 실제 관측한 sandbox 오류와 Git 오탐의 자동 복구를 구분하며 실행 중 managed 정책 변경을 별도로 감시하는 테스트는 요구하지 않는다. 확인·거부 기록에는 비밀값과 source checkout의 로컬 파일 내용을 포함하지 않는다.
- Claude CLI·유료 계정이 없는 개발 환경에서도 공식 웹 문서에 대응하는 설정·호출 구성과 모의 테스트로 지원 구현을 검증할 수 있다. 신규·재개·fallback의 역할별 쓰기 제한, Bash·하위 프로세스와 Edit·Write·NotebookEdit 파일 도구의 source·다른 worktree·resolved shared Git 보호, 자동 승인 검토 유지 및 권한 확대 거부를 확인한다. 추가 허용 설정만으로 범위 제한을 인정하지 않고 상속·배열 병합·임시 경로를 확인한다. 실제 호출 검증 미수행은 명시하되 그 부재로 개발·완료를 보류하지 않는다. CLI 설치·계정 확보·유료 호출·완전 지원 증명을 시작·완료 조건으로 추가하지 않는다. 실제 Claude 호출에서 관측한 인증·지원·정책 오류는 상태를 보존하고 해당 호출에 한해 처리하며 모의 성공과 실제 정책 적용 성공을 구분한다.
- 실행 중 managed 정책 변경의 감시·적용 전 차단·지속 제한 보장과 실환경 점검은 현재 완료 조건에서 제외되고 FUTURE_IDEAS에 후속 확인으로 남는다. 이 미확인 때문에 사용자 판단 대기·새 infrastructure 요구·독립 Codex 작업 차단을 만들지 않는다.
- 전체 verify에서 exact-SHA runtime bootstrap, process lock, Manager runtime scope reconciliation, PR conflict recovery, source checkout 보존과 origin-only Planner·Worker 입력 규칙이 유지된다.

## Product Impact

- PR의 실제 완료 상태가 source checkout 동기화 판단보다 먼저 반영되며, 종료된 PR의 낡은 상태 때문에 기본 worktree 갱신이 불필요하게 지연되지 않는다.
- source bootstrap이 뒤처져도 검증한 최신 원격 bootstrap·runtime으로 실행을 넘기고, 로컬 동기화는 실행과 분리한다.
- 단계 경계에서 self 원격 base의 실행 입력 변경을 확인하면 현재 단계만 마치고 상태를 보존해 자동 갱신·재시작한다.
- non-overlap dirty source checkout은 로컬 변경을 유지한 채 원격 base로 전진할 수 있다.
- source checkout을 갱신할 수 없는 로컬 상태는 시작·복구 시점 모두 원격 bootstrap·runtime과 관리 worktree 작업을 막지 않는다.
- bootstrap이 이미 원격 base revision이면 dirty 상태를 정리하지 않고 실행하며, Agent 실행 중 bootstrap 파일 상태가 바뀌어도 관리 worktree 위반으로 오인하거나 과거 signature에 프로젝트를 영구 차단하지 않는다.
- Agent 접근 권한은 sandbox가 담당하며 Git 상태 차이의 범위 위반 판정을 제거한다. Manager는 현재 동작 조건으로 동기화·게시·복구를 수행하고 기존 Git 오탐 차단을 자동 해소한다.
- Codex를 기본으로 유지하고 Claude는 일반적인 호출 구성을 공식 문서·모의 테스트로 개발한다. 실환경 미검증과 범위 밖 managed 변경 대응은 지원 구현이나 독립 Codex 작업의 완료를 막지 않는다.
- provider의 내장 workspace 쓰기 제한과 자동 승인 검토를 함께 사용한다. 격리 밖 쓰기 권한을 자동 승인하지 않으며, 실제 선택 호출에서 미지원·정책 충돌·제한 적용 실패가 확인되면 상태와 근거를 보존하고 해당 호출을 멈춘다.

## Non-Goals

- 현재 프로세스 안에서 runtime module을 hot reload하거나 새 runtime 자식을 재귀적으로 시작하지 않는다.
- overlap dirty, ahead, diverged, 다른 branch 또는 소유권 불명의 source checkout을 강제로 최신화하지 않는다.
- R-056이 허용한 안전한 diverged 로컬 commit rebase 외에 source를 강제 갱신하지 않는다. dirty 변경의 자동 stash·autostash는 수행하지 않는다.
- 저장소 이름이나 GitHub URL만으로 임의의 clone을 현재 bootstrap source와 동일한 checkout으로 신뢰하지 않는다.
- 기존 PR 충돌 복구의 rebase, force-with-lease, branch 소유권 또는 Agent 권한을 확대하지 않는다.
- runtime refresh를 이유로 영속화되지 않은 작업을 폐기하거나 관리 worktree, Agent session, retry history를 정리하지 않는다.
- PR Merge 자체·일반 문서 전용 게시·다른 프로젝트의 변경은 runtime 재시작 사유로 삼지 않는다. self 원격의 실행 입력 변경만 감지하며 PR 관리 기능의 기존 완료·충돌 복구 규칙을 바꾸지 않는다.
- Git 범위 검사 제거를 이유로 sandbox의 source·다른 worktree·shared Git metadata 쓰기 제한이나 Manager의 작업물 보존·후보 검증·조건부 게시를 완화하지 않는다.

