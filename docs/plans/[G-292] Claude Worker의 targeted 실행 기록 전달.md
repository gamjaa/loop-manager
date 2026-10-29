---
id: G-292
title: Claude Worker의 targeted 실행 기록 전달
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-191
  - G-291
  - G-305
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: medium
risk: medium
priority: high
---

## Purpose

관리 Claude의 실제 기록 파일이 물리 invocation·종료 receipt와 함께 공통 collector/SQLite까지 이어지는 남은 회귀를 완성한다. 현재 원격에는 facade→adapter→`on_launch_prepared` 전달, Worker runner/owned attempt 환경과 process receipt가 구현돼 있다. `test_managed_service_persists_call_session_policy_and_targeted_binding`은 실제 service·SQLite 연결을 검사하지만 기록 결과는 0건이다. 비어 있지 않은 기록의 retry/fallback·수집 대기를 이 실제 Claude 연결에서 확인하며, 이미 구현된 전달 경로·정책·collector는 다시 만들지 않는다.

## Acceptance Criteria

- R-055: 지원 가능한 Claude Worker의 신규·재개·retry·fallback 호출마다 프로젝트 Python으로 실행할 runtime targeted runner와 현재 호출의 쓰기 허용 기록 폴더를 제공한다. 완료·중단한 개별 결과는 G-291의 동일 repository·ExecPlan·물리 invocation 문맥으로 수집·재관측한다.
- 실제 프로세스 시작 전에 기존 `on_launch_prepared`→`observe_launch()`→`observe_targeted()`가 정규 call 디렉터리·파일시스템 identity와 call 바깥 receipt를 결합한다. receipt는 그 Claude writer의 실제 시작·종료 확인과 같은 경로이며 invocation·작업 호출 예약의 귀속과 일치해야 한다. callback/결합 저장 실패는 launch를 시작하지 않는다. 별도 `on_targeted_prepared` 전달을 중복 추가하거나 Codex의 verified 정책을 Claude 종료·격리 근거로 대용하지 않는다.
- R-091의 일시 DB 경합은 G-305의 공통 호출 준비/저장 대기로 처리한다. 결합 저장이 확정되기 전 미시작을 유지하되 이를 Claude 정책 실패·targeted 실패·교정 소진으로 바꾸지 않는다. 실제 실행 뒤의 수집 대기에서는 물리 결과·사용량·session과 미소비 자료를 보존하고 완료한 CLI를 반복하지 않는다.
- R-085: 공통 인증 홈·개인 원본·다른 호출의 임시 폴더를 기록 쓰기 범위로 사용하지 않는다. 부모 환경·설정 source·session·실패/사용량 기록을 보존하며 provider 전환이나 권한 확대로 수집 누락을 우회하지 않는다. Reviewer·읽기 전용 Planner에는 Worker 기록 권한을 추가하지 않는다.
- R-023의 현재 정책/환경 확인을 통과하기 전 Claude를 실행하거나 미지원 환경을 허용하지 않는다. `prepared` 구성 근거와 실제 process/기록 관측을 구분한다. source 소비 교정은 G-264에 유지하고 이 작업은 고정된 안전한 managed fixture에서 기존 호출 경로를 검사한다. 정책 거부·준비 실패는 테스트 실패가 아니며 결과 없는 호출을 실패로 세지 않는다. Codex 전달·일반 호출 계약은 유지한다.
- 같은 테스트의 세 실제 실패, 다른 테스트의 실패, skip/expected failure·성공에 의한 초기화와 새 실행 근거 부재 pause가 provider에 따라 달라지지 않는다. 실제 CLI/계정·OS 호환성 성공을 mock 회귀로 합성하지 않는다.

## Implementation

