---
id: G-244
title: 사용자 문서 변경에 따른 draft 판단 대기 해소
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
  - docs/requirements/[R-053] Timely Progress Notifications.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
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

사용자 원격 문서의 답변을 본문에 통합하고 동일 ID의 draft 대기를 안전하게 해소하는 경로를 완결한다. 현재 사용자 변경 관측·질문별 결과·Manager의 입력 정리 영수증과 게시/DB 적용은 직접 구현돼 있다. 이를 재사용해 정리 이후 새 조건 재평가, 충분한 출처·결과 검증, 부분 답변과 알림의 수명을 보완한다.

## Acceptance Criteria

- R-061: Requirement만의 답변, draft 표시를 유지한 본문 선택 및 유효한 User Decision을 모두 현재 질문·사용자 원문·원격 revision에 연결한다. 질문별 의미·충분성과 목표/완료 조건의 영향을 Planner가 판단하고 확인된 선택을 본문에 통합한다. 명시된 stale revision, reject/defer·불충분·상충·무관한 수정은 실행 승인이 아니다.
- R-061/R-064: Manager는 실제 사용자 변경과 Agent/Manager의 게시를 구별하고 원격 내용·계보·현재 입력을 검증한다. 마지막 author나 부분 인용만으로 서로 다른 작성자의 변경을 통째로 사용자 답변으로 인정하지 않는다. 결과의 중복 marker/key·잘못된 필드/질문·누락·상충·오래된 근거는 대기로 보존하며 자기 승인을 허용하지 않는다.
- R-061: 모두 해소되면 선택은 본문에, 필요한 기술 이유는 Decision Log에 남기고 Manager만 해결된 Draft Decision·User Decision 입력과 draft를 정리한다. 일부 해소에서는 해당 임시 입력만 소비하고 미해결 질문·관련 사용자 입력·Facts/Options/Impact·draft를 보존한다. 확인한 사용자 Requirement는 Planner가 대신 수정하지 않는다.
- R-061/R-082: 정리 결과·사용자 원문·출처·질문별 판단·현재 입력/소유권과 원격 게시 확인을 기존 영수증·publication transaction으로 결합한다. 게시 전 경합·중단·재시작에서 gate를 성급히 열지 않으며 같은 결정을 재요구하거나 입력 양식을 복원하지 않는다. 수동 준비/소유·실제 PR·started 보호는 G-243을 따른다.
- R-061: 해소 후 양식이 없는 정상 재관측은 같은 결정을 유지하되, 관련 목표·source·질문이 실제로 바뀌면 이전 결정의 유효성을 다시 평가한다. 무조건 return하여 새 조건을 놓치거나 모든 prose 변경을 새 승인 요구로 바꾸지 않는다. 새 모호함의 draft 전환은 미착수·무소유 경계 안에서만 한다.
- R-053: 남은 질문·판단 지문·알림 링크는 같은 원격 근거를 가리킨다. 해소·교체된 질문의 미전송 알림은 현재 수명에 맞게 정리하고 이미 전송한 감사는 보존한다. 같은 판단의 재처리가 중복 요청이나 과거 질문의 최신 링크 재통지를 만들지 않는다.
- R-061/R-071/R-082: 해소 뒤에도 실제 dependency Merge·실행·게시 gate를 적용한다. 미해소 draft/의존 작업·catalog hold·수동 소유·독립 실패/session/level은 보존하고 독립 작업은 계속한다. 완료·Requirement archive에서 판단 대기를 누락하지 않는다. 제품·설계·고정 지침의 실제 계약을 정합하게 유지하며 G-171의 다른 Worker 문서 목표는 추가하지 않는다.

## Implementation

- [ ] `loop_manager_internal/catalog/draft_decisions.py`의 구현된 사용자 변경·질문별 판정·정리 handler를 재사용한다. 보존된 질문부터 답변까지 실제 변경의 출처를 대조하고 기존 게시/PR/catalog 감사로 설명되는 Agent 내용, 혼합 author·rename·조회 실패의 불확실성을 보존한다. 결과 형식은 기존 엄격한 JSON 처리 helper를 먼저 확인해 재사용한다. R-061/R-064의 의미 판단과 Manager 검증을 분리한다.
- [ ] `loop_manager_internal/storage/execplan_draft_state.py`의 해소 후 조기 반환을 현재 scope/source 근거와 대조한다. 동일 결정을 잃지 않으면서 새로운 관련 조건을 재판단할 수 있도록 이전 질문·해소 감사·근거를 유지한다. 현재 테스트의 양식 정리 뒤 재관측과 양식이 남은 과거 승인 재평가를 새 조건 변경까지 연결한다.
- [ ] `draft_decisions.py`의 부분 정리에서 남은 질문과 미소비 사용자 입력을 보존한다. Manager가 확인한 부분만 정리하고 Agent의 User Decision 편집·draft 자기 해제 금지는 유지한다. 본문/관계 hash와 실제 사용자 변경을 정리 전후에 결합한다.
- [ ] `loop_manager_internal/planning/planner_validation.py`, `validation_ports.py`, `planner_publication.py`와 `loop_manager_internal/storage/planning_publication_state.py`의 구현된 결과·영수증·manifest를 재사용해 경합·중단 재개를 완결한다. G-243의 수동 준비/소유·실제 PR 보호를 소비하며 Worker scope·실행 이력을 덮어쓰지 않는다. R-082 게시 확인 전 pending 해제는 금지한다.
- [ ] `loop_manager_internal/notifications/events.py`, `incidents.py`, `work_exhaustion.py`에서 R-053의 질문 사건·링크·미전송 수명을 질문별 전환과 맞춘다. 새 알림 전송 도구나 일반 알림 정책을 도입하지 않는다.
- [ ] `tests/test_execplan_drafts.py`, `tests/test_incremental_planning.py`의 직접 구현 회귀와 기존 publication/notification fixture를 확장한다. PRODUCT·ARCHITECTURE·Planner 지침은 이미 변경된 부분을 재작성하지 않고 실제 남은 구현 차이만 정합화한다.

