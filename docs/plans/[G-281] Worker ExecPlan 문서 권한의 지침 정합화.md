---
id: G-281
title: Worker ExecPlan 문서 권한의 지침 정합화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: low
risk: low
priority: normal
draft: true
---

## Purpose

Worker의 문서 수정 범위와 선언 교정 책임을 현재 R-082에 맞게 설명한다. 최신 원격의 `docs/AGENTS.worker.md`·작업 prompt·범위 검사는 선택된 ExecPlan 본문만 허용하고 모든 Requirement 변경을 금지하도록 이미 바뀌었다. 이 부분은 재구현하지 않는다. 남은 R-061의 역할 표·연관 문서 설명과 Acceptance Criteria는 Requirement·다른 ExecPlan 수정을 허용하는 표현을 유지한다. 두 활성 명세의 상충과 사용자 소유 문서의 정합화가 남아 있으므로 기존 ID의 draft로 판단을 보존하며 Worker에게 Requirement 편집을 맡기지 않는다.

## Acceptance Criteria

- R-064의 고정 Worker instruction 원본은 선택된 ExecPlan 본문만 허용하고 frontmatter·다른 ExecPlan·활성 및 archive의 모든 Requirement 추가·수정·삭제·rename·이동을 금지하는 최신 R-082와 일치한다. 관련 PRODUCT·ARCHITECTURE·FUTURE_IDEAS·지침·사용 안내와 목표·필수 검증·사용자 결정 보호를 유지한다. 이미 반영된 지침·prompt·결과 검사를 다시 구현하지 않는다.
- 사용자 판단과 원격 문서 반영 뒤 R-061의 역할 표·연관 문서 정합성 설명·해당 Acceptance Criteria를 R-082의 구체적 제한과 대조한다. Worker가 Requirement나 다른 계획의 대응표·기록·dependency를 직접 갱신할 수 있다는 상충 표현의 해소는 사용자의 Requirement 변경으로 확인한다. Planner·Worker가 R-061을 대신 편집하지 않으며 draft·User Decision 정책과 다른 역할 권한은 유지한다.
- 기존 source 문자열은 rename 조회 호환으로 해석한다. 누락·모호함이나 잘못된 선언은 원인과 경로를 보고해 Planner·Manager의 공식 선언·catalog·DB 반영으로 교정하며, Worker의 source 수정이나 metadata 비교 완화로 우회하지 않는다고 설명한다.
- 현재 작업의 고정 원본 묶음·session·수행 기록은 보존하고 지침 편집만으로 진행 중인 호출의 버전이나 권한을 바꾸지 않는다. 신규 작업의 원본 선택은 기존 R-064 경계를 사용하며 새 배포·snapshot·provider 실행 gate를 만들지 않는다.
- 직접 구현된 다른 ExecPlan 경로 거부·정확한 선언 비교·불일치 필드 진단과 관련 회귀는 유지한다. 문서 정합화를 그 구현의 새 성공 기록이나 Requirement 전체 충족으로 취급하지 않는다.
- Manager의 선택 frontmatter 부분 복원과 본문 bytes·mode·실행 기록 보존, 복원 후 Worker의 최종 diff·문서 보고 갱신은 기존 구현을 사용한다. 다른 범위 밖 문서의 파일·index 복원은 별도 후속 작업이며 이번 지침 정합화에 합치지 않는다.

## Implementation

- [ ] 사용자가 반영한 활성 R-061의 역할 표·연관 문서 정합성·Acceptance Criteria와 현재 R-082를 읽기 전용으로 대조한다. 상충이 남으면 구체적 원문과 미해결 범위를 보고하며 Requirement를 편집하거나 draft를 직접 해제하지 않는다. 허용된 지침에 실제 남은 불일치가 있을 때만 관련 절을 교정하고 이미 반영된 제한을 반복 구현하지 않는다.
- [ ] 확인한 `docs/AGENTS.worker.md`, `loop_manager_internal/agents/agent_prompts.py:worker_prompt()`·`agent_scope_decision.py:_worker_path_allowed()`·`agents/scope_adapter.py:validate_worker_workspace_scope()`와 `execution/plan_validation.py:restore_worker_frontmatter()`·`validate_active_execplan_metadata()`를 문서 설명과 대조한다. 공통·다른 역할 원본에 Worker 전용 규칙을 복제하거나 코드·테스트·공통 PLANS·사용자 source 문자열을 문서에 맞춰 바꾸지 않는다. PRODUCT·ARCHITECTURE의 이번 delta 설명은 이미 맞으므로 반복 수정하지 않는다.

## Validation

