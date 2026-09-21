# [R-001] Automation-First Planning and Priority

## Context

현재 Planner는 하나 이상의 ExecPlan을 만들 수 있지만 현재 입력에서 식별 가능한 작업 전체를 한 번에 계획해야 한다는 보장이 없다. 반대로 실행 중이거나 READY인 계획이 있는데 저장소 입력이 바뀔 때마다 Planner를 먼저 호출하면 이미 준비된 작업의 진행이 지연되고 불필요한 AI 호출이 반복될 수 있다. 실행 순서도 숫자 ID만 사용하므로 사용자의 반복 개입을 줄이는 자동화 기반 작업보다 다른 기능이 먼저 실행될 수 있다.

Loop Manager의 목적은 안전을 유지하면서 계획, 구현, 검증, 복구와 다음 작업 선택을 사용자의 개입 없이 최대한 계속하는 것이다. 따라서 다음 Worker를 시작하기 전에 전체 작업 카탈로그와 자동화 중심 우선순위를 확보해야 한다.

Planner가 중복을 피하기 위해 누적된 모든 ExecPlan 본문을 매번 읽으면 완료된 계획이 늘어날수록 토큰 사용량도 계속 증가한다. Merge된 구현의 현재 상태는 최신 코드와 테스트에 반영돼 있고 완료된 Requirement는 archive되므로, MERGED 계획의 본문을 다시 읽는 것은 일반적인 계획 수립에 필요하지 않다.

## Requirement

이 Requirement는 현재 PRODUCT와 ARCHITECTURE에 기록된 “계획 입력이 변경되면 READY ExecPlan보다 Planner를 먼저 실행한다”는 계약을 의도적으로 변경한다. 사용자는 실행 중인 변경 작업이 없는 선택 경계에서 일반 Planner보다 기존 READY 카탈로그를 먼저 처리하고, dependency를 충족하지 못한 READY만 남은 경우에도 Planner로 우회하지 않는 READY-first 정책을 승인한다. 본 명세 변경에서 사용자가 ARCHITECTURE의 Planner-first 문구를 READY-first 계약으로 갱신해 `origin/main`에 먼저 게시하며, G-024 Worker는 갱신된 ARCHITECTURE를 읽기 전용 입력으로 사용하고 코드와 PRODUCT만 구현 PR에서 전환한다. 이 승인은 G-024 범위의 READY 소진 순서에 한정하며, 안정된 `PR_OPEN`과 독립 READY의 동시 진행은 G-031 범위로 유지한다.

Loop Manager는 저장소를 변경하는 실행 단계의 ExecPlan이 있으면 해당 상태를 먼저 이어서 처리해야 한다. Merge만 기다리는 안정된 `PR_OPEN` ExecPlan은 완료되지 않은 계획으로 유지하되 프로젝트의 실행 자리를 점유하지 않는다. 모든 Open PR의 상태를 먼저 동기화한 뒤 충돌 복구가 필요하지 않고 실행 중인 변경 작업도 없으면, Open PR에 직접 또는 간접 의존하지 않는 READY ExecPlan을 선택해 실행한다. 같은 프로젝트에서 Worker·verify·Review·게시 또는 PR 충돌 복구는 항상 하나만 실행하지만, Merge를 기다리는 PR은 여러 개 존재할 수 있다.

진행 중인 변경 작업이 없으면 의존성을 충족한 READY ExecPlan을 선택해 실행하고, 남아 있는 READY ExecPlan과 `PR_OPEN` ExecPlan이 모두 없을 때만 Planner를 한 번 실행해야 한다. READY 또는 `PR_OPEN` ExecPlan이 남아 있는 동안에는 계획 입력이 변경됐더라도 Planner를 먼저 실행하지 않는다. `PR_OPEN`에 의존하는 READY 계획은 선행 PR의 Merge가 확인될 때까지 선택하지 않는다.

예외적으로 주기적 리팩터링 평가가 도래한 프로젝트는 진행 중인 작업이 끝나고 `PR_OPEN`, `PLAN_BLOCKED`와 `PROJECT_BLOCKED`가 없을 때 READY 선택 전에 유지보수 Planner를 실행할 수 있다. 이 호출은 현재 구조에서 리팩터링 필요성을 확인하고 아직 시작하지 않은 READY 계획 사이에 dependency 장벽을 구성하거나, 리팩터링 Merge 뒤 장벽 뒤 계획을 최신 구조에 맞게 재검증하는 범위로 제한한다. 일반적인 새 제품 입력을 계획하거나 활성·Merge 대기·차단 계획을 수정하기 위한 우회로 사용하지 않는다.

