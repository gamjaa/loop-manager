---
id: G-066
title: Minimal Worker Session Prompts
sources:
  - docs/requirements/[R-033] Minimal Worker Session Prompts.md
depends_on:
  - G-058
  - G-065
  - G-127
  - G-129
complexity: high
risk: medium
priority: low
---

## Purpose

현재 `worker_prompt`는 재개하는 Worker 세션에도 역할, ExecPlan 경로, 범위와 테스트 규칙을 매번 보내고 `execution_handlers.py`는 archive 후보·guidance 문맥·비검증 오류를 반복 전달한다. 세션이 이미 받은 내용은 생략하고 새 세션에는 미완료 작업을 잇는 최소 문맥만 제공한다.

## Acceptance Criteria

- 최초 Worker turn에는 역할·결과 형식, 선택된 ExecPlan·worktree, 범위·승인된 guidance 및 targeted test와 전체 verify 책임을 완전하게 전달한다. 같은 provider·Model 세션의 일반 수정, effort 변경, verify·Review 교정에서는 변경되지 않은 지시와 이미 전달한 결과를 반복하지 않고 새 사실과 교정만 보낸다.
- archive 후보, guidance 결정 또는 비검증 오류가 새로 생기거나 바뀌면 해당 세션에 한 번 전달한다. 변화가 없으면 재전송하지 않고 validation feedback delta와 비검증 오류로 동일 내용을 두 번 보내지 않는다.
- provider·Model 교체 또는 session-unavailable fallback으로 새 세션이 시작되면 현재 역할·결과 형식, 계획·작업 공간, 미완료 작업과 미해결 피드백만 간결히 재구성한다. 이전 대화, Worker 결과와 verify·Review 원문 전체는 보내지 않는다.
- 선택한 CLI가 프로젝트 지침을 자동 로딩하면 `AGENTS.md` 본문이나 매 turn 읽기 지시를 prompt에 넣지 않는다. 자동 로딩이 불가능하면 새 세션 첫 메시지에서만 지침 확인을 요청한다.
- 축약 뒤에도 Worker 권한, AGENTS guidance 승인, 결과 protocol, targeted test 범위와 전체 verify 분리, 비밀값 가림 및 세션·작업 공간 복구 계약은 유지된다. 전송 도중 중단·재시작해도 새 정보를 누락하지 않는다.

## Implementation

- `loop_manager.py`의 Worker prompt를 새 세션 초기 문맥과 기존 세션 delta로 분리하고, G-058의 공통 품질 지침은 초기 문맥에만 포함한다. CLI별 프로젝트 지침 자동 로딩 가능 여부를 기존 Agent 호출 경계에서 결정한다.
- `execution_handlers.py`와 `worker_session.py`의 실제 session decision을 기준으로 archive 후보·guidance 결정·비검증 오류의 변경을 비교한다. 필요한 최소 per-session 전달 cursor를 SQLite에 보존하고 Agent가 새 문맥을 수용한 뒤 전진시켜 중단 시 재전송 가능하게 한다. verify·Review 피드백은 기존 `validation_feedback_state.py` cursor를 재사용한다.
- 세션 교체 시 저장된 상태에서 미완료 요점만 구성하고 이전 `worker_result`·검증 원문은 prompt에서 제거한다. G-065의 집중된 verify 진단만 미해결 피드백으로 연결한다.

## Validation

- Worker는 `tests/test_execution_handlers.py`의 Worker turn·session 전이 사례, `tests/test_validation_feedback.py`의 prompt 사례와 `tests/test_loop_manager.py`의 역할·결과 형식 사례에 해당하는 최소 targeted unit test만 실행한다.
- 최초 turn, 동일 세션 일반 수정·effort 변경·verify·Review 교정, archive·guidance·비검증 오류 변경과 중복 없음, provider·Model 변경·session-unavailable fallback, 전송 중단·재시작 및 자동 지침 로딩 유무를 검증한다.
- 전체 verify는 Agent 범위·guidance 승인, Worker 결과 검증, feedback cursor와 CLI session 복구가 유지되는지 확인한다.

## Progress

- [ ] 초기·재개·교체 Worker prompt 경계 분리
- [ ] 새 archive·guidance·오류 전달 cursor 연결
- [ ] 관련 targeted 회귀 사례 추가

## Decision Log

- CLI가 보존하는 대화를 Loop Manager가 별도로 압축하거나 재구성하지 않는다. G-058의 공통 지침과 G-065의 verify 피드백을 전제로 한다.
- 2026-10-02 사용자 결정: 리팩터링을 먼저 진행하므로 G-127 Merge 이후 시작한다. SQLite에서 미착수·무소유 READY임을 확인했으며 원래 목표·Acceptance Criteria와 기존 dependency는 유지한다. 실행 시 G-119~G-125에서 정리된 prompt·session·handler와 테스트의 최신 경로를 확인한다.
