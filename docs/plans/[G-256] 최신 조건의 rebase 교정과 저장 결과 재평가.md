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

G-255의 문서 충돌 처리를 최신 Planner 조건·Worker 교정·검증과 저장된 결과/차단의 재평가에 연결한다. 직접 구현된 입력 변경 시 작업 보존·성공 token 무효화는 유지하고, 충돌 중 생략되는 문서 판단과 과거 교정 결과의 최신 입력 확인을 보완한다.

## Acceptance Criteria

- Worker 최초·재개·교정 진입 전에 필요한 프로젝트 일반 planning의 관련 개정 게시·DB 적용을 마치고 그 immutable base의 ExecPlan을 반영한다. PR 등록 뒤 기존 계획 개정 금지·수동 소유 보호·dependency 준비 조건을 지키며 Planner와 구현 worktree의 writer/Git operation을 겹치지 않는다. 소유 rebase가 이미 진행 중이면 기존 target·pinned 문서로 그 operation의 완료/복구를 먼저 처리하고 안전한 경계에서 남은 최신 명세 planning·반영을 이어간다. 작업별 상위 명세 영향 판단·새 plan_text 생성을 다시 도입하지 않는다.
- 문서-only 자동 해소 뒤 발견 단계와 실제 구현·검증 근거로 필요한 단계만 선택한다. 그 외 파일 충돌은 verify·Review·게시 단계에서 발견해도 같은 Worker session의 교정으로 연결하고, 최신 문서를 기준으로 양쪽 형상을 검토·선택·결합하며 필요한 targeted test를 수행한다. Reviewer의 읽기 전용 권한과 선행 Review·검증 보완의 기존 의미를 유지한다.
- 본문·sources·연결 명세·archive 범위·지침 변화와 코드 충돌이 함께 있거나 이전 definition 원문이 없어도, 현재 identity·소유권·유효한 최신 조건이 확인되면 보존된 작업의 교정·검증을 이어간다. 과거 성공을 재사용할 증거가 없으면 성공 근거만 무효화하며, 입력 변경 자체로 최종 차단하거나 session·작업물·실행/실패·level 감사를 폐기하지 않는다.
- 저장된 rebase Worker 결과와 pending stage/continue는 현재 후보·문서 조건과 결합해 소비한다. 파일 상태가 같아도 조건이 바뀌면 이전 결과의 READY나 scope marker만으로 새 조건 충족을 승인하지 않는다. 입력이 같은 완료 교정은 중복 호출하지 않고 필요한 후속 Git/검증 단계부터 재개한다.
- 이 입력 불일치만으로 이미 차단된 작업은 현재 소유 operation·조건·남은 교정을 재평가한다. 안전한 경로가 있으면 해당 원인만 해제하고 기존 자원과 session을 이어간다. 실제 위반·소유권/문서 유효성 불확실·필수 사용자 결정·미충족 dependency·진전 없는 반복은 기존 한도와 영향 범위의 보존·차단을 유지하며 단순 지문 변경을 요구하거나 무한 교정을 만들지 않는다.
- Planner 게시·문서 적용·Worker 결과 저장·Git 계속 진행·차단 해제 사이 중단과 추가 원격 전진에서도 기존 target·원형·실제 확정 지점을 보존한다. 완료한 소유 operation 뒤 최신 base 반영을 이어가고, 최신 조건의 targeted test·필요한 Manager verify·최종 후보 Review와 게시 안전 조건 없이 구현 완료나 PR 게시를 승인하지 않는다.

## Implementation

`execution/stage_documents.py`의 ExecPlan 실행 대응·legacy document_revision 복구, `execution_revision_handler.py`의 정의/성공 token 판정, `execution_worker_handler.py`의 교정 호출·저장 결과 소비와 기존 rebase correction/checkpoint 저장을 연결한다. 직접 구현된 프로젝트 planning 전환과 plan_text=null 계약을 재사용한다. 최신 ExecPlan 조건·교정 turn의 입력과 과거 수행 근거를 구분하고, 진행 중 rebase는 기존 operation을 보존해 처리한 뒤 안전한 경계에서 일반 planning·게시·DB 적용과 남은 최신 base 반영을 이어간다. G-242의 의미·PR 검증을 소비하며 상위 명세의 개정을 document_revision에 다시 맡기지 않는다.

