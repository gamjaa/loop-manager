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
| `maintenance_file_inventory.py`, `maintenance_file_ledger.py`, `maintenance_file_publication.py`, `maintenance_file_review.py`, `maintenance_findings.py`, `maintenance_git.py`, `maintenance_orchestration.py`, `maintenance_planning.py`, `maintenance_reconciliation.py`, `maintenance_state.py`, `planning_runtime.py`, `full_assessment.py` | 내부 maintenance/assessment 구현. revision-bound inventory, file review ledger, planning coordinator와 tests가 직접 import한다. `full_assessment.py`는 runtime prompt와 verify compile 목록에도 남아 있다. | G-063에서 `loop_manager_internal/maintenance/`으로 이동 후보. G-125가 planning/assessment port를 분리하고 G-127이 verified replacement 뒤 temporary assessment code를 제거한다. |
| `managed_config.py`, `managed_rebase_adoption.py`, `managed_rebase_evidence.py`, `managed_rebase_observation.py`, `managed_rebase_publication.py`, `managed_rebase_runtime.py`, `managed_rebase_validation.py`, `manager_git_journal.py`, `rebase_block_reconciliation.py`, `rebase_correction.py`, `rebase_correction_state.py`, `rebase_file_evidence.py`, `runtime_scope.py` | 내부 config/Git/workspace 구현. Manager and execution/recovery modules directly import them; rebase tests patch their exact symbols and journal/lease boundaries. | G-063에서 `loop_manager_internal/git/`, `runtime/`, `config/`으로 이동 후보. G-120/G-122가 state·workspace/Git mutation ownership을 분리한다. |
| `document_drafts.py`, `document_publication.py`, `guidance_approval.py`, `notifications.py`, `quality_guidance.py`, `reviewer_specification.py`, `project_registry.py`, `recovery_dispatcher.py`, `recovery_runtime.py`, `recovery_state.py`, `runtime_state.py`, `work_exhaustion.py` | 내부 document/guidance/notification/project/runtime 구현. Manager와 execution/planning/recovery modules가 import하고 관련 tests가 public helper와 SQLite event state를 검증한다. | G-063에서 `loop_manager_internal/documents/`, `notifications/`, `runtime/`, `storage/`으로 이동 후보. G-120, G-122, G-126이 해당 책임을 분리한다. |

각 internal 후보는 아래 import graph 조사에서 하나의 module로 취급하며 파일 이동은 하지 않는다. 모든 internal 후보의 공통 안전 목적지는 G-063이 확정할 root 이름과 충돌하지 않는 `loop_manager_internal/` namespace다. leaf import부터 이동하고 `loop_manager.py`는 필요한 명시적 public re-export만 유지한다. 각 후보는 `loop_manager.py`의 top-level import, 직접 import하는 production module, 해당 `tests/test_*.py` patch/import와 Linux·Windows verify의 compile 목록을 함께 갱신할 때만 이동할 수 있다. 단지 길이가 길거나 현재 root에 있다는 사실은 이동 근거가 아니다.

`loop_manager.py`의 SQLite transaction/schema·scheduler composition은 G-120/G-123, Agent session/isolation은 G-121, Git mutation·remote lease·checkpoint/validation token·recovery adapter는 G-122, execution handler의 recover/guard/run/verify/review/publish는 G-124, planning/assessment 및 `full_assessment` 소비는 G-125, notification outbox/rendering/delivery는 G-126, 남은 inventory·temporary code와 verify references는 G-127로 대응한다. `tests/test_loop_manager.py`의 module-wide profile/session/Git/catalog patch 수명은 G-119가 먼저 분리한다. 이 대응표는 현재 ownership을 바꾸지 않으며 SQLite 저장 형식, CLI, launcher, user path, public contract와 verify path를 유지한다.

