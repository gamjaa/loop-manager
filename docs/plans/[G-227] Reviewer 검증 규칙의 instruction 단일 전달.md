---
id: G-227
title: Reviewer 검증 규칙의 instruction 단일 전달
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on:
  - G-242
  - G-265
  - G-266
  - G-267
  - G-268
complexity: low
risk: low
priority: normal
---

## Purpose

Reviewer의 정적 검증 책임은 매 호출 현재 선택 runtime의 common+reviewer instruction으로 전달하고 사용자 prompt에는 후보·diff·결과·교정 문맥만 남긴다. 현재 `reviewer_prompt`는 테스트·verify 재실행 금지를 다시 쓰며 `tests/test_loop_manager.py`와 `tests/test_worker_validation.py`도 그 중복을 요구한다. 직접 구현된 호출별 지침 선택과 최종 후보 검증 결과 전달·진입 gate는 재사용한다.

## Acceptance Criteria

- 실제 Reviewer 호출의 현재 runtime instruction에는 읽기 전용 검토, 테스트 command·전체 verify·lint·build·type check 재실행 금지와 전달 결과로 테스트 누락·회귀·검증 약화를 판단하는 책임이 유지된다. 사용자 prompt에는 이 정적 규칙을 중복하지 않는다.
- 최초·같은 세션 재개·프로토콜 교정·새 세션 복구에 같은 경계가 적용된다. 현재 후보의 Worker 보고 또는 구체적 미실행 사유, Manager 실제 verify PASS/증명된 reuse/수동 생략의 구분, 후보 HEAD·base와 필요한 ISSUE_ID 결과 형식은 유지된다.
- 최종 승인용 자동 Reviewer는 유효한 Worker 결과와 현재 후보에 연결된 Manager 전체 verify 성공 뒤에 시작한다. R-082의 관련 문서 변경에 따른 선행 Review에는 최신 문서·현재 구현·과거 verify의 확인 범위와 새 조건 미검증을 구분해 전달한다. 선행 PASS/VERIFY_REQUIRED는 최종 승인·게시 token이 아니며 구현 부족은 Worker, 검증 실행만 부족하면 Manager verify로 연결한다. 수동 제출·waiver를 자동 성공으로 합성하지 않고 원격 checkpoint 재개도 같은 구분을 보존한다. R-086 일반 선행 Reviewer에는 Manager 부분 PASS·선택/미선택 범위와 전체 verify 미실행을 전달한다. 동일 후보·실제 호출 identity·이후 전체 run을 확인한 최종 승격은 추가 Reviewer를 요구하지 않으며 R-082 문서 선행 결과와 혼합하지 않는다.
- 실제 prompt를 사용하는 호출을 자동 테스트로 검사해 현재 runtime instruction의 허용·금지 경계와 사용자 메시지의 중복 부재를 함께 증명한다. 임의의 `delta only` 문자열 또는 prompt 함수 단독 검사만으로 실제 전달을 대신하지 않는다. 같은 작업·세션의 재개에도 현재 지침을 전달하고 과거 호출 해시·session 귀속은 보존한다.

## Implementation

- [ ] `agents/agent_prompts.py`와 실제 Review/checkpoint 호출의 기존 prompt 조합을 확인해 instruction에 이미 있는 검증 규칙의 반복을 제거한다. 결과 형식과 후보별 사실은 사용자 메시지에 유지하고, 기존 common_guidance/provider의 현재 원본 선택·실행용 파일 검증·read_only 경계를 재사용한다. 작업별 snapshot 선택·복원은 추가하지 않는다.
- [ ] `docs/AGENTS.common.md`·`docs/AGENTS.reviewer.md`의 현재 책임을 보존한다. 결과 전달을 위해 원본을 재작성하지 않으며 후보 worktree 편집본을 실행 입력으로 사용하지 않는다. 대응 테스트에서 사용자 메시지에 금지 문구를 요구하던 기대값을 실제 instruction과 결과 문맥 검사로 분리한다.
- [ ] Worker의 초기/delta/복구 prompt는 현재 `worker_prompt`·`execution_worker_handler.py`와 `storage/validation_feedback_state.py`의 직접 구현을 유지한다. 이 계획은 Worker session cursor, Review 성공 재사용, receipt 정책과 알림을 추가 구현하지 않는다. PRODUCT·ARCHITECTURE의 현재 단일 전달 계약은 유지하고 필요한 관련 설명만 정합하게 갱신한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 Reviewer prompt case와 `tests/test_agent_invocation.py`의 역할별 전달 fixture를 재사용한 관련 최소 unit case만 실행한다. 실제 Reviewer prompt·현재 runtime 원본을 사용하는 provider double로 Codex·Claude의 신규/재개 요청에서 instruction과 사용자 문맥을 각각 확인한다. 같은 작업의 역할 원본 변경 뒤에도 정적 규칙은 현재 instruction에만 있고 후보·검증 사실은 사용자 prompt에 남아야 한다. 기존 지침 변경/감사 보존 회귀는 재사용하고 실제 Agent·Git 관측은 mock으로 대체한다.
- `tests/test_execution_verify_phase.py`·`tests/test_worker_validation.py`·`tests/test_review_specification_recovery.py`의 관련 assertion은 기본 경로의 Worker 반환→Manager 전체 verify→Reviewer 순서와 R-086의 부분 verify→일반 선행 Reviewer→실제 전체 verify→최종 승격 순서, 빈 stdout 성공, 후보/receipt 누락 거부 및 checkpoint 근거 전달을 보존한다. `tests/test_stage_documents.py`의 선행 Review에서는 과거 PASS 참고 표시, PASS/FAIL/VERIFY_REQUIRED의 분기와 최신 근거 없는 게시 금지를 실제 prompt 전달로 확인한다. 수동 결과 구분은 `tests/test_manual_work.py`의 기존 fixture를 재사용한다. `tests/test_preliminary_review.py`의 일반 선행 FAIL→전체 verify no-call·승인 재사용·증거 부재 fallback을 보존한다.
- Manager 전체 verify는 실패 시 동일 Worker session 복귀, targeted 내부 재실행의 SQLite retry 비소비, 읽기 전용 Review와 최종 후보·게시 gate를 통과해야 한다. 검증 규칙의 전달 위치 변경으로 검증 실행·범위를 축소하지 않는다.

