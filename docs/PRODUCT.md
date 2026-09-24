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
- 마지막 관련 ExecPlan의 구현 PR에서 완료된 Requirement를 자동으로 archive할 수 있다
- GitHub Merge가 확인된 MERGED ExecPlan을 구현 PR과 분리된 정리 commit으로 자동 archive할 수 있다
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
- 최신으로 검증한 `origin/<base-branch>`의 active 및 archived ExecPlan 파일은 ID를 제외한 현재 계획 선언의 원본이다. SQLite는 최초 게시 path·hash·SHA를 불변 감사 정보로 보존하고 현재 path·title·sources·depends_on·complexity·risk·priority·hash·revision을 별도 cache로 동기화한다
- ExecPlan 실행 상태의 유일한 원본은 Loop Manager의 SQLite다
- 유효한 ExecPlan 선언 변경은 계획 또는 프로젝트 차단 사유가 아니며, catalog 전체가 유효하고 원격 revision과 SQLite 상태·소유권 snapshot이 그대로일 때만 모든 현재 cache delta를 한 transaction으로 반영한다
- 원격 ExecPlan catalog가 누락 파일, 잘못된 형식·source·ID 또는 dependency graph 때문에 유효하지 않으면 다른 ExecPlan 상태를 바꾸지 않고 실제 원인 파일을 포함한 프로젝트 범위 진단으로 격리한다
- ExecPlan 생성 전 Planner 실패는 프로젝트의 `planning_error`로 SQLite에 저장하며 ExecPlan BLOCKED 상태를 만들지 않는다
- 미충족 PRODUCT 또는 Requirement에 근거해서만 ExecPlan을 작성한다
- 실행 중인 변경 작업은 새 계획 입력이 선점하지 않는다. 안전한 선택 경계에서 마지막 성공 Planner 이후 명세가 바뀌었으면 대기 중인 READY보다 일반 Planner를 정확히 한 번 먼저 실행하고, 명세가 같으면 실행 가능한 READY를 우선순위대로 시작하며, 실행 가능한 READY가 없을 때만 변경된 전체 계획 입력을 한 번 재계획한다. due 유지보수 평가는 기존 순서를 유지한다
- 프로젝트별 성공한 유지보수 평가 이후 새로 MERGED가 된 ExecPlan 수로 다음 평가 시점을 계산하며, 이력이 없으면 첫 선택 경계에서 평가한다. 공통 `refactoring_review_interval`은 양의 정수이고 기본값은 5다
- 실행 가능한 READY ExecPlan은 명세 변경 우선 계획이 없을 때 안전한 무인 자동화를 막는 작업부터 결정적 순서로 선택한다. dependency 미충족 READY, `PLAN_BLOCKED`·안정된 `PR_OPEN`만 남은 경우에도 명세 또는 전체 계획 입력 trigger를 평가하되, 활성 유지보수 주기의 prerequisite 집합과 리팩터링 DAG 순서는 유지한다
- Planner는 최신 `origin/<base-branch>` 기준의 detached planning worktree에서 새 ExecPlan을 작성하고, SQLite와 Git snapshot으로 증명된 미착수 READY만 같은 ID로 수정·rename·삭제할 수 있다. 그 밖의 기존 계획과 파일은 변경하지 않으며 범위 밖 아이디어만 `docs/FUTURE_IDEAS.md`에 기록한다
- 유지보수 Planner만 활성 cycle mode와 무소유 READY snapshot에 한정해 barrier dependency 또는 사후 구조 참조를 개정할 수 있다. READY가 없고 일반 계획 입력도 변경된 due 경계에서만 일반 계획과 유지보수 평가를 한 호출로 결합한다
- 유지보수 planning batch는 원격 fast-forward가 확인된 뒤 catalog와 cycle 상태를 원자적으로 반영하며, 평가 없음 또는 사후 재검증 성공 전에는 평가 기준선을 전진시키지 않는다
- `docs/FUTURE_IDEAS.md`는 Planner 입력이나 실행 상태가 아니며, 항목을 같은 계획 주기의 ExecPlan 범위로 사용하거나 Requirement·ExecPlan으로 자동 승격하지 않는다
- 검증된 새 ExecPlan, 미착수 READY의 개정·rename·삭제와 `docs/FUTURE_IDEAS.md` 변경은 하나의 planning commit으로 만들고 `origin/<base-branch>`에 fast-forward push한다. 삭제 ID는 취소 감사로 예약하며, 원격 확인 뒤 catalog 변경과 명세·전체 입력 성공 지문을 하나의 SQLite transaction으로 적용한다
- 같은 프로젝트에는 여러 READY, `PLAN_BLOCKED`, 안정된 `PR_OPEN` ExecPlan을 함께 보존할 수 있지만 IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING 또는 PR 충돌 복구로 저장소를 변경하는 실행은 동시에 하나만 둔다
- ExecPlan은 모든 `depends_on`의 PR이 실제로 Merge된 뒤에만 시작한다
- 각 ExecPlan은 하나의 결정적 branch, 별도 worktree와 하나의 PR을 사용한다
- 서로 다른 프로젝트의 동시 실행 수는 `max_workers`를 넘지 않는다
- 전체 검증 실패 또는 Reviewer의 blocking issue가 있는 작업으로 PR을 생성하지 않는다
- 전체 검증은 Worker가 아니라 Loop Manager 코드가 실행하며 실패한 경우에만 Worker에게 결과를 전달한다
- Requirement는 다른 모든 관련 ExecPlan이 MERGED인 마지막 구현 PR에서만 자동 archive한다
- ExecPlan은 GitHub Merge 확인과 SQLite MERGED 전이 뒤에만 별도 정리 대상으로 삼고, 최신 원격 base의 detached 임시 worktree에서 내용 보존 rename-only commit으로 일괄 archive한다
- ExecPlan archive는 원본 hash, regular-file source, 비어 있는 destination과 원격 base 불변성을 검증해 fast-forward로 직접 게시하며, 후보별 충돌·원격 경쟁·push 실패는 MERGED 상태와 다른 작업·완료 처리를 막지 않는 archive pending으로 재시도한다
- ExecPlan은 연결된 PR이 실제로 Merge된 경우에만 MERGED 상태가 된다
- 자동 작업의 Git 기준은 최신 `origin/<base-branch>`이며 로컬 base ref, 기본 worktree 파일과 로컬 전용 commit을 입력으로 사용하지 않는다
- Codex가 Agent turn 추적을 위해 자동 생성·갱신·정리하는 `refs/codex/turn-diffs/*`는 게시 또는 workspace 소유권에 사용하지 않는 provider 소유의 비권위 임시 ref다. 그 namespace의 순변경만으로 Agent 범위 위반이나 `PROJECT_BLOCKED`를 만들지 않으며, 소유 branch, remote ref, tag와 그 밖의 공유 ref 변경은 계속 보호한다
- Loop Manager는 등록 프로젝트 기본 worktree가 safe fast-forward 조건을 만족할 때만 HEAD, index와 tracked 파일을 fetch된 원격 base로 전진시키며, 그 밖의 로컬 상태는 보존한다
- SQLite, 관리 worktree, 로그, 임시 파일과 lock은 source checkout 밖의 사용자 상태 디렉터리에 저장한다
- 대상 프로젝트의 기존 변경을 사용자 승인 없이 삭제하거나 덮어쓰지 않는다
- 동일한 사용자 상태 디렉터리를 사용하는 Loop Manager가 동시에 둘 이상 작업하지 않는다
- 등록된 모든 프로젝트는 Loop Manager가 제공하는 동일한 `docs/PLANS.md`를 사용한다
- ExecPlan 메타데이터 파싱과 Model/Effort 결정에는 AI를 사용하지 않는다
- `PRODUCT.md`, Requirement와 `ARCHITECTURE.md`는 정해진 예외 외에는 자동 Loop가 수정하지 않는다
- 동일한 PR, 차단 scope·구조화 원인·관련 ExecPlan identity와 프로젝트 완료 이벤트를 중복 전송하지 않는다

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
- 완료된 Requirement는 마지막 관련 ExecPlan의 구현 PR에서 `docs/requirements/archive/`로 자동 이동한다

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

