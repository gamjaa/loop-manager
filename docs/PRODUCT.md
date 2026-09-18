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

- 여러 로컬 GitHub 프로젝트를 등록하고 관리할 수 있다
- 프로젝트 공통 파일을 코드로 검증하고 사용자 선택으로 `AGENTS.md` 불일치를 승인할 수 있다
- Specification Skill로 PRODUCT, Requirement와 ARCHITECTURE 작성을 도울 수 있다
- PRODUCT와 Requirement를 읽어 필요한 구현 작업을 하나 이상의 ExecPlan으로 작성할 수 있다
- 현재 입력에서 식별 가능한 구현 작업 전체를 Worker 시작 전에 ExecPlan 카탈로그로 만들고, 안전한 무인 자동화에 기여하는 순서로 처리할 수 있다
- ExecPlan의 고정 메타데이터와 의존성을 코드로 파싱해 실행 가능 여부, Model과 Effort를 결정할 수 있다
- ExecPlan 실행 상태, 재시도 횟수, AI Agent 세션과 작업 공간을 SQLite로 관리할 수 있다
- Worker가 구현, 관련 테스트와 프로젝트별 전체 검증 스크립트를 작성할 수 있다
- 전체 검증과 독립 Review를 자동으로 수행하고 실패 시 기존 작업을 이어서 수정할 수 있다
- 검증이 완료된 작업으로 GitHub PR을 생성할 수 있다
- 같은 프로젝트의 작업은 직렬로, 서로 다른 프로젝트는 설정된 동시 실행 수 안에서 병렬로 처리할 수 있다
- 변경되지 않은 프로젝트에는 Planner를 불필요하게 다시 실행하지 않을 수 있다
- PR 생성, BLOCKED 발생과 프로젝트 완료를 Telegram으로 알릴 수 있다
- 프로그램 중단 후에도 저장된 상태에서 작업을 재개할 수 있다

### Non-Goals

- 같은 프로젝트 안의 병렬 작업
- 자동 Merge
- 내장 스케줄러
- 지정된 이벤트 외 실시간 상태 알림

## Product Invariants

- `docs/requirements/` 바로 아래의 Requirement는 별도 상태 없이 모두 Planner 입력이다
- Requirement는 제품 변화와 Acceptance Criteria를 기록하며 구현 계획이나 실행 상태를 포함하지 않는다
- ExecPlan 파일은 계획의 고정 메타데이터와 내용을 기록하며 실행 상태를 포함하지 않는다
- ExecPlan 실행 상태의 유일한 원본은 Loop Manager의 SQLite다
- ExecPlan 생성 전 Planner 실패는 프로젝트의 `planning_error`로 SQLite에 저장하며 ExecPlan BLOCKED 상태를 만들지 않는다
- 미충족 PRODUCT 또는 Requirement에 근거해서만 ExecPlan을 작성한다
- 계획 입력이 변경되면 다음 Worker를 시작하기 전에 현재 입력에서 식별 가능한 모든 미충족 작업을 하나의 계획 주기에 ExecPlan으로 작성한다
- 실행 중인 작업은 새 계획으로 선점하지 않으며, 실행 가능한 READY ExecPlan은 안전한 무인 자동화를 막는 작업을 우선하는 결정적 순서로 선택한다
- Planner는 최신 `origin/<base-branch>` 기준의 detached planning worktree에서 ExecPlan만 작성한다
- 검증된 ExecPlan은 구현을 시작하기 전에 commit하고 `origin/<base-branch>`에 fast-forward push한다
- 같은 프로젝트에는 여러 READY ExecPlan을 등록할 수 있지만 동시에 하나의 ExecPlan만 실행한다
- ExecPlan은 모든 `depends_on`의 PR이 실제로 Merge된 뒤에만 시작한다
- 각 ExecPlan은 하나의 결정적 branch, 별도 worktree와 하나의 PR을 사용한다
- 서로 다른 프로젝트의 동시 실행 수는 `max_workers`를 넘지 않는다
- 전체 검증 실패 또는 Reviewer의 blocking issue가 있는 작업으로 PR을 생성하지 않는다
- 전체 검증은 Worker가 아니라 Loop Manager 코드가 실행하며 실패한 경우에만 Worker에게 결과를 전달한다
- ExecPlan은 연결된 PR이 실제로 Merge된 경우에만 MERGED 상태가 된다
- 자동 작업의 Git 기준은 최신 `origin/<base-branch>`다
- Loop Manager는 대상 프로젝트 기본 worktree의 파일, index와 HEAD를 수정하지 않는다
- SQLite, 관리 worktree, 로그, 임시 파일과 lock은 source checkout 밖의 사용자 상태 디렉터리에 저장한다
- 대상 프로젝트의 기존 변경을 사용자 승인 없이 삭제하거나 덮어쓰지 않는다
- 동일한 사용자 상태 디렉터리를 사용하는 Loop Manager가 동시에 둘 이상 작업하지 않는다
- 등록된 모든 프로젝트는 Loop Manager가 제공하는 동일한 `docs/PLANS.md`를 사용한다
- ExecPlan 메타데이터 파싱과 Model/Effort 결정에는 AI를 사용하지 않는다
- `PRODUCT.md`, Requirement와 `ARCHITECTURE.md`는 정해진 예외 외에는 자동 Loop가 수정하지 않는다
- 동일한 PR, BLOCKED와 프로젝트 완료 이벤트를 중복 전송하지 않는다

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

