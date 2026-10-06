# [R-077] G 계획 수동 구현과 R·코드 일괄 제출

## Context

수동 구현은 두 가지 경로를 지원해야 한다. 하나는 Manager가 생성한 기존 G 계획을 사용자가 다른 기기에서 구현하는 경우다. 다른 하나는 G 없이 사용자가 R 문서와 코드를 함께 작성해 원격 branch에 push하는 경우다. Loop Manager는 두 경로의 제출을 받아 최종 검증·Review·자동 교정·PR 게시와 실제 Merge 추적을 담당한다. 기존 G의 수동 구현을 기다리는 동안에는 해당 계획만 자동 Worker 대상에서 제외하고 독립적인 다른 계획은 계속 처리한다.

현재 실행 상태와 branch·worktree·PR 연결의 원본은 SQLite다. Manager 소유 작업 branch에 사용자가 push한 변경을 반영하는 경로는 있지만, 별도 외부 PR을 G 계획의 완료 근거로 자동 연결하는 기능은 확인되지 않았다. 실제 Merge 인식은 저장된 계획의 branch·PR identity를 기준으로 한다.

이 Requirement는 수동 제출을 기존 Worker 제출과 구분하고, G 없는 R·코드 제출에서는 Manager가 추적용 G를 생성·등록하게 한다. 사용자가 외부에서 이미 수행한 전체 검증 또는 Review의 실행은 각각 명시적으로 생략할 수 있다. 완료·제출 요청을 수락한 뒤에는 Manager가 후속 처리를 맡으며 검증·Review·충돌 실패는 기본적으로 기존 자동 복구와 Worker 교정으로 처리한다. [R-060](%5BR-060%5D%20Collaborative%20Git%20Recovery%20and%20Remote%20Blocked%20Snapshots.md)의 사용자 변경 보존·협업 Git 복구·원격 보존 규칙은 유지한다. 기존 Worker 확인과 최종 검증 정책의 예외는 이 문서에 정의한 수동 제출 후보에만 적용하며 이후 자동 Worker 교정에는 기존 조건을 적용한다.

## Requirement

### 명령과 사용자 흐름

등록된 저장소 루트와 명시적인 작업 대상을 받는 다음 명령을 제공한다. 기존 G 경로는 정확한 ExecPlan ID를, G 없는 제출 경로는 원격 branch와 해당 commit 안의 R 문서 경로를 사용한다. 기존 전역 `--state-dir`와 `--config`를 따른다.

```text
python loop_manager.py manual start <repository> <plan-id>

python loop_manager.py manual complete <repository> <plan-id>
  [--skip-verify] [--skip-review]
  [--expected-head <full-commit-sha>] [--skip-reason <text>]

python loop_manager.py manual submit <repository>
  --branch <remote-branch> --requirement <requirement-path>
  [--skip-verify] [--skip-review]
  [--expected-head <full-commit-sha>] [--skip-reason <text>]
```

| 경로 | 입력 | Manager의 역할 |
| --- | --- | --- |
| 기존 G의 수동 구현 | 기존 G ID, 소유 작업 branch의 사용자 구현 | start로 작업을 예약하고 complete로 제출받아 기존 G의 후속 처리를 수행한다. |
| G 없는 R·코드 일괄 제출 | 사용자가 push한 원격 branch, 그 안의 R 문서와 코드 | submit으로 제출 범위를 확인하고 추적용 G와 소유 작업 branch를 준비한 뒤 같은 후속 처리를 수행한다. |

기존 G 경로의 사용 흐름은 다음과 같다.

1. 사용자가 `manual start`로 G 계획을 지정한다. Manager는 선행 작업 각각이 등록된 원격 base에 실제 Merge됐는지 먼저 확인한다. 미Merge 또는 확인 불가인 선행 작업이 있으면 ID·상태·원인을 오류로 출력하고 branch를 생성하지 않는다.
2. 선행 작업 조건이 모두 충족되면 Manager가 최신으로 확인한 원격 base와 계획 정의를 기준으로 소유 작업 branch를 준비하고 원격에 push한다.
3. Manager가 해당 계획을 SQLite에 수동 작업 중으로 확정하고 branch·원격 HEAD·base와 다른 기기에서 사용할 checkout 안내를 출력한다. 자동 Worker는 이 계획을 선택하지 않는다.
4. 사용자가 다른 기기에서 안내된 branch를 checkout해 구현하고 push한다.
5. 사용자가 `manual complete`를 실행하면 Manager가 완료 요청을 수락해 후속 처리를 인수한다. 원격 작업 변경과 최신 base를 반영하고 기본적으로 전체 verify와 읽기 전용 Reviewer 검토를 수행하며, 실패하면 자동 복구·Worker 교정·재검증을 이어간다.
6. 최종 후보의 검증·Review 조건을 만족하면 기존 게시 흐름으로 PR을 생성하거나 동일 소유 branch의 PR을 연결한다. 실제 PR Merge를 확인한 뒤에만 MERGED로 처리한다.

start/complete/submit은 같은 운영 SQLite를 사용하는 Manager에서 실행한다. 다른 기기에서의 구현에 SQLite 복제·직접 수정이나 Manager 실행을 요구하지 않는다. 미등록 저장소, start/complete의 없는 G, submit의 없는 원격 branch·R 문서, 손상되거나 없는 운영 상태는 이유를 알리고 거부하며 빈 DB나 branch 이름으로 기존 작업을 인수하지 않는다. submit에는 사전에 G가 존재할 것을 요구하지 않는다.

### R·코드 일괄 제출과 G 등록