시작 조건:
- 프로젝트에 미충족 PRODUCT 또는 Requirement가 있다
- 기존 ExecPlan 작업을 이어서 처리할 수 있다

흐름:
1. 프로그램이 모든 `PR_OPEN`의 Merge·Close·충돌 상태를 대상 ExecPlan별로 동기화하고 `PROJECT_BLOCKED`의 저장소 안전을 먼저 확인한다. 프로젝트 안전이 증명되지 않으면 fetch, 기본 worktree 동기화와 저장소 변경 단계를 실행하지 않는다. 단, 원격 ExecPlan catalog 오류 차단은 원인 push를 fetch하고 재검증해야 하므로 이 저장소 안전 gate와 별도로 처리한다
2. 프로젝트 안전 gate가 열려 있으면 fetch하고 eligible 기본 worktree만 원격 base로 safe fast-forward한 뒤 각 `PLAN_BLOCKED`의 계획별 해제 조건 및 `planning_error`의 근거 입력 변경 여부를 독립적으로 확인한다
3. 동시에 처리할 프로젝트를 `max_workers` 범위에서 선택한다
4. 실행 중인 변경 단계가 없으면 dependency를 충족한 확정 PR 충돌 복구 하나를 먼저 선택하고, 없으면 프로젝트별 유지보수 평가 주기를 확인한다. `PLAN_BLOCKED` 또는 `PR_OPEN`이 있으면 유지보수 평가는 보류하고 독립 READY 선택으로 이동한다
5. due 유지보수 cycle이 prerequisite 또는 리팩터링 계획을 기록했으면 그 집합의 dependency 충족 계획만 선택하고, 사후 재검증 단계면 장벽 뒤 Worker보다 유지보수 Planner를 먼저 실행한다
6. 유지보수 cycle이 없으면 최신 원격 PRODUCT·ARCHITECTURE·활성 Requirement 명세 지문을 먼저 비교한다. 마지막 성공 기준과 다르면 READY·`PLAN_BLOCKED`·안정된 `PR_OPEN`보다 일반 Planner를 한 번 먼저 실행하고, 같으면 실행 가능한 READY를 priority와 숫자 ID 순으로 시작한다. 실행 가능한 READY가 없을 때만 전체 계획 입력 지문을 비교해 달라진 경우 한 번 재계획하며, 두 지문이 같으면 대기하거나 완료한다
7. Planner가 PRODUCT, Requirement, ARCHITECTURE, 현재 코드, 비종료 compact catalog와 프로그램이 제공한 미착수 READY snapshot을 확인한다
8. 계획과 `docs/FUTURE_IDEAS.md` 변경이 모두 없으면 관측한 명세·전체 계획 입력 지문을 함께 저장하고 planning worktree를 정리한 뒤 프로젝트 완료 여부를 확인한다
9. 작업이 필요하면 Planner가 미충족 작업을 새 ExecPlan으로 나누어 일괄 작성하고, snapshot의 미착수 READY를 같은 ID로 수정·rename·삭제할 수 있다. 현재 범위 밖의 아이디어는 `docs/FUTURE_IDEAS.md`에 분리한다
10. 프로그램이 create/update/rename/delete와 FUTURE_IDEAS 변경을 분류하고, snapshot 원본·현재 SQLite 무소유 상태 및 적용 후 전체 catalog의 ID·source·dependency graph를 검증한다
11. 검증 실패 내용을 같은 Planner 세션에 전달하고 최대 3회까지 수정을 요청한다
12. 3회 후에도 유효하지 않으면 프로젝트의 `planning_error`에 원인과 계획 입력 지문을 저장하고 처리를 중단한다
13. 유효한 생성·개정·rename·삭제와 `docs/FUTURE_IDEAS.md` 변경을 planning worktree에서 하나의 planning commit으로 만든다
14. 계획 commit이 아직 원격에 없는데 `origin/<base-branch>`가 계획 기준 이후 바뀌었으면 planning worktree를 정리하고 최신 기준으로 다시 계획한다
15. 기준과 mutable READY snapshot이 그대로면 복구 가능한 publication intent를 저장한 뒤 계획 commit을 `origin/<base-branch>`에 fast-forward push한다
16. push된 commit을 확인한 뒤에만 생성 row 등록, 개정 row 갱신, 삭제 row 제거와 취소 감사, 두 성공 지문 및 planning 상태 정리를 하나의 SQLite transaction으로 적용한다. FUTURE_IDEAS 변경만으로는 ExecPlan 실행 상태를 만들지 않는다
17. 모든 `depends_on`이 MERGED인 READY ExecPlan 중 제품 우선순위가 가장 높은 계획 하나를 결정적으로 선택한다
18. 갱신된 `origin/<base-branch>`에서 해당 ExecPlan 전용 branch와 worktree를 생성하거나 기존 작업 공간을 복구한다
19. 프로그램이 complexity와 risk로 최초 Worker level 1~7을 계산하고 저장된 effective level의 Model과 Effort를 선택한다
20. 프로그램이 활성 Requirement의 관련 ExecPlan source와 SQLite 상태를 결합해 archive 후보를 계산한다
21. Worker가 구현, 관련 테스트와 전체 검증 스크립트를 작성하거나 갱신하고 계산된 Requirement를 내용 변경 없이 archive로 이동한다
22. Requirement 구현으로 장기 제품 규칙이 바뀌면 Worker가 같은 PR의 `PRODUCT.md`를 갱신한다
23. 프로그램이 Worker 결과의 실제 archive 이동 집합을 다시 검증한다
24. 프로그램이 최신 원격 기준 branch를 rebase로 반영한다
25. 프로그램이 전체 검증 스크립트를 실행하고, 실패한 경우에만 출력 내용을 같은 Worker 세션에 전달한다
26. 검증이 성공하면 읽기 전용 Reviewer를 실행한다
27. 게시 직전에 최신 base에서 archive 후보를 다시 계산하고 이동 집합이 달라지면 같은 Worker 세션에서 수정한 뒤 검증과 Review를 반복한다
28. 프로그램이 같은 branch의 기존 PR을 확인하고, 없을 때만 새 PR을 생성한 뒤 SQLite 상태를 PR_OPEN으로 변경한다
29. 안정된 `PR_OPEN`은 실행 자리를 비우며, 프로그램은 해당 계획에 직접·간접 의존하지 않는 다음 READY를 최신 `origin/<base-branch>`에서 순차적으로 시작한다

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
4. 일반·유지보수 batch의 prepared intent와 planning worktree는 push·SQLite 적용 실패 뒤에도 보존한다. 원격에 batch가 있으면 원자 적용을 재개하고, 없으면 최신 base와 snapshot에서 다시 계획한다

