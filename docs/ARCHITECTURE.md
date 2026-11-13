# Architecture

> **문서 상태 — 구현 전 목표 설계.** [PRODUCT.md](PRODUCT.md)의 정책을 구현하기 위한 구조와 데이터 계약이다. 실제 적용은 구현·검증 이후에 이루어지며 이 브랜치를 Manager 자동 실행의 base로 사용하지 않는다.

## 1. 구조와 책임

Loop Manager는 외부 스케줄러가 호출하는 Python 프로그램이다. Manager는 원격 Git·GitHub를 관측해 작업을 선택하고, Planner·Worker·Reviewer에게 의미 판단을 위임한다. 상태 전이·검증 프로세스·Git 게시·PR 동기화는 Manager가 확정한다.

| 책임 | 구성 요소 |
|---|---|
| 원격 문서·PR 변경 관측, 입력 지문 비교 | Observer / Catalog |
| 요구 분석·ExecPlan 생성·계획 PR 갱신 | Planner / Planning |
| G별 구현·관련 테스트·교정 | Worker / Execution |
| 프로젝트 필수 검증 및 결과 확인 | Verify Runner |
| 후보·명세·검증 근거에 대한 독립 판정 | Reviewer |
| 작업 선택, 단계·동시성·재개 조율 | Coordinator |
| 격리 작업 공간, Merge, Commit·Push, PR 갱신 | Git / GitHub |
| G/R 완료·보관·작업 공간과 실행 데이터 정리 | Completion |
| 빠른 CLI 조회·조건부 상태 변경, 장기 작업 요청의 영속 기록 | CLI / State Store |
| 수동 구현·제출·명령 실행 | Coordinator / Runtime |
| 실행 runtime 선택·갱신 | Bootstrap / Runtime Handoff |
| 실행 상태·성공 근거·알림·사용량 | SQLite |
| 공통 Telegram Outbox | Notification |

이 표는 논리적 책임이며 구성 요소마다 클래스·패키지·별도 저장소 계층을 만드는 요구는 아니다. 공통 안전 조건은 실제 파일 변경·원격 게시·삭제 경계에서 한 번씩 확인한다.

**정보 원본:** 활성 Requirement·ExecPlan은 원격 base, 실제 코드와 작업물은 Git, PR의 Open·Merged·Draft·충돌 상태는 GitHub, 실제 실행 결과·진행 상태는 메인 SQLite가 기준이다. CLI의 요청 표식은 **후속 작업 의도**이며 실행 성공이 아니다. 모든 실행·CLI 상태는 동일 SQLite의 명시된 테이블에서 확인한다. SQLite 값만으로 GitHub의 Merge나 원격 게시 성공을 추정하지 않는다.

## 2. 문서와 변경 감지

Requirement는 `R-xxx`, ExecPlan은 `G-xxx` ID로 식별한다. 활성 문서는 `docs/requirements/`와 `docs/plans/`, 완료 문서는 각 `archive/`에 둔다. 파일 이름·경로 변경과 보관 이동은 삭제로 취급하지 않는다. 동일 ID 중복, 잘못된 frontmatter, 존재하지 않는 dependency와 순환 참조는 해당 문서·작업의 유효성 오류로 기록한다.

ExecPlan은 [PLANS.md](PLANS.md)에 정의된 `id`, `title`, `sources`, `depends_on`, `complexity`, `risk`, `priority` 필드를 사용한다. `priority`가 없는 문서는 `normal`로 해석한다. 세션·실행 상태·검증 결과는 본문 metadata에 넣지 않는다.

Planner는 활성 Requirement의 목표와 Acceptance Criteria를 비교한다. 실제로 상충하는 조건은 **R-ID가 높은 활성 Requirement의 해당 규칙만 우선**하며, 다른 조건은 보존한다. 적용 범위가 불확실하면 사용자 판단을 요청한다. Git 수정 시각이나 `blame`은 정책 우선순위가 아니다.

Observer는 원격 base·문서 내용·G의 정의·PR 상태를 구분해 관측한다. 입력 지문과 마지막 처리 결과가 유효하면 Planner·충돌 조회·Verify를 호출하지 않는다. 단순 base SHA 변경은 관련 작업의 의미 변경과 다르다. PR Merge·Close·Draft 관측은 충돌 판단 캐시와 독립적으로 갱신한다.

## 3. SQLite와 상태 저장 설계

**실행·명세·검증·복구에 필요한 상태만 저장하는 단일 SQLite 스키마**를 사용한다.

- `(repo, plan_id)`와 원격 URL·Commit SHA를 안정적인 연결 키로 사용한다.
- 프로젝트·문서의 확정 정의와 작업 실행 주기·Agent 호출·외부 게시 의도를 분리한다.
- 동일 사실을 `projects`와 `exec_plans`, 여러 복구 테이블에 동시에 복제하지 않는다.
- 필요한 성공 근거와 재시도 상태만 지속 저장한다. 실행 중 로그·snapshot·임시 파일은 필요할 때만 사용하고, 완료 뒤 확정 근거는 **작은 SQLite 값으로만** 보관한다. 별도 로그·Receipt 파일을 최종 보존하지 않는다.
- 원격 진실은 GitHub와 Git에 남긴다. SQLite가 없어도 원격 자료에서 안전하게 복원할 수 있어야 한다.
- **완료·취소한 G의 불필요한 실행 로그·세션·Checkpoint·호출별 토큰·임시 데이터는 정리**한다. R 완료·철회 뒤에도 불필요해진 판단 원본을 정리한다. 원격 PR·Archive 근거와 재실행 방지 ID, 필요한 전송 사건만 유지한다.
- 각 테이블은 단일 책임과 실제 복구 필요성을 기준으로 유지한다.

### 주요 테이블과 역할

아래는 논리적 데이터 계약이다. 최종 DDL과 인덱스는 구현 단계에서 정하되, 불필요하게 새 테이블을 늘리지 않는다.

