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
complexity: high
risk: high
priority: normal
---

## Purpose

실제 MERGED ExecPlan archive를 프로젝트 전체 유휴 gate에서 분리하고 기존 Manager 조율의 안전 경계에서 처리·재발견·재시도한다. 후보 검증·내용/mode 보존 이동·fast-forward 게시·준비 commit 재사용은 기존 dispatcher와 Git adapter를 유지하며 실행 자원 판단·기회·보류 관측의 미충족 범위만 보완한다.

현재 `archive_service._execplan_archive_housekeeping_safe()`는 project phase·planning 자리·활성 작업·Agent scope·유지보수 cycle로 반환한다. 호출은 `ProjectService._synchronize_source_after_pr_reconciliation()`에 있고 `run_project_tasks()`의 연속 배정 중 기회는 없다. gate 진입 전 반환은 archive 사유를 남기지 않으며 candidate pending 로그는 매 dispatch 반복 출력한다. R-094는 이 두 경계를 바꾸되 함께 실행하는 Requirement 교정의 기존 보호는 유지하도록 요구한다.

## Acceptance Criteria

- R-094 AC1–3: Worker·verify·Reviewer 실행, 독립 차단 계획·Planner 자리·유지보수 cycle만으로 archive를 미루지 않는다. 실제 충돌이 없으면 연속 작업 배정 중에도 기존 안전 경계에서 MERGED 후보를 처리한다. PR 동기화 직후 미처리·후보 기록 없이 진입 전 보류·Manager 재시작에도 DB MERGED와 최신 원격 catalog로 자동 재발견한다.
- 같은 base 게시·미확정 Git 쓰기·대상 자원의 수동 소유·실행/종료 불명 writer와 실제 프로젝트 안전 차단은 보호한다. 충돌 자원과 재개 조건을 남겨 해당 archive만 보류하며 자원 반환 뒤 다음 안전 경계에서 재확인한다. 다른 작업의 worktree·index·후보·session·검증 근거를 변경하거나 호출을 선점하지 않는다.
- Requirement 식별 정보 교정은 source·draft·planning 예약·writer 보호를 그대로 유지한다. 교정이 실행 불가하다는 이유만으로 안전한 archive를 막지 않으며 같은 base 등 실제 공유 충돌은 직렬화한다. 교정 실패를 삼켜 모델 평가로 진행시키는 기존 금지는 유지한다.
- R-094 AC4–6: 실제 PR Merge와 SQLite MERGED가 확인된 후보만 최신 원격 detached worktree에서 최종 hash/revision·내용/mode를 보존한 rename-only commit으로 fast-forward 게시한다. 후보별 hash·경로·revision·일반 파일·목적지/동일 ID 충돌 검사를 유지해 문제 후보와 안전한 후보를 분리한다. 기본 checkout·활성 작업 공간을 편집하지 않는다.
- commit 준비·게시 직전 원격/후보/소유권 경쟁은 재관측하며 force push·정리 commit 자동 rebase를 하지 않는다. push 실패·push 직후 중단·catalog/SQLite 확정 누락에서는 실제 원격 이동과 보존 operation을 대조한다. 유효한 같은 준비 commit을 재사용하고 이미 게시된 이동은 새 commit/push 없이 확정한다. 불확실한 결과·낡은 후보·타 문서 덮어쓰기로 성공을 만들지 않는다.
- R-094 AC7: 진입 전을 포함한 실행별/후보별 보류의 범주·대상 자원·원인·재개 조건을 기존 DB/운영 로그에서 조회할 수 있다. 실행 기회·공유 자원·원격 관측·개별 후보·게시/확정 실패를 구분하고 동일 상태의 반복 출력은 억제하되 사유 변경·실제 시도 결과·완료는 보존한다. 사용자 조치 없는 Telegram은 0이다.
- R-094 AC8/R-074: archive 대기/실패는 MERGED·dependency·독립 배정·Planner·유지보수 종료·프로젝트 완료를 바꾸지 않는다. 내용 보존 이동으로 새 planning·작업 주기·Merge 수·완료 알림을 만들지 않는다. 별도 archive Agent·실행용 ExecPlan·PR·상주 스케줄러·명령을 추가하지 않고 기존 Manager 안에서 처리한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/catalog/archive_service.py`·`application/project_service.py`의 공통 gate를 archive 실행 자원 판단과 Requirement 교정의 기존 안전 판단으로 분리한다. `runtime/resource_lock.py`·`git/manager_git_journal.py`와 현재 게시 소유권을 재사용해 실제 충돌 자원만 검사·예약하고 준비/게시 직전에 다시 확인한다. 무관한 작업의 전체 Git snapshot이나 단순 phase를 쓰기 권한 근거로 삼지 않는다.
- [ ] 확인한 `application/task_scheduling.py:run_project_tasks()`의 완료 수집·다음 배정 경계와 `ProjectService`의 기존 조율/PR 후속 경계에 archive 기회를 연결한다. G-297의 배정 중단·현재 자격 재확인 구조를 소비하고 별도 polling/상주 thread 없이 계속 배정되는 경우의 기아를 없앤다. runtime 종료 결정·사용자 pause·실제 프로젝트 차단을 우회하지 않으며 외부 조회/Git 대기 중 DB 접근을 유지하지 않는다.
- [ ] 확인한 `catalog/execplan_archive.py`·`archive_git.py`·`storage/execplan_archive_state.py`의 후보 재발견·pending·prepared operation·원격 결과 확정을 재사용한다. 진입 전 실행 보류와 이유 전이를 기존 저장 경계에 최소 확장하고 변경된 catalog/generation은 짧은 DB 요청에서 재확인한다. 후보 기록이 없다는 이유로 MERGED 문서를 누락하지 않으며 이미 완료한 push를 저장 재시도 때문에 반복하지 않는다.
- [ ] 확인한 `catalog/project_catalog_refresh.py`·`storage/planning_input_state.py`의 증명된 archive delta 적용을 유지한다. 실제 rename 검증 뒤 해당 문서만 이동 처리하고 unrelated planning 기준·동작 중 작업의 투영·PR 불변 링크·감사를 보존한다. PRODUCT의 사용자 승인/완료·archive 비차단 규칙과 ARCHITECTURE의 `### MERGED ExecPlan archive`·`### Requirement 교정 게시와 복구` 및 조율 설명을 실제 구현과 함께 통합한다.
- [ ] 확인한 `tests/test_archive_housekeeping_adapter.py`·`tests/test_project_services.py`·`tests/test_task_scheduling.py`·`tests/test_pr_source_synchronization.py`의 fixture에 새 자원/기회 경계를 연결한다. 기존 `tests/test_execplan_archive.py`·`tests/test_execplan_archive_state.py`의 검증·재시작 회귀를 재사용하며 필요한 실제 효과만 기존 격리 Git fixture에 추가한다. 새 CLI나 G-194/G-195의 status 구현을 끌어들이지 않고 기존 저장 사유·로그를 조회 근거로 제공한다.