`maintenance_file_inventory.root_placement_inventory()`는 추적 tree의 모든 top-level 항목을 제공하며, 자동 분류는 조사 결과가 아니다. `ProvisionalReviewRepository.progress()`는 정상 파일 평가 안에서 루트 조사 단위를 선택하고 기존 `FILE_REVIEW_RESULT`·세션·재시도 경로를 재사용한다. 조사 결과는 모든 항목의 실제 참조 인용, 사용자 경로, 소유권, 안전한 목적지 또는 유지·불확실 근거를 포함한다. 참조 원본과 항목별 coverage를 검증한 결과만 publication intent와 종합 증거에 전달하며, `FileReviewRepository.record_success()`가 source review와 같은 transaction에서 저장한다. 파일 목록만으로 실행·사용 계약을 입증할 수 없으며, 참조·소유권을 확인하지 못한 항목은 `uncertain`과 빈 목적지로 기록한다.

기존 조사 결과는 같은 immutable revision의 coverage와 원본 근거가 유효할 때 재사용한다. 기존 자동 분류만 저장된 결과나 revision·참조 drift는 다음 정상 평가에서 조사 단위를 다시 선택한다. source 선택이 비어 있어도 미조사 루트가 있으면 Agent 평가를 거치며 stabilization fast path는 이를 건너뛰지 않는다. 조사에서 내부 항목과 안전한 목적지가 확인되면 빈 source 완료를 건너뛰고 종합·발견·계획 생성으로 연결한다. source inventory에서 제외된 문서·asset도 검증된 루트 조사 hash와 목적지에 결합된 독립 발견으로 새 계획 또는 동일 문제의 기존 계획에 연결하며, 후보 누락이나 목적지 변경은 거부한다. Git tree 자체가 빈 revision은 조사할 루트가 없으므로 이전 file·root review를 retire한 baseline으로 성공할 수 있다. publication 뒤 Planner 소유 출력 밖의 원본 변경은 성공 처리를 막거나 정상 평가를 재시작한다. 등록·배포만으로 별도 평가를 시작하지 않으며 기존 경로·SQLite schema와 verify 경로를 유지한다.

### 파일별 import·test patch·이동 대응표

아래의 `importer`는 현재 직접 production import, `test`는 이동 때 import/patch target을 함께 바꿔야 하는 대표 직접 test module, `destination`은 G-063의 leaf-first package 이동 목적지다. launcher·CLI·manifest·config·verify path와 SQLite schema는 이 표의 이동 대상이 아니다.

