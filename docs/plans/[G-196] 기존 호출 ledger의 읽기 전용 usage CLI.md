---
id: G-196
title: 기존 호출 ledger의 읽기 전용 usage CLI
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
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
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: medium
priority: normal
---

## Purpose

`usage <repository> [--plan <id>] [--json]`으로 이미 저장된 호출·사용량을 제공한다. 기존 summary의 포함 관계와 집계를 보존하면서 부분 수집·미수집·실측 0을 구분한다. R-073의 수집 확대 없이 독립적으로 사용할 수 있는 조회가 완료 조건이다.

## Acceptance Criteria

- 프로젝트 조회는 계획 연결 없는 Planner·유지보수 호출도 포함한다. plan 조회는 해당 프로젝트의 지정 계획에 연결된 호출만 포함하며 미연결 호출을 배분하지 않는다. 명시한 종료 계획도 조회하며 정상적인 빈 범위와 알 수 없는 프로젝트/계획을 구분한다.
- 기존 `storage/agent_invocation_state.py`의 집계 경계를 재사용해 provider·Model·역할·Effort·session 전이별 값을 제공한다. R-076의 CLI `calls`는 ledger 행 수, `retries`는 `attempt > 1`인 행 수로 유지하고 기록된 시도 수임을 명시한다. 현재 `summary()`의 동명 필드는 실행이 확인된 행만 세므로, CLI 시도 수와 실제 실행·실행 retry·실행 전 거부·미확인을 각각 구분해 표시한다. 목적·실행 여부·범위가 없는 과거 기록은 미확인으로 보존하고 실제 실행·청구 호출 수로 추정하지 않는다.
- 기록된 input·cached input·cache write input·output·reasoning output token과 소요시간은 provider의 포함 관계를 보존한다. cache·reasoning을 input·output에 다시 더하지 않으며 비교 가능한 값만 기존 cache 비중에 사용한다.
- 각 token·시간 지표에 알려진 부분 합계·알려진 호출 수·미수집 호출 수와 수집 상태를 제공한다. 전부 미수집이면 미확인, 일부 수집이면 부분 합계, 실측 0이면 0으로 표현한다. 진행 중·중단·실패 호출의 누락을 드러내며 빈 호출 범위에서도 미수집 token·시간을 가짜 0으로 채우지 않는다.
- 실제 실행 합계는 현행 summary와 같이 `executed`의 token·`execution_duration_ms`만 사용한다. 거부·미확인 및 legacy 행과 그 기록된 관측값을 숨기지 않되 실제 실행 합계와 구분한다. 기존 `duration_ms`의 호출 준비를 포함한 시도 시간으로 미확인 실행 시간을 채우지 않는다. 각 부분 합계의 대상 범위와 알려진/누락 분모가 기본 출력·JSON·문서에서 일치한다.
- G-194의 일관된 읽기 snapshot, 관측 시각·출처·미확인 의미, 기본 출력/JSON 공통 envelope와 오류·종료 코드·비밀값 제거를 따른다. 실행·수집·invocation 정리·기록 갱신을 하지 않으며 금액 비용·예산·추정 절감은 제공하지 않는다.
- 최신 R-076의 list/status/usage 공통 envelope를 사용하며 usage의 source는 null이다. 도움말·예시에서 통합 status와 새 등록 명령을 사용하고 별도 explain이나 제거된 config list/add/remove를 안내하지 않는다.
- 기존 사용자 문서에 usage 범위와 모든 추가 필드·자료형·단위·nullable 의미·버전 호환, provider 포함 관계·ledger 집계 의미·수집 상태 및 정상·부분·전부 미수집·실측 0·오류 예시를 기록한다.

## Implementation

- 현재 summary와 provider fixture를 재사용해 같은 집계 경계에 optional plan 필터와 시도/실행별 계수·지표별 알려진/미수집 분모를 추가한다. summary의 실행 수 의미를 유지하고 CLI 호환 필드에는 명시적으로 시도 행 수를 연결한다. 현재 summary의 목적·plan/task·target별 결과는 이 CLI의 기본 집계 차원으로 통합하되 미연결 호출을 계획에 배분하지 않는다. 별도의 중복 SQL·provider 집계 규칙을 만들지 않는다.
- G-194의 조회 연결과 결과·출력 경계에 usage를 연결한다. schema 변경이나 새로운 수집 서비스 없이 읽기 가능한 기존 기록의 한계를 표시하며 R-073의 기간·목적·실행 여부 확장은 G-185에서 같은 경계를 확장한다.
- G-184의 저장 필드가 있는 경우에는 이미 기록된 실행 여부·실행 시간을 읽고, 지원되는 legacy schema에 해당 선택 필드가 없으면 미확인으로 처리한다. schema 초기화·migration을 수행하는 Repository 생성자를 조회에서 호출하지 않는다. G-185에는 새로운 범위 수집과 기간·목적·실행 여부 필터를 남기며 이 작업은 저장된 자료만 조회한다.
- 상위 명세는 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를, 자동 역할·검증 책임은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를 따른다.

