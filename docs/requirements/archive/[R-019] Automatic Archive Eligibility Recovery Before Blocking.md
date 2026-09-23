# [R-019] Automatic Archive Eligibility Recovery Before Blocking

## Context

Requirement archive eligibility는 Git의 ExecPlan 파일과 SQLite의 ExecPlan 상태·source 관계가 모두 일치할 때만 완료된 Requirement를 안전하게 이동할 수 있다. 현재 구현은 이 관계를 확정할 수 없으면 즉시 현재 ExecPlan을 `BLOCKED`로 전환하고 사용자에게 알린다.

실행 중인 관리 branch가 최신 `origin/<base-branch>`보다 뒤처진 사이 Planner가 새 ExecPlan을 게시하면, SQLite에는 새 계획이 등록됐지만 기존 관리 revision에는 해당 파일이 아직 없을 수 있다. 이 불일치는 최신 base를 fetch하고 소유가 확인된 관리 branch에 반영한 뒤 eligibility를 다시 계산하면 자동으로 해소된다. 그런데도 즉시 사용자-visible BLOCKED와 알림을 만들면 사용자가 수정할 내용이 없는 일시적 상태를 수동 조치가 필요한 문제처럼 노출하고, 외부 스케줄러의 다음 실행이 스스로 복구할 수 있는 작업을 불필요하게 중단한다.

archive 안전성은 완화하지 않되, 자동으로 복구 가능한 최신-base 또는 catalog 시차와 실제로 사용자의 판단이나 외부 조치가 필요한 불확실성을 구분해야 한다.

전체 `BLOCKED` 전이 경로를 같은 기준으로 점검하면 archive 시차 밖에도 사용자가 고칠 수 없는 중간 실패를 즉시 차단하는 경우가 있다. rebase 충돌을 해결하는 Worker가 한 번 정상 종료하지 못한 경우, rebase의 stage·continue 또는 관리 worktree·기존 PR 복구 중 일시적인 Git 명령이 실패한 경우, 실제 소유 branch에서 다시 계산할 수 있는 `active_sha`·`validated_base_sha` checkpoint가 어긋난 경우다. 이 상태들은 보존된 worktree와 세션, 다음 외부 실행 또는 실제 Git 상태를 이용해 다시 시도할 수 있으므로 곧바로 사용자 조치로 바꾸면 안 된다.

반대로 guidance 승인, Merge 없이 닫힌 PR, 소유권을 증명할 수 없는 branch·worktree, Agent 범위 위반, 반복해도 같은 verify·Review·충돌 실패가 남는 경우와 rebase 뒤 게시할 변경이 없어 현재 완료 계약을 충족할 수 없는 경우는 자동 복구와 구분해야 한다. `BLOCKED`는 단순한 실행 실패가 아니라 안전한 자동 진행이 더 이상 불가능하고 사용자가 실제로 결정하거나 외부 상태를 바꿀 수 있다는 판정이어야 한다.

## Requirement

Loop Manager는 Requirement archive eligibility 불일치를 사용자-visible `BLOCKED`로 전환하기 전에 최신 원격 기준으로 자동 복구를 시도해야 한다.

이 원칙은 archive 판정뿐 아니라 모든 사용자-visible `BLOCKED` 후보에 적용한다. 프로그램은 자유 형식 오류 문구가 아니라 구조화된 원인 코드로 다음 범주를 구분해야 한다.

- `TRANSIENT_RETRY`: timeout, 일시적 서비스 오류, Git lock, fetch·상태 조회·stage·rebase continue·worktree 복구 명령의 일시 실패처럼 같은 보존 상태에서 다시 실행할 수 있는 오류
- `STATE_RECONCILE`: 최신 base와 관리 branch의 시차, 유효한 원격 ExecPlan 선언과 SQLite cache 차이, 중단된 단계의 checkpoint 또는 `active_sha`·`validated_base_sha` 차이처럼 신뢰할 수 있는 원본에서 다시 계산할 수 있는 상태
- `AGENT_CORRECTION`: archive 이동 집합, rebase 충돌과 같이 같은 Worker 세션에 결정적인 피드백을 전달해 수정할 수 있는 상태
- `USER_ACTION_REQUIRED`: 사용자 승인·판단, 닫힌 PR 재개, 소유권 또는 Agent 범위 위반, 외부에서 변경된 원격 branch, 반복해도 진전이 없는 실패나 안전한 원본으로 확정할 수 없는 손상

