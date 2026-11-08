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
draft: true
---

## Purpose

최신 PRODUCT·R-055·R-091의 동일 targeted test 연속 5회 실패 기준을 유지한다. 저장·Worker/verify gate·회귀에는 이 기준에 따른 구현 변경이 남아 있지 않다. 기존 5회 답변과 R-091 정정은 보존한다. G-357의 질문별 근거 결합은 현재 코드에 반영됐지만 이번 Manager 입력의 verified ARCHITECTURE·PRODUCT·R-055 변경에는 임계값 선택을 해소할 추가 원문이 없고, 기존 Answer·R-091 정정의 사용자 변경 근거도 제공되지 않았다. 따라서 질문·User Decision·draft 대기는 유지하며 이 작업의 Worker·추가 테스트는 시작하지 않는다.

## Acceptance Criteria

- 현재 PRODUCT·R-055·R-091과 저장 구현의 기준은 5회다. 세 번째 실패로 동작을 변경하거나 새 설정·저장 형식·기존 이력 변환을 추가하지 않는다.
- 현재 `TargetedTestRepository.observe()`의 `failures >= 5`와 Worker/verify gate를 유지한다. 다른 테스트 비합산·동일 실행 중복 비소비·성공/skip/expected failure 초기화·fixture/불명확 결과 보존·재시작과 작업별 격리는 기존 구현·회귀로 확인한다.
- 세 물리 CLI 호출·session fallback의 회귀는 고유 실행 3건·실패 누계 3·도달 표식 없음을 검사한다. 다섯 번째 실패의 별도 도달/차단 회귀를 유지하며 호출 횟수를 늘리거나 같은 테스트를 복제하지 않는다.
- Manager가 현재 질문과 검증된 사용자 변경·원격 입력·소유권을 연결하기 전에는 draft·기존 질문·User Decision·판단 대기와 후속 dependency를 유지한다. 문서 정합성과 구현 잔여 없음은 승인·검증 성공·차단 해제의 근거로 대용하지 않는다. 사용자에게 같은 양식 재작성을 요구하지 않고 기존 Answer·R-091 정정의 보존된 출처를 현재 근거 결합 경계에서 재평가한다.

## Implementation

- [x] 검토 revision의 PRODUCT·R-055·정정된 R-091과 `loop_manager_internal/storage/targeted_test_state.py:TargetedTestRepository.observe()`·`execution/execution_worker_handler.py:_targeted_test_block()`·`execution/execution_verify_handler.py:_targeted_test_gate()`를 정적으로 대조했다. 5회 기준에 따른 코드·저장 호환 변경은 없다.
- [x] 이전 게시 준비의 오류 “User Decision의 현재 사용자 판단 근거를 확인할 수 없습니다.”는 당시 `catalog/draft_decisions.py:prepare()`·`storage/execplan_draft_state.py:observe()`의 전역 readiness 거부로 확인했다. 이 과거 진단은 보존하며 현재의 거부 원인으로 재사용하지 않는다.
- [x] 현재 같은 파일의 `decision_evidence()`·질문별 `evidence` 인용·부분 답변 정리/재관측·게시 CAS와 `tests/test_execplan_drafts.py`의 대응 회귀를 정적으로 확인했다. 기술적 근거 결합 교정은 중복 계획하지 않는다. 이번 verified 변경의 추가 원문은 PR Merge/archive 우선 완료 판단과 PLANNING 중 archive 이력 복구이며 임계값 답변의 출처를 대신하지 못한다.
- [ ] Manager가 기존 Answer와 R-091 정정의 보존된 사용자 출처·before/after·현재 질문/입력·소유권을 확인해 재평가 자료로 연결한다. 확인된 질문 해소 이후에만 본문 통합·draft 정리·관계 반영·잔여 작업 철회를 판단한다. Planner는 User Decision·draft·실행 상태를 직접 변경하지 않는다.

## Validation

- 정적 근거는 `tests/test_targeted_test_streak.py:TargetedHistoryTests`의 다섯 고유 실행·중복 비소비·다른 테스트 격리·초기화·재시작과 `WorkerTargetedFailureTests`의 다섯 번째 실패 도달 뒤 다음 Worker/verify 미호출·같은 turn 후속 성공 회귀다.
- `tests/test_agent_invocation.py:test_targeted_records_follow_each_physical_retry_and_session_fallback`은 세 호출·서로 다른 run/invocation·누계 `(3, 0)`과 원래 실패 결과를 확인한다. 이 동작은 변경하지 않으며 문서의 정적 대조를 위해 targeted test·전체 verify를 실행하지 않는다.
- 현재 `tests/test_execplan_drafts.py`의 질문별 복수 근거·Answer-only·stale/reject/defer·미확인 답변 보존·부분 정리/재관측 회귀를 재사용한다. 소유 코드가 반영됐다는 사실과 이번 판단에 필요한 사용자 출처가 충분하다는 판단을 구별한다. 이번에는 기존 질문 전체를 `outcome: uncertain`으로 반환하며 사용자 의미 선택의 재질문과 구현된 기준의 재작업을 만들지 않는다.

## Progress

- [x] 재검토 revision `2e642e0e8148bbec6244c23c99b266682402f4e7`의 verified ARCHITECTURE·PRODUCT·R-055 추가 원문을 기존 질문과 대조했다. 새 내용은 완료/archive 우선과 PLANNING 중 이력 복구이며 임계값 선택·기존 Answer/R-091 정정의 사용자 출처를 증명하지 않는다. 질문·User Decision·draft는 보존하고 구현·테스트·verify·대기 해제는 수행하지 않았다.

