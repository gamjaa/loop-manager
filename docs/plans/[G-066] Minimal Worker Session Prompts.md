---
id: G-066
title: Minimal Worker Session Prompts
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-033] Minimal Worker Session Prompts.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
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
  - G-180
  - G-181
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
  - G-242
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: medium
priority: low
---

## Purpose

현재 `worker_prompt`와 `loop_manager_internal/execution/execution_worker_handler.py`에는 ExecPlan 경로·작업 범위·archive 후보·비검증 오류의 반복 전달이 남아 있다. 공통·Worker 역할 규칙은 별도 instruction으로 전달하고, 사용자 메시지는 세션이 이미 받은 내용을 생략하며 새 세션에는 미완료 작업을 잇는 최소 문맥만 제공한다.

## Acceptance Criteria

- 최초 Worker turn에는 고정된 공통·Worker 역할 instruction과 작업별 결과 형식·선택된 ExecPlan·worktree·범위·승인 문맥을 완전하게 전달한다. 역할·권한·targeted test와 전체 verify 책임은 instruction에 유지하고 사용자 메시지에 복사하지 않는다. 같은 provider·Model 세션의 일반 수정, effort 변경, verify·Review 교정에서는 변경되지 않은 작업 문맥과 이미 전달한 결과를 반복하지 않고 새 사실과 교정만 보낸다.
- archive 후보 또는 비검증 오류가 새로 생기거나 바뀌면 해당 세션에 한 번 전달한다. 변화가 없으면 재전송하지 않고 validation feedback delta와 비검증 오류로 동일 내용을 두 번 보내지 않는다. 폐지된 CLI 파일 승인 요청·patch 대조를 복원하지 않는다.
- provider·Model 교체 또는 session-unavailable fallback으로 새 세션이 시작되면 같은 작업에 고정된 공통·Worker 역할 instruction을 재사용하고 사용자 메시지에는 결과 형식·계획·작업 공간·미완료 작업·미해결 피드백만 간결히 재구성한다. 이전 대화, Worker 결과와 verify·Review 원문 전체는 보내지 않는다.
- 프로젝트 지침은 Agent CLI가 해석한다. 최초·재개·fallback 사용자 prompt에 루트 `AGENTS.md`의 읽기·승인·복원 지시를 넣지 않고 존재·인코딩·내용·자동 로딩 여부를 Manager의 추가 실행 gate로 삼지 않는다.
- 축약 뒤에도 Worker 권한, 일반 draft·User Decision 보호, 고정 원본 묶음과 역할별 실제 instruction, 결과 protocol, targeted test 범위와 전체 verify 분리, 비밀값 가림 및 세션·작업 공간 복구 계약은 유지된다. 과거 파일 승인 감사·작업물·session은 보존한다. 전송 도중 중단·재시작해도 새 정보를 누락하지 않는다.
- 수동 complete/submit 수락 뒤 자동 Worker 교정에도 같은 초기·delta·복구 계약을 적용한다. 수동 문맥을 덧붙이는 경로가 이미 전달한 결과나 verify·Review 원문을 다시 넣지 않는다. verify 성공 출력은 Worker에게 전달하지 않으며 미해결 실패는 기존 진단 helper의 필요한 피드백으로 전달한다. 수동 제출의 전체 G·후보 R·미확인 외부 테스트와 사용자 생략을 보존하고 실제 Worker 성공으로 합성하지 않는다.
- R-082의 프로젝트 일반 planning·개정 게시·DB 적용과 필요한 rebase를 마친 최신 ExecPlan을 최초·재개 Worker의 기준으로 전달한다. 상위 명세의 관련성·개정을 작업별 prompt에서 다시 판단시키지 않는다. 유효한 planning 결과가 작업 조건 유지를 확인했다면 source 지문·SHA 전진이나 순수 진행 기록만으로 Worker 호출·문맥 재전송을 만들지 않는다. 실제 조건 변경·미완료 보완과 기존 역할의 검증 입력은 보존하고 확인된 전달 cursor만 전진시킨다.

## Implementation

