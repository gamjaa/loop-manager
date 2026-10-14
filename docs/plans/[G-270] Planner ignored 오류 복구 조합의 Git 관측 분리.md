---
id: G-270
title: Planner ignored 오류 복구 조합의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

`tests/test_planning_error_recovery.py`의 `test_ignored_scope_error_restarts_same_input_without_repeating_planner`가 preserved 여부와 일곱 mutation 조건마다 실제 저장소를 준비하는 잔여 비용을 제거한다. 현재 R-055 구현은 폐기된 Git 진단을 자동 해소하되 결과 변경 범위 오류와 독립 오류를 유지한다. 이미 mock인 현재 snapshot과 실제 저장 transaction을 재사용하며 이 동작을 새로 구현하지 않는다.

## Acceptance Criteria

- 기존 14개 조건은 Git 프로세스 없이 실제 `_PlanningAgentPorts`, `PlanningSelectionService` 오류 gate와 `RuntimeStateRepository`를 실행한다. 오류 해제·결과 판정·transaction을 성공값으로 대체하지 않는다.
- ignored·config·current_tracked는 폐기된 Git 진단만 해소하고 tracked는 현재 `planner 결과 변경 범위 오류`를 유지한다. race·ownership·other_error도 원래 보류/오류를 유지한다. config 변화나 tracked 후보 차이 자체를 과거 Agent 권한 위반으로 다시 분류하지 않는다.
- 파일 SQLite의 close/reopen·suspend/resume, 원문 snapshot·session·retry, 두 번째 no-op·Planner no-call을 보존한다. 해소 뒤 `planning_result_completed`·`planning_result_pending_validation`은 원래 값을 유지하며 해제만으로 결과 검증 성공을 만들지 않는다. 바뀐 현재 후보는 별도 결과 검증/교정 대상이다.
- 실제 후보·현재 대상의 관측 실패·연속 HEAD 변화와 DB 경쟁을 명시적으로 제공하고 오류·진단·저장 상태를 보존한다. 무관한 shared ref/config 차이는 로컬 후보 경쟁과 구분한다. 예상 밖 Git 명령은 실패한다.
- 실제 ignore 수집·planning workspace·게시·중단 효과는 기존 `test_scope_ignored_artifacts.py`와 이 모듈의 통합 회귀에 대응시킨다. 허용 밖 게시 결과 commit과 원격 입력 전진의 effect assertion을 유지하되 이를 사후 접근 권한 위반으로 설명하지 않는다.
- 필수 수집·compile·timeout·skip 조건을 유지하고 전환 전후 assertion 대응과 호출 수·영향 모듈 비용을 확인할 수 있다. 이 계획은 planning 오류 처리나 독립 작업 선택 정책을 변경하지 않는다.

## Implementation

- [ ] 확인한 `tests/support/planning_case.py`·`scope.py`와 파일 SQLite 준비로 원문·snapshot·현재 대상 및 관측 응답을 구성한다. `planning/agent_ports.py`의 현재 `recover_ignored_scope_error()`와 `planning/selection_service.py`의 gate를 실제로 실행하고 Git 관측·workspace 확인 입력만 좁은 double로 제공한다.
- [ ] 현재 14개 조건의 해소/유지 결과와 완료 marker 불변을 그대로 검사한다. 전환 전후 assertion을 대응시키고 예상 밖 subprocess를 거부하며 운영 복구 정책·runner·cache를 변경하지 않는다.
- [ ] 같은 모듈의 결과 범위 오류·원격 입력 전진·게시 보존 회귀와 실제 Git 효과의 대응을 남긴다. 정책 조합을 전체 planning 실행으로 반복하지 않고 G-278의 실제 실행 자리 보완과 분리한다.

## Validation

- Worker는 해당 14개 조건과 변경한 관측 실패 case의 최소 targeted unit만 실행한다. 실제 DB 재개·원문/session/retry 보존·해제/no-op·완료 marker 불변·Planner/subprocess no-call을 확인한다.
- 부분/전체 검증의 회귀 조건은 현재 결과 오류·입력 전진·작업물 보존·실제 transaction과 필수 수집의 유지다. readonly/legacy 권한 귀속을 복원하거나 mock이 판단 자체를 대신하면 실패해야 한다.
- 이번 직접 구현 뒤의 같은 코드·수집·계측에서 영향 모듈 호출·시간을 비교한다. 아래 과거 PASS/316회는 현재 코드의 전환 전 기준이나 새 성공으로 사용하지 않는다.

## Progress

- [x] 2026-10-13 기준 `20e6f0fbfdb6ceb5384d42c83f3904490b441356`의 해당 테스트와 오류 gate·support를 정적으로 대조했다. 현재 조합은 저장소 생성·commit·detach·snapshot 수집 후 다시 snapshot을 mock한다.
- [x] 코드·테스트가 같은 저장 verify `64fd60600657b242f833a1b1355aaab0`에서 해당 조합은 PASS이며 Git 316 호출을 기록했다. 전체는 3,631 success·5 skip 및 compile PASS다. 실행하지 않은 새 검증이나 비용 감소를 기록하지 않는다.
- [ ] 정책 조합 전환·assertion 대응과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 이번 delta에서 자동 해소의 의미는 ignored-only 일치 증명에서 폐기된 Git 진단 해소로 바뀌었다. 실제 결과 범위 오류·현재 대상/관측 경쟁·독립 오류와 미확인 결과 marker는 유지한다. 변경한 운영 코드를 소급 구현하지 않고 테스트 준비 비용만 전환한다. 기존 Progress의 조사·실측 원문은 역사 감사로 보존한다.
- complexity medium: preserved 상태·파일 DB 재개·현재 후보 관측 경쟁과 폐기/현재 오류 분류를 기존 경계에서 함께 구성한다. risk medium: 결과 오류·독립 원인이나 완료 marker 보존을 놓칠 수 있으므로 실제 판정·저장과 assertion 대응·기존 통합 회귀로 확인하고 테스트 교정으로 복구한다.
- Merge된 catalog/planning 테스트 전환을 소급 계획하지 않는다. 현재 남아 있는 특정 오류 복구 조합만 다루며 catalog의 차단 경로 감사·planning 정책 변경과 구분한다.
