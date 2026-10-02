---
id: G-060
title: Project Root Placement Policy and Inventory
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-051
  - G-052
  - G-053
  - G-054
  - G-055
  - G-056
  - G-057
  - G-058
  - G-059
  - G-061
  - G-062
  - G-064
  - G-065
  - G-067
  - G-069
  - G-071
  - G-072
  - G-075
  - G-077
  - G-080
  - G-081
  - G-087
  - G-092
  - G-095
  - G-101
  - G-107
complexity: medium
risk: medium
priority: critical
---

## Purpose

현재 루트에는 필수 launcher·문서 외에 `agent_invocation.py`, `execution_handlers.py`, `maintenance_state.py` 등 내부 모듈이 직접 놓여 있다. `loop_manager.py`, `run.sh`·`run.ps1`과 테스트 import가 이 배치에 결합돼 있으므로 단순 파일 이동은 실행을 깨뜨린다. 실제 파일 이동에 앞서 모든 루트 항목의 배치 근거와 공통 품질 기준을 고정한다.

## Acceptance Criteria

- 루트의 모든 현재 항목을 사용자 진입, 실행·빌드·배포, 도구 필수 탐색 또는 내부 구현으로 분류하고 근거와 참조를 기록한다. `.agents/`, `docs/`, `scripts/`와 고정 파일, launcher·manifest·필수 설정은 지정 경로를 유지한다.
- 내부 모듈의 import graph, 테스트 참조, 실행·도구 탐색 근거와 안전한 하위 패키지 목적지를 기록한다. 이동할 수 없는 항목에는 구체적인 이유를 남긴다.
- 이후 신규 루트 파일은 구체적 루트 필요 근거를 확인한다. 이 계획은 기존 파일의 path, CLI, 사용자 동작, 공개 계약, SQLite 저장 형식과 verify 경로를 변경하지 않는다.
- 등록된 모든 프로젝트의 Planner·Worker·Reviewer 공통 지침에도 필수 루트 경로 보존과 파일별 이동 근거 확인을 적용하되 기존 프로젝트의 실제 파일은 이 계획에서 이동하지 않는다.
- 기존·신규 등록 프로젝트의 첫 정상 파일별 구조 평가에서 루트 항목의 실제 참조, 사용자 이용 경로, 소유권과 안전한 목적지를 조사한다. 정책 적용 전에 이미 구조 평가를 마친 프로젝트는 확인 가능한 결과를 다음 정상 평가에서 재사용한다. R-042의 첫 평가 시점보다 앞서 등록·배포만을 이유로 별도 전수 스캔을 시작하지 않으며, 근거가 확인된 이동만 R-027의 동작 보존 계획으로 연결한다.
- 사용자 요청에 따른 Loop Manager 자체 리팩터링의 기준선에는 큰 파일의 책임·호출 관계·중복·테스트 fixture 결합과 분리 목적지를 포함한다. 모든 추적 Python·실행·검증 파일을 확인하고 발견을 G-063 및 G-119~G-127의 담당 범위 또는 분리 불필요 근거와 연결한다. 줄 수는 조사 우선순위에만 사용하며 파일 분할 자체를 성공 조건으로 삼지 않는다.
- `full_assessment.py`의 revision-bound inventory와 기존 평가 결과를 재사용한다. 기존 결과의 revision·hash가 현재 원본과 다르면 변경된 관계를 다시 확인하며 과거 평가를 현재 검토의 증거로 사용하지 않는다. inventory만 산출한 것을 전수 검토 완료나 SQLite 평가 성공으로 기록하지 않는다.
- inventory·발견·기존 함수/테스트에서 새 책임 경계로의 대응표는 이 ExecPlan의 Implementation·Progress에 기록한다. 별도 임시 분석 모듈·보고서나 새로운 제품 Requirement를 만들지 않는다. 최종 G-127은 이 목록의 미처리 범위와 임시 코드 정리를 확인한다.

## Implementation

