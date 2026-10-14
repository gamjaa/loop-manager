# [R-020] Manager-Owned Runtime Worktree Scope Reconciliation

## Context

Loop Manager의 exact-SHA runtime 선택·등록·실행 identity 검증은 기존 ARCHITECTURE의 계약이다. 이 Requirement는 runtime 준비·재사용을 과거 Agent Git 상태와 비교해 접근 권한 위반으로 오인하는 잔여 경로를 제거하고 중단된 작업을 이어가는 데 집중한다. 접근 권한의 sandbox 강제, Git 범위 검사 제거와 기존 차단의 자동 복구는 R-055를 따른다.

Loop Manager 저장소 자체를 등록 프로젝트로 운영하면 bootstrap도 같은 저장소의 shared Git metadata를 사용한다. `origin/main`이 새 SHA로 전진한 다음 실행에서 부모 bootstrap은 process lock 안에서 새 SHA의 detached runtime worktree를 등록한 뒤 runtime 자식을 시작한다. 이전 실행에서 저장된 Planner 또는 Worker scope가 남아 있으면 새 runtime 등록은 그 기준선에 없지만, 현재 runtime 자식은 이 변경이 Agent 호출 전에 부모가 만든 것이라는 출처를 전달받지 못한다. 그 결과 clean한 exact-SHA runtime도 Worker 또는 Planner가 만든 공유 Git 변경으로 오인돼 프로젝트가 반복적으로 차단될 수 있다.

process lock과 현재 runtime의 identity 검증은 실행 대상을 보호한다. 다른 worktree의 상태 변화나 runtime 등록을 Agent의 쓰기로 추정하는 검사는 R-055에 따라 제거하고, 실제 선택 runtime의 유효성만 실행 조건으로 확인한다.

## Requirement

부모 bootstrap과 runtime 자식은 현재 실행의 source repository, 등록 `origin/<base-branch>`에서 선택한 SHA, 결정적 runtime 경로와 실행 identity를 같은 값으로 해석한다. 기존 process lock·runtime 검증은 유지하되 저장된 Agent scope나 전체 worktree catalog와의 과거 비교를 자식 실행 조건으로 요구하지 않는다. 등록 base를 `main`으로 고정하지 않는다.

runtime SHA는 현재 실행을 시작할 때 선택한 코드 revision이고, 이후 관측한 원격 base SHA는 관리 작업의 최신 입력이다. 선택 SHA B의 소유 runtime이 유효한 동안 추적 ref가 정상적으로 C로 전진해도 `origin/<base-branch> == B`를 계속 요구하지 않는다. 선택 당시의 원격 근거와 현재 실행 identity·정확한 runtime 상태를 검증하고 C의 전진은 R-023대로 별도 처리한다. 추적 ref 변경만으로 실행 중인 runtime을 C로 바꾸거나 새 handoff를 만들지 않으며, 실제 handoff는 R-023의 기존 조건을 따른다.

runtime 생성·재사용·정리는 Agent Git 범위 판정이나 비교 기준선 갱신을 요구하지 않는다. 현재 runtime이 유효하면 session·작업물·재시도 이력·계획을 보존해 이어가며 다른 fetch/ref/worktree의 변화로 실행을 막지 않는다. 현재 작업의 원격 갱신과 후보 검증은 R-055·R-023의 Manager 경계에서 처리한다.

Manager 소유 runtime으로 인정하려면 최소한 다음 조건을 모두 만족해야 한다.

- 경로가 현재 state directory와 source repository identity에서 계산한 `<state-dir>/runtimes/<repository-id>/<runtime-sha>/`의 정확한 경로다.
- worktree가 현재 source repository에 등록돼 있고 clean한 detached `HEAD`이며, `HEAD`와 예상 runtime SHA가 정확히 같다.
- 예상 SHA는 현재 bootstrap이 fetch한 등록 `origin/<base-branch>`에서 선택됐으며 그 선택 근거와 부모·자식이 공유하는 실행 identity에 결합돼 있다. 이후 추적 ref의 값만으로 선택 근거를 대체하지 않는다.
- 등록 또는 재사용이 동일한 process lock의 현재 실행 안에서 발생했음을 확인할 수 있고, 그 사실을 Agent의 자유 형식 출력이나 경로 이름만으로 추론하지 않는다.
- 실제 선택 runtime의 현재 등록과 상태가 정확한 경로·repository·선택 SHA·실행 identity에 결합돼 있다. 다른 worktree의 등록·상태와 과거 catalog delta는 이 runtime의 실행 조건이 아니다.