전체 검증 또는 Review 실패:
1. 프로그램이 전체 검증 실패 내용 또는 Reviewer의 blocking issue를 같은 Worker 세션에 전달하고 Reviewer도 저장된 세션을 재사용한다
2. 전체 검증 성공 출력은 Worker에게 전달하지 않는다
3. 전체 검증과 Review 출력을 결정적으로 정규화해 단계별 실패 지문과 발생 횟수를 SQLite에 저장한다
4. 새로운 실패 지문이 나타나는 동안에는 총 실패 횟수와 관계없이 같은 프로그램 실행에서 Worker 수정, 전체 검증과 Review를 계속한다
5. 같은 지문이 Worker 수정 후 세 번 다시 발생하면 구조화된 원인과 지문을 기록하고 ExecPlan을 `PLAN_BLOCKED`로 변경하며, 중간에 다른 지문이 발생해도 이전 이력을 유지한다
6. 유효한 Review FAIL이 같은 effective level에서 동일 지문으로 두 번 확인되거나 서로 다른 지문 세 개로 확인되면 다음 Worker 수정부터 level을 한 단계 올리며 level 8을 상한으로 한다
7. 전체 verify 실패, Worker 결과 형식 오류, Reviewer 실행 오류와 유효한 FAIL이 없는 결과는 effective level과 Review 승격 이력을 변경하지 않는다
8. 전체 검증 성공은 verify 지문 이력만, Review 성공은 Review 지문 이력만 초기화하며 effective level과 level별 승격 이력은 유지한다