`TRANSIENT_RETRY`, `STATE_RECONCILE`와 아직 재시도 가능한 `AGENT_CORRECTION`은 ExecPlan을 `BLOCKED`로 바꾸거나 BLOCKED 알림을 생성하지 않아야 한다. 현재 phase, branch, worktree, Agent 세션과 검증 결과를 보존하고 같은 실행의 제한된 재시도 또는 다음 외부 스케줄 실행에서 계속해야 한다. 서로 다른 예외 종류를 하나의 catch에서 처리하더라도 일시적인 `RuntimeError`를 소유권·범위·게시 안전 위반과 같은 최종 차단으로 분류하지 않아야 한다.

자동 복구는 최신 원격과 PR 상태 갱신, R-017의 파일 원본 ExecPlan 선언과 SQLite cache 동기화, 소유가 확인된 실제 branch·worktree 상태에 따른 실행 checkpoint 재구성, 필요한 rebase·Worker correction 순서로 수행해야 한다. 선언 cache의 오래된 path, title, sources, dependencies 또는 revision은 최신 원격 catalog가 유효하면 자동 동기화하고 독립적인 현재 ExecPlan을 차단하지 않는다. 원격 revision 경쟁이나 transaction 전후 상태 변화가 있으면 부분 결과를 적용하지 않고 최신 snapshot에서 다시 시작한다.

현재 관리 revision에 SQLite 등록 ExecPlan이 없더라도, 저장된 plan path와 게시 revision이 유효하고 최신 `origin/<base-branch>`에서 같은 ExecPlan을 확인할 수 있으면 이를 최신-base 시차로 분류한다. 프로그램은 기존 worktree 소유권과 branch 안전 조건을 검증한 뒤 G-014의 최신-base 게시 흐름으로 관리 branch를 갱신하고, 동일한 Git revision에서 전체 ExecPlan catalog, SQLite 상태와 source 관계를 다시 대조해야 한다.

자동 복구 중에는 현재 ExecPlan, 관리 worktree, Worker 세션, 승인 기록과 구현 변경을 보존한다. 최신 base에 새로 추가된 ExecPlan이 현재 Requirement와 관련 없으면 기존 archive 후보를 유지할 수 있고, 관련이 있거나 상태 조건을 바꾸면 해당 Requirement를 archive 후보에서 제외한 뒤 같은 Worker 세션에서 실제 이동 집합을 최신 판정과 일치시켜야 한다.

rebase 충돌은 충돌 상태와 원래 구현을 보존하고 같은 Worker 세션에 전달한다. Worker 또는 Agent subprocess가 한 번 정상 종료하지 못했지만 worktree와 rebase 상태가 계속 안전하면 그 한 번의 실패만으로 차단하지 않고 기존 Agent 재시도 및 다음 외부 실행 정책으로 다시 시도한다. Worker가 결과를 반환한 뒤 stage 또는 `rebase --continue`가 Git lock이나 프로세스 중단 같은 일시적 이유로 실패해도 실제 rebase 상태를 다시 읽어 멱등하게 이어간다. 같은 충돌 지문과 같은 진전 없는 결과가 Worker correction 뒤 세 번 다시 발생하거나 rebase 상태가 안전하게 복구되지 않을 때만 최종 차단한다.

게시 단계의 `active_sha`, `validated_base_sha` 또는 저장 phase가 서로 다르더라도 관리 branch와 worktree 소유권이 확인되고 실제 `origin/<base-branch>` ancestry와 HEAD에서 안전한 다음 단계를 결정할 수 있으면 저장 checkpoint를 보정하고 rebase, 전체 verify와 Review를 다시 수행한다. push 뒤 PR 생성 전 중단, 원격 fetch 뒤 상태 저장 전 중단과 같이 이미 존재하는 원격 branch·PR을 조회해 재개할 수 있는 경우도 같은 원칙을 따른다. 실제 상태가 둘 이상의 상충하는 해석을 허용하거나 관측하지 않은 원격 변경을 덮어써야만 진행할 수 있으면 자동 보정하지 않는다.

