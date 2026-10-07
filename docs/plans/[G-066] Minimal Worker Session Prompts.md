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
  - G-134
  - G-135
  - G-136
  - G-137
  - G-138
  - G-139
  - G-140
  - G-141
  - G-142
  - G-143
  - G-144
  - G-145
  - G-146
  - G-147
  - G-160
  - G-171
  - G-173
  - G-180
  - G-181
complexity: high
risk: medium
priority: low
---

## Purpose

현재 `worker_prompt`는 재개하는 Worker 세션에도 역할, ExecPlan 경로, 범위와 테스트 규칙을 매번 보내고 `loop_manager_internal/execution/execution_worker_handler.py`는 archive 후보·guidance 문맥·비검증 오류를 반복 전달한다. 세션이 이미 받은 내용은 생략하고 새 세션에는 미완료 작업을 잇는 최소 문맥만 제공한다.

## Acceptance Criteria

- 최초 Worker turn에는 역할·결과 형식, 선택된 ExecPlan·worktree, 범위·승인된 guidance 및 targeted test와 전체 verify 책임을 완전하게 전달한다. 같은 provider·Model 세션의 일반 수정, effort 변경, verify·Review 교정에서는 변경되지 않은 지시와 이미 전달한 결과를 반복하지 않고 새 사실과 교정만 보낸다.
- archive 후보, guidance 결정 또는 비검증 오류가 새로 생기거나 바뀌면 해당 세션에 한 번 전달한다. 변화가 없으면 재전송하지 않고 validation feedback delta와 비검증 오류로 동일 내용을 두 번 보내지 않는다.
- provider·Model 교체 또는 session-unavailable fallback으로 새 세션이 시작되면 현재 역할·결과 형식, 계획·작업 공간, 미완료 작업과 미해결 피드백만 간결히 재구성한다. 이전 대화, Worker 결과와 verify·Review 원문 전체는 보내지 않는다.
- 선택한 CLI가 프로젝트 지침을 자동 로딩하면 `AGENTS.md` 본문이나 매 turn 읽기 지시를 prompt에 넣지 않는다. 자동 로딩이 불가능하면 새 세션 첫 메시지에서만 지침 확인을 요청한다.
- 축약 뒤에도 Worker 권한, AGENTS guidance 승인, 결과 protocol, targeted test 범위와 전체 verify 분리, 비밀값 가림 및 세션·작업 공간 복구 계약은 유지된다. 전송 도중 중단·재시작해도 새 정보를 누락하지 않는다.
- 수동 complete/submit 수락 뒤 자동 Worker 교정에도 같은 초기·delta·복구 계약을 적용한다. 수동 문맥을 덧붙이는 경로가 이미 전달한 결과나 verify·Review 원문을 다시 넣지 않는다. verify 성공 출력은 Worker에게 전달하지 않으며 미해결 실패는 기존 진단 helper의 필요한 피드백으로 전달한다. 수동 제출의 전체 G·후보 R·미확인 외부 테스트와 사용자 생략을 보존하고 실제 Worker 성공으로 합성하지 않는다.

## Implementation

- `loop_manager_internal/agents/agent_prompts.py`의 `worker_prompt`는 세션 초기·복구 문맥과 기존 세션 delta의 표현을 맡는다. G-147이 공통 호출 경계에 보존한 지침 버전을 재사용하고 G-058의 품질 지침을 사용자 메시지에 중복하지 않는다. 프로젝트 지침 자동 로딩 여부는 기존 provider 호출 경계에서 확인하며, 불가능한 환경에서만 새 세션에 한 번 확인을 요청한다.
- `loop_manager_internal/execution/execution_worker_handler.py`의 호출별 prompt 구성은 `worker_session.py`의 `SessionDecision`·`SessionTransition`과 실제 retry·fallback의 session identity를 사용한다. 같은 resolved provider·Model의 effort 변경은 재개이고 provider·Model 교체, legacy 미결합 세션과 session-unavailable fallback은 최소 문맥 복구다. `agents/agent_invocation.py`의 retry와 `agents/invocation_service.py`의 실제 호출 기록·격리 책임을 중복 구현하지 않는다.
- archive 후보·승인 guidance·비검증 오류는 현재 준비 snapshot과 승인·오류 상태에서 필요한 내용의 변경만 비교한다. 기존 `storage/validation_feedback_state.py`의 `prepare`·`bind_session`·`delivered`와 `execution/validation_feedback.py`의 `render_delta`·`is_validation_error`를 재사용하고, 나머지 전달 상태만 기존 storage·transaction 경계에 최소한으로 추가한다. 호출 전 전달할 revision을 고정하고 실제 session에 결합하며, 기존 범위·guidance·Worker 결과 검증을 통과한 전달만 확인한다. session 시작 관측만으로 cursor를 전진시키지 않고 중단·실패·저장 실패에는 미확인 내용을 보존해 재전송한다. 호출 이후 새로 생긴 정보까지 확인 처리하지 않는다.
- 교체 세션에는 저장 상태의 미완료 요점과 G-065의 미해결 진단만 구성하고 이전 `worker_result`·검증 원문은 넣지 않는다. 정적 코드 조회 안내의 초기 전달과 현재 rebase 후보·ExecPlan 변경에 필요한 재확인 지시는 기존 경계에 연결한다. 축약을 후보 검증 성공 재사용으로 취급하지 않으며 Requirement 완료 판단·이동은 현재 Planner·Manager 경계를 유지한다. archive 후보 변경 전달을 이유로 Worker의 Requirement 이동을 다시 요구하지 않는다.