외부 CLI 실패:
- 실행 실패, timeout 또는 일시적 외부 오류는 현재 프로그램 실행에서 최대 3회까지 재시도한다
- 3회 재시도 후에도 실패하면 상태와 작업 공간을 보존하고 해당 프로젝트 처리를 이번 실행에서 중단한다
- 다음 Loop Manager 실행에서 같은 상태부터 다시 시도한다
- AI 사용 한도 소진이 확인되면 현재 실행에서는 재시도하지 않고 즉시 다음 프로젝트로 넘어간다

### 계획 및 프로젝트 차단 해제

시작 조건:
- ExecPlan이 `PLAN_BLOCKED`이거나 프로젝트에 `PROJECT_BLOCKED` 레코드가 있다

흐름:
1. `PLAN_BLOCKED` 상태, scope, 구조화 원인과 판단 근거를 SQLite에 먼저 commit하고 PR이 없는 계획에 별도 durable checkpoint request를 기록한다
2. 저장된 branch·결정적 worktree·workspace 소유권·HEAD와 Worker 허용 변경 범위가 모두 일치하고 미해결 Git operation, symlink, ignored 파일, 범위 밖 staged entry, index flag 또는 공유 metadata 불확실성이 없을 때만 검증된 pathspec으로 checkpoint commit을 만든다. 이미 commit된 변경이나 변경 없음에는 빈 commit을 만들지 않는다
3. 같은 소유 branch의 원격이 없으면 생성하고, 저장된 expected OID와 일치하는 fast-forward만 갱신한다. PR 없는 기존 rebase 복구만 정확한 `--force-with-lease`를 사용할 수 있으며 예상하지 못한 원격 commit, branch protection과 권한 거부를 우회하지 않는다
4. commit·push phase와 local/remote OID를 저장해 중단 후 같은 checkpoint를 재개하고, 일시적 push 실패는 실행당 제한 횟수만 재시도한 뒤 local commit과 worktree를 보존한다. checkpoint 결과와 무관하게 ExecPlan은 `PLAN_BLOCKED`로 남고 독립 READY 선택 자리를 점유하지 않는다
5. `PROJECT_BLOCKED`, `planning_error`, 등록·branch 생성 전 실패, 기존 PR 또는 소유권을 증명할 수 없는 legacy 차단에는 원격 쓰기를 수행하지 않고 미게시 이유를 저장한다. 차단 알림은 checkpoint 결과 확정 뒤 성공 시 commit OID와 immutable branch link를, 미게시 시 local-only 이유를 포함한다
6. 계획별 입력 변경, guidance 결정 또는 닫힌 PR reopen 조건은 해당 `PLAN_BLOCKED`만 재평가한다. PR 없는 게시 checkpoint의 저장된 원격 branch가 달라지면 게시 OID와 마지막 채택 head를 포함하는 후속 commit인지 확인한다. 계획·branch·worktree·저장소 소유권, 로컬 HEAD·index·작업 파일·ignored 경로·Git operation 및 허용 변경 범위를 검증한 뒤 로컬 작업을 보존할 수 있을 때만 같은 branch를 fast-forward한다. 삭제·재작성·분기·소유권 불명확·충돌은 구체적인 이유로 계속 차단한다. 새 OID만으로 해제하지 않고 실제 변경과 원인별 조건을 재평가하며, 해결된 사용자 commit은 Worker 검토와 targeted test, 전체 verify, Review를 다시 거친다. 해결되지 않은 검사 결과는 원격 head와 함께 저장해 동일 입력의 반복 시도와 알림을 막는다. 게시 증거가 없는 legacy checkpoint와 닫힌 PR에는 이 경로를 적용하지 않는다
7. 해제 조건이 충족된 계획은 기존 branch와 worktree에서 재개하고 재시도 횟수와 verify·Review 실패 지문 이력을 초기화하며 effective Worker level과 승격 이력을 최초 level로 되돌린 뒤 READY 또는 PR_OPEN으로 변경한다. 같은 branch의 최초 PR은 최종 verify와 Review 성공 뒤에만 생성한다
8. `PROJECT_BLOCKED`는 기본 worktree, 허용되지 않은 공유 ref, 외부 worktree와 workspace 소유권을 다시 검증해 프로젝트 안전이 증명된 경우에만 해제한다. `refs/codex/turn-diffs/*`의 순변경은 해제 조건을 무효화하지 않는다
9. 프로젝트 차단이 해제돼도 별도 계획 원인이 남은 ExecPlan은 `PLAN_BLOCKED`를 유지하며, 다른 차단 계획의 fingerprint와 작업 공간은 변경하지 않는다

