---
id: G-256
title: 최신 조건의 rebase 교정과 저장 결과 재평가
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on:
  - G-255
  - G-242
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

G-255의 문서 충돌 처리를 최신 작업 조건·Worker 교정·검증과 저장된 결과/차단의 재평가에 연결한다. 보존된 G-256 후보의 저장 입력·READY/pending 수명·완료 definition 연결 구현을 이어받아, 최신 base의 응답 선저장·중간 선언 검사 유예·Git 완료 후 frontmatter 복원과 통합한다. 후보의 기존 구현·검증·교정 기록을 보존하고 아직 확인하지 않은 통합 조건만 보완한다.

## Acceptance Criteria

- Worker 최초·재개·교정 진입 전에 필요한 프로젝트 일반 planning의 관련 개정 게시·DB 적용을 마치고 그 immutable base의 ExecPlan을 반영한다. PR 등록 뒤 기존 계획 개정 금지·수동 소유 보호·dependency 준비 조건을 지키며 Planner와 구현 worktree의 writer/Git operation을 겹치지 않는다. 소유 rebase가 이미 진행 중이면 기존 target·pinned 문서로 그 operation의 완료/복구를 먼저 처리하고 안전한 경계에서 남은 최신 명세 planning·반영을 이어간다. 작업별 상위 명세 영향 판단·새 plan_text 생성을 다시 도입하지 않는다.
- R-082: 상위 명세 변경에 필요한 일반 planning과 ExecPlan·코드 변경만의 실행 대응을 구분한다. 후자는 선언·source·dependency의 Manager 검증과 기존 Worker 세션에서의 구현 판단·필요한 보완·targeted test로 처리한다. 유효한 구현 충족 근거가 있으면 필요한 verify·Review를 계속하고, 새 조건을 확인할 근거가 없으면 Worker에 최신 ExecPlan·delta·과거 근거를 함께 전달한다. 별도 문서 영향·구현 적합성 Planner, 코드 전용 점검·일반 Planner 인계를 되살리지 않는다.
- 문서-only 자동 해소 뒤 발견 단계와 실제 구현·검증 근거로 필요한 단계만 선택한다. 그 외 파일 충돌은 verify·Review·게시 단계에서 발견해도 같은 Worker session의 교정으로 연결하고, 최신 문서를 기준으로 양쪽 형상을 검토·선택·결합하며 필요한 targeted test를 수행한다. Reviewer의 읽기 전용 권한과 선행 Review·검증 보완의 기존 의미를 유지한다.
- 본문·sources·연결 명세·archive 범위·지침 변화와 코드 충돌이 함께 있거나 이전 definition 원문이 없어도, 현재 identity·소유권·유효한 최신 조건이 확인되면 보존된 작업의 교정·검증을 이어간다. 과거 성공을 재사용할 증거가 없으면 성공 근거만 무효화하며, 입력 변경 자체로 최종 차단하거나 session·작업물·실행/실패·level 감사를 폐기하지 않는다.
- 저장된 rebase Worker 결과와 pending stage/continue는 현재 후보·문서 조건과 결합해 소비한다. 파일 상태가 같아도 조건이 바뀌면 이전 READY나 과거 Agent snapshot/marker만으로 새 조건 충족을 승인하지 않는다. 반대로 구현 입력이 유효한 OID 변화만으로 Worker를 반복하지 않고 필요한 verify·Review로 이어간다. 입력이 같은 완료 교정은 필요한 후속 Git/검증 단계부터 재개한다.
- R-082·현재 PRODUCT: 충돌 교정 응답·실제 호출 시점의 입력 snapshot·session·종료 코드·결과 종류는 종료 후 범위/선언 검사보다 먼저 같은 turn에 보존한다. 후속 검사 실패나 저장 뒤 중단에도 응답과 당시 입력이 남으며 최신 definition을 과거 호출 입력으로 소급하지 않는다. 선언 오류는 `PLAN_INPUT_INVALID`로 처리하고 같은 오류를 Worker 교정으로 반복 전달하지 않는다.
- R-055: 저장 결과/pending continue의 재평가에서도 전체 역할 diff·문서/archive 최종 내용 검사는 rebase 시작 전과 완료 후에 적용한다. commit 재생·일부 충돌 stage·autostash 복원 중간 형상은 최종 위반으로 차단하지 않으며 현재 소유권·target·원격·경로/mode/index flags와 충돌 해결 Agent 전후 변경 검사는 유지한다. 단계가 바뀐 관측은 재관측하고, 중간 서비스의 archive 계약을 새로 구성해 완료 후보의 전체 변경·보호 원문·복원 결과를 검증한 뒤 후속 단계로 연결한다. 보조 Git 표식만으로 완료 상태를 pending으로 되돌리지 않는다.
- Git 완료 후에는 기존 `normalize_frontmatter()`가 고정 target base의 선택 ExecPlan 선언을 복원한 뒤 새 서비스·checkpoint generation·완료 HEAD로 최종 검사와 저장 Worker 입력 판정을 이어간다. 복원 전후 본문·mode·문서 채택 이력·다른 파일 상태와 중단 복구 근거를 유지한다. 복원이 만든 commit·OID 변화만으로 검증된 Worker 결과를 버리거나 복원 자체로 과거 READY·verify·Review를 승인하지 않는다. 정상 Worker의 frontmatter 부분 복원과 다른 문서의 파일/index 복원은 별도 경계로 유지한다.
- Requirement 이름 변경의 공식 `sources` 교정은 게시 확인·catalog/DB 반영 뒤 실행 입력으로 사용한다. 정확한 활성 경로·동일 파일명 archive·고유 R ID의 기존 조회 순서를 지키며, archive로 해석되는 source를 같은 ID의 활성 문서로 바꾸거나 활성/archive의 모호함을 무시하지 않는다. 고유 R ID의 조회 호환을 선언 일치나 저장 교정 결과의 입력 증명으로 대신하지 않는다. Worker가 코드 충돌과 관련 targeted 검증을 마치고 확인된 source 파일명 차이만 남으면 선언을 수정하지 않고 `ready_for_validation`으로 인계한다. 실제 source 누락·모호함·미해결 구현이나 사용자 판단은 그대로 보고하며 인계 응답을 Manager 검증 성공으로 취급하지 않는다. 진행 중 operation은 원래 target 선언으로 완료·복구한 뒤 더 최신인 공식 선언과 남은 조건을 반영하며 새 source 경로를 얻기 위해 target을 바꾸지 않는다.
- 이 입력 불일치만으로 이미 차단된 작업은 현재 소유 operation·조건·남은 교정을 재평가한다. 안전한 경로가 있으면 해당 원인만 해제하고 기존 자원과 session을 이어간다. 실제 결과 오류·sandbox 정책 실패·현재 대상/문서 유효성 불확실·필수 사용자 결정·미충족 dependency·진전 없는 반복은 기존 경계에서 유지한다. 폐지된 Git snapshot 권한 비교를 재활성화하거나 무한 교정을 만들지 않는다.
- Planner 게시·문서 적용·Worker 결과 저장·Git 계속 진행·차단 해제 사이 중단과 추가 원격 전진에서도 기존 target·원형·실제 확정 지점을 보존한다. 완료한 소유 operation 뒤 최신 base 반영을 이어가고, 최신 조건의 targeted test·필요한 Manager verify·최종 후보 Review와 게시 안전 조건 없이 구현 완료나 PR 게시를 승인하지 않는다.

## Implementation

구현 경로는 `loop_manager_internal/` 아래다. 보존된 작업 공간 `/home/jeong/.local/state/loop-manager/worktrees/ae1e641e09ef3149/G-256`의 `7212411af28c6ad9caf4b00e159adc29f04da966`에는 nullable `rebase_corrections.input_snapshot`, `_guard_rebase_worker_result()`, pending 소비 통합, 다음 충돌의 이전 READY 제거와 `retain_clean_rebase_worker()`의 완료 definition 연결이 있다. 이를 시작점으로 삼고 후보의 Progress·구현 결과·targeted 실행·Manager 진단·Review 교정 원문을 유지한다. 이 planning 파일의 개정을 후보 본문의 기존 수행 기록을 덮어쓰는 근거로 사용하지 않는다.

