---
id: G-333
title: 정책 테스트의 Git 준비와 판단 경계 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

Git 자체의 효과를 assertion하지 않는 정책·저장 테스트에서 실제 repository 준비와 정책 판단의 책임 경계를 분리한다. 관측 경계에 입력을 공급하고 실제 판단·SQLite·파일 보존 검증을 유지하며, 필요한 Git 통합 회귀는 남긴다.

revision `f5a9d4ea510c62cea769eacd32c604f32bf25425`의 저장된 발견 13·14·18·21·22·27은 공통 setUp이나 조건 행렬이 실제 init/commit/push를 요구하지만 assertion은 schema·상태·재시도·순수 분류에 있는 사례를 확인했다. 이 계획은 해당 사례만 전환하며 실제 Git 효과 검증이나 이미 완료한 R-080 전환을 다시 계획하지 않는다.

## Acceptance Criteria

- 아래 지정한 정책/저장 사례는 실제 Git subprocess 없이 같은 정상·실패·연속 관측·재시작 조건과 assertion을 검사한다. 실제 운영 판단·DB 전이를 성공 mock으로 바꾸지 않는다.
- 필요한 SQLite와 문서/파일 보존은 실제 fixture로 유지한다. Git 관측은 기존 port/adapter double을 재사용하고 새로운 범용 fixture·전역 cache를 만들지 않는다.
- 사용자 동작과 공개 계약인 CLI/API·수집 범위·필수 Git 통합, DB/문서/receipt의 저장 형식을 보존한다. 테스트 skip·timeout 확대·필수 assertion 삭제로 전환을 대신하지 않는다.
- 전환한 조건과 남긴 통합 회귀의 대응, 변경한 모듈의 실제 Git 호출 감소를 확인할 수 있다. 측정하지 않은 전체 verify 시간·절감률을 주장하지 않는다.

## Implementation

- [ ] 확인한 `tests/test_blocked_remote_observation.py`의 schema migration과 `test_remote_failure_cause_changes_are_persisted_as_new_input`를 독립 SQLite/관측 mock fixture로 옮긴다. 실제 원격 변화·재시작·작업 경로를 검증하는 나머지 통합 사례는 유지한다.
- [ ] 확인한 `tests/test_catalog_scheduling.py:test_dependency_blocked_ready_is_not_an_empty_catalog`는 Git 관측을 주입하고 현재 scheduler·SQLite·반환 종류·호출 역할·planning baseline·계획 상태를 실제로 검사한다. 기존 관측 port만 바꾸며 G-297의 배정 직전 재검사/대기 정책은 구현하지 않는다. `test_planning_and_ready_selection_pin_fingerprint_to_resolved_sha`의 실제 선택 SHA/worktree HEAD 연결은 남긴다.
- [ ] 확인한 `tests/test_maintenance_file_inventory.py:test_root_placement_inventory_keeps_fixed_paths_and_marks_root_source_for_review`는 명시적 `SourceInventory`로 category·reason을 검사하게 한다. 같은 파일의 multilanguage 원격 blob/mode·로컬 dirty 분리와 unsafe mode/revision 검증은 통합 회귀로 유지한다.
- [ ] 확인한 `tests/test_planner_validation_recovery.py`의 세 번째 저장 retry 무추가 호출·첫 validation retry·CLI 실패의 retry 비소비를 `tests/test_planning_recovery.py`·`tests/support/planning_case.py`의 기존 관측 경계와 실제 SQLite/임시 파일로 분리한다. 세션·호출 횟수·pending/error·파일 보존 assertion을 유지하고 실제 commit/게시·충돌/index 검증은 planning publication/rebase에 남긴다.
- [ ] 확인한 `tests/test_requirement_archiving.py:test_missing_archived_source_does_not_block_unrelated_requirement_review`를 `tests/test_requirement_archive.py`의 순수 calculate 경계로 통합한다. catalog/states/문서 입력과 candidates assertion을 유지하고 공통 repository/DB setUp 의존만 없앤다. 실제 blob·worktree·이동 보존 사례는 남긴다.
- [ ] 확인한 `tests/test_scope_project_recovery.py:test_active_execplan_uses_reconciled_identity_not_publication_equality`의 여섯 metadata/body 조건에 Git 관측/등록 입력을 주입한다. 실제 문서 파싱·reconciled SQLite 선언·필드별 수용과 보존을 유지한다. `tests/test_scope_rebase_recovery.py`·`tests/test_source_sync.py`의 실제 rebase/후보 보존을 대신 제거하지 않는다.

## Validation

- Worker targeted unit은 위 여섯 전환 대상의 변경한 case와 통합한 순수 계산 case다. 실제 Git 명령 경계가 호출되면 실패하는 guard로 정책 사례의 Git 호출 0회를 확인하면서 실제 판단/SQLite assertion을 실행한다.
- 이전 조건·호출 횟수·session·오류·pending·문서 보존·category/candidates assertion의 대응을 검토하고, 오류/경쟁 관측도 mock의 일괄 성공으로 사라지지 않는지 확인한다.
- 전체 verify는 남긴 exact-SHA/worktree·원격 변화·blob/mode·게시/rebase/index·archive 보존 통합 회귀와 전환한 정책 사례를 함께 포함해야 한다. 같은 모듈 준비 비용 비교가 필요하면 동일 계측/수집 범위에서 비교하며 전체 실행을 반복하는 새 완료 조건을 만들지 않는다.
- 읽기 전용 Review에서 실제 Git 효과 assertion이 없는 사례만 전환했는지, 판단 함수를 mock하지 않았는지와 모든 이동/삭제 assertion의 대응을 확인한다.

## Progress

- [x] 저장된 6건의 정책/실제 효과 구분과 남길 통합 회귀를 확인하고, 현재 관측 port에서 독립적으로 전환할 범위를 확정했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 정책 fixture 전환과 기존 assertion·통합 검증 대응 확인

## Decision Log

- complexity medium: 여섯 테스트의 관측 경계는 명확하지만 실제 SQLite·연속 입력·상태 보존을 서로 맞춰야 한다. risk medium: 과도한 mock은 실제 정책 회귀를 가릴 수 있어 입력 관측만 대체하고 판단/저장과 필요한 통합을 유지한다. 최초 level은 3이다.
- 현재 scheduler의 관측 입력만 분리하므로 G-297의 새 배정·대기 구현을 선행으로 요구하지 않는다. G-297은 해당 동작 변경을 계속 소유하며 이 계획은 현재 판단·저장 assertion을 보존한다. 이후 재평가 없이 지정 사례만 바꾸며 다른 느린 테스트로 범위를 확대하지 않는다.
