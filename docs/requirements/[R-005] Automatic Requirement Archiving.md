# [R-005] Automatic Requirement Archiving

## Context

현재 완료된 Requirement는 사용자가 `docs/requirements/archive/`로 직접 옮겨야 한다. 이 작업을 빠뜨리면 이미 충족된 Requirement가 계속 활성 Planner 입력으로 남아 중복 계획의 원인이 된다.

단순히 파일만 이동하면 기존 ExecPlan의 `sources`가 가리키는 원래 경로가 사라져 ExecPlan 카탈로그 검증도 실패한다. Requirement의 원래 경로는 안정적 식별자로 유지하면서, 완료된 문서는 archive에서 조회할 수 있어야 한다.

현재 이력에서는 `[R-003] Targeted Worker Unit Tests`의 유일한 관련 계획인 G-006이 PR #5로 Merge됐으므로 이미 archive 대상이다. R-001은 관련 자동화 작업이 완료되지 않았고, R-002는 G-007이 아직 Merge되지 않았으며, R-004는 관련 ExecPlan이 아직 없어 활성 상태로 남아야 한다.

## Requirement

Requirement를 완료하는 마지막 ExecPlan의 Worker는 해당 Requirement를 `docs/requirements/archive/`로 이동하고 그 변경을 구현 PR에 포함해야 한다. PR이 Merge될 때 코드와 archive 이동이 함께 base branch에 반영돼, 완료된 Requirement가 다음 Planner 입력에서 자동으로 제외돼야 한다.

Loop Manager는 Requirement를 source로 사용하는 다른 ExecPlan이 모두 MERGED이고 현재 ExecPlan이 마지막 미완료 계획일 때만 archive 이동을 허용해야 한다. 기존 ExecPlan frontmatter의 source 경로는 수정하지 않으며, 활성 경로에 파일이 없으면 같은 파일명의 archive 경로에서 완료된 source를 조회해야 한다.

기능 도입 시에는 Git과 GitHub 이력을 기준으로 이미 완료된 Requirement도 같은 PR에서 archive해야 한다.

## Acceptance Criteria

- Requirement를 source로 사용하는 다른 모든 ExecPlan이 MERGED이고 현재 ExecPlan만 남았을 때 Worker가 해당 Requirement를 `docs/requirements/archive/`로 이동한다.
- 관련 READY, IMPLEMENTING, VERIFYING, REVIEWING, PR_OPEN 또는 BLOCKED ExecPlan이 현재 작업 외에 하나라도 남아 있으면 Requirement를 이동하지 않는다.
- 하나의 ExecPlan이 여러 Requirement의 마지막 작업이면 조건을 충족한 Requirement를 모두 이동한다.
- archive 이동은 최종 구현과 같은 branch와 PR에 commit되며 PR이 Merge되지 않으면 base branch의 활성 Requirement는 그대로 유지된다.
- PR 생성 직전 rebase로 새로운 관련 ExecPlan이 추가되거나 상태 조건이 달라지면 archive 여부를 다시 계산한다.
- archive에 같은 파일이 이미 있거나 source 관계와 상태를 확정할 수 없으면 파일을 덮어쓰지 않고 해당 ExecPlan을 BLOCKED로 처리한다.
- Planner는 `docs/requirements/` 바로 아래의 `*.md`만 입력으로 읽고 `archive/`는 읽지 않는다.
- 기존 ExecPlan의 `sources`에는 원래 활성 Requirement 경로를 유지한다.
- ExecPlan source 검증과 조회는 원래 경로가 없을 때 동일 파일명의 `docs/requirements/archive/` 경로를 사용한다.
- BLOCKED 해제를 위한 내용 해시는 활성 Requirement에만 적용하며, MERGED ExecPlan의 archived source는 재실행 입력으로 사용하지 않는다.
- archive 이동만으로 새로운 계획 주기나 프로젝트 완료 알림을 만들지 않는다.
- 기능 도입 PR은 구현 시점의 이력을 다시 확인해 이미 완료된 Requirement를 함께 archive하며, 현재 확인된 R-003이 여전히 활성 경로에 있으면 이동한다.
- R-001, R-002와 R-004는 각각의 관련 ExecPlan 완료 조건을 만족하기 전에는 이동하지 않는다.
- 단일·복수 Requirement, 남은 관련 계획, PR 미Merge, rebase 중 새 계획 추가, archive 충돌, 기존 archived source 조회와 과거 완료 Requirement 정리를 자동 테스트로 검증한다.

## Product Impact

- 완료된 Requirement는 별도 사용자 작업 없이 Planner 입력에서 제거된다.
- Requirement archive를 빠뜨려 완료된 작업이 다시 계획되는 문제를 방지한다.
- 기존 ExecPlan은 원래 source 식별자를 유지하면서 완료 당시의 Requirement를 계속 추적할 수 있다.

## Non-Goals

- Requirement가 완료되기 전에 사용자의 판단만으로 자동 archive하지 않는다.
- PRODUCT.md나 ExecPlan 파일을 Requirement와 함께 archive하지 않는다.
- archive된 Requirement를 자동으로 다시 활성화하지 않는다.
- 기존 ExecPlan의 source 경로를 archive 경로로 일괄 수정하지 않는다.

## Open Questions

- 없음
