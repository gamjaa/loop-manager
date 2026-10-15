---
id: G-244
title: 사용자 문서 변경에 따른 draft 판단 대기 해소
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-071] 남은 규칙 기반 작업의 Manager 처리.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on:
  - G-243
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

사용자 원격 문서의 답변을 본문에 통합하고 동일 ID의 draft 대기를 안전하게 해소하는 경로를 완결한다. 현재 사용자 변경 관측·질문별 결과·Manager의 입력 정리 영수증과 게시/DB 적용은 직접 구현돼 있다. 이를 재사용해 정리 이후 새 조건 재평가, 충분한 출처·결과 검증, 부분 답변과 알림의 수명을 보완한다. 보존 후보의 완료한 구현·검증 기록은 유지한다.

R-061의 before/after 전문 제외와 catalog 중복 제거는 원격 base·보존 후보에 이미 구현됐으나, 여러 draft가 공유하는 동일 변경 diff는 별도 판단 문맥 안에서 반복된다. 남은 입력 축약은 이 공통 근거를 한 번 전달하면서 각 질문의 출처·판단 연결을 유지하는 범위다. 최신 R-053의 프로젝트별 draft 목록·ID 링크·분할 전송은 base에 직접 반영됐다. 이 구현을 재사용해 후보의 기존 결정 자동 재평가와 실제 사용자 질문을 목록 생성·전송 재검사 양쪽에서 구별하고, 부분/전체 해소 뒤의 목록 수명을 연결한다.

## Acceptance Criteria