1. 사용자가 `docs/requirements/` 바로 아래의 R 문서와 그 구현·테스트를 같은 원격 branch에 commit·push한다. 사용자가 G 문서를 작성하거나 manual start를 먼저 실행할 필요는 없다.
2. 사용자가 `manual submit`으로 등록 저장소, 원격 branch와 R 문서 경로를 명시한다. `--requirement`는 여러 관련 R을 함께 제출할 때 반복할 수 있다. Manager는 같은 원격 commit에서 R 문서와 구현을 읽고 제출 HEAD·변경 범위·원격 identity를 기록한다. 기본 checkout에만 있는 파일은 입력에 포함하지 않는다.
3. Manager는 제출한 R의 목적·Acceptance Criteria와 기존 제품 규칙, 구현 diff 및 기존 계획의 관계를 확인해 실제 제출 범위를 추적할 G를 하나 이상 생성한다. 각 G는 기존 PLANS 형식의 완성된 문서여야 하며 `sources`에 해당 R을 연결하고 실제 선행 작업이 있으면 `depends_on`에 기록한다. 생성 G별 목표·완료 조건과 제출 R·구현 범위의 연결을 명확히 하며, 제출되지 않은 다른 목표를 섞지 않는다. 구현됐다는 주장만으로 G나 R의 조건을 축소하거나 PASS·MERGED로 만들지 않는다.
4. Manager는 기존 active·archive·실행·취소·예약 이력을 고려해 생성 G들의 고유 ID를 예약하고 하나의 제출 묶음으로 연결한다. 같은 제출의 반복·중단 복구는 같은 묶음·G 목록을 사용하며, 이미 진행 중인 기존 G와 작업 범위가 중복되면 기존 관계를 안내하고 별도 G로 같은 작업을 이중 등록하지 않는다. G가 없거나 범위·관계가 불확실하면 작업 branch를 만들지 않고 원인을 반환한다. 복수 G의 dependency와 게시 단위는 아래 묶음 정책으로 검증한다.
5. 제출 원격 branch는 읽기 입력으로 취급하고 수정·rebase·force push하지 않는다. Manager는 제출 commit을 보존하는 별도의 소유 작업 branch 하나를 준비하고 생성한 모든 G 문서를 그 branch에 함께 게시한다. 원래 R·코드와 생성 G들을 하나의 최종 PR로 검토할 수 있어야 한다. 최초 source branch·commit·R 문서와 제출 묶음·전체 G ID·소유 branch의 연결을 SQLite에 보존한다.
6. 수락한 submit은 사전 수동 대기를 만들지 않고 자동 후속 처리로 이어진다. 최신 base 반영, 기본 전체 verify·읽기 전용 Review와 필요 시 Worker 교정·재검증을 수행하고 Manager 소유 branch의 PR을 게시한다. 이후 완료 기준은 해당 연결 PR의 실제 Merge다.

R이 아직 base에 없고 제출 branch에만 있어도 그 commit의 문서를 근거로 G를 생성·검증·등록할 수 있어야 한다. 생성 G들이 base에 Merge되기 전에는 저장된 제출 묶음 identity와 게시된 소유 branch의 정확한 G·R revision을 이 작업의 근거로 사용한다. 이 명시적 제출 예외를 일반 Planner의 미등록 branch 검색이나 임의 branch 채택으로 확대하지 않는다. 일반 catalog reconciliation이 base의 G 부재만으로 이 작업을 취소·재등록하거나 전체 프로젝트를 차단하지 않도록 구분한다.

수락·전체 G ID 예약·문서 생성·branch 준비·push·SQLite 확정 사이의 intent를 보존한다. 생성이나 게시 실패는 기존 유한 재시도와 원격 보존으로 복구하며, 묶음의 모든 G 등록이 준비될 때까지 교정 Worker를 시작하지 않는다. 일부 G 문서나 row만 준비됐다고 독립 READY로 노출하거나 일부만 게시하지 않는다. 생성 G의 문서 게시만으로 다른 자동 Worker가 이를 새로운 READY로 선택하지 않도록 제출 소유권과 후속 처리 상태를 함께 확정한다. 실제 Merge 뒤에는 원격 base의 같은 G들을 기존 실행 row와 조정하고 수동 제출·교정·검증·PR 이력을 유지한다.

submit 성공 또는 수락 후 대기 안내에는 제출 묶음과 생성·예약된 전체 G ID 목록, 최초 제출 commit, Manager 소유 branch와 이후 수정·추적 대상을 제공한다. 이후 사용자는 안내된 소유 branch에 수정해 push할 수 있고 `manual complete`로 생성 G를 명시해 재요청할 수도 있다. 복수 G의 구성원 하나를 지정한 complete도 해당 묶음 전체를 처리하며 대상 G 목록을 명시한다. 최초 source branch의 변경은 별도 submit 없이 기존 승인에 자동 반영하지 않는다. 같은 source·R 범위의 반복 submit은 기존 묶음과 전체 G 연결을 대조하고 새 commit이라면 같은 작업의 새 후보로 보존·반영하며 G를 중복 생성하지 않는다.

### 복수 G의 제출 묶음

- G가 하나이면 기존 단일 계획의 후속 처리를 사용한다. 여러 개이면 전체 G ID·정의·R 연결과 하나의 source commit·소유 branch·worktree·PR을 결합한 제출 묶음으로 추적한다. 등록 시 전체 구성원을 확정하고 단순 재시도·복구로 G를 추가·제거하지 않는다. 한 G를 임의로 대표해 나머지 G를 누락하거나 생성 개수만으로 범위를 합치지 않는다.
- 같은 묶음에서 함께 게시할 G들은 서로 선행 Merge를 요구하지 않아야 한다. 묶음 내부 `depends_on`이 있으면 선행 PR Merge 전에 후행 G를 같은 PR로 검증·게시할 수 없으므로, Manager 작업 branch 생성 전에 오류를 반환하고 해당 G·의존 관계 및 별도 branch/PR의 순차 제출 필요성을 안내한다. 내부 dependency를 삭제하거나 Merge된 것으로 가정하지 않으며 제출 코드를 임의 분할하지 않는다. 등록 후 사용자 계획 개정으로 이런 관계가 생겨도 같은 검사를 적용하고 기존 branch·작업물은 보존한다.
- 묶음 밖 dependency는 모든 구성 G의 선행 관계를 합쳐 확인한다. 전부 등록된 원격 base에 실제 Merge된 뒤에만 소유 작업 branch를 생성한다. 조건이 미충족이면 제출·생성 초안과 대기 근거를 보존하고 branch를 만들지 않는다. 수락된 유효 요청은 조건 충족 뒤 자동 재개할 수 있다.
- 공유 branch·worktree의 소유권은 저장된 묶음 identity와 전체 구성 G 목록으로 검증한다. 같은 branch를 사용하는 다른 계획을 묶음 구성원으로 추정하지 않으며 각 G가 공유 자원을 독립적으로 변경·정리하지 않는다. 구성 G의 개별 manual start·별도 Worker 선택은 허용하지 않는다.
- 한 프로젝트에서 묶음의 작업은 직렬로 수행한다. 교정 대상 G 하나만 현재 활성 작업으로 선택하고 나머지 구성원은 묶음 대기로 유지한다. 여러 G가 같은 worktree에서 동시에 Worker·verify를 실행하거나 ACTIVE row를 여러 개 만들지 않는다.
- 최종 verify는 공유된 최종 후보를 대상으로 하고 Review는 그 후보에서 모든 G의 Acceptance Criteria·R 관계·구현을 확인해 G별 결과를 남긴다. 하나라도 미충족·blocking이면 실패한 G의 원인을 자동 교정하고 필요한 검증·Review를 다시 수행하며 묶음을 게시 완료로 처리하지 않는다. 후보가 바뀌면 각 G의 관련 근거와 묶음의 게시·생략 근거를 함께 재판단한다.
- 모든 구성 G의 최종 조건이 충족된 같은 후보에서 PR 하나를 생성·연결한다. 각 G에는 같은 묶음·branch·PR identity와 자신의 결과를 연결한다. PR의 실제 Merge와 외부 dependency를 확인한 뒤 묶음 전체 G의 MERGED를 같은 transaction으로 확정한다. 일부 G만 임의로 완료하거나 source branch Merge를 대신 완료 근거로 쓰지 않는다.
- 반복 submit/complete와 중단·재시작은 전체 구성·상태·단계 결과를 대조한다. 일부 row 갱신이나 PR 생성 뒤 중단돼도 같은 묶음과 PR을 복구하며 새 G·branch·PR을 중복 생성하지 않는다. PR closed/reopen·차단은 묶음 공유 원인과 G별 원인을 구분하고 worktree 정리는 묶음당 한 번 수행한다.