- G-059의 Loop Manager 자체 전수 inventory를 최신 루트 파일 집합과 대조해 파일별 참조, 소유권과 이동 목적지를 기록한다. G-051, G-064~G-072, G-077, G-080, G-081, G-083~G-095, G-099~G-101과 G-104~G-110에서 추가·변경된 루트 모듈이 있으면 import·검증 참조에 포함하고, `ROOT` 기반 resource lookup과 launcher가 요구하는 고정 파일을 식별한다.
- 필수 폴더·파일과 신규 루트 항목 판정 기준을 공통 provider-neutral prompt에 반영한다. 실제 내부 모듈 이동은 G-063에서 수행한다.
- 기존 등록 프로젝트에도 적용되는 provider-neutral prompt에 필수 루트 경로와 안전한 파일 배치 기준을 반영한다.
- G-086의 정상 파일별 평가가 처음 도래할 때 유지보수 Planner의 평가 입력과 결과에 루트 항목 inventory를 포함한다. G-083의 추적 source 목록만으로 문서·고정 경로 등 루트 항목을 누락하지 않으며, 별도 조기 평가나 중복 전체 파일 읽기를 만들지 않는다. 이미 평가된 프로젝트는 다음 해당 평가에서 확인된 기존 결과를 재사용한다. 소유권·참조를 증명하지 못한 항목은 이동 계획에서 제외하고 판단 근거를 기록한다.
- 자체 저장소의 기준선 조사에는 `full_assessment.source_inventory(repository, exact_revision)`를 사용하고 launcher·config·manifest 등 inventory 밖의 루트 필수 항목을 별도로 대조한다. 이것은 사용자가 요청한 계획의 근거 수집이며 다른 등록 프로젝트의 평가 일정이나 운영 cycle을 앞당기지 않는다.
- 아래 초안 조사 결과를 선행 계획 Merge 후 최신 원격 revision에서 갱신한다. import graph뿐 아니라 SQLite transaction 소유자, Git mutation·원격 lease, session binding, checkpoint·validation token과 fixture patch 수명을 기록해 분리 후에도 유지할 경계를 고정한다.

| 현재 파일/범위 | 2026-10-02 기준선과 확인한 결합 | 담당 계획 |
| --- | --- | --- |
| `loop_manager.py` | 25,132줄, 최상위 정의 518개. schema·bootstrap·provider 호출·scope·복구·planning ports·scheduler가 한 파일에 있고 `_run_project_step`은 671줄이다. | G-120~G-123 |
| `tests/test_loop_manager.py` | 19,483줄, TestCase 22개. module-wide profile/session patch와 Git·catalog fixture가 다른 test module에 import된다. | G-119 |
| `execution_handlers.py`, `tests/test_execution_handlers.py` | 각각 4,902줄, 7,521줄. 하나의 handler가 recover·guard·run·verify·review·publish와 문서·rebase 교정을 함께 담당한다. | G-124 |
| `planning_runtime.py`, planning·maintenance tests | coordinator 2,168줄에 입력 선택·workspace·파일 평가·검증·게시·activation이 결합돼 있고 `_publish_planner_result`는 460줄이다. | G-125 |
| `tests/test_scoped_blocking.py`, `tests/test_blocked_checkpoint.py` | 각각 4,275줄, 2,732줄. plan/project 차단, 원격 checkpoint 및 recovery adapter 시나리오를 책임별로 분리한다. | G-122 |
| `tests/test_origin_isolation.py`, `tests/test_agent_invocation.py` | 각각 2,649줄, 1,517줄. startup·source sync·provider 격리 테스트와 공통 fixture의 소비 관계를 확인한다. | G-120, G-121 |
| `notifications.py`, `tests/test_notifications.py` | 각각 1,552줄, 2,379줄. SQLite incident/outbox, event 생성, rendering·분할과 Telegram 전송이 결합돼 있다. | G-126 |
| `runtime_state.py`, `execplan_catalog_state.py`, `manager_git_journal.py` 등 나머지 추적 파일 | 저장소·증거·외부 도구 경계별 책임과 반복 코드를 전수 대조한다. 응집도가 있는 큰 파일은 이유를 남기고, 관련 중복은 해당 저장/외부 도구 계획에서 처리한다. | G-120, G-122, G-127 |
| `full_assessment.py` 및 이를 소비하는 runtime·test·verify 참조 | 단순 분석 스크립트가 아니라 일회성 평가의 inventory·prompt·결과 검증에 사용된다. 기존 pending cycle과 감사 자료를 보존할 대체 경로가 먼저 필요하다. | G-125, G-127 |

