---
id: G-321
title: MERGED archive의 자원 기반 실행과 자동 재시도
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-094] MERGED ExecPlan archive의 독립 실행과 자동 재시도.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
depends_on:
  - G-297
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
  - G-336
complexity: high
risk: high
priority: normal
---

## Purpose

실제 MERGED ExecPlan archive를 프로젝트 전체 유휴 gate에서 분리하고 기존 Manager 조율의 안전 경계에서 처리·재발견·재시도한다. 후보 검증·내용/mode 보존 이동·fast-forward 게시·준비 commit 재사용은 기존 dispatcher와 Git adapter를 유지하며 실행 자원 판단·기회·보류 관측의 미충족 범위만 보완한다.

원격 base의 기존 구현에서 `archive_service._execplan_archive_housekeeping_safe()`는 project phase·planning 자리·활성 작업·Agent scope·유지보수 cycle로 반환한다. 호출은 `ProjectService._synchronize_source_after_pr_reconciliation()`에 있고 `run_project_tasks()`의 연속 배정 중 기회는 없다. gate 진입 전 반환은 archive 사유를 남기지 않으며 candidate pending 로그는 매 dispatch 반복 출력한다. R-094는 이 두 경계를 바꾸되 함께 실행하는 Requirement 교정의 기존 보호는 유지하도록 요구한다.

## Acceptance Criteria

- 최신 R-055의 미Merge CLOSED 보류와 삭제 확정/ID 표식은 실제 MERGED archive와 별개다. 닫히거나 철회된 작업을 MERGED 후보로 추가하거나 archive 근거로 삭제를 취소하지 않는다. 다른 작업의 삭제·관리 보존 대기/명시적 인계만으로 안전한 MERGED 후보를 전역 보류하지 않으며, 대상의 물리 writer/소유 lease·같은 base 게시 충돌은 계속 보호한다.

- R-094 AC1–3: Worker·verify·Reviewer 실행, 독립 차단 계획·Planner 자리·유지보수 cycle만으로 archive를 미루지 않는다. 실제 충돌이 없으면 연속 작업 배정 중에도 기존 안전 경계에서 MERGED 후보를 처리한다. PR 동기화 직후 미처리·후보 기록 없이 진입 전 보류·Manager 재시작에도 DB MERGED와 최신 원격 catalog로 자동 재발견한다.
- 같은 base 게시·실제 충돌하는 미확정 Git 쓰기·대상 자원의 수동 소유·실행/종료 불명 writer와 실제 프로젝트 안전 차단은 보호한다. G-336의 공통 판단으로 독립성이 확인된 다른 작업의 미완료 rebase는 archive의 전제 조건으로 요구하지 않는다. 충돌 자원과 재개 조건을 남겨 해당 archive만 보류하며 자원 반환 뒤 문서 지문이 같아도 다음 안전 경계에서 재확인한다. 다른 작업의 worktree·index·후보·session·검증 근거를 변경하거나 호출을 선점하지 않는다.
- Requirement 식별 정보 교정은 source·draft·planning 예약·writer 보호를 그대로 유지한다. 교정이 실행 불가하다는 이유만으로 안전한 archive를 막지 않으며 같은 base 등 실제 공유 충돌은 직렬화한다. 교정 실패를 삼켜 모델 평가로 진행시키는 기존 금지는 유지한다.
- R-094 AC4–6: 실제 PR Merge와 SQLite MERGED가 확인된 후보만 최신 원격 detached worktree에서 최종 hash/revision·내용/mode를 보존한 rename-only commit으로 fast-forward 게시한다. 후보별 hash·경로·revision·일반 파일·목적지/동일 ID 충돌 검사를 유지해 문제 후보와 안전한 후보를 분리한다. 기본 checkout·활성 작업 공간을 편집하지 않는다.
- commit 준비·게시 직전 원격/후보/소유권 경쟁은 재관측하며 force push·정리 commit 자동 rebase를 하지 않는다. push 실패·push 직후 중단·catalog/SQLite 확정 누락에서는 실제 원격 이동과 보존 operation을 대조한다. 유효한 같은 준비 commit을 재사용하고 이미 게시된 이동은 새 commit/push 없이 확정한다. 불확실한 결과·낡은 후보·타 문서 덮어쓰기로 성공을 만들지 않는다.
- R-094 AC7: 진입 전을 포함한 실행별/후보별 보류의 범주·대상 자원·원인·재개 조건을 기존 DB/운영 로그에서 조회할 수 있다. 실행 기회·공유 자원·원격 관측·개별 후보·게시/확정 실패를 구분하고 동일 상태의 반복 출력은 억제하되 사유 변경·실제 시도 결과·완료는 보존한다. 사용자 조치 없는 Telegram은 0이다.
- R-094 AC8/R-074: archive 대기/실패는 MERGED·dependency·독립 배정·Planner·유지보수 종료·프로젝트 완료를 바꾸지 않는다. 내용 보존 이동으로 새 planning·작업 주기·Merge 수·완료 알림을 만들지 않는다. 별도 archive Agent·실행용 ExecPlan·PR·상주 스케줄러·명령을 추가하지 않고 기존 Manager 안에서 처리한다.

