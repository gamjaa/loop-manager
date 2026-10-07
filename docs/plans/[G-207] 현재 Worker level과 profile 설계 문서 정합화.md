---
id: G-207
title: 현재 Worker level과 profile 설계 문서 정합화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-052] Worker Classification and Initial Level Calibration.md
depends_on:
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: low
risk: low
priority: normal
---

## Purpose

ARCHITECTURE의 Worker 최초 level 산식과 profile 설정 설명을 실제 Merge된 R-052 구현에 맞춘다. 현재 PRODUCT·코드·12개 조합 테스트는 critical risk만 가산하지만 설계 문서는 과거 risk 가산과 level 7 상한을 현행 구조로 설명한다.

## Acceptance Criteria

- ARCHITECTURE의 `## 설정`과 `## Model과 Effort`가 complexity 1·3·5, critical risk만 +1인 최초 level 1~6 및 12개 조합을 정확히 설명한다. level 7·8은 완료한 교정 뒤 유효 Review 반복에 따른 승격으로만 도달하며 상한은 8이다.
- 선택 provider의 Worker level 1~8, 일반·점검 Planner profile과 참조 alias의 명시 설정·지원 조합 검증, 유지보수 Planner의 기존 일반 Planner 연결을 현재 구현대로 설명한다. Model·Effort는 운영 설정이 원본이고 예시는 누락 fallback이나 강제 profile이 아니다.
- 코드·PRODUCT·활성 R-052 및 관련 설계 설명 사이에 반대 산식·상한을 남기지 않는다. 실행된 계획의 effective level·session·작업물·승격 감사 보존과 정상 BLOCKED 해제의 초기화 계약은 유지한다. 사용자 결정 대기인 R-001·archive identity·legacy scope 정책을 변경하지 않는다.

## Implementation

- `storage/execplan_catalog_state.py`가 연결한 level 계산, `agents/agent_profiles.py`의 profile 검증과 기존 회귀를 읽어 위 두 설계 절과 필요한 직접 참조만 정합하게 갱신한다. 초기 산식과 Review 승격, 실제 Model·Effort 선택 책임을 구분한다.
- R-052와 PRODUCT의 현행 정책을 참조하고 이미 구현된 산식·설정·상태 전이를 재구현하지 않는다. 코드·설정 예시·Requirement 또는 다른 계획의 목표는 변경하지 않는다. 설계 문서가 provider 모델명을 새로 추천하거나 실행 권한을 부여하지 않도록 한다.

## Validation

- 문서만 변경하며 실행 동작을 추가하지 않으므로 새 unit test는 작성하지 않는다. Worker는 `tests/test_provider_profiles.py`의 12개 조합·명시 profile·누락 거부와 기존 Review 승격/level 보존 테스트를 읽어 문구·표를 대조하고 unit 미실행 사유를 보고한다.
- Manager 전체 verify는 기존 level 계산·profile 선택·Review 승격·저장 상태 보존 회귀를 유지한다. Reviewer는 최종 문구가 코드·PRODUCT·R-052와 일치하고 실행·검증·사용자 결정 조건을 변경하지 않았는지 읽기 전용으로 확인한다.

## Progress

- [x] 2026-10-07 `98f86422157333ff13f82e351f9721ee16558986`의 활성 명세 전문·level/profile 구현·관련 테스트 및 비종료 catalog를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 설계 문서 정합화
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- ARCHITECTURE는 Worker 설정을 level 1~7로 적고 low/medium/high/critical risk에 0/1/2/3을 가산해 최대 7로 제한한다. PRODUCT의 `### Model과 Effort 선택`, 현재 level 계산과 `ProviderProfilesTest.test_worker_level_boundaries_and_cap`는 R-052의 1~6 산식 및 level 1~8 명시 설정을 따른다. 이는 새 정책 선택이 아닌 현재 설계 설명의 미반영이다.
- G-202는 수동 제출 Planner의 입력·작성 근거와 compact catalog를 교정하는 미완료 계획이다. 이 계획은 이미 Merge된 전역 level/profile의 설계 설명만 다루므로 G-202의 구현을 중복하거나 그 작업을 완료로 추정하지 않는다.
- complexity low: 두 기존 설계 절을 명확한 구현·테스트에 맞추는 국소 문서 변경으로 새로운 상태 전이나 설계 선택이 없다. risk low: 오류가 설정·배정 설명에 국한되고 원본 문서 diff로 쉽게 복구할 수 있으며 실행 코드·저장 상태·외부 자원에 부작용을 만들지 않는다.