| 테이블 | 주요 키·필드 | 목적 |
|---|---|---|
| `projects` | `repo`, `base_branch`, 등록·Provider 설정 | 프로젝트 설정 |
| `exec_plans` | `(repo, plan_id)`, 정의 필드, **`primary_requirement_id`**, `sources`, `status`, `branch`, `pr_url` | G 식별·주 R 소속·다른 R과 공유 관계·현재 상태 |
| `planning_input_baselines` | `(repo, scope)`, R-ID 단위 입력 지문·버전·마지막 확정 revision | 동일 Requirement의 계획 중복 실행 방지 |
| `execplan_runs` | `(repo, plan_id)`, `generation`, `input_fingerprint`, `worktree`, `retry_after`, `last_error`, `manual_mode`, **`request_id`, `request_kind`, `feedback_mode`, `request_result`** | G 실행 세대·수동 예약·후속 요청·비동기 CLI 결과. 실행 단계 중복 없음 |
| `agent_calls` | `invocation_id`, `repo`, `scope`, `role`, `provider`, `model`, `session_id`, 지침 해시·토큰 | 세션·호출 중복 방지·모델별 집계 |
| `validation_receipts` | `repo`, `plan_id`, `stage`, `input_fingerprint`, `generation`, 결과·근거 | Verify·Review 성공 재사용 |
| `managed_prs` | `repo`, `pr_number`, 종류·계획 PR의 **주 R-ID**·연결 G-ID 목록·head/base·Draft·Closed·충돌 관측 | 주 R별 계획 PR 하나와 구현 G별 PR 관측·연결 |
| `external_jobs` | 고유 작업 키, `repo`, 종류·대상·입력·상태·원격 확인 결과 | 중단 복구가 필요한 미완료 게시·Archive·정리만 기록 |
| `requirement_judgments` | 충족 여부·최종 판단 근거 | R의 최종 충족 판단 |
| `notification_outbox` | 고유 사건 키·전송 상태 | Telegram 재시도·중복 억제 |
| `execplan_tombstones` | `(repo, plan_id)`, 철회 revision·근거 | 취소 G-ID 재사용 방지 |

### 단일 SQLite WAL과 CLI 직접 접근

**운영 DB는 로컬 `loop-manager.db` 하나만 사용하고 WAL 모드로 운영한다.** CLI 요청은 해당 상태 테이블에 직접 기록하고 runtime은 새 작업을 선택할 때 이를 조회한다. WAL의 읽기·쓰기 병행을 활용하며, 같은 DB의 쓰기 트랜잭션은 여전히 한 번에 하나만 가능하다는 제약을 지킨다.

DB 접근에는 별도 애플리케이션 순번 대기열·접근 잠금을 두지 않고 일반 SQLite 연결·내장 트랜잭션·제한된 busy 재시도를 사용한다. **여러 runtime의 중복 실행을 막는 프로세스 실행 잠금은 유지**한다. CLI 프로세스는 이 runtime 실행 잠금을 획득하지 않는다.

| 테이블 | CLI가 직접 처리하는 내용 | runtime이 처리하는 내용 |
|---|---|---|
| `pause_scopes` | 전체·프로젝트 scope의 pause/resume 상태와 변경 순번 즉시 갱신 | 새 단계 배정 직전 최신 pause 상태 조회 |
| `exec_plans` / `execplan_runs` | 기존 G의 차단 해제·수동 시작 예약·수동 제출·복구 의도를 조건부 갱신. **수동 시작 브랜치·Worktree는 CLI에서 직접 준비** | 차단 재평가, 수동 제출 후 Verify·Review·PR, 일반 단계 전이 |
| `projects` | 등록·해제 등 설정 변경·후속 확인 필요 상태 기록 | 실제 원격·프로젝트 준비 작업 및 설정 영향 반영 |
| 각 상태·집계 테이블 | `list`·`status`·`usage`·`logs`의 읽기 전용 SELECT | 필요한 실행·집계·정리 기록 작성 |

`execplan_runs`의 실행 세대·수동 소유·복구 요청 표식과 `exec_plans.status`는 다른 정보를 표현한다. `manual_mode`는 작업의 수동 소유·준비 여부이지 별도의 G 실행 `phase`가 아니다. **CLI는 G의 목표 상태나 후속 작업 요청을 기록하되 미수행 Git·Verify·Review의 완료 상태를 임의로 기록하지 않는다.**

`init`에서 스키마와 SQLite WAL 설정을 적용하고 `PRAGMA journal_mode`가 실제 `wal`인지 확인한다. 평소 CLI 연결은 조회마다 모드 전환·스키마 수정을 시도하지 않는다. DB 파일이 없는 첫 실행과 이미 존재하는 DB의 접근·스키마 오류를 구분하고 운영 데이터를 임의 초기화하지 않는다.

**DB 트랜잭션은 읽기·조건 검사·필요한 INSERT/UPDATE·COMMIT으로 짧게 끝낸다.** Agent·Verify·Git·네트워크·프로세스 대기·파일 삭제·runtime 교체는 트랜잭션 밖에서 실행한다. 명령 처리마다 `BEGIN IMMEDIATE`를 무조건 선점하지 않고, 복합 조건부 변경에만 짧은 쓰기 트랜잭션을 사용한다. CLI에는 제한된 `busy_timeout`을 적용해 잠금 오류를 명확히 반환하거나 짧게 재시도하고, 성공하지 않은 쓰기를 성공으로 표시하지 않는다.

읽기 전용 CLI는 필요한 SELECT만 수행해 연결·읽기 트랜잭션을 즉시 닫는다. runtime의 길게 유지되는 읽기 snapshot을 허용하지 않으며 WAL 자동 checkpoint를 기본으로 사용한다. WAL의 `-wal`·`-shm` 파일은 SQLite가 관리하는 정상 파일이므로 실행 중 임의 삭제하지 않는다. 여러 호스트가 하나의 DB 파일을 공유하는 WAL 운용은 지원하지 않으며 기존 환경 이전은 이전 Writer 종료 후 진행한다.

`exec_plans.status`는 **G 실행 단계의 유일한 원본**이다. `execplan_runs`는 실행 세대·입력·대기 사유·수동 여부만 기록하며 `phase`를 두지 않는다. GitHub PR 상태는 별도 관측값이다.

수동 작업의 요청·준비·제출 상태는 **G 하나에 귀속**하며 `execplan_runs.manual_mode`와 관련 접수 상태로 관리한다.

