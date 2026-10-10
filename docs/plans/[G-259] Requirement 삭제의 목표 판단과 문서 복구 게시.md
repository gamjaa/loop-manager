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
complexity: high
risk: high
priority: high
---

## Purpose

Requirement 삭제를 일반 Planner의 목표 철회·유지·대체 판단과 허용된 관련 ExecPlan 삭제·개정의 원격 게시에 연결한다. source 부재를 오류로 보존하는 것에서 끝내지 않고 남은 PRODUCT·Requirement 목표와 직접·간접 참조를 함께 정리한다.

## Acceptance Criteria

- G-258의 검증된 삭제 관측과 보존 원문·마지막 source·현재 PRODUCT 및 활성 Requirement를 Planner 입력으로 제공한다. `missing-source`, `missing-plan`, `missing-plan-reference`, `deleted-goal`은 실행 gate를 유지하면서 복구 검토에 연결한다. 삭제된 Requirement를 현재 활성 경로에서 다시 읽어야만 철회 판단을 반환할 수 있도록 요구하지 않는다.
- Planner 판단은 작업 전체의 불필요함과 복수 source·PRODUCT에 남은 목표를 구분한다. 전체 철회는 권한 안의 문서 삭제안으로, 일부 근거 삭제는 해당 source·조건·구현·검증 지시·본문 링크의 개정안으로 반환한다. 필요한 목표·필수 검증을 source 경로 제거만으로 생략하거나 이미 구현된 코드를 되돌리지 않는다.
- 삭제된 작업의 직접·간접 dependency 및 본문·구현·검증 참조를 함께 재평가한다. 필요한 목표는 기존 유효 작업 또는 새 ID의 대체 작업으로 연결하고, obsolete/replaced 판단의 최신 source·구체적 이유·대체 dependency를 검증한다. 전체 graph·ID 예약·source 유효성과 실제 Merge gate를 유지한다.
- Planner의 직접 편집은 revision·hash·실행 근거·실제 PR·수동 소유권에 결합된 허용 snapshot으로 제한한다. 직접 삭제는 미착수·무소유 대상만 허용하고 시작한 작업의 개정은 같은 ID·경로·수행 기록을 보존한다. 시작한 작업 등 직접 삭제 권한 밖의 전체 철회는 보존 근거가 있는 읽기 전용 판단안으로 Manager에 전달해 안전한 게시·수명 처리로 연결한다. 실제 PR·수동 소유·draft 사용자 판단·archive·실행 중 writer의 보호를 우회하지 않으며 확정할 수 없는 조치만 근거와 함께 보류한다. 삭제된 작업 자체를 임의 복원하지 않는다.
- Manager는 목표 보존·허용 diff·최신 원격·PR·소유권을 재검증하고 원격 게시 확인 뒤 source·dependency·현재 선택 관계를 같은 결과로 반영한다. G-257의 기존 선행 보류는 계속 전파하며 해소된 참조 원인만 해제한다. 문서 게시나 Planner 판정 자체는 Worker 실행 준비·검증 성공·Merge 완료가 아니다.
- 수정안 생성·게시·DB 적용 중단, 추가 원격 삭제·source 변경·PR 또는 수동 예약 출현에서 실제 확정 지점부터 재검증한다. 미게시·부분 판단·오래된 snapshot으로 보류를 해제하거나 같은 판단·commit·push를 반복하지 않는다. 의미를 확정하지 못한 관계만 보존·보류한다.

## Implementation

현재 `remote_deletions`의 영향 관계·`recovery_goals`·hash 결합 판단과 `planning_catalog`·`planner_validation`·`planner_publication` 및 `planning_publication_state`를 재사용한다. Requirement 삭제와 여러 근거 중 일부 삭제를 복구 입력·반환 계약에 포함하고, 살아 있는 현행 source를 요구하는 계속 실행/새 작업 검증과 마지막 근거로 판단하는 전체 철회 검증을 구분한다. source 문자열뿐 아니라 목표·AC·본문 관계의 의미 판단은 Planner에 남긴다.

G-243의 수동 보호와 G-257의 게시 유효성 검증을 소비하고 전체 transaction·publication intent·원격 확인을 재사용한다. Worker·Planner가 소유권 밖 계획이나 archive를 편집하도록 지침을 넓히지 않는다. G-260이 실행 이력 있는 작업의 현재 취소를, G-261이 자원 정리를 담당하므로 이 작업은 그 성공을 문서 삭제로 합성하지 않는다. 기존 G-189의 감사·일반 planning과 최종 전문 확인을 대체하지 않는다. 실제 복구 입력·허용 게시·재개 계약만 PRODUCT·ARCHITECTURE에 갱신한다.

