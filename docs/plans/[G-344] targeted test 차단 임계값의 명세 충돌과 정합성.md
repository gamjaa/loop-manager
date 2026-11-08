---
id: G-344
title: targeted test 차단 임계값의 명세 충돌과 정합성
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on: []
complexity: low
risk: medium
priority: high
---

## Purpose

동일 targeted test의 실제 연속 실패 차단 기준은 5회로 유지한다. 검증된 사용자 Answer와 현재 PRODUCT·R-055·정정된 R-091, 저장·Worker/verify gate·회귀가 같은 기준이다. 이 선택에 따른 구현·저장 호환 변경은 남아 있지 않다. 질문 해소 결과를 Manager에 반환하며, 기존 질문·User Decision·draft와 후속 관계의 공식 정리 및 잔여 작업 철회 판단은 Manager가 수행한다.

## Acceptance Criteria

- 현재 PRODUCT·R-055·R-091과 저장 구현의 기준은 5회다. 세 번째 실패로 동작을 변경하거나 새 설정·저장 형식·기존 이력 변환을 추가하지 않는다.
- 현재 `TargetedTestRepository.observe()`의 `failures >= 5`와 Worker/verify gate를 유지한다. 다른 테스트 비합산·동일 실행 중복 비소비·성공/skip/expected failure 초기화·fixture/불명확 결과 보존·재시작과 작업별 격리는 기존 구현·회귀로 확인한다.
- 세 물리 CLI 호출·session fallback의 회귀는 고유 실행 3건·실패 누계 3·도달 표식 없음을 검사한다. 다섯 번째 실패의 별도 도달/차단 회귀를 유지하며 호출 횟수를 늘리거나 같은 테스트를 복제하지 않는다.
- 사용자 Answer의 5회 유지·R-091 정정·불필요한 구현 금지 선택을 본문에 반영한다. Manager의 출처·판단 revision·현재 입력·소유권 검증과 게시 확정 전에는 draft·기존 질문·User Decision·후속 dependency를 직접 제거하지 않는다. 구현 잔여 없음의 정적 판단은 테스트·verify 성공이나 차단 해제를 뜻하지 않는다.

## Implementation

- [x] 검토 revision의 PRODUCT·R-055·정정된 R-091과 `loop_manager_internal/storage/targeted_test_state.py:TargetedTestRepository.observe()`·`execution/execution_worker_handler.py:_targeted_test_block()`·`execution/execution_verify_handler.py:_targeted_test_gate()`를 정적으로 대조했다. 5회 기준에 따른 코드·저장 호환 변경은 없다.
- [x] `catalog/draft_decisions.py:user_changes()`·`prepare()`·`_clean_answers()`와 `storage/execplan_draft_state.py:observe()`의 사용자 출처·질문별 인용·게시 CAS를 정적으로 대조했다. archive 관측 `proof_revision`과 실제 Agent 게시 revision의 분리, 질문/Answer 유지 시 비교 기준·근거 보존은 현재 직접 구현으로 소비한다. 이전 readiness 오류와 출처 미확인 기록은 당시 관측으로 보존하며 새 교정 작업을 만들지 않는다.
- [x] Manager의 verified `change-3`에 있는 사용자 Answer를 현재 질문 및 원문과 대조했다. 5회 유지와 3회로 변경하지 않는 선택이 질문 전체를 해소한다. 본문에 선택을 통합하고 동일 판단 지문의 `DRAFT_DECISION_RESULT`로 반환한다.
- [ ] Manager가 현재 입력·소유권과 보존된 사용자 출처를 검증하고 질문별 감사·draft/임시 입력·후속 관계 및 구현 잔여 없는 작업의 정리를 확정한다. 이를 위한 Worker 구현·새 테스트는 계획하지 않는다.

## Validation

- 정적 근거는 `tests/test_targeted_test_streak.py:TargetedHistoryTests`의 다섯 고유 실행·중복 비소비·다른 테스트 격리·초기화·재시작과 `WorkerTargetedFailureTests`의 다섯 번째 실패 도달 뒤 다음 Worker/verify 미호출·같은 turn 후속 성공 회귀다.
- `tests/test_agent_invocation.py:test_targeted_records_follow_each_physical_retry_and_session_fallback`은 세 호출·서로 다른 run/invocation·누계 `(3, 0)`과 원래 실패 결과를 확인한다. 이 동작은 변경하지 않으며 문서의 정적 대조를 위해 targeted test·전체 verify를 실행하지 않는다.
- `tests/test_execplan_drafts.py`의 질문별 복수 근거·Answer-only·stale/reject/defer·미확인 답변 보존·부분 정리/재관측과 `test_historical_observation_is_not_an_agent_publication`·`test_pending_question_keeps_baseline_across_input_changes`를 현재 출처 처리의 정적 근거로 확인한다. 사용자 Answer의 추가 원문·현재 원문 일치와 본문 반영은 Manager가 게시 전에 검증하며, Planner는 테스트나 전체 verify를 실행하지 않는다.

## Progress