| 파일 | importer / test patch | destination 및 보존 경계 |
| --- | --- | --- |
| `agent_codex_sandbox.py` | `agent_write_isolation.py`; `tests/test_agent_invocation.py` | `loop_manager_internal/agents/sandbox.py`; Codex sandbox policy identity 유지 (G-121). |
| `agent_invocation.py`, `agent_invocation_state.py`, `agent_provider_usage.py`, `agent_sandbox_exec.py`, `agent_scope_decision.py`, `agent_write_isolation.py` | `loop_manager.py`, `execution_handlers.py`, `worker_session.py`; `tests/test_agent_invocation.py`, `tests/test_agent_invocation_state.py`, `tests/test_agent_scope_decision.py`, `tests/test_origin_isolation.py`, `tests/test_worker_session.py` | `loop_manager_internal/agents/`; session binding·provider isolation·scope approval patch points 유지 (G-121). |
| `blocked_checkpoint.py`, `blocked_checkpoint_state.py`, `blocked_plan_reconciliation.py`, `blocked_remote_observation.py`, `checkpoint_ownership.py` | `loop_manager.py`, `project_blocked_recovery.py`, `execution_handlers.py`; `tests/test_blocked_checkpoint.py`, `tests/test_blocked_checkpoint_state.py`, `tests/test_blocked_remote_observation.py`, `tests/test_execution_checkpoint.py`, `tests/test_catalog_checkpoint_wiring.py` | `loop_manager_internal/recovery/`; checkpoint identity, remote observation, SQLite transaction owner 유지 (G-122). |
| `project_blocked_recovery.py`, `scoped_blocking.py` | `loop_manager.py`, `recovery_runtime.py`, `execution_handlers.py`; `tests/test_scoped_blocking.py`, `tests/test_recovery_runtime.py`, `tests/test_execution_handlers.py` | `loop_manager_internal/recovery/`; plan/project scope and recovery disposition 유지 (G-122). |
| `execution_checkpoint.py`, `execution_checkpoint_state.py`, `execution_revision.py`, `execution_runtime.py`, `execution_state.py` | `loop_manager.py`, `execution_handlers.py`, `managed_rebase_*`; `tests/test_execution_checkpoint.py`, `tests/test_execution_checkpoint_git.py`, `tests/test_execution_revision.py`, `tests/test_execution_runtime.py` | `loop_manager_internal/execution/`; validation token, revision identity, state transition 유지 (G-123/G-124). |
| `execution_handlers.py`, `publication_resume.py`, `validation_feedback.py`, `validation_feedback_state.py`, `verify_process.py`, `worker_outcome.py`, `worker_session.py` | `loop_manager.py`, `execution_runtime.py`; `tests/test_execution_handlers.py`, `tests/test_validation_feedback.py`, `tests/test_validation_feedback_state.py`, `tests/test_verify_process.py`, `tests/test_worker_outcome.py`, `tests/test_worker_session.py` | `loop_manager_internal/execution/`; recover→guard→run→verify→review→publish 순서와 Worker result protocol 유지 (G-124). |
| `execplan_archive.py`, `execplan_archive_state.py`, `execplan_catalog.py`, `execplan_catalog_state.py`, `planning_catalog.py`, `project_catalog_refresh.py` | `loop_manager.py`, `document_drafts.py`, `planning_runtime.py`; `tests/test_execplan_archive.py`, `tests/test_execplan_catalog.py`, `tests/test_execplan_catalog_state.py`, `tests/test_project_catalog_refresh.py` | `loop_manager_internal/catalog/`; catalog revision, archive eligibility, SQLite source authority 유지 (G-120/G-122). |
| `requirement_archive.py`, `requirement_archive_recovery.py`, `requirement_archive_state.py` | `loop_manager.py`, `execution_handlers.py`, `recovery_runtime.py`; `tests/test_requirement_archive.py`, `tests/test_requirement_archive_recovery.py`, `tests/test_requirement_archive_state.py`, `tests/test_requirement_archiving.py` | `loop_manager_internal/catalog/`; content-preserving archive and recovery identity 유지 (G-122). |
| `maintenance_file_inventory.py`, `maintenance_file_ledger.py`, `maintenance_file_publication.py`, `maintenance_file_review.py`, `maintenance_reconciliation.py`, `maintenance_findings.py` | `planning_runtime.py`, `loop_manager.py`; `tests/test_maintenance_file_inventory.py`, `tests/test_maintenance_file_review.py`, `tests/test_maintenance_reconciliation.py`, `tests/test_maintenance_findings.py`, `tests/test_maintenance_state.py` | `loop_manager_internal/maintenance/`; revision/hash-bound inventory, root review ledger, chunk evidence와 fixture patch target 유지 (G-125). |
| `maintenance_git.py`, `maintenance_orchestration.py`, `maintenance_planning.py`, `maintenance_state.py`, `planning_runtime.py`, `full_assessment.py` | `loop_manager.py`; `tests/test_maintenance_orchestration.py`, `tests/test_maintenance_planning.py`, `tests/test_planning_runtime.py`, `tests/test_maintenance_state.py` | `loop_manager_internal/maintenance/`; assessment cycle, planner authority, source revision and pending full-assessment report 유지 (G-125/G-127). |
| `managed_config.py`, `managed_rebase_adoption.py`, `managed_rebase_evidence.py`, `managed_rebase_observation.py`, `managed_rebase_publication.py`, `managed_rebase_runtime.py`, `managed_rebase_validation.py`, `manager_git_journal.py`, `rebase_block_reconciliation.py`, `rebase_correction.py`, `rebase_correction_state.py`, `rebase_file_evidence.py`, `runtime_scope.py` | `loop_manager.py`, `execution_handlers.py`, `project_blocked_recovery.py`; `tests/test_managed_configuration.py`, `tests/test_managed_rebase_*`, `tests/test_manager_git_journal.py`, `tests/test_rebase_*`, `tests/test_runtime_scope.py` | `loop_manager_internal/git/`, `runtime/`, `config/`; Git mutation lease, journal receipt, workspace scope and config identity 유지 (G-120/G-122). |
| `document_drafts.py`, `document_publication.py`, `guidance_approval.py`, `notifications.py`, `reviewer_specification.py` | `loop_manager.py`, `execution_handlers.py`; `tests/test_guidance_approval.py`, `tests/test_notifications.py`, `tests/test_maintenance_planning.py` | `loop_manager_internal/documents/`, `notifications/`; approval hash, document publication and outbox/rendering contract 유지 (G-126). |
| `project_registry.py`, `recovery_dispatcher.py`, `recovery_runtime.py`, `recovery_state.py`, `runtime_state.py`, `work_exhaustion.py`, `quality_guidance.py` | `loop_manager.py`, `execution_handlers.py`, `planning_runtime.py`; `tests/test_managed_configuration.py`, `tests/test_recovery_runtime.py`, `tests/test_recovery_state.py`, `tests/test_runtime_state.py`, `tests/test_notifications.py`, `tests/test_loop_manager.py` | `loop_manager_internal/runtime/` and `quality/`; registry/base identity, execution state, completion event and shared prompt contract 유지 (G-120/G-126). |

