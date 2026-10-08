---
id: G-210
title: Worker 세션 저장값과 복구 판정 경계 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-052] Worker Classification and Initial Level Calibration.md
  - docs/requirements/[R-003] Targeted Worker Unit Tests.md
depends_on:
  - G-214
complexity: medium
risk: medium
priority: normal
---

## Purpose

Worker 세션 저장값의 해석과 실행 복구를 명시적으로 연결하고, 세션 전이 회귀를 독립 정책별로 검증할 수 있게 정리한다. 검증 없는 JSON 필드 채택과 복구 경계 밖의 비구조화 예외를 없앤다.

## Acceptance Criteria

- SessionBinding/SessionTransition의 저장 JSON 객체·필수 필드·문자열·지원 kind·중첩 binding을 검증한 뒤 세션 판정에 사용한다. 기존 nullable 필드의 None, 유효 JSON 형식·직렬화 값·pending target 우선과 binding 없는 과거 세션의 기존 전환·profile 변경/정상 fallback을 유지한다.
- 손상값은 CLI 호출 전에 기존 저장 오류/복구 경계에서 진단하며 session·pending 원문·phase·작업물·실패/승격 감사를 보존한다. 손상 pending을 무시해 과거 session을 실행하거나 임의 새 session·성공·차단 해제를 만들지 않는다. 기존 disposition·재시도/최종 사용자 조치 기준을 변경하지 않는다.
- session 교체·fallback/중단 사례를 기존 Worker phase/공유 fixture 경계로 정리한다. 통합 순서·commit intent·revision artifact·dependency·재개 알림의 사례와 assertion을 누락하지 않는다.
- coordinator 대역이 실제 `latest_base` 인자를 확인하고 dispatch 후에도 recover 1회 계약을 검증한다. 사용자 동작·공개 계약(façade)·저장 형식과 schema·profile 정책을 보존한다.

## Implementation

- `loop_manager_internal/execution/worker_session.py`의 codec 검증과 `execution_worker_handler`의 복구 연결을 분리한다. 기존 오류 판정/진단 경계를 사용하며 저장 format 버전·migration·새 복구 서비스는 추가하지 않는다.
- `tests/test_execution_failure_policy.py`의 서로 다른 책임을 기존 phase별 테스트에 배치하고 통합 사례는 남긴다. G-214의 활성 Worker 준비 계약을 재사용하며 테스트 이동/수집 대응을 기록한다.
- `tests/test_execution_runtime.py`의 FakePorts가 받은 인자를 기록·검증한 뒤 위임하도록 한다. 대역이 복구/상태 전이를 대신 수행하지 않게 한다.
- `test_replacement_start_failure_reuses_pending_target_after_restart`와 `test_unavailable_fallback_intent_survives_failed_new_start`는 `tests/test_execution_worker_phase.py`에서 세션 전이·재시작 책임으로 검증한다. codec 형식은 `tests/test_worker_session.py`, coordinator의 `latest_base` 전달과 단일 recover는 `tests/test_execution_runtime.py`가 소유한다.

## Validation

- Worker targeted unit: `tests.test_worker_session`, `tests.test_execution_runtime` 및 변경한 세션 phase 사례. 잘못된 JSON/type/누락 필드/kind/target, 유효 round-trip, pending 재시작, profile/fallback, 잘못된 전달 인자와 dispatch 뒤 recover 중복을 확인한다.
- Manager 전체 verify: 정리 전후 모든 기존 실패/세션 시나리오 수집, 중단 후 동일 작업 재개·승격 감사·dependency gate·후보 검증/게시 순서를 유지한다. 손상 저장값에서 Agent 미호출과 원문/상태 보존을 확인한다.

## Progress

- [x] 저장된 평가 발견 2·14·15·17의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/execution/worker_session.py:46`, `tests/test_execution_runtime.py:130,150`, `tests/test_execution_failure_policy.py:60`의 저장 해석·대역·책임 혼합 판단을 재사용했다.
- G-214를 먼저 Merge해 준비/cleanup 계약과 세션 사례 재배치의 충돌을 줄인다. 추가 기존 READY prerequisite는 없다. G-207은 설계 설명, G-197은 Reviewer 근거 전달로 범위가 다르다.
- complexity medium은 명확한 codec→기존 복구 경계와 책임별 테스트 연결, risk medium은 해당 계획의 세션 선택/재개 오류이며 저장 원문·작업물 보존과 기존 복구로 대응한다. 손상 상태를 자동 초기화하는 정책은 추가하지 않는다.
