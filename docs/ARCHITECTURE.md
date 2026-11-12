# Architecture

> **문서 상태 — 재설계 목표 (미구현).** [PRODUCT.md](PRODUCT.md)의 목표를 구현하기 위한 구조와 데이터 계약이다. 현재 `main`의 구현·DB·문서와 구분한다. 이전 구조는 `main` 및 Git 이력에서 참고하며 새 정책에 충돌하는 기존 규칙은 이행 시 교체한다. 이 브랜치를 Manager 자동 실행의 base로 사용하지 않는다.

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
| 모든 CLI 명령의 공통 접수·영속 대기열·조회 결과 | CLI / Control Inbox |
| 수동 구현·제출·명령 실행 | Coordinator / Runtime |
| 실행 runtime 선택·갱신 | Bootstrap / Runtime Handoff |
| 실행 상태·성공 근거·알림·사용량 | SQLite |
| 공통 Telegram Outbox | Notification |

이 표는 논리적 책임이며 구성 요소마다 클래스·패키지·별도 저장소 계층을 만드는 요구는 아니다. 공통 안전 조건은 실제 파일 변경·원격 게시·삭제 경계에서 한 번씩 확인한다.

**정보 원본:** 활성 Requirement·ExecPlan은 원격 base, 실제 코드와 작업물은 Git, PR의 Open·Merged·Draft·충돌 상태는 GitHub, 실제 실행 결과·진행 상태는 메인 SQLite가 기준이다. 독립된 제어 DB의 명령 기록은 **요청이 접수됐다는 근거**이지 실행 성공이나 G 상태의 대체 원본이 아니다. SQLite 값만으로 GitHub의 Merge나 원격 게시 성공을 추정하지 않는다.

## 2. 문서와 변경 감지

Requirement는 `R-xxx`, ExecPlan은 `G-xxx` ID로 식별한다. 활성 문서는 `docs/requirements/`와 `docs/plans/`, 완료 문서는 각 `archive/`에 둔다. 파일 이름·경로 변경과 보관 이동은 삭제로 취급하지 않는다. 동일 ID 중복, 잘못된 frontmatter, 존재하지 않는 dependency와 순환 참조는 해당 문서·작업의 유효성 오류로 기록한다.

ExecPlan은 기존 [PLANS.md](PLANS.md)의 `id`, `title`, `sources`, `depends_on`, `complexity`, `risk`, `priority` 필드를 유지한다. 기존 문서 중 `priority`가 없는 경우 `normal`로 해석한다. 세션·실행 상태·검증 결과는 본문 metadata에 넣지 않는다.

Planner는 활성 Requirement의 목표와 Acceptance Criteria를 비교한다. 실제로 상충하는 조건은 **R-ID가 높은 활성 Requirement의 해당 규칙만 우선**하며, 다른 조건은 보존한다. 적용 범위가 불확실하면 사용자 판단을 요청한다. Git 수정 시각이나 `blame`은 정책 우선순위가 아니다.

Observer는 원격 base·문서 내용·G의 정의·PR 상태를 구분해 관측한다. 입력 지문과 마지막 처리 결과가 유효하면 Planner·충돌 조회·Verify를 호출하지 않는다. 단순 base SHA 변경은 관련 작업의 의미 변경과 다르다. PR Merge·Close·Draft 관측은 충돌 판단 캐시와 독립적으로 갱신한다.

## 3. SQLite 설계 원칙과 기존 DB 판정

기존 DB를 통째로 유지하지 않는다. **유용한 키·필드·데이터 의미는 재사용하고, 지나치게 넓거나 중복된 테이블은 새 스키마에 이관한다.**

- `(repo, plan_id)`와 원격 URL·Commit SHA는 안정적인 연결 키로 재사용한다.
- 프로젝트·문서의 확정 정의와 작업 실행 주기·Agent 호출·외부 게시 의도를 분리한다.
- 동일 사실을 `projects`와 `exec_plans`, 여러 복구 테이블에 동시에 복제하지 않는다.
- 필요한 성공 근거와 재시도 상태만 지속 저장한다. 실행 중 로그·snapshot·임시 파일은 필요할 때만 사용하고, 완료 뒤 확정 근거는 **작은 SQLite 값으로만** 보관한다. 별도 로그·Receipt 파일을 최종 보존하지 않는다.
- 원격 진실은 GitHub와 Git에 남긴다. SQLite가 없어도 원격 자료에서 안전하게 복원할 수 있어야 한다.
- **완료·취소한 G의 불필요한 실행 로그·세션·Checkpoint·호출별 토큰·임시 데이터는 정리**한다. R 완료·철회 뒤에도 불필요해진 판단 원본을 정리한다. 원격 PR·Archive 근거와 재실행 방지 ID, 필요한 전송 사건만 유지한다.
- 테이블 선택은 구현의 복잡성을 줄이기 위한 것이다. 과거 테이블을 삭제하려는 목적 자체로 새 테이블을 만들지 않는다.

### 기존 테이블·필드별 재사용 판단

`main`의 `loop_manager_internal/storage/database.py` 및 각 `*_state.py`에 정의된 실제 스키마를 기준으로 한다.