runtime 등록 전후에 중단돼도 다음 실행은 durable한 실행 identity와 실제 선택 runtime을 확인해 미완료 준비를 멱등하게 완성하거나 유효한 runtime을 재사용한다. runtime·원격 ref 변화와 낡은 Git snapshot 때문에 남은 차단은 R-055에 따라 입력 지문 변경이나 수동 DB 수정 없이 자동 해소한다. 같은 Agent session·작업 공간과 실패 감사를 보존하고 실제 sandbox 오류·독립 차단은 유지한다.

Agent 시작·결과·Manager checkpoint의 원문은 감사로 보존하며 현재 상태와의 일치를 요구하지 않는다. runtime 선택·저장 단계 재개·checkpoint에서 Git 범위 검사가 다시 실행되지 않게 하고, 진단에는 실제 선택 runtime SHA·경로·repository·실행 identity와 필요한 원격 관측 결과를 비밀값 없이 남긴다.

예상 runtime 경로가 다른 repository에 속하거나 dirty·attached·다른 SHA·소유권 불명·미완료 operation이면 reset·clean·재등록·snapshot 초기화로 복구하지 않는다. 해당 runtime 준비·재개만 안전하게 보류하고 작업물을 보존한다. 무관한 ref 변화와 실제 runtime 자원 위반을 R-055대로 구분한다.

이 변경은 bootstrap이 이미 수행하도록 승인된 exact-SHA runtime 선택과 fetch의 귀속만 명시적으로 연결한다. Agent가 worktree 등록, commit, fetch, rebase, push 또는 config 변경을 수행할 권한을 추가하지 않으며, source checkout이나 활성 runtime의 파일·index·`HEAD`를 변경할 권한도 넓히지 않는다.

## Acceptance Criteria