### 제출 후보의 R 문서

두 경로 모두 관련 R 문서와 코드를 같은 작업 branch에 commit·push할 수 있다. 완료 검증과 Reviewer는 제출·최종 후보 commit의 R 원문을 구현과 함께 읽으며 base의 옛 R만으로 새 구현을 판단하지 않는다. 사용자 R 작성·수정과 Manager·Agent의 변경은 구분해 기록한다.

기존 G 경로에서는 사용자가 G의 원래 목적·sources·dependency를 보존하거나 정당한 사용자 개정으로 일치시켜야 한다. G 없는 경로에서는 Manager가 제출 R과 생성 G의 연결을 담당한다. R·G의 관계나 Acceptance Criteria가 바뀌면 기존 후보 근거와 생략의 유효성을 재판단하고, 수락 뒤에는 필요한 확인·검증·Review를 기본 수행한다. 두 경로 모두 R의 일부 구현을 R 전체 완료로 추정하지 않으며 기존 최종 충족 판단·archive 조건을 유지한다.

### 수동 상태와 실행 자리

- 사용자 구현 대기는 SQLite의 `MANUAL_WORKING` 상태로 표현한다. 실행 상태를 ExecPlan frontmatter에 기록하지 않는다.
- 수동 시작·제출 이력과 현재 후속 처리 주체를 구분한다. 기존 G의 complete 수락 전에는 수동 작업을 보호하고, complete/submit 수락 뒤에는 자동 후속 처리로 전환한다. 필요하면 IMPLEMENTING으로 이동해 Worker가 교정할 수 있으며 최초 사용자 제출과 실제 Worker 교정 결과는 별도로 보존한다.
- 수락 뒤 실행 자리·dependency·재시도 대기는 자동 후속 처리의 대기로 저장·표시하고 미제출 MANUAL_WORKING과 구분한다. 대기 자체는 활성 상태나 실행 자리 점유 근거가 아니며 실제 작업을 시작할 때만 기존 단일 활성 계획 규칙을 적용한다.
- 수동 대기는 프로젝트의 활성 변경 실행 자리를 점유하지 않는다. branch·worktree·실패 기록·session을 보존하더라도 대기만으로 project 실행 projection이나 scheduler 작업 자리를 유지하지 않는다.
- start의 실제 자원 준비와 complete/submit의 문서·작업 branch 준비·원격 반영·검증·Review·게시 동안에는 기존 단일 프로젝트 변경 자리와 lock을 사용한다. 다른 작업이나 소유 operation을 선점하지 않는다.
- complete 수락 전의 수동 계획과 그 미Merge 결과에 의존하는 계획은 자동 Worker가 선택하지 않는다. 수락 뒤에는 해당 계획의 자동 복구·교정이 가능하지만 종속 계획은 계속 실제 Merge를 기다린다. 독립 계획은 기존 priority와 실제 안전·유지보수 gate가 허용하는 범위에서 계속 처리한다.
- 수동 대기 중 사용자 push나 start 실패만으로 자동 Worker를 재개하지 않는다. complete/submit 수락이 자동 후속 처리의 경계이며 별도 자동 교정 옵션이나 추가 승인을 요구하지 않는다. 수락 전에는 수동 작업을 자동 구현·교정·rebase·검증·Review·게시하지 않고 기존 안전 경계에서의 원격 관측만 계속한다.
- 수동 계획은 비종료 작업이다. 관련 프로젝트 완료와 Requirement 충족·archive를 수동 시작, 검증 완료 또는 생략만으로 확정하지 않는다.

### 시작 가능 상태와 기존 작업

다음 start 정책은 기존 G의 수동 구현 경로에 적용한다. G 없는 R·코드 제출은 앞의 submit 등록 조건을 따른다.

| 현재 상태 | 시작 정책 |
| --- | --- |
| READY | 최신 선언과 안전 gate가 유효하면 시작한다. 기존 소유 자원이 있으면 같은 계획의 검증된 자원만 재사용한다. |
| PLAN_BLOCKED | 코드·검증·Review 문제 등 사용자가 수정할 수 있고 안전하게 인계 가능한 계획만 시작한다. 기존 원인·실패·승인 대기는 보존한다. |
| MANUAL_WORKING | 같은 수동 작업의 미완료 시작을 복구하거나 현재 branch와 상태를 안내한다. 새 branch를 만들거나 원격 후속 commit을 되돌리지 않는다. |
| IMPLEMENTING·VERIFYING·REVIEWING·PUBLISHING | 신규 인계를 거부한다. 실행 중 Agent·verify나 미완료 작업의 자원을 탈취하지 않는다. |
| PR_OPEN | 신규 시작을 거부한다. 이미 수동 제출한 계획의 후속 변경·충돌은 기존 자동 복구·재검증으로 처리한다. |
| MERGED·취소·삭제·archive된 비활성 계획 | 시작을 거부한다. ID를 재사용하거나 READY로 재등록하지 않는다. |

