---
id: G-349
title: 저장 Worker 재사용 회귀의 입력 정합화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: low
risk: medium
priority: high
---

## Purpose

Git 관측 실패 때 저장된 Worker 근거와 session을 보존하는 기존 회귀가 실제 재검사 경로에 도달하도록 테스트 준비 입력을 현재 실행 계약에 맞춘다. 보고 432의 session identity 오류를 운영 코드의 검증 완화로 우회하지 않고, 유효한 저장 성공과 근거가 부족한 과거 결과를 구분하는 R-072의 gate를 그대로 검증한다.

현재 `tests/test_execution_retry_policy.py:test_git_observation_failure_pauses_without_discarding_saved_worker`는 기본 IMPLEMENTING fixture에 결과·session 문자열만 저장한다. `run_execution_policy()`는 그 단계의 실제 coordinator를 실행하므로 Worker를 호출할 수 있고, 공통 Agent double은 요청 session과 무관하게 `session`을 반환한다. 저장된 `worker-session`과 충돌해 의도한 fetch 실패·대기·보존 assertion에 도달하지 못한다. catalog의 G-344는 targeted 실패 임계값의 사용자 판단, G-278/G-338은 운영 planning·복구의 진행 조건을 소유하며 이 테스트 입력 교정과는 다르다.

## Acceptance Criteria

- R-080/R-072: 해당 회귀가 현재 입력에 결합된 저장 Worker 성공과 적절한 다음 실행 단계를 준비하고 실제 revision gate·최신 base 관측·복구 판단·SQLite 저장을 통과한다. 결과 문자열의 존재만으로 재사용을 허용하거나 gate 자체를 성공 mock으로 바꾸지 않는다.
- 주입한 정확한 base fetch 실패를 기존 제한 재시도까지 관측한 뒤 TRANSIENT_PAUSE로 반환한다. 원래 결과·`worker-session`과 작업 identity는 그대로 남고 Agent는 호출되지 않는다. 기존 fetch 3회·recovery attempts 3·예상 밖 Git 명령 거부 assertion을 유지한다. 이 횟수는 targeted test의 3회·5회 차단 기준을 선택하는 근거가 아니다.
- 현재 입력에 결합되지 않은 결과를 자동 성공으로 취급하지 않는 기존 회귀를 유지한다. session 불일치 거부·검증 근거 수집·실패/중단 감사와 다른 정책 테스트의 CLI retry·session fallback 조건을 약화하지 않는다.
- 실제 Git·CLI·모델을 추가 실행하거나 전체 lifecycle을 반복하지 않고 기존 mock Git·실제 handler·SQLite fixture로 검증한다. 테스트 삭제·skip·예외 무시·assertion 완화로 실패를 없애지 않는다.

## Implementation

- [ ] 확인한 `tests/test_execution_retry_policy.py`의 해당 case와 `tests/support/execution.py:execution_policy_fixture()`·`ExecutionFixture.context()`·`review_context()`를 재사용해 저장 성공의 정의·입력 token·archive 근거와 실행 단계의 준비를 정합화한다. `review_context()`의 DefinitionSnapshot 결합 패턴을 참고하되 이 회귀에 필요 없는 verify 성공 receipt나 Review 결과를 만들지 않는다. 한 case에 필요한 준비는 해당 case에 두고 공유 fixture 변경은 같은 계약을 쓰는 소비자가 실제로 필요할 때만 한다.
- [ ] 확인한 `execution/execution_runtime.py:ExecutionPhaseCoordinator.dispatch()`·`execution/execution_revision.py`의 정의/Worker 입력 gate와 `execution/execution_latest_base_handler.py`의 fetch 경계를 따라 준비한 단계가 원하는 실패에 도달하는지 대조한다. 이 운영 코드들은 읽기 전용 비교 근거이며 이 작업에서 동작을 바꾸지 않는다. `storage/validation_feedback_state.py:bind_session()`의 불일치 거부도 유지한다.
- [ ] 기존 테스트의 결과·session·Agent no-call·fetch 횟수·복구 저장 assertion을 유지해 테스트 입력 교정을 확인한다. 확인한 `tests/test_execution_revision_gate.py:test_exact_legacy_worker_token_retains_implementation_after_source_only_change`의 정의/token 준비와 `tests/test_execution_revision.py:test_missing_worker_result_cannot_skip_implementation`의 근거 부재 거부를 재사용하며 동일 조건의 새 테스트를 복제하지 않는다. 공통 Agent double의 고정 session을 바꾸는 것만으로 no-call 조건을 충족했다고 보지 않는다.