- `execution/manual_candidate.py`의 최종 Worker prompt도 같은 전달 경계에서 확인한다. 저장된 원문·지문·단계 결과는 보존하고 기존 focused feedback과 delta를 재사용한다. Reviewer의 최종 후보 근거 전달은 G-197에 남기며 생략 identity나 Review 성공 재사용을 새로 설계하지 않는다.

## Validation

- Worker는 `tests/test_execution_worker_phase.py`의 prompt·fallback, `tests/test_worker_session.py`의 전이 및 `tests/test_validation_feedback_state.py`의 전달 확인·재개 중 변경한 경계의 최소 targeted unit case만 실행한다. `tests/support/execution.py`의 기존 fixture·Agent double을 재사용한다. 기존 재개 prompt의 역할 반복과 복구 prompt의 이전 결과 포함을 요구하는 assertion은 이 계획의 초기 전달·delta·최소 복구 계약에 맞춰 갱신한다.
- 최초 turn, 같은 세션의 일반 수정·effort 변경·verify·Review 교정, archive·guidance·비검증 오류 변경 및 두 채널 중복 방지, resolved provider·Model 교체·legacy 복구·session-unavailable fallback을 검증한다. 자동 지침 로딩 유무와 G-147의 고정 공통 버전 재사용도 확인하며 실제 Agent를 호출하지 않는다.
- session 시작 직후 timeout, 잘못된 Worker 결과, fallback 시작 실패, 전달 확인 전후 중단·재시작·저장 실패와 호출 뒤 새 revision 발생에서 미확인 내용이 누락되지 않고 확인된 동일 내용을 반복하지 않는지 검증한다. 비밀값 가림과 승인 문맥은 `tests/test_validation_feedback.py`·`tests/test_execution_guidance.py`, 결과 형식은 `tests/test_worker_result_contract.py`의 직접 관련 case를 변경 시 연결한다.
- 전체 verify는 Agent 범위·guidance 사전 승인, Worker 결과·사용자 결정 protocol, 피드백 audit·cursor, 실제 호출·session 복구와 rebase 후보 재확인 회귀를 유지한다. 기존 unittest manifest 수집·Agent 호출 차단을 사용하며 전체 verify와 Reviewer의 책임은 `docs/PLANS.md`를 따른다.

- 기존 manual fixture로 수동 Worker prompt의 최소 unit case를 확인한다. Review 실패 뒤 저장된 verify PASS 출력, verify 실패 뒤 raw 출력 재추가, 동일 session 재개와 새 session fallback에서 중복·원문 복제가 없고 필요한 실패 진단·전체 G·후보 R은 남는지 검증한다. Manager 전체 verify는 생략 무효화·실제 Worker 교정·원문 및 실패 지문 보존과 수동/일반 Reviewer 입력의 회귀를 확인한다.

## Progress

- [x] 2026-10-07 기준 94e9e0b의 수동 준비 병행 변경, R-033·R-077, 최종 수동 prompt와 G-197을 정적으로 대조함. 활성 R-033의 기존 최소 전달 목표를 수동 교정 경계에 명시했으며 구현·테스트 실행은 하지 않음.

- [x] 2026-10-05 지정 READY snapshot의 목표·범위와 최신 prompt·session·feedback 관련 회귀 경계를 문서·원본으로 재검증함 (구현·테스트 실행 아님)
- [ ] 초기·재개·교체 Worker prompt 경계 분리
- [ ] 새 archive·guidance·오류 전달 cursor 연결
- [ ] 관련 targeted 회귀 사례 추가