### 2026-10-02 최신 기준 root inventory

`origin/main`과 일치하는 `d523989e65103ca187815b54808abf09e7ac867f`에서
`full_assessment.source_inventory(repository, exact_revision)`는 추적 Python·실행·검증·활성 제품 문서 147개를 산출했다. 이 inventory는 hash-bound 분석 대상이며 루트 config·manifest·문서 폴더 자체를 모두 포함하지 않으므로, 같은 revision의 `git ls-tree` 루트 항목과 별도로 대조했다. inventory를 산출한 것만으로 전체 평가 또는 SQLite 평가 성공으로 기록하지 않는다.

| 루트 항목 | 분류와 확인 근거 | 이동 판정 / 소유자 |
| --- | --- | --- |
| `.agents/`, `docs/`, `scripts/` | 도구 필수 탐색. 등록은 `.agents`의 지침과 `docs/PLANS.md`·`docs/ARCHITECTURE.md`를 확인하고, `scripts/verify.sh`·`scripts/verify.ps1`은 Manager가 현재 OS에서 직접 찾는다. | 고정 경로이며 후보가 아니다. |
| `AGENTS.md` | 도구 필수 탐색. 등록 시 기준 지침 포함과 승인 hash를 확인한다. | 고정 파일이며 후보가 아니다. |
| `tests/` | 실행·검증. 두 verify script가 `unittest discover -s tests`를 실행하고 root module을 직접 import하는 unit test가 있다. | root test package를 유지한다. G-119가 fixture와 test 책임만 분리한다. |
| `README.md` | 사용자 진입. Python으로 `loop_manager.py`를 실행하는 설치·운영 명령을 제공한다. | root 문서로 유지한다. |
| `run.sh`, `run.ps1`, `loop_manager.py` | 사용자 실행. 두 launcher가 같은 directory의 `.venv` interpreter와 `loop_manager.py`를 직접 지정하고, bootstrap runtime child도 `runtime.path / "loop_manager.py"`를 실행한다. | root launcher/CLI 계약으로 유지한다. G-123은 CLI 구성만 줄이고 경로와 공개 호출은 보존한다. |
| `config/`, `requirements.txt`, `.gitignore` | 실행·빌드·배포 또는 도구 설정. Architecture가 source checkout 기준 `config/config.ini`을 runtime child에 전달하고, `requirements.txt`는 Python 의존성 manifest이며 `.gitignore`는 runtime cache·비밀 config의 추적 경계를 정한다. | 현재 위치를 유지한다. 이동 근거가 없다. |
| `agent_codex_sandbox.py`, `agent_invocation.py`, `agent_invocation_state.py`, `agent_provider_usage.py`, `agent_sandbox_exec.py`, `agent_scope_decision.py`, `agent_write_isolation.py` | 내부 구현. `loop_manager.py`·Agent invocation/state와 직접 unit test가 import하며 sandbox, session, provider usage와 write isolation을 구현한다. | G-063에서 `loop_manager_internal/agents/`로 이동 후보. G-121이 invocation·isolation 책임을 분리한다. |
| `blocked_checkpoint.py`, `blocked_checkpoint_state.py`, `blocked_plan_reconciliation.py`, `blocked_remote_observation.py`, `checkpoint_ownership.py`, `project_blocked_recovery.py`, `scoped_blocking.py` | 내부 구현. checkpoint·block recovery와 SQLite state의 직접 import 및 `test_blocked_*`/`test_scoped_blocking.py`가 결합돼 있다. | G-063에서 `loop_manager_internal/recovery/` 또는 `storage/`로 이동 후보. G-122가 checkpoint·recovery adapter를 분리한다. |
| `execution_checkpoint.py`, `execution_checkpoint_state.py`, `execution_handlers.py`, `execution_revision.py`, `execution_runtime.py`, `execution_state.py`, `publication_resume.py`, `validation_feedback.py`, `validation_feedback_state.py`, `verify_process.py`, `worker_outcome.py`, `worker_session.py` | 내부 실행·검증 구현. `loop_manager.py`와 handler/runtime/state가 직접 import하고 handler tests가 recovery·verify·review·publish 경계를 patch한다. | G-063에서 `loop_manager_internal/execution/`으로 이동 후보. G-123~G-124가 composition과 handler 책임을 분리한다. |
| `execplan_archive.py`, `execplan_archive_state.py`, `execplan_catalog.py`, `execplan_catalog_state.py`, `planning_catalog.py`, `project_catalog_refresh.py`, `requirement_archive.py`, `requirement_archive_recovery.py`, `requirement_archive_state.py` | 내부 catalog·archive·SQLite 구현. Manager, planning/runtime, archive tests가 직접 import하며 archive 경로·catalog reconciliation을 보존해야 한다. | G-063에서 `loop_manager_internal/catalog/`과 `storage/`로 이동 후보. G-120/G-122가 transaction과 archive adapter를 분리한다. |
| `maintenance_file_inventory.py`, `maintenance_file_ledger.py`, `maintenance_file_publication.py`, `maintenance_file_review.py`, `maintenance_findings.py`, `maintenance_git.py`, `maintenance_orchestration.py`, `maintenance_planning.py`, `maintenance_reconciliation.py`, `maintenance_state.py`, `full_assessment.py` | 내부 maintenance/assessment 구현. revision-bound inventory, file review ledger, planning coordinator와 tests가 직접 import한다. `full_assessment.py`는 runtime prompt와 verify compile 목록에도 남아 있다. | G-063에서 `loop_manager_internal/maintenance/`으로 이동 후보. G-125가 planning/assessment port를 분리하고 G-127이 verified replacement 뒤 temporary assessment code를 제거한다. |
| `managed_config.py`, `managed_rebase_adoption.py`, `managed_rebase_evidence.py`, `managed_rebase_observation.py`, `managed_rebase_publication.py`, `managed_rebase_runtime.py`, `managed_rebase_validation.py`, `manager_git_journal.py`, `rebase_block_reconciliation.py`, `rebase_correction.py`, `rebase_correction_state.py`, `rebase_file_evidence.py`, `runtime_scope.py` | 내부 config/Git/workspace 구현. Manager and execution/recovery modules directly import them; rebase tests patch their exact symbols and journal/lease boundaries. | G-063에서 `loop_manager_internal/git/`, `runtime/`, `config/`으로 이동 후보. G-120/G-122가 state·workspace/Git mutation ownership을 분리한다. |
| `document_drafts.py`, `document_publication.py`, `guidance_approval.py`, `notifications.py`, `quality_guidance.py`, `reviewer_specification.py`, `project_registry.py`, `recovery_dispatcher.py`, `recovery_runtime.py`, `recovery_state.py`, `runtime_state.py`, `work_exhaustion.py` | 내부 document/guidance/notification/project/runtime 구현. Manager와 execution/planning/recovery modules가 import하고 관련 tests가 public helper와 SQLite event state를 검증한다. | G-063에서 `loop_manager_internal/documents/`, `notifications/`, `runtime/`, `storage/`으로 이동 후보. G-120, G-122, G-126이 해당 책임을 분리한다. |