| 기존 테이블 | 결정 | 유지하거나 이관할 정보 |
|---|---|---|
| `projects` | 핵심 필드 재사용 | `repo`, `base_branch`, `origin`, `provider`, `registered`, `max_tasks_per_project`. `processed_sha` 등 관측값은 캐시로만 사용 |
| `exec_plans` | **테이블 재구성** | `repo`, `plan_id`, `path`, `title`, `sources`, `depends_on`, `priority`, `content_hash`, `current_complexity`, `current_risk`, `current_revision_sha`, `status`, `branch`, `pr_url`, `pr_head_oid`의 의미를 계승 |
| `planning_input_baselines` | 재사용·버전 갱신 | `repo`, `scope`, 입력 항목, 지문 형식·기준 revision. 활성 Requirement·실제 계획 판단 입력에 맞는 지문 버전 사용. PRODUCT·ARCHITECTURE·지침 단독 변경은 Planner 실행 조건이 아님 |
| `requirement_judgments` | 재사용 | `source_path`, `input_token`, `outcome`, `criteria_evidence`, `relevant_paths`, `assessed_sha`, `invalidation_reason`. 이동 전후 R-ID 연결은 별도로 확인 |
| `notification_outbox` | 재사용 | `event_key`, `repo`, `event_type`, `payload`, `attempts`, `sent_at`, `canceled_at` 등 중복 억제·재전송 기록 |
| `agent_invocations` | 필요한 정보만 재구성 | 세션·PR 토큰 집계와 중복 방지에 필요한 필드만 유지. 작업 완료 후 불필요한 호출 원본 삭제 |
| `verify_receipt_cycles` / `verify_receipts` | 단순화 | 현재 후보·검증 입력·결과의 유효성만 유지. 부분 Verify와 세부 재사용 연쇄 제거 |
| `execplan_archive_items` / `requirement_archive_targets` / `requirement_move_receipts` | 공통 후속 작업으로 통합 | 완료 확인 근거, 기대 원격 revision, 진행 단계 및 게시 재시도 |
| `cleanup_pending` / `merged_worktree_cleanups` | 새 정리 작업으로 대체 | 경로·미게시 변경 확인·재시도만 유지. `workspace_owned` 증명과 필수 `pr_url` 제약은 승계하지 않음 |
| `execution_checkpoints` 및 `general_planning_publications` | 외부 효과 기록으로 통합 | 작업·입력·대상·원격 확인 상태·재시도. Rebase·소유권 지문 필드는 제외 |
| `recovery_records` / `persistent_execution_failures` | 단순한 대기·차단 원인으로 대체 | 재시도 조건과 사용자 판단을 유지하되 Git 차단은 자동 Merge 충돌 해소 실패에 한정 |
| `rebase_corrections` / `blocked_checkpoints`의 소유권 증명 계열 | 승계하지 않음 | Rebase 전용 상태·과거 snapshot 증명·진단 브랜치 연쇄 관리 제거 |
| `preliminary_reviews` / 유지보수·공통 결함·별도 테스트 분산 계열 | 기본 구조에서 제외 | 부분 Verify·선행 Review·자동 정기 유지보수는 새 PRODUCT의 기본 흐름이 아님 |

`exec_plans`에는 현재 구현·Review·Verify·복구·게시·Agent 세션 필드까지 함께 존재한다. 새 스키마에서는 **계획 정의와 현재 종단 상태를 간결하게 유지하고, 단계별 진행은 별도 실행 기록에 둔다.** `projects`에도 중복 저장된 단계별 상태·세션 필드는 이어받지 않는다.

### 목표 최소 스키마

아래는 논리적 데이터 계약이다. 최종 DDL과 인덱스는 구현 단계에서 정하되, 불필요하게 새 테이블을 늘리지 않는다.

| 테이블 | 주요 키·필드 | 목적 |
|---|---|---|
| `projects` | `repo`, `base_branch`, 등록·Provider 설정 | 프로젝트 설정 |
| `exec_plans` | `(repo, plan_id)`, 정의 필드, `status`, `branch`, `pr_url` | G 식별·현재 계획·종단 상태 |
| `planning_input_baselines` | `(repo, scope)`, R-ID 단위 입력 지문·버전·마지막 확정 revision | 동일 Requirement의 계획 중복 실행 방지 |
| `execplan_runs` (신규) | `(repo, plan_id)`, `generation`, `input_fingerprint`, `worktree`, `retry_after`, `last_error`, `manual_mode` | G 실행 세대·수동 접수·대기·재개. 실행 단계 중복 없음 |
| `agent_calls` (간소화) | `invocation_id`, `repo`, `scope`, `role`, `provider`, `model`, `session_id`, 지침 해시·토큰 | 세션·호출 중복 방지·모델별 집계 |
| `validation_receipts` (통합) | `repo`, `plan_id`, `stage`, `input_fingerprint`, `generation`, 결과·근거 | Verify·Review 성공 재사용 |
| `managed_prs` (신규) | `repo`, `pr_number`, 종류·계획 PR의 source R-ID·연결 G-ID 목록·head/base·Draft·Closed·충돌 관측 | R별 계획 PR 하나와 구현 G별 PR 관측·연결 |
| `external_jobs` (통합) | 고유 작업 키, `repo`, 종류·대상·입력·상태·원격 확인 결과 | 중단 복구가 필요한 미완료 게시·Archive·정리만 기록 |
| `requirement_judgments` | 기존 완료 판단 근거 | R의 최종 충족 판단 |
| `notification_outbox` | 기존 고유 사건·전송 정보 | Telegram 재시도·중복 억제 |
| `execplan_tombstones` (신규) | `(repo, plan_id)`, 철회 revision·근거 | 취소 G-ID 재사용 방지. 기존 `execplan_cancellations`의 유효 기록만 이관 |

### 독립 CLI 제어 저장소

