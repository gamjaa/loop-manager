# [R-037] Replan Before READY on Input Changes

## Context

현재 일반 Planner는 명세 문서가 바뀌면 READY보다 먼저 실행하지만, 소스·테스트·설정만 바뀌면 실행 가능한 READY를 모두 먼저 처리한다. 그 사이 기존 계획이 최신 코드 구조와 맞지 않아 작업을 다시 해야 할 수 있다. 이 Requirement는 현재 PRODUCT·ARCHITECTURE의 전체 입력 변경 시 READY 우선 순서를 변경한다. ExecPlan archive 이동 자체는 계획 입력에서 제외되어 있지만, 마지막 ExecPlan이 `MERGED`되면 저장된 전체 계획 입력 지문을 비워 실제 입력 변경이 없어도 다음 선택 시점에 Planner가 실행될 수 있다.

## Requirement

일반 Planner의 실행 여부는 마지막 성공 기준과 최신 `origin/<base-branch>`의 명세·전체 계획 입력이 실제로 달라졌는지로 판단한다. 전체 계획 입력에는 기존과 같이 `AGENTS.md`, PRODUCT, ARCHITECTURE, 활성 Requirement 및 archive와 계획 문서를 제외한 소스·테스트·설정을 포함한다. 안전한 선택 시점에 이 입력 중 하나라도 바뀌었으면 실행 가능한 READY보다 일반 Planner를 먼저 한 번 실행한다. Planner는 최신 입력과 기존 READY를 대조해 필요한 계획을 갱신·취소하거나 새 계획을 작성한 뒤, 유효한 READY를 선택하게 한다. Planner가 변경할 계획이 없다고 판단해도 성공한 입력 기준을 저장하고 기존 READY를 실행한다.

마지막 ExecPlan의 `MERGED` 전이, ExecPlan 파일의 생성·수정·archive 이동, 내용 변경 없는 Requirement 자동 archive만으로는 성공한 일반 Planner의 입력 기준을 무효화하거나 새 계획 주기를 시작하지 않는다. 이 사건과 함께 소스 또는 다른 계획 입력이 실제로 바뀌었으면 그 변경을 우선 감지한다. 입력이 그대로면 실행 가능한 READY를 기존 순서대로 선택하고, 없으면 안정적으로 대기하거나 기존 완료 조건을 평가한다. 활성 Requirement가 남아 있지만 시작 가능한 ExecPlan이 없으면 완료로 오인하지 않고 기존 작업 없음 사유 알림 정책에 따라 이유를 알린다.

프로젝트에 성공한 일반 Planner 기준이 아직 없다면 최초 계획 수립은 한 번 허용한다. 이미 진행 중인 Worker나 Planner 주기를 새 입력이 선점하지 않고 다음 안전한 선택 시점에 판단한다. Planner 실패 복구·게시 재개와 별도 주기로 정의된 유지보수 Planner는 기존 조건을 따른다.

## Acceptance Criteria

- 마지막 ExecPlan이 `MERGED`된 뒤 `docs/plans/` 정리 등 제외된 경로만 바뀌고 명세·전체 계획 입력이 그대로면, 다음 반복 실행에서 일반 Planner를 호출하지 않는다.
- 소스·테스트·설정만 바뀌고 실행 가능한 READY가 남아 있어도, 다음 안전한 선택 시점에는 Worker보다 일반 Planner를 먼저 한 번 실행해 최신 `origin/<base-branch>`와 READY를 대조한다. 필요하면 미착수 READY를 갱신·취소하고, 변경이 불필요하면 기존 READY를 실행한다.
- ExecPlan의 구현 PR이 `MERGED`되면서 소스·테스트·설정이 바뀌었으면, 다른 실행 가능한 READY가 남았더라도 새 전체 계획 입력을 기준으로 일반 Planner를 먼저 한 번 실행한다.
- `AGENTS.md`, PRODUCT, ARCHITECTURE 또는 활성 Requirement의 실질적인 변경도 READY보다 먼저 감지한다. 성공 후 입력이 다시 바뀌지 않으면 Planner를 반복 호출하지 않는다.
- ExecPlan 파일의 생성·수정·archive 이동과 활성 Requirement의 내용 보존 자동 archive만으로는 일반 Planner를 호출하지 않는다. 같은 변경에 다른 계획 입력의 실질적인 변경이 포함되면 그 변경은 감지한다.
- 마지막 `MERGED`, ExecPlan archive 이동 또는 Requirement 자동 archive와 명세·소스·테스트·설정 변경이 같은 선택 주기에 겹쳐도, 제외 대상 사건이 유효한 입력 변경을 가리지 않는다. 명세와 전체 입력이 함께 바뀌어도 최신 입력을 대상으로 일반 Planner를 한 번만 실행한 뒤 READY를 선택한다.
- 일반 Planner가 실패하거나 게시가 완료되지 않으면 성공 기준을 전진시키거나 변경 전 READY를 새 입력에 맞는 계획으로 간주해 Worker를 시작하지 않는다. 기존 오류·복구 정책을 따른다.
- 계획 입력이 변경되지 않은 채 모든 ExecPlan이 `MERGED`돼도, 미해결 활성 Requirement나 차단 사유가 있으면 프로젝트 완료로 알리지 않는다. 시작 가능한 작업이 없는 이유는 기존 알림·로그 계약에 따라 확인할 수 있다.
- 최초 계획 수립, 진행 중인 Planner의 복구·재시도 및 주기적 유지보수 Planner는 해당 흐름의 기존 조건에 따라 실행할 수 있다.

## Product Impact

- 최신 원격 명세·코드에 맞춰 기존 READY를 확인한 뒤 실행하며, Merge 상태 전이나 완료 계획 정리만으로 중복 계획하지 않는다.

## Non-Goals

- 이미 진행 중인 ExecPlan이나 Planner 주기를 새 입력 때문에 선점하지 않는다.
- 유지보수 Planner의 실행 주기와 조건을 변경하지 않는다.
- 소스·테스트·설정 변경을 일반 Planner 입력에서 제외하지 않는다.

## Open Questions

- 없음