최신 base가 복구 도중 다시 바뀌면 낡은 revision의 eligibility나 archive 이동을 게시하지 않고 새 원격 revision에서 다시 계산한다. 한 번의 프로그램 실행 안에서 안정된 최신 revision을 얻지 못하면 상태와 작업 공간을 보존한 채 종료하고 다음 외부 스케줄 실행에서 자동 재시도하며, 이 일시적 원격 경쟁만으로 BLOCKED나 사용자 알림을 만들지 않는다. 프로세스가 중단돼도 다음 실행은 저장된 branch와 worktree를 검증한 뒤 같은 복구를 멱등하게 재개해야 한다.

최신-base 시차와 그 자동 복구 과정은 ExecPlan을 `BLOCKED`로 전환하거나 BLOCKED 알림 이벤트를 만들지 않는다. 운영 로그에는 복구 원인, 이전 revision, 최신 base와 재계산 결과를 남길 수 있지만 사용자에게 수정할 파일을 요구하지 않는다.

다음과 같이 자동 복구 후에도 안전성을 확정할 수 없는 경우에만 현재 ExecPlan을 `BLOCKED`로 전환하고 사용자에게 알린다.

- 최신 원격에도 SQLite 등록 ExecPlan의 파일이나 유효한 게시 이력이 없다.
- 최신 원격 catalog와 파일 원본 cache 동기화 후에도 같은 ID의 대응, path, frontmatter, source 또는 dependency를 하나로 확정할 수 없다.
- ExecPlan 상태가 유효하지 않거나 source·archive 목적지 충돌이 남아 있다.
- 소유가 확인된 기존 복구 흐름과 제한된 Worker correction을 거쳐도 같은 rebase 충돌 또는 작업 공간 불일치를 해소할 수 없다.
- guidance 결정처럼 명시적인 사용자 승인이 필요하거나 PR이 Merge 없이 닫혀 같은 PR을 다시 열어야 한다.
- branch·worktree 소유권, Agent 변경 범위 또는 원격 branch OID가 불명확해 자동 변경이 사용자나 외부 작업을 덮어쓸 수 있다.
- 전체 verify나 Review의 동일 실패가 기존 진전 감지 한도만큼 반복된다.
- rebase 또는 Worker 결과에 게시할 변경이 남지 않아, 연결된 PR의 Merge로만 완료한다는 현재 계약 아래에서 ExecPlan 충족 여부를 프로그램이 확정할 수 없다.

최종 BLOCKED 알림에는 수행한 자동 복구, 끝내 확정할 수 없는 조건과 실제 사용자 조치가 가능한 대상만 포함해야 한다. 동일한 원인과 revision에 대한 복구 시도 또는 프로세스 재실행이 중복 BLOCKED 알림을 만들면 안 된다.

## Acceptance Criteria

