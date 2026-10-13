---
id: G-259
title: Requirement 삭제의 목표 판단과 문서 복구 게시
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on:
  - G-258
  - G-257
  - G-243
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

Requirement 철회의 의미와 남은 공유 목표를 허용된 동일 ID 개정·문서 게시·현재 DB 관계에 일치시킨다. 현재 missing-source 원문 보존, REQUIREMENT_RECOVERY, 시작한 작업의 Manager 삭제 준비와 전체 철회/PRODUCT 공유 목표 회귀는 직접 구현돼 있다. 이를 재사용해 복수 근거·부분 조건·엄격한 판단 입력과 경쟁/중단 복구를 완결한다.

## Acceptance Criteria

- R-055: 실제 source 삭제의 원문·마지막 revision·현재 PRODUCT/다른 활성 Requirement를 같은 복구 입력으로 제공한다. missing-source와 기존 missing-plan/reference/deleted-goal은 보류와 별도로 검토하며 삭제된 활성 경로를 다시 읽도록 강제하지 않는다. archive·동일 identity rename·조회 실패는 철회가 아니다.
- R-055/R-082: 전체 목표 철회와 일부 source/Acceptance Criteria 철회를 구분한다. 부분 철회는 해당 조건만의 체크리스트·관련 파일/변경 목적·검증·본문 참조를 갱신하고 다른 근거의 목표·공유 구현·필수 검증을 보존한다. 일반 조건 변경의 개정은 기존 planning 입력/게시를 재사용하며 전체 source 삭제로 오인하지 않는다.
- R-055/R-082: 복수 삭제 source의 결과가 같은 작업에 연결돼도 남은 목표·모든 근거와 일치해야 한다. 중복/상충 판단·잘못된 identity/outcome·빈 이유·오래된 원문/revision은 거부하거나 해당 범위만 보류한다. 일부 판단만으로 전체 철회·관계 해제를 만들지 않는다. Planner가 의미를 판단하고 Manager가 근거·형식·권한을 검증한다.
- 삭제된 선행의 직접/간접 dependency와 본문·구현·검증 참조를 함께 대조한다. 필요한 목표는 유효한 대체 작업으로 연결하고 DEPENDENCY_RECOVERY의 최신 source·이유·replacement를 검증한다. 전체 graph·ID 예약·실제 Merge gate와 G-257의 기존 선행 보류는 유지한다.
- Planner 직접 삭제는 허용된 미착수 대상만 가능하다. 시작한 작업은 ID/경로/수행 기록을 보존한 개정 또는 읽기 전용 전체 철회 판단으로 전달한다. Manager만 안전한 writer 경계·실행 snapshot·원문 보존·실제 PR 부재·수동 소유권을 검증해 삭제를 준비한다. G-243의 수동 intent/membership, 미해결 사용자 판단·archive·writer 보호를 우회하지 않는다.
- 원격 최종 변경을 확인한 뒤 개정·삭제·source·dependency·현재 선택 관계와 복구 감사를 같은 기존 publication transaction으로 반영한다. push 뒤 DB 미적용·정리 준비 후 중단·원격/PR/수동 경쟁은 실제 확정 지점부터 재검증한다. 미게시·부분 판단으로 보류를 해제하거나 이미 게시한 결과를 반복하지 않는다. 문서 철회가 구현/검증/Merge 성공이 되지 않는다.

## Implementation

- [ ] `loop_manager_internal/catalog/remote_deletions.py`의 직접 구현된 requirement_deletions·recovery_goals·REQUIREMENT_RECOVERY·prepare_withdrawals를 재사용한다. 삭제 원문과 현재 유지 목표를 구별하고 복수 source 결과를 작업별로 대조한다. 판단 형식은 기존 엄격한 JSON helper를 먼저 확인해 재사용하며 의미 판단을 해시/상태만으로 대체하지 않는다.
- [ ] R-082의 부분 조건 철회는 `loop_manager_internal/planning/input_fingerprints.py`, `planner_validation.py`의 기존 변경 입력·snapshot 개정으로 처리한다. source 문자열만 지운 예시를 확장해 작업 항목·파일 목적·공유 구현·테스트·dependency의 근거 연결을 확인한다. 살아 있는 source가 필요한 계속 실행/새 계획 검증과 보존 원문으로 하는 전체 철회 검증을 혼동하지 않는다.
- [ ] `planner_validation.py`, `planning_catalog.py`, `planner_publication.py`와 `loop_manager_internal/storage/planning_publication_state.py`의 구현된 withdrawal manifest·정리 영수증·CAS·원자 적용을 재사용한다. 준비 중 및 게시 직전 실행/원문/삭제 revision·실제 PR·수동 준비/소유의 변화에서 오래된 판단을 적용하지 않으며 시작한 작업의 직접 파일 삭제는 계속 거부한다.
- [ ] G-257의 선행 보류 보존과 G-243의 수동 보호를 소비한다. 취소·요청 수명은 G-260, 물리 자원 정리는 G-261의 별도 범위로 연결하고 이 작업의 문서 게시만으로 그 완료를 만들지 않는다. snapshot 밖·PR/archive 계획은 편집하지 않는다.
- [ ] `tests/test_incremental_planning.py`, `tests/test_planner_validation_recovery.py`, `tests/test_planning_publication.py`의 원문 보존·withdrawn/retained·started resume fixture를 확장한다. PRODUCT·ARCHITECTURE는 이미 반영된 복구 설명을 유지하며 실제 보완한 계약만 통합한다.

