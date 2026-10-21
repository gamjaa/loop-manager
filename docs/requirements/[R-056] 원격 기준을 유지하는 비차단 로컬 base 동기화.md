# [R-056] 원격 기준을 유지하는 비차단 로컬 base 동기화

## Summary

자동 planning·구현·runtime은 검증한 원격 OID를 사용하고 로컬 base 동기화는 별도 부수 처리로 둔다. 단계 경계의 runtime 갱신은 R-023을 따른다. equal은 유지, ahead는 보존·skip, behind는 변경 보존 가능한 FF-only, clean diverged는 안전한 rebase, dirty diverged는 skip하며 실패가 원격 기준 작업을 막지 않게 한다. 사용자 operation·파일·index를 보호하고 Manager 소유 rebase만 확인된 조건에서 복구한다. 작업의 실행·관리 한도와 배정은 R-091을 따르며 공유 Git 자원 보호는 유지한다.

## Context

현재 기본 worktree 동기화와 bootstrap은 원격 revision 선택을 로컬 상태와 분리하며, 안전한 fast-forward와 clean diverged의 로컬 commit rebase를 지원한다. 이미 구현된 기능을 다시 만들지 않고 아래 상태별 처리와 보존 조건의 누락을 확인한다.

로컬 base의 ahead·behind·diverged 상태를 명확하게 처리하고, 로컬 동기화의 실패가 원격 기준 작업의 중단으로 확대되지 않게 한다. 이 Requirement는 기본 checkout에서 rebase하지 않던 PRODUCT·ARCHITECTURE 및 R-051의 제한을 아래의 로컬 base 동기화에 한해서 변경한다. R-023의 source bootstrap HEAD 일치·로컬 갱신 성공 gate도 원격 실행 revision 검증으로 대체한다. 관리 작업의 원격 반영·재검증은 R-051과 R-060을 따르며, 아래 로컬 동기화는 그 작업의 시작·복구 조건이 아닌 별도의 부수 처리다.

## Requirement

### 원격 기준과 로컬 처리

계획 입력, 관리 작업 공간의 시작·rebase 기준과 catalog는 검증한 최신 `origin/<base-branch>`의 immutable OID를 사용한다. 실행 중 runtime 파일은 선택 revision으로 유지하며, 단계 경계의 원격 변경 확인과 자동 갱신·재시작은 R-023을 따른다. 로컬 base의 전용 commit, 미커밋 문서·코드와 파일을 자동 작업 또는 실행 코드의 입력으로 사용하지 않는다.

로컬 base와 기본 checkout의 상태 변화는 입력·Agent 범위·차단 해제의 비교 조건에서 제외한다. R-060에 따라 원격 base와 원격·로컬 관리 작업 branch의 현재 상태로 작업을 판단하며, 과거 snapshot·차단 기록 때문에 기본 checkout을 이전 상태로 복원하도록 요구하지 않는다.

등록된 base branch가 기본 worktree에 checkout돼 있고 안전 조건을 만족하면 fetch된 원격 기준으로 다음과 같이 처리한다.

- **equal**: 동기화할 내용이 없으므로 그대로 진행한다.
- **ahead-only**: 로컬 commit과 checkout을 보존하고 동기화를 건너뛴 뒤 원격 기준 작업을 계속한다. 로컬 commit을 자동 push하거나 자동 작업에 포함하지 않는다.
- **behind-only**: clean·dirty 모두 `pull --ff-only`에 해당하는 동기화를 시도한다. 이미 fetch한 원격 OID를 적용해 중복 fetch를 피할 수 있다. staged·unstaged·untracked·ignored 파일과 index를 보존할 수 없거나 실제 경로 충돌이 있으면 동기화만 건너뛴다.
- **clean diverged**: 로컬 전용 commit의 보존과 소유권을 확인해 최신 원격 base 위로 안전한 rebase를 시도한다. 자동 게시하지 않으며 결과의 로컬 HEAD도 자동 작업 기준으로 채택하지 않는다.
- **dirty diverged**: FF-only로는 통합할 수 없으므로 현재 HEAD·index·파일을 보존하고 동기화만 건너뛴다. dirty 상태에서는 rebase하지 않는다.

다른 branch·detached checkout을 base로 강제 전환하지 않는다. 해당 base가 다른 사용자 worktree에서 사용 중이면 그 작업 공간을 자동 수정하지 않는다. 이런 로컬 사정은 동기화 skip 사유로 기록하고 원격 기준 작업을 계속한다.

### 로컬 작업 보존과 실패