- [x] `execution/execution_worker_handler.py:WorkerHandler.run()`에서 최신 base의 `finish_turn()` 선저장 순서와 후보의 `input_snapshot=turn_definition...` 저장을 함께 유지한다. 통합 후보는 범위 검사 전에 응답과 실제 호출 입력을 함께 저장한다. 실제 호출 전에 고정한 definition을 응답에 결합하고, 저장 뒤 검사 오류·중단에서도 같은 결과를 재수집·소비하며 입력 부재를 현재 definition으로 채우지 않는다. `storage/rebase_correction_state.py`의 기존 nullable 호환·CAS와 결과 이력을 재사용한다.
- [x] `agents/scope_adapter.py`의 소유된 중간 rebase 검사 유예, `execution/execution_revision_handler.py`의 `PLAN_INPUT_INVALID` 분류와 후보의 저장 결과 guard를 통합한다. 구조·operation·원격 소유권·index 안전 조건과 충돌 해결 Agent의 직접 변경 검사를 유지하고 완료 전의 선언을 최종 후보로 판정하지 않는다. `_resume_saved_rebase_worker()`와 pending stage/continue도 실제 당시 입력·현재 조건을 대조하며 동일 입력에는 후속 단계부터 이어간다.
- [x] `git/rebase_documents.py:restore_rebase_frontmatter()`·`ManagedRebaseService.normalize_frontmatter()`와 최신 `execution_rebase_handler.py`·`execution_workspace_handler.py`의 호출을 재사용한다. 후보의 `_post_rebase_observation()`·`_complete_rebase_intent()`·Workspace 재개가 복원 뒤 갱신한 checkpoint·서비스·HEAD를 사용하도록 연결하고, 실제 완료 definition과 Worker 결과만 새 후보에 결합한다. 원문/파일 적용·index stage·commit 복원 엔진이나 허용 선언 비교를 재구현하지 않는다.
- [x] `git/direct_rebase_service.py`와 `recovery/blocked_plan_service.py`·`project_block_service.py`·`scope_reconciliation.py`의 직접 구현된 정규화·소유 rebase 우선 복구를 보존한다. `storage/recovery_state.py:RecoveryRepository.resolve_rebase_block()`의 확인된 입력 오류 원인 해소와 worktree 복구 실패의 `preserve_cause=True`도 재사용한다. 현재 후보와 합칠 때 `plan_input_invalid`를 일반 branch 복구로 되돌리거나 같은 응답의 교정 횟수를 다시 세지 않는다. 기존 차단 서비스의 원인별 해제·dependency·독립 원인 보존을 사용하고 저장 결과 연결의 실제 누락만 보완한다. 과거 Worker 교정 분류·진단 문구 차이로 해소된 입력 오류를 재차단하지 않고 원래 detail·실패 감사를 유지한다.
- [x] `catalog/requirement_sources.py:reconcile_execplan_sources()`와 `requirement_correction_publication.py`의 공식 source 교정을 재사용해 최신 선언을 소비한다. 이미 반영된 정확한 archive 우선·활성/archive 모호함 거부와 `agents/agent_prompts.py:worker_prompt()`의 source 파일명 차이만 남은 충돌 해결 인계를 유지하며 그 기능을 재구현하지 않는다. Manager가 공식 교정한 이번 계획의 R-072·R-055 현행 source 경로를 그대로 소비하며, Worker는 선언을 변경하지 않는다. `execution/stage_documents.py:refresh()`의 legacy 판단·게시 복구와 G-242의 저장 결과/legacy PR 검증을 유지하며 작업별 Planner·새 `document_revision` 판단을 만들지 않는다.
- [x] G-255의 `rebase_documents` pinned revision·교체 전 bytes·중간 HEAD·replay별 이력과 일반 문서 채택의 최종 HEAD/index/파일 대조를 유지한다. `resolve(failed_files=...)`·`git/rebase_file_evidence.py`·`rebase_files`의 복구 불가 파일 재작업은 별도 모드로 재사용하며 확보 가능한 자료·실패 이유·다른 파일·target·session을 보존한다. 과거 원형의 완전 보존을 재작업 진입 gate로 추가하지 않는다. PRODUCT·ARCHITECTURE의 실제 구현 설명은 최신 base와 후보를 통합하고 새 상태·재시도 엔진·비호환 schema는 추가하지 않는다. NO_CHANGE·다른 Git 차단·cleanup의 전수 감사는 G-189에 남긴다.

## Validation

- `tests/test_execution_revision_gate.py`, `tests/test_stage_documents.py`, `tests/test_execution_rebase_correction_phase.py`·`tests/test_planning_selection.py`의 최소 targeted unit으로 operation이 없는 최초/재개/교정의 프로젝트 planning 게시·DB 적용→ExecPlan 적용→Worker 순서를 확인한다. 진행 중 rebase는 기존 target 완료/복구→추가 planning·최신 base 반영 순서이며 중첩 operation이 없어야 한다. 문서-only의 불필요한 Worker 억제와 그 외 파일/혼합 충돌의 단계별 복귀를 실제 handler·SQLite로 확인하고 Git/Agent 관측만 mock한다.
- 저장 Worker READY 뒤 source/본문 조건만 변경되고 과거 Agent marker는 같은 경우, definition 원문 부재, pending stage/continue 재시작과 동일 입력 완료 결과를 대조한다. 현재 구현 입력은 유효하고 후보 OID만 바뀐 경우도 분리해 불필요한 Worker·오래된 성공·게시를 막는다.
- 후보의 `tests/test_execution_rebase_correction_phase.py:SavedRebaseInputTests`·`tests/test_rebase_correction_state.py`와 최신 base의 `tests/test_execution_worker_phase.py:test_rebase_response_is_saved_before_metadata_failure_without_worker_retry`를 연결한다. 검사 전에 저장된 응답에 실제 호출 입력이 함께 남고, metadata 오류·저장 후 재시작에서도 동일 session·실패 기록을 보존하며 Worker를 중복 호출하지 않아야 한다. 입력 근거가 없는 legacy READY는 후보의 기존 같은 session 재확인 회귀로 계속 검사한다.
- 최신 base의 `tests/test_execution_current_rebase_adoption_phase.py:test_worker_end_defers_intermediate_metadata_and_scope_but_rechecks_remote`와 `tests/test_scope_block_revalidation.py`의 `plan_input_invalid` 두 직접 회귀를 유지한다. 후자는 과거 `WORKER_CORRECTION` 분류·다른 진단 문구가 남아도 해소된 원인만 `reconcile`로 전환하고 원래 detail을 보존하는 assertion까지 포함한다. 후보의 완료 definition 설치·다음 dispatch·다음 충돌/continue 직후 재시작 회귀와 조합해 검사 시점 유예가 최종 금지 변경·원격 경쟁·독립 원인 검사를 건너뛰지 않고 이전 READY나 실패 횟수를 다음 충돌에 재적용하지 않는지 최소 mock/SQLite unit으로 확인한다. worktree 복구 실패의 진단 갱신도 원래 차단 원인·evidence를 바꾸지 않아야 한다.
- `tests/test_rebase_definition_boundary.py`의 기존 실제 Git 회귀는 복원 commit·index·본문 보존·commit 직후 중단 재개의 효과를 계속 확인한다. Worker는 실제 Git lifecycle을 새로 반복하지 않고 Manager 검증에 남긴다. 파일 교체·stage 전후·완료 저장 전 중단에서 후보의 입력/history·문서 채택 및 `rebase_frontmatter` 근거가 함께 유지되는지 기존 관측 fixture의 최소 unit을 보강한다. 복원 후 새 HEAD에는 검증된 Worker 결과만 연결되고 필요한 verify/Review 근거는 별도로 판단해야 한다.
- `tests/test_requirement_correction_publication.py`의 rename source·CRLF 본문·archive 보존 및 모호한 후보 거부와 `tests/test_requirement_sources.py:RequirementSourceResolutionTest`의 정확한 archive 유지·활성/archive ID 모호함 거부 회귀를 재사용한다. 같은 R ID의 공식 source 경로만 바뀐 경우에도 원격 선언·catalog 반영과 고정 rebase target을 구분하고, 조회 fallback만으로 metadata 일치나 저장 입력 성공을 합성하지 않는 소비 경계를 확인한다. `tests/test_loop_manager.py:PublicFacadeTest.test_worker_and_reviewer_prompts_define_test_execution_boundary`의 이미 구현된 인계 안내를 유지하되 문구 검사만으로 실제 결과 저장·소비의 성공을 대신하지 않는다. 확인된 이름 차이만 남은 READY는 기존 입력 guard·완료 검증으로 이어지고 실제 누락·모호함·미해결 사용자 판단은 그 인계로 우회되지 않아야 한다. source 교정이나 prompt 기능의 통합 lifecycle을 추가로 복제하지 않는다.
- 위 저장 결과/재시작 조합의 검사 시점은 `tests/test_managed_rebase_validation.py`·`test_managed_mutation_evidence.py`·`test_worker_conflict_scope.py`의 직접 회귀를 재사용한다. replay·일부 stage·autostash 중 내용 유예와 구조/소유권 거부를 구분하고, 완료 관측에는 새 archive 계약·보호 원문·최종 금지 변경 검사가 적용되는지 확인한다. 이중 관측 중 단계 변경은 현재 상태 재관측으로 이어지고 기존 결과를 성공이나 범위 위반으로 확정하지 않아야 한다. 실제 누락된 저장 결과 소비의 최소 unit만 보강하고 일반 rebase·cleanup 전체 조합을 반복 실행하지 않는다.
- 위 조건에서 호출 역할도 확인한다. ExecPlan·코드 변경만으로 일반·점검·문서 영향 Planner는 호출되지 않아야 하며, 미확인 새 조건은 기존 Worker 세션에서 필요한 교정과 함께 한 번 처리돼야 한다. 수행 기록만의 변경·이미 충족한 조건과 미확인 조건을 구분하고 선행 Review·최종 verify/Review gate는 보존한다.
- `tests/test_rebase_correction.py`의 채택 이력·후속 uncertain·최종 원문 대조와 기존 lifecycle fixture를 재사용해 문서 채택과 `document_revision` 판단이 함께 있는 중단/재개를 확인한다. Planner 게시나 입력 무효화가 pinned revision·원형·기존 target을 덮어쓰지 않으며, 뒤늦은 비충돌 재생으로 달라진 문서는 완료로 채택하지 않는다. 동일 원문 적용 통합 경로를 불필요하게 반복하지 않는다.
- `tests/test_task_remote_inputs.py`·`tests/test_rebase_file_evidence.py`의 직접 구현된 파일별 복원·새 사용자 교정 보존 회귀를 재사용한다. 저장 Worker 결과/pending continue를 복원 전후의 후보·실제 최신 조건에 연결하는 unit을 보강하며 일반 문서 채택 완료와 재작업 완료를 혼동하지 않는다. 파일 삭제/이동·일부 원형 부재·중단 뒤 새 교정·나머지 파일 보존은 현재 intent 경계에서 검사한다.
- `tests/test_managed_rebase_lifecycle.py`, `tests/test_scope_block_revalidation.py`와 현재 rebase 수용 회귀에서 동일 입력 재평가·원인별 해제 및 실제 sandbox/결과 오류·독립 차단·사용자 operation·dependency 대기를 확인한다. 과거 ref/config/snapshot 차이를 접근 위반으로 복원하지 않고 실제 target·index·작업물 효과는 기존 통합 fixture를 재사용한다.
- 이 작업의 회귀 조건은 원격 재전진·PR/소유권 경쟁에서도 session/실패/level·반복 한도·정확한 lease를 보존하고, 새 조건 미검증의 PASS 채택·다른 원인 해제·target 덮어쓰기·중첩 rebase를 거부하는 것이다. 후보에 기록된 targeted 결과·부분 verify 실패/교정·Review 교정을 최신 통합 후보의 성공으로 소급하지 않는다.

