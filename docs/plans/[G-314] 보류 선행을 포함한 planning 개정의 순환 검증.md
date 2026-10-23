---
id: G-314
title: 보류 선행을 포함한 planning 개정의 순환 검증
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

등록된 보류 선행을 참조하는 planning 결과의 허용은 유지하면서, 보류 원문이 실행 catalog에서 제외된 경우에도 이번 생성·개정이 만드는 dependency 순환을 게시 전에 거부한다. 일반·유지보수 planning의 결과 검증과 게시 재검증이 같은 관계를 판단하도록 기존 공통 helper를 보완한다.

현재 `catalog/project_catalog_refresh.py:assert_planning_dependency_references()`는 보류 선행의 등록 여부를 검사하고, catalog에 없는 노드의 dependency는 `original_dependencies`에서 이어 읽는다. 그러나 `visited`로 재방문을 종료하므로 이 결합 관계의 순환을 검증하지 않는다. `catalog/execplan_catalog.py:_graph_issues()`도 일반 metadata 파싱에서 제외된 보류 노드의 outgoing dependency는 검사하지 않는다. 예를 들어 등록된 H의 저장 dependency가 T이고 H의 원문이 잘못된 risk로 보류된 상태에서, 허용 개정 T에 H를 추가하면 H→T→H가 두 검사 사이를 통과할 수 있다. 이는 정적 경로 대조이며 재현 실행이나 검증 성공이 아니다.

## Acceptance Criteria

