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

- [ ] `execution/execution_worker_handler.py:WorkerHandler.run()`에서 최신 base의 `finish_turn()` 선저장 순서와 후보의 `input_snapshot=turn_definition...` 저장을 함께 유지한다. 현재 후보는 범위 검사 뒤 저장하고 최신 base는 검사 전 저장하지만 호출 입력 필드는 아직 없다. 실제 호출 전에 고정한 definition을 응답에 결합하고, 저장 뒤 검사 오류·중단에서도 같은 결과를 재수집·소비하며 입력 부재를 현재 definition으로 채우지 않는다. `storage/rebase_correction_state.py`의 기존 nullable 호환·CAS와 결과 이력을 재사용한다.
- [ ] `agents/scope_adapter.py`의 소유된 중간 rebase 검사 유예, `execution/execution_revision_handler.py`의 `PLAN_INPUT_INVALID` 분류와 후보의 저장 결과 guard를 통합한다. 구조·operation·원격 소유권·index 안전 조건과 충돌 해결 Agent의 직접 변경 검사를 유지하고 완료 전의 선언을 최종 후보로 판정하지 않는다. `_resume_saved_rebase_worker()`와 pending stage/continue도 실제 당시 입력·현재 조건을 대조하며 동일 입력에는 후속 단계부터 이어간다.
- [ ] `git/rebase_documents.py:restore_rebase_frontmatter()`·`ManagedRebaseService.normalize_frontmatter()`와 최신 `execution_rebase_handler.py`·`execution_workspace_handler.py`의 호출을 재사용한다. 후보의 `_post_rebase_observation()`·`_complete_rebase_intent()`·Workspace 재개가 복원 뒤 갱신한 checkpoint·서비스·HEAD를 사용하도록 연결하고, 실제 완료 definition과 Worker 결과만 새 후보에 결합한다. 원문/파일 적용·index stage·commit 복원 엔진이나 허용 선언 비교를 재구현하지 않는다.
- [ ] `git/direct_rebase_service.py`와 `recovery/blocked_plan_service.py`·`project_block_service.py`·`scope_reconciliation.py`의 직접 구현된 정규화·소유 rebase 우선 복구를 보존한다. `storage/recovery_state.py:RecoveryRepository.resolve_rebase_block()`의 확인된 입력 오류 원인 해소와 worktree 복구 실패의 `preserve_cause=True`도 재사용한다. 현재 후보와 합칠 때 `plan_input_invalid`를 일반 branch 복구로 되돌리거나 같은 응답의 교정 횟수를 다시 세지 않는다. 기존 차단 서비스의 원인별 해제·dependency·독립 원인 보존을 사용하고 저장 결과 연결의 실제 누락만 보완한다. 과거 Worker 교정 분류·진단 문구 차이로 해소된 입력 오류를 재차단하지 않고 원래 detail·실패 감사를 유지한다.
- [ ] `catalog/requirement_sources.py:reconcile_execplan_sources()`와 `requirement_correction_publication.py`의 공식 source 교정을 재사용해 최신 선언을 소비한다. 이미 반영된 정확한 archive 우선·활성/archive 모호함 거부와 `agents/agent_prompts.py:worker_prompt()`의 source 파일명 차이만 남은 충돌 해결 인계를 유지하며 그 기능을 재구현하지 않는다. 이번 계획의 R-072·R-055는 정확한 기존 활성/archive 경로가 없고 같은 원본 ID의 활성 문서가 유일함을 확인해 현행 source 경로로 정합화한다. `execution/stage_documents.py:refresh()`의 legacy 판단·게시 복구와 G-242의 저장 결과/legacy PR 검증을 유지하며 작업별 Planner·새 `document_revision` 판단을 만들지 않는다.
- [ ] G-255의 `rebase_documents` pinned revision·교체 전 bytes·중간 HEAD·replay별 이력과 일반 문서 채택의 최종 HEAD/index/파일 대조를 유지한다. `resolve(failed_files=...)`·`git/rebase_file_evidence.py`·`rebase_files`의 복구 불가 파일 재작업은 별도 모드로 재사용하며 확보 가능한 자료·실패 이유·다른 파일·target·session을 보존한다. 과거 원형의 완전 보존을 재작업 진입 gate로 추가하지 않는다. PRODUCT·ARCHITECTURE의 실제 구현 설명은 최신 base와 후보를 통합하고 새 상태·재시도 엔진·비호환 schema는 추가하지 않는다. NO_CHANGE·다른 Git 차단·cleanup의 전수 감사는 G-189에 남긴다.

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
- [ ] 기존 후보 구현·수행 기록을 보존한 최신 base 통합과 위 추가 조건의 최소 targeted unit 검증
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
