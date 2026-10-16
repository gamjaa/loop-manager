---
id: G-293
title: 승인 수정본의 Planner 호출 제거와 기존 작업 전달
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

문서 수정본 승인 자체로 Planner를 호출하거나 별도 ExecPlan 생성을 강제하는 잔여 경로를 제거한다. 적용할 기존 작업과 승인이 확정돼 있으면 현재 작업의 실행 입력으로 전달하고, 독립적인 명세 변경·사용자 답변·Requirement 완료 점검은 본래 목적대로 처리한다.

현재 `PlanningSelectionService`는 `pending_document_approval()`을 일반 planning 조건에 포함한다. 이 port는 `DocumentDraftRepository.unplanned_approved()`를 읽으며, `validation_ports.py`는 승인 후보가 있으면 정상 prompt를 전용 계획 생성 지시로 교체하고 결과도 새 계획 하나로 제한한다. `tests/test_planning_selection.py:test_new_document_approval_forces_planning_with_unchanged_fingerprints`는 그 옛 동작을 요구한다. 코드 전용 점검·새 작업별 문서 영향 Planner 제거와 Requirement 완료 점검은 이미 직접 구현돼 있어 이 작업에서 재구현하지 않는다.

## Acceptance Criteria

- R-082: 명세·실제 draft 답변 등 독립적인 호출 조건이 없으면 승인 수정본의 존재·재관측만으로 일반·점검·문서 영향 Planner를 호출하지 않는다. READY 있음/없음/dependency 대기, 실행 재개와 재시작에서도 같은 기준을 적용한다.
- 승인 수정본과 적용할 기존 작업의 명시적 연결·repository·현재 선언·소유권·승인 revision/원문/bytes·source 관계가 확인되면 해당 작업의 기존 Worker 실행 입력으로 전달한다. 같은 session·작업물·실패/level·dependency·사용자 판단·실제 PR gate를 보존하고 구현 적합성 확인과 필요한 보완은 그 세션에서 함께 수행한다. 승인 자체를 구현·검증·게시 성공으로 쓰지 않는다.
- 적용 대상이 없거나 모호함·stale 원문·취소/종료한 작업·수동 소유·PR 이후 범위 확장·권한 불일치가 있으면 원문·승인·미적용 이유를 보존한다. 경로나 유사한 source만으로 작업을 임의 선택하거나 새 ExecPlan을 만들지 않으며 독립 정상 작업과 유효한 Planner 판단을 막지 않는다.
- 명세 변경·실제 ExecPlan draft 답변·Requirement 완료 점검 등 별도 목적의 Planner 호출에서는 승인 후보가 prompt·결과를 전용 새 작업 하나로 가로채지 않는다. 각 목적의 판단·게시·receipt를 유지하며 승인 수정본과 R-061의 draft 사용자 답변을 서로 대신하지 않는다.
- Worker의 모든 Requirement·다른 ExecPlan·frontmatter 변경 금지는 유지한다. 기존 Manager의 승인 bytes 적용·원본 재검증·의미 보존 Review·PR/사용자 Merge 및 이미 게시한 문서 계획의 복구 계약은 보존한다. 이 작업으로 새로운 파일/index/Git 쓰기 권한이나 승인 예외를 만들지 않는다. 현재 권한으로 적용할 수 없는 대상은 미적용 상태와 이유를 보존한다.
- 전달 전/후·원격 입력 변경·중단/재시작에서도 같은 승인 artifact를 다른 작업에 재결합하거나 적용을 반복하지 않는다. 원본·수정본·승인/미소비 감사와 기존 publication intent를 보존하고 실제 상위 명세가 게시된 뒤에는 별도의 명세 변경 조건을 따른다.

## Implementation