### 사용자 승인 및 완료

시작 조건:
- ExecPlan이 PR_OPEN 상태다

흐름:
1. 사용자가 PR을 검토하고 Merge한다
2. 다음 실행에서 프로그램이 GitHub의 Merge 상태를 확인한다
3. SQLite의 ExecPlan 상태를 MERGED로 변경한다
4. PR Merge 방식과 관계없이 관리 worktree에 미커밋 변경이나 PR에 포함되지 않은 로컬 전용 commit이 없으면 정리한다
5. 변경이 남아 있으면 worktree를 보존하고 `cleanup_pending`에 경로와 원인을 기록한다
6. 최신 원격 base에서 안전한 MERGED ExecPlan을 `docs/plans/archive/`로 내용 변경 없이 일괄 이동하고 fast-forward로 직접 게시한다
7. archive할 수 없는 후보는 원인과 operation identity를 저장하되 다음 READY·Planner·완료 처리를 계속한다
8. 의존성이 충족된 다음 READY ExecPlan이 있으면 새 관리 worktree에서 처리한다
9. 대기 중인 ExecPlan이 없으면 Planner가 추가 ExecPlan이 필요한지 다시 확인한다
10. 추가 작업이 없으면 프로젝트 완료 이벤트를 전송한다
11. Merge된 PR에 archive 이동이 포함된 Requirement는 활성 Planner 입력에서 제외된다

