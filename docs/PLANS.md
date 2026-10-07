- 파일명 형식은 `docs/plans/[G-000] Title.md`다.
- ExecPlan 생성·계획 작성은 매니저가 호출하는 자동 Planner 흐름이 담당한다. 직접 작업·Specification Skill은 관련 Requirement를 작성·갱신하며 ExecPlan을 만들지 않는다. 자동 Planner는 현재 프로젝트에 필요한 ExecPlan을 하나 이상 완성된 형태로 작성할 수 있다.
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

## Planner 변경 범위

상태·소유권과 실행 mode는 프로그램이 제공한 snapshot과 runtime의 `docs/AGENTS.common.md`+해당 역할 지침을 합친 instruction을 기준으로 판단한다. 정적 역할·권한·검증 규칙은 해당 원본 파일에 두며 작업 prompt에는 작업별 문맥만 둔다.

일반 Planner는 새 `docs/plans/*.md`를 생성하고, 현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어만 `docs/FUTURE_IDEAS.md`에 생성하거나 수정할 수 있다.

예외적으로 프로그램이 SQLite 상태와 Git revision에 결합된 mutable READY snapshot을 제공한 경우에만 snapshot 안의 미착수 READY ExecPlan을 같은 ID로 수정·이름 변경·삭제할 수 있다. snapshot 원본과 현재 content hash·revision·무소유 상태가 일치해야 하며, snapshot 밖의 기존 ExecPlan·실행된 계획·MERGED·archive 계획과 앞에서 허용한 파일 외의 파일은 변경하지 않는다.

코드·설계·계획 변경으로 다른 ExecPlan의 경로·구현·검증·dependency가 영향을 받으면, Planner는 허용된 snapshot 안의 해당 계획도 함께 개정한다. 관련성이 있다는 이유로 snapshot 밖의 계획이나 실행 이력의 변경 권한을 확대하지 않는다.

예외적으로 프로그램이 SQLite의 활성 유지보수 주기에서 현재 단계를 `barrier` 또는 `post-refactor revalidation` mode로 지정하고 대상 snapshot을 제공한 경우에만 유지보수 Planner가 기존 ExecPlan을 개정할 수 있다. 대상은 SQLite 상태가 `READY`이고 branch, worktree, workspace reservation, Worker·Reviewer session과 PR이 없는 계획으로 제한한다. 저장된 content hash, plan revision SHA 또는 소유 상태가 snapshot과 다르면 개정을 거부한다.

- `barrier` mode는 기존 ID, 경로, title, sources, complexity, risk, priority, Purpose, Acceptance Criteria와 기존 `depends_on`을 보존하고, 검증된 리팩터링 DAG의 모든 말단 ID만 `depends_on`에 추가한다.
- `post-refactor revalidation` mode는 ID, 경로, title, sources, Purpose와 원래 제품 목표·범위를 보존한다. 최신 구조에 필요한 Acceptance Criteria의 검증된 경로 표현, Implementation, Validation, Progress, Decision Log와 dependency만 개정할 수 있으며 목표와 범위 보존을 확정할 수 없으면 거부한다.
- 유지보수 Planner도 기존 파일을 삭제·이름 변경하거나 대상 snapshot 밖의 계획를 수정할 수 없다. 이 예외는 일반 Planner의 권한을 넓히지 않는다.

사용자가 BLOCKED 해제나 명세 보완을 위해 기존 ExecPlan을 직접 편집한 결과는 유지보수 Planner의 개정 결과가 아니다. 저장소 파일이 변경됐다는 사실만으로 유지보수 예외를 부여하지 않으며, 프로그램이 선택한 mode, SQLite 대상 snapshot과 Agent 실행 전후 diff가 모두 일치할 때만 유지보수 Planner 결과로 검증·게시한다.

## 연관 문서의 정합성

Worker는 현재 작업에 제공된 문서 수정 권한 안에서 코드·설계·계획 변경에 필요한 연관 문서를 함께 갱신하고, 이유·영향과 검증 조건을 현재 ExecPlan에 기록한다. Planner는 위 변경 범위의 영향받는 ExecPlan 개정을 담당하고 Reviewer는 구현·문서·요청의 정합성을 읽기 전용으로 검토한다. 새 목표 추가·사용자 결정을 문서 수정으로 대신하지 않는다.

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
