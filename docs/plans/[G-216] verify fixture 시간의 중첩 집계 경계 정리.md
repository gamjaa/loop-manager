---
id: G-216
title: verify fixture 시간의 중첩 집계 경계 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on: []
complexity: medium
risk: low
priority: normal
---

## Purpose

verify comparison의 fixture 비용 집계에 포함 관계를 명시해 상위 구간과 하위 단계의 시간을 중복 합산하지 않게 한다. 측정 이벤트와 비용 비교의 책임을 분리하고 기존 진단 형식을 유지한다.

## Acceptance Criteria

- `git_fixture_seconds`는 boundary_save/restore 안의 copy/confirmation 등 포함된 하위 시간을 상위 구간과 다시 더하지 않는다. 포함 관계를 확인할 수 있는 집계 경계와 비중복 총계의 의미가 명시된다.
- 원래 개별 phase event·duration·진단 schema를 보존하고 전체 경과/개별 누적/fixture 비용을 구분한다. 서로 다른 테스트/프로세스의 독립 구간을 잘못 하나로 합치거나 자료 없는 시간을 생성하지 않는다.
- 사용자 동작(verify 결과·명령·timeout·자원 예약)·공개 계약(comparison key/type)·저장 형식(진단 자료)·성공 판단을 보존한다. 과거 진단을 재작성하거나 실측하지 않은 성능 향상을 주장하지 않는다.

## Implementation

- `loop_manager_internal/verification/timing.py`의 comparison에서 기존 phase identity와 측정 경계를 재사용해 상위 구간/포함 관계를 집계 책임으로 표현한다. 필요한 경우 기존 계측 caller의 구간 표현만 정리하고 범용 tracing infrastructure를 만들지 않는다.
- `tests/support/recovery_boundary.py`의 실제 중첩 형태를 회귀 입력으로 사용한다. 이벤트 수집과 시간 집계를 검증 실행/성공 결과 재사용에 결합하지 않는다.

## Validation

- Worker targeted unit: `tests.test_verify_runtime`의 comparison 경계에 중첩 save/restore·copy/confirmation, 형제/독립 구간·별도 테스트/프로세스·불완전 진단 사례를 추가한다. 숫자 총계뿐 아니라 원래 event 보존과 집계 의미를 확인한다.
- Manager 전체 verify: timing/fixture 진단·복구 경계 digest·직렬/병렬 동일 검증 범위 및 timeout/중단 기록을 유지한다. Reviewer는 중첩 제거가 측정 비용의 누락/임의 절감으로 바뀌지 않았는지 확인한다.

## Progress

- [x] 저장된 평가 발견 8의 revision·원본 hash·인용을 확인했다. 구현·테스트·실측은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/verification/timing.py:250`의 prefix 전체 합산과 저장된 boundary 중첩 판단은 ARCHITECTURE의 하위 단계 비중복 집계 계약과 어긋난다.
- G-187은 새 공통 runner/두 언어의 실제 OS·부하 비교 자료이며 현재 중첩 계측의 집계 책임을 재구성하는 작업을 명시하지 않는다. 현재 자체 verify의 국소 집계 수정 후 G-187이 이를 재사용하며 추가 기존 READY prerequisite는 없다.
- complexity medium은 중첩/독립 구간의 명확한 집계 경계 연결, risk low는 실행/성공에 영향을 주지 않는 진단 총계 오류이며 원래 event 보존과 집계 코드 수정으로 복구할 수 있다.
