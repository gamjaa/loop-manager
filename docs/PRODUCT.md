# Loop Manager

## Summary

외부 스케줄러가 반복 실행하며 여러 로컬 Git 프로젝트의 계획, 구현, 검증과 PR 생성을 자동으로 진행하는 Loop Engineering 관리 프로그램을 만든다.

## Target Users

로컬 Git 프로젝트를 Loop Engineering 하려는 프로그래머

- 프로젝트의 제품 문서와 기술 설계를 작성한다
- 대상 프로젝트를 등록하고 외부 스케줄러로 Loop Manager를 반복 실행한다
- BLOCKED 사유와 생성된 PR을 확인하고 필요한 결정을 내린다
- 검토가 끝난 PR을 Merge한다

## Scope

### Current Goals

- 여러 로컬 GitHub 프로젝트를 등록하고 공통 파일을 코드로 검증한다. `AGENTS.md` 불일치는 사용자 확인으로 승인할 수 있다.
- Specification Skill로 PRODUCT·Requirement·ARCHITECTURE 작성을 지원한다.
- 미충족 PRODUCT·Requirement 작업을 Worker 시작 전에 ExecPlan catalog로 만들고, 메타데이터·의존성·우선순위에 따라 처리한다.
- 계획별 상태·재시도·Agent session·작업 공간을 SQLite에 보존해 중단 후 재개한다.
- Worker가 구현·관련 테스트·전체 검증 스크립트를 작성하고, 프로그램이 전체 검증과 독립 Review를 실행한다. 실패는 기존 작업에서 교정한다.
- 검증된 작업으로 PR을 생성하고 사용자 Merge를 기다린다.
- 완료된 Requirement는 마지막 관련 구현 PR에서, MERGED ExecPlan은 별도 정리 commit에서 자동 archive한다.
- 같은 프로젝트의 변경은 직렬로, 다른 프로젝트는 설정한 동시 실행 수 안에서 병렬로 처리한다.
- 변경되지 않은 입력에서는 Planner를 반복 호출하지 않는다.
- PR 생성·차단·프로젝트 완료를 Telegram으로 알린다.

### Non-Goals

- 같은 프로젝트 안의 병렬 작업
- 자동 Merge
- 내장 스케줄러
- 지정된 이벤트 외 실시간 상태 알림

## Product Invariants

- 미충족 PRODUCT 또는 활성 Requirement에 근거한 작업만 계획한다. 범위 밖 아이디어는 현재 작업으로 자동 승격하지 않는다.
- 제품 문서는 동작과 장기 규칙, Requirement는 원하는 변화와 Acceptance Criteria, ExecPlan은 구현 계획을 기록한다. 자동 수정 권한은 각 문서의 기존 예외로 제한한다.
- ExecPlan ID는 계획과 실행 이력을 연결하는 안정적인 식별자다. 실행 상태의 원본은 SQLite이며 문서에 상태를 기록하지 않는다.
- 자동 작업은 최신으로 검증한 `origin/<base-branch>`를 기준으로 한다. 기본 worktree의 로컬 변경·commit은 보존하고 자동 입력에 포함하지 않는다.
- 실행 중인 변경 작업은 새 계획 입력이 선점하지 않는다. 명세·전체 계획 입력 변경은 다음 안전한 선택 경계에서 반영한다.
- 같은 프로젝트의 변경 실행은 하나이며, 모든 선행 ExecPlan이 MERGED인 계획만 시작한다. 서로 다른 프로젝트는 `max_workers` 안에서 병렬 처리한다.
- Agent는 선택된 관리 worktree와 역할별 허용 범위에서만 작업한다. 불확실한 소유권이나 실제 범위 위반은 자동으로 허용하지 않는다.
- 실패·중단·차단 시 작업물·session·진단을 보존한다. 원인과 안전 조건이 실제로 해소된 작업만 재개한다.
- 최종 후보의 관련 테스트·전체 검증·독립 Review가 성공해야 PR을 생성한다. 연결 PR의 실제 Merge만 MERGED와 완료의 근거다.
- 사용자 변경을 승인 없이 삭제하거나 덮어쓰지 않는다. 정리 안전성을 확인할 수 없는 worktree는 보존한다.
- 같은 사용자 상태 디렉터리에서는 Loop Manager 실행을 하나만 허용한다.
- 동일한 PR·차단·완료 사건은 중복 통지하지 않는다.

문서 입력·등록·작업 선택·실패·게시의 상세 정책은 아래 해당 절에서 정의한다. 저장·검증·Git 적용 방식은 [ARCHITECTURE.md](ARCHITECTURE.md), ExecPlan 형식은 [PLANS.md](PLANS.md), 설치와 실행은 [README.md](../README.md)를 따른다.

### 활성 변경 요구사항과 현재 동작

이 문서의 본문은 현재 Merge된 동작을 설명한다. 다음 활성 Requirement는 해당 부분을 변경할 요청이며, 계획·구현에서는 명시한 변경 범위와 유지할 보호 조건을 함께 적용한다. 문서 작성·로컬 commit만으로 구현 완료로 취급하지 않으며, 구현 PR이 Merge된 뒤 해당 장기 규칙을 본문에 반영한다.