## Progress

- [x] 2026-10-10 `ab0264d94da1212d68141b28fc55f0204c1c9e09`의 delta와 관련 handler·저장 교정/차단 판정 및 테스트를 정적으로 확인했다. 구현·테스트·전체 verify를 실행하지 않았다.
- [x] 2026-10-13 `f56739c31a49ec8fb5547a459688bfb02772586e`의 G-255 원문 적용·재개·최종 대조와 현재 `stage_documents.refresh`·저장 Worker 결과 소비를 정적으로 확인했다. 전자는 현재 구현을 재사용하고 후자의 최신 조건 연결은 여전히 남아 있다. 기존 수행 기록과 미완료 항목을 보존하며 구현·테스트·verify는 실행하지 않았다.
- [x] `8e1fbe0f0a449783e9d4a245f5011a6d5989efbc` delta와 보존된 `7212411af28c6ad9caf4b00e159adc29f04da966` 후보의 코드·본문 기록을 읽기 전용으로 대조했다. 후보에는 저장 입력·결과 수명·완료 definition 구현과 targeted/교정 기록이 있으며, 최신 base의 응답 선저장·정규화와의 통합은 아직 확인하지 않았다. 후보나 실행 이력은 수정하지 않았고 검증을 실행하지 않았다.
- [x] 재평가 revision `e788b2cdb347270915220cea2336b9cbbd7e34c4`에서 추가된 7개 코드·테스트 파일을 이전 검토와 대조했다. Worker 인계 안내·archive 조회 우선순위·입력 오류의 복구 원인 보존은 이미 직접 구현돼 있어 재구현 작업을 만들지 않았다. 보존 후보의 HEAD와 clean 상태·구현/교정 기록이 유지됨을 읽기 전용으로 확인했으며 최신 통합 후보의 검증 성공은 확정하지 않았다.
- [x] 기존 후보 구현·수행 기록을 보존한 최신 base 통합과 위 추가 조건의 최소 targeted unit 검증 (아래 현재 후보 검증 기록 참조)
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 이전 개정안은 `8e1fbe0f0a449783e9d4a245f5011a6d5989efbc` 이후 코드·테스트 전진으로 게시 전에 재평가됐다. 현재 권한은 snapshot token `cc7c106f45811013027271b13e42ed4bc13862f2f5a0f3ca2e97b41ca76fecfa`, revision `e788b2cdb347270915220cea2336b9cbbd7e34c4`, 원본 hash `a468f2cf92cf7f0585f1bd7ee9d55ae2f15260e1593b1b1892a7343d784659f6`에 결합된 PR 전 G-256이다. 명세·기존 원문은 같고 추가 7개 파일의 실제 변경을 재검토해 보존할 통합 조건에 반영했다. ID·경로·dependency·분류·기존 기록과 R-072·R-055의 유일한 활성 source 관계를 유지한다. 직접 구현에 소급 계획을 만들지 않으며 후보의 구현·실패·교정·미실행 통합 검증 기록은 그 작업 공간의 원문으로 보존한다. G-277의 진단 ref 삭제와 G-284의 다른 문서 파일/index 복원은 이 frontmatter 복원으로 대체하거나 권한을 확대하지 않는다. 기존 high complexity/risk는 저장 입력·정규화 commit·checkpoint generation·완료 definition의 확정 지점 연결과 오래된 READY/잘못된 원인 해제에 따른 게시 위험에 계속 적용한다. unittest version 1·launcher Python 3.11–3.14·독립 tests 수집·필수 compile은 유지하며 실제 검증 성공과 프로젝트 최종 전문 확인을 기록하지 않는다.

- 최신 R-055·ARCHITECTURE의 검사 시점 변경과 `94ae3d1fc12b11d8d02d220bb91aaf633cc99e4f`의 rebase/runtime/scope 직접 구현·회귀 원본을 대조했다. 중간 replay·autostash 유예와 완료 검증 자체는 재구현하지 않고 이 계획의 남은 저장 결과/continue 연결에 그 계약을 통합한다. G-189는 정상·복구·cleanup의 후속 전수 감사를 담당한다. snapshot token `6e4fb916f06c1c1469efa63d153f96a3890ee168b792392fe50d15d977ae9d72`의 원본 hash `81be4060a95b00c9661395ff833ea4ac5e1e306a43e4d7b69bbe1275f0c88b55`를 확인했고 ID·경로·metadata·기존 수행 기록과 high complexity/risk의 중단/재개·오래된 결과 채택 위험을 보존했다. 검증 adapter는 기존 unittest version 1·launcher Python 3.11–3.14·독립 수집과 필수 compile을 유지한다. 구현·테스트·verify·사용자 결정은 수행하지 않았고 G-246 후보를 수정하지 않았다.

- 이번 delta의 원격 입력 통합·OID만 변경된 후보의 verify 선택·복구 불가 파일 복원은 이미 직접 구현됐다. 남은 최신 조건·저장 교정 결과/pending continue 연결에서 그 구현을 재사용한다. 일반 문서 원문 채택과 파일 재작업의 보존 조건을 구분하고 기존 목표·metadata·source 문자열·실행 기록을 유지한다. 아래 과거 strict scope·원문 비교 기록을 폐기된 권한 검사나 복원 전 파일 원형 완전 보존의 새 gate로 사용하지 않는다.
- 2026-10-14 delta 개정: 원격 R-082의 프로젝트 planning·작업 실행 대응 분리와 원래 소유 operation 우선 복구를 재사용하도록 현재 지시를 갱신했다. 직접 반영된 source 지문만의 Worker token 변화 제외는 일반 planning 완료를 전제로 하며 verify·Review 입력의 제외가 아니다. 저장된 rebase 교정 결과/차단의 최신 조건 연결이라는 남은 목표, 기존 분류·dependency·실행 기록을 유지한다. 구현·테스트·verify는 실행하지 않았다.

- 2026-10-13 delta 개정: mutable snapshot token `bbe557975a5581bd2185b8b767a35088a476de189a346cb28f0469aca8c109f7`, revision `f56739c31a49ec8fb5547a459688bfb02772586e`, 원본 hash `f13365260f2bb3c5cdc4f18ad1f7a635f2393ada7a2157bc2a75af0d884fc12a`와 일치하는 이 계획만 개정했다. G-255의 동일 ID archive 이동은 삭제 목표가 아니므로 dependency를 유지한다. ID·경로·metadata·목표·기록을 보존하고 새 원문 보존 경계의 재사용·회귀 연결만 구체화했다. 기존 high complexity/risk의 판단과 복구 조건을 유지하며 프로젝트 최종 전문 확인이나 구현 성공을 기록하지 않는다.

- 현재 `RevisionHandler.guard`는 충돌 중 입력 변경/과거 원문 부재에서 runtime 성공 token을 비우고 기존 session·rebase target을 보존한다. 해당 직접 구현과 회귀를 재사용한다. 최신 `stage_documents.refresh`는 저장된 legacy 결과·게시만 복구하고 새 판단을 호출하지 않는다. `_resume_saved_rebase_worker`의 저장 교정 결과는 최신 조건과 결합해 소비하고, 근거가 부족하면 같은 Worker 세션의 교정으로 연결한다.
- G-242의 남은 저장 DOCUMENT_RESULT 형식·legacy PR 경합, G-243의 PR 전 snapshot 수동 소유 보호, G-245의 일반 Planner lifecycle, G-183/G-241의 성공 입력 증명과 책임을 나눈다. snapshot 밖 G-242는 수정하지 않으며 그 과거 새 호출 설명을 이 작업의 실행 지시로 사용하지 않는다.
- 2026-10-15 delta: 원격 R-082의 호출 축소와 직접 반영된 `stage_documents`·선택·회귀를 대조해 위 잔여 연결을 Worker 책임으로 정합화했다. ID·경로·dependency와 이전 수행 기록은 보존하고 구현·테스트·verify를 실행하지 않았다.
- complexity high: 문서 갱신·진행 중 Git operation·저장 Worker 결과·phase·원인별 차단의 서로 다른 확정 지점을 중단/재개에도 맞춰야 한다. risk high: 오래된 READY 소비나 잘못된 원인 해제는 최신 조건 미충족 후보의 검증·게시 또는 사용자 작업 충돌을 만들 수 있다. 원형·입력·turn·checkpoint·실패 감사를 남겨 수동 재평가·복구할 수 있어야 하며 이미 발생한 게시를 코드 rollback으로 되돌린다고 가정하지 않는다. 관련 경합·재개·무근거 성공 거부를 AC·Validation·Review에 포함한다.


## 보존 후보의 수행 원문

아래는 `7212411af28c6ad9caf4b00e159adc29f04da966` 후보의 기존 수행·실패·교정 기록이다. 현재 rebase 재생 중인 형상의 검증 성공으로 소급하지 않는다.

## 구현 결과와 검증 근거 (2026-10-16)

저장된 Worker READY와 pending stage/continue는 실제 교정 호출의 definition snapshot을 최신 Worker·archive token과 대조한 뒤에만 재사용한다. 호출 중 조건 변경도 종료 후 definition으로 성공을 덮어쓰지 않는다. nullable `rebase_corrections.input_snapshot`은 기존 DB·과거 결과를 보존해 추가하며, 입력 근거가 없는 과거 READY는 같은 session의 재확인으로 연결한다. 새 관측이나 turn에 과거 입력 근거가 따라붙지 않도록 결과와 함께 수명을 관리한다.

입력 불일치에서는 checkpoint history에 이전 입력·결과·turn·pending·stage 근거를 보존한 뒤 해당 READY/pending 승인과 성공 token만 비운다. 과거 결과는 Worker 피드백에도 남기고 파일·session·실패/level 이력·원래 target·문서 채택/파일 복원 intent를 유지한다. 중단 시 transaction rollback 뒤 같은 저장 결과에서 재평가한다. pending의 별도 stage/continue 소비를 기존 Worker 완료 경로로 통합했고, 완료 시 새 현재 상태 서비스의 최종 검증/adoption 뒤 검증된 Worker 근거를 완료 HEAD에 연결한다. 필요한 verify·Review·publication 성공은 계속 비운다. OID/source 지문만의 변화와 순수 수행 기록은 기존 Worker 입력 규칙을 재사용한다.