`control.db`는 메인 `loop-manager.db`와 분리된 SQLite 파일이다. **모든 CLI 명령은 제어 DB를 공통 요청·응답 경계로 사용**한다. CLI는 메인 DB 쓰기·Manager 실행 잠금·Git·Agent 실행에 직접 진입하지 않지만, **메인 DB를 짧게 읽기 전용으로 여는 것은 허용**한다. CLI의 직접 조회 결과는 그 시점의 확정된 SQLite 상태이며, 실행 시점의 최종 판단은 runtime이 담당한다.

| 테이블 | 주요 정보 | 역할 |
|---|---|---|
| `pause_scopes` | 전체·프로젝트 scope, 최신 pause 상태·변경 순번 | 즉시 반영할 새 단계 진입 제어 |
| `control_requests` | 고유 `request_id`, 명령 종류, repo/G 등 대상, 최소 인자, 접수 순서, `status`, 요약 결과·확인 시각 | 변경·수동 구현·초기화·실행·조회 등 **모든 CLI 요청**과 처리 결과 |
 
요청 상태는 `PENDING`(접수), `PROCESSING`(runtime 인수), `APPLIED`(실행 또는 조회 완료), `REJECTED`(유효성 오류)를 구분한다. 일시 오류는 재시도 시각·사유를 남겨 `PENDING`으로 돌린다. 조회 결과는 길이·보존 기간을 제한한 작은 값으로 관리하고, 출력용 로그 전체나 상세 실행 데이터를 제어 DB에 장기 보관하지 않는다. 같은 요청 ID를 다시 처리하거나 완료 기록을 재조회해도 실제 효과를 중복 발생시키지 않는다.

제어 DB에는 짧은 읽기·쓰기 트랜잭션만 허용한다. CLI 접수는 메인 DB 접근 순번과 Manager 전역 실행 잠금을 획득하지 않는다. CLI 간 경쟁은 SQLite의 짧은 busy 대기·재시도로 처리하며, 대기열에 긴 작업을 수행하는 트랜잭션을 만들지 않는다. 제어 DB가 실제로 쓰기 불가능하면 **접수했다고 표시하지 않고 오류를 반환**한다.

`init`·`run`·`list`·`status`·`usage`·`logs`도 동일한 제어 경계를 사용한다. **읽기 전용 명령은 메인 DB의 직접 조회가 가능하면 즉시 처리**하고 요청 ID의 결과·확인 시각을 제어 DB에 기록한다. 읽기가 불가능하거나 원격 재관측이 필요한 항목만 runtime 조회로 넘기며 이전 결과를 현재 값처럼 표시하지 않는다. `run` 요청은 접수 뒤 runtime이 없으면 경량 기동기를 통해 처리 주체를 시작한다. 초기 `control.db` 파일·스키마를 생성하는 Bootstrap 외에는 **CLI가 메인 실행 DB를 직접 초기화하거나 명령 효과를 즉시 적용하지 않는다.** runtime이 없을 때 다른 변경 명령은 다음 예약 실행 또는 명시적 `run`에서 처리될 수 있다.

기존 `pause.db`의 유효한 pause 값은 `control.db`로 한 번 이관한다. 기존 파일 조회 오류는 일시중지 해제의 근거로 사용하지 않는다. **새 환경에 이전 제어 파일이 모두 없다면 pause 없음·미처리 명령 없음으로 초기화**하며, 원격 Git 상태로 사용자가 접수하지 않은 명령을 생성하지 않는다.

`exec_plans.status`는 **G 실행 단계의 유일한 원본**이다. `execplan_runs`는 실행 세대·입력·대기 사유·수동 여부만 기록하며 `phase`를 두지 않는다. GitHub PR 상태는 별도 관측값이다.

기존 `manual_work_groups`·`manual_work_members` 등 다중 G 수동 제출 그룹은 계승하지 않는다. 수동 요청은 기존 G 하나에 연결하며 `execplan_runs.manual_mode`와 미완료 접수 상태로 처리한다. 별도 그룹·구성원 테이블은 만들지 않는다.

`agent_calls`는 **세션과 사용량에 필요한 최소 호출 단위**만 남긴다. 호출별 상세 비용·프롬프트·전환 이력 전체를 새로운 DB에 복제하지 않는다. 사용량은 `invocation_id`로 중복 기록을 막고 PR 게시 시 역할·모델별로 합산한다. Planner의 여러 G에 공유된 사용량은 계획 PR에 귀속하고 각 G에 임의 분할하지 않는다. Provider별 입력·캐시 토큰의 의미를 유지하며 모르는 값은 0으로 채우지 않는다. PR 본문에 필요한 집계·재시도 근거를 보존하다가 완료 후 호출별 원본을 정리한다.

`validation_receipts`는 실제 성공 근거를 입력 지문에 연결한다. 후보·검증 설정 지문·성공 여부·간결한 판단 근거만 **SQLite 행에 저장**하고 테스트 출력 전체·Agent 대화·상세 로그 파일을 완료 증거로 보관하지 않는다. R의 최종 Acceptance Criteria 확인에 필요한 소형 근거는 확인이 끝날 때까지만 보존한다. 새로운 실행 주기가 이전 검증 결과를 자동 무효화하는 것은 아니다. **현재 후보에 같은 입력이 유지되고 성공 근거를 확인할 수 있을 때만 재사용**한다. 실행 중이거나 결과 확인이 안 된 시도는 PASS가 아니다.