## Validation

- 실제 SQLite·parser·Manager handler에 모의 Planner/Git 관측을 제공해 본문/Requirement/User Decision의 완전·부분 해소, revision 생략·stale 명시, reject/defer·상충·형식 중복·무관한 변경을 검사한다. 부분 해소에서 남은 질문과 사용자 원문이 보존되어야 한다.
- 양식이 정리된 해소 완료 상태에서 동일 입력 재관측, 무관 prose 변경, 관련 source/목표 변경과 새 모호함을 대조한다. 기존 감사가 유지되고 유효성 재평가를 건너뛰지 않아야 하며 실제 실행 이력 있는 작업을 draft로 되돌리지 않는다.
- 혼합 작성자·Manager 게시 재현·rename·계보/조회 실패, snapshot 후 수동 intent/membership·실제 PR 출현과 원격 입력 경합에서 무근거 정리·gate 해제가 없어야 한다. 독립 계획은 계속 선택 가능해야 한다.
- `tests/test_planning_publication.py`, `tests/test_notification_events.py`, `tests/test_notification_incidents.py`, `tests/test_notification_delivery.py`의 관련 fixture로 정리 전/후·push 후 DB 미적용·transaction 중단과 재시작을 확인한다. 남은 질문 알림·미전송 취소·과거 전송 감사·중복 방지와 dependency/완료/archive 회귀를 유지한다. 실제 모델·외부 알림은 호출하지 않는다.
- 부분/전체 verify의 회귀 조건은 기존 명시적 결정·자기 승인 거부·Worker 기록 보존·수동/PR 보호·판단 대기와 게시 복구다. R-082 문서 판단과 R-086 선행 Review 성공을 사용자 결정 근거로 사용하지 않는다.

## Progress

원격 직접 구현의 사용자 변경 관측, 완전/부분 답변, revision 생략, 정리 영수증과 게시/DB 결합 및 관련 회귀를 정적으로 확인했다. 이전 조사와 보존 verify summary는 새 잔여 조건의 검증 성공이 아니며 이 ExecPlan의 구현·테스트를 실행하지 않았다.

- [ ] 출처·결과 검증과 부분 입력 보존
- [ ] 양식 정리 뒤 새 조건 재평가·중단 재개·현재 알림 연결
- [ ] targeted unit 검증과 관련 문서 정합성

## Decision Log

- 직접 구현된 해소 경로를 재구현하지 않는다. 정리된 양식의 부재를 재승인 이유로 쓰지 않는 보호와, 새 관련 입력을 재검토하는 R-061 조건을 함께 지켜야 한다. 현재 `observe`의 양식 없는 완료 상태 조기 반환은 이 대조를 하지 않으므로 남은 목표다.
- 일부 질문 해소 때 현재 handler는 User Decision 절 전체를 제거한다. 답변이 여러 질문을 포함하는 경우 미해결 입력을 잃지 않는 질문별 소비를 완료 조건에 남긴다.
- G-243은 수동 개정 권한, G-242는 단계별 DOCUMENT_RESULT/PR 경합을 담당한다. 이 작업은 DRAFT_DECISION_RESULT·사용자 출처·질문별 수명에 한정하며 각 선행·기존 ID/경로/source를 보존한다.
- complexity high: 혼합 출처·부분 해소·정리 후 새 조건과 원격/DB/알림 사이 중단을 동일 결정 감사로 연결하는 판단이 남아 있다. risk high: 오판하면 사용자 선택 없는 실행/게시 또는 답변 손실이 생긴다. 원문·근거·정리 영수증·publication intent를 보존해 수동 재평가/복구하며 출처·부분 입력·경합·중단 시나리오로 gate 보존을 검증한다.
- 과거 보존 verify summary의 3,356 성공·5 skip은 당시 후보의 기록이다. 이번 잔여 R-061 조건에 재사용한 PASS를 만들지 않으며 선택된 unittest 수집·compile과 실제 Agent guard를 유지한다.
