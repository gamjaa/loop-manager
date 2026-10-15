---
id: G-256
title: 최신 조건의 rebase 교정과 저장 결과 재평가
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
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

G-255의 문서 충돌 처리를 최신 작업 조건·Worker 교정·검증과 저장된 결과/차단의 재평가에 연결한다. 직접 구현된 입력 변경 시 작업 보존·성공 token 무효화와 R-082의 작업별 Planner 제거를 유지하고, 저장 교정 결과와 최신 입력을 연결하는 미충족 부분만 보완한다.

## Acceptance Criteria

- Worker 최초·재개·교정 진입 전에 필요한 프로젝트 일반 planning의 관련 개정 게시·DB 적용을 마치고 그 immutable base의 ExecPlan을 반영한다. PR 등록 뒤 기존 계획 개정 금지·수동 소유 보호·dependency 준비 조건을 지키며 Planner와 구현 worktree의 writer/Git operation을 겹치지 않는다. 소유 rebase가 이미 진행 중이면 기존 target·pinned 문서로 그 operation의 완료/복구를 먼저 처리하고 안전한 경계에서 남은 최신 명세 planning·반영을 이어간다. 작업별 상위 명세 영향 판단·새 plan_text 생성을 다시 도입하지 않는다.
- R-082: 상위 명세 변경에 필요한 일반 planning과 ExecPlan·코드 변경만의 실행 대응을 구분한다. 후자는 선언·source·dependency의 Manager 검증과 기존 Worker 세션에서의 구현 판단·필요한 보완·targeted test로 처리한다. 유효한 구현 충족 근거가 있으면 필요한 verify·Review를 계속하고, 새 조건을 확인할 근거가 없으면 Worker에 최신 ExecPlan·delta·과거 근거를 함께 전달한다. 별도 문서 영향·구현 적합성 Planner, 코드 전용 점검·일반 Planner 인계를 되살리지 않는다.
- 문서-only 자동 해소 뒤 발견 단계와 실제 구현·검증 근거로 필요한 단계만 선택한다. 그 외 파일 충돌은 verify·Review·게시 단계에서 발견해도 같은 Worker session의 교정으로 연결하고, 최신 문서를 기준으로 양쪽 형상을 검토·선택·결합하며 필요한 targeted test를 수행한다. Reviewer의 읽기 전용 권한과 선행 Review·검증 보완의 기존 의미를 유지한다.
- 본문·sources·연결 명세·archive 범위·지침 변화와 코드 충돌이 함께 있거나 이전 definition 원문이 없어도, 현재 identity·소유권·유효한 최신 조건이 확인되면 보존된 작업의 교정·검증을 이어간다. 과거 성공을 재사용할 증거가 없으면 성공 근거만 무효화하며, 입력 변경 자체로 최종 차단하거나 session·작업물·실행/실패·level 감사를 폐기하지 않는다.
- 저장된 rebase Worker 결과와 pending stage/continue는 현재 후보·문서 조건과 결합해 소비한다. 파일 상태가 같아도 조건이 바뀌면 이전 READY나 과거 Agent snapshot/marker만으로 새 조건 충족을 승인하지 않는다. 반대로 구현 입력이 유효한 OID 변화만으로 Worker를 반복하지 않고 필요한 verify·Review로 이어간다. 입력이 같은 완료 교정은 필요한 후속 Git/검증 단계부터 재개한다.
- R-055: 저장 결과/pending continue의 재평가에서도 전체 역할 diff·문서/archive 최종 내용 검사는 rebase 시작 전과 완료 후에 적용한다. commit 재생·일부 충돌 stage·autostash 복원 중간 형상은 최종 위반으로 차단하지 않으며 현재 소유권·target·원격·경로/mode/index flags와 충돌 해결 Agent 전후 변경 검사는 유지한다. 단계가 바뀐 관측은 재관측하고, 중간 서비스의 archive 계약을 새로 구성해 완료 후보의 전체 변경·보호 원문·복원 결과를 검증한 뒤 후속 단계로 연결한다. 보조 Git 표식만으로 완료 상태를 pending으로 되돌리지 않는다.
- 이 입력 불일치만으로 이미 차단된 작업은 현재 소유 operation·조건·남은 교정을 재평가한다. 안전한 경로가 있으면 해당 원인만 해제하고 기존 자원과 session을 이어간다. 실제 결과 오류·sandbox 정책 실패·현재 대상/문서 유효성 불확실·필수 사용자 결정·미충족 dependency·진전 없는 반복은 기존 경계에서 유지한다. 폐지된 Git snapshot 권한 비교를 재활성화하거나 무한 교정을 만들지 않는다.
- Planner 게시·문서 적용·Worker 결과 저장·Git 계속 진행·차단 해제 사이 중단과 추가 원격 전진에서도 기존 target·원형·실제 확정 지점을 보존한다. 완료한 소유 operation 뒤 최신 base 반영을 이어가고, 최신 조건의 targeted test·필요한 Manager verify·최종 후보 Review와 게시 안전 조건 없이 구현 완료나 PR 게시를 승인하지 않는다.

