---
id: G-184
title: Agent 호출 목적과 실제 실행 여부 기록
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-073] Invocation Purpose Usage Scope and Queries.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

기존 invocation ledger에 실제 호출 위치의 목적과 실행 준비/실제 실행/미확인 상태를 연결한다.

## Acceptance Criteria

- 일반 planning·최종 확인·유지보수 검토/발견 처리/계획 작성·Worker 구현/검증 교정·Review 교정·충돌 해결의 목적을 실제 호출 경계에서 기록한다. 생략된 단계를 목적 분류만을 위해 실행하지 않는다.
- 실제 CLI 종료·실패·timeout·중단 직후 invocation identity·대상·profile/CLI 버전·session 전이/retry·시작/종료/소요 시간·결과와 사용량 관측을 연결한다.
- 설정/격리 거부·프로세스 생성 실패·모델 없는 probe는 실제 호출 수에서 제외하고 실행 여부 불명은 따로 표시한다. legacy 기록의 목적·실행 여부를 추측하지 않는다.
- 중단/재시작의 동일 종료를 중복하지 않는다. 관측/로그 저장 실패는 Agent 성공·작업 상태를 바꾸거나 재호출하지 않으며 필수 안전 증거 저장 실패와 구분한다. 비밀/prompt/코드 전문을 추가하지 않는다.

## Implementation

- agents/invocation_service.py·실제 process callback과 storage/agent_invocation_state.py의 기존 ledger를 확장한다. 목적은 호출자가 명시하고 실행 여부는 실제 경계 증거로 기록한다.
- 기존 session transition·provider usage·durable 로그를 재사용한다. optional 관측 실패와 필수 격리/소유권 검증의 예외 책임을 분리한다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_agent_invocation.py, test_agent_invocation_state.py와 provider runtime 테스트에서 시작 전 거부·생성 실패·실제 종료/timeout·fallback·legacy·중복 종료의 최소 unit case를 실행한다.
- Manager 전체 verify는 모든 호출 경계 목적·상태 연결, 관측 실패의 작업 지속, 필수 safety 실패 유지·비밀 가림을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 기존 ledger의 관측 필드와 호출 위치 연결로 complexity medium, 잘못된 호출 집계 영향으로 risk medium이다. provider 범위와 조회는 G-185에서 추가한다.