실패:
- PR이 Merge되지 않은 동안 완료 처리하지 않는다
- PR이 Merge 없이 닫히면 해당 ExecPlan만 `PLAN_BLOCKED`로 변경한다
- 닫힌 PR 때문에 `PLAN_BLOCKED`가 된 경우에는 사용자가 같은 PR을 다시 열어야 하며, 문서 변경만으로 READY로 전환하지 않는다

### 실행 종료 및 재개

1. 같은 사용자 상태 디렉터리의 다른 Loop Manager가 실행 중이면 상태를 바꾸지 않고 종료한다
2. 각 프로젝트의 PR, worktree와 저장된 실행 상태를 외부 상태와 대조한다
3. 기본 worktree의 dirty, staged, untracked, ahead, diverged, detached 또는 다른 branch 상태는 원격 SHA 기반 작업을 막지 않는다. safe fast-forward 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
4. 프로젝트별로 하나의 작업만 선택하고 전체 동시 실행 수를 `max_workers`로 제한한다
5. 처리 중인 상태, 세션과 작업 공간을 SQLite에 저장한다
6. 중단된 작업은 다음 실행에서 저장된 상태부터 재개한다

### Telegram 알림

- PR 생성: 프로젝트명, ExecPlan과 PR 링크
- 차단 발생: checkpoint 결과 확정 뒤 프로젝트명, scope, 구조화 원인, 관련 ExecPlan, 수정할 파일 경로와 원격 commit link 또는 local-only 보존 이유
- 프로젝트 완료: 프로젝트명과 마지막 완료 작업
- 알림 실패는 작업 상태에 영향을 주지 않으며 다음 실행에서 다시 시도한다
- 새 PRODUCT/Requirement 변경으로 새로운 작업 주기가 시작되면 해당 프로젝트의 완료 알림을 다시 보낼 수 있다

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

`PROJECT_BLOCKED`는 ExecPlan 상태가 아니라 프로젝트별 안전 gate다. 현재 실행 단계와 작업 공간을 보존한 채 Planner, maintenance Planner, Worker와 Git 쓰기를 중단하며, GitHub PR 상태 확인과 durable 알림 dispatch 같은 읽기·관리 작업만 계속한다. 차단 전이는 코드가 정의한 reason-to-scope mapping, 관련 ExecPlan, 판단 근거와 해제 fingerprint를 하나의 SQLite transaction에 저장한다. 범위를 증명할 수 없는 legacy `BLOCKED`는 보수적으로 `PROJECT_BLOCKED`로 이관한다.

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
- Requirement는 제품에 원하는 변화, 이유와 Acceptance Criteria를 기록한다
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
- ExecPlan ID만 Git 선언과 SQLite 실행 이력을 잇는 안정적인 identity다. 같은 ID의 유효한 원격 파일에서 path·title·sources·depends_on·complexity·risk·priority 또는 본문이 바뀌면 현재 cache를 갱신하고 상태·실패 이력·Agent session·branch·worktree·workspace·PR 연결은 보존한다
- source와 dependency 변경은 같은 reconciliation 결과를 다음 archive 후보·입력 지문·READY 선택에 사용하고 priority 변경은 다음 선택부터 반영한다. complexity·risk 변경은 최초 Worker level을 다시 계산하되 실행 이력이 있는 계획의 effective level과 Review 승격 이력을 낮추지 않는다
- 현재 path와 title은 compact catalog, 로그·알림과 이후 파일 조회에 사용하지만 기존 PR의 ExecPlan 링크는 최초 게시 SHA와 최초 active path를 계속 사용한다
- 일반 Planner는 안전한 선택 경계에서 명세 변경 우선 trigger 또는 실행 가능한 READY가 없는 전체 입력 fallback trigger가 있을 때만 실행하며, 비종료 compact catalog와 미착수 READY snapshot을 사용해 현재 식별 가능한 미충족 작업을 한 번에 모두 계획한다
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
- 기본 worktree가 clean한 저장 base branch이고 원격과 equal 또는 behind-only이면 fetch한 원격 ref로 no-op 또는 fast-forward하고, 그 밖의 상태나 ignored 경로 충돌은 이유를 기록한 뒤 보존한다
- Loop Manager가 만드는 계획과 구현 변경은 대상 프로젝트 밖의 Loop Manager 관리 경로에 만든 별도 worktree와 branch에만 기록한다
- 자동 실행 중 기본 worktree에서는 검증된 `git merge --ff-only` 상당의 safe fast-forward 외에 checkout, pull, reset, stash, clean, commit과 파일 생성을 수행하지 않는다