## Implementation

보존 후보 HEAD `5336b1dfd14bc2fd27b8f576c10e1f70b4019b8b`에는 아래 기존 항목의 구현과 targeted 기록이 있다. 기존 base 상태 설명/체크리스트는 원래 범위이며 재구현 지시가 아니다. 후보의 구현·실패/교정/검증 원문은 그 작업 공간의 같은 ExecPlan `Progress`에 보존하며 최신 base 조건과의 통합/잔여 검증만 이어간다.

- [x] 확인한 최신 `storage/management_admission.py`·`task_calls.py`·`application/task_scheduling.py`의 parked/인계·반환 경계에 후보의 archive 실행 기회를 통합한다. 관리 자리 반납을 물리 writer 종료로 간주하지 않고, 명시 요청의 우선 순서와 단위/실행 한도·planning/runtime drain·pause를 유지한다. archive는 관리 자리나 별도 실행용 G를 요구하지 않는다.
- [x] 확인한 `catalog/deleted_plan_cleanup.py`·`storage/deleted_plan_state.py`·`recovery/waiting_pr_service.py`의 CLOSED/삭제 예외와 archive 후보의 실제 MERGED 근거를 대조한다. 삭제 정리의 공유 자원 충돌만 보류하고, G-260/G-261의 기록/자원 제거를 archive 구현 안에서 수행하지 않는다. 최신 ARCHITECTURE의 관련 소유 절에는 후보의 기존 archive 변경과 실제 새 통합만 반영한다.

- [x] 확인한 `loop_manager_internal/catalog/archive_service.py`·`application/project_service.py`의 공통 gate를 archive 실행 자원 판단과 Requirement 교정의 기존 안전 판단으로 분리한다. `runtime/resource_lock.py`·`git/manager_git_journal.py`와 현재 게시 소유권을 재사용해 실제 충돌 자원만 검사·예약하고 준비/게시 직전에 다시 확인한다. 무관한 작업의 전체 Git snapshot이나 단순 phase를 쓰기 권한 근거로 삼지 않는다.
- [x] 확인한 `application/task_scheduling.py:run_project_tasks()`의 완료 수집·다음 배정 경계와 `ProjectService`의 기존 조율/PR 후속 경계에 archive 기회를 연결한다. G-297의 배정 중단·현재 자격 재확인 구조를 소비하고 별도 polling/상주 thread 없이 계속 배정되는 경우의 기아를 없앤다. runtime 종료 결정·사용자 pause·실제 프로젝트 차단을 우회하지 않으며 외부 조회/Git 대기 중 DB 접근을 유지하지 않는다.
- [x] 확인한 `catalog/execplan_archive.py`·`archive_git.py`·`storage/execplan_archive_state.py`의 후보 재발견·pending·prepared operation·원격 결과 확정을 재사용한다. 진입 전 실행 보류와 이유 전이를 기존 저장 경계에 최소 확장하고 변경된 catalog/generation은 짧은 DB 요청에서 재확인한다. 후보 기록이 없다는 이유로 MERGED 문서를 누락하지 않으며 이미 완료한 push를 저장 재시도 때문에 반복하지 않는다.
- [x] 확인한 `catalog/project_catalog_refresh.py`·`storage/planning_input_state.py`의 증명된 archive delta 적용을 유지한다. 실제 rename 검증 뒤 해당 문서만 이동 처리하고 unrelated planning 기준·동작 중 작업의 투영·PR 불변 링크·감사를 보존한다. PRODUCT의 사용자 승인/완료·archive 비차단 규칙과 ARCHITECTURE의 `### MERGED ExecPlan archive`·`### Requirement 교정 게시와 복구` 및 조율 설명을 실제 구현과 함께 통합한다.
- [x] 확인한 `tests/test_archive_housekeeping_adapter.py`·`tests/test_project_services.py`·`tests/test_task_scheduling.py`·`tests/test_pr_source_synchronization.py`의 fixture에 새 자원/기회 경계를 연결한다. 기존 `tests/test_execplan_archive.py`·`tests/test_execplan_archive_state.py`의 검증·재시작 회귀를 재사용하며 필요한 실제 효과만 기존 격리 Git fixture에 추가한다. 새 CLI나 G-194/G-195의 status 구현을 끌어들이지 않고 기존 저장 사유·로그를 조회 근거로 제공한다.

## Validation

- 후보의 기존 SQLite/scheduler/자원 guard fixture를 재사용해 명시 요청 수락→관리 단위 안전 반환/인계 중에도 독립 MERGED 처리 기회가 유지되는지 최소 연결로 확인한다. parked지만 물리 reservation/실제 충돌 lease가 남은 대상은 보류하며 다른 작업 단계·순서·후보/진단은 불변이어야 한다. admission 순서 엔진·삭제 엔진의 회귀는 담당 계획을 재사용하고 같은 lifecycle을 복제하지 않는다.
- 실제 MERGED·미Merge CLOSED·최종 삭제 표식/정리 pending을 같은 catalog/SQLite 입력으로 구별한다. MERGED 후보만 archive하고 타 작업 삭제의 실제 공유 base/journal 충돌은 보류하되 무관한 정리는 독립 진행해야 한다. 기존 FF/mode/중단·기본 checkout/index 보존 회귀를 유지한다.

