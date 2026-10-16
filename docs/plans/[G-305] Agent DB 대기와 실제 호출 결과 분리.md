---
id: G-305
title: Agent DB 대기와 실제 호출 결과 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-073] 호출 목적과 사용량 범위 기록 및 조회.md
depends_on:
  - G-303
complexity: high
risk: high
priority: high
---

## Purpose

Agent의 지침·session·격리/실행 identity·호출 기록 저장에서 DB 대기를 실제 provider 실패와 구분한다. 이미 확보한 물리 호출 결과를 보존하고 DB 재시도 때문에 같은 Agent를 다시 실행하지 않는다. 필수 안전 증거가 없는 호출은 시작/승인하지 않으며 선택적인 사용량 수집 실패는 원래 결과를 유지한다.

현재 `agents/invocation_service.py`는 최초 guidance 준비의 `DatabaseWaitTimeout`은 다시 전달한다. 반면 invocation 생성 오류는 일반 수집 오류로 흡수하고, 격리/실행 identity·targeted 결합의 DB 오류는 `IsolationError`로 감싼다. 정상 provider 반환 뒤 `guidance_repository.bind_session()`의 DB 대기는 전체 예외 경로로 이동해 이미 받은 실제 결과를 호출 실패로 취급할 수 있다. 이 공통 호출·저장 경계의 남은 처리를 보완한다.

## Acceptance Criteria

