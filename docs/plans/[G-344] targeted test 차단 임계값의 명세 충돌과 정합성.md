---
id: G-344
title: targeted test 차단 임계값의 명세 충돌과 정합성
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on: []
complexity: low
risk: medium
priority: high
draft: true
---

## Purpose

동일 targeted test의 연속 실패 차단 기준이 활성 명세에서 3회와 5회로 갈리는 범위를 사용자 판단 대기로 보존한다. 현재 구현과 회귀는 5회를 사용한다. 이를 새 구현의 성공이나 R-091의 충족으로 확정하지 않으며, 사용자 결정·명세 정합화 뒤 실제로 남는 구현·검증만 수행한다. 독립적인 rebase·pause·배정 개선은 이 임계값 선택과 분리한다.

## Acceptance Criteria

- 사용자 출처가 확인된 결정과 일치하는 PRODUCT·R-055·R-091에서 적용할 하나의 임계값을 확인한다. Planner나 Worker가 명세의 숫자를 선택·수정하거나 현재 코드·문서의 수정 시점으로 우선순위를 만들지 않는다.
- 5회 유지가 결정되면 현재 저장·차단·회귀와 대조해 실제 미충족 구현이 있는지만 판단한다. R-091 정정만 남고 구현이 이미 충족하면 소급 구현·불필요한 테스트를 만들지 않으며, 이 draft의 안전한 철회·후속 관계 반영은 Manager가 담당한다.
- 3회 변경이 결정되면 사용자 명세가 정합해진 뒤 같은 테스트의 세 번째 고유한 실제 실패에서만 도달 표식을 만들고 기존 Worker/verify 진입 gate가 소비하도록 필요한 국소 변경을 한다. 기존 저장 이력도 확인된 실패 수와 결정에 맞게 처리하되 결과를 재생·중복 집계하거나 session·실패 원문을 초기화하지 않는다. 미완료 수집·귀속 불명은 실제 실패로 추가하지 않는다.
- 어느 선택에서도 다른 테스트 비합산·동일 실행 중복 비소비·성공/skip/expected failure 초기화·fixture/불명확 결과 보존과 작업별 격리를 유지한다. 세 물리 CLI 호출·session fallback과 호출별 결과/귀속 assertion은 유지하며 임계값에 해당하는 도달 기대만 결정에 맞춘다. 전체 verify·Review·문서 오류의 별도 집계를 바꾸지 않는다.

## Implementation

- [ ] Manager가 검증한 사용자 결정과 최신 source를 대조해 실제 잔여 구현을 확정한다. 5회 유지로 구현 부족이 없으면 Worker를 시작하지 않고 Manager에 그 근거를 반환한다. PRODUCT·Requirement 정정은 이 작업의 Worker 수정 범위가 아니다.
- [ ] 구현 변경이 필요한 경우 확인한 `loop_manager_internal/storage/targeted_test_state.py:TargetedTestRepository.observe()`의 실패 누계·도달 표식과 기존 저장 호환 경계만 보완한다. 별도 설정·범용 정책 엔진·새 DB 원본을 만들지 않는다. 이미 저장된 누계/표식의 처리도 실제 결과·기존 소비 감사와 연결하고 파괴적 초기화나 원문 삭제로 전환하지 않는다.
- [ ] 확인한 `loop_manager_internal/execution/execution_worker_handler.py:_targeted_test_block()`과 `loop_manager_internal/execution/execution_verify_handler.py:_targeted_test_gate()`의 기존 gate를 재사용한다. G-236의 pause 보존, G-297의 호출 배정, G-313의 Git/게시 복구와 G-325의 지속 이력은 재구현하지 않는다.
- [ ] 확인한 `tests/test_targeted_test_streak.py`의 저장·중복·초기화·재시작·Worker 진입 회귀와 `tests/test_agent_invocation.py:test_targeted_records_follow_each_physical_retry_and_session_fallback`을 결정에 맞춰 최소 수정한다. G-339의 Git fixture 교정과 세 실행의 결과·귀속 검증은 현재 원격 구현으로 재사용한다.

## Validation

