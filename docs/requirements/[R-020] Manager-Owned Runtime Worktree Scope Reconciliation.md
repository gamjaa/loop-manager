# [R-020] Manager-Owned Runtime Worktree Scope Reconciliation

## Context

Loop Manager는 실행 중인 AI Agent가 허용 범위 밖의 파일, ref, Git config 또는 worktree 등록을 변경하지 않았는지 확인하기 위해 저장소 전체의 Git snapshot을 Agent 시작 전에 저장하고, 프로세스가 중단돼도 다음 실행에서 같은 기준선으로 검증을 재개한다. 이 경계는 실제 외부 변경과 Agent의 범위 위반을 차단하는 데 필요하다.

Loop Manager 저장소 자체를 등록 프로젝트로 운영하면 bootstrap도 같은 저장소의 shared Git metadata를 사용한다. `origin/main`이 새 SHA로 전진한 다음 실행에서 부모 bootstrap은 process lock 안에서 새 SHA의 detached runtime worktree를 등록한 뒤 runtime 자식을 시작한다. 이전 실행에서 저장된 Planner 또는 Worker scope가 남아 있으면 새 runtime 등록은 그 기준선에 없지만, 현재 runtime 자식은 이 변경이 Agent 호출 전에 부모가 만든 것이라는 출처를 전달받지 못한다. 그 결과 clean한 exact-SHA runtime도 Worker 또는 Planner가 만든 공유 Git 변경으로 오인돼 프로젝트가 반복적으로 차단될 수 있다.

process lock은 동시 실행을 막지만 이 귀속 문제를 해결하지 않는다. runtime 등록은 의도적으로 lock 안에서 persisted Agent scope 검증보다 먼저 발생하기 때문이다. 반대로 state directory 아래의 모든 worktree 변경을 무조건 허용하면 실제 범위 위반이나 외부 변경을 숨기므로, Manager가 소유한 정확한 mutation만 증명하고 기준선에 반영하는 좁은 복구 경계가 필요하다.

G-052 도입 전의 부모 bootstrap은 runtime 자식에 `--runtime-source`와 `--runtime-sha`만 전달한다. 실행별 mutation identity를 기록하지 않고 기존 clean detached runtime도 재사용하므로, 신버전 자식은 이 인자와 Git 상태만으로 부모가 직접 등록한 runtime과 같은 경로의 외부 등록을 구분할 수 없다. 사용자는 이 구버전 부모에서 신버전 자식으로 넘어가는 동안 아래의 제한적인 신뢰 예외를 허용한다.

## Requirement

Loop Manager는 부모 bootstrap이 현재 실행을 위해 생성하거나 재사용한 runtime worktree의 identity와 Git mutation을 runtime 자식에 검증 가능한 형태로 전달해야 한다. 구버전 전환 예외를 제외하고, persisted Planner, Worker 또는 Reviewer scope를 검증하기 전에 runtime 자식은 해당 mutation이 process lock 아래에서 수행된 Manager 소유 작업이며, 현재 실행의 source repository, 선택된 `origin/main` SHA와 결정적 runtime 경로에 정확히 결합돼 있는지 확인해야 한다.

검증된 Manager runtime mutation과 아래에서 명시적으로 허용한 구버전 전환 runtime만 이전 Agent snapshot의 기준선에 조정해 반영한다. runtime 등록이 Agent 실행 사이에 추가됐다는 사실만으로 저장된 Agent session, 결과 snapshot, 작업 diff, 재시도 이력 또는 현재 ExecPlan을 폐기하거나 `PLAN_BLOCKED`·`PROJECT_BLOCKED`로 전환해서는 안 된다. 조정 뒤에는 기존 Agent 범위 검사를 새 기준선에서 그대로 수행하고, runtime 외의 설명되지 않은 변경이 하나라도 있으면 자동 허용하지 않아야 한다.

