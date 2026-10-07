# [R-078] Requirement archive 확정과 구현 문서 반영

## Context

Requirement 구현 결과는 코드와 제품·설계 문서에 남아야 한다. 현재 Worker는 ARCHITECTURE 수정을 거부하며, archive된 Requirement를 과거 감사 대상으로 읽고 활성 경로로 복원하는 흐름이 있다. 완료된 변경의 현재 기준은 구현과 PRODUCT·ARCHITECTURE이므로 archive를 다시 활성 요구로 취급하지 않는다.

## Requirement

- Worker는 선택한 Requirement·ExecPlan 구현에 필요한 PRODUCT·ARCHITECTURE를 같은 변경에서 갱신한다. 파일 경로만으로 ARCHITECTURE 수정을 거부하지 않는다. 합의된 목표·별도 사용자 결정과 프로젝트 루트 `AGENTS.md` 변경에 대한 사용자 승인 조건은 유지한다. Loop Manager 자체 저장소의 공통·역할별 `docs/AGENTS.*.md` 원본 파일은 현재 역할·목표의 일반 문서 수정과 PR 보고 규칙을 따르며, 파일을 수정해도 현재 작업에 고정된 Loop Manager instruction은 바뀌지 않는다.
- 활성 ExecPlan·draft가 해당 Requirement를 source로 참조하면 archive하지 않는다. 연결된 활성 작업이 없어지면 일반 Planner가 최신 원격 코드·검증·관련 제품·설계 문서로 전체 Acceptance Criteria를 다시 판단한다. 미충족이면 후속 ExecPlan을 작성하고, 충족이면 Manager가 원문 그대로 archive한다. 연결 계획이 없는 직접 구현도 실제 근거로 판단한다.
- 관련 계획의 실제 MERGED 확인, 미완료 수동 제출·문서 draft, 취소된 source 관계의 불확실성, 이동 충돌, 판단 캐시의 입력 결합과 원격 게시 확인은 기존 보호 조건을 유지한다. 게시 전 입력 변화에 따른 이동 철회는 완료된 archive의 복원이 아니다.
- archive된 Requirement는 Planner의 재평가·새 작업·프로젝트 완료 대기 입력으로 사용하지 않으며 자동으로 복원하지 않는다. 기존 ExecPlan의 source 조회·검증을 위한 읽기 호환성은 유지하지만 새 ExecPlan은 archived Requirement를 source로 사용할 수 없다.
- 과거 감사 기록은 보존하되 새 감사를 생성하거나 완료를 막지 않는다. 미게시된 과거 자동 복원은 원문·소유 범위가 확인될 때 archive로 되돌리고 현재 입력을 재평가한다. 사용자 수정·충돌이 있으면 덮어쓰지 않고 기존 작업과 이유를 보존한다. 저장된 복원 게시 후보도 새로 게시하지 않는다.

## Acceptance Criteria

- Worker의 ARCHITECTURE 수정은 허용된다. Planner는 `docs/PRODUCT.md`·`docs/ARCHITECTURE.md`·Requirement를 직접 수정하지 않으며 ExecPlan은 허용된 mode·snapshot 범위에서 생성·수정한다. Reviewer는 파일을 수정하지 않는다.
- source로 참조하는 비종료 계획·draft가 있으면 Requirement를 archive하지 않는다. 연결 작업이 완료되거나 없는 경우 일반 Planner의 전체 기준 판단 없이는 이동하지 않는다.
- 미충족 판단에는 후속 ExecPlan이 연결되고 충족 판단에는 최신 구현·검증·제품·설계 문서 근거가 있으며, Manager는 검증한 원문만 원격 게시한다.
- archived Requirement와 과거 감사 상태는 일반 Planner 호출이나 프로젝트 완료 대기를 만들지 않으며 활성 경로로 자동 이동하지 않는다.
- 기존 archived source는 조회·검증할 수 있지만 새 계획의 archived source는 거부된다.
- 과거 복원의 미게시 작업·저장된 게시 후보를 재개해도 새 복원을 게시하지 않는다. 안전한 철회는 재시작 가능하고 사용자 수정·감사 기록을 보존한다.
- 관련 targeted unit test로 archive gate, 보관 후 미평가, 기존 source 호환성과 복원 중단 복구를 확인한다.

## Product Impact

- 구현과 PRODUCT·ARCHITECTURE가 현재 제품·설계 기준을 유지한다.
- Requirement archive는 완료한 변경의 보관이며 새 작업이나 자동 복원의 입력이 아니다.

## Non-Goals

- ExecPlan 실행 상태·dependency·검증·Review·사용자 Merge 조건을 완화하지 않는다.
- 과거 archive나 감사 데이터를 삭제하지 않는다.
- Planner의 `docs/PRODUCT.md`·`docs/ARCHITECTURE.md`·Requirement 직접 수정, 자동 Requirement 내용 변경 또는 프로젝트 루트 `AGENTS.md` 변경의 Agent 자기 승인을 허용하지 않는다.

## Open Questions

- 없음