`agent_calls`는 **세션과 사용량에 필요한 최소 호출 단위**만 남긴다. 호출별 상세 비용·프롬프트·전환 이력 전체를 새로운 DB에 복제하지 않는다. 사용량은 `invocation_id`로 중복 기록을 막고 PR 게시 시 역할·모델별로 합산한다. Planner의 여러 G에 공유된 사용량은 계획 PR에 귀속하고 각 G에 임의 분할하지 않는다. Provider별 입력·캐시 토큰의 의미를 유지하며 모르는 값은 0으로 채우지 않는다. PR 본문에 필요한 집계·재시도 근거를 보존하다가 완료 후 호출별 원본을 정리한다.

`validation_receipts`는 실제 성공 근거를 입력 지문에 연결한다. 후보·검증 설정 지문·성공 여부·간결한 판단 근거만 **SQLite 행에 저장**하고 테스트 출력 전체·Agent 대화·상세 로그 파일을 완료 증거로 보관하지 않는다. R의 최종 Acceptance Criteria 확인에 필요한 소형 근거는 확인이 끝날 때까지만 보존한다. 새로운 실행 주기가 이전 검증 결과를 자동 무효화하는 것은 아니다. **현재 후보에 같은 입력이 유지되고 성공 근거를 확인할 수 있을 때만 재사용**한다. 실행 중이거나 결과 확인이 안 된 시도는 PASS가 아니다.

`managed_prs`는 PR 식별과 원격 관측만 담고, `external_jobs`는 외부 효과의 성공 확인 또는 DB 적용이 아직 끝나지 않았을 때만 사용한다. 확인된 게시 정보는 PR 관측으로 이어지고 완료 job의 세부 기록은 정리한다. 모든 Git 조회·명령을 journal에 기록하지 않는다.

`external_jobs`는 서로 다른 외부 효과를 임의의 JSON 상태 기계로 합치는 수단이 아니다. 작업 종류·식별 대상·진행 단계·외부 성공 확인 방법을 구분한 작은 공통 기록이다. 수행 전에 의도를 기록하고, 재시작 후에는 원격을 먼저 확인한다. Archive·Worktree·불필요한 실행 데이터 정리는 실행·완료를 차단하지 않는 독립 후속 작업으로 실행한다. 정리 실패를 G의 BLOCKED로 승격하지 않는다.

## 4. Planning과 작업 반영

Planner 입력은 최신 유효 Requirement, 실제 코드·테스트, 활성·완료 G, 의존 관계와 열린 계획 PR로 구성한다. 이미 충족된 목표와 기존 G의 중복을 먼저 배제하고 현재 파악 가능한 전체 작업을 제안한다.

Planner는 **계획 평가를 시작한 Requirement를 주 R-ID**로 확정한다. 여기서 새로 도출된 G의 `primary_requirement_id`와 계획 PR의 주 R-ID를 일치시키고, `loop-manager/plans/{short-summary}`의 **하나의 주 R 계획 PR**에서 G를 추가·수정·제거한다. 그 G가 다른 R도 충족하면 `sources`에 관련 R을 연결하지만 **소속 PR은 바꾸지 않고 새로운 G·계획 PR을 중복 생성하지 않는다.** 다른 R의 변경으로 기존 공유 G의 필요성을 다시 평가할 수 있으나 생성 출처는 임의로 교체하지 않는다. 사용자 PR 편집을 보존하며 제안 G가 없어져도 주 R과 PR의 연결은 유지한다.

**같은 R의 계획에 `draft: true`인 G가 하나라도 있으면 GitHub Draft PR로 게시**한다. 일반 G와 draft G를 별도 계획 PR로 분리하지 않으며, 질문이 해소될 때 같은 PR에서 관련 G를 다시 평가한다. 모든 draft가 해소되고 실제 계획 후보가 있으면 같은 PR을 Ready로 전환한다. 사용자가 Merge 없이 닫은 계획 PR은 입력이 동일한 동안 자동 재생성하지 않는다.

사용자는 base·계획 PR·구현 브랜치에서 문서를 수정할 수 있다. base의 G는 실행 기준, 계획 PR의 G는 미확정 후보, 구현 브랜치의 편집은 해당 G의 작업 입력이다. 다른 G의 확정 목표는 base 변경 없이 자동 교체하지 않는다.

G 정의·Requirement 변경은 영향받은 현재 실행에 반영한다. 이미 열린 구현 PR에서 추가 작업이 필요하면 Draft 전환을 확인한 뒤 Worker·Verify·Reviewer를 다시 수행한다. 완료된 G는 수정으로 소급 미완료 처리하지 않는다.

제품 결정이 불명확한 계획은 **`draft: true`와 `Draft Decision`의 Question·Facts·Options·Impact**로 기록하고 그 G를 실행하지 않는다. 동일 계획 PR의 다른 미확정 G도 Merge 전에는 실행하지 않으며, base에 확정된 독립 G는 계속 진행한다. 사용자 답변 또는 문서 수정으로 미결정 사항이 해소되면 기존 G를 갱신한다. ExecPlan의 draft 선언과 GitHub Draft PR은 별개 정보지만, 계획 PR의 Draft 여부는 포함된 미해결 draft G로 결정한다. 계획 PR 등록 알림에는 draft G-ID·제목·질문을 포함한다.

### 불필요해진 계획 PR과 Requirement 완료

Planner가 최신 R 전체 Acceptance Criteria, 실제 코드·테스트와 이미 확정된 G를 대조하여 **해당 R의 모든 제안 ExecPlan이 불필요하고 남은 목표도 충족됐다고 확정**하면, 기존 미Merge 계획 PR을 Close하고 해당 R의 Archive를 후속 게시한다. **Close 직전 원격 PR의 최신 head/base 및 PR에만 있는 사용자 편집을 다시 확인**하고, 해당 편집이 R·G 판단에 영향을 주면 그 내용을 포함해 재평가한다. 미Merge 사용자 변경은 원격 PR·커밋·브랜치에 보존하고 자동 삭제하지 않는다. 다른 변경이 현재 기준을 바꾸었거나 충족 여부가 불확실하면 Close·Archive를 보류한다. G가 0개라는 사실만으로 완료를 추정하지 않는다.