- R-061: Planner에는 사용자 변경의 문맥 diff·출처·revision을 별도 판단 문맥에서 전달하며 before/after 전문을 반복하지 않는다. compact catalog는 질문·판단 identity만 전달한다. 여러 draft가 동일한 변경 근거를 공유하면 diff·출처는 한 번 전달하고 각 계획 ID·판단 revision·질문에서 그 근거를 식별할 수 있게 한다. 같은 경로·원격 revision이어도 변경 전 원문·변경 후 원문 또는 출처가 다르면 합치지 않는다. Manager는 계획별 저장된 before/after 원문과 검증된 추가 인용으로 출처·질문별 의미를 검사하고 Planner가 필요한 원문을 해당 원격 revision에서 조회할 수 있게 한다. 큰 문서의 작은 답변도 입력을 중복하지 않으며 이 축약을 근거/판단 대기 삭제로 대신하지 않는다.
- R-061: Requirement만의 답변, draft 표시를 유지한 본문 선택 및 유효한 User Decision을 모두 현재 질문·사용자 원문·원격 revision에 연결한다. 질문별 의미·충분성과 목표/완료 조건의 영향을 Planner가 판단하고 확인된 선택을 본문에 통합한다. 명시된 stale revision, reject/defer·불충분·상충·무관한 수정은 실행 승인이 아니다.
- R-061/R-064: Manager는 실제 사용자 변경과 Agent/Manager의 게시를 구별하고 원격 내용·계보·현재 입력을 검증한다. 마지막 author나 부분 인용만으로 서로 다른 작성자의 변경을 통째로 사용자 답변으로 인정하지 않는다. 결과의 중복 marker/key·잘못된 필드/질문·누락·상충·오래된 근거는 대기로 보존하며 자기 승인을 허용하지 않는다.
- R-061: 모두 해소되면 선택은 본문에, 필요한 기술 이유는 Decision Log에 남기고 Manager만 해결된 Draft Decision·User Decision 입력과 draft를 정리한다. 일부 해소에서는 해당 임시 입력만 소비하고 미해결 질문·관련 사용자 입력·Facts/Options/Impact·draft를 보존한다. 확인한 사용자 Requirement는 Planner가 대신 수정하지 않는다.
- R-061/R-082: 정리 결과·사용자 원문·출처·질문별 판단·현재 입력/소유권과 원격 게시 확인을 기존 영수증·publication transaction으로 결합한다. 게시 전 경합·중단·재시작에서 gate를 성급히 열지 않으며 같은 결정을 재요구하거나 입력 양식을 복원하지 않는다. 수동 준비/소유·실제 PR·started 보호는 G-243을 따른다.
- R-061: 해소 후 양식이 없는 정상 재관측은 같은 결정을 유지하되, 관련 목표·source·질문이 실제로 바뀌면 이전 결정의 유효성을 다시 평가한다. 무조건 return하여 새 조건을 놓치거나 모든 prose 변경을 새 승인 요구로 바꾸지 않는다. 새 모호함의 draft 전환은 미착수·무소유 경계 안에서만 한다.
- R-053/R-061: 프로젝트별 목록은 실제 사용자 질문의 판단 대기만 ID·제목·확인된 원격 ID 링크로 표시한다. 양식 정리 후 기존 결정의 자동 유효성 재평가(`reassessment`)는 실행 보류·감사를 유지하되 사용자 질문 목록과 건수에는 포함하지 않는다. 새 모호함으로 실제 질문이 생기면 그 질문의 identity·revision으로 목록에 포함한다. 목록 생성과 전송 전 구성·판단 근거 재검사는 같은 대상을 기준으로 하며, 자동 재평가 때문에 다른 미해소 draft의 유효한 목록이 취소되지 않는다. 부분/전체 해소·질문 교체 뒤 오래된 목록의 미전송 조각을 취소하고 이미 전송한 감사는 보존한다. 같은 목록·판단 근거와 빈 목록을 반복 통지하거나 과거 질문을 최신 링크로 재요청하지 않는다. 정책 적용 전 개별 draft 알림의 payload·고정 텍스트·전송 진행률은 유지하며 정책 변경만으로 취소·재작성하지 않는다.
- R-061/R-071/R-082: 해소 뒤에도 실제 dependency Merge·실행·게시 gate를 적용한다. 미해소 draft/의존 작업·catalog hold·수동 소유·독립 실패/session/level은 보존하고 독립 작업은 계속한다. 완료·Requirement archive에서 판단 대기를 누락하지 않는다. 제품·설계·고정 지침의 실제 계약을 정합하게 유지하며 G-171의 다른 Worker 문서 목표는 추가하지 않는다.
- 최신 R-082의 호출 축소에서도 실제 사용자 답변의 의미 판단과 새 관련 명세로 인한 기존 결정 재평가는 유지한다. 순수 수행 기록·코드·ExecPlan 변경만의 점검이나 작업별 구현 적합성 Planner를 만들지 않는다. 관련 Requirement 완료 확인의 pending gate·질문별 근거와 일반 planning·프로젝트 최종 확인 receipt를 구분한다. 이 작업은 이미 직접 반영된 호출 선택·Requirement 완료 점검 전체를 다시 구현하지 않는다.

## Implementation