## Progress

- [x] 2026-10-09 제공된 delta revision에서 R-003·R-064, 실제 prompt·instruction 합성·Review gate와 관련 테스트를 정적으로 대조했다. 구현·테스트는 실행하지 않았다.
- [ ] Reviewer prompt 정합화와 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-064의 현재 runtime 적용과 R-033의 같은 session delta 계약에 맞춰 호출별 지침 선택을 직접 구현으로 소비한다. 남은 작업은 실제 Reviewer 사용자 prompt의 정적 규칙 중복 제거이며 이전 로그의 고정 버전 표현은 현행 선택 조건이 아니다. ID·경로·기존 dependency와 복구 판단·수행 기록은 보존하고 새 지침 엔진이나 Worker prompt 작업을 만들지 않는다. 이번 delta에서 구현·테스트는 실행하지 않았다.

- 지정 delta revision `16115a547319c40aab74e67094536c43bdae7fce`와 snapshot token `41c4344480fa2e611f329b73b10ee282021edebf66ec448d2eb1623329109377`에서 삭제 원문과 현재 Worker 전달·session·수동 문맥을 대조했다. 기존 ID·경로·metadata·dependency·수행 기록을 보존하며 Reviewer의 정적 규칙 중복 제거만 미충족으로 유지한다. 아래 판단은 현재 취소 근거에 대한 개정이며 과거 복구 문구나 삭제 자체를 성공 근거로 사용하지 않는다.
DEPENDENCY_RECOVERY: {"deleted_id":"G-066","outcome":"obsolete","replacement_ids":[],"sources":["docs/PRODUCT.md","docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md","docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md"],"reason":"보존된 최소 Worker prompt 목표는 현재 worker_prompt의 초기/재개/복구 분리, WorkerHandler의 실제 session별 context delta, validation_feedback_state의 고정 attempt/전달 확인, manual_candidate.worker_context의 원문 없는 단계 요약에 직접 구현돼 있다. tests/test_execution_worker_phase.py의 같은 session 변경분/잘못된 반환 미확인 회귀, tests/test_validation_feedback_state.py의 호출 중 변경/저장 실패 회귀와 tests/test_agent_invocation.py의 실제 Worker prompt·고정 instruction 검사를 정적으로 대조했다. Worker 기능의 대체 선행 구현은 필요하지 않으며 이 계획의 독립적인 Reviewer 전달 교정과 기존 dependency는 유지한다. 구현·테스트 실행·자동 검증·Merge 성공을 새로 기록하지 않는다."}

- 2026-10-12 사용자 요청의 문서 정합화: 기본·문서 선행·일반 선행 경로의 전달 사실과 승인 gate를 구분했다. R-086 상태/receipt를 다시 구현하지 않으며 instruction 단일 전달 목표·metadata·dependency·기존 수행 기록을 보존한다.

- 2026-10-09 full 기준 eb797d3a와 mutable token 324c039278646b522a5cbad59f1ba7ad5c4d543c36002cf4bdc2efc2bd0e05ed, 원본 hash b213723241bb50c4b4dea40490620c5a35925fea614a4c5a7762c1257734be02를 확인했다. R-082의 직접 구현으로 생긴 선행 Review 예외를 기존 전달 목표에 반영한다. 고정 instruction 단일 전달·최종 후보 gate·low complexity/risk·기존 기록을 유지하며 Review 단계 판단을 재구현하지 않는다. 구현·테스트 실행 없음.

- complexity low: 기존 instruction과 호출 경계를 재사용하는 국소 prompt·assertion 교정이며 새 상태 전이 판단이 없다. risk low: 잘못된 문맥 제거는 검토 입력 누락을 만들 수 있으나 기존 결과/후보 gate와 읽기 전용 권한을 보존하고 해당 prompt를 교정해 복구할 수 있다. 필수 명세 정합화로 priority normal이다.
- G-197의 검증 근거 전달을 중복 계획하지 않는다. Worker prompt와 Reviewer prompt의 책임을 분리해 Reviewer의 확인된 중복과 실제 전달 회귀를 독립 범위로 둔다. Merge된 계획·archive·사용자 결정은 변경하지 않는다. 이 delta 검토는 프로젝트 최종 전문 확인이 아니다.