각 internal 후보는 아래 import graph 조사에서 하나의 module로 취급하며 파일 이동은 하지 않는다. 모든 internal 후보의 공통 안전 목적지는 G-063이 확정할 root 이름과 충돌하지 않는 `loop_manager_internal/` namespace다. leaf import부터 이동하고 `loop_manager.py`는 필요한 명시적 public re-export만 유지한다. 각 후보는 `loop_manager.py`의 top-level import, 직접 import하는 production module, 해당 `tests/test_*.py` patch/import와 Linux·Windows verify의 compile 목록을 함께 갱신할 때만 이동할 수 있다. 단지 길이가 길거나 현재 root에 있다는 사실은 이동 근거가 아니다.

`loop_manager.py`의 SQLite transaction/schema·scheduler composition은 G-120/G-123, Agent session/isolation은 G-121, Git mutation·remote lease·checkpoint/validation token·recovery adapter는 G-122, execution handler의 recover/guard/run/verify/review/publish는 G-124, planning/assessment 및 `full_assessment` 소비는 G-125, notification outbox/rendering/delivery는 G-126, 남은 inventory·temporary code와 verify references는 G-127로 대응한다. `tests/test_loop_manager.py`의 module-wide profile/session/Git/catalog patch 수명은 G-119가 먼저 분리한다. 이 대응표는 현재 ownership을 바꾸지 않으며 SQLite 저장 형식, CLI, launcher, user path, public contract와 verify path를 유지한다.