`managed_prs`는 PR 식별과 원격 관측만 담고, `external_jobs`는 외부 효과의 성공 확인 또는 DB 적용이 아직 끝나지 않았을 때만 사용한다. 확인된 게시 정보는 PR 관측으로 이어지고 완료 job의 세부 기록은 정리한다. 모든 Git 조회·명령을 journal에 기록하지 않는다.

`external_jobs`는 서로 다른 외부 효과를 임의의 JSON 상태 기계로 합치는 수단이 아니다. 작업 종류·식별 대상·진행 단계·외부 성공 확인 방법을 구분한 작은 공통 기록이다. 수행 전에 의도를 기록하고, 재시작 후에는 원격을 먼저 확인한다. Archive·Worktree·불필요한 실행 데이터 정리는 실행·완료를 차단하지 않는 독립 후속 작업으로 실행한다. 정리 실패를 G의 BLOCKED로 승격하지 않는다.

### 기존 DB 이행

새 스키마를 기존 파일 위에 무조건 덮어쓰거나 `CREATE TABLE IF NOT EXISTS`만으로 완료했다고 판단하지 않는다.

1. 기존 DB 스키마·버전을 판별하고, 실행 중인 구버전 Writer가 없는 안전한 전환 지점에서 임시 백업한다. 전환·복구·대조가 확인되면 자동 실행의 최종 근거로 백업 파일을 남기지 않고 정리한다. 사용자 요청으로 별도 보관한 DB 백업은 작업 결과의 최소 근거로 취급하지 않는다.
2. 프로젝트 등록·설정과 활성 G 정의를 이관한다. `READY` 외의 기존 상태 명칭은 새 단계로 명시적으로 대응시키고, 대응이 불확실하면 성공·MERGED로 승격하지 않는다.
3. 진행 중 G의 브랜치·PR·Checkpoint·외부 효과를 실제 GitHub·원격 Git과 대조한다. GitHub에서 확인되지 않는 과거 성공·소유권 표식을 새 상태로 복사하지 않는다.
4. 검증 Receipt는 실제 입력·후보·PASS 근거가 확인될 때만 이관한다. 불완전한 과거 기록은 재검증 대상으로 둔다.
5. 사용량은 사용 가능한 원본에서 호출 식별자별로 한 번만 이관한다. 이전 집계가 불완전하면 누락으로 표시한다.
6. 알림 Outbox와 취소·완료·Archive·보류 상태를 보존한다. 이미 전송한 사건과 이미 생성한 PR을 재발행하지 않는다. 오래된 로그·세션·사용량·검증 기록은 완료·PR/Archive 근거를 안전하게 이관한 뒤에만 정리한다.
7. 새 스키마에서 주요 ID 수·현재 PR 연결·완료·미완료·취소·알림을 대조한 뒤 전환을 확정한다.

이행은 **재실행 가능하고 원자적**이어야 한다. 실패하면 기존 DB를 보존한 채 이전 상태로 계속 접근하거나 안전하게 재시도한다. 새 환경에 DB가 없는 경우에는 원격 문서·브랜치·PR에서 재구성하고 확인되지 않는 과거 세션·검증·토큰 수치는 추정하지 않는다.

## 4. Planning과 작업 반영

Planner 입력은 최신 유효 Requirement, 실제 코드·테스트, 활성·완료 G, 의존 관계와 열린 계획 PR로 구성한다. 이미 충족된 목표와 기존 G의 중복을 먼저 배제하고 현재 파악 가능한 전체 작업을 제안한다.

Planner는 **Requirement R-ID를 계획 PR의 묶음 기준**으로 사용한다. 한 R에서 도출한 신규·수정 G를 `loop-manager/plans/{short-summary}`의 한 계획 PR에 게시하고, 해당 R의 열린 계획 PR이 있으면 같은 PR에서 G를 추가·수정·제거한다. 다른 R과 공유하는 구현은 이미 존재하는 G를 연결하고 중복 생성하지 않는다. PR 관측은 source R-ID로 연결하며 G가 한 개도 남지 않아도 해당 R의 PR identity를 유지한다. 사용자의 PR 편집은 보존한다.

**같은 R의 계획에 `draft: true`인 G가 하나라도 있으면 GitHub Draft PR로 게시**한다. 일반 G와 draft G를 별도 계획 PR로 분리하지 않으며, 질문이 해소될 때 같은 PR에서 관련 G를 다시 평가한다. 모든 draft가 해소되고 실제 계획 후보가 있으면 같은 PR을 Ready로 전환한다. 사용자가 Merge 없이 닫은 계획 PR은 입력이 동일한 동안 자동 재생성하지 않는다.

사용자는 base·계획 PR·구현 브랜치에서 문서를 수정할 수 있다. base의 G는 실행 기준, 계획 PR의 G는 미확정 후보, 구현 브랜치의 편집은 해당 G의 작업 입력이다. 다른 G의 확정 목표는 base 변경 없이 자동 교체하지 않는다.

G 정의·Requirement 변경은 영향받은 현재 실행에 반영한다. 이미 열린 구현 PR에서 추가 작업이 필요하면 Draft 전환을 확인한 뒤 Worker·Verify·Reviewer를 다시 수행한다. 완료된 G는 수정으로 소급 미완료 처리하지 않는다.

