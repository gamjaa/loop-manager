# [R-041] Safe Adoption of User-Published ExecPlans

## Context

현재 Loop Manager는 Planner가 새 ExecPlan 파일을 원격 기본 branch에 게시한 직후 같은 transaction에서 SQLite 실행 row를 등록한다. 사용자가 새 ExecPlan을 직접 기본 branch에 push하면 파일과 내용이 유효해도 SQLite에 해당 ID가 없어 `unknown-plan-id`로 처리되고, 작업이 중단된다. G-077을 직접 게시했을 때 이 상태가 발생했다. 사용자 직접 게시와 Planner 게시의 출처를 구분하면서도, 유효한 새 계획을 누락 없이 등록할 수 있어야 한다.

## Requirement

Loop Manager는 사용자가 원격 기본 branch에 직접 게시한 신규 active ExecPlan을 안전한 계획 목록 동기화 시점에 발견하면, 원격 revision과 파일의 identity 및 전체 계획 목록을 검증한 뒤 SQLite에 미착수 READY 계획으로 등록해야 한다. 기존 Planner 게시 절차와 실행 이력의 소유권은 유지한다.

진행 중인 일반·유지보수 Planner 게시 intent가 있으면 먼저 기존 게시 복구 절차로 처리한다. 미해결 intent와 무관한 신규 ID를 외부 게시로 추정하지 않는다. 그 뒤 등록은 현재 SQLite에 있는 모든 ExecPlan ID가 원격 목록에 남아 있고, 새 ID가 저장된 active·archive·취소 이력의 최대 ID 다음부터 중복 없이 연속하며, 경로·제목·source·dependency graph와 파일 형식이 유효한 경우에만 허용한다. 새 계획에 branch, worktree, Agent session, PR 또는 기존 실행 이력이 없음을 확인한다. 추가된 파일의 원격 commit, 경로와 내용 지문을 최초 게시 감사 정보로 기록한다. 같은 revision을 다시 관측하거나 게시 뒤 DB 반영 전에 중단돼도 중복 row 없이 같은 결과로 수렴해야 한다.

기존 ID의 파일 누락·교체 의심, 역사적 ID 재사용, 신규 ID의 불연속·충돌, 새 archive 계획, 형식·source·dependency 오류 또는 원격 revision과 SQLite 소유권의 동시 변경은 자동 등록하지 않는다. 이 경우 원인 ID와 파일, 충돌 사실을 진단하고 다른 계획의 실행 상태를 변경하지 않는다. 외부에서 추가된 계획을 수용해도 Planner의 성공 입력 지문이나 유지보수 평가 완료 상태를 성공으로 간주하지 않는다.

## Acceptance Criteria

- 원격 기본 branch에 유효한 새 active ExecPlan 하나 또는 연속된 여러 개를 직접 push하면, 다음 안전한 계획 목록 동기화에서 각 계획이 정확히 한 번 READY로 등록되고 정상적인 priority·depends_on 선택을 따른다.
- 등록된 계획의 최초 게시 경로·내용 지문·원격 revision과 초기 Worker level은 파일에 맞고, 기존 계획의 상태·실패 이력·Agent session·branch·worktree·PR 연결은 보존된다.
- Planner가 push한 계획은 저장된 게시 intent로 복구하고 외부 게시로 중복 수용하지 않는다. 원격 push 후 DB 반영 전 중단, 같은 revision 재처리 및 동시 원격 변경에서 중복 row나 부분 등록이 발생하지 않는다. 원격 revision 또는 SQLite 소유권 snapshot이 달라지면 최신 입력으로 다시 판단한다.
- 기존 ID 누락과 미등록 ID가 동시에 있거나, 신규 ID가 불연속·재사용·중복되거나, 새 파일이 archive에만 있거나, 전체 catalog가 유효하지 않으면 자동 등록하지 않고 원인 파일을 포함한 프로젝트 범위 진단을 남긴다.
- 사용자 게시 계획의 수용만으로 Planner 성공 지문, 유지보수 cycle 또는 기존 PR Merge 상태를 전진시키지 않는다. 다른 등록 프로젝트에도 같은 검증 기준을 적용한다.

## Product Impact

- 사용자가 직접 게시한 유효한 신규 ExecPlan도 안전 검증 뒤 실행 대기 목록에 들어간다.
- 위험하거나 출처가 모호한 계획 변경은 계속 차단된다.

## Non-Goals

- 계획 파일만으로 기존 실행 상태를 복원하거나 MERGED 상태를 추정하지 않는다.
- 기존 ID의 삭제·교체, historical ID 재사용, archive-only 신규 계획을 자동 수용하지 않는다.
- 검증 없이 원격 파일을 Worker 실행으로 바로 연결하지 않는다.

## Open Questions

- 없음