Planner는 기존 카탈로그와 현재 입력을 대조해 현재 식별 가능한 모든 미충족 작업을 독립적으로 검증하고 Merge할 수 있는 ExecPlan으로 일괄 작성해야 한다. 유효한 계획을 만들거나 추가 계획이 없다는 정상 결과로 종료하면 해당 원격 저장소 입력 지문을 저장한다. 이후 같은 입력 지문에서는 READY 계획이 없어도 Planner를 다시 호출하지 않으며, 계획 입력에 해당하는 원격 저장소 내용이 바뀐 경우에만 새 계획 주기를 허용한다. 주기적 유지보수 Planner가 기존 READY 카탈로그 앞에 리팩터링 장벽을 삽입하는 경우에는 새 일반 기능 계획을 같은 batch에 추가하지 않고, 리팩터링과 사후 READY 재검증이 끝난 뒤 최신 코드에서 일반 계획 주기를 다시 판단한다.

Loop Manager 코드는 AI를 사용하지 않고 ExecPlan frontmatter와 SQLite 상태를 조합해 비종료 ExecPlan의 compact catalog를 만든다. Planner는 활성 PRODUCT와 Requirement, ARCHITECTURE, 최신 코드·테스트 및 이 compact catalog를 기준으로 계획한다. MERGED ExecPlan은 Planner prompt와 기본 읽기 대상에서 제외하며, 프로그램만 ID 고유성, 의존성 검증과 감사 이력을 위해 유지한다.

각 ExecPlan에는 코드가 검증하고 정렬할 수 있는 고정 priority를 기록한다. 값과 순서는 다음과 같다.

1. `critical`: 안전한 무인 실행을 막거나 데이터 손실·보안 위험 및 반복적인 수동 복구를 유발하는 작업
2. `high`: 정상 경로에서 사용자의 반복 개입을 제거하고 계획부터 PR 생성까지의 핵심 자동화를 완성하는 작업
3. `normal`: 그 밖의 Current Goal과 필수 유지보수
4. `low`: 무인 완료에 필요하지 않은 편의 기능과 최적화

실행 중인 작업은 새 입력이나 더 높은 priority로 선점하지 않는다. 다음 Worker 선택 시에는 `depends_on`을 모두 충족한 READY 계획 중 priority가 가장 높은 계획을 선택하고, 같은 priority에서는 숫자 ID가 가장 작은 계획을 선택한다.

## Acceptance Criteria

