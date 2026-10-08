---
id: G-213
title: rebase 교정의 다음 저장 상태 가독성 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-003] Targeted Worker Unit Tests.md
depends_on: []
complexity: low
risk: medium
priority: normal
---

## Purpose

rebase 교정 저장소의 반복된 UPDATE 조건을 필드별 다음 상태로 표현해 저장 책임을 명확히 하고, 관련 관측 fixture가 빈 충돌/파일을 정확히 나타내게 한다. 감사 보존과 실패 결과 재생 금지의 차이를 검토 가능한 경계로 유지한다.

## Acceptance Criteria

- `observe`의 pending_turn·worker_result·worker_code·result_kind·scope_marker 각각의 보존/초기화 조건이 의미 있는 지역 값으로 드러난다. 조건이 다른 필드를 하나의 공통 분기로 합치지 않는다.
- 동일 입력·fingerprint 재발·corrected·새 Worker 결과·persist_worker_result 조합, generation CAS와 pending turn 재개가 기존과 같은 상태를 저장한다. 실패 결과 감사는 보존하되 다시 실행하지 않는다.
- 테스트 관측 helper는 None에만 기본값을 적용하고 명시적 빈 tuple은 유지한다. CONTINUE/SKIP의 실제 좁은 Git argv·editor·check=False와 실패 returncode/stdout 계약도 확인한다.
- 사용자 동작·공개 계약(호출/예외)·저장 형식과 schema/값·교정/재시도 한도·session/감사 및 현재 상태 기반 rebase 수락을 보존한다.

## Implementation

- `loop_manager_internal/storage/rebase_correction_state.py`에서 기존 transaction/generation 조건을 유지한 채 다음 필드 값을 명명한다. 새 상태 모델이나 저장 format을 도입하지 않는다.
- `tests/test_rebase_correction.py`의 관측 기본값과 좁은 실행 대역을 정리한다. 공통 대역이 충돌 해소나 상태 전이를 대신 수행하지 않게 한다.

## Validation

- Worker targeted unit: `tests.test_rebase_correction_state`, `tests.test_rebase_correction`의 보존 조건·generation 경쟁·빈 관측·autostash 및 CONTINUE/SKIP 실행/실패 사례.
- Manager 전체 verify: 실패 결과의 감사/재생 금지, 중단된 pending turn 재개, 실제 Git 충돌·수동 stage/continue와 session/작업물 보존·후보 재검증/lease 회귀를 유지한다.

## Progress

- [x] 저장된 평가 발견 7·22·23의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/storage/rebase_correction_state.py:166`, `tests/test_rebase_correction.py:15,90`의 필드별 조건과 관측/실행 대역 판단을 재사용했다.
- 현재 상태 전이 실패를 주장하지 않는다. G-182의 성공 근거/구현 입력 분류를 변경하지 않아 추가 기존 READY prerequisite는 없다. complexity low는 기존 조건의 국소 명명, risk medium은 해당 교정 감사/재개 오류이며 저장 generation·작업물 보존과 기존 복구로 대응한다.