PR Close 이후 Archive 게시 전에 중단되면 원격 PR의 Closed 상태·R의 최신 입력·완료 판단과 사용자 편집의 보존 여부를 확인하고 **Archive만 재시도**한다. 동일 입력으로 계획 PR을 다시 만들지 않는다. PR Close와 Archive는 외부 효과 기록에 결합하되, 이미 완료한 효과를 재실행하지 않는다. 판단 불충분·미충족·미해결 draft라면 계획 PR을 자동 Close하거나 R을 Archive하지 않고 해당 판단·계획 보완을 이어간다.

### Planner 실행 조건

Planner는 최초 계획, 활성 Requirement의 추가·수정·삭제, draft 질문 해소, 관련 G가 모두 소진된 Requirement의 최종 조건 확인에 필요한 경우만 실행한다. **PRODUCT·ARCHITECTURE·ExecPlan만의 변경과 코드·base SHA 변경은 독립적인 Planner 실행 사유가 아니다.** 완료 확인은 해당 R의 관련 작업을 소진한 시점에 수행하고 유효한 근거가 있으면 반복하지 않는다.

### 수동 개발·제출

`manual start`와 `manual complete`는 기존 G의 상태·의존성·요청 중복을 확인하고 짧은 SQLite 트랜잭션으로 반영한다. **`manual start`는 CLI가 Git 준비까지 완료하고 즉시 최종 결과를 돌려주며**, `manual complete`는 제출 접수만 반환하고 runtime이 Verify·Reviewer·PR을 수행한다.

- `manual start <repo> <G-ID>`: CLI는 base의 확정 G·dependency·진행 중 Writer를 확인하고, **CAS로 `manual_mode=PREPARING`을 예약·COMMIT**해 같은 G의 자동 Worker 선정을 막는다. 그 뒤 DB 트랜잭션 **밖에서** 해당 G의 브랜치·Worktree를 Git으로 준비한다. 실제 브랜치·경로 확인 후 `manual_mode=MANUAL_WORKING`을 기록하고 성공을 반환한다. Git 실패 시 오류를 바로 반환하고 준비 의도를 보존하며, 재실행 시 기존 경로·Git 결과를 확인해 안전하게 완료·재시도한다. 이미 자동 Worker가 예약한 G를 빼앗거나 다른 작업물·사용자 파일을 덮어쓰지 않는다.
- `manual complete <repo> <G-ID>`: runtime이 사용자 원격 브랜치의 제출 후보 HEAD를 확인해 Verify → Reviewer → 동일 G 구현 PR로 인수한다. 실패하면 같은 G에서 교정한다.
- **수동 제출은 확정된 G 하나의 브랜치·검증·구현 PR로 관리한다.**
- `manual_mode=PREPARING`부터 해당 G의 자동 Worker 선정을 중지한다. CLI 종료·Git 실패로 준비가 미확정이면 재실행에서 실제 브랜치·Worktree를 확인해 완료하거나 안전하게 보류한다. **준비 완료 전 `manual complete`는 성공으로 취급하지 않는다.** 준비 실패만으로 G를 BLOCKED로 전환하지 않는다. 수동 상태는 G별 최신 값만 남기고 불필요한 이력을 정리한다.
- 요청 접수와 자원 배정을 분리하고, 동일 G·브랜치의 중복·상충 요청을 검증한다. 수동 후보 HEAD가 바뀌면 기존 Verify·Review 근거를 재검사한다.
- 수동 G도 필수 Verify·Review를 거치며 구현 PR Merge 후에 완료된다.

## 5. Scheduling과 실행 전이

Coordinator는 명세·계획 갱신, 기존 진행·교정·복구, 신규 G, PR 게시, 완료 보관을 **선택 우선순위**로 처리한다. CLI는 `loop-manager.db`의 해당 테이블을 짧게 조회·조건부 수정하며, **`manual start`의 분리된 Git 준비만 CLI가 수행**한다. 나머지 원격 게시·Agent·Verify·PR 작업은 runtime이 담당한다. `run`은 기존 runtime 실행 잠금을 확인해 실행을 시작하는 진입 명령이며 별도의 DB 명령 큐를 거치지 않는다. `init`은 DB 생성·WAL 전환과 필수 스키마 초기화만 담당한다. 읽기 전용 `list`·`status`·`usage`·`logs`는 직접 SELECT하고 Agent·Verify·Git 게시를 유발하지 않는다. 전역 단계 장벽을 두지 않으며 독립 작업은 겹쳐 수행한다.

Worker·Reviewer는 전역 공통 AI 한도, Verify는 독립된 전역 1개 한도를 사용한다. 한 프로젝트의 Worker는 기본적으로 1개이며 그 Worker가 Verify에 들어가면 다른 독립 G의 Worker를 시작할 수 있다. 자원을 실제 사용하지 않는 PR Merge 대기·사용자 판단·재시도는 슬롯을 점유하지 않는다.

ExecPlan 실행 단계는 `exec_plans.status`의 `READY`, `WORKING`, `VERIFYING`, `REVIEWING`, `PR_OPEN`, `BLOCKED`, `CANCELLED`, `MERGED`로만 관리한다. 재시도·순번 대기는 `execplan_runs.retry_after`와 대기 사유로 표시한다. GitHub의 PR Open·Draft·Ready·Merged는 별도 관측 상태다. 재작업 중 `PR_OPEN`이었던 G가 다시 `WORKING`에 들어가도 동일 PR을 계속 연결한다.

모든 Agent·Verify·게시 후보는 `(repo, plan_id, generation, input_fingerprint)`에 결합한다. 새 주기나 취소 이후 늦게 도착한 결과는 CAS 방식의 현재 주기 검사를 통과하지 못하면 상태를 변경하지 않는다. **Worktree 소유권·전체 Git snapshot의 증명 실패는 단독 BLOCKED 사유로 사용하지 않는다. 원격·파일 경로·일시 작업 환경 오류는 해당 쓰기만 대기·재시도하며 Git 차단으로 승격하지 않는다.**

선행 G는 실제 구현 PR Merge 후에만 완료 의존성을 충족한다. 취소·삭제로 의존성이 사라지면 필요한 목표를 건너뛰지 않고 영향받은 G의 계획을 다시 판단한다. 독립 작업은 계속 진행한다.