- [x] `loop_manager_internal/storage/execplan_draft_state.py:prompt_evidence()`의 문맥 diff projection, catalog의 `include_changes=False`와 `loop_manager_internal/planning/validation_ports.py`의 별도 근거 전달 및 `tests/test_execplan_drafts.py`의 대형 원문 회귀는 원격 base와 보존 후보에 이미 있다. Manager의 원문 저장·추가 인용 검사와 미해소 대기를 유지하며 새 입력 계약에 대한 재구현을 지시하지 않는다.
- [ ] R-061의 공통 변경 근거 1회 전달을 `validation_ports.py:_PlanningValidationPorts.planner_prompt()`의 최종 입력 조립에 완결한다. 현재 `decisions`는 각 계획의 `prompt_evidence()`를 그대로 포함해 같은 PRODUCT·ARCHITECTURE 변경을 계획 수만큼 반복한다. 기존 projection을 재사용해 동일한 경로·원격 revision·commit·author·검증 여부와 before/after 원문을 가진 근거만 공유하고 계획별 질문·판단 revision·source 연결은 유지한다. 실제 전달 형식의 안내도 같은 조립 경계에서 맞춘다. DB 원문·결정 감사·질문별 인용 검사·부분 답변 보존을 공통 저장으로 옮기거나 중복 제거를 승인·질문 해소로 취급하지 않는다.
- [x] `loop_manager_internal/catalog/draft_decisions.py`의 구현된 사용자 변경·질문별 판정·정리 handler를 재사용한다. 보존된 질문부터 답변까지 실제 변경의 출처를 대조하고 기존 게시/PR/catalog 감사로 설명되는 Agent 내용, 혼합 author·rename·조회 실패의 불확실성을 보존한다. 결과 형식은 기존 엄격한 JSON 처리 helper를 먼저 확인해 재사용한다. R-061/R-064의 의미 판단과 Manager 검증을 분리한다.
- [x] `loop_manager_internal/storage/execplan_draft_state.py`의 해소 후 조기 반환을 현재 scope/source 근거와 대조한다. 동일 결정을 잃지 않으면서 새로운 관련 조건을 재판단할 수 있도록 이전 질문·해소 감사·근거를 유지한다. 현재 테스트의 양식 정리 뒤 재관측과 양식이 남은 과거 승인 재평가를 새 조건 변경까지 연결한다.
- [x] `draft_decisions.py`의 부분 정리에서 남은 질문과 미소비 사용자 입력을 보존한다. Manager가 확인한 부분만 정리하고 Agent의 User Decision 편집·draft 자기 해제 금지는 유지한다. 본문/관계 hash와 실제 사용자 변경을 정리 전후에 결합한다.
- [x] `loop_manager_internal/planning/planner_validation.py`, `validation_ports.py`, `planner_publication.py`와 `loop_manager_internal/storage/planning_publication_state.py`의 구현된 결과·영수증·manifest를 재사용해 경합·중단 재개를 완결한다. G-243의 수동 준비/소유·실제 PR 보호를 소비하며 Worker scope·실행 이력을 덮어쓰지 않는다. R-082 게시 확인 전 pending 해제는 금지한다.
- [x] `loop_manager_internal/notifications/events.py`, `incidents.py`, `work_exhaustion.py`에서 R-053의 질문 사건·링크·미전송 수명을 질문별 전환과 맞춘다. 새 알림 전송 도구나 일반 알림 정책을 도입하지 않는다.
- [ ] 최신 R-053 목록 구현에 후보의 질문 해소·재평가 처리를 연결한다. `notifications/events.py:enqueue_draft_decisions()`와 `delivery.py:_claim_piece()`는 현재 모든 `pending_ids()`를 목록 대상으로 삼지만, 보존 후보의 `storage/execplan_draft_state.py:observe()`는 질문 없는 자동 재평가도 `pending=True, reassessment=True`로 보존한다. 이 실행 gate는 유지하고 기존 evidence로 실제 사용자 질문의 목록 대상을 일관되게 구별한다. 기존 개별 알림용 후보 변경과 공개 export·관련 테스트를 최신 목록 API에 맞추고 `notifications.py`·`work_exhaustion.py`의 호출·상태 의미를 대조한다. 해소/교체 뒤의 취소는 현재 근거와 연결하고 정책 전환만으로 legacy 알림을 취소하지 않는다. base의 목록 중복 제거·고정 payload·text_link 및 UTF-16 조각 처리·재시도·감사를 재사용하며 이미 Merge된 목록 기능의 재구현을 지시하지 않는다.
- [x] `tests/test_execplan_drafts.py`, `tests/test_incremental_planning.py`의 직접 구현 회귀와 기존 publication/notification fixture를 확장한다. PRODUCT·ARCHITECTURE·Planner 지침은 이미 변경된 부분을 재작성하지 않고 실제 남은 구현 차이만 정합화한다.
- [ ] 보존 후보의 결정 재평가를 최신 `storage/execplan_draft_state.py:needs_semantic_review()`·`planning/state_ports.py`와 결합한다. `tests/test_planning_selection.py`의 검증된 답변에 대한 단일 호출 fixture와 기존 해소/부분 답변 회귀를 재사용해 실제 질문 답변·관련 명세 변경·질문 없는 자동 재평가를 구분한다. 같은 입력 재시작·Progress 변경에 새 호출을 추가하거나 알림 제외를 위해 pending gate를 지우지 않는다. 최신 base와 후보 결합은 Manager/Worker가 담당하며 현재 Planner는 후보를 수정하지 않는다.

## Validation