- `loop_manager_internal/agents/agent_prompts.py`의 `worker_prompt`는 세션 초기·복구 문맥과 기존 세션 delta의 표현을 맡는다. `agents/invocation_service.py`·`agents/common_guidance.py`의 고정 원본과 역할별 별도 instruction 전달을 재사용한다. version 2 Worker의 기존 Ponytail 상시 포함과 과거 snapshot 보존도 유지한다. 정적 품질·역할·권한·검증 규칙은 사용자 메시지에 중복하지 않으며 프로젝트 지침의 해석은 CLI에 맡긴다.
- `loop_manager_internal/execution/execution_worker_handler.py`의 호출별 prompt 구성은 `worker_session.py`의 `SessionDecision`·`SessionTransition`과 실제 retry·fallback의 session identity를 사용한다. 같은 resolved provider·Model의 effort 변경은 재개이고 provider·Model 교체, legacy 미결합 세션과 session-unavailable fallback은 최소 문맥 복구다. `agents/agent_invocation.py`의 retry와 `agents/invocation_service.py`의 실제 호출 기록·격리 책임을 중복 구현하지 않는다.
- archive 후보·비검증 오류는 현재 준비 snapshot과 오류 상태에서 필요한 내용의 변경만 비교한다. 기존 `storage/validation_feedback_state.py`의 `prepare`·`bind_session`·`delivered`와 `execution/validation_feedback.py`의 `render_delta`·`is_validation_error`를 재사용하고, 나머지 전달 상태만 기존 storage·transaction 경계에 최소한으로 추가한다. 호출 전 전달할 revision을 고정하고 실제 session에 결합하며, 기존 범위·고정 instruction·Worker 결과 검증을 통과한 전달만 확인한다. session 시작 관측만으로 cursor를 전진시키지 않고 중단·실패·저장 실패에는 미확인 내용을 보존해 재전송한다. 호출 이후 새로 생긴 정보까지 확인 처리하지 않는다.
- 교체 세션에는 저장 상태의 미완료 요점과 G-065의 미해결 진단만 구성하고 이전 `worker_result`·검증 원문은 넣지 않는다. 정적 코드 조회 안내의 초기 전달과 현재 rebase 후보·ExecPlan 변경에 필요한 재확인 지시는 기존 경계에 연결한다. 축약을 후보 검증 성공 재사용으로 취급하지 않으며 Requirement 완료 판단·이동은 현재 Planner·Manager 경계를 유지한다. archive 후보 변경 전달을 이유로 Worker의 Requirement 이동을 다시 요구하지 않는다.

- `execution/manual_candidate.py`의 최종 Worker prompt도 같은 전달 경계에서 확인한다. 저장된 원문·지문·단계 결과는 보존하고 기존 focused feedback과 delta를 재사용한다. Reviewer의 최종 후보 근거 전달은 G-197에 남기며 생략 identity나 Review 성공 재사용을 새로 설계하지 않는다.

## Validation

