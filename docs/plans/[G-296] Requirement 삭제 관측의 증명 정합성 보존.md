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

- [x] `requirement_deletions()`가 활성 실행 row와 현재 `missing-source` 영향이 있는 취소 감사 row의 원문·원문 revision·삭제 관측 revision을 먼저 대조하고, 세 필드가 모두 같은 경우에만 영향 ID를 합치게 했다. 현재 활성 영향이 없는 취소 감사는 보존하되 source proof 집계에서 제외한다. 검증된 MERGED·archive 실행 row도 `verified_merged_archive_execution_ids()` 판정으로 제외한다. 활성 proof의 상충은 기존 Planner 오류 경계로 거부하며 결과는 조회 순서와 무관하게 정렬한다. 동일 proof의 row 문맥도 안정적으로 선택한다.
- [x] `_apply_execution_holds()`는 원문과 원문 revision이 같은 반복 부재에서 최초 proof·삭제 revision·row 문맥을 기존 hold와 새 hold 모두에 전달하고 최신 관측 revision은 hold에 따로 기록한다. 기존 hold 사이의 proof 충돌은 거부하며 공통 resolved 판단과 영향 ID를 보존한다. 영향 ID가 늘면 전체 대상이 바뀐 `prepared_withdrawal`은 재사용하지 않고, 원문 또는 원문 revision 변경은 현재 CAS transaction을 롤백해 저장된 proof·receipt·resume_status를 보존한다.
- [x] `catalog_service`의 기존 관측→hold transaction, `recovery_goals()`의 복수 proof 충돌 검사, `requirement_recovery_decisions()`의 최신 snapshot 재확인과 `prompt_payload()`·snapshot token 전달을 대조했다. 기존 결과 형식과 오류 경계를 유지하고 Planner가 상충 proof를 선택하지 않게 했다.
- [x] `RequirementRecoveryUnitTests`에 활성 source 이슈가 없는 상태에서 서로 다른 revision의 취소 감사 둘과 독립 READY를 둔 비차단·감사 보존 회귀, 활성 source 이슈가 있을 때 동일한 활성/취소 proof 합집합과 상충 거부, 활성 row끼리의 상충 거부를 추가했다. 서로 다른 revision의 검증된 MERGED·archive row와 독립 READY를 함께 두고 `recovery_goals()`가 비어 있는 비차단 회귀, 반복 부재의 최초 증명·판단·receipt·resume_status 보존, 새 hold의 최초 proof·resolved 판단 전달과 `recovery_goals()` 회귀, 영향 ID 추가, prompt/token 증명 결합 회귀도 유지한다.
- [x] PRODUCT의 Requirement 변경·취소 절과 ARCHITECTURE의 삭제 관측·recovery snapshot 절이 proof 비병합, 최초 관측 보존, 최신 snapshot 결합을 이미 명시함을 확인했다. 현재 구현과의 차이를 이번 코드 변경으로 해소했으며 문서 추가 수정은 필요하지 않았다. Requirement·다른 ExecPlan은 수정하지 않았다.

## Validation

- Worker는 새로 추가·변경한 최소 unit case만 선택해 실행한다. 실제 SQLite·증명 JSON·파일·snapshot/판단 검증을 사용하고 Git tree·원문 조회·PR 효과만 mock한다. Planner는 이번 계획 작성에서 테스트를 실행하지 않았다.
- `RequirementRecoveryUnitTests`에 같은 source를 가진 복수 실행 row 및 실행/취소 row 조합을 둔다. 활성 `missing-source` issue가 있으면 동일 proof는 영향 ID를 보존하고, 원문·원문 revision·삭제 관측 revision이 다르면 기존 오류 경계를 유지한다. 현재 활성 영향이 없는 취소 감사의 상충은 catalog를 막지 않으며 원래 cancellation row가 보존되어야 한다. row 순서를 뒤집어도 같은 결과이고 잘못된 철회 영수증·문서 삭제·활성 row 제거가 없어야 한다.
- 기존 catalog 조율 fixture에서 최초 삭제 관측을 적용한 뒤 무관한 최신 원격 commit을 관측하고 같은 부재를 재확인한다. 원문·source revision·최초 삭제 revision·resume_status와 기존 판단/영수증이 유지되는지, 현재 원격 관측은 별도로 확인되는지 검사한다. 추가 회귀는 서로 다른 revision의 verified MERGED·archive row 두 개와 독립 READY를 함께 두고 `recovery_goals()`가 비어 있는 상태에서 삭제 proof 집계가 정상 완료되며 독립 작업 상태를 보존하는지, R1 삭제 proof가 든 G1 hold 뒤 R2 삭제 proof와 새 G2 hold를 적용해 R1의 최초 proof·resolved 판단이 모든 hold에 같고 `recovery_goals()`가 R2 복구 목표를 만드는지 검사한다. 실제 원문/identity 변경에서는 stale 판단을 거부하고 이전 자료를 보존해야 한다. 이 상태 조합에 실제 Git lifecycle을 반복하지 않는다.
- 저장 직전/직후 중단·rollback 후 재개, 새 영향 작업 추가와 상충 proof 재관측을 확인한다. 유효한 동일 proof만 ID를 합치고 다른 proof·기존 사용자 판단·독립 작업은 보존해야 한다. 기존 snapshot 변경 거부·복수 source 철회·rename/archive·PR/수동 보호 회귀를 재사용하며 수행하지 않은 검증 성공을 기록하지 않는다.