일반 planning의 선행 gate, 진행 중 operation 우선 처리, 문서-only 자동 진행·최종 원문 대조, 파일별 재작업 intent와 원인별 차단 해제는 현재 구현을 재사용했다. 작업별 Planner나 새 상태·재시도 엔진을 추가하지 않았다. PRODUCT의 복구 결과와 ARCHITECTURE의 nullable 저장 형식·입력 무효화·완료 후보 연결 계약을 갱신했다. `scripts/verify-inputs.json`의 기존 `loop_manager_internal/**`·`tests/**`가 변경된 실행 입력을 포함하므로 목록 변경은 필요하지 않았다. 문서 prose는 기존 verify 입력 계약에 따라 제외된 상태이며 외부 runtime 입력의 완전성을 주장하지 않는다.

source 조회에서 R-072는 `docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md`, R-055는 `docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md`의 고유 ID로 확인했다. 계획의 과거 영어 제목 source 문자열은 그대로 보존했으며 공식 선언 교정·DB 반영은 Planner/Manager 소관이다. Requirement·다른 ExecPlan·PLANS와 선택 계획의 frontmatter는 수정하지 않았다.

### Targeted 실행 결과

검증 환경은 Linux Python 3.12, `scripts/verify.json`의 unittest adapter version 1·launcher Python이며 지원 범위는 3.11–3.14다. 모든 명령은 제공된 `LOOP_MANAGER_TARGETED_TEST_RUNNER`로 실행해 `LOOP_MANAGER_TARGETED_TEST_DIRECTORY`에 결과를 남겼다.

- 저장 입력 회귀 `SavedRebaseInputTests`의 최종 12 case: 입력/기록/OID 구분, 선언·archive·지침 변화, definition 부재, pending 재시작·rollback, 호출 도중 조건 변경과 실제 현재 상태 서비스의 완료 Worker/verify 전이를 확인했다.
- 기존 `ExecutionRebaseCorrectionPhaseTests` 34 case: 최종 재실행 PASS. 입력 근거가 있는 pending fixture를 추가하고 유효한 완료 교정을 IMPLEMENTING으로 되돌리던 assertion을 VERIFYING·Worker token 보존·verify/Review token 무효화 검사로 교체했다. 기존 stage 경로·허용 파일·실패·session assertion은 유지했다. 이 클래스에 기존 실제 Git fixture를 쓰는 legacy 복구 case도 포함되어 있었으며 별도 전체/integration suite를 실행한 것은 아니다.
- `tests.test_rebase_correction_state` 6 case: PASS. 구형 DB의 nullable 필드 추가·결과 보존·재시작·CAS와 새 관측의 과거 입력 근거 제거를 확인했다.
- 저장 legacy READY·새 outcome 누락·durable continue의 개별 3 case: 최종 재실행 PASS. 최신 metadata를 결과 parser에 전달하는지 확인했다.
- 원래 본문 부재/기록 비교·충돌 입력 변경·dependency 복구의 기존 revision case 4개, 중간 replay/autostash의 내용 유예·구조/소유권 유지 2개, Worker 직접 문서 delta 검사 1개: 최종 실행 PASS.
- 기존 차단의 입력 변경 재평가·dependency 대기·독립 원인 유지 3개와 상위 명세의 작업별 Planner 미호출 1개: PASS.
- 새 테스트 중 archive·guidance 결합 검사는 필드별 선언 변경 회귀가 같은 결과·보존 assertion을 보장하므로 중복 추가를 제거했다. 기존 회귀를 실패/지연을 이유로 삭제·skip하거나 assertion을 약화하지 않았다.

실패한 실행은 아래 동일 명령으로 수정 후 재실행했다. 처음 두 `--unittest` 옵션을 반복한 호출은 runner가 마지막 모듈만 선택해 저장소 회귀 6개만 실행했으므로 새 입력 회귀의 실행 근거로 계산하지 않았다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests

OID fixture가 원래 REBASE_HEAD까지 바꾸던 구성을 고정해 원격 OID 변화와 replay 위치 변화를 분리했다. 동일 명령 재실행 9 case PASS.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests

기존 pending fixture에 실제 입력 근거를 추가하고 새 VERIFYING/Worker 보존 계약을 검사했다. 두 차례 실패 뒤 동일 명령 최종 재실행 34 case PASS.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests tests.test_execution_failure_policy.ExecutionFailurePolicyTests.test_saved_worker_result_finishes_after_restart_without_second_agent_turn tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_saved_new_rebase_result_requires_outcome_before_finishing
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests tests.test_execution_failure_policy.ExecutionFailurePolicyTests.test_saved_worker_result_finishes_after_restart_without_second_agent_turn tests.test_execution_checkpoint_recovery_phase.ExecutionCheckpointRecoveryPhaseTests.test_saved_new_rebase_result_requires_outcome_before_finishing

새 필드별 회귀의 context와 재생 marker를 각 turn에 갱신하고 legacy parser가 받는 최신 metadata의 list 계약을 명시했다. 두 차례 실패 뒤 동일 명령 최종 재실행 14 case PASS.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_owned_completed_service_retains_worker_but_requires_new_verify_and_review tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflicted_rebase_without_original_body_invalidates_success_for_worker tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflicted_rebase_changed_inputs_clear_success_without_losing_recovery tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflicted_rebase_uses_original_legacy_body_for_record_comparison tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflict_revision_dependency_merge_resumes_worker tests.test_managed_rebase_validation.CurrentRebasePolicyTests.test_intermediate_replay_defers_content_but_keeps_structural_and_ownership_checks tests.test_managed_rebase_validation.CurrentRebasePolicyTests.test_autostash_content_waits_for_restoration_but_auxiliary_markers_do_not tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests.test_worker_delta_preserves_manager_replay_documents_but_rejects_new_edits
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_owned_completed_service_retains_worker_but_requires_new_verify_and_review tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflicted_rebase_without_original_body_invalidates_success_for_worker tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflicted_rebase_changed_inputs_clear_success_without_losing_recovery tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflicted_rebase_uses_original_legacy_body_for_record_comparison tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflict_revision_dependency_merge_resumes_worker tests.test_managed_rebase_validation.CurrentRebasePolicyTests.test_intermediate_replay_defers_content_but_keeps_structural_and_ownership_checks tests.test_managed_rebase_validation.CurrentRebasePolicyTests.test_autostash_content_waits_for_restoration_but_auxiliary_markers_do_not tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests.test_worker_delta_preserves_manager_replay_documents_but_rejects_new_edits

완료 서비스 unit fixture를 resolving_rebase context로 진입하도록 수정했다. 동일 명령 최종 재실행 8 case PASS. verify/Review 로그는 mock 효과이며 실제 전체 verify 성공이 아니다.

최종 실행에 미해결 실패는 없다. 전체 suite·실제 전체 verify·프로젝트 lint/build/type check·`scripts/verify`와 실제 Agent/API 호출은 실행하지 않았다. 작업 저장소의 commit·fetch·rebase·push·PR 생성이나 Manager 운영 SQLite 변경은 수행하지 않았다. 기존 클래스의 실제 Git fixture 실행은 위에 별도 명시했다. Manager 전체 verify·읽기 전용 Review·dependency 최종 확인과 사용자 Merge는 남아 있다.


### Worker 완료 보고 교정 (2026-10-16)

이번 재진입의 `TargetedTestFailure`는 테스트 실행 결과가 아니라 이전 최종 보고의 `TARGETED_TESTS` 안에 있던 “실패 명령·수정·동일 명령 재실행 결과는 선택된 ExecPlan 본문에 기록”이라는 설명에서 발생했다. 실제 보고 parser로 해당 문장이 미해결 실패 표현으로 잡히는 것을 확인했다. 이전 호출의 JSONL을 읽기 전용으로 대조한 결과 기록된 67개 test ID의 마지막 결과는 모두 success였다. 이 수에는 이후 중복으로 제거한 새 테스트 1개가 포함되므로 현재 테스트의 66개와 구분한다. 과거 실행 결과와 FAILED_COMMAND/RERUN 기록은 보존했다.

