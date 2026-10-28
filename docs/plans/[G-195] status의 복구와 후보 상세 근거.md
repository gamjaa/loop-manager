---
id: G-195
title: status의 복구와 후보 상세 근거
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
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
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: medium
risk: medium
priority: normal
---

## Purpose

`status <repository> <id> [--json]`에서 특정 작업의 저장된 전환·복구 결정과 현재 후보에 결합된 검증 근거를 설명한다. G-194의 통합 status와 읽기 경계를 확장하며 상태를 바꾸거나 없는 이력·현재 후보의 통과를 추정하지 않는 것이 완료 조건이다. 별도 explain 명령은 만들지 않는다.

## Acceptance Criteria

- repository와 위치 인자 plan ID로 종료 작업을 포함한 특정 작업 하나를 조회하며 G-194의 상태·범위·기본 출력/JSON·오류 계약을 따른다. list 요약과 status 상세의 공통 사실·사유·재개 조건이 일치하며 상세를 요약에 반복하지 않는다.
- 저장된 마지막 단계 전환의 이전·이후 단계·시각·사유를 근거가 있는 범위에서 보여준다. 이력이 없으면 현재 상태로 전환 이력을 만들지 않고 미확인과 이유를 표시한다. 실제 진전·관측 시각·신선도도 G-194의 근거 규칙을 따른다.
- 복구 disposition·pending action·원인 code·scope, retry와 반복 횟수, 최종 차단 근거·남은 불확실성·확인된 사용자 대상 및 대기 조건을 보여준다. 저장된 판단과 다음 실행의 재판정을 구분하며 사용자 행동이나 자동 재개 가능성을 추측하지 않는다.
- R-055 B06: 원인 소유 작업과 영향받는 작업을 구분하고 이 작업의 operation/intent·충돌 자원·planning/복구 대기 관계·남은 단계와 다음 조건을 저장 근거로 연결한다. 다른 작업의 독립 intent·정체를 이 후보의 실패나 프로젝트 전체 차단으로 표시하지 않는다. 작업 ID 없는 프로젝트 사건은 G-194의 프로젝트 status에 연결하며 누락된 관계는 미확인이다.
- 현재 후보 commit·plan revision·검증된 base와 verify·Review 결과의 기존 ID·지문·근거 token·checkpoint를 관련 입력과 함께 보여준다. 다른 후보나 입력의 성공은 현재 통과로 표시하지 않고 불일치·누락을 드러낸다. 근거 식별자와 인증 비밀값을 구분하며 prompt·코드·문서 전문은 출력하지 않는다.
- 모든 정상·오류·JSON 경로가 G-194의 읽기 전용 snapshot과 비밀값 제거를 유지한다. 조회는 저장·복구·재시도·검증·게시·알림을 실행하지 않는다. 기존 사용자 문서에 status 상세 범위, 모든 추가 필드·자료형·단위·nullable 의미·버전 호환과 정상·미확인·오류 예시를 기록하고 별도 explain을 안내하지 않는다.

## Implementation

- 저장된 실행 checkpoint·검증 피드백·복구 상태의 책임 경계에서 설명에 필요한 사실만 읽고 G-194의 공통 상태 투영을 확장한다. 전환 이력이나 증명 체계를 새로 저장하지 않는다.
- 기존 후보/입력 identity와 순수 복구 판단을 재사용해 결과 연결을 검증하고, 읽기만으로 확정할 수 없는 원격·소유권 조건은 재검사 필요로 남긴다. 정상 실행의 정책이나 상태 전이를 바꾸지 않는다.
- [ ] 확인한 `storage/recovery_state.py`·`storage/execution_checkpoint_state.py`와 G-194가 읽는 구조화 원인 근거를 작업/operation identity로 연결한다. 현재 로그 text를 새 진단으로 파싱하거나 journal·Git을 새로 읽어 복구를 판정하지 않는다. `tests/test_recovery_state.py`·`tests/test_scope_block_revalidation.py`의 보존 근거를 조회 fixture에 사용해 복수 원인·legacy 부재·독립 해소의 출력/JSON을 검사한다.
- CLI parser와 문서·출력 계약을 연결한다. 상위 문서는 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를, 자동 역할·검증 책임은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를 따른다.

## Validation

- 원인 작업 A와 영향 작업 B, 별도 프로젝트 publication 사건을 같은 읽기 snapshot에 두고 operation/intent·자원·상호 대기와 남은 조건을 실제 저장값대로 보여주는지 확인한다. 독립 사건 해소를 B의 후보 성공/전역 해소로 표시하지 않고 legacy 관계 부재는 미확인이다. Git/복구/정리 효과 0회와 DB·outbox·이력 불변을 G-194의 조회 fixture에서 검사한다.
- Worker는 기존 execution checkpoint·validation feedback·복구 fixture의 targeted unit test로 전환 기록 유무, 복수 원인·scope·retry, 사용자 대상 확인/부재, 후보·plan revision·base 일치/불일치와 누락된 성공 근거를 검증한다. 저장 시각을 진전으로 오인하는 사례와 진단 비밀값 제거도 확인한다.
- Manager 전체 verify는 같은 fixture의 list/status 사실 일치, 기본 출력/JSON 의미·문서 계약, 종료 작업·조회 오류 및 선택 정보 누락, 정상·오류 경로의 외부 효과 차단과 DB 기록 불변을 실제 provider 없이 검증한다. 기존 실행·복구 판정은 유지돼야 한다.

## Progress

- [x] 2026-10-06 기준 6b717e3의 R-076, 구조화 복구·실행 checkpoint·검증 피드백 원본과 관련 테스트를 확인했다. 구현·테스트 실행은 하지 않았다.
- [x] 2026-10-09 최신 R-076이 상세를 status로 통합함을 확인했다. 제공된 미착수 snapshot에서 같은 ID로 title/path를 개정하며 기존 근거 조회 목표·이전 확인 기록은 보존한다. 구현·테스트 실행 없음.
- [ ] status 상세 근거 구현·관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-055의 확장된 진행 장벽 진단은 기존 작업별 status 상세에 통합한다. 원인 근거 생산·프로젝트 투영은 G-325→G-194를 소비하며 새 저장 이력이나 실행 권한을 만들지 않는다. 기존 후보 성공의 정확한 연결·무변경 조회·미확인 의미와 기록을 유지한다.

- G-194 이후 저장된 근거를 해석하는 책임으로 한정한다. 후보가 바뀐 성공 결과를 현재 통과로 오인하는 위험을 targeted case로 검증한다.
- 상태·사용량 수집 확대나 새 전환 이력 저장은 R-076의 범위가 아니며 이 계획의 선행 조건도 아니다.
- complexity medium: 기존 checkpoint·복구·결과 identity의 확인 가능한 저장 사실을 통합 status에 연결한다. risk medium: 다른 후보 성공이나 저장 시각을 잘못 표시하는 국소 조회 오류이며 원본을 보존해 출력 교정으로 복구할 수 있다. 새 검증 성공이나 상태 전이를 만들지 않는다.
