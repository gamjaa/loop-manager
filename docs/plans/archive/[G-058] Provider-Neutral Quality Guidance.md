---
id: G-058
title: Provider-Neutral Quality Guidance
sources:
  - docs/requirements/[R-027] Shared Quality Guidance and Full Project Refactoring Assessment.md
depends_on:
  - G-051
complexity: medium
risk: medium
priority: normal
---

## Purpose

현재 `worker_prompt`, `reviewer_prompt`와 Planner prompt는 역할별 계약을 제공하지만 코드·문서의 불필요한 증가를 막는 공통 판단 기준은 분산돼 있다. 기존 등록 프로젝트에도 provider에 관계없이 같은 기준을 적용한다.

## Acceptance Criteria

- Worker·Reviewer는 기존 코드 및 표준 도구 재사용, 현재 목표에 필요한 최소 구조, 오류 처리·보안·검증 보존을 같은 provider-neutral prompt에서 확인한다.
- Planner와 Specification Skill의 문서 검토는 중복·장황함을 줄이면서 PRODUCT·Requirement·ARCHITECTURE의 역할, 결정·예외·실패 흐름과 Acceptance Criteria의 의미를 유지한다.
- 외부 스킬 설치·호출에 의존하지 않고 Codex·Claude 및 기존·신규 등록 프로젝트에서 같은 핵심 기준이 적용된다. 스킬 사용 가능 여부가 실행 성공 조건이 되지 않는다.
- 기존 Agent 응답 protocol, 문서 위치·저장 형식과 scope 권한을 유지한다. 공통 품질 지침은 사용자 소유 원본문서의 자동 수정 권한을 넓히지 않는다. R-027의 일회성 문서 개선은 G-059의 별도 수정본과 수정본별 사용자 승인 절차로 처리하며 공통 AGENTS.md는 변경하지 않는다.

## Implementation

- 중복 prompt 문구를 재사용 가능한 좁은 품질 지침으로 합치고 `loop_manager.py`의 역할별 prompt와 `.agents/skills/specification-docs/SKILL.md`의 문서 가독성 지침에 연결한다.
- 외부 ponytail·i-have-adhd의 설치나 실행을 필수로 하지 않는다. 현재 요구사항보다 짧은 문서나 검증 축소를 유도하는 문구는 넣지 않는다.
- Worker·Reviewer·Planner prompt와 Specification Skill에는 문서의 의미·역할·필수 판단을 보존하는 품질 기준만 연결한다. AGENTS.md 자동 수정 예외를 제안하거나 적용하는 구현은 이 계획 범위에서 제외한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 역할별 prompt, `tests/test_managed_configuration.py`의 공통 skill 배포 관련 targeted unit test만 실행한다.
- Codex·Claude 및 이미 등록된 프로젝트의 prompt가 동일 기준을 포함하고, 기존 protocol·권한·필수 명세 요소가 유지되는지 검증한다.
- 문서 품질 지침이 사용자 소유 원본문서의 직접 수정 권한을 만들지 않고 R-027의 일회성 수정본 승인 흐름과 분리되는지 검증한다.
- 전체 verify에서 Planner validation, Reviewer parsing, 공통 파일 설치와 Agent scope 회귀가 없어야 한다.

## Progress

- [ ] 역할별 공통 품질 지침 연결
- [ ] 문서 skill의 완전성·가독성 지침 갱신

## Decision Log

- 외부 스킬은 참고 근거이며 제품의 실행 의존성이 아니다.
- 기존 specification-document-refactoring 제안은 당시 판단의 기록으로 보존한다. 새 R-027 방향에서는 공통 AGENTS.md 변경이 필요하지 않으며, 대응 User Decision은 사용자가 직접 거절로 확정해야 한다.

### Agent Guidance Proposal: specification-document-refactoring
Target: `AGENTS.md`
Scope: `common`
Reason: R-027은 의미 보존 명세 문서 리팩터링을 자동으로 준비하는 예외를 공통 AGENTS.md에 명시적으로 승인·반영한 뒤에만 허용한다. 현재 공통 지침에는 해당 예외가 없다.
Impact: 승인된 예외 없이 문서 리팩터링 권한을 구현하면 사용자 소유 PRODUCT·Requirement·ARCHITECTURE의 기본 수정 제한을 우회하게 되어 이 ExecPlan의 Acceptance Criteria를 충족할 수 없다.
Patch:
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -12,4 +12,6 @@
 - 새로운 기능이나 정책 변경은 Requirement에서 시작한다.
 - `PRODUCT.md`, `docs/requirements/*.md`, `docs/ARCHITECTURE.md`는 사용자가 작성하며 AI는 기본적으로 수정하지 않는다.
 - 사용자가 Specification Skill을 이용한 문서 작성을 명시적으로 요청한 경우에는 해당 문서를 함께 작성하거나 수정할 수 있다.
+- 공통 `AGENTS.md`의 guidance 변경을 승인 절차로 반영한 프로젝트에서만, 의미 보존 명세 문서 리팩터링 ExecPlan은 표현·구조·중복 정리만 자동으로 준비할 수 있다. 정책·사용자 동작·Acceptance Criteria·문서 역할·이력의 의미를 바꾸거나 보존을 증명할 수 없는 변경은 사용자 결정으로 보낸다.
+- 이 예외의 문서 변경은 개별 ExecPlan과 별도 PR에서 Reviewer의 의미 보존 검토를 거쳐 사용자가 Merge한 뒤에만 반영된다. 승인되지 않은 프로젝트에서는 읽기 전용 평가와 수정 제안만 수행하며, 기존 등록 프로젝트의 `AGENTS.md`를 자동으로 덮어쓰지 않는다.
 - Requirement 구현 결과가 앞으로도 지켜야 할 제품 규칙이면 예외적으로 코드와 같은 PR에서 `PRODUCT.md`를 갱신할 수 있다.
```

### Agent Guidance User Decision: specification-document-refactoring
Decision: `reject`
Revision: `reject-20260924-1`
Patch:
```diff
```