- 기존 `ExecPlanDraftCatalogTest.test_planner_uses_change_diff_once_and_preserves_full_decision_evidence`는 draft 하나만 구성한다. 해당 실제 prompt 조립 fixture를 확장해 여러 draft가 공유하는 대형 before/after의 작은 diff가 전체 prompt에 한 번만 있고 catalog에는 없으며, 모든 계획의 질문·판단 revision·source 연결과 DB 원문·미해소 대기가 보존되는지 확인한다. 같은 경로·revision의 서로 다른 before/after·출처와 공유/고유 근거가 섞인 경우에는 각 근거를 구별해야 한다. 공유 근거를 인용한 계획별 `prepare()`와 부분 해소도 기존 실제 parser·SQLite fixture로 확인하고 다른 질문을 함께 해소하지 않아야 한다. Worker는 수정한 최소 case/class만 실행하며 이미 있는 단일 draft 회귀를 중복 작성하거나 과거 통과를 최신 합성 후보의 성공으로 전환하지 않는다.
- 실제 SQLite·parser·Manager handler에 모의 Planner/Git 관측을 제공해 본문/Requirement/User Decision의 완전·부분 해소, revision 생략·stale 명시, reject/defer·상충·형식 중복·무관한 변경을 검사한다. 부분 해소에서 남은 질문과 사용자 원문이 보존되어야 한다.
- 양식이 정리된 해소 완료 상태에서 동일 입력 재관측, 무관 prose 변경, 관련 source/목표 변경과 새 모호함을 대조한다. 기존 감사가 유지되고 유효성 재평가를 건너뛰지 않아야 하며 실제 실행 이력 있는 작업을 draft로 되돌리지 않는다.
- 새 호출 선택과 결합한 최소 회귀에서는 검증된 본문/User Decision 답변·관련 상위 명세 변경에 필요한 판단은 한 번 수행하고 같은 입력·순수 Progress·코드 전진만으로 일반/점검/문서 영향 Planner를 반복하지 않아야 한다. Requirement만의 완료 점검과 일반 planning·최종 전문 확인의 성공 근거를 서로 대신하지 않아야 한다.
- `tests/test_execplan_drafts.py`의 `test_cleaned_resolution_reassesses_changed_inputs_without_reasking_user`와 `tests/test_notification_delivery.py:DraftListNotificationTest`의 실제 SQLite fixture를 재사용해 해소 게시 → 관련 입력 변경 → 자동 재평가와 다른 미해소 draft의 공존 → 유효성 재확인 또는 새 질문 생성을 목록 생성·전송 재검사까지 연결한다. 자동 재평가의 pending gate·해소 감사는 보존되고 사용자 질문 건수에서는 제외되며 다른 유효한 목록은 전송돼야 한다. 실제 새 질문과 부분 해소는 현재 identity·revision의 목록에 반영돼야 한다. 후보의 기존 개별 알림 테스트를 이 경계로 옮기고 이미 있는 목록 표시·중복·UTF-16 링크 테스트를 복제하지 않는다. 오래된 목록이 일부 전송된 뒤 질문이 해소되는 조건에서는 남은 조각만 취소하고, 동일 근거의 legacy 개별 알림은 원래 payload·텍스트·진행률로 이어 보내며 sent 감사는 보존해야 한다. 외부 전송은 기존 mock adapter만 사용한다.
- 혼합 작성자·Manager 게시 재현·rename·계보/조회 실패, snapshot 후 수동 intent/membership·실제 PR 출현과 원격 입력 경합에서 무근거 정리·gate 해제가 없어야 한다. 독립 계획은 계속 선택 가능해야 한다.
- `tests/test_planning_publication.py`, `tests/test_notification_events.py`, `tests/test_notification_incidents.py`, `tests/test_notification_delivery.py`의 관련 fixture로 정리 전/후·push 후 DB 미적용·transaction 중단과 재시작을 확인한다. 남은 질문 알림·미전송 취소·과거 전송 감사·중복 방지와 dependency/완료/archive 회귀를 유지한다. 실제 모델·외부 알림은 호출하지 않는다.
- 부분/전체 verify의 회귀 조건은 기존 명시적 결정·자기 승인 거부·Worker 기록 보존·수동/PR 보호·판단 대기와 게시 복구다. R-082 문서 판단과 R-086 선행 Review 성공을 사용자 결정 근거로 사용하지 않는다.