공통 provider-neutral prompt에는 고정 root 경로, 신규 root 파일의 구체적 계약 근거, 첫 정상 파일별 평가의 root inventory, 불확실한 소유권의 자동 이동 금지와 Reviewer blocking 기준을 추가했다. 따라서 등록·배포만을 이유로 별도 전수 평가를 시작하지 않고, G-086의 다음 정상 평가가 현재 revision-bound inventory와 이전 결과를 재사용하거나 hash drift를 다시 확인한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 역할별 prompt와 `tests/test_managed_configuration.py`의 공통 지침 배포 관련 targeted unit test만 실행한다.
- 현재 루트 inventory가 필수 launcher·manifest·문서와 내부 모듈을 근거로 구분하고 기존 등록 프로젝트의 prompt가 고정 경로를 포함하는지 검증한다.
- 기존·신규 프로젝트의 첫 정상 파일별 평가에서 루트 항목을 빠짐없이 조사하고 등록·배포 시 별도 전수 평가를 시작하지 않으며, 불확실한 항목을 자동 이동 대상으로 만들지 않는지 검증한다.
- 전체 verify에서 기존 실행 진입점, 문서·verify 경로와 등록 프로젝트 공통 지침이 유지돼야 한다.
- Worker는 inventory 재사용·분류를 변경한 경우 그 경계의 targeted unit test만 실행한다. 코드 변경 없이 자체 조사 결과만 기록한 부분은 unit test 비실행 사유를 명시한다. 전체 파일을 읽는 분석과 테스트 실행을 구분한다.

## Progress

- [x] 루트 inventory 및 안전한 이동 경계 확정
- [x] 공통 파일 배치 지침과 targeted test 추가
- [x] 최신 원격 기준 자체 구조·중복·fixture 대응표와 G-119~G-127 범위 확인

## Decision Log