- [x] revision `42b79e867779a261371e27b47b100f795408f6fe`에서 PRODUCT의 5회, R-055의 5회, R-091 본문의 3회와 저장 구현 `failures >= 5`·기존 회귀를 정적으로 대조했다. 사용자 판단을 요청했으며 구현·테스트·verify는 실행하지 않았다.
- [x] 검토 revision `d59136fc6d559ba21c69e2232a21bb4a4be9fd90`에서 현재 명세·코드/회귀의 5회 일치와 게시 준비의 근거 결합 오류를 정적으로 확인했다. 이전 Planner의 해소 결과는 Manager가 수락한 결정이 아니며 이번 호출도 구현·테스트·verify를 실행하지 않았다.
- [x] 검토 revision `7be6f620bff8f2df1cd36454c60b4dbceae0a72a`에서 근거 결합의 직접 구현과 현재 제공된 두 verified 사용자 변경을 대조했다. 새 변경은 기존 임계값 질문·Answer의 추가 원문이 아니며 보존된 답변의 사용자 출처는 이번 자료에서 확인하지 못했다. 구현·테스트·verify·대기 해제는 수행하지 않았다.
- [ ] Manager의 기존 사용자 출처 연결·현재 근거 재평가·draft/후속 관계 정리

## Decision Log

- snapshot token `b0bcfcab7b44cddd72d6724d12dbb817c6171b471f831aea814d4d9fc618b0bf`와 원문 hash를 대조했다. 현재 사용자 변경 근거에 맞춰 설명만 갱신하고 ID·경로·metadata·기존 기록·Draft Decision/User Decision 원문을 유지한다. 이번 결과는 같은 질문의 `outcome: uncertain`이며 사용자의 답변이나 새로운 승인을 작성하지 않는다.

- 현재 R-091·코드·회귀와 기존 Answer는 5회로 일치한다. 이 의미와 원문은 보존하지만 이번 판단에는 Answer/R-091 정정의 verified 추가 원문·사용자 출처가 없다. 제공된 ARCHITECTURE·PRODUCT·R-055 변경이 verified라는 사실만으로 무관한 임계값 질문을 해소하지 않는다. `Decision: approve`를 추가하거나 사용자 원문을 고치지 않고 `DRAFT_DECISION_RESULT`에 기존 질문 전체를 uncertain으로 반환한다. 새로운 정책 선택이나 같은 양식 재작성을 요구하는 판단이 아니다.
- G-357의 전역 readiness 거부 교정·복수 인용·소비 범위 재생·게시 CAS는 현재 원격 구현으로 소비한다. 과거 기술적 거부를 계속 미구현으로 기록하거나 같은 교정 계획을 만들지 않는다. 근거가 제공되면 같은 질문과 기존 입력으로 재평가하며 draft 정리·실제 영향 후보의 검증/재개는 Manager가 담당한다. snapshot token `2a4613355d51b07821f0b5bf7cf48c4dd08127e667baaef0d4a36e5fdbe36aa8`의 원문 hash와 ID·경로·metadata·무dependency·질문/User Decision·기존 기록을 보존했다.
- 보고 369의 Git 관측 fixture·세 물리 실패/비도달 assertion은 현재 코드에 반영돼 있다. 이 질문이나 기술적 근거 결합 오류로 그 교정을 반복하지 않는다. complexity low·risk medium·priority high와 ID·경로·기존 정적 조사 기록을 보존한다.

## Draft Decision

Question: 동일 targeted test의 실제 연속 실패 차단 임계값을 R-055·PRODUCT·현재 구현의 5회로 유지할 것인가, R-091 본문에 적힌 3회로 변경할 것인가?
Facts: R-055는 “같은 테스트의 실제 연속 5회 실패로 해당 작업을 차단한다”라고 정하고 PRODUCT도 5회를 보장한다. R-091은 “실제 동일 테스트 연속 3회 실패 등 기존 최종 차단 조건은 유지한다”라고 명시한다. 현재 `targeted_test_state.py`와 `TargetedHistoryTests`는 5회를 사용하며, 기존 catalog에는 이 충돌을 다루는 비종료 계획이 없다. 현재 구현·문서 수정 시점은 사용자 선택의 근거가 아니다.
Options: A. 5회를 유지하고 사용자 명세 R-091의 숫자를 정합화한다. 현재 구현이 모두 충족하면 이 draft에는 실행할 구현이 남지 않는다. B. 3회로 변경하도록 PRODUCT·R-055를 정합화하고 실제 남는 저장/차단·회귀를 이 계획에서 구현한다.
Impact: A는 기존 자동 차단 동작을 유지하며 문서 정합화가 필요하다. B는 세 번째 실패부터 차단되도록 사용자 동작과 기존 저장 이력의 해석이 바뀌므로 해당 명세 결정·호환 처리·회귀가 필요하다. 답변 전에는 임계값을 바꾸거나 어느 숫자를 최종 충족 기준으로 확정하지 않는다. 독립적인 rebase·pause 작업은 기존 gate와 기록을 보존하며 진행할 수 있다.

## User Decision

Answer: 동일 targeted test의 실제 연속 실패 차단 기준은 5회로 유지한다. R-091의 3회 표기를 5회로 정정하며, 3회로 동작을 변경하지 않는다. 현재 구현이 충족하면 불필요한 구현을 추가하지 않고 Manager가 잔여 작업과 draft 정리를 판단한다.