### CLI 상태 변경과 runtime 재관측

CLI의 상태 변경은 **짧은 SELECT·조건부 UPDATE/INSERT를 같은 SQLite 트랜잭션에서 처리**한다. 이미 다른 프로세스가 바꾼 상태를 덮어쓰지 않도록 `WHERE status = ...` 등 현재 G 상태·실행 세대·요청 순번을 비교하는 CAS를 사용한다.

- `pause` / `resume`: `pause_scopes`의 최신 의도를 즉시 갱신한다. runtime은 신규 작업·Agent·Verify 단계 배정 직전에 읽는다. 이미 실행 중인 호출은 안전하게 반환하며, 오래된 pause 요청이 이후 resume을 뒤집지 않는다.
- `unblock`: CLI가 `BLOCKED` G와 대상의 현재 상태를 확인한 뒤 `execplan_runs`에 재평가·재개 요청을 기록한다. **요청 기록은 실제 충돌 해결이나 READY 전이의 성공이 아니다.** runtime이 안전한 경계에서 최신 원격·차단 사유를 재검증하고 유효할 때만 상태를 전이시킨다. 재개 불가·요청 거부가 확정되더라도 CLI의 최종 피드백이 없었다면 결과·원인·사용자 조치를 Telegram으로 통지한다.
- `recover`: 관련 G 또는 프로젝트의 현재 실행 기록에 복구 요청을 표시하고, runtime이 외부 Git 효과·검증 근거를 확인해 필요한 단계만 재개한다.
- `manual start` / `manual complete`: `manual start`는 CLI의 G 예약 후 직접 Git 준비·확정까지 처리하고 결과를 즉시 돌려준다. `manual complete`는 해당 G의 원격 제출 요청을 기록하며 runtime이 원격 HEAD 확인·Verify·Review를 수행한다. 준비 중인 G는 자동 Worker가 선택하지 않는다.
- 프로젝트 설정 변경: `projects`의 설정·등록 요청을 조건부 갱신하고 원격 확인 등 실제 외부 효과는 runtime에서 수행한다.
- `list` / `status` / `usage` / `logs`: 조회 시점의 DB 내용을 반환한다. 조회 자체로 Planner·Worker·Verify를 호출하거나 상태 기록을 만들지 않는다.

runtime은 시작·재시작, 현재 호출의 완료·실패·중단 후 **다음 단계·작업 선택마다** 최신 `pause_scopes`, 프로젝트 설정과 관련 G의 요청 표식을 다시 조회한다. 이미 진행 중인 Agent·Verify를 CLI 명령 때문에 강제 종료하거나, 각 작업 도중 일정 주기로 DB를 조회하는 별도 polling 루프를 만들지 않는다. 경합으로 실패한 CLI 변경은 명시적인 잠금·상태 변경 오류를 반환하며 영속 기록이 없는데 요청이 접수됐다고 주장하지 않는다.

요청 표식과 실제 상태 변경·외부 효과는 분리한다. runtime은 Git·Agent 작업 전 짧은 트랜잭션으로 실행 대상과 `generation`을 확정하고 COMMIT한다. 실행 종료 후 새 트랜잭션에서 현재 세대와 기대한 상태를 확인한 다음 결과를 적용한다. 중단되면 요청 표식·확정 상태·실제 GitHub 효과를 다시 관측하고 이미 완료된 외부 작업을 중복 실행하지 않는다. 요청이 적용·거부되거나 필요 없어지면 최종 결과와 알림 의도를 보존한 뒤 해당 G의 불필요한 요청 표식을 정리한다. **CLI가 접수 결과만 반환한 명령은 최종 결과(성공·실패·거부)를 기록하면서 동일 트랜잭션으로 `notification_outbox`에 사건을 추가**한다. 일시 오류·자동 재시도는 최종 결과로 기록하지 않는다. 한 G의 요청이나 잠금 문제가 다른 독립 G를 BLOCKED로 만들지 않는다.


### CLI 비동기 결과 식별

CLI가 요청을 반영할 때 `request_id`(G·프로젝트별 고유 ID), `request_kind`, `feedback_mode`(`CLI_FINAL` 또는 `TELEGRAM_PENDING`), 현재 요청 세대·결과를 **같은 짧은 트랜잭션**으로 저장한다. `unblock`·`recover`·`manual complete`처럼 실행을 넘기는 요청은 `TELEGRAM_PENDING`이며, 수동 Git 준비처럼 CLI가 최종 결과를 직접 돌려준 요청만 `CLI_FINAL`로 확정한다. **`manual start` 준비 도중 CLI가 중단돼 최종 응답이 확인되지 않은 경우에는 기존 Git 상태를 재관측해 재실행·결과 조회를 허용하고, 비동기로 최종 확정했다면 Telegram 통지 대상으로 처리**한다. 응답 전달과 DB 기록 사이의 중단은 미통지보다 중복 가능성을 감수하되 요청 ID·Outbox로 반복 전송을 억제한다. 단순 상태 조회·pause 등 즉시 확정되는 명령은 별도의 후속 알림을 만들지 않는다.

한 G에 미완료 비동기 요청이 이미 있으면 같은 내용의 반복 요청은 기존 ID로 응답하고, **결과를 덮어쓸 다른 요청은 즉시 거부하거나 기존 결과 확정 뒤에 다시 접수**한다. 프로젝트 단위 요청은 `projects`의 요청 표식으로 같은 규칙을 따른다. runtime은 실제 성공·거부·실패를 확정할 때 `request_id`·`feedback_mode`를 함께 조회한다. `TELEGRAM_PENDING`에 대해서는 **요청의 최종 결과 기록과 `notification_outbox` 저장을 동일 SQLite 트랜잭션으로 처리**하고, 전송될 사건을 남긴 뒤 요청 표식을 정리한다. 지연된 결과나 재시작에서 요청 ID가 같다면 중복 사건을 만들지 않는다. 즉시 처리된 명령은 Telegram에 반복 통지하지 않는다.

## 6. Agent 실행과 품질 지침