Merge된 G-255의 `git/rebase_documents.py`와 RebaseHandler의 문서 자동 진행을 원문 적용 경계로 재사용한다. `document_revision` 판단·게시와 `rebase_documents`의 pinned revision·교체 전 bytes·중간 HEAD·replay별 채택 이력은 서로의 성공 근거가 아니며 checkpoint에서 함께 보존한다. 진행 중 operation의 target·채택 revision을 새 Planner 결과로 바꾸지 않고, 해당 operation을 완료한 뒤 추가 원격 전진을 기존 최신 base 반영으로 처리한다. 최종 HEAD·index·작업 파일의 pinned 원문 대조는 최신 조건 교정·차단 재평가에서도 유지한다. 문서-only/혼합 분기·원문 교체·continue/skip·원문 보존을 다시 구현하지 않는다.

기존 rebase block 선택·현재 상태 수용·원인별 해제 서비스를 대조해 이 불일치의 잔여 재차단 경로만 교정한다. 이미 가능한 현재 상태 복구는 재구현하지 않는다. 조건 변경 시 runtime row의 token과 별도로 남는 저장 교정 결과의 입력 유효성을 확인한다. 새 완료 상태·재시도 엔진·비호환 schema를 만들지 않으며, NO_CHANGE와 다른 Git 차단의 전수 판단은 G-189에 남긴다. PRODUCT·ARCHITECTURE에는 실제 충돌 교정·단계 선택·실패 보존 동작만 반영한다.

## Validation

- `tests/test_execution_revision_gate.py`, `tests/test_stage_documents.py`, `tests/test_execution_rebase_correction_phase.py`·`tests/test_planning_selection.py`의 최소 targeted unit으로 operation이 없는 최초/재개/교정의 프로젝트 planning 게시·DB 적용→ExecPlan 적용→Worker 순서를 확인한다. 진행 중 rebase는 기존 target 완료/복구→추가 planning·최신 base 반영 순서이며 중첩 operation이 없어야 한다. 문서-only의 불필요한 Worker 억제와 그 외 파일/혼합 충돌의 단계별 복귀를 실제 handler·SQLite로 확인하고 Git/Agent 관측만 mock한다.
- 저장 Worker READY 뒤 source/본문 조건만 변경되고 scope marker는 같은 경우, 과거 definition 부재, pending stage/continue 재시작과 동일 입력 완료 결과를 대조한다. 새 조건의 무근거 성공 재사용·중복 호출·오래된 게시가 없어야 한다.
- `tests/test_rebase_correction.py`의 채택 이력·후속 uncertain·최종 원문 대조와 기존 lifecycle fixture를 재사용해 문서 채택과 `document_revision` 판단이 함께 있는 중단/재개를 확인한다. Planner 게시나 입력 무효화가 pinned revision·원형·기존 target을 덮어쓰지 않으며, 뒤늦은 비충돌 재생으로 달라진 문서는 완료로 채택하지 않는다. 동일 원문 적용 통합 경로를 불필요하게 반복하지 않는다.
- `tests/test_managed_rebase_lifecycle.py`, `tests/test_scope_block_revalidation.py`와 기존 현재 rebase 수용 회귀에서 과거 불일치 차단의 동일 입력 재평가, 안전한 원인별 해제 및 실제 위반·독립 차단·사용자 operation·dependency 대기 보존을 확인한다. 실제 Git/SQLite가 필요한 target·index·작업물 보존 효과는 기존 통합 fixture를 재사용한다.
- Manager 전체 verify는 원격 재전진·PR/소유권 경쟁·session/실패/level 보존, 반복 한도, 최신 후보 verify·Review·lease와 사용자 Merge gate를 유지한다. Reviewer는 새 조건 미검증의 PASS 채택, 다른 원인 해제·target 덮어쓰기·중첩 rebase를 blocking issue로 확인한다.