- Worker는 변경한 자원 판단·조율·저장 경계의 최소 unit case만 실행한다. Worker/verify/Reviewer·Planner 자리·활성 유지보수·독립 PLAN_BLOCKED가 있어도 실제 충돌 없음이면 dispatch하며, 같은 base 게시·실제 충돌하는 미확정 journal·대상 소유/종료 불명 writer이면 보류해야 한다. 독립성이 확인된 별도 rebase의 intent·차단은 보존한 채 archive dispatch에 도달해야 한다. 자원 반환 뒤 기존 안전 경계에서 재시도하고 활성 파일/index·session·다른 operation이 불변인지 검사한다.
- `tests/test_task_scheduling.py`·`tests/test_project_services.py`에서 연속 신규 배정 사이의 이전 MERGED 처리, PR 후속 기회 누락, 후보 row 없는 진입 전 보류와 재시작을 모의 자원/실제 SQLite로 검사한다. 안전한 준비/결과 확정 중 경쟁과 DB 대기에도 독립 배정·기존 순서·runtime/planning drain이 유지돼야 한다. sleep이나 호스트 부하로 성공을 만들지 않는다.
- Requirement 교정이 source/draft/planning/writer gate로 보류돼도 독립 archive가 실행되는 경우와 실제 base 경합으로 둘 다 보존되는 경우를 분리한다. 교정 오류의 기존 중단/복구 의미·보존 intent를 유지하고 교정 보호 조건 자체를 느슨하게 하는 테스트 변경은 금지한다.
- `tests/test_execplan_archive.py`의 safe subset·base 재전진·준비 commit 재사용·이미 이동된 원격 무중복 확정과 state의 최종 blob/CAS 회귀를 유지한다. 실제 detached 이동·mode 보존·FF push·활성 worktree 불변은 `tests/test_pr_source_synchronization.py`의 기존 효과 경로를 최소 확장해 확인한다. push 직후 중단 및 DB 확정 누락 뒤 commit/push 호출 0도 실제 원격 관측과 연결한다. 정책 조합마다 저장소·게시 전체를 반복하지 않는다.
- 보류 범주/재개 조건·사유 전이의 내구성, 동일 사유 반복 출력 억제, 실제 재시도 결과 보존과 Telegram 0을 검사한다. 기존 전체 유휴만을 요구하는 archive assertion은 새 자원 조건으로 교체하되 실제 프로젝트 안전 차단·Requirement 교정 보호·사용자 자료 보존 회귀는 남긴다. archive만으로 planning/작업 주기/실제 Merge 집계/완료 통지나 다음 작업의 실패를 만들면 실패다.

## Progress

- [x] revision `296da23b69e3bc76df27264eeb978c46d7caa100`의 R-094 전문과 archive service·dispatcher·Git/state·조율·관련 테스트를 정적으로 대조했다. 기존 내용 보존/복구 구현은 재사용하며 R-094 전체는 미충족이다. 구현·테스트·verify는 실행하지 않았다.
- [x] 자원 판단 분리·보류 관측 연결: 자원 기반 archive guard와 별도 교정 gate, 실제 base 예약·대상 writer/lease·G-336 판단을 연결했다.
- [x] 안전 경계의 실행 기회·재발견·자동 재시도: 완료 수집·연속 배정 경계에서 MERGED DB 후보를 재발견하고 자원 보류·DB 대기를 독립 배정 실패로 바꾸지 않는다.
- [x] 기존 게시/복구 계약과 최소 회귀·문서 정합성: Git 관측/DB CAS 분리, 실제 게시 후 확정 중단 복구·mode/index 보존과 설계 문서를 반영했다.

### 최종 구현과 검증 후보

자원 기반 실행·조율 기회·보류 관측과 결과 확정 경계를 연결했다. 아래 세 준비 보존점의 미완료 설명은 당시 기록이며 현재 남은 구현 목록이 아니다. `prepare_catalog`는 열린 archive fetch lease 아래에서 DB 요청 밖의 rename-only diff·ref와 catalog snapshot을 관측하고, 성공 transaction에는 lease·snapshot을 재확인하는 로컬 CAS만 전달한다. 기존 소비자의 기본 revision callback·경쟁 거부와 원자적 성공/catalog/planning delta 적용은 유지한다.

실제 로컬 Git fixture에서 executable mode `100755`의 문서를 게시한 직후 DB 확정을 중단시켰다. 다음 archive 호출은 원격 결과를 확인해 item을 확정했고 prepare/push 호출은 모두 0이었다. 기본 checkout HEAD, 기존 작업 worktree HEAD·staged tree·dirty/untracked 내용은 모두 그대로였다. 기존 실제 게시·source/catalog 동기화 회귀도 통과했다. 실제 Agent CLI/API는 실행하지 않았다.

