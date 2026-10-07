# [R-033] Minimal Worker Session Prompts

## Context

Loop Manager는 Codex CLI 또는 Claude Code의 Worker 세션을 재개하지만, 작업 prompt의 역할 규칙·ExecPlan 경로·작업 범위·`AGENTS.md` 읽기 지시가 반복되면 새 정보 없이 문맥과 입력량만 늘어난다. CLI가 같은 세션의 대화를 관리하는 동안 작업별 입력은 새 사실에 한정해야 한다. 반대로 Model 변경이나 세션 복구로 새 세션을 시작할 때는 미완료 작업을 이어갈 최소 문맥이 필요하다.

## Requirement

Loop Manager는 R-064의 고정된 common+Worker 역할 instruction을 매 호출의 실행 설정에 유지한다. 같은 Worker 세션의 사용자 prompt에는 최초 작업 지시와 변경되지 않은 작업 문맥을 반복 전송하지 않고, 직전 Worker turn 이후 새로 확인된 작업 변화와 교정 요청만 전달해야 한다. verify·Review 피드백은 기존의 중복 없는 delta 계약을 따르며, archive 후보·guidance 결정·비검증 오류도 새롭거나 달라진 내용만 전달해야 한다. 기존 세션의 대화 보존과 압축은 Codex CLI 또는 Claude Code에 맡긴다.

실제 provider 또는 Model 변경이나 저장된 세션을 사용할 수 없어 새 Worker 세션을 시작하면, 같은 작업의 고정 원본에서 common+Worker instruction을 다시 전달한다. 사용자 prompt에는 결과 형식·선택된 ExecPlan과 worktree 위치·미완료 작업의 요점·미해결 피드백만 한 번 전달하고 정적 역할·권한·검증 규칙이나 이전 대화·검증 원문 전체를 복사하지 않아야 한다.

저장소 지침은 선택한 CLI의 프로젝트 지침 로딩을 이용한다. Loop Manager는 `AGENTS.md` 본문을 prompt에 복사하거나 같은 세션에 매번 읽으라고 지시하지 않아야 한다. 사용 중인 CLI 환경에서 지침 자동 로딩을 사용할 수 없을 때만 새 세션 시작 메시지에서 한 번 확인을 요청한다. 이 축약으로 Worker의 허용 범위, 승인된 guidance, 테스트·검증 책임과 결과 형식 요구가 빠져서는 안 된다.

## Acceptance Criteria

- 같은 Worker 세션의 일반 수정, effort 변경, verify·Review 교정에서 사용자 prompt에는 역할 규칙·ExecPlan·변경되지 않은 작업 범위와 이미 전달한 결과가 반복되지 않고 새 변경 및 교정만 전달된다. 호출 설정의 고정 instruction은 유지된다.
- archive 후보, guidance 결정 또는 비검증 오류가 새로 생기거나 바뀌면 같은 세션에 필요한 내용이 한 번 전달된다. 변경이 없으면 같은 내용을 재전송하지 않는다.
- provider·Model 변경 또는 session-unavailable fallback의 새 세션에도 같은 고정 원본의 common+Worker instruction을 전달한다. 사용자 prompt에는 현재 작업의 결과 형식·계획·작업 공간 위치·미완료 상태만 담으며 역할 규칙·이전 대화·Worker 결과·verify·Review 원문 전체는 포함하지 않는다.
- 저장소 지침을 자동 로딩하는 CLI에서는 `AGENTS.md` 본문이나 매 turn 읽기 지시를 prompt에 넣지 않는다. 자동 로딩이 없는 환경에서는 새 세션 시작 시에만 한 번 확인을 요청한다.
- prompt가 짧아져도 승인된 guidance의 적용, Worker 범위 제한, targeted test와 전체 verify의 책임 분리 및 최종 결과 형식은 유지된다.
- 동일한 정보가 비검증 오류와 validation feedback 등 두 경로로 중복 전달되지 않는다.

## Product Impact

- 같은 Worker 세션에는 새 사실만 전달하고, 새 세션에는 미완료 작업을 잇는 데 필요한 정보만 다시 전달한다.

## Non-Goals

- Loop Manager가 CLI 대화를 별도로 요약·압축하거나 전체 대화를 재구성하지 않는다.
- 세션의 실제 prompt cache 적중을 보장하거나 Model 선택·세션 교체 정책을 변경하지 않는다.
- Worker의 작업 권한, 검증 범위 또는 사용자 승인 절차를 완화하지 않는다.

## Open Questions

- 없음