## Progress

- [ ] 최신 R-082 호출 선택과 보존 후보 결정 재평가의 결합 회귀. 복수 diff 축약·알림 목록 연결과 함께 남은 조건이며 과거 단일 draft/개별 알림 검증과 구분한다.
- [ ] 복수 draft의 동일 변경 diff 중복 제거와 질문별 연결·서로 다른 근거 보존 회귀. 단일 draft의 기존 projection 구현·통과 기록과 구분한 잔여 조건이다.
- [ ] 최신 목록 알림과 후보의 자동 결정 재평가·부분/전체 해소 연결. 아래 기존 개별 알림 구현·검증 기록과 구분한 잔여 조건이며 최신 base와 결합한 검증은 아직 하지 않았다.
- [x] 출처·결과 검증과 부분 입력 보존: 경로의 전체 변경 author·Manager 감사를 확인하고 공통 strict JSON 파서를 재사용한다. 현재 ExecPlan 원문/source hash와 인용을 대조하며 부분 해소에서 미소비 답변·출처·판단을 보존한다.
- [x] 양식 정리 뒤 새 조건 재평가·중단 재개·현재 알림 연결: 일반 계획 scope와 source 입력을 비교하고 기존 해소 감사로 Planner가 유효성을 재검토한다. 유효한 기존 선택은 이유를 Decision Log에 기록해 기존 영수증·게시 transaction으로 반영하며, 새 모호함은 같은 ID의 draft로 남긴다. 기존 prepared cleanup·transaction 중단 회귀를 유지하고 과거 질문의 미전송 알림은 현재 질문·revision·링크와 대조한다.
- [x] targeted unit 검증과 관련 문서 정합성: PRODUCT·ARCHITECTURE와 동적 Planner 지침을 갱신했다. 검증 adapter는 `unittest` v1, 프로젝트 Python은 launcher 선택이며 지원 범위는 Python 3.11–3.14다. 변경된 코드·테스트는 기존 `scripts/verify-inputs.json`의 `loop_manager_internal/**`·`tests/**` 범위에 포함되므로 입력 목록 변경은 필요하지 않다.

검증 기록: `python3 -m unittest tests.test_execplan_drafts tests.test_notification_events.TimelyProgressEventTest.test_draft_notice_tracks_question_revision_and_preserves_sent_audit`를 실행했다. 첫 실행은 알림 테스트의 기존 `support` import 경로 때문에 수집 오류로 실패했다(성공으로 간주하지 않음). `tests.support` 경로로 수정하고 동일 명령을 재실행해 통과했다. 최종 재실행은 43 tests, OK였으며 skip 없이 통과했다. 실제 Agent·외부 알림은 호출하지 않았으며 전체 suite·integration·verify는 실행하지 않았다. 전체 검증과 게시 경합의 통합 회귀는 Manager 검증에 남긴다.

검증 피드백 보완(P1, verified-answer-lost-on-reobservation): `user_changes`가 같은 관측 원문이라는 이유로 미소비 답변 근거를 지우던 원인을 수정했다. 현재 원문과 저장된 after가 같으면 원래 출처·revision·before/after를 보존한다. Manager가 일부 질문을 정리해 ExecPlan bytes가 바뀐 경우에는 현재 hash와 해소 게시 감사의 hash·동일 사용자 근거를 함께 확인해 남은 답변을 연결한다. 일치하지 않는 근거의 삭제와 현재 인용·source hash 검증은 유지한다.

`DraftAnswerReobservationTest`는 근거를 직접 주입하지 않고 Git 조회만 mock으로 제공해 실제 `user_changes → catalog/observe → 같은 원격 재관측 → prepare`를 실행한다. 본문 답변, Requirement 답변의 부분 해소 뒤 재관측, 본문 부분 해소 뒤 게시 감사 연결, 답변 철회 후 거부를 검사한다. 첫 재현에서 본문·Requirement 2개가 실패했고, 원문 일치 보존 수정 뒤 3개가 통과했다. 본문 부분 해소까지 추가한 뒤 1개가 실패해 게시 감사 연결을 보완했으며, 동일 명령의 최종 재실행은 4 tests, OK였다.