이번 최종 단계의 targeted 실행은 DB 밖 관측/transaction 내 CAS·실패 rollback 및 기존 catalog 경쟁의 4개 case, 실제 게시/복구 효과, 독립 rebase의 intent 보존과 반복 판단 로그 억제·실제 충돌 거부를 확인했다. 기존 대기 loop의 로컬 자원 반환 관측을 연결하고, 완료/배정 이벤트 없이 base lease만 반환돼도 실행 중 Worker 종료 전에 이전 MERGED를 처리하는 경로를 확인했다. 처리 중 새 MERGED 전이를 다음 시도에서 누락하지 않도록 시도 전 token만 저장하고 배정 전에 변화 여부를 재확인한다. 공통 journal의 안전 판단은 매번 수행하며 archive 관측에 같은 판단을 저장했을 때만 중복 출력한다. 일반 소비자의 기본 보고 동작은 유지한다. 기록은 각 호출의 Manager targeted 결과 폴더에 남겼다. 기존 테스트 삭제·skip·assertion 약화는 없으며 기존 lifecycle 테스트는 교정 전용 gate의 보호를 계속 검증한다.

PRODUCT의 MERGED archive 보장과 ARCHITECTURE의 실행 기회·lease·보류 저장 계약·교정 분리·DB 결과 확정을 실제 구현에 맞춰 통합했다. 변경 코드·테스트는 기존 `scripts/verify-inputs.json`의 `loop_manager_internal/**`, `tests/**` 범위에 포함되며 새 고정 입력은 추가하지 않았다. 목록 밖 경로 전체의 비소비 identity는 선언하지 않는다. 전체 suite/verify·Reviewer·Merge는 Worker가 수행하지 않았으며 Manager 검증으로 인계한다.

### 교정 전용 진입 점유의 추가 대조

복구점 뒤 변경과 R-094를 다시 대조해, lifecycle gate가 열려 있어도 이미 존재하는 Requirement 문서 draft·종료 미확인 writer를 교정 내부의 실행 오류로 받아 독립 archive까지 막는 경로를 확인했다. 이전 검증 후보의 교정 gate 분리는 이 조건까지 충족하지 못했다.

ProjectService의 교정 진입 판단은 기존 lifecycle 보호에 더해 수동 source·planning 문서 예약·현재 catalog에 있는 Requirement draft·종료 미확인 writer를 확인한다. writer 조회는 기존 교정 실행 중 검사를 helper로 추출해 재사용하므로 종료/미실행 판정이 달라지지 않는다. 이미 알려진 점유는 교정만 진입하지 않으며, 실제 base 게시 예약·대상 writer·공유 Git 충돌은 archive의 별도 guard에서 계속 보호한다. 진입 뒤 gate 변화나 조회/게시 실패는 기존 `CorrectionPending`으로 전파하고 오류를 삼켜 archive나 Planner로 진행하지 않는다.

실제 SQLite·교정 publisher·ProjectService와 archive 자원 guard를 연결한 최소 case에서 draft와 무관한 종료 미확인 writer가 각각 독립 archive dispatch에 도달하는지 확인했다. 교정 fetch/commit/push는 0이고 draft 및 writer 증거는 보존한다. 기존 writer 판정·진입 후 gate 닫힘·교정 오류 전파를 함께 실행해 4 tests가 통과했다. 추가한 보존 assertion은 해당 새 case만 재실행해 확인했고, 이후 기존 실제 Git archive 게시·source/catalog 적용 회귀 1개도 통과했다. 전체 verify는 실행하지 않았다.

### 최종 조율 경쟁 재검증

자원 반환 뒤 재시도를 완료/새 배정 이벤트에만 의존하지 않도록 기존 loop에서 로컬 상태 token을 관측한다. 새 thread·원격 polling은 없으며 token은 안전 판단을 생략하는 근거가 아니다. 후보 수에 비례하는 조회의 상한과 storage change notification으로의 전환 조건은 해당 코드의 ponytail 주석에 남겼다.

첫 실행에서 archive 처리 도중 G-001이 MERGED로 바뀐 관측을 이미 시도한 token으로 저장해, archive 전에 G-002를 배정하는 assertion 진단을 발견했다. 테스트 내부 scheduler가 이 오류를 재시도한 뒤 조건 대기에 들어가 실행을 중단했다(exit 130). unittest testcase 종료 결과는 없으므로 확정된 테스트 실패 횟수로 합산하지 않는다. 진단과 중단 기록을 보존했다. 시도 전 token만 기억하고 처리 중 변화가 있으면 다음 배정 전에 재검사하도록 수정했으며, fixture는 실제 작업 Future 종료를 사용해 안전하게 처리 가능한 후보의 배정 순서를 검증한다. 오류 경로의 무한 대기는 fixture에서 차단했다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_task_scheduling.TaskSchedulingTests.test_archive_runs_between_continuous_admissions_without_candidate_records tests.test_task_scheduling.TaskSchedulingTests.test_archive_discovery_db_wait_does_not_prevent_independent_admission

RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_task_scheduling.TaskSchedulingTests.test_archive_runs_between_continuous_admissions_without_candidate_records tests.test_task_scheduling.TaskSchedulingTests.test_archive_discovery_db_wait_does_not_prevent_independent_admission

재실행 결과: 2 tests, OK. lease 반환 전 진입 보류, 반환 후 실행 중 Worker와 독립된 archive 처리, 중간 MERGED 전이의 재관측, 이후 연속 배정과 DB 대기 비차단을 확인했다. 중단 실행의 assertion 진단을 통과로 기록하지 않았다.

### 안전 판단 분리의 준비 보존점

`archive_service`는 선택적인 `correction_safety`를 받아 Requirement 교정의 안전 판단과 archive의 판단을 따로 전달할 수 있다. 기존 호출은 같은 gate를 유지한다. 교정의 catalog refresh는 교정 자신의 source/draft/writer 판단을 사용하며, archive gate로 교정의 실행 가능 여부를 대신 판단하지 않는다. 교정 실패의 `CorrectionPending` 전파는 유지한다.

archive dispatcher는 catalog refresh 이후뿐 아니라 실제 commit 준비 직전과 push 직전에도 `execution_safety`를 확인한다. 닫힌 경계는 기존 operation 저장소에 `execution_deferred`와 단계별 사유를 기록한다. push 전 보류는 준비 commit·tree를 보존하고 gate가 다시 열리면 같은 commit을 재사용한다. 아직 프로젝트 유휴 gate를 해제하거나 새로운 병렬 게시를 허용하지 않았다.

프로젝트 Python `python3`와 `LOOP_MANAGER_TARGETED_TEST_RUNNER --unittest`로 아래 개별 case만 실행했다. 첫 실행 4개, 두 번째 실행 5개가 모두 통과했으며 전체 verify·통합 suite는 실행하지 않았다. 실행 기록은 Manager가 제공한 `LOOP_MANAGER_TARGETED_TEST_DIRECTORY`에 남겼다.

- `tests.test_execplan_archive.ExecPlanArchiveTest`: `test_execution_safety_is_rechecked_before_commit`, `test_execution_safety_deferral_retains_prepared_commit_for_retry`, `test_prepared_commit_is_reused_after_push_failure`, `test_remote_archive_after_interruption_converges_without_new_commit`.
- `tests.test_requirement_correction_publication.RequirementCorrectionPublicationTest`: `test_correction_deferral_does_not_close_independent_archive_safety`, `test_connection_dispatches_correction_before_archive_without_model`, `test_archive_safety_does_not_replace_correction_refresh_gate`, `test_project_boundary_does_not_swallow_correction_failure`.
- `tests.test_project_catalog_refresh.CatalogArchiveConsumerTests.test_archive_refresh_rechecks_safety_before_preparing_or_publishing`.

다음 작업은 ProjectService의 실제 자원 판단·lease와 진입 전 보류 관측, 연속 배정 중 archive 기회, 반복 로그 억제 및 PRODUCT·ARCHITECTURE 갱신이다. 이 보존점은 R-094나 G-321의 구현 완료 후보가 아니다.

### 보류 관측의 준비 보존점

`execplan_archive_observations`는 `(repo, scope)`별 현재 범주·대상 자원·사유·재개 조건과 실제 dispatch 시도 누계를 보존한다. `scope=dispatch`는 후보 row가 없는 진입 전 보류도 기록하며, 후보 ID scope는 개별 보류와 완료 전이를 기록한다. 같은 관측을 반복하면 로그를 생략하지만 실제 시도 누계는 갱신한다. 사유 변경·완료 이후 같은 사유의 재발은 다시 출력한다. 이 관측은 준비 operation·후보 row·MERGED나 알림 상태를 변경하지 않는다.

ProjectService의 기존 lifecycle/journal 진입 전 반환과 archive 예외를 저장 경계에 연결했다. 관측 DB 처리가 실패하면 사유·자원·재개 조건과 기록 미확정 사실을 운영 로그에 남기고 독립 처리를 계속한다. Requirement 교정 실패의 기존 전파는 유지한다. dispatcher의 반복 pending 출력은 관측 비교로 억제하고 원격에서 이미 완료된 이동도 완료 확인을 출력한다. 자원 gate 전환·연속 배정 기회와 최종 설계 문서 갱신은 아직 남아 있다.

