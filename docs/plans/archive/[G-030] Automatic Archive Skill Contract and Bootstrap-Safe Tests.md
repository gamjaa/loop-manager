---
id: G-030
title: Automatic Archive Skill Contract and Bootstrap-Safe Tests
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-005] Automatic Requirement Archiving.md
depends_on:
  - G-011
complexity: low
risk: medium
priority: critical
---

## Purpose

새 프로젝트에 배포되는 Specification Skill의 Requirement 수명주기 안내를 이미 Merge된 자동 archive 동작과 일치시키고, G-023이 R-003과 R-005를 실제로 archive한 뒤에도 전체 verify가 현재 checkout의 활성 파일 위치를 전제로 실패하지 않게 한다. 이 선행 정리는 예정된 bootstrap archive가 결정적으로 검증 실패해 자동 실행을 차단하는 상태를 제거한다.

## Acceptance Criteria

- `.agents/skills/specification-docs/SKILL.md`는 `docs/requirements/` 바로 아래의 Requirement가 활성 Planner 입력이며, 관련 ExecPlan 중 마지막 구현 PR에서 Loop Manager가 eligibility를 다시 확인해 내용 변경 없이 `docs/requirements/archive/`로 자동 이동한다는 현재 제품 계약을 설명한다.
- Specification Skill은 완료된 Requirement를 사용자가 정상 수명주기의 일부로 직접 archive해야 한다고 지시하거나, 대화형 문서 작성 과정에서 자동 archive eligibility를 우회해 파일을 이동하지 않는다.
- Skill은 archive PR이 Merge되기 전까지 활성 Requirement가 Planner 입력으로 남고, 남은 관련 비종료 ExecPlan, archive 충돌 또는 source 관계 불확실성이 있으면 자동 이동되지 않는다는 안전 경계를 간결하게 유지한다.
- 기존 문서 역할, Ready Check, Specification Skill의 쓰기 범위와 `docs/plans/*.md` 비변경 규칙은 보존한다.
- `tests/test_requirement_archiving.py`의 bootstrap 회귀 검증은 현재 저장소 루트에 R-003과 R-005가 계속 활성 파일로 존재한다고 가정하지 않는다. G-023 Merge 뒤 두 파일이 archive에 있어도 전체 verify가 통과해야 한다.
- G-011 기능 도입 PR이 Requirement를 직접 이동하지 않았고, 일반 이력과 eligibility 계산이 후속 G-023의 R-003/R-005 후보를 만든다는 동작은 저장소의 현재 파일 위치와 독립적인 fixture 기반 테스트로 계속 검증한다.
- 공유 Specification Skill이 자동 archive 계약을 포함하고 더 이상 사용자 수동 archive를 지시하지 않는다는 회귀 검증을 추가한다.
- 이 계획은 archive runtime, eligibility 계산, Requirement 내용과 기존 ExecPlan을 변경하지 않는다. 실행 시 계산된 일반 archive 후보가 없다면 Requirement 파일도 이동하지 않는다.

## Implementation

- Specification Skill의 `Requirement Lifecycle`을 PRODUCT와 R-005의 자동 archive 흐름, 마지막 관련 계획 조건 및 충돌 시 보존 규칙에 맞게 수정한다.
- 현재 checkout의 R-003/R-005 활성 경로 존재 여부만 확인하는 `test_g011_keeps_bootstrap_requirements_active`를 제거하거나 fixture 기반 수명주기·Skill 계약 테스트로 교체한다.
- 기존 `test_bootstrap_uses_general_history_for_r003_and_r005`가 G-011 Merge 이력, G-023 현재 계획과 일반 후보 계산을 계속 검증하도록 유지하고, 실제 프로젝트 문서 위치에 결합된 중복 assertion은 만들지 않는다.
- 복사 스크립트가 `.agents` 전체를 배포하는 기존 경계를 유지해 수정된 Skill이 새 프로젝트에도 동일하게 전달되도록 하며, 별도의 runtime 분기나 Requirement ID별 특별 처리를 추가하지 않는다.

## Validation

- Worker는 `RequirementArchivingTest`의 bootstrap·Skill 계약 관련 개별 test case 또는 해당 test module만 targeted unit test로 실행한다.
- R-003/R-005가 활성 경로에 있는 상태와 archive 경로로 이동한 상태 어느 쪽에서도 테스트가 repository-root 위치 가정 때문에 실패하지 않는지 검증한다.
- Skill에 자동 archive 주체, 마지막 관련 ExecPlan 조건과 충돌 시 보존 경계가 있고 사용자 수동 archive 지시가 제거됐는지 검증한다.
- 전체 verify에서 G-011 archive eligibility, G-023 bootstrap 후보, archived source fallback, Planner 활성 입력 경계와 공통 파일 복사 동작이 유지돼야 한다.

## Progress

- [ ] Specification Skill의 Requirement 수명주기 계약 정합화
- [ ] repository-root 활성 파일 위치에 결합된 bootstrap 테스트 제거
- [ ] fixture 기반 bootstrap 및 공유 Skill 회귀 검증 보강

## Decision Log

- G-011의 일반 archive runtime은 이미 Merge됐고 G-023은 실제 문서 이동만 계획하지만, 현재 테스트 하나가 이동 전 repository 상태를 영구 조건으로 고정해 G-023의 전체 verify를 실패시킨다. 자동 실행 차단을 막기 위해 이 계획을 `critical`로 두고 G-023보다 먼저 선택되게 한다.
- Specification Skill은 초기 설정 시 `.agents` 전체와 함께 새 프로젝트로 복사되므로, 수동 archive 지시는 현재 저장소만의 문서 오기가 아니라 이후 프로젝트에도 전파되는 제품 계약 오류다.
- bootstrap 동작은 Requirement ID를 runtime에 특별 처리하지 않고 기존 fixture 기반 eligibility 테스트로 검증한다.
