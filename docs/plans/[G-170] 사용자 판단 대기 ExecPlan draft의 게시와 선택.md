---
id: G-170
title: 사용자 판단 대기 ExecPlan draft의 게시와 선택
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

결정이 필요한 유효한 계획을 planning 실패 대신 draft로 게시하고, 독립 작업은 계속할 수 있게 한다. 기존 document_drafts는 문서 수정 승인으로서 이 기능을 대신하지 않는다.

## Acceptance Criteria

- draft 계획과 실행 가능한 계획을 검증한 한 batch로 게시·catalog에 반영한다. draft는 질문·선택 이유·필요한 결정을 담으며 Worker/branch/worktree/session/PR을 예약하거나 시작하지 않는다.
- draft와 영향받는 의존 계획은 실행하지 않는다. 독립 READY·planning·PR 관측은 계속하며 draft를 planning 오류·프로젝트 차단·완료 또는 Requirement 충족으로 취급하지 않는다.
- 사용자가 원격에서 결정하고 draft 표시를 제거한 유효한 계획만 최신 원격·dependency·소유권 확인 뒤 READY 실행 대상이 된다. Agent의 자기 승인·묵시적 해제와 같은 질문의 반복 게시를 거부한다.
- 질문과 원격 링크·대기 상태를 보존하고, 게시 중단·원격 수정·재시작에서 중복 draft·결정 손실·불완전한 catalog 채택이 없다.

## Implementation

- catalog parser·계획 snapshot·storage와 planning 게시/selection의 책임 경계에 draft metadata와 사용자 결정 대기를 연결한다. 기존 계획 상태·취소·source·ID 고유성의 의미를 보존한다.
- 일반 Planner prompt·결과 검증과 retirement/완료 gate에서 draft를 유효한 사용자 판단 대기로 다룬다. 기존 문서 draft 승인과 AGENTS User Decision은 별도 계약으로 유지한다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- catalog metadata·planning selection·catalog batches·requirement retirement의 draft parsing, batch 혼합, dependency, 원격 해제, 자기 승인·재시작 관련 최소 unit case를 실행한다.
- Manager 전체 verify는 draft 무예약·무호출, 독립 계획/프로젝트 지속, 실제 게시·catalog 확정과 기존 문서 draft·guidance 계약을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 draft 파싱·혼합 batch 등록·원격 결정 대기·선택 및 완료 gate 구현. 새 draft unit module과 Requirement retirement case 21개, 기존 metadata·compact catalog·READY 선택·runtime 예약/전이·retirement 회귀 case 6개 통과.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- catalog·게시·선택·완료에 걸쳐 새 대기 의미를 연결하므로 complexity high, 오실행·자기 승인 영향으로 risk high다. 알림 표현은 G-175에서 연결한다.
- `catalog/execplan_drafts.py`는 문서 계약·질문 중복·Agent 자기 승인 검증, `storage/execplan_draft_state.py`는 결정 대기·근거·게시 링크·결정 감사 이력을 담당한다. 기존 READY phase와 publication transaction·snapshot CAS를 재사용하며 추가 SQLite table은 기존 실행 이력을 변경하지 않는다. draft와 직접·간접 의존 계획은 선택에서 제외하고 runtime 실행·workspace 예약 경계에서도 거부한다.
- draft 문서의 `Draft Decision` 절에는 비어 있지 않은 `Question:`, `Facts:`, `Options:`, `Impact:` 줄을 둔다. 사용자는 해당 절을 보존하고 원격에서 draft 표시를 제거한 뒤 `User Decision` 절에 `Decision: approve`, `Revision: compact catalog의 decision_wait.revision 내용 해시`, `Answer: 구체적 답변`을 기록한다. reject/defer·불완전한 결정·표시만 제거한 변경은 계속 대기한다. 새 문서 draft 승인이나 AGENTS 승인 형식은 변경하지 않는다.
- 판단 근거는 Progress·Decision Log·User Decision을 제외한 계획 선언·본문과 현재 source·PRODUCT·ARCHITECTURE의 내용 지문이다. 이 근거가 달라지면 이전 결정을 사용하지 않고 최신 판단 revision을 보존한다. 변경된 목표·구현 방향까지 함께 결정하려면 최신 질문·계획을 먼저 게시한 후 그 revision에 대한 결정을 기록한다. 질문을 원격에서 삭제해도 기존 대기 질문·결정 감사는 보존한다. 동일 근거의 원격 base 전진·재시작은 질문 링크와 대기 identity를 바꾸지 않는다.
- 일반·허용된 유지보수 Planner의 작성 지침과 결과 검증을 연결했다. Worker의 결정 변경은 scope 검증에서 거부한다. 기존 안정 대기 결과·로그·알림에는 질문과 게시 링크를 제공하며 별도 진행 알림 정책은 구현하지 않았다. PRODUCT에는 이 Requirement 구현에서 앞으로 유지할 draft 동작만 반영했다. AGENTS·PLANS·ARCHITECTURE·Requirement와 루트 배치는 변경하지 않았다.
- 검증: `python3 -m unittest tests.test_execplan_drafts tests.test_requirement_retirement.RequirementRetirementTest.test_execplan_decision_hold_prevents_final_requirement_judgment` (21개 통과). 추가로 `CatalogMetadataTest.test_execplan_catalog_validates_fixed_metadata`, `CatalogBatchesTest.test_compact_catalog_excludes_merged_bodies_and_has_only_allowed_fields`, `CatalogSchedulingTest.test_dependency_gate_then_priority_and_active_plan_preemption`, `RuntimeStateRepositoryTest.test_activation_update_hydrate_and_clear_round_trip_runtime_fields`, `RuntimeStateRepositoryTest.test_workspace_reservation_and_merge_are_repository_operations`, `RequirementRetirementTest.test_final_merge_defers_then_reuses_judgment_and_preserves_bytes` 6개를 개별 선택해 통과했다. Git·Agent는 fake이며 실제 게시·호출을 하지 않았다. Linux verify는 기존 unittest discovery runner가 새 module을 포함하므로 스크립트 변경이 필요 없고 전체 verify는 실행하지 않았다.
- 전달된 verify 진단의 planning selection 실패는 공유 `_code_check_ports` fixture가 frontmatter 없는 `plan` 문자열을 READY snapshot 본문으로 사용한 데서 발생했다. 기존 `tests.support.catalog.plan_text` helper와 실제 본문의 SHA-256을 사용하도록 fixture를 수정했다. 제품 parser의 엄격한 검증은 유지한다. `test_accepted_check_does_not_claim_later_general_planning`을 개별 재현한 뒤 동일 명령으로 재실행해 통과했고, 진단의 나머지 실패 case 15개와 변경한 draft snapshot/prompt case 1개만 선택한 targeted unit test도 모두 통과했다. snapshot 저장·복원 후 draft 표시의 생략·true·false가 prompt에 동일하게 전달되는 검증을 추가했다. Worker는 전체 verify를 실행하지 않았다.