최소 unit 실행은 재시작/후보 row 부재, 준비 operation 불변, 진입 전 사유 전이, 후보 로그 억제·시도 누계·완료 전이의 4개 case가 통과했다. 이어서 로그 변경과 DB 오류 처리, 교정 실패 전파, 충돌 rebase 보류와 독립 rebase 허용의 5개 case를 실행했다. DB 오류 테스트의 Mock 연결이 schema 조회를 지원하지 않아 1개 오류가 발생했고, 실제 격리 SQLite 연결에서 오류를 주입하도록 수정했다. 같은 명령 재실행은 5개 모두 통과했다. 전체 verify·suite는 실행하지 않았다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_archive_housekeeping_adapter.ExecPlanArchiveHousekeepingAdapterTest.test_candidate_log_suppression_keeps_attempt_count_and_completion_transition tests.test_archive_housekeeping_adapter.ExecPlanArchiveHousekeepingAdapterTest.test_deferral_storage_failure_keeps_archive_non_gating tests.test_requirement_correction_publication.RequirementCorrectionPublicationTest.test_project_boundary_does_not_swallow_correction_failure tests.test_requirement_correction_publication.RequirementCorrectionPublicationTest.test_pending_rebase_defers_housekeeping_and_reaches_execution_recovery tests.test_requirement_correction_publication.RequirementCorrectionPublicationTest.test_housekeeping_dispatches_with_an_independent_rebase_and_keeps_safety_gate

RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_archive_housekeeping_adapter.ExecPlanArchiveHousekeepingAdapterTest.test_candidate_log_suppression_keeps_attempt_count_and_completion_transition tests.test_archive_housekeeping_adapter.ExecPlanArchiveHousekeepingAdapterTest.test_deferral_storage_failure_keeps_archive_non_gating tests.test_requirement_correction_publication.RequirementCorrectionPublicationTest.test_project_boundary_does_not_swallow_correction_failure tests.test_requirement_correction_publication.RequirementCorrectionPublicationTest.test_pending_rebase_defers_housekeeping_and_reaches_execution_recovery tests.test_requirement_correction_publication.RequirementCorrectionPublicationTest.test_housekeeping_dispatches_with_an_independent_rebase_and_keeps_safety_gate

재실행 결과: 5 tests, OK. 기록 경로는 이번 호출의 `LOOP_MANAGER_TARGETED_TEST_DIRECTORY`이며 기존 실패 기록을 성공으로 대체하지 않았다.

### 자원 기반 실행·연속 배정 연결 보존점

`ArchiveExecutionResources`가 같은 base 게시·fetch·공유 설정과 후보별 plan/branch/worktree의 기존 lease를 dispatch 수명 동안 유지한다. archive는 기다리지 않는 lease 요청을 사용하며, 같은 thread의 다른 소유자 lease를 자기 권한으로 수락하지 않는다. 기록된 fetch만 자신이 확보한 열린 archive fetch lease를 재사용한다. G-336의 별도 rebase 판단에는 같은 ExitStack을 전달해 독립성 확인 자원을 게시까지 보존한다.

프로젝트 안전 차단·base 변경·일반/유지보수/교정 게시 예약·실제 writer/lease 경합은 보류한다. 단순 Worker/verify/Reviewer·Planner 자리·유지보수 cycle·독립 PLAN_BLOCKED는 archive gate가 아니다. 교정은 기존 lifecycle/source/draft/planning/writer gate를 그대로 사용하고 교정 실패는 계속 전파한다. 수동 소유·종료 불명 writer 후보는 다른 안전한 후보와 분리하며 원본 실행 row와 journal을 수정하지 않는다. 초기 관측 전과 commit/push 직전의 재확인에 같은 guard를 사용한다.

`run_project_tasks`는 진입·완료 수집·새 배정 다음 경계에 archive 기회를 둔다. 후보가 없어도 DB MERGED에서 다시 찾고, 대기 loop 자체로 원격 조회를 반복하지 않는다. scheduling 경계는 교정을 새로 시도하지 않으며 planning drain·runtime handoff·사용자 pause를 지킨다. archive 뒤 새 배정 전에 기존 planning 입력 재확인을 수행한다. 후보 조회 DB 대기는 저장/로그에 남기고 독립 READY 배정을 계속한다.

이번 호출의 최소 실행 결과는 다음과 같다. 모든 명령은 프로젝트 `python3`와 `LOOP_MANAGER_TARGETED_TEST_RUNNER --unittest`를 사용했고 전체 suite/verify는 실행하지 않았다.

- 진입 전 사유/DB 기록 실패·교정 실패 전파·충돌/독립 rebase의 5개 case 통과.
- 자원 guard 4개와 기존 commit/push 재확인·catalog 안전 gate의 3개 case 실행에서 유지보수 게시 예약 테스트 1개 실패. fixture의 `get()`은 없는 cycle을 생성하지 않으므로 실제 cycle row를 삽입하도록 고쳤고 같은 7개 case 재실행은 모두 통과했다.
- 후보 row 없는 연속 배정 사이 archive 순서와 기존 독립 Worker/직렬 verify의 2개 case 통과.
- 대상 종료 불명 writer 보존/미실행 확인, 후보별 안전 subset, 기존 실제 detached archive·FF 게시·catalog 적용 효과의 3개 case 통과. 실제 Agent는 호출하지 않았다.
- planning drain·fetch lease 소유권과 catalog/PR/archive 순서 4개 case 통과. 첫 명령은 catalog case의 class 이름을 잘못 지정해 수집이 거부됐으며 본문 테스트 실패가 아니다. 실제 class `ProjectCatalogConsumerTests`로 수정한 명령에서 4개 모두 실행했다.
- 후보 조회 DB 대기 뒤 독립 배정, base lease 충돌 비대기·반환 뒤 재시도의 2개 case 통과.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_archive_housekeeping_adapter.ArchiveExecutionResourcesTest tests.test_execplan_archive.ExecPlanArchiveTest.test_execution_safety_is_rechecked_before_commit tests.test_execplan_archive.ExecPlanArchiveTest.test_execution_safety_deferral_retains_prepared_commit_for_retry tests.test_project_catalog_refresh.CatalogArchiveConsumerTests.test_archive_refresh_rechecks_safety_before_preparing_or_publishing

RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_archive_housekeeping_adapter.ArchiveExecutionResourcesTest tests.test_execplan_archive.ExecPlanArchiveTest.test_execution_safety_is_rechecked_before_commit tests.test_execplan_archive.ExecPlanArchiveTest.test_execution_safety_deferral_retains_prepared_commit_for_retry tests.test_project_catalog_refresh.CatalogArchiveConsumerTests.test_archive_refresh_rechecks_safety_before_preparing_or_publishing

재실행 결과: 7 tests, OK. 이후 추가한 대상 writer·drain·fetch case는 별도의 최소 명령으로 실행했다. 실행 기록은 이번 호출의 `LOOP_MANAGER_TARGETED_TEST_DIRECTORY`에 남겼다.

남은 작업은 catalog 결과 확정 callback의 기존 Git 관측을 짧은 DB transaction 밖으로 분리하는 검토/교정, 실제 push 후 DB 확정 누락 복구와 mode·활성 작업 공간 불변의 필요한 회귀 보완, PRODUCT·ARCHITECTURE의 최종 통합 및 최종 diff/검증 입력 검토다. 이 보존점은 완료 후보가 아니다.

### 최신 delta의 미충족 통합

보존 후보 HEAD `5336b1dfd14bc2fd27b8f576c10e1f70b4019b8b`의 clean 상태와 위 Progress 원문·archive/scheduler 연결을 읽기 전용으로 확인했다. 위 기존 수행/실패/검증 기록과 Implementation의 완료 표시는 후보에서 그대로 보존한 기록이다. 후보 파일·Git·실행 이력은 수정하지 않았으며 최신 조건의 성공으로 승계하지 않는다.

- [x] 최신 관리 인계/삭제 예외와 보존 후보 통합·관련 최소 회귀
- [ ] 최신 후보의 필요한 Manager verify·Review·사용자 Merge


### 관리 rebase 뒤 최신 조건 통합과 verify 실패 교정

호출 시작 HEAD `78531e5cac73f48d4c646f9a5f10e980bb28c09b`와 base `1829086bc5a8caa02ce073061af87cf7e584f3d0` 사이의 최종 diff 전체, 최신 ExecPlan·sources와 현재 구현을 다시 대조했다. 이전 후보의 성공을 현재 후보 성공으로 승계하지 않았다. Manager verify 피드백 `verify:e69ffb3b612c0d5b26911a223b1e3362fd564ba63aa86a51b8d085eb367d530e` occurrence=1 revision=1의 두 실패를 현재 코드에서 재현했다.

첫 실패는 active assessment가 최초 workspace를 만들기 전에 독립 archive가 base를 전진시켜 cycle 기준 SHA가 남은 것이었다. 게시 intent·평가 결과·완료·보존 workspace가 없는 초기 assessment만 기존 intent-free context 재시작을 사용해 같은 cycle/단계에서 최신 base로 준비한다. 저장된 평가·workspace·게시 복구를 우회하지 않는다. 두 번째는 archive 이전 SHA를 종료 기준으로 고정한 assertion이었다. 실제 Git의 내용 보존 archive-only 이동을 검증한 뒤 종료 기준을 현재 원격 SHA와 비교하고 실제 Merge 수가 1인 assertion을 추가했다. 기존 역할 호출·선택 순서·idle 종료 검증은 유지했다.

FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_maintenance_cycle_publication.MaintenanceCycleTest.test_published_combined_batch_recovers_before_changed_fingerprint_cleanup tests.test_maintenance_cycle_scheduling.MaintenanceCycleTest.test_prerequisite_merge_returns_to_outside_ready_without_reassessment`
결과: 2개 실행, 2개 failure. 게시 intent 저장 전에 평가 기준 불일치와 archive 이전 종료 SHA assertion을 확인했다.

RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_maintenance_cycle_publication.MaintenanceCycleTest.test_published_combined_batch_recovers_before_changed_fingerprint_cleanup tests.test_maintenance_cycle_scheduling.MaintenanceCycleTest.test_prerequisite_merge_returns_to_outside_ready_without_reassessment`
결과: 수정 후 동일 2개 모두 통과. 첫 case는 의도한 push 후 SQLite 중단까지 도달하고 보존된 prepared batch를 입력 지문 정리 전에 복구한다.