Manager가 수행할 전체 철회 판단안의 대상·마지막 source·원문 hash·삭제 revision·이유를 허용된 직접 편집 결과와 구분해 검증한다. 실행 이력이 있다는 이유만으로 판단안을 거부하지 않되 실제 PR·수동 소유권·보존을 확인하지 못하면 그 대상만 보류한다. Manager의 게시 결과를 G-260의 현재 취소 경계로 전달하며 읽기 전용 판단안 자체로 현재 DB나 실행 상태를 바꾸지 않는다.

## Validation

- `tests/test_incremental_planning.py`, `tests/test_planner_validation_recovery.py`, `tests/test_planning_publication.py`의 최소 targeted unit으로 R-055 D02/D03/D04를 확인한다. 단일 근거 전체 철회, 복수 source 일부 삭제, PRODUCT의 유지 목표, 본문에만 남은 참조와 필요한 대체 목표를 실제 parser·validator·SQLite로 검사하고 Git/Agent 관측은 mock한다.
- 삭제된 현행 source를 읽지 못해도 검증된 마지막 근거로 철회안을 평가하는 사례와, 같은 예외를 새 계획·계속 실행 source에 적용하면 거부되는 사례를 대조한다. 직접·간접 선행 보류의 G-257 회귀, started 기록 보존, PR·수동·draft·archive 경계 및 전체 graph 거부를 함께 유지한다.
- 시작한 작업의 직접 파일 삭제는 거부하고 같은 대상의 보존된 전체 철회 판단안은 Manager 검증에 전달되는지 확인한다. 판단안 이후 PR/수동 소유·원문·삭제 revision이 바뀌면 오래된 판단으로 게시·취소하지 않아야 한다.
- 기존 publication fixture에서 push 뒤 DB 미적용·재시작·원격 경쟁·판단 후 source 변경을 검사한다. 실제 허용 diff·원격 commit의 보존이 필요한 통합 회귀만 재사용하고 정책 조합마다 Git 게시를 반복하지 않는다.
- Manager 전체 verify는 게시 전 보류·현재 관계 원자 적용·독립 작업 지속·필수 dependency와 실제 Merge gate를 유지한다. Reviewer는 남은 목표 제거, 권한 밖 문서 수정, 미게시 보류 해제와 삭제의 성공 합성을 blocking issue로 확인한다.

## Progress

- [x] 2026-10-10 delta·R-055 D02–D04·현재 삭제 참조 입력 및 결과 검증·게시·관련 테스트를 정적으로 대조했다. 구현·테스트·전체 verify를 실행하지 않았다.
- [ ] Requirement 삭제 의미 판단·허용 문서 복구 게시 및 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 현재 삭제 복구는 ExecPlan의 missing-plan/취소 근거에 연결된다. `validate_planner_result`는 기존 개정도 우선 현재 source를 검증하며 `validate_recovery`는 영향 작업의 명시적 개정을 요구한다. Requirement만 삭제된 전체 목표 철회와 관련 삭제안까지 같은 검증·게시 계약으로 연결하는 작업이 남아 있다. 이미 구현된 무근거 dependency 제거 거부·원자 게시·hash 결합 참조 승인은 재사용한다.
- G-257은 복구 개정이 원래 필요로 하던 보류 선행 때문에 게시 거부되는 국소 결함이다. 그 목표를 확장하거나 중복하지 않고 이 작업의 선행으로 유지한다. 이번 snapshot의 `deleted_goals`는 비어 있으므로 실제 삭제 대상이나 DEPENDENCY_RECOVERY 성공 기록을 추정하지 않는다.
- complexity high: 삭제된 근거로 전체 철회를 검증하는 예외와 계속 실행의 현행 source 계약, 영향 closure·소유권 snapshot·게시/DB 확정 사이 불변식을 연결할 설계 판단이 남아 있다. risk high: 잘못된 철회·대체 게시가 필요한 목표·필수 검증을 없애거나 권한 밖 계획을 바꿀 수 있다. 마지막 원문·판단·publication intent·실행 감사를 보존해 수동 복구하며 외부 게시를 코드 rollback으로 복구한다고 가정하지 않는다. AC·Validation은 전체/부분 철회·경쟁·무근거 목표 제거를 구체적으로 확인한다.