규칙:
- Requirement에는 frontmatter, 상태, complexity와 risk를 기록하지 않는다
- `docs/requirements/` 바로 아래에 저장된 모든 Requirement는 활성 Planner 입력이다
- 완료된 Requirement는 사용자가 `docs/requirements/archive/`로 옮긴다

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
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PR_OPEN 또는 BLOCKED ExecPlan이 있거나 해결되지 않은 `planning_error`가 있는 프로젝트는 등록 해제할 수 없다
- Telegram token과 chat ID가 모두 비어 있으면 알림 기능을 비활성화하고 전송을 시도하지 않는다
- Telegram token과 chat ID 중 하나만 설정돼 있으면 설정 오류로 처리한다

실패:
- 선택한 AI Agent가 실행 불가하거나 인증돼있지 않다
- Model, Effort, 일부만 입력된 Telegram 설정 또는 `max_workers` 설정이 유효하지 않다
- 프로젝트가 등록 조건을 만족하지 않는다
- `docs/PLANS.md`가 Loop Manager 기준 파일과 일치하지 않는다

### ExecPlan 생성과 자동 처리

시작 조건:
- 프로젝트에 미충족 PRODUCT 또는 Requirement가 있다
- 기존 ExecPlan 작업을 이어서 처리할 수 있다

