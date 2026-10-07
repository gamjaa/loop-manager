# [R-020] Manager-Owned Runtime Worktree Scope Reconciliation

## Context

Loop Manager는 실행 중인 AI Agent가 허용 범위 밖의 파일, ref, Git config 또는 worktree 등록을 변경하지 않았는지 확인하기 위해 저장소 전체의 Git snapshot을 Agent 시작 전에 저장하고, 프로세스가 중단돼도 다음 실행에서 같은 기준선으로 검증을 재개한다. 이 경계는 실제 외부 변경과 Agent의 범위 위반을 차단하는 데 필요하다.

Loop Manager 저장소 자체를 등록 프로젝트로 운영하면 bootstrap도 같은 저장소의 shared Git metadata를 사용한다. `origin/main`이 새 SHA로 전진한 다음 실행에서 부모 bootstrap은 process lock 안에서 새 SHA의 detached runtime worktree를 등록한 뒤 runtime 자식을 시작한다. 이전 실행에서 저장된 Planner 또는 Worker scope가 남아 있으면 새 runtime 등록은 그 기준선에 없지만, 현재 runtime 자식은 이 변경이 Agent 호출 전에 부모가 만든 것이라는 출처를 전달받지 못한다. 그 결과 clean한 exact-SHA runtime도 Worker 또는 Planner가 만든 공유 Git 변경으로 오인돼 프로젝트가 반복적으로 차단될 수 있다.

process lock은 동시 실행을 막지만 이 귀속 문제를 해결하지 않는다. runtime 등록은 의도적으로 lock 안에서 persisted Agent scope 검증보다 먼저 발생하기 때문이다. 반대로 state directory 아래의 모든 worktree 변경을 무조건 허용하면 실제 범위 위반이나 외부 변경을 숨기므로, Manager가 소유한 정확한 mutation만 증명하고 기준선에 반영하는 좁은 복구 경계가 필요하다.

## Requirement

Loop Manager는 부모 bootstrap이 현재 실행을 위해 생성하거나 재사용한 runtime worktree의 identity와 Git mutation을 runtime 자식에 검증 가능한 형태로 전달해야 한다. persisted Planner, Worker 또는 Reviewer scope를 검증하기 전에 runtime 자식은 해당 mutation이 process lock 아래에서 수행된 Manager 소유 작업이며, 현재 실행의 source repository, 등록된 `origin/<base-branch>`에서 선택한 SHA와 결정적 runtime 경로에 정확히 결합돼 있는지 확인해야 한다. 등록 base는 부모·자식·저장 scope·checkpoint에서 같은 값으로 해석하며 `main`으로 고정하지 않는다.

runtime SHA는 현재 실행을 시작할 때 선택한 코드 revision이고, 이후 관측한 원격 base SHA는 관리 작업의 최신 입력이다. 선택 SHA B의 소유 runtime이 유효한 동안 추적 ref가 정상적으로 C로 전진해도 `origin/<base-branch> == B`를 계속 요구하지 않는다. 선택 당시의 원격 근거와 현재 실행 identity·정확한 runtime 상태를 검증하고 C의 전진은 R-023대로 별도 처리한다. 추적 ref 변경만으로 실행 중인 runtime을 C로 바꾸거나 새 handoff를 만들지 않으며, 실제 handoff는 R-023의 기존 조건을 따른다.

검증된 Manager runtime mutation만 이전 Agent snapshot의 기준선에 조정해 반영한다. runtime 등록이 Agent 실행 사이에 추가됐다는 사실만으로 저장된 Agent session, 결과 snapshot, 작업 diff, 재시도 이력 또는 현재 ExecPlan을 폐기하거나 `PLAN_BLOCKED`·`PROJECT_BLOCKED`로 전환해서는 안 된다. 조정 뒤에는 기존 Agent 범위 검사를 새 기준선에서 그대로 수행하고, runtime 외의 설명되지 않은 변경이 하나라도 있으면 자동 허용하지 않아야 한다.

Manager 소유 runtime으로 인정하려면 최소한 다음 조건을 모두 만족해야 한다.

- 경로가 현재 state directory와 source repository identity에서 계산한 `<state-dir>/runtimes/<repository-id>/<runtime-sha>/`의 정확한 경로다.
- worktree가 현재 source repository에 등록돼 있고 clean한 detached `HEAD`이며, `HEAD`와 예상 runtime SHA가 정확히 같다.
- 예상 SHA는 현재 bootstrap이 fetch한 등록 `origin/<base-branch>`에서 선택됐으며 그 선택 근거와 부모·자식이 공유하는 실행 identity에 결합돼 있다. 이후 추적 ref의 값만으로 선택 근거를 대체하지 않는다.
- 등록 또는 재사용이 동일한 process lock의 현재 실행 안에서 발생했음을 확인할 수 있고, 그 사실을 Agent의 자유 형식 출력이나 경로 이름만으로 추론하지 않는다.
- 기준선과 현재 상태 사이에서 R-023에 따라 실제 비교해야 하는 worktree catalog 차이가 검증된 runtime mutation으로 완전히 설명된다. 유효한 격리 증거에 따라 제외할 수 있는 무관한 변화와 확인된 정상 원격 전진은 R-023대로 구분하고, 사용 worktree·소유 branch·Git config·operation 또는 설명되지 않은 ref 변화가 함께 있으면 거부한다.

