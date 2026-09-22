# [R-013] 계획 및 프로젝트 차단 범위 분리

## Context

현재 Loop Manager는 하나의 ExecPlan이 `BLOCKED`가 되면 같은 프로젝트의 다른 `READY` ExecPlan도 실행하지 않는다. 또한 구현과 검증이 끝난 계획이 `PR_OPEN`으로 Merge만 기다리는 동안에도 프로젝트의 유일한 실행 자리를 계속 점유한다. 저장소나 작업 공간의 안전이 훼손된 경우에는 프로젝트 전체 제한이 필요하지만, 특정 계획의 실패·사용자 결정·PR Merge 대기처럼 영향 범위가 한 계획에 한정된 경우까지 독립적인 작업을 모두 중단하는 것은 과도하다.

## Requirement

Loop Manager는 차단의 영향 범위를 다음 두 상태로 구분해야 한다.

- `PLAN_BLOCKED`: 원인이 특정 ExecPlan에 한정된다. 해당 계획만 보류하고, 그 계획에 의존하지 않는 다른 `READY` 계획은 기존 우선순위와 직렬 실행 규칙에 따라 계속 처리한다.
- `PROJECT_BLOCKED`: 원인이 저장소, 공유 Git 상태 또는 기본 worktree의 안전에 영향을 준다. 안전이 복구될 때까지 해당 프로젝트의 Planner와 Worker 실행을 모두 중단한다.

차단 범위는 AI의 자유 형식 설명을 해석하지 않고, Loop Manager 코드가 생성한 구조화된 원인 분류로 결정해야 한다.

`PLAN_BLOCKED`에는 다음과 같이 다른 계획의 작업 공간을 신뢰할 수 있는 상태에서 발생한 원인을 포함한다.

- 특정 계획의 verify 또는 Review 재시도 한도 초과
- 특정 계획에만 필요한 사용자 결정이나 승인 대기
- 특정 계획의 PR 종료 또는 계획 내부 충돌
- 해당 계획의 요구사항이나 구현 정보가 부족하거나 모순된 경우

`PROJECT_BLOCKED`에는 다음과 같이 같은 저장소의 다른 작업도 안전하게 시작할 수 없는 원인을 포함한다.

- AI Agent가 기본 worktree의 파일, index 또는 `HEAD`를 변경한 경우
- Agent가 소유하지 않은 공유 Git ref 또는 `refs/codex/turn-diffs/*`를 변경한 경우
- branch나 worktree의 소유권과 격리를 확인할 수 없는 경우
- 관리 대상 worktree 밖으로 영향을 준 작업 범위 위반이 발생한 경우
- 저장소 상태나 Loop Manager의 작업 공간 기록이 손상되어 안전한 실행 위치를 결정할 수 없는 경우

Merge만 기다리는 안정된 `PR_OPEN`은 차단 상태가 아니라 계획 단위 대기 상태다. GitHub에서 Open 상태이고 충돌 복구가 시작되지 않은 `PR_OPEN`은 프로젝트 실행 자리를 점유하지 않으며 여러 개가 동시에 존재할 수 있다. Loop Manager는 각 실행에서 모든 Open PR을 먼저 동기화하고, Merge 또는 Close 전이를 계획별로 적용한 뒤, 저장소를 변경하는 현재 실행이나 충돌 복구가 없으면 독립적인 `READY` 계획 하나를 선택한다.

Open PR이 충돌 상태로 확정되면 해당 계획의 G-026 복구가 새 Worker보다 먼저 프로젝트의 단일 실행 자리를 사용한다. 여러 PR이 동시에 복구 대상이면 기존 priority와 숫자 ID 순서로 하나만 선택한다. mergeability가 계산 중이거나 불명확한 PR은 branch를 변경하지 않고 `PR_OPEN`으로 유지하되 독립적인 READY 선택을 막지 않는다.

`PLAN_BLOCKED`가 되면 해당 계획은 프로젝트의 활성 실행 자리를 점유하지 않는다. 같은 프로젝트에서는 여전히 Worker를 하나만 실행하며, `depends_on`으로 차단된 계획에 직접 또는 간접 의존하는 계획은 실행하지 않는다. 여러 계획이 각각 `PLAN_BLOCKED` 상태로 남을 수 있다.

`PLAN_BLOCKED`와 안정된 `PR_OPEN`이 실행 자리를 점유하지 않더라도 같은 프로젝트의 Worker, verify, Review, 게시와 PR 충돌 복구는 합쳐서 항상 하나만 실행한다. `PR_OPEN` 또는 `PLAN_BLOCKED`에 직접·간접 의존하는 READY 계획은 선행 계획이 MERGED되거나 정상 해제될 때까지 선택하지 않는다. 독립 READY를 시작할 때 Open PR의 미Merge 변경을 새 branch에 포함된 것으로 간주하지 않는다.

기존 READY 우선 정책은 유지한다. 독립적인 `READY` 계획을 모두 처리한 뒤 차단 또는 Merge 대기 중인 계획만 남았다면 새 일반 Planner나 유지보수 Planner를 실행해 이를 우회하지 않는다. `PLAN_BLOCKED`, `PROJECT_BLOCKED` 또는 `PR_OPEN`이 하나라도 남아 있으면 프로젝트 완료로 판단하지 않는다.