흐름:
1. 프로그램이 GitHub PR 상태와 SQLite의 ExecPlan 상태를 동기화한다
2. BLOCKED ExecPlan과 `planning_error`의 근거 입력 변경 여부를 확인한다
3. 동시에 처리할 프로젝트를 `max_workers` 범위에서 선택한다
4. 실행 중인 작업이 없고 계획 입력이 변경됐으면 READY ExecPlan의 존재와 관계없이 최신 원격 기준 branch에서 임시 planning worktree를 만든다
5. 계획 입력이 변경되지 않았고 대기 중인 READY ExecPlan이 있으면 계획 생성을 건너뛰고 실행 가능한 계획 선택으로 이동한다
6. Planner가 PRODUCT, Requirement, ARCHITECTURE, 현재 코드와 기존 ExecPlan을 확인한다
7. 추가 작업이 없으면 계획 입력 지문을 저장하고 planning worktree를 정리한 뒤 프로젝트 완료 여부를 확인한다
8. 작업이 필요하면 Planner가 현재 입력에서 식별 가능한 미충족 작업 전체를 독립적으로 검증·Merge할 수 있는 ExecPlan으로 나누어 planning worktree의 `docs/plans/`에 일괄 작성하고, 의존성과 제품 우선순위를 기록한다
9. 프로그램이 Planner의 변경 범위, ExecPlan 메타데이터와 `depends_on`을 코드로 검증한다
10. 검증 실패 내용을 같은 Planner 세션에 전달하고 최대 3회까지 수정을 요청한다
11. 3회 후에도 유효하지 않으면 프로젝트의 `planning_error`에 원인과 계획 입력 지문을 저장하고 처리를 중단한다
12. 유효한 ExecPlan을 planning worktree에서 commit한다
13. 계획 commit이 아직 원격에 없는데 `origin/<base-branch>`가 계획 기준 이후 바뀌었으면 planning worktree를 정리하고 최신 기준으로 다시 계획한다
14. 기준이 그대로면 계획 commit을 `origin/<base-branch>`에 fast-forward push한다
15. push된 commit을 확인한 뒤에만 각 ExecPlan을 SQLite에 READY 상태로 등록하고 planning worktree와 기존 `planning_error`를 정리한다
16. 모든 `depends_on`이 MERGED인 READY ExecPlan 중 제품 우선순위가 가장 높은 계획 하나를 결정적으로 선택한다
17. 갱신된 `origin/<base-branch>`에서 해당 ExecPlan 전용 branch와 worktree를 생성하거나 기존 작업 공간을 복구한다
18. 프로그램이 complexity와 risk를 Worker level로 환산해 Model과 Effort를 결정한다
19. Worker가 구현, 관련 테스트와 전체 검증 스크립트를 작성하거나 갱신한다
20. Requirement 구현으로 장기 제품 규칙이 바뀌면 Worker가 같은 PR의 `PRODUCT.md`를 갱신한다
21. 프로그램이 최신 원격 기준 branch를 rebase로 반영한다
22. 프로그램이 전체 검증 스크립트를 실행하고, 실패한 경우에만 출력 내용을 같은 Worker 세션에 전달한다
23. 검증이 성공하면 읽기 전용 Reviewer를 실행한다
24. 프로그램이 같은 branch의 기존 PR을 확인하고, 없을 때만 새 PR을 생성한 뒤 SQLite 상태를 PR_OPEN으로 변경한다
25. 해당 프로젝트는 PR이 Merge되거나 BLOCKED가 해제될 때까지 다른 ExecPlan을 시작하지 않는다

### 실패와 재시도

Planner 결과 검증 실패:
1. 변경 범위 또는 ExecPlan 형식 오류를 같은 Planner 세션에 전달한다
2. 최대 3회까지 수정과 재검증을 수행한다
3. 계속 실패하면 계획 입력 지문과 원인을 `planning_error`에 저장하고 planning worktree를 보존한다
4. 같은 계획 입력 지문에서는 Planner를 다시 실행하지 않는다
5. 계획 입력 지문이 바뀌면 이전 planning worktree의 검증 기록을 보존하고 해당 worktree를 정리한 뒤 `planning_error`를 지우고 새로운 planning worktree에서 다시 시작한다

ExecPlan 게시 실패:
1. 계획 commit을 push하기 전에 `origin/<base-branch>`가 바뀌면 기존 계획 commit을 rebase하지 않고 폐기한 뒤 최신 기준으로 다시 계획한다
2. fast-forward push가 거부되면 계획 commit과 오류를 보존하고 다음 실행에서 다시 시도한다
3. push 성공을 원격에서 확인하기 전에는 ExecPlan을 READY로 등록하거나 Worker를 시작하지 않는다

전체 검증 또는 Review 실패:
1. 프로그램이 전체 검증 실패 내용 또는 Reviewer의 blocking issue를 같은 Worker 세션에 전달한다
2. 전체 검증 성공 출력은 Worker에게 전달하지 않는다
3. 최초 실패 후 전체 검증과 Review 단계별로 최대 3회까지 수정과 재시도를 수행한다
4. 재시도에 성공하면 정상 흐름을 계속한다
5. 3회 재시도 후에도 실패하면 ExecPlan을 BLOCKED로 변경한다

외부 CLI 실패:
- 실행 실패, timeout 또는 일시적 외부 오류는 현재 프로그램 실행에서 최대 3회까지 재시도한다
- 3회 실패하면 상태와 작업 공간을 보존하고 해당 프로젝트 처리를 이번 실행에서 중단한다
- 다음 Loop Manager 실행에서 같은 상태부터 다시 시도한다
- AI 사용 한도 소진이 확인되면 현재 실행에서는 재시도하지 않고 즉시 다음 프로젝트로 넘어간다

### BLOCKED 해제