- `loop_manager.py`는 현재 launcher가 직접 참조하므로 루트에 둔다. 이동 후보는 파일명이나 길이가 아니라 실행·도구 탐색 필요 여부로 판정한다.
- G-051의 분리 모듈도 루트 inventory에 포함하되, 실제 이동 여부는 G-060의 동일한 참조·소유권 기준으로 판정한다.
- G-077과 G-080의 실행·계획 catalog 변경이 Merge된 뒤의 루트 구조를 조사한다. 두 작업에서 바뀐 import·테스트 참조를 G-063의 이동 근거에 포함한다.
- G-081의 checkpoint 복구 경계와 G-083~G-087의 파일별 평가 모듈·실행 경계가 Merge된 뒤 루트 inventory를 확정한다. R-028의 첫 구조 조사는 R-042의 정상 평가 일정 안에서 수행하며 별도 조기 평가를 만들지 않는다.
- G-088~G-092의 Agent 격리·Git 범위·Manager checkpoint 및 차단 복구가 Merge된 뒤 그 경계의 새 모듈과 테스트 참조도 inventory에 포함한다.
- G-093~G-095의 planning 상태·차단 사건 복구가 Merge된 뒤 변경된 복구 경계와 테스트 참조를 inventory에 포함한다.
- G-099~G-101의 단계별 rebase 증거·복구가 Merge된 뒤 변경된 코드·테스트·검증 참조를 inventory에 포함한다.
- G-104~G-107의 중간 checkpoint 게시와 차단 중 원격 복구가 Merge된 뒤 변경된 코드·테스트·검증 참조를 inventory에 포함한다.
- 2026-09-30: mutable READY snapshot의 기준 revision·원본 hash를 확인해 inventory 범위를 갱신했다. G-092와 G-107의 dependency를 통해 G-108~G-110의 현재 상태 rebase 판정·수용·복구가 먼저 Merge된다. 그 변경의 내부 모듈·import·테스트 참조도 조사하며 동일한 선행 작업을 새 루트 정리 계획으로 중복하지 않는다.
- 2026-10-02: 사용자가 큰 코드·테스트 파일의 기능별 분리, 중복 제거와 평가용 임시 코드 정리 초안을 요청하고 미착수 계획 보완을 허용했다. 읽기 전용 SQLite 확인에서 G-060·G-063은 READY, ever_started=0, workspace_owned=0이고 branch·worktree·Worker/Reviewer session·PR이 없으며 파일 hash와 current revision이 일치했다. 이번 보완은 사용자 요청에 따른 직접 계획 편집이며 자동 유지보수 mode나 mutable snapshot 권한을 새로 부여하지 않는다. SQLite와 활성 assessment cycle은 변경하지 않는다.
- 초안의 원본 revision은 `1d2636243231f9c8b2a7921ff703d95136838d98`이다. `full_assessment.source_inventory()`로 추적 대상 146개를 산출하고 AST와 직접 읽기로 위 구조를 확인했다. 이는 전체 의미 검토의 완료 결과가 아니다. 실제 구현 시작 시 최신 원격 revision의 코드·테스트를 다시 확인한다.
- 2026-10-02 사용자 결정: 리팩터링을 기존 다른 미착수 작업보다 먼저 진행한다. G-060·G-063·G-119~G-127을 `critical`로 지정하고, G-060에서 미착수 G-066·G-112·G-113·G-114 선행 의존성을 제거한다. 네 기존 계획은 원래 목표·검증 조건을 보존한 채 G-127 Merge를 기다린다. 이미 MERGED인 선행 작업과 기존 실행·소유권·유지보수 안전 gate는 유지하며 SQLite 상태를 직접 바꾸지 않는다.
- 실행 순서는 G-060 → G-063 → G-119 → G-120 → G-121 → G-122 → G-123 → G-124 → G-125 → G-126 → G-127이다. 이후 기존 미착수 계획은 원래 priority·dependency 규칙으로 재개하고 최신 구조의 구현·테스트 경로를 확인한다. 이 사용자 요청은 직접 계획 편집이며 자동 Planner의 변경 권한을 확대하지 않는다.
- 2026-10-02: `d523989e65103ca187815b54808abf09e7ac867f`의 `full_assessment.source_inventory()`와 같은 revision의 root tree를 대조했다. inventory는 147개의 hash-bound 추적 분석 대상을 산출했지만 full assessment 완료나 SQLite 성공으로 기록하지 않았다. root 고정 항목, launcher·config·manifest 및 내부 module의 import/test/verify 결합은 Implementation의 대응표에 기록했으며 실제 이동은 G-063 이후 책임 분리 계획만 수행한다.
- 2026-10-02: 모든 역할에 주입되는 provider-neutral `AGENT_QUALITY_GUIDANCE`에 root placement 기준을 추가했다. 이 기준은 첫 정상 파일별 구조 평가의 root inventory를 요구하되 평가 schedule을 앞당기지 않고, 근거가 불명확한 파일의 자동 이동·삭제를 금지한다.