- 실제 `worker_prompt`를 사용한 호출에서 R-064의 common+worker instruction과 사용자 메시지를 각각 검사한다. 최소 test case/class/module, 실패 뒤 동일 command 재실행, 문서·설정/관련 unit 부재의 구체적 사유와 전체 검증 금지는 고정 instruction에 유지하고 사용자 메시지의 중복을 제거한다. `tests/test_loop_manager.py`의 Worker prompt에 정적 규칙을 요구하는 assertion을 instruction 전달 검사로 옮기며, `tests/test_agent_invocation.py`의 임의 `delta only` fixture만으로 실제 prompt의 중복 부재를 확정하지 않는다. Reviewer prompt 교정은 별도 담당 계획에 남긴다.
- Worker는 `tests/test_execution_worker_phase.py`의 prompt·fallback, `tests/test_worker_session.py`의 전이 및 `tests/test_validation_feedback_state.py`의 전달 확인·재개 중 변경한 경계의 최소 targeted unit case만 실행한다. `tests/support/execution.py`의 기존 fixture·Agent double을 재사용한다. 기존 재개 prompt의 역할 반복과 복구 prompt의 이전 결과 포함을 요구하는 assertion은 이 계획의 초기 전달·delta·최소 복구 계약에 맞춰 갱신한다.
- 최초 turn, 같은 세션의 일반 수정·effort 변경·verify·Review 교정, archive·비검증 오류 변경 및 두 채널 중복 방지, resolved provider·Model 교체·legacy 복구·session-unavailable fallback을 검증한다. 루트 지침 관리 prompt 부재와 고정된 역할 instruction·Ponytail의 재사용, 사용자 메시지의 정적 규칙 중복 부재도 확인한다. 기존 `tests/test_agent_invocation.py`·`tests/test_provider_runtime.py`의 호출 fixture를 재사용하며 실제 Agent를 호출하지 않는다.
- session 시작 직후 timeout, 잘못된 Worker 결과, fallback 시작 실패, 전달 확인 전후 중단·재시작·저장 실패와 호출 뒤 새 revision 발생에서 미확인 내용이 누락되지 않고 확인된 동일 내용을 반복하지 않는지 검증한다. 비밀값 가림과 승인 문맥은 `tests/test_validation_feedback.py`·`tests/test_execution_guidance.py`, 결과 형식은 `tests/test_worker_result_contract.py`의 직접 관련 case를 변경 시 연결한다.
- 전체 verify는 Agent 범위·고정 지침/과거 승인 감사 보존, Worker 결과·일반 사용자 결정 protocol, 피드백 audit·cursor, 실제 호출·session 복구와 rebase 후보 재확인 회귀를 유지한다. 기존 unittest manifest 수집·Agent 호출 차단을 사용하며 전체 verify와 Reviewer의 책임은 `docs/AGENTS.common.md`·`docs/AGENTS.worker.md`·`docs/AGENTS.reviewer.md`를, 계획 작성 규칙은 `docs/PLANS.md`를 따른다.

- 기존 manual fixture로 수동 Worker prompt의 최소 unit case를 확인한다. Review 실패 뒤 저장된 verify PASS 출력, verify 실패 뒤 raw 출력 재추가, 동일 session 재개와 새 session fallback에서 중복·원문 복제가 없고 필요한 실패 진단·전체 G·후보 R은 남는지 검증한다. Manager 전체 verify는 생략 무효화·실제 Worker 교정·원문 및 실패 지문 보존과 수동/일반 Reviewer 입력의 회귀를 확인한다.
- `tests/test_planning_selection.py`·`tests/test_stage_documents.py`의 최신 문서 진입 fixture와 실제 Worker prompt 구성을 연결한다. 프로젝트 planning 후 ExecPlan 조건 유지/변경, source 지문만 변경, 순수 기록·미확인 조건이 섞인 변경, 동일 session·fallback·전달 중 중단에서 필요한 새 조건은 전달하고 확인된 동일 문맥은 반복하지 않는지 최소 unit으로 확인한다. planning 미완료를 조건 무변경으로 대신 확정하거나 전달 cursor를 전진시키지 않는다. 필요한 Worker 호출의 prompt만 검사하며 전체 planning을 각 세션 조합마다 다시 실행하지 않는다.

## Progress

- [x] 2026-10-07 기준 94e9e0b의 수동 준비 병행 변경, R-033·R-077, 최종 수동 prompt와 G-197을 정적으로 대조함. 활성 R-033의 기존 최소 전달 목표를 수동 교정 경계에 명시했으며 구현·테스트 실행은 하지 않음.

- [x] 2026-10-05 지정 READY snapshot의 목표·범위와 최신 prompt·session·feedback 관련 회귀 경계를 문서·원본으로 재검증함 (구현·테스트 실행 아님)
- [ ] 초기·재개·교체 Worker prompt 경계 분리
- [ ] 새 archive·오류 전달 cursor 연결
- [ ] 관련 targeted 회귀 사례 추가

## Decision Log

- 2026-10-14 delta 개정: 현재 R-082의 프로젝트 planning 우선 처리와 ExecPlan 실행 대응 분리에 맞춰 새 문맥의 기준·호출 조건을 정합화했다. 직접 반영된 planning·기록 기준 helper를 재구현하지 않고 최소 prompt·확인 cursor의 기존 목표와 수행 기록을 보존한다. source 지문은 verify·Review의 독립 입력으로 유지하며 구현·테스트·verify 성공을 기록하지 않는다.

