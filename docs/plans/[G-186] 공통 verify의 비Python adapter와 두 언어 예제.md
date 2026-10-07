---
id: G-186
title: 공통 verify의 비Python adapter와 두 언어 예제
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
depends_on:
  - G-148
complexity: high
risk: high
priority: normal
---

## Purpose

G-148의 공통 core에 최소 하나의 실제 비Python 도구를 연결하고 Python과 다른 언어의 적합성 예제를 제공한다.

## Acceptance Criteria

- 실제 수집·구조화 native reporter·프로세스 단위 실행·내부 병렬 제어가 가능한 비Python 도구/지원 버전을 공식 근거로 선택한다. stdout 추측·가짜 metadata로 지원을 선언하지 않는다.
- 두 언어 예제는 같은 Manager core의 manifest/event/result·예약·종료·진단을 사용하고 테스트 assertion/fixture 의미는 adapter에서 유지한다. 예제에 순서 있는 필수 compile/build 또는 명령 단계, Manager와 단독 호출을 포함한다.
- 수집/실행 ID 관계·예상 범위 완결성·skip/error·시작/통신 오류·부분 결과·forced termination을 확인한다. 연결 오류는 실패이며 미지원 미연결 프로젝트는 기존 경로를 유지한다.
- 초기 adapter의 내부 병렬을 끄고 실제 Agent 경계를 언어에 맞게 차단한다. Python guard 하나로 모든 언어를 보호했다고 주장하지 않으며 테스트는 계정·인증·외부 AI 응답에 의존하지 않는다.

## Implementation

- G-148의 정적 adapter registry·공통 process 계약을 재사용한다. 필요한 도구별 수집/fixture/reporter 책임만 추가하고 일반 DAG·자동 설치·새 infrastructure를 만들지 않는다.
- 예제와 적합성 fixture는 기존 tests/support 또는 scripts 하위의 적절한 위치에 둔다. 실제 설치 도구를 구현 환경에서 확인하고 지원 버전·필수 의존성·지원하지 않는 계약을 명시한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- 공통 adapter 적합성 테스트에서 비Python 수집/완결성·오류·skip·내부 병렬 설정·Agent 차단의 최소 unit case를 실행한다. Worker는 전체 integration을 실행하지 않는다.
- Manager 전체 verify는 실제 두 언어 예제의 같은 core 사용·필수 검증 범위·단독/Manager 실패와 격리를 검증한다. 도구 부재를 성공/skip으로 위장하지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 두 언어의 결과·수집 의미를 하나의 계약에 결합해 complexity high, 필수 테스트 누락/실제 Agent 호출 영향으로 risk high다. 외부 등록 저장소 변경은 이 저장소 예제의 범위에 포함하지 않는다.