## Progress

- [x] 검토 revision `2a63e07c47f712b232717a7f5b471558b5a8c9d9`의 변경 diff와 현재 함수·저장 소비자·관련 테스트를 정적으로 대조했다. 최초 집계는 동일 source의 후속 원문/revision을 대조하지 않고, hold 갱신은 새 proof가 있으면 이전 proof를 대체함을 확인했다. 기존 비종료 catalog에는 이 관측 보완을 구현하는 독립 작업이 없다.
- [x] 최초 집계에서 상충을 거부하고 동일 proof만 합쳤으며, 반복 부재에서는 최초 proof와 기존 복구 상태를 CAS transaction 안에서 보존했다. 실제 원문·revision 변경은 최신 입력으로 조용히 덮지 않고 기존 hold와 함께 오류로 남긴다.
- [x] PRODUCT·ARCHITECTURE의 현재 계약을 대조하고 좁은 targeted unit 회귀를 실행했다. 환경은 `scripts/verify.json`의 unittest adapter version 1, launcher Python 3.12.3/Linux이며 `docs/VERIFY.md`의 Python 3.11–3.14 지원 범위 안이다. 전체 verify는 실행하지 않았다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_source_proofs_union_affected_and_reject_stale_conflicts tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_union_only_identical_active_and_cancelled_proofs tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_absence_keeps_first_proof_receipt_and_resume_phase tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_deleted_renamed_source_uses_preserved_identity_and_observation_errors_are_not_withdrawal
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_source_proofs_union_affected_and_reject_stale_conflicts tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_union_only_identical_active_and_cancelled_proofs tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_absence_keeps_first_proof_receipt_and_resume_phase tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_deleted_renamed_source_uses_preserved_identity_and_observation_errors_are_not_withdrawal
재실행 결과: PASS — 4 tests, OK. 최초 실패는 새 재관측 case가 setUp의 기존 hold proof를 제거하지 않아 발생했으며, fixture를 격리한 뒤 동일 명령으로 통과했다.

TARGETED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_source_proofs_union_affected_and_reject_stale_conflicts tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_union_only_identical_active_and_cancelled_proofs tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_absence_keeps_first_proof_receipt_and_resume_phase tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_deleted_renamed_source_uses_preserved_identity_and_observation_errors_are_not_withdrawal tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_source_text_and_last_revision_are_snapshot_bound_before_preparation
최종 결과: PASS — 5 tests, OK. 실패 이후 전체 suite·integration·end-to-end·lint·build·type check·`scripts/verify`는 실행하지 않았다.