runtime 등록 전후에 프로세스가 중단돼도 다음 실행은 durable한 실행 identity와 실제 Git 상태를 대조해 동일한 Manager mutation을 멱등하게 완성하거나 기준선에 반영해야 한다. 이미 범위 위반으로 저장된 결과 snapshot이 현재 상태와 같더라도 그 snapshot을 무조건 신뢰해서는 안 되며, 저장된 시작 snapshot과의 비교 차이가 검증된 Manager runtime mutation과 R-023이 수용하는 원격 전진으로만 설명되는 경우에만 해당 false-positive 차단을 자동 해소하고 같은 Agent session과 작업 공간에서 재개해야 한다. 실제 위반과 다른 차단은 보존한다.

검증된 runtime 등록과 base·symbolic HEAD 전이는 원본을 보존한 비교 기준에만 반영한다. Agent 시작·결과·Manager checkpoint와 현재 snapshot을 같은 규칙으로 검사하고, 선검증에서 수용한 전이가 저장 결과 재검사나 checkpoint에서 다시 거부되지 않게 한다. 수락·거부 진단에는 선택 runtime SHA, 현재 base OID, 비교 snapshot identity, 관련 경로와 설명되지 않은 delta를 구분하며 비밀값은 노출하지 않는다.

예상 경로가 다른 repository에 속하거나 dirty, attached, 다른 SHA, 소유권 불명 또는 진행 중인 Git operation 상태이면 reset, clean, 재등록, snapshot 재설정 또는 범위 예외로 복구하지 않는다. 기존 bootstrap 안전 오류 또는 구조화된 Agent scope 위반으로 중단하고 사용자 상태를 보존한다. R-023의 증명된 비교 제외·원격 전진에 해당하지 않는 임의 worktree·branch·Git config 및 shared Git metadata 변경도 기존 정책대로 차단한다.

이 변경은 bootstrap이 이미 수행하도록 승인된 exact-SHA runtime 선택과 fetch의 귀속만 명시적으로 연결한다. Agent가 worktree 등록, commit, fetch, rebase, push 또는 config 변경을 수행할 권한을 추가하지 않으며, source checkout이나 활성 runtime의 파일·index·`HEAD`를 변경할 권한도 넓히지 않는다.

## Acceptance Criteria

