---
id: G-270
title: Planner ignored 오류 복구 조합의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

`tests/test_planning_error_recovery.py`의 `test_ignored_scope_error_restarts_same_input_without_repeating_planner`가 preserved 여부와 일곱 mutation 조건마다 실제 저장소를 준비하는 잔여 비용을 제거한다. 이 테스트는 현재 snapshot도 이미 mock으로 제공한다. Git 관측값으로 정해지는 오류 재평가·해제 정책은 mock 입력과 실제 저장 transaction으로 검사하고 필요한 실제 연결 회귀는 유지한다.

## Acceptance Criteria

- preserved false/true와 ignored·tracked·config·current_tracked·race·ownership·other_error의 기존 14개 조건은 Git 프로세스 없이 실제 `_PlanningAgentPorts`, `PlanningSelectionService`의 오류 gate와 `RuntimeStateRepository`를 실행한다. scope 정규화·오류 해제·결과 완료 판정을 성공값으로 대체하지 않는다.
- 파일 SQLite의 close/reopen을 유지해 저장된 planning 상태와 suspend/resume를 검사한다. ignored-only 해제에서 원본 snapshot·session·retry·planning 결과 완료, 두 번째 해제 no-op과 Planner no-call을 보존한다. 나머지 조건은 원래 오류·저장 상태와 독립 원인을 보존한다.
- 실제 tracked 파일 변화나 설정 변경에 해당하는 snapshot과 시작 규칙 증명 입력을 구별한다. 연속 관측 HEAD 변화, 관측 실패와 예상 밖 Git 명령은 성공으로 삼키지 않으며 해당 실패에서 오류·진단·저장 상태를 보존한다.
- 실제 Git의 ignore source/legacy 증명과 planning workspace·게시·중단 연결은 기존 `test_scope_ignored_artifacts.py` 및 이 모듈의 필요한 통합 회귀에 대응시킨다. 기존 실제 무허가 commit·원격 입력 전진의 effect assertion은 제거하지 않는다.
- 필수 수집·compile·timeout·skip 조건을 유지하고 전환 전후 assertion 대응과 호출 수·영향 모듈 비용을 확인할 수 있다. 이 계획은 planning 오류 처리나 독립 작업 선택 정책을 변경하지 않는다.

## Implementation

기존 `tests/support/planning_case.py`, `tests/support/scope.py`와 파일 SQLite 준비를 재사용해 해당 테스트가 필요한 원문·snapshot·관측 응답만 제공한다. Git/ignore 관측과 현재 HEAD의 변경 입력은 좁은 double로 구성하고 실제 scope 소비자·오류 복구·저장 경계는 그대로 실행한다. 예상 밖 명령과 subprocess 실행을 거부하며 새 runner·공유 cache를 추가하지 않는다.

기존 Git 연결 회귀를 재사용하고 정책 조합을 실제 전체 planning 실행으로 다시 확장하지 않는다. 실제 계약에 필요한 준비·재개까지만 수행하며 후속 교정 소진·게시가 별도 검증 대상인 기존 테스트에 대응됨을 기록한다.

## Validation

- Worker는 변경한 14개 조건 및 관측 실패 case를 검사하는 최소 targeted unit case만 실행한다. 실제 DB 재개·원문 보존·해제/no-op·Planner no-call과 subprocess no-call을 확인한다.
- Manager 전체 verify는 이 모듈의 기존 실제 scope 위반·입력 전진·작업물 보존 회귀와 전환한 정책 조건을 모두 포함한다. Reviewer는 관측만 mock인지, 실제 판정과 transaction이 실행되는지 및 assertion 대응을 확인한다.
- 같은 전체 기준 검증을 사용해 Manager가 영향 모듈의 Git 호출·시간을 비교하고 현재 검증 성공과 비용 자료를 구분한다.

## Progress

- [x] 2026-10-13 기준 `20e6f0fbfdb6ceb5384d42c83f3904490b441356`의 해당 테스트와 오류 gate·support를 정적으로 대조했다. 현재 조합은 저장소 생성·commit·detach·snapshot 수집 후 다시 snapshot을 mock한다.
- [x] 코드·테스트가 같은 저장 verify `64fd60600657b242f833a1b1355aaab0`에서 해당 조합은 PASS이며 Git 316 호출을 기록했다. 전체는 3,631 success·5 skip 및 compile PASS다. 실행하지 않은 새 검증이나 비용 감소를 기록하지 않는다.
- [ ] 정책 조합 전환·assertion 대응과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: legacy scope 증명·preserved 상태·DB 재개와 관측 경쟁을 기존 경계에서 함께 구성해야 한다. risk medium: 오류 해제나 독립 원인 보존 assertion을 약화할 수 있으므로 실제 판정·저장과 전후 대응 및 기존 통합 회귀로 방지하고 테스트 교정으로 복구한다.
- Merge된 catalog/planning 테스트 전환을 소급 계획하지 않는다. 현재 남아 있는 특정 오류 복구 조합만 다루며 catalog의 차단 경로 감사·planning 정책 변경과 구분한다.