## Implementation

`execution/stage_documents.py`의 legacy document_revision 복구, `execution_revision_handler.py`의 정의/성공 token 판정, `execution_worker_handler.py`의 교정 호출·저장 결과 소비와 기존 rebase correction/checkpoint 저장을 연결한다. 새 문서 판단을 만들지 않는 현재 `refresh()`와 Worker revision gate를 재사용한다. 최신 ExecPlan 조건·교정 turn의 입력과 과거 수행 근거를 구분하고, 진행 중 rebase는 기존 operation을 보존해 처리한 뒤 안전한 경계에서 필요한 상위 명세 planning·게시·DB 적용과 남은 최신 base 반영을 이어간다. G-242의 저장된 결과·legacy 게시/PR 검증만 소비하며 작업별 Planner 호출이나 새 document_revision 판단·개정 후보를 만들지 않는다.

Merge된 G-255의 `git/rebase_documents.py`와 RebaseHandler의 문서 자동 진행을 원문 적용 경계로 재사용한다. `document_revision` 판단·게시와 `rebase_documents`의 pinned revision·교체 전 bytes·중간 HEAD·replay별 채택 이력은 서로의 성공 근거가 아니며 checkpoint에서 함께 보존한다. 진행 중 operation의 target·채택 revision을 새 Planner 결과로 바꾸지 않고, 해당 operation을 완료한 뒤 추가 원격 전진을 기존 최신 base 반영으로 처리한다. 일반 문서 채택의 최종 HEAD·index·작업 파일 원문 대조는 유지한다. R-055의 복구 불가 파일 재작업은 직접 구현된 `resolve(failed_files=...)`·`git/rebase_file_evidence.py`와 `rebase_files` intent를 재사용하는 별도 복구다. 확보 가능한 원형·실패 사유를 보존하고 해당 파일의 Worker 재작업·새 입력 검증으로 연결하며, 과거 파일 원형의 완전 보존을 재작업 진입 조건으로 요구하지 않는다. 다른 파일·기존 target·session·실패 이력은 보존한다. 두 모드의 교정 결과·입력 수명 연결만 보완하고 원문 교체·continue/skip·파일 복원을 재구현하지 않는다.

기존 rebase block 선택·현재 상태 수용·원인별 해제 서비스를 대조해 이 불일치의 잔여 재차단 경로만 교정한다. 이미 가능한 현재 상태 복구는 재구현하지 않는다. 확인한 `git/managed_rebase_runtime.py`·`managed_rebase_validation.py`·`managed_rebase_observation.py`의 중간/완료 구분과 새 archive 계약 구성을 저장 Worker 결과·pending continue의 소비에도 재사용한다. 중간 상태의 서비스나 불안정 관측을 완료 근거로 사용하지 않고 충돌 해결 Agent의 직접 변경 검사는 별도로 유지한다. 조건 변경 시 runtime row의 token과 별도로 남는 저장 교정 결과의 입력 유효성을 확인한다. 새 완료 상태·재시도 엔진·비호환 schema를 만들지 않으며, NO_CHANGE와 다른 Git 차단 및 cleanup의 전수 판단은 G-189에 남긴다. PRODUCT·ARCHITECTURE에는 실제 충돌 교정·단계 선택·실패 보존 동작만 반영한다.

## Validation