FAILED_COMMAND: python3 -m unittest tests.test_execplan_drafts.DraftAnswerReobservationTest
Initial run: FAIL (본문·Requirement 재관측 및 본문 부분 정리에서 근거 유실 재현).
RERUN: python3 -m unittest tests.test_execplan_drafts.DraftAnswerReobservationTest
Final run: PASS (4 tests, OK, skip 없음; 직전 교정 세션 실행). 이전 targeted 43개와 전달된 부분 verify 723개 PASS는 이번 교정의 검증으로 재사용하지 않았고 재실행하지 않았다. 전체 verify는 Worker가 실행하지 않았으며 Manager 후속 검증에 남긴다. 변경 코드는 기존 verify 입력 glob에 포함된다.

결과 보고 교정: 직전 응답은 최초 실행 결과인 FAIL을 FAILED_COMMAND와 RERUN 사이에 명시하지 않아 결과 parser가 미해결 targeted 오류로 거부했다. 실패 이력과 실제 최종 통과 결과를 유지하며 위 보고 형식을 교정했다. 현재 `parse_worker_result`를 직접 호출하는 단위 self-check에서 이전 보고의 거부와 교정 보고의 수용을 확인했다(PASS). 코드·테스트는 추가 수정하지 않았고 이미 통과한 4개 회귀는 재실행하지 않았다.

진행 기록 수정 보완(P1 occurrence 2/revision 2): 부분 해소 게시 뒤 Progress만 수정해도 `user_changes`가 남은 답변을 이미 포함한 새 before로 덮어쓰던 문제를 재현했다. 원격 계보·이전 원문 조회 이후 동일한 scope·source 입력·User Decision의 검증된 ExecPlan 답변은 원래 출처·revision·before/after를 유지하도록 수정했다. Planner의 인용 검증과 답변 철회 거부는 유지한다.

`DraftAnswerReobservationTest`의 기존 실제 관측·저장·부분 정리 fixture를 재사용해 부분 게시 → Progress 수정 → 같은 원격 재관측 → 남은 답변 prepare를 연결했다. 원래 근거와 판단 revision의 보존 및 최종 정리를 확인하고, Progress와 함께 답변을 철회하면 해소를 거부하는 조건을 검사했다. 최초 실행에서 근거 덮어쓰기를 재현했고 수정 후 동일한 두 case만 재실행했다.

FAILED_COMMAND: python3 -m unittest tests.test_execplan_drafts.DraftAnswerReobservationTest.test_body_answers_survive_progress_edit_after_partial_cleanup tests.test_execplan_drafts.DraftAnswerReobservationTest.test_progress_edit_does_not_preserve_withdrawn_body_answer
Initial run: FAIL (Progress 수정 뒤 원래 답변 근거 덮어쓰기 재현).
RERUN: python3 -m unittest tests.test_execplan_drafts.DraftAnswerReobservationTest.test_body_answers_survive_progress_edit_after_partial_cleanup tests.test_execplan_drafts.DraftAnswerReobservationTest.test_progress_edit_does_not_preserve_withdrawn_body_answer
Final run: PASS (2 tests, OK, skip 없음).

기존 targeted 4개와 전달된 부분 verify 727개 PASS는 이번 교정의 검증으로 재사용하거나 재실행하지 않았다. 선택 adapter는 unittest v1·launcher Python, 지원 범위는 3.11–3.14이며 코드·테스트는 기존 verify 입력 glob에 포함된다. 전체 verify와 필수 후속 단계는 Manager 검증에 남긴다.

source 선언 조회: R-061·R-053·R-071의 기존 정확 경로와 동일 파일명 archive는 없고, 고유 R-ID로 각각 활성 `[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md`, `[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md`, `[R-071] 남은 규칙 기반 작업의 Manager 처리.md`를 확인했다. `sources` 문자열은 보존했으며 공식 선언 교정과 DB 반영은 Planner·Manager에 맡긴다.


