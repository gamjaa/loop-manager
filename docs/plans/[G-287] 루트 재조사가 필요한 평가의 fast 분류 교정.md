---
id: G-287
title: 루트 재조사가 필요한 평가의 fast 분류 교정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
  - docs/requirements/[R-074] 유지보수 1회 평가와 즉시 종료.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

루트 조사 모델 호출이 필요한 평가를 정밀 범위로 분류해 PRODUCT의 모델 없는 `fast` 계약을 지킨다. G-206에 반영된 모든 루트 항목 조사·인용 검증·이동 계획 연결·게시 검사는 재구현하지 않는다. 남은 작업은 루트 근거 재사용 불가와 평가 mode·호출·성공 기준의 일치다.

현재 `TieredReviewRepository.select()`는 모듈 입력과 루트 배치가 같아도 재사용할 루트 근거가 없으면 `root_review_required` 이유의 `fast`를 반환한다. 일반적인 저위험 모듈 변경도 같은 mode를 사용한다. `units()`는 그 mode에서도 `root_reviews=None`이면 루트 unit을 만들고 `prepare_turn()`이 모델을 호출한다. 기존 fast 무호출 테스트는 루트 항목이 없는 fixture여서 이 조합을 확인하지 않는다.

## Acceptance Criteria

- PRODUCT의 `fast`는 루트·모듈 검토 모델 호출과 새 의미 검토 receipt를 만들지 않는다. 현재 원본·지침에 유효한 루트 조사 재사용 근거가 없으면 필요한 루트 unit을 포함한 `targeted` 또는 기존 조건에 해당하는 `full` 평가로 분류한다. 근거 부족을 빈 조사 결과로 바꿔 호출을 생략하지 않는다.
- R-028: 모듈 원본이 같거나 저위험 변경뿐인 tree, 소스 모듈 없는 문서·자산 tree도 모든 루트 조사와 현재 원문 인용 검증을 유지한다. 예약 경로 보호·불확실한 항목의 이동 금지·이동 후보 계획 연결·게시 전 성공 금지는 그대로 적용한다.
- 같은 revision·지침의 검증된 루트 근거를 재사용할 수 있으면 기존 정적 fast 경로를 유지한다. 지침 변경, 원본 revision 차이, legacy 성공 row의 근거 부재를 재사용 성공으로 합성하지 않는다. root-only targeted 성공은 마지막 전체 정밀 성공의 Merge 수·revision을 전진시키지 않는다.
- R-074: 이미 시작한 같은 cycle의 준비·재개·평가 원본 변경·legacy assessment 확장에서도 mode와 실제 필요한 호출 범위가 일치한다. 이전 원문·session·audit·완료한 모듈 판단은 보존하며 독립적인 새 cycle·중복 전체 스캔·사후 재검증을 추가하지 않는다. 정당한 전체 점검 조건과 기존 성공 기준의 수명은 유지한다.
- PRODUCT·ARCHITECTURE의 관련 설명은 실제 mode·루트 조사·재사용 경계와 일치한다. 기존 규칙을 완화해 fast 모델 호출을 허용하는 것으로 교정하지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/maintenance/tiered_review.py`의 `select()`·`prepare()`·`units()`에서 정적 fast 가능 조건과 루트 조사 필요 조건을 함께 판정한다. 기존 `Selection`·`root_review_source`·검증된 재사용 근거와 module/root protocol을 사용하고 별도 mode·설정·cache는 추가하지 않는다. 강제/첫/주기 전체 평가는 기존 조건을 유지한다.
- [x] 확인한 `loop_manager_internal/maintenance/tiered_ports.py:TieredMaintenancePort.prepare()`의 활성 assessment 재사용·같은 cycle 관련 범위 재개와 legacy 확장을 대조한다. 이미 저장된 fast 평가에 미완료 root unit이 있는 경우도 모델 없는 완료로 취급하지 않는다. 필요한 mode 교정은 기존 assessment identity 검증을 사용하고 원문·audit·완료한 판단·session의 보존과 실제 재사용 가능 여부를 구분한다.
- [x] 확인한 `loop_manager_internal/planning/tiered_assessment.py:prepare_turn()`의 호출·진행 문구·최종 계획 인계가 교정된 mode를 소비하게 한다. root 후보가 있으면 기존 계획 연결 경로로 계속 인계한다. `tiered_review.py:promote()`의 전체/부분 성공 기준과 `storage/maintenance_state.py`의 cycle 진입·종료 주기는 바꾸지 않는다.
- [x] 확인한 `tests/test_tiered_review.py`·`tests/test_tiered_assessment.py`의 기존 지침 hash 불일치·fast 무호출·소스 없는 조사 fixture를 조합해 아래 회귀를 추가한다. 필요할 때만 `tests/test_tiered_publication.py`의 성공 기준 보존 assertion을 확장한다. PRODUCT의 `### 유지보수 평가`와 ARCHITECTURE의 `### 유지보수 분석과 성공 근거`에는 실제 보완한 경계만 통합한다.

