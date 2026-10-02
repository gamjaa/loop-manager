---
id: G-123
title: Reduce Root Entry Point and Compose Project Services
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-122
complexity: high
risk: high
priority: critical
---

## Purpose

기반·Agent·Git/recovery를 분리한 뒤에도 `loop_manager.py`에는 planning port 구성, project 선택, 기존 PR 동기화와 여러 project의 scheduler가 남는다. 이를 독립적인 application service로 옮겨 root를 안정적인 CLI 진입점과 구성·공개 호환성의 경계로 만든다.

## Acceptance Criteria

- `_PlanningStatePorts`, `_PlanningGitPorts`, Agent/validation/file-review/reconciliation port와 `_ExecutionLifecyclePorts`는 담당 application adapter에서 구성한다. port에 root module 전체를 넘기지 않는다.
- `_run_project_step`, `_run_project_result`, registry/document draft command, 다중 project scheduling과 self-runtime handoff를 project 처리, command 처리와 scheduling으로 나눈다. 상태 전이와 실행 순서는 기존 coordinator에 위임한다.
- `loop_manager.py` 본문에는 CLI 인자 해석, 진입점, service 구성의 위임과 확인된 공개 export만 남는다. SQL/schema·provider subprocess·Git mutation·업무 상태 전이·역할 prompt의 본문을 root에 남기거나 거대한 새 facade에 그대로 옮기지 않는다.
- 진행 중 작업의 비선점, catalog 재조정, PR 상태 우선, priority/dependency, maintenance barrier·post-refactor revalidation, 최대 동시 실행 수와 self Merge handoff 순서를 유지한다. R-049의 새 Planner 역할을 추가하지 않는다.
- package import만으로 CLI·DB 접속·Agent 호출·registry 변경이 발생하지 않는다. 내부 의존은 contract/domain→adapter→구성 방향으로 순환 없이 유지하며 resource/state/config는 명시적 identity를 사용한다.

## Implementation

1. G-120~G-122 이후 root에 남은 함수와 소비자를 다시 확인해 project application service, 역할별 port 구성, command handler와 scheduler에 대응시킨다.
2. `_run_project_step`의 선택·동기화·planning·execution을 기존 catalog/maintenance/planning/execution service에 위임하고 typed 처리 결과를 scheduler에 반환한다. 선택 분기를 또 하나의 거대한 함수로 모으지 않는다.
3. port 구성은 G-120 repository, G-121 Agent와 G-122 Git/PR adapter를 필요한 단위로 받는다. 기존 호환 API도 같은 service에 연결하며 이중 구현을 남기지 않는다.
4. CLI와 bootstrap 위임은 root에 유지하고 필요한 공개 helper만 명시적으로 export한다. G-119의 공개 계약·scheduler test는 실제 구성에 연결되는 patch/port를 검증한다.
5. G-060 대응표에 남은 root 본문의 이동·통합·공개 근거를 기록한다. G-124/G-125가 분리할 domain coordinator의 상세 구현을 root로 되돌리지 않는다.

## Validation

- Worker는 CLI dispatch, port 구성, project 선택과 scheduler decision의 변경 경계를 직접 확인하는 targeted unit test만 실행한다.
- critical/normal 우선순위, 미Merge dependency, planning 실행 자리 소유권, maintenance prerequisite/refactoring/revalidation, PR 관측과 새 Worker 시작의 순서를 검증한다.
- self Merge handoff 뒤 새 Agent/verify를 시작하지 않고 기존 session·retry·workspace를 보존하는지, project 실패가 다른 project 결과를 지우지 않는지 확인한다.
- Manager 전체 verify에서 직접 CLI, Windows/Linux launcher, 다중 project 처리와 중단·재개 결과가 유지돼야 한다. import graph와 root/새 module의 diff로 남은 책임·순환 의존·거대한 facade 유무를 읽기 전용 확인한다.

## Progress