이전 planning의 조사 기록(당시 미실행 상태): 원격 직접 구현의 사용자 변경 관측, 완전/부분 답변, revision 생략, 정리 영수증과 게시/DB 결합 및 관련 회귀를 정적으로 확인했다. 이전 조사와 보존 verify summary는 새 잔여 조건의 검증 성공이 아니며 이 ExecPlan의 구현·테스트를 실행하지 않았다.

2026-10-15 planning 재평가: snapshot의 기존 ID·경로와 보존 후보 `027ee749aefc16def78a6ffccd59e24344eefec7`의 구현 체크·실패/재실행·검증 보고를 이 문서에 보존했다. 기록은 기존 Worker의 실행 보고이며 현재 Planner가 재실행하거나 최신 base 결합의 verify/Review 성공으로 확인한 것이 아니다. 최신 R-061 입력 축약은 현재 base·후보의 동일한 projection/회귀에서 이미 구현돼 있어 이 조건의 새 미충족 구현은 발견하지 않았다. 세 활성 source의 고유 R-ID와 현재 경로를 확인해 공식 선언만 정합화했다. 후보 worktree·Git·실행 이력은 읽기 전용으로 유지했다. 원격 base의 targeted runner·Worker 지침 변경은 일반 재개에서 현재 지침과 호출 환경으로 소비하며 과거 targeted 실행을 새 기록으로 합성하지 않는다. 최신 합성 후보의 검증·Review·게시 판정은 Manager에 남아 있고 현재 Planner는 구현·테스트·전체 verify를 실행하지 않았다.

보존 후보 `13f5e92aced2c6ccf7c8bf8804f2f4d2b9fb85da`의 미커밋 Progress 두 문단도 읽기 전용으로 확인했다. 아래는 당시 Worker 보고 원문이며, 현재 Planner의 수행이나 최신 원격 `5f49ff2f087d7108d92b481ad1a3bee98b0839cb`와 결합한 검증 성공이 아니다.

2026-10-15 관리 rebase 후 Worker 재검증: 현재 HEAD `13f5e92`와 base `269e6ce`의 최종 diff 전체를 확인하고 현재 PRODUCT·ARCHITECTURE·활성 sources와 이 ExecPlan을 다시 읽었다. Planner·Manager가 공식 반영한 현재 source 경로는 모두 존재하며 frontmatter는 수정하지 않았다. 최신 R-061의 diff 입력 축약·DB 원문 보존 조건은 기존 projection과 관련 회귀에 반영돼 있어 추가 구현은 필요하지 않았다. 기존 구현·실패/교정 이력은 보존하고 이전 후보의 성공을 재사용하지 않았다.

현재 후보에서 `python3 -m unittest tests.test_execplan_drafts tests.test_notification_events.TimelyProgressEventTest.test_draft_notice_tracks_question_revision_and_preserves_sent_audit`를 새로 실행했다. 결과는 49 tests, OK, skip 없음이다. 명시된 대형 문서 projection 회귀·결정 재평가·부분 답변·반복 관측·Progress 수정·철회와 현재 질문 알림 검증을 포함한다. unittest v1·launcher Python과 기존 verify 입력 glob·필수 단계는 유지했다. 코드·테스트를 추가 수정하지 않았고 전체 suite·integration·verify·Review는 실행하지 않았다. 전체 verify와 최종 Review·게시 판단은 Manager가 수행한다.

## Decision Log