- [x] 2026-10-16 관리 rebase target이자 현재 HEAD `a5b182fe2da4b40d1c50097696905a548bc82b7c`를 기준으로 전체 Worker diff를 다시 검토했다. 변경은 이 ExecPlan 본문, `remote_deletions.py`, `execplan_catalog_state.py`, 관련 recovery test에 한정된다. R-055와 R-082 source 경로는 활성 파일과 일치하며 frontmatter·Requirement·다른 ExecPlan은 수정하지 않았다.
- [x] 2026-10-16 재검증에서 이전 HEAD의 성공을 재사용하지 않고 현재 후보의 위 다섯 `RequirementRecoveryUnitTests` case를 실행해 PASS (5 tests, OK)를 확인했다. 환경은 `scripts/verify.json`의 unittest adapter version 1과 launcher Python 3.12.3/Linux이며 `docs/VERIFY.md`의 Python 3.11–3.14 지원 범위에 든다. 전체 verify는 실행하지 않았다.
- [x] 2026-10-22 Manager가 수용한 rebase 결과 HEAD `886ceeef321cd60b803cfcf39f34395b238318f4`의 base `d0ed33bb77ab1cabfc2c397780851c4e9f5f7e22` 대비 전체 diff를 다시 검토했다. 변경은 이 ExecPlan, 삭제 proof 집계·hold 저장 코드와 관련 recovery 및 verify 회귀 test에 한정되며 선택 선언과 source 경로는 유효하다.
- [x] 현재 HEAD에서 proof/recovery 5개, 이전 verify 피드백의 partial/full runner와 Perl standalone/Manager 통합, coordinator fixture를 포함한 targeted test 8개를 실행해 PASS (8 tests, OK)로 확인했다. 이전 후보의 결과는 재사용하지 않았고 전체 verify는 Worker가 실행하지 않았다.
- [x] 2026-10-22 이번 Worker 호출에서도 HEAD `886ceeef321cd60b803cfcf39f34395b238318f4`의 base 대비 전체 diff를 다시 검토하고 같은 8개 관련 case를 targeted runner로 새 실행해 PASS (8 tests, OK) 및 현재 호출의 개별 test 기록을 확인했다. 전체 verify는 실행하지 않았다.
- [x] 2026-10-22 새 G2 hold가 추가될 때 기존 G1의 `R1@A`와 현재 입력의 `R1@B`가 서로 다른 hold에 저장되는 경로를 확인했다. 기존 hold proof를 먼저 통합해 현재 missing source를 위한 공통 proof로 모든 새 hold에 전달하고, 과거 hold 간 identity 충돌은 보류했다.

- [x] 2026-10-23 Manager가 수용한 rebase 결과 HEAD `71c8d1f41413667c3d8f7feab343dfbfa51ae307`의 base `55446254671206a070427af9fd5c32428e667e27` 대비 전체 diff를 재검토했다. PRODUCT·ARCHITECTURE와 R-055/R-082의 삭제·반복 관측 계약을 현재 hold 병합과 `recovery_goals()`에 대조했으며, 변경은 선택된 ExecPlan 본문, 삭제 proof 처리, 관련 recovery 및 verify 회귀 테스트에 한정된다. `git diff --check` 통과.
- [x] 2026-10-23 현재 후보에서 새 hold 추가·최초 proof 보존·`recovery_goals()` 회귀와 반복 부재 및 proof 충돌 검사를 targeted runner로 새 실행해 PASS (3 tests, OK)로 확인했다. 이전 후보의 테스트 결과를 재사용하지 않았고 전체 verify는 Worker가 실행하지 않았다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_new_hold_reuses_first_deletion_proof_and_recovery_goals
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_new_hold_reuses_first_deletion_proof_and_recovery_goals
재실행 결과: PASS — 1 test, OK. 최초 실행은 새 hold 추가 뒤 `recovery_goals()`가 `deletion_revision` A/B 상충을 거부하는 회귀를 재현했고, 기존 최초 proof를 새 hold에 복사하도록 수정한 뒤 같은 명령으로 통과했다.

TARGETED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_new_hold_reuses_first_deletion_proof_and_recovery_goals tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_absence_keeps_first_proof_receipt_and_resume_phase tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_repeated_source_proofs_union_affected_and_reject_stale_conflicts
최종 결과: PASS — 3 tests, OK. 전체 verify는 실행하지 않았다.

- [x] 2026-10-23 HEAD `98714d6b6032da00dcfaa1c960f4d8501fd87345`의 base `55446254671206a070427af9fd5c32428e667e27` 대비 전체 diff와 새 review 원인을 다시 대조했다. diff는 이 ExecPlan 본문, `remote_deletions.py`, `execplan_catalog_state.py`, 관련 recovery·verify tests에 한정되며 source와 frontmatter·Requirement는 바뀌지 않았다. `reference_issues()`에서 사용하는 검증된 MERGED·archive row 판정을 `requirement_deletions()`에도 적용해 완료 row를 활성 source proof 집계에서 제외하고, 비완료 실행 row와 취소 감사의 기존 proof 검증은 유지했다. `git diff --check` 통과.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_verified_merged_archive_rows
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_verified_merged_archive_rows
재실행 결과: PASS — 1 test, OK. 최초 실행은 서로 다른 revision의 verified archive MERGED row 둘이 활성 Requirement proof 충돌을 발생시키는 오류를 재현했고, 완료 row 제외 후 동일 테스트가 통과했다.

