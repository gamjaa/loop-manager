---
id: G-296
title: Requirement 삭제 관측의 증명 정합성 보존
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

Requirement 삭제를 처음 관측하거나 다시 관측할 때 작업별 원문·원문 revision·삭제 관측 revision을 보존한다. 같은 source 문자열만으로 서로 다른 증명을 하나로 합치거나 최초 삭제 증명을 최신 관측으로 덮어쓰지 않게 한다. 현재 구현된 source resolver, 복수 source 판단 검증, snapshot token, 철회 영수증과 publication transaction은 재사용하며, G-260의 취소 이후 수명 처리와 분리해 관측 근거만 바로잡는다.

## Acceptance Criteria

- R-055/R-082: 같은 source를 참조하는 실행 row와 취소 감사 row의 과거 원문을 각각 해당 revision의 resolver로 확인한다. 원문·원문 revision·삭제 관측 revision이 모두 같은 proof만 영향 ID를 합친다. 어느 하나라도 다르면 첫 row·최신 revision·조회 순서로 대표 증명을 선택하거나 다른 작업의 증명으로 대체하지 않는다. 상충 근거는 원문과 대상 관계를 보존하고 기존 실패·보류 경계로 전달하며, 잘못 합친 증명으로 작업 철회·문서 삭제·보류 해제를 승인하지 않는다.
- 같은 Requirement가 계속 부재인 상태에서 무관한 원격 commit을 다시 관측해도 이미 보존한 최초 삭제 proof와 작업의 resume_status·기존 판단/철회 준비 영수증을 덮어쓰지 않는다. 현재 관측 revision은 최초 삭제 revision과 구별한다. 원문·source identity·소유권 등이 실제 변경되면 기존 근거를 보존한 채 재검증하며 과거 판단을 새 증명에 자동 적용하지 않는다.
- Planner 입력의 text·source_revision·deletion_revision·affected_ids, snapshot token과 철회 직전 재확인이 같은 증명을 가리킨다. 대상별 증명을 구분할 수 없으면 불확실성을 남기며, 다른 source·작업의 유효한 근거나 독립 실행 gate를 임의로 초기화하지 않는다.
- 기존 rename/archive와 실제 삭제 구분, 복수 삭제 source의 전부 일치 조건, draft·수동 ownership·실제 PR·writer 보호, 원문 보존 후 삭제 및 중단 재개를 유지한다. 문서·실행 row·감사 자료를 증명 충돌의 해결 수단으로 삭제하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/catalog/remote_deletions.py:requirement_deletions()`의 source별 `setdefault()` 집계를 수정한다. 서로 다른 row의 조회 결과를 합치기 전에 증명 필드를 대조하고, 기존 `recovery_goals()`의 동일 proof 합집합·상충 거부와 일관되게 처리한다. 활성 row와 취소 감사 row를 모두 확인하고 과거 경로의 identity resolver를 재사용한다. row 조회 순서에 따라 증명이 달라지지 않게 한다.
- [ ] 확인한 `loop_manager_internal/storage/execplan_catalog_state.py:DefinitionStateRepository._apply_execution_holds()`의 기존 `deleted_sources` 병합을 수정한다. 현재 코드는 새 관측에 같은 source가 있으면 이전 proof를 보존하지 않는다. 반복 부재 관측과 실제 증명 변경을 구분하고, 최초 proof·resolved 판단·prepared_withdrawal·resume_status를 현재 CAS/transaction과 대조해 보존한다. 기존 JSON 근거와 저장 책임을 재사용하며 비호환 schema나 별도 상태 원본을 추가하지 않는다.
- [ ] 확인한 `loop_manager_internal/catalog/catalog_service.py:reconcile_execplan_catalog()`의 관측→hold 적용, `remote_deletions.py:recovery_goals()`·`requirement_recovery_decisions()`와 `loop_manager_internal/planning/planning_catalog.py:MutableReadySnapshot.prompt_payload()`를 끝까지 대조한다. 기존 결과 형식·snapshot 결합·현재 증명 검사로 전달하며, 상충을 Planner의 임의 의미 선택이나 성공 결과로 바꾸지 않는다.
- [ ] 확인한 `tests/test_planner_validation_recovery.py:RequirementRecoveryUnitTests`와 `tests/test_incremental_planning.py`의 삭제/공유 목표·재관측 fixture를 확장한다. 현재의 단일 row 과거 원문 조회와 이미 저장된 복수 hold 상충 회귀는 유지하고 최초 집계·다음 원격 revision 재관측의 빠진 조건만 보완한다.
- [ ] PRODUCT의 Requirement 변경·취소 절과 ARCHITECTURE의 개정 snapshot·작업 취소 절을 실제 구현과 대조한다. 이미 명시된 서로 다른 proof 비병합·반복 관측 보존 계약을 구현하고, 설명 보완이 필요하면 해당 절에만 통합한다. Requirement·다른 ExecPlan은 Worker가 수정하지 않는다.

## Validation

- Worker는 새로 추가·변경한 최소 unit case만 선택해 실행한다. 실제 SQLite·증명 JSON·파일·snapshot/판단 검증을 사용하고 Git tree·원문 조회·PR 효과만 mock한다. Planner는 이번 계획 작성에서 테스트를 실행하지 않았다.
- `RequirementRecoveryUnitTests`에 같은 source를 가진 복수 실행 row 및 실행/취소 row 조합을 둔다. 동일 proof는 모든 영향 ID를 보존하고, 다른 원문·같은 원문이지만 다른 원문 revision·다른 삭제 관측 revision은 합쳐지지 않아야 한다. row 순서를 뒤집어도 같은 보존/거부 결과이며 잘못된 철회 영수증·문서 삭제·활성 row 제거가 없어야 한다.
- 기존 catalog 조율 fixture에서 최초 삭제 관측을 적용한 뒤 무관한 최신 원격 commit을 관측하고 같은 부재를 재확인한다. 원문·source revision·최초 삭제 revision·resume_status와 기존 판단/영수증이 유지되는지, 현재 원격 관측은 별도로 확인되는지 검사한다. 실제 원문/identity 변경에서는 stale 판단을 거부하고 이전 자료를 보존해야 한다. 이 상태 조합에 실제 Git lifecycle을 반복하지 않는다.
- 저장 직전/직후 중단·rollback 후 재개, 새 영향 작업 추가와 상충 proof 재관측을 확인한다. 유효한 동일 proof만 ID를 합치고 다른 proof·기존 사용자 판단·독립 작업은 보존해야 한다. 기존 snapshot 변경 거부·복수 source 철회·rename/archive·PR/수동 보호 회귀를 재사용하며 수행하지 않은 검증 성공을 기록하지 않는다.

## Progress

- [x] 검토 revision `2a63e07c47f712b232717a7f5b471558b5a8c9d9`의 변경 diff와 현재 함수·저장 소비자·관련 테스트를 정적으로 대조했다. 최초 집계는 동일 source의 후속 원문/revision을 대조하지 않고, hold 갱신은 새 proof가 있으면 이전 proof를 대체함을 확인했다. 기존 비종료 catalog에는 이 관측 보완을 구현하는 독립 작업이 없다.
- [ ] 최초 집계·반복 관측의 증명 보존과 기존 소비 경계 연결
- [ ] 최소 targeted unit 회귀와 현재 제품·설계 계약 대조

## Decision Log

- G-259의 Merge된 복수 source 의미 판단·엄격한 결과 parser·철회 준비/영수증·게시를 다시 구현하지 않는다. 현재 `test_repeated_source_proofs_union_affected_and_reject_stale_conflicts`는 이미 저장된 hold 사이의 상충만 검사하고, `test_deleted_renamed_source_uses_preserved_identity_and_observation_errors_are_not_withdrawal`은 하나의 실행 row만 검사한다. 최초 관측에서 정보가 합쳐지면 이 후속 검사는 차이를 복구할 수 없다.
- 이 작업은 현재 PRODUCT의 변경된 삭제 원문/revision으로 철회를 확정하지 않는 계약과 ARCHITECTURE의 proof 비병합·반복 관측 보존을 구현한다. 최신 revision을 우선하는 새 정책, source 삭제 취소, 자동 사용자 승인이나 물리 자원 정리는 도입하지 않는다. G-260은 여기서 검증한 근거를 소비해 후속 수명을 처리한다.
- complexity medium: 기존 resolver·proof·hold·snapshot/CAS를 연결하는 한정된 관측 및 오류 처리 변경이다. risk high: 잘못 합친 근거는 필요한 작업의 철회와 문서 삭제를 허용하고, 잘못 바꾼 증명은 저장된 복구 판단을 무효화한다. 원문·실행/취소 감사를 보존하고 집계 전 상충 거부·재관측·중단 복구를 검증해 수동 재평가가 가능하게 한다. 초기 level은 3이며 Model/Effort는 Manager 설정을 따른다. priority high는 취소 판단의 안전 근거 보완 때문이다.
