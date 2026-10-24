---
id: G-318
title: PR 전 일반 차단 계획의 snapshot 개정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

일반 `PLAN_BLOCKED`도 실제 PR 부재와 현재 선언·소유권이 확인되면 일반 Planner의 개정 snapshot에 포함한다. 삭제·참조 복구 여부와 개정 자격을 분리하고, 같은 ID·작업물·질문·차단 및 실행 이력을 보존한 문서 개정을 원격·catalog·DB에 반영한다. 개정 확정 뒤 원인별 재평가는 G-319가 소비한다.

현재 `planning/planner_validation.py:mutable_ready_snapshot()`은 조회 상태에 `PLAN_BLOCKED`를 포함하지만 `remote_deletions.recoverable_ids()`에 없는 차단 계획을 다시 제외한다. 반면 `remote_deletions.revisable_row()`와 `storage/planning_publication_state.py:apply_mutable_catalog_changes()`의 개정 CAS는 이미 PR 전 차단 상태를 지원한다. 이 기존 경계를 재사용해 일반 차단 계획의 누락만 보완한다.

## Acceptance Criteria

- R-082: 삭제·참조 복구 대상이 아닌 PR 미등록 `PLAN_BLOCKED`도 선언·revision·소유권과 writer 종료가 확인되면 snapshot에 들어간다. 최초 실행 여부, branch·worktree·session·checkpoint 또는 차단 상태만으로 제외하지 않는다. 관련 명세를 검토한 Planner가 필요한 같은 ID 개정을 제출할 수 있다.
- R-061/R-082: 개정 전후 ID와 시작한 계획의 경로, 본문 수행 기록·원래 질문·차단 사유·checkpoint·실패 및 승격 감사가 보존된다. 문서 개정으로 READY 전환·차단 삭제·사용자 승인·검증 성공을 만들거나 effective Worker level을 낮추지 않는다. 시작한 계획의 rename·직접 삭제·draft 전환은 허용하지 않는다.
- 실제 PR 등록·PR 조회 실패, 수동 소유·준비 intent·membership 변화, 실행 중 writer, 선언/hash/revision·원격 게시 권한 경쟁은 기존 보호 조건으로 거부하거나 보류한다. catalog의 독립 오류와 삭제 복구 proof를 일반 상태 변경으로 해제하지 않는다. snapshot 밖·PR 등록·MERGED·archive 계획과 유지보수의 무소유 READY 제한은 유지한다.
- 신규·개정 계획이 유효하게 식별되는 보류 선행을 참조해도 기존 선언·전체 graph 검증 뒤 게시·등록할 수 있다. 선행의 원래 보류와 실제 Merge 실행 gate는 유지하고, 부재·취소 ID·잘못된 source·자기 의존·순환 관계는 계속 거부한다. 이미 구현된 보류 dependency 등록을 재설계하지 않는다.
- 게시 전 실패, push 뒤 DB 미반영과 재시작에서도 저장 intent·snapshot을 재검증해 같은 개정을 멱등 반영한다. 원격·catalog·DB 확정 전에는 개정 적용이나 원인 해소를 기록하지 않는다. 다른 독립 계획의 정상 게시·진행을 보존한다.

## Implementation

- [x] 확인한 `loop_manager_internal/planning/planner_validation.py`의 `mutable_ready_snapshot()`·`_assert_mutable_ready_snapshot_current()`에서 일반 차단 상태와 catalog 복구 허용 집합의 책임을 분리한다. `catalog/remote_deletions.py:revisable_row()`·`recoverable_ids()`의 기존 PR/복구 보호와 `storage/work_claims.py`의 수동 소유 지문을 재사용한다. 상태 목록 전체를 느슨하게 하거나 새로운 개정 mode를 만들지 않는다.
- [x] 확인한 `planning/planning_catalog.py`의 실행 상태·수동 소유 snapshot 직렬화와 `storage/planning_publication_state.py:apply_mutable_catalog_changes()`의 실행 상태 CAS를 대조한다. 일반 차단 계획도 같은 manifest·게시 intent·현재 PR 재조회·짧은 확정 transaction을 사용하게 하고 기존 차단 row·질문·감사를 그대로 유지한다. 기존 지원으로 충족하는 코드는 변경하지 않는다.
- [x] 확인한 `planning/planner_publication.py`의 `_apply_general_planning_batch()`·복구 경로, `catalog/project_catalog_refresh.py`의 등록된 보류 선행 참조·최종 graph 검증을 소비한다. 개정 확정 자료를 G-319가 읽을 수 있는 기존 반영 경계에 남기되, 이 작업에서 원인별 재평가나 자동 해제를 추가하지 않는다.
- [x] 확인한 `tests/test_incremental_planning.py`·`tests/test_planning_publication.py`의 실제 SQLite·파일 및 모의 PR/Git fixture를 확장한다. PRODUCT의 기존 작업 개정 절과 ARCHITECTURE의 개정 snapshot 절에 실제 지원한 차단 계획 포함·보존 계약을 통합한다. 자동 역할 지침의 원본을 바꿀 필요가 있으면 `docs/AGENTS.planner.md`의 소유 절만 정합화하며 작업 prompt에 공통 규칙을 복제하지 않는다.