제품 결정이 불명확한 계획은 **`draft: true`와 `Draft Decision`의 Question·Facts·Options·Impact**로 기록하고 그 G를 실행하지 않는다. 동일 계획 PR의 다른 미확정 G도 Merge 전에는 실행하지 않으며, base에 확정된 독립 G는 계속 진행한다. 사용자 답변 또는 문서 수정으로 미결정 사항이 해소되면 기존 G를 갱신한다. ExecPlan의 draft 선언과 GitHub Draft PR은 별개 정보지만, 계획 PR의 Draft 여부는 포함된 미해결 draft G로 결정한다. 계획 PR 등록 알림에는 draft G-ID·제목·질문을 포함한다.

### 불필요해진 계획 PR과 Requirement 완료

Planner가 최신 R 전체 Acceptance Criteria, 실제 코드·테스트와 이미 확정된 G를 대조하여 **해당 R의 모든 제안 ExecPlan이 불필요하고 남은 목표도 충족됐다고 확정**하면, 기존 미Merge 계획 PR을 Close하고 해당 R의 Archive를 후속 게시한다. 아직 관련 미완료 G가 있는지 확인하며, G가 0개라는 사실만으로 무조건 완료를 추정하지 않는다.

PR Close 이후 Archive 게시 전에 중단되면 원격 PR의 Closed 상태·R의 최신 입력·완료 판단을 확인하고 **Archive만 재시도**한다. 동일 입력으로 계획 PR을 다시 만들지 않는다. PR Close와 Archive는 외부 효과 기록에 결합하되, 이미 완료한 효과를 재실행하지 않는다. 판단 불충분·미충족·미해결 draft라면 계획 PR을 자동 Close하거나 R을 Archive하지 않고 해당 판단·계획 보완을 이어간다.

### Planner 실행 조건

Planner는 최초 계획, 활성 Requirement의 추가·수정·삭제, draft 질문 해소, 관련 G가 모두 소진된 Requirement의 최종 조건 확인에 필요한 경우만 실행한다. **PRODUCT·ARCHITECTURE·ExecPlan만의 변경과 코드·base SHA 변경은 독립적인 Planner 실행 사유가 아니다.** 완료 확인은 해당 R의 관련 작업을 소진한 시점에 수행하고 유효한 근거가 있으면 반복하지 않는다.

### 수동 개발·제출

`manual start`와 `manual complete`는 **다른 CLI 명령과 동일하게 `control_requests`에 접수**한다. CLI는 원격 Git 작업이나 메인 DB 변경을 직접 수행하지 않고 요청 ID와 접수 상태를 반환한다.

- `manual start <repo> <G-ID>`: runtime이 base의 기존 G와 dependency·현재 실행 상태를 확인한 뒤 수동 작업 브랜치를 준비한다. **브랜치 준비가 실제 완료된 후에만** CLI 조회 결과에서 준비 완료로 표시하며, 해당 G의 자동 Worker 중복 선정을 막는다.
- `manual complete <repo> <G-ID>`: runtime이 사용자 원격 브랜치의 제출 후보 HEAD를 확인해 Verify → Reviewer → 동일 G 구현 PR로 인수한다. 실패하면 같은 G에서 교정한다.
- **수동 제출 하나는 확정된 G 하나에만 연결한다.** G 없는 `manual submit`, Requirement·코드 묶음 제출, 여러 G 공유 수동 그룹은 지원하지 않는다.
- 같은 G의 start·complete 요청은 접수 순서와 실제 G 상태를 고려해 처리한다. start가 아직 적용되지 않았거나 브랜치가 준비되지 않았다면 complete를 성공으로 처리하지 않고 후속 검증을 기다린다.
- 요청 접수와 자원 배정을 분리하고, 동일 G·브랜치의 중복·상충 요청을 검증한다. 수동 후보 HEAD가 바뀌면 기존 Verify·Review 근거를 재검사한다.
- 과거 `--skip-verify`·`--skip-review` 옵션은 제외한다. 구현 PR Merge 전에는 수동 G도 완료가 아니다.

## 5. Scheduling과 실행 전이

Coordinator는 명세·계획 갱신, 기존 진행·교정·복구, 신규 G, PR 게시, 완료 보관을 **선택 우선순위**로 처리한다. **수동 `manual start`·`manual complete`를 포함한 모든 CLI 명령은 `control.db`에 먼저 접수**한다. 변경·수동 작업·초기화는 Coordinator가 실제 상태를 검증하고 적용한다. `run`은 제어 DB를 통해 실행을 요청하고 필요하면 별도 경량 기동기가 runtime을 시작한다. 읽기 전용 `list`·`status`·`usage`·`logs`도 제어 DB에서 조회 요청과 결과를 관리하며 Agent·Verify·Git 게시를 유발하지 않는다. 전역 단계 장벽을 두지 않으며 독립 작업은 겹쳐 수행한다.

Worker·Reviewer는 전역 공통 AI 한도, Verify는 독립된 전역 1개 한도를 사용한다. 한 프로젝트의 Worker는 기본적으로 1개이며 그 Worker가 Verify에 들어가면 다른 독립 G의 Worker를 시작할 수 있다. 자원을 실제 사용하지 않는 PR Merge 대기·사용자 판단·재시도는 슬롯을 점유하지 않는다.

ExecPlan 실행 단계는 `exec_plans.status`의 `READY`, `WORKING`, `VERIFYING`, `REVIEWING`, `PR_OPEN`, `BLOCKED`, `CANCELLED`, `MERGED`로만 관리한다. 재시도·순번 대기는 `execplan_runs.retry_after`와 대기 사유로 표시한다. GitHub의 PR Open·Draft·Ready·Merged는 별도 관측 상태다. 재작업 중 `PR_OPEN`이었던 G가 다시 `WORKING`에 들어가도 동일 PR을 계속 연결한다.