PROJECT_BLOCKED, catalog hold, 불명확한 origin·branch·worktree 소유권 또는 사용자 Git operation을 시작 명령으로 우회하지 않는다. Agent guidance/User Decision 승인 대기와 불완전한 명세도 수동 시작으로 해제하지 않는다. 인계 가능 여부는 현재 구조화된 원인과 안전 근거로 판단한다.

작업 branch 이름과 기존 저장 identity는 현재 Manager 규칙을 따른다. branch 이름 prefix만으로 소유권을 인정하지 않는다. 같은 계획에 연결된 branch·worktree는 SQLite 소유 근거와 실제 Git 등록이 일치할 때만 재사용한다. 같은 이름의 사용자 branch, 다른 계획의 worktree와 소유 증거 없는 경로는 덮어쓰기·삭제 없이 거부한다. 자원이 없어도 소유 생성 근거와 원격 identity가 유효하면 기존 복구 정책으로 재구성할 수 있다. submit의 명시적 source branch는 이 소유권 검사와 구분하는 읽기 입력이며, 기존 사용자 branch를 Manager 소유로 바꾸는 근거가 아니다.

기존 원격 commit과 로컬 commit·staged·unstaged·untracked 작업물은 R-060에 따라 함께 보존한다. 저장된 PR이 있으면 정확한 동일 branch·PR인지 확인하며, Merge 없이 닫힌 PR의 기존 reopen 조건을 유지한다. PR 존재만으로 작업이 검증됐거나 완료됐다고 간주하지 않는다.

원격 push 전에 복구 가능한 수동 시작 intent와 소유 예약을 SQLite에 기록한다. 이 시점부터 자동 Worker 선택과 Planner 개정을 막되, 사용자 작업 준비 완료는 push와 원격 확인 뒤에만 확정한다. branch 생성·push 후 DB 확정 전에 중단돼도 같은 작업을 재구성한다. 부분 실패를 단순 READY로 되돌려 자동 Worker에게 넘기지 않는다.

### 미Merge dependency

- start는 선택한 G의 모든 선행 작업이 등록된 원격 base에 실제 Merge됐음을 확인한 뒤에만 허용한다. SQLite의 MERGED나 계획 본문의 완료 표시만으로 충족을 추정하지 않고 저장된 계획·PR identity, 실제 GitHub Merge 대상·상태와 최신으로 확인한 원격 base의 Merge 근거를 대조한다. squash·rebase Merge는 기존 검증된 Merge 근거로 판단하며 원래 작업 HEAD의 단순 ancestry만 요구하지 않는다.
- 미Merge·Merge 대상 불일치·원격 근거 확인 불가이면 start는 종료 코드 1로 선행 G ID·관측 상태·원인과 필요한 조치를 출력한다. branch·worktree 생성, push와 신규 수동 시작 확정을 수행하지 않는다. 기존 소유 자원이 있으면 그대로 보존한다. 미Merge dependency를 허용하는 우회 옵션은 제공하지 않는다.
- 미존재 ID·자기 의존성·순환·미검증 archive identity를 거부하며 로컬 base, 다른 branch의 Merge 또는 선행 작업 branch를 합친 사실로 대체하지 않는다. branch 생성·게시 직전 기준이 바뀌면 같은 gate를 다시 확인하고 불일치한 근거로 새 작업을 게시하지 않는다.
- complete/submit은 유효한 완료·제출 요청을 수락해 dependency 대기로 보존할 수 있다. submit은 모든 생성 G의 dependency를 검증하며 사전 G 부재를 dependency 없음으로 간주하지 않는다. submit의 새 소유 branch 생성, 최종 후보 검증·Review·PR 게시와 교정 Worker 시작 전에는 적용되는 선행 작업의 원격 base Merge를 다시 확인한다. 미충족이면 실행 자리를 비우고 미충족 ID·상태를 안내한다. 생략 옵션으로 우회하거나 미래 후보에 대한 생략 승인을 예약하지 않는다.
- dependency Merge 뒤에는 수락한 요청을 다음 정상 실행에서 자동 재개하며 complete 재실행을 요구하지 않는다. Manager는 최신 base 반영부터 진행하고 기존 후보의 검증·생략을 새 후보에 자동 적용하지 않는다. 생략이 무효화된 단계는 기본 검증·Review로 처리한다.

### Planner와 유지보수 보호

수동 소유 계획과 start/submit의 준비 중 intent는 일반 Planner의 mutable READY, 읽기 전용 READY 점검, 유지보수 barrier·post-refactor revalidation의 수정 가능 대상에서 제외한다. compact catalog에는 수동 상태와 관계를 계속 제공한다. Planner snapshot 이후 수동 시작·제출 등록이 발생했으면 게시 직전 소유권 재검사에서 해당 계획의 자동 수정·rename·삭제를 거부한다. complete/submit 수락 뒤에도 실행 이력·소유 자원이 있는 기존 계획의 보호 규칙을 유지하며 자동 교정 허용이 Planner의 개정·삭제 권한을 넓히지 않는다.

이 보호는 Planner의 자동 편집 권한에 적용한다. 사용자가 원격에서 같은 ID의 계획을 직접 개정하면 기존 catalog reconciliation으로 유효한 변경을 반영하고 branch·worktree·PR·이력을 보존한다. 관련 검증·Review·생략 근거는 다시 판단한다. 사용자의 삭제나 잘못된 dependency 변경은 영향 범위와 원인을 보존하며 계획을 임의 재등록하거나 MERGED로 만들지 않는다. 독립 계획에 대한 기존 복구·진행 정책은 유지한다.

complete 수락 전의 수동 대기 자체는 일반 Planner와 독립 자동 계획을 막지 않는다. 읽기 전용 유지보수 분석은 허용하지만 이 대기 중 새 유지보수 리팩터링 cycle의 게시·실행은 보류한다. 이미 시작한 수동 계획에는 무소유 READY용 barrier를 추가해 보호할 수 없기 때문이다. 보류된 due·명시적 점검 요청은 보존하고 일반 READY 선택은 계속 허용한다. complete 수락 뒤에는 기존 진행 작업과 유지보수 선택 정책을 따른다. 이미 활성인 유지보수 cycle의 gate는 유지하며 수동 명령이 prerequisite/refactoring 순서를 우회하지 않는다.

### 완료와 최종 후보