기존 구현·판정 계약을 변경하거나 성공한 구현 회귀를 반복하지 않고, 현재 교정은 최종 보고와 이 본문 기록에 한정했다. 신규 테스트를 추가할 실행 동작 변경이 없으며 기존 보고 계약의 명시적 재실행·한국어 성공/부정 표현 회귀가 이 조건을 검사한다. 제공된 unittest version 1 runner로 아래 개별 2개만 실행했다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_worker_result_contract.WorkerResultContractTest.test_two_corrected_runs_keep_initial_outcomes_and_clean_final_results tests.test_worker_result_contract.WorkerResultContractTest.test_korean_inflected_test_success_is_recognized_without_accepting_negation
```

결과: PASS (2 tests). 최종 보고의 TARGETED_TESTS에는 위 실행 명령과 확정 결과만 보고하며, 이전 교정의 상세 실패 이력은 위 소유 기록에서 확인한다. 이번 호출에서는 새 테스트 실행 실패가 없었다. 전체 verify·integration suite·Git 쓰기·Manager 운영 상태 변경은 수행하지 않았다. Manager 전체 verify·Review·사용자 Merge는 계속 남아 있다.


### Manager verify 진단 반영 (2026-10-16, verify revision 1)

Manager의 `/home/jeong/.local/state/loop-manager/logs/verify/9e19ac82913fa304d4b49ecd085a575a/summary.json`과 해당 worker 로그를 읽어 생략된 진단까지 확인했다. 이 실행은 partial verify이며 failure 8개·error 4개를 기록했다. Worker가 전체 verify나 integration을 다시 실행하지 않았다.

원인은 새 입력 검증을 우회한 직접 Worker fixture 호출과 폐기된 완료 단계 기대값이었다. 실제 coordinator는 Worker 전에 revision gate를 실행한다. 입력 snapshot 없이 `handler.run()`만 호출하던 unit fixture와 lifecycle의 첫/재시작 교정 호출을 그 순서에 맞췄다. 운영 Worker의 근거 부재 거부를 완화하거나 최신 definition으로 과거 결과를 승인하지 않았다.

유효한 교정은 바로 VERIFYING으로 이어지므로 priority/profile 회귀는 Worker 결과·입력 token 보존, verify/Review token 부재와 Worker 1회 호출을 검사하도록 바꿨다. session·원래 metadata 검증·level 단조 유지·실패/교정 집계 assertion은 유지했다. verify 오류 복귀 회귀에서 완료 직후 다시 실행하던 Worker 1회를 제거하고, 정상 완료와 verify 실패 후 같은 Worker 복귀를 계속 검사한다. 근거 없는 저장 결과의 재확인은 기존 `SavedRebaseInputTests.test_saved_ready_without_definition_requires_same_session_reassessment`가 계속 담당한다.

legacy lifecycle fixture의 `rebase_*` journal 필터에서 현재 Worker 입력 근거 `rebase_worker_input`을 보존했다. 이 근거는 구형 Git journal 형식과 별개다. 검증된 자동 교정은 재개 후 VERIFYING·기존 Worker 결과를 유지하고, 수동 완료·게시 경계 전용 후보·별도 누적 rebase처럼 교정 입력 증명이 없는 후보는 기존 Worker 확인을 요구한다. 실제 index·replay/skip·중단 위치·원격 lease·session·감사·verify/Review assertion은 유지했으며 skip하거나 제거하지 않았다.

이번 Worker는 아래 mock 기반 개별 unit 8개만 제공된 runner로 실행했다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_conflict_revision_priority_change_allows_old_workspace_metadata tests.test_execution_verify_phase.ExecutionVerifyPhaseTests.test_resolved_rebase_then_verify_failure_returns_to_same_worker tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_rebase_worker_result_format_error_stops_run_for_same_session_retry tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_targeted_rebase_failures_use_individual_execution_history_instead_of_report_count tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_conflict_profile_complexity_change_resumes_same_worker tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_conflict_profile_downgrade_preserves_escalated_level tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_conflict_profile_risk_change_resumes_same_worker tests.test_execution_publication_phase.ExecutionPublicationPhaseTests.test_owned_pr_correction_stages_only_publishable_resolution
```

결과: PASS (8 tests, Python 3.12/Linux, unittest adapter version 1). 이번 targeted 실행에는 실패가 없었다.

실제 Git integration 4개(`test_checkpoint_multiconflict_skip_restart_verify_review`, `test_pr_multiconflict_skip_restart_verify_review`, `test_legacy_checkpoint_restart_before_journal_seal`, `test_legacy_same_file_replay_restart_before_journal_seal`)의 fixture는 수정했지만 Worker의 integration 실행 금지에 따라 실행하지 않았다. Manager의 해당 재검증과 필요한 verify·Review는 미완료이며 통과로 보고하지 않는다. PRODUCT·ARCHITECTURE의 기존 보장과 schema/상태 전이 구현은 이번에 변경하지 않았다. `scripts/verify-inputs.json`의 `tests/**`가 이번 변경을 포함하므로 입력 목록 변경도 필요하지 않다.


### Review 교정: 완료 definition과 다음 충돌의 결과 수명 (2026-10-16)

`completed-definition-not-installed`는 adoption 뒤 검증된 definition을 runtime에 함께 저장해 해결했다. 공통 `retain_clean_rebase_worker`의 기존 transaction에서 definition snapshot·Worker/후보 token을 연결하며, 진행 중 operation에는 기존 definition·target 고정을 유지한다. 본문 A→B 교정 완료 뒤 다음 실제 dispatch가 Worker를 호출하지 않고 mock verify→REVIEWING으로 진행하는 unit으로 확인했다. verify·Review·게시 성공을 합성하지 않았다.

`stale-pending-on-next-conflict`는 새 unmerged index를 이전 continue 결과의 소비 경계로 처리했다. continue intent는 index 해소 뒤에만 생기므로 새 unmerged stage는 이전 READY의 적용 대상이 아니다. 공통 `_post_rebase_observation`이 이전 pending·결과·입력·stage 근거를 기존 history에 남기고 승인만 제거한다. Git 직후 중단된 pending도 Workspace recover에서 같은 post-command 관측으로 연결한다. 이후 `recover → guard → run`이 같은 session의 새 Worker turn을 시작하며 이전 READY로 실패 횟수를 증가시키거나 파일 복원에 진입하지 않는다.

새 mock/SQLite unit은 정상 continue와 Git 직후 중단의 두 경우에서 recover를 반복해도 correction occurrence가 0임을 확인하고, 다음 Worker 1회 호출·기존 target/HEAD·session·level 감사·과거 targeted 실패 1회 보존·파일 복원 미진입을 검사한다. 기존 stage/continue 잠금 및 부분 stage 재개 3개, 실제 완료 adoption 후 Worker 보존 1개와 공통 clean-rebase 후보/CAS 결합 1개도 최소 개별 unit으로 확인했다. 총 8개 각 최종 실행 PASS이며 실제 Git·Agent·전체 verify는 실행하지 않았다. mock verify의 scope=full 로그는 실제 전체 verify 실행을 뜻하지 않는다.

새 회귀의 최초 실행은 3개가 실패했다. P2 회귀 fixture가 진행 중 rebase context를 유지하도록 수정했고, P1의 실제 stale pending을 함께 교정한 뒤 다음 동일 명령으로 재실행했다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_completed_changed_definition_survives_next_dispatch_without_worker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_next_conflict_after_continue_reenters_worker_without_replaying_ready tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_restart_after_continue_reenters_worker_without_replaying_ready
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_completed_changed_definition_survives_next_dispatch_without_worker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_next_conflict_after_continue_reenters_worker_without_replaying_ready tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_restart_after_continue_reenters_worker_without_replaying_ready

결과: PASS (3 tests). 재실행에 미해결 실패는 없다.

나머지 실행 명령과 결과:

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests.test_stage_lock_resume_uses_checkpoint_without_new_worker_turn tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests.test_continue_lock_resume_does_not_repeat_stage tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests.test_pending_stage_with_unmerged_index_retries_only_accepted_paths tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_owned_completed_service_retains_worker_but_requires_new_verify_and_review
```

결과: PASS (4 tests).

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_verify_receipts.VerifyReceiptPolicyTest.test_clean_worker_binding_uses_the_accepted_head_and_checkpoint_cas
```

결과: PASS (1 test).

PRODUCT·ARCHITECTURE의 해당 결과/저장·소비 계약을 같은 변경에 반영했다. 기존 `scripts/verify-inputs.json`이 변경된 코드·테스트를 포함하므로 입력 목록은 유지한다. Requirement·다른 ExecPlan·선택 계획 frontmatter와 사용자 결정은 변경하지 않았다. Manager verify·Review 및 사용자 Merge는 계속 남아 있다.


## 최신 base rebase 1/3 충돌 교정 (2026-10-16)

고정 revision `ca05750ab3f3b100c28d7bc9f6c444de879d03b1`의 ExecPlan·PRODUCT와 R-082·R-072·R-055를 git show로 읽고 첫 재생 commit `26c8bcb`의 양쪽 코드를 대조했다. 선택 계획은 Manager가 적용한 최신 원문과 frontmatter를 유지하고 보존 후보의 수행 원문을 위에 복구했다. 현재 활성 R-072·R-055 경로는 고정 revision의 정확한 파일과 고유 R ID로 확인했다. Worker가 sources나 frontmatter를 교정하지 않았다.

코드 충돌은 최신 base의 응답 선저장 위치를 유지하고 그 `finish_turn()`에 실제 호출 전의 `turn_definition`을 `input_snapshot`으로 함께 저장하는 방식으로 해결했다. 범위 검사 뒤의 중복 저장 블록은 제거해 같은 generation/turn의 이중 확정을 막았다. 후보의 저장 입력 guard·nullable 호환·session/종료/결과 분류와 최신 base의 중간 선언 검사 유예·PLAN_INPUT_INVALID 처리를 보존했다. 범위 검사에서 나중 definition으로 바뀌더라도 저장 응답에는 원래 호출 입력이 남는지 기존 응답 선저장 회귀를 보강했다.

정규화 뒤 pending 재개는 기존 frontmatter 복원과 새 checkpoint/service 관측을 재사용한다. 새 unit은 복원으로 HEAD·checkpoint generation이 바뀐 뒤 갱신된 상태와 원래 교정 입력을 다음 완료 경계가 사용하는지 확인하며 실제 Git 복원 commit은 실행하지 않는다. 기존 중간 선언/범위 유예·원격 재확인, 입력 근거 부재, 호출 중 조건 변경 회귀도 개별 실행했다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_rebase_response_is_saved_before_metadata_failure_without_worker_retry tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_worker_end_defers_intermediate_metadata_and_scope_but_rechecks_remote
```

결과: PASS (2 tests).

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_pending_recovery_uses_checkpoint_after_frontmatter_normalization tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_saved_ready_without_definition_requires_same_session_reassessment tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_live_worker_ready_is_not_blessed_after_inputs_change_during_turn
```

결과: PASS (3 tests). runner는 unittest adapter v1·launcher Python 3.11–3.14 계약을 유지하며 실제 실행은 Python 3.12/Linux다. 첫 수집 시 기존 class 이름을 `CurrentRebaseAdoptionPhaseTests`로 잘못 지정해 수집만 중단됐고 테스트는 실행되지 않았다. 실제 `CurrentRebaseAdoptionTests` ID로 바로잡은 위 명령에서 2개가 통과했으며 수집 오류를 테스트 실패나 통과로 계산하지 않았다.