- 구현 변경이 있을 때의 Worker targeted unit은 `tests.test_targeted_test_streak.TargetedHistoryTests`의 변경한 경계와 위 `RunnerTests`의 물리 재시도 case다. 실제 SQLite와 기존 관측 double로 임계값 직전·도달·후속 성공, 재시작과 같은 실행 재관측을 확인한다. 세 물리 호출 자체를 늘려 새 임계값에 맞추지 않는다.
- 저장된 임계값 전후 누계와 소비된/미소비 표식의 호환 처리를 실제 저장 결과로 확인한다. 새 실패 없이 임의 초기화·중복 집계하거나 다른 계획의 이력을 바꾸면 실패해야 한다. 필요한 Worker/verify gate 연결은 기존 `WorkerTargetedFailureTests`·`tests/test_execution_verify_phase.py`의 회귀를 재사용하고 전체 lifecycle을 반복하지 않는다.
- 명세 정정만으로 현재 구현이 충족되면 새로운 테스트 실행이나 성공 기록을 만들지 않는다. 사용자 결정·source 정합화·구현 충족과 영향 작업의 실제 검증/차단 해제는 각각 구분한다.

## Progress

- [x] revision `42b79e867779a261371e27b47b100f795408f6fe`에서 PRODUCT의 5회, R-055의 5회, R-091 본문의 3회와 저장 구현 `failures >= 5`·기존 회귀를 정적으로 대조했다. 사용자 판단을 요청했으며 구현·테스트·verify는 실행하지 않았다.
- [ ] 사용자 결정·source 정합화와 실제 미충족 구현의 유무 확인
- [ ] 필요한 경우에만 국소 구현·저장 호환·관련 회귀 확인

## Decision Log

- 보고 369의 세 물리 실패/도달 assertion은 현재 G-339 교정으로 구현돼 있다. 이번 draft는 그 교정을 되돌리거나 재수행하는 작업이 아니라 서로 다른 활성 명세가 요구하는 임계값을 아직 선택할 수 없다는 판단을 보존한다. 질문 해소·draft 전환·철회·실행 상태는 Manager가 검증한다.
- complexity low: 사용자 결정 뒤 필요한 기술 변경은 기존 실패 누계와 gate·회귀의 국소 정합화다. 정책 모호함을 높은 effort로 대신 결정하지 않는다. risk medium: 임계값 변경 시 저장된 누계와 표식을 잘못 처리하면 작업을 일찍 차단하거나 필요한 차단을 놓칠 수 있으므로 원문/소비 감사 보존·재시작·경계 회귀가 필요하다. priority high는 자동 차단의 실제 기준과 관련 검증 판정을 명확히 하기 위한 것이다.

## Draft Decision

Question: 동일 targeted test의 실제 연속 실패 차단 임계값을 R-055·PRODUCT·현재 구현의 5회로 유지할 것인가, R-091 본문에 적힌 3회로 변경할 것인가?
Facts: R-055는 “같은 테스트의 실제 연속 5회 실패로 해당 작업을 차단한다”라고 정하고 PRODUCT도 5회를 보장한다. R-091은 “실제 동일 테스트 연속 3회 실패 등 기존 최종 차단 조건은 유지한다”라고 명시한다. 현재 `targeted_test_state.py`와 `TargetedHistoryTests`는 5회를 사용하며, 기존 catalog에는 이 충돌을 다루는 비종료 계획이 없다. 현재 구현·문서 수정 시점은 사용자 선택의 근거가 아니다.
Options: A. 5회를 유지하고 사용자 명세 R-091의 숫자를 정합화한다. 현재 구현이 모두 충족하면 이 draft에는 실행할 구현이 남지 않는다. B. 3회로 변경하도록 PRODUCT·R-055를 정합화하고 실제 남는 저장/차단·회귀를 이 계획에서 구현한다.
Impact: A는 기존 자동 차단 동작을 유지하며 문서 정합화가 필요하다. B는 세 번째 실패부터 차단되도록 사용자 동작과 기존 저장 이력의 해석이 바뀌므로 해당 명세 결정·호환 처리·회귀가 필요하다. 답변 전에는 임계값을 바꾸거나 어느 숫자를 최종 충족 기준으로 확정하지 않는다. 독립적인 rebase·pause 작업은 기존 gate와 기록을 보존하며 진행할 수 있다.