## Validation

- 기존 전체 철회·PRODUCT 공유 목표·started 이력 보존 사례에 복수 Requirement, 본문에만 남은 목표와 공유 구현, 일부 Acceptance Criteria만 삭제된 사례를 연결한다. 해당 조건만 제거되고 남은 체크리스트·파일 목적·검증·source/dependency가 실제 문서와 DB에 유지되어야 한다.
- 복수 삭제 source의 상충·부분/uncertain 판단, 중복 marker/key·잘못된 target·stale 원문/revision과 새 계획에 archive/삭제 source 예외를 적용하는 경우를 거부한다. 필요한 선행의 제거는 기존 대체 근거/실제 Merge gate 없이 게시되지 않아야 한다.
- Manager의 시작한 작업 삭제 준비 직전/직후 중단과 재시작, 판단 후 원격·실제 PR·수동 intent/membership·writer 변화에서 원문/이력 보존·무게시·보류를 확인한다. 기존 실제 parser·SQLite·파일을 사용하고 정책 조합의 Git/Agent 관측은 mock한다.
- 기존 publication fixture로 push 후 DB 미적용·transaction rollback·재시작과 source 관계의 멱등 적용을 검사한다. 실제 Git 연결 회귀는 원격 최종 문서/관계 보존에 필요한 최소 사례만 사용한다. 부분/전체 verify 회귀는 원자 적용·독립 진행·catalog hold·현재 source/dependency·ID 예약과 사용자 판단 보호다.

## Progress

현재 원격의 삭제 source 증명, 복구 결과·Manager withdrawal 준비/manifest와 미착수·started의 전체 철회/공유 목표 회귀를 정적으로 확인했다. 직접 구현을 이 ExecPlan의 수행·검증 성공으로 기록하지 않으며 테스트는 실행하지 않았다.

- [ ] 복수 근거·부분 철회의 의미/작업 연결과 판단 입력 검증
- [ ] 준비/게시/DB 경합·중단 재개 보완
- [ ] 관련 targeted 검증과 문서 정합성

## Decision Log

- 삭제 source가 있어도 남은 PRODUCT·Requirement 목표는 유지한다. 직접 구현된 source별 복구를 다시 만들지 않고 작업별 공유 목표·부분 조건·실제 게시에 남은 검증을 보완한다. 일부 AC 삭제는 ordinary delta이며 missing-source의 전체 철회 예외로 처리하지 않는다.
- `prepare_withdrawals`가 시작한 작업의 보존 원문과 준비 근거를 저장한 뒤 Manager가 문서를 제거하는 경로는 구현됐다. 이를 Planner의 직접 삭제 권한으로 일반화하지 않으며 안전한 취소 이후 현재 수명은 G-260에서 완결한다.
- G-257/G-243의 선행 목표와 기존 source 문자열을 유지한다. 이번 deleted_goals는 비어 있어 DEPENDENCY_RECOVERY나 REQUIREMENT_RECOVERY의 실제 성공 판정을 만들지 않는다.
- complexity high: 복수 철회/공유 목표·현재 source 계약·준비 영수증·권한 및 원격/DB 중단 사이의 일관된 판단이 남아 있다. risk high: 잘못된 전체 철회나 대체 게시는 필요한 목표/검증 또는 소유권 밖 문서를 잃게 한다. 원문·판단·intent·실행 감사를 보존해 수동 재평가/복구하며 부분 철회·상충·PR/수동 경쟁·중단을 검증한다.