시작 조건:
- ExecPlan이 BLOCKED 상태다

흐름:
1. BLOCKED 알림에 원인, ExecPlan 경로와 근거 문서 경로를 포함한다
2. 사용자가 ExecPlan 또는 근거가 된 PRODUCT/Requirement를 수정한다
3. 프로그램이 저장된 내용 해시와 현재 내용을 비교한다
4. 내용이 변경됐으면 재시도 횟수를 초기화하고 ExecPlan을 READY로 변경한다
5. 내용이 바뀌지 않았으면 BLOCKED 상태를 유지한다

### 사용자 승인 및 완료

시작 조건:
- ExecPlan이 PR_OPEN 상태다

흐름:
1. 사용자가 PR을 검토하고 Merge한다
2. 다음 실행에서 프로그램이 GitHub의 Merge 상태를 확인한다
3. SQLite의 ExecPlan 상태를 MERGED로 변경한다
4. PR Merge 방식과 관계없이 관리 worktree에 미커밋 변경이나 PR에 포함되지 않은 로컬 전용 commit이 없으면 정리한다
5. 변경이 남아 있으면 worktree를 보존하고 `cleanup_pending`에 경로와 원인을 기록한다
6. 의존성이 충족된 다음 READY ExecPlan이 있으면 새 관리 worktree에서 처리한다
7. 대기 중인 ExecPlan이 없으면 Planner가 추가 ExecPlan이 필요한지 다시 확인한다
8. 추가 작업이 없으면 프로젝트 완료 이벤트를 전송한다
9. 완료된 Requirement는 사용자가 archive로 옮긴다

실패:
- PR이 Merge되지 않은 동안 완료 처리하지 않는다
- PR이 Merge 없이 닫히면 ExecPlan을 BLOCKED로 변경한다
- 닫힌 PR 때문에 BLOCKED가 된 경우에는 사용자가 같은 PR을 다시 열어야 하며, 문서 변경만으로 READY로 전환하지 않는다

### 실행 종료 및 재개

1. 같은 사용자 상태 디렉터리의 다른 Loop Manager가 실행 중이면 상태를 바꾸지 않고 종료한다
2. 각 프로젝트의 PR, worktree와 저장된 실행 상태를 외부 상태와 대조한다
3. 기본 worktree가 dirty하거나 원격보다 앞서 있거나 diverge된 프로젝트는 변경 작업만 건너뛴다. 원격보다 뒤처진 상태는 허용하며 PR 상태 동기화와 알림은 계속한다
4. 프로젝트별로 하나의 작업만 선택하고 전체 동시 실행 수를 `max_workers`로 제한한다
5. 처리 중인 상태, 세션과 작업 공간을 SQLite에 저장한다
6. 중단된 작업은 다음 실행에서 저장된 상태부터 재개한다

### Telegram 알림

- PR 생성: 프로젝트명, ExecPlan과 PR 링크
- BLOCKED 발생: 프로젝트명, ExecPlan, 원인과 수정할 파일 경로
- 프로젝트 완료: 프로젝트명과 마지막 완료 작업
- 알림 실패는 작업 상태에 영향을 주지 않으며 다음 실행에서 다시 시도한다
- 새 PRODUCT/Requirement 변경으로 새로운 작업 주기가 시작되면 해당 프로젝트의 완료 알림을 다시 보낼 수 있다

## States and Transitions

ExecPlan 실행 상태는 파일이 아니라 SQLite에만 저장한다.

```text
READY → IMPLEMENTING → VERIFYING → REVIEWING → PR_OPEN → MERGED
VERIFYING 또는 REVIEWING → IMPLEMENTING (재시도)
READY, IMPLEMENTING, VERIFYING 또는 REVIEWING → BLOCKED
BLOCKED → READY (ExecPlan 또는 근거 문서 변경)
PR_OPEN → BLOCKED (Merge 없이 PR 종료)
BLOCKED → PR_OPEN (같은 PR을 사용자가 다시 열음)
```