TARGETED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_verified_merged_archive_rows tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_union_only_identical_active_and_cancelled_proofs
최종 결과: PASS — 2 tests, OK. 완료 이력은 제외하고 활성/취소 이력의 기존 동일 proof 합집합·상충 거부를 유지했다. 전체 verify는 실행하지 않았다.

- [x] 2026-10-23 HEAD `5925a6ed27d9adbdedb88dbab4e166824adb7af4`의 base `55446254671206a070427af9fd5c32428e667e27` 대비 전체 diff와 ARCHITECTURE의 취소 감사 계약을 대조했다. 현재 `missing-source` issue가 있는 source에 한해 취소 row proof를 활성 집계에 포함하고, 영향이 없는 취소 행은 저장된 audit으로 보존하며 과거 source revision의 proof 조회·상충 판정에 끌어오지 않는다. 활성 proof 간 상충 검사는 유지했다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_conflicting_cancellations_without_active_impact
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_conflicting_cancellations_without_active_impact
재실행 결과: PASS — 1 test, OK. 최초 실행은 서로 다른 revision의 취소 감사 둘만 있어도 Requirement 삭제 proof 집계가 실패하는 경로를 재현했고, 현재 활성 source 영향과 분리한 뒤 통과했다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_conflicting_cancellations_without_active_impact tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_union_only_identical_active_and_cancelled_proofs tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_verified_merged_archive_rows
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_conflicting_cancellations_without_active_impact tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_union_only_identical_active_and_cancelled_proofs tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_verified_merged_archive_rows
재실행 결과: PASS — 3 tests, OK. 최초 조합 실패는 수정 과정에서 targeted fixture가 `snapshot`을 전달하지 못한 점과 테스트 호출 인자 누락 때문이었으며, fixture를 교정해 같은 명령으로 통과했다.

TARGETED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_conflicting_cancellations_without_active_impact tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_union_only_identical_active_and_cancelled_proofs tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_requirement_deletions_ignore_verified_merged_archive_rows
최종 결과: PASS — 3 tests, OK. 취소 감사만 있을 때 비차단·보존, 활성 issue가 있을 때 기존 proof 일치/충돌, 완료 archive 제외를 확인했다. 전체 verify는 실행하지 않았다.

## Decision Log

- G-259의 Merge된 복수 source 의미 판단·엄격한 결과 parser·철회 준비/영수증·게시를 다시 구현하지 않는다. 현재 `test_repeated_source_proofs_union_affected_and_reject_stale_conflicts`는 이미 저장된 hold 사이의 상충만 검사하고, `test_deleted_renamed_source_uses_preserved_identity_and_observation_errors_are_not_withdrawal`은 하나의 실행 row만 검사한다. 최초 관측에서 정보가 합쳐지면 이 후속 검사는 차이를 복구할 수 없다.
- 이 작업은 현재 PRODUCT의 변경된 삭제 원문/revision으로 철회를 확정하지 않는 계약과 ARCHITECTURE의 proof 비병합·반복 관측 보존을 구현한다. 최신 revision을 우선하는 새 정책, source 삭제 취소, 자동 사용자 승인이나 물리 자원 정리는 도입하지 않는다. G-260은 여기서 검증한 근거를 소비해 후속 수명을 처리한다.
- complexity medium: 기존 resolver·proof·hold·snapshot/CAS를 연결하는 한정된 관측 및 오류 처리 변경이다. risk high: 잘못 합친 근거는 필요한 작업의 철회와 문서 삭제를 허용하고, 잘못 바꾼 증명은 저장된 복구 판단을 무효화한다. 원문·실행/취소 감사를 보존하고 집계 전 상충 거부·재관측·중단 복구를 검증해 수동 재평가가 가능하게 한다. 초기 level은 3이며 Model/Effort는 Manager 설정을 따른다. priority high는 취소 판단의 안전 근거 보완 때문이다.