- complete는 수동으로 시작하거나 submit으로 등록한 동일 G를 대상으로 한다. 제출 묶음의 구성원이면 저장된 전체 묶음을 후속 처리 대상으로 사용한다. submit은 G 없는 명시적 R·코드 입력을 받아 새 추적 identity를 준비하는 경로다. complete만으로 무관한 자동 계획이나 외부 branch를 인수하지 않는다.
- 원격 계획 정의 또는 submit의 R·구현 입력, dependency, 사용자 승인, 독립 block과 소유 branch·PR identity를 확인한다. 제출할 원격 commit과 입력을 확인하고 복구 가능한 완료·제출 요청을 SQLite에 기록한 시점에 자동 후속 처리를 인수한다. 잘못된 대상·expected HEAD 불일치·소유권 불확실성 등 수락 전 거부는 기존 상태를 유지하고 자동 교정을 시작하지 않는다.
- 수락한 요청은 실행 자리가 사용 중이거나 dependency가 미충족이면 기존 순서를 기다린다. 다른 활성 작업·planning·publication을 선점하지 않으며 자리가 나거나 조건이 충족되면 정상 실행에서 자동 재개한다.
- R-060의 동일한 Git 정책으로 원격 작업 branch를 먼저 반영하고 최신 base를 결합한다. 충돌·실패에는 작업물을 보존하고 기존 자동 복구와 허용된 Worker 교정을 먼저 수행한다. 실패를 처음 관측했다는 이유만으로 수동 구현 대기로 되돌리지 않는다.
- 최종 변경·commit 정리 후 후보 commit, base와 계획 정의·단계별 입력을 확정한다. 미커밋 사용자 작업이나 불확실한 후보를 통과·게시 근거로 사용하지 않는다.
- 수동 제출은 사용자 구현과 완료 요청의 근거로 기록한다. 기존 Worker 성공이나 targeted unit test 통과를 만들어 넣지 않는다. 외부 테스트 결과가 없으면 미확인으로 전달한다.
- 기본 전체 verify 실패·timeout 또는 Reviewer blocking·프로토콜 실패 시 결과와 진단을 보존하고 기존 원인별 자동 재시도·Worker 교정·재검증을 수행한다. Worker는 필요한 코드·테스트·검증 스크립트와 충돌을 교정하고 관련 targeted unit test를 실행하며 전체 verify와 읽기 전용 Review는 기존 담당 경계에서 다시 수행한다.
- Reviewer는 읽기 전용이며 테스트·verify를 다시 실행하지 않는다. 수동 제출, 실제 verify 결과 또는 사용자 생략과 미확인 외부 테스트를 구분해 전달받는다.
- 후보 확정 뒤 원격 head·base·계획·관련 검증 입력이 바뀌면 오래된 결과로 게시하지 않는다. 사용자 변경을 보존·반영하고 관련 근거를 무효화한 뒤 필요한 Worker 확인·검증·Review부터 자동으로 이어간다. 생략 범위를 새 후보로 넓히지 않고 무효화된 단계는 기본 실행한다.

사용자 결정·guidance 승인, 실제 소유권·공유 안전 문제, 자동 복구로 해소할 수 없는 원인 또는 기존 재시도·교정 한도 소진에는 기존 계획·프로젝트 차단과 원격 보존 정책을 적용한다. 해당 원인과 필요한 사용자 행동을 안내하고 다른 원인·실패 이력을 유지한다. 자동 후속 처리 중 차단됐다는 이유만으로 미제출 수동 작업 상태로 되돌리지 않는다. 사용자 수정으로 원인이 해소되면 기존 자동 재평가·재개 규칙을 따르며 complete/submit 재실행을 필수로 요구하지 않는다.

### verify와 Review 생략

complete/submit의 `--skip-verify`와 `--skip-review`는 각각 전체 verify 실행과 Reviewer 호출만 생략한다. 두 옵션은 독립적이며 함께 사용할 수 있다. 옵션이 없으면 기본 검증·Review 정책을 적용한다.

| 옵션 | 전체 verify | Reviewer |
| --- | --- | --- |
| 없음 | 실행해 통과해야 한다. | 실행해 blocking issue가 없어야 한다. |
| `--skip-verify` | 사용자 생략으로 기록한다. | 실행해 blocking issue가 없어야 한다. |
| `--skip-review` | 실행해 통과해야 한다. | 사용자 생략으로 기록한다. |
| 둘 다 | 각 단계의 사용자 생략으로 기록한다. | 각 단계의 사용자 생략으로 기록한다. |

생략 옵션이 하나라도 있으면 `--expected-head`와 비어 있지 않은 `--skip-reason`을 요구한다. expected HEAD는 명령이 확인한 원격 제출 commit 전체 SHA다. complete는 해당 G의 소유 작업 branch, submit은 명시한 source branch를 기준으로 한다. 짧은 SHA나 이동 가능한 ref는 받지 않는다. 값이 다르면 실제 원격 HEAD를 알리고 요청 수락 전에 거부한다. expected HEAD는 생략 없이도 지정할 수 있으며 reason만 지정하는 입력은 거부한다.

생략은 외부 검증·Review의 통과를 Manager가 확인했다는 뜻이 아니라 사용자가 그 단계 실행을 명시적으로 면제한 사실이다. SQLite에는 단계별 실제 PASS·FAIL·NOT_RUN과 `SKIPPED_BY_USER`를 구분해 저장하고 완료 요청, 대상 commit·입력, reason·시각을 감사 근거로 남긴다. 생략을 실제 성공 cache, Worker 결과 또는 실행된 Agent 호출로 기록하지 않는다. 기존 수단으로 확인할 수 없는 승인자 identity는 추정하지 않는다.

생략은 해당 수동 작업 또는 제출 묶음과 요청의 제출 commit, base, 전체 대상 G 구성·정의 및 단계별 입력에만 유효하다. 제출 commit이 실제 최종 후보와 일치하고 입력이 유효할 때만 적용한다. verify 입력은 검증 스크립트·설정·환경을 포함하고 Review 입력은 diff·모든 대상 G와 R·지침·verify 근거를 포함한다. 구성 G의 추가·제거·정의 변경은 기존 묶음 생략을 무효화하며 한 G의 생략을 무관한 계획에 적용하지 않는다. PR 연결 후에는 동일 PR identity도 유지해야 한다.