- READY: 유효한 ExecPlan이 등록돼 프로젝트 실행 순서와 `depends_on` 충족을 대기
- IMPLEMENTING: Worker가 구현 또는 실패 수정을 진행 중
- VERIFYING: 전체 검증 스크립트 실행 중
- REVIEWING: Reviewer가 blocking issue를 확인 중
- PR_OPEN: PR 생성 후 사용자 Merge 대기
- MERGED: 연결된 PR의 Merge가 확인된 최종 상태
- BLOCKED: 사용자 결정이나 문서 수정이 필요함

외부 CLI의 일시적 실패와 사용 한도 소진은 별도 ExecPlan 상태를 만들지 않고 현재 상태를 보존한다.

`planning_error`와 `cleanup_pending`은 ExecPlan 상태가 아니다. `planning_error`는 해당 프로젝트의 새 계획과 완료 처리를 막지만, `cleanup_pending`은 이미 Merge된 작업의 보존 경고이므로 다음 ExecPlan 실행과 프로젝트 완료를 막지 않는다.

## Business Rules

### PRODUCT, Requirement와 ExecPlan

- `PRODUCT.md`는 현재 Merge된 제품 동작, 경계와 장기 규칙의 기준이다
- Requirement는 제품에 원하는 변화, 이유와 Acceptance Criteria를 기록한다
- Requirement는 PRODUCT의 변경 요청이며 ExecPlan의 구현 상세나 실행 상태를 포함하지 않는다
- Requirement 파일 경로를 안정적인 식별자로 사용한다
- `docs/requirements/` 바로 아래의 모든 `*.md`를 Planner 입력으로 사용한다
- `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 Planner 입력에서 제외한다
- ExecPlan의 `sources`에는 근거가 된 PRODUCT 또는 Requirement 경로가 있어야 한다
- 계획 입력이 변경되면 Planner는 실행 중인 작업이 없는 다음 선택 시점에 READY ExecPlan의 존재와 관계없이 전체 카탈로그를 확인하고, 현재 식별 가능한 미충족 작업을 한 번에 모두 계획한다
- ExecPlan의 `depends_on`은 선행 ExecPlan ID 목록이며, 모든 선행 상태가 MERGED여야 Worker를 시작할 수 있다
- Worker는 장기 제품 규칙이 바뀌는 경우에만 코드와 같은 PR에서 `PRODUCT.md`를 갱신한다

### 프로젝트 등록 및 실행 조건

- 대상 경로는 origin이 GitHub 저장소인 Git 프로젝트여야 한다
- `AGENTS.md`, `docs/PLANS.md`, `docs/ARCHITECTURE.md`가 있어야 한다
- `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 Requirement가 하나 이상 있어야 한다
- 변경되지 않은 PRODUCT/ARCHITECTURE 템플릿만 있는 프로젝트는 작업 가능 상태로 등록하지 않는다
- `docs/PLANS.md`는 Loop Manager 기준 파일과 SHA-256 해시가 같아야 한다
- 공통 내용이 없는 `AGENTS.md`는 사용자 확인 후 강제 등록할 수 있다
- `AGENTS.md` 포함 검사와 `docs/PLANS.md` 해시 검사는 프로젝트 등록 시에만 수행한다
- 자동 작업의 기준은 `origin/<base-branch>`다
- 기본 worktree가 dirty하거나 원격보다 앞서 있거나 diverge됐으면 해당 프로젝트를 건너뛴다
- 기본 worktree가 원격보다 뒤처진 상태는 허용하며 자동 작업을 막지 않는다
- Loop Manager가 만드는 계획과 구현 변경은 대상 프로젝트 밖의 Loop Manager 관리 경로에 만든 별도 worktree와 branch에만 기록한다
- 자동 실행 중 기본 worktree에서는 checkout, pull, reset, stash, clean, commit과 파일 생성을 수행하지 않는다

### 작업 선택과 동시 실행