모든 Agent·Verify·게시 후보는 `(repo, plan_id, generation, input_fingerprint)`에 결합한다. 새 주기나 취소 이후 늦게 도착한 결과는 CAS 방식의 현재 주기 검사를 통과하지 못하면 상태를 변경하지 않는다. **과거 Worktree 소유권·전체 Git snapshot의 증명 실패는 단독 BLOCKED 사유로 사용하지 않는다. 원격·파일 경로·일시 작업 환경 오류는 해당 쓰기만 대기·재시도하며 Git 차단으로 승격하지 않는다.**

선행 G는 실제 구현 PR Merge 후에만 완료 의존성을 충족한다. 취소·삭제로 의존성이 사라지면 필요한 목표를 건너뛰지 않고 영향받은 G의 계획을 다시 판단한다. 독립 작업은 계속 진행한다.

### 제어 명령 접수와 runtime 적용

SQLite의 쓰기 트랜잭션은 DB 파일별로 직렬화된다. 기존 메인 DB의 접근 조율 잠금이 CLI까지 지연시키지 않도록, **모든 CLI 명령은 `control.db`의 짧은 트랜잭션으로 진입**한다. CLI는 **메인 DB를 읽기 전용으로 조회할 수 있으나 쓰기는 하지 않으며**, Git·Agent 실행도 runtime에 맡긴다. 입력의 즉시 검증 결과와 실제 명령 적용 성공은 구분한다.

1. **접수·즉시 검증**: CLI는 문법·인자·대상을 확인하고 `request_id`와 접수 순서를 제어 DB에 저장한다. 빠른 입력 검증이 필요하면 **전용 읽기 전용 SQLite 연결**로 메인 DB의 현재 차단·G·dependency 상태를 직접 확인할 수 있다. 이는 runtime의 실행 순번 잠금·메인 DB 쓰기 조율을 획득하지 않고, 짧은 조회 제한 시간 뒤 연결을 닫는다. 읽기가 잠금·마이그레이션 때문에 실패하면 접수를 막지 않고 **검증 미완료**로 남긴다. 확인한 값도 실행 시점에 runtime이 다시 검증하며 접수만으로 `APPLIED`로 표시하지 않는다.
2. **조회·응답**: `list`·`status`·`logs`·`usage` 등 읽기 전용 CLI도 요청 ID로 관리하되 **CLI가 메인 DB를 읽을 수 있으면 즉시 조회하고 `APPLIED` 결과·확인 시각을 제어 DB에 기록한다.** 조회는 대기열의 다른 변경 명령을 기다리지 않는다. 메인 DB가 사용 중이거나 원격 최신 확인이 필요한 경우에는 검증 미완료·대기 조회를 표시하고 runtime이 후속 조회한다. 긴 로그 전문은 큐에 장기 적재하지 않는다.
3. **runtime 기동**: `run`도 먼저 제어 요청으로 접수한다. 활성 runtime이 없으면 짧은 Bootstrap이 동일 제어 DB를 확인하고 정상 실행 임대를 얻어 runtime을 시작할 수 있다. `init`도 제어 요청이며 실제 메인 DB 초기화는 runtime이 수행한다. **오직 제어 DB를 최초 생성하는 Bootstrap만 접수 이전 동작**으로 허용한다. 별도 상시 daemon은 만들지 않는다.
4. **적용 시점**: runtime은 시작·재시작과 현재 작업이 안전한 단계 경계를 통과해 새 작업을 선택하기 전에 미처리 요청을 읽는다. Worker·Verify를 명령 때문에 임의 종료하지 않는다. runtime이 없으면 요청은 다음 예약 실행이나 명시적 `run`이 기동한 runtime에서 처리한다.
5. **최종 검증**: runtime은 명령의 실제 대상과 최신 상태를 확인한다. `unblock`은 현재 차단 사유를 검사한 뒤 해제하고 기존 G를 재개 우선순위에 둔다. `manual start`·`manual complete`는 확정된 G와 원격 후보·선행 요청 순서를 검사한다. 요청 접수만으로 실행 성공이나 작업 완료를 주장하지 않는다.
6. **중복·중단 복구**: 같은 대상·입력의 미완료 요청은 요청 ID를 재사용하거나 연결하고, 같은 G에 상충하는 명령은 접수 순서대로 유효성을 판단한다. runtime은 요청을 짧은 트랜잭션으로 인수해 `PROCESSING`으로 바꾸고 실제 작업은 제어 DB 트랜잭션 밖에서 수행한다. 중단 후 `PROCESSING`이 남으면 메인 DB·원격 효과를 확인하고 이미 적용한 부분을 재수행하지 않는다. 두 DB를 가로지르는 단일 원자적 commit을 가정하지 않는다.
7. **완료·격리**: 요청이 실제 적용되면 `APPLIED`, 실행 불가로 확정되면 `REJECTED`와 근거를 기록한다. 재시도 가능한 오류는 `PENDING`에 사유·재시도 시각을 남긴다. 하나의 명령 대기로 독립 프로젝트·G·조회 명령을 가로막지 않는다. CLI는 요청 ID로 완료·거부 상태를 확인하며, 오래된 결과는 정리한다.