### G-123: 잔여 root 본문과 공개 구성 대응

`71b6c0e7784393643e6128a76be38a7b565100ab`의 root와 기존 G-063 패키지를 다시 확인했다. 아래는 G-123의 이동 기록이며 다른 계획의 목표·상태를 바꾸지 않는다. 새 root 항목은 없다.

| 현재 root 항목 | 실제 참조·사용자 경로 / 소유권 | 목적지 또는 유지 근거 |
| --- | --- | --- |
| `.agents/`, `docs/`, `scripts/`, `AGENTS.md` | 등록·Planner·역할 지침과 Manager의 OS별 verify 탐색; 저장소 관리 파일 | 기존 고정 경로 유지 |
| `README.md` | 사용자 설치·CLI 실행 안내; 저장소 관리 문서 | 사용자 진입 문서로 유지 |
| `run.sh`, `run.ps1`, `loop_manager.py` | launcher와 `runtime.bootstrap`의 runtime child 실행; 저장소 관리 진입점 | 경로 유지. Python root에는 `build_parser`, `main`, 공개 export와 명시적 서비스 구성만 유지 |
| `config/`, `requirements.txt`, `.gitignore` | source config 전달, setup의 Python 의존성 설치, Git의 config/cache 제외; 저장소 관리 설정 | 실행·도구 계약 때문에 유지 |
| `tests/`, `loop_manager_internal/` | 두 verify의 discovery/compile, CLI import와 복사 runtime fixture; 저장소 관리 테스트·내부 구현 | 기존 package root 유지; 내부 본문은 아래 책임별 하위 module로 이동 |
| `.git` | linked worktree Git identity와 index/ref 조회; Git 소유 control 경로 | 이동·삭제 대상이 아님 |
| `__pycache__/` (검증 중 생성) | Python import가 만든 `.gitignore` 제외 파생 cache; 사용자 입력·추적 source가 아님 | source·CLI 이동 근거로 사용하지 않고 이 변경에서는 보존 |
| `.aws/`, `.codex/` | 실행 환경이 노출한 읽기 전용 비추적 경로; 이 변경에서 저장소 소유·사용 참조를 확정하지 않음 | 불확실 항목으로 보존하고 이동·삭제하지 않음 |

아래 경로는 모두 `loop_manager_internal/` 기준이다. 실제 소비자는 root의 명시적 export·callback 구성과 해당 역할의 기존 coordinator이며, callback에는 module 전체를 전달하지 않는다. root patch는 생성 이후에도 callback 호출 시 다시 해석한다.