원격 후속 commit, rebase·amend·Worker 교정 등 새 SHA, base 변경, 관련 계획·source·guidance·검증 환경 변경과 verify 근거 변경은 해당 생략을 무효화한다. 동일 tree라도 SHA가 다르면 새 후보이며, HEAD가 같아도 관련 입력이 달라졌으면 재사용하지 않는다. submit에서 생성 G를 commit해 최종 SHA가 바뀌는 경우도 예외 없이 포함한다. 코드가 같다는 이유로 최초 source commit의 생략을 새 commit으로 옮기지 않는다. 유효성을 증명할 수 없으면 생략을 이어받지 않는다. 수락 뒤 이 변화가 발생하면 해당 단계를 기본 실행하고 자동 처리를 계속하며, 새 생략 선택을 기다리는 절차를 요구하지 않는다. 사용자가 새 후보의 생략을 원하면 안내된 생성 G·소유 branch의 해당 SHA와 reason을 지정한 complete로 별도의 요청을 해야 한다.

같은 후보에 기록된 실제 verify 실패나 Reviewer blocking을 생략 옵션만으로 없애지 않는다. 해당 실패는 자동 교정과 실제 재실행 성공으로 해소한다. Worker 또는 사용자가 수정한 새 후보에는 이전 실패를 감사 이력으로 보존하고 기본 검증·Review를 수행하며, 별도의 유효한 생략 요청이 있으면 그 범위에서만 적용한다. 승인 대기·dependency·소유권·실제 공유 안전 gate는 생략이나 후보 변경으로 해제하지 않는다.

### 반복 실행과 중단 복구

- 같은 후보·입력·옵션의 complete/submit은 유효한 요청과 단계 결과·게시 intent를 대조해 이어간다. 이미 생성·등록한 G, 성공한 같은 단계, push, PR 생성과 완료 사건을 중복하지 않는다. 실제 성공 결과의 재사용은 [R-072](%5BR-072%5D%20Reuse%20Successful%20Candidate%20Results%20with%20Proven%20Inputs.md)의 입력 동일성 조건을 유지한다.
- 생략 옵션 없는 새로운 complete는 과거 생략을 암묵적으로 이어받지 않는다. 게시 전에 기본 요청으로 바꾸면 실제 PASS가 없는 단계를 수행한다. 동일 후보로 이미 PR_OPEN에 도달한 작업의 재확인은 기존 결과·PR 상태를 반환하며 새 제출로 간주하지 않는다.
- run과 재시작은 이미 수락한 미완료 complete/submit 요청의 등록·후속 처리·교정·재검증을 자동 재개한다. 이는 새로운 요청과 구분하며 현재 후보와 단계 근거를 대조한다. 저장된 생략은 같은 요청·후보·입력에서만 재사용하고 변경됐으면 무효화된 단계를 기본 실행한다. 실제 Worker 교정 session과 결과도 보존한다.
- 일시 네트워크·도구·원격 경쟁 실패는 기존 유한 재시도와 보존 정책을 사용한다. 진전 없는 동일 입력에서 무한 Worker 호출·검증·Review·rebase를 만들지 않는다. 일시 대기·dependency 대기·최종 차단은 실행 자리를 해제하고 독립 작업을 허용한다. 미완료 Git operation·미확정 게시가 있으면 그 복구가 끝나기 전 충돌하는 변경을 시작하지 않는다.
- complete/submit 수락 전에는 원격 사용자 push만으로 자동 Worker를 재개하지 않는다. 수락 뒤에는 Manager 소유 작업 branch의 사용자 후속 push를 기존 자동 반영·원인 재검사 경로로 처리하며 요청을 다시 제출하지 않아도 필요한 단계부터 이어간다. submit의 최초 읽기 source branch는 별도 재제출 대상이라는 구분을 유지한다.

### PR 연결과 실제 Merge

- 검증·Review의 실제 통과 또는 유효한 사용자 생략 뒤 기존 최신 base·원격 OID·게시 gate로 PR을 생성하거나 연결한다. PR 설명과 저장 결과에는 수동 제출, 실제 실행 결과, 생략 단계·reason·대상 commit을 구분해 표시한다.
- 연결 대상은 같은 저장소, 등록 base와 해당 계획의 소유 head branch가 정확히 일치하는 PR이다. 이미 저장된 PR identity는 바꾸지 않는다. 사용자가 수동 작업 branch에서 만든 동일 PR은 조건 확인 후 연결할 수 있다.
- 다른 branch의 외부 PR, 계획 ID가 적힌 PR, base의 비슷한 변경이나 commit 포함만으로 G 계획 완료를 추정하지 않는다.
- PR_OPEN 후 새 사용자 commit·충돌·관련 입력 변경을 관측하면 이전 승인·생략을 무효화하고 PR 연결은 보존한다. 기존 자동 PR 복구·Worker 교정·재검증·Review·게시로 이어가며 complete 재실행을 요구하지 않는다. 새 후보에 생략을 자동 적용하지 않는다.
- Merge 없이 닫힌 PR에는 기존 close block과 동일 PR reopen 조건을 적용한다. 새 PR로 대체해 완료로 처리하지 않는다.
- PR 생성·연결, 검증·Review 통과와 생략은 MERGED 근거가 아니다. 저장된 branch·PR identity의 실제 GitHub Merge 및 dependency 완료 조건을 확인한 뒤 기존 Merge 처리·안전 cleanup·archive를 적용한다. 복수 G의 명시적인 제출 묶음은 전체 구성원의 최종 근거를 대조해 함께 확정하고 공유 worktree의 정리·재시도는 한 번 수행한다. Merge 상태를 확인할 수 없으면 완료하지 않는다.
- MERGED 계획에 complete를 반복해도 저장된 실제 Merge 결과만 반환한다. 새 branch·PR·검증 요청을 만들지 않는다.

### lock과 사용자 변경 보존

기존 중복 실행 process lock, 프로젝트 변경 자리, bootstrap/runtime 소유권과 Git operation journal을 준수한다. 같은 worktree에서 Agent·verify가 사용 중일 때 checkout·rebase·stage하지 않는다. Git lock 삭제, reset·clean, 사용자 operation의 임의 abort와 불명확한 자원 탈취를 복구 수단으로 사용하지 않는다.

소유 작업 branch의 사용자 변경과 로컬 작업물은 R-060에 따라 보존한다. 이력 갱신이 필요한 검증된 동일 branch에만 정확히 관측한 원격 OID의 lease를 사용하며 예상 밖 원격 commit을 덮지 않는다. base force push와 branch protection 우회는 허용하지 않는다. 보존용 checkpoint·진단 branch는 검증 통과·Review 승인·수동 완료의 근거가 아니다.