`pause`/`resume`은 다른 CLI와 동일한 제어 진입점에서 접수하면서 `pause_scopes`의 최신 의도를 즉시 갱신한다. 새 Agent·Verify·게시 단계 진입 전에 runtime이 이를 확인한다. **pause 적용 이후 추가 실행을 시작하지 않는 효과는 빠르게 발생**하지만 이미 시작한 작업은 안전한 경계에서 반환·보존한다. 관리·조회 요청은 pause 중에도 처리할 수 있으며 resume은 다른 BLOCKED를 자동 해제하지 않는다. 상충하는 pause·resume 요청은 저장된 순서·최신 의도로 해석해 오래된 요청이 제어 상태를 다시 뒤집지 않도록 한다.

## 6. Agent 실행과 품질 지침

각 호출에는 Loop Manager runtime의 공통·역할별 지침과 프로젝트 고유 지침을 구분해 적용한다. **프로젝트 루트 `AGENTS.md`의 읽기·해석은 Agent CLI에 맡기고** Manager는 그 파일을 별도 등록·승인·입력 해시 검증 대상으로 삼지 않는다.

| 역할 | runtime 지침 |
|---|---|
| Planner | `docs/AGENTS.common.md` + `docs/AGENTS.planner.md` |
| Worker | 공통 + `docs/AGENTS.worker.md` + `docs/AGENTS.ponytail.md` |
| Reviewer | 공통 + `docs/AGENTS.reviewer.md` |

Ponytail은 Worker에 매번 정확히 한 번 전달하며 외부 Plugin·Skill 설치를 요구하지 않는다. 기존 코드·표준 도구 재사용, 작은 변경, 근본 원인 해결을 권장하지만 필수 검증·안전 조건을 줄이는 근거는 아니다. `i-have-adhd`의 결과 우선·단계 구분·Progressive Disclosure는 Specification Skill·Planner·PLANS의 문서 작성 기준으로 유지하고, 별도 설치·호출을 요구하지 않는다.

지침은 현재 선택 runtime 원본에서 불러오고 Provider의 기존 system/developer 지침과 프로젝트 규칙을 보존한다. 적용한 지침의 내용 해시를 Agent 결과와 연결한다. 같은 Worker 세션에는 새로운 변경·미해결 실패·완료 목표 위주로 전달한다. Codex·Claude는 사용자 개인 설정 전체를 복제하지 않는 자동 실행 전용 Provider별 홈을 사용하고 필요한 인증만 안전하게 재사용한다. 공통 Provider 홈에서도 호출별 세션·임시 파일·역할별 실행 설정·출력을 분리한다. 세션이 유실되면 최신 원격 작업물과 짧은 현재 문맥으로 새 세션을 시작한다.

Worker는 구현 완료 후보·Checkpoint·사용자 결정 요청·복구 불가 충돌을 구조화해 반환한다. Reviewer는 PASS 또는 구체적 교정 근거를 반환한다. **Manager만 실제 실행 단계와 PR 상태를 확정**한다. Provider 오류는 일시적 실패·사용량 한도·인증/설정 실패·세션 소실·출력 형식 오류를 구분하며 중복 재시도 계층을 만들지 않는다.

Worker는 관련 테스트를 실행하고 필요하면 중간 Checkpoint를 요청한다. Manager가 변경을 확인해 Commit·Push하고 작업물을 보존한다. Checkpoint는 최종 Verify·Review 성공이 아니다.

## 7. Verify와 Reviewer

기본 흐름은 **Worker 관련 테스트 → 프로젝트 필수 Verify → Reviewer**다. 별도 영향 기반 부분 Verify·선행 Review·공통 테스트 분산 Runner는 기본 경로에 두지 않는다.

Verify는 프로젝트에서 지정한 명령·실행 환경·작업 디렉터리·시간 제한을 사용한다. 언어별 Runner를 강제하지 않으며 테스트 내부 병렬화는 프로젝트의 기존 도구가 담당한다.

- 실제 Verify 시작 전 자원 대기와 실행 시간을 구분한다.
- 종료 코드·로그·시간 초과·취소 및 하위 프로세스 종료 여부를 확인한다.
- 성공 근거는 해당 코드 후보·명세·검증 명령과 입력 지문에 결합한다.
- 실제 입력이 변하지 않았고 유효한 성공 근거가 있으면 재실행하지 않는다. **검증 명령·설정·테스트·의존성 등 필수 입력을 안전하게 열거·비교할 수 없으면 성공을 재사용하지 않는다.** 근거가 없거나 무효화되면 필요한 단계를 다시 실행한다.
- 타임아웃·중단·종료 불명은 PASS가 아니다. Reviewer는 전체 Verify를 반복하지 않고 전달된 근거를 검토한다.
- Reviewer 결과도 diff·명세·검증 근거·지침 해시에 결합한다.

## 8. Git과 PR

Planner·G별로 격리된 작업 디렉터리를 사용하며 사용자 기본 checkout은 수정하지 않는다. 실행에 필요한 경로·대상 저장소·브랜치와 동시 쓰기 충돌만 확인한다. 과거 Git 소유권·미세한 파일 상태 이력을 복잡하게 재증명하는 전제는 두지 않는다. **ignored-only 파일·테스트 캐시·빌드 부산물의 생성·변경은 Agent 권한 위반이나 BLOCKED로 처리하지 않는다.** Git이 실제로 덮어쓸 경로와 충돌하면 파일을 보존해 해당 쓰기만 보류하고 가능하면 새 격리 공간에서 계속한다. 이전 Worktree를 안전하게 재사용할 수 없고 원격 작업물이 유효하면 새 공간에서 이어간다.

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