- 2026-10-10 사용자 정리: R-003은 내용 보존 archive하고, 정적 검증 규칙의 instruction 단일 전달은 활성 R-064에 연결한다. Worker prompt의 초기·delta·복구 목표와 기존 검증·dependency는 유지하며 아래 R-003 참조는 당시 검토 기록이다.

- 2026-10-09 full 기준 eb797d3a와 mutable token 324c039278646b522a5cbad59f1ba7ad5c4d543c36002cf4bdc2efc2bd0e05ed, 원본 hash 6cd0e0bfd53789f4f1619b65000721dcf97f2fd0677386023f1d10c8ca1334b3를 확인했다. 직접 구현된 R-082의 단계별 문서 갱신을 기존 초기/delta/복구 목표에 연결하고 G-242의 보완 뒤 실제 Worker 전달을 검증한다. 기존 ID·경로·분류·목표·기록을 보존하며 문서 판단/게시 기능을 중복 구현하지 않는다. 구현·테스트 실행 없음.

- 2026-10-09 delta 기준 b29bb81c9c3c76cc196b30fd04eed1f52a02d6fa, mutable READY token d249095f1d5c09428e872445c00ba5d442b3732eb5ea3eef1ecbf3c2a5833dac와 원본 hash 82d08b12f5b8797a95e26c3de9c9a976eb86386e2ca90fc395b3cf0f13faa0dd를 대조했다. G-197의 검증 근거 전달은 Merge됐지만 실제 Worker prompt와 PublicFacadeTest는 정적 테스트 규칙의 중복을 유지한다. 이 계획의 기존 첫 Acceptance Criterion이 이미 담당하는 제거 목표에 활성 R-003 source와 실제 호출 검증만 연결한다. ID·Purpose·기존 dependency·complexity high·risk medium·priority low와 초기/delta/복구 범위를 보존하며 Reviewer 교정을 추가 구현하지 않는다. 구현·테스트 실행 및 Requirement 충족·자동 검증 성공을 기록하지 않았다.

- 2026-10-08 기준 a3ce7f88cef4cd46b34e0ead46ec5472fb5bd6f7, mutable READY token c309db89ee1035657daf102b0381c0ca72de2235143e2e7bbded9eede0b66e3a와 원본 hash 2a30673ba81e224490cd6803fa0f42f67f71cfeb766e7afe81c678418e4d8df6을 대조했다. 현재 R-066·R-064·PRODUCT와 tests/test_execution_guidance.py·test_planning_fingerprint.py는 CLI 루트 지침의 읽기·승인·patch 관리를 제거한다. 이 때문에 폐지된 파일 재승인 lifecycle의 G-173은 최소 prompt 구현의 선행 목표가 아니므로 dependency와 해당 전용 전달 요구만 제거한다. 일반 User Decision·실행 권한·고정 instruction·과거 감사는 보존한다. G-173 파일·실행 이력은 snapshot 밖이므로 변경하지 않았다. 원래 초기/delta/최소 복구 목표, ID·분류·priority와 나머지 dependency는 유지하며 구현·테스트·검증 성공을 만들지 않았다. 아래 과거 승인 전달 기록은 당시 결정의 감사이며 현행 구현 지시로 사용하지 않는다.