- 같은 프로젝트에 여러 READY ExecPlan이 대기할 수 있다
- 같은 프로젝트에서는 IMPLEMENTING부터 PR_OPEN 또는 BLOCKED까지 하나의 ExecPlan만 현재 작업으로 둔다
- 새 계획 입력은 실행 중인 작업을 선점하지 않지만, 다음 Worker를 시작하기 전에 ExecPlan 카탈로그와 우선순위에 반영한다
- READY ExecPlan은 모든 `depends_on`이 MERGED이고 다른 현재 작업이 없을 때만 시작한다
- 제품 우선순위는 안전한 무인 실행을 막거나 반복적인 사용자 복구를 요구하는 작업, 정상 경로의 사용자 개입을 제거하는 핵심 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순이다
- 실행 가능한 READY ExecPlan이 여럿이면 제품 우선순위가 높은 계획을 먼저 선택하고 같은 우선순위에서는 숫자 ExecPlan ID가 가장 작은 계획을 선택한다
- 각 ExecPlan은 고유하고 결정적인 이름의 branch와 별도 관리 worktree에서 작업한다
- 서로 다른 프로젝트는 `max_workers` 범위에서 병렬로 처리한다
- `max_workers` 기본값은 3이며 `config/config.ini`에서 변경할 수 있다
- BLOCKED 또는 PR_OPEN 프로젝트에서는 다른 ExecPlan을 시작하거나 Planner를 다시 실행하지 않는다

### Model과 Effort 선택

- Planner가 ExecPlan의 complexity와 risk를 판단한다
- 프로그램은 complexity를 low=1, medium=3, high=5로 환산한다
- 프로그램은 risk를 low=0, medium=1, high=2, critical=3으로 환산한다
- 두 점수의 합을 최대 7로 제한해 `level1`부터 `level7`까지의 Worker level로 사용한다
- 정확한 Model 이름과 Effort는 `config/config.ini`에서 관리한다

### PR 생성 조건

- Worker가 현재 환경에 맞는 전체 검증 스크립트를 생성하거나 갱신해야 한다
- Worker는 전체 검증 스크립트를 직접 실행하지 않으며 Loop Manager 코드가 실행한다
- PR 생성 직전에 최신 `origin/<base-branch>`를 rebase로 반영하고 전체 검증과 Review를 다시 통과해야 한다
- 관련 테스트와 전체 검증이 통과해야 한다
- Reviewer의 blocking issue가 없어야 한다
- 같은 ExecPlan branch에 기존 PR이 있으면 이를 복구해 사용하고 중복 PR을 생성하지 않는다
- 프로그램은 PR을 자동으로 Merge하지 않는다
- Merge commit, Squash merge와 Rebase merge를 모두 정상 Merge로 처리하며 Merge 방식만으로 관리 worktree를 보존하지 않는다
- Merge된 PR의 head와 관리 worktree를 대조해 PR 이후의 로컬 전용 commit이 없고 worktree가 clean이면 원격 head branch의 존재 여부와 관계없이 worktree를 자동 정리한다
- 미커밋 변경, PR 이후의 로컬 전용 commit 또는 안전 여부를 확인할 수 없는 상태만 `cleanup_pending`으로 보존한다

### 프로젝트 완료 조건

- Planner가 현재 PRODUCT와 활성 Requirement에서 추가 ExecPlan이 필요 없다고 판단해야 한다
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PR_OPEN 또는 BLOCKED 상태의 ExecPlan이 없어야 한다
- 해결되지 않은 `planning_error`가 없어야 한다
- `cleanup_pending`은 프로젝트 완료를 막지 않는다
- Requirement와 ExecPlan에는 별도의 초안 상태를 두지 않는다
- 프로젝트 완료 알림은 프로젝트별로 관리한다
- 새로운 PRODUCT/Requirement 변경으로 작업 주기가 시작되고 다시 완료되면 새 완료 알림을 전송한다
- Requirement archive 이동만으로 완료 알림을 다시 활성화하지 않는다

### Telegram 알림 조건

- Telegram token과 chat ID가 모두 설정된 경우에만 알림을 생성하고 전송한다
- 두 값이 모두 비어 있으면 알림을 비활성화하며 작업 흐름에는 영향을 주지 않는다
- PR 생성 알림에는 PR 링크가 포함돼야 한다
- BLOCKED 알림에는 ExecPlan 또는 계획 단계, 원인과 수정 가능한 파일 경로가 포함돼야 한다
- 프로젝트 완료 알림은 해당 프로젝트의 현재 작업 주기가 완료됐을 때 전송한다
- 동일한 이벤트는 한 번만 전송하며 실패한 알림만 다음 실행에서 재시도한다