현재는 rebase 1/3의 파일 내용 충돌을 해결한 상태다. index staging·continue와 뒤의 기존 교정 commit 재생·Git 완료·고정 base frontmatter 복원·최종 검증은 Manager가 수행한다. 후속 commit에 있는 완료 definition 설치·다음 충돌 READY 수명 교정의 과거 검증을 현재 중간 후보의 성공으로 보고하지 않는다. 전체 suite·integration·전체 verify·commit·push·PR 생성과 운영 SQLite 변경은 수행하지 않았다. Requirement·다른 ExecPlan·PLANS와 사용자 결정은 변경하지 않았으며, 기존 verify 입력 목록이 변경 코드·테스트를 포함한다.


## 관리 rebase 완료 후보 검증 (2026-10-16)

현재 HEAD `0a056e6`와 고정 base `ca05750ab3f3b100c28d7bc9f6c444de879d03b1` 사이 전체 19개 파일 diff를 다시 검토했다. 최신 ExecPlan과 연결된 PRODUCT·R-082·R-072·R-055를 현재 파일로 확인했으며 sources의 정확한 현행 경로가 모두 존재한다. 관리 rebase 완료 상태를 수용하며 frontmatter·sources·Requirement·다른 ExecPlan·PLANS·사용자 결정은 수정하지 않았다.

응답 선저장에 호출 전 definition을 결합한 구현과 nullable 호환·저장 결과 guard·pending 소비 통합·완료 definition 설치·다음 충돌의 이전 READY 종료가 함께 유지된다. 최신 base의 중간 범위/선언 검사 유예·PLAN_INPUT_INVALID·정규화 뒤 checkpoint/service 재관측·원인 보존 복구·공식 source 교정은 기존 구현을 사용한다. 이번 재검토에서는 추가 코드/테스트 보완이 필요하지 않았고 기존 변경 테스트를 현재 후보에서 직접 실행했다. 계획 본문에서 누락된 보존 후보의 수행/진단/교정 기록은 첫 재생 commit `2d4dda1`의 원문으로 복구했으며 위의 rebase 1/3 기록은 당시 중간 상태의 이력이다. 그 과거 결과는 현재 후보의 성공 근거가 아니다.

현재 후보의 targeted 명령은 아래와 같다. 각 항목은 mock Git/Agent와 격리된 SQLite를 사용하는 개별 unit이며 실제 Git lifecycle은 실행하지 않았다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_rebase_response_is_saved_before_metadata_failure_without_worker_retry tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_worker_end_defers_intermediate_metadata_and_scope_but_rechecks_remote tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_pending_recovery_uses_checkpoint_after_frontmatter_normalization tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_saved_ready_without_definition_requires_same_session_reassessment tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_saved_ready_rechecks_changed_body_with_identical_file_marker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_saved_ready_accepts_oid_and_source_fingerprint_only_changes tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_pending_continue_rechecks_inputs_after_restart tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_live_worker_ready_is_not_blessed_after_inputs_change_during_turn tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_completed_changed_definition_survives_next_dispatch_without_worker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_next_conflict_after_continue_reenters_worker_without_replaying_ready tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_restart_after_continue_reenters_worker_without_replaying_ready tests.test_rebase_correction_state.RebaseCorrectionStateTests.test_legacy_schema_upgrade_preserves_result_and_new_contract_after_reopen tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests.test_pending_stage_with_unmerged_index_retries_only_accepted_paths tests.test_execution_rebase_correction_phase.ExecutionRebaseCorrectionPhaseTests.test_continue_lock_resume_does_not_repeat_stage tests.test_scope_block_revalidation.EvidenceFreeRebaseBlockTests.test_plan_input_block_uses_owned_rebase_before_ordinary_branch_recovery tests.test_scope_block_revalidation.EvidenceFreeRebaseBlockTests.test_plan_input_rebase_block_keeps_original_cause_if_remote_changes
```

결과: **PASS — 16 tests, exit 0**. 이번 실행에서 새로 수집된 각 결과가 모두 `ok`이며 이전 후보의 결과를 재사용하지 않았다. 로그의 VERIFYING/REVIEWING 전이는 mock verify를 사용하는 unit assertion으로 실제 전체 verify·Review의 성공을 뜻하지 않는다. 실행은 Python 3.12/Linux, unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집 계약이며 필수 compile 선언은 유지한다. 기존 verify 입력 목록은 변경 코드·테스트를 포함한다.

현재 Worker 범위 구현·targeted 검증을 마쳤다. 전체 verify·최종 Review·게시 및 사용자 Merge는 Manager의 후속 단계로 남기며 commit·push·PR 생성·scripts/verify·전체 suite·integration·전체 lint/build/type check는 이번 실행에서 수행하지 않았다.


## Manager 부분 verify 진단 교정 (2026-10-16, e42dd326 revision 1)

현재 HEAD `8b88055`의 ExecPlan·sources·검증 계약과 고정 base `ca05750ab3f3b100c28d7bc9f6c444de879d03b1` 대비 누적 diff를 확인했다. 직전 후보 성공을 이번 검증으로 재사용하지 않았다. 전달된 Manager summary는 부분 verify의 652개 성공·7개 실패이며 전체 verify 성공이 아니다. 생략된 진단에서도 `test_selected_body_contract_accepts_worker_receipt_before_completion`을 확인해 같은 targeted 실행에 포함했다.

실패 원인은 최신 base에서 바뀐 두 계약을 테스트가 반영하지 않은 것이다. metadata 오류는 `PLAN_INPUT_INVALID`로 즉시 보류하므로 Worker 교정 반복/횟수 소진을 기대한 세 테스트를 첫 결과의 차단·원래 session/result·verify 출력 보존과 Agent 중복 호출 방지로 갱신했다. rebase 테스트는 실제 guard 입력과 범위 검사 전에 저장된 응답/input snapshot도 확인한다. 기존 테스트 ID는 같은 명령 재실행과 실행 이력 연결을 위해 유지한다.

완료 시 선언·사용자 결정 검사는 실제 선택 ExecPlan 파일과 target 원문을 읽는다. runtime adapter fixture의 가상 `/owned`·빈 `git show`·필수 frontmatter 누락을 개별 테스트 전용 임시 worktree와 유효한 고정 원문으로 교체했다. identity 검사와 서비스 생성 시 declaration 검사를 분리해 확인하며 scope predicate의 I/O 부재 assertion을 유지한다. 기존 외부 notes·다른 문서·ID 변경·원격 소유권·중간/완료 경계의 거부 assertion은 유지했다. 생산 코드·계약을 완화하지 않았고 실제 Git/Agent 프로세스를 실행하지 않았다.

최초 targeted 실행은 7개 test 실패(8개 실패 assertion/subtest 포함)로 종료했다. 첫 수정 뒤 metadata 3개는 해소됐지만 fixture 원문에 필수 frontmatter가 빠져 4개 test가 남았다(5개 실패 assertion/subtest 포함). 모든 필수 필드를 갖춘 원문과 실제 임시 경로 연결로 보완한 뒤 아래 동일 명령의 최종 재실행에서 7개 전부 통과했다. 앞선 두 실행은 통과로 집계하지 않는다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_rebase_metadata_correction_preserves_resume_context tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_verify_metadata_mismatch_returns_to_worker_with_output tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_worker_metadata_mismatch_corrects_then_final_blocks tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_adopted_document_pin_does_not_reject_later_worker_notes tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_auxiliary_markers_preserve_original_document_preflight_exception tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_factory_uses_current_inputs_exact_lease_and_final_scope tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_selected_body_contract_accepts_worker_receipt_before_completion
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_rebase_metadata_correction_preserves_resume_context tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_verify_metadata_mismatch_returns_to_worker_with_output tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_worker_metadata_mismatch_corrects_then_final_blocks tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_adopted_document_pin_does_not_reject_later_worker_notes tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_auxiliary_markers_preserve_original_document_preflight_exception tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_factory_uses_current_inputs_exact_lease_and_final_scope tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_selected_body_contract_accepts_worker_receipt_before_completion

최종 재실행 결과: **PASS — 7 tests, exit 0**, 모든 test `ok`.

공유 fixture를 사용하는 나머지 네 개 개별 unit만 추가 확인했다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_windows_file_modes_reuse_index_without_per_file_git_queries tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_clean_replay_head_and_staged_document_are_checked_separately tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_owned_user_document_conflicts_allow_preservation_without_final_acceptance tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_user_document_conflict_permission_requires_recorded_input_and_exact_operation
```

결과: **PASS — 4 tests, exit 0**. 이번 후보에서 최종 성공한 개별 unit은 총 11개이며 과거 16개 성공을 이번 실행 결과로 합산하지 않는다. unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집·필수 compile 선언과 기존 verify 입력 목록을 유지하며 실행 환경은 Python 3.12/Linux다. Windows mode 테스트는 모의 OS unit이며 Windows native 실행을 뜻하지 않는다.

선택된 계획 frontmatter·sources·사용자 결정·Requirement·다른 계획·PLANS는 변경하지 않았다. 누적 PRODUCT·ARCHITECTURE diff의 설명과 hash는 현재 base 대비로 다시 확인했다. 전체 verify·Review·게시·Merge는 Manager의 후속 단계이며 Worker는 전체 suite/integration/verify/compile·전체 lint/build/type check·commit/push/PR 생성을 실행하지 않았다.


## 最新 base autostash 충돌 교정 (2026-10-16)

지정 revision `890d296cc1df0afdd122fdf42a7a3887452b3ece`의 ExecPlan·PRODUCT·R-082·R-072·R-055를 git show로 확인하고 재생된 HEAD `39ee152` 및 autostash 형상과 대조했다. 최신 base에는 이전 verify 진단의 metadata 즉시 차단·유효한 임시 fixture 교정과 새 테스트 이름이 이미 있다. 해당 base 교정을 중복 구현하지 않고 stashed 응답/input snapshot·session 보존 assertion과 최초 occurrence·Agent 단일 호출 assertion을 결합했다.