| 기존 root 범위 / 소비 계약 | 이동·통합 목적지 / 책임 |
| --- | --- |
| `_run_project_step`, `_run_project_result`, `run_project_result`, `run_project` / catalog·scheduler unit tests | `application/project_service.py`, `project_runner.py`, `project_outcomes.py`; 안전 확인→catalog/cleanup→PR→기존 실행 복구→기존 coordinator 호출과 typed 결과 |
| `_run_registered_project`, 다중 project scheduling, self Merge / scheduler·handoff tests | `application/scheduler.py`, `self_runtime.py`, `runtime/handoff_gate.py`; 연결별 실행, 최대 동시 수, preflight와 subprocess 시작 gate |
| registry·document draft CLI / CLI 및 복사 runtime tests | `application/commands.py`, `runtime/registration_service.py`; 기존 command dispatch·등록 검증과 외부 config identity |
| planning port와 execution port / planning·maintenance·execution 소비자 | `planning/state_ports.py`, `git_ports.py`, `agent_ports.py`, `validation_ports.py`, `event_ports.py`, `planning_ports.py`, `maintenance/review_ports.py`, `execution/execution_ports.py`; 역할별 named effects 조립. coordinator 본문은 기존 module에 유지 |
| catalog, READY/dependency/maintenance 선택, archive / catalog refresh·selection tests | `catalog/catalog_service.py`, `legacy_migration.py`, `project_selection.py`, `archive_service.py`, `requirement_archive_service.py`, `maintenance/cycle_service.py`; cache와 실행 자리, PR 우선 및 cycle DAG 유지 |
| planning 입력·검증·게시 / 기존 planning coordinator와 port | `planning/input_fingerprints.py`, `planner_validation.py`, `planner_publication.py`, `planning_workspace.py`, `diagnostics.py`, `maintenance/maintenance_validation.py`, `maintenance_publication.py`; 기존 revision·snapshot·publication transaction 경계 유지 |
| bootstrap·state·provider·scope의 잔여 연결 / 기존 adapter·repository | `runtime/bootstrap_adapter.py`, `operation_events.py`, `storage/project_state_adapter.py`, `agents/agent_adapter.py`, `scope_adapter.py`, `scope_state_service.py`; 기존 G-120/G-121 adapter에 위임하고 실제 state/resource identity 전달 |
| Git/workspace/publication의 잔여 본문 / 기존 Git adapter와 execution | `git/git_adapter.py`, `workspace_service.py`, `direct_rebase_service.py`, `publication_service.py`, `base_fetch_service.py`, `cleanup_service.py`, `merge_cleanup_service.py`; lease·receipt·cleanup 판정과 소유 증거 보존 |
| block/checkpoint/PR recovery의 잔여 본문 / 기존 recovery coordinator | `recovery/recovery_adapter.py`, `pr_ownership.py`, `block_service.py`, `blocked_agent_service.py`, `blocked_plan_service.py`, `project_block_service.py`, `checkpoint_service.py`, `waiting_pr_service.py`; 계획/프로젝트 차단과 session·retry 보존 |
| metadata·Worker/Reviewer 응답·guidance·PR 본문 / 기존 execution handler | `execution/plan_validation.py`, `result_validation.py`, `documents/guidance_service.py`, `pr_description.py`; 기존 검증·승인·게시 규칙 유지 |
| 공개 결과·예외 및 helper 호출 서명 / 기존 direct import·patch 소비자 | `application/contracts.py`, `compatibility.py`; 기존 class identity와 함수 인자를 보존하고 주입 인자는 공개 서명에서 제외 |

484개 원본 정의를 이동한 뒤 AST를 역대응해 비교했으며, 의도적으로 바뀐 본문은 프로젝트 단계 분리와 execution contract의 `globals()` 제거 두 곳이다. 그 밖의 본문은 named effect 접근으로만 바뀌었다. 상태 필드·status 집합은 `storage/project_state_adapter.py`가 기존 `runtime_state`의 선언을 재사용하므로 순수 결과 contract가 repository를 import하지 않는다. G-124/G-125/G-126의 상세 coordinator·알림 분리는 수행하지 않았다. 두 verify의 compile 목록에 새 module을 포함했으며 Worker는 스크립트를 실행하지 않았다.

### G-124: 실행 phase와 recovery 대응

기준 revision `eb3331da49ce36d7ff9e55d8d6eca8b5ca1ae306`의 실제 루트 17개 항목은 위 G-123 inventory와 일치한다. `.agents/`, `docs/`, `scripts/`, `AGENTS.md`는 등록·Agent 지침·verify의 고정 경로, `README.md`는 사용자 안내, `run.sh`·`run.ps1`·`loop_manager.py`는 launcher/bootstrap 진입점이다. `config/`·`requirements.txt`·`.gitignore`는 source 설정·setup·Git 탐색, `tests/`·`loop_manager_internal/`는 verify discovery와 Python import의 소유 경계로 유지한다. Git 소유 `.git`, ignored 파생 `__pycache__/`, 소유권을 확정하지 않은 읽기 전용 `.aws/`·`.codex/`는 이동·삭제하지 않는다. 새 루트 항목은 없으며 아래 목적지는 모두 기존 패키지와 tests/support 안이다.