- self-hosted Loop Manager 저장소에서 SHA A의 persisted Planner, Worker 또는 Reviewer scope가 남아 있고 등록 `origin/<base-branch>`가 SHA B로 전진한 경우, 다음 bootstrap이 exact-SHA runtime B를 등록한 뒤 runtime 자식이 이를 Agent 범위 위반으로 귀속하지 않고 저장된 단계부터 재개한다. `main`·`develop`과 `origin/main` 부재·다른 SHA를 각각 확인한다.
- B 선택 뒤 선검증·저장 재개·checkpoint 검사 전에 외부 fetch/push로 base가 정상적으로 C로 전진해도, 유효한 runtime B와 선택 근거는 유지되고 C는 R-023의 관리 rebase·재검증으로 처리된다. 단순 tracking ref 불일치 때문에 runtime 기준선 오류나 새 handoff를 만들지 않는다.
- 파생 `origin/HEAD` OID 전진과 증명된 최초 생성은 R-023대로 검사 경로 전반에서 일관되게 수용한다. target 변조·삭제·OID 불일치, 증거 부족과 실제 범위 위반 혼합은 거부하며 원본 snapshot·실패 감사를 보존한다.
- 정상 재개 시 기존 Agent session ID, 관리 worktree, Worker diff, 결과 snapshot, retry·failure history와 ExecPlan 상태가 보존되며 runtime 등록만으로 `PLAN_BLOCKED`, `PROJECT_BLOCKED` 또는 새 Agent session을 만들지 않는다.
- runtime B가 이미 정확한 clean detached worktree로 등록돼 있으면 다음 실행은 기준선 조정과 runtime 선택을 멱등한 no-op으로 처리하고 중복 등록이나 반복 차단을 만들지 않는다.
- 허용되는 worktree catalog 변화는 현재 실행 identity가 지정한 정확한 runtime 경로 하나의 등록 또는 검증된 재사용으로 제한된다. state directory 아래의 다른 경로와 과거·미래 SHA 경로를 prefix만으로 허용하지 않는다.
- runtime 경로, repository identity, detached 상태, clean 상태와 `HEAD == runtime SHA`를 실제 Git 상태로 검증한다. 경로 이름, 저장된 문자열 또는 Agent 응답만으로 소유권을 인정하지 않는다.
- bootstrap의 runtime 준비와 persisted Agent scope 검증 사이에 프로세스가 중단돼도 다음 실행은 기록된 의도와 실제 worktree catalog를 대조해 동일 mutation을 한 번만 반영하고 저장된 작업을 재개한다.
- 저장된 범위 오류의 시작 snapshot과 결과 snapshot 사이 차이가 검증된 Manager runtime 등록뿐이고 결과 snapshot이 현재 Git 상태와 일치하면 사용자 SQLite 수정 없이 false-positive 차단을 자동 해소한다.
- 결과 snapshot에 R-023으로 설명되지 않는 worktree, local branch, Git config, operation state 또는 허용되지 않은 ref 변경이 함께 있으면 Manager runtime 등록 부분만을 이유로 전체 결과를 신뢰하거나 기준선을 재설정하지 않고 기존 범위 위반을 유지한다.
- 예상 runtime이 dirty, attached, 다른 SHA, 다른 repository 또는 ownership 불명 상태이면 reset, clean, 강제 checkout, 제거나 재등록 없이 진단 가능한 오류로 중단하고 기존 Agent session과 작업 공간을 보존한다.
- 유효한 호출 격리에서 사용하지 않는 외부 worktree의 등록·변경은 R-023의 비교 제외를 따른다. 격리 증거 없는 설명되지 않은 worktree 변화와 Agent의 worktree 등록·branch·Git config 쓰기는 계속 차단된다.
- runtime 귀속 조정 전후의 snapshot identity, 허용한 exact path와 SHA 및 거부한 설명되지 않은 delta를 비밀값 없이 진단 로그에 남긴다.
- 자동 테스트는 persisted scope를 SHA A에서 저장한 뒤 같은 state directory의 실제 bootstrap 경로로 등록 `origin/<base-branch>` SHA B를 선택하고 runtime B를 등록해 runtime 자식의 선검증과 단계 재개까지 통과시킨다. B 선택 후 정상 외부 전진·HEAD 변화를 더해 저장 결과·checkpoint까지 검사하며, scope 검증 함수에 허용 경로를 직접 주입하는 단위 테스트만으로 이 기준을 충족한 것으로 보지 않는다.
- 자동 테스트는 Planner, Worker와 Reviewer의 persisted scope, 같은 SHA runtime 재사용, 등록 직후 중단, 기존 false-positive 결과 복구, 임의 worktree 추가, runtime dirty·attached·wrong-SHA·wrong-repository, runtime과 다른 shared Git 변경이 동시에 존재하는 경우를 검증한다.
- 전체 verify에서 기존 Agent scope 위반 차단, runtime bootstrap 격리, 기본 worktree 보존, process lock과 중단 재개 테스트가 유지된다.

## Product Impact

- Loop Manager 저장소가 자신을 관리하는 경우에도 등록 원격 base의 runtime 선택, 이후 정상 원격 전진과 중단된 Agent 작업 재개가 서로를 오인해 반복 차단하지 않는다.
- Manager가 만든 Git mutation과 Agent 또는 외부 프로세스의 mutation을 명시적으로 구분하면서 기존 Agent 격리 경계를 유지한다.
- 검증 가능한 false positive는 사용자 DB 수정 없이 자동 복구되고, 설명할 수 없는 shared Git 변화는 계속 사용자 확인 대상으로 남는다.

## Non-Goals

- `<state-dir>/runtimes/` 아래의 모든 경로나 모든 `git worktree` 변경을 신뢰하지 않는다.
- Agent에 worktree 등록·제거, fetch, branch, Git config 또는 다른 shared Git metadata 변경 권한을 부여하지 않는다.
- dirty, wrong-SHA, attached, 다른 repository 또는 소유권 불명의 runtime을 reset, clean, checkout, 삭제나 재등록으로 자동 수정하지 않는다.
- 일반 ExecPlan 관리 worktree의 생성·정리, 오래된 runtime garbage collection 또는 사용자 worktree 변경 허용 범위를 확대하지 않는다.
- 실제 Agent 범위 위반이 섞인 결과 snapshot을 false positive로 재분류하거나 전체 snapshot으로 기준선을 덮어쓰지 않는다.
- source checkout의 파일, index, `HEAD` 또는 local `main`을 runtime 선택에 맞춰 변경하지 않는다.

## Open Questions

- 없음