## Validation

- Worker는 위 두 테스트 모듈의 변경 관련 최소 unit case만 실행한다. 일반 차단 계획과 삭제 복구 차단 계획을 각각 snapshot에 포함하고 관련 본문 개정이 반영되는지 검사한다. `test_mutable_snapshot_includes_pr_free_ready_with_execution_evidence`·`test_started_revision_preserves_execution_audit_and_worker_level`의 assertion을 확장해 상태·원래 질문·session·실패·checkpoint·level 보존을 확인한다.
- 실제 PR 출현/조회 실패, snapshot 뒤 writer·수동 intent/membership·실행 근거·원격 변경을 제공해 게시가 거부되고 문서/DB의 기존 상태가 보존되는지 확인한다. 기존 PR·수동 소유 회귀와 같은 조건의 실제 Git/CLI 실행을 추가하지 않는다.
- 일반 차단 계획의 개정과 보류 선행을 유지/추가하는 후속 계획이 함께 게시·등록돼도 Worker 호출 수는 선행 실제 Merge 전까지 0이어야 한다. 기존 등록→보류 해제→실제 Merge 배정 회귀는 재사용하고, 무효·취소·순환 참조의 거부 assertion을 유지한다.
- 게시 전 중단·push 뒤 DB 적용 전 중단·동일 intent 재처리에서 개정은 한 번 반영되고 차단·질문은 그대로 남아야 한다. 문서 개정만으로 원인 평가 호출이나 성공 receipt·새 사용자 결정이 생기지 않는지 확인한다. 실제 게시 효과는 기존 publication 통합 회귀에 맡긴다.

## Progress

- [x] 제공된 delta revision의 R-082 추가 조건과 snapshot 필터·개정 CAS·게시/복구·관련 회귀를 정적으로 대조해 일반 차단 상태 전용 제외 조건과 재사용 가능한 개정 저장 경계를 확인했다.
- [x] 일반 `PLAN_BLOCKED` 상태를 삭제 복구 대상과 별도 필터링하던 조건을 제거하고 기존 catalog hold·실제 PR·수동 ownership 경계를 유지했다. SQLite·파일 회귀에서 시작 상태, 같은 ID·경로, 본문 Progress, 질문, 차단 사유, checkpoint, 세션, 실패·승격 이력을 확인했다.
- [x] 삭제 목표 복구 hold 중인 blocked 계획의 snapshot·본문 개정·DB revision 반영을 확인하고 status와 미해결 catalog hold가 유지됨을 확인했다. publication intent 복구·멱등 반영, PR 재조회·조회 실패, 수동 시작 intent와 게시 경합 targeted test를 통과했다.
- [x] PRODUCT의 일반 계획 개정 동작과 ARCHITECTURE의 snapshot·게시 보존 계약을 갱신했다. 원인별 재평가나 자동 해제는 추가하지 않았다.

## Decision Log

- 이 작업은 개정 자격·문서 게시만 담당하고 G-319는 확정된 개정과 관련 명세를 원인 재평가에 연결한다. 개정 가능성과 실행 가능성을 분리하므로 차단 해제를 선행하지 않고 독립적으로 검증·Merge할 수 있다. 현재 snapshot 밖의 차단 계획을 이번 Planner가 직접 편집할 권한을 만들지 않는다.
- complexity medium: 기존 snapshot·manifest·CAS가 차단 상태를 지원해 남은 책임은 후보 선정과 해당 경계의 보존 검증이다. risk high: 잘못된 소유권/PR 확인이나 이력 초기화는 진행 작업을 덮거나 사용자 판단을 우회할 수 있다. 실제 PR 재조회·실행/수동 소유 CAS·게시 intent와 기존 원문 보존으로 복구하며 경쟁·중단 회귀를 유지한다. priority high는 관련 명세를 반영하지 못한 채 차단이 고착되는 경로를 고치는 기준이다.
- 선택 adapter는 version 1 unittest·launcher Python 3.11–3.14·모듈별 독립 실행과 필수 compile이다. 기존 verify 연결을 변경하지 않으며 합성/모의 검증을 실제 OS 실행으로 보고하지 않는다. 이번 판단은 `85c605033cb894c95b1171304831404ce87bf3c2` → `21bcc798a6b4d4d5070cab88e98c10ad75f0c59d`의 변경 검토이며 최종 제품 전문 확인이나 실행 성공이 아니다.