## Decision Log

- 2026-10-07 mutable READY token 5b96f9d2d8d50ffcdf25f4829238a01aa03f6a52846d0435d8b315ce6ad09aa3, 기준 94e9e0b0825d3bd2cbee5e75aac06f9f2a335e0f 및 원본 hash 28864fe6b250133c147d7876020166ac5705f27d520e92a4cb1f6d75b6cbd522를 확인했다. R-032 복원 철회를 존중하며 archived 원문을 활성 source나 새 후속 계획의 근거로 사용하지 않는다. 남은 수동 Worker 원문 재전송은 활성 R-033의 기존 목표와 PRODUCT의 verify 성공 출력 미전달, R-077의 자동 교정 계약에 포함한다. Merge된 G-065의 formatter는 재구현하지 않는다. ID·sources·기존 dependency·목표·metadata를 보존한다.
- complexity high: 전달 revision 고정·실제 session 결합·정상 반환·fallback·중단 및 호출 중 새 정보 발생 사이에서 전달 확인을 안전하게 확정하는 판단이 남아 있다. risk medium: 잘못된 cursor나 축약은 교정을 누락·중복하지만 저장된 원문·피드백·작업물과 기존 scope·후보 gate에서 복구할 수 있다. 수동 준비의 병렬화나 Reviewer 근거 전달은 이 계획의 새 목표가 아니다.

- 2026-10-05 일반 mutable READY snapshot `202cd7f3a965a13572b02c54e2fefa108d58d6d661ef94298e7d4a2ed22ef059`에 따라 ID와 기존 목표를 보존했다. 새 Review 교정·문서 권한·User Decision·결과 정규화·verify 입력 확인의 전달 계약이 먼저 확정되도록 G-160·G-171·G-173·G-180·G-181을 dependency에 추가했다. 이 계획이 해당 기능을 중복 구현하지 않으며 session delta는 확정된 계약을 따른다.

- CLI가 보존하는 대화를 Loop Manager가 별도로 압축하거나 재구성하지 않는다. G-058의 공통 지침과 G-065의 verify 피드백을 전제로 한다.
- 2026-10-02 사용자 결정: 리팩터링을 먼저 진행하므로 G-127 Merge 이후 시작한다. SQLite에서 미착수·무소유 READY임을 확인했으며 원래 목표·Acceptance Criteria와 기존 dependency는 유지한다. 실행 시 G-119~G-125에서 정리된 prompt·session·handler와 테스트의 최신 경로를 확인한다.

- 2026-10-03 사용자 요청에 따른 대기 계획 전체 점검: R-033과 현재 PRODUCT/R-050에 목표 충돌이 없음을 확인했다. G-119~G-129 이후의 실제 prompt·Worker handler·session·feedback 및 테스트 경로로 갱신했다. 직접 Codex 실행과 신규·재개·fallback의 역할별 내장 sandbox 정책은 prompt 축약과 독립적으로 유지한다. SQLite와 실행 상태는 수정하지 않는다.

- 2026-10-05 post-refactor revalidation: source revision `c203b1437537b868862a104e668586d392c5ad0a`의 `agents/agent_prompts.py:165`, `execution/execution_worker_handler.py:239`·`:334`·`:545`, `execution/worker_session.py:65`, `storage/validation_feedback_state.py:211`를 `loop_manager_internal/` 아래 원본과 관련 테스트로 대조했다. 세션 전이·검증 피드백 cursor는 이미 분리돼 있으므로 재사용하고 나머지 전달 상태만 확장한다. ID·경로·title·sources·complexity·risk·priority·Purpose·Acceptance Criteria와 기존 dependency는 보존한다.
- G-147의 공통 지침 주입·버전 보존을 먼저 Merge하도록 dependency를 추가했다. 공통 규칙의 별도 주입과 사용자 turn의 축약을 연결하기 위한 순서이며 G-147의 구현·등록·배포 범위를 가져오지 않는다. 이전 초기 메시지 품질 지침 포함 방향은 해당 공통 버전 재사용으로 연결하고, R-033의 반복 방지·최소 복구·승인·권한·검증 목표는 유지한다. Requirement retirement, 공통 verify 규약과 후보 성공 재사용은 별도 책임으로 남기며 이번 재검증은 실행 상태나 성공 증거를 만들지 않는다.