실행별 mutation identity를 전달하는 일반 경로에서 Manager 소유 runtime으로 인정하려면 최소한 다음 조건을 모두 만족해야 한다.

- 경로가 현재 state directory와 source repository identity에서 계산한 `<state-dir>/runtimes/<repository-id>/<runtime-sha>/`의 정확한 경로다.
- worktree가 현재 source repository에 등록돼 있고 clean한 detached `HEAD`이며, `HEAD`와 예상 runtime SHA가 정확히 같다.
- 예상 SHA는 현재 bootstrap이 fetch한 `origin/main`에서 선택됐으며 부모와 자식이 공유하는 실행 identity에 결합돼 있다.
- 등록 또는 재사용이 동일한 process lock의 현재 실행 안에서 발생했음을 확인할 수 있고, 그 사실을 Agent의 자유 형식 출력이나 경로 이름만으로 추론하지 않는다.
- 기준선과 현재 상태 사이의 worktree catalog 차이가 검증된 runtime mutation으로 완전히 설명된다. 별도 worktree, 소유 branch, Git config, 진행 중인 Git operation 또는 허용되지 않은 ref 변화가 함께 있지 않다.

구버전 전환 예외는 부모가 보낸 `--runtime-source`와 `--runtime-sha`는 있지만 `--runtime-mutation-id`가 없는 runtime 자식 호출에만 적용한다. 자식은 구버전 형식의 부모 process lock이 실제로 유지되는지 확인하고, 전달된 source repository와 현재 `origin/main` SHA, 결정적 runtime 경로, 등록된 같은 repository의 clean detached `HEAD`, 나머지 worktree catalog와 공유 Git 상태를 대조한다. 기대된 `origin/main` 이동과 정확한 runtime 한 항목 외의 Git 변화는 허용하지 않는다. 인자 누락만으로 소유권이 증명됐다고 주장하지 않으며, 이 조건을 만족한 runtime을 전환 기간의 정책적 신뢰 대상으로 받아들인다. 구버전 부모가 이미 존재하던 같은 경로의 외부 등록을 재사용했을 가능성은 이 증거만으로 배제할 수 없으며, 사용자는 이 제한된 불확실성을 명시적으로 수용한다.

mutation identity가 전달된 호출에는 이 예외를 적용하지 않고 기존의 완전한 소유 증명을 요구한다. 구버전 lock이 없거나 부모의 인자가 일부만 전달된 경우, 경로·repository·SHA·clean·detached 상태가 다르거나 다른 설명되지 않은 Git 변경이 섞인 경우에도 기존 안전 오류 또는 범위 위반으로 중단한다. 이 예외를 일반 runtime 재사용, 임의의 worktree 경로 또는 Agent의 Git 변경 권한으로 확대하지 않는다.

runtime 등록 전후에 프로세스가 중단돼도 다음 실행은 일반 경로의 durable한 실행 identity 또는 구버전 전환 예외의 증거를 실제 Git 상태와 대조해 동일한 runtime 변화를 멱등하게 완성하거나 기준선에 반영해야 한다. 이미 범위 위반으로 저장된 결과 snapshot이 현재 상태와 같더라도 그 snapshot을 무조건 신뢰해서는 안 되며, 저장된 시작 snapshot과의 차이가 검증된 Manager runtime mutation 또는 허용된 구버전 전환 runtime으로만 구성된 경우에만 false-positive 차단을 자동 해소하고 같은 Agent session과 작업 공간에서 재개해야 한다.

예상 경로가 다른 repository에 속하거나 dirty, attached, 다른 SHA, 구버전 전환 예외에도 해당하지 않는 소유권 불명 또는 진행 중인 Git operation 상태이면 reset, clean, 재등록, snapshot 재설정 또는 범위 예외로 복구하지 않는다. 기존 bootstrap 안전 오류 또는 구조화된 Agent scope 위반으로 중단하고 사용자 상태를 보존한다. 임의로 추가·제거된 worktree와 branch, Git config 및 Agent가 소유하지 않은 shared Git metadata 변경도 기존 정책대로 차단한다.

