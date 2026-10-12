---
id: G-185
title: provider 사용량 범위와 목적별 조회
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-073] Invocation Purpose Usage Scope and Queries.md
depends_on:
  - G-184
  - G-196
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: medium
priority: normal
---

## Purpose

기존 provider token 파싱에 범위·포함 관계·부분 수집 의미를 보존하고 알려진 분모로 조회한다.

## Acceptance Criteria

- Codex/Claude의 turn·call·session 누적, 하위 Agent 포함과 모델별 원본을 식별한다. 중간/최종/모델별 중복을 합산하지 않고 input의 cache read·output의 reasoning 포함 관계를 보존한다.
- 누적 증가분은 비교 가능한 이전 범위·reset 증거가 있을 때만 계산한다. 누락/부분 필드/형식 오류/중단은 0·완전 합계로 표시하지 않고 누적 원본과 미확인 증가분을 구분한다.
- 정상 종료뿐 아니라 실패·timeout·중단 직후에도 해당 호출에서 이미 관측한 구조화 사용량과 수집 상태를 invocation identity·원래 로그에 연결한다. 예외 경로가 관측한 값을 빈 `Usage()`로 대체하지 않으며, 회수하지 못한 출력·종료·범위는 미확인으로 남긴다. 선택적 수집 실패는 원래 Agent 결과·예외와 필수 격리 증거를 바꾸지 않는다.
- 기간·프로젝트·역할·목적·model/effort·session 전이별 실제 호출·시간·retry/실패 비중과 실행 전 거부/미확인, 토큰별 부분 합계·알려진 대상 평균·누락/오류 수를 조회할 수 있다.
- 같은 provider/범위의 비교 가능한 값만 cache 비중을 계산하고 큰 비용/반복 교정에서 원래 로그·계획을 찾을 수 있다. reuse·no-call은 호출/사용량이 아니며 비용·미측정 절감·legacy 범위를 추정하지 않는다.
- G-196의 읽기 전용 usage·공통 출력/오류·부분 수집 계약을 유지하며 기간·목적·실행 여부 조회를 확장한다. R-076의 CLI `calls`/`retries`는 기록된 시도 행 수로 명시하고, R-073의 실제 실행 수·실행 retry·실행 시간·실행 전 거부·미확인은 별도 필드로 구분한다. 현재 ledger `summary()`의 `calls`/`retries`는 이미 실행이 확인된 행만 세므로 이를 CLI의 시도 수로 그대로 옮기거나 미확인 행의 token·준비 시간을 실제 실행 합계에 넣지 않는다.

## Implementation

- agents/agent_provider_usage.py와 기존 provider 구조화 출력 fixture·ledger summary를 확장한다. provider별 범위/포함 관계를 보존하고 raw source identity로 dedup한다.
- G-184가 구현한 목적·target·process 시작/종료·실행 여부와 additive ledger를 재사용한다. `provider_adapters.py`에서 관측한 사용량이 `invocation_service.py`의 정상·예외 종료 저장까지 전달되는 경계를 보완하고, 누적 범위·중복 제거·부분 수집을 기존 invocation에 결합한다. 시작·종료 관측이나 목적 전달을 다시 구현하지 않고 구조화 자료가 없는 과거 로그를 추측으로 채우지 않는다.
- G-196이 제공한 usage CLI와 기존 ledger 집계 경계에 기간·목적·실행 여부 필터를 확장한다. R-076의 읽기 전용 연결·snapshot·JSON/오류·미수집 계약을 재사용한다. optional 수집/조회 오류를 진단하면서 작업 성공·필수 safety·재시도 정책과 분리하고 새 측정 서비스를 만들지 않는다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_agent_provider_usage.py, test_agent_invocation_state.py와 조회 parser 테스트에서 두 provider·중복·부분·누적/reset·legacy·필터/분모의 최소 unit case를 실행한다. 실제 실행·실행 전 거부·미확인·retry 행을 섞어 CLI 시도 수와 ledger의 실제 실행 수를 각각 대조한다.
- `tests/test_provider_runtime.py`·`tests/test_agent_invocation.py`의 최소 fixture에서 구조화 사용량 관측 뒤 실패·timeout·중단, 출력 회수 실패와 수집 저장 실패를 확인한다. provider process는 double로 대체하되 실제 파서·종료 저장을 실행하고, 관측한 부분값·원래 결과/예외·미확인 종료·필수 안전 gate가 보존되는지 검사한다.
- Manager 전체 verify는 실제 Agent 없는 fixture 사용, 기본 집계 및 G-196 CLI 계약 호환성, ledger 행 수/실제 실행 수와 준비/실행 시간의 구분, 중단·수집/조회 실패의 작업 지속과 비밀 비노출을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 기준 6b717e347bb4d9a1acbca5de26079ffdf528ffe5와 mutable READY snapshot token `37c91f6fd44bc210be11ab0ea09f9dcba26097efd1034321dd88594dbf69821e`의 원본 hash를 확인하고, R-076의 독립 usage 제공 뒤 같은 조회 경계를 확장하도록 dependency·호환 조건을 개정했다.
- [x] 2026-10-11 delta `d5ede15690d3006cf7115b27a5d69ee539dc43a3` → `5c3cb5ca75abdb597a6c5d063c70276e1e0c263d`, snapshot token `e295087f0dd5c3bcd450a01cede54cabe9d65ad9cbee9f8bff94a471ac75552d`와 원본 hash를 대조했다. G-184의 목적·실행 기록과 집계·회귀를 정적으로 확인하고 범위/누적 처리·예외 종료 사용량·조회의 남은 경계만 개정했다. 구현·테스트·전체 verify는 실행하지 않았다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity high: provider별 사용량 범위와 중복 이벤트 identity, 누적 재개/reset의 비교 가능성과 부분값 분모를 함께 판정할 설계 판단이 남아 있다. risk medium: 잘못된 판정은 제한된 관측 합계·평균을 왜곡하며 원래 ledger·로그를 보존해 집계를 교정할 수 있다. 실행·권한 정책을 바꾸지 않는다.
- G-196은 기존 기록 조회만 제공하고 R-073의 수집 확대·기간/목적 분석을 대신하지 않는다. G-184 dependency와 기존 목표를 보존하며 G-196을 선행시켜 중복 CLI·집계 구현을 피한다.
- 2026-10-11: 현재 `summary()`는 `executed`의 token·retry·`execution_duration_ms`만 집계하고 거부/미확인을 별도 반환한다. R-076의 시도 행 수 계약은 CLI에서 별도로 유지해 두 관측을 혼합하지 않는다. provider 예외 종료는 현재 빈 `Usage()`를 저장하며 Claude timeout 회귀는 회수 로그 보존까지만 확인하므로, 관측 사용량의 종료 저장은 이 계획의 기존 부분 수집 목표로 남긴다. G-184의 archive 이동은 삭제가 아니며 선행 ID를 유지한다. R-071의 rename source 호환 규칙에 따라 기존 source 문자열을 보존한다. 이번 검토는 프로젝트 최종 전문 확인이 아니다.
