---
id: G-292
title: Claude Worker의 targeted 실행 기록 전달
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
depends_on:
  - G-191
  - G-264
  - G-291
complexity: medium
risk: medium
priority: high
---

## Purpose

Claude 지원 경계가 준비된 뒤 Worker targeted 기록을 이미 구현된 호출 결합·재수집에 연결한다. `AgentRequest.on_targeted_prepared(directory, receipt)`와 공통 durable 수집은 원격에 반영됐지만 `ClaudeAgentAdapter`·facade·`run_claude()`는 이 callback과 runner/기록 폴더를 전달하지 않는다. 현재 Claude attempt는 `state/logs/<worktree 해시>/claude-...`이고, 공통 결합은 `state/agent-runtime/<runtime>/<call>`과 call 바깥의 process receipt를 요구한다. 공급자 준비 문맥에서 이 계약에 맞는 실제 경로·종료 근거를 전달하는 잔여 작업만 다룬다. G-191의 내장 정책과 G-264의 개인 설정 제외·일반 임시 경로 분리는 담당 선행을 재사용한다.

## Acceptance Criteria

- R-055: 지원 가능한 Claude Worker의 신규·재개·retry·fallback 호출마다 프로젝트 Python으로 실행할 runtime targeted runner와 현재 호출의 쓰기 허용 기록 폴더를 제공한다. 완료·중단한 개별 결과는 G-291의 동일 repository·ExecPlan·물리 invocation 문맥으로 수집·재관측한다.
- 실제 프로세스 시작 전에 `on_targeted_prepared`로 정규 call 디렉터리·파일시스템 identity와 call 바깥 receipt를 결합한다. receipt는 그 Claude writer의 실제 시작·종료 확인과 같은 경로여야 하며 기존 invocation·작업 호출 예약의 귀속과 일치해야 한다. callback/결합 저장 실패는 launch를 시작하지 않는다. Codex launch 객체나 종료 코드만으로 Claude 종료·격리 근거를 합성하지 않는다.
- R-085: 공통 인증 홈·개인 원본·다른 호출의 임시 폴더를 기록 쓰기 범위로 사용하지 않는다. 부모 환경·설정 source·session·실패/사용량 기록을 보존하며 provider 전환이나 권한 확대로 수집 누락을 우회하지 않는다. Reviewer·읽기 전용 Planner에는 Worker 기록 권한을 추가하지 않는다.
- G-191/G-264의 실제 정책/환경 확인을 통과하기 전 Claude를 실행하거나 미지원 환경을 허용하지 않는다. 정책 거부·준비 실패는 테스트 실패가 아니며 결과가 없는 호출을 세 번 실패로 세지 않는다. Codex의 이미 구현된 runner 전달·기존 일반 호출 계약과 수집을 유지한다.
- 같은 테스트의 세 실제 실패, 다른 테스트의 실패, skip/expected failure·성공에 의한 초기화와 새 실행 근거 부재 pause가 provider에 따라 달라지지 않는다. 실제 CLI/계정·OS 호환성 성공을 mock 회귀로 합성하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/agents/provider_adapters.py:ClaudeAgentAdapter/run_claude`와 `agents/agent_adapter.py:run_claude` facade에 `AgentRequest.on_targeted_prepared`를 전달한다. `invocation_service.py:observe_targeted`의 이미 구현된 bind를 재사용하고, G-191/G-264 뒤 실제 준비 환경·허용 call 디렉터리·receipt를 프로세스 생성 전에 한 번 연결한다. 현재 logs 아래 attempt를 그대로 결합해 실패시키거나 별도 collector로 경로 검사를 우회하지 않는다.
- [ ] 확인한 `agents/agent_codex_sandbox.py`의 Worker runner 환경 전달과 `agents/process_receipt.py`·`storage/agent_invocation_state.py`·`storage/task_calls.py`의 호출/예약 귀속을 소비한다. 선행이 준비한 Claude writer의 시작·종료 receipt와 targeted callback을 같은 문맥에 연결하고, 실제 종료 확인 뒤의 공통 재수집·pending 표식을 사용한다. 기록 누락으로 물리 provider 결과·사용량·session을 덮지 않으며 별도 streak ledger·runner·인증 정책은 만들지 않는다.
- [ ] 확인한 `tests/test_agent_invocation.py`, `tests/test_provider_runtime.py`, `tests/test_native_platform_agents.py`, `tests/test_codex_native_invocation.py`의 provider fixture에 환경·callback·거부/재개 회귀를 추가한다. G-291의 실제 파일·SQLite 수집 경계를 사용하며 Claude 결과를 단순 성공값으로 주입해 전달 검증을 대신하지 않는다. PRODUCT·ARCHITECTURE·필요한 Worker 안내에는 실제 지원 범위와 기록 연결만 정합화한다.

## Validation

- Worker는 변경한 Claude 환경 전달·수집 callback의 최소 unittest case만 실행한다. provider 프로세스는 double로 두고 실제 임시 기록 파일→공통 collector→SQLite를 연결해 세 호출의 동일 테스트 실패와 서로 다른 테스트 실패를 비교한다. Git·실계정·유료 Agent를 실행하지 않는다.
- 신규·동일 session 재개·transient retry·session fallback의 각 호출에 고유한 허용 기록 경로가 전달되고 동일 ExecPlan의 실패 이력은 이어져야 한다. 별도 작업·역할·공급자·상태 루트의 폴더나 결과는 섞이지 않아야 한다.
- 준비/정책 거부와 기록 없는 중단은 실패 횟수를 만들지 않아야 한다. 완료 결과 뒤 interruption과 재시작 재관측은 한 번만 반영되고 세 번째 실패 뒤 새 CLI retry·verify는 없어야 한다. 읽기 전용 역할의 쓰기 정책·부모 환경·인증/개인 설정 원본은 유지돼야 한다.
- facade→adapter→prepared callback→실제 임시 파일→공통 collector/SQLite 연결을 확인한다. callback의 경로/receipt와 invocation·호출 예약 identity가 일치하고 저장 실패에서는 모의 process launch도 호출되지 않아야 한다. live·종료 불명과 경로 교체에서는 미소비 원문을 유지하며 종료 확인/오류 해소 뒤 재수집돼야 한다. 기존 Codex `tests/test_agent_invocation.py`의 KeyboardInterrupt·retry/fallback·독립 provider 실패 회귀를 재사용하고 지원되지 않는 Claude OS/정책 거부와 실환경 미수행 구분을 유지한다.

## Progress

- [x] 현재 provider별 환경·facade·callback·수집 조건과 G-191/G-264의 기존 책임을 읽기 전용으로 대조했다. Claude의 관리 호출은 현재 안전 거부되며 그 해제는 이 계획의 목표가 아니다.
- [ ] 선행 Merge 뒤 실제 Claude 준비 환경과 공통 기록 port 연결
- [x] revision `937b03f1dd9e182e0e48e565ea093dc560f2de5b`에서 공통 `on_targeted_prepared`·durable 결합/수집과 기존 회귀를 정적으로 확인했다. Claude facade/adapter의 미전달·logs 아래 attempt와 공통 경로 계약의 차이는 남아 있다. 새 구현·검증 성공은 기록하지 않는다.
- [ ] provider 전달·중단·거부의 최소 unit 회귀 및 관련 설명

## Decision Log

- snapshot token `e108f535e66d7a1ec6bbe0c12265d2b209bdfe14e05e4bc20c155efd5dc80e7c`과 원본 hash `2612b2f7871601f112a3df3795f52e69efa3a03502d3fe4734352de30701be8d`를 대조해 새 공통 port·경로·receipt 계약을 같은 ID에 반영했다. G-291의 구현은 재사용하고 기존 G-191/G-264/G-291 dependency·목표·metadata·기록을 보존한다. G-302의 검증 재개 gate와 독립 구현 경계이므로 이 연결을 기다리게 하지 않는다. 이번 delta는 프로젝트 최종 전문 확인·실계정/OS 검증이 아니다.

- G-264는 인증/개인 설정 source, G-191은 내장 쓰기 정책, G-291은 공급자 공통 기록 수명, 이 작업은 Claude의 전달 연결을 소유한다. 선행 준비가 필요한 이 연결을 Codex의 독립 native 분류·중단 복구에 묶지 않는다. G-290의 결과 의미는 공통 집계가 담당하며 이 계획이 별도 분류 정책을 만들지 않는다.
- complexity medium: 선행이 확정한 준비 환경·허용 임시 경로·물리 callback을 기존 facade와 공통 수집에 연결하는 경계 작업이다. risk medium: 누락/오귀속은 한 작업의 진행·집계를 잘못 만들 수 있으나 원문·호출/저장 감사와 기존 정책 거부를 유지해 재관측할 수 있다. 경로 분리와 실제 collector/SQLite 회귀로 완화한다. priority high는 공급자별 필수 차단 정책 누락을 막기 위한 것이다.
- unittest version 1·launcher Python 3.11–3.14·필수 compile과 기존 Agent guard를 유지한다. Planner는 구현·테스트·verify를 실행하지 않았다. 실계정·새 OS 지원·인증 방식 변경과 G-191 판단을 대신하지 않는다.