아래 handler 경로는 `loop_manager_internal/execution/` 기준이다. 소비자는 기존 `ExecutionPhaseCoordinator` → `ExecutionHandlers` 위임 경계이며 SQLite 형식과 Git 효과 adapter를 바꾸지 않는다.

| 원본 method·입력 | 목적지·재사용 service | 효과·상태 확정 경계 |
| --- | --- | --- |
| `_context`, `_cached_plan`, `_advanced`, `work_started` / 선택된 row | `execution_context_handler.py`; `ExecutionStatePort`, notification outbox | 최신 projection 확인 뒤 immutable 실행 context 구성; 작업 재개 알림의 transaction 유지 |
| `prepare`, `recover`, `_workspace_boundary` / context·checkpoint | `execution_workspace_handler.py`; 기존 checkpoint `assess/observe` | workspace 예약·소유권·scope와 저장 checkpoint를 확인한 뒤 make/recover; stale marker 교정은 기존 snapshot transaction 사용 |
| `_recover`, `_block_final`, `recover_failure` / typed reason·실패 증거 | `execution_recovery_handler.py`; `RecoveryDispatcher` | retry·pause·final block 지문과 횟수·사용자 대상 기록; 단계별 정책을 그대로 사용 |
| `_owned_checkpoint_remote`, current/persisted ownership helpers / live remote·PR·checkpoint | `execution_ownership_handler.py`; `ManagedRebaseService`, `checkpoint_ownership` | 현재 lease·worktree·수용된 checkpoint identity 재검사; 과거 journal만으로 게시 허용하지 않음 |
| rebase intent·continue·stage·completion·중간 push / context·현재 관측 | `execution_rebase_handler.py`; manager journal, rebase correction repository, managed adoption/publication | Git 전 intent, stage 증거와 journal 확인, completion·remote lease의 원자 확정 및 재시작 멱등성 유지 |
| `guard`, revision refresh/install, metadata correction / definition·base·stage token | `execution_revision_handler.py`; `DefinitionSnapshot`, `decide_revision_gate` | catalog/base 일치·dependency·후보 HEAD·archive token 재검사; 필요한 phase만 무효화하고 session 보존 |
| archive candidate·target·collision·workspace gates / revision-bound archive snapshot | `execution_archive_handler.py`; 기존 archive callbacks와 `ArchiveTargetRepository` | 각 단계의 최신 후보·실제 이동 집합을 대조하고 target 저장 시 base/catalog race 거부 |
| `run`, saved rebase result, `_finish_worker`, outcome·사용자 결정 / immutable context | `execution_worker_handler.py`; worker_session, worker_outcome, validation_feedback | profile/session 전환 intent → Agent → 결과·scope·guidance·archive 검증 → VERIFYING; correction/usage pause와 같은 세션 재개 유지 |
| `verify`, `_verify_once`, `_finish_verified` / 후보 HEAD·Worker 결과 | `execution_verify_handler.py`; 기존 Manager verify callback·feedback repository | 최신 base 반영 뒤 verify, scope와 HEAD 재검사 후 verify token 저장; 실패만 Worker로 전달 |
| `review`, reviewer attempt·entry evidence / 현재 Worker·verify 증거 | `execution_review_handler.py`; reviewer parser·feedback·recovery correction budget | 읽기 전용 Reviewer, 각 attempt 감사·교정 한도와 session 유지; 성공 후보에만 review/publication token 저장 |
| `reconcile`, latest-base helpers / BEFORE_VERIFY 또는 BEFORE_PUBLICATION | `execution_latest_base_handler.py`; 공통 rebase/checkpoint 서비스 | 두 시점의 독립 fetch·재검증 유지; base 변경 뒤 Worker 확인과 verify·Review를 다시 요구 |
| `publish`, commit/push/PR intent·resume / immutable 후보·live PR/remote | `execution_publication_handler.py`; publication_resume와 기존 Git/PR adapter | 게시 전후 lease·후보·scope·archive 재확인; 성공 OID/PR 확인 뒤에만 PR_OPEN transaction |
| 승인 문서 lookup·guard·apply·Review artifact / draft identity·승인 원본 | `execution_documents_handler.py`; document_drafts/publication | 승인 mapping·원본·정확한 bytes·의미 보존 verdict 확인; Agent 없이 승인 문서만 적용하고 기존 차단 경계 유지 |
| 기존 effects·임시 contract·준비 cache / G-123 조립 | `execution_effects.py`, `execution_session.py`, `execution_assembly.py` | frozen contract/identity/preparation, 명시적 필요 callback만 phase port로 전달; 전체 effects는 조립 경계에만 남음 |
| `tests/test_execution_handlers.py` / 원본 277개 test method | 단계별 `tests/test_execution_*.py`; `tests/support/execution*_scenarios.py`, `execution_assertions.py` | G-119 fixture·모든 기존 assertion 재사용, TestCase를 support로 가져오지 않음; 외부 효과 fake는 구현 전이를 대신하지 않음 |