- [x] 검토 revision `e09d3db46f7f84781001dd173e4875536c27c240`에서 verified Answer와 현재 5회 명세·저장/실행 gate·기존 회귀를 정적으로 대조해 질문 해소와 구현 잔여 없음을 반환했다. 아래 출처 미확인·uncertain 기록은 이전 호출 당시의 근거이며 이번 판단으로 대체된다. draft·사용자 입력·실행 상태와 과거 검증 기록은 변경하지 않았다.

- [x] 재검토 revision `2e642e0e8148bbec6244c23c99b266682402f4e7`의 verified ARCHITECTURE·PRODUCT·R-055 추가 원문을 기존 질문과 대조했다. 새 내용은 완료/archive 우선과 PLANNING 중 이력 복구이며 임계값 선택·기존 Answer/R-091 정정의 사용자 출처를 증명하지 않는다. 질문·User Decision·draft는 보존하고 구현·테스트·verify·대기 해제는 수행하지 않았다.

- [x] revision `42b79e867779a261371e27b47b100f795408f6fe`에서 PRODUCT의 5회, R-055의 5회, R-091 본문의 3회와 저장 구현 `failures >= 5`·기존 회귀를 정적으로 대조했다. 사용자 판단을 요청했으며 구현·테스트·verify는 실행하지 않았다.
- [x] 검토 revision `d59136fc6d559ba21c69e2232a21bb4a4be9fd90`에서 현재 명세·코드/회귀의 5회 일치와 게시 준비의 근거 결합 오류를 정적으로 확인했다. 이전 Planner의 해소 결과는 Manager가 수락한 결정이 아니며 이번 호출도 구현·테스트·verify를 실행하지 않았다.
- [x] 검토 revision `7be6f620bff8f2df1cd36454c60b4dbceae0a72a`에서 근거 결합의 직접 구현과 현재 제공된 두 verified 사용자 변경을 대조했다. 새 변경은 기존 임계값 질문·Answer의 추가 원문이 아니며 보존된 답변의 사용자 출처는 이번 자료에서 확인하지 못했다. 구현·테스트·verify·대기 해제는 수행하지 않았다.
- [ ] Manager의 기존 사용자 출처 연결·현재 근거 재평가·draft/후속 관계 정리

## Decision Log

- snapshot token `34ff7f02723d618d3badc9f716484d6a0285a5fc677ca9bd2b33dc121470fd30`의 ID·경로·원문 hash와 기존 metadata를 확인했다. 이번에는 사용자 `bfa7954b2897a80a776db6619238ec24b5229f21`의 Answer 추가가 verified 근거로 제공돼 기존 질문을 해소한다. Answer 근거의 보존 revision은 다른 source 변경의 `5bac6c66`·`e09d3db4`와 다르다. 해당 source의 rebase 이력·draft 출처 처리 변경을 임계값 선택의 추가 근거로 인용하지 않는다. 정정된 R-091·PRODUCT·R-055·현재 코드와의 의미 대조로 5회 선택이 유효함을 확인했으며, 새 승인 표시나 사용자 입력을 작성하지 않았다. 아래 uncertain 기록은 당시 입력에 대한 감사로 유지한다. complexity low는 구현 변경 없이 근거를 대조하는 범위, risk medium은 잘못된 해소로 차단 기준을 바꿀 영향이며 원문·지문·게시 검증으로 제한한다.

- snapshot token `b0bcfcab7b44cddd72d6724d12dbb817c6171b471f831aea814d4d9fc618b0bf`와 원문 hash를 대조했다. 현재 사용자 변경 근거에 맞춰 설명만 갱신하고 ID·경로·metadata·기존 기록·Draft Decision/User Decision 원문을 유지한다. 이번 결과는 같은 질문의 `outcome: uncertain`이며 사용자의 답변이나 새로운 승인을 작성하지 않는다.

- 현재 R-091·코드·회귀와 기존 Answer는 5회로 일치한다. 이 의미와 원문은 보존하지만 이번 판단에는 Answer/R-091 정정의 verified 추가 원문·사용자 출처가 없다. 제공된 ARCHITECTURE·PRODUCT·R-055 변경이 verified라는 사실만으로 무관한 임계값 질문을 해소하지 않는다. `Decision: approve`를 추가하거나 사용자 원문을 고치지 않고 `DRAFT_DECISION_RESULT`에 기존 질문 전체를 uncertain으로 반환한다. 새로운 정책 선택이나 같은 양식 재작성을 요구하는 판단이 아니다.
- G-357의 전역 readiness 거부 교정·복수 인용·소비 범위 재생·게시 CAS는 현재 원격 구현으로 소비한다. 과거 기술적 거부를 계속 미구현으로 기록하거나 같은 교정 계획을 만들지 않는다. 근거가 제공되면 같은 질문과 기존 입력으로 재평가하며 draft 정리·실제 영향 후보의 검증/재개는 Manager가 담당한다. snapshot token `2a4613355d51b07821f0b5bf7cf48c4dd08127e667baaef0d4a36e5fdbe36aa8`의 원문 hash와 ID·경로·metadata·무dependency·질문/User Decision·기존 기록을 보존했다.
- 보고 369의 Git 관측 fixture·세 물리 실패/비도달 assertion은 현재 코드에 반영돼 있다. 이 질문이나 기술적 근거 결합 오류로 그 교정을 반복하지 않는다. complexity low·risk medium·priority high와 ID·경로·기존 정적 조사 기록을 보존한다.