- `tests/test_execution_revision_gate.py`, `tests/test_stage_documents.py`, `tests/test_execution_rebase_correction_phase.py`·`tests/test_planning_selection.py`의 최소 targeted unit으로 operation이 없는 최초/재개/교정의 프로젝트 planning 게시·DB 적용→ExecPlan 적용→Worker 순서를 확인한다. 진행 중 rebase는 기존 target 완료/복구→추가 planning·최신 base 반영 순서이며 중첩 operation이 없어야 한다. 문서-only의 불필요한 Worker 억제와 그 외 파일/혼합 충돌의 단계별 복귀를 실제 handler·SQLite로 확인하고 Git/Agent 관측만 mock한다.
- 저장 Worker READY 뒤 source/본문 조건만 변경되고 과거 Agent marker는 같은 경우, definition 원문 부재, pending stage/continue 재시작과 동일 입력 완료 결과를 대조한다. 현재 구현 입력은 유효하고 후보 OID만 바뀐 경우도 분리해 불필요한 Worker·오래된 성공·게시를 막는다.
- 위 저장 결과/재시작 조합의 검사 시점은 `tests/test_managed_rebase_validation.py`·`test_managed_mutation_evidence.py`·`test_worker_conflict_scope.py`의 직접 회귀를 재사용한다. replay·일부 stage·autostash 중 내용 유예와 구조/소유권 거부를 구분하고, 완료 관측에는 새 archive 계약·보호 원문·최종 금지 변경 검사가 적용되는지 확인한다. 이중 관측 중 단계 변경은 현재 상태 재관측으로 이어지고 기존 결과를 성공이나 범위 위반으로 확정하지 않아야 한다. 실제 누락된 저장 결과 소비의 최소 unit만 보강하고 일반 rebase·cleanup 전체 조합을 반복 실행하지 않는다.
- 위 조건에서 호출 역할도 확인한다. ExecPlan·코드 변경만으로 일반·점검·문서 영향 Planner는 호출되지 않아야 하며, 미확인 새 조건은 기존 Worker 세션에서 필요한 교정과 함께 한 번 처리돼야 한다. 수행 기록만의 변경·이미 충족한 조건과 미확인 조건을 구분하고 선행 Review·최종 verify/Review gate는 보존한다.
- `tests/test_rebase_correction.py`의 채택 이력·후속 uncertain·최종 원문 대조와 기존 lifecycle fixture를 재사용해 문서 채택과 `document_revision` 판단이 함께 있는 중단/재개를 확인한다. Planner 게시나 입력 무효화가 pinned revision·원형·기존 target을 덮어쓰지 않으며, 뒤늦은 비충돌 재생으로 달라진 문서는 완료로 채택하지 않는다. 동일 원문 적용 통합 경로를 불필요하게 반복하지 않는다.
- `tests/test_task_remote_inputs.py`·`tests/test_rebase_file_evidence.py`의 직접 구현된 파일별 복원·새 사용자 교정 보존 회귀를 재사용한다. 저장 Worker 결과/pending continue를 복원 전후의 후보·실제 최신 조건에 연결하는 unit을 보강하며 일반 문서 채택 완료와 재작업 완료를 혼동하지 않는다. 파일 삭제/이동·일부 원형 부재·중단 뒤 새 교정·나머지 파일 보존은 현재 intent 경계에서 검사한다.
- `tests/test_managed_rebase_lifecycle.py`, `tests/test_scope_block_revalidation.py`와 현재 rebase 수용 회귀에서 동일 입력 재평가·원인별 해제 및 실제 sandbox/결과 오류·독립 차단·사용자 operation·dependency 대기를 확인한다. 과거 ref/config/snapshot 차이를 접근 위반으로 복원하지 않고 실제 target·index·작업물 효과는 기존 통합 fixture를 재사용한다.
- Manager 전체 verify는 원격 재전진·PR/소유권 경쟁·session/실패/level 보존, 반복 한도, 최신 후보 verify·Review·lease와 사용자 Merge gate를 유지한다. Reviewer는 새 조건 미검증의 PASS 채택, 다른 원인 해제·target 덮어쓰기·중첩 rebase를 blocking issue로 확인한다.

## Progress

- [x] 2026-10-10 `ab0264d94da1212d68141b28fc55f0204c1c9e09`의 delta와 관련 handler·저장 교정/차단 판정 및 테스트를 정적으로 확인했다. 구현·테스트·전체 verify를 실행하지 않았다.
- [x] 2026-10-13 `f56739c31a49ec8fb5547a459688bfb02772586e`의 G-255 원문 적용·재개·최종 대조와 현재 `stage_documents.refresh`·저장 Worker 결과 소비를 정적으로 확인했다. 전자는 현재 구현을 재사용하고 후자의 최신 조건 연결은 여전히 남아 있다. 기존 수행 기록과 미완료 항목을 보존하며 구현·테스트·verify는 실행하지 않았다.
- [ ] 최신 조건의 충돌 교정·저장 결과/차단 재평가와 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 최신 R-055·ARCHITECTURE의 검사 시점 변경과 `94ae3d1fc12b11d8d02d220bb91aaf633cc99e4f`의 rebase/runtime/scope 직접 구현·회귀 원본을 대조했다. 중간 replay·autostash 유예와 완료 검증 자체는 재구현하지 않고 이 계획의 남은 저장 결과/continue 연결에 그 계약을 통합한다. G-189는 정상·복구·cleanup의 후속 전수 감사를 담당한다. snapshot token `6e4fb916f06c1c1469efa63d153f96a3890ee168b792392fe50d15d977ae9d72`의 원본 hash `81be4060a95b00c9661395ff833ea4ac5e1e306a43e4d7b69bbe1275f0c88b55`를 확인했고 ID·경로·metadata·기존 수행 기록과 high complexity/risk의 중단/재개·오래된 결과 채택 위험을 보존했다. 검증 adapter는 기존 unittest version 1·launcher Python 3.11–3.14·독립 수집과 필수 compile을 유지한다. 구현·테스트·verify·사용자 결정은 수행하지 않았고 G-246 후보를 수정하지 않았다.