차단 해제 조건이 충족되면 `PLAN_BLOCKED` 계획은 `READY`로 돌아가 정상 선택 대상이 된다. `PROJECT_BLOCKED`는 공유 저장소 안전이 복구되고 검증된 뒤 해제한다. 프로젝트 안전 문제와 계획 내부 문제가 함께 남아 있다면 프로젝트 차단 해제 후 해당 계획을 `PLAN_BLOCKED`로 유지한다. 계획 처리 중 프로젝트 범위의 안전 문제를 발견하면 즉시 `PROJECT_BLOCKED`로 승격한다.

차단 상태, 구조화된 원인 코드, 관련 ExecPlan ID, 판단 근거와 해제 감지용 입력 지문은 SQLite에 보존한다. 기존의 범위 정보가 없는 `BLOCKED` 기록은 저장된 구조화 정보로 안전하게 분류할 수 있는 경우에만 변환하며, 그 외에는 `PROJECT_BLOCKED`로 취급한다.

Telegram BLOCKED 알림을 사용하는 경우 차단 범위와 관련 ExecPlan을 함께 표시하고, 범위·원인·계획이 같은 알림은 중복 전송하지 않는다.

## Acceptance Criteria

- 한 ExecPlan이 계획 내부 원인으로 `PLAN_BLOCKED`가 되면 그 계획과 의존 계획은 선택되지 않고, 독립적인 다음 `READY` 계획은 실행된다.
- 안정된 `PR_OPEN`이 하나 이상 있어도 실행 중인 변경 단계나 충돌 복구가 없으면 독립적인 다음 `READY` 계획이 실행된다.
- 같은 프로젝트에서 여러 계획이 `PR_OPEN`으로 대기할 수 있지만 Worker·verify·Review·게시 및 PR 충돌 복구는 한 번에 하나만 실행된다.
- Worker 선택 전에 모든 Open PR의 Merge·Close·충돌 상태를 동기화하며, Merge와 Close 전이는 해당 ExecPlan에만 적용된다.
- 충돌이 확정된 Open PR은 새 Worker보다 먼저 하나씩 복구하고, mergeability가 불명확한 PR은 변경하지 않은 채 독립 READY 실행을 허용한다.
- `PLAN_BLOCKED`가 존재해도 같은 프로젝트에서 둘 이상의 Worker가 동시에 실행되지 않는다.
- 저장소 또는 작업 공간 안전 원인이 발생하면 프로젝트가 `PROJECT_BLOCKED`가 되고 Planner와 Worker가 실행되지 않는다.
- `PROJECT_BLOCKED` 중에도 PR 상태 동기화와 알림처럼 저장소를 변경하지 않는 관리 작업은 계속 수행할 수 있다.
- 차단 범위는 구조화된 원인 코드로 결정되며 AI 출력 문구의 키워드나 의미 분석에 의존하지 않는다.
- `depends_on` 대상이 `PLAN_BLOCKED`이면 모든 직접·간접 의존 계획이 대기한다.
- 독립적인 `READY` 계획이 없고 차단 또는 `PR_OPEN` 계획만 남으면 일반 Planner와 유지보수 Planner를 실행하지 않는다.
- 계획 차단 원인이 해소되면 해당 계획이 `READY`로 돌아가고, 프로젝트 차단 원인이 해소되면 기존 우선순위 규칙에 따라 작업을 재개한다.
- 프로젝트 안전은 복구됐지만 계획 내부 원인이 남아 있으면 프로젝트는 재개되고 해당 계획만 `PLAN_BLOCKED`로 남는다.
- 범위 정보가 없는 기존 `BLOCKED` 기록은 안전하게 분류할 수 없을 때 `PROJECT_BLOCKED`로 처리된다.
- `PLAN_BLOCKED`, `PROJECT_BLOCKED` 또는 `PR_OPEN`이 남아 있는 프로젝트에는 전체 완료 알림을 보내지 않는다.
- BLOCKED Telegram 알림에는 차단 범위, 관련 ExecPlan과 원인이 포함되며 같은 차단 사건은 한 번만 전송된다.

## Product Impact

- 계획 내부 문제는 해당 계획과 의존 계획만 중단한다.
- PR Merge 대기는 해당 계획과 의존 계획만 대기시키며 독립적인 기존 READY 계획은 순차적으로 계속 처리한다.
- 저장소나 작업 공간의 안전 문제는 프로젝트 전체 실행을 중단한다.
- 차단 또는 Merge 대기 중인 계획과 독립적인 작업은 안전이 확인된 범위에서 순차적으로 계속 진행한다.

## Non-Goals

- 같은 프로젝트에서 여러 Worker를 병렬 실행하지 않는다.
- `depends_on` 대기를 우회하지 않는다.
- Open PR을 자동 Merge·Close하거나 Open PR의 미Merge 변경을 독립 branch에 암묵적으로 누적하지 않는다.
- 단순히 base보다 뒤처진 Open PR을 충돌이 확정되지 않은 상태에서 다시 게시하지 않는다.
- 차단된 계획을 피해 새 ExecPlan을 생성하도록 Planner를 실행하지 않는다.
- AI가 자유 형식 차단 사유를 읽어 차단 범위를 결정하지 않는다.
- 안전을 확인하지 않은 채 `PROJECT_BLOCKED`를 자동으로 낮추거나 해제하지 않는다.

## Open Questions

- 없음