- [x] root 잔여 책임과 port 구성 대응표 확인
- [x] application service·scheduler·command 처리 분리
- [x] root를 진입점/공개 export로 줄이고 targeted unit test 갱신
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: 임의의 root 행 수 상한보다 구현 책임이 root에서 빠지고 독립적으로 변경·테스트할 service로 이동한 것을 완료 기준으로 삼는다. 남길 공개 helper는 G-060에서 확인한 호출 계약을 따른다.
- 계획은 각각 검증·Merge하기 위해 분리한다. 같은 root와 구성을 바꾸는 G-120~G-123에는 순차 dependency를 두고 구조 변경이 Merge된 뒤 다음 Worker를 시작한다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.

- 2026-10-02 Worker: G-060 Implementation에 최신 root inventory와 잔여 본문 대응표를 추가했다. 신규 root 항목 없이 기존 하위 package를 사용한다. root의 함수/클래스 본문은 `build_parser`와 `main`만 남기고 공개 helper는 서명 보존 export와 lazy named callback으로 같은 서비스에 연결했다.
- 프로젝트의 안전 확인, catalog/cleanup, PR 관측, 기존 실행 복구와 coordinator 호출을 `ProjectService`의 독립 단계로 분리했다. 프로젝트 identity와 호출별 관측을 별도 dataclass로 관리하며 기존 planning/execution coordinator가 선택·전이를 수행한다. SQLite 형식·저장 경계, provider prompt, registry 정책과 R-049 역할은 바꾸지 않았다.
- root 전체를 받는 port와 동적 `globals()` contract 구성을 없앴다. effect 객체 생성만으로 DB·Git·Agent를 호출하지 않으며 기존 port 생성 뒤의 root patch도 실제 callback에 전달한다. self Merge gate는 기존 ContextVar identity를 한 runtime module에서 공유한다.
- targeted 검증: `test_project_services` 8개, `test_registered_project_scheduler` 17개, `ProjectCatalogRefreshTest` 34개, priority/dependency 1개, maintenance cycle/revalidation·prerequisite 2개, 실제 Planner port 재개 1개, 공개 identity·복사 runtime·verify 목록 3개를 각각 module/class/case 범위로 실행했다. 중간 import 구성·test fixture·동명 effect class 오류를 수정한 뒤 같은 실패 명령을 재실행해 통과했다. 전체 test/verify·commit·push·PR 생성은 하지 않았으며 Manager verify, Review와 사용자 Merge는 남아 있다.
- 2026-10-02 관리 rebase 후 재확인: HEAD `709b12c`의 ExecPlan·PRODUCT·R-028·ARCHITECTURE와 추적 변경 5개·추가 파일 58개의 최종 후보 전체를 다시 확인했다. 새 HEAD의 Agent prompt·scope 판정은 그대로 재사용하며, 이전 후보의 검증 결과를 대신 사용하지 않았다. HEAD 원본과 이동된 484개 정의를 AST로 역대응했고 의도적인 본문 변경은 프로젝트 단계 분리와 execution contract 구성 두 곳이었다. 내부 169개 module의 import 시점 graph에는 순환이 없고 새 root 파일이나 보호 문서 변경은 없다. 위와 같은 최소 module/class/case 범위의 unit test 66개를 현재 후보에서 새로 실행해 모두 통과했다. 이 재검증에서는 targeted test 실패가 없었으며 전체 verify와 Review는 Manager가 수행한다.
- 2026-10-02 결과 계약 재확인: 이전 최종 보고의 `TARGETED_TESTS`에 쓴 `실패 없음`은 기존 strict 검증기가 미해결 실패로 해석하는 문구였다. 정책 변경 없이 현재 결과를 명시적인 PASS 증거로 보고한다. 이동된 `parse_worker_result` 공개 export의 정상 보고 수용·모호한 보고 거부를 확인하는 unit test를 추가했다. HEAD·근거 입력과 최종 diff 전체를 다시 확인하고 이전 통과 결과를 재사용하지 않은 채 project service 9개, scheduler 17개, catalog refresh 34개, 선택·유지보수·Planner port 4개, 공개 계약·복사 runtime·verify 목록 3개를 새로 실행해 67개 모두 통과했다.