### 결과와 안내

start 성공에는 branch·원격 HEAD·base, 다른 기기 작업 안내와 complete 명령을 제공한다. complete/submit에는 요청 수락 여부, 제출 묶음과 전체 G ID·각 G의 결과, source·소유 작업 branch, 제출·현재 후보, 단계별 실행·생략·실패 결과, 자동 교정·재시도 진행 또는 대기 조건과 PR URL을 표시한다. 생성 G의 commit 등으로 생략이 무효화됐으면 사유와 실제 실행한 단계를 안내한다. 사용자 조치가 필요한 최종 차단은 실제 대상과 이유를 안내한다. 생략과 실패는 실제 통과와 다른 표현으로 출력하며 기존 비밀값 제거 규칙을 따른다.

원격 준비까지 끝난 start, PR_OPEN까지 끝난 complete/submit과 기존 완료 결과의 재확인은 종료 코드 0이다. 자동 처리 계속 중·G 등록 준비 중·dependency 대기·실행 자리 사용 중·최종 차단 또는 수락 전 후보 불일치·안전 거부에는 1을 반환하고 요청 수락·보존 여부를 명시한다. 수락된 요청은 0이 아닌 종료 코드만으로 취소되거나 수동 작업으로 돌아가지 않는다. 잘못된 인자는 기존 CLI의 2를 따른다. 시작 준비 중·자동 교정 진행 중·PR Merge 대기를 계획의 최종 완료로 보고하지 않는다.

## Acceptance Criteria

- start로 지정한 G 계획의 Manager 소유 branch를 원격에서 checkout할 수 있고 SQLite는 수동 작업 중으로 기록한다. start는 Worker·Reviewer를 호출하지 않는다.
- G 없는 원격 branch의 R 문서·코드를 submit하면 사전 start나 사용자 작성 G 없이 Manager가 완성된 G를 하나 이상 생성·등록한다. 모든 G의 sources·dependency·고유 ID와 제출 묶음·HEAD·R·작업 범위가 연결되고 중복 계획을 만들지 않는다.
- 아직 base에 없는 제출 R과 생성 G를 정확한 제출·소유 branch revision에서 확인하며 일반 catalog·Planner가 미등록 branch를 임의 채택하거나 base의 G 부재를 잘못된 프로젝트 차단으로 만들지 않는다. 실제 Merge 뒤 같은 G row·이력을 유지한다.
- submit은 원래 사용자 source branch를 수정·탈취하지 않고 제출 commit을 보존한 Manager 소유 branch에서 R·코드·생성 G를 같은 PR로 게시한다. 두 경로의 최종 검증·Review가 후보의 R 문서와 구현을 함께 기준으로 삼는다.
- complete 수락 전 수동 대기는 실행 자리를 비우고 자동 Worker 선택에서 제외된다. 수락 뒤에는 같은 계획의 자동 후속 처리·교정이 가능하다. 독립 READY는 기존 gate에서 계속 선택되고 종속 계획은 실제 Merge를 기다린다. 수동 대기가 프로젝트 완료·관련 Requirement archive를 허용하지 않는다.
- READY, 소유 자원이 있는 READY, 인계 가능·불가 PLAN_BLOCKED와 ACTIVE·PR_OPEN·MERGED를 위 시작 정책대로 처리한다. 같은 start 반복과 소유 자원 복구에 중복 branch·원격 덮어쓰기·자동 인계가 없다.
- start는 선행 작업의 등록 원격 base Merge를 실제 근거로 확인한다. 미Merge·대상 불일치·확인 불가와 SQLite MERGED만 있는 경우에는 ID·상태·원인의 오류를 반환하며 새 branch·worktree 생성·push·수동 시작 확정이 없다. 기존 작업물을 유지하고 squash·rebase Merge도 검증된 근거로 올바르게 판단한다. dependency 우회 옵션은 없다.
- 유효하지 않은 graph는 거부되며 Merge 전 submit의 새 작업 branch 생성·교정 Worker·최종 검증·Review·PR 게시가 생략 옵션으로도 진행되지 않는다. 수락된 유효 complete/submit은 외부 dependency 대기를 보존하고 실제 원격 base Merge 뒤 재제출 없이 자동 재개한다.
- 복수 G가 서로 선행 Merge를 요구하지 않으면 하나의 제출 묶음·소유 branch·PR에서 모든 G의 조건과 결과를 검증한다. 구성 G는 독립 READY·병렬 Worker로 선택되지 않고 한 번에 한 G만 활성 실행한다. 일부 G의 실패를 자동 교정하기 전 묶음 전체를 게시 완료로 처리하지 않는다.
- 묶음 내부 dependency가 있거나 G가 없거나 범위가 불확실하면 Manager 작업 branch를 생성하지 않고 오류·대상 G·관계·분리 제출 필요성을 안내한다. dependency 삭제·임의 코드 분할·선행 Merge 가정으로 같은 PR 게시를 강행하지 않는다.
- 명시적 복수 G 묶음의 같은 PR이 실제 Merge됐고 각 G의 최종 조건·외부 dependency가 확인되면 전체 MERGED를 같은 transaction으로 확정한다. 구성원 하나를 지정한 complete, 부분 등록·결과 갱신·PR 생성·Merge 확정의 중단·재시작에도 같은 전체 G 목록·PR을 복구하며 worktree cleanup·게시·완료 사건을 중복하지 않는다.
- 일반·유지보수 Planner의 자동 개정·rename·삭제와 오래된 snapshot 게시에서 수동 계획·시작 intent를 보호한다. 사용자 직접 변경은 기존 reconciliation으로 처리하고 자원·이력과 영향 범위를 보존한다.
- 수동 대기 중 새 유지보수 리팩터링의 게시·실행을 보류하면서 due·명시적 요청을 보존하고 독립 일반 계획은 계속한다. 이미 활성인 cycle의 gate는 수동 명령으로 우회되지 않는다.
- 다른 기기의 사용자 push와 로컬 보존 작업, 정상 분기·최신 base를 함께 반영한다. complete/submit 수락 뒤 conflict·검증·Review 실패는 자동 복구·Worker 교정을 우선한다. 실제 소유권·lock·사용자 결정과 최종 차단에는 기존 안전·보존 정책을 적용한다.
- 기본 complete/submit은 최종 후보의 전체 verify와 읽기 전용 Reviewer를 수행한 뒤 기존 게시 경로에 연결한다. 수동 제출을 Worker 성공이나 외부 targeted test 통과로 위장하지 않는다.
- 네 가지 생략 조합, 제출 commit의 전체 expected SHA·reason 필수 조건과 수락 전 후보 불일치를 확인한다. 각 단계의 SKIPPED_BY_USER는 실제 PASS·FAIL과 구분되고 Reviewer 입력·PR 설명·저장 결과가 일치한다.
- commit·base·관련 명세·지침·검증 환경·verify 근거 변경에서 생략이 무효화된다. 동일 tree 새 SHA와 동일 HEAD의 관련 입력 변경도 포함한다. Worker 교정·rebase 뒤에는 무효화된 검증·Review를 별도 사용자 선택 없이 기본 실행하고 새 무옵션 완료 요청이 과거 생략을 상속하지 않는다.
- complete/submit 수락 뒤 verify 실패·timeout·Review blocking·프로토콜 실패·일시 오류에 자동 교정·재시도·재검증을 수행하고 PR 게시까지 이어간다. 같은 후보의 실제 실패를 생략으로 지우거나 실패만으로 MANUAL_WORKING에 돌려보내지 않는다. 승인·안전 문제와 한도 소진에는 기존 차단을 적용하고 독립 차단을 일괄 해제하지 않는다.
- verify 생략 후 Review blocking, Review 생략 후 verify 실패에서 Worker가 후보를 수정하면 이전 후보의 생략을 무효화하고 새 후보의 전체 verify·Review를 기본 수행한다. 두 단계 생략 뒤 최신 base 반영으로 후보가 바뀌는 경우에도 재제출을 기다리지 않고 필요한 단계를 자동 실행한다.
- 동일 후보 반복 완료와 start/완료 수락/원격 반영/Worker 교정/verify/Review/push/PR 생성/SQLite 확정 경계의 중단·재시작에서 유효한 결과·intent·session을 재구성한다. 수락 뒤 요청을 다시 제출할 필요 없이 자동 후속 처리가 재개되며 작업물 손실·중복 PR·중복 게시·새 후보에 대한 오래된 승인 적용이 없다.
- submit의 수락/G ID 예약/G 생성/branch 준비/push/등록 확정 경계에서도 같은 요청과 G를 복구한다. 명시적 source HEAD·R 변경의 재제출은 기존 연결의 새 후보로 처리하고 무관한 입력·기존 G 중복 범위·고유 ID 충돌·손상된 문서는 안전하게 거부한다.
- submit의 생성 G commit으로 SHA가 바뀌면 최초 source commit의 생략을 무효화하고 필요한 전체 verify·Review를 자동 수행한다. 무효화 사실과 이후 명시적 생략에 사용할 G·소유 branch·후보 SHA가 결과에 표시된다.
- 동일 소유 branch·base의 PR만 연결하고 임의 외부 PR은 완료 근거로 사용하지 않는다. PR_OPEN 후 변경·충돌은 자동 교정·재검증으로 처리하고 closed/reopen과 반복 complete도 기존 identity·수동 제출 이력을 유지한다.
- 실제 연결 PR Merge와 dependency를 확인하기 전에는 MERGED를 만들지 않는다. 실제 Merge 뒤 기존 안전 cleanup과 보존이 동작하며 반복 complete가 새 작업을 만들지 않는다.
- 기존 lock·단일 변경 자리·원격 lease·사용자 작업물·guidance 승인·PROJECT_BLOCKED 규칙과 자동 Worker 경로의 검증·Review 조건이 유지된다. 실제 provider 호출 없는 관련 targeted unit test와 기존 전체 검증으로 확인한다.