- IMPLEMENTING, VERIFYING, REVIEWING, PUBLISHING 또는 PR 충돌 복구 상태의 현재 ExecPlan이 있으면 새 Planner나 다른 Worker를 시작하지 않고 해당 상태의 처리·복구를 먼저 이어간다.
- 다음 Worker를 선택하기 전에 모든 `PR_OPEN` ExecPlan을 조회해 Merge, Close와 충돌 여부를 동기화한다. Merge된 계획은 MERGED로 전이하고, Merge 없이 닫힌 계획과 충돌 복구가 필요한 계획은 해당 계획의 상태 전이 및 복구를 먼저 처리한다.
- Merge만 기다리는 `PR_OPEN` ExecPlan은 프로젝트의 실행 자리를 점유하지 않는다. 실행 중인 변경 단계나 복구가 없으면 직접·간접 dependency에 MERGED가 아닌 계획을 포함하지 않는 READY ExecPlan 하나를 기존 우선순위로 실행한다.
- 같은 프로젝트에서는 Worker·verify·Review·게시 및 PR 충돌 복구를 합쳐 한 번에 하나만 실행한다. 이 단일 실행 규칙을 유지하면서 여러 ExecPlan이 동시에 안정된 `PR_OPEN` 상태로 Merge를 기다릴 수 있다.
- 현재 변경 실행이 없고 의존성을 충족한 READY ExecPlan이 있으면 주기적 유지보수 평가 또는 사후 재검증이 도래한 경우를 제외하고 Planner보다 해당 계획을 먼저 선택해 Worker를 시작한다.
- 남아 있는 READY ExecPlan이 하나라도 있으면 일반 계획 입력 변경만을 이유로 Planner를 실행하지 않는다.
- READY ExecPlan은 남아 있지만 모두 `depends_on`을 충족하지 못한 경우 이를 READY가 없는 상태로 취급하지 않으며 일반 Planner를 실행하지 않는다. `PR_OPEN`, `PLAN_BLOCKED` 또는 `PROJECT_BLOCKED`가 함께 있으면 유지보수 Planner도 실행하지 않는다.
- 사용자 소유 명세 변경은 G-024 재개 전에 ARCHITECTURE의 ExecPlan 실행 흐름·동시 실행 계약에서 “입력 변경 시 READY보다 Planner 우선”인 문구를 “READY 카탈로그 소진 후 Planner 판단”으로 바꿔 `origin/main`에 게시한다. G-024 구현 PR은 현재 PRODUCT의 Product Invariants·핵심 실행 흐름·Business Rules와 코드를 같은 정책으로 바꾸되 Worker가 ARCHITECTURE를 수정하지 않으며, 완료 시 코드·PRODUCT·Requirement·ARCHITECTURE 사이에 상반된 선택 순서를 남기지 않는다.
- 주기적 리팩터링 평가가 도래하고 현재 변경 실행, `PR_OPEN`, `PLAN_BLOCKED`와 `PROJECT_BLOCKED`가 모두 없으면 READY 존재 여부와 관계없이 다음 Worker 선택 전에 유지보수 Planner를 한 번 실행한다.
- 유지보수 Planner는 branch, worktree, Agent 세션과 PR이 없는 READY 계획만 R-012의 dependency 장벽 구성 또는 사후 구조 재검증 범위에서 수정할 수 있다.
- 리팩터링 전에 기존 READY 구현이 필요하면 최소 dependency-closed 선행 집합만 처리한 뒤 최신 코드에서 다시 평가하고, 리팩터링 계획을 미리 확정하지 않는다.
- 새 리팩터링 계획과 READY dependency 변경은 전체 카탈로그를 검증한 하나의 계획 commit으로 게시하고, 원격 확인 뒤 SQLite catalog를 원자적으로 갱신한다.
- 확정된 리팩터링 장벽 뒤의 기존 READY 계획은 리팩터링 DAG의 말단 계획에 의존하며, 장벽의 다음 실행 순서는 일반 priority 정렬보다 먼저 적용한다.
- 마지막 리팩터링 Merge 뒤에는 장벽 뒤 READY Worker를 시작하기 전에 해당 계획을 최신 원격 코드 기준으로 재검증하고, 안전한 개정·게시·catalog 갱신을 완료하거나 planning error로 중단한다.
- 현재 실행 중인 ExecPlan, READY ExecPlan과 `PR_OPEN` ExecPlan이 모두 없을 때만 현재 원격 계획 입력 지문을 기준으로 Planner 실행 여부를 판단한다.
- 현재 입력 지문에 대해 완료된 Planner 실행 이력이 없으면 Planner를 한 번 실행한다.
- Planner가 유효한 ExecPlan 집합을 만들거나 추가 계획이 없다는 정상 결과로 종료하면 해당 입력 지문을 완료된 계획 주기로 저장한다.
- 완료된 계획 주기와 입력 지문이 같으면 이후 스케줄러 실행에서도 Planner를 다시 호출하지 않는다.
- PRODUCT, 활성 Requirement, ARCHITECTURE, AGENTS, 관련 코드·테스트 등 계획 입력에 포함되는 `origin/<base-branch>` 내용이 바뀌면 새 입력 지문으로 Planner를 다시 한 번 실행할 수 있다.
- 로컬 미commit 변경과 MERGED ExecPlan 본문처럼 계획 입력에 포함되지 않는 변경은 Planner 재실행 조건으로 사용하지 않는다.
- Planner CLI 실패, 결과 검증 실패와 게시 실패는 정상적으로 완료된 계획 주기로 저장하지 않으며 기존의 제한된 재시도와 planning error 정책을 따른다.
- Planner는 현재 입력에서 식별 가능한 미충족 작업을 하나만 작성하고 종료하지 않고, 독립적으로 검증·Merge 가능한 전체 ExecPlan 집합과 의존성 그래프를 한 계획 commit에 작성한다.
- 최신 코드·테스트에 이미 구현됐거나 비종료 ExecPlan이 충족하는 작업을 중복 계획하지 않는다.
- compact catalog에는 비종료 ExecPlan의 ID, 제목, SQLite 상태, sources, depends_on과 priority만 포함하며 AI로 별도 요약하지 않는다.
- Planner는 compact catalog에서 현재 입력과 겹치는 비종료 ExecPlan만 필요에 따라 전문을 읽고, 모든 ExecPlan 본문을 선행 입력으로 읽지 않는다.
- MERGED ExecPlan의 메타데이터와 본문은 Planner prompt에 포함하지 않고 Planner가 기본적으로 읽지 않는다.
- 프로그램은 MERGED ExecPlan도 파싱해 ID 고유성, 새 ExecPlan 번호, 의존성 충족과 감사 이력을 코드로 관리한다.
- ExecPlan의 priority는 `critical`, `high`, `normal`, `low` 중 하나이며 공통 형식과 코드 파서가 검증하고 SQLite에 등록한다. 기능 도입 전에 작성된 ExecPlan에는 `normal`을 적용한다.
- 실행 가능한 READY 계획은 dependency gate를 먼저 적용한 뒤 `critical`, `high`, `normal`, `low` 순서와 숫자 ID 오름차순으로 결정적으로 선택된다.
- 안전한 무인 실행 차단 또는 반복적인 사용자 복구를 제거하는 계획이 편의 기능이나 선택적 최적화보다 먼저 선택된다.
- 새 입력은 실행 중이거나 Merge 대기 중인 작업을 중단·수정하지 않는다. 안정된 `PR_OPEN`이 있어도 이미 계획된 독립 READY 작업은 계속 처리하지만, 새 일반 계획과 주기적 유지보수 평가는 모든 `PR_OPEN`이 종료되고 남은 READY 계획까지 소진된 다음 선택 경계까지 보류한다.
- 입력이 변경되지 않으면 READY 계획을 모두 소진한 뒤에도 Planner를 불필요하게 다시 호출하지 않는다.
- `tests/test_loop_manager.py`의 `AutomationFirstCatalogTest.test_dependency_blocked_ready_is_not_an_empty_catalog`는 의존성을 충족하지 못한 READY와 변경된 원격 입력을 가진 프로젝트를 실제 `run_project_result` 및 등록 프로젝트 scheduler 경로로 실행한다. 이 테스트는 해당 프로젝트에서 Planner·Worker Agent가 한 번도 호출되지 않고 `ProjectRunKind.WAITING`이 반환되며, READY catalog 행과 기존 `projects.planning_fingerprint`가 그대로 보존되고, 같은 scheduler 실행의 다른 실행 가능한 프로젝트는 계속 진행하는지를 함께 검증한다. scheduler의 프로젝트별 결정 경로를 `run_project_result` mock으로 대체해서는 이 기준을 충족한 것으로 보지 않는다.
- priority 추가와 선택 순서, 기존 계획 호환성, compact catalog 내용, MERGED 계획의 Planner 입력 제외, 진행 중 작업 재개, 일반 READY 우선 선택, 유지보수 장벽 예외, READY 소진 후 단일 Planner 실행 및 동일 입력 재실행 방지를 자동 테스트로 검증한다.