## Validation

- Worker는 변경한 자원 판단·조율·저장 경계의 최소 unit case만 실행한다. Worker/verify/Reviewer·Planner 자리·활성 유지보수·독립 PLAN_BLOCKED가 있어도 실제 충돌 없음이면 dispatch하며, 같은 base 게시·미확정 journal·대상 소유/종료 불명 writer이면 보류해야 한다. 자원 반환 뒤 기존 안전 경계에서 재시도하고 활성 파일/index·session·다른 operation이 불변인지 검사한다.
- `tests/test_task_scheduling.py`·`tests/test_project_services.py`에서 연속 신규 배정 사이의 이전 MERGED 처리, PR 후속 기회 누락, 후보 row 없는 진입 전 보류와 재시작을 모의 자원/실제 SQLite로 검사한다. 안전한 준비/결과 확정 중 경쟁과 DB 대기에도 독립 배정·기존 순서·runtime/planning drain이 유지돼야 한다. sleep이나 호스트 부하로 성공을 만들지 않는다.
- Requirement 교정이 source/draft/planning/writer gate로 보류돼도 독립 archive가 실행되는 경우와 실제 base 경합으로 둘 다 보존되는 경우를 분리한다. 교정 오류의 기존 중단/복구 의미·보존 intent를 유지하고 교정 보호 조건 자체를 느슨하게 하는 테스트 변경은 금지한다.
- `tests/test_execplan_archive.py`의 safe subset·base 재전진·준비 commit 재사용·이미 이동된 원격 무중복 확정과 state의 최종 blob/CAS 회귀를 유지한다. 실제 detached 이동·mode 보존·FF push·활성 worktree 불변은 `tests/test_pr_source_synchronization.py`의 기존 효과 경로를 최소 확장해 확인한다. push 직후 중단 및 DB 확정 누락 뒤 commit/push 호출 0도 실제 원격 관측과 연결한다. 정책 조합마다 저장소·게시 전체를 반복하지 않는다.
- 보류 범주/재개 조건·사유 전이의 내구성, 동일 사유 반복 출력 억제, 실제 재시도 결과 보존과 Telegram 0을 검사한다. 기존 전체 유휴만을 요구하는 archive assertion은 새 자원 조건으로 교체하되 실제 프로젝트 안전 차단·Requirement 교정 보호·사용자 자료 보존 회귀는 남긴다. archive만으로 planning/작업 주기/실제 Merge 집계/완료 통지나 다음 작업의 실패를 만들면 실패다.

## Progress

- [x] revision `296da23b69e3bc76df27264eeb978c46d7caa100`의 R-094 전문과 archive service·dispatcher·Git/state·조율·관련 테스트를 정적으로 대조했다. 기존 내용 보존/복구 구현은 재사용하며 R-094 전체는 미충족이다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 자원 판단 분리·보류 관측 연결
- [ ] 안전 경계의 실행 기회·재발견·자동 재시도
- [ ] 기존 게시/복구 계약과 최소 회귀·문서 정합성

## Decision Log

- R-094는 기존 archive의 검증/게시 알고리즘을 새로 만드는 목표가 아니다. 실행 기회와 자원 판단·보류 기록은 같은 dispatch 수명에서 검증돼야 하므로 한 작업으로 묶는다. G-297의 조율 경계를 먼저 소비하며 기존 catalog의 R-055 전체 감사는 이 작업의 실제 Merge 뒤 후속 소비만 확인한다.
- complexity high: 진행 중 독립 작업과 실제 공유 쓰기·교정 게시를 구분하고 실행 기회·저장 이전 보류·게시 후 복구를 일관되게 연결해야 한다. risk high: 잘못된 자원 판정이 다른 writer의 index/operation이나 현재 catalog를 손상시키면 보존한 Git/DB 근거로 수동 복구가 필요할 수 있다. 대상별 소유권·기존 lease·FF-only·관측/CAS·활성 작업 불변 및 중단 회귀로 완화한다. priority normal은 완료 문서 정리의 필수 보완이며 구현·승인 자체를 막는 작업보다 우선하지 않는다. 최초 level은 5이며 Model·Effort는 Manager 설정을 따른다.