## Validation

- Worker는 위 두 테스트 모듈에서 변경한 최소 unit case만 실행한다. 기존 실제 root citation fixture와 SQLite repository를 재사용하고, mode·호출 조합은 Git 관측·Agent double로 검사한다. 새로운 Git 저장소나 전체 게시 lifecycle을 조건마다 반복하지 않는다.
- 모듈/루트 배치 동일·근거 부재, 저위험 모듈 변경·새 revision, 같은 revision·지침 변경, legacy 성공 row의 nullable 근거에서 `fast`를 벗어나 필요한 root unit만 검토하는지 확인한다. 모든 루트 항목·원문 인용·누락 거부는 유지돼야 한다.
- 같은 원본·지침의 유효한 조사 재사용에서는 fast의 모델 호출 수가 0이고 새 module/root 의미 receipt가 없어야 한다. 새 전체 점검 조건에서는 기존 full 범위가 유지되고, root-only targeted 게시 후에는 이전 전체 Merge 수·revision이 유지돼야 한다.
- 활성 fast assessment 재개와 원본 변경 뒤 같은 cycle의 관련 범위 재개를 검사한다. 미완료 root unit이 누락되지 않으며 저장된 원문·완료한 모듈 판단·감사와 검증 가능한 session을 유지하고 새로운 전체 스캔을 만들지 않아야 한다. 기존 root 후보 계획/게시 검증도 유지한다.
- 선택된 `unittest` version 1·launcher Python 3.11–3.14·독립 모듈 수집·fixture 의미·Agent 호출 차단·필수 compile을 유지한다. adapter나 전체 검증 체계를 개편하지 않는다.

## Progress

- [x] `d73e16632a5646dc6aa77482f7a2eb92cdec1e13` 이후 `5f0dc1c2af8027aed3bf13501f3d5c223f375215`의 변경, PRODUCT·ARCHITECTURE 관련 절, R-028·R-074와 현재 selector/unit/turn·테스트 원본을 정적으로 대조했다. 기존 비종료 catalog에 이 mode 조합을 담당하는 작업은 없다.
- [x] 평가 mode·루트 조사 필요 조건·재개 경계 교정
- [x] 최소 targeted unit 회귀와 관련 문서 정합성
- 2026-10-24: 현재 rebase 후보에서 fast 무호출·유효 근거 재사용과 선택/저장 사이 지침 hash 재검사·legacy 근거 거부·저장된 root result/session 재연결·root-only targeted 성공 기준·source drift 관련 범위와 실제 root citation 회귀를 targeted unit test로 확인했다. 전체 verify는 Manager가 수행한다.

## Decision Log

- G-206의 루트 조사·계획 hash·최종 ID allocation·publication/catalog 연결은 이미 반영됐다. 그 구현·과거 실행을 이 작업으로 소급 기록하지 않으며 현재 Planner는 구현·테스트·verify를 실행하지 않았다.
- root-only 정밀 필요를 `targeted`로 표현하는 것은 기존 세 mode의 의미를 유지하는 구현 교정이다. 새 평가 주기나 제품 기능을 추가하지 않는다. R-028의 신규 루트 정책 적용 범위 질문과 독립적으로 구현·검증·Merge할 수 있다.
- complexity medium: selector, 활성/legacy assessment 재개와 성공 기준의 경계를 함께 맞춰야 한다. risk medium: 잘못된 mode는 모델 비용·조사 누락·전체 성공 기준 오표시를 만들 수 있으나 새 파일 이동이나 Git 쓰기 권한은 없다. 기존 원문·receipt·cycle 근거를 보존하고 실제 호출 수·부분/전체 기준·재개 조합으로 검증한다. priority normal은 기존 필수 동작의 정합성 교정이며 Model·Effort는 Manager 설정을 따른다.
