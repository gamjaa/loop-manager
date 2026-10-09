---
id: G-196
title: 기존 호출 ledger의 읽기 전용 usage CLI
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] Read-Only Operational State CLI.md
depends_on:
  - G-194
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: medium
risk: medium
priority: normal
---

## Purpose

`usage <repository> [--plan <id>] [--json]`으로 이미 저장된 호출·사용량을 제공한다. 기존 summary의 포함 관계와 집계를 보존하면서 부분 수집·미수집·실측 0을 구분한다. R-073의 수집 확대 없이 독립적으로 사용할 수 있는 조회가 완료 조건이다.

## Acceptance Criteria

- 프로젝트 조회는 계획 연결 없는 Planner·유지보수 호출도 포함한다. plan 조회는 해당 프로젝트의 지정 계획에 연결된 호출만 포함하며 미연결 호출을 배분하지 않는다. 명시한 종료 계획도 조회하며 정상적인 빈 범위와 알 수 없는 프로젝트/계획을 구분한다.
- 기존 `storage/agent_invocation_state.py`의 `summary()`와 같은 provider·Model·역할·Effort·session 전이별 집계를 제공한다. `calls`는 ledger 행 수, `retries`는 `attempt > 1`인 행 수라는 의미를 유지하고 실제 모델 실행·청구 호출 수로 단정하지 않는다. 목적·실행 여부·범위가 없는 과거 기록은 추정하지 않는다.
- 기록된 input·cached input·cache write input·output·reasoning output token과 소요시간은 provider의 포함 관계를 보존한다. cache·reasoning을 input·output에 다시 더하지 않으며 비교 가능한 값만 기존 cache 비중에 사용한다.
- 각 token·시간 지표에 알려진 부분 합계·알려진 호출 수·미수집 호출 수와 수집 상태를 제공한다. 전부 미수집이면 미확인, 일부 수집이면 부분 합계, 실측 0이면 0으로 표현한다. 진행 중·중단·실패 호출의 누락을 드러내며 빈 호출 범위에서도 미수집 token·시간을 가짜 0으로 채우지 않는다.
- G-194의 일관된 읽기 snapshot, 관측 시각·출처·미확인 의미, 기본 출력/JSON 공통 envelope와 오류·종료 코드·비밀값 제거를 따른다. 실행·수집·invocation 정리·기록 갱신을 하지 않으며 금액 비용·예산·추정 절감은 제공하지 않는다.
- 최신 R-076의 list/status/usage 공통 envelope를 사용하며 usage의 source는 null이다. 도움말·예시에서 통합 status와 새 등록 명령을 사용하고 별도 explain이나 제거된 config list/add/remove를 안내하지 않는다.
- 기존 사용자 문서에 usage 범위와 모든 추가 필드·자료형·단위·nullable 의미·버전 호환, provider 포함 관계·ledger 집계 의미·수집 상태 및 정상·부분·전부 미수집·실측 0·오류 예시를 기록한다.

## Implementation

- 현재 summary와 provider fixture를 재사용해 같은 집계 경계에 optional plan 필터와 지표별 알려진/미수집 분모를 추가한다. 기본 summary 소비자의 의미를 유지하고 별도의 중복 SQL·provider 집계 규칙을 만들지 않는다.
- G-194의 조회 연결과 결과·출력 경계에 usage를 연결한다. schema 변경이나 새로운 수집 서비스 없이 읽기 가능한 기존 기록의 한계를 표시하며 R-073의 기간·목적·실행 여부 확장은 G-185에서 같은 경계를 확장한다.
- 상위 명세는 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를, 자동 역할·검증 책임은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를 따른다.

## Validation

- Worker는 tests/test_agent_invocation_state.py의 summary fixture를 재사용한 targeted unit test로 두 provider, 역할·Model·Effort·session 전이, retry 행 수, plan 필터·미연결 호출·다른 프로젝트 격리, 빈 결과와 지표마다 다른 부분 수집·전부 미수집·실측 0을 검증한다.
- 미종료·중단·실패 호출과 schema 선택 정보 누락에서 token·시간 분모와 경고를 검증하고 기존 summary 소비자의 값·cache 포함 관계가 유지되는지 확인한다.
- Manager 전체 verify는 usage의 정상·오류·JSON 경로에서 외부 실행·수집·알림 차단, DB 및 중단 호출 기록 불변, 동시 조회의 일관성, 기본 출력/JSON·문서 계약과 기존 ledger·provider 파싱 회귀를 실제 provider 없이 검증한다.

## Progress

- [x] 2026-10-06 기준 6b717e3의 R-076·R-073, 기존 summary와 provider·invocation 테스트를 대조했다. CLI와 plan 필터·지표별 미수집 분모가 없으며 기존 기본 집계는 재사용한다. 구현·테스트 실행은 하지 않았다.
- [ ] usage 구현·관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-076은 R-073 전체 구현을 기다리지 않는다. G-184·G-185와 반대 방향의 dependency를 만들지 않고 G-185가 이 조회를 확장하게 한다.
- 호출 행 수와 실제 실행 수는 다른 관측이다. 후속 수집 확대 후에도 기존 calls/retries 의미를 명시적으로 유지한다.
- 2026-10-09: 제공된 snapshot과 원본 hash를 확인하고 확장된 R-076의 공통 출력만 반영한다. complexity medium은 기존 집계에 필터·미수집 분모와 읽기 전용 출력 연결을 추가하는 경계가 명확한 변경이다. risk medium은 조회 범위·부분 합계의 잘못된 표시이며 원본 ledger를 변경하지 않아 출력 교정으로 복구할 수 있다. 구현·테스트 실행 없음.