### 작업 선택과 동시 실행

- 같은 프로젝트에 여러 READY ExecPlan이 대기할 수 있다
- 같은 프로젝트에서는 IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING 또는 PR 충돌 복구 중인 ExecPlan 하나만 현재 변경 실행으로 둔다. `PLAN_BLOCKED`와 충돌 복구 중이 아닌 `PR_OPEN`은 이 자리를 점유하지 않는다
- 새 계획 입력은 실행 중인 작업을 선점하지 않는다. 마지막 성공 Planner 뒤의 명세 변경은 다음 안전한 선택 경계에서 READY보다 먼저 한 번 반영하고, 그 밖의 전체 입력 변경은 실행 가능한 READY가 없을 때만 반영한다
- READY ExecPlan은 모든 `depends_on`이 MERGED이고 다른 현재 작업이 없을 때만 시작한다
- dependency를 충족하지 못한 READY만 남은 프로젝트도 명세 또는 no-runnable 전체 입력 trigger가 있으면 Planner를 한 번 실행하고, 성공 기준과 입력이 같으면 대기한다
- 제품 우선순위는 안전한 무인 실행을 막거나 반복적인 사용자 복구를 요구하는 작업, 정상 경로의 사용자 개입을 제거하는 핵심 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순이다
- 실행 가능한 READY ExecPlan이 여럿이면 제품 우선순위가 높은 계획을 먼저 선택하고 같은 우선순위에서는 숫자 ExecPlan ID가 가장 작은 계획을 선택한다
- 활성 유지보수 prerequisite 집합은 dependency gate 뒤 priority와 숫자 ID 순으로, 리팩터링 DAG는 dependency gate 뒤 숫자 ID 순으로 선택하며 집합 밖 READY는 cycle이 끝날 때까지 시작하지 않는다. 이때도 `PROJECT_BLOCKED`는 모든 실행을 막고, 다른 `PLAN_BLOCKED`·`PR_OPEN`과 dependency로 연결된 계획은 건너뛴다
- 마지막 리팩터링 계획 Merge 뒤에는 최신 원격 기준 사후 재검증을 성공하고 계획 개정의 원격 게시와 catalog 반영을 마칠 때까지 장벽 뒤 READY를 시작하지 않는다
- 각 ExecPlan은 고유하고 결정적인 이름의 branch와 별도 관리 worktree에서 작업한다
- 서로 다른 프로젝트는 `max_workers` 범위에서 병렬로 처리한다
- `max_workers` 기본값은 3이며 `config/config.ini`에서 변경할 수 있다
- `PLAN_BLOCKED` 또는 안정된 `PR_OPEN`이 있어도 명세 변경 우선 trigger를 먼저 평가하고, 없으면 독립 READY 하나를 기존 priority와 숫자 ID 순으로 시작한다. 독립 READY가 없으면 변경된 전체 입력만 한 번 재계획한다
- `PROJECT_BLOCKED` 동안에는 새 Planner, maintenance Planner, Worker와 저장소 변경 단계를 시작하지 않는다

### 복구 판단과 최종 차단

