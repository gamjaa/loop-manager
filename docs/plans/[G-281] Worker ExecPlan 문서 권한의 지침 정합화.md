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
---

## Purpose

Worker의 ExecPlan 수정 범위를 선택된 본문으로 한정한 R-082와 현재 코드에 맞춰 고정 Worker 지침·R-061의 관련 설명을 정합하게 만든다. 현재 `docs/AGENTS.worker.md`는 기존 ExecPlan을 함께 갱신할 수 있다고 설명하고 frontmatter 제한을 명시하지 않는다. `agents/agent_prompts.py`의 새 사용자 메시지와 결과 검사는 이미 제한을 적용하므로 재구현하지 않는다.

## Acceptance Criteria

- R-064의 고정 Worker instruction 원본은 선택된 ExecPlan의 구현·검증 기록 등 본문만 수정하고 frontmatter·다른 ExecPlan·현재 작업과 무관한 Requirement는 수정하지 않는다고 명시한다. 관련 PRODUCT·ARCHITECTURE·활성 Requirement·지침·사용 안내의 기존 수정 범위와 목표·필수 검증·사용자 결정 보호는 유지한다.
- R-061의 역할 표·연관 문서 정합성 설명은 ExecPlan에 적용되는 R-082의 구체적 제한과 Planner·Manager의 선언 개정 책임을 연결한다. Worker가 다른 계획의 대응표·기록·dependency를 직접 갱신할 수 있다는 해석을 남기지 않으며 draft·User Decision 정책과 다른 역할 권한은 변경하지 않는다.
- 기존 source 문자열은 rename 조회 호환으로 해석한다. 누락·모호함이나 잘못된 선언은 원인과 경로를 보고해 Planner·Manager의 공식 선언·catalog·DB 반영으로 교정하며, Worker의 source 수정이나 metadata 비교 완화로 우회하지 않는다고 설명한다.
- 현재 작업의 고정 원본 묶음·session·수행 기록은 보존하고 지침 편집만으로 진행 중인 호출의 버전이나 권한을 바꾸지 않는다. 신규 작업의 원본 선택은 기존 R-064 경계를 사용하며 새 배포·snapshot·provider 실행 gate를 만들지 않는다.
- 직접 구현된 다른 ExecPlan 경로 거부·정확한 선언 비교·불일치 필드 진단과 관련 회귀는 유지한다. 문서 정합화를 그 구현의 새 성공 기록이나 Requirement 전체 충족으로 취급하지 않는다.

## Implementation

- [ ] 확인한 `docs/AGENTS.worker.md`의 구현 범위 절을 R-082의 선택 본문·선언 교정 책임으로 통합한다. 공통·다른 역할 원본에 Worker 전용 규칙을 복제하거나 작업 prompt의 기존 제한을 이번 작업에서 제거하지 않는다.
- [ ] 확인한 활성 R-061의 역할별 접근 표와 연관 문서 정합성 문장을 필요한 범위만 갱신하고 R-082의 ExecPlan 갱신 경계로 연결한다. 권한 밖 ExecPlan에 필요한 변경은 현재 작업의 보고로 남겨 Planner·Manager가 처리하게 하며 기존 목표·Acceptance Criteria·판단 대기와 사용자 원문을 보존한다.
- [ ] 확인한 `loop_manager_internal/agents/agent_prompts.py:worker_prompt()`·`agent_scope_decision.py:_worker_path_allowed()`·`agents/scope_validation.py:validate_agent_scope()`와 `execution/plan_validation.py:validate_active_execplan_metadata()`를 문서 설명과 대조한다. 코드·테스트·공통 PLANS·사용자 source 문자열을 문서에 맞춰 바꾸지 않는다. PRODUCT·ARCHITECTURE의 이번 delta 설명은 이미 맞으므로 반복 수정하지 않는다.

## Validation

- 문서만 변경하므로 실행할 targeted unit test가 없다는 사유와 대조한 구현·회귀 경로를 결과에 남긴다. prose 검사 테스트·새 fixture·verify 입력 추가 없이 변경 전후 허용/거부·선언 교정 주체·기록 보존을 읽기 전용으로 확인한다.
- 확인한 `tests/test_execution_guidance.py:WorkerPromptFocusTests`, `tests/test_agent_scope_decision.py:WorkerPlanDocumentScopeTests`, `tests/test_requirement_sources.py:ExecPlanRequirementSourceCatalogTest`의 기존 회귀와 설명을 대조한다. 선택 본문 허용·다른 계획 거부·source rename 조회와 선언 불일치 거부·공식 선언 채택 뒤 본문 보존을 구분한다. 테스트 파일의 존재를 실행 PASS로 기록하지 않는다.
- R-064의 새 작업 원본 선택과 진행 작업/legacy snapshot 보존, R-082의 Planner 게시·DB 반영 책임이 문서 편집으로 바뀌지 않아야 한다. 보고 규칙 정합화와 Worker prompt 중복 제거는 기존 별도 작업에 남긴다.

## Progress

- [ ] 고정 Worker 지침과 R-061의 관련 절 정합화
- [ ] 구현·기존 회귀와 문서 의미 보존 대조

## Decision Log

- complexity low: 확정된 R-082 제한과 현재 구현을 기존 두 문서의 관련 절에 반영하는 국소 교정이며 새 정책·상태 설계가 없다. risk low: 관련 설명만 바꾸고 고정 실행 snapshot·코드·저장 상태는 보존하므로 문서 교정으로 복구할 수 있다. priority normal은 현재 역할 지침의 필수 정합성 보완이다.
- 6695ba9ec1fed30e916d3d241d82055305516d0b의 변경 검토에서 식별했다. G-272의 PR 문서 보고 표시 정합화와 G-066의 초기/delta/복구 prompt 축약은 유지하며 이 작업은 그 구현을 추가하지 않는다. 현재 G-243의 후보·원본 묶음·실행 및 검증 이력은 수정하지 않는다. Planner는 구현·테스트·verify를 실행하지 않았다.
