---
id: G-130
title: Classify Planner Inputs and Verified Publication Changes
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-049] Input-Specific Planner Roles.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

현재 입력 지문은 active ExecPlan을 제외하므로 사용자가 게시한 계획 변경을 일반 Planner trigger로 사용할 수 없다. 최신 원격 입력을 문서·지침 변경과 코드·테스트·설정 변경으로 구분하고, 검증된 자체 계획 게시와 archive를 사용자 변경과 구별한다. 일반 Planner의 전체 작업 확인 기준과 제한된 점검의 비교 기준을 혼동하지 않는 입력 경계를 마련한다.

## Acceptance Criteria

- 같은 immutable `origin/<base-branch>` revision에서 PRODUCT, 활성 Requirement, ARCHITECTURE, AGENTS 및 `docs/plans/` 바로 아래 active ExecPlan의 추가·수정·rename·삭제를 분류한다. 사용자 계획 변경이나 문서와 코드의 동시 변경은 안전 gate가 허용하는 다음 선택 경계에서 READY보다 일반 Planner를 우선한다. AGENTS 변경은 기존 승인·검증 절차를 유지한다.
- 코드·테스트·설정 변경은 기존 전체 계획 입력의 포함 범위를 유지하며 문서 변경과 구분된다. local-only 변경, SQLite 실행 상태 변경, `docs/FUTURE_IDEAS.md`, Requirement template 및 archive 본문은 새 일반 계획의 입력으로 사용하지 않는다. 단순 SHA 전진과 같은 내용의 재관측은 trigger를 만들지 않는다.
- 일반 Planner의 검증된 create/update/rename/delete 게시 결과는 원격 확인과 catalog 적용에 결합해 이미 반영한 선언으로 기록한다. 다음 실행에서 같은 자체 결과가 일반 Planner를 다시 유발하지 않으며 push·적용 실패나 출처 불명확 변경은 성공으로 소비하지 않는다.
- 유지보수 Planner의 검증된 게시·개정도 실제로 적용한 계획 delta만 소비한다. 결합된 일반 계획이 없는 유지보수 성공으로 전체 PRODUCT·Requirement 확인 성공을 만들거나 동시에 바뀐 사용자 문서·코드를 무시하지 않는다.
- 기존 내용 보존 Requirement archive-only 및 검증된 ExecPlan archive 예외를 유지한다. archive에 내용 변경, 일반 사용자 계획 변경 또는 별도 입력 변경이 섞이면 그 변경을 제외하지 않는다. 경로 이동이나 commit 주체·메시지만으로 Manager 게시를 추정하지 않는다.
- 저장된 비교 기준은 근거 revision, 지문 형식과 확인 범위를 식별한다. 일반 Planner의 추가 작업 없음 기준과 향후 점검의 예정 작업 비교 기준은 별도로 보존한다. 비교 가능한 성공 기준이 없는 기존 DB는 일반 Planner를 통해 기준을 확정하며 기존 session·오류·실행·게시·유지보수 이력을 보존한다.

## Implementation

1. `loop_manager_internal/catalog/catalog_service.py`의 입력 경로 판별, `loop_manager_internal/planning/input_fingerprints.py`의 Git tree 기반 digest와 `loop_manager_internal/catalog/execplan_catalog.py`의 archive 변경 분류를 재사용한다. 필요한 문서/active-plan 선언/그 밖의 계획 입력을 명시적으로 분류하고 경로·object identity의 변경을 결정적으로 비교한다. 역할 판단에 필요한 작은 값 객체와 순수 비교 함수만 추가하며 별도 저장소 스캐너를 만들지 않는다.
2. `loop_manager_internal/planning/selection_service.py`에 사용자 active-plan 변경의 일반 Planner trigger를 연결한다. catalog 오류, 프로젝트 안전, 활성 실행·PR 충돌·유지보수 우선순위와 READY dependency 조건을 유지한다. 코드 전용 입력을 읽기 전용 점검으로 전환하는 실행 흐름은 G-132가 담당하며 이 계획에서는 기존 일반 Planner 경로를 보존한다.
3. `loop_manager_internal/planning/planner_publication.py`, `loop_manager_internal/storage/planning_publication_state.py`의 원격 확인·manifest·원자 적용 경계에서 일반 게시 후의 선언 비교 기준을 확정한다. `loop_manager_internal/maintenance/maintenance_publication.py`와 기존 유지보수 intent에는 실제 검증·적용한 delta의 근거만 연결한다. 원격이 게시 commit 이후 전진한 복구에서는 추가 변경을 별도로 재분류해 미관측 입력을 소비하지 않는다.
4. 기존 `runtime_state.py`와 `database.py`의 additive 저장·transaction 방식을 사용해 필요한 최소 기준 정보만 보존한다. 기존 지문을 이름만 바꾸어 점검 성공으로 해석하거나 현재 catalog만으로 과거 게시 provenance를 만들지 않는다. 호환 가능한 기존 성공 revision을 재대조할 수 없으면 일반 계획을 선택한다. DB schema·상태의 비호환 변경이나 운영 DB 수동 편집은 필요하지 않다.
5. 새 구현 파일이 필요하면 기존 planning/storage 패키지 안에 둔다. 루트 구조, 공통 문서·AGENTS와 기존 source/게시 경계를 변경하지 않는다. 새 모듈은 현재 OS verify의 기존 compile/discovery 범위에 포함한다.