- [ ] 확인한 `tests/test_claude_managed_runtime.py:ClaudeManagedRuntimeTests`의 process double을 확장해 실제 전달된 `LOOP_MANAGER_TARGETED_TEST_DIRECTORY` 안에 표준 `EventWriter`의 고유 run 결과를 쓴다. `manager.run_agent('claude', ...)`·실제 `ClaudeAgentAdapter`/facade와 `invocation_service.observe_launch()`·공통 collector/SQLite를 그대로 연결하며 provider 전체를 가짜 성공 adapter로 바꾸지 않는다.
- [ ] `tests/test_agent_invocation.py:RunnerTests.test_targeted_records_follow_each_physical_retry_and_session_fallback`의 세 물리 호출·session 순서·run/invocation 귀속 assertion을 Claude 연결에서 필요한 한 사례로 재사용한다. 기존 provider-neutral 회귀는 보존하고 운영 retry 횟수·5회 threshold·failure 분류는 바꾸지 않는다. `agent_claude_preflight.prepare()`의 runner/attempt 설정, `provider_adapters.run_claude()`의 spawn 전 callback과 `process_receipt` 종료 근거는 현재 구현으로 소비한다.
- [ ] G-305의 DB 대기와 `storage/targeted_test_state.py`의 bind/reconcile·`agents/invocation_service.py`의 결과 저장 복구를 재사용해 실제 기록이 있는 Claude 호출의 준비 저장 실패·완료 뒤 수집 대기/재관측을 연결한다. 관측한 전달/수집 누락이 있을 때만 해당 provider 경계를 교정하며 별도 callback·collector·streak ledger·인증/source 정책은 만들지 않는다. 기존 빈 기록 및 결과 원문 복구 회귀와 같은 조건/assertion을 복제하지 않는다.

## Validation

- Worker는 `tests/test_claude_managed_runtime.py`의 새 비어 있지 않은 기록 연결과 수정한 오류 경계의 최소 unit case만 실행한다. process·CLI 시작 조회만 double로 두고 실제 임시 파일→공통 collector→SQLite를 사용한다. 기존 `tests/test_agent_invocation.py`의 세 호출 회귀와 `tests/test_targeted_test_streak.py`의 5회 도달/초기화 회귀는 재사용하고 같은 threshold 조합을 Claude별로 반복하지 않는다. Git·실계정·유료 Agent를 실행하지 않는다.
- 신규·동일 session 재개·transient retry·session fallback의 각 호출에 고유한 허용 기록 경로가 전달되고 동일 ExecPlan의 실패 이력은 이어져야 한다. 별도 작업·역할·공급자·상태 루트의 폴더나 결과는 섞이지 않아야 한다.
- 준비/정책 거부와 기록 없는 중단은 실패 횟수를 만들지 않아야 한다. 완료 결과 뒤 interruption과 재시작 재관측은 한 번만 반영되고 연속 다섯 번째 실패가 해소되지 않으면 새 CLI retry·verify는 없어야 한다. 후속 성공을 수집하면 실패 횟수와 도달 표식을 함께 초기화해야 한다. 읽기 전용 역할의 쓰기 정책·부모 환경·인증/개인 설정 원본은 유지돼야 한다.
- facade→adapter→launch callback→실제 임시 파일→공통 collector/SQLite 연결에서 세 고유 실행의 동일 테스트 누계 3·도달 표식 없음·서로 다른 run/invocation과 원래 session/fallback 순서를 확인한다. 경로/receipt·호출 귀속이 일치하고 준비 저장 실패에서는 spawn 0회여야 한다. 완료 뒤 수집/DB 대기에서는 원문·receipt·물리 결과/사용량을 보존하고 완료한 CLI를 반복하지 않으며 재관측 결과가 한 번만 반영돼야 한다. live·종료 불명·경로 교체·5회 도달/초기화의 공통 행렬은 기존 targeted 회귀로 유지한다.
- 공통 DB 대기의 일반 정책 조합은 G-305의 회귀를 재사용하고, Claude 전달 경계에서 결합 대기 해소 뒤 최초 launch 한 번·한도 초과 시 launch 0과 targeted 실패 미증가만 추가 확인한다. 실제 정책/경로 오류의 기존 거부와 구분한다.