- R-091: 호출 전 필수 DB 준비는 공통 예산에서 최신 상태를 재확인해 제한 재시도한다. 예산 초과/취소는 해당 요청·작업의 보존된 대기로 처리하고 Worker 교정·일반 단계 실패·provider 설정/권한 실패 횟수에 합산하지 않는다. 미시작 대기는 실제 호출 수·실행 timeout을 만들지 않는다.
- 필수 invocation·지침/session 귀속·격리/실행 identity·targeted 호출 결합을 저장하지 못하면 CLI를 시작하거나 검증/게시 승인을 내리지 않는다. 일시 DB 경합과 실제 정책/경로/권한·자료 오류를 구별하며 재시도를 이유로 필수 증거 저장을 선택 사항으로 바꾸지 않는다.
- 실제 시작 뒤 callback·session 확정·호출 종료 저장이 대기하면 이미 관측한 시작/종료·provider 결과/예외·session·사용량과 기존 launch/process receipt를 보존한다. DB 확정만 재시도·재개하고 완료한 provider 실행을 반복하지 않는다. 종료 불명은 계속 불명이며 호출 자리도 기존 실제 종료 근거 없이 해제하지 않는다.
- R-073: 선택적인 ledger/usage 수집 오류는 원래 Agent 결과·예외를 바꾸지 않고 부분 수집/오류로 남는다. 관측한 결과를 DB 오류용 빈 성공/실패로 덮거나 새로운 invocation·가짜 사용량을 만들지 않는다. 자료가 부족하면 해당 후속 처리를 보류하고 실행 성공이나 안전 증거를 추정하지 않는다.
- 정상·실패·중단·재시작에서 같은 물리 호출의 저장은 중복되지 않는다. 원래 오류와 저장 오류를 모두 진단할 수 있고, 독립 작업은 계속한다. 비밀값·prompt·코드/문서 전문을 운영 요약이나 새 감사 자료에 추가하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/agents/invocation_service.py:run_agent()`의 invocation 준비, `observe_session/isolation/execution_identity/targeted/launch`와 정상/예외 종료 저장을 필수 준비·실제 실행·저장 확정의 기존 경계에 연결한다. G-303의 DB 요청 재시도를 소비하고 임의 provider callback 전체를 replay하지 않는다. 공통 대기 오류를 영구 설정/격리 실패로 재분류하지 않는다.
- [ ] 확인한 `storage/common_guidance_state.py`·`agent_invocation_state.py`·`task_calls.py`의 task/session·실제 호출/종료·예약 identity를 재사용한다. 이미 확보한 결과와 `agents/process_receipt.py`·기존 launch 자료를 같은 invocation에 연결해 저장 대기 뒤 확정하며 새 ledger·인증/설정 캐시·관측 서비스를 만들지 않는다. 실제 provider 결과가 없는 과거 행은 추측으로 채우지 않는다.
- [ ] 확인한 `storage/targeted_test_state.py`와 Worker의 기존 수집 pending 경계를 유지한다. G-292는 Claude의 callback·허용 경로 전달을, G-185는 provider 사용량 범위·파싱·조회와 예외 원문 회수를 담당한다. 이 작업은 공통 DB 경합의 분류·기존 결과 보존만 맡고 해당 기능을 중복 구현하지 않는다.
- [ ] 확인한 `tests/test_agent_invocation.py:RunnerTests`·`tests/test_agent_invocation_state.py`·`tests/test_task_scheduling.py`의 실제 저장/호출 예약 fixture에 DB 대기 위치별 최소 unit을 연결한다. PRODUCT·ARCHITECTURE의 Agent 준비·호출 저장 절은 실제 구현한 결과로 정합화한다. G-191의 보존된 Claude 구현·실행 이력은 변경하지 않는다.

## Validation

- Worker는 위 파일의 변경 관련 최소 unit case만 실행한다. provider는 double로 사용하고 실제 invocation/session·예약·targeted 저장 정책을 검사한다. 필수 준비 전 BUSY 해소/한도 초과/취소에서 mock CLI 호출 수가 각각 1/0/0이며 일반 실패·교정 횟수는 늘지 않아야 한다.
- 실제 결과 반환 직후 session 저장 대기, 격리/실행 identity 저장 대기, 시작·종료 callback 저장 대기와 targeted 결합 실패를 구분한다. 선택적 usage 오류와 필수 안전 증거 부재의 결과가 달라야 하고 실행 중 timeout·실제 provider 실패·종료 불명은 그대로 유지해야 한다.
- 물리 성공/실패 뒤 DB 확정 전 중단·재시작에서 기존 invocation·receipt·session과 관측 사용량을 보존하고 provider 재호출·중복 종료·가짜 PASS가 없어야 한다. 새 저장 오류는 원래 오류를 로그에서 가리지 않아야 한다.
- 기존 `test_ledger_failure_does_not_change_agent_success`·`test_auxiliary_collection_error_keeps_success_and_usage_in_ledger`와 targeted 중단/retry/fallback·독립 provider 실패 회귀를 유지한다. 같은 파서/사용량 범위 assertion을 복제하지 않으며 mock 결과를 실제 Claude/Codex 계정·OS 격리 성공으로 기록하지 않는다.

## Progress

- [x] 현재 revision의 invocation 서비스·영속화·실제 결과 보존 회귀를 정적으로 대조했다. 최초 guidance DB 대기 전파와 선택적 ledger 오류의 기존 성공 보존은 재구현 대상에서 제외한다.
- [ ] 필수 준비/실제 실행/저장 확정의 DB 대기 분리
- [ ] 최소 targeted 회귀와 중단 뒤 비중복 확정 검증

## Decision Log

- complexity high: 필수 안전 증거와 선택 수집을 구분하면서 실제 CLI 시작 전/실행 중/완료 뒤의 대기·취소·결과 보존을 기존 session·호출 예약 수명과 결합해야 한다. risk high: 잘못된 분류는 증거 없는 호출·승인이나 완료한 writer의 재실행을 만들 수 있다. 실제 receipt·invocation identity·기존 자료를 유지하고 불확실하면 해당 후속 처리만 보류하며 준비/완료 경계의 중단 회귀로 검증한다. priority high는 무인 실행의 정확한 호출·안전 경계 보완 기준이다.
- R-073의 사용량 관측 확대나 G-191의 Claude 정책 구현과 별개다. 일반 DB 대기를 provider 실패로 처리하지 않되 실제 격리·인증·권한·도구 오류의 안전 거부는 유지한다. 실계정 실행·새 infrastructure·권한 확대나 사용자 결정을 추가하지 않는다.