- self-hosted Loop Manager 저장소에서 SHA A의 persisted Planner, Worker 또는 Reviewer scope가 남아 있고 등록 `origin/<base-branch>`가 SHA B로 전진한 경우, 다음 bootstrap이 exact-SHA runtime B를 등록한 뒤 runtime 자식이 이를 Agent 범위 위반으로 귀속하지 않고 저장된 단계부터 재개한다. `main`·`develop`과 `origin/main` 부재·다른 SHA를 각각 확인한다.
- B 선택 뒤 선검증·저장 재개·checkpoint 검사 전에 외부 fetch/push로 base가 정상적으로 C로 전진해도, 유효한 runtime B와 선택 근거는 유지되고 C는 R-023의 관리 rebase·재검증으로 처리된다. 단순 tracking ref 불일치 때문에 runtime 기준선 오류나 새 handoff를 만들지 않는다.
- 파생 `origin/HEAD` 변화·최초 생성과 다른 worktree의 등록·정리는 Git 범위 위반을 만들지 않는다. 실제 원격 선택에 필요한 기준이 확인되지 않으면 해당 선택만 재관측·보류하며 기존 감사는 보존한다.
- 정상 재개 시 기존 Agent session ID, 관리 worktree, Worker diff, 결과 snapshot, retry·failure history와 ExecPlan 상태가 보존되며 runtime 등록만으로 `PLAN_BLOCKED`, `PROJECT_BLOCKED` 또는 새 Agent session을 만들지 않는다.
- runtime B가 이미 정확한 clean detached worktree로 등록돼 있으면 다음 실행은 재사용을 멱등한 no-op으로 처리하고 기준선 조정·중복 등록·반복 차단을 요구하지 않는다.
- 실행 대상으로 인정하는 runtime은 현재 실행 identity가 지정한 정확한 경로다. 다른 경로와 과거·미래 SHA의 runtime을 prefix만으로 선택하지 않되, 이들의 등록·상태 변화 때문에 현재 runtime을 거부하지 않는다.
- runtime 경로, repository identity, detached 상태, clean 상태와 `HEAD == runtime SHA`를 실제 Git 상태로 검증한다. 경로 이름, 저장된 문자열 또는 Agent 응답만으로 소유권을 인정하지 않는다.
- bootstrap의 runtime 준비와 저장 단계 재개 사이에 중단돼도 다음 실행은 기록된 의도와 실제 선택 runtime을 확인해 동일 준비를 한 번만 반영하고 작업을 재개한다.
- 기존 runtime·ref·worktree 변경 오탐의 차단과 대기 확인 요청은 현재 실행 조건이 충족되면 자동 해소된다. 과거 snapshot의 완전 일치나 동일 입력의 변경을 요구하지 않는다.
- 다른 worktree·branch·Git config·ref의 변화가 함께 있어도 이를 Agent 접근 권한 위반으로 추정하지 않는다. 현재 선택 runtime의 실제 오류와 sandbox 정책 실패·독립 차단은 별도로 보존한다.
- 예상 runtime이 dirty, attached, 다른 SHA, 다른 repository 또는 ownership 불명 상태이면 reset, clean, 강제 checkout, 제거나 재등록 없이 진단 가능한 오류로 중단하고 기존 Agent session과 작업 공간을 보존한다.
- 선택 runtime·작업과 무관한 외부 fetch/ref/worktree 변화는 실행을 막지 않는다. Agent의 worktree 등록·branch/config 쓰기 제한은 sandbox로 강제한다.
- runtime 선택 SHA·정확한 경로·실행 identity와 실제 검증 결과를 비밀값 없이 진단 로그에 남긴다. 과거 snapshot은 현재 접근 권한을 판정하는 근거로 사용하지 않는다.
- 자동 테스트는 SHA A의 작업·과거 scope를 저장한 뒤 실제 bootstrap 경로로 등록 `origin/<base-branch>` SHA B의 runtime을 선택해 저장 단계부터 재개한다. B 선택 후 정상 원격 전진·다른 worktree 변화와 ledger 부재·호출 실패를 더해도 재개·checkpoint에서 Git 범위 검사가 재활성화되지 않음을 확인한다.
- 자동 테스트는 Planner, Worker와 Reviewer의 persisted scope, 같은 SHA runtime 재사용, 등록 직후 중단, 기존 false-positive 결과 복구, 임의 worktree 추가, runtime dirty·attached·wrong-SHA·wrong-repository, runtime과 다른 shared Git 변경이 동시에 존재하는 경우를 검증한다.
- 전체 verify에서 sandbox 쓰기 제한, 실제 runtime 유효성, 기본 worktree 보존, process lock과 중단 재개 조건이 유지된다.

## Product Impact

- Loop Manager 저장소가 자신을 관리하는 경우에도 등록 원격 base의 runtime 선택, 이후 정상 원격 전진과 중단된 Agent 작업 재개가 서로를 오인해 반복 차단하지 않는다.
- Agent 접근 권한은 sandbox가 담당하고 Manager는 실제 선택 runtime의 실행·복구 조건을 확인한다.
- 기존 Git 오탐은 사용자 DB 수정 없이 자동 복구되며 다른 공유 Git 변화만으로 사용자 확인 요청을 만들지 않는다.

## Non-Goals

- `<state-dir>/runtimes/` 아래의 모든 경로나 모든 `git worktree` 변경을 신뢰하지 않는다.
- Agent에 worktree 등록·제거, fetch, branch, Git config 또는 다른 shared Git metadata 변경 권한을 부여하지 않는다.
- dirty, wrong-SHA, attached, 다른 repository 또는 소유권 불명의 runtime을 reset, clean, checkout, 삭제나 재등록으로 자동 수정하지 않는다.
- 일반 ExecPlan 관리 worktree의 생성·정리, 오래된 runtime garbage collection 또는 사용자 worktree 변경 허용 범위를 확대하지 않는다.
- 실제 sandbox 오류·검증 실패·독립 차단을 Git 오탐과 함께 해제하거나 감사 snapshot을 덮어쓰지 않는다.
- source checkout의 파일, index, `HEAD` 또는 local `main`을 runtime 선택에 맞춰 변경하지 않는다.

## Open Questions

- 없음