## Progress

- [x] 현재 provider별 환경·facade·callback·수집 조건과 G-191/G-264의 기존 책임을 읽기 전용으로 대조했다. Claude의 관리 호출은 현재 안전 거부되며 그 해제는 이 계획의 목표가 아니다.
- [x] 원격 `d51829e329dc2ea6bda81f63f049d6caffeea1b8`의 G-191 구현에서 실제 Claude 준비 환경·runner·owned attempt·launch callback·receipt와 빈 기록의 service 수집 연결을 정적으로 확인했다. 이 Planner가 구현·테스트·검증 성공을 수행하거나 기록한 것은 아니다.
- [x] revision `937b03f1dd9e182e0e48e565ea093dc560f2de5b`에서 공통 `on_targeted_prepared`·durable 결합/수집과 기존 회귀를 정적으로 확인했다. Claude facade/adapter의 미전달·logs 아래 attempt와 공통 경로 계약의 차이는 남아 있다. 새 구현·검증 성공은 기록하지 않는다.
- [ ] 비어 있지 않은 기록의 실제 Claude retry/fallback·수집 대기/재관측 연결 회귀 및 관측 누락만 보완

## Decision Log

- 현재 delta의 G-191이 전달/경로 문제를 구현했으므로 남은 목표를 실제 기록 수집의 연결 검증으로 좁힌다. G-264는 R-085의 source 소비·조회 정합성 교정을 소유하고, 이 고정 fixture의 기록 검증에는 그 변경이 필요하지 않아 선행 edge를 제거한다. R-085의 인증/개인 설정 보존 조건과 G-191/G-291/G-305 및 나머지 dependency는 유지한다. source 정책 변경·Claude 지원 확대·공통 수집 재구현을 이 작업에 넣지 않는다.

- snapshot token `e108f535e66d7a1ec6bbe0c12265d2b209bdfe14e05e4bc20c155efd5dc80e7c`과 원본 hash `2612b2f7871601f112a3df3795f52e69efa3a03502d3fe4734352de30701be8d`를 대조해 새 공통 port·경로·receipt 계약을 같은 ID에 반영했다. G-291의 구현은 재사용하고 기존 G-191/G-264/G-291 dependency·목표·metadata·기록을 보존한다. G-302의 검증 재개 gate와 독립 구현 경계이므로 이 연결을 기다리게 하지 않는다. 이번 delta는 프로젝트 최종 전문 확인·실계정/OS 검증이 아니다.

- G-264는 인증/설정 source, G-191은 내장 정책·현재 전달 구현, G-291은 공통 기록 수명, 이 작업은 비어 있지 않은 Claude 기록의 남은 연결 회귀를 소유한다. G-339의 세 실패 assertion 교정도 원격 구현으로 소비하며 소급 ExecPlan·자동 성공을 만들지 않는다. G-290의 결과 의미와 공통 집계를 변경하지 않는다.
- complexity medium: 선행이 확정한 준비 환경·허용 임시 경로·물리 callback을 기존 facade와 공통 수집에 연결하는 경계 작업이다. risk medium: 누락/오귀속은 한 작업의 진행·집계를 잘못 만들 수 있으나 원문·호출/저장 감사와 기존 정책 거부를 유지해 재관측할 수 있다. 경로 분리와 실제 collector/SQLite 회귀로 완화한다. priority high는 공급자별 필수 차단 정책 누락을 막기 위한 것이다.
- unittest version 1·launcher Python 3.11–3.14·필수 compile과 기존 Agent guard를 유지한다. Planner는 구현·테스트·verify를 실행하지 않았다. 실계정·새 OS 지원·인증 방식 변경과 G-191 판단을 대신하지 않는다.