관리 admission의 명시 요청 수락·live reservation 반환 테스트에 기존 archive 경계를 연결했다. 요청이 parked인 동안과 앞선 물리 호출 반환 후 모두 독립 MERGED 후보를 처리하며 기존 배정 우선순위·한도를 보존한다. 별도 guard case는 parked 대상의 running/불확실 영수증에는 해당 후보만 보류하고 stopped 영수증 확인 뒤 같은 예약·admission을 보존한 채 재개함을 확인했다. CLOSED 미Merge와 삭제 ID·정리 파일 pending은 후보에 들어가지 않고 독립 MERGED는 허용하되 실제 공유 base lease에는 보류한다. 삭제·admission 엔진은 변경하지 않았고 기존 lifecycle을 복제하지 않았다.

현재 후보에서 targeted runner로 새로 확인한 최소 case는 위 실패 교정 2개, parked/삭제 자원 guard 2개, 명시 요청 반환/연속 배정 archive 기회 2개, 실제 Git push 후 DB 확정 중단 복구·mode/작업 공간 보존 1개, transaction 밖 catalog 준비와 stale CAS rollback 1개, 교정 draft/writer 점유 중 독립 archive 1개, 기존 유지보수 push 실패 뒤 저장 결과로 재계획 없이 재개하는 1개로 총 10개다. 모두 통과했으며 기록은 이번 호출의 `LOOP_MANAGER_TARGETED_TEST_DIRECTORY`에 남겼다. 전체 verify는 Worker가 실행하지 않았으며 Manager의 재검증이 남아 있다. 테스트 삭제·skip은 없고 폐기된 archive 이전 SHA assertion만 현재 native 효과와 Merge 수 검증으로 교체했다.


### 결과 보고 보완과 현재 후보 재확인

결과 검증 거부는 이전 최종 응답의 TARGETED_TESTS에서 최초 실패 설명이 FAILED_COMMAND 앞에 위치하고, FAILED_COMMAND와 RERUN 사이에는 최초 결과가 없어서 발생했다. `execution/result_validation.py`의 기존 계약과 `tests/test_verify_receipts.py`의 보고 예제를 대조했다. 결과 검증기는 변경하지 않는다. 이전 호출의 실제 두 실패와 동일 명령 재실행 성공은 위 원문에 보존하고, 최종 보고에서도 명령 → 최초 결과 → 동일 명령 재실행 → 현재 결과 순서로 제출한다.

최신 ExecPlan·R-094와 근거 입력, HEAD/base의 전체 변경 범위를 다시 대조했다. 현재 호출에서 위 동일 targeted 명령을 새로 실행해 2 tests, OK (9.771s, exit 0)를 확인했다. 기록은 `call-fljf77gh`의 `LOOP_MANAGER_TARGETED_TEST_DIRECTORY`에 남겼다. 이전 호출의 10개 성공 기록을 이번 실행으로 합성하지 않는다. 전체 verify는 Manager 재실행이 필요하다.

## Decision Log

- 이번 delta는 G-321을 초기화하거나 완료한 구현을 중복 계획하지 않는다. 앞의 후보에 보존된 구현/검증 원문과 과거 준비점은 유지하고 현재 원격의 관리 인계·삭제 정책과 만나는 경계만 미충족 통합으로 구분한다. started ID·경로·기존 dependency·metadata를 보존한다. 후보 확인·기존 targeted 기록은 최신 전체 verify/Review 성공·Merge가 아니다.

- R-055 B01/B02/B06/B08의 공통 journal 판단은 G-336을 선행 소비한다. 이 작업은 `_archive_housekeeping_safe()`·Requirement 교정과의 gate 분리·연속 배정 중 기회·진입 전 사유와 재발견을 계속 소유한다. 공통 helper의 허용만으로 archive dispatch 전체를 완료로 간주하지 않으며 실행 기회/자원 보류도 G-325의 정체 관측에서 빠지지 않도록 기존 구조화 결과를 제공한다.

- R-094는 기존 archive의 검증/게시 알고리즘을 새로 만드는 목표가 아니다. 실행 기회와 자원 판단·보류 기록은 같은 dispatch 수명에서 검증돼야 하므로 한 작업으로 묶는다. G-297의 조율 경계를 먼저 소비하며 기존 catalog의 R-055 전체 감사는 이 작업의 실제 Merge 뒤 후속 소비만 확인한다.
- complexity high: 진행 중 독립 작업과 실제 공유 쓰기·교정 게시를 구분하고 실행 기회·저장 이전 보류·게시 후 복구를 일관되게 연결해야 한다. risk high: 잘못된 자원 판정이 다른 writer의 index/operation이나 현재 catalog를 손상시키면 보존한 Git/DB 근거로 수동 복구가 필요할 수 있다. 대상별 소유권·기존 lease·FF-only·관측/CAS·활성 작업 불변 및 중단 회귀로 완화한다. priority normal은 완료 문서 정리의 필수 보완이며 구현·승인 자체를 막는 작업보다 우선하지 않는다. 최초 level은 5이며 Model·Effort는 Manager 설정을 따른다.