## Validation

- Worker는 tests/test_agent_invocation_state.py의 summary fixture를 재사용한 targeted unit test로 두 provider, 역할·Model·Effort·session 전이, retry 행 수, plan 필터·미연결 호출·다른 프로젝트 격리, 빈 결과와 지표마다 다른 부분 수집·전부 미수집·실측 0을 검증한다.
- 미종료·중단·실패 호출과 schema 선택 정보 누락에서 token·시간 분모와 경고를 검증하고 기존 summary 소비자의 값·cache 포함 관계가 유지되는지 확인한다.
- 같은 범위에 executed·not_executed·unknown·prepared 및 각 retry를 섞어 CLI `calls`/`retries`의 행 수와 실제 실행 계수를 따로 확인한다. 미확인/거부 행의 token과 준비 시간이 실행 합계에 들어가지 않고 현재 summary의 실행 계수·token·실행 시간과 일치해야 한다. 목적·task별 여러 summary 그룹을 기본 출력 차원으로 합칠 때 중복과 계획 없는 호출 손실이 없어야 한다.
- Manager 전체 verify는 usage의 정상·오류·JSON 경로에서 외부 실행·수집·알림 차단, DB 및 중단 호출 기록 불변, 동시 조회의 일관성, 기본 출력/JSON·문서 계약과 기존 ledger·provider 파싱 회귀를 실제 provider 없이 검증한다.

## Progress

- [x] 2026-10-06 기준 6b717e3의 R-076·R-073, 기존 summary와 provider·invocation 테스트를 대조했다. CLI와 plan 필터·지표별 미수집 분모가 없으며 기존 기본 집계는 재사용한다. 구현·테스트 실행은 하지 않았다.
- [x] 2026-10-11 기준 `5c3cb5ca75abdb597a6c5d063c70276e1e0c263d`와 snapshot token `e295087f0dd5c3bcd450a01cede54cabe9d65ad9cbee9f8bff94a471ac75552d`의 원본 hash를 확인했다. delta의 실행 여부·summary 변경을 R-076·R-073·PRODUCT에 대조해 시도 수 호환과 현재 실행 집계를 구분했다. 구현·테스트·전체 verify는 실행하지 않았다.
- [ ] usage 구현·관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-076은 R-073 전체 구현을 기다리지 않는다. G-184·G-185와 반대 방향의 dependency를 만들지 않고 G-185가 이 조회를 확장하게 한다.
- 호출 행 수와 실제 실행 수는 다른 관측이다. 후속 수집 확대 후에도 기존 calls/retries 의미를 명시적으로 유지한다.
- 2026-10-09: 제공된 snapshot과 원본 hash를 확인하고 확장된 R-076의 공통 출력만 반영한다. complexity medium은 기존 집계에 필터·미수집 분모와 읽기 전용 출력 연결을 추가하는 경계가 명확한 변경이다. risk medium은 조회 범위·부분 합계의 잘못된 표시이며 원본 ledger를 변경하지 않아 출력 교정으로 복구할 수 있다. 구현·테스트 실행 없음.
- 2026-10-11: G-184가 기본 summary의 `calls`/`retries`를 실제 process 시작으로, 시간 합계를 `execution_duration_ms`로 변경했다. R-076이 유지하는 CLI의 기록 행 수와 같은 이름이라고 혼합하지 않으며 공유 집계에서 시도 수와 실제 실행 수를 각각 읽어 출력 계약에 연결한다. 새 실행 관측·목적 수집은 중복 구현하지 않는다. R-071의 source 호환 규칙에 따라 rename 전 source 문자열과 기존 ID·dependency·수행 기록을 보존한다. complexity medium·risk medium·priority normal 및 기존 조회 목표를 유지하며 이번 delta 검토는 프로젝트 최종 전문 확인이 아니다.