- 문서만 변경하므로 실행할 targeted unit test가 없다는 사유와 대조한 구현·회귀 경로를 결과에 남긴다. prose 검사 테스트·새 fixture·verify 입력 추가 없이 변경 전후 허용/거부·선언 교정 주체·기록 보존을 읽기 전용으로 확인한다.
- 확인한 `tests/test_execution_guidance.py:WorkerPromptFocusTests`, `tests/test_scope_role_boundaries.py:ScopeRoleBoundariesTest`, `tests/test_requirement_sources.py:WorkerFrontmatterRestoreTests`·`ExecPlanRequirementSourceCatalogTest`와 `tests/test_execution_worker_phase.py`의 기존 회귀와 설명을 대조한다. 선택 본문 허용·모든 Requirement 및 다른 계획 거부·source rename 조회와 선언 불일치 거부·공식 선언 부분 복원 뒤 본문 보존을 구분한다. 테스트 파일의 존재를 실행 PASS로 기록하지 않는다.
- R-064의 새 작업 원본 선택과 진행 작업/legacy snapshot 보존, R-082의 Planner 게시·DB 반영 책임이 문서 편집으로 바뀌지 않아야 한다. 보고 규칙 정합화와 Worker prompt 중복 제거는 기존 별도 작업에 남긴다.

## Progress

- [ ] 고정 Worker 지침과 R-061의 관련 절 정합화
- [ ] 구현·기존 회귀와 문서 의미 보존 대조
- [ ] R-061의 상충 표현에 대한 사용자 판단과 원격 문서 정합화 확인

## Draft Decision

Question: R-061에 남아 있는 Worker의 Requirement·다른 ExecPlan 수정 허용 표현을 사용자가 최신 R-082의 선택 본문 제한·모든 Requirement 변경 금지에 맞게 정합화할 것인가, 아니면 이 문서 정합화 목표를 보류할 것인가?
Facts: efc71a72f4e2ce152c9c4355e6d08bdc4ee242ac의 R-082·PRODUCT·Worker 지침과 코드는 모든 Requirement 변경을 금지한다. 활성 R-061의 역할 표·연관 문서 절·Acceptance Criteria는 여전히 Requirement·기존 ExecPlan 수정을 허용한다. 기존 지침 구현 목표는 직접 반영됐지만 R-061 정합화는 남았다. 현재 역할 권한으로 Planner·Worker가 사용자 Requirement를 대신 변경할 수 없으며 이 질문은 최신 실행 제한을 다시 승인받거나 Worker의 예외 권한을 만드는 질문이 아니다.
Options: (A) 사용자가 R-061의 해당 역할·문서 정합성 표현을 최신 R-082의 제한과 연결해 원격에 반영한다. (B) 남은 문서 정합화 목표를 보류하고 상충 표현과 판단 대기를 보존한다. 다른 권한 정책을 원하면 사용자 명세에서 먼저 확정한다.
Impact: A의 원문과 출처를 확인하면 Manager가 같은 ID의 남은 질문·실제 미충족 범위를 재평가한다. B에서는 이 작업과 의존 작업의 대기를 유지하고 독립 작업은 진행한다. 판단 전에 Worker·workspace·branch·PR을 시작하거나 이미 반영된 지침 교정을 중복 실행하지 않는다.

## Decision Log

- 2026-10-14 최신 delta 재평가: 58814a9afa875bf5c440d610afe4e58725826921 이후의 R-082·Worker 지침·scope·frontmatter 부분 복원 변경을 efc71a72f4e2ce152c9c4355e6d08bdc4ee242ac에서 대조했다. 이미 구현된 지침 교정은 작업 지시에서 제외하고, R-061의 남은 상충과 사용자 소유 문서 반영을 같은 ID의 draft로 보존한다. 기존 Progress·source·dependency·분류·priority는 유지한다. 구현·테스트·검증 성공이나 사용자 결정을 만들지 않았다.
- 현재 complexity low: 사용자 문서 반영 뒤 기존 지침·구현과 관련 설명을 대조하는 국소 정합화이며 새 정책·상태 설계가 없다. risk low: 고정 실행 snapshot·코드·저장 상태를 보존하고 불명확한 사용자 명세는 덮어쓰지 않는다. priority normal은 현재 역할 지침의 필수 정합성 보완이다. 최초 작성 시 두 문서의 관련 절 교정을 예상했던 근거는 직접 구현과 최신 Requirement 금지를 확인해 위 범위로 대체했다.
- 6695ba9ec1fed30e916d3d241d82055305516d0b의 변경 검토에서 식별했다. G-272의 PR 문서 보고 표시 정합화와 G-066의 초기/delta/복구 prompt 축약은 유지하며 이 작업은 그 구현을 추가하지 않는다. 현재 G-243의 후보·원본 묶음·실행 및 검증 이력은 수정하지 않는다. Planner는 구현·테스트·verify를 실행하지 않았다.