재시작 시 메인 SQLite의 현재 실행·외부 효과와 실제 Git·GitHub 상태를 대조하고 마지막 확정된 단계 이후부터 재개한다. 독립 `control.db`의 미처리 CLI 요청과 중단된 처리 상태도 확인하고, 메인 DB 적용 여부를 재검증한 뒤 안전하게 처리한다. Runtime 인계용 임시 영수증 파일은 미확정 인계 중에만 사용하며 인계 확인·소비 후 삭제한다. 이는 최종 보존 결과가 아니다. 일시적인 API·자원 오류는 자동 재시도하고, 해결할 수 없는 정책 충돌이나 필수 사용자 입력 부족 등만 관련 G를 차단한다. 이전 Agent 결과가 늦게 도착하면 `generation`과 입력을 확인한 뒤 적용한다.

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

모든 프로젝트의 Telegram은 기존 `notification_outbox`를 활용하는 공통 큐에서 처리한다. 작업·PR·현재 후보·차단 원인으로 사건 키를 만들어 중복 전송을 억제한다. 계획/구현 PR 등록, 구현 PR Ready 재검토, 충돌 복구, 최종 차단과 확인된 장기 무진전은 일반 알림이고, 신규 G 시작·차단된 G 실제 재개는 무음이다. 계획 PR이 Draft라면 등록 사건에 draft G-ID·제목·질문을 포함하고 별도 draft 알림을 생성하지 않는다. Draft 해제와 충돌 해소가 같은 후보의 사건이면 하나의 알림으로 합친다. 전송 실패는 개발 작업을 막지 않는다. 해결·취소돼 더 이상 유효하지 않은 미전송 payload는 취소하며 발송 뒤 불필요한 본문 데이터는 정리한다.

Agent에는 역할·작업 공간에 필요한 최소 권한과 프로젝트 설정만 전달한다. 사용자 checkout·다른 G의 작업물·공개 커밋을 덮어쓰지 않고, 비밀값·환경정보가 포함된 로그를 PR·Telegram에 노출하지 않는다. 실제 대상 게시·삭제 시점에만 필요한 보호 검사를 수행한다. ignored-only 변경이나 다른 Worktree의 일반 변경을 Agent의 권한 위반으로 추정하지 않는다.

## 14. 구현 이행과 검증

이번 문서는 **목표 아키텍처**다. 기존 SQLite, 커밋·원격 작업 브랜치·PR·전송 알림을 초기화하지 않는다.

재사용 출발점은 `loop_manager_internal/storage/database.py`, `execplan_catalog_state.py`, `planning_input_state.py`, `requirement_judgment_state.py`, `notifications/repository.py`, `agents/common_guidance.py`, `agents/agent_provider_usage.py`, `execution/`, `git/` 및 `verification/`이다. 재사용한다는 뜻은 현재 파일·클래스·테이블 분할을 그대로 유지한다는 뜻이 아니다.

**Git 테스트는 최소화한다.** 정책 조합·SQLite·실패·재개의 대부분은 Mock과 작은 단위 테스트로 검증하고, 실제 Git·CLI 통합 테스트는 Merge 충돌·사용자 변경 보존·게시 후 중단 복구·ignored-only 비차단처럼 실제 효과를 확인해야 하는 소수 대표 경로에 집중한다. 같은 결과를 검증하는 반복적인 실제 Git 실행·대규모 조합을 추가하지 않는다.

실행 검증은 정상 작업뿐 아니라 다음 경계를 포함한다.

- 최신 문서 변경·충돌하는 R-ID 우선순위, 동일 입력 계획 중복 방지
- 구버전 DB 데이터 이행·완료/취소 후 불필요한 데이터 정리·중단 재시도·기존 PR/알림 중복 방지
- G별 수동 start/complete의 실제 검증과 자체 runtime 갱신의 안전한 인계·pause 초기 판정
- Requirement별 계획 PR에 G 묶기·Draft 전환·모든 G 불필요 판정 후 PR Close와 R Archive의 중단 복구
- Manager가 긴 작업 중에도 `manual start`·`manual complete`, 차단 해제·복구·등록/해제·조회 명령을 공통 제어 DB로 접수하는지
- 초기화·run의 제어 DB 진입, 실행 중/부재 runtime 기동, 별도 SQLite 읽기 전용 연결의 즉시 검증·조회·잠금 실패 시 큐 전환과 결과 확인 시각
- 같은 G에 연속된 수동 명령·상충하는 pause/resume·동일 요청 중복·두 DB 사이 중단 후 재시도
- 독립 제어 DB의 pause/resume 접수, 기존 pause.db 값 이관, 새 환경 control.db 부재 초기화와 실제 상태 구분
- 계획 Draft PR 등록·알림 및 ignored-only 파일의 비차단 처리
- Worker·Verify·Reviewer의 겹친 실행, 오래된 호출 결과의 적용 거부
- GitHub 충돌 확인 후 Merge 교정과 비충돌 시 불필요한 복구 배제
- 구현 PR Draft → Ready 전환과 현재 후보의 검증 근거
- 이전 Worktree를 사용 못할 때 원격 브랜치로 복구, 작업물 보존
- MERGED 후 G Archive와 R 완료 재확인·후속 계획·Archive
- 완료 후 Worktree 정리 실패가 다른 작업을 막지 않음
- Agent 지침·Ponytail·세션·Checkpoint·실제 모델 사용량 PR 집계
- Telegram 재전송·중복 억제, DB 없는 새 환경의 원격 복구
- 서로 다른 언어·프레임워크에서 프로젝트 정의 Verify 명령 실행

문서만 변경된 상태에서는 이 결과를 실제 구현 테스트 성공으로 표현하지 않는다.