원본 테스트 277개는 정확히 한 destination에 대응하며 assertion을 보존했다. 두 Review deadlock 테스트는 원래 source 변경을 immutable 준비 snapshot에 명시적으로 설치하도록 setup만 보완했다. 별도의 composition unit test 3개는 준비 정보 격리·새 snapshot 관측·낡은 context 거부를 검증한다. 두 verify 스크립트는 새 module의 compile 목록만 확장하고 전체 unittest discovery를 유지한다.

### G-127: 최종 구조·임시 평가 코드 대조

기준 HEAD `0795a1a90c289d174b1e7ca55d4fab7f993a14d8`와 현재 작업 tree를 대조했다. 위 최신 root inventory의 추적 루트 13개는 같은 사용자 진입·실행·도구 소유권으로 남아 있고 새 루트 항목은 없다. `.git`과 소유권 불명 `.aws/`·`.codex/`, ignored `__pycache__/`는 이동·삭제하지 않았다. 아래 대응은 G-060의 구조·중복 발견 각각의 현재 소비·목적지이며 전체 verify·Review·Merge 완료 선언이 아니다.

| G-060 발견 | 현재 책임·소비 및 확인 결과 |
| --- | --- |
| root `loop_manager.py`의 혼합 책임 | G-120/G-123의 `application/`, `runtime/`, `catalog/`, `storage/`와 역할별 adapter가 본문을 소유한다. root는 launcher와 공개 구성 경계로 유지한다. |
| `tests/test_loop_manager.py`의 fixture·patch 결합 | G-119의 `tests/support/`와 책임별 `tests/test_*.py`가 소비한다. 현재 AST 조사에서 중복 TestCase 이름이 없다. |
| execution handler와 거대 테스트 | G-124의 `execution/execution_*_handler.py`, `tests/test_execution_*.py` 및 `tests/support/execution*_scenarios.py` 대응표를 유지한다. |
| planning coordinator와 maintenance 테스트 결합 | G-125의 `planning/assessment_service.py`, `planning/planning_service.py`, `planning/*_service.py`와 각 port·분리된 tests가 소유한다. |
| block/checkpoint·origin/invocation 테스트 결합 | G-120~G-122의 `recovery/`, `agents/`, `runtime/`, `git/` adapter와 분리된 test module이 소유한다. |
| notification 저장·rendering·delivery 혼합 | G-126의 `notifications/repository.py`, `rendering.py`, `delivery.py`가 소유하며 `notifications.py`는 공개 facade다. |
| 나머지 state·Git 결합 | G-120/G-122의 repository·Git adapter가 유지한다. `runtime_state.py`, `execplan_catalog_state.py`, `manager_git_journal.py`의 저장·증거 책임은 그대로이므로 길이만을 근거로 이동하지 않는다. |
| 일회성 `full_assessment.py` | G-127에서 삭제했다. `source_inventory`는 기존 `maintenance_file_inventory.py`, prompt는 `maintenance_planning.py`, 결과 parser와 이전 report reader는 `maintenance_validation.py`가 소유한다. `planning/assessment_port.py`가 세 경계를 호출하고 `assessment_service.py`가 중단 재개·bounded retry를 맡는다. `documents/document_drafts.py`의 완료 report 소비와 SQLite column·marker는 유지한다. |