두 테스트 파일의 충돌 표식을 제거했다. runtime adapter는 최신 base의 `_plan_text`·공용 helper·명시적 remote fixture·사용자 결정 작성 거부 회귀를 유지했다. 충돌로 중복된 `git show` stub과 독립 fixture 생성을 제거하고 기존 helper의 유효한 원문·임시 worktree를 재사용한다. 보조 표식 테스트의 임시 DB 갱신은 repo/plan을 함께 지정한다. 선언 검사 선평가와 identity 관측 분리·순수 scope predicate assertion도 보존했다. 생산 코드의 저장 입력 guard·완료 definition·다음 충돌 READY 종료와 기존 문서 계약은 변경하지 않았다.

현재 통합 후보에서 아래 개별 unit만 새로 실행했다. 이전 후보의 7+4 결과는 과거 기록이며 현재 성공으로 재사용하지 않는다. 과거 metadata 테스트 이름 셋은 최신 base가 바꾼 ID로 실행했다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_rebase_metadata_block_preserves_resume_context tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_verify_metadata_mismatch_blocks_with_saved_output tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_worker_metadata_mismatch_blocks_without_repeated_correction tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_adopted_document_pin_does_not_reject_later_worker_notes tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_auxiliary_markers_preserve_original_document_preflight_exception tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_factory_uses_current_inputs_exact_lease_and_final_scope tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_selected_body_contract_accepts_worker_receipt_before_completion tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_completed_rebase_rejects_worker_supplied_user_decision
```

결과: **PASS — 8 tests, exit 0**, 모든 test `ok`. Git/Agent는 mock이며 SQLite·파일은 독립 fixture다. unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집·필수 compile 선언을 유지하며 실제 실행은 Python 3.12/Linux다. 전체 verify·integration·전체 suite·compile·전체 lint/build/type check는 실행하지 않았다.

선택 계획의 기존 본문·수행 기록과 frontmatter·sources·사용자 결정을 보존했다. Requirement·다른 ExecPlan·PLANS는 수정하지 않았다. Worker는 파일 내용만 교정했으며 index의 unmerged 표시 해제·autostash 보존/정리·관리 Git 완료 및 후속 verify/Review는 Manager가 수행한다. commit·push·PR 생성·rebase continue·stash 명령과 운영 SQLite 변경을 수행하지 않았다. 누적 PRODUCT·ARCHITECTURE 보고 hash는 지정 base `890d296` 대비 최종 문서 diff로 다시 산출했다.


## autostash 반영 완료 후보 확인 (2026-10-16)

Manager가 관리 rebase 결과를 수용한 현재 HEAD `0cf428b`에서 clean worktree와 충돌 없는 파일을 확인했다. 현재 ExecPlan과 PRODUCT·R-082·R-072·R-055를 다시 읽고 base `890d296cc1df0afdd122fdf42a7a3887452b3ece` 대비 누적 21개 파일 diff를 기존 교정과 대조했다. 직전 HEAD 이후 변경은 선택 계획 수행 기록과 두 테스트 파일의 assertion 결합이며, 생산 코드의 호출 입력 선저장·저장 결과 guard·완료 definition 설치·다음 충돌 READY 소비는 그대로 보존된다. 최신 fixture와 사용자 결정 거부 회귀도 유지된다. 추가 구현이나 테스트 변경이 필요한 누락은 확인하지 않았다.

현재 후보의 결과를 새로 얻기 위해 metadata·scope 경계 8개와 응답/input 선저장·정규화 후 pending·입력 변경 재평가·다음 dispatch·다음 충돌 및 중단 재진입 6개를 개별 unit ID로 실행했다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_rebase_metadata_block_preserves_resume_context tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_verify_metadata_mismatch_blocks_with_saved_output tests.test_execution_revision_gate.ExecutionRevisionGateTests.test_worker_metadata_mismatch_blocks_without_repeated_correction tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_adopted_document_pin_does_not_reject_later_worker_notes tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_auxiliary_markers_preserve_original_document_preflight_exception tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_factory_uses_current_inputs_exact_lease_and_final_scope tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_selected_body_contract_accepts_worker_receipt_before_completion tests.test_managed_mutation_evidence.CurrentRebaseRuntimeAdapterTests.test_completed_rebase_rejects_worker_supplied_user_decision tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_pending_recovery_uses_checkpoint_after_frontmatter_normalization tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_saved_ready_rechecks_changed_body_with_identical_file_marker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_completed_changed_definition_survives_next_dispatch_without_worker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_next_conflict_after_continue_reenters_worker_without_replaying_ready tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_restart_after_continue_reenters_worker_without_replaying_ready tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_rebase_response_is_saved_before_metadata_failure_without_worker_retry
```

결과: **PASS — 14 tests, exit 0**, 모든 test `ok`. 이전 후보의 성공을 이번 실행으로 재사용하거나 합산하지 않았다. Git/Agent는 mock, SQLite/파일은 독립 fixture다. unit 로그의 VERIFYING/REVIEWING은 mock 검증 단계 전이이며 실제 전체 verify/Review 성공이 아니다. unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집·필수 compile 선언을 확인했으며 실행 환경은 Python 3.12/Linux다.

기존 수행/교정 원문을 보존하고 이번 검증 기록만 선택 계획 본문에 추가했다. frontmatter·sources·사용자 결정·Requirement·다른 ExecPlan·PLANS는 수정하지 않았다. 누적 PRODUCT·ARCHITECTURE의 보고 hash는 현재 base 대비 최종 diff에서 다시 확인했다. 전체 verify·Review·게시·Merge는 Manager의 후속 단계로 남기며 전체 suite/integration/compile·전체 lint/build/type check·scripts/verify·commit/push/PR 생성은 실행하지 않았다.


## 전체 verify 전달 진단 교정 (2026-10-16, 1e9811cb revision 1)

Manager summary `e15efee2a94ab1cb10eb470a13bed637`의 4282 success·7 skip·9 error·3 failure와 원본 worker 로그를 확인했다. 이는 전체 성공이 아니다. 현재 HEAD `12364ee`와 base `890d296`의 누적 구현·관련 테스트 및 현재 계획·검증 계약을 대조했다. 진단에서 생략된 `test_worker_conflict_scope`의 공통 fixture/오류 분류 문제도 확인했다.

최초 충돌 해결 호출 전에 저장한 definition의 `worker_guidance_hash`가 비어 있고 실제 Agent 전달 뒤 invocation ledger에 hash가 생기면, 구현 조건 delta가 없어도 token이 달라 같은 Worker를 재호출했다. `WorkerHandler.run`은 응답 저장 전에 기존 `worker_content_hash()`로 실제 전달 근거만 호출 입력에 결합한다. 본문·sources·제약·고정 지침 원문은 호출 전 snapshot을 유지하고 현재 definition으로 교체하지 않는다. 새 mock/SQLite unit은 최초 전달 뒤 단일 Worker 결과가 완료 경계로 이어지고 저장 입력의 나머지 필드가 원래 호출과 같음을 검사한다. 기존 호출 중 조건 변경·근거 부재 회귀도 다시 실행했다.

scope adapter의 중간 판정 전에 발생하는 일반 소유권 오류는 기존 최종 경계와 같은 `WorkspaceOwnershipError`로 전달하고, typed `RecoveryFailure`는 동일 객체로 보존해 PLAN_INPUT_INVALID/실제 범위 위반 원인을 유지한다. 완료 scope fixture에는 실제 관측 계약의 metadata/operations/staged/index를 채우고 중간 유예 검사와 완료 최종 검사 두 관측을 모두 유지했다. 실제 정책 회귀는 범위 위반의 typed 원인과 다른 소유권 오류를 각각 assertion한다. PR 철회 준비 테스트는 실제 PR 출현의 전용 `MutableReadyPRConflict`를 기대하도록 교정하며 파일 보존·최초/재개 조회 순서를 유지한다.

PR 충돌 중 pause/재개와 autostash lifecycle의 역할 assertion 두 곳은 유효한 교정 뒤 불필요한 두 번째 Worker를 기대하지 않도록 `worker → reviewer`로 정합화했다. 원격·source 보존·실제 Git 효과·verify/PR assertion은 유지했다. empty conflict의 단일 Worker assertion은 여전히 유효하므로 변경하지 않았다. 이 세 실제 Git lifecycle 테스트는 Worker 실행 금지에 따라 실행하지 않았고 Manager 후속 검증에 남긴다. 초기 autostash의 중복 Worker/남은 index 증상과 empty conflict 재호출은 새 실제 전달 hash unit의 원인 교정으로 다룬다.

최초 아래 targeted 실행은 신규 전달 hash 회귀 1 failure와 scope/PR 예외 16 errors로 종료했다. 첫 보완 뒤에는 실제 scope 위반의 typed 예외를 기존 일반 소유권 예외로 기대한 한 subtest error만 남았다. 해당 원인 assertion을 정확한 코드로 보완한 뒤 같은 명령의 최종 실행에서 모두 통과했다. 이전 두 실행을 통과로 집계하지 않는다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_first_worker_delivery_binds_guidance_to_saved_rebase_inputs tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_actual_pr_check_runs_before_any_receipt_or_unlink_and_on_resume
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_first_worker_delivery_binds_guidance_to_saved_rebase_inputs tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_actual_pr_check_runs_before_any_receipt_or_unlink_and_on_resume

최종 재실행 결과: **PASS — 17 tests, exit 0**, 모든 test `ok`.