- 이번 delta의 원격 입력 통합·OID만 변경된 후보의 verify 선택·복구 불가 파일 복원은 이미 직접 구현됐다. 남은 최신 조건·저장 교정 결과/pending continue 연결에서 그 구현을 재사용한다. 일반 문서 원문 채택과 파일 재작업의 보존 조건을 구분하고 기존 목표·metadata·source 문자열·실행 기록을 유지한다. 아래 과거 strict scope·원문 비교 기록을 폐기된 권한 검사나 복원 전 파일 원형 완전 보존의 새 gate로 사용하지 않는다.
- 2026-10-14 delta 개정: 원격 R-082의 프로젝트 planning·작업 실행 대응 분리와 원래 소유 operation 우선 복구를 재사용하도록 현재 지시를 갱신했다. 직접 반영된 source 지문만의 Worker token 변화 제외는 일반 planning 완료를 전제로 하며 verify·Review 입력의 제외가 아니다. 저장된 rebase 교정 결과/차단의 최신 조건 연결이라는 남은 목표, 기존 분류·dependency·실행 기록을 유지한다. 구현·테스트·verify는 실행하지 않았다.

- 2026-10-13 delta 개정: mutable snapshot token `bbe557975a5581bd2185b8b767a35088a476de189a346cb28f0469aca8c109f7`, revision `f56739c31a49ec8fb5547a459688bfb02772586e`, 원본 hash `f13365260f2bb3c5cdc4f18ad1f7a635f2393ada7a2157bc2a75af0d884fc12a`와 일치하는 이 계획만 개정했다. G-255의 동일 ID archive 이동은 삭제 목표가 아니므로 dependency를 유지한다. ID·경로·metadata·목표·기록을 보존하고 새 원문 보존 경계의 재사용·회귀 연결만 구체화했다. 기존 high complexity/risk의 판단과 복구 조건을 유지하며 프로젝트 최종 전문 확인이나 구현 성공을 기록하지 않는다.

- 현재 `RevisionHandler.guard`는 충돌 중 입력 변경/과거 원문 부재에서 runtime 성공 token을 비우고 기존 session·rebase target을 보존한다. 해당 직접 구현과 회귀를 재사용한다. 최신 `stage_documents.refresh`는 저장된 legacy 결과·게시만 복구하고 새 판단을 호출하지 않는다. `_resume_saved_rebase_worker`의 저장 교정 결과는 최신 조건과 결합해 소비하고, 근거가 부족하면 같은 Worker 세션의 교정으로 연결한다.
- G-242의 남은 저장 DOCUMENT_RESULT 형식·legacy PR 경합, G-243의 PR 전 snapshot 수동 소유 보호, G-245의 일반 Planner lifecycle, G-183/G-241의 성공 입력 증명과 책임을 나눈다. snapshot 밖 G-242는 수정하지 않으며 그 과거 새 호출 설명을 이 작업의 실행 지시로 사용하지 않는다.
- 2026-10-15 delta: 원격 R-082의 호출 축소와 직접 반영된 `stage_documents`·선택·회귀를 대조해 위 잔여 연결을 Worker 책임으로 정합화했다. ID·경로·dependency와 이전 수행 기록은 보존하고 구현·테스트·verify를 실행하지 않았다.
- complexity high: 문서 갱신·진행 중 Git operation·저장 Worker 결과·phase·원인별 차단의 서로 다른 확정 지점을 중단/재개에도 맞춰야 한다. risk high: 오래된 READY 소비나 잘못된 원인 해제는 최신 조건 미충족 후보의 검증·게시 또는 사용자 작업 충돌을 만들 수 있다. 원형·입력·turn·checkpoint·실패 감사를 남겨 수동 재평가·복구할 수 있어야 하며 이미 발생한 게시를 코드 rollback으로 되돌린다고 가정하지 않는다. 관련 경합·재개·무근거 성공 거부를 AC·Validation·Review에 포함한다.