각 호출에는 Loop Manager runtime의 공통·역할별 지침과 프로젝트 고유 지침을 구분해 적용한다. **프로젝트 루트 `AGENTS.md`의 읽기·해석은 Agent CLI에 맡기고** Manager는 그 파일을 별도 등록·승인·입력 해시 검증 대상으로 삼지 않는다.

| 역할 | runtime 지침 |
|---|---|
| Planner | `docs/AGENTS.common.md` + `docs/AGENTS.planner.md` |
| Worker | 공통 + `docs/AGENTS.worker.md` + `docs/AGENTS.ponytail.md` |
| Reviewer | 공통 + `docs/AGENTS.reviewer.md` |

Ponytail은 Worker에 매번 정확히 한 번 전달한다. 재사용 가능한 코드·표준 도구를 우선하고 변경 범위를 작게 유지하면서 근본 원인을 해결한다. Specification Skill·Planner·PLANS에는 결과 우선·단계 구분·Progressive Disclosure를 문서 작성 기준으로 적용한다.

지침은 현재 선택 runtime 원본에서 불러오고 Provider의 기존 system/developer 지침과 프로젝트 규칙을 보존한다. 적용한 지침의 내용 해시를 Agent 결과와 연결한다. 같은 Worker 세션에는 새로운 변경·미해결 실패·완료 목표 위주로 전달한다. Codex·Claude는 사용자 개인 설정 전체를 복제하지 않는 자동 실행 전용 Provider별 홈을 사용하고 필요한 인증만 안전하게 재사용한다. 공통 Provider 홈에서도 호출별 세션·임시 파일·역할별 실행 설정·출력을 분리한다. 세션이 유실되면 최신 원격 작업물과 짧은 현재 문맥으로 새 세션을 시작한다.

Worker는 구현 완료 후보·Checkpoint·사용자 결정 요청·복구 불가 충돌을 구조화해 반환한다. Reviewer는 PASS 또는 구체적 교정 근거를 반환한다. **Manager만 실제 실행 단계와 PR 상태를 확정**한다. Provider 오류는 일시적 실패·사용량 한도·인증/설정 실패·세션 소실·출력 형식 오류를 구분하며 중복 재시도 계층을 만들지 않는다.

Worker는 관련 테스트를 실행하고 필요하면 중간 Checkpoint를 요청한다. Manager가 변경을 확인해 Commit·Push하고 작업물을 보존한다. Checkpoint는 최종 Verify·Review 성공이 아니다.

## 7. Verify와 Reviewer

기본 흐름은 **Worker 관련 테스트 → 프로젝트 필수 Verify → Reviewer**다.

Verify는 프로젝트에서 지정한 명령·실행 환경·작업 디렉터리·시간 제한을 사용한다. 언어별 Runner를 강제하지 않으며 테스트 내부 병렬화는 프로젝트의 기존 도구가 담당한다.

- 실제 Verify 시작 전 자원 대기와 실행 시간을 구분한다.
- 종료 코드·로그·시간 초과·취소 및 하위 프로세스 종료 여부를 확인한다.
- 성공 근거는 해당 코드 후보·명세·검증 명령과 입력 지문에 결합한다.
- 실제 입력이 변하지 않았고 유효한 성공 근거가 있으면 재실행하지 않는다. **검증 명령·설정·테스트·의존성 등 필수 입력을 안전하게 열거·비교할 수 없으면 성공을 재사용하지 않는다.** 근거가 없거나 무효화되면 필요한 단계를 다시 실행한다.
- 타임아웃·중단·종료 불명은 PASS가 아니다. Reviewer는 전체 Verify를 반복하지 않고 전달된 근거를 검토한다.
- Reviewer 결과도 diff·명세·검증 근거·지침 해시에 결합한다.

## 8. Git과 PR

Planner·G별로 격리된 작업 디렉터리를 사용하며 사용자 기본 checkout은 수정하지 않는다. 실행에 필요한 경로·대상 저장소·브랜치와 동시 쓰기 충돌만 확인한다. 불필요한 Git 소유권·파일 상태 증명을 실행 전제에 추가하지 않는다. **ignored-only 파일·테스트 캐시·빌드 부산물의 생성·변경은 Agent 권한 위반이나 BLOCKED로 처리하지 않는다.** Git이 실제로 덮어쓸 경로와 충돌하면 파일을 보존해 해당 쓰기만 보류하고 가능하면 새 격리 공간에서 계속한다. 이전 Worktree를 안전하게 재사용할 수 없고 원격 작업물이 유효하면 새 공간에서 이어간다.

Manager만 Commit·Push·PR 게시를 담당한다. 원격 게시 전에 실제 대상 branch와 HEAD를 확인하고, 충돌 시 원격 변경과 **Merge**로 통합한다. 게시 결과가 불명확하면 실제 원격을 먼저 조회한다. 강제 Push·자동 Rebase·Reset으로 공개 이력을 재작성하지 않는다.

GitHub의 PR mergeability가 충돌 판단의 기준이다. 신규 PR 또는 PR head·base HEAD 변경 시 조회하며 미확정이면 필요한 경우 재조회한다. 충돌이 확인된 경우에만 최신 base를 작업 브랜치에 Merge하고, 계획 문서 충돌은 Planner·구현 충돌은 Worker가 교정한다. **자동 Merge·Agent 교정으로 해결 불가가 확정된 경우에만 Git 원인의 BLOCKED**를 기록한다. 원격 조회 경쟁·일시 게시 실패·경로 충돌은 대기·재시도하며 독립 작업을 막지 않는다. 단순 base 전진만으로 Merge·Verify를 반복하지 않는다. **미Merge 관리 작업 브랜치의 실제 원격 삭제는 자동 재생성하지 않으며** 해당 G의 기록과 작업물을 보존해 명시적인 재시작·복구 입력을 기다린다.

계획 PR은 미해결 draft G가 있을 때 Draft로 등록하고, 그 G 목록과 질문을 PR 등록 알림에 한 번에 표시한다. 구현 PR에서 추가 작업이 필요하면 기존 PR을 Draft로 전환한다. 현재 후보가 Verify·Review를 통과하고 충돌 해소가 확인되면 Ready로 전환한다. 같은 후보의 재조회와 재시작은 새 알림을 만들지 않는다.