추가로 typed 입력 오류 동일 객체 보존과 응답 선저장·호출 중 조건 변경·입력 부재·완료 다음 dispatch의 개별 unit만 확인했다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests.test_owned_rebase_preserves_typed_input_failure tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_rebase_response_is_saved_before_metadata_failure_without_worker_retry tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_live_worker_ready_is_not_blessed_after_inputs_change_during_turn tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_saved_ready_without_definition_requires_same_session_reassessment tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_completed_changed_definition_survives_next_dispatch_without_worker
```

결과: **PASS — 5 tests, exit 0**. 현재 수정에 관련된 최종 성공 unit은 22개다. Git/Agent는 mock이고 SQLite/파일은 독립 fixture이며 mock verify 로그를 실제 전체 verify 성공으로 보고하지 않는다. unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집·필수 compile 선언과 기존 verify 입력 목록을 유지한다. 실제 실행은 Python 3.12/Linux다.

ARCHITECTURE에 실제 전달 hash 결합 시점과 호출 전 조건 보존을 반영했다. PRODUCT의 기존 보장은 유지하며 누적 두 문서의 hash를 현재 base 대비로 다시 산출한다. 선택 계획 frontmatter·sources·사용자 결정·Requirement·다른 계획·PLANS는 변경하지 않았다. 전체 verify·실제 Git lifecycle/통합·Review·게시·Merge는 Manager에 남기며 commit/push/PR·scripts/verify·전체 suite/compile·전체 lint/build/type check는 실행하지 않았다.


## 최신 완료 검사 경계와 autostash 교정 통합 (2026-10-16)

지정 revision `97459737e66adb29fab32b38378d168b1129e492`의 ExecPlan·PRODUCT·R-082·R-072·R-055와 해당 코드 delta를 git show로 확인했다. 재생된 HEAD `d3f9c74` 및 stashed 교정을 대조했다. 최신 base는 replay/autostash가 실제 진행 중일 때만 앞선 검사를 유예하며 완료 형상은 기존 최종 경계의 단일 관측으로 검사한다. 이 최신 순서를 유지하고 scope adapter의 충돌 표식을 제거했다. 앞선 이력의 '완료 시 두 관측' 설명과 완료 scope 위반의 조기 typed 전달 설명은 당시 후보 기록이며 현재 적용 계약은 아니다.

중간 검사에서는 stashed `RecoveryFailure` 보존을 결합해 입력 오류 등 확인된 원인을 일반 소유권 오류로 바꾸지 않는다. 완료 시에는 최신 base의 최종 검사/오류 경계를 유지한다. 완료 단일 관측 assertion과 실제 완료 정책의 소유권 오류 assertion을 최신 base에 맞추고, typed 입력 오류 동일 객체 보존 회귀는 실제 replay pending 형상에서 검사한다. 소유권·원격·index 보안·최종 범위 거부 assertion은 유지한다. 완료 관측 필드를 채운 fixture와 새 최초 전달 지침 hash unit도 보존했다.

응답 저장 전에 실제 전달 지침 hash만 호출 입력에 결합하는 원인 교정, PR 충돌/자동 stash의 불필요한 후속 Worker 기대값 정합화, 공식 PR 경합 예외 기대값은 최신 base와 함께 유지된다. 기존 본문/수행 원문·문서 계약·frontmatter·sources·사용자 결정을 보존했다. Requirement·다른 계획·PLANS는 변경하지 않았다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_first_worker_delivery_binds_guidance_to_saved_rebase_inputs tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_worker_end_defers_intermediate_metadata_and_scope_but_rechecks_remote tests.test_planner_validation_recovery.RequirementRecoveryUnitTests.test_actual_pr_check_runs_before_any_receipt_or_unlink_and_on_resume
```

현재 통합 후보 결과: **PASS — 20 tests, exit 0**, 모든 test `ok`. 이전 후보의 결과를 성공으로 재사용하지 않았다. Git/Agent는 mock, SQLite/파일은 독립 fixture다. unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집·필수 compile 선언을 유지하며 실제 실행은 Python 3.12/Linux다. 실제 Git lifecycle 세 개와 전체 verify/Review는 Manager의 후속 검증에 남긴다. 전체 suite/integration/compile·scripts/verify·전체 lint/build/type check는 실행하지 않았다.

Worker는 파일 내용 충돌만 해결했으며 index 확정·autostash 정리·관리 Git 완료·게시·Merge는 Manager에 맡긴다. commit/push/PR 생성·rebase continue/stash·운영 SQLite 변경은 수행하지 않았다. PRODUCT·ARCHITECTURE의 누적 문서 보고 hash는 지정 base `9745973` 대비로 다시 산출했다.


## 管理 rebase 완료 HEAD 재검증 (2026-10-16)

Manager가 수용한 현재 HEAD `5de88a9`의 clean 상태를 확인하고 현재 ExecPlan·PRODUCT·ARCHITECTURE·연결 R-082/R-072/R-055와 base `97459737e66adb29fab32b38378d168b1129e492` 대비 전체 25개 파일 diff를 이전 교정과 대조했다. 호출 전 입력/실제 전달 hash·저장 결과 재평가·완료 definition·다음 충돌 READY 종료 및 최신 중간/최종 scope 경계가 함께 유지된다. 추가 코드나 테스트 수정이 필요한 누락은 확인하지 않았다. 기존 테스트 변경과 실제 Git lifecycle의 단일 Worker 기대값 정합화도 보존한다.

현재 후보를 직접 검증하기 위해 아래 개별 unit 11개를 새로 실행했다. 이전 후보의 20개 통과를 현재 결과로 재사용하거나 합산하지 않는다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_first_worker_delivery_binds_guidance_to_saved_rebase_inputs tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_live_worker_ready_is_not_blessed_after_inputs_change_during_turn tests.test_execution_worker_phase.ExecutionWorkerPhaseTests.test_rebase_response_is_saved_before_metadata_failure_without_worker_retry tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests.test_owned_rebase_preserves_typed_input_failure tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests.test_pending_checkpoint_defers_final_declaration_checks_only_after_ownership tests.test_worker_conflict_scope.WorkerConflictScopeUnitTests.test_completed_rebase_uses_real_current_result_policy tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_worker_end_defers_intermediate_metadata_and_scope_but_rechecks_remote tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_completed_changed_definition_survives_next_dispatch_without_worker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_next_conflict_after_continue_reenters_worker_without_replaying_ready tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_restart_after_continue_reenters_worker_without_replaying_ready tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_pending_recovery_uses_checkpoint_after_frontmatter_normalization
```

결과: **PASS — 11 tests, exit 0**, 모든 test `ok`. 실제 전달/응답 저장·호출 중 조건 변경·typed 입력 오류·중간 검사 유예/원격 확인·최종 거부·완료 다음 dispatch·다음 충돌/중단 재진입·정규화 뒤 checkpoint 재개를 확인했다. Git/Agent/verify는 mock이며 SQLite/파일은 독립 fixture다. mock VERIFYING/REVIEWING 로그는 전체 verify/Review 성공을 뜻하지 않는다. unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집·필수 compile 선언과 verify 입력 목록을 유지하며 실행은 Python 3.12/Linux다.

이번 변경은 선택 계획 본문의 검증 기록뿐이다. 기존 본문·frontmatter·sources·사용자 결정·Requirement·다른 ExecPlan·PLANS는 보존했다. PRODUCT·ARCHITECTURE의 누적 diff hash를 현재 base 대비로 다시 확인했다. 실제 Git lifecycle 세 개·전체 verify·Review·게시·Merge는 Manager가 후속 검증하며 Worker는 전체 suite/integration/compile·전체 lint/build/type check·scripts/verify·commit/push/PR 생성과 운영 SQLite 변경을 수행하지 않았다.


## PR lease 회귀의 Worker 호출 기대값 교정 (2026-10-16, 9791d11c revision 1)

현재 HEAD `2aecfd0`, base `97459737e66adb29fab32b38378d168b1129e492`의 누적 diff와 현재 계획·sources·검증 계약을 다시 확인했다. Manager 진단 `2b24ed650872bfc1f89114e7a83f87cc`는 644 success·1 failure이며 전체 성공은 아니다. 남은 오류는 `PrConflictLeaseTests.test_conflicting_pr_reuses_sessions_revalidates_new_base_and_leases_push`의 과거 중복 Worker 호출 기대값이다. 실제 전달 입력과 완료 definition을 보존하므로 충돌 해결 Worker 한 번 뒤에는 두 Review가 실행된다.

해당 실제 Git 회귀의 역할 순서를 `worker → reviewer → reviewer`, session 목록을 `worker-existing → reviewer-existing → reviewer-existing`으로 함께 교정했다. verify 두 번·push 두 번·각 예상 OID의 force-with-lease·기존 PR 및 source checkout 보존·새 PR 생성 거부·최종 상태 assertion은 유지했다. 생산 코드의 재사용 판정을 변경하거나 일반 실패/재교정 테스트의 Worker 호출 횟수를 일괄 바꾸지 않았다.

```sh
python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_first_worker_delivery_binds_guidance_to_saved_rebase_inputs tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_completed_changed_definition_survives_next_dispatch_without_worker tests.test_execution_rebase_correction_phase.SavedRebaseInputTests.test_saved_ready_accepts_oid_and_source_fingerprint_only_changes
```

현재 후보 결과: **PASS — 3 tests, exit 0**, 모든 test `ok`. 실제 전달 근거·완료 후 다음 dispatch의 Worker 재호출 방지·OID/source 지문만의 변화 수용을 mock Git/Agent·격리 SQLite unit으로 확인했다. 이전 통과를 이번 결과로 재사용하지 않았다. 변경한 lease 테스트는 실제 Git/PR lifecycle이므로 Worker의 integration 실행 금지에 따라 실행하지 않았고 동일 테스트의 직접 재실행은 Manager 검증에 남긴다. 따라서 Manager가 보고한 과거 통합 오류가 재실행으로 해소됐다고 주장하지 않는다.

unittest adapter v1·launcher Python·지원 3.11–3.14·독립 수집·필수 compile 선언과 verify 입력 목록은 유지하며 실행 환경은 Python 3.12/Linux다. 선택 계획 frontmatter·sources·사용자 결정·Requirement·다른 계획·PLANS는 변경하지 않았다. 누적 PRODUCT/ARCHITECTURE 문서 hash를 현재 base 대비로 다시 확인했다. 전체 verify·suite·integration·compile·전체 lint/build/type check·commit/push/PR 생성은 실행하지 않았다. 전체 verify·Review·게시·Merge는 Manager의 후속 단계다.