- 관리 branch가 뒤처진 동안 새 ExecPlan이 base branch에 게시·등록돼 발생한 Git/SQLite catalog 불일치는 최신 base를 반영한 뒤 자동으로 재검증된다.
- 최신 base에서 누락 계획과 SQLite의 path, ID, sources, dependencies 및 상태를 확인할 수 있으면 현재 ExecPlan을 BLOCKED로 전환하지 않고 BLOCKED 알림도 생성하지 않는다.
- 최신 원격 ExecPlan catalog가 유효하면 오래된 SQLite 선언 cache를 동기화하고, cache 차이만으로 현재 또는 독립적인 ExecPlan을 BLOCKED로 전환하지 않는다.
- 새 계획이 archive 대상 Requirement와 관련 없으면 기존 후보를 유지하고, 관련이 있거나 상태 조건을 바꾸면 후보에서 제외한다.
- 자동 복구는 현재 관리 worktree, branch, 구현 변경, Worker 세션과 guidance 승인 기록을 보존한다.
- 최신-base 반영으로 archive 후보 또는 실제 Requirement 이동 집합이 달라지면 같은 Worker 세션에서 최신 판정에 맞게 수정한 뒤 verify와 Review를 다시 수행한다.
- 복구 중 원격 base가 다시 변경되면 낡은 eligibility 결과를 사용하거나 archive 이동을 게시하지 않고 최신 revision에서 다시 계산한다.
- 한 프로그램 실행에서 원격 경쟁이 계속되면 현재 상태를 보존하고 다음 외부 스케줄 실행에서 자동 재시도하며, 그 사실만으로 BLOCKED나 사용자 알림을 만들지 않는다.
- Git lock, timeout, 일시적 서비스 오류와 재시도 가능한 fetch·상태 조회·stage·rebase continue·worktree 복구 실패는 현재 상태를 보존하고 기존 제한 안에서 다시 시도하며 BLOCKED 알림을 만들지 않는다.
- worktree와 rebase 상태가 안전하게 보존된 경우 충돌 해결 Worker의 단일 비정상 종료는 BLOCKED로 전환하지 않고 같은 세션 또는 기존 session fallback으로 다시 시도한다.
- Worker correction 뒤 같은 rebase 충돌 지문과 진전 없는 결과가 세 번 다시 발생할 때만 원인과 보존 상태를 포함해 BLOCKED로 전환한다.
- 소유 branch의 실제 HEAD와 최신 base ancestry로 실행 위치를 하나로 확정할 수 있으면 `active_sha`, `validated_base_sha` 또는 phase checkpoint 불일치를 보정하고 전체 verify와 Review가 필요한 단계부터 자동 재개한다.
- push 또는 PR 생성 경계에서 중단돼도 기존 원격 branch와 동일 head PR을 먼저 조회하고, 확인 가능한 게시 결과를 중복 push나 중복 PR 없이 복구한다.
- 자동 복구 도중 프로세스가 중단돼도 다음 실행에서 중복 commit, 중복 알림, force push, reset, stash 또는 clean 없이 재개된다.
- 자동 복구가 가능한 시차는 검증·Review 재시도 실패나 일반 BLOCKED 해제용 사용자 문서 변경으로 처리하지 않는다.
- 최신 원격과 저장된 게시 이력을 확인한 뒤에도 계획 파일 누락, Git/SQLite 선언 불일치, 유효하지 않은 상태, archive 충돌 또는 해결되지 않은 rebase·작업 공간 문제가 남을 때만 BLOCKED로 전환한다.
- guidance 승인, 닫힌 PR, 소유권·Agent 범위·외부 원격 OID 불확실성, 동일 verify·Review 실패 반복과 의미를 자동 확정할 수 없는 무변경 결과는 최종 사용자 조치 대상으로 유지한다.
- 최종 BLOCKED 알림은 자동 복구 시도와 실패 지점, 사용자가 실제로 조치할 수 있는 경로를 포함한다.
- 일시 오류나 다음 실행에서 자동 재시도할 상태에는 `Files to edit`를 추측해 표시하지 않는다.
- 동일 원인과 검증 revision에 대해서는 사용자-visible BLOCKED 알림을 한 번만 생성한다.
- stale branch에 새 비관련 계획이 추가된 경우, 새 관련 계획이 추가된 경우, 선언 cache 시차, 연속 base 변경, checkpoint 불일치, 일시적인 Git 명령 실패, 충돌 해결 Worker 단일 실패와 반복 무진전, push·PR 생성 중단·재개, 지속되는 catalog 불일치, archive 충돌과 해결 불가능한 rebase를 자동 테스트로 검증한다.

## Product Impact

- 최신-base 동기화로 해소할 수 있는 archive catalog 시차는 사용자 개입 없이 자동으로 복구된다.
- 일시적인 도구 실패, 재구성 가능한 실행 checkpoint와 아직 수정 가능한 rebase 충돌은 사용자 BLOCKED로 노출되지 않고 보존 상태에서 자동 재개된다.
- 사용자는 자동 복구가 끝내 실패해 실제 판단이나 외부 조치가 필요한 경우에만 BLOCKED 알림을 받는다.
- Requirement archive는 계속 Git과 SQLite의 source 관계 및 상태를 확정한 경우에만 수행된다.

## Non-Goals

- Git/SQLite catalog 불일치를 무시하거나 SQLite 값만 신뢰해 Requirement를 archive하지 않는다.
- 안전성이 불확실한 Requirement를 임의로 완료 처리하거나 archive하지 않는다.
- 소유가 불확실한 branch·worktree를 reset, clean, stash 또는 강제 갱신하지 않는다.
- 해결되지 않은 rebase 충돌, source 불일치, archive 충돌과 손상된 상태의 사용자 알림을 숨기지 않는다.
- 반복 한도를 소진한 동일 verify·Review·rebase 실패를 무제한 재시도하지 않는다.
- 게시할 diff가 사라졌다는 사실만으로 해당 ExecPlan이 이미 충족됐거나 MERGED됐다고 추정하지 않는다.
- 닫힌 PR을 자동으로 다시 열거나 guidance 결정을 사용자를 대신해 승인하지 않는다.
- BLOCKED 알림이 아닌 일반 운영 로그까지 제거하지 않는다.

## Open Questions

- 없음