## Product Impact

- 사용자는 Manager가 준비한 원격 branch에서 특정 계획을 직접 구현하고 같은 계획의 기존 PR 게시·Merge 추적 흐름으로 돌아올 수 있다.
- 사용자는 G 없이 R 문서와 코드를 함께 제출할 수 있고 Manager가 G·소유 작업 branch·검증·PR 추적을 준비한다. 사용자가 G를 직접 작성할 필요는 없다.
- 수동 구현 대기는 독립 자동 작업을 막지 않고 Planner의 자동 편집과 자동 Worker 교정으로부터 해당 계획을 보호한다.
- complete/submit 요청 수락 뒤에는 실패·충돌·후속 후보 변경을 기본적으로 Manager가 자동 복구·Worker 교정·재검증하며 사용자 재제출은 요구하지 않는다.
- 외부 검증·Review를 이유로 선택한 생략은 대상 후보와 입력에 한정된 명시적 사용자 면제로 기록되며 실제 통과와 구분된다.
- 생성 G가 여러 개면 명시적 제출 묶음의 전체 목표를 같은 후보·PR에서 검증하고 실제 Merge 뒤 함께 완료한다. 묶음 내부의 선행 Merge 의존성은 별도 순차 제출로 처리한다.
- 수동·자동 경로 모두 계획의 최종 완료는 연결 PR의 실제 Merge를 기준으로 한다.

## Non-Goals

- 자동 PR Merge, 임의 외부 branch·PR의 자동 검색·완료 근거 채택과 stacked PR을 추가하지 않는다. submit의 명시적 R·코드 branch 입력은 허용하지만 기존 사용자 branch·PR을 Manager 소유로 인수하거나 이미 검증·Merge됐다고 추정하지 않는다.
- 별도의 자동 Worker 전환 명령, 다른 기기의 SQLite 공유·복제와 branch 이름에 의한 소유권 인수를 추가하지 않는다. 자동 후속 처리의 기본 경계는 complete/submit 수락이다.
- complete 수락 전 수동 대기 중 자동 구현·충돌 교정·검증·Review·게시나 새 유지보수 리팩터링 실행을 추가하지 않는다.
- 미Merge dependency를 허용하는 수동 시작, 묶음 내부 dependency 우회와 제출 코드를 자동 분할하는 다중 PR 생성은 추가하지 않는다.
- 외부 검증·Review의 통과를 추정하거나 생략으로 사용자 승인·dependency·소유권·실제 Merge 조건을 우회하지 않는다.
- 새 실행 엔진·Agent 역할·외부 infrastructure를 도입하거나 R-060의 Git 보존·복구 정책을 별도로 재정의하지 않는다.

## Open Questions

- 없음