- R-082: 이번 생성·허용 개정이 보류 선행의 확인된 dependency와 결합해 새 순환을 만들면 결과 검증·원격 게시 전에 거부한다. 직접·간접 순환과 여러 변경 계획을 함께 적용한 최종 관계를 검사하며, 보류 노드가 일반 metadata 파싱에서 제외돼도 결과가 달라지지 않는다.
- 현재 revision에 결합된 선언·보류 identity·원문/hash와 등록된 관계의 근거를 확인한다. 현재 검증한 변경 선언을 과거 DB dependency로 대체하지 않으며, 보류 원문을 정상 선언·실행 가능 계획으로 승격하거나 식별 불명 노드를 임의 생성하지 않는다. 관계를 확정할 수 없는 경우 해당 결과의 게시를 보류하고 원래 근거를 남긴다.
- 순환을 만들지 않는 신규·개정은 등록된 보류 선행을 직접·간접 참조할 수 있다. 기존 오류·보류 원인과 무관한 독립 계획을 전역 graph 오류로 거부하지 않으며, 기존 보류 관계에 단순히 의존하는 경우와 이번 변경으로 순환에 참여하는 경우를 구별한다. 선행 보류·작업물·session·실패 이력과 실제 Merge 실행 gate는 유지한다.
- 일반 planning, 유지보수의 허용된 mode별 개정 및 게시 후보 재검증에 같은 판정을 적용한다. 검증 후 원격 revision·선언 hash·snapshot·소유권이 바뀌면 기존 CAS/intent 복구를 유지하고 오래된 결과로 등록하지 않는다. 새 순환 거부는 실행 상태 변경·보류 해제·성공 지문 갱신을 만들지 않는다.
- 기존 잘못된 source/ID·미등록 선행·자기 의존·파싱 가능한 순환·삭제/취소 ID 참조의 거부와 유효한 결과의 중단 후 멱등 게시·등록은 유지된다. Worker 배정·priority·사용자 판단·문서 수정 권한과 ID 예약 정책을 바꾸지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/catalog/project_catalog_refresh.py`의 두 planning helper와 `catalog/execplan_catalog.py`의 graph 검증을 재사용한다. 검증한 변경 선언과 보류 선행의 근거 있는 관계를 결합해 이번 변경으로 생기는 순환을 판정한다. 단순 방문 중복과 현재 탐색 경로의 순환을 구별하고, 기존 오류를 격리하는 규칙을 전역 정상 catalog 요구로 되돌리지 않는다. 새 graph 저장소·범용 엔진·설정은 만들지 않는다.
- [ ] 확인한 `planning/planner_validation.py`의 변경 선언 overlay·`original_dependencies` 구성과 `maintenance/maintenance_validation.py`·`maintenance/maintenance_publication.py`의 동일 helper 호출을 대조해 최종 관계·등록 identity·고정 revision 검사를 일치시킨다. `storage/execplan_catalog_state.py:new_declaration_issues()`의 신규 선언 검사와 기존 보류 원문 보존은 유지한다. 일반 게시의 검증 결과 소비는 확인한 `planning/planner_publication.py`·`storage/planning_publication_state.py`의 기존 intent/CAS 경계를 사용한다.
- [ ] 확인한 `tests/test_project_catalog_refresh.py:ScopedCatalogRefreshTests`의 실제 SQLite·문서·모의 Git fixture와 `tests/test_incremental_planning.py`의 허용 개정/삭제 복구 fixture를 재사용해 빠진 결합 순환 사례만 보강한다. PRODUCT·ARCHITECTURE에는 실제 보완한 graph 검증 경계만 소유 절에 통합하고 Requirement·다른 ExecPlan은 Worker가 수정하지 않는다.

## Validation

- Worker는 위 두 테스트 모듈과 변경한 유지보수 게시 경계의 관련 최소 unit case를 실행한다. 등록된 H→T를 두고 H의 risk만 무효로 만들어 catalog에서 제외한 뒤, mutable T의 T→H 개정을 거부하는지 실제 parser·snapshot 검증으로 확인한다. H→U→T의 간접 경로와 같은 batch의 변경 선언 overlay도 같은 fixture에서 검사하며 판단 함수를 성공/실패 상수로 mock하지 않는다.
- 같은 fixture의 비순환 개정은 통과하고 H의 원문/hash·보류 evidence와 기존 T의 session·단계·실패 기록은 검증 전후 동일해야 한다. 기존 보류 순환에 의존하기만 하는 신규 계획을 이번 개정이 만드는 순환으로 오인하지 않는 사례를 포함한다. 유효한 등록과 실제 선행 Merge 전 미배정은 기존 `test_planner_can_publish_new_dependencies_on_held_plans_without_enabling_execution`을 재사용한다.
- 유지보수의 eligible READY와 새 계획을 통한 간접 순환은 결과 검증과 게시 재검증 모두에서 거부돼야 한다. `tests/test_project_catalog_refresh.py`의 기존 유지보수 검증·중단 게시 복구 fixture를 확장해 잘못된 결과의 intent/DB 등록·성공 지문 미생성과, 유효한 비순환 결과의 재시작 후 멱등 반영을 구별한다. 전체 Git lifecycle을 조건별로 복제하지 않는다.
- 기존 미등록 보류·누락 source/priority·자기 의존·일반 순환 거부, held 원문 변경·revision/소유권 경쟁 거부와 삭제 복구의 목표 보존 회귀를 유지한다. R-080에 따라 Git/원격은 관측 mock을 사용하고 실제 SQLite 저장·보존 assertion을 남긴다. 이미 파싱 가능한 순환을 검사하는 기존 테스트를 이 새 경계 테스트로 대체하지 않는다.

## Progress

- [x] 지정 revision `4a7f03fff00251bd3b021acac8206178d8e5e293`의 R-082 delta, 공통 helper·일반/유지보수 소비자와 기존 회귀를 정적으로 대조했다. 보류 선행 허용·등록·실제 Merge gate는 직접 구현으로 제외했다.
- [ ] 보류 선행과 변경 선언의 결합 순환 검사
- [ ] 결과/게시 검증·중단 보존의 최소 targeted 회귀

## Decision Log

- 이 계획은 R-082가 명시한 순환 거부의 남은 경계만 맡는다. G-278의 실패 planning suspend/독립 실행, G-297의 실제 호출 직전 실행 자격, G-189의 전체 차단·재개 감사와 중복하지 않는다. G-236 등의 보류를 해제하거나 그 작업의 구현을 선행으로 요구하지 않는다.
- complexity medium(최초 level 3): 기존 graph·보류 snapshot·변경 overlay의 책임 경계가 확인돼 있으며 여러 검증 소비자에 동일한 판정을 연결한다. risk medium: 잘못된 결과는 원격 계획에 순환을 게시해 후속 작업을 계속 대기시킬 수 있으나 기존 실행 보류·Merge gate가 남는다. 게시 전 거부·원문/상태 보존·경합 재검증으로 복구 범위를 제한한다. priority high는 핵심 자동 planning의 유효한 결과 등록과 잘못된 관계 거부를 함께 유지하기 때문이다. Model·Effort는 Manager 설정을 따른다.
- unittest adapter version 1, launcher Python 3.11–3.14, 모듈별 독립 수집·필수 compile·실제 Agent 호출 차단을 유지한다. Planner는 구현·테스트·전체 verify를 실행하지 않았다. 이 결과는 delta의 작업 구성으로, Requirement 전체 충족이나 프로젝트 최종 전문 확인이 아니다.