- 차단 후보는 disposition, 안정적인 원인 code, plan ID, phase, plan/base revision, 진단 지문과 진전 marker로 기록한다. 복구 disposition과 `PLAN_BLOCKED`/`PROJECT_BLOCKED` scope는 별도 기준이며 최종 사용자 조치가 확정된 뒤에만 원인 code로 scope를 정한다
- 일시적 Git·서비스 실패는 현재 실행을 보존해 다음 실행에서 재시도하고, 유효한 최신 base·catalog 및 실제 소유 Git 상태로 하나의 다음 단계를 확정할 수 있으면 상태를 재구성한다. archive 이동 집합과 rebase 충돌은 진전과 반복 한도를 기록하며 교정한다. Manager가 처음 rebase 충돌을 관측하거나 Worker 결과 형식 오류를 받으면 현재 실행을 끝내고 다음 실행에서 보존된 Worker 세션으로 재개한다. 진행 중인 Worker·검증·Review 교정은 같은 실행에서 이어갈 수 있다
- 일시 재시도, 상태 재구성, 한도 내 Agent 교정은 현재 phase, branch, worktree, 소유권, Agent session, 승인, 변경, 검증 원문과 실패 이력을 보존하며 차단, 원격 checkpoint, BLOCKED 알림을 만들지 않는다. 한 실행에서 최신 원격 snapshot이 안정되지 않으면 같은 상태로 일시 중단하고 다음 실행에서 다시 판정한다
- 승인·PR reopen·소유권·Agent 범위·원격 OID 불확실성, 안전한 원본으로 확정할 수 없는 손상, 반복 한도 소진과 의미를 확정할 수 없는 무변경 결과만 최종 사용자 조치로 차단한다. 최종 기록에는 복구 횟수, 마지막 안정 revision, 남은 불확실성과 실제 조치 대상을 포함한다
- 최종 `PLAN_BLOCKED`만 안전한 원격 checkpoint 대상이다. BLOCKED 알림은 최종 차단 transaction과 checkpoint 결과 뒤에 생성하고 동일 plan/scope/reason/fingerprint/revision은 한 번만 알린다. 사용자 수정 경로는 원인이 실제 대상을 제공할 때만 표시한다

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

### PR 생성 조건

- Worker가 현재 환경에 맞는 전체 검증 스크립트를 생성하거나 갱신해야 한다
- Worker는 전체 검증 스크립트를 직접 실행하지 않으며 Loop Manager 코드가 실행한다
- PR 생성 직전에 최신 `origin/<base-branch>`를 rebase로 반영하고 전체 검증과 Review를 다시 통과해야 한다
- 게시 직전 archive eligibility가 검증된 이동 집합과 다르면 같은 Worker 세션으로 돌아가 전체 검증과 Review를 다시 수행한다
- 관련 테스트와 전체 검증이 통과해야 한다
- Reviewer의 blocking issue가 없어야 한다
- 같은 ExecPlan branch에 기존 PR이 있으면 이를 복구해 사용하고 중복 PR을 생성하지 않는다
- 프로그램은 PR을 자동으로 Merge하지 않는다
- Merge commit, Squash merge와 Rebase merge를 모두 정상 Merge로 처리하며 Merge 방식만으로 관리 worktree를 보존하지 않는다
- Merge된 PR의 head와 관리 worktree를 대조해 PR 이후의 로컬 전용 commit이 없고 worktree가 clean이면 원격 head branch의 존재 여부와 관계없이 worktree를 자동 정리한다
- 미커밋 변경, PR 이후의 로컬 전용 commit 또는 안전 여부를 확인할 수 없는 상태만 `cleanup_pending`으로 보존한다

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

### Telegram 알림 조건

- Telegram token과 chat ID가 모두 설정된 경우에만 알림을 생성하고 전송한다
- 두 값이 모두 비어 있으면 알림을 비활성화하며 작업 흐름에는 영향을 주지 않는다
- PR 생성 알림에는 PR 링크가 포함돼야 한다
- 차단 알림에는 scope, 구조화 원인, 관련 ExecPlan, 실제 수정 대상이 확인된 경우에만 그 경로, checkpoint 성공 시 원격 commit link·OID 또는 미게시 시 local-only 보존 이유가 포함돼야 한다
- 차단 알림은 plan·scope·원인·지문·revision이 같을 때 같은 사건으로 중복 제거한다
- 프로젝트 완료 알림은 해당 프로젝트의 현재 작업 주기가 완료됐을 때 전송한다
- 동일한 이벤트는 한 번만 전송하며 실패한 알림만 다음 실행에서 재시도한다
