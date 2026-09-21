- 파일명 형식은 `docs/plans/[G-000] Title.md`다.
- Planner가 현재 프로젝트에 필요한 ExecPlan을 하나 이상 완성된 형태로 작성할 수 있다.
- ID는 프로젝트 안에서 고유하며 기존 최대 번호 다음 값을 사용한다.
- 파일 상단 frontmatter에는 실행 상태가 아닌 계획의 고정 메타데이터만 기록한다.
- `sources`에는 이 계획의 근거가 된 PRODUCT 또는 Requirement 경로를 기록한다.
- `depends_on`에는 이 계획보다 먼저 PR이 Merge돼야 하는 ExecPlan ID를 기록하며, 선행 작업이 없으면 빈 목록으로 둔다.
- Planner는 작업 범위와 구현 난이도로 `complexity`를, 실패 가능성과 변경 영향으로 `risk`를 작성한다.
- Planner는 `priority`를 `critical`, `high`, `normal`, `low` 중 하나로 기록한다. 안전한 무인 실행을 막는 작업부터 핵심 자동화, 일반 필수 작업, 선택적 편의 순으로 분류한다.
- 새 ExecPlan에는 `priority`가 필수이며, 이 형식 도입 전에 작성돼 값이 없는 기존 ExecPlan은 `normal`로 해석한다.
- Loop Manager는 frontmatter를 코드로 파싱하고 검증한 뒤 실행 상태를 SQLite에 `READY`로 등록한다.
- 실행 상태는 파일에 기록하지 않는다. 같은 프로젝트를 직렬로 실행하더라도 모든 `depends_on`이 SQLite에서 `MERGED`가 되기 전에는 Worker를 시작하지 않는다.
- Loop Manager는 존재하지 않는 ID, 자기 자신에 대한 의존성과 순환 의존성을 코드로 거부한다.
- BLOCKED 해제를 위해 사용자가 ExecPlan을 수정할 수 있으며, Loop Manager는 내용 변경을 감지해 작업을 재개한다.
- `Validation`에는 Worker가 실행할 계획별 targeted unit test, 추가할 테스트 시나리오와 전체 verify가 확인해야 할 계획 고유의 회귀 조건만 기록한다.
- Loop Manager가 현재 운영체제의 전체 verify 스크립트를 항상 실행하므로 `scripts/verify.ps1`과 `scripts/verify.sh` 경로를 ExecPlan마다 반복하지 않는다.

## Agent guidance 변경 제안과 사용자 결정

Worker가 현재 ExecPlan 달성에 꼭 필요한 `AGENTS.md` 규칙 변경을 발견하면 파일을 직접 수정하지 않고 해당 ExecPlan의 `Decision Log` 끝에 아래 제안과 빈 사용자 결정을 한 쌍으로 추가한 뒤 작업을 중단한다. proposal ID는 ExecPlan 안에서 고유한 소문자·숫자·하이픈 값이어야 한다. `Target`은 저장소 루트의 `AGENTS.md`만 허용하며, `Scope`는 현재 프로젝트 전용이면 `project`, Loop Manager 기준 원본의 공통 규칙이면 `common`이다. 공통 제안은 Loop Manager 자체 저장소의 Worker만 작성할 수 있다. Planner와 Reviewer는 제안하거나 적용하지 않는다.

````markdown
### Agent Guidance Proposal: guidance-example
Target: `AGENTS.md`
Scope: `project`
Reason: 현재 ExecPlan에 이 규칙이 필요한 구체적인 이유
Impact: 적용하지 않으면 현재 구현에 생기는 구체적인 영향
Patch:
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1,1 +1,2 @@
 기존 줄
+추가할 정확한 줄
```

### Agent Guidance User Decision: guidance-example
Decision: `pending`
Revision: ``
Patch:
```diff
```
````

사용자는 제안 본문을 바꾸지 않고 같은 ID의 `User Decision`만 다음과 같이 편집한다.

- 승인: `Decision`을 `approve`로, `Revision`을 비어 있지 않은 결정 식별자로 작성하고 `Patch`는 비워 둔다. Worker는 Proposal의 patch만 적용한다.
- 거절: `Decision`을 `reject`로, `Revision`을 비어 있지 않은 결정 식별자로 작성하고 `Patch`는 비워 둔다. Worker는 대상 파일을 변경하지 않는다.
- 대체: `Decision`을 `replace`로, `Revision`을 비어 있지 않은 결정 식별자로 작성하고 `Patch`에 사용자가 확정한 전체 대체 diff를 기록한다. Worker는 대체 patch만 적용한다.

`pending`의 `Revision`과 `Patch`는 모두 비어 있어야 한다. 제안과 결정은 순서대로 인접해야 하며 기존 제안·결정을 수정하거나 삭제하지 않는다. Loop Manager는 결정, revision, 대상 경로와 확정 patch의 hash를 결합해 승인 상태를 판정한다. `User Decision` 밖의 ExecPlan 내용이나 근거 PRODUCT/Requirement 변경만으로는 승인 대기 `BLOCKED`를 해제하지 않는다. 승인 또는 대체 patch와 실제 `AGENTS.md` diff가 정확히 일치하지 않으면 새 제안과 결정을 받아야 한다.

```markdown
---
id: G-002
title: ExecPlan Title
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-001] Requirement Title.md
depends_on:
  - G-001
complexity: low|medium|high
risk: low|medium|high|critical
priority: critical|high|normal|low
---

## Purpose

...

## Acceptance Criteria

...

## Implementation

...

## Validation

- Worker가 실행할 관련 targeted unit test 또는 검증 시나리오
- 전체 verify에서 확인할 이 계획 고유의 회귀 조건

## Progress

...

## Decision Log

...
```
