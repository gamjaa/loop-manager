---
id: G-195
title: 저장된 복구와 후보 근거의 explain CLI
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] Read-Only Operational State CLI.md
depends_on:
  - G-194
complexity: medium
risk: medium
priority: normal
---

## Purpose

`explain <repository> --plan <id> [--json]`으로 특정 작업의 저장된 전환·복구 결정과 현재 후보에 결합된 검증 근거를 설명한다. G-194의 조회 경계를 재사용하고, 상태를 바꾸거나 없는 이력·현재 후보의 통과를 추정하지 않는 것이 완료 조건이다.

## Acceptance Criteria

- repository와 plan ID로 종료 작업을 포함한 특정 작업 하나를 조회하며 G-194의 상태·범위·기본 출력/JSON·오류 계약을 따른다. status와 공통 사실·사유·재개 조건이 일치한다.
- 저장된 마지막 단계 전환의 이전·이후 단계·시각·사유를 근거가 있는 범위에서 보여준다. 이력이 없으면 현재 상태로 전환 이력을 만들지 않고 미확인과 이유를 표시한다. 실제 진전·관측 시각·신선도도 G-194의 근거 규칙을 따른다.
- 복구 disposition·pending action·원인 code·scope, retry와 반복 횟수, 최종 차단 근거·남은 불확실성·확인된 사용자 대상 및 대기 조건을 보여준다. 저장된 판단과 다음 실행의 재판정을 구분하며 사용자 행동이나 자동 재개 가능성을 추측하지 않는다.
- 현재 후보 commit·plan revision·검증된 base와 verify·Review 결과의 기존 ID·지문·근거 token·checkpoint를 관련 입력과 함께 보여준다. 다른 후보나 입력의 성공은 현재 통과로 표시하지 않고 불일치·누락을 드러낸다. 근거 식별자와 인증 비밀값을 구분하며 prompt·코드·문서 전문은 출력하지 않는다.
- 모든 정상·오류·JSON 경로가 G-194의 읽기 전용 snapshot과 비밀값 제거를 유지한다. 조회는 저장·복구·재시도·검증·게시·알림을 실행하지 않는다. 기존 사용자 문서에 explain 범위, 모든 추가 필드·자료형·단위·nullable 의미·버전 호환과 정상·미확인·오류 예시를 기록한다.

## Implementation

- 저장된 실행 checkpoint·검증 피드백·복구 상태의 책임 경계에서 설명에 필요한 사실만 읽고 G-194의 공통 상태 투영을 확장한다. 전환 이력이나 증명 체계를 새로 저장하지 않는다.
- 기존 후보/입력 identity와 순수 복구 판단을 재사용해 결과 연결을 검증하고, 읽기만으로 확정할 수 없는 원격·소유권 조건은 재검사 필요로 남긴다. 정상 실행의 정책이나 상태 전이를 바꾸지 않는다.
- CLI parser와 문서·출력 계약을 연결한다. 상위 문서와 검증 주체는 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 따른다.

## Validation

- Worker는 기존 execution checkpoint·validation feedback·복구 fixture의 targeted unit test로 전환 기록 유무, 복수 원인·scope·retry, 사용자 대상 확인/부재, 후보·plan revision·base 일치/불일치와 누락된 성공 근거를 검증한다. 저장 시각을 진전으로 오인하는 사례와 진단 비밀값 제거도 확인한다.
- Manager 전체 verify는 같은 fixture의 status/explain 사실 일치, 기본 출력/JSON 의미·문서 계약, 종료 작업·조회 오류 및 선택 정보 누락, 정상·오류 경로의 외부 효과 차단과 DB 기록 불변을 실제 provider 없이 검증한다. 기존 실행·복구 판정은 유지돼야 한다.

## Progress

- [x] 2026-10-06 기준 6b717e3의 R-076, 구조화 복구·실행 checkpoint·검증 피드백 원본과 관련 테스트를 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] explain 구현·관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- G-194 이후 저장된 근거를 해석하는 책임으로 한정한다. 후보가 바뀐 성공 결과를 현재 통과로 오인하는 위험을 targeted case로 검증한다.
- 상태·사용량 수집 확대나 새 전환 이력 저장은 R-076의 범위가 아니며 이 계획의 선행 조건도 아니다.