- [x] 확인한 `planning/selection_service.py`·`planning/state_ports.py`의 승인 전용 trigger와 `planning/validation_ports.py`의 prompt 조기 반환·단일 계획 강제를 제거한다. 승인 상태는 전달/보류 판단으로 소비하고 R-082의 실제 Planner 목적·유효한 결과/게시 복구는 유지한다. 다른 문서 관련성 판단을 모델 호출로 옮기지 않는다.
- [x] 확인한 `documents/document_drafts.py`의 승인·`plan_id` 연결·`assert_current()`와 `execution/execution_documents_handler.py`의 mapping·실행 guard를 재사용한다. 기존 작업으로의 명시적 연결이 확인된 입력만 Worker/Manager의 현재 실행 경계에 전달한다. 새 대상 선택 heuristic·CLI·범용 상태 모델을 만들지 않는다. 원문·source 관계·권한·소유권·dependency/PR 조건의 검증 실패는 해당 artifact의 보존/보류로 남긴다.
- [x] `documents/document_publication.py`의 기존 전용 claim·승인 bytes·Review/게시 검증을 legacy 복구와 일반 작업 전달에서 구분한다. 과거 게시된 claim과 `R027_SOURCE`의 조회 호환은 보존하되 archive된 R-027을 새 계획 source로 요구하거나 활성 복원하지 않는다. 일반 작업을 옛 전용 계획으로 재작성하거나 Worker의 Requirement 수정 예외로 처리하지 않는다.
- [x] 확인한 `tests/test_planning_selection.py`·`tests/test_maintenance_state.py`·`tests/test_execution_documents.py`·`tests/test_maintenance_planning.py`의 실제 저장소/승인/guard fixture를 재사용해 호출 수·전달·stale/경합·복구 검사를 보완한다. PRODUCT·ARCHITECTURE·실행 지침은 실제 연결한 동작 설명만 정합화하고 사용자 Requirement는 수정하지 않는다.

## Validation

- Worker는 위 모듈의 변경한 최소 case/class를 실행한다. 실제 SQLite·artifact bytes·현재 실행 handler를 사용하고 Git/원격/provider 관측은 기존 double로 제공한다. 유료 Agent·외부 전송·새 Git 통합 실행을 추가하지 않는다.
- 승인만 변경한 READY 유무·dependency 대기·현재 작업 재개에서 Planner 호출은 0이어야 한다. 유효한 기존 작업 연결은 같은 Worker 세션의 입력에 정확히 한 번 전달되고, 무연결/stale/권한 불일치·종료/수동 소유/실제 PR 경합은 artifact와 독립 작업을 보존해야 한다.
- 승인 후보와 명세 변경·실제 draft 답변·Requirement 완료 재확인을 함께 구성한다. 본래 목적의 prompt·판단·결과/receipt가 전용 문서 계획 요구로 교체되지 않으며 필요한 판단은 한 번만 수행돼야 한다. 동일 입력 재시작은 유효한 결과와 미완료 적용을 재사용해야 한다.
- 적용 전 원격 원문/승인 bytes/작업 연결 변경과 전달 후 DB 적용 중단을 확인한다. 잘못된 대상 쓰기·재결합·중복 적용·임의 승인·verify/Review 성공 합성이 없어야 한다. 기존 전용 문서 작업의 CRLF 승인 diff·원본 stale 거부·의미/bytes Review·같은 PR 복구는 유지한다.
- 소유 중인 rebase에서 현재 workspace/input fingerprint가 재관측 사이 불일치하면 최신 service를 한 번 재구성해 같은 checkpoint로 전체 소유권 검사를 다시 수행한다. 재검사도 불일치하거나 다른 lease·scope 검사가 실패하면 계속 거부하며 fingerprint 불일치를 승인 근거로 사용하지 않는다.

## Progress

