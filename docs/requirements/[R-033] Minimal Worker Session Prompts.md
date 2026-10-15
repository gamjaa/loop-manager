# [R-033] Minimal Worker Session Prompts

## Summary

같은 Worker 세션의 사용자 prompt에는 직전 turn 이후 새 작업 변화·미해결 교정만 전달하고, 고정된 common+Worker instruction은 매 호출 유지한다. provider·Model 변경이나 session 복구로 새 세션을 시작할 때만 선택 작업·작업 공간·미완료 요점을 최소 문맥으로 전달하며 결과 형식·권한·검증 책임을 보존한다.

## Context

Loop Manager는 Codex CLI 또는 Claude Code의 Worker 세션을 재개하지만, 작업 prompt의 역할 규칙·ExecPlan 경로·작업 범위가 반복되면 새 정보 없이 문맥과 입력량만 늘어난다. CLI가 같은 세션의 대화를 관리하는 동안 작업별 입력은 새 사실에 한정해야 한다. 반대로 Model 변경이나 세션 복구로 새 세션을 시작할 때는 미완료 작업을 이어갈 최소 문맥이 필요하다.

## Requirement

Loop Manager는 R-064의 고정된 common+Worker 역할 instruction을 매 호출의 실행 설정에 유지한다. 같은 Worker 세션의 사용자 prompt에는 최초 작업 지시와 변경되지 않은 작업 문맥을 반복 전송하지 않고, 직전 Worker turn 이후 새로 확인된 작업 변화와 교정 요청만 전달해야 한다. verify·Review 피드백은 기존의 중복 없는 delta 계약을 따르며, archive 후보·비검증 오류도 새롭거나 달라진 내용만 전달해야 한다. 기존 세션의 대화 보존과 압축은 Codex CLI 또는 Claude Code에 맡긴다.

실제 provider 또는 Model 변경이나 저장된 세션을 사용할 수 없어 새 Worker 세션을 시작하면, 같은 작업의 고정 원본에서 common+Worker instruction을 다시 전달한다. 사용자 prompt에는 결과 형식·선택된 ExecPlan과 worktree 위치·미완료 작업의 요점·미해결 피드백만 한 번 전달하고 정적 역할·권한·검증 규칙이나 이전 대화·검증 원문 전체를 복사하지 않아야 한다. 실제 worktree와 계획 목표를 기준으로 이미 수행한 변경·미해결 실패·다음 작업을 확인해 이어가며 정상 세션 재개에 전체 상태 요약을 요구하지 않는다.


## Acceptance Criteria

- 같은 Worker 세션의 일반 수정, effort 변경, verify·Review 교정에서 사용자 prompt에는 역할 규칙·ExecPlan·변경되지 않은 작업 범위와 이미 전달한 결과가 반복되지 않고 새 변경 및 교정만 전달된다. 호출 설정의 고정 instruction은 유지된다.
- archive 후보 또는 비검증 오류가 새로 생기거나 바뀌면 같은 세션에 필요한 내용이 한 번 전달된다. 변경이 없으면 같은 내용을 재전송하지 않는다.
- provider·Model 변경 또는 session-unavailable fallback의 새 세션에도 같은 고정 원본의 common+Worker instruction을 전달한다. 사용자 prompt에는 현재 작업의 결과 형식·계획·작업 공간 위치·미완료 상태만 담으며 역할 규칙·이전 대화·Worker 결과·verify·Review 원문 전체는 포함하지 않는다.
- 재시도는 미해결 검증 실패와 관련 변경을 우선하고 기존 성공 작업을 불필요하게 반복하거나 범위 밖 기능으로 확대하지 않는다. 필요한 리팩토링·코드 제거는 허용하며 검증 없이 성공을 보고하지 않는다.
- prompt가 짧아져도 Worker 범위 제한, 지침 snapshot, targeted test와 전체 verify의 책임 분리 및 WORKER_OUTCOME·COMMIT_TITLE·PR_SUMMARY·TARGETED_TESTS·FAILED_COMMAND·RERUN 계약은 유지된다.
- 동일한 정보가 비검증 오류와 validation feedback 등 두 경로로 중복 전달되지 않는다.

## Product Impact

- 같은 Worker 세션에는 새 사실만 전달하고, 새 세션에는 미완료 작업을 잇는 데 필요한 정보만 다시 전달한다.

## Non-Goals

- Loop Manager가 CLI 대화를 별도로 요약·압축하거나 전체 대화를 재구성하지 않는다.
- 세션의 실제 prompt cache 적중을 보장하거나 Model 선택·세션 교체 정책을 변경하지 않는다.
- Worker의 작업 권한, 검증 범위 또는 사용자 승인 절차를 완화하지 않는다.

## Open Questions

- 없음