## Validation

- Worker는 `tests/test_planning_fingerprint.py`, `tests/test_incremental_planning.py`의 입력·archive 사례와 `tests/test_planning_selection.py`의 문서 변경 선택 사례 중 변경한 메서드 및 새 회귀만 완전한 unittest 이름으로 실행한다. 예: `python3 -m unittest tests.test_planning_fingerprint.PlanningFingerprintTest.test_planning_input_path_boundary`. 실제 수정 경계에 필요한 사례만 추가한다.
- active-plan 추가·본문/metadata 수정·rename·삭제, 코드 동시 변경, AGENTS 변경, 같은 내용의 SHA 전진과 local-only 입력 제외를 확인한다. 기존의 계획 변경 무시 테스트는 R-049에 맞는 사용자 변경/검증된 자체 결과 구분으로 갱신한다.
- 게시 저장 경계를 수정하면 `tests/test_planning_publication.py`, `tests/test_incremental_planning.py`, `tests/test_maintenance_publication.py`의 직접 관련 사례에 push 확인 전·후 중단, SQLite 적용 실패·재시작, 원격 후속 사용자 변경과 provenance 부족을 보강한다. 유지보수 단독 성공이 전체 계획 기준을 전진시키지 않는지 확인한다.
- Manager 전체 verify는 사용자 계획 변경의 재계획, 자체 게시·내용 보존 archive의 반복 trigger 방지, catalog 원자 적용과 기존 오류·session·유지보수 기준 보존을 확인한다. Worker와 Reviewer는 전체 검증을 실행하지 않는다.

## Progress

- [x] 원격 입력 분류와 사용자 active-plan 변경 trigger 연결
- [x] 일반·유지보수 게시 및 archive의 검증된 비교 기준 보존
- [x] 호환 저장·중단 복구와 관련 targeted 회귀 검증
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-03: 현재 `_is_planning_input`은 `docs/plans/`를 제외하고 `_is_specification_input`은 PRODUCT·ARCHITECTURE·활성 Requirement만 포함한다. 관련 테스트도 계획 편집을 무시하도록 고정돼 있어 R-049의 사용자 계획 변경 감지가 미충족임을 확인했다.
- 현재 catalog의 G-066과 G-112~G-114는 Worker prompt·runtime·Planner 투영·Codex 경계를 담당하며 이 입력 분류 변경을 포함하지 않는다. 해당 작업과 과거 완료 계획을 재계획하지 않는다.
- 일반 게시의 원격 확인·mutable READY CAS·원자 적용과 유지보수 게시 intent를 재사용한다. 검증된 자동 delta를 소비하는 일과 전체 작업을 확인한 성공을 구분해 G-132가 제한된 점검 기준을 안전하게 사용할 수 있게 한다.
- priority는 `high`다. 사용자가 바꾼 예정 작업을 자동 실행 전에 반영하는 핵심 계획 경계를 다룬다. R-049의 실행 선택 변경을 근거로 하며 새로운 제품 목표나 게시 권한을 추가하지 않는다.
- 2026-10-03: 전체 입력 지문을 active ExecPlan을 포함하는 v3으로 올리고, 기존 코드·문서 비교 범위는 v2의 별도 비교 함수로 유지했다. 일반 계획의 성공 revision과 계획 선언 경로·내용 해시 기준을 `general` scope 및 형식과 함께 저장한다. 유지보수 게시와 검증된 ExecPlan archive는 원자 적용된 계획 delta만 이 기준에 반영한다. 후속 원격 사용자 계획 변경은 게시 배치 경로를 보존하는 한 별도 입력으로 남긴다. 기존 성공 revision의 저장 지문이 v2와 재대조될 때만 비교를 재사용하며, 그렇지 않으면 일반 Planner를 선택한다.
- 2026-10-03: Reviewer 지적에 따라 legacy `processed_sha`는 Planner 실행 없이 전진할 수 있음을 확인했다. 따라서 v2 지문과 해당 revision의 선언이 일치해도 과거 사용자 계획 변경의 검토를 증명하지 못한다. 저장된 검증 기준이 없는 기존 DB는 일반 Planner로 기준을 확정하며, 같은 기준을 요구하는 완료 판정도 보류한다.

- 2026-10-03: Reviewer의 동시 archive 회귀를 수정했다. 저장된 검증 선언과 현재 active ExecPlan이 일치하는 선택·완료 판정에서만 Requirement archive와 내용 보존 ExecPlan archive를 함께 분류한다. 진행 중 Planner 결과의 복구는 엄격한 기본 판정을 유지한다. READY 유무·반복 관측, 미검증 이동, 사용자 계획·코드·archive 내용 변경 및 완료 판정의 targeted 회귀를 확인했다.