## Progress

- [x] 2026-10-10 `ab0264d94da1212d68141b28fc55f0204c1c9e09`의 delta와 관련 handler·저장 교정/차단 판정 및 테스트를 정적으로 확인했다. 구현·테스트·전체 verify를 실행하지 않았다.
- [x] 2026-10-13 `f56739c31a49ec8fb5547a459688bfb02772586e`의 G-255 원문 적용·재개·최종 대조와 현재 `stage_documents.refresh`·저장 Worker 결과 소비를 정적으로 확인했다. 전자는 현재 구현을 재사용하고 후자의 최신 조건 연결은 여전히 남아 있다. 기존 수행 기록과 미완료 항목을 보존하며 구현·테스트·verify는 실행하지 않았다.
- [ ] 최신 조건의 충돌 교정·저장 결과/차단 재평가와 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-14 delta 개정: 원격 R-082의 프로젝트 planning·작업 실행 대응 분리와 원래 소유 operation 우선 복구를 재사용하도록 현재 지시를 갱신했다. 직접 반영된 source 지문만의 Worker token 변화 제외는 일반 planning 완료를 전제로 하며 verify·Review 입력의 제외가 아니다. 저장된 rebase 교정 결과/차단의 최신 조건 연결이라는 남은 목표, 기존 분류·dependency·실행 기록을 유지한다. 구현·테스트·verify는 실행하지 않았다.

- 2026-10-13 delta 개정: mutable snapshot token `bbe557975a5581bd2185b8b767a35088a476de189a346cb28f0469aca8c109f7`, revision `f56739c31a49ec8fb5547a459688bfb02772586e`, 원본 hash `f13365260f2bb3c5cdc4f18ad1f7a635f2393ada7a2157bc2a75af0d884fc12a`와 일치하는 이 계획만 개정했다. G-255의 동일 ID archive 이동은 삭제 목표가 아니므로 dependency를 유지한다. ID·경로·metadata·목표·기록을 보존하고 새 원문 보존 경계의 재사용·회귀 연결만 구체화했다. 기존 high complexity/risk의 판단과 복구 조건을 유지하며 프로젝트 최종 전문 확인이나 구현 성공을 기록하지 않는다.

- 현재 `RevisionHandler.guard`는 충돌 중 입력 변경/과거 원문 부재에서 runtime 성공 token을 비우고 기존 session·rebase target을 보존한다. 해당 직접 구현과 회귀는 재사용한다. `stage_documents.refresh`는 resolving_rebase에서 판단을 생략하며, `_resume_saved_rebase_worker`는 저장 교정 결과를 파일 progress와 parser로 확인한 뒤 소비하므로 최신 문서 조건에 결합된 판정이 추가로 필요하다.
- G-242는 DOCUMENT_RESULT 형식과 PR 경합, G-243은 PR 전 snapshot의 수동 소유 보호, G-245는 일반 Planner lifecycle 경계, G-183/G-241은 성공 입력 증명을 각각 담당한다. 이 계획은 충돌 중 문서·교정 결과·차단 재평가의 연결만 맡고 기존 미충족 목표나 전수 감사를 대체하지 않는다.
- complexity high: 문서 갱신·진행 중 Git operation·저장 Worker 결과·phase·원인별 차단의 서로 다른 확정 지점을 중단/재개에도 맞춰야 한다. risk high: 오래된 READY 소비나 잘못된 원인 해제는 최신 조건 미충족 후보의 검증·게시 또는 사용자 작업 충돌을 만들 수 있다. 원형·입력·turn·checkpoint·실패 감사를 남겨 수동 재평가·복구할 수 있어야 하며 이미 발생한 게시를 코드 rollback으로 되돌린다고 가정하지 않는다. 관련 경합·재개·무근거 성공 거부를 AC·Validation·Review에 포함한다.
