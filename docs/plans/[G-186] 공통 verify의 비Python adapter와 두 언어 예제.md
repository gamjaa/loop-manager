---
id: G-186
title: 공통 verify의 비Python adapter와 두 언어 예제
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
depends_on:
  - G-148
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
- 현재 `scripts/verify.json`·`docs/VERIFY.md`와 `verification/contract.py`·`runner.py`·`core.py`의 version 1 계약을 기준으로 adapter metadata와 실제 구현 등록을 함께 연결한다. Manager runtime과 프로젝트 실행 도구, run·단계·단위 identity를 구분하고 기존 unittest 수집·fixture·필수 compile 및 선언 오류 거부를 유지한다. 지원 버전·공식 참조·적합성 한계와 두 언어 연결 예제는 `docs/VERIFY.md`에 반영하며 현재 core와 unittest adapter를 다시 구현하지 않는다.
- 예제와 적합성 fixture는 기존 tests/support 또는 scripts 하위의 적절한 위치에 둔다. 실제 설치 도구를 구현 환경에서 확인하고 지원 버전·필수 의존성·지원하지 않는 계약을 명시한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- 공통 adapter 적합성 테스트에서 비Python 수집/완결성·오류·skip·내부 병렬 설정·Agent 차단의 최소 unit case를 실행한다. Worker는 전체 integration을 실행하지 않는다.
- Manager 전체 verify는 실제 두 언어 예제의 같은 core 사용·필수 검증 범위·단독/Manager 실패와 격리를 검증한다. 도구 부재를 성공/skip으로 위장하지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-09 `bac6f84e`와 mutable snapshot token `72f8256fbf8d111d1e3b5471dfeb314312d188d8eeec9fbcb704063918d9c50a`에서 version 1의 공통 core·unittest 연결 및 적합성 회귀를 정적으로 대조했다. registry와 실제 구현은 unittest만 제공하며 비Python 도구·두 언어 예제는 남아 있다. 구현·테스트·verify는 실행하지 않았다.
- [x] 2026-10-14 Perl 5.38.2 / Test::More 1.302194 / TAP::Parser 3.44를 확인하고 정적 registry·프로젝트 도구 선언·파일 수집·native reporter·공통 worker IPC/예약·별도 Perl guard를 연결했다. Python/Perl의 compile → tests 예제와 Manager 전용 실제 호출 통합 회귀를 작성했다. `tests.test_verify_perl_adapter` 10개 unit case와 entrypoint 등록 1개, 기존 선언/단계 순서/시작 실패·종료 불명 3개 targeted case를 통과했다. 첫 실행의 테스트 코드 오류와 후속 native bailout의 null plan 처리 오류는 같은 명령 재실행으로 교정했다. 전체 verify·통합 회귀·실제 forced termination·Windows 및 동시 실행 실측은 실행하지 않았다.
- [x] 2026-10-14 Manager verify의 `ee82674c69f46563d364dfabc6713450e7c33c5edeed1cdefe13362d25fb773d` 실패 진단에서 Perl native JSON Lines가 공통 `*.jsonl` 이벤트 집계에 들어가 `KeyError: kind`를 일으킴을 확인했다. native 원본을 `*.native.ndjson`으로 분리하고 성공·중단의 진단 생성 및 시작/통신 실패 원본 보존을 두 개의 targeted unit case로 검증했다. 회귀 확인의 동일 case는 최초 `KeyError`와 중단 phase 기대값 오류를 교정한 뒤 통과했다. Worker는 통합 테스트·전체 verify를 재실행하지 않았으며 Manager 재검증이 필요하다.
- [x] 2026-10-14 관리 rebase 후 base `202d8ef` 대비 현재 후보의 전체 변경과 sources를 다시 확인했다. Review의 bailout-success를 명시적 bailout 거부와 완료 plan·종료 코드 0 회귀로 교정했다. platform-regression은 플랫폼 독립 임시 도구 fixture와 Linux native case의 프로젝트 PATH 경로로 교정했으며 Python/Perl 통합 case를 분리해 Windows의 Python 검증과 기존 PowerShell 선택을 유지했다. 현재 후보의 adapter unit module 12개 case와 Windows 수집 정책 unit case를 통과했다. bailout 회귀의 최초 실패는 동일 명령 재실행으로 통과했다. 통합·전체 verify·실제 Windows 검증은 실행하지 않았으며 Manager 재검증이 필요하다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 두 언어의 결과·수집 의미를 하나의 계약에 결합해 complexity high, 필수 테스트 누락/실제 Agent 호출 영향으로 risk high다. 외부 등록 저장소 변경은 이 저장소 예제의 범위에 포함하지 않는다.
- 현재 snapshot의 ID·경로·metadata·dependency·기존 기록을 보존한다. 남은 high complexity는 두 번째 도구의 수집/실행 identity·동적 등록과 native fixture/skip/error를 공통 완결성 판정에 연결하고 언어별 Agent 차단 경계를 확인하는 판단이다. high risk는 누락된 검증의 성공 처리나 실제 Agent 호출이며, 원본 결과·종료 진단을 보존하고 외부 호출 중단 및 잘못 게시한 후보의 수동 교정이 필요하다. Validation의 부분 결과·forced termination·Agent 차단과 Reviewer의 필수 범위 확인으로 이 영향을 검증한다.
