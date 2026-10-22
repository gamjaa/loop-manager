---
id: G-290
title: targeted test의 native 실패와 fixture 결과 구분
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

직접 구현된 targeted runner와 테스트별 연속 실패 저장을 유지하면서 native 결과의 분류를 바로잡는다. 현재 `TargetedTestRepository.observe()`는 `unexpected_success`를 실패 이력의 초기화로 처리하고 기존 테스트도 이를 요구한다. `TimedResult`는 개별 `setUp`·`tearDown` 오류를 테스트 본문의 오류와 같은 `test_end: error`로 남기며 targeted runner는 그 차이를 전달하지 않는다. JUnit 경로도 testcase의 error/failure tag만으로 분류한다. 이 때문에 실제 실패를 초기화하거나 fixture 오류를 세 번째 테스트 실패로 오인할 수 있다.

## Acceptance Criteria

- R-055/PRODUCT: 동일 테스트의 native 실패만 실제 실행별로 누적한다. unittest의 unexpected success는 native 실패 의미를 유지하며 성공·skip·expected failure만 명시된 초기화 결과로 사용한다. 다른 테스트·후보·commit·세션 변화는 해당 테스트의 연속성에 영향을 주지 않는다.
- ARCHITECTURE의 검증 경계: 수집·module/class 및 개별 테스트 fixture 오류, 실행 중단과 identity 불명은 개별 테스트 실패를 합성하거나 기존 실패 이력을 초기화하지 않는다. 테스트 본문의 확인된 assertion failure/error와 fixture 실패를 구분할 수 있다. 완료한 개별 결과와 실행·수집 오류의 감사는 함께 보존한다.
- unittest subTest는 현재 계약대로 상위 `test.id()`의 최종 결과로 집계한다. 파라미터별 새 차단 정책이나 별도 ID 체계를 도입하지 않는다. JUnit의 안정적인 classname·name 및 파라미터 identity, 새 XML 경로·중복/상충 거부를 유지한다. native 근거로 fixture·테스트 실패를 구별할 수 없는 결과는 추측해 세 번째 실패로 확정하지 않는다.
- 기존 결과의 중복 방지·관측 transaction, 세 번째 실패 도달 표식과 그 뒤 성공의 구분, 정상 Worker 경계에서 차단/소비의 원자성을 유지한다. 과거 저장 결과를 새 분류로 소급 변조하거나 운영 DB를 교정하지 않는다. 문서 보고·전체 verify·Review와 독립 안전/사용자 판단 정책은 유지한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/verification/targeted.py`의 unittest/JUnit 기록과 `verification/timing.py:TimedResult`의 native callback을 대조한다. 기존 reporter·fixture 관측을 재사용해 targeted 집계에 필요한 발생 경계를 전달한다. shared reporter를 바꾸면 전체 verify의 결과·수집·fixture 의미를 보존하며 필요하지 않은 범용 reporter나 새 adapter를 만들지 않는다.
- [ ] 확인한 `loop_manager_internal/storage/targeted_test_state.py`에서 `unexpected_success` 처리와 fixture/불확실 결과의 집계 제외를 명시한다. 기존 결과·streak 구조를 호환되게 확장하는 최소 방식을 선택하고 실행 ID·테스트 ID 중복/상충 검사를 약화하지 않는다. 비호환 상태 변경이 필요하면 구현하지 말고 Architecture Change Policy의 판단에 남긴다.
- [ ] `targeted.py:junit_results()`가 받은 native 원본의 결과·오류 근거를 보존한다. 자유 형식 메시지나 명령 종료 코드만으로 개별 테스트 실패를 만들지 않는다. 지원하는 결과에서 필요한 구분과 불확실한 경우를 명시하고 새 언어/도구 지원이나 전체 verify 연결을 이 작업에 추가하지 않는다.
- [ ] 확인한 `tests/test_targeted_test_streak.py`의 잘못된 unexpected-success 초기화 기대를 교정하고 native unittest fixture·subTest·JUnit 경계 회귀를 추가한다. `tests/test_execution_worker_phase.py`와 기존 `tests/test_verify_runtime.py`·`tests/test_verify_contract.py`에서 shared reporter 변경의 관련 assertion을 재사용한다. PRODUCT·ARCHITECTURE·`docs/AGENTS.worker.md`는 실제로 보완한 결과 구분만 정합화하며 사용자 Requirement는 수정하지 않는다.

## Validation

- Worker는 새 분류를 확인하는 `tests.test_targeted_test_streak`의 최소 case/class만 실행한다. 실제 unittest callback 의미는 임시 테스트 파일과 프로젝트 Python의 좁은 실행으로 확인하고, 조건별 집계·차단은 기존 SQLite repository와 ExecutionFixture를 사용한다. Agent CLI·실제 Git은 호출하지 않는다.
- unexpected success 3회는 같은 테스트의 세 번째 native 실패로 차단해야 한다. failure→success/skip/expected failure→failure→failure는 차단하지 않으며 서로 다른 테스트의 세 실패도 합산하지 않아야 한다.
- module/class fixture 및 개별 setUp/tearDown 오류, import/수집 실패, 본문 assertion/error와 subTest 실패를 구분한다. fixture/수집/중단만으로 횟수를 늘리거나 이력을 지우지 않고 확인된 상위 테스트 결과는 실행별 한 번만 집계해야 한다. 본문의 확인된 실패 뒤 teardown도 실패한 경우에는 본문 실패 한 번을 잃거나 두 번 세지 않아야 한다.
- JUnit의 파라미터별 ID, 확인된 테스트 실패, fixture/수집 오류 및 구분 불명·중복·상충 결과를 검사한다. 오류 실행을 성공·skip으로 숨기지 않으며 명령 종료 코드만으로 testcase 실패를 만들지 않아야 한다.
- shared reporter 변경 시 기존 전체 verify의 fixture skip·expected failure·unexpected success·subTest 결과 의미를 유지하는 관련 unit만 추가 확인한다. 기존 원자 관측과 세 번째 실패 후 같은 turn의 성공·중복 재관측·차단 소비 회귀도 유지한다.

## Progress

- [x] `5f0dc1c2af8027aed3bf13501f3d5c223f375215`부터 `5c133fbaddf281fef15d555f926b9aa81aa1aee9`까지의 변경과 R-055·관련 제품/설계 절, runner·reporter·저장 및 기존 회귀 원본을 정적으로 대조했다. 직접 구현된 집계 전체를 재계획하지 않았다.
- [ ] native 결과 구분과 호환 집계 교정
- [ ] 최소 targeted unit 회귀와 관련 문서 정합성

## Decision Log

- 기존 비종료 G-189는 후속 소비자까지의 전수 감사이며 이 native 분류 교정은 별도 Merge 가능한 구현이다. G-291의 호출 기록 복구, G-292의 Claude 연결과 책임을 나눈다. 기존 테스트가 현재 동작을 요구한다는 사실을 R-055 충족 근거로 사용하지 않는다.
- complexity medium: reporter의 발생 경계와 저장 결과 의미를 함께 맞추는 작업이다. risk medium: 잘못된 분류가 한 작업을 부당하게 차단하거나 실패 한도를 놓치지만 원본·저장 감사와 기존 실행 gate를 보존해 재평가할 수 있다. 불확실 결과를 실패로 합성하지 않는 회귀로 완화한다. priority high는 새 무인 차단 정책의 정확성을 위한 필수 교정이다.
- 선택 verify는 `unittest` version 1·launcher Python 3.11–3.14와 독립 모듈 수집·필수 compile이다. 이 작업은 adapter·전체 검증 범위를 바꾸지 않는다. 현재 Planner는 구현·테스트·verify를 실행하지 않았으며 직접 구현의 성공 기록을 소급 생성하지 않는다.

## Review 보완 결과 (2026-10-22)

- Review에서 확인한 교차 클래스 귀속 오류는 `collect()`가 결과별 ID 대신 마지막으로 읽은 ID를 fixture 영향 판정에 재사용한 것이었다. 판정 입력을 각 `test_end` 이벤트 ID로 바꾸고, 한 모듈에 클래스 fixture 오류가 있는 성공과 무관한 클래스의 성공을 함께 실행하는 회귀를 추가했다. 두 테스트 ID에 각각 두 번의 기존 실패를 시드해 fixture 대상의 이력이 보존되고 무관한 성공만 초기화하는지 확인한다.
- 개별 setup/teardown의 assertion은 `addFailure()`로 전달되므로 callback 경계에서 fixture의 scope와 원래 exception identity를 함께 보존한다. 대응하는 callback 결과만 `fixture_error`로 기록하고, 앞선 본문 assertion과 뒤이은 teardown assertion이 함께 발생하면 본문 `failure`와 fixture 오류를 각각 한 번 남긴다. RuntimeError fixture 회귀도 유지한다.
- 새 회귀를 추가한 뒤 `TargetedRunnerTests` 실행은 위 두 누락 조건에서 실패했다. 수정 후 동일 명령으로 10개 테스트가 통과했으며, 마지막으로 두 회귀 case만 지정한 실행에서도 2개가 통과했다. `/usr/bin/python3` 3.12.3과 현재 후보의 `targeted.py`를 `LOOP_MANAGER_TARGETED_TEST_RUNNER`에 지정해 실행했다. 전체 `scripts/verify`는 실행하지 않았다.

  FAILED_COMMAND: export LOOP_MANAGER_TARGETED_TEST_RUNNER=/home/ubuntu/.local/state/loop-manager/worktrees/e1b66bcef2ec3ace/G-290/loop_manager_internal/verification/targeted.py; export LOOP_MANAGER_TARGETED_TEST_DIRECTORY=/tmp/loop-manager-targeted-g290-review-q9l8v7tf; /usr/bin/python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_targeted_test_streak.TargetedRunnerTests

  RERUN: export LOOP_MANAGER_TARGETED_TEST_RUNNER=/home/ubuntu/.local/state/loop-manager/worktrees/e1b66bcef2ec3ace/G-290/loop_manager_internal/verification/targeted.py; export LOOP_MANAGER_TARGETED_TEST_DIRECTORY=/tmp/loop-manager-targeted-g290-review-q9l8v7tf; /usr/bin/python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_targeted_test_streak.TargetedRunnerTests

  재실행 결과: 10개 테스트 통과. 이어서 두 새 회귀 case만 지정한 targeted 실행은 2개 테스트 통과했다.