- full 검토 기준은 `d99e91f5f47ccdadc7e95c2541922b01a8e3a88b`다. 앞서 전문 확인한 PRODUCT·ARCHITECTURE·활성 Requirement 중 변경된 세 문서와 실제 목록 코드·테스트를 재검토했다. base의 목록 알림은 직접 구현된 기준으로 유지한다. 보존 후보는 개별 알림에서만 `reassessment`를 제외하고 새 base의 목록 생성·전송은 모든 pending ID를 사용하므로, 같은 G-244 안에서 사용자 질문과 자동 재평가의 대상 판정을 연결한다. 내부 gate를 해제해 알림을 줄이거나 개별 알림 생성 경로를 되살리지 않는다. 기존 개별 알림 기록과 Merge된 목록 구현을 최신 결합 후보의 검증 성공으로 취급하지 않는다.
- 현재 base와 읽기 전용 보존 후보 `13f5e92aced2c6ccf7c8bf8804f2f4d2b9fb85da` 모두에서 공통 diff의 계획별 반복과 단일 draft 회귀를 확인했다. 전달 표현만 공유하고 질문별 원문·출처·판단 수명은 유지한다. 후보 보고의 targeted 49 tests, OK, skip 없음은 복수 draft 및 최신 목록 연결 조건의 성공 근거가 아니다. 기존 metadata·dependency·구현/실패/검증 기록을 보존하며 후보 worktree·Git·실행 이력은 수정하지 않는다. 현재 Planner는 구현·테스트·verify를 실행하지 않았고 새 ExecPlan이나 사용자 승인을 만들지 않는다.
- 최신 R-061의 입력 축약은 기존 의미 판단·원문 인용 검증·부분 해소 보존과 양립한다. diff는 전달 표현이고 저장된 원문·사용자 출처는 판단 원본이다. 이미 구현된 projection과 대형 문서 회귀를 재사용하며 G-277·G-281·G-284·G-286·G-288의 무관한 변경을 질문 해소로 승격하지 않는다. 시작한 G-244의 identity·수행 기록을 유지하고 새 draft나 중복 구현 계획을 만들지 않는다.
- 직접 구현된 해소 경로를 재구현하지 않는다. 정리된 양식의 부재를 재승인 이유로 쓰지 않는 보호와, 새 관련 입력을 재검토하는 R-061 조건을 함께 지켜야 한다. `observe`는 정리 뒤 scope/source 변경을 유효성 재검토 대기로 보존한다. 기존 선택의 재사용은 Planner의 현재 revision·이유와 이전 해소 감사에 결합하며, 동일 입력과 진행 기록 변경은 새 사용자 승인을 요구하지 않는다.
- 일부 질문 해소에서는 질문별 인용과 정확히 일치하는 답변 줄만 소비한다. 하나의 자유 문장이 여러 질문에 걸치면 안전하게 분리할 수 없으므로 남은 질문과 함께 보존한다.
- G-243은 수동 개정 권한, G-242는 저장된 DOCUMENT_RESULT·legacy 게시/PR 경합을 담당한다. 새 작업별 문서 영향 Planner를 호출하지 않는 R-082를 유지하며 이 작업은 DRAFT_DECISION_RESULT·사용자 출처·질문별 수명에 한정한다. 기존 ID/경로/source·dependency와 수행 기록을 보존한다.
- 최신 snapshot `b1f123e7ed66ec9f15dd83f86565d738d5600745db1274ba2bca9d834fdf0e6a`·원격 `5f49ff2f087d7108d92b481ad1a3bee98b0839cb`에서 R-061/R-064/R-082의 호출 조건 변경을 대조했다. 보존 작업은 초기화하지 않으며 기존 두 잔여 목표에 결정 재평가/호출 선택 연결만 보강한다. 다섯 draft의 사용자 변경은 호출 조건·유지보수·Requirement 완료 점검에 관한 것으로 기존 질문의 권한·원격 삭제·파일/index 복원·PR 보고·루트 적용 범위를 해소하지 않는다. 해당 draft·User Decision은 편집하지 않았다.
- complexity high: 혼합 출처·부분 해소·정리 후 새 조건과 원격/DB/알림 사이 중단을 동일 결정 감사로 연결하는 판단이 남아 있다. risk high: 오판하면 사용자 선택 없는 실행/게시 또는 답변 손실이 생긴다. 원문·근거·정리 영수증·publication intent를 보존해 수동 재평가/복구하며 출처·부분 입력·경합·중단 시나리오로 gate 보존을 검증한다.
- 과거 보존 verify summary의 3,356 성공·5 skip은 당시 후보의 기록이다. 이번 잔여 R-061 조건에 재사용한 PASS를 만들지 않으며 선택된 unittest 수집·compile과 실제 Agent guard를 유지한다.
