- 파일명 형식은 `docs/plans/[G-000] Title.md`다.
- Planner가 현재 프로젝트에 필요한 ExecPlan을 하나 이상 완성된 형태로 작성할 수 있다.
- ID는 프로젝트 안에서 고유하며 기존 최대 번호 다음 값을 사용한다.
- 파일 상단 frontmatter에는 실행 상태가 아닌 계획의 고정 메타데이터만 기록한다.
- `sources`에는 이 계획의 근거가 된 PRODUCT 또는 Requirement 경로를 기록한다.
- `depends_on`에는 이 계획보다 먼저 PR이 Merge돼야 하는 ExecPlan ID를 기록하며, 선행 작업이 없으면 빈 목록으로 둔다.
- Planner는 작업 범위와 구현 난이도로 `complexity`를, 실패 가능성과 변경 영향으로 `risk`를 작성한다.
- Loop Manager는 frontmatter를 코드로 파싱하고 검증한 뒤 실행 상태를 SQLite에 `READY`로 등록한다.
- 실행 상태는 파일에 기록하지 않는다. 같은 프로젝트를 직렬로 실행하더라도 모든 `depends_on`이 SQLite에서 `MERGED`가 되기 전에는 Worker를 시작하지 않는다.
- Loop Manager는 존재하지 않는 ID, 자기 자신에 대한 의존성과 순환 의존성을 코드로 거부한다.
- BLOCKED 해제를 위해 사용자가 ExecPlan을 수정할 수 있으며, Loop Manager는 내용 변경을 감지해 작업을 재개한다.
- `Validation`에는 Worker가 실행할 계획별 targeted unit test, 추가할 테스트 시나리오와 전체 verify가 확인해야 할 계획 고유의 회귀 조건만 기록한다.
- Loop Manager가 현재 운영체제의 전체 verify 스크립트를 항상 실행하므로 `scripts/verify.ps1`과 `scripts/verify.sh` 경로를 ExecPlan마다 반복하지 않는다.

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