이 변경은 bootstrap이 이미 수행하도록 승인된 exact-SHA runtime 선택과 fetch의 귀속만 명시적으로 연결한다. Agent가 worktree 등록, commit, fetch, rebase, push 또는 config 변경을 수행할 권한을 추가하지 않으며, source checkout이나 활성 runtime의 파일·index·`HEAD`를 변경할 권한도 넓히지 않는다.

## Acceptance Criteria

- self-hosted Loop Manager 저장소에서 SHA A의 persisted Planner, Worker 또는 Reviewer scope가 남아 있고 `origin/main`이 SHA B로 전진한 경우, 다음 bootstrap이 exact-SHA runtime B를 등록한 뒤 runtime 자식이 이를 Agent 범위 위반으로 귀속하지 않고 저장된 단계부터 재개한다.
- 구버전 부모가 source·SHA 인자만 전달하고 실행 중인 구버전 process lock 아래에서 신버전 runtime 자식을 시작한 첫 전환에서도, 자식은 정확한 repository·`origin/main` SHA·결정적 경로·clean detached 상태와 다른 Git 변경 부재를 확인한 뒤 같은 저장 단계부터 재개한다. 같은 SHA의 검증된 재사용과 중단 후 재실행도 중복 등록 없이 처리한다.
- mutation ID가 빠졌다는 이유만으로 새 형식 부모나 불완전한 호출을 구버전으로 단정하지 않는다. 구버전 lock 부재, 잘못된 source·SHA·경로·repository, dirty·attached runtime 또는 혼합 Git 변경은 전환 예외로 허용하지 않는다. mutation ID가 전달된 호출은 기존 소유 증명 검사를 계속 통과해야 한다.
- 정상 재개 시 기존 Agent session ID, 관리 worktree, Worker diff, 결과 snapshot, retry·failure history와 ExecPlan 상태가 보존되며 runtime 등록만으로 `PLAN_BLOCKED`, `PROJECT_BLOCKED` 또는 새 Agent session을 만들지 않는다.
- runtime B가 이미 정확한 clean detached worktree로 등록돼 있으면 다음 실행은 기준선 조정과 runtime 선택을 멱등한 no-op으로 처리하고 중복 등록이나 반복 차단을 만들지 않는다.
- 허용되는 worktree catalog 변화는 현재 실행 identity 또는 명시적 구버전 전환 예외가 지정한 정확한 runtime 경로 하나의 등록 또는 검증된 재사용으로 제한된다. state directory 아래의 다른 경로와 과거·미래 SHA 경로를 prefix만으로 허용하지 않는다.
- runtime 경로, repository identity, detached 상태, clean 상태와 `HEAD == runtime SHA`를 실제 Git 상태로 검증한다. 경로 이름, 저장된 문자열 또는 Agent 응답만으로 소유권을 인정하지 않는다.
- bootstrap의 runtime 준비와 persisted Agent scope 검증 사이에 프로세스가 중단돼도 다음 실행은 일반 경로의 기록된 의도 또는 구버전 전환 예외의 증거를 실제 worktree catalog와 대조해 동일한 runtime 변화를 한 번만 반영하고 저장된 작업을 재개한다.
- 저장된 범위 오류의 시작 snapshot과 결과 snapshot 사이 차이가 검증된 Manager runtime 등록 또는 허용된 구버전 전환 runtime뿐이고 결과 snapshot이 현재 Git 상태와 일치하면 사용자 SQLite 수정 없이 false-positive 차단을 자동 해소한다.
- 결과 snapshot에 임의 worktree, local branch, Git config, operation state 또는 허용되지 않은 ref 변경이 함께 있으면 Manager runtime 등록 부분만을 이유로 전체 결과를 신뢰하거나 기준선을 재설정하지 않고 기존 범위 위반을 유지한다.
- 예상 runtime이 dirty, attached, 다른 SHA, 다른 repository 또는 구버전 전환 예외에도 해당하지 않는 ownership 불명 상태이면 reset, clean, 강제 checkout, 제거나 재등록 없이 진단 가능한 오류로 중단하고 기존 Agent session과 작업 공간을 보존한다.
- 사용자가 별도 `git worktree add`를 수행하거나 Agent가 worktree 등록·branch·Git config를 변경하는 기존 위반 사례는 계속 차단된다.
- runtime 귀속 조정 전후의 snapshot identity, 허용한 exact path와 SHA 및 거부한 설명되지 않은 delta를 비밀값 없이 진단 로그에 남긴다.
- 자동 테스트는 persisted scope를 SHA A에서 저장한 뒤 같은 state directory의 실제 bootstrap 경로로 `origin/main` SHA B를 선택하고 runtime B를 등록해 runtime 자식의 선검증과 단계 재개까지 통과시킨다. 구버전 부모의 실제 인자·lock 계약에서 신버전 자식을 시작하는 전환도 검증한다. scope 검증 함수에 허용 경로를 직접 주입하는 단위 테스트만으로 이 기준을 충족한 것으로 보지 않는다.
- 자동 테스트는 Planner, Worker와 Reviewer의 persisted scope, 같은 SHA runtime 재사용, 등록 직후 중단, 기존 false-positive 결과 복구, 임의 worktree 추가, runtime dirty·attached·wrong-SHA·wrong-repository, runtime과 다른 shared Git 변경이 동시에 존재하는 경우를 검증한다.
- 전체 verify에서 기존 Agent scope 위반 차단, runtime bootstrap 격리, 기본 worktree 보존, process lock과 중단 재개 테스트가 유지된다.