**외부 효과 재개:** 작업별 Commit·Push·PR 생성/수정·Draft/Ready·Archive에는 중복 실행을 막는 의도·대상·기대 입력·확인 상태를 보존한다. 새 실행에서는 먼저 GitHub·원격 Git의 실제 결과를 확인한 다음 필요한 단계만 수행한다.

## 9. 완료·취소·정리

구현 PR의 실제 Merge가 확인되면 G를 `MERGED`로 처리하고 `docs/plans/archive/`에 이동한다. Archive 게시 실패는 이동만 재시도한다. 실제 Merge에 따른 다른 G의 dependency 충족은 유지한다.

관련 G의 작업이 모두 소진되거나 이미 직접 구현으로 충족된 R은 Planner가 최신 Acceptance Criteria와 구현·테스트 근거를 다시 대조한다. 충족하면 `docs/requirements/archive/`로 이동하고, 미충족이면 필요한 G만 계획한다. **열린 계획 PR에서 더 이상 필요한 G가 없고 R 전체 충족이 확인되면 그 PR을 Close한 뒤 Archive한다.** 판단 근거가 불충분하면 완료를 유보한다. 각 G Merge 때마다 전면 평가를 반복하지 않는다.

Requirement·ExecPlan의 실제 철회는 ID와 변경 근거를 확인한 경우에만 취소한다. 다른 R에서도 필요한 공유 G는 남은 목표를 유지하고, 미Merge 구현 PR은 필요 없는 경우 안전하게 Close한다. 삭제·이름 변경·보관·조회 실패는 서로 구분한다. 완료·취소 ID는 다시 사용하지 않는다.

**Worktree와 불필요한 실행 데이터의 정리는 G의 완료·취소 확정 이후 별도 후속 작업**이다. 최종 PR 사용량과 R 최종 판단에 필요한 소형 DB 근거를 먼저 확정하고 로그·검증 출력·Process Receipt·임시 파일을 삭제한다. **최소 확정 결과는 DB에만 남기며 별도 결과 파일을 장기 보존하지 않는다.** 미게시 사용자 변경은 보존한다. 최종 PR의 사용량 집계·Archive·미전송 알림에 필요하다면 해당 원본 정리를 미룬다. 이후 세션·검증 근거·호출별 토큰·로그·Checkpoint 등 불필요한 로컬 기록은 삭제하고, 취소 G는 재사용 방지 최소 표식을 유지한다. 새 환경에서는 원격 Git 이력과 관련 PR의 기존 G-ID도 확인해 DB 부재만으로 취소 ID를 신규 G에 재할당하지 않는다. 실제 삭제 직전에 작업 디렉터리·미게시 변경·진행 중 Git 작업·프로세스를 확인한다. 제거할 수 없는 디렉터리는 보존하고 정리만 재시도한다. 소유권 증명이나 정리 실패로 G 완료·다른 독립 실행을 막지 않는다. 원격 작업 브랜치는 별도 보존 정책 없이 지우지 않는다.

## 10. 중단·재시작·새 환경 복구

재시작 시 단일 SQLite의 현재 실행·CLI 요청 표식·외부 효과와 실제 Git·GitHub 상태를 대조하고 마지막 확정된 단계 이후부터 재개한다. 영속 요청 표식은 해당 G·프로젝트에서 읽어 실제 적용 여부를 확인한 뒤 필요한 작업만 처리한다. Runtime 인계용 임시 영수증 파일은 미확정 인계 중에만 사용하며 인계 확인·소비 후 삭제한다. 이는 최종 보존 결과가 아니다. 일시적인 API·자원 오류는 자동 재시도하고, 해결할 수 없는 정책 충돌이나 필수 사용자 입력 부족 등만 관련 G를 차단한다. 이전 Agent 결과가 늦게 도착하면 `generation`과 입력을 확인한 뒤 적용한다.

DB 없는 새 환경에서는 Repository·base·활성/보관 문서·`loop-manager/G-xxx` 브랜치·기존 PR 이력으로 안전하게 복구한다. 원격 자료가 있으면 새 격리 공간에서 구현을 이어가고, 근거 없는 Verify·Review 성공과 과거 세션·토큰 사용량은 합성하지 않는다. 원격에 게시되지 않은 로컬 전용 변경은 복원 대상이 아니다. 환경 이전은 이전 Writer가 종료된 순차 인수를 기본으로 한다.

## 11. 자체 runtime 선택과 갱신

Bootstrap은 자신의 등록 원격 base에서 **검증된 실행 revision**을 선택한다. 사용자 기본 checkout이나 G의 Worktree는 실행 코드 원본이 아니다. 선택 runtime의 경로·Repository·SHA가 실제로 일치하는지만 확인하며, 다른 Worktree 변화나 과거 scope snapshot을 차단 조건으로 삼지 않는다.

원격 실행 코드·스크립트·의존성·공통/역할별 `docs/AGENTS.*.md`의 실질적인 변화는 **새 단계 진입 시점에** 확인한다. 중복 조회는 마지막 성공 관측으로 억제하고 자원·사용량·PR 대기에는 변경 감시용 polling을 수행하지 않는다. Requirement·ExecPlan·PRODUCT·ARCHITECTURE만의 수정이나 SHA 변경 자체는 runtime 갱신 조건이 아니다.

갱신 필요가 확정되면 새 Agent·Verify·게시 단계의 배정을 멈추고 진행 중 호출의 안전한 반환·Checkpoint·상태 저장 뒤 최신 runtime으로 인계한다. 진행 중 호출을 강제 종료하지 않으며 G의 PR Merge도 기다리지 않는다. 자기 runtime 갱신은 새 기능 자동 개발·PR Merge와 별개의 동작이다.

## 12. 모델 사용량과 PR 본문

Provider가 제공하는 실제 토큰 사용량을 **Planner·Worker·Reviewer × Provider·모델**별로 집계한다. `agent_calls.invocation_id`가 중복 집계를 막는다. 실패·재시도 호출에도 실제 사용량이 보고됐다면 포함하며, 수집 실패나 누락된 값은 0이 아닌 확인 불가로 표시한다.

Codex·Claude의 입력·캐시 읽기·캐시 쓰기·출력 토큰 정의를 보존해 중복 합산하지 않는다. 가격 환산·사용량 대시보드·상세 프롬프트 이력은 구축하지 않는다.