| 변경 범위 | 적용할 Requirement |
|---|---|
| Planner 역할 선택·변경 중심 검토·최종 확인·draft와 Agent 문서 권한 | [R-049](requirements/archive/%5BR-049%5D%20Input-Specific%20Planner%20Roles.md), [R-057](requirements/%5BR-057%5D%20Change-Focused%20Planning%20and%20Final%20Full%20Verification.md), [R-061](requirements/%5BR-061%5D%20Agent%20Roles%20and%20Draft%20ExecPlan%20Decisions.md) |
| Worker 최초 level·Review 승격·Planner와 Worker의 명시적 모델 설정 | [R-052](requirements/%5BR-052%5D%20Worker%20Classification%20and%20Initial%20Level%20Calibration.md) |
| 차단 범위·비차단 로컬 base 동기화·Git 통합과 원격 보존·로컬 환경 재검사 | [R-055](requirements/%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md), [R-056](requirements/%5BR-056%5D%20Non-Blocking%20Local%20Base%20Synchronization.md), [R-060](requirements/%5BR-060%5D%20Collaborative%20Git%20Recovery%20and%20Remote%20Blocked%20Snapshots.md), [R-062](requirements/%5BR-062%5D%20Local%20Environment%20Recovery%20Command.md) |
| Merge 대기 PR이 없는 due 리팩터링의 우선 처리 | [R-054](requirements/%5BR-054%5D%20Due%20Refactoring%20Priority.md) |
| 이벤트 시점의 진행·사용자 조치 알림과 무음 구분 | [R-053](requirements/%5BR-053%5D%20Timely%20Progress%20Notifications.md) |
| 테스트별 시간·실제 Agent 없는 테스트·안전한 Python 캐시 | [R-058](requirements/%5BR-058%5D%20Test%20Timing%20and%20Agent-Free%20Test%20Execution.md), [R-059](requirements/%5BR-059%5D%20Safe%20Python%20Bytecode%20Cache%20Use.md) |
| 규칙 기반 작업의 Manager 처리·모델 호출과 입출력 감소·동일 입력 성공 결과 재사용 | [R-063](requirements/%5BR-063%5D%20Manager-Owned%20Deterministic%20Work%20and%20Minimal%20Model%20Use.md) |

R-023의 원격 실행·self handoff 목표는 유지하며 로컬 갱신 gate는 R-056, 역할별 파일 권한은 R-061을 따른다. R-055의 planning 오류와 R-061의 유효한 draft 사용자 판단 대기는 구분한다. draft는 planning 오류가 아니며 독립 작업을 막지 않지만 프로젝트 완료와 관련 Requirement archive를 허용하는 완료 상태도 아니다.

## Product Flows

### 제품 문서 작성 지원

시작 조건:
- 사용자가 PRODUCT, Requirement 또는 ARCHITECTURE 작성이나 검토를 요청한다

흐름:
1. Specification Skill이 기존 문서와 템플릿을 확인한다
2. 확정된 내용과 사용자 결정이 필요한 내용을 구분한다
3. 문서 역할에 맞는 가장 좁은 범위만 작성하거나 수정한다
4. 정상·실패 흐름, 상태 전이, 권한, Product Invariant와 edge case를 점검한다
5. 구현자가 임의로 결정해야 하는 사항이 없을 때까지 사용자와 문서를 보완한다
6. 완성된 Requirement는 `docs/requirements/` 바로 아래에 저장한다

### 프로그램 설정

시작 조건:
- 설정 파일이 없거나 사용자가 설정 모드로 실행한다

흐름:
1. 프로젝트별로 사용할 AI Agent provider를 선택한다
2. 대상 프로젝트 경로를 추가하거나 제거한다
3. 프로젝트의 필수 조건과 공통 파일을 코드로 검증한다
4. `AGENTS.md`에 Loop Manager 공통 내용이 없으면 경고한다
5. 사용자가 동의하면 파일을 바꾸지 않고 현재 `AGENTS.md`로 강제 등록한다
6. `max_workers`를 포함한 유효한 설정을 저장한다

규칙:
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING, PR_OPEN 또는 PLAN_BLOCKED ExecPlan, PROJECT_BLOCKED 레코드가 있거나 해결되지 않은 `planning_error`가 있는 프로젝트는 등록 해제할 수 없다
- Telegram token과 chat ID가 모두 비어 있으면 알림 기능을 비활성화하고 전송을 시도하지 않는다
- Telegram token과 chat ID 중 하나만 설정돼 있으면 설정 오류로 처리한다

실패:
- 선택한 AI Agent가 실행 불가하거나 인증돼있지 않다
- Model, Effort, 일부만 입력된 Telegram 설정 또는 `max_workers` 설정이 유효하지 않다
- 프로젝트가 등록 조건을 만족하지 않는다
- `docs/PLANS.md`가 Loop Manager 기준 파일과 일치하지 않는다

### ExecPlan 생성과 자동 처리

시작 조건은 미충족 PRODUCT·Requirement 또는 이어서 처리할 기존 작업이 있는 경우다.

1. 저장된 작업과 대기 PR을 확인한다. 안전 조건이 허용한 Merge·Close 반영을 먼저 수행하고, 최신 원격 입력과 catalog를 확인한다.
2. 기존 변경 작업과 확정 PR 충돌 복구를 우선한다. 유지보수·일반 Planner·READY 선택은 아래 작업 선택 정책을 따른다.
3. Planner가 현재 명세·코드·compact catalog와 허용된 미착수 READY snapshot을 확인한다. 새 작업을 계획하고 허용된 기존 계획만 조정한다.
4. 프로그램이 범위·형식·source·의존성을 검증하고 계획을 원격 base에 게시한다. 원격 게시를 확인한 결과만 실행 대상으로 반영한다.
5. dependency를 충족한 READY 하나를 선택해 전용 branch·worktree에서 Worker를 실행한다. Worker는 구현·관련 테스트·verify 스크립트와 해당 Requirement archive를 준비한다.
6. 프로그램이 Worker 결과와 archive 이동을 검증하고 최신 base를 반영한다. 최종 후보의 전체 검증과 읽기 전용 Review를 수행하며 실패는 같은 작업에서 교정한다.
7. 게시 직전 base와 archive eligibility가 바뀌었으면 필요한 교정·후보 확인·검증·Review를 다시 수행한 뒤 기존 PR을 복구하거나 새 PR을 생성한다.
8. 안정된 PR_OPEN은 실행 자리를 비운다. 해당 계획에 직접·간접 의존하지 않는 READY는 순차적으로 시작할 수 있고, 연결 PR의 Merge 확인 뒤 의존 작업이 시작된다.