원본 `full_assessment.py`의 runtime 소비는 `planning/assessment_port.py` 한 곳이었고, 테스트 소비는 `tests/test_maintenance_planning.py`와 `tests/test_planning_assessment.py`였다. `tests/test_maintenance_publication.py`, `tests/test_maintenance_assessment.py`, `tests/test_maintenance_authority.py`, `tests/support/maintenance_planning_case.py`의 미사용 import도 제거했다. `scripts/verify.sh`와 `scripts/verify.ps1`은 삭제한 module의 compile 인자를 제거했고 같은 198개 runtime/package Python 파일을 대상으로 한다. 작업 tree의 Python 파일 396개, test module 167개와 컴파일 경로를 실행 없이 대조했으며 두 verify의 전체 unittest discovery는 유지했다. 과거 문서의 경로·revision·판단 근거는 감사 기록으로 보존한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 역할별 prompt와 `tests/test_managed_configuration.py`의 공통 지침 배포 관련 targeted unit test만 실행한다.
- 현재 루트 inventory가 필수 launcher·manifest·문서와 내부 모듈을 근거로 구분하고 기존 등록 프로젝트의 prompt가 고정 경로를 포함하는지 검증한다.
- 기존·신규 프로젝트의 첫 정상 파일별 평가에서 루트 항목을 빠짐없이 조사하고 등록·배포 시 별도 전수 평가를 시작하지 않으며, 불확실한 항목을 자동 이동 대상으로 만들지 않는지 검증한다.
- 전체 verify에서 기존 실행 진입점, 문서·verify 경로와 등록 프로젝트 공통 지침이 유지돼야 한다.
- Worker는 inventory 재사용·분류를 변경한 경우 그 경계의 targeted unit test만 실행한다. 코드 변경 없이 자체 조사 결과만 기록한 부분은 unit test 비실행 사유를 명시한다. 전체 파일을 읽는 분석과 테스트 실행을 구분한다.

## Progress

- [x] 루트 inventory 및 안전한 이동 경계 확정
- [x] 공통 파일 배치 지침과 targeted test 추가
- [x] 미조사·기존 자동 분류 결과의 성공 처리 차단, revision-bound 루트 조사·종합·publication·재사용 검증
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
- 2026-10-02: Review 지적에 따라 root placement를 prompt 문구만으로 처리하지 않았다. immutable tree의 모든 top-level classification을 file-review input으로 전달하고, source review와 빈 source selection 모두 같은 transaction에서 root result를 ledger에 기록하도록 했다. `planning_runtime.py`와 root-inventory 대상의 direct importer/test patch/destination을 Implementation 대응표에 추가했다.

- 2026-10-02: 반복 Review 피드백에 따라 inventory 저장을 성공 조사로 취급하는 경로를 제거했다. 정상 평가의 루트 조사 결과에 참조·사용자 경로·소유권·목적지 계약을 적용하고 원본 인용과 coverage를 검증한다. source가 없는 asset-only tree, 기존 자동 분류, 잘못된 저장 결과 재시도, 참조 drift와 빈 tree retire를 targeted unit test로 확인한다. 기존 verify는 변경 모듈 compile과 전체 unittest discovery를 이미 포함하므로 변경하거나 실행하지 않는다.

- 2026-10-02: 업그레이드 전 `root_reviews`가 없는 durable publication intent는 저장 증거·활성 cycle·source·원격 게시 commit의 identity를 확인한 경우에만 당시 source 평가 범위로 멱등하게 복구한다. 루트 성공 결과를 생성하지 않으며 루트 조사는 다음 정상 평가에 남긴다. 새 형식의 빈 결과나 새로 만든 legacy 증거는 이 예외를 사용할 수 없다. source가 없는 확인된 루트 이동도 종합·계획 연결을 요구하고, 제외된 문서·asset은 루트 조사 근거 hash로 발견을 검증한다. cold restart, 위조·누락·drift, 기존 계획 재사용과 빈 source 분기를 targeted unit test로 확인한다.