## Product Impact

- Planner가 실행될 때 현재 입력에서 식별 가능한 작업은 한 번에 ExecPlan 카탈로그로 작성된다.
- 작업 순서는 숫자 ID만이 아니라 안전한 무인 자동화라는 제품 목적을 우선한다.
- 실행 중인 작업은 항상 먼저 처리하고 기존 READY 계획은 주기적 유지보수 장벽이 없을 때 먼저 처리하며, 같은 우선순위에서는 결정적인 순서를 유지한다.
- 완료된 계획 수가 늘어나도 Planner 입력은 활성 문서와 비종료 계획 중심으로 유지된다.
- 변경되지 않은 저장소를 반복 실행해도 Planner 호출이 반복되지 않는다.

## Non-Goals

- 실행 중인 Worker, PR 충돌 복구 또는 해결되지 않은 프로젝트 범위 차단을 중단하거나 우회하지 않는다.
- Merge 대기 중인 PR을 자동 Merge·Close하거나 단순히 최신 base보다 뒤처졌다는 이유로 다시 게시하지 않는다.
- READY 계획이 남아 있는 동안 새 입력만을 이유로 Planner를 선행 실행하지 않는다.
- 의존성을 아직 충족하지 못한 READY 계획을 숨기거나 삭제해 Planner 실행 조건을 만들지 않는다.
- 주기적 유지보수 예외를 기존 READY 계획의 제품 목표, sources 또는 식별자를 다른 작업으로 바꾸는 데 사용하지 않는다.
- 같은 프로젝트에서 둘 이상의 Worker·verify·Review·게시 또는 PR 충돌 복구를 병렬 실행하지 않는다.
- MERGED가 아닌 dependency를 건너뛰거나 Open PR의 변경을 후속 계획 branch에 암묵적으로 포함하지 않는다.
- 제품 문서에 없는 작업을 자동으로 추가하지 않는다.
- 의미 검색을 위한 embedding, vector database 또는 AI 요약 cache를 도입하지 않는다.

## Open Questions

- 없음