계획 게시의 원자 적용, 작업 공간 복구와 단계별 Git 처리는 [ARCHITECTURE.md](ARCHITECTURE.md#계획-게시와-작업-공간)를 따른다.

### 실패와 재시도

Planner 결과 검증 실패:
1. 변경 범위 또는 ExecPlan 형식 오류를 같은 Planner 세션에 전달한다
2. 최대 3회까지 수정과 재검증을 수행한다
3. 계속 실패하면 계획 입력 지문과 원인을 `planning_error`에 저장하고 planning worktree를 보존한다
4. 같은 계획 입력 지문에서는 Planner를 다시 실행하지 않는다
5. 원격 입력이 전진하면 이전 planning worktree·검증 기록·세션을 보존해 반영한다. 오류 원인이 실제로 해소됐는지 재검증하기 전에는 `planning_error`를 지우지 않는다

ExecPlan 게시 실패:
1. 계획 commit을 push하기 전에 `origin/<base-branch>`가 전진하면 미게시 후보를 보존해 관리 worktree에서 rebase하고 최신 catalog·SQLite 권한과 결과를 재검증한다
2. fast-forward push가 거부되면 계획 commit과 오류를 보존하고 다음 실행에서 다시 시도한다
3. push 성공을 원격에서 확인하기 전에는 ExecPlan을 READY로 등록하거나 Worker를 시작하지 않는다
4. 일반·유지보수 batch의 prepared intent와 planning worktree는 push·SQLite 적용 실패 뒤에도 보존한다. 원격에 batch가 있으면 원자 적용을 재개하고, 없으면 보존 후보를 최신 base로 옮겨 재검증한다

전체 검증 또는 Review 실패:
1. 프로그램이 전체 검증 실패 내용 또는 Reviewer의 blocking issue를 같은 Worker 세션에 전달하고 Reviewer도 저장된 세션을 재사용한다
2. 전체 검증 성공 출력은 Worker에게 전달하지 않는다
3. 전체 검증과 Review 출력을 결정적으로 정규화해 단계별 실패 지문과 발생 횟수를 SQLite에 저장한다
4. 전체 verify의 실패 지문 이력은 Review와 독립적으로 유지한다. 새로운 verify 지문이 나타나는 동안에는 총 실패 횟수와 관계없이 Worker 수정과 전체 검증을 계속한다
5. 같은 verify 지문이 Worker 수정 후 세 번 다시 발생하면 구조화된 원인과 지문을 기록하고 ExecPlan을 `PLAN_BLOCKED`로 변경하며, 중간에 다른 지문이 발생해도 이전 이력을 유지한다. Review의 ISSUE_ID와 진단·해결 이력은 피드백과 감사에 보존하되 발생 횟수만으로 최종 차단하지 않는다
6. 한 외부 실행에서 계획별 Review FAIL에 따른 Worker 교정은 최대 세 번 시작한다. 복수 issue는 한 교정으로 세며 CLI 재시도와 중단된 동일 turn의 복구는 중복 집계하지 않는다. 세 번째 교정의 verify·Review가 성공하면 게시를 계속하고, 추가 교정이 필요하면 Worker 호출 전에 phase·작업물·세션·피드백을 보존해 이번 실행을 일시 중단한다. 다음 외부 실행은 저장된 피드백과 세션으로 재개하며 이 중단은 BLOCKED·checkpoint·차단 알림을 만들지 않는다
7. 유효한 Review FAIL이 같은 effective level에서 동일 지문으로 두 번 확인되거나 서로 다른 지문 세 개로 확인되면 다음 Worker 수정부터 level을 한 단계 올리며 level 8을 상한으로 한다
8. 전체 verify 실패, Worker 결과 형식 오류, Reviewer 실행 오류와 유효한 FAIL이 없는 결과는 effective level과 Review 승격 이력을 변경하지 않는다
9. 전체 검증 성공은 verify 지문 이력만, Review 성공은 Review 지문 이력만 초기화하며 effective level과 level별 승격 이력은 유지한다

과거 `review_retry_exhausted` 차단은 저장된 원인과 현재 작업 공간·branch·원격 OID·Agent 범위·PR·rebase 상태를 재확인한다. Review 횟수 외 유효한 차단이 없고 소유권이 확인되면 기존 세션·실패 피드백·감사를 보존한 채 같은 교정 경로로 재개한다. 안전 근거가 부족하거나 별도 차단 원인이 있으면 그 상태를 유지한다.

PR 없는 게시 checkpoint의 과거 Review 횟수 차단은 현재 게시 소유권과 작업 범위를 재검증해 해당 원인만 해제한다. 이 복구는 수행하지 않은 rebase의 완료 증거를 만들지 않으며, 남은 Review issue와 다른 차단은 보존한다.

외부 CLI 실패:
- 실행 실패, timeout 또는 일시적 외부 오류는 현재 프로그램 실행에서 최대 3회까지 재시도한다
- 3회 재시도 후에도 실패하면 상태와 작업 공간을 보존하고 해당 프로젝트 처리를 이번 실행에서 중단한다
- 다음 Loop Manager 실행에서 같은 상태부터 다시 시도한다
- AI 사용 한도 소진이 확인되면 현재 실행에서는 재시도하지 않고 즉시 다음 프로젝트로 넘어간다

### 계획 및 프로젝트 차단 해제

| 차단·변경 조건 | 허용되는 처리 |
|---|---|
| PLAN_BLOCKED | 해당 계획의 입력·guidance 결정·같은 PR reopen 조건만 재평가한다. 독립 READY는 계속 선택할 수 있다. |
| PROJECT_BLOCKED | 일반 Agent·Git 변경을 멈춘다. PR 상태·계획별 원격 관측·알림은 계속하며, 실행 자리와 현재 범위가 검증된 연결 계획만 복구·보존할 수 있다. |
| 안전한 원격 후속 commit 또는 base 전진 | 기존 branch·worktree·session과 로컬 작업을 보존해 반영하고 원래 차단 원인을 다시 검사한다. 새 OID나 checkpoint 게시만으로 해제하지 않는다. |
| 삭제·재작성·경쟁·소유권 불명확 또는 미해결 충돌 | 원격 ref를 덮지 않고 차단·작업물과 실제 미게시 이유를 보존한다. |

1. 최종 차단 상태와 원인을 먼저 보존한다. PR 없는 검증 가능한 대상 작업은 안전한 원격 checkpoint 또는 별도 진단 branch에 보존하고 결과를 알린다.
2. 저장된 원인과 현재 입력·작업 상태를 대조한다. 같은 입력의 실패를 반복하지 않으며 다른 계획·planning·사용자 결정의 차단을 함께 해제하지 않는다.
3. 일반적인 입력·승인·PR reopen 해제는 해당 계획의 재시도·실패 지문과 Worker level을 초기화해 READY 또는 PR_OPEN으로 되돌린다. 원격 복구는 기존 session·실패·승격 감사를 보존한다.
4. 후보가 바뀐 작업은 Worker의 현재 입력·최종 diff 확인과 targeted test, 프로그램 전체 verify·Review를 다시 통과한다. 첫 PR도 최종 검증과 Review 성공 뒤에만 생성한다.

현재 상태 기반 rebase 판정, checkpoint lease, 진단 ref와 중단 복구의 상세 조건은 [ARCHITECTURE.md](ARCHITECTURE.md#차단-작업의-원격-반영과-보존)를 따른다.

### 사용자 승인 및 완료

1. 사용자가 PR을 검토하고 Merge한다. Merge 없이 닫힌 PR은 해당 계획만 차단하며 같은 PR을 다시 열어야 한다.
2. 프로그램이 실제 Merge를 확인해 MERGED와 알림을 기록한다. 관리 worktree는 아래 정리 조건을 만족할 때만 제거한다.
3. 최신 원격 base에서 안전한 MERGED ExecPlan을 내용 보존 archive로 게시한다. 정리 실패는 작업 완료를 되돌리거나 다음 작업을 막지 않는다.
4. 다음 의존 작업을 시작하거나 Planner의 추가 작업 없음과 프로젝트 완료 gate를 확인한다.
5. 모든 완료 조건이 충족되면 프로젝트 완료 이벤트를 전송한다. Merge된 PR의 Requirement archive는 활성 입력에서 제외된다.

### 실행 종료 및 재개

1. 같은 사용자 상태 디렉터리의 다른 Loop Manager가 실행 중이면 상태를 바꾸지 않고 종료한다
2. 각 프로젝트의 PR, worktree와 저장된 실행 상태를 외부 상태와 대조한다
3. 기본 worktree의 dirty, staged, untracked, ahead, diverged, detached 또는 다른 branch 상태는 원격 SHA 기반 작업을 막지 않는다. safe fast-forward 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
4. 프로젝트별로 하나의 작업만 선택하고 전체 동시 실행 수를 `max_workers`로 제한한다
5. 처리 중인 상태, 세션과 작업 공간을 SQLite에 저장한다
6. 중단된 작업은 다음 실행에서 저장된 상태부터 재개한다

## States and Transitions

ExecPlan 실행 상태는 파일이 아니라 SQLite에만 저장한다.

```text
READY → IMPLEMENTING → VERIFYING → REVIEWING → PR_OPEN → MERGED
VERIFYING 또는 REVIEWING → IMPLEMENTING (재시도)
READY, IMPLEMENTING, VERIFYING 또는 REVIEWING → PLAN_BLOCKED
PLAN_BLOCKED → READY (계획별 해제 조건 충족)
PR_OPEN → PLAN_BLOCKED (Merge 없이 PR 종료)
PLAN_BLOCKED → PR_OPEN (같은 PR을 사용자가 다시 열음)
```

- READY: 유효한 ExecPlan이 등록돼 프로젝트 실행 순서와 `depends_on` 충족을 대기
- IMPLEMENTING: Worker가 구현 또는 실패 수정을 진행 중
- VERIFYING: 전체 검증 스크립트 실행 중
- REVIEWING: Reviewer가 blocking issue를 확인 중
- PR_OPEN: PR 생성 후 사용자 Merge 대기
- MERGED: 연결된 PR의 Merge가 확인된 최종 상태
- PLAN_BLOCKED: 특정 계획의 사용자 결정, 입력 변경, 검증 한도 또는 PR reopen을 기다림

`PROJECT_BLOCKED`는 ExecPlan 상태가 아니라 프로젝트 안전 gate다. 현재 단계와 작업 공간을 보존하고 일반 Planner·유지보수 Planner·Worker·Git 변경을 멈춘다. PR 상태·원격 관측·알림과 검증 가능한 연결 계획의 복구·보존 예외는 위 차단 정책을 따른다.

유지보수 cycle은 ExecPlan 상태와 별도로 프로젝트별 SQLite에 보존한다.

```text
IDLE → ASSESSMENT → IDLE (평가 없음)
ASSESSMENT → PREREQUISITE_WAIT → ASSESSMENT
ASSESSMENT → REFACTORING → POST_REFACTOR_REVALIDATION → IDLE
```

활성 작업은 cycle 시작보다 우선하며, 각 cycle 전이는 기록된 계획 집합의 Merge 또는 검증된 Planner 결과로만 진행한다.

외부 CLI의 일시적 실패와 사용 한도 소진은 별도 ExecPlan 상태를 만들지 않고 현재 상태를 보존한다.

`planning_error`, `cleanup_pending`과 ExecPlan archive pending은 ExecPlan 상태가 아니다. `planning_error`는 해당 프로젝트의 새 계획과 완료 처리를 막지만, `cleanup_pending`과 archive pending은 이미 Merge된 작업의 보존·재시도 정보이므로 다음 ExecPlan 실행과 프로젝트 완료를 막지 않는다.

## Business Rules

### PRODUCT, Requirement와 ExecPlan

- `PRODUCT.md`는 현재 Merge된 제품 동작, 경계와 장기 규칙의 기준이다
- Requirement는 원하는 변화·이유·Acceptance Criteria만 기록하며 frontmatter·상태·complexity·risk를 두지 않는다
- 기존 ExecPlan의 활성 Requirement source가 없으면 동일 파일명의 `docs/requirements/archive/` 문서를 조회하되 원래 source 경로는 바꾸지 않는다
- 새 ExecPlan의 source와 Planner 입력은 활성 PRODUCT 또는 `docs/requirements/` 바로 아래 문서로 제한한다
- 현재 ExecPlan을 제외한 모든 관련 ExecPlan이 MERGED인 Requirement만 archive 후보이며, 이미 모든 관련 계획이 MERGED인 과거 Requirement도 후보에 포함한다
- archive 이동 집합의 결정적인 차이는 같은 Worker 문맥에서 한도 내 교정하고, 최신 base·catalog 시차는 신뢰할 수 있는 원본으로 재평가한다. 이동 목적지 충돌이나 source 관계·상태를 끝내 하나로 확정할 수 없을 때만 덮어쓰기 없이 현재 ExecPlan을 `PLAN_BLOCKED`로 만든다
- `PLAN_BLOCKED` 해제 지문은 원래 활성 Requirement 경로만 사용하고 archive 변경에는 반응하지 않는다
- Requirement는 PRODUCT의 변경 요청이며 ExecPlan의 구현 상세나 실행 상태를 포함하지 않는다
- Requirement 파일 경로를 안정적인 식별자로 사용한다
- `docs/requirements/` 바로 아래의 모든 `*.md`를 Planner 입력으로 사용한다
- `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 Planner 입력에서 제외한다
- ExecPlan의 `sources`에는 근거가 된 PRODUCT 또는 Requirement 경로가 있어야 한다

- source와 dependency 변경은 같은 reconciliation 결과를 다음 archive 후보·입력 지문·READY 선택에 사용하고 priority 변경은 다음 선택부터 반영한다. complexity·risk 변경은 최초 Worker level을 다시 계산하되 실행 이력이 있는 계획의 effective level과 Review 승격 이력을 낮추지 않는다
- 현재 path와 title은 compact catalog, 로그·알림과 이후 파일 조회에 사용하지만 기존 PR의 ExecPlan 링크는 최초 게시 SHA와 최초 active path를 계속 사용한다
- 최초 또는 비교 가능한 전체 계획 성공 기준이 없거나, PRODUCT·활성 Requirement·ARCHITECTURE·AGENTS·사용자 active ExecPlan이 바뀌면 일반 Planner를 READY 선택보다 먼저 실행한다. 일반 Planner는 비종료 compact catalog와 미착수 READY snapshot으로 현재 식별 가능한 미충족 작업을 한 번에 계획한다
- 문서·지침·active ExecPlan은 그대로이고 코드·테스트·설정만 바뀌면 수정 가능한 미착수 READY를 읽기 전용 점검 Planner가 먼저 확인한다. dependency 대기 READY도 포함하며 실행 이력·소유 증거가 있는 계획은 대상에서 제외한다. 대상이 없으면 일반 Planner가 전체 미충족 작업을 확인한다
- 점검의 수정 불필요 결과는 같은 입력과 대상에서 재사용하고 일반 Planner 없이 기존 READY 선택을 이어간다. 수정 필요·판단 불확실은 근거와 대상을 일반 Planner에 인계한다. 점검 성공은 예정 작업의 유지 가능성만 확인하며 전체 계획·유지보수 성공 기준이나 프로젝트 완료를 확정하지 않는다
- `docs/FUTURE_IDEAS.md`는 계획 입력 지문, 활성 Requirement와 프로젝트 완료 판정에서 제외하며 그 항목을 자동으로 Requirement 또는 ExecPlan으로 승격하지 않는다
- MERGED ExecPlan은 Planner 입력에서 제외하며 프로그램만 ID 고유성, 의존성 충족과 감사 이력을 위해 관리한다
- Planner와 Worker의 실행 후보는 `docs/plans/` 바로 아래의 active ExecPlan으로 제한하고, `docs/plans/archive/`는 ID 고유성, 다음 번호, dependency, 복구와 감사 검증에만 포함한다
- ExecPlan archive-only 이동은 계획 입력 지문, 유지보수 Merge count, 프로젝트 완료 cycle을 바꾸지 않는다
- ExecPlan의 `depends_on`은 선행 ExecPlan ID 목록이며, 모든 선행 상태가 MERGED여야 Worker를 시작할 수 있다
- Worker는 장기 제품 규칙이 바뀌는 경우에만 코드와 같은 PR에서 `PRODUCT.md`를 갱신한다

### 프로젝트 등록 및 실행 조건

- 대상 경로는 origin이 GitHub 저장소인 Git 프로젝트여야 한다
- fetch된 `origin/<base-branch>` tree에 `AGENTS.md`, `docs/PLANS.md`, `docs/ARCHITECTURE.md`가 있어야 한다
- fetch된 `origin/<base-branch>` tree에 `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 Requirement가 하나 이상 있어야 한다
- 변경되지 않은 PRODUCT/ARCHITECTURE 템플릿만 있는 프로젝트는 작업 가능 상태로 등록하지 않는다
- `docs/PLANS.md`는 Loop Manager 기준 파일과 SHA-256 해시가 같아야 한다
- 공통 내용이 없는 `AGENTS.md`는 사용자 확인 후 강제 등록할 수 있다
- `AGENTS.md` 포함 검사와 `docs/PLANS.md` 해시 검사는 프로젝트 등록 시 선택한 원격 commit blob에만 수행하며 자동 Loop에서는 반복하지 않는다
- 자동 작업의 기준은 `origin/<base-branch>`다
- 기본 worktree의 로컬 변경, checkout branch와 ahead·diverged 이력은 자동 작업을 막지 않으며 local-only 내용은 자동 입력에 포함하지 않는다

- 기본 worktree의 안전한 동기화가 불가능하면 이유를 기록하고 로컬 상태를 보존하며 원격 SHA 기반 작업을 계속한다. 검증된 safe fast-forward 외의 기본 worktree 변경은 수행하지 않는다.

- Loop Manager가 만드는 계획과 구현 변경은 대상 프로젝트 밖의 Loop Manager 관리 경로에 만든 별도 worktree와 branch에만 기록한다
- 자동 실행 중 기본 worktree에서는 검증된 `git merge --ff-only` 상당의 safe fast-forward 외에 checkout, pull, reset, stash, clean, commit과 파일 생성을 수행하지 않는다

### 작업 선택과 동시 실행

- 실행 중인 변경을 먼저 재개한다. 없으면 dependency를 충족한 확정 PR 충돌 복구를 일반 Planner·유지보수 평가·READY 선택보다 먼저 수행한다.
- Merge 대기 PR이 있으면 새 유지보수 평가는 보류한다. 독립 계획의 PLAN_BLOCKED 자체는 단계별 유지보수 평가를 막지 않으며, PROJECT_BLOCKED와 현재 변경 실행·공유 안전 gate는 유지한다.
- 같은 프로젝트에 여러 READY ExecPlan이 대기할 수 있다
- 같은 프로젝트에서는 IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING 또는 PR 충돌 복구 중인 ExecPlan 하나만 현재 변경 실행으로 둔다. `PLAN_BLOCKED`와 충돌 복구 중이 아닌 `PR_OPEN`은 이 자리를 점유하지 않는다
- 새 계획 입력은 실행 중인 작업을 선점하지 않는다. 문서·지침·사용자 active ExecPlan 변경은 다음 안전한 선택 경계에서 일반 Planner로, 코드 전용 변경은 수정 가능한 READY가 있으면 점검 Planner로 READY 시작 전에 반영한다
- READY ExecPlan은 모든 `depends_on`이 MERGED이고 다른 현재 작업이 없을 때만 시작한다
- dependency를 충족하지 못한 READY만 남아도 변경 입력에 해당하는 Planner 역할을 한 번 실행하고, 확인 기준과 입력·대상이 같으면 대기한다
- 제품 우선순위는 안전한 무인 실행을 막거나 반복적인 사용자 복구를 요구하는 작업, 정상 경로의 사용자 개입을 제거하는 핵심 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순이다
- 실행 가능한 READY ExecPlan이 여럿이면 제품 우선순위가 높은 계획을 먼저 선택하고 같은 우선순위에서는 숫자 ExecPlan ID가 가장 작은 계획을 선택한다
- 활성 유지보수 prerequisite 집합은 dependency gate 뒤 priority와 숫자 ID 순으로, 리팩터링 DAG는 dependency gate 뒤 숫자 ID 순으로 선택하며 집합 밖 READY는 cycle이 끝날 때까지 시작하지 않는다. 이때도 `PROJECT_BLOCKED`는 일반 실행을 막고, 다른 `PLAN_BLOCKED`·`PR_OPEN`과 dependency로 연결된 계획은 건너뛴다
- 마지막 리팩터링 계획 Merge 뒤에는 최신 원격 기준 사후 재검증을 성공하고 계획 개정의 원격 게시와 catalog 반영을 마칠 때까지 장벽 뒤 READY를 시작하지 않는다
- 각 ExecPlan은 고유하고 결정적인 이름의 branch와 별도 관리 worktree에서 작업한다
- 서로 다른 프로젝트는 `max_workers` 범위에서 병렬로 처리한다

- Loop Manager 자체 저장소의 새 PR Merge가 확인되고 등록 base의 최신 원격 SHA가 실행 runtime의 fast-forward 후속이면 현재 실행을 정상 종료한다. 다음 외부 실행이 최신 runtime을 시작하며 handoff 뒤에는 새 Agent·verify·게시·다른 프로젝트 작업을 시작하지 않는다.
- self Merge 없음, 다른 프로젝트 Merge와 일반 push는 handoff 근거가 아니다. repository·origin·PR base·fetch·ancestry를 확인할 수 없으면 성공으로 처리하지 않고 기존 상태를 보존한다.

- `max_workers` 기본값은 3이며 `config/config.ini`에서 변경할 수 있다
- PLAN_BLOCKED 또는 안정된 PR_OPEN이 있어도 명세·전체 계획 입력 변경 trigger를 먼저 평가한다. 입력이 같으면 독립 READY 하나를 기존 priority와 숫자 ID 순으로 시작하며, 없으면 대기·완료 조건을 평가한다
- `PROJECT_BLOCKED` 동안에는 새 Planner, maintenance Planner, Worker와 일반 저장소 변경 단계를 시작하지 않는다. 연결 계획이 기존 실행 자리를 점유했거나 자리가 비어 있고 현재 작업 범위를 독립적으로 검증한 경우에만 해당 관리 worktree의 원격 복구·보존 Git 쓰기를 허용한다

### 유지보수 평가

- 기존 활성 cycle을 먼저 복구·완료하고 과거 성공 이력은 유지한다. 새 평가는 최신 원격 입력을 정적으로 확인해 모델 없는 `fast`, 영향받은 모듈의 `targeted`, 전체 모듈의 `full` 점검 중 필요한 범위를 선택한다.
- 첫 전체 정밀 점검은 초기 입력에서 수행하며, 이후 마지막 성공한 전체 정밀 점검 이후 새 MERGED 수가 `refactoring_review_interval`에 도달하거나 사용자가 전체 점검을 요청하면 다시 수행한다. 설정은 양의 정수이며 기본값은 20이다. 정적·부분 점검은 이 전체 정밀 기준을 전진시키지 않는다.
- 주기 사이에는 같은 입력에서 평가 모델을 반복 호출하지 않는다. 변경은 정적으로 확인하고 공개 계약·상태·저장·복구·권한·관계 불확실성·순환·중복·루트 배치 등 확인이 필요한 영향에는 관련 모듈과 테스트를 포함한 정밀 점검을 수행한다. 미확인 사실을 성공한 의미 검토로 만들지 않는다.
- 원본에 결합된 성공 모듈 검토는 해당 묶음의 입력이 같을 때 재사용한다. 검토에서 발견이 없고 일반 계획을 함께 수행할 필요도 없으면 별도 종합·계획 호출을 하지 않는다. 발견이 있으면 새 계획·기존 비종료 계획·근거 있는 제외에 연결해 처리한다.
- 리팩터링은 동작·공개 계약·저장 형식을 보존한다. 평가 성공·게시·사후 재검증이 실제로 완료되기 전에는 성공 기준을 전진시키지 않는다.
- 종전 파일별 평가·게시 결과를 보존한 cycle은 해당 검증·복구 경로로 마친다. 그 이력과 완료 후보의 종전 안정화 판정을 새 단계별 평가의 기본 반복 조건으로 사용하지 않는다.

### 복구 판단과 최종 차단

- 차단 후보는 disposition, 안정적인 원인 code, plan ID, phase, plan/base revision, 진단 지문과 진전 marker로 기록한다. 복구 disposition과 `PLAN_BLOCKED`/`PROJECT_BLOCKED` scope는 별도 기준이며 최종 사용자 조치가 확정된 뒤에만 원인 code로 scope를 정한다
- 일시적 Git·서비스 실패는 현재 실행을 보존해 다음 실행에서 재시도하고, 유효한 최신 base·catalog 및 실제 소유 Git 상태로 하나의 다음 단계를 확정할 수 있으면 상태를 재구성한다. archive 이동 집합과 rebase 충돌은 진전과 반복 한도를 기록하며 교정한다. Manager가 처음 rebase 충돌을 관측하거나 Worker 결과 형식 오류를 받으면 현재 실행을 끝내고 다음 실행에서 보존된 Worker 세션으로 재개한다. 진행 중인 Worker·검증·Review 교정은 같은 실행에서 이어갈 수 있다
- 일시 재시도, 상태 재구성, 한도 내 Agent 교정은 현재 phase, branch, worktree, 소유권, Agent session, 승인, 변경, 검증 원문과 실패 이력을 보존하며 차단, 원격 checkpoint, BLOCKED 알림을 만들지 않는다. 한 실행에서 최신 원격 snapshot이 안정되지 않으면 같은 상태로 일시 중단하고 다음 실행에서 다시 판정한다
- 승인·PR reopen·소유권·Agent 범위·원격 OID 불확실성, 안전한 원본으로 확정할 수 없는 손상, verify·rebase·Worker 결과 형식·Reviewer 프로토콜의 최종 반복 한도 소진과 의미를 확정할 수 없는 무변경 결과만 최종 사용자 조치로 차단한다. Review 유발 Worker 교정의 실행별 한도는 작업을 보존하는 일시 중단으로 처리한다. 최종 기록에는 복구 횟수, 마지막 안정 revision, 남은 불확실성과 실제 조치 대상을 포함한다
- 최종 `PLAN_BLOCKED`와 계획에 연결된 `PROJECT_BLOCKED`의 검증 가능한 대상 작업만 안전한 원격 checkpoint 또는 진단 게시 대상이다. BLOCKED 알림은 최종 차단 transaction과 checkpoint 결과 뒤에 생성하고 동일 plan/scope/reason/fingerprint/revision은 한 번만 알린다. 사용자 수정 경로는 원인이 실제 대상을 제공할 때만 표시한다

### Model과 Effort 선택

- Planner가 ExecPlan의 complexity와 risk를 판단한다
- 프로그램은 complexity를 low=1, medium=3, high=5로 환산한다
- 프로그램은 risk를 low=0, medium=1, high=2, critical=3으로 환산한다
- 두 점수의 합을 최대 7로 제한해 `level1`부터 `level7`까지의 Worker level로 사용한다
- 정확한 Model 이름과 Effort는 `config/config.ini`에서 관리한다
- 최초 Worker level 계산은 level 8을 반환하지 않는다
- 유효한 Review FAIL의 저장된 반복 진행만 effective level을 한 단계씩 올릴 수 있으며 한 ExecPlan 안에서는 level을 낮추지 않는다
- Codex level 8은 `gpt-6-astra,low`, Claude level 8은 Opus strong alias의 `max`를 사용하고 level 8보다 높은 자동 profile은 없다
- effective level과 level별 Review 승격 이력은 SQLite에 저장하고 새 ExecPlan 또는 정상적인 BLOCKED 해제에서 최초 level로 초기화한다

### PR 생성과 Merge 후 정리

- Worker는 현재 환경의 전체 검증 스크립트를 생성·유지하고, 프로그램이 Agent 밖에서 실행한다.
- Loop Manager 자체 verify는 동일한 unittest manifest와 fixture 계약을 직렬·프로세스 병렬 모드에서 유지하고 전체 compile을 포함한다. 실제 Agent 실행을 테스트 경계에서 차단하며 테스트·fixture·대기·정리의 시간과 중단 진단을 성공 stdout과 분리해 보존한다.
- 자체 verify의 `verify_parallelism`은 생략 시 `auto`, 양의 정수는 상한, `1`은 직렬이다. 실행량은 코드가 CPU 제한·실제 부하·메모리 여유와 미반영 예약으로 결정하며 등록 프로젝트 수로 고정 분할하지 않는다. 관측 실패는 직렬로 축소하고 자원 대기도 전체 timeout에 포함한다.
- Manager 실행 인스턴스가 동시 verify의 확인·예약을 조율한다. 예약은 해당 자식 종료 또는 미시작을 확인한 뒤 해제하며 종료 불명은 보존한다. 기존 프로젝트 선택과 같은 프로젝트의 ExecPlan 동시 실행 정책은 유지한다. 과거 시간 자료는 현재 후보 검증을 대신하지 않는다.
- 최종 후보에서 관련 targeted test와 전체 verify가 통과하고 Reviewer blocking issue가 없어야 한다.
- 최신 base 반영이나 후보·관련 입력 변경으로 기존 성공 근거가 무효화되면 Worker 후보 확인·targeted test·전체 verify·Review를 다시 수행한다.
- 게시 직전 archive eligibility가 이동 집합과 다르면 같은 Worker 세션에서 교정하고 검증·Review를 반복한다.
- 관리 rebase는 자동 Worker 해결과 사용자 수동 해결을 같은 현재 상태 기준으로 수용한다. 현재 소유권·범위·Git 상태·원격 기대값을 검증하며, 과거 turn·stage·receipt·journal 완전성을 완료 조건으로 요구하지 않는다.
- PR 없는 동일 계획의 기존 원격 작업 branch를 rebase한 결과는 다음 Worker·verify·Review·추가 rebase보다 먼저 중간 checkpoint 게시 또는 안전한 거부를 확정한다. 이 게시는 차단 해제·검증 성공·PR 승인을 대신하지 않는다.
- 같은 branch의 기존 PR을 복구해 사용하며 중복 PR을 만들지 않는다. PR은 사용자가 Merge한다.

| Merge 후 작업 공간 | 처리 |
|---|---|
| clean이고 PR head에 포함되지 않은 로컬 commit 없음 | Merge·Squash·Rebase 방식과 원격 head branch 존재 여부에 관계없이 정리한다. |
| 미커밋 변경·미게시 로컬 commit 또는 안전성 불확실 | 경로·원인을 cleanup_pending에 기록하고 보존한다. 이후 안전 조건이 충족되거나 경로가 사라지면 해제한다. |

게시 lease·중단 복구·후보 검증 토큰의 적용은 [ARCHITECTURE.md](ARCHITECTURE.md#관리-rebase와-후보-검증)를 따른다.

### 프로젝트 완료 조건

- Planner가 현재 PRODUCT와 활성 Requirement에서 추가 ExecPlan이 필요 없다고 판단해야 한다
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING, PR_OPEN 또는 PLAN_BLOCKED 상태의 ExecPlan과 PROJECT_BLOCKED 레코드가 없어야 한다
- due 또는 활성 유지보수 cycle과 해결되지 않은 유지보수 planning error가 없어야 하며, 성공 기준선이 최신 평가 또는 사후 재검증 결과까지 전진해야 한다
- 해결되지 않은 `planning_error`가 없어야 한다
- `cleanup_pending`은 프로젝트 완료를 막지 않는다
- Requirement와 ExecPlan에는 별도의 초안 상태를 두지 않는다
- 프로젝트 완료 알림은 프로젝트별로 관리한다
- 새로운 PRODUCT/Requirement 변경으로 작업 주기가 시작되고 다시 완료되면 새 완료 알림을 전송한다
- Requirement archive 이동만으로 완료 알림을 다시 활성화하지 않는다
- 안정적인 작업 선택에서 Planner가 최신 입력에 추가 작업이 없음을 확인하고 활성 Requirement·비종료 ExecPlan·프로젝트 차단·planning error·활성 또는 due 유지보수 작업이 모두 없을 때만 완료로 판정한다
- 시작 가능한 계획이 없지만 활성 Requirement 또는 비종료 ExecPlan이 남으면 계획별 dependency 상태, PR Merge 대기, 구조화된 차단 원인과 확인된 사용자 조치, 활성 Requirement의 대기 이유를 결과·로그에 남기고 별도 대기 사건으로 한 번 알린다
- 실행 가능한 READY, 진행·복구 중인 단계, 일시 오류 대기에는 작업 소진 알림을 만들지 않으며, 기존 BLOCKED 사건만으로 설명되는 상태는 대기 알림을 중복 생성하지 않는다

### Telegram 알림 조건

- Telegram token과 chat ID가 모두 설정된 경우에만 알림을 생성하고 전송한다
- 두 값이 모두 비어 있으면 알림을 비활성화하며 작업 흐름에는 영향을 주지 않는다
- PR 생성 알림에는 PR 링크가 포함돼야 한다
- 차단 알림에는 scope, 구조화 원인, 관련 ExecPlan, 실제 수정 대상이 확인된 경우에만 그 경로, checkpoint 성공 시 원격 commit link·OID 또는 미게시 시 local-only 보존 이유가 포함돼야 한다
- 차단 알림은 plan·scope·원인·지문·revision이 같을 때 같은 사건으로 중복 제거한다
- 미해결 `planning_error`는 같은 입력의 반복 실행·재시작, 다른 계획·PR 상태 변경과 단순 원격 base 전진에도 같은 차단 사건을 유지한다. 실제 planning 오류 해제와 사건 해제·미전송 알림 취소를 하나의 SQLite transaction에 기록하며, 소유권 확인이나 정리가 실패하면 모두 미해결로 보존한다. 과거 중복 사건과 전송 감사 기록은 삭제하지 않고 현재 사건의 추가 분할과 중복 알림을 막는다
- BLOCKED 알림의 한 조각 이상 전송이 확인된 차단이 해제되고 해당 계획의 Worker·PR 처리 또는 프로젝트·Planner의 정상 작업이 실제로 시작되면, 이전 scope·계획·원인과 시작한 작업을 담은 재개 알림을 사건당 한 번 전송한다. 단순 해제, 다른 계획의 시작, 미전송 차단과 Telegram 비활성화에는 만들지 않으며 해제된 차단의 미전송 조각은 취소한다. 같은 원인으로 별도 통지된 이후 차단은 새 사건으로 취급한다
- 프로젝트 완료 알림은 해당 프로젝트의 현재 작업 주기가 완료됐을 때 전송한다
- 완료 알림에는 Planner의 추가 작업 없음 확인, 활성 Requirement 없음과 마지막 완료 계획을 포함한다
- 같은 안정적 대기 원인은 재실행·재시작 뒤 중복 알리지 않고, 작업 진행 또는 관련 입력·차단 원인이 바뀐 새 대기 상태는 한 번 다시 알릴 수 있다
- Telegram이 비활성화되거나 전송에 실패해도 완료·대기 이유는 실행 결과와 로그에 남는다
- 동일한 이벤트는 한 번만 전송하며 실패한 알림만 다음 실행에서 재시도한다