- [x] 원격 `5f49ff2f087d7108d92b481ad1a3bee98b0839cb`의 R-082 변경과 위 실제 trigger·prompt/결과 제한·문서 실행 guard 및 기존 회귀를 정적으로 대조했다. compact catalog에 이 잔여 전달/호출 제거 목표를 담당하는 비종료 작업은 없다.
- [x] 승인 전용 호출·결과 제한을 제거하고 source snapshot에서 하나로 식별되는 기존 자동 Worker에만 승인 artifact를 전달했다. no-link·ambiguous·stale·수동 소유·PR·현재 declaration 불일치에는 이유를 기록하고 적용을 보류한다.
- [x] Python 3.12·`unittest` adapter의 targeted runner로 Planner 선택/검증, legacy claim 복구, 기존 작업 전달, source/권한 보류, exact CRLF bytes와 단일 mapping을 확인했다. source/legacy 연결 변경 뒤 14개 case, persisted approval revision 재확인 뒤 4개 case, target mode·no-link 보류 재확인 3개 case, 최종 apply conflict·reason 회귀 3개 case, Planner·prompt·handoff 회귀 16개 case가 각각 통과했다. 전체 `scripts/verify`와 suite는 실행하지 않았다.
- [x] Manager 결과 검증의 미해결 targeted test 진단 뒤 Planner/source/권한/PR handoff 16-case selection을 동일 명령으로 새 runner 기록에 재실행했다. 16개 모두 통과했다. 전체 suite와 `scripts/verify`는 실행하지 않았다.
- [x] 관리 rebase 후 HEAD `61ecda61146f8b1db241b5983e743c094e0c68a3` 기준 diff를 검토한 뒤, Manager의 미해결 targeted test 진단을 받은 현재 후보에서도 Python 3.12·`unittest` adapter runner로 Planner 선택·source 검증·handoff·수동 소유·PR·CRLF 회귀 16개를 다시 실행해 모두 통과했다. 전체 suite와 `scripts/verify`는 실행하지 않았다.
- [x] 최신 R-082 revision `55446254671206a070427af9fd5c32428e667e27`의 rebase 조건을 반영해 Worker 충돌 prompt에서 Manager의 checkpoint/latest-revision 문맥과 승인 수정본 전달을 함께 보존했다. 소유 rebase의 결합 workspace/input mismatch는 current service로 한 번 재검증하고 반복 실패는 거부한다. 관련 Worker prompt·기존 작업 전달·소유 rebase 재검증 4개 case와 planning drain 중 base-fetch·ignored-file 보존·relative-import 4개 case가 targeted runner에서 통과했다. Git rebase 완료와 전체 verify는 Manager가 수행한다.

### Worker 검증 기록

- 승인 전용 trigger 제거: READY runnable·dependency 대기·작업 없음 각각에서 Planner/maintenance 호출 없이 기존 선택 상태를 유지한다.
- 기존 작업 연결: 단일 source link는 같은 Worker 입력에 전달되고 승인 bytes만 Manager가 적용한다. source 불일치·stale·ambiguous·manual owner·PR은 매핑 없이 승인과 이유를 보존한다.
- bytes·복구: mixed task 변경을 유지하고 target의 정확한 CRLF 승인 bytes·diff·mode를 검증한다. 기존 `DOCUMENT_DRAFT_ID` 문서 전용 claim과 R-027 source 복구는 유지한다.
- 최신 rebase 조건: Manager가 전달한 checkpoint·revision을 Worker prompt에 유지하고, owned rebase의 결합 identity/fingerprint mismatch만 최신 current service로 한 번 전체 재검증한다. 재검증 거부·다른 ownership 실패는 그대로 보존한다.
- planning drain: 작업 실행은 차단하되 Planner 선행 조건인 `base-fetch`의 Git 자원 대기·획득은 허용하며 runtime handoff 차단은 유지한다.
- `docs/PRODUCT.md`와 `docs/ARCHITECTURE.md`에 구현한 호출 조건·전달·보류·복구 계약을 반영했다. Requirement와 frontmatter는 수정하지 않았다.

## Decision Log

- R-082의 명시된 호출 축소와 기존 작업 전달만 구현한다. R-061의 질문별 의미 판단·공통 diff 축약·알림은 G-244, 저장 DOCUMENT_RESULT/legacy PR 경합은 snapshot 밖 G-242에 남긴다. 이 작업은 그 계획을 개정하거나 완료를 대신하지 않는다. G-239의 명령 Skill과 R-074의 0 지원도 별도 책임이다.
- complexity medium: 기존 승인 mapping·원본 검증·실행 입력의 경계가 있으며 필요한 변화는 호출 선택과 입력/결과 제한 및 전달의 연결이다. risk high: 잘못된 작업 연결·stale 승인 수용은 사용자 원문 덮어쓰기나 승인 밖 게시로 이어질 수 있어 코드 rollback만으로 복구할 수 없다. 명시적 identity·원문/bytes·소유권/PR 검증과 미적용 감사·기존 게시 복구를 유지하고 경합/중단 회귀로 완화한다. 새로운 Git 쓰기나 Worker 권한 확대는 구현하지 않는다.
- 새 계획 번호는 현재 active/archive 파일의 최대 ID 292 다음 293을 사용했다. `docs/FUTURE_IDEAS.md`의 항목을 목표로 사용하지 않았다. 선택 verify는 `unittest` version 1·launcher Python 3.11–3.14·독립 tests 수집·필수 compile이며 입력 glob은 기존 `loop_manager_internal/**`·`tests/**`가 포괄한다. Planner는 구현·테스트·verify·Git 게시를 실행하지 않았다.