## Validation

- Worker는 `tests.test_execution_retry_policy.ExecutionRetryPolicyTest.test_git_observation_failure_pauses_without_discarding_saved_worker`를 targeted unit으로 실행한다. session 오류 없이 주입한 Git 실패가 관측되고 TRANSIENT_PAUSE·원래 결과/session·Agent no-call·정확한 fetch/attempts assertion이 모두 성립해야 한다. 새로운 실제 프로세스 호출은 없어야 한다.
- 준비 코드가 실제로 공유 fixture를 바꾼 경우에만 직접 영향받는 최소 소비자와 위 revision gate의 저장 성공/입력 부재 case를 실행한다. 확인한 `tests/test_validation_feedback_state.py:ValidationFeedbackStateTests.test_delivery_cannot_be_acknowledged_by_another_session`은 bind/delivered의 다른 session 거부를 계속 검증한다. 이 session 저장 코드를 바꾸거나 같은 검증을 복제하지 않으며 변경된 경계 밖의 대규모 통합 반복은 추가하지 않는다.
- 전체 검증에는 수정한 기존 회귀가 계속 수집돼야 한다. 이 한 회귀의 성공을 보고 406·409의 별도 timeout이나 다른 공통 결함, 영향 작업의 현재 후보 검증 성공으로 확대하지 않는다.

## Progress

- [x] revision `f253d5aae8f393896268430517ee0caf2dcf411d`에서 보고 432의 원래 revision과 현재 테스트·fixture·coordinator·revision/session gate를 정적으로 대조했다. 현재 catalog에 같은 교정 목표의 비종료 계획이 없음을 확인했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 저장 Worker 근거·단계 준비 정합화와 기존 assertion 유지
- [ ] 변경 관련 최소 targeted unit 및 영향받은 공유 소비자 확인

## Decision Log

- 원인은 저장 결과를 준비하는 회귀의 입력 계약과 실제 dispatcher의 불일치다. session binding 오류는 그 뒤 고정 session double에서 드러나는 결과다. R-072가 요구하는 유효한 성공 근거를 준비하며 운영 단계 순서·session 검증을 바꾸지 않는다. 보고 432 이후 직접 반영된 정확한 fetch 인수의 실패 주입·3회 assertion과 G-339의 Git diff/merge-base fixture·물리 targeted 실행 귀속 검증은 재사용한다. 이 직접 보완에도 남은 단계/근거 준비만 맡으며 G-344의 질문·draft·임계값은 그대로 둔다.
- 독립 테스트 입력 교정이므로 선행 dependency를 추가하지 않는다. 보고 원인의 교정과 영향 작업의 실제 전체 verify·Review·Merge 및 차단 재평가는 별개다. 다른 미확정 보고를 같은 원인으로 연결하지 않으며 이미 Merge된 계획을 재작성하지 않는다.
- complexity low·최초 level 1: 기존 정의/token·단계 준비 패턴을 한 회귀에 적용하는 국소 변경이다. risk medium: 준비를 잘못하면 필수 입력 gate나 Agent no-call 검증이 사라져 근거 보존 회귀를 놓칠 수 있으므로 실제 handler·SQLite와 기존 assertion을 유지한다. 외부 게시·운영 데이터 변경이 없고 테스트 diff로 되돌릴 수 있다. priority high는 여러 작업의 전체 verify에서 해당 회귀가 의도한 조건 전에 실패하는 결함을 교정하기 위한 것이다. Model·Effort는 Manager의 명시 설정을 따른다.
