---
id: G-279
title: 진행 기록 기준의 commit 중단 복구
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

소유 commit이 끝난 뒤 완료 checkpoint·진행 기록 기준 저장 전에 중단돼도, 보존된 변경 전 definition과 승인 tree로 같은 기록 반영을 복구한다. 직접 구현된 `document_record`·순수 기록 판정·원격에서 확인한 delta 소비를 재사용하고, commit 복구 과정에서 그 근거가 유실되는 연결만 보완한다.

현재 `_commit_owned()`는 commit 전 `document_record.previous/tree`를 저장하고 완료 뒤 `after_head`와 기준을 기록한다. `WorkspaceHandler.recover()`의 `CheckpointAction.CORRECT`는 `commit_completed_before_checkpoint`에서도 선택된 필드만 새 snapshot으로 옮기며 `document_record`를 보존하지 않는다. 새 회귀는 완료 operation과 `after_head`를 미리 둔 기준 저장 중단만 검사하므로 실제 commit 후 완료 checkpoint 전 중단은 확인하지 못한다.

## Acceptance Criteria

- R-082: commit 전 보존한 definition·승인 tree·작업/소유 identity와 실제 commit 결과를 검증한 뒤, 완료 checkpoint 저장 전 중단도 같은 기록 반영을 마친다. 완료한 commit을 반복하거나 현재 문서로 변경 전 원문을 합성하지 않는다.
- commit 복구·후속 checkpoint 교체는 미완료 기록 반영 근거와 확정 결과를 보존한다. 정확한 결과 commit을 식별하지 못하거나 tree·계보·소유권·원문이 다르면 이전 기준과 미확정 이유를 남기며 다른 commit/HEAD의 내용을 기록 성공으로 채택하지 않는다.
- R-072/R-082: 순수 Progress 체크·기존 명확한 실행 기록만 기준에 반영한다. 같은 문서/commit의 새 목표·조건·검증 지시·sources·dependency·사용자 결정·frontmatter 변화나 비교 불가 원문은 함께 소비하지 않는다. legacy 입력 부재에서 과거 성공을 합성하지 않는다.
- 작업 definition과 적용 완료 checkpoint는 같은 CAS/transaction으로 확정하고 재중단·재시작에서 멱등 소비한다. 여러 순수 기록 commit이 이어지면 확인한 시작/결과의 관계를 유지하며 미완료 근거를 후속 intent로 덮어쓰지 않는다.
- 프로젝트 planning 기준은 원격에서 확인한 해당 문서 bytes/hash의 delta만 기존 `advance_verified_delta()`로 소비한다. 미게시/원격에서 제거·변경된 기록, 같은 경로의 미확인 조건과 다른 입력은 남긴다. `reference_revision`·명세/전체 planning 성공 지문·verify/Review/게시 성공·실패/level 감사를 기록 갱신으로 바꾸지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/execution/execution_publication_handler.py`의 commit intent/완료와 `execution_workspace_handler.py`의 `CheckpointAction.CORRECT`를 연결한다. `execution_checkpoint.py`와 `recovery/checkpoint_ownership.py`의 기존 실제 commit·tree·부모·소유 확인을 재사용해 `document_record`와 정확한 결과 commit을 보존한다. 기존 게시 재개·문서 반영·scope 근거도 새 snapshot에서 유지한다.
- [ ] `execution/execution_revision.py`의 `record_only_definition`, `advance_owned_record`와 `execution_revision_handler.py`의 재진입을 재사용한다. commit 완료 복구 후 기록 기준의 미완료 적용부터 수행하고 checkpoint 교체·추가 rebase가 이를 건너뛰거나 낡은 원문으로 덮지 않게 한다. 기록 반영 자체로 구현/검증 token의 성공을 만들지 않는다.
- [ ] `storage/execution_checkpoint_state.py`, `runtime_state.py`, `planning_input_state.py`의 기존 generation·transaction·원격 문서 delta 관측을 유지한다. 기록/후속 commit의 근거가 이어지는지 확인하며 새 저장 원본·별도 캐시·범용 journal은 추가하지 않는다.
- [ ] PRODUCT·ARCHITECTURE의 마지막 반영 기준 절에는 실제 보완한 commit 중단 복구·근거 보존만 통합한다. 현재 구현된 순수 기록 문법과 정상 원격 delta 처리는 재구현하지 않는다.

## Validation

- Worker는 `tests/test_execution_review_phase.py`의 commit 완료 복구, `tests/test_stage_documents.py`의 기록 기준 적용, `tests/test_runtime_state.py`의 원격 delta 소비에서 수정한 최소 targeted unit case만 실행한다. `tests/support/execution.py`의 Git double과 실제 checkpoint/SQLite를 재사용한다.
- commit 전 intent→실제 commit 완료→완료 checkpoint 전 중단→새 handler 재개를 연결한다. 이전 원문·승인 tree·결과 commit·기준/적용 marker 보존, commit no-call, 재중단 뒤 동일 결과 no-op과 session/실패/검증 근거의 독립 보존을 확인한다. 미리 `after_head`를 채운 fixture만으로 완료하지 않는다.
- tree/부모/소유/결과 commit 변경, 혼합 조건·frontmatter 변경·원문 부재, 원격 재전진·기록 제거, 연속 기록 commit과 checkpoint CAS 실패에서는 미확인 변경을 소비하지 않는다. 성공·거부 모두 실제 기준 row와 checkpoint를 검사한다.
- `tests/test_execution_checkpoint_recovery_phase.py`, `tests/test_execution_publication_phase.py`, `tests/test_incremental_planning.py`의 기존 복구/게시 연결을 유지한다. 실제 commit 효과가 mock으로 확인되지 않는 경우에만 기존 Git fixture의 최소 연결 사례를 보강하며 정책 조합별 전체 실행을 반복하지 않는다.

## Progress

직접 구현된 기록 기준 helper와 commit 완료 복구의 snapshot 교체·기존 회귀를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.

- [ ] commit 완료 복구의 기록 근거/결과 보존 연결
- [ ] 중단·혼합 조건·연속 commit·원격 delta 회귀 검증

## Decision Log

- complexity medium: 이미 있는 commit 소유 확인·기록 문법·checkpoint CAS·원격 delta의 명확한 경계를 연결한다. 새로운 기록 분류나 실행 정책을 설계하지 않는다.
- risk medium: 근거 유실은 같은 입력의 재planning·낡은 비교 기준과 진행 보류를 만들며, 기존 후보/게시 gate 및 보존 원문으로 복구할 수 있다. 결과 commit 불일치와 혼합 조건의 채택 거부를 확인한다.
- priority high: 중단 후 반복적인 문서 재판단과 사용자 복구를 요구하는 경로를 보완한다. 기록 반영과 실제 verify/Review·planning 성공을 분리하며 직접 구현에 소급 성공 기록을 만들지 않는다.