- 2026-10-07 mutable READY token 5b96f9d2d8d50ffcdf25f4829238a01aa03f6a52846d0435d8b315ce6ad09aa3, 기준 94e9e0b0825d3bd2cbee5e75aac06f9f2a335e0f 및 원본 hash 28864fe6b250133c147d7876020166ac5705f27d520e92a4cb1f6d75b6cbd522를 확인했다. R-032 복원 철회를 존중하며 archived 원문을 활성 source나 새 후속 계획의 근거로 사용하지 않는다. 남은 수동 Worker 원문 재전송은 활성 R-033의 기존 목표와 PRODUCT의 verify 성공 출력 미전달, R-077의 자동 교정 계약에 포함한다. Merge된 G-065의 formatter는 재구현하지 않는다. ID·sources·기존 dependency·목표·metadata를 보존한다.
- complexity high: 전달 revision 고정·실제 session 결합·정상 반환·fallback·중단 및 호출 중 새 정보 발생 사이에서 전달 확인을 안전하게 확정하는 판단이 남아 있다. risk medium: 잘못된 cursor나 축약은 교정을 누락·중복하지만 저장된 원문·피드백·작업물과 기존 scope·후보 gate에서 복구할 수 있다. 수동 준비의 병렬화나 Reviewer 근거 전달은 이 계획의 새 목표가 아니다.

- 2026-10-05 일반 mutable READY snapshot `202cd7f3a965a13572b02c54e2fefa108d58d6d661ef94298e7d4a2ed22ef059`에 따라 ID와 기존 목표를 보존했다. 새 Review 교정·문서 권한·User Decision·결과 정규화·verify 입력 확인의 전달 계약이 먼저 확정되도록 G-160·G-171·G-173·G-180·G-181을 dependency에 추가했다. 이 계획이 해당 기능을 중복 구현하지 않으며 session delta는 확정된 계약을 따른다.

- CLI가 보존하는 대화를 Loop Manager가 별도로 압축하거나 재구성하지 않는다. G-058의 공통 지침과 G-065의 verify 피드백을 전제로 한다.
- 2026-10-02 사용자 결정: 리팩터링을 먼저 진행하므로 G-127 Merge 이후 시작한다. SQLite에서 미착수·무소유 READY임을 확인했으며 원래 목표·Acceptance Criteria와 기존 dependency는 유지한다. 실행 시 G-119~G-125에서 정리된 prompt·session·handler와 테스트의 최신 경로를 확인한다.

- 2026-10-03 사용자 요청에 따른 대기 계획 전체 점검: R-033과 현재 PRODUCT/R-050에 목표 충돌이 없음을 확인했다. G-119~G-129 이후의 실제 prompt·Worker handler·session·feedback 및 테스트 경로로 갱신했다. 직접 Codex 실행과 신규·재개·fallback의 역할별 내장 sandbox 정책은 prompt 축약과 독립적으로 유지한다. SQLite와 실행 상태는 수정하지 않는다.

- 2026-10-05 post-refactor revalidation: source revision `c203b1437537b868862a104e668586d392c5ad0a`의 `agents/agent_prompts.py:165`, `execution/execution_worker_handler.py:239`·`:334`·`:545`, `execution/worker_session.py:65`, `storage/validation_feedback_state.py:211`를 `loop_manager_internal/` 아래 원본과 관련 테스트로 대조했다. 세션 전이·검증 피드백 cursor는 이미 분리돼 있으므로 재사용하고 나머지 전달 상태만 확장한다. ID·경로·title·sources·complexity·risk·priority·Purpose·Acceptance Criteria와 기존 dependency는 보존한다.
- G-147의 공통 지침 주입·버전 보존을 먼저 Merge하도록 dependency를 추가했다. 공통 규칙의 별도 주입과 사용자 turn의 축약을 연결하기 위한 순서이며 G-147의 구현·등록·배포 범위를 가져오지 않는다. 이전 초기 메시지 품질 지침 포함 방향은 해당 공통 버전 재사용으로 연결하고, R-033의 반복 방지·최소 복구·승인·권한·검증 목표는 유지한다. Requirement retirement, 공통 verify 규약과 후보 성공 재사용은 별도 책임으로 남기며 이번 재검증은 실행 상태나 성공 증거를 만들지 않는다.
- 2026-10-07 사용자 요청에 따른 지침 분리 정합화: 공통·역할별 파일을 고정해 합치는 호출 경계를 전제로 초기·복구 전달 위치를 명확히 했다. 이 계획은 작업별 사용자 메시지와 delta만 줄이며 지침 파일 병합·배포를 재구현하지 않는다. 기존 ID·분류·dependency와 반복 방지·복구 목표를 유지한다.