계획 PR에는 관련 Planner 집계, 구현 PR에는 그 G의 Worker·Reviewer 집계와 공유 Planner 계획 PR 링크를 표시한다. 한 Planner 호출이 여러 G를 만든 경우 사용량을 임의로 분할하지 않는다. 같은 G의 재작업 누적 사용량을 반영하고, 본문 수정 시 사용자 작성 영역은 보존하며 Manager가 관리하는 집계 영역만 갱신한다. PR 집계·게시가 확정되고 작업이 종료되면 호출별 사용량 원본은 필요 범위가 사라진 순서대로 삭제한다. 사용량 수집 오류는 PR 게시를 막지 않는다.

## 13. Telegram과 보안

모든 프로젝트의 Telegram은 기존 `notification_outbox`를 활용하는 공통 큐에서 처리한다. 작업·PR·현재 후보·차단 원인으로 사건 키를 만들어 중복 전송을 억제한다. **비동기 CLI 명령의 결과는 프로젝트·대상 G·명령 종류·요청 순번에 결합한 사건 키**로 한 번만 발행한다. 최종 결과의 성공·거부·실패 여부와 원인, 현재 상태 및 필요한 사용자 조치를 작은 payload에 포함한다. 처리 도중 종료되면 기존 요청과 실제 상태를 대조해 동일 결과 알림을 다시 생성하지 않는다. 계획/구현 PR 등록, 구현 PR Ready 재검토, 충돌 복구, 최종 차단과 확인된 장기 무진전은 일반 알림이고, 신규 G 시작·자율적으로 재개된 G는 무음이다. **명시적 CLI `unblock` 요청에 대한 늦은 재개 성공 또는 재개 불가 확정은 일반 알림**이다. 즉시 CLI로 최종 결과를 돌려준 경우에는 별도 결과 알림을 보내지 않는다. 계획 PR이 Draft라면 등록 사건에 draft G-ID·제목·질문을 포함하고 별도 draft 알림을 생성하지 않는다. Draft 해제와 충돌 해소가 같은 후보의 사건이면 하나의 알림으로 합친다. 전송 실패는 개발 작업을 막지 않는다. 해결·취소돼 더 이상 유효하지 않은 미전송 payload는 취소하며 발송 뒤 불필요한 본문 데이터는 정리한다.

Agent에는 역할·작업 공간에 필요한 최소 권한과 프로젝트 설정만 전달한다. 사용자 checkout·다른 G의 작업물·공개 커밋을 덮어쓰지 않고, 비밀값·환경정보가 포함된 로그를 PR·Telegram에 노출하지 않는다. 실제 대상 게시·삭제 시점에만 필요한 보호 검사를 수행한다. ignored-only 변경이나 다른 Worktree의 일반 변경을 Agent의 권한 위반으로 추정하지 않는다.

## 14. 구현과 검증

실제 운영 데이터·Git 커밋·작업 브랜치·PR·전송 알림은 보존한다.

**Git 테스트는 최소화한다.** 정책 조합·SQLite·실패·재개의 대부분은 Mock과 작은 단위 테스트로 검증하고, 실제 Git·CLI 통합 테스트는 Merge 충돌·사용자 변경 보존·게시 후 중단 복구·ignored-only 비차단처럼 실제 효과를 확인해야 하는 소수 대표 경로에 집중한다. 같은 결과를 검증하는 반복적인 실제 Git 실행·대규모 조합을 추가하지 않는다.

실행 검증은 정상 작업뿐 아니라 다음 경계를 포함한다.

- 최신 문서 변경·충돌하는 R-ID 우선순위, 동일 입력 계획 중복 방지
- 완료/취소 후 불필요한 데이터 정리·중단 재시도·기존 PR/알림 중복 방지
- G별 CLI `manual start` 예약 → Git 준비 → 확정·오류·중단 복구, `manual complete`의 자동 검증 및 자체 runtime 갱신·pause 초기 판정
- 주 Requirement의 공유 G 소속·R별 계획 PR·Draft 전환·최신 사용자 수정 반영 후 모든 G 불필요 판정·PR Close와 R Archive의 중단 복구
- WAL의 동시 읽기·짧은 CLI 쓰기·런타임 쓰기 경합에서 응답 시간을 제한하고, DB 접근 순번 대기열 없이 동작하는지
- 트랜잭션 중 Agent·Git·Verify·네트워크 호출이 없고, 오류 시 잠금을 반환하며 다른 G는 진행 가능한지
- 같은 G의 수동 요청·차단 해제·복구·상충하는 pause/resume을 조건부 갱신하고 다음 배정 경계에서 정확하게 관측하는지
- 실행 중 CLI 요청·재시작·오래된 Agent 결과·DB busy와 SQLite 초기 상태에서 중복 효과나 작업 유실이 없는지
- 계획 Draft PR 등록·알림 및 ignored-only 파일의 비차단 처리
- Worker·Verify·Reviewer의 겹친 실행, 오래된 호출 결과의 적용 거부
- GitHub 충돌 확인 후 Merge 교정과 비충돌 시 불필요한 복구 배제
- 구현 PR Draft → Ready 전환과 현재 후보의 검증 근거
- 이전 Worktree를 사용 못할 때 원격 브랜치로 복구, 작업물 보존
- MERGED 후 G Archive와 R 완료 재확인·후속 계획·Archive
- 완료 후 Worktree 정리 실패가 다른 작업을 막지 않음
- Agent 지침·Ponytail·세션·Checkpoint·실제 모델 사용량 PR 집계
- 비동기 CLI `unblock` 재개 성공·재개 거부 통지, 요청 시점의 피드백 방식 보존·중복 요청 및 즉시 결과 응답의 중복 억제
- CLI 결과 확정 직후 중단 시 결과·Outbox 원자 기록과 재전송 중복 방지
- Telegram 재전송·중복 억제, DB 없는 새 환경의 원격 복구
- 서로 다른 언어·프레임워크에서 프로젝트 정의 Verify 명령 실행

문서만 변경된 상태에서는 이 결과를 실제 구현 테스트 성공으로 표현하지 않는다.