## Product Impact

- Loop Manager 저장소가 자신을 관리하는 경우에도 새 `origin/main` SHA의 runtime 선택과 중단된 Agent 작업 재개가 서로를 오인해 반복 차단하지 않는다.
- 일반 경로에서는 Manager가 만든 Git mutation과 Agent 또는 외부 프로세스의 mutation을 명시적으로 구분한다. 구버전 전환에서는 승인된 정확한 runtime 한 항목에 한해 남는 출처 불확실성을 허용하고, 그 밖의 Agent 격리 경계는 유지한다.
- 검증 가능한 false positive는 사용자 DB 수정 없이 자동 복구되고, 설명할 수 없는 shared Git 변화는 계속 사용자 확인 대상으로 남는다.

## Non-Goals

- `<state-dir>/runtimes/` 아래의 모든 경로나 모든 `git worktree` 변경을 신뢰하지 않는다.
- Agent에 worktree 등록·제거, fetch, branch, Git config 또는 다른 shared Git metadata 변경 권한을 부여하지 않는다.
- dirty, wrong-SHA, attached, 다른 repository 또는 구버전 전환 예외에도 해당하지 않는 소유권 불명의 runtime을 reset, clean, checkout, 삭제나 재등록으로 자동 수정하지 않는다.
- 일반 ExecPlan 관리 worktree의 생성·정리, 오래된 runtime garbage collection 또는 사용자 worktree 변경 허용 범위를 확대하지 않는다.
- 실제 Agent 범위 위반이 섞인 결과 snapshot을 false positive로 재분류하거나 전체 snapshot으로 기준선을 덮어쓰지 않는다.
- 구버전 전환 예외를 mutation ID가 있는 호출이나 일반적인 소유권 불명 runtime으로 확대하지 않는다. 구버전 부모가 같은 정확한 runtime 경로의 외부 등록을 재사용했는지는 기존 증거만으로 구분할 수 없다는 승인된 한계는 유지한다.
- source checkout의 파일, index, `HEAD` 또는 local `main`을 runtime 선택에 맞춰 변경하지 않는다.

## Open Questions

- 없음
