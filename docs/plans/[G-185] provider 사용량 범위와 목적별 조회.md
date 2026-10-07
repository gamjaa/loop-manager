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
complexity: high
risk: medium
priority: normal
---

## Purpose

기존 provider token 파싱에 범위·포함 관계·부분 수집 의미를 보존하고 알려진 분모로 조회한다.

## Acceptance Criteria

- Codex/Claude의 turn·call·session 누적, 하위 Agent 포함과 모델별 원본을 식별한다. 중간/최종/모델별 중복을 합산하지 않고 input의 cache read·output의 reasoning 포함 관계를 보존한다.
- 누적 증가분은 비교 가능한 이전 범위·reset 증거가 있을 때만 계산한다. 누락/부분 필드/형식 오류/중단은 0·완전 합계로 표시하지 않고 누적 원본과 미확인 증가분을 구분한다.
- 기간·프로젝트·역할·목적·model/effort·session 전이별 실제 호출·시간·retry/실패 비중과 실행 전 거부/미확인, 토큰별 부분 합계·알려진 대상 평균·누락/오류 수를 조회할 수 있다.
- 같은 provider/범위의 비교 가능한 값만 cache 비중을 계산하고 큰 비용/반복 교정에서 원래 로그·계획을 찾을 수 있다. reuse·no-call은 호출/사용량이 아니며 비용·미측정 절감·legacy 범위를 추정하지 않는다.
- G-196의 읽기 전용 usage·공통 출력/오류·부분 수집 계약을 유지하며 기간·목적·실행 여부 조회를 확장한다. 기존 `calls`/`retries`의 ledger 행 수 의미와 R-073의 실제 실행 수를 별도로 명시하고 같은 집계로 혼합하지 않는다.

## Implementation

- agents/agent_provider_usage.py와 기존 provider 구조화 출력 fixture·ledger summary를 확장한다. provider별 범위/포함 관계를 보존하고 raw source identity로 dedup한다.
- G-196이 제공한 usage CLI와 기존 ledger 집계 경계에 기간·목적·실행 여부 필터를 확장한다. R-076의 읽기 전용 연결·snapshot·JSON/오류·미수집 계약을 재사용한다. optional 수집/조회 오류를 진단하면서 작업 성공·필수 safety·재시도 정책과 분리하고 새 측정 서비스를 만들지 않는다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_agent_provider_usage.py, test_agent_invocation_state.py와 조회 parser 테스트에서 두 provider·중복·부분·누적/reset·legacy·필터/분모의 최소 unit case를 실행한다.
- Manager 전체 verify는 실제 Agent 없는 fixture 사용, 기본 집계 및 G-196 CLI 계약 호환성, ledger 행 수/실제 실행 수의 구분, 중단·수집/조회 실패의 작업 지속과 비밀 비노출을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 기준 6b717e347bb4d9a1acbca5de26079ffdf528ffe5와 mutable READY snapshot token `37c91f6fd44bc210be11ab0ea09f9dcba26097efd1034321dd88594dbf69821e`의 원본 hash를 확인하고, R-076의 독립 usage 제공 뒤 같은 조회 경계를 확장하도록 dependency·호환 조건을 개정했다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- provider별 범위·누적·집계를 결합하므로 complexity high, 관측 합계 오류 영향으로 risk medium이다.
- G-196은 기존 기록 조회만 제공하고 R-073의 수집 확대·기간/목적 분석을 대신하지 않는다. G-184 dependency와 기존 목표를 보존하며 G-196을 선행시켜 중복 CLI·집계 구현을 피한다.