- rebase는 정상 index, 진행 중인 Git operation 없음, 작업 중인 다른 프로세스·사용자 변경과 충돌 없음 및 로컬 작업 보존을 확인한 경우에만 시작한다. 미커밋·staged 변경을 자동 stash하거나 사용자 파일을 삭제해 실행 조건을 만들지 않는다. 안전하게 처리할 수 없으면 로컬 상태를 보존하고 동기화만 건너뛴다.
- 로컬 전용 commit의 작업을 잃거나 임의로 충돌 내용을 선택하지 않는다. rebase 충돌·실패는 Manager가 시작했고 이후 사용자 변경이 없음을 확인한 경우 `rebase --abort`로 취소한 뒤 시작 전 HEAD·index·tracked 파일의 보존을 검증한다. 그 근거가 없으면 현재 파일·operation·진단을 보존한다. 어느 경우든 로컬 동기화 실패만으로 원격 기준 계획·구현을 차단하지 않는다.
- 동기화 직전·직후와 중단 뒤 재시작에서 실제 branch·HEAD·index·원격 OID를 확인한다. 사용자 Git operation이나 동시 변경을 Manager 작업으로 간주해 abort·reset하거나 덮어쓰지 않는다.
- 로컬 동기화의 skip·실패·충돌·정리 보류는 이유와 보존 상태를 기록하고 자동 작업 결과와 구분한다. 동일한 미해결 로컬 상태에 무한 rebase·복구를 반복하지 않는다.
- bootstrap, 자체 runtime 갱신·handoff, 일반 프로젝트 처리와 차단 복구에도 같은 원격 기준을 적용한다. bootstrap은 source checkout의 동기화 실패나 ahead·diverged 때문에 검증된 원격 runtime 실행을 막지 않는다.
- fetch 실패나 원격 삭제·재작성·소유권 불확실성 때문에 안전한 원격 기준 자체를 검증할 수 없으면 기존 재시도·보존 정책을 따른다. 로컬 HEAD 또는 오래된 입력으로 대체해 게시하지 않으며 다른 프로젝트까지 불필요하게 중단하지 않는다.
- 작업의 실행·관리 한도와 배정 순서는 [R-091](%5BR-091%5D%20프로젝트%20공정%20배정과%20우선순위%20기반%20병렬%20작업.md)을 따른다. 프로젝트 전체의 단일 변경 실행은 요구하지 않으며, 같은 checkout·index·ref를 사용하는 Git 작업의 소유권·배타 조건은 유지한다.

## Acceptance Criteria

- equal·ahead-only·behind-only·diverged 각각이 no-op·보존 후 skip·안전한 fast-forward·안전한 rebase로 처리되고, 네 경우 모두 자동 작업은 같은 검증된 원격 SHA를 사용한다.
- diverged의 성공한 rebase가 로컬 전용 작업을 보존하고 원격을 덮어쓰지 않는다. rebase 후 로컬 전용 commit도 Planner·Worker와 자체 runtime 입력에 포함되지 않는다.
- rebase 충돌·lock 오류·fast-forward 실패·다른 branch·dirty 상태·동시 사용자 변경은 로컬 동기화만 실패 또는 skip으로 기록하며, 사용 가능한 원격 기준의 계획·구현·PR 관측을 막지 않는다.
- behind 상태에서 기존의 disjoint 로컬 변경과 staged·ignored 파일 보존 조건이 유지된다. 겹치는 파일이나 비정상 index를 삭제·reset·stash로 처리하지 않는다.
- Manager가 시작한 실패 rebase만 확인된 조건에서 복구하고 사용자 operation·작업물을 훼손하지 않는다. 중단·재시작에서 진행 상태를 대조해 중복 rebase와 잘못된 abort를 방지한다.
- source checkout이 ahead·diverged이거나 동기화가 실패해도 bootstrap은 최신 원격 SHA의 관리 runtime을 실행할 수 있다. 자체 handoff도 로컬 동기화 성공을 원격 revision 채택 조건으로 요구하지 않는다.
- 기본 checkout의 코드·문서·index·HEAD·로컬 base ref가 작업 중 또는 차단 중에 바뀌어도 관리 작업의 입력·범위 위반·복구 조건을 바꾸지 않는다. 격리 증거 없는 legacy 실행에도 같은 비교 제외를 적용하며 과거 로컬 base 복원을 요구하지 않는다. 실제 Agent 위반의 보존과 선택 작업 재검증은 R-055를 따른다.
- dirty behind의 안전한 FF-only·실패 시 보존, dirty diverged의 rebase 금지, clean diverged의 성공 및 Manager 소유 충돌 abort 뒤 시작 상태 보존을 구분해 검증한다. 사용자 operation은 abort하지 않는다.
- 로그에서 사용한 원격 SHA, 로컬 관계, 수행·skip·실패·복구 이유를 구분할 수 있다. fetch 실패와 원격 기준 불확실성을 로컬 동기화 실패와 혼동하지 않는다.
- 실행·관리 한도와 배정은 R-091을 따르고, 기존 안전·승인·lease·검증·Review·공유 Git 자원 보호와 자동 Merge 금지 조건이 유지된다.

## Product Impact

- 안전한 로컬 base 분기는 원격 위로 rebase할 수 있고, 로컬 동기화 실패는 자동 작업의 차단 조건이 아니다.
- bootstrap과 자체 실행까지 로컬 checkout과 원격 작업 기준을 분리한다.

## Non-Goals

- 로컬 전용 commit의 자동 push, 사용자 변경의 자동 stash·삭제 또는 다른 checkout의 강제 전환을 수행하지 않는다.
- 충돌 내용을 AI가 임의로 해결하거나 원격 base를 force push하지 않는다.
- 실제 원격·소유권·범위 불확실성 또는 최종 검증 실패를 무시하지 않는다.

## Open Questions

- 없음
